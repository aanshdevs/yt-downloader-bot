"""YouTube Downloader Bot  --  bot created by anshhx.ai

Owner-only Telegram bot:
  - YouTube video / Shorts link bhejo -> quality buttons -> file mil jati hai
  - 49 MB tak: seedha Telegram me
  - usse badi file: R2 configured ho to R2 pe, warna Gofile.io pe (no card/signup) -> download link
  - /latest [n]: CHANNEL_URL ki latest n videos
"""
import asyncio
import base64
import html
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import boto3
import requests
import yt_dlp
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Har message / caption / description me ye credit jata hai
CREDIT = "bot created by anshhx.ai"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ytbot")

MB = 1024 * 1024
GB = 1024 * MB

# ------------------------- Config (env vars) -------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CHANNEL_URL = os.getenv("CHANNEL_URL", "").strip()  # .../videos ya .../shorts
DEFAULT_HEIGHT = int(os.getenv("DEFAULT_HEIGHT", "720"))  # /latest ke liye
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "3000"))  # is se bada nahi
LINK_HOURS = max(1, min(168, int(os.getenv("LINK_HOURS", "24"))))  # R2 max 7 din
JS_RUNTIME = os.getenv("JS_RUNTIME", "").strip()  # sirf Termux pe: "node"

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "").strip()
R2_ENABLED = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET])
R2_PREFIX = "ytbot/"  # sirf isi folder ki files auto-delete hoti hain

# Gofile.io: R2 na ho to badi files ke liye ye use hota hai, koi card/signup nahi chahiye
GOFILE_TOKEN = os.getenv("GOFILE_TOKEN", "").strip()  # optional: account se link kabhi expire nahi hoga

TG_LIMIT = 49 * MB  # Telegram Bot API upload limit ~50 MB

# Sirf single video links (watch / shorts / live / youtu.be)
URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?\S*v=|shorts/|live/)|youtu\.be/)"
    r"[\w-]{11}\S*",
    re.IGNORECASE,
)

# Optional cookies (agar YouTube "confirm you're not a bot" bole)
COOKIES_PATH = None
_b64 = os.getenv("YT_COOKIES_B64", "").strip()
if _b64:
    COOKIES_PATH = str(Path(tempfile.gettempdir()) / "yt_cookies.txt")
    Path(COOKIES_PATH).write_bytes(base64.b64decode(_b64))
elif Path("cookies.txt").is_file():
    COOKIES_PATH = "cookies.txt"

s3 = None
if R2_ENABLED:
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 5, "mode": "standard"},
            # naye boto3 ke default checksums R2 ke saath dikkat dete hain
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )

DL_SEM = asyncio.Semaphore(1)  # ek time pe ek hi download
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


# ------------------------- Small helpers -------------------------
def credited(text: str) -> str:
    return f"{text}\n\n\U0001F916 {CREDIT}"


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt_size(n: float) -> str:
    return f"{n / GB:.2f} GB" if n >= GB else f"{n / MB:.0f} MB"


def fmt_dur(sec) -> str:
    if not sec:
        return "?"
    hh, rem = divmod(int(sec), 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh}:{mm:02d}:{ss:02d}" if hh else f"{mm}:{ss:02d}"


def friendly_error(e: Exception) -> str:
    err = str(e)
    log.warning("Error: %s", err)
    if "Sign in" in err or "confirm you" in err:
        return (
            "\u274C YouTube login / bot-check maang raha hai "
            "(server IP block ya age-restricted video).\n"
            "YT_COOKIES_B64 set karo ya bot ko Termux pe chalao."
        )
    return f"\u274C {err[-300:]}"


# ------------------------- Telegram message helpers -------------------------
# Bot ka har message in do functions se jata hai, isliye credit kabhi miss nahi hota.
async def send_text(bot, chat_id, text, *, html_mode=False, markup=None):
    return await bot.send_message(
        chat_id=chat_id,
        text=credited(text),
        parse_mode=ParseMode.HTML if html_mode else None,
        reply_markup=markup,
        link_preview_options=NO_PREVIEW,
    )


async def edit_text(msg, text, *, html_mode=False, markup=None):
    try:
        await msg.edit_text(
            credited(text),
            parse_mode=ParseMode.HTML if html_mode else None,
            reply_markup=markup,
            link_preview_options=NO_PREVIEW,
        )
    except Exception as e:  # "message is not modified", flood limit, etc.
        log.debug("edit_text fail: %s", e)


