import asyncio
import json
import os
import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

# ==================== CONFIG ====================
BOT_TOKEN = "8916867955:AAHyukQPlmi3fP4LONsAUPM5J3MXKSZYg6s"

OWNER_USERNAME = "@welmaDEV"
OWNER_USERNAME_CLEAN = "welmadev"

SAVE_FILE = "market_saves.json"
STRICT_SUBSCRIPTION = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("market")

# ==================== STORAGE ====================
@dataclass
class User:
    user_id: int
    username: str = ""
    first_name: str = ""
    downloads: int = 0
    purchases: List[str] = None
    is_admin: bool = False

    def __post_init__(self):
        if self.purchases is None:
            self.purchases = []


@dataclass
class FileItem:
    file_id: str
    name: str
    file_type: str = "document"
    size: int = 0
    description: str = ""
    added_by: int = 0


class Storage:
    def __init__(self):
        self.users: Dict[int, User] = {}
        self.files: Dict[str, FileItem] = {}
        self.owner_id: Optional[int] = None
        self.admins: List[int] = []
        self.subscription_channel: Optional[str] = None
        self.states: Dict[int, dict] = {}

    def get_user(self, user_id: int, username: str = "", first_name: str = "") -> User:
        u = self.users.get(user_id)
        if not u:
            u = User(user_id=user_id, username=username, first_name=first_name)
            self.users[user_id] = u
        else:
            if username: u.username = username
            if first_name: u.first_name = first_name
        return u

    def save(self):
        try:
            data = {
                "owner_id": self.owner_id,
                "admins": self.admins,
                "subscription_channel": self.subscription_channel,
                "users": {str(k): asdict(v) for k, v in self.users.items()},
                "files": {k: asdict(v) for k, v in self.files.items()},
            }
            with open(SAVE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            log.exception("save error")

    def load(self):
        if not os.path.exists(SAVE_FILE):
            return
        try:
            with open(SAVE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.owner_id = data.get("owner_id")
            self.admins = data.get("admins", [])
            self.subscription_channel = data.get("subscription_channel")
            for k, v in data.get("users", {}).items():
                self.users[int(k)] = User(**v)
            for k, v in data.get("files", {}).items():
                self.files[k] = FileItem(**v)
            log.info(f"Loaded {len(self.users)} users, {len(self.files)} files")
        except Exception:
            log.exception("load error")


store = Storage()

# ==================== BOT SETUP ====================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ==================== UTILS ====================
def btn(text, callback_data=None, url=None):
    kwargs = {"text": text}
    if callback_data: kwargs["callback_data"] = callback_data
    if url: kwargs["url"] = url
    return InlineKeyboardButton(**kwargs)


def is_owner(u: User) -> bool:
    if store.owner_id and u.user_id == store.owner_id:
        return True
    if u.username and u.username.lower() == OWNER_USERNAME_CLEAN:
        return True
    return u.is_admin or u.user_id in store.admins


def user_display(u: User) -> str:
    name = u.first_name or u.username or str(u.user_id)
    if u.username:
        return f"{name} (@{u.username})"
    return name


def fmt_size(b: int) -> str:
    if b < 1024: return f"{b} B"
    if b < 1024 * 1024: return f"{b / 1024:.1f} KB"
    if b < 1024 * 1024 * 1024: return f"{b / 1024 / 1024:.1f} MB"
    return f"{b / 1024 / 1024 / 1024:.2f} GB"


def channel_url(ch: str) -> Optional[str]:
    if not ch:
        return None
    if ch.lstrip("-").isdigit():
        return None
    return f"https://t.me/{ch.lstrip('@')}"


# ==================== KEYBOARDS ====================
def main_menu_kb(u: User):
    rows = [
        [btn("📂 Маркетплейс", "menu:market")],
        [btn("👤 Профиль", "menu:profile")],
        [btn("ℹ️ Помощь", "menu:help")],
    ]
    if is_owner(u):
        rows.append([btn("👑 Админ-меню", "admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def market_kb(page: int = 0, per_page: int = 8):
    files = list(store.files.values())
    total = len(files)
    start = page * per_page
    end = start + per_page
    chunk = files[start:end]

    rows = []
    for f in chunk:
        rows.append([btn(f"📄 {f.name}", f"file:get:{f.name}")])

    nav = []
    if page > 0:
        nav.append(btn("◀️", f"market:page:{page - 1}"))
    pages = max(1, (total - 1) // per_page + 1) if total else 1
    nav.append(btn(f"{page + 1}/{pages}", "market:noop"))
    if (page + 1) * per_page < total:
        nav.append(btn("▶️", f"market:page:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([btn("◀️ Меню", "menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("➕ Добавить файл", "admin:addfile")],
        [btn("📋 Список файлов", "admin:files")],
        [btn("🗑 Удалить файл", "admin:delfile")],
        [btn("📡 Обязательная подписка", "admin:sub")],
        [btn("📢 Рассылка", "admin:broadcast")],
        [btn("👥 Админы", "admin:admins")],
        [btn("📊 Статистика", "admin:stats")],
        [btn("◀️ Меню", "menu:main")],
    ])


def sub_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("➕ Установить канал", "admin:setchannel")],
        [btn("➖ Убрать канал", "admin:clearchannel")],
        [btn("◀️ Назад", "admin:panel")],
    ])


def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("❌ Отмена", "admin:cancel")],
    ])


