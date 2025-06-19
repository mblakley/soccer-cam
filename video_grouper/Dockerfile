FROM python:3.10-slim

# Build arguments for versioning
ARG VERSION=0.0.0
ARG BUILD_NUMBER=0

# Install ffmpeg
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

# Set up working directory
WORKDIR /app

# Copy only necessary files
COPY video_grouper/__init__.py /app/video_grouper/
COPY video_grouper/__main__.py /app/video_grouper/
COPY video_grouper/video_grouper.py /app/video_grouper/
COPY video_grouper/ffmpeg_utils.py /app/video_grouper/
COPY video_grouper/models.py /app/video_grouper/
COPY video_grouper/match_info.ini.dist /app/video_grouper/
COPY video_grouper/cameras /app/video_grouper/cameras

# Create version.py with build arguments
RUN echo '"""' > /app/video_grouper/version.py && \
    echo 'Version information for VideoGrouper.' >> /app/video_grouper/version.py && \
    echo 'This file is automatically updated during the build process.' >> /app/video_grouper/version.py && \
    echo '"""' >> /app/video_grouper/version.py && \
    echo '' >> /app/video_grouper/version.py && \
    echo "VERSION = '${VERSION}'" >> /app/video_grouper/version.py && \
    echo "BUILD_NUMBER = '${BUILD_NUMBER}'" >> /app/video_grouper/version.py

# Install dependencies directly
RUN pip install httpx aiofiles

# Set version as environment variable
ENV VERSION=${VERSION}
ENV BUILD_NUMBER=${BUILD_NUMBER}

# Set the entry point
CMD ["python", "-m", "video_grouper"]
