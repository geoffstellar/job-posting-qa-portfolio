# syntax=docker/dockerfile:1.6
# QA Dashboard — production container.
#
# Build:   docker build -t cedarline-qa-dashboard .
# Run:     docker run -p 5000:5000 --env-file .env -v qa_data:/app/data cedarline-qa-dashboard
# Compose: docker compose up
#
# Base: python:3.13-slim-bookworm — ships a working glibc + enough of Debian
# to pip-install the anthropic SDK's native deps without apt-get trickery.

FROM python:3.13-slim-bookworm AS base

# Core Python hygiene for containers:
#   PYTHONDONTWRITEBYTECODE — no .pyc clutter in the image layer
#   PYTHONUNBUFFERED         — logs stream to stdout in real time
#   PIP_NO_CACHE_DIR         — smaller image (no ~/.cache/pip layer)
#   PIP_DISABLE_PIP_VERSION_CHECK — no noisy "newer pip available" warning
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Runtime defaults that compose / Fly / any host can override.
ENV APP_DATA_DIR=/app/data \
    HOST=0.0.0.0 \
    PORT=5000 \
    FLASK_ENV=production \
    USE_DB_COPY_WORKAROUND=0

# Minimal system deps:
#   curl — HEALTHCHECK probe hits /healthz with it
#   gosu — lets the entrypoint chown /app/data as root on boot, then drop
#          cleanly to the non-root `app` user before exec'ing gunicorn.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root app user + writable data dir owned by it. Most container hosts
# prefer non-root for defense-in-depth.
RUN groupadd --system app \
    && useradd --system --gid app --home /app --shell /usr/sbin/nologin app \
    && mkdir -p /app /app/data \
    && chown -R app:app /app

WORKDIR /app

# Install deps in a dedicated layer so code edits don't blow the pip cache.
COPY --chown=app:app requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source last — the layer that rebuilds on every code change.
# .dockerignore keeps .env / *.db* / __pycache__ / venv out.
COPY --chown=app:app . ./

# Ensure the data dir exists and the entrypoint is executable. We do NOT
# pre-chown /app/data here because hosts like Fly.io mount persistent
# volumes as root:root at runtime — entrypoint.sh chowns on every boot
# (a fast no-op when already correct). chmod the entrypoint too, since git
# on Windows sometimes drops the exec bit.
RUN mkdir -p /app/data \
    && chmod +x /app/entrypoint.sh

# NOTE: no USER directive. The entrypoint runs as root so it can chown the
# mounted volume, then drops to `app` via gosu before exec'ing alembic /
# gunicorn. See entrypoint.sh for the handoff.

EXPOSE 5000

# Healthcheck: Docker / Compose / Fly all respect this. 30s grace for app
# startup (alembic upgrade + gunicorn boot). The endpoint hits a trivial
# SELECT 1 against DATABASE_URL — see qa_dashboard.healthz().
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-5000}/healthz" || exit 1

# Entrypoint runs migrations then exec's gunicorn so gunicorn inherits PID 1
# and receives SIGTERM properly on `docker stop` / host restarts.
ENTRYPOINT ["/app/entrypoint.sh"]
