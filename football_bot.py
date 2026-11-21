"""Original @GussesFootballprowzn_bot posting logic.

Runs as a SECOND python-telegram-bot Application in its own daemon thread using
BOT_TOKEN_2.  The WZN bot in app.py is completely untouched: it keeps its own
token, deep links, /post, uploads, albums, backup and admin commands.

Flow:
    admin sends post link to this bot DM
      -> link_forwarder forwards ~300 source posts into THIS bot DM
      -> forwarded messages (sent by the admin user account) hit handle_forwarded
      -> old repo logic posts them to CHANNEL_IDS (including the backup channel)
"""
import asyncio
import os
import secrets
import string
import tempfile
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient
from telegram import (
    InputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
    error as tg_error,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

from link_forwarder import (
    _english_file_name,
    _localize_text,
    _make_movie_title,
    add_channel_footer,
    parse_link,
    remove_links_from_text,
)

BOT_TOKEN_2 = os.getenv("BOT_TOKEN_2", "").strip()
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",") if x.strip()]
_raw_channels = [int(x.strip()) for x in os.getenv("CHANNEL_IDS", "").split(",") if x.strip()]
if not _raw_channels and os.getenv("CHANNEL_ID"):
    try:
        _raw_channels = [int(os.getenv("CHANNEL_ID"))]
    except ValueError:
        pass
POST_CHANNEL_IDS = list(dict.fromkeys(_raw_channels))
try:
    BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "0") or 0)
except ValueError:
    BACKUP_CHANNEL_ID = 0
MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "telegram_bot")

_submit_link = None
_loop = None


def is_admin(user_id):
    return user_id in ADMIN_IDS


if MONGODB_URI:
    _client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    _db = _client[MONGO_DB_NAME]
    files_col = _db["files"]
else:
    _client = None
    files_col = None


def generate_id(length=6):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def store_file(file_id, media_type, caption="", filename=None, file_size=None):
    for _ in range(5):
        sid = generate_id()
        if not files_col.find_one({"_id": sid}):
            files_col.insert_one({
                "_id": sid, "file_id": file_id, "media_type": media_type,
                "caption": caption or "", "filename": filename,
                "file_size": file_size, "created_at": datetime.now(timezone.utc),
            })
            return sid
    raise RuntimeError("Could not generate a unique id")


def store_album(items, caption=""):
    for _ in range(5):
        sid = generate_id()
        if not files_col.find_one({"_id": sid}):
            files_col.insert_one({
                "_id": sid, "media_type": "album", "caption": caption or "",
                "items": items, "created_at": datetime.now(timezone.utc),
            })
            return sid
    raise RuntimeError("Could not generate a unique id")


def build_deeplink(bot_username, short_id):
    return f"https://t.me/{bot_username}?start={short_id}"


def clean_filename(filename, fallback_ext=""):
    if not filename:
        return "movie" + fallback_ext
    cleaned = remove_links_from_text(filename)
    if not cleaned:
        return "movie" + fallback_ext
    _, ext = os.path.splitext(cleaned)
    if not ext and fallback_ext:
        cleaned += fallback_ext
    return cleaned


_CHANNEL_LOCK = asyncio.Lock()
_last_post_time = 0.0
MIN_POST_INTERVAL = 1.2


async def _pace():
    global _last_post_time
    async with _CHANNEL_LOCK:
        now = asyncio.get_event_loop().time()
        if _last_post_time and now - _last_post_time < MIN_POST_INTERVAL:
            await asyncio.sleep(MIN_POST_INTERVAL - (now - _last_post_time))
        _last_post_time = asyncio.get_event_loop().time()


async def run_with_retry(factory, max_retries=12):
    await _pace()
    retries = 0
    while True:
        try:
            return await factory()
        except tg_error.RetryAfter as e:
            wait = max(1, getattr(e, "retry_after", 1)) + 2
            print(f"[football-bot] rate limited {wait}s", flush=True)
            await asyncio.sleep(wait)
        except (tg_error.TimedOut, tg_error.NetworkError) as e:
            wait = min(2 ** retries, 30) + 2
            await asyncio.sleep(wait)
        except Exception:
            raise
        retries += 1
        if retries >= max_retries:
            raise


