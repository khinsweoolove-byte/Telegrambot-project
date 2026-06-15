import os
import asyncio
import threading
import logging
import sys
import secrets
import re
from datetime import datetime
from flask import Flask, request
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters, CallbackQueryHandler
)
from telegram.helpers import create_deep_linked_url
from pymongo import MongoClient
from telegraph import Telegraph
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running", 200

# ---------- MongoDB ----------
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI not set")
    sys.exit(1)

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    logger.info("MongoDB connected successfully")
except Exception as e:
    logger.warning(f"MongoDB connection failed: {e}")
    mongo_client = MongoClient(MONGO_URI, tlsAllowInvalidCertificates=True, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    logger.info("MongoDB connected (SSL bypassed)")

db = mongo_client["movie_bot_v3"]
file_store_collection = db["file_store"]
users_collection = db["users"]
stats_collection = db["stats"]
blocked_collection = db["blocked_users"]

def init_stats():
    if stats_collection.count_documents({"_id": "total_requests"}) == 0:
        stats_collection.insert_one({"_id": "total_requests", "count": 0})
init_stats()

def get_total_requests():
    doc = stats_collection.find_one({"_id": "total_requests"})
    return doc["count"] if doc else 0

def increment_requests():
    stats_collection.update_one({"_id": "total_requests"}, {"$inc": {"count": 1}}, upsert=True)

def add_user(user_id):
    if not users_collection.find_one({"user_id": user_id}):
        users_collection.insert_one({"user_id": user_id, "first_seen": datetime.now(), "attempts": 0})

def get_all_users():
    return [doc["user_id"] for doc in users_collection.find({}, {"user_id": 1})]

def save_file_info(payload, file_id, file_name):
    file_store_collection.update_one(
        {"payload": payload},
        {"$set": {"file_id": file_id, "file_name": file_name}},
        upsert=True
    )
    logger.info(f"Saved: {payload} -> {file_name}")

def get_file_info(payload):
    doc = file_store_collection.find_one({"payload": payload})
    if doc:
        return {"file_id": doc["file_id"], "file_name": doc["file_name"]}
    return None

def is_user_blocked(user_id):
    return blocked_collection.find_one({"user_id": user_id}) is not None

def block_user(user_id):
    if not is_user_blocked(user_id):
        blocked_collection.insert_one({"user_id": user_id, "blocked_at": datetime.now()})

def unblock_user(user_id):
    blocked_collection.delete_one({"user_id": user_id})

def get_blocked_users():
    return [doc["user_id"] for doc in blocked_collection.find({}, {"user_id": 1})]

def get_attempt_count(user_id):
    doc = users_collection.find_one({"user_id": user_id})
    return doc.get("attempts", 0) if doc else 0

def increment_attempts(user_id):
    users_collection.update_one({"user_id": user_id}, {"$inc": {"attempts": 1}}, upsert=True)

def reset_attempts(user_id):
    users_collection.update_one({"user_id": user_id}, {"$set": {"attempts": 0}}, upsert=True)

# ---------- Telegram Config ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    logger.error("TELEGRAM_TOKEN not set")
    sys.exit(1)

BOT_USERNAME = os.environ.get("BOT_USERNAME")
if not BOT_USERNAME:
    logger.error("BOT_USERNAME not set")
    sys.exit(1)

ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_ID", "").split(",") if x.strip()]

REQUIRED_CHANNELS = [
    {"id": "-1003753299714", "name": "🎬 ဇာတ်ကားချန်နယ် (ပင်မ)", "invite": "https://t.me/wznmoviescollector"},
    {"id": "-1003899625672", "name": "🎬 ဇာတ်ကားချန်နယ် (အရံ)", "invite": "https://t.me/moviesandseriesforallwzn"},
    {"id": "-1003792838735", "name": "🔞 လူကြီးများအတွက် သီးသန့်ချန်နယ်", "invite": "https://t.me/everyboyhobby"},
    {"id": "-1003785717514", "name": "🎵 မြန်မာသီချင်းချန်နယ်", "invite": "https://t.me/wznmusiclibary"}
]

def is_admin(user_id):
    return user_id in ADMIN_IDS

def generate_payload():
    return secrets.token_urlsafe(16)

async def is_member_of_channel(user_id, channel_id, bot):
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def check_all_channels(user_id, bot):
    missing = []
    for ch in REQUIRED_CHANNELS:
        if not await is_member_of_channel(user_id, ch["id"], bot):
            missing.append(ch)
    return len(missing) == 0, missing

# ---------- Telegraph ----------
telegraph = Telegraph()
try:
    telegraph.create_account(short_name=BOT_USERNAME)
except:
    pass

async def create_telegraph_page(title, content):
    try:
        html = content.replace('\n', '<br>')
        page = await asyncio.to_thread(
            telegraph.create_page,
            title=title,
            html_content=f"<p>{html}</p>",
            author_name="ရုပ်ရှင်အချက်အလက်"
        )
        return page['url']
    except:
        return None

# ---------- Translation ----------
translator_en_to_my = GoogleTranslator(source='en', target='my')
translator_my_to_en = GoogleTranslator(source='my', target='en')

def translate_text(text, source='en', target='my'):
    if not text or text == 'N/A':
        return text
    try:
        if source == 'en' and target == 'my':
            return translator_en_to_my.translate(text)
        elif source == 'my' and target == 'en':
            return translator_my_to_en.translate(text)
    except:
        return text

def contains_burmese(text):
    return bool(re.search(r'[\u1000-\u109F]', text))

def normalize_movie_name(text):
    if contains_burmese(text):
        try:
            return translator_my_to_en.translate(text).strip()
        except:
            return text
    return text

def make_narrative_plot(plot_en):
    if not plot_en or plot_en == 'N/A':
        return "ဇာတ်လမ်းအကျဉ်း မရရှိနိုင်ပါ။"
    plot_my = translate_text(plot_en, source='en', target='my')
    return f"🎙️ **ဇာတ်လမ်းအကျဉ်းချုပ်** (သဘာဝကျကျ ပြောပြထားသည်)\n\n{plot_my}"

# ---------- OMDB ----------
OMDB_API_KEY = "5025f95c"

def get_movie_info(movie_input):
    name = normalize_movie_name(movie_input)
    year_match = re.search(r'[\(\[]?(\d{4})[\)\]]?', name)
    if year_match:
        year = year_match.group(1)
        name = re.sub(r'[\(\[]?\d{4}[\)\]]?\s*$', '', name).strip()
    else:
        year = None
    params = {'t': name, 'apikey': OMDB_API_KEY, 'plot': 'full'}
    if year:
        params['y'] = year
    try:
        resp = requests.get("http://www.omdbapi.com/", params=params, timeout=10).json()
        if resp.get('Response') == 'False':
            return None
        plot_narrative = make_narrative_plot(resp.get('Plot', 'N/A'))
        runtime_raw = resp.get('Runtime', 'N/A')
        runtime = runtime_raw
        if 'min' in runtime_raw:
            try:
                minutes = int(runtime_raw.split()[0])
                hours = minutes // 60
                mins = minutes % 60
                runtime = f"{hours}နာရီ {mins}မိနစ်" if hours else f"{mins}မိနစ်"
            except:
                pass
        return {
            'title': translate_text(resp.get('Title', 'N/A')),
            'year': resp.get('Year', 'N/A'),
            'genre': translate_text(resp.get('Genre', 'N/A')),
            'actors': translate_text(resp.get('Actors', 'N/A')),
            'director': translate_text(resp.get('Director', 'N/A')),
            'runtime': runtime,
            'country': translate_text(resp.get('Country', 'N/A')),
            'language': translate_text(resp.get('Language', 'N/A')),
            'imdb_rating': resp.get('imdbRating', 'N/A'),
            'imdb_votes': resp.get('imdbVotes', 'N/A'),
            'plot_narrative': plot_narrative,
            'poster': resp.get('Poster', 'N/A'),
        }
    except Exception as e:
        logger.error(f"OMDb error: {e}")
        return None

def format_movie_info_plain(movie):
    stars = ""
    try:
        rating = float(movie['imdb_rating'])
        stars = '⭐' * int(rating // 2) + ('✨' if rating % 2 >= 0.5 else '')
    except:
        pass
    return f"""🎬 **{movie['title']}** ({movie['year']})

📌 **အမျိုးအစား** – {movie['genre']}
🎭 **သရုပ်ဆောင်များ** – {movie['actors']}
🎥 **ဒါရိုက်တာ** – {movie['director']}
⏱️ **ကြာချိန်** – {movie['runtime']}
🌍 **နိုင်ငံ** – {movie['country']}
🗣️ **ဘာသာစကား** – {movie['language']}
⭐ **IMDb** – {movie['imdb_rating']}/10 {stars}
🗳️ **မဲအရေအတွက်** – {movie['imdb_votes']}

{movie['plot_narrative']}"""

# ========== /movie Conversation ==========
MOVIE_NAME, MOVIE_POSTER, MOVIE_VIDEO = range(3)

async def movie_start(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin အတွက်သာ။")
        return ConversationHandler.END
    await update.message.reply_text("🎬 ဇာတ်ကားအမည် (နှစ်) ထည့်ပါ။\nဥပမာ - /movie Inception 2010 သို့မဟုတ် အင်စက်ပရှင်")
    return MOVIE_NAME

async def movie_get_name(update, context):
    movie_input = update.message.text.strip()
    msg = await update.message.reply_text(f"🔍 '{movie_input}' ကိုရှာနေသည်...")
    movie = get_movie_info(movie_input)
    if not movie:
        await msg.edit_text("မတွေ့ပါ။ အင်္ဂလိပ်အမည်အပြည့်ထည့်ပါ။")
        return MOVIE_NAME
    context.user_data['movie_data'] = movie
    await msg.edit_text(f"✅ တွေ့ရှိပါသည်။\n\n{format_movie_info_plain(movie)}")
    await update.message.reply_text("📸 Poster ပုံပို့ပါ။")
    return MOVIE_POSTER

async def movie_get_poster(update, context):
    if not update.message.photo:
        await update.message.reply_text("ပုံပို့ပါ။")
        return MOVIE_POSTER
    context.user_data['poster'] = update.message.photo[-1].file_id
    await update.message.reply_text("🎬 Video ဖိုင်ပို့ပါ။")
    return MOVIE_VIDEO

async def movie_get_video(update, context):
    video = None
    file_name = None
    if update.message.video:
        video = update.message.video
        file_name = video.file_name or "movie"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            video = doc
            file_name = doc.file_name or "movie"
    if not video:
        await update.message.reply_text("Video ဖိုင်ပို့ပါ။")
        return MOVIE_VIDEO

    movie = context.user_data.get('movie_data')
    poster = context.user_data.get('poster')
    if not movie or not poster:
        await update.message.reply_text("ဒေတာပျောက်နေသည်။ /movie ဖြင့်ပြန်စပါ။")
        return ConversationHandler.END

    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    logger.info(f"Deep link created: {deep_link}")

    keyboard = [
        [InlineKeyboardButton("🎬 ရုပ်ရှင်ရယူရန်", url=deep_link)],
        [InlineKeyboardButton("🔞 လူကြီးချန်နယ်", url="https://t.me/everyboyhobby")],
        [InlineKeyboardButton("🎬 Movie Channel", url="https://t.me/moviesandseriesforallwzn")],
        [InlineKeyboardButton("🎵 မြန်မာသီချင်းချန်နယ်", url="https://t.me/wznmusiclibary")],
    ]

    await update.message.reply_photo(photo=poster, caption=format_movie_info_plain(movie), reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text(f"✅ Post ပြင်ဆင်ပြီး။\nDeep Link: {deep_link}")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_movie(update, context):
    await update.message.reply_text("Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ========== Auto Deep Link ==========
async def auto_deep_link(update, context):
    if not is_admin(update.effective_user.id):
        return
    if context.user_data.get('movie_name') is not None:
        return
    video = None
    file_name = None
    if update.message.video:
        video = update.message.video
        file_name = video.file_name or "video"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            video = doc
            file_name = doc.file_name or "video"
    if not video:
        return
    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    await update.message.reply_text(f"🔗 Deep Link:\n{deep_link}\n\n{file_name}")

# ========== /start ==========
async def start(update, context):
    user_id = update.effective_user.id
    if context.args:
        payload = context.args[0]
        info = get_file_info(payload)
        if not info:
            await update.message.reply_text("လင့်မမှန်ပါ။")
            return
        if is_user_blocked(user_id):
            await update.message.reply_text("သင်ပိတ်ခံထားရသည်။")
            return
        ok, _ = await check_all_channels(user_id, context.bot)
        if not ok:
            attempts = get_attempt_count(user_id) + 1
            increment_attempts(user_id)
            if attempts >= 10:
                block_user(user_id)
                await update.message.reply_text("၁၀ကြိမ်ကျော်သောကြောင့်ပိတ်သည်။")
                return
            msg = "Channel များအားလုံးဝင်ပါ:\n"
            for ch in REQUIRED_CHANNELS:
                msg += f"• {ch['name']}: {ch['invite']}\n"
            msg += f"\nအကြိမ်ရေ: {attempts}/10"
            await update.message.reply_text(msg)
            return
        try:
            await update.message.reply_text(f"📹 {info['file_name']} ပို့နေပါပြီ...")
            await context.bot.send_video(chat_id=user_id, video=info['file_id'], caption=f"🎬 {info['file_name']}")
            add_user(user_id)
            increment_requests()
            reset_attempts(user_id)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
    else:
        if is_admin(user_id):
            await show_menu(update, context)
        else:
            await update.message.reply_text("🎬 မင်္ဂလာပါ။ /movie ဖြင့်ရှာနိုင်သည်။")

# ---------- Admin Menu ----------
async def show_menu(update, context):
    keyboard = [
        [InlineKeyboardButton("🎬 Movie Post", callback_data="menu_movie")],
        [InlineKeyboardButton("🔗 Deep Link", callback_data="menu_newfile")],
        [InlineKeyboardButton("📦 Batch Link", callback_data="menu_batch")],
        [InlineKeyboardButton("📊 Stats", callback_data="menu_stats")],
        [InlineKeyboardButton("🚫 Blocklist", callback_data="menu_block")],
    ]
    await update.message.reply_text("Admin Menu", reply_markup=InlineKeyboardMarkup(keyboard))

async def menu_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("ခွင့်မပြု")
        return
    if query.data == "menu_movie":
        await query.edit_message_text("/movie")
    elif query.data == "menu_newfile":
        await query.edit_message_text("Video ဖိုင်ပို့ပါ။")
    elif query.data == "menu_batch":
        await query.edit_message_text("/batchlink")
    elif query.data == "menu_stats":
        total_users = users_collection.count_documents({})
        total_req = get_total_requests()
        await query.edit_message_text(f"👥 {total_users} users\n🎬 {total_req} requests")
    elif query.data == "menu_block":
        blocked = get_blocked_users()
        msg = "Blocked:\n" + "\n".join(str(uid) for uid in blocked) if blocked else "Empty"
        await query.edit_message_text(msg)

async def newfile_command(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("Video ဖိုင်ပို့ပါ။")
    context.user_data['waiting_newfile'] = True

async def newfile_receive(update, context):
    if not context.user_data.get('waiting_newfile'):
        return
    if not is_admin(update.effective_user.id):
        return
    video = None
    file_name = None
    if update.message.video:
        video = update.message.video
        file_name = video.file_name or "video"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            video = doc
            file_name = doc.file_name or "video"
    if not video:
        await update.message.reply_text("Video ဖိုင်ပို့ပါ။")
        return
    payload = generate_payload()
    save_file_info(payload, video.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    await update.message.reply_text(f"Deep Link:\n{deep_link}\n\n{file_name}")
    context.user_data.pop('waiting_newfile', None)

async def batchlink_start(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data['batch_videos'] = []
    await update.message.reply_text("Batch Mode\nVideo များဆက်တိုက်ပို့ပါ။ /done")

async def batchlink_receive(update, context):
    if not is_admin(update.effective_user.id):
        return
    if 'batch_videos' not in context.user_data:
        return
    video = None
    name = None
    if update.message.video:
        video = update.message.video
        name = video.file_name or "video"
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type and doc.mime_type.startswith('video/'):
            video = doc
            name = doc.file_name or "video"
    if not video:
        await update.message.reply_text("Video ပို့ပါ")
        return
    context.user_data['batch_videos'].append({"file_id": video.file_id, "name": name})
    await update.message.reply_text(f"✅ #{len(context.user_data['batch_videos'])}: {name}")

async def batchlink_done(update, context):
    if not is_admin(update.effective_user.id):
        return
    videos = context.user_data.get('batch_videos', [])
    if not videos:
        await update.message.reply_text("ဗီဒီယိုမရှိပါ")
        return
    results = []
    for v in videos:
        payload = generate_payload()
        save_file_info(payload, v["file_id"], v["name"])
        link = create_deep_linked_url(BOT_USERNAME, payload)
        results.append(f"• {v['name']}\n  {link}")
    text = "Batch Links:\n" + "\n".join(results)
    if len(text) > 4000:
        text = text[:4000] + "..."
    await update.message.reply_text(text)
    context.user_data.clear()

async def cancel_batch(update, context):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")

async def stats(update, context):
    if is_admin(update.effective_user.id):
        total_users = users_collection.count_documents({})
        total_req = get_total_requests()
        await update.message.reply_text(f"👥 Users: {total_users}\n🎬 Requests: {total_req}")

async def blocklist(update, context):
    if is_admin(update.effective_user.id):
        blocked = get_blocked_users()
        msg = "Blocked:\n" + "\n".join(str(uid) for uid in blocked) if blocked else "None"
        await update.message.reply_text(msg)

async def unblock(update, context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("/unblock user_id")
        return
    try:
        uid = int(context.args[0])
        if is_user_blocked(uid):
            unblock_user(uid)
            await update.message.reply_text(f"Unblocked {uid}")
        else:
            await update.message.reply_text(f"{uid} not blocked")
    except:
        await update.message.reply_text("Invalid ID")

async def menu_command(update, context):
    if is_admin(update.effective_user.id):
        await show_menu(update, context)

# ---------- Webhook ----------
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL missing")
    sys.exit(1)

telegram_app = Application.builder().token(TOKEN).build()

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("menu", menu_command))
telegram_app.add_handler(CommandHandler("stats", stats))
telegram_app.add_handler(CommandHandler("blocklist", blocklist))
telegram_app.add_handler(CommandHandler("unblock", unblock))
telegram_app.add_handler(CommandHandler("newfile", newfile_command))
telegram_app.add_handler(CommandHandler("batchlink", batchlink_start))
telegram_app.add_handler(CommandHandler("done", batchlink_done))
telegram_app.add_handler(CommandHandler("cancel", cancel_batch))
telegram_app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL & ~filters.COMMAND, auto_deep_link))
telegram_app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL & ~filters.COMMAND, newfile_receive))
telegram_app.add_handler(CallbackQueryHandler(menu_callback, pattern="menu_"))

movie_conv = ConversationHandler(
    entry_points=[CommandHandler('movie', movie_start)],
    states={
        MOVIE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, movie_get_name)],
        MOVIE_POSTER: [MessageHandler(filters.PHOTO, movie_get_poster)],
        MOVIE_VIDEO: [MessageHandler(filters.VIDEO | filters.Document.ALL, movie_get_video)],
    },
    fallbacks=[CommandHandler('cancel', cancel_movie)],
)
telegram_app.add_handler(movie_conv)

batch_conv = ConversationHandler(
    entry_points=[CommandHandler('batchlink', batchlink_start)],
    states={0: [MessageHandler(filters.VIDEO | filters.Document.ALL, batchlink_receive)]},
    fallbacks=[CommandHandler('done', batchlink_done), CommandHandler('cancel', cancel_batch)],
)
telegram_app.add_handler(batch_conv)

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        try:
            json_data = request.get_json(force=True)
            update = Update.de_json(json_data, telegram_app.bot)
            asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), loop)
            return "ok", 200
        except Exception as e:
            logger.exception("Webhook error")
            return "error", 500
    return "method not allowed", 405

def start_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

async def setup_webhook():
    await telegram_app.bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telegram_app.initialize())
    loop.run_until_complete(setup_webhook())
    threading.Thread(target=start_flask, daemon=True).start()
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        loop.run_until_complete(telegram_app.shutdown())
