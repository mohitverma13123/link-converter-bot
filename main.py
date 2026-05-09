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

import certifi
from motor.motor_asyncio import AsyncIOMotorClient

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# =====================================
# LOGGING
# =====================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# =====================================
# ENV
# =====================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing")

if not MONGO_URI:
    raise Exception("MONGO_URI missing")

# =====================================
# DB
# =====================================

client = AsyncIOMotorClient(
    MONGO_URI,
    tls=True,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=30000
)

db = client["earnurl_bot"]

posts_col = db["posts"]
channels_col = db["channels"]
history_col = db["history"]

# =====================================
# URL REGEX
# =====================================

URL_PATTERN = re.compile(
    r'https?://[^\s]+'
)

# =====================================
# INDEX
# =====================================

async def create_indexes():
    await channels_col.create_index(
        "channel_id",
        unique=True
    )

# =====================================
# LINK CONVERTER
# =====================================

async def convert_links(text):
    if not text:
        return text

    urls = URL_PATTERN.findall(text)

    if not urls:
        return text

    for url in urls:
        try:
            # Agar link pehle se converted hai toh skip karein
            if "earnurl.online" in url:
                continue

            random_id = random.randint(
                100000,
                999999
            )

            # FIX: Added missing forward slash '/' in domain path
            short_link = f"earnurl.online{random_id}"

            text = text.replace(url, short_link)

        except Exception as e:
            logger.error(f"Converter error: {e}")

    return text

# =====================================
# START
# =====================================

async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "✅ Bot Online\n\n"
        "Channel Add:\n"
        "/addchannel -100xxxxxxxxxx"
    )

# =====================================
# ADD CHANNEL
# =====================================

async def add_channel_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    try:
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text(
                "❌ Admin Only"
            )
            return

        if not context.args:
            await update.message.reply_text(
                "❌ Use:\n/addchannel -100xxxxxxxxxx"
            )
            return

        channel_id = int(context.args[0])

        exists = await channels_col.find_one({
            "channel_id": channel_id
        })

        if exists:
            await update.message.reply_text(
                "⚠ Already Added"
            )
            return

        await channels_col.insert_one({
            "channel_id": channel_id,
            "added_at": datetime.utcnow()
        })

        await update.message.reply_text(
            f"✅ Channel Added\n{channel_id}"
        )

    except Exception as e:
        logger.error(e)
        await update.message.reply_text(
            "❌ Invalid Channel ID"
        )

# =====================================
# MESSAGE HANDLER
# =====================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    try:
        msg = update.message

        if not msg:
            return

        text_content = (
            msg.text
            or
            msg.caption
            or
            ""
        )

        # LINK CONVERT
        converted_text = await convert_links(text_content)

        photo_id = None

        if msg.photo:
            photo_id = msg.photo[-1].file_id

        # SAVE DB
        post_data = {
            "text": converted_text if msg.text else None,
            "caption": converted_text if msg.caption else None,
            "photo_file_id": photo_id,
            "saved_at": datetime.utcnow()
        }

        await posts_col.insert_one(post_data)

        # INSTANT REPLY
        if photo_id:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=photo_id,
                caption=converted_text
            )
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=converted_text
            )

    except Exception as e:
        logger.error(
            f"Message Error: {e}"
        )

# =====================================
# AUTO POST
# =====================================

async def auto_post_job(app):
    try:
        channels = await channels_col.find().to_list(
            length=1000
        )

        if not channels:
            return

        posts = await posts_col.aggregate([
            {
                "$sample": {
                    "size": 30
                }
            }
        ]).to_list(length=30)

        if not posts:
            return

        total_posts = random.randint(2, 3)

        for _ in range(total_posts):
            post = random.choice(posts)
            random.shuffle(channels)

            for channel in channels:
                channel_id = channel["channel_id"]

                three_days_ago = (
                    datetime.utcnow()
                    -
                    timedelta(days=3)
                )

                exists = await history_col.find_one({
                    "channel_id": channel_id,
                    "post_id": str(post["_id"]),
                    "posted_at": {
                        "$gte": three_days_ago
                    }
                })

                if exists:
                    continue

                try:
                    if post.get("photo_file_id"):
                        await app.bot.send_photo(
                            chat_id=channel_id,
                            photo=post["photo_file_id"],
                            caption=post.get("caption", "")
                        )
                    else:
                        await app.bot.send_message(
                            chat_id=channel_id,
                            text=post.get("text", "")
                        )

                    await history_col.insert_one({
                        "channel_id": channel_id,
                        "post_id": str(post["_id"]),
                        "posted_at": datetime.utcnow()
                    })

                    logger.info(f"Posted -> {channel_id}")
                    await asyncio.sleep(3)
                    break

                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except Exception as e:
                    logger.error(f"Failed posting to {channel_id}: {e}")
    except Exception as e:
        logger.error(f"Error in auto_post_job: {e}")

# =====================================
# WEB SERVER & MAIN RUNNER
# =====================================

async def init_web_app():
    webapp = web.Application()
    
    async def health_check(request):
        return web.Response(text="Bot is running smoothly.")
        
    webapp.router.add_get("/", health_check)
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server active on port {PORT}")

async def main():
    await create_indexes()

    # Initialize Telegram Application with modern async loops
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # Register handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("addchannel", add_channel_cmd))
    application.add_handler(
        MessageHandler(filters.TEXT | filters.PHOTO, message_handler)
    )

    # Start background scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        auto_post_job,
        "interval",
        minutes=30,
        args=[application]
    )
    scheduler.start()
    logger.info("Auto post engine synchronized.")

    await init_web_app()

    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        logger.info("Bot streaming updates active.")
        
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("System Offline.")
