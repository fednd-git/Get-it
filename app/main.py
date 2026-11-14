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
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
COOKIES = os.environ.get("COOKIES_FILE")  # optional Netscape cookies.txt for logged-in-only posts
MAX_FILESIZE = int(os.environ.get("MAX_FILESIZE_MB", "500")) * 1024 * 1024

app = FastAPI(title="Get-it", docs_url=None, redoc_url=None)

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
        "socket_timeout": 20,
        "max_filesize": MAX_FILESIZE,
    }
    if COOKIES and Path(COOKIES).exists():
        opts["cookiefile"] = COOKIES
    opts.update(extra)
    return opts


def friendly(err: Exception) -> str:
    msg = str(err).lower()
    if "private" in msg or "login" in msg or "cookies" in msg or "rate-limit" in msg:
        return "This post is private or needs a login to view."
    if "unsupported url" in msg:
        return "This site isn't supported yet."
    if "404" in msg or "not found" in msg or "unavailable" in msg or "removed" in msg:
        return "This post is gone or the link has expired. Copy it again."
    if "max_filesize" in msg or "larger than" in msg:
        return "That file is too big for this server."
    return "Couldn't read that link. Check it opens in your browser, then try again."


# ---------- yt-dlp ----------

def size_for(formats: list[dict], height: int | None) -> int | None:
    def fs(f):
        return f.get("filesize") or f.get("filesize_approx")

    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none") and fs(f)]
    best_audio = max((fs(f) for f in audio), default=0)
    if height is None:
        return best_audio or None
    vids = [f for f in formats if f.get("vcodec") not in (None, "none") and (f.get("height") or 0) <= height and fs(f)]
    if not vids:
        return None
    top = max(vids, key=lambda f: ((f.get("height") or 0), fs(f)))
    extra = best_audio if top.get("acodec") in (None, "none") else 0
    return fs(top) + extra


def item_from_info(info: dict) -> dict:
    formats = info.get("formats") or []
    heights = sorted({f.get("height") for f in formats if f.get("vcodec") not in (None, "none") and f.get("height")}, reverse=True)
    has_video = bool(heights) or info.get("vcodec") not in (None, "none") and info.get("ext") in ("mp4", "webm", "mov")
    has_audio = any(f.get("acodec") not in (None, "none") for f in formats) or info.get("acodec") not in (None, "none")

    qualities = []
    if heights:
        top = heights[0]
        for h in HEIGHTS:
            if h <= top and any(x >= h * 0.9 for x in heights):
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


def probe_ytdlp(url: str) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts(skip_download=True, extract_flat=False, playlistend=50)) as ydl:
        info = ydl.extract_info(url, download=False)
    info = ydl.sanitize_info(info)
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
    return cmd


def probe_gallery(url: str) -> dict:
    r = subprocess.run(gdl_base() + ["--dump-json", url], capture_output=True, text=True, timeout=60)
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
    first_err: Exception | None = None
    try:
        data = probe_ytdlp(url)
        if any(i["kind"] in ("video", "audio") for i in data["items"]):
            return {"url": url, "platform": platform_of(url), **data}
    except Exception as e:  # noqa: BLE001
        first_err = e
    try:
        return {"url": url, "platform": platform_of(url), **probe_gallery(url)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, friendly(first_err or e))


def safe_name(s: str, fallback: str = "get-it") -> str:
    s = re.sub(r"[^\w\-. ]+", "", s or "").strip()[:60]
    return s or fallback


def finish(paths: list[Path], workdir: str, base: str) -> FileResponse:
    cleanup = BackgroundTask(shutil.rmtree, workdir, ignore_errors=True)
    if len(paths) == 1:
        p = paths[0]
        return FileResponse(p, filename=f"{safe_name(base)}{p.suffix}", background=cleanup)
    zpath = Path(workdir) / f"{safe_name(base)}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for i, p in enumerate(sorted(paths), 1):
            z.write(p, f"{i:02d}{p.suffix}")
    return FileResponse(zpath, filename=zpath.name, media_type="application/zip", background=cleanup)


def files_in(d: str) -> list[Path]:
    return [p for p in Path(d).rglob("*") if p.is_file() and not p.name.endswith((".part", ".json", ".ytdl"))]


@app.get("/api/download")
def download(
    url: str,
    quality: str = "best",
    engine: str = "ytdlp",
    items: str = Query("", description="comma list of 1-based item numbers; empty = all"),
    name: str = "get-it",
):
    url = clean_url(url)
    workdir = tempfile.mkdtemp(prefix="getit-")
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
                extra["format"] = "ba/b"
                extra["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            elif quality.isdigit():
                h = int(quality)
                extra["format"] = f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b"
                extra["merge_output_format"] = "mp4"
            else:
                extra["format"] = "bv*+ba/b"
                extra["merge_output_format"] = "mp4"
            with yt_dlp.YoutubeDL(ydl_opts(**extra)) as ydl:
                ydl.download([url])
        paths = files_in(workdir)
        if not paths:
            raise RuntimeError("nothing downloaded")
        return finish(paths, workdir, name)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(422, friendly(e))


@app.get("/api/thumb")
async def thumb(u: str):
    """Proxy thumbnails so CDNs that block cross-site images (Instagram, TikTok) still show."""
    if not u.startswith("https://"):
        raise HTTPException(400)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(u, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(404)
    return Response(r.content, media_type=r.headers["content-type"], headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
def health():
    return {"ok": True, "yt_dlp": yt_dlp.version.__version__}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
