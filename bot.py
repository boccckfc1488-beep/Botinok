import asyncio
import random
import time
import json
import os
import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

import aiohttp

# ==================== CONFIG ====================
BOT_TOKEN = "1780245103:PxwQMTFsHIAsrbzc-hJJp1SBTVklympFJ2c"
BASE_URL = "http://188.134.95.254:2610"
OWNER_USERNAME = "@YourPrince"
OWNER_USERNAME_CLEAN = "yourprince"

CARD_COOLDOWN = 86400  # 24 часа
SUBSCRIPTION_CHANNEL = "@YourPrince"  # канал обязательной подписки
CAT_IMAGE_URL = "https://cataas.com/cat?width=800&height=600"

SAVE_FILE = "komaru_saves.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("komaru")

# ==================== RARITIES ====================
RARITIES = [
    # (name, emoji, weight, price_min, price_max, color_style)
    ("Обычная",     "⚪", 45,   10,    50,   None),
    ("Необычная",   "🟢", 25,   50,    150,  "success"),
    ("Редкая",      "🔵", 15,   150,   400,  "primary"),
    ("Эпическая",   "🟣", 8,    400,   1000, "primary"),
    ("Легендарная", "🟡", 5,    1000,  3000, "success"),
    ("Мифическая",  "🔴", 2,    3000,  10000,"danger"),
]

CAT_BREEDS = [
    "Мурзик", "Барсик", "Васька", "Пушок", "Рыжик", "Снежок", "Дымок",
    "Тишка", "Кузя", "Лаки", "Симба", "Лео", "Мия", "Луна", "Белла",
    "Оскар", "Тайсон", "Мурка", "Плюша", "Зефир", "Кексик", "Пряник",
]

CAT_SUFFIX = [
    "Пушистый", "Усатый", "Полосатый", "Сонный", "Игривый", "Хитрый",
    "Важный", "Дерзкий", "Ласковый", "Грозный", "Величественный",
]

CAT_DESCRIPTIONS = [
    "Спит 20 часов в сутки.",
    "Обожает коробки больше, чем еду.",
    "Требует поглаживаний немедленно.",
    "Охотится на лазерную точку.",
    "Мяукает по утрам вместо будильника.",
    "Считает себя хозяином дома.",
    "Прячется в шкафу при гостях.",
    "Пьёт воду только из-под крана.",
    "Игнорирует дорогие игрушки, играет с фантиком.",
    "Мурчит так, что дрожит стена.",
]


def roll_rarity():
    total = sum(r[2] for r in RARITIES)
    pick = random.uniform(0, total)
    acc = 0
    for r in RARITIES:
        acc += r[2]
        if pick <= acc:
            return r
    return RARITIES[0]


def make_card() -> dict:
    r = roll_rarity()
    name = f"{random.choice(CAT_BREEDS)} {random.choice(CAT_SUFFIX)}"
    price = random.randint(r[3], r[4])
    desc = random.choice(CAT_DESCRIPTIONS)
    return {
        "name": name,
        "rarity": r[0],
        "emoji": r[1],
        "price": price,
        "description": desc,
        "style": r[5],
        "card_id": f"{int(time.time())}-{random.randint(1000,9999)}",
        "image_url": f"{CAT_IMAGE_URL}&t={int(time.time())}_{random.randint(1,99999)}",
    }


# ==================== STORAGE ====================
@dataclass
class User:
    user_id: int
    username: str = ""
    first_name: str = ""
    last_card_ts: float = 0.0
    cards: List[dict] = None
    cards_total: int = 0
    subscription_bonus_claimed: bool = False
    chats_added: List[int] = None

    def __post_init__(self):
        if self.cards is None:
            self.cards = []
        if self.chats_added is None:
            self.chats_added = []


