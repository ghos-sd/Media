FROM python:3.11-slim

# ffmpeg مهم للفيديو/الصوت
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# إعدادات بسيطة للّوجز
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY . .

# مكتبات بايثون
RUN pip install --no-cache-dir -r requirements.txt

# شغّل البوت (لاحظ bot.py)
CMD ["python", "bot.py"]
