# -*- coding: utf-8 -*-

# ---- Core Python Imports ----
import os
import io
import re
import requests
import asyncio
from threading import Thread
import logging
import time

# --- Third-party Library Imports ---
from PIL import Image, ImageDraw, ImageFont
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.errors import UserNotParticipant, FloodWait
from flask import Flask
from dotenv import load_dotenv
import motor.motor_asyncio

# ---- 1. CONFIGURATION AND SETUP ----
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL")
INVITE_LINK = os.getenv("INVITE_LINK")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
LOG_CHANNEL = os.getenv("LOG_CHANNEL") # .env ফাইলে এটি যুক্ত করুন

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
settings_collection = db.settings

# ---- Global Variables & Bot Initialization ----
user_conversations = {}
bot = Client("UltimateMovieBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---- Flask App (for Keep-Alive) ----
app = Flask(__name__)
@app.route('/')
def home(): return "✅ Bot is Running!"
Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080))), daemon=True).start()

# ---- 2. DECORATORS AND HELPER FUNCTIONS ----

def download_font():
    font_file = "HindSiliguri-Bold.ttf"
    if not os.path.exists(font_file):
        logger.info(f"Downloading {font_file} for badge text...")
        url = "https://github.com/google/fonts/raw/main/ofl/hindsiliguri/HindSiliguri-Bold.ttf"
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            with open(font_file, 'wb') as f:
                f.write(r.content)
        except Exception as e:
            logger.error(f"Could not download font file. Error: {e}")
            return None
    return font_file

# --- ADMIN & LOGGING HELPERS ---

async def log_event(text):
    """লগ চ্যানেলে ইভেন্ট সেন্ড করা"""
    if LOG_CHANNEL:
        try:
            await bot.send_message(int(LOG_CHANNEL), f"🔔 **LOG:**\n{text}")
        except Exception as e:
            logger.error(f"Log Error: {e}")

async def auto_delete_message(client, chat_id, message_id, delay=300):
    """নির্দিষ্ট সময় পর মেসেজ ডিলিট করা"""
    await asyncio.sleep(delay)
    try:
        await client.delete_messages(chat_id, message_id)
    except:
        pass

async def add_user_to_db(user):
    await users_collection.update_one(
        {'_id': user.id},
        {
            '$set': {'first_name': user.first_name, 'username': user.username},
            '$setOnInsert': {'is_premium': False, 'join_date': time.time()} 
        },
        upsert=True
    )

