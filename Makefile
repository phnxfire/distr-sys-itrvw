.PHONY: install test lint run-gateway run-account docker-up docker-down

PYTHON ?= .venv/bin/python

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check src tests

run-gateway:
	$(PYTHON) -m uvicorn gateway_service.main:app --reload --port 8000

run-account:
	$(PYTHON) -m uvicorn account_service.main:app --reload --port 8001

docker-up:
	docker compose up --build

docker-down:
	docker compose down
