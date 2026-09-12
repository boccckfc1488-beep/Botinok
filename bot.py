import asyncio
import logging
import os
import re
import secrets
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8632883159:AAH8ApaMDa8sBBypTLSvSc4UxkkBA9pUNzw")

ADMIN_USERNAME = "nazpab"

DB_NAME = "bot.db"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DB_NAME)
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

    db.commit()


# ============================================================
# BOT / DP
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# ADMIN HELPERS
# ============================================================

def is_admin(message: Message) -> bool:
    username = message.from_user.username

    return (
        username is not None
        and username.lower() == ADMIN_USERNAME.lower()
    )


def admin_keyboard():
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="📁 Загрузить файл",
            callback_data="admin_upload"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="➕ Добавить канал",
            callback_data="admin_add_channel"
        ),
        InlineKeyboardButton(
            text="➖ Удалить канал",
            callback_data="admin_delete_channel"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📋 Список каналов",
            callback_data="admin_channels"
        )
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
    """
    Поддерживает:

    @mychannel
    https://t.me/mychannel
    t.me/mychannel
    mychannel
    """

    text = text.strip()

    if text.startswith("@"):
        return text

    match = re.search(
        r"(?:https?://)?t\.me/([A-Za-z0-9_]+)",
        text
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
            user_id=user_id
        )

        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR
        }

    except Exception as e:
        logging.warning(
            "Не удалось проверить подписку %s в %s: %s",
            user_id,
            chat_id,
            e
        )

        return False


async def check_all_subscriptions(user_id: int):
    channels = get_channels()

    not_subscribed = []

    for channel in channels:
        subscribed = await check_subscription(
            user_id,
            channel["chat_id"]
        )

        if not subscribed:
            not_subscribed.append(channel)

    return not_subscribed


def subscription_keyboard(channels, token):
    builder = InlineKeyboardBuilder()

    for channel in channels:
        link = channel["link"]

        builder.row(
            InlineKeyboardButton(
                text=f"📢 {channel['title'] or 'Подписаться'}",
                url=link
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="✅ Я подписался — проверить",
            callback_data=f"check_{token}"
        )
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
            (token,)
        ).fetchone()

        if not exists:
            return token


def save_file(message: Message):
    token = create_file_token()

    file_name = None

    if message.document:
        file_name = message.document.file_name

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
            file_name
        )
    )

    db.commit()

    return token


def get_file(token):
    return db.execute(
        "SELECT * FROM files WHERE token = ?",
        (token,)
    ).fetchone()


# ============================================================
# /start
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    args = message.text.split(maxsplit=1)

    # Обычный /start
    if len(args) == 1:
        if is_admin(message):
            await message.answer(
                "👑 <b>Админ-панель</b>\n\n"
                "Выберите действие:",
                reply_markup=admin_keyboard(),
                parse_mode="HTML"
            )
        else:
            await message.answer(
                "👋 Привет!\n\n"
                "Чтобы получить файл, откройте ссылку на нужный файл."
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
            "❌ Этот файл не найден или ссылка недействительна."
        )
        return

    channels = get_channels()

    # Если каналов нет — сразу выдаём файл
    if not channels:
        await send_file_to_user(
            message.chat.id,
            file_data
        )
        return

    not_subscribed = await check_all_subscriptions(
        message.from_user.id
    )

    if not_subscribed:
        await message.answer(
            "🔒 <b>Чтобы получить файл, подпишитесь на все каналы:</b>\n\n"
            "После подписки нажмите кнопку "
            "«Я подписался — проверить».",
            reply_markup=subscription_keyboard(
                not_subscribed,
                token
            ),
            parse_mode="HTML"
        )
        return

    await send_file_to_user(
        message.chat.id,
        file_data
    )


# ============================================================
# SEND FILE
# ============================================================

