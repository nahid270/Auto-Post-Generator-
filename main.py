# -*- coding: utf-8 -*-

# ---- Core Python Imports ----
import os
import io
import re
import requests
import sqlite3
from threading import Thread

# --- Third-party Library Imports ---
from PIL import Image, ImageDraw, ImageFont
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.errors import UserNotParticipant, MessageNotModified
from flask import Flask
from dotenv import load_dotenv

# ---- 1. CONFIGURATION AND SETUP ----
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL")
INVITE_LINK = os.getenv("INVITE_LINK")

# ---- আপনার চ্যানেলের লিংক এখানে যুক্ত করুন ----
JOIN_CHANNEL_TEXT = "🎬 সকল মুভি এবং সিরিজের আপডেট পেতে"
JOIN_CHANNEL_LINK = "https://t.me/YourChannelUsername" # এখানে আপনার চ্যানেলের লিংক দিন


# ---- Database Setup ----
DB_FILE = "bot_settings.db"
def db_query(query, params=(), fetch=None):
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        if fetch == 'one': return cursor.fetchone()
        if fetch == 'all': return cursor.fetchall()

db_query('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, watermark_text TEXT, channel_id TEXT)')

# ---- Global Variables & Bot Initialization ----
user_conversations = {}
bot = Client("UltimateMovieBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---- Flask App (for Keep-Alive on Render/Koyeb) ----
app = Flask(__name__)
@app.route('/')
def home(): return "✅ Bot is Running!"
Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080))), daemon=True).start()

# ---- 2. DECORATORS AND HELPER FUNCTIONS ----
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
        print(f"TMDB Search Error: {e}"); return []

def get_tmdb_details(media_type: str, media_id: int):
    url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}&append_to_response=credits,videos"
    try:
        r = requests.get(url, timeout=10); r.raise_for_status(); return r.json()
    except Exception as e:
        print(f"TMDB Details Error: {e}"); return None

def watermark_poster(poster_url: str, watermark_text: str):
    if not poster_url: return None, "Poster URL not found."
    try:
        img_data = requests.get(poster_url, timeout=20).content
        img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        if watermark_text:
            draw = ImageDraw.Draw(img)
            try: font = ImageFont.truetype("Poppins-Bold.ttf", 25)
            except IOError: font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), watermark_text, font=font)
            text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x, y = img.width - text_width - 20, img.height - text_height - 20
            draw.text((x+1, y+1), watermark_text, font=font, fill=(0, 0, 0, 128))
            draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255, 220))
        buffer = io.BytesIO(); buffer.name = "poster.png"
        img.save(buffer, "PNG"); buffer.seek(0)
        return buffer, None
    except requests.exceptions.RequestException as e: return None, f"Network Error: {e}"
    except Exception as e: return None, f"Image processing error. Is 'Poppins-Bold.ttf' missing? Error: {e}"

