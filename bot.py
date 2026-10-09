import asyncio
import logging
import os
import random
import re
import json

from aiogram import Bot, Dispatcher
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

logging.basicConfig(level=logging.INFO)

# ======== НАСТРОЙКИ ========
# Токен берём из переменной окружения BOT_TOKEN (см. инструкцию ниже — как её задать).
# Никогда не вставляйте токен прямо в код, если собираетесь кому-то показывать файл.
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Задайте переменную окружения BOT_TOKEN перед запуском.")

CHAT_ID_FILE = "chat_id.txt"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ======== Хранение ID рабочего чата ========
def get_chat_id():
    if os.path.exists(CHAT_ID_FILE):
        with open(CHAT_ID_FILE, "r") as f:
            content = f.read().strip()
            return int(content) if content else None
    return None


def save_chat_id(chat_id: int):
    with open(CHAT_ID_FILE, "w") as f:
        f.write(str(chat_id))


# ======== Распознавание отчётов ========
# Форматы, встречающиеся в вашем чате:
#   "ТО    5410 / 6386"
#   "ТО План 10946/ Факт 4333грн."
STORE_KEYWORDS = {
    "глушко": "Глушко",
    "рабина": "Рабина",
    "черноморск": "Черноморск",
    "преображенск": "Преображенская",
}

TO_LINE_RE = re.compile(
    r"то\s*(?:план)?\s*[:\-]?\s*(\d+)\s*/\s*(?:факт)?\s*(\d+)",
    re.IGNORECASE,
)


def parse_report(text: str) -> dict | None:
    text_low = text.lower()
    match = TO_LINE_RE.search(text_low)
    if not match:
        return None

    to_plan, to_fact = float(match.group(1)), float(match.group(2))

    store_name = "ТТ"
    for kw, name in STORE_KEYWORDS.items():
        if kw in text_low:
            store_name = name
            break

    return {"to_plan": to_plan, "to_fact": to_fact, "store_name": store_name}


# ======== Банк реплик ========
HIGH_RESULT_REPLIES = [
    "🔥 {store} в огне! План сделан на {percent:.0f}%!",
    "👑 {store} снова делает кассу — {percent:.0f}% плана!",
    "🚀 {store} летит выше графика: {percent:.0f}%!",
]
MID_RESULT_REPLIES = [
    "💪 {store}: {percent:.0f}%. Нормально идём, но можно поднажать!",
    "👀 {store} на {percent:.0f}%. До плана рукой подать.",
]
LOW_RESULT_REPLIES = [
    "🙃 {store}: {percent:.0f}%. Завтра точно зайдёт оптовик за чехлами.",
    "☕️ {store}: {percent:.0f}%. Ничего, вечер длинный.",
]

OUTAGE_TRIGGERS = ["висит 1с", "1с легла", "нет интернета", "не работает терминал"]
OUTAGE_REPLIES = [
    "🤖 1С снова ушла в нирвану. Держитесь там!",
    "🤬 Отправил сисадмину лучи гнева. Пьём кофе и ждём.",
    "🧘‍♂️ Спокойствие, только спокойствие. Клиентам улыбаемся шире.",
]

MORNING_JOKES = [
    "🔮 Гороскоп продавца на сегодня: ожидается наплыв клиентов со сломанными экранами. Протираем витрины!",
    "⚡️ Клятва дня: не отпускать покупателя без защитного стекла. Погнали!",
    "☕️ Доброе утро! Сегодня план сам себя не сделает, но кофе точно поможет.",
]


# ======== Обработчик сообщений ========
@dp.message()
async def global_handler(message: Message):
    if message.chat.type in ("group", "supergroup"):
        if get_chat_id() != message.chat.id:
            save_chat_id(message.chat.id)
            logging.info(f"ID группы сохранён: {message.chat.id}")

    text = message.text
    if not text:
        return
    text_low = text.lower()

    # /top — заглушка-рейтинг (расширяется по мере накопления данных)
    if text_low.startswith("/top"):
        await message.answer(
            "🏆 <b>Рейтинг пока копится</b> — присылайте отчёты, и здесь появится топ дня!",
            parse_mode="HTML",
        )
        return

    # Отчёт по ТО
    report = parse_report(text)
    if report:
        percent = (report["to_fact"] / report["to_plan"] * 100) if report["to_plan"] else 0
        store = report["store_name"]
        if percent >= 100:
            reply = random.choice(HIGH_RESULT_REPLIES).format(store=store, percent=percent)
        elif percent >= 70:
            reply = random.choice(MID_RESULT_REPLIES).format(store=store, percent=percent)
        else:
            reply = random.choice(LOW_RESULT_REPLIES).format(store=store, percent=percent)
        await message.reply(reply)
        return

    # Технические сбои
    if any(trigger in text_low for trigger in OUTAGE_TRIGGERS):
        await message.reply(random.choice(OUTAGE_REPLIES))
        return


# ======== Плановые задачи ========
async def morning_warmup():
    chat_id = get_chat_id()
    if chat_id:
        await bot.send_message(chat_id, random.choice(MORNING_JOKES))


async def midday_ping():
    chat_id = get_chat_id()
    if chat_id:
        await bot.send_message(chat_id, "⏰ Время промежуточных итогов! Кто сегодня идёт в план?")


def setup_scheduler():
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    scheduler.add_job(morning_warmup, trigger="cron", hour=9, minute=30)
    scheduler.add_job(midday_ping, trigger="cron", hour=13, minute=0)
    scheduler.start()


async def handle_ping(request):
    return web.Response(text="OK, bot is alive")


async def start_keepalive_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    setup_scheduler()
    await bot.delete_webhook(drop_pending_updates=True)
    await start_keepalive_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
