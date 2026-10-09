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
from telegram.error import BadRequest, Forbidden, Conflict, NetworkError, RetryAfter, TimedOut
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
mongo = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    socketTimeoutMS=20000,
    retryWrites=True,
)
db = mongo[os.getenv("MONGO_DB", "earnurl_bot")]
posts_col    = db["posts"]
channels_col = db["channels"]
meta_col     = db["meta"]
users_col    = db["users"]
dead_posts_col = db["dead_posts"]

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
    posts_col.create_index([("owner_id", 1), ("_id", 1)])
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
    """Return False ONLY when the website clearly says the link does not exist.
    Any error / rate limit / timeout counts as alive so old posts are never lost by mistake."""
    for attempt in range(2):
        try:
            async with session.post(
                f"{SUPABASE_URL}/rest/v1/rpc/link_exists",
                json={"_slug": slug.strip().lower()},
                headers={"apikey": SUPABASE_ANON_KEY,
                         "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                         "Content-Type": "application/json"},
                timeout=15,
            ) as r:
                if r.status != 200:
                    log.warning("link_exists HTTP %s for %s — keeping post", r.status, slug)
                    if r.status == 429 and attempt == 0:
                        await asyncio.sleep(3)
                        continue
                    return True
                data = await r.json(content_type=None)
                if data is False:
                    return False
                return True
        except Exception as e:
            log.warning("link_exists check failed for %s: %s", slug, e)
            return True  # network issue -> don't delete
    return True


async def post_links_alive(post) -> bool:
    slugs = _extract_slugs(post.get("text") or "")
    if not slugs:
        return True
    async with aiohttp.ClientSession() as s:
        for slug in slugs:
            if not await _slug_alive(s, slug):
                return False
    return True


def _archive_post(post, reason: str):
    """Move a post to dead_posts instead of deleting forever (can be restored with /restoreposts)."""
    try:
        doc = dict(post)
        doc["archived_reason"] = reason
        doc["archived_at"] = datetime.now(timezone.utc)
        dead_posts_col.replace_one({"_id": post["_id"]}, doc, upsert=True)
    except Exception as e:
        log.warning("archive failed for %s: %s", post.get("_id"), e)
        return
    posts_col.delete_one({"_id": post["_id"]})


async def cleanup_dead_posts(owner_query=None) -> int:
    q = owner_query or {}
    removed = 0
    for post in list(posts_col.find(q)):
        if not await post_links_alive(post):
            _archive_post(post, "deleted_link")
            removed += 1
        await asyncio.sleep(0.2)  # gentle on the website, avoids rate limits
    return removed


def restore_archived_posts(owner_query=None) -> int:
    q = owner_query or {}
    restored = 0
    for doc in list(dead_posts_col.find(q)):
        doc.pop("archived_reason", None)
        doc.pop("archived_at", None)
        try:
            posts_col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
            dead_posts_col.delete_one({"_id": doc["_id"]})
            restored += 1
        except Exception as e:
            log.warning("restore failed for %s: %s", doc.get("_id"), e)
    return restored


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
        await q.message.reply_text("📤 Posting check kar raha hoon…")
        await q.message.reply_text(await manual_post_result(ctx.application, uid),
                                   reply_markup=_panel_markup())

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
            "/restoreposts — purane hataye gaye posts wapas rotation me\n"
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
        f"📦 saved forever: {total} posts ({media} media)\n📡 your channels: {chs}\n"
        f"🔁 Continuous rotation ON\n"
        f"⏱ autopost every {AUTOPOST_INTERVAL//60} min · {POSTS_PER_CYCLE}/channel/cycle")


async def postnow_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    await update.message.reply_text("📤 Posting check kar raha hoon…")
    await update.message.reply_text(await manual_post_result(ctx.application, uid))


async def cleanposts_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("🧹 Checking queue for deleted links…")
    n = await cleanup_dead_posts(owner_filter(uid))
    await update.message.reply_text(f"🗑 Removed {n} post(s) with deleted/expired links.")


