# -*- coding: utf-8 -*-

# ---- Core Python Imports ----
import os
import io
import sys
import re
import requests
import sqlite3
from threading import Thread

# --- Third-party Library Imports ---
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.errors import UserNotParticipant
from flask import Flask
from dotenv import load_dotenv

# ---- 1. CONFIGURATION AND SETUP ----
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL")

# ---- Database Setup ----
DB_FILE = "bot_settings.db"
def db_query(query, params=(), fetch=None):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        if fetch == 'one': return cursor.fetchone()

db_query('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, watermark_text TEXT, channel_id TEXT)')

# ---- Global Variables & Bot Initialization ----
user_conversations = {}
bot = Client("moviebot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---- Flask App ----
app = Flask(__name__)
@app.route('/')
def home(): return "✅ Dual-Format Post Bot is Running!"
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ---- Font Configuration ----
try:
    FONT_BOLD = ImageFont.truetype("Poppins-Bold.ttf", 32)
    FONT_REGULAR = ImageFont.truetype("Poppins-Regular.ttf", 24)
    FONT_SMALL = ImageFont.truetype("Poppins-Regular.ttf", 18)
    FONT_WATERMARK = ImageFont.truetype("Poppins-Bold.ttf", 22)
except IOError:
    print("⚠️ Warning: Font files not found.")
    FONT_BOLD = FONT_REGULAR = FONT_SMALL = FONT_WATERMARK = ImageFont.load_default()

# ---- 2. DECORATORS AND HELPER FUNCTIONS ----

def force_subscribe(func):
    """Decorator to check for channel membership."""
    async def wrapper(client, message):
        if FORCE_SUB_CHANNEL:
            try:
                await client.get_chat_member(chat_id=FORCE_SUB_CHANNEL, user_id=message.from_user.id)
            except UserNotParticipant:
                link = f"https://t.me/{FORCE_SUB_CHANNEL.replace('@', '')}"
                return await message.reply_text(
                    "❗ **Join Our Channel to Use Me**",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👉 Join Channel", url=link)]])
                )
        await func(client, message)
    return wrapper

# ---- 3. TMDB API & CONTENT GENERATION ----

def search_tmdb(query: str):
    """Searches TMDB."""
    year, name = None, query.strip()
    match = re.search(r'(.+?)\s*\(?(\d{4})\)?$', query)
    if match: name, year = match.group(1).strip(), match.group(2)
    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={name}"
    if year: url += f"&year={year}"
    try:
        r = requests.get(url); r.raise_for_status()
        return [res for res in r.json().get("results", []) if res.get("media_type") in ["movie", "tv"]][:5]
    except: return []

def get_tmdb_details(media_type: str, media_id: int):
    """Fetches full details."""
    url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}&append_to_response=credits,videos"
    try:
        r = requests.get(url); r.raise_for_status(); return r.json()
    except: return None

# --- NEW: Function to add watermark to the original poster ---
def watermark_poster(poster_url: str, watermark_text: str):
    """Downloads a poster, adds a watermark, and returns it as a file object."""
    if not poster_url or not watermark_text: return None
    try:
        img_data = requests.get(poster_url).content
        img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        draw = ImageDraw.Draw(img)
        
        bbox = draw.textbbox((0, 0), watermark_text, font=FONT_WATERMARK)
        text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = img.width - text_width - 15
        y = img.height - text_height - 15
        draw.text((x, y), watermark_text, font=FONT_WATERMARK, fill=(255, 255, 255, 150))
        
        buffer = io.BytesIO()
        buffer.name = "watermarked_poster.png"
        img.save(buffer, "PNG")
        buffer.seek(0)
        return buffer
    except Exception as e:
        print(f"Watermarking error: {e}")
        return None

# --- NEW: Function to generate the specific caption for Telegram channels ---
def generate_channel_caption(data: dict, language: str, fixed_links: dict):
    """Generates the caption for the Telegram channel post."""
    title = data.get("title") or data.get("name") or "N/A"
    year = (data.get("release_date") or data.get("first_air_date") or "----")[:4]
    genres = ", ".join([g["name"] for g in data.get("genres", [])[:2]]) # Limit to 2 genres
    
    caption = (
        f"**{title} ({year})**\n\n"
        f"🎭 **Genres:** {genres}\n"
        f"🔊 **Language:** {language}\n\n"
        "📥 **Download Links** 👇\n"
    )
    
    # Add fixed links if they exist
    if fixed_links.get("480p"): caption += f"🔹 **480p:** [Link Here]({fixed_links['480p']})\n"
    if fixed_links.get("720p"): caption += f"🔹 **720p:** [Link Here]({fixed_links['720p']})\n"
    if fixed_links.get("1080p"): caption += f"🔹 **1080p:** [Link Here]({fixed_links['1080p']})\n"
        
    return caption

