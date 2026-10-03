# bot.py — FunGame utils: казино, игры, дуэль-рулетка, бизнес, магазин, звёзды, админка
# python 3.11+ / aiogram 3.13.1
# pip install aiogram==3.13.1

import asyncio
import html
import random
import sqlite3
import time
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
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, Message, PreCheckoutQuery,
)

# ================= CONFIG =================
BOT_TOKEN = "8920969460:AAF93q-q-UnAosrD6VU-dRH6fucxwZgtBro"  # FILL: ревокни позже
DB_PATH = "fungame.db"

START_BALANCE = 1000
DAILY_BONUS = 500
COINS_PER_STAR = 100
STAR_PACKS = [(1, 100), (5, 550), (10, 1200), (50, 7000)]

OWNER_USERNAME = "WelmaDEV"
ADMIN_USERNAMES = {"welmadev"}   # регистронезависимо
ADMINS: set[int] = set()          # FILL: свой id через /id

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
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                balance     INTEGER DEFAULT 0,
                wins        INTEGER DEFAULT 0,
                losses      INTEGER DEFAULT 0,
                wagered     INTEGER DEFAULT 0,
                won_total   INTEGER DEFAULT 0,
                last_bonus  INTEGER DEFAULT 0,
                created_at  INTEGER DEFAULT 0
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


def now_ts() -> int:
    return int(time.time())


def ensure_user(uid: int, username: str | None, first_name: str | None):
    with closing(db()) as c:
        row = c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            c.execute(
                "INSERT INTO users(user_id, username, first_name, balance, created_at) VALUES(?,?,?,?,?)",
                (uid, username or "", first_name or "", START_BALANCE, now_ts()),
            )
        else:
            c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?",
                      (username or "", first_name or "", uid))
        c.commit()


def get_balance(uid: int) -> int:
    with closing(db()) as c:
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
    return row["balance"] if row else 0


def add_balance(uid: int, delta: int):
    with closing(db()) as c:
        c.execute("UPDATE users SET balance=MAX(0, balance+?) WHERE user_id=?", (delta, uid))
        c.commit()


def record_wager(uid: int, bet: int):
    with closing(db()) as c:
        c.execute("UPDATE users SET wagered=wagered+? WHERE user_id=?", (bet, uid))
        c.commit()


def record_win(uid: int, amount: int):
    with closing(db()) as c:
        c.execute("UPDATE users SET wins=wins+1, won_total=won_total+? WHERE user_id=?",
                  (amount, uid))
        c.commit()


def record_loss(uid: int):
    with closing(db()) as c:
        c.execute("UPDATE users SET losses=losses+1 WHERE user_id=?", (uid,))
        c.commit()


def is_admin(uid: int, username: str | None = None) -> bool:
    if uid in ADMINS:
        return True
    if username and username.lower() in {u.lower() for u in ADMIN_USERNAMES}:
        return True
    return False


# ============ forced subs ============
async def missing_subs(uid: int) -> list:
    with closing(db()) as c:
        subs = c.execute("SELECT * FROM forced_subs").fetchall()
    if not subs:
        return []
    missing = []
    for s in subs:
        try:
            m = await bot.get_chat_member(s["channel"], uid)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                missing.append(s)
        except (TelegramBadRequest, TelegramForbiddenError):
            missing.append(s)
    return missing


