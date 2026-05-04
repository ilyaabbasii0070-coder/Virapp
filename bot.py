import os
import sqlite3
import random
import string
import threading
import time
from datetime import datetime

import telebot
from telebot import types
from flask import Flask, jsonify

# ─────────────────────────────────────────────
#  ENV
# ─────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))
SECRET_KEY = os.environ.get("SECRET_KEY", "viranet_secret")
PORT       = int(os.environ.get("PORT", 8080))

SUPPORT_USERNAME = "@ViraNet0"
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
            id          INTEGER PRIMARY KEY,
            user_id     INTEGER UNIQUE NOT NULL,
            username    TEXT,
            full_name   TEXT,
            wallet      INTEGER DEFAULT 0,
            referral_code TEXT UNIQUE,
            referred_by INTEGER,
            is_banned   INTEGER DEFAULT 0,
            joined_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            plan_key    TEXT NOT NULL,
            quantity    INTEGER NOT NULL,
            total_price INTEGER NOT NULL,
            payment_method TEXT,
            status      TEXT DEFAULT 'pending',
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS order_services (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            service_name TEXT NOT NULL,
            config_text TEXT,
            plan_key    TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS receipts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            order_id    INTEGER,
            wallet_amount INTEGER,
            receipt_type TEXT NOT NULL,
            file_id     TEXT,
            status      TEXT DEFAULT 'pending',
            admin_msg_id INTEGER,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS wallet_requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            amount      INTEGER NOT NULL,
            status      TEXT DEFAULT 'pending',
            admin_msg_id INTEGER,
            created_at  TEXT DEFAULT (datetime('now'))
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
            full_name = (tg_user.first_name or "") + (" " + tg_user.last_name if tg_user.last_name else "")
            conn.execute(
                "INSERT INTO users(user_id,username,full_name,referral_code,referred_by) VALUES(?,?,?,?,?)",
                (tg_user.id, tg_user.username, full_name, ref_code, referred_by)
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

def fmt_price(p):
    return f"{p:,}"

def random_service_name():
    adjectives = ["Swift", "Storm", "Nova", "Volt", "Blaze", "Echo", "Apex", "Core", "Flux", "Zen"]
    nouns      = ["Link", "Node", "Wave", "Star", "Gate", "Net", "Byte", "Cloud", "Edge", "Hub"]
    return f"{random.choice(adjectives)}{random.choice(nouns)}{random.randint(10, 99)}"

# ─────────────────────────────────────────────
#  STATE MACHINE
# ─────────────────────────────────────────────
user_states = {}   # user_id -> dict

def set_state(uid, **kwargs):
    if uid not in user_states:
        user_states[uid] = {}
    user_states[uid].update(kwargs)

def get_state(uid):
    return user_states.get(uid, {})

def clear_state(uid):
    user_states.pop(uid, None)

# ─────────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ── Main Menu ──────────────────────────────
def main_menu_kb(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("🛒 فروشگاه"),
        types.KeyboardButton("💰 کیف پول"),
        types.KeyboardButton("📦 سرویس‌های من"),
        types.KeyboardButton("👥 دعوت دوستان"),
        types.KeyboardButton("🆘 پشتیبانی"),
    )
    if user_id == ADMIN_ID:
        kb.add(types.KeyboardButton("⚙️ پنل ادمین"))
    return kb

def send_main_menu(chat_id, user_id, text=None):
    bot.send_message(
        chat_id,
        text or (
            "🏠 <b>منوی اصلی</b>\n\n"
            "برای ادامه یکی از گزینه‌های زیر را انتخاب کنید:"
        ),
        reply_markup=main_menu_kb(user_id)
    )

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
            ref_user = conn.execute("SELECT user_id FROM users WHERE referral_code=?", (ref_code,)).fetchone()
            if ref_user and ref_user["user_id"] != msg.from_user.id:
                referred_by = ref_user["user_id"]

    ensure_user(msg.from_user, referred_by)
    clear_state(msg.from_user.id)

    welcome = (
        "✨ <b>به ویرا نت خوش آمدید!</b>\n\n"
        "💎 <b>سیستم حرفه‌ای مدیریت سرویس‌ها</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🌐 با استفاده از این ربات می‌توانید سرویس‌های اینترنتی پرسرعت و باکیفیت ما را خریداری کنید.\n\n"
        "⚡ <b>ویژگی‌های ما:</b>\n"
        "  • سرعت بالا و پایداری کامل\n"
        "  • پشتیبانی ۲۴ ساعته\n"
        "  • فعال‌سازی فوری\n"
        "  • قیمت‌های رقابتی\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 از منوی زیر گزینه مورد نظر را انتخاب کنید:"
    )
    bot.send_message(msg.chat.id, welcome, reply_markup=main_menu_kb(msg.from_user.id))

# ─────────────────────────────────────────────
#  🛒 SHOP
# ─────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "🛒 فروشگاه")
def shop_menu(msg):
    u = get_user(msg.from_user.id)
    if u and u["is_banned"]:
        return bot.send_message(msg.chat.id, "⛔ حساب شما مسدود شده است.")
    ensure_user(msg.from_user)
    clear_state(msg.from_user.id)
    set_state(msg.from_user.id, step="shop_plan")

    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, plan in PLANS.items():
        kb.add(types.InlineKeyboardButton(plan["label"], callback_data=f"plan_{key}"))

    bot.send_message(
        msg.chat.id,
        "🛒 <b>فروشگاه ویرا نت</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎯 لطفاً پلن مورد نظر خود را انتخاب کنید:\n\n"
        "تمامی سرویس‌ها شامل:\n"
        "  ✅ فعال‌سازی فوری\n"
        "  ✅ سرعت نامحدود\n"
        "  ✅ پشتیبانی کامل\n\n"
        "━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("plan_"))
def cb_plan(call):
    plan_key = call.data[5:]
    if plan_key not in PLANS:
        return bot.answer_callback_query(call.id, "پلن نامعتبر است")

    plan = PLANS[plan_key]
    set_state(call.from_user.id, step="shop_quantity", plan_key=plan_key)
    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        f"✅ <b>پلن انتخاب شده:</b> {plan['label']}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔢 <b>چند سرویس می‌خواهید؟</b>\n\n"
        "لطفاً تعداد سرویس مورد نیاز خود را وارد کنید.\n"
        "به ازای هر سرویس اضافه، همان قیمت پلن انتخابی اعمال می‌شود.\n\n"
        f"💰 قیمت هر عدد: <b>{fmt_price(plan['price'])} تومان</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
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

    state   = get_state(msg.from_user.id)
    plan    = PLANS[state["plan_key"]]
    total   = plan["price"] * qty
    set_state(msg.from_user.id, step="shop_name", quantity=qty, total_price=total, names=[], name_index=0)

    _ask_name_for_index(msg.chat.id, msg.from_user.id, 0, qty, state["plan_key"], total)

def _ask_name_for_index(chat_id, user_id, index, qty, plan_key, total):
    plan = PLANS[plan_key]
    kb   = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎲 اسم رندم", callback_data=f"name_random_{index}"),
        types.InlineKeyboardButton("✍️ اسم دلخواه", callback_data=f"name_custom_{index}"),
    )
    bot.send_message(
        chat_id,
        f"🏷 <b>نام‌گذاری سرویس {index + 1} از {qty}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 پلن: <b>{plan['label']}</b>\n"
        f"💰 مبلغ کل: <b>{fmt_price(total)} تومان</b>\n\n"
        "چه نامی برای این سرویس انتخاب می‌کنید؟\n\n"
        "  🎲 <b>اسم رندم</b> — سیستم یک نام منحصربه‌فرد برای شما انتخاب می‌کند\n"
        "  ✍️ <b>اسم دلخواه</b> — نام دلخواه خود را وارد کنید\n\n"
        "━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("name_random_") or c.data.startswith("name_custom_"))
def cb_name(call):
    state = get_state(call.from_user.id)
    if state.get("step") != "shop_name":
        return bot.answer_callback_query(call.id, "مرحله نادرست")

    parts = call.data.split("_")
    action = parts[1]  # random or custom
    index  = int(parts[2])
    bot.answer_callback_query(call.id)

    if action == "random":
        name = random_service_name()
        names = state.get("names", [])
        names.append(name)
        qty = state["quantity"]
        set_state(call.from_user.id, names=names, name_index=index + 1)

        bot.send_message(call.message.chat.id, f"✅ نام رندم ثبت شد: <b>{name}</b>")

        if index + 1 < qty:
            _ask_name_for_index(call.message.chat.id, call.from_user.id, index + 1, qty, state["plan_key"], state["total_price"])
        else:
            _ask_payment(call.message.chat.id, call.from_user.id)

    else:  # custom
        set_state(call.from_user.id, step="shop_name_input", name_index=index)
        bot.send_message(
            call.message.chat.id,
            f"✍️ <b>ارسال نام دلخواه — سرویس {index + 1}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🖊 لطفاً نام دلخواه خود را برای این سرویس ارسال کنید.\n\n"
            "این نام برای شناسایی سرویس در پنل شما استفاده می‌شود.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "👇 نام را همین‌جا تایپ و ارسال کنید:"
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
        _ask_name_for_index(msg.chat.id, msg.from_user.id, index + 1, qty, state["plan_key"], state["total_price"])
    else:
        _ask_payment(msg.chat.id, msg.from_user.id)

def _ask_payment(chat_id, user_id):
    state  = get_state(user_id)
    plan   = PLANS[state["plan_key"]]
    total  = state["total_price"]
    wallet = get_wallet(user_id)
    set_state(user_id, step="shop_payment")

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(f"💰 پرداخت از کیف پول (موجودی: {fmt_price(wallet)} تومان)", callback_data="pay_wallet"),
        types.InlineKeyboardButton("💳 پرداخت کارت به کارت", callback_data="pay_card"),
    )

    names_text = "\n".join([f"  {i+1}. {n}" for i, n in enumerate(state["names"])])

    bot.send_message(
        chat_id,
        f"💳 <b>مرحله پرداخت</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 <b>پلن:</b> {plan['label']}\n"
        f"🔢 <b>تعداد:</b> {state['quantity']} سرویس\n"
        f"🏷 <b>نام‌ها:</b>\n{names_text}\n\n"
        f"💰 <b>مبلغ قابل پرداخت:</b> {fmt_price(total)} تومان\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "روش پرداخت خود را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data in ("pay_wallet", "pay_card"))
def cb_payment(call):
    state  = get_state(call.from_user.id)
    if state.get("step") != "shop_payment":
        return bot.answer_callback_query(call.id, "مرحله نادرست")

    bot.answer_callback_query(call.id)
    total  = state["total_price"]
    wallet = get_wallet(call.from_user.id)

    if call.data == "pay_wallet":
        if wallet < total:
            shortage = total - wallet
            return bot.send_message(
                call.message.chat.id,
                f"❌ <b>موجودی ناکافی</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💰 موجودی فعلی شما: <b>{fmt_price(wallet)} تومان</b>\n"
                f"💳 مبلغ مورد نیاز: <b>{fmt_price(total)} تومان</b>\n"
                f"⚠️ کمبود: <b>{fmt_price(shortage)} تومان</b>\n\n"
                "برای شارژ کیف پول از بخش 💰 <b>کیف پول</b> در منوی اصلی اقدام کنید.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━"
            )
        # Deduct and create order
        deduct_wallet(call.from_user.id, total)
        _create_order_and_notify(call.from_user.id, call.message.chat.id, state, "wallet")

    else:  # card
        set_state(call.from_user.id, step="shop_receipt_wait")
        bot.send_message(
            call.message.chat.id,
            f"💳 <b>پرداخت کارت به کارت</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 <b>مبلغ پرداختی:</b> {fmt_price(total)} تومان\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🏦 <b>مشخصات حساب جهت واریز:</b>\n\n"
            f"  💳 شماره کارت:\n  <code>{CARD_NUMBER}</code>\n\n"
            f"  👤 به نام: <b>{CARD_OWNER}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📌 <b>مراحل پرداخت:</b>\n"
            f"۱. مبلغ <b>{fmt_price(total)} تومان</b> را به شماره کارت بالا واریز کنید\n"
            "۲. تصویر رسید یا فیش واریزی را ذخیره کنید\n"
            "۳. رسید خود را دقیقاً در همین چت ارسال کنید\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📥 <b>ارسال رسید:</b>\n"
            "پس از انجام واریز، تصویر رسید بانکی خود را در این گفتگو ارسال کنید.\n"
            "کارشناسان ما در کوتاه‌ترین زمان ممکن رسید شما را بررسی و سرویس را فعال خواهند کرد.\n\n"
            "⏳ زمان بررسی: معمولاً کمتر از ۳۰ دقیقه\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "👇 رسید واریزی خود را ارسال کنید:"
        )

def _create_order_and_notify(user_id, chat_id, state, payment_method):
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
        # Notify admin directly for wallet payments
        u       = get_user(user_id)
        uname   = u["username"] or u["full_name"] or str(user_id)
        names_t = "\n".join([f"  {i+1}. {n}" for i, n in enumerate(names)])
        kb      = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ تایید و ارسال کانفیگ", callback_data=f"adm_approve_{order_id}"),
            types.InlineKeyboardButton("❌ رد سفارش", callback_data=f"adm_reject_order_{order_id}"),
        )
        adm_msg = bot.send_message(
            ADMIN_ID,
            f"🛒 <b>سفارش جدید (کیف پول)</b>\n\n"
            f"👤 کاربر: @{uname} — <code>{user_id}</code>\n"
            f"📦 پلن: <b>{plan['label']}</b>\n"
            f"🔢 تعداد: {qty}\n"
            f"🏷 نام‌ها:\n{names_t}\n"
            f"💰 مبلغ: {fmt_price(total)} تومان\n"
            f"💳 روش: کیف پول",
            reply_markup=kb
        )
        with get_db() as conn:
            conn.execute(
                "INSERT INTO receipts(user_id,order_id,receipt_type,status,admin_msg_id) VALUES(?,?,?,?,?)",
                (user_id, order_id, "purchase_wallet", "pending", adm_msg.message_id)
            )
            conn.commit()

        bot.send_message(
            chat_id,
            "✅ <b>سفارش شما با موفقیت ثبت شد!</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 مبلغ {fmt_price(total)} تومان از کیف پول شما کسر شد.\n"
            "📋 سفارش شما در صف بررسی قرار گرفت.\n\n"
            "⏳ پس از تایید ادمین، کانفیگ‌های شما ارسال خواهد شد.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        clear_state(user_id)

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
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        svc_rows = conn.execute("SELECT * FROM order_services WHERE order_id=?", (order_id,)).fetchall()

    plan   = PLANS[order["plan_key"]]
    names  = [r["service_name"] for r in svc_rows]
    names_t = "\n".join([f"  {i+1}. {n}" for i, n in enumerate(names)])

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ تایید", callback_data=f"adm_approve_{order_id}"),
        types.InlineKeyboardButton("❌ رد", callback_data=f"adm_reject_order_{order_id}"),
    )

    adm_msg = bot.send_photo(
        ADMIN_ID,
        file_id,
        caption=(
            f"📥 <b>رسید جدید — خرید سرویس</b>\n\n"
            f"👤 کاربر: @{uname} — <code>{msg.from_user.id}</code>\n"
            f"📦 پلن: <b>{plan['label']}</b>\n"
            f"🔢 تعداد: {order['quantity']}\n"
            f"🏷 نام‌ها:\n{names_t}\n"
            f"💰 مبلغ: {fmt_price(order['total_price'])} تومان\n"
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
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ رسید واریزی شما با موفقیت به تیم پشتیبانی ارسال شد.\n\n"
        "⏳ <b>در حال بررسی توسط کارشناسان ما...</b>\n\n"
        "پس از تایید، کانفیگ‌های شما به صورت خودکار در همین چت ارسال خواهد شد.\n\n"
        "📌 زمان بررسی معمولاً کمتر از ۳۰ دقیقه است.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"در صورت هرگونه سوال به پشتیبانی مراجعه کنید: {SUPPORT_USERNAME}"
    )

# ─────────────────────────────────────────────
#  ADMIN APPROVE / REJECT ORDER
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_approve_"))
def cb_admin_approve(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "دسترسی ندارید")
    order_id = int(call.data.split("_")[2])
    bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_send_config", order_id=order_id)
    bot.send_message(
        call.message.chat.id,
        f"✅ <b>تایید سفارش #{order_id}</b>\n\n"
        "لطفاً کانفیگ‌ها را ارسال کنید.\n\n"
        "اگر تعداد سرویس بیش از ۱ است، هر کانفیگ را در یک پیام جداگانه ارسال کنید.\n"
        "پس از ارسال تمام کانفیگ‌ها دستور /done را بفرستید."
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_reject_order_"))
def cb_admin_reject_order(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "دسترسی ندارید")
    order_id = int(call.data.split("_")[3])
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        conn.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
        conn.commit()
    bot.send_message(
        order["user_id"],
        "❌ <b>رسید شما رد شد</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "متأسفانه رسید ارسالی شما مورد تایید قرار نگرفت.\n\n"
        "🔍 دلایل احتمالی:\n"
        "  • رسید نامعتبر یا غیرخوانا\n"
        "  • مغایرت مبلغ واریزی\n"
        "  • تصویر رسید مخدوش\n\n"
        "برای پیگیری و اطلاعات بیشتر با پشتیبانی در تماس باشید:\n"
        f"📞 {SUPPORT_USERNAME}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )
    bot.send_message(call.message.chat.id, f"❌ سفارش #{order_id} رد شد.")

# Admin sending configs
pending_configs = {}  # admin_id -> {order_id, configs list}

@bot.message_handler(
    func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_send_config"
)
def adm_send_config(msg):
    if msg.text and msg.text.strip() == "/done":
        state    = get_state(ADMIN_ID)
        order_id = state["order_id"]
        configs  = state.get("configs", [])
        _deliver_configs(order_id, configs)
        clear_state(ADMIN_ID)
        return bot.send_message(msg.chat.id, f"✅ {len(configs)} کانفیگ با موفقیت ارسال شد.")

    config_text = msg.text or msg.caption or ""
    config_text  = config_text.strip()
    if not config_text:
        return bot.send_message(msg.chat.id, "⚠️ لطفاً متن کانفیگ را ارسال کنید.")

    state    = get_state(ADMIN_ID)
    order_id = state["order_id"]
    configs  = state.get("configs", [])
    configs.append(config_text)
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
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏷 <b>نام سرویس:</b> {svc['service_name']}\n"
            f"📦 <b>پلن:</b> {plan['gb']}GB — {plan['days']} روز\n"
            f"💰 <b>حجم:</b> {plan['gb']} گیگابایت\n"
            f"📅 <b>مدت اعتبار:</b> {plan['days']} روز\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔐 <b>کانفیگ اتصال:</b>\n\n"
            f"<code>{cfg}</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📌 <b>راهنمای استفاده:</b>\n"
            "کد بالا را کپی کرده و در اپلیکیشن مورد نظر ایمپورت کنید.\n\n"
            f"در صورت نیاز به راهنمایی: {SUPPORT_USERNAME}\n\n"
            "از خرید شما سپاسگزاریم! 🙏"
        )

    with get_db() as conn:
        conn.execute("UPDATE orders SET status='delivered' WHERE id=?", (order_id,))
        conn.commit()

# ─────────────────────────────────────────────
#  📦 MY SERVICES
# ─────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "📦 سرویس‌های من")
def my_services(msg):
    ensure_user(msg.from_user)
    with get_db() as conn:
        services = conn.execute(
            "SELECT * FROM order_services WHERE user_id=? AND config_text IS NOT NULL ORDER BY id DESC",
            (msg.from_user.id,)
        ).fetchall()

    if not services:
        return bot.send_message(
            msg.chat.id,
            "📦 <b>سرویس‌های من</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "شما هنوز سرویس فعالی ندارید.\n\n"
            "برای خرید سرویس از بخش 🛒 <b>فروشگاه</b> اقدام کنید.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )

    kb = types.InlineKeyboardMarkup(row_width=1)
    for svc in services:
        plan = PLANS.get(svc["plan_key"], {})
        label = f"📦 {svc['service_name']} — {plan.get('gb','?')}GB"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"viewsvc_{svc['id']}"))

    bot.send_message(
        msg.chat.id,
        "📦 <b>سرویس‌های من</b>\n\n"
        f"شما <b>{len(services)}</b> سرویس فعال دارید.\n\n"
        "برای مشاهده جزئیات روی هر سرویس کلیک کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("viewsvc_"))
def cb_view_service(call):
    svc_id = int(call.data[8:])
    with get_db() as conn:
        svc = conn.execute("SELECT * FROM order_services WHERE id=? AND user_id=?", (svc_id, call.from_user.id)).fetchone()
    if not svc:
        return bot.answer_callback_query(call.id, "سرویس یافت نشد")
    plan = PLANS.get(svc["plan_key"], {})
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"📦 <b>جزئیات سرویس</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏷 <b>نام:</b> {svc['service_name']}\n"
        f"📊 <b>حجم:</b> {plan.get('gb', '?')} گیگابایت\n"
        f"📅 <b>مدت:</b> {plan.get('days', '?')} روز\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔐 <b>کانفیگ:</b>\n\n"
        f"<code>{svc['config_text']}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

# ─────────────────────────────────────────────
#  💰 WALLET
# ─────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "💰 کیف پول")
def wallet_menu(msg):
    ensure_user(msg.from_user)
    clear_state(msg.from_user.id)
    wallet = get_wallet(msg.from_user.id)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("💳 شارژ کیف پول", callback_data="wallet_charge"),
        types.InlineKeyboardButton("🔙 بازگشت", callback_data="wallet_back"),
    )
    bot.send_message(
        msg.chat.id,
        "💰 <b>کیف پول</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💎 <b>موجودی فعلی:</b> {fmt_price(wallet)} تومان\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "برای شارژ کیف پول دکمه زیر را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "wallet_back")
def cb_wallet_back(call):
    bot.answer_callback_query(call.id)
    send_main_menu(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "wallet_charge")
def cb_wallet_charge(call):
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, step="wallet_amount")
    bot.send_message(
        call.message.chat.id,
        "💳 <b>شارژ کیف پول</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💰 لطفاً مبلغ مورد نظر برای شارژ را به تومان وارد کنید.\n\n"
        "📌 حداقل مبلغ شارژ: <b>۵۰,۰۰۰ تومان</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 مبلغ را وارد کنید (مثال: 100000):"
    )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "wallet_amount")
def wallet_amount(msg):
    try:
        amount = int(msg.text.strip().replace(",", ""))
        if amount < 50000:
            raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ لطفاً یک مبلغ معتبر (حداقل ۵۰,۰۰۰ تومان) وارد کنید.")

    set_state(msg.from_user.id, step="wallet_receipt", wallet_amount=amount)
    bot.send_message(
        msg.chat.id,
        f"💳 <b>شارژ کیف پول — مرحله پرداخت</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 <b>مبلغ شارژ:</b> {fmt_price(amount)} تومان\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🏦 <b>مشخصات حساب جهت واریز:</b>\n\n"
        f"  💳 شماره کارت:\n  <code>{CARD_NUMBER}</code>\n\n"
        f"  👤 به نام: <b>{CARD_OWNER}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"۱. مبلغ <b>{fmt_price(amount)} تومان</b> را واریز کنید\n"
        "۲. تصویر رسید واریزی را در همین چت ارسال کنید\n\n"
        "⏳ پس از تایید ادمین، موجودی کیف پول شما شارژ خواهد شد.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 تصویر رسید را ارسال کنید:"
    )

@bot.message_handler(
    content_types=["photo"],
    func=lambda m: get_state(m.from_user.id).get("step") == "wallet_receipt"
)
def wallet_receipt(msg):
    state  = get_state(msg.from_user.id)
    amount = state["wallet_amount"]
    file_id = msg.photo[-1].file_id
    u       = get_user(msg.from_user.id)
    uname   = u["username"] or u["full_name"] or str(msg.from_user.id)

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO wallet_requests(user_id,amount) VALUES(?,?)",
            (msg.from_user.id, amount)
        )
        req_id = cur.lastrowid
        conn.commit()

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ تایید شارژ", callback_data=f"adm_wallet_ok_{req_id}_{msg.from_user.id}_{amount}"),
        types.InlineKeyboardButton("❌ رد", callback_data=f"adm_wallet_rej_{req_id}_{msg.from_user.id}"),
    )

    adm_msg = bot.send_photo(
        ADMIN_ID,
        file_id,
        caption=(
            f"💰 <b>درخواست شارژ کیف پول</b>\n\n"
            f"👤 کاربر: @{uname} — <code>{msg.from_user.id}</code>\n"
            f"💰 مبلغ: <b>{fmt_price(amount)} تومان</b>"
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
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ رسید شما به تیم پشتیبانی ارسال شد.\n"
        "⏳ <b>منتظر تایید ادمین باشید...</b>\n\n"
        "پس از تایید، موجودی کیف پول شما به‌روزرسانی خواهد شد.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_wallet_ok_"))
