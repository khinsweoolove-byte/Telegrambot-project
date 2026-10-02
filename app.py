import os
import asyncio
import threading
import logging
import secrets
import sys
import time
from datetime import datetime
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    ConversationHandler, CallbackQueryHandler
)
from telegram.helpers import create_deep_linked_url
from pymongo import MongoClient
from telegraph import Telegraph

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------- MongoDB ----------
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI not set")
    sys.exit(1)

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    logger.info("MongoDB connected")
except:
    mongo_client = MongoClient(MONGO_URI, tlsAllowInvalidCertificates=True, serverSelectionTimeoutMS=5000)

db = mongo_client["file_share_bot"]
files_col = db["files"]
users_col = db["users"]
stats_col = db["stats"]

def save_file(payload, file_id, file_name):
    files_col.update_one({"payload": payload}, {"$set": {"file_id": file_id, "file_name": file_name}}, upsert=True)

def get_file(payload):
    doc = files_col.find_one({"payload": payload})
    if doc:
        return doc["file_id"], doc["file_name"]
    return None, None

def delete_file_by_payload(payload):
    result = files_col.delete_one({"payload": payload})
    return result.deleted_count > 0

def add_user(user_id):
    if not users_col.find_one({"user_id": user_id}):
        users_col.insert_one({"user_id": user_id, "first_seen": datetime.now()})

def get_all_users():
    return [doc["user_id"] for doc in users_col.find({}, {"user_id": 1})]

def increment_requests():
    stats_col.update_one({"_id": "total_requests"}, {"$inc": {"count": 1}}, upsert=True)
    if stats_col.count_documents({"_id": "total_requests"}) == 0:
        stats_col.insert_one({"_id": "total_requests", "count": 0})

def get_total_requests():
    doc = stats_col.find_one({"_id": "total_requests"})
    return doc["count"] if doc else 0

# ---------- Telegraph ----------
telegraph = Telegraph()
try:
    telegraph.create_account(short_name="MoviePostBot")
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

# ---------- Telegram Config ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").strip()
if BOT_USERNAME.startswith("@"):
    BOT_USERNAME = BOT_USERNAME[1:]

if not TOKEN or not BOT_USERNAME:
    logger.error("TELEGRAM_TOKEN and BOT_USERNAME required")
    sys.exit(1)

logger.info(f"🔧 Using Bot Username: @{BOT_USERNAME}")

ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_ID", "").split(",") if x.strip()]
logger.info(f"👑 Admin IDs: {ADMIN_IDS}")

# ============================================================
# 🔥 Environment Variable ကနေ Channel IDs ကိုဖတ်မယ်
# ============================================================
RAW_CHANNELS = os.environ.get("REQUIRED_CHANNELS", "")
REQUIRED_CHANNELS = []

CHANNEL_NAMES = {
    "-1003753299714": "🎬 ဇာတ်ကားချန်နယ် (ပင်မ)",
    "-1003899625672": "🎬 ဇာတ်ကားချန်နယ် (အရံ)",
    "-1003792838735": "🔞 လူကြီးများအတွက် သီးသန့်ချန်နယ်",
    "-1003785717514": "🎵 မြန်မာသီချင်းချန်နယ်"
}

CHANNEL_INVITES = {
    "-1003753299714": "https://t.me/wznmoviescollector",
    "-1003899625672": "https://t.me/moviesandseriesforallwzn",
    "-1003792838735": "https://t.me/everyboyhobby",
    "-1003785717514": "https://t.me/wznmusiclibary"
}

if RAW_CHANNELS:
    for ch_id in RAW_CHANNELS.split(","):
        ch_id = ch_id.strip()
        if ch_id:
            name = CHANNEL_NAMES.get(ch_id, f"Channel {ch_id}")
            invite = CHANNEL_INVITES.get(ch_id, "#")
            REQUIRED_CHANNELS.append({"id": ch_id, "name": name, "invite": invite})
    logger.info(f"📢 Loaded {len(REQUIRED_CHANNELS)} channels from ENV")
else:
    REQUIRED_CHANNELS = [
        {"id": "-1003753299714", "name": "🎬 ဇာတ်ကားချန်နယ် (ပင်မ)", "invite": "https://t.me/wznmoviescollector"},
        {"id": "-1003899625672", "name": "🎬 ဇာတ်ကားချန်နယ် (အရံ)", "invite": "https://t.me/moviesandseriesforallwzn"},
        {"id": "-1003792838735", "name": "🔞 လူကြီးများအတွက် သီးသန့်ချန်နယ်", "invite": "https://t.me/everyboyhobby"},
        {"id": "-1003785717514", "name": "🎵 မြန်မာသီချင်းချန်နယ်", "invite": "https://t.me/wznmusiclibary"}
    ]
    logger.warning("⚠️ REQUIRED_CHANNELS not set in ENV, using defaults")

def is_admin(user_id):
    return user_id in ADMIN_IDS

def generate_payload():
    return secrets.token_urlsafe(12)

async def is_member_of_channel(user_id, channel_id, bot):
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        is_member = member.status in ["member", "administrator", "creator"]
        logger.info(f"User {user_id} in channel {channel_id}: {is_member} (status: {member.status})")
        return is_member
    except Exception as e:
        logger.error(f"Channel check error for {channel_id}: {e}")
        return False

async def check_all_channels(user_id, bot):
    missing = []
    for ch in REQUIRED_CHANNELS:
        if not await is_member_of_channel(user_id, ch["id"], bot):
            missing.append(ch)
    return len(missing) == 0, missing

# ---------- Auto-delete helper ----------
async def delete_messages_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_ids: list, delay_seconds: int = 300):
    await asyncio.sleep(delay_seconds)
    for msg_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            logger.info(f"Auto-deleted message {msg_id}")
        except Exception as e:
            logger.warning(f"Failed to delete {msg_id}: {e}")

