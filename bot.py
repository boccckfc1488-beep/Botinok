"""
VaultChat Server + Telegram Bot
Railway deployment | aiogram 3.22.0 | aiohttp HTTP API | SQLite

HTTP API (для Android-клиента):
  POST /api/register        {phone, name, username}
  POST /api/login           {phone}  → присылает код в Telegram
  POST /api/verify          {phone, code} → token
  GET  /api/me?token=...
  POST /api/me/update       {token, name, bio, status, avatar_color}
  GET  /api/users?token=... → список всех юзеров
  GET  /api/online?token=...→ онлайн-пользователи
  POST /api/ping            {token} → обновить last_seen
  POST /api/message/send    {token, to_phone, text}
  GET  /api/messages?token=...&with=<phone>&after=<id>
  GET  /api/chats?token=... → список чатов с последним сообщением

Telegram Bot:
  /start  — приветствие
  /files  — твоя оригинальная функция (добавь сам)
  Коды верификации приходят автоматически
"""

import asyncio
import hashlib
import json
import os
import random
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message

# ── Конфиг ─────────────────────────────────────────────────────────────────
BOT_TOKEN  = "8501402161:AAGuBiYq4ATma9u4oMo8-bFGhkQ3dn6jnAQ"
API_PORT   = int(os.environ.get("PORT", 8080))
DB_PATH    = os.environ.get("DB_PATH", "vaultchat.db")

# Время жизни кода верификации (секунды)
CODE_TTL   = 300   # 5 минут
# Время жизни токена (секунды)
TOKEN_TTL  = 60 * 60 * 24 * 30  # 30 дней

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# ── База данных ─────────────────────────────────────────────────────────────

