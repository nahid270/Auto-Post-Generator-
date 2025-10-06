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
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL") # Channel for Force Subscribe

# ---- Database Setup (for user settings) ----
DB_FILE = "bot_settings.db"
def db_query(query, params=(), fetch=None):
    """A simple helper function to interact with the SQLite database."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        if fetch == 'one': return cursor.fetchone()

# Create table for user-specific settings if it doesn't exist
db_query(
    'CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, watermark_text TEXT, channel_id TEXT)'
)

# ---- Global Variables & Bot Initialization ----
user_conversations = {}
bot = Client("moviebot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---- Flask App for Keep-Alive ----
app = Flask(__name__)
@app.route('/')
def home():
    return "✅ Bot is Running Perfectly!"
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ---- Font Configuration ----
try:
    FONT_BOLD = ImageFont.truetype("Poppins-Bold.ttf", 32)
    FONT_REGULAR = ImageFont.truetype("Poppins-Regular.ttf", 24)
    FONT_SMALL = ImageFont.truetype("Poppins-Regular.ttf", 18)
    FONT_WATERMARK = ImageFont.truetype("Poppins-Bold.ttf", 22)
except IOError:
    print("⚠️ Warning: Font files not found. Using default fonts.")
    FONT_BOLD = FONT_REGULAR = FONT_SMALL = FONT_WATERMARK = ImageFont.load_default()

# ---- 2. HELPER FUNCTIONS AND DECORATORS ----

def force_subscribe(func):
    """Decorator to check if a user is a member of the force subscribe channel."""
    async def wrapper(client, message):
        if FORCE_SUB_CHANNEL:
            try:
                # Check member status
                await client.get_chat_member(chat_id=FORCE_SUB_CHANNEL, user_id=message.from_user.id)
            except UserNotParticipant:
                # If user is not a member, send a message with a join link
                channel_link = f"https://t.me/{FORCE_SUB_CHANNEL.replace('@', '')}"
                return await message.reply_text(
                    "❗ **Join Our Channel to Use Me**\n\n"
                    "To use this bot, you must be a member of our channel. It helps us grow!",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👉 Join Channel", url=channel_link)]])
                )
        # If user is a member, proceed with the original function
        await func(client, message)
    return wrapper

# ---- 3. TMDB API & CONTENT GENERATION FUNCTIONS ----

def search_tmdb(query: str):
    """Searches TMDB for movies/TV shows."""
    year, name = None, query.strip()
    match = re.search(r'(.+?)\s*\(?(\d{4})\)?$', query)
    if match:
        name, year = match.group(1).strip(), match.group(2)
    
    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={name}"
    if year: url += f"&year={year}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        results = [r for r in response.json().get("results", []) if r.get("media_type") in ["movie", "tv"]]
        return results[:5]
    except requests.exceptions.RequestException as e:
        print(f"TMDB Search Error: {e}")
        return []

def get_tmdb_details(media_type: str, media_id: int):
    """Fetches full details for a specific movie/TV show."""
    url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}&append_to_response=credits,videos"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"TMDB Details Error: {e}")
        return None

def generate_caption(data: dict):
    """Generates a clean, formatted text caption."""
    title = data.get("title") or data.get("name") or "N/A"
    year = (data.get("release_date") or data.get("first_air_date") or "----")[:4]
    rating = f"⭐ {round(data.get('vote_average', 0), 1)}/10"
    genres = ", ".join([g["name"] for g in data.get("genres", [])] or ["N/A"])
    overview = data.get("overview", "N/A")
    director = next((m["name"] for m in data.get("credits", {}).get("crew", []) if m.get("job") == "Director"), "N/A")
    cast = ", ".join([a["name"] for a in data.get("credits", {}).get("cast", [])[:5]] or ["N/A"])
    
    return (
        f"🎬 **{title} ({year})**\n\n"
        f"**Rating:** {rating}\n**Genres:** {genres}\n**Director:** {director}\n**Cast:** {cast}\n\n"
        f"**Plot:** _{overview[:500]}{'...' if len(overview) > 500 else ''}_"
    )

def generate_html(data: dict, links: list):
    """Generates a robust, Blogger-friendly HTML snippet."""
    title = data.get("title") or data.get("name") or "N/A"
    year = (data.get("release_date") or data.get("first_air_date") or "----")[:4]
    rating = round(data.get('vote_average', 0), 1)
    overview = data.get("overview", "No overview available.")
    genres = ", ".join([g["name"] for g in data.get("genres", [])] or ["N/A"])
    poster = f"https://image.tmdb.org/t/p/w500{data['poster_path']}" if data.get('poster_path') else ""
    backdrop = f"https://image.tmdb.org/t/p/original{data['backdrop_path']}" if data.get('backdrop_path') else ""
    trailer_key = next((v['key'] for v in data.get('videos', {}).get('results', []) if v['site'] == 'YouTube'), None)
    
    trailer_button = f'<a href="https://www.youtube.com/watch?v={trailer_key}" target="_blank" class="trailer-button">🎬 Watch Trailer</a>' if trailer_key else ""
    download_buttons = "".join([f'<a href="{link["url"]}" target="_blank">🔽 {link["label"]}</a>' for link in links])
    
    return f"""
