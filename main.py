import os
import re
import random
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from pymongo import MongoClient
from telegram import Update
from telegram.constants import ChatType
from telegram.error import Conflict
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ---------------- CONFIG ----------------
BOT_TOKEN       = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI       = os.getenv("MONGO_URI", "").strip()
ADMIN_EARNURL_KEY = (os.getenv("EARNURL_API_KEY") or os.getenv("EARNURL_API") or "").strip()

_admins_raw = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or ""
ADMIN_IDS = [int(x) for x in re.split(r"[,\s]+", _admins_raw) if x.strip().lstrip("-").isdigit()]

PORT              = int(os.getenv("PORT", "8080"))
AUTOPOST_INTERVAL = int(os.getenv("AUTOPOST_INTERVAL", "1800"))
POSTS_PER_CYCLE   = int(os.getenv("POSTS_PER_CYCLE", "3"))

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")
log.info("Boot -> admins=%s admin_key_set=%s mongo_set=%s",
         ADMIN_IDS, bool(ADMIN_EARNURL_KEY), bool(MONGO_URI))

if not MONGO_URI:
    raise SystemExit("MONGO_URI missing!")

mongo = MongoClient(MONGO_URI)
db = mongo["earnurl_bot"]
posts_col    = db["posts"]
channels_col = db["channels"]
meta_col     = db["meta"]
users_col    = db["users"]

posts_col.create_index([("owner_id", 1), ("kind", 1)])
posts_col.create_index("sent_to")
channels_col.create_index([("owner_id", 1), ("chat_id", 1)], unique=True)
users_col.create_index("user_id", unique=True)

URL_RE      = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)
EARNURL_RE  = re.compile(r"https?://([a-z0-9-]+\.)?earnurl\.online", re.IGNORECASE)
TELEGRAM_RE = re.compile(r"https?://(?:t\.me|telegram\.me|telegram\.dog)/[^\s)>\]]+", re.IGNORECASE)
API_KEY_RE  = re.compile(r"^eu_[a-f0-9]{40,64}$", re.IGNORECASE)

SHORTEN_ENDPOINT = os.getenv(
    "SHORTEN_ENDPOINT",
    "https://mgtvdesmjqqrgczgvnbz.supabase.co/functions/v1/shorten-api",
).strip()

def get_user(uid: int) -> dict:
    return users_col.find_one({"user_id": uid}) or {}

def get_user_api_key(uid: int) -> str:
    u = get_user(uid)
    key = (u.get("api_key") or "").strip()
    if key: return key
    if uid in ADMIN_IDS and ADMIN_EARNURL_KEY: return ADMIN_EARNURL_KEY
    return ""

async def shorten_url(session, long_url, api_key):
    if TELEGRAM_RE.match(long_url) or EARNURL_RE.search(long_url): return long_url
    if not api_key: return long_url
    try:
        params = {"api": api_key, "url": long_url, "mode": "quick"}
        async with session.get(SHORTEN_ENDPOINT, params=params, timeout=20) as r:
            try: data = await r.json(content_type=None)
            except Exception: return long_url
            if data.get("ok") and data.get("short_url"): return data["short_url"]
            if data.get("status") == "success" and data.get("shortenedUrl"): return data["shortenedUrl"]
            log.warning("shorten failed: %s", data)
    except Exception as e:
        log.error("shorten_url error: %s", e)
    return long_url

async def shorten_all_in_text(text, api_key):
    if not text: return text
    urls = URL_RE.findall(text)
    if not urls: return text
    async with aiohttp.ClientSession() as s:
        for u in urls:
            if TELEGRAM_RE.match(u): continue
            short = await shorten_url(s, u, api_key)
            if short and short != u: text = text.replace(u, short)
    return text

def is_admin(uid): return uid in ADMIN_IDS

def _normalize_channel(ch):
    ch = ch.strip()
    if re.fullmatch(r"-?\d+", ch): return int(ch)
    if not ch.startswith("@"): ch = "@" + ch
    return ch