# ---------- Conversation states ----------
POST_PHOTO, POST_MOVIE = range(2)
POST_TEXT_PHOTO, POST_TEXT_MOVIE = range(10, 12)

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv", ".rmvb", ".divx", ".vob",
}


def extract_video(message):
    """ဗီဒီယိုဖိုင် ရယူပါ။ mime_type မရှိတဲ့ / application/octet-stream ဖြစ်တဲ့
    ဖိုင်တွေကိုလည်း extension ကနေ ခွင့်ပေးပါတယ်။"""
    if message.video:
        return message.video, (message.video.file_name or "video")
    doc = message.document
    if doc:
        mime = (doc.mime_type or "").lower()
        name = doc.file_name or "movie"
        ext = os.path.splitext(name)[1].lower()
        if mime.startswith("video/") or ext in VIDEO_EXTS:
            return doc, name
    return None, None

# ---------- /post ----------
async def post_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ အဒ်မင်များသာ အသုံးပြုနိုင်ပါသည်။")
        return ConversationHandler.END
    await update.message.reply_text(
        "📸 ပိုစတာ (Poster) ပုံများကို စတင်ပို့ပါ။\n\n"
        "1️⃣ ပုံများ ပို့ပါ\n"
        "2️⃣ ရုပ်ရှင်ဖိုင် (video) ပို့ပါ\n"
        "3️⃣ စာသား (caption) ပို့ပါ\n\n"
        "📝 စာသား မလိုဘူးဆိုရင် အဆင့် ၃ မှာ 'aa' ဟု ရိုက်ပါ — ချက်ချင်း ဖန်တီးပေးပါမယ်။"
    )
    context.user_data['photos'] = []
    context.user_data['_post_conversation'] = True
    return POST_PHOTO

async def post_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    # ---- ရုပ်ရှင်ဖိုင် (video/document) ----
    file_obj, file_name = extract_video(message)
    if file_obj:
        if not context.user_data.get('photos'):
            await message.reply_text("⚠️ ပိုစတာ ပုံ မတွေ့ပါ။ ပုံပို့ပြီးမှ ရုပ်ရှင်ဖိုင်ပို့ပါ။")
            return POST_PHOTO
        # အဆင့် ၂ ပြီးပါပြီ — အဆင့် ၃ (စာသား) ကို စောင့်မယ်
        context.user_data['pending_movie'] = (file_obj, file_name)
        await message.reply_text(
            f"✅ ပိုစတာ {len(context.user_data['photos'])} ပုံ၊ ရုပ်ရှင်ဖိုင် လက်ခံရရှိပါပြီ။\n\n"
            "✍️ ယခု စာသား (caption) ပို့ပါ။\n"
            "မလိုဘူးဆိုရင် 'aa' ဟု ရိုက်ပါ။"
        )
        return POST_MOVIE

    # ---- စာသား ----
    if message.text:
        text = message.text.strip()
        if not text:
            await message.reply_text("ကျေးဇူးပြု၍ ဓာတ်ပုံ ပုံတစ်ပုံ သို့မဟုတ် ရုပ်ရှင်ဖိုင် ပို့ပါ။")
            return POST_PHOTO
        if not context.user_data.get('photos'):
            await message.reply_text("⚠️ ပိုစတာ ပုံ မတွေ့ပါ။ ပုံပို့ပြီးမှ ရုပ်ရှင်ဖိုင်ပို့ပါ။")
            return POST_PHOTO
        # ပုံတွေပြီးမှ စာသား ပို့လာတာ — movie ကို စောင့်မယ်
        context.user_data['custom_caption'] = text
        await message.reply_text(
            "✍️ စာသား သိမ်းပြီးပါပြီ။\n\n🎬 ယခု ရုပ်ရှင်ဖိုင် (video or document) ကို ပို့ပေးပါ။"
        )
        return POST_MOVIE

    # ---- ပုံ (album အတွင်း ပုံအပြားလုံးကို ယူရပါတယ်) ----
    if not message.photo:
        await message.reply_text("ကျေးဇူးပြု၍ ဓာတ်ပုံ ပုံတစ်ပုံ ပို့ပေးပါ။")
        return POST_PHOTO
    photos = context.user_data.setdefault('photos', [])
    is_first = not photos
    # message.photo က ပုံတစ်ပုံရဲ့ အရွယ်အစားအမျိုးအစားတွေ ဖြစ်ပါတယ်။
    # အားလုံးကို ပို့ပေးရင် ပုံတစ်ခုက ပုံ ၃-၅ ခုအဖြစ် ပို့သွားမှာမို့
    # အကြီးဆုံးတစ်ခုတည်းကိုသာ ယူပါမယ်။
    photos.append(message.photo[-1].file_id)
    if is_first and message.caption:
        context.user_data['custom_caption'] = message.caption
    await message.reply_text(
        f"✅ ပုံ {len(photos)} လက်ခံရရှိပါပြီ။ ဆက်ပို့နိုင်ပါသည်။"
    )
    return POST_PHOTO

