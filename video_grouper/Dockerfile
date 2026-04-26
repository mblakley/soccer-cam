# Multi-stage GPU-capable image. Single tag works on hosts with or
# without an NVIDIA GPU — onnxruntime falls back to CPUExecutionProvider
# when CUDA isn't available. To actually use the GPU on a capable host,
# pass `--gpus all` (docker run) or set deploy.resources.reservations.devices
# (docker compose).

ARG CUDA_BASE=nvidia/cuda:12.6.3-runtime-ubuntu22.04
ARG PYTHON_VERSION=3.13

# ─── Builder ──────────────────────────────────────────────────────────
FROM ${CUDA_BASE} AS builder

ARG PYTHON_VERSION
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates curl && \
    add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        python${PYTHON_VERSION} python${PYTHON_VERSION}-venv && \
    rm -rf /var/lib/apt/lists/* && \
    ln -sf /usr/bin/python${PYTHON_VERSION} /usr/local/bin/python

# uv standalone installer (no pip bootstrap)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -sf /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY video_grouper/ ./video_grouper/

# Build a self-contained .venv with only runtime deps (no dev/tray/service extras).
# --frozen requires uv.lock to match pyproject.toml.
RUN uv sync --frozen --no-dev

# ─── Runtime ──────────────────────────────────────────────────────────
FROM ${CUDA_BASE} AS runtime

ARG VERSION=0.0.0
ARG BUILD_NUMBER=0
ARG PYTHON_VERSION
ENV DEBIAN_FRONTEND=noninteractive

# Only the interpreter in the runtime stage. PyAV bundles libav* via wheel —
# no system ffmpeg needed. software-properties-common is purged after we're
# done with deadsnakes to keep the image small.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates && \
    add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y --no-install-recommends python${PYTHON_VERSION} && \
    apt-get purge -y software-properties-common && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/* && \
    ln -sf /usr/bin/python${PYTHON_VERSION} /usr/local/bin/python

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY video_grouper/ /app/video_grouper/

ENV PATH="/app/.venv/bin:${PATH}"

RUN printf '"""Version information for VideoGrouper."""\nVERSION = "%s"\nBUILD_NUMBER = "%s"\nFULL_VERSION = f"{VERSION}+{BUILD_NUMBER}"\n__version__ = VERSION\n__version_full__ = FULL_VERSION\n' \
        "${VERSION}" "${BUILD_NUMBER}" > /app/video_grouper/version.py

ENV VERSION=${VERSION}
ENV BUILD_NUMBER=${BUILD_NUMBER}

CMD ["python", "-m", "video_grouper"]
