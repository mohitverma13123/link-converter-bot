import os
import re
import random
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, OperationFailure
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.error import Conflict
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------------- CONFIG ----------------
BOT_TOKEN         = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI         = os.getenv("MONGO_URI", "").strip()
ADMIN_EARNURL_KEY = (os.getenv("EARNURL_API_KEY") or os.getenv("EARNURL_API") or "").strip()

_admins_raw = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or ""
ADMIN_IDS = [int(x) for x in re.split(r"[,\s]+", _admins_raw) if x.strip().lstrip("-").isdigit()]

PORT              = int(os.getenv("PORT", "8080"))
AUTOPOST_INTERVAL = int(os.getenv("AUTOPOST_INTERVAL", "1800"))  # 30 min default
POSTS_PER_CYCLE   = int(os.getenv("POSTS_PER_CYCLE", "3"))

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

log.info("Boot -> admins=%s admin_key_set=%s mongo_set=%s",
         ADMIN_IDS, bool(ADMIN_EARNURL_KEY), bool(MONGO_URI))

if not MONGO_URI:
    raise SystemExit("MONGO_URI missing!")

# ---------------- DB ----------------
mongo = MongoClient(MONGO_URI)
db = mongo[os.getenv("MONGO_DB", "earnurl_bot")]
posts_col    = db["posts"]
channels_col = db["channels"]
meta_col     = db["meta"]
users_col    = db["users"]

DEFAULT_OWNER = ADMIN_IDS[0] if ADMIN_IDS else 0


def migrate_legacy_docs():
    """Purane docs (bina owner_id ke) ko admin ke naam kar do + legacy indexes fix."""
    try:
        if DEFAULT_OWNER:
            r1 = channels_col.update_many({"owner_id": {"$exists": False}},
                                          {"$set": {"owner_id": DEFAULT_OWNER}})
            r2 = channels_col.update_many({"owner_id": None},
                                          {"$set": {"owner_id": DEFAULT_OWNER}})
            r3 = posts_col.update_many({"owner_id": {"$exists": False}},
                                       {"$set": {"owner_id": DEFAULT_OWNER}})
            r4 = posts_col.update_many({"owner_id": None},
                                       {"$set": {"owner_id": DEFAULT_OWNER}})
            log.info("migration: channels=%d posts=%d",
                     r1.modified_count + r2.modified_count,
                     r3.modified_count + r4.modified_count)

        # legacy field name support: channel_id -> chat_id
        for c in channels_col.find({"chat_id": {"$exists": False}}):
            cid = c.get("channel_id") or c.get("id")
            if cid is not None:
                channels_col.update_one({"_id": c["_id"]}, {"$set": {"chat_id": cid}})

        # purane unique indexes hata do (ye naye add ko block karte the)
        for name, spec in list(channels_col.index_information().items()):
            if name == "_id_":
                continue
            keys = [k for k, _ in spec.get("key", [])]
            if keys != ["owner_id", "chat_id"]:
                try:
                    channels_col.drop_index(name)
                    log.info("dropped legacy index %s", name)
                except OperationFailure as e:
                    log.warning("drop index %s failed: %s", name, e)
    except Exception as e:
        log.error("migration failed: %s", e)


migrate_legacy_docs()

try:
    posts_col.create_index([("owner_id", 1), ("kind", 1)])
    posts_col.create_index("sent_to")
    users_col.create_index("user_id", unique=True)
    channels_col.create_index([("owner_id", 1), ("chat_id", 1)], unique=True)
except Exception as e:
    log.warning("index setup: %s", e)

# ---------------- REGEX ----------------
URL_RE      = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)
EARNURL_RE  = re.compile(r"https?://([a-z0-9-]+\.)?earnurl\.online", re.IGNORECASE)
TELEGRAM_RE = re.compile(r"https?://(?:t\.me|telegram\.me|telegram\.dog)/[^\s)>\]]+", re.IGNORECASE)
API_KEY_RE  = re.compile(r"^eu_[a-f0-9]{40,64}$", re.IGNORECASE)

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://mgtvdesmjqqrgczgvnbz.supabase.co").strip()
SUPABASE_ANON_KEY = os.getenv(
    "SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1ndHZkZXNtanFxcmdjemd2bmJ6Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQ5NzQ1ODEsImV4cCI6MjA5MDU1MDU4MX0.gBcSUSNxIUJZ71UVlrDD6QYrKp4pyOkgjgnliMj9gSE",
).strip()

