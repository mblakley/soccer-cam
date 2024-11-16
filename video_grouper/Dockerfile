FROM python:3.10-slim
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget ca-certificates xz-utils && \
    wget -O ffmpeg-static.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz && \
    mkdir -p /usr/local/bin/ffmpeg-static && \
    tar -xJf ffmpeg-static.tar.xz --strip-components=1 -C /usr/local/bin/ffmpeg-static && \
    mv /usr/local/bin/ffmpeg-static/ffmpeg /usr/local/bin/ && \
    mv /usr/local/bin/ffmpeg-static/ffprobe /usr/local/bin/ && \
    chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe && \
    rm -rf ffmpeg-static.tar.xz /usr/local/bin/ffmpeg-static && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
COPY config.ini .
COPY video_grouper.py .
RUN pip install -r requirements.txt
CMD ["python", "./video_grouper.py"]
