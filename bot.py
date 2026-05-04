import os
import sqlite3
import random
import string
import threading
import io
import html as html_lib
from datetime import datetime

import requests
import telebot
from telebot import types
from flask import Flask, jsonify

try:
    import qrcode
    QR_OK = True
except ImportError:
    QR_OK = False

def make_qr_bytes(text: str) -> io.BytesIO | None:
    if not QR_OK:
        return None
    try:
        img = qrcode.make(text)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"QR build error: {e}")
        return None

# ─────────────────────────────────────────────
#  ENV
# ─────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "8030883585"))
SECRET_KEY     = os.environ.get("SECRET_KEY", "viranet_secret")
PORT           = int(os.environ.get("PORT", 8080))
TRX_WALLET     = os.environ.get("TRX_WALLET", "YOUR_TRX_WALLET_ADDRESS")
USD_TO_TOMAN   = int(os.environ.get("USD_TO_TOMAN", "90000"))

SUPPORT_USERNAME = "ViraNet0"
REFERRAL_BONUS   = 5000

# ── مقادیر پویا (از DB بارگذاری می‌شن) ──────────
CARD_NUMBER = "123456789456123"
CARD_OWNER  = "حسین حسینی"

# ── وضعیت ربات ────────────────────────────────
BOT_ONLINE = True

# ── محصولات پویا (از DB بارگذاری می‌شن) ─────────
PLANS: dict = {}

# ── tracker برای thread‌های آپدیت قیمت کریپتو ──
crypto_stop_events: dict = {}

# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────
DB_PATH = "viranet.db"

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    global CARD_NUMBER, CARD_OWNER
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
            sub_link     TEXT,
            plan_key     TEXT NOT NULL,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS receipts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            order_id      INTEGER,
            wallet_amount INTEGER,
            receipt_type  TEXT NOT NULL,
            file_id       TEXT,
            status        TEXT DEFAULT 'pending',
            admin_msg_id  INTEGER,
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS wallet_requests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            amount       INTEGER NOT NULL,
            status       TEXT DEFAULT 'pending',
            admin_msg_id INTEGER,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS products (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_key TEXT UNIQUE NOT NULL,
            label    TEXT NOT NULL,
            gb       INTEGER NOT NULL,
            days     INTEGER NOT NULL,
            price    INTEGER NOT NULL,
            active   INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)

        # Migration: add sub_link column if missing
        try:
            conn.execute("ALTER TABLE order_services ADD COLUMN sub_link TEXT")
            conn.commit()
        except Exception:
            pass

        # اگه جدول محصولات خالیه، محصولات پیش‌فرض رو اضافه کن
        cnt = conn.execute("SELECT COUNT(*) as c FROM products").fetchone()["c"]
        if cnt == 0:
            default_products = [
                ("1gb",  "⚡ 1GB  —  30 روز  —  400,000 تومان",  1, 30, 400_000),
                ("2gb",  "🚀 2GB  —  30 روز  —  780,000 تومان",  2, 30, 780_000),
                ("3gb",  "🔥 3GB  —  30 روز  —  1,100,000 تومان", 3, 30, 1_100_000),
                ("5gb",  "💥 5GB  —  30 روز  —  1,800,000 تومان", 5, 30, 1_800_000),
            ]
            conn.executemany(
                "INSERT OR IGNORE INTO products(plan_key,label,gb,days,price) VALUES(?,?,?,?,?)",
                default_products
            )
            conn.commit()

        # تنظیمات کارت
        r_card = conn.execute("SELECT value FROM settings WHERE key='card_number'").fetchone()
        r_owner = conn.execute("SELECT value FROM settings WHERE key='card_owner'").fetchone()
        if not r_card:
            conn.execute("INSERT INTO settings(key,value) VALUES('card_number',?)", (CARD_NUMBER,))
        if not r_owner:
            conn.execute("INSERT INTO settings(key,value) VALUES('card_owner',?)", (CARD_OWNER,))
        conn.commit()

        # بارگذاری تنظیمات
        r_card  = conn.execute("SELECT value FROM settings WHERE key='card_number'").fetchone()
        r_owner = conn.execute("SELECT value FROM settings WHERE key='card_owner'").fetchone()
        if r_card:
            CARD_NUMBER = r_card["value"]
        if r_owner:
            CARD_OWNER = r_owner["value"]

    reload_plans()
    print("✅ Database ready")

def reload_plans():
    global PLANS
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY price").fetchall()
    PLANS = {}
    for r in rows:
        PLANS[r["plan_key"]] = {
            "label": r["label"],
            "gb":    r["gb"],
            "days":  r["days"],
            "price": r["price"],
        }

def reload_card_settings():
    global CARD_NUMBER, CARD_OWNER
    with get_db() as conn:
        r_card  = conn.execute("SELECT value FROM settings WHERE key='card_number'").fetchone()
        r_owner = conn.execute("SELECT value FROM settings WHERE key='card_owner'").fetchone()
    if r_card:
        CARD_NUMBER = r_card["value"]
    if r_owner:
        CARD_OWNER = r_owner["value"]

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def get_user(user_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def ensure_user(tg_user, referred_by=None):
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (tg_user.id,)).fetchone():
            rc   = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
            full = ((tg_user.first_name or "") + " " + (tg_user.last_name or "")).strip()
            conn.execute(
                "INSERT INTO users(user_id,username,full_name,referral_code,referred_by) VALUES(?,?,?,?,?)",
                (tg_user.id, tg_user.username, full, rc, referred_by)
            )
            if referred_by:
                conn.execute("UPDATE users SET wallet=wallet+? WHERE user_id=?", (REFERRAL_BONUS, referred_by))
            conn.commit()
            return True  # کاربر جدید
    return False  # کاربر قدیمی

def get_wallet(uid):
    u = get_user(uid)
    return u["wallet"] if u else 0

def add_wallet(uid, amount):
    with get_db() as conn:
        conn.execute("UPDATE users SET wallet=wallet+? WHERE user_id=?", (amount, uid))
        conn.commit()

def deduct_wallet(uid, amount):
    with get_db() as conn:
        conn.execute("UPDATE users SET wallet=wallet-? WHERE user_id=?", (amount, uid))
        conn.commit()

def fmt(p):
    return f"{p:,}"

def random_name():
    adj  = ["Swift", "Storm", "Nova", "Volt", "Blaze", "Echo", "Apex", "Core", "Flux", "Zen"]
    noun = ["Link", "Node", "Wave", "Star", "Gate", "Net", "Byte", "Cloud", "Edge", "Hub"]
    return f"{random.choice(adj)}{random.choice(noun)}{random.randint(10, 99)}"

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")

# ── قیمت لحظه‌ای TRX ─────────────────────────
def get_trx_price_usd() -> float | None:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=tron&vs_currencies=usd",
            timeout=6
        )
        return float(r.json()["tron"]["usd"])
    except Exception as e:
        print(f"[TRX price error] {e}")
        return None

def toman_to_trx(toman_amount: int) -> float | None:
    price = get_trx_price_usd()
    if not price:
        return None
    return round(toman_amount / (USD_TO_TOMAN * price), 2)

