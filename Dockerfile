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

# The entrypoint is the program; the MODE is the command argument, so a single
# image serves every cron job: `docker run <image> submit|collect|weekly|...`.
# CMD is the default mode when none is supplied (the morning collect).
# (To get a shell for debugging: `docker run --entrypoint bash <image>`.)
ENTRYPOINT ["python", "brief.py"]
CMD ["collect"]
