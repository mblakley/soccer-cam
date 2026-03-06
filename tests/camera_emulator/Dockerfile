FROM python:3.13-slim
WORKDIR /app
RUN pip install aiohttp
COPY tests/camera_emulator/*.py ./
CMD ["python", "server.py"]