EARNURL_LINK_RE = re.compile(
    r"https?://(?:www\.)?earnurl\.online/(?:([a-z0-9_-]+)/)?([A-Za-z0-9_-]+)", re.IGNORECASE)

SHORTEN_ENDPOINT = os.getenv(
    "SHORTEN_ENDPOINT",
    "https://mgtvdesmjqqrgczgvnbz.supabase.co/functions/v1/shorten-api",
).strip()


# ---------------- USER ----------------
def get_user(uid: int) -> dict:
    return users_col.find_one({"user_id": uid}) or {}


def get_user_api_key(uid: int) -> str:
    key = (get_user(uid).get("api_key") or "").strip()
    if key:
        return key
    if uid in ADMIN_IDS and ADMIN_EARNURL_KEY:
        return ADMIN_EARNURL_KEY
    return ""


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def owner_filter(uid: int) -> dict:
    """Admin ko sab kuch dikhe (legacy docs samet), normal user ko sirf apna."""
    if is_admin(uid):
        return {"$or": [{"owner_id": uid}, {"owner_id": {"$in": ADMIN_IDS}},
                        {"owner_id": {"$exists": False}}, {"owner_id": None}]}
    return {"owner_id": uid}


# ---------------- SHORTEN ----------------
async def shorten_url(session: aiohttp.ClientSession, long_url: str, api_key: str) -> str:
    if TELEGRAM_RE.match(long_url) or EARNURL_RE.search(long_url):
        return long_url
    if not api_key:
        return long_url
    try:
        params = {"api": api_key, "url": long_url, "mode": "quick"}
        async with session.get(SHORTEN_ENDPOINT, params=params, timeout=20) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                return long_url
            if data.get("ok") and data.get("short_url"):
                return data["short_url"]
            if data.get("status") == "success" and data.get("shortenedUrl"):
                return data["shortenedUrl"]
            log.warning("shorten failed: %s", data)
    except Exception as e:
        log.error("shorten_url error: %s", e)
    return long_url


async def shorten_all_in_text(text: str, api_key: str) -> str:
    if not text:
        return text
    urls = URL_RE.findall(text)
    if not urls:
        return text
    async with aiohttp.ClientSession() as session:
        for u in urls:
            if TELEGRAM_RE.match(u):
                continue
            short = await shorten_url(session, u, api_key)
            if short and short != u:
                text = text.replace(u, short)
    return text


# ---------------- LINK ALIVE CHECK ----------------
def _extract_slugs(text: str):
    slugs = []
    for m in EARNURL_LINK_RE.finditer(text or ""):
        slug = (m.group(2) or "").strip("/")
        if slug and slug.lower() not in ("api-keys", "dashboard", "login", "signup", "blog", "games"):
            slugs.append(slug)
    return slugs


async def _slug_alive(session: aiohttp.ClientSession, slug: str) -> bool:
    try:
        async with session.post(
            f"{SUPABASE_URL}/rest/v1/rpc/link_exists",
            json={"_slug": slug},
            headers={"apikey": SUPABASE_ANON_KEY,
                     "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                     "Content-Type": "application/json"},
            timeout=15,
        ) as r:
            data = await r.json(content_type=None)
            return data is True
    except Exception as e:
        log.warning("link_exists check failed for %s: %s", slug, e)
        return True  # network issue -> don't delete


async def post_links_alive(post) -> bool:
    slugs = _extract_slugs(post.get("text") or "")
    if not slugs:
        return True
    async with aiohttp.ClientSession() as s:
        for slug in slugs:
            if not await _slug_alive(s, slug):
                return False
    return True


async def cleanup_dead_posts(owner_query=None) -> int:
    q = owner_query or {}
    removed = 0
    for post in list(posts_col.find(q)):
        if not await post_links_alive(post):
            posts_col.delete_one({"_id": post["_id"]})
            removed += 1
    return removed


# ---------------- HELPERS ----------------
def _normalize_channel(ch: str):
    ch = ch.strip()
    if re.fullmatch(r"-?\d+", ch):
        return int(ch)
    ch = ch.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "")
    if not ch.startswith("@"):
        ch = "@" + ch
    return ch


def _mask_key(k: str) -> str:
    if not k or len(k) < 10:
        return "—"
    return k[:6] + "…" + k[-4:]


