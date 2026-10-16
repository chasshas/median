COMPOSE ?= docker compose
RUN := $(COMPOSE) run --rm --no-deps web

.PHONY: up down logs migrate test lint format shell psql build

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
