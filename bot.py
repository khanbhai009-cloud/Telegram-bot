import os
import asyncio
import random
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List

import requests
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

# ===================================
# 🔐 ENVIRONMENT
# ===================================
load_dotenv()

BOT_TOKEN = os.getenv("USER_BOT_TOKEN", "")
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY", "")

# Keep-alive (prevent sleep)
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL", "https://telegram-bot-km29.onrender.com")
KEEPALIVE_INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", str(60 * 5)))  # 5 minutes

if not BOT_TOKEN or not FIREBASE_PROJECT_ID:
    raise SystemExit("❌ Missing USER_BOT_TOKEN or FIREBASE_PROJECT_ID in .env")

BASE_URL = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("EarningBot")

CONFIG_CACHE: Dict[str, Any] = {}
BOT_USERNAME: str = "EarningBot"

# ===================================
# 🔥 FIRESTORE HELPERS (REST)
# ===================================

def _fs_value(v: Any) -> Dict[str, Any]:
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: _fs_value(v[k]) for k in v}}}
    if isinstance(v, list):
        return {"arrayValue": {"values": [_fs_value(i) for i in v]}}
    return {"stringValue": str(v)}

def _fs_parse(fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    fields = fields or {}
    out: Dict[str, Any] = {}
    for k, v in fields.items():
        if "stringValue" in v:
            out[k] = v["stringValue"]
        elif "integerValue" in v:
            out[k] = int(v["integerValue"])
        elif "doubleValue" in v:
            out[k] = float(v["doubleValue"])
        elif "booleanValue" in v:
            out[k] = v["booleanValue"]
        elif "mapValue" in v:
            out[k] = _fs_parse(v["mapValue"].get("fields", {}))
        elif "arrayValue" in v:
            arr = v["arrayValue"].get("values", []) or []
            out[k] = [_fs_parse({"x": i})["x"] for i in arr]
        elif "timestampValue" in v:
            out[k] = v["timestampValue"]
        else:
            out[k] = None
    return out

def firestore_get(path: str) -> Optional[Dict[str, Any]]:
    url = f"{BASE_URL}/{path}"
    params = {"key": FIREBASE_API_KEY} if FIREBASE_API_KEY else {}
    r = requests.get(url, params=params, timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return _fs_parse(r.json().get("fields", {}))

def firestore_set(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{BASE_URL}/{path}"
    params = {"key": FIREBASE_API_KEY} if FIREBASE_API_KEY else {}
    body = {"fields": {k: _fs_value(v) for k, v in data.items()}}
    r = requests.patch(url, params=params, json=body, timeout=15)
    r.raise_for_status()
    return _fs_parse(r.json().get("fields", {}))

def firestore_create(collection: str, doc_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{BASE_URL}/{collection}"
    params = {"documentId": doc_id}
    if FIREBASE_API_KEY:
        params["key"] = FIREBASE_API_KEY
    body = {"fields": {k: _fs_value(v) for k, v in data.items()}}
    r = requests.post(url, params=params, json=body, timeout=15)
    r.raise_for_status()
    return _fs_parse(r.json().get("fields", {}))

def run_query_equals(collection: str, field: str, value: Any) -> List[Dict[str, Any]]:
    url = f"{BASE_URL}:runQuery"
    params = {"key": FIREBASE_API_KEY} if FIREBASE_API_KEY else {}
    payload = {
        "structuredQuery": {
            "from": [{"collectionId": collection}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": field},
                    "op": "EQUAL",
                    "value": _fs_value(value),
                }
            },
        }
    }
    r = requests.post(url, params=params, json=payload, timeout=15)
    r.raise_for_status()
    rows = []
    for item in r.json():
        if "document" in item:
            rows.append(_fs_parse(item["document"].get("fields", {})))
    return rows

# Domain helpers

def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def get_config(force_refresh: bool = False) -> Dict[str, Any]:
    global CONFIG_CACHE
    if CONFIG_CACHE and not force_refresh:
        return CONFIG_CACHE
    cfg = firestore_get("config/global") or {}
    cfg.setdefault("referralReward", 10)
    cfg.setdefault("bonusReward", 20)
    cfg.setdefault("adRewardMin", 1)
    cfg.setdefault("adRewardMax", 5)
    cfg.setdefault("adWebsiteURL", "https://example.com")
    cfg.setdefault("supportBot", "https://t.me/ExampleSupportBot")
    cfg.setdefault("vipMultipliers", {"vip1": 1.5, "vip2": 2.0, "vip3": 3.0})
    cfg.setdefault("vipCosts", {"vip1": 10, "vip2": 20, "vip3": 50})  # Stars amounts
    CONFIG_CACHE = cfg
    return cfg

def get_channels() -> List[Dict[str, str]]:
    """Fetch required join channels from Firestore: config/channels { channels: [{name,link}] }"""
    data = firestore_get("config/channels") or {}
    return data.get("channels", [])

def get_user(uid: str) -> Optional[Dict[str, Any]]:
    return firestore_get(f"users/{uid}")

def add_user(uid: str, name: str, ref_by: str = "") -> Dict[str, Any]:
    data = {
        "id": uid,
        "name": name,
        "coins": 0,
        "reffer": 0,
        "refferBy": ref_by,
        "adsWatched": 0,
        "tasksCompleted": 0,
        "totalWithdrawals": 0,
        "vipTier": "free",
        "vipActivatedAt": "",
        "withdrawalsDone": 0,
        "joinedAt": _now_ts(),
        "lastBonusAt": "",
        "banned": False,
    }
    firestore_set(f"users/{uid}", data)
    return data

def update_user(uid: str, data: Dict[str, Any]) -> None:
    firestore_set(f"users/{uid}", data)

def get_referral_count(uid: str) -> int:
    try:
        return len(run_query_equals("users", "refferBy", uid))
    except Exception:
        return 0

def vip_multiplier(tier: str, cfg: Dict[str, Any]) -> float:
    if not tier or tier == "free":
        return 1.0
    return float(cfg.get("vipMultipliers", {}).get(tier, 1.0))

# ===================================
# 💬 UI HELPERS
# ===================================

def main_menu_kb() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton("▶️ Ad Dekho")],
        [KeyboardButton("💰 Balance"), KeyboardButton("👥 Refer & Earn")],
        [KeyboardButton("🎁 Bonus"), KeyboardButton("⚙️ Extra")],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)

def extra_menu_kb(cfg: Dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 VIP Plans", callback_data="vip")],
        [InlineKeyboardButton("📊 Stats", callback_data="stats")],
        [InlineKeyboardButton("🏦 Withdraw Funds", callback_data="withdraw_start")],
        [InlineKeyboardButton("🆘 Support", url=cfg.get("supportBot", "https://t.me/"))],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_home")],
    ])