def cb_wallet_approve(call):
    if call.from_user.id != ADMIN_ID:
        return
    parts   = call.data.split("_")
    req_id  = int(parts[3])
    user_id = int(parts[4])
    amount  = int(parts[5])
    bot.answer_callback_query(call.id)

    add_wallet(user_id, amount)
    with get_db() as conn:
        conn.execute("UPDATE wallet_requests SET status='approved' WHERE id=?", (req_id,))
        conn.commit()

    new_bal = get_wallet(user_id)
    bot.send_message(
        user_id,
        f"✅ <b>کیف پول شما شارژ شد!</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 <b>مبلغ شارژ:</b> {fmt_price(amount)} تومان\n"
        f"💎 <b>موجودی جدید:</b> {fmt_price(new_bal)} تومان\n\n"
        "از شارژ کیف پول شما سپاسگزاریم! 🙏\n\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )
    bot.send_message(call.message.chat.id, f"✅ کیف پول کاربر {user_id} — {fmt_price(amount)} تومان شارژ شد.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_wallet_rej_"))
def cb_wallet_reject(call):
    if call.from_user.id != ADMIN_ID:
        return
    parts   = call.data.split("_")
    req_id  = int(parts[3])
    user_id = int(parts[4])
    bot.answer_callback_query(call.id)

    with get_db() as conn:
        conn.execute("UPDATE wallet_requests SET status='rejected' WHERE id=?", (req_id,))
        conn.commit()

    bot.send_message(
        user_id,
        "❌ <b>درخواست شارژ رد شد</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "متأسفانه رسید ارسالی شما مورد تایید قرار نگرفت.\n\n"
        "برای پیگیری با پشتیبانی تماس بگیرید:\n"
        f"📞 {SUPPORT_USERNAME}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )
    bot.send_message(call.message.chat.id, f"❌ درخواست شارژ کاربر {user_id} رد شد.")

# ─────────────────────────────────────────────
#  👥 REFERRAL
# ─────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "👥 دعوت دوستان")
def referral_menu(msg):
    ensure_user(msg.from_user)
    u = get_user(msg.from_user.id)
    bot_info = bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{u['referral_code']}"

    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) as c FROM users WHERE referred_by=?", (msg.from_user.id,)).fetchone()["c"]

    bot.send_message(
        msg.chat.id,
        "👥 <b>دعوت دوستان</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 به ازای هر دوستی که با لینک اختصاصی شما ربات را استارت کند،\n"
        f"<b>{fmt_price(REFERRAL_BONUS)} تومان</b> به کیف پول شما اضافه می‌شود!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 تعداد دعوت‌های موفق: <b>{count}</b>\n"
        f"💰 درآمد کسب شده: <b>{fmt_price(count * REFERRAL_BONUS)} تومان</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔗 <b>لینک اختصاصی شما:</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        "این لینک را کپی کرده و برای دوستان خود ارسال کنید.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

# ─────────────────────────────────────────────
#  🆘 SUPPORT
# ─────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "🆘 پشتیبانی")
def support_menu(msg):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💬 تماس با پشتیبانی", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}"))
    bot.send_message(
        msg.chat.id,
        "🆘 <b>پشتیبانی ویرا نت</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "تیم پشتیبانی ما آماده پاسخگویی به سوالات شماست.\n\n"
        "⏰ ساعات پاسخگویی: ۸ صبح تا ۱۲ شب\n\n"
        "برای ارتباط مستقیم دکمه زیر را بزنید:\n\n"
        "━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=kb
    )

