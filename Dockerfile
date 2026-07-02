FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Команда задаётся в docker-compose per-service:
#   python -m userbot | python -m bot | python -m worker | python -m db.migrate
CMD ["python", "-m", "bot"]