def crypto_payment_text(total_toman: int, trx_amount: float | None) -> str:
    trx_line = (
        f"<b>💎 معادل ترون (TRX): {trx_amount} TRX</b>"
        if trx_amount is not None
        else "⚠️ خطا در دریافت قیمت — لطفاً صبر کنید..."
    )
    return (
        "🔐 <b>پرداخت با کریپتو — ترون (TRX)</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💵 <b>مبلغ سفارش شما:</b> {fmt(total_toman)} تومان\n\n"
        f"{trx_line}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 <b>آدرس کیف پول TRX ما:</b>\n\n"
        f"<code>{TRX_WALLET}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 <b>مراحل پرداخت:</b>\n\n"
        "   ۱️⃣  مقدار دقیق TRX بالا را به آدرس فوق ارسال کنید\n"
        "   ۲️⃣  پس از ارسال، هش تراکنش (Transaction ID) خود را کپی کنید\n"
        "   ۳️⃣  هش تراکنش را همین‌جا در چت برای ما ارسال کنید\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ <b>نکات مهم:</b>\n\n"
        "   🔹 فقط از شبکه <b>TRON (TRC-20)</b> استفاده کنید\n"
        "   🔹 مقدار TRX را دقیقاً طبق نرخ لحظه ارسال کنید\n"
        "   🔹 قیمت TRX هر ۱۰ ثانیه آپدیت می‌شود\n"
        "   🔹 پس از ارسال هش، سفارش شما بررسی و فعال می‌شود\n"
        "   🔹 زمان بررسی: کمتر از ۳۰ دقیقه\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🕐 <b>آخرین به‌روزرسانی قیمت:</b> " + datetime.now().strftime("%H:%M:%S") + "\n\n"
        "👇 <b>هش تراکنش خود را ارسال کنید یا منتظر آپدیت قیمت بمانید:</b>"
    )

def crypto_payment_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔄 بروزرسانی قیمت", callback_data="crypto_refresh"))
    kb.add(types.InlineKeyboardButton("🔙 بازگشت به روش پرداخت", callback_data="crypto_back"))
    return kb

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
    # توقف thread آپدیت قیمت
    if uid in crypto_stop_events:
        crypto_stop_events[uid].set()
        del crypto_stop_events[uid]

# ─────────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ── پیام خاموشی ربات ──────────────────────────
OFFLINE_MSG = (
    "🔧 <b>ربات موقتاً در دسترس نیست</b>\n\n"
    "⚙️ در حال انجام بروزرسانی و بهبود سرویس هستیم.\n\n"
    "🕐 این فرآیند ممکن است چند دقیقه طول بکشد.\n\n"
    "📢 به محض اتمام عملیات، ربات مجدداً فعال خواهد شد.\n\n"
    "🙏 از صبر و شکیبایی شما سپاسگزاریم.\n\n"
    f"❓ برای اطلاعات بیشتر با پشتیبانی تماس بگیرید: @{SUPPORT_USERNAME}"
)

def is_offline_for(user_id: int) -> bool:
    return not BOT_ONLINE and user_id != ADMIN_ID

# ── Main Menu ──────────────────────────────────
def main_menu_kb(user_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🛒 فروشگاه", callback_data="menu_shop"),
        types.InlineKeyboardButton("💰 کیف پول", callback_data="menu_wallet"),
    )
    kb.add(
        types.InlineKeyboardButton("📦 سرویس‌های من", callback_data="menu_services"),
        types.InlineKeyboardButton("👥 دعوت دوستان", callback_data="menu_referral"),
    )
    kb.add(types.InlineKeyboardButton("🆘 پشتیبانی", url=f"https://t.me/{SUPPORT_USERNAME}"))
    if user_id == ADMIN_ID:
        kb.add(types.InlineKeyboardButton("⚙️ پنل ادمین", callback_data="menu_admin"))
        kb.add(
            types.InlineKeyboardButton("🔴 خاموش کردن ربات", callback_data="admin_bot_off"),
            types.InlineKeyboardButton("🟢 روشن کردن ربات", callback_data="admin_bot_on"),
        )
    return kb

def send_main_menu(chat_id, user_id, text=None):
    bot.send_message(
        chat_id,
        text or "🏠 <b>منوی اصلی</b>\n\n✨ گزینه مورد نظر را انتخاب کنید:",
        reply_markup=main_menu_kb(user_id)
    )

# ─────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    if is_offline_for(msg.from_user.id):
        return bot.send_message(msg.chat.id, OFFLINE_MSG)

    args = msg.text.split()
    referred_by = None
    if len(args) > 1 and args[1].startswith("ref_"):
        with get_db() as conn:
            r = conn.execute("SELECT user_id FROM users WHERE referral_code=?", (args[1][4:],)).fetchone()
            if r and r["user_id"] != msg.from_user.id:
                referred_by = r["user_id"]

    is_new = ensure_user(msg.from_user, referred_by)
    clear_state(msg.from_user.id)

    # اطلاع‌رسانی به ادمین برای کاربر جدید
    if is_new:
        try:
            u = get_user(msg.from_user.id)
            bot.send_message(
                ADMIN_ID,
                f"👤 <b>کاربر جدید ثبت شد!</b>\n\n"
                f"🆔 آیدی: <code>{msg.from_user.id}</code>\n"
                f"📛 نام: {u['full_name'] or '---'}\n"
                f"👤 یوزرنیم: @{u['username'] or '---'}\n"
                f"🕐 زمان: {now_str()}"
            )
        except Exception:
            pass

    bot.send_message(
        msg.chat.id,
        "✨ <b>به ویرا نت خوش آمدید!</b> 🎉\n\n"
        "💎 <b>سیستم حرفه‌ای مدیریت سرویس‌ها</b>\n\n"
        "🌐 با این ربات می‌توانید سرویس‌های اینترنتی پرسرعت و باکیفیت ما را خریداری کنید.\n\n"
        "⚡ <b>چرا ویرا نت؟</b>\n"
        "  🔹 سرعت بالا و پایداری کامل\n"
        "  🔹 پشتیبانی ۲۴ ساعته\n"
        "  🔹 فعال‌سازی فوری پس از پرداخت\n"
        "  🔹 قیمت‌های رقابتی و منصفانه\n\n"
        "👇 از منوی زیر گزینه مورد نظر را انتخاب کنید:",
        reply_markup=main_menu_kb(msg.from_user.id)
    )

# ─────────────────────────────────────────────
#  MENU CALLBACKS
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "menu_shop")
def cb_menu_shop(call):
    if is_offline_for(call.from_user.id):
        return bot.answer_callback_query(call.id, "⚠️ ربات موقتاً خاموش است.", show_alert=True)
    u = get_user(call.from_user.id)
    if u and u["is_banned"]:
        return bot.answer_callback_query(call.id, "⛔ حساب شما مسدود است.", show_alert=True)
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user)
    clear_state(call.from_user.id)
    _show_shop(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_wallet")
def cb_menu_wallet(call):
    if is_offline_for(call.from_user.id):
        return bot.answer_callback_query(call.id, "⚠️ ربات موقتاً خاموش است.", show_alert=True)
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user)
    clear_state(call.from_user.id)
    _show_wallet(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_services")
def cb_menu_services(call):
    if is_offline_for(call.from_user.id):
        return bot.answer_callback_query(call.id, "⚠️ ربات موقتاً خاموش است.", show_alert=True)
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user)
    _show_my_services(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_referral")
def cb_menu_referral(call):
    if is_offline_for(call.from_user.id):
        return bot.answer_callback_query(call.id, "⚠️ ربات موقتاً خاموش است.", show_alert=True)
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

# ── روشن/خاموش ربات ───────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "admin_bot_off")
def cb_bot_off(call):
    global BOT_ONLINE
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    bot.answer_callback_query(call.id)
    BOT_ONLINE = False
    bot.send_message(
        call.message.chat.id,
        "🔴 <b>ربات خاموش شد!</b>\n\n"
        "از این لحظه، هیچ کاربری نمی‌تواند از ربات استفاده کند.\n"
        "برای روشن کردن ربات، دکمه «🟢 روشن کردن ربات» را بزنید."
    )

@bot.callback_query_handler(func=lambda c: c.data == "admin_bot_on")
def cb_bot_on(call):
    global BOT_ONLINE
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    bot.answer_callback_query(call.id)
    BOT_ONLINE = True
    bot.send_message(
        call.message.chat.id,
        "🟢 <b>ربات روشن شد!</b>\n\n"
        "ربات مجدداً فعال است و کاربران می‌توانند از آن استفاده کنند."
    )

# ─────────────────────────────────────────────
#  🛒 SHOP
# ─────────────────────────────────────────────
def _show_shop(chat_id, user_id):
    reload_plans()
    if not PLANS:
        return bot.send_message(chat_id, "❌ هیچ محصولی موجود نیست. لطفاً بعداً مراجعه کنید.")
    set_state(user_id, step="shop_plan")
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, plan in PLANS.items():
        kb.add(types.InlineKeyboardButton(plan["label"], callback_data=f"plan_{key}"))
    kb.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))
    bot.send_message(
        chat_id,
        "🛒 <b>فروشگاه ویرا نت</b> 🌟\n\n"
        "🎯 پلن مورد نظر خود را انتخاب کنید:\n\n"
        "✅ فعال‌سازی فوری\n"
        "✅ سرعت نامحدود\n"
        "✅ پشتیبانی ۲۴ ساعته\n\n"
        "👇 پلن را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("plan_"))
def cb_plan(call):
    if is_offline_for(call.from_user.id):
        return bot.answer_callback_query(call.id, "⚠️ ربات موقتاً خاموش است.", show_alert=True)
    plan_key = call.data[5:]
    reload_plans()
    if plan_key not in PLANS:
        return bot.answer_callback_query(call.id, "پلن نامعتبر")
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, step="shop_quantity", plan_key=plan_key)
    plan = PLANS[plan_key]
    bot.send_message(
        call.message.chat.id,
        f"✅ <b>پلن انتخاب شده:</b>\n{plan['label']}\n\n"
        "🔢 <b>تعداد سرویس</b>\n\n"
        f"💰 قیمت هر عدد: <b>{fmt(plan['price'])} تومان</b>\n\n"
        "📌 چند سرویس می‌خواهید؟\n"
        "👇 عدد را ارسال کنید (مثال: ۱ یا ۳):"
    )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "shop_quantity")
