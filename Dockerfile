###############################################################################
#  Userbot Manager — production image
#
#  Two stages keep the runtime image small: build deps (gcc, headers) live in
#  the `builder` stage; only compiled wheels travel to the final image.
###############################################################################

FROM python:3.13-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build deps for cryptography, psycopg2, tgcrypto.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt && \
    # tgcrypto is optional in requirements.txt because Windows can't build
    # it. Inside this Linux builder, install it for the 10x Telethon speedup.
    pip wheel --wheel-dir /wheels tgcrypto==1.2.5


###############################################################################
#  Runtime
###############################################################################
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings

# Run as a non-root user. UID/GID 1000 matches typical desktop Linux defaults
# so bind-mounted volumes from the host don't end up root-owned.
RUN groupadd --system --gid 1000 app && \
    useradd  --system --uid 1000 --gid app --create-home --shell /sbin/nologin app

# libpq5 + curl for healthchecks. No build deps in the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

# Source last so unrelated code changes don't bust the dep-install layer.
COPY --chown=app:app . .

# Bulletproof entrypoint:
#   - sed strips CRLF — when the repo is checked out on Windows with
#     core.autocrlf=true, the shebang reads as `#!/bin/sh\r` and the kernel
#     fails to find the interpreter (surfaces as exit 126 from tini).
#   - chmod +x — NTFS doesn't preserve the POSIX exec bit through Docker COPY.
# Both are safe no-ops when the file is already correct.
# Same RUN also creates the runtime dirs (incl. /app/logs for the new LOGGING).
RUN sed -i 's/\r$//' /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh \
    && mkdir -p /app/media /app/staticfiles /app/data /app/logs \
    && chown -R app:app /app/media /app/staticfiles /app/data /app/logs

USER app

EXPOSE 8000

# tini is PID 1 so Ctrl+C / SIGTERM reaches uvicorn cleanly.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["web"]