def subscription_gate_kb():
    rows = []
    url = channel_url(store.subscription_channel)
    if url:
        rows.append([btn("📡 Подписаться", url=url)])
    rows.append([btn("✅ Я подписался", "sub:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==================== SUBSCRIPTION ====================
async def _try_get_member(ch_id, user_id) -> Optional[bool]:
    try:
        member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
        status = member.status
        if status in ("creator", "administrator", "member"):
            return True
        if status in ("left", "kicked"):
            return False
        if status == "restricted":
            return bool(getattr(member, "is_member", False))
        return None
    except Exception as e:
        log.info(f"getChatMember('{ch_id}', {user_id}) failed: {e}")
        return None


async def check_subscription(user_id: int) -> bool:
    ch = store.subscription_channel
    if not ch:
        return True

    res = await _try_get_member(ch, user_id)
    if res is not None:
        return res

    if ch.startswith("@"):
        res = await _try_get_member(ch.lstrip("@"), user_id)
        if res is not None:
            return res

    log.warning(f"Subscription check failed for {ch}/{user_id}, strict={STRICT_SUBSCRIPTION}")
    return not STRICT_SUBSCRIPTION


async def require_subscription(message_or_chat_id, u: User) -> bool:
    ch = store.subscription_channel
    if not ch:
        return True
    ok = await check_subscription(u.user_id)
    if ok:
        return True
    txt = (
        f"🔒 {user_display(u)}, доступ закрыт.\n\n"
        f"Подпишитесь на канал: {ch}\n\n"
        f"После подписки нажмите «✅ Я подписался»."
    )
    markup = subscription_gate_kb()
    if isinstance(message_or_chat_id, Message):
        await message_or_chat_id.answer(txt, reply_markup=markup)
    else:
        await bot.send_message(message_or_chat_id, txt, reply_markup=markup)
    return False


# ==================== VIEWS ====================
async def show_main_menu(target, u: User, edit: bool = False):
    txt = (
        f"📦 Файловый маркетплейс\n\n"
        f"👤 {user_display(u)}\n"
        f"📥 Загрузок: {u.downloads}\n"
        f"📂 Файлов в каталоге: {len(store.files)}\n\n"
        f"Выберите действие:"
    )
    kb = main_menu_kb(u)
    if edit and isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(txt, reply_markup=kb)
        except Exception:
            await target.message.answer(txt, reply_markup=kb)
    elif isinstance(target, Message):
        await target.answer(txt, reply_markup=kb)
    else:
        await bot.send_message(target, txt, reply_markup=kb)


async def show_market(target, page: int = 0, edit: bool = False):
    files = list(store.files.values())
    if not files:
        txt = "📂 Каталог пока пуст."
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ Меню", "menu:main")]])
    else:
        total = len(files)
        per_page = 8
        start = page * per_page
        end = start + per_page
        chunk = files[start:end]
        pages = (total - 1) // per_page + 1
        lines = [f"📂 Каталог файлов ({total}) — стр. {page + 1}/{pages}\n"]
        for i, f in enumerate(chunk, start=start + 1):
            lines.append(f"{i}. {f.name} — {fmt_size(f.size)}")
        txt = "\n".join(lines)
        kb = market_kb(page, per_page)
    if edit and isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(txt, reply_markup=kb)
        except Exception:
            await target.message.answer(txt, reply_markup=kb)
    elif isinstance(target, Message):
        await target.answer(txt, reply_markup=kb)
    else:
        await bot.send_message(target, txt, reply_markup=kb)


async def show_profile(target, u: User, edit: bool = False):
    txt = (
        f"👤 Профиль\n\n"
        f"Имя: {u.first_name or '—'}\n"
        f"Username: @{u.username or '—'}\n"
        f"ID: {u.user_id}\n"
        f"📥 Загрузок: {u.downloads}\n"
        f"📦 Куплено: {len(u.purchases)}\n"
        f"👑 Статус: {'админ' if is_owner(u) else 'пользователь'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ Меню", "menu:main")]])
    if edit and isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(txt, reply_markup=kb)
        except Exception:
            await target.message.answer(txt, reply_markup=kb)
    elif isinstance(target, Message):
        await target.answer(txt, reply_markup=kb)
    else:
        await bot.send_message(target, txt, reply_markup=kb)


