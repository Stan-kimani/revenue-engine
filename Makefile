.PHONY: setup dev migrate worker test golden check

setup:
	uv sync

dev:
	docker compose up

migrate:
	uv run python scripts/migrate.py

worker: setup
	@if [ ! -f scripts/run_worker.py ]; then \
		echo "scripts/run_worker.py is created in M0.3 — nothing to run yet."; \
		exit 0; \
	fi
	uv run python scripts/run_worker.py

test: setup
	uv run pytest tests/unit tests/contracts tests/integration; code=$$?; \
	if [ $$code -eq 5 ]; then echo "No tests collected yet."; exit 0; fi; \
	exit $$code

golden:
	uv run pytest tests/golden -m golden; code=$$?; \
	if [ $$code -eq 5 ]; then echo "No golden tests collected yet."; exit 0; fi; \
	exit $$code

check: setup
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy --strict src/revenue_engine/core src/revenue_engine/db