async def is_user_premium(user_id: int) -> bool:
    if user_id == OWNER_ID: return True
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
    async def wrapper(client, message):
        user_id = message.from_user.id
        if await is_user_premium(user_id):
            await func(client, message)
        else:
            await message.reply_text(
                "⛔ **Access Denied!**\n\n"
                "This is a **Premium Feature**. You need to buy a subscription.\n\n"
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
    
    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={name}&include_adult=true" + (f"&year={year}" if year else "")
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

# ---- 🎨 CINEMATIC POSTER GENERATOR ----

def create_gradient_overlay(width, height):
    """পোস্টারের নিচে কালো শ্যাডো তৈরি করা"""
    gradient = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(gradient)
    for i in range(height):
        if i > height * 0.6: # নিচের 40% অংশ
            alpha = int(255 * ((i - height * 0.6) / (height * 0.4)))
            draw.line([(0, i), (width, i)], fill=(0, 0, 0, alpha))
    return gradient

def watermark_poster(poster_input, watermark_text: str, badge_text: str = None, movie_title: str = "", rating: str = ""):
    if not poster_input: return None, "Poster not found."
    try:
        if isinstance(poster_input, str):
            img_data = requests.get(poster_input, timeout=20).content
            original_img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        else:
            original_img = Image.open(poster_input).convert("RGBA")
        
        # High Quality Resize
        base_width = 1200
        w_percent = (base_width / float(original_img.size[0]))
        h_size = int((float(original_img.size[1]) * float(w_percent)))
        img = original_img.resize((base_width, h_size), Image.Resampling.LANCZOS)
        
        draw = ImageDraw.Draw(img)
        font_path = download_font()
        
        # Gradient
        gradient = create_gradient_overlay(img.width, img.height)
        img = Image.alpha_composite(img, gradient)
        draw = ImageDraw.Draw(img)

        # Title & Rating
        if movie_title:
            try:
                title_font_size = 75 if len(movie_title) <= 20 else 60
                title_font = ImageFont.truetype(font_path, title_font_size) if font_path else ImageFont.load_default()
                meta_font = ImageFont.truetype(font_path, 45) if font_path else ImageFont.load_default()
            except:
                title_font = ImageFont.load_default(); meta_font = ImageFont.load_default()

            text_x, text_y = 50, img.height - 180
            draw.text((text_x+3, text_y+3), movie_title, font=title_font, fill="black") # Shadow
            draw.text((text_x, text_y), movie_title, font=title_font, fill="white")
            
            sub_text = f"⭐ {rating}/10  |  {watermark_text or 'MovieBot'}"
            draw.text((text_x, text_y + 85), sub_text, font=meta_font, fill="#FFD700")

        # Top Right Badge
        if badge_text:
            try:
                badge_font = ImageFont.truetype(font_path, 50) if font_path else ImageFont.load_default()
            except: badge_font = ImageFont.load_default()
            
            bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
            bw, bh = bbox[2] - bbox[0] + 50, bbox[3] - bbox[1] + 30
            bx, by = img.width - bw - 40, 40
            
            draw.rounded_rectangle([(bx, by), (bx + bw, by + bh)], radius=20, fill=(220, 20, 60, 240))
            text_x = bx + (bw - (bbox[2] - bbox[0])) / 2
            text_y = by + (bh - (bbox[3] - bbox[1])) / 2 - 8
            draw.text((text_x, text_y), badge_text, font=badge_font, fill="white")

        buffer = io.BytesIO()
        buffer.name = "poster.png"
        img.convert("RGB").save(buffer, "PNG")
        buffer.seek(0)
        return buffer, None
    except Exception as e:
        return None, f"Image processing error: {e}"

# ---- 📝 DYNAMIC CAPTION GENERATOR ----

async def generate_channel_caption(data: dict, language: str, links: dict, user_data: dict):
    # Info
    overview = data.get("overview", "")
    if len(overview) > 200: overview = overview[:200] + "..."
    if not overview: overview = "No synopsis available."

    genre_str = " | ".join([g["name"] for g in data.get("genres", [])[:3]]) if isinstance(data.get("genres"), list) else "N/A"
    year = (data.get("release_date") or data.get("first_air_date") or "----")[:4]
    
    caption = f"""
🎬 **{data.get('title') or data.get('name')}** ({year})
➖➖➖➖➖➖➖➖➖➖➖
⭐ **Rating:** {data.get('vote_average', 0):.1f}/10
🎭 **Genre:** {genre_str}
🔊 **Language:** {language}
⏰ **Runtime:** {format_runtime(data.get("runtime", 0) if 'runtime' in data else (data.get("episode_run_time") or [0])[0])}
➖➖➖➖➖➖➖➖➖➖➖
📝 **Storyline:**
_{overview}_

👇 **DOWNLOAD LINKS** 👇
"""
    
    # Style Logic
    style = user_data.get('link_style', '1') if user_data else '1'
    def format_link(quality, url, s_type):
        if s_type == '2': return f"📥 **{quality}** [{url}]"
        elif s_type == '3': return f"🔘 **[{quality} Quality]({url})**"
        elif s_type == '4': return f"⚡ **[{quality}]({url})**"
        else: return f"🔹 **[Download {quality}]({url})**"

    if data.get('media_type') == 'tv':
        if links:
            try: sorted_seasons = sorted(links.keys(), key=lambda x: int(x))
            except: sorted_seasons = links.keys()

            for season_num in sorted_seasons:
                season_data = links[season_num]
                caption += f"\n📂 **Season {season_num}**\n"
                if isinstance(season_data, dict):
                    parts = []
                    join_char = " | " if style in ['3', '4'] else "\n"
                    if season_data.get('480p'): parts.append(format_link("480p", season_data['480p'], style))
                    if season_data.get('720p'): parts.append(format_link("720p", season_data['720p'], style))
                    if season_data.get('1080p'): parts.append(format_link("1080p", season_data['1080p'], style))
                    
                    if parts: caption += join_char.join(parts)
                    else: caption += "Links coming soon..."
                else:
                    caption += f"✅ [Download Season]({season_data})"
    else:
        movie_links = []
        if links.get('480p'): movie_links.append(format_link("480p", links['480p'], style))
        if links.get('720p'): movie_links.append(format_link("720p", links['720p'], style))
        if links.get('1080p'): movie_links.append(format_link("1080p", links['1080p'], style))
        caption += "\n".join(movie_links)

    static_footer = """
➖➖➖➖➖➖➖➖➖➖➖
Movie ReQuest Group 
👇👇👇
https://t.me/Terabox_search_group

Premium Backup Group link 👇👇👇
https://t.me/+GL_XAS4MsJg4ODM1"""

    if user_data and user_data.get('tutorial_link'):
        caption += f"\n\n🎥 **How To Download:** [Watch Tutorial]({user_data['tutorial_link']})"
    
    caption += static_footer
    return caption.strip()

# ---- 4. BOT HANDLERS ----

@bot.on_message(filters.command("start") & filters.private)
@force_subscribe
async def start_cmd(client, message: Message):
    user = message.from_user
    uid = user.id
    await add_user_to_db(user)
    
    if uid in user_conversations: del user_conversations[uid]
    is_premium = await is_user_premium(uid)
    is_owner = (uid == OWNER_ID)
    status_text = "💎 **Premium User**" if is_premium else "👤 **Free User**"
    
    if is_owner:
        welcome_text = f"👑 **Welcome Boss, {user.first_name}!**\n\nAdmin Control Panel:"
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
             InlineKeyboardButton("📊 User Stats", callback_data="admin_stats")],
            [InlineKeyboardButton("➕ Add Premium", callback_data="admin_add_premium"),
             InlineKeyboardButton("➖ Remove Premium", callback_data="admin_rem_premium")],
            [InlineKeyboardButton("📝 Help Guide", callback_data="help_guide")]
        ])
    else:
        welcome_text = f"👋 **Hello {user.first_name}!**\n\nYour Status: {status_text}\n\n⚠️ Only Premium users can generate posts."
        user_buttons = [[InlineKeyboardButton("👤 My Account", callback_data="my_account"),
             InlineKeyboardButton("❓ Help", callback_data="help_guide")]]
        if not is_premium:
            user_buttons.insert(0, [InlineKeyboardButton("💎 Buy Premium Access", user_id=OWNER_ID)])
        buttons = InlineKeyboardMarkup(user_buttons)

    await message.reply_text(welcome_text, reply_markup=buttons)

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
        text = "**📚 Bot Command Guide:**\n\nUse `/post Movie Name` to start.\nUse `/settings` to configure."
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_home")]]))
    elif data.startswith("admin_"):
        if uid != OWNER_ID: return await cb.answer("❌ You are not the Admin!", show_alert=True)
        if data == "admin_stats":
            total = await users_collection.count_documents({})
            prem = await users_collection.count_documents({'is_premium': True})
            await cb.answer(f"📊 Total Users: {total}\n💎 Premium Users: {prem}", show_alert=True)
        elif data == "admin_broadcast":
            await cb.message.edit_text("📢 **Broadcast Mode**\nSend message to broadcast.\n(Type `/pin` at start to pin message).")
            user_conversations[uid] = {"state": "admin_broadcast_wait", "is_manual": False}
        elif data == "admin_add_premium":
            await cb.message.edit_text("➕ **Add Premium**\nSend User ID.")
            user_conversations[uid] = {"state": "admin_add_prem_wait", "is_manual": False}
        elif data == "admin_rem_premium":
            await cb.message.edit_text("➖ **Remove Premium**\nSend User ID.")
            user_conversations[uid] = {"state": "admin_rem_prem_wait", "is_manual": False}

# --- SETTINGS & STYLE HANDLERS ---

@bot.on_message(filters.command("setstyle") & filters.private)
@force_subscribe
@check_premium
async def set_link_style(client, message: Message):
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("Style 1: [Download 720p]", callback_data="style_1")],
        [InlineKeyboardButton("Style 2: 📥 720p [Link]", callback_data="style_2")],
        [InlineKeyboardButton("Style 3: 🔘 720p Quality", callback_data="style_3")],
        [InlineKeyboardButton("Style 4: ⚡ 720p (Fast)", callback_data="style_4")]
    ])
    await message.reply_text("🎨 **Choose Link Style:**", reply_markup=buttons)

@bot.on_callback_query(filters.regex(r"^style_"))
async def save_link_style(client, cb: CallbackQuery):
    style_id = cb.data.split("_")[1]
    await users_collection.update_one({'_id': cb.from_user.id}, {'$set': {'link_style': style_id}}, upsert=True)
    await cb.message.edit_text(f"✅ **Link Style Set to:** Style {style_id}")

@bot.on_message(filters.command(["setwatermark", "cancel", "setapi", "setdomain", "settutorial", "settings", "badge"]) & filters.private)
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
            await message.reply_text(f"✅ Shortener API Key set.")
        else: await message.reply_text("Usage: `/setapi <KEY>`")

    elif command == "setdomain":
        if len(message.command) > 1:
            domain = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$set': {'shortener_url': domain}}, upsert=True)
            await message.reply_text(f"✅ Domain set: `{domain}`")
        else: await message.reply_text("Usage: `/setdomain example.com`")

    elif command == "settutorial":
        if len(message.command) > 1:
            link = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$set': {'tutorial_link': link}}, upsert=True)
            await message.reply_text(f"✅ Tutorial link set.")
        else:
            await users_collection.update_one({'_id': uid}, {'$unset': {'tutorial_link': ""}}); await message.reply_text("✅ Tutorial link removed.")

    elif command == "settings":
        user_data = await users_collection.find_one({'_id': uid})
        if not user_data: return await message.reply_text("No settings saved.")
        style = user_data.get('link_style', '1')
        await message.reply_text(f"**Settings:**\nWatermark: `{user_data.get('watermark_text', 'Not Set')}`\nStyle: `Style {style}`")

