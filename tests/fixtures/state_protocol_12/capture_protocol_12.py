"""Capture the protocol-12 state files in this directory from the pre-change controller.

ADR 0009's S3 exit tests (#388) load protocol-12 REMOTE files *captured from
`main`* in every phase, so that the migration to protocol 13 is held to what
the controller before it really wrote, not to what this tree's serializer
writes under an old label. This script makes those captures. It is run by
hand, once, never by the suite:

    git archive 7df991782f8d8b918b3d8119baa342d617f783b9 | tar -x -C /tmp/main-p12
    uv run python tests/fixtures/state_protocol_12/capture_protocol_12.py \\
        /tmp/main-p12 tests/fixtures/state_protocol_12

It runs the scripted tests :data:`FIXTURES` names *from that tree*, with that
tree's own ``autoforge`` and ``tests/conftest.py`` (its fakes, scripted
providers and bare origins: no real agent, no GitHub, no network), loads
itself into that run as a pytest plugin, and records every ``state.json``
the controller's engine or CLI saves there (``SafeRoot.write_bytes`` called
from ``autoforge/engine.py`` or ``autoforge/cli.py``). For each fixture it
then copies one of those saves, byte for byte: of the saves its test made in
its phase that hold every field it requires, the one with the most non-empty
fields, the latest on a tie. ``captures.json`` records the source commit and,
per file, the test, the phase, the requirement and the SHA-256.

The files are never written or edited by hand. To change them, change this
script and capture again from the same commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_COMMIT = "7df991782f8d8b918b3d8119baa342d617f783b9"
PROTOCOL = "12"
MANIFEST = "captures.json"

# What a run of the source tree writes into this directory's files: paths of
# the tests' temporary trees (the recorded configuration file, the worktree
# root, block reasons) and the interpreter its validation commands name
# (``sys.executable``). Fixed, neutral paths keep them free of the capturing
# host's home and user name: the tests run under BASETEMP, and the
# interpreter is reached through PYTHON_LINK, a link to its prefix.
BASETEMP = "/tmp/autoforge-protocol-12-capture"
PYTHON_LINK = "/tmp/autoforge-protocol-12-python"

_CLI = "tests/test_cli.py::test_resume_with_gate_open_merges_via_controller_to_done"
_GATED = (
    "tests/test_merge_certificate.py::"
    "test_a_certificate_and_every_live_gate_continue_to_the_existing_merge"
)
_NOTES = "tests/test_integration.py::test_operator_notes_on_related_issues_end_to_end"
_JOURNALS = "tests/test_replan.py::test_r5f1_every_journal_the_controller_writes_loads_complete"
_REJECTED = (
    "tests/test_replan.py::test_a_replacement_that_fails_validation_is_rejected_before_any_effect"
)

# (file, test node id in the source tree, phase, what the save must hold).
# A requirement is a top-level field that must be non-empty, or
# ``path=value`` for a dotted path that must hold that value.
FIXTURES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    # One run through the CLI: `run`, then `resume --allow-merge` to DONE.
    ("initializing.json", _CLI, "INITIALIZING", ("config_fingerprint", "sandbox_pin")),
    (
        "analyze_execute.json",
        _CLI,
        "ANALYZE_EXECUTE",
        ("effect_records", "entry_observation", "completion_context", "launch_label"),
    ),
    ("review.json", _CLI, "REVIEW", ("review_evidence", "effect_records", "completion_context")),
    ("ready_for_merge.json", _CLI, "READY_FOR_MERGE", ("review_certificate", "review_history")),
    ("merge.json", _CLI, "MERGE", ("review_certificate",)),
    (
        "update_epic.json",
        _CLI,
        "UPDATE_EPIC",
        ("counted_merged_prs", "effect_records", "entry_observation", "completion_context"),
    ),
    ("done.json", _CLI, "DONE", ("counted_merged_prs", "review_certificate")),
    # A run whose candidates pass the configured validation to the merge.
    ("review-validated.json", _GATED, "REVIEW", ("validation_records", "review_evidence")),
    (
        "ready_for_merge-validated.json",
        _GATED,
        "READY_FOR_MERGE",
        ("validation_records", "review_certificate"),
    ),
    ("merge-validated.json", _GATED, "MERGE", ("validation_records", "review_certificate")),
    ("update_epic-validated.json", _GATED, "UPDATE_EPIC", ("validation_records",)),
    # ANALYZE_EXECUTE: a producer's failed validation, an operator's override.
    (
        "analyze_execute-validation-failed.json",
        "tests/test_validation_gates.py::"
        "test_a_failing_implementation_is_not_published_and_its_relaunch_sees_the_failure",
        "ANALYZE_EXECUTE",
        ("producer_validation_failure", "validation_records"),
    ),
    (
        "analyze_execute-unblocked.json",
        "tests/test_validation_gates.py::"
        "test_unblock_sets_the_watermark_and_the_same_candidate_is_validated_again",
        "ANALYZE_EXECUTE",
        ("validation_override_seq", "unblock_history"),
    ),
    # REVIEW after a fix with operator notes, after a replan, after a correction.
    (
        "review-operator-notes.json",
        _NOTES,
        "REVIEW",
        ("operator_comment_outcomes", "last_fix_resolutions", "review_history", "effect_records"),
    ),
    (
        "review-after-replan.json",
        "tests/test_replan.py::"
        "test_a_crash_after_the_plan_is_saved_publishes_on_resume_without_the_agent",
        "REVIEW",
        ("superseded_prs", "escalation_count"),
    ),
    (
        "review-prior-findings.json",
        "tests/test_engine.py::"
        "test_correction_after_the_fix_was_pushed_goes_to_review_without_relaunching",
        "REVIEW",
        ("prior_findings",),
    ),
    # FIX: publishing operator notes, a declined acceptance action, a failed
    # validation of the fix, a PR that closed while the fixer ran.
    (
        "fix.json",
        _NOTES,
        "FIX",
        ("open_findings", "entry_observation", "effect_records", "review_evidence"),
    ),
    (
        "fix-acceptance-declined.json",
        "tests/test_engine.py::"
        "test_fix_acceptance_action_declined_reruns_the_fixer_with_the_decline",
        "FIX",
        ("acceptance_outcomes", "open_findings"),
    ),
    (
        "fix-validation-failed.json",
        "tests/test_validation_gates.py::test_a_failing_fix_is_not_pushed_and_starts_no_review",
        "FIX",
        ("producer_validation_failure", "validation_records", "open_findings"),
    ),
    (
        "fix-verification-failed.json",
        "tests/test_engine.py::test_fix_pr_closed_while_the_fixer_ran_is_refused",
        "FIX",
        ("verification_failures", "open_findings"),
    ),
    # REPLAN_REEXECUTE: each stage of the journal the controller writes.
    *(
        (
            f"replan_reexecute-{stage}.json",
            _JOURNALS,
            "REPLAN_REEXECUTE",
            (f"replan_transaction.stage={stage}", "open_findings"),
        )
        for stage in ("pending", "prepared", "verified", "supersede_intent", "superseded")
    ),
    (
        "replan_reexecute-compensating.json",
        "tests/test_replan.py::test_a_base_rewritten_inside_the_close_window_is_undone[close]",
        "REPLAN_REEXECUTE",
        ("replan_transaction.stage=compensating",),
    ),
    (
        "replan_reexecute-validating.json",
        _REJECTED,
        "REPLAN_REEXECUTE",
        ("replan_transaction.stage=prepared", "validation_attempt", "validation_records"),
    ),
    (
        "replan_reexecute-rejected.json",
        _REJECTED,
        "REPLAN_REEXECUTE",
        ("replan_transaction.stage=rejected", "validation_records"),
    ),
    # READY_FOR_MERGE after an approved acceptance comment; MERGE after the
    # pre-merge commands; UPDATE_EPIC after a refused next issue.
    (
        "ready_for_merge-acceptance.json",
        "tests/test_engine.py::"
        "test_fix_acceptance_action_approved_is_published_then_the_fix_pushed",
        "READY_FOR_MERGE",
        ("acceptance_outcomes", "unblock_history", "review_certificate"),
    ),
    (
        "merge-premerge-verified.json",
        "tests/test_engine.py::test_validation_and_premerge_commands_run_under_the_allowlist",
        "MERGE",
        ("premerge_verified_head_sha", "premerge_verified_commands"),
    ),
    (
        "update_epic-next-issue-refused.json",
        "tests/test_engine.py::"
        "test_update_epic_null_refused_then_a_remaining_sub_issue_selected_switches",
        "UPDATE_EPIC",
        ("next_issue_rejections", "effect_records"),
    ),
    # BLOCKED and FAILED, each from more than one phase.
    (
        "blocked-review-round-cap.json",
        "tests/test_engine.py::"
        "test_review_round_cap_blocks_with_findings_and_never_starts_the_last_fix",
        "BLOCKED",
        ("block_reason", "open_findings", "review_history"),
    ),
    (
        "blocked-replan-rejected.json",
        "tests/test_replan.py::"
        "test_a_candidate_that_does_not_descend_from_the_base_is_rejected_before_any_effect",
        "BLOCKED",
        ("block_reason", "replan_transaction.stage=rejected"),
    ),
    (
        "blocked-update-epic.json",
        "tests/test_engine.py::"
        "test_update_epic_rejection_is_retried_once_with_the_reason_then_blocked",
        "BLOCKED",
        ("block_reason", "next_issue_rejections"),
    ),
    (
        "failed.json",
        "tests/test_engine.py::test_agent_failure_moves_to_failed",
        "FAILED",
        ("block_reason",),
    ),
    (
        "failed-replan.json",
        "tests/test_replan.py::"
        "test_t2_a_failed_replan_leaves_the_review_accounting_untouched[agent_failure]",
        "FAILED",
        ("block_reason", "replan_transaction", "open_findings"),
    ),
)

_RAW_ENV = "AUTOFORGE_PROTOCOL_12_CAPTURE_RAW"
_TREE_ENV = "AUTOFORGE_PROTOCOL_12_CAPTURE_TREE"


def is_empty(value: object) -> bool:
    """Whether ``value`` is empty: ``""``, ``0``, ``false``, ``null``, ``[]`` or ``{}``."""
    return value is None or value is False or value in ("", 0, [], {})


def holds(data: dict, requirement: str) -> bool:
    """Whether ``data`` meets one requirement of :data:`FIXTURES`."""
    if "=" not in requirement:
        return requirement in data and not is_empty(data[requirement])
    path, expected = requirement.split("=", 1)
    value: object = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return value == expected


# -- the plugin half: loaded into the source tree's pytest run ------------------------------

_current = {"nodeid": ""}


def _saved_by_the_controller() -> bool:
    frame = sys._getframe(2)
    while frame is not None:
        name = frame.f_code.co_filename
        if name.endswith(os.path.join("autoforge", "engine.py")) or name.endswith(
            os.path.join("autoforge", "cli.py")
        ):
            return True
        frame = frame.f_back
    return False


def pytest_configure(config) -> None:  # pragma: no cover - runs in the source tree
    import autoforge
    from autoforge import safefs

    tree = Path(os.environ[_TREE_ENV]).resolve()
    if autoforge.__protocol_version__ != PROTOCOL or tree not in Path(autoforge.__file__).parents:
        raise RuntimeError(f"not the source tree's protocol-{PROTOCOL} controller: {autoforge}")
    raw = Path(os.environ[_RAW_ENV])
    original = safefs.SafeRoot.write_bytes
    count = [0]

    def write_bytes(self, relpath, data, *, mode=0o600):
        original(self, relpath, data, mode=mode)
        if os.path.basename(str(relpath)) != "state.json" or not _saved_by_the_controller():
            return
        count[0] += 1
        name = f"{count[0]:06d}.json"
        (raw / name).write_bytes(data)
        with open(raw / "index.jsonl", "a", encoding="utf-8") as index:
            index.write(json.dumps({"file": name, "test": _current["nodeid"]}) + "\n")

    safefs.SafeRoot.write_bytes = write_bytes


def pytest_runtest_setup(item) -> None:  # pragma: no cover - runs in the source tree
    _current["nodeid"] = item.nodeid


# -- the driver half -------------------------------------------------------------------------


def _select(raw: Path) -> dict[str, tuple[str, bytes]]:
    saves: list[tuple[int, str, bytes, dict]] = []
    for seq, line in enumerate((raw / "index.jsonl").read_text(encoding="utf-8").splitlines()):
        entry = json.loads(line)
        data = (raw / entry["file"]).read_bytes()
        saves.append((seq, entry["test"], data, json.loads(data)))
    chosen: dict[str, tuple[str, bytes]] = {}
    for name, test, phase, requires in FIXTURES:
        matching = [
            (sum(not is_empty(v) for v in state.values()), seq, data)
            for seq, saved_by, data, state in saves
            if saved_by == test
            and state.get("phase") == phase
            and state.get("protocol_version") == PROTOCOL
            and state.get("mode") == "REMOTE"
            and all(holds(state, r) for r in requires)
        ]
        if not matching:
            raise SystemExit(f"{name}: {test} saved no {phase} state holding {requires}")
        chosen[name] = (test, max(matching)[2])
    return chosen


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    tree, out = Path(argv[0]).resolve(), Path(argv[1]).resolve()
    for path in (BASETEMP, PYTHON_LINK):
        if os.path.lexists(path):
            # pytest empties its --basetemp, and the link is made here: take neither.
            print(f"{path} exists; remove it first", file=sys.stderr)
            return 2
    here = Path(__file__).resolve().parent
    python = os.path.join(PYTHON_LINK, "bin", os.path.basename(sys.executable))
    tests = sorted({test for _, test, _, _ in FIXTURES})
    argv_pytest = [python, "-m", "pytest", "-p", Path(__file__).stem]
    argv_pytest += ["-p", "no:cacheprovider", "-q", "--basetemp", BASETEMP, *tests]
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp)
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": os.pathsep.join([str(tree / "src"), str(here)]),
                "PYTHONDONTWRITEBYTECODE": "1",
                _RAW_ENV: str(raw),
                _TREE_ENV: str(tree),
            }
        )
        os.symlink(sys.prefix, PYTHON_LINK)
        try:
            subprocess.run(argv_pytest, cwd=tree, env=env, check=True)
        finally:
            shutil.rmtree(BASETEMP, ignore_errors=True)
            os.unlink(PYTHON_LINK)
        chosen = _select(raw)
    entries = []
    for name, _test, phase, requires in FIXTURES:
        saved_by, data = chosen[name]
        (out / name).write_bytes(data)
        entries.append(
            {
                "file": name,
                "test": saved_by,
                "phase": phase,
                "requires": list(requires),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {"source_commit": SOURCE_COMMIT, "protocol_version": PROTOCOL, "files": entries}
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(entries)} captures and {MANIFEST} to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
