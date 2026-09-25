import asyncio
import json
import random
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp
from aiohttp import web

# ==================== CONFIG ====================
BOT_TOKEN = "1780245087:Tn-mEK_pGCpxggIav8-XZf9XotvaWvfEaIa"
BASE_URL = "http://188.134.95.254:2610"
OWNER_USERNAME = "@YourPrince"
OWNER_ID = None  # будет установлен при первом /owner или через getMe

API = f"{BASE_URL}/bot{BOT_TOKEN}"
FILE_API = f"{BASE_URL}/file/bot{BOT_TOKEN}"

WEBHOOK_HOST = "0.0.0.0"
WEBHOOK_PORT = 8080
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = None  # если нужен внешний URL — укажите

STARS_CURRENCY = "⭐"
START_BALANCE = 1000
MIN_BET = 10
MAX_BET = 100000

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("casino")

# ==================== STORAGE ====================
@dataclass
class User:
    user_id: int
    username: str = ""
    first_name: str = ""
    balance: int = START_BALANCE
    stars: int = 0
    total_wins: int = 0
    total_losses: int = 0
    total_wagered: int = 0
    total_won: int = 0
    banned: bool = False
    pending_deposit: int = 0
    last_bonus: float = 0.0

    def to_dict(self):
        return self.__dict__.copy()


