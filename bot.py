import asyncio
import html
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = "8501402161:AAGuBiYq4ATma9u4oMo8-bFGhkQ3dn6jnAQ"

ADMIN_USERNAME = "nazpab"
DB_NAME = "bot.db"

CAPTCHA_TTL = 10 * 60          # 10 минут
CAPTCHA_ATTEMPTS = 3           # попытки ответа
DOWNLOAD_COOLDOWN = 2          # защита от спама выдачей
BROADCAST_DELAY = 0.05


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DB_NAME, check_same_thread=False)
db.row_factory = sqlite3.Row


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

    db.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        )
    """)

    columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(users)").fetchall()
    }
    if "is_blocked" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
    if "captcha_passed" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN captcha_passed INTEGER DEFAULT 0")
    if "files_received" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN files_received INTEGER DEFAULT 0")

    db.commit()


# ============================================================
# BOT / DISPATCHER
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# HELPERS
# ============================================================

def is_admin(message: Message) -> bool:
    username = message.from_user.username if message.from_user else None
    return bool(
        username and username.lower() == ADMIN_USERNAME.lower()
    )


def is_admin_user(user) -> bool:
    username = getattr(user, "username", None)
    return bool(username and username.lower() == ADMIN_USERNAME.lower())


def esc(value) -> str:
    return html.escape(str(value or ""))


def get_setting(key: str, default: str = "") -> str:
    row = db.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,),
    ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    db.execute(
        """
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )
    db.commit()


def captcha_enabled() -> bool:
    return get_setting("captcha_enabled", "1") == "1"


def register_user(user):
    if not user:
        return

    db.execute(
        """
        INSERT INTO users (
            user_id, username, first_name, last_name
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_seen=CURRENT_TIMESTAMP
        """,
        (
            user.id,
            user.username,
            user.first_name,
            user.last_name,
        ),
    )
    db.commit()


def user_is_blocked(user_id: int) -> bool:
    row = db.execute(
        "SELECT is_blocked FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return bool(row and row["is_blocked"])


def mark_file_received(user_id: int, token: str):
    db.execute(
        """
        UPDATE users
        SET files_received = files_received + 1,
            last_seen = CURRENT_TIMESTAMP
        WHERE user_id = ?
        """,
        (user_id,),
    )
    db.execute(
        """
        INSERT INTO file_downloads(user_id, file_token)
        VALUES (?, ?)
        """,
        (user_id, token),
    )
    db.commit()


# ============================================================
# ADMIN MENU
# ============================================================

def admin_keyboard():
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="📁 Загрузить файл",
            callback_data="admin_upload",
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text="➕ Добавить канал",
            callback_data="admin_add_channel",
        ),
        InlineKeyboardButton(
            text="➖ Удалить канал",
            callback_data="admin_delete_channel",
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text="📋 Список каналов",
            callback_data="admin_channels",
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text="🤖 CAPTCHA заявок",
            callback_data="admin_captcha",
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text="📊 Статистика",
            callback_data="admin_stats",
        ),
        InlineKeyboardButton(
            text="👥 Пользователи",
            callback_data="admin_users",
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text="📣 Рассылка",
            callback_data="admin_broadcast",
        ),
    )

    return builder.as_markup()


# ============================================================
# CHANNEL HELPERS
# ============================================================

def get_channels():
    return db.execute(
        "SELECT * FROM channels ORDER BY id ASC"
    ).fetchall()


def extract_channel_username(text: str):
    text = text.strip()

    if text.startswith("@"):
        return text

    match = re.search(
        r"(?:https?://)?t\.me/([A-Za-z0-9_]+)",
        text,
    )

    if match:
        return "@" + match.group(1)

    if re.fullmatch(r"[A-Za-z0-9_]+", text):
        return "@" + text

    return None


async def check_subscription(user_id: int, chat_id: int) -> bool:
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

        return False

    except Exception as e:
        logging.warning(
            "Не удалось проверить подписку %s в %s: %s",
            user_id,
            chat_id,
            e,
        )
        return False


async def check_all_subscriptions(user_id: int):
    channels = get_channels()
    not_subscribed = []

    for channel in channels:
        if not await check_subscription(
            user_id,
            channel["chat_id"],
        ):
            not_subscribed.append(channel)

    return not_subscribed


