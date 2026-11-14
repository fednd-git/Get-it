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
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from starlette.background import BackgroundTask

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
COOKIES = os.environ.get("COOKIES_FILE")  # optional Netscape cookies.txt for logged-in-only posts
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
        "socket_timeout": 20,
        "max_filesize": MAX_FILESIZE,
        # speed: pull HLS/DASH fragments in parallel, big buffers, no throttled single stream
        "concurrent_fragment_downloads": 16,
        "http_chunk_size": 10 * 1024 * 1024,
        "buffersize": 1024 * 1024,
        "retries": 3,
        "fragment_retries": 3,
    }
    if COOKIES and Path(COOKIES).exists():
        opts["cookiefile"] = COOKIES
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    opts.update(extra)
    return opts


def friendly(err: Exception) -> str:
    print(f"[get-it] {type(err).__name__}: {err}", flush=True)  # real reason shows in the host's logs
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

def short_side(f: dict) -> int:
    """'720p' means the short side: a portrait 720x1280 TikTok is 720p, not 1280p."""
    w, h = f.get("width") or 0, f.get("height") or 0
    return min(w, h) if w and h else (h or w)


def has_v(f): return f.get("vcodec") not in (None, "none")
def has_a(f): return f.get("acodec") not in (None, "none")


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
    has_audio = any(f.get("acodec") not in (None, "none") for f in formats) or info.get("acodec") not in (None, "none")

    qualities = []
    if heights:
        top = heights[0]
        for h in HEIGHTS:
            if any(h * 0.75 <= x <= h * 1.05 for x in heights):
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


def plan(url: str, quality: str) -> dict:
    """Read the post once and decide the fastest way to deliver the asked quality.
    direct: one ready-made file with sound and picture -> relay it straight from the CDN.
    spec:   exact format ids for yt-dlp to fetch (in parallel) and merge."""
    with yt_dlp.YoutubeDL(ydl_opts(skip_download=True, noplaylist=True)) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or info.get("_type") == "playlist" or info.get("entries"):
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
                cookie = ydl.cookiejar.get_cookie_header(best["url"])
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
            return {"spec": v["format_id"]}
        return {"spec": f"{v['format_id']}+{a['format_id']}"}
    if vids:
        v = max(vids, key=lambda f: (has_a(f), short_side(f), codec_rank(f)))
        return {"spec": v["format_id"]}
    return {}


async def relay(src: dict, name: str):
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=60), follow_redirects=True, http2=False,
                               limits=httpx.Limits(max_connections=4))
    req = client.build_request("GET", src["url"], headers=src["headers"])
    upstream = await client.send(req, stream=True)
    if upstream.status_code >= 400:
        print(f"[get-it] relay got {upstream.status_code}, falling back", flush=True)
        await upstream.aclose()
        await client.aclose()
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
    if engine == "ytdlp" and not items:
        try:
            p = await run_in_threadpool(plan, url, quality)
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
    return await run_in_threadpool(download_saved, url, quality, engine, items, name, spec)


def download_saved(
    url: str,
    quality: str = "best",
    engine: str = "ytdlp",
    items: str = Query("", description="comma list of 1-based item numbers; empty = all"),
    name: str = "get-it",
    spec: str | None = None,
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
                extra["format"] = spec
                extra["merge_output_format"] = "mp4"
                extra["noplaylist"] = True
            elif FFMPEG:
                h = int(quality) if quality.isdigit() else 4320
                extra["format"] = f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
                extra["merge_output_format"] = "mp4"
            else:
                # no ffmpeg: only single files that already carry sound and picture
                h = int(quality) if quality.isdigit() else 4320
                extra["format"] = f"b[height<={h}][acodec!=none][vcodec!=none]/b[acodec!=none][vcodec!=none]/b"
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
    return {"ok": True, "yt_dlp": yt_dlp.version.__version__, "ffmpeg": bool(FFMPEG)}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