class Storage:
    def __init__(self):
        self.users: Dict[int, User] = {}
        self.pending_deposits: Dict[int, dict] = {}  # owner_msg_id -> {user_id, amount}
        self.lock = asyncio.Lock()

    def get(self, user_id: int, username: str = "", first_name: str = "") -> User:
        u = self.users.get(user_id)
        if not u:
            u = User(user_id=user_id, username=username, first_name=first_name)
            self.users[user_id] = u
        else:
            if username:
                u.username = username
            if first_name:
                u.first_name = first_name
        return u

    def top(self, n: int = 10) -> List[User]:
        return sorted(self.users.values(), key=lambda x: x.balance, reverse=True)[:n]


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

    async def send_message(self, chat_id, text, entities=None, reply_markup=None, disable_web_page_preview=True):
        params = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if entities:
            params["entities"] = entities
        if reply_markup:
            params["reply_markup"] = reply_markup
        return await self.call("sendMessage", **params)

    async def edit_message_text(self, chat_id, message_id, text, entities=None, reply_markup=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if entities:
            params["entities"] = entities
        if reply_markup:
            params["reply_markup"] = reply_markup
        return await self.call("editMessageText", **params)

    async def answer_callback(self, cq_id, text="", show_alert=False):
        return await self.call("answerCallbackQuery",
                               callback_query_id=cq_id, text=text, show_alert=show_alert)

    async def delete_message(self, chat_id, message_id):
        return await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    async def get_updates(self, offset=0, timeout=30):
        return await self.call("getUpdates", offset=offset, timeout=timeout, limit=100)

    async def set_webhook(self, url, secret=None):
        p = {"url": url}
        if secret:
            p["secret_token"] = secret
        return await self.call("setWebhook", **p)

    async def delete_webhook(self):
        return await self.call("deleteWebhook")

    async def set_my_commands(self, commands):
        return await self.call("setMyCommands", commands=commands)


bot = BotAPI(BOT_TOKEN)

# ==================== UTILS ====================
def btn(text, callback_data=None, url=None, style=None, **kw):
    b = {"text": text}
    if callback_data: b["callback_data"] = callback_data
    if url: b["url"] = url
    if style: b["style"] = style
    b.update(kw)
    return b


def ikb(rows):
    return {"inline_keyboard": rows}


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def user_display(u: User) -> str:
    name = u.first_name or u.username or str(u.user_id)
    if u.username:
        return f"{name} (@{u.username})"
    return name


def e(text, type_):
    return {"type": type_, "offset": 0, "length": 0}  # placeholder, not used directly


def entities_for_bold(text: str, substrings: List[str]) -> List[dict]:
    """Строит entities для bold-подстрок."""
    ents = []
    for sub in substrings:
        start = 0
        while True:
            idx = text.find(sub, start)
            if idx == -1:
                break
            ents.append({"type": "bold", "offset": idx, "length": len(sub)})
            start = idx + len(sub)
    return ents


def parse_bet(text: str) -> Optional[int]:
    t = text.strip().lower().replace(" ", "").replace(",", "")
    if t in ("all", "allin", "вабанк", "всё", "все"):
        return -1  # all-in marker
    if t in ("half", "половина"):
        return -2
    try:
        return int(t)
    except ValueError:
        return None


# ==================== KEYBOARDS ====================
def main_menu_kb(is_owner=False):
    rows = [
        [btn("🎰 Слоты", "game:slots", style="primary"),
         btn("🎲 Кости", "game:dice", style="primary")],
        [btn("🪙 Монетка", "game:coin", style="primary"),
         btn("🎡 Рулетка", "game:roulette", style="primary")],
        [btn("🃏 Блэкджек", "game:blackjack", style="primary")],
        [btn("💰 Баланс", "menu:balance", style="success"),
         btn("🏆 Топ", "menu:top", style="success")],
        [btn("⭐ Пополнить звёздами", "menu:deposit", style="success")],
        [btn("🎁 Бонус", "menu:bonus", style="success")],
    ]
    if is_owner:
        rows.append([btn("👑 Админ-панель", "admin:panel", style="danger")])
    return ikb(rows)


def back_kb(target="menu:main"):
    return ikb([[btn("◀️ Назад", target, style="primary")]])


def bet_kb(game: str):
    return ikb([
        [btn("10", f"bet:{game}:10"), btn("50", f"bet:{game}:50"),
         btn("100", f"bet:{game}:100")],
        [btn("500", f"bet:{game}:500"), btn("1000", f"bet:{game}:1000"),
         btn("5000", f"bet:{game}:5000")],
        [btn("Ва-банк", f"bet:{game}:all", style="danger")],
        [btn("◀️ Назад", "menu:main", style="primary")],
    ])


def deposit_kb():
    return ikb([
        [btn("50 ⭐", "dep:50"), btn("100 ⭐", "dep:100"), btn("250 ⭐", "dep:250")],
        [btn("500 ⭐", "dep:500"), btn("1000 ⭐", "dep:1000")],
        [btn("◀️ Назад", "menu:main", style="primary")],
    ])


def admin_kb():
    return ikb([
        [btn("📊 Статистика", "admin:stats", style="primary")],
        [btn("📢 Рассылка", "admin:broadcast", style="primary")],
        [btn("🚫 Бан/Разбан", "admin:ban", style="danger")],
        [btn("💰 Выдать баланс", "admin:give", style="success")],
        [btn("⏳ Заявки на пополнение", "admin:deposits", style="success")],
        [btn("◀️ Назад", "menu:main", style="primary")],
    ])


# ==================== GAMES ====================
SLOT_SYMBOLS = ["🍒", "🍋", "🍊", "🍇", "💎", "7️⃣"]
SLOT_WEIGHTS = [30, 25, 20, 15, 8, 2]


def spin_slots():
    return random.choices(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, k=3)


def slots_payout(reels):
    a, b, c = reels
    if a == b == c:
        if a == "7️⃣":
            return 50
        if a == "💎":
            return 25
        return 10
    if a == b or b == c or a == c:
        return 2
    return 0


def dice_roll():
    return random.randint(1, 6)


def coin_flip():
    return random.choice(["Орёл", "Решка"])


def roulette_spin():
    red = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    n = random.randint(0, 36)
    if n == 0:
        color = "🟢 Зелёный"
    elif n in red:
        color = "🔴 Красный"
    else:
        color = "⚫ Чёрный"
    return n, color


# ==================== BLACKJACK (упрощённый, одна раздача) ====================
def bj_deck():
    ranks = ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]
    suits = ["♠","♥","♦","♣"]
    deck = [f"{r}{s}" for r in ranks for s in suits]
    random.shuffle(deck)
    return deck


def bj_value(hand):
    total = 0
    aces = 0
    for card in hand:
        r = card[:-1]
        if r in ("J","Q","K"):
            total += 10
        elif r == "A":
            aces += 1
            total += 11
        else:
            total += int(r)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


# ==================== MESSAGE BUILDERS ====================
def balance_text(u: User) -> str:
    return (
        f"💰 Баланс: {fmt(u.balance)} монет\n"
        f"⭐ Звёзд: {u.stars}\n"
        f"📈 Побед: {u.total_wins} | 📉 Поражений: {u.total_losses}\n"
        f"🎯 Всего ставок: {fmt(u.total_wagered)}\n"
        f"🏆 Всего выиграно: {fmt(u.total_won)}"
    )


