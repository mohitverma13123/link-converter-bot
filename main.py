# main.py
import os
import re
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType, ParseMode
from telegram.error import Conflict
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ---------------- CONFIG ----------------
BOT_TOKEN       = os.getenv("BOT_TOKEN")
MONGO_URI       = os.getenv("MONGO_URI")
EARNURL_API_KEY = os.getenv("EARNURL_API_KEY")
ADMIN_IDS       = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
PORT            = int(os.getenv("PORT", "8080"))
AUTOPOST_INTERVAL = int(os.getenv("AUTOPOST_INTERVAL", "1800"))  # 30 min default

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ---------------- DB ----------------
mongo = MongoClient(MONGO_URI)
db = mongo["earnurl_bot"]
posts_col    = db["posts"]      # queued posts
channels_col = db["channels"]   # target channels

# ---------------- EARNURL SHORTENER ----------------
URL_RE = re.compile(r"https?://[^\s)]+", re.IGNORECASE)

async def shorten_url(session: aiohttp.ClientSession, long_url: str) -> str:
    """Shorten a single URL via earnurl.online API. Returns short url or original on failure."""
    try:
        api = f"https://earnurl.online/api?api={EARNURL_API_KEY}&url={long_url}"
        async with session.get(api, timeout=15) as r:
            data = await r.json(content_type=None)
            if data.get("status") == "success" and data.get("shortenedUrl"):
                return data["shortenedUrl"]
            log.warning("earnurl failed for %s : %s", long_url, data)
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
            short = await shorten_url(session, u)
            text = text.replace(u, short)
    return text

# ---------------- HANDLERS ----------------
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await update.message.reply_text(
        "👋 Send me any text/photo/video with links — I'll shorten via earnurl.online "
        "and queue for auto-posting to your channels.\n\n"
        "Admin commands:\n"
        "/addchannel <@channel or -100id>\n"
        "/removechannel <@channel or -100id>\n"
        "/listchannels\n"
        "/queue\n"
        "/postnow"
    )

async def add_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /addchannel <@channel or -100id>")
        return
    ch = ctx.args[0]
    channels_col.update_one({"chat_id": ch}, {"$set": {"chat_id": ch}}, upsert=True)
    await update.message.reply_text(f"✅ Added channel: {ch}")

async def remove_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /removechannel <@channel or -100id>")
        return
    ch = ctx.args[0]
    channels_col.delete_one({"chat_id": ch})
    await update.message.reply_text(f"🗑 Removed: {ch}")

async def list_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chs = [c["chat_id"] for c in channels_col.find()]
    await update.message.reply_text("Channels:\n" + ("\n".join(chs) if chs else "(none)"))

async def queue_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    pending = posts_col.count_documents({"posted": False})
    posted  = posts_col.count_documents({"posted": True})
    await update.message.reply_text(f"📦 Pending: {pending}\n✅ Posted: {posted}")

async def postnow_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    n = await autopost_once(ctx.application)
    await update.message.reply_text(f"📤 Posted {n} item(s).")

async def handle_private_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Receive content in DM, convert links, save to queue."""
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if not is_admin(update.effective_user.id):
        return

    msg = update.message
    text = msg.text or msg.caption or ""
    converted = await shorten_all_in_text(text)

    doc = {
        "text": converted,
        "photo": msg.photo[-1].file_id if msg.photo else None,
        "video": msg.video.file_id if msg.video else None,
        "document": msg.document.file_id if msg.document else None,
        "posted": False,
        "created_at": datetime.now(timezone.utc),
    }
    posts_col.insert_one(doc)
    await msg.reply_text("✅ Converted & queued for auto-post.")

# ---------------- AUTOPOST ----------------
async def autopost_once(app: Application) -> int:
    channels = [c["chat_id"] for c in channels_col.find()]
    if not channels:
        return 0
    posts = list(posts_col.find({"posted": False}).sort("created_at", 1).limit(1))
    sent = 0
    for post in posts:
        for ch in channels:
            try:
                if post.get("photo"):
                    await app.bot.send_photo(ch, post["photo"], caption=post.get("text") or "")
                elif post.get("video"):
                    await app.bot.send_video(ch, post["video"], caption=post.get("text") or "")
                elif post.get("document"):
                    await app.bot.send_document(ch, post["document"], caption=post.get("text") or "")
                else:
                    await app.bot.send_message(ch, post.get("text") or "", disable_web_page_preview=False)
            except Exception as e:
                log.error("send to %s failed: %s", ch, e)
        posts_col.update_one({"_id": post["_id"]}, {"$set": {"posted": True, "posted_at": datetime.now(timezone.utc)}})
        sent += 1
    return sent

async def autopost_loop(app: Application):
    while True:
        try:
            n = await autopost_once(app)
            if n:
                log.info("autopost sent %d", n)
        except Exception as e:
            log.error("autopost error: %s", e)
        await asyncio.sleep(AUTOPOST_INTERVAL)

# ---------------- HEALTH SERVER (Render port bind) ----------------
async def health(_req):
    try:
        pending = posts_col.count_documents({"posted": False})
        posted  = posts_col.count_documents({"posted": True})
        chs     = channels_col.count_documents({})
    except Exception:
        pending = posted = chs = -1
    return web.json_response({"ok": True, "pending": pending, "posted": posted, "channels": chs})

async def start_health_server(app: Application):
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    app.bot_data["_health_runner"] = runner
    log.info("Health server listening on :%d", PORT)
    # also start autopost loop
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
        log.error("409 Conflict: another instance is polling with the same BOT_TOKEN. "
                  "Stop the old/local bot or delete the old Render service.")
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
