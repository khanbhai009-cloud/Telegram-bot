import os
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
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ===================================
# 🔐 ENVIRONMENT
# ===================================
load_dotenv()

BOT_TOKEN = os.getenv("USER_BOT_TOKEN")
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")

if not BOT_TOKEN or not FIREBASE_PROJECT_ID:
    raise SystemExit("❌ Missing USER_BOT_TOKEN or FIREBASE_PROJECT_ID in .env")

BASE_URL = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("EarningBot")

WITHDRAW_UPI, WITHDRAW_AMOUNT = range(2)
CONFIG_CACHE = {}
BOT_USERNAME = "EarningBot"


# ===================================
# 🔥 FIRESTORE HELPER FUNCTIONS
# ===================================

def _fs_value(v):
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: _fs_value(v[k]) for k in v}}}
    return {"stringValue": str(v)}


def _fs_parse(fields):
    result = {}
    for k, v in (fields or {}).items():
        if "stringValue" in v:
            result[k] = v["stringValue"]
        elif "integerValue" in v:
            result[k] = int(v["integerValue"])
        elif "doubleValue" in v:
            result[k] = float(v["doubleValue"])
        elif "booleanValue" in v:
            result[k] = v["booleanValue"]
        elif "mapValue" in v:
            result[k] = _fs_parse(v["mapValue"].get("fields", {}))
        else:
            result[k] = None
    return result


def firestore_get(path: str):
    url = f"{BASE_URL}/{path}?key={FIREBASE_API_KEY}"
    r = requests.get(url, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return _fs_parse(r.json().get("fields", {}))


def firestore_set(path: str, data: Dict[str, Any]):
    url = f"{BASE_URL}/{path}?key={FIREBASE_API_KEY}"
    body = {"fields": {k: _fs_value(v) for k, v in data.items()}}
    r = requests.patch(url, json=body, timeout=10)
    r.raise_for_status()
    return _fs_parse(r.json().get("fields", {}))


def firestore_create(collection: str, doc_id: str, data: Dict[str, Any]):
    url = f"{BASE_URL}/{collection}?documentId={doc_id}&key={FIREBASE_API_KEY}"
    body = {"fields": {k: _fs_value(v) for k, v in data.items()}}
    r = requests.post(url, json=body, timeout=10)
    r.raise_for_status()
    return _fs_parse(r.json().get("fields", {}))


def get_config():
    global CONFIG_CACHE
    if CONFIG_CACHE:
        return CONFIG_CACHE
    cfg = firestore_get("config/global") or {}
    cfg.setdefault("referralReward", 10)
    cfg.setdefault("bonusReward", 20)
    cfg.setdefault("adRewardMin", 1)
    cfg.setdefault("adRewardMax", 5)
    cfg.setdefault("adWebsiteURL", "https://example.com")
    cfg.setdefault("supportBot", "https://t.me/ExampleSupportBot")
    cfg.setdefault("vipMultipliers", {"vip1": 1.5, "vip2": 2, "vip3": 3})
    CONFIG_CACHE = cfg
    return cfg


def get_user(uid):
    return firestore_get(f"users/{uid}")


def add_user(uid, name, ref_by=""):
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "id": uid,
        "name": name,
        "coins": 0,
        "reffer": 0,
        "refferBy": ref_by,
        "adsWatched": 0,
        "vipTier": "free",
        "joinedAt": now,
        "lastBonusAt": "",
    }
    firestore_set(f"users/{uid}", data)
    return data


def update_user(uid, data):
    firestore_set(f"users/{uid}", data)


def vip_multiplier(tier, cfg):
    return float(cfg.get("vipMultipliers", {}).get(tier, 1.0))


# ===================================
# 💬 UI HELPERS
# ===================================

