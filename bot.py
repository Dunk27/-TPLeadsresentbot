"""
Telegram-бот ФРАУ_КУХНИ
- Пересылает сообщения с тегами заказчикам в личку
- Админ-панель для управления тегами и заказчиками
- Render.com + Python 3.14 + python-telegram-bot v21
"""
import logging
import sqlite3
import re
import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler
)

TELEGRAM_TOKEN = os.getenv("8731218498:AAGBs5axqXN2fnxY-u5ATftmKCOdsp17KWI", "")
# ID администраторов через запятую: "123456789,987654321"
ADMIN_IDS = [int(x) for x in os.getenv("756974370", "").split(",") if x.strip()]
DB_FILE = os.getenv("DB_FILE", "clients.db")
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Состояния ConversationHandler
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


# ─── База данных ─────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        # Таблица: тег → заказчик
        conn.execute("""
            CREATE TABLE IF NOT EXISTS routes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                tag           TEXT UNIQUE NOT NULL,
                customer_id   INTEGER NOT NULL,
                customer_name TEXT NOT NULL
            )
        """)
        # Лог пересылок
        conn.execute("""
            CREATE TABLE IF NOT EXISTS forward_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tag        TEXT,
                customer_id INTEGER,
                message    TEXT,
                ts         DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    logger.info("✅ БД инициализирована")


def get_all_routes():
    with sqlite3.connect(DB_FILE) as conn:
        return conn.execute(
            "SELECT id, tag, customer_id, customer_name FROM routes ORDER BY tag"
        ).fetchall()


def get_route_by_tag(tag: str):
    with sqlite3.connect(DB_FILE) as conn:
        return conn.execute(
            "SELECT customer_id, customer_name FROM routes WHERE tag = ?",
            (tag.upper(),)
        ).fetchone()


def add_route(tag: str, customer_id: int, customer_name: str):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO routes (tag, customer_id, customer_name) VALUES (?,?,?)",
            (tag.upper(), customer_id, customer_name)
        )
        conn.commit()


def delete_route(route_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        conn.commit()


def log_forward(tag, customer_id, message):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO forward_log (tag, customer_id, message) VALUES (?,?,?)",
            (tag, customer_id, message[:500])
        )
        conn.commit()


# ─── Проверка прав ────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ─── Основная логика пересылки ────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text
    # Ищем все теги в сообщении
    tags = re.findall(r"#[\w\u0400-\u04FF]+", text)
    if not tags:
        return

    forwarded = False
    for tag in tags:
        route = get_route_by_tag(tag)
        if route:
            customer_id, customer_name = route
            try:
                # Пересылаем с подписью
                header = f"📩 Новая заявка по тегу {tag}:\n\n"
                await context.bot.send_message(
                    chat_id=customer_id,
                    text=header + text
                )
                log_forward(tag, customer_id, text)
                logger.info("✅ Переслано [%s] → %s (%s)", tag, customer_name, customer_id)
                forwarded = True
            except Exception as e:
                logger.error("❌ Ошибка пересылки [%s] → %s: %s", tag, customer_id, e)

    if not forwarded and tags:
        logger.info("⚠️ Теги %s не найдены в маршрутах", tags)


# ─── Команда /start ───────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await show_admin_menu(update, context)
    else:
        # Показываем chat_id пользователя (нужно для настройки маршрутов)
        await update.message.reply_text(
            f"👋 Привет!\n\n"
            f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
            f"Сообщите этот ID администратору для подключения к системе.",
            parse_mode="HTML"
        )


# ─── Админ-панель ─────────────────────────────────────────────────────────────

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    routes = get_all_routes()
    text = "⚙️ <b>Админ-панель</b>\n\n"

    if routes:
        text += "📋 <b>Текущие маршруты:</b>\n"
        for r in routes:
            rid, tag, cid, cname = r
            text += f"  • {tag} → {cname} (<code>{cid}</code>)\n"
    else:
        text += "📋 Маршруты пока не настроены.\n"

    keyboard = [
        [InlineKeyboardButton("➕ Добавить маршрут", callback_data="admin_add")],
        [InlineKeyboardButton("🗑 Удалить маршрут", callback_data="admin_delete")],
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
    ]

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )


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
        "Или /отмена для выхода",
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
        f"💡 Заказчик может узнать свой ID написав боту /start",
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
        f"Шаг 3/3: Введите имя заказчика (для удобства, например: <i>Иван Петров</i>)",
        parse_mode="HTML"
    )
    return WAIT_CUSTOMER_NAME


async def admin_add_customer_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    tag = context.user_data["new_tag"]
    cid = context.user_data["new_customer_id"]

    add_route(tag, cid, name)

    await update.message.reply_text(
        f"✅ <b>Маршрут добавлен!</b>\n\n"
        f"Тег: <code>{tag}</code>\n"
        f"Заказчик: {name} (<code>{cid}</code>)\n\n"
        f"Теперь все сообщения с тегом {tag} будут пересылаться этому заказчику.",
        parse_mode="HTML"
    )
    # Показываем меню снова
    await show_admin_menu_text(update, context)
    return ConversationHandler.END


async def show_admin_menu_text(update, context):
    routes = get_all_routes()
    text = "⚙️ <b>Админ-панель</b>\n\n"
    if routes:
        text += "📋 <b>Текущие маршруты:</b>\n"
        for r in routes:
            rid, tag, cid, cname = r
            text += f"  • {tag} → {cname} (<code>{cid}</code>)\n"
    keyboard = [
        [InlineKeyboardButton("➕ Добавить маршрут", callback_data="admin_add")],
        [InlineKeyboardButton("🗑 Удалить маршрут", callback_data="admin_delete")],
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
    ]
    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.")
    await show_admin_menu_text(update, context)
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

    keyboard = []
    for r in routes:
        rid, tag, cid, cname = r
        keyboard.append([InlineKeyboardButton(
            f"🗑 {tag} → {cname}",
            callback_data=f"del_{rid}"
        )])
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

    with sqlite3.connect(DB_FILE) as conn:
        total = conn.execute("SELECT COUNT(*) FROM forward_log").fetchone()[0]
        by_tag = conn.execute(
            "SELECT tag, COUNT(*) as cnt FROM forward_log GROUP BY tag ORDER BY cnt DESC LIMIT 10"
        ).fetchall()

    text = f"📊 <b>Статистика пересылок</b>\n\nВсего пересылок: <b>{total}</b>\n\n"
    if by_tag:
        text += "По тегам:\n"
        for tag, cnt in by_tag:
            text += f"  • {tag}: {cnt}\n"

    keyboard = [[InlineKeyboardButton("« Назад", callback_data="admin_back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


# ─── Запуск ──────────────────────────────────────────────────────────────────

async def run_bot():
    init_db()

    if not TELEGRAM_TOKEN:
        logger.error("❌ Переменная TELEGRAM_TOKEN не задана!")
        return

    if not ADMIN_IDS:
        logger.warning("⚠️ ADMIN_IDS не задан! Добавьте переменную окружения ADMIN_IDS.")

    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("✅ Health-сервер запущен на порту %s", PORT)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Диалог добавления маршрута
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern="^admin_add$")],
        states={
            WAIT_TAG:           [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_tag)],
            WAIT_CUSTOMER_ID:   [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_customer_id)],
            WAIT_CUSTOMER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_customer_name)],
        },
        fallbacks=[CommandHandler("отмена", cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(admin_delete_menu,    pattern="^admin_delete$"))
    app.add_handler(CallbackQueryHandler(admin_stats,          pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_delete_confirm, pattern=r"^del_\d+$"))
    app.add_handler(CallbackQueryHandler(show_admin_menu,      pattern="^admin_back$"))
    # Обработчик входящих сообщений с тегами — последний
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Бот запущен...")
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run_bot())
