COMPOSE ?= docker compose
RUN := $(COMPOSE) run --rm --no-deps web

.PHONY: up down logs migrate test lint format shell psql build backup restore backup-files

## Start db, apply migrations, then start web and worker.
up:
	$(COMPOSE) up -d --build --wait db
	$(MAKE) migrate
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

build:
	$(COMPOSE) build

## Alembic upgrade head + Procrastinate schema (idempotent).
migrate:
	$(COMPOSE) run --rm web python -m app.db.migrate

## pytest in the web container against TEST_DATABASE_URL.
test:
	$(COMPOSE) run --rm web pytest $(ARGS)

lint:
	$(RUN) ruff format --check .
	$(RUN) ruff check .

format:
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

shell:
	$(COMPOSE) exec web bash

psql:
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

## Dump the database now (also runs daily at 04:00 APP_TIMEZONE and from /settings).
backup:
	$(COMPOSE) exec -T web python -m app.cli backup

## Replace the app database with a dump. FILE is a file on this machine, or the name of a dump
## in DATA_DIR/backups (e.g. db-20261018T040000Z.dump). Asks first unless CONFIRM=yes.
restore:
	@test -n "$(FILE)" || { echo "usage: make restore FILE=<dump> [CONFIRM=yes]"; exit 2; }
	@if [ "$(CONFIRM)" != yes ]; then \
		printf 'This replaces everything in the app database with %s. Type yes to continue: ' "$(FILE)"; \
		read answer; [ "$$answer" = yes ] || { echo "cancelled"; exit 1; }; \
	fi
	$(COMPOSE) up -d --wait db
	$(COMPOSE) stop web worker
	@if [ -f "$(FILE)" ]; then \
		$(COMPOSE) run --rm -T --no-deps web python -m app.cli restore --yes - < "$(FILE)"; \
	else \
		$(COMPOSE) run --rm -T --no-deps web python -m app.cli restore --yes "$(FILE)"; \
	fi
	$(COMPOSE) up -d web worker

## Copy the media (archive/ and thumbs/) to DEST on this machine with rsync. The database
## backup does not contain media; back them up this way, separately.
backup-files:
	@test -n "$(DEST)" || { echo "usage: make backup-files DEST=<directory>"; exit 2; }
	mkdir -p "$(DEST)"
	$(COMPOSE) run --rm --no-deps -T --user "$$(id -u):$$(id -g)" \
		-v "$(abspath $(DEST))":/backup-dest web \
		sh -c 'rsync -a --info=stats1 "$$DATA_DIR/archive" "$$DATA_DIR/thumbs" /backup-dest/'
