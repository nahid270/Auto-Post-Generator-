# -*- coding: utf-8 -*-

# ---- Core Python Imports ----
import os
import io
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
INVITE_LINK = os.getenv("INVITE_LINK")

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

# ---- Flask App & Font Config ----
app = Flask(__name__)
@app.route('/')
def home(): return "✅ 100% Final Bot is Running!"
Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

try:
    FONT_BOLD = ImageFont.truetype("Poppins-Bold.ttf", 32)
    FONT_REGULAR = ImageFont.truetype("Poppins-Regular.ttf", 24)
    FONT_SMALL = ImageFont.truetype("Poppins-Regular.ttf", 18)
    FONT_WATERMARK = ImageFont.truetype("Poppins-Bold.ttf", 22)
except IOError: FONT_BOLD = FONT_REGULAR = FONT_SMALL = FONT_WATERMARK = ImageFont.load_default()

# ---- 2. DECORATORS AND HELPER FUNCTIONS ----
def force_subscribe(func):
    async def wrapper(client, message):
        if FORCE_SUB_CHANNEL:
            try:
                chat_id = int(FORCE_SUB_CHANNEL) if FORCE_SUB_CHANNEL.startswith("-100") else FORCE_SUB_CHANNEL
                await client.get_chat_member(chat_id, message.from_user.id)
            except UserNotParticipant:
                join_link = INVITE_LINK or f"https://t.me/{FORCE_SUB_CHANNEL.replace('@', '')}"
                return await message.reply_text(
                    "❗ **Join Our Channel to Use Me**",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👉 Join Channel", url=join_link)]]),
                    parse_mode=enums.ParseMode.MARKDOWN
                )
        await func(client, message)
    return wrapper

# ---- 3. TMDB API & CONTENT GENERATION ----
def search_tmdb(query: str):
    year, name = None, query.strip()
    match = re.search(r'(.+?)\s*\(?(\d{4})\)?$', query)
    if match: name, year = match.group(1).strip(), match.group(2)
    url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={name}" + (f"&year={year}" if year else "")
    try:
        r = requests.get(url, timeout=10); r.raise_for_status()
        return [res for res in r.json().get("results", []) if res.get("media_type") in ["movie", "tv"]][:5]
    except: return []

def get_tmdb_details(media_type: str, media_id: int):
    url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}&append_to_response=credits,videos"
    try:
        r = requests.get(url, timeout=10); r.raise_for_status(); return r.json()
    except: return None

def watermark_poster(poster_url: str, watermark_text: str):
    if not poster_url: return None
    try:
        img_data = requests.get(poster_url, timeout=10).content
        img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        if watermark_text:
            draw = ImageDraw.Draw(img)
            bbox = draw.textbbox((0, 0), watermark_text, font=FONT_WATERMARK)
            text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x, y = img.width - text_width - 15, img.height - text_height - 15
            draw.text((x, y), watermark_text, font=FONT_WATERMARK, fill=(255, 255, 255, 150))
        buffer = io.BytesIO(); buffer.name = "poster.png"; img.save(buffer, "PNG"); buffer.seek(0)
        return buffer
    except Exception as e:
        print(f"Image Error: {e}")
        return None

def generate_channel_caption(data: dict, language: str, links: dict):
    title = data.get("title") or data.get("name") or "N/A"
    year = (data.get("release_date") or data.get("first_air_date") or "----")[:4]
    genres = ", ".join([g["name"] for g in data.get("genres", [])[:2]])
    caption = f"**{title} ({year})**\n\n🎭 **Genres:** {genres}\n🔊 **Language:** {language}\n\n📥 **Download Links** 👇\n"
    if links.get("480p"): caption += f"🔹 **480p:** [Link Here]({links['480p']})\n"
    if links.get("720p"): caption += f"🔹 **720p:** [Link Here]({links['720p']})\n"
    if links.get("1080p"): caption += f"🔹 **1080p:** [Link Here]({links['1080p']})\n"
    return caption

