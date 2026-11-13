FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1

# Install system dependencies (ffmpeg is essential for video muxing and thumbnails)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r ./requirements.txt

# Copy all project files
COPY . .

# Expose port (Koyeb uses port 8000 by default)
EXPOSE 8000

# Run both Web Server (for Koyeb health checks) and Telegram Bot
CMD ["python", "app.py"]
