# -*- coding: utf-8 -*-

# ---- Core Python Imports ----
import os
import io
import re
import requests
import asyncio
from threading import Thread
import logging

# --- Third-party Library Imports ---
from PIL import Image, ImageDraw, ImageFont
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.errors import UserNotParticipant, FloodWait
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
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # <--- MUST SET THIS IN .ENV

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
        except Exception as e:
            logger.error(f"Could not download cascade file. Error: {e}")
            return None
    return cascade_file

# --- DATABASE & PREMIUM HELPERS ---

async def add_user_to_db(user):
    # Default is_premium to False unless already set
    await users_collection.update_one(
        {'_id': user.id},
        {
            '$set': {'first_name': user.first_name},
            '$setOnInsert': {'is_premium': False} 
        },
        upsert=True
    )

async def is_user_premium(user_id: int) -> bool:
    if user_id == OWNER_ID: return True # Owner is always premium
    user_data = await users_collection.find_one({'_id': user_id})
    return user_data.get('is_premium', False) if user_data else False

# --- DECORATORS ---

def force_subscribe(func):
    async def wrapper(client, message):
        if FORCE_SUB_CHANNEL:
            try:
                chat_id = int(FORCE_SUB_CHANNEL) if FORCE_SUB_CHANNEL.startswith("-100") else FORCE_SUB_CHANNEL
                await client.get_chat_member(chat_id, message.from_user.id)
            except UserNotParticipant:
                join_link = INVITE_LINK or f"https://t.me/{FORCE_SUB_CHANNEL.replace('@', '')}"
                return await message.reply_text(
                    "❗ **You must join our channel to use this bot.**", 
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👉 Join Channel", url=join_link)]])
                )
        await func(client, message)
    return wrapper

def check_premium(func):
    """Decorator to restrict commands to Premium Users only"""
    async def wrapper(client, message):
        user_id = message.from_user.id
        if await is_user_premium(user_id):
            await func(client, message)
        else:
            await message.reply_text(
                "⛔ **Access Denied!**\n\n"
                "This is a **Premium Feature**. You need to buy a subscription to use this bot.\n\n"
                "👉 Contact Admin to buy Premium.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👑 Contact Admin", user_id=OWNER_ID)]
                ])
            )
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
            return data["shortenedUrl"]
        else:
            return long_url
    except requests.exceptions.RequestException:
        return long_url

def format_runtime(minutes: int):
    if not minutes or not isinstance(minutes, int): return "N/A"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if hours > 0 else f"{mins}m"

# ---- 3. TMDB API & CONTENT GENERATION ----

def search_tmdb_by_imdb(imdb_id: str):
    url = f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={TMDB_API_KEY}&external_source=imdb_id"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("movie_results", []) + data.get("tv_results", [])
    except Exception:
        return []

def search_tmdb(query: str):
    year, name = None, query.strip()
    match = re.search(r'(.+?)\s*\(?(\d{4})\)?$', query)
    if match: name, year = match.group(1).strip(), match.group(2)
    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={name}" + (f"&year={year}" if year else "")
    try:
        r = requests.get(url, timeout=10); r.raise_for_status()
        return [res for res in r.json().get("results", []) if res.get("media_type") in ["movie", "tv"]][:5]
    except Exception:
        return []

def get_tmdb_details(media_type: str, media_id: int):
    url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}&append_to_response=credits"
    try:
        r = requests.get(url, timeout=10); r.raise_for_status(); return r.json()
    except Exception:
        return None

def watermark_poster(poster_input, watermark_text: str, badge_text: str = None):
    # poster_input can be a String (URL) or BytesIO (File)
    if not poster_input: return None, "Poster not found."
    try:
        if isinstance(poster_input, str):
            img_data = requests.get(poster_input, timeout=20).content
            original_img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        else:
            original_img = Image.open(poster_input).convert("RGBA")
        
        img = Image.new("RGBA", original_img.size)
        img.paste(original_img)
        draw = ImageDraw.Draw(img)

        # ---- Badge Text Logic ----
        if badge_text:
            badge_font_size = int(img.width / 9)
            try:
                badge_font = ImageFont.truetype("HindSiliguri-Bold.ttf", badge_font_size)
            except IOError:
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
                        y_pos = img.height * 0.25
                except Exception: pass

            y = y_pos
            padding = int(badge_font_size * 0.1)
            rect_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            rect_draw = ImageDraw.Draw(rect_layer)
            rect_draw.rectangle((x - padding, y - padding, x + text_width + padding, y + text_height + padding), fill=(0, 0, 0, 140))
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
    except Exception as e:
        return None, f"Image processing error. Error: {e}"

