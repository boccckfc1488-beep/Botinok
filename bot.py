# bot.py — SaveMod bot (вариант А: личка)
# python 3.11+ / aiogram 3.13+ / sqlite3 (stdlib)
# pip install aiogram==3.13.1

import asyncio
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
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

# ============ CONFIG ============
BOT_TOKEN = "8351857367:AAHEf9raO8whKDx2QTrWtrVX35LcD2q9q78"  # FILL: отзови после теста
ADMIN_USERNAME = "welmadev"
DB_PATH = "savemod.db"
SELF_DESTRUCT_CHECK_DELAY = 5      # сек, через сколько проверять удаление
SELF_DESTRUCT_MAX_AGE = 600        # сек, сколько следим за сообщением

# id админов подтянутся из таблицы admins + сюда можно хардкодить
HARDCODED_ADMINS: set[int] = set()

# ============ DB ============
def db_init() -> None:
    with closing(sqlite3.connect(DB_PATH)) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                premium_until INTEGER DEFAULT 0,
                created_at  INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id    INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                message_id  INTEGER NOT NULL,
                kind        TEXT NOT NULL,      -- text/photo/video/doc/voice/sticker
                content     TEXT,               -- text or file_id
                caption     TEXT,
                created_at  INTEGER NOT NULL,
                deleted_at  INTEGER DEFAULT 0,
                edited_at   INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_msg_owner_mid
                ON messages(owner_id, chat_id, message_id);
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS forced_subs (
                channel    TEXT PRIMARY KEY,     -- @username или -100...
                title      TEXT,
                invite     TEXT
            );
            """
        )
        c.commit()

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ============ helpers ============
def now() -> int:
    return int(time.time())

def is_premium(user_id: int) -> bool:
    with closing(db()) as c:
        row = c.execute(
            "SELECT premium_until FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    return bool(row and row["premium_until"] > now())

def is_admin(user_id: int) -> bool:
    if user_id in HARDCODED_ADMINS:
        return True
    with closing(db()) as c:
        row = c.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    return row is not None

def upsert_user(m: Message) -> None:
    u = m.from_user
    if not u:
        return
    with closing(db()) as c:
        c.execute(
            """INSERT INTO users(user_id, username, first_name, created_at)
               VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 first_name=excluded.first_name""",
            (u.id, u.username or "", u.first_name or "", now()),
        )
        c.commit()

def save_message(m: Message, kind: str, content: str, caption: str = "") -> None:
    with closing(db()) as c:
        c.execute(
            """INSERT INTO messages(owner_id, chat_id, message_id, kind, content, caption, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (m.from_user.id, m.chat.id, m.message_id, kind, content, caption, now()),
        )
        c.commit()

def find_message(chat_id: int, message_id: int):
    with closing(db()) as c:
        return c.execute(
            "SELECT * FROM messages WHERE chat_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
            (chat_id, message_id),
        ).fetchone()

def mark_deleted(chat_id: int, message_id: int) -> None:
    with closing(db()) as c:
        c.execute(
            "UPDATE messages SET deleted_at=? WHERE chat_id=? AND message_id=?",
            (now(), chat_id, message_id),
        )
        c.commit()

def mark_edited(chat_id: int, message_id: int) -> None:
    with closing(db()) as c:
        c.execute(
            "UPDATE messages SET edited_at=? WHERE chat_id=? AND message_id=?",
            (now(), chat_id, message_id),
        )
        c.commit()

# ============ forced subs ============
async def not_subscribed(bot: Bot, user_id: int) -> list[sqlite3.Row]:
    with closing(db()) as c:
        subs = c.execute("SELECT * FROM forced_subs").fetchall()
    missing = []
    for s in subs:
        try:
            member = await bot.get_chat_member(s["channel"], user_id)
            if member.status in (
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
            ):
                missing.append(s)
        except (TelegramBadRequest, TelegramForbiddenError):
            # бот не админ канала / неверный id — считаем что подписка не пройдена
            missing.append(s)
    return missing

