.PHONY: install dev down test lint typecheck migrate fmt clean

install:
	pip install -e ".[dev]"
	playwright install chromium

dev:
	docker compose up -d

down:
	docker compose down

test:
	pytest

lint:
	ruff check postcheck tests

fmt:
	ruff format postcheck tests
	ruff check --fix postcheck tests

typecheck:
	mypy postcheck

migrate:
	alembic upgrade head

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