class Storage:
    def __init__(self):
        self.users: Dict[int, User] = {}
        self.owner_id: Optional[int] = None
        self.added_chats: List[int] = []
        self.states: Dict[int, dict] = {}
        self.lock = asyncio.Lock()

    def get(self, user_id: int, username: str = "", first_name: str = "") -> User:
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
                "added_chats": self.added_chats,
                "users": {str(k): asdict(v) for k, v in self.users.items()},
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
            self.added_chats = data.get("added_chats", [])
            for k, v in data.get("users", {}).items():
                u = User(**v)
                self.users[int(k)] = u
            log.info(f"Loaded {len(self.users)} users, {len(self.added_chats)} chats")
        except Exception:
            log.exception("load error")


store = Storage()

# ==================== HTTP API ====================
class BotAPI:
    def __init__(self, token: str):
        self.token = token
        self.base = f"{BASE_URL}/bot{token}"
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))

    async def close(self):
        if self.session:
            await self.session.close()

    async def call(self, method: str, **params):
        url = f"{self.base}/{method}"
        try:
            async with self.session.post(url, json=params) as r:
                data = await r.json(content_type=None)
                if not data.get("ok"):
                    log.warning(f"API error {method}: {data}")
                return data
        except Exception as e:
            log.exception(f"HTTP error {method}: {e}")
            return {"ok": False, "description": str(e)}

    async def get_me(self):
        return await self.call("getMe")

    async def send_message(self, chat_id, text, entities=None, reply_markup=None,
                           disable_web_page_preview=True):
        p = {"chat_id": chat_id, "text": text,
             "disable_web_page_preview": disable_web_page_preview}
        if entities: p["entities"] = entities
        if reply_markup: p["reply_markup"] = reply_markup
        return await self.call("sendMessage", **p)

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        p = {"chat_id": chat_id, "photo": photo}
        if caption: p["caption"] = caption
        if reply_markup: p["reply_markup"] = reply_markup
        return await self.call("sendPhoto", **p)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        p = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup: p["reply_markup"] = reply_markup
        return await self.call("editMessageText", **p)

    async def answer_callback(self, cq_id, text="", show_alert=False):
        return await self.call("answerCallbackQuery",
                               callback_query_id=cq_id, text=text, show_alert=show_alert)

    async def get_updates(self, offset=0, timeout=30):
        return await self.call("getUpdates", offset=offset, timeout=timeout, limit=100)

    async def set_my_commands(self, commands):
        return await self.call("setMyCommands", commands=commands)

    async def get_chat_member(self, chat_id, user_id):
        return await self.call("getChatMember", chat_id=chat_id, user_id=user_id)

    async def get_chat(self, chat_id):
        return await self.call("getChat", chat_id=chat_id)


bot = BotAPI(BOT_TOKEN)

# ==================== UTILS ====================
def btn(text, callback_data=None, style=None, url=None):
    b = {"text": text}
    if callback_data: b["callback_data"] = callback_data
    if url: b["url"] = url
    if style: b["style"] = style
    return b


def ikb(rows):
    return {"inline_keyboard": rows}


def fmt(n) -> str:
    try: return f"{int(n):,}".replace(",", " ")
    except Exception: return str(n)


def user_display(u: User) -> str:
    name = u.first_name or u.username or str(u.user_id)
    if u.username:
        return f"{name} (@{u.username})"
    return name


def is_owner(u: User) -> bool:
    if store.owner_id and u.user_id == store.owner_id:
        return True
    if u.username and u.username.lower() == OWNER_USERNAME_CLEAN:
        return True
    return False


def card_caption(card: dict) -> str:
    return (
        f"{card['emoji']} {card['name']}\n\n"
        f"Редкость: {card['rarity']}\n"
        f"Цена: {fmt(card['price'])} монет\n"
        f"Описание: {card['description']}\n\n"
        f"ID карты: {card['card_id']}"
    )


def time_left(ts: float) -> str:
    left = int(CARD_COOLDOWN - (time.time() - ts))
    if left <= 0:
        return "доступно"
    h = left // 3600
    m = (left % 3600) // 60
    return f"{h}ч {m}м"