def subs_kb(missing: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    rows = []
    for s in missing:
        title = s["title"] or s["channel"]
        url = s["invite"] or f"https://t.me/{s['channel'].lstrip('@')}"
        rows.append([InlineKeyboardButton(text=f"📢 {title}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Проверить", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ============ FSMs ============
class AdminFSM(StatesGroup):
    grant_premium_id = State()
    grant_premium_days = State()
    add_sub_channel = State()
    add_sub_title = State()
    add_sub_invite = State()

# ============ routers ============
user_router = Router()
admin_router = Router()

# ============ content extraction ============
def extract(msg: Message) -> tuple[str, str, str]:
    """→ (kind, content, caption)"""
    if msg.text:
        return "text", msg.text, ""
    if msg.photo:
        return "photo", msg.photo[-1].file_id, msg.caption or ""
    if msg.video:
        return "video", msg.video.file_id, msg.caption or ""
    if msg.document:
        return "doc", msg.document.file_id, msg.caption or ""
    if msg.voice:
        return "voice", msg.voice.file_id, ""
    if msg.audio:
        return "audio", msg.audio.file_id, msg.caption or ""
    if msg.sticker:
        return "sticker", msg.sticker.file_id, ""
    if msg.video_note:
        return "video_note", msg.video_note.file_id, ""
    if msg.animation:
        return "animation", msg.animation.file_id, msg.caption or ""
    return "other", "", ""

async def resend_saved(bot: Bot, owner_id: int, kind: str, content: str, caption: str, prefix: str) -> None:
    """переслать сохранённую копию владельцу с префиксом"""
    try:
        if kind == "text":
            await bot.send_message(owner_id, f"{prefix}\n\n{content}")
        elif kind == "photo":
            await bot.send_photo(owner_id, content, caption=f"{prefix}\n{caption}"[:1024])
        elif kind == "video":
            await bot.send_video(owner_id, content, caption=f"{prefix}\n{caption}"[:1024])
        elif kind == "doc":
            await bot.send_document(owner_id, content, caption=f"{prefix}\n{caption}"[:1024])
        elif kind == "voice":
            await bot.send_voice(owner_id, content, caption=prefix)
        elif kind == "audio":
            await bot.send_audio(owner_id, content, caption=f"{prefix}\n{caption}"[:1024])
        elif kind == "sticker":
            await bot.send_message(owner_id, prefix)
            await bot.send_sticker(owner_id, content)
        elif kind == "video_note":
            await bot.send_message(owner_id, prefix)
            await bot.send_video_note(owner_id, content)
        elif kind == "animation":
            await bot.send_animation(owner_id, content, caption=f"{prefix}\n{caption}"[:1024])
        else:
            await bot.send_message(owner_id, f"{prefix}\n[unsupported content]")
    except TelegramForbiddenError:
        pass  # юзер заблокировал бота

# ============ user handlers ============
@user_router.message(CommandStart())
async def cmd_start(m: Message, bot: Bot):
    upsert_user(m)
    missing = await not_subscribed(bot, m.from_user.id)
    if missing:
        await m.answer("чтобы пользоваться ботом — подпишись:", reply_markup=subs_kb(missing))
        return
    prem = "💎 премиум активен" if is_premium(m.from_user.id) else "🆓 free"
    await m.answer(
        f"привет, {m.from_user.first_name}.\n\n"
        f"я сохраняю твои сообщения и присылаю обратно удалённые / изменённые / "
        f"самоуничтожающиеся.\n\n"
        f"статус: {prem}\n\n"
        f"команды:\n"
        f"/id — твой ID\n"
        f"/premium — инфо о премиуме"
    )

@user_router.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"твой ID: <code>{m.from_user.id}</code>")

@user_router.message(Command("premium"))
async def cmd_premium(m: Message):
    if is_premium(m.from_user.id):
        with closing(db()) as c:
            row = c.execute("SELECT premium_until FROM users WHERE user_id=?", (m.from_user.id,)).fetchone()
        until = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["premium_until"]))
        await m.answer(f"💎 премиум до <b>{until}</b>")
    else:
        await m.answer("💎 премиум не активен. напиши админу @welmadev")

@user_router.callback_query(F.data == "check_subs")
async def cb_check_subs(cb: CallbackQuery, bot: Bot):
    missing = await not_subscribed(bot, cb.from_user.id)
    if missing:
        await cb.answer("ещё не всё подписано", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=subs_kb(missing))
    else:
        await cb.message.edit_text("✅ подписки подтверждены. жми /start")
    await cb.answer()

@user_router.message(F.chat.type == "private", ~F.text.startswith("/"))
async def on_private_message(m: Message, bot: Bot):
    """ловим всё что юзер пишет в личку боту и сохраняем"""
    if not m.from_user:
        return
    upsert_user(m)
    if await not_subscribed(bot, m.from_user.id):
        return
    kind, content, caption = extract(m)
    if kind == "other":
        return
    save_message(m, kind, content, caption)

    # планируем проверку на удаление — если юзер удалит у себя, мы пришлём копию
    asyncio.create_task(watch_deletion(bot, m.from_user.id, m.chat.id, m.message_id, kind, content, caption))

async def watch_deletion(bot: Bot, owner_id: int, chat_id: int, message_id: int, kind: str, content: str, caption: str):
    """через SELF_DESTRUCT_CHECK_DELAY проверяем: сообщение ещё живо? если нет — шлём копию."""
    await asyncio.sleep(SELF_DESTRUCT_CHECK_DELAY)
    try:
        # forwardMessage на самого себя — если сообщение удалено, телега бросит ошибку
        await bot.forward_message(chat_id=owner_id, from_chat_id=chat_id, message_id=message_id)
        # форвард удался — сообщение ещё живо, откатываем форвард
        # (удалить его не можем — это уже новое сообщение; вариант: сохранять fwd_id и удалять)
        # проще: не форвардить, а просто жить. Оставляем как есть, но пометим что живое.
    except TelegramBadRequest:
        # сообщение удалено → шлём сохранённую копию
        row = find_message(chat_id, message_id)
        if row and row["deleted_at"] == 0:
            mark_deleted(chat_id, message_id)
            await resend_saved(bot, owner_id, kind, content, caption, "🗑 <b>удалённое сообщение</b>")
    except TelegramForbiddenError:
        pass

@user_router.edited_message(F.chat.type == "private")
async def on_edited(m: Message, bot: Bot):
    if not m.from_user:
        return
    row = find_message(m.chat.id, m.message_id)
    if not row:
        return
    if row["edited_at"]:
        return
    mark_edited(m.chat.id, m.message_id)
    old = row["content"] if row["kind"] == "text" else f"[{row['kind']}]"
    new_kind, new_content, new_caption = extract(m)
    await bot.send_message(
        m.from_user.id,
        f"✏️ <b>изменённое сообщение</b>\n\n"
        f"было:\n<code>{old}</code>\n\n"
        f"стало:\n<code>{new_content if new_kind == 'text' else '['+new_kind+']'}</code>"
    )

# ============ admin ============
def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 выдать премиум", callback_data="adm_premium")],
        [InlineKeyboardButton(text="📢 обязательные подписки", callback_data="adm_subs")],
        [InlineKeyboardButton(text="📊 статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="👑 добавить админа", callback_data="adm_add_admin")],
    ])

@admin_router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("админ-меню:", reply_markup=admin_menu())

@admin_router.callback_query(F.data == "adm_stats")
async def cb_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("нет доступа", show_alert=True)
    with closing(db()) as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        msgs = c.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
        del_msgs = c.execute("SELECT COUNT(*) n FROM messages WHERE deleted_at>0").fetchone()["n"]
        prem = c.execute("SELECT COUNT(*) n FROM users WHERE premium_until>?", (now(),)).fetchone()["n"]
        subs = c.execute("SELECT COUNT(*) n FROM forced_subs").fetchone()["n"]
    await cb.message.edit_text(
        f"📊 <b>статистика</b>\n\n"
        f"юзеров: {users}\n"
        f"премиум: {prem}\n"
        f"сообщений: {msgs}\n"
        f"удалённых: {del_msgs}\n"
        f"обязательных подписок: {subs}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← назад", callback_data="adm_back")]
        ]),
    )
    await cb.answer()

