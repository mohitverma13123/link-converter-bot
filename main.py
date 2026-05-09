import os
import re
import logging
import asyncio
import random
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from telegram.error import FloodWait, TelegramError
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web
import httpx

# Logging Configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
EARNURL_API = os.getenv("EARNURL_API")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN or not MONGO_URI or not EARNURL_API:
    raise ValueError("CRITICAL: BOT_TOKEN, MONGO_URI aur EARNURL_API variables set karein!")

# MongoDB Initialization
client = AsyncIOMotorClient(MONGO_URI)
db = client["tg_autoposter_db"]
posts_col = db["posts"]
channels_col = db["channels"]
history_col = db["history"]

URL_PATTERN = re.compile(r'https?://[^\s]+')

async def convert_links(text: str) -> str:
    """EarnURL API ka use karke text ke saare links ko short links me convert karta hai."""
    if not text or not EARNURL_API:
        return text
    
    urls = URL_PATTERN.findall(text)
    if not urls:
        return text

    async with httpx.AsyncClient() as http_client:
        for url in urls:
            # EarnURL shortener standard API format
            if "earnurl.online" in url:
                continue # Pehle se short kiye gaye links ko skip karein
                
            try:
                api_url = f"earnurl.online{EARNURL_API}&url={url}"
                response = await http_client.get(api_url, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    # EarnURL JSON response handler
                    if data.get("status") == "success" and data.get("shortenedUrl"):
                        short_url = data["shortenedUrl"]
                        text = text.replace(url, short_url)
            except Exception as e:
                logger.error(f"Link short karne me dikkat aayi: {e}")
                continue
                
    return text

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! Main ek advanced Auto-Posting Bot hoon.\n\n"
        "📢 *Commands:*\n"
        "/addchannel <Channel_ID> - Naya channel add karein\n\n"
        "👉 Koi bhi post mujhe forward ya send karein, main use save kar loonga."
    )

async def add_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Format: `/addchannel -100xxxxxxxxx`")
        return
    channel_id = context.args[0].strip()
    try:
        channel_id_int = int(channel_id)
        exists = await channels_col.find_one({"channel_id": channel_id_int})
        if exists:
            await update.message.reply_text("ℹ️ Yeh channel pehle se added hai.")
            return
        await channels_col.insert_one({"channel_id": channel_id_int, "added_at": datetime.utcnow()})
        await update.message.reply_text(f"✅ Channel `{channel_id_int}` successfully add ho gaya!")
    except ValueError:
        await update.message.reply_text("❌ Channel ID sirf numbers me honi chahiye.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text_content = msg.text or msg.caption or ""
    
    # Send waiting message for heavy process
    waiting_msg = await msg.reply_text("⏳ Links convert ho rahe hain aur post save ho rahi hai...")
    
    updated_text = await convert_links(text_content)
    
    post_data = {
        "text": updated_text if msg.text else None,
        "caption": updated_text if msg.caption else None,
        "photo_file_id": msg.photo[-1].file_id if msg.photo else None,
        "saved_at": datetime.utcnow()
    }
    await posts_col.insert_one(post_data)
    await waiting_msg.edit_text("📥 Post successfully convert aur database me save ho gayi!")

async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Auto-posting cycle started...")
    channels = await channels_col.find().to_list(length=100)
    if not channels:
        return
    
    total_posts_to_send = random.randint(2, 5)
    three_days_ago = datetime.utcnow() - timedelta(days=3)
    await history_col.delete_many({"posted_at": {"$lt": three_days_ago}})

    for _ in range(total_posts_to_send):
        pipeline = [{"$sample": {"size": 10}}]
        random_posts = await posts_col.aggregate(pipeline).to_list(length=10)
        if not random_posts:
            break

        post_sent_in_this_turn = False
        for post in random_posts:
            target_channel = random.choice(channels)
            chan_id = target_channel["channel_id"]
            post_id = post["_id"]

            already_posted = await history_col.find_one({
                "channel_id": chan_id,
                "post_id": post_id,
                "posted_at": {"$gte": three_days_ago}
            })
            if already_posted:
                continue

            try:
                if post.get("photo_file_id"):
                    await context.bot.send_photo(chat_id=chan_id, photo=post["photo_file_id"], caption=post.get("caption"))
                elif post.get("text"):
                    await context.bot.send_message(chat_id=chan_id, text=post["text"])
                
                await history_col.insert_one({"channel_id": chan_id, "post_id": post_id, "posted_at": datetime.utcnow()})
                post_sent_in_this_turn = True
                await asyncio.sleep(3)
                break
            except FloodWait as e:
                await asyncio.sleep(e.retry_after)
            except TelegramError:
                continue

async def handle_ping(request):
    return web.Response(text="Bot is Alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

async def main():
    await start_web_server()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addchannel", add_channel_cmd))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, message_handler))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_post_job, "interval", minutes=5, args=[app.job_queue])
    scheduler.start()

    logger.info("Bot starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    import sys
    if sys.platform != "win32":
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
