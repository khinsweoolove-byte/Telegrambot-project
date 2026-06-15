import os
import asyncio
import threading
import logging
import secrets
from datetime import datetime
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
BOT_USERNAME = os.environ.get("BOT_USERNAME")
if not TOKEN or not BOT_USERNAME:
    logger.error("TELEGRAM_TOKEN and BOT_USERNAME required")
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
    return secrets.token_urlsafe(12)

async def is_member_of_channel(user_id, channel_id, bot):
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def check_all_channels(user_id, bot):
    for ch in REQUIRED_CHANNELS:
        if not await is_member_of_channel(user_id, ch["id"], bot):
            return False, ch
    return True, None

# ---------- Auto-delete helper ----------
async def delete_messages_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_ids: list, delay_seconds: int = 300):
    await asyncio.sleep(delay_seconds)
    for msg_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            logger.info(f"Auto-deleted message {msg_id} in chat {chat_id}")
        except Exception as e:
            logger.warning(f"Failed to delete message {msg_id}: {e}")

# ---------- Conversation states ----------
POST_PHOTO, POST_MOVIE = range(2)
POST_TEXT_PHOTO, POST_TEXT_CAPTION, POST_TEXT_MOVIE = range(10, 13)

# ---------- /post conversation ----------
async def post_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ အဒ်မင်များသာ အသုံးပြုနိုင်ပါသည်။")
        return ConversationHandler.END
    await update.message.reply_text("📸 ပိုစတာ (Poster) ပုံတစ်ပုံ ပို့ပေးပါ။ ပုံပို့ရာတွင် **Caption** ထည့်ပေးနိုင်ပါသည်။")
    return POST_PHOTO

async def post_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("ကျေးဇူးပြု၍ ဓာတ်ပုံတစ်ပုံ ပို့ပေးပါ။")
        return POST_PHOTO
    context.user_data['poster'] = update.message.photo[-1].file_id
    # Save caption if provided
    if update.message.caption:
        context.user_data['photo_caption'] = update.message.caption
    else:
        context.user_data['photo_caption'] = None
    await update.message.reply_text("🎬 ယခု ရုပ်ရှင်ဖိုင် (video or document) ကို ပို့ပေးပါ။")
    return POST_MOVIE