async def restoreposts_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    n = restore_archived_posts(owner_filter(uid))
    total = posts_col.count_documents(owner_filter(uid))
    await update.message.reply_text(
        f"♻️ Restored {n} old post(s) back into rotation.\n📦 Queue now: {total} posts")


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


def message_text_with_links(msg) -> str:
    """Expose Telegram's hidden hyperlinks before shortening; offsets are UTF-16."""
    text = msg.text or msg.caption or ""
    entities = (getattr(msg, "entities", None) if msg.text else
                getattr(msg, "caption_entities", None)) or []
    encoded = text.encode("utf-16-le")
    for entity in sorted(entities, key=lambda item: item.offset, reverse=True):
        if entity.type != "text_link" or not entity.url:
            continue
        start, end = entity.offset * 2, (entity.offset + entity.length) * 2
        label = encoded[start:end].decode("utf-16-le")
        replacement = entity.url if label == entity.url else f"{label} ({entity.url})"
        encoded = encoded[:start] + replacement.encode("utf-16-le") + encoded[end:]
    return encoded.decode("utf-16-le")


def telegram_text_parts(text: str, limit: int = 4096):
    """Split by Telegram's UTF-16 limits without breaking a Unicode character."""
    parts, current, size = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        parts.append("".join(current))
    return parts


async def handle_private_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    # A forwarded channel post is also queue content, not just channel setup.
    await on_forwarded(update, ctx)

    uid = update.effective_user.id
    api_key = get_user_api_key(uid)
    if not api_key:
        await update.message.reply_text(
            "⚠️ Pehle /setapi <your_key> se apna EarnURL API key set karein.\n"
            "Get key: https://earnurl.online/api-keys")
        return

    msg = update.message
    text = message_text_with_links(msg)
    converted = await shorten_all_in_text(text, api_key) if URL_RE.search(text) else text

    has_photo = bool(msg.photo); has_video = bool(msg.video); has_doc = bool(msg.document)
    if not text.strip() and not (has_photo or has_video or has_doc):
        await msg.reply_text("Photo, video, document ya link wala text bhejein.")
        return
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
    """Send with bounded retries so a temporary Telegram failure cannot stop rotation."""
    errors = app.bot_data.setdefault("_send_errors", {})
    text = post.get("text") or ""
    has_media = bool(post.get("photo") or post.get("video") or post.get("document"))
    # A converted caption can exceed 1024 units; keep its links in a follow-up.
    caption = text if len(text.encode("utf-16-le")) // 2 <= 1024 else ""
    followups = telegram_text_parts(text) if not has_media or not caption else []
    media_sent, next_part = False, 0
    for attempt in range(3):
        try:
            if has_media and not media_sent:
                if post.get("photo"):
                    await app.bot.send_photo(ch_id, post["photo"], caption=caption)
                elif post.get("video"):
                    await app.bot.send_video(ch_id, post["video"], caption=caption)
                else:
                    await app.bot.send_document(ch_id, post["document"], caption=caption)
                media_sent = True
            while next_part < len(followups):
                await app.bot.send_message(ch_id, followups[next_part], disable_web_page_preview=True)
                next_part += 1
            if not has_media and not followups:
                errors[ch_id] = "Empty saved post; send text or supported media."
                return False
            errors.pop(ch_id, None)
            return True
        except RetryAfter as e:
            errors[ch_id] = "Telegram rate limit; the post will be retried in rotation."
            delay = e.retry_after.total_seconds() if hasattr(e.retry_after, "total_seconds") else float(e.retry_after)
            delay = min(max(delay, 1), 120)
            log.warning("Telegram rate limit for %s; retrying in %.1fs", ch_id, delay)
            await asyncio.sleep(delay)
        except (BadRequest, Forbidden) as e:
            # BadRequest inherits NetworkError: do not hide permanent errors as outages.
            errors[ch_id] = str(e)[:300]
            log.error("Telegram rejected send to %s: %s", ch_id, e)
            return False
        except (TimedOut, NetworkError) as e:
            errors[ch_id] = f"{type(e).__name__}: {str(e)[:250]}"
            if attempt == 2:
                log.error("send to %s failed after retries: %s", ch_id, e)
                return False
            delay = 2 ** attempt
            log.warning("temporary send error for %s; retrying in %ss: %s", ch_id, delay, e)
            await asyncio.sleep(delay)
        except Exception as e:
            errors[ch_id] = str(e)[:300]
            log.error("send to %s failed: %s", ch_id, e)
            return False
    return False