def main_menu_text(u: User) -> str:
    return (
        f"🎰 Добро пожаловать в казино, {user_display(u)}!\n\n"
        f"💰 Баланс: {fmt(u.balance)} монет\n"
        f"⭐ Звёзд: {u.stars}\n\n"
        f"Выбери игру:"
    )


# ==================== HANDLERS ====================
async def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    from_ = msg.get("from", {})
    user_id = from_.get("id")
    username = from_.get("username", "")
    first_name = from_.get("first_name", "")
    text = msg.get("text", "")

    u = store.get(user_id, username, first_name)

    if u.banned:
        await bot.send_message(chat_id, "🚫 Вы забанены в казино.")
        return

    # /start
    if text.startswith("/start"):
        await bot.send_message(chat_id, main_menu_text(u),
                               reply_markup=main_menu_kb(is_owner=is_owner(u)))
        return

    # /help
    if text.startswith("/help"):
        help_text = (
            "🎰 Казино-бот\n\n"
            "Команды:\n"
            "/start — главное меню\n"
            "/balance — баланс\n"
            "/top — топ игроков\n"
            "/bonus — ежедневный бонус\n"
            "/deposit — пополнить звёздами\n"
            "/owner — информация о владельце\n"
        )
        await bot.send_message(chat_id, help_text)
        return

    if text.startswith("/balance"):
        await bot.send_message(chat_id, balance_text(u), reply_markup=back_kb())
        return

    if text.startswith("/top"):
        await bot.send_message(chat_id, top_text(), reply_markup=back_kb())
        return

    if text.startswith("/bonus"):
        await cmd_bonus(chat_id, u)
        return

    if text.startswith("/deposit"):
        await bot.send_message(chat_id,
            f"⭐ Пополнение звёздами AltGram\n\n"
            f"1 ⭐ = 100 монет\n"
            f"Выберите сумму:",
            reply_markup=deposit_kb())
        return

    if text.startswith("/owner"):
        await bot.send_message(chat_id,
            f"👑 Владелец казино: {OWNER_USERNAME}\n"
            f"Связь: {OWNER_USERNAME}")
        return

    # admin commands
    if is_owner(u):
        if text.startswith("/admin"):
            await bot.send_message(chat_id, "👑 Админ-панель", reply_markup=admin_kb())
            return
        if text.startswith("/give"):
            parts = text.split()
            if len(parts) >= 3:
                try:
                    target_id = int(parts[1])
                    amount = int(parts[2])
                    t = store.users.get(target_id)
                    if t:
                        t.balance += amount
                        await bot.send_message(chat_id, f"✅ Выдано {fmt(amount)} монет пользователю {target_id}")
                    else:
                        await bot.send_message(chat_id, "❌ Пользователь не найден")
                except ValueError:
                    await bot.send_message(chat_id, "❌ Формат: /give <user_id> <amount>")
            return

    # если это ответ на заявку пополнения
    if is_owner(u) and msg.get("reply_to_message"):
        await handle_owner_deposit_reply(msg)
        return


