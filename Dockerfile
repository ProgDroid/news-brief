FROM python:3.15-slim

WORKDIR /app

# System deps for feedparser/requests (curl removed: nothing in the image uses it).
# `upgrade` is deliberate: python:3.12-slim lags Debian security updates by days
# to weeks, and the CI Trivy gate fails the run on a FIXABLE CRITICAL/HIGH. Without
# this the build ships whatever the base image last baked — 2026-08-31 that was
# openssl 3.5.6-1~deb13u2, one patch behind CVE-2026-14456. Upgrading here patches
# the class, not the instance, so the next CVE does not need another commit.
#
# APT_REFRESH is what makes that true in CI (news-brief-y49). The gha layer cache
# keys a RUN on its text and its parent, neither of which changes when Debian
# ships a fix, so this upgrade and the PGDG install below (which pulls krb5 in
# through libpq) were served from cache: on 2026-11-23 Trivy failed on krb5
# deb13u1 while deb13u2 was already the Debian candidate. The workflow passes a
# per-run value, so these layers and everything after them rebuild on every run;
# both builds in one run share it, so the pushed image is the scanned one.
# Echoed rather than merely declared so the RUN visibly depends on it.
ARG APT_REFRESH=unset
RUN echo "apt refresh: ${APT_REFRESH}" \
    && apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# pg_dump for the scheduled backup job, from PGDG rather than Debian.
# This is NOT interchangeable with `postgresql-client`: Debian trixie's is
# version 17, and pg_dump REFUSES to dump a server newer than itself, so the
# Debian package would produce a backup job that fails every night and looks
# from the outside exactly like a job that has not run yet. The major here must
# track the `postgres:` image major in docker-compose.yml -- backup.py compares
# the two at runtime and refuses loudly if they ever diverge, so this coupling
# alerts instead of rotting.
# The signing key is fetched with the interpreter already in the image, which is
# why curl still does not need to come back.
RUN . /etc/os-release \
    && install -d /usr/share/keyrings \
    && python -c "import urllib.request; urllib.request.urlretrieve('https://www.postgresql.org/media/keys/ACCC4CF8.asc','/usr/share/keyrings/pgdg.asc')" \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Fail the BUILD, not a 06:30 run, if the zone database is missing (watch.py).
RUN python -c "import zoneinfo; zoneinfo.ZoneInfo('Europe/London'); zoneinfo.ZoneInfo('America/New_York')"

# This list is an allowlist: a module absent from it does not exist inside the
# image, however present it is in the repo. tests/test_packaging.py walks the
# import graph and fails when one is missing, because nothing else can -- pytest
# and CI both run against a full checkout, where the absence is invisible.
COPY common.py config.py trading.py validation.py brief.py capture.py comprehend.py brief_memory.py claim_store.py claim_verify.py retention.py db.py scheduler.py supervisor.py backup.py census.py census_metrics.py labeller.py watch.py dated_memory.py storyline_store.py storylines.py embeddings.py group_store.py grouping.py retrieval.py .
COPY migrations/ ./migrations/
COPY enrichment/ ./enrichment/
COPY labeller_static/ ./labeller_static/

# news-brief-115: no longer temporary. Added 2026-09-05 for the comprehension
# gate; it now has standing consumers, all run from inside the container:
#
#   docker compose run --rm --entrypoint python newsbrief scripts/embed_backfill.py ...
#   docker compose run --rm --entrypoint python newsbrief scripts/embed_calibrate.py ...
#   docker compose run --rm --entrypoint python newsbrief scripts/storylines_fetch_probe.py
#   docker compose run --rm --entrypoint python newsbrief scripts/brief_cost.py
#   docker compose run --rm --entrypoint python newsbrief scripts/grouping_rules_check.py ...
#   docker compose run --rm --entrypoint python newsbrief scripts/grouping_decider_dev.py --dev
#   docker compose run --rm --entrypoint python newsbrief scripts/grouping_label_audit.py ...
#
# They are the embedding backfill, the calibration, the fetch probe, brief_cost,
# and the pre-registered grouping rules check, decider selection and label
# audit. Nothing in `scripts/` is runtime code and the supervisor never invokes
# it; do not remove this line without moving those seven elsewhere.
COPY scripts/ ./scripts/

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
