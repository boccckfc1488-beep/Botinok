import os
import json
import random
import logging
from datetime import datetime

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
    ContextTypes,
)

# ============ CONFIG ============
load_dotenv()

BOT_TOKEN = "1780245087:Tn-mEK_pGCpxggIav8-XZf9XotvaWvfEaIa"
BASE_URL = os.environ.get("ALTGram_BASE_URL", "http://188.134.95.254:2610")

OWNER_USERNAMES = [
    u.strip().lstrip("@").lower()
    for u in os.environ.get("OWNER_USERNAMES", "Grayson,YourPrince").split(",")
    if u.strip()
]

DB_FILE = "casino_db.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============ MINI DB (JSON) ============
def load_db() -> dict:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"users": {}, "settings": {"start_balance": 500, "min_bet": 10, "max_bet": 5000}}


def save_db(db: dict) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


DB = load_db()


def get_user(user_id: int, username: str = None, first_name: str = None) -> dict:
    uid = str(user_id)
    if uid not in DB["users"]:
        DB["users"][uid] = {
            "id": user_id,
            "username": username or "",
            "first_name": first_name or "",
            "balance": DB["settings"]["start_balance"],
            "wins": 0,
            "losses": 0,
            "games": 0,
            "captcha_passed": False,
            "stars_spent": 0,
            "created_at": datetime.utcnow().isoformat(),
        }
        save_db(DB)
    if username is not None:
        DB["users"][uid]["username"] = username
    if first_name is not None:
        DB["users"][uid]["first_name"] = first_name
    if "stars_spent" not in DB["users"][uid]:
        DB["users"][uid]["stars_spent"] = 0
    return DB["users"][uid]


def is_owner(update: Update) -> bool:
    user = update.effective_user
    if not user or not user.username:
        return False
    return user.username.lower() in OWNER_USERNAMES


# ============ STAR PACKS ============
STAR_PACKS = {
    "pack_100":  {"stars": 100,  "rub": 1500,  "label": "100 звёзд = 1500 ₽"},
    "pack_500":  {"stars": 500,  "rub": 5000,  "label": "500 звёзд = 5000 ₽"},
    "pack_699":  {"stars": 699,  "rub": 7500,  "label": "699 звёзд = 7500 ₽"},
    "pack_1000": {"stars": 1000, "rub": 10000, "label": "1000 звёзд = 10000 ₽"},
}


# ============ KEYBOARDS ============
def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎰 Играть", callback_data="menu_play")],
        [InlineKeyboardButton("👤 Профиль", callback_data="menu_profile")],
        [InlineKeyboardButton("🏆 Топ игроков", callback_data="menu_top")],
        [InlineKeyboardButton("💎 Pay✅ 0", callback_data="shop_open")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("⚙️ Админ-меню", callback_data="admin_menu")])
    return InlineKeyboardMarkup(rows)


def shop_menu() -> InlineKeyboardMarkup:
    rows = []
    for key, pack in STAR_PACKS.items():
        rows.append([
            InlineKeyboardButton(
                f"⭐ {pack['label']}",
                callback_data=f"buy_{key}"
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def play_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Кости (x2) 10", callback_data="bet_dice_10"),
         InlineKeyboardButton("🎲 Кости (x2) 50", callback_data="bet_dice_50")],
        [InlineKeyboardButton("🎯 Рулетка (x3) 25", callback_data="bet_roulette_25"),
         InlineKeyboardButton("🎯 Рулетка (x3) 100", callback_data="bet_roulette_100")],
        [InlineKeyboardButton("🎰 Слоты (x5) 20", callback_data="bet_slots_20"),
         InlineKeyboardButton("🎰 Слоты (x5) 200", callback_data="bet_slots_200")],
        [InlineKeyboardButton("🃏 Блекджек (x2.5) 30", callback_data="bet_bj_30"),
         InlineKeyboardButton("🃏 Блекджек (x2.5) 150", callback_data="bet_bj_150")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu_main")],
    ])


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Выдать баланс", callback_data="admin_give")],
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔧 Настройки", callback_data="admin_settings")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu_main")],
    ])