# ─────────────────────────────────────────────
#  ⚙️ ADMIN PANEL
# ─────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "⚙️ پنل ادمین" and m.from_user.id == ADMIN_ID)
def admin_panel(msg):
    clear_state(ADMIN_ID)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("👤 جستجوی کاربر", callback_data="adm_search"),
        types.InlineKeyboardButton("📊 آمار کلی", callback_data="adm_stats"),
        types.InlineKeyboardButton("📋 سفارشات معلق", callback_data="adm_pending"),
    )
    bot.send_message(
        msg.chat.id,
        "⚙️ <b>پنل ادمین ویرا نت</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "از گزینه‌های زیر استفاده کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "adm_stats" and c.from_user.id == ADMIN_ID)
def cb_adm_stats(call):
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        users_count   = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        pending_count = conn.execute("SELECT COUNT(*) as c FROM receipts WHERE status='pending'").fetchone()["c"]
        total_sales   = conn.execute("SELECT SUM(total_price) as s FROM orders WHERE status='delivered'").fetchone()["s"] or 0

    bot.send_message(
        call.message.chat.id,
        "📊 <b>آمار کلی</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 تعداد کاربران: <b>{users_count}</b>\n"
        f"📥 رسیدهای در انتظار: <b>{pending_count}</b>\n"
        f"💰 فروش کل: <b>{fmt_price(total_sales)} تومان</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

@bot.callback_query_handler(func=lambda c: c.data == "adm_search" and c.from_user.id == ADMIN_ID)
def cb_adm_search(call):
    bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_search")
    bot.send_message(call.message.chat.id, "👤 آیدی عددی یا یوزرنیم کاربر را ارسال کنید:")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_search")
def adm_search_user(msg):
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
    uid = u["user_id"]
    kb  = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("💰 شارژ کیف پول", callback_data=f"adm_charge_{uid}"),
        types.InlineKeyboardButton("⛔ بن کردن" if not u["is_banned"] else "✅ رفع بن", callback_data=f"adm_ban_{uid}"),
    )

    with get_db() as conn:
        orders_count = conn.execute("SELECT COUNT(*) as c FROM orders WHERE user_id=?", (uid,)).fetchone()["c"]

    bot.send_message(
        msg.chat.id,
        f"👤 <b>اطلاعات کاربر</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 آیدی: <code>{uid}</code>\n"
        f"👤 نام: {u['full_name']}\n"
        f"📛 یوزرنیم: @{u['username'] or '---'}\n"
        f"💰 موجودی: {fmt_price(u['wallet'])} تومان\n"
        f"🛒 تعداد سفارش: {orders_count}\n"
        f"🔗 کد رفرال: {u['referral_code']}\n"
        f"⛔ وضعیت: {'مسدود' if u['is_banned'] else 'فعال'}\n"
        f"📅 عضویت: {u['joined_at']}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_charge_") and c.from_user.id == ADMIN_ID)
