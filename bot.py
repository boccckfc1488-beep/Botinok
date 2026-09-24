import os
import json
import random
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ============ CONFIG ============

BOT_TOKEN = "1780245087:Tn-mEK_pGCpxggIav8-XZf9XotvaWvfEaIa"
BASE_URL = "http://188.134.95.254:2610"
BASE_FILE_URL = "http://188.134.95.254:2610/file"

OWNER_USERNAMES = ["grayson", "yourprince"]

DB_FILE = "casino_db.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============ HEALTH CHECK SERVER ============

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()


# ============ MINI DB ============

def load_db() -> dict:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "users": {},
        "settings": {"start_balance": 500, "min_bet": 10, "max_bet": 5000},
        "requests": {},
    }


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
    "pack_100": {"stars": 100, "rub": 1500, "label": "100 stars = 1500 rub"},
    "pack_500": {"stars": 500, "rub": 5000, "label": "500 stars = 5000 rub"},
    "pack_699": {"stars": 699, "rub": 7500, "label": "699 stars = 7500 rub"},
    "pack_1000": {"stars": 1000, "rub": 10000, "label": "1000 stars = 10000 rub"},
}


# ============ KEYBOARDS ============

def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Play", callback_data="menu_play")],
        [InlineKeyboardButton("Profile", callback_data="menu_profile")],
        [InlineKeyboardButton("Top players", callback_data="menu_top")],
        [InlineKeyboardButton("Pay", callback_data="shop_open")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("Admin", callback_data="admin_menu")])
    return InlineKeyboardMarkup(rows)


