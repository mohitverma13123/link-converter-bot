import os
import re
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from pymongo import MongoClient
from telegram import Update
from telegram.constants import ChatType
from telegram.error import Conflict
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ---------------- CONFIG ----------------
BOT_TOKEN       = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI       = os.getenv("MONGO_URI", "").strip()
EARNURL_API_KEY = (os.getenv("EARNURL_API_KEY") or os.getenv("EARNURL_API") or "").strip()

SHORTEN_ENDPOINT = os.getenv(
    "SHORTEN_ENDPOINT",
    "https://mgtvdesmjqqrgczgvnbz.supabase.co/functions/v1/shorten-api",
).strip()

_admins_raw = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or ""
ADMIN_IDS = [int(x) for x in re.split(r"[,\s]+", _admins_raw) if x.strip().lstrip("-").isdigit()]

PORT              = int(os.getenv("PORT", "8080"))
AUTOPOST_INTERVAL = int(os.getenv("AUTOPOST_INTERVAL", "1800"))  # 30 min

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

log.info("Boot config -> admins=%s earnurl_key_set=%s mongo_set=%s endpoint=%s",
         ADMIN_IDS, bool(EARNURL_API_KEY), bool(MONGO_URI), SHORTEN_ENDPOINT)

# ---------------- DB ----------------
mongo = MongoClient(MONGO_URI)
db = mongo["earnurl_bot"]
posts_col    = db["posts"]
channels_col = db["channels"]

# ---------------- SHORTENER ----------------
URL_RE     = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)
EARNURL_RE = re.compile(r"https?://([a-z0-9-]+\.)?earnurl\.online", re.IGNORECASE)

async def shorten_url(session: aiohttp.ClientSession, long_url: str) -> str:
    """Shorten via Supabase shorten-api edge function. Skip already-earnurl links."""
    if EARNURL_RE.search(long_url):
        return long_url
    if not EARNURL_API_KEY:
        log.error("EARNURL_API_KEY missing — cannot shorten")
        return long_url
    try:
        params = {"api": EARNURL_API_KEY, "url": long_url, "mode": "quick"}
        async with session.get(SHORTEN_ENDPOINT, params=params, timeout=20) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                txt = await r.text()
                log.warning("shorten non-json (%s): %s", r.status, txt[:200])
                return long_url
            log.info("shorten resp: %s", data)
            if data.get("ok") and data.get("short_url"):
                return data["short_url"]
            log.warning("shorten failed for %s : %s", long_url, data)
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
            if short and short != u:
                text = text.replace(u, short)
    return text

# ---------------- HANDLERS ----------------
def is_admin(uid: int) -> bool:
    if not ADMIN_IDS:
        return True
    return uid in ADMIN_IDS

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await update.message.reply_text(
        "👋 Send me any text/photo/video with links — I'll shorten via earnurl.online "
        "and queue for auto-posting to your channels.\n\n"
        f"Your user id: {update.effective_user.id}\n\n"
        "Admin commands:\n"
        "/addchannel <@channel or -100id>\n"
        "/removechannel <@channel or -100id>\n"
        "/listchannels\n"
        "/queue\n"
        "/postnow"
    )

def _normalize_channel(ch: str):
    ch = ch.strip()
    if re.fullmatch(r"-?\d+", ch):
        return int(ch)
    if not ch.startswith("@"):
        ch = "@" + ch
    return ch

