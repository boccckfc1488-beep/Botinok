# bot.py — OSINT bot (aiogram 3, вариант Б: номер/юзернейм/почта)
# python 3.11+ / aiogram 3.13.1 / phonenumbers 8.13.0 / aiohttp 3.9.5
# pip install aiogram==3.13.1 phonenumbers==8.13.0 aiohttp==3.9.5

import asyncio
import html
import os
import re
import sqlite3
import time
from contextlib import closing

import aiohttp
import phonenumbers
from phonenumbers import geocoder, carrier, timezone
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ================= CONFIG =================
BOT_TOKEN = "8982090896:AAFpK-DnXHyLej2pX5rq1DhnEVu9Q_c1H4g"  # FILL: РЕВОКНИ в @BotFather
DB_PATH = "osint_bot.db"
FREE_LIMIT = 5

SHERLOCK_DIR = "./sherlock"       # git clone https://github.com/sherlock-project/sherlock
HOLEHE_DIR = "./holehe"           # git clone https://github.com/megadose/holehe

HIBP_KEY = ""                     # FILL: https://haveibeenpwned.com/API/Key (бесплатный)
ADMINS = {0}                      # FILL: твой user_id (узнаешь через /id)
# ==========================================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ============ DB ============
def db_init():
    with closing(sqlite3.connect(DB_PATH)) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                premium_until INTEGER DEFAULT 0,
                created_at    INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER NOT NULL,
                day     TEXT NOT NULL,
                count   INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, day)
            );
        """)
        c.commit()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def today() -> str: return time.strftime("%Y-%m-%d")
def now_ts() -> int: return int(time.time())

def upsert_user(m: Message):
    u = m.from_user
    if not u: return
    with closing(db()) as c:
        c.execute(
            """INSERT INTO users(user_id, username, first_name, created_at)
               VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username, first_name=excluded.first_name""",
            (u.id, u.username or "", u.first_name or "", now_ts()),
        )
        c.commit()

def is_premium(user_id: int) -> bool:
    with closing(db()) as c:
        row = c.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row["premium_until"] > now_ts())

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

def check_and_bump(user_id: int) -> tuple[bool, int]:
    if is_premium(user_id) or is_admin(user_id):
        return True, 9999
    with closing(db()) as c:
        row = c.execute("SELECT count FROM usage WHERE user_id=? AND day=?",
                        (user_id, today())).fetchone()
        used = row["count"] if row else 0
        if used >= FREE_LIMIT:
            return False, 0
        c.execute("""INSERT INTO usage(user_id, day, count) VALUES(?,?,1)
                     ON CONFLICT(user_id, day) DO UPDATE SET count=count+1""",
                  (user_id, today()))
        c.commit()
    return True, FREE_LIMIT - (used + 1)

def remaining(user_id: int) -> str:
    if is_premium(user_id) or is_admin(user_id):
        return "∞"
    with closing(db()) as c:
        row = c.execute("SELECT count FROM usage WHERE user_id=? AND day=?",
                        (user_id, today())).fetchone()
    used = row["count"] if row else 0
    return str(max(0, FREE_LIMIT - used))

# ============ OSINT: phone ============
def phone_lookup(number: str) -> dict:
    try:
        n = phonenumbers.parse(number, None)
        if not phonenumbers.is_valid_number(n):
            return {"ok": False, "err": "невалидный номер"}
        return {
            "ok": True,
            "e164": phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164),
            "international": phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
            "country": geocoder.description_for_number(n, "ru") or "?",
            "carrier": carrier.name_for_number(n, "ru") or "?",
            "timezone": ", ".join(timezone.time_zones_for_number(n)),
            "line_type": "мобильный" if phonenumbers.number_type(n) == phonenumbers.PhoneNumberType.MOBILE else "другой",
        }
    except Exception as e:
        return {"ok": False, "err": str(e)}

# ============ subprocess helper (async) ============
async def run_subprocess(cmd: list[str], cwd: str | None = None, timeout: int = 300):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "timeout"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

# ============ OSINT: sherlock (ник) ============
async def username_sherlock(username: str) -> dict:
    script = os.path.join(SHERLOCK_DIR, "sherlock_project", "sherlock.py")
    if not os.path.isfile(script):
        alt = os.path.join(SHERLOCK_DIR, "sherlock.py")
        if os.path.isfile(alt):
            script = alt
        else:
            return {"ok": False, "err": f"нет {script} — клонируй sherlock"}
    code, out, err = await run_subprocess(
        ["python", script, username, "--print-found", "--no-color", "--timeout", "15"],
        timeout=300,
    )
    if code == -1:
        return {"ok": False, "err": "timeout (слишком много площадок)"}
    found = []
    for line in out.splitlines():
        m = re.match(r"\[\+\]\s*([^:]+):\s*(\S+)", line.strip())
        if m:
            found.append((m.group(1).strip(), m.group(2).strip()))
    return {"ok": True, "found": found}

# ============ OSINT: holehe (почта) ============
async def email_holehe(email: str) -> dict:
    script = os.path.join(HOLEHE_DIR, "holehe")
    if not os.path.isfile(script):
        return {"ok": False, "err": f"нет {script} — клонируй holehe"}
    code, out, err = await run_subprocess(
        ["python", script, email, "--no-color", "--only-used"],
        timeout=120,
    )
    if code == -1:
        return {"ok": False, "err": "timeout"}
    found = []
    for line in out.splitlines():
        m = re.search(r"\[([^\]]+)\]", line)
        if m and ("✔" in line or "used" in line.lower()):
            found.append(m.group(1))
    return {"ok": True, "found": found}

# ============ OSINT: HIBP (утечки) ============
async def hibp_lookup(email: str) -> dict:
    if not HIBP_KEY:
        return {"ok": False, "err": "HIBP_KEY не задан в конфиге"}
    url = f"https://haveibeenpwned.com/api/v3/breachedaccount/{email}?truncateResponse=false"
    headers = {"hibp-api-key": HIBP_KEY, "user-agent": "osint-bot"}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            if r.status == 404: return {"ok": True, "breaches": []}
            if r.status == 401: return {"ok": False, "err": "HIBP_KEY неверный"}
            if r.status == 429: return {"ok": False, "err": "rate limit HIBP"}
            if r.status != 200: return {"ok": False, "err": f"HTTP {r.status}"}
            data = await r.json()
    return {"ok": True, "breaches": data}

# ============ formatters ============
def fmt_phone(r: dict) -> str:
    return (
        f"📱 <b>телефон</b>\n"
        f"E164: <code>{html.escape(r['e164'])}</code>\n"
        f"формат: {html.escape(r['international'])}\n"
        f"страна/регион: <b>{html.escape(r['country'])}</b>\n"
        f"оператор: <b>{html.escape(r['carrier'])}</b>\n"
        f"тип: {r['line_type']}\n"
        f"часовой пояс: {html.escape(r['timezone'])}"
    )

def fmt_holehe(email: str, r: dict) -> str:
    if not r["found"]:
        return f"📧 <b>{html.escape(email)}</b>\nсервисов с регистрацией не найдено"
    lst = "\n".join(f"• {html.escape(x)}" for x in r["found"])
    return f"📧 <b>{html.escape(email)}</b>\nзарегана на {len(r['found'])} сервисах:\n{lst}"

def fmt_sherlock(username: str, r: dict) -> str:
    if not r["found"]:
        return f"🔎 <b>@{html.escape(username)}</b>\nничего не найдено"
    lines = [f"• <a href=\"{html.escape(url)}\">{html.escape(site)}</a>" for site, url in r["found"]]
    lst = "\n".join(lines[:60])
    more = f"\n… и ещё {len(r['found']) - 60}" if len(r["found"]) > 60 else ""
    return f"🔎 <b>@{html.escape(username)}</b>\nнайдено {len(r['found'])} площадок:\n{lst}{more}"

def fmt_hibp(email: str, r: dict) -> str:
    if not r["breaches"]:
        return f"🛡 <b>{html.escape(email)}</b>\nв известных бреачах не найдено"
    lst = "\n".join(
        f"• {html.escape(b.get('Name','?'))} ({b.get('BreachDate','?')})"
        for b in r["breaches"][:30]
    )
    return f"🛡 <b>{html.escape(email)}</b>\nнайдено {len(r['breaches'])} бреачей:\n{lst}"

# ============ FSM ============
class UserFSM(StatesGroup):
    username = State()
    email = State()
    phone = State()
    leak_email = State()

class AdminFSM(StatesGroup):
    premium_uid = State()
    premium_days = State()

# ============ menus ============
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 отправить юзера", callback_data="m_user")],
        [InlineKeyboardButton(text="📧 отправить почту", callback_data="m_email")],
        [InlineKeyboardButton(text="📱 отправить номер", callback_data="m_phone")],
        [InlineKeyboardButton(text="🛡 проверить утечки", callback_data="m_leak")],
        [InlineKeyboardButton(text="👤 профиль", callback_data="m_me")],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← отмена", callback_data="m_back")]
    ])

def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 выдать премиум", callback_data="a_prem")],
        [InlineKeyboardButton(text="📊 статистика", callback_data="a_stats")],
    ])

# ============ gate ============
async def gate(m: Message) -> bool:
    upsert_user(m)
    ok, _ = check_and_bump(m.from_user.id)
    if not ok:
        await m.answer(
            f"🚫 дневной лимит исчерпан ({FREE_LIMIT}/{FREE_LIMIT}).\n"
            f"премиум — без лимита. @welmadev",
        )
        return False
    return True

# ============ commands ============
@router.message(CommandStart())
async def cmd_start(m: Message):
    upsert_user(m)
    prem = "💎 премиум" if is_premium(m.from_user.id) else "🆓 free"
    await m.answer(
        f"привет, {html.escape(m.from_user.first_name or '')}.\n"
        f"статус: {prem} • осталось сегодня: <b>{remaining(m.from_user.id)}</b>\n\n"
        f"открой меню: /menu",
        reply_markup=main_menu(),
    )

@router.message(Command("menu"))
async def cmd_menu(m: Message):
    upsert_user(m)
    prem = "💎 премиум" if is_premium(m.from_user.id) else "🆓 free"
    await m.answer(
        f"<b>OSINT панель</b>\nстатус: {prem} • осталось: <b>{remaining(m.from_user.id)}</b>",
        reply_markup=main_menu(),
    )

@router.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"твой ID: <code>{m.from_user.id}</code>")

@router.message(Command("premium"))
async def cmd_premium(m: Message):
    if is_premium(m.from_user.id):
        with closing(db()) as c:
            row = c.execute("SELECT premium_until FROM users WHERE user_id=?",
                            (m.from_user.id,)).fetchone()
        until = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["premium_until"]))
        await m.answer(f"💎 премиум до <b>{until}</b>")
    else:
        await m.answer("💎 премиум: без лимита запросов. написать @welmadev")

# ============ inline: menu ============
@router.callback_query(F.data == "m_back")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        f"<b>OSINT панель</b> • осталось: <b>{remaining(cb.from_user.id)}</b>",
        reply_markup=main_menu(),
    )
    await cb.answer()

@router.callback_query(F.data == "m_me")
async def cb_me(cb: CallbackQuery):
    upsert_user(cb.message)
    prem = "💎 премиум" if is_premium(cb.from_user.id) else "🆓 free"
    await cb.message.edit_text(
        f"👤 <b>профиль</b>\n"
        f"id: <code>{cb.from_user.id}</code>\n"
        f"статус: {prem}\n"
        f"осталось сегодня: <b>{remaining(cb.from_user.id)}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← назад", callback_data="m_back")]
        ]),
    )
    await cb.answer()

@router.callback_query(F.data == "m_user")
async def cb_user(cb: CallbackQuery, state: FSMContext):
    await state.set_state(UserFSM.username)
    await cb.message.edit_text("введи ник (без @):", reply_markup=back_kb())
    await cb.answer()

@router.callback_query(F.data == "m_email")
async def cb_email(cb: CallbackQuery, state: FSMContext):
    await state.set_state(UserFSM.email)
    await cb.message.edit_text("введи почту:", reply_markup=back_kb())
    await cb.answer()

@router.callback_query(F.data == "m_phone")
async def cb_phone(cb: CallbackQuery, state: FSMContext):
    await state.set_state(UserFSM.phone)
    await cb.message.edit_text("введи номер +7XXXXXXXXXX:", reply_markup=back_kb())
    await cb.answer()

@router.callback_query(F.data == "m_leak")
async def cb_leak(cb: CallbackQuery, state: FSMContext):
    await state.set_state(UserFSM.leak_email)
    await cb.message.edit_text("введи почту для проверки утечек:", reply_markup=back_kb())
    await cb.answer()

# ============ FSM handlers ============
@router.message(UserFSM.username)
async def fsm_username(m: Message, state: FSMContext):
    await state.clear()
    if not await gate(m): return
    nick = (m.text or "").strip().lstrip("@")
    if not re.match(r"^[A-Za-z0-9_.\-]{1,64}$", nick):
        await m.answer("невалидный ник", reply_markup=back_kb())
        return
    msg = await m.answer(f"⏳ ищу @{html.escape(nick)} по 400+ площадкам…")
    r = await username_sherlock(nick)
    if not r["ok"]:
        await msg.edit_text(f"ошибка: {html.escape(r['err'])}", reply_markup=back_kb())
        return
    await msg.edit_text(fmt_sherlock(nick, r), disable_web_page_preview=True,
                        reply_markup=back_kb())

@router.message(UserFSM.email)
async def fsm_email(m: Message, state: FSMContext):
    await state.clear()
    if not await gate(m): return
    email = (m.text or "").strip()
    if "@" not in email:
        await m.answer("невалидная почта", reply_markup=back_kb())
        return
    msg = await m.answer("⏳ ищу…")
    r = await email_holehe(email)
    if not r["ok"]:
        await msg.edit_text(f"ошибка: {html.escape(r['err'])}", reply_markup=back_kb())
        return
    await msg.edit_text(fmt_holehe(email, r), reply_markup=back_kb())

@router.message(UserFSM.phone)
async def fsm_phone(m: Message, state: FSMContext):
    await state.clear()
    if not await gate(m): return
    number = (m.text or "").strip()
    r = phone_lookup(number)
    if not r["ok"]:
        await m.answer(f"ошибка: {html.escape(r['err'])}", reply_markup=back_kb())
        return
    await m.answer(fmt_phone(r), reply_markup=back_kb())

@router.message(UserFSM.leak_email)
async def fsm_leak(m: Message, state: FSMContext):
    await state.clear()
    if not await gate(m): return
    email = (m.text or "").strip()
    if "@" not in email:
        await m.answer("невалидная почта", reply_markup=back_kb())
        return
    msg = await m.answer("⏳ проверяю…")
    r = await hibp_lookup(email)
    if not r["ok"]:
        await msg.edit_text(f"ошибка: {html.escape(r['err'])}", reply_markup=back_kb())
        return
    await msg.edit_text(fmt_hibp(email, r), reply_markup=back_kb())

# ============ admin ============
@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer("админ-меню:", reply_markup=admin_menu())

@router.callback_query(F.data == "a_stats")
async def cb_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("нет доступа", show_alert=True)
    with closing(db()) as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        prem = c.execute("SELECT COUNT(*) n FROM users WHERE premium_until>?",
                         (now_ts(),)).fetchone()["n"]
        tday = c.execute("SELECT COALESCE(SUM(count),0) n FROM usage WHERE day=?",
                         (today(),)).fetchone()["n"]
    await cb.message.edit_text(
        f"📊 <b>статистика</b>\n\nюзеров: {users}\nпремиум: {prem}\nзапросов сегодня: {tday}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← назад", callback_data="a_back")]
        ]),
    )
    await cb.answer()

@router.callback_query(F.data == "a_back")
async def cb_aback(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("нет доступа", show_alert=True)
    await cb.message.edit_text("админ-меню:", reply_markup=admin_menu())
    await cb.answer()

@router.callback_query(F.data == "a_prem")
async def cb_prem(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("нет доступа", show_alert=True)
    await state.set_state(AdminFSM.premium_uid)
    await cb.message.edit_text("введи user_id:")
    await cb.answer()

@router.message(AdminFSM.premium_uid)
async def adm_uid(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    val = (m.text or "").strip()
    if not val.lstrip("-").isdigit():
        await m.answer("нужен числовой ID")
        return
    await state.update_data(uid=int(val))
    await state.set_state(AdminFSM.premium_days)
    await m.answer("на сколько дней? (-1 чтобы снять)")

@router.message(AdminFSM.premium_days)
async def adm_days(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    val = (m.text or "").strip()
    if not val.lstrip("-").isdigit():
        await m.answer("число, плиз")
        return
    days = int(val)
    data = await state.get_data()
    uid = data["uid"]
    await state.clear()
    if days < 0:
        with closing(db()) as c:
            c.execute("UPDATE users SET premium_until=0 WHERE user_id=?", (uid,))
            c.commit()
        await m.answer(f"премиум снят с {uid}")
    else:
        until = now_ts() + days * 86400
        with closing(db()) as c:
            c.execute("UPDATE users SET premium_until=? WHERE user_id=?", (until, uid))
            c.commit()
        await m.answer(f"💎 выдан {uid} до {time.strftime('%Y-%m-%d', time.localtime(until))}")

# ============ main ============
async def main():
    db_init()
    me = await bot.get_me()
    print(f"[+] @{me.username} ({me.id}) запущен")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
