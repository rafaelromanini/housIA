# Atalhos do HousIA (MVP). Requer Docker para `up`/`seed`; uv e npm para o desenvolvimento local.
.PHONY: up down seed logs test api-dev web-dev e2e lint

up:            ## sobe web, api, db, fhir e carrega os cenários
	docker compose up --build -d

down:
	docker compose down

seed:          ## recarrega os cenários no HAPI reancorando os horários no "agora" (rode antes da demo)
	docker compose run --rm seed

logs:
	docker compose logs -f api web

test:          ## testes do backend (LLM mock, FHIR em memória)
	cd apps/api && .venv/bin/pytest

lint:
	cd apps/api && .venv/bin/ruff check app tests
	cd apps/web && npm run lint && npm run typecheck

api-dev:       ## API local sem Docker (FHIR em memória, SQLite, LLM mock)
	cd apps/api && uv venv -q && uv pip install -q -e ".[dev]" && .venv/bin/uvicorn app.main:app --reload --port 8000

web-dev:
	cd apps/web && npm install && npm run dev

e2e:           ## Playwright contra a stack rodando (localhost:3000 / :8000)
	cd apps/web && npx playwright test
