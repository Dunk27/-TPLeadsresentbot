"""
Telegram-бот ФРАУ_КУХНИ
- Пересылает сообщения с тегами заказчикам в личку
- Админ-панель для управления тегами и заказчиками
- Еженедельный отчёт администратору
- Уведомление если заказчик не получил сообщение
- Render.com + Python 3.14 + python-telegram-bot v21 + PostgreSQL
"""
import logging
import re
import os
import asyncio
import threading
from datetime import datetime, time
from http.server import HTTPServer, BaseHTTPRequestHandler

import psycopg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes,
    ConversationHandler, JobQueue
)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_IDS       = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DATABASE_URL    = os.getenv("DATABASE_URL", "")
PORT            = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

WAIT_TAG, WAIT_CUSTOMER_ID, WAIT_CUSTOMER_NAME = range(3)


# ─── Health check ─────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args): pass

def run_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


# ─── База данных (PostgreSQL) ─────────────────────────────────────────────────

def get_conn():
    return psycopg.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS routes (
                    id            SERIAL PRIMARY KEY,
                    tag           TEXT UNIQUE NOT NULL,
                    customer_id   BIGINT NOT NULL,
                    customer_name TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS forward_log (
                    id           SERIAL PRIMARY KEY,
                    tag          TEXT,
                    customer_id  BIGINT,
                    message      TEXT,
                    delivered    BOOLEAN DEFAULT TRUE,
                    error_text   TEXT,
                    ts           TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()
    logger.info("✅ PostgreSQL БД инициализирована")


def get_all_routes():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, tag, customer_id, customer_name FROM routes ORDER BY tag")
            return cur.fetchall()

def get_route_by_tag(tag: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT customer_id, customer_name FROM routes WHERE tag = %s",
                (tag.upper(),)
            )
            return cur.fetchone()

def add_route(tag: str, customer_id: int, customer_name: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO routes (tag, customer_id, customer_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (tag) DO UPDATE
                SET customer_id = EXCLUDED.customer_id,
                    customer_name = EXCLUDED.customer_name
            """, (tag.upper(), customer_id, customer_name))
        conn.commit()

def delete_route(route_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM routes WHERE id = %s", (route_id,))
        conn.commit()

def log_forward(tag, customer_id, message, delivered=True, error_text=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO forward_log (tag, customer_id, message, delivered, error_text)
                   VALUES (%s, %s, %s, %s, %s)""",
                (tag, customer_id, message[:500], delivered, error_text)
            )
        conn.commit()

def get_weekly_stats():
    """Статистика за последние 7 дней."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE delivered = TRUE)  as ok,
                    COUNT(*) FILTER (WHERE delivered = FALSE) as failed,
                    COUNT(*) as total
                FROM forward_log
                WHERE ts >= NOW() - INTERVAL '7 days'
            """)
            total_row = cur.fetchone()

            cur.execute("""
                SELECT tag, COUNT(*) as cnt
                FROM forward_log
                WHERE ts >= NOW() - INTERVAL '7 days'
                GROUP BY tag ORDER BY cnt DESC LIMIT 10
            """)
            by_tag = cur.fetchall()

            cur.execute("""
                SELECT tag, customer_id, message, error_text, ts
                FROM forward_log
                WHERE delivered = FALSE
                  AND ts >= NOW() - INTERVAL '7 days'
                ORDER BY ts DESC LIMIT 5
            """)
            failed = cur.fetchall()

    return total_row, by_tag, failed


# ─── Вспомогательные функции ─────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ─── Пересылка сообщений ─────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text
    tags = re.findall(r"#[\w\u0400-\u04FF]+", text)
    if not tags:
        return

    for tag in tags:
        route = get_route_by_tag(tag)
        if not route:
            logger.info("⚠️ Тег %s не найден в маршрутах", tag)
            continue

        customer_id, customer_name = route
        try:
            await context.bot.send_message(
                chat_id=customer_id,
                text=f"📩 Новая заявка по тегу {tag}:\n\n{text}"
            )
            log_forward(tag, customer_id, text, delivered=True)
            logger.info("✅ Переслано [%s] → %s (%s)", tag, customer_name, customer_id)

        except Exception as e:
            error = str(e)
            log_forward(tag, customer_id, text, delivered=False, error_text=error)
            logger.error("❌ Ошибка пересылки [%s] → %s: %s", tag, customer_id, error)

            # 🔔 Уведомляем всех администраторов о недоставке
            alert = (
                f"🔔 <b>Заявка НЕ доставлена!</b>\n\n"
                f"Тег: <code>{tag}</code>\n"
                f"Заказчик: {customer_name} (<code>{customer_id}</code>)\n"
                f"Причина: <code>{error}</code>\n\n"
                f"📝 Сообщение:\n{text[:300]}"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=alert,
                        parse_mode="HTML"
                    )
                except Exception as ae:
                    logger.error("❌ Не удалось уведомить админа %s: %s", admin_id, ae)


# ─── Еженедельный отчёт ───────────────────────────────────────────────────────

async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Отправляется каждый понедельник в 09:00."""
    logger.info("📊 Отправка еженедельного отчёта...")
    total_row, by_tag, failed = get_weekly_stats()
    ok, fail, total = total_row

    text = (
        f"📊 <b>Еженедельный отчёт</b>\n"
        f"За последние 7 дней:\n\n"
        f"✅ Доставлено: <b>{ok}</b>\n"
        f"❌ Не доставлено: <b>{fail}</b>\n"
        f"📨 Всего заявок: <b>{total}</b>\n"
    )

    if by_tag:
        text += "\n📋 <b>По тегам:</b>\n"
        for tag, cnt in by_tag:
            text += f"  • {tag}: {cnt}\n"

    if failed:
        text += "\n⚠️ <b>Последние ошибки доставки:</b>\n"
        for tag, cid, msg, err, ts in failed:
            text += f"  • {tag} | {ts.strftime('%d.%m %H:%M')} | {err or 'неизвестно'}\n"

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error("❌ Не удалось отправить отчёт админу %s: %s", admin_id, e)


# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await show_admin_menu(update, context)
    else:
        await update.message.reply_text(
            f"👋 Привет!\n\n"
            f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
            f"Сообщите этот ID администратору для подключения к системе.",
            parse_mode="HTML"
        )


# ─── Команда /report (ручной отчёт) ──────────────────────────────────────────

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await send_weekly_report(context)


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

    keyboard = [
        [InlineKeyboardButton("➕ Добавить маршрут", callback_data="admin_add")],
        [InlineKeyboardButton("🗑 Удалить маршрут",  callback_data="admin_delete")],
        [InlineKeyboardButton("📊 Отчёт за неделю",  callback_data="admin_stats")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await show_admin_menu(update, context)


# ─── Добавление маршрута ──────────────────────────────────────────────────────

async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    await query.edit_message_text(
        "➕ <b>Добавление маршрута</b>\n\n"
        "Шаг 1/3: Введите тег (например: <code>#ФРАУ_КУХНИ</code>)\n\n"
        "Или /cancel для выхода",
        parse_mode="HTML"
    )
    return WAIT_TAG

async def admin_add_tag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tag = update.message.text.strip()
    if not tag.startswith("#"):
        tag = "#" + tag
    context.user_data["new_tag"] = tag.upper()
    await update.message.reply_text(
        f"✅ Тег: <code>{tag.upper()}</code>\n\n"
        f"Шаг 2/3: Введите <b>Telegram ID</b> заказчика.\n\n"
        f"💡 Заказчик узнает свой ID написав /start боту",
        parse_mode="HTML"
    )
    return WAIT_CUSTOMER_ID

async def admin_add_customer_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом. Попробуйте ещё раз:")
        return WAIT_CUSTOMER_ID
    context.user_data["new_customer_id"] = cid
    await update.message.reply_text(
        f"✅ ID: <code>{cid}</code>\n\n"
        f"Шаг 3/3: Введите имя заказчика (например: <i>Иван Петров</i>)",
        parse_mode="HTML"
    )
    return WAIT_CUSTOMER_NAME

async def admin_add_customer_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    tag  = context.user_data["new_tag"]
    cid  = context.user_data["new_customer_id"]
    add_route(tag, cid, name)
    await update.message.reply_text(
        f"✅ <b>Маршрут добавлен!</b>\n\n"
        f"Тег: <code>{tag}</code>\n"
        f"Заказчик: {name} (<code>{cid}</code>)",
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
    keyboard = [
        [InlineKeyboardButton("➕ Добавить маршрут", callback_data="admin_add")],
        [InlineKeyboardButton("🗑 Удалить маршрут",  callback_data="admin_delete")],
        [InlineKeyboardButton("📊 Отчёт за неделю",  callback_data="admin_stats")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.")
    await show_admin_menu_msg(update, context)
    return ConversationHandler.END


# ─── Удаление маршрута ────────────────────────────────────────────────────────

async def admin_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    routes = get_all_routes()
    if not routes:
        await query.edit_message_text("📋 Маршрутов нет.")
        return
    keyboard = [
        [InlineKeyboardButton(f"🗑 {tag} → {cname}", callback_data=f"del_{rid}")]
        for rid, tag, cid, cname in routes
    ]
    keyboard.append([InlineKeyboardButton("« Назад", callback_data="admin_back")])
    await query.edit_message_text(
        "🗑 <b>Выберите маршрут для удаления:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )

async def admin_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    route_id = int(query.data.split("_")[1])
    delete_route(route_id)
    await query.answer("✅ Маршрут удалён", show_alert=True)
    await show_admin_menu(update, context)


# ─── Статистика ───────────────────────────────────────────────────────────────

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    await send_weekly_report(context)
    keyboard = [[InlineKeyboardButton("« Назад", callback_data="admin_back")]]
    await query.edit_message_text(
        "📊 Отчёт отправлен вам в чат.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─── Запуск ──────────────────────────────────────────────────────────────────

async def run_bot():
    init_db()

    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN не задан!")
        return
    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL не задан!")
        return
    if not ADMIN_IDS:
        logger.warning("⚠️ ADMIN_IDS не задан!")

    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("✅ Health-сервер запущен на порту %s", PORT)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Еженедельный отчёт — каждый понедельник в 09:00
    app.job_queue.run_daily(
        send_weekly_report,
        time=time(hour=9, minute=0),
        days=(0,),  # 0 = понедельник
        name="weekly_report"
    )

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern="^admin_add$")],
        states={
            WAIT_TAG:           [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_tag)],
            WAIT_CUSTOMER_ID:   [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_customer_id)],
            WAIT_CUSTOMER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_customer_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("admin",  cmd_admin))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(admin_delete_menu,    pattern="^admin_delete$"))
    app.add_handler(CallbackQueryHandler(admin_stats,          pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_delete_confirm, pattern=r"^del_\d+$"))
    app.add_handler(CallbackQueryHandler(show_admin_menu,      pattern="^admin_back$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Бот запущен...")
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run_bot())
