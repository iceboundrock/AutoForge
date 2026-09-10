.PHONY: test lint typecheck fmt fmt-check check sync lock

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

fmt-check:
	uv run ruff format --check src tests

# Everything the hosted CI workflow runs.
check: test lint fmt-check typecheck