def balance_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏦 Withdraw Funds", callback_data="withdraw_start")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_home")],
    ])

def vip_menu_kb(cfg: Dict[str, Any]) -> InlineKeyboardMarkup:
    costs = cfg.get("vipCosts", {"vip1": 10, "vip2": 20, "vip3": 50})
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"VIP 1 • {costs.get('vip1', 10)}⭐", callback_data="vip_set:vip1")],
        [InlineKeyboardButton(f"VIP 2 • {costs.get('vip2', 20)}⭐", callback_data="vip_set:vip2")],
        [InlineKeyboardButton(f"VIP 3 • {costs.get('vip3', 50)}⭐", callback_data="vip_set:vip3")],
        [InlineKeyboardButton("⬅️ Back", callback_data="extra")],
    ])

def join_channels_kb(channels: List[Dict[str, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"📢 {c['name']}", url=c["link"])] for c in channels]
    rows.append([InlineKeyboardButton("✅ Joined", callback_data="verify_joined")])
    return InlineKeyboardMarkup(rows)

def ad_prompt_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open Ad", url=url)],
        [InlineKeyboardButton("✅ I watched", callback_data="ad_claim")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_home")],
    ])

# ===================================
# 🛰️ KEEP-ALIVE PING
# ===================================

async def _ping_once():
    try:
        await asyncio.to_thread(requests.get, KEEPALIVE_URL, timeout=10)
        log.info("Keepalive ping → %s", KEEPALIVE_URL)
    except Exception as e:
        log.warning("Keepalive error: %s", e)

