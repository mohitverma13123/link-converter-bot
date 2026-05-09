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
from telegram.error import TelegramError, RetryAfter
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web
import httpx

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
EARNURL_API = os.getenv("EARNURL_API")
PORT = int(os.getenv("PORT", "8080"))

ADMIN_ID = 2091839003

if not BOT_TOKEN or not MONGO_URI or not EARNURL_API:
    raise ValueError("CRITICAL: Environment variables missing on Render!")

client = AsyncIOMotorClient(MONGO_URI)
db = client["tg_autoposter_db"]
posts_col = db["posts"]
channels_col = db["channels"]
history_col = db["history"]

URL_PATTERN = re.compile(r'https?://[^\s]+')

async def convert_links(text: str) -> str:
    if not text or not EARNURL_API:
        return text
    urls = URL_PATTERN.findall(text)
    if not urls:
        return text
    async with httpx.AsyncClient() as http_client:
        for url in urls:
            if "earnurl.online" in url:
                continue
            try:
                api_url = f"earnurl.online{EARNURL_API}&url={url}"
                response = await http_client.get(api_url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("status") == "success" and data.get("shortenedUrl"):
                        text = text.replace(url, data["shortenedUrl"])
            except Exception as e:
                logger.error(f"Shortener error: {e}")
                continue
    return text

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("👋 Live!\n/addchannel <ID> se channels add karein.")

async def add_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Format: /addchannel -100xxxxxxx")
        return
    channel_id = context.args[0].strip()
    try:
        channel_id_int = int(channel_id)
        exists = await channels_col.find_one({"channel_id": channel_id_int})
        if exists:
            await update.message.reply_text("ℹ️ Exists.")
            return
        await channels_col.insert_one({"channel_id": channel_id_int, "added_at": datetime.utcnow()})
        await update.message.reply_text("✅ Added!")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = update.message
    text_content = msg.text or msg.caption or ""
    waiting_msg = await msg.reply_text("⏳ Processing...")
    updated_text = await convert_links(text_content)
    
    post_data = {
        "text": updated_text if msg.text else None,
        "caption": updated_text if msg.caption else None,
        "photo_file_id": msg.photo[-1].file_id if msg.photo else None,
        "saved_at": datetime.utcnow()
    }
    await posts_col.insert_one(post_data)
    await waiting_msg.edit_text("📥 Saved!")

async def auto_post_job(app):
    logger.info("Auto-post execution...")
    channels = await channels_col.find().to_list(length=100)
    if not channels:
        return
    total_posts = random.randint(2, 5)
    three_days_ago = datetime.utcnow() - timedelta(days=3)
    await history_col.delete_many({"posted_at": {"$lt": three_days_ago}})
    for _ in range(total_posts):
        pipeline = [{"$sample": {"size": 10}}]
        random_posts = await posts_col.aggregate(pipeline).to_list(length=10)
        if not random_posts:
            break
        for post in random_posts:
            target = random.choice(channels)
            chan_id = target["channel_id"]
            if await history_col.find_one({"channel_id": chan_id, "post_id": post["_id"], "posted_at": {"$gte": three_days_ago}}):
                continue
            try:
                if post.get("photo_file_id"):
                    await app.bot.send_photo(chat_id=chan_id, photo=post["photo_file_id"], caption=post.get("caption"))
                elif post.get("text"):
                    await app.bot.send_message(chat_id=chan_id, text=post["text"])
                await history_col.insert_one({"channel_id": chan_id, "post_id": post["_id"], "posted_at": datetime.utcnow()})
                await asyncio.sleep(3)
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramError:
                continue

async def handle_ping(request):
    return web.Response(text="Alive")

async def main():
    # Native combined async runtime builder
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addchannel", add_channel_cmd))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, message_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_post_job, "interval", minutes=5, args=[app])
    scheduler.start()

    webapp = web.Application()
    webapp.router.add_get("/", handle_ping)
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Single Engine Async Core System Online.")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
if __name__ == "__main__":
    asyncio.run(main())