# ==================== KEYBOARDS ====================
def main_menu_kb(owner=False):
    rows = [
        [btn("🐱 Получить карту", "card:get", style="success")],
        [btn("📚 Моя коллекция", "card:collection", style="primary"),
         btn("🏆 Топ", "card:top", style="primary")],
        [btn("📊 Профиль", "card:profile", style="primary")],
        [btn("ℹ️ Помощь", "card:help", style="primary")],
    ]
    if owner:
        rows.append([btn("👑 Админ", "admin:panel", style="danger")])
    return ikb(rows)


def collection_kb(page: int, total: int, per_page: int = 5):
    rows = []
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"col:page:{page-1}", style="primary"))
    nav.append(btn(f"{page+1}/{(total-1)//per_page+1 if total else 1}", "col:noop"))
    if (page + 1) * per_page < total:
        nav.append(btn("▶️", f"col:page:{page+1}", style="primary"))
    if nav:
        rows.append(nav)
    rows.append([btn("◀️ Меню", "menu:main", style="primary")])
    return ikb(rows)


def admin_kb():
    return ikb([
        [btn("➕ Добавить чат", "admin:addchat", style="success")],
        [btn("➖ Удалить чат", "admin:delchat", style="danger")],
        [btn("📋 Список чатов", "admin:chats", style="primary")],
        [btn("📢 Рассылка", "admin:broadcast", style="primary")],
        [btn("📊 Статистика", "admin:stats", style="primary")],
        [btn("🎁 Выдать карту", "admin:givecard", style="success")],
        [btn("◀️ Меню", "menu:main", style="primary")],
    ])


def cancel_kb():
    return ikb([[btn("❌ Отмена", "admin:cancel", style="danger")]])


# ==================== SUBSCRIPTION ====================
async def check_subscription(user_id: int) -> bool:
    """Проверяет подписку на канал. Если канал недоступен — считаем подписанным."""
    if not SUBSCRIPTION_CHANNEL:
        return True
    try:
        r = await bot.get_chat_member(SUBSCRIPTION_CHANNEL, user_id)
        if r.get("ok"):
            status = r["result"].get("status", "")
            return status in ("creator", "administrator", "member")
        return True
    except Exception:
        return True


# ==================== CARD ISSUE ====================
async def issue_card(chat_id: int, u: User, is_daily: bool = True):
    if is_daily:
        now = time.time()
        if now - u.last_card_ts < CARD_COOLDOWN:
            await bot.send_message(chat_id,
                f"⏳ Следующая карта доступна через {time_left(u.last_card_ts)}",
                reply_markup=main_menu_kb(owner=is_owner(u)))
            return

    # бонус за подписку +1 карта
    bonus = 0
    sub = await check_subscription(u.user_id)
    if sub and not u.subscription_bonus_claimed:
        bonus = 1
        u.subscription_bonus_claimed = True

    total_cards = 1 + bonus
    issued = []
    for _ in range(total_cards):
        card = make_card()
        u.cards.append(card)
        u.cards_total += 1
        issued.append(card)

    if is_daily:
        u.last_card_ts = time.time()

    # отправляем по одной карте
    for card in issued:
        markup = ikb([
            [btn("📚 Коллекция", "card:collection", style="primary"),
             btn("🐱 Ещё раз", "card:get", style="success")],
            [btn("◀️ Меню", "menu:main", style="primary")],
        ])
        await bot.send_photo(chat_id, card["image_url"],
                             caption=card_caption(card),
                             reply_markup=markup)

    if bonus:
        await bot.send_message(chat_id,
            f"🎁 Бонус за подписку на {SUBSCRIPTION_CHANNEL}: +1 карта!\n"
            f"Всего выдано: {total_cards}")

    store.save()