async def handle_callback(cq: dict):
    cq_id = cq["id"]
    from_ = cq.get("from", {})
    user_id = from_.get("id")
    username = from_.get("username", "")
    first_name = from_.get("first_name", "")
    data = cq.get("data", "")
    msg = cq.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")

    u = store.get(user_id, username, first_name)

    if u.banned:
        await bot.answer_callback(cq_id, "🚫 Вы забанены", show_alert=True)
        return

    # --- MENU ---
    if data == "menu:main":
        await bot.edit_message_text(chat_id, message_id, main_menu_text(u),
                                    reply_markup=main_menu_kb(is_owner=is_owner(u)))
        await bot.answer_callback(cq_id)
        return

    if data == "menu:balance":
        await bot.edit_message_text(chat_id, message_id, balance_text(u), reply_markup=back_kb())
        await bot.answer_callback(cq_id)
        return

    if data == "menu:top":
        await bot.edit_message_text(chat_id, message_id, top_text(), reply_markup=back_kb())
        await bot.answer_callback(cq_id)
        return

    if data == "menu:deposit":
        await bot.edit_message_text(chat_id, message_id,
            "⭐ Пополнение звёздами AltGram\n\n1 ⭐ = 100 монет\nВыберите сумму:",
            reply_markup=deposit_kb())
        await bot.answer_callback(cq_id)
        return

    if data == "menu:bonus":
        await cmd_bonus_cb(chat_id, message_id, u, cq_id)
        return

    # --- GAMES ---
    if data.startswith("game:"):
        game = data.split(":")[1]
        titles = {
            "slots": "🎰 Слоты",
            "dice": "🎲 Кости",
            "coin": "🪙 Монетка",
            "roulette": "🎡 Рулетка",
            "blackjack": "🃏 Блэкджек",
        }
        desc = {
            "slots": "Собери 3 одинаковых символа. 7️⃣ x50, 💎 x25, остальные x10. Пара — x2.",
            "dice": "Угадай, выпадет больше или меньше 3.5. Выигрыш x2.",
            "coin": "Угадай сторону монеты. Выигрыш x2.",
            "roulette": "Угадай цвет. Красный/Чёрный x2, Зелёный x14.",
            "blackjack": "Набери больше очков, чем дилер, но не больше 21. Победа x2.",
        }
        await bot.edit_message_text(chat_id, message_id,
            f"{titles.get(game, game)}\n\n{desc.get(game, '')}\n\nВыберите ставку:",
            reply_markup=bet_kb(game))
        await bot.answer_callback(cq_id)
        return

    if data.startswith("bet:"):
        parts = data.split(":")
        game = parts[1]
        bet_str = parts[2]
        if bet_str == "all":
            bet = u.balance
        else:
            bet = int(bet_str)

        if bet < MIN_BET:
            await bot.answer_callback(cq_id, f"Минимальная ставка {MIN_BET}", show_alert=True)
            return
        if bet > u.balance:
            await bot.answer_callback(cq_id, "Недостаточно средств", show_alert=True)
            return
        if bet > MAX_BET:
            await bot.answer_callback(cq_id, f"Максимальная ставка {MAX_BET}", show_alert=True)
            return

        # списываем
        u.balance -= bet
        u.total_wagered += bet

        if game == "slots":
            await play_slots(chat_id, message_id, u, bet, cq_id)
        elif game == "dice":
            await play_dice(chat_id, message_id, u, bet, cq_id)
        elif game == "coin":
            await play_coin(chat_id, message_id, u, bet, cq_id)
        elif game == "roulette":
            await bot.edit_message_text(chat_id, message_id,
                f"🎡 Рулетка\n\nСтавка: {fmt(bet)}\n\nВыберите цвет:",
                reply_markup=ikb([
                    [btn("🔴 Красный x2", f"roul:red:{bet}", style="danger"),
                     btn("⚫ Чёрный x2", f"roul:black:{bet}", style="primary")],
                    [btn("🟢 Зелёный x14", f"roul:green:{bet}", style="success")],
                    [btn("◀️ Назад", "menu:main")],
                ]))
            await bot.answer_callback(cq_id)
        elif game == "blackjack":
            await play_blackjack(chat_id, message_id, u, bet, cq_id)
        return

    if data.startswith("roul:"):
        parts = data.split(":")
        choice = parts[1]
        bet = int(parts[2])
        n, color = roulette_spin()
        win = False
        mult = 0
        if choice == "red" and "Красный" in color:
            win, mult = True, 2
        elif choice == "black" and "Чёрный" in color:
            win, mult = True, 2
        elif choice == "green" and "Зелёный" in color:
            win, mult = True, 14
        if win:
            payout = bet * mult
            u.balance += payout
            u.total_wins += 1
            u.total_won += payout
            result = f"✅ Победа! Выпало {n} {color}\nВыигрыш: {fmt(payout)}"
        else:
            u.total_losses += 1
            result = f"❌ Проигрыш. Выпало {n} {color}\nПотеряно: {fmt(bet)}"
        await bot.edit_message_text(chat_id, message_id,
            f"🎡 Рулетка\n\n{result}\n\n💰 Баланс: {fmt(u.balance)}",
            reply_markup=back_kb("menu:main"))
        await bot.answer_callback(cq_id)
        return

    # --- DEPOSIT ---
    if data.startswith("dep:"):
        amount = int(data.split(":")[1])
        stars = amount
        coins = amount * 100
        # создаём заявку
        owner_id = get_owner_id()
        req_text = (
            f"⭐ Заявка на пополнение\n\n"
            f"От: {user_display(u)} (ID: {u.user_id})\n"
            f"Сумма: {stars} ⭐\n"
            f"К зачислению: {fmt(coins)} монет\n\n"
            f"Ответьте на это сообщение /approve или /reject"
        )
        if owner_id:
            r = await bot.send_message(owner_id, req_text)
            if r.get("ok"):
                mid = r["result"]["message_id"]
                store.pending_deposits[mid] = {
                    "user_id": u.user_id, "stars": stars, "coins": coins,
                }
        u.pending_deposit = coins
        await bot.edit_message_text(chat_id, message_id,
            f"✅ Заявка на {stars} ⭐ создана.\n\n"
            f"Ожидайте подтверждения владельца {OWNER_USERNAME}.\n"
            f"После подтверждения вы получите {fmt(coins)} монет.",
            reply_markup=back_kb("menu:main"))
        await bot.answer_callback(cq_id)
        return

    # --- ADMIN ---
    if data.startswith("admin:"):
        if not is_owner(u):
            await bot.answer_callback(cq_id, "Нет доступа", show_alert=True)
            return
        sub = data.split(":")[1]
        if sub == "panel":
            await bot.edit_message_text(chat_id, message_id, "👑 Админ-панель", reply_markup=admin_kb())
            await bot.answer_callback(cq_id)
            return
        if sub == "stats":
            total_users = len(store.users)
            total_balance = sum(x.balance for x in store.users.values())
            total_wagered = sum(x.total_wagered for x in store.users.values())
            text = (
                f"📊 Статистика\n\n"
                f"👥 Пользователей: {total_users}\n"
                f"💰 Общий баланс: {fmt(total_balance)}\n"
                f"🎯 Всего ставок: {fmt(total_wagered)}\n"
                f"⏳ Заявок: {len(store.pending_deposits)}"
            )
            await bot.edit_message_text(chat_id, message_id, text, reply_markup=back_kb("admin:panel"))
            await bot.answer_callback(cq_id)
            return
        if sub == "deposits":
            if not store.pending_deposits:
                await bot.edit_message_text(chat_id, message_id, "Нет заявок", reply_markup=back_kb("admin:panel"))
                await bot.answer_callback(cq_id)
                return
            lines = ["⏳ Заявки на пополнение:\n"]
            for mid, d in store.pending_deposits.items():
                lines.append(f"• {d['stars']} ⭐ → {fmt(d['coins'])} монет (user {d['user_id']})")
            await bot.edit_message_text(chat_id, message_id, "\n".join(lines), reply_markup=back_kb("admin:panel"))
            await bot.answer_callback(cq_id)
            return
        await bot.answer_callback(cq_id)
        return

    await bot.answer_callback(cq_id)