async def send_file_to_user(chat_id: int, file_data):
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=file_data["from_chat_id"],
            message_id=file_data["message_id"]
        )

    except Exception as e:
        logging.exception("Ошибка отправки файла: %s", e)

        await bot.send_message(
            chat_id,
            "❌ Не удалось выдать файл.\n"
            "Возможно, исходное сообщение было удалено."
        )


# ============================================================
# CHECK SUBSCRIPTION BUTTON
# ============================================================

@dp.callback_query(F.data.startswith("check_"))
async def check_subscription_callback(
    callback: CallbackQuery
):
    token = callback.data[6:]

    file_data = get_file(token)

    if not file_data:
        await callback.answer(
            "Файл не найден.",
            show_alert=True
        )
        return

    not_subscribed = await check_all_subscriptions(
        callback.from_user.id
    )

    if not_subscribed:
        await callback.answer(
            "❌ Вы ещё не подписались на все каналы.",
            show_alert=True
        )

        try:
            await callback.message.edit_text(
                "🔒 <b>Вы ещё не подписались на все каналы.</b>\n\n"
                "Подпишитесь на каналы ниже и снова нажмите "
                "«Я подписался — проверить».",
                reply_markup=subscription_keyboard(
                    not_subscribed,
                    token
                ),
                parse_mode="HTML"
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
        file_data
    )


# ============================================================
# ADMIN MENU
# ============================================================

@dp.callback_query(F.data == "admin_upload")
async def admin_upload_callback(callback: CallbackQuery):
    if callback.from_user.username is None:
        await callback.answer(
            "Не удалось определить администратора.",
            show_alert=True
        )
        return

    if callback.from_user.username.lower() != ADMIN_USERNAME.lower():
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True
        )
        return

    await callback.message.answer(
        "📁 <b>Отправьте мне файл</b>\n\n"
        "Можно отправить документ, видео, фото, аудио или другой "
        "поддерживаемый Telegram-файл.\n\n"
        "После загрузки я создам ссылку.",
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# ADMIN FILE UPLOAD
# ============================================================

@dp.message(
    F.document |
    F.video |
    F.photo |
    F.audio |
    F.animation
)
async def admin_file_handler(message: Message):
    if not is_admin(message):
        return

    token = save_file(message)

    me = await bot.get_me()

    link = f"https://t.me/{me.username}?start=file_{token}"

    await message.answer(
        "✅ <b>Файл сохранён!</b>\n\n"
        "🔗 <b>Ссылка на файл:</b>\n"
        f"<code>{link}</code>\n\n"
        "Теперь отправляйте эту ссылку пользователям.",
        parse_mode="HTML"
    )


# ============================================================
# ADD CHANNEL
# ============================================================

@dp.callback_query(F.data == "admin_add_channel")
async def admin_add_channel_callback(
    callback: CallbackQuery
):
    if callback.from_user.username.lower() != ADMIN_USERNAME.lower():
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True
        )
        return

    await callback.message.answer(
        "➕ <b>Добавление канала</b>\n\n"
        "Отправьте ссылку на публичный канал.\n\n"
        "Например:\n"
        "<code>@mychannel</code>\n"
        "<code>https://t.me/mychannel</code>\n\n"
        "⚠️ Бот должен быть администратором этого канала.",
        parse_mode="HTML"
    )

    await callback.answer()


