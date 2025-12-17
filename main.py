# -*- coding: utf-8 -*-

# ---- Core Python Imports ----
import os
import io
import re
import requests
from threading import Thread
import logging

# --- Third-party Library Imports ---
from PIL import Image, ImageDraw, ImageFont
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.errors import UserNotParticipant
from flask import Flask
from dotenv import load_dotenv
import motor.motor_asyncio
import numpy as np
import cv2  # OpenCV for Face Detection

# ---- 1. CONFIGURATION AND SETUP ----
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL")
INVITE_LINK = os.getenv("INVITE_LINK")

# ⭐️ Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---- ✨ MongoDB Database Setup ✨ ----
DB_URI = os.getenv("DATABASE_URI")
DB_NAME = os.getenv("DATABASE_NAME", "MovieBotDB")
if not DB_URI:
    logger.critical("CRITICAL: DATABASE_URI is not set. Bot cannot start without a database.")
    exit()
db_client = motor.motor_asyncio.AsyncIOMotorClient(DB_URI)
db = db_client[DB_NAME]
users_collection = db.users

# ---- Global Variables & Bot Initialization ----
user_conversations = {}
bot = Client("UltimateMovieBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---- Flask App (for Keep-Alive) ----
app = Flask(__name__)
@app.route('/')
def home(): return "✅ Bot is Running!"
Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080))), daemon=True).start()

# ---- 2. DECORATORS AND HELPER FUNCTIONS ----

# Helper to download Haar Cascade file if not present
def download_cascade():
    cascade_file = "haarcascade_frontalface_default.xml"
    if not os.path.exists(cascade_file):
        logger.info(f"Downloading {cascade_file} for face detection...")
        url = "https://raw.githubusercontent.com/opencv/opencv/master/data/haarcascades/haarcascade_frontalface_default.xml"
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            with open(cascade_file, 'wb') as f:
                f.write(r.content)
            logger.info("Download complete.")
        except Exception as e:
            logger.error(f"Could not download cascade file. Face detection will be disabled. Error: {e}")
            return None
    return cascade_file

async def add_user_to_db(user):
    await users_collection.update_one(
        {'_id': user.id},
        {'$set': {'first_name': user.first_name}},
        upsert=True
    )

def force_subscribe(func):
    async def wrapper(client, message):
        if FORCE_SUB_CHANNEL:
            try:
                chat_id = int(FORCE_SUB_CHANNEL) if FORCE_SUB_CHANNEL.startswith("-100") else FORCE_SUB_CHANNEL
                await client.get_chat_member(chat_id, message.from_user.id)
            except UserNotParticipant:
                join_link = INVITE_LINK or f"https://t.me/{FORCE_SUB_CHANNEL.replace('@', '')}"
                return await message.reply_text("❗ **You must join our channel to use this bot.**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👉 Join Channel", url=join_link)]]))
        await func(client, message)
    return wrapper

async def shorten_link(user_id: int, long_url: str):
    user_data = await users_collection.find_one({'_id': user_id})
    if not user_data or 'shortener_api' not in user_data or 'shortener_url' not in user_data:
        return long_url 

    api_key = user_data['shortener_api']
    base_url = user_data['shortener_url']
    api_url = f"https://{base_url}/api?api={api_key}&url={long_url}"
    
    try:
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "success" and data.get("shortenedUrl"):
            logger.info(f"Successfully shortened URL for user {user_id}")
            return data["shortenedUrl"]
        else:
            logger.warning(f"Shortener API returned an error for user {user_id}: {data.get('message', 'Unknown error')}")
            return long_url
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to call shortener API for user {user_id}. Error: {e}")
        return long_url

def format_runtime(minutes: int):
    if not minutes or not isinstance(minutes, int): return "N/A"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if hours > 0 else f"{mins}m"

# ---- 3. TMDB API & CONTENT GENERATION ----

def search_tmdb_by_imdb(imdb_id: str):
    """IMDb ID (tt...) দিয়ে TMDB তে মুভি বা সিরিজ খোঁজার ফাংশন"""
    url = f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={TMDB_API_KEY}&external_source=imdb_id"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = data.get("movie_results", []) + data.get("tv_results", [])
        return results
    except Exception as e:
        logger.error(f"TMDB Find Error: {e}")
        return []