def cb_adm_charge(call):
    uid = int(call.data[11:])
    bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_manual_charge", target_user=uid)
    bot.send_message(call.message.chat.id, f"💰 مبلغ شارژ (تومان) برای کاربر {uid} را وارد کنید:")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_manual_charge")
def adm_manual_charge(msg):
    try:
        amount = int(msg.text.strip().replace(",", ""))
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ مبلغ معتبر وارد کنید.")

    state  = get_state(ADMIN_ID)
    uid    = state["target_user"]
    add_wallet(uid, amount)
    new_bal = get_wallet(uid)
    clear_state(ADMIN_ID)

    bot.send_message(uid,
        f"✅ <b>کیف پول شما شارژ شد!</b>\n\n"
        f"💰 مبلغ: <b>{fmt_price(amount)} تومان</b>\n"
        f"💎 موجودی جدید: <b>{fmt_price(new_bal)} تومان</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )
    bot.send_message(msg.chat.id, f"✅ کیف پول کاربر {uid} — {fmt_price(amount)} تومان شارژ شد. موجودی جدید: {fmt_price(new_bal)}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_ban_") and c.from_user.id == ADMIN_ID)
def cb_adm_ban(call):
    uid = int(call.data[8:])
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        u = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
        new_status = 0 if u["is_banned"] else 1
        conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (new_status, uid))
        conn.commit()
    status_text = "مسدود" if new_status else "فعال"
    bot.send_message(call.message.chat.id, f"✅ وضعیت کاربر {uid} به <b>{status_text}</b> تغییر یافت.")
    if new_status:
        bot.send_message(uid, "⛔ حساب شما توسط ادمین مسدود شده است.")

@bot.callback_query_handler(func=lambda c: c.data == "adm_pending" and c.from_user.id == ADMIN_ID)
def cb_adm_pending(call):
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        pending = conn.execute(
            "SELECT r.*,u.username,u.full_name FROM receipts r JOIN users u ON r.user_id=u.user_id WHERE r.status='pending' ORDER BY r.created_at DESC LIMIT 10"
        ).fetchall()

    if not pending:
        return bot.send_message(call.message.chat.id, "✅ هیچ رسید معلقی وجود ندارد.")

    for r in pending:
        uname = r["username"] or r["full_name"] or str(r["user_id"])
        bot.send_message(
            call.message.chat.id,
            f"📥 رسید #{r['id']} — @{uname} — {r['created_at'][:16]}\n"
            f"نوع: {r['receipt_type']} | سفارش: {r['order_id']}"
        )

# ─────────────────────────────────────────────
#  FLASK HEALTH CHECK (for Railway)
# ─────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": "ViraNet"})

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
    print(f"🚀 ViraNet Bot starting on port {PORT}...")

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("✅ Flask health server started")

    print("✅ Bot polling started")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