async def add_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not admin. Your id: " + str(update.effective_user.id))
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /addchannel <@channel or -100id>")
        return
    ch = _normalize_channel(ctx.args[0])
    try:
        chat = await ctx.bot.get_chat(ch)
        ch_id = chat.id
    except Exception as e:
        await update.message.reply_text(
            f"❌ Couldn't access {ch}. Make the bot an admin in that channel first.\nError: {e}"
        )
        return
    channels_col.update_one(
        {"chat_id": ch_id},
        {"$set": {"chat_id": ch_id, "title": getattr(chat, 'title', None),
                  "added_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await update.message.reply_text(f"✅ Added channel: {chat.title or ch_id} ({ch_id})")

async def remove_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /removechannel <@channel or -100id>")
        return
    ch = _normalize_channel(ctx.args[0])
    try:
        chat = await ctx.bot.get_chat(ch)
        ch_id = chat.id
    except Exception:
        ch_id = ch
    res = channels_col.delete_one({"chat_id": ch_id})
    await update.message.reply_text(f"🗑 Removed: {ch_id} (deleted={res.deleted_count})")

async def list_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chs = list(channels_col.find())
    if not chs:
        await update.message.reply_text("Channels: (none)")
        return
    lines = [f"• {c.get('title') or ''} {c['chat_id']}" for c in chs]
    await update.message.reply_text("Channels:\n" + "\n".join(lines))

async def queue_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    pending = posts_col.count_documents({"posted": False})
    posted  = posts_col.count_documents({"posted": True})
    chs     = channels_col.count_documents({})
    await update.message.reply_text(
        f"📦 Pending: {pending}\n✅ Posted: {posted}\n📡 Channels: {chs}"
    )

async def postnow_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    n = await autopost_once(ctx.application)
    await update.message.reply_text(f"📤 Posted {n} item(s).")

async def handle_private_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if not is_admin(update.effective_user.id):
        return

    msg = update.message
    text = msg.text or msg.caption or ""

    has_url = bool(URL_RE.search(text))
    converted = await shorten_all_in_text(text) if has_url else text

    doc = {
        "text": converted,
        "photo": msg.photo[-1].file_id if msg.photo else None,
        "video": msg.video.file_id if msg.video else None,
        "document": msg.document.file_id if msg.document else None,
        "posted": False,
        "created_at": datetime.now(timezone.utc),
    }
    posts_col.insert_one(doc)

    try:
        if doc["photo"]:
            await msg.reply_photo(doc["photo"], caption=converted or None)
        elif doc["video"]:
            await msg.reply_video(doc["video"], caption=converted or None)
        elif doc["document"]:
            await msg.reply_document(doc["document"], caption=converted or None)
        else:
            await msg.reply_text(converted or "(no text)", disable_web_page_preview=False)
    except Exception as e:
        log.warning("reply failed: %s", e)

    note = "✅ Converted & queued." if has_url else "✅ Queued (no links to shorten)."
    if has_url and converted == text:
        note = "⚠️ Queued, but shortener didn't return a short link. Check EARNURL_API_KEY in Render env vars."
    await msg.reply_text(note)

# ---------------- AUTOPOST ----------------
async def autopost_once(app: Application) -> int:
    channels = [c["chat_id"] for c in channels_col.find()]
    if not channels:
        log.info("autopost: no channels")
        return 0
    posts = list(posts_col.find({"posted": False}).sort("created_at", 1).limit(1))
    if not posts:
        log.info("autopost: no pending posts")
        return 0
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
        posts_col.update_one(
            {"_id": post["_id"]},
            {"$set": {"posted": True, "posted_at": datetime.now(timezone.utc)}},
        )
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

# ---------------- HEALTH SERVER ----------------
async def health(_req):
    try:
        pending = posts_col.count_documents({"posted": False})
        posted  = posts_col.count_documents({"posted": True})
        chs     = channels_col.count_documents({})
    except Exception:
        pending = posted = chs = -1
    return web.json_response({"ok": True, "pending": pending, "posted": posted, "channels": chs})

async def start_health_server(app: Application):
    # Force-clear any leftover webhook so getUpdates won't 409
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook cleared on startup")
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)

    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    app.bot_data["_health_runner"] = runner
    log.info("Health server listening on :%d", PORT)
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
        log.error("409 Conflict: another instance is polling with the same BOT_TOKEN.")
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