async def generate_channel_caption(data: dict, language: str, links: dict, user_data: dict):
    # Determine Genre
    if isinstance(data.get("genres"), list) and len(data["genres"]) > 0:
        genre_str = ", ".join([g["name"] for g in data.get("genres", [])[:3]]) if isinstance(data["genres"][0], dict) else str(data.get("genres"))
    else:
        genre_str = str(data.get("genres", "N/A"))

    # Determine Year
    if data.get('media_type') == 'tv':
        date = data.get("first_air_date") or "----"
    else:
        date = data.get("release_date") or "----"

    info = {
        "title": data.get("title") or data.get("name") or "N/A",
        "year": date[:4],
        "genres": genre_str,
        "rating": f"{data.get('vote_average', 0):.1f}",
        "language": language,
        "runtime": format_runtime(data.get("runtime", 0) if 'runtime' in data else (data.get("episode_run_time") or [0])[0]),
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
    
    if data.get('media_type') == 'tv':
        if links:
            try: sorted_seasons = sorted(links.keys(), key=lambda x: int(x))
            except: sorted_seasons = links.keys()

            season_lines = []
            for season_num in sorted_seasons:
                season_data = links[season_num]
                if isinstance(season_data, dict):
                    parts = []
                    if season_data.get('480p'): parts.append(f"**[480p]({season_data['480p']})**")
                    if season_data.get('720p'): parts.append(f"**[720p]({season_data['720p']})**")
                    if season_data.get('1080p'): parts.append(f"**[1080p]({season_data['1080p']})**")
                    if parts:
                        link_line = " | ".join(parts)
                        season_lines.append(f"📂 **Season {season_num}:** {link_line}")
                else:
                    season_lines.append(f"✅ **[Download Season {season_num}]({season_data})**")
            download_links = "\n".join(season_lines)
    else:
        movie_links = []
        if links.get('480p'): movie_links.append(f"**[Download 480p]({links['480p']})**")
        if links.get('720p'): movie_links.append(f"**[Download 720p]({links['720p']})**")
        if links.get('1080p'): movie_links.append(f"**[Download 1080p]({links['1080p']})**")
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

# ---- 4. BOT HANDLERS (UPDATED START & PREMIUM LOGIC) ----

@bot.on_message(filters.command("start") & filters.private)
@force_subscribe
async def start_cmd(client, message: Message):
    user = message.from_user
    uid = user.id
    await add_user_to_db(user)
    
    # Clean up previous states
    if uid in user_conversations: del user_conversations[uid]

    is_premium = await is_user_premium(uid)
    is_owner = (uid == OWNER_ID)
    
    status_text = "💎 **Premium User**" if is_premium else "👤 **Free User**"
    
    # --- ADMIN / OWNER MENU ---
    if is_owner:
        welcome_text = (
            f"👑 **Welcome Boss, {user.first_name}!**\n\n"
            "**Admin Control Panel:**\n"
            "Use the buttons below to manage your bot and users."
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
             InlineKeyboardButton("📊 User Stats", callback_data="admin_stats")],
            [InlineKeyboardButton("➕ Add Premium", callback_data="admin_add_premium"),
             InlineKeyboardButton("➖ Remove Premium", callback_data="admin_rem_premium")],
            [InlineKeyboardButton("📝 Help Guide", callback_data="help_guide")]
        ])
    
    # --- USER MENU ---
    else:
        welcome_text = (
            f"👋 **Hello {user.first_name}!**\n\n"
            "I am your **Ultimate Movie & Series Post Generator**.\n"
            f"Your Status: {status_text}\n\n"
            "⚠️ **Note:** Only Premium users can generate posts."
        )
        
        user_buttons = [
            [InlineKeyboardButton("👤 My Account", callback_data="my_account"),
             InlineKeyboardButton("❓ Help", callback_data="help_guide")]
        ]
        
        # If user is NOT premium, show Buy button
        if not is_premium:
            user_buttons.insert(0, [InlineKeyboardButton("💎 Buy Premium Access", user_id=OWNER_ID)])
            
        buttons = InlineKeyboardMarkup(user_buttons)

    await message.reply_text(welcome_text, reply_markup=buttons)

