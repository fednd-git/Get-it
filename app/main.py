"""Get-it: paste a link, get the video or image.

Engines: yt-dlp (videos, 1000+ sites) with gallery-dl as the fallback for
image posts and carousels (Instagram photos, X images, Pinterest, etc.).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import urlparse

import http.cookiejar
import urllib.request

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from starlette.background import BackgroundTask

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PROXY = os.environ.get("PROXY")  # optional http(s)/socks proxy, e.g. a residential one, for sites that block cloud servers
# PO_TOKEN: a pre-generated YouTube Proof-of-Origin token, e.g. "web.gvs+<base64url token>".
# YouTube asks for one when it flags the server's IP. A token is good for hours/days,
# then it has to be refreshed. Cookies (COOKIES_TXT) are the more reliable fix.
PO_TOKEN = os.environ.get("PO_TOKEN")


def _cookies_path() -> str | None:
    """COOKIES_FILE = path to a Netscape cookies.txt. COOKIES_TXT = the file's contents (raw or base64),
    handy on Vercel where you can only set environment variables."""
    path = os.environ.get("COOKIES_FILE")
    if path and Path(path).exists():
        return path
    raw = os.environ.get("COOKIES_TXT")
    if not raw:
        return None
    if "\t" not in raw:
        import base64

        try:
            raw = base64.b64decode(raw).decode()
        except Exception:  # noqa: BLE001
            return None
    dst = os.path.join(os.environ.get("TMPDIR") or "/tmp", "getit-cookies.txt")
    with open(dst, "w") as f:
        f.write(raw if raw.startswith("# ") else "# Netscape HTTP Cookie File\n" + raw)
    return dst


COOKIES = _cookies_path()
MAX_FILESIZE = int(os.environ.get("MAX_FILESIZE_MB", "500")) * 1024 * 1024

app = FastAPI(title="Get-it", docs_url=None, redoc_url=None)


def find_ffmpeg() -> str | None:
    """System ffmpeg (Docker) or the static one bundled by imageio-ffmpeg (Vercel, other serverless hosts)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if not os.access(exe, os.X_OK):  # read-only bundles can drop the exec bit: copy to /tmp
            dst = os.path.join(os.environ.get("TMPDIR") or "/tmp", "ffmpeg")
            if not os.path.exists(dst):
                shutil.copy(exe, dst)
            os.chmod(dst, 0o755)
            exe = dst
        return exe
    except Exception:  # noqa: BLE001
        return None


FFMPEG = find_ffmpeg()
TMP = os.environ.get("TMPDIR") or "/tmp"

PLATFORMS = {
    "instagram": ["instagram.com", "instagr.am"],
    "tiktok": ["tiktok.com"],
    "facebook": ["facebook.com", "fb.watch", "fb.com"],
    "x": ["x.com", "twitter.com", "t.co"],
    "youtube": ["youtube.com", "youtu.be"],
    "reddit": ["reddit.com", "redd.it"],
    "pinterest": ["pinterest.com", "pin.it"],
    "snapchat": ["snapchat.com"],
    "threads": ["threads.net", "threads.com"],
    "vimeo": ["vimeo.com"],
    "soundcloud": ["soundcloud.com"],
    "twitch": ["twitch.tv"],
    "linkedin": ["linkedin.com"],
    "tumblr": ["tumblr.com"],
    "dailymotion": ["dailymotion.com", "dai.ly"],
    "bilibili": ["bilibili.com", "b23.tv"],
}

HEIGHTS = [2160, 1440, 1080, 720, 480, 360]


class Link(BaseModel):
    url: str


def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for name, domains in PLATFORMS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return name
    return "web"


def clean_url(raw: str) -> str:
    raw = (raw or "").strip()
    m = re.search(r"https?://\S+", raw)  # people paste whole share texts
    if not m:
        raise HTTPException(400, "That doesn't look like a link. Copy the post's link and paste it again.")
    return m.group(0).rstrip(").,]\"'")


