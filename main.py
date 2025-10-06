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

# ---- 3. TMDB API & CONTENT GENERATION ----
def search_tmdb(query: str):
    year, name = None, query.strip()
    match = re.search(r'(.+?)\s*\(?(\d{4})\)?$', query)
    if match: name, year = match.group(1).strip(), match.group(2)
    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={name}" + (f"&year={year}" if year else "")
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return [res for res in r.json().get("results", []) if res.get("media_type") in ["movie", "tv"]][:5]
    except Exception as e:
        print(f"TMDB Search Error: {e}")
        return []

def get_tmdb_details(media_type: str, media_id: int):
    url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}&append_to_response=credits,videos"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"TMDB Details Error: {e}")
        return None

def watermark_poster(poster_url: str, watermark_text: str):
    if not poster_url:
        return None, "Poster URL not found."
    try:
        img_data = requests.get(poster_url, timeout=20).content
        img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        if watermark_text:
            draw = ImageDraw.Draw(img)
            try:
                # Make sure 'Poppins-Bold.ttf' is in the same folder as your script
                font = ImageFont.truetype("Poppins-Bold.ttf", 25)
            except IOError:
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), watermark_text, font=font)
            text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x, y = img.width - text_width - 20, img.height - text_height - 20
            # Shadow for better visibility
            draw.text((x+1, y+1), watermark_text, font=font, fill=(0, 0, 0, 128))
            draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255, 220))
        buffer = io.BytesIO()
        buffer.name = "poster.png"
        img.save(buffer, "PNG")
        buffer.seek(0)
        return buffer, None
    except requests.exceptions.RequestException as e:
        return None, f"Network Error downloading poster: {e}"
    except Exception as e:
        return None, f"Image processing error. Is 'Poppins-Bold.ttf' missing? Error: {e}"

def generate_channel_caption(data: dict, language: str, links: dict):
    title = data.get("title") or data.get("name") or "N/A"
    year = (data.get("release_date") or data.get("first_air_date") or "----")[:4]
    genres = ", ".join([g["name"] for g in data.get("genres", [])[:2]])
    caption = f"🎬 **{title} ({year})**\n\n🎭 **ধরন:** {genres}\n🔊 **ভাষা:** {language}\n\n📥 **ডাউনলোড লিংক** 👇\n"
    if links.get("480p"): caption += f"🔹 **480p:** [ডাউনলোড করুন]({links['480p']})\n"
    if links.get("720p"): caption += f"🔹 **720p:** [ডাউনলোড করুন]({links['720p']})\n"
    if links.get("1080p"): caption += f"🔹 **1080p:** [ডাউনলোড করুন]({links['1080p']})\n"
    return caption

# Other helper functions like generate_html can be added here if needed for /blogger