async def show_help(target, u: User, edit: bool = False):
    ch = store.subscription_channel
    sub_line = f"\n📡 Обязательная подписка: {ch}" if ch else ""
    txt = (
        "ℹ️ Помощь\n\n"
        "📂 Маркетплейс — список файлов\n"
        "👤 Профиль — ваша статистика\n"
        "📥 Скачать файл — нажмите на его название\n"
        + sub_line + "\n\n"
        "Команды:\n"
        "/start — меню\n"
        "/market — каталог\n"
        "/profile — профиль\n"
        "/help — помощь"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ Меню", "menu:main")]])
    if edit and isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(txt, reply_markup=kb)
        except Exception:
            await target.message.answer(txt, reply_markup=kb)
    elif isinstance(target, Message):
        await target.answer(txt, reply_markup=kb)
    else:
        await bot.send_message(target, txt, reply_markup=kb)


# ==================== COMMANDS ====================
@dp.message(Command("start", "menu"))
async def cmd_start(message: Message):
    u = store.get_user(message.from_user.id,
                       message.from_user.username or "",
                       message.from_user.first_name or "")
    store.save()
    if not await require_subscription(message, u):
        return
    await show_main_menu(message, u)


@dp.message(Command("iamowner"))
async def cmd_iamowner(message: Message):
    uname = (message.from_user.username or "").lower()
    if uname == OWNER_USERNAME_CLEAN:
        u = store.get_user(message.from_user.id,
                           message.from_user.username or "",
                           message.from_user.first_name or "")
        store.owner_id = u.user_id
        store.save()
        await message.answer(f"👑 Вы зарегистрированы как владелец.\nID: {u.user_id}",
                             reply_markup=main_menu_kb(u))
    else:
        await message.answer("❌ Вы не владелец.")


@dp.message(Command("market"))
async def cmd_market(message: Message):
    u = store.get_user(message.from_user.id,
                       message.from_user.username or "",
                       message.from_user.first_name or "")
    if not await require_subscription(message, u):
        return
    await show_market(message, 0)


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    u = store.get_user(message.from_user.id,
                       message.from_user.username or "",
                       message.from_user.first_name or "")
    if not await require_subscription(message, u):
        return
    await show_profile(message, u)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    u = store.get_user(message.from_user.id,
                       message.from_user.username or "",
                       message.from_user.first_name or "")
    if not await require_subscription(message, u):
        return
    await show_help(message, u)


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    u = store.get_user(message.from_user.id,
                       message.from_user.username or "",
                       message.from_user.first_name or "")
    if not is_owner(u):
        await message.answer("❌ Нет доступа.")
        return
    await message.answer("👑 Админ-меню", reply_markup=admin_kb())


@dp.message(Command("addadmin"))
async def cmd_addadmin(message: Message):
    u = store.get_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "")
    if not is_owner(u):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ /addadmin <user_id>")
        return
    try:
        uid = int(parts[1].strip())
    except ValueError:
        await message.answer("❌ Некорректный ID")
        return
    if uid not in store.admins:
        store.admins.append(uid)
        store.save()
    await message.answer(f"✅ {uid} добавлен в админы")


