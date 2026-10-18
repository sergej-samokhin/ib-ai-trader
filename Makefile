.PHONY: install test lint format schema demo

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

schema:
	ibtrader schema --out schemas/signal-contract-1.0.schema.json

demo:
	ibtrader run --demo
