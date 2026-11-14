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

## Private posts and blocked sites

Some posts (private accounts, age-gated or login-only content) need a signed-in session. Export a `cookies.txt` from your browser (Netscape format) for that site, add it to the server as a secret file, and set:

```
COOKIES_FILE=/etc/secrets/cookies.txt
```

Use a spare account for this, not your main one.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `COOKIES_FILE` | none | Path to a cookies.txt for login-only posts |
| `MAX_FILESIZE_MB` | 500 | Biggest file the server will fetch |
| `PORT` | 8000 | Port to listen on |

## How it works

```
app/main.py        FastAPI: /api/info reads the link, /api/download fetches and streams the file
app/static/        the paste screen (HTML, CSS, JS), PWA manifest with share target
Dockerfile         Python + ffmpeg; updates yt-dlp and gallery-dl on every start
render.yaml        one-click deploy config
```

Sites change their pages often. When a platform stops working, a restart pulls the newest yt-dlp and gallery-dl, which usually fixes it.

Only download content you have the right to save.