async def keepalive_loop():
    await asyncio.sleep(5)
    while True:
        await _ping_once()
        await asyncio.sleep(KEEPALIVE_INTERVAL)

# ===================================
# 🤖 BOT COMMANDS / HANDLERS
# ===================================

async def post_init(app):
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username
    app.create_task(keepalive_loop())
    log.info("Keepalive loop started (every %s sec)", KEEPALIVE_INTERVAL)

# ----- Channel Gate: verify membership -----

async def verify_all_joined(bot, user_id: int, channels: List[Dict[str, str]]) -> bool:
    for ch in channels:
        try:
            chat = await bot.get_chat(ch["link"])   # accepts @username or t.me link
            member = await bot.get_chat_member(chat.id, user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except BadRequest:
            # If chat not accessible, skip it (treat as joined to avoid blocking)
            continue
        except Exception:
            return False
    return True

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    name = user.full_name
    args = context.args or []
    ref_by = args[0] if args else ""

    cfg = get_config()
    channels = get_channels()

    # Register if new
    existing = get_user(uid)
    if not existing:
        add_user(uid, name, ref_by)
        reward = int(cfg.get("referralReward", 10))
        update_user(uid, {"coins": reward})
        if ref_by and ref_by != uid:
            ref_u = get_user(ref_by)
            if ref_u:
                update_user(ref_by, {
                    "coins": int(ref_u.get("coins", 0)) + reward,
                    "reffer": int(ref_u.get("reffer", 0)) + 1
                })

    # Channel gate
    if channels:
        await update.message.reply_text(
            f"👋 Welcome, {name}!\nBefore using the bot, please join all required channels:",
            reply_markup=join_channels_kb(channels)
        )
        return

    await update.message.reply_text(
        f"👋 Welcome, {name}!\nUse the buttons below to earn and manage your balance.",
        reply_markup=main_menu_kb()
    )

async def verify_joined_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    channels = get_channels()
    if not channels:
        await q.message.edit_text("✅ No channels configured. You already have access!", reply_markup=main_menu_kb())
        return
    joined = await verify_all_joined(context.bot, q.from_user.id, channels)
    if joined:
        await q.message.edit_text("✅ Verified! You can now use the bot.", reply_markup=main_menu_kb())
    else:
        await q.message.reply_text("❌ You haven’t joined all channels yet. Please join and tap ✅ Joined again.")

# ----- ReplyKeyboard: text buttons -----

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower()
    uid = str(update.effective_user.id)
    cfg = get_config()

    try:
        if "ad" in text:
            # Step 1: show ad prompt (URL + claim button). Record time.
            context.user_data["ad_shown_at"] = datetime.now(timezone.utc)
            await update.message.reply_text(
                "🎬 Open the sponsor link, explore it, then tap ✅ I watched to claim your reward.",
                reply_markup=ad_prompt_kb(cfg.get("adWebsiteURL", "https://example.com"))
            )

        elif "bonus" in text:
            user = get_user(uid) or add_user(uid, update.effective_user.full_name)
            last = user.get("lastBonusAt", "")
            can_claim = True
            if last:
                try:
                    dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    can_claim = datetime.now(timezone.utc) - dt >= timedelta(days=1)
                except Exception:
                    can_claim = True
            if not can_claim:
                await update.message.reply_text("⏳ Bonus already claimed today.", reply_markup=main_menu_kb())
                return
            base = int(cfg["bonusReward"])
            mult = vip_multiplier(user.get("vipTier", "free"), cfg)
            reward = int(round(base * mult))
            coins = int(user.get("coins", 0)) + reward
            update_user(uid, {"coins": coins, "lastBonusAt": _now_ts()})
            await update.message.reply_text(f"🎁 Bonus +{reward} coins!\nCurrent Balance: {coins}", reply_markup=main_menu_kb())

        elif "refer" in text:
            link = f"https://t.me/{BOT_USERNAME}?start={uid}"
            refs = get_referral_count(uid)
            await update.message.reply_text(
                f"👥 Refer & Earn\nYour link:\n{link}\nReferrals: {refs}",
                disable_web_page_preview=True,
                reply_markup=main_menu_kb(),
            )

        elif "balance" in text:
            user = get_user(uid) or add_user(uid, update.effective_user.full_name)
            await update.message.reply_text(
                f"💰 Coins: {user.get('coins',0)}\nVIP: {user.get('vipTier','free')}",
                reply_markup=main_menu_kb()
            )

        elif "extra" in text:
            await update.message.reply_text("✨ Extra", reply_markup=extra_menu_kb(cfg))

        else:
            await update.message.reply_text("❓ Please use the buttons below.", reply_markup=main_menu_kb())

    except Exception as e:
        log.exception("Error in handle_text: %s", e)
        await update.message.reply_text("⚠️ An error occurred. Please try again.", reply_markup=main_menu_kb())

# ----- Inline callbacks (menus) -----

async def back_home_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("🏠 Home", reply_markup=main_menu_kb())

async def extra_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("✨ Extra", reply_markup=extra_menu_kb(get_config()))

async def stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = get_user(str(q.from_user.id))
    if not user:
        await q.edit_message_text("❌ Please /start first.")
        return
    refs = get_referral_count(str(q.from_user.id))
    text = (
        "📊 Stats\n\n"
        f"Name: {user.get('name')}\n"
        f"Coins: {user.get('coins', 0)}\n"
        f"VIP: {user.get('vipTier', 'free')}\n"
        f"Ads Watched: {user.get('adsWatched', 0)}\n"
        f"Referrals: {refs}\n"
        f"Total Withdrawals: {user.get('totalWithdrawals', 0)}"
    )
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="extra")]]))