def main_menu_kb():
    buttons = [
        [KeyboardButton("▶️ Ad Dekho")],
        [KeyboardButton("💰 Balance"), KeyboardButton("👥 Refer & Earn")],
        [KeyboardButton("🎁 Bonus"), KeyboardButton("⚙️ Extra")],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def inline_back_home():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_home")]])


# ===================================
# 🤖 BOT COMMANDS
# ===================================

async def post_init(app):
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    name = user.full_name
    args = context.args or []
    ref_by = args[0] if args else ""

    existing = get_user(uid)
    cfg = get_config()
    if not existing:
        add_user(uid, name, ref_by)
        reward = int(cfg["referralReward"])
        update_user(uid, {"coins": reward})
        if ref_by and ref_by != uid:
            ref_user = get_user(ref_by)
            if ref_user:
                update_user(ref_by, {
                    "coins": int(ref_user.get("coins", 0)) + reward,
                    "reffer": int(ref_user.get("reffer", 0)) + 1
                })

    await update.message.reply_text(
        f"👋 Welcome, {name}!\nYou are already registered.\nUse the buttons below to earn and manage your balance.",
        reply_markup=main_menu_kb()
    )


# ===================================
# 🎬 FEATURES
# ===================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower()
    user_id = str(update.effective_user.id)
    cfg = get_config()

    if "ad" in text:
        user = get_user(user_id)
        base = random.randint(int(cfg["adRewardMin"]), int(cfg["adRewardMax"]))
        mult = vip_multiplier(user.get("vipTier", "free"), cfg)
        reward = int(round(base * mult))
        coins = int(user.get("coins", 0)) + reward
        ads = int(user.get("adsWatched", 0)) + 1
        update_user(user_id, {"coins": coins, "adsWatched": ads})
        await update.message.reply_text(
            f"🎬 Ad watched!\nReward: +{reward} coins (base {base} × VIP {mult}x)\nCurrent Balance: {coins} coins",
            reply_markup=inline_back_home()
        )

    elif "bonus" in text:
        user = get_user(user_id)
        last = user.get("lastBonusAt", "")
        can_claim = True
        if last:
            try:
                dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                can_claim = datetime.now(timezone.utc) - dt > timedelta(days=1)
            except:
                can_claim = True
        if not can_claim:
            await update.message.reply_text("⏳ Bonus already claimed today.", reply_markup=main_menu_kb())
            return
        base = int(cfg["bonusReward"])
        mult = vip_multiplier(user.get("vipTier", "free"), cfg)
        reward = int(round(base * mult))
        coins = int(user.get("coins", 0)) + reward
        update_user(user_id, {"coins": coins, "lastBonusAt": datetime.now(timezone.utc).isoformat()})
        await update.message.reply_text(f"🎁 Bonus +{reward} coins!\nCurrent Balance: {coins}", reply_markup=main_menu_kb())

    elif "refer" in text:
        link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
        await update.message.reply_text(
            f"👥 Refer & Earn\nShare this link:\n{link}\nEarn bonus for each referral!",
            disable_web_page_preview=True,
            reply_markup=main_menu_kb(),
        )

    elif "balance" in text:
        user = get_user(user_id)
        await update.message.reply_text(
            f"💰 Coins: {user.get('coins',0)}\nVIP: {user.get('vipTier','free')}",
            reply_markup=main_menu_kb()
        )

    elif "extra" in text:
        cfg = get_config()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👑 VIP Plans", callback_data="vip")],
            [InlineKeyboardButton("🆘 Support", url=cfg.get("supportBot"))],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_home")]
        ])
        await update.message.reply_text("✨ Extra Options:", reply_markup=kb)


async def back_home_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("🏠 Back to menu", reply_markup=main_menu_kb())


async def vip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    cfg = get_config()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("VIP 1", callback_data="vip_set:vip1")],
        [InlineKeyboardButton("VIP 2", callback_data="vip_set:vip2")],
        [InlineKeyboardButton("VIP 3", callback_data="vip_set:vip3")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_home")]
    ])
    await update.callback_query.message.reply_text("👑 Choose your VIP tier:", reply_markup=kb)