@bot.on_message(filters.command(["addchannel", "delchannel", "mychannels"]) & filters.private)
@force_subscribe
@check_premium
async def channel_management(client, message: Message):
    command = message.command[0].lower()
    uid = message.from_user.id
    
    if command == "addchannel":
        if len(message.command) > 1 and message.command[1].startswith("-100"):
            cid = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$addToSet': {'channel_ids': cid}}, upsert=True)
            await message.reply_text(f"✅ Channel `{cid}` added.")
        else: await message.reply_text("Usage: `/addchannel -100...`")
    elif command == "delchannel":
        if len(message.command) > 1 and message.command[1].startswith("-100"):
            cid = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$pull': {'channel_ids': cid}})
            await message.reply_text(f"✅ Channel `{cid}` removed.")
    elif command == "mychannels":
        user_data = await users_collection.find_one({'_id': uid})
        channels = user_data.get('channel_ids', [])
        await message.reply_text("📋 **Channels:**\n" + "\n".join([f"`{ch}`" for ch in channels]) if channels else "No channels set.")

# --- Badge Decision & Post Preview ---

async def ask_badge_decision(client, message, uid):
    buttons = [
        [InlineKeyboardButton("✅ Add Custom Badge", callback_data="ask_badge_text"),
         InlineKeyboardButton("🇧🇩 বাংলা ডাবিং", callback_data="set_badge_bangla")],
        [InlineKeyboardButton("⏭️ Skip Badge", callback_data="skip_badge")]
    ]
    convo = user_conversations.get(uid)
    if convo: convo["state"] = "wait_badge_decision"
    await message.reply_text(
        "🎨 **Poster Customization:**\n\nDo you want to add a Badge/Tag on the Top-Right of the poster?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def generate_final_post_preview(client, uid, cid, msg):
    convo = user_conversations.get(uid)
    if not convo: return
    
    user_data = await users_collection.find_one({'_id': uid})
    caption = await generate_channel_caption(convo["details"], convo["language"], convo["links"], user_data)
    watermark = user_data.get('watermark_text')
    badge = convo.get('temp_badge_text', None)
    
    m_title = convo["details"].get("title") or convo["details"].get("name")
    m_rating = f"{convo['details'].get('vote_average', 0):.1f}"

    poster_input = None
    if convo['details'].get('poster_bytes'):
        poster_input = convo['details']['poster_bytes']
        poster_input.seek(0)
    elif convo['details'].get('poster_path'):
        poster_input = f"https://image.tmdb.org/t/p/w780{convo['details']['poster_path']}"
    
    await msg.edit_text("🖼️ Creating cinematic poster...")
    
    poster, error = watermark_poster(poster_input, watermark, badge_text=badge, movie_title=m_title, rating=m_rating)
    
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
                buttons.append([InlineKeyboardButton(f"📢 {channel_name}", callback_data=f"postto_{channel_id}")])
            except:
                buttons.append([InlineKeyboardButton(f"📢 {channel_id}", callback_data=f"postto_{channel_id}")])
        
        if poster_buffer:
            poster_buffer.seek(0)
            preview_msg = await client.send_photo(cid, photo=poster_buffer, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
        else:
            preview_msg = await client.send_message(cid, caption, parse_mode=enums.ParseMode.MARKDOWN)

        if buttons:
            await client.send_message(cid, "**👆 Preview generated. Choose channel to post:**", reply_to_message_id=preview_msg.id, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        if poster_buffer:
            poster_buffer.seek(0)
            await client.send_photo(cid, photo=poster_buffer, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await client.send_message(cid, caption, parse_mode=enums.ParseMode.MARKDOWN)
        await client.send_message(cid, "✅ Preview generated. (No channels saved to post).")

@bot.on_message(filters.command("post") & filters.private)
@force_subscribe
@check_premium
async def search_commands(client, message: Message):
    if len(message.command) == 1:
        return await message.reply_text("**Usage:** `/post Movie Name`")
    
    query = " ".join(message.command[1:]).strip()
    processing_msg = await message.reply_text(f"🔍 Searching for `{query}`...")

    results = []
    tmdb_link_match = re.search(r'(?:themoviedb\.org|tmdb\.org)/(movie|tv)/(\d+)', query)
    imdb_match = re.search(r'(tt\d{6,})', query)
    
    try:
        if tmdb_link_match:
            media_type = tmdb_link_match.group(1)
            tmdb_id = tmdb_link_match.group(2)
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
        return await processing_msg.edit_text(f"❌ Error: {e}")

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
    
    buttons.append([InlineKeyboardButton("📝 Create Manually", callback_data="manual_start")])
    msg = await processing_msg.edit_text(f"👇 **Results for:** `{query}`", reply_markup=InlineKeyboardMarkup(buttons))
    
    # 🆕 Auto Delete Task
    asyncio.create_task(auto_delete_message(client, message.chat.id, msg.id))

@bot.on_callback_query(filters.regex("^manual_"))
async def manual_handler(client, cb: CallbackQuery):
    data = cb.data
    uid = cb.from_user.id
    if data == "manual_start":
        await cb.message.edit_text("Type?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Movie", callback_data="manual_type_movie"),
             InlineKeyboardButton("📺 Web Series", callback_data="manual_type_tv")]
        ]))
    elif data.startswith("manual_type_"):
        m_type = data.split("_")[2]
        user_conversations[uid] = {
            "details": {"media_type": m_type},
            "links": {},
            "state": "wait_manual_title",
            "is_manual": True
        }
        await cb.message.edit_text(f"📝 **Manual {m_type} Mode**\nSend Title:")