@dp.message(Command("deladmin"))
async def cmd_deladmin(message: Message):
    u = store.get_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "")
    if not is_owner(u):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ /deladmin <user_id>")
        return
    try:
        uid = int(parts[1].strip())
    except ValueError:
        await message.answer("❌ Некорректный ID")
        return
    if uid in store.admins:
        store.admins.remove(uid)
        store.save()
        await message.answer(f"🗑 {uid} удалён из админов")
    else:
        await message.answer("❌ Не админ")


# ==================== CALLBACKS: MENU ====================
@dp.callback_query(F.data == "menu:main")
async def cb_menu_main(cq: CallbackQuery):
    u = store.get_user(cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or "")
    await show_main_menu(cq, u, edit=True)
    await cq.answer()


@dp.callback_query(F.data == "menu:market")
async def cb_menu_market(cq: CallbackQuery):
    u = store.get_user(cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or "")
    if not await require_subscription(cq.message.chat.id, u):
        await cq.answer()
        return
    await show_market(cq, 0, edit=True)
    await cq.answer()


@dp.callback_query(F.data == "menu:profile")
async def cb_menu_profile(cq: CallbackQuery):
    u = store.get_user(cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or "")
    if not await require_subscription(cq.message.chat.id, u):
        await cq.answer()
        return
    await show_profile(cq, u, edit=True)
    await cq.answer()


@dp.callback_query(F.data == "menu:help")
async def cb_menu_help(cq: CallbackQuery):
    u = store.get_user(cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or "")
    if not await require_subscription(cq.message.chat.id, u):
        await cq.answer()
        return
    await show_help(cq, u, edit=True)
    await cq.answer()


@dp.callback_query(F.data.startswith("market:page:"))
async def cb_market_page(cq: CallbackQuery):
    page = int(cq.data.split(":")[2])
    u = store.get_user(cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or "")
    if not await require_subscription(cq.message.chat.id, u):
        await cq.answer()
        return
    await show_market(cq, page, edit=True)
    await cq.answer()


@dp.callback_query(F.data == "market:noop")
async def cb_market_noop(cq: CallbackQuery):
    await cq.answer()


