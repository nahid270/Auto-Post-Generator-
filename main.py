# -*- coding: utf-8 -*-

# ---- Core Python Imports ----
import os
import io
import re
import requests
import random
from threading import Thread
import logging

# --- Third-party Library Imports ---
from PIL import Image, ImageDraw, ImageFont
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.errors import UserNotParticipant
from flask import Flask
from dotenv import load_dotenv
import motor.motor_asyncio # ✨ MongoDB-র জন্য

# ---- 1. CONFIGURATION AND SETUP ----
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL")
INVITE_LINK = os.getenv("INVITE_LINK")
JOIN_CHANNEL_TEXT = "🎬 সকল মুভি এবং সিরিজের আপডেট পেতে"
JOIN_CHANNEL_LINK = "https://t.me/+60goZWp-FpkxNzVl" # আপনার চ্যানেলের লিংক দিন

# ⭐️ NEW: Setup basic logging
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
    # 🔄 MODIFIED: Store user's first name for better interaction
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
                return await message.reply_text("❗ **এই বট ব্যবহার করতে আমাদের চ্যানেলে যোগ দিন।**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👉 চ্যানেলে যোগ দিন", url=join_link)]]))
        await func(client, message)
    return wrapper

# ⭐️ NEW: Link Shortener Function
async def shorten_link(user_id: int, long_url: str):
    """Shortens a URL using the user's saved shortener settings."""
    user_data = await users_collection.find_one({'_id': user_id})
    if not user_data or 'shortener_api' not in user_data or 'shortener_url' not in user_data:
        return long_url # Return original URL if no shortener is configured

    api_key = user_data['shortener_api']
    base_url = user_data['shortener_url']
    
    # Construct the API URL. This format is common for many shorteners (e.g., Zagl, ShrinkEarn).
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
            return long_url # Return original on API error
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to call shortener API for user {user_id}. Error: {e}")
        return long_url # Return original on network error

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

async def generate_channel_caption(data: dict, language: str, links: dict, user_data: dict):
    title = data.get("title") or data.get("name") or "N/A"
    year = (data.get("release_date") or data.get("first_air_date") or "----")[:4]
    genres = ", ".join([g["name"] for g in data.get("genres", [])[:3]]) or "N/A"
    rating = f"{data.get('vote_average', 0):.1f}/10"
    overview = data.get("overview", "কাহিনী সংক্ষেপ পাওয়া যায়নি।")
    if len(overview) > 250: overview = overview[:250] + "..."
    cast = ", ".join([a['name'] for a in data.get('credits', {}).get('cast', [])[:3]]) or "N/A"
    
    caption_header = (f"⭐️ **রেটিং:** {rating}\n🎭 **ধরন:** {genres}\n"
                      f"🔊 **ভাষা:** {language}\n")

    if 'first_air_date' in data: # TV Show
        runtime_list = data.get("episode_run_time", [])
        runtime = format_runtime(runtime_list[0] if runtime_list else 0)
        seasons = data.get("number_of_seasons", "N/A")
        caption_header = (f"📺 **{title} ({year})**\n\n" + caption_header +
                          f"📊 **মোট সিজন:** {seasons}\n"
                          f"⏰ **প্রতি পর্বের রানটাইম:** {runtime}\n"
                          f"👥 **অভিনয়ে:** {cast}\n\n📝 **কাহিনী সংক্ষেপ:** {overview}\n\n")
        
        link_section = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n📥 **ডাউনলোড লিংকসমূহ** 📥\n▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        if links:
            sorted_seasons = sorted(links.keys(), key=int)
            for season_num in sorted_seasons:
                link_section += f"✅ **[সিজন {season_num} ডাউনলোড করুন]({links[season_num]})**\n"
    else: # Movie
        runtime = format_runtime(data.get("runtime", 0))
        caption_header = (f"🎬 **{title} ({year})**\n\n" + caption_header +
                          f"⏰ **রানটাইম:** {runtime}\n"
                          f"👥 **অভিনয়ে:** {cast}\n\n📝 **কাহিনী সংক্ষেপ:** {overview}\n\n")
        
        link_section = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n📥 **ডাউনলোড লিংকসমূহ** 📥\n▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        if links.get("480p"): link_section += f"✅ **[480p কোয়ালিটি ডাউনলোড]({links['480p']})**\n"
        if links.get("720p"): link_section += f"✅ **[720p কোয়ালিটি ডাউনলোড]({links['720p']})**\n"
        if links.get("1080p"): link_section += f"✅ **[1080p কোয়ালিটি ডাউনলোড]({links['1080p']})**\n"

    caption = caption_header + link_section

    if user_data and user_data.get('tutorial_link'):
        caption += f"\n🎥 **কিভাবে ডাউনলোড করবেন:** [টিউটোরিয়াল দেখুন]({user_data['tutorial_link']})"

    if JOIN_CHANNEL_TEXT and JOIN_CHANNEL_LINK:
        caption += f"\n---\n**আমাদের অন্য চ্যানেলে যোগ দিন 👇**\n[👉 {JOIN_CHANNEL_TEXT}]({JOIN_CHANNEL_LINK})"
    return caption