def ydl_opts(**extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 20,  # Meta's pages can take a while to answer when they tar-pit a cloud IP
        "max_filesize": MAX_FILESIZE,
        # speed: pull HLS/DASH fragments in parallel, big buffers, no throttled single stream
        "concurrent_fragment_downloads": 16,
        "http_chunk_size": 10 * 1024 * 1024,
        "buffersize": 1024 * 1024,
        "retries": 2,
        "fragment_retries": 3,
    }
    if COOKIES and Path(COOKIES).exists():
        opts["cookiefile"] = COOKIES
    if PROXY:
        opts["proxy"] = PROXY
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    opts.update(extra)
    return opts


def _strip_ytdlp_footer(msg: str) -> str:
    """yt-dlp appends a generic footer to almost every error
    ("Confirm you are on the latest version using yt-dlp -U", "You can get more
    help at ..."). It is not part of the real failure and must never be matched
    on — it used to make every failure on every site look like a YouTube
    bot-check."""
    for marker in ("; please report this issue", "you can get more help",
                   "confirm you are on the latest version"):
        if marker in msg:
            msg = msg.split(marker, 1)[0]
    return msg


def _meta_login_msg(platform: str) -> str:
    """The one fix for every Meta failure from a cloud server: Meta serves
    nothing to a browser it doesn't recognise, so the app needs the user's
    own logged-in session (cookies)."""
    name = "Facebook" if platform == "facebook" else "Instagram"
    return (f"{name} only shows this to logged-in browsers from this server. Fix: export your browser "
            "cookies as cookies.txt and add them as COOKIES_TXT in Vercel "
            "(Project → Settings → Environment Variables), then redeploy.")


def friendly(err: Exception, downloading: bool = False, platform: str = "web") -> str:
    print(f"[get-it] {type(err).__name__}: {err}", flush=True)  # real reason shows in the host's logs
    msg = _strip_ytdlp_footer(str(err).lower())
    # A post is private only when the extractor says so in its own words.
    # A rate-limit, a bot-check or a login wall is a server block, not a private post.
    if ("private video" in msg or "this video is private" in msg or "this account is private" in msg
            or "post is private" in msg or "private account" in msg or "only visible to" in msg):
        return "This post is private or needs a login to view."
    if "rate limit" in msg or "rate-limit" in msg or "too many requests" in msg or "429" in msg:
        return "The platform is rate-limiting the server right now. Wait a minute and try again."
    if ("not a bot" in msg or "confirm you're not a bot" in msg or "confirm you are not a bot" in msg
            or "failed to extract any player response" in msg
            or "po token" in msg or "the page needs to be reloaded" in msg or "no video formats found" in msg):
        if platform == "youtube":
            return ("YouTube is blocking the server right now, not your video. "
                    "Adding your cookies as COOKIES_TXT in Vercel usually fixes it — or try again in a minute.")
        if platform in ("facebook", "instagram"):
            return _meta_login_msg(platform)
        return "The platform is blocking the server right now, not your video. Try again in a minute."
    if "login" in msg or "sign in" in msg or "log in" in msg:
        if platform in ("facebook", "instagram"):
            return _meta_login_msg(platform)
        return "This post needs a login to view."
    if "unsupported url" in msg:
        return "This site isn't supported yet."
    if "404" in msg or "not found" in msg or "unavailable" in msg or "removed" in msg or "does not exist" in msg:
        return "This post is gone or the link has expired. Copy it again."
    if "max_filesize" in msg or "larger than" in msg:
        return "That file is too big for this server."
    if "timed out" in msg or "timeout" in msg or "connection" in msg:
        if platform in ("facebook", "instagram"):
            # Meta stalls requests from a server it doesn't recognise instead of
            # answering — a login wall in slow motion. Retrying won't help.
            return _meta_login_msg(platform)
        return "The platform took too long to respond. Try again in a minute."
    if platform in ("facebook", "instagram"):
        # Without a login, Meta serves nothing to this server — whatever the
        # exact error was (403, empty page, bad response), the fix is the same.
        return _meta_login_msg(platform)
    if downloading:
        return "Couldn't download this one. Try another quality, or try again in a minute."
    return "Couldn't read that link. Check it opens in your browser, then try again."