def shop_quantity(msg):
    if is_offline_for(msg.from_user.id):
        return bot.send_message(msg.chat.id, OFFLINE_MSG)
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
    kb   = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎲 اسم رندم", callback_data=f"name_r_{index}"),
        types.InlineKeyboardButton("✍️ اسم دلخواه", callback_data=f"name_c_{index}"),
    )
    bot.send_message(
        chat_id,
        f"🏷️ <b>نام‌گذاری سرویس {index + 1} از {qty}</b>\n\n"
        f"📦 پلن: <b>{plan['gb']}GB — {plan['days']} روز</b>\n"
        f"💰 مبلغ کل: <b>{fmt(total)} تومان</b>\n\n"
        "روش نام‌گذاری را انتخاب کنید:\n\n"
        "  🎲 <b>اسم رندم</b> — سیستم یک نام منحصربه‌فرد انتخاب می‌کند\n"
        "  ✍️ <b>اسم دلخواه</b> — نام دلخواه خود را وارد کنید",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("name_r_") or c.data.startswith("name_c_"))
def cb_name(call):
    if is_offline_for(call.from_user.id):
        return bot.answer_callback_query(call.id, "⚠️ ربات موقتاً خاموش است.", show_alert=True)
    state = get_state(call.from_user.id)
    if state.get("step") not in ("shop_name", "shop_name_input"):
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
        set_state(call.from_user.id, step="shop_name", names=names, name_index=index + 1)
        bot.send_message(call.message.chat.id, f"✅ نام رندم ثبت شد: <b>{name}</b> 🎲")
        if index + 1 < qty:
            _ask_name(call.message.chat.id, call.from_user.id, index + 1, qty, state["plan_key"], state["total_price"])
        else:
            _ask_payment(call.message.chat.id, call.from_user.id)
    else:
        set_state(call.from_user.id, step="shop_name_input", name_index=index)
        bot.send_message(
            call.message.chat.id,
            f"✍️ <b>ارسال نام دلخواه — سرویس {index + 1}</b>\n\n"
            "نام دلخواه خود را ارسال کنید:\n"
            "👇 همین‌جا تایپ کنید:"
        )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "shop_name_input")
