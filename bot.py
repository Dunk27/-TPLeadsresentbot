"""
Telegram-бот ФРАУ_КУХНИ
- Webhook режим (один процесс, нет конфликтов)
- Пересылает заявки с тегами заказчикам в личку
- Кнопки Принял / Отклонил / Комментарий
- Дедлайн 2 часа — напоминание заказчику и уведомление админу
- Еженедельный отчёт
- Render.com + Python 3.14 + python-telegram-bot v21 + PostgreSQL
"""
import logging
import re
import os
import asyncio
from datetime import time

import psycopg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_IDS      = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DATABASE_URL   = os.getenv("DATABASE_URL", "")
PORT           = int(os.getenv("PORT", 10000))
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "")  # https://tp-leads-bot.onrender.com
DEADLINE_HOURS = 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

WAIT_TAG, WAIT_CUSTOMER_ID, WAIT_CUSTOMER_NAME = range(3)
WAIT_COMMENT = 10


# ─── База данных ─────────────────────────────────────────────────────────────

def get_conn():
    url = DATABASE_URL
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url = url + sep + "sslmode=require"
    return psycopg.connect(url)

def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS routes (
                id            SERIAL PRIMARY KEY,
                tag           TEXT UNIQUE NOT NULL,
                customer_id   BIGINT NOT NULL,
                customer_name TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id                SERIAL PRIMARY KEY,
                tag               TEXT,
                manager_chat_id   BIGINT,
                customer_id       BIGINT,
                customer_name     TEXT,
                message           TEXT,
                status            TEXT DEFAULT 'pending',
                bot_message_id    BIGINT,
                ts                TIMESTAMP DEFAULT NOW(),
                deadline_notified BOOLEAN DEFAULT FALSE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS forward_log (
                id          SERIAL PRIMARY KEY,
                tag         TEXT,
                customer_id BIGINT,
                message     TEXT,
                delivered   BOOLEAN DEFAULT TRUE,
                error_text  TEXT,
                ts          TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
    logger.info("✅ PostgreSQL БД инициализирована")

def get_all_routes():
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, tag, customer_id, customer_name FROM routes ORDER BY tag"
        ).fetchall()

def get_route_by_tag(tag):
    with get_conn() as conn:
        return conn.execute(
            "SELECT customer_id, customer_name FROM routes WHERE tag = %s", (tag.upper(),)
        ).fetchone()

def add_route(tag, customer_id, customer_name):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO routes (tag, customer_id, customer_name) VALUES (%s, %s, %s)
            ON CONFLICT (tag) DO UPDATE
            SET customer_id = EXCLUDED.customer_id, customer_name = EXCLUDED.customer_name
        """, (tag.upper(), customer_id, customer_name))
        conn.commit()

def delete_route(route_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM routes WHERE id = %s", (route_id,))
        conn.commit()

def save_lead(tag, manager_chat_id, customer_id, customer_name, message, bot_message_id):
    with get_conn() as conn:
        row = conn.execute("""
            INSERT INTO leads (tag, manager_chat_id, customer_id, customer_name, message, bot_message_id)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """, (tag, manager_chat_id, customer_id, customer_name, message[:1000], bot_message_id)).fetchone()
        conn.commit()
        return row[0]

def get_lead(lead_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM leads WHERE id = %s", (lead_id,)).fetchone()

def update_lead_status(lead_id, status):
    with get_conn() as conn:
        conn.execute("UPDATE leads SET status = %s WHERE id = %s", (status, lead_id))
        conn.commit()

def get_pending_leads_overdue():
    with get_conn() as conn:
        return conn.execute("""
            SELECT id, tag, manager_chat_id, customer_id, customer_name, message
            FROM leads
            WHERE status = 'pending'
              AND deadline_notified = FALSE
              AND ts < NOW() - INTERVAL '%s hours'
        """, (DEADLINE_HOURS,)).fetchall()

def mark_deadline_notified(lead_id):
    with get_conn() as conn:
        conn.execute("UPDATE leads SET deadline_notified = TRUE WHERE id = %s", (lead_id,))
        conn.commit()

def log_forward(tag, customer_id, message, delivered=True, error_text=None):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO forward_log (tag, customer_id, message, delivered, error_text)
            VALUES (%s, %s, %s, %s, %s)
        """, (tag, customer_id, message[:500], delivered, error_text))
        conn.commit()