# ==================== MESSAGE HANDLER ====================
async def handle_message(msg: dict):
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private")
    from_ = msg.get("from", {})
    user_id = from_.get("id")
    username = from_.get("username", "") or ""
    first_name = from_.get("first_name", "") or ""
    text = msg.get("text", "") or ""

    if not user_id:
        return

    u = store.get(user_id, username, first_name)

    # FSM
    if user_id in store.states:
        if text.strip().lower() in ("/cancel", "отмена", ".cancel"):
            store.states.pop(user_id, None)
            await bot.send_message(chat_id, "❌ Отменено.",
                                   reply_markup=main_menu_kb(owner=is_owner(u)))
            return
        if await handle_state(user_id, chat_id, text, u):
            return

    # команды
    if text.startswith("/start") or text.startswith("/menu"):
        await bot.send_message(chat_id,
            f"🐱 Привет, {user_display(u)}!\n\n"
            f"Это бот-коллекционер кошачьих карточек.\n"
            f"Каждый день ты можешь получить 1 карту (или 2, если подписан на {SUBSCRIPTION_CHANNEL}).\n\n"
            f"Выбери действие:",
            reply_markup=main_menu_kb(owner=is_owner(u)))
        return

    if text.startswith("/iamowner"):
        if username.lower() == OWNER_USERNAME_CLEAN:
            store.owner_id = user_id
            store.save()
            await bot.send_message(chat_id,
                f"👑 Вы зарегистрированы как владелец.\nID: {user_id}",
                reply_markup=main_menu_kb(owner=True))
        else:
            await bot.send_message(chat_id, "❌ Вы не владелец.")
        return

    if text.startswith("/card") or text.startswith("/get"):
        await issue_card(chat_id, u, is_daily=True)
        return

    if text.startswith("/collection") or text.startswith("/col"):
        await show_collection(chat_id, u, page=0)
        return

    if text.startswith("/top"):
        await show_top(chat_id)
        return

    if text.startswith("/profile"):
        await show_profile(chat_id, u)
        return

    if text.startswith("/help"):
        await send_help(chat_id, u)
        return

    if text.startswith("/admin") and is_owner(u):
        await bot.send_message(chat_id, "👑 Админ-панель", reply_markup=admin_kb())
        return

    # владелец может добавлять чат командой /addchat <chat_id>
    if is_owner(u) and text.startswith("/addchat"):
        parts = text.split()
        if len(parts) >= 2:
            try:
                cid = int(parts[1])
                if cid not in store.added_chats:
                    store.added_chats.append(cid)
                    store.save()
                await bot.send_message(chat_id, f"✅ Чат {cid} добавлен")
            except ValueError:
                await bot.send_message(chat_id, "❌ /addchat <chat_id>")
        else:
            await bot.send_message(chat_id, "❌ /addchat <chat_id>")
        return