# ==================== GAME PLAY ====================
async def play_slots(chat_id, message_id, u: User, bet: int, cq_id):
    reels = spin_slots()
    mult = slots_payout(reels)
    payout = bet * mult
    if payout > 0:
        u.balance += payout
        u.total_wins += 1
        u.total_won += payout
        status = f"✅ Выигрыш x{mult}!\n+{fmt(payout)} монет"
    else:
        u.total_losses += 1
        status = f"❌ Проигрыш\n-{fmt(bet)} монет"
    text = (
        f"🎰 Слоты\n\n"
        f"┌─────────┐\n"
        f"│ {reels[0]} {reels[1]} {reels[2]} │\n"
        f"└─────────┘\n\n"
        f"Ставка: {fmt(bet)}\n{status}\n\n"
        f"💰 Баланс: {fmt(u.balance)}"
    )
    await bot.edit_message_text(chat_id, message_id, text,
        reply_markup=ikb([
            [btn("🎰 Ещё раз", f"bet:slots:{bet}", style="primary")],
            [btn("◀️ Меню", "menu:main")],
        ]))
    await bot.answer_callback(cq_id)


async def play_dice(chat_id, message_id, u: User, bet: int, cq_id):
    roll = dice_roll()
    # игрок ставит на >3.5
    win = roll >= 4
    if win:
        payout = bet * 2
        u.balance += payout
        u.total_wins += 1
        u.total_won += payout
        status = f"✅ Выигрыш!\n+{fmt(payout)} монет"
    else:
        u.total_losses += 1
        status = f"❌ Проигрыш\n-{fmt(bet)} монет"
    text = (
        f"🎲 Кости\n\n"
        f"Выпало: {roll}\n"
        f"Ставка: Больше 3.5\n\n"
        f"{status}\n\n"
        f"💰 Баланс: {fmt(u.balance)}"
    )
    await bot.edit_message_text(chat_id, message_id, text,
        reply_markup=ikb([
            [btn("🎲 Ещё раз", f"bet:dice:{bet}", style="primary")],
            [btn("◀️ Меню", "menu:main")],
        ]))
    await bot.answer_callback(cq_id)


