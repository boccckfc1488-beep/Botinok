# bot.py — FunGame utils v2: инлайн-меню, премиум, дуэли для всех, приветствие в чат
# python 3.11+ / aiogram 3.13.1
# pip install aiogram==3.13.1

import asyncio
import html
import random
import sqlite3
import time
import uuid
from contextlib import closing

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, Message, PreCheckoutQuery,
)

# ================= CONFIG =================
BOT_TOKEN = "8920969460:AAF93q-q-UnAosrD6VU-dRH6fucxwZgtBro"  # FILL: ревокни позже
DB_PATH = "fungame.db"

START_BALANCE = 1000
DAILY_BONUS = 500
DAILY_BONUS_PREMIUM = 1000       # премиум x2 бонус
PREMIUM_WIN_MULT = 1.2           # +20% к выигрышу
PREMIUM_PRICE_STARS = 100        # 100 звёзд за 30 дней
COINS_PER_STAR = 100
STAR_PACKS = [(1, 100), (5, 550), (10, 1200), (50, 7000)]

OWNER_USERNAME = "WelmaDEV"
ADMIN_USERNAMES = {"welmadev"}
ADMINS: set[int] = set()

BUSINESSES = {
    "kiosk":   ("🥤 ларёк",      5_000,    500),
    "shop":    ("🏪 магазин",    25_000,   2_800),
    "factory": ("🏭 завод",      100_000,  12_000),
    "bank":    ("🏦 банк",       500_000,  70_000),
    "oil":     ("🛢 нефтевышка", 2_000_000, 320_000),
}

SHOP_ITEMS = {
    "shield": ("🛡 щит",      3_000, "следующая проигранная игра вернёт 50%"),
    "double": ("⚡ удвоитель", 5_000, "следующая победа ×2"),
    "reroll": ("🎲 реролл",   1_500, "перебросить неудачный бросок"),
}

RED_NUMBERS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
# ==========================================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
admin_router = Router()
user_router = Router()