def subscription_keyboard(channels, token):
    builder = InlineKeyboardBuilder()

    for channel in channels:
        builder.row(
            InlineKeyboardButton(
                text=f"📢 {channel['title'] or 'Подписаться'}",
                url=channel["link"],
            ),
        )

    builder.row(
        InlineKeyboardButton(
            text="✅ Я подписался — проверить",
            callback_data=f"check_{token}",
        ),
    )

    return builder.as_markup()


# ============================================================
# FILE HELPERS
# ============================================================

def create_file_token():
    while True:
        token = secrets.token_urlsafe(8)

        exists = db.execute(
            "SELECT id FROM files WHERE token = ?",
            (token,),
        ).fetchone()

        if not exists:
            return token


def save_file(message: Message):
    token = create_file_token()
    file_name = None

    if message.document:
        file_name = message.document.file_name
    elif message.video:
        file_name = message.video.file_name
    elif message.audio:
        file_name = message.audio.file_name

    db.execute(
        """
        INSERT INTO files (
            token,
            from_chat_id,
            message_id,
            file_name
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            token,
            message.chat.id,
            message.message_id,
            file_name,
        ),
    )

    db.commit()
    return token


def get_file(token):
    return db.execute(
        "SELECT * FROM files WHERE token = ?",
        (token,),
    ).fetchone()


# ============================================================
# SEND FILE
# ============================================================

async def send_file_to_user(chat_id: int, file_data):
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=file_data["from_chat_id"],
            message_id=file_data["message_id"],
        )

        mark_file_received(chat_id, file_data["token"])
        return True

    except Exception as e:
        logging.exception("Ошибка отправки файла: %s", e)

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

    if user_is_blocked(message.from_user.id):
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
                "Чтобы получить файл, откройте ссылку на нужный файл.",
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
            "❌ Этот файл не найден или ссылка недействительна.",
        )
        return

    channels = get_channels()

    if channels:
        not_subscribed = await check_all_subscriptions(
            message.from_user.id,
        )

        if not_subscribed:
            await message.answer(
                "🔒 <b>Чтобы получить файл, подпишитесь на все каналы:</b>\n\n"
                "После подписки нажмите кнопку "
                "«Я подписался — проверить».",
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


# ============================================================
# CHECK SUBSCRIPTION
# ============================================================

@dp.callback_query(F.data.startswith("check_"))
async def check_subscription_callback(callback: CallbackQuery):
    register_user(callback.from_user)

    token = callback.data[6:]
    file_data = get_file(token)

    if not file_data:
        await callback.answer(
            "Файл не найден.",
            show_alert=True,
        )
        return

    if user_is_blocked(callback.from_user.id):
        await callback.answer(
            "🚫 Доступ ограничен.",
            show_alert=True,
        )
        return

    not_subscribed = await check_all_subscriptions(
        callback.from_user.id,
    )

    if not_subscribed:
        await callback.answer(
            "❌ Вы ещё не подписались на все каналы.",
            show_alert=True,
        )

        try:
            await callback.message.edit_text(
                "🔒 <b>Вы ещё не подписались на все каналы.</b>\n\n"
                "Подпишитесь на каналы ниже и снова нажмите "
                "«Я подписался — проверить».",
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
# ADMIN: UPLOAD
# ============================================================

@dp.callback_query(F.data == "admin_upload")
async def admin_upload_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        "📁 <b>Отправьте мне файл</b>\n\n"
        "Можно отправить документ, видео, фото, аудио или анимацию.\n\n"
        "После загрузки я создам ссылку.",
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
        "🔗 <b>Ссылка на файл:</b>\n"
        f"<code>{esc(link)}</code>\n\n"
        "Теперь отправляйте эту ссылку пользователям.",
        parse_mode="HTML",
    )


# ============================================================
# ADMIN: ADD CHANNEL
# ============================================================

@dp.callback_query(F.data == "admin_add_channel")
async def admin_add_channel_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        "➕ <b>Добавление канала</b>\n\n"
        "Отправьте ссылку на публичный канал.\n\n"
        "Например:\n"
        "<code>@mychannel</code>\n"
        "<code>https://t.me/mychannel</code>\n\n"
        "⚠️ Бот должен быть администратором этого канала.",
        parse_mode="HTML",
    )
    await callback.answer()


# ============================================================
# ADMIN: TEXT INPUT
# ============================================================

@dp.message(F.text)
async def text_handler(message: Message):
    register_user(message.from_user)

    if not is_admin(message):
        return

    text = message.text.strip()

    if text.startswith("/"):
        return

    mode = get_setting(f"admin_mode_{message.from_user.id}")

    if mode == "broadcast":
        await process_broadcast(message)
        return

    username = extract_channel_username(text)

    if username:
        try:
            chat = await bot.get_chat(username)
        except Exception:
            await message.answer(
                "❌ Не удалось найти этот канал.\n\n"
                "Проверьте ссылку и убедитесь, что канал публичный.",
            )
            return

        try:
            me = await bot.get_me()
            bot_member = await bot.get_chat_member(
                chat.id,
                me.id,
            )

            if bot_member.status not in {
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            }:
                await message.answer(
                    "❌ Бот не является администратором этого канала.\n\n"
                    "Сначала добавьте бота в канал и выдайте ему "
                    "права администратора.",
                )
                return

        except Exception as e:
            logging.warning("Ошибка проверки прав: %s", e)
            await message.answer(
                "❌ Не удалось проверить права бота в канале.",
            )
            return

        exists = db.execute(
            "SELECT id FROM channels WHERE chat_id = ?",
            (chat.id,),
        ).fetchone()

        if exists:
            await message.answer("⚠️ Этот канал уже добавлен.")
            return

        link = (
            f"https://t.me/{chat.username}"
            if chat.username
            else text
        )

        db.execute(
            """
            INSERT INTO channels (
                chat_id,
                username,
                title,
                link
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                chat.id,
                chat.username,
                chat.title,
                link,
            ),
        )
        db.commit()

        await message.answer(
            "✅ <b>Канал добавлен!</b>\n\n"
            f"📢 {esc(chat.title)}\n"
            f"🔗 {esc(link)}",
            parse_mode="HTML",
        )


