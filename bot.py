"""
VaultChat Server + Telegram Bot v2
- Владелец @f3lma может выдавать значки пользователям
- Галочка (verified), звезда (star), значок бота (bot_badge)
- Занятые номера/юзернеймы защищены
- Поиск по username через API
- Каналы и групповые чаты
- Баг с повторной регистрацией исправлен
"""

import asyncio, json, os, random, secrets, sqlite3, time
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)

BOT_TOKEN  = "8501402161:AAGuBiYq4ATma9u4oMo8-bFGhkQ3dn6jnAQ"
API_PORT   = int(os.environ.get("PORT", 8080))
DB_PATH    = os.environ.get("DB_PATH", "vaultchat.db")
OWNER_TG   = "f3lma"   # владелец — может выдавать значки
CODE_TTL   = 300
TOKEN_TTL  = 60 * 60 * 24 * 30

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ── FSM ─────────────────────────────────────────────────────────────────────
class Reg(StatesGroup):
    waiting_phone = State()

class BadgeState(StatesGroup):
    waiting_target = State()
    waiting_badge  = State()

# ── БД ──────────────────────────────────────────────────────────────────────
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def db_init():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        phone        TEXT    UNIQUE NOT NULL,
        name         TEXT    NOT NULL DEFAULT '',
        username     TEXT    UNIQUE,
        bio          TEXT    DEFAULT '',
        status       TEXT    DEFAULT 'В сети',
        avatar_color INTEGER DEFAULT 0,
        badge        TEXT    DEFAULT NULL,
        created_at   INTEGER DEFAULT 0,
        last_seen    INTEGER DEFAULT 0,
        tg_chat_id   INTEGER DEFAULT 0,
        tg_username  TEXT    DEFAULT NULL
    );
    CREATE TABLE IF NOT EXISTS verify_codes (
        phone      TEXT PRIMARY KEY,
        code       TEXT NOT NULL,
        expires_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tokens (
        token      TEXT PRIMARY KEY,
        phone      TEXT NOT NULL,
        expires_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        from_phone  TEXT    NOT NULL,
        to_id       TEXT    NOT NULL,
        chat_type   TEXT    NOT NULL DEFAULT 'private',
        text        TEXT    NOT NULL,
        sent_at     INTEGER NOT NULL,
        read        INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS chats (
        id          TEXT    PRIMARY KEY,
        type        TEXT    NOT NULL DEFAULT 'group',
        title       TEXT    NOT NULL,
        description TEXT    DEFAULT '',
        avatar_color INTEGER DEFAULT 0,
        created_by  TEXT    NOT NULL,
        created_at  INTEGER DEFAULT 0,
        invite_link TEXT    DEFAULT NULL
    );
    CREATE TABLE IF NOT EXISTS chat_members (
        chat_id TEXT NOT NULL,
        phone   TEXT NOT NULL,
        role    TEXT NOT NULL DEFAULT 'member',
        joined_at INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, phone)
    );
    CREATE INDEX IF NOT EXISTS idx_msg ON messages(to_id, sent_at);
    CREATE INDEX IF NOT EXISTS idx_tok ON tokens(token);
    CREATE INDEX IF NOT EXISTS idx_uname ON users(username);
    """)
    con.commit()
    con.close()

# ── Хелперы ─────────────────────────────────────────────────────────────────
def now() -> int: return int(time.time())

def clean_phone(p: str) -> str:
    digits = "".join(c for c in str(p) if c.isdigit())
    return "+" + digits if digits else ""

def auth_token(con, token: str):
    row = con.execute(
        "SELECT phone FROM tokens WHERE token=? AND expires_at>?", (token, now())
    ).fetchone()
    return row["phone"] if row else None

def get_user(con, phone: str):
    return con.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()

def get_user_by_username(con, username: str):
    return con.execute(
        "SELECT * FROM users WHERE username=?", (username.lstrip("@"),)
    ).fetchone()

def badge_emoji(badge: str) -> str:
    return {
        "verified": "✅",
        "star":     "⭐",
        "bot":      "🤖",
        "vip":      "💎",
        "dev":      "👨‍💻",
        "owner":    "👑",
    }.get(badge, "")

def user_dict(row) -> dict:
    if not row: return None
    badge = row["badge"] if row["badge"] else None
    return {
        "phone":        row["phone"],
        "name":         row["name"],
        "username":     row["username"] or "",
        "bio":          row["bio"] or "",
        "status":       row["status"] or "В сети",
        "avatar_color": row["avatar_color"],
        "badge":        badge,
        "badge_emoji":  badge_emoji(badge) if badge else "",
        "verified":     badge == "verified",
        "last_seen":    row["last_seen"],
        "online":       (now() - row["last_seen"]) < 60,
    }

def ok(data: dict) -> web.Response:
    return web.Response(text=json.dumps(data, ensure_ascii=False),
                        content_type="application/json")

def err(msg: str, status=400) -> web.Response:
    return web.Response(text=json.dumps({"ok": False, "error": msg}, ensure_ascii=False),
                        content_type="application/json", status=status)

def is_owner(msg: Message) -> bool:
    return (msg.from_user.username or "").lower() == OWNER_TG.lower()

def gen_chat_id() -> str:
    return secrets.token_hex(8)

async def tg_send_code(phone: str, chat_id: int) -> str:
    code = f"{random.randint(0, 99999):05d}"
    con  = db()
    con.execute(
        "INSERT OR REPLACE INTO verify_codes (phone,code,expires_at) VALUES (?,?,?)",
        (phone, code, now() + CODE_TTL)
    )
    con.commit()
    con.close()
    try:
        await bot.send_message(
            chat_id,
            f"🔐 Код VaultChat:\n\n*{code}*\n\nДействителен 5 минут.",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[TG] send error: {e}")
    return code

# ── Клавиатуры ───────────────────────────────────────────────────────────────
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📱 Войти / Зарегистрироваться", callback_data="enter")
    ]])

def kb_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[
        KeyboardButton(text="📱 Поделиться номером", request_contact=True)
    ],[
        KeyboardButton(text="✏️ Ввести вручную")
    ]], resize_keyboard=True, one_time_keyboard=True)

def kb_cancel() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[
        KeyboardButton(text="❌ Отмена")
    ]], resize_keyboard=True, one_time_keyboard=True)

def kb_badges() -> InlineKeyboardMarkup:
    badges = [
        ("✅ Верификация", "verified"),
        ("⭐ Звезда",      "star"),
        ("💎 VIP",         "vip"),
        ("👨‍💻 Dev",        "dev"),
        ("👑 Owner",       "owner"),
        ("🤖 Bot",         "bot"),
        ("❌ Убрать значок","none"),
    ]
    rows = []
    for label, data in badges:
        rows.append([InlineKeyboardButton(text=label, callback_data=f"badge_{data}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── Telegram Bot ──────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    con  = db()
    user = con.execute(
        "SELECT phone,name,badge FROM users WHERE tg_chat_id=?", (msg.chat.id,)
    ).fetchone()
    con.close()

    if user:
        badge = badge_emoji(user["badge"]) if user["badge"] else ""
        await msg.answer(
            f"👋 С возвращением, *{user['name']}* {badge}\n"
            f"📱 Номер: `{user['phone']}`\n\n"
            f"/newcode — получить код входа\n"
            f"/myphone — мой номер",
            parse_mode="Markdown",
            reply_markup=kb_main()
        )
    else:
        await msg.answer(
            "👋 Привет! Я бот *VaultChat*.\n\n"
            "Привяжи номер телефона чтобы получать коды входа в Telegram.",
            parse_mode="Markdown",
            reply_markup=kb_main()
        )

@dp.callback_query(F.data == "enter")
async def cb_enter(call: CallbackQuery, state: FSMContext):
    await call.message.answer(
        "📱 Выбери способ.\n\n"
        "Можно использовать *любой* номер — хоть `+0`.\n"
        "Этот номер станет твоим ID в VaultChat.",
        parse_mode="Markdown",
        reply_markup=kb_phone()
    )
    await state.set_state(Reg.waiting_phone)
    await call.answer()

@dp.message(Reg.waiting_phone, F.contact)
async def got_contact(msg: Message, state: FSMContext):
    phone = clean_phone(msg.contact.phone_number)
    await _do_register(msg, state, phone)

@dp.message(Reg.waiting_phone, F.text == "✏️ Ввести вручную")
async def enter_manual(msg: Message):
    await msg.answer(
        "✏️ Введи любой номер:\n`+79648259956`, `+0`, `+123`",
        parse_mode="Markdown", reply_markup=kb_cancel()
    )

@dp.message(Reg.waiting_phone, F.text == "❌ Отмена")
async def reg_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    await cmd_start(msg, state)

@dp.message(Reg.waiting_phone, F.text)
async def got_phone_text(msg: Message, state: FSMContext):
    digits = "".join(c for c in msg.text if c.isdigit())
    if not digits:
        await msg.answer("❌ Введи номер с цифрами, например `+79991234567` или `0`",
                         parse_mode="Markdown")
        return
    await _do_register(msg, state, "+" + digits)

async def _do_register(msg: Message, state: FSMContext, phone: str):
    await state.clear()
    con = db()
    try:
        existing = get_user(con, phone)

        # ── Баг-фикс: номер уже занят другим tg_chat_id ──────────────
        if existing and existing["tg_chat_id"] and existing["tg_chat_id"] != msg.chat.id:
            await msg.answer(
                f"❌ Номер *{phone}* уже зарегистрирован другим пользователем.",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove()
            )
            return

        tg_uname = msg.from_user.username or None

        if existing:
            # Уже наш — просто обновляем chat_id
            con.execute(
                "UPDATE users SET tg_chat_id=?, tg_username=?, last_seen=? WHERE phone=?",
                (msg.chat.id, tg_uname, now(), phone)
            )
            con.commit()
        else:
            name = msg.from_user.first_name or "Пользователь"
            badge = "owner" if tg_uname and tg_uname.lower() == OWNER_TG.lower() else None
            # username из TG только если ещё не занят
            app_uname = None
            if tg_uname:
                taken = con.execute(
                    "SELECT 1 FROM users WHERE username=?", (tg_uname,)
                ).fetchone()
                if not taken:
                    app_uname = tg_uname
            try:
                con.execute(
                    "INSERT INTO users (phone,name,username,badge,created_at,last_seen,tg_chat_id,tg_username) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (phone, name, app_uname, badge, now(), now(), msg.chat.id, tg_uname)
                )
            except sqlite3.IntegrityError:
                con.execute(
                    "INSERT OR IGNORE INTO users (phone,name,username,badge,created_at,last_seen,tg_chat_id,tg_username) "
                    "VALUES (?,?,NULL,?,?,?,?,?)",
                    (phone, name, badge, now(), now(), msg.chat.id, tg_uname)
                )
            con.commit()
    finally:
        con.close()

    code = await tg_send_code(phone, msg.chat.id)
    await msg.answer(
        f"✅ Номер *{phone}* привязан!\n\n"
        f"Твой код входа:\n🔑 *{code}*\n\n"
        f"Введи его в приложении VaultChat.\n"
        f"Действителен 5 минут.\n\n"
        f"/newcode — получить новый код",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(Command("newcode"))
async def cmd_newcode(msg: Message):
    con  = db()
    user = con.execute(
        "SELECT phone FROM users WHERE tg_chat_id=?", (msg.chat.id,)
    ).fetchone()
    con.close()
    if not user:
        await msg.answer("❌ Номер не привязан. /start"); return
    code = await tg_send_code(user["phone"], msg.chat.id)
    await msg.answer(
        f"🔑 Код для *{user['phone']}*:\n\n*{code}*\n\nДействителен 5 минут.",
        parse_mode="Markdown"
    )

@dp.message(Command("myphone"))
async def cmd_myphone(msg: Message):
    con  = db()
    user = con.execute(
        "SELECT phone,name,username,badge FROM users WHERE tg_chat_id=?", (msg.chat.id,)
    ).fetchone()
    con.close()
    if not user:
        await msg.answer("Номер не привязан. /start"); return
    badge = badge_emoji(user["badge"]) if user["badge"] else ""
    await msg.answer(
        f"📱 *{user['name']}* {badge}\n"
        f"Номер: `{user['phone']}`\n"
        f"Username: @{user['username'] or '—'}",
        parse_mode="Markdown"
    )

@dp.message(Command("status"))
async def cmd_status(msg: Message):
    con = db()
    uc  = con.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    mc  = con.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
    cc  = con.execute("SELECT COUNT(*) as c FROM chats").fetchone()["c"]
    on  = con.execute("SELECT COUNT(*) as c FROM users WHERE last_seen>?",
                      (now()-60,)).fetchone()["c"]
    con.close()
    await msg.answer(
        f"📊 *VaultChat Server*\n\n"
        f"👥 Пользователей: {uc}\n"
        f"🟢 Онлайн: {on}\n"
        f"💬 Сообщений: {mc}\n"
        f"📢 Каналов/чатов: {cc}",
        parse_mode="Markdown"
    )

# ── Значки (только владелец) ─────────────────────────────────────────────────

@dp.message(Command("badge"))
async def cmd_badge(msg: Message, state: FSMContext):
    if not is_owner(msg):
        await msg.answer("❌ Только владелец может выдавать значки."); return
    await msg.answer(
        "Кому выдать значок?\n"
        "Введи @username или номер телефона:",
        reply_markup=kb_cancel()
    )
    await state.set_state(BadgeState.waiting_target)

@dp.message(BadgeState.waiting_target, F.text == "❌ Отмена")
async def badge_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.", reply_markup=ReplyKeyboardRemove())

@dp.message(BadgeState.waiting_target, F.text)
async def badge_target(msg: Message, state: FSMContext):
    if not is_owner(msg):
        await state.clear(); return
    text = msg.text.strip()
    con  = db()
    if text.startswith("@"):
        user = get_user_by_username(con, text)
    else:
        phone = clean_phone(text)
        user  = get_user(con, phone) if phone else None
    con.close()

    if not user:
        await msg.answer("❌ Пользователь не найден. Попробуй ещё:"); return

    await state.update_data(target_phone=user["phone"], target_name=user["name"])
    badge = badge_emoji(user["badge"]) if user["badge"] else "нет"
    await msg.answer(
        f"Пользователь: *{user['name']}* (`{user['phone']}`)\n"
        f"Текущий значок: {badge}\n\n"
        f"Выбери новый значок:",
        parse_mode="Markdown",
        reply_markup=kb_badges()
    )
    await state.set_state(BadgeState.waiting_badge)

@dp.callback_query(BadgeState.waiting_badge, F.data.startswith("badge_"))
async def badge_set(call: CallbackQuery, state: FSMContext):
    if not is_owner(call):
        await call.answer("❌ Нет прав"); return

    data  = await state.get_data()
    phone = data.get("target_phone")
    name  = data.get("target_name")
    badge = call.data.replace("badge_", "")
    if badge == "none": badge = None

    con = db()
    con.execute("UPDATE users SET badge=? WHERE phone=?", (badge, phone))
    con.commit()
    # Уведомляем юзера если он привязан
    row = con.execute("SELECT tg_chat_id FROM users WHERE phone=?", (phone,)).fetchone()
    con.close()

    emoji = badge_emoji(badge) if badge else ""
    if row and row["tg_chat_id"]:
        try:
            await bot.send_message(
                row["tg_chat_id"],
                f"🎉 Вам {'выдан' if badge else 'убран'} значок "
                f"*{emoji} {badge or ''}* в VaultChat!",
                parse_mode="Markdown"
            )
        except: pass

    await call.message.edit_text(
        f"✅ Значок *{emoji} {badge or 'убран'}* для *{name}* обновлён.",
        parse_mode="Markdown"
    )
    await state.clear()
    await call.answer()

# ── HTTP API ──────────────────────────────────────────────────────────────────

async def h_register(req: web.Request):
    try: body = await req.json()
    except: return err("invalid json")

    phone    = clean_phone(body.get("phone",""))
    name     = str(body.get("name","")).strip()[:100]
    username = str(body.get("username","")).strip().lstrip("@")[:30] or None

    if not phone: return err("phone required")
    if not name:  return err("name required")

    con = db()
    try:
        existing = get_user(con, phone)

        # Баг-фикс: если телефон уже есть — просто шлём новый код, не пересоздаём
        if existing:
            return await _api_send_code(con, phone)

        # Проверка занятости username
        if username:
            if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                return err("username taken")

        badge = "owner" if username and username.lower() == OWNER_TG.lower() else None
        con.execute(
            "INSERT INTO users (phone,name,username,badge,created_at,last_seen) VALUES (?,?,?,?,?,?)",
            (phone, name, username, badge, now(), now())
        )
        con.commit()
        return await _api_send_code(con, phone)
    except sqlite3.IntegrityError as e:
        return err(f"conflict: {e}")
    finally:
        con.close()

async def _api_send_code(con, phone: str) -> web.Response:
    code = f"{random.randint(0,99999):05d}"
    con.execute(
        "INSERT OR REPLACE INTO verify_codes (phone,code,expires_at) VALUES (?,?,?)",
        (phone, code, now()+CODE_TTL)
    )
    con.commit()
    row = con.execute("SELECT tg_chat_id FROM users WHERE phone=?", (phone,)).fetchone()
    sent_tg = False
    if row and row["tg_chat_id"]:
        try:
            await bot.send_message(
                row["tg_chat_id"],
                f"🔐 Код VaultChat:\n\n*{code}*\n\nДействителен 5 минут.",
                parse_mode="Markdown"
            )
            sent_tg = True
        except: pass
    return ok({"ok": True, "sent_tg": sent_tg, "debug_code": code})

async def h_login(req: web.Request):
    try: body = await req.json()
    except: return err("invalid json")
    phone = clean_phone(body.get("phone",""))
    if not phone: return err("phone required")
    con = db()
    try:
        if not get_user(con, phone): return err("user not found")
        return await _api_send_code(con, phone)
    finally: con.close()

async def h_verify(req: web.Request):
    try: body = await req.json()
    except: return err("invalid json")
    phone = clean_phone(body.get("phone",""))
    code  = str(body.get("code","")).strip()
    con   = db()
    try:
        row = con.execute(
            "SELECT * FROM verify_codes WHERE phone=? AND expires_at>?", (phone, now())
        ).fetchone()
        if not row or row["code"] != code: return err("wrong or expired code", 401)
        con.execute("DELETE FROM verify_codes WHERE phone=?", (phone,))
        token = secrets.token_hex(32)
        con.execute(
            "INSERT OR REPLACE INTO tokens (token,phone,expires_at) VALUES (?,?,?)",
            (token, phone, now()+TOKEN_TTL)
        )
        con.execute("UPDATE users SET last_seen=? WHERE phone=?", (now(), phone))
        con.commit()
        return ok({"ok": True, "token": token, "user": user_dict(get_user(con, phone))})
    finally: con.close()

async def h_me(req: web.Request):
    con = db()
    try:
        phone = auth_token(con, req.rel_url.query.get("token",""))
        if not phone: return err("unauthorized", 401)
        con.execute("UPDATE users SET last_seen=? WHERE phone=?", (now(), phone))
        con.commit()
        return ok({"ok": True, "user": user_dict(get_user(con, phone))})
    finally: con.close()

async def h_me_update(req: web.Request):
    try: body = await req.json()
    except: return err("invalid json")
    con = db()
    try:
        phone = auth_token(con, body.get("token",""))
        if not phone: return err("unauthorized", 401)
        fields, vals = [], []
        for k in ("name","bio","status"):
            if k in body: fields.append(f"{k}=?"); vals.append(str(body[k])[:200])
        if "avatar_color" in body:
            fields.append("avatar_color=?"); vals.append(int(body["avatar_color"]))
        if "username" in body:
            u = str(body["username"]).strip().lstrip("@")[:30] or None
            if u:
                dup = con.execute(
                    "SELECT 1 FROM users WHERE username=? AND phone!=?", (u, phone)
                ).fetchone()
                if dup: return err("username taken")
            fields.append("username=?"); vals.append(u)
        if fields:
            vals += [now(), phone]
            con.execute(f"UPDATE users SET {','.join(fields)},last_seen=? WHERE phone=?", vals)
            con.commit()
        return ok({"ok": True, "user": user_dict(get_user(con, phone))})
    finally: con.close()

async def h_ping(req: web.Request):
    try: body = await req.json()
    except: return err("invalid json")
    con = db()
    try:
        phone = auth_token(con, body.get("token",""))
        if not phone: return err("unauthorized", 401)
        con.execute("UPDATE users SET last_seen=? WHERE phone=?", (now(), phone))
        con.commit()
        return ok({"ok": True})
    finally: con.close()

async def h_users(req: web.Request):
    con = db()
    try:
        if not auth_token(con, req.rel_url.query.get("token","")): return err("unauthorized",401)
        rows = con.execute("SELECT * FROM users ORDER BY last_seen DESC").fetchall()
        return ok({"ok": True, "users": [user_dict(r) for r in rows]})
    finally: con.close()

async def h_search(req: web.Request):
    """GET /api/search?token=...&q=username"""
    q     = req.rel_url.query.get("q","").strip().lstrip("@")
    con   = db()
    try:
        if not auth_token(con, req.rel_url.query.get("token","")): return err("unauthorized",401)
        if not q: return err("q required")
        rows = con.execute(
            "SELECT * FROM users WHERE username LIKE ? OR name LIKE ? LIMIT 20",
            (f"%{q}%", f"%{q}%")
        ).fetchall()
        return ok({"ok": True, "users": [user_dict(r) for r in rows]})
    finally: con.close()

async def h_online(req: web.Request):
    con = db()
    try:
        if not auth_token(con, req.rel_url.query.get("token","")): return err("unauthorized",401)
        rows = con.execute(
            "SELECT * FROM users WHERE last_seen>? ORDER BY last_seen DESC", (now()-60,)
        ).fetchall()
        return ok({"ok": True, "online": [user_dict(r) for r in rows]})
    finally: con.close()

# ── Личные сообщения ──────────────────────────────────────────────────────────

async def h_send(req: web.Request):
    try: body = await req.json()
    except: return err("invalid json")
    con = db()
    try:
        phone    = auth_token(con, body.get("token",""))
        if not phone: return err("unauthorized",401)
        to_phone = clean_phone(body.get("to_phone",""))
        text     = str(body.get("text","")).strip()[:4096]
        if not to_phone or not text: return err("to_phone and text required")
        if not get_user(con, to_phone): return err("recipient not found")
        cur = con.execute(
            "INSERT INTO messages (from_phone,to_id,chat_type,text,sent_at) VALUES (?,?,?,?,?)",
            (phone, to_phone, "private", text, now())
        )
        con.commit()
        rec    = con.execute("SELECT tg_chat_id,name FROM users WHERE phone=?", (to_phone,)).fetchone()
        sender = con.execute("SELECT name FROM users WHERE phone=?", (phone,)).fetchone()
        if rec and rec["tg_chat_id"] and sender:
            try:
                await bot.send_message(
                    rec["tg_chat_id"],
                    f"💬 *{sender['name']}*:\n{text[:200]}{'…' if len(text)>200 else ''}",
                    parse_mode="Markdown"
                )
            except: pass
        return ok({"ok": True, "id": cur.lastrowid})
    finally: con.close()

async def h_messages(req: web.Request):
    q        = req.rel_url.query
    con      = db()
    try:
        phone    = auth_token(con, q.get("token",""))
        if not phone: return err("unauthorized",401)
        with_ph  = clean_phone(q.get("with",""))
        after_id = int(q.get("after","0"))
        if not with_ph: return err("with required")
        rows = con.execute("""
            SELECT * FROM messages
            WHERE id>? AND chat_type='private'
              AND ((from_phone=? AND to_id=?) OR (from_phone=? AND to_id=?))
            ORDER BY id ASC LIMIT 200
        """, (after_id, phone, with_ph, with_ph, phone)).fetchall()
        con.execute(
            "UPDATE messages SET read=1 WHERE to_id=? AND from_phone=? AND read=0 AND chat_type='private'",
            (phone, with_ph)
        )
        con.commit()
        return ok({"ok": True, "messages": [{
            "id": r["id"], "from": r["from_phone"], "to": r["to_id"],
            "text": r["text"], "sent_at": r["sent_at"],
            "read": bool(r["read"]), "outgoing": r["from_phone"] == phone
        } for r in rows]})
    finally: con.close()

async def h_chats(req: web.Request):
    con = db()
    try:
        phone = auth_token(con, req.rel_url.query.get("token",""))
        if not phone: return err("unauthorized",401)
        rows = con.execute("""
            SELECT CASE WHEN from_phone=? THEN to_id ELSE from_phone END AS peer,
                   MAX(id) as last_id, MAX(sent_at) as last_at
            FROM messages WHERE chat_type='private' AND (from_phone=? OR to_id=?)
            GROUP BY peer ORDER BY last_at DESC
        """, (phone,phone,phone)).fetchall()
        result = []
        for r in rows:
            peer = r["peer"]
            last = con.execute("SELECT * FROM messages WHERE id=?", (r["last_id"],)).fetchone()
            pu   = get_user(con, peer)
            unr  = con.execute(
                "SELECT COUNT(*) as c FROM messages "
                "WHERE from_phone=? AND to_id=? AND read=0 AND chat_type='private'",
                (peer,phone)
            ).fetchone()["c"]
            result.append({
                "type": "private",
                "peer": user_dict(pu) if pu else {"phone": peer},
                "last_message": {"text": last["text"], "sent_at": last["sent_at"],
                                 "outgoing": last["from_phone"]==phone},
                "unread": unr
            })
        # + групповые чаты где состоит юзер
        group_rows = con.execute("""
            SELECT c.*, cm.role FROM chats c
            JOIN chat_members cm ON c.id=cm.chat_id
            WHERE cm.phone=?
            ORDER BY c.created_at DESC
        """, (phone,)).fetchall()
        for gr in group_rows:
            last = con.execute(
                "SELECT * FROM messages WHERE to_id=? AND chat_type!=? ORDER BY id DESC LIMIT 1",
                (gr["id"], "private")
            ).fetchone()
            unr = con.execute(
                "SELECT COUNT(*) as c FROM messages WHERE to_id=? AND read=0 AND from_phone!=?",
                (gr["id"], phone)
            ).fetchone()["c"]
            result.append({
                "type":  gr["type"],
                "id":    gr["id"],
                "title": gr["title"],
                "description": gr["description"],
                "avatar_color": gr["avatar_color"],
                "role":  gr["role"],
                "last_message": {"text": last["text"], "sent_at": last["sent_at"],
                                 "outgoing": last["from_phone"]==phone} if last else None,
                "unread": unr
            })
        return ok({"ok": True, "chats": result})
    finally: con.close()

# ── Каналы / Групповые чаты ───────────────────────────────────────────────────

async def h_chat_create(req: web.Request):
    """POST /api/chat/create {token, type, title, description, avatar_color}"""
    try: body = await req.json()
    except: return err("invalid json")
    con = db()
    try:
        phone = auth_token(con, body.get("token",""))
        if not phone: return err("unauthorized",401)
        chat_type = body.get("type","group")
        if chat_type not in ("group","channel"): return err("type must be group or channel")
        title = str(body.get("title","")).strip()[:100]
        if not title: return err("title required")
        desc  = str(body.get("description","")).strip()[:500]
        color = int(body.get("avatar_color", 0))
        cid   = gen_chat_id()
        link  = secrets.token_urlsafe(8)
        con.execute(
            "INSERT INTO chats (id,type,title,description,avatar_color,created_by,created_at,invite_link) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cid, chat_type, title, desc, color, phone, now(), link)
        )
        con.execute(
            "INSERT INTO chat_members (chat_id,phone,role,joined_at) VALUES (?,?,?,?)",
            (cid, phone, "owner", now())
        )
        con.commit()
        return ok({"ok": True, "chat": {
            "id": cid, "type": chat_type, "title": title,
            "description": desc, "invite_link": link
        }})
    finally: con.close()

async def h_chat_join(req: web.Request):
    """POST /api/chat/join {token, chat_id or invite_link}"""
    try: body = await req.json()
    except: return err("invalid json")
    con = db()
    try:
        phone = auth_token(con, body.get("token",""))
        if not phone: return err("unauthorized",401)
        cid  = body.get("chat_id","")
        link = body.get("invite_link","")
        chat = None
        if cid:
            chat = con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
        elif link:
            chat = con.execute("SELECT * FROM chats WHERE invite_link=?", (link,)).fetchone()
        if not chat: return err("chat not found")
        if chat["type"] == "channel": return err("channels are join-only via invite")
        already = con.execute(
            "SELECT 1 FROM chat_members WHERE chat_id=? AND phone=?", (chat["id"], phone)
        ).fetchone()
        if already: return err("already a member")
        con.execute(
            "INSERT INTO chat_members (chat_id,phone,role,joined_at) VALUES (?,?,?,?)",
            (chat["id"], phone, "member", now())
        )
        con.commit()
        return ok({"ok": True, "chat_id": chat["id"], "title": chat["title"]})
    finally: con.close()

async def h_chat_send(req: web.Request):
    """POST /api/chat/send {token, chat_id, text}"""
    try: body = await req.json()
    except: return err("invalid json")
    con = db()
    try:
        phone  = auth_token(con, body.get("token",""))
        if not phone: return err("unauthorized",401)
        cid    = body.get("chat_id","")
        text   = str(body.get("text","")).strip()[:4096]
        if not cid or not text: return err("chat_id and text required")
        chat   = con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
        if not chat: return err("chat not found")
        member = con.execute(
            "SELECT role FROM chat_members WHERE chat_id=? AND phone=?", (cid, phone)
        ).fetchone()
        if not member: return err("not a member")
        # В каналах пишут только owner/admin
        if chat["type"] == "channel" and member["role"] not in ("owner","admin"):
            return err("only admins can post in channels")
        cur = con.execute(
            "INSERT INTO messages (from_phone,to_id,chat_type,text,sent_at) VALUES (?,?,?,?,?)",
            (phone, cid, chat["type"], text, now())
        )
        con.commit()
        return ok({"ok": True, "id": cur.lastrowid})
    finally: con.close()

async def h_chat_messages(req: web.Request):
    """GET /api/chat/messages?token=&chat_id=&after="""
    q    = req.rel_url.query
    con  = db()
    try:
        phone    = auth_token(con, q.get("token",""))
        if not phone: return err("unauthorized",401)
        cid      = q.get("chat_id","")
        after_id = int(q.get("after","0"))
        if not cid: return err("chat_id required")
        member = con.execute(
            "SELECT 1 FROM chat_members WHERE chat_id=? AND phone=?", (cid,phone)
        ).fetchone()
        if not member: return err("not a member")
        rows = con.execute(
            "SELECT m.*, u.name as sender_name, u.badge as sender_badge "
            "FROM messages m LEFT JOIN users u ON m.from_phone=u.phone "
            "WHERE m.to_id=? AND m.id>? ORDER BY m.id ASC LIMIT 200",
            (cid, after_id)
        ).fetchall()
        con.execute(
            "UPDATE messages SET read=1 WHERE to_id=? AND from_phone!=? AND read=0",
            (cid, phone)
        )
        con.commit()
        return ok({"ok": True, "messages": [{
            "id":           r["id"],
            "from":         r["from_phone"],
            "sender_name":  r["sender_name"] or r["from_phone"],
            "sender_badge": badge_emoji(r["sender_badge"]) if r["sender_badge"] else "",
            "text":         r["text"],
            "sent_at":      r["sent_at"],
            "outgoing":     r["from_phone"] == phone
        } for r in rows]})
    finally: con.close()

async def h_chat_members(req: web.Request):
    """GET /api/chat/members?token=&chat_id="""
    q   = req.rel_url.query
    con = db()
    try:
        phone = auth_token(con, q.get("token",""))
        if not phone: return err("unauthorized",401)
        cid   = q.get("chat_id","")
        if not cid: return err("chat_id required")
        rows  = con.execute("""
            SELECT u.*, cm.role FROM users u
            JOIN chat_members cm ON u.phone=cm.phone
            WHERE cm.chat_id=? ORDER BY cm.joined_at
        """, (cid,)).fetchall()
        return ok({"ok": True, "members": [{**user_dict(r), "role": r["role"]} for r in rows]})
    finally: con.close()

async def h_chat_info(req: web.Request):
    """GET /api/chat/info?token=&chat_id="""
    q   = req.rel_url.query
    con = db()
    try:
        phone = auth_token(con, q.get("token",""))
        if not phone: return err("unauthorized",401)
        cid   = q.get("chat_id","")
        chat  = con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
        if not chat: return err("not found")
        count = con.execute("SELECT COUNT(*) as c FROM chat_members WHERE chat_id=?", (cid,)).fetchone()["c"]
        return ok({"ok": True, "chat": {
            "id": chat["id"], "type": chat["type"], "title": chat["title"],
            "description": chat["description"], "avatar_color": chat["avatar_color"],
            "members_count": count, "invite_link": chat["invite_link"]
        }})
    finally: con.close()

# ── Запуск ────────────────────────────────────────────────────────────────────
async def main():
    db_init()
    app = web.Application()
    # Пользователи
    app.router.add_post("/api/register",       h_register)
    app.router.add_post("/api/login",          h_login)
    app.router.add_post("/api/verify",         h_verify)
    app.router.add_get ("/api/me",             h_me)
    app.router.add_post("/api/me/update",      h_me_update)
    app.router.add_post("/api/ping",           h_ping)
    app.router.add_get ("/api/users",          h_users)
    app.router.add_get ("/api/online",         h_online)
    app.router.add_get ("/api/search",         h_search)
    # Личные сообщения
    app.router.add_post("/api/message/send",   h_send)
    app.router.add_get ("/api/messages",       h_messages)
    app.router.add_get ("/api/chats",          h_chats)
    # Группы / каналы
    app.router.add_post("/api/chat/create",    h_chat_create)
    app.router.add_post("/api/chat/join",      h_chat_join)
    app.router.add_post("/api/chat/send",      h_chat_send)
    app.router.add_get ("/api/chat/messages",  h_chat_messages)
    app.router.add_get ("/api/chat/members",   h_chat_members)
    app.router.add_get ("/api/chat/info",      h_chat_info)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", API_PORT).start()
    print(f"[VaultChat] HTTP API :{API_PORT}")
    print("[VaultChat] Bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