# ---------- yt-dlp ----------

def short_side(f: dict) -> int:
    """'720p' means the short side: a portrait 720x1280 TikTok is 720p, not 1280p."""
    w, h = f.get("width") or 0, f.get("height") or 0
    return min(w, h) if w and h else (h or w)


def has_v(f: dict) -> bool:
    """Has picture. Some sites (X) leave codecs blank on their plain MP4s, so a blank codec
    with a known size counts as video."""
    vc = f.get("vcodec")
    return vc != "none" and (vc is not None or bool(f.get("height") or f.get("width")))


def has_a(f: dict) -> bool:
    """Has sound. A blank audio codec counts unless the format is marked video-only."""
    ac, vc = f.get("acodec"), f.get("vcodec")
    return ac != "none" and (ac is not None or vc is None or vc == "none")


def size_for(formats: list[dict], height: int | None) -> int | None:
    def fs(f):
        return f.get("filesize") or f.get("filesize_approx")

    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none") and fs(f)]
    best_audio = max((fs(f) for f in audio), default=0)
    if height is None:
        return best_audio or None
    vids = [f for f in formats if has_v(f) and short_side(f) <= height * 1.05 and fs(f)]
    if not vids:
        return None
    top = max(vids, key=lambda f: (short_side(f), fs(f)))
    extra = best_audio if top.get("acodec") in (None, "none") else 0
    return fs(top) + extra


def item_from_info(info: dict) -> dict:
    formats = info.get("formats") or []
    heights = sorted({short_side(f) for f in formats if has_v(f) and short_side(f)}, reverse=True)
    has_video = bool(heights) or info.get("vcodec") not in (None, "none") and info.get("ext") in ("mp4", "webm", "mov")
    has_audio = any(has_a(f) for f in formats) or info.get("acodec") not in (None, "none")

    qualities = []
    if heights:
        top = heights[0]
        for h in HEIGHTS:
            if any(h * 0.8 <= x <= h * 1.05 for x in heights):
                qualities.append({"id": str(h), "label": f"{h}p", "size": size_for(formats, h)})
        if not qualities:
            qualities.append({"id": str(top), "label": f"{top}p", "size": size_for(formats, top)})
    elif has_video:
        qualities.append({"id": "best", "label": "Best", "size": info.get("filesize") or info.get("filesize_approx")})
    if has_audio:
        qualities.append({"id": "audio", "label": "Audio (MP3)", "size": size_for(formats, None)})

    kind = "video" if (heights or has_video) else ("audio" if has_audio else "image")
    if kind == "image" and not qualities:
        qualities.append({"id": "best", "label": "Original", "size": None})
    return {
        "kind": kind,
        "thumbnail": info.get("thumbnail"),
        "title": info.get("title") or info.get("description") or "",
        "duration": info.get("duration"),
        "qualities": qualities,
    }


# YouTube often refuses cloud servers with its default web client. Other YouTube apps' clients
# (TV, Safari, Android VR, mobile web) are checked differently, so we try them in turn and
# remember the one that last worked.
YT_CLIENTS = [None, ["tv", "web_safari"], ["android_vr"], ["mweb"], ["tv_simply"], ["web_embedded"], ["ios"]]
_yt_winner: list | None = None


