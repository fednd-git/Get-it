# Get-it

Paste a link from Instagram, TikTok, Facebook, X, YouTube, Reddit, Pinterest and 1,000+ other sites. Get the video, the images, or the audio.

- **Videos** through [yt-dlp](https://github.com/yt-dlp/yt-dlp), with quality picks (1080p, 720p, 480p…) and Audio (MP3).
- **Image posts and carousels** through [gallery-dl](https://github.com/mikf/gallery-dl). Pick the slides you want; several come down as one zip.
- **Mobile first.** Install it to your Android home screen, then share a post straight to Get-it from the app's Share button.
- **Recent downloads** stay on your device only.

## Run it locally

```bash
pip install -r requirements.txt   # needs ffmpeg installed too
uvicorn app.main:app --reload
# open http://localhost:8000
```

Or with Docker:

```bash
docker build -t get-it .
docker run -p 8000:8000 get-it
```

## Put it online (free)

**Vercel** (what Get-it runs on now): import the repo at vercel.com/new. It finds the FastAPI app in `app/main.py`, and `vercel.json` gives downloads up to 5 minutes. ffmpeg comes from the `imageio-ffmpeg` package, so merging HD video and making MP3s work without a system install. Files stream back, so there's no 4.5 MB cap.

**Render / any Docker host**: [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/fednd-git/Get-it) uses the Dockerfile.

Then on your phone, open the link in Chrome, tap ⋮ and **Install app**. Get-it shows up in Android's Share menu.

## YouTube, private posts and blocked sites

**Facebook and Instagram need a login now.** Meta only serves their videos to logged-in browsers from cloud servers, even public posts — without cookies every link fails with a login error (or stalls forever). The fix is your own cookies:

1. Sign in to that site in a desktop browser with a **spare** account.
2. Export its cookies as `cookies.txt` (Netscape format) with an extension like "Get cookies.txt LOCALLY".
3. On Vercel: Project, Settings, Environment Variables, add `COOKIES_TXT` with the file's contents (or base64 of it). Redeploy.

The same cookies also fix YouTube when it throws its "confirm you're not a bot" check at the server. Get-it already retries YouTube through several app clients, and the lookup is capped to answer within ~50 seconds — Vercel's free tier kills a function at 60 seconds, so a slow bot-checked lookup used to die with no answer at all.

Two more levers, only if cookies aren't enough:

- `PO_TOKEN`: a pre-generated YouTube Proof-of-Origin token, format `web.gvs+<token>`. It proves the request came from a real browser, but it expires after hours/days and has to be refreshed — cookies last longer.
- `PROXY`: a residential proxy also gets past cloud-IP blocks. This one costs money; cookies are free.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `COOKIES_TXT` | none | Contents of a cookies.txt (raw or base64), for bot checks and login-only posts |
| `COOKIES_FILE` | none | Path to a cookies.txt instead (Docker hosts) |
| `PO_TOKEN` | none | Pre-generated YouTube Proof-of-Origin token, e.g. `web.gvs+<token>`; expires, refresh when YouTube blocks again |
| `PROXY` | none | http/https/socks proxy for sites that block cloud servers |
| `MAX_FILESIZE_MB` | 500 | Biggest file the server will fetch |
| `PORT` | 8000 | Port to listen on (Docker) |

## How it works

```
app/main.py        FastAPI: /api/info reads the link, /api/download fetches and streams the file
app/static/        the paste screen (HTML, CSS, JS), PWA manifest with share target
Dockerfile         Python + ffmpeg; updates yt-dlp and gallery-dl on every start
render.yaml        one-click deploy config
```

Sites change their pages often. When a platform stops working, a restart pulls the newest yt-dlp and gallery-dl, which usually fixes it.

Only download content you have the right to save.