def captcha_keyboard(correct: int) -> InlineKeyboardMarkup:
    options = {correct}
    while len(options) < 4:
        delta = random.choice([-3, -2, -1, 1, 2, 3])
        cand = correct + delta
        if cand > 0:
            options.add(cand)
    opts = list(options)
    random.shuffle(opts)
    row1 = [InlineKeyboardButton(str(opts[0]), callback_data=f"captcha_{opts[0]}"),
            InlineKeyboardButton(str(opts[1]), callback_data=f"captcha_{opts[1]}")]
    row2 = [InlineKeyboardButton(str(opts[2]), callback_data=f"captcha_{opts[2]}"),
            InlineKeyboardButton(str(opts[3]), callback_data=f"captcha_{opts[3]}")]
    return InlineKeyboardMarkup([row1, row2])


# ============ CAPTCHA ============
async def send_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    a, b = random.randint(2, 9), random.randint(2, 9)
    correct = a + b
    context.user_data["captcha_answer"] = correct
    text = f"🤖 Подтверди, что ты человек:\n\nСколько будет {a} + {b}?"
    kb = captcha_keyboard(correct)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)


# ============ HANDLERS ============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user(user.id, user.username, user.first_name)

    if not u["captcha_passed"]:
        context.user_data["pending_captcha_for"] = "start"
        await send_captcha(update, context, user.id)
        return

    await update.message.reply_text(
        f"🎰 Добро пожаловать в Casino Simulator, {user.first_name}!\n"
        f"Баланс: {u['balance']} 🪙",
        reply_markup=main_menu(is_owner(update)),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Главное меню:", reply_markup=main_menu(is_owner(update)))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — старт + капча\n"
        "/menu — главное меню\n"
        "/profile — профиль\n"
        "/help — помощь"
    )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user(user.id, user.username, user.first_name)
    text = (
        f"👤 Профиль\n\n"
        f"ID: {u['id']}\n"
        f"Username: @{u['username'] or '—'}\n"
        f"Имя: {u['first_name'] or '—'}\n"
        f"Баланс: {u['balance']} 🪙\n"
        f"Игр: {u['games']} | Побед: {u['wins']} | Проигрышей: {u['losses']}\n"
        f"⭐ Звёзд потрачено: {u.get('stars_spent', 0)}\n"
    )
    if is_owner(update):
        text += "\n👑 Ты — владелец бота."
    await update.message.reply_text(text, reply_markup=main_menu(is_owner(update)))


