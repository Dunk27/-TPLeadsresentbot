"""
Telegram-бот ФРАУ_КУХНИ
Версия для Render.com + Python 3.14
python-telegram-bot v21
"""
import logging
import sqlite3
import re
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TARGET_TAG = "#ФРАУ_КУХНИ"
DB_FILE = os.getenv("DB_FILE", "clients.db")
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Health check (Render требует открытый порт) ──────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info("✅ Health-сервер запущен на порту %s", PORT)
    server.serve_forever()


# ─── База данных ─────────────────────────────────────────────────────────────

def init_db(db_file=DB_FILE):
    with sqlite3.connect(db_file) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                phone    TEXT,
                username TEXT,
                region   TEXT,
                budget   TEXT,
                tags     TEXT
            )
        """)
        conn.commit()
    logger.info("✅ БД инициализирована")


def save_client(phone, username, region, budget, tags, db_file=DB_FILE):
    tags_str = ",".join(tags) if tags else ""
    with sqlite3.connect(db_file) as conn:
        cur = conn.execute(
            "INSERT INTO clients (phone, username, region, budget, tags) VALUES (?,?,?,?,?)",
            (phone, username, region, budget, tags_str),
        )
        conn.commit()
        new_id = cur.lastrowid
    logger.info("✅ Клиент сохранён id=%s", new_id)
    return new_id


def find_client(phone, username, db_file=DB_FILE):
    conditions, params = [], []
    if phone:
        conditions.append("phone = ?")
        params.append(phone)
    if username:
        conditions.append("username = ?")
        params.append(username)
    if not conditions:
        return None
    sql = f"SELECT id FROM clients WHERE {' OR '.join(conditions)} LIMIT 1"
    with sqlite3.connect(db_file) as conn:
        row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


# ─── Парсинг ─────────────────────────────────────────────────────────────────

def extract_client_data(message_text):
    if not message_text:
        return None, None, None, None, []

    tags = re.findall(r"#[\w\u0400-\u04FF]+", message_text)

    phone_m = re.search(r"\+7\d{10}|\+380\d{9}|\+1\d{10}", message_text)
    phone = phone_m.group(0) if phone_m else None

    username_m = re.search(r"@[\w\u0400-\u04FF]+", message_text)
    username = username_m.group(0) if username_m else None

    region_m = re.search(
        r"(?:город|г\.?)\s+([\w\u0400-\u04FF\-]+)", message_text, re.IGNORECASE
    )
    region = region_m.group(1) if region_m else None

    budget_m = re.search(r"[Бб]юджет\s+([^\n]+)", message_text)
    budget = budget_m.group(1).strip() if budget_m else None

    return phone, username, region, budget, tags


# ─── Обработчик ──────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text
    if TARGET_TAG.upper() not in text.upper():
        return

    phone, username, region, budget, tags = extract_client_data(text)
    logger.info("📋 phone=%s username=%s region=%s budget=%s", phone, username, region, budget)

    client_id = find_client(phone, username)
    if client_id:
        try:
            await context.bot.send_message(chat_id=client_id, text=text)
            logger.info("✅ Сообщение отправлено → %s", client_id)
        except Exception as e:
            logger.error("❌ Ошибка отправки: %s", e)
    else:
        logger.warning("⚠️ Клиент не найден — сохраняем")
        save_client(phone, username, region, budget, tags)


# ─── Запуск ──────────────────────────────────────────────────────────────────

def main():
    init_db()

    if not TELEGRAM_TOKEN:
        logger.error("❌ Переменная TELEGRAM_TOKEN не задана!")
        return

    # Health-сервер в отдельном потоке
    threading.Thread(target=run_health_server, daemon=True).start()

    # Бот v21
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
