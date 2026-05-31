FROM python:3.12-slim

WORKDIR /app

# System deps for feedparser/requests
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY brief.py .

# Logs and brief archive persist via volume mount
RUN mkdir -p /app/logs/briefs

# Default: run in collect mode (called by cron at 6am)
# Override with "submit" for the evening cron
CMD ["python", "brief.py", "collect"]
