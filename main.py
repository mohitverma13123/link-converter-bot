"""
EarnURL Telegram Bot — Stable + Auto-Post
File: main.py
Python: 3.10.13

requirements.txt:
    python-telegram-bot==21.4
    motor==3.5.1
    aiohttp==3.9.5
    certifi
    dnspython
"""

import os
import re
import ssl
import asyncio
import logging
import random
from datetime import datetime

import aiohttp
import certifi
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from telegram.error import Forbidden, BadRequest, NetworkError, TimedOut

# ---------------- CONFIG ----------------
BOT_TOKEN   = os.getenv("BOT_TOKEN",   "8297833639:AAFdUSW966A6MjAqNEjuDeGJJf444wiJMVU")
ADMIN_ID    = int(os.getenv("ADMIN_ID", "2091839003"))
EARNURL_API = os.getenv("EARNURL_API", "eu_60b982605ce1300b75250f4d23c8a79b1dacb65e0be080b8")
MONGO_URI   = os.getenv(
    "MONGO_URI",
    "mongodb+srv://mohitverma13123:vOsUq4vUMA0XgwrU@cluster0.gs9rzsf.mongodb.net/earnurl_bot?retryWrites=true&w=majority",
)
PORT        = int(os.getenv("PORT", "10000"))

# EarnURL shortener endpoint
EARNURL_ENDPOINT = "https://earnurl.in/api"

# Auto-post timing
AUTO_POST_MIN_SECONDS = 300   # 5 min
AUTO_POST_MAX_SECONDS = 600   # 10 min
POSTS_PER_ROUND_MIN   = 2
POSTS_PER_ROUND_MAX   = 3

URL_REGEX = re.compile(r"https?://[^\s]+")

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("earnurl-bot")

# ---------------- DB ----------------
mongo = AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo["earnurl_bot"]

posts_col   = db["posts"]
chats_col   = db["chats"]
history_col = db["history"]
users_col   = db["users"]

ssl_ctx = ssl.create_default_context(cafile=certifi.where())

async def ensure_indexes():
    await posts_col.create_index("short_url", unique=True)
    await chats_col.create_index("chat_id", unique=True)
    await history_col.create_index([("chat_id", 1), ("post_id", 1)], unique=True)
    await users_col.create_index("user_id", unique=True)

# ---------------- HELPERS ----------------
def is_admin(uid: int) -> bool:
    return ADMIN_ID and uid == ADMIN_ID

async def shorten_url(long_url: str) -> str | None:
    try:
        params = {"api": EARNURL_API, "url": long_url}
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(EARNURL_ENDPOINT, params=params, ssl=ssl_ctx) as r:
                data = await r.json(content_type=None)
                if isinstance(data, dict):
                    return (
                        data.get("shortenedUrl")
                        or data.get("short")
                        or data.get("shortened_url")
                    )
    except Exception as e:
        log.error("shorten_url error: %s", e)
    return None

def extract_media(message):
    if message.photo:     return "photo",     message.photo[-1].file_id
    if message.video:     return "video",     message.video.file_id
    if message.animation: return "animation", message.animation.file_id
    if message.document:  return "document",  message.document.file_id
    return None, None

async def save_post(short_url, text, media_type, file_id):
    doc = {
        "short_url": short_url,
        "text": text or "",
        "media_type": media_type,
        "file_id": file_id,
        "created_at": datetime.utcnow(),
    }
    try:
        await posts_col.update_one(
            {"short_url": short_url},
            {"$setOnInsert": doc},
            upsert=True,
        )
    except Exception as e:
        log.error("save_post: %s", e)

async def pick_post_for_chat(chat_id: int):
    total = await posts_col.count_documents({})
    if total == 0:
        return None
    posted_ids = [
        h["post_id"] async for h in history_col.find({"chat_id": chat_id}, {"post_id": 1})
    ]
    if len(posted_ids) >= total:
        await history_col.delete_many({"chat_id": chat_id})
        posted_ids = []
    cursor = posts_col.find({"_id": {"$nin": posted_ids}})
    candidates = [p async for p in cursor]
    if not candidates:
        return None
    return random.choice(candidates)

async def mark_posted(chat_id: int, post_id):
    try:
        await history_col.insert_one({
            "chat_id": chat_id,
            "post_id": post_id,
            "posted_at": datetime.utcnow(),
        })
    except Exception:
        pass

async def send_post(bot, chat_id: int, post: dict) -> bool:
    text = post.get("text") or ""
    short = post["short_url"]
    if short and short not in text:
        caption = (text + ("\n\n" if text else "") + short).strip()
    else:
        caption = text or short
    media_type = post.get("media_type")
    file_id = post.get("file_id")
    try:
        if media_type == "photo":
            await bot.send_photo(chat_id, file_id, caption=caption)
        elif media_type == "video":
            await bot.send_video(chat_id, file_id, caption=caption)
        elif media_type == "animation":
            await bot.send_animation(chat_id, file_id, caption=caption)
        elif media_type == "document":
            await bot.send_document(chat_id, file_id, caption=caption)
        else:
            await bot.send_message(chat_id, caption, disable_web_page_preview=False)
        return True
    except (Forbidden, BadRequest) as e:
        log.warning("send_post failed in %s: %s", chat_id, e)
        return False
    except Exception as e:
        log.error("send_post error: %s", e)
        return False

