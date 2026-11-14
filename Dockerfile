FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PORT=8000
EXPOSE 8000
# keep the engines fresh: sites change often, so update yt-dlp + gallery-dl on every boot
CMD pip install --no-cache-dir -q -U yt-dlp gallery-dl; uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers
