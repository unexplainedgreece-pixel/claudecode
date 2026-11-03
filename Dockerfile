FROM python:3.13-slim

# ffmpeg for video posts, fonts for captions, build tools for a few wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p data/images data/audio data/videos data/uploaded_videos data/bg_music data/stock

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python3", "-m", "uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