def generate_html(data: dict, all_links: list):
    title = data.get("title") or data.get("name") or "N/A"
    year = (data.get("release_date") or data.get("first_air_date") or "----")[:4]
    overview = data.get("overview", "No overview available.")
    genres = ", ".join([g["name"] for g in data.get("genres", [])] or ["N/A"])
    poster = f"https://image.tmdb.org/t/p/w500{data['poster_path']}" if data.get('poster_path') else ""
    backdrop = f"https://image.tmdb.org/t/p/original{data['backdrop_path']}" if data.get('backdrop_path') else ""
    trailer_key = next((v['key'] for v in data.get('videos', {}).get('results', []) if v['site'] == 'YouTube'), None)
    trailer_button = f'<a href="https://www.youtube.com/watch?v={trailer_key}" target="_blank" class="trailer-button">🎬 Watch Trailer</a>' if trailer_key else ""
    download_buttons = "".join([f'<a href="{link["url"]}" target="_blank">🔽 {link["label"]}</a>' for link in all_links])
    return f"""<style>.movie-card-container{{max-width:700px;margin:20px auto;background:#1c1c1c;border-radius:20px;padding:20px;color:#e0e0e0;font-family:sans-serif;}}.movie-content{{display:flex;flex-wrap:wrap;}}.movie-poster-container{{flex:1 1 200px;margin-right:20px;}}.movie-poster-container img{{width:100%;border-radius:15px;}}.movie-details{{flex:2 1 300px;}}.movie-details b{{color:#00e676;}}.backdrop-container img{{max-width:100%;border-radius:15px;margin-top:20px;}}.action-buttons{{text-align:center;margin-top:20px;}}.action-buttons a{{display:inline-block;background:linear-gradient(45deg,#ff512f,#dd2476);color:white!important;padding:12px 25px;margin:8px;border-radius:25px;text-decoration:none;}}.action-buttons .trailer-button{{background:#c4302b}}</style><div class="movie-card-container"><h2>{title} ({year})</h2><div class="movie-content"><div class="movie-poster-container"><img src="{poster}" alt="{title}"/></div><div class="movie-details"><p><b>Genre:</b> {genres}</p><p><b>Rating:</b> ⭐ {round(data.get('vote_average',0),1)}/10</p><p><b>Overview:</b> {overview}</p></div></div><div class="backdrop-container"><a href="{backdrop}" target="_blank"><img src="{backdrop}" alt="{title}"/></a></div><div class="action-buttons">{trailer_button}{download_buttons or ""}</div></div>"""