# ---- 4. BOT HANDLERS ----
@bot.on_message(filters.command("start") & filters.private)
@force_subscribe
async def start_cmd(client, message: Message):
    db_query("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
    await message.reply_text(
        "👋 **স্বাগতম! আমি মুভি পোস্টার বট।**\n\n"
        "**আমার কমান্ডগুলো হলো:**\n"
        "🔹 `/post <name>` - ধাপে ধাপে বিস্তারিত পোস্ট তৈরি করুন।\n"
        "🔹 `/quickpost <name>` - দ্রুত একটি পোস্ট তৈরি করুন।\n"
        "🔹 `/blogger <name>` - ব্লগার পোস্টের জন্য HTML কোড তৈরি করুন।\n\n"
        "**সেটিংস:**\n"
        "🔹 `/setchannel <ID>` - পোস্ট করার জন্য আপনার চ্যানেল সেট করুন।\n"
        "🔹 `/setwatermark <text>` - পোস্টারে আপনার ওয়াটারমার্ক সেট করুন।\n"
        "🔹 `/cancel` - যেকোনো চলমান প্রক্রিয়া বাতিল করুন।",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@bot.on_message(filters.command(["post", "blogger"]) & filters.private)
@force_subscribe
async def search_commands(client, message: Message):
    command = message.command[0].lower()
    if len(message.command) == 1:
        return await message.reply_text(f"**ব্যবহার:** `/{command} Movie Name`", parse_mode=enums.ParseMode.MARKDOWN)
    
    query = " ".join(message.command[1:])
    processing_msg = await message.reply_text(f"🔍 `{query}`-এর জন্য খোঁজা হচ্ছে...", parse_mode=enums.ParseMode.MARKDOWN)
    results = search_tmdb(query)
    if not results:
        return await processing_msg.edit_text("❌ কোনো ফলাফল পাওয়া যায়নি।")
    
    buttons = [[InlineKeyboardButton(
        f"{'🎬' if r['media_type'] == 'movie' else '📺'} {r.get('title') or r.get('name')} ({(r.get('release_date') or r.get('first_air_date') or '----').split('-')[0]})",
        callback_data=f"select_{command}_{r['media_type']}_{r['id']}"
    )] for r in results]
    await processing_msg.edit_text("**👇 ফলাফল থেকে বেছে নিন:**", reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_message(filters.command("quickpost") & filters.private)
@force_subscribe
async def quick_post_search(client, message: Message):
    if len(message.command) == 1:
        return await message.reply_text("**ব্যবহার:** `/quickpost Movie Name`", parse_mode=enums.ParseMode.MARKDOWN)

    user_settings = db_query("SELECT channel_id FROM users WHERE user_id = ?", (message.from_user.id,), 'one')
    if not user_settings or not user_settings[0]:
        return await message.reply_text("⚠️ দ্রুত পোস্টের জন্য প্রথমে আপনার চ্যানেল সেট করুন।\nব্যবহার করুন: `/setchannel <channel_id>`")

    query = " ".join(message.command[1:])
    processing_msg = await message.reply_text(f"🔍 `{query}`-এর জন্য খোঁজা হচ্ছে...", parse_mode=enums.ParseMode.MARKDOWN)
    results = search_tmdb(query)
    if not results:
        return await processing_msg.edit_text("❌ কোনো ফলাফল পাওয়া যায়নি।")

    buttons = [[InlineKeyboardButton(
        f"{'🎬' if r['media_type'] == 'movie' else '📺'} {r.get('title') or r.get('name')} ({(r.get('release_date') or r.get('first_air_date') or '----').split('-')[0]})",
        callback_data=f"qpost_{r['media_type']}_{r['id']}"
    )] for r in results]
    await processing_msg.edit_text("**👇 দ্রুত পোস্টের জন্য বেছে নিন:**", reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_callback_query(filters.regex("^qpost_"))
async def quick_post_select(client, cb: Message):
    await cb.message.edit("⏳ পোস্ট তৈরি করা হচ্ছে...")
    try:
        _, media_type, media_id = cb.data.split("_", 2)
        media_id = int(media_id)
    except:
        return await cb.message.edit("❌ অবৈধ অনুরোধ।")

    details = get_tmdb_details(media_type, media_id)
    if not details:
        return await cb.message.edit("❌ বিস্তারিত তথ্য পাওয়া যায়নি।")

    user_settings = db_query("SELECT channel_id, watermark_text FROM users WHERE user_id = ?", (cb.from_user.id,), 'one')
    channel_id, watermark_text = user_settings

    title = details.get("title") or details.get("name")
    year = (details.get("release_date") or details.get("first_air_date") or "----")[:4]
    rating = round(details.get("vote_average", 0), 1)
    caption = f"🎬 **{title} ({year})**\n⭐️ **Rating:** {rating}/10"

    poster_url = f"https://image.tmdb.org/t/p/w500{details['poster_path']}" if details.get('poster_path') else None
    poster, error = watermark_poster(poster_url, watermark_text)
    
    if error:
        await client.send_message(cb.from_user.id, f"⚠️ **পোস্টার তৈরিতে সমস্যা:** `{error}`")

    try:
        if poster:
            poster.seek(0)
            await client.send_photo(int(channel_id), photo=poster, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await client.send_message(int(channel_id), caption, parse_mode=enums.ParseMode.MARKDOWN)
        
        await cb.message.edit(f"✅ **'{title}'** সফলভাবে `{channel_id}`-এ পোস্ট করা হয়েছে!")
    except Exception as e:
        await cb.message.edit(f"❌ চ্যানেলে পোস্ট করতে সমস্যা হয়েছে।\n**ত্রুটি:** `{e}`")

@bot.on_callback_query(filters.regex("^select_"))
async def selection_cb(client, cb: Message):
    await cb.answer("Fetching details...")
    try: _, flow, media_type, mid = cb.data.split("_", 3)
    except: return await cb.message.edit_text("Invalid callback.")
    details = get_tmdb_details(media_type, int(mid))
    if not details: return await cb.message.edit_text("❌ Failed to get details.")
    uid = cb.from_user.id
    user_conversations[uid] = {"flow": flow, "details": details, "links": [], "fixed_links": {}, "state": ""}
    if flow == "blogger":
        await cb.message.edit("Blogger flow is not fully implemented in this version yet.") # Placeholder
    elif flow == "post":
        user_conversations[uid]["state"] = "wait_channel_lang"
        await cb.message.edit_text("**চ্যানেল পোস্ট:** পোস্টের জন্য ভাষা লিখুন।")

# ---- CORRECTED LINE IS HERE ----
@bot.on_message(filters.text & filters.private & ~filters.command(["start", "post", "blogger", "quickpost", "setwatermark", "setchannel", "cancel"]))
@force_subscribe
async def conversation_handler(client, message: Message):
    uid, convo = message.from_user.id, user_conversations.get(message.from_user.id)
    if not convo or "state" not in convo: return
    
    state, text = convo["state"], message.text.strip()

    if state == "wait_channel_lang":
        convo["language"] = text; convo["state"] = "wait_480p"
        await message.reply_text("✅ ভাষা সেট হয়েছে। এখন **480p** লিংক পাঠান অথবা `skip` লিখুন।")
    elif state == "wait_480p":
        if text.lower() != 'skip': convo["fixed_links"]["480p"] = text
        convo["state"] = "wait_720p"
        await message.reply_text("✅ ঠিক আছে। এখন **720p** লিংক পাঠান অথবা `skip` লিখুন।")
    elif state == "wait_720p":
        if text.lower() != 'skip': convo["fixed_links"]["720p"] = text
        convo["state"] = "wait_1080p"
        await message.reply_text("✅ ওকে। এখন **1080p** লিংক পাঠান অথবা `skip` লিখুন।")
    elif state == "wait_1080p":
        if text.lower() != 'skip': convo["fixed_links"]["1080p"] = text
        msg = await message.reply_text("✅ তথ্য সংগ্রহ সম্পন্ন! চ্যানেল পোস্ট তৈরি করা হচ্ছে...", quote=True)
        await generate_final_post_preview(client, uid, message.chat.id, msg)

async def generate_final_post_preview(client, uid, cid, msg):
    convo = user_conversations.get(uid)
    if not convo: return
    
    caption = generate_channel_caption(convo["details"], convo["language"], convo["fixed_links"])
    
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

@bot.on_callback_query(filters.regex("^finalpost_"))
async def post_to_channel_cb(client, cb: Message):
    uid = int(cb.data.split("_")[1])
    if cb.from_user.id != uid: return await cb.answer("This is not for you!", show_alert=True)
    
    convo = user_conversations.get(uid)
    channel_data = db_query("SELECT channel_id FROM users WHERE user_id=?", (uid,), 'one')
    
    if not convo or not channel_data or not channel_data[0]:
        return await cb.message.edit("❌ সেশন বা চ্যানেল আইডি পাওয়া যায়নি।")

    channel_id = int(channel_data[0])
    post_data = convo['final_post']
    
    try:
        if post_data['poster']:
            post_data['poster'].seek(0)
            await client.send_photo(channel_id, photo=post_data['poster'], caption=post_data['caption'], parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await client.send_message(channel_id, post_data['caption'], parse_mode=enums.ParseMode.MARKDOWN)
        
        await cb.message.delete()
        if cb.message.reply_to_message:
            await cb.message.reply_to_message.delete()
        await client.send_message(cb.from_user.id, f"✅ সফলভাবে `{channel_id}`-এ পোস্ট করা হয়েছে!")
    except Exception as e:
        await cb.message.edit(f"❌ চ্যানেলে পোস্ট করতে সমস্যা হয়েছে: {e}")
    finally:
        if uid in user_conversations:
            del user_conversations[uid]

@bot.on_message(filters.command(["setwatermark", "setchannel", "cancel"]))
@force_subscribe
async def settings_commands(client, message: Message):
    command, uid = message.command[0].lower(), message.from_user.id
    if command == "setwatermark":
        text = " ".join(message.command[1:]) if len(message.command) > 1 else None
        db_query("UPDATE users SET watermark_text = ? WHERE user_id = ?", (text, uid))
        await message.reply_text(f"✅ ওয়াটারমার্ক {'সেট হয়েছে: `' + text + '`' if text else 'মুছে ফেলা হয়েছে।'}", parse_mode=enums.ParseMode.MARKDOWN)
    elif command == "setchannel":
        if len(message.command) > 1 and message.command[1].startswith("-100") and message.command[1][1:].isdigit():
            cid = message.command[1]
            db_query("UPDATE users SET channel_id = ? WHERE user_id = ?", (cid, uid))
            await message.reply_text(f"✅ চ্যানেল সেট হয়েছে: `{cid}`", parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await message.reply_text("⚠️ অবৈধ চ্যানেল আইডি। আইডি অবশ্যই `-100` দিয়ে শুরু হতে হবে।")
    elif command == "cancel":
        if uid in user_conversations:
            del user_conversations[uid]
            await message.reply_text("✅ প্রক্রিয়া বাতিল করা হয়েছে।")
        else:
            await message.reply_text("🚫 বাতিল করার মতো কোনো প্রক্রিয়া চালু নেই।")

# ---- 5. START THE BOT ----
if __name__ == "__main__":
    print("🚀 Bot is starting...")
    bot.run()
    print("👋 Bot has stopped.")