# ============ PAYMENT ============
async def on_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def on_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user(user.id, user.username, user.first_name)
    payment = update.message.successful_payment

    payload = payment.invoice_payload
    pack = STAR_PACKS.get(payload)

    if not pack:
        await update.message.reply_text("❌ Ошибка: пакет не найден.")
        return

    rub_added = pack["rub"]
    u["balance"] += rub_added
    u["stars_spent"] = u.get("stars_spent", 0) + pack["stars"]
    save_db(DB)

    await update.message.reply_text(
        f"✅ Оплата прошла!\n\n"
        f"⭐ Получено звёзд: {pack['stars']}\n"
        f"💰 Баланс пополнен на {rub_added} ₽\n"
        f"💳 Текущий баланс: {u['balance']} 🪙",
        reply_markup=main_menu(is_owner(update)),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    u = get_user(user.id, user.username, user.first_name)
    data = query.data

    # --- КАПЧА ---
    if data.startswith("captcha_"):
        await query.answer()
        try:
            answer = int(data.split("_", 1)[1])
        except ValueError:
            await query.edit_message_text("Ошибка капчи. Напиши /start заново.")
            return
        if answer == context.user_data.get("captcha_answer"):
            u["captcha_passed"] = True
            save_db(DB)
            context.user_data.pop("captcha_answer", None)
            await query.edit_message_text(
                "✅ Капча пройдена!\n\n🎰 Добро пожаловать в Casino Simulator.",
                reply_markup=main_menu(is_owner(update)),
            )
        else:
            await query.edit_message_text("❌ Неверно. Попробуй ещё раз: /start")
        return

    if not u["captcha_passed"]:
        await query.answer("Сначала пройди капчу: /start", show_alert=True)
        return

    # --- МЕНЮ ---
    if data == "menu_main":
        await query.answer()
        await query.edit_message_text("Главное меню:", reply_markup=main_menu(is_owner(update)))
        return

    if data == "menu_play":
        await query.answer()
        await query.edit_message_text(
            f"🎰 Выбери игру. Баланс: {u['balance']} 🪙",
            reply_markup=play_menu(),
        )
        return

    if data == "menu_profile":
        await query.answer()
        text = (
            f"👤 Профиль\n\n"
            f"ID: {u['id']}\n"
            f"Username: @{u['username'] or '—'}\n"
            f"Баланс: {u['balance']} 🪙\n"
            f"Игр: {u['games']} | Побед: {u['wins']} | Проигрышей: {u['losses']}\n"
            f"⭐ Звёзд потрачено: {u.get('stars_spent', 0)}"
        )
        await query.edit_message_text(text, reply_markup=main_menu(is_owner(update)))
        return

    if data == "menu_top":
        await query.answer()
        users = sorted(DB["users"].values(), key=lambda x: x["balance"], reverse=True)[:10]
        lines = ["🏆 Топ-10 игроков:\n"]
        for i, usr in enumerate(users, 1):
            name = f"@{usr['username']}" if usr.get("username") else usr.get("first_name") or f"id{usr['id']}"
            lines.append(f"{i}. {name} — {usr['balance']} 🪙")
        await query.edit_message_text("\n".join(lines), reply_markup=main_menu(is_owner(update)))
        return

    # --- МАГАЗИН ---
    if data == "shop_open":
        await query.answer()
        await query.edit_message_text(
            "💎 Пополнение баланса звёздами\n\n"
            "Выбери пакет:",
            reply_markup=shop_menu(),
        )
        return

    if data.startswith("buy_"):
        await query.answer()
        pack = STAR_PACKS.get(data)
        if not pack:
            await query.edit_message_text("❌ Пакет не найден.")
            return

        try:
            await context.bot.send_invoice(
                chat_id=user.id,
                title=f"Пополнение баланса: {pack['label']}",
                description=f"Получи {pack['rub']} ₽ на баланс казино за {pack['stars']} звёзд.",
                payload=data,
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label=pack['label'], amount=pack['stars'])],
            )
        except Exception as e:
            logger.exception("Invoice error: %s", e)
            await query.edit_message_text(
                "❌ Не удалось создать счёт. Попробуй позже.",
                reply_markup=shop_menu(),
            )
        return

    # --- ИГРЫ ---
    if data.startswith("bet_"):
        await query.answer()
        parts = data.split("_")
        game = parts[1]
        bet = int(parts[2])

        if u["balance"] < bet:
            await query.edit_message_text(
                f"❌ Недостаточно средств. Баланс: {u['balance']} 🪙",
                reply_markup=play_menu(),
            )
            return

        u["games"] += 1
        u["balance"] -= bet
        result = ""

        # УСЛОЖНЁННЫЙ ФАРМ: шансы снижены
        if game == "dice":
            if random.random() < 0.35:
                win = bet * 2
                u["balance"] += win
                u["wins"] += 1
                result = f"🎲 Победа! +{win - bet} 🪙 (x2)"
            else:
                u["losses"] += 1
                result = f"🎲 Проигрыш. -{bet} 🪙"

        elif game == "roulette":
            if random.random() < 0.20:
                win = bet * 3
                u["balance"] += win
                u["wins"] += 1
                result = f"🎯 Победа! +{win - bet} 🪙 (x3)"
            else:
                u["losses"] += 1
                result = f"🎯 Проигрыш. -{bet} 🪙"

        elif game == "slots":
            if random.random() < 0.12:
                win = bet * 5
                u["balance"] += win
                u["wins"] += 1
                result = f"🎰 ДЖЕКПОТ! +{win - bet} 🪙 (x5)"
            elif random.random() < 0.30:
                win = bet * 2
                u["balance"] += win
                u["wins"] += 1
                result = f"🎰 Совпадение! +{win - bet} 🪙 (x2)"
            else:
                u["losses"] += 1
                result = f"🎰 Мимо. -{bet} 🪙"

        elif game == "bj":
            if random.random() < 0.42:
                win = int(bet * 2.5)
                u["balance"] += win
                u["wins"] += 1
                result = f"🃏 Блекджек! +{win - bet} 🪙 (x2.5)"
            else:
                u["losses"] += 1
                result = f"🃏 Перебор. -{bet} 🪙"

        else:
            result = "❌ Неизвестная игра."

        save_db(DB)
        await query.edit_message_text(
            f"{result}\n\nБаланс: {u['balance']} 🪙",
            reply_markup=play_menu(),
        )
        return

    # --- АДМИНКА ---
    if data.startswith("admin_"):
        if not is_owner(update):
            await query.answer("⛔ Нет доступа", show_alert=True)
            return

        if data == "admin_menu":
            await query.answer()
            await query.edit_message_text("⚙️ Админ-меню владельца:", reply_markup=admin_menu())
            return

        if data == "admin_stats":
            await query.answer()
            total_users = len(DB["users"])
            total_money = sum(x["balance"] for x in DB["users"].values())
            total_games = sum(x["games"] for x in DB["users"].values())
            total_stars = sum(x.get("stars_spent", 0) for x in DB["users"].values())
            await query.edit_message_text(
                f"📊 Статистика:\n\n"
                f"Пользователей: {total_users}\n"
                f"Денег в системе: {total_money} 🪙\n"
                f"Игр сыграно: {total_games}\n"
                f"⭐ Звёзд потрачено: {total_stars}",
                reply_markup=admin_menu(),
            )
            return

        if data == "admin_give":
            await query.answer()
            context.user_data["admin_await"] = "give"
            await query.edit_message_text(
                "💰 Отправь сообщение в формате:\n<user_id> <сумма>\n\nНапример: 123456789 500",
                reply_markup=admin_menu(),
            )
            return

        if data == "admin_broadcast":
            await query.answer()
            context.user_data["admin_await"] = "broadcast"
            await query.edit_message_text(
                "📢 Отправь текст для рассылки всем пользователям.",
                reply_markup=admin_menu(),
            )
            return

        if data == "admin_settings":
            await query.answer()
            s = DB["settings"]
            await query.edit_message_text(
                f"🔧 Настройки:\n\n"
                f"Стартовый баланс: {s['start_balance']}\n"
                f"Мин. ставка: {s['min_bet']}\n"
                f"Макс. ставка: {s['max_bet']}\n\n"
                f"Чтобы изменить — напиши в формате:\n"
                f"start_balance 500\nmin_bet 5\nmax_bet 10000",
                reply_markup=admin_menu(),
            )
            return

    await query.answer()


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user(user.id, user.username, user.first_name)
    text = update.message.text.strip()

    if is_owner(update) and context.user_data.get("admin_await"):
        mode = context.user_data.pop("admin_await")

        if mode == "give":
            try:
                parts = text.split()
                target_id = str(int(parts[0]))
                amount = int(parts[1])
                if target_id not in DB["users"]:
                    await update.message.reply_text("❌ Пользователь не найден.")
                    return
                DB["users"][target_id]["balance"] += amount
                save_db(DB)
                await update.message.reply_text(
                    f"✅ Выдано {amount} 🪙 пользователю {target_id}.",
                    reply_markup=admin_menu(),
                )
            except Exception:
                await update.message.reply_text("❌ Формат: <user_id> <сумма>")
            return

        if mode == "broadcast":
            sent, failed = 0, 0
            for uid in DB["users"].keys():
                try:
                    await context.bot.send_message(chat_id=int(uid), text=f"📢 {text}")
                    sent += 1
                except Exception:
                    failed += 1
            await update.message.reply_text(
                f"✅ Рассылка завершена. Отправлено: {sent}, ошибок: {failed}.",
                reply_markup=admin_menu(),
            )
            return

    if is_owner(update):
        for key in ("start_balance", "min_bet", "max_bet"):
            if text.startswith(key + " "):
                try:
                    val = int(text.split()[1])
                    DB["settings"][key] = val
                    save_db(DB)
                    await update.message.reply_text(
                        f"✅ {key} = {val}",
                        reply_markup=admin_menu(),
                    )
                except Exception:
                    await update.message.reply_text("❌ Неверное значение.")
                return

    await update.message.reply_text("Используй /menu для навигации.")


# ============ MAIN ============
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url(BASE_URL)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(PreCheckoutQueryHandler(on_pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Casino bot запущен на %s", BASE_URL)
    app.run_polling(allowed_updates=["message", "edited_message", "callback_query", "pre_checkout_query"])


if __name__ == "__main__":
    main()