# --- CALLBACK QUERY HANDLER FOR MENUS ---
@bot.on_callback_query(filters.regex(r"^(admin_|my_account|help_guide|back_home)"))
async def menu_callbacks(client, cb: CallbackQuery):
    data = cb.data
    uid = cb.from_user.id
    
    if data == "back_home":
        await start_cmd(client, cb.message)
        return

    if data == "my_account":
        status = "Premium 💎" if await is_user_premium(uid) else "Free 👤"
        await cb.answer(f"User: {cb.from_user.first_name}\nID: {uid}\nStatus: {status}", show_alert=True)
    
    elif data == "help_guide":
        text = (
            "**📚 Bot Command Guide:**\n\n"
            "**For Premium Users:**\n"
            "🔹 `/post <Movie Name>` - Create a post (TMDB).\n"
            "🔹 `/post <IMDb ID>` - Create post by IMDb ID.\n"
            "🔹 `/post <Link>` - Create post by TMDB Link.\n"
            "🔹 `/badge <Text>` - Add badge to poster.\n"
            "🔹 `/settings` - Manage watermark & shortener.\n"
            "🔹 `/addchannel <ID>` - Add channel (-100...).\n\n"
            "**For Admins:**\n"
            "Use the buttons in `/start` menu."
        )
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_home")]]))

    # --- ADMIN ACTIONS ---
    elif data.startswith("admin_"):
        if uid != OWNER_ID:
            return await cb.answer("❌ You are not the Admin!", show_alert=True)

        if data == "admin_stats":
            total = await users_collection.count_documents({})
            prem = await users_collection.count_documents({'is_premium': True})
            await cb.answer(f"📊 Total Users: {total}\n💎 Premium Users: {prem}", show_alert=True)
        
        elif data == "admin_broadcast":
            await cb.message.edit_text("📢 **Broadcast Mode**\n\nPlease send the message you want to broadcast to all users.\n\nType `/cancel` to stop.")
            user_conversations[uid] = {"state": "admin_broadcast_wait", "is_manual": False}
        
        elif data == "admin_add_premium":
            await cb.message.edit_text("➕ **Add Premium User**\n\nSend the **User ID** to grant Premium access.\n\nType `/cancel` to stop.")
            user_conversations[uid] = {"state": "admin_add_prem_wait", "is_manual": False}
        
        elif data == "admin_rem_premium":
            await cb.message.edit_text("➖ **Remove Premium User**\n\nSend the **User ID** to revoke Premium access.\n\nType `/cancel` to stop.")
            user_conversations[uid] = {"state": "admin_rem_prem_wait", "is_manual": False}

# ---- PREMIUM LOCKED COMMANDS ----