def shop_name_input(msg):
    if is_offline_for(msg.from_user.id):
        return bot.send_message(msg.chat.id, OFFLINE_MSG)
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
    state      = get_state(user_id)
    plan       = PLANS[state["plan_key"]]
    total      = state["total_price"]
    wallet     = get_wallet(user_id)
    names_text = "\n".join([f"  {i+1}. 🏷️ {n}" for i, n in enumerate(state["names"])])
    set_state(user_id, step="shop_payment")

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(f"💰 پرداخت از کیف پول  (موجودی: {fmt(wallet)} تومان)", callback_data="pay_wallet"),
        types.InlineKeyboardButton("💳 پرداخت کارت به کارت", callback_data="pay_card"),
        types.InlineKeyboardButton("🌐 پرداخت با کریپتو", callback_data="pay_crypto"),
    )
    bot.send_message(
        chat_id,
        f"💳 <b>مرحله پرداخت</b> 🧾\n\n"
        f"📦 <b>پلن:</b> {plan['label']}\n"
        f"🔢 <b>تعداد:</b> {state['quantity']} سرویس\n"
        f"🏷️ <b>نام‌ها:</b>\n{names_text}\n\n"
        f"💰 <b>مبلغ قابل پرداخت:</b> {fmt(total)} تومان\n\n"
        "👇 روش پرداخت را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data in ("pay_wallet", "pay_card", "pay_crypto"))
def cb_payment(call):
    if is_offline_for(call.from_user.id):
        return bot.answer_callback_query(call.id, "⚠️ ربات موقتاً خاموش است.", show_alert=True)
    state = get_state(call.from_user.id)
    if state.get("step") != "shop_payment":
        return bot.answer_callback_query(call.id)
    bot.answer_callback_query(call.id)
    total  = state["total_price"]
    wallet = get_wallet(call.from_user.id)

    if call.data == "pay_wallet":
        if wallet < total:
            return bot.send_message(
                call.message.chat.id,
                f"❌ <b>موجودی کیف پول کافی نیست!</b>\n\n"
                f"💰 موجودی فعلی: <b>{fmt(wallet)} تومان</b>\n"
                f"💳 مبلغ مورد نیاز: <b>{fmt(total)} تومان</b>\n"
                f"⚠️ کمبود: <b>{fmt(total - wallet)} تومان</b>\n\n"
                "🔄 برای شارژ کیف پول از منوی اصلی اقدام کنید."
            )
        deduct_wallet(call.from_user.id, total)
        _create_order_and_notify(call.from_user.id, call.message.chat.id, state, "wallet")

    elif call.data == "pay_card":
        _create_order_and_notify(call.from_user.id, call.message.chat.id, state, "card")

    else:  # pay_crypto
        _start_crypto_payment(call.from_user.id, call.message.chat.id, state)

# ── پرداخت کریپتو ────────────────────────────
def _start_crypto_payment(user_id, chat_id, state):
    set_state(user_id, step="shop_crypto_currency")
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔷 ترون (TRX)", callback_data="crypto_trx"),
        types.InlineKeyboardButton("🔙 بازگشت", callback_data="crypto_back"),
    )
    bot.send_message(
        chat_id,
        "🌐 <b>پرداخت با ارز دیجیتال</b>\n\n"
        "ارز دیجیتال مورد نظر خود را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "crypto_trx")
def cb_crypto_trx(call):
    if is_offline_for(call.from_user.id):
        return bot.answer_callback_query(call.id, "⚠️ ربات موقتاً خاموش است.", show_alert=True)
    state = get_state(call.from_user.id)
    if state.get("step") not in ("shop_crypto_currency", "shop_crypto_wait"):
        return bot.answer_callback_query(call.id)
    bot.answer_callback_query(call.id, "⏳ در حال دریافت قیمت لحظه‌ای...")

    total     = state["total_price"]
    trx_amt   = toman_to_trx(total)
    msg_text  = crypto_payment_text(total, trx_amt)

    sent = bot.send_message(call.message.chat.id, msg_text, reply_markup=crypto_payment_kb())
    set_state(call.from_user.id, step="shop_crypto_wait", crypto_msg_id=sent.message_id, crypto_chat_id=call.message.chat.id)

    # شروع thread آپدیت خودکار
    _start_crypto_updater(call.from_user.id, call.message.chat.id, sent.message_id, total)

def _start_crypto_updater(user_id, chat_id, message_id, total_toman):
    if user_id in crypto_stop_events:
        crypto_stop_events[user_id].set()

    stop_ev = threading.Event()
    crypto_stop_events[user_id] = stop_ev

    def updater():
        while not stop_ev.wait(10):
            if get_state(user_id).get("step") != "shop_crypto_wait":
                break
            trx_amt = toman_to_trx(total_toman)
            try:
                bot.edit_message_text(
                    crypto_payment_text(total_toman, trx_amt),
                    chat_id,
                    message_id,
                    reply_markup=crypto_payment_kb(),
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"[crypto updater] edit error: {e}")
                break

    threading.Thread(target=updater, daemon=True).start()

@bot.callback_query_handler(func=lambda c: c.data == "crypto_refresh")
def cb_crypto_refresh(call):
    if is_offline_for(call.from_user.id):
        return bot.answer_callback_query(call.id, "⚠️ ربات موقتاً خاموش است.", show_alert=True)
    state = get_state(call.from_user.id)
    if state.get("step") != "shop_crypto_wait":
        return bot.answer_callback_query(call.id)
    bot.answer_callback_query(call.id, "🔄 در حال بروزرسانی قیمت...")
    total   = state["total_price"]
    trx_amt = toman_to_trx(total)
    try:
        bot.edit_message_text(
            crypto_payment_text(total, trx_amt),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=crypto_payment_kb(),
            parse_mode="HTML"
        )
    except Exception:
        pass

@bot.callback_query_handler(func=lambda c: c.data == "crypto_back")
def cb_crypto_back(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    if uid in crypto_stop_events:
        crypto_stop_events[uid].set()
        del crypto_stop_events[uid]
    state = get_state(uid)
    set_state(uid, step="shop_payment")
    _ask_payment(call.message.chat.id, uid)

# دریافت هش تراکنش کریپتو
@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "shop_crypto_wait")
def crypto_tx_hash(msg):
    if is_offline_for(msg.from_user.id):
        return bot.send_message(msg.chat.id, OFFLINE_MSG)
    tx_hash = (msg.text or "").strip()
    if len(tx_hash) < 20:
        return bot.send_message(msg.chat.id, "⚠️ هش تراکنش نامعتبر است. لطفاً هش صحیح را ارسال کنید.")

    state = get_state(msg.from_user.id)
    uid   = msg.from_user.id

    # توقف thread آپدیت
    if uid in crypto_stop_events:
        crypto_stop_events[uid].set()
        del crypto_stop_events[uid]

    _create_order_and_notify(uid, msg.chat.id, state, "crypto", tx_hash=tx_hash)

def _create_order_and_notify(user_id, chat_id, state, payment_method, tx_hash=None):
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

    u       = get_user(user_id)
    uname   = u["username"] or u["full_name"] or str(user_id)
    names_t = "\n".join([f"  {i+1}. {n}" for i, n in enumerate(names)])
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ تایید — ارسال کانفیگ", callback_data=f"adm_ok_{order_id}"),
        types.InlineKeyboardButton("❌ رد سفارش", callback_data=f"adm_rej_{order_id}"),
    )

    if payment_method == "wallet":
        bot.send_message(
            ADMIN_ID,
            f"🛒 <b>سفارش جدید — کیف پول</b>\n\n"
            f"👤 کاربر: @{uname}  |  <code>{user_id}</code>\n"
            f"🕐 زمان: {now_str()}\n\n"
            f"📦 پلن: <b>{plan['label']}</b>\n"
            f"🔢 تعداد: {qty} سرویس\n"
            f"🏷️ نام‌ها:\n{names_t}\n\n"
            f"💰 مبلغ: <b>{fmt(total)} تومان</b>\n"
            f"💳 روش: کیف پول",
            reply_markup=kb
        )
        clear_state(user_id)
        bot.send_message(
            chat_id,
            "✅ <b>سفارش شما با موفقیت ثبت شد!</b> 🎉\n\n"
            f"💰 مبلغ <b>{fmt(total)} تومان</b> از کیف پول شما کسر شد.\n"
            "📋 سفارش در صف بررسی قرار گرفت.\n\n"
            "⏳ پس از تایید ادمین، کانفیگ‌های شما ارسال خواهد شد.\n\n"
            f"❓ سوال؟ پشتیبانی: @{SUPPORT_USERNAME}"
        )

    elif payment_method == "crypto":
        trx_amt = toman_to_trx(total)
        bot.send_message(
            ADMIN_ID,
            f"🌐 <b>سفارش جدید — پرداخت کریپتو (TRX)</b>\n\n"
            f"👤 کاربر: @{uname}  |  <code>{user_id}</code>\n"
            f"🕐 زمان: {now_str()}\n\n"
            f"📦 پلن: <b>{plan['label']}</b>\n"
            f"🔢 تعداد: {qty} سرویس\n"
            f"🏷️ نام‌ها:\n{names_t}\n\n"
            f"💰 مبلغ: <b>{fmt(total)} تومان</b>\n"
            f"🔷 معادل TRX: <b>{trx_amt} TRX</b>\n"
            f"🔑 هش تراکنش: <code>{html_lib.escape(tx_hash or '')}</code>",
            reply_markup=kb
        )
        clear_state(user_id)
        bot.send_message(
            chat_id,
            "✅ <b>هش تراکنش شما دریافت شد!</b> 🔷\n\n"
            "🚀 در حال بررسی تراکنش کریپتو شما هستیم.\n\n"
            "⏳ پس از تایید، کانفیگ‌ها ارسال می‌شود.\n\n"
            "📌 زمان بررسی: کمتر از ۳۰ دقیقه\n\n"
            f"❓ سوال؟ @{SUPPORT_USERNAME}"
        )

    else:  # card
        set_state(user_id, step="shop_receipt_wait", order_id=order_id)
        bot.send_message(
            chat_id,
            f"💳 <b>پرداخت کارت به کارت</b> 🏦\n\n"
            f"💰 <b>مبلغ پرداختی:</b> {fmt(total)} تومان\n\n"
            "🏦 <b>مشخصات حساب جهت واریز:</b>\n\n"
            f"  💳 شماره کارت:\n  <code>{CARD_NUMBER}</code>\n\n"
            f"  👤 به نام: <b>{CARD_OWNER}</b>\n\n"
            "📌 <b>مراحل پرداخت:</b>\n"
            f"  ۱. مبلغ <b>{fmt(total)} تومان</b> را واریز کنید\n"
            "  ۲. تصویر رسید بانکی را ذخیره کنید\n"
            "  ۳. رسید را همین‌جا در چت ارسال کنید\n\n"
            "⏳ زمان بررسی: کمتر از ۳۰ دقیقه\n\n"
            "👇 تصویر رسید واریزی خود را ارسال کنید:"
        )

# ── Unified photo handler
@bot.message_handler(content_types=["photo"])
def handle_all_photos(msg):
    if is_offline_for(msg.from_user.id):
        return bot.send_message(msg.chat.id, OFFLINE_MSG)
    step = get_state(msg.from_user.id).get("step")
    if step == "shop_receipt_wait":
        _handle_shop_receipt(msg)
    elif step == "wallet_receipt":
        _handle_wallet_receipt(msg)