def _pick_next_post(base_q: dict, cursor, used_this_channel: set):
    """Pick posts in a fair, persistent order and wrap after the newest post."""
    def query(after_cursor):
        q = dict(base_q)
        id_filter = {}
        if after_cursor is not None:
            id_filter["$gt"] = after_cursor
        if used_this_channel:
            id_filter["$nin"] = list(used_this_channel)
        if id_filter:
            q["_id"] = id_filter
        return posts_col.find_one(q, sort=[("_id", 1)])

    post = query(cursor)
    return post if post is not None else query(None)


async def _mark_sent(post, ch_id):
    await asyncio.to_thread(posts_col.update_one, {"_id": post["_id"]},
                         {"$addToSet": {"sent_to": ch_id},
                          "$set": {"last_sent_at": datetime.now(timezone.utc)}})


async def autopost_for_user(app: Application, uid: int) -> int:
    locks = app.bot_data.setdefault("_autopost_locks", {})
    lock = locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        log.info("autopost: cycle already running for %s", uid)
        return 0

    async with lock:
        result = {"channels": 0, "posts": 0, "sent": 0, "deleted": 0, "errors": []}
        app.bot_data.setdefault("_autopost_results", {})[uid] = result
        base_q = owner_filter(uid)
        result["_query"] = base_q
        channels = await asyncio.to_thread(lambda: list(channels_col.find(base_q)))
        result["channels"] = len(channels)
        if not channels:
            log.info("autopost: no channels for %s", uid)
            return 0
        result["posts"] = await asyncio.to_thread(posts_col.count_documents, base_q)
        if result["posts"] == 0:
            log.info("autopost: empty queue for %s", uid)
            return 0

        sent = 0
        random.shuffle(channels)
        for ch in channels:
            try:
                delivered = await autopost_channel(app, base_q, ch)
                sent += delivered
                result["sent"] = sent
            except Exception as e:
                log.exception("autopost channel %s failed; continuing: %s", ch.get("chat_id"), e)
                result["errors"].append(f"{ch.get('title') or ch.get('chat_id')}: {str(e)[:300]}")
            error = app.bot_data.get("_send_errors", {}).get(ch.get("chat_id"))
            if error:
                result["errors"].append(f"{ch.get('title') or ch.get('chat_id')}: {error}")
        return sent