<!-- Generated by Bot -->
<style>
.movie-card-container{{max-width:700px;margin:20px auto;background:#1c1c1c;border-radius:20px;padding:20px;color:#e0e0e0;font-family:sans-serif;overflow:hidden;}}
.movie-header{{text-align:center;color:#00bcd4;margin-bottom:20px;}}
.movie-content{{display:flex;flex-wrap:wrap;align-items:flex-start;}}
.movie-poster-container{{flex:1 1 200px;margin-right:20px;}}
.movie-poster-container img{{width:100%;height:auto;border-radius:15px;display:block;}}
.movie-details{{flex:2 1 300px;}}
.movie-details p{{margin:0 0 10px 0;}}
.movie-details b{{color:#00e676;}}
.backdrop-container{{width:100%;margin-top:20px;}}
.backdrop-container img{{max-width:100%;height:auto;border-radius:15px;display:block;margin:auto;}}
.action-buttons{{text-align:center;margin-top:20px;width:100%;}}
.action-buttons a{{display:inline-block;background:linear-gradient(45deg,#ff512f,#dd2476);color:white!important;padding:12px 25px;margin:8px;border-radius:25px;text-decoration:none;font-weight:600;}}
.action-buttons .trailer-button{{background:#c4302b;}}
</style>
<div class="movie-card-container">
<h2 class="movie-header">{title} ({year})</h2>
<div class="movie-content">
<div class="movie-poster-container"><img src="{poster}" alt="{title} Poster"/></div>
<div class="movie-details"><p><b>Genre:</b> {genres}</p><p><b>Rating:</b> ⭐ {rating}/10</p><p><b>Overview:</b> {overview}</p></div>
</div>
<div class="backdrop-container"><a href="{backdrop}" target="_blank"><img src="{backdrop}" alt="{title} Backdrop"/></a></div>
<div class="action-buttons">{trailer_button}{download_buttons or ""}</div>
</div>
"""

def generate_image(data: dict, user_id: int):
    """Generates a custom image with an optional watermark."""
    try:
        # Get user's watermark text from DB
        watermark_data = db_query("SELECT watermark_text FROM users WHERE user_id = ?", (user_id,), fetch='one')
        watermark_text = watermark_data[0] if watermark_data and watermark_data[0] else None

        poster_url = f"https://image.tmdb.org/t/p/w500{data['poster_path']}" if data.get('poster_path') else None
        if not poster_url: return None
        
        poster_img = Image.open(io.BytesIO(requests.get(poster_url).content)).convert("RGBA").resize((400, 600))

        if data.get('backdrop_path'):
            backdrop_url = f"https://image.tmdb.org/t/p/w1280{data['backdrop_path']}"
            bg_img = Image.open(io.BytesIO(requests.get(backdrop_url).content)).convert("RGBA").resize((1280, 720))
            bg_img = bg_img.filter(ImageFilter.GaussianBlur(3))
            darken_layer = Image.new('RGBA', bg_img.size, (0, 0, 0, 128))
            bg_img = Image.alpha_composite(bg_img, darken_layer)
        else:
            bg_img = Image.new('RGBA', (1280, 720), (10, 10, 20))
        
        bg_img.paste(poster_img, (50, 60), poster_img)
        draw = ImageDraw.Draw(bg_img)
        
        # Draw text details
        title = data.get("title") or data.get("name") or "N/A"
        year = (data.get("release_date") or data.get("first_air_date") or "----")[:4]
        draw.text((480, 80), f"{title} ({year})", font=FONT_BOLD, fill="white")
        draw.text((480, 140), f"⭐ {round(data.get('vote_average', 0), 1)}/10", font=FONT_REGULAR, fill="#00e676")
        draw.text((480, 180), " | ".join([g["name"] for g in data.get("genres", [])]), font=FONT_SMALL, fill="#00bcd4")
        overview, y_text = data.get("overview", ""), 250
        lines = [overview[i:i+80] for i in range(0, len(overview), 80)]
        for line in lines[:7]:
            draw.text((480, y_text), line, font=FONT_REGULAR, fill="#E0E0E0")
            y_text += 30

        # Add watermark if it exists
        if watermark_text:
            bbox = draw.textbbox((0, 0), watermark_text, font=FONT_WATERMARK)
            text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = bg_img.width - text_width - 20
            y = bg_img.height - text_height - 20
            draw.text((x, y), watermark_text, font=FONT_WATERMARK, fill=(255, 255, 255, 150)) # Semi-transparent white
            
        img_buffer = io.BytesIO()
        img_buffer.name = "poster.png"
        bg_img.save(img_buffer, format="PNG")
        img_buffer.seek(0)
        return img_buffer
    except Exception as e:
        print(f"Image Generation Error: {e}")
        return None


# ---- 4. BOT HANDLERS ----

@bot.on_message(filters.command("start") & filters.private)
@force_subscribe
async def start_cmd(client, message: Message):
    # Ensure user is in the database
    db_query("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
    await message.reply_text(
        "👋 **Welcome!** Send me a movie or series name to get started.\n\n"
        "**Commands:**\n"
        "`/setwatermark Your Text` - Add a watermark to generated images.\n"
        "`/setchannel @ID` - Set the channel for direct posting.\n"
        "`/cancel` - Cancel any ongoing operation."
    )

@bot.on_message(filters.command("setwatermark") & filters.private)
@force_subscribe
async def watermark_cmd(client, message: Message):
    user_id = message.from_user.id
    watermark_text = " ".join(message.command[1:]) if len(message.command) > 1 else None
    db_query("UPDATE users SET watermark_text = ? WHERE user_id = ?", (watermark_text, user_id))
    reply_text = f"✅ Watermark set to: `{watermark_text}`" if watermark_text else "✅ Watermark has been removed."
    await message.reply_text(reply_text)

@bot.on_message(filters.command("setchannel") & filters.private)
@force_subscribe
async def channel_cmd(client, message: Message):
    user_id = message.from_user.id
    channel_id = message.command[1] if len(message.command) > 1 else None
    db_query("UPDATE users SET channel_id = ? WHERE user_id = ?", (channel_id, user_id))
    reply_text = f"✅ Posting channel set to: `{channel_id}`" if channel_id else "✅ Posting channel has been removed."
    await message.reply_text(reply_text)

@bot.on_message(filters.command("cancel") & filters.private)
@force_subscribe
async def cancel_cmd(client, message: Message):
    if message.from_user.id in user_conversations:
        del user_conversations[message.from_user.id]
        await message.reply_text("✅ Operation cancelled.")

# Main handler for text messages (searches and link conversation)
@bot.on_message(filters.text & filters.private & ~filters.command(["start", "setwatermark", "setchannel", "cancel"]))
@force_subscribe
async def text_handler(client, message: Message):
    user_id = message.from_user.id
    # If user is in a conversation, route to the link handler
    if user_id in user_conversations and user_conversations[user_id].get("state") != "done":
        return await link_conversation_handler(client, message)
    
    # Otherwise, perform a new search
    processing_msg = await message.reply_text("🔍 Searching...")
    results = search_tmdb(message.text.strip())
    if not results:
        return await processing_msg.edit_text("❌ No content found. Please check the spelling.")
    
    buttons = []
    for r in results:
        title = r.get('title') or r.get('name')
        year = (r.get('release_date') or r.get('first_air_date') or '----').split('-')[0]
        icon = '🎬' if r['media_type'] == 'movie' else '📺'
        buttons.append([
            InlineKeyboardButton(
                f"{icon} {title} ({year})",
                callback_data=f"select_{r['media_type']}_{r['id']}"
            )
        ])
    await processing_msg.edit_text("**👇 Choose from the search results:**", reply_markup=InlineKeyboardMarkup(buttons))

# --- Conversation and Callback Handlers ---

@bot.on_callback_query(filters.regex("^select_"))
async def selection_cb(client, cb: Message):
    await cb.answer("Fetching details...")
    _, media_type, media_id_str = cb.data.split("_", 2)
    
    details = get_tmdb_details(media_type, int(media_id_str))
    if not details:
        return await cb.message.edit_text("❌ Failed to get details. Please try again.")

    user_id = cb.from_user.id
    user_conversations[user_id] = {"details": details, "links": []}
    
    buttons = [
        [InlineKeyboardButton("✅ Yes, add links", callback_data=f"addlink_yes_{user_id}")],
        [InlineKeyboardButton("❌ No, skip", callback_data=f"addlink_no_{user_id}")]
    ]
    await cb.message.edit_text(
        "**🔗 Add Download Links?**\n\nDo you want to add custom download links to this post?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@bot.on_callback_query(filters.regex("^(addlink|skip)_"))
async def addlink_cb(client, cb: Message):
    action, user_id_str = cb.data.split("_", 1)
    user_id = int(user_id_str)
    
    if cb.from_user.id != user_id: return await cb.answer("This is not for you!", show_alert=True)
    
    convo = user_conversations.get(user_id)
    if not convo: return await cb.answer("Session expired.", show_alert=True)

    if action == "addlink_yes":
        convo["state"] = "wait_link_label"
        await cb.message.edit_text(
            "**Step 1: Button Text**\n\nPlease send the text for the download button (e.g., `Season 1 720p`)."
        )
    else: # This handles "skip" and "no, I'm done"
        await cb.message.edit_text("✅ All set! Generating your content now...")
        await generate_final_content(client, user_id, cb.message.chat.id, cb.message)

async def link_conversation_handler(client, message: Message):
    user_id = message.from_user.id
    convo = user_conversations.get(user_id)
    if not convo: return
    
    state = convo.get("state")
    text = message.text.strip()

    if state == "wait_link_label":
        convo["current_label"] = text
        convo["state"] = "wait_link_url"
        await message.reply_text(f"**Step 2: URL**\n\nNow send the download URL for **'{text}'**.")
    
    elif state == "wait_link_url":
        if not text.startswith("http"):
            return await message.reply_text("⚠️ Invalid URL. Please send a valid link.")
        
        convo["links"].append({"label": convo["current_label"], "url": text})
        del convo["current_label"]
        
        buttons = [
            [InlineKeyboardButton("✅ Yes, add another", callback_data=f"addlink_yes_{user_id}")],
            [InlineKeyboardButton("✅ No, I'm done", callback_data=f"skip_{user_id}")]
        ]
        await message.reply_text(
            f"✅ Link added!\n\nDo you want to add another link?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

async def generate_final_content(client, user_id, chat_id, msg):
    convo = user_conversations.get(user_id)
    if not convo: return
    
    details, links = convo["details"], convo["links"]
    
    await msg.edit_text("📝 Generating caption & HTML...")
    caption = generate_caption(details)
    html_code = generate_html(details, links)
    
    await msg.edit_text("🎨 Generating image with watermark (if set)...")
    image_file = generate_image(details, user_id)

    convo["generated"] = {"caption": caption, "html": html_code, "image": image_file}
    convo["state"] = "done"

    channel_data = db_query("SELECT channel_id FROM users WHERE user_id = ?", (user_id,), 'one')
    channel_id = channel_data[0] if channel_data else None
    
    buttons = [
        [InlineKeyboardButton("📝 Get HTML Code", callback_data=f"get_html_{user_id}")],
        [InlineKeyboardButton("📄 Get Text Caption", callback_data=f"get_caption_{user_id}")],
    ]
    if channel_id:
        buttons.append([InlineKeyboardButton("📢 Post to Channel", callback_data=f"post_channel_{user_id}")])
    
    if hasattr(msg, 'from_user') and msg.from_user.is_self: await msg.delete()
    
    if image_file:
        await client.send_photo(chat_id, photo=image_file, caption=caption, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await client.send_message(chat_id, caption, reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_callback_query(filters.regex("^(get_|post_)"))
async def final_action_cb(client, cb: Message):
    try:
        action, user_id_str = cb.data.rsplit("_", 1)
        user_id = int(user_id_str)
    except (ValueError, IndexError): return await cb.answer("Error: Invalid callback.", show_alert=True)
    
    if cb.from_user.id != user_id: return await cb.answer("This is not for you!", show_alert=True)
    
    convo = user_conversations.get(user_id)
    if not convo or "generated" not in convo: return await cb.answer("Session expired.", show_alert=True)
    
    generated_content = convo["generated"]
    
    if action == "get_html":
        await cb.answer()
        html_code = generated_content["html"]
        if len(html_code) > 4000:
            title = (convo["details"].get("title") or convo["details"].get("name") or "post").replace(" ", "_")
            await client.send_document(cb.message.chat.id, document=io.BytesIO(html_code.encode('utf-8')), file_name=f"{title}.html")
        else:
            await client.send_message(cb.message.chat.id, f"```html\n{html_code}\n```", parse_mode=enums.ParseMode.MARKDOWN)

    elif action == "get_caption":
        await cb.answer()
        await client.send_message(cb.message.chat.id, generated_content["caption"])
        
    elif action == "post_channel":
        channel_data = db_query("SELECT channel_id FROM users WHERE user_id = ?", (user_id,), 'one')
        channel_id = channel_data[0] if channel_data else None
        if not channel_id: return await cb.answer("Channel not set. Use /setchannel first.", show_alert=True)
        
        await cb.answer("Posting...", show_alert=False)
        try:
            image_file = generated_content.get("image")
            caption = generated_content["caption"]
            if image_file:
                image_file.seek(0)
                await client.send_photo(channel_id, photo=image_file, caption=caption)
            else:
                await client.send_message(channel_id, caption)
            
            await cb.edit_message_reply_markup(reply_markup=None)
            await client.send_message(cb.message.chat.id, f"✅ Successfully posted to `{channel_id}`!")
        except Exception as e:
            await client.send_message(cb.message.chat.id, f"❌ Failed to post. Error: {e}")

# ---- 5. START THE BOT ----
if __name__ == "__main__":
    print("🚀 Bot is starting...")
    bot.run()
    print("👋 Bot has stopped.")
