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

def save_file(payload, file_id, file_name):
    files_col.update_one({"payload": payload}, {"$set": {"file_id": file_id, "file_name": file_name}}, upsert=True)

def get_file(payload):
    doc = files_col.find_one({"payload": payload})
    if doc:
        return doc["file_id"], doc["file_name"]
    return None, None

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

# ========== /newlink ==========
async def newlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only")
        return
    await update.message.reply_text("📤 Send me a file (document, video, photo, audio) to get a deep link.")
    context.user_data['waiting_newlink'] = True

async def newlink_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_newlink'):
        return
    if not is_admin(update.effective_user.id):
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
        await message.reply_text("Please send a valid file.")
        return
    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    await message.reply_text(
        f"🔗 **Deep Link ready!**\n\n"
        f"File: `{file_name}`\n"
        f"Link:\n{deep_link}\n\n"
        f"Anyone who clicks this link (after joining required channels) can get the file."
    )
    context.user_data.pop('waiting_newlink', None)

# ========== /post (without text) ==========
POST_PHOTO, POST_MOVIE = range(2)

async def post_no_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END
    await update.message.reply_text("📸 Please send the poster image.")
    return POST_PHOTO

async def post_no_text_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Please send a photo.")
        return POST_PHOTO
    context.user_data['poster'] = update.message.photo[-1].file_id
    await update.message.reply_text("🎬 Now send the movie file (video or document).")
    return POST_MOVIE

async def post_no_text_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await message.reply_text("Poster not found. Restart /post.")
        return ConversationHandler.END

    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    caption = "🎬 **New Movie Post**\n\nClick button below to get movie."
    keyboard = [[InlineKeyboardButton("🎬 Get Movie", url=deep_link)]]
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.reply_photo(photo=poster, caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
    await message.reply_text("✅ Post created successfully!")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ========== /post_text (with custom text) ==========
POST_TEXT_PHOTO, POST_TEXT_CAPTION, POST_TEXT_MOVIE = range(10, 13)

async def post_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END
    await update.message.reply_text("📸 Please send the poster image.")
    return POST_TEXT_PHOTO

async def post_text_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Please send a photo.")
        return POST_TEXT_PHOTO
    context.user_data['poster'] = update.message.photo[-1].file_id
    await update.message.reply_text("✍️ Now send the movie description / synopsis (text).")
    return POST_TEXT_CAPTION

async def post_text_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption_text = update.message.text
    context.user_data['caption_text'] = caption_text
    context.user_data['telegraph_url'] = None

    if len(caption_text) > 1024:
        await update.message.reply_text("⏳ Text too long, creating Telegraph page...")
        try:
            title = f"Movie Synopsis - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            page_url = await create_telegraph_page(title, caption_text)
            if page_url:
                context.user_data['telegraph_url'] = page_url
                await update.message.reply_text(f"✅ Telegraph page created:\n{page_url}")
            else:
                await update.message.reply_text("Failed to create Telegraph page. Text will be used as-is.")
        except Exception as e:
            logger.error(f"Telegraph error: {e}")
            await update.message.reply_text("Telegraph creation failed, using plain text.")
    else:
        pass

    await update.message.reply_text("🎬 Now send the movie file (video or document).")
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
        await message.reply_text("Please send a video file.")
        return POST_TEXT_MOVIE

    poster = context.user_data.get('poster')
    caption_text = context.user_data.get('caption_text', '')
    telegraph_url = context.user_data.get('telegraph_url')
    if not poster:
        await message.reply_text("Poster missing. Restart /post_text.")
        return ConversationHandler.END

    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    if telegraph_url:
        preview = caption_text[:300] + "..." if len(caption_text) > 300 else caption_text
        caption = f"{preview}\n\n📖 [Read full synopsis]({telegraph_url})\n\n🎬 Click below to get the movie."
        parse_mode = "Markdown"
    else:
        caption = f"{caption_text}\n\n🎬 Click below to get the movie."
        parse_mode = None

    keyboard = [[InlineKeyboardButton("🎬 Get Movie", url=deep_link)]]
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.reply_photo(photo=poster, caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
    await message.reply_text("✅ Post created successfully!")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_post_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- Deep link handler (for users and admin) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # If admin just opening the bot
    if is_admin(user_id) and not context.args:
        await update.message.reply_text(
            "🎬 **Admin Panel**\n\n"
            "/newlink - Send any file to get a deep link instantly.\n"
            "/post - Create a movie post (poster + video).\n"
            "/post_text - Create a movie post with description (poster + text + video)."
        )
        return

    # If it's a deep link click (has payload)
    if context.args:
        payload = context.args[0]
        file_id, file_name = get_file(payload)
        if not file_id:
            await update.message.reply_text("❌ Invalid or expired link.")
            return

        # Admin bypass channel check
        if not is_admin(user_id):
            ok, missing_ch = await check_all_channels(user_id, context.bot)
            if not ok:
                msg = "🎬 **Please join all required channels to get the file.**\n\n"
                for ch in REQUIRED_CHANNELS:
                    status = "✅" if ch["id"] != missing_ch["id"] else "❌"
                    msg += f"{status} {ch['name']}: [Join]({ch['invite']})\n"
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
                "⚠️ ⚠️ ⚠️ **IMPORTANT** ⚠️ ⚠️ ⚠️\n\n"
                "This file will be deleted in 5 minutes (due to copyright issues).\n\n"
                "Please forward this file to your Saved Messages to keep it.\n\n"
                "🙏 Thanks for supporting our channels.\n\n"
                "!!! IMPORTANT !!!\n"
                "This file will be deleted in 5 mins.\n"
                "Please forward to Saved Messages now."
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
            await update.message.reply_text(f"❌ Error: {e}")
        return

    # Non-admin users without payload
    await update.message.reply_text(
        "🎬 **File to Deep Link Bot**\n\n"
        "Admin will provide deep links. Click a link to get the file (after joining required channels)."
    )

# ---------- Webhook ----------
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL not set")
    sys.exit(1)

telegram_app = Application.builder().token(TOKEN).build()

# Handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("newlink", newlink_command))
telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, newlink_receive))

# /post without text
telegram_app.add_handler(ConversationHandler(
    entry_points=[CommandHandler('post', post_no_text_start)],
    states={
        POST_PHOTO: [MessageHandler(filters.PHOTO, post_no_text_photo)],
        POST_MOVIE: [MessageHandler(filters.VIDEO | filters.Document.ALL, post_no_text_movie)],
    },
    fallbacks=[CommandHandler('cancel', cancel_post)],
))

# /post_text with text
telegram_app.add_handler(ConversationHandler(
    entry_points=[CommandHandler('post_text', post_text_start)],
    states={
        POST_TEXT_PHOTO: [MessageHandler(filters.PHOTO, post_text_photo)],
        POST_TEXT_CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, post_text_caption)],
        POST_TEXT_MOVIE: [MessageHandler(filters.VIDEO | filters.Document.ALL, post_text_movie)],
    },
    fallbacks=[CommandHandler('cancel', cancel_post_text)],
))

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
        