def search_tmdb(query: str):
    year, name = None, query.strip()
    match = re.search(r'(.+?)\s*\(?(\d{4})\)?$', query)
    if match: name, year = match.group(1).strip(), match.group(2)
    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={name}" + (f"&year={year}" if year else "")
    try:
        r = requests.get(url, timeout=10); r.raise_for_status()
        return [res for res in r.json().get("results", []) if res.get("media_type") in ["movie", "tv"]][:5]
    except Exception as e:
        logger.error(f"TMDB Search Error: {e}"); return []

def get_tmdb_details(media_type: str, media_id: int):
    url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}&append_to_response=credits"
    try:
        r = requests.get(url, timeout=10); r.raise_for_status(); return r.json()
    except Exception as e:
        logger.error(f"TMDB Details Error: {e}"); return None

def watermark_poster(poster_url: str, watermark_text: str, badge_text: str = None):
    if not poster_url: return None, "Poster URL not found."
    try:
        img_data = requests.get(poster_url, timeout=20).content
        original_img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        
        img = Image.new("RGBA", original_img.size)
        img.paste(original_img)
        
        draw = ImageDraw.Draw(img)

        # ---- Badge Text Logic ----
        if badge_text:
            badge_font_size = int(img.width / 9)
            try:
                badge_font = ImageFont.truetype("HindSiliguri-Bold.ttf", badge_font_size)
            except IOError:
                logger.warning("HindSiliguri-Bold.ttf not found. Using default font for badge.")
                badge_font = ImageFont.load_default()

            bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            
            x = (img.width - text_width) / 2
            
            # --- Face Detection Logic ---
            y_pos = img.height * 0.03
            cascade_path = download_cascade()
            if cascade_path:
                try:
                    cv_image = np.array(original_img.convert('RGB'))
                    gray = cv2.cvtColor(cv_image, cv2.COLOR_RGB2GRAY)
                    face_cascade = cv2.CascadeClassifier(cascade_path)
                    faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
                    
                    padding = int(badge_font_size * 0.2)
                    text_box_y1 = y_pos + text_height + padding
                    is_collision = any(y_pos < (fy + fh) and text_box_y1 > fy for (fx, fy, fw, fh) in faces)
                    
                    if is_collision:
                        logger.info("Face detected at the top. Moving badge text lower.")
                        y_pos = img.height * 0.25
                except Exception as e:
                    logger.error(f"Face detection failed: {e}")

            y = y_pos

            padding = int(badge_font_size * 0.1)
            rect_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            rect_draw = ImageDraw.Draw(rect_layer)
            rect_draw.rectangle(
                (x - padding, y - padding, x + text_width + padding, y + text_height + padding), 
                fill=(0, 0, 0, 140)
            )
            img = Image.alpha_composite(img, rect_layer)
            draw = ImageDraw.Draw(img)

            gradient = Image.new('RGBA', (text_width, text_height), (0, 0, 0, 0))
            gradient_draw = ImageDraw.Draw(gradient)
            
            gradient_start_color = (255, 255, 0)
            gradient_end_color = (255, 20, 0)
            for i in range(text_width):
                ratio = i / text_width
                r = int(gradient_start_color[0] * (1 - ratio) + gradient_end_color[0] * ratio)
                g = int(gradient_start_color[1] * (1 - ratio) + gradient_end_color[1] * ratio)
                b = int(gradient_start_color[2] * (1 - ratio) + gradient_end_color[2] * ratio)
                gradient_draw.line([(i, 0), (i, text_height)], fill=(r, g, b, 255))
            
            mask = Image.new('L', (text_width, text_height), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.text((-bbox[0], -bbox[1]), badge_text, font=badge_font, fill=255)
            
            img.paste(gradient, (int(x), int(y)), mask)

        # ---- Watermark Logic ----
        if watermark_text:
            font_size = int(img.width / 12)
            try:
                font = ImageFont.truetype("Poppins-Bold.ttf", font_size)
            except IOError:
                font = ImageFont.load_default()
            
            thumbnail = img.resize((150, 150))
            colors = thumbnail.getcolors(150*150)
            text_color = (255, 255, 255, 230)
            if colors:
                dominant_color = sorted(colors, key=lambda x: x[0], reverse=True)[0][1]
                text_color = (255 - dominant_color[0], 255 - dominant_color[1], 255 - dominant_color[2], 230)

            bbox = draw.textbbox((0, 0), watermark_text, font=font)
            text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            wx = (img.width - text_width) / 2
            wy = img.height - text_height - (img.height * 0.05)
            draw.text((wx + 2, wy + 2), watermark_text, font=font, fill=(0, 0, 0, 128))
            draw.text((wx, wy), watermark_text, font=font, fill=text_color)
            
        buffer = io.BytesIO()
        buffer.name = "poster.png"
        img.convert("RGB").save(buffer, "PNG")
        buffer.seek(0)
        return buffer, None
    except requests.exceptions.RequestException as e:
        return None, f"Network Error: {e}"
    except Exception as e:
        return None, f"Image processing error. Error: {e}"

async def generate_channel_caption(data: dict, language: str, links: dict, user_data: dict):
    info = {
        "title": data.get("title") or data.get("name") or "N/A",
        "year": (data.get("release_date") or data.get("first_air_date") or "----")[:4],
        "genres": ", ".join([g["name"] for g in data.get("genres", [])[:3]]) or "N/A",
        "rating": f"{data.get('vote_average', 0):.1f}",
        "language": language,
        "runtime": format_runtime(data.get("runtime", 0) if 'runtime' in data else (data.get("episode_run_time") or [0])[0]),
        "link_480p": links.get('480p', ''),
        "link_720p": links.get('720p', ''),
        "link_1080p": links.get('1080p', ''),
    }

    caption_header = f"""🎬 **{info['title']} ({info['year']})**
━━━━━━━━━━━━━━━━━━━━━━━
⭐ **Rating:** {info['rating']}/10
🎭 **Genre:** {info['genres']}
🔊 **Language:** {info['language']}
⏰ **Runtime:** {info['runtime']}
━━━━━━━━━━━━━━━━━━━━━━━"""

    download_section_header = """👀 𝗪𝗔𝗧𝗖𝗛 𝗢𝗡𝗟𝗜𝗡𝗘/📤𝗗𝗢𝗪𝗡𝗟𝗢𝗔𝗗
👇  ℍ𝕚𝕘𝕙 𝕊𝕡𝕖𝕖𝕕 | ℕ𝕠 𝔹𝕦𝕗𝕗𝕖𝕣𝕚𝕟𝕘  👇"""
    
    download_links = ""
    if 'first_air_date' in data and links:
        sorted_seasons = sorted(links.keys(), key=lambda x: int(x))
        season_links = []
        for season_num in sorted_seasons:
            season_links.append(f"✅ **[Download Season {season_num}]({links[season_num]})**")
        download_links = "\n".join(season_links)
    else:
        movie_links = []
        if info['link_480p']: movie_links.append(f"**[Download 480p]({info['link_480p']})**")
        if info['link_720p']: movie_links.append(f"**[Download 720p]({info['link_720p']})**")
        if info['link_1080p']: movie_links.append(f"**[Download 1080p]({info['link_1080p']})**")
        download_links = "\n\n".join(movie_links)

    static_footer = """Movie ReQuest Group 
👇👇👇
https://t.me/Terabox_search_group

Premium Backup Group link 👇👇👇
https://t.me/+GL_XAS4MsJg4ODM1"""

    caption_parts = [caption_header, download_section_header]
    if download_links: caption_parts.append(download_links.strip())
    
    if user_data and user_data.get('tutorial_link'):
        tutorial_text = f"🎥 **How To Download:** **[Watch Tutorial]({user_data['tutorial_link']})**"
        caption_parts.append(tutorial_text)
    
    caption_parts.append(static_footer)
    return "\n\n".join(caption_parts)

# ---- 4. BOT HANDLERS ----
@bot.on_message(filters.command("start") & filters.private)
@force_subscribe
async def start_cmd(client, message: Message):
    await add_user_to_db(message.from_user)
    await message.reply_text(f"👋 **Welcome, {message.from_user.first_name}! I'm a Movie & Series Post Generator Bot.**\n\n"
        "**Available Commands:**\n"
        "🔹 `/post <name>` - Create a post for a movie or series.\n"
        "🔹 `/badge <text>` - Set a temporary text badge for the next post.\n"
        "🔹 `/cancel` - Cancel any ongoing process.\n\n"
        "**Channel Management:**\n"
        "🔹 `/addchannel <ID>` - Add a new channel for posting.\n"
        "🔹 `/delchannel <ID>` - Remove a saved channel.\n"
        "🔹 `/mychannels` - View your list of saved channels.\n\n"
        "**Settings:**\n"
        "🔹 `/settings` - View your current settings.\n"
        "🔹 `/setwatermark <text>` - Set a watermark for posters.\n"
        "🔹 `/setapi <API_KEY>` - Set your link shortener API Key.\n"
        "🔹 `/setdomain <URL>` - Set your shortener domain.\n"
        "🔹 `/settutorial <link>` - Set the download tutorial link.")

@bot.on_message(filters.command("badge") & filters.private)
@force_subscribe
async def set_badge_text(client, message: Message):
    uid = message.from_user.id
    if len(message.command) > 1:
        badge_text = " ".join(message.command[1:])
        if uid not in user_conversations:
            user_conversations[uid] = {}
        user_conversations[uid]['temp_badge_text'] = badge_text
        await message.reply_text(f"✅ **Badge text set to:** `{badge_text}`\n\nThis will be applied to your next `/post`.")
    else:
        if uid in user_conversations and 'temp_badge_text' in user_conversations[uid]:
            del user_conversations[uid]['temp_badge_text']
            await message.reply_text("✅ Badge text has been removed.")
        else:
            await message.reply_text("⚠️ **Usage:** `/badge Your Text Here`\nTo remove a badge, use `/badge` without any text.")

@bot.on_message(filters.command(["setwatermark", "cancel", "setapi", "setdomain", "settutorial", "settings"]) & filters.private)
@force_subscribe
async def settings_commands(client, message: Message):
    command = message.command[0].lower()
    uid = message.from_user.id
    await add_user_to_db(message.from_user)

    if command == "setwatermark":
        text = " ".join(message.command[1:]) if len(message.command) > 1 else None
        await users_collection.update_one({'_id': uid}, {'$set': {'watermark_text': text}}, upsert=True)
        await message.reply_text(f"✅ Watermark has been {'set to: `' + text + '`' if text else 'removed.'}")
            
    elif command == "cancel":
        if uid in user_conversations: del user_conversations[uid]; await message.reply_text("✅ Process cancelled.")
        else: await message.reply_text("🚫 No active process to cancel.")

    elif command == "setapi":
        if len(message.command) > 1:
            api_key = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$set': {'shortener_api': api_key}}, upsert=True)
            await message.reply_text(f"✅ Shortener API Key has been set: `{api_key}`")
        else: await message.reply_text("⚠️ Incorrect format!\n**Usage:** `/setapi <YOUR_API_KEY>`")

    elif command == "setdomain":
        if len(message.command) > 1:
            domain = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$set': {'shortener_url': domain}}, upsert=True)
            await message.reply_text(f"✅ Shortener domain has been set: `{domain}`")
        else: await message.reply_text("⚠️ Incorrect format!\n**Usage:** `/setdomain yourshortener.com`")

    elif command == "settutorial":
        if len(message.command) > 1:
            link = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$set': {'tutorial_link': link}}, upsert=True)
            await message.reply_text(f"✅ Tutorial link has been set: {link}")
        else:
            await users_collection.update_one({'_id': uid}, {'$unset': {'tutorial_link': ""}}); await message.reply_text("✅ Tutorial link removed.")

    elif command == "settings":
        user_data = await users_collection.find_one({'_id': uid})
        if not user_data: return await message.reply_text("You haven't saved any settings yet.")
        
        channels = user_data.get('channel_ids', [])
        channel_text = "\n".join([f"`{ch}`" for ch in channels]) if channels else "`Not Set`"

        settings_text = "**⚙️ Your Current Settings:**\n\n"
        settings_text += f"**Saved Channels:**\n{channel_text}\n\n"
        settings_text += f"**Watermark:** `{user_data.get('watermark_text', 'Not Set')}`\n"
        settings_text += f"**Tutorial Link:** `{user_data.get('tutorial_link', 'Not Set')}`\n"
        
        shortener_api = user_data.get('shortener_api')
        shortener_url = user_data.get('shortener_url')
        if shortener_api and shortener_url:
            settings_text += f"**Shortener API:** `{shortener_api}`\n**Shortener URL:** `{shortener_url}`\n"
        else: settings_text += "**Shortener:** `Not Set`\n"
            
        await message.reply_text(settings_text)

@bot.on_message(filters.command(["addchannel", "delchannel", "mychannels"]) & filters.private)
@force_subscribe
async def channel_management(client, message: Message):
    command = message.command[0].lower()
    uid = message.from_user.id
    
    if command == "addchannel":
        if len(message.command) > 1 and message.command[1].startswith("-100") and message.command[1][1:].isdigit():
            cid = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$addToSet': {'channel_ids': cid}}, upsert=True)
            await message.reply_text(f"✅ Channel `{cid}` added successfully.")
        else: await message.reply_text("⚠️ Invalid Channel ID. It must start with `-100`.\n**Usage:** `/addchannel -100...`")

    elif command == "delchannel":
        if len(message.command) > 1 and message.command[1].startswith("-100") and message.command[1][1:].isdigit():
            cid = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$pull': {'channel_ids': cid}})
            await message.reply_text(f"✅ Channel `{cid}` removed if it existed.")
        else: await message.reply_text("⚠️ Invalid Channel ID.\n**Usage:** `/delchannel -100...`")

    elif command == "mychannels":
        user_data = await users_collection.find_one({'_id': uid})
        channels = user_data.get('channel_ids', [])
        if not channels:
            return await message.reply_text("You have no saved channels. Use `/addchannel` to add one.")
        channel_text = "📋 **Your Saved Channels:**\n\n" + "\n".join([f"🔹 `{ch}`" for ch in channels])
        await message.reply_text(channel_text)

async def generate_final_post_preview(client, uid, cid, msg):
    convo = user_conversations.get(uid)
    if not convo: return
    
    user_data = await users_collection.find_one({'_id': uid})
    caption = await generate_channel_caption(convo["details"], convo["language"], convo["links"], user_data)
    watermark = user_data.get('watermark_text')
    
    badge = convo.pop('temp_badge_text', None) 
    
    poster_url = f"https://image.tmdb.org/t/p/w500{convo['details']['poster_path']}" if convo['details'].get('poster_path') else None
    
    await msg.edit_text("🖼️ Creating smart poster...")
    poster, error = watermark_poster(poster_url, watermark, badge_text=badge)
    
    await msg.delete()
    if error: await client.send_message(cid, f"⚠️ **Error creating poster:** `{error}`")

    poster_buffer = io.BytesIO(poster.read()) if poster else None
    
    preview_msg = await client.send_photo(cid, photo=io.BytesIO(poster_buffer.getvalue()) if poster_buffer else poster, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
    user_conversations[uid]['final_post'] = {'caption': caption, 'poster': poster_buffer}

    saved_channels = user_data.get('channel_ids', [])
    if saved_channels:
        buttons = []
        for channel_id in saved_channels:
            try:
                chat = await client.get_chat(int(channel_id))
                channel_name = chat.title
                buttons.append([InlineKeyboardButton(f"📢 Post to {channel_name}", callback_data=f"postto_{channel_id}")])
            except Exception as e:
                logger.warning(f"Could not get chat info for ID {channel_id}. Maybe bot was kicked? Error: {e}")
        
        if buttons:
            await client.send_message(cid, "**👆 This is a preview. Choose a channel to post to:**",
                reply_to_message_id=preview_msg.id,
                reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await client.send_message(cid, "⚠️ Could not find any valid channels. Make sure the bot is a member of the channels you've added.", reply_to_message_id=preview_msg.id)
    else:
        await client.send_message(cid, "✅ Preview generated. You have no channels saved. Use `/addchannel` to add one.", reply_to_message_id=preview_msg.id)

# --- UPDATED: New /post handler with TMDB Link Support ---
@bot.on_message(filters.command("post") & filters.private)
@force_subscribe
async def search_commands(client, message: Message):
    if len(message.command) == 1:
        return await message.reply_text("**Usage:**\n`/post Movie Name`\nOR\n`/post TMDB Link`\nOR\n`/post IMDb ID`")
    
    query = " ".join(message.command[1:]).strip()
    processing_msg = await message.reply_text(f"🔍 Searching for `{query}`...")

    results = []
    
    # Check for TMDB Link (themoviedb.org/movie/12345...)
    tmdb_link_match = re.search(r'(?:themoviedb\.org|tmdb\.org)/(movie|tv)/(\d+)', query)
    
    # Check for IMDb ID (tt12345...)
    imdb_match = re.search(r'(tt\d{6,})', query)
    
    try:
        if tmdb_link_match:
            # --- TMDB Link Logic ---
            media_type = tmdb_link_match.group(1) # movie or tv
            tmdb_id = tmdb_link_match.group(2)    # ID
            
            await processing_msg.edit_text(f"🔗 TMDB Link detected (ID: {tmdb_id}). Fetching...")
            details = get_tmdb_details(media_type, int(tmdb_id))
            
            if details:
                details['media_type'] = media_type 
                results = [details]
        
        elif imdb_match:
            # --- IMDb ID Logic ---
            imdb_id = imdb_match.group(1)
            await processing_msg.edit_text(f"🔗 IMDb ID `{imdb_id}` detected. Fetching...")
            results = search_tmdb_by_imdb(imdb_id)
            
        else:
            # --- Standard Name Search Logic ---
            results = search_tmdb(query)

    except Exception as e:
        logger.error(f"Search processing error: {e}")
        return await processing_msg.edit_text(f"❌ Error processing link: {e}")

    if not results:
        return await processing_msg.edit_text("❌ No results found. Please check the Name or Link.")
    
    buttons = []
    for r in results:
        m_type = r.get('media_type')
        if not m_type:
            if 'title' in r: m_type = 'movie'
            elif 'name' in r: m_type = 'tv'
            else: continue

        media_icon = '🎬' if m_type == 'movie' else '📺'
        title = r.get('title') or r.get('name')
        
        date = r.get('release_date') or r.get('first_air_date') or '----'
        year = date.split('-')[0]
        
        buttons.append([InlineKeyboardButton(
            f"{media_icon} {title} ({year})", 
            callback_data=f"select_post_{m_type}_{r['id']}"
        )])
        
    await processing_msg.edit_text("**👇 Choose from the results:**", reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_message(filters.text & filters.private)
@force_subscribe
async def conversation_handler(client, message: Message):
    uid, text = message.from_user.id, message.text.strip()
    convo = user_conversations.get(uid)
    if not convo or "state" not in convo: return
    
    state = convo["state"]
    processing_msg = None

    async def process_link(quality, next_state, next_prompt):
        nonlocal processing_msg
        if text.lower() != 'skip':
            processing_msg = await message.reply("🔗 Shortening link...", quote=True)
            shortened = await shorten_link(uid, text)
            convo["links"][quality] = shortened
        convo["state"] = next_state
        prompt = f"✅ Link {'shortened and added' if text.lower() != 'skip' else 'skipped'}. Now, {next_prompt}"
        if processing_msg: await processing_msg.edit_text(prompt)
        else: await message.reply_text(prompt)

    if state == "wait_movie_lang":
        convo["language"] = text; convo["state"] = "wait_480p"
        await message.reply_text("✅ Language set. Now send the **480p** link or type `skip`.")
    elif state == "wait_480p":
        await process_link("480p", "wait_720p", "send the **720p** link or type `skip`.")
    elif state == "wait_720p":
        await process_link("720p", "wait_1080p", "send the **1080p** link or type `skip`.")
    elif state == "wait_1080p":
        if text.lower() != 'skip':
            processing_msg = await message.reply("🔗 Shortening link...", quote=True)
            shortened = await shorten_link(uid, text)
            convo["links"]["1080p"] = shortened
        msg = await (processing_msg.edit_text if processing_msg else message.reply)("✅ Data collection complete! Generating preview...")
        await generate_final_post_preview(client, uid, message.chat.id, msg)

    elif state == "wait_tv_lang":
        convo["language"] = text; convo["state"] = "wait_season_number"
        await message.reply_text("✅ Language set. Now enter the season number (e.g., 1, 2).")
    elif state == "wait_season_number":
        if text.lower() == 'done':
            if not convo.get('seasons'): return await message.reply_text("⚠️ You haven't added any season links.")
            msg = await message.reply_text("✅ All season data collected! Generating preview...", quote=True)
            convo['links'] = convo['seasons']
            await generate_final_post_preview(client, uid, message.chat.id, msg)
            return
        if not text.isdigit() or int(text) <= 0: return await message.reply_text("❌ Invalid number. Please enter a valid season number.")
        convo['current_season'] = text; convo['state'] = 'wait_season_link'
        await message.reply_text(f"👍 OK. Now send the download link for **Season {text}**.")
    elif state == "wait_season_link":
        season_num = convo.get('current_season')
        processing_msg = await message.reply("🔗 Shortening link...", quote=True)
        shortened = await shorten_link(uid, text)
        convo.setdefault('seasons', {})[season_num] = shortened
        convo['state'] = 'wait_season_number'
        await processing_msg.edit_text(f"✅ Link for Season {season_num} added.\n\n**👉 Enter the next season number, or type `done` to finish.**")

@bot.on_callback_query(filters.regex("^select_"))
async def selection_cb(client, cb: CallbackQuery):
    await cb.answer("Fetching details...", show_alert=False)
    try: _, flow, media_type, mid = cb.data.split("_", 3)
    except Exception as e:
        logger.error(f"Callback Error on split: {e}"); return await cb.message.edit_text("Invalid callback data.")
        
    details = get_tmdb_details(media_type, int(mid))
    if not details: return await cb.message.edit_text("❌ Sorry, couldn't fetch details from TMDB.")
    
    uid = cb.from_user.id
    if uid not in user_conversations:
        user_conversations[uid] = {}
        
    user_conversations[uid].update({"flow": flow, "details": details, "links": {}, "state": ""})
    
    if media_type == "tv":
        user_conversations[uid]["state"] = "wait_tv_lang"
        await cb.message.edit_text("**Web Series Post:** Enter the language for the series (e.g., Bengali, English).")
    elif media_type == "movie":
        user_conversations[uid]["state"] = "wait_movie_lang"
        await cb.message.edit_text("**Movie Post:** Enter the language for the movie.")

@bot.on_callback_query(filters.regex("^postto_"))
async def post_to_channel_cb(client, cb: CallbackQuery):
    uid = cb.from_user.id
    channel_id = cb.data.split("_")[1]

    convo = user_conversations.get(uid)
    if not convo or 'final_post' not in convo:
        await cb.answer("❌ Session expired!", show_alert=True)
        return await cb.message.edit_text("Sorry, the data for this post is no longer available. Please start over.")

    await cb.answer("⏳ Posting to channel...", show_alert=False)
    
    final_post = convo['final_post']
    caption = final_post['caption']
    poster = final_post['poster']

    try:
        chat = await client.get_chat(int(channel_id))
        channel_name = chat.title

        if poster:
            poster.seek(0)
            await client.send_photo(chat_id=int(channel_id), photo=poster, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await client.send_message(chat_id=int(channel_id), text=caption, parse_mode=enums.ParseMode.MARKDOWN)
        
        await cb.message.edit_text(f"✅ **Successfully posted to channel '{channel_name}'!**")
    except Exception as e:
        logger.error(f"Failed to post to channel {channel_id} for user {uid}. Error: {e}")
        await cb.message.edit_text(f"❌ **Failed to post to channel `{channel_id}`.**\n\n**Error:** `{e}`")
    finally:
        if uid in user_conversations:
            del user_conversations[uid]

# ---- 5. START THE BOT ----
if __name__ == "__main__":
    logger.info("🚀 Bot is starting with MongoDB connection...")
    bot.run()
    logger.info("👋 Bot has stopped.")