async def post_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    photos = context.user_data.get('photos', [])
    if not photos:
        await message.reply_text("ပိုစတာ မတွေ့ပါ။ /post ဖြင့် ပြန်စတင်ပါ။")
        return ConversationHandler.END

    # ---- စာသား လာမယ် (အဆင့် ၃) ----
    # ရုပ်ရှင်ဖိုင် ရပြီးမှသာ စာသားကို လက်ခံမယ်။
    if message.text:
        text = message.text.strip()
        if not context.user_data.get('pending_movie'):
            await message.reply_text(
                "🎬 ရုပ်ရှင်ဖိုင် ကို ဦးစွာ ပို့ပေးပါ။ စာသား နောက်မှ လက်ခံမယ်။"
            )
            return POST_MOVIE
        if text.lower() != "aa":
            context.user_data['custom_caption'] = text
        return await publish_post(update, context)

    # ---- ရုပ်ရှင်ဖိုင် လာမယ် (အဆင့် ၂) ----
    file_obj, file_name = extract_video(message)
    if not file_obj:
        await message.reply_text(
            "ကျေးဇူးပြု၍ ဗီဒီယိုဖိုင် (mp4, mkv, etc.) ပို့ပေးပါ။"
        )
        return POST_MOVIE

    context.user_data['pending_movie'] = (file_obj, file_name)

    # စာသား အရင်ကတည်း ရောက်ထားတာ (flow: photo → text → movie) ဆိုရင်
    # အခု အဆင့် ၃ ပြီးသားဖြစ်လို့ ချက်ချင်း ဖန်တီးမယ်။
    if context.user_data.get('custom_caption'):
        return await publish_post(update, context)

    await message.reply_text(
        "✅ ရုပ်ရှင်ဖိုင် လက်ခံရရှိပါပြီ။\n\n"
        "✍️ ယခု စာသား (caption) ပို့ပါ။\n"
        "မလိုဘူးဆိုရင် 'aa' ဟု ရိုက်ပါ။"
    )
    return POST_MOVIE


async def send_photo_batches(message, photos):
    """Telegram media_group limit က 10 ခု — ကျော်တာကို chunk ခွဲပါမယ်။"""
    for i in range(0, len(photos), 10):
        chunk = photos[i:i + 10]
        try:
            await message.reply_media_group(
                media=[InputMediaPhoto(media=p) for p in chunk]
            )
        except Exception as e:
            logger.error(f"Media group chunk failed ({len(chunk)} photos): {e}")
            for photo in chunk:
                try:
                    await message.reply_photo(photo=photo)
                except Exception as pe:
                    logger.error(f"reply_photo failed: {pe}")


