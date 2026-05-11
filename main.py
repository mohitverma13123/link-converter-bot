import os
import re
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
from telegram.error import Forbidden, BadRequest

# ---------------- CONFIG ----------------
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
EARNURL_API  = os.environ["EARNURL_API"]
MONGO_URI    = os.environ["MONGO_URI"]
PORT         = int(os.environ.get("PORT", "10000"))

EARNURL_ENDPOINT  = "https://mgtvdesmjqqrgczgvnbz.supabase.co/functions/v1/shorten-api"
AUTO_POST_SECONDS = int(os.environ.get("AUTO_POST_SECONDS", "300"))   # 5 min
BATCH_MIN         = int(os.environ.get("BATCH_MIN", "2"))             # 2 posts min
BATCH_MAX         = int(os.environ.get("BATCH_MAX", "4"))             # 4 posts max

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("earnurl-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# ---------------- DB ----------------
mongo = AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo.earnurl_bot
channels_col = db.channels
posts_col    = db.posts   # status: "pending" | "posted"

# ---------------- HTTP ----------------
HTTP: aiohttp.ClientSession | None = None
URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)

# ---------------- HELPERS ----------------
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def get_message_text_or_caption(msg) -> str:
    return msg.text or msg.caption or ""

def detect_media(msg):
    if msg.photo:     return "photo",     msg.photo[-1].file_id
    if msg.video:     return "video",     msg.video.file_id
    if msg.animation: return "animation", msg.animation.file_id
    if msg.document:  return "document",  msg.document.file_id
    return None, None

async def send_post(bot, chat_id, post):
    text       = post.get("text") or ""
    media_type = post.get("media_type")
    file_id    = post.get("file_id")

    if media_type == "photo" and file_id:
        return await bot.send_photo(chat_id=chat_id, photo=file_id,
                                    caption=text[:1024], disable_web_page_preview=True)
    if media_type == "video" and file_id:
        return await bot.send_video(chat_id=chat_id, video=file_id, caption=text[:1024])
    if media_type == "animation" and file_id:
        return await bot.send_animation(chat_id=chat_id, animation=file_id, caption=text[:1024])
    if media_type == "document" and file_id:
        return await bot.send_document(chat_id=chat_id, document=file_id, caption=text[:1024])

    return await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)

async def reply_converted_message(msg, converted_text: str):
    media_type, file_id = detect_media(msg)
    if media_type == "photo" and file_id:
        return await msg.reply_photo(photo=file_id, caption=converted_text[:1024])
    if media_type == "video" and file_id:
        return await msg.reply_video(video=file_id, caption=converted_text[:1024])
    if media_type == "animation" and file_id:
        return await msg.reply_animation(animation=file_id, caption=converted_text[:1024])
    if media_type == "document" and file_id:
        return await msg.reply_document(document=file_id, caption=converted_text[:1024])
    return await msg.reply_text(converted_text, disable_web_page_preview=True)

# ---------------- SHORTEN ----------------
async def shorten_one(url: str) -> str:
    try:
        params = {"api": EARNURL_API, "url": url, "mode": "quick"}
        async with HTTP.get(EARNURL_ENDPOINT, params=params,
                            timeout=aiohttp.ClientTimeout(total=25)) as r:
            data = await r.json(content_type=None)
            if isinstance(data, dict) and data.get("short_url"):
                short_url = data["short_url"]
                short_url = short_url.replace("https://earnurl.lovable.app", "https://earnurl.online")
                short_url = short_url.replace("http://earnurl.lovable.app",  "https://earnurl.online")
                return short_url
            log.warning(f"shorten failed payload: {data}")
    except Exception as e:
        log.error(f"shorten error for {url}: {e}")
    return url

async def shorten_text(text: str) -> str:
    urls = URL_RE.findall(text)
    if not urls:
        return text
    unique_urls = list(dict.fromkeys(urls))
    mapping = {u: await shorten_one(u) for u in unique_urls}
    out = text
    for original, short in mapping.items():
        out = out.replace(original, short)
    return out

# ---------------- COMMANDS ----------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "👋 EarnURL Bot live!\n\n"
        "DM me any text/photo/video with links — instantly converted.\n\n"
        "Admin commands:\n"
        "/addchannel @username or -100xxxx\n"
        "/listchannels\n"
        "/stats\n"
        "/reset_queue  → mark all posts pending again"
    )

async def cmd_addchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Admin only.")
    if not ctx.args:
        return await update.message.reply_text("Usage: /addchannel @channel  OR  /addchannel -100123456789")

    ident = ctx.args[0]
    try:
        chat = await ctx.bot.get_chat(ident)
        await channels_col.update_one(
            {"channel_id": chat.id},
            {"$set": {
                "channel_id": chat.id,
                "title": chat.title or chat.username or str(chat.id),
                "username": chat.username,
                "added_at": datetime.utcnow(),
            }},
            upsert=True,
        )
        await update.message.reply_text(f"✅ Added: {chat.title or chat.username or chat.id}\nID: {chat.id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}\n\nMake sure bot is admin in the channel.")

