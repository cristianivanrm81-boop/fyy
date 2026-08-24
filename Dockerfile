FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py token_manager.py ./

# Data files live in /app/data — mount a volume here to persist across deploys
RUN mkdir -p /app/data

ENV TOKENS_FILE=/app/data/tokens.json \
    COOLDOWNS_FILE=/app/data/cooldowns.json \
    STATS_FILE=/app/data/stats.json \
    CHANNELS_FILE=/app/data/channels.json

CMD ["python", "bot.py"]
