FROM python:3.10-slim
WORKDIR /app

# Install required packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget ca-certificates xz-utils git && \
    wget -O ffmpeg-static.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz && \
    mkdir -p /usr/local/bin/ffmpeg-static && \
    tar -xJf ffmpeg-static.tar.xz --strip-components=1 -C /usr/local/bin/ffmpeg-static && \
    mv /usr/local/bin/ffmpeg-static/ffmpeg /usr/local/bin/ && \
    mv /usr/local/bin/ffmpeg-static/ffprobe /usr/local/bin/ && \
    chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe && \
    rm -rf ffmpeg-static.tar.xz /usr/local/bin/ffmpeg-static && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
    
# Install Python dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy application files
COPY video_grouper_onvif.py .
COPY config.ini .

CMD ["python", "video_grouper_onvif.py"]