# ---- ✨ নতুন ক্যাপশন ফাংশন (মুভি ও ওয়েব সিরিজের জন্য আলাদা) ✨ ----
def generate_channel_caption(data: dict, language: str, links: dict):
    title = data.get("title") or data.get("name") or "N/A"
    year = (data.get("release_date") or data.get("first_air_date") or "----")[:4]
    genres = ", ".join([g["name"] for g in data.get("genres", [])[:3]]) or "N/A"
    rating = f"{data.get('vote_average', 0):.1f}/10"
    overview = data.get("overview", "কাহিনী সংক্ষেপ পাওয়া যায়নি।")
    if len(overview) > 250: overview = overview[:250] + "..."
    cast = ", ".join([a['name'] for a in data.get('credits', {}).get('cast', [])[:3]]) or "N/A"
    
    # ---- ওয়েব সিরিজের জন্য বিশেষ টেমপ্লেট ----
    if 'first_air_date' in data: # এটি ওয়েব সিরিজ কিনা তা পরীক্ষা করে
        runtime_list = data.get("episode_run_time", [])
        runtime = format_runtime(runtime_list[0] if runtime_list else 0)
        seasons = data.get("number_of_seasons", "N/A")

        caption = (f"📺 **{title} ({year})**\n\n"
                   f"⭐️ **রেটিং:** {rating}\n🎭 **ধরন:** {genres}\n"
                   f"🔊 **ভাষা:** {language}\n📊 **মোট সিজন:** {seasons}\n"
                   f"⏰ **প্রতি পর্বের রানটাইম:** {runtime}\n"
                   f"👥 **অভিনয়ে:** {cast}\n\n📝 **কাহিনী সংক্ষেপ:** {overview}\n\n"
                   "📥 **ডাউনলোড লিংক** 👇\n")
        
        # সিজন অনুযায়ী লিংক যোগ করা
        if links:
            sorted_seasons = sorted(links.keys(), key=int)
            for season_num in sorted_seasons:
                caption += f"🔹 **সিজন {season_num}:** [ডাউনলোড করুন]({links[season_num]})\n"

    # ---- মুভির জন্য পুরনো টেমপ্লেট ----
    else:
        runtime = format_runtime(data.get("runtime", 0))
        caption = (f"🎬 **{title} ({year})**\n\n"
                   f"⭐️ **রেটিং:** {rating}\n🎭 **ধরন:** {genres}\n"
                   f"🔊 **ভাষা:** {language}\n⏰ **রানটাইম:** {runtime}\n"
                   f"👥 **অভিনয়ে:** {cast}\n\n📝 **কাহিনী সংক্ষেপ:** {overview}\n\n"
                   "📥 **ডাউনলোড লিংক** 👇\n")
        if links.get("480p"): caption += f"🔹 **480p:** [ডাউনলোড করুন]({links['480p']})\n"
        if links.get("720p"): caption += f"🔹 **720p:** [ডাউনলোড করুন]({links['720p']})\n"
        if links.get("1080p"): caption += f"🔹 **1080p:** [ডাউনলোড করুন]({links['1080p']})\n"

    if JOIN_CHANNEL_TEXT and JOIN_CHANNEL_LINK:
        caption += f"\n---\n**আমাদের অন্য চ্যানেলে যোগ দিন 👇**\n[👉 {JOIN_CHANNEL_TEXT}]({JOIN_CHANNEL_LINK})"
    return caption