def _file_id_of(msg):
    if not msg:
        return None
    if msg.video:
        return msg.video.file_id
    if msg.document:
        return msg.document.file_id
    if msg.photo:
        return msg.photo[-1].file_id
    return None


async def _find_existing(bot, chat_id, media, limit=8):
    unique_id = getattr(media, "file_unique_id", None)
    if not unique_id:
        return None
    try:
        async for msg in bot.get_chat_history(chat_id, limit=limit):
            for cand in (msg.video, msg.document, *(msg.photo or ())):
                if cand and getattr(cand, "file_unique_id", None) == unique_id:
                    return msg
    except Exception:
        pass
    return None


def _no_duplicate_retry(factory, bot, chat_id, media):
    attempt = {"n": 0}

    async def runner():
        attempt["n"] += 1
        if attempt["n"] > 1:
            existing = await _find_existing(bot, chat_id, media)
            reused = _file_id_of(existing)
            if reused:
                return reused
        return await factory()

    return runner


def _input_media(item, caption=None):
    if item.get("media_type") == "photo":
        return InputMediaPhoto(media=item["file_id"], caption=caption)
    if item.get("media_type") == "video":
        return InputMediaVideo(media=item["file_id"], caption=caption, supports_streaming=True)
    return InputMediaDocument(
        media=item["file_id"], caption=caption,
        filename=clean_filename(item.get("filename")),
    )


def _extract_media(message):
    if message.video:
        return "video", message.video
    if message.document:
        return "document", message.document
    if message.photo:
        return "photo", message.photo[-1]
    return None, None


MAX_REUPLOAD_BYTES = 45 * 1024 * 1024
ALBUM_MAX = 10
ALBUM_GRACE = 0.6
AUTO_DELETE_SECONDS = 300


async def send_by_file_id(bot, chat_id, media, media_type, caption, original_name):
    if media_type == "video":
        sent = await bot.send_video(chat_id=chat_id, video=media.file_id,
                                    caption=caption or None, supports_streaming=True)
        return sent.video.file_id
    if media_type == "document":
        sent = await bot.send_document(chat_id=chat_id, document=media.file_id,
                                       filename=clean_filename(original_name), caption=caption or None)
        return sent.document.file_id
    if media_type == "photo":
        sent = await bot.send_photo(chat_id=chat_id, photo=media.file_id, caption=caption or None)
        return sent.photo[-1].file_id
    raise ValueError(f"Unsupported media type: {media_type}")


async def reupload_media(bot, chat_id, media, media_type, caption, original_name):
    with tempfile.TemporaryDirectory() as tmp_dir:
        file = await bot.get_file(media.file_id)
        ext = {
            "video": ".mp4",
            "document": Path(original_name).suffix or Path(file.file_path or "").suffix or ".bin",
            "photo": ".jpg",
        }.get(media_type, ".bin")
        path = os.path.join(tmp_dir, f"movie_{media_type}{ext}")
        await file.download_to_drive(custom_path=path)
        thumb_path = None
        thumb = getattr(media, "thumbnail", None)
        if thumb:
            try:
                tf = await bot.get_file(thumb.file_id)
                thumb_path = os.path.join(tmp_dir, "thumb.jpg")
                await tf.download_to_drive(custom_path=thumb_path)
            except Exception:
                thumb_path = None
        name = clean_filename(original_name, fallback_ext=ext)
        if media_type == "video":
            sent = await bot.send_video(
                chat_id=chat_id, video=InputFile(path, filename=name), caption=caption or None,
                supports_streaming=True, thumbnail=InputFile(thumb_path) if thumb_path else None,
            )
            return sent.video.file_id
        if media_type == "document":
            sent = await bot.send_document(chat_id=chat_id,
                                           document=InputFile(path, filename=name), caption=caption or None)
            return sent.document.file_id
        if media_type == "photo":
            sent = await bot.send_photo(chat_id=chat_id, photo=InputFile(path), caption=caption or None)
            return sent.photo[-1].file_id
        raise ValueError(f"Unsupported media type: {media_type}")


