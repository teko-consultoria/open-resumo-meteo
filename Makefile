.PHONY: install test lint check doctor build run-today

install:
	uv sync --python 3.12 --extra dev --extra postgres

test:
	uv run --extra dev pytest

lint:
	uv run --extra dev ruff check src tests scripts

check: lint test

doctor:
	uv run resumo-meteo doctor

build:
	uv build

run-today:
	uv run resumo-meteo run --date $$(date +%F) --cycle 00