def _mask_key(k):
    if not k or len(k) < 10: return "—"
    return k[:6] + "…" + k[-4:]

# ---------------- COMMANDS ----------------
async def start_cmd(update, ctx):
    if update.effective_chat.type != ChatType.PRIVATE: return
    uid = update.effective_user.id
    has_key = bool(get_user_api_key(uid))
    msg = (f"👋 Welcome to EarnURL AutoPost Bot!\nYour id: {uid}\n\n"
           "🎉 Sab kuch FREE — unlimited channels & autopost.\n\n")
    if not has_key:
        msg += ("⚠️ Pehle apna EarnURL API key set karein:\n"
                "1) https://earnurl.online par login\n"
                "2) Dashboard → API Keys → Generate\n"
                "3) /setapi YOUR_KEY\n\n")
    else:
        msg += "✅ API key configured.\n\n"
    msg += ("📋 Commands:\n/setapi <key>\n/myapi\n/removeapi\n"
            "/addchannel <@ch>\n/removechannel <@ch>\n/listchannels\n"
            "/queue\n/postnow\n/stats\n\n"
            f"⏱ Autopost har {AUTOPOST_INTERVAL//60} min, {POSTS_PER_CYCLE} post/channel/cycle.")
    await update.message.reply_text(msg, disable_web_page_preview=True)