def save_channel(uid: int, ch_id, title=None) -> bool:
    try:
        channels_col.update_one(
            {"owner_id": uid, "chat_id": ch_id},
            {"$set": {"owner_id": uid, "chat_id": ch_id, "title": title,
                      "added_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        return True
    except DuplicateKeyError:
        return True
    except Exception as e:
        log.error("save_channel failed: %s", e)
        return False


# ---------------- CONTROL PANEL ----------------
def _panel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Post Now (all my channels)", callback_data="postnow")],
        [InlineKeyboardButton("🗑 Delete ALL Terabox links", callback_data="purge_tb_ask")],
        [InlineKeyboardButton("🧹 Clean dead posts", callback_data="cleanposts"),
         InlineKeyboardButton("📦 Queue", callback_data="queue")],
    ])


async def purge_terabox_for_key(api_key: str) -> dict:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{SUPABASE_URL}/rest/v1/rpc/purge_terabox_links_by_key",
                json={"_api_key": api_key},
                headers={"apikey": SUPABASE_ANON_KEY,
                         "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                         "Content-Type": "application/json"},
                timeout=60,
            ) as r:
                data = await r.json(content_type=None)
                return data if isinstance(data, dict) else {"ok": False, "error": str(data)}
    except Exception as e:
        log.error("purge_terabox failed: %s", e)
        return {"ok": False, "error": str(e)}


def _remove_terabox_posts(uid: int) -> int:
    q = dict(owner_filter(uid))
    q["text"] = {"$regex": "1024terabox", "$options": "i"}
    return posts_col.delete_many(q).deleted_count


async def panel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await update.message.reply_text("🎛 Control Panel", reply_markup=_panel_markup())


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data or ""
    await q.answer()

    if data == "postnow":
        chs = channels_col.count_documents(owner_filter(uid))
        posts = posts_col.count_documents(owner_filter(uid))
        if chs == 0:
            await q.message.reply_text("❌ Koi channel add nahi hai. /addchannel @yourchannel",
                                       reply_markup=_panel_markup())
            return
        if posts == 0:
            await q.message.reply_text("❌ Queue khali hai. Photo/video + link bhejein.",
                                       reply_markup=_panel_markup())
            return
        await q.message.reply_text(f"📤 Posting to {chs} channel(s)…")
        n = await autopost_for_user(ctx.application, uid)
        await q.message.reply_text(f"✅ Sent {n} post(s).", reply_markup=_panel_markup())

    elif data == "queue":
        total = posts_col.count_documents(owner_filter(uid))
        media = posts_col.count_documents({**owner_filter(uid), "kind": "media"})
        chs = channels_col.count_documents(owner_filter(uid))
        await q.message.reply_text(f"📦 posts: {total} (media: {media})\n📡 channels: {chs}",
                                   reply_markup=_panel_markup())

    elif data == "cleanposts":
        await q.message.reply_text("🧹 Checking queue for deleted links…")
        n = await cleanup_dead_posts(owner_filter(uid))
        await q.message.reply_text(f"🗑 Removed {n} post(s) with deleted links.",
                                   reply_markup=_panel_markup())

    elif data == "purge_tb_ask":
        await q.message.reply_text(
            "⚠️ Ye aapke saare 1024terabox.com short links delete kar dega, "
            "aur unke posts autopost queue se bhi hat jayenge. Confirm?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, delete all", callback_data="purge_tb_yes"),
                 InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]),
        )

    elif data == "purge_tb_yes":
        key = get_user_api_key(uid)
        if not key:
            await q.message.reply_text("❌ Pehle /setapi <key> set karein.")
            return
        await q.message.reply_text("🗑 Deleting all Terabox links…")
        res = await purge_terabox_for_key(key)
        if res.get("ok"):
            removed = _remove_terabox_posts(uid)
            dead = await cleanup_dead_posts(owner_filter(uid))
            await q.message.reply_text(
                f"✅ Deleted {res.get('deleted', 0)} Terabox short link(s).\n"
                f"🧹 Queue se {removed + dead} post(s) hataye.",
                reply_markup=_panel_markup())
        else:
            await q.message.reply_text(f"❌ Failed: {res.get('error')}", reply_markup=_panel_markup())

    elif data == "cancel":
        await q.message.reply_text("Cancelled.", reply_markup=_panel_markup())


async def purgeterabox_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await update.message.reply_text(
        "⚠️ Saare 1024terabox.com short links delete karein?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, delete all", callback_data="purge_tb_yes"),
             InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]),
    )