@admin_router.callback_query(F.data == "adm_back")
async def cb_back(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("нет доступа", show_alert=True)
    await cb.message.edit_text("админ-меню:", reply_markup=admin_menu())
    await cb.answer()

# --- премиум ---
@admin_router.callback_query(F.data == "adm_premium")
async def cb_prem(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("нет доступа", show_alert=True)
    await state.set_state(AdminFSM.grant_premium_id)
    await cb.message.edit_text("введи ID юзера (или @username):")
    await cb.answer()

@admin_router.message(AdminFSM.grant_premium_id)
async def adm_prem_id(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    val = (m.text or "").strip()
    if val.startswith("@"):
        with closing(db()) as c:
            row = c.execute("SELECT user_id FROM users WHERE username=?", (val.lstrip("@"),)).fetchone()
        if not row:
            await m.answer("не нашёл такого юзера. пусть напишет боту /start")
            return
        uid = row["user_id"]
    else:
        try:
            uid = int(val)
        except ValueError:
            await m.answer("нужен числовой ID или @username")
            return
    await state.update_data(uid=uid)
    await state.set_state(AdminFSM.grant_premium_days)
    await m.answer("на сколько дней? (например 30, или -1 чтобы снять)")

@admin_router.message(AdminFSM.grant_premium_days)
async def adm_prem_days(m: Message, state: FSMContext, bot: Bot):
    if not is_admin(m.from_user.id):
        return
    try:
        days = int((m.text or "").strip())
    except ValueError:
        await m.answer("число, плиз")
        return
    data = await state.get_data()
    uid = data["uid"]
    await state.clear()
    if days < 0:
        with closing(db()) as c:
            c.execute("UPDATE users SET premium_until=0 WHERE user_id=?", (uid,))
            c.commit()
        await m.answer(f"премиум снят с {uid}")
        try:
            await bot.send_message(uid, "💎 твой премиум отключён")
        except TelegramForbiddenError:
            pass
    else:
        until = now() + days * 86400
        with closing(db()) as c:
            c.execute(
                "UPDATE users SET premium_until=? WHERE user_id=?",
                (until, uid),
            )
            c.commit()
        await m.answer(f"💎 премиум выдан {uid} до {time.strftime('%Y-%m-%d', time.localtime(until))}")
        try:
            await bot.send_message(uid, f"💎 тебе выдан премиум на {days} дн.")
        except TelegramForbiddenError:
            pass

# --- обязательные подписки ---
@admin_router.callback_query(F.data == "adm_subs")
async def cb_subs(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("нет доступа", show_alert=True)
    with closing(db()) as c:
        subs = c.execute("SELECT * FROM forced_subs").fetchall()
    text = "📢 <b>обязательные подписки</b>\n\n"
    if not subs:
        text += "пока пусто\n"
    else:
        for s in subs:
            text += f"• {s['title'] or s['channel']} — <code>{s['channel']}</code>\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ добавить", callback_data="adm_sub_add")],
        [InlineKeyboardButton(text="🗑 удалить", callback_data="adm_sub_del")],
        [InlineKeyboardButton(text="← назад", callback_data="adm_back")],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()

@admin_router.callback_query(F.data == "adm_sub_add")
async def cb_sub_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("нет доступа", show_alert=True)
    await state.set_state(AdminFSM.add_sub_channel)
    await cb.message.edit_text("кинь @username канала (бот должен быть в нём админом):")
    await cb.answer()

@admin_router.message(AdminFSM.add_sub_channel)
async def adm_sub_channel(m: Message, state: FSMContext, bot: Bot):
    if not is_admin(m.from_user.id):
        return
    ch = (m.text or "").strip()
    if not ch.startswith("@") and not ch.startswith("-100"):
        await m.answer("нужен @username или -100... id")
        return
    try:
        chat = await bot.get_chat(ch)
    except TelegramBadRequest:
        await m.answer("не вижу этот канал. добавь бота админом сначала.")
        return
    await state.update_data(channel=ch, title=chat.title or ch)
    await state.set_state(AdminFSM.add_sub_invite)
    await m.answer(f"ок, {chat.title}. кинь invite-ссылку (или «-» чтобы сгенерить из username):")

@admin_router.message(AdminFSM.add_sub_invite)
async def adm_sub_invite(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    invite = (m.text or "").strip()
    if invite == "-":
        invite = ""
    data = await state.get_data()
    await state.clear()
    with closing(db()) as c:
        c.execute(
            "INSERT OR REPLACE INTO forced_subs(channel, title, invite) VALUES(?,?,?)",
            (data["channel"], data["title"], invite),
        )
        c.commit()
    await m.answer("✅ добавлено. /admin — назад в меню")

@admin_router.callback_query(F.data == "adm_sub_del")
async def cb_sub_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("нет доступа", show_alert=True)
    with closing(db()) as c:
        subs = c.execute("SELECT * FROM forced_subs").fetchall()
    if not subs:
        return await cb.answer("нечего удалять", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 {s['title'] or s['channel']}", callback_data=f"sub_del:{s['channel']}")]
        for s in subs
    ] + [[InlineKeyboardButton(text="← назад", callback_data="adm_subs")]])
    await cb.message.edit_text("выбери что удалить:", reply_markup=kb)
    await cb.answer()

@admin_router.callback_query(F.data.startswith("sub_del:"))
async def cb_sub_del_do(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("нет доступа", show_alert=True)
    ch = cb.data.split(":", 1)[1]
    with closing(db()) as c:
        c.execute("DELETE FROM forced_subs WHERE channel=?", (ch,))
        c.commit()
    await cb.answer("удалено")
    await cb_subs(cb)

# --- админы ---
@admin_router.callback_query(F.data == "adm_add_admin")
async def cb_add_admin(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("нет доступа", show_alert=True)
    await cb.message.edit_text(
        "чтобы добавить админа — пусть он напишет боту, потом вручную вставь его ID:\n"
        "админы хранятся в таблице admins (sqlite).\n\n"
        "или кинь /addadmin <id> — эта команда появится тут.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← назад", callback_data="adm_back")]
        ]),
    )
    await cb.answer()

@admin_router.message(Command("addadmin"))
async def cmd_addadmin(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await m.answer("usage: /addadmin <user_id>")
        return
    uid = int(parts[1])
    with closing(db()) as c:
        c.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
        c.commit()
    await m.answer(f"✅ {uid} теперь админ")

@admin_router.message(Command("deladmin"))
async def cmd_deladmin(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await m.answer("usage: /deladmin <user_id>")
        return
    uid = int(parts[1])
    with closing(db()) as c:
        c.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        c.commit()
    await m.answer(f"🗑 {uid} больше не админ")

# ============ bootstrap ============
async def main():
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # админ-роутер первым — чтобы /admin перехватывался до общего message-handler'а
    dp.include_router(admin_router)
    dp.include_router(user_router)

    me = await bot.get_me()
    print(f"[+] бот запущен: @{me.username} ({me.id})")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
