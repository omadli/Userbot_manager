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

# Permanent dirs that need to be writable at runtime.
RUN mkdir -p /app/media /app/staticfiles /app/data \
    && chown -R app:app /app/media /app/staticfiles /app/data

USER app

EXPOSE 8000

# tini is PID 1 so Ctrl+C / SIGTERM reaches uvicorn cleanly.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["web"]