# ---------------- COMMANDS ----------------
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    has_key = bool(get_user_api_key(uid))

    msg = (f"👋 Welcome to EarnURL AutoPost Bot!\nYour id: {uid}\n\n"
           "🎉 Sab kuch FREE hai — unlimited channels, unlimited autopost.\n\n")
    if not has_key:
        msg += ("⚠️ Pehle apna EarnURL API key set karein:\n"
                "1) https://earnurl.online par login karein\n"
                "2) Dashboard → API Keys → Generate\n"
                "3) Yahan bhejein: /setapi YOUR_KEY\n\n")
    else:
        msg += "✅ Aapka API key configured hai.\n\n"

    msg += ("📋 Commands:\n"
            "/setapi <key> — apna EarnURL API key save\n"
            "/myapi — apna saved key dekhein\n"
            "/removeapi — key hata dein\n"
            "/addchannel <@channel or -100id> — channel jodein (ya channel se koi post forward karein)\n"
            "/removechannel <@channel> — channel hatayein\n"
            "/listchannels — apne channels dekhein\n"
            "/queue — queue status\n"
            "/postnow — abhi post bhejein\n"
            "/stats — stats\n"
            "/cleanposts — dead links wale posts hatayein\n"
            "/panel — buttons wala control panel\n"
            "/purgeterabox — saare 1024terabox.com links delete\n\n"
            "💡 Photo/video + URLs bhejein, mai shorten karke autopost queue me daal dunga.\n"
            f"⏱ Autopost har {AUTOPOST_INTERVAL//60} min me, {POSTS_PER_CYCLE} post/channel/cycle.")
    await update.message.reply_text(msg, disable_web_page_preview=True, reply_markup=_panel_markup())


