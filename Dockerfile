FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram_bot.py wallet.py bot_utils.py README.md ./
RUN mkdir -p /app/data

CMD ["python", "telegram_bot.py"]
