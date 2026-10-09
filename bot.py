# -*- coding: utf-8 -*-
"""
TOP TEAM — рабочий бот для чата 16 магазинов (Сота Альянс, Одесский регион).

Что умеет (всё по правилам, без платных нейросетей):
  * читает отчёты магазинов (утро / обед / вечер), понимает все форматы из чата
  * магазин определяется по имени отправителя (в тексте отчёта его нет)
  * ставит эмодзи-реакции на отчёты (только разрешённые Telegram эмодзи)
  * хвалит текстом, когда магазин закрыл план к обеду, и за красивые чеки (фото)
  * раз в час (с 10:00 до 17:30) шлёт рабочую шутку / совет, но не спамит
  * напоминает, кто ещё не сдал утренний/обеденный отчёт
  * утром пишет итоги вчерашнего дня, по понедельникам — итоги недели
  * НИЧЕГО не пишет текстом после 18:00 (реакции на вечерние отчёты — включены)
  * молчит, когда в чате авария (1С, свет) или серьёзные темы (сборы, прощания)

Запуск: python bot.py   (нужны переменные BOT_TOKEN и желательно CHAT_ID)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import date, datetime, timedelta
from html import escape
from statistics import mean
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Message, MessageReactionUpdated, ReactionTypeEmoji
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("topteam")

# ============================ НАСТРОЙКИ ============================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Задайте переменную окружения BOT_TOKEN.")

ENV_CHAT_ID = os.environ.get("CHAT_ID", "").strip()      # например -1002034080349
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

try:
    TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # старые базы часовых поясов
    TZ = ZoneInfo("Europe/Kiev")

SPEAK_FROM = (7, 30)     # раньше этого времени бот не пишет текстом
SPEAK_UNTIL = (18, 0)    # С 18:00 бот НЕ пишет текстом (жёсткое правило)
REACT_AFTER_HOURS = True  # реакции-эмодзи на вечерние отчёты (после 18:00) — тихие, без текста
MIN_GAP_MIN = 20          # минимум минут между самостоятельными сообщениями бота
SELF_SPAM_LIMIT = 4       # сколько сообщений бота подряд без ответа людей, потом пауза
HUMAN_BURST_SEC = 75      # если люди только что писали — не перебиваем
SLOT_GRACE_MIN = 25       # сколько минут после времени слота бот ещё может его выполнить
PHOTO_REACTORS_NEEDED = 2  # сколько людей должны поставить 👍/🔥 на фото чека
MAX_PRAISE_PER_DAY = 5

HQ_MARKERS = ("ᴋɴɴᴠsᴋʏ", "knnvsky")           # официальный аккаунт головного офиса — не трогаем
BOSS_MARKERS = ("гусев", "стелла яссин")      # руководители, которые постят красивые чеки
ALIASES = {                                   # личные аккаунты, пишущие отчёты за магазин
    "денис черноморск": "Черноморск",
    "руслан черноморск": "Черноморск",
    "алина старосенная": "Старосенная кс",
}

# Эмодзи, которые Telegram разрешает ставить ботом как реакцию
ALLOWED = {
    "👍", "🔥", "🎉", "🤩", "😎", "🏆", "💯", "⚡", "🤝", "👌", "🫡", "🤗", "🙏", "👀",
    "🤡", "🙈", "🥱", "🤨", "🍾", "❤", "🤣", "😁", "👏", "💅", "🦄", "🗿",
}
POSITIVE = {"👍", "🔥", "❤", "🎉", "🤩", "😎", "👏", "💯", "⚡", "🏆"}


# ============================ ВРЕМЯ ============================
def now() -> datetime:
    return datetime.now(TZ)


def biz_date(dt: datetime) -> date:
    """Ночные (00:00–03:00) отчёты относим к предыдущему рабочему дню."""
    return (dt - timedelta(hours=3)).date()


def in_speak_hours(n: datetime) -> bool:
    cur = (n.hour, n.minute)
    return SPEAK_FROM <= cur < SPEAK_UNTIL


def classify_window(dt: datetime) -> str:
    minutes = dt.hour * 60 + dt.minute
    if dt.hour < 3:
        return "evening"
    if minutes < 11 * 60:
        return "morning"
    if minutes < 16 * 60 + 30:
        return "midday"
    return "evening"


# ============================ СОСТОЯНИЕ ============================
STATE: dict = {"chat_id": None, "history": {}, "bags": {}, "done": {}}


class RT:
    """Оперативная память (не сохраняется)."""
    last_human = 0.0
    last_bot = 0.0
    streak = 0
    mute_until = 0.0
    tech_at = 0.0
    last_tech_reply = 0.0
    last_praise = 0.0
    praise_day = ""
    praise_n = 0
    midday_praised: set = set()
    greeted: set = set()
    photo_watch: dict = {}
    last_bday = 0.0


def load_state() -> None:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            STATE.update({k: data.get(k, STATE[k]) for k in STATE})
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Не удалось прочитать состояние: %s", e)


def save_state() -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning("Не удалось сохранить состояние: %s", e)


def get_chat_id():
    if ENV_CHAT_ID:
        try:
            return int(ENV_CHAT_ID)
        except ValueError:
            pass
    return STATE.get("chat_id")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================ РАЗБОР ОТЧЁТОВ ============================
_SPACE_DIGITS = re.compile(r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?!\d))")
TO_LINE = re.compile(
    r"^\W*(?:план\s+)?т?о(?![A-Za-zА-Яа-яЁё])(?!\s*(?:в\s+)?кредит)\s*(?:план\w*)?\s*[:\-–]?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
MARKER_RE = re.compile(
    r"^\W*(?:[cс]март|тел|сер|[cс]у\b|ак[cс]|стр|трейд|trade|заявк|крдт|кредит|"
    r"(?:завтра\s+)?на\s+(?:смене|зміні|змене))",
    re.IGNORECASE,
)
SHIFT_RE = re.compile(r"на\s+(?:смене|зміні|змене)\s*[:\-]?\s*(?P<names>.+)", re.IGNORECASE | re.DOTALL)
NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁёІіЇїЄєҐґ'’\-]+(?:\s+[A-Za-zА-Яа-яЁёІіЇїЄєҐґ'’\-]+)?$")


def _clean(text: str) -> str:
    t = text.replace("\u00a0", " ").replace("\u200b", "").replace("\u202f", " ")
    return _SPACE_DIGITS.sub("", t)


def parse_shift(text: str) -> tuple[list, bool]:
    m = SHIFT_RE.search(text)
    if not m:
        return [], False
    tomorrow = bool(re.search(r"завтра\s+на", text, re.IGNORECASE))
    names = []
    for part in re.split(r"[,\n;/]", m.group("names")):
        p = part.strip(" .:-–\t\u200b")
        if 2 <= len(p) <= 25 and NAME_RE.match(p):
            names.append(p)
    return names[:5], tomorrow


def calc_pct(plan, fact, closed):
    if closed:
        return 100.0
    if plan and fact is not None and plan > 0:
        return fact / plan * 100.0
    return None


def parse_report(text: str):
    """Возвращает dict(plan, fact, closed, pct, shift, tomorrow) или None."""
    if not text or len(text) > 1800:
        return None
    t = _clean(text)
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    to = None
    for l in lines:
        m = TO_LINE.match(l)
        if not m:
            continue
        rest = m.group("rest")
        closed = "+" in rest or bool(re.search(r"закр[иы]т", rest, re.IGNORECASE))
        nums = [int(x) for x in re.findall(r"\d+", rest)]
        if closed:
            to = (None, nums[-1] if nums else None, True)
        else:
            if not nums:
                continue
            to = (nums[0], nums[1] if len(nums) > 1 else None, False)
        break
    if not to:
        return None
    if sum(1 for l in lines if MARKER_RE.match(l)) < 2:
        return None
    plan, fact, closed = to
    shift, tomorrow = parse_shift(t)
    return {
        "plan": plan, "fact": fact, "closed": closed,
        "pct": calc_pct(plan, fact, closed), "shift": shift, "tomorrow": tomorrow,
    }


def is_hq(name: str) -> bool:
    low = name.lower()
    return any(k in low for k in HQ_MARKERS)


def is_boss(name: str) -> bool:
    low = name.lower()
    return any(k in low for k in BOSS_MARKERS)


def store_name(name: str) -> str:
    return ALIASES.get(name.strip().lower(), name.strip())


# ============================ ИСТОРИЯ / СТАТИСТИКА ============================
def record_report(store: str, window: str, dt: datetime, rep: dict) -> None:
    bd = biz_date(dt).isoformat()
    day = STATE["history"].setdefault(bd, {})
    day.setdefault(store, {})[window] = {
        "plan": rep["plan"], "fact": rep["fact"], "closed": rep["closed"],
        "pct": rep["pct"], "shift": rep["shift"] if not rep["tomorrow"] else [],
        "t": dt.strftime("%H:%M"),
    }
    for old in sorted(STATE["history"])[:-40]:
        STATE["history"].pop(old, None)


def day_entries(bd: str, window: str) -> dict:
    return {s: w[window] for s, w in STATE["history"].get(bd, {}).items() if window in w}


def expected_stores(window: str, today: str) -> set:
    days = [d for d in sorted(STATE["history"]) if d < today][-7:]
    if len(days) < 3:
        return set()
    counts: dict = {}
    for d in days:
        for s, w in STATE["history"][d].items():
            if window in w:
                counts[s] = counts.get(s, 0) + 1
    need = max(3, len(days) // 2)
    return {s for s, c in counts.items() if c >= need}


def pct_str(p) -> str:
    return f"{p:.0f}%"


def evening_stats(dates: list) -> dict:
    res: dict = {}
    for d in dates:
        for s, e in day_entries(d, "evening").items():
            if e.get("pct") is not None:
                res.setdefault(s, []).append((d, e["pct"]))
    return res


RU_DAY_ACC = ["в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье"]


def build_yesterday(today: date):
    y = (today - timedelta(days=1)).isoformat()
    ev = day_entries(y, "evening")
    ranked = sorted(((s, e["pct"]) for s, e in ev.items() if e.get("pct") is not None), key=lambda x: -x[1])
    if len(ranked) < 8:
        return None
    closed = sum(1 for _, p in ranked if p >= 100)
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📊 <b>Итоги вчера:</b> вечерний отчёт сдали {len(ev)} точек, план закрыли {closed}."]
    for i, (s, p) in enumerate(ranked[:3]):
        lines.append(f"{medals[i]} {escape(s)} — {pct_str(p)}")
    lines.append("Сегодня берём новые высоты 💪")
    return "\n".join(lines)


def build_week(dates: list, title: str):
    stats = evening_stats(dates)
    avg = {s: mean(min(p, 300) for _, p in v) for s, v in stats.items() if len(v) >= 3}
    if len(avg) < 5:
        return None
    ranked = sorted(avg.items(), key=lambda x: -x[1])
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 <b>{title}</b>"]
    for i, (s, a) in enumerate(ranked[:3]):
        lines.append(f"{medals[i]} {escape(s)} — {pct_str(a)} в среднем")
    best = max(((s, d, p) for s, v in stats.items() for d, p in v if p < 1000), key=lambda x: x[2], default=None)
    if best:
        wd = RU_DAY_ACC[date.fromisoformat(best[1]).weekday()]
        lines.append(f"🚀 Рекорд дня: <b>{escape(best[0])}</b> — {pct_str(best[2])} ({wd})")
    full = [s for s, v in stats.items() if len(v) >= len(dates) and len(dates) >= 5]
    if full and len(full) <= 8:
        lines.append("🎖 Железные отчётники (все вечерние отчёты в срок): " + ", ".join(escape(s) for s in sorted(full)))
    elif full:
        lines.append(f"🎖 Железная дисциплина: {len(full)} точек сдали все вечерние отчёты недели!")
    lines.append("\n<i>Новая неделя — новая гонка!</i>")
    return "\n".join(lines)


def week_dates(end: date, days: int = 7) -> list:
    return [(end - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


# ============================ БАНК ФРАЗ ============================
JOKES = {
    "general": [
        "📱 Клиент: «Мне просто посмотреть». Через 15 минут: «А скидку и чехол в подарок можно?» Классика.",
        "🔋 У клиента 3% заряда, у продавца 100% терпения. Баланс во вселенной соблюдён.",
        "🛡 Защитное стекло — это не допродажа, это забота. Клиент скажет спасибо, когда телефон встретится с плиткой.",
        "📶 «А у вас связь ловит?» — главный вопрос в магазине связи. Держимся, коллеги.",
        "🧾 Закон подлости: «а есть такой же, но дешевле» приходит ровно в тот момент, когда вы собрались на обед.",
        "🎧 Клиент взял в руки три телефона и сказал «я подумаю». Это не отказ, это стадия «сравниваю».",
        "🧠 Продавец: человек, который знает разницу между 128 и 256 ГБ и объясняет её в пятнадцатый раз за день.",
        "🕵️ Самый честный отзыв о телефоне: «а он точно не взорвётся?» Улыбаемся и показываем гарантию.",
        "🔌 Клиент просит «такую же зарядку, только чтобы быстрее». Продавец мысленно уже на кассе.",
        "☕ Кофе — единственный аксессуар, который продаётся сам. Берегите запасы на точке.",
        "📲 Телефон в руках клиента: «а он водонепроницаемый?» Продавец: «а вы его в воду собираетесь?»",
        "🏪 Магазин без клиентов — это не тишина, это затишье перед очередью из пятерых за одним чехлом.",
        "🧮 Калькулятор в телефоне у клиента есть. Но «сколько будет в рассрочку» всё равно спросят у вас.",
        "🎯 Хороший продавец не навязывает, а «подсказывает». Результат тот же, нервов меньше.",
        "📦 Ничто так не мотивирует, как последний телефон нужного цвета на витрине и двое желающих.",
        "🙌 Улыбка на кассе — самый дешёвый аксессуар. И почему-то самый эффективный.",
        "🤖 Ой, а я бот, у меня нет плана продаж. Но я за вас болею, честно.",
        "🛒 «А можно в кредит?» — «Можно!» Эти два слова делают ТО красивее. Фокус не забываем.",
        "🧑‍💼 Если клиент знает все характеристики лучше вас — поздравляю, он сегодня читал обзоры. Берём на вооружение.",
        "🧊 Холодный клиент греется за три минуты разговора. Проверено на практике.",
        "🏃 План — как автобус: вроде далеко, а потом внезапно всё сразу. Ловим момент.",
        "😎 Лучший вид рекламы — довольный клиент, который вернулся за стеклом для второго телефона.",
        "Доброго дня! Чехлы не купить — це як чай без цукру: можна, але навіщо 😄",
        "Клієнт каже «просто подивлюсь» — і виходить з трьома аксесуарами. Магія роздрібу ✨",
    ],
    "monday": [
        "📅 Понедельник: день, когда даже кофе просит выходной. Но план сам себя не сделает!",
        "🚀 Понедельник — это просто пятница, которая начинает с чистого листа. Новая неделя — новая гонка!",
        "☕ Понедельник. Заряжаем себя, потом телефоны клиентов. В таком порядке.",
    ],
    "friday": [
        "🎉 Пятница! Клиенты думают о выходных, мы — о том, чтобы закрыть неделю красиво.",
        "🏁 Финишная прямая недели. Кто закроет пятницу с плюсом — тот и герой выходных.",
        "🍾 До выходных рукой подать. Добиваем план, а потом отдыхаем со спокойной душой!",
    ],
    "weekend": [
        "🛍 Выходные в рознице — это когда у остальных отдых, а у нас турбо-режим. Вы молодцы!",
        "😌 Суббота и воскресенье: покупатели с детьми, мнениями и вопросом «а где туалет». Держитесь!",
        "⭐ Кто работает в выходные — тот делает план для всего филиала. Респект!",
    ],
    "month": [
        "🗓 Конец месяца — время, когда план превращается из цифры в цель. Давайте дожмём!",
        "📈 Последняя неделя месяца: каждый чек — это шаг к плану. Не расслабляемся!",
        "🧮 Месяц на исходе. Если сегодня закрыть на 100% — завтра будет легче дышать.",
    ],
    "lunch": [
        "🍕 Обед — это святое. Но если в магазин зашёл клиент, обед подождёт пять минут.",
        "🥪 Лучший момент для клиента — когда вы только откусили бутерброд. Закон Мерфи для рознички.",
    ],
    "tips": [
        "💡 Совет дня: предложите стекло и чехол вместе — комплект продаётся легче, чем по отдельности.",
        "💡 Фокус дня: спросите клиента «вам удобнее сразу или частями?» — вопрос, который открывает кредит.",
        "💡 Совет: упомяните гарантию и страховку до того, как клиент достал карту. Потом уже поздно.",
        "💡 Совет: покажите клиенту, как легко перенести данные на новый телефон — продажа ближе на 10 шагов.",
        "💡 Совет: трейд-ин часто решает вопрос «дорого». Предложите оценить старый телефон до разговора о цене.",
        "💡 Если клиент сомневается между двумя моделями — покажите разницу в одном параметре, который важен именно ему.",
    ],
}

OPEN_MESSAGES = [
    "☀️ Доброе утро, команда! Протираем витрины, включаем улыбки, открываем день на максимум!",
    "🔮 Гороскоп продавца на сегодня: ожидается наплыв клиентов, которые «просто спросить». Будьте готовы!",
    "⚡️ Клятва дня: не отпускать клиента без защитного стекла. Погнали!",
    "☕ Доброе утро! План сам себя не сделает, но кофе точно поможет.",
    "🚀 Новый день — новые чеки. Утренние отчёты ждём, вечерние — рекордные!",
]

PUSH_MESSAGES = [
    "🏁 Финишная прямая перед вечерними цифрами! Добиваем смену и закрываем план.",
    "⏳ Последний рывок дня: каждый чек сейчас — в копилку вечернего отчёта. Вы можете!",
    "💪 Ещё чуть-чуть до вечерних итогов. Улыбаемся клиентам и дожимаем!",
]

TECH_REPLIES = [
    "🤝 Принято. Если сбой у вас не один — отпишитесь, у кого так же: быстрее поймём, что проблема общая.",
    "🫡 Понял. Пишите, у кого ещё не работает — так проще передать вопрос дальше.",
]

PRAISE_DEFAULT = [
    "🔥 Чек огонь! Вот так закрываются смены.",
    "👏 Красивая продажа! Именно так и делается план.",
    "😎 Такой чек не стыдно на стену славы. Так держать!",
]
PRAISE_CREDIT = ["💳 Продажа в кредит — в самую точку! Фокус дня засчитан 🔥"]
PRAISE_WARRANTY = ["🛡 Ещё и с гарантией — это уже высший пилотаж! 💪"]
PRAISE_FLAGSHIP = ["📱 Флагман в чеке — аплодисменты! 👏"]

# ============================ ТРИГГЕРЫ ============================
TECH_RE = re.compile(
    r"(?:1\s?[сc].{0,25}(?:не\s?работ|завис|вис|лег|лёг|упал|тормоз|глюч|не\s?груз))|сбой|збій|"
    r"не\s?работает|не\s?працює|нет\s+связи|нема\s+зв.язку|техн\w+\s+проблем",
    re.IGNORECASE,
)
TECH_FIXED_RE = re.compile(r"заработал|запрацював|уже\s+работает|вже\s+працює|всё\s+ок|все\s+ок", re.IGNORECASE)
POWER_RE = re.compile(r"свет\s*[+\-–]|генератор\s*[+\-–]|світл\w*\s*[+\-–]", re.IGNORECASE)
SERIOUS_RE = re.compile(
    r"бригад|броник|амуниц|каска|збір\b|сбор\s+(?:средств|денег)|донат|всу\b|зсу\b|загинул|загибл|погиб|"
    r"прощан|похорон|памят[иь]|співчу|соболезн|умер|помер",
    re.IGNORECASE,
)
FAREWELL_RE = re.compile(r"последн\w+\s+рабоч|остан\w+\s+робоч", re.IGNORECASE)
GREET_RE = re.compile(r"доброе утро|доброго ранку|доброго дня|добрый день|добрий день|добрий ранок", re.IGNORECASE)
BDAY_RE = re.compile(r"с\s+дн[её]м\s+рожд|з\s+дн[её]м\s+народж", re.IGNORECASE)


# ============================ ОТПРАВКА ============================
def can_speak(n: datetime, gap: bool = True, jokes: bool = False) -> bool:
    if not in_speak_hours(n):
        return False
    t = time.time()
    if t < RT.mute_until:
        return False
    if gap and t - RT.last_bot < MIN_GAP_MIN * 60:
        return False
    if jokes and RT.streak >= SELF_SPAM_LIMIT:
        return False
    return True


async def say(text: str, msg: Message | None = None, html: bool = False) -> bool:
    kwargs = {"parse_mode": "HTML"} if html else {}
    try:
        if msg is not None:
            await msg.reply(text, **kwargs)
        else:
            cid = get_chat_id()
            if cid is None:
                return False
            await bot.send_message(cid, text, **kwargs)
    except TelegramRetryAfter as e:
        await asyncio.sleep(min(e.retry_after + 1, 30))
        return False
    except Exception as e:
        log.warning("Не удалось отправить сообщение: %s", e)
        return False
    RT.last_bot = time.time()
    RT.streak += 1
    return True


async def react(msg: Message, pool: list) -> bool:
    emojis = [e for e in pool if e in ALLOWED]
    random.shuffle(emojis)
    for e in emojis[:2]:
        for attempt in range(2):
            try:
                await bot.set_message_reaction(
                    chat_id=msg.chat.id, message_id=msg.message_id,
                    reaction=[ReactionTypeEmoji(emoji=e)],
                )
                return True
            except TelegramRetryAfter as ex:
                await asyncio.sleep(min(ex.retry_after + 1, 20))
            except Exception as ex:
                log.warning("Реакция %s не поставилась: %s", e, ex)
                break
    return False


def pick_bag(cat: str, items: list) -> str:
    bag = STATE["bags"].setdefault(cat, [])
    bag[:] = [i for i in bag if isinstance(i, int) and i < len(items)]
    if not bag:
        bag.extend(random.sample(range(len(items)), len(items)))
    return items[bag.pop()]


def joke_for(n: datetime) -> str:
    cats = ["general"] * 4 + ["tips"]
    wd = n.weekday()
    if wd == 0:
        cats += ["monday"] * 2
    if wd == 4:
        cats += ["friday"] * 3
    if wd >= 5:
        cats += ["weekend"] * 2
    if n.day >= 26 or n.day <= 2:
        cats += ["month"] * 2
    if n.hour in (12, 13, 14):
        cats += ["lunch"]
    cat = random.choice(cats)
    return pick_bag(cat, JOKES[cat])


# ============================ РЕАКЦИИ НА ОТЧЁТЫ ============================
def report_reactions(window: str, pct, closed: bool) -> list:
    if window == "morning":
        return ["👍", "😎", "🤝", "🫡"]
    if closed or (pct is not None and pct >= 100):
        return ["🔥", "🎉", "🤩", "🏆", "💯"] if (pct or 100) < 200 else ["🔥", "🏆", "🤩", "💯"]
    if pct is None:
        return ["👍"]
    if window == "midday":
        if pct >= 50:
            return ["🔥", "😎", "👏"]
        if pct >= 25:
            return ["👍", "🤝", "👌"]
        return ["👀", "🫡", "🤝"]
    if pct >= 70:
        return ["👍", "🤝", "👌", "⚡"]
    if pct >= 40:
        return ["👀", "🤨", "🫡"]
    return ["🤡", "🙈", "🥱"]


def maybe_reset_day_counters(n: datetime) -> None:
    d = n.date().isoformat()
    if RT.praise_day != d:
        RT.praise_day = d
        RT.praise_n = 0
        RT.midday_praised = set()
        RT.greeted = set()


async def handle_report(message: Message, store: str, window: str, rep: dict, dt: datetime) -> None:
    await asyncio.sleep(random.uniform(0.6, 2.5))
    n = now()
    if n.hour >= SPEAK_UNTIL[0] and not REACT_AFTER_HOURS:
        return
    await react(message, report_reactions(window, rep["pct"], rep["closed"]))

    # Текстовая похвала: план закрыт уже к обеду
    maybe_reset_day_counters(n)
    if window == "midday" and (rep["closed"] or (rep["pct"] or 0) >= 100) and store not in RT.midday_praised:
        if RT.praise_n < MAX_PRAISE_PER_DAY and can_speak(n, gap=False):
            RT.midday_praised.add(store)
            bd = biz_date(dt).isoformat()
            shift = STATE["history"].get(bd, {}).get(store, {}).get("morning", {}).get("shift") or []
            pct_txt = "" if rep["closed"] and rep["pct"] == 100.0 and not rep["plan"] else f" — {pct_str(rep['pct'])}"
            text = f"🔥 {store} закрыл план уже к обеду{pct_txt}! Вот это темп 👏"
            if shift:
                text += f"\nСмена: {', '.join(shift)} — красавцы!"
            if await say(text, message):
                RT.praise_n += 1


# ============================ ФОТО-ЧЕКИ ============================
async def praise_check(message: Message, caption: str) -> None:
    n = now()
    maybe_reset_day_counters(n)
    await react(message, ["🔥", "👏", "😎", "🤩"])
    if RT.praise_n >= MAX_PRAISE_PER_DAY or time.time() - RT.last_praise < 600:
        return
    if not can_speak(n, gap=False):
        return
    low = caption.lower()
    if re.search(r"кредит|частями|скибоч|факторинг|привaт|моно", low):
        pool = PRAISE_CREDIT
    elif re.search(r"гарант|страх", low):
        pool = PRAISE_WARRANTY
    elif re.search(r"флагман|iphone|айфон|samsung|s2\d|pro max", low):
        pool = PRAISE_FLAGSHIP
    else:
        pool = PRAISE_DEFAULT
    if await say(random.choice(pool), message):
        RT.praise_n += 1
        RT.last_praise = time.time()


@dp.message_reaction()
async def on_reaction(ev: MessageReactionUpdated):
    try:
        if ev.chat.id != get_chat_id():
            return
        w = RT.photo_watch.get(ev.message_id)
        if not w or w["done"] or ev.user is None or ev.user.id == w["sender"]:
            return
        new = {r.emoji for r in ev.new_reaction if isinstance(r, ReactionTypeEmoji)}
        if new & POSITIVE:
            w["users"].add(ev.user.id)
        else:
            w["users"].discard(ev.user.id)
        if len(w["users"]) >= PHOTO_REACTORS_NEEDED:
            w["done"] = True
            await praise_check(w["msg"], w["caption"])
    except Exception as e:
        log.warning("Ошибка в on_reaction: %s", e)


# ============================ КОМАНДЫ ============================
HELP_TEXT = (
    "🤖 Я бот чата TOP TEAM.\n"
    "/top — рейтинг недели по вечерним отчётам\n"
    "/today — как идёт сегодняшний день\n"
    "/joke — шутка по запросу\n"
    "Отчёты и чеки я отмечаю реакциями, а по утрам пишу итоги."
)


async def handle_command(message: Message, cmd: str) -> None:
    n = now()
    if not in_speak_hours(n):
        await react(message, ["🫡"])  # после 18:00 текстом не пишем
        return
    today = biz_date(n)
    if cmd == "/help" or cmd == "/start":
        await say(HELP_TEXT, message)
    elif cmd == "/joke":
        if time.time() - RT.last_bot > 120:
            await say(joke_for(n), message)
    elif cmd == "/top":
        start = today - timedelta(days=today.weekday())
        dates = [(start + timedelta(days=i)).isoformat() for i in range((today - start).days + 1)]
        stats = evening_stats(dates)
        avg = {s: mean(min(p, 300) for _, p in v) for s, v in stats.items()}
        if len(avg) < 3:
            await say("🏆 Рейтинг пока пуст — нужны вечерние отчёты за несколько дней.", message)
            return
        ranked = sorted(avg.items(), key=lambda x: -x[1])[:5]
        medals = ["🥇", "🥈", "🥉", "▪️", "▪️"]
        lines = ["🏆 <b>Рейтинг недели (средний % плана):</b>"]
        lines += [f"{medals[i]} {escape(s)} — {pct_str(a)}" for i, (s, a) in enumerate(ranked)]
        await say("\n".join(lines), message, html=True)
    elif cmd == "/today":
        bd = today.isoformat()
        for window, title in (("evening", "вечер"), ("midday", "обед")):
            ent = day_entries(bd, window)
            ranked = sorted(((s, e["pct"]) for s, e in ent.items() if e.get("pct") is not None), key=lambda x: -x[1])
            if len(ranked) >= 3:
                top = ", ".join(f"{escape(s)} ({pct_str(p)})" for s, p in ranked[:3])
                await say(f"📈 Сегодня ({title}) впереди: {top}. Отчётов: {len(ent)}.", message, html=True)
                return
        await say("📈 Пока мало отчётов за сегодня — подожду остальных.", message)


# ============================ ГЛАВНЫЙ ОБРАБОТЧИК ============================
def mute_for(minutes: float) -> None:
    RT.mute_until = max(RT.mute_until, time.time() + minutes * 60)


async def process(message: Message, edited: bool) -> None:
    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        return
    cid = get_chat_id()
    if cid is None:
        STATE["chat_id"] = chat.id
        save_state()
        cid = chat.id
        log.info("Рабочий чат запомнен: %s", cid)
    if chat.id != cid:
        return
    user = message.from_user
    if user is None or user.is_bot:
        return

    name = (user.full_name or "").strip()
    text = (message.text or message.caption or "").strip()
    low = text.lower()
    n = now()
    maybe_reset_day_counters(n)

    # Официальный аккаунт головного офиса: бот только "слушает"
    if is_hq(name):
        if TECH_RE.search(low):
            RT.tech_at = time.time()
            mute_for(45)
        if SERIOUS_RE.search(low):
            mute_for(180)
        return

    if not edited:
        RT.last_human = time.time()
        RT.streak = 0

    # Серьёзные темы — полная тишина на 3 часа
    if SERIOUS_RE.search(low):
        mute_for(180)
        return

    # Команды
    if low.startswith("/") and not edited:
        cmd = low.split()[0].split("@")[0]
        if cmd in ("/top", "/today", "/joke", "/help", "/start"):
            await handle_command(message, cmd)
        return

    # Отчёты магазинов
    if text:
        rep = parse_report(text)
        if rep:
            store = store_name(name)
            dt = message.date.astimezone(TZ) if message.date else n
            window = classify_window(dt)
            record_report(store, window, dt, rep)
            save_state()
            if not edited:
                await handle_report(message, store, window, rep, dt)
            return

    if edited:
        return

    # Фото-чеки: от руководителей — сразу, от остальных — когда другие поставят 👍/🔥
    if message.photo and len(text) < 120:
        if is_boss(name):
            await praise_check(message, text)
        else:
            RT.photo_watch[message.message_id] = {
                "users": set(), "done": False, "sender": user.id, "msg": message, "caption": text,
            }
            for old in list(RT.photo_watch)[:-100]:
                RT.photo_watch.pop(old, None)
        return

    if not text:
        return

    # Свет / генератор — деликатная тема, шуток нет
    if POWER_RE.search(low):
        mute_for(60)
        return

    # Сбои 1С / связи
    if TECH_RE.search(low):
        RT.tech_at = time.time()
        mute_for(45)
        await react(message, ["🫡", "👀"])
        if time.time() - RT.last_tech_reply > 1800 and can_speak(n, gap=False):
            RT.last_tech_reply = time.time()
            await say(random.choice(TECH_REPLIES), message)
        return
    if TECH_FIXED_RE.search(low) and time.time() - RT.tech_at < 5400:
        RT.mute_until = 0.0
        await react(message, ["👍", "🙏"])
        return

    if FAREWELL_RE.search(low):
        await react(message, ["🫡", "🤗"])
        mute_for(30)
        return

    if BDAY_RE.search(low):
        await react(message, ["🎉", "🍾"])
        if time.time() - RT.last_bday > 3600 and can_speak(n, gap=False):
            RT.last_bday = time.time()
            await say("🎂 Присоединяюсь к поздравлениям! Пусть план радует так же, как этот день 🎉", message)
        return

    # Приветствия — только реакция, один раз в день на человека
    if len(text) <= 60 and GREET_RE.search(low):
        key = (n.date().isoformat(), user.id)
        if key not in RT.greeted:
            RT.greeted.add(key)
            await react(message, ["👍", "😎", "🤗", "🫡"])


@dp.message()
async def on_message(message: Message):
    try:
        await process(message, edited=False)
    except Exception:
        log.exception("Ошибка обработки сообщения")


@dp.edited_message()
async def on_edited(message: Message):
    try:
        await process(message, edited=True)
    except Exception:
        log.exception("Ошибка обработки правки")


# ============================ РАСПИСАНИЕ ============================
# (id, час, минута, тип, fallback_на_шутку)
SLOTS = [
    ("open", 7, 55, "open", False),
    ("nudge_m", 8, 55, "nudge_morning", False),
    ("sum", 9, 30, "summary", True),
    ("j10", 10, 20, "joke", False),
    ("j11", 11, 20, "joke", False),
    ("j12", 12, 20, "joke", False),
    ("nudge_d", 13, 30, "nudge_midday", True),
    ("stats", 14, 20, "midday_stats", True),
    ("j15", 15, 20, "joke", False),
    ("j16", 16, 20, "joke", False),
    ("j17", 17, 20, "joke", False),
    ("push", 17, 45, "push", False),
]


def missing_text(window: str, n: datetime, label: str):
    today = biz_date(n).isoformat()
    reported = set(day_entries(today, window))
    miss = sorted(expected_stores(window, today) - reported)
    if len(reported) >= 8 and 1 <= len(miss) <= 4:
        return f"{label} ещё ждём от: {', '.join(miss)}. Коллеги, не забываем 🙏"
    return None


async def run_slot(kind: str, fallback: bool, n: datetime) -> str:
    """Возвращает 'sent' | 'skip' (пропустить слот навсегда) | 'retry'."""
    jokes_kind = kind == "joke" or fallback
    if not can_speak(n, jokes=False):
        # вне времени/мьют/пауза — попробуем позже в пределах grace
        return "retry"
    if time.time() - RT.last_human < HUMAN_BURST_SEC:
        return "retry"

    text, html = None, False
    if kind == "open":
        text = pick_bag("open", OPEN_MESSAGES)
    elif kind == "push":
        text = pick_bag("push", PUSH_MESSAGES)
    elif kind == "nudge_morning":
        text = missing_text("morning", n, "☕ Утренний отчёт")
        if not text:
            return "skip"
    elif kind == "nudge_midday":
        text = missing_text("midday", n, "⏳ Обеденный отчёт")
    elif kind == "summary":
        today = biz_date(n)
        text = build_week(week_dates(today - timedelta(days=1)), "Клуб миллионеров — итоги недели") \
            if today.weekday() == 0 else build_yesterday(today)
        html = text is not None
    elif kind == "midday_stats":
        ent = day_entries(biz_date(n).isoformat(), "midday")
        ranked = sorted(((s, e["pct"]) for s, e in ent.items() if e.get("pct") is not None), key=lambda x: -x[1])
        ranked = [r for r in ranked if r[1] >= 30][:3]
        if len(ent) >= 8 and len(ranked) >= 2:
            top = ", ".join(f"{escape(s)} ({pct_str(p)})" for s, p in ranked)
            text = f"📈 К обеду впереди: {top}. Остальные, у вас вся вторая половина дня 😉"
            html = True

    if text is None and (kind == "joke" or fallback):
        if RT.streak >= SELF_SPAM_LIMIT:
            return "skip"  # бот уже говорит сам с собой — пауза
        text = joke_for(n)
        html = False
    if text is None:
        return "skip"
    if jokes_kind and text and not can_speak(n, jokes=True):
        return "skip"
    return "sent" if await say(text, html=html) else "retry"


async def tick() -> None:
    n = now()
    if not get_chat_id():
        return
    today = n.date().isoformat()
    done = STATE["done"].setdefault(today, [])
    for old in sorted(STATE["done"])[:-3]:
        STATE["done"].pop(old, None)
    for slot_id, h, m, kind, fallback in SLOTS:
        if slot_id in done:
            continue
        start = n.replace(hour=h, minute=m, second=0, microsecond=0)
        if n < start:
            continue
        if n > start + timedelta(minutes=SLOT_GRACE_MIN):
            done.append(slot_id)
            continue
        status = await run_slot(kind, fallback, n)
        if status in ("sent", "skip"):
            done.append(slot_id)
            save_state()
        break  # не больше одного слота за проход


async def scheduler_loop() -> None:
    while True:
        try:
            await tick()
        except Exception:
            log.exception("Ошибка планировщика")
        await asyncio.sleep(20)


# ============================ ВЕБ-СЕРВЕР ДЛЯ ХОСТИНГА ============================
async def handle_ping(request):
    return web.Response(text="OK, bot is alive")


async def start_keepalive_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()


async def main() -> None:
    load_state()
    await bot.delete_webhook(drop_pending_updates=True)
    await start_keepalive_server()
    asyncio.create_task(scheduler_loop())
    log.info("Бот запущен. Время в Одессе: %s", now().strftime("%Y-%m-%d %H:%M"))
    await dp.start_polling(bot, allowed_updates=["message", "edited_message", "message_reaction"])


if __name__ == "__main__":
    asyncio.run(main())