async def publish_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ပိုစတာ + ရုပ်ရှင်ဖိုင် + caption အားလုံး ရှိပြီးပြီဆိုတဲ့အခါ ခေါ်မယ်။"""
    message = update.message
    pending = context.user_data.pop('pending_movie', None)
    file_obj, file_name = pending

    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    logger.info(f"✅ NEW POST DEEP LINK: {deep_link}")

    caption = context.user_data.get('custom_caption') or "🎬 ရုပ်ရှင်အသစ်"
    keyboard = [[InlineKeyboardButton("🎬 ရုပ်ရှင်ရယူရန်", url=deep_link)]]
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])

    await send_photo_batches(message, context.user_data['photos'])

    await message.reply_text(
        text=caption,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    await message.reply_text("✅ ပိုစတာ ဖန်တီးခြင်း အောင်မြင်ပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- /post_text ----------
async def post_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ အဒ်မင်များသာ အသုံးပြုနိုင်ပါသည်။")
        return ConversationHandler.END
    await update.message.reply_text(
        "📸 ပိုစတာ (Poster) ပုံများကို စတင်ပို့ပါ။\n\n"
        "1️⃣ ပုံများ ပို့ပါ\n"
        "2️⃣ ရုပ်ရှင်ဖိုင် (video) ပို့ပါ\n"
        "3️⃣ ဇာတ်ညွှန်း စာသား ပို့ပါ\n\n"
        "📝 စာသားရှည် (1024 ကျော်) ဆိုရင် Telegraph page အဖြစ် အလိုအလျောက် ဖန်တီးပေးပါမယ်။\n"
        "မလိုဘူးဆိုရင် အဆင့် ၃ မှာ 'aa' ဟု ရိုက်ပါ။"
    )
    context.user_data['photos'] = []
    context.user_data['_post_conversation'] = True
    return POST_TEXT_PHOTO

async def post_text_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    # ---- ရုပ်ရှင်ဖိုင် (video/document) ----
    file_obj, file_name = extract_video(message)
    if file_obj:
        if not context.user_data.get('photos'):
            await message.reply_text("⚠️ ပိုစတာ ပုံ မတွေ့ပါ။ ပုံပို့ပြီးမှ ရုပ်ရှင်ဖိုင်ပို့ပါ။")
            return POST_TEXT_PHOTO
        # အဆင့် ၂ ပြီးပါပြီ — အဆင့် ၃ (ဇာတ်ညွှန်း) ကို စောင့်မယ်
        context.user_data['pending_movie'] = (file_obj, file_name)

        # ဇာတ်ညွှန်း အရင်ကတည်း ရောက်ထားတာ (photo → text → movie) ဆိုရင်
        # အခု အဆင့် ၃ ပြီးသားဖြစ်လို့ ချက်ချင်း ဖန်တီးမယ်။
        if context.user_data.get('caption_text'):
            return await publish_post_text(update, context)

        await message.reply_text(
            f"✅ ပိုစတာ {len(context.user_data['photos'])} ပုံ၊ ရုပ်ရှင်ဖိုင် လက်ခံရရှိပါပြီ။\n\n"
            "✍️ ယခု ဇာတ်ညွှန်း စာသား ပို့ပါ။\n"
            "မလိုဘူးဆိုရင် 'aa' ဟု ရိုက်ပါ။"
        )
        return POST_TEXT_MOVIE

    # ---- စာသား ----
    if message.text:
        text = message.text.strip()
        if not text:
            await message.reply_text("ကျေးဇူးပြု၍ ဓာတ်ပုံ ပုံတစ်ပုံ သို့မဟုတ် ရုပ်ရှင်ဖိုင် ပို့ပါ။")
            return POST_TEXT_PHOTO
        if not context.user_data.get('photos'):
            await message.reply_text("⚠️ ပိုစတာ ပုံ မတွေ့ပါ။ ပုံပို့ပြီးမှ ရုပ်ရှင်ဖိုင်ပို့ပါ။")
            return POST_TEXT_PHOTO
        # ပုံတွေပြီးမှ စာသား ပို့လာတာ — movie ကို စောင့်မယ်
        context.user_data['caption_text'] = text
        await maybe_create_telegraph_page(message, context, text)
        await message.reply_text(
            "✍️ စာသား သိမ်းပြီးပါပြီ။\n\n🎬 ယခု ရုပ်ရှင်ဖိုင် (video or document) ကို ပို့ပေးပါ။"
        )
        return POST_TEXT_MOVIE

    # ---- ပုံ (album အတွင်း ပုံအပြားလုံးကို ယူရပါတယ်) ----
    if not message.photo:
        await message.reply_text("ကျေးဇူးပြု၍ ဓာတ်ပုံ ပုံတစ်ပုံ ပို့ပေးပါ။")
        return POST_TEXT_PHOTO
    photos = context.user_data.setdefault('photos', [])
    is_first = not photos
    photos.append(message.photo[-1].file_id)
    if is_first and message.caption:
        context.user_data['caption_text'] = message.caption
        await maybe_create_telegraph_page(message, context, message.caption)
    await message.reply_text(
        f"✅ ပုံ {len(photos)} လက်ခံရရှိပါပြီ။ ဆက်ပို့နိုင်ပါသည်။"
    )
    return POST_TEXT_PHOTO

async def maybe_create_telegraph_page(message, context: ContextTypes.DEFAULT_TYPE, text: str):
    """ဇာတ်ညွှန်း 1024 ကျော်ရှည်နေရင် Telegraph page ဖန်တီးပါ။"""
    if len(text) <= 1024:
        return
    await message.reply_text(
        "⏳ စာသားရှည်နေပါသည်။ Telegraph စာမျက်နှာ ဖန်တီးနေပါပြီ..."
    )
    title = f"Movie Synopsis - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    telegraph_url = await create_telegraph_page(title, text)
    if telegraph_url:
        context.user_data['telegraph_url'] = telegraph_url
        await message.reply_text(
            f"✅ Telegraph စာမျက်နှာ ဖန်တီးပြီးပါပြီ။\n{telegraph_url}"
        )
    else:
        await message.reply_text(
            "❌ Telegraph ဖန်တီးရာတွင် အမှား။ စာသားကို အတွင်းသုံးပါမည်။"
        )

async def post_text_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    photos = context.user_data.get('photos', [])
    if not photos:
        await message.reply_text("ပိုစတာ မတွေ့ပါ။ /post_text ဖြင့် ပြန်စတင်ပါ။")
        return ConversationHandler.END

    # ---- ဇာတ်ညွှန်း လာမယ် (အဆင့် ၃) ----
    if message.text:
        text = message.text.strip()
        if not context.user_data.get('pending_movie'):
            await message.reply_text(
                "🎬 ရုပ်ရှင်ဖိုင် ကို ဦးစွာ ပို့ပေးပါ။ ဇာတ်ညွှန်း နောက်မှ လက်ခံမယ်။"
        )
            return POST_TEXT_MOVIE
        if text.lower() != "aa":
            context.user_data['caption_text'] = text
            await maybe_create_telegraph_page(message, context, text)
        return await publish_post_text(update, context)

    # ---- ရုပ်ရှင်ဖိုင် လာမယ် (အဆင့် ၂) ----
    file_obj, file_name = extract_video(message)
    if not file_obj:
        await message.reply_text(
            "ကျေးဇူးပြု၍ ဗီဒီယိုဖိုင် (mp4, mkv, etc.) ပို့ပေးပါ။"
        )
        return POST_TEXT_MOVIE

    context.user_data['pending_movie'] = (file_obj, file_name)

    # ဇာတ်ညွှန်း အရင်ကတည်း ရောက်ထားတာ (photo → text → movie) ဆိုရင်
    # အခု အဆင့် ၃ ပြီးသားဖြစ်လို့ ချက်ချင်း ဖန်တီးမယ်။
    if context.user_data.get('caption_text'):
        return await publish_post_text(update, context)

    await message.reply_text(
        "✅ ရုပ်ရှင်ဖိုင် လက်ခံရရှိပါပြီ။\n\n"
        "✍️ ယခု ဇာတ်ညွှန်း စာသား ပို့ပါ။\n"
        "မလိုဘူးဆိုရင် 'aa' ဟု ရိုက်ပါ။"
    )
    return POST_TEXT_MOVIE


async def publish_post_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ပိုစတာ + ရုပ်ရှင်ဖိုင် + ဇာတ်ညွှန်း အားလုံး ရှိပြီးပြီဆိုတဲ့အခါ ခေါ်မယ်။"""
    message = update.message
    file_obj, file_name = context.user_data.pop('pending_movie')

    photos = context.user_data.get('photos', [])
    caption_text = context.user_data.get('caption_text', '')
    telegraph_url = context.user_data.get('telegraph_url')

    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    
    logger.info(f"✅ NEW POST_TEXT DEEP LINK: {deep_link}")

    if telegraph_url:
        preview = caption_text[:300] + "..." if len(caption_text) > 300 else caption_text
        caption = f"{preview}\n\n📖 ဇာတ်ညွှန်းအပြည့်အစုံဖတ်ရန်: {telegraph_url}\n\n🎬 ရုပ်ရှင်ရယူရန် အောက်ပါခလုတ်ကို နှိပ်ပါ။"
    elif caption_text:
        caption = f"{caption_text}\n\n🎬 ရုပ်ရှင်ရယူရန် အောက်ပါခလုတ်ကို နှိပ်ပါ။"
    else:
        caption = "🎬 ရုပ်ရှင်အသစ်\n\n🎬 ရုပ်ရှင်ရယူရန် အောက်ပါခလုတ်ကို နှိပ်ပါ။"

    keyboard = [[InlineKeyboardButton("🎬 ရုပ်ရှင်ရယူရန်", url=deep_link)]]
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await send_photo_batches(message, photos)

    await message.reply_text(text=caption, reply_markup=reply_markup)
    await message.reply_text("✅ ပိုစတာ ဖန်တီးခြင်း အောင်မြင်ပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_post_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- Standalone file upload ----------
# Admin က ဖိုင်အများကြီး ပို့လို့တဲ့အခါ Telegram က album (media_group) အဖြစ်
# ပို့လာပါတယ်။ Album ထဲက ပထမဆုံး ဖိုင်တွေကိုပဲ handle လုပ်ပြီး ကျန်တာ skip ဖြစ်ပါတယ်။
# ဒါကြောင့် album အတုံးအလိုက် buffer လုပ်ပြီး အားလုံးပြီးတိုင်မှ အစဉ်လိုက် deep link ထုတ်ပေးပါတယ်။

_albums = {}  # {media_group_id: {"items": [...], "started": ts, "last_update": ts, "chat_id": int, "task": Task}}

ALBUM_QUIET_PERIOD = 1.5  # နောက်ဆုံး ဖိုင် ရောက်ပြီးမကြာမီ ထပ်ပို့လာလို့ မရှိသေးရင် flush လုပ်မယ့်ခါ
ALBUM_MAX_WAIT = 8.0     # album စတင်ခဲ့တာကနေ အမြင့်ဆုံး စောင့်ချိန်

def _cancel_album_task(entry):
    task = entry.get("task")
    if task and not task.done():
        task.cancel()

async def _flush_album(context: ContextTypes.DEFAULT_TYPE, media_group_id: str):
    """Album တစ်ခုလုံး ပြီးသွားရင် အစဉ်လိုက် deep link ထုတ်ပေးပါတယ်။"""
    await asyncio.sleep(ALBUM_QUIET_PERIOD)
    entry = _albums.get(media_group_id)
    if not entry:
        return

    now = time.monotonic()
    quiet = now - entry["last_update"]
    total = now - entry["started"]
    if total < ALBUM_MAX_WAIT and quiet < ALBUM_QUIET_PERIOD:
        entry["task"] = context.application.create_task(
            _flush_album(context, media_group_id)
        )
        return

    await _send_album_links(context, media_group_id)


async def _send_album_links(context: ContextTypes.DEFAULT_TYPE, media_group_id: str):
    entry = _albums.pop(media_group_id, None)
    if not entry or not entry["items"]:
        return

    items = entry["items"]
    chat_id = entry["chat_id"]
    total = len(items)

    if total == 1:
        file_id, file_name, deep_link = items[0]
        keyboard = [[InlineKeyboardButton("🎬 ရုပ်ရှင်ရယူရန်", url=deep_link)]]
        for ch in REQUIRED_CHANNELS:
            keyboard.append([InlineKeyboardButton(ch["name"], url=ch["invite"])])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Deep Link အဆင်သင့်ပါပြီ ({file_name})",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    lines = [f"✅ Deep Links {total} ခု အဆင်သင့်ဖြစ်ပါပြီ။", ""]
    buttons = []
    for i, (file_id, file_name, deep_link) in enumerate(items, start=1):
        lines.append(f"{i}. {file_name}")
        buttons.append([
            InlineKeyboardButton(f"{i}. 🎬 {file_name[:40]}", url=deep_link)
        ])

    for ch in REQUIRED_CHANNELS:
        buttons.append([InlineKeyboardButton(ch["name"], url=ch["invite"])])

    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    # /post လုပ်ဆောင်နေတဲ့အခါ standalone upload ကို မနှောက်ပါ — user_data ပေါ်
    # အခြေအနေမှတစ်ခုတည်းနဲ့ gate မလုပ်ဘဲ ConversationHandler က active
    # ကြောင်း တိုင်းတာပါ။
    if context.user_data.get('_post_conversation'):
        return
    message = update.message
    file_obj = None
    file_name = "file"
    if message.document:
        file_obj = message.document
        file_name = file_obj.file_name or "document"
    elif message.video:
        file_obj = message.video
        file_name = file_obj.file_name or "video"
    elif message.photo:
        file_obj = message.photo[-1]
        file_name = "photo.jpg"
    elif message.audio:
        file_obj = message.audio
        file_name = file_obj.file_name or "audio"
    else:
        return

    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    logger.info(f"✅ DEEP LINK: {file_name} -> {deep_link}")

    media_group_id = getattr(message, "media_group_id", None)

    # -------- Album မဟုတ်ရင် ချက်ချင်း ပေးပါ --------
    if not media_group_id:
        keyboard = [[InlineKeyboardButton("🎬 ရုပ်ရှင်ရယူရန်", url=deep_link)]]
        for ch in REQUIRED_CHANNELS:
            keyboard.append([InlineKeyboardButton(ch["name"], url=ch["invite"])])
        await message.reply_text(
            f"🔗 သင်၏ Deep Link အဆင်သင့်ဖြစ်ပါပြီ။\n\n{deep_link}\n\n{file_name}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # -------- Album ဖြစ်ရင် buffer ထဲ သို့သွင့်းပါ --------
    loop_time = time.monotonic()
    if media_group_id not in _albums:
        _albums[media_group_id] = {
            "items": [],
            "started": loop_time,
            "last_update": loop_time,
            "chat_id": message.chat_id,
        }
    entry = _albums[media_group_id]
    entry["items"].append((file_obj.file_id, file_name, deep_link))
    entry["last_update"] = loop_time

    _cancel_album_task(entry)
    entry["task"] = context.application.create_task(
        _flush_album(context, media_group_id)
    )


# ---------- /done — စောင့်ဆိုင်းနေသော album တွေကို ချက်ချင်းထုတ်ပေးပါတယ် ----------
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    pending = [gid for gid, e in _albums.items() if e["items"]]
    if not pending:
        await update.message.reply_text("ℹ️ ထုတ်ပေးရန် စောင့်ဆိုင်းနေသော ဖိုင် မရှိပါ။")
        return
    for gid in pending:
        entry = _albums.get(gid)
        if entry:
            _cancel_album_task(entry)
        await _send_album_links(context, gid)
    await update.message.reply_text(
        f"✅ {len(pending)} အုပ်စု ထုတ်ပေးပြီးပါပြီ။",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel လုပ်ရန်", callback_data="cmd_cancel")]]
        ),
    )

# ---------- Admin commands ----------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_col.count_documents({})
    total_req = get_total_requests()
    await update.message.reply_text(f"📊 စာရင်းအင်း\n\n👥 အသုံးပြုသူဦးရေ: {total_users}\n🎬 တောင်းဆိုမှုအရေအတွက်: {total_req}")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("📢 /broadcast <message> - အသုံးပြုသူအားလုံးသို့ စာပို့ရန်။")
        return
    msg = ' '.join(context.args)
    users = get_all_users()
    count = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            count += 1
        except:
            pass
    await update.message.reply_text(f"📢 ပြန်လွှင့်ခြင်း ပြီးဆုံးပါပြီ။ လက်ခံသူ {count} ဦး။")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data:
        context.user_data.clear()
        await update.message.reply_text("✅ လက်ရှိလုပ်ဆောင်နေသော လုပ်ငန်းစဉ်ကို ဖျက်သိမ်းလိုက်ပါသည်။")
    else:
        await update.message.reply_text("❌ လက်ရှိ လုပ်ဆောင်နေသော လုပ်ငန်းစဉ် မရှိပါ။")

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("🗑️ /delete <payload> - သိမ်းဆည်းထားသော ဖိုင်တစ်ခုကို ဖျက်ရန်။")
        return
    payload = context.args[0]
    if delete_file_by_payload(payload):
        await update.message.reply_text(f"✅ ဖိုင် {payload} ကို ဖျက်လိုက်ပါသည်။")
    else:
        await update.message.reply_text(f"❌ ဖိုင် {payload} မတွေ့ပါ။")

# ---------- Admin menu ----------
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ အဒ်မင်များသာ အသုံးပြုနိုင်ပါသည်။")
        return
    keyboard = [
        [InlineKeyboardButton("🎬 Post ဖန်တီးရန်", callback_data="cmd_post")],
        [InlineKeyboardButton("📝 Post_Text ဖန်တီးရန်", callback_data="cmd_post_text")],
        [InlineKeyboardButton("📊 စာရင်းအင်းကြည့်ရန်", callback_data="cmd_stats")],
        [InlineKeyboardButton("📢 Broadcast ပို့ရန်", callback_data="cmd_broadcast")],
        [InlineKeyboardButton("❌ Cancel လုပ်ရန်", callback_data="cmd_cancel")],
        [InlineKeyboardButton("🗑️ ဖိုင်ဖျက်ရန်", callback_data="cmd_delete")],
    ]
    await update.message.reply_text("🎬 အဒ်မင် ထိန်းချုပ်မှု PANEL\n\nအောက်ပါခလုတ်များမှ သင်လိုချင်သော လုပ်ဆောင်ချက်ကို ရွေးချယ်ပါ။", reply_markup=InlineKeyboardMarkup(keyboard))

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("⛔ အဒ်မင်များသာ အသုံးပြုနိုင်ပါသည်။")
        return
    data = query.data
    if data == "cmd_post":
        await query.edit_message_text("🎬 /post command ကို ရိုက်ထည့်ပါ။")
        await post_start(update, context)
    elif data == "cmd_post_text":
        await query.edit_message_text("📝 /post_text command ကို ရိုက်ထည့်ပါ။")
        await post_text_start(update, context)
    elif data == "cmd_stats":
        await stats_command(update, context)
    elif data == "cmd_broadcast":
        await query.edit_message_text("📢 /broadcast <message> ဖြင့် စာပို့နိုင်ပါသည်။")
    elif data == "cmd_cancel":
        await cancel_command(update, context)
    elif data == "cmd_delete":
        await query.edit_message_text("🗑️ /delete <payload> ဖြင့် ဖိုင်ဖျက်နိုင်ပါသည်။")

# ============================================================
# ========== START FUNCTION ==========
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"🟢 Start from User ID: {user_id}, Args: {context.args}")

    # ----- Admin ဖြစ်ရင် Channel မစစ်ဘဲ ကျော်ပါ -----
    if is_admin(user_id):
        logger.info(f"✅ User {user_id} is admin - skipping channel check")
        if context.args:
            payload = context.args[0]
            file_id, file_name = get_file(payload)
            if not file_id:
                await update.message.reply_text("❌ လင့်ခ် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
                return
            try:
                if file_name.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    sent_msg = await context.bot.send_photo(chat_id=user_id, photo=file_id, caption=f"📂 {file_name}")
                elif file_name.endswith(('.mp4', '.mkv', '.avi')):
                    sent_msg = await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"📂 {file_name}")
                else:
                    sent_msg = await context.bot.send_document(chat_id=user_id, document=file_id, filename=file_name)

                # 🔥 သတိပေးစာ
                warning_text = (
                    "⚠️ ⚠️ ⚠️ အရေးကြီးပါတယ် ⚠️ ⚠️ ⚠️\n\n"
                    "👉ဤရုပ်ရှင်ဖိုင်များ/ဗီဒီယိုများကို 5 မိနစ်အတွင်း (မူပိုင်ခွင့်ပြဿနာများကြောင့်) ဖျက်ပါမည်။\n"
                    "👉ကျေးဇူးပြု၍ ဤဖိုင်များ/ဗီဒီယိုများအားလုံးကို သင်၏ Saved Messages များသို့ Forward လုပ်ပြီး ထိုနေရာတွင် ဇာတ်ကားအား ကြည့်ရှုပါ။\n"
                    "👉ကျွန်ုပ်၏ Channel ကို လာရောက်အားပေးမှုအတွက် ကျေးဇူးအထူးတင်ပါတယ် 🙏🙏🙏\n"
                    "👉Channel ကို Share ခြင်းဖြင့်လည်း ကူညီနိုင်ပါတယ်။\n"
                    "👉အားလုံးကို ကျေးဇူးတင်ပါတယ်။"
                )
                keyboard = [
                    [InlineKeyboardButton("🎬 Movie Channel", url="https://t.me/moviesandseriesforallwzn")],
                    [InlineKeyboardButton("🔞 Adult Channel", url="https://t.me/everyboyhobby")],
                    [InlineKeyboardButton("🎵 Music Channel", url="https://t.me/wznmusiclibary")],
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                warn_msg = await context.bot.send_message(chat_id=user_id, text=warning_text, reply_markup=reply_markup)

                context.application.create_task(delete_messages_after_delay(context, user_id, [sent_msg.message_id, warn_msg.message_id], 300))
            except Exception as e:
                await update.message.reply_text(f"❌ ဖိုင်ပို့ရာတွင် အမှားရှိသည်: {e}")
        else:
            await admin_menu(update, context)
        return

    # ----- သာမန် User အတွက် (Channel စစ်မယ်) -----
    logger.info(f"ℹ️ User {user_id} is NOT admin - checking channels")
    
    try:
        if not context.args:
            await update.message.reply_text(
                "🎬 ဖိုင်မှ Deep Link ဘော့\n\n"
                "အဒ်မင်က ဖိုင်တစ်ခုခု ပို့လိုက်လျှင် Deep Link ထုတ်ပေးပါမည်။\n"
                "အဆိုပါလင့်ခ်ကို နှိပ်ပါက လိုအပ်သော Channel များအားလုံးဝင်ပြီးမှသာ ဖိုင်ကိုရယူနိုင်ပါသည်။\n"
                "ဖိုင်ကို 5 မိနစ်အကြာတွင် အလိုအလျောက် ဖျက်ပစ်ပါမည်။"
            )
            return

        payload = context.args[0]
        file_id, file_name = get_file(payload)
        
        if not file_id:
            await update.message.reply_text(
                "❌ လင့်ခ် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။\n\n"
                "ကျေးဇူးပြု၍ Channel ရှိ Post အသစ်များမှ လင့်ခ်ကို ပြန်လည်ရယူပါ။"
            )
            return

        # 🔥 Channel အားလုံး ဝင်ထားရဲ့လား စစ်မယ်
        ok, missing = await check_all_channels(user_id, context.bot)
        
        # မဝင်သေးရင် Button တွေနဲ့ ပြမယ်
        if not ok:
            keyboard = []
            for ch in REQUIRED_CHANNELS:
                if ch in missing:
                    keyboard.append([InlineKeyboardButton(f"❌ {ch['name']} ဝင်ရန်", url=ch['invite'])])
                else:
                    keyboard.append([InlineKeyboardButton(f"✅ {ch['name']}", callback_data="joined")])
            
            keyboard.append([InlineKeyboardButton("🔄 ပြန်စစ်မယ်", callback_data="check_again")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "🎬 ဖိုင်ရယူရန် အောက်ပါ Channel များအားလုံးကို ဝင်ထားပါ။\n\n"
                "ဝင်ပြီးပါက '🔄 ပြန်စစ်မယ်' ကိုနှိပ်ပါ။",
                reply_markup=reply_markup
            )
            return

        # 🔥 Channel အားလုံးဝင်ထားပြီးရင် Video ပို့မယ်
        if file_name.endswith(('.jpg', '.jpeg', '.png', '.gif')):
            sent_msg = await context.bot.send_photo(chat_id=user_id, photo=file_id, caption=f"📂 {file_name}")
        elif file_name.endswith(('.mp4', '.mkv', '.avi')):
            sent_msg = await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"📂 {file_name}")
        else:
            sent_msg = await context.bot.send_document(chat_id=user_id, document=file_id, filename=file_name)

        # 🔥 သတိပေးစာ
        warning_text = (
            "⚠️ ⚠️ ⚠️ အရေးကြီးပါတယ် ⚠️ ⚠️ ⚠️\n\n"
            "👉ဤရုပ်ရှင်ဖိုင်များ/ဗီဒီယိုများကို 5 မိနစ်အတွင်း (မူပိုင်ခွင့်ပြဿနာများကြောင့်) ဖျက်ပါမည်။\n"
            "👉ကျေးဇူးပြု၍ ဤဖိုင်များ/ဗီဒီယိုများအားလုံးကို သင်၏ Saved Messages များသို့ Forward လုပ်ပြီး ထိုနေရာတွင် ဇာတ်ကားအား ကြည့်ရှုပါ။\n"
            "👉ကျွန်ုပ်၏ Channel ကို လာရောက်အားပေးမှုအတွက် ကျေးဇူးအထူးတင်ပါတယ် 🙏🙏🙏\n"
            "👉Channel ရေရှည်တည်တံ့ဖို့အတွက် Support ပေးချင်ပါက Wave Pay (09767011991) ကို ကူညီနိုင်ပါတယ်။\n"
            "👉Channel ကို Share ခြင်းဖြင့်လည်း ကူညီနိုင်ပါတယ်။\n"
            "👉အားလုံးကို ကျေးဇူးတင်ပါတယ်။"
        )
        
        keyboard = [
            [InlineKeyboardButton("🎬 Movie Channel", url="https://t.me/moviesandseriesforallwzn")],
            [InlineKeyboardButton("🔞 Adult Channel", url="https://t.me/everyboyhobby")],
            [InlineKeyboardButton("🎵 Music Channel", url="https://t.me/wznmusiclibary")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        warn_msg = await context.bot.send_message(chat_id=user_id, text=warning_text, reply_markup=reply_markup)
        
        context.application.create_task(delete_messages_after_delay(context, user_id, [sent_msg.message_id, warn_msg.message_id], 300))
        add_user(user_id)
        increment_requests()

    except Exception as e:
        logger.exception(f"Error in start for user {user_id}: {e}")
        await update.message.reply_text(
            "❌ စနစ်တွင် ချို့ယွင်းချက်ရှိနေပါသည်။ ကျေးဇူးပြု၍ မိနစ်အနည်းငယ်အကြာ ပြန်ကြိုးစားပါ။"
        )

# ---------- Callback Query Handler (Refresh Button အတွက်) ----------
async def check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "check_again":
        await query.edit_message_text(
            "🔄 ကျေးဇူးပြု၍ ဇာတ်ကားပို့စ်အောက်ကလင့်ခ်ကို ထပ်မံနှိပ်ပြီးတော့ ဇာတ်ကားကိုရယူနိုင်ပါပြီ။"
        )

# ---------- Webhook ----------
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
USE_POLLING = os.environ.get("USE_POLLING", "").strip().lower() in ("1", "true", "yes")

# Render free tier မှာ service အိပ်သွားတတ်လို့ webhook အလုပ်မလုပ်နိုင်ပါ။
# USE_POLLING=true နဲ့ polling mode သုံးနိုင်ပါတယ်။
if not USE_POLLING and not WEBHOOK_URL:
    USE_POLLING = True
    logger.warning("⚠️ WEBHOOK_URL not set — falling back to POLLING mode")

MODE = "POLLING" if USE_POLLING else "WEBHOOK"
logger.info(f"🌐 Mode: {MODE}")
if not USE_POLLING:
    logger.info(f"🌐 Webhook URL: {WEBHOOK_URL}")

async def post_init(app_obj):
    """Webhook mode မှာသာ webhook set မယ်။"""
    if not USE_POLLING and WEBHOOK_URL:
        await app_obj.bot.set_webhook(
            WEBHOOK_URL,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        logger.info(f"✅ Webhook set to {WEBHOOK_URL}")

async def post_shutdown(app_obj):
    if not USE_POLLING and WEBHOOK_URL:
        try:
            await app_obj.bot.delete_webhook(drop_pending_updates=False)
            logger.info("🧹 Webhook removed")
        except Exception as e:
            logger.warning(f"Webhook delete failed: {e}")


telegram_app = (
    Application.builder()
    .token(TOKEN)
    .post_init(post_init)
    .post_shutdown(post_shutdown)
    .build()
)

telegram_app.add_handler(ConversationHandler(
    entry_points=[CommandHandler('post', post_start)],
    states={
        POST_PHOTO: [MessageHandler(
            (filters.PHOTO | filters.VIDEO | filters.Document.ALL)
            | (filters.TEXT & ~filters.COMMAND), post_photo)],
        POST_MOVIE: [MessageHandler(filters.VIDEO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), post_movie)],
    },
    fallbacks=[CommandHandler('cancel', cancel_post)],
))
telegram_app.add_handler(ConversationHandler(
    entry_points=[CommandHandler('post_text', post_text_start)],
    states={
        POST_TEXT_PHOTO: [MessageHandler(
            (filters.PHOTO | filters.VIDEO | filters.Document.ALL)
            | (filters.TEXT & ~filters.COMMAND), post_text_photo)],
        POST_TEXT_MOVIE: [MessageHandler(filters.VIDEO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), post_text_movie)],
    },
    fallbacks=[CommandHandler('cancel', cancel_post_text)],
))

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("stats", stats_command))
telegram_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_app.add_handler(CommandHandler("cancel", cancel_command))
telegram_app.add_handler(CommandHandler("delete", delete_command))
telegram_app.add_handler(CommandHandler("done", done_command))
telegram_app.add_handler(CommandHandler("menu", admin_menu))
telegram_app.add_handler(CallbackQueryHandler(menu_callback, pattern="cmd_"))
telegram_app.add_handler(CallbackQueryHandler(check_callback, pattern="check_again"))
telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_file_upload))

