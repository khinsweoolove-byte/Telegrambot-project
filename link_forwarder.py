"""Post-link forwarder for the user account.

The bot (python-telegram-bot) delivers post links from its DM to this module
via submit_link().  Every post from the supplied link up to the newest post of
the source channel is forwarded back into the same bot DM, then the work stops
until another link arrives.

Runs in its own daemon thread with its own asyncio loop so it never interferes
with python-telegram-bot's polling/webhook loop or the existing bot features.
"""
import asyncio
import os
import re
import tempfile
import threading
from pathlib import Path

import httpx
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.extensions.binaryreader import tlobjects as _tlobjects
from telethon.sessions import StringSession
from telethon.tl import types as _tl_types
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto, MessageReplyHeader, MessageService


# Telegram still sometimes sends the layer 228-231 `channel` constructor
# (0xd49f34c6) even when Telethon negotiates a newer layer.  It has the same
# layout as the current constructor as long as the newer optional flags are
# unset, so registering it as a subclass keeps those responses readable.
class _LegacyChannel(_tl_types.Channel):
    CONSTRUCTOR_ID = 0xD49F34C6


_tlobjects.setdefault(_LegacyChannel.CONSTRUCTOR_ID, _LegacyChannel)


# Telegram still sends the older MessageReplyHeader constructor (0x1b97dd66)
# alongside the new one.  Telethon 1.46 dropped the old ID, which makes any
# messages response containing a reply unreadable.  Re-register that ID.
class _LegacyReplyHeader(MessageReplyHeader):
    CONSTRUCTOR_ID = 0x1B97DD66


_tlobjects.setdefault(_LegacyReplyHeader.CONSTRUCTOR_ID, _LegacyReplyHeader)

API_ID = int(os.getenv("TG_API_ID") or "31778137")
API_HASH = os.getenv("TG_API_HASH") or "357247ed45b28459c6600e31cce5b773"
SESSION = (os.getenv("TG_SESSION") or "").strip()
# Destinations are bot DMs.  Comma-separated list supported (both bots).
BOT_USERNAMES = [b.strip() for b in os.getenv(
    "FORWARD_BOT", "@WZNmoviefilsend_bot,@GussesFootballprowzn_bot"
).split(",") if b.strip()]
DELAY = float(os.getenv("FORWARD_DELAY", "1.5") or "1.5")

# ---------- Original @GussesFootballprowzn_bot channel posting behaviour ----------
# Ported from kokyi152-wzn/telegram-file-bot (main.py) so posts keep the same
# title cleaning, translation, footer and fresh-upload style as the old bot.
POST_ENABLED = os.getenv("POST_TO_CHANNEL", "true").strip().lower() not in ("0", "false", "no")
POST_CHANNEL = os.getenv("POST_CHANNEL", "@everyboyhobby").strip()
BACKUP_POST_CHANNEL = os.getenv("BACKUP_POST_CHANNEL", "@mybackupchannel18").strip()

CHANNEL_FOOTER_LINES = (
    "မြန်မာစာတန်းထိုးဇာတ်ကားကြည့်ရန် - https://t.me/wznmoviescollector",
    "upload by - https://t.me/everyboyhobby",
)

EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001F2FF"
    "\U0001F300-\U0001FAFF"
    "\u2190-\u21FF"
    "\u2300-\u23FF"
    "\u2460-\u24FF"
    "\u25A0-\u27BF"
    "\u2B00-\u2BFF"
    "\uFE0F\u200D"
    "\u2122\u00A9\u00AE"
    "]+"
)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
MYANMAR_RE = re.compile(r"[\u1000-\u109F\uAA60-\uAA7F\uA9E0-\uA9FF]")
LATIN_RE = re.compile(r"[A-Za-z]")
OTHER_SCRIPT_RE = re.compile(
    "[\u0370-\u03ff\u0400-\u04ff\u0530-\u058f\u0590-\u05ff\u0600-\u06ff"
    "\u0750-\u077f\u0900-\u097f\u0980-\u09ff\u0a00-\u0a7f\u0a80-\u0aff"
    "\u0b00-\u0b7f\u0b80-\u0bff\u0c00-\u0c7f\u0c80-\u0cff\u0d00-\u0d7f"
    "\u0d80-\u0dff\u0e00-\u0e7f\u0e80-\u0eff\u0f00-\u0fff\u10a0-\u10ff"
    "\u1100-\u11ff\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff"
    "\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]"
)
_SCRIPT_SOURCE = [
    (re.compile(r"[\u3040-\u30ff]"), "ja"),
    (re.compile(r"[\uac00-\ud7af\u1100-\u11ff]"), "ko"),
    (re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]"), "zh-CN"),
    (re.compile(r"[\u0e00-\u0e7f]"), "th"),
    (re.compile(r"[\u0400-\u04ff]"), "ru"),
    (re.compile(r"[\u0600-\u06ff\u0750-\u077f]"), "ar"),
    (re.compile(r"[\u0900-\u097f]"), "hi"),
    (re.compile(r"[\u0980-\u09ff]"), "bn"),
    (re.compile(r"[\u0b80-\u0bff]"), "ta"),
    (re.compile(r"[\u0c00-\u0c7f]"), "te"),
    (re.compile(r"[\u0590-\u05ff]"), "he"),
    (re.compile(r"[\u10a0-\u10ff]"), "ka"),
]
_MYMEMORY_ERROR_MARKERS = ("LANGPAIR", "INVALID SOURCE", "MYMEMORY WARNING", "UNABLE TO", "NO QUOTA")
EN_LABEL = "🇬🇧 English:"
MY_LABEL = "🇲🇲 Myanmar:"