async def vip_set_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, tier = update.callback_query.data.split(":")
    uid = str(update.effective_user.id)
    update_user(uid, {"vipTier": tier, "vipActivatedAt": datetime.now(timezone.utc).isoformat()})
    await update.callback_query.answer("VIP activated!")
    await update.callback_query.message.reply_text(f"✅ VIP {tier.upper()} activated!", reply_markup=main_menu_kb())


# ===================================
# 🚀 MAIN
# ===================================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(back_home_cb, pattern="^back_home$"))
    app.add_handler(CallbackQueryHandler(vip_cb, pattern="^vip$"))
    app.add_handler(CallbackQueryHandler(vip_set_cb, pattern="^vip_set:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()  
    
    if doc_id:
        params["documentId"] = doc_id
    url = f"{BASE_URL}/{collection}"
    body = {"fields": {k: _fs_value(v) for k, v in data.items()}}
    r = requests.post(url, params=params, json=body, headers=_fs_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def _run_query(collection: str, where_field: str, op: str, value: Any, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Simple equality queries for counting referrals etc."""
    params = {}
    if FIREBASE_API_KEY:
        params["key"] = FIREBASE_API_KEY
    url = f"{BASE_URL}:runQuery"
    structured_query: Dict[str, Any] = {
        "from": [{"collectionId": collection}],
        "where": {
            "fieldFilter": {
                "field": {"fieldPath": where_field},
                "op": op,  # "EQUAL"
                "value": _fs_value(value),
            }
        },
    }
    if limit:
        structured_query["limit"] = limit
    r = requests.post(url, params=params, json={"structuredQuery": structured_query}, headers=_fs_headers(), timeout=15)
    r.raise_for_status()
    results = []
    for doc in r.json():
        if "document" in doc:
            results.append(_fs_parse(doc["document"].get("fields", {})))
    return results


# Public API for bot features

def get_user(uid: str) -> Optional[Dict[str, Any]]:
    return _get_document("users", uid)


def add_user(uid: str, name: str, ref_by: Optional[str]) -> Dict[str, Any]:
    # Defaults
    joined = _now_ts()
    user_data = {
        "id": uid,
        "name": name,
        "coins": 0,
        "reffer": 0,
        "refferBy": ref_by or "",
        "adsWatched": 0,
        "tasksCompleted": 0,
        "totalWithdrawals": 0,
        "vipTier": "free",
        "vipActivatedAt": "",
        "withdrawalsDone": 0,
        "joinedAt": joined,
        "lastBonusAt": "",
        "banned": False,
    }
    _patch_document("users", uid, user_data)  # idempotent create/replace
    return user_data


def update_user(uid: str, data: Dict[str, Any]):
    _patch_document("users", uid, data, update_mask=list(data.keys()))


def create_withdrawal(uid: str, upi: str, amount: int) -> str:
    doc_id = f"wd_{uid}_{int(datetime.now().timestamp())}_{random.randint(1000,9999)}"
    payload = {
        "userId": uid,
        "upi": upi,
        "amount": int(amount),
        "status": "pending",
        "requestedAt": _now_ts(),
        "processedAt": "",
    }
    _create_document("withdrawals", doc_id, payload)
    return doc_id


def get_config(force_refresh: bool = False) -> Dict[str, Any]:
    global CONFIG_CACHE
    if CONFIG_CACHE and not force_refresh:
        return CONFIG_CACHE
    cfg = _get_document("config", "global") or {}
    # Reasonable fallbacks if config is empty
    cfg.setdefault("referralReward", 10)
    cfg.setdefault("bonusReward", 20)
    cfg.setdefault("adRewardMin", 1)
    cfg.setdefault("adRewardMax", 5)
    cfg.setdefault("adWebsiteURL", "https://example.com")
    cfg.setdefault("supportBot", "https://t.me/ExampleSupportBot")
    cfg.setdefault("minRefForWithdraw", 0)
    cfg.setdefault("vipCosts", {"vip1": 0, "vip2": 0, "vip3": 0})
    cfg.setdefault("vipMultipliers", {"vip1": 1.5, "vip2": 2.0, "vip3": 3.0})
    CONFIG_CACHE = cfg
    return cfg


def get_referral_count(uid: str) -> int:
    try:
        rows = _run_query("users", "refferBy", "EQUAL", uid, limit=None)
        return len(rows)
    except Exception:
        return 0


def vip_multiplier(tier: str, cfg: Dict[str, Any]) -> float:
    if not tier or tier == "free":
        return 1.0
    m = cfg.get("vipMultipliers", {})
    return float(m.get(tier, 1.0))


# ========================
# UI Helpers
# ========================
def main_menu_kb() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton("▶️ Ad Dekho")],
        [KeyboardButton("💰 Balance"), KeyboardButton("👥 Refer & Earn")],
        [KeyboardButton("🎁 Bonus"), KeyboardButton("⚙️ Extra")],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)


def extra_menu_kb(cfg: Dict[str, Any]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton("👑 VIP Plans", callback_data="vip")],
        [InlineKeyboardButton("📊 Stats", callback_data="stats")],
        [InlineKeyboardButton("🆘 Support", url=cfg.get("supportBot", "https://t.me/"))],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_home")],
    ]
    return InlineKeyboardMarkup(btns)


def balance_menu_kb() -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton("🏦 Withdraw Funds", callback_data="withdraw")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_home")],
    ]
    return InlineKeyboardMarkup(btns)


def vip_menu_kb(cfg: Dict[str, Any]) -> InlineKeyboardMarkup:
    costs = cfg.get("vipCosts", {})
    btns = [
        [InlineKeyboardButton(f"VIP 1 • Cost: {costs.get('vip1', 0)}", callback_data="vip_set:vip1")],
        [InlineKeyboardButton(f"VIP 2 • Cost: {costs.get('vip2', 0)}", callback_data="vip_set:vip2")],
        [InlineKeyboardButton(f"VIP 3 • Cost: {costs.get('vip3', 0)}", callback_data="vip_set:vip3")],
        [InlineKeyboardButton("⬅️ Back", callback_data="extra")],
    ]
    return InlineKeyboardMarkup(btns)


# ========================
# Bot Handlers
# ========================

async def post_init(app):
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Registers the user if not exists and applies referral reward if /start <refId>."""
    cfg = get_config()
    user = update.effective_user
    uid = str(user.id)
    name = user.full_name

    args = context.args or []
    ref_by = args[0] if args else ""

    existing = get_user(uid)
    first_time = existing is None
    if first_time:
        add_user(uid, name, ref_by)
        # Apply referral rewards (both sides, simple demo logic)
        reward = int(cfg.get("referralReward", 10))
        # New user gets reward
        update_user(uid, {"coins": reward})
        # Referrer gets reward & referral counter
        if ref_by and ref_by != uid:
            ref_u = get_user(ref_by)
            if ref_u:
                update_user(ref_by, {
                    "coins": int(ref_u.get("coins", 0)) + reward,
                    "reffer": int(ref_u.get("reffer", 0)) + 1
                })

    text = (
        f"👋 Welcome, {name}!\n\n"
        f"{'You were registered successfully.' if first_time else 'You are already registered.'}\n"
        f"Use the buttons below to earn and manage your balance."
    )
    # Show an ad URL button too for quick action
    kb = main_menu_kb()
    await update.effective_chat.send_message(text, reply_markup=kb)


async def home_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🏠 Home", reply_markup=main_menu_kb())


async def ads_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = get_config()
    user = get_user(str(q.from_user.id))
    if not user or user.get("banned"):
        await q.edit_message_text("❌ You are not allowed to use this bot.")
        return

    low = int(cfg.get("adRewardMin", 1))
    high = int(cfg.get("adRewardMax", 5))
    base = random.randint(low, high)
    mult = vip_multiplier(user.get("vipTier", "free"), cfg)
    reward = int(round(base * mult))

    new_coins = int(user.get("coins", 0)) + reward
    new_ads = int(user.get("adsWatched", 0)) + 1
    update_user(user["id"], {"coins": new_coins, "adsWatched": new_ads})

    ad_url = cfg.get("adWebsiteURL", "https://example.com")
    text = (
        f"🎬 Ad watched!\n"
        f"Reward: +{reward} coins (base {base} × VIP {mult}x)\n\n"
        f"Current Balance: {new_coins} coins"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Visit Sponsor", url=ad_url)],
        [InlineKeyboardButton("🎬 Watch More", callback_data="ads")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_home")],
    ])
    await q.edit_message_text(text, reply_markup=kb)


async def bonus_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = get_config()
    user = get_user(str(q.from_user.id))
    if not user or user.get("banned"):
        await q.edit_message_text("❌ You are not allowed to use this bot.")
        return

    last = user.get("lastBonusAt", "")
    can_claim = True
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            can_claim = datetime.now(timezone.utc) - last_dt >= timedelta(days=1)
        except Exception:
            can_claim = True

    if not can_claim:
        await q.edit_message_text("⏳ Daily bonus already claimed. Try again later.", reply_markup=main_menu_kb())
        return

    base = int(cfg.get("bonusReward", 20))
    mult = vip_multiplier(user.get("vipTier", "free"), cfg)
    reward = int(round(base * mult))

    new_coins = int(user.get("coins", 0)) + reward
    update_user(user["id"], {"coins": new_coins, "lastBonusAt": _now_ts()})

    text = (
        f"🎁 Daily Bonus: +{reward} coins (base {base} × VIP {mult}x)\n\n"
        f"Current Balance: {new_coins} coins"
    )
    await q.edit_message_text(text, reply_markup=main_menu_kb())


async def refer_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)
    cfg = get_config()
    reward = int(cfg.get("referralReward", 10))
    link = f"https://t.me/{BOT_USERNAME}?start={uid}"
    refs = get_referral_count(uid)
    text = (
        "👥 Refer & Earn\n\n"
        f"Referral Reward: +{reward} coins to both of you.\n"
        f"Your link:\n{link}\n\n"
        f"Current referrals: {refs}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="back_home")]
    ])
    await q.edit_message_text(text, reply_markup=kb, disable_web_page_preview=True)


async def balance_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = get_user(str(q.from_user.id))
    if not user:
        await q.edit_message_text("❌ Please /start first.")
        return
    text = (
        "💸 Balance\n\n"
        f"Coins: {user.get('coins', 0)}\n"
        f"VIP Tier: {user.get('vipTier', 'free')}\n"
        f"Total Withdrawals: {user.get('totalWithdrawals', 0)}\n"
    )
    await q.edit_message_text(text, reply_markup=balance_menu_kb())


async def extra_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = get_config()
    text = "✨ Extra"
    await q.edit_message_text(text, reply_markup=extra_menu_kb(cfg))


async def stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = get_config()
    user = get_user(str(q.from_user.id))
    if not user:
        await q.edit_message_text("❌ Please /start first.")
        return
    refs = get_referral_count(user["id"])
    text = (
        "📊 Stats\n\n"
        f"Name: {user.get('name')}\n"
        f"Coins: {user.get('coins', 0)}\n"
        f"VIP: {user.get('vipTier', 'free')}\n"
        f"Ads Watched: {user.get('adsWatched', 0)}\n"
        f"Referrals: {refs}\n"
        f"Total Withdrawals: {user.get('totalWithdrawals', 0)}"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="extra")]])
    await q.edit_message_text(text, reply_markup=kb)