# ============================================================
# ADMIN: CHANNEL LIST
# ============================================================

@dp.callback_query(F.data == "admin_channels")
async def admin_channels_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True,
        )
        return

    channels = get_channels()

    if not channels:
        await callback.message.answer(
            "📋 <b>Список каналов пуст.</b>",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "📋 <b>Каналы для подписки:</b>\n\n"

    for i, channel in enumerate(channels, 1):
        text += (
            f"{i}. {esc(channel['title'] or 'Без названия')}\n"
            f"   {esc(channel['link'])}\n\n"
        )

    await callback.message.answer(
        text,
        parse_mode="HTML",
    )
    await callback.answer()


# ============================================================
# ADMIN: DELETE CHANNEL
# ============================================================

@dp.callback_query(F.data == "admin_delete_channel")
async def admin_delete_channel_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True,
        )
        return

    channels = get_channels()

    if not channels:
        await callback.message.answer("📋 Список каналов пуст.")
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()

    for channel in channels:
        builder.row(
            InlineKeyboardButton(
                text=f"❌ {channel['title'] or channel['link']}",
                callback_data=f"delete_channel_{channel['id']}",
            ),
        )

    await callback.message.answer(
        "➖ <b>Выберите канал для удаления:</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("delete_channel_"))
async def delete_channel_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True,
        )
        return

    try:
        channel_id = int(
            callback.data.replace("delete_channel_", ""),
        )
    except ValueError:
        await callback.answer("Некорректный ID.", show_alert=True)
        return

    channel = db.execute(
        "SELECT * FROM channels WHERE id = ?",
        (channel_id,),
    ).fetchone()

    if not channel:
        await callback.answer(
            "Канал уже удалён.",
            show_alert=True,
        )
        return

    db.execute(
        "DELETE FROM channels WHERE id = ?",
        (channel_id,),
    )
    db.commit()

    await callback.message.edit_text(
        "✅ Канал удалён:\n\n"
        f"📢 {esc(channel['title'] or channel['link'])}",
        parse_mode="HTML",
    )
    await callback.answer("Удалено!")


# ============================================================
# CAPTCHA: JOIN REQUESTS
# ============================================================

def make_math_captcha():
    a = random.randint(2, 20)
    b = random.randint(2, 20)
    operation = random.choice(["+", "-"])

    if operation == "+":
        answer = a + b
        question = f"{a} + {b}"
    else:
        if b > a:
            a, b = b, a
        answer = a - b
        question = f"{a} − {b}"

    options = {answer}
    while len(options) < 4:
        delta = random.choice([-3, -2, -1, 1, 2, 3])
        value = answer + delta
        if value >= 0:
            options.add(value)

    options = list(options)
    random.shuffle(options)
    return question, answer, options