def db_connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def db_init():
    con = db_connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        phone        TEXT    UNIQUE NOT NULL,
        name         TEXT    NOT NULL DEFAULT '',
        username     TEXT    UNIQUE,
        bio          TEXT    DEFAULT '',
        status       TEXT    DEFAULT 'В сети',
        avatar_color INTEGER DEFAULT 0,
        verified     INTEGER DEFAULT 0,
        created_at   INTEGER DEFAULT 0,
        last_seen    INTEGER DEFAULT 0,
        tg_chat_id   INTEGER DEFAULT 0
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
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        from_phone TEXT NOT NULL,
        to_phone   TEXT NOT NULL,
        text       TEXT NOT NULL,
        sent_at    INTEGER NOT NULL,
        read       INTEGER DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_msg_pair
        ON messages(from_phone, to_phone, sent_at);
    CREATE INDEX IF NOT EXISTS idx_token
        ON tokens(token);
    """)
    con.commit()
    con.close()

# ── Хелперы ─────────────────────────────────────────────────────────────────

def now() -> int:
    return int(time.time())

def make_token() -> str:
    return secrets.token_hex(32)

def clean_phone(phone: str) -> str:
    return "+" + "".join(c for c in phone if c.isdigit())

def auth(con, token: str):
    """Возвращает phone если токен валиден, иначе None."""
    row = con.execute(
        "SELECT phone FROM tokens WHERE token=? AND expires_at>?",
        (token, now())
    ).fetchone()
    return row["phone"] if row else None

def user_by_phone(con, phone: str):
    return con.execute(
        "SELECT * FROM users WHERE phone=?", (phone,)
    ).fetchone()

def user_row_to_dict(row) -> dict:
    if row is None:
        return None
    return {
        "phone":        row["phone"],
        "name":         row["name"],
        "username":     row["username"] or "",
        "bio":          row["bio"] or "",
        "status":       row["status"] or "В сети",
        "avatar_color": row["avatar_color"],
        "verified":     bool(row["verified"]),
        "last_seen":    row["last_seen"],
        "online":       (now() - row["last_seen"]) < 60,
    }

def json_ok(data: dict) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json"
    )

def json_err(msg: str, status=400) -> web.Response:
    return web.Response(
        text=json.dumps({"ok": False, "error": msg}, ensure_ascii=False),
        content_type="application/json",
        status=status
    )

# ── HTTP handlers ────────────────────────────────────────────────────────────

async def handle_register(req: web.Request) -> web.Response:
    """
    POST /api/register
    {phone, name, username}
    Создаёт юзера и шлёт код в Telegram (если tg_chat_id известен).
    """
    try:
        body = await req.json()
    except Exception:
        return json_err("invalid json")

    phone    = clean_phone(body.get("phone", ""))
    name     = body.get("name", "").strip()
    username = body.get("username", "").strip().lstrip("@") or None

    if not phone or not name:
        return json_err("phone and name required")

    con = db_connect()
    try:
        existing = user_by_phone(con, phone)
        if existing:
            # Уже зарегистрирован — просто шлём новый код
            return await _send_code(con, phone)

        # Проверяем уникальность username
        if username:
            dup = con.execute(
                "SELECT phone FROM users WHERE username=?", (username,)
            ).fetchone()
            if dup:
                return json_err("username taken")

        verified = 1 if phone == "+888888888888" else 0
        con.execute(
            "INSERT INTO users (phone,name,username,verified,created_at,last_seen) "
            "VALUES (?,?,?,?,?,?)",
            (phone, name, username, verified, now(), now())
        )
        con.commit()
        return await _send_code(con, phone)
    finally:
        con.close()


async def _send_code(con, phone: str) -> web.Response:
    """Генерирует код, сохраняет в БД, шлёт в Telegram если tg_chat_id известен."""
    code    = f"{random.randint(0, 99999):05d}"
    expires = now() + CODE_TTL

    con.execute(
        "INSERT OR REPLACE INTO verify_codes (phone,code,expires_at) VALUES (?,?,?)",
        (phone, code, expires)
    )
    con.commit()

    # Пробуем отправить в Telegram
    row = con.execute(
        "SELECT tg_chat_id FROM users WHERE phone=?", (phone,)
    ).fetchone()

    sent_tg = False
    if row and row["tg_chat_id"]:
        try:
            await bot.send_message(
                row["tg_chat_id"],
                f"🔐 Ваш код VaultChat: *{code}*\n\nДействителен 5 минут.\nНикому не сообщайте.",
                parse_mode="Markdown"
            )
            sent_tg = True
        except Exception:
            pass

    return json_ok({
        "ok":     True,
        "sent_tg": sent_tg,
        # В debug-режиме возвращаем код — убери в продакшне!
        "debug_code": code
    })


async def handle_login(req: web.Request) -> web.Response:
    """POST /api/login  {phone}  — отправить новый код."""
    try:
        body = await req.json()
    except Exception:
        return json_err("invalid json")

    phone = clean_phone(body.get("phone", ""))
    if not phone:
        return json_err("phone required")

    con = db_connect()
    try:
        if not user_by_phone(con, phone):
            return json_err("user not found")
        return await _send_code(con, phone)
    finally:
        con.close()


async def handle_verify(req: web.Request) -> web.Response:
    """POST /api/verify  {phone, code} → {token, user}"""
    try:
        body = await req.json()
    except Exception:
        return json_err("invalid json")

    phone = clean_phone(body.get("phone", ""))
    code  = str(body.get("code", "")).strip()

    con = db_connect()
    try:
        row = con.execute(
            "SELECT * FROM verify_codes WHERE phone=? AND expires_at>?",
            (phone, now())
        ).fetchone()

        if not row or row["code"] != code:
            return json_err("wrong or expired code", 401)

        # Удаляем использованный код
        con.execute("DELETE FROM verify_codes WHERE phone=?", (phone,))

        # Создаём токен
        token = make_token()
        con.execute(
            "INSERT OR REPLACE INTO tokens (token,phone,expires_at) VALUES (?,?,?)",
            (token, phone, now() + TOKEN_TTL)
        )

        # Обновляем last_seen
        con.execute(
            "UPDATE users SET last_seen=? WHERE phone=?", (now(), phone)
        )
        con.commit()

        user = user_row_to_dict(user_by_phone(con, phone))
        return json_ok({"ok": True, "token": token, "user": user})
    finally:
        con.close()


async def handle_me(req: web.Request) -> web.Response:
    """GET /api/me?token=..."""
    token = req.rel_url.query.get("token", "")
    con   = db_connect()
    try:
        phone = auth(con, token)
        if not phone:
            return json_err("unauthorized", 401)
        con.execute("UPDATE users SET last_seen=? WHERE phone=?", (now(), phone))
        con.commit()
        return json_ok({"ok": True, "user": user_row_to_dict(user_by_phone(con, phone))})
    finally:
        con.close()


async def handle_me_update(req: web.Request) -> web.Response:
    """POST /api/me/update  {token, name, bio, status, avatar_color}"""
    try:
        body = await req.json()
    except Exception:
        return json_err("invalid json")

    con = db_connect()
    try:
        phone = auth(con, body.get("token", ""))
        if not phone:
            return json_err("unauthorized", 401)

        fields, vals = [], []
        for key in ("name", "bio", "status"):
            if key in body:
                fields.append(f"{key}=?")
                vals.append(str(body[key])[:200])
        if "avatar_color" in body:
            fields.append("avatar_color=?")
            vals.append(int(body["avatar_color"]))
        if "username" in body:
            uname = str(body["username"]).strip().lstrip("@")[:30] or None
            if uname:
                dup = con.execute(
                    "SELECT phone FROM users WHERE username=? AND phone!=?",
                    (uname, phone)
                ).fetchone()
                if dup:
                    return json_err("username taken")
            fields.append("username=?")
            vals.append(uname)

        if fields:
            vals += [now(), phone]
            con.execute(
                f"UPDATE users SET {','.join(fields)}, last_seen=? WHERE phone=?",
                vals
            )
            con.commit()

        return json_ok({"ok": True, "user": user_row_to_dict(user_by_phone(con, phone))})
    finally:
        con.close()


async def handle_ping(req: web.Request) -> web.Response:
    """POST /api/ping  {token}  — обновить онлайн-статус."""
    try:
        body = await req.json()
    except Exception:
        return json_err("invalid json")

    con = db_connect()
    try:
        phone = auth(con, body.get("token", ""))
        if not phone:
            return json_err("unauthorized", 401)
        con.execute("UPDATE users SET last_seen=? WHERE phone=?", (now(), phone))
        con.commit()
        return json_ok({"ok": True})
    finally:
        con.close()


async def handle_users(req: web.Request) -> web.Response:
    """GET /api/users?token=...  — все юзеры."""
    token = req.rel_url.query.get("token", "")
    con   = db_connect()
    try:
        if not auth(con, token):
            return json_err("unauthorized", 401)
        rows = con.execute(
            "SELECT * FROM users ORDER BY last_seen DESC"
        ).fetchall()
        return json_ok({"ok": True, "users": [user_row_to_dict(r) for r in rows]})
    finally:
        con.close()


async def handle_online(req: web.Request) -> web.Response:
    """GET /api/online?token=...  — кто онлайн (last_seen < 60 сек)."""
    token = req.rel_url.query.get("token", "")
    con   = db_connect()
    try:
        if not auth(con, token):
            return json_err("unauthorized", 401)
        cutoff = now() - 60
        rows = con.execute(
            "SELECT * FROM users WHERE last_seen>? ORDER BY last_seen DESC",
            (cutoff,)
        ).fetchall()
        return json_ok({"ok": True, "online": [user_row_to_dict(r) for r in rows]})
    finally:
        con.close()


async def handle_send(req: web.Request) -> web.Response:
    """POST /api/message/send  {token, to_phone, text}"""
    try:
        body = await req.json()
    except Exception:
        return json_err("invalid json")

    con = db_connect()
    try:
        phone = auth(con, body.get("token", ""))
        if not phone:
            return json_err("unauthorized", 401)

        to_phone = clean_phone(body.get("to_phone", ""))
        text     = str(body.get("text", "")).strip()[:4096]

        if not to_phone or not text:
            return json_err("to_phone and text required")

        if not user_by_phone(con, to_phone):
            return json_err("recipient not found")

        cur = con.execute(
            "INSERT INTO messages (from_phone,to_phone,text,sent_at) VALUES (?,?,?,?)",
            (phone, to_phone, text, now())
        )
        con.commit()
        msg_id = cur.lastrowid

        # Пробуем уведомить получателя в Telegram
        rec = con.execute(
            "SELECT tg_chat_id, name FROM users WHERE phone=?", (to_phone,)
        ).fetchone()
        sender = con.execute(
            "SELECT name FROM users WHERE phone=?", (phone,)
        ).fetchone()
        if rec and rec["tg_chat_id"] and sender:
            try:
                await bot.send_message(
                    rec["tg_chat_id"],
                    f"💬 *{sender['name']}* написал вам в VaultChat:\n{text[:100]}{'…' if len(text)>100 else ''}",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        return json_ok({"ok": True, "id": msg_id})
    finally:
        con.close()


async def handle_messages(req: web.Request) -> web.Response:
    """GET /api/messages?token=...&with=<phone>&after=<id>"""
    token    = req.rel_url.query.get("token", "")
    with_ph  = req.rel_url.query.get("with", "")
    after_id = int(req.rel_url.query.get("after", "0"))

    con = db_connect()
    try:
        phone = auth(con, token)
        if not phone:
            return json_err("unauthorized", 401)

        with_ph = clean_phone(with_ph) if with_ph else None
        if not with_ph:
            return json_err("with required")

        rows = con.execute("""
            SELECT * FROM messages
            WHERE id > ?
              AND ((from_phone=? AND to_phone=?) OR (from_phone=? AND to_phone=?))
            ORDER BY id ASC
            LIMIT 200
        """, (after_id, phone, with_ph, with_ph, phone)).fetchall()

        # Помечаем как прочитанные
        con.execute("""
            UPDATE messages SET read=1
            WHERE to_phone=? AND from_phone=? AND read=0
        """, (phone, with_ph))
        con.commit()

        msgs = [{
            "id":       r["id"],
            "from":     r["from_phone"],
            "to":       r["to_phone"],
            "text":     r["text"],
            "sent_at":  r["sent_at"],
            "read":     bool(r["read"]),
            "outgoing": r["from_phone"] == phone,
        } for r in rows]

        return json_ok({"ok": True, "messages": msgs})
    finally:
        con.close()


async def handle_chats(req: web.Request) -> web.Response:
    """GET /api/chats?token=...  — список диалогов."""
    token = req.rel_url.query.get("token", "")
    con   = db_connect()
    try:
        phone = auth(con, token)
        if not phone:
            return json_err("unauthorized", 401)

        # Последнее сообщение с каждым собеседником
        rows = con.execute("""
            SELECT
                CASE WHEN from_phone=? THEN to_phone ELSE from_phone END AS peer,
                MAX(id) as last_id,
                MAX(sent_at) as last_at
            FROM messages
            WHERE from_phone=? OR to_phone=?
            GROUP BY peer
            ORDER BY last_at DESC
        """, (phone, phone, phone)).fetchall()

        chats = []
        for r in rows:
            peer    = r["peer"]
            last_m  = con.execute(
                "SELECT * FROM messages WHERE id=?", (r["last_id"],)
            ).fetchone()
            peer_u  = user_by_phone(con, peer)
            unread  = con.execute(
                "SELECT COUNT(*) as cnt FROM messages "
                "WHERE from_phone=? AND to_phone=? AND read=0",
                (peer, phone)
            ).fetchone()["cnt"]

            chats.append({
                "peer":         user_row_to_dict(peer_u) if peer_u else {"phone": peer},
                "last_message": {
                    "text":    last_m["text"],
                    "sent_at": last_m["sent_at"],
                    "outgoing": last_m["from_phone"] == phone,
                },
                "unread": unread,
            })

        return json_ok({"ok": True, "chats": chats})
    finally:
        con.close()


# ── Telegram Bot handlers ────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    """Сохраняем tg_chat_id по номеру телефона если передан через deep link."""
    args = msg.text.split(maxsplit=1)
    phone = None

    if len(args) > 1:
        raw = args[1].strip()
        if raw.lstrip("+").isdigit():
            phone = clean_phone(raw)

    con = db_connect()
    try:
        if phone:
            user = user_by_phone(con, phone)
            if user:
                con.execute(
                    "UPDATE users SET tg_chat_id=? WHERE phone=?",
                    (msg.chat.id, phone)
                )
                con.commit()
                await msg.answer(
                    f"✅ Telegram привязан к аккаунту VaultChat!\n"
                    f"Теперь коды подтверждения будут приходить сюда."
                )
                return

        # Сохраняем chat_id для последнего незарегистрированного — по запросу /link <phone>
        await msg.answer(
            "👋 Привет! Я бот VaultChat.\n\n"
            "Чтобы получать коды подтверждения здесь, "
            "отправь команду:\n`/link +79991234567`",
            parse_mode="Markdown"
        )
    finally:
        con.close()


@dp.message(F.text.startswith("/link"))
async def cmd_link(msg: Message):
    """Привязать Telegram к номеру телефона вручную."""
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Использование: `/link +79991234567`", parse_mode="Markdown")
        return

    phone = clean_phone(parts[1].strip())
    con   = db_connect()
    try:
        user = user_by_phone(con, phone)
        if not user:
            await msg.answer("❌ Пользователь с таким номером не найден в VaultChat.")
            return

        con.execute(
            "UPDATE users SET tg_chat_id=? WHERE phone=?",
            (msg.chat.id, phone)
        )
        con.commit()
        await msg.answer(
            f"✅ Telegram привязан к номеру `{phone}`!\n"
            f"Коды верификации будут приходить сюда.",
            parse_mode="Markdown"
        )
    finally:
        con.close()


@dp.message(F.text.startswith("/status"))
async def cmd_status(msg: Message):
    """Статус сервера."""
    con = db_connect()
    try:
        users_count = con.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        msgs_count  = con.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
        cutoff = now() - 60
        online  = con.execute(
            "SELECT COUNT(*) as c FROM users WHERE last_seen>?", (cutoff,)
        ).fetchone()["c"]
        await msg.answer(
            f"📊 *VaultChat Server*\n\n"
            f"👥 Пользователей: {users_count}\n"
            f"🟢 Онлайн: {online}\n"
            f"💬 Сообщений: {msgs_count}",
            parse_mode="Markdown"
        )
    finally:
        con.close()


# ── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    db_init()

    # HTTP сервер
    app = web.Application()
    app.router.add_post("/api/register",      handle_register)
    app.router.add_post("/api/login",         handle_login)
    app.router.add_post("/api/verify",        handle_verify)
    app.router.add_get ("/api/me",            handle_me)
    app.router.add_post("/api/me/update",     handle_me_update)
    app.router.add_post("/api/ping",          handle_ping)
    app.router.add_get ("/api/users",         handle_users)
    app.router.add_get ("/api/online",        handle_online)
    app.router.add_post("/api/message/send",  handle_send)
    app.router.add_get ("/api/messages",      handle_messages)
    app.router.add_get ("/api/chats",         handle_chats)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    print(f"[VaultChat] HTTP API запущен на порту {API_PORT}")

    # Telegram bot polling
    print("[VaultChat] Telegram бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