# ============ DB ============
def db_init():
    with closing(sqlite3.connect(DB_PATH)) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                balance       INTEGER DEFAULT 0,
                wins          INTEGER DEFAULT 0,
                losses        INTEGER DEFAULT 0,
                wagered       INTEGER DEFAULT 0,
                won_total     INTEGER DEFAULT 0,
                last_bonus    INTEGER DEFAULT 0,
                premium_until INTEGER DEFAULT 0,
                created_at    INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS businesses (
                user_id   INTEGER NOT NULL,
                biz_id    TEXT NOT NULL,
                bought_at INTEGER NOT NULL,
                last_pay  INTEGER NOT NULL,
                PRIMARY KEY (user_id, biz_id)
            );
            CREATE TABLE IF NOT EXISTS inventory (
                user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                qty     INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, item_id)
            );
            CREATE TABLE IF NOT EXISTS forced_subs (
                channel TEXT PRIMARY KEY,
                title   TEXT,
                invite  TEXT
            );
        """)
        c.commit()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def now_ts() -> int: return int(time.time())

def ensure_user(uid, username=None, first_name=None):
    with closing(db()) as c:
        row = c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            c.execute("INSERT INTO users(user_id, username, first_name, balance, created_at) VALUES(?,?,?,?,?)",
                      (uid, username or "", first_name or "", START_BALANCE, now_ts()))
        else:
            c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?",
                      (username or "", first_name or "", uid))
        c.commit()

def get_user(uid):
    with closing(db()) as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def get_balance(uid) -> int:
    u = get_user(uid)
    return u["balance"] if u else 0

def add_balance(uid, delta):
    with closing(db()) as c:
        c.execute("UPDATE users SET balance=MAX(0, balance+?) WHERE user_id=?", (delta, uid))
        c.commit()

def record_wager(uid, bet):
    with closing(db()) as c:
        c.execute("UPDATE users SET wagered=wagered+? WHERE user_id=?", (bet, uid))
        c.commit()

def record_win(uid, amount):
    with closing(db()) as c:
        c.execute("UPDATE users SET wins=wins+1, won_total=won_total+? WHERE user_id=?",
                  (amount, uid))
        c.commit()

def record_loss(uid):
    with closing(db()) as c:
        c.execute("UPDATE users SET losses=losses+1 WHERE user_id=?", (uid,))
        c.commit()

def is_premium(uid) -> bool:
    u = get_user(uid)
    return bool(u and u["premium_until"] > now_ts())

def premium_multiplier(uid) -> float:
    return PREMIUM_WIN_MULT if is_premium(uid) else 1.0

def is_admin(uid, username=None) -> bool:
    if uid in ADMINS: return True
    if username and username.lower() in {u.lower() for u in ADMIN_USERNAMES}: return True
    return False


# ============ forced subs ============
async def missing_subs(uid) -> list:
    with closing(db()) as c:
        subs = c.execute("SELECT * FROM forced_subs").fetchall()
    if not subs: return []
    missing = []
    for s in subs:
        try:
            m = await bot.get_chat_member(s["channel"], uid)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                missing.append(s)
        except (TelegramBadRequest, TelegramForbiddenError):
            missing.append(s)
    return missing

def subs_kb(missing):
    rows = [[InlineKeyboardButton(text=f"📢 {s['title'] or s['channel']}",
                                  url=s["invite"] or f"https://t.me/{s['channel'].lstrip('@')}")]
            for s in missing]
    rows.append([InlineKeyboardButton(text="✅ я подписался", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============ helpers ============
def parse_bet_text(text, balance):
    raw = text.strip().lower()
    if raw in ("all", "всё", "все"):
        if balance <= 0: return False, 0, "у тебя 0"
        return True, balance, ""
    if raw in ("half", "половина"):
        if balance < 2: return False, 0, "мало"
        return True, balance // 2, ""
    if not raw.isdigit(): return False, 0, "число, all или half"
    bet = int(raw)
    if bet <= 0: return False, 0, "> 0"
    if bet > balance: return False, 0, f"у тебя только {balance}"
    return True, bet, ""

def in_group(m): return m.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

async def require_sub(m):
    if is_admin(m.from_user.id, m.from_user.username): return True
    miss = await missing_subs(m.from_user.id)
    if miss:
        await m.reply("🚀 сначала подпишись:", reply_markup=subs_kb(miss))
        return False
    return True


# ============ FSM ============
class PlayFSM(StatesGroup):
    waiting_bet = State()          # ждём ставку. data: game
    waiting_roulette = State()     # ждём ставку рулетки. после — выбор

class AdminFSM(StatesGroup):
    give_uid = State()
    give_amount = State()
    grant_premium_uid = State()
    grant_premium_days = State()

class SubFSM(StatesGroup):
    channel = State()
    invite = State()


# ============ клавиатуры ============
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 игры", callback_data="menu:games")],
        [InlineKeyboardButton(text="💼 бизнес", callback_data="menu:biz")],
        [InlineKeyboardButton(text="🛒 магазин", callback_data="menu:shop")],
        [InlineKeyboardButton(text="💎 премиум", callback_data="menu:prem")],
        [InlineKeyboardButton(text="🏆 топ", callback_data="menu:top")],
        [InlineKeyboardButton(text="👤 профиль", callback_data="menu:profile")],
        [InlineKeyboardButton(text="🎁 бонус", callback_data="menu:daily")],
    ])

def kb_games() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 казино", callback_data="game:casino"),
         InlineKeyboardButton(text="🎲 кости", callback_data="game:dice")],
        [InlineKeyboardButton(text="🍒 слоты", callback_data="game:slots"),
         InlineKeyboardButton(text="🪙 монетка", callback_data="game:coin")],
        [InlineKeyboardButton(text="🎡 рулетка", callback_data="game:roulette"),
         InlineKeyboardButton(text="🃏 блэкджек", callback_data="game:bj")],
        [InlineKeyboardButton(text="🚀 краш", callback_data="game:crash"),
         InlineKeyboardButton(text="💣 мины", callback_data="game:mines")],
        [InlineKeyboardButton(text="🎡 колесо", callback_data="game:wheel"),
         InlineKeyboardButton(text="📦 кейс", callback_data="game:case")],
        [InlineKeyboardButton(text="🏰 башня", callback_data="game:tower"),
         InlineKeyboardButton(text="🐎 гонка", callback_data="game:horse")],
        [InlineKeyboardButton(text="⚔️ дуэль (открытая)", callback_data="game:duel")],
        [InlineKeyboardButton(text="← назад", callback_data="menu:main")],
    ])

def kb_biz(uid) -> InlineKeyboardMarkup:
    with closing(db()) as c:
        owned = {r["biz_id"] for r in c.execute(
            "SELECT biz_id FROM businesses WHERE user_id=?", (uid,)).fetchall()}
    rows = []
    for bid, (name, price, income) in BUSINESSES.items():
        if bid in owned:
            rows.append([InlineKeyboardButton(text=f"✅ {name} — забрать", callback_data=f"biz_claim:{bid}")])
        else:
            rows.append([InlineKeyboardButton(text=f"{name} — {price:,} (+{income:,}/д)",
                                              callback_data=f"biz_buy:{bid}")])
    rows.append([InlineKeyboardButton(text="💰 забрать всё", callback_data="biz_collect")])
    rows.append([InlineKeyboardButton(text="← назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_shop() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{n} — {p:,}", callback_data=f"shop_buy:{iid}")]
            for iid, (n, p, _) in SHOP_ITEMS.items()]
    rows.append([InlineKeyboardButton(text="🎒 инвентарь", callback_data="inv_open")])
    rows.append([InlineKeyboardButton(text="← назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_premium(uid) -> InlineKeyboardMarkup:
    rows = []
    if is_premium(uid):
        rows.append([InlineKeyboardButton(text="💎 активен", callback_data="prem_noop")])
    else:
        rows.append([InlineKeyboardButton(text=f"💎 купить 30 дней — {PREMIUM_PRICE_STARS} ⭐",
                                          callback_data="prem_buy")])
    rows.append([InlineKeyboardButton(text="← назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_back(to="menu:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← назад", callback_data=to)]
    ])

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 выдать баланс", callback_data="a_give")],
        [InlineKeyboardButton(text="💎 выдать премиум", callback_data="a_grant_prem")],
        [InlineKeyboardButton(text="📢 обязательные подписки", callback_data="a_subs")],
        [InlineKeyboardButton(text="📊 статистика", callback_data="a_stats")],
    ])


# ============ главное меню ============
def menu_text(uid) -> str:
    u = get_user(uid)
    if not u: return "профиль не найден"
    prem = "💎 премиум" if is_premium(uid) else "🆓 free"
    return (
        f"🎮 <b>FunGame utils</b>\n\n"
        f"👤 {html.escape(u['first_name'] or 'игрок')}\n"
        f"💰 баланс: <b>{u['balance']:,}</b>\n"
        f"⭐ статус: {prem}\n"
        f"🏆 {u['wins']} побед • 💀 {u['losses']} проигрышей"
    )

@user_router.message(CommandStart())
async def cmd_start(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    if in_group(m):
        await m.reply(f"🎮 привет! открой меню — /menu")
    else:
        await m.answer(menu_text(m.from_user.id), reply_markup=kb_main())

@user_router.message(Command("menu"))
async def cmd_menu(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    await m.answer(menu_text(m.from_user.id), reply_markup=kb_main())

@user_router.message(Command("id"))
async def cmd_id(m: Message):
    await m.reply(f"🆔 <code>{m.from_user.id}</code>")


# ============ приветствие бота в чате ============
@user_router.my_chat_member()
async def on_bot_added(ev: ChatMemberUpdated):
    # когда бота добавили в группу/канал
    if ev.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): return
    new_status = ev.new_chat_member.status
    if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        try:
            await bot.send_message(
                ev.chat.id,
                "🎮 <b>FunGame utils</b> на борту!\n\n"
                "казино, дуэли, бизнесы, топ игроков. жми <b>/menu</b> чтобы начать.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🎮 открыть меню", callback_data="menu:main_ephemeral")],
                ]),
            )
        except Exception:
            pass


# ============ диспетчер меню ============
@user_router.callback_query(F.data == "menu:main")
async def cb_main(cb: CallbackQuery):
    ensure_user(cb.from_user.id, cb.from_user.username, cb.from_user.first_name)
    await cb.message.edit_text(menu_text(cb.from_user.id), reply_markup=kb_main())
    await cb.answer()

@user_router.callback_query(F.data == "menu:main_ephemeral")
async def cb_main_eph(cb: CallbackQuery):
    await cb.message.answer(menu_text(cb.from_user.id), reply_markup=kb_main())
    await cb.answer()

@user_router.callback_query(F.data == "menu:games")
async def cb_games(cb: CallbackQuery):
    await cb.message.edit_text("🎮 <b>игры</b>\nвыбери игру:", reply_markup=kb_games())
    await cb.answer()

@user_router.callback_query(F.data == "menu:biz")
async def cb_biz(cb: CallbackQuery):
    await cb.message.edit_text(
        f"💼 <b>бизнесы</b>\nбаланс: <b>{get_balance(cb.from_user.id):,}</b>\n\nкаждый приносит доход раз в 24ч",
        reply_markup=kb_biz(cb.from_user.id),
    )
    await cb.answer()

@user_router.callback_query(F.data == "menu:shop")
async def cb_shop(cb: CallbackQuery):
    await cb.message.edit_text(
        f"🛒 <b>магазин</b>\nбаланс: <b>{get_balance(cb.from_user.id):,}</b>",
        reply_markup=kb_shop(),
    )
    await cb.answer()

@user_router.callback_query(F.data == "menu:prem")
async def cb_prem(cb: CallbackQuery):
    uid = cb.from_user.id
    if is_premium(uid):
        u = get_user(uid)
        until = time.strftime("%Y-%m-%d", time.localtime(u["premium_until"]))
        text = (
            f"💎 <b>премиум активен</b> до <b>{until}</b>\n\n"
            f"что даёт:\n"
            f"• ежедневный бонус ×2 (<b>{DAILY_BONUS_PREMIUM:,}</b> вместо {DAILY_BONUS:,})\n"
            f"• +20% к выигрышу во всех играх\n"
            f"• VIP-значок в топе"
        )
    else:
        text = (
            f"💎 <b>премиум</b>\n\n"
            f"что даёт:\n"
            f"• ежедневный бонус ×2 (<b>{DAILY_BONUS_PREMIUM:,}</b> вместо {DAILY_BONUS:,})\n"
            f"• +20% к выигрышу во всех играх\n"
            f"• VIP-значок в топе\n\n"
            f"цена: <b>{PREMIUM_PRICE_STARS} ⭐ / 30 дней</b>"
        )
    await cb.message.edit_text(text, reply_markup=kb_premium(uid))
    await cb.answer()

@user_router.callback_query(F.data == "prem_noop")
async def cb_prem_noop(cb: CallbackQuery):
    await cb.answer("премиум уже активен", show_alert=False)

@user_router.callback_query(F.data == "prem_buy")
async def cb_prem_buy(cb: CallbackQuery):
    try:
        await bot.send_invoice(
            chat_id=cb.from_user.id,
            title="FunGame premium — 30 дней",
            description=f"премиум на 30 дней: бонус ×2, +20% к выигрышу",
            payload=f"premium:{cb.from_user.id}:30",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="FunGame premium 30d", amount=PREMIUM_PRICE_STARS)],
        )
        await cb.answer("счёт отправлен в лс")
    except Exception as e:
        await cb.answer(f"ошибка: {e}", show_alert=True)

@user_router.callback_query(F.data == "menu:top")
async def cb_top(cb: CallbackQuery):
    with closing(db()) as c:
        rows = c.execute("SELECT user_id, first_name, username, balance, premium_until FROM users ORDER BY balance DESC LIMIT 10").fetchall()
    lines = []
    for i, r in enumerate(rows, 1):
        name = r["first_name"] or r["username"] or str(r["user_id"])
        crown = "💎 " if r["premium_until"] > now_ts() else ""
        lines.append(f"{i}. {crown}<b>{html.escape(name)}</b> — {r['balance']:,}")
    text = "🏆 <b>топ по балансу</b>\n\n" + ("\n".join(lines) if lines else "пусто")
    await cb.message.edit_text(text, reply_markup=kb_back())
    await cb.answer()

@user_router.callback_query(F.data == "menu:profile")
async def cb_profile(cb: CallbackQuery):
    u = get_user(cb.from_user.id)
    if not u:
        ensure_user(cb.from_user.id, cb.from_user.username, cb.from_user.first_name)
        u = get_user(cb.from_user.id)
    total = u["wins"] + u["losses"]
    wr = f"{u['wins']*100//total}%" if total else "—"
    prem = "💎 премиум" if is_premium(cb.from_user.id) else "🆓 free"
    text = (
        f"👤 <b>{html.escape(u['first_name'] or 'игрок')}</b>\n"
        f"🆔 <code>{u['user_id']}</code>\n"
        f"⭐ {prem}\n"
        f"💰 баланс: <b>{u['balance']:,}</b>\n"
        f"🏆 {u['wins']} • 💀 {u['losses']} • winrate <b>{wr}</b>\n"
        f"🎲 проставлено: <b>{u['wagered']:,}</b>\n"
        f"💵 выиграно: <b>{u['won_total']:,}</b>"
    )
    await cb.message.edit_text(text, reply_markup=kb_back())
    await cb.answer()

@user_router.callback_query(F.data == "menu:daily")
async def cb_daily(cb: CallbackQuery):
    u = get_user(cb.from_user.id)
    if now_ts() - u["last_bonus"] < 86400:
        left = 86400 - (now_ts() - u["last_bonus"])
        return await cb.answer(f"через {left//3600}ч {(left%3600)//60}м", show_alert=True)
    bonus = DAILY_BONUS_PREMIUM if is_premium(cb.from_user.id) else DAILY_BONUS
    add_balance(cb.from_user.id, bonus)
    with closing(db()) as c:
        c.execute("UPDATE users SET last_bonus=? WHERE user_id=?", (now_ts(), cb.from_user.id))
        c.commit()
    await cb.answer(f"🎁 +{bonus:,} коинов", show_alert=True)
    try:
        await cb.message.edit_text(menu_text(cb.from_user.id), reply_markup=kb_main())
    except Exception:
        pass


# ============ игра: выбор ставки через FSM ============
GAME_NAMES = {
    "casino": "🎰 казино", "dice": "🎲 кости", "slots": "🍒 слоты",
    "coin": "🪙 монетка", "roulette": "🎡 рулетка", "bj": "🃏 блэкджек",
    "crash": "🚀 краш", "mines": "💣 мины", "wheel": "🎡 колесо",
    "case": "📦 кейс", "tower": "🏰 башня", "horse": "🐎 гонка",
    "duel": "⚔️ дуэль",
}

@user_router.callback_query(F.data.startswith("game:"))
async def cb_game(cb: CallbackQuery, state: FSMContext):
    game = cb.data.split(":", 1)[1]
    if game == "duel":
        await cb.answer()
        return await cb.message.edit_text(
            "⚔️ <b>открытая дуэль</b>\n\n"
            "напиши в чат: <code>/duel 500</code>\n"
            "любой желающий сможет принять вызов кнопкой.",
            reply_markup=kb_back("menu:games"),
        )
    await state.set_state(PlayFSM.waiting_bet)
    await state.update_data(game=game)
    label = GAME_NAMES.get(game, game)
    await cb.message.edit_text(
        f"{label}\n\nвведи ставку ответом на это сообщение:\n"
        f"<code>100</code> — число\n<code>all</code> — всё\n<code>half</code> — половина\n\n"
        f"твой баланс: <b>{get_balance(cb.from_user.id):,}</b>",
        reply_markup=kb_back("menu:games"),
    )
    await cb.answer()

@user_router.message(PlayFSM.waiting_bet)
async def fsm_bet(m: Message, state: FSMContext):
    data = await state.get_data()
    game = data.get("game")
    await state.clear()
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet_text(m.text or "", bal)
    if not ok:
        return await m.reply(f"❌ {err}\n/menu чтобы начать заново")
    # запуск игры
    await run_game(m, game, bet)


async def run_game(m: Message, game: str, bet: int):
    uid = m.from_user.id
    pmult = premium_multiplier(uid)
    tag = " 💎" if pmult > 1 else ""

    if game == "casino":
        roll = random.randint(0, 36)
        record_wager(uid, bet)
        if roll == 0:
            win = int(bet * 14 * pmult)
            add_balance(uid, win - bet); record_win(uid, win)
            return await m.reply(f"🎰 <b>зеро!</b> ×14{tag} → <b>+{win-bet:,}</b>\nбаланс: <b>{get_balance(uid):,}</b>")
        if roll % 2 == 0:
            win = int(bet * 2 * pmult)
            add_balance(uid, win - bet); record_win(uid, win)
            return await m.reply(f"🎰 {roll} чёт{tag} → <b>+{win-bet:,}</b>\nбаланс: <b>{get_balance(uid):,}</b>")
        add_balance(uid, -bet); record_loss(uid)
        return await m.reply(f"🎰 {roll} нечет → <b>-{bet:,}</b>\nбаланс: <b>{get_balance(uid):,}</b>")

    if game == "dice":
        you, bot_roll = random.randint(1,6), random.randint(1,6)
        record_wager(uid, bet)
        if you > bot_roll:
            win = int(bet * 2 * pmult)
            add_balance(uid, win - bet); record_win(uid, win)
            return await m.reply(f"🎲 ты {you} • бот {bot_roll}{tag}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")
        if you < bot_roll:
            add_balance(uid, -bet); record_loss(uid)
            return await m.reply(f"🎲 ты {you} • бот {bot_roll}\n💀 -{bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")
        return await m.reply(f"🎲 ничья {you}={bot_roll}")

    if game == "slots":
        icons = ["🍒","🍋","💎","7️⃣","⭐","🔔"]
        s = [random.choice(icons) for _ in range(3)]
        line = " | ".join(s)
        record_wager(uid, bet)
        if s[0]==s[1]==s[2]:
            mult = 20 if s[0]=="7️⃣" else 10
            win = int(bet * mult * pmult)
            add_balance(uid, win - bet); record_win(uid, win)
            return await m.reply(f"🎰 {line}\n🔥 ×{mult}{tag} → <b>+{win-bet:,}</b>\nбаланс: <b>{get_balance(uid):,}</b>")
        if s[0]==s[1] or s[1]==s[2] or s[0]==s[2]:
            win = int(bet * 2 * pmult)
            add_balance(uid, win - bet); record_win(uid, win)
            return await m.reply(f"🎰 {line}\nпара{tag} → <b>+{win-bet:,}</b>\nбаланс: <b>{get_balance(uid):,}</b>")
        add_balance(uid, -bet); record_loss(uid)
        return await m.reply(f"🎰 {line}\n💀 -{bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")

    if game == "coin":
        res = random.choice(["орел","решка"])
        pick = random.choice(["орел","решка"])
        record_wager(uid, bet)
        if pick == res:
            win = int(bet * 2 * pmult)
            add_balance(uid, win - bet); record_win(uid, win)
            return await m.reply(f"🪙 {res}{tag}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")
        add_balance(uid, -bet); record_loss(uid)
        return await m.reply(f"🪙 {res}\n💀 -{bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")

    if game == "roulette":
        roll = random.randint(0, 36)
        record_wager(uid, bet)
        pick = random.choice(["красное","чёрное","чёт","нечет"])
        mult = 0
        if pick == "красное" and roll in RED_NUMBERS: mult = 2
        elif pick == "чёрное" and roll != 0 and roll not in RED_NUMBERS: mult = 2
        elif pick == "чёт" and roll != 0 and roll % 2 == 0: mult = 2
        elif pick == "нечет" and roll % 2 == 1: mult = 2
        color = "🟢" if roll == 0 else ("🔴" if roll in RED_NUMBERS else "⚫")
        if mult:
            win = int(bet * mult * pmult)
            add_balance(uid, win - bet); record_win(uid, win)
            return await m.reply(f"🎡 {color} {roll}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")
        add_balance(uid, -bet); record_loss(uid)
        return await m.reply(f"🎡 {color} {roll}\n💀 -{bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")

    if game == "bj":
        player = [bj_draw(), bj_draw()]
        dealer = [bj_draw(), bj_draw()]
        active_bj[uid] = {"bet": bet, "player": player, "dealer": dealer, "chat_id": m.chat.id}
        return await m.reply(
            f"🃏 <b>блэкджек</b> ставка {bet:,}{tag}\n\n"
            f"твои: {' '.join(map(str,player))} = <b>{bj_score(player)}</b>\n"
            f"дилер: {dealer[0]} ?",
            reply_markup=bj_kb(uid),
        )

    if game == "crash":
        crash_at = round(max(1.0, random.expovariate(1/1.5)), 2)
        target = 2.0
        record_wager(uid, bet)
        if crash_at >= target:
            win = int(bet * target * pmult)
            add_balance(uid, win - bet); record_win(uid, win)
            return await m.reply(f"🚀 ×{crash_at}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")
        add_balance(uid, -bet); record_loss(uid)
        return await m.reply(f"🚀 рухнуло ×{crash_at}\n💀 -{bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")

    if game == "mines":
        grid = [1]*22 + [0]*3
        random.shuffle(grid)
        active_mines[uid] = {"bet": bet, "grid": grid, "revealed": set(), "chat_id": m.chat.id}
        return await m.reply(
            f"💣 <b>мины</b> ставка {bet:,}{tag}\n3 мины на 25, шаг ×1.2",
            reply_markup=mines_kb(uid, set(), grid),
        )

    if game == "wheel":
        seg = random.choices([0,1.2,1.5,2,3,5,10,0.5], weights=[30,25,15,12,8,5,2,3])[0]
        record_wager(uid, bet)
        if seg >= 1:
            win = int(bet * seg * pmult)
            add_balance(uid, win - bet); record_win(uid, win)
            return await m.reply(f"🎡 ×{seg}{tag}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")
        add_balance(uid, -bet); record_loss(uid)
        return await m.reply(f"🎡 ×{seg}\n💀 -{bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")

    if game == "case":
        r = random.random()
        if r < 0.01: mult, tag2 = 50, "🌟 джекпот"
        elif r < 0.05: mult, tag2 = 10, "🔥 rare"
        elif r < 0.15: mult, tag2 = 3, "✨ uncommon"
        elif r < 0.40: mult, tag2 = 1.5, "🟢 common"
        else: mult, tag2 = 0, "💀 пусто"
        record_wager(uid, bet)
        if mult:
            win = int(bet * mult * pmult)
            add_balance(uid, win - bet); record_win(uid, win)
            return await m.reply(f"📦 {tag2} ×{mult}{tag}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")
        add_balance(uid, -bet); record_loss(uid)
        return await m.reply(f"📦 {tag2}\n💀 -{bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")

    if game == "tower":
        mult = 1.0
        for floor in range(5):
            if random.random() < 0.35:
                add_balance(uid, -bet); record_wager(uid, bet); record_loss(uid)
                return await m.reply(f"🏰 упал на {floor+1} (×{mult:.2f})\n💀 -{bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")
            mult *= 1.5
        win = int(bet * mult * pmult)
        add_balance(uid, win - bet); record_wager(uid, bet); record_win(uid, win)
        return await m.reply(f"🏰 прошёл ×{mult:.2f}{tag}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")

    if game == "horse":
        pick = random.randint(1, 6)
        winner = random.choices(range(1,7), weights=[10,15,25,25,15,10])[0]
        record_wager(uid, bet)
        if winner == pick:
            win = int(bet * 3 * pmult)
            add_balance(uid, win - bet); record_win(uid, win)
            return await m.reply(f"🐎 №{winner} — твоя!{tag}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")
        add_balance(uid, -bet); record_loss(uid)
        return await m.reply(f"🐎 №{winner}, ты ставил №{pick}\n💀 -{bet:,}\nбаланс: <b>{get_balance(uid):,}</b>")


# ============ блэкджек / мины helper'ы ============
active_bj: dict[int, dict] = {}
active_mines: dict[int, dict] = {}

def bj_score(hand):
    s, aces = sum(hand), hand.count(11)
    while s > 21 and aces:
        s -= 10; aces -= 1
    return s

def bj_draw(): return random.choice([2,3,4,5,6,7,8,9,10,10,10,10,11])

def bj_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🃏 взять", callback_data=f"bj_hit:{uid}"),
        InlineKeyboardButton(text="✋ стоп", callback_data=f"bj_stand:{uid}"),
    ]])

@user_router.callback_query(F.data.startswith("bj_hit:"))
async def cb_bj_hit(cb: CallbackQuery):
    uid = int(cb.data.split(":",1)[1])
    if cb.from_user.id != uid: return await cb.answer("не твоя игра", show_alert=True)
    st = active_bj.get(uid)
    if not st: return await cb.answer("нет игры", show_alert=True)
    st["player"].append(bj_draw())
    score = bj_score(st["player"])
    if score > 21:
        add_balance(uid, -st["bet"]); record_wager(uid, st["bet"]); record_loss(uid)
        active_bj.pop(uid, None)
        return await cb.message.edit_text(f"🃏 перебор {score}\n💀 -{st['bet']:,}\nбаланс: <b>{get_balance(uid):,}</b>")
    await cb.message.edit_text(
        f"🃏 <b>блэкджек</b> ставка {st['bet']:,}\n\n"
        f"твои: {' '.join(map(str,st['player']))} = <b>{score}</b>\n"
        f"дилер: {st['dealer'][0]} ?",
        reply_markup=bj_kb(uid),
    )
    await cb.answer()

@user_router.callback_query(F.data.startswith("bj_stand:"))
async def cb_bj_stand(cb: CallbackQuery):
    uid = int(cb.data.split(":",1)[1])
    if cb.from_user.id != uid: return await cb.answer("не твоя игра", show_alert=True)
    st = active_bj.get(uid)
    if not st: return await cb.answer("нет игры", show_alert=True)
    while bj_score(st["dealer"]) < 17:
        st["dealer"].append(bj_draw())
    ps, ds = bj_score(st["player"]), bj_score(st["dealer"])
    pmult = premium_multiplier(uid)
    record_wager(uid, st["bet"])
    if ds > 21 or ps > ds:
        win = int(st["bet"] * 2 * pmult)
        add_balance(uid, win - st["bet"]); record_win(uid, win)
        result = f"🏆 победа! <b>+{win-st['bet']:,}</b>"
    elif ps == ds:
        result = "🤝 ничья"
    else:
        add_balance(uid, -st["bet"]); record_loss(uid)
        result = f"💀 проигрыш <b>-{st['bet']:,}</b>"
    active_bj.pop(uid, None)
    await cb.message.edit_text(
        f"🃏 <b>блэкджек</b>\n\n"
        f"твои: {' '.join(map(str,st['player']))} = <b>{ps}</b>\n"
        f"дилер: {' '.join(map(str,st['dealer']))} = <b>{ds}</b>\n\n"
        f"{result}\nбаланс: <b>{get_balance(uid):,}</b>"
    )
    await cb.answer()

def mines_kb(uid, revealed, grid):
    rows = []
    for r in range(5):
        row = []
        for c in range(5):
            i = r*5+c
            txt = ("💎" if grid[i] else "💥") if i in revealed else "⬜"
            row.append(InlineKeyboardButton(text=txt, callback_data=f"mn:{uid}:{i}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="💰 забрать", callback_data=f"mn_cash:{uid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@user_router.callback_query(F.data.startswith("mn:"))
async def cb_mines(cb: CallbackQuery):
    _, uid_s, i_s = cb.data.split(":")
    uid, i = int(uid_s), int(i_s)
    if cb.from_user.id != uid: return await cb.answer("не твоя", show_alert=True)
    st = active_mines.get(uid)
    if not st: return await cb.answer("нет игры", show_alert=True)
    if i in st["revealed"]: return await cb.answer("уже открыто")
    st["revealed"].add(i)
    if st["grid"][i] == 0:
        add_balance(uid, -st["bet"]); record_wager(uid, st["bet"]); record_loss(uid)
        kb = mines_kb(uid, st["revealed"], st["grid"])
        active_mines.pop(uid, None)
        return await cb.message.edit_text(f"💥 мина! -{st['bet']:,}\nбаланс: <b>{get_balance(uid):,}</b>", reply_markup=kb)
    mult = 1.2 ** len(st["revealed"])
    await cb.message.edit_text(
        f"💣 ×{mult:.2f} • выигрыш <b>{int(st['bet']*mult*premium_multiplier(uid)):,}</b>",
        reply_markup=mines_kb(uid, st["revealed"], st["grid"]),
    )
    await cb.answer()

@user_router.callback_query(F.data.startswith("mn_cash:"))
async def cb_mines_cash(cb: CallbackQuery):
    uid = int(cb.data.split(":",1)[1])
    if cb.from_user.id != uid: return await cb.answer("не твоя", show_alert=True)
    st = active_mines.get(uid)
    if not st: return await cb.answer("нет игры", show_alert=True)
    if not st["revealed"]: return await cb.answer("открой хотя бы одну", show_alert=True)
    mult = 1.2 ** len(st["revealed"])
    win = int(st["bet"] * mult * premium_multiplier(uid))
    add_balance(uid, win - st["bet"]); record_wager(uid, st["bet"]); record_win(uid, win)
    kb = mines_kb(uid, st["revealed"], st["grid"])
    active_mines.pop(uid, None)
    await cb.message.edit_text(f"💰 ×{mult:.2f} → <b>+{win-st['bet']:,}</b>\nбаланс: <b>{get_balance(uid):,}</b>", reply_markup=kb)
    await cb.answer()


# ============ бизнес ============
@user_router.callback_query(F.data.startswith("biz_buy:"))
async def cb_biz_buy(cb: CallbackQuery):
    bid = cb.data.split(":",1)[1]
    if bid not in BUSINESSES: return await cb.answer("нет такого", show_alert=True)
    name, price, income = BUSINESSES[bid]
    if get_balance(cb.from_user.id) < price:
        return await cb.answer(f"нужно {price:,}", show_alert=True)
    with closing(db()) as c:
        row = c.execute("SELECT 1 FROM businesses WHERE user_id=? AND biz_id=?",
                        (cb.from_user.id, bid)).fetchone()
        if row: return await cb.answer("уже куплен", show_alert=True)
        add_balance(cb.from_user.id, -price)
        c.execute("INSERT INTO businesses(user_id, biz_id, bought_at, last_pay) VALUES(?,?,?,?)",
                  (cb.from_user.id, bid, now_ts(), now_ts()))
        c.commit()
    await cb.answer(f"куплено: {name}", show_alert=True)
    await cb.message.edit_reply_markup(reply_markup=kb_biz(cb.from_user.id))

@user_router.callback_query(F.data.startswith("biz_claim:"))
async def cb_biz_claim(cb: CallbackQuery):
    bid = cb.data.split(":",1)[1]
    if bid not in BUSINESSES: return await cb.answer("нет такого", show_alert=True)
    name, price, income = BUSINESSES[bid]
    with closing(db()) as c:
        row = c.execute("SELECT last_pay FROM businesses WHERE user_id=? AND biz_id=?",
                        (cb.from_user.id, bid)).fetchone()
    if not row: return await cb.answer("не куплен", show_alert=True)
    elapsed = now_ts() - row["last_pay"]
    if elapsed < 86400:
        left = 86400 - elapsed
        return await cb.answer(f"через {left//3600}ч {(left%3600)//60}м", show_alert=True)
    periods = elapsed // 86400
    amount = income * periods
    add_balance(cb.from_user.id, amount)
    with closing(db()) as c:
        c.execute("UPDATE businesses SET last_pay=? WHERE user_id=? AND biz_id=?",
                  (now_ts(), cb.from_user.id, bid))
        c.commit()
    await cb.answer(f"+{amount:,}", show_alert=True)
    await cb.message.edit_reply_markup(reply_markup=kb_biz(cb.from_user.id))

@user_router.callback_query(F.data == "biz_collect")
async def cb_biz_collect(cb: CallbackQuery):
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM businesses WHERE user_id=?", (cb.from_user.id,)).fetchall()
    if not rows: return await cb.answer("нет бизнесов", show_alert=True)
    total = 0
    for r in rows:
        elapsed = now_ts() - r["last_pay"]
        if elapsed >= 86400:
            total += BUSINESSES[r["biz_id"]][2] * (elapsed // 86400)
    if total == 0: return await cb.answer("пока нечего", show_alert=True)
    add_balance(cb.from_user.id, total)
    with closing(db()) as c:
        c.execute("UPDATE businesses SET last_pay=? WHERE user_id=?", (now_ts(), cb.from_user.id))
        c.commit()
    await cb.answer(f"+{total:,}", show_alert=True)
    await cb.message.edit_reply_markup(reply_markup=kb_biz(cb.from_user.id))


# ============ магазин / инвентарь ============
@user_router.callback_query(F.data.startswith("shop_buy:"))
async def cb_shop_buy(cb: CallbackQuery):
    iid = cb.data.split(":",1)[1]
    if iid not in SHOP_ITEMS: return await cb.answer("нет такого", show_alert=True)
    name, price, _ = SHOP_ITEMS[iid]
    if get_balance(cb.from_user.id) < price:
        return await cb.answer(f"нужно {price:,}", show_alert=True)
    add_balance(cb.from_user.id, -price)
    with closing(db()) as c:
        c.execute("""INSERT INTO inventory(user_id, item_id, qty) VALUES(?,?,1)
                     ON CONFLICT(user_id, item_id) DO UPDATE SET qty=qty+1""",
                  (cb.from_user.id, iid))
        c.commit()
    await cb.answer(f"куплено: {name}", show_alert=True)

@user_router.callback_query(F.data == "inv_open")
async def cb_inv(cb: CallbackQuery):
    with closing(db()) as c:
        rows = c.execute("SELECT item_id, qty FROM inventory WHERE user_id=?",
                         (cb.from_user.id,)).fetchall()
    if not rows:
        return await cb.answer("инвентарь пуст", show_alert=True)
    text = "🎒 <b>инвентарь</b>\n\n" + "\n".join(
        f"• {SHOP_ITEMS[r['item_id']][0]} ×{r['qty']}" for r in rows if r["item_id"] in SHOP_ITEMS
    )
    await cb.message.edit_text(text, reply_markup=kb_back("menu:shop"))
    await cb.answer()


# ============ ДУЭЛЬ: ОТКРЫТАЯ, ЛЮБОЙ МОЖЕТ ПРИНЯТЬ ============
# ключ: duel_id (str) -> state
active_duels: dict[str, dict] = {}
user_in_duel: dict[int, str] = {}     # uid -> duel_id

def _open_duel_text(d: dict) -> str:
    cid = d["challenger"]
    name = html.escape(d["challenger_name"])
    return (
        f"⚔️ <b>открытый вызов на дуэль</b>\n\n"
        f"<b>{name}</b> вызывает любого желающего!\n"
        f"ставка: <b>{d['bet']:,}</b> с каждого\n"
        f"банк: <b>{d['bet']*2:,}</b>\n\n"
        f"первый, кто нажмёт «⚔️ принять» — станет соперником.\n"
        f"<i>автор вызова не может принять свою же дуэль.</i>"
    )

def _open_duel_kb(duel_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ принять", callback_data=f"duel:accept:{duel_id}")],
        [InlineKeyboardButton(text="❌ отменить", callback_data=f"duel:cancel:{duel_id}")],
    ])

def _in_progress_text(d: dict) -> str:
    cn = html.escape(d["challenger_name"])
    tn = html.escape(d["target_name"])
    turn = cn if d["turn"] == d["challenger"] else tn
    return (
        f"⚔️ <b>русская рулетка</b>\n"
        f"{cn} vs {tn} • банк <b>{d['bet']*2:,}</b>\n\n"
        f"🎯 ход: <b>{turn}</b>\n"
        f"🔫 выстрелов: <b>{d['shots']}</b>\n"
        f"🌀 барабан: 6 гнёзд, 1 патрон"
    )

def _in_progress_kb(duel_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔫 в себя",     callback_data=f"duel:self:{duel_id}"),
        InlineKeyboardButton(text="🔫 в соперника", callback_data=f"duel:enemy:{duel_id}"),
    ]])

@user_router.message(Command("duel"))
async def cmd_duel(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    if not in_group(m):
        return await m.reply("дуэль только в группах")
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.reply("usage: <code>/duel 500</code>")
    bet = int(parts[1])
    if bet <= 0: return await m.reply("ставка > 0")
    if get_balance(m.from_user.id) < bet:
        return await m.reply(f"у тебя <b>{get_balance(m.from_user.id):,}</b>")
    if m.from_user.id in user_in_duel:
        return await m.reply("ты уже в дуэли")

    duel_id = uuid.uuid4().hex[:8]
    d = {
        "id": duel_id,
        "challenger": m.from_user.id,
        "challenger_name": m.from_user.first_name or str(m.from_user.id),
        "target": None, "target_name": None,
        "bet": bet, "state": "open", "shots": 0, "turn": None,
        "chamber": [1]+[0]*5, "pos": 0,
        "chat_id": m.chat.id,
        "message_id": None,
    }
    random.shuffle(d["chamber"])
    active_duels[duel_id] = d
    user_in_duel[m.from_user.id] = duel_id
    sent = await m.reply(_open_duel_text(d), reply_markup=_open_duel_kb(duel_id))
    d["message_id"] = sent.message_id

@user_router.callback_query(F.data.startswith("duel:"))
async def cb_duel(cb: CallbackQuery):
    parts = cb.data.split(":")
    if len(parts) < 3: return await cb.answer()
    action, duel_id = parts[1], parts[2]
    d = active_duels.get(duel_id)
    if not d: return await cb.answer("дуэль не найдена", show_alert=True)
    uid = cb.from_user.id

    # === accept / cancel в открытом состоянии ===
    if d["state"] == "open":
        if action == "cancel":
            if uid != d["challenger"]:
                return await cb.answer("только автор может отменить", show_alert=True)
            user_in_duel.pop(d["challenger"], None)
            active_duels.pop(duel_id, None)
            return await cb.message.edit_text("❌ вызов отменён"), await cb.answer()
        if action == "accept":
            if uid == d["challenger"]:
                return await cb.answer("нельзя принять свою же дуэль", show_alert=True)
            if uid in user_in_duel:
                return await cb.answer("ты уже в другой дуэли", show_alert=True)
            ensure_user(uid, cb.from_user.username, cb.from_user.first_name)
            if get_balance(uid) < d["bet"]:
                return await cb.answer(f"нужно {d['bet']:,} коинов", show_alert=True)
            if get_balance(d["challenger"]) < d["bet"]:
                active_duels.pop(duel_id, None); user_in_duel.pop(d["challenger"], None)
                return await cb.answer("у автора уже нет коинов", show_alert=True)
            # старт
            d["target"] = uid
            d["target_name"] = cb.from_user.first_name or str(uid)
            d["state"] = "progress"
            d["turn"] = random.choice([d["challenger"], uid])
            add_balance(d["challenger"], -d["bet"])
            add_balance(uid, -d["bet"])
            user_in_duel[uid] = duel_id
            await cb.message.edit_text(_in_progress_text(d), reply_markup=_in_progress_kb(duel_id))
            return await cb.answer("принято! чей ход — у того кнопки")

    # === выстрелы ===
    if d["state"] != "progress":
        return await cb.answer("дуэль не активна", show_alert=True)
    if uid != d["turn"]:
        return await cb.answer("не твой ход", show_alert=True)
    if action not in ("self", "enemy"):
        return await cb.answer()

    pos = d["pos"] % len(d["chamber"])
    fired = d["chamber"][pos] == 1
    d["shots"] += 1
    d["pos"] = (pos + 1) % len(d["chamber"])
    if d["pos"] == 0:
        ch = [1]+[0]*5; random.shuffle(ch); d["chamber"] = ch

    opponent = d["target"] if uid == d["challenger"] else d["challenger"]
    if fired:
        loser = uid if action == "self" else opponent
        winner = d["target"] if loser == d["challenger"] else d["challenger"]
        bank = d["bet"] * 2
        add_balance(winner, bank)
        record_wager(winner, d["bet"]); record_win(winner, bank)
        record_wager(loser, d["bet"]);  record_loss(loser)
        wname = html.escape(d["challenger_name"] if winner == d["challenger"] else d["target_name"])
        lname = html.escape(d["challenger_name"] if loser == d["challenger"] else d["target_name"])
        user_in_duel.pop(d["challenger"], None); user_in_duel.pop(d["target"], None)
        active_duels.pop(duel_id, None)
        return await cb.message.edit_text(
            f"💥 <b>БАБАХ!</b>\n\n{lname} — мёртв(а) 💀\n"
            f"🏆 победил(а) <b>{wname}</b>\n"
            f"банк <b>{bank:,}</b>\n"
            f"баланс: <b>{get_balance(winner):,}</b>"
        ), await cb.answer()

    d["turn"] = opponent
    await cb.message.edit_text(
        "🌀 <i>щёлк, осечка...</i>\n\n" + _in_progress_text(d),
        reply_markup=_in_progress_kb(duel_id),
    )
    await cb.answer()


# ============ topup / звёзды ============
@user_router.callback_query(F.data == "topup_open")
async def cb_topup(cb: CallbackQuery):
    rows = [[InlineKeyboardButton(text=f"⭐ {s} — {c:,} коинов", callback_data=f"buy_stars:{s}")]
            for s, c in STAR_PACKS]
    rows.append([InlineKeyboardButton(text="← назад", callback_data="menu:main")])
    await cb.message.edit_text(f"💎 курс: <b>1 ⭐ = {COINS_PER_STAR} коинов</b>",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@user_router.callback_query(F.data.startswith("buy_stars:"))
async def cb_buy_stars(cb: CallbackQuery):
    stars = int(cb.data.split(":",1)[1])
    coins = next((c for s, c in STAR_PACKS if s == stars), stars * COINS_PER_STAR)
    try:
        await bot.send_invoice(
            chat_id=cb.from_user.id,
            title="FunGame utils — пополнение",
            description=f"{coins:,} коинов за {stars} ⭐",
            payload=f"topup:{cb.from_user.id}:{coins}",
            provider_token="", currency="XTR",
            prices=[LabeledPrice(label=f"{coins} коинов", amount=stars)],
        )
        await cb.answer("счёт отправлен в лс")
    except Exception as e:
        await cb.answer(f"ошибка: {e}", show_alert=True)

@user_router.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@user_router.message(F.successful_payment)
async def on_paid(m: Message):
    payload = m.successful_payment.invoice_payload or ""
    try:
        parts = payload.split(":")
        kind = parts[0]
        uid = int(parts[1])
        if kind == "topup":
            coins = int(parts[2])
            add_balance(uid, coins)
            return await m.reply(f"✅ зачислено <b>{coins:,}</b>\nбаланс: <b>{get_balance(uid):,}</b>")
        if kind == "premium":
            days = int(parts[2])
            u = get_user(uid)
            base = max(now_ts(), u["premium_until"])
            new_until = base + days * 86400
            with closing(db()) as c:
                c.execute("UPDATE users SET premium_until=? WHERE user_id=?", (new_until, uid))
                c.commit()
            until_str = time.strftime("%Y-%m-%d", time.localtime(new_until))
            return await m.reply(f"💎 премиум активирован до <b>{until_str}</b>\n+50% к бонусу, +20% к выигрышу")
    except Exception:
        pass
    await m.reply("оплата прошла, но payload битый — напиши @" + OWNER_USERNAME)


# ============ подписки ============
@user_router.callback_query(F.data == "check_subs")
async def cb_check_subs(cb: CallbackQuery):
    miss = await missing_subs(cb.from_user.id)
    if miss:
        await cb.answer("ещё не всё подписано", show_alert=True)
        try: await cb.message.edit_reply_markup(reply_markup=subs_kb(miss))
        except Exception: pass
    else:
        await cb.message.edit_text("✅ подписки подтверждены", reply_markup=kb_main())
    await cb.answer()


# ============ ADMIN ============
@admin_router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id, m.from_user.username): return
    await m.answer(f"🎮 <b>FunGame utils</b> — админка @{OWNER_USERNAME}", reply_markup=kb_admin())

@admin_router.callback_query(F.data == "a_back")
async def cb_aback(cb: CallbackQuery):
    if not is_admin(cb.from_user.id, cb.from_user.username):
        return await cb.answer("нет доступа", show_alert=True)
    await cb.message.edit_text("админ-меню:", reply_markup=kb_admin())
    await cb.answer()

@admin_router.callback_query(F.data == "a_stats")
async def cb_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id, cb.from_user.username):
        return await cb.answer("нет доступа", show_alert=True)
    with closing(db()) as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        total_money = c.execute("SELECT COALESCE(SUM(balance),0) n FROM users").fetchone()["n"]
        subs = c.execute("SELECT COUNT(*) n FROM forced_subs").fetchone()["n"]
        total_wagered = c.execute("SELECT COALESCE(SUM(wagered),0) n FROM users").fetchone()["n"]
        prem = c.execute("SELECT COUNT(*) n FROM users WHERE premium_until>?", (now_ts(),)).fetchone()["n"]
    await cb.message.edit_text(
        f"📊 <b>статистика</b>\n\n"
        f"юзеров: {users}\nпремиум: {prem}\n"
        f"коинов: <b>{total_money:,}</b>\n"
        f"проставлено: <b>{total_wagered:,}</b>\n"
        f"форс-сабов: {subs}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="← назад", callback_data="a_back")]
        ]),
    )
    await cb.answer()

@admin_router.callback_query(F.data == "a_give")
async def cb_give(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id, cb.from_user.username):
        return await cb.answer("нет доступа", show_alert=True)
    await state.set_state(AdminFSM.give_uid)
    await cb.message.edit_text("введи user_id или @username:")
    await cb.answer()

@admin_router.message(AdminFSM.give_uid)
async def adm_uid(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id, m.from_user.username): return
    val = (m.text or "").strip()
    if val.startswith("@"):
        with closing(db()) as c:
            row = c.execute("SELECT user_id FROM users WHERE username=?", (val.lstrip("@"),)).fetchone()
        if not row: return await m.answer("не нашёл")
        uid = row["user_id"]
    elif val.lstrip("-").isdigit():
        uid = int(val)
    else:
        return await m.answer("числовой id или @username")
    await state.update_data(uid=uid)
    await state.set_state(AdminFSM.give_amount)
    await m.answer(f"юзер {uid}. введи сумму (+500 или -500):")

@admin_router.message(AdminFSM.give_amount)
async def adm_amount(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id, m.from_user.username): return
    val = (m.text or "").strip()
    if not val.lstrip("-").isdigit(): return await m.answer("число")
    amount = int(val)
    data = await state.get_data(); uid = data["uid"]; await state.clear()
    add_balance(uid, amount)
    await m.answer(f"✅ {amount:+,} → {uid}\nбаланс: <b>{get_balance(uid):,}</b>")
    try:
        await bot.send_message(uid, f"💰 админ начислил <b>{amount:+,}</b>\nбаланс: <b>{get_balance(uid):,}</b>")
    except Exception: pass


# --- премиум выдача ---
@admin_router.callback_query(F.data == "a_grant_prem")
async def cb_grant_prem(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id, cb.from_user.username):
        return await cb.answer("нет доступа", show_alert=True)
    await state.set_state(AdminFSM.grant_premium_uid)
    await cb.message.edit_text("введи user_id или @username для премиума:")
    await cb.answer()

@admin_router.message(AdminFSM.grant_premium_uid)
async def adm_prem_uid(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id, m.from_user.username): return
    val = (m.text or "").strip()
    if val.startswith("@"):
        with closing(db()) as c:
            row = c.execute("SELECT user_id FROM users WHERE username=?", (val.lstrip("@"),)).fetchone()
        if not row: return await m.answer("не нашёл")
        uid = row["user_id"]
    elif val.lstrip("-").isdigit():
        uid = int(val)
    else:
        return await m.answer("числовой id или @username")
    await state.update_data(uid=uid)
    await state.set_state(AdminFSM.grant_premium_days)
    await m.answer(f"юзер {uid}. введи дней премиума (30, или -1 чтобы снять):")

@admin_router.message(AdminFSM.grant_premium_days)
async def adm_prem_days(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id, m.from_user.username): return
    val = (m.text or "").strip()
    if not val.lstrip("-").isdigit(): return await m.answer("число")
    days = int(val)
    data = await state.get_data(); uid = data["uid"]; await state.clear()
    if days < 0:
        with closing(db()) as c:
            c.execute("UPDATE users SET premium_until=0 WHERE user_id=?", (uid,))
            c.commit()
        await m.answer(f"премиум снят с {uid}")
        try: await bot.send_message(uid, "💎 премиум отключён")
        except Exception: pass
    else:
        u = get_user(uid)
        base = max(now_ts(), u["premium_until"] if u else now_ts())
        new_until = base + days * 86400
        with closing(db()) as c:
            c.execute("UPDATE users SET premium_until=? WHERE user_id=?", (new_until, uid))
            c.commit()
        until_str = time.strftime("%Y-%m-%d", time.localtime(new_until))
        await m.answer(f"💎 премиум выдан {uid} до {until_str}")
        try: await bot.send_message(uid, f"💎 тебе выдан премиум до <b>{until_str}</b>")
        except Exception: pass


# --- форс-сабы ---
@admin_router.callback_query(F.data == "a_subs")
async def cb_subs(cb: CallbackQuery):
    if not is_admin(cb.from_user.id, cb.from_user.username):
        return await cb.answer("нет доступа", show_alert=True)
    with closing(db()) as c:
        subs = c.execute("SELECT * FROM forced_subs").fetchall()
    text = "📢 <b>обязательные подписки</b>\n\n"
    text += "пусто\n" if not subs else "".join(
        f"• {s['title'] or s['channel']} — <code>{s['channel']}</code>\n" for s in subs)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ добавить", callback_data="a_sub_add")],
        [InlineKeyboardButton(text="🗑 удалить", callback_data="a_sub_del")],
        [InlineKeyboardButton(text="← назад", callback_data="a_back")],
    ])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()

@admin_router.callback_query(F.data == "a_sub_add")
async def cb_sub_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id, cb.from_user.username):
        return await cb.answer("нет доступа", show_alert=True)
    await state.set_state(SubFSM.channel)
    await cb.message.edit_text("кинь @username канала или -100... id. бот должен быть админом.")
    await cb.answer()

@admin_router.message(SubFSM.channel)
async def sub_channel(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id, m.from_user.username): return
    ch = (m.text or "").strip()
    if not ch.startswith("@") and not ch.startswith("-100"):
        return await m.answer("@username или -100...")
    try:
        chat = await bot.get_chat(ch)
    except Exception:
        return await m.answer("не вижу канал, добавь бота админом")
    await state.update_data(channel=ch, title=chat.title or ch)
    await state.set_state(SubFSM.invite)
    await m.answer(f"ок, <b>{html.escape(chat.title or ch)}</b>\nкинь invite (или «-»):")

@admin_router.message(SubFSM.invite)
async def sub_invite(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id, m.from_user.username): return
    invite = (m.text or "").strip()
    if invite == "-": invite = ""
    data = await state.get_data(); await state.clear()
    with closing(db()) as c:
        c.execute("INSERT OR REPLACE INTO forced_subs(channel, title, invite) VALUES(?,?,?)",
                  (data["channel"], data["title"], invite))
        c.commit()
    await m.answer("✅ добавлено. /admin — назад")

@admin_router.callback_query(F.data == "a_sub_del")
async def cb_sub_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id, cb.from_user.username):
        return await cb.answer("нет доступа", show_alert=True)
    with closing(db()) as c:
        subs = c.execute("SELECT * FROM forced_subs").fetchall()
    if not subs:
        return await cb.answer("нечего", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 {s['title'] or s['channel']}",
                              callback_data=f"sub_del:{s['channel']}")]
        for s in subs
    ] + [[InlineKeyboardButton(text="← назад", callback_data="a_subs")]])
    await cb.message.edit_text("что удалить?", reply_markup=kb)
    await cb.answer()

@admin_router.callback_query(F.data.startswith("sub_del:"))
async def cb_sub_del_do(cb: CallbackQuery):
    if not is_admin(cb.from_user.id, cb.from_user.username):
        return await cb.answer("нет доступа", show_alert=True)
    ch = cb.data.split(":",1)[1]
    with closing(db()) as c:
        c.execute("DELETE FROM forced_subs WHERE channel=?", (ch,))
        c.commit()
    await cb.answer("удалено")
    await cb_subs(cb)


# ============ main ============
async def main():
    db_init()
    dp.include_router(admin_router)
    dp.include_router(user_router)
    me = await bot.get_me()
    print(f"[+] @{me.username} ({me.id})")
    print(f"[+] owner: @{OWNER_USERNAME}")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