async def manual_post_result(app: Application, uid: int) -> str:
    """Return the actual reason instead of reporting every early exit as Sent 0."""
    lock = app.bot_data.get("_autopost_locks", {}).get(uid)
    if lock and lock.locked():
        return "⏳ Posting pehle se chal rahi hai. Thoda wait karein, phir /health bhejein."
    try:
        sent = await autopost_for_user(app, uid)
    except Exception as e:
        log.exception("manual posting failed for %s", uid)
        return f"❌ Posting check fail hua: {str(e)[:300]}"
    result = app.bot_data.get("_autopost_results", {}).get(uid, {})
    lines = [f"📤 Sent {sent} post(s).",
             f"📡 Channels: {result.get('channels', 0)} · Saved posts: {result.get('posts', 0)}"]
    if not result.get("channels"):
        lines.append("❌ Channel nahi mila. /addchannel @yourchannel bhejein; bot ko post permission dein.")
    elif not result.get("posts"):
        lines.append("❌ Queue khali hai. Bot ko post bhejein. Purane archived posts ke liye /restoreposts.")
    elif POSTS_PER_CYCLE <= 0:
        lines.append("❌ POSTS_PER_CYCLE setting 1 ya usse zyada honi chahiye.")
    if result.get("deleted"):
        lines.append(f"🗑 Deleted website links wale {result['deleted']} posts skip hue.")
    if result.get("errors"):
        lines.append("❌ Posting errors:\n" + "\n".join(result["errors"][:5]))
    elif sent == 0 and result.get("posts") and result.get("channels") and not result.get("deleted"):
        lines.append("❌ Koi deliverable post nahi mila. /health ka result bhejein.")
    return "\n".join(lines)


async def autopost_channel(app: Application, base_q: dict, ch: dict) -> int:
    ch_id = ch.get("chat_id")
    if ch_id is None:
        app.bot_data.setdefault("_send_errors", {})[None] = "Saved channel ID missing; /addchannel se channel dobara jodein."
        return 0
    app.bot_data.setdefault("_send_errors", {}).pop(ch_id, None)
    cursor = ch.get("rotation_cursor")
    used = set()
    channel_sent = 0
    # Deleted or invalid posts must not consume all delivery slots.
    scan_limit = await asyncio.to_thread(posts_col.count_documents, base_q)
    while channel_sent < POSTS_PER_CYCLE and len(used) < scan_limit:
        post = await asyncio.to_thread(_pick_next_post, base_q, cursor, used)
        if not post:
            break
        used.add(post["_id"])
        if not await post_links_alive(post):
            dead_id = post["_id"]
            await asyncio.to_thread(_archive_post, post, "deleted_link")
            for result in app.bot_data.get("_autopost_results", {}).values():
                # Only the active owner's report belongs to this channel cycle.
                if result.get("_query") == base_q:
                    result["deleted"] += 1
            used.add(dead_id)
            cursor = dead_id
            log.info("autopost: removed post with deleted link: %s", dead_id)
            continue
        if await _send_post(app, ch_id, post):
            used.add(post["_id"])
            cursor = post["_id"]
            # Telegram delivery already succeeded; a bookkeeping failure is not Sent 0.
            channel_sent += 1
            try:
                await _mark_sent(post, ch_id)
                await asyncio.to_thread(channels_col.update_one,
                    {"_id": ch["_id"]},
                    {"$set": {
                        "rotation_cursor": cursor,
                        "last_sent_at": datetime.now(timezone.utc),
                    }, "$unset": {"last_error": ""}},
                )
            except Exception as e:
                app.bot_data.setdefault("_send_errors", {})[ch_id] = f"Post sent, saved status update failed: {str(e)[:200]}"
                log.exception("sent post bookkeeping failed for %s", ch_id)
        else:
            # Advance past this failure, but retain the post for the next
            # wraparound. One invalid media file must not block all posts.
            cursor = post["_id"]
            try:
                await asyncio.to_thread(channels_col.update_one,
                    {"_id": ch["_id"]},
                    {"$set": {
                        "rotation_cursor": cursor,
                        "last_error": app.bot_data.get("_send_errors", {}).get(ch_id, "Telegram send failed"),
                        "last_error_at": datetime.now(timezone.utc),
                    }},
                )
            except Exception:
                log.exception("failed post cursor update failed for %s; continuing", ch_id)
        await asyncio.sleep(0.5)
    return channel_sent

async def autopost_once(app: Application) -> int:
    owners = [o for o in await asyncio.to_thread(channels_col.distinct, "owner_id") if o]
    limit = asyncio.Semaphore(5)
    async def run_owner(uid):
        async with limit:
            try:
                return await autopost_for_user(app, uid)
            except Exception as e:
                log.exception("autopost user %s failed: %s", uid, e)
                return 0
    return sum(await asyncio.gather(*(run_owner(uid) for uid in owners)))