async def cmd_listchannels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Admin only.")
    items = await channels_col.find({}).to_list(length=500)
    if not items:
        return await update.message.reply_text("No channels added.")
    txt = "📺 Channels:\n\n" + "\n".join(
        f"• {c.get('title','Unknown')} ({c['channel_id']})" for c in items)
    await update.message.reply_text(txt)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Admin only.")
    nch     = await channels_col.count_documents({})
    pending = await posts_col.count_documents({"status": "pending"})
    posted  = await posts_col.count_documents({"status": "posted"})
    await update.message.reply_text(
        f"📊 Stats\n\n"
        f"Channels: {nch}\n"
        f"Pending posts: {pending}\n"
        f"Posted posts: {posted}\n"
        f"Auto-post: every {AUTO_POST_SECONDS//60} min, batch {BATCH_MIN}-{BATCH_MAX}"
    )

async def cmd_reset_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Admin only.")
    res = await posts_col.update_many({}, {"$set": {"status": "pending"}})
    await update.message.reply_text(f"♻️ Reset {res.modified_count} posts to pending.")

# ---------------- MESSAGE HANDLER (PRIVATE ONLY) ----------------
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    # STRICT: only private chats. Ignore groups/channels.
    if update.effective_chat.type != "private":
        return

    text = get_message_text_or_caption(msg)
    if not text or not URL_RE.search(text):
        return

    processing = None
    try:
        processing = await msg.reply_text("⏳ Shortening...")
        converted = await shorten_text(text)
        media_type, file_id = detect_media(msg)

        # Save EVERY converted post to queue as pending
        await posts_col.insert_one({
            "text": converted,
            "media_type": media_type,
            "file_id": file_id,
            "source_user_id": update.effective_user.id if update.effective_user else None,
            "status": "pending",
            "created_at": datetime.utcnow(),
            "posted_at": None,
        })

        try:
            await processing.delete()
        except Exception:
            pass

        await reply_converted_message(msg, converted)

    except Exception as e:
        log.exception("handle_message failed")
        try:
            if processing:
                await processing.edit_text(f"❌ Error: {e}")
            else:
                await msg.reply_text(f"❌ Error: {e}")
        except Exception:
            pass

# ---------------- AUTO POST (BATCH → ALL CHANNELS) ----------------
async def auto_post_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        channels = await channels_col.find({}).to_list(length=500)
        if not channels:
            log.info("auto-post: no channels")
            return

        batch_size = random.randint(BATCH_MIN, BATCH_MAX)

        # 1. Try fresh pending posts first
        pending = await posts_col.find({"status": "pending"}).to_list(length=batch_size * 3)

        # 2. Infinite mode: if no pending, recycle oldest posted
        if not pending:
            log.info("auto-post: no pending → recycling oldest posted")
            recycled = await posts_col.find({"status": "posted"}) \
                                      .sort("posted_at", 1) \
                                      .to_list(length=batch_size * 3)
            if not recycled:
                log.info("auto-post: db is empty")
                return
            # Mark them pending again so the same loop logic works
            ids = [p["_id"] for p in recycled]
            await posts_col.update_many(
                {"_id": {"$in": ids}},
                {"$set": {"status": "pending"}}
            )
            pending = recycled

        # Pick batch
        batch = random.sample(pending, k=min(batch_size, len(pending)))

        for post in batch:
            sent_to_any = False
            for ch in channels:
                cid = ch["channel_id"]
                try:
                    await send_post(ctx.bot, cid, post)
                    sent_to_any = True
                    log.info(f"auto-post → {cid} (post {post['_id']})")
                    # Tiny stagger to avoid Telegram flood limits
                    await asyncio.sleep(0.4)
                except (Forbidden, BadRequest) as e:
                    log.warning(f"removing dead channel {cid}: {e}")
                    await channels_col.delete_one({"channel_id": cid})
                except Exception as e:
                    log.error(f"send fail {cid}: {e}")
                    # don't stop — continue scheduler

            # Mark as posted ONLY if at least one channel got it
            if sent_to_any:
                await posts_col.update_one(
                    {"_id": post["_id"]},
                    {"$set": {"status": "posted", "posted_at": datetime.utcnow()}}
                )

    except Exception:
        log.exception("auto_post_job crashed")

# ---------------- KEEPALIVE WEB ----------------
async def health(_req):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"web on :{PORT}")

# ---------------- LIFECYCLE ----------------
async def on_startup(app):
    global HTTP
    HTTP = aiohttp.ClientSession()
    await start_web()
    app.job_queue.run_repeating(
        auto_post_job,
        interval=AUTO_POST_SECONDS,
        first=30,
        name="auto_post_batch",
    )
    log.info("startup complete")

async def on_shutdown(app):
    global HTTP
    if HTTP:
        await HTTP.close()

# ---------------- MAIN ----------------
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("addchannel",   cmd_addchannel))
    app.add_handler(CommandHandler("listchannels", cmd_listchannels))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CommandHandler("reset_queue",  cmd_reset_queue))

    # Only private chats — groups/channels strictly ignored
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_message
    ))

    log.info("Bot starting polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
if __name__ == "__main__":
    main()
