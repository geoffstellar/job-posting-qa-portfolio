#!/bin/sh
# QA Dashboard — container entrypoint.
#
# Two modes:
#   1. Called with NO args → full app boot: `alembic upgrade head`, then
#      exec gunicorn. The normal startup path.
#   2. Called WITH args → exec the args as the `app` user and exit. This is
#      how Fly.io's release_command works: Fly invokes
#      `entrypoint.sh alembic upgrade head` in a one-off machine and expects
#      it to exit when the command completes. If we ignored the args and ran
#      gunicorn instead, the release machine would stay alive forever and the
#      deploy would time out.
#
# Privilege handling: we run as root on entry so we can chown /app/data
# (hosts like Fly mount fresh persistent volumes owned by root, which the
# non-root `app` user can't write to until ownership is fixed). After the
# chown, every command runs under `app` via `gosu`, so the web server and
# migrations execute without root.
#
# Env vars honoured (all have sane defaults from the Dockerfile):
#   PORT                — listen port (default 5000)
#   GUNICORN_WORKERS    — worker processes (default 2)
#   GUNICORN_THREADS    — threads per worker (default 4)
#   GUNICORN_TIMEOUT    — worker kill timeout in seconds (default 120)
#   GUNICORN_EXTRA_ARGS — extra flags (e.g. --log-level debug)

set -eu

# Chown the mounted volume so the non-root app user can write. Idempotent
# and fast — a no-op if already correctly owned.
if [ -d /app/data ]; then
    chown -R app:app /app/data
fi

# Mode 2 — release_command / arbitrary commands. One-shot machines should
# exec the command and exit, not start the long-running server.
if [ "$#" -gt 0 ]; then
    echo "[entrypoint] running with args (as app): $*"
    exec gosu app "$@"
fi

# Mode 1 — normal app boot. `alembic upgrade head` is a no-op when the DB is
# already at head; on a fresh volume it creates the full schema.
echo "[entrypoint] alembic upgrade head (as app)"
gosu app alembic upgrade head

: "${PORT:=5000}"
: "${GUNICORN_WORKERS:=2}"
: "${GUNICORN_THREADS:=4}"
: "${GUNICORN_TIMEOUT:=120}"
: "${GUNICORN_EXTRA_ARGS:=}"

echo "[entrypoint] starting gunicorn on 0.0.0.0:${PORT} (as app)"
# shellcheck disable=SC2086   # intentional word-splitting on GUNICORN_EXTRA_ARGS
exec gosu app gunicorn \
    --bind "0.0.0.0:${PORT}" \
    --workers "${GUNICORN_WORKERS}" \
    --threads "${GUNICORN_THREADS}" \
    --timeout "${GUNICORN_TIMEOUT}" \
    --access-logfile - \
    --error-logfile - \
    ${GUNICORN_EXTRA_ARGS} \
    qa_dashboard:app