# ==================== CALLBACKS: FILE GET ====================
@dp.callback_query(F.data.startswith("file:get:"))
async def cb_file_get(cq: CallbackQuery):
    name = cq.data.split(":", 2)[2]
    u = store.get_user(cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or "")
    if not await require_subscription(cq.message.chat.id, u):
        await cq.answer()
        return
    f = store.files.get(name)
    if not f:
        await cq.answer("Файл не найден", show_alert=True)
        return
    try:
        if f.file_type == "document":
            await bot.send_document(cq.from_user.id, f.file_id, caption=f"📄 {f.name}\n{fmt_size(f.size)}")
        elif f.file_type == "photo":
            await bot.send_photo(cq.from_user.id, f.file_id, caption=f"🖼 {f.name}")
        elif f.file_type == "video":
            await bot.send_video(cq.from_user.id, f.file_id, caption=f"🎬 {f.name}")
        elif f.file_type == "audio":
            await bot.send_audio(cq.from_user.id, f.file_id, caption=f"🎵 {f.name}")
        elif f.file_type == "voice":
            await bot.send_voice(cq.from_user.id, f.file_id, caption=f"🎙 {f.name}")
        elif f.file_type == "animation":
            await bot.send_animation(cq.from_user.id, f.file_id, caption=f"🎞 {f.name}")
        else:
            await bot.send_document(cq.from_user.id, f.file_id, caption=f"📄 {f.name}")
        u.downloads += 1
        if name not in u.purchases:
            u.purchases.append(name)
        store.save()
        await cq.answer("📥 Отправлено в ЛС")
    except Exception as e:
        log.exception("send file error")
        await cq.answer(f"Ошибка: {e}", show_alert=True)


# ==================== CALLBACKS: SUB CHECK ====================
@dp.callback_query(F.data == "sub:check")
async def cb_sub_check(cq: CallbackQuery):
    u = store.get_user(cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or "")
    ok = await check_subscription(u.user_id)
    if ok:
        await cq.answer("✅ Подписка подтверждена", show_alert=True)
        await show_main_menu(cq, u, edit=True)
    else:
        await cq.answer("❌ Подписка не найдена. Убедитесь, что бот админ канала.", show_alert=True)


