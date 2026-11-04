FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir yt-dlp==2026.10.21 m3u8==6.0.0
WORKDIR /app
COPY archive.py chat_archive.py archive_insights.py transfer_stats.py archive_schedule.py /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Moscow
ARG APP_UID=1000
ARG APP_GID=1000
USER ${APP_UID}:${APP_GID}
ENTRYPOINT ["python", "/app/archive.py"]
