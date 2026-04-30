#!/bin/sh
set -e

# Userbot Manager container entrypoint.
#
# Usage (set as Dockerfile CMD or compose `command:`):
#   init     — one-shot: migrations + collectstatic, then exit (compose `init` service)
#   web      — start the ASGI server (assumes `init` already ran)
#   worker   — start the background task worker
#   migrate  — one-shot: just run migrations and exit
#   shell    — interactive Django shell
#   manage <subcommand>  — pass through to manage.py

ROLE="${1:-web}"
shift || true

# Wait until Postgres is ready when DB_ENGINE=postgres. web/worker/init all
# share the same image so any role might boot before the DB is healthy.
wait_for_postgres() {
    if [ "$DB_ENGINE" = "postgres" ]; then
        host="${POSTGRES_HOST:-db}"
        port="${POSTGRES_PORT:-5432}"
        echo "[entrypoint] waiting for postgres at $host:$port…"
        i=0
        until python -c "import socket,sys; s=socket.socket(); s.settimeout(2);
try:
    s.connect(('$host', $port))
except Exception as e:
    sys.exit(1)
" 2>/dev/null; do
            i=$((i+1))
            if [ "$i" -ge 60 ]; then
                echo "[entrypoint] postgres not reachable after 60 attempts — aborting"
                exit 1
            fi
            sleep 1
        done
        echo "[entrypoint] postgres is up"
    fi
}

case "$ROLE" in
    init)
        # One-shot setup: migrations + collectstatic. Compose runs this with
        # restart: "no" and `web` waits for it via service_completed_successfully,
        # so multi-replica web rollouts don't race on migrate / static manifest.
        wait_for_postgres
        echo "[entrypoint] running migrations…"
        python manage.py migrate --noinput
        echo "[entrypoint] collecting static…"
        python manage.py collectstatic --noinput
        echo "[entrypoint] init complete"
        ;;

    web)
        wait_for_postgres
        echo "[entrypoint] starting uvicorn"
        exec uvicorn config.asgi:application \
            --host 0.0.0.0 \
            --port 8000 \
            --workers "${WEB_WORKERS:-2}" \
            --proxy-headers \
            --forwarded-allow-ips='*' \
            --no-access-log
        ;;

    worker)
        wait_for_postgres
        # `--reset-stuck` flips any tasks left in `running` from a previous
        # crashed worker back to `failed`, so they don't block the queue.
        echo "[entrypoint] starting background worker"
        exec python manage.py run_worker --reset-stuck
        ;;

    migrate)
        wait_for_postgres
        exec python manage.py migrate --noinput
        ;;

    shell)
        wait_for_postgres
        exec python manage.py shell
        ;;

    manage)
        wait_for_postgres
        exec python manage.py "$@"
        ;;

    *)
        # Pass-through: anything else runs as-is. Useful for ad-hoc commands.
        exec "$ROLE" "$@"
        ;;
esac
