FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir aiohttp pycryptodome av

EXPOSE 80 8080 9000

CMD ["python", "server.py"]