def subs_kb(missing: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"📢 {s['title'] or s['channel']}",
        url=s["invite"] or f"https://t.me/{s['channel'].lstrip('@')}"
    )] for s in missing]
    rows.append([InlineKeyboardButton(text="✅ я подписался", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============ helpers ============
def parse_bet(text: str, balance: int) -> tuple[bool, int, str]:
    parts = text.split()
    if len(parts) < 2:
        return False, 0, "укажи ставку: <code>100</code> или <code>all</code>"
    raw = parts[1].lower()
    if raw in ("all", "всё", "все"):
        if balance <= 0:
            return False, 0, "у тебя 0 коинов"
        return True, balance, ""
    if raw in ("half", "половина"):
        if balance < 2:
            return False, 0, "недостаточно"
        return True, balance // 2, ""
    if not raw.isdigit():
        return False, 0, "ставка — число, all или half"
    bet = int(raw)
    if bet <= 0:
        return False, 0, "ставка > 0"
    if bet > balance:
        return False, 0, f"у тебя только <b>{balance}</b>"
    return True, bet, ""


def in_group(m: Message) -> bool:
    return m.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def require_sub(m: Message) -> bool:
    if is_admin(m.from_user.id, m.from_user.username):
        return True
    miss = await missing_subs(m.from_user.id)
    if miss:
        await m.reply("🚀 сначала подпишись:", reply_markup=subs_kb(miss))
        return False
    return True


# ============ FSM ============
class AdminFSM(StatesGroup):
    give_uid = State()
    give_amount = State()


class SubFSM(StatesGroup):
    channel = State()
    invite = State()


# ============ menus ============
def profile_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 пополнить", callback_data="topup_open")],
        [InlineKeyboardButton(text="💼 бизнесы", callback_data="biz_menu")],
        [InlineKeyboardButton(text="🛒 магазин", callback_data="shop_open")],
        [InlineKeyboardButton(text="🎁 бонус", callback_data="daily_bonus")],
    ])


def topup_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"⭐ {s} — {c:,} коинов", callback_data=f"buy_stars:{s}")]
            for s, c in STAR_PACKS]
    rows.append([InlineKeyboardButton(text="← назад", callback_data="profile_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def biz_kb(uid: int) -> InlineKeyboardMarkup:
    with closing(db()) as c:
        owned = {r["biz_id"] for r in c.execute(
            "SELECT biz_id FROM businesses WHERE user_id=?", (uid,)).fetchall()}
    rows = []
    for bid, (name, price, income) in BUSINESSES.items():
        if bid in owned:
            rows.append([InlineKeyboardButton(text=f"✅ {name} — забрать",
                                              callback_data=f"biz_claim:{bid}")])
        else:
            rows.append([InlineKeyboardButton(text=f"{name} — {price:,} (+{income:,}/д)",
                                              callback_data=f"biz_buy:{bid}")])
    rows.append([InlineKeyboardButton(text="💰 забрать всё", callback_data="biz_collect")])
    rows.append([InlineKeyboardButton(text="← назад", callback_data="profile_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{n} — {p:,}", callback_data=f"shop_buy:{iid}")]
            for iid, (n, p, _) in SHOP_ITEMS.items()]
    rows.append([InlineKeyboardButton(text="🎒 инвентарь", callback_data="inv_open")])
    rows.append([InlineKeyboardButton(text="← назад", callback_data="profile_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 выдать баланс", callback_data="a_give")],
        [InlineKeyboardButton(text="📢 обязательные подписки", callback_data="a_subs")],
        [InlineKeyboardButton(text="📊 статистика", callback_data="a_stats")],
    ])


# ============ user commands ============
@user_router.message(CommandStart())
async def cmd_start(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m):
        return
    if in_group(m):
        await m.reply(
            f"🎮 <b>FunGame utils</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>\n\n"
            f"игры: /casino /dice /slots /coin /roulette /bj /crash /mines /wheel /case /tower /horse\n"
            f"дуэль: /duel 100 (в ответ на сообщение)\n"
            f"экономика: /profile /business /shop /inv /top /pay\n"
            f"топап: /topup"
        )
    else:
        await m.answer(
            f"🎮 <b>FunGame utils</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>",
            reply_markup=profile_kb(m.from_user.id),
        )


@user_router.message(Command("id"))
async def cmd_id(m: Message):
    await m.reply(f"твой ID: <code>{m.from_user.id}</code>")


@user_router.message(Command("profile"))
async def cmd_profile(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m):
        return
    with closing(db()) as c:
        u = c.execute("SELECT * FROM users WHERE user_id=?", (m.from_user.id,)).fetchone()
        biz_count = c.execute("SELECT COUNT(*) n FROM businesses WHERE user_id=?",
                              (m.from_user.id,)).fetchone()["n"]
    total = u["wins"] + u["losses"]
    wr = f"{u['wins']*100//total}%" if total else "—"
    text = (
        f"👤 <b>{html.escape(u['first_name'] or 'игрок')}</b>\n"
        f"🆔 <code>{u['user_id']}</code>\n"
        f"💰 баланс: <b>{u['balance']:,}</b>\n"
        f"🏆 {u['wins']} • 💀 {u['losses']} • winrate <b>{wr}</b>\n"
        f"🎲 проставлено: <b>{u['wagered']:,}</b>\n"
        f"💵 выиграно: <b>{u['won_total']:,}</b>\n"
        f"💼 бизнесов: <b>{biz_count}</b>"
    )
    if in_group(m):
        await m.reply(text)
    else:
        await m.answer(text, reply_markup=profile_kb(m.from_user.id))


@user_router.message(Command("topup"))
async def cmd_topup(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    await m.answer(f"💎 курс: <b>1 ⭐ = {COINS_PER_STAR} коинов</b>", reply_markup=topup_kb())


@user_router.message(Command("daily"))
async def cmd_daily(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    with closing(db()) as c:
        u = c.execute("SELECT last_bonus FROM users WHERE user_id=?", (m.from_user.id,)).fetchone()
    if now_ts() - u["last_bonus"] < 86400:
        left = 86400 - (now_ts() - u["last_bonus"])
        await m.reply(f"⏳ через {left//3600}ч {(left%3600)//60}м")
        return
    add_balance(m.from_user.id, DAILY_BONUS)
    with closing(db()) as c:
        c.execute("UPDATE users SET last_bonus=? WHERE user_id=?", (now_ts(), m.from_user.id))
        c.commit()
    await m.reply(f"🎁 +{DAILY_BONUS:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")


# ============ ИГРЫ ============
@user_router.message(Command("casino"))
async def cmd_casino(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    roll = random.randint(0, 36)
    record_wager(m.from_user.id, bet)
    if roll == 0:
        win = bet * 14
        add_balance(m.from_user.id, win - bet); record_win(m.from_user.id, win)
        return await m.reply(f"🎰 <b>зеро!</b> ×14 → <b>+{win-bet:,}</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    if roll % 2 == 0:
        add_balance(m.from_user.id, bet); record_win(m.from_user.id, bet*2)
        await m.reply(f"🎰 {roll} (чёт) → <b>+{bet:,}</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    else:
        add_balance(m.from_user.id, -bet); record_loss(m.from_user.id)
        await m.reply(f"🎰 {roll} (нечет) → <b>-{bet:,}</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")


@user_router.message(Command("dice"))
async def cmd_dice(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    you, bot_roll = random.randint(1,6), random.randint(1,6)
    record_wager(m.from_user.id, bet)
    if you > bot_roll:
        add_balance(m.from_user.id, bet); record_win(m.from_user.id, bet*2)
        await m.reply(f"🎲 ты {you} • бот {bot_roll}\n🏆 <b>+{bet:,}</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    elif you < bot_roll:
        add_balance(m.from_user.id, -bet); record_loss(m.from_user.id)
        await m.reply(f"🎲 ты {you} • бот {bot_roll}\n💀 <b>-{bet:,}</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    else:
        await m.reply(f"🎲 {you}={bot_roll} ничья")


@user_router.message(Command("slots"))
async def cmd_slots(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    icons = ["🍒","🍋","💎","7️⃣","⭐","🔔"]
    s = [random.choice(icons) for _ in range(3)]
    line = " | ".join(s)
    record_wager(m.from_user.id, bet)
    if s[0]==s[1]==s[2]:
        mult = 20 if s[0]=="7️⃣" else 10
        win = bet*mult
        add_balance(m.from_user.id, win-bet); record_win(m.from_user.id, win)
        await m.reply(f"🎰 {line}\n🔥 ×{mult} → <b>+{win-bet:,}</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    elif s[0]==s[1] or s[1]==s[2] or s[0]==s[2]:
        add_balance(m.from_user.id, bet); record_win(m.from_user.id, bet*2)
        await m.reply(f"🎰 {line}\nпара → <b>+{bet:,}</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    else:
        add_balance(m.from_user.id, -bet); record_loss(m.from_user.id)
        await m.reply(f"🎰 {line}\nмимо → <b>-{bet:,}</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")


@user_router.message(Command("coin"))
async def cmd_coin(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    bal = get_balance(m.from_user.id)
    parts = (m.text or "").split()
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    pick = parts[2].lower() if len(parts) > 2 else "орел"
    if pick not in ("орел","решка","heads","tails","o","r"):
        return await m.reply("выбери: орел или решка")
    pick = "орел" if pick in ("орел","heads","o") else "решка"
    res = random.choice(["орел","решка"])
    record_wager(m.from_user.id, bet)
    if pick == res:
        add_balance(m.from_user.id, bet); record_win(m.from_user.id, bet*2)
        await m.reply(f"🪙 {res}\n🏆 <b>+{bet:,}</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    else:
        add_balance(m.from_user.id, -bet); record_loss(m.from_user.id)
        await m.reply(f"🪙 {res}\n💀 <b>-{bet:,}</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")


@user_router.message(Command("roulette"))
async def cmd_roulette(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    parts = (m.text or "").split()
    if len(parts) < 3:
        return await m.reply("🎡 <code>/roulette 100 красное</code>\nставки: красное/чёрное/чёт/нечет ×2, зеро/число ×36")
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    pick = " ".join(parts[2:]).lower()
    roll = random.randint(0, 36)
    record_wager(m.from_user.id, bet)
    mult = 0
    if pick in ("красное","red","к") and roll in RED_NUMBERS: mult = 2
    elif pick in ("чёрное","черное","black","ч") and roll != 0 and roll not in RED_NUMBERS: mult = 2
    elif pick in ("чёт","чет","even") and roll != 0 and roll % 2 == 0: mult = 2
    elif pick in ("нечет","нечёт","odd") and roll % 2 == 1: mult = 2
    elif pick in ("зеро","zero","0") and roll == 0: mult = 36
    elif pick.isdigit() and int(pick) == roll: mult = 36
    color = "🟢 зеро" if roll == 0 else ("🔴 красное" if roll in RED_NUMBERS else "⚫ чёрное")
    if mult:
        win = bet * mult
        add_balance(m.from_user.id, win - bet); record_win(m.from_user.id, win)
        await m.reply(f"🎡 {roll} {color}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    else:
        add_balance(m.from_user.id, -bet); record_loss(m.from_user.id)
        await m.reply(f"🎡 {roll} {color}\n💀 -{bet:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")


# --- блэкджек ---
active_bj: dict[int, dict] = {}

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

@user_router.message(Command("bj"))
async def cmd_bj(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    if m.from_user.id in active_bj:
        return await m.reply("у тебя уже идёт /bj")
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    player, dealer = [bj_draw(), bj_draw()], [bj_draw(), bj_draw()]
    active_bj[m.from_user.id] = {"bet": bet, "player": player, "dealer": dealer}
    await m.reply(
        f"🃏 <b>блэкджек</b> — ставка <b>{bet:,}</b>\n\n"
        f"твои: {' '.join(map(str,player))} = <b>{bj_score(player)}</b>\n"
        f"дилер: {dealer[0]} ?",
        reply_markup=bj_kb(m.from_user.id),
    )

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
        return await cb.message.edit_text(f"🃏 перебор {score}\n💀 <b>-{st['bet']:,}</b>\nбаланс: <b>{get_balance(uid):,}</b>")
    await cb.message.edit_text(
        f"🃏 <b>блэкджек</b> — ставка <b>{st['bet']:,}</b>\n\n"
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
    record_wager(uid, st["bet"])
    if ds > 21 or ps > ds:
        add_balance(uid, st["bet"]); record_win(uid, st["bet"]*2)
        result = f"🏆 победа! <b>+{st['bet']:,}</b>"
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


@user_router.message(Command("crash"))
async def cmd_crash(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    parts = (m.text or "").split()
    if len(parts) < 3:
        return await m.reply("🚀 <code>/crash 100 2.5</code>")
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    try:
        target = float(parts[2].replace(",", "."))
        if target < 1.1 or target > 100: raise ValueError
    except ValueError:
        return await m.reply("множитель 1.1 - 100")
    crash_at = round(max(1.0, random.expovariate(1/1.5)), 2)
    record_wager(m.from_user.id, bet)
    if crash_at >= target:
        win = int(bet * target)
        add_balance(m.from_user.id, win - bet); record_win(m.from_user.id, win)
        await m.reply(f"🚀 ×{crash_at}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    else:
        add_balance(m.from_user.id, -bet); record_loss(m.from_user.id)
        await m.reply(f"🚀 рухнуло ×{crash_at}\n💀 -{bet:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")


# --- mines ---
active_mines: dict[int, dict] = {}

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

@user_router.message(Command("mines"))
async def cmd_mines(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    if m.from_user.id in active_mines:
        return await m.reply("уже идёт /mines")
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    grid = [1]*22 + [0]*3
    random.shuffle(grid)
    active_mines[m.from_user.id] = {"bet": bet, "grid": grid, "revealed": set()}
    await m.reply(f"💣 <b>мины</b> — ставка <b>{bet:,}</b>\n3 мины на 25, шаг ×1.2",
                  reply_markup=mines_kb(m.from_user.id, set(), grid))

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
        f"💣 ×{mult:.2f} • выигрыш <b>{int(st['bet']*mult):,}</b>",
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
    win = int(st["bet"] * mult)
    add_balance(uid, win - st["bet"]); record_wager(uid, st["bet"]); record_win(uid, win)
    kb = mines_kb(uid, st["revealed"], st["grid"])
    active_mines.pop(uid, None)
    await cb.message.edit_text(f"💰 ×{mult:.2f} → <b>+{win-st['bet']:,}</b>\nбаланс: <b>{get_balance(uid):,}</b>", reply_markup=kb)
    await cb.answer()


@user_router.message(Command("wheel"))
async def cmd_wheel(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    seg = random.choices([0,1.2,1.5,2,3,5,10,0.5], weights=[30,25,15,12,8,5,2,3])[0]
    record_wager(m.from_user.id, bet)
    if seg >= 1:
        win = int(bet * seg)
        add_balance(m.from_user.id, win - bet); record_win(m.from_user.id, win)
        await m.reply(f"🎡 ×{seg}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    else:
        add_balance(m.from_user.id, -bet); record_loss(m.from_user.id)
        await m.reply(f"🎡 ×{seg}\n💀 -{bet:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")


@user_router.message(Command("case"))
async def cmd_case(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    r = random.random()
    if r < 0.01: mult, tag = 50, "🌟 джекпот"
    elif r < 0.05: mult, tag = 10, "🔥 rare"
    elif r < 0.15: mult, tag = 3, "✨ uncommon"
    elif r < 0.40: mult, tag = 1.5, "🟢 common"
    else: mult, tag = 0, "💀 пусто"
    record_wager(m.from_user.id, bet)
    if mult:
        win = int(bet * mult)
        add_balance(m.from_user.id, win - bet); record_win(m.from_user.id, win)
        await m.reply(f"📦 {tag} ×{mult}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    else:
        add_balance(m.from_user.id, -bet); record_loss(m.from_user.id)
        await m.reply(f"📦 {tag}\n💀 -{bet:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")


@user_router.message(Command("tower"))
async def cmd_tower(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    mult = 1.0
    for floor in range(5):
        if random.random() < 0.35:
            add_balance(m.from_user.id, -bet); record_wager(m.from_user.id, bet); record_loss(m.from_user.id)
            return await m.reply(f"🏰 упал на {floor+1} (×{mult:.2f})\n💀 -{bet:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
        mult *= 1.5
    win = int(bet * mult)
    add_balance(m.from_user.id, win - bet); record_wager(m.from_user.id, bet); record_win(m.from_user.id, win)
    await m.reply(f"🏰 прошёл ×{mult:.2f}\n🏆 +{win-bet:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")


@user_router.message(Command("horse"))
async def cmd_horse(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    parts = (m.text or "").split()
    if len(parts) < 3:
        return await m.reply("🐎 <code>/horse 100 3</code> (1-6)")
    bal = get_balance(m.from_user.id)
    ok, bet, err = parse_bet(m.text or "", bal)
    if not ok: return await m.reply(err)
    try:
        pick = int(parts[2])
        if not 1 <= pick <= 6: raise ValueError
    except ValueError:
        return await m.reply("номер 1-6")
    winner = random.choices(range(1,7), weights=[10,15,25,25,15,10])[0]
    record_wager(m.from_user.id, bet)
    if winner == pick:
        win = bet * 3
        add_balance(m.from_user.id, win - bet); record_win(m.from_user.id, win)
        await m.reply(f"🐎 №{winner} — твоя!\n🏆 +{win-bet:,} (×3)\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")
    else:
        add_balance(m.from_user.id, -bet); record_loss(m.from_user.id)
        await m.reply(f"🐎 №{winner}, ты ставил №{pick}\n💀 -{bet:,}\nбаланс: <b>{get_balance(m.from_user.id):,}</b>")


# ============ ДУЭЛЬ: русская рулетка (инлайн) ============
active_duels: dict[int, dict[int, dict]] = {}

def _duel_kb(d: dict) -> InlineKeyboardMarkup:
    if d["state"] == "pending":
        cid = d["challenger"]
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚔️ принять", callback_data=f"duel:accept:{cid}"),
            InlineKeyboardButton(text="❌ отклонить", callback_data=f"duel:decline:{cid}"),
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔫 в себя",     callback_data=f"duel:self:{d['challenger']}"),
        InlineKeyboardButton(text="🔫 в соперника", callback_data=f"duel:enemy:{d['challenger']}"),
    ]])

def _duel_text(d: dict, names: dict[int, str]) -> str:
    cid, tid = d["challenger"], d["target"]
    cn, tn = html.escape(names.get(cid, str(cid))), html.escape(names.get(tid, str(tid)))
    if d["state"] == "pending":
        return (
            f"⚔️ <b>вызов на дуэль</b>\n"
            f"{cn} → {tn}\n"
            f"ставка <b>{d['bet']:,}</b> с каждого\n"
            f"банк: <b>{d['bet']*2:,}</b>\n\n"
            f"{tn}, принять?"
        )
    turn = cn if d["turn"] == cid else tn
    return (
        f"⚔️ <b>русская рулетка</b>\n"
        f"{cn} vs {tn} • банк <b>{d['bet']*2:,}</b>\n\n"
        f"🎯 ход: <b>{turn}</b>\n"
        f"🔫 выстрелов: <b>{d['shots']}</b>\n"
        f"🌀 барабан: <b>{len(d['chamber'])}</b> гнёзд, 1 патрон"
    )

def _duel_names(cid: int, tid: int) -> dict[int, str]:
    with closing(db()) as c:
        rows = c.execute("SELECT user_id, first_name FROM users WHERE user_id IN (?,?)",
                         (cid, tid)).fetchall()
    return {r["user_id"]: (r["first_name"] or str(r["user_id"])) for r in rows}

@user_router.message(Command("duel"))
async def cmd_duel(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    if not in_group(m):
        return await m.reply("дуэль только в группах")
    parts = (m.text or "").split()
    target_user = None
    bet_raw = None

    if m.reply_to_message and m.reply_to_message.from_user:
        target_user = m.reply_to_message.from_user
        if len(parts) < 2:
            return await m.reply("usage: <code>/duel 100</code> (в ответ на сообщение)")
        bet_raw = parts[1]
    elif len(parts) >= 3 and parts[1].startswith("@"):
        uname = parts[1].lstrip("@")
        with closing(db()) as c:
            row = c.execute("SELECT user_id, username, first_name FROM users WHERE username=?",
                            (uname,)).fetchone()
        if not row:
            return await m.reply("не нашёл юзера в базе")
        class _U:
            id = row["user_id"]; username = row["username"]; first_name = row["first_name"]; is_bot = False
        target_user = _U()
        bet_raw = parts[2]
    else:
        return await m.reply("ответь на сообщение соперника <code>/duel 100</code>")

    if target_user.id == m.from_user.id:
        return await m.reply("сам с собой? нет")
    if getattr(target_user, "is_bot", False):
        return await m.reply("с ботами не дуэлимся")
    if not bet_raw or not bet_raw.isdigit():
        return await m.reply("ставка — число")
    bet = int(bet_raw)
    if bet <= 0:
        return await m.reply("ставка > 0")

    ensure_user(target_user.id, getattr(target_user, "username", None),
                getattr(target_user, "first_name", None))

    chat_id = m.chat.id
    for d in active_duels.get(chat_id, {}).values():
        if m.from_user.id in (d["challenger"], d["target"]) or target_user.id in (d["challenger"], d["target"]):
            return await m.reply("кто-то из вас уже в дуэли")

    if get_balance(m.from_user.id) < bet:
        return await m.reply(f"у тебя <b>{get_balance(m.from_user.id):,}</b>")
    if get_balance(target_user.id) < bet:
        return await m.reply(f"у {html.escape(target_user.first_name or '')} недостаточно")

    d = {
        "challenger": m.from_user.id, "target": target_user.id, "bet": bet,
        "state": "pending", "chamber": [1]+[0]*5, "pos": 0, "shots": 0,
        "turn": None, "chat_id": chat_id,
    }
    random.shuffle(d["chamber"])
    active_duels.setdefault(chat_id, {})[m.from_user.id] = d
    names = _duel_names(d["challenger"], d["target"])
    await m.reply(_duel_text(d, names), reply_markup=_duel_kb(d))

@user_router.callback_query(F.data.startswith("duel:"))
async def cb_duel(cb: CallbackQuery):
    parts = cb.data.split(":")
    if len(parts) < 3: return await cb.answer()
    action, cid = parts[1], int(parts[2])
    chat_id = cb.message.chat.id
    d = active_duels.get(chat_id, {}).get(cid)
    if not d: return await cb.answer("дуэль не найдена", show_alert=True)

    if d["state"] == "pending":
        if cb.from_user.id != d["target"]:
            return await cb.answer("это не твой вызов", show_alert=True)
        if action == "decline":
            active_duels[chat_id].pop(cid, None)
            return await cb.message.edit_text("❌ вызов отклонён"), await cb.answer()
        if action == "accept":
            if get_balance(d["challenger"]) < d["bet"] or get_balance(d["target"]) < d["bet"]:
                active_duels[chat_id].pop(cid, None)
                return await cb.answer("у кого-то не хватает коинов", show_alert=True)
            d["state"] = "in_progress"
            d["turn"] = random.choice([d["challenger"], d["target"]])
            add_balance(d["challenger"], -d["bet"])
            add_balance(d["target"], -d["bet"])
            names = _duel_names(d["challenger"], d["target"])
            await cb.message.edit_text(_duel_text(d, names), reply_markup=_duel_kb(d))
            return await cb.answer()

    if d["state"] != "in_progress":
        return await cb.answer("дуэль не активна", show_alert=True)
    if cb.from_user.id != d["turn"]:
        return await cb.answer("не твой ход", show_alert=True)
    if action not in ("self", "enemy"):
        return await cb.answer()

    pos = d["pos"] % len(d["chamber"])
    fired = d["chamber"][pos] == 1
    d["shots"] += 1
    d["pos"] = (pos + 1) % len(d["chamber"])
    if d["pos"] == 0:
        ch = [1]+[0]*5
        random.shuffle(ch)
        d["chamber"] = ch

    shooter = cb.from_user.id
    opponent = d["target"] if shooter == d["challenger"] else d["challenger"]
    names = _duel_names(d["challenger"], d["target"])

    if fired:
        loser = shooter if action == "self" else opponent
        winner = d["target"] if loser == d["challenger"] else d["challenger"]
        bank = d["bet"] * 2
        add_balance(winner, bank)
        record_wager(winner, d["bet"]); record_win(winner, bank)
        record_wager(loser, d["bet"]);  record_loss(loser)
        active_duels[chat_id].pop(cid, None)
        wn, ln = html.escape(names.get(winner, str(winner))), html.escape(names.get(loser, str(loser)))
        return await cb.message.edit_text(
            f"💥 <b>БАБАХ!</b>\n\n{ln} — мёртв(а) 💀\n"
            f"🏆 победил(а) <b>{wn}</b>\n"
            f"банк <b>{bank:,}</b>\n"
            f"баланс: <b>{get_balance(winner):,}</b>"
        ), await cb.answer()

    d["turn"] = opponent
    await cb.message.edit_text(
        "🌀 <i>щёлк, осечка...</i>\n\n" + _duel_text(d, names),
        reply_markup=_duel_kb(d),
    )
    await cb.answer()


# ============ бизнесы ============
@user_router.message(Command("business"))
async def cmd_business(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    text = f"💼 <b>бизнесы</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>"
    if in_group(m):
        await m.reply(text)
    else:
        await m.answer(text, reply_markup=biz_kb(m.from_user.id))

@user_router.callback_query(F.data.startswith("biz_buy:"))
async def cb_biz_buy(cb: CallbackQuery):
    bid = cb.data.split(":", 1)[1]
    if bid not in BUSINESSES:
        return await cb.answer("нет такого", show_alert=True)
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
    await cb.message.edit_reply_markup(reply_markup=biz_kb(cb.from_user.id))

@user_router.callback_query(F.data.startswith("biz_claim:"))
async def cb_biz_claim(cb: CallbackQuery):
    bid = cb.data.split(":", 1)[1]
    if bid not in BUSINESSES:
        return await cb.answer("нет такого", show_alert=True)
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

@user_router.callback_query(F.data == "biz_collect")
async def cb_biz_collect(cb: CallbackQuery):
    with closing(db()) as c:
        rows = c.execute("SELECT * FROM businesses WHERE user_id=?", (cb.from_user.id,)).fetchall()
    if not rows: return await cb.answer("нет бизнесов", show_alert=True)
    total = 0
    for r in rows:
        name, price, income = BUSINESSES[r["biz_id"]]
        elapsed = now_ts() - r["last_pay"]
        if elapsed < 86400: continue
        total += income * (elapsed // 86400)
    if total == 0: return await cb.answer("пока нечего", show_alert=True)
    add_balance(cb.from_user.id, total)
    with closing(db()) as c:
        c.execute("UPDATE businesses SET last_pay=? WHERE user_id=?", (now_ts(), cb.from_user.id))
        c.commit()
    await cb.answer(f"+{total:,}", show_alert=True)

@user_router.callback_query(F.data == "biz_menu")
async def cb_biz_menu(cb: CallbackQuery):
    await cb.message.edit_text(
        f"💼 <b>бизнесы</b>\nбаланс: <b>{get_balance(cb.from_user.id):,}</b>",
        reply_markup=biz_kb(cb.from_user.id),
    )
    await cb.answer()


# ============ магазин / инвентарь ============
@user_router.message(Command("shop"))
async def cmd_shop(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    desc = "\n".join(f"{n} — <b>{p:,}</b>\n<i>{d}</i>" for n, p, d in SHOP_ITEMS.values())
    await m.answer(f"🛒 <b>магазин</b>\nбаланс: <b>{get_balance(m.from_user.id):,}</b>\n\n{desc}",
                   reply_markup=shop_kb())

@user_router.callback_query(F.data == "shop_open")
async def cb_shop_open(cb: CallbackQuery):
    await cb.message.edit_text(
        f"🛒 <b>магазин</b>\nбаланс: <b>{get_balance(cb.from_user.id):,}</b>",
        reply_markup=shop_kb(),
    )
    await cb.answer()

@user_router.callback_query(F.data.startswith("shop_buy:"))
async def cb_shop_buy(cb: CallbackQuery):
    iid = cb.data.split(":", 1)[1]
    if iid not in SHOP_ITEMS:
        return await cb.answer("нет такого", show_alert=True)
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
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← назад", callback_data="profile_back")]
    ]))
    await cb.answer()


@user_router.message(Command("inv"))
async def cmd_inv(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    with closing(db()) as c:
        rows = c.execute("SELECT item_id, qty FROM inventory WHERE user_id=?",
                         (m.from_user.id,)).fetchall()
    if not rows:
        return await m.reply("🎒 пусто")
    text = "🎒 <b>инвентарь</b>\n\n" + "\n".join(
        f"• {SHOP_ITEMS[r['item_id']][0]} ×{r['qty']}" for r in rows if r["item_id"] in SHOP_ITEMS
    )
    await m.reply(text)


# ============ топ / переводы ============
@user_router.message(Command("top"))
async def cmd_top(m: Message):
    with closing(db()) as c:
        top_bal = c.execute("SELECT user_id, first_name, username, balance FROM users ORDER BY balance DESC LIMIT 10").fetchall()
        top_won = c.execute("SELECT user_id, first_name, username, won_total FROM users ORDER BY won_total DESC LIMIT 10").fetchall()
    def line(i, r, key):
        name = r["first_name"] or r["username"] or str(r["user_id"])
        return f"{i}. <b>{html.escape(name)}</b> — {r[key]:,}"
    text = "🏆 <b>топ по балансу</b>\n" + "\n".join(line(i, r, "balance") for i, r in enumerate(top_bal, 1))
    text += "\n\n💵 <b>топ по выигрышу</b>\n" + "\n".join(line(i, r, "won_total") for i, r in enumerate(top_won, 1))
    await m.reply(text)

@user_router.message(Command("pay"))
async def cmd_pay(m: Message):
    ensure_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not await require_sub(m): return
    parts = (m.text or "").split()
    if len(parts) < 3:
        return await m.reply("usage: <code>/pay @user 500</code> или <code>/pay 123456789 500</code>")
    target_raw, amount_raw = parts[1], parts[2]
    if not amount_raw.isdigit():
        return await m.reply("сумма — число")
    amount = int(amount_raw)
    if amount <= 0:
        return await m.reply("сумма > 0")
    with closing(db()) as c:
        if target_raw.startswith("@"):
            row = c.execute("SELECT user_id FROM users WHERE username=?",
                            (target_raw.lstrip("@"),)).fetchone()
        else:
            row = c.execute("SELECT user_id FROM users WHERE user_id=?",
                            (int(target_raw) if target_raw.isdigit() else -1,)).fetchone()
    if not row:
        return await m.reply("не нашёл получателя")
    target = row["user_id"]
    if target == m.from_user.id:
        return await m.reply("себе? нет")
    if get_balance(m.from_user.id) < amount:
        return await m.reply("недостаточно коинов")
    add_balance(m.from_user.id, -amount)
    add_balance(target, amount)
    try:
        await bot.send_message(target, f"💸 тебе перевели <b>{amount:,}</b> коинов")
    except Exception:
        pass
    await m.reply(f"💸 переведено <b>{amount:,}</b> → <code>{target}</code>")


# ============ topup / звёзды ============
@user_router.callback_query(F.data == "topup_open")
async def cb_topup(cb: CallbackQuery):
    await cb.message.edit_text(
        f"💎 курс: <b>1 ⭐ = {COINS_PER_STAR} коинов</b>",
        reply_markup=topup_kb(),
    )
    await cb.answer()

@user_router.callback_query(F.data.startswith("buy_stars:"))
async def cb_buy_stars(cb: CallbackQuery):
    stars = int(cb.data.split(":", 1)[1])
    coins = next((c for s, c in STAR_PACKS if s == stars), stars * COINS_PER_STAR)
    try:
        await bot.send_invoice(
            chat_id=cb.from_user.id,
            title="FunGame utils — пополнение",
            description=f"{coins:,} коинов за {stars} ⭐",
            payload=f"topup:{cb.from_user.id}:{coins}",
            provider_token="",
            currency="XTR",
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
        _, uid_s, coins_s = payload.split(":")
        uid, coins = int(uid_s), int(coins_s)
    except Exception:
        return await m.reply("оплата прошла, но payload битый — напиши @" + OWNER_USERNAME)
    add_balance(uid, coins)
    await m.reply(f"✅ зачислено <b>{coins:,}</b>\nбаланс: <b>{get_balance(uid):,}</b>")


@user_router.callback_query(F.data == "profile_back")
async def cb_profile_back(cb: CallbackQuery):
    with closing(db()) as c:
        u = c.execute("SELECT * FROM users WHERE user_id=?", (cb.from_user.id,)).fetchone()
    await cb.message.edit_text(
        f"👤 <b>{html.escape(u['first_name'] or 'игрок')}</b>\n"
        f"💰 баланс: <b>{u['balance']:,}</b>\n"
        f"🏆 {u['wins']} • 💀 {u['losses']}",
        reply_markup=profile_kb(cb.from_user.id),
    )
    await cb.answer()

@user_router.callback_query(F.data == "daily_bonus")
async def cb_daily(cb: CallbackQuery):
    with closing(db()) as c:
        u = c.execute("SELECT last_bonus FROM users WHERE user_id=?", (cb.from_user.id,)).fetchone()
    if now_ts() - u["last_bonus"] < 86400:
        left = 86400 - (now_ts() - u["last_bonus"])
        return await cb.answer(f"через {left//3600}ч {(left%3600)//60}м", show_alert=True)
    add_balance(cb.from_user.id, DAILY_BONUS)
    with closing(db()) as c:
        c.execute("UPDATE users SET last_bonus=? WHERE user_id=?", (now_ts(), cb.from_user.id))
        c.commit()
    await cb.answer(f"+{DAILY_BONUS} коинов", show_alert=True)

@user_router.callback_query(F.data == "check_subs")
async def cb_check_subs(cb: CallbackQuery):
    miss = await missing_subs(cb.from_user.id)
    if miss:
        await cb.answer("ещё не всё подписано", show_alert=True)
        try: await cb.message.edit_reply_markup(reply_markup=subs_kb(miss))
        except Exception: pass
    else:
        await cb.message.edit_text(f"✅ подписан. жми /start")
    await cb.answer()


# ============ ADMIN ============
@admin_router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id, m.from_user.username): return
    await m.answer(f"🎮 <b>FunGame utils</b> — админка @{OWNER_USERNAME}",
                   reply_markup=admin_menu())

@admin_router.callback_query(F.data == "a_back")
async def cb_aback(cb: CallbackQuery):
    if not is_admin(cb.from_user.id, cb.from_user.username):
        return await cb.answer("нет доступа", show_alert=True)
    await cb.message.edit_text("админ-меню:", reply_markup=admin_menu())
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
    await cb.message.edit_text(
        f"📊 <b>статистика</b>\n\n"
        f"юзеров: {users}\n"
        f"коинов в обороте: <b>{total_money:,}</b>\n"
        f"проставлено всего: <b>{total_wagered:,}</b>\n"
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
            row = c.execute("SELECT user_id FROM users WHERE username=?",
                            (val.lstrip("@"),)).fetchone()
        if not row:
            return await m.answer("не нашёл")
        uid = row["user_id"]
    elif val.lstrip("-").isdigit():
        uid = int(val)
    else:
        return await m.answer("числовой id или @username")
    await state.update_data(uid=uid)
    await state.set_state(AdminFSM.give_amount)
    await m.answer(f"юзер {uid}. введи сумму (можно -500 чтобы снять):")

@admin_router.message(AdminFSM.give_amount)
async def adm_amount(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id, m.from_user.username): return
    val = (m.text or "").strip()
    if not val.lstrip("-").isdigit():
        return await m.answer("число")
    amount = int(val)
    data = await state.get_data()
    uid = data["uid"]
    await state.clear()
    add_balance(uid, amount)
    await m.answer(f"✅ {amount:+,} → {uid}\nбаланс: <b>{get_balance(uid):,}</b>")
    try:
        await bot.send_message(uid, f"💰 админ начислил <b>{amount:+,}</b>\nбаланс: <b>{get_balance(uid):,}</b>")
    except Exception:
        pass


# --- forced subs admin ---
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
    data = await state.get_data()
    await state.clear()
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
    ch = cb.data.split(":", 1)[1]
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
