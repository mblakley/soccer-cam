FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
COPY config.ini .
COPY video_grouper.py .
RUN pip install -r requirements.txt
CMD ["python", "video_grouper.py"]