@bot.on_callback_query(filters.regex("^select_"))
async def selection_cb(client, cb: CallbackQuery):
    await cb.answer("Fetching...")
    try: _, flow, media_type, mid = cb.data.split("_", 3)
    except: return
    details = get_tmdb_details(media_type, int(mid))
    if not details: return await cb.message.edit_text("❌ Failed to fetch details.")
    if 'media_type' not in details: details['media_type'] = media_type
    uid = cb.from_user.id
    user_conversations[uid] = {"details": details, "links": {}, "state": ""}
    
    if media_type == "tv":
        user_conversations[uid]["state"] = "wait_tv_lang"
        await cb.message.edit_text("**Web Series:** Enter Language:")
    elif media_type == "movie":
        user_conversations[uid]["state"] = "wait_movie_lang"
        await cb.message.edit_text("**Movie:** Enter Language:")

@bot.on_callback_query(filters.regex(r"^(ask_badge_text|skip_badge|set_badge_bangla)"))
async def badge_callbacks(client, cb: CallbackQuery):
    data = cb.data
    uid = cb.from_user.id
    convo = user_conversations.get(uid)
    if not convo: return await cb.answer("Session expired.", show_alert=True)

    if data == "skip_badge":
        convo['temp_badge_text'] = None
        await cb.message.edit_text("✅ Badge skipped. Generating...")
        await generate_final_post_preview(client, uid, cb.message.chat.id, cb.message)
    
    elif data == "set_badge_bangla":
        convo['temp_badge_text'] = "বাংলা ডাবিং"
        await cb.message.edit_text("✅ Badge set to: **বাংলা ডাবিং**. Generating...")
        await generate_final_post_preview(client, uid, cb.message.chat.id, cb.message)
    
    elif data == "ask_badge_text":
        convo["state"] = "wait_badge_text"
        await cb.message.edit_text("✍️ **Send the text you want on the badge:**\n(e.g., HD Rip, 4K, Dual Audio)")