def get_weekly_stats():
    with get_conn() as conn:
        total_row = conn.execute("""
            SELECT
                COUNT(*) FILTER (WHERE delivered = TRUE),
                COUNT(*) FILTER (WHERE delivered = FALSE),
                COUNT(*)
            FROM forward_log WHERE ts >= NOW() - INTERVAL '7 days'
        """).fetchone()
        by_tag = conn.execute("""
            SELECT tag, COUNT(*) FROM forward_log
            WHERE ts >= NOW() - INTERVAL '7 days'
            GROUP BY tag ORDER BY 2 DESC LIMIT 10
        """).fetchall()
        by_status = conn.execute("""
            SELECT status, COUNT(*) FROM leads
            WHERE ts >= NOW() - INTERVAL '7 days'
            GROUP BY status
        """).fetchall()
    return total_row, by_tag, by_status

def is_admin(user_id): return user_id in ADMIN_IDS


# ─── Пересылка заявки ────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    text = message.text
    tags = re.findall(r"#[\w\u0400-\u04FF]+", text)
    if not tags:
        return

    manager_chat_id = message.chat_id
    for tag in tags:
        route = get_route_by_tag(tag)
        if not route:
            logger.info("⚠️ Тег %s не найден", tag)
            continue
        customer_id, customer_name = route
        try:
            sent = await context.bot.send_message(
                chat_id=customer_id,
                text=f"📩 <b>Новая заявка</b> по тегу {tag}:\n\n{text}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Принял",      callback_data="lead_accept_0"),
                    InlineKeyboardButton("❌ Отклонил",    callback_data="lead_decline_0"),
                    InlineKeyboardButton("💬 Комментарий", callback_data="lead_comment_0"),
                ]]),
                parse_mode="HTML"
            )
            lead_id = save_lead(tag, manager_chat_id, customer_id, customer_name, text, sent.message_id)
            await context.bot.edit_message_reply_markup(
                chat_id=customer_id,
                message_id=sent.message_id,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Принял",      callback_data=f"lead_accept_{lead_id}"),
                    InlineKeyboardButton("❌ Отклонил",    callback_data=f"lead_decline_{lead_id}"),
                    InlineKeyboardButton("💬 Комментарий", callback_data=f"lead_comment_{lead_id}"),
                ]])
            )
            log_forward(tag, customer_id, text, delivered=True)
            logger.info("✅ Заявка #%s [%s] → %s", lead_id, tag, customer_name)
        except Exception as e:
            error = str(e)
            log_forward(tag, customer_id, text, delivered=False, error_text=error)
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🔔 <b>Заявка НЕ доставлена!</b>\n\nТег: <code>{tag}</code>\nЗаказчик: {customer_name}\nПричина: <code>{error}</code>\n\n{text[:300]}",
                        parse_mode="HTML"
                    )
                except: pass


# ─── Кнопки заказчика ────────────────────────────────────────────────────────

