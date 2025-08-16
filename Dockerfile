FROM python:3.11-slim

# Install ffmpeg (مهم للفيديو والصوت)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set workdir
WORKDIR /app

# Copy project files
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run the bot (لاحظ الاسم هنا مطابق للملف عندك)
CMD ["python", "bot(1).py"]