async def autopost_loop(app: Application):
    await asyncio.sleep(10)
    while True:
        try:
            n = await autopost_once(app)
            app.bot_data["_autopost_last_cycle"] = datetime.now(timezone.utc)
            log.info("autopost cycle done, sent %d post(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("autopost cycle recovered after error: %s", e)
        try:
            await asyncio.sleep(max(AUTOPOST_INTERVAL, 30))
        except asyncio.CancelledError:
            raise


async def autopost_supervisor(app: Application):
    """Keep the permanent rotation alive even if its worker exits unexpectedly."""
    while True:
        worker = asyncio.create_task(autopost_loop(app))
        app.bot_data["_autopost_worker"] = worker
        try:
            await worker
            log.error("autopost worker stopped unexpectedly; restarting in 5 seconds")
        except asyncio.CancelledError:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            raise
        except Exception as e:
            log.exception("autopost worker crashed; restarting in 5 seconds: %s", e)
        await asyncio.sleep(5)


# ---------------- HEALTH ----------------
async def health_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    task = ctx.application.bot_data.get("_autopost_task")
    last_cycle = ctx.application.bot_data.get("_autopost_last_cycle")
    channels = await asyncio.to_thread(lambda: list(channels_col.find(owner_filter(uid))))
    total_posts = await asyncio.to_thread(posts_col.count_documents, owner_filter(uid))
    errors = []
    for ch in channels:
        error = ctx.application.bot_data.get("_send_errors", {}).get(ch.get("chat_id")) or ch.get("last_error")
        if error:
            errors.append(f"• {ch.get('title') or ch.get('chat_id')}: {error}")
    await update.message.reply_text(
        f"Autopost: {'running' if task and not task.done() else 'stopped'}\n"
        f"Last cycle (UTC): {last_cycle or 'waiting for first cycle'}\n"
        f"Channels: {len(channels)}\n"
        f"Saved posts: {total_posts}\n"
        f"Interval: {AUTOPOST_INTERVAL // 60} min\n"
        + ("Last send errors:\n" + "\n".join(errors[:5]) if errors else "No recorded send errors."))


async def health(_req):
    # Never wait for MongoDB here. Hosting providers use this endpoint to decide
    # whether the process should stay alive, so it must answer immediately even
    # during a temporary database slowdown.
    telegram_app = _req.app.get("telegram_app")
    task = telegram_app.bot_data.get("_autopost_task") if telegram_app else None
    return web.json_response({
        "ok": True,
        "autopost_running": bool(task and not task.done()),
    })


async def start_health_server(app: Application):
    web_app = web.Application()
    web_app["telegram_app"] = app
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    app.bot_data["_health_runner"] = runner
    log.info("Health on :%d", PORT)
    app.bot_data["_autopost_task"] = asyncio.create_task(autopost_supervisor(app))


async def stop_health_server(app: Application):
    runner = app.bot_data.get("_health_runner")
    if runner:
        await runner.cleanup()
    task = app.bot_data.get("_autopost_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


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
           .post_stop(stop_health_server)
           .build())

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("setapi", setapi_cmd))
    app.add_handler(CommandHandler("myapi", myapi_cmd))
    app.add_handler(CommandHandler("removeapi", removeapi_cmd))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("removechannel", remove_channel))
    app.add_handler(CommandHandler("listchannels", list_channels))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("postnow", postnow_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("cleanposts", cleanposts_cmd))
    app.add_handler(CommandHandler("restoreposts", restoreposts_cmd))
    app.add_handler(CommandHandler("panel", panel_cmd))
    app.add_handler(CommandHandler("purgeterabox", purgeterabox_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))
    app.add_error_handler(error_handler)
    # Preserve messages received during a restart; they still belong in the permanent queue.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