# ============================================================
# 🌐 Flask webhook — run_coroutine_threadsafe လုပ်ရန် loop ကို
#    module-level မှာ ကြိုတင်ထားရပါတယ်။ ဒီမဟုတ်ရင် webhook mode တွင်
#    `/webhook` handler က NameError ဖြစ်ပြီး bot အလုပ်မလုပ်ပါ။
# ============================================================
loop = None


@app.route('/', methods=['GET'])
def home():
    return "Bot is running!", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    if loop is None or not loop.is_running():
        logger.error("❌ Event loop not running — webhook update dropped")
        return "error", 503
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)
        logger.info(f"📨 Webhook received update")
        asyncio.run_coroutine_threadsafe(
            telegram_app.process_update(update), loop
        )
        return "ok", 200
    except Exception as e:
        logger.exception("Webhook error")
        return "error", 500

def start_flask():
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🌐 Flask listening on 0.0.0.0:{port}")
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    if USE_POLLING:
        # ---------- Polling mode (Render free tier / local) ----------
        # Flask ကို thread ထဲမှာ run ပြီး run_polling က event loop ကို ကိုင်မယ်။
        threading.Thread(target=start_flask, daemon=True).start()
        logger.info("🚀 Bot started (polling mode)")
        telegram_app.run_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
            close_loop=False,
        )
    else:
        # ---------- Webhook mode (production / always-on) ----------
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(telegram_app.initialize())
        loop.run_until_complete(telegram_app.start())
        threading.Thread(target=start_flask, daemon=True).start()
        logger.info("🚀 Bot started (webhook mode)")
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            loop.run_until_complete(telegram_app.shutdown())
            loop.close()
