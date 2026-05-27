FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# /data is the Railway-mounted volume where backups live persistently.
ENV DATA_DIR=/data
CMD ["python", "bot.py"]