@bot.on_message(filters.command("badge") & filters.private)
@force_subscribe
@check_premium
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
@check_premium
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
@check_premium
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
    
    poster_input = None
    if convo['details'].get('poster_bytes'):
        poster_input = convo['details']['poster_bytes']
        poster_input.seek(0)
    elif convo['details'].get('poster_path'):
        poster_input = f"https://image.tmdb.org/t/p/w500{convo['details']['poster_path']}"
    
    await msg.edit_text("🖼️ Creating smart poster...")
    poster, error = watermark_poster(poster_input, watermark, badge_text=badge)
    
    await msg.delete()
    if error: await client.send_message(cid, f"⚠️ **Error creating poster:** `{error}`")

    poster_buffer = None
    if poster:
        poster_buffer = io.BytesIO(poster.read())
        poster_buffer.name = "final_poster.png"

    user_conversations[uid]['final_post'] = {'caption': caption, 'poster': poster_buffer}

    saved_channels = user_data.get('channel_ids', [])
    if saved_channels:
        buttons = []
        for channel_id in saved_channels:
            try:
                chat = await client.get_chat(int(channel_id))
                channel_name = chat.title
                buttons.append([InlineKeyboardButton(f"📢 Post to {channel_name}", callback_data=f"postto_{channel_id}")])
            except Exception:
                buttons.append([InlineKeyboardButton(f"📢 Post to {channel_id}", callback_data=f"postto_{channel_id}")])
        
        if poster_buffer:
            poster_buffer.seek(0)
            preview_msg = await client.send_photo(cid, photo=poster_buffer, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
        else:
            preview_msg = await client.send_message(cid, caption, parse_mode=enums.ParseMode.MARKDOWN)

        if buttons:
            await client.send_message(cid, "**👆 This is a preview. Choose a channel to post to:**", reply_to_message_id=preview_msg.id, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        if poster_buffer:
            poster_buffer.seek(0)
            await client.send_photo(cid, photo=poster_buffer, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await client.send_message(cid, caption, parse_mode=enums.ParseMode.MARKDOWN)
        await client.send_message(cid, "✅ Preview generated. You have no channels saved. Use `/addchannel` to add one.")

@bot.on_message(filters.command("post") & filters.private)
@force_subscribe
@check_premium
async def search_commands(client, message: Message):
    if len(message.command) == 1:
        return await message.reply_text("**Usage:**\n`/post Movie Name`\nOR\n`/post TMDB Link`\nOR\n`/post IMDb ID`")
    
    query = " ".join(message.command[1:]).strip()
    processing_msg = await message.reply_text(f"🔍 Searching for `{query}`...")

    results = []
    tmdb_link_match = re.search(r'(?:themoviedb\.org|tmdb\.org)/(movie|tv)/(\d+)', query)
    imdb_match = re.search(r'(tt\d{6,})', query)
    
    try:
        if tmdb_link_match:
            media_type = tmdb_link_match.group(1) # movie or tv
            tmdb_id = tmdb_link_match.group(2)    # ID
            await processing_msg.edit_text(f"🔗 TMDB Link detected (ID: {tmdb_id}). Fetching...")
            details = get_tmdb_details(media_type, int(tmdb_id))
            if details:
                details['media_type'] = media_type 
                results = [details]
        
        elif imdb_match:
            imdb_id = imdb_match.group(1)
            await processing_msg.edit_text(f"🔗 IMDb ID `{imdb_id}` detected. Fetching...")
            results = search_tmdb_by_imdb(imdb_id)
        else:
            results = search_tmdb(query)

    except Exception as e:
        logger.error(f"Search processing error: {e}")
        return await processing_msg.edit_text(f"❌ Error processing link: {e}")

    buttons = []
    if results:
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
            
            buttons.append([InlineKeyboardButton(f"{media_icon} {title} ({year})", callback_data=f"select_post_{m_type}_{r['id']}")])
    
    buttons.append([InlineKeyboardButton("📝 Create Manually (Not in TMDB)", callback_data="manual_start")])
    await processing_msg.edit_text(f"👇 **Results for:** `{query}`", reply_markup=InlineKeyboardMarkup(buttons))

# Handler for Manual Flow & Select
@bot.on_callback_query(filters.regex("^manual_"))
async def manual_handler(client, cb: CallbackQuery):
    data = cb.data
    uid = cb.from_user.id
    
    if data == "manual_start":
        await cb.message.edit_text("Is this a Movie or a Web Series?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Movie", callback_data="manual_type_movie"),
             InlineKeyboardButton("📺 Web Series", callback_data="manual_type_tv")]
        ]))
    
    elif data.startswith("manual_type_"):
        m_type = data.split("_")[2] # movie or tv
        user_conversations[uid] = {
            "details": {"media_type": m_type},
            "links": {},
            "state": "wait_manual_title",
            "is_manual": True
        }
        await cb.message.edit_text(f"📝 **Manual {m_type.capitalize()} Mode**\n\nPlease send the **Title** of the content:")