def generate_html(data: dict, all_links: list):
    """Generates the HTML for Blogger (this function is mostly unchanged)."""
    title = data.get("title") or data.get("name") or "N/A"
    year = (data.get("release_date") or data.get("first_air_date") or "----")[:4]
    rating = round(data.get('vote_average', 0), 1)
    overview = data.get("overview", "No overview available.")
    genres = ", ".join([g["name"] for g in data.get("genres", [])] or ["N/A"])
    poster = f"https://image.tmdb.org/t/p/w500{data['poster_path']}" if data.get('poster_path') else ""
    backdrop = f"https://image.tmdb.org/t/p/original{data['backdrop_path']}" if data.get('backdrop_path') else ""
    trailer_key = next((v['key'] for v in data.get('videos', {}).get('results', []) if v['site'] == 'YouTube'), None)
    
    trailer_button = f'<a href="https://www.youtube.com/watch?v={trailer_key}" target="_blank" class="trailer-button">🎬 Watch Trailer</a>' if trailer_key else ""
    # Use all collected links for the HTML
    download_buttons = "".join([f'<a href="{link["url"]}" target="_blank">🔽 {link["label"]}</a>' for link in all_links])
    
    return f"""
<!-- Generated by Bot -->
<style>.movie-card-container{{max-width:700px;margin:20px auto;background:#1c1c1c;border-radius:20px;padding:20px;color:#e0e0e0;font-family:sans-serif;overflow:hidden;}}.movie-header{{text-align:center;color:#00bcd4;margin-bottom:20px;}}.movie-content{{display:flex;flex-wrap:wrap;align-items:flex-start;}}.movie-poster-container{{flex:1 1 200px;margin-right:20px;}}.movie-poster-container img{{width:100%;height:auto;border-radius:15px;display:block;}}.movie-details{{flex:2 1 300px;}}.movie-details p{{margin:0 0 10px 0;}}.movie-details b{{color:#00e676;}}.backdrop-container{{width:100%;margin-top:20px;}}.backdrop-container img{{max-width:100%;height:auto;border-radius:15px;display:block;margin:auto;}}.action-buttons{{text-align:center;margin-top:20px;width:100%;}}.action-buttons a{{display:inline-block;background:linear-gradient(45deg,#ff512f,#dd2476);color:white!important;padding:12px 25px;margin:8px;border-radius:25px;text-decoration:none;font-weight:600;}}.action-buttons .trailer-button{{background:#c4302b;}}</style>
<div class="movie-card-container"><h2 class="movie-header">{title} ({year})</h2><div class="movie-content"><div class="movie-poster-container"><img src="{poster}" alt="{title}"/></div><div class="movie-details"><p><b>Genre:</b> {genres}</p><p><b>Rating:</b> ⭐ {rating}/10</p><p><b>Overview:</b> {overview}</p></div></div><div class="backdrop-container"><a href="{backdrop}" target="_blank"><img src="{backdrop}" alt="{title}"/></a></div><div class="action-buttons">{trailer_button}{download_buttons or ""}</div></div>
"""

# ---- 4. BOT HANDLERS ----

