FROM python:3.14-slim

# Build arguments for versioning
ARG VERSION=0.0.0
ARG BUILD_NUMBER=0

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /app

# Install uv, our package manager
RUN pip install uv

# Copy dependency definitions and README to leverage Docker cache
COPY pyproject.toml uv.lock README.md ./

# Install dependencies only (skip local package build for now)
RUN uv sync --no-install-project

# Copy the application source code
COPY video_grouper/ ./video_grouper/

# Now install the local package
RUN uv pip install -e .

# Create version.py with build arguments. This will overwrite any existing version.py
RUN echo '"""' > /app/video_grouper/version.py && \
    echo 'Version information for VideoGrouper.' >> /app/video_grouper/version.py && \
    echo 'This file is automatically updated during the build process.' >> /app/video_grouper/version.py && \
    echo '"""' >> /app/video_grouper/version.py && \
    echo '' >> /app/video_grouper/version.py && \
    echo "VERSION = '${VERSION}'" >> /app/video_grouper/version.py && \
    echo "BUILD_NUMBER = '${BUILD_NUMBER}'" >> /app/video_grouper/version.py

# Set version as environment variable for runtime access
ENV VERSION=${VERSION}
ENV BUILD_NUMBER=${BUILD_NUMBER}

# Set the entry point to run the application module
CMD ["python", "-m", "video_grouper"]