# ==================== CALLBACKS: ADMIN ====================
@dp.callback_query(F.data.startswith("admin:"))
async def cb_admin(cq: CallbackQuery):
    u = store.get_user(cq.from_user.id, cq.from_user.username or "", cq.from_user.first_name or "")
    if not is_owner(u):
        await cq.answer("Нет доступа", show_alert=True)
        return
    sub = cq.data.split(":", 1)[1]

    if sub == "panel":
        try:
            await cq.message.edit_text("👑 Админ-меню", reply_markup=admin_kb())
        except Exception:
            await cq.message.answer("👑 Админ-меню", reply_markup=admin_kb())
        await cq.answer()
        return

    if sub == "cancel":
        store.states.pop(u.user_id, None)
        try:
            await cq.message.edit_text("👑 Админ-меню", reply_markup=admin_kb())
        except Exception:
            await cq.message.answer("👑 Админ-меню", reply_markup=admin_kb())
        await cq.answer()
        return

    if sub == "stats":
        txt = (
            f"📊 Статистика\n\n"
            f"👥 Пользователей: {len(store.users)}\n"
            f"📂 Файлов: {len(store.files)}\n"
            f"📥 Всего загрузок: {sum(x.downloads for x in store.users.values())}\n"
            f"📡 Канал: {store.subscription_channel or '—'}\n"
            f"👑 Владелец: {'✅' if store.owner_id else '❌'}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ Назад", "admin:panel")]])
        try:
            await cq.message.edit_text(txt, reply_markup=kb)
        except Exception:
            await cq.message.answer(txt, reply_markup=kb)
        await cq.answer()
        return

    if sub == "addfile":
        store.states[u.user_id] = {"action": "addfile_wait_file"}
        txt = (
            "➕ Отправьте файл (любого формата) в этот чат.\n\n"
            "После отправки бот попросит название.\n\n/cancel — отмена."
        )
        try:
            await cq.message.edit_text(txt, reply_markup=cancel_kb())
        except Exception:
            await cq.message.answer(txt, reply_markup=cancel_kb())
        await cq.answer()
        return

    if sub == "files":
        if not store.files:
            txt = "📋 Список файлов пуст."
        else:
            lines = [f"📋 Файлы ({len(store.files)}):\n"]
            for i, f in enumerate(store.files.values(), 1):
                lines.append(f"{i}. {f.name} — {fmt_size(f.size)}")
            txt = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ Назад", "admin:panel")]])
        try:
            await cq.message.edit_text(txt, reply_markup=kb)
        except Exception:
            await cq.message.answer(txt, reply_markup=kb)
        await cq.answer()
        return

    if sub == "delfile":
        store.states[u.user_id] = {"action": "delfile_wait_name"}
        txt = "🗑 Отправьте название файла для удаления.\n\n/cancel — отмена."
        try:
            await cq.message.edit_text(txt, reply_markup=cancel_kb())
        except Exception:
            await cq.message.answer(txt, reply_markup=cancel_kb())
        await cq.answer()
        return

    if sub == "sub":
        ch = store.subscription_channel or "не задан"
        txt = (
            f"📡 Обязательная подписка\n\n"
            f"Канал: {ch}\n"
            f"Режим проверки: {'жёсткий' if STRICT_SUBSCRIPTION else 'мягкий'}\n\n"
            f"⚠️ Бот должен быть администратором канала."
        )
        try:
            await cq.message.edit_text(txt, reply_markup=sub_kb())
        except Exception:
            await cq.message.answer(txt, reply_markup=sub_kb())
        await cq.answer()
        return

    if sub == "setchannel":
        store.states[u.user_id] = {"action": "setchannel"}
        txt = (
            "📡 Отправьте @username канала или ID (-100...).\n\n"
            "Пример: @DevBlog\n\n/cancel — отмена."
        )
        try:
            await cq.message.edit_text(txt, reply_markup=cancel_kb())
        except Exception:
            await cq.message.answer(txt, reply_markup=cancel_kb())
        await cq.answer()
        return

    if sub == "clearchannel":
        store.subscription_channel = None
        store.save()
        try:
            await cq.message.edit_text("✅ Канал обязательной подписки убран.", reply_markup=sub_kb())
        except Exception:
            await cq.message.answer("✅ Канал обязательной подписки убран.", reply_markup=sub_kb())
        await cq.answer()
        return

    if sub == "broadcast":
        store.states[u.user_id] = {"action": "broadcast"}
        txt = "📢 Отправьте текст для рассылки всем пользователям.\n\n/cancel — отмена."
        try:
            await cq.message.edit_text(txt, reply_markup=cancel_kb())
        except Exception:
            await cq.message.answer(txt, reply_markup=cancel_kb())
        await cq.answer()
        return

    if sub == "admins":
        txt = "👥 Админы:\n\n"
        if store.owner_id:
            txt += f"👑 Владелец: {store.owner_id}\n"
        if store.admins:
            for a in store.admins:
                txt += f"• {a}\n"
        else:
            txt += "• (пусто)\n"
        txt += "\nУправление: /addadmin ID, /deladmin ID"
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("◀️ Назад", "admin:panel")]])
        try:
            await cq.message.edit_text(txt, reply_markup=kb)
        except Exception:
            await cq.message.answer(txt, reply_markup=kb)
        await cq.answer()
        return

    await cq.answer()


