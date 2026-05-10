MONGO_URI    = os.environ["MONGO_URI"]
PORT         = int(os.environ.get("PORT", "10000"))

# EarnURL API endpoint
EARNURL_ENDPOINT = "https://mgtvdesmjqqrgczgvnbz.supabase.co/functions/v1/shorten-api"

# Auto post every 5 minutes
AUTO_POST_SECONDS = int(os.environ.get("AUTO_POST_SECONDS", "300"))

# 1 = admin + users ke converted posts save honge
# 0 = sirf admin ke converted posts save honge
AUTO_SAVE_ALL = os.environ.get("AUTO_SAVE_ALL", "1") == "1"

# ---------------- LOGGING ----------------
logging.basicConfig(
format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
@@ -46,102 +54,294 @@
HTTP: aiohttp.ClientSession | None = None
URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)

# ---------------- HELPERS ----------------
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def get_message_text_or_caption(msg) -> str:
    return msg.text or msg.caption or ""

def detect_media(msg):
    """
    Returns: (media_type, file_id)
    """
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    if msg.video:
        return "video", msg.video.file_id
    if msg.animation:
        return "animation", msg.animation.file_id
    if msg.document:
        return "document", msg.document.file_id
    return None, None

async def send_post(bot, chat_id, post):
    text = post.get("text") or ""
    media_type = post.get("media_type")
    file_id = post.get("file_id")

    if media_type == "photo" and file_id:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=text[:1024],
            disable_web_page_preview=True,
        )

    if media_type == "video" and file_id:
        return await bot.send_video(
            chat_id=chat_id,
            video=file_id,
            caption=text[:1024],
        )

    if media_type == "animation" and file_id:
        return await bot.send_animation(
            chat_id=chat_id,
            animation=file_id,
            caption=text[:1024],
        )

    if media_type == "document" and file_id:
        return await bot.send_document(
            chat_id=chat_id,
            document=file_id,
            caption=text[:1024],
        )

    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        disable_web_page_preview=True,
    )

async def reply_converted_message(msg, converted_text: str):
    media_type, file_id = detect_media(msg)

    if media_type == "photo" and file_id:
        return await msg.reply_photo(
            photo=file_id,
            caption=converted_text[:1024],
        )

    if media_type == "video" and file_id:
        return await msg.reply_video(
            video=file_id,
            caption=converted_text[:1024],
        )

    if media_type == "animation" and file_id:
        return await msg.reply_animation(
            animation=file_id,
            caption=converted_text[:1024],
        )

    if media_type == "document" and file_id:
        return await msg.reply_document(
            document=file_id,
            caption=converted_text[:1024],
        )

    return await msg.reply_text(
        converted_text,
        disable_web_page_preview=True,
    )

# ---------------- SHORTEN ----------------
async def shorten_one(url: str) -> str:
try:
        params = {"api": EARNURL_API, "url": url, "mode": "quick"}
        async with HTTP.get(EARNURL_ENDPOINT, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
        params = {
            "api": EARNURL_API,
            "url": url,
            "mode": "quick",
        }

        async with HTTP.get(
            EARNURL_ENDPOINT,
            params=params,
            timeout=aiohttp.ClientTimeout(total=25),
        ) as r:
data = await r.json(content_type=None)

if isinstance(data, dict) and data.get("short_url"):
                return data["short_url"]
                short_url = data["short_url"]

                # Safety fix: agar backend purana domain de bhi de, bot earnurl.online hi bheje
                short_url = short_url.replace("https://earnurl.lovable.app", "https://earnurl.online")
                short_url = short_url.replace("http://earnurl.lovable.app", "https://earnurl.online")

                return short_url

log.warning(f"shorten failed payload: {data}")

except Exception as e:
log.error(f"shorten error for {url}: {e}")

return url

async def shorten_text(text: str) -> str:
urls = URL_RE.findall(text)

if not urls:
return text
    unique = list(dict.fromkeys(urls))

    unique_urls = list(dict.fromkeys(urls))
mapping = {}
    for u in unique:

    for u in unique_urls:
mapping[u] = await shorten_one(u)

out = text
    for orig, short in mapping.items():
        out = out.replace(orig, short)
    for original, short in mapping.items():
        out = out.replace(original, short)

return out

# ---------------- COMMANDS ----------------
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

await update.message.reply_text(
"👋 EarnURL Bot is live!\n\n"
        "Send any message with links — I'll shorten them.\n\n"
        "Send any message/photo/video with links — I'll shorten them.\n\n"
"Admin commands:\n"
"/addchannel @username or -100xxxx\n"
"/listchannels\n"
"/stats"
)

async def cmd_addchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

if not is_admin(update.effective_user.id):
return await update.message.reply_text("❌ Admin only.")

if not ctx.args:
        return await update.message.reply_text("Usage: /addchannel @channel  OR  /addchannel -100123456789")
        return await update.message.reply_text(
            "Usage:\n"
            "/addchannel @channel\n"
            "OR\n"
            "/addchannel -100123456789"
        )

ident = ctx.args[0]

try:
chat = await ctx.bot.get_chat(ident)

await channels_col.update_one(
{"channel_id": chat.id},
            {"$set": {"channel_id": chat.id, "title": chat.title or chat.username, "added_at": datetime.utcnow()}},
            {
                "$set": {
                    "channel_id": chat.id,
                    "title": chat.title or chat.username or str(chat.id),
                    "username": chat.username,
                    "added_at": datetime.utcnow(),
                }
            },
upsert=True,
)
        await update.message.reply_text(f"✅ Added: {chat.title or chat.username} ({chat.id})")

        await update.message.reply_text(
            f"✅ Added: {chat.title or chat.username or chat.id}\nID: {chat.id}"
        )

except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}\nMake sure bot is admin in that channel.")
        await update.message.reply_text(
            f"❌ Failed: {e}\n\n"
            "Make sure:\n"
            "1. Bot channel me added hai\n"
            "2. Bot channel ka admin hai\n"
            "3. Channel username/ID correct hai"
        )