@bot.on_callback_query(filters.regex("^select_"))
async def selection_cb(client, cb: CallbackQuery):
    await cb.answer("Fetching details...", show_alert=False)
    try: _, flow, media_type, mid = cb.data.split("_", 3)
    except: return await cb.message.edit_text("Invalid callback data.")
        
    details = get_tmdb_details(media_type, int(mid))
    if not details: return await cb.message.edit_text("❌ Sorry, couldn't fetch details from TMDB.")
    
    if 'media_type' not in details: details['media_type'] = media_type
    uid = cb.from_user.id
    user_conversations[uid] = {"details": details, "links": {}, "state": ""}
    
    if media_type == "tv":
        user_conversations[uid]["state"] = "wait_tv_lang"
        await cb.message.edit_text("**Web Series Post:** Enter the language for the series (e.g., Bengali, English).")
    elif media_type == "movie":
        user_conversations[uid]["state"] = "wait_movie_lang"
        await cb.message.edit_text("**Movie Post:** Enter the language for the movie.")

# ---- 5. UNIFIED CONVERSATION HANDLER (Admin Inputs + Post Inputs) ----
@bot.on_message(filters.private & (filters.text | filters.photo))
@force_subscribe
async def conversation_handler(client, message: Message):
    uid = message.from_user.id
    convo = user_conversations.get(uid)
    if not convo or "state" not in convo: return
    
    state = convo["state"]
    text = message.text.strip() if message.text else None
    
    # --- ADMIN STATES ---
    if state == "admin_broadcast_wait":
        if uid != OWNER_ID: return
        msg = await message.reply_text("📣 Sending Broadcast... Please wait.")
        users = users_collection.find({})
        sent, failed = 0, 0
        async for user in users:
            try:
                await message.copy(chat_id=user['_id'])
                sent += 1
                await asyncio.sleep(0.1)
            except: failed += 1
        await msg.edit_text(f"✅ **Broadcast Complete!**\n\nSent: {sent}\nFailed: {failed}")
        del user_conversations[uid]
        return

    elif state == "admin_add_prem_wait":
        if uid != OWNER_ID: return
        try:
            target_id = int(text)
            await users_collection.update_one({'_id': target_id}, {'$set': {'is_premium': True}}, upsert=True)
            await message.reply_text(f"✅ User `{target_id}` is now **Premium**.")
        except: await message.reply_text("❌ Invalid ID.")
        del user_conversations[uid]
        return

    elif state == "admin_rem_prem_wait":
        if uid != OWNER_ID: return
        try:
            target_id = int(text)
            await users_collection.update_one({'_id': target_id}, {'$set': {'is_premium': False}})
            await message.reply_text(f"✅ User `{target_id}` is now **Free**.")
        except: await message.reply_text("❌ Invalid ID.")
        del user_conversations[uid]
        return

    # --- REGULAR USER POST STATES ---
    # (Premium Check applied via logic flow, initial entry was guarded)
    
    if state == "wait_manual_title":
        convo["details"]["title"] = text
        convo["details"]["name"] = text
        convo["state"] = "wait_manual_year"
        await message.reply_text("✅ Title set. Now send the **Year** (e.g. 2025):")

    elif state == "wait_manual_year":
        if convo["details"]["media_type"] == "tv":
            convo["details"]["first_air_date"] = f"{text}-01-01"
            convo["details"]["release_date"] = None
        else:
            convo["details"]["release_date"] = f"{text}-01-01"
            convo["details"]["first_air_date"] = None
        convo["state"] = "wait_manual_rating"
        await message.reply_text("✅ Year set. Now send the **Rating** (e.g. 7.5):")

    elif state == "wait_manual_rating":
        try: rating = float(text)
        except: rating = 0.0
        convo["details"]["vote_average"] = rating
        convo["state"] = "wait_manual_genres"
        await message.reply_text("✅ Rating set. Now send the **Genres** (e.g. Action, Drama):")

    elif state == "wait_manual_genres":
        convo["details"]["genres"] = text
        convo["state"] = "wait_manual_poster"
        await message.reply_text("✅ Genres set. Now **Send a Photo** to use as the Poster:")

    elif state == "wait_manual_poster":
        if not message.photo: return await message.reply_text("⚠️ Please send an image (Photo).")
        msg = await message.reply_text("📥 Downloading poster...")
        photo = await client.download_media(message, in_memory=True)
        convo["details"]["poster_bytes"] = photo
        
        if convo["details"]["media_type"] == "tv":
            convo["state"] = "wait_tv_lang"
            await msg.edit_text("✅ Poster saved.\n\n**Web Series:** Enter the language (e.g. English, Hindi):")
        else:
            convo["state"] = "wait_movie_lang"
            await msg.edit_text("✅ Poster saved.\n\n**Movie:** Enter the language:")

    elif state == "wait_movie_lang":
        convo["language"] = text; convo["state"] = "wait_480p"
        await message.reply_text("✅ Language set. Now send the **480p** link or type `skip`.")
    
    elif state == "wait_480p":
        if text.lower() != 'skip':
            convo["links"]["480p"] = await shorten_link(uid, text)
        convo["state"] = "wait_720p"
        await message.reply_text("✅ Saved. Now send the **720p** link or type `skip`.")

    elif state == "wait_720p":
        if text.lower() != 'skip':
            convo["links"]["720p"] = await shorten_link(uid, text)
        convo["state"] = "wait_1080p"
        await message.reply_text("✅ Saved. Now send the **1080p** link or type `skip`.")

    elif state == "wait_1080p":
        if text.lower() != 'skip':
            convo["links"]["1080p"] = await shorten_link(uid, text)
        msg = await message.reply_text("✅ All data collected! Generating preview...")
        await generate_final_post_preview(client, uid, message.chat.id, msg)

    elif state == "wait_tv_lang":
        convo["language"] = text; convo["state"] = "wait_season_number"
        await message.reply_text("✅ Language set. Now enter the **Season Number** (e.g., 1).")

    elif state == "wait_season_number":
        if text.lower() == 'done':
            if not convo.get('links'): return await message.reply_text("⚠️ No season links added.")
            msg = await message.reply_text("✅ Generating preview...", quote=True)
            await generate_final_post_preview(client, uid, message.chat.id, msg)
            return
        
        if not text.isdigit(): return await message.reply_text("❌ Invalid number.")
        convo['current_season'] = text
        if 'links' not in convo: convo['links'] = {}
        if text not in convo['links']: convo['links'][text] = {}
        convo['state'] = 'wait_season_480'
        await message.reply_text(f"👍 **Season {text}** selected.\n\nSend **480p** link (or type `skip`).")

    elif state == "wait_season_480":
        s_num = convo['current_season']
        if text.lower() != 'skip': convo['links'][s_num]['480p'] = await shorten_link(uid, text)
        convo['state'] = 'wait_season_720'
        await message.reply_text(f"Send **720p** link (or `skip`).")

    elif state == "wait_season_720":
        s_num = convo['current_season']
        if text.lower() != 'skip': convo['links'][s_num]['720p'] = await shorten_link(uid, text)
        convo['state'] = 'wait_season_1080'
        await message.reply_text(f"Send **1080p** link (or `skip`).")

    elif state == "wait_season_1080":
        s_num = convo['current_season']
        if text.lower() != 'skip': convo['links'][s_num]['1080p'] = await shorten_link(uid, text)
        convo['state'] = 'wait_season_number'
        await message.reply_text(f"✅ Season {s_num} saved.\n\n**👉 Enter next Season Number, or type `done` to finish.**")

@bot.on_callback_query(filters.regex("^postto_"))
async def post_to_channel_cb(client, cb: CallbackQuery):
    uid = cb.from_user.id
    channel_id = cb.data.split("_")[1]

    convo = user_conversations.get(uid)
    if not convo or 'final_post' not in convo:
        await cb.answer("❌ Session expired!", show_alert=True)
        return

    await cb.answer("⏳ Posting...", show_alert=False)
    final_post = convo['final_post']
    
    try:
        if final_post['poster']:
            final_post['poster'].seek(0)
            await client.send_photo(int(channel_id), final_post['poster'], caption=final_post['caption'], parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await client.send_message(int(channel_id), final_post['caption'], parse_mode=enums.ParseMode.MARKDOWN)
        await cb.message.edit_text(f"✅ **Posted to channel successfully!**")
    except Exception as e:
        await cb.message.edit_text(f"❌ **Failed to post.**\nError: `{e}`")
    finally:
        if uid in user_conversations: del user_conversations[uid]

# ---- 6. START THE BOT ----
if __name__ == "__main__":
    logger.info("🚀 Bot is starting with Premium System...")
    bot.run()