async def lead_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("✅ Статус обновлён")
    lead_id = int(query.data.split("_")[2])
    lead = get_lead(lead_id)
    if not lead: return
    update_lead_status(lead_id, "accepted")
    await query.edit_message_text(
        query.message.text + "\n\n✅ <b>Вы приняли эту заявку</b>", parse_mode="HTML"
    )
    try:
        await context.bot.send_message(
            chat_id=lead[2],
            text=f"✅ <b>Заявка принята!</b>\n\nТег: <code>{lead[1]}</code>\nЗаказчик: {lead[4]}\n\n{lead[5][:300]}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error("Ошибка уведомления менеджера: %s", e)

async def lead_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("❌ Статус обновлён")
    lead_id = int(query.data.split("_")[2])
    lead = get_lead(lead_id)
    if not lead: return
    update_lead_status(lead_id, "declined")
    await query.edit_message_text(
        query.message.text + "\n\n❌ <b>Вы отклонили эту заявку</b>", parse_mode="HTML"
    )
    try:
        await context.bot.send_message(
            chat_id=lead[2],
            text=f"❌ <b>Заявка отклонена!</b>\n\nТег: <code>{lead[1]}</code>\nЗаказчик: {lead[4]}\n\n{lead[5][:300]}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error("Ошибка уведомления менеджера: %s", e)

async def lead_comment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lead_id = int(query.data.split("_")[2])
    context.user_data["comment_lead_id"] = lead_id
    await context.bot.send_message(
        chat_id=query.from_user.id,
        text="💬 Напишите комментарий к заявке — он будет отправлен менеджеру:"
    )
    return WAIT_COMMENT

async def lead_comment_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lead_id = context.user_data.get("comment_lead_id")
    if not lead_id: return ConversationHandler.END
    lead = get_lead(lead_id)
    if not lead:
        await update.message.reply_text("⚠️ Заявка не найдена.")
        return ConversationHandler.END
    try:
        await context.bot.send_message(
            chat_id=lead[2],
            text=f"💬 <b>Комментарий от заказчика</b>\n\nТег: <code>{lead[1]}</code>\nЗаказчик: {lead[4]}\n\n📝 Заявка:\n{lead[5][:200]}\n\n💬 Комментарий:\n{update.message.text}",
            parse_mode="HTML"
        )
        await update.message.reply_text("✅ Комментарий отправлен менеджеру!")
    except Exception as e:
        await update.message.reply_text("⚠️ Не удалось отправить комментарий.")
    return ConversationHandler.END


# ─── Дедлайн ─────────────────────────────────────────────────────────────────

async def check_deadlines(context: ContextTypes.DEFAULT_TYPE):
    for lead in get_pending_leads_overdue():
        lead_id, tag, manager_chat_id, customer_id, customer_name, message = lead
        try:
            await context.bot.send_message(
                chat_id=customer_id,
                text=f"⏰ <b>Напоминание!</b>\n\nВы не ответили на заявку по тегу <code>{tag}</code>.\n\n{message[:300]}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Принял",      callback_data=f"lead_accept_{lead_id}"),
                    InlineKeyboardButton("❌ Отклонил",    callback_data=f"lead_decline_{lead_id}"),
                    InlineKeyboardButton("💬 Комментарий", callback_data=f"lead_comment_{lead_id}"),
                ]]),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error("Ошибка напоминания: %s", e)
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"⏰ <b>Заявка не обработана {DEADLINE_HOURS} ч!</b>\n\nТег: <code>{tag}</code>\nЗаказчик: {customer_name} (<code>{customer_id}</code>)\n\n{message[:300]}",
                    parse_mode="HTML"
                )
            except: pass
        mark_deadline_notified(lead_id)


# ─── Отчёт ───────────────────────────────────────────────────────────────────

async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    total_row, by_tag, by_status = get_weekly_stats()
    ok, fail, total = total_row
    status_map = {"pending": "⏳ Ожидают", "accepted": "✅ Приняты", "declined": "❌ Отклонены"}
    text = f"📊 <b>Еженедельный отчёт</b>\n\n📨 Всего: <b>{total}</b>\n✅ Доставлено: <b>{ok}</b>\n❌ Не доставлено: <b>{fail}</b>\n"
    if by_status:
        text += "\n📋 <b>По статусам:</b>\n"
        for status, cnt in by_status:
            text += f"  • {status_map.get(status, status)}: {cnt}\n"
    if by_tag:
        text += "\n🏷 <b>По тегам:</b>\n"
        for tag, cnt in by_tag:
            text += f"  • {tag}: {cnt}\n"
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error("Ошибка отчёта: %s", e)


# ─── Команды ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await show_admin_menu(update, context)
    else:
        await update.message.reply_text(
            f"👋 Привет!\n\nВаш Telegram ID: <code>{user_id}</code>\n\nСообщите этот ID администратору.",
            parse_mode="HTML"
        )

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await send_weekly_report(context)

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await show_admin_menu(update, context)


# ─── Админ-панель ─────────────────────────────────────────────────────────────

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    routes = get_all_routes()
    text = "⚙️ <b>Админ-панель</b>\n\n"
    if routes:
        text += "📋 <b>Текущие маршруты:</b>\n"
        for rid, tag, cid, cname in routes:
            text += f"  • {tag} → {cname} (<code>{cid}</code>)\n"
    else:
        text += "📋 Маршруты пока не настроены.\n"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить маршрут", callback_data="admin_add")],
        [InlineKeyboardButton("🗑 Удалить маршрут",  callback_data="admin_delete")],
        [InlineKeyboardButton("📊 Отчёт за неделю",  callback_data="admin_stats")],
    ])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    await query.edit_message_text(
        "➕ <b>Добавление маршрута</b>\n\nШаг 1/3: Введите тег (например: <code>#ФРАУ_КУХНИ</code>)\n\nИли /cancel для выхода",
        parse_mode="HTML"
    )
    return WAIT_TAG

async def admin_add_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tag = update.message.text.strip()
    if not tag.startswith("#"): tag = "#" + tag
    context.user_data["new_tag"] = tag.upper()
    await update.message.reply_text(
        f"✅ Тег: <code>{tag.upper()}</code>\n\nШаг 2/3: Введите <b>Telegram ID</b> заказчика.\n\n💡 Заказчик узнает ID написав /start боту",
        parse_mode="HTML"
    )
    return WAIT_CUSTOMER_ID

