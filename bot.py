import os, sqlite3, random, string, threading, io, html as html_lib, hmac, hashlib, json, urllib.parse, time
from datetime import datetime

import requests
import telebot
from telebot import types
from flask import Flask, jsonify, request, Response

try:
    import qrcode
    QR_OK = True
except ImportError:
    QR_OK = False

def make_qr_bytes(text):
    if not QR_OK: return None
    try:
        img = qrcode.make(text)
        buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
        return buf
    except Exception as e:
        print(f"QR error: {e}"); return None

# ─────────────────────────────────────────────
#  ENV
# ─────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "8030883585"))
PORT           = int(os.environ.get("PORT", 8080))
USD_TO_TOMAN   = int(os.environ.get("USD_TO_TOMAN", "90000"))
WEBAPP_URL     = os.environ.get("WEBAPP_URL", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")

SUPPORT_USERNAME = "ViraNet0"
REFERRAL_BONUS   = 5000
AGENCY_MIN_WALLET = 1  # حداقل ۱ تومان برای درخواست نمایندگی

CARD_NUMBER = "123456789456123"
CARD_OWNER  = "حسین حسینی"
TRX_WALLET  = "YOUR_TRX_WALLET_ADDRESS"
BOT_ONLINE  = True
PLANS: dict = {}
crypto_stop_events: dict = {}
agent_bots: dict = {}  # توکن → thread bot

# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────
DB_PATH = "viranet.db"

def get_db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    global CARD_NUMBER, CARD_OWNER, TRX_WALLET
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL,
            username TEXT, full_name TEXT, wallet INTEGER DEFAULT 0,
            referral_code TEXT UNIQUE, referred_by INTEGER,
            is_banned INTEGER DEFAULT 0, joined_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            plan_key TEXT NOT NULL, quantity INTEGER NOT NULL,
            total_price INTEGER NOT NULL, payment_method TEXT,
            status TEXT DEFAULT 'pending', created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS order_services (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, service_name TEXT NOT NULL,
            config_text TEXT, sub_link TEXT, plan_key TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            order_id INTEGER, wallet_amount INTEGER, receipt_type TEXT NOT NULL,
            file_id TEXT, status TEXT DEFAULT 'pending', admin_msg_id INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS wallet_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL, status TEXT DEFAULT 'pending',
            admin_msg_id INTEGER, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_key TEXT UNIQUE NOT NULL,
            label TEXT NOT NULL, gb INTEGER NOT NULL, days INTEGER NOT NULL,
            price INTEGER NOT NULL, active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agency_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            bot_token TEXT NOT NULL, status TEXT DEFAULT 'pending',
            admin_msg_id INTEGER, created_at TEXT DEFAULT (datetime('now'))
        );
        """)
        try:
            conn.execute("ALTER TABLE order_services ADD COLUMN sub_link TEXT")
            conn.commit()
        except Exception: pass

        cnt = conn.execute("SELECT COUNT(*) as c FROM products").fetchone()["c"]
        if cnt == 0:
            planets = [
                ("pluto",    "🌸 پلوتو",    2,  30,  504_000),
                ("jupiter",  "🎁 مشتری",    3,  30,  720_000),
                ("saturn",   "🔱 زحل",      4,  30,  945_000),
                ("venus",    "🏄 زهره",     5,  30, 1_143_000),
                ("earth",    "🌎 زمین",     6,  30, 1_395_000),
                ("neptune",  "🚀 نپتون",    7,  30, 1_665_000),
                ("mars",     "🎁 مریخ",     8,  30, 1_890_000),
                ("uranus",   "🎁 اورانوس", 10,  30, 1_980_000),
                ("mercury2", "🔱 عطارد ۲", 20,  30, 3_600_000),
            ]
            for key, lbl, gb, days, price in planets:
                conn.execute("INSERT OR IGNORE INTO products(plan_key,label,gb,days,price) VALUES(?,?,?,?,?)", (key, lbl, gb, days, price))
            conn.commit()

        for k, v in [("card_number", CARD_NUMBER), ("card_owner", CARD_OWNER), ("trx_wallet", TRX_WALLET)]:
            if not conn.execute("SELECT 1 FROM settings WHERE key=?", (k,)).fetchone():
                conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))
        conn.commit()

        for row in conn.execute("SELECT key,value FROM settings").fetchall():
            if row["key"] == "card_number": CARD_NUMBER = row["value"]
            if row["key"] == "card_owner":  CARD_OWNER  = row["value"]
            if row["key"] == "trx_wallet":  TRX_WALLET  = row["value"]

    reload_plans()
    print("✅ Database ready")

def _make_label(gb, days, price):
    icons = {1:"⚡", 2:"🚀", 3:"🔥", 5:"💥", 10:"🌟"}
    icon  = icons.get(gb, "📦")
    months = days // 30
    period = f"{months} ماهه" if months >= 1 else f"{days} روزه"
    return f"{icon} {gb} گیگ | {period} | {fmt(price)} تومان"

def reload_plans():
    global PLANS
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY price").fetchall()
    PLANS = {r["plan_key"]: {"label": r["label"], "gb": r["gb"], "days": r["days"], "price": r["price"]} for r in rows}

def save_setting(key, value):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
        conn.commit()

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def get_user(uid):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def ensure_user(tg_user, referred_by=None):
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (tg_user.id,)).fetchone():
            rc   = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
            full = ((tg_user.first_name or "") + " " + (tg_user.last_name or "")).strip()
            conn.execute("INSERT INTO users(user_id,username,full_name,referral_code,referred_by) VALUES(?,?,?,?,?)",
                         (tg_user.id, tg_user.username, full, rc, referred_by))
            if referred_by:
                conn.execute("UPDATE users SET wallet=wallet+? WHERE user_id=?", (REFERRAL_BONUS, referred_by))
            conn.commit()
            return True
    return False

def get_wallet(uid):
    u = get_user(uid); return u["wallet"] if u else 0
def add_wallet(uid, amt):
    with get_db() as conn:
        conn.execute("UPDATE users SET wallet=wallet+? WHERE user_id=?", (amt, uid)); conn.commit()
def deduct_wallet(uid, amt):
    with get_db() as conn:
        conn.execute("UPDATE users SET wallet=wallet-? WHERE user_id=?", (amt, uid)); conn.commit()
def fmt(p): return f"{p:,}"
def random_name():
    adj  = ["Swift","Storm","Nova","Volt","Blaze","Echo","Apex","Core","Flux","Zen"]
    noun = ["Link","Node","Wave","Star","Gate","Net","Byte","Cloud","Edge","Hub"]
    return f"{random.choice(adj)}{random.choice(noun)}{random.randint(10,99)}"
def now_str(): return datetime.now().strftime("%Y-%m-%d %H:%M")

def safe_delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass

# ── قیمت TRX ──────────────────────────────────
def get_trx_price_usd():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=tron&vs_currencies=usd", timeout=6)
        return float(r.json()["tron"]["usd"])
    except Exception as e:
        print(f"[TRX] {e}"); return None

def toman_to_trx(toman):
    p = get_trx_price_usd()
    return round(toman / (USD_TO_TOMAN * p), 2) if p else None

def crypto_payment_text(total, trx_amt):
    trx_line = f"💎 <b>معادل TRX:</b>  <code>{trx_amt}</code>  TRX" if trx_amt else "⏳ <b>در حال دریافت قیمت...</b>"
    return (
        "🔷 <b>پرداخت با ترون (TRX)</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🛒 <b>مبلغ سفارش:</b>  {fmt(total)} تومان\n\n"
        f"{trx_line}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 <b>آدرس کیف پول TRX:</b>\n\n"
        f"<code>{TRX_WALLET}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 <b>مراحل پرداخت:</b>\n\n"
        "   1️⃣  مقدار TRX بالا را به آدرس فوق ارسال کنید\n"
        "   2️⃣  پس از واریز دکمه ✅ <b>واریز شد</b> را بزنید\n"
        "   3️⃣  عکس رسید تراکنش را ارسال کنید\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ <b>نکات مهم:</b>\n\n"
        "   🔹 فقط شبکه <b>TRON (TRC-20)</b>\n"
        "   🔹 آدرس را با یک ضربه کپی کنید\n"
        "   🔹 زمان بررسی: کمتر از ۳۰ دقیقه\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🕐 <b>آخرین آپدیت:</b>  {datetime.now().strftime('%H:%M:%S')}"
    )

def crypto_payment_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✅ واریز شد", callback_data="crypto_paid"))
    kb.add(types.InlineKeyboardButton("🔙 بازگشت به روش پرداخت", callback_data="crypto_back"))
    return kb

# ── Telegram WebApp validation ─────────────────
def verify_webapp(init_data: str):
    try:
        params = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        recv_hash = params.pop("hash", None)
        if not recv_hash: return None
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc   = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash): return None
        return json.loads(params.get("user", "{}"))
    except Exception as e:
        print(f"[webapp verify] {e}"); return None

# ── AI chat ────────────────────────────────────
def ai_chat(messages: list) -> str:
    if OPENAI_API_KEY:
        url     = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        model   = "gpt-4o-mini"
    elif GROQ_API_KEY:
        url     = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        model   = "llama-3.3-70b-versatile"
    else:
        return "⚙️ برای استفاده از هوش مصنوعی، متغیر OPENAI_API_KEY یا GROQ_API_KEY را در Railway تنظیم کنید."
    try:
        r = requests.post(url, headers=headers, json={"model": model, "messages": messages, "max_tokens": 2000}, timeout=30)
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"❌ خطا در ارتباط با هوش مصنوعی: {e}"

AI_SYSTEM = (
    "تو یک توسعه‌دهنده ارشد ربات تلگرام هستی که با Python و pyTelegramBotAPI کار می‌کنی. "
    "ربات ViraNet یک فروشگاه VPN است با SQLite، Flask، و inline keyboard. "
    "کاربر از تو می‌خواهد تغییراتی در ربات ایجاد کنی. "
    "پاسخ دقیق، کاربردی و با کد Python بده. "
    "کدها را داخل ```python ``` بنویس. توضیحات را فارسی بنویس."
)

# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────
user_states: dict = {}
def set_state(uid, **kw): user_states.setdefault(uid, {}).update(kw)
def get_state(uid): return user_states.get(uid, {})
def clear_state(uid):
    user_states.pop(uid, None)
    if uid in crypto_stop_events:
        crypto_stop_events[uid].set(); del crypto_stop_events[uid]

# ─────────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

OFFLINE_MSG = (
    "🔧 <b>ربات موقتاً در دسترس نیست</b>\n\n"
    "⚙️ در حال بروزرسانی هستیم.\n\n"
    f"❓ اطلاعات بیشتر: @{SUPPORT_USERNAME}"
)

def is_offline_for(uid): return not BOT_ONLINE and uid != ADMIN_ID

# ── Main Menu ──────────────────────────────────
def main_menu_kb(user_id):
    kb = types.InlineKeyboardMarkup(row_width=2)

    # ردیف ۱: فروشگاه (آبی) + سرویس‌های من (سبز تیره)
    kb.add(
        types.InlineKeyboardButton("🔵 🛒 فروشگاه",        callback_data="menu_shop"),
        types.InlineKeyboardButton("🟢 📦 سرویس‌های من",   callback_data="menu_services"),
    )
    # ردیف ۲: تست رایگان (قرمز/نارنجی)
    kb.add(
        types.InlineKeyboardButton("📦 تست رایگان",         callback_data="menu_free_test"),
    )
    # ردیف ۳: کیف پول (سبز) + حساب کاربری (آبی)
    kb.add(
        types.InlineKeyboardButton("🟢 💰 کیف پول",         callback_data="menu_wallet"),
        types.InlineKeyboardButton("🔵 👤 حساب کاربری",    callback_data="menu_account"),
    )
    # ردیف ۴: دعوت دوستان + پنل کاربری
    kb.add(
        types.InlineKeyboardButton("🎁 دعوت دوستان",        callback_data="menu_referral"),
        types.InlineKeyboardButton("🌐 پنل کاربری",         callback_data="menu_panel"),
    )
    # ردیف ۵: پشتیبانی (قرمز)
    kb.add(types.InlineKeyboardButton("🔴 🆘 پشتیبانی",    url=f"https://t.me/{SUPPORT_USERNAME}"))
    # ردیف ۶: درخواست نمایندگی
    kb.add(types.InlineKeyboardButton("🤝 درخواست نمایندگی", callback_data="menu_agency"))

    if user_id == ADMIN_ID:
        kb.add(
            types.InlineKeyboardButton("🔴 خاموش", callback_data="admin_bot_off"),
            types.InlineKeyboardButton("🟢 روشن",  callback_data="admin_bot_on"),
        )
        kb.add(types.InlineKeyboardButton("⚙️ پنل ادمین (چت)", callback_data="menu_admin"))
    return kb

def send_main_menu(chat_id, user_id, text=None, delete_msg_id=None):
    if delete_msg_id:
        safe_delete(chat_id, delete_msg_id)
    bot.send_message(chat_id,
        text or "🏠 <b>منوی اصلی ViraNet</b>\n\n✨ گزینه مورد نظر را انتخاب کنید:",
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
    if is_new:
        try:
            u = get_user(msg.from_user.id)
            bot.send_message(ADMIN_ID,
                f"👤 <b>کاربر جدید!</b>\n\n🆔 <code>{msg.from_user.id}</code>\n"
                f"📛 {u['full_name'] or '---'}\n👤 @{u['username'] or '---'}\n🕐 {now_str()}"
            )
        except Exception: pass
    bot.send_message(msg.chat.id,
        "✨ <b>به ویرا نت خوش آمدید!</b> 🎉\n\n"
        "💎 <b>فروشگاه سرویس‌های اینترنتی پرسرعت و امن</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ <b>سرعت بالا</b> بدون محدودیت\n"
        "🛡️ <b>امنیت کامل</b> با رمزنگاری پیشرفته\n"
        "🔧 <b>پشتیبانی ۲۴/۷</b> در هر ساعت از شبانه‌روز\n"
        "🚀 <b>فعال‌سازی فوری</b> بعد از تایید پرداخت\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 از منوی زیر انتخاب کنید:",
        reply_markup=main_menu_kb(msg.from_user.id)
    )

# ─────────────────────────────────────────────
#  MENU CALLBACKS
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "menu_shop")
def cb_menu_shop(call):
    if is_offline_for(call.from_user.id): return bot.answer_callback_query(call.id, "⚠️ ربات خاموش است.", show_alert=True)
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user); clear_state(call.from_user.id)
    safe_delete(call.message.chat.id, call.message.message_id)
    _show_shop(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_account")
def cb_menu_account(call):
    if is_offline_for(call.from_user.id): return bot.answer_callback_query(call.id, "⚠️ ربات خاموش است.", show_alert=True)
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user)
    safe_delete(call.message.chat.id, call.message.message_id)
    _show_account(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_wallet")
def cb_menu_wallet(call):
    if is_offline_for(call.from_user.id): return bot.answer_callback_query(call.id, "⚠️ ربات خاموش است.", show_alert=True)
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user); clear_state(call.from_user.id)
    safe_delete(call.message.chat.id, call.message.message_id)
    _show_wallet(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_services")
def cb_menu_services(call):
    if is_offline_for(call.from_user.id): return bot.answer_callback_query(call.id, "⚠️ ربات خاموش است.", show_alert=True)
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user)
    safe_delete(call.message.chat.id, call.message.message_id)
    _show_my_services(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_referral")
def cb_menu_referral(call):
    if is_offline_for(call.from_user.id): return bot.answer_callback_query(call.id, "⚠️ ربات خاموش است.", show_alert=True)
    bot.answer_callback_query(call.id)
    ensure_user(call.from_user)
    safe_delete(call.message.chat.id, call.message.message_id)
    _show_referral(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "menu_admin")
def cb_menu_admin(call):
    if call.from_user.id != ADMIN_ID: return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    bot.answer_callback_query(call.id); clear_state(ADMIN_ID)
    _show_admin_panel(call.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "back_main")
def cb_back_main(call):
    bot.answer_callback_query(call.id); clear_state(call.from_user.id)
    safe_delete(call.message.chat.id, call.message.message_id)
    send_main_menu(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "admin_bot_off")
def cb_bot_off(call):
    global BOT_ONLINE
    if call.from_user.id != ADMIN_ID: return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    bot.answer_callback_query(call.id); BOT_ONLINE = False
    bot.send_message(call.message.chat.id, "🔴 <b>ربات خاموش شد.</b>")

@bot.callback_query_handler(func=lambda c: c.data == "admin_bot_on")
def cb_bot_on(call):
    global BOT_ONLINE
    if call.from_user.id != ADMIN_ID: return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    bot.answer_callback_query(call.id); BOT_ONLINE = True
    bot.send_message(call.message.chat.id, "🟢 <b>ربات روشن شد.</b>")

# ── پنل کاربری ─────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "menu_panel")
def cb_menu_panel(call):
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    if uid != ADMIN_ID:
        bot.answer_callback_query(call.id,
            "🔒 این بخش فقط برای ادمین است!",
            show_alert=True
        )
        bot.send_message(call.message.chat.id,
            "🔒 <b>دسترسی محدود شد</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🚫 <b>این بخش فقط برای مدیریت ربات است</b>\n\n"
            "👤 شما به عنوان کاربر عادی دسترسی به پنل مدیریت ندارید.\n\n"
            "📌 <b>شما می‌توانید از منوی اصلی استفاده کنید:</b>\n\n"
            "   🛒 خرید سرویس از فروشگاه\n"
            "   💰 مشاهده و شارژ کیف پول\n"
            "   📦 مشاهده سرویس‌های فعال\n"
            "   🆘 تماس با پشتیبانی\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"❓ <b>سوال دارید؟</b> @{SUPPORT_USERNAME}",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("🏠 بازگشت به منوی اصلی", callback_data="back_main")
            )
        )
        return
    # ادمین → پنل وب
    kb = types.InlineKeyboardMarkup(row_width=1)
    if WEBAPP_URL:
        kb.add(types.InlineKeyboardButton("🌐 باز کردن پنل مدیریت", web_app=types.WebAppInfo(url=WEBAPP_URL + "/panel")))
    kb.add(types.InlineKeyboardButton("⚙️ پنل چتی", callback_data="menu_admin"))
    kb.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))
    bot.send_message(call.message.chat.id,
        "🌐 <b>پنل مدیریت ViraNet</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚙️ از اینجا می‌توانید:\n\n"
        "   📊 آمار و گزارشات را مشاهده کنید\n"
        "   📥 رسیدهای معلق را بررسی کنید\n"
        "   👥 کاربران را مدیریت کنید\n"
        "   📦 محصولات را ویرایش کنید\n"
        "   🤖 با هوش مصنوعی تغییرات بدهید\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=kb
    )

# ── تست رایگان ─────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "menu_free_test")
def cb_free_test(call):
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🏠 بازگشت به منوی اصلی", callback_data="back_main"))
    safe_delete(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id,
        "📦 <b>تست رایگان ViraNet</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🙏 <b>ممنون از علاقه‌مندی شما!</b>\n\n"
        "⏳ متأسفانه در حال حاضر <b>تست رایگان</b> در دسترس نیست.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💡 <b>پیشنهاد ما:</b>\n\n"
        "   🌸 پلن <b>پلوتو</b> با کمترین قیمت را امتحان کنید\n"
        "   📦 ۲ گیگابایت | ۳۰ روز\n"
        "   ✅ فعال‌سازی فوری\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔔 به زودی تست رایگان فعال می‌شود!\n\n"
        f"❓ اطلاعات بیشتر: @{SUPPORT_USERNAME}",
        reply_markup=kb
    )

# ── درخواست نمایندگی ────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "menu_agency")
def cb_agency(call):
    if is_offline_for(call.from_user.id):
        return bot.answer_callback_query(call.id, "⚠️ ربات خاموش است.", show_alert=True)
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    wallet = get_wallet(uid)
    safe_delete(call.message.chat.id, call.message.message_id)

    if wallet < AGENCY_MIN_WALLET:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("💰 شارژ کیف پول", callback_data="menu_wallet"),
            types.InlineKeyboardButton("🏠 بازگشت", callback_data="back_main"),
        )
        bot.send_message(call.message.chat.id,
            "🤝 <b>درخواست همکاری و نمایندگی</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⚠️ <b>موجودی ناکافی!</b>\n\n"
            "برای ثبت درخواست همکاری، حداقل موجودی کیف پول شما باید:\n\n"
            "   💎 <b>۱ تومان</b> (حداقل یک خرید انجام داده باشید)\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 <b>موجودی فعلی:</b> {fmt(wallet)} تومان\n\n"
            "📌 <b>چرا این شرط وجود دارد؟</b>\n\n"
            "   🔹 اطمینان از جدیت درخواست\n"
            "   🔹 تجربه خرید از پلتفرم ما\n"
            "   🔹 آشنایی با کیفیت سرویس‌ها\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "👇 کیف پول خود را شارژ کرده و دوباره تلاش کنید:",
            reply_markup=kb
        )
        return

    # موجودی کافی → درخواست توکن
    set_state(uid, step="agency_token")
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("❌ انصراف", callback_data="back_main"))
    bot.send_message(call.message.chat.id,
        "🤝 <b>درخواست همکاری و نمایندگی ViraNet</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎉 <b>تبریک!</b> شما واجد شرایط درخواست نمایندگی هستید!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 <b>مزایای نمایندگی:</b>\n\n"
        "   ✅ ربات اختصاصی با برند خودتان\n"
        "   ✅ تمام امکانات ViraNet در ربات شما\n"
        "   ✅ پشتیبانی فنی کامل\n"
        "   ✅ درآمد از فروش سرویس‌ها\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 <b>مراحل:</b>\n\n"
        "   1️⃣  یک ربات جدید از @BotFather بسازید\n"
        "   2️⃣  توکن ربات را دریافت کنید\n"
        "   3️⃣  توکن را اینجا ارسال کنید\n"
        "   4️⃣  منتظر تایید ادمین باشید\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 <b>توکن ربات خود را ارسال کنید:</b>\n"
        "<i>(مثال: 1234567890:ABCdef...)</i>",
        reply_markup=kb
    )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "agency_token")
def agency_token_input(msg):
    if is_offline_for(msg.from_user.id): return bot.send_message(msg.chat.id, OFFLINE_MSG)
    token = (msg.text or "").strip()
    if not token or ":" not in token or len(token) < 20:
        return bot.send_message(msg.chat.id,
            "⚠️ <b>توکن نامعتبر!</b>\n\n"
            "فرمت صحیح: <code>1234567890:ABCdefGHIjkl...</code>\n\n"
            "👇 لطفاً توکن صحیح ارسال کنید:"
        )
    uid = msg.from_user.id
    u = get_user(uid); uname = u["username"] or u["full_name"] or str(uid)

    # ذخیره در DB
    with get_db() as conn:
        cur = conn.execute("INSERT INTO agency_requests(user_id,bot_token,status) VALUES(?,?,?)",
                           (uid, token, "pending"))
        req_id = cur.lastrowid; conn.commit()

    # ارسال به ادمین
    adm_kb = types.InlineKeyboardMarkup(row_width=2)
    adm_kb.add(
        types.InlineKeyboardButton("✅ تایید نمایندگی", callback_data=f"agency_ok_{req_id}"),
        types.InlineKeyboardButton("❌ رد",              callback_data=f"agency_rej_{req_id}"),
    )
    adm_msg = bot.send_message(ADMIN_ID,
        f"🤝 <b>درخواست نمایندگی جدید!</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>کاربر:</b> @{uname}  |  <code>{uid}</code>\n"
        f"💰 <b>موجودی:</b> {fmt(get_wallet(uid))} تومان\n"
        f"🕐 <b>زمان:</b> {now_str()}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 <b>توکن ربات:</b>\n<code>{token}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👇 درخواست را تایید یا رد کنید:",
        reply_markup=adm_kb
    )
    with get_db() as conn:
        conn.execute("UPDATE agency_requests SET admin_msg_id=? WHERE id=?", (adm_msg.message_id, req_id))
        conn.commit()

    clear_state(uid)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🏠 بازگشت به منوی اصلی", callback_data="back_main"))
    bot.send_message(msg.chat.id,
        "✅ <b>درخواست نمایندگی ثبت شد!</b> 🎉\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📬 درخواست شما برای ادمین ارسال شد.\n\n"
        "⏳ <b>زمان بررسی:</b> کمتر از ۲۴ ساعت\n\n"
        "📌 پس از تایید، ربات شما به صورت کامل راه‌اندازی می‌شود.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"❓ پیگیری: @{SUPPORT_USERNAME}",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("agency_ok_"))
def cb_agency_approve(call):
    if call.from_user.id != ADMIN_ID: return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    req_id = int(call.data[10:])
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        req = conn.execute("SELECT * FROM agency_requests WHERE id=?", (req_id,)).fetchone()
        if not req: return bot.send_message(call.message.chat.id, "❌ درخواست یافت نشد.")
        conn.execute("UPDATE agency_requests SET status='approved' WHERE id=?", (req_id,))
        conn.commit()
    token = req["bot_token"]; uid = req["user_id"]

    # راه‌اندازی ربات نمایندگی در thread جداگانه
    def run_agent_bot(agent_token, owner_id):
        try:
            agent = telebot.TeleBot(agent_token, parse_mode="HTML")

            @agent.message_handler(commands=["start"])
            def agent_start(msg):
                ensure_user(msg.from_user)
                agent.send_message(msg.chat.id,
                    "✨ <b>به ویرا نت خوش آمدید!</b> 🎉\n\n"
                    "💎 <b>فروشگاه سرویس‌های اینترنتی</b>\n\n"
                    "⚡ سرعت بالا | 🛡️ امنیت کامل | 🔧 پشتیبانی ۲۴ ساعته\n\n"
                    "👇 از منوی زیر انتخاب کنید:",
                    reply_markup=main_menu_kb(msg.from_user.id)
                )

            @agent.message_handler(func=lambda m: True, content_types=['text','photo','document'])
            def agent_fallback(msg):
                try:
                    bot.forward_message(ADMIN_ID, msg.chat.id, msg.message_id)
                    bot.send_message(ADMIN_ID, f"📨 پیام از ربات نمایندگی\n👤 <code>{msg.from_user.id}</code>\n🤖 مالک: <code>{owner_id}</code>")
                except Exception: pass

            agent_bots[agent_token] = agent
            agent.infinity_polling(timeout=30, long_polling_timeout=20)
        except Exception as e:
            print(f"[agent bot] Error: {e}")

    t = threading.Thread(target=run_agent_bot, args=(token, uid), daemon=True)
    t.start()

    # پاک کردن پیام ادمین
    safe_delete(call.message.chat.id, call.message.message_id)

    # اطلاع به کاربر
    try:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("🏠 بازگشت به منوی اصلی", callback_data="back_main"))
        bot.send_message(uid,
            "🎉 <b>تبریک! درخواست نمایندگی تایید شد!</b> 🚀\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "✅ ربات شما با موفقیت راه‌اندازی شد!\n\n"
            "🤖 <b>ربات شما هم‌اکنون فعال است</b> و تمام امکانات ViraNet را دارد:\n\n"
            "   🛒 فروشگاه کامل با تمام پلن‌ها\n"
            "   💰 سیستم کیف پول\n"
            "   📦 مدیریت سرویس‌ها\n"
            "   🆘 پشتیبانی\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📌 <b>نکته:</b> تمام سفارش‌ها و رسیدها به ادمین اصلی ارسال می‌شود.\n\n"
            f"❓ پشتیبانی: @{SUPPORT_USERNAME}",
            reply_markup=kb
        )
    except Exception: pass
    bot.send_message(call.message.chat.id, f"✅ نمایندگی برای کاربر <code>{uid}</code> فعال شد!")

@bot.callback_query_handler(func=lambda c: c.data.startswith("agency_rej_"))
def cb_agency_reject(call):
    if call.from_user.id != ADMIN_ID: return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    req_id = int(call.data[11:])
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        req = conn.execute("SELECT * FROM agency_requests WHERE id=?", (req_id,)).fetchone()
        conn.execute("UPDATE agency_requests SET status='rejected' WHERE id=?", (req_id,))
        conn.commit()
    safe_delete(call.message.chat.id, call.message.message_id)
    if req:
        try:
            bot.send_message(req["user_id"],
                "❌ <b>درخواست نمایندگی رد شد</b> 😔\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "متأسفانه درخواست همکاری شما در این مرحله تایید نشد.\n\n"
                "📌 <b>دلایل احتمالی:</b>\n\n"
                "   🔹 توکن ربات نامعتبر\n"
                "   🔹 ظرفیت نمایندگی پر شده\n"
                "   🔹 شرایط لازم تکمیل نیست\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📞 برای پیگیری: @{SUPPORT_USERNAME}"
            )
        except Exception: pass
    bot.send_message(call.message.chat.id, "❌ درخواست نمایندگی رد شد.")

# ── حساب کاربری ────────────────────────────────
def _show_account(chat_id, user_id):
    u = get_user(user_id)
    if not u: return
    with get_db() as conn:
        svc_cnt = conn.execute("SELECT COUNT(*) as c FROM order_services WHERE user_id=? AND config_text IS NOT NULL", (user_id,)).fetchone()["c"]
        ord_cnt = conn.execute("SELECT COUNT(*) as c FROM orders WHERE user_id=?", (user_id,)).fetchone()["c"]
    bot_info = bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{u['referral_code']}"
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))
    bot.send_message(chat_id,
        "👤 <b>حساب کاربری</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📛 <b>نام:</b>  {u['full_name'] or '---'}\n"
        f"🆔 <b>آیدی:</b>  <code>{user_id}</code>\n"
        f"👤 <b>یوزرنیم:</b>  @{u['username'] or '---'}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 <b>موجودی کیف پول:</b>  {fmt(u['wallet'])} تومان\n"
        f"📦 <b>سرویس‌های فعال:</b>  {svc_cnt} عدد\n"
        f"🛒 <b>تعداد خرید:</b>  {ord_cnt} سفارش\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔗 <b>لینک دعوت شما:</b>\n\n"
        f"<code>{ref_link}</code>",
        reply_markup=kb
    )

# ─────────────────────────────────────────────
#  🛒 SHOP
# ─────────────────────────────────────────────
def _fmt_price_short(price):
    t = price // 1000
    return f"◄{t:,} T"

def _show_shop(chat_id, user_id):
    reload_plans()
    if not PLANS: return bot.send_message(chat_id, "❌ هیچ محصولی موجود نیست.")
    set_state(user_id, step="shop_plan")
    kb = types.InlineKeyboardMarkup(row_width=3)
    for key, plan in PLANS.items():
        gb_txt    = f"📦 {plan['gb']} گیگ"
        price_txt = _fmt_price_short(plan["price"])
        kb.row(
            types.InlineKeyboardButton(plan["label"],  callback_data=f"plan_{key}"),
            types.InlineKeyboardButton(gb_txt,         callback_data=f"plan_{key}"),
            types.InlineKeyboardButton(price_txt,      callback_data=f"plan_{key}"),
        )
    if WEBAPP_URL:
        kb.add(types.InlineKeyboardButton("🌐 خرید از پنل کاربری", web_app=types.WebAppInfo(url=WEBAPP_URL + "/panel")))
    kb.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))
    bot.send_message(chat_id,
        "🛒 <b>فروشگاه ویرا نت</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ فعال‌سازی فوری\n"
        "✅ سرعت نامحدود\n"
        "✅ پشتیبانی ۲۴ ساعته\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 پلن مورد نظر را انتخاب کنید:",
        reply_markup=kb
    )

@bot.message_handler(content_types=["web_app_data"])
def handle_web_app_data(msg):
    try:
        data = json.loads(msg.web_app_data.data)
    except Exception:
        return
    action = data.get("action")
    if action == "buy_plan":
        plan_key = data.get("plan_key")
        reload_plans()
        if plan_key not in PLANS:
            return bot.send_message(msg.chat.id, "❌ پلن نامعتبر.")
        ensure_user(msg.from_user)
        set_state(msg.from_user.id, step="shop_quantity", plan_key=plan_key)
        plan = PLANS[plan_key]
        bot.send_message(msg.chat.id,
            f"✅ <b>پلن انتخاب‌شده:</b>\n{plan['label']}\n\n"
            f"💰 قیمت هر عدد: <b>{fmt(plan['price'])} تومان</b>\n\n"
            "📌 چند سرویس می‌خواهید؟ (۱ تا ۲۰):"
        )

@bot.callback_query_handler(func=lambda c: c.data.startswith("plan_"))
def cb_plan(call):
    if is_offline_for(call.from_user.id): return bot.answer_callback_query(call.id, "⚠️ ربات خاموش است.", show_alert=True)
    plan_key = call.data[5:]; reload_plans()
    if plan_key not in PLANS: return bot.answer_callback_query(call.id, "پلن نامعتبر")
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, step="shop_quantity", plan_key=plan_key)
    plan = PLANS[plan_key]
    safe_delete(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id,
        f"✅ <b>پلن انتخاب‌شده:</b>\n{plan['label']}\n\n"
        f"📊 <b>حجم:</b> {plan['gb']} گیگابایت  |  📅 <b>مدت:</b> {plan['days']} روز\n"
        f"💰 <b>قیمت هر عدد:</b> {fmt(plan['price'])} تومان\n\n"
        "📌 چند سرویس می‌خواهید؟ (۱ تا ۲۰):"
    )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "shop_quantity")
def shop_quantity(msg):
    if is_offline_for(msg.from_user.id): return bot.send_message(msg.chat.id, OFFLINE_MSG)
    try:
        qty = int(msg.text.strip())
        if qty < 1 or qty > 20: raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ لطفاً عددی بین ۱ تا ۲۰ وارد کنید.")
    state = get_state(msg.from_user.id)
    total = PLANS[state["plan_key"]]["price"] * qty
    set_state(msg.from_user.id, step="shop_name", quantity=qty, total_price=total, names=[], name_index=0)
    _ask_name(msg.chat.id, msg.from_user.id, 0, qty, state["plan_key"], total)

def _ask_name(chat_id, user_id, index, qty, plan_key, total):
    plan   = PLANS[plan_key]
    kb     = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎲 اسم رندم",   callback_data=f"name_r_{index}"),
        types.InlineKeyboardButton("✍️ اسم دلخواه", callback_data=f"name_c_{index}"),
    )
    bot.send_message(chat_id,
        f"🏷️ <b>نام‌گذاری سرویس {index+1} از {qty}</b>\n\n"
        f"📦 {plan['gb']}GB — {plan['days']} روز\n"
        f"💰 مبلغ کل: <b>{fmt(total)} تومان</b>\n\n"
        "👇 روش نام‌گذاری را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("name_r_") or c.data.startswith("name_c_"))
def cb_name(call):
    if is_offline_for(call.from_user.id): return bot.answer_callback_query(call.id, "⚠️ ربات خاموش است.", show_alert=True)
    state = get_state(call.from_user.id)
    if state.get("step") not in ("shop_name", "shop_name_input"): return bot.answer_callback_query(call.id)
    parts = call.data.split("_"); action = parts[1]; index = int(parts[2])
    bot.answer_callback_query(call.id)
    if action == "r":
        name  = random_name()
        names = state.get("names", []); names.append(name)
        qty   = state["quantity"]
        set_state(call.from_user.id, step="shop_name", names=names, name_index=index+1)
        safe_delete(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, f"✅ نام رندم: <b>{name}</b> 🎲")
        if index+1 < qty:
            _ask_name(call.message.chat.id, call.from_user.id, index+1, qty, state["plan_key"], state["total_price"])
        else:
            _ask_payment(call.message.chat.id, call.from_user.id)
    else:
        set_state(call.from_user.id, step="shop_name_input", name_index=index)
        bot.send_message(call.message.chat.id, f"✍️ نام سرویس {index+1} را ارسال کنید:")

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "shop_name_input")
def shop_name_input(msg):
    if is_offline_for(msg.from_user.id): return bot.send_message(msg.chat.id, OFFLINE_MSG)
    name  = msg.text.strip()[:30]; state = get_state(msg.from_user.id)
    names = state.get("names", []); names.append(name)
    index = state["name_index"]; qty = state["quantity"]
    set_state(msg.from_user.id, step="shop_name", names=names, name_index=index+1)
    bot.send_message(msg.chat.id, f"✅ نام <b>{name}</b> ثبت شد.")
    if index+1 < qty:
        _ask_name(msg.chat.id, msg.from_user.id, index+1, qty, state["plan_key"], state["total_price"])
    else:
        _ask_payment(msg.chat.id, msg.from_user.id)

def _ask_payment(chat_id, user_id):
    state  = get_state(user_id); plan = PLANS[state["plan_key"]]
    total  = state["total_price"]; wallet = get_wallet(user_id)
    names_text = "\n".join([f"  {i+1}. 🏷️ {n}" for i, n in enumerate(state["names"])])
    set_state(user_id, step="shop_payment")
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(f"💰 کیف پول  (موجودی: {fmt(wallet)} ت)", callback_data="pay_wallet"),
        types.InlineKeyboardButton("💳 کارت به کارت",       callback_data="pay_card"),
        types.InlineKeyboardButton("🔷 ترون (TRX)",          callback_data="pay_crypto"),
    )
    bot.send_message(chat_id,
        f"💳 <b>مرحله پرداخت</b>\n\n"
        f"📦 <b>پلن:</b> {plan['label']}\n"
        f"🔢 <b>تعداد:</b> {state['quantity']} سرویس\n"
        f"🏷️ <b>نام‌ها:</b>\n{names_text}\n\n"
        f"💰 <b>مبلغ:</b> {fmt(total)} تومان\n\n"
        "👇 روش پرداخت را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data in ("pay_wallet", "pay_card", "pay_crypto"))
def cb_payment(call):
    if is_offline_for(call.from_user.id): return bot.answer_callback_query(call.id, "⚠️ ربات خاموش است.", show_alert=True)
    state = get_state(call.from_user.id)
    if state.get("step") != "shop_payment": return bot.answer_callback_query(call.id)
    bot.answer_callback_query(call.id)
    total = state["total_price"]; wallet = get_wallet(call.from_user.id)
    if call.data == "pay_wallet":
        if wallet < total:
            return bot.send_message(call.message.chat.id,
                f"❌ <b>موجودی کیف پول کافی نیست!</b>\n\n"
                f"💰 موجودی: <b>{fmt(wallet)} ت</b>\n"
                f"💳 نیاز: <b>{fmt(total)} ت</b>\n"
                f"⚠️ کمبود: <b>{fmt(total-wallet)} ت</b>"
            )
        deduct_wallet(call.from_user.id, total)
        _create_order_and_notify(call.from_user.id, call.message.chat.id, state, "wallet")
    elif call.data == "pay_card":
        _create_order_and_notify(call.from_user.id, call.message.chat.id, state, "card")
    else:
        _start_crypto_payment(call.from_user.id, call.message.chat.id, state)

# ─────────────────────────────────────────────
#  🔷 CRYPTO
# ─────────────────────────────────────────────
def _start_crypto_payment(user_id, chat_id, state):
    bot.send_message(chat_id, "⏳ <b>در حال دریافت قیمت لحظه‌ای...</b>")
    total = state["total_price"]; trx_amt = toman_to_trx(total)
    sent  = bot.send_message(chat_id, crypto_payment_text(total, trx_amt), reply_markup=crypto_payment_kb())
    set_state(user_id, step="shop_crypto_wait",
              crypto_msg_id=sent.message_id, crypto_chat_id=chat_id,
              total_price=state["total_price"], plan_key=state["plan_key"],
              quantity=state["quantity"], names=state["names"])
    _start_crypto_updater(user_id, chat_id, sent.message_id, total)

def _start_crypto_updater(user_id, chat_id, message_id, total):
    if user_id in crypto_stop_events: crypto_stop_events[user_id].set()
    stop_ev = threading.Event(); crypto_stop_events[user_id] = stop_ev
    def updater():
        while not stop_ev.wait(5):
            if get_state(user_id).get("step") != "shop_crypto_wait": break
            trx_amt = toman_to_trx(total)
            try:
                bot.edit_message_text(crypto_payment_text(total, trx_amt), chat_id, message_id,
                                      reply_markup=crypto_payment_kb(), parse_mode="HTML")
            except Exception as e:
                if "message is not modified" not in str(e): print(f"[crypto] {e}")
    threading.Thread(target=updater, daemon=True).start()

@bot.callback_query_handler(func=lambda c: c.data == "crypto_paid")
def cb_crypto_paid(call):
    if is_offline_for(call.from_user.id): return bot.answer_callback_query(call.id, "⚠️ ربات خاموش است.", show_alert=True)
    state = get_state(call.from_user.id)
    if state.get("step") != "shop_crypto_wait": return bot.answer_callback_query(call.id)
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    if uid in crypto_stop_events: crypto_stop_events[uid].set(); del crypto_stop_events[uid]
    total = state["total_price"]; trx_amt = toman_to_trx(total)
    trx_str = f"{trx_amt} TRX" if trx_amt else "---"
    set_state(uid, step="shop_crypto_receipt")
    bot.send_message(call.message.chat.id,
        "📸 <b>ارسال رسید پرداخت کریپتو</b> 🔷\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✅ ممنون از واریز شما!\n\n"
        f"💰 <b>مبلغ:</b>  {fmt(total)} تومان\n"
        f"🔷 <b>معادل TRX:</b>  {trx_str}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 <b>مرحله آخر:</b>\n\n"
        "   🖼️  یک <b>اسکرین‌شات</b> از تراکنش\n"
        "       یا <b>رسید پرداخت</b> خود را ارسال کنید\n\n"
        "   📋  رسید باید شامل باشد:\n"
        "       🔹 مقدار TRX ارسال‌شده\n"
        "       🔹 آدرس مقصد (ولت ما)\n"
        "       🔹 تاریخ و زمان تراکنش\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🕐 زمان بررسی: کمتر از ۳۰ دقیقه\n\n"
        "👇 <b>عکس رسید را همین‌جا ارسال کنید:</b>"
    )

@bot.callback_query_handler(func=lambda c: c.data == "crypto_back")
def cb_crypto_back(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    if uid in crypto_stop_events: crypto_stop_events[uid].set(); del crypto_stop_events[uid]
    set_state(uid, step="shop_payment")
    _ask_payment(call.message.chat.id, uid)

# ─────────────────────────────────────────────
#  ORDER CREATION
# ─────────────────────────────────────────────
def _create_order_and_notify(user_id, chat_id, state, payment_method):
    plan_key = state["plan_key"]; plan = PLANS[plan_key]
    qty = state["quantity"]; total = state["total_price"]; names = state["names"]
    with get_db() as conn:
        cur = conn.execute("INSERT INTO orders(user_id,plan_key,quantity,total_price,payment_method,status) VALUES(?,?,?,?,?,?)",
                           (user_id, plan_key, qty, total, payment_method, "pending"))
        order_id = cur.lastrowid
        for name in names:
            conn.execute("INSERT INTO order_services(order_id,user_id,service_name,plan_key) VALUES(?,?,?,?)", (order_id, user_id, name, plan_key))
        conn.commit()
    u = get_user(user_id); uname = u["username"] or u["full_name"] or str(user_id)
    names_t = "\n".join([f"  {i+1}. {n}" for i, n in enumerate(names)])
    adm_kb = types.InlineKeyboardMarkup(row_width=2)
    adm_kb.add(
        types.InlineKeyboardButton("✅ تایید", callback_data=f"adm_ok_{order_id}"),
        types.InlineKeyboardButton("❌ رد",    callback_data=f"adm_rej_{order_id}"),
    )
    if payment_method == "wallet":
        adm_msg = bot.send_message(ADMIN_ID,
            f"🛒 <b>سفارش جدید — کیف پول</b>\n\n"
            f"👤 @{uname}  |  <code>{user_id}</code>\n"
            f"🕐 {now_str()}\n\n"
            f"📦 {plan['label']}\n🔢 {qty} سرویس\n🏷️ نام‌ها:\n{names_t}\n\n"
            f"💰 {fmt(total)} تومان", reply_markup=adm_kb
        )
        with get_db() as conn:
            conn.execute("INSERT INTO receipts(user_id,order_id,receipt_type,status,admin_msg_id) VALUES(?,?,?,?,?)",
                         (user_id, order_id, "purchase_wallet", "pending", adm_msg.message_id))
            conn.commit()
        clear_state(user_id)
        bot.send_message(chat_id,
            "✅ <b>سفارش ثبت شد!</b> 🎉\n\n"
            f"💰 {fmt(total)} تومان از کیف پول کسر شد.\n"
            "⏳ پس از تایید ادمین، کانفیگ ارسال می‌شود."
        )
    else:  # card
        set_state(user_id, step="shop_receipt_wait", order_id=order_id)
        bot.send_message(chat_id,
            f"💳 <b>پرداخت کارت به کارت</b>\n\n"
            f"💰 <b>مبلغ:</b>  {fmt(total)} تومان\n\n"
            "🏦 <b>مشخصات حساب:</b>\n\n"
            f"  💳 شماره کارت:\n  <code>{CARD_NUMBER}</code>\n\n"
            f"  👤 به نام: <b>{CARD_OWNER}</b>\n\n"
            f"  1️⃣  {fmt(total)} تومان واریز کنید\n"
            "  2️⃣  رسید را همین‌جا ارسال کنید\n\n"
            "👇 <b>تصویر رسید را ارسال کنید:</b>"
        )

# ─────────────────────────────────────────────
#  PHOTO HANDLER
# ─────────────────────────────────────────────
@bot.message_handler(content_types=["photo"])
def handle_all_photos(msg):
    if is_offline_for(msg.from_user.id): return bot.send_message(msg.chat.id, OFFLINE_MSG)
    step = get_state(msg.from_user.id).get("step")
    if step == "shop_receipt_wait":   _handle_shop_receipt(msg)
    elif step == "shop_crypto_receipt": _handle_crypto_receipt(msg)
    elif step == "wallet_receipt":      _handle_wallet_receipt(msg)

def _handle_shop_receipt(msg):
    state = get_state(msg.from_user.id); order_id = state.get("order_id")
    if not order_id: return bot.send_message(msg.chat.id, "⚠️ خطا: سفارش یافت نشد.")
    file_id = msg.photo[-1].file_id
    u = get_user(msg.from_user.id); uname = u["username"] or u["full_name"] or str(msg.from_user.id)
    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        svc_rows = conn.execute("SELECT * FROM order_services WHERE order_id=?", (order_id,)).fetchall()
    plan = PLANS.get(order["plan_key"], {"label":"---"})
    names_t = "\n".join([f"  {i+1}. {r['service_name']}" for i, r in enumerate(svc_rows)])
    adm_kb = types.InlineKeyboardMarkup(row_width=2)
    adm_kb.add(
        types.InlineKeyboardButton("✅ تایید", callback_data=f"adm_ok_{order_id}"),
        types.InlineKeyboardButton("❌ رد",    callback_data=f"adm_rej_{order_id}"),
    )
    adm_msg = bot.send_photo(ADMIN_ID, file_id,
        caption=(
            f"📥 <b>رسید جدید — کارت به کارت</b>\n\n"
            f"👤 @{uname}  |  <code>{msg.from_user.id}</code>\n"
            f"🕐 {now_str()}\n\n"
            f"📦 {plan['label']}\n🔢 {order['quantity']} سرویس\n🏷️ نام‌ها:\n{names_t}\n\n"
            f"💰 {fmt(order['total_price'])} تومان"
        ), reply_markup=adm_kb
    )
    with get_db() as conn:
        conn.execute("INSERT INTO receipts(user_id,order_id,file_id,receipt_type,status,admin_msg_id) VALUES(?,?,?,?,?,?)",
                     (msg.from_user.id, order_id, file_id, "purchase_card", "pending", adm_msg.message_id))
        conn.commit()
    clear_state(msg.from_user.id)
    bot.send_message(msg.chat.id,
        "📥 <b>رسید دریافت شد!</b> ✅\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏳ <b>در حال بررسی رسید شما...</b>\n\n"
        "📌 پس از تایید، کانفیگ در همین چت ارسال می‌شود.\n\n"
        f"⏱️ زمان بررسی: کمتر از ۳۰ دقیقه\n\n"
        f"❓ سوال؟ @{SUPPORT_USERNAME}"
    )

def _handle_crypto_receipt(msg):
    state = get_state(msg.from_user.id); file_id = msg.photo[-1].file_id
    u = get_user(msg.from_user.id); uname = u["username"] or u["full_name"] or str(msg.from_user.id)
    plan_key = state.get("plan_key"); qty = state.get("quantity", 1)
    total = state.get("total_price", 0); names = state.get("names", [])
    plan = PLANS.get(plan_key, {"label":"---"}); names_t = "\n".join([f"  {i+1}. {n}" for i,n in enumerate(names)])
    trx_amt = toman_to_trx(total); trx_str = f"{trx_amt} TRX" if trx_amt else "---"
    with get_db() as conn:
        cur = conn.execute("INSERT INTO orders(user_id,plan_key,quantity,total_price,payment_method,status) VALUES(?,?,?,?,?,?)",
                           (msg.from_user.id, plan_key, qty, total, "crypto", "pending"))
        order_id = cur.lastrowid
        for name in names:
            conn.execute("INSERT INTO order_services(order_id,user_id,service_name,plan_key) VALUES(?,?,?,?)", (order_id, msg.from_user.id, name, plan_key))
        conn.commit()
    adm_kb = types.InlineKeyboardMarkup(row_width=2)
    adm_kb.add(
        types.InlineKeyboardButton("✅ تایید", callback_data=f"adm_ok_{order_id}"),
        types.InlineKeyboardButton("❌ رد",    callback_data=f"adm_rej_{order_id}"),
    )
    adm_msg = bot.send_photo(ADMIN_ID, file_id,
        caption=(
            f"🔷 <b>رسید جدید — TRX</b>\n\n"
            f"👤 @{uname}  |  <code>{msg.from_user.id}</code>\n"
            f"🕐 {now_str()}\n\n"
            f"📦 {plan['label']}\n🔢 {qty} سرویس\n🏷️ نام‌ها:\n{names_t}\n\n"
            f"💰 {fmt(total)} تومان\n🔷 {trx_str}"
        ), reply_markup=adm_kb
    )
    with get_db() as conn:
        conn.execute("INSERT INTO receipts(user_id,order_id,file_id,receipt_type,status,admin_msg_id) VALUES(?,?,?,?,?,?)",
                     (msg.from_user.id, order_id, file_id, "purchase_crypto", "pending", adm_msg.message_id))
        conn.commit()
    clear_state(msg.from_user.id)
    bot.send_message(msg.chat.id,
        "📥 <b>رسید TRX دریافت شد!</b> ✅\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏳ <b>در حال بررسی تراکنش...</b>\n\n"
        "📌 پس از تایید، کانفیگ ارسال می‌شود.\n\n"
        f"⏱️ زمان بررسی: کمتر از ۳۰ دقیقه\n\n"
        f"❓ سوال؟ @{SUPPORT_USERNAME}"
    )

# ─────────────────────────────────────────────
#  ADMIN: APPROVE / REJECT
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_ok_"))
def cb_admin_approve(call):
    if call.from_user.id != ADMIN_ID: return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    order_id = int(call.data[7:]); bot.answer_callback_query(call.id)
    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    qty = order["quantity"] if order else 1
    set_state(ADMIN_ID, step="adm_config", order_id=order_id, configs=[], subs=[], expected_qty=qty)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ پایان و ارسال (/done)", callback_data=f"adm_done_{order_id}"))
    bot.send_message(call.message.chat.id,
        f"✅ <b>تایید سفارش #{order_id}</b>\n\n"
        f"🔢 تعداد سرویس: <b>{qty}</b> عدد\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 <b>مرحله ۱:</b> کانفیگ سرویس اول را ارسال کنید\n"
        "📡 <b>مرحله ۲:</b> ساب‌لینک آن را ارسال کنید\n"
        "🔁 این کار را برای هر سرویس تکرار کنید\n"
        "✅ در پایان /done بفرستید یا دکمه زیر را بزنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_done_"))
def cb_adm_done(call):
    if call.from_user.id != ADMIN_ID: return
    order_id = int(call.data[9:])
    state = get_state(ADMIN_ID)
    if state.get("step") != "adm_config" or state.get("order_id") != order_id: return
    bot.answer_callback_query(call.id)
    configs = state.get("configs", []); subs = state.get("subs", [])
    if not configs:
        return bot.send_message(call.message.chat.id, "⚠️ هیچ کانفیگی ثبت نشده است.")
    _deliver_configs(order_id, configs, subs)
    _delete_receipt_admin_msg(order_id)
    clear_state(ADMIN_ID)
    bot.send_message(call.message.chat.id, f"✅ <b>{len(configs)} کانفیگ با موفقیت ارسال شد!</b>")

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_rej_"))
def cb_admin_reject(call):
    if call.from_user.id != ADMIN_ID: return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    order_id = int(call.data[8:]); bot.answer_callback_query(call.id)
    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        conn.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
        conn.execute("UPDATE receipts SET status='rejected' WHERE order_id=?", (order_id,))
        conn.commit()
    _delete_receipt_admin_msg(order_id)
    try:
        bot.send_message(order["user_id"],
            "❌ <b>رسید شما رد شد</b> 😔\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "متأسفانه رسید ارسالی شما تایید نشد.\n\n"
            "📌 <b>دلایل احتمالی:</b>\n\n"
            "   🔹 رسید ناخوانا یا ناقص\n"
            "   🔹 مبلغ واریزی اشتباه\n"
            "   🔹 رسید قدیمی یا تکراری\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📞 برای پیگیری: @{SUPPORT_USERNAME}"
        )
    except Exception: pass
    bot.send_message(call.message.chat.id, f"❌ سفارش #{order_id} رد شد.")

def _delete_receipt_admin_msg(order_id):
    """پاک کردن پیام رسید ادمین بعد از تایید یا رد"""
    try:
        with get_db() as conn:
            r = conn.execute("SELECT admin_msg_id FROM receipts WHERE order_id=?", (order_id,)).fetchone()
        if r and r["admin_msg_id"]:
            safe_delete(ADMIN_ID, r["admin_msg_id"])
    except Exception: pass

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_config")
def adm_receive_config(msg):
    if msg.text and msg.text.strip() == "/done":
        state = get_state(ADMIN_ID); order_id = state["order_id"]
        configs = state.get("configs", []); subs = state.get("subs", [])
        if not configs: return bot.send_message(msg.chat.id, "⚠️ هیچ کانفیگی ثبت نشده.")
        _deliver_configs(order_id, configs, subs)
        _delete_receipt_admin_msg(order_id)
        clear_state(ADMIN_ID)
        return bot.send_message(msg.chat.id, f"✅ <b>{len(configs)} کانفیگ ارسال شد.</b>")
    state = get_state(ADMIN_ID); configs = state.get("configs", []); subs = state.get("subs", [])
    text = (msg.text or "").strip()
    if not text: return bot.send_message(msg.chat.id, "⚠️ متن خالی است.")
    if len(configs) == len(subs):
        configs.append(text); set_state(ADMIN_ID, configs=configs)
        bot.send_message(msg.chat.id,
            f"✅ <b>کانفیگ {len(configs)} ثبت شد.</b>\n\n"
            "📡 حالا ساب‌لینک این کانفیگ را ارسال کنید:\n"
            "<i>(اگر ساب‌لینک ندارد یک خط تیره - بفرستید)</i>"
        )
    else:
        sub_text = text if text != "-" else ""
        subs.append(sub_text); set_state(ADMIN_ID, subs=subs)
        expected = state.get("expected_qty", 999)
        if len(configs) >= expected:
            # همه سرویس‌ها ثبت شدن، ارسال خودکار
            order_id = state["order_id"]
            _deliver_configs(order_id, configs, subs)
            _delete_receipt_admin_msg(order_id)
            clear_state(ADMIN_ID)
            bot.send_message(msg.chat.id, f"✅ <b>همه {len(configs)} کانفیگ ارسال شد!</b>")
        else:
            bot.send_message(msg.chat.id,
                f"✅ <b>ساب‌لینک {len(subs)} ثبت شد.</b>\n\n"
                f"📋 کانفیگ بعدی را ارسال کنید (سرویس {len(configs)+1}):"
            )

def _deliver_configs(order_id, configs, subs):
    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        svc_rows = conn.execute("SELECT * FROM order_services WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
    if not order or not svc_rows: return
    plan = PLANS.get(order["plan_key"], {"gb":"?", "days":"?"}); user_id = order["user_id"]
    for i, svc in enumerate(svc_rows):
        cfg = configs[i] if i < len(configs) else "---"
        sub = subs[i]    if i < len(subs)    else ""
        try:
            with get_db() as conn:
                conn.execute("UPDATE order_services SET config_text=?, sub_link=? WHERE id=?", (cfg, sub, svc["id"]))
                conn.commit()
        except Exception as e:
            print(f"[deliver] {e}"); continue

        activation_time = datetime.now().strftime("%Y/%m/%d — %H:%M")
        safe_cfg = html_lib.escape(cfg); sub_is_url = sub.startswith("http")

        qr_buf = make_qr_bytes(sub if sub else cfg)
        if qr_buf:
            try: bot.send_photo(user_id, qr_buf)
            except Exception as e: print(f"[QR] {e}")

        kb = types.InlineKeyboardMarkup(row_width=1)
        if sub_is_url:
            kb.add(types.InlineKeyboardButton("🔗 اتصال با ساب‌لینک", url=sub))
        kb.add(types.InlineKeyboardButton("✏️ تغییر نام سرویس", callback_data=f"rename_{svc['id']}"))

        full_text = (
            f"🎉 <b>سرویس شما فعال شد!</b> 🚀\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🏷️ <b>نام سرویس:</b>  {html_lib.escape(svc['service_name'])}\n"
            f"📊 <b>حجم:</b>  {plan['gb']} گیگابایت\n"
            f"📅 <b>اعتبار:</b>  {plan['days']} روز\n"
            f"🕐 <b>فعال‌سازی:</b>  {activation_time}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔐 <b>کانفیگ</b>  👆 <i>بزنید تا کپی شود</i>\n\n"
            f"<code>{safe_cfg}</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        if sub:
            full_text += (
                "🔗 <b>ساب‌لینک</b>  👆 <i>بزنید تا کپی شود</i>\n\n"
                f"<code>{html_lib.escape(sub)}</code>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            )
        full_text += (
            "📌 <b>راهنما:</b>\n\n"
            "  1️⃣  کانفیگ را کپی کرده در اپ ایمپورت کنید\n"
            "  2️⃣  یا دکمه ساب‌لینک زیر را بزنید\n"
            "  3️⃣  یا QR کد بالایی را اسکن کنید\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆘 پشتیبانی: @{SUPPORT_USERNAME}\n"
            "💙 از اعتماد شما سپاسگزاریم! 🌟"
        )
        try: bot.send_message(user_id, full_text, reply_markup=kb)
        except Exception as e: print(f"[deliver] {e}")

    with get_db() as conn:
        conn.execute("UPDATE orders SET status='delivered' WHERE id=?", (order_id,))
        conn.execute("UPDATE receipts SET status='approved' WHERE order_id=?", (order_id,))
        conn.commit()

# ── Rename ──────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("rename_"))
def cb_rename(call):
    svc_id = int(call.data[7:]); caller_id = call.from_user.id
    with get_db() as conn:
        svc = conn.execute("SELECT * FROM order_services WHERE id=?", (svc_id,)).fetchone()
    if not svc or int(svc["user_id"]) != int(caller_id):
        return bot.answer_callback_query(call.id, "سرویس یافت نشد", show_alert=True)
    bot.answer_callback_query(call.id)
    set_state(caller_id, step="rename_service", svc_id=svc_id)
    bot.send_message(call.message.chat.id,
        f"✏️ <b>تغییر نام سرویس</b>\n\n"
        f"نام فعلی: <b>{html_lib.escape(svc['service_name'])}</b>\n\n"
        "👇 نام جدید را وارد کنید:"
    )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "rename_service")
def rename_service(msg):
    state = get_state(msg.from_user.id); svc_id = state.get("svc_id")
    if not svc_id: return clear_state(msg.from_user.id)
    name = (msg.text or "").strip()[:30]
    if not name: return bot.send_message(msg.chat.id, "⚠️ نام نمی‌تواند خالی باشد.")
    with get_db() as conn:
        svc = conn.execute("SELECT user_id FROM order_services WHERE id=?", (svc_id,)).fetchone()
        if not svc or int(svc["user_id"]) != int(msg.from_user.id):
            clear_state(msg.from_user.id); return bot.send_message(msg.chat.id, "❌ دسترسی ندارید.")
        conn.execute("UPDATE order_services SET service_name=? WHERE id=?", (name, svc_id)); conn.commit()
    clear_state(msg.from_user.id)
    bot.send_message(msg.chat.id, f"✅ نام سرویس به <b>{name}</b> تغییر یافت! ✨")

# ─────────────────────────────────────────────
#  📦 MY SERVICES
# ─────────────────────────────────────────────
def _show_my_services(chat_id, user_id):
    with get_db() as conn:
        svcs = [s for s in conn.execute("SELECT * FROM order_services WHERE config_text IS NOT NULL ORDER BY id DESC").fetchall()
                if int(s["user_id"]) == int(user_id)]
    if not svcs:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("🛒 رفتن به فروشگاه", callback_data="menu_shop"))
        kb.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))
        return bot.send_message(chat_id,
            "📦 <b>سرویس‌های من</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "❌ <b>سرویس فعالی ندارید.</b>\n\n"
            "💡 از فروشگاه اقدام به خرید کنید.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=kb
        )
    kb = types.InlineKeyboardMarkup(row_width=1)
    for svc in svcs:
        plan = PLANS.get(svc["plan_key"], {})
        kb.add(types.InlineKeyboardButton(f"📦 {svc['service_name']}  |  {plan.get('gb','?')}GB", callback_data=f"vs_{svc['id']}"))
    kb.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))
    bot.send_message(chat_id,
        f"📦 <b>سرویس‌های من</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔢 <b>{len(svcs)}</b> سرویس فعال\n\n"
        "👇 برای مشاهده جزئیات روی سرویس بزنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("vs_"))
def cb_view_svc(call):
    svc_id = int(call.data[3:])
    with get_db() as conn:
        svc = conn.execute("SELECT * FROM order_services WHERE id=? AND user_id=?", (svc_id, call.from_user.id)).fetchone()
    if not svc: return bot.answer_callback_query(call.id, "سرویس یافت نشد", show_alert=True)
    plan = PLANS.get(svc["plan_key"], {}); bot.answer_callback_query(call.id)
    sub = svc["sub_link"] or ""
    kb = types.InlineKeyboardMarkup(row_width=1)
    if sub.startswith("http"): kb.add(types.InlineKeyboardButton("🔗 اتصال با ساب‌لینک", url=sub))
    kb.add(types.InlineKeyboardButton("✏️ تغییر نام", callback_data=f"rename_{svc['id']}"))
    kb.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="menu_services"))
    safe_delete(call.message.chat.id, call.message.message_id)
    text = (
        f"📦 <b>جزئیات سرویس</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏷️ <b>نام:</b>  {svc['service_name']}\n"
        f"📊 <b>حجم:</b>  {plan.get('gb','?')} GB\n"
        f"📅 <b>مدت:</b>  {plan.get('days','?')} روز\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔐 <b>کانفیگ</b>  👆 <i>بزنید تا کپی شود</i>\n\n"
        f"<code>{svc['config_text']}</code>\n\n"
    )
    if sub:
        text += (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔗 <b>ساب‌لینک</b>  👆 <i>بزنید تا کپی شود</i>\n\n"
            f"<code>{html_lib.escape(sub)}</code>\n\n"
        )
    bot.send_message(call.message.chat.id, text, reply_markup=kb)

# ─────────────────────────────────────────────
#  💰 WALLET
# ─────────────────────────────────────────────
def _show_wallet(chat_id, user_id):
    wallet = get_wallet(user_id)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("💳 شارژ کیف پول", callback_data="wallet_charge"),
        types.InlineKeyboardButton("🔙 بازگشت",        callback_data="back_main"),
    )
    bot.send_message(chat_id,
        "💰 <b>کیف پول</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✨ <b>موجودی:</b> {fmt(wallet)} تومان\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 برای شارژ دکمه زیر را بزنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "wallet_charge")
def cb_wallet_charge(call):
    if is_offline_for(call.from_user.id): return bot.answer_callback_query(call.id, "⚠️ ربات خاموش است.", show_alert=True)
    bot.answer_callback_query(call.id)
    set_state(call.from_user.id, step="wallet_amount")
    bot.send_message(call.message.chat.id,
        "💳 <b>شارژ کیف پول</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 حداقل: <b>۵۰,۰۰۰ تومان</b>\n\n"
        "👇 مبلغ مورد نظر را وارد کنید:\n"
        "<i>(مثال: 200000)</i>"
    )

@bot.message_handler(func=lambda m: get_state(m.from_user.id).get("step") == "wallet_amount")
def wallet_amount(msg):
    if is_offline_for(msg.from_user.id): return bot.send_message(msg.chat.id, OFFLINE_MSG)
    try:
        amount = int(msg.text.strip().replace(",","").replace("٬",""))
        if amount < 50_000: raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ مبلغ معتبر (حداقل ۵۰,۰۰۰ تومان) وارد کنید.")
    set_state(msg.from_user.id, step="wallet_receipt", wallet_amount=amount)
    bot.send_message(msg.chat.id,
        f"💳 <b>شارژ کیف پول — پرداخت</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 <b>مبلغ:</b> {fmt(amount)} تومان\n\n"
        "🏦 <b>اطلاعات حساب:</b>\n\n"
        f"  💳 شماره کارت:\n  <code>{CARD_NUMBER}</code>\n\n"
        f"  👤 به نام: <b>{CARD_OWNER}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 <b>تصویر رسید را ارسال کنید:</b>"
    )

def _handle_wallet_receipt(msg):
    state = get_state(msg.from_user.id); amount = state["wallet_amount"]
    file_id = msg.photo[-1].file_id
    u = get_user(msg.from_user.id); uname = u["username"] or u["full_name"] or str(msg.from_user.id)
    with get_db() as conn:
        cur = conn.execute("INSERT INTO wallet_requests(user_id,amount) VALUES(?,?)", (msg.from_user.id, amount))
        req_id = cur.lastrowid; conn.commit()
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ تایید شارژ", callback_data=f"wadm_ok_{req_id}_{msg.from_user.id}_{amount}"),
        types.InlineKeyboardButton("❌ رد",          callback_data=f"wadm_rej_{req_id}_{msg.from_user.id}"),
    )
    adm_msg = bot.send_photo(ADMIN_ID, file_id,
        caption=(
            f"💰 <b>درخواست شارژ کیف پول</b>\n\n"
            f"👤 @{uname}  |  <code>{msg.from_user.id}</code>\n"
            f"🕐 {now_str()}\n\n"
            f"💰 {fmt(amount)} تومان"
        ),
        reply_markup=kb
    )
    with get_db() as conn:
        conn.execute("UPDATE wallet_requests SET admin_msg_id=? WHERE id=?", (adm_msg.message_id, req_id)); conn.commit()
    clear_state(msg.from_user.id)
    bot.send_message(msg.chat.id,
        "📥 <b>رسید دریافت شد!</b> ✅\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏳ منتظر تایید ادمین باشید.\n\n"
        f"⏱️ زمان بررسی: کمتر از ۳۰ دقیقه\n\n"
        f"❓ پیگیری: @{SUPPORT_USERNAME}"
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("wadm_ok_"))
def cb_wallet_approve(call):
    if call.from_user.id != ADMIN_ID: return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    parts = call.data.split("_"); req_id = int(parts[2]); user_id = int(parts[3]); amount = int(parts[4])
    bot.answer_callback_query(call.id)
    add_wallet(user_id, amount)
    with get_db() as conn:
        conn.execute("UPDATE wallet_requests SET status='approved' WHERE id=?", (req_id,)); conn.commit()
    safe_delete(call.message.chat.id, call.message.message_id)
    new_bal = get_wallet(user_id)
    try:
        bot.send_message(user_id,
            f"✅ <b>کیف پول شارژ شد!</b> 🎉\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 <b>مبلغ شارژ:</b> {fmt(amount)} تومان\n"
            f"💎 <b>موجودی جدید:</b> {fmt(new_bal)} تومان\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🛒 از فروشگاه خرید کنید!"
        )
    except Exception: pass
    bot.send_message(call.message.chat.id, f"✅ {fmt(amount)} تومان به {user_id} اضافه شد.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("wadm_rej_"))
def cb_wallet_reject(call):
    if call.from_user.id != ADMIN_ID: return bot.answer_callback_query(call.id, "دسترسی ندارید", show_alert=True)
    parts = call.data.split("_"); req_id = int(parts[2]); user_id = int(parts[3])
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        conn.execute("UPDATE wallet_requests SET status='rejected' WHERE id=?", (req_id,)); conn.commit()
    safe_delete(call.message.chat.id, call.message.message_id)
    try:
        bot.send_message(user_id,
            f"❌ <b>درخواست شارژ رد شد.</b>\n\n"
            f"📞 پیگیری: @{SUPPORT_USERNAME}"
        )
    except Exception: pass
    bot.send_message(call.message.chat.id, "❌ درخواست شارژ رد شد.")

# ─────────────────────────────────────────────
#  👥 REFERRAL
# ─────────────────────────────────────────────
def _show_referral(chat_id, user_id):
    u = get_user(user_id); bot_info = bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{u['referral_code']}"
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) as c FROM users WHERE referred_by=?", (user_id,)).fetchone()["c"]
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="back_main"))
    bot.send_message(chat_id,
        "👥 <b>دعوت دوستان</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 <b>پاداش هر دعوت:</b> {fmt(REFERRAL_BONUS)} تومان\n"
        f"👤 <b>دعوت‌های موفق:</b> {count} نفر\n"
        f"💸 <b>درآمد کسب‌شده:</b> {fmt(count*REFERRAL_BONUS)} تومان\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 <b>نحوه کار:</b>\n\n"
        "   1️⃣  لینک اختصاصی خود را کپی کنید\n"
        "   2️⃣  برای دوستان ارسال کنید\n"
        "   3️⃣  بعد از ثبت‌نام دوستان، پاداش دریافت کنید\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔗 <b>لینک اختصاصی شما:</b>\n\n"
        f"<code>{ref_link}</code>",
        reply_markup=kb
    )

# ─────────────────────────────────────────────
#  ⚙️ ADMIN PANEL (in-chat)
# ─────────────────────────────────────────────
def _show_admin_panel(chat_id):
    bot_status = "🟢 روشن" if BOT_ONLINE else "🔴 خاموش"
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("👥 لیست کاربران",    callback_data="ap_users_0"),
        types.InlineKeyboardButton("🔍 جستجوی کاربر",    callback_data="ap_search"),
        types.InlineKeyboardButton("📊 آمار کلی",         callback_data="ap_stats"),
        types.InlineKeyboardButton("📋 رسیدهای معلق",    callback_data="ap_pending"),
        types.InlineKeyboardButton("🤝 درخواست‌های نمایندگی", callback_data="ap_agency"),
        types.InlineKeyboardButton("⚙️ تنظیمات",          callback_data="ap_settings"),
    )
    bot.send_message(chat_id,
        f"⚙️ <b>پنل ادمین ViraNet</b>\n\n"
        f"📡 وضعیت ربات: <b>{bot_status}</b>\n\n"
        "👇 گزینه مورد نظر را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "ap_agency" and c.from_user.id == ADMIN_ID)
def cb_ap_agency(call):
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        reqs = conn.execute(
            "SELECT ar.*,u.username,u.full_name FROM agency_requests ar "
            "JOIN users u ON ar.user_id=u.user_id "
            "WHERE ar.status='pending' ORDER BY ar.created_at DESC"
        ).fetchall()
    if not reqs:
        return bot.send_message(call.message.chat.id, "✅ هیچ درخواست نمایندگی معلقی وجود ندارد.")
    for r in reqs:
        uname = r["username"] or r["full_name"] or str(r["user_id"])
        adm_kb = types.InlineKeyboardMarkup(row_width=2)
        adm_kb.add(
            types.InlineKeyboardButton("✅ تایید", callback_data=f"agency_ok_{r['id']}"),
            types.InlineKeyboardButton("❌ رد",    callback_data=f"agency_rej_{r['id']}"),
        )
        bot.send_message(call.message.chat.id,
            f"🤝 <b>درخواست نمایندگی #{r['id']}</b>\n\n"
            f"👤 @{uname} | <code>{r['user_id']}</code>\n"
            f"🕐 {r['created_at'][:16]}\n\n"
            f"🤖 توکن: <code>{r['bot_token']}</code>",
            reply_markup=adm_kb
        )

@bot.callback_query_handler(func=lambda c: c.data == "ap_settings" and c.from_user.id == ADMIN_ID)
def cb_ap_settings(call):
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("💳 اطلاعات کارت", callback_data="ap_card_settings"),
        types.InlineKeyboardButton("📦 محصولات",       callback_data="ap_products"),
        types.InlineKeyboardButton("🔙 پنل ادمین",     callback_data="menu_admin"),
    )
    bot.send_message(call.message.chat.id, "⚙️ <b>تنظیمات</b>\n\nگزینه مورد نظر را انتخاب کنید:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "ap_card_settings" and c.from_user.id == ADMIN_ID)
def cb_ap_card_settings(call):
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("💳 تغییر شماره کارت",    callback_data="ap_change_card"),
        types.InlineKeyboardButton("👤 تغییر نام صاحب کارت", callback_data="ap_change_owner"),
        types.InlineKeyboardButton("🔷 تغییر آدرس ولت TRX",  callback_data="ap_change_wallet"),
        types.InlineKeyboardButton("🔙 تنظیمات",              callback_data="ap_settings"),
    )
    bot.send_message(call.message.chat.id,
        f"💳 <b>اطلاعات پرداخت</b>\n\n"
        f"شماره کارت: <code>{CARD_NUMBER}</code>\n"
        f"نام: <b>{CARD_OWNER}</b>\n"
        f"آدرس TRX: <code>{TRX_WALLET}</code>\n\n"
        "👇 گزینه مورد نظر را انتخاب کنید:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data == "ap_change_card" and c.from_user.id == ADMIN_ID)
def cb_ap_change_card(call):
    bot.answer_callback_query(call.id); set_state(ADMIN_ID, step="adm_change_card")
    bot.send_message(call.message.chat.id, f"💳 شماره کارت فعلی: <code>{CARD_NUMBER}</code>\n\n👇 شماره کارت جدید را ارسال کنید:")

@bot.callback_query_handler(func=lambda c: c.data == "ap_change_owner" and c.from_user.id == ADMIN_ID)
def cb_ap_change_owner(call):
    bot.answer_callback_query(call.id); set_state(ADMIN_ID, step="adm_change_owner")
    bot.send_message(call.message.chat.id, f"👤 نام فعلی: <b>{CARD_OWNER}</b>\n\n👇 نام جدید را ارسال کنید:")

@bot.callback_query_handler(func=lambda c: c.data == "ap_change_wallet" and c.from_user.id == ADMIN_ID)
def cb_ap_change_wallet(call):
    bot.answer_callback_query(call.id); set_state(ADMIN_ID, step="adm_change_wallet")
    bot.send_message(call.message.chat.id, f"🔷 آدرس فعلی: <code>{TRX_WALLET}</code>\n\n👇 آدرس جدید را ارسال کنید:")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") in ("adm_change_card","adm_change_owner","adm_change_wallet"))
def adm_change_settings(msg):
    global CARD_NUMBER, CARD_OWNER, TRX_WALLET
    step = get_state(ADMIN_ID)["step"]; value = (msg.text or "").strip()
    if not value: return bot.send_message(msg.chat.id, "⚠️ مقدار خالی!")
    if step == "adm_change_card":
        CARD_NUMBER = value; save_setting("card_number", value)
        bot.send_message(msg.chat.id, f"✅ شماره کارت به <code>{value}</code> تغییر یافت.")
    elif step == "adm_change_owner":
        CARD_OWNER = value; save_setting("card_owner", value)
        bot.send_message(msg.chat.id, f"✅ نام به <b>{value}</b> تغییر یافت.")
    elif step == "adm_change_wallet":
        TRX_WALLET = value; save_setting("trx_wallet", value)
        bot.send_message(msg.chat.id, f"✅ آدرس ولت به <code>{value}</code> تغییر یافت.")
    clear_state(ADMIN_ID)

# ── Products ─────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "ap_products" and c.from_user.id == ADMIN_ID)
def cb_ap_products(call):
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        prods = conn.execute("SELECT * FROM products ORDER BY price").fetchall()
    if not prods:
        return bot.send_message(call.message.chat.id, "محصولی وجود ندارد.")
    kb = types.InlineKeyboardMarkup(row_width=1)
    for p in prods:
        status = "✅" if p["active"] else "❌"
        kb.add(types.InlineKeyboardButton(f"{status} {p['label']} | {fmt(p['price'])}ت", callback_data=f"ap_prod_{p['id']}"))
    kb.add(types.InlineKeyboardButton("➕ محصول جدید", callback_data="ap_product_add"))
    kb.add(types.InlineKeyboardButton("🔙 تنظیمات",    callback_data="ap_settings"))
    bot.send_message(call.message.chat.id, "📦 <b>محصولات</b>\n\nبرای ویرایش روی محصول بزنید:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_prod_") and c.from_user.id == ADMIN_ID)
def cb_ap_prod(call):
    bot.answer_callback_query(call.id)
    prod_id = int(call.data[8:])
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (prod_id,)).fetchone()
    if not p: return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("💰 تغییر قیمت", callback_data=f"ap_chprice_{prod_id}"),
        types.InlineKeyboardButton("✏️ تغییر نام",  callback_data=f"ap_chname_{prod_id}"),
    )
    toggle = "❌ غیرفعال" if p["active"] else "✅ فعال"
    kb.add(types.InlineKeyboardButton(toggle, callback_data=f"ap_toggle_{prod_id}"))
    kb.add(types.InlineKeyboardButton("🔙 محصولات", callback_data="ap_products"))
    bot.send_message(call.message.chat.id,
        f"📦 <b>ویرایش محصول</b>\n\n"
        f"نام: {p['label']}\n"
        f"حجم: {p['gb']}GB | مدت: {p['days']} روز\n"
        f"قیمت: {fmt(p['price'])} تومان\n"
        f"وضعیت: {'✅ فعال' if p['active'] else '❌ غیرفعال'}",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_toggle_") and c.from_user.id == ADMIN_ID)
def cb_ap_toggle(call):
    prod_id = int(call.data[10:]); bot.answer_callback_query(call.id)
    with get_db() as conn:
        p = conn.execute("SELECT active FROM products WHERE id=?", (prod_id,)).fetchone()
        new_status = 0 if p["active"] else 1
        conn.execute("UPDATE products SET active=? WHERE id=?", (new_status, prod_id)); conn.commit()
    reload_plans()
    bot.send_message(call.message.chat.id, f"✅ وضعیت محصول {'فعال' if new_status else 'غیرفعال'} شد.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_chprice_") and c.from_user.id == ADMIN_ID)
def cb_ap_chprice(call):
    prod_id = int(call.data[11:]); bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_change_price", prod_id=prod_id)
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (prod_id,)).fetchone()
    bot.send_message(call.message.chat.id,
        f"💰 قیمت فعلی: <b>{fmt(p['price'])} تومان</b>\n\n"
        "👇 قیمت جدید را وارد کنید:"
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_chname_") and c.from_user.id == ADMIN_ID)
def cb_ap_chname(call):
    prod_id = int(call.data[10:]); bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_change_pname", prod_id=prod_id)
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (prod_id,)).fetchone()
    bot.send_message(call.message.chat.id,
        f"نام فعلی: <b>{p['label']}</b>\n\n"
        "👇 نام جدید را وارد کنید:"
    )

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_change_price")
def adm_change_price(msg):
    try:
        new_price = int((msg.text or "").strip().replace(",","").replace("٬",""))
        if new_price <= 0: raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ قیمت معتبر وارد کنید.")
    prod_id = get_state(ADMIN_ID)["prod_id"]
    with get_db() as conn:
        p = conn.execute("SELECT * FROM products WHERE id=?", (prod_id,)).fetchone()
        new_label = _make_label(p["gb"], p["days"], new_price)
        conn.execute("UPDATE products SET price=?, label=? WHERE id=?", (new_price, new_label, prod_id)); conn.commit()
    reload_plans(); clear_state(ADMIN_ID)
    bot.send_message(msg.chat.id, f"✅ قیمت تغییر یافت!\n\n{new_label}")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_change_pname")
def adm_change_pname(msg):
    new_label = (msg.text or "").strip()
    if len(new_label) < 3: return bot.send_message(msg.chat.id, "⚠️ نام باید حداقل ۳ کاراکتر باشد.")
    prod_id = get_state(ADMIN_ID)["prod_id"]
    with get_db() as conn:
        conn.execute("UPDATE products SET label=? WHERE id=?", (new_label, prod_id)); conn.commit()
    reload_plans(); clear_state(ADMIN_ID)
    bot.send_message(msg.chat.id, f"✅ نام محصول تغییر یافت!\n\n<b>{new_label}</b>")

@bot.callback_query_handler(func=lambda c: c.data == "ap_product_add" and c.from_user.id == ADMIN_ID)
def cb_ap_product_add(call):
    bot.answer_callback_query(call.id); set_state(ADMIN_ID, step="adm_add_product_gb")
    bot.send_message(call.message.chat.id, "➕ <b>محصول جدید</b>\n\n📊 حجم (گیگابایت):")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_add_product_gb")
def adm_add_gb(msg):
    try:
        gb = int((msg.text or "").strip())
        if gb <= 0: raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ عدد معتبر وارد کنید.")
    set_state(ADMIN_ID, step="adm_add_product_days", new_gb=gb)
    bot.send_message(msg.chat.id, f"✅ حجم: {gb}GB\n\n📅 مدت (روز):")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_add_product_days")
def adm_add_days(msg):
    try:
        days = int((msg.text or "").strip())
        if days <= 0: raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ عدد معتبر وارد کنید.")
    set_state(ADMIN_ID, step="adm_add_product_price", new_days=days)
    bot.send_message(msg.chat.id, f"✅ مدت: {days} روز\n\n💰 قیمت (تومان):")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_add_product_price")
def adm_add_price(msg):
    try:
        price = int((msg.text or "").strip().replace(",","").replace("٬",""))
        if price <= 0: raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ قیمت معتبر وارد کنید.")
    state = get_state(ADMIN_ID); gb = state["new_gb"]; days = state["new_days"]
    label    = _make_label(gb, days, price)
    plan_key = f"prod_{int(time.time())}"
    with get_db() as conn:
        conn.execute("INSERT INTO products(plan_key,label,gb,days,price) VALUES(?,?,?,?,?)", (plan_key, label, gb, days, price)); conn.commit()
    reload_plans(); clear_state(ADMIN_ID)
    bot.send_message(msg.chat.id, f"✅ <b>محصول اضافه شد!</b>\n\n{label}")

# ── User management ─────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "ap_stats" and c.from_user.id == ADMIN_ID)
def cb_ap_stats(call):
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        uc = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        pc = conn.execute("SELECT COUNT(*) as c FROM receipts WHERE status='pending'").fetchone()["c"]
        ts = conn.execute("SELECT SUM(total_price) as s FROM orders WHERE status='delivered'").fetchone()["s"] or 0
        ac = conn.execute("SELECT COUNT(*) as c FROM agency_requests WHERE status='pending'").fetchone()["c"]
    bot.send_message(call.message.chat.id,
        f"📊 <b>آمار کلی ViraNet</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 <b>کاربران:</b> {uc}\n"
        f"📥 <b>رسید معلق:</b> {pc}\n"
        f"🤝 <b>درخواست نمایندگی:</b> {ac}\n"
        f"💰 <b>فروش کل:</b> {fmt(ts)} تومان\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_users_") and c.from_user.id == ADMIN_ID)
def cb_ap_users(call):
    bot.answer_callback_query(call.id)
    page = int(call.data[9:]); limit = 8; offset = page * limit
    with get_db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        total = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    if not users: return bot.send_message(call.message.chat.id, "کاربری وجود ندارد.")
    kb = types.InlineKeyboardMarkup(row_width=1)
    for u in users:
        label = f"{'⛔ ' if u['is_banned'] else '✅ '}@{u['username'] or u['full_name'] or u['user_id']}  |  {fmt(u['wallet'])}ت"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"ap_user_{u['user_id']}"))
    nav = []
    if page > 0: nav.append(types.InlineKeyboardButton("◀️ قبلی", callback_data=f"ap_users_{page-1}"))
    if offset + limit < total: nav.append(types.InlineKeyboardButton("بعدی ▶️", callback_data=f"ap_users_{page+1}"))
    if nav: kb.add(*nav)
    kb.add(types.InlineKeyboardButton("🔙 پنل ادمین", callback_data="menu_admin"))
    bot.send_message(call.message.chat.id, f"👥 <b>کاربران</b> ({total}) — صفحه {page+1}:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "ap_search" and c.from_user.id == ADMIN_ID)
def cb_ap_search(call):
    bot.answer_callback_query(call.id); set_state(ADMIN_ID, step="adm_search")
    bot.send_message(call.message.chat.id, "🔍 آیدی یا یوزرنیم کاربر را ارسال کنید:")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") == "adm_search")
def adm_search(msg):
    q = msg.text.strip().lstrip("@")
    with get_db() as conn:
        u = (conn.execute("SELECT * FROM users WHERE user_id=?", (q,)).fetchone()
             or conn.execute("SELECT * FROM users WHERE username LIKE ?", (f"%{q}%",)).fetchone()
             or conn.execute("SELECT * FROM users WHERE full_name LIKE ?", (f"%{q}%",)).fetchone())
    if not u: return bot.send_message(msg.chat.id, "❌ کاربر یافت نشد.")
    clear_state(ADMIN_ID); _show_user_detail(msg.chat.id, u["user_id"])

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_user_") and c.from_user.id == ADMIN_ID)
def cb_ap_user(call):
    bot.answer_callback_query(call.id); _show_user_detail(call.message.chat.id, int(call.data[8:]))

def _show_user_detail(chat_id, uid):
    u = get_user(uid)
    if not u: return bot.send_message(chat_id, "❌ کاربر یافت نشد.")
    with get_db() as conn:
        oc = conn.execute("SELECT COUNT(*) as c FROM orders WHERE user_id=?", (uid,)).fetchone()["c"]
        ot = conn.execute("SELECT SUM(total_price) as s FROM orders WHERE user_id=? AND status='delivered'", (uid,)).fetchone()["s"] or 0
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ شارژ",   callback_data=f"ap_add_{uid}"),
        types.InlineKeyboardButton("➖ کسر",    callback_data=f"ap_sub_{uid}"),
    )
    kb.add(types.InlineKeyboardButton("⛔ بن" if not u["is_banned"] else "✅ رفع بن", callback_data=f"ap_ban_{uid}"))
    kb.add(types.InlineKeyboardButton("🔙 لیست کاربران", callback_data="ap_users_0"))
    bot.send_message(chat_id,
        f"👤 <b>اطلاعات کاربر</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 <code>{uid}</code>\n"
        f"📛 {u['full_name'] or '---'}\n"
        f"👤 @{u['username'] or '---'}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 موجودی: <b>{fmt(u['wallet'])} ت</b>\n"
        f"🛒 سفارش: {oc}  |  💸 خرید: {fmt(ot)} ت\n"
        f"⛔ {'🔴 مسدود' if u['is_banned'] else '🟢 فعال'}\n"
        f"📅 {u['joined_at'][:16]}",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_add_") and c.from_user.id == ADMIN_ID)
def cb_ap_add(call):
    uid = int(call.data[7:]); bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_add_wallet", target_uid=uid)
    bot.send_message(call.message.chat.id, f"💰 مبلغ شارژ برای <code>{uid}</code>:")

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_sub_") and c.from_user.id == ADMIN_ID)
def cb_ap_sub(call):
    uid = int(call.data[7:]); bot.answer_callback_query(call.id)
    set_state(ADMIN_ID, step="adm_sub_wallet", target_uid=uid)
    bot.send_message(call.message.chat.id, f"➖ مبلغ کسر از <code>{uid}</code>:")

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and get_state(ADMIN_ID).get("step") in ("adm_add_wallet","adm_sub_wallet"))
def adm_modify_wallet(msg):
    try:
        amount = int(msg.text.strip().replace(",",""))
        if amount <= 0: raise ValueError
    except ValueError:
        return bot.send_message(msg.chat.id, "⚠️ مبلغ معتبر وارد کنید.")
    state = get_state(ADMIN_ID); uid = state["target_uid"]; action = state["step"]
    if action == "adm_add_wallet":
        add_wallet(uid, amount)
        try: bot.send_message(uid, f"✅ {fmt(amount)} تومان به کیف پولتان اضافه شد!\n💎 موجودی: {fmt(get_wallet(uid))} تومان")
        except Exception: pass
        bot.send_message(msg.chat.id, f"✅ {fmt(amount)} تومان اضافه شد. موجودی: {fmt(get_wallet(uid))}")
    else:
        dec = min(amount, get_wallet(uid)); deduct_wallet(uid, dec)
        bot.send_message(msg.chat.id, f"➖ {fmt(dec)} تومان کسر شد. موجودی: {fmt(get_wallet(uid))}")
    clear_state(ADMIN_ID)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ap_ban_") and c.from_user.id == ADMIN_ID)
def cb_ap_ban(call):
    uid = int(call.data[7:]); bot.answer_callback_query(call.id)
    with get_db() as conn:
        u = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
        ns = 0 if u["is_banned"] else 1
        conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (ns, uid)); conn.commit()
    label = "🔴 مسدود" if ns else "🟢 فعال"
    bot.send_message(call.message.chat.id, f"✅ وضعیت <code>{uid}</code> → <b>{label}</b>")
    if ns:
        try: bot.send_message(uid, "⛔ حساب شما مسدود شده است.")
        except Exception: pass

@bot.callback_query_handler(func=lambda c: c.data == "ap_pending" and c.from_user.id == ADMIN_ID)
def cb_ap_pending(call):
    bot.answer_callback_query(call.id)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT r.*,u.username,u.full_name FROM receipts r JOIN users u ON r.user_id=u.user_id "
            "WHERE r.status='pending' ORDER BY r.created_at DESC LIMIT 10"
        ).fetchall()
    if not rows: return bot.send_message(call.message.chat.id, "✅ هیچ رسید معلقی وجود ندارد.")
    for r in rows:
        uname = r["username"] or r["full_name"] or str(r["user_id"])
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ تایید", callback_data=f"adm_ok_{r['order_id']}"),
            types.InlineKeyboardButton("❌ رد",    callback_data=f"adm_rej_{r['order_id']}"),
        )
        bot.send_message(call.message.chat.id,
            f"📥 <b>رسید #{r['id']}</b>\n"
            f"👤 @{uname} | <code>{r['user_id']}</code>\n"
            f"📅 {r['created_at'][:16]}\n"
            f"نوع: {r['receipt_type']}", reply_markup=kb
        )

# ─────────────────────────────────────────────
#  FALLBACK
# ─────────────────────────────────────────────
@bot.message_handler(content_types=["text"], func=lambda m: True)
def fallback(msg):
    if is_offline_for(msg.from_user.id): return bot.send_message(msg.chat.id, OFFLINE_MSG)
    u = get_user(msg.from_user.id)
    if u and u["is_banned"]: return
    if get_state(msg.from_user.id).get("step"): return
    send_main_menu(msg.chat.id, msg.from_user.id, "🏠 از منوی زیر انتخاب کنید:")

# ─────────────────────────────────────────────
#  WEBAPP HTML
# ─────────────────────────────────────────────
WEBAPP_HTML = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>ViraNet Panel</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Segoe UI',Tahoma,sans-serif;background:#0a1628;color:#e8f4fd;min-height:100vh;direction:rtl;}
.screen{display:none;min-height:100vh;}
.screen.active{display:block;}

/* ── User locked ── */
#user-locked{
  background:linear-gradient(160deg,#060e1f 0%,#0a2040 40%,#0d3060 70%,#0a2040 100%);
  display:flex;flex-direction:column;align-items:center;justify-content:flex-start;
  min-height:100vh;padding:2.5rem 1.5rem 2rem;overflow-y:auto;
}
.vn-logo{font-size:3.5rem;margin-bottom:.5rem;animation:float 3s ease-in-out infinite;}
@keyframes float{0%,100%{transform:translateY(0);}50%{transform:translateY(-8px);}}
.vn-brand{font-size:1.8rem;font-weight:800;background:linear-gradient(90deg,#4fc3f7,#81d4fa,#b3e5fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:.3rem;}
.vn-slogan{font-size:.9rem;color:#78909c;margin-bottom:2rem;letter-spacing:.03em;}
.user-card{width:100%;max-width:360px;background:linear-gradient(135deg,rgba(79,195,247,.12),rgba(33,150,243,.08));border:1px solid rgba(79,195,247,.25);border-radius:20px;padding:1.5rem;margin-bottom:1.2rem;backdrop-filter:blur(10px);}
.user-card-header{display:flex;align-items:center;gap:.8rem;margin-bottom:1.2rem;padding-bottom:1rem;border-bottom:1px solid rgba(79,195,247,.15);}
.user-avatar{width:50px;height:50px;border-radius:50%;background:linear-gradient(135deg,#1565c0,#42a5f5);display:flex;align-items:center;justify-content:center;font-size:1.4rem;flex-shrink:0;}
.user-card-name{font-size:1rem;font-weight:700;color:#e8f4fd;}
.user-card-id{font-size:.8rem;color:#78909c;}
.info-row{display:flex;justify-content:space-between;align-items:center;padding:.6rem 0;}
.info-row+.info-row{border-top:1px solid rgba(255,255,255,.05);}
.info-label{font-size:.85rem;color:#78909c;display:flex;align-items:center;gap:.4rem;}
.info-value{font-size:.95rem;font-weight:700;color:#4fc3f7;}
.feature-grid{display:grid;grid-template-columns:1fr 1fr;gap:.8rem;width:100%;max-width:360px;margin-top:.5rem;}
.feature-box{background:rgba(255,255,255,.04);border:1px solid rgba(79,195,247,.1);border-radius:14px;padding:1rem;text-align:center;}
.feature-icon{font-size:1.6rem;margin-bottom:.3rem;}
.feature-label{font-size:.75rem;color:#90caf9;line-height:1.3;}
.lock-notice{width:100%;max-width:360px;background:rgba(255,82,82,.08);border:1px solid rgba(255,82,82,.2);border-radius:14px;padding:1rem;text-align:center;margin-top:1rem;font-size:.85rem;color:#ef9a9a;}

/* ── Admin panel ── */
#admin-panel{background:#0a1628;display:flex;flex-direction:column;min-height:100vh;}
.nav{background:rgba(10,22,40,.95);backdrop-filter:blur(12px);border-bottom:1px solid rgba(79,195,247,.12);padding:.9rem 1rem;position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:.7rem;}
.nav-logo{font-size:1.3rem;}
.nav-title{font-size:1rem;font-weight:700;color:#4fc3f7;flex:1;}
.nav-badge{background:#ff5252;color:#fff;border-radius:999px;padding:.15rem .55rem;font-size:.72rem;font-weight:800;animation:badgePulse 1.5s infinite;}
@keyframes badgePulse{0%,100%{box-shadow:0 0 0 0 rgba(255,82,82,.5);}50%{box-shadow:0 0 0 5px rgba(255,82,82,0);}}
.tabs{display:flex;overflow-x:auto;padding:.6rem 1rem;gap:.5rem;scrollbar-width:none;border-bottom:1px solid rgba(79,195,247,.07);}
.tabs::-webkit-scrollbar{display:none;}
.tab{flex-shrink:0;padding:.45rem 1rem;border-radius:999px;border:1px solid rgba(79,195,247,.25);background:transparent;color:#78909c;font-size:.82rem;cursor:pointer;transition:all .2s;font-family:inherit;}
.tab.active{background:linear-gradient(135deg,#1565c0,#1976d2);color:#fff;font-weight:700;border-color:#1976d2;box-shadow:0 2px 12px rgba(21,101,192,.35);}
.content{padding:1rem;flex:1;overflow-y:auto;}

/* Cards */
.card{background:rgba(255,255,255,.03);border:1px solid rgba(79,195,247,.1);border-radius:16px;padding:1.2rem;margin-bottom:1rem;backdrop-filter:blur(6px);}
.card-title{font-size:.95rem;font-weight:700;color:#4fc3f7;margin-bottom:1rem;display:flex;align-items:center;gap:.5rem;}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:.8rem;}
.stat-box{background:linear-gradient(135deg,rgba(79,195,247,.07),rgba(33,150,243,.05));border:1px solid rgba(79,195,247,.12);border-radius:14px;padding:1.1rem;text-align:center;}
.stat-num{font-size:1.9rem;font-weight:800;background:linear-gradient(135deg,#4fc3f7,#81d4fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.stat-lbl{font-size:.78rem;color:#78909c;margin-top:.3rem;}

/* Receipts */
.receipt-item{background:rgba(255,255,255,.03);border:1px solid rgba(79,195,247,.1);border-radius:14px;padding:1rem;margin-bottom:.8rem;transition:border-color .2s;}
.receipt-item:hover{border-color:rgba(79,195,247,.25);}
.receipt-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.5rem;}
.receipt-user{font-weight:700;color:#4fc3f7;font-size:.95rem;}
.receipt-date{font-size:.75rem;color:#455a64;background:rgba(255,255,255,.05);padding:.2rem .5rem;border-radius:6px;}
.receipt-info{font-size:.82rem;color:#90caf9;margin-bottom:.8rem;line-height:1.5;}
.receipt-amount{font-size:1rem;font-weight:700;color:#a5d6a7;margin-bottom:.8rem;}
.btn-row{display:flex;gap:.6rem;}
.btn{flex:1;padding:.65rem;border:none;border-radius:10px;font-size:.85rem;font-weight:700;cursor:pointer;font-family:inherit;transition:all .2s;}
.btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.3);}
.btn:active{transform:translateY(0);}
.btn-approve{background:linear-gradient(135deg,#00c853,#00e676);color:#fff;}
.btn-reject{background:linear-gradient(135deg,#ff5252,#ff6e40);color:#fff;}
.btn-primary{background:linear-gradient(135deg,#1565c0,#1976d2);color:#fff;}

/* Config form */
.config-form{display:none;margin-top:.8rem;padding-top:.8rem;border-top:1px solid rgba(79,195,247,.1);}
.config-form.visible{display:block;}
.step-badge{display:inline-flex;align-items:center;gap:.4rem;background:rgba(79,195,247,.12);border:1px solid rgba(79,195,247,.2);color:#4fc3f7;border-radius:10px;padding:.4rem .8rem;font-size:.82rem;font-weight:700;margin-bottom:.8rem;}
.form-label{font-size:.82rem;color:#78909c;margin-bottom:.35rem;margin-top:.6rem;display:block;}
.form-input{width:100%;background:rgba(255,255,255,.04);border:1px solid rgba(79,195,247,.18);border-radius:10px;padding:.7rem;color:#e8f4fd;font-size:.88rem;font-family:inherit;resize:vertical;min-height:64px;transition:border-color .2s;}
.form-input:focus{outline:none;border-color:#4fc3f7;background:rgba(79,195,247,.04);}

/* Settings */
.setting-group{margin-bottom:.5rem;}
.setting-label{font-size:.8rem;color:#78909c;margin-bottom:.4rem;font-weight:600;letter-spacing:.02em;}
.setting-row{display:flex;gap:.5rem;align-items:center;}
.setting-input{flex:1;background:rgba(255,255,255,.04);border:1px solid rgba(79,195,247,.15);border-radius:10px;padding:.65rem;color:#e8f4fd;font-size:.9rem;font-family:inherit;transition:border-color .2s;}
.setting-input:focus{outline:none;border-color:#4fc3f7;}
.btn-save{padding:.65rem 1rem;background:linear-gradient(135deg,#1565c0,#1976d2);color:#fff;border:none;border-radius:10px;font-weight:700;cursor:pointer;font-family:inherit;font-size:.85rem;white-space:nowrap;}

/* Products */
.product-item{background:rgba(255,255,255,.03);border:1px solid rgba(79,195,247,.1);border-radius:14px;padding:1rem;margin-bottom:.8rem;}
.product-header{font-size:1rem;font-weight:700;margin-bottom:.3rem;}
.product-meta{font-size:.8rem;color:#78909c;margin-bottom:.8rem;}
.edit-input{flex:1;background:rgba(255,255,255,.04);border:1px solid rgba(79,195,247,.15);border-radius:8px;padding:.5rem;color:#e8f4fd;font-size:.85rem;font-family:inherit;}
.edit-row{display:flex;gap:.5rem;margin-bottom:.4rem;}

/* AI chat (تغییرات) */
#tab-ai{display:none;flex-direction:column;height:calc(100vh - 120px);}
#tab-ai.visible{display:flex;}
.ai-header{background:linear-gradient(135deg,rgba(21,101,192,.15),rgba(33,150,243,.1));border:1px solid rgba(79,195,247,.15);border-radius:14px;padding:1rem;margin-bottom:.8rem;}
.ai-header-title{font-size:.95rem;font-weight:700;color:#4fc3f7;margin-bottom:.3rem;}
.ai-header-sub{font-size:.8rem;color:#78909c;line-height:1.5;}
.ai-quick-btns{display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:.8rem;}
.ai-quick-btn{background:rgba(79,195,247,.08);border:1px solid rgba(79,195,247,.2);color:#90caf9;border-radius:20px;padding:.35rem .8rem;font-size:.78rem;cursor:pointer;font-family:inherit;transition:all .2s;}
.ai-quick-btn:hover{background:rgba(79,195,247,.15);color:#4fc3f7;}
.chat-area{flex:1;overflow-y:auto;padding:.5rem;display:flex;flex-direction:column;gap:.7rem;scroll-behavior:smooth;}
.msg{max-width:88%;border-radius:14px;padding:.85rem 1rem;font-size:.88rem;line-height:1.6;}
.msg-user{background:linear-gradient(135deg,#1565c0,#1976d2);color:#fff;align-self:flex-end;border-radius:14px 14px 4px 14px;box-shadow:0 2px 8px rgba(21,101,192,.3);}
.msg-ai{background:rgba(255,255,255,.04);border:1px solid rgba(79,195,247,.15);align-self:flex-start;border-radius:14px 14px 14px 4px;}
.msg-ai pre{background:rgba(0,0,0,.4);border-radius:8px;padding:.8rem;overflow-x:auto;margin:.6rem 0;font-size:.78rem;border:1px solid rgba(79,195,247,.1);}
.msg-ai code{background:rgba(0,0,0,.3);border-radius:4px;padding:.1rem .3rem;font-size:.82rem;font-family:monospace;}
.copy-btn{background:rgba(79,195,247,.1);border:1px solid rgba(79,195,247,.2);color:#4fc3f7;border-radius:6px;padding:.3rem .7rem;font-size:.75rem;cursor:pointer;font-family:inherit;margin-top:.5rem;transition:all .2s;}
.copy-btn:hover{background:rgba(79,195,247,.2);}
.typing-dots{display:flex;gap:.3rem;align-items:center;padding:.5rem;}
.typing-dots span{width:7px;height:7px;border-radius:50%;background:#4fc3f7;animation:dotBounce .9s infinite;}
.typing-dots span:nth-child(2){animation-delay:.15s;}
.typing-dots span:nth-child(3){animation-delay:.3s;}
@keyframes dotBounce{0%,80%,100%{transform:scale(.6);opacity:.4;}40%{transform:scale(1);opacity:1;}}
.chat-input-row{background:rgba(10,22,40,.95);padding:.8rem;display:flex;gap:.5rem;border-top:1px solid rgba(79,195,247,.1);position:sticky;bottom:0;}
.chat-input{flex:1;background:rgba(255,255,255,.05);border:1px solid rgba(79,195,247,.2);border-radius:12px;padding:.7rem;color:#e8f4fd;font-size:.9rem;font-family:inherit;resize:none;min-height:44px;max-height:120px;}
.chat-input:focus{outline:none;border-color:#4fc3f7;}
.chat-send{background:linear-gradient(135deg,#1565c0,#42a5f5);border:none;border-radius:12px;width:44px;height:44px;color:#fff;font-size:1.3rem;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:opacity .2s;}
.chat-send:hover{opacity:.85;}

/* Toast */
.toast{position:fixed;bottom:1.2rem;left:50%;transform:translateX(-50%) translateY(120px);background:rgba(33,33,33,.95);color:#fff;padding:.7rem 1.4rem;border-radius:999px;font-size:.88rem;transition:transform .3s cubic-bezier(.175,.885,.32,1.275);z-index:9999;backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.1);}
.toast.show{transform:translateX(-50%) translateY(0);}

/* Loader */
.loader{position:fixed;inset:0;background:#060e1f;display:flex;align-items:center;justify-content:center;z-index:9999;flex-direction:column;gap:1rem;}
.spinner{width:48px;height:48px;border:3px solid rgba(79,195,247,.15);border-top-color:#4fc3f7;border-radius:50%;animation:spin 1s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}

/* Bot status badge */
.bot-status-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:.8rem;}
.status-badge{padding:.3rem .8rem;border-radius:999px;font-size:.8rem;font-weight:700;}
.status-on{background:rgba(0,200,83,.15);color:#00c853;border:1px solid rgba(0,200,83,.3);}
.status-off{background:rgba(255,82,82,.15);color:#ff5252;border:1px solid rgba(255,82,82,.3);}
.toggle-row{display:flex;gap:.5rem;margin-top:.8rem;}
.toggle-btn{flex:1;padding:.6rem;border:none;border-radius:10px;font-weight:700;cursor:pointer;font-family:inherit;font-size:.85rem;}
.toggle-on{background:linear-gradient(135deg,#00c853,#69f0ae);color:#fff;}
.toggle-off{background:linear-gradient(135deg,#ff5252,#ff6e40);color:#fff;}
</style>
</head>
<body>

<div class="loader" id="loader">
  <div class="spinner"></div>
  <div style="color:#4fc3f7;font-size:.88rem;letter-spacing:.05em;">ViraNet در حال بارگذاری...</div>
</div>

<!-- USER PAGE (Blue locked) -->
<div class="screen" id="user-locked">
  <div class="vn-logo">🌐</div>
  <div class="vn-brand">ViraNet</div>
  <div class="vn-slogan">سرویس‌های اینترنتی پرسرعت و امن</div>

  <div class="user-card">
    <div class="user-card-header">
      <div class="user-avatar" id="u-avatar">👤</div>
      <div>
        <div class="user-card-name" id="u-name">در حال بارگذاری...</div>
        <div class="user-card-id" id="u-id-text">---</div>
      </div>
    </div>
    <div class="info-row">
      <span class="info-label">🆔 شناسه</span>
      <span class="info-value" id="u-id">---</span>
    </div>
    <div class="info-row">
      <span class="info-label">💰 موجودی کیف پول</span>
      <span class="info-value" id="u-wallet">---</span>
    </div>
    <div class="info-row">
      <span class="info-label">📦 سرویس‌های فعال</span>
      <span class="info-value" id="u-svcs">---</span>
    </div>
  </div>

  <div class="feature-grid">
    <div class="feature-box"><div class="feature-icon">⚡</div><div class="feature-label">سرعت بالا</div></div>
    <div class="feature-box"><div class="feature-icon">🛡️</div><div class="feature-label">امنیت کامل</div></div>
    <div class="feature-box"><div class="feature-icon">🔧</div><div class="feature-label">پشتیبانی ۲۴/۷</div></div>
    <div class="feature-box"><div class="feature-icon">🚀</div><div class="feature-label">فعال‌سازی فوری</div></div>
  </div>

  <div class="lock-notice" style="margin-top:1.2rem;">
    🔒 این پنل فقط برای مدیریت ربات است.<br>برای خرید از منوی ربات استفاده کنید.
  </div>
</div>

<!-- ADMIN PANEL -->
<div class="screen" id="admin-panel">
  <div class="nav">
    <div class="nav-logo">🌐</div>
    <div class="nav-title">ViraNet Admin</div>
    <span class="nav-badge" id="pending-badge" style="display:none">0</span>
  </div>

  <div class="tabs">
    <button class="tab active" onclick="switchTab('dashboard',this)">📊 داشبورد</button>
    <button class="tab" onclick="switchTab('receipts',this)">📥 رسیدها</button>
    <button class="tab" onclick="switchTab('settings',this)">⚙️ تنظیمات</button>
    <button class="tab" onclick="switchTab('products',this)">📦 محصولات</button>
    <button class="tab" onclick="switchTab('ai',this)">🤖 تغییرات</button>
  </div>

  <div class="content">
    <!-- Dashboard -->
    <div id="tab-dashboard">
      <div class="card">
        <div class="card-title">📡 وضعیت ربات</div>
        <div class="bot-status-row">
          <span id="bot-status-text" style="font-size:.9rem;color:#90caf9;">در حال بارگذاری...</span>
          <span class="status-badge status-on" id="bot-status-badge">🟢 آنلاین</span>
        </div>
        <div class="toggle-row">
          <button class="toggle-btn toggle-on" onclick="setBotStatus(true)">🟢 روشن</button>
          <button class="toggle-btn toggle-off" onclick="setBotStatus(false)">🔴 خاموش</button>
        </div>
      </div>
      <div class="stat-grid">
        <div class="stat-box"><div class="stat-num" id="stat-users">—</div><div class="stat-lbl">👥 کاربران</div></div>
        <div class="stat-box"><div class="stat-num" id="stat-pending">—</div><div class="stat-lbl">📥 رسید معلق</div></div>
        <div class="stat-box"><div class="stat-num" id="stat-revenue">—</div><div class="stat-lbl">💰 درآمد (M)</div></div>
        <div class="stat-box"><div class="stat-num" id="stat-orders">—</div><div class="stat-lbl">🛒 سفارش‌ها</div></div>
      </div>
      <div class="card" style="margin-top:.8rem;">
        <div class="card-title">⚡ دسترسی سریع</div>
        <div style="display:flex;flex-direction:column;gap:.5rem;">
          <button class="btn btn-primary" onclick="switchTab('receipts',document.querySelectorAll('.tab')[1])">📥 مشاهده رسیدها</button>
          <button class="btn btn-primary" onclick="loadStats()">🔄 بروزرسانی آمار</button>
        </div>
      </div>
    </div>

    <!-- Receipts -->
    <div id="tab-receipts" style="display:none;">
      <div class="card">
        <div class="card-title">📥 رسیدهای معلق
          <button onclick="loadReceipts()" style="margin-right:auto;background:rgba(79,195,247,.1);border:1px solid rgba(79,195,247,.2);color:#4fc3f7;border-radius:8px;padding:.25rem .6rem;font-size:.75rem;cursor:pointer;font-family:inherit;">🔄 بروز</button>
        </div>
        <div id="receipts-list"></div>
      </div>
    </div>

    <!-- Settings -->
    <div id="tab-settings" style="display:none;">
      <div class="card">
        <div class="card-title">💳 اطلاعات کارت بانکی</div>
        <div class="setting-group">
          <div class="setting-label">شماره کارت</div>
          <div class="setting-row">
            <input class="setting-input" id="s-card" placeholder="شماره کارت">
            <button class="btn-save" onclick="saveSetting('card_number','s-card')">✓</button>
          </div>
        </div>
        <div class="setting-group" style="margin-top:.8rem;">
          <div class="setting-label">نام صاحب کارت</div>
          <div class="setting-row">
            <input class="setting-input" id="s-owner" placeholder="نام صاحب کارت">
            <button class="btn-save" onclick="saveSetting('card_owner','s-owner')">✓</button>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">🔷 آدرس کیف پول TRX</div>
        <div class="setting-group">
          <div class="setting-label">آدرس ولت ترون</div>
          <div class="setting-row">
            <input class="setting-input" id="s-wallet" placeholder="T...">
            <button class="btn-save" onclick="saveSetting('trx_wallet','s-wallet')">✓</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Products -->
    <div id="tab-products" style="display:none;">
      <div class="card">
        <div class="card-title">📦 مدیریت محصولات</div>
        <div id="products-list"></div>
      </div>
    </div>

    <!-- AI تغییرات -->
    <div id="tab-ai">
      <div class="ai-header">
        <div class="ai-header-title">🤖 هوش مصنوعی — تغییرات ربات</div>
        <div class="ai-header-sub">
          هر تغییری که می‌خواهید توضیح دهید — کد دقیق Python با راهنمای کامل دریافت کنید.<br>
          مثال: «یه دکمه اضافه کن که کاربر بتونه سرویسش رو تمدید کنه»
        </div>
      </div>
      <div class="ai-quick-btns">
        <button class="ai-quick-btn" onclick="quickMsg('یه دکمه تمدید سرویس اضافه کن')">🔄 تمدید سرویس</button>
        <button class="ai-quick-btn" onclick="quickMsg('سیستم کد تخفیف اضافه کن')">🎁 کد تخفیف</button>
        <button class="ai-quick-btn" onclick="quickMsg('پیام خوش‌آمدگویی رو بهتر کن')">✨ بهبود متن</button>
        <button class="ai-quick-btn" onclick="quickMsg('یه دکمه آمار کاربران به منو اضافه کن')">📊 آمار کاربران</button>
        <button class="ai-quick-btn" onclick="quickMsg('سیستم اعلان خودکار اضافه کن')">🔔 اعلان‌ها</button>
      </div>
      <div class="chat-area" id="chat-area"></div>
      <div class="chat-input-row">
        <textarea class="chat-input" id="chat-input" placeholder="تغییر مورد نظر را شرح دهید..." rows="1"></textarea>
        <button class="chat-send" onclick="sendAiMsg()">➤</button>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const tg = window.Telegram.WebApp;
tg.ready(); tg.expand();
tg.setHeaderColor('#0a1628');
tg.setBackgroundColor('#0a1628');
const initData = tg.initData;
let adminMode = false;
let approveState = {};

async function api(path, body={}) {
  try {
    const r = await fetch('/webapp' + path, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({init_data: initData, ...body})
    });
    return r.json();
  } catch(e) { return {ok:false, error: String(e)}; }
}

function showToast(msg, dur=2800) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), dur);
}

function switchTab(tab, btn) {
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  ['dashboard','receipts','settings','products','ai'].forEach(t => {
    const el = document.getElementById('tab-'+t);
    if(el) el.style.display = 'none';
  });
  const el = document.getElementById('tab-'+tab);
  if(!el) return;
  el.style.display = tab === 'ai' ? 'flex' : 'block';
  if (tab === 'ai') el.style.flexDirection = 'column';
  if (tab === 'receipts') loadReceipts();
  if (tab === 'settings') loadSettings();
  if (tab === 'products') loadProducts();
  if (tab === 'dashboard') loadStats();
}

async function loadStats() {
  const d = await api('/stats');
  if (!d.ok) return;
  document.getElementById('stat-users').textContent = d.users;
  document.getElementById('stat-pending').textContent = d.pending;
  document.getElementById('stat-revenue').textContent = Math.round(d.revenue/1000000);
  document.getElementById('stat-orders').textContent = d.orders;
  const online = d.bot_online;
  document.getElementById('bot-status-text').textContent = online ? '🟢 ربات در حال اجرا است' : '🔴 ربات خاموش است';
  const badge = document.getElementById('bot-status-badge');
  badge.textContent = online ? '🟢 آنلاین' : '🔴 آفلاین';
  badge.className = 'status-badge ' + (online ? 'status-on' : 'status-off');
  const b = document.getElementById('pending-badge');
  if (d.pending > 0) { b.textContent = d.pending; b.style.display = ''; }
  else b.style.display = 'none';
}

async function setBotStatus(online) {
  const d = await api('/bot-status', {online});
  if (d.ok) { showToast(online ? '🟢 ربات روشن شد' : '🔴 ربات خاموش شد'); loadStats(); }
  else showToast('❌ خطا در تغییر وضعیت');
}

async function loadReceipts() {
  const d = await api('/pending');
  const el = document.getElementById('receipts-list');
  if (!d.ok) { el.innerHTML = '<div style="text-align:center;color:#ef5350;padding:2rem;">❌ خطا در بارگذاری</div>'; return; }
  if (!d.receipts.length) {
    el.innerHTML = '<div style="text-align:center;color:#455a64;padding:3rem;font-size:.9rem;">✅ رسید معلقی وجود ندارد</div>';
    return;
  }
  el.innerHTML = d.receipts.map(r => `
    <div class="receipt-item" id="rec-${r.id}">
      <div class="receipt-top">
        <span class="receipt-user">👤 ${r.username}</span>
        <span class="receipt-date">${r.created_at}</span>
      </div>
      <div class="receipt-info">📦 ${r.plan_label} &nbsp;|&nbsp; ${r.qty} سرویس &nbsp;|&nbsp; پرداخت: ${r.type}</div>
      <div class="receipt-amount">💰 ${r.total.toLocaleString()} تومان</div>
      <div class="btn-row">
        <button class="btn btn-approve" onclick="startApprove(${r.order_id},${r.qty},${r.id})">✅ تایید و ارسال کانفیگ</button>
        <button class="btn btn-reject" onclick="rejectOrder(${r.order_id},${r.id})">❌ رد</button>
      </div>
      <div class="config-form" id="form-${r.order_id}">
        <div class="step-badge" id="step-badge-${r.order_id}">📋 سرویس ۱</div>
        <label class="form-label" id="cfg-label-${r.order_id}">کانفیگ سرویس ۱:</label>
        <textarea class="form-input" id="cfg-${r.order_id}" placeholder="vless://... یا vmess://..."></textarea>
        <label class="form-label">ساب‌لینک (اختیاری):</label>
        <textarea class="form-input" id="sub-${r.order_id}" placeholder="https://..."></textarea>
        <div class="btn-row" style="margin-top:.8rem;">
          <button class="btn btn-primary" onclick="nextStep(${r.order_id})">⬅️ بعدی / ارسال</button>
        </div>
      </div>
    </div>
  `).join('');
}

function startApprove(orderId, qty, recId) {
  approveState[orderId] = {qty, step:0, configs:[], subs:[], recId};
  document.getElementById('form-'+orderId).classList.add('visible');
  updateStep(orderId);
}

function updateStep(orderId) {
  const s = approveState[orderId];
  const stepNum = s.step + 1;
  document.getElementById('step-badge-'+orderId).textContent = '📋 سرویس ' + stepNum + ' از ' + s.qty;
  document.getElementById('cfg-label-'+orderId).textContent = 'کانفیگ سرویس ' + stepNum + ':';
  document.getElementById('cfg-'+orderId).value = '';
  document.getElementById('sub-'+orderId).value = '';
  document.getElementById('cfg-'+orderId).focus();
}

async function nextStep(orderId) {
  const s = approveState[orderId];
  const cfg = document.getElementById('cfg-'+orderId).value.trim();
  const sub = document.getElementById('sub-'+orderId).value.trim();
  if (!cfg) { showToast('⚠️ کانفیگ را وارد کنید'); return; }
  s.configs.push(cfg); s.subs.push(sub); s.step++;
  if (s.step < s.qty) {
    updateStep(orderId);
    showToast('✅ سرویس ' + s.step + ' ثبت شد — بعدی را وارد کنید');
  } else {
    const d = await api('/approve', {order_id: orderId, configs: s.configs, subs: s.subs});
    if (d.ok) {
      showToast('🎉 ' + s.configs.length + ' کانفیگ ارسال شد!', 3500);
      const recEl = document.getElementById('rec-'+s.recId);
      if (recEl) recEl.remove();
      delete approveState[orderId];
    } else {
      showToast('❌ خطا: ' + (d.error || 'نامشخص'));
    }
  }
}

async function rejectOrder(orderId, recId) {
  if (!confirm('رسید رد شود؟')) return;
  const d = await api('/reject', {order_id: orderId});
  if (d.ok) {
    showToast('❌ رسید رد شد');
    const recEl = document.getElementById('rec-'+recId);
    if (recEl) recEl.remove();
  }
}

async function loadSettings() {
  const d = await api('/settings-get');
  if (!d.ok) return;
  document.getElementById('s-card').value = d.card_number || '';
  document.getElementById('s-owner').value = d.card_owner || '';
  document.getElementById('s-wallet').value = d.trx_wallet || '';
}

async function saveSetting(key, inputId) {
  const value = document.getElementById(inputId).value.trim();
  if (!value) { showToast('⚠️ مقدار را وارد کنید'); return; }
  const d = await api('/settings-set', {key, value});
  d.ok ? showToast('✅ ' + (key==='trx_wallet'?'آدرس ولت':'اطلاعات کارت') + ' ذخیره شد!') : showToast('❌ خطا در ذخیره');
}

async function loadProducts() {
  const d = await api('/products-get');
  const el = document.getElementById('products-list');
  if (!d.ok || !d.products.length) { el.innerHTML = '<div style="text-align:center;color:#455a64;padding:2rem;">محصولی وجود ندارد</div>'; return; }
  el.innerHTML = d.products.map(p => `
    <div class="product-item">
      <div class="product-header">${p.active ? '✅' : '❌'} ${p.label}</div>
      <div class="product-meta">${p.gb} گیگابایت | ${p.days} روز | ${p.price.toLocaleString()} تومان</div>
      <div class="edit-row">
        <input class="edit-input" id="plbl-${p.id}" value="${p.label}" placeholder="نام محصول">
        <button class="btn-save" style="flex-shrink:0;" onclick="saveProduct(${p.id})">✓ ذخیره</button>
      </div>
      <div class="edit-row">
        <input class="edit-input" id="pprice-${p.id}" value="${p.price}" type="number" placeholder="قیمت (تومان)">
      </div>
    </div>
  `).join('');
}

async function saveProduct(id) {
  const label = document.getElementById('plbl-'+id).value.trim();
  const price = parseInt(document.getElementById('pprice-'+id).value);
  if (!label || !price) { showToast('⚠️ مقادیر را وارد کنید'); return; }
  const d = await api('/products-update', {id, label, price});
  d.ok ? showToast('✅ محصول ویرایش شد!') : showToast('❌ خطا');
}

const aiHistory = [];

function quickMsg(text) {
  document.getElementById('chat-input').value = text;
  sendAiMsg();
}

async function sendAiMsg() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = ''; input.style.height = 'auto';
  addMsg(text, 'user');
  aiHistory.push({role:'user', content: text});
  const typing = addTyping();
  const d = await api('/ai-chat', {messages: aiHistory});
  typing.remove();
  if (d.ok) {
    aiHistory.push({role:'assistant', content: d.reply});
    addMsg(d.reply, 'ai');
  } else {
    addMsg('❌ برای استفاده از هوش مصنوعی، متغیر OPENAI_API_KEY یا GROQ_API_KEY را در Railway تنظیم کنید.', 'ai');
  }
}

function addMsg(text, role) {
  const div = document.createElement('div');
  div.className = 'msg msg-' + role;
  if (role === 'ai') {
    let html = text
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/```python([\s\S]*?)```/g, (_, code) => {
        const id = 'code_' + Math.random().toString(36).slice(2);
        return '<pre><code id="' + id + '">' + code + '</code></pre><button class="copy-btn" onclick="copyCode(\'' + id + '\')">📋 کپی کد</button>';
      })
      .replace(/```([\s\S]*?)```/g, '<pre>$1</pre>')
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\n/g, '<br>');
    div.innerHTML = html;
  } else {
    div.textContent = text;
  }
  const area = document.getElementById('chat-area');
  area.appendChild(div); area.scrollTop = area.scrollHeight;
  return div;
}

function copyCode(id) {
  const el = document.getElementById(id);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent).then(() => showToast('✅ کد کپی شد!')).catch(() => showToast('❌ خطا در کپی'));
}

function addTyping() {
  const div = document.createElement('div');
  div.className = 'msg msg-ai';
  div.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
  const area = document.getElementById('chat-area');
  area.appendChild(div); area.scrollTop = area.scrollHeight;
  return div;
}

const chatInput = document.getElementById('chat-input');
chatInput?.addEventListener('keydown', e => { if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); sendAiMsg(); } });
chatInput?.addEventListener('input', function() { this.style.height='auto'; this.style.height=Math.min(this.scrollHeight,120)+'px'; });

async function init() {
  const d = await api('/me');
  document.getElementById('loader').style.display = 'none';
  if (d.ok && d.is_admin) {
    adminMode = true;
    document.getElementById('admin-panel').classList.add('active');
    loadStats();
  } else {
    document.getElementById('user-locked').classList.add('active');
    if (d.user) {
      const n = (d.user.full_name || '').trim() || 'کاربر';
      document.getElementById('u-name').textContent = n;
      document.getElementById('u-avatar').textContent = n[0] || '👤';
      document.getElementById('u-id-text').textContent = 'شناسه: ' + d.user.user_id;
      document.getElementById('u-id').textContent = d.user.user_id;
      document.getElementById('u-wallet').textContent = (d.user.wallet||0).toLocaleString() + ' تومان';
      document.getElementById('u-svcs').textContent = (d.user.svcs||0) + ' سرویس';
    }
  }
}
init();
</script>
</body>
</html>"""

# ─────────────────────────────────────────────
#  FLASK
# ─────────────────────────────────────────────
flask_app = Flask(__name__)

def require_admin(init_data):
    user = verify_webapp(init_data)
    if not user: return None, False
    return user, user.get("id") == ADMIN_ID

@flask_app.route("/health")
def health(): return jsonify({"status":"ok","bot_online":BOT_ONLINE})

@flask_app.route("/")
def index(): return "🤖 ViraNet Bot is running!", 200

@flask_app.route("/shop")
def shop_page(): return Response(WEBAPP_HTML, content_type="text/html; charset=utf-8")

@flask_app.route("/admin")
def admin_page(): return Response(WEBAPP_HTML, content_type="text/html; charset=utf-8")

@flask_app.route("/panel")
def panel_page(): return Response(WEBAPP_HTML, content_type="text/html; charset=utf-8")

# ── WebApp API ─────────────────────────────────
def wa_json(data): return flask_app.response_class(json.dumps(data, ensure_ascii=False), content_type="application/json")

@flask_app.route("/webapp/me", methods=["POST"])
def wa_me():
    body = request.get_json(force=True)
    user = verify_webapp(body.get("init_data",""))
    if not user: return wa_json({"ok":False,"error":"invalid"})
    uid = user.get("id")
    u   = get_user(uid)
    with get_db() as conn:
        svcs = conn.execute("SELECT COUNT(*) as c FROM order_services WHERE user_id=? AND config_text IS NOT NULL",(uid,)).fetchone()["c"]
    is_admin = uid == ADMIN_ID
    return wa_json({
        "ok": True, "is_admin": is_admin,
        "user": {"user_id":uid,"full_name":user.get("first_name","")+" "+user.get("last_name",""),
                 "wallet": u["wallet"] if u else 0, "svcs": svcs} if u else None
    })

@flask_app.route("/webapp/stats", methods=["POST"])
def wa_stats():
    body = request.get_json(force=True)
    _, is_admin = require_admin(body.get("init_data",""))
    if not is_admin: return wa_json({"ok":False,"error":"forbidden"})
    with get_db() as conn:
        uc = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        pc = conn.execute("SELECT COUNT(*) as c FROM receipts WHERE status='pending'").fetchone()["c"]
        ts = conn.execute("SELECT SUM(total_price) as s FROM orders WHERE status='delivered'").fetchone()["s"] or 0
        oc = conn.execute("SELECT COUNT(*) as c FROM orders").fetchone()["c"]
    return wa_json({"ok":True,"users":uc,"pending":pc,"revenue":ts,"orders":oc,"bot_online":BOT_ONLINE})

@flask_app.route("/webapp/bot-status", methods=["POST"])
def wa_bot_status():
    global BOT_ONLINE
    body = request.get_json(force=True)
    _, is_admin = require_admin(body.get("init_data",""))
    if not is_admin: return wa_json({"ok":False,"error":"forbidden"})
    BOT_ONLINE = bool(body.get("online", True))
    return wa_json({"ok":True})

@flask_app.route("/webapp/pending", methods=["POST"])
def wa_pending():
    body = request.get_json(force=True)
    _, is_admin = require_admin(body.get("init_data",""))
    if not is_admin: return wa_json({"ok":False,"error":"forbidden"})
    with get_db() as conn:
        rows = conn.execute(
            "SELECT r.*,u.username,u.full_name,o.quantity,o.total_price,o.plan_key "
            "FROM receipts r "
            "JOIN users u ON r.user_id=u.user_id "
            "JOIN orders o ON r.order_id=o.id "
            "WHERE r.status='pending' ORDER BY r.created_at DESC LIMIT 20"
        ).fetchall()
    result = []
    for r in rows:
        plan = PLANS.get(r["plan_key"], {})
        result.append({
            "id": r["id"], "order_id": r["order_id"],
            "user_id": r["user_id"],
            "username": r["username"] or r["full_name"] or str(r["user_id"]),
            "created_at": r["created_at"][:16],
            "type": "TRX" if r["receipt_type"] == "purchase_crypto" else "کارت",
            "plan_label": plan.get("label","---"),
            "qty": r["quantity"], "total": r["total_price"]
        })
    return wa_json({"ok":True,"receipts":result})

@flask_app.route("/webapp/approve", methods=["POST"])
def wa_approve():
    body = request.get_json(force=True)
    _, is_admin = require_admin(body.get("init_data",""))
    if not is_admin: return wa_json({"ok":False,"error":"forbidden"})
    order_id = body.get("order_id")
    configs  = body.get("configs", [])
    subs     = body.get("subs", [])
    if not order_id or not configs: return wa_json({"ok":False,"error":"missing data"})
    try:
        _deliver_configs(order_id, configs, subs)
        # پاک کردن پیام ادمین در تلگرام
        _delete_receipt_admin_msg(order_id)
        return wa_json({"ok":True})
    except Exception as e:
        return wa_json({"ok":False,"error":str(e)})

@flask_app.route("/webapp/reject", methods=["POST"])
def wa_reject():
    body = request.get_json(force=True)
    _, is_admin = require_admin(body.get("init_data",""))
    if not is_admin: return wa_json({"ok":False,"error":"forbidden"})
    order_id = body.get("order_id")
    try:
        with get_db() as conn:
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            conn.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
            conn.execute("UPDATE receipts SET status='rejected' WHERE order_id=?", (order_id,))
            conn.commit()
        _delete_receipt_admin_msg(order_id)
        try:
            bot.send_message(order["user_id"],
                "❌ <b>رسید شما رد شد.</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📞 پیگیری: @{SUPPORT_USERNAME}"
            )
        except Exception: pass
        return wa_json({"ok":True})
    except Exception as e:
        return wa_json({"ok":False,"error":str(e)})

@flask_app.route("/webapp/settings-get", methods=["POST"])
def wa_settings_get():
    body = request.get_json(force=True)
    _, is_admin = require_admin(body.get("init_data",""))
    if not is_admin: return wa_json({"ok":False,"error":"forbidden"})
    return wa_json({"ok":True,"card_number":CARD_NUMBER,"card_owner":CARD_OWNER,"trx_wallet":TRX_WALLET})

@flask_app.route("/webapp/settings-set", methods=["POST"])
def wa_settings_set():
    global CARD_NUMBER, CARD_OWNER, TRX_WALLET
    body = request.get_json(force=True)
    _, is_admin = require_admin(body.get("init_data",""))
    if not is_admin: return wa_json({"ok":False,"error":"forbidden"})
    key = body.get("key"); value = (body.get("value") or "").strip()
    if not key or not value: return wa_json({"ok":False,"error":"missing"})
    save_setting(key, value)
    if key == "card_number": CARD_NUMBER = value
    if key == "card_owner":  CARD_OWNER  = value
    if key == "trx_wallet":  TRX_WALLET  = value
    return wa_json({"ok":True})

@flask_app.route("/webapp/products-get", methods=["POST"])
def wa_products_get():
    body = request.get_json(force=True)
    _, is_admin = require_admin(body.get("init_data",""))
    if not is_admin: return wa_json({"ok":False,"error":"forbidden"})
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY price").fetchall()
    return wa_json({"ok":True,"products":[{"id":r["id"],"label":r["label"],"gb":r["gb"],"days":r["days"],"price":r["price"],"active":r["active"]} for r in rows]})

@flask_app.route("/webapp/products-update", methods=["POST"])
def wa_products_update():
    body = request.get_json(force=True)
    _, is_admin = require_admin(body.get("init_data",""))
    if not is_admin: return wa_json({"ok":False,"error":"forbidden"})
    prod_id = body.get("id"); label = body.get("label","").strip(); price = body.get("price",0)
    if not prod_id or not label or not price: return wa_json({"ok":False,"error":"missing"})
    with get_db() as conn:
        conn.execute("UPDATE products SET label=?, price=? WHERE id=?", (label, int(price), prod_id)); conn.commit()
    reload_plans()
    return wa_json({"ok":True})

@flask_app.route("/webapp/ai-chat", methods=["POST"])
def wa_ai_chat():
    body = request.get_json(force=True)
    _, is_admin = require_admin(body.get("init_data",""))
    if not is_admin: return wa_json({"ok":False,"error":"forbidden"})
    messages = body.get("messages", [])
    full_msgs = [{"role":"system","content":AI_SYSTEM}] + messages
    reply = ai_chat(full_msgs)
    return wa_json({"ok":True,"reply":reply})

def run_flask(): flask_app.run(host="0.0.0.0", port=PORT, debug=False)

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print(f"🚀 ViraNet Bot | port={PORT} | admin={ADMIN_ID} | webapp={WEBAPP_URL or 'disabled'}")
    threading.Thread(target=run_flask, daemon=True).start()
    print("✅ Flask started")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