async def setapi_cmd(update, ctx):
    if update.effective_chat.type != ChatType.PRIVATE: return
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: /setapi eu_xxxx\nGet key: https://earnurl.online/api-keys"); return
    key = ctx.args[0].strip()
    if not API_KEY_RE.match(key):
        await update.message.reply_text("❌ Invalid format. Example: eu_xxxxxxxxxxxxxxxx"); return
    async with aiohttp.ClientSession() as s:
        test = await shorten_url(s, "https://example.com/earnurl-bot-test", key)
        if "earnurl.online" not in test:
            await update.message.reply_text("❌ Key validate nahi hua. Check earnurl.online par."); return
    users_col.update_one({"user_id": uid},
        {"$set": {"user_id": uid, "api_key": key, "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    await update.message.reply_text(f"✅ API key saved: {_mask_key(key)}\nAb /addchannel se channel jodein.")

async def myapi_cmd(update, ctx):
    if update.effective_chat.type != ChatType.PRIVATE: return
    key = get_user_api_key(update.effective_user.id)
    await update.message.reply_text(f"🔑 Your key: {_mask_key(key)}" if key else "❌ No key. Use /setapi <key>")

async def removeapi_cmd(update, ctx):
    if update.effective_chat.type != ChatType.PRIVATE: return
    users_col.update_one({"user_id": update.effective_user.id}, {"$unset": {"api_key": ""}})
    await update.message.reply_text("🗑 API key removed.")

async def add_channel(update, ctx):
    uid = update.effective_user.id
    if not ctx.args: await update.message.reply_text("Usage: /addchannel <@ch>"); return
    ch = _normalize_channel(ctx.args[0])
    try:
        chat = await ctx.bot.get_chat(ch); ch_id = chat.id
    except Exception as e:
        await update.message.reply_text(f"❌ Can't access {ch}. Bot ko admin banayein.\n{e}"); return
    channels_col.update_one({"owner_id": uid, "chat_id": ch_id},
        {"$set": {"owner_id": uid, "chat_id": ch_id, "title": getattr(chat,'title',None),
                  "added_at": datetime.now(timezone.utc)}}, upsert=True)
    await update.message.reply_text(f"✅ Added: {chat.title or ch_id}")

async def remove_channel(update, ctx):
    uid = update.effective_user.id
    if not ctx.args: await update.message.reply_text("Usage: /removechannel <@ch>"); return
    ch = _normalize_channel(ctx.args[0])
    try: ch_id = (await ctx.bot.get_chat(ch)).id
    except Exception: ch_id = ch
    res = channels_col.delete_one({"owner_id": uid, "chat_id": ch_id})
    await update.message.reply_text(f"🗑 Removed (deleted={res.deleted_count})")

async def list_channels(update, ctx):
    uid = update.effective_user.id
    chs = list(channels_col.find({"owner_id": uid}))
    if not chs: await update.message.reply_text("Channels: (none)\nUse /addchannel"); return
    lines = [f"• {c.get('title') or ''} ({c['chat_id']})" for c in chs]
    await update.message.reply_text(f"Your channels ({len(chs)}):\n" + "\n".join(lines))

async def queue_cmd(update, ctx):
    uid = update.effective_user.id
    media = posts_col.count_documents({"owner_id": uid, "kind": "media"})
    chs   = channels_col.count_documents({"owner_id": uid})
    await update.message.reply_text(
        f"📦 media: {media}\n📡 channels: {chs}\n⏱ every {AUTOPOST_INTERVAL//60}min · {POSTS_PER_CYCLE}/cycle")

async def postnow_cmd(update, ctx):
    uid = update.effective_user.id
    n = await autopost_for_user(ctx.application, uid)
    await update.message.reply_text(f"📤 Sent {n} post(s).")

async def stats_cmd(update, ctx):
    uid = update.effective_user.id
    if is_admin(uid):
        tu = users_col.count_documents({}); tc = channels_col.count_documents({}); tp = posts_col.count_documents({})
        rows = list(channels_col.aggregate([
            {"$group": {"_id": "$owner_id", "channels": {"$sum": 1}}},
            {"$sort": {"channels": -1}}, {"$limit": 30}]))
        body = "\n".join(f"• {r['_id']}: {r['channels']} ch · {posts_col.count_documents({'owner_id': r['_id']})} posts"
                        for r in rows) or "(no channels yet)"
        await update.message.reply_text(f"👑 ADMIN STATS\n👥 users: {tu}\n📡 channels: {tc}\n📦 posts: {tp}\n\nTop users:\n{body}")
    else:
        mc = channels_col.count_documents({"owner_id": uid})
        mp = posts_col.count_documents({"owner_id": uid})
        mm = posts_col.count_documents({"owner_id": uid, "kind": "media"})
        hk = "✅" if get_user_api_key(uid) else "❌"
        await update.message.reply_text(
            f"📊 Your Stats\n🔑 API: {hk}\n📡 channels: {mc}\n📦 posts: {mp} (media: {mm})\n⏱ every {AUTOPOST_INTERVAL//60}min")

async def handle_private_message(update, ctx):
    if update.effective_chat.type != ChatType.PRIVATE: return
    uid = update.effective_user.id
    api_key = get_user_api_key(uid)
    if not api_key:
        await update.message.reply_text("⚠️ Pehle /setapi <key> set karein.\nGet: https://earnurl.online/api-keys"); return
    msg = update.message
    text = msg.text or msg.caption or ""
    converted = await shorten_all_in_text(text, api_key) if URL_RE.search(text) else text
    has_photo = bool(msg.photo); has_video = bool(msg.video); has_doc = bool(msg.document)
    kind = "media" if (has_photo or has_video or has_doc) else "text"
    posts_col.insert_one({"owner_id": uid, "text": converted,
        "photo": msg.photo[-1].file_id if has_photo else None,
        "video": msg.video.file_id if has_video else None,
        "document": msg.document.file_id if has_doc else None,
        "kind": kind, "sent_to": [], "created_at": datetime.now(timezone.utc)})
    try:
        if has_photo:   await msg.reply_photo(msg.photo[-1].file_id, caption=converted or None)
        elif has_video: await msg.reply_video(msg.video.file_id, caption=converted or None)
        elif has_doc:   await msg.reply_document(msg.document.file_id, caption=converted or None)
        else:           await msg.reply_text(converted or "(no text)")
    except Exception as e:
        log.warning("reply failed: %s", e)

# ---------------- AUTOPOST ----------------
async def _send_post(app, ch_id, post):
    try:
        if post.get("photo"):     await app.bot.send_photo(ch_id, post["photo"], caption=post.get("text") or "")
        elif post.get("video"):   await app.bot.send_video(ch_id, post["video"], caption=post.get("text") or "")
        elif post.get("document"):await app.bot.send_document(ch_id, post["document"], caption=post.get("text") or "")
        else:                     await app.bot.send_message(ch_id, post.get("text") or "")
        return True
    except Exception as e:
        log.error("send to %s failed: %s", ch_id, e); return False

def _pick_post(owner_id, ch_id, used):
    q = {"owner_id": owner_id, "kind": "media", "sent_to": {"$ne": ch_id}}
    if used: q["_id"] = {"$nin": list(used)}
    cands = list(posts_col.find(q))
    if not cands:
        q2 = {"owner_id": owner_id, "kind": "media"}
        if used: q2["_id"] = {"$nin": list(used)}
        cands = list(posts_col.find(q2)) or list(posts_col.find({"owner_id": owner_id, "kind": "media"}))
    return random.choice(cands) if cands else None

async def _mark_sent(post, ch_id):
    posts_col.update_one({"_id": post["_id"]},
        {"$addToSet": {"sent_to": ch_id}, "$set": {"last_sent_at": datetime.now(timezone.utc)}})

async def autopost_for_user(app, uid):
    channels = list(channels_col.find({"owner_id": uid}))
    if not channels or posts_col.count_documents({"owner_id": uid, "kind": "media"}) == 0: return 0
    used = set(); sent = 0; random.shuffle(channels)
    for ch in channels:
        for _ in range(POSTS_PER_CYCLE):
            post = _pick_post(uid, ch["chat_id"], used)
            if not post: break
            if await _send_post(app, ch["chat_id"], post):
                used.add(post["_id"]); await _mark_sent(post, ch["chat_id"]); sent += 1
            await asyncio.sleep(0.5)
    return sent

async def autopost_once(app):
    total = 0
    for uid in channels_col.distinct("owner_id"):
        try: total += await autopost_for_user(app, uid)
        except Exception as e: log.error("autopost %s: %s", uid, e)
    return total

async def autopost_loop(app):
    while True:
        try:
            n = await autopost_once(app)
            if n: log.info("autopost sent %d", n)
        except Exception as e: log.error("autopost: %s", e)
        await asyncio.sleep(AUTOPOST_INTERVAL)

# ---------------- HEALTH ----------------
async def health(_req):
    try:
        return web.json_response({"ok": True,
            "users": users_col.count_documents({}),
            "channels": channels_col.count_documents({}),
            "posts": posts_col.count_documents({})})
    except Exception: return web.json_response({"ok": False})

async def start_health_server(app):
    w = web.Application(); w.router.add_get("/", health); w.router.add_get("/health", health)
    runner = web.AppRunner(w); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    app.bot_data["_health_runner"] = runner
    log.info("Health on :%d", PORT)
    app.bot_data["_autopost_task"] = asyncio.create_task(autopost_loop(app))

async def stop_health_server(app):
    r = app.bot_data.get("_health_runner")
    if r: await r.cleanup()
    t = app.bot_data.get("_autopost_task")
    if t: t.cancel()

async def error_handler(update, ctx):
    if isinstance(ctx.error, Conflict):
        log.error("409 Conflict: duplicate polling instance."); return
    log.exception("Unhandled: %s", ctx.error)

# ---------------- MAIN ----------------
def main():
    if not BOT_TOKEN: raise SystemExit("BOT_TOKEN missing")
    app = (Application.builder().token(BOT_TOKEN)
           .post_init(start_health_server).post_shutdown(stop_health_server).build())
    for name, h in [("start",start_cmd),("setapi",setapi_cmd),("myapi",myapi_cmd),
                    ("removeapi",removeapi_cmd),("addchannel",add_channel),
                    ("removechannel",remove_channel),("listchannels",list_channels),
                    ("queue",queue_cmd),("postnow",postnow_cmd),("stats",stats_cmd)]:
        app.add_handler(CommandHandler(name, h))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