def shop_menu() -> InlineKeyboardMarkup:
    rows = []
    for key, pack in STAR_PACKS.items():
        rows.append([
            InlineKeyboardButton(
                f"{pack['label']}",
                callback_data=f"buy_{key}"
            )
        ])
    rows.append([InlineKeyboardButton("Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def play_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Dice x2 10", callback_data="bet_dice_10"),
         InlineKeyboardButton("Dice x2 50", callback_data="bet_dice_50")],
        [InlineKeyboardButton("Roulette x3 25", callback_data="bet_roulette_25"),
         InlineKeyboardButton("Roulette x3 100", callback_data="bet_roulette_100")],
        [InlineKeyboardButton("Slots x5 20", callback_data="bet_slots_20"),
         InlineKeyboardButton("Slots x5 200", callback_data="bet_slots_200")],
        [InlineKeyboardButton("Blackjack x2.5 30", callback_data="bet_bj_30"),
         InlineKeyboardButton("Blackjack x2.5 150", callback_data="bet_bj_150")],
        [InlineKeyboardButton("Back", callback_data="menu_main")],
    ])


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Give balance", callback_data="admin_give")],
        [InlineKeyboardButton("Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("Settings", callback_data="admin_settings")],
        [InlineKeyboardButton("Payment requests", callback_data="admin_requests")],
        [InlineKeyboardButton("Back", callback_data="menu_main")],
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
    text = f"Check you are human:\n\nHow much is {a} + {b}?"
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
        f"Casino Simulator. Welcome, {user.first_name}!\nBalance: {u['balance']}",
        reply_markup=main_menu(is_owner(update)),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Main menu:", reply_markup=main_menu(is_owner(update)))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/start - start + captcha\n"
        "/menu - main menu\n"
        "/profile - profile\n"
        "/help - help"
    )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user(user.id, user.username, user.first_name)
    text = (
        f"Profile\n\n"
        f"ID: {u['id']}\n"
        f"Username: @{u['username'] or '-'}\n"
        f"Name: {u['first_name'] or '-'}\n"
        f"Balance: {u['balance']}\n"
        f"Games: {u['games']} | Wins: {u['wins']} | Losses: {u['losses']}\n"
        f"Stars spent: {u.get('stars_spent', 0)}\n"
    )
    if is_owner(update):
        text += "\nYou are the owner."
    await update.message.reply_text(text, reply_markup=main_menu(is_owner(update)))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    u = get_user(user.id, user.username, user.first_name)
    data = query.data

    if data.startswith("captcha_"):
        await query.answer()
        try:
            answer = int(data.split("_", 1)[1])
        except ValueError:
            await query.edit_message_text("Captcha error. Send /start again.")
            return
        if answer == context.user_data.get("captcha_answer"):
            u["captcha_passed"] = True
            save_db(DB)
            context.user_data.pop("captcha_answer", None)
            await query.edit_message_text(
                "Captcha passed!\n\nWelcome to Casino Simulator.",
                reply_markup=main_menu(is_owner(update)),
            )
        else:
            await query.edit_message_text("Wrong. Try again: /start")
        return

    if not u["captcha_passed"]:
        await query.answer("Pass captcha first: /start", show_alert=True)
        return

    if data == "menu_main":
        await query.answer()
        await query.edit_message_text("Main menu:", reply_markup=main_menu(is_owner(update)))
        return

    if data == "menu_play":
        await query.answer()
        await query.edit_message_text(
            f"Choose game. Balance: {u['balance']}",
            reply_markup=play_menu(),
        )
        return

    if data == "menu_profile":
        await query.answer()
        text = (
            f"Profile\n\n"
            f"ID: {u['id']}\n"
            f"Username: @{u['username'] or '-'}\n"
            f"Balance: {u['balance']}\n"
            f"Games: {u['games']} | Wins: {u['wins']} | Losses: {u['losses']}\n"
            f"Stars spent: {u.get('stars_spent', 0)}"
        )
        await query.edit_message_text(text, reply_markup=main_menu(is_owner(update)))
        return

    if data == "menu_top":
        await query.answer()
        users = sorted(DB["users"].values(), key=lambda x: x["balance"], reverse=True)[:10]
        lines = ["Top-10 players:\n"]
        for i, usr in enumerate(users, 1):
            name = f"@{usr['username']}" if usr.get("username") else usr.get("first_name") or f"id{usr['id']}"
            lines.append(f"{i}. {name} - {usr['balance']}")
        await query.edit_message_text("\n".join(lines), reply_markup=main_menu(is_owner(update)))
        return

    if data == "shop_open":
        await query.answer()
        await query.edit_message_text(
            "Top up balance with stars\n\nChoose a pack:",
            reply_markup=shop_menu(),
        )
        return

    if data.startswith("buy_"):
        await query.answer()
        pack = STAR_PACKS.get(data)
        if not pack:
            await query.edit_message_text("Pack not found.")
            return

        req_id = str(random.randint(100000, 999999))
        DB["requests"][req_id] = {
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "pack": data,
            "stars": pack["stars"],
            "rub": pack["rub"],
            "label": pack["label"],
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
        }
        save_db(DB)

        await query.edit_message_text(
            f"Payment request created!\n\n"
            f"Pack: {pack['label']}\n"
            f"Request ID: {req_id}\n\n"
            f"Contact the owner to complete payment:\n"
            f"@Grayson or @Yourprince\n\n"
            f"After payment your balance will be topped up.",
            reply_markup=shop_menu(),
        )

        owner_text = (
            f"New payment request!\n\n"
            f"User: @{user.username or user.first_name or user.id}\n"
            f"ID: {user.id}\n"
            f"Pack: {pack['label']}\n"
            f"Request ID: {req_id}\n\n"
            f"To confirm write: /confirm_{req_id}"
        )
        for uid_str, usr in DB["users"].items():
            if usr.get("username", "").lower() in OWNER_USERNAMES:
                try:
                    await context.bot.send_message(chat_id=usr["id"], text=owner_text)
                except Exception as e:
                    logger.warning("Can't notify owner %s: %s", usr["id"], e)
        return

    if data.startswith("bet_"):
        await query.answer()
        parts = data.split("_")
        game = parts[1]
        bet = int(parts[2])

        if u["balance"] < bet:
            await query.edit_message_text(
                f"Not enough funds. Balance: {u['balance']}",
                reply_markup=play_menu(),
            )
            return

        u["games"] += 1
        u["balance"] -= bet
        result = ""

        if game == "dice":
            if random.random() < 0.35:
                win = bet * 2
                u["balance"] += win
                u["wins"] += 1
                result = f"Dice WIN! +{win - bet} (x2)"
            else:
                u["losses"] += 1
                result = f"Dice LOSS. -{bet}"

        elif game == "roulette":
            if random.random() < 0.20:
                win = bet * 3
                u["balance"] += win
                u["wins"] += 1
                result = f"Roulette WIN! +{win - bet} (x3)"
            else:
                u["losses"] += 1
                result = f"Roulette LOSS. -{bet}"

        elif game == "slots":
            if random.random() < 0.12:
                win = bet * 5
                u["balance"] += win
                u["wins"] += 1
                result = f"JACKPOT! +{win - bet} (x5)"
            elif random.random() < 0.30:
                win = bet * 2
                u["balance"] += win
                u["wins"] += 1
                result = f"Slots match! +{win - bet} (x2)"
            else:
                u["losses"] += 1
                result = f"Slots miss. -{bet}"

        elif game == "bj":
            if random.random() < 0.42:
                win = int(bet * 2.5)
                u["balance"] += win
                u["wins"] += 1
                result = f"Blackjack! +{win - bet} (x2.5)"
            else:
                u["losses"] += 1
                result = f"Bust. -{bet}"

        else:
            result = "Unknown game."

        save_db(DB)
        await query.edit_message_text(
            f"{result}\n\nBalance: {u['balance']}",
            reply_markup=play_menu(),
        )
        return

    if data.startswith("admin_"):
        if not is_owner(update):
            await query.answer("No access", show_alert=True)
            return

        if data == "admin_menu":
            await query.answer()
            await query.edit_message_text("Admin menu:", reply_markup=admin_menu())
            return

        if data == "admin_stats":
            await query.answer()
            total_users = len(DB["users"])
            total_money = sum(x["balance"] for x in DB["users"].values())
            total_games = sum(x["games"] for x in DB["users"].values())
            total_stars = sum(x.get("stars_spent", 0) for x in DB["users"].values())
            pending = sum(1 for r in DB["requests"].values() if r["status"] == "pending")
            await query.edit_message_text(
                f"Statistics:\n\n"
                f"Users: {total_users}\n"
                f"Money in system: {total_money}\n"
                f"Games played: {total_games}\n"
                f"Stars spent: {total_stars}\n"
                f"Pending requests: {pending}",
                reply_markup=admin_menu(),
            )
            return

        if data == "admin_give":
            await query.answer()
            context.user_data["admin_await"] = "give"
            await query.edit_message_text(
                "Send message in format:\n<user_id> <amount>\n\nExample: 123456789 500",
                reply_markup=admin_menu(),
            )
            return

        if data == "admin_broadcast":
            await query.answer()
            context.user_data["admin_await"] = "broadcast"
            await query.edit_message_text(
                "Send text for broadcast to all users.",
                reply_markup=admin_menu(),
            )
            return

        if data == "admin_settings":
            await query.answer()
            s = DB["settings"]
            await query.edit_message_text(
                f"Settings:\n\n"
                f"Start balance: {s['start_balance']}\n"
                f"Min bet: {s['min_bet']}\n"
                f"Max bet: {s['max_bet']}\n\n"
                f"To change, write:\n"
                f"start_balance 500\nmin_bet 5\nmax_bet 10000",
                reply_markup=admin_menu(),
            )
            return

        if data == "admin_requests":
            await query.answer()
            pending = [(k, v) for k, v in DB["requests"].items() if v["status"] == "pending"]
            if not pending:
                await query.edit_message_text("No pending requests.", reply_markup=admin_menu())
                return

            text = "Pending requests:\n\n"
            rows = []
            for req_id, r in pending[:10]:
                text += (
                    f"ID: {req_id}\n"
                    f"User: @{r['username'] or r['first_name']} ({r['user_id']})\n"
                    f"Pack: {r['label']}\n\n"
                )
                rows.append([
                    InlineKeyboardButton(
                        f"Approve {req_id}",
                        callback_data=f"approve_{req_id}"
                    ),
                    InlineKeyboardButton(
                        f"Reject {req_id}",
                        callback_data=f"reject_{req_id}"
                    ),
                ])
            rows.append([InlineKeyboardButton("Back", callback_data="admin_menu")])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
            return

    if data.startswith("approve_"):
        if not is_owner(update):
            await query.answer("No access", show_alert=True)
            return
        req_id = data.replace("approve_", "")
        req = DB["requests"].get(req_id)
        if not req or req["status"] != "pending":
            await query.answer("Request not found", show_alert=True)
            return
        uid = str(req["user_id"])
        if uid in DB["users"]:
            DB["users"][uid]["balance"] += req["rub"]
            DB["users"][uid]["stars_spent"] = DB["users"][uid].get("stars_spent", 0) + req["stars"]
        req["status"] = "approved"
        save_db(DB)
        await query.answer("Approved!")
        await query.edit_message_text(
            f"Request {req_id} approved.\nUser {req['user_id']} got {req['rub']}.",
            reply_markup=admin_menu(),
        )
        try:
            await context.bot.send_message(
                chat_id=req["user_id"],
                text=f"Payment approved!\nBalance topped up by {req['rub']}.",
            )
        except Exception:
            pass
        return

    if data.startswith("reject_"):
        if not is_owner(update):
            await query.answer("No access", show_alert=True)
            return
        req_id = data.replace("reject_", "")
        req = DB["requests"].get(req_id)
        if not req:
            await query.answer("Request not found", show_alert=True)
            return
        req["status"] = "rejected"
        save_db(DB)
        await query.answer("Rejected")
        await query.edit_message_text(
            f"Request {req_id} rejected.",
            reply_markup=admin_menu(),
        )
        return

    await query.answer()


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user(user.id, user.username, user.first_name)
    text = update.message.text.strip()

    if is_owner(update) and text.startswith("/confirm_"):
        req_id = text.replace("/confirm_", "").strip()
        req = DB["requests"].get(req_id)
        if not req or req["status"] != "pending":
            await update.message.reply_text("Request not found or already processed.")
            return
        uid = str(req["user_id"])
        if uid in DB["users"]:
            DB["users"][uid]["balance"] += req["rub"]
            DB["users"][uid]["stars_spent"] = DB["users"][uid].get("stars_spent", 0) + req["stars"]
        req["status"] = "approved"
        save_db(DB)
        await update.message.reply_text(
            f"Request {req_id} approved. User {req['user_id']} got {req['rub']}.",
            reply_markup=admin_menu(),
        )
        try:
            await context.bot.send_message(
                chat_id=req["user_id"],
                text=f"Payment approved!\nBalance topped up by {req['rub']}.",
            )
        except Exception:
            pass
        return

    if is_owner(update) and context.user_data.get("admin_await"):
        mode = context.user_data.pop("admin_await")

        if mode == "give":
            try:
                parts = text.split()
                target_id = str(int(parts[0]))
                amount = int(parts[1])
                if target_id not in DB["users"]:
                    await update.message.reply_text("User not found.")
                    return
                DB["users"][target_id]["balance"] += amount
                save_db(DB)
                await update.message.reply_text(
                    f"Given {amount} to user {target_id}.",
                    reply_markup=admin_menu(),
                )
            except Exception:
                await update.message.reply_text("Format: <user_id> <amount>")
            return

        if mode == "broadcast":
            sent, failed = 0, 0
            for uid in DB["users"].keys():
                try:
                    await context.bot.send_message(chat_id=int(uid), text=text)
                    sent += 1
                except Exception:
                    failed += 1
            await update.message.reply_text(
                f"Broadcast done. Sent: {sent}, failed: {failed}.",
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
                        f"{key} = {val}",
                        reply_markup=admin_menu(),
                    )
                except Exception:
                    await update.message.reply_text("Wrong value.")
                return

    await update.message.reply_text("Use /menu to navigate.")


# ============ MAIN ============

def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url(BASE_URL)
        .base_file_url(BASE_FILE_URL)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Casino bot started on %s", BASE_URL)
    app.run_polling(allowed_updates=["message", "edited_message", "callback_query"])


if __name__ == "__main__":
    main()