async def send_media_to_channel(bot, chat_id, media, media_type, caption, original_name):
    caption = add_channel_footer(caption)
    size = getattr(media, "file_size", None)
    if media_type == "photo" or (size is not None and size > MAX_REUPLOAD_BYTES):
        return await run_with_retry(_no_duplicate_retry(
            lambda: send_by_file_id(bot, chat_id, media, media_type, caption, original_name),
            bot, chat_id, media))
    try:
        return await run_with_retry(_no_duplicate_retry(
            lambda: reupload_media(bot, chat_id, media, media_type, caption, original_name),
            bot, chat_id, media))
    except Exception as e:
        print(f"[football-bot] re-upload failed ({e}); sending by file_id", flush=True)
        return await run_with_retry(_no_duplicate_retry(
            lambda: send_by_file_id(bot, chat_id, media, media_type, caption, original_name),
            bot, chat_id, media))


async def post_to_all_channels(bot, media, media_type, caption, original_name):
    last = None
    errors = []
    for chat_id in POST_CHANNEL_IDS:
        try:
            last = await send_media_to_channel(bot, chat_id, media, media_type, caption, original_name)
            print(f"[football-bot] posted {media_type} to {chat_id}", flush=True)
        except Exception as e:
            errors.append(f"{chat_id}: {e}")
            print(f"[football-bot] failed {chat_id}: {e}", flush=True)
    if not last:
        raise RuntimeError("Could not post to any channel: " + "; ".join(errors[-3:]))
    return last


async def _album_items_from(buf):
    items = []
    for it in buf["items"]:
        title = await _make_movie_title(it["file_name"], it["caption"])
        items.append({
            "media": it["media"], "file_id": it["media"].file_id,
            "media_type": it["media_type"],
            "filename": _english_file_name(title, it["file_name"]),
            "title": title, "file_size": getattr(it["media"], "file_size", None),
        })
    return items


def _first_file_id(sent, fallback):
    if isinstance(sent, str):
        return sent
    if isinstance(sent, list) and sent:
        att = sent[0].photo or sent[0].video or sent[0].document
        if att is not None:
            return (att[-1] if isinstance(att, (list, tuple)) else att).file_id
    return getattr(fallback, "file_id", None)


async def _post_album_to_channels(bot, items, caption):
    visual = [i for i in items if i["media_type"] in ("photo", "video")]
    others = [i for i in items if i["media_type"] not in ("photo", "video")]
    last = None
    errors = []
    group_caption = add_channel_footer(caption)

    async def single(chat_id, item):
        nonlocal last
        last = await send_media_to_channel(bot, chat_id, item["media"], item["media_type"],
                                           item["title"], item["filename"])

    if len(visual) >= 2:
        for chat_id in POST_CHANNEL_IDS:
            for start in range(0, len(visual), ALBUM_MAX):
                chunk = visual[start:start + ALBUM_MAX]
                if len(chunk) < 2:
                    for item in chunk:
                        try:
                            await single(chat_id, item)
                        except Exception as e:
                            errors.append(str(e))
                    continue
                group = [_input_media(item, group_caption if idx == 0 else None)
                         for idx, item in enumerate(chunk)]
                try:
                    sent = await run_with_retry(_no_duplicate_retry(
                        lambda g=group, c=chat_id: bot.send_media_group(chat_id=c, media=g),
                        bot, chat_id, chunk[0]["media"]))
                    last = _first_file_id(sent, chunk[0]["media"])
                    print(f"[football-bot] posted album {len(chunk)} to {chat_id}", flush=True)
                except Exception as e:
                    errors.append(str(e))
                    for item in chunk:
                        try:
                            await single(chat_id, item)
                        except Exception as inner:
                            errors.append(str(inner))
    elif len(visual) == 1:
        for chat_id in POST_CHANNEL_IDS:
            try:
                await single(chat_id, visual[0])
            except Exception as e:
                errors.append(str(e))

    for item in others:
        for chat_id in POST_CHANNEL_IDS:
            try:
                await single(chat_id, item)
            except Exception as e:
                errors.append(str(e))
    if not last:
        raise RuntimeError("Album could not be posted: " + "; ".join(errors[-3:]))
    return last