# ---- 4. BOT HANDLERS ----
@bot.on_message(filters.command("start") & filters.private)
@force_subscribe
async def start_cmd(client, message: Message):
    await add_user_to_db(message.from_user)
    await message.reply_text(f"👋 **স্বাগতম, {message.from_user.first_name}! আমি মুভি ও সিরিজ পোস্ট জেনারেটর বট।**\n\n"
        "**আমার কমান্ডগুলো হলো:**\n"
        "🔹 `/post <name>` - মুভি বা সিরিজের জন্য পোস্ট তৈরি করুন।\n"
        "🔹 `/cancel` - যেকোনো চলমান প্রক্রিয়া বাতিল করুন।\n\n"
        "**সেটিংস:**\n"
        "🔹 `/settings` - আপনার বর্তমান সেটিংস দেখুন।\n"
        "🔹 `/setchannel <ID>` - পোস্ট করার জন্য চ্যানেল আইডি সেট করুন।\n"
        "🔹 `/setwatermark <text>` - পোস্টারে ওয়াটারমার্ক সেট করুন।\n"
        "🔹 `/setapi <API_KEY>` - আপনার লিঙ্ক শর্টনারের API Key সেট করুন।\n"
        "🔹 `/setdomain <URL>` - আপনার শর্টনার ডোমেইন সেট করুন (e.g., yoursite.com)।\n"
        "🔹 `/settutorial <link>` - ডাউনলোড টিউটোরিয়াল লিঙ্ক সেট করুন।")

