.PHONY: test lint typecheck fmt sync lock

sync:
	uv sync

lock:
	uv lock

test:
	uv run pytest

lint:
	uv run ruff check src tests

typecheck:
	uv run mypy src

fmt:
	uv run ruff format src tests