async def _delete_after_delay(bot, chat_id, message_id, delay=AUTO_DELETE_SECONDS):
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def send_file_by_doc(message, doc):
    media_type = doc["media_type"]
    caption = doc.get("caption") or None
    try:
        if media_type == "album":
            items = doc.get("items") or []
            visual = [i for i in items if i.get("media_type") in ("photo", "video")]
            for item in visual:
                if item.get("media_type") == "video":
                    sent = await message.reply_video(item["file_id"], caption=caption, supports_streaming=True)
                else:
                    sent = await message.reply_photo(item["file_id"], caption=caption)
                asyncio.create_task(_delete_after_delay(message.get_bot(), sent.chat_id, sent.message_id))
            return
        if media_type == "video":
            sent = await message.reply_video(doc["file_id"], caption=caption, supports_streaming=True)
        elif media_type == "document":
            sent = await message.reply_document(doc["file_id"], filename=clean_filename(doc.get("filename")), caption=caption)
        elif media_type == "photo":
            sent = await message.reply_photo(doc["file_id"], caption=caption)
        else:
            await message.reply_text(doc.get("caption") or "")
            return
        asyncio.create_task(_delete_after_delay(message.get_bot(), sent.chat_id, sent.message_id))
    except Exception as e:
        print(f"[football-bot] delivery error: {e}", flush=True)


_MEDIA_QUEUE = None
_MEDIA_WORKER = None


def _ensure_media_worker():
    global _MEDIA_QUEUE, _MEDIA_WORKER
    if _MEDIA_WORKER is None or _MEDIA_WORKER.done():
        _MEDIA_QUEUE = asyncio.Queue()
        _MEDIA_WORKER = asyncio.create_task(_media_worker())
    return _MEDIA_QUEUE


def _enqueue_media(context, message, forwarded=False):
    media_type, media = _extract_media(message)
    if media is None:
        return
    queue = _ensure_media_worker()
    key = None
    if getattr(message, "media_group_id", None):
        key = f"{message.chat.id}:{message.media_group_id}"
    queue.put_nowait({
        "context": context, "message": message, "media_type": media_type,
        "media": media, "file_name": getattr(media, "file_name", None) or "",
        "caption": message.caption or "", "album_key": key, "forwarded": forwarded,
    })


async def _media_worker():
    pending = None
    while True:
        entry = await _MEDIA_QUEUE.get()
        try:
            key = entry["album_key"]
            if pending and pending["key"] != key:
                await _publish_album(pending["context"], pending)
                pending = None
            if key is None:
                if entry["forwarded"]:
                    await _publish_forward_single(entry["context"], entry["message"],
                                                  entry["media_type"], entry["media"])
                else:
                    await _direct_upload(entry["context"], entry["message"],
                                         entry["media_type"], entry["media"])
            else:
                if pending is None:
                    pending = {"key": key, "context": entry["context"],
                               "message": entry["message"], "items": []}
                pending["items"].append({
                    "media_type": entry["media_type"], "media": entry["media"],
                    "file_name": entry["file_name"], "caption": entry["caption"],
                })
                pending["message"] = entry["message"]
            if pending and _MEDIA_QUEUE.empty():
                await asyncio.sleep(ALBUM_GRACE)
                if _MEDIA_QUEUE.empty():
                    await _publish_album(pending["context"], pending)
                    pending = None
        except Exception as e:
            print(f"[football-bot] media worker error: {e}", flush=True)
            print(traceback.format_exc(), flush=True)


async def _publish_forward_single(context, msg, media_type, media):
    name = getattr(media, "file_name", None) or ""
    title = await _make_movie_title(name, (msg.caption or "").strip())
    post_name = _english_file_name(title, name)
    new_id = await post_to_all_channels(context.bot, media, media_type, title, post_name)
    sid = store_file(new_id, media_type, caption=title, filename=post_name,
                     file_size=getattr(media, "file_size", None))
    link = build_deeplink(context.bot.username, sid)
    await msg.reply_text("✅ Movie post တင်ပြီးပါပြီ!")
    await msg.reply_text(f"🔗 Deeplink: {link}")


async def _direct_upload(context, message, media_type, media):
    name = getattr(media, "file_name", None) or ""
    title = await _make_movie_title(name, message.caption or "")
    post_name = _english_file_name(title, name)
    sid = store_file(media.file_id, media_type, caption=title, filename=post_name)
    link = build_deeplink(context.bot.username, sid)
    caption = f"{title}\n\n🔗 {link}" if title else f"🔗 {link}"
    try:
        if media_type == "video":
            await message.reply_video(media.file_id, caption=caption, supports_streaming=True)
        elif media_type == "document":
            await message.reply_document(media.file_id, filename=clean_filename(post_name), caption=caption)
        else:
            await message.reply_photo(media.file_id, caption=caption)
    except Exception as e:
        print(f"[football-bot] upload reply failed: {e}", flush=True)