# ---- 4. BOT HANDLERS ----
@bot.on_message(filters.command("start") & filters.private)
@force_subscribe
async def start_cmd(client, message: Message):
    db_query("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
    await message.reply_text(
        text="👋 **Welcome! I create posts for Blogs and Channels.**\n\n"
             "**Choose a command:**\n"
             "🔹 `/blogger <name>` - To generate HTML for Blogger.\n"
             "🔹 `/channelpost <name>` - To generate a post for Telegram.\n\n"
             "Use `/setwatermark` and `/setchannel` to configure.",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@bot.on_message(filters.command(["blogger", "channelpost"]) & filters.private)
@force_subscribe
async def search_commands(client, message: Message):
    command = message.command[0].lower()
    if len(message.command) == 1:
        return await message.reply_text(f"**Usage:** `/{command} Movie Name`", parse_mode=enums.ParseMode.MARKDOWN)
    
    query = " ".join(message.command[1:])
    processing_msg = await message.reply_text(f"🔍 Searching for `{query}`...", parse_mode=enums.ParseMode.MARKDOWN)
    results = search_tmdb(query)
    if not results: return await processing_msg.edit_text("❌ No content found.")
    
    buttons = [[InlineKeyboardButton(
        f"{'🎬' if r['media_type'] == 'movie' else '📺'} {r.get('title') or r.get('name')} ({(r.get('release_date') or r.get('first_air_date') or '----').split('-')[0]})",
        callback_data=f"select_{command}_{r['media_type']}_{r['id']}"
    )] for r in results]
    await processing_msg.edit_text("**👇 Choose from results:**", reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.MARKDOWN)

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
        user_conversations[uid]["state"] = "wait_blogger_link_label"
        await cb.message.edit_text("**Blogger Post: Add Links**\nSend the button text for the first link.")
    elif flow == "channelpost":
        user_conversations[uid]["state"] = "wait_channel_lang"
        await cb.message.edit_text("**Channel Post: Language**\nEnter the language for this post.")

@bot.on_message(filters.text & filters.private & ~filters.command(["start", "blogger", "channelpost", "setwatermark", "setchannel", "cancel"]))
@force_subscribe
async def conversation_handler(client, message: Message):
    uid, convo = message.from_user.id, user_conversations.get(message.from_user.id)
    if not convo: return
    state, text, flow = convo.get("state"), message.text.strip(), convo.get("flow")

    if flow == "blogger":
        if state == "wait_blogger_link_label":
            convo["current_label"] = text; convo["state"] = "wait_blogger_link_url"
            await message.reply_text(f"OK, now send the URL for **'{text}'**.", parse_mode=enums.ParseMode.MARKDOWN)
        elif state == "wait_blogger_link_url":
            if not text.startswith("http"): return await message.reply_text("⚠️ Invalid URL.")
            convo["links"].append({"label": convo["current_label"], "url": text}); del convo["current_label"]
            buttons = [[InlineKeyboardButton("✅ Add another", callback_data=f"addbloggerlink_{uid}")],
                       [InlineKeyboardButton("✅ Done", callback_data=f"doneblogger_{uid}")]]
            await message.reply_text("Link added! Add another?", reply_markup=InlineKeyboardMarkup(buttons))
    elif flow == "channelpost":
        if state == "wait_channel_lang":
            convo["language"] = text; convo["state"] = "wait_480p"
            await message.reply_text("✅ Language set. Now, send **480p** link or `skip`.")
        elif state == "wait_480p":
            if text.lower() != 'skip': convo["fixed_links"]["480p"] = text
            convo["state"] = "wait_720p"
            await message.reply_text("✅ Got it. Now, send **720p** link or `skip`.")
        elif state == "wait_720p":
            if text.lower() != 'skip': convo["fixed_links"]["720p"] = text
            convo["state"] = "wait_1080p"
            await message.reply_text("✅ Okay. Now, send **1080p** link or `skip`.")
        elif state == "wait_1080p":
            if text.lower() != 'skip': convo["fixed_links"]["1080p"] = text
            msg = await message.reply_text("✅ All info collected! Generating channel post...", quote=True)
            await generate_channel_post(client, uid, message.chat.id, msg)

@bot.on_callback_query(filters.regex("^(addbloggerlink|doneblogger)_"))
async def blogger_link_cb(client, cb: Message):
    action, uid_str = cb.data.split("_", 1); uid = int(uid_str)
    if cb.from_user.id != uid: return await cb.answer("Not for you!", show_alert=True)
    convo = user_conversations.get(uid)
    if not convo: return await cb.answer("Session expired.", show_alert=True)
    if action == "addbloggerlink":
        convo["state"] = "wait_blogger_link_label"
        await cb.message.edit_text("OK, send the button text for the next link.")
    else:
        msg = await cb.message.edit_text("✅ Generating Blogger post...")
        await generate_blogger_post(client, uid, cb.message.chat.id, msg)

async def generate_blogger_post(client, uid, cid, msg):
    convo = user_conversations.get(uid)
    if not convo: return
    html = generate_html(convo["details"], convo["links"])
    convo["generated_html"] = html; convo["state"] = "done"
    if hasattr(msg, 'delete'): await msg.delete()
    await client.send_message(cid, f"✅ **Blogger Post Generated!**", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("📝 Get HTML Code", callback_data=f"get_html_{uid}")]]), parse_mode=enums.ParseMode.MARKDOWN)