@bot.on_message(filters.private & (filters.text | filters.photo))
@force_subscribe
async def conversation_handler(client, message: Message):
    uid = message.from_user.id
    convo = user_conversations.get(uid)
    if not convo or "state" not in convo: return
    
    state = convo["state"]
    text = message.text.strip() if message.text else None
    
    # --- ADMIN STATES (UPDATED BROADCAST) ---
    if state == "admin_broadcast_wait":
        if uid != OWNER_ID: return
        
        do_pin = False
        if text.startswith("/pin"):
            do_pin = True
            text = text.replace("/pin", "").strip()

        if not text and not message.reply_to_message:
            return await message.reply_text("❌ Please send a message or reply to one.")

        status_msg = await message.reply_text("📣 **Broadcast Started...** 0%")
        users = users_collection.find({})
        total = await users_collection.count_documents({})
        done, blocked = 0, 0
        
        async for user in users:
            try:
                if message.reply_to_message:
                    msg = await message.reply_to_message.copy(chat_id=user['_id'])
                else:
                    msg = await client.send_message(chat_id=user['_id'], text=text)
                
                if do_pin and msg:
                    try: await msg.pin(disable_notification=False)
                    except: pass
                
                done += 1
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                blocked += 1
            
            if done % 20 == 0:
                await status_msg.edit_text(f"📣 **Broadcasting...**\n✅ Sent: {done}\n🚫 Blocked: {blocked}\n📊 Total: {total}")

        await status_msg.edit_text(f"✅ **Broadcast Complete!**\nSent: {done}\nBlocked: {blocked}")
        await log_event(f"📢 Broadcast finished. Sent: {done}, Blocked: {blocked}")
        del user_conversations[uid]
        return

    elif state == "admin_add_prem_wait":
        if uid != OWNER_ID: return
        try:
            target_id = int(text)
            await users_collection.update_one({'_id': target_id}, {'$set': {'is_premium': True}}, upsert=True)
            await message.reply_text(f"✅ User `{target_id}` is now Premium.")
            await log_event(f"💎 Premium added to {target_id}")
        except: await message.reply_text("❌ Invalid ID.")
        del user_conversations[uid]
        return

    elif state == "admin_rem_prem_wait":
        if uid != OWNER_ID: return
        try:
            target_id = int(text)
            await users_collection.update_one({'_id': target_id}, {'$set': {'is_premium': False}})
            await message.reply_text(f"✅ Removed Premium from `{target_id}`.")
        except: await message.reply_text("❌ Invalid ID.")
        del user_conversations[uid]
        return

    # --- POST GENERATION STATES ---
    if state == "wait_manual_title":
        convo["details"]["title"] = text
        convo["details"]["name"] = text
        convo["state"] = "wait_manual_year"
        await message.reply_text("✅ Title set. Send Year:")
    elif state == "wait_manual_year":
        convo["details"]["release_date"] = f"{text}-01-01" if convo["details"]["media_type"] == "movie" else None
        convo["details"]["first_air_date"] = f"{text}-01-01" if convo["details"]["media_type"] == "tv" else None
        convo["state"] = "wait_manual_rating"
        await message.reply_text("✅ Year set. Send Rating:")
    elif state == "wait_manual_rating":
        convo["details"]["vote_average"] = float(text) if text.replace('.','').isdigit() else 0.0
        convo["state"] = "wait_manual_genres"
        await message.reply_text("✅ Rating set. Send Genres:")
    elif state == "wait_manual_genres":
        convo["details"]["genres"] = text
        convo["state"] = "wait_manual_poster"
        await message.reply_text("✅ Genres set. Send Poster Photo:")
    elif state == "wait_manual_poster":
        if not message.photo: return await message.reply_text("⚠️ Send a photo.")
        photo = await client.download_media(message, in_memory=True)
        convo["details"]["poster_bytes"] = photo
        m_type = convo["details"]["media_type"]
        convo["state"] = "wait_movie_lang" if m_type == "movie" else "wait_tv_lang"
        await message.reply_text(f"✅ Poster saved. Enter Language for {m_type}:")

    elif state == "wait_movie_lang":
        convo["language"] = text; convo["state"] = "wait_480p"
        await message.reply_text("✅ Lang set. Send **480p** link (or `skip`):")
    elif state == "wait_480p":
        if text.lower() != 'skip': convo["links"]["480p"] = await shorten_link(uid, text)
        convo["state"] = "wait_720p"
        await message.reply_text("✅ Saved. Send **720p** link (or `skip`):")
    elif state == "wait_720p":
        if text.lower() != 'skip': convo["links"]["720p"] = await shorten_link(uid, text)
        convo["state"] = "wait_1080p"
        await message.reply_text("✅ Saved. Send **1080p** link (or `skip`):")
    elif state == "wait_1080p":
        if text.lower() != 'skip': convo["links"]["1080p"] = await shorten_link(uid, text)
        await ask_badge_decision(client, message, uid)

    elif state == "wait_tv_lang":
        convo["language"] = text; convo["state"] = "wait_season_number"
        await message.reply_text("✅ Lang set. Enter **Season Number** (e.g., 1):")
    elif state == "wait_season_number":
        if text.lower() == 'done':
            if not convo.get('links'): return await message.reply_text("⚠️ No seasons added.")
            await ask_badge_decision(client, message, uid)
            return
        
        if not text.isdigit(): return await message.reply_text("❌ Invalid number.")
        convo['current_season'] = text
        if 'links' not in convo: convo['links'] = {}
        if text not in convo['links']: convo['links'][text] = {}
        convo['state'] = 'wait_season_480'
        await message.reply_text(f"👍 **Season {text}** selected.\nSend **480p** link (or `skip`).")
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
        await message.reply_text(f"✅ Season {s_num} saved.\n**Enter next Season Number OR type `done`:**")

    elif state == "wait_badge_text":
        convo['temp_badge_text'] = text
        msg = await message.reply_text(f"✅ Badge text set: **{text}**\nGenerating preview...")
        await generate_final_post_preview(client, uid, message.chat.id, msg)

@bot.on_callback_query(filters.regex("^postto_"))
async def post_to_channel_cb(client, cb: CallbackQuery):
    uid = cb.from_user.id
    channel_id = cb.data.split("_")[1]
    convo = user_conversations.get(uid)
    
    if not convo or 'final_post' not in convo:
        return await cb.answer("❌ Session expired!", show_alert=True)

    await cb.answer("⏳ Posting...", show_alert=False)
    final_post = convo['final_post']
    try:
        if final_post['poster']:
            final_post['poster'].seek(0)
            await client.send_photo(int(channel_id), final_post['poster'], caption=final_post['caption'], parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await client.send_message(int(channel_id), final_post['caption'], parse_mode=enums.ParseMode.MARKDOWN)
        await cb.message.edit_text(f"✅ **Posted to channel!**")
        await log_event(f"📤 Post published by User {uid} to Channel {channel_id}")
    except Exception as e:
        await cb.message.edit_text(f"❌ Failed: `{e}`")
    finally:
        if uid in user_conversations: del user_conversations[uid]

if __name__ == "__main__":
    logger.info("🚀 Bot is starting...")
    bot.run()
