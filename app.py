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

# ---------- Auto-delete helper (webhook-safe) ----------
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
POST_TEXT_PHOTO, POST_TEXT_CAPTION, POST_TEXT_MOVIE = range(10, 13)

# ---------- /post ----------
async def post_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END
    await update.message.reply_text("📸 Send poster image (caption allowed).")
    return POST_PHOTO

async def post_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Send a photo.")
        return POST_PHOTO
    context.user_data['poster'] = update.message.photo[-1].file_id
    if update.message.caption:
        context.user_data['caption'] = update.message.caption
    await update.message.reply_text("🎬 Now send the video file.")
    return POST_MOVIE

async def post_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    file_obj = None
    file_name = "movie"
    if message.video:
        file_obj = message.video
        file_name = file_obj.file_name or "video"
    elif message.document and message.document.mime_type.startswith('video/'):
        file_obj = message.document
        file_name = file_obj.file_name or "movie"
    else:
        await message.reply_text("Send a video file (mp4, mkv, etc.).")
        return POST_MOVIE

    poster = context.user_data.get('poster')
    if not poster:
        await message.reply_text("Poster missing. Restart /post.")
        return ConversationHandler.END

    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)

    caption = context.user_data.get('caption', "🎬 **New Movie**\n\nClick below to get the movie.")
    keyboard = [[InlineKeyboardButton("🎬 Get Movie", url=deep_link)]]
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])
    await message.reply_photo(photo=poster, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await message.reply_text("✅ Post created.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- /post_text ----------
async def post_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END
    await update.message.reply_text("📸 Send poster image.")
    return POST_TEXT_PHOTO

async def post_text_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Send a photo.")
        return POST_TEXT_PHOTO
    context.user_data['poster'] = update.message.photo[-1].file_id
    await update.message.reply_text("✍️ Send movie description (text).")
    return POST_TEXT_CAPTION

async def post_text_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption_text = update.message.text
    context.user_data['caption_text'] = caption_text
    telegraph_url = None
    if len(caption_text) > 1024:
        await update.message.reply_text("Text too long, creating Telegraph page...")
        title = f"Movie Synopsis {datetime.now().strftime('%Y%m%d_%H%M%S')}"
        telegraph_url = await create_telegraph_page(title, caption_text)
        if telegraph_url:
            context.user_data['telegraph_url'] = telegraph_url
            await update.message.reply_text(f"Telegraph link: {telegraph_url}")
        else:
            await update.message.reply_text("Failed to create Telegraph page, using plain text.")
    await update.message.reply_text("🎬 Now send the video file.")
    return POST_TEXT_MOVIE

async def post_text_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    file_obj = None
    file_name = "movie"
    if message.video:
        file_obj = message.video
        file_name = file_obj.file_name or "video"
    elif message.document and message.document.mime_type.startswith('video/'):
        file_obj = message.document
        file_name = file_obj.file_name or "movie"
    else:
        await message.reply_text("Send a video file.")
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
        preview = caption_text[:200] + "..." if len(caption_text) > 200 else caption_text
        caption = f"{preview}\n\n📖 [Read full description]({telegraph_url})\n\n🎬 Click below to get the movie."
        parse_mode = "Markdown"
    else:
        caption = f"{caption_text}\n\n🎬 Click below to get the movie."
        parse_mode = None

    keyboard = [[InlineKeyboardButton("🎬 Get Movie", url=deep_link)]]
    for ch in REQUIRED_CHANNELS:
        keyboard.append([InlineKeyboardButton(ch['name'], url=ch['invite'])])
    await message.reply_photo(photo=poster, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=parse_mode)
    await message.reply_text("✅ Post created.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_post_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- Standalone file upload -> Deep Link ----------
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
    await message.reply_text(f"🔗 Deep Link ready!\n\n{deep_link}\n\nFile: `{file_name}`", parse_mode="Markdown")

# ---------- Admin commands ----------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users = users_col.count_documents({})
    total_req = get_total_requests()
    await update.message.reply_text(f"📊 Stats\n👥 Users: {total_users}\n🎬 Requests: {total_req}")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("📢 /broadcast <message>")
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
    await update.message.reply_text(f"Sent to {count} users.")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data:
        context.user_data.clear()
        await update.message.reply_text("✅ Cancelled ongoing operation.")
    else:
        await update.message.reply_text("No ongoing operation.")

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("🗑️ /delete <payload>")
        return
    payload = context.args[0]
    if delete_file_by_payload(payload):
        await update.message.reply_text(f"Deleted file `{payload}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"File `{payload}` not found.", parse_mode="Markdown")

# ---------- Admin menu with buttons ----------
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    keyboard = [
        [InlineKeyboardButton("🎬 Create Post", callback_data="cmd_post")],
        [InlineKeyboardButton("📝 Create Post Text", callback_data="cmd_post_text")],
        [InlineKeyboardButton("📊 Stats", callback_data="cmd_stats")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="cmd_broadcast")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cmd_cancel")],
        [InlineKeyboardButton("🗑️ Delete File", callback_data="cmd_delete")],
    ]
    await update.message.reply_text("🎬 **Admin Panel**\nChoose an action:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.edit_message_text("Admin only.")
        return
    data = query.data
    if data == "cmd_post":
        await query.edit_message_text("Starting /post...")
        await post_start(update, context)
    elif data == "cmd_post_text":
        await query.edit_message_text("Starting /post_text...")
        await post_text_start(update, context)
    elif data == "cmd_stats":
        await stats_command(update, context)
    elif data == "cmd_broadcast":
        await query.edit_message_text("Use /broadcast <message>")
    elif data == "cmd_cancel":
        await cancel_command(update, context)
    elif data == "cmd_delete":
        await query.edit_message_text("Use /delete <payload>")

# ---------- Start handler (Admin panel + User deep link) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        if context.args:
            payload = context.args[0]
            file_id, file_name = get_file(payload)
            if not file_id:
                await update.message.reply_text("Invalid link.")
                return
            try:
                if file_name.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    sent_msg = await context.bot.send_photo(chat_id=user_id, photo=file_id, caption=file_name)
                elif file_name.endswith(('.mp4', '.mkv', '.avi')):
                    sent_msg = await context.bot.send_video(chat_id=user_id, video=file_id, caption=file_name)
                else:
                    sent_msg = await context.bot.send_document(chat_id=user_id, document=file_id, filename=file_name)
                # Auto-delete after 5 mins
                context.application.create_task(delete_messages_after_delay(context, user_id, [sent_msg.message_id], 300))
            except Exception as e:
                await update.message.reply_text(f"Error: {e}")
        else:
            await admin_menu(update, context)
        return

    # Non-admin
    if not context.args:
        await update.message.reply_text("🎬 Welcome. Use /movie or channel links.")
        return
    payload = context.args[0]
    file_id, file_name = get_file(payload)
    if not file_id:
        await update.message.reply_text("Invalid or expired link.")
        return
    ok, _ = await check_all_channels(user_id, context.bot)
    if not ok:
        msg = "Join all required channels first:\n"
        for ch in REQUIRED_CHANNELS:
            msg += f"• {ch['name']}: [Join]({ch['invite']})\n"
        await update.message.reply_text(msg, disable_web_page_preview=True)
        return
    try:
        if file_name.endswith(('.jpg', '.jpeg', '.png', '.gif')):
            sent_msg = await context.bot.send_photo(chat_id=user_id, photo=file_id, caption=file_name)
        elif file_name.endswith(('.mp4', '.mkv', '.avi')):
            sent_msg = await context.bot.send_video(chat_id=user_id, video=file_id, caption=file_name)
        else:
            sent_msg = await context.bot.send_document(chat_id=user_id, document=file_id, filename=file_name)
        warning_text = "⚠️ This file will be deleted in 5 mins. Forward to Saved Messages to keep it."
        warn_msg = await context.bot.send_message(chat_id=user_id, text=warning_text)
        context.application.create_task(delete_messages_after_delay(context, user_id, [sent_msg.message_id, warn_msg.message_id], 300))
        add_user(user_id)
        increment_requests()
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

# ---------- Webhook setup ----------
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL not set")
    sys.exit(1)

telegram_app = Application.builder().token(TOKEN).build()

# Add handlers
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
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("stats", stats_command))
telegram_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_app.add_handler(CommandHandler("cancel", cancel_command))
telegram_app.add_handler(CommandHandler("delete", delete_command))
telegram_app.add_handler(CommandHandler("menu", admin_menu))
telegram_app.add_handler(CallbackQueryHandler(menu_callback, pattern="cmd_"))
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
