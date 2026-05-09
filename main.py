import os
import re
import logging
import asyncio
import random
from datetime import datetime, timedelta

import certifi
import aiohttp
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError, RetryAfter

# =====================================
# LOGGING
# =====================================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =====================================
# ENV
# =====================================
BOT_TOKEN     = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI     = os.getenv("MONGO_URI", "").strip()
ADMIN_ID      = int(os.getenv("ADMIN_ID", "0"))
EARNURL_API   = os.getenv("EARNURL_API", "").strip()
PORT          = int(os.getenv("PORT", "10000"))

EARNURL_ENDPOINT = "https://mgtvdesmjqqrgczgvnbz.supabase.co/functions/v1/shorten-api"

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing")
if not MONGO_URI:
    raise Exception("MONGO_URI missing")
if not EARNURL_API:
    raise Exception("EARNURL_API missing")

# =====================================
# DB
# =====================================
mongo_client = AsyncIOMotorClient(
    MONGO_URI,
    tls=True,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=30000,
)
db          = mongo_client["earnurl_bot"]
posts_col   = db["posts"]
channels_col = db["channels"]
history_col = db["history"]

# =====================================
# URL REGEX
# =====================================
URL_PATTERN = re.compile(r'https?://[^\s]+')

# Shared aiohttp session (created in main)
HTTP: aiohttp.ClientSession | None = None

# =====================================
# INDEXES
# =====================================
async def create_indexes():
    await channels_col.create_index("channel_id", unique=True)
    await history_col.create_index([("channel_id", 1), ("post_id", 1)])

# =====================================
# REAL EarnURL SHORTENER (calls your API)
# =====================================
async def shorten_one(url: str) -> str:
    """Call EarnURL API and return Diskwala-style short link."""
    try:
        params = {"api": EARNURL_API, "url": url, "mode": "quick"}
        async with HTTP.get(EARNURL_ENDPOINT, params=params, timeout=20) as r:
            data = await r.json()
            if data.get("ok") and data.get("short_url"):
                return data["short_url"]
            logger.error(f"EarnURL API error: {data}")
    except Exception as e:
        logger.error(f"shorten_one failed: {e}")
    return url  # fallback: keep original

async def convert_links(text: str) -> str:
    if not text:
        return text
    urls = URL_PATTERN.findall(text)
    if not urls:
        return text
    # de-duplicate while preserving order
    seen = []
    for u in urls:
        if u not in seen:
            seen.append(u)
    # skip already-shortened
    to_short = [u for u in seen if "earnurl" not in u]
    results = await asyncio.gather(*[shorten_one(u) for u in to_short])
    mapping = dict(zip(to_short, results))
    for original, short in mapping.items():
        text = text.replace(original, short)
    return text

# =====================================
# COMMANDS
# =====================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ EarnURL Bot Online\n\n"
        "Send any text/photo with links → I'll convert them.\n\n"
        "Admin commands:\n"
        "/addchannel -100xxxxxxxxxx\n"
        "/listchannels\n"
        "/stats"
    )

async def add_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Admin Only")
    if not context.args:
        return await update.message.reply_text("Use: /addchannel -100xxxxxxxxxx")
    try:
        channel_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ Invalid Channel ID")
    if await channels_col.find_one({"channel_id": channel_id}):
        return await update.message.reply_text("⚠ Already added")
    await channels_col.insert_one({"channel_id": channel_id, "added_at": datetime.utcnow()})
    await update.message.reply_text(f"✅ Channel added: {channel_id}")

async def list_channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    channels = await channels_col.find().to_list(length=500)
    if not channels:
        return await update.message.reply_text("No channels yet.")
    msg = "📢 Channels:\n" + "\n".join(str(c["channel_id"]) for c in channels)
    await update.message.reply_text(msg)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    p = await posts_col.count_documents({})
    c = await channels_col.count_documents({})
    h = await history_col.count_documents({})
    await update.message.reply_text(f"📊 Posts: {p}\nChannels: {c}\nAuto-posts done: {h}")

