import asyncio
import logging
import os
import random
import re
import json

import datetime
from aiogram import Bot, Dispatcher
from aiogram.types import Message, ReactionTypeEmoji
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

# Отчёты, собранные за сегодня (сбрасываются в полночь) — только в памяти.
today_reports = []  # [{"store": str, "percent": float, "time": datetime}]

# "Пульс чата": считаем обычные сообщения (не отчёты), чтобы иногда вставлять реплику.
chat_pulse_buffer = []  # последние фразы, не являющиеся отчётами
last_pulse_comment_at = None  # когда бот последний раз комментировал "просто так"
PULSE_COOLDOWN_MINUTES = 60
PULSE_TRIGGER_COUNT = 25  # после скольки "обычных" сообщений подряд можно вставить реплику

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

# Реакции-эмодзи на отчёт вместо текстового ответа (чтобы не спамить в 13:00)
HIGH_REACTIONS = ["🔥", "👍", "🎉"]
MID_REACTIONS = ["💪", "👌"]
LOW_REACTIONS = ["🤔", "😬"]

# Напоминание за 10 минут до дедлайна отчётов
PRE_DEADLINE_REMINDERS = [
    "⏳ 10 минут до обеденных итогов! У кого отчёт ещё не готов — время собраться.",
    "📢 Через 10 минут сдаём цифры, доставайте калькуляторы!",
]

# Короткие фразы-реакции "по настроению чата" — используются редко
PULSE_COMMENTS = [
    "👀 Чат сегодня живой, наблюдаю.",
    "☕️ Пока вы спорите, я тут цифры считаю.",
    "🙂 Движение в чате — хороший знак, значит работаем.",
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

    # Отчёт по ТО — ставим эмодзи-реакцию и запоминаем, но НЕ пишем текстом в чат,
    # чтобы не спамить, когда все 16 точек присылают отчёты почти одновременно.
    report = parse_report(text)
    if report:
        percent = (report["to_fact"] / report["to_plan"] * 100) if report["to_plan"] else 0
        store = report["store_name"]

        today_reports.append({
            "store": store,
            "percent": percent,
            "time": datetime.datetime.now(),
        })

        if percent >= 100:
            emoji = random.choice(HIGH_REACTIONS)
        elif percent >= 70:
            emoji = random.choice(MID_REACTIONS)
        else:
            emoji = random.choice(LOW_REACTIONS)

        try:
            await bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
        except Exception as e:
            logging.warning(f"Не удалось поставить реакцию: {e}")
        return

    # Технические сбои — тут текстом, это нечастое событие и людям нужна поддержка
    if any(trigger in text_low for trigger in OUTAGE_TRIGGERS):
        await message.reply(random.choice(OUTAGE_REPLIES))
        return

    # "Пульс чата": копим обычные сообщения, изредка (не чаще раза в час)
    # вставляем короткую реплику, если накопилось достаточно активности.
    global last_pulse_comment_at
    chat_pulse_buffer.append(text)
    if len(chat_pulse_buffer) >= PULSE_TRIGGER_COUNT:
        now = datetime.datetime.now()
        cooldown_ok = (
            last_pulse_comment_at is None
            or (now - last_pulse_comment_at).total_seconds() >= PULSE_COOLDOWN_MINUTES * 60
        )
        if cooldown_ok:
            await message.answer(random.choice(PULSE_COMMENTS))
            last_pulse_comment_at = now
        chat_pulse_buffer.clear()


# ======== Плановые задачи ========
async def morning_warmup():
    chat_id = get_chat_id()
    if chat_id:
        await bot.send_message(chat_id, random.choice(MORNING_JOKES))


async def pre_deadline_reminder():
    chat_id = get_chat_id()
    if chat_id:
        await bot.send_message(chat_id, random.choice(PRE_DEADLINE_REMINDERS))


async def post_deadline_summary():
    """Через ~20 минут после дедлайна — один общий комментарий по итогам,
    а не реакция на каждый отчёт по отдельности."""
    chat_id = get_chat_id()
    if not chat_id or not today_reports:
        return

    # Берём только отчёты за последние ~40 минут (то есть именно этот заход)
    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=40)
    recent = [r for r in today_reports if r["time"] >= cutoff]
    if not recent:
        return

    best = max(recent, key=lambda r: r["percent"])
    worst = min(recent, key=lambda r: r["percent"])

    text = f"📊 Промежуточные итоги: {len(recent)} точек отчитались.\n"
    text += f"👑 Впереди всех — {best['store']} ({best['percent']:.0f}%).\n"
    if worst["percent"] < 70 and worst["store"] != best["store"]:
        text += f"🙃 {worst['store']} пока догоняет ({worst['percent']:.0f}%), но день длинный."

    await bot.send_message(chat_id, text)


async def reset_daily_reports():
    today_reports.clear()


def setup_scheduler():
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    scheduler.add_job(morning_warmup, trigger="cron", hour=9, minute=30)
    # За 10 минут до обеденного дедлайна (считаем дедлайн 13:00)
    scheduler.add_job(pre_deadline_reminder, trigger="cron", hour=12, minute=50)
    # Через 20 минут после дедлайна — один общий комментарий по итогам
    scheduler.add_job(post_deadline_summary, trigger="cron", hour=13, minute=20)
    # Сброс собранных отчётов в полночь
    scheduler.add_job(reset_daily_reports, trigger="cron", hour=0, minute=0)
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
