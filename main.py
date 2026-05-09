import os
import re
import asyncio
import logging
import random
from datetime import datetime, timedelta

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

EARNURL_ENDPOINT = "https://mgtvdesmjqqrgczgvnbz.supabase.co/functions/v1/shorten-api"

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
posts_col    = db.posts
history_col  = db.history

# ---------------- HTTP SESSION ----------------
HTTP: aiohttp.ClientSession | None = None
URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)

# ---------------- SHORTEN ----------------
async def shorten_one(url: str) -> str:
    try:
        params = {"api": EARNURL_API, "url": url, "mode": "quick"}
        async with HTTP.get(EARNURL_ENDPOINT, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json(content_type=None)
            if isinstance(data, dict) and data.get("short_url"):
                return data["short_url"]
            log.warning(f"shorten failed payload: {data}")
    except Exception as e:
        log.error(f"shorten error for {url}: {e}")
    return url

async def shorten_text(text: str) -> str:
    urls = URL_RE.findall(text)
    if not urls:
        return text
    unique = list(dict.fromkeys(urls))
    mapping = {}
    for u in unique:
        mapping[u] = await shorten_one(u)
    out = text
    for orig, short in mapping.items():
        out = out.replace(orig, short)
    return out

# ---------------- COMMANDS ----------------
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 EarnURL Bot is live!\n\n"
        "Send any message with links — I'll shorten them.\n\n"
        "Admin commands:\n"
        "/addchannel @username or -100xxxx\n"
        "/listchannels\n"
        "/stats"
    )

async def cmd_addchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Admin only.")
    if not ctx.args:
        return await update.message.reply_text("Usage: /addchannel @channel  OR  /addchannel -100123456789")
    ident = ctx.args[0]
    try:
        chat = await ctx.bot.get_chat(ident)
        await channels_col.update_one(
            {"channel_id": chat.id},
            {"$set": {"channel_id": chat.id, "title": chat.title or chat.username, "added_at": datetime.utcnow()}},
            upsert=True,
        )
        await update.message.reply_text(f"✅ Added: {chat.title or chat.username} ({chat.id})")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}\nMake sure bot is admin in that channel.")

async def cmd_listchannels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    items = await channels_col.find({}).to_list(length=500)
    if not items:
        return await update.message.reply_text("No channels added.")
    txt = "📺 Channels:\n" + "\n".join(f"• {c.get('title')} ({c['channel_id']})" for c in items)
    await update.message.reply_text(txt)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    nch = await channels_col.count_documents({})
    npo = await posts_col.count_documents({})
    nhi = await history_col.count_documents({})
    await update.message.reply_text(f"📊 Stats\nChannels: {nch}\nSaved posts: {npo}\nAuto-posts done: {nhi}")

# ---------------- MESSAGE HANDLER ----------------
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    text = msg.text or msg.caption
    if not text:
        return
    if not URL_RE.search(text):
        return

    processing = None
    try:
        processing = await msg.reply_text("⏳ Shortening...")
        converted = await shorten_text(text)

        if update.effective_user and update.effective_user.id == ADMIN_ID:
            await posts_col.insert_one({"text": converted, "created_at": datetime.utcnow()})

        await processing.edit_text(converted, disable_web_page_preview=True)
    except Exception as e:
        log.exception("handle_message failed")
        try:
            if processing:
                await processing.edit_text(f"❌ Error: {e}")
            else:
                await msg.reply_text(f"❌ Error: {e}")
        except Exception:
            pass

# ---------------- AUTO POST ----------------
async def auto_post_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        channels = await channels_col.find({}).to_list(length=500)
        if not channels:
            log.info("auto-post: no channels")
        else:
            posts = await posts_col.find({}).to_list(length=1000)
            if not posts:
                log.info("auto-post: no saved posts")
            else:
                cutoff = datetime.utcnow() - timedelta(days=7)
                pick_channels = random.sample(channels, k=min(len(channels), random.randint(2, 4)))
                for ch in pick_channels:
                    cid = ch["channel_id"]
                    recent = await history_col.find(
                        {"channel_id": cid, "posted_at": {"$gte": cutoff}}
                    ).to_list(length=1000)
                    used_ids = {h["post_id"] for h in recent}
                    fresh = [p for p in posts if p["_id"] not in used_ids]
                    if not fresh:
                        log.info(f"auto-post: no fresh posts for {cid}")
                        continue
                    p = random.choice(fresh)
                    try:
                        await ctx.bot.send_message(chat_id=cid, text=p["text"], disable_web_page_preview=True)
                        await history_col.insert_one({
                            "channel_id": cid, "post_id": p["_id"], "posted_at": datetime.utcnow()
                        })
                        log.info(f"auto-post -> {cid}")
                    except (Forbidden, BadRequest) as e:
                        log.warning(f"removing dead channel {cid}: {e}")
                        await channels_col.delete_one({"channel_id": cid})
                    except Exception as e:
                        log.error(f"auto-post send fail {cid}: {e}")
    except Exception:
        log.exception("auto_post_job crashed")
    finally:
        delay = random.randint(300, 600)
        ctx.job_queue.run_once(auto_post_job, when=delay, name="auto_post")
        log.info(f"next auto-post in {delay}s")

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
    app.job_queue.run_once(auto_post_job, when=30, name="auto_post")
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("listchannels", cmd_listchannels))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))

    log.info("Bot starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
