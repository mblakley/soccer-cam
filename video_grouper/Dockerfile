FROM python:3.10-slim

# Build arguments for versioning
ARG VERSION=0.0.0
ARG BUILD_NUMBER=0

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

COPY requirements-docker.txt .
RUN pip install -r requirements-docker.txt

# Create version.py with build arguments
RUN cat > version.py << EOF
"""
Version information for VideoGrouper.
This file is automatically updated during the build process.
"""

VERSION = "${VERSION}"
BUILD_NUMBER = "${BUILD_NUMBER}"
EOF

COPY video_grouper.py .
COPY match_info.ini.dist .

# Set version as environment variable
ENV VERSION=${VERSION}
ENV BUILD_NUMBER=${BUILD_NUMBER}

CMD ["python", "./video_grouper.py"]