async def cmd_listchannels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
    if not update.message:
return

    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Admin only.")

items = await channels_col.find({}).to_list(length=500)

if not items:
return await update.message.reply_text("No channels added.")
    txt = "📺 Channels:\n" + "\n".join(f"• {c.get('title')} ({c['channel_id']})" for c in items)

    txt = "📺 Channels:\n\n" + "\n".join(
        f"• {c.get('title', 'Unknown')} ({c['channel_id']})"
        for c in items
    )

await update.message.reply_text(txt)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
    if not update.message:
return

    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Admin only.")

nch = await channels_col.count_documents({})
npo = await posts_col.count_documents({})
nhi = await history_col.count_documents({})
    await update.message.reply_text(f"📊 Stats\nChannels: {nch}\nSaved posts: {npo}\nAuto-posts done: {nhi}")

    await update.message.reply_text(
        f"📊 Stats\n\n"
        f"Channels: {nch}\n"
        f"Saved posts: {npo}\n"
        f"Auto-posts done: {nhi}\n"
        f"Auto-post timer: every {AUTO_POST_SECONDS // 60} min"
    )

# ---------------- MESSAGE HANDLER ----------------
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
msg = update.effective_message

if not msg:
return
    text = msg.text or msg.caption

    text = get_message_text_or_caption(msg)

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
        media_type, file_id = detect_media(msg)

        should_save = AUTO_SAVE_ALL or (
            update.effective_user and update.effective_user.id == ADMIN_ID
        )

        if should_save:
            await posts_col.insert_one({
                "text": converted,
                "media_type": media_type,
                "file_id": file_id,
                "source_user_id": update.effective_user.id if update.effective_user else None,
                "created_at": datetime.utcnow(),
            })

        try:
            await processing.delete()
        except Exception:
            pass

        await reply_converted_message(msg, converted)

        await processing.edit_text(converted, disable_web_page_preview=True)
except Exception as e:
log.exception("handle_message failed")

try:
if processing:
await processing.edit_text(f"❌ Error: {e}")
@@ -154,43 +354,60 @@ async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
async def auto_post_job(ctx: ContextTypes.DEFAULT_TYPE):
try:
channels = await channels_col.find({}).to_list(length=500)

if not channels:
log.info("auto-post: no channels")
            return

        posts = await posts_col.find({}).to_list(length=2000)

        if not posts:
            log.info("auto-post: no saved posts")
            return

        # Har 5 minute me 1 random channel
        channel = random.choice(channels)
        cid = channel["channel_id"]

        cutoff = datetime.utcnow() - timedelta(days=7)

        recent = await history_col.find(
            {
                "channel_id": cid,
                "posted_at": {"$gte": cutoff},
            }
        ).to_list(length=3000)

        used_ids = {h["post_id"] for h in recent if h.get("post_id")}

        fresh_posts = [p for p in posts if p["_id"] not in used_ids]

        # Agar sab posts use ho chuki hain to dobara all posts me se random bhej do
        if fresh_posts:
            post = random.choice(fresh_posts)
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
            post = random.choice(posts)

        try:
            await send_post(ctx.bot, cid, post)

            await history_col.insert_one({
                "channel_id": cid,
                "post_id": post["_id"],
                "posted_at": datetime.utcnow(),
            })

            log.info(f"auto-post sent -> {cid}")

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
@@ -199,22 +416,36 @@ async def health(_req):
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

    # Pehla auto-post 30 sec baad, phir har 5 min
    app.job_queue.run_repeating(
        auto_post_job,
        interval=AUTO_POST_SECONDS,
        first=30,
        name="auto_post_every_5_min",
    )

log.info("startup complete")

async def on_shutdown(app):
global HTTP

if HTTP:
await HTTP.close()

@@ -232,10 +463,16 @@ def main():
app.add_handler(CommandHandler("addchannel", cmd_addchannel))
app.add_handler(CommandHandler("listchannels", cmd_listchannels))
app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))

    # Text + photo/video/document caption links handle karega
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

log.info("Bot starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
main()