async def setapi_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: /setapi eu_xxxxxxxxxx\nGet key: https://earnurl.online/api-keys")
        return
    key = ctx.args[0].strip()
    if not API_KEY_RE.match(key):
        await update.message.reply_text("❌ Invalid format. Key: eu_xxxxxxxxxxxxxxxx")
        return
    async with aiohttp.ClientSession() as s:
        test = await shorten_url(s, "https://example.com/earnurl-bot-test", key)
        if "earnurl.online" not in test:
            await update.message.reply_text("❌ Key validate nahi ho saka. Check karein key active hai.")
            return
    users_col.update_one({"user_id": uid},
                         {"$set": {"user_id": uid, "api_key": key,
                                   "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    await update.message.reply_text(f"✅ API key saved: {_mask_key(key)}\nAb /addchannel se channel jodein.")


async def myapi_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    key = get_user_api_key(update.effective_user.id)
    await update.message.reply_text(f"🔑 Your key: {_mask_key(key)}" if key else "❌ No key. /setapi <your_key>")


async def removeapi_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    users_col.update_one({"user_id": update.effective_user.id}, {"$unset": {"api_key": ""}})
    await update.message.reply_text("🗑 API key removed.")


async def add_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text(
            "Usage: /addchannel <@channel or -100id>\n"
            "Ya us channel se koi bhi post yahan FORWARD kar dein — mai auto add kar dunga.\n"
            "⚠️ Bot ko channel me admin (post permission) banana zaroori hai.")
        return
    ch = _normalize_channel(ctx.args[0])
    try:
        chat = await ctx.bot.get_chat(ch)
    except Exception as e:
        await update.message.reply_text(
            f"❌ {ch} access nahi ho paya.\nBot ko channel me ADMIN banayein, phir dobara try karein.\n\nError: {e}")
        return
    ok = save_channel(uid, chat.id, getattr(chat, "title", None))
    if ok:
        await update.message.reply_text(
            f"✅ Added: {chat.title or chat.id} ({chat.id})\n"
            f"📡 Total channels: {channels_col.count_documents(owner_filter(uid))}")
    else:
        await update.message.reply_text("❌ Database me save nahi ho paya, dobara try karein.")


async def on_forwarded(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Channel se forward kiya gaya message -> channel auto add. True agar handle kiya."""
    msg = update.message
    fc = getattr(msg, "forward_from_chat", None)
    if fc is None:
        origin = getattr(msg, "forward_origin", None)
        fc = getattr(origin, "chat", None) if origin else None
    if fc is None or fc.type != ChatType.CHANNEL:
        return False
    uid = update.effective_user.id
    try:
        await ctx.bot.get_chat(fc.id)
    except Exception as e:
        await msg.reply_text(f"❌ Bot ko {fc.title} me admin banayein.\n{e}")
        return True
    save_channel(uid, fc.id, fc.title)
    await msg.reply_text(f"✅ Channel added: {fc.title} ({fc.id})",
                         reply_markup=_panel_markup())
    return True


async def remove_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Usage: /removechannel <@channel or -100id>")
        return
    ch = _normalize_channel(ctx.args[0])
    try:
        ch_id = (await ctx.bot.get_chat(ch)).id
    except Exception:
        ch_id = ch
    q = dict(owner_filter(uid))
    q["chat_id"] = ch_id
    res = channels_col.delete_many(q)
    await update.message.reply_text(f"🗑 Removed (deleted={res.deleted_count})")


async def list_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chs = list(channels_col.find(owner_filter(uid)))
    if not chs:
        await update.message.reply_text("Channels: (none)\nUse /addchannel @yourchannel")
        return
    lines = [f"• {c.get('title') or ''} ({c.get('chat_id')})" for c in chs]
    await update.message.reply_text(f"Your channels ({len(chs)}):\n" + "\n".join(lines))


async def queue_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    total = posts_col.count_documents(owner_filter(uid))
    media = posts_col.count_documents({**owner_filter(uid), "kind": "media"})
    chs   = channels_col.count_documents(owner_filter(uid))
    await update.message.reply_text(
        f"📦 posts: {total} (media: {media})\n📡 your channels: {chs}\n"
        f"⏱ autopost every {AUTOPOST_INTERVAL//60} min · {POSTS_PER_CYCLE}/channel/cycle")


async def postnow_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    n = await autopost_for_user(ctx.application, uid)
    await update.message.reply_text(f"📤 Sent {n} post(s).")


async def cleanposts_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("🧹 Checking queue for deleted links…")
    n = await cleanup_dead_posts(owner_filter(uid))
    await update.message.reply_text(f"🗑 Removed {n} post(s) with deleted/expired links.")


async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_admin(uid):
        total_users    = users_col.count_documents({})
        total_channels = channels_col.count_documents({})
        total_posts    = posts_col.count_documents({})
        rows = list(channels_col.aggregate([
            {"$group": {"_id": "$owner_id", "channels": {"$sum": 1}}},
            {"$sort": {"channels": -1}}, {"$limit": 30},
        ]))
        lines = [f"• {r['_id']}: {r['channels']} ch · "
                 f"{posts_col.count_documents({'owner_id': r['_id']})} posts" for r in rows]
        await update.message.reply_text(
            f"👑 ADMIN STATS\n👥 users: {total_users}\n📡 channels: {total_channels}\n"
            f"📦 posts: {total_posts}\n\nTop users:\n" + ("\n".join(lines) or "(no channels yet)"))
    else:
        await update.message.reply_text(
            f"📊 Your Stats\n"
            f"🔑 API key: {'✅' if get_user_api_key(uid) else '❌'}\n"
            f"📡 channels: {channels_col.count_documents(owner_filter(uid))}\n"
            f"📦 posts: {posts_col.count_documents(owner_filter(uid))}\n"
            f"⏱ autopost every {AUTOPOST_INTERVAL//60} min")


def classify_post(has_media: bool, text: str) -> str:
    return "media" if has_media else "text"


async def handle_private_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if await on_forwarded(update, ctx):
        return

    uid = update.effective_user.id
    api_key = get_user_api_key(uid)
    if not api_key:
        await update.message.reply_text(
            "⚠️ Pehle /setapi <your_key> se apna EarnURL API key set karein.\n"
            "Get key: https://earnurl.online/api-keys")
        return

    msg = update.message
    text = msg.text or msg.caption or ""
    converted = await shorten_all_in_text(text, api_key) if URL_RE.search(text) else text

    has_photo = bool(msg.photo); has_video = bool(msg.video); has_doc = bool(msg.document)
    kind = classify_post(has_photo or has_video or has_doc, converted)

    posts_col.insert_one({
        "owner_id": uid,
        "text": converted,
        "photo": msg.photo[-1].file_id if has_photo else None,
        "video": msg.video.file_id if has_video else None,
        "document": msg.document.file_id if has_doc else None,
        "kind": kind,
        "sent_to": [],
        "created_at": datetime.now(timezone.utc),
    })

    try:
        if has_photo:
            await msg.reply_photo(msg.photo[-1].file_id, caption=converted or None)
        elif has_video:
            await msg.reply_video(msg.video.file_id, caption=converted or None)
        elif has_doc:
            await msg.reply_document(msg.document.file_id, caption=converted or None)
        else:
            await msg.reply_text(converted or "(no text)", disable_web_page_preview=False)
    except Exception as e:
        log.warning("reply failed: %s", e)


# ---------------- AUTOPOST ----------------
async def _send_post(app: Application, ch_id, post) -> bool:
    try:
        if post.get("photo"):
            await app.bot.send_photo(ch_id, post["photo"], caption=post.get("text") or "")
        elif post.get("video"):
            await app.bot.send_video(ch_id, post["video"], caption=post.get("text") or "")
        elif post.get("document"):
            await app.bot.send_document(ch_id, post["document"], caption=post.get("text") or "")
        else:
            await app.bot.send_message(ch_id, post.get("text") or "", disable_web_page_preview=False)
        return True
    except Exception as e:
        log.error("send to %s failed: %s", ch_id, e)
        return False


def _pick_post(base_q: dict, ch_id, used_this_round: set):
    for extra in ({"kind": "media", "sent_to": {"$ne": ch_id}},
                  {"sent_to": {"$ne": ch_id}},
                  {"kind": "media"},
                  {}):
        q = {**base_q, **extra}
        if used_this_round:
            q["_id"] = {"$nin": list(used_this_round)}
        cands = list(posts_col.find(q).limit(200))
        if cands:
            return random.choice(cands)
    return None


async def _mark_sent(post, ch_id):
    posts_col.update_one({"_id": post["_id"]},
                         {"$addToSet": {"sent_to": ch_id},
                          "$set": {"last_sent_at": datetime.now(timezone.utc)}})


async def autopost_for_user(app: Application, uid: int) -> int:
    base_q = owner_filter(uid)
    channels = list(channels_col.find(base_q))
    if not channels:
        log.info("autopost: no channels for %s", uid)
        return 0
    if posts_col.count_documents(base_q) == 0:
        log.info("autopost: empty queue for %s", uid)
        return 0

    used = set()
    sent = 0
    random.shuffle(channels)
    for ch in channels:
        ch_id = ch.get("chat_id")
        if ch_id is None:
            continue
        for _ in range(POSTS_PER_CYCLE):
            post = _pick_post(base_q, ch_id, used)
            if not post:
                break
            if not await post_links_alive(post):
                posts_col.delete_one({"_id": post["_id"]})
                used.add(post["_id"])
                continue
            if await _send_post(app, ch_id, post):
                used.add(post["_id"])
                await _mark_sent(post, ch_id)
                sent += 1
            await asyncio.sleep(0.5)
    return sent


async def autopost_once(app: Application) -> int:
    owners = [o for o in channels_col.distinct("owner_id") if o]
    total = 0
    for uid in owners:
        try:
            total += await autopost_for_user(app, uid)
        except Exception as e:
            log.error("autopost user %s failed: %s", uid, e)
    return total


async def autopost_loop(app: Application):
    await asyncio.sleep(10)
    while True:
        try:
            n = await autopost_once(app)
            log.info("autopost cycle done, sent %d post(s)", n)
        except Exception as e:
            log.error("autopost error: %s", e)
        await asyncio.sleep(AUTOPOST_INTERVAL)


# ---------------- HEALTH ----------------
async def health(_req):
    try:
        return web.json_response({
            "ok": True,
            "users": users_col.count_documents({}),
            "channels": channels_col.count_documents({}),
            "posts": posts_col.count_documents({}),
        })
    except Exception:
        return web.json_response({"ok": False})


async def start_health_server(app: Application):
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    app.bot_data["_health_runner"] = runner
    log.info("Health on :%d", PORT)
    app.bot_data["_autopost_task"] = asyncio.create_task(autopost_loop(app))


async def stop_health_server(app: Application):
    runner = app.bot_data.get("_health_runner")
    if runner:
        await runner.cleanup()
    task = app.bot_data.get("_autopost_task")
    if task:
        task.cancel()


async def error_handler(update, ctx):
    err = ctx.error
    if isinstance(err, Conflict):
        log.error("409 Conflict: another instance is polling with the same BOT_TOKEN.")
        return
    log.exception("Unhandled: %s", err)


# ---------------- MAIN ----------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing")

    app = (Application.builder().token(BOT_TOKEN)
           .post_init(start_health_server)
           .post_shutdown(stop_health_server)
           .build())

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("setapi", setapi_cmd))
    app.add_handler(CommandHandler("myapi", myapi_cmd))
    app.add_handler(CommandHandler("removeapi", removeapi_cmd))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("removechannel", remove_channel))
    app.add_handler(CommandHandler("listchannels", list_channels))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("postnow", postnow_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("cleanposts", cleanposts_cmd))
    app.add_handler(CommandHandler("panel", panel_cmd))
    app.add_handler(CommandHandler("purgeterabox", purgeterabox_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
