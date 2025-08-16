FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ffmpeg عشان التحويل، و curl لو احتجته لفحصات بسيطة
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

# للبيئات اللي ما فيها Start Command
CMD ["python", "media_bot.py"]