async def vip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = get_config()
    info = (
        "👑 VIP Plans (demo)\n\n"
        "Activate any VIP instantly (no payment). Multiplier applies to Ads & Bonus.\n"
    )
    await q.edit_message_text(info, reply_markup=vip_menu_kb(cfg))


async def vip_set_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, tier = q.data.split(":", 1)
    user = get_user(str(q.from_user.id))
    if not user:
        await q.edit_message_text("❌ Please /start first.")
        return
    update_user(user["id"], {"vipTier": tier, "vipActivatedAt": _now_ts()})
    await q.edit_message_text(f"✅ VIP activated: {tier.upper()}", reply_markup=main_menu_kb())


# ============== Withdraw Flow ==============

async def withdraw_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = get_user(str(q.from_user.id))
    if not user:
        await q.edit_message_text("❌ Please /start first.")
        return ConversationHandler.END

    cfg = get_config()
    min_refs = int(cfg.get("minRefForWithdraw", 0))
    refs = get_referral_count(user["id"])
    if refs < min_refs:
        await q.edit_message_text(
            f"⚠️ You need at least {min_refs} referrals to withdraw. Current: {refs}",
            reply_markup=main_menu_kb()
        )
        return ConversationHandler.END

    await q.edit_message_text("🏦 Enter your UPI ID (e.g., username@upi):",
                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_withdraw")]]))
    return WITHDRAW_UPI


async def withdraw_upi_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upi = (update.message.text or "").strip()
    if "@" not in upi or len(upi) < 5:
        await update.message.reply_text("❌ Invalid UPI. Try again or /cancel")
        return WITHDRAW_UPI
    context.user_data["withdraw_upi"] = upi
    await update.message.reply_text("💰 Enter amount to withdraw (coins):")
    return WITHDRAW_AMOUNT


async def withdraw_amount_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(str(update.effective_user.id))
    if not user:
        await update.message.reply_text("❌ Please /start first.")
        return ConversationHandler.END

    try:
        amt = int(update.message.text.strip())
    except Exception:
        await update.message.reply_text("❌ Enter a valid number.")
        return WITHDRAW_AMOUNT

    coins = int(user.get("coins", 0))
    if amt <= 0:
        await update.message.reply_text("❌ Amount must be > 0.")
        return WITHDRAW_AMOUNT
    if amt > coins:
        await update.message.reply_text(f"❌ Insufficient balance. You have {coins} coins.")
        return WITHDRAW_AMOUNT

    upi = context.user_data.get("withdraw_upi", "")
    wd_id = create_withdrawal(user["id"], upi, amt)
    update_user(user["id"], {
        "coins": coins - amt,
        "withdrawalsDone": int(user.get("withdrawalsDone", 0)) + 1,
        "totalWithdrawals": int(user.get("totalWithdrawals", 0)) + amt
    })

    await update.message.reply_text(
        f"✅ Withdrawal request created.\n\n"
        f"ID: {wd_id}\nUPI: {upi}\nAmount: {amt}\nStatus: pending",
        reply_markup=main_menu_kb()
    )
    context.user_data.pop("withdraw_upi", None)
    return ConversationHandler.END


async def withdraw_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Cancelled")
    await q.edit_message_text("❌ Withdraw cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("withdraw_upi", None)
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


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
# Main
# ========================

def build_application():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(home_cb, pattern="^back_home$"))
    app.add_handler(CallbackQueryHandler(ads_cb, pattern="^ads$"))
    app.add_handler(CallbackQueryHandler(bonus_cb, pattern="^bonus$"))
    app.add_handler(CallbackQueryHandler(refer_cb, pattern="^refer$"))
    app.add_handler(CallbackQueryHandler(balance_cb, pattern="^balance$"))
    app.add_handler(CallbackQueryHandler(extra_cb, pattern="^extra$"))
    app.add_handler(CallbackQueryHandler(stats_cb, pattern="^stats$"))
    app.add_handler(CallbackQueryHandler(vip_cb, pattern="^vip$"))
    app.add_handler(CallbackQueryHandler(vip_set_cb, pattern="^vip_set:"))
    app.add_handler(CallbackQueryHandler(withdraw_cancel_cb, pattern="^cancel_withdraw$"))

    # Withdraw conversation
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_start_cb, pattern="^withdraw$")],
        states={
            WITHDRAW_UPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_upi_msg)],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_msg)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        name="withdraw_conv",
        persistent=False,
    )
    app.add_handler(conv)

    app.add_error_handler(error_handler)
    return app


def main():
    app = build_application()
    log.info("Starting bot…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
