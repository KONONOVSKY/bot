# -*- coding: utf-8 -*-
"""
TOP TEAM — рабочий бот для чата 16 магазинов (Сота Альянс, Одесский регион).
Режимы: active / react_only / silent / stop
Умная идентификация отчётов, напоминания, реакции, ответы на теги, 1С-шутки.
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
from io import BytesIO
from statistics import mean
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, MessageReactionUpdated, ReactionTypeEmoji,
)
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("topteam")

# ══════════════════════════════════════════════════════════════
# НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Задайте переменную окружения BOT_TOKEN.")

ENV_CHAT_ID  = os.environ.get("CHAT_ID", "").strip()
TEST_MODE    = os.environ.get("TEST_MODE", "").strip().lower() in ("1", "true", "yes")
STATE_FILE   = os.environ.get("STATE_FILE", "state.json")

try:
    TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    TZ = ZoneInfo("Europe/Kiev")

SPEAK_FROM        = (7, 30)
SPEAK_UNTIL       = (19, 0)
REACT_AFTER_HOURS = True
SLOT_GRACE_MIN    = 25
JOKE_AFTER_MANAGER_MIN = 30
JOKE_MAX_DELAY_MIN     = 60

# Идентификаторы
ADMIN_ID      = 486540367
HQ_USER_IDS   = {486540367, 987578033}
BOSS_USER_IDS = {394259493}
HQ_MARKERS    = ("ᴋɴɴᴠsᴋʏ", "knnvsky")
BOSS_MARKERS  = ("гусев", "стелла яссин")
BOT_NAME_MARKERS = ("смотрящий за оф2",)

HISTORY_DEBOUNCE_SEC  = 4.0
HISTORY_CONTINUE_SEC  = 120.0
MANAGER_REACTIONS     = ["👍", "🔥", "🫡", "🫡", "🫡", "❤️"]

STORE_BY_USER_ID: dict[int, str] = {
    1083997329: "Преображенська 52",
    5533202319: "Старосенная Киевстар",
    559046806:  "Пантелеймоновская 25",
    1254629648: "Королёва Киевстар",
    1262155078: "Одесса Рабина",
    1380667136: "Осипова 25",
    993488483:  "Черноморск",
    839363325:  "Добровольского 137",
    6596253291: "Рабина Водафон",
    5288219863: "Белгород-Днестровский",
    7837076662: "Королёва Водафон",
    6450083571: "Измаил Водафон",
    912622659:  "Киевстар арнаутская",
    7743570275: "Королёва лайф",
    1314535620: "Измаил Киевстар",
    1109152184: "Глушко 14",
}

ALIASES = {
    "измаил вф":     "Измаил Водафон",
    "измаил водфон": "Измаил Водафон",
    "старосенная кс":"Старосенная Киевстар",
}

STORE_NAMES    = tuple(dict.fromkeys(STORE_BY_USER_ID.values()))
USER_ID_BY_STORE = {v: k for k, v in STORE_BY_USER_ID.items()}
TOTAL_STORES   = len(STORE_NAMES)

# Реакции
ALLOWED  = {
    "👍","🔥","🎉","🤩","😎","🏆","💯","⚡","🤝","👌","🫡","🤗","🙏","👀",
    "❤","🤣","😁","👏","💅","🦄","✅","😔","😢","😲","🤯",
}
POSITIVE = {"👍","🔥","❤","🎉","🤩","😎","👏","💯","⚡","🏆"}
# Негативные реакции + зумерский слэнг (🥵 = душно = негативно)
NEGATIVE = {"👎","🤡","💩","🥱","🤮","🤬","🤨","🥵"}

MONTHS_MAP = {"янв":1,"фев":2,"мар":3,"апр":4,"мая":5,"июн":6,
              "июл":7,"авг":8,"сен":9,"окт":10,"ноя":11,"дек":12}

# ══════════════════════════════════════════════════════════════
# ВРЕМЯ
# ══════════════════════════════════════════════════════════════
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

# ══════════════════════════════════════════════════════════════
# СОСТОЯНИЕ
# ══════════════════════════════════════════════════════════════
STATE: dict = {
    "chat_id": None, "history": {}, "bags": {}, "done": {},
    "jokes_used": [], "queued_joke": None, "review_pending": {},
    "tech_reply_date": "", "fans": {},
    "bot_mode": "active",          # active | react_only | silent | stop
    "queued_stats": None, "pm_push_enabled": True,
    "pending_report_push": {}, "custom_jokes": [],
}

class RT:
    last_human            = 0.0
    last_bot              = 0.0
    last_manager_message  = 0.0
    mute_until            = 0.0
    tech_at               = 0.0
    jokes_paused_until    = 0.0
    current_day           = ""
    greeted: set          = set()
    last_bday             = 0.0
    bot_msgs: dict        = {}      # msg_id -> {neg: set, text: str, type: str}
    is_suspended          = True
    awaiting_history      = True
    awaiting_confirmation = False
    bot_username          = ""
    bot_name_norm         = ""
    pm_last_reply: dict   = {}
    group_mentions: list  = []      # временные метки тегов не-менеджеров
    ignore_mentions_until = 0.0
    startup_ts            = 0.0
    resumed_ts            = 0.0

def get_bot_mode() -> str:
    return STATE.get("bot_mode", "active")

def _apply_state_data(data: dict) -> None:
    if not isinstance(data, dict):
        raise ValueError("корень state.json должен быть объектом")
    if not isinstance(data.get("history", {}), dict) or not isinstance(data.get("done", {}), dict):
        raise ValueError("некорректная структура history/done")
    STATE.update({k: data.get(k, STATE[k]) for k in STATE})
    migrate_state()
    log.info("Состояние применено: дней истории=%d", len(STATE.get("history", {})))

def load_state() -> None:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _apply_state_data(data)
    except FileNotFoundError:
        log.warning("Файл состояния %s не найден.", STATE_FILE)
    except Exception as e:
        log.exception("Не удалось прочитать состояние %s: %s", STATE_FILE, e)

def migrate_state() -> None:
    # переименование старого магазина
    old, new = "Измаил Водфон", "Измаил Водафон"
    for day in STATE.get("history", {}).values():
        if old in day:
            day.setdefault(new, day[old])
            day.pop(old, None)
    # миграция старых полей режима
    if "bot_mode" not in STATE:
        if STATE.get("full_stop"):
            STATE["bot_mode"] = "stop"
        elif STATE.get("jokes_disabled"):
            STATE["bot_mode"] = "react_only"
        else:
            STATE["bot_mode"] = "active"
    # удаление устаревших ключей
    for obsolete in ("suggested_jokes", "vibe_history_stats", "last_auto_gen_date",
                     "full_stop", "jokes_disabled"):
        STATE.pop(obsolete, None)

def save_state() -> None:
    try:
        hist = STATE.get("history", {})
        for old_day in sorted(hist)[:-40]:
            hist.pop(old_day, None)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning("Не удалось сохранить состояние: %s", e)

def env_chat_ids() -> list[int]:
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
dp  = Dispatcher()

# ══════════════════════════════════════════════════════════════
# РАЗБОР ОТЧЁТОВ
# ══════════════════════════════════════════════════════════════
CAT_PATTERNS = {
    "to":     re.compile(r"^\W*(?:план\s+)?(?:[тt]\s*[оo0]|[оo0]\s*[тt]|пт|то|от|ot)(?![a-zа-я])(?!\s*(?:в\s+)?кредит)\s*(?:план\w*)?\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "smart":  re.compile(r"^\W*(?:смарт|тел|смарты)\b\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "serv":   re.compile(r"^\W*(?:серв|су|сервис)\b\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "aks":    re.compile(r"^\W*(?:акс|аксы)\b\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "str":    re.compile(r"^\W*(?:стр|страх|стрх)\b\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "trade":  re.compile(r"^\W*(?:трейд|trade)\b\s*(?:ин)?\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
    "credit": re.compile(r"^\W*(?:кредит|крдт|то\s+(?:в\s+)?кредит)\b\s*[:\-]?\s*(?P<rest>.*)$", re.IGNORECASE),
}

# Паттерн для нечёткой идентификации отчёта (даже с опечатками)
REPORT_HINT_RE = re.compile(
    r"(?:"
    r"[тt]\s*[оo0]|[оo0]\s*[тt]|товар\w*\s*оборот|тов\.?\s*об|"
    r"отч[её]т|отч\.|рапорт|репорт|рпт|рапот|"          # «отчёт» + опечатки
    r"смарт|тел(?:еф)?|акс|серв|страх|трейд|кредит|"    # строки показателей
    r"план|факт|₴|грн"                                    # метки плана/факта
    r")",
    re.IGNORECASE,
)

def extract_text(text_data) -> str:
    if isinstance(text_data, list):
        return "".join(t if isinstance(t, str) else t.get("text", "") for t in text_data)
    return str(text_data)

def normalize_report_text(text: str) -> str:
    t = text.replace("\u00a0", " ").replace("\u200b", "").replace("\u202f", " ")
    t = re.sub(r"(?<=\d)\s+(?=\d)", "", t)
    t = t.replace("/", " / ")
    t = re.sub(r"[.,](?=\d{3}(?!\d))", "", t)
    return t

def calc_pct(plan, fact, closed):
    if closed:
        return 100.0
    if plan and fact is not None and plan > 0:
        return fact / plan * 100.0
    return None

def _extract_nums_from_rest(rest: str):
    closed = "+" in rest or bool(re.search(r"закр[иы]т", rest, re.IGNORECASE))
    clean_rest = re.split(r"(?i)на\s+смен[еі]|на\s+змін[іи]|стажер", rest)[0]
    nums = [int(x) for x in re.findall(r"\d+", clean_rest)]
    return nums, closed

def parse_report(text: str):
    """
    Разбирает текст как отчёт магазина.
    Понимает разные форматы и опечатки (напр. «ТО», «от», «тo», «OT», «тов.об.»).
    Возвращает dict с полями или None.
    """
    if not text or len(text) > 1800:
        return None
    # Быстрая предфильтрация: должен содержать хотя бы одну числовую строку
    # и хоть один признак отчёта
    if not any(ch.isdigit() for ch in text):
        return None
    if not REPORT_HINT_RE.search(text):
        return None

    t = normalize_report_text(text)
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    data = {
        "plan": None, "fact": None, "closed": False, "pct": None,
        "smart": {"plan": None, "fact": None},
        "serv":  {"plan": None, "fact": None},
        "aks":   {"plan": None, "fact": None},
        "trade": None, "ins": None, "credit": None,
    }
    found_to = False
    for l in lines:
        m = CAT_PATTERNS["to"].match(l)
        if m:
            rest = m.group("rest")
            nums, closed = _extract_nums_from_rest(rest)
            if closed:
                data["fact"] = nums[-1] if nums else None
                data["closed"] = True
            elif len(nums) >= 2:
                data["plan"], data["fact"] = nums[0], nums[1]
            elif len(nums) == 1:
                data["fact"] = nums[0]
            found_to = True
            continue
        for cat in ("smart", "serv", "aks"):
            mc = CAT_PATTERNS[cat].match(l)
            if mc:
                nums, _ = _extract_nums_from_rest(mc.group("rest"))
                if len(nums) >= 2:
                    data[cat]["plan"], data[cat]["fact"] = nums[0], nums[1]
                elif len(nums) == 1:
                    data[cat]["fact"] = nums[0]
                break
        ms = CAT_PATTERNS["str"].match(l)
        if ms:
            nums, _ = _extract_nums_from_rest(ms.group("rest"))
            if nums:
                data["ins"] = nums[-1]
            continue
        mt = CAT_PATTERNS["trade"].match(l)
        if mt:
            nums, _ = _extract_nums_from_rest(mt.group("rest"))
            if nums:
                data["trade"] = nums[-1]
            continue
        mc2 = CAT_PATTERNS["credit"].match(l)
        if mc2:
            nums, _ = _extract_nums_from_rest(mc2.group("rest"))
            if nums:
                data["credit"] = nums[-1]
            continue

    if not found_to:
        return None
    data["pct"] = calc_pct(data["plan"], data["fact"], data["closed"])
    return data

# ══════════════════════════════════════════════════════════════
# ИДЕНТИФИКАЦИЯ ПОЛЬЗОВАТЕЛЕЙ
# ══════════════════════════════════════════════════════════════
_SMALLCAPS = str.maketrans({
    "ᴀ":"a","ʙ":"b","ᴄ":"c","ᴅ":"d","ᴇ":"e","ꜰ":"f","ɢ":"g","ʜ":"h",
    "ɪ":"i","ᴊ":"j","ᴋ":"k","ʟ":"l","ᴍ":"m","ɴ":"n","ᴏ":"o","ᴘ":"p",
    "ꞯ":"q","ʀ":"r","ꜱ":"s","ᴛ":"t","ᴜ":"u","ᴠ":"v","ᴡ":"w","ʏ":"y","ᴢ":"z",
})

def _norm_name(name: str) -> str:
    import unicodedata
    value = unicodedata.normalize("NFKC", name or "").translate(_SMALLCAPS).casefold().replace("ё", "е")
    value = re.sub(r"[^a-zа-яіїєґ0-9]+", " ", value)
    return " ".join(value.split())

def is_hq(name: str, user_id: int | None = None) -> bool:
    if user_id is not None and user_id in HQ_USER_IDS:
        return True
    low = _norm_name(name)
    return any(_norm_name(k) in low for k in HQ_MARKERS)

def is_boss(name: str, user_id: int | None = None) -> bool:
    if user_id is not None and user_id in BOSS_USER_IDS:
        return True
    low = _norm_name(name)
    return any(_norm_name(k) in low for k in BOSS_MARKERS)

def is_manager(name: str, user_id: int | None = None) -> bool:
    return is_hq(name, user_id) or is_boss(name, user_id)

def is_bot_name(name: str) -> bool:
    norm = _norm_name(name)
    if not norm:
        return False
    if RT.bot_name_norm and norm == RT.bot_name_norm:
        return True
    return any(_norm_name(k) == norm for k in BOT_NAME_MARKERS)

def store_name(name: str, user_id: int | None = None, report_text: str = "") -> str:
    if user_id is not None and user_id in STORE_BY_USER_ID:
        return STORE_BY_USER_ID[user_id]
    original = (name or "").strip()
    norm = _norm_name(original)
    if not norm:
        return original or "Неизвестная точка"
    alias_norm = {_norm_name(k): v for k, v in ALIASES.items()}
    if norm in alias_norm:
        return alias_norm[norm]
    head = _norm_name("\n".join((report_text or "").splitlines()[:2])[:120])
    text_markers = [
        ("осипова 25", "Осипова 25"), ("осипова", "Осипова 25"),
        ("черноморск", "Черноморск"),
        ("старосенная", "Старосенная Киевстар"),
        ("преображенська 52", "Преображенська 52"), ("преображенская 52", "Преображенська 52"),
        ("добровольского 137", "Добровольского 137"),
        ("пантелеймоновская 25", "Пантелеймоновская 25"),
        ("белгород днестровский", "Белгород-Днестровский"),
        ("глушко 14", "Глушко 14"),
        ("королева киевстар", "Королёва Киевстар"),
        ("королева водафон", "Королёва Водафон"),
        ("королева лайф", "Королёва лайф"),
        ("киевстар арнаутская", "Киевстар арнаутская"),
        ("измаил киевстар", "Измаил Киевстар"),
        ("измаил водафон", "Измаил Водафон"),
        ("измаил вф", "Измаил Водафон"),
        ("рабина водафон", "Рабина Водафон"),
        ("одесса рабина", "Одесса Рабина"),
    ]
    for marker, canonical in sorted(text_markers, key=lambda x: len(x[0]), reverse=True):
        if marker in head:
            return canonical
    for candidate in sorted(STORE_NAMES, key=lambda s: len(_norm_name(s)), reverse=True):
        cn = _norm_name(candidate)
        if cn and (cn in norm or norm in cn):
            return candidate
    return original or "Неизвестная точка"

# ══════════════════════════════════════════════════════════════
# ИСТОРИЯ / СТАТИСТИКА
# ══════════════════════════════════════════════════════════════
def record_report(store: str, window: str, dt: datetime, rep: dict) -> bool:
    if window not in ("morning", "midday", "evening"):
        return False
    bd = biz_date(dt).isoformat()
    day = STATE["history"].setdefault(bd, {})
    if store not in STORE_NAMES:
        unk = day.setdefault("__unknown__", {})
        store_data = unk.setdefault(store, {})
        store_data[window] = {
            "plan": rep["plan"], "fact": rep["fact"], "closed": rep["closed"],
            "pct": rep["pct"], "t": dt.strftime("%H:%M"),
        }
        return True
    store_data = day.setdefault(store, {})
    old = store_data.get(window)
    if old and old.get("t"):
        try:
            old_m = int(old["t"].split(":")[0]) * 60 + int(old["t"].split(":")[1])
            new_m = dt.hour * 60 + dt.minute
            if old_m > new_m:
                return False
        except (TypeError, ValueError, IndexError):
            pass
    store_data[window] = {
        "plan": rep["plan"], "fact": rep["fact"], "closed": rep["closed"],
        "pct": rep["pct"], "t": dt.strftime("%H:%M"),
    }
    return True

def day_entries(bd: str, window: str) -> dict:
    return {
        s: w[window]
        for s, w in STATE["history"].get(bd, {}).items()
        if s != "__unknown__" and window in w
    }

def count_reports_today(bd: str, window: str) -> tuple[int, int]:
    day_data = STATE["history"].get(bd, {})
    known = sum(1 for s, sd in day_data.items() if s != "__unknown__" and window in sd)
    unk   = sum(1 for sd in day_data.get("__unknown__", {}).values() if window in sd)
    return known, unk

def pct_str(p) -> str:
    return f"{p:.0f}%"

def evening_stats(dates: list) -> dict:
    res: dict = {}
    for d in dates:
        for s, e in day_entries(d, "evening").items():
            if e.get("pct") is not None:
                res.setdefault(s, []).append((d, e["pct"]))
    return res

# ══════════════════════════════════════════════════════════════
# БАНК ФРАЗcounts
# ══════════════════════════════════════════════════════════════
JOKES = {
    "general": [
        "🧮 Продавец — это когда ты гуманитарий, но рассрочку на 10 платежей с учётом допов считаешь в уме быстрее калькулятора.",
        "🤝 Тот самый момент, когда закрыл сложного клиента на флагман со всеми допами. Ощущение, будто где-то на фоне одобрительно кивнул Денис Гусев.",
        "🎯 Покупатель уверен, что 13-й айфон с разбитым в крошку экраном по трейд-ину покроет 90% стоимости нового. Просто киваем и мягко возвращаем его в реальность.",
        "📱 Закон подлости: твой личный телефон разрывается от звонков ровно в тот момент, когда ты формируешь чек на 50к.",
        "🛍 Идеальная продажа — это когда клиент уходит с ощущением, что он сам всё решил, а ты просто вовремя пробивал чек.",
        "🧠 Когда 1С висит на оплате терминалом уже третью минуту, а клиент смотрит тебе прямо в душу. В этот момент важно уметь притвориться NPC.",
        "🪄 Мы не настраиваем телефоны, мы проводим экзорцизм вирусов и рекламы. Прайс на изгнание демонов соответствующий.",
        "💀 Супервайзер Киевстара пришёл проверить операторский план. В этот момент клиент попросил кредит, второй — домашний интернет, третий — пароль от Wi-Fi. Администратор открыл третий глаз и начал искать стажёра.",
        "📚 Стажёр выучил характеристики всех смартфонов, условия страховок, правила кредитования и процедуру возврата. На следующий день ему сказали, что сегодня он наставник.",
        "🌀 На Королёва Киевстар однажды открыли дверь в подсобку, а там оказался Белгород-Днестровский. Артамонов сказал, что это обычное перемещение.",
        "📡 Супервайзеры Киевстара, Водафона и Лайфа одновременно пришли на одну точку. Продавец оформил три операторских плана, два смартфона и случайно открыл четвёртого оператора — себя.",
        "🪑 АХО приехали чинить стул на Глушко. Выяснилось, что стул требует согласования с регионалом. Стул оформил увольнение.",
        "📲 «А зарядка в комплекте?» — вопрос, после которого хочется её туда убрать.",
        "😎 Не каждый герой носит плащ, но некоторые умеют перенести контакты за 4000.",
        "🥊 Гусев может дожать на рассрочку с допами даже неработающий банкомат.",
        "⚡ Читкод: если Артамонов не отвечает 20 минут, напишите, что Боляк пришёл проверять остатки. Перемещение прилетит со скоростью света.",
        "🤡 Закон подлости: если перемещение от Артамонова прошло мгновенно — не радуйтесь. Значит, сейчас зависнет 1С.",
        "💡 Совет дня: если Роман Анатольевич зашёл на точку, просто начинайте продавать витрину. Целиком. Вместе со стеллажами.",
        "💡 Совет дня: разбивайте сумму на платежи так виртуозно, чтобы клиент думал, будто это вы ему доплачиваете.",
        "🧊 Клиент сказал «я подумаю» настолько убедительно, что даже витрина перестала надеяться.",
        "👀 Когда клиент говорит у терминала «что тут нажимать?» — вся команда инстинктивно делает вид, что занята.",
        "📦 На складе всё есть, кроме именно того товара, который нужен прямо сейчас. Классика жанра.",
        "📈 Не всё решается одним большим чеком. Иногда день собирается из маленьких удачных карасиков.",
        "🧾 Провёл чек в кредит с первым взносом по карте, добавил трейд-ин, всё с первого раза — в магазине на секунду стало неловко, потому что никто не знает, как на такое реагировать.",
    ]
}

# 1С — переписано в зумерском стиле без «она моя любимая»
TECH_REPLIES = [
    "1С опять слетела. Ну что, коллеги, не в первый и не в последний раз — набирайте айти 🙃",
    "1С решила взять незапланированный выходной прямо посреди рабочего дня. Айти уже в курсе? 📵",
    "1С снова в оффлайне. Она просто так живёт. Пишите в айти — они привыкли 🗿",
    "Очередной сбой 1С — добро пожаловать в нашу реальность. Айти зовите, они знают что делать 🤙",
    "1С зависла? No surprise. Это уже традиция, как утренний кофе. Сообщите айти ☕",
    "1С ушла думать. Долго. Помогите ей — позвоните в айти 🧘‍♂️",
    "Система 1С временно находится в режиме «мне всё равно». Айти поднимут 🔌",
    "1С опять чудит. Просто наберите айти, они уже знают наизусть этот скрипт 📞",
    "Сбой 1С — это не баг, это фича. Стабильная такая фича. Айти, your turn 🤡",
    "1С снова пропала без объяснений. Айти — вы там? Ваш выход 👋",
    "Классика жанра: 1С висит, план идёт, айти спасают ситуацию. Звоните им 🚑",
    "1С сегодня решила поработать на пол-ставки. Айти в курсе, но можно напомнить 🛠️",
    "Ну что, 1С опять приколист. Айти — принимайте! 🎭",
    "1С зависла ровно в самый неподходящий момент. Случайность? Едва ли. Айти, помогите 🕵️",
    "1С и стабильность — это два разных понятия. Сообщите айти, пусть разберутся 🧩",
    "Очередная встреча с багами 1С. Айти уже набрали? Если нет — самое время 📲",
]

ZOOMER_PHRASES = [
    "Окей, эту кринжатину удаляю 🤡",
    "Шутка не зашла — сношу. Сорян 🚮",
    "Собрала больше отказов, чем кредит в таргете. Стёр 📉",
    "Минус вайб поймал, делитнул от греха 📉",
    "Шутка оказалась с браком — оформляю возврат ♻️",
    "Скипаем этот кринж. Я просто тестил вашу бдительность 👀",
    "Вайб-чек шутки не пройден. Отмена операции 🚫",
    "Снёс этот кринж, забыли 🙄",
    "Кажется, перегрелся с юмором. Делитнул, делаем вид, что этого не было 🫠",
    "Ладно-ладно, душнить не надо. Убрал 🙄",
    "Not it 💅 удалено",
    "Ок ок, понял. Это был краш-тест юмора. Провалился 🪦",
]

REMINDER_FAIL_PHRASES = [
    "Кажется, мои алгоритмы подсчёта дали сбой. Ухожу на диагностику 🤐",
    "Ой, вижу ошибку в матрице отчётов. Вызываю фиксиков и беру паузу 🛠",
    "Счётчик сломался от ваших мощных продаж. Перезагружаюсь 🔌",
    "Системный сбой распознавания! Отключаю напоминания, пока IT не проверят логи 🖥",
    "Ушёл пересчитывать базу и медитировать 🧘‍♂️",
    "Мои датчики зафиксировали возмущение. Приостанавливаю пуши 🚨",
    "Сбой синхронизации с 1С. Убежал к серверу 🏃‍♂️",
    "Внутренний калькулятор перегрелся. Умолкаю 🔇",
    "Вижу конфликт данных. Ухожу в безопасный режим 💤",
    "Где-то коротнуло в модуле отчётов. Жду техподдержку 📞",
]

SITUATIONS = {
    "morning": [
        "☀️ Доброе утро, ОФ2! Всем отличного настроения и продуктивного дня!",
        "☕ Бодрого утра, ОФ2! Желаю лёгкой смены и высоких продаж! 💸",
        "🚀 Всем привет! Успешного дня и побольше довольных клиентов!",
        "✨ Доброе утро, ОФ2! Пусть сегодняшний день принесёт отличные результаты!",
        "🎯 Отличного старта! Всем выполнения планов и крутых чеков!",
        "☀️ Доброе утро! Желаю энергии на весь день и классных продаж! 🛒",
        "💼 Всем доброго утра и позитивной смены!",
        "☕ Приветствуем, ОФ2! Пусть день пройдёт легко и результативно! 📈",
        "🌟 Доброе утро, команда! Желаю отличных продаж и лояльных покупателей!",
        "⚡ Бодрого начала дня! Всем удачи и стабильно высоких чеков! 💳",
        "☀️ Доброе утро, ОФ2! Желаю спокойной и продуктивной смены! 🤝",
        "🎯 Всем привет! Пусть планы закрываются легко и с удовольствием!",
        "🚀 Доброе утро, ОФ2! Настраиваемся на позитив и мощные продажи!",
        "✨ Успешной смены, коллеги! Пусть день будет богат на хорошие чеки! 💰",
        "☕ Доброе утро! Всем отличных результатов и благодарных клиентов!",
        "☀️ Доброе утро, ОФ2! Удачной смены, сильных чеков и спокойной 1С. Поехали 🚀",
        "🌟 ОФ2, на связи! Пусть сегодняшний день пройдёт без возвратов и токсиков.",
    ],
    # Руководителям — сбой и уход на диагностику (замолкает на 30 мин)
    "bot_mention_manager": [
        "Упс, кажется, мои нейроны перегрелись. Ушёл к IT-отделу на диагностику, скоро вернусь! 🔧",
        "Системная ошибка 404: модуль ответов руководству временно недоступен. Связываюсь с сисадмином. 🛠️",
        "Принял запрос! Но процессор требует охлаждения. Ушёл на внеплановое ТО. ⚙️",
        "Где-то коротнуло... Вызываю IT-помощь. Ухожу в режим тишины. 🔌",
        "Алло, IT? Тут меня начальство тегает, а у меня скрипты шалят. Беру паузу! 🔄",
        "Кажется, поймал баг лояльности. Ушёл к разработчикам на фикс. 👨‍💻",
        "Ваше сообщение слишком важное, а я всего лишь бот. Ушёл советоваться с сервером. ⏳",
        "Мои шестерёнки закрутились не в ту сторону. Звоню айтишникам, я на ремонте! 🤖",
        "Синий экран смерти в модуле ответов. Ушёл на срочную перезагрузку. 🖥️",
        "Запрос получен, но база данных пошла пить кофе. Я с ней, скоро будем! ☕",
        "Рад бы ответить развернуто, но сервер сказал «хватит болтать». Ставлю беззвучный режим. 🔇",
    ],
    # Магазинам — лояльно, напоминание о продажах
    "bot_mention_store": [
        "Принял! Но текстовый модуль занят подсчётом чеков. Не отвлекаемся, клиенты ждут! 🎯",
        "Я always на связи. Но лучше бы ты так активно предлагал страховки! 🛡️",
        "Слышу вас! Рад бы поболтать, но давайте лучше по клиентам — идём работать! 💼",
        "Контакт есть! А теперь идём конвертировать этот контакт в продажу! 🤝",
        "Вижу твоё сообщение, а вот нового чека в 1С пока нет. Исправляем? 👀",
        "Бот на месте, всё фиксирует. Желаю побольше клиентов с хорошим настроением! ☀️",
        "Я literally машина для сбора отчётов. Жду крутых цифр! 📊",
        "Связь установлена. Но давай лучше установим защиту на экран клиенту! 📱",
        "Я тут purely for vibes и фиксации ваших побед. Погнали делать кассу! 💸",
        "Окей, летс гоу! Но только после того, как закроете кредитную заявку. 💳",
        "Тегаете бота? Лучше тегните клиента на покупку аксессуаров! 🎧",
        "Поговорим, когда скинешь чек с флагманом и рассрочкой. Жду. ⏳",
        "Всегда рад сообщению! Но давайте лучше обсудим вашу следующую продажу. 💬",
        "Привет-привет! База данных готова к вечерним отчётам. Не подведите! 📝",
        "Магазин принял. Бот подтвердил. Идём работать дальше! 🫡",
        "Мой алгоритм подсказывает, что кому-то пора проверить остатки на витрине. 📦",
        "Ценю твой актив в чате! Главное, чтобы и в 1С такой же был. 📈",
    ],
    # Обычные сотрудники (не магазин, не руководитель) — дерзкие ответы
    "bot_mention_employee": [
        "Я бы ответил, но мы же оба знаем, что тебе лучше пойти клиенту страховку продать. 👀",
        "POV: у тебя ноль продаж, и ты решил докопаться до бота. Сорри, я занят. 💅",
        "Мог бы поболтать, но кто-то ещё не закрыл план. 🎯",
        "Пишешь боту, чтобы создать видимость бурной деятельности? Я понимаю, но отвечать мне запретили. 🥱",
        "Если бы ты так же активно продавал допы, как пишешь мне, мы бы уже выполнили план на год. 🤡",
        "Бро, я не твой личный психолог. Иди закрой кого-то на ультру, полегчает. 💅",
        "О, снова ты. Клиенты закончились или просто план сам себя выполнил? (Спойлер: нет). 🗿",
        "Ты тегаешь бота, когда в зале люди без чехлов ходят. Расставляй приоритеты. 👀",
        "Могу скинуть гайд «Как перестать писать боту и начать продавать», надо? 🤡",
        "POV: ты прокрастинируешь вместо того, чтобы предложить клиенту настройки. Я всё вижу. 👁️",
        "За каждый тег бота где-то грустит один непредложенный чехол. 📱",
        "Ты молодец, честно. Но если закроешь ещё одну рассрочку — будешь вообще легендой! 🏆",
        "Такое ощущение, что я работаю больше тебя. А я даже не получаю ЗП. 💸",
        "Мои сервера находятся слишком далеко, чтобы терпеть твою прокрастинацию. 🌍",
        "Бот загружен на 10%, а ты? Включай режим турбо! 🚀",
        "Ты написал боту. Бот прочитал. Мир не изменился. Иди продай страховку! 🛡️",
    ],
    "farewell": [
        "🤗 Спасибо за всё! Пусть на новом месте будет такой же классный вайб.",
        "🫡 Было круто работать вместе. Успехов на новом пути!",
        "🌟 Спасибо за вклад в команду. Пусть дальше всё сложится идеально!",
        "🤝 Огромный лайк за работу и отношение. Удачи во всех делах!",
        "💛 Спасибо, что были с нами. Всего самого лучшего!",
        "🍀 Новый этап — новые возможности. Спасибо за всё!",
        "🙏 Спасибо за совместные смены. Пусть всё получится!",
        "👏 Команда говорит спасибо. Успехов на новом месте!",
        "☀️ Спасибо за работу. Двигайтесь только вперёд!",
    ],
    "month_start": [
        "🗓 Новый месяц — обнуляем статистику и летим за новым планом. Погнали!",
        "🚀 Первое число: чистый лист и свежий таргет. Стартуем!",
        "💪 Месяц начался: каждый чек имеет значение. Вперёд!",
        "🔥 Перезапуск системы продаж. Новый месяц — новые рекорды!",
        "🎯 Первое число — идеальное время задать темп. Погнали!",
        "😎 План обновился, амбиции остались. Начинаем сильно!",
        "🌟 Новые клиенты, новые чеки, новый профит. Вперёд!",
    ],
    "newyear": [
        "🎄 Новогодний вайб включён! Готовимся продавать подарки.",
        "🎁 Праздничный ажиотаж — лучшее время для кросс-сейла. Погнали!",
        "✨ Пусть каждый клиент сегодня будет как Дед Мороз — с деньгами и без лишних вопросов!",
        "🥂 Праздник праздником, а чеки по расписанию. Удачной смены!",
        "🎉 Все за подарками — а мы тут как тут. Вперёд!",
        "❄️ Уютного дня, лояльных клиентов и топовых чеков!",
    ],
    "women": [
        "🌷 С 8 Марта! Пусть настроение будет весенним, а клиенты — максимально приятными.",
        "💐 Девушки, с праздником! Вы — душа и двигатель этого филиала!",
        "🌸 С 8 Марта! Пусть сегодня всё получается легко и с улыбкой.",
        "🎀 Желаем весеннего вайба, комплиментов и адекватных покупателей!",
        "✨ С праздником весны! Вы делаете рабочие будни намного ярче.",
        "💛 С 8 Марта! Спасибо за ваш труд, терпение и красоту.",
        "🌼 Желаем сегодня только позитива и никаких проблем с 1С!",
    ],
}

# ══════════════════════════════════════════════════════════════
# ТРИГГЕРЫ
# ══════════════════════════════════════════════════════════════
TECH_RE = re.compile(
    r"(?<!\w)1[сc](?!\w)|сбой|збій|не\s?работает|не\s?працює|нет\s+связи|нема\s+зв.язку|техн\w+\s+проблем",
    re.IGNORECASE,
)
ONE_C_RE       = re.compile(r"(?<!\w)1[сc](?!\w)", re.IGNORECASE)
TECH_FIXED_RE  = re.compile(r"заработал|запрацював|уже\s+работает|вже\s+працює|всё\s+ок|все\s+ок", re.IGNORECASE)
FAREWELL_RE    = re.compile(r"последн\w+\s+рабоч|остан\w+\s+робоч", re.IGNORECASE)
GREET_RE       = re.compile(r"доброе утро|доброго ранку|доброго дня|добрый день|добрий день|добрий ранок", re.IGNORECASE)
BDAY_RE        = re.compile(r"с\s+дн[её]м\s+рожд|з\s+дн[её]м\s+народж", re.IGNORECASE)

def jokes_temporarily_paused() -> bool:
    return time.time() < RT.jokes_paused_until

def register_manager_activity(ts: float) -> None:
    RT.last_manager_message = max(RT.last_manager_message, ts)
    RT.jokes_paused_until   = max(RT.jokes_paused_until, ts + JOKE_AFTER_MANAGER_MIN * 60)

# ══════════════════════════════════════════════════════════════
# ОТПРАВКА И РЕАКЦИИ
# ══════════════════════════════════════════════════════════════
def can_speak(n: datetime, gap: bool = True, jokes: bool = False, ignore_mute: bool = False) -> bool:
    if not in_speak_hours(n):
        return False
    t = time.time()
    if not ignore_mute and t < RT.mute_until:
        return False
    return True

async def say(
    text: str,
    msg: Message | None = None,
    html: bool = False,
    track_deletable: bool = False,
    msg_type: str = "joke",
) -> bool:
    mode = get_bot_mode()
    if mode in ("stop", "react_only"):
        return False
    kwargs = {"parse_mode": "HTML"} if html else {}
    if msg is not None and msg.chat.type in ("group", "supergroup"):
        if is_after_hard_mute() or mode == "silent":
            return False
    try:
        if msg is not None:
            sent = await msg.reply(text, **kwargs)
            RT.last_bot = time.time()
            if sent.chat.type in ("group", "supergroup"):
                await mirror_to_admin(sent, text)
        else:
            cid = get_chat_id()
            if cid is None:
                return False
            sent = await send_chat_message(cid, text, **kwargs)
            if sent is None:
                return False
        if track_deletable:
            RT.bot_msgs[sent.message_id] = {"neg": set(), "text": text, "type": msg_type}
            if len(RT.bot_msgs) > 50:
                del RT.bot_msgs[next(iter(RT.bot_msgs))]
    except TelegramRetryAfter as e:
        await asyncio.sleep(min(e.retry_after + 1, 30))
        return False
    except Exception as e:
        log.warning("Не удалось отправить сообщение: %s", e)
        return False
    RT.last_bot = time.time()
    return True

async def maybe_send_tech_reply(message: Message, n: datetime) -> bool:
    day_key = n.date().isoformat()
    if STATE.get("tech_reply_date", "") == day_key:
        return False
    sent = await say(random.choice(TECH_REPLIES), message, track_deletable=True, msg_type="tech")
    if sent:
        STATE["tech_reply_date"] = day_key
        save_state()
    return sent

async def mirror_to_admin(sent: Message, text: str | None = None) -> None:
    if sent.chat.type not in ("group", "supergroup"):
        return
    content = text if text is not None else (sent.text or sent.caption or "")
    content = unescape(re.sub(r"</?(?:b|i|code)>", "", content, flags=re.IGNORECASE))
    token = secrets.token_hex(4)
    pending = STATE.setdefault("review_pending", {})
    pending[token] = {
        "chat_id": sent.chat.id, "message_id": sent.message_id,
        "text": content[:4096], "created_at": time.time(),
    }
    while len(pending) > 200:
        pending.pop(next(iter(pending)))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Оставить", callback_data=f"rv:k:{token}"),
        InlineKeyboardButton(text="🗑 Удалить",  callback_data=f"rv:d:{token}"),
    ]])
    preview = content[:3300] + ("…\n[текст сокращён в копии]" if len(content) > 3300 else "")
    try:
        admin_msg = await bot.send_message(
            ADMIN_ID, f"📨 Бот отправил сообщение в рабочий чат:\n\n{preview}",
            reply_markup=keyboard,
        )
        pending[token]["admin_message_id"] = admin_msg.message_id
        save_state()
    except Exception as e:
        pending.pop(token, None)
        log.warning("Не удалось продублировать сообщение админу: %s", e)

async def send_chat_message(chat_id: int, text: str, **kwargs) -> Message | None:
    if is_allowed_chat(chat_id) and is_after_hard_mute():
        return None
    if is_allowed_chat(chat_id) and get_bot_mode() in ("react_only", "silent", "stop"):
        return None
    try:
        sent = await bot.send_message(chat_id, text, **kwargs)
    except TelegramRetryAfter as e:
        log.warning("Telegram ограничил отправку: %s", e)
        return None
    except Exception as e:
        log.warning("Не удалось отправить сообщение в чат: %s", e)
        return None
    RT.last_bot = time.time()
    await mirror_to_admin(sent, text)
    return sent

async def reply_and_mirror(message: Message, text: str, **kwargs) -> Message | None:
    if message.chat.type in ("group", "supergroup"):
        if is_after_hard_mute() or get_bot_mode() in ("react_only", "silent", "stop"):
            return None
    sent = await message.reply(text, **kwargs)
    RT.last_bot = time.time()
    if sent.chat.type in ("group", "supergroup"):
        await mirror_to_admin(sent, text)
    return sent

async def react(msg: Message, pool: list) -> bool:
    if get_bot_mode() == "stop":
        return False
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

HOLIDAYS = {(1, 1): "newyear", (12, 31): "newyear", (3, 8): "women"}

def greeting_text(n: datetime) -> str:
    cat = HOLIDAYS.get((n.month, n.day))
    if not cat and n.day == 1:
        cat = "month_start"
    if cat:
        return pick_bag(cat, SITUATIONS[cat])
    return pick_bag("morning", SITUATIONS["morning"])

# ══════════════════════════════════════════════════════════════
# ШУТКИ БЕЗ ПОВТОРОВ
# ══════════════════════════════════════════════════════════════
JOKES_USED_MAX  = 400
JOKE_SIMILARITY = 0.85
JOKE_MIN_LEN    = 15

def get_full_joke_bank() -> list:
    return JOKES["general"] + STATE.get("custom_jokes", [])

def _joke_norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", (text or "").lower().replace("ё", "е")).strip()

def _joke_key(norm: str) -> str:
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]

def _find_matches(norm: str, others: list, first_only: bool = False) -> list:
    found = []
    if not norm:
        return found
    sm = SequenceMatcher(None, autojunk=False)
    sm.set_seq2(norm)
    for i, o in enumerate(others):
        if not o:
            continue
        hit = o == norm
        if not hit:
            short, long_ = (o, norm) if len(o) <= len(norm) else (norm, o)
            hit = len(short) >= 25 and short in long_
        if not hit:
            sm.set_seq1(o)
            hit = (sm.real_quick_ratio() >= JOKE_SIMILARITY
                   and sm.quick_ratio() >= JOKE_SIMILARITY
                   and sm.ratio() >= JOKE_SIMILARITY)
        if hit:
            found.append(i)
            if first_only:
                break
    return found

def _jokes_used() -> list:
    used = STATE.get("jokes_used")
    if not isinstance(used, list):
        used = STATE["jokes_used"] = []
    return used

def mark_joke_used(text: str, ts: float | None = None) -> None:
    norm = _joke_norm(text)
    if len(norm) < JOKE_MIN_LEN:
        return
    ts = ts or time.time()
    key = _joke_key(norm)
    used = _jokes_used()
    for e in used:
        if e.get("k") == key:
            e["ts"] = max(e.get("ts", 0), ts)
            break
    else:
        used.append({"k": key, "n": norm[:300], "t": text[:300], "ts": ts, "likes": 0})
    used.sort(key=lambda e: e.get("ts", 0))
    del used[:-JOKES_USED_MAX]

def sync_jokes_from_history(items: list) -> int:
    msgs = [(dt.timestamp(), _joke_norm(t)) for dt, t in items]
    msgs = [(ts, nt) for ts, nt in msgs if len(nt) >= JOKE_MIN_LEN]
    if not msgs:
        return 0
    norms = [nt for _, nt in msgs]
    found = 0
    for joke in get_full_joke_bank():
        idx = _find_matches(_joke_norm(joke), norms)
        if idx:
            mark_joke_used(joke, max(msgs[i][0] for i in idx))
            found += 1
    return found

def joke_stats() -> tuple:
    bank = get_full_joke_bank()
    used_norms = [e["n"] for e in _jokes_used() if e.get("n")]
    used = sum(1 for j in bank if _find_matches(_joke_norm(j), used_norms, True))
    return used, len(bank)

def joke_for(n: datetime) -> str:
    bank = get_full_joke_bank()
    used = _jokes_used()
    used_norms = [e["n"] for e in used if e.get("n")]
    fresh = [j for j in bank if not _find_matches(_joke_norm(j), used_norms, True)]
    if not fresh:
        keep = min(max(3, len(bank) // 5), len(bank) - 1)
        used[:] = sorted(used, key=lambda e: e.get("ts", 0))[-keep:]
        recent = [e["n"] for e in used if e.get("n")]
        fresh = [j for j in bank if not _find_matches(_joke_norm(j), recent, True)] or list(bank)
    return random.choice(fresh)

def queued_joke_for(n: datetime | None = None) -> str:
    current  = STATE.get("queued_joke")
    bank     = get_full_joke_bank()
    used_norms = [e["n"] for e in _jokes_used() if e.get("n")]
    if current and current in bank and not _find_matches(_joke_norm(current), used_norms, True):
        return current
    chosen = joke_for(n or now())
    STATE["queued_joke"] = chosen
    save_state()
    return chosen

def replace_queued_joke(n: datetime | None = None) -> str | None:
    current    = queued_joke_for(n)
    used_norms = [e["n"] for e in _jokes_used() if e.get("n")]
    bank       = get_full_joke_bank()
    fresh = [j for j in bank
             if not _find_matches(_joke_norm(j), used_norms, True)
             and _joke_norm(j) != _joke_norm(current)]
    if not fresh:
        return None
    STATE["queued_joke"] = random.choice(fresh)
    save_state()
    return STATE["queued_joke"]

# ══════════════════════════════════════════════════════════════
# РЕАКЦИИ НА ОТЧЁТЫ
# ══════════════════════════════════════════════════════════════
def report_reactions(window: str, pct, closed: bool, all_facts: list | None = None) -> list:
    """
    Утро   → ❤ всегда.
    Обед   → зависит от факта относительно других магазинов и % плана.
    Вечер  → только реакции (без логики напоминаний).
    all_facts — список фактов всех магазинов за это окно, для сравнения.
    """
    if window == "morning":
        return ["❤"]
    if pct is None:
        return ["🤝"]
    # Топ по факту относительно других
    high_relative = False
    if all_facts and len(all_facts) >= 3:
        threshold = sorted(all_facts, reverse=True)[len(all_facts) // 3]
        # узнаем факт текущего магазина через замыкание — передаётся снаружи
        high_relative = True  # флаг будет управляться снаружи
    if closed or pct >= 100:
        return ["🔥", "🤩", "🎉"]
    if pct >= 85:
        return ["🔥", "👍"]
    if pct >= 60:
        return ["🤝", "👍"] if high_relative else ["🤝"]
    return ["😔"]

def _midday_reactions(pct, closed: bool, fact, all_facts: list) -> list:
    """Обеденные реакции с учётом факта среди всех магазинов."""
    if pct is None and not closed:
        return ["🤝"]
    if closed or (pct is not None and pct >= 100):
        return ["🔥", "🤩", "🎉"]
    # Высокий факт относительно остальных — позитив
    if fact is not None and all_facts:
        top_third = sorted(all_facts, reverse=True)[:max(1, len(all_facts) // 3)]
        if fact in top_third or fact >= top_third[-1]:
            if pct is not None and pct >= 50:
                return ["🔥", "👍"]
    if pct is not None:
        if pct >= 85:
            return ["🔥", "👍"]
        if pct >= 60:
            return ["🤝"]
    return ["😔"]

def maybe_reset_day_counters(n: datetime) -> None:
    d = n.date().isoformat()
    if getattr(RT, "current_day", "") != d:
        RT.current_day = d
        RT.greeted = set()

def mute_for(minutes: float) -> None:
    RT.mute_until = max(RT.mute_until, time.time() + minutes * 60)

async def handle_report(message: Message, store: str, window: str, rep: dict, dt: datetime) -> None:
    await asyncio.sleep(random.uniform(0.2, 0.8))
    n = now()
    if n.hour >= SPEAK_UNTIL[0] and not REACT_AFTER_HOURS:
        return
    mode = get_bot_mode()
    if mode == "stop":
        return

    # Собираем факты всех магазинов за это окно для сравнения
    bd = biz_date(dt).isoformat()
    all_facts = [
        e["fact"] for e in day_entries(bd, window).values()
        if e.get("fact") is not None
    ]

    if window == "morning":
        pool = ["❤"]
    elif window == "midday":
        pool = _midday_reactions(rep["pct"], rep["closed"], rep.get("fact"), all_facts)
    else:  # evening
        pool = report_reactions(window, rep["pct"], rep["closed"])

    asyncio.create_task(react(message, pool))

    # Магические числа (только в active)
    if can_speak(n, gap=False) and mode == "active" and not RT.is_suspended:
        magic_triggered = False
        for val in (rep.get("plan"), rep.get("fact")):
            if not val:
                continue
            s_val = str(val)
            if s_val.endswith("777"):
                await say("Три топора! 777🎰", message, track_deletable=True)
                magic_triggered = True
                break
            elif s_val.endswith("00000"):
                await say("Ювелирная работа 💎", message, track_deletable=True)
                magic_triggered = True
                break
            elif "666666666" in s_val:
                await say("Вызываю экзорциста ✝️", message, track_deletable=True)
                magic_triggered = True
                break
        if magic_triggered:
            mute_for(30)

# ══════════════════════════════════════════════════════════════
# РЕАКЦИИ НА СООБЩЕНИЯ БОТА (негатив → удаление)
# ══════════════════════════════════════════════════════════════
@dp.message_reaction()
async def on_reaction(ev: MessageReactionUpdated):
    try:
        if get_bot_mode() == "stop" or not is_allowed_chat(ev.chat.id):
            return
        if ev.message_id not in RT.bot_msgs or not ev.user:
            return

        new_emojis = {r.emoji for r in ev.new_reaction if isinstance(r, ReactionTypeEmoji)}
        msg_data = RT.bot_msgs[ev.message_id]
        if isinstance(msg_data, set):
            msg_data = {"neg": msg_data, "text": "", "type": "joke"}
            RT.bot_msgs[ev.message_id] = msg_data
        m_type = msg_data.get("type", "joke")

        # Напоминания: любая негативная → сразу удаляем
        if m_type == "reminder":
            cancel_emojis = NEGATIVE | {"😲", "🤯", "👀"}
            if new_emojis & cancel_emojis:
                try:
                    await bot.edit_message_text(
                        "Сообщение удалено.", chat_id=ev.chat.id, message_id=ev.message_id
                    )
                except Exception:
                    pass
                del RT.bot_msgs[ev.message_id]
                await send_chat_message(ev.chat.id, random.choice(REMINDER_FAIL_PHRASES))
                mute_for(30)
                return
        else:
            # Обычные сообщения: 2 негативных → удаляем (требование: 2 или более)
            if new_emojis & NEGATIVE:
                msg_data["neg"].add(ev.user.id)
            else:
                msg_data["neg"].discard(ev.user.id)
            if len(msg_data["neg"]) >= 2:
                try:
                    await bot.edit_message_text(
                        "Сообщение удалено.", chat_id=ev.chat.id, message_id=ev.message_id
                    )
                except Exception:
                    pass
                del RT.bot_msgs[ev.message_id]
                if get_bot_mode() == "active":
                    await send_chat_message(ev.chat.id, random.choice(ZOOMER_PHRASES))
                return

        if new_emojis & POSITIVE:
            fans = STATE.setdefault("fans", {})
            uname = ev.user.full_name or "Неизвестный"
            fans[uname] = fans.get(uname, 0) + 1
            txt = msg_data.get("text", "")
            if txt and m_type == "joke":
                key = _joke_key(_joke_norm(txt))
                for j in _jokes_used():
                    if j.get("k") == key:
                        j["likes"] = j.get("likes", 0) + 1
                        break
            save_state()
    except Exception as e:
        log.warning("Ошибка в on_reaction: %s", e)

# ══════════════════════════════════════════════════════════════
# КОМАНДЫ В ГРУППЕ
# ══════════════════════════════════════════════════════════════
HELP_TEXT = (
    "🤖 Я бот чата TOP TEAM. Слежу за цифрами и поддерживаю вайб.\n"
    "/top  — рейтинг недели по вечерним отчётам\n"
    "/today — статистика за сегодняшний день\n"
    "/joke  — шутка по запросу\n"
    "Отчёты отмечаю реакциями."
)

async def handle_command(message: Message, cmd: str) -> None:
    mode = get_bot_mode()
    if mode in ("stop", "silent", "react_only"):
        return
    n = now()
    if not in_speak_hours(n):
        await react(message, ["🫡"])
        return
    today = biz_date(n)
    if cmd in ("/help", "/start"):
        await say(HELP_TEXT, message)
    elif cmd == "/joke":
        if mode != "active":
            await react(message, ["🤫"])
            return
        if time.time() - RT.last_bot > 120 and not jokes_temporarily_paused():
            joke = queued_joke_for(n)
            if await say(joke, message, track_deletable=True, msg_type="joke"):
                mark_joke_used(joke)
                STATE["queued_joke"] = None
                save_state()
    elif cmd == "/top":
        start = today - timedelta(days=today.weekday())
        dates = [(start + timedelta(days=i)).isoformat() for i in range((today - start).days + 1)]
        stats = evening_stats(dates)
        avg   = {s: mean(min(p, 300) for _, p in v) for s, v in stats.items()}
        if len(avg) < 3:
            await say("🏆 Рейтинг пока пуст — нужны вечерние отчёты за несколько дней.", message)
            return
        ranked = sorted(avg.items(), key=lambda x: -x[1])[:5]
        medals = ["🥇", "🥈", "🥉", "▪️", "▪️"]
        lines  = ["🏆 <b>Рейтинг недели (средний % плана):</b>"]
        lines += [f"{medals[i]} {escape(s)} — {pct_str(a)}" for i, (s, a) in enumerate(ranked)]
        await say("\n".join(lines), message, html=True)
    elif cmd == "/today":
        bd = today.isoformat()
        for window, title in (("evening", "вечер"), ("midday", "обед")):
            ent = day_entries(bd, window)
            ranked = sorted(
                ((s, e["pct"]) for s, e in ent.items() if e.get("pct") is not None),
                key=lambda x: -x[1]
            )
            if len(ranked) >= 3:
                top = ", ".join(f"{escape(s)} ({pct_str(p)})" for s, p in ranked[:3])
                await say(f"📈 Сегодня ({title}) впереди: {top}. Отчётов: {len(ent)}.", message, html=True)
                return
        await say("📈 Пока мало отчётов за сегодня — подожду остальных.", message)

# ══════════════════════════════════════════════════════════════
# РАСПИСАНИЕ
# ══════════════════════════════════════════════════════════════
SLOTS = [
    ("greet",   8, 30, "greeting",      False),
    ("nudge_m", 8, 55, "nudge_morning", False),
    ("nudge_d",13, 30, "nudge_midday",  False),
]

KIND_NAMES = {
    "greeting":      "Утреннее приветствие",
    "nudge_morning": "Напоминание (Утро)",
    "nudge_midday":  "Напоминание (Обед)",
}

def joke_window_end(start: datetime) -> datetime:
    return start + timedelta(minutes=JOKE_MAX_DELAY_MIN)

def mark_slot_done(done: list, slot_id: str) -> None:
    if slot_id not in done:
        done.append(slot_id)

def sync_done_from_bot_messages(n: datetime, bot_times: list) -> list:
    done = STATE["done"].setdefault(n.date().isoformat(), [])
    for t in sorted(bot_times):
        if t.date() != n.date():
            continue
        best = None
        for slot_id, h, m, kind, _ in SLOTS:
            if slot_id in done:
                continue
            start = t.replace(hour=h, minute=m, second=0, microsecond=0)
            limit = JOKE_MAX_DELAY_MIN if kind == "joke" else SLOT_GRACE_MIN
            if start <= t <= start + timedelta(minutes=limit):
                best = slot_id
        if best:
            mark_slot_done(done, best)
    return done

# ══════════════════════════════════════════════════════════════
# ИМПОРТ ИСТОРИИ
# ══════════════════════════════════════════════════════════════
def import_history_json(data: dict, n: datetime):
    parsed = 0
    latest = None
    latest_author = ("", False)
    unknown = set()
    bot_slot_times = []
    bot_all = []
    bot_jokes = []
    last_mgr = None

    messages = data.get("messages", [])
    for msg in messages:
        if msg.get("type") != "message":
            continue
        text_str = extract_text(msg.get("text", "")).strip()
        date_str = msg.get("date")
        try:
            if date_str:
                dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
                dt = dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
            elif msg.get("date_unixtime"):
                dt = datetime.fromtimestamp(int(msg["date_unixtime"]), TZ)
            else:
                continue
        except (ValueError, TypeError, OverflowError):
            continue

        sender  = msg.get("from", "")
        bot_msg = is_bot_name(sender)
        mgr_msg = (not bot_msg) and is_manager(sender)

        if latest is None or dt >= latest:
            latest = dt
            latest_author = (sender, mgr_msg)
        if mgr_msg and (last_mgr is None or dt >= last_mgr[0]):
            last_mgr = (dt, sender)
        if not text_str:
            continue
        if bot_msg:
            bot_all.append(dt)
            bot_jokes.append((dt, text_str))
            if not msg.get("reply_to_message_id"):
                bot_slot_times.append(dt)
            continue

        rep = parse_report(text_str)
        if not rep:
            continue
        raw_from_id = str(msg.get("from_id", ""))
        id_match    = re.search(r"(\d+)$", raw_from_id)
        sender_id   = int(id_match.group(1)) if id_match else None
        store = store_name(sender, sender_id, text_str)
        if store not in STORE_NAMES:
            unknown.add(sender)
        record_report(store, classify_window(dt), dt, rep)
        parsed += 1

    if latest is None and parsed == 0:
        return None

    if bot_all:
        RT.last_bot = max(RT.last_bot, max(bot_all).timestamp())
    if last_mgr:
        register_manager_activity(last_mgr[0].timestamp())

    sync_done_from_bot_messages(n, bot_slot_times)
    sync_jokes_from_history(bot_jokes)
    save_state()

    return {
        "parsed": parsed, "latest": latest, "author": latest_author,
        "last_mgr": last_mgr, "unknown": sorted(unknown), "jokes": joke_stats(),
    }

def build_plan(n: datetime) -> str:
    done  = STATE["done"].get(n.date().isoformat(), [])
    lines = []
    for slot_id, h, minute, kind, _ in SLOTS:
        if slot_id in done:
            continue
        start = n.replace(hour=h, minute=minute, second=0, microsecond=0)
        title = KIND_NAMES.get(kind, kind)
        if n > start + timedelta(minutes=SLOT_GRACE_MIN):
            continue
        lines.append(f"• {max(start, n):%H:%M} — {title}")
    return "\n".join(lines) if lines else "На сегодня больше плановых задач нет."

async def _grace_period_notify(n: datetime) -> None:
    await asyncio.sleep(120)
    bd    = biz_date(n).isoformat()
    lines = [f"✅ <b>Данные актуальны. Отчёты за {n.strftime('%d.%m')}:</b>"]
    all_ok = True
    for win, title in (("morning", "Утро"), ("midday", "Обед"), ("evening", "Вечер")):
        reported   = set(day_entries(bd, win))
        known, unk = count_reports_today(bd, win)
        total      = known + unk
        if total >= TOTAL_STORES:
            lines.append(f"<b>{title}:</b> ✅ все {total} отчётов получены")
            continue
        missing = sorted(set(STORE_NAMES) - reported)
        if missing:
            all_ok = False
            lines.append(f"<b>{title} ({total}/{TOTAL_STORES}):</b> не сдали: {', '.join(missing[:8])}")
        else:
            lines.append(f"<b>{title} ({total}/{TOTAL_STORES}):</b> ✅")
    if all_ok:
        lines.append("🎉 Все магазины сдали отчёты за все окна!")
    try:
        await bot.send_message(ADMIN_ID, "\n".join(lines), parse_mode="HTML",
                               reply_markup=admin_reports_keyboard())
    except Exception as e:
        log.warning("Не удалось отправить уведомление после grace period: %s", e)

def force_resume(n: datetime) -> None:
    done = STATE["done"].setdefault(n.date().isoformat(), [])
    for slot_id, h, minute, _, _ in SLOTS:
        if n.replace(hour=h, minute=minute, second=0, microsecond=0) <= n:
            mark_slot_done(done, slot_id)
    STATE["bot_mode"] = "active"
    save_state()
    RT.is_suspended   = False
    RT.awaiting_history = False
    RT.resumed_ts     = time.time()
    asyncio.create_task(_grace_period_notify(n))

# ══════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ И МЕНЮ ЛС
# ══════════════════════════════════════════════════════════════
def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Режимы работы",        callback_data="menu:modes")],
        [InlineKeyboardButton(text="🎭 Шутки / Отправить",    callback_data="menu:jokes")],
        [InlineKeyboardButton(text="📊 Отчёты и Пуш",         callback_data="menu:reports")],
        [InlineKeyboardButton(text="💾 Скачать state.json",   callback_data="admin:state")],
    ])

def admin_modes_keyboard() -> InlineKeyboardMarkup:
    mode = get_bot_mode()
    def lbl(key, text): return text + (" ✅" if mode == key else "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=lbl("active",     "🟢 Полная активность"),         callback_data="mode:active")],
        [InlineKeyboardButton(text=lbl("react_only", "👁 Только реакции"),             callback_data="mode:react_only")],
        [InlineKeyboardButton(text=lbl("silent",     "🔇 Реакции + напоминания"),      callback_data="mode:silent")],
        [InlineKeyboardButton(text=lbl("stop",       "🔴 Полный стоп"),                callback_data="mode:stop")],
        [InlineKeyboardButton(text="🔙 Главное меню",                                  callback_data="menu:main")],
    ])

def admin_jokes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👀 Шутка в очереди",              callback_data="panel:queue")],
        [InlineKeyboardButton(text="🔄 Заменить шутку",               callback_data="panel:change")],
        [InlineKeyboardButton(text="🚀 Отправить шутку сейчас в чат", callback_data="panel:force")],
        [InlineKeyboardButton(text="🔙 Главное меню",                  callback_data="menu:main")],
    ])

def admin_reports_keyboard() -> InlineKeyboardMarkup:
    in_grace = bool(RT.resumed_ts and time.time() - RT.resumed_ts < 120)
    if in_grace:
        secs_left = int(120 - (time.time() - RT.resumed_ts))
        debtor_buttons = [[InlineKeyboardButton(
            text=f"⏳ Данные загружаются... ({secs_left} сек)", callback_data="admin:grace_info"
        )]]
    else:
        debtor_buttons = [
            [InlineKeyboardButton(text="🎯 Точечный пуш должников в ЛС", callback_data="admin:push")],
            [InlineKeyboardButton(text="📋 Должники списком",             callback_data="admin:missing")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=[
        *debtor_buttons,
        [InlineKeyboardButton(text="📊 Статистика за сегодня", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🔙 Главное меню",          callback_data="menu:main")],
    ])

def _report_status_block(n: datetime) -> str:
    """Компактный блок статистики отчётов по всем окнам для панели создателя."""
    bd = biz_date(n).isoformat()
    day_data = STATE["history"].get(bd, {})
    all_unknown = day_data.get("__unknown__", {})

    WIN_META = [
        ("morning", "☀️ Утро",  "до 11:00"),
        ("midday",  "🌤 Обед",  "11:00–16:30"),
        ("evening", "🌙 Вечер", "после 16:30"),
    ]

    # Какие окна вообще имеют хоть что-то?
    active_windows = []
    for win, label, hint in WIN_META:
        known, unk = count_reports_today(bd, win)
        if known + unk > 0:
            active_windows.append((win, label, hint, known, unk))

    if not active_windows:
        return f"📭 <b>Отчётов за {n.strftime('%d.%m')} ещё нет</b>"

    lines = [f"📊 <b>Отчёты {n.strftime('%d.%m')} ({TOTAL_STORES} магазинов):</b>"]

    for win, label, hint, known, unk in active_windows:
        total = known + unk
        bar_filled = round(total / TOTAL_STORES * 8)
        bar = "█" * bar_filled + "░" * (8 - bar_filled)

        # Прогресс-строка
        lines.append(f"\n{label} <code>[{bar}]</code> <b>{total}/{TOTAL_STORES}</b>")

        # Магазины-молчуны
        reported_stores = set(day_entries(bd, win).keys())
        missing = sorted(set(STORE_NAMES) - reported_stores)

        # Строка: из магазинов / сторонних
        src_parts = [f"✅ магазинов: <b>{known}</b>"]
        if unk > 0:
            unk_names = [k for k, v in all_unknown.items() if win in v]
            unk_short = ", ".join(unk_names[:3]) + ("…" if len(unk_names) > 3 else "")
            src_parts.append(f"👤 сторонних: <b>{unk}</b> ({escape(unk_short)})")
        lines.append("   " + " · ".join(src_parts))

        # Не сдали (компактно)
        if missing:
            miss_short = ", ".join(missing[:5])
            if len(missing) > 5:
                miss_short += f" и ещё {len(missing) - 5}"
            lines.append(f"   ❌ не сдали: <i>{escape(miss_short)}</i>")
        else:
            lines.append("   🎉 все магазины сдали!")

    return "\n".join(lines)


def admin_panel_text(note: str | None = None) -> str:
    n       = now()
    planned = queued_joke_for(n)
    mode    = get_bot_mode()
    status_map = {
        "stop":       "🔴 ПОЛНЫЙ СТОП",
        "react_only": "👁 ТОЛЬКО РЕАКЦИИ (без сообщений, без шуток)",
        "silent":     "🔇 РЕАКЦИИ + НАПОМИНАНИЯ (без шуток, без ответов на теги)",
        "active":     "🟢 ПОЛНАЯ АКТИВНОСТЬ",
    }
    status = status_map.get(mode, mode)
    mute_status = (
        "Выключен" if RT.mute_until < time.time()
        else datetime.fromtimestamp(RT.mute_until, TZ).strftime("%H:%M:%S")
    )
    parts = [
        "👑 <b>Панель управления TOP TEAM</b>",
        f"Режим: <b>{status}</b>",
        f"Мут: <b>{mute_status}</b>",
        "",
        _report_status_block(n),
        "",
        "🎭 <b>Следующая шутка:</b>",
        escape(planned) if planned else "Нет",
        "",
        "<i>Категории ниже для настройки.</i>",
        "<i>/say [текст] — написать в чат от имени бота.</i>",
        "<i>/joke [текст] — отправить конкретную шутку в чат (только в ЛС).</i>",
    ]
    if note:
        parts = [f"💬 <i>{escape(note)}</i>", ""] + parts
    return "\n".join(parts)

async def send_fresh_menu(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.delete()
    except Exception:
        pass
    await bot.send_message(ADMIN_ID, text, parse_mode="HTML", reply_markup=markup)

# ══════════════════════════════════════════════════════════════
# ОБРАБОТКА ДОКУМЕНТОВ (ИСТОРИЯ ЧАТА)
# ══════════════════════════════════════════════════════════════
async def _process_history_file(message: Message, file_doc) -> None:
    await message.reply("⏳ Загружаю и анализирую файл истории чата...")
    try:
        file_in_io = BytesIO()
        await bot.download(file_doc, destination=file_in_io)
        file_in_io.seek(0)
        data = json.load(file_in_io)
        n    = now()
        res  = import_history_json(data, n)
        if not res:
            await message.reply("❌ Не смог распознать сообщения или история пуста.")
            return

        RT.awaiting_history = False
        author = res.get("author", ("?", False))
        lines  = [
            "✅ <b>История чата загружена!</b>",
            f"Найдено отчётов: {res['parsed']}.",
            f"Синхронизировано до: {res['latest'].strftime('%d.%m %H:%M') if res['latest'] else '?'}.",
            f"Последним писал: {escape(author[0])}{' (руководитель)' if author[1] else ''}.",
            f"🎭 Шутки бота: {res['jokes'][0]} из {res['jokes'][1]} уже были — повторять не буду.",
        ]
        if res["latest"] and res["latest"].date() < n.date():
            lines += [
                "",
                f"⚠️ <b>Внимание:</b> экспорт заканчивается {res['latest']:%d.%m %H:%M} — нет данных за сегодня ({n:%d.%m}).",
                "Отчёты за сегодня нужно переслать отдельно или загрузить свежий экспорт.",
            ]
        if res["latest"] and res["latest"].date() == n.date() and res["latest"].hour >= SPEAK_UNTIL[0]:
            lines += [
                "",
                f"🌙 Экспорт содержит данные после {SPEAK_UNTIL[0]}:00 — сегодняшние слоты будут пропущены.",
            ]
        lines += ["", "⏳ <i>После «Запустить» бот подождёт 2 мин для дообработки очереди.</i>"]
        if res["last_mgr"]:
            mdt, mname = res["last_mgr"]
            until = datetime.fromtimestamp(RT.jokes_paused_until, TZ)
            lines.append(
                f"Руководитель ({escape(mname)}) писал в {mdt.strftime('%d.%m %H:%M')}: "
                + (f"шутки не раньше {until.strftime('%H:%M')}." if until > n else "пауза уже прошла.")
            )
        if res["unknown"]:
            lines.append("⚠️ Не опознал точки (пропущены): " + escape(", ".join(res["unknown"])))
        lines += ["", "📋 <b>План на остаток дня:</b>", build_plan(n), ""]

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Запустить в работу", callback_data="hist:start")],
            [InlineKeyboardButton(text="⏸ Оставить на паузе",  callback_data="hist:pause")],
        ])
        await message.reply("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        log.exception("Ошибка обработки файла истории")
        await message.reply(f"❌ Ошибка обработки файла: {e}")

@dp.message(F.document)
async def handle_document(message: Message):
    if message.chat.type != "private" or not message.from_user or message.from_user.id != ADMIN_ID:
        return
    doc   = message.document
    fname = (doc.file_name or "") if doc else ""
    if fname.lower().endswith(".json"):
        await _process_history_file(message, doc)
    else:
        await message.reply("ℹ️ Отправь JSON-файл экспорта истории чата (из Telegram Desktop).")

# ══════════════════════════════════════════════════════════════
# ЛИЧНЫЕ СООБЩЕНИЯ (только создатель)
# ══════════════════════════════════════════════════════════════
async def handle_private_message(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    if user.id != ADMIN_ID:
        await message.reply(pick_bag("bot_mention_employee", SITUATIONS["bot_mention_employee"]))
        return

    text = (message.text or message.caption or "").strip()
    low  = text.lower()

    # Пересланный отчёт
    fwd_dt = None
    if message.forward_date:
        fwd_dt = message.forward_date.astimezone(TZ)
    elif getattr(message, "forward_origin", None) and hasattr(message.forward_origin, "date"):
        fwd_dt = message.forward_origin.date.astimezone(TZ)

    if text and (fwd_dt or parse_report(text)):
        rep = parse_report(text)
        if rep:
            dt       = fwd_dt or (message.date.astimezone(TZ) if message.date else now())
            fwd_from = getattr(message, "forward_from", None)
            fwd_user_id = fwd_from.id if fwd_from else None
            fwd_name    = getattr(message, "forward_sender_name", "") or (fwd_from.full_name if fwd_from else "")
            store    = store_name(fwd_name, fwd_user_id, text)
            win      = classify_window(dt)
            recorded = record_report(store, win, dt, rep)
            save_state()
            if recorded:
                await message.reply(
                    f"✅ Пересланный отчёт добавлен!\nМагазин: <b>{escape(store)}</b>\nОкно: <b>{win}</b> ({dt.strftime('%d.%m %H:%M')})",
                    parse_mode="HTML"
                )
            else:
                await message.reply(
                    f"ℹ️ Отчёт для {escape(store)} ({win}) за {dt.strftime('%d.%m %H:%M')} пропущен (уже есть).",
                    parse_mode="HTML"
                )
            return

    # /say — отправить произвольный текст в чат
    if low.startswith("/say "):
        cid = get_chat_id()
        if not cid:
            await message.reply("❌ ID рабочего чата не задан.")
            return
        if is_after_hard_mute():
            await message.reply("❌ Заблокировано: после 19:00 действует жёсткий мут.")
            return
        msg_to_send = text[5:].strip()
        if not msg_to_send:
            await message.reply("❌ Укажи текст после /say")
            return
        sent = await send_chat_message(cid, msg_to_send)
        await message.reply("✅ Отправлено в рабочий чат." if sent else "❌ Не удалось отправить.")
        return

    # /joke — отправить конкретную шутку или случайную из банка в чат
    if low.startswith("/joke"):
        cid = get_chat_id()
        if not cid:
            await message.reply("❌ ID рабочего чата не задан.")
            return
        if is_after_hard_mute():
            await message.reply("❌ После 19:00 жёсткий мут.")
            return
        if get_bot_mode() in ("stop", "react_only", "silent"):
            await message.reply("❌ Режим не позволяет отправить шутку.")
            return
        custom = text[6:].strip() if len(text) > 6 else ""
        joke_text = custom if custom else queued_joke_for(now())
        sent = await send_chat_message(cid, joke_text)
        if sent:
            if not custom:
                mark_joke_used(joke_text)
                STATE["queued_joke"] = None
                save_state()
            await message.reply("✅ Шутка отправлена в чат!")
        else:
            await message.reply("❌ Не удалось отправить.")
        return

    await bot.send_message(ADMIN_ID, admin_panel_text(), parse_mode="HTML", reply_markup=admin_main_keyboard())

# ══════════════════════════════════════════════════════════════
# ДИАГНОСТИКА
# ══════════════════════════════════════════════════════════════
async def diag(message: Message, cmd: str) -> None:
    n = now()
    if cmd == "/test":
        src = message.reply_to_message
        t   = ((src.text or src.caption or "") if src else "").strip()
        if not t:
            await reply_and_mirror(message, "Ответьте командой /test на сообщение с отчётом.")
            return
        rep = parse_report(t)
        if not rep:
            await reply_and_mirror(message, "❌ Не распознал это сообщение как отчёт.")
            return
        who    = src.from_user.full_name if src.from_user else "?"
        dt     = n if TEST_MODE else (src.date.astimezone(TZ) if src.date else n)
        window = classify_window(dt)
        pool   = report_reactions(window, rep["pct"], rep["closed"])
        pct    = "-" if rep["pct"] is None else f"{rep['pct']:.0f}%"
        await reply_and_mirror(
            message,
            f"✅ Отчёт распознан\nМагазин: {store_name(who)}\nОкно: {window} ({dt.strftime('%H:%M')})\n"
            f"План: {rep['plan']} | Факт: {rep['fact']} | Закрыт: {rep['closed']} | % = {pct}\n"
            f"Трейд-ин: {rep.get('trade')} | Страховки: {rep.get('ins')}\n"
            f"Реакция: {' '.join(pool)}"
        )
        return
    user = message.from_user
    work = is_allowed_chat(message.chat.id)
    await reply_and_mirror(
        message,
        f"🤖 Статус бота\nID чата: {message.chat.id}\n"
        f"Разрешён: {'да' if work else 'НЕТ'}\n"
        f"Режим: {get_bot_mode()}\n"
        f"Время: {n.strftime('%d.%m %H:%M')}\n"
        f"Писать текстом: {'да' if in_speak_hours(n) else 'нет (тихие часы)'}\n"
        f"TEST_MODE: {'включён' if TEST_MODE else 'выключен'}"
    )

# ══════════════════════════════════════════════════════════════
# CALLBACKS СОЗДАТЕЛЯ
# ══════════════════════════════════════════════════════════════
@dp.callback_query()
async def on_callback_query(callback: CallbackQuery) -> None:
    data = callback.data or ""
    if not callback.from_user or callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    n = now()
    try:
        # ─── Меню ───
        if data == "menu:main":
            await send_fresh_menu(callback, admin_panel_text(), admin_main_keyboard())
        elif data == "menu:modes":
            await send_fresh_menu(callback, admin_panel_text(), admin_modes_keyboard())
        elif data == "menu:jokes":
            await send_fresh_menu(callback, admin_panel_text(), admin_jokes_keyboard())
        elif data == "menu:reports":
            await send_fresh_menu(callback, admin_panel_text(), admin_reports_keyboard())

        # ─── Режимы ───
        elif data.startswith("mode:"):
            target_mode = data.split(":", 1)[1]
            labels = {
                "active":     "Полная активность",
                "react_only": "Только реакции",
                "silent":     "Реакции + напоминания",
                "stop":       "Полный стоп",
            }
            if target_mode in labels:
                STATE["bot_mode"] = target_mode
                RT.is_suspended   = (target_mode == "stop")
                if target_mode != "stop":
                    RT.resumed_ts = time.time()
                save_state()
                note = f"Режим переключён: «{labels[target_mode]}»"
                await send_fresh_menu(callback, admin_panel_text(note), admin_modes_keyboard())
                await callback.answer(labels[target_mode])
                return

        # ─── Запуск / Пауза после загрузки истории ───
        elif data == "hist:start":
            STATE["bot_mode"] = "active"
            force_resume(n)
            await callback.message.edit_reply_markup(reply_markup=None)
            await bot.send_message(ADMIN_ID, "🚀 <b>Бот запущен в работу.</b>",
                                   parse_mode="HTML", reply_markup=admin_main_keyboard())
            await callback.answer("Запущен")
            return
        elif data == "hist:pause":
            STATE["bot_mode"] = "stop"
            RT.is_suspended   = True
            save_state()
            await callback.message.edit_reply_markup(reply_markup=None)
            await bot.send_message(ADMIN_ID, "⏸ <b>Бот оставлен на паузе.</b>",
                                   parse_mode="HTML", reply_markup=admin_main_keyboard())
            await callback.answer("На паузе")
            return

        # ─── Шутки ───
        elif data == "panel:queue":
            await send_fresh_menu(callback, admin_panel_text("👀 Шутка в очереди:"), admin_jokes_keyboard())
        elif data == "panel:change":
            replacement = replace_queued_joke(n)
            if replacement is None:
                await callback.answer("Нет свободных шуток для замены.", show_alert=True)
                return
            await send_fresh_menu(callback, admin_panel_text("🔄 Шутка заменена:"), admin_jokes_keyboard())
            await callback.answer("Заменено")
            return
        elif data == "panel:force":
            if is_after_hard_mute():
                await callback.answer("❌ После 19:00 жёсткий мут.", show_alert=True)
                return
            if get_bot_mode() in ("stop", "react_only", "silent"):
                await callback.answer("❌ В текущем режиме отправка шуток заблокирована.", show_alert=True)
                return
            if jokes_temporarily_paused():
                until = datetime.fromtimestamp(RT.jokes_paused_until, TZ).strftime("%H:%M")
                await callback.answer(f"Пауза после руководителя до {until}.", show_alert=True)
                return
            cid = get_chat_id()
            if cid is None:
                await callback.answer("Не задан CHAT_ID.", show_alert=True)
                return
            joke = queued_joke_for(n)
            sent = await send_chat_message(cid, joke)
            if not sent:
                await callback.answer("Не удалось отправить шутку.", show_alert=True)
                return
            mark_joke_used(joke)
            STATE["queued_joke"] = None
            save_state()
            await send_fresh_menu(callback, admin_panel_text("✅ Шутка отправлена в чат!"), admin_jokes_keyboard())
            await callback.answer("Отправлено!")
            return

        # ─── Пуш должников ───
        elif data == "admin:push":
            bd  = biz_date(n).isoformat()
            win = classify_window(n)
            known, unk = count_reports_today(bd, win)
            if known + unk >= TOTAL_STORES:
                await callback.answer("Квота закрыта! Все отчёты получены.", show_alert=True)
                return
            reported = set(day_entries(bd, win))
            miss = sorted(set(STORE_NAMES) - reported)
            if not miss:
                await callback.answer("Все идентифицированные отчёты сданы!", show_alert=True)
                return
            kb = [[InlineKeyboardButton(text=m, callback_data=f"push:{m[:30]}")] for m in miss]
            kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu:reports")])
            await send_fresh_menu(callback, f"🎯 Пуш в ЛС (текущее окно: {win}):", InlineKeyboardMarkup(inline_keyboard=kb))
            await callback.answer()
            return
        elif data.startswith("push:"):
            push_store = data.split(":", 1)[1]
            target_id  = USER_ID_BY_STORE.get(push_store)
            if not target_id:
                await callback.answer("❌ ID магазина не найден.", show_alert=True)
                return
            try:
                await bot.send_message(target_id, "⏳ Напоминаю: время сдавать отчёт. Жду цифры в общем чате!")
                STATE.setdefault("pending_report_push", {})[target_id] = time.time()
                save_state()
                await send_fresh_menu(callback, f"✅ Пуш отправлен в ЛС: {escape(push_store)}!", admin_reports_keyboard())
                await callback.answer("Отправлено в ЛС")
            except Exception:
                await callback.answer("❌ Ошибка: бот заблокирован магазином.", show_alert=True)
            return

        # ─── Статистика ───
        elif data == "admin:stats":
            today      = biz_date(n)
            start_date = today - timedelta(days=today.weekday())
            week_dates = [(start_date + timedelta(days=i)).isoformat() for i in range((today - start_date).days + 1)]
            today_str  = today.isoformat()
            today_data = STATE["history"].get(today_str, {})

            today_valid, today_missed = {}, []
            for s in STORE_NAMES:
                best = (today_data.get(s) or {})
                best_rep = best.get("evening") or best.get("midday") or best.get("morning")
                if best_rep and best_rep.get("fact") is not None and best_rep.get("pct") is not None:
                    today_valid[s] = best_rep
                else:
                    today_missed.append(s)

            top_today_fact = sorted(today_valid.items(), key=lambda x: -x[1]["fact"])[:3]
            top_today_pct  = sorted(today_valid.items(), key=lambda x: -x[1]["pct"])[:3]
            weekly_stats   = {s: {"fact": 0, "plan": 0, "missed": 0} for s in STORE_NAMES}
            for d in week_dates:
                dd = STATE["history"].get(d, {})
                for s in STORE_NAMES:
                    best_rep = (dd.get(s) or {})
                    best_rep = best_rep.get("evening") or best_rep.get("midday") or best_rep.get("morning")
                    if best_rep and best_rep.get("fact") is not None:
                        weekly_stats[s]["fact"] += best_rep["fact"]
                        if best_rep.get("plan"):
                            weekly_stats[s]["plan"] += best_rep["plan"]
                    else:
                        weekly_stats[s]["missed"] += 1
            for s in STORE_NAMES:
                p = weekly_stats[s]["plan"]
                weekly_stats[s]["pct"] = (weekly_stats[s]["fact"] / p * 100) if p > 0 else 0

            top_week_fact = sorted([(s, v) for s, v in weekly_stats.items() if v["fact"] > 0], key=lambda x: -x[1]["fact"])[:3]
            top_week_pct  = sorted([(s, v) for s, v in weekly_stats.items() if v["pct"] > 0],  key=lambda x: -x[1]["pct"])[:3]
            week_missed   = [f"{s} ({v['missed']} шт.)" for s, v in weekly_stats.items() if v["missed"] > 0]
            medals = ["🥇", "🥈", "🥉"]
            lines  = [f"📊 <b>Статистика за сегодня ({today.strftime('%d.%m')}):</b>", "<b>По сумме:</b>"]
            for i, (s, rep) in enumerate(top_today_fact):
                lines.append(f"{medals[i]} {escape(s)} — {rep['fact']} грн ({pct_str(rep['pct'])})")
            if not top_today_fact:
                lines.append("<i>Нет данных</i>")
            lines.append("<b>По выполнению:</b>")
            for i, (s, rep) in enumerate(top_today_pct):
                lines.append(f"{medals[i]} {escape(s)} — {pct_str(rep['pct'])}")
            if not top_today_pct:
                lines.append("<i>Нет данных</i>")
            lines.append(
                f"⚠️ <i>Нет отчётов сегодня:</i> {escape(', '.join(today_missed))}"
                if today_missed else "✅ <i>Все сдали отчёты сегодня!</i>"
            )
            lines.extend(["", f"🏆 <b>Неделя (с {start_date.strftime('%d.%m')} по {today.strftime('%d.%m')}):</b>", "<b>По сумме:</b>"])
            for i, (s, v) in enumerate(top_week_fact):
                lines.append(f"{medals[i]} {escape(s)} — {v['fact']} грн ({pct_str(v['pct'])})")
            if not top_week_fact:
                lines.append("<i>Нет данных</i>")
            lines.append("<b>По выполнению:</b>")
            for i, (s, v) in enumerate(top_week_pct):
                lines.append(f"{medals[i]} {escape(s)} — {pct_str(v['pct'])}")
            if not top_week_pct:
                lines.append("<i>Нет данных</i>")
            if week_missed:
                lines.append(f"❌ <i>Пропуски за неделю:</i> {escape(', '.join(week_missed))}")
            report_text = "\n".join(lines)
            STATE["queued_stats"] = report_text
            save_state()
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📢 Опубликовать в общий чат", callback_data="pub:stats_complex")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="menu:reports")],
            ])
            await send_fresh_menu(callback, report_text, kb)
            await callback.answer()
            return

        elif data == "pub:stats_complex":
            if is_after_hard_mute() or get_bot_mode() in ("stop", "react_only", "silent"):
                await callback.answer("❌ Публикация заблокирована.", show_alert=True)
                return
            text_to_send = STATE.get("queued_stats")
            if not text_to_send:
                await callback.answer("❌ Нет сохранённого отчёта.", show_alert=True)
                return
            cid = get_chat_id()
            if not cid:
                await callback.answer("❌ Не задан ID чата.", show_alert=True)
                return
            sent = await send_chat_message(cid, text_to_send, parse_mode="HTML")
            if sent:
                await callback.answer("✅ Улетело в чат!")
                await send_fresh_menu(callback, admin_panel_text("✅ Опубликовано в рабочий чат!"), admin_reports_keyboard())
            else:
                await callback.answer("❌ Ошибка отправки.", show_alert=True)
            return

        elif data == "admin:missing":
            if RT.resumed_ts and time.time() - RT.resumed_ts < 120:
                secs = int(120 - (time.time() - RT.resumed_ts))
                await callback.answer(f"⏳ Подожди ещё {secs} сек.", show_alert=True)
                return
            bd    = biz_date(n).isoformat()
            lines = [f"📋 <b>Не сдали отчёты за {bd}:</b>"]
            for win, title in (("morning", "Утро"), ("midday", "Обед"), ("evening", "Вечер")):
                known, unk = count_reports_today(bd, win)
                total      = known + unk
                reported   = set(day_entries(bd, win))
                if total >= TOTAL_STORES:
                    lines.append(f"<b>{title} ({total}/{TOTAL_STORES}):</b> все сдали ✅")
                else:
                    missing = sorted(set(STORE_NAMES) - reported)
                    lines.append(
                        f"<b>{title} ({total}/{TOTAL_STORES}):</b> "
                        + (", ".join(missing) if missing else "все идентифицированные сдали")
                    )
            await send_fresh_menu(callback, "\n".join(lines), admin_reports_keyboard())
            await callback.answer()
            return

        elif data == "admin:grace_info":
            if RT.resumed_ts and time.time() - RT.resumed_ts < 120:
                secs = int(120 - (time.time() - RT.resumed_ts))
                await callback.answer(f"⏳ Бот дообрабатывает очередь. Осталось ~{secs} сек.", show_alert=True)
            else:
                await send_fresh_menu(callback, admin_panel_text("✅ Данные актуальны."), admin_reports_keyboard())
                await callback.answer("✅ Готово")
            return

        elif data == "admin:state":
            state_str = json.dumps(STATE, ensure_ascii=False, indent=2)
            file      = BufferedInputFile(state_str.encode("utf-8"), filename="state.json")
            await callback.message.reply_document(file, caption="Актуальный state.json")
            await callback.answer()
            return

        # ─── Модерация сообщений бота ───
        elif data.startswith("rv:"):
            _, action, token = (data.split(":", 2) + [""])[:3]
            pending_item = STATE.setdefault("review_pending", {}).get(token)
            if not pending_item:
                await callback.answer("Запись устарела.", show_alert=True)
                return
            original = pending_item.get("text", "")
            if action == "k":
                STATE["review_pending"].pop(token, None)
                save_state()
                await callback.message.edit_text(f"✅ Оставлено:\n\n{original[:3600]}")
                await callback.answer("Оставлено")
            elif action == "d":
                try:
                    await bot.delete_message(int(pending_item["chat_id"]), int(pending_item["message_id"]))
                except Exception:
                    await callback.answer("Не удалось удалить сообщение в чате.", show_alert=True)
                    return
                STATE["review_pending"].pop(token, None)
                save_state()
                await callback.message.edit_text(f"🗑 Удалено:\n\n{original[:3600]}")
                await callback.answer("Удалено")
            return

        await callback.answer()
    except Exception:
        log.exception("Ошибка обработки кнопки")
        try:
            await callback.answer("Произошла ошибка.", show_alert=True)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════
# ГЛАВНЫЙ ОБРАБОТЧИК ГРУППЫ
# ══════════════════════════════════════════════════════════════
async def process(message: Message, edited: bool) -> None:
    mode = get_bot_mode()
    if mode == "stop":
        return
    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        return
    user = message.from_user
    if user is None or user.is_bot:
        return

    # Диагностические команды (только создателю)
    head = (message.text or "").strip().lower()
    if head.startswith("/") and not edited and user.id == ADMIN_ID:
        c = head.split()[0].split("@")[0]
        if c in ("/status", "/id", "/test"):
            await diag(message, c)
            return

    if not env_chat_ids() and STATE.get("chat_id") is None:
        STATE["chat_id"] = chat.id
        save_state()
        log.info("Рабочий чат запомнен: %s", chat.id)
    if not is_allowed_chat(chat.id):
        return

    name = (user.full_name or "").strip()
    text = (message.text or message.caption or "").strip()
    low  = text.lower()
    n    = now()
    maybe_reset_day_counters(n)

    # Определяем, обращаются ли к боту
    is_bot_reply     = message.reply_to_message and getattr(message.reply_to_message.from_user, "id", None) == bot.id
    has_bot_mention  = bool(re.search(r"\b(бот|бота|боту|ботяра)\b", low))
    if not has_bot_mention and RT.bot_username and ("@" + RT.bot_username.lower()) in low:
        has_bot_mention = True
    directed_to_bot  = is_bot_reply or has_bot_mention

    # ─── ОТЧЁТ ───
    if text:
        rep = parse_report(text)
        if rep:
            store = store_name(name, user.id, text)
            if store in STORE_NAMES:
                dt     = message.date.astimezone(TZ) if message.date else wall_now()
                window = classify_window(dt)
                record_report(store, window, dt, rep)
                save_state()
                if not RT.is_suspended:
                    if not edited:
                        await handle_report(message, store, window, rep, dt)
                    elif not is_manager(name, user.id) and user.id != ADMIN_ID:
                        await react(message, ["👀"])
                return
        else:
            # Уведомляем создателя о подозрительных сообщениях от магазинов
            if len(text) > 15 and re.search(r'\b(то|от|смарт|серв|акс|чек|план)\b', low) and any(ch.isdigit() for ch in text):
                store = store_name(name, user.id, text)
                if store in STORE_NAMES:
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"⚠️ Сообщение от <b>{escape(store)}</b> ({escape(name)}) не распознано как отчёт:\n\n<pre>{escape(text)}</pre>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

    # ─── РУКОВОДИТЕЛЬ ───
    if user.id == ADMIN_ID:
        await react(message, MANAGER_REACTIONS)
        return

    if is_manager(name, user.id):
        await react(message, MANAGER_REACTIONS)
        if not edited:
            RT.last_human = time.time()
            register_manager_activity(time.time())
        # 1С снова работает
        if TECH_FIXED_RE.search(low) and (time.time() - RT.tech_at < 5400 or ONE_C_RE.search(text)):
            RT.mute_until = 0.0
            await react(message, ["👍", "🙏"])
            return
        # Тег бота → сбой + 30 мин мут
        if directed_to_bot:
            if mode == "active" and not RT.is_suspended:
                await say(pick_bag("bot_mention_manager", SITUATIONS["bot_mention_manager"]),
                          message, track_deletable=True, msg_type="tech")
            mute_for(30)
            return
        # 1С проблемы
        if ONE_C_RE.search(text):
            RT.tech_at = time.time()
            if not RT.is_suspended and mode == "active":
                await maybe_send_tech_reply(message, n)
        if TECH_RE.search(low):
            RT.tech_at = time.time()
        return

    # ─── ОБЫЧНЫЕ УЧАСТНИКИ ───
    if not edited:
        RT.last_human = time.time()

    # Прощание
    if FAREWELL_RE.search(low):
        await react(message, ["🫡", "🤗"])
        if can_speak(n, gap=False) and mode == "active" and not RT.is_suspended:
            await say(pick_bag("farewell", SITUATIONS["farewell"]), message)
        mute_for(30)
        return

    # Команды группы
    if low.startswith("/") and not edited:
        cmd = low.split()[0].split("@")[0]
        if cmd in ("/top", "/today", "/joke", "/help", "/start"):
            if not RT.is_suspended and mode in ("active",):
                await handle_command(message, cmd)
        return

    if edited or not text:
        return

    # 1С заработала
    if TECH_FIXED_RE.search(low) and (time.time() - RT.tech_at < 5400 or ONE_C_RE.search(text)):
        RT.mute_until = 0.0
        await react(message, ["👍", "🙏"])
        return

    # 1С сломалась
    if ONE_C_RE.search(text):
        RT.tech_at = time.time()
        await react(message, ["🗿", "🤡", "🙈", "🥱"])
        if not RT.is_suspended and mode == "active":
            await maybe_send_tech_reply(message, n)
        return

    if TECH_RE.search(low):
        RT.tech_at = time.time()
        await react(message, ["🗿", "🤡", "🙈", "🥱"])
        return

    # День рождения
    if BDAY_RE.search(low):
        await react(message, ["🎉", "🍾"])
        if (time.time() - RT.last_bday > 3600 and can_speak(n, gap=False)
                and mode == "active" and not RT.is_suspended):
            RT.last_bday = time.time()
            await say("🎂 Присоединяюсь к поздравлениям! Пусть план радует так же, как этот день 🎉", message)
        return

    # ─── ТЕГ БОТА ───
    if directed_to_bot and len(text) < 200:
        if mode in ("stop", "react_only", "silent") or RT.is_suspended:
            await react(message, ["🤫", "⏳", "👀"])
            return
        if not can_speak(n, gap=False):
            await react(message, ["🤫"])
            return

        now_ts = time.time()

        # Игнорируем во время мута
        if now_ts < RT.ignore_mentions_until:
            await react(message, ["🤫", "⏳", "👀"])
            return

        # Считаем теги в последние 10 минут
        RT.group_mentions = [ts for ts in RT.group_mentions if now_ts - ts < 600]
        RT.group_mentions.append(now_ts)

        is_store_user = user.id in STORE_BY_USER_ID
        pool_key = "bot_mention_store" if is_store_user else "bot_mention_employee"

        if len(RT.group_mentions) >= 3:
            # 3-й тег подряд → сбой + 30 мин молчание (как при менеджере)
            await say(
                pick_bag("bot_mention_manager", SITUATIONS["bot_mention_manager"]),
                message, track_deletable=True, msg_type="tech"
            )
            RT.ignore_mentions_until = now_ts + 1800  # 30 мин
            RT.group_mentions.clear()
        else:
            await say(
                pick_bag(pool_key, SITUATIONS[pool_key]),
                message, track_deletable=True, msg_type="joke"
            )
        return

    # Приветствие
    if len(text) <= 60 and GREET_RE.search(low):
        key = (n.date().isoformat(), user.id)
        if key not in RT.greeted:
            RT.greeted.add(key)
            if not RT.is_suspended:
                await react(message, ["👍", "😎", "🤗", "🫡"])

# ══════════════════════════════════════════════════════════════
# DISPATCHER
# ══════════════════════════════════════════════════════════════
@dp.message()
async def on_message(message: Message):
    try:
        if message.chat.type == "private":
            await handle_private_message(message)
        else:
            await process(message, edited=False)
    except Exception:
        log.exception("Ошибка обработки сообщения")

@dp.edited_message()
async def on_edited(message: Message):
    try:
        if message.chat.type != "private":
            await process(message, edited=True)
    except Exception:
        log.exception("Ошибка обработки правки")

# ══════════════════════════════════════════════════════════════
# ПЛАНИРОВЩИК
# ══════════════════════════════════════════════════════════════
async def run_slot(kind: str, n: datetime) -> str:
    mode = get_bot_mode()
    if mode in ("stop", "react_only"):
        return "skip"
    critical = kind in ("nudge_morning", "nudge_midday")
    if not can_speak(n, jokes=False, ignore_mute=critical):
        return "retry"

    text, html = None, False
    msg_type   = "text"
    track_del  = False

    if kind == "greeting":
        text = greeting_text(n)

    elif critical:
        if RT.resumed_ts and time.time() - RT.resumed_ts < 120:
            return "retry"
        window    = "morning" if kind == "nudge_morning" else "midday"
        today     = biz_date(n).isoformat()
        known, unk = count_reports_today(today, window)
        total      = known + unk

        if total >= TOTAL_STORES:
            reported = set(day_entries(today, window))
            miss     = sorted(set(STORE_NAMES) - reported)
            unk_keys = list(STATE["history"].get(today, {}).get("__unknown__", {}).keys())
            info     = (
                f"📊 <b>Окно {window}:</b> Получено {total} отчётов (из них {unk} с неизвестных).\n"
                + (f"Не сдали с официальных учёток: {', '.join(miss)}.\n" if miss else "")
                + (f"Неизвестные аккаунты: {', '.join(unk_keys)}.\n" if unk_keys else "")
                + "Напоминание в общий чат отменено ✅"
            )
            try:
                await bot.send_message(ADMIN_ID, info, parse_mode="HTML")
            except Exception:
                pass
            return "skip"

        reported = set(day_entries(today, window))
        miss     = sorted(set(STORE_NAMES) - reported)
        if not miss:
            return "skip"

        try:
            warning = f" (⚠️ Неопознанных: {unk})" if unk > 0 else ""
            await bot.send_message(
                ADMIN_ID,
                f"📊 <b>Окно {window}:</b> {total}/{TOTAL_STORES} отчётов.{warning}\n"
                f"Не идентифицированы или не сдали: {', '.join(miss[:8])}{'...' if len(miss) > 8 else ''}\n"
                f"Отправлено напоминание в группу.",
                parse_mode="HTML"
            )
        except Exception:
            pass

        label_cap = "☕ Утренний отчёт" if window == "morning" else "⏳ Обеденный отчёт"
        shown = ", ".join(miss[:8])
        more  = f" и ещё {len(miss) - 8}" if len(miss) > 8 else ""
        text  = f"{label_cap} ещё ждём от: {shown}{more}. Коллеги, пожалуйста, не забываем 🙏"
        msg_type  = "reminder"
        track_del = True

    if text is None:
        return "skip"

    ok = await say(text, html=html, track_deletable=track_del, msg_type=msg_type)
    return "sent" if ok else "retry"

async def tick() -> None:
    n     = now()
    if not get_chat_id():
        return
    today = n.date().isoformat()
    done  = STATE["done"].setdefault(today, [])
    for old in sorted(STATE["done"])[:-3]:
        STATE["done"].pop(old, None)

    for slot_id, h, m, kind, _ in SLOTS:
        if slot_id in done:
            continue
        start = n.replace(hour=h, minute=m, second=0, microsecond=0)
        if n < start:
            continue
        if n > start + timedelta(minutes=SLOT_GRACE_MIN):
            done.append(slot_id)
            continue
        status = await run_slot(kind, n)
        if status in ("sent", "skip"):
            mark_slot_done(done, slot_id)
            save_state()
            break
        if status == "retry" and kind in ("nudge_morning", "nudge_midday"):
            break

async def scheduler_loop() -> None:
    await asyncio.sleep(90)
    log.info("Планировщик активирован (90 сек после старта)")
    while True:
        try:
            if get_bot_mode() not in ("stop", "react_only") and not RT.is_suspended and not is_after_hard_mute():
                await tick()
        except Exception:
            log.exception("Ошибка планировщика")
        await asyncio.sleep(20)

# ══════════════════════════════════════════════════════════════
# ВЕБ-СЕРВЕР (keepalive)
# ══════════════════════════════════════════════════════════════
async def handle_ping(request):
    return web.Response(text="OK, bot is alive")

async def start_keepalive_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()

# ══════════════════════════════════════════════════════════════
# СТАРТ
# ══════════════════════════════════════════════════════════════
async def main() -> None:
    load_state()
    RT.startup_ts  = time.time()
    RT.last_human  = time.time()
    await bot.delete_webhook(drop_pending_updates=False)
    await start_keepalive_server()
    try:
        me = await bot.get_me()
        RT.bot_username  = me.username or ""
        RT.bot_name_norm = _norm_name(me.full_name)
    except Exception as e:
        log.warning("get_me не удался: %s", e)

    asyncio.create_task(scheduler_loop())
    log.info("Бот запущен. Время: %s", now().strftime("%Y-%m-%d %H:%M"))

    n = now()
    history_days = len(STATE.get("history", {}))
    today_str    = n.date().isoformat()
    today_total  = sum(
        1 for sd in STATE["history"].get(today_str, {}).values()
        if any(w in sd for w in ("morning", "midday", "evening"))
    )

    if history_days > 0 and today_total > 0:
        restart_msg = (
            f"🔄 <b>Перезапустился.</b>\n\n"
            f"📦 История: {history_days} дн. | Отчётов за сегодня ({n.strftime('%d.%m')}): {today_total}\n\n"
            f"Скинь свежий <b>result.json</b> чтобы актуализировал.\n"
            f"<i>Или нажми «Запустить» — работаю с тем что есть.</i>"
        )
    elif history_days > 0:
        restart_msg = (
            f"🔄 <b>Перезапустился.</b> История ({history_days} дн.) есть, но отчётов за сегодня нет.\n\n"
            f"Скинь <b>result.json</b> или перешли отчёты мне в ЛС.\n"
            f"<i>Или нажми «Запустить» без файла.</i>"
        )
    else:
        restart_msg = (
            f"🔄 <b>Перезапустился.</b> Память пустая.\n\n"
            f"📋 Скинь <b>result.json</b> (экспорт из Telegram Desktop) или перешли отчёты за сегодня ({n.strftime('%d.%m')}).\n"
            f"<i>Или нажми «Запустить» — подожду 2 мин и начну работу.</i>"
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Запустить в работу", callback_data="hist:start")],
        [InlineKeyboardButton(text="⏸ Оставить на паузе",  callback_data="hist:pause")],
    ])
    try:
        await bot.send_message(ADMIN_ID, restart_msg, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        log.warning("Не удалось отправить уведомление о рестарте: %s", e)

    await dp.start_polling(bot, allowed_updates=["message", "edited_message", "message_reaction", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())