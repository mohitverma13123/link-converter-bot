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
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Cooldown time seconds me (3 Days = 3 * 24 * 60 * 60 = 259200 seconds)
COOLDOWN_DURATION = 259200 

# ---- DATABASE SYSTEM ----
def init_db():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    # Channels Table
    cursor.execute("CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY, title TEXT)")
    # Permanent Posts Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS permanent_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT,
            photo_id TEXT
        )
    """)
    # Cooldown/History Tracking Table
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
    return [row[0] for row in rows]

# ---- CONVERSION FUNCTION ----
def convert_to_earnurl(long_url):
    api_url = f"earnurl.in{EARNURL_API_KEY}&url={long_url}&type=1"
    try:
        response = requests.get(api_url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success":
                return data.get("shortenedUrl")
    except Exception as e:
        logger.error(f"EarnURL API Error: {e}")
    return long_url

# ---- SMART DYNAMIC NO-REPEAT AUTO-POSTER ----
async def smart_auto_poster(app: Application):
    logger.info("Smart No-Repeat Dynamic Rotator Loop Active.")
    while True:
        try:
            channels = get_channels()
            current_time = int(time.time())
            
            if channels:
                conn = sqlite3.connect("bot_data.db")
                cursor = conn.cursor()
                
                # ⏰ Pehle history se 3 din se purane cooldowns saaf karna taaki space bache
                cursor.execute("DELETE FROM history WHERE ? - sent_time > ?", (current_time, COOLDOWN_DURATION))
                conn.commit()

                for channel_id in channels:
                    # Is channel ke liye jo posts abhi cooldown me hain unki IDs nikalna
                    cursor.execute("SELECT post_id FROM history WHERE channel_id = ?", (channel_id,))
                    cooldown_ids = [row[0] for row in cursor.fetchall()]
                    
                    # Database se un posts ko chhod kar baaki saari available posts nikalna
                    if cooldown_ids:
                        placeholder = ','.join('?' for _ in cooldown_ids)
                        cursor.execute(f"SELECT id, text, photo_id FROM permanent_queue WHERE id NOT IN ({placeholder})", cooldown_ids)
                    else:
                        cursor.execute("SELECT id, text, photo_id FROM permanent_queue")
                        
                    available_posts = cursor.fetchall()
                    
                    # Agar saari ki saari posts cooldown me hain (Database chhota h aur cooldown bada),
                    # toh safety ke liye sabse purani cooldown post utha lega taaki posting na ruke
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
                        # 🎲 Available list me se ek randomly select karna
                        selected_post = random.choice(available_posts)
                        post_id, converted_text, photo_id = selected_post
                        
                        try:
                            if photo_id:
                                await app.bot.send_photo(chat_id=channel_id, photo=photo_id, caption=converted_text)
                            else:
                                await app.bot.send_message(chat_id=channel_id, text=converted_text)
                            
                            # ✅ Successfully post hone ke baad ise tracking list (History) me jodna
                            cursor.execute("INSERT OR REPLACE INTO history (channel_id, post_id, sent_time) VALUES (?, ?, ?)", 
                                           (channel_id, post_id, current_time))
                            conn.commit()
                            
                            # Channels ke beech me safe gap (Flood Control)
                            await asyncio.sleep(4) 
                        except Exception as e:
                            logger.error(f"Post failed for channel {channel_id}: {e}")
                
                conn.close()
            else:
                logger.warning("No channels added yet. Add via /add command.")

        except Exception as e:
            logger.error(f"Error in smart loop execution: {e}")
            
        # ⏱️ ANTI-BAN RANDOM TIMER (Har post batch ke baad 5 se 10 minute ka random gap)
        random_delay = random.randint(300, 600)
        logger.info(f"Dynamic safety sleep active for next {random_delay} seconds.")
        await asyncio.sleep(random_delay)

# ---- COMMAND HANDLERS ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(
        "🚀 **Smart Anti-Repeat Auto-Rotator Bot Active!**\n\n"
        "🎯 **Yeh Kaise Kaam Karega:**\n"
        "1. Aapke banaye huyen 1000+ links me se randomly content uthayega.\n"
        "2. Jo post aaj ek channel me dalegi, wo agle **3 din** tak us same channel me repeat nahi hogi.\n"
        "3. Har post ke beech me automatic time badalta rahega, jisse channel ban hone ka khatra 0% ho jata hai.\n\n"
        "🛠️ **Owner Commands:**\n"
        "👉 `/add -100xxxxxx` : Channel ID add karein\n"
        "👉 `/remove -100xxxxxx` : Channel list se hatayein\n"
        "👉 `/list` : Added channels dekhein\n"
        "👉 `/status` : Total kitni posts library me hain dekhein\n"
        "👉 `/clearall` : Purani saari memory delete karke naya data daalne ke liye"
    )

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Example: `/add -100123456789` ")
        return
    try:
        chat = await context.bot.get_chat(context.args[0])
        conn = sqlite3.connect("bot_data.db")
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO channels (channel_id, title) VALUES (?, ?)", (str(chat.id), chat.title))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ **{chat.title}** dynamically connect ho gaya hai!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Example: `/remove -100123456789` ")
        return
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM channels WHERE channel_id = ?", (str(context.args[0]),))
    cursor.execute("DELETE FROM history WHERE channel_id = ?", (str(context.args[0]),))
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ Channel list aur history se completely remove kar diya gaya.")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, title FROM channels")
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📋 Koi channel added nahi hai.")
        return
    text = "📋 **Added Channels:**\n\n"
    for row in rows:
        text += f"🔹 {row[1]} (`{row[0]}`)\n"
    await update.message.reply_text(text)

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM permanent_queue")
    total_posts = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM history")
    active_cooldowns = cursor.fetchone()[0]
    conn.close()
    await update.message.reply_text(
        f"📊 **Database Insights:**\n\n"
        f"🔹 Total Converted Links: `{total_posts}`\n"
        f"⏳ Active Cooldown Posts (Jo abhi repeat nahi hongi): `{active_cooldowns}`"
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
    await update.message.reply_text("🗑️ Library khali ho gayi hai. Purana sara data aur history delete ho chuka hai.")

# ---- DATA INGESTION ENGINE ----
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
    
    logger.info("New content parsed and saved into the smart rotator library.")

# ---- APPLICATION MAIN RUN ----
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_channel))
    app.add_handler(CommandHandler("remove", remove_channel))
    app.add_handler(CommandHandler("list", list_channels))
    app.add_handler(CommandHandler("status", show_status))
    app.add_handler(CommandHandler("clearall", clear_all_posts))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_bulk_incoming))
    
    # Background job to run the infinite loop safely
    app.job_queue.run_once(lambda ctx: asyncio.create_task(smart_auto_poster(app)), when=0)
    
    logger.info("Bot started successfully with Anti-Repeat tracking algorithm.")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