@dp.message(F.text)
async def text_handler(message: Message):
    if not is_admin(message):
        return

    text = message.text.strip()

    # --------------------------------------------------------
    # Добавление канала
    # --------------------------------------------------------

    username = extract_channel_username(text)

    if username:
        # Проверяем, существует ли канал
        try:
            chat = await bot.get_chat(username)

        except Exception:
            await message.answer(
                "❌ Не удалось найти этот канал.\n\n"
                "Проверьте ссылку и убедитесь, что канал публичный."
            )
            return

        # Проверяем права бота
        try:
            me = await bot.get_me()

            bot_member = await bot.get_chat_member(
                chat.id,
                me.id
            )

            if bot_member.status not in {
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR
            }:
                await message.answer(
                    "❌ Бот не является администратором этого канала.\n\n"
                    "Сначала добавьте бота в канал и выдайте ему "
                    "права администратора."
                )
                return

        except Exception as e:
            logging.warning(e)

            await message.answer(
                "❌ Не удалось проверить права бота в канале."
            )
            return

        # Проверяем, нет ли уже канала
        exists = db.execute(
            "SELECT id FROM channels WHERE chat_id = ?",
            (chat.id,)
        ).fetchone()

        if exists:
            await message.answer(
                "⚠️ Этот канал уже добавлен."
            )
            return

        link = f"https://t.me/{chat.username}" \
            if chat.username \
            else text

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
                link
            )
        )

        db.commit()

        await message.answer(
            "✅ <b>Канал добавлен!</b>\n\n"
            f"📢 {chat.title}\n"
            f"🔗 {link}",
            parse_mode="HTML"
        )

        return


# ============================================================
# CHANNEL LIST
# ============================================================

@dp.callback_query(F.data == "admin_channels")
async def admin_channels_callback(
    callback: CallbackQuery
):
    if callback.from_user.username.lower() != ADMIN_USERNAME.lower():
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True
        )
        return

    channels = get_channels()

    if not channels:
        await callback.message.answer(
            "📋 <b>Список каналов пуст.</b>",
            parse_mode="HTML"
        )
        await callback.answer()
        return

    text = "📋 <b>Каналы для подписки:</b>\n\n"

    for i, channel in enumerate(channels, 1):
        text += (
            f"{i}. {channel['title'] or 'Без названия'}\n"
            f"   {channel['link']}\n\n"
        )

    await callback.message.answer(
        text,
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# DELETE CHANNEL
# ============================================================

@dp.callback_query(F.data == "admin_delete_channel")
async def admin_delete_channel_callback(
    callback: CallbackQuery
):
    if callback.from_user.username.lower() != ADMIN_USERNAME.lower():
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True
        )
        return

    channels = get_channels()

    if not channels:
        await callback.message.answer(
            "📋 Список каналов пуст."
        )
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()

    for channel in channels:
        builder.row(
            InlineKeyboardButton(
                text=f"❌ {channel['title'] or channel['link']}",
                callback_data=f"delete_channel_{channel['id']}"
            )
        )

    await callback.message.answer(
        "➖ <b>Выберите канал для удаления:</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("delete_channel_"))
async def delete_channel_callback(
    callback: CallbackQuery
):
    if callback.from_user.username.lower() != ADMIN_USERNAME.lower():
        await callback.answer(
            "❌ Доступ запрещён.",
            show_alert=True
        )
        return

    channel_id = int(
        callback.data.replace(
            "delete_channel_",
            ""
        )
    )

    channel = db.execute(
        "SELECT * FROM channels WHERE id = ?",
        (channel_id,)
    ).fetchone()

    if not channel:
        await callback.answer(
            "Канал уже удалён.",
            show_alert=True
        )
        return

    db.execute(
        "DELETE FROM channels WHERE id = ?",
        (channel_id,)
    )

    db.commit()

    await callback.message.edit_text(
        "✅ Канал удалён:\n\n"
        f"📢 {channel['title'] or channel['link']}"
    )

    await callback.answer("Удалено!")


# ============================================================
# ADMIN COMMAND
# ============================================================

@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message):
        await message.answer(
            "❌ Доступ запрещён."
        )
        return

    await message.answer(
        "👑 <b>Админ-панель</b>\n\n"
        "Выберите действие:",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    if BOT_TOKEN == "8632883159:AAH8ApaMDa8sBBypTLSvSc4UxkkBA9pUNzw":
        raise RuntimeError(
            "Укажите токен бота в BOT_TOKEN "
            "или переменной окружения BOT_TOKEN."
        )

    init_db()

    logging.info("Бот запускается...")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
