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

def watermark_poster(poster_url: str, watermark_text: str):
    if not poster_url: return None, "Poster URL not found."
    try:
        img_data = requests.get(poster_url, timeout=20).content
        img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        if watermark_text:
            draw = ImageDraw.Draw(img)
            font_size = int(img.width / 12)
            try:
                font = ImageFont.truetype("Poppins-Bold.ttf", font_size)
            except IOError:
                logger.warning("Poppins-Bold.ttf not found. Using default font.")
                font = ImageFont.load_default()
            
            thumbnail = img.resize((150, 150))
            colors = thumbnail.getcolors(150*150)
            text_color = (255, 255, 255, 230)
            if colors:
                dominant_color = sorted(colors, key=lambda x: x[0], reverse=True)[0][1]
                text_color = (255 - dominant_color[0], 255 - dominant_color[1], 255 - dominant_color[2], 230)

            bbox = draw.textbbox((0, 0), watermark_text, font=font)
            text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = (img.width - text_width) / 2
            y = img.height - text_height - (img.height * 0.05)
            draw.text((x + 2, y + 2), watermark_text, font=font, fill=(0, 0, 0, 128))
            draw.text((x, y), watermark_text, font=font, fill=text_color)
            
        buffer = io.BytesIO(); buffer.name = "poster.png"
        img.save(buffer, "PNG"); buffer.seek(0)
        return buffer, None
    except requests.exceptions.RequestException as e: return None, f"Network Error: {e}"
    except Exception as e: return None, f"Image processing error. Error: {e}"

# ⭐️ FINAL TEMPLATE SYSTEM (WITH TUTORIAL LINK) ⭐️
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
    # For TV Series
    if 'first_air_date' in data and links:
        sorted_seasons = sorted(links.keys(), key=lambda x: int(x))
        season_links = []
        for season_num in sorted_seasons:
            season_links.append(f"✅ [Download Season {season_num}]({links[season_num]})")
        download_links = "\n".join(season_links)
    # For Movies
    else:
        movie_links = []
        if info['link_480p']: movie_links.append(f"[Download 480p]({info['link_480p']})")
        if info['link_720p']: movie_links.append(f"[Download 720p]({info['link_720p']})")
        if info['link_1080p']: movie_links.append(f"[Download 1080p]({info['link_1080p']})")
        download_links = "\n\n".join(movie_links)

    static_footer = """Movie ReQuest Group 
👇👇👇
https://t.me/Terabox_search_group

Premium Backup Group link 👇👇👇
https://t.me/+GL_XAS4MsJg4ODM1"""

    caption_parts = [caption_header, download_section_header]
    if download_links:
        caption_parts.append(download_links.strip())
    
    if user_data and user_data.get('tutorial_link'):
        tutorial_text = f"🎥 **How To Download:** [Watch Tutorial]({user_data['tutorial_link']})"
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

# ⭐️ Multi-Channel Management Commands ⭐️
@bot.on_message(filters.command(["addchannel", "delchannel", "mychannels"]) & filters.private)
@force_subscribe
async def channel_management(client, message: Message):
    command = message.command[0].lower()
    uid = message.from_user.id
    
    if command == "addchannel":
        if len(message.command) > 1 and message.command[1].startswith("-100") and message.command[1][1:].isdigit():
            cid = message.command[1]
            await users_collection.update_one(
                {'_id': uid},
                {'$addToSet': {'channel_ids': cid}},
                upsert=True
            )
            await message.reply_text(f"✅ Channel `{cid}` added successfully.")
        else: await message.reply_text("⚠️ Invalid Channel ID. It must start with `-100`.\n**Usage:** `/addchannel -100...`")

    elif command == "delchannel":
        if len(message.command) > 1 and message.command[1].startswith("-100") and message.command[1][1:].isdigit():
            cid = message.command[1]
            await users_collection.update_one(
                {'_id': uid},
                {'$pull': {'channel_ids': cid}}
            )
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
    poster_url = f"https://image.tmdb.org/t/p/w500{convo['details']['poster_path']}" if convo['details'].get('poster_path') else None
    
    await msg.edit_text("🖼️ Creating poster and adding watermark...")
    poster, error = watermark_poster(poster_url, watermark)
    
    await msg.delete()
    if error: await client.send_message(cid, f"⚠️ **Error creating poster:** `{error}`")

    poster_buffer = io.BytesIO(poster.read()) if poster else None
    
    preview_msg = await client.send_photo(cid, photo=io.BytesIO(poster_buffer.getvalue()) if poster_buffer else poster, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
    user_conversations[uid]['final_post'] = {'caption': caption, 'poster': poster_buffer}

    saved_channels = user_data.get('channel_ids', [])
    if saved_channels:
        buttons = []
        for channel in saved_channels:
            buttons.append([InlineKeyboardButton(f"📢 Post to {channel}", callback_data=f"postto_{channel}")])
        
        await client.send_message(cid, "**👆 This is a preview. Choose a channel to post to:**",
            reply_to_message_id=preview_msg.id,
            reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await client.send_message(cid, "✅ Preview generated. You have no channels saved. Use `/addchannel` to add one.",
            reply_to_message_id=preview_msg.id)

@bot.on_message(filters.command("post") & filters.private)
@force_subscribe
async def search_commands(client, message: Message):
    if len(message.command) == 1:
        return await message.reply_text("**Usage:** `/post Movie or Series Name`")
    query = " ".join(message.command[1:])
    processing_msg = await message.reply_text(f"🔍 Searching for `{query}`...")
    results = search_tmdb(query)
    if not results:
        return await processing_msg.edit_text("❌ No results found.")
    
    buttons = []
    for r in results:
        media_icon = '🎬' if r['media_type'] == 'movie' else '📺'
        title = r.get('title') or r.get('name')
        year = (r.get('release_date') or r.get('first_air_date') or '----').split('-')[0]
        buttons.append([InlineKeyboardButton(f"{media_icon} {title} ({year})", callback_data=f"select_post_{r['media_type']}_{r['id']}")])
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
    user_conversations[uid] = {"flow": flow, "details": details, "links": {}, "state": ""}
    
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
        if poster:
            poster.seek(0)
            await client.send_photo(chat_id=int(channel_id), photo=poster, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await client.send_message(chat_id=int(channel_id), text=caption, parse_mode=enums.ParseMode.MARKDOWN)
        
        await cb.message.edit_text(f"✅ **Successfully posted to channel `{channel_id}`!**")
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
