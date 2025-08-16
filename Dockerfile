# استخدم صورة Python خفيفة
FROM python:3.11-slim

# إعداد متغيرات البيئة
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# تثبيت الحزم الأساسية
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# إنشاء مجلد للتطبيق
WORKDIR /app

# نسخ ملفات المشروع
COPY . /app

# تثبيت المتطلبات
RUN pip install --no-cache-dir -r requirements.txt

# تشغيل البوت
CMD ["python", "bot.py"]