# ---- 4. BOT HANDLERS ----
@bot.on_message(filters.command("start") & filters.private)
@force_subscribe
async def start_cmd(client, message: Message):
    db_query("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
    await message.reply_text("👋 **স্বাগতম! আমি মুভি ও সিরিজ পোস্ট জেনারেটর বট।**\n\n"
        "**আমার কমান্ডগুলো হলো:**\n"
        "🔹 `/post <name>` - মুভি বা সিরিজের জন্য পোস্ট তৈরি করুন।\n"
        "🔹 `/quickpost <name>` - দ্রুত পোস্ট তৈরি করুন (শুধু মুভির জন্য)।\n"
        "🔹 `/testpost` - চ্যানেল সংযোগ পরীক্ষা করুন।\n\n"
        "**সেটিংস:**\n"
        "🔹 `/setchannel <ID>` - পোস্ট করার জন্য আপনার চ্যানেল সেট করুন।\n"
        "🔹 `/setwatermark <text>` - পোস্টারে আপনার ওয়াটারমার্ক সেট করুন।\n"
        "🔹 `/cancel` - যেকোনো চলমান প্রক্রিয়া বাতিল করুন।",
        parse_mode=enums.ParseMode.MARKDOWN)

@bot.on_message(filters.command(["post", "blogger"]) & filters.private)
@force_subscribe
async def search_commands(client, message: Message):
    command = message.command[0].lower()
    if len(message.command) == 1:
        return await message.reply_text(f"**ব্যবহার:** `/{command} Movie or Series Name`")
    
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
        buttons.append([InlineKeyboardButton(f"{media_icon} {title} ({year})", callback_data=f"select_{command}_{r['media_type']}_{r['id']}")])
    
    await processing_msg.edit_text("**👇 ফলাফল থেকে বেছে নিন:**", reply_markup=InlineKeyboardMarkup(buttons))

# ... (quickpost অপরিবর্তিত থাকবে কারণ এটি শুধু মুভির জন্য) ...
@bot.on_message(filters.command("quickpost") & filters.private)
@force_subscribe
async def quick_post_search(client, message: Message):
    # ... (এই কোড অপরিবর্তিত) ...
    if len(message.command) == 1: return await message.reply_text("**ব্যবহার:** `/quickpost Movie Name`")
    user_settings = db_query("SELECT channel_id FROM users WHERE user_id = ?", (message.from_user.id,), 'one')
    if not user_settings or not user_settings[0]: return await message.reply_text("⚠️ দ্রুত পোস্টের জন্য প্রথমে আপনার চ্যানেল সেট করুন।\nব্যবহার করুন: `/setchannel <channel_id>`")
    query = " ".join(message.command[1:])
    processing_msg = await message.reply_text(f"🔍 `{query}`-এর জন্য খোঁজা হচ্ছে...")
    results = search_tmdb(query)
    # শুধুমাত্র মুভি ফিল্টার করা হচ্ছে
    movie_results = [r for r in results if r['media_type'] == 'movie']
    if not movie_results: return await processing_msg.edit_text("❌ কোনো মুভি পাওয়া যায়নি।")
    buttons = [[InlineKeyboardButton(f"🎬 {r.get('title')} ({r.get('release_date', '----').split('-')[0]})", callback_data=f"qpost_{r['media_type']}_{r['id']}")] for r in movie_results]
    await processing_msg.edit_text("**👇 দ্রুত পোস্টের জন্য মুভি বেছে নিন:**", reply_markup=InlineKeyboardMarkup(buttons))
    

# ---- ✨ নতুন সিলেকশন হ্যান্ডলার (মুভি ও ওয়েব সিরিজের জন্য আলাদা ফ্লো) ✨ ----
@bot.on_callback_query(filters.regex("^select_"))
async def selection_cb(client, cb: Message):
    await cb.answer("Fetching details...")
    try: _, flow, media_type, mid = cb.data.split("_", 3)
    except: return await cb.message.edit_text("Invalid callback.")
    
    details = get_tmdb_details(media_type, int(mid))
    if not details: return await cb.message.edit_text("❌ Failed to get details.")
    
    uid = cb.from_user.id
    user_conversations[uid] = {"flow": flow, "details": details, "links": {}, "state": ""}
    
    if flow == "blogger":
        return await cb.message.edit("Blogger flow not implemented.")
    
    # ---- ওয়েব সিরিজের জন্য নতুন ফ্লো ----
    if media_type == "tv":
        user_conversations[uid]["state"] = "wait_tv_lang"
        user_conversations[uid]['seasons'] = {}
        await cb.message.edit_text("**ওয়েব সিরিজ পোস্ট:** সিরিজটির জন্য ভাষা লিখুন (যেমন: বাংলা, ইংরেজি)।")
    # ---- মুভির জন্য পুরনো ফ্লো ----
    elif media_type == "movie":
        user_conversations[uid]["state"] = "wait_movie_lang"
        await cb.message.edit_text("**মুভি পোস্ট:** মুভিটির জন্য ভাষা লিখুন।")


# ---- ✨ নতুন কনভারসেশন হ্যান্ডলার (সিজন লিংক যোগ করার সুবিধাসহ) ✨ ----
@bot.on_message(filters.text & filters.private & ~filters.command(["start", "post", "blogger", "quickpost", "setwatermark", "setchannel", "cancel", "testpost"]))
@force_subscribe
async def conversation_handler(client, message: Message):
    uid = message.from_user.id
    convo = user_conversations.get(uid)
    if not convo or "state" not in convo: return
    
    state, text = convo["state"], message.text.strip()

    # --- মুভি পোস্ট ফ্লো ---
    if state == "wait_movie_lang":
        convo["language"] = text
        convo["state"] = "wait_480p"
        await message.reply_text("✅ ভাষা সেট হয়েছে। এখন **480p** লিংক পাঠান অথবা `skip` লিখুন।")
    elif state == "wait_480p":
        if text.lower() != 'skip': convo["links"]["480p"] = text
        convo["state"] = "wait_720p"
        await message.reply_text("✅ ঠিক আছে। এখন **720p** লিংক পাঠান অথবা `skip` লিখুন।")
    elif state == "wait_720p":
        if text.lower() != 'skip': convo["links"]["720p"] = text
        convo["state"] = "wait_1080p"
        await message.reply_text("✅ ওকে। এখন **1080p** লিংক পাঠান অথবা `skip` লিখুন।")
    elif state == "wait_1080p":
        if text.lower() != 'skip': convo["links"]["1080p"] = text
        msg = await message.reply_text("✅ তথ্য সংগ্রহ সম্পন্ন! পোস্ট তৈরি করা হচ্ছে...", quote=True)
        await generate_final_post_preview(client, uid, message.chat.id, msg)

    # --- ওয়েব সিরিজ পোস্ট ফ্লো ---
    elif state == "wait_tv_lang":
        convo["language"] = text
        convo["state"] = "wait_season_number"
        await message.reply_text("✅ ভাষা সেট হয়েছে। এখন সিজনের নম্বর লিখুন (যেমন: 1, 2 ইত্যাদি)।")
    elif state == "wait_season_number":
        if text.lower() == 'done':
            if not convo.get('seasons'):
                return await message.reply_text("⚠️ আপনি কোনো সিজনের লিংক যোগ করেননি। অন্তত একটি সিজনের নম্বর ও লিংক যোগ করুন।")
            msg = await message.reply_text("✅ সকল সিজনের তথ্য সংগ্রহ সম্পন্ন! পোস্ট তৈরি করা হচ্ছে...", quote=True)
            convo['links'] = convo['seasons'] # ফাইনাল লিংক হিসেবে সিজনগুলো সেট করা
            await generate_final_post_preview(client, uid, message.chat.id, msg)
            return

        if not text.isdigit() or int(text) <= 0:
            return await message.reply_text("❌ ভুল নম্বর। দয়া করে একটি সঠিক সিজন নম্বর লিখুন (যেমন: 1)।")
        
        convo['current_season'] = text
        convo['state'] = 'wait_season_link'
        await message.reply_text(f"👍 ঠিক আছে। এখন **সিজন {text}**-এর ডাউনলোড লিংক পাঠান।")
    
    elif state == "wait_season_link":
        season_num = convo.get('current_season')
        convo['seasons'][season_num] = text
        convo['state'] = 'wait_season_number'
        await message.reply_text(f"✅ সিজন {season_num}-এর লিংক যোগ করা হয়েছে।\n\n**👉 পরবর্তী সিজনের নম্বর লিখুন, অথবা পোস্ট শেষ করতে `done` লিখুন।**")


async def generate_final_post_preview(client, uid, cid, msg):
    convo = user_conversations.get(uid)
    if not convo: return
    
    caption = generate_channel_caption(convo["details"], convo["language"], convo["links"])
    
    watermark_data = db_query("SELECT watermark_text FROM users WHERE user_id=?", (uid,), 'one')
    watermark = watermark_data[0] if watermark_data else None
    poster_url = f"https://image.tmdb.org/t/p/w500{convo['details']['poster_path']}" if convo['details'].get('poster_path') else None
    
    poster, error = watermark_poster(poster_url, watermark)
    
    await msg.delete()
    
    if error:
        await client.send_message(cid, f"⚠️ **পোস্টার তৈরিতে সমস্যা:** `{error}`")

    if poster:
        poster.seek(0)
        preview_msg = await client.send_photo(cid, photo=poster, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
    else:
        preview_msg = await client.send_message(cid, caption, parse_mode=enums.ParseMode.MARKDOWN)

    channel_data = db_query("SELECT channel_id FROM users WHERE user_id=?", (uid,), 'one')
    if channel_data and channel_data[0]:
        await client.send_message(
            cid, "**👆 এটি একটি প্রিভিউ।**\nআপনার চ্যানেলে পোস্ট করবেন?",
            reply_to_message_id=preview_msg.id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 হ্যাঁ, চ্যানেলে পোস্ট করুন", callback_data=f"finalpost_{uid}")]]),
        )
    user_conversations[uid]['final_post'] = {'caption': caption, 'poster': poster}

# ... (বাকি কোড অপরিবর্তিত থাকবে) ...
@bot.on_callback_query(filters.regex("^finalpost_"))
async def post_to_channel_cb(client, cb: Message):
    uid = int(cb.data.split("_")[1])
    if cb.from_user.id != uid: return await cb.answer("This is not for you!", show_alert=True)
    
    convo = user_conversations.get(uid)
    channel_data = db_query("SELECT channel_id FROM users WHERE user_id=?", (uid,), 'one')
    if not convo or not channel_data or not channel_data[0]:
        return await cb.message.edit("❌ সেশন বা চ্যানেল আইডি পাওয়া যায়নি।")
    channel_id, post_data = int(channel_data[0]), convo['final_post']
    
    try:
        if post_data['poster']: post_data['poster'].seek(0); await client.send_photo(channel_id, photo=post_data['poster'], caption=post_data['caption'], parse_mode=enums.ParseMode.MARKDOWN)
        else: await client.send_message(channel_id, post_data['caption'], parse_mode=enums.ParseMode.MARKDOWN)
        
        await cb.message.delete()
        if cb.message.reply_to_message: await cb.message.reply_to_message.delete()
        await client.send_message(cb.from_user.id, f"✅ সফলভাবে `{channel_id}`-এ পোস্ট করা হয়েছে!")
    except Exception as e:
        await cb.message.edit(f"❌ চ্যানেলে পোস্ট করতে সমস্যা হয়েছে: {e}")
    finally:
        if uid in user_conversations: del user_conversations[uid]

@bot.on_message(filters.command("testpost") & filters.private)
@force_subscribe
async def test_post_command(client, message: Message):
    uid = message.from_user.id
    processing_msg = await message.reply_text("⏳ আপনার সেট করা চ্যানেলে একটি পরীক্ষামূলক বার্তা পাঠানোর চেষ্টা করা হচ্ছে...")
    channel_data = db_query("SELECT channel_id FROM users WHERE user_id=?", (uid,), 'one')
    if not channel_data or not channel_data[0]:
        return await processing_msg.edit("❌ আপনার কোনো চ্যানেল সেট করা নেই। প্রথমে `/setchannel <ID>` ব্যবহার করুন।")
    channel_id_str = channel_data[0]
    try: channel_id = int(channel_id_str)
    except ValueError: return await processing_msg.edit(f"❌ চ্যানেল আইডি `{channel_id_str}` সঠিক নয়।")
    try:
        await client.send_message(chat_id=channel_id, text="✅ এটি বট থেকে পাঠানো একটি পরীক্ষামূলক বার্তা। যদি এই বার্তাটি দেখতে পান, তার মানে সবকিছু ঠিকভাবে কাজ করছে।")
        await processing_msg.edit(f"✅ সফলভাবে `{channel_id}` চ্যানেলে পরীক্ষামূলক বার্তা পাঠানো হয়েছে!")
    except Exception as e:
        error_message = (f"❌ চ্যানেলে বার্তা পাঠাতে ব্যর্থ!\n\n**ত্রুটি:**\n`{e}`")
        await processing_msg.edit(error_message)

@bot.on_message(filters.command(["setwatermark", "setchannel", "cancel"]))
@force_subscribe
async def settings_commands(client, message: Message):
    command, uid = message.command[0].lower(), message.from_user.id
    if command == "setwatermark":
        text = " ".join(message.command[1:]) if len(message.command[1:]) > 0 else None
        db_query("INSERT INTO users (user_id, watermark_text) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET watermark_text = excluded.watermark_text", (uid, text))
        await message.reply_text(f"✅ ওয়াটারমার্ক {'সেট হয়েছে: `' + text + '`' if text else 'মুছে ফেলা হয়েছে।'}")
    elif command == "setchannel":
        if len(message.command) > 1 and message.command[1].startswith("-100") and message.command[1][1:].isdigit():
            cid = message.command[1]
            db_query("INSERT INTO users (user_id, channel_id) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET channel_id = excluded.channel_id", (uid, cid))
            await message.reply_text(f"✅ চ্যানেল সেট হয়েছে: `{cid}`")
        else: await message.reply_text("⚠️ অবৈধ চ্যানেল আইডি। আইডি অবশ্যই `-100` দিয়ে শুরু হতে হবে।")
    elif command == "cancel":
        if uid in user_conversations: del user_conversations[uid]; await message.reply_text("✅ প্রক্রিয়া বাতিল করা হয়েছে।")
        else: await message.reply_text("🚫 বাতিল করার মতো কোনো প্রক্রিয়া চালু নেই।")

# ---- 5. START THE BOT ----
if __name__ == "__main__":
    print("🚀 Bot is starting...")
    bot.run()
    print("👋 Bot has stopped.")
