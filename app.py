import os
import asyncio
import threading
import logging
import secrets
from datetime import datetime
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from telegram.helpers import create_deep_linked_url
from pymongo import MongoClient

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
    logger.warning("SSL error, retrying with tlsAllowInvalidCertificates")
    mongo_client = MongoClient(MONGO_URI, tlsAllowInvalidCertificates=True, serverSelectionTimeoutMS=5000)

db = mongo_client["file_share_bot"]
files_col = db["files"]

def save_file(payload, file_id, file_name):
    files_col.update_one({"payload": payload}, {"$set": {"file_id": file_id, "file_name": file_name}}, upsert=True)

def get_file(payload):
    doc = files_col.find_one({"payload": payload})
    if doc:
        return doc["file_id"], doc["file_name"]
    return None, None

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

# ---------- Existing: Admin file upload → Deep Link (keep as is) ----------
async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Admin only.")
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
        await message.reply_text("Please send a file (document, video, photo, or audio).")
        return

    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    await message.reply_text(
        f"🔗 **Your Deep Link is ready!**\n\n"
        f"**File:** `{file_name}`\n"
        f"**Link:**\n{deep_link}\n\n"
        f"Anyone who clicks this link will get the file (after joining required channels)."
    )

# ========== /post command (no initial text) ==========
POST_PHOTO, POST_MOVIE = range(2)

async def post_start_no_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END
    await update.message.reply_text("📸 Please send the poster image.")
    return POST_PHOTO

async def post_receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Please send a photo.")
        return POST_PHOTO
    context.user_data['poster'] = update.message.photo[-1].file_id
    await update.message.reply_text("🎬 Now send the movie file (video or document).")
    return POST_MOVIE

async def post_receive_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await message.reply_text("Please send a video file (mp4, mkv, etc.)")
        return POST_MOVIE

    poster = context.user_data.get('poster')
    if not poster:
        await message.reply_text("Poster not found. Please restart /post.")
        return ConversationHandler.END

    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    caption = "🎬 **New Movie Post**\n\nClick the button below to get the movie."
    keyboard = [[InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)]]
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.reply_photo(photo=poster, caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
    await message.reply_text("✅ Post created successfully!")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Post creation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ========== /post with text: e.g., /post This is my movie caption ==========
POST_TEXT_PHOTO, POST_TEXT_MOVIE = range(10, 12)

async def post_with_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END
    # Capture the text after /post command
    if context.args:
        context.user_data['custom_text'] = ' '.join(context.args)
    else:
        await update.message.reply_text("Please use: /post Your caption text here")
        return ConversationHandler.END
    await update.message.reply_text("📸 Now send the poster image.")
    return POST_TEXT_PHOTO

async def post_text_receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Please send a photo.")
        return POST_TEXT_PHOTO
    context.user_data['poster'] = update.message.photo[-1].file_id
    await update.message.reply_text("🎬 Now send the movie file.")
    return POST_TEXT_MOVIE

async def post_text_receive_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await message.reply_text("Please send a video file.")
        return POST_TEXT_MOVIE

    poster = context.user_data.get('poster')
    custom_text = context.user_data.get('custom_text', '')
    if not poster:
        await message.reply_text("Poster not found. Please restart /post.")
        return ConversationHandler.END

    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    caption = f"{custom_text}\n\n🎬 Click below to get the movie."
    keyboard = [[InlineKeyboardButton("🎬 ဇာတ်ကားရယူရန်", url=deep_link)]]
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.reply_photo(photo=poster, caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
    await message.reply_text("✅ Post created successfully!")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_post_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Post creation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ========== Deep link handler for users (Admin bypass channel check) ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Admin bypass channel check
    if is_admin(user_id):
        # If admin, just send welcome or menu
        if not context.args:
            await update.message.reply_text(
                "🎬 **Admin Panel**\n\n"
                "Send any file to get a deep link.\n"
                "Use /post to create a movie post with poster + video.\n"
                "Use /post Your caption to create a post with custom text."
            )
        else:
            payload = context.args[0]
            file_id, file_name = get_file(payload)
            if not file_id:
                await update.message.reply_text("❌ Invalid or expired link.")
                return
            # Admin gets file directly without channel check
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
                
                async def delete_files():
                    await asyncio.sleep(300)
                    try:
                        await context.bot.delete_message(chat_id=user_id, message_id=sent_msg.message_id)
                    except:
                        pass
                    try:
                        await context.bot.delete_message(chat_id=user_id, message_id=warn_msg.message_id)
                    except:
                        pass
                asyncio.create_task(delete_files())
            except Exception as e:
                await update.message.reply_text(f"❌ Error sending file: {e}")
        return
    
    # For non-admin users
    if not context.args:
        await update.message.reply_text(
            "🎬 **File to Deep Link Bot**\n\n"
            "Admin မှ ဖိုင်တစ်ခုခု ပို့လိုက်လျှင် Deep Link ထုတ်ပေးပါမည်။\n"
            "အဆိုပါလင့်ကို နှိပ်ပါက လိုအပ်သော Channel များအားလုံးဝင်ပြီးမှ ဖိုင်ရယူနိုင်ပါသည်။\n"
            "ဖိုင်ကို 5 မိနစ်အကြာတွင် အလိုအလျောက် ဖျက်ပစ်ပါမည်။"
        )
        return

    payload = context.args[0]
    file_id, file_name = get_file(payload)
    if not file_id:
        await update.message.reply_text("❌ Invalid or expired link.")
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
        
        async def delete_files():
            await asyncio.sleep(300)
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=sent_msg.message_id)
            except:
                pass
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=warn_msg.message_id)
            except:
                pass
        asyncio.create_task(delete_files())
    except Exception as e:
        await update.message.reply_text(f"❌ Error sending file: {e}")

# ---------- Webhook ----------
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL not set")
    sys.exit(1)

telegram_app = Application.builder().token(TOKEN).build()

# Command handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_file_upload))

# /post conversation (no initial text)
post_conv = ConversationHandler(
    entry_points=[CommandHandler('post', post_start_no_text)],
    states={
        POST_PHOTO: [MessageHandler(filters.PHOTO, post_receive_photo)],
        POST_MOVIE: [MessageHandler(filters.VIDEO | filters.Document.ALL, post_receive_movie)],
    },
    fallbacks=[CommandHandler('cancel', cancel_post)],
)
telegram_app.add_handler(post_conv)

# /post with text conversation (command with arguments)
post_text_conv = ConversationHandler(
    entry_points=[CommandHandler('post', post_with_text_start, filters=filters.COMMAND)],
    states={
        POST_TEXT_PHOTO: [MessageHandler(filters.PHOTO, post_text_receive_photo)],
        POST_TEXT_MOVIE: [MessageHandler(filters.VIDEO | filters.Document.ALL, post_text_receive_movie)],
    },
    fallbacks=[CommandHandler('cancel', cancel_post_text)],
)
telegram_app.add_handler(post_text_conv)

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
