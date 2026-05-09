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

from telegram.error import (
    TelegramError,
    RetryAfter
)

from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing")

if not MONGO_URI:
    raise Exception("MONGO_URI missing")

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
    r'https?://[^\s]+'
)

# =========================
# CREATE INDEXES
# =========================

async def create_indexes():

    try:

        await channels_col.create_index(
            "channel_id",
            unique=True
        )

        await history_col.create_index([
            ("channel_id", 1),
            ("post_id", 1)
        ])

        logger.info("Indexes Ready")

    except Exception as e:

        logger.error(e)

# =========================
# LINK CONVERTER
# =========================

async def convert_links(text):

    if not text:
        return text

    urls = URL_PATTERN.findall(text)

    if not urls:
        return text

    for url in urls:

        try:

            if "earnurl.online" in url:
                continue

            short_link = (
                "https://earnurl.online/"
                + str(random.randint(10000,99999))
            )

            text = text.replace(
                url,
                short_link
            )

        except Exception as e:

            logger.error(e)

    return text

# =========================
# START COMMAND
# =========================

async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if (
        ADMIN_ID != 0
        and
        update.effective_user.id != ADMIN_ID
    ):
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

    if (
        ADMIN_ID != 0
        and
        update.effective_user.id != ADMIN_ID
    ):
        return

    if not context.args:

        await update.message.reply_text(
            "Use:\n/addchannel -100xxxxxxxx"
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
                "Already Added"
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

    try:

        if (
            ADMIN_ID != 0
            and
            update.effective_user.id != ADMIN_ID
        ):
            return

        msg = update.message

        text_content = (
            msg.text
            or
            msg.caption
            or
            ""
        )

        waiting = await msg.reply_text(
            "⏳ Processing..."
        )

        converted_text = await convert_links(
            text_content
        )

        duplicate = await posts_col.find_one({

            "$or": [

                {"text": converted_text},

                {"caption": converted_text}

            ]

        })

        if duplicate:

            await waiting.edit_text(
                "⚠ Duplicate Post"
            )

            return

        photo_id = None

        if msg.photo:

            photo_id = (
                msg.photo[-1].file_id
            )

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

        # SEND FINAL CONVERTED MESSAGE

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

        await waiting.delete()

    except Exception as e:

        logger.error(e)

# =========================
# AUTO POSTER
# =========================

async def auto_post_job(app):

    logger.info("Scheduler Running")

    try:

        channels = await channels_col.find().to_list(
            length=1000
        )

        if not channels:
            return

        posts = await posts_col.aggregate([
            {"$sample": {"size": 20}}
        ]).to_list(length=20)

        if not posts:
            return

        total_posts = random.randint(1, 2)

        three_days_ago = (
            datetime.utcnow()
            -
            timedelta(days=3)
        )

        await history_col.delete_many({

            "posted_at": {
                "$lt": three_days_ago
            }

        })

        posted = 0

        for post in posts:

            if posted >= total_posts:
                break

            random.shuffle(channels)

            for channel in channels:

                channel_id = (
                    channel["channel_id"]
                )

                already = await history_col.find_one({

                    "channel_id":
                        channel_id,

                    "post_id":
                        post["_id"],

                    "posted_at": {
                        "$gte":
                            three_days_ago
                    }

                })

                if already:
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

                    posted += 1

                    logger.info(
                        f"Posted -> {channel_id}"
                    )

                    await asyncio.sleep(
                        random.randint(5, 10)
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

                    logger.error(e)

                    continue

                except Exception as e:

                    logger.error(e)

                    continue

    except Exception as e:

        logger.error(e)

# =========================
# KEEP ALIVE
# =========================

async def home(request):

    return web.Response(
        text="Bot Running"
    )

# =========================
# MAIN
# =========================

async def main():

    logger.info("Starting Bot")

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

    web_app = web.Application()

    web_app.router.add_get(
        "/",
        home
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
        f"Web Running : {PORT}"
    )

    while True:

        await asyncio.sleep(3600)

# =========================
# START
# =========================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info("Stopped")

    except Exception as e:

        logger.error(e)