def _handle_shop_receipt(msg):
    state    = get_state(msg.from_user.id)
    order_id = state.get("order_id")
    if not order_id:
        return bot.send_message(msg.chat.id, "⚠️ خطا: سفارش یافت نشد. لطفاً دوباره از فروشگاه اقدام کنید.")

    file_id = msg.photo[-1].file_id
    u       = get_user(msg.from_user.id)
    uname   = u["username"] or u["full_name"] or str(msg.from_user.id)

    with get_db() as conn:
        order    = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        svc_rows = conn.execute("SELECT * FROM order_services WHERE order_id=?", (order_id,)).fetchall()

    plan    = PLANS.get(order["plan_key"], {"gb": "?", "days": "?"})
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
            f"👤 کاربر: @{uname}  |  <code>{msg.from_user.id}</code>\n"
            f"🕐 زمان: {now_str()}\n\n"
            f"📦 پلن: <b>{plan['label']}</b>\n"
            f"🔢 تعداد: {order['quantity']} سرویس\n"
            f"🏷️ نام‌ها:\n{names_t}\n\n"
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
        "📥 <b>رسید شما دریافت شد!</b> ✅\n\n"
        "🚀 رسید واریزی شما به تیم پشتیبانی ارسال شد.\n\n"
        "⏳ <b>در حال بررسی...</b>\n\n"
        "پس از تایید، کانفیگ‌ها در همین چت ارسال می‌شود.\n\n"
        "📌 زمان بررسی: معمولاً کمتر از ۳۰ دقیقه\n\n"
        f"❓ سوال؟ @{SUPPORT_USERNAME}"
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
    set_state(ADMIN_ID, step="adm_config", order_id=order_id, configs=[], subs=[])
    bot.send_message(
        call.message.chat.id,
        f"✅ <b>تایید سفارش #{order_id}</b>\n\n"
        "📋 لطفاً کانفیگ اول را ارسال کنید:\n\n"
        "(اگر تعداد بیش از ۱ است، پس از هر کانفیگ یک ساب‌لینک هم ارسال می‌کنید)\n"
        "بعد از همه کانفیگ‌ها دستور /done بفرستید."
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
        "❌ <b>رسید شما رد شد</b> 😔\n\n"
        "متأسفانه رسید ارسالی شما تایید نشد.\n\n"
        "🔍 دلایل احتمالی:\n"
        "  ❗ رسید نامعتبر یا غیرخوانا\n"
        "  ❗ مغایرت مبلغ واریزی\n"
        "  ❗ تصویر رسید مخدوش\n\n"
        "برای پیگیری با پشتیبانی تماس بگیرید:\n"
        f"📞 @{SUPPORT_USERNAME}"
    )
    bot.send_message(call.message.chat.id, f"❌ سفارش #{order_id} رد شد و کاربر مطلع شد.")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_config")
def adm_receive_config(msg):
    if msg.text and msg.text.strip() == "/done":
        state    = get_state(ADMIN_ID)
        order_id = state["order_id"]
        configs  = state.get("configs", [])
        subs     = state.get("subs", [])
        if not configs:
            return bot.send_message(msg.chat.id, "⚠️ هیچ کانفیگی ثبت نشده. لطفاً حداقل یک کانفیگ ارسال کنید.")
        _deliver_configs(order_id, configs, subs)
        clear_state(ADMIN_ID)
        return bot.send_message(msg.chat.id, f"✅ {len(configs)} کانفیگ با موفقیت ارسال شد.")

    state   = get_state(ADMIN_ID)
    configs = state.get("configs", [])
    subs    = state.get("subs", [])
    text    = (msg.text or "").strip()

    if not text:
        return bot.send_message(msg.chat.id, "⚠️ متن خالی است. کانفیگ یا ساب‌لینک را ارسال کنید.")

    if len(configs) == len(subs):
        configs.append(text)
        set_state(ADMIN_ID, configs=configs)
        bot.send_message(msg.chat.id, f"✅ کانفیگ {len(configs)} ثبت شد.\n\n📡 حالا ساب‌لینک مربوط به این کانفیگ را ارسال کنید:")
    else:
        subs.append(text)
        set_state(ADMIN_ID, subs=subs)
        if len(configs) < state.get("order_qty", 99):
            bot.send_message(msg.chat.id, f"✅ ساب‌لینک {len(subs)} ثبت شد.\n\n📋 کانفیگ بعدی را ارسال کنید یا /done بفرستید:")
        else:
            bot.send_message(msg.chat.id, "✅ ساب‌لینک ثبت شد. برای ارسال /done بفرستید:")

def _deliver_configs(order_id, configs, subs):
    with get_db() as conn:
        order    = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        svc_rows = conn.execute("SELECT * FROM order_services WHERE order_id=? ORDER BY id", (order_id,)).fetchall()

    if not order or not svc_rows:
        print(f"[deliver] ERROR: order or svc_rows missing for order_id={order_id}")
        return

    plan    = PLANS.get(order["plan_key"], {"gb": "?", "days": "?"})
    user_id = order["user_id"]

    for i, svc in enumerate(svc_rows):
        cfg = configs[i] if i < len(configs) else "---"
        sub = subs[i]    if i < len(subs)    else ""

        try:
            with get_db() as conn:
                conn.execute("UPDATE order_services SET config_text=?, sub_link=? WHERE id=?",
                             (cfg, sub, svc["id"]))
                conn.commit()
        except Exception as e:
            print(f"[deliver] DB update ERROR svc_id={svc['id']}: {e}")
            continue

        activation_time = datetime.now().strftime("%Y/%m/%d — %H:%M")

        kb = types.InlineKeyboardMarkup(row_width=1)
        sub_is_url = sub.startswith("http://") or sub.startswith("https://")
        if sub_is_url:
            kb.add(types.InlineKeyboardButton("🔗 باز کردن ساب‌لینک", url=sub))
        kb.add(types.InlineKeyboardButton("✏️ تغییر نام سرویس", callback_data=f"rename_{svc['id']}"))

        safe_cfg = html_lib.escape(cfg)
        safe_sub = html_lib.escape(sub) if sub else ""

        full_text = (
            f"🎉 <b>سرویس شما با موفقیت فعال شد!</b> 🚀\n\n"
            f"🏷️ <b>نام سرویس:</b> {html_lib.escape(svc['service_name'])}\n"
            f"📊 <b>حجم:</b> {plan['gb']} گیگابایت\n"
            f"📅 <b>مدت اعتبار:</b> {plan['days']} روز\n"
            f"🕐 <b>زمان فعال‌سازی:</b> {activation_time}\n\n"
            f"🔐 <b>کانفیگ اتصال</b> (روی آن بزنید تا کپی شود):\n\n"
            f"<code>{safe_cfg}</code>\n\n"
        )
        if sub:
            full_text += f"🔗 <b>ساب‌لینک:</b>\n<code>{safe_sub}</code>\n\n"
        full_text += (
            "📌 <b>راهنمای استفاده:</b>\n"
            "  ۱. کانفیگ بالا را کپی کرده در اپ ایمپورت کنید\n"
            "  ۲. یا دکمه ساب‌لینک زیر را بزنید\n"
            "  ۳. یا QR کد پایین را با اپ اسکن کنید\n\n"
            f"🆘 پشتیبانی: @{SUPPORT_USERNAME}\n"
            "💙 از خرید شما سپاسگزاریم!"
        )

        try:
            bot.send_message(user_id, full_text, reply_markup=kb)
        except Exception as e:
            print(f"[deliver] send_message ERROR: {e}")

        qr_target = sub if sub else cfg
        qr_buf = make_qr_bytes(qr_target)
        if qr_buf:
            try:
                bot.send_photo(user_id, qr_buf,
                               caption=f"📷 QR کد سرویس <b>{html_lib.escape(svc['service_name'])}</b>\nبرای اتصال سریع اسکن کنید 📱")
            except Exception as e:
                print(f"[deliver] QR send ERROR: {e}")

    with get_db() as conn:
        conn.execute("UPDATE orders SET status='delivered' WHERE id=?", (order_id,))
        conn.commit()

# ── Rename service ─────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("rename_"))
def cb_rename(call):
    svc_id    = int(call.data[7:])
    caller_id = call.from_user.id
    with get_db() as conn:
        svc = conn.execute("SELECT * FROM order_services WHERE id=?", (svc_id,)).fetchone()
    if not svc or int(svc["user_id"]) != int(caller_id):
        return bot.answer_callback_query(call.id, "سرویس یافت نشد", show_alert=True)
    bot.answer_callback_query(call.id)
    set_state(caller_id, step="rename_service", svc_id=svc_id)
    bot.send_message(
        call.message.chat.id,
        f"✏️ <b>تغییر نام سرویس</b>\n\n"
        f"نام فعلی: <b>{html_lib.escape(svc['service_name'])}</b>\n\n"
        "👇 نام جدید را وارد کنید (حداکثر ۳۰ کاراکتر):"
    )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "rename_service")