# =====================================
# MESSAGE HANDLER (instant convert + save)
# =====================================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    try:
        original_text = msg.text or msg.caption or ""
        if not original_text and not msg.photo:
            return

        converted = await convert_links(original_text)

        photo_id = msg.photo[-1].file_id if msg.photo else None

        await posts_col.insert_one({
            "text":          converted if msg.text else None,
            "caption":       converted if msg.caption else None,
            "photo_file_id": photo_id,
            "saved_at":      datetime.utcnow(),
        })

        # Reply with shortened version
        if photo_id:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=photo_id,
                caption=converted or None,
            )
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=converted or "✅ Saved",
                disable_web_page_preview=False,
            )
    except Exception as e:
        logger.error(f"message_handler error: {e}")
        try:
            await msg.reply_text(f"❌ Error: {e}")
        except Exception:
            pass

# =====================================
# AUTO POST JOB (every 5–10 min, 2–4 posts, no repeat in 7 days)
# =====================================
async def auto_post_job(app):
    try:
        channels = await channels_col.find().to_list(length=1000)
        if not channels:
            return
        posts = await posts_col.aggregate([{"$sample": {"size": 50}}]).to_list(length=50)
        if not posts:
            return

        total_posts = random.randint(2, 4)
        seven_days_ago = datetime.utcnow() - timedelta(days=7)

        for _ in range(total_posts):
            random.shuffle(channels)
            random.shuffle(posts)
            posted = False
            for post in posts:
                for channel in channels:
                    cid = channel["channel_id"]
                    pid = str(post["_id"])
                    exists = await history_col.find_one({
                        "channel_id": cid,
                        "post_id": pid,
                        "posted_at": {"$gte": seven_days_ago},
                    })
                    if exists:
                        continue
                    try:
                        if post.get("photo_file_id"):
                            await app.bot.send_photo(
                                chat_id=cid,
                                photo=post["photo_file_id"],
                                caption=post.get("caption") or "",
                            )
                        else:
                            await app.bot.send_message(
                                chat_id=cid,
                                text=post.get("text") or "",
                            )
                        await history_col.insert_one({
                            "channel_id": cid,
                            "post_id": pid,
                            "posted_at": datetime.utcnow(),
                        })
                        logger.info(f"Auto-posted to {cid}")
                        posted = True
                        await asyncio.sleep(3)
                        break
                    except RetryAfter as e:
                        await asyncio.sleep(e.retry_after)
                    except TelegramError as e:
                        logger.error(f"Telegram error on {cid}: {e}")
                if posted:
                    break
    except Exception as e:
        logger.error(f"auto_post_job error: {e}")

def schedule_next(scheduler, app):
    """Reschedule with random 5–10 minute gap (infinite mode)."""
    delay = random.randint(5, 10)
    scheduler.add_job(
        _run_then_reschedule,
        "date",
        run_date=datetime.utcnow() + timedelta(minutes=delay),
        args=[scheduler, app],
        id=f"autopost_{datetime.utcnow().timestamp()}",
        max_instances=1,
    )
    logger.info(f"Next auto-post in {delay} min")

async def _run_then_reschedule(scheduler, app):
    await auto_post_job(app)
    schedule_next(scheduler, app)

# =====================================
# WEB SERVER (keep-alive for Render)
# =====================================
async def home(request):
    return web.Response(text="EarnURL Bot Running ✅")

async def init_web():
    web_app = web.Application()
    web_app.router.add_get("/", home)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server on port {PORT}")

# =====================================
# MAIN
# =====================================
async def main():
    global HTTP
    HTTP = aiohttp.ClientSession()

    logger.info("Connecting MongoDB...")
    await create_indexes()
    logger.info("MongoDB ready")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addchannel", add_channel_cmd))
    app.add_handler(CommandHandler("listchannels", list_channels_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
        message_handler,
    ))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot started")

    scheduler = AsyncIOScheduler()
    scheduler.start()
    schedule_next(scheduler, app)
    logger.info("Scheduler started (5–10 min random)")

    await init_web()

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