async def admin_add_customer_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом:")
        return WAIT_CUSTOMER_ID
    context.user_data["new_customer_id"] = cid
    await update.message.reply_text(
        f"✅ ID: <code>{cid}</code>\n\nШаг 3/3: Введите имя заказчика",
        parse_mode="HTML"
    )
    return WAIT_CUSTOMER_NAME

async def admin_add_customer_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    tag  = context.user_data["new_tag"]
    cid  = context.user_data["new_customer_id"]
    add_route(tag, cid, name)
    await update.message.reply_text(
        f"✅ <b>Маршрут добавлен!</b>\n\nТег: <code>{tag}</code>\nЗаказчик: {name} (<code>{cid}</code>)",
        parse_mode="HTML"
    )
    await show_admin_menu_msg(update, context)
    return ConversationHandler.END

async def show_admin_menu_msg(update, context):
    routes = get_all_routes()
    text = "⚙️ <b>Админ-панель</b>\n\n"
    if routes:
        text += "📋 <b>Текущие маршруты:</b>\n"
        for rid, tag, cid, cname in routes:
            text += f"  • {tag} → {cname} (<code>{cid}</code>)\n"
    else:
        text += "📋 Маршруты пока не настроены.\n"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить маршрут", callback_data="admin_add")],
        [InlineKeyboardButton("🗑 Удалить маршрут",  callback_data="admin_delete")],
        [InlineKeyboardButton("📊 Отчёт за неделю",  callback_data="admin_stats")],
    ]), parse_mode="HTML")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.")
    await show_admin_menu_msg(update, context)
    return ConversationHandler.END

async def admin_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    routes = get_all_routes()
    if not routes:
        await query.edit_message_text("📋 Маршрутов нет.")
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🗑 {tag} → {cname}", callback_data=f"del_{rid}")]
         for rid, tag, cid, cname in routes] +
        [[InlineKeyboardButton("« Назад", callback_data="admin_back")]]
    )
    await query.edit_message_text("🗑 <b>Выберите маршрут для удаления:</b>",
                                   reply_markup=keyboard, parse_mode="HTML")

async def admin_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    delete_route(int(query.data.split("_")[1]))
    await query.answer("✅ Маршрут удалён", show_alert=True)
    await show_admin_menu(update, context)

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    await send_weekly_report(context)
    await query.edit_message_text(
        "📊 Отчёт отправлен вам в чат.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="admin_back")]])
    )


# ─── Запуск через Webhook ─────────────────────────────────────────────────────

async def run_bot():
    init_db()
    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN не задан!"); return
    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL не задан!"); return
    if not WEBHOOK_URL:
        logger.error("❌ WEBHOOK_URL не задан!"); return
    if not ADMIN_IDS:
        logger.warning("⚠️ ADMIN_IDS не задан!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Проверка дедлайнов каждые 30 минут
    app.job_queue.run_repeating(check_deadlines, interval=1800, first=60)
    # Еженедельный отчёт — понедельник 09:00
    app.job_queue.run_daily(send_weekly_report, time=time(9, 0), days=(0,))

    route_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern="^admin_add$")],
        states={
            WAIT_TAG:           [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_tag)],
            WAIT_CUSTOMER_ID:   [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_customer_id)],
            WAIT_CUSTOMER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_customer_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    comment_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lead_comment_start, pattern=r"^lead_comment_\d+$")],
        states={
            WAIT_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, lead_comment_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("admin",  cmd_admin))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(route_conv)
    app.add_handler(comment_conv)
    app.add_handler(CallbackQueryHandler(lead_accept,          pattern=r"^lead_accept_\d+$"))
    app.add_handler(CallbackQueryHandler(lead_decline,         pattern=r"^lead_decline_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_delete_menu,    pattern="^admin_delete$"))
    app.add_handler(CallbackQueryHandler(admin_stats,          pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_delete_confirm, pattern=r"^del_\d+$"))
    app.add_handler(CallbackQueryHandler(show_admin_menu,      pattern="^admin_back$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    webhook_path = f"/webhook/{TELEGRAM_TOKEN}"
    full_webhook_url = WEBHOOK_URL.rstrip("/") + webhook_path

    logger.info("🤖 Запуск через webhook: %s", full_webhook_url)

    async with app:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.start()
        await app.updater.start_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=full_webhook_url,
        )
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run_bot())