async def handle_state(user_id: int, chat_id: int, text: str, u: User) -> bool:
    state = store.states.get(user_id)
    if not state:
        return False
    action = state.get("action")

    if action == "addchat":
        store.states.pop(user_id, None)
        try:
            cid = int(text.strip())
        except ValueError:
            await bot.send_message(chat_id, "❌ Нужен числовой chat_id.",
                                   reply_markup=admin_kb())
            return True
        if cid not in store.added_chats:
            store.added_chats.append(cid)
            store.save()
        await bot.send_message(chat_id, f"✅ Чат {cid} добавлен.", reply_markup=admin_kb())
        return True

    if action == "delchat":
        store.states.pop(user_id, None)
        try:
            cid = int(text.strip())
        except ValueError:
            await bot.send_message(chat_id, "❌ Нужен числовой chat_id.",
                                   reply_markup=admin_kb())
            return True
        if cid in store.added_chats:
            store.added_chats.remove(cid)
            store.save()
            await bot.send_message(chat_id, f"🗑 Чат {cid} удалён.", reply_markup=admin_kb())
        else:
            await bot.send_message(chat_id, "❌ Чат не найден.", reply_markup=admin_kb())
        return True

    if action == "broadcast":
        store.states.pop(user_id, None)
        sent, failed = 0, 0
        targets = list(store.users.keys()) + list(store.added_chats)
        seen = set()
        for tid in targets:
            if tid in seen: continue
            seen.add(tid)
            try:
                r = await bot.send_message(tid, f"📢 Сообщение от администрации:\n\n{text}")
                if r.get("ok"): sent += 1
                else: failed += 1
            except Exception:
                failed += 1
        await bot.send_message(chat_id,
            f"📢 Рассылка завершена.\n✅ {sent}\n❌ {failed}",
            reply_markup=admin_kb())
        return True

    if action == "givecard":
        store.states.pop(user_id, None)
        parts = text.replace(",", " ").split()
        if not parts:
            await bot.send_message(chat_id, "❌ Формат: @username или user_id",
                                   reply_markup=admin_kb())
            return True
        raw = parts[0].lstrip("@")
        target = None
        if raw.lstrip("-").isdigit():
            target = store.users.get(int(raw))
        else:
            for x in store.users.values():
                if (x.username or "").lower() == raw.lower():
                    target = x
                    break
        if not target:
            await bot.send_message(chat_id, "❌ Пользователь не найден.", reply_markup=admin_kb())
            return True
        card = make_card()
        target.cards.append(card)
        target.cards_total += 1
        store.save()
        await bot.send_message(chat_id,
            f"✅ Выдана карта {user_display(target)}:\n{card['emoji']} {card['name']} ({card['rarity']})",
            reply_markup=admin_kb())
        try:
            await bot.send_photo(target.user_id, card["image_url"],
                                 caption=f"🎁 Вам выдана карта!\n\n{card_caption(card)}")
        except Exception:
            pass
        return True

    return False


