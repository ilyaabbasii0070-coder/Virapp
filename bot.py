import os
import sqlite3
import random
import string
import threading
from datetime import datetime

import telebot
from telebot import types
from flask import Flask, jsonify

# ─────────────────────────────────────────────
#  ENV
# ─────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "8030883585"))
SECRET_KEY = os.environ.get("SECRET_KEY", "viranet_secret")
PORT       = int(os.environ.get("PORT", 8080))

SUPPORT_USERNAME = "ViraNet0"
CARD_NUMBER      = "123456789456123"
CARD_OWNER       = "حسین حسینی"
REFERRAL_BONUS   = 5000

PLANS = {
    "1gb": {"label": "⚡ 1GB  —  30 روز  —  400,000 تومان", "gb": 1, "days": 30, "price": 400_000},
    "2gb": {"label": "🚀 2GB  —  30 روز  —  780,000 تومان", "gb": 2, "days": 30, "price": 780_000},
    "3gb": {"label": "🔥 3GB  —  30 روز  —  1,100,000 تومان", "gb": 3, "days": 30, "price": 1_100_000},
    "5gb": {"label": "💥 5GB  —  30 روز  —  1,800,000 تومان", "gb": 5, "days": 30, "price": 1_800_000},
}

# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────
DB_PATH = "viranet.db"

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY,
            user_id       INTEGER UNIQUE NOT NULL,
            username      TEXT,
            full_name     TEXT,
            wallet        INTEGER DEFAULT 0,
            referral_code TEXT UNIQUE,
            referred_by   INTEGER,
            is_banned     INTEGER DEFAULT 0,
            joined_at     TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS orders (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            plan_key       TEXT NOT NULL,
            quantity       INTEGER NOT NULL,
            total_price    INTEGER NOT NULL,
            payment_method TEXT,
            status         TEXT DEFAULT 'pending',
            created_at     TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS order_services (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            service_name TEXT NOT NULL,
            config_text  TEXT,
            plan_key     TEXT NOT NULL,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS receipts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            order_id     INTEGER,
            wallet_amount INTEGER,
            receipt_type TEXT NOT NULL,
            file_id      TEXT,
            status       TEXT DEFAULT 'pending',
            admin_msg_id INTEGER,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS wallet_requests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            amount       INTEGER NOT NULL,
            status       TEXT DEFAULT 'pending',
            admin_msg_id INTEGER,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        """)
    print("✅ Database initialized")

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def get_user(user_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def ensure_user(tg_user, referred_by=None):
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM users WHERE user_id=?", (tg_user.id,)).fetchone()
        if not existing:
            ref_code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
            full = (tg_user.first_name or "") + (" " + tg_user.last_name if tg_user.last_name else "")
            conn.execute(
                "INSERT INTO users(user_id,username,full_name,referral_code,referred_by) VALUES(?,?,?,?,?)",
                (tg_user.id, tg_user.username, full.strip(), ref_code, referred_by)
            )
            if referred_by:
                conn.execute("UPDATE users SET wallet=wallet+? WHERE user_id=?", (REFERRAL_BONUS, referred_by))
            conn.commit()

def get_wallet(user_id):
    u = get_user(user_id)
    return u["wallet"] if u else 0

def add_wallet(user_id, amount):
    with get_db() as conn:
        conn.execute("UPDATE users SET wallet=wallet+? WHERE user_id=?", (amount, user_id))
        conn.commit()

def deduct_wallet(user_id, amount):
    with get_db() as conn:
        conn.execute("UPDATE users SET wallet=wallet-? WHERE user_id=?", (amount, user_id))
        conn.commit()

def fmt(p):
    return f"{p:,}"

def random_name():
    adj  = ["Swift", "Storm", "Nova", "Volt", "Blaze", "Echo", "Apex", "Core", "Flux", "Zen"]
    noun = ["Link", "Node", "Wave", "Star", "Gate", "Net", "Byte", "Cloud", "Edge", "Hub"]
    return f"{random.choice(adj)}{random.choice(noun)}{random.randint(10,99)}"

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")

# ─────────────────────────────────────────────
#  STATE MACHINE
# ─────────────────────────────────────────────
user_states: dict = {}

def set_state(uid, **kw):
    user_states.setdefault(uid, {}).update(kw)

def get_state(uid):
    return user_states.get(uid, {})

def clear_state(uid):
    user_states.pop(uid, None)

# ─────────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ── Main Menu (Inline Keyboard) ─────────────
def main_menu_kb(user_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔵 فروشگاه", callback_data="menu_shop"),
        types.InlineKeyboardButton("💰 کیف پول", callback_data="menu_wallet"),
    )
    kb.add(
        types.InlineKeyboardButton("📦 سرویس‌های من", callback_data="menu_services"),
        types.InlineKeyboardButton("👥 دعوت دوستان", callback_data="menu_referral"),
    )
    kb.add(
        types.InlineKeyboardButton("🆘 پشتیبانی", url=f"https://t.me/{SUPPORT_USERNAME}"),
    )
    if user_id == ADMIN_ID:
        kb.add(types.InlineKeyboardButton("⚙️ پنل ادمین", callback_data="menu_admin"))
    return kb

def send_main_menu(chat_id, user_id, text=None):
    msg_text = text or (
        "🏠 <b>منوی اصلی</b>\n\n"
        "گزینه مورد نظر را انتخاب کنید:"
    )
    bot.send_message(chat_id, msg_text, reply_markup=main_menu_kb(user_id))

# ─────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    args = msg.text.split()
    referred_by = None
    if len(args) > 1 and args[1].startswith("ref_"):
        ref_code = args[1][4:]
        with get_db() as conn:
            r = conn.execute("SELECT user_id FROM users WHERE referral_code=?", (ref_code,)).fetchone()
            if r and r["user_id"] != msg.from_user.id:
                referred_by = r["user_id"]

    ensure_user(msg.from_user, referred_by)
    clear_state(msg.from_user.id)

    bot.send_message(
        msg.chat.id,
        "✨ <b>به ویرا نت خوش آمدید!</b>\n\n"
        "💎 <b>سیستم حرفه‌ای مدیریت سرویس‌ها</b>\n\n"
        "🌐 با استفاده از این ربات می‌توانید سرویس‌های اینترنتی پرسرعت و باکیفیت ما را خریداری کنید.\n\n"
        "⚡ <b>ویژگی‌های ما:</b>\n"
        "  • سرعت بالا و پایداری کامل\n"
        "  • پشتیبانی ۲۴ ساعته\n"
        "  • فعال‌سازی فوری\n"
        "  • قیمت‌های رقابتی\n\n"
        "👇 از منوی زیر گزینه مورد نظر را انتخاب کنید:",
        reply_markup=main_menu_kb(msg.from_user.id)
    )

# ─────────────────────────────────────────────
#  MENU CALLBACKS
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "menu_shop")
def cb_menu_shop(call):
    u = get_user(call.from_user.id)
    if u and u["is_banned"]:
        return bot.answer_callback_query(call.id, "⛔ حساب شما مسدود شده است.", show_alert=True)
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user)
    clear_state(call.from_user.id)
    _show_shop(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_wallet")
def cb_menu_wallet(call):
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user)
    clear_state(call.from_user.id)
    _show_wallet(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_services")
def cb_menu_services(call):
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user)
    _show_my_services(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_referral")
def cb_menu_referral(call):
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user)
    _show_referral(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_admin")
def cb_menu_admin(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    bot.answer_callback_query(call.id)
    clear_state(ADMIN_ID)
    _show_admin_panel(call.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "back_main")
def cb_back_main(call):
    bot.answer_callback_query(call.id)
    clear_state(call.from_user.id)
    send_main_menu(call.message.chat.id, call.from_user.id)

# ─────────────────────────────────────────────
#  🛒 SHOP
# ─────────────────────────────────────────────
def _show_shop(chat_id, user_id):
    set_state(user_id, step="shop_plan")
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, plan in PLANS.items():
        kb.add(types.InlineKeyboardButton(plan["label"], callback_data=f"plan_{key}"))
    kb.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))
    bot.send_message(
        chat_id,
        "🛒 <b>فروشگاه ویرا نت</b>\n\n"
        "🎯 پلن مورد نظر خود را انتخاب کنید:\n\n"
        "تمامی سرویس‌ها شامل:\n"
        "  ✅ فعال‌سازی فوری\n"
        "  ✅ سرعت نامحدود\n"
        "  ✅ پشتیبانی کامل",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("plan_"))
def cb_plan(call):
    plan_key = call.data[5:]
    if plan_key not in PLANS:
        return bot.answer_callback_query(call.id, "پلن نامعتبر")
    bot.answer_callback_query(call.id)
    plan = PLANS[plan_key]
    set_state(call.from_user.id, step="shop_quantity", plan_key=plan_key)
    bot.send_message(
        call.message.chat.id,
        f"✅ <b>پلن انتخاب شده:</b> {plan['label']}\n\n"
        "🔢 <b>تعداد سرویس</b>\n\n"
        "چند سرویس می‌خواهید؟\n"
        f"💰 قیمت هر عدد: <b>{fmt(plan['price'])} تومان</b>\n\n"
        "👇 عدد تعداد را ارسال کنید (مثال: ۱ یا ۳):"
    )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "shop_quantity")
def shop_quantity(msg):
    try:
        qty = int(msg.text.strip())
        if qty < 1 or qty > 20:
            raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ لطفاً یک عدد صحیح بین ۱ تا ۲۰ وارد کنید.")
    state = get_state(msg.from_user.id)
    total = PLANS[state["plan_key"]]["price"] * qty
    set_state(msg.from_user.id, step="shop_name", quantity=qty, total_price=total, names=[], name_index=0)
    _ask_name(msg.chat.id, msg.from_user.id, 0, qty, state["plan_key"], total)

def _ask_name(chat_id, user_id, index, qty, plan_key, total):
    plan = PLANS[plan_key]
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎲 اسم رندم", callback_data=f"name_r_{index}"),
        types.InlineKeyboardButton("✍️ اسم دلخواه", callback_data=f"name_c_{index}"),
    )
    bot.send_message(
        chat_id,
        f"🏷 <b>نام‌گذاری سرویس {index + 1} از {qty}</b>\n\n"
        f"📦 پلن: <b>{plan['label']}</b>\n"
        f"💰 مبلغ کل: <b>{fmt(total)} تومان</b>\n\n"
        "روش نام‌گذاری را انتخاب کنید:\n\n"
        "  🎲 <b>اسم رندم</b> — سیستم یک نام منحصربه‌فرد انتخاب می‌کند\n"
        "  ✍️ <b>اسم دلخواه</b> — نام دلخواه خود را وارد کنید",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("name_r_") or c.data.startswith("name_c_"))
def cb_name(call):
    state = get_state(call.from_user.id)
    if state.get("step") != "shop_name":
        return bot.answer_callback_query(call.id)
    parts  = call.data.split("_")
    action = parts[1]
    index  = int(parts[2])
    bot.answer_callback_query(call.id)

    if action == "r":
        name  = random_name()
        names = state.get("names", [])
        names.append(name)
        qty   = state["quantity"]
        set_state(call.from_user.id, names=names, name_index=index + 1)
        bot.send_message(call.message.chat.id, f"✅ نام رندم ثبت شد: <b>{name}</b>")
        if index + 1 < qty:
            _ask_name(call.message.chat.id, call.from_user.id, index + 1, qty, state["plan_key"], state["total_price"])
        else:
            _ask_payment(call.message.chat.id, call.from_user.id)
    else:
        set_state(call.from_user.id, step="shop_name_input", name_index=index)
        bot.send_message(
            call.message.chat.id,
            f"✍️ <b>ارسال نام دلخواه — سرویس {index + 1}</b>\n\n"
            "نام دلخواه خود را برای این سرویس ارسال کنید:\n\n"
            "👇 نام را همین‌جا تایپ کنید:"
        )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "shop_name_input")
def shop_name_input(msg):
    name  = msg.text.strip()[:30]
    state = get_state(msg.from_user.id)
    names = state.get("names", [])
    names.append(name)
    index = state["name_index"]
    qty   = state["quantity"]
    set_state(msg.from_user.id, step="shop_name", names=names, name_index=index + 1)
    bot.send_message(msg.chat.id, f"✅ نام <b>{name}</b> ثبت شد.")
    if index + 1 < qty:
        _ask_name(msg.chat.id, msg.from_user.id, index + 1, qty, state["plan_key"], state["total_price"])
    else:
        _ask_payment(msg.chat.id, msg.from_user.id)

def _ask_payment(chat_id, user_id):
    state  = get_state(user_id)
    plan   = PLANS[state["plan_key"]]
    total  = state["total_price"]
    wallet = get_wallet(user_id)
    set_state(user_id, step="shop_payment")
    names_text = "\n".join([f"  {i+1}. {n}" for i, n in enumerate(state["names"])])
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(f"💰 پرداخت از کیف پول  (موجودی: {fmt(wallet)} تومان)", callback_data="pay_wallet"),
        types.InlineKeyboardButton("💳 پرداخت کارت به کارت", callback_data="pay_card"),
    )
    bot.send_message(
        chat_id,
        f"💳 <b>مرحله پرداخت</b>\n\n"
        f"📦 <b>پلن:</b> {plan['label']}\n"
        f"🔢 <b>تعداد:</b> {state['quantity']} سرویس\n"
        f"🏷 <b>نام‌ها:</b>\n{names_text}\n\n"
        f"💰 <b>مبلغ قابل پرداخت:</b> {fmt(total)} تومان\n\n"
        "روش پرداخت را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data in ("pay_wallet", "pay_card"))
def cb_payment(call):
    state = get_state(call.from_user.id)
    if state.get("step") != "shop_payment":
        return bot.answer_callback_query(call.id)
    bot.answer_callback_query(call.id)
    total  = state["total_price"]
    wallet = get_wallet(call.from_user.id)

    if call.data == "pay_wallet":
        if wallet < total:
            shortage = total - wallet
            return bot.send_message(
                call.message.chat.id,
                f"❌ <b>موجودی کیف پول کافی نیست</b>\n\n"
                f"💰 موجودی فعلی: <b>{fmt(wallet)} تومان</b>\n"
                f"💳 مبلغ مورد نیاز: <b>{fmt(total)} تومان</b>\n"
                f"⚠️ کمبود: <b>{fmt(shortage)} تومان</b>\n\n"
                "برای شارژ کیف پول از منوی اصلی اقدام کنید."
            )
        deduct_wallet(call.from_user.id, total)
        _create_order(call.from_user.id, call.message.chat.id, state, "wallet")

    else:
        set_state(call.from_user.id, step="shop_receipt_wait")
        bot.send_message(
            call.message.chat.id,
            f"💳 <b>پرداخت کارت به کارت</b>\n\n"
            f"💰 <b>مبلغ پرداختی:</b> {fmt(total)} تومان\n\n"
            "🏦 <b>مشخصات حساب جهت واریز:</b>\n\n"
            f"  💳 شماره کارت:\n  <code>{CARD_NUMBER}</code>\n\n"
            f"  👤 به نام: <b>{CARD_OWNER}</b>\n\n"
            "📌 <b>مراحل پرداخت:</b>\n"
            f"۱. مبلغ <b>{fmt(total)} تومان</b> را به کارت بالا واریز کنید\n"
            "۲. تصویر رسید واریزی را ذخیره کنید\n"
            "۳. رسید را همین‌جا در چت ارسال کنید\n\n"
            "⏳ زمان بررسی: معمولاً کمتر از ۳۰ دقیقه\n\n"
            "👇 تصویر رسید واریزی خود را ارسال کنید:"
        )

def _create_order(user_id, chat_id, state, payment_method):
    plan_key = state["plan_key"]
    plan     = PLANS[plan_key]
    qty      = state["quantity"]
    total    = state["total_price"]
    names    = state["names"]

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO orders(user_id,plan_key,quantity,total_price,payment_method,status) VALUES(?,?,?,?,?,?)",
            (user_id, plan_key, qty, total, payment_method, "pending")
        )
        order_id = cur.lastrowid
        for name in names:
            conn.execute(
                "INSERT INTO order_services(order_id,user_id,service_name,plan_key) VALUES(?,?,?,?)",
                (order_id, user_id, name, plan_key)
            )
        conn.commit()

    set_state(user_id, step="shop_receipt_wait", order_id=order_id)

    if payment_method == "wallet":
        u     = get_user(user_id)
        uname = u["username"] or u["full_name"] or str(user_id)
        names_t = "\n".join([f"  {i+1}. {n}" for i, n in enumerate(names)])
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ تایید — ارسال کانفیگ", callback_data=f"adm_ok_{order_id}"),
            types.InlineKeyboardButton("❌ رد سفارش", callback_data=f"adm_rej_{order_id}"),
        )
        bot.send_message(
            ADMIN_ID,
            f"🛒 <b>سفارش جدید — پرداخت کیف پول</b>\n\n"
            f"👤 کاربر: @{uname}  |  آیدی: <code>{user_id}</code>\n"
            f"🕐 زمان: {now_str()}\n\n"
            f"📦 پلن: <b>{plan['label']}</b>\n"
            f"🔢 تعداد: {qty} سرویس\n"
            f"🏷 نام‌ها:\n{names_t}\n\n"
            f"💰 مبلغ: <b>{fmt(total)} تومان</b>\n"
            f"💳 روش پرداخت: کیف پول",
            reply_markup=kb
        )
        clear_state(user_id)
        bot.send_message(
            chat_id,
            "✅ <b>سفارش شما ثبت شد!</b>\n\n"
            f"💰 مبلغ <b>{fmt(total)} تومان</b> از کیف پول شما کسر شد.\n"
            "📋 سفارش در صف بررسی قرار گرفت.\n\n"
            "⏳ پس از تایید ادمین، کانفیگ‌های شما ارسال خواهد شد."
        )

# ── Receipt from user ─────────────────────────
@bot.message_handler(
    content_types=["photo"],
    func=lambda m: get_state(m.from_user.id).get("step") == "shop_receipt_wait"
)
def shop_receipt(msg):
    state    = get_state(msg.from_user.id)
    order_id = state.get("order_id")
    if not order_id:
        return

    file_id = msg.photo[-1].file_id
    u       = get_user(msg.from_user.id)
    uname   = u["username"] or u["full_name"] or str(msg.from_user.id)

    with get_db() as conn:
        order    = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        svc_rows = conn.execute("SELECT * FROM order_services WHERE order_id=?", (order_id,)).fetchall()

    plan    = PLANS[order["plan_key"]]
    names   = [r["service_name"] for r in svc_rows]
    names_t = "\n".join([f"  {i+1}. {n}" for i, n in enumerate(names)])

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ تایید — ارسال کانفیگ", callback_data=f"adm_ok_{order_id}"),
        types.InlineKeyboardButton("❌ رد رسید", callback_data=f"adm_rej_{order_id}"),
    )

    adm_msg = bot.send_photo(
        ADMIN_ID,
        file_id,
        caption=(
            f"📥 <b>رسید جدید — خرید سرویس</b>\n\n"
            f"👤 کاربر: @{uname}  |  آیدی: <code>{msg.from_user.id}</code>\n"
            f"🕐 زمان ارسال: {now_str()}\n\n"
            f"📦 پلن انتخابی: <b>{plan['label']}</b>\n"
            f"🔢 تعداد: {order['quantity']} سرویس\n"
            f"🏷 نام‌ها:\n{names_t}\n\n"
            f"💰 مبلغ: <b>{fmt(order['total_price'])} تومان</b>\n"
            f"💳 روش: کارت به کارت"
        ),
        reply_markup=kb
    )

    with get_db() as conn:
        conn.execute(
            "INSERT INTO receipts(user_id,order_id,file_id,receipt_type,status,admin_msg_id) VALUES(?,?,?,?,?,?)",
            (msg.from_user.id, order_id, file_id, "purchase_card", "pending", adm_msg.message_id)
        )
        conn.commit()

    clear_state(msg.from_user.id)
    bot.send_message(
        msg.chat.id,
        "📥 <b>رسید شما دریافت شد!</b>\n\n"
        "✅ رسید واریزی شما با موفقیت به تیم پشتیبانی ارسال شد.\n\n"
        "⏳ <b>منتظر تایید ادمین باشید...</b>\n\n"
        "پس از تایید، کانفیگ‌های شما در همین چت ارسال خواهد شد.\n\n"
        "📌 زمان بررسی: معمولاً کمتر از ۳۰ دقیقه\n\n"
        f"در صورت سوال: @{SUPPORT_USERNAME}"
    )

# ─────────────────────────────────────────────
#  ADMIN: APPROVE / REJECT ORDER
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_ok_"))
def cb_admin_approve(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    order_id = int(call.data[7:])
    bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_config", order_id=order_id, configs=[])
    bot.send_message(
        call.message.chat.id,
        f"✅ <b>تایید سفارش #{order_id}</b>\n\n"
        "لطفاً کانفیگ مشتری را وارد کنید.\n\n"
        "اگر تعداد بیش از ۱ است، هر کانفیگ را در یک پیام جداگانه ارسال کنید.\n"
        "بعد از ارسال همه کانفیگ‌ها /done بفرستید."
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_rej_"))
def cb_admin_reject(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    order_id = int(call.data[8:])
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        conn.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
        conn.commit()
    bot.send_message(
        order["user_id"],
        "❌ <b>رسید شما رد شد</b>\n\n"
        "متأسفانه رسید ارسالی شما مورد تایید قرار نگرفت.\n\n"
        "🔍 دلایل احتمالی:\n"
        "  • رسید نامعتبر یا غیرخوانا\n"
        "  • مغایرت مبلغ واریزی\n"
        "  • تصویر رسید مخدوش\n\n"
        "برای پیگیری و اطلاعات بیشتر با پشتیبانی تماس بگیرید:\n"
        f"📞 @{SUPPORT_USERNAME}"
    )
    bot.send_message(call.message.chat.id, f"❌ سفارش #{order_id} رد شد و کاربر مطلع شد.")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_config")
def adm_receive_config(msg):
    if msg.text and msg.text.strip() == "/done":
        state    = get_state(ADMIN_ID)
        order_id = state["order_id"]
        configs  = state.get("configs", [])
        _deliver_configs(order_id, configs)
        clear_state(ADMIN_ID)
        return bot.send_message(msg.chat.id, f"✅ {len(configs)} کانفیگ با موفقیت ارسال شد.")

    config = (msg.text or "").strip()
    if not config:
        return bot.send_message(msg.chat.id, "⚠️ متن کانفیگ را ارسال کنید.")
    state   = get_state(ADMIN_ID)
    configs = state.get("configs", [])
    configs.append(config)
    set_state(ADMIN_ID, configs=configs)
    bot.send_message(msg.chat.id, f"✅ کانفیگ {len(configs)} ثبت شد. بعدی را ارسال کنید یا /done بفرستید.")

def _deliver_configs(order_id, configs):
    with get_db() as conn:
        order    = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        svc_rows = conn.execute("SELECT * FROM order_services WHERE order_id=? ORDER BY id", (order_id,)).fetchall()

    plan    = PLANS[order["plan_key"]]
    user_id = order["user_id"]

    for i, svc in enumerate(svc_rows):
        cfg = configs[i] if i < len(configs) else "---"
        with get_db() as conn:
            conn.execute("UPDATE order_services SET config_text=? WHERE id=?", (cfg, svc["id"]))
            conn.commit()

        bot.send_message(
            user_id,
            f"🎉 <b>سرویس شما آماده است!</b>\n\n"
            f"🏷 <b>نام سرویس:</b> {svc['service_name']}\n"
            f"📊 <b>حجم:</b> {plan['gb']} گیگابایت\n"
            f"📅 <b>مدت اعتبار:</b> {plan['days']} روز\n\n"
            "🔐 <b>کانفیگ اتصال شما:</b>\n\n"
            f"<code>{cfg}</code>\n\n"
            "📌 <b>راهنمای استفاده:</b>\n"
            "کد بالا را کپی کرده و در اپلیکیشن مورد نظر ایمپورت کنید.\n\n"
            f"در صورت نیاز به راهنمایی: @{SUPPORT_USERNAME}\n\n"
            "از خرید شما سپاسگزاریم! 🙏"
        )

    with get_db() as conn:
        conn.execute("UPDATE orders SET status='delivered' WHERE id=?", (order_id,))
        conn.commit()

# ─────────────────────────────────────────────
#  📦 MY SERVICES
# ─────────────────────────────────────────────
def _show_my_services(chat_id, user_id):
    with get_db() as conn:
        svcs = conn.execute(
            "SELECT * FROM order_services WHERE user_id=? AND config_text IS NOT NULL ORDER BY id DESC",
            (user_id,)
        ).fetchall()

    if not svcs:
        return bot.send_message(
            chat_id,
            "📦 <b>سرویس‌های من</b>\n\n"
            "شما هنوز سرویس فعالی ندارید.\n\n"
            "برای خرید از منوی اصلی به فروشگاه مراجعه کنید."
        )

    kb = types.InlineKeyboardMarkup(row_width=1)
    for svc in svcs:
        plan  = PLANS.get(svc["plan_key"], {})
        label = f"📦 {svc['service_name']}  —  {plan.get('gb','?')}GB"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"vs_{svc['id']}"))
    kb.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))

    bot.send_message(
        chat_id,
        f"📦 <b>سرویس‌های من</b>\n\n"
        f"شما <b>{len(svcs)}</b> سرویس فعال دارید.\n\n"
        "برای مشاهده جزئیات روی هر سرویس کلیک کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("vs_"))
def cb_view_svc(call):
    svc_id = int(call.data[3:])
    with get_db() as conn:
        svc = conn.execute("SELECT * FROM order_services WHERE id=? AND user_id=?", (svc_id, call.from_user.id)).fetchone()
    if not svc:
        return bot.answer_callback_query(call.id, "سرویس یافت نشد")
    plan = PLANS.get(svc["plan_key"], {})
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"📦 <b>جزئیات سرویس</b>\n\n"
        f"🏷 <b>نام:</b> {svc['service_name']}\n"
        f"📊 <b>حجم:</b> {plan.get('gb', '?')} گیگابایت\n"
        f"📅 <b>مدت:</b> {plan.get('days', '?')} روز\n\n"
        "🔐 <b>کانفیگ:</b>\n\n"
        f"<code>{svc['config_text']}</code>"
    )

# ─────────────────────────────────────────────
#  💰 WALLET
# ─────────────────────────────────────────────
def _show_wallet(chat_id, user_id):
    wallet = get_wallet(user_id)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("💳 شارژ کیف پول", callback_data="wallet_charge"),
        types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"),
    )
    bot.send_message(
        chat_id,
        "💰 <b>کیف پول</b>\n\n"
        f"💎 <b>موجودی فعلی:</b> {fmt(wallet)} تومان\n\n"
        "برای شارژ کیف پول دکمه زیر را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "wallet_charge")
def cb_wallet_charge(call):
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, step="wallet_amount")
    bot.send_message(
        call.message.chat.id,
        "💳 <b>شارژ کیف پول</b>\n\n"
        "💰 لطفاً مبلغ مورد نظر را به تومان وارد کنید.\n\n"
        "📌 حداقل مبلغ شارژ: <b>۵۰,۰۰۰ تومان</b>\n\n"
        "👇 مبلغ را وارد کنید (مثال: 100000):"
    )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "wallet_amount")
def wallet_amount(msg):
    try:
        amount = int(msg.text.strip().replace(",", "").replace("٬", ""))
        if amount < 50000:
            raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ لطفاً یک مبلغ معتبر (حداقل ۵۰,۰۰۰ تومان) وارد کنید.")

    set_state(msg.from_user.id, step="wallet_receipt", wallet_amount=amount)
    bot.send_message(
        msg.chat.id,
        f"💳 <b>شارژ کیف پول — مرحله پرداخت</b>\n\n"
        f"💰 <b>مبلغ شارژ:</b> {fmt(amount)} تومان\n\n"
        "🏦 <b>مشخصات حساب جهت واریز:</b>\n\n"
        f"  💳 شماره کارت:\n  <code>{CARD_NUMBER}</code>\n\n"
        f"  👤 به نام: <b>{CARD_OWNER}</b>\n\n"
        f"۱. مبلغ <b>{fmt(amount)} تومان</b> را واریز کنید\n"
        "۲. تصویر رسید واریزی را در همین چت ارسال کنید\n\n"
        "⏳ پس از تایید ادمین، موجودی کیف پول شما شارژ خواهد شد.\n\n"
        "👇 تصویر رسید را ارسال کنید:"
    )

@bot.message_handler(
    content_types=["photo"],
    func=lambda m: get_state(m.from_user.id).get("step") == "wallet_receipt"
)
def wallet_receipt(msg):
    state   = get_state(msg.from_user.id)
    amount  = state["wallet_amount"]
    file_id = msg.photo[-1].file_id
    u       = get_user(msg.from_user.id)
    uname   = u["username"] or u["full_name"] or str(msg.from_user.id)

    with get_db() as conn:
        cur    = conn.execute("INSERT INTO wallet_requests(user_id,amount) VALUES(?,?)", (msg.from_user.id, amount))
        req_id = cur.lastrowid
        conn.commit()

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ تایید شارژ", callback_data=f"wadm_ok_{req_id}_{msg.from_user.id}_{amount}"),
        types.InlineKeyboardButton("❌ رد", callback_data=f"wadm_rej_{req_id}_{msg.from_user.id}"),
    )

    adm_msg = bot.send_photo(
        ADMIN_ID,
        file_id,
        caption=(
            f"💰 <b>درخواست شارژ کیف پول</b>\n\n"
            f"👤 کاربر: @{uname}  |  آیدی: <code>{msg.from_user.id}</code>\n"
            f"🕐 زمان: {now_str()}\n\n"
            f"💰 مبلغ درخواستی: <b>{fmt(amount)} تومان</b>"
        ),
        reply_markup=kb
    )

    with get_db() as conn:
        conn.execute("UPDATE wallet_requests SET admin_msg_id=? WHERE id=?", (adm_msg.message_id, req_id))
        conn.commit()

    clear_state(msg.from_user.id)
    bot.send_message(
        msg.chat.id,
        "📥 <b>رسید دریافت شد!</b>\n\n"
        "✅ رسید شما به تیم پشتیبانی ارسال شد.\n"
        "⏳ <b>منتظر تایید ادمین باشید...</b>\n\n"
        "پس از تایید، موجودی کیف پول شما به‌روزرسانی خواهد شد."
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("wadm_ok_"))
def cb_wallet_approve(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    parts   = call.data.split("_")
    req_id  = int(parts[2])
    user_id = int(parts[3])
    amount  = int(parts[4])
    bot.answer_callback_query(call.id)
    add_wallet(user_id, amount)
    with get_db() as conn:
        conn.execute("UPDATE wallet_requests SET status='approved' WHERE id=?", (req_id,))
        conn.commit()
    new_bal = get_wallet(user_id)
    bot.send_message(
        user_id,
        f"✅ <b>کیف پول شما شارژ شد!</b>\n\n"
        f"💰 مبلغ شارژ: <b>{fmt(amount)} تومان</b>\n"
        f"💎 موجودی جدید: <b>{fmt(new_bal)} تومان</b>\n\n"
        "از شارژ کیف پول شما سپاسگزاریم! 🙏"
    )
    bot.send_message(call.message.chat.id, f"✅ کیف پول کاربر {user_id} — {fmt(amount)} تومان شارژ شد.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("wadm_rej_"))
def cb_wallet_reject(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    parts   = call.data.split("_")
    req_id  = int(parts[2])
    user_id = int(parts[3])
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        conn.execute("UPDATE wallet_requests SET status='rejected' WHERE id=?", (req_id,))
        conn.commit()
    bot.send_message(
        user_id,
        "❌ <b>درخواست شارژ رد شد</b>\n\n"
        "متأسفانه رسید ارسالی شما مورد تایید قرار نگرفت.\n\n"
        f"برای پیگیری: @{SUPPORT_USERNAME}"
    )
    bot.send_message(call.message.chat.id, f"❌ درخواست شارژ کاربر {user_id} رد شد.")

# ─────────────────────────────────────────────
#  👥 REFERRAL
# ─────────────────────────────────────────────
def _show_referral(chat_id, user_id):
    u        = get_user(user_id)
    bot_info = bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{u['referral_code']}"
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) as c FROM users WHERE referred_by=?", (user_id,)).fetchone()["c"]
    bot.send_message(
        chat_id,
        "👥 <b>دعوت دوستان</b>\n\n"
        f"🎁 به ازای هر دعوت موفق: <b>{fmt(REFERRAL_BONUS)} تومان</b>\n\n"
        f"👤 تعداد دعوت‌های موفق: <b>{count}</b>\n"
        f"💰 درآمد کسب شده: <b>{fmt(count * REFERRAL_BONUS)} تومان</b>\n\n"
        "🔗 <b>لینک اختصاصی شما:</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        "این لینک را برای دوستان ارسال کنید."
    )

# ─────────────────────────────────────────────
#  ⚙️ ADMIN PANEL (Inline — only in admin's DM)
# ─────────────────────────────────────────────
def _show_admin_panel(chat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("👥 لیست کاربران", callback_data="ap_users_0"),
        types.InlineKeyboardButton("📊 آمار کلی", callback_data="ap_stats"),
        types.InlineKeyboardButton("📋 رسیدهای معلق", callback_data="ap_pending"),
    )
    bot.send_message(
        chat_id,
        "⚙️ <b>پنل ادمین ویرا نت</b>\n\n"
        "گزینه مورد نظر را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "ap_stats" and c.from_user.id == ADMIN_ID)
def cb_ap_stats(call):
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        uc  = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        pc  = conn.execute("SELECT COUNT(*) as c FROM receipts WHERE status='pending'").fetchone()["c"]
        ts  = conn.execute("SELECT SUM(total_price) as s FROM orders WHERE status='delivered'").fetchone()["s"] or 0
    bot.send_message(
        call.message.chat.id,
        "📊 <b>آمار کلی</b>\n\n"
        f"👥 تعداد کاربران: <b>{uc}</b>\n"
        f"📥 رسیدهای در انتظار: <b>{pc}</b>\n"
        f"💰 فروش کل: <b>{fmt(ts)} تومان</b>"
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_users_") and c.from_user.id == ADMIN_ID)
def cb_ap_users(call):
    bot.answer_callback_query(call.id)
    page   = int(call.data[9:])
    limit  = 8
    offset = page * limit
    with get_db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        total = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]

    if not users:
        return bot.send_message(call.message.chat.id, "کاربری یافت نشد.")

    kb = types.InlineKeyboardMarkup(row_width=1)
    for u in users:
        label = f"{'⛔ ' if u['is_banned'] else ''}@{u['username'] or u['full_name'] or u['user_id']}  |  {fmt(u['wallet'])} تومان"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"ap_user_{u['user_id']}"))

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️ قبلی", callback_data=f"ap_users_{page-1}"))
    if offset + limit < total:
        nav.append(types.InlineKeyboardButton("بعدی ▶️", callback_data=f"ap_users_{page+1}"))
    if nav:
        kb.add(*nav)
    kb.add(types.InlineKeyboardButton("🔍 جستجوی کاربر", callback_data="ap_search"))
    kb.add(types.InlineKeyboardButton("🔙 پنل ادمین", callback_data="menu_admin"))

    bot.send_message(
        call.message.chat.id,
        f"👥 <b>لیست کاربران</b>  ({total} نفر)\n\nصفحه {page+1}:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "ap_search" and c.from_user.id == ADMIN_ID)
def cb_ap_search(call):
    bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_search")
    bot.send_message(call.message.chat.id, "🔍 آیدی عددی یا یوزرنیم کاربر را ارسال کنید:")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_search")
def adm_search(msg):
    q = msg.text.strip().lstrip("@")
    with get_db() as conn:
        u = (
            conn.execute("SELECT * FROM users WHERE user_id=?", (q,)).fetchone()
            or conn.execute("SELECT * FROM users WHERE username LIKE ?", (f"%{q}%",)).fetchone()
            or conn.execute("SELECT * FROM users WHERE full_name LIKE ?", (f"%{q}%",)).fetchone()
        )
    if not u:
        return bot.send_message(msg.chat.id, "❌ کاربر یافت نشد.")
    clear_state(ADMIN_ID)
    _show_user_detail(msg.chat.id, u["user_id"])

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_user_") and c.from_user.id == ADMIN_ID)
def cb_ap_user(call):
    bot.answer_callback_query(call.id)
    uid = int(call.data[8:])
    _show_user_detail(call.message.chat.id, uid)

def _show_user_detail(chat_id, uid):
    u = get_user(uid)
    if not u:
        return bot.send_message(chat_id, "❌ کاربر یافت نشد.")
    with get_db() as conn:
        orders_count = conn.execute("SELECT COUNT(*) as c FROM orders WHERE user_id=?", (uid,)).fetchone()["c"]
        orders_total = conn.execute("SELECT SUM(total_price) as s FROM orders WHERE user_id=? AND status='delivered'", (uid,)).fetchone()["s"] or 0

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ شارژ کیف پول", callback_data=f"ap_add_{uid}"),
        types.InlineKeyboardButton("➖ کم کردن موجودی", callback_data=f"ap_sub_{uid}"),
    )
    kb.add(
        types.InlineKeyboardButton("⛔ بن" if not u["is_banned"] else "✅ رفع بن", callback_data=f"ap_ban_{uid}"),
    )
    kb.add(types.InlineKeyboardButton("🔙 لیست کاربران", callback_data="ap_users_0"))

    bot.send_message(
        chat_id,
        f"👤 <b>اطلاعات کاربر</b>\n\n"
        f"🆔 آیدی: <code>{uid}</code>\n"
        f"👤 نام: {u['full_name'] or '---'}\n"
        f"📛 یوزرنیم: @{u['username'] or '---'}\n"
        f"💰 موجودی کیف پول: <b>{fmt(u['wallet'])} تومان</b>\n"
        f"🛒 تعداد سفارش: {orders_count}\n"
        f"💸 مجموع خرید: {fmt(orders_total)} تومان\n"
        f"⛔ وضعیت: {'مسدود' if u['is_banned'] else 'فعال'}\n"
        f"📅 عضویت: {u['joined_at'][:16]}",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_add_") and c.from_user.id == ADMIN_ID)
def cb_ap_add(call):
    uid = int(call.data[7:])
    bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_add_wallet", target_uid=uid)
    bot.send_message(call.message.chat.id, f"💰 مبلغ شارژ (تومان) برای کاربر <code>{uid}</code> را وارد کنید:")

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_sub_") and c.from_user.id == ADMIN_ID)
def cb_ap_sub(call):
    uid = int(call.data[7:])
    bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_sub_wallet", target_uid=uid)
    bot.send_message(call.message.chat.id, f"➖ مبلغ کسر (تومان) از کیف پول کاربر <code>{uid}</code> را وارد کنید:")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") in ("adm_add_wallet", "adm_sub_wallet"))
def adm_modify_wallet(msg):
    try:
        amount = int(msg.text.strip().replace(",", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ مبلغ معتبر وارد کنید.")
    state  = get_state(ADMIN_ID)
    uid    = state["target_uid"]
    action = state["step"]
    if action == "adm_add_wallet":
        add_wallet(uid, amount)
        new_bal = get_wallet(uid)
        bot.send_message(uid,
            f"✅ <b>کیف پول شما شارژ شد!</b>\n\n"
            f"💰 مبلغ: <b>{fmt(amount)} تومان</b>\n"
            f"💎 موجودی جدید: <b>{fmt(new_bal)} تومان</b>"
        )
        bot.send_message(msg.chat.id, f"✅ {fmt(amount)} تومان به کیف پول {uid} اضافه شد. موجودی جدید: {fmt(new_bal)}")
    else:
        cur_bal = get_wallet(uid)
        deduct  = min(amount, cur_bal)
        deduct_wallet(uid, deduct)
        new_bal = get_wallet(uid)
        bot.send_message(msg.chat.id, f"➖ {fmt(deduct)} تومان از کیف پول {uid} کسر شد. موجودی جدید: {fmt(new_bal)}")
    clear_state(ADMIN_ID)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_ban_") and c.from_user.id == ADMIN_ID)
def cb_ap_ban(call):
    uid = int(call.data[7:])
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        u          = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
        new_status = 0 if u["is_banned"] else 1
        conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (new_status, uid))
        conn.commit()
    label = "مسدود" if new_status else "فعال"
    bot.send_message(call.message.chat.id, f"✅ وضعیت کاربر {uid} به <b>{label}</b> تغییر یافت.")
    if new_status:
        bot.send_message(uid, "⛔ حساب شما توسط ادمین مسدود شده است.")

@bot.callback_query_handler(func=lambda c: c.data == "ap_pending" and c.from_user.id == ADMIN_ID)
def cb_ap_pending(call):
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT r.*,u.username,u.full_name FROM receipts r JOIN users u ON r.user_id=u.user_id WHERE r.status='pending' ORDER BY r.created_at DESC LIMIT 10"
        ).fetchall()
    if not rows:
        return bot.send_message(call.message.chat.id, "✅ هیچ رسید معلقی وجود ندارد.")
    for r in rows:
        uname = r["username"] or r["full_name"] or str(r["user_id"])
        bot.send_message(
            call.message.chat.id,
            f"📥 رسید #{r['id']}\n👤 @{uname}\n📅 {r['created_at'][:16]}\nنوع: {r['receipt_type']}"
        )

# ─────────────────────────────────────────────
#  FALLBACK
# ─────────────────────────────────────────────
@bot.message_handler(func=lambda m: True)
def fallback(msg):
    u = get_user(msg.from_user.id)
    if u and u["is_banned"]:
        return
    # If admin is mid-flow, don't interrupt
    state = get_state(msg.from_user.id)
    if state.get("step"):
        return
    send_main_menu(msg.chat.id, msg.from_user.id, "برای شروع از منوی زیر استفاده کنید:")

# ─────────────────────────────────────────────
#  FLASK (Railway health check)
# ─────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"})

@flask_app.route("/")
def index():
    return "🤖 ViraNet Bot is running!", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print(f"🚀 ViraNet Bot starting — port {PORT} — admin {ADMIN_ID}")
    threading.Thread(target=run_flask, daemon=True).start()
    print("✅ Flask started")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
