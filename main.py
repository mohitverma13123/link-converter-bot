import os
import re
import logging
import sqlite3
import requests
import asyncio
import random
import time
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---- LOGGING SETUP ----
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---- CONFIGURATION ----
BOT_TOKEN = os.getenv("BOT_TOKEN")
EARNURL_API_KEY = os.getenv("EARNURL_API_KEY")
OWNER_ID = 2091839003 # Aapki verified secure owner id numeric direct config fixed

COOLDOWN_DURATION = 259200 # 3 Days loop restriction window

# ---- DATABASE SYSTEM ----
def init_db():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY, title TEXT)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS permanent_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            photo_id TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS history (
            channel_id TEXT,
            post_id INTEGER,
            sent_time INTEGER,
            PRIMARY KEY (channel_id, post_id)
        )
    """)
    conn.commit()
    conn.close()

def get_channels():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id FROM channels")
    rows = cursor.fetchall()
    conn.close()
    return [row for row in rows]

# ---- VERIFIED LIVE API GATEWAY CONVERSION ----
def convert_to_earnurl(long_url):
    # Aapka exact custom full backend route function path mapping setup kiya gaya hai
    endpoint_url = "supabase.co"
    
    # Supabase standard payload and bearer tracking
    headers = {
        "Authorization": f"Bearer {EARNURL_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Custom parameter validation dictionary injection mapping
    json_data = {
        "url": long_url,
        "type": 1
    }
    
    try:
        # POST gateway calling payload transmission standard mapping
        response = requests.post(endpoint_url, json=json_data, headers=headers, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success" or "shortenedUrl" in data:
                return data.get("shortenedUrl") or data.get("short_url")
                
        # Alternative secondary query routing configuration model bypass
        fallback_params = {"api_key": EARNURL_API_KEY, "url": long_url, "type": "1"}
        fallback_res = requests.get(endpoint_url, params=fallback_params, timeout=15)
        if fallback_res.status_code == 200:
            d = fallback_res.json()
            return d.get("shortenedUrl") or d.get("short_url")
            
    except Exception as e:
        logger.error(f"Live Supabase Connection Exception: {e}")
        
    return long_url

# ---- STABLE BACKGROUND AUTO-POSTER LOOP ----
async def smart_auto_poster(app: Application):
    logger.info("Background Rotation Sequence loop is successfully listening.")
    while True:
        try:
            channels = get_channels()
            current_time = int(time.time())
            
            if channels:
                conn = sqlite3.connect("bot_data.db")
                cursor = conn.cursor()
                
                # Active cooldown window sweep tracking
                cursor.execute("DELETE FROM history WHERE ? - sent_time > ?", (current_time, COOLDOWN_DURATION))
                conn.commit()

                for channel_id in channels:
                    cursor.execute("SELECT post_id FROM history WHERE channel_id = ?", (channel_id,))
                    cooldown_ids = [row for row in cursor.fetchall()]
                    
                    if cooldown_ids:
                        placeholder = ','.join('?' for _ in cooldown_ids)
                        cursor.execute(f"SELECT id, text, photo_id FROM permanent_queue WHERE id NOT IN ({placeholder})", cooldown_ids)
                    else:
                        cursor.execute("SELECT id, text, photo_id FROM permanent_queue")
                        
                    available_posts = cursor.fetchall()
                    
                    if not available_posts:
                        cursor.execute("""
                            SELECT pq.id, pq.text, pq.photo_id 
                            FROM permanent_queue pq 
                            JOIN history h ON pq.id = h.post_id 
                            WHERE h.channel_id = ? 
                            ORDER BY h.sent_time ASC LIMIT 1
                        """, (channel_id,))
                        available_posts = cursor.fetchall()

                    if available_posts:
                        selected_post = random.choice(available_posts)
                        post_id, converted_text, photo_id = selected_post
                        
                        try:
                            if photo_id:
                                await app.bot.send_photo(chat_id=channel_id, photo=photo_id, caption=converted_text)
                            else:
                                await app.bot.send_message(chat_id=channel_id, text=converted_text)
                            
                            cursor.execute("INSERT OR REPLACE INTO history (channel_id, post_id, sent_time) VALUES (?, ?, ?)", 
                                           (channel_id, post_id, current_time))
                            conn.commit()
                            await asyncio.sleep(4)
                        except Exception as e:
                            logger.error(f"Post injection dropped for chat targets {channel_id}: {e}")
                conn.close()
            else:
                logger.info("Awaiting for dynamic channels via /add interface configuration.")
        except Exception as e:
            logger.error(f"Fatal error handling execution sequence: {e}")
            
        # ⏱️ Multi-channel dynamic safety timer gap loop execution context (5 to 10 min)
        random_delay = random.randint(300, 600)
        await asyncio.sleep(random_delay)

# ---- COMMAND HANDLERS ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(
        "🚀 **EarnURL Online Ultimate Dynamic Rotator is Active!**\n\n"
        "🛠️ **Owner Interface Controls:**\n"
        "👉 `/add -100xxxxxx` : Channel database tracking map configure karein\n"
        "👉 `/remove -100xxxxxx` : Post injection sequence stop karein\n"
        "👉 `/list` : Available active targets database display\n"
        "👉 `/status` : Permanent content dictionary size insights\n"
        "👉 `/clearall` : Memory database wipe context target execution"
    )

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Configuration execution sample query format: `/add -100123456789` ")
        return
    try:
        chat = await context.bot.get_chat(context.args)
        conn = sqlite3.connect("bot_data.db")
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO channels (channel_id, title) VALUES (?, ?)", (str(chat.id), chat.title))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ Target **{chat.title}** successfully synchronized into core database memory stream tracker.")
    except Exception as e:
        await update.message.reply_text(f"❌ Handshake deployment authorization missing: {e}")

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Structure syntax format constraint: `/remove -100123456789` ")
        return
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM channels WHERE channel_id = ?", (str(context.args),))
    cursor.execute("DELETE FROM history WHERE channel_id = ?", (str(context.args),))
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ Selected tracking target context purged from internal configuration list maps.")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, title FROM channels")
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📋 Configuration matrix map database registry index array empty.")
        return
    text = "📋 **Target Synchronization Mapping Indexes:**\n\n"
    for row in rows:
        text += f"🔹 {row} (`{row}`)\n"
    await update.message.reply_text(text)

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM permanent_queue")
    total_posts = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM history")
    active_cooldowns = cursor.fetchone()
    conn.close()
    await update.message.reply_text(
        f"📊 **Rotator Datastore Operational Analytics Metrics:**\n\n"
        f"🔹 Total Converted Repository Library Scope Size: `{total_posts}`\n"
        f"⏳ Isolated Cooldown Lock Window Registry Keys Active: `{active_cooldowns}`"
    )

async def clear_all_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM permanent_queue")
    cursor.execute("DELETE FROM history")
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ Content dataset structure libraries wiped out from storage array nodes.")

# ---- BULK DATA INGESTION ENGINE ----
async def handle_bulk_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    text = update.message.text or update.message.caption
    if not text:
        return

    urls = re.findall(r'(https?://[^\s]+)', text)
    if not urls:
        return

    new_text = text
    converted_any = False
    
    for url in urls:
        if any(domain in url.lower() for domain in ["terabox", "diskula", "nephobox", "sharelinks", "4shared", "box", "drive"]):
            short_url = convert_to_earnurl(url)
            new_text = new_text.replace(url, short_url)
            converted_any = True

    if not converted_any:
        return

    photo_id = update.message.photo[-1].file_id if update.message.photo else None

    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO permanent_queue (text, photo_id) VALUES (?, ?)", (new_text, photo_id))
    conn.commit()
    conn.close()
    
    logger.info("Incoming stream schema transformed and appended to non-volatile datastore library.")

async def post_init(application: Application):
    # Initializes background event tracking engine loops automatically
    asyncio.create_task(smart_auto_poster(application))

# ---- APPLICATION EXECUTION ENTRY POINT ----
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_channel))
    app.add_handler(CommandHandler("remove", remove_channel))
    app.add_handler(CommandHandler("list", list_channels))
    app.add_handler(CommandHandler("status", show_status))
    app.add_handler(CommandHandler("clearall", clear_all_posts))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_bulk_incoming))
    
    logger.info("Starting production polling runtime loop...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
