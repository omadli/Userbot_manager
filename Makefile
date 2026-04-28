# Convenience shortcuts. Run `make help` for the catalog.
#
# All targets are safe to invoke from a Git Bash / WSL shell on Windows.
# `docker compose` (v2 syntax) is required.

.DEFAULT_GOAL := help
.PHONY: help up down logs restart shell migrate makemigrations \
        superuser worker-logs build pull rebuild backup restore \
        dev dev-down secrets check-deploy backup-telegram

PYTHON ?= venv/Scripts/python.exe
COMPOSE ?= docker compose

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Production (Docker) ─────────────────────────────────────────────────────

up: ## Start prod stack (web + worker + db + caddy)
	$(COMPOSE) up -d

down: ## Stop prod stack (data preserved in volumes)
	$(COMPOSE) down

restart: ## Restart all prod services
	$(COMPOSE) restart

logs: ## Tail web + worker logs (Ctrl+C to stop)
	$(COMPOSE) logs -f web worker

worker-logs: ## Tail only the worker logs
	$(COMPOSE) logs -f worker

build: ## Rebuild the app image (after code changes)
	$(COMPOSE) build

rebuild: ## Rebuild image AND recreate containers
	$(COMPOSE) up -d --build

shell: ## Django shell inside the web container
	$(COMPOSE) exec web /app/entrypoint.sh shell

migrate: ## Apply migrations (idempotent — safe to run anytime)
	$(COMPOSE) exec web /app/entrypoint.sh manage migrate

makemigrations: ## Generate new migrations from model changes
	$(COMPOSE) exec web /app/entrypoint.sh manage makemigrations

superuser: ## Create an admin user
	$(COMPOSE) exec web /app/entrypoint.sh manage createsuperuser

encrypt-secrets: ## Re-encrypt legacy plaintext sessions in DB
	$(COMPOSE) exec web /app/entrypoint.sh manage encrypt_existing

check-deploy: ## Run `manage.py check --deploy` against production settings
	$(COMPOSE) exec -T -e DEBUG=False web python manage.py check --deploy

# ─── Backups ─────────────────────────────────────────────────────────────────

backup: ## Dump postgres + media to ./backups/<timestamp>/, then push to Telegram
	@mkdir -p backups
	@ts=$$(date +%Y%m%d_%H%M%S); \
	dir="backups/$$ts"; \
	project=$$(basename "$$PWD" | tr '[:upper:] ' '[:lower:]_'); \
	mkdir -p "$$dir"; \
	echo "→ $$dir"; \
	$(COMPOSE) exec -T db pg_dump -U $${POSTGRES_USER:-userbots} $${POSTGRES_DB:-userbots} | gzip > "$$dir/db.sql.gz"; \
	docker run --rm -v "$${project}_media":/from -v "$$PWD/$$dir":/to alpine \
	  sh -c 'tar czf /to/media.tar.gz -C /from .' ; \
	cp .env "$$dir/.env.backup"; \
	chmod 600 "$$dir/.env.backup"; \
	echo "Backup complete: $$dir"; \
	$(COMPOSE) exec -T -w /app worker python manage.py backup_to_telegram "/app/$$dir" || \
	  echo "(Telegram upload skipped — set BACKUP_BOT_TOKEN/BACKUP_CHAT_ID to enable)"

backup-telegram: ## Re-send a specific backup to Telegram — set TS=YYYYMMDD_HHMMSS
	@if [ -z "$$TS" ]; then echo "Usage: make backup-telegram TS=YYYYMMDD_HHMMSS"; exit 1; fi
	$(COMPOSE) exec -T -w /app worker python manage.py backup_to_telegram "/app/backups/$$TS"

restore: ## Restore from backups/<timestamp>/ — set TS=YYYYMMDD_HHMMSS
	@if [ -z "$$TS" ]; then echo "Usage: make restore TS=YYYYMMDD_HHMMSS"; exit 1; fi
	@dir="backups/$$TS"; \
	if [ ! -d "$$dir" ]; then echo "Not found: $$dir"; exit 1; fi; \
	echo "Restoring from $$dir …"; \
	gunzip -c "$$dir/db.sql.gz" | $(COMPOSE) exec -T db psql -U $${POSTGRES_USER:-userbots} $${POSTGRES_DB:-userbots}

# ─── Local Docker dev ────────────────────────────────────────────────────────

dev: ## Start dev stack (sqlite, no caddy, port 8000)
	$(COMPOSE) -f docker-compose.dev.yml up

dev-down: ## Stop dev stack
	$(COMPOSE) -f docker-compose.dev.yml down

# ─── Local native (Windows venv) ─────────────────────────────────────────────

native-runserver: ## Run Django dev server (native, port 8000)
	$(PYTHON) manage.py runserver

native-worker: ## Run worker (native, separate terminal)
	$(PYTHON) manage.py run_worker

native-migrate: ## Apply migrations natively
	$(PYTHON) manage.py migrate

native-superuser: ## Create admin user natively
	$(PYTHON) manage.py createsuperuser

# ─── Secrets ─────────────────────────────────────────────────────────────────

secrets: ## Print fresh SECRET_KEY + DB_ENCRYPTION_KEY for .env
	@$(PYTHON) -c "from django.core.management.utils import get_random_secret_key; print('SECRET_KEY=\047' + get_random_secret_key() + '\047')"
	@$(PYTHON) -c "from cryptography.fernet import Fernet; print('DB_ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
	@$(PYTHON) -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32))"