# ----- VIP (Stars Invoice) -----

async def vip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("👑 Choose your VIP tier:", reply_markup=vip_menu_kb(get_config()))

async def vip_set_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, tier = q.data.split(":", 1)
    cfg = get_config()
    cost_map = cfg.get("vipCosts", {"vip1": 10, "vip2": 20, "vip3": 50})
    cost_stars = int(cost_map.get(tier, 10))  # Stars units (XTR)
    prices = [LabeledPrice(label=f"VIP {tier.upper()} Access", amount=cost_stars)]
    await q.message.reply_invoice(
        title=f"VIP {tier.upper()} Activation",
        description=f"Unlock VIP {tier.upper()} — multipliers apply to Ads & Bonus.",
        payload=f"vip_{tier}",
        provider_token="TelegramStars",   # required param, placeholder for Stars
        currency="XTR",
        prices=prices,
        start_parameter=f"vip_{tier}",
    )

async def precheckout_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    try:
        await query.answer(ok=True)
    except Exception as e:
        log.exception("PreCheckout error: %s", e)
        await query.answer(ok=False, error_message="Payment error. Please try again later.")

async def successful_payment_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    payload = sp.invoice_payload
    if not payload.startswith("vip_"):
        return
    tier = payload.split("_", 1)[1]
    uid = str(update.effective_user.id)
    update_user(uid, {"vipTier": tier, "vipActivatedAt": _now_ts()})
    await update.message.reply_text(f"✅ VIP {tier.upper()} activated!", reply_markup=main_menu_kb())

# ----- Watch Ads: claim after prompt -----

async def ad_claim_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)
    cfg = get_config()

    # Simple cooldown: must wait >= 5 seconds after prompt
    shown_at: Optional[datetime] = context.user_data.get("ad_shown_at")
    if not shown_at or (datetime.now(timezone.utc) - shown_at).total_seconds() < 5:
        await q.edit_message_text("⏳ Please explore the sponsor link first, then try again.", reply_markup=ad_prompt_kb(cfg.get("adWebsiteURL", "https://example.com")))
        return

    user = get_user(uid) or add_user(uid, q.from_user.full_name)
    low, high = int(cfg["adRewardMin"]), int(cfg["adRewardMax"])
    base = random.randint(low, high)
    mult = vip_multiplier(user.get("vipTier", "free"), cfg)
    reward = int(round(base * mult))
    coins = int(user.get("coins", 0)) + reward
    ads = int(user.get("adsWatched", 0)) + 1
    update_user(uid, {"coins": coins, "adsWatched": ads})
    context.user_data.pop("ad_shown_at", None)

    await q.edit_message_text(
        f"🎬 Ad watched!\nReward: +{reward} coins (base {base} × VIP {mult}x)\nCurrent Balance: {coins}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_home")]])
    )