async def generate_channel_post(client, uid, cid, msg):
    convo = user_conversations.get(uid)
    if not convo: return
    
    await msg.edit_text("🎨 Downloading poster & applying watermark...")
    
    watermark_data = db_query("SELECT watermark_text FROM users WHERE user_id=?", (uid,), 'one')
    watermark = watermark_data[0] if watermark_data else None
    poster_url = f"https://image.tmdb.org/t/p/w500{convo['details']['poster_path']}" if convo['details'].get('poster_path') else None
    
    caption = generate_channel_caption(convo["details"], convo["language"], convo["fixed_links"])
    watermarked_poster = watermark_poster(poster_url, watermark)
    
    convo["generated_poster"] = watermarked_poster
    convo["generated_channel_post"] = {"caption": caption}
    convo["state"] = "done"
    
    channel_data = db_query("SELECT channel_id FROM users WHERE user_id=?", (uid,), 'one')
    channel_id = channel_data[0] if channel_data and channel_data[0] else None
    
    if hasattr(msg, 'delete'): await msg.delete()

    if watermarked_poster:
        watermarked_poster.seek(0)
        await client.send_photo(cid, photo=watermarked_poster, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
    else:
        await client.send_message(cid, "⚠️ **Warning:** Could not generate poster. Sending text-only preview.", parse_mode=enums.ParseMode.MARKDOWN)
        await client.send_message(cid, caption, parse_mode=enums.ParseMode.MARKDOWN)
        
    if channel_id:
        await client.send_message(cid, "**👆 This is a preview.**\nPost to your channel?",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Yes, Post to Channel", callback_data=f"post_channel_{uid}")]]),
                                  parse_mode=enums.ParseMode.MARKDOWN)

@bot.on_callback_query(filters.regex("^(get_html|post_channel)_"))
async def final_action_cb(client, cb: Message):
    try: action, uid_str = cb.data.rsplit("_", 1); uid = int(uid_str)
    except: return await cb.answer("Error.", show_alert=True)
    if cb.from_user.id != uid: return await cb.answer("Not for you!", show_alert=True)
    convo = user_conversations.get(uid)
    if not convo or convo.get("state") != "done": return await cb.answer("Session expired.", show_alert=True)

    if action == "get_html":
        html = convo.get("generated_html")
        if not html: return await cb.answer("HTML not generated.", show_alert=True)
        await cb.answer()
        if len(html) > 4000:
            title = (convo["details"].get("title") or "post").replace(" ", "_")
            await client.send_document(cb.message.chat.id, document=io.BytesIO(html.encode('utf-8')), file_name=f"{title}.html")
        else:
            await client.send_message(cb.message.chat.id, f"```html\n{html}\n```", parse_mode=enums.ParseMode.MARKDOWN)

    elif action == "post_channel":
        post_data = convo.get("generated_channel_post")
        if not post_data: return await cb.answer("Channel post not generated.", show_alert=True)
        channel_data = db_query("SELECT channel_id FROM users WHERE user_id=?", (uid,), 'one')
        channel_id = channel_data[0] if channel_data and channel_data[0] else None
        if not channel_id: return await cb.answer("Channel not set.", show_alert=True)
        await cb.answer("Posting...", show_alert=False)
        try:
            poster = convo.get("generated_poster")
            caption = post_data["caption"]
            chat_id_int = int(channel_id) if channel_id.startswith("-100") else channel_id
            if poster:
                poster.seek(0)
                await client.send_photo(chat_id_int, photo=poster, caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
            else:
                await client.send_message(chat_id_int, caption, parse_mode=enums.ParseMode.MARKDOWN)
            if cb.message.reply_markup: await cb.message.delete()
            await client.send_message(cb.from_user.id, f"✅ Successfully posted to `{channel_id}`!", parse_mode=enums.ParseMode.MARKDOWN)
        except Exception as e:
            await cb.message.edit_text(f"❌ Failed to post. Error: {e}")

@bot.on_message(filters.command(["setwatermark", "setchannel", "cancel"]))
@force_subscribe
async def other_commands(client, message: Message):
    command = message.command[0].lower()
    uid = message.from_user.id
    if command == "setwatermark":
        text = " ".join(message.command[1:]) if len(message.command) > 1 else None
        db_query("UPDATE users SET watermark_text = ? WHERE user_id = ?", (text, uid))
        await message.reply_text(f"✅ Watermark {'set to: `' + text + '`' if text else 'removed.'}", parse_mode=enums.ParseMode.MARKDOWN)
    elif command == "setchannel":
        cid = message.command[1] if len(message.command) > 1 else None
        db_query("UPDATE users SET channel_id = ? WHERE user_id = ?", (cid, uid))
        await message.reply_text(f"✅ Channel {'set to: `' + cid + '`' if cid else 'removed.'}", parse_mode=enums.ParseMode.MARKDOWN)
    elif command == "cancel":
        if uid in user_conversations:
            del user_conversations[uid]
            await message.reply_text("✅ Operation cancelled.")

# ---- 5. START THE BOT ----
if __name__ == "__main__":
    print("🚀 Bot is starting... (100% Final, Bug-Fixed Version 2.0)")
    bot.run()
    print("👋 Bot has stopped.")