async def _publish_album(context, buf):
    items = await _album_items_from(buf)
    caption = next((i["title"] for i in items if i["title"]), "")
    await _post_album_to_channels(context.bot, items, caption)
    sid = store_album([{k: v for k, v in i.items() if k != "media"} for i in items], caption=caption)
    link = build_deeplink(context.bot.username, sid)
    await buf["message"].reply_text(f"✅ Post {len(items)} ခု အုပ်စုလိုက် တင်ပြီးပါပြီ!\n🔗 Deeplink: {link}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and files_col is not None:
        doc = files_col.find_one({"_id": args[0]})
        if doc:
            await send_file_by_doc(update.message, doc)
            return
    if is_admin(update.effective_user.id):
        await update.message.reply_text("🎛 Football bot အဆင်သင့်ပါ။ Post link ပို့ပါ။")
    else:
        await update.message.reply_text("Admin ပေးထားတဲ့ link ကိုနှိပ်ပြီး ဖိုင်ရယူပါ။")


async def handle_post_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    text = (update.effective_message.text or "").strip()
    if not text or not parse_link(text):
        return
    if _submit_link and _submit_link(text):
        await update.effective_message.reply_text("Link ရပါပြီ — forward စပါမယ်။")
        print(f"[football-bot] link accepted: {text[:120]}", flush=True)


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    msg = update.message
    raw = (msg.caption or "").strip()
    media = msg.video or msg.document or (msg.photo and msg.photo[-1])
    if media is None:
        caption = await _localize_text(raw)
        if not caption:
            return
        sid = store_file("", "text", caption=caption)
        link = build_deeplink(context.bot.username, sid)
        post_text = add_channel_footer(caption)
        for chat_id in POST_CHANNEL_IDS:
            await run_with_retry(lambda c=chat_id: context.bot.send_message(chat_id=c, text=post_text))
        print(f"[football-bot] posted text to {len(POST_CHANNEL_IDS)} channel(s)", flush=True)
        await msg.reply_text("✅ Post တင်ပြီးပါပြီ!")
        await msg.reply_text(f"🔗 Deeplink: {link}")
        return
    _enqueue_media(context, msg, forwarded=True)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    _enqueue_media(context, update.message, forwarded=False)


async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"[football-bot] unhandled error: {context.error}", flush=True)


def _build_app():
    app = (Application.builder().token(BOT_TOKEN_2)
           .request(HTTPXRequest(
               connect_timeout=30, read_timeout=120, write_timeout=120, pool_timeout=30))
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND
        & ~filters.FORWARDED & ~filters.UpdateType.CHANNEL_POST,
        handle_post_link))
    app.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))
    app.add_handler(MessageHandler(
        (filters.VIDEO | filters.Document.ALL | filters.PHOTO)
        & ~filters.FORWARDED & ~filters.UpdateType.CHANNEL_POST,
        handle_media))
    app.add_error_handler(on_error)
    return app


async def _run():
    print(f"[football-bot] starting; channels={POST_CHANNEL_IDS} backup={BACKUP_CHANNEL_ID}", flush=True)
    if not BOT_TOKEN_2:
        print("[football-bot] BOT_TOKEN_2 not set - disabled", flush=True)
        return
    if not POST_CHANNEL_IDS:
        print("[football-bot] CHANNEL_IDS not set - disabled", flush=True)
        return
    if files_col is None:
        print("[football-bot] MONGO_URI/MONGODB_URI not set - disabled", flush=True)
        return
    app = _build_app()
    me = await app.bot.get_me()
    print(f"[football-bot] {me.username} ({me.id}) ready for post links", flush=True)
    await app.run_polling(drop_pending_updates=True)


def start_football_bot(link_submitter):
    """Start the football bot in a daemon thread (never touches WZN bot)."""
    global _submit_link
    if not BOT_TOKEN_2:
        print("[football-bot] BOT_TOKEN_2 not set - disabled", flush=True)
        return
    _submit_link = link_submitter

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        except Exception as e:
            print(f"[football-bot] stopped: {type(e).__name__}: {e}", flush=True)

    threading.Thread(target=runner, name="football-bot", daemon=True).start()
