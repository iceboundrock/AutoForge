.PHONY: test test-parallel test-fast test-sandbox test-sandbox-measured lint typecheck fmt fmt-check lock-check check check-matrix dist-check sync lock

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

# `test` on several workers (pytest-xdist, #340): the same tests, options and
# outcomes, which tests/parallel_bench.py reconciled test by test against the
# serial run (docs/development.md, "The suite on several workers"). Opt-in,
# like check-matrix: `check` and CI still run `test`.
PYTEST_WORKERS ?= 4
test-parallel:
	uv run pytest -n $(PYTEST_WORKERS) --dist=load

# Feedback while working, never a gate (ADR 0010): the test modules that the
# paths changed since the merge base with FAST_BASE map to
# (tests/fast_targets.py), or the full suite as soon as one of them maps to
# it. It says what it selected and why. A FAST pass is no evidence: `check`,
# `check-matrix` and CI never run this, and tests/test_fast_targets.py keeps
# it that way. FAST_PYTEST_ARGS reach pytest, e.g. FAST_PYTEST_ARGS="-n 4 -x".
FAST_BASE ?= main
FAST_PYTEST_ARGS ?=
test-fast:
	uv run python -m tests.fast_targets --base $(FAST_BASE) --run -- $(FAST_PYTEST_ARGS)

# The tests that launch the real outer-sandbox backend (bubblewrap), on a host
# that must be able to enforce it: each skips with the probe's reason
# elsewhere, and here fails instead (AUTOFORGE_REQUIRE_SANDBOX_BACKEND=1).
# tests/test_ci_workflow.py checks that every module that launches the real
# backend is listed. Not part of `check`: whether a host can enforce is the
# host's (docs/development.md, "The outer sandbox").
SANDBOX_TESTS = tests/test_sandbox_bwrap.py tests/test_sandbox_routing.py \
	tests/test_sandbox_parity.py
test-sandbox:
	AUTOFORGE_REQUIRE_SANDBOX_BACKEND=1 uv run pytest -rs $(SANDBOX_TESTS)

# What the hosted CI's `sandbox` job runs, after installing bubblewrap: record
# what this host provides (tests/sandbox_host.py; in CI, into the job summary),
# then the same tests, failing instead of skipping exactly when that
# measurement says the host can enforce (AUTOFORGE_REQUIRE_SANDBOX_BACKEND=auto).
test-sandbox-measured:
	uv run python -m tests.sandbox_host
	AUTOFORGE_REQUIRE_SANDBOX_BACKEND=auto uv run pytest -rs $(SANDBOX_TESTS)

# Build the sdist and the wheel, install the wheel non-editably into a fresh
# environment outside the source tree, and run the installed command and every
# prompt template from it (tests/dist_check.py, #256). What the hosted CI's
# `dist` job runs on each interpreter of CI_PYTHON_VERSIONS (DIST_PYTHON set
# per matrix entry); locally it defaults to a tested interpreter, never to
# .python-version's. `--no-project` runs the check on that bare interpreter,
# so it never imports the source tree. Not part of `check`: it builds and
# installs, and fetches the build backend from the package index.
DIST_PYTHON ?= 3.12
dist-check:
	uv run --no-project --python $(DIST_PYTHON) python -I tests/dist_check.py

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
