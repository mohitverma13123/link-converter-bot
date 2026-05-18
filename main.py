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
EARNURL_API_KEY = (os.getenv("EARNURL_API_KEY") or os.getenv("EARNURL_API") or "").strip()

_admins_raw = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or ""
ADMIN_IDS = [int(x) for x in re.split(r"[,\s]+", _admins_raw) if x.strip().lstrip("-").isdigit()]

PORT              = int(os.getenv("PORT", "8080"))
AUTOPOST_INTERVAL = int(os.getenv("AUTOPOST_INTERVAL", "1800"))  # 30 min

POSTS_PER_CHANNEL_PER_CYCLE = int(os.getenv("POSTS_PER_CHANNEL_PER_CYCLE", "3"))
PROMO_EVERY_N_SENDS         = int(os.getenv("PROMO_EVERY_N_SENDS", "25"))
PROMO_LINK_THRESHOLD        = int(os.getenv("PROMO_LINK_THRESHOLD", "5"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

log.info("Boot -> admins=%s earnurl_key_set=%s mongo_set=%s",
         ADMIN_IDS, bool(EARNURL_API_KEY), bool(MONGO_URI))

if not MONGO_URI:
    raise SystemExit("MONGO_URI missing! Without it the bot forgets channels/posts on every deploy.")

# ---------------- DB ----------------
mongo = MongoClient(MONGO_URI)
db = mongo["earnurl_bot"]
posts_col    = db["posts"]
channels_col = db["channels"]
meta_col     = db["meta"]

posts_col.create_index("sent_to")
posts_col.create_index("kind")
channels_col.create_index("chat_id", unique=True)

# ---------------- LINK REGEX ----------------
URL_RE     = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)
EARNURL_RE = re.compile(r"https?://([a-z0-9-]+\.)?earnurl\.online", re.IGNORECASE)
TELEGRAM_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me|telegram\.dog)/[^\s)>\]]+",
    re.IGNORECASE,
)

SHORTEN_ENDPOINT = os.getenv(
    "SHORTEN_ENDPOINT",
    "https://mgtvdesmjqqrgczgvnbz.supabase.co/functions/v1/shorten-api",
).strip()

async def shorten_url(session: aiohttp.ClientSession, long_url: str) -> str:
    if TELEGRAM_RE.match(long_url):
        return long_url
    if EARNURL_RE.search(long_url):
        return long_url
    if not EARNURL_API_KEY:
        log.error("EARNURL_API_KEY missing")
        return long_url
    try:
        params = {"api": EARNURL_API_KEY, "url": long_url, "mode": "quick"}
        async with session.get(SHORTEN_ENDPOINT, params=params, timeout=20) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                return long_url
            if data.get("ok") and data.get("short_url"):
                return data["short_url"]
            if data.get("status") == "success" and data.get("shortenedUrl"):
                return data["shortenedUrl"]
            log.warning("shorten failed: %s", data)
    except Exception as e:
        log.error("shorten_url error: %s", e)
    return long_url

async def shorten_all_in_text(text: str) -> str:
    if not text:
        return text
    urls = URL_RE.findall(text)
    if not urls:
        return text
    async with aiohttp.ClientSession() as session:
        for u in urls:
            if TELEGRAM_RE.match(u):
                continue
            short = await shorten_url(session, u)
            if short and short != u:
                text = text.replace(u, short)
    return text

def count_telegram_links(text: str) -> int:
    if not text:
        return 0
    return len(TELEGRAM_RE.findall(text))

# ---------------- HANDLERS ----------------
def is_admin(uid: int) -> bool:
    if not ADMIN_IDS:
        return True
    return uid in ADMIN_IDS

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await update.message.reply_text(
        "👋 Send any photo/video with links — I'll shorten and queue.\n"
        f"Your user id: {update.effective_user.id}\n\n"
        "/addchannel <@channel or -100id>\n"
        "/removechannel <@channel or -100id>\n"
        "/listchannels\n/queue\n/postnow"
    )

def _normalize_channel(ch: str):
    ch = ch.strip()
    if re.fullmatch(r"-?\d+", ch):
        return int(ch)
    if not ch.startswith("@"):
        ch = "@" + ch
    return ch

async def add_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not admin. Your id: " + str(update.effective_user.id))
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /addchannel <@channel or -100id>")
        return
    ch = _normalize_channel(ctx.args[0])
    try:
        chat = await ctx.bot.get_chat(ch)
        ch_id = chat.id
    except Exception as e:
        await update.message.reply_text(f"❌ Couldn't access {ch}. Make the bot admin there.\n{e}")
        return
    channels_col.update_one(
        {"chat_id": ch_id},
        {"$set": {"chat_id": ch_id, "title": getattr(chat, 'title', None),
                  "added_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await update.message.reply_text(f"✅ Added: {chat.title or ch_id} ({ch_id})")

async def remove_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /removechannel <@channel or -100id>")
        return
    ch = _normalize_channel(ctx.args[0])
    try:
        chat = await ctx.bot.get_chat(ch)
        ch_id = chat.id
    except Exception:
        ch_id = ch
    res = channels_col.delete_one({"chat_id": ch_id})
    await update.message.reply_text(f"🗑 Removed: {ch_id} (deleted={res.deleted_count})")

async def list_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chs = list(channels_col.find())
    if not chs:
        await update.message.reply_text("Channels: (none)")
        return
    lines = [f"• {c.get('title') or ''} {c['chat_id']}" for c in chs]
    await update.message.reply_text("Channels:\n" + "\n".join(lines))

async def queue_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    media = posts_col.count_documents({"kind": "media"})
    promo = posts_col.count_documents({"kind": "promo"})
    text  = posts_col.count_documents({"kind": "text"})
    chs   = channels_col.count_documents({})
    counter = (meta_col.find_one({"_id": "counter"}) or {}).get("sends_since_promo", 0)
    await update.message.reply_text(
        f"📦 media: {media}\n📣 promo: {promo}\n📝 text(skipped): {text}\n"
        f"📡 channels: {chs}\n🔁 sends since last promo: {counter}/{PROMO_EVERY_N_SENDS}"
    )

async def postnow_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    n = await autopost_once(ctx.application)
    await update.message.reply_text(f"📤 Sent {n} post(s).")

def classify_post(has_media: bool, text: str) -> str:
    if has_media:
        return "media"
    if count_telegram_links(text) >= PROMO_LINK_THRESHOLD:
        return "promo"
    return "text"

async def handle_private_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if not is_admin(update.effective_user.id):
        return

    msg = update.message
    text = msg.text or msg.caption or ""
    converted = await shorten_all_in_text(text) if URL_RE.search(text) else text

    has_photo = bool(msg.photo)
    has_video = bool(msg.video)
    has_doc   = bool(msg.document)
    kind = classify_post(has_photo or has_video or has_doc, converted)

    doc = {
        "text": converted,
        "photo": msg.photo[-1].file_id if has_photo else None,
        "video": msg.video.file_id if has_video else None,
        "document": msg.document.file_id if has_doc else None,
        "kind": kind,
        "sent_to": [],
        "created_at": datetime.now(timezone.utc),
    }
    posts_col.insert_one(doc)

    try:
        if doc["photo"]:
            await msg.reply_photo(doc["photo"], caption=converted or None)
        elif doc["video"]:
            await msg.reply_video(doc["video"], caption=converted or None)
        elif doc["document"]:
            await msg.reply_document(doc["document"], caption=converted or None)
        else:
            await msg.reply_text(converted or "(no text)", disable_web_page_preview=False)
    except Exception as e:
        log.warning("reply failed: %s", e)

# ---------------- AUTOPOST ----------------
async def _send_post(app: Application, ch_id, post) -> bool:
    try:
        if post.get("photo"):
            await app.bot.send_photo(ch_id, post["photo"], caption=post.get("text") or "")
        elif post.get("video"):
            await app.bot.send_video(ch_id, post["video"], caption=post.get("text") or "")
        elif post.get("document"):
            await app.bot.send_document(ch_id, post["document"], caption=post.get("text") or "")
        else:
            await app.bot.send_message(ch_id, post.get("text") or "", disable_web_page_preview=False)
        return True
    except Exception as e:
        log.error("send to %s failed: %s", ch_id, e)
        return False

def _pick_post_for_channel(ch_id, used_this_round: set, kind: str):
    query = {"kind": kind, "sent_to": {"$ne": ch_id}}
    if used_this_round:
        query["_id"] = {"$nin": list(used_this_round)}
    candidates = list(posts_col.find(query))
    if not candidates:
        q2 = {"kind": kind}
        if used_this_round:
            q2["_id"] = {"$nin": list(used_this_round)}
        candidates = list(posts_col.find(q2))
        if not candidates:
            candidates = list(posts_col.find({"kind": kind}))
    if not candidates:
        return None
    return random.choice(candidates)

async def _mark_sent(post, ch_id):
    posts_col.update_one(
        {"_id": post["_id"]},
        {"$addToSet": {"sent_to": ch_id},
         "$set": {"last_sent_at": datetime.now(timezone.utc)}},
    )

def _bump_counter(delta: int) -> int:
    res = meta_col.find_one_and_update(
        {"_id": "counter"},
        {"$inc": {"sends_since_promo": delta}},
        upsert=True,
        return_document=True,
    )
    return (res or {}).get("sends_since_promo", 0)

def _reset_counter():
    meta_col.update_one({"_id": "counter"}, {"$set": {"sends_since_promo": 0}}, upsert=True)

async def _broadcast_promo(app: Application, channels):
    log.info("Broadcasting telegram-promo to %d channels", len(channels))
    promo_used = set()
    for ch in channels:
        ch_id = ch["chat_id"]
        promo = _pick_post_for_channel(ch_id, promo_used, "promo")
        if not promo:
            continue
        ok = await _send_post(app, ch_id, promo)
        if ok:
            promo_used.add(promo["_id"])
            await _mark_sent(promo, ch_id)
        await asyncio.sleep(0.5)

async def autopost_once(app: Application) -> int:
    channels = list(channels_col.find())
    if not channels:
        log.info("autopost: no channels")
        return 0

    media_total = posts_col.count_documents({"kind": "media"})
    promo_total = posts_col.count_documents({"kind": "promo"})
    if media_total == 0 and promo_total == 0:
        log.info("autopost: no media/promo posts in pool")
        return 0

    sent_count = 0
    random.shuffle(channels)
    used_this_round = set()

    for ch in channels:
        ch_id = ch["chat_id"]
        for _ in range(POSTS_PER_CHANNEL_PER_CYCLE):
            post = _pick_post_for_channel(ch_id, used_this_round, "media")
            if not post:
                break
            ok = await _send_post(app, ch_id, post)
            if ok:
                used_this_round.add(post["_id"])
                await _mark_sent(post, ch_id)
                sent_count += 1
                count = _bump_counter(1)
                if count >= PROMO_EVERY_N_SENDS and promo_total > 0:
                    await _broadcast_promo(app, channels)
                    _reset_counter()
            await asyncio.sleep(0.5)

    return sent_count

async def autopost_loop(app: Application):
    while True:
        try:
            n = await autopost_once(app)
            if n:
                log.info("autopost sent %d post(s)", n)
        except Exception as e:
            log.error("autopost error: %s", e)
        await asyncio.sleep(AUTOPOST_INTERVAL)

# ---------------- HEALTH ----------------
async def health(_req):
    try:
        total = posts_col.count_documents({})
        chs   = channels_col.count_documents({})
    except Exception:
        total = chs = -1
    return web.json_response({"ok": True, "posts": total, "channels": chs})

async def start_health_server(app: Application):
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    app.bot_data["_health_runner"] = runner
    log.info("Health on :%d", PORT)
    app.bot_data["_autopost_task"] = asyncio.create_task(autopost_loop(app))

async def stop_health_server(app: Application):
    runner = app.bot_data.get("_health_runner")
    if runner:
        await runner.cleanup()
    task = app.bot_data.get("_autopost_task")
    if task:
        task.cancel()

# ---------------- ERRORS ----------------
async def error_handler(update, ctx):
    err = ctx.error
    if isinstance(err, Conflict):
        log.error("409 Conflict: another instance is polling with the same BOT_TOKEN.")
        return
    log.exception("Unhandled error: %s", err)

# ---------------- MAIN ----------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(start_health_server)
        .post_shutdown(stop_health_server)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("removechannel", remove_channel))
    app.add_handler(CommandHandler("listchannels", list_channels))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("postnow", postnow_cmd))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))
    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