# 🔄 পরিবর্তন: লিঙ্ক শর্টনার কমান্ডগুলো আলাদা করা হয়েছে এবং settings কমান্ডের সাথে যুক্ত করা হয়েছে।
@bot.on_message(filters.command(["setwatermark", "setchannel", "cancel", "setapi", "setdomain", "settutorial", "settings"]) & filters.private)
@force_subscribe
async def settings_commands(client, message: Message):
    command = message.command[0].lower()
    uid = message.from_user.id
    await add_user_to_db(message.from_user)

    if command == "setwatermark":
        text = " ".join(message.command[1:]) if len(message.command) > 1 else None
        await users_collection.update_one({'_id': uid}, {'$set': {'watermark_text': text}}, upsert=True)
        await message.reply_text(f"✅ ওয়াটারমার্ক {'সেট হয়েছে: `' + text + '`' if text else 'মুছে ফেলা হয়েছে।'}")
    
    elif command == "setchannel":
        if len(message.command) > 1 and message.command[1].startswith("-100") and message.command[1][1:].isdigit():
            cid = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$set': {'channel_id': cid}}, upsert=True)
            await message.reply_text(f"✅ চ্যানেল সেট হয়েছে: `{cid}`")
        else:
            await message.reply_text("⚠️ অবৈধ চ্যানেল আইডি। আইডি অবশ্যই `-100` দিয়ে শুরু হতে হবে।\n**ব্যবহার:** `/setchannel -100...`")
            
    elif command == "cancel":
        if uid in user_conversations:
            del user_conversations[uid]
            await message.reply_text("✅ প্রক্রিয়া বাতিল করা হয়েছে।")
        else:
            await message.reply_text("🚫 বাতিল করার মতো কোনো প্রক্রিয়া চালু নেই।")

    # ⭐️ NEW: Shortener API command handler
    elif command == "setapi":
        if len(message.command) > 1:
            api_key = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$set': {'shortener_api': api_key}}, upsert=True)
            await message.reply_text(f"✅ শর্টনার API Key সেট হয়েছে: `{api_key}`")
        else:
            await message.reply_text("⚠️ ভুল ফরম্যাট!\n**ব্যবহার:** `/setapi <আপনার_API_KEY>`")

    # ⭐️ NEW: Shortener Domain command handler
    elif command == "setdomain":
        if len(message.command) > 1:
            domain = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$set': {'shortener_url': domain}}, upsert=True)
            await message.reply_text(f"✅ শর্টনার ডোমেইন সেট হয়েছে: `{domain}`")
        else:
            await message.reply_text("⚠️ ভুল ফরম্যাট!\n**ব্যবহার:** `/setdomain yourshortener.com` (http:// বা https:// ছাড়া)।")

    # ⭐️ NEW: Tutorial command handler
    elif command == "settutorial":
        if len(message.command) > 1:
            link = message.command[1]
            await users_collection.update_one({'_id': uid}, {'$set': {'tutorial_link': link}}, upsert=True)
            await message.reply_text(f"✅ টিউটোরিয়াল লিঙ্ক সেট হয়েছে: {link}")
        else:
            await users_collection.update_one({'_id': uid}, {'$unset': {'tutorial_link': ""}})
            await message.reply_text("✅ টিউটোরিয়াল লিঙ্ক মুছে ফেলা হয়েছে।")

    # ⭐️ NEW: Settings view command
    elif command == "settings":
        user_data = await users_collection.find_one({'_id': uid})
        if not user_data:
            return await message.reply_text("আপনার কোনো সেটিংস সেভ করা নেই।")
        
        settings_text = "**⚙️ আপনার বর্তমান সেটিংস:**\n\n"
        settings_text += f"**চ্যানেল আইডি:** `{user_data.get('channel_id', 'সেট করা নেই')}`\n"
        settings_text += f"**ওয়াটারমার্ক:** `{user_data.get('watermark_text', 'সেট করা নেই')}`\n"
        settings_text += f"**টিউটোরিয়াল লিঙ্ক:** `{user_data.get('tutorial_link', 'সেট করা নেই')}`\n"
        
        shortener_api = user_data.get('shortener_api')
        shortener_url = user_data.get('shortener_url')
        if shortener_api and shortener_url:
            settings_text += f"**শর্টনার API:** `{shortener_api}`\n"
            settings_text += f"**শর্টনার URL:** `{shortener_url}`\n"
        else:
            settings_text += "**শর্টনার:** `সেট করা নেই` (API ও ডোমেইন দুটোই সেট করুন)\n"
            
        await message.reply_text(settings_text)