async def post_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    file_obj = None
    file_name = "movie"
    if message.video:
        file_obj = message.video
        file_name = file_obj.file_name or "video"
    elif message.document and message.document.mime_type and message.document.mime_type.startswith('video/'):
        file_obj = message.document
        file_name = file_obj.file_name or "movie"
    else:
        await message.reply_text("ကျေးဇူးပြု၍ ဗီဒီယိုဖိုင် (mp4, mkv, etc.) ပို့ပေးပါ။")
        return POST_MOVIE

    poster = context.user_data.get('poster')
    photo_caption = context.user_data.get('photo_caption')
    if not poster:
        await message.reply_text("ပိုစတာ မတွေ့ပါ။ /post ဖြင့် ပြန်စတင်ပါ။")
        return ConversationHandler.END

    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    # Build caption with optional user-provided caption
    if photo_caption:
        caption = f"{photo_caption}\n\n🎬 ရုပ်ရှင်ရယူရန် အောက်ပါခလုတ်ကို နှိပ်ပါ။"
    else:
        caption = "🎬 **ရုပ်ရှင်အသစ်**\n\nရုပ်ရှင်ရယူရန် အောက်ပါခလုတ်ကို နှိပ်ပါ။"
    keyboard = [[InlineKeyboardButton("🎬 ရုပ်ရှင်ရယူရန်", url=deep_link)]]
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.reply_photo(photo=poster, caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
    await message.reply_text("✅ ပိုစတာ ဖန်တီးခြင်း အောင်မြင်ပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- /post_text conversation ----------
async def post_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ အဒ်မင်များသာ အသုံးပြုနိုင်ပါသည်။")
        return ConversationHandler.END
    await update.message.reply_text("📸 ပိုစတာ (Poster) ပုံတစ်ပုံ ပို့ပေးပါ။ ပုံပို့ရာတွင် **Caption** ထည့်ပေးနိုင်ပါသည်။")
    return POST_TEXT_PHOTO

async def post_text_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("ကျေးဇူးပြု၍ ဓာတ်ပုံတစ်ပုံ ပို့ပေးပါ။")
        return POST_TEXT_PHOTO
    context.user_data['poster'] = update.message.photo[-1].file_id
    # Save caption if provided
    if update.message.caption:
        context.user_data['photo_caption'] = update.message.caption
    else:
        context.user_data['photo_caption'] = None
    await update.message.reply_text("✍️ ယခု ဇာတ်ကားအကြောင်း စာသား (ဇာတ်ညွှန်း) ကို ပို့ပေးပါ။")
    return POST_TEXT_CAPTION

async def post_text_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption_text = update.message.text
    context.user_data['caption_text'] = caption_text
    context.user_data['telegraph_url'] = None

    if len(caption_text) > 1024:
        await update.message.reply_text("⏳ စာသားရှည်နေပါသည်။ Telegraph စာမျက်နှာ ဖန်တီးနေပါပြီ...")
        try:
            title = f"Movie Synopsis - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            page_url = await create_telegraph_page(title, caption_text)
            if page_url:
                context.user_data['telegraph_url'] = page_url
                await update.message.reply_text(f"✅ Telegraph စာမျက်နှာ ဖန်တီးပြီးပါပြီ။\n{page_url}")
            else:
                await update.message.reply_text("❌ Telegraph ဖန်တီးရာတွင် အမှား။ စာသားကို အတိုင်းသုံးပါမည်။")
        except Exception as e:
            logger.error(f"Telegraph error: {e}")
            await update.message.reply_text("❌ Telegraph စာမျက်နှာ ဖန်တီးရာတွင် ချို့ယွင်းချက်ရှိသည်။")
    await update.message.reply_text("🎬 ယခု ရုပ်ရှင်ဖိုင် (video or document) ကို ပို့ပေးပါ။")
    return POST_TEXT_MOVIE

async def post_text_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    file_obj = None
    file_name = "movie"
    if message.video:
        file_obj = message.video
        file_name = file_obj.file_name or "video"
    elif message.document and message.document.mime_type and message.document.mime_type.startswith('video/'):
        file_obj = message.document
        file_name = file_obj.file_name or "movie"
    else:
        await message.reply_text("ကျေးဇူးပြု၍ ဗီဒီယိုဖိုင် ပို့ပေးပါ။")
        return POST_TEXT_MOVIE

    poster = context.user_data.get('poster')
    photo_caption = context.user_data.get('photo_caption')
    caption_text = context.user_data.get('caption_text', '')
    telegraph_url = context.user_data.get('telegraph_url')
    if not poster:
        await message.reply_text("ပိုစတာ မတွေ့ပါ။ /post_text ဖြင့် ပြန်စတင်ပါ။")
        return ConversationHandler.END

    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    # Build the final caption
    if telegraph_url:
        preview = caption_text[:300] + "..." if len(caption_text) > 300 else caption_text
        desc_part = f"{preview}\n\n📖 [ဇာတ်ညွှန်းအပြည့်အစုံဖတ်ရန်]({telegraph_url})"
        parse_mode = "Markdown"
    else:
        desc_part = caption_text
        parse_mode = None

    if photo_caption:
        final_caption = f"{photo_caption}\n\n{desc_part}\n\n🎬 ရုပ်ရှင်ရယူရန် အောက်ပါခလုတ်ကို နှိပ်ပါ။"
    else:
        final_caption = f"{desc_part}\n\n🎬 ရုပ်ရှင်ရယူရန် အောက်ပါခလုတ်ကို နှိပ်ပါ။"

    keyboard = [[InlineKeyboardButton("🎬 ရုပ်ရှင်ရယူရန်", url=deep_link)]]
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.reply_photo(photo=poster, caption=final_caption, reply_markup=reply_markup, parse_mode=parse_mode)
    await message.reply_text("✅ ပိုစတာ ဖန်တီးခြင်း အောင်မြင်ပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_post_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- Standalone file upload -> Instant deep link ----------
async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    if context.user_data:
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

    await message.reply_text(
        f"🔗 **သင်၏ Deep Link အဆင်သင့်ဖြစ်ပါပြီ။**\n\n"
        f"**ဖိုင်အမည်:** `{file_name}`\n"
        f"**လင့်ခ်:**\n{deep_link}\n\n"
        f"ဤလင့်ခ်ကို နှိပ်သူတိုင်း (လိုအပ်သော Channel များဝင်ပြီးပါက) ဖိုင်ကို ရယူနိုင်ပါသည်။"
    )

# ---------- Admin commands ----------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_col.count_documents({})
    total_req = get_total_requests()
    await update.message.reply_text(f"📊 **စာရင်းအင်း**\n\n👥 အသုံးပြုသူဦးရေ: {total_users}\n🎬 တောင်းဆိုမှုအရေအတွက်: {total_req}", parse_mode="Markdown")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("📢 `/broadcast <message>` - အသုံးပြုသူအားလုံးသို့ စာပို့ရန်။")
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
    if not context.user_data:
        await update.message.reply_text("❌ လက်ရှိ လုပ်ဆောင်နေသော လုပ်ငန်းစဉ် မရှိပါ။")
        return
    context.user_data.clear()
    await update.message.reply_text("✅ လက်ရှိလုပ်ဆောင်နေသော လုပ်ငန်းစဉ်ကို ဖျက်သိမ်းလိုက်ပါသည်။")

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("🗑️ `/delete <payload>` - သိမ်းဆည်းထားသော ဖိုင်တစ်ခုကို ဖျက်ရန်။")
        return
    payload = context.args[0]
    if delete_file_by_payload(payload):
        await update.message.reply_text(f"✅ ဖိုင် `{payload}` ကို ဖျက်လိုက်ပါသည်။", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ ဖိုင် `{payload}` မတွေ့ပါ။", parse_mode="Markdown")

# ========== ADMIN MENU WITH BUTTONS ==========
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
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
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🎬 **ADMIN ထိန်းချုပ်မှု PANEL**\n\n"
        "အောက်ပါခလုတ်များမှ သင်လိုချင်သော လုပ်ဆောင်ချက်ကို ရွေးချယ်ပါ။",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

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
        await query.edit_message_text("📊 /stats command ကို ရိုက်ထည့်ပါ။")
        await stats_command(update, context)
    elif data == "cmd_broadcast":
        await query.edit_message_text("📢 `/broadcast <message>` - အသုံးပြုသူအားလုံးသို့ စာပို့ရန်။")
    elif data == "cmd_cancel":
        await query.edit_message_text("❌ /cancel command ကို ရိုက်ထည့်ပါ။")
        await cancel_command(update, context)
    elif data == "cmd_delete":
        await query.edit_message_text("🗑️ `/delete <payload>` - သိမ်းဆည်းထားသော ဖိုင်တစ်ခုကို ဖျက်ရန်။")

# ---------- Start handler (Admin panel with buttons + User deep link) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_admin(user_id):
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

                warning_text = (
                    "⚠️ ⚠️ ⚠️ **အရေးကြီးပါတယ်** ⚠️ ⚠️ ⚠️\n\n"
                    "ဤရုပ်ရှင်ဖိုင်များ/ဗီဒီယိုများကို 5 မိနစ်အတွင်း (မူပိုင်ခွင့်ပြဿနာများကြောင့်) ဖျက်ပါမည်။\n\n"
                    "ကျေးဇူးပြု၍ ဤဖိုင်များ/ဗီဒီယိုများအားလုံးကို သင်၏ Saved Messages များသို့ Forward လုပ်ပြီး ထိုနေရာတွင် ဇာတ်ကားအား ကြည့်ရှုပါ။\n\n"
                    "ကျွန်ုပ်၏ Channel ကို လာရောက်အားပေးမှုအတွက် ကျေးဇူးအထူးတင်ပါတယ် 🙏🙏🙏\n\n"
                    "Channel ရေရှည်တည်တံ့ဖို့အတွက် Support ပေးချင်ပါက Wave Pay (09767011991) ကို ကူညီနိုင်ပါတယ်။\n\n"
                    "အားလုံးကို ကျေးဇူးတင်ပါတယ်။\n\n"
                    "!!! IMPORTANT !!!\n"
                    "This Movie Files/Videos will be deleted in 5 mins (Due to Copyright Issues).\n"
                    "Please forward these ALL Files/Videos to your Saved Messages and start downloading there."
                )
                keyboard = [
                    [InlineKeyboardButton("🎬 Movie Channel", url="https://t.me/moviesandseriesforallwzn")],
                    [InlineKeyboardButton("🔞 Adult Channel", url="https://t.me/everyboyhobby")],
                    [InlineKeyboardButton("🎵 Music Channel", url="https://t.me/wznmusiclibary")],
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                warn_msg = await context.bot.send_message(chat_id=user_id, text=warning_text, reply_markup=reply_markup, parse_mode="Markdown")

                asyncio.create_task(
                    delete_messages_after_delay(
                        context,
                        chat_id=user_id,
                        message_ids=[sent_msg.message_id, warn_msg.message_id],
                        delay_seconds=300
                    )
                )
            except Exception as e:
                await update.message.reply_text(f"❌ ဖိုင်ပို့ရာတွင် အမှားရှိသည်: {e}")
        else:
            await admin_menu(update, context)
        return

    # Non-admin users
    if not context.args:
        await update.message.reply_text(
            "🎬 **ဖိုင်မှ Deep Link ဘော့**\n\n"
            "အဒ်မင်က ဖိုင်တစ်ခုခု ပို့လိုက်လျှင် Deep Link ထုတ်ပေးပါမည်။\n"
            "အဆိုပါလင့်ခ်ကို နှိပ်ပါက လိုအပ်သော Channel များအားလုံးဝင်ပြီးမှသာ ဖိုင်ကိုရယူနိုင်ပါသည်။\n"
            "ဖိုင်ကို 5 မိနစ်အကြာတွင် အလိုအလျောက် ဖျက်ပစ်ပါမည်။"
        )
        return

    payload = context.args[0]
    file_id, file_name = get_file(payload)
    if not file_id:
        await update.message.reply_text("❌ လင့်ခ် မမှန်ကန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
        return

    ok, missing_ch = await check_all_channels(user_id, context.bot)
    if not ok:
        msg = "🎬 **ဖိုင်ရယူရန် အောက်ပါ Channel များအားလုံးကို ဝင်ထားပါ။**\n\n"
        for ch in REQUIRED_CHANNELS:
            status = "✅" if ch["id"] != missing_ch["id"] else "❌"
            msg += f"{status} {ch['name']}: [ဝင်ရန်]({ch['invite']})\n"
        await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
        return

    try:
        if file_name.endswith(('.jpg', '.jpeg', '.png', '.gif')):
            sent_msg = await context.bot.send_photo(chat_id=user_id, photo=file_id, caption=f"📂 {file_name}")
        elif file_name.endswith(('.mp4', '.mkv', '.avi')):
            sent_msg = await context.bot.send_video(chat_id=user_id, video=file_id, caption=f"📂 {file_name}")
        else:
            sent_msg = await context.bot.send_document(chat_id=user_id, document=file_id, filename=file_name)

        warning_text = (
            "⚠️ ⚠️ ⚠️ **အရေးကြီးပါတယ်** ⚠️ ⚠️ ⚠️\n\n"
            "ဤရုပ်ရှင်ဖိုင်များ/ဗီဒီယိုများကို 5 မိနစ်အတွင်း (မူပိုင်ခွင့်ပြဿနာများကြောင့်) ဖျက်ပါမည်။\n\n"
            "ကျေးဇူးပြု၍ ဤဖိုင်များ/ဗီဒီယိုများအားလုံးကို သင်၏ Saved Messages များသို့ Forward လုပ်ပြီး ထိုနေရာတွင် ဇာတ်ကားအား ကြည့်ရှုပါ။\n\n"
            "ကျွန်ုပ်၏ Channel ကို လာရောက်အားပေးမှုအတွက် ကျေးဇူးအထူးတင်ပါတယ် 🙏🙏🙏\n\n"
            "Channel ရေရှည်တည်တံ့ဖို့အတွက် Support ပေးချင်ပါက Wave Pay (09767011991) ကို ကူညီနိုင်ပါတယ်။\n\n"
            "အားလုံးကို ကျေးဇူးတင်ပါတယ်။\n\n"
            "!!! IMPORTANT !!!\n"
            "This Movie Files/Videos will be deleted in 5 mins (Due to Copyright Issues).\n"
            "Please forward these ALL Files/Videos to your Saved Messages and start downloading there."
        )
        keyboard = [
            [InlineKeyboardButton("🎬 Movie Channel", url="https://t.me/moviesandseriesforallwzn")],
            [InlineKeyboardButton("🔞 Adult Channel", url="https://t.me/everyboyhobby")],
            [InlineKeyboardButton("🎵 Music Channel", url="https://t.me/wznmusiclibary")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        warn_msg = await context.bot.send_message(chat_id=user_id, text=warning_text, reply_markup=reply_markup, parse_mode="Markdown")

        asyncio.create_task(
            delete_messages_after_delay(
                context,
                chat_id=user_id,
                message_ids=[sent_msg.message_id, warn_msg.message_id],
                delay_seconds=300
            )
        )
        add_user(user_id)
        increment_requests()
    except Exception as e:
        await update.message.reply_text(f"❌ ဖိုင်ပို့ရာတွင် အမှားရှိသည်: {e}")

# ---------- Webhook ----------
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL not set")
    sys.exit(1)

telegram_app = Application.builder().token(TOKEN).build()

# Conversation handlers
telegram_app.add_handler(ConversationHandler(
    entry_points=[CommandHandler('post', post_start)],
    states={POST_PHOTO: [MessageHandler(filters.PHOTO, post_photo)],
            POST_MOVIE: [MessageHandler(filters.VIDEO | filters.Document.ALL, post_movie)]},
    fallbacks=[CommandHandler('cancel', cancel_post)],
))
telegram_app.add_handler(ConversationHandler(
    entry_points=[CommandHandler('post_text', post_text_start)],
    states={POST_TEXT_PHOTO: [MessageHandler(filters.PHOTO, post_text_photo)],
            POST_TEXT_CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, post_text_caption)],
            POST_TEXT_MOVIE: [MessageHandler(filters.VIDEO | filters.Document.ALL, post_text_movie)]},
    fallbacks=[CommandHandler('cancel', cancel_post_text)],
))

# Command handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("stats", stats_command))
telegram_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_app.add_handler(CommandHandler("cancel", cancel_command))
telegram_app.add_handler(CommandHandler("delete", delete_command))
telegram_app.add_handler(CommandHandler("menu", admin_menu))
telegram_app.add_handler(CallbackQueryHandler(menu_callback, pattern="cmd_"))

# File upload handler (must be last)
telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_file_upload))

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)
        asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), loop)
        return "ok", 200
    except Exception as e:
        logger.exception("Webhook error")
        return "error", 500

def start_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

async def set_webhook():
    await telegram_app.bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telegram_app.initialize())
    loop.run_until_complete(set_webhook())
    threading.Thread(target=start_flask, daemon=True).start()
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        loop.run_until_complete(telegram_app.shutdown())