# ==================== FSM STATES ====================
@dp.message()
async def state_handler(message: Message):
    user_id = message.from_user.id
    state = store.states.get(user_id)
    if not state:
        return
    action = state.get("action")
    text = (message.text or "").strip()

    if text.lower() in ("/cancel", "отмена", ".cancel"):
        store.states.pop(user_id, None)
        await message.answer("❌ Отменено.", reply_markup=admin_kb())
        return

    # --- Загрузка файла ---
    if action == "addfile_wait_file":
        file_id = None
        file_type = "document"
        file_name = None
        file_size = 0

        if message.document:
            file_id = message.document.file_id
            file_type = "document"
            file_name = message.document.file_name or "file"
            file_size = message.document.file_size or 0
        elif message.photo:
            file_id = message.photo[-1].file_id
            file_type = "photo"
            file_name = "photo.jpg"
            file_size = message.photo[-1].file_size or 0
        elif message.video:
            file_id = message.video.file_id
            file_type = "video"
            file_name = message.video.file_name or "video.mp4"
            file_size = message.video.file_size or 0
        elif message.audio:
            file_id = message.audio.file_id
            file_type = "audio"
            file_name = message.audio.file_name or "audio.mp3"
            file_size = message.audio.file_size or 0
        elif message.voice:
            file_id = message.voice.file_id
            file_type = "voice"
            file_name = "voice.ogg"
            file_size = message.voice.file_size or 0
        elif message.animation:
            file_id = message.animation.file_id
            file_type = "animation"
            file_name = message.animation.file_name or "animation.gif"
            file_size = message.animation.file_size or 0
        else:
            await message.answer("❌ Отправьте файл любого формата (document/photo/video/audio/voice/animation).")
            return

        store.states[user_id] = {
            "action": "addfile_wait_name",
            "file_id": file_id,
            "file_type": file_type,
            "file_size": file_size,
            "suggested_name": file_name,
        }
        await message.answer(
            f"📎 Файл получен.\n"
            f"Тип: {file_type}\n"
            f"Размер: {fmt_size(file_size)}\n"
            f"Предлагаемое имя: {file_name}\n\n"
            f"Отправьте название файла для каталога.\n"
            f"Или напишите «-», чтобы использовать предложенное имя.\n\n"
            f"/cancel — отмена."
        )
        return

    if action == "addfile_wait_name":
        name = text if text != "-" else state.get("suggested_name", "file")
        name = name.strip()
        if not name:
            await message.answer("❌ Пустое имя.")
            return
        file_id = state["file_id"]
        file_type = state["file_type"]
        file_size = state["file_size"]
        store.files[name] = FileItem(
            file_id=file_id,
            name=name,
            file_type=file_type,
            size=file_size,
            added_by=user_id,
        )
        store.states.pop(user_id, None)
        store.save()
        await message.answer(
            f"✅ Файл «{name}» добавлен в каталог.\n"
            f"Всего файлов: {len(store.files)}",
            reply_markup=admin_kb(),
        )
        return

    # --- Удаление файла ---
    if action == "delfile_wait_name":
        name = text.strip()
        if name in store.files:
            del store.files[name]
            store.states.pop(user_id, None)
            store.save()
            await message.answer(f"🗑 Файл «{name}» удалён.", reply_markup=admin_kb())
        else:
            await message.answer("❌ Файл не найден. Попробуйте снова или /cancel.")
        return

    # --- Канал подписки ---
    if action == "setchannel":
        ch = text.strip()
        if not ch:
            await message.answer("❌ Пусто.")
            return
        if not ch.startswith("@") and not ch.lstrip("-").isdigit():
            ch = "@" + ch
        store.subscription_channel = ch
        store.states.pop(user_id, None)
        store.save()
        await message.answer(
            f"✅ Канал подписки: {ch}\n\n"
            f"⚠️ Бот должен быть администратором канала.",
            reply_markup=sub_kb(),
        )
        return

    # --- Рассылка ---
    if action == "broadcast":
        store.states.pop(user_id, None)
        sent, failed = 0, 0
        for uid in list(store.users.keys()):
            try:
                await bot.send_message(uid, f"📢 Сообщение от администрации:\n\n{text}")
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await message.answer(
            f"📢 Рассылка завершена.\n✅ Доставлено: {sent}\n❌ Ошибок: {failed}",
            reply_markup=admin_kb(),
        )
        return


# ==================== MAIN ====================
async def main():
    store.load()

    me = await bot.get_me()
    log.info(f"Bot started: @{me.username} (id={me.id})")

    await bot.set_my_commands([
        {"command": "start", "description": "Главное меню"},
        {"command": "market", "description": "Каталог файлов"},
        {"command": "profile", "description": "Профиль"},
        {"command": "help", "description": "Помощь"},
    ])

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped")