def rename_service(msg):
    state  = get_state(msg.from_user.id)
    svc_id = state.get("svc_id")
    if not svc_id:
        clear_state(msg.from_user.id)
        return
    name = (msg.text or "").strip()[:30]
    if not name:
        return bot.send_message(msg.chat.id, "⚠️ نام نمی‌تواند خالی باشد.")
    with get_db() as conn:
        svc = conn.execute("SELECT user_id FROM order_services WHERE id=?", (svc_id,)).fetchone()
        if not svc or int(svc["user_id"]) != int(msg.from_user.id):
            clear_state(msg.from_user.id)
            return bot.send_message(msg.chat.id, "❌ دسترسی ندارید.")
        conn.execute("UPDATE order_services SET service_name=? WHERE id=?", (name, svc_id))
        conn.commit()
    clear_state(msg.from_user.id)
    bot.send_message(msg.chat.id, f"✅ نام سرویس به <b>{name}</b> تغییر یافت! ✨")

# ─────────────────────────────────────────────
#  📦 MY SERVICES
# ─────────────────────────────────────────────
def _show_my_services(chat_id, user_id):
    with get_db() as conn:
        all_svcs = conn.execute(
            "SELECT * FROM order_services WHERE config_text IS NOT NULL ORDER BY id DESC"
        ).fetchall()
    svcs = [s for s in all_svcs if int(s["user_id"]) == int(user_id)]

    if not svcs:
        return bot.send_message(
            chat_id,
            "📦 <b>سرویس‌های من</b>\n\n"
            "❌ شما هنوز سرویس فعالی ندارید.\n\n"
            "🛒 برای خرید از بخش فروشگاه اقدام کنید."
        )

    kb = types.InlineKeyboardMarkup(row_width=1)
    for svc in svcs:
        plan = PLANS.get(svc["plan_key"], {})
        kb.add(types.InlineKeyboardButton(
            f"📦 {svc['service_name']}  |  {plan.get('gb','?')}GB",
            callback_data=f"vs_{svc['id']}"
        ))
    kb.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))

    bot.send_message(
        chat_id,
        f"📦 <b>سرویس‌های من</b> ✨\n\n"
        f"🔢 شما <b>{len(svcs)}</b> سرویس فعال دارید.\n\n"
        "👇 برای مشاهده جزئیات روی سرویس کلیک کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("vs_"))
def cb_view_svc(call):
    svc_id = int(call.data[3:])
    with get_db() as conn:
        svc = conn.execute("SELECT * FROM order_services WHERE id=? AND user_id=?", (svc_id, call.from_user.id)).fetchone()
    if not svc:
        return bot.answer_callback_query(call.id, "سرویس یافت نشد", show_alert=True)
    plan = PLANS.get(svc["plan_key"], {})
    bot.answer_callback_query(call.id)

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✏️ تغییر نام سرویس", callback_data=f"rename_{svc['id']}"))

    text = (
        f"📦 <b>جزئیات سرویس</b>\n\n"
        f"🏷️ <b>نام:</b> {svc['service_name']}\n"
        f"📊 <b>حجم:</b> {plan.get('gb','?')} گیگابایت\n"
        f"📅 <b>مدت:</b> {plan.get('days','?')} روز\n\n"
        "🔐 <b>کانفیگ:</b>\n\n"
        f"<code>{svc['config_text']}</code>\n\n"
    )
    if svc["sub_link"]:
        text += f"🔗 <b>ساب‌لینک:</b>\n<code>{svc['sub_link']}</code>\n\n"

    bot.send_message(call.message.chat.id, text, reply_markup=kb)

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
        "💰 <b>کیف پول</b> 💎\n\n"
        f"✨ <b>موجودی فعلی:</b> {fmt(wallet)} تومان\n\n"
        "👇 برای شارژ دکمه زیر را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "wallet_charge")
def cb_wallet_charge(call):
    if is_offline_for(call.from_user.id):
        return bot.answer_callback_query(call.id, "⚠️ ربات موقتاً خاموش است.", show_alert=True)
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, step="wallet_amount")
    bot.send_message(
        call.message.chat.id,
        "💳 <b>شارژ کیف پول</b> 💰\n\n"
        "📌 حداقل مبلغ شارژ: <b>۵۰,۰۰۰ تومان</b>\n\n"
        "👇 مبلغ را به تومان وارد کنید (مثال: 100000):"
    )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "wallet_amount")
def wallet_amount(msg):
    if is_offline_for(msg.from_user.id):
        return bot.send_message(msg.chat.id, OFFLINE_MSG)
    try:
        amount = int(msg.text.strip().replace(",", "").replace("٬", ""))
        if amount < 50_000:
            raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ لطفاً یک مبلغ معتبر (حداقل ۵۰,۰۰۰ تومان) وارد کنید.")

    set_state(msg.from_user.id, step="wallet_receipt", wallet_amount=amount)
    bot.send_message(
        msg.chat.id,
        f"💳 <b>شارژ کیف پول — مرحله پرداخت</b> 🏦\n\n"
        f"💰 <b>مبلغ شارژ:</b> {fmt(amount)} تومان\n\n"
        "🏦 <b>مشخصات حساب جهت واریز:</b>\n\n"
        f"  💳 شماره کارت:\n  <code>{CARD_NUMBER}</code>\n\n"
        f"  👤 به نام: <b>{CARD_OWNER}</b>\n\n"
        f"  ۱. مبلغ <b>{fmt(amount)} تومان</b> را واریز کنید\n"
        "  ۲. تصویر رسید را در همین چت ارسال کنید\n\n"
        "⏳ پس از تایید ادمین، موجودی کیف پول شارژ می‌شود.\n\n"
        "👇 تصویر رسید را ارسال کنید:"
    )

def _handle_wallet_receipt(msg):
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
            f"👤 کاربر: @{uname}  |  <code>{msg.from_user.id}</code>\n"
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
        "📥 <b>رسید شما دریافت شد!</b> ✅\n\n"
        "🚀 رسید به تیم پشتیبانی ارسال شد.\n"
        "⏳ <b>منتظر تایید ادمین باشید...</b>\n\n"
        "پس از تایید، موجودی کیف پول به‌روزرسانی خواهد شد."
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
        f"✅ <b>کیف پول شما شارژ شد!</b> 🎉\n\n"
        f"💰 مبلغ شارژ: <b>{fmt(amount)} تومان</b>\n"
        f"💎 موجودی جدید: <b>{fmt(new_bal)} تومان</b>\n\n"
        "از شارژ کیف پول شما سپاسگزاریم! 🙏"
    )
    bot.send_message(call.message.chat.id, f"✅ {fmt(amount)} تومان به کیف پول {user_id} اضافه شد.")

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
        "❌ <b>درخواست شارژ رد شد</b> 😔\n\n"
        "رسید ارسالی تایید نشد.\n\n"
        f"📞 برای پیگیری: @{SUPPORT_USERNAME}"
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
        "👥 <b>دعوت دوستان</b> 🎁\n\n"
        f"💰 به ازای هر دعوت موفق: <b>{fmt(REFERRAL_BONUS)} تومان</b>\n\n"
        f"👤 دعوت‌های موفق: <b>{count}</b>\n"
        f"💸 درآمد کسب شده: <b>{fmt(count * REFERRAL_BONUS)} تومان</b>\n\n"
        "🔗 <b>لینک اختصاصی شما:</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        "📤 این لینک را برای دوستان ارسال کنید."
    )

