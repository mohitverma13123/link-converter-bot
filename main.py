```python
import os
import re
import logging
import asyncio
import random
from datetime import datetime, timedelta
from urllib.parse import quote

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

# =========================
# LOGGING
# =========================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# =========================
# ENV VARIABLES
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
EARNURL_API = os.getenv("EARNURL_API")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")

if not MONGO_URI:
    raise ValueError("MONGO_URI missing")

if not EARNURL_API:
    raise ValueError("EARNURL_API missing")

# =========================
# DATABASE
# =========================

client = AsyncIOMotorClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000
)

db = client["telegram_autoposter"]

posts_col = db["posts"]
channels_col = db["channels"]
history_col = db["history"]

# =========================
# URL REGEX
# =========================

URL_PATTERN = re.compile(
    r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+(?:/[^\s]*)?'
)

# =========================
# DATABASE INDEXES
# =========================

async def create_indexes():

    await channels_col.create_index(
        "channel_id",
        unique=True
    )

    await history_col.create_index(
        [("channel_id", 1), ("post_id", 1)]
    )

    logger.info("Indexes Created")

# =========================
# LINK CONVERTER
# =========================

async def convert_links(text: str):

    if not text:
        return text

    urls = URL_PATTERN.findall(text)

    if not urls:
        return text

    async with httpx.AsyncClient() as http_client:

        for url in urls:

            try:

                if "earnurl.online" in url:
                    continue

                api_url = (
                    f"https://earnurl.online"
                    f"{EARNURL_API}"
                    f"&url={quote(url)}"
                )

                response = await http_client.get(
                    api_url,
                    timeout=15
                )

                if response.status_code != 200:
                    continue

                try:

                    data = response.json()

                except Exception:

                    logger.error("Invalid JSON")
                    continue

                if (
                    data.get("status") == "success"
                    and
                    data.get("shortenedUrl")
                ):

                    short_url = data["shortenedUrl"]

                    text = text.replace(
                        url,
                        short_url
                    )

                    logger.info(
                        f"Converted: {url}"
                    )

            except Exception as e:

                logger.error(
                    f"Convert Error: {e}"
                )

                continue

    return text

# =========================
# START COMMAND
# =========================

async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(
        "✅ Bot Online"
    )

# =========================
# ADD CHANNEL
# =========================

async def add_channel_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:

        await update.message.reply_text(
            "Usage:\n/addchannel -100xxxxxxxx"
        )

        return

    try:

        channel_id = int(
            context.args[0]
        )

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
            "✅ Channel Added"
        )

    except Exception as e:

        logger.error(e)

        await update.message.reply_text(
            "❌ Invalid Channel ID"
        )

# =========================
# SAVE POSTS
# =========================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != ADMIN_ID:
        return

    try:

        msg = update.message

        text_content = (
            msg.text
            or
            msg.caption
            or
            ""
        )

        processing = await msg.reply_text(
            "⏳ Processing..."
        )

        converted_text = await convert_links(
            text_content
        )

        # Prevent duplicates

        duplicate = await posts_col.find_one({

            "$or": [

                {"text": converted_text},

                {"caption": converted_text}

            ]

        })

        if duplicate:

            await processing.edit_text(
                "⚠ Duplicate Post"
            )

            return

        photo_id = None

        if msg.photo:

            photo_id = (
                msg.photo[-1].file_id
            )

        # Telegram caption limit

        if len(converted_text) > 1024:

            converted_text = (
                converted_text[:1020]
            )

        post_data = {

            "text":
                converted_text
                if msg.text else None,

            "caption":
                converted_text
                if msg.caption else None,

            "photo_file_id":
                photo_id,

            "saved_at":
                datetime.utcnow()

        }

        await posts_col.insert_one(
            post_data
        )

        await processing.edit_text(
            "✅ Saved"
        )

    except Exception as e:

        logger.error(e)

# =========================
# AUTO POSTER
# =========================

async def auto_post_job(app):

    logger.info(
        "Auto Posting Started"
    )

    try:

        channels = await channels_col.find().to_list(
            length=1000
        )

        if not channels:

            logger.info("No Channels")
            return

        posts = await posts_col.aggregate([
            {"$sample": {"size": 20}}
        ]).to_list(length=20)

        if not posts:

            logger.info("No Posts")
            return

        total_posts = random.randint(1, 2)

        three_days_ago = (
            datetime.utcnow()
            -
            timedelta(days=3)
        )

        # Cleanup old history

        await history_col.delete_many({

            "posted_at": {
                "$lt": three_days_ago
            }

        })

        posted_count = 0

        for post in posts:

            if posted_count >= total_posts:
                break

            random.shuffle(channels)

            for channel in channels:

                channel_id = (
                    channel["channel_id"]
                )

                already_posted = (
                    await history_col.find_one({

                        "channel_id":
                            channel_id,

                        "post_id":
                            post["_id"],

                        "posted_at": {
                            "$gte":
                                three_days_ago
                        }

                    })
                )

                if already_posted:
                    continue

                try:

                    if post.get("photo_file_id"):

                        caption = (
                            post.get("caption")
                            or ""
                        )

                        if len(caption) > 1024:
                            caption = caption[:1020]

                        await app.bot.send_photo(

                            chat_id=channel_id,

                            photo=post["photo_file_id"],

                            caption=caption

                        )

                    elif post.get("text"):

                        await app.bot.send_message(

                            chat_id=channel_id,

                            text=post["text"]

                        )

                    await history_col.insert_one({

                        "channel_id":
                            channel_id,

                        "post_id":
                            post["_id"],

                        "posted_at":
                            datetime.utcnow()

                    })

                    logger.info(
                        f"Posted -> {channel_id}"
                    )

                    posted_count += 1

                    await asyncio.sleep(
                        random.randint(5, 12)
                    )

                    break

                except RetryAfter as e:

                    logger.warning(
                        f"FloodWait {e.retry_after}"
                    )

                    await asyncio.sleep(
                        e.retry_after
                    )

                except TelegramError as e:

                    logger.error(
                        f"Telegram Error: {e}"
                    )

                    continue

                except Exception as e:

                    logger.error(
                        f"Post Error: {e}"
                    )

                    continue

    except Exception as e:

        logger.error(
            f"Scheduler Error: {e}"
        )

# =========================
# KEEP ALIVE
# =========================

async def handle_ping(request):

    return web.Response(
        text="Bot Running"
    )

# =========================
# MAIN
# =========================

async def main():

    logger.info("Starting Bot...")

    await create_indexes()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start_cmd
        )
    )

    app.add_handler(
        CommandHandler(
            "addchannel",
            add_channel_cmd
        )
    )

    app.add_handler(
        MessageHandler(
            (
                filters.TEXT
                |
                filters.PHOTO
            )
            &
            ~filters.COMMAND,

            message_handler
        )
    )

    await app.initialize()

    await app.start()

    await app.updater.start_polling()

    logger.info("Telegram Started")

    # Scheduler

    scheduler = AsyncIOScheduler()

    scheduler.add_job(

        auto_post_job,

        "interval",

        minutes=5,

        args=[app],

        max_instances=1

    )

    scheduler.start()

    logger.info("Scheduler Started")

    # Keep Alive Web Server

    web_app = web.Application()

    web_app.router.add_get(
        "/",
        handle_ping
    )

    runner = web.AppRunner(
        web_app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logger.info(
        f"Web Server Running : {PORT}"
    )

    # Infinite loop

    while True:

        await asyncio.sleep(3600)

# =========================
# START
# =========================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info("Bot Stopped")

    except Exception as e:

        logger.error(e)
```
