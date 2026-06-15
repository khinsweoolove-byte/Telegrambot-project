import os
import asyncio
import threading
import logging
import secrets
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ContextTypes
from telegram.helpers import create_deep_linked_url
from pymongo import MongoClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------- MongoDB ----------
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI environment variable not set!")
    # For testing without MongoDB, you can use in-memory dict (not recommended for production)
    # But we'll keep MongoDB as required
    file_store = {}
else:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client["file_share_bot"]
    file_collection = db["files"]

def save_file(payload, file_id, file_name, file_type):
    if MONGO_URI:
        file_collection.update_one(
            {"payload": payload},
            {"$set": {"file_id": file_id, "file_name": file_name, "file_type": file_type}},
            upsert=True
        )
    else:
        file_store[payload] = {"file_id": file_id, "file_name": file_name, "file_type": file_type}
    logger.info(f"Saved: {payload} -> {file_name}")

def get_file(payload):
    if MONGO_URI:
        doc = file_collection.find_one({"payload": payload})
        if doc:
            return doc["file_id"], doc["file_name"], doc.get("file_type", "document")
        return None, None, None
    else:
        data = file_store.get(payload)
        if data:
            return data["file_id"], data["file_name"], data.get("file_type", "document")
        return None, None, None

# ---------- Telegram Config ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
if not TOKEN or not BOT_USERNAME:
    logger.error("TELEGRAM_TOKEN and BOT_USERNAME required")
    exit(1)

def generate_payload():
    return secrets.token_urlsafe(12)

# ---------- Handlers ----------
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    file_obj = None
    file_name = "file"
    file_type = "document"
    
    if message.document:
        file_obj = message.document
        file_name = file_obj.file_name or "document"
        file_type = "document"
        logger.info(f"Received document: {file_name}")
    elif message.video:
        file_obj = message.video
        file_name = file_obj.file_name or "video.mp4"
        file_type = "video"
        logger.info(f"Received video: {file_name}")
    elif message.photo:
        file_obj = message.photo[-1]
        file_name = "photo.jpg"
        file_type = "photo"
        logger.info(f"Received photo")
    elif message.audio:
        file_obj = message.audio
        file_name = file_obj.file_name or "audio.mp3"
        file_type = "audio"
        logger.info(f"Received audio: {file_name}")
    else:
        await message.reply_text("Please send a file (document, video, photo, or audio).")
        return
    
    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name, file_type)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    
    await message.reply_text(
        f"🔗 **Your Deep Link is ready!**\n\n"
        f"**File name:** `{file_name}`\n"
        f"**Link:**\n{deep_link}\n\n"
        f"Anyone who clicks this link will receive the file immediately."
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.args:
        payload = context.args[0]
        file_id, file_name, file_type = get_file(payload)
        if not file_id:
            await update.message.reply_text("❌ Invalid or expired link.")
            return
        await update.message.reply_text(f"📂 Sending **{file_name}**...")
        try:
            if file_type == "photo":
                await context.bot.send_photo(chat_id=user_id, photo=file_id, caption=file_name)
            elif file_type == "video":
                await context.bot.send_video(chat_id=user_id, video=file_id, caption=file_name)
            elif file_type == "audio":
                await context.bot.send_audio(chat_id=user_id, audio=file_id, caption=file_name)
            else:
                await context.bot.send_document(chat_id=user_id, document=file_id, filename=file_name)
        except Exception as e:
            await update.message.reply_text(f"Error sending file: {e}")
        return
    await update.message.reply_text(
        "🎯 **File to Deep Link Bot**\n\n"
        "Send me any file (document, video, photo, audio) and I will instantly give you a deep link.\n"
        "Clicking that link will deliver the file to anyone."
    )

# ---------- Webhook ----------
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL environment variable not set!")
    exit(1)

telegram_app = Application.builder().token(TOKEN).build()
# Order matters: put file handler first because it has more specific filters
telegram_app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.PHOTO | filters.AUDIO, handle_file))
telegram_app.add_handler(CommandHandler("start", start))
# Also catch any non-command text that might be a direct file? Already handled above.

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
    loop.run_forever()