# ----- Withdraw flow (no admin chat message; admin bot will handle from Firebase) -----

async def withdraw_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["wd_state"] = "await_upi"
    await q.message.reply_text("🏦 Enter your UPI ID (e.g., name@upi):")

async def withdraw_text_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("wd_state")
    uid = str(update.effective_user.id)
    msg = (update.message.text or "").strip()
    user = get_user(uid) or add_user(uid, update.effective_user.full_name)

    if state == "await_upi":
        if "@" not in msg or len(msg) < 5:
            await update.message.reply_text("❌ Invalid UPI. Try again (e.g., name@upi).")
            return
        context.user_data["wd_upi"] = msg
        context.user_data["wd_state"] = "await_amount"
        await update.message.reply_text("💰 Enter amount (coins) to withdraw:")
        return

    if state == "await_amount":
        try:
            amt = int(msg)
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Enter a number:")
            return
        if amt <= 0:
            await update.message.reply_text("❌ Amount must be > 0.")
            return
        coins = int(user.get("coins", 0))
        if amt > coins:
            await update.message.reply_text(f"❌ Insufficient balance. You have {coins} coins.")
            return

        upi = context.user_data.get("wd_upi", "")
        doc_id = f"wd_{uid}_{int(datetime.now().timestamp())}_{random.randint(1000,9999)}"
        payload = {
            "userId": uid,
            "upi": upi,
            "amount": amt,
            "status": "pending",
            "requestedAt": _now_ts(),
            "processedAt": "",
        }
        firestore_create("withdrawals", doc_id, payload)
        update_user(uid, {
            "coins": coins - amt,
            "withdrawalsDone": int(user.get("withdrawalsDone", 0)) + 1,
            "totalWithdrawals": int(user.get("totalWithdrawals", 0)) + amt
        })
        context.user_data.pop("wd_state", None)
        context.user_data.pop("wd_upi", None)
        await update.message.reply_text(
            f"✅ Withdrawal request created.\nID: {doc_id}\nUPI: {upi}\nAmount: {amt}\nStatus: pending",
            reply_markup=main_menu_kb()
        )
        return

# ========================
# Error Handler
# ========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Update caused error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await update.effective_chat.send_message("⚠️ An error occurred. Please try again.")
    except Exception:
        pass

# ========================
# APP BUILD & RUN
# ========================

def build_application():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))

    # ReplyKeyboard text buttons
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Inline callbacks / menus
    app.add_handler(CallbackQueryHandler(back_home_cb, pattern="^back_home$"))
    app.add_handler(CallbackQueryHandler(extra_cb, pattern="^extra$"))
    app.add_handler(CallbackQueryHandler(stats_cb, pattern="^stats$"))
    app.add_handler(CallbackQueryHandler(CallbackQueryHandler, pattern="^refer$"))  # not used; refer handled via text or add if needed
    app.add_handler(CallbackQueryHandler(CallbackQueryHandler, pattern="^balance$"))  # same
    app.add_handler(CallbackQueryHandler(vip_cb, pattern="^vip$"))
    app.add_handler(CallbackQueryHandler(vip_set_cb, pattern="^vip_set:"))

    # Channel gate
    app.add_handler(CallbackQueryHandler(verify_joined_cb, pattern="^verify_joined$"))

    # Ads claim
    app.add_handler(CallbackQueryHandler(ad_claim_cb, pattern="^ad_claim$"))

    # Withdraw flow
    app.add_handler(CallbackQueryHandler(withdraw_start_cb, pattern="^withdraw_start$"))
    # UPI and amount come as plain text messages from the user:
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_text_cb))

    # Payments (Stars)
    app.add_handler(PreCheckoutQueryHandler(precheckout_cb))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_cb))

    # Errors
    app.add_error_handler(error_handler)
    return app

def main():
    app = build_application()
    log.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