class Progress:
    """Thread se safe, throttled status-message updater."""

    def __init__(self, loop, msg, every: float = 7.0):
        self.loop, self.msg, self.every = loop, msg, every
        self._last = 0.0

    def push(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last < self.every:
            return
        self._last = now
        asyncio.run_coroutine_threadsafe(edit_text(self.msg, text), self.loop)


def owner_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or user.id != OWNER_ID:
            if update.callback_query:
                await update.callback_query.answer()
            elif update.effective_chat:
                await send_text(
                    context.bot, update.effective_chat.id, "\U0001F512 Ye private bot hai."
                )
            return
        return await fn(update, context)

    return wrapper


# ------------------------- yt-dlp helpers (blocking; thread me) -------------------------
def base_opts() -> dict:
    opts = {"quiet": True, "noprogress": True, "retries": 5, "socket_timeout": 30}
    if COOKIES_PATH:
        opts["cookiefile"] = COOKIES_PATH
    if JS_RUNTIME:
        opts["js_runtimes"] = {JS_RUNTIME: {}}
    return opts


def analyze(url: str) -> dict:
    """Video ki info nikalo aur available qualities (144p ... 4K) ki list banao."""
    with yt_dlp.YoutubeDL(base_opts() | {"noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("is_live"):
        raise RuntimeError("Live stream abhi download nahi ho sakti.")
    fmts = info.get("formats") or []

    def fsize(f):
        return f.get("filesize") or f.get("filesize_approx") or 0

    audios = [
        f
        for f in fmts
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]
    best_audio = max(
        audios, key=lambda f: (f.get("ext") == "m4a", f.get("abr") or 0), default=None
    )
    audio_size = fsize(best_audio) if best_audio else 0

    # Quality = chhota side (Shorts 1080x1920 bhi "1080p" hi hai)
    groups: dict = {}
    for f in fmts:
        if f.get("vcodec") in (None, "none"):
            continue
        w, hgt = f.get("width"), f.get("height")
        if not w or not hgt:
            continue
        groups.setdefault(min(w, hgt), []).append(f)

    choices = {}
    for q, lst in groups.items():
        best = max(
            lst,
            key=lambda f: (
                str(f.get("vcodec") or "").startswith("avc1"),
                f.get("tbr") or 0,
            ),
        )
        muxed = best.get("acodec") not in (None, "none")
        choices[q] = {
            "size": fsize(best) + (0 if muxed else audio_size),
            "fps": best.get("fps") or 0,
        }
    return {
        "url": url,
        "id": info.get("id"),
        "title": info.get("title") or info.get("id") or "video",
        "duration": info.get("duration"),
        "choices": choices,
        "created": time.time(),
    }


def ydl_hook(progress: Progress, label: str):
    def hook(d):
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        pct = f"{done * 100 / total:.0f}%" if total else "..."
        spd = d.get("speed")
        speed = f" \u2022 {spd / MB:.1f} MB/s" if spd else ""
        progress.push(f"\u23EC Download ({label}): {pct}{speed}")

    return hook


def download_video(url: str, outdir: Path, quality: int, hook):
    opts = base_opts() | {
        "format": "bv*+ba/b",
        # res:<q> = chhota side <= q me sabse best; H.264 + m4a ko prefer karo
        "format_sort": [f"res:{quality}", "vcodec:h264", "acodec:m4a"],
        "merge_output_format": "mp4",
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = [
        p
        for p in outdir.iterdir()
        if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
    ]
    if not files:
        raise RuntimeError("Download hua par file nahi mili")
    return max(files, key=lambda p: p.stat().st_size), info


def list_latest(n: int) -> list:
    opts = base_opts() | {"extract_flat": True, "playlistend": n, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(CHANNEL_URL, download=False)
    urls: list = []

    def walk(entries):
        for e in entries or []:
            if not e:
                continue
            if e.get("entries"):
                walk(e["entries"])
            elif e.get("id") and len(e["id"]) == 11:  # video id = 11 chars
                urls.append(f"https://www.youtube.com/watch?v={e['id']}")

    walk(info.get("entries"))
    return urls[:n]


# ------------------------- Cloudflare R2 (blocking; thread me) -------------------------
def upload_to_r2(path: Path, title: str, video_id: str, label: str, on_progress) -> str:
    key = f"{R2_PREFIX}{int(time.time())}_{video_id}_{label}{path.suffix}"
    nice = f"{title[:80]} [{label}]{path.suffix}"
    ascii_name = re.sub(r"[^A-Za-z0-9._\[\] -]+", "", nice).strip() or f"{video_id}{path.suffix}"
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{urllib.parse.quote(nice)}"
    )
    total = path.stat().st_size
    sent = 0
    lock = threading.Lock()

    def cb(n):
        nonlocal sent
        with lock:
            sent += n
            on_progress(sent, total)

    s3.upload_file(
        str(path),
        R2_BUCKET,
        key,
        ExtraArgs={
            "ContentType": "video/mp4" if path.suffix.lower() == ".mp4" else "application/octet-stream",
            "ContentDisposition": disposition,
        },
        Config=TransferConfig(
            multipart_threshold=16 * MB, multipart_chunksize=16 * MB, max_concurrency=4
        ),
        Callback=cb,
    )
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": key},
        ExpiresIn=LINK_HOURS * 3600,
    )


def cleanup_r2_blocking() -> int:
    """Link expire hone ke baad purani files delete (free 10 GB me rehne ke liye)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LINK_HOURS + 1)
    deleted = 0
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=R2_BUCKET, Prefix=R2_PREFIX
    ):
        for obj in page.get("Contents", []):
            if obj["LastModified"] < cutoff:
                s3.delete_object(Bucket=R2_BUCKET, Key=obj["Key"])
                deleted += 1
    return deleted


async def cleanup_loop():
    while True:
        try:
            n = await asyncio.to_thread(cleanup_r2_blocking)
            if n:
                log.info("R2 cleanup: %d purani files delete hui", n)
        except Exception:
            log.exception("R2 cleanup fail")
        await asyncio.sleep(3600)


# ------------------------- Gofile.io (blocking; thread me) -------------------------
# R2 configure na ho to ye use hota hai. Koi card/account/signup nahi chahiye.
# GOFILE_TOKEN diya ho to file us account me save hoti hai (kabhi expire nahi hoti),
# warna guest upload ~10 din baad (agar koi download na kare) khud delete ho jati hai.
def upload_to_gofile(path: Path, on_progress) -> str:
    servers_resp = requests.get("https://api.gofile.io/servers", timeout=20)
    servers_resp.raise_for_status()
    servers = servers_resp.json()["data"]["servers"]
    if not servers:
        raise RuntimeError("Gofile: koi server available nahi hai")
    server = servers[0]["name"]
    upload_url = f"https://{server}.gofile.io/uploadFile"

    total = path.stat().st_size

    def cb(monitor):
        on_progress(monitor.bytes_read, total)

    headers = {}
    if GOFILE_TOKEN:
        headers["Authorization"] = f"Bearer {GOFILE_TOKEN}"

    with open(path, "rb") as f:
        encoder = MultipartEncoder(fields={"file": (path.name, f, "application/octet-stream")})
        monitor = MultipartEncoderMonitor(encoder, cb)
        headers["Content-Type"] = monitor.content_type
        resp = requests.post(upload_url, data=monitor, headers=headers, timeout=None)

    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Gofile upload fail: {data}")
    return data["data"]["downloadPage"]


# ------------------------- Download + deliver flow -------------------------
async def deliver(bot, chat_id, status, path: Path, info: dict, quality: int, progress):
    size = path.stat().st_size
    title = info.get("title") or path.stem
    w, hgt = info.get("width"), info.get("height")
    label = f"{min(w, hgt)}p" if w and hgt else f"{quality}p"
    meta_line = f"{label} \u2022 {fmt_size(size)}"

    if size <= TG_LIMIT:
        await edit_text(status, f"\U0001F4E4 Telegram pe bhej raha hu ({fmt_size(size)})...")
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
        await bot.send_video(
            chat_id=chat_id,
            video=path,
            caption=credited(f"{title[:800]}\n{meta_line}"),
            duration=int(info["duration"]) if info.get("duration") else None,
            width=w,
            height=hgt,
            supports_streaming=True,
            read_timeout=300,
            write_timeout=300,
            connect_timeout=30,
            pool_timeout=30,
        )
    elif R2_ENABLED:
        await edit_text(
            status, f"\u2601\uFE0F Badi file ({fmt_size(size)}) - R2 pe upload ho rahi hai..."
        )

        def on_prog(sent, total):
            progress.push(
                f"\u2601\uFE0F R2 upload: {sent * 100 // total}% "
                f"({fmt_size(sent)} / {fmt_size(total)})"
            )

        try:
            url = await asyncio.to_thread(
                upload_to_r2, path, title, info.get("id") or path.stem, label, on_prog
            )
        except Exception as e:
            log.exception("R2 upload fail")
            raise RuntimeError(f"R2 upload fail: {str(e)[-200:]}") from e
        await send_text(
            bot,
            chat_id,
            f"\u2601\uFE0F <b>{esc(title)}</b>\n{esc(meta_line)}\n\n"
            f'<a href="{esc(url)}">\u2B07\uFE0F Download link</a>\n'
            f"(link {LINK_HOURS} ghante valid)",
            html_mode=True,
        )
    else:
        await edit_text(
            status, f"\u2601\uFE0F Badi file ({fmt_size(size)}) - Gofile pe upload ho rahi hai..."
        )

        def on_prog(sent, total):
            progress.push(
                f"\u2601\uFE0F Gofile upload: {sent * 100 // total}% "
                f"({fmt_size(sent)} / {fmt_size(total)})"
            )

        try:
            url = await asyncio.to_thread(upload_to_gofile, path, on_prog)
        except Exception as e:
            log.exception("Gofile upload fail")
            raise RuntimeError(f"Gofile upload fail: {str(e)[-200:]}") from e

        expiry_note = (
            "(link tab tak valid, kabhi expire nahi hoga - account se linked hai)"
            if GOFILE_TOKEN
            else "(agar 10 din tak koi download na kare to link apne aap expire ho jayega)"
        )
        await send_text(
            bot,
            chat_id,
            f"\u2601\uFE0F <b>{esc(title)}</b>\n{esc(meta_line)}\n\n"
            f'<a href="{esc(url)}">\u2B07\uFE0F Download link</a>\n{expiry_note}',
            html_mode=True,
        )


async def process_download(bot, chat_id, status, meta: dict, quality: int):
    loop = asyncio.get_running_loop()
    progress = Progress(loop, status)
    label = f"{quality}p"
    tmp = Path(tempfile.mkdtemp(prefix="ytbot_"))
    ok = False
    try:
        await edit_text(status, f"\u23F3 Line me hai ({label})...")
        async with DL_SEM:
            await edit_text(status, f"\u23EC Download shuru ({label})...")
            path, info = await asyncio.to_thread(
                download_video, meta["url"], tmp, quality, ydl_hook(progress, label)
            )
            await deliver(bot, chat_id, status, path, info, quality, progress)
        ok = True
    except Exception as e:
        log.exception("process_download fail")
        await edit_text(status, friendly_error(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if ok:
        try:
            await status.delete()
        except Exception:
            pass


# ------------------------- Handlers -------------------------
def q_label(q: int, c: dict) -> str:
    fps = "60" if c["fps"] and c["fps"] > 30 else ""
    size = f" \u00B7 ~{fmt_size(c['size'])}" if c["size"] else ""
    return f"{q}p{fps}{size}"


def build_keyboard(token: str, choices: dict) -> InlineKeyboardMarkup:
    btns = [
        InlineKeyboardButton(q_label(q, c), callback_data=f"dl|{token}|{q}")
        for q, c in sorted(choices.items(), reverse=True)
    ]
    rows = [btns[i : i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton("\u274C Cancel", callback_data=f"x|{token}|0")])
    return InlineKeyboardMarkup(rows)


def prune_pending(pending: dict) -> None:
    cutoff = time.time() - 3600
    for k in [k for k, v in pending.items() if v["created"] < cutoff]:
        pending.pop(k, None)
    while len(pending) > 50:
        pending.pop(next(iter(pending)))


@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_text(
        context.bot,
        update.effective_chat.id,
        "\U0001F3AC YouTube Downloader\n\n"
        "\u2022 Video / Shorts ka link bhejo -> quality chuno -> file mil jayegi\n"
        "\u2022 49 MB tak seedha Telegram me, usse badi file "
        + (f"R2 download link se ({LINK_HOURS} ghante valid)\n" if R2_ENABLED else "Gofile download link se\n")
        + "\u2022 /latest [n] - channel ki latest n videos\n\n"
        f"\u2601\uFE0F Badi files: {'R2' if R2_ENABLED else 'Gofile (no setup)'}",
    )


@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    m = URL_RE.search(update.effective_message.text or "")
    if not m:
        await send_text(context.bot, chat_id, "YouTube video ya Shorts ka link bhejo.")
        return
    status = await send_text(
        context.bot, chat_id, "\U0001F50E Video ki info nikal raha hu..."
    )
    try:
        meta = await asyncio.to_thread(analyze, m.group(0))
    except Exception as e:
        await edit_text(status, friendly_error(e))
        return
    if not meta["choices"]:
        await edit_text(status, "\u274C Koi video format nahi mila.")
        return
    pending = context.bot_data["pending"]
    prune_pending(pending)
    token = secrets.token_hex(4)
    pending[token] = meta
    await edit_text(
        status,
        f"\U0001F3AC {meta['title']}\n\u23F1 {fmt_dur(meta['duration'])}\n\nQuality chuno:",
        markup=build_keyboard(token, meta["choices"]),
    )


@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        action, token, val = q.data.split("|")
    except ValueError:
        await q.answer()
        return
    pending = context.bot_data["pending"]
    meta = pending.get(token)
    if meta is None:
        await q.answer("Request expire ho gayi. Link dobara bhejo.", show_alert=True)
        return
    if action == "x":
        pending.pop(token, None)
        await q.answer()
        await edit_text(q.message, "\u274C Cancel kar diya.")
        return
    quality = int(val)
    choice = meta["choices"].get(quality)
    if choice is None:
        await q.answer()
        return
    if choice["size"] > MAX_DOWNLOAD_MB * MB:
        await q.answer(
            f"Ye ~{fmt_size(choice['size'])} hai (limit {MAX_DOWNLOAD_MB} MB). "
            "Chhoti quality chuno.",
            show_alert=True,
        )
        return
    pending.pop(token, None)
    await q.answer()
    await process_download(context.bot, q.message.chat_id, q.message, meta, quality)


@owner_only
async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not CHANNEL_URL:
        await send_text(
            context.bot,
            chat_id,
            "CHANNEL_URL set karo (jaise https://www.youtube.com/@naam/shorts).",
        )
        return
    try:
        n = max(1, min(10, int(context.args[0]))) if context.args else 1
    except ValueError:
        n = 1
    status = await send_text(
        context.bot, chat_id, f"\U0001F50E Latest {n} video dhoondh raha hu..."
    )
    try:
        urls = await asyncio.to_thread(list_latest, n)
    except Exception as e:
        await edit_text(status, friendly_error(e))
        return
    if not urls:
        await edit_text(
            status,
            "Koi video nahi mili. CHANNEL_URL ke end me /videos ya /shorts lagao.",
        )
        return
    await edit_text(
        status, f"\u2705 {len(urls)} video mili, {DEFAULT_HEIGHT}p tak me bhej raha hu..."
    )
    for u in urls:
        st = await send_text(context.bot, chat_id, "\u23F3 Shuru ho raha hai...")
        await process_download(context.bot, chat_id, st, {"url": u}, DEFAULT_HEIGHT)


# ------------------------- App setup -------------------------
async def post_init(app: Application) -> None:
    app.bot_data["pending"] = {}
    try:
        await app.bot.set_my_commands(
            [BotCommand("start", "Help"), BotCommand("latest", "Channel ki latest videos")]
        )
        await app.bot.set_my_short_description(f"Private YouTube downloader \u2022 {CREDIT}")
        await app.bot.set_my_description(
            credited("YouTube videos aur Shorts kisi bhi quality me download karo.")
        )
    except Exception:
        log.exception("Bot profile set nahi hua (ignore kar sakte ho)")
    if R2_ENABLED:
        app.bot_data["cleanup_task"] = asyncio.create_task(cleanup_loop())
    log.info(
        "Bot ready | badi files -> %s | %s",
        "R2" if R2_ENABLED else "Gofile (no setup)",
        CREDIT,
    )


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("cleanup_task")
    if task:
        task.cancel()


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("latest", cmd_latest, filters=private))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
