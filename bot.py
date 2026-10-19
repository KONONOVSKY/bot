# -*- coding: utf-8 -*-
"""
TOP TEAM - рабочий бот для чата 16 магазинов.
Оптимизированная версия с умным восстановлением памяти, 
ночным режимом перезапуска и прямым пушем должников.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import secrets
import time
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from html import escape, unescape
from statistics import mean
from zoneinfo import ZoneInfo
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    Message, MessageReactionUpdated, ReactionTypeEmoji, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("topteam")

# ============================ НАСТРОЙКИ ============================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ENV_CHAT_ID = os.environ.get("CHAT_ID", "").strip()      
TEST_MODE = os.environ.get("TEST_MODE", "").strip().lower() in ("1", "true", "yes")  
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
TG_STATE_BACKUP = os.environ.get("TG_STATE_BACKUP", "1").strip().lower() not in ("0", "false", "no")

try:
    TZ = ZoneInfo("Europe/Kyiv")
except Exception:  
    TZ = ZoneInfo("Europe/Kiev")

SPEAK_FROM = (7, 30)     
SPEAK_UNTIL = (19, 0)    
REACT_AFTER_HOURS = True  
SLOT_GRACE_MIN = 25       
JOKE_AFTER_MANAGER_MIN = 30  
JOKE_MAX_DELAY_MIN = 120     

ADMIN_ID = 486540367
HQ_USER_IDS = {486540367, 987578033}
BOSS_USER_IDS = {394259493}
HQ_MARKERS = ("ᴋɴɴᴠsᴋʏ", "knnvsky")
BOT_NAME_MARKERS = ("смотрящий за оф2",)   
BOSS_MARKERS = ("гусев", "стелла яссин")
SILENT_USER_IDS = {486540367}
MANAGER_REACTIONS = ["👍", "🔥", "🫡", "🫡", "🫡", "❤️"]

STORE_BY_USER_ID = {
    1083997329: "Преображенська 52",
    5533202319: "Старосенная Киевстар",
    559046806: "Пантелеймоновская 25",
    1254629648: "Королёва Киевстар",
    1262155078: "Одесса Рабина",
    1380667136: "Осипова 25",
    993488483: "Черноморск",
    839363325: "Добровольского 137",
    6596253291: "Рабина Водафон",
    5288219863: "Белгород-Днестровский",
    7837076662: "Королёва Водафон",
    6450083571: "Измаил Водафон",
    912622659: "Киевстар арнаутская",
    7743570275: "Королёва лайф",
    1314535620: "Измаил Киевстар",
    1109152184: "Глушко 14",
}

ALIASES = {
    "измаил вф": "Измаил Водафон",
    "измаил водфон": "Измаил Водафон",
    "старосенная кс": "Старосенная Киевстар",
}

STORE_NAMES = tuple(dict.fromkeys(STORE_BY_USER_ID.values()))
USER_ID_BY_STORE = {v: k for k, v in STORE_BY_USER_ID.items()}

ALLOWED = {
    "👍", "🔥", "🎉", "🤩", "😎", "🏆", "💯", "⚡", "🤝", "👌", "🫡", "🤗", "🙏", "👀",
    "❤", "🤣", "😁", "👏", "💅", "🦄", "✅", "😔", "😢"
}
POSITIVE = {"👍", "🔥", "❤", "🎉", "🤩", "😎", "👏", "💯", "⚡", "🏆"}
NEGATIVE = {"👎", "🤡", "💩", "🥱", "🤮", "🤬", "🤨"}

# ============================ ВРЕМЯ ============================
def now() -> datetime:
    dt = datetime.now(TZ)
    if TEST_MODE:
        return dt.replace(hour=16, minute=0, second=0, microsecond=0)
    return dt

def biz_date(dt: datetime) -> date:
    return dt.date()

def in_speak_hours(n: datetime) -> bool:
    cur = (n.hour, n.minute)
    return SPEAK_FROM <= cur < SPEAK_UNTIL

def wall_now() -> datetime:
    return datetime.now(TZ)

def is_after_hard_mute() -> bool:
    cur = (wall_now().hour, wall_now().minute)
    return cur >= SPEAK_UNTIL or cur < SPEAK_FROM

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
STATE: dict = {
    "chat_id": None, "history": {}, "bags": {}, "done": {},
    "jokes_used": [], "queued_joke": None, "review_pending": {},
    "tech_reply_date": "", "fans": {},
    "jokes_disabled": False, "full_stop": False,
    "queued_stats": None, "pending_report_push": {}
}

class RT:
    last_human = 0.0
    last_bot = 0.0
    last_manager_message = 0.0
    mute_until = 0.0
    tech_at = 0.0
    jokes_paused_until = 0.0 
    current_day = ""
    greeted: set = set()
    last_bday = 0.0
    bot_msgs: dict = {} 
    is_suspended = True         
    awaiting_history = True     
    bot_username = ""
    bot_name_norm = ""
    pm_last_reply: dict = {}
    group_mentions: list = []           
    ignore_mentions_until: float = 0.0
    resumed_ts: float = 0.0     

def _apply_state_data(data: dict) -> None:
    if not isinstance(data, dict):
        raise ValueError("корень state.json должен быть объектом")
    STATE.update({k: data.get(k, STATE[k]) for k in STATE})
    migrate_state()

def load_state() -> None:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _apply_state_data(data)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.exception("Не удалось прочитать состояние: %s", e)

_last_tg_backup: float = 0.0

async def save_state_backup_to_telegram() -> None:
    global _last_tg_backup
    if not TG_STATE_BACKUP:
        return
    now_ts = time.time()
    if now_ts - _last_tg_backup < 60:
        return
    _last_tg_backup = now_ts
    try:
        state_bytes = json.dumps(STATE, ensure_ascii=False).encode("utf-8")
        file = BufferedInputFile(state_bytes, filename="state_backup.json")
        await bot.send_document(ADMIN_ID, file, caption="💾 Авто-бэкап state (перешли мне этот файл при сбое)")
    except Exception:
        pass

def migrate_state() -> None:
    old, new = "Измаил Водфон", "Измаил Водафон"
    for day in STATE.get("history", {}).values():
        if old in day:
            day.setdefault(new, day[old]); day.pop(old, None)

def save_state() -> None:
    try:
        hist = STATE.get("history", {})
        for old_day in sorted(hist)[:-40]:
            hist.pop(old_day, None)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(save_state_backup_to_telegram())
    except RuntimeError:
        pass

def env_chat_ids() -> list:
    ids = []
    for part in ENV_CHAT_ID.split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                pass
    return ids

def get_chat_id():
    ids = env_chat_ids()
    return ids[0] if ids else STATE.get("chat_id")

def is_allowed_chat(cid: int) -> bool:
    ids = env_chat_ids()
    return cid in ids if ids else cid == STATE.get("chat_id")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ============================ РАЗБОР ОТЧЁТОВ И УТИЛИТЫ ============================
CAT_PATTERNS = {
    "to": re.compile(r"^\W*(?:план\s+)?(?:[тt]\s*[оo0]|пт|то)(?![a-zа-я])(?!\s*(?:в\s+)?кредит)\s*(?:план\w*)?\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "smart": re.compile(r"^\W*(?:смарт|тел|смарты)\b\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "serv": re.compile(r"^\W*(?:серв|су|сервис)\b\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "aks": re.compile(r"^\W*(?:акс|аксы)\b\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "str": re.compile(r"^\W*(?:стр|страх|стрх)\b\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "trade": re.compile(r"^\W*(?:трейд|trade)\b\s*(?:ин)?\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "credit": re.compile(r"^\W*(?:кредит|крдт|то\s+(?:в\s+)?кредит)\b\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
}

def normalize_report_text(text: str) -> str:
    t = text.replace("\u00a0", " ").replace("\u200b", "").replace("\u202f", " ")
    t = re.sub(r'(?<=\d)\s+(?=\d)', '', t)
    t = t.replace('/', ' / ')
    t = re.sub(r"[.,](?=\d{3}(?!\d))", "", t)
    return t

def calc_pct(plan, fact, closed):
    if closed: return 100.0
    if plan and fact is not None and plan > 0: return fact / plan * 100.0
    return None

def _extract_nums_from_rest(rest: str):
    closed = "+" in rest or bool(re.search(r"закр[иы]т", rest, re.IGNORECASE))
    clean_rest = re.split(r"(?i)на\s+смен[еі]|на\s+змін[іи]|стажер", rest)[0]
    nums = [int(x) for x in re.findall(r"\d+", clean_rest)]
    return nums, closed

def parse_report(text: str):
    if not text or len(text) > 1800:
        return None
    t = normalize_report_text(text)
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    
    data = {"plan": None, "fact": None, "closed": False, "pct": None}
    found_to = False
    for l in lines:
        m_to = CAT_PATTERNS["to"].match(l)
        if m_to:
            rest = m_to.group("rest")
            nums, closed = _extract_nums_from_rest(rest)
            if closed:
                data["fact"] = nums[-1] if nums else None
                data["closed"] = True
            elif len(nums) >= 2:
                data["plan"], data["fact"] = nums[0], nums[1]
            elif len(nums) == 1:
                data["fact"] = nums[0]
            found_to = True
            break
            
    if not found_to: return None
    data["pct"] = calc_pct(data["plan"], data["fact"], data["closed"])
    return data

_SMALLCAPS = str.maketrans({"ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g", "ʜ": "h",
                            "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p",
                            "ꞯ": "q", "ʀ": "r", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z"})

def _norm_name(name: str) -> str:
    import unicodedata
    value = unicodedata.normalize("NFKC", name or "").translate(_SMALLCAPS).casefold().replace("ё", "е")
    value = re.sub(r"[^a-zа-яіїєґ0-9]+", " ", value)
    return " ".join(value.split())

def is_hq(name: str, user_id: int | None = None) -> bool:
    if user_id is not None and user_id in HQ_USER_IDS: return True
    low = _norm_name(name)
    return any(_norm_name(k) in low for k in HQ_MARKERS)

def is_boss(name: str, user_id: int | None = None) -> bool:
    if user_id is not None and user_id in BOSS_USER_IDS: return True
    low = _norm_name(name)
    return any(_norm_name(k) in low for k in BOSS_MARKERS)

def is_manager(name: str, user_id: int | None = None) -> bool:
    return is_hq(name, user_id) or is_boss(name, user_id)

def is_bot_name(name: str) -> bool:
    norm = _norm_name(name)
    if not norm: return False
    if RT.bot_name_norm and norm == RT.bot_name_norm: return True
    return any(_norm_name(k) == norm for k in BOT_NAME_MARKERS)

def store_name(name: str, user_id: int | None = None, report_text: str = "") -> str:
    if user_id is not None and user_id in STORE_BY_USER_ID:
        return STORE_BY_USER_ID[user_id]
    original = (name or "").strip()
    norm = _norm_name(original)
    alias_norm = {_norm_name(k): v for k, v in ALIASES.items()}
    if norm in alias_norm:
        return alias_norm[norm]
    head = _norm_name("\n".join((report_text or "").splitlines()[:2])[:120])
    text_markers = [
        ("осипова", "Осипова 25"), ("черноморск", "Черноморск"),
        ("старосенная", "Старосенная Киевстар"), ("преображенська 52", "Преображенська 52"),
        ("добровольского 137", "Добровольского 137"), ("пантелеймоновская 25", "Пантелеймоновская 25"),
        ("белгород днестровский", "Белгород-Днестровский"), ("глушко 14", "Глушко 14"),
        ("королева киевстар", "Королёва Киевстар"), ("королева водафон", "Королёва Водафон"),
        ("королева лайф", "Королёва лайф"), ("киевстар арнаутская", "Киевстар арнаутская"),
        ("измаил киевстар", "Измаил Киевстар"), ("измаил водафон", "Измаил Водафон"),
        ("рабина водафон", "Рабина Водафон"), ("одесса рабина", "Одесса Рабина"),
    ]
    for marker, canonical in sorted(text_markers, key=lambda item: len(item[0]), reverse=True):
        if marker in head: return canonical
    for candidate in sorted(STORE_NAMES, key=lambda s: len(_norm_name(s)), reverse=True):
        if _norm_name(candidate) in norm: return candidate
    return original or "Неизвестная точка"

# ============================ ИСТОРИЯ И СТАТИСТИКА ============================
def record_report(store: str, window: str, dt: datetime, rep: dict) -> bool:
    if store not in STORE_NAMES or window not in ("morning", "midday", "evening"): return False
    bd = biz_date(dt).isoformat()
    day = STATE["history"].setdefault(bd, {})
    store_data = day.setdefault(store, {})
    old = store_data.get(window)
    if old and old.get("t"):
        try:
            old_minutes = int(old["t"].split(":")[0]) * 60 + int(old["t"].split(":")[1])
            if old_minutes > dt.hour * 60 + dt.minute: return False
        except (TypeError, ValueError, IndexError): pass
    store_data[window] = {
        "plan": rep["plan"], "fact": rep["fact"], "closed": rep["closed"],
        "pct": rep["pct"], "t": dt.strftime("%H:%M"),
    }
    return True

def day_entries(bd: str, window: str) -> dict:
    return {s: w[window] for s, w in STATE["history"].get(bd, {}).items() if window in w}

def expected_stores(window: str, today: str) -> set:
    if window in ("morning", "midday"): return set(STORE_NAMES)
    return set(STORE_NAMES)

def pct_str(p) -> str:
    return f"{p:.0f}%"

def evening_stats(dates: list) -> dict:
    res: dict = {}
    for d in dates:
        for s, e in day_entries(d, "evening").items():
            if e.get("pct") is not None:
                res.setdefault(s, []).append((d, e["pct"]))
    return res

async def generate_and_send_evening_stats(n: datetime):
    today = biz_date(n)
    today_str = today.isoformat()
    today_data = STATE["history"].get(today_str, {})
    today_valid = {}
    
    for s in STORE_NAMES:
        store_day = today_data.get(s, {})
        best_rep = store_day.get("evening") or store_day.get("midday") or store_day.get("morning")
        if best_rep and best_rep.get("fact") is not None and best_rep.get("pct") is not None:
            today_valid[s] = best_rep
            
    top_today_fact = sorted(today_valid.items(), key=lambda x: -x[1]["fact"])[:3]
    top_today_pct = sorted(today_valid.items(), key=lambda x: -x[1]["pct"])[:3]
    
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🌙 <b>Итоги дня ({today.strftime('%d.%m')}):</b>", "<b>По сумме:</b>"]
    for i, (s, rep) in enumerate(top_today_fact):
        lines.append(f"{medals[i]} {escape(s)} - {rep['fact']} грн ({pct_str(rep['pct'])})")
    if not top_today_fact: lines.append("<i>Нет данных</i>")
    
    lines.append("\n<b>По выполнению:</b>")
    for i, (s, rep) in enumerate(top_today_pct):
        lines.append(f"{medals[i]} {escape(s)} - {pct_str(rep['pct'])}")
    if not top_today_pct: lines.append("<i>Нет данных</i>")
    
    report_text = "\n".join(lines)
    try:
        await bot.send_message(ADMIN_ID, report_text, parse_mode="HTML")
    except Exception: pass

# ============================ БАНК ШУТОК И ТРИГГЕРЫ ============================
JOKES = {
    "general": [
        "🧮 Продавец - это когда ты гуманитарий, но рассрочку на 10 платежей с учетом допов считаешь в уме быстрее калькулятора.",
        "🤝 Тот самый момент, когда закрыл сложного клиента на флагман со всеми допами. Ощущение, будто где-то на фоне одобрительно кивнул Денис Гусев.",
        "🎯Покупатель уверен, что 13-й айфон с разбитым в крошку экраном по трейд-ину покроет 90% стоимости нового. Просто киваем и мягко возвращаем его в реальность.",
        "📱 Закон подлости: твой личный телефон разрывается от звонков ровно в тот момент, когда ты формируешь чек на 50к.",
        "🛍 Идеальная продажа - это когда клиент уходит с ощущением, что он сам всё решил, а ты просто вовремя пробивал чек.",
        "🧠 Когда 1С висит на оплате терминалом уже третью минуту, а клиент смотрит тебе прямо в душу. В этот момент важно уметь притвориться NPC.",
        "😎 Не каждый герой носит плащ, но некоторые умеют перенести контакты за 4000.",
        "🥊 Гусев может дожать на рассрочку с допами даже неработающий банкомат.",
        "🙅‍♀️Продать телефон без сервиса в ОФ2 - это red flag, после которого Стелла лично удалит тебя из чата."
    ]
}

TECH_RE = re.compile(r"(?<!\w)1[сc](?!\w)|сбой|збій|не\s?работает|нет\s+связи", re.IGNORECASE)
TECH_FIXED_RE = re.compile(r"заработал|уже\s+работает|всё\s+ок", re.IGNORECASE)
FAREWELL_RE = re.compile(r"последн\w+\s+рабоч", re.IGNORECASE)
GREET_RE = re.compile(r"доброе утро|доброго ранку|добрый день|добрий день", re.IGNORECASE)
BDAY_RE = re.compile(r"с\s+дн[её]м\s+рожд", re.IGNORECASE)

def _joke_norm(text: str) -> str: return re.sub(r"[\W_]+", " ", (text or "").lower().replace("ё", "е")).strip()
def _joke_key(norm: str) -> str: return hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]

def mark_joke_used(text: str) -> None:
    norm = _joke_norm(text)
    if len(norm) < 15: return
    key = _joke_key(norm)
    used = STATE.setdefault("jokes_used", [])
    for e in used:
        if e.get("k") == key:
            e["ts"] = time.time()
            return
    used.append({"k": key, "n": norm[:300], "t": text[:300], "ts": time.time(), "likes": 0})
    del used[:-200]

def queued_joke_for(n: datetime) -> str:
    current = STATE.get("queued_joke")
    if current and current in JOKES["general"]: return current
    fresh = [j for j in JOKES["general"] if _joke_key(_joke_norm(j)) not in [e.get("k") for e in STATE.get("jokes_used", [])]]
    chosen = random.choice(fresh or JOKES["general"])
    STATE["queued_joke"] = chosen
    save_state()
    return chosen

def jokes_temporarily_paused() -> bool: return time.time() < RT.jokes_paused_until

def mute_for(minutes: float) -> None:
    RT.mute_until = max(RT.mute_until, time.time() + minutes * 60)

async def say(text: str, msg: Message | None = None, html: bool = False, track_deletable: bool = False) -> bool:
    kwargs = {"parse_mode": "HTML"} if html else {}
    if msg is not None and msg.chat.type in ("group", "supergroup") and is_after_hard_mute(): return False
    try:
        if msg is not None:
            sent = await msg.reply(text, **kwargs)
        else:
            cid = get_chat_id()
            if not cid or is_after_hard_mute(): return False
            sent = await bot.send_message(cid, text, **kwargs)
            
        if track_deletable:
            RT.bot_msgs[sent.message_id] = {"neg": set(), "text": text}
    except Exception:
        return False
    RT.last_bot = time.time()
    return True

async def react(msg: Message, pool: list) -> bool:
    emojis = [e for e in pool if e in ALLOWED]
    random.shuffle(emojis)
    for e in emojis[:2]:
        try:
            await bot.set_message_reaction(chat_id=msg.chat.id, message_id=msg.message_id, reaction=[ReactionTypeEmoji(emoji=e)])
            return True
        except Exception: pass
    return False

# ============================ РАСПИСАНИЕ ============================
SLOTS = [
    ("greet", 8, 30, "greeting"),
    ("nudge_m", 8, 55, "nudge_morning"),
    ("j11", 11, 0, "joke"),
    ("j12", 12, 0, "joke"),
    ("nudge_d", 13, 30, "nudge_midday"),
    ("j15", 15, 0, "joke"),
    ("j16", 16, 0, "joke"),
]

def mark_slot_done(done: list, slot_id: str) -> None:
    if slot_id not in done:
        done.append(slot_id)

def build_plan(n: datetime) -> str:
    done = STATE["done"].get(n.date().isoformat(), [])
    lines = []
    for slot_id, h, minute, kind in SLOTS:
        if slot_id in done: continue
        start = n.replace(hour=h, minute=minute, second=0, microsecond=0)
        if start > n: lines.append(f"• {start:%H:%M} - {kind}")
    return "\n".join(lines) if lines else "На сегодня больше плановых задач нет."

def force_resume(n: datetime) -> None:
    done = STATE["done"].setdefault(n.date().isoformat(), [])
    for slot_id, h, minute, _ in SLOTS:
        if n.replace(hour=h, minute=minute, second=0, microsecond=0) <= n:
            mark_slot_done(done, slot_id)
    STATE["full_stop"] = False
    STATE["jokes_disabled"] = False
    save_state()
    RT.is_suspended = False
    RT.awaiting_history = False
    RT.resumed_ts = time.time()

# ============================ КЛАВИАТУРЫ И ПАНЕЛЬ ============================
def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Режимы работы", callback_data="menu:modes")],
        [InlineKeyboardButton(text="📊 Отчёты и Пуш", callback_data="menu:reports")],
        [InlineKeyboardButton(text="💾 Скачать state.json", callback_data="admin:state")]
    ])

def admin_modes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Нормальный режим", callback_data="mode:normal")],
        [InlineKeyboardButton(text="🤫 Без шуток (Постоянно)", callback_data="mode:no_jokes")],
        [InlineKeyboardButton(text="🔴 Полный стоп", callback_data="mode:full_stop")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu:main")]
    ])

def admin_reports_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Напомнить всем (в ЧАТ)", callback_data="admin:push_chat")],
        [InlineKeyboardButton(text="🎯 Напомнить всем (в ЛС)", callback_data="admin:push_pm")],
        [InlineKeyboardButton(text="📋 Должники списком", callback_data="admin:missing")],
        [InlineKeyboardButton(text="📊 Статистика за сегодня", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu:main")]
    ])

def admin_panel_text(note: str | None = None) -> str:
    status = "🟢 НОРМАЛЬНЫЙ"
    if STATE.get("full_stop", False) or RT.is_suspended: status = "🔴 ПОЛНЫЙ СТОП"
    elif STATE.get("jokes_disabled", False): status = "🤫 БЕЗ ШУТОК"
    
    parts = [
        "👑 <b>Панель управления TOP TEAM</b>",
        f"Режим бота: <b>{status}</b>",
        f"Режим тишины (мут): <b>{'Выключен' if RT.mute_until < time.time() else datetime.fromtimestamp(RT.mute_until, TZ).strftime('%H:%M:%S')}</b>",
        ""
    ]
    if note: parts.insert(0, f"💬 <i>{escape(note)}</i>\n")
    return "\n".join(parts)

# ============================ ОБРАБОТЧИК ДОКУМЕНТОВ ============================
@dp.message(F.document)
async def handle_document(message: Message):
    if message.chat.type != "private" or message.from_user.id != ADMIN_ID:
        return
        
    doc_name = message.document.file_name or ""
    
    if doc_name.startswith("state") and doc_name.endswith(".json"):
        await message.reply("⏳ Восстанавливаю память из бэкапа...")
        try:
            file_in_io = BytesIO()
            await bot.download(message.document, destination=file_in_io)
            file_in_io.seek(0)
            data = json.load(file_in_io)
            
            _apply_state_data(data)
            save_state()
            
            n = now()
            history_days = len(STATE.get("history", {}))
            today_reports = sum(
                1 for s in STATE["history"].get(n.date().isoformat(), {}).values()
                if any(w in s for w in ("morning", "midday", "evening"))
            )
            
            RT.awaiting_history = False
            
            if n.hour >= 19:
                force_resume(n)
                await message.reply(f"✅ Состояние успешно загружено ({today_reports} отчетов за сегодня). Рабочий день окончен, генерирую вечернюю сводку.")
                await generate_and_send_evening_stats(n)
            else:
                force_resume(n)
                await message.reply(f"✅ Состояние восстановлено! Найдено {today_reports} отчетов за сегодня. Бот продолжает работу по графику.", reply_markup=admin_main_keyboard())
        except Exception as e:
            await message.reply(f"❌ Ошибка загрузки бэкапа: {e}")
        return

    if doc_name == "result.json":
        await message.reply("⏳ Анализирую result.json...")
        try:
            file_in_io = BytesIO()
            await bot.download(message.document, destination=file_in_io)
            file_in_io.seek(0)
            data = json.load(file_in_io)
            
            n = now()
            today_str = n.date().isoformat()
            
            # Быстрый парсинг json (упрощенно для примера)
            parsed = 0
            for msg in data.get("messages", []):
                if msg.get("type") != "message": continue
                text_str = "".join(item if isinstance(item, str) else item.get("text", "") for item in (msg.get("text", []) if isinstance(msg.get("text"), list) else [msg.get("text")])).strip()
                date_str = msg.get("date")
                if not date_str or today_str not in str(date_str): continue
                
                rep = parse_report(text_str)
                if rep:
                    sender = msg.get("from", "")
                    store = store_name(sender, None, text_str)
                    dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00")).replace(tzinfo=TZ)
                    if record_report(store, classify_window(dt), dt, rep):
                        parsed += 1

            if parsed == 0:
                await message.reply(f"❌ В этом файле НЕТ отчетов за сегодня ({today_str}). Загрузи свежий экспорт или перешли мне последний state_backup.json.")
                return
                
            save_state()
            RT.awaiting_history = False
            force_resume(n)
            await message.reply(f"✅ Найдено {parsed} отчетов за сегодня! Бот запущен в работу.", reply_markup=admin_main_keyboard())
        except Exception as e:
            await message.reply(f"❌ Ошибка: {e}")

# ============================ ЛС И CALLBACKS ============================
@dp.message(F.chat.type == "private")
async def handle_private_message(message: Message) -> None:
    if message.from_user.id != ADMIN_ID: return
    if (message.text or "").startswith("/say "):
        cid = get_chat_id()
        if cid and not is_after_hard_mute():
            await bot.send_message(cid, message.text.replace("/say ", "", 1))
            await message.reply("✅ Отправлено.")
        return
    await message.reply(admin_panel_text(), parse_mode="HTML", reply_markup=admin_main_keyboard())

@dp.callback_query()
async def on_callback_query(callback: CallbackQuery) -> None:
    data = callback.data or ""
    if callback.from_user.id != ADMIN_ID: return
    n = now()
    
    try:
        if data == "menu:main":
            await callback.message.edit_text(admin_panel_text(), parse_mode="HTML", reply_markup=admin_main_keyboard())
        elif data == "menu:modes":
            await callback.message.edit_text(admin_panel_text(), parse_mode="HTML", reply_markup=admin_modes_keyboard())
        elif data == "menu:reports":
            await callback.message.edit_text(admin_panel_text(), parse_mode="HTML", reply_markup=admin_reports_keyboard())
            
        elif data == "mode:normal":
            STATE["full_stop"] = False; STATE["jokes_disabled"] = False
            RT.is_suspended = False; RT.resumed_ts = time.time(); save_state()
            await callback.message.edit_text(admin_panel_text("Переключено на Нормальный режим!"), parse_mode="HTML", reply_markup=admin_modes_keyboard())
        elif data == "mode:no_jokes":
            STATE["jokes_disabled"] = True; STATE["full_stop"] = False
            RT.is_suspended = False; save_state()
            await callback.message.edit_text(admin_panel_text("Режим Без шуток включен!"), parse_mode="HTML", reply_markup=admin_modes_keyboard())
        elif data == "mode:full_stop":
            STATE["full_stop"] = True; RT.is_suspended = True; save_state()
            await callback.message.edit_text(admin_panel_text("Полный стоп активирован!"), parse_mode="HTML", reply_markup=admin_modes_keyboard())

        elif data in ("admin:push_chat", "admin:push_pm"):
            win = classify_window(n)
            bd = biz_date(n).isoformat()
            reported = set(day_entries(bd, win))
            expected = expected_stores(win, bd)
            miss = sorted(expected - reported)
            
            if not miss:
                await callback.answer("Все отчеты сданы! Должников нет.", show_alert=True)
                return
                
            if data == "admin:push_chat":
                cid = get_chat_id()
                if cid:
                    await bot.send_message(cid, f"⏳ Напоминаю, ждем отчеты от: {', '.join(miss)} 👀")
                    await callback.answer("Пуш отправлен в общий чат!")
            else:
                for m in miss:
                    tid = USER_ID_BY_STORE.get(m)
                    if tid:
                        try:
                            await bot.send_message(tid, "⏳ Напоминаю: время сдавать отчёт. Жду цифры в общем чате!")
                        except Exception: pass
                await callback.answer("Напоминания разосланы в ЛС!")
                
        elif data == "admin:missing":
            bd = biz_date(n).isoformat()
            lines = [f"📋 <b>Не сдали отчёты за {bd}:</b>"]
            for win, title in (("morning", "Утро"), ("midday", "Обед"), ("evening", "Вечер")):
                reported = set(day_entries(bd, win))
                missing = sorted(expected_stores(win, bd) - reported)
                lines.append(f"<b>{title} ({len(reported)}/16):</b> " + (", ".join(missing) if missing else "все сдали"))
            await callback.message.reply("\n".join(lines), parse_mode="HTML")
            
        elif data == "admin:state":
            file = BufferedInputFile(json.dumps(STATE, ensure_ascii=False, indent=2).encode('utf-8'), filename="state.json")
            await callback.message.reply_document(file, caption="Актуальный state.json")

    except Exception: pass
    await callback.answer()

# ============================ ОБРАБОТЧИК ЧАТА ============================
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def process(message: Message) -> None:
    if not is_allowed_chat(message.chat.id): return
    user = message.from_user
    if not user or user.is_bot: return

    text = (message.text or message.caption or "").strip()
    n = now()
    
    if text:
        rep = parse_report(text)
        if rep:
            store = store_name(user.full_name, user.id, text)
            if store in STORE_NAMES:
                dt = message.date.astimezone(TZ) if message.date else wall_now()
                window = classify_window(dt)
                record_report(store, window, dt, rep)
                save_state()
                
                # Старые сообщения из очереди (более 10 мин) не комментируем
                age = max(0.0, (wall_now() - dt).total_seconds())
                if age <= 600 and not STATE.get("full_stop") and not RT.is_suspended:
                    pool = ["❤"] if window == "morning" else (["🔥", "🤩"] if rep["closed"] or (rep["pct"] and rep["pct"] >= 100) else ["🤝"])
                    await react(message, pool)
                return

    if STATE.get("full_stop"): return
    if is_manager(user.full_name or "", user.id):
        await react(message, MANAGER_REACTIONS)
        RT.last_human = time.time(); RT.jokes_paused_until = time.time() + 30 * 60
        return

# ============================ ПЛАНИРОВЩИК ============================
async def run_slot(kind: str, n: datetime) -> str:
    if STATE.get("full_stop") or (kind == "joke" and STATE.get("jokes_disabled")): return "skip"
    if not can_speak(n, ignore_mute=(kind in ("nudge_morning", "nudge_midday"))): return "retry"

    text = None
    if kind == "greeting":
        text = random.choice(["☀️ Доброе утро, команда! Желаю отличных продаж и мощных чеков!"])
    elif kind in ("nudge_morning", "nudge_midday"):
        if RT.resumed_ts and time.time() - RT.resumed_ts < 120: return "retry"
        window = "morning" if kind == "nudge_morning" else "midday"
        today = biz_date(n).isoformat()
        reported = set(day_entries(today, window))
        if len(reported) >= 16: return "skip"
        
        miss = sorted(expected_stores(window, today) - reported)
        if miss:
            label = "☕ Утренний отчёт" if window == "morning" else "⏳ Обеденный отчёт"
            text = f"{label} ещё ждём от: {', '.join(miss[:8])}{' и др.' if len(miss) > 8 else ''}. Коллеги, не забываем 🙏"
            
    elif kind == "joke":
        if jokes_temporarily_paused(): return "retry"
        text = queued_joke_for(n)

    if not text: return "skip"
    if await say(text):
        if kind == "joke": mark_joke_used(text); STATE["queued_joke"] = None; save_state()
        return "sent"
    return "retry"

async def tick() -> None:
    n = now()
    if not get_chat_id(): return
    today = n.date().isoformat()
    done = STATE["done"].setdefault(today, [])
    
    for slot_id, h, m, kind in SLOTS:
        if slot_id in done: continue
        start = n.replace(hour=h, minute=m, second=0, microsecond=0)
        if n < start: continue
        if kind == "joke" and n >= min(start + timedelta(minutes=JOKE_MAX_DELAY_MIN), start.replace(hour=13 if start.hour < 13 else 17)):
            done.append(slot_id); continue
        elif n > start + timedelta(minutes=SLOT_GRACE_MIN):
            done.append(slot_id); continue

        status = await run_slot(kind, n)
        if status in ("sent", "skip"): mark_slot_done(done, slot_id); save_state(); break
        if status == "retry" and kind not in ("joke",): break

async def scheduler_loop() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            if not STATE.get("full_stop") and not RT.is_suspended and not is_after_hard_mute():
                await tick()
        except Exception: pass
        await asyncio.sleep(20)

async def start_keepalive_server() -> None:
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()

async def main() -> None:
    load_state()
    await bot.delete_webhook(drop_pending_updates=False)
    await start_keepalive_server()
    asyncio.create_task(scheduler_loop())

    n = now()
    history_days = len(STATE.get("history", {}))
    today_reports = sum(1 for s in STATE["history"].get(n.date().isoformat(), {}).values() if any(w in s for w in ("morning", "midday", "evening")))

    if today_reports == 0:
        msg = "🔄 <b>Бот запущен, но память пуста (Render стер локальные данные).</b>\n\nПерешли мне последний файл 💾 <code>state_backup.json</code> (я отправлял его выше), чтобы я вспомнил все отчеты за сегодня!"
    elif n.hour >= 19:
        msg = "🔄 <b>Бот перезапущен после 19:00. Сегодняшние данные на месте.</b>\nРабочий день окончен. Спамить в общий чат не буду, перехожу в ночной режим ожидания."
        force_resume(n)
        await generate_and_send_evening_stats(n)
    else:
        msg = "🔄 <b>Бот успешно перезапущен и готов к работе.</b> Все сегодняшние отчеты на месте."

    try:
        await bot.send_message(ADMIN_ID, msg, parse_mode="HTML", reply_markup=admin_main_keyboard())
    except Exception: pass

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())