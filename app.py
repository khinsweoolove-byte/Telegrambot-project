import os
import asyncio
import threading
import logging
import secrets
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.helpers import create_deep_linked_url
from pymongo import MongoClient

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Flask ----------
app = Flask(__name__)

# ---------- MongoDB ----------
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI not set")
    exit(1)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["file_share_bot"]
file_collection = db["files"]

def save_file(payload, file_id, file_name):
    file_collection.update_one(
        {"payload": payload},
        {"$set": {"file_id": file_id, "file_name": file_name}},
        upsert=True
    )

def get_file(payload):
    doc = file_collection.find_one({"payload": payload})
    if doc:
        return doc["file_id"], doc["file_name"]
    return None, None

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
    """User က ဖိုင်ပို့လိုက်တိုင်း Deep Link ထုတ်ပေးမယ်"""
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
        await message.reply_text("ကျေးဇူးပြု၍ ဖိုင် (document, video, photo, audio) တစ်ခု ပို့ပါ။")
        return

    # Save file info and generate deep link
    payload = generate_payload()
    save_file(payload, file_obj.file_id, file_name)
    deep_link = create_deep_linked_url(BOT_USERNAME, payload)
    
    await message.reply_text(
        f"✅ **သင်၏ Deep Link အသင့်ရှိပါပြီ။**\n\n"
        f"**ဖိုင်အမည်:** `{file_name}`\n"
        f"**လင့် (link):**\n{deep_link}\n\n"
        f"ဤလင့်ကို နှိပ်လိုက်ရုံဖြင့် ဖိုင်ကို ရယူနိုင်ပါသည်။"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        payload = context.args[0]
        file_id, file_name = get_file(payload)
        if not file_id:
            await update.message.reply_text("❌ လင့်မမှန်ပါ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
            return
        await update.message.reply_text(f"📂 **{file_name}** ပို့နေပါပြီ...")
        if file_name.endswith(('.jpg', '.jpeg', '.png', '.gif')):
            await context.bot.send_photo(chat_id=update.effective_user.id, photo=file_id, caption=file_name)
        elif file_name.endswith(('.mp4', '.mkv', '.avi')):
            await context.bot.send_video(chat_id=update.effective_user.id, video=file_id, caption=file_name)
        else:
            await context.bot.send_document(chat_id=update.effective_user.id, document=file_id, filename=file_name)
        return
    await update.message.reply_text(
        "🎯 **File to Deep Link Bot**\n\n"
        "သင်ပို့လိုက်တဲ့ ဖိုင်တိုင်းအတွက် Deep Link ကို ကျွန်တော် ချက်ချင်းထုတ်ပေးပါမယ်။\n"
        "အဲဒီလင့်ကို သင်ဖြစ်စေ၊ တစ်ခြားသူများ ဖြစ်စေ နှိပ်လိုက်ရုံနဲ့ ဖိုင်ကို ရယူနိုင်ပါပြီ။"
    )

# ---------- Webhook Setup ----------
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL not set")
    exit(1)

telegram_app = Application.builder().token(TOKEN).build()
telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_file))
telegram_app.add_handler(MessageHandler(filters.COMMAND, start))

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