async def play_coin(chat_id, message_id, u: User, bet: int, cq_id):
    # упрощённо: 50/50
    result = coin_flip()
    win = random.random() < 0.5
    if win:
        payout = bet * 2
        u.balance += payout
        u.total_wins += 1
        u.total_won += payout
        status = f"✅ Выигрыш!\n+{fmt(payout)} монет"
    else:
        u.total_losses += 1
        status = f"❌ Проигрыш\n-{fmt(bet)} монет"
    text = (
        f"🪙 Монетка\n\n"
        f"Выпало: {result}\n\n"
        f"{status}\n\n"
        f"💰 Баланс: {fmt(u.balance)}"
    )
    await bot.edit_message_text(chat_id, message_id, text,
        reply_markup=ikb([
            [btn("🪙 Ещё раз", f"bet:coin:{bet}", style="primary")],
            [btn("◀️ Меню", "menu:main")],
        ]))
    await bot.answer_callback(cq_id)


async def play_blackjack(chat_id, message_id, u: User, bet: int, cq_id):
    deck = bj_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    pv = bj_value(player)
    dv = bj_value(dealer)

    # упрощённо: игрок всегда берёт до 17, дилер до 17
    while pv < 17:
        player.append(deck.pop())
        pv = bj_value(player)
    while dv < 17:
        dealer.append(deck.pop())
        dv = bj_value(dealer)

    if pv > 21:
        u.total_losses += 1
        status = f"❌ Перебор! Проигрыш\n-{fmt(bet)} монет"
    elif dv > 21 or pv > dv:
        payout = bet * 2
        u.balance += payout
        u.total_wins += 1
        u.total_won += payout
        status = f"✅ Победа!\n+{fmt(payout)} монет"
    elif pv == dv:
        u.balance += bet
        status = f"🤝 Ничья. Ставка возвращена"
    else:
        u.total_losses += 1
        status = f"❌ Проигрыш\n-{fmt(bet)} монет"

    text = (
        f"🃏 Блэкджек\n\n"
        f"Ваши карты: {' '.join(player)} = {pv}\n"
        f"Дилер: {' '.join(dealer)} = {dv}\n\n"
        f"{status}\n\n"
        f"💰 Баланс: {fmt(u.balance)}"
    )
    await bot.edit_message_text(chat_id, message_id, text,
        reply_markup=ikb([
            [btn("🃏 Ещё раз", f"bet:blackjack:{bet}", style="primary")],
            [btn("◀️ Меню", "menu:main")],
        ]))
    await bot.answer_callback(cq_id)


# ==================== BONUS ====================
async def cmd_bonus(chat_id, u: User):
    now = time.time()
    if now - u.last_bonus < 86400:
        left = int(86400 - (now - u.last_bonus))
        h = left // 3600
        m = (left % 3600) // 60
        await bot.send_message(chat_id, f"⏳ Бонус доступен через {h}ч {m}м")
        return
    amount = random.randint(100, 500)
    u.balance += amount
    u.last_bonus = now
    await bot.send_message(chat_id, f"🎁 Ежедневный бонус: +{fmt(amount)} монет\n\n💰 Баланс: {fmt(u.balance)}",
                           reply_markup=back_kb("menu:main"))


async def cmd_bonus_cb(chat_id, message_id, u: User, cq_id):
    now = time.time()
    if now - u.last_bonus < 86400:
        left = int(86400 - (now - u.last_bonus))
        h = left // 3600
        m = (left % 3600) // 60
        await bot.answer_callback(cq_id, f"Бонус через {h}ч {m}м", show_alert=True)
        return
    amount = random.randint(100, 500)
    u.balance += amount
    u.last_bonus = now
    await bot.edit_message_text(chat_id, message_id,
        f"🎁 Ежедневный бонус: +{fmt(amount)} монет\n\n💰 Баланс: {fmt(u.balance)}",
        reply_markup=back_kb("menu:main"))
    await bot.answer_callback(cq_id)