# ---------------- HANDLERS ----------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await users_col.update_one(
        {"user_id": u.id},
        {"$set": {"user_id": u.id, "name": u.full_name, "joined": datetime.utcnow()}},
        upsert=True,
    )
    await update.message.reply_text(
        "👋 Welcome!\n\n"
        "Private chat me link (with optional photo/video + caption) bhejo, "
        "mai shorten karke wapas dunga aur auto-post pool me save kar dunga.\n\n"
        "Channel/Group me admin /addhere bhejke wahan auto-posting on kar sakta hai."
    )

async def cmd_addhere(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chat = update.effective_chat
    await chats_col.update_one(
        {"chat_id": chat.id},
        {"$set": {"chat_id": chat.id, "title": chat.title or chat.type, "added": datetime.utcnow()}},
        upsert=True,
    )
    await update.message.reply_text(f"✅ Registered: {chat.title or chat.id}")

async def cmd_removehere(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await chats_col.delete_one({"chat_id": update.effective_chat.id})
    await update.message.reply_text("🗑️ Removed from auto-post list.")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    p = await posts_col.count_documents({})
    c = await chats_col.count_documents({})
    u = await users_col.count_documents({})
    await update.message.reply_text(f"📊 Posts: {p}\n📣 Chats: {c}\n👤 Users: {u}")

async def cmd_autopostnow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    n = await run_post_round(ctx.application.bot)
    await update.message.reply_text(f"🚀 Posted {n} message(s).")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Sirf private chat me convert karega — group/channel me ignore
    if update.effective_chat.type != "private":
        return
    msg = update.message
    if not msg:
        return

    text = msg.caption or msg.text or ""
    urls = URL_REGEX.findall(text)
    if not urls:
        return

    media_type, file_id = extract_media(msg)
    new_text = text
    short_links = []

    for url in urls:
        short = await shorten_url(url)
        if short:
            new_text = new_text.replace(url, short)
            short_links.append(short)
            await save_post(short, new_text, media_type, file_id)

    if not short_links:
        await msg.reply_text("⚠️ Shorten nahi ho paya. EARNURL_API ya endpoint check karo.")
        return

    try:
        if media_type == "photo":
            await msg.reply_photo(file_id, caption=new_text)
        elif media_type == "video":
            await msg.reply_video(file_id, caption=new_text)
        elif media_type == "animation":
            await msg.reply_animation(file_id, caption=new_text)
        elif media_type == "document":
            await msg.reply_document(file_id, caption=new_text)
        else:
            await msg.reply_text(new_text, disable_web_page_preview=False)
    except Exception as e:
        log.error("reply failed: %s", e)
        await msg.reply_text(new_text)

# ---------------- AUTO-POST WORKER ----------------
async def run_post_round(bot) -> int:
    chats = [c async for c in chats_col.find({})]
    if not chats:
        return 0
    sent = 0
    rounds = random.randint(POSTS_PER_ROUND_MIN, POSTS_PER_ROUND_MAX)
    random.shuffle(chats)
    for chat in chats[:rounds]:
        post = await pick_post_for_chat(chat["chat_id"])
        if not post:
            continue
        ok = await send_post(bot, chat["chat_id"], post)
        if ok:
            await mark_posted(chat["chat_id"], post["_id"])
            sent += 1
        await asyncio.sleep(2)
    return sent

async def auto_post_worker(app):
    await asyncio.sleep(15)
    while True:
        try:
            n = await run_post_round(app.bot)
            log.info("Auto-post round done. Sent=%d", n)
        except Exception as e:
            log.error("auto_post_worker: %s", e)
        await asyncio.sleep(random.randint(AUTO_POST_MIN_SECONDS, AUTO_POST_MAX_SECONDS))

# ---------------- HEALTH SERVER ----------------
async def health(_):
    return web.Response(text="OK")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Health server on :%s", PORT)

# ---------------- MAIN ----------------
async def run_bot_once():
    await ensure_indexes()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("addhere", cmd_addhere))
    app.add_handler(CommandHandler("removehere", cmd_removehere))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("autopostnow", cmd_autopostnow))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL)
        & ~filters.COMMAND,
        handle_message,
    ))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    asyncio.create_task(auto_post_worker(app))

    log.info("✅ Bot started.")
    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

async def main():
    await start_health_server()
    while True:
        try:
            await run_bot_once()
        except (NetworkError, TimedOut) as e:
            log.warning("Network issue, restart in 5s: %s", e)
            await asyncio.sleep(5)
        except Exception as e:
            log.exception("Bot crashed, restart in 10s: %s", e)
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