def captcha_keyboard(token: str, options):
    builder = InlineKeyboardBuilder()

    for value in options:
        builder.button(
            text=str(value),
            callback_data=f"jc_{token}_{value}",
        )

    builder.adjust(2)
    return builder.as_markup()


@dp.chat_join_request()
async def join_request_handler(request: ChatJoinRequest):
    user = request.from_user
    register_user(user)

    if not captcha_enabled():
        try:
            await bot.approve_chat_join_request(
                chat_id=request.chat.id,
                user_id=user.id,
            )
            await bot.send_message(
                user.id,
                "✅ Ваша заявка принята.",
            )
        except Exception as e:
            logging.exception("Не удалось автоматически принять заявку: %s", e)
        return

    token = secrets.token_urlsafe(8)
    question, answer, options = make_math_captcha()

    db.execute(
        "DELETE FROM join_captchas WHERE user_id = ?",
        (user.id,),
    )

    db.execute(
        """
        INSERT INTO join_captchas(
            token, chat_id, user_id, answer, attempts, expires_at
        )
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (
            token,
            request.chat.id,
            user.id,
            answer,
            int(time.time()) + CAPTCHA_TTL,
        ),
    )
    db.commit()

    try:
        await bot.send_message(
            user.id,
            "🛡️ <b>Проверка безопасности</b>\n\n"
            f"Решите пример: <b>{esc(question)}</b>\n\n"
            "Нажмите правильный ответ, чтобы заявка "
            "была принята автоматически.\n\n"
            f"⏱ Время: {CAPTCHA_TTL // 60} минут.",
            reply_markup=captcha_keyboard(token, options),
            parse_mode="HTML",
        )
    except Exception as e:
        logging.warning(
            "Не удалось отправить CAPTCHA пользователю %s: %s",
            user.id,
            e,
        )


@dp.callback_query(F.data.startswith("jc_"))
async def join_captcha_callback(callback: CallbackQuery):
    register_user(callback.from_user)

    parts = callback.data.split("_", 2)
    if len(parts) != 3:
        await callback.answer("Некорректная CAPTCHA.", show_alert=True)
        return

    token = parts[1]

    try:
        selected = int(parts[2])
    except ValueError:
        await callback.answer("Некорректный ответ.", show_alert=True)
        return

    captcha = db.execute(
        "SELECT * FROM join_captchas WHERE token = ?",
        (token,),
    ).fetchone()

    if not captcha:
        await callback.answer(
            "❌ CAPTCHA не найдена или уже использована.",
            show_alert=True,
        )
        return

    if captcha["user_id"] != callback.from_user.id:
        await callback.answer(
            "❌ Эта CAPTCHA предназначена для другого пользователя.",
            show_alert=True,
        )
        return

    if captcha["expires_at"] < int(time.time()):
        db.execute(
            "DELETE FROM join_captchas WHERE token = ?",
            (token,),
        )
        db.commit()
        await callback.answer(
            "⏰ CAPTCHA истекла. Отправьте заявку заново.",
            show_alert=True,
        )
        return

    if selected != captcha["answer"]:
        attempts = captcha["attempts"] + 1

        if attempts >= CAPTCHA_ATTEMPTS:
            db.execute(
                "DELETE FROM join_captchas WHERE token = ?",
                (token,),
            )
            db.commit()

            await callback.answer(
                "🚫 Слишком много ошибок. Заявка не принята.",
                show_alert=True,
            )

            try:
                await callback.message.edit_text(
                    "🚫 Проверка не пройдена.\n"
                    "Создайте новую заявку и попробуйте снова.",
                )
            except Exception:
                pass
            return

        db.execute(
            """
            UPDATE join_captchas
            SET attempts = ?
            WHERE token = ?
            """,
            (attempts, token),
        )
        db.commit()

        left = CAPTCHA_ATTEMPTS - attempts
        await callback.answer(
            f"❌ Неверно. Осталось попыток: {left}",
            show_alert=True,
        )
        return

    try:
        await bot.approve_chat_join_request(
            chat_id=captcha["chat_id"],
            user_id=captcha["user_id"],
        )

        db.execute(
            "DELETE FROM join_captchas WHERE token = ?",
            (token,),
        )
        db.execute(
            """
            UPDATE users
            SET captcha_passed = captcha_passed + 1
            WHERE user_id = ?
            """,
            (callback.from_user.id,),
        )
        db.commit()

        await callback.answer("✅ CAPTCHA пройдена!")

        try:
            await callback.message.edit_text(
                "✅ <b>Проверка пройдена!</b>\n\n"
                "Ваша заявка автоматически принята.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    except Exception as e:
        logging.exception(
            "Не удалось принять заявку %s в %s: %s",
            captcha["user_id"],
            captcha["chat_id"],
            e,
        )
        await callback.answer(
            "❌ Не удалось принять заявку. "
            "Проверьте права бота.",
            show_alert=True,
        )


# ============================================================
# ADMIN: CAPTCHA SETTINGS
# ============================================================

@dp.callback_query(F.data == "admin_captcha")
async def admin_captcha_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True,
        )
        return

    status = "🟢 ВКЛЮЧЕНА" if captcha_enabled() else "🔴 ВЫКЛЮЧЕНА"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🟢 Включить",
            callback_data="captcha_on",
        ),
        InlineKeyboardButton(
            text="🔴 Выключить",
            callback_data="captcha_off",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data="admin_back",
        ),
    )

    await callback.message.answer(
        "🤖 <b>CAPTCHA заявок</b>\n\n"
        f"Статус: <b>{status}</b>\n\n"
        "Когда пользователь отправляет заявку на вступление "
        "в чат/канал через join-request ссылку, бот отправляет "
        "математическую CAPTCHA. После правильного ответа "
        "заявка принимается автоматически.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "captcha_on")
async def captcha_on_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    set_setting("captcha_enabled", "1")
    await callback.answer("✅ CAPTCHA включена!")
    await callback.message.answer("🤖 CAPTCHA для заявок включена.")


@dp.callback_query(F.data == "captcha_off")
async def captcha_off_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    set_setting("captcha_enabled", "0")
    await callback.answer("🔴 CAPTCHA выключена.")
    await callback.message.answer("🤖 CAPTCHA для заявок выключена.")


# ============================================================
# ADMIN: STATS
# ============================================================

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    users = db.execute(
        "SELECT COUNT(*) AS c FROM users"
    ).fetchone()["c"]

    blocked = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE is_blocked = 1"
    ).fetchone()["c"]

    files = db.execute(
        "SELECT COUNT(*) AS c FROM files"
    ).fetchone()["c"]

    downloads = db.execute(
        "SELECT COUNT(*) AS c FROM file_downloads"
    ).fetchone()["c"]

    captcha_passed = db.execute(
        "SELECT COALESCE(SUM(captcha_passed), 0) AS c FROM users"
    ).fetchone()["c"]

    channels = db.execute(
        "SELECT COUNT(*) AS c FROM channels"
    ).fetchone()["c"]

    await callback.message.answer(
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"🚫 Заблокировано: <b>{blocked}</b>\n"
        f"📁 Файлов: <b>{files}</b>\n"
        f"📥 Выдач файлов: <b>{downloads}</b>\n"
        f"🛡️ Пройдено CAPTCHA: <b>{captcha_passed}</b>\n"
        f"📢 Каналов: <b>{channels}</b>",
        parse_mode="HTML",
    )
    await callback.answer()


# ============================================================
# ADMIN: USERS / BLOCK
# ============================================================

@dp.callback_query(F.data == "admin_users")
async def admin_users_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user):
        await callback.answer("❌ Доступ запрещён.", show_alert=True)
        return

    rows = db.execute(
        """
        SELECT *
        FROM users
        ORDER BY last_seen DESC
        LIMIT 10
        """
    ).fetchall()

    if not rows:
        await callback.message.answer("👥 Пользователей пока нет.")
        await callback.answer()
        return

    text = "👥 <b>Последние пользователи</b>\n\n"
    builder = InlineKeyboardBuilder()

    for row in rows:
        name = (
            f"@{row['username']}"
            if row["username"]
            else row["first_name"] or str(row["user_id"])
        )
        status = "🚫" if row["is_blocked"] else "🟢"

        text += (
            f"{status} <b>{esc(name)}</b>\n"
            f"ID: <code>{row['user_id']}</code>\n"
            f"Файлов: {row['files_received']}\n\n"
        )

        if row["is_blocked"]:
            button_text = f"✅ Разблокировать {row['user_id']}"
        else:
            button_text = f"🚫 Заблокировать {row['user_id']}"

        builder.row(
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"toggle_block_{row['user_id']}",
            )
        )

    await callback.message.answer(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
  )