def variants(url: str) -> list[dict]:
    """Try the site's no-login API first, then fall back to the heavier ones."""
    plat = platform_of(url)
    if plat == "x":
        return [
            {"extractor_args": {"twitter": {"api": ["syndication"]}}},  # no guest token, fast
            {"extractor_args": {"twitter": {"api": ["graphql"]}}},       # needs a guest token, slower
            {},
        ]
    if plat == "youtube":
        # winner first, then every client in turn — including the default web
        # client (None). The old list-comprehension dropped None when there was
        # no winner yet, so the default client — the one a PO token applies
        # to — was never tried on a fresh server.
        order = ([_yt_winner] if _yt_winner else [None]) + [c for c in YT_CLIENTS if c != _yt_winner]
        out = []
        for c in order:
            args: dict = {}
            if c is not None:
                args["player_client"] = c
            if PO_TOKEN:
                # format: "web.gvs+<token>" or "web+<token>"; a pre-generated token
                # proves the request came from a real browser session. It only
                # applies to the client named in it (usually web, the first variant).
                args["po_token"] = [PO_TOKEN]
            out.append({} if not args else {"extractor_args": {"youtube": args}})
        return out
    return [{}]


def blocked(e: Exception) -> bool:
    """True when the failure is a platform block or a flaky extraction — worth
    retrying with another player client / API instead of giving up. Terminal
    errors (deleted video, unsupported URL) are NOT in here so they fail fast."""
    msg = _strip_ytdlp_footer(str(e).lower())
    return any(k in msg for k in (
        "not a bot", "confirm you", "player response", "po token", "sign in", "log in", "login",
        "403", "429", "rate limit", "rate-limit", "requested format is not available",
        "the page needs to be reloaded", "no video formats found", "no formats",
        "timed out", "timeout", "unable to extract", "failed to extract",
        "connection", "reset by peer", "temporary", "try again", "server returned",
    ))


_INFO_CACHE: dict[tuple, tuple] = {}
_INFO_CACHE_TTL = 300  # seconds; media URLs stay signed far longer than this
_INFO_CACHE_MAX = 64


def _cookie_header(url: str) -> str:
    """Cookie header for a URL, read straight from the cookies file. Used on
    cache hits, where there is no live YoutubeDL instance to ask."""
    if not (COOKIES and Path(COOKIES).exists()):
        return ""
    try:
        jar = http.cookiejar.MozillaCookieJar(COOKIES)
        jar.load(ignore_discard=True, ignore_expires=True)
        req = urllib.request.Request(url)
        jar.add_cookie_header(req)
        return req.get_header("Cookie") or ""
    except Exception:  # noqa: BLE001
        return ""


def extract(url: str, budget_s: float = 60, **opts):
    """extract_info with platform fallback. Returns (info, ydl, variant).

    A 5-minute in-memory cache means the download re-uses the probe the info
    screen just did, instead of re-extracting (and re-hitting each platform's
    flaky path) two more times. On a cache hit ydl is None — callers that need
    cookies must read them from the cookies file themselves."""
    global _yt_winner
    last: Exception | None = None
    start = time.monotonic()
    for v in variants(url):
        if time.monotonic() - start > budget_s:
            break
        # the cache is keyed on the caller's options too: a probe (playlistend=50)
        # and a plan (noplaylist) for the same URL can legitimately differ
        key = (url, json.dumps(v, sort_keys=True), json.dumps(opts, sort_keys=True, default=str))
        hit = _INFO_CACHE.get(key)
        if hit and time.monotonic() - hit[0] < _INFO_CACHE_TTL:
            # hand back the YoutubeDL that read the post: its cookie jar holds the
            # session cookies (TikTok's tt_chain_token) the CDN wants for the relay
            return hit[1], hit[3], hit[2]
        ydl = yt_dlp.YoutubeDL(ydl_opts(**opts, **v))
        try:
            info = ydl.extract_info(url, download=False)
            if v and "youtube" in v.get("extractor_args", {}):
                _yt_winner = v["extractor_args"]["youtube"]["player_client"]
            if len(_INFO_CACHE) >= _INFO_CACHE_MAX:
                _INFO_CACHE.pop(next(iter(_INFO_CACHE)))
            _INFO_CACHE[key] = (time.monotonic(), info, v, ydl)
            return info, ydl, v
        except Exception as e:  # noqa: BLE001
            last = e
            if not blocked(e):
                break
    raise last  # type: ignore[misc]