def remove_links_from_text(text):
    if not text:
        return text
    text = re.sub(r"https?://[^\s]+", "", text)
    text = re.sub(r"www\.[^\s]+", "", text)
    text = re.sub(r"t\.me/[^\s]+", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _clean_title(text):
    if not text:
        return ""
    text = remove_links_from_text(text)
    text = re.sub(r"@\w+", " ", text)
    text = re.sub(r"#\S+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip().strip("._ -")


def _first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _has_other_script(text):
    return bool(text and OTHER_SCRIPT_RE.search(text))


def _has_foreign_script(text):
    return bool(text and (MYANMAR_RE.search(text) or OTHER_SCRIPT_RE.search(text)))


def _detect_source_lang(text):
    if not text:
        return "en"
    for regex, code in _SCRIPT_SOURCE:
        if regex.search(text):
            return code
    return "en"


async def _translate_text(text, target_lang):
    if not text or len(text.strip()) < 3:
        return text
    try:
        async with httpx.AsyncClient(timeout=20) as hc:
            resp = await hc.get(
                "https://clients5.google.com/translate_a/t",
                params={"client": "dict-chrome-ex", "sl": "auto", "tl": target_lang, "q": text},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            data = resp.json()
        translated = ""
        if isinstance(data, list) and data:
            first = data[0]
            translated = first[0] if isinstance(first, list) and first and isinstance(first[0], str) else (first if isinstance(first, str) else "")
        elif isinstance(data, str):
            translated = data
        translated = (translated or "").strip()
        if translated and translated.lower() != text.strip().lower():
            return translated
    except Exception:
        pass
    source = _detect_source_lang(text)
    if source and source.lower() != (target_lang or "").lower():
        try:
            async with httpx.AsyncClient(timeout=20) as hc:
                resp = await hc.get(
                    "https://api.mymemory.translated.net/get",
                    params={"q": text, "langpair": f"{source}|{target_lang}"},
                )
                data = resp.json() if resp.status_code == 200 else {}
            translated = ((data.get("responseData") or {}).get("translatedText") or "").strip()
            upper = translated.upper()
            if (translated and not data.get("quotaFinished")
                    and not any(m in upper for m in _MYMEMORY_ERROR_MARKERS)
                    and translated.lower() != text.strip().lower()):
                return translated
        except Exception:
            pass
    return text


async def _localize_text(text):
    if not text or not text.strip():
        return text
    if LATIN_RE.search(text) and MYANMAR_RE.search(text):
        return text
    if not _has_other_script(text):
        return text
    en = (await _translate_text(text, "en") or text).strip()
    my = (await _translate_text(text, "my") or text).strip()
    if not my or my == en:
        return f"{EN_LABEL}\n{en}"
    return f"{EN_LABEL}\n{en}\n\n{MY_LABEL}\n({my})"


def _file_stem(name):
    return Path(EMOJI_RE.sub(" ", _clean_title(name or ""))).stem


async def _make_movie_title(file_name, caption_fallback=""):
    name = _file_stem(file_name)
    caption = _clean_title(_first_line(caption_fallback))
    if caption and _has_foreign_script(caption) and not _has_foreign_script(name):
        name = caption
    if not name:
        name = caption
    if not name:
        return ""
    return await _localize_text(name)


def _english_file_name(title, original_name):
    ext = os.path.splitext(original_name or "")[1]
    part = title or ""
    if EN_LABEL in part:
        part = part.split(EN_LABEL, 1)[1].split(MY_LABEL, 1)[0]
    part = part.strip()
    base = EMOJI_RE.sub(" ", _clean_title(part.splitlines()[0] if part else "")).strip()
    if not base:
        base = os.path.splitext(original_name or "movie")[0]
    return (base[:120] or "movie") + (ext or ".mp4")


def add_channel_footer(text=""):
    footer = "\n\n".join(CHANNEL_FOOTER_LINES)
    text = (text or "").strip()
    if CHANNEL_FOOTER_LINES[-1] in text:
        return text
    return f"{text}\n\n{footer}" if text else footer


def _media_kind(msg):
    media = getattr(msg, "media", None)
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc and any(isinstance(a, _tl_types.DocumentAttributeVideo) for a in doc.attributes):
            return "video"
        return "document"
    return None


def _message_file_name(msg):
    media = getattr(msg, "media", None)
    if not isinstance(media, MessageMediaDocument) or not media.document:
        return ""
    for attr in media.document.attributes:
        if isinstance(attr, _tl_types.DocumentAttributeFilename):
            return attr.file_name or ""
    return ""


def _group_messages(messages):
    result, pending, current_gid = [], [], None
    for msg in messages:
        gid = getattr(msg, "grouped_id", None)
        if gid is None:
            if pending:
                result.append(pending)
                pending = []
            result.append([msg])
        else:
            if pending and gid != current_gid:
                result.append(pending)
                pending = []
            pending.append(msg)
            current_gid = gid
    if pending:
        result.append(pending)
    return result


_PRIVATE = re.compile(r"t\.me/c/(\d+)/(\d+)")
_PUBLIC = re.compile(r"t\.me/([A-Za-z0-9_]+)/(\d+)")

_loop = None
_forwarder = None


def parse_link(text):
    """Return (source_ref, start_id) from a channel post link, else None."""
    if not text:
        return None
    m = _PRIVATE.search(text)
    if m:
        return "-100" + m.group(1), int(m.group(2))
    m = _PUBLIC.search(text)
    if m and m.group(1).lower() not in ("s", "c", "share", "joinchat"):
        return "@" + m.group(1), int(m.group(2))
    return None


async def _ensure_connected(client):
    """Reconnect the user client if Telegram dropped the connection."""
    try:
        if not client.is_connected():
            print("[link-forwarder] reconnecting ...", flush=True)
            await client.connect()
        return True
    except Exception as exc:
        print(f"[link-forwarder] RECONNECT FAILED {type(exc).__name__}: {exc}", flush=True)
        return False


class _Forwarder:
    def __init__(self, client, bots, post_targets):
        self.client = client
        self.bots = bots  # list of (entity, display_name)
        self.post_targets = post_targets  # list of (entity, label, silent)
        self.queue = asyncio.Queue()
        self.active = set()
        self.busy = False

    async def submit(self, text):
        parsed = parse_link(text)
        if not parsed:
            print("[link-forwarder] no channel post link found, ignoring", flush=True)
            return
        if parsed in self.active:
            print(f"[link-forwarder] duplicate link already running: {parsed}", flush=True)
            return
        self.active.add(parsed)
        await self.queue.put(parsed)
        print(f"[link-forwarder] queued {parsed}", flush=True)
        if not self.busy:
            asyncio.create_task(self.run())

    async def run(self):
        self.busy = True
        try:
            while not self.queue.empty():
                ref, start = await self.queue.get()
                try:
                    await self.forward(ref, start)
                except Exception as exc:
                    print(f"[link-forwarder] ERROR {type(exc).__name__}: {exc}", flush=True)
                finally:
                    self.active.discard((ref, start))
                    self.queue.task_done()
        finally:
            self.busy = False

    async def _send_with_retry(self, factory, label):
        for _ in range(6):
            try:
                return await factory()
            except FloodWaitError as exc:
                print(f"[link-forwarder] post FloodWait {exc.seconds}s ({label})", flush=True)
                await asyncio.sleep(exc.seconds + 2)
            except ConnectionError:
                if not await _ensure_connected(self.client):
                    return None
            except RPCError as exc:
                print(f"[link-forwarder] post FAILED {label}: {exc}", flush=True)
                return None
            except Exception as exc:
                print(f"[link-forwarder] post ERROR {label}: {type(exc).__name__}: {exc}", flush=True)
                return None
        return None

    async def post_to_channel(self, messages):
        """Repost forwarded posts using the old football bot's channel style."""
        if not POST_ENABLED or not self.post_targets or not messages:
            return
        for group in _group_messages(messages):
            try:
                await self._post_group(group)
            except Exception as exc:
                print(f"[link-forwarder] channel post ERROR {type(exc).__name__}: {exc}", flush=True)

    async def _post_group(self, group):
        client = self.client
        first = group[0]
        media_msgs = [m for m in group if _media_kind(m)]

        if not media_msgs:
            raw = (first.message or "").strip()
            caption = add_channel_footer(await _localize_text(remove_links_from_text(raw)))
            if not caption:
                return
            for entity, label, silent in self.post_targets:
                await self._send_with_retry(
                    lambda e=entity, c=caption[:1000], s=silent: client.send_message(e, c, silent=s),
                    f"text -> {label}",
                )
            print(f"[link-forwarder] channel text posted to {len(self.post_targets)} target(s)", flush=True)
            return

        name = _message_file_name(first)
        title = await _make_movie_title(name, (first.caption or "").strip())
        filename = _english_file_name(title, name)
        caption = add_channel_footer(title)[:1000]

        with tempfile.TemporaryDirectory(prefix="wznpost_") as tmp:
            files = []
            for idx, msg in enumerate(media_msgs):
                ext = os.path.splitext(_message_file_name(msg))[1] or (
                    ".jpg" if _media_kind(msg) == "photo" else ".mp4"
                )
                path = os.path.join(tmp, f"file_{idx}{ext}")
                await client.download_media(msg.media, file=path)
                if os.path.exists(path):
                    files.append(path)
            if not files:
                print("[link-forwarder] channel post skipped - no downloaded media", flush=True)
                return
            for entity, label, silent in self.post_targets:
                if len(files) > 1:
                    await self._send_with_retry(
                        lambda e=entity, f=list(files), c=caption, s=silent: client.send_file(
                            e, f, caption=c, silent=s
                        ),
                        f"album({len(files)}) -> {label}",
                    )
                else:
                    streaming = _media_kind(first) == "video"
                    await self._send_with_retry(
                        lambda e=entity, f=files[0], c=caption, s=silent, v=streaming: client.send_file(
                            e, f, caption=c, silent=s, supports_streaming=v
                        ),
                        f"media -> {label}",
                    )
        print(f"[link-forwarder] channel media posted to {len(self.post_targets)} target(s): {filename}", flush=True)

    async def forward(self, ref, start):
        client = self.client
        if not await _ensure_connected(client):
            print("[link-forwarder] cannot forward while disconnected", flush=True)
            return
        try:
            source = await client.get_entity(ref)
        except Exception as exc:
            print(f"[link-forwarder] cannot open {ref}: {exc}", flush=True)
            return
        title = getattr(source, "title", str(ref))
        head = await client.get_messages(source, limit=1)
        end = head[0].id if head else 0
        print(f"[link-forwarder] {title}: {start} -> {end}", flush=True)
        if start > end:
            print("[link-forwarder] start is newer than latest post, nothing to do", flush=True)
            return

        pending = []
        sent = 0

        async def flush():
            nonlocal pending, sent
            if not pending:
                return
            ids = [m.id for m in pending]
            for bot, bot_name in self.bots:
                while True:
                    try:
                        await client.forward_messages(bot, ids, from_peer=source)
                        sent += len(ids)
                        print(f"[link-forwarder] forwarded {ids[0]}..{ids[-1]} -> {bot_name} (total {sent})", flush=True)
                        break
                    except FloodWaitError as exc:
                        print(f"[link-forwarder] FloodWait {exc.seconds}s", flush=True)
                        await asyncio.sleep(exc.seconds + 3)
                    except ConnectionError:
                        print("[link-forwarder] connection dropped while forwarding, reconnecting ...", flush=True)
                        await asyncio.sleep(2)
                        if not await _ensure_connected(client):
                            break
                    except RPCError as exc:
                        print(f"[link-forwarder] FAILED {ids} -> {bot_name}: {exc}", flush=True)
                        break
            # Old football-bot style: after the DM forwards, publish the same
            # posts to the channel and then mirror them into the backup channel.
            await self.post_to_channel(list(pending))
            pending = []
            await asyncio.sleep(DELAY)

        async for m in client.iter_messages(source, min_id=start - 1, reverse=True):
            if isinstance(m, MessageService):
                continue
            if not (m.media or (m.message and m.message.strip())):
                continue
            # Skip single-photo posts.  Album photos (grouped_id) are kept.
            if not getattr(m, "grouped_id", None) and isinstance(m.media, MessageMediaPhoto):
                print(f"[link-forwarder] skip single-photo post {m.id}", flush=True)
                continue
            gid = getattr(m, "grouped_id", None)
            if gid:
                if pending and getattr(pending[0], "grouped_id", None) != gid:
                    await flush()
                pending.append(m)
                if len(pending) >= 10:
                    await flush()
                continue
            await flush()
            pending = [m]
            await flush()
        await flush()
        print(f"[link-forwarder] DONE {title}: {sent} messages", flush=True)


def submit_link(text):
    """Thread-safe entry point called from the python-telegram-bot thread."""
    if not _loop or not _forwarder:
        print("[link-forwarder] not ready - link ignored", flush=True)
        return False
    try:
        asyncio.run_coroutine_threadsafe(_forwarder.submit(text), _loop)
        return True
    except Exception as exc:
        print(f"[link-forwarder] SUBMIT ERROR {type(exc).__name__}: {exc}", flush=True)
        return False


async def _listen():
    global _loop, _forwarder
    _loop = asyncio.get_running_loop()

    if not SESSION:
        print("[link-forwarder] TG_SESSION not set - disabled", flush=True)
        return
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("[link-forwarder] session not authorised - disabled", flush=True)
        await client.disconnect()
        return
    me = await client.get_me()
    bots = []
    for name in BOT_USERNAMES:
        try:
            entity = await client.get_entity(name)
            bots.append((entity, name))
            print(f"[link-forwarder] destination {name} -> {entity.id}", flush=True)
        except Exception as exc:
            print(f"[link-forwarder] cannot resolve {name}: {exc}", flush=True)
    if not bots:
        print("[link-forwarder] no destination bots - disabled", flush=True)
        await client.disconnect()
        return
    post_targets = []
    if POST_ENABLED:
        for ref, label, silent in (
            (POST_CHANNEL, "channel", False),
            (BACKUP_POST_CHANNEL, "backup", True),
        ):
            if not ref:
                continue
            try:
                entity = await client.get_entity(ref)
                post_targets.append((entity, label, silent))
                print(f"[link-forwarder] post destination {label} {ref} -> {entity.id}", flush=True)
            except Exception as exc:
                print(f"[link-forwarder] cannot resolve post destination {ref}: {exc}", flush=True)
    else:
        print("[link-forwarder] channel posting disabled", flush=True)

    _forwarder = _Forwarder(client, bots, post_targets)
    print(f"[link-forwarder] user {me.id} -> {len(bots)} bot(s), {len(post_targets)} post target(s)", flush=True)
    print("[link-forwarder] ready for links from bot ...", flush=True)

    while True:
        await asyncio.sleep(60)
        await _ensure_connected(client)


def start_link_forwarder():
    """Start the listener in a daemon thread (safe next to PTB polling)."""
    if not SESSION:
        print("[link-forwarder] TG_SESSION not set - disabled", flush=True)
        return
    threading.Thread(target=lambda: asyncio.run(_listen()),
                     name="link-forwarder", daemon=True).start()