# ─────────────────────────────────────────────
#  ⚙️ ADMIN PANEL
# ─────────────────────────────────────────────
def _show_admin_panel(chat_id):
    bot_status = "🟢 روشن" if BOT_ONLINE else "🔴 خاموش"
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("👥 لیست کاربران", callback_data="ap_users_0"),
        types.InlineKeyboardButton("🔍 جستجوی کاربر", callback_data="ap_search"),
        types.InlineKeyboardButton("📊 آمار کلی", callback_data="ap_stats"),
        types.InlineKeyboardButton("📋 رسیدهای معلق", callback_data="ap_pending"),
        types.InlineKeyboardButton("⚙️ تنظیمات", callback_data="ap_settings"),
    )
    bot.send_message(
        chat_id,
        f"⚙️ <b>پنل ادمین ویرا نت</b>\n\n"
        f"📡 وضعیت ربات: <b>{bot_status}</b>\n\n"
        "👇 گزینه مورد نظر را انتخاب کنید:",
        reply_markup=kb
    )

# ── تنظیمات ─────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "ap_settings" and c.from_user.id == ADMIN_ID)
def cb_ap_settings(call):
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("💳 تنظیمات اطلاعات کارت", callback_data="ap_card_settings"),
        types.InlineKeyboardButton("📦 تنظیمات محصولات", callback_data="ap_products"),
        types.InlineKeyboardButton("🔙 پنل ادمین", callback_data="menu_admin"),
    )
    bot.send_message(
        call.message.chat.id,
        "⚙️ <b>تنظیمات</b>\n\n"
        "یکی از گزینه‌های زیر را انتخاب کنید:",
        reply_markup=kb
    )

# ── تنظیمات کارت ────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "ap_card_settings" and c.from_user.id == ADMIN_ID)
def cb_ap_card_settings(call):
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("💳 تغییر شماره کارت", callback_data="ap_change_card"),
        types.InlineKeyboardButton("👤 تغییر اسم صاحب کارت", callback_data="ap_change_owner"),
        types.InlineKeyboardButton("🔙 تنظیمات", callback_data="ap_settings"),
    )
    bot.send_message(
        call.message.chat.id,
        f"💳 <b>تنظیمات اطلاعات کارت</b>\n\n"
        f"شماره کارت فعلی: <code>{CARD_NUMBER}</code>\n"
        f"نام صاحب کارت: <b>{CARD_OWNER}</b>\n\n"
        "👇 گزینه مورد نظر را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "ap_change_card" and c.from_user.id == ADMIN_ID)
def cb_ap_change_card(call):
    bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_change_card")
    bot.send_message(
        call.message.chat.id,
        "💳 <b>تغییر شماره کارت</b>\n\n"
        f"شماره کارت فعلی: <code>{CARD_NUMBER}</code>\n\n"
        "👇 شماره کارت جدید را وارد کنید:"
    )

@bot.callback_query_handler(func=lambda c: c.data == "ap_change_owner" and c.from_user.id == ADMIN_ID)
def cb_ap_change_owner(call):
    bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_change_owner")
    bot.send_message(
        call.message.chat.id,
        "👤 <b>تغییر نام صاحب کارت</b>\n\n"
        f"نام فعلی: <b>{CARD_OWNER}</b>\n\n"
        "👇 نام صاحب کارت جدید را وارد کنید:"
    )

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_change_card")
def adm_change_card(msg):
    global CARD_NUMBER
    new_card = (msg.text or "").strip().replace(" ", "").replace("-", "")
    if len(new_card) < 10 or not new_card.isdigit():
        return bot.send_message(msg.chat.id, "⚠️ شماره کارت نامعتبر است. فقط اعداد وارد کنید (حداقل ۱۰ رقم).")
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('card_number',?)", (new_card,))
        conn.commit()
    CARD_NUMBER = new_card
    clear_state(ADMIN_ID)
    bot.send_message(msg.chat.id, f"✅ شماره کارت با موفقیت تغییر یافت!\n\nشماره جدید: <code>{CARD_NUMBER}</code>")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_change_owner")
def adm_change_owner(msg):
    global CARD_OWNER
    new_owner = (msg.text or "").strip()
    if len(new_owner) < 3:
        return bot.send_message(msg.chat.id, "⚠️ نام نمی‌تواند کمتر از ۳ کاراکتر باشد.")
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('card_owner',?)", (new_owner,))
        conn.commit()
    CARD_OWNER = new_owner
    clear_state(ADMIN_ID)
    bot.send_message(msg.chat.id, f"✅ نام صاحب کارت با موفقیت تغییر یافت!\n\nنام جدید: <b>{CARD_OWNER}</b>")

# ── تنظیمات محصولات ──────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "ap_products" and c.from_user.id == ADMIN_ID)
def cb_ap_products(call):
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📋 لیست محصولات", callback_data="ap_product_list"),
        types.InlineKeyboardButton("➕ اضافه کردن محصول", callback_data="ap_product_add"),
        types.InlineKeyboardButton("🔙 تنظیمات", callback_data="ap_settings"),
    )
    bot.send_message(
        call.message.chat.id,
        "📦 <b>تنظیمات محصولات</b>\n\n"
        "👇 گزینه مورد نظر را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "ap_product_list" and c.from_user.id == ADMIN_ID)
def cb_ap_product_list(call):
    bot.answer_callback_query(call.id)
    reload_plans()
    with get_db() as conn:
        products = conn.execute("SELECT * FROM products ORDER BY price").fetchall()

    if not products:
        return bot.send_message(call.message.chat.id, "❌ هیچ محصولی ثبت نشده.")

    kb = types.InlineKeyboardMarkup(row_width=1)
    for p in products:
        status = "✅" if p["active"] else "❌"
        kb.add(types.InlineKeyboardButton(
            f"{status} {p['gb']}GB — {p['days']} روز — {fmt(p['price'])} تومان",
            callback_data=f"ap_prod_{p['id']}"
        ))
    kb.add(types.InlineKeyboardButton("🔙 تنظیمات محصولات", callback_data="ap_products"))

    bot.send_message(
        call.message.chat.id,
        "📋 <b>لیست محصولات</b>\n\n"
        "روی هر محصول بزنید تا قیمت آن را تغییر دهید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_prod_") and c.from_user.id == ADMIN_ID)
def cb_ap_prod_detail(call):
    bot.answer_callback_query(call.id)
    prod_id = int(call.data[8:])
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (prod_id,)).fetchone()
    if not p:
        return bot.send_message(call.message.chat.id, "❌ محصول یافت نشد.")

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("💰 تغییر قیمت", callback_data=f"ap_chprice_{prod_id}"),
        types.InlineKeyboardButton("🔙 لیست محصولات", callback_data="ap_product_list"),
    )
    bot.send_message(
        call.message.chat.id,
        f"📦 <b>جزئیات محصول</b>\n\n"
        f"🔑 کلید: <code>{p['plan_key']}</code>\n"
        f"📊 حجم: <b>{p['gb']} گیگابایت</b>\n"
        f"📅 مدت: <b>{p['days']} روز</b>\n"
        f"💰 قیمت: <b>{fmt(p['price'])} تومان</b>\n"
        f"📌 وضعیت: {'✅ فعال' if p['active'] else '❌ غیرفعال'}\n\n"
        "👇 عملیات مورد نظر را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_chprice_") and c.from_user.id == ADMIN_ID)
def cb_ap_change_price(call):
    bot.answer_callback_query(call.id)
    prod_id = int(call.data[11:])
    set_state(ADMIN_ID, step="adm_change_price", prod_id=prod_id)
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (prod_id,)).fetchone()
    bot.send_message(
        call.message.chat.id,
        f"💰 <b>تغییر قیمت محصول</b>\n\n"
        f"📦 محصول: <b>{p['gb']}GB — {p['days']} روز</b>\n"
        f"💰 قیمت فعلی: <b>{fmt(p['price'])} تومان</b>\n\n"
        "👇 قیمت جدید را به تومان وارد کنید:"
    )

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_change_price")
def adm_change_price(msg):
    try:
        new_price = int((msg.text or "").strip().replace(",", "").replace("٬", ""))
        if new_price <= 0:
            raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ قیمت معتبر وارد کنید (عدد مثبت).")

    prod_id = get_state(ADMIN_ID)["prod_id"]
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (prod_id,)).fetchone()
        new_label = f"{'⚡' if p['gb'] == 1 else '🚀' if p['gb'] == 2 else '🔥' if p['gb'] == 3 else '💥'} {p['gb']}GB  —  {p['days']} روز  —  {fmt(new_price)} تومان"
        conn.execute("UPDATE products SET price=?, label=? WHERE id=?", (new_price, new_label, prod_id))
        conn.commit()
    reload_plans()
    clear_state(ADMIN_ID)
    bot.send_message(
        msg.chat.id,
        f"✅ قیمت محصول با موفقیت تغییر یافت!\n\nقیمت جدید: <b>{fmt(new_price)} تومان</b>"
    )

