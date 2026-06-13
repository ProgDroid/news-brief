FROM python:3.12-slim

WORKDIR /app

# System deps for feedparser/requests (curl removed: nothing in the image uses it)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common.py trading.py brief.py .

# Logs and brief archive persist via volume mount. Run as a real non-root user
# so a bare `docker run` is unprivileged too — docker-compose's `user:` still
# overrides this with the host uid:gid that owns the volume.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 1000 newsbrief \
    && mkdir -p /app/logs/briefs \
    && chown -R newsbrief:newsbrief /app/logs
USER newsbrief

# The entrypoint is the program; the MODE is the command argument, so a single
# image serves every cron job: `docker run <image> submit|collect|weekly|...`.
# CMD is the default mode when none is supplied (the morning collect).
# (To get a shell for debugging: `docker run --entrypoint bash <image>`.)
ENTRYPOINT ["python", "brief.py"]
CMD ["collect"]
