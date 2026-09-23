.PHONY: test lint typecheck fmt fmt-check lock-check check check-matrix sync lock

# The interpreters the hosted CI matrix runs `pytest` on (`python-version:`
# in .github/workflows/ci.yml). tests/test_ci_workflow.py fails when the two
# lists differ, so the matrix is maintained here and checked there.
CI_PYTHON_VERSIONS = 3.11 3.12
# The matrix targets are phony like every other target here: a stray file
# named test-py3.11 must not let make treat that interpreter's run as up to
# date and skip it. .PHONY takes no pattern, and a phony target is never
# matched against a pattern rule, so the targets are listed expanded and
# their recipe below is a static pattern rule (an explicit rule per target).
MATRIX_TARGETS = $(addprefix test-py,$(CI_PYTHON_VERSIONS))
.PHONY: $(MATRIX_TARGETS)

sync:
	uv sync

lock:
	uv lock

# What `uv sync --locked` enforces in CI: uv.lock still matches pyproject.toml.
lock-check:
	uv lock --check

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

# The same commands as the hosted CI workflow, on the one interpreter the
# local environment resolves (.python-version). CI also runs `test` on every
# interpreter in CI_PYTHON_VERSIONS; `check-matrix` does that, opt-in.
check: lock-check test lint fmt-check typecheck

# `pytest` on every CI interpreter, from a locked install like CI's.
# --isolated gives each run its own cached environment: without it
# `uv run --python X` replaces the project's .venv with an X environment.
check-matrix: $(MATRIX_TARGETS)

$(MATRIX_TARGETS): test-py%:
	uv run --isolated --locked --python $* pytest