# ── اضافه کردن محصول ─────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "ap_product_add" and c.from_user.id == ADMIN_ID)
def cb_ap_product_add(call):
    bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_add_product_gb")
    bot.send_message(
        call.message.chat.id,
        "➕ <b>اضافه کردن محصول جدید</b>\n\n"
        "📊 <b>حجم محصول را وارد کنید (گیگابایت):</b>\n\n"
        "مثال: ۵ یا 10"
    )

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_add_product_gb")
def adm_add_product_gb(msg):
    try:
        gb = int((msg.text or "").strip())
        if gb <= 0:
            raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ عدد معتبر وارد کنید (مثال: 5).")
    set_state(ADMIN_ID, step="adm_add_product_days", new_gb=gb)
    bot.send_message(
        msg.chat.id,
        f"✅ حجم: <b>{gb} گیگابایت</b>\n\n"
        "📅 <b>زمان محصول را وارد کنید (روز):</b>\n\n"
        "مثال: 30"
    )

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_add_product_days")
def adm_add_product_days(msg):
    try:
        days = int((msg.text or "").strip())
        if days <= 0:
            raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ عدد معتبر وارد کنید (مثال: 30).")
    set_state(ADMIN_ID, step="adm_add_product_price", new_days=days)
    bot.send_message(
        msg.chat.id,
        f"✅ زمان: <b>{days} روز</b>\n\n"
        "💰 <b>قیمت محصول را به تومان وارد کنید:</b>\n\n"
        "مثال: 500000"
    )

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_add_product_price")
def adm_add_product_price(msg):
    try:
        price = int((msg.text or "").strip().replace(",", "").replace("٬", ""))
        if price <= 0:
            raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ قیمت معتبر وارد کنید.")

    state = get_state(ADMIN_ID)
    gb    = state["new_gb"]
    days  = state["new_days"]

    icons = {1: "⚡", 2: "🚀", 3: "🔥", 5: "💥"}
    icon  = icons.get(gb, "📦")
    label = f"{icon} {gb}GB  —  {days} روز  —  {fmt(price)} تومان"

    import time
    plan_key = f"prod_{int(time.time())}"

    with get_db() as conn:
        conn.execute(
            "INSERT INTO products(plan_key,label,gb,days,price) VALUES(?,?,?,?,?)",
            (plan_key, label, gb, days, price)
        )
        conn.commit()

    reload_plans()
    clear_state(ADMIN_ID)
    bot.send_message(
        msg.chat.id,
        f"✅ <b>محصول با موفقیت اضافه شد!</b> 🎉\n\n"
        f"📊 حجم: <b>{gb} گیگابایت</b>\n"
        f"📅 مدت: <b>{days} روز</b>\n"
        f"💰 قیمت: <b>{fmt(price)} تومان</b>"
    )

# ─────────────────────────────────────────────
#  ADMIN: USER MANAGEMENT
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "ap_stats" and c.from_user.id == ADMIN_ID)
def cb_ap_stats(call):
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        uc = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        pc = conn.execute("SELECT COUNT(*) as c FROM receipts WHERE status='pending'").fetchone()["c"]
        ts = conn.execute("SELECT SUM(total_price) as s FROM orders WHERE status='delivered'").fetchone()["s"] or 0
    bot.send_message(
        call.message.chat.id,
        f"📊 <b>آمار کلی</b>\n\n"
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
        return bot.send_message(call.message.chat.id, "کاربری وجود ندارد.")

    kb = types.InlineKeyboardMarkup(row_width=1)
    for u in users:
        label = f"{'⛔ ' if u['is_banned'] else '✅ '}@{u['username'] or u['full_name'] or u['user_id']}  |  {fmt(u['wallet'])} ت"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"ap_user_{u['user_id']}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️ قبلی", callback_data=f"ap_users_{page-1}"))
    if offset + limit < total:
        nav.append(types.InlineKeyboardButton("بعدی ▶️", callback_data=f"ap_users_{page+1}"))
    if nav:
        kb.add(*nav)
    kb.add(types.InlineKeyboardButton("🔙 پنل ادمین", callback_data="menu_admin"))

    bot.send_message(
        call.message.chat.id,
        f"👥 <b>لیست کاربران</b>  ({total} نفر)  —  صفحه {page+1}:",
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
    _show_user_detail(call.message.chat.id, int(call.data[8:]))

def _show_user_detail(chat_id, uid):
    u = get_user(uid)
    if not u:
        return bot.send_message(chat_id, "❌ کاربر یافت نشد.")
    with get_db() as conn:
        oc = conn.execute("SELECT COUNT(*) as c FROM orders WHERE user_id=?", (uid,)).fetchone()["c"]
        ot = conn.execute("SELECT SUM(total_price) as s FROM orders WHERE user_id=? AND status='delivered'", (uid,)).fetchone()["s"] or 0

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ شارژ کیف پول", callback_data=f"ap_add_{uid}"),
        types.InlineKeyboardButton("➖ کسر از کیف پول", callback_data=f"ap_sub_{uid}"),
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
        f"🛒 تعداد سفارش: {oc}\n"
        f"💸 مجموع خرید: {fmt(ot)} تومان\n"
        f"⛔ وضعیت: {'🔴 مسدود' if u['is_banned'] else '🟢 فعال'}\n"
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
            f"✅ <b>کیف پول شما شارژ شد!</b> 🎉\n\n"
            f"💰 مبلغ: <b>{fmt(amount)} تومان</b>\n"
            f"💎 موجودی جدید: <b>{fmt(new_bal)} تومان</b>"
        )
        bot.send_message(msg.chat.id, f"✅ {fmt(amount)} تومان به کیف پول {uid} اضافه شد. موجودی: {fmt(new_bal)}")
    else:
        cur = get_wallet(uid)
        dec = min(amount, cur)
        deduct_wallet(uid, dec)
        bot.send_message(msg.chat.id, f"➖ {fmt(dec)} تومان از کیف پول {uid} کسر شد. موجودی جدید: {fmt(get_wallet(uid))}")
    clear_state(ADMIN_ID)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_ban_") and c.from_user.id == ADMIN_ID)
def cb_ap_ban(call):
    uid = int(call.data[7:])
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        u  = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
        ns = 0 if u["is_banned"] else 1
        conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (ns, uid))
        conn.commit()
    label = "🔴 مسدود" if ns else "🟢 فعال"
    bot.send_message(call.message.chat.id, f"✅ وضعیت کاربر {uid} به <b>{label}</b> تغییر یافت.")
    if ns:
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
@bot.message_handler(content_types=["text"], func=lambda m: True)
def fallback(msg):
    if is_offline_for(msg.from_user.id):
        return bot.send_message(msg.chat.id, OFFLINE_MSG)
    u = get_user(msg.from_user.id)
    if u and u["is_banned"]:
        return
    state = get_state(msg.from_user.id)
    if state.get("step"):
        return
    send_main_menu(msg.chat.id, msg.from_user.id, "🏠 از منوی زیر گزینه مورد نظر را انتخاب کنید:")

# ─────────────────────────────────────────────
#  FLASK — Railway health check
# ─────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/health")
def health():
    return jsonify({"status": "ok", "bot_online": BOT_ONLINE})

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
    print(f"🚀 ViraNet Bot — port {PORT} — admin {ADMIN_ID}")
    threading.Thread(target=run_flask, daemon=True).start()
    print("✅ Flask started")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