# ==================== CALLBACK HANDLER ====================
async def handle_callback(cq: dict):
    cq_id = cq["id"]
    from_ = cq.get("from", {})
    user_id = from_.get("id")
    username = from_.get("username", "") or ""
    first_name = from_.get("first_name", "") or ""
    data = cq.get("data", "")
    msg = cq.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")

    u = store.get(user_id, username, first_name)

    if data == "menu:main":
        await bot.edit_message_text(chat_id, message_id,
            f"🐱 Главное меню\n\nВыбери действие:",
            reply_markup=main_menu_kb(owner=is_owner(u)))
        await bot.answer_callback(cq_id)
        return

    if data == "card:get":
        await bot.answer_callback(cq_id)
        await issue_card(chat_id, u, is_daily=True)
        return

    if data == "card:collection":
        await bot.answer_callback(cq_id)
        await show_collection(chat_id, u, page=0, edit_id=message_id)
        return

    if data.startswith("col:page:"):
        page = int(data.split(":")[2])
        await bot.answer_callback(cq_id)
        await show_collection(chat_id, u, page=page, edit_id=message_id)
        return

    if data == "col:noop":
        await bot.answer_callback(cq_id)
        return

    if data == "card:top":
        await bot.answer_callback(cq_id)
        await show_top(chat_id, edit_id=message_id)
        return

    if data == "card:profile":
        await bot.answer_callback(cq_id)
        await show_profile(chat_id, u, edit_id=message_id)
        return

    if data == "card:help":
        await bot.answer_callback(cq_id)
        await send_help(chat_id, u, edit_id=message_id)
        return

    if data.startswith("admin:"):
        if not is_owner(u):
            await bot.answer_callback(cq_id, "Нет доступа", show_alert=True)
            return
        sub = data.split(":")[1]

        if sub == "panel":
            await bot.edit_message_text(chat_id, message_id, "👑 Админ-панель",
                                        reply_markup=admin_kb())
            await bot.answer_callback(cq_id)
            return

        if sub == "cancel":
            store.states.pop(user_id, None)
            await bot.edit_message_text(chat_id, message_id, "👑 Админ-панель",
                                        reply_markup=admin_kb())
            await bot.answer_callback(cq_id)
            return

        if sub == "addchat":
            store.states[user_id] = {"action": "addchat"}
            await bot.edit_message_text(chat_id, message_id,
                "➕ Введите chat_id для добавления.\n\n/cancel — отмена.",
                reply_markup=cancel_kb())
            await bot.answer_callback(cq_id)
            return

        if sub == "delchat":
            store.states[user_id] = {"action": "delchat"}
            await bot.edit_message_text(chat_id, message_id,
                "➖ Введите chat_id для удаления.\n\n/cancel — отмена.",
                reply_markup=cancel_kb())
            await bot.answer_callback(cq_id)
            return

        if sub == "chats":
            if not store.added_chats:
                txt = "📋 Список чатов пуст."
            else:
                txt = "📋 Добавленные чаты:\n" + "\n".join(f"• {c}" for c in store.added_chats)
            await bot.edit_message_text(chat_id, message_id, txt,
                                        reply_markup=ikb([[btn("◀️ Назад", "admin:panel", style="primary")]]))
            await bot.answer_callback(cq_id)
            return

        if sub == "broadcast":
            store.states[user_id] = {"action": "broadcast"}
            await bot.edit_message_text(chat_id, message_id,
                "📢 Введите текст для рассылки.\n\n/cancel — отмена.",
                reply_markup=cancel_kb())
            await bot.answer_callback(cq_id)
            return

        if sub == "stats":
            total_users = len(store.users)
            total_cards = sum(x.cards_total for x in store.users.values())
            sub_bonus = sum(1 for x in store.users.values() if x.subscription_bonus_claimed)
            txt = (
                f"📊 Статистика\n\n"
                f"👥 Пользователей: {total_users}\n"
                f"🐱 Карт выдано: {total_cards}\n"
                f"🎁 Бонус за подписку: {sub_bonus}\n"
                f"💬 Чатов: {len(store.added_chats)}\n"
                f"👑 Владелец: {'✅' if store.owner_id else '❌'}"
            )
            await bot.edit_message_text(chat_id, message_id, txt,
                                        reply_markup=ikb([[btn("◀️ Назад", "admin:panel", style="primary")]]))
            await bot.answer_callback(cq_id)
            return

        if sub == "givecard":
            store.states[user_id] = {"action": "givecard"}
            await bot.edit_message_text(chat_id, message_id,
                "🎁 Введите @username или user_id.\n\n/cancel — отмена.",
                reply_markup=cancel_kb())
            await bot.answer_callback(cq_id)
            return

        await bot.answer_callback(cq_id)
        return

    await bot.answer_callback(cq_id)


# ==================== VIEWS ====================
async def show_collection(chat_id, u: User, page: int = 0, edit_id=None):
    per_page = 5
    total = len(u.cards)
    if total == 0:
        txt = "📚 Ваша коллекция пуста.\n\nПолучите первую карту в главном меню."
        markup = ikb([[btn("🐱 Получить карту", "card:get", style="success")],
                      [btn("◀️ Меню", "menu:main", style="primary")]])
        if edit_id:
            await bot.edit_message_text(chat_id, edit_id, txt, reply_markup=markup)
        else:
            await bot.send_message(chat_id, txt, reply_markup=markup)
        return

    start = page * per_page
    end = start + per_page
    chunk = u.cards[start:end]
    lines = [f"📚 Коллекция ({total} карт) — стр. {page+1}\n"]
    for i, c in enumerate(chunk, start=start+1):
        lines.append(f"{i}. {c['emoji']} {c['name']} — {c['rarity']} — {fmt(c['price'])}")
    txt = "\n".join(lines)
    markup = collection_kb(page, total, per_page)
    if edit_id:
        await bot.edit_message_text(chat_id, edit_id, txt, reply_markup=markup)
    else:
        await bot.send_message(chat_id, txt, reply_markup=markup)


