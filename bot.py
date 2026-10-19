import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from pyrogram import Client

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Конфигурация (замените на свои данные или используйте .env)
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "1234567"))      # Необходим для Pyrogram
API_HASH = os.getenv("API_HASH", "YOUR_API_HASH") # Необходим для Pyrogram
CHAT_ID = int(os.getenv("CHAT_ID", "-100123456789"))
DB_PATH = "reports.db"

# Инициализация Aiogram (Основной фреймворк)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализация Pyrogram (Только для вычитывания истории при старте)
app = Client("knnvdsky_sync", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

class BotState:
    def __init__(self):
        self.can_speak = True
        self.is_after_hard_mute = False

bot_state = BotState()

# ==========================================
# РАБОТА С БАЗОЙ ДАННЫХ (SQLite)
# ==========================================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY,
                last_sync_time TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_name TEXT,
                report_text TEXT,
                timestamp TEXT
            )
        ''')
        # Инициализация времени, если БД пустая
        cursor.execute('SELECT last_sync_time FROM state WHERE id = 1')
        if not cursor.fetchone():
            cursor.execute('INSERT INTO state (id, last_sync_time) VALUES (1, ?)', (datetime.now().isoformat(),))
        conn.commit()

def get_last_sync_time() -> datetime:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT last_sync_time FROM state WHERE id = 1')
        result = cursor.fetchone()
        return datetime.fromisoformat(result[0]) if result else datetime.now()

def update_last_sync_time():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE state SET last_sync_time = ? WHERE id = 1', (datetime.now().isoformat(),))
        conn.commit()

def save_report_to_db(store_name: str, report_text: str, msg_date: datetime):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO reports (store_name, report_text, timestamp) VALUES (?, ?, ?)',
                       (store_name, report_text, msg_date.isoformat()))
        conn.commit()

# ==========================================
# ПАРСЕР ОТЧЕТОВ И ЛОГИКА
# ==========================================
def is_report_message(text: str) -> bool:
    if not text:
        return False
    # Поддержка форматов "ТО: 123", "ТО : 123", "ТО ПЛАН", "План ТО"
    return bool(re.search(r'(?i)(ТО\s*[:/]?\s*\d+|ТО\s+ПЛАН|ПЛАН\s+ТО)', text))

def extract_store_name(message) -> str:
    # Заглушка: в реальности можно брать из sender_chat, имени пользователя или парсить из текста
    if message.sender_chat:
        return message.sender_chat.title
    if message.from_user:
        return f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()
    return "Неизвестная ТТ"

def process_report_data(message_text: str, store_name: str, msg_date: datetime):
    # Логика обработки и сохранения цифр (Утро/Обед/Вечер)
    save_report_to_db(store_name, message_text, msg_date)
    logging.info(f"Отчет от {store_name} успешно учтен в базе.")

# ==========================================
# ГЕЙМИФИКАЦИЯ (Генерация ответов)
# ==========================================
async def handle_gamification(message: Message, store_name: str):
    if not bot_state.can_speak:
        return  # Тихий режим (дозагрузка истории или принудительный мут)

    text = message.text.lower()
    
    # Реакции
    try:
        await message.react(reaction=[{"type": "emoji", "emoji": "🔥"}])
    except Exception as e:
        logging.warning(f"Не удалось поставить реакцию: {e}")

    # Примеры геймификации ОФ2
    if "рассрочк" in text and "доп" in text:
        await message.reply("🥊 Гусев может дожать на рассрочку с допами даже неработающий банкомат. Отличная работа!")
    elif "безлимит" in text or "лицензи" in text:
        await message.reply("🪫 Поставили безлимитную лицензию, как Стелла учила? Респект!")
    elif "1с" in text and "не работает" in text:
        await message.reply("🐢 1С не висит, она просто экономит энергию. Спокойно ждём.")

    # Если бот только что вышел из хард мьюта
    if bot_state.is_after_hard_mute:
        bot_state.is_after_hard_mute = False
        await message.answer("😎 Я снова в строю. Все пропущенные отчеты обработаны, продолжаем делать план!")

# ==========================================
# ОСНОВНОЙ ХЭНДЛЕР СООБЩЕНИЙ (Aiogram)
# ==========================================
@dp.message(F.chat.id == CHAT_ID)
async def chat_message_handler(message: Message):
    if not message.text:
        return

    if is_report_message(message.text):
        store_name = extract_store_name(message)
        process_report_data(message.text, store_name, message.date)
        await handle_gamification(message, store_name)
    
    # Обновляем время последней активности в чате
    update_last_sync_time()

# ==========================================
# СИНХРОНИЗАЦИЯ ПРИ СТАРТЕ (Pyrogram)
# ==========================================
async def sync_missed_reports(chat_id: int, last_sync_date: datetime):
    logging.info(f"Начинаю синхронизацию сообщений с {last_sync_date}")
    
    # Принудительно глушим бота во избежание спама старыми реакциями
    bot_state.can_speak = False 
    
    async with app:
        count = 0
        # Парсим историю с момента последней записи
        async for msg in app.get_chat_history(chat_id):
            if msg.date <= last_sync_date:
                break # Дошли до уже сохраненных сообщений
                
            if msg.text and is_report_message(msg.text):
                store_name = extract_store_name(msg)
                process_report_data(msg.text, store_name, msg.date)
                count += 1
                
        logging.info(f"Синхронизация завершена. Восстановлено отчетов: {count}")
            
    # Возвращаем голос и фиксируем успешный старт
    bot_state.can_speak = True
    if count > 0:
        bot_state.is_after_hard_mute = True
    update_last_sync_time()

# ==========================================
# ЗАПУСК БОТА
# ==========================================
async def on_startup():
    init_db()
    last_sync = get_last_sync_time()
    # Запускаем дозагрузку пропущенных сообщений
    await sync_missed_reports(CHAT_ID, last_sync)
    logging.info("Бот готов к работе (Aiogram Polling запущен).")

async def main():
    dp.startup.register(on_startup)
    # Запуск поллинга aiogram
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())