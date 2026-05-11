import os
import re
import json
import asyncio
import logging
import random
from datetime import datetime, timedelta

import aiohttp
import certifi
from motor.motor_asyncio import AsyncIOMotorClient

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ---------------- CONFIG ----------------
BOT_TOKEN     = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI     = os.getenv("MONGO_URI", "").strip()
DB_NAME       = os.getenv("DB_NAME", "earnurl_bot")
SHORTENER_API = os.getenv("SHORTENER_API", "https://earnurl.online/api/shorten").strip()
API_KEY       = os.getenv("EARNURL_API_KEY", "").strip()
ADMIN_IDS     = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
AUTO_POST_SECONDS = int(os.getenv("AUTO_POST_SECONDS", "300"))
BATCH_MIN     = int(os.getenv("BATCH_MIN", "2"))
BATCH_MAX     = int(os.getenv("BATCH_MAX", "4"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("earnurl-bot")

# ---------------- DB ----------------
mongo = AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo[DB_NAME]
posts_col    = db["posts"]      # {text, links, media, status, created_at, posted_to:[chat_id]}
channels_col = db["channels"]   # {chat_id, title, added_by, added_at}

# ---------------- HELPERS ----------------
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
EARNURL_RE = re.compile(r"https?://([a-z0-9-]+\.)?earnurl\.online", re.IGNORECASE)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def has_earnurl_link(text: str) -> bool:
    if not text:
        return False
    return bool(EARNURL_RE.search(text))

def extract_links(text: str):
    if not text:
        return []
    return URL_RE.findall(text)

async def shorten_one(session: aiohttp.ClientSession, url: str) -> str:
    try:
        payload = {"url": url}
        headers = {"Content-Type": "application/json"}
        if API_KEY:
            headers["x-api-key"] = API_KEY
        async with session.post(SHORTENER_API, json=payload, headers=headers, timeout=20) as r:
            data = await r.json(content_type=None)
            short = data.get("short_url") or data.get("shortUrl") or url
            short = short.replace("earnurl.lovable.app", "earnurl.online")
            return short
    except Exception as e:
        log.warning("shorten failed for %s: %s", url, e)
        return url

async def shorten_text(text: str) -> str:
    links = extract_links(text)
    if not links:
        return text
    async with aiohttp.ClientSession() as session:
        mapping = {}
        for u in set(links):
            if EARNURL_RE.match(u):
                mapping[u] = u
            else:
                mapping[u] = await shorten_one(session, u)
    out = text
    for orig, short in mapping.items():
        out = out.replace(orig, short)
    return out

def detect_media(msg):
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    if msg.video:
        return "video", msg.video.file_id
    if msg.animation:
        return "animation", msg.animation.file_id
    if msg.document:
        return "document", msg.document.file_id
    return None, None

# ---------------- HANDLERS ----------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "👋 EarnURL Bot ready.\n\nDM me any link/text/media — I’ll instantly shorten and (if admin) queue it for auto-post."
    )

async def cmd_addchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /addchannel <chat_id or @username>")
        return
    target = ctx.args[0]
    try:
        chat = await ctx.bot.get_chat(target)
        await channels_col.update_one(
            {"chat_id": chat.id},
            {"$set": {"chat_id": chat.id, "title": chat.title or chat.username,
                      "added_by": update.effective_user.id, "added_at": datetime.utcnow()}},
            upsert=True
        )
        await update.message.reply_text(f"✅ Added: {chat.title or chat.username} ({chat.id})")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")

async def cmd_removechannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /removechannel <chat_id>")
        return
    try:
        cid = int(ctx.args[0])
        await channels_col.delete_one({"chat_id": cid})
        await update.message.reply_text(f"🗑 Removed channel {cid}")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    pending = await posts_col.count_documents({"status": "pending"})
    posted  = await posts_col.count_documents({"status": "posted"})
    chans   = await channels_col.count_documents({})
    await update.message.reply_text(
        f"📊 Stats\nPending: {pending}\nPosted: {posted}\nChannels: {chans}"
    )

async def cmd_reset_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    res = await posts_col.update_many({}, {"$set": {"status": "pending", "posted_to": []}})
    await update.message.reply_text(f"♻️ Reset {res.modified_count} posts to pending.")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat

    # STRICT: private chat only. Ignore groups/channels even if user is admin.
    if chat.type != "private":
        return
    if not msg:
        return

    text = msg.text or msg.caption or ""
    media_type, file_id = detect_media(msg)

    # If no link and no media -> ignore silently
    links = extract_links(text)
    if not links and not media_type:
        return

    # Skip if message already contains an earnurl.online link -> no convert, no reply
    if has_earnurl_link(text):
        log.info("Skipping message with existing earnurl.online link from user %s", msg.from_user.id)
        return

    # Convert
    converted = await shorten_text(text) if links else text

    # Reply instantly
    try:
        if media_type == "photo" and file_id:
            await msg.reply_photo(photo=file_id, caption=converted[:1024] if converted else None)
        elif media_type == "video" and file_id:
            await msg.reply_video(video=file_id, caption=converted[:1024] if converted else None)
        elif media_type == "animation" and file_id:
            await msg.reply_animation(animation=file_id, caption=converted[:1024] if converted else None)
        elif media_type == "document" and file_id:
            await msg.reply_document(document=file_id, caption=converted[:1024] if converted else None)
        else:
            await msg.reply_text(converted or "(no text)")
    except Exception as e:
        log.warning("reply failed: %s", e)

    # Save to queue (ALL private DMs that had a link OR media)
    try:
        await posts_col.insert_one({
            "text": converted,
            "original_text": text,
            "media_type": media_type,
            "file_id": file_id,
            "from_user": msg.from_user.id,
            "status": "pending",
            "posted_to": [],
            "created_at": datetime.utcnow(),
        })
        log.info("Queued post from user %s (media=%s)", msg.from_user.id, media_type)
    except Exception as e:
        log.error("queue insert failed: %s", e)

# ---------------- AUTO POST ----------------
async def send_post_to_channel(bot, chat_id: int, post: dict):
    text = post.get("text") or ""
    mtype = post.get("media_type")
    fid = post.get("file_id")
    try:
        if mtype == "photo" and fid:
            await bot.send_photo(chat_id, photo=fid, caption=text[:1024])
        elif mtype == "video" and fid:
            await bot.send_video(chat_id, video=fid, caption=text[:1024])
        elif mtype == "animation" and fid:
            await bot.send_animation(chat_id, animation=fid, caption=text[:1024])
        elif mtype == "document" and fid:
            await bot.send_document(chat_id, document=fid, caption=text[:1024])
        else:
            await bot.send_message(chat_id, text or "(empty)")
        return True
    except Exception as e:
        log.warning("send to %s failed: %s", chat_id, e)
        return False

async def auto_post_job(ctx: ContextTypes.DEFAULT_TYPE):
    bot = ctx.bot
    channels = [c async for c in channels_col.find({})]
    if not channels:
        return

    batch_size = random.randint(BATCH_MIN, BATCH_MAX)

    pending = [p async for p in posts_col.find({"status": "pending"}).limit(batch_size)]
    if not pending:
        # Recycle: oldest posted -> reset to pending
        old = [p async for p in posts_col.find({"status": "posted"}).sort("created_at", 1).limit(batch_size)]
        if not old:
            return
        ids = [p["_id"] for p in old]
        await posts_col.update_many({"_id": {"$in": ids}}, {"$set": {"status": "pending", "posted_to": []}})
        pending = [p async for p in posts_col.find({"_id": {"$in": ids}})]

    for post in pending:
        sent_to = []
        for ch in channels:
            ok = await send_post_to_channel(bot, ch["chat_id"], post)
            if ok:
                sent_to.append(ch["chat_id"])
            await asyncio.sleep(0.5)
        await posts_col.update_one(
            {"_id": post["_id"]},
            {"$set": {"status": "posted", "posted_to": sent_to, "last_posted_at": datetime.utcnow()}}
        )
        log.info("Auto-posted %s to %d channels", post["_id"], len(sent_to))

# ---------------- MAIN ----------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("removechannel", cmd_removechannel))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("reset_queue", cmd_reset_queue))

    # PRIVATE ONLY — group messages completely ignored
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_message
    ))

    # Schedule auto-post
    app.job_queue.run_repeating(auto_post_job, interval=AUTO_POST_SECONDS, first=30)

    log.info("Bot starting (private-only mode)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