async def show_top(chat_id, edit_id=None):
    top = sorted(store.users.values(), key=lambda x: x.cards_total, reverse=True)[:10]
    lines = ["🏆 Топ коллекционеров:\n"]
    for i, x in enumerate(top, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} {user_display(x)} — {x.cards_total} карт")
    txt = "\n".join(lines) if len(lines) > 1 else "Пока никого нет."
    markup = ikb([[btn("◀️ Меню", "menu:main", style="primary")]])
    if edit_id:
        await bot.edit_message_text(chat_id, edit_id, txt, reply_markup=markup)
    else:
        await bot.send_message(chat_id, txt, reply_markup=markup)


async def show_profile(chat_id, u: User, edit_id=None):
    sub = await check_subscription(u.user_id)
    txt = (
        f"👤 Профиль\n\n"
        f"Имя: {u.first_name or '—'}\n"
        f"Username: @{u.username or '—'}\n"
        f"ID: {u.user_id}\n"
        f"🐱 Карт всего: {u.cards_total}\n"
        f"📅 Следующая карта: {time_left(u.last_card_ts)}\n"
        f"📡 Подписка: {'✅' if sub else '❌'}\n"
        f"🎁 Бонус за подписку: {'✅ получен' if u.subscription_bonus_claimed else '❌ не получен'}"
    )
    markup = ikb([
        [btn("🐱 Получить карту", "card:get", style="success")],
        [btn("◀️ Меню", "menu:main", style="primary")],
    ])
    if edit_id:
        await bot.edit_message_text(chat_id, edit_id, txt, reply_markup=markup)
    else:
        await bot.send_message(chat_id, txt, reply_markup=markup)


async def send_help(chat_id, u: User, edit_id=None):
    txt = (
        "🐱 Комару — бот коллекционных кошачьих карточек\n\n"
        "Как играть:\n"
        "• 1 раз в день — бесплатная карта\n"
        "• Подпишись на " + SUBSCRIPTION_CHANNEL + " — получишь +1 карту (один раз)\n"
        "• Собирай карты разной редкости\n"
        "• Смотри топ коллекционеров\n\n"
        "Редкости:\n"
        "⚪ Обычная\n"
        "🟢 Необычная\n"
        "🔵 Редкая\n"
        "🟣 Эпическая\n"
        "🟡 Легендарная\n"
        "🔴 Мифическая\n\n"
        "Команды:\n"
        "/start — меню\n"
        "/card — получить карту\n"
        "/collection — коллекция\n"
        "/top — топ\n"
        "/profile — профиль\n"
        "/help — помощь"
    )
    markup = ikb([[btn("◀️ Меню", "menu:main", style="primary")]])
    if edit_id:
        await bot.edit_message_text(chat_id, edit_id, txt, reply_markup=markup)
    else:
        await bot.send_message(chat_id, txt, reply_markup=markup)


# ==================== POLLING ====================
async def polling_loop():
    offset = 0
    log.info("Starting polling...")
    while True:
        try:
            r = await bot.get_updates(offset=offset, timeout=30)
            if not r.get("ok"):
                await asyncio.sleep(2)
                continue
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    if "message" in upd:
                        await handle_message(upd["message"])
                    elif "callback_query" in upd:
                        await handle_callback(upd["callback_query"])
                except Exception:
                    log.exception("handler error")
        except Exception:
            log.exception("polling error")
            await asyncio.sleep(3)


# ==================== MAIN ====================
async def main():
    store.load()
    await bot.start()

    me = await bot.get_me()
    if me.get("ok"):
        info = me["result"]
        log.info(f"Komaru bot: @{info.get('username')} (id={info.get('id')})")

    await bot.set_my_commands([
        {"command": "start", "description": "Главное меню"},
        {"command": "card", "description": "Получить карту"},
        {"command": "collection", "description": "Моя коллекция"},
        {"command": "top", "description": "Топ коллекционеров"},
        {"command": "profile", "description": "Профиль"},
        {"command": "help", "description": "Помощь"},
    ])

    await polling_loop()
    await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped")