def probe_ytdlp(url: str) -> dict:
    info, _ydl, _ = extract(url, budget_s=40, skip_download=True, extract_flat=False, playlistend=50)
    # sanitize_info is a static method that returns a fresh dict, so it is safe
    # on the cached raw info (the cache keeps the raw copy for plan()).
    info = yt_dlp.YoutubeDL.sanitize_info(info)
    entries = [e for e in (info.get("entries") or []) if e]
    items = [item_from_info(e) for e in entries] if entries else [item_from_info(info)]
    return {
        "engine": "ytdlp",
        "title": info.get("title") or info.get("description") or (entries[0].get("title") if entries else ""),
        "author": info.get("uploader") or info.get("channel") or info.get("creator") or "",
        "thumbnail": info.get("thumbnail") or (items[0]["thumbnail"] if items else None),
        "duration": info.get("duration"),
        "items": items,
    }


# ---------- gallery-dl ----------

def gdl_base() -> list[str]:
    cmd = [sys.executable, "-m", "gallery_dl", "--quiet"]
    if COOKIES and Path(COOKIES).exists():
        cmd += ["--cookies", COOKIES]
    if PROXY:
        cmd += ["--proxy", PROXY]
    return cmd


def probe_gallery(url: str) -> dict:
    r = subprocess.run(gdl_base() + ["--dump-json", url], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(r.stderr or "gallery-dl found nothing")
    data = json.loads(r.stdout)
    items, author, title = [], "", ""
    for msg in data:
        # messages look like [3, url, metadata] for files
        if isinstance(msg, list) and len(msg) >= 3 and msg[0] == 3:
            file_url, meta = msg[1], msg[2] or {}
            ext = (meta.get("extension") or "").lower()
            is_video = ext in ("mp4", "webm", "mov", "m4v") or meta.get("video_url")
            items.append({
                "kind": "video" if is_video else "image",
                "thumbnail": meta.get("display_url") or meta.get("thumbnail") or (None if is_video else file_url),
                "title": "",
                "duration": meta.get("duration"),
                "qualities": [{"id": "best", "label": "Original", "size": meta.get("filesize")}],
            })
            a = meta.get("author")
            author = author or meta.get("username") or meta.get("fullname") or (a.get("name", "") if isinstance(a, dict) else (a or ""))
            title = title or meta.get("description") or meta.get("content") or meta.get("title") or ""
    if not items:
        raise RuntimeError("no files")
    return {"engine": "gallery", "title": title, "author": author, "thumbnail": items[0]["thumbnail"], "duration": None, "items": items}


# ---------- routes ----------

@app.post("/api/info")
def info(link: Link):
    url = clean_url(link.url)
    platform = platform_of(url)
    # Both engines run at once. yt-dlp's format list is what powers the quality
    # picker, so it gets a short head start (~8s — syndication and the usual
    # clients answer in 2-6s). If it is still stuck after that and gallery-dl
    # already has media, take gallery's answer instead of waiting 20-60s.
    # (Sequentially, a slow or dead engine used to cost 20-60s before the
    # other one even started.)
    results: dict[str, dict | Exception] = {}

    def run(name: str, fn):
        try:
            results[name] = fn(url)
        except Exception as e:  # noqa: BLE001
            results[name] = e

    def has_media(name: str) -> bool:
        r = results.get(name)
        return isinstance(r, dict) and any(i["kind"] in ("video", "audio") for i in r.get("items", []))

    # No `with` block: exiting one would wait for the slow engine. The
    # straggler keeps running in its own thread and finishes on its own.
    ex = ThreadPoolExecutor(max_workers=2)
    ytdlp_fut = ex.submit(run, "ytdlp", probe_ytdlp)
    gallery_fut = ex.submit(run, "gallery", probe_gallery)
    try:
        done, _ = wait([ytdlp_fut], timeout=8)
        if ytdlp_fut not in done and not (gallery_fut.done() and has_media("gallery")):
            # Bound the whole lookup to ~50s. Vercel's free tier kills the
            # function at 60s, and a bot-checked lookup used to burn past that
            # and die with no answer at all. Better to reply in 50s with the
            # best error than to be killed at 60s with nothing.
            wait([ytdlp_fut], timeout=35)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    data = results.get("ytdlp")
    if not (isinstance(data, dict) and has_media("ytdlp")):
        # yt-dlp gave no media: give gallery-dl a short chance to finish, then use it.
        if not gallery_fut.done():
            wait([gallery_fut], timeout=7)
        data = results.get("gallery")
    if isinstance(data, dict):
        return {"url": url, "platform": platform, **data}
    err = results.get("ytdlp") if isinstance(results.get("ytdlp"), Exception) else results.get("gallery")
    if not isinstance(err, Exception):
        # both engines were still running when the time budget ran out
        err = RuntimeError("timed out")
    raise HTTPException(422, friendly(err, platform=platform))


def safe_name(s: str, fallback: str = "get-it") -> str:
    s = re.sub(r"[^\w\-. ]+", "", s or "").strip()[:60]
    return s or fallback


def stream_file(path: Path, workdir: str, filename: str, media_type: str) -> StreamingResponse:
    """Stream in chunks: serverless hosts (Vercel) cap buffered responses at 4.5 MB but not streamed ones."""

    def gen():
        try:
            with open(path, "rb") as f:
                while chunk := f.read(1024 * 256):
                    yield chunk
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return StreamingResponse(
        gen(),
        media_type=media_type,
        headers={
            "Content-Length": str(path.stat().st_size),
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


MEDIA = {".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".mp3": "audio/mpeg",
         ".m4a": "audio/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


def finish(paths: list[Path], workdir: str, base: str) -> StreamingResponse:
    if len(paths) == 1:
        p = paths[0]
        return stream_file(p, workdir, f"{safe_name(base)}{p.suffix}", MEDIA.get(p.suffix.lower(), "application/octet-stream"))
    zpath = Path(workdir) / f"{safe_name(base)}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for i, p in enumerate(sorted(paths), 1):
            z.write(p, f"{i:02d}{p.suffix}")
    return stream_file(zpath, workdir, zpath.name, "application/zip")


def files_in(d: str) -> list[Path]:
    return [p for p in Path(d).rglob("*") if p.is_file() and not p.name.endswith((".part", ".json", ".ytdl"))]


# ---------- fast path: relay the source file straight to the phone ----------

DIRECT_PROTOCOLS = {"http", "https"}


def codec_rank(f: dict) -> int:
    v = (f.get("vcodec") or "").lower()
    return 2 if v.startswith(("avc", "h264")) else 1 if v.startswith(("hev", "h265", "hvc", "vp9", "vp09")) else 0


def plan(url: str, quality: str, items: str = "") -> dict:
    """Read the post once and decide the fastest way to deliver the asked quality.
    direct: one ready-made file with sound and picture -> relay it straight from the CDN.
    spec:   exact format ids for yt-dlp to fetch (in parallel) and merge.
    Uses the same options as the info probe so the download re-uses the probe's
    cached extraction instead of re-extracting."""
    info, ydl, variant = extract(url, skip_download=True, extract_flat=False, playlistend=50)
    entries = [e for e in (info.get("entries") or []) if e]
    if entries:
        # A post with several files: the relay can only serve one. If exactly
        # one item was picked, serve that one; a lone entry serves itself.
        if len(entries) == 1 and not items:
            info = entries[0]
        elif items and "," not in items and items.strip().isdigit() and 1 <= int(items.strip()) <= len(entries):
            info = entries[int(items.strip()) - 1]
        else:
            return {}
    if not info:
        return {}
    formats = [f for f in (info.get("formats") or [info]) if f.get("url")]
    if quality == "audio":
        return {}
    q = int(quality) if quality.isdigit() else 10**6
    top_short = max((short_side(f) for f in formats if has_v(f)), default=0)
    want = min(q, top_short) if top_short else q

    prog = [f for f in formats if has_v(f) and has_a(f) and f.get("protocol") in ("http", "https")
            and short_side(f) <= q * 1.05]
    if prog:
        best = max(prog, key=lambda f: (short_side(f), codec_rank(f), f.get("tbr") or 0))
        if not want or short_side(best) >= want * 0.85:
            headers = dict(best.get("http_headers") or info.get("http_headers") or {})
            try:  # CDNs like TikTok's only serve the file to the session that read the post
                cookie = (ydl.cookiejar.get_cookie_header(best["url"]) if ydl
                          else _cookie_header(best["url"]))
                if cookie:
                    headers["Cookie"] = cookie
            except Exception:  # noqa: BLE001
                pass
            return {"direct": {
                "url": best["url"],
                "headers": headers,
                "ext": best.get("ext") or "mp4",
            }}
    vids = [f for f in formats if has_v(f) and short_side(f) <= q * 1.05]
    auds = [f for f in formats if has_a(f) and not has_v(f)]
    if vids and auds and FFMPEG:
        v = max(vids, key=lambda f: (short_side(f), codec_rank(f), f.get("tbr") or 0))
        a = max(auds, key=lambda f: ((f.get("ext") == "m4a"), f.get("abr") or f.get("tbr") or 0))
        if has_a(v):
            return {"spec": v["format_id"], "variant": variant}
        return {"spec": f"{v['format_id']}+{a['format_id']}", "variant": variant}
    if vids:
        v = max(vids, key=lambda f: (has_a(f), short_side(f), codec_rank(f)))
        return {"spec": v["format_id"], "variant": variant}
    return {"variant": variant}


async def relay(src: dict, name: str):
    # trust_env=False: the host's proxy env vars must never break the relay.
    # httpx parses NO_PROXY entries as URL patterns, and a bracketed IPv6 entry
    # like [::1] makes client construction raise "Invalid port: ':1]'". The
    # app's own PROXY setting (if any) is passed explicitly instead.
    upstream = None
    client = None
    for attempt in range(2):
        client = httpx.AsyncClient(timeout=httpx.Timeout(10, read=90), follow_redirects=True, http2=False,
                                   trust_env=False, proxy=PROXY or None,
                                   limits=httpx.Limits(max_connections=4))
        req = client.build_request("GET", src["url"], headers=src["headers"])
        upstream = await client.send(req, stream=True)
        if upstream.status_code < 400:
            break
        # A 403/429 can mean the CDN saw a different egress IP than the one that
        # read the post (hosts with rotating egress IPs). One retry on a fresh
        # connection is cheap and sometimes lands on the right one.
        retry = attempt == 0 and upstream.status_code in (403, 429)
        print(f"[get-it] relay got {upstream.status_code}, {'retrying once' if retry else 'falling back'}", flush=True)
        await upstream.aclose()
        await client.aclose()
        upstream, client = None, None
        if not retry:
            return None
    if upstream is None:  # both attempts were refused
        return None

    async def body():
        try:
            async for chunk in upstream.aiter_raw(1024 * 512):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {"Content-Disposition": f'attachment; filename="{safe_name(name)}.{src["ext"]}"', "Cache-Control": "no-store",
               "X-Getit-Path": "relay"}
    length = upstream.headers.get("content-length")
    if length and not upstream.headers.get("content-encoding"):
        headers["Content-Length"] = length
    return StreamingResponse(body(), media_type=MEDIA.get("." + src["ext"], "video/mp4"), headers=headers)


@app.get("/api/download")
async def download(
    url: str,
    quality: str = "best",
    engine: str = "ytdlp",
    items: str = Query("", description="comma list of 1-based item numbers; empty = all"),
    name: str = "get-it",
):
    url = clean_url(url)
    spec = None
    variant = None
    if engine == "ytdlp":
        try:
            p = await run_in_threadpool(plan, url, quality, items)
        except Exception as e:  # noqa: BLE001
            print(f"[get-it] plan failed: {e}", flush=True)
            p = {}
        if p.get("direct"):
            try:
                resp = await relay(p["direct"], name)
                if resp:
                    return resp
            except Exception as e:  # noqa: BLE001
                print(f"[get-it] relay failed, falling back: {e}", flush=True)
        spec = p.get("spec")
        variant = p.get("variant")
    return await run_in_threadpool(download_saved, url, quality, engine, items, name, spec, variant)


def download_saved(
    url: str,
    quality: str = "best",
    engine: str = "ytdlp",
    items: str = Query("", description="comma list of 1-based item numbers; empty = all"),
    name: str = "get-it",
    spec: str | None = None,
    variant: dict | None = None,
):
    workdir = tempfile.mkdtemp(prefix="getit-", dir=TMP)
    try:
        if engine == "gallery":
            cmd = gdl_base() + ["-D", workdir]
            if items:
                cmd += ["--range", items]
            cmd.append(url)
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0 and not files_in(workdir):
                raise RuntimeError(r.stderr)
        else:
            extra: dict = {"outtmpl": os.path.join(workdir, "%(autonumber)02d.%(ext)s"), "noplaylist": False}
            if items:
                extra["playlist_items"] = items
            if quality == "audio":
                if FFMPEG:
                    extra["format"] = "ba/b"
                    extra["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
                else:
                    extra["format"] = "ba[ext=m4a]/ba/b"
            elif spec:
                h = int(quality) if quality.isdigit() else 4320
                extra["format"] = f"{spec}/bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
                extra["merge_output_format"] = "mp4"
                if not items:  # a picked item needs the playlist selection to apply
                    extra["noplaylist"] = True
            elif FFMPEG:
                h = int(quality) if quality.isdigit() else 4320
                extra["format"] = f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
                extra["merge_output_format"] = "mp4"
            else:
                # no ffmpeg: only single files that already carry sound and picture
                h = int(quality) if quality.isdigit() else 4320
                extra["format"] = f"b[height<={h}][acodec!=none][vcodec!=none]/b[acodec!=none][vcodec!=none]/b"
            # A recorded winning variant is tried first, but a flaky win must
            # not doom the download: fall through the rest of the variants.
            tries = ([variant] + [v for v in variants(url) if v != variant]) \
                if variant is not None else variants(url)
            for i, v in enumerate(tries):
                try:
                    with yt_dlp.YoutubeDL(ydl_opts(**extra, **(v or {}))) as ydl:
                        ydl.download([url])
                    break
                except Exception as e:  # noqa: BLE001
                    if i == len(tries) - 1 or not blocked(e):
                        raise
        paths = files_in(workdir)
        if not paths:
            raise RuntimeError("nothing downloaded")
        return finish(paths, workdir, name)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(422, friendly(e, downloading=True, platform=platform_of(url)))


@app.get("/api/thumb")
async def thumb(u: str):
    """Proxy thumbnails so CDNs that block cross-site images (Instagram, TikTok) still show."""
    if not u.startswith("https://"):
        raise HTTPException(400)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, trust_env=False,
                                 proxy=PROXY or None) as c:
        r = await c.get(u, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(404)
    return Response(r.content, media_type=r.headers["content-type"], headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
def health():
    return {"ok": True, "yt_dlp": yt_dlp.version.__version__, "ffmpeg": bool(FFMPEG)}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