async def generate_final_post_preview(client, uid, cid, msg):
    convo = user_conversations.get(uid)
    if not convo: return
    
    user_data = await users_collection.find_one({'_id': uid})
    
    caption = await generate_channel_caption(convo["details"], convo["language"], convo["links"], user_data)
    
    watermark = user_data.get('watermark_text') if user_data else None
    poster_url = f"https://image.tmdb.org/t/p/w500{convo['details']['poster_path']}" if convo['details'].get('poster_path') else None
    
    await msg.edit_text("🖼️ পোস্টার তৈরি এবং ওয়াটারমার্ক যোগ করা হচ্ছে...")
    poster, error = watermark_poster(poster_url, watermark)
    
    await msg.delete()
    if error:
        await client.send_message(cid, f"⚠️ **পোস্টার তৈরিতে সমস্যা:** `{error}`")

    poster_buffer = None
    if poster:
        poster.seek(0)
        poster_buffer = io.BytesIO(poster.read())
        poster.seek(0)

    preview_msg = await client.send_photo(cid, photo=poster, caption=caption, parse_mode=enums.ParseMode.MARKDOWN) if poster else await client.send_message(cid, caption, parse_mode=enums.ParseMode.MARKDOWN)
    
    user_conversations[uid]['final_post'] = {'caption': caption, 'poster': poster_buffer}

    channel_id = user_data.get('channel_id') if user_data else None
    if channel_id:
        await client.send_message(cid, "**👆 এটি একটি প্রিভিউ।**\nআপনার চ্যানেলে পোস্ট করবেন?",
            reply_to_message_id=preview_msg.id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 হ্যাঁ, চ্যানেলে পোস্ট করুন", callback_data=f"finalpost_{uid}")]]))

@bot.on_message(filters.command("post") & filters.private)
@force_subscribe
async def search_commands(client, message: Message):
    if len(message.command) == 1:
        return await message.reply_text("**ব্যবহার:** `/post Movie or Series Name`")
    query = " ".join(message.command[1:])
    processing_msg = await message.reply_text(f"🔍 `{query}`-এর জন্য খোঁজা হচ্ছে...")
    results = search_tmdb(query)
    if not results:
        return await processing_msg.edit_text("❌ কোনো ফলাফল পাওয়া যায়নি।")
    buttons = []
    for r in results:
        media_icon = '🎬' if r['media_type'] == 'movie' else '📺'
        title = r.get('title') or r.get('name')
        year = (r.get('release_date') or r.get('first_air_date') or '----').split('-')[0]
        buttons.append([InlineKeyboardButton(f"{media_icon} {title} ({year})", callback_data=f"select_post_{r['media_type']}_{r['id']}")])
    await processing_msg.edit_text("**👇 ফলাফল থেকে বেছে নিন:**", reply_markup=InlineKeyboardMarkup(buttons))

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
            processing_msg = await message.reply("🔗 লিঙ্কটি শর্ট করা হচ্ছে...", quote=True)
            shortened = await shorten_link(uid, text)
            convo["links"][quality] = shortened
        convo["state"] = next_state
        prompt = f"✅ লিঙ্ক {'শর্ট এবং যোগ করা হয়েছে' if text.lower() != 'skip' else 'স্কিপ করা হয়েছে'}। এখন {next_prompt}"
        if processing_msg: await processing_msg.edit_text(prompt)
        else: await message.reply_text(prompt)

    if state == "wait_movie_lang":
        convo["language"] = text; convo["state"] = "wait_480p"
        await message.reply_text("✅ ভাষা সেট হয়েছে। এখন **480p** লিংক পাঠান অথবা `skip` লিখুন।")
    elif state == "wait_480p":
        await process_link("480p", "wait_720p", "**720p** লিংক পাঠান অথবা `skip` লিখুন।")
    elif state == "wait_720p":
        await process_link("720p", "wait_1080p", "**1080p** লিংক পাঠান অথবা `skip` লিখুন।")
    elif state == "wait_1080p":
        if text.lower() != 'skip':
            processing_msg = await message.reply("🔗 লিঙ্কটি শর্ট করা হচ্ছে...", quote=True)
            shortened = await shorten_link(uid, text)
            convo["links"]["1080p"] = shortened
        msg = await (processing_msg.edit_text if processing_msg else message.reply)("✅ তথ্য সংগ্রহ সম্পন্ন! প্রিভিউ তৈরি করা হচ্ছে...")
        await generate_final_post_preview(client, uid, message.chat.id, msg)

    elif state == "wait_tv_lang":
        convo["language"] = text; convo["state"] = "wait_season_number"
        await message.reply_text("✅ ভাষা সেট হয়েছে। এখন সিজনের নম্বর লিখুন (যেমন: 1, 2)।")
    elif state == "wait_season_number":
        if text.lower() == 'done':
            if not convo.get('seasons'): return await message.reply_text("⚠️ আপনি কোনো সিজনের লিংক যোগ করেননি।")
            msg = await message.reply_text("✅ সকল সিজনের তথ্য সংগ্রহ সম্পন্ন! প্রিভিউ তৈরি করা হচ্ছে...", quote=True)
            convo['links'] = convo['seasons']
            await generate_final_post_preview(client, uid, message.chat.id, msg)
            return
        if not text.isdigit() or int(text) <= 0: return await message.reply_text("❌ ভুল নম্বর। দয়া করে একটি সঠিক সিজন নম্বর দিন।")
        convo['current_season'] = text
        convo['state'] = 'wait_season_link'
        await message.reply_text(f"👍 ঠিক আছে। এখন **সিজন {text}**-এর ডাউনলোড লিংক পাঠান।")
    elif state == "wait_season_link":
        season_num = convo.get('current_season')
        processing_msg = await message.reply("🔗 লিঙ্কটি শর্ট করা হচ্ছে...", quote=True)
        shortened = await shorten_link(uid, text)
        convo['seasons'][season_num] = shortened
        convo['state'] = 'wait_season_number'
        await processing_msg.edit_text(f"✅ সিজন {season_num}-এর লিংক যোগ করা হয়েছে।\n\n**👉 পরবর্তী সিজনের নম্বর লিখুন, অথবা পোস্ট শেষ করতে `done` লিখুন।**")

@bot.on_callback_query(filters.regex("^select_"))
async def selection_cb(client, cb: CallbackQuery):
    await cb.answer("Fetching details...", show_alert=False)
    try: _, flow, media_type, mid = cb.data.split("_", 3)
    except Exception as e:
        logger.error(f"Callback Error on split: {e}")
        return await cb.message.edit_text("Invalid callback data.")
        
    details = get_tmdb_details(media_type, int(mid))
    if not details: return await cb.message.edit_text("❌ দুঃখিত, TMDB থেকে বিস্তারিত তথ্য আনতে পারিনি।")
    
    uid = cb.from_user.id
    user_conversations[uid] = {"flow": flow, "details": details, "links": {}, "state": ""}
    
    if media_type == "tv":
        user_conversations[uid]["state"] = "wait_tv_lang"
        user_conversations[uid]['seasons'] = {}
        await cb.message.edit_text("**ওয়েব সিরিজ পোস্ট:** সিরিজটির জন্য ভাষা লিখুন (যেমন: বাংলা, ইংরেজি)।")
    elif media_type == "movie":
        user_conversations[uid]["state"] = "wait_movie_lang"
        await cb.message.edit_text("**মুভি পোস্ট:** মুভিটির জন্য ভাষা লিখুন।")

@bot.on_callback_query(filters.regex("^finalpost_"))
async def post_to_channel_cb(client, cb: CallbackQuery):
    uid = cb.from_user.id
    user_data = await users_collection.find_one({'_id': uid})
    channel_id = user_data.get('channel_id') if user_data else None

    if not channel_id:
        await cb.answer("⚠️ চ্যানেল সেট করা নেই!", show_alert=True)
        return await cb.message.edit_text("আপনি এখনও কোনো চ্যানেল সেট করেননি। `/setchannel <ID>` কমান্ড ব্যবহার করুন।")

    convo = user_conversations.get(uid)
    if not convo or 'final_post' not in convo:
        await cb.answer("❌ সেশন শেষ হয়ে গেছে!", show_alert=True)
        return await cb.message.edit_text("দুঃখিত, এই পোস্টের তথ্য আর পাওয়া যাচ্ছে না। অনুগ্রহ করে আবার শুরু করুন।")

    await cb.answer("⏳ পোস্ট করা হচ্ছে...", show_alert=False)
    
    final_post = convo['final_post']
    caption = final_post['caption']
    poster = final_post['poster']

    try:
        if poster:
            poster.seek(0)
            await client.send_photo(chat_id=int(channel_id), photo=poster, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await client.send_message(chat_id=int(channel_id), text=caption, parse_mode=enums.ParseMode.MARKDOWN)
        
        await cb.message.edit_text("✅ **সফলভাবে আপনার চ্যানেলে পোস্ট করা হয়েছে!**")
    except Exception as e:
        logger.error(f"Failed to post to channel {channel_id} for user {uid}. Error: {e}")
        error_message = (f"❌ **চ্যানেলে পোস্ট করতে সমস্যা হয়েছে।**\n\n"
                         f"**সম্ভাব্য কারণ:**\n"
                         f"1. বট কি আপনার চ্যানেলের (`{channel_id}`) সদস্য?\n"
                         f"2. বটের কি 'Post Messages' করার অনুমতি আছে?\n"
                         f"3. চ্যানেল আইডি কি সঠিক?\n\n"
                         f"**Error:** `{e}`")
        await cb.message.edit_text(error_message)
    finally:
        if uid in user_conversations:
            del user_conversations[uid]

# ---- 5. START THE BOT ----
if __name__ == "__main__":
    logger.info("🚀 Bot is starting with MongoDB connection...")
    bot.run()
    logger.info("👋 Bot has stopped.")
