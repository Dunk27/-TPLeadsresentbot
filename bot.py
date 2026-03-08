"""
Telegram-бот для фильтрации лидов (Т-профит / ФРАУ_КУХНИ).
Исправленная версия. Внешние зависимости: python-telegram-bot==13.x
"""

import logging
import sqlite3
import re
import os

# ─── Настройки ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TARGET_TAG = "#ФРАУ_КУХНИ"
DB_FILE = os.getenv("DB_FILE", "clients.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ─── База данных ─────────────────────────────────────────────────────────────

def init_db(db_file=DB_FILE):
    """Создаёт таблицу clients, если её нет."""
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
    logger.info("✅ БД инициализирована: %s", db_file)


def save_client(phone, username, region, budget, tags, db_file=DB_FILE):
    """Сохраняет нового клиента. Возвращает его id."""
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
    """Ищет клиента по телефону ИЛИ username. Возвращает id или None."""
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


# ─── Парсинг сообщений ────────────────────────────────────────────────────────

def extract_client_data(message_text):
    """Разбирает сообщение → (phone, username, region, budget, tags)."""
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


# ─── Отправка ────────────────────────────────────────────────────────────────

def send_private_message(bot, chat_id, message):
    """Отправляет сообщение. Возвращает True/False."""
    try:
        bot.send_message(chat_id=chat_id, text=message)
        logger.info("✅ Отправлено → %s", chat_id)
        return True
    except Exception as exc:
        logger.error("❌ Ошибка → %s: %s", chat_id, exc)
        return False


# ─── Обработчик ──────────────────────────────────────────────────────────────

def handle_message(update, context):
    """Точка входа для telegram.ext."""
    message = update.message
    if not message or not message.text:
        return

    text = message.text
    if TARGET_TAG.upper() not in text.upper():
        logger.warning("⚠️ Нет тега — пропускаем")
        return

    phone, username, region, budget, tags = extract_client_data(text)
    client_id = find_client(phone, username)

    if client_id:
        send_private_message(context.bot, client_id, text)
    else:
        logger.warning("⚠️ Клиент не найден — сохраняем")
        save_client(phone, username, region, budget, tags)


# ─── Запуск ──────────────────────────────────────────────────────────────────

def main():
    from telegram.ext import Updater, MessageHandler, Filters

    init_db()
    if TELEGRAM_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        logger.error("❌ Установите переменную TELEGRAM_TOKEN!")
        return

    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    updater.dispatcher.add_handler(
        MessageHandler(Filters.text & ~Filters.command, handle_message)
    )
    logger.info("🤖 Бот запущен...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