# ==================== TOP ====================
def top_text() -> str:
    top = store.top(10)
    lines = ["🏆 Топ-10 игроков:\n"]
    for i, u in enumerate(top, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} {user_display(u)} — {fmt(u.balance)}")
    return "\n".join(lines)


# ==================== OWNER ====================
_owner_id_cache = None

def get_owner_id() -> Optional[int]:
    return _owner_id_cache

def is_owner(u: User) -> bool:
    if _owner_id_cache and u.user_id == _owner_id_cache:
        return True
    if u.username and f"@{u.username}".lower() == OWNER_USERNAME.lower():
        return True
    return False


async def handle_owner_deposit_reply(msg: dict):
    """Ответ владельца на заявку пополнения."""
    reply_to = msg.get("reply_to_message", {})
    mid = reply_to.get("message_id")
    text = (msg.get("text") or "").strip().lower()
    if mid in store.pending_deposits:
        d = store.pending_deposits[mid]
        target = store.users.get(d["user_id"])
        if not target:
            await bot.send_message(msg["chat"]["id"], "❌ Пользователь не найден")
            return
        if text in ("/approve", "approve", "+", "да", "yes"):
            target.balance += d["coins"]
            target.pending_deposit = 0
            await bot.send_message(msg["chat"]["id"],
                f"✅ Зачислено {fmt(d['coins'])} монет пользователю {target.user_id}")
            try:
                await bot.send_message(target.user_id,
                    f"✅ Пополнение подтверждено!\n+{fmt(d['coins'])} монет\n\n💰 Баланс: {fmt(target.balance)}")
            except Exception:
                pass
            del store.pending_deposits[mid]
        elif text in ("/reject", "reject", "-", "нет", "no"):
            target.pending_deposit = 0
            await bot.send_message(msg["chat"]["id"], "❌ Заявка отклонена")
            try:
                await bot.send_message(target.user_id, "❌ Заявка на пополнение отклонена")
            except Exception:
                pass
            del store.pending_deposits[mid]
    else:
        await bot.send_message(msg["chat"]["id"], "❌ Заявка не найдена")


# ==================== POLLING ====================
async def polling_loop():
    offset = 0
    log.info("Starting long polling...")
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
                    elif "edited_message" in upd:
                        pass
                except Exception:
                    log.exception("handler error")
        except Exception:
            log.exception("polling error")
            await asyncio.sleep(3)


# ==================== WEBHOOK MODE (альтернатива polling) ====================
async def webhook_handler(request: web.Request):
    try:
        upd = await request.json()
    except Exception:
        return web.json_response({"ok": False})
    try:
        if "message" in upd:
            await handle_message(upd["message"])
        elif "callback_query" in upd:
            await handle_callback(upd["callback_query"])
    except Exception:
        log.exception("webhook handler error")
    return web.json_response({"ok": True})


async def run_webhook():
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()
    log.info(f"Webhook server on {WEBHOOK_HOST}:{WEBHOOK_PORT}{WEBHOOK_PATH}")
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL, secret="altgram-casino-secret")
    while True:
        await asyncio.sleep(3600)


# ==================== MAIN ====================
async def main():
    global _owner_id_cache
    await bot.start()

    me = await bot.get_me()
    if me.get("ok"):
        info = me["result"]
        log.info(f"Bot started: @{info.get('username')} (id={info.get('id')})")

    # попробуем найти owner_id через getUpdates (если владелец уже писал)
    # владелец также может установить себя командой /iamowner
    await bot.set_my_commands([
        {"command": "start", "description": "Главное меню"},
        {"command": "balance", "description": "Баланс"},
        {"command": "top", "description": "Топ игроков"},
        {"command": "bonus", "description": "Ежедневный бонус"},
        {"command": "deposit", "description": "Пополнить звёздами"},
        {"command": "owner", "description": "Владелец"},
    ])

    if WEBHOOK_URL:
        await run_webhook()
    else:
        await polling_loop()

    await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped")
