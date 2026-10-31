"""LOCAL mode's scripted-run helpers, shared by the modules that drive a LOCAL run.

Moved out of ``tests/test_local.py`` (ADR 0009 D12 S1a, #386) so that the
modules importing them keep collecting when that module goes. Like
``make_local_engine`` in ``tests/conftest.py``, they build LOCAL runs and go
with LOCAL's code (#388).
"""

from __future__ import annotations

from pathlib import Path

from .conftest import block, commit_all, git_repo, write_feature

IMPL_FILE = "src/app.py"


def local_repo(tmp_path) -> Path:
    """A git repository with one committed file and a feature specification."""
    root = git_repo(tmp_path)
    (root / "src").mkdir(exist_ok=True)
    (root / IMPL_FILE).write_text("def main():\n    pass\n", encoding="utf-8")
    write_feature(root)
    commit_all(root, "initial")
    return root


def impl_result(changed: bool = True, summary: str = "Implemented the filter.") -> str:
    return block(
        {
            "phase": "ANALYZE_EXECUTE",
            "status": "success",
            "summary": summary,
            "changed_workspace": changed,
            "tests_attempted": ["pytest -q"],
        }
    )


def review_result(fingerprint: str, round: int = 1, findings: list[dict] | None = None) -> str:
    findings = findings or []
    return block(
        {
            "phase": "REVIEW",
            "status": "success",
            "round": round,
            "reviewed_workspace_fingerprint": fingerprint,
            "needs_fix_round": bool(findings),
            "findings": findings,
            "observations": ["Reads cleanly."],
        }
    )


def finding(round: int = 1, n: int = 1) -> dict:
    return {
        "id": f"R{round}-F{n}",
        "classification": "non-blocked",
        "title": "Missing test",
        "location": IMPL_FILE,
        "required_resolution": "Add a unit test for the date filter.",
    }


def fix_result(finding_ids: list[str], changed: bool = True, resolution: str = "fixed") -> str:
    return block(
        {
            "phase": "FIX",
            "status": "success",
            "changed_workspace": changed,
            "resolutions": [
                {
                    "finding_id": fid,
                    "resolution": resolution,
                    "rationale": "The behaviour is already covered elsewhere in the suite.",
                }
                for fid in finding_ids
            ],
            "blocked_reason": "",
        }
    )


def touch_impl(root: Path, text: str) -> None:
    (root / IMPL_FILE).write_text(text, encoding="utf-8")


def scripted(engine, root: Path, steps: list):
    """Handler that mutates the workspace like a real agent would.

    ``steps`` are ``(mutate(root) | None, stdout_factory(engine))`` pairs
    consumed in order.
    """
    queue = list(steps)

    def handler(req):
        mutate, stdout = queue.pop(0)
        if mutate is not None:
            mutate(root)
        return stdout(engine)

    return handler
