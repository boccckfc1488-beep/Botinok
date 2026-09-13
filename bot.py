import asyncio
import html
import logging
import os
import random
import re
import secrets
import sqlite3
import time

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = "8501402161:AAGuBiYq4ATma9u4oMo8-bFGhkQ3dn6jnAQ"

ADMIN_USERNAME = "nazpab"
DB_NAME = "bot.db"

CAPTCHA_TTL = 10 * 60
CAPTCHA_ATTEMPTS = 3
BROADCAST_DELAY = 0.08


# ============================================================
# LOGGING / BOT
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

db = sqlite3.connect(DB_NAME, check_same_thread=False)
db.row_factory = sqlite3.Row


# ============================================================
# DATABASE
# ============================================================

def init_db():
    db.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL UNIQUE,
            username TEXT,
            title TEXT,
            link TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            from_chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            file_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_blocked INTEGER DEFAULT 0,
            captcha_passed INTEGER DEFAULT 0,
            files_received INTEGER DEFAULT 0
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS join_captchas (
            token TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            answer INTEGER NOT NULL,
            attempts INTEGER DEFAULT 0,
            expires_at INTEGER NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS file_downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_token TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    db.commit()


# ============================================================
# GENERAL HELPERS
# ============================================================

def esc(value):
    return html.escape(str(value or ""))


def is_admin_user(user) -> bool:
    username = getattr(user, "username", None)
    return bool(username and username.lower() == ADMIN_USERNAME.lower())


def is_admin(message: Message) -> bool:
    return is_admin_user(message.from_user)


def register_user(user):
    if not user:
        return

    db.execute("""
        INSERT INTO users (
            user_id, username, first_name, last_name
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_seen=CURRENT_TIMESTAMP
    """, (
        user.id,
        user.username,
        user.first_name,
        user.last_name,
    ))
    db.commit()


def blocked(user_id: int) -> bool:
    row = db.execute(
        "SELECT is_blocked FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return bool(row and row["is_blocked"])


def get_setting(key, default=""):
    row = db.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,),
    ).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db.execute("""
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    db.commit()


def captcha_enabled():
    return get_setting("captcha_enabled", "1") == "1"


# ============================================================
# ADMIN KEYBOARD
# ============================================================

def admin_keyboard():
    b = InlineKeyboardBuilder()

    b.row(
        InlineKeyboardButton(
            text="📁 Загрузить файл",
            callback_data="admin_upload",
        )
    )

    b.row(
        InlineKeyboardButton(
            text="📦 Управление файлами",
            callback_data="admin_files",
        )
    )

    b.row(
        InlineKeyboardButton(
            text="➕ Добавить канал",
            callback_data="admin_add_channel",
        ),
        InlineKeyboardButton(
            text="➖ Удалить канал",
            callback_data="admin_delete_channel",
        ),
    )

    b.row(
        InlineKeyboardButton(
            text="📋 Каналы",
            callback_data="admin_channels",
        )
    )

    b.row(
        InlineKeyboardButton(
            text="🤖 CAPTCHA",
            callback_data="admin_captcha",
        ),
        InlineKeyboardButton(
            text="📊 Статистика",
            callback_data="admin_stats",
        ),
    )

    b.row(
        InlineKeyboardButton(
            text="👥 Пользователи",
            callback_data="admin_users",
        ),
        InlineKeyboardButton(
            text="📣 Рассылка",
            callback_data="admin_broadcast",
        ),
    )

    return b.as_markup()


# ============================================================
# CHANNELS
# ============================================================

def get_channels():
    return db.execute(
        "SELECT * FROM channels ORDER BY id ASC"
    ).fetchall()


def extract_channel_username(text):
    text = text.strip()

    if text.startswith("@"):
        return text

    m = re.search(
        r"(?:https?://)?t\.me/([A-Za-z0-9_]+)",
        text,
    )

    if m:
        return "@" + m.group(1)

    if re.fullmatch(r"[A-Za-z0-9_]+", text):
        return "@" + text

    return None


async def check_subscription(user_id, chat_id):
    try:
        member = await bot.get_chat_member(
            chat_id=chat_id,
            user_id=user_id,
        )

        if member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }:
            return True

        if member.status == ChatMemberStatus.RESTRICTED:
            return bool(getattr(member, "is_member", False))

    except Exception as e:
        logging.warning(
            "Subscription check failed: %s",
            e,
        )

    return False


async def check_all_subscriptions(user_id):
    result = []

    for channel in get_channels():
        if not await check_subscription(
            user_id,
            channel["chat_id"],
        ):
            result.append(channel)

    return result


def subscription_keyboard(channels, token):
    b = InlineKeyboardBuilder()

    for channel in channels:
        b.row(
            InlineKeyboardButton(
                text=f"📢 {channel['title'] or 'Подписаться'}",
                url=channel["link"],
            )
        )

    b.row(
        InlineKeyboardButton(
            text="✅ Проверить подписку",
            callback_data=f"check_{token}",
        )
    )

    return b.as_markup()


# ============================================================
# FILES
# ============================================================

def create_file_token():
    while True:
        token = secrets.token_urlsafe(8)
        exists = db.execute(
            "SELECT id FROM files WHERE token=?",
            (token,),
        ).fetchone()

        if not exists:
            return token


def save_file(message):
    token = create_file_token()
    file_name = None

    if message.document:
        file_name = message.document.file_name
    elif message.video:
        file_name = message.video.file_name
    elif message.audio:
        file_name = message.audio.file_name

    db.execute("""
        INSERT INTO files(
            token,
            from_chat_id,
            message_id,
            file_name
        )
        VALUES (?, ?, ?, ?)
    """, (
        token,
        message.chat.id,
        message.message_id,
        file_name,
    ))

    db.commit()
    return token


def get_file(token):
    return db.execute(
        "SELECT * FROM files WHERE token=?",
        (token,),
    ).fetchone()


async def send_file_to_user(chat_id, file_data):
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=file_data["from_chat_id"],
            message_id=file_data["message_id"],
        )

        db.execute("""
            INSERT INTO file_downloads(
                user_id,
                file_token
            )
            VALUES (?, ?)
        """, (
            chat_id,
            file_data["token"],
        ))

        db.execute("""
            UPDATE users
            SET files_received=files_received+1
            WHERE user_id=?
        """, (chat_id,))

        db.commit()
        return True

    except Exception as e:
        logging.exception("File send error: %s", e)

        await bot.send_message(
            chat_id,
            "❌ Не удалось выдать файл.\n"
            "Возможно, исходное сообщение было удалено.",
        )
        return False


# ============================================================
# START / FILE DELIVERY
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    register_user(message.from_user)

    if blocked(message.from_user.id):
        await message.answer("🚫 Доступ ограничен.")
        return

    args = message.text.split(maxsplit=1)

    if len(args) == 1:
        if is_admin(message):
            await message.answer(
                "👑 <b>Админ-панель</b>\n\n"
                "Выберите действие:",
                reply_markup=admin_keyboard(),
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "👋 Привет!\n\n"
                "Откройте ссылку на нужный файл, чтобы получить его.",
            )
        return

    payload = args[1]

    if not payload.startswith("file_"):
        await message.answer("❌ Неверная ссылка.")
        return

    token = payload[5:]
    file_data = get_file(token)

    if not file_data:
        await message.answer(
            "❌ Файл не найден или ссылка недействительна."
        )
        return

    not_subscribed = await check_all_subscriptions(
        message.from_user.id
    )

    if not_subscribed:
        await message.answer(
            "🔒 <b>Сначала подпишитесь на все каналы.</b>\n\n"
            "После подписки нажмите кнопку проверки.",
            reply_markup=subscription_keyboard(
                not_subscribed,
                token,
            ),
            parse_mode="HTML",
        )
        return

    await send_file_to_user(
        message.chat.id,
        file_data,
    )


@dp.callback_query(F.data.startswith("check_"))
async def check_callback(callback: CallbackQuery):
    register_user(callback.from_user)

    if blocked(callback.from_user.id):
        await callback.answer(
            "🚫 Доступ ограничен.",
            show_alert=True,
        )
        return

    token = callback.data[6:]
    file_data = get_file(token)

    if not file_data:
        await callback.answer(
            "Файл не найден.",
            show_alert=True,
        )
        return

    not_subscribed = await check_all_subscriptions(
        callback.from_user.id
    )

    if not_subscribed:
        await callback.answer(
            "❌ Не все подписки выполнены.",
            show_alert=True,
        )

        try:
            await callback.message.edit_text(
                "🔒 <b>Подпишитесь на все каналы.</b>",
                reply_markup=subscription_keyboard(
                    not_subscribed,
                    token,
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        return

    await callback.answer("✅ Подписка подтверждена!")

    try:
        await callback.message.delete()
    except Exception:
        pass

    await send_file_to_user(
        callback.from_user.id,
        file_data,
    )


# ============================================================
# ADMIN FILE UPLOAD
# ============================================================

@dp.callback_query(F.data == "admin_upload")
async def admin_upload_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True,
        )
        return

    set_setting(
        f"admin_mode_{callback.from_user.id}",
        "upload",
    )

    await callback.message.answer(
        "📁 <b>Отправьте файл</b>\n\n"
        "Поддерживаются документы, видео, фото, аудио и GIF.",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(
    F.document |
    F.video |
    F.photo |
    F.audio |
    F.animation
)
async def admin_file_handler(message: Message):
    register_user(message.from_user)

    if not is_admin(message):
        return

    token = save_file(message)

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=file_{token}"

    await message.answer(
        "✅ <b>Файл сохранён!</b>\n\n"
        f"📄 {esc(message.document.file_name if message.document else 'Медиа')}\n\n"
        "🔗 <b>Ссылка:</b>\n"
        f"<code>{esc(link)}</code>",
        parse_mode="HTML",
    )


# ============================================================
# ADMIN FILE MANAGEMENT
# ============================================================

@dp.callback_query(F.data == "admin_files")
async def admin_files_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    rows = db.execute("""
        SELECT
            f.*,
            COUNT(d.id) AS downloads
        FROM files f
        LEFT JOIN file_downloads d
            ON d.file_token=f.token
        GROUP BY f.id
        ORDER BY f.id DESC
        LIMIT 20
    """).fetchall()

    if not rows:
        await callback.message.answer("📦 Файлов пока нет.")
        await callback.answer()
        return

    b = InlineKeyboardBuilder()
    text = "📦 <b>Файлы</b>\n\n"

    for i, row in enumerate(rows, 1):
        name = row["file_name"] or f"Файл #{row['id']}"
        text += (
            f"{i}. {esc(name)}\n"
            f"   📥 Выдач: {row['downloads']}\n"
            f"   🔑 <code>{esc(row['token'])}</code>\n\n"
        )

        b.row(
            InlineKeyboardButton(
                text=f"🗑 Удалить #{row['id']}",
                callback_data=f"delete_file_{row['id']}",
            )
        )

    await callback.message.answer(
        text,
        reply_markup=b.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("delete_file_"))
async def delete_file_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    try:
        file_id = int(callback.data.replace("delete_file_", ""))
    except ValueError:
        await callback.answer("Ошибка ID.", show_alert=True)
        return

    row = db.execute(
        "SELECT * FROM files WHERE id=?",
        (file_id,),
    ).fetchone()

    if not row:
        await callback.answer(
            "Файл уже удалён.",
            show_alert=True,
        )
        return

    db.execute(
        "DELETE FROM files WHERE id=?",
        (file_id,),
    )
    db.execute(
        "DELETE FROM file_downloads WHERE file_token=?",
        (row["token"],),
    )
    db.commit()

    await callback.answer("🗑 Файл удалён.")
    try:
        await callback.message.edit_text(
            f"🗑 Удалён файл: <b>{esc(row['file_name'] or row['token'])}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ============================================================
# ADMIN CHANNEL ADD
# ============================================================

@dp.callback_query(F.data == "admin_add_channel")
async def add_channel_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    set_setting(
        f"admin_mode_{callback.from_user.id}",
        "channel",
    )

    await callback.message.answer(
        "➕ <b>Добавление канала</b>\n\n"
        "Отправьте @username или https://t.me/username\n\n"
        "⚠️ Бот должен быть администратором канала.",
        parse_mode="HTML",
    )
    await callback.answer()


# ============================================================
# ADMIN TEXT INPUT
# ============================================================

@dp.message(F.text)
async def text_handler(message: Message):
    register_user(message.from_user)

    if not is_admin(message):
        return

    if message.text.startswith("/"):
        return

    mode_key = f"admin_mode_{message.from_user.id}"
    mode = get_setting(mode_key)

    if mode == "broadcast":
        await process_broadcast(message)
        return

    if mode != "channel":
        return

    text = message.text.strip()
    username = extract_channel_username(text)

    if not username:
        await message.answer(
            "❌ Не удалось распознать канал.\n"
            "Пример: @mychannel"
        )
        return

    try:
        chat = await bot.get_chat(username)
    except Exception:
        await message.answer(
            "❌ Канал не найден. Проверьте @username."
        )
        return

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(
            chat.id,
            me.id,
        )

        if member.status not in {
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }:
            await message.answer(
                "❌ Бот не является администратором этого канала."
            )
            return

    except Exception:
        await message.answer(
            "❌ Не удалось проверить права бота."
        )
        return

    exists = db.execute(
        "SELECT id FROM channels WHERE chat_id=?",
        (chat.id,),
    ).fetchone()

    if exists:
        await message.answer("⚠️ Канал уже добавлен.")
        return

    link = (
        f"https://t.me/{chat.username}"
        if chat.username
        else text
    )

    db.execute("""
        INSERT INTO channels(
            chat_id,
            username,
            title,
            link
        )
        VALUES (?, ?, ?, ?)
    """, (
        chat.id,
        chat.username,
        chat.title,
        link,
    ))

    db.commit()
    set_setting(mode_key, "")

    await message.answer(
        "✅ <b>Канал добавлен!</b>\n\n"
        f"📢 {esc(chat.title)}\n"
        f"🔗 {esc(link)}",
        parse_mode="HTML",
    )


# ============================================================
# CHANNEL LIST / DELETE
# ============================================================

@dp.callback_query(F.data == "admin_channels")
async def channels_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    channels = get_channels()

    if not channels:
        await callback.message.answer(
            "📋 Каналов пока нет."
        )
        await callback.answer()
        return

    text = "📋 <b>Обязательные каналы</b>\n\n"
    b = InlineKeyboardBuilder()

    for channel in channels:
        text += (
            f"📢 <b>{esc(channel['title'])}</b>\n"
            f"{esc(channel['link'])}\n\n"
        )
        b.row(
            InlineKeyboardButton(
                text=f"🗑 Удалить {channel['title'] or channel['id']}",
                callback_data=f"delete_channel_{channel['id']}",
            )
        )

    await callback.message.answer(
        text,
        reply_markup=b.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_delete_channel")
async def delete_channel_menu(callback: CallbackQuery):
    await channels_callback(callback)


@dp.callback_query(F.data.startswith("delete_channel_"))
async def delete_channel_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    try:
        channel_id = int(
            callback.data.replace("delete_channel_", "")
        )
    except ValueError:
        await callback.answer("Ошибка ID.", show_alert=True)
        return

    row = db.execute(
        "SELECT * FROM channels WHERE id=?",
        (channel_id,),
    ).fetchone()

    if not row:
        await callback.answer(
            "Канал уже удалён.",
            show_alert=True,
        )
        return

    db.execute(
        "DELETE FROM channels WHERE id=?",
        (channel_id,),
    )
    db.commit()

    await callback.answer("🗑 Канал удалён.")

    try:
        await callback.message.edit_text(
            f"🗑 Удалён канал: <b>{esc(row['title'] or row['link'])}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ============================================================
# CAPTCHA JOIN REQUEST
# ============================================================

def make_captcha():
    a = random.randint(2, 20)
    b = random.randint(2, 20)

    if random.choice([True, False]):
        answer = a + b
        question = f"{a} + {b}"
    else:
        if b > a:
            a, b = b, a
        answer = a - b
        question = f"{a} − {b}"

    options = {answer}

    while len(options) < 4:
        n = answer + random.randint(-4, 4)
        if n >= 0:
            options.add(n)

    options = list(options)
    random.shuffle(options)

    return question, answer, options


def captcha_keyboard(token, options):
    b = InlineKeyboardBuilder()

    for value in options:
        b.button(
            text=str(value),
            callback_data=f"captcha_{token}_{value}",
        )

    b.adjust(2)
    return b.as_markup()


@dp.chat_join_request()
async def join_request_handler(request: ChatJoinRequest):
    user = request.from_user
    register_user(user)

    if blocked(user.id):
        return

    if not captcha_enabled():
        try:
            await bot.approve_chat_join_request(
                chat_id=request.chat.id,
                user_id=user.id,
            )
            await bot.send_message(
                user.id,
                "✅ Ваша заявка принята автоматически.",
            )
        except Exception as e:
            logging.exception(
                "Auto approve error: %s",
                e,
            )
        return

    question, answer, options = make_captcha()
    token = secrets.token_urlsafe(8)

    db.execute(
        "DELETE FROM join_captchas WHERE user_id=?",
        (user.id,),
    )

    db.execute("""
        INSERT INTO join_captchas(
            token,
            chat_id,
            user_id,
            answer,
            attempts,
            expires_at
        )
        VALUES (?, ?, ?, ?, 0, ?)
    """, (
        token,
        request.chat.id,
        user.id,
        answer,
        int(time.time()) + CAPTCHA_TTL,
    ))

    db.commit()

    try:
        await bot.send_message(
            user.id,
            "🛡️ <b>CAPTCHA</b>\n\n"
            f"Решите пример: <b>{esc(question)}</b>\n\n"
            "После правильного ответа заявка будет "
            "принята автоматически.\n\n"
            "⏱ CAPTCHA действует 10 минут.",
            reply_markup=captcha_keyboard(
                token,
                options,
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logging.warning(
            "Can't send CAPTCHA to %s: %s",
            user.id,
            e,
        )


@dp.callback_query(F.data.startswith("captcha_"))
async def captcha_callback(callback: CallbackQuery):
    register_user(callback.from_user)

    parts = callback.data.split("_", 2)

    if len(parts) != 3:
        await callback.answer(
            "Ошибка CAPTCHA.",
            show_alert=True,
        )
        return

    token = parts[1]

    try:
        selected = int(parts[2])
    except ValueError:
        await callback.answer(
            "Ошибка ответа.",
            show_alert=True,
        )
        return

    row = db.execute(
        "SELECT * FROM join_captchas WHERE token=?",
        (token,),
    ).fetchone()

    if not row:
        await callback.answer(
            "❌ CAPTCHA не найдена или истекла.",
            show_alert=True,
        )
        return

    if row["user_id"] != callback.from_user.id:
        await callback.answer(
            "❌ Эта CAPTCHA не ваша.",
            show_alert=True,
        )
        return

    if row["expires_at"] < int(time.time()):
        db.execute(
            "DELETE FROM join_captchas WHERE token=?",
            (token,),
        )
        db.commit()

        await callback.answer(
            "⏰ CAPTCHA истекла.",
            show_alert=True,
        )
        return

    if selected != row["answer"]:
        attempts = row["attempts"] + 1

        if attempts >= CAPTCHA_ATTEMPTS:
            db.execute(
                "DELETE FROM join_captchas WHERE token=?",
                (token,),
            )
            db.commit()

            await callback.answer(
                "🚫 Лимит попыток исчерпан.",
                show_alert=True,
            )

            try:
                await callback.message.edit_text(
                    "🚫 CAPTCHA не пройдена.\n\n"
                    "Создайте новую заявку.",
                )
            except Exception:
                pass

            return

        db.execute(
            "UPDATE join_captchas SET attempts=? WHERE token=?",
            (attempts, token),
        )
        db.commit()

        await callback.answer(
            f"❌ Неверно. Осталось: {CAPTCHA_ATTEMPTS - attempts}",
            show_alert=True,
        )
        return

    try:
        await bot.approve_chat_join_request(
            chat_id=row["chat_id"],
            user_id=row["user_id"],
        )

        db.execute(
            "DELETE FROM join_captchas WHERE token=?",
            (token,),
        )

        db.execute("""
            UPDATE users
            SET captcha_passed=captcha_passed+1
            WHERE user_id=?
        """, (callback.from_user.id,))

        db.commit()

        await callback.answer("✅ CAPTCHA пройдена!")

        try:
            await callback.message.edit_text(
                "✅ <b>Проверка пройдена!</b>\n\n"
                "Заявка автоматически принята.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    except Exception as e:
        logging.exception(
            "Approve join request error: %s",
            e,
        )

        await callback.answer(
            "❌ Не удалось принять заявку. "
            "Проверьте права бота.",
            show_alert=True,
        )


# ============================================================
# CAPTCHA SETTINGS
# ============================================================

@dp.callback_query(F.data == "admin_captcha")
async def admin_captcha_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    status = "🟢 ВКЛЮЧЕНА" if captcha_enabled() else "🔴 ВЫКЛЮЧЕНА"

    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="🟢 Включить",
            callback_data="captcha_on",
        ),
        InlineKeyboardButton(
            text="🔴 Выключить",
            callback_data="captcha_off",
        ),
    )

    await callback.message.answer(
        "🤖 <b>CAPTCHA заявок</b>\n\n"
        f"Статус: <b>{status}</b>\n\n"
        "CAPTCHA срабатывает на заявки "
        "на вступление, если Telegram присылает "
        "боту ChatJoinRequest.",
        reply_markup=b.as_markup(),
        parse_mode="HTML",
    )

    await callback.answer()


@dp.callback_query(F.data == "captcha_on")
async def captcha_on(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    set_setting("captcha_enabled", "1")
    await callback.answer("🟢 CAPTCHA включена!")
    await callback.message.answer("🤖 CAPTCHA включена.")


@dp.callback_query(F.data == "captcha_off")
async def captcha_off(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    set_setting("captcha_enabled", "0")
    await callback.answer("🔴 CAPTCHA выключена!")
    await callback.message.answer("🤖 CAPTCHA выключена.")


# ============================================================
# STATISTICS
# ============================================================

@dp.callback_query(F.data == "admin_stats")
async def stats_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    users = db.execute(
        "SELECT COUNT(*) c FROM users"
    ).fetchone()["c"]

    blocked_count = db.execute(
        "SELECT COUNT(*) c FROM users WHERE is_blocked=1"
    ).fetchone()["c"]

    files = db.execute(
        "SELECT COUNT(*) c FROM files"
    ).fetchone()["c"]

    downloads = db.execute(
        "SELECT COUNT(*) c FROM file_downloads"
    ).fetchone()["c"]

    captcha = db.execute(
        "SELECT COALESCE(SUM(captcha_passed),0) c FROM users"
    ).fetchone()["c"]

    channels = db.execute(
        "SELECT COUNT(*) c FROM channels"
    ).fetchone()["c"]

    await callback.message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователи: <b>{users}</b>\n"
        f"🚫 Заблокировано: <b>{blocked_count}</b>\n"
        f"📁 Файлы: <b>{files}</b>\n"
        f"📥 Выдачи: <b>{downloads}</b>\n"
        f"🛡 CAPTCHA пройдено: <b>{captcha}</b>\n"
        f"📢 Каналов: <b>{channels}</b>",
        parse_mode="HTML",
    )
    await callback.answer()


# ============================================================
# USERS / BAN
# ============================================================

@dp.callback_query(F.data == "admin_users")
async def users_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    rows = db.execute("""
        SELECT *
        FROM users
        ORDER BY last_seen DESC
        LIMIT 15
    """).fetchall()

    if not rows:
        await callback.message.answer("👥 Пользователей нет.")
        await callback.answer()
        return

    text = "👥 <b>Пользователи</b>\n\n"
    b = InlineKeyboardBuilder()

    for row in rows:
        name = (
            "@" + row["username"]
            if row["username"]
            else row["first_name"] or str(row["user_id"])
        )

        text += (
            f"{'🚫' if row['is_blocked'] else '🟢'} "
            f"<b>{esc(name)}</b> — "
            f"<code>{row['user_id']}</code>\n"
            f"📥 Файлов: {row['files_received']}\n\n"
        )

        b.row(
            InlineKeyboardButton(
                text=(
                    f"✅ Разблокировать {row['user_id']}"
                    if row["is_blocked"]
                    else f"🚫 Заблокировать {row['user_id']}"
                ),
                callback_data=f"toggle_block_{row['user_id']}",
            )
        )

    await callback.message.answer(
        text,
        reply_markup=b.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle_block_"))
async def toggle_block(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    try:
        user_id = int(
            callback.data.replace("toggle_block_", "")
        )
    except ValueError:
        await callback.answer("Ошибка ID.", show_alert=True)
        return

    row = db.execute(
        "SELECT is_blocked FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()

    if not row:
        await callback.answer(
            "Пользователь не найден.",
            show_alert=True,
        )
        return

    new_value = 0 if row["is_blocked"] else 1

    db.execute(
        "UPDATE users SET is_blocked=? WHERE user_id=?",
        (new_value, user_id),
    )
    db