# -- Command Handlers --
@bot.on_message(filters.command("start") & filters.private)
@force_subscribe
async def start_cmd(client, message: Message):
    db_query("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
    await message.reply_text(
        "👋 **Welcome!** Send me a movie or series name.\n\n"
        "I will generate two types of posts: one for your **Blogger** site and another for your **Telegram Channel**."
    )

@bot.on_message(filters.command("setwatermark") & filters.private)
@force_subscribe
async def watermark_cmd(client, message: Message):
    text = " ".join(message.command[1:]) if len(message.command) > 1 else None
    db_query("UPDATE users SET watermark_text = ? WHERE user_id = ?", (text, message.from_user.id))
    await message.reply_text(f"✅ Watermark {'set to: `' + text + '`' if text else 'removed.'}")

@bot.on_message(filters.command("setchannel") & filters.private)
@force_subscribe
async def channel_cmd(client, message: Message):
    cid = message.command[1] if len(message.command) > 1 else None
    db_query("UPDATE users SET channel_id = ? WHERE user_id = ?", (cid, message.from_user.id))
    await message.reply_text(f"✅ Channel {'set to: `' + cid + '`' if cid else 'removed.'}")

@bot.on_message(filters.command("cancel") & filters.private)
@force_subscribe
async def cancel_cmd(client, message: Message):
    if message.from_user.id in user_conversations:
        del user_conversations[message.from_user.id]
        await message.reply_text("✅ Operation cancelled.")

# -- Main Text and Conversation Handler --
@bot.on_message(filters.text & filters.private & ~filters.command(["start", "setwatermark", "setchannel", "cancel"]))
@force_subscribe
async def main_handler(client, message: Message):
    uid = message.from_user.id
    if uid in user_conversations and user_conversations[uid].get("state") != "done":
        await conversation_flow_handler(client, message)
    else:
        await search_handler(client, message)

async def search_handler(client, message: Message):
    processing_msg = await message.reply_text("🔍 Searching...")
    results = search_tmdb(message.text.strip())
    if not results: return await processing_msg.edit_text("❌ No content found.")
    
    buttons = [[InlineKeyboardButton(
        f"{'🎬' if r['media_type'] == 'movie' else '📺'} {r.get('title') or r.get('name')} ({(r.get('release_date') or r.get('first_air_date') or '----').split('-')[0]})",
        callback_data=f"select_{r['media_type']}_{r['id']}"
    )] for r in results]
    await processing_msg.edit_text("**👇 Choose from results:**", reply_markup=InlineKeyboardMarkup(buttons))

async def conversation_flow_handler(client, message: Message):
    """Manages the step-by-step conversation for collecting data."""
    uid = message.from_user.id
    convo = user_conversations.get(uid)
    if not convo: return
    
    state = convo.get("state")
    text = message.text.strip()
    
    # State machine for conversation
    if state == "wait_language":
        convo["language"] = text
        convo["state"] = "wait_480p"
        await message.reply_text("✅ Language set. Now, send the **480p** link or type `skip`.")
    elif state == "wait_480p":
        if text.lower() != 'skip': convo["fixed_links"]["480p"] = text
        convo["state"] = "wait_720p"
        await message.reply_text("✅ Got it. Now, send the **720p** link or type `skip`.")
    elif state == "wait_720p":
        if text.lower() != 'skip': convo["fixed_links"]["720p"] = text
        convo["state"] = "wait_1080p"
        await message.reply_text("✅ Okay. Now, send the **1080p** link or type `skip`.")
    elif state == "wait_1080p":
        if text.lower() != 'skip': convo["fixed_links"]["1080p"] = text
        convo["state"] = "ask_custom_links"
        buttons = [
            [InlineKeyboardButton("✅ Yes, add more", callback_data=f"addcustom_yes_{uid}")],
            [InlineKeyboardButton("❌ No, I'm done", callback_data=f"addcustom_no_{uid}")],
        ]
        await message.reply_text(
            "**Blogger Links (Optional)**\n\nDo you want to add more custom links (for series episodes, etc.) for the Blogger post?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    elif state == "wait_custom_label":
        convo["current_label"] = text
        convo["state"] = "wait_custom_url"
        await message.reply_text(f"OK, now send the URL for **'{text}'**.")
    elif state == "wait_custom_url":
        if not text.startswith("http"): return await message.reply_text("⚠️ Invalid URL.")
        convo["custom_links"].append({"label": convo["current_label"], "url": text})
        del convo["current_label"]
        buttons = [
            [InlineKeyboardButton("✅ Add another", callback_data=f"addcustom_yes_{uid}")],
            [InlineKeyboardButton("✅ I'm done", callback_data=f"addcustom_no_{uid}")],
        ]
        await message.reply_text("Link added! Add another?", reply_markup=InlineKeyboardMarkup(buttons))

# --- Callback Handlers ---
@bot.on_callback_query(filters.regex("^select_"))
async def selection_cb(client, cb: Message):
    await cb.answer("Fetching details...")
    _, media_type, media_id_str = cb.data.split("_", 2)
    details = get_tmdb_details(media_type, int(media_id_str))
    if not details: return await cb.message.edit_text("❌ Failed to get details.")

    uid = cb.from_user.id
    user_conversations[uid] = {
        "details": details,
        "language": None,
        "fixed_links": {},
        "custom_links": [],
        "state": "wait_language"
    }
    await cb.message.edit_text(
        "**Step 1: Language**\n\nPlease enter the language for this post (e.g., `English`, `Hindi Dubbed`)."
    )

@bot.on_callback_query(filters.regex("^addcustom_"))
async def custom_link_cb(client, cb: Message):
    action, uid_str = cb.data.split("_", 1)
    uid = int(uid_str)
    if cb.from_user.id != uid: return await cb.answer("Not for you!", show_alert=True)
    convo = user_conversations.get(uid)
    if not convo: return await cb.answer("Session expired.", show_alert=True)
    
    if action == "addcustom_yes":
        convo["state"] = "wait_custom_label"
        await cb.message.edit_text("**Custom Link:**\n\nPlease send the button text (e.g., `Episode 01 480p`).")
    else: # "no"
        await cb.message.edit_text("✅ All info collected! Generating your posts now...")
        await generate_final_content(client, uid, cb.message.chat.id, cb.message)

# -- Final Content Generation & Action Callbacks --
async def generate_final_content(client, uid, cid, msg):
    convo = user_conversations.get(uid)
    if not convo: return

    details = convo["details"]
    lang = convo["language"]
    fixed_links = convo["fixed_links"]
    custom_links = convo["custom_links"]
    
    # Prepare all links for Blogger
    all_blogger_links = []
    for q, url in fixed_links.items(): all_blogger_links.append({"label": f"Download {q}", "url": url})
    all_blogger_links.extend(custom_links)

    await msg.edit_text("📝 Generating content for Blogger & Channel...")
    html_code = generate_html(details, all_blogger_links)
    channel_caption = generate_channel_caption(details, lang, fixed_links)
    
    await msg.edit_text("🎨 Applying watermark to poster...")
    watermark_data = db_query("SELECT watermark_text FROM users WHERE user_id=?", (uid,), 'one')
    watermark = watermark_data[0] if watermark_data else None
    poster_url = f"https://image.tmdb.org/t/p/w500{details['poster_path']}" if details.get('poster_path') else None
    watermarked_poster = watermark_poster(poster_url, watermark)

    convo["generated"] = {
        "html": html_code,
        "channel_caption": channel_caption,
        "channel_poster": watermarked_poster
    }
    convo["state"] = "done"

    channel_data = db_query("SELECT channel_id FROM users WHERE user_id=?", (uid,), 'one')
    channel_id = channel_data[0] if channel_data and channel_data[0] else None
    
    buttons = [[InlineKeyboardButton("📝 Get Blogger HTML", callback_data=f"get_html_{uid}")]]
    if channel_id:
        buttons.append([InlineKeyboardButton("📢 Post to Channel", callback_data=f"post_channel_{uid}")])

    await msg.delete()
    await client.send_message(
        cid,
        f"✅ **Content Generated for `{details.get('title') or details.get('name')}`**\n\nChoose an option below:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@bot.on_callback_query(filters.regex("^(get_|post_)"))
async def final_action_cb(client, cb: Message):
    try: action, uid_str = cb.data.rsplit("_", 1); uid = int(uid_str)
    except: return await cb.answer("Error.", show_alert=True)
    if cb.from_user.id != uid: return await cb.answer("Not for you!", show_alert=True)
    convo = user_conversations.get(uid);
    if not convo or "generated" not in convo: return await cb.answer("Session expired.", show_alert=True)
    
    gen = convo["generated"]
    
    if action == "get_html":
        await cb.answer()
        html = gen["html"]
        if len(html) > 4000:
            title = (convo["details"].get("title") or convo["details"].get("name") or "post").replace(" ", "_")
            await client.send_document(cb.message.chat.id, document=io.BytesIO(html.encode('utf-8')), file_name=f"{title}.html")
        else:
            await client.send_message(cb.message.chat.id, f"```html\n{html}\n```", parse_mode=enums.ParseMode.MARKDOWN)

    elif action == "post_channel":
        channel_data = db_query("SELECT channel_id FROM users WHERE user_id=?", (uid,), 'one')
        channel_id = channel_data[0] if channel_data and channel_data[0] else None
        if not channel_id: return await cb.answer("Channel not set.", show_alert=True)
        
        await cb.answer("Posting...", show_alert=False)
        try:
            poster, caption = gen["channel_poster"], gen["channel_caption"]
            if poster:
                poster.seek(0)
                await client.send_photo(channel_id, photo=poster, caption=caption)
            else: # Fallback if no poster
                await client.send_message(channel_id, caption)
            
            await cb.edit_message_reply_markup(reply_markup=None)
            await cb.message.reply_text(f"✅ Successfully posted to `{channel_id}`!")
        except Exception as e:
            await cb.message.reply_text(f"❌ Failed to post. Error: {e}")

# ---- 5. START THE BOT ----
if __name__ == "__main__":
    print("🚀 Bot is starting... (Dual-Format Final Version)")
    bot.run()
    print("👋 Bot has stopped.")
