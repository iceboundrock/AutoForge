"""Operator actions (K10, #243): comments on other issues the operator posts.

The controller never posts them. It derives the allowed targets once, at
FIX entry, from the run's issue body and the EPIC; checks a FIX result's
``operator_acceptance`` against that set; saves the plan with the K10
records first; verifies each one from the target's complete comment
listing in FIX's prefix pass; and blocks while any is not verified, sending
nothing after the prefix. ``unblock`` re-enters FIX only on progress, binds
existing comments as evidence (digest-pinned, adequacy never judged) and
declines or withdraws actions. Nothing here is an assertion: a reason, a
checkbox or an unbound comment never fulfils an action.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from autoforge.claims import OPERATOR_COMMENT, render_operator_comment_marker, scan
from autoforge.config import default_config
from autoforge.effects import EffectRecord, Stage, operator_comment_body, sha256_text
from autoforge.errors import (
    ConfigurationError,
    GitHubError,
    GitHubUnavailableError,
    GitHubWrongTargetError,
)
from autoforge.state import MAX_DECLINES_PER_ROUND, load_state
from autoforge.transitions import Phase
from tests.conftest import (
    BRANCH,
    EPIC,
    ISSUE,
    NO_CHANGE_RATIONALE,
    PR,
    FakeGitHub,
    block,
    comment_url,
    fixer,
    make_engine,
    publish_pr_head,
    read_context_of,
    review_result,
    scripted,
)

REPO = "https://github.com/owner/repo"
T16 = f"{REPO}/issues/16"
T158 = f"{REPO}/issues/158"
ISSUE_BODY = (
    "Ship the feature.\n\nAcceptance: coordination notes on #16 and "
    f"{T158} (see PR #42 and https://github.com/other/repo/issues/7)."
)
NOTE16 = f"Coordination note for #16: the scheduler contract is in {REPO}/blob/main/docs/adr.md."
NOTE158 = "Coordination note for #158: the worker settles at its single-issue boundary."
RATIONALE = (
    "The issue's acceptance criteria require coordination notes on #16 and #158; the "
    "fixer cannot post them, so the operator does."
)
REASON = "operator posted the coordination notes"
FINDING = {
    "id": "R1-F1",
    "classification": "bug",
    "title": "notes missing",
    "location": "docs/adr.md:1",
    "required_resolution": "publish the coordination notes",
}


def operator(fid: str, *actions: tuple[int, str], rationale: str = RATIONALE) -> dict:
    """An ``operator_acceptance`` resolution asking for ``actions`` (issue, text)."""
    return {
        "finding_id": fid,
        "resolution": "operator_acceptance",
        "rationale": rationale,
        "operator_acceptance": {
            "actions": [
                {"kind": "related_issue_comment", "issue": issue, "body": text}
                for issue, text in actions
            ]
        },
    }


BOTH = operator("R1-F1", (16, NOTE16), (158, NOTE158))
NO_CHANGE = {
    "finding_id": "R1-F1",
    "resolution": "no_change_with_rationale",
    "rationale": NO_CHANGE_RATIONALE,
}


def payload(target: str, text: str, fid: str = "R1-F1", round_: int = 1) -> str:
    """The exact bytes the controller asks the operator to post on ``target``."""
    return operator_comment_body(text, ISSUE, PR, round_, fid, target)


def _operating(
    tmp_state_dir,
    gh: FakeGitHub,
    script,
    policy: str = "approve",
    round_: int = 1,
    findings=1,
    body: str = ISSUE_BODY,
):
    """A run in FIX (``round_``, ``findings`` open) whose issue references #16 and #158.

    #16 is closed and #158 open, both issues of the run's repository; the
    EPIC is #1. The PR's HEAD is a real commit on the origin's PR branch.
    ``body`` is the run's issue body.
    """
    gh.add_issue(ISSUE, "Ship the feature", body=body)
    gh.add_issue(T16, "Scheduler", state="CLOSED")
    gh.add_issue(T158, "Worker")
    gh.add_issue(EPIC, "EPIC")
    cfg = default_config()
    cfg.safety = replace(cfg.safety, acceptance_actions=policy)
    eng = make_engine(tmp_state_dir, script, github=gh, cfg=cfg, origin=True)
    head = publish_pr_head(eng)
    gh.add_pr(head_sha=head)
    eng.state.phase = Phase.FIX
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = head
    eng.state.reviewed_head_sha = head
    eng.state.review_round = round_
    eng.state.last_review_comment_url = comment_url(PR, 100)
    eng.state.open_findings = [dict(FINDING, id=f"R{round_}-F{n}") for n in range(1, findings + 1)]
    return eng


def _records(eng) -> list[EffectRecord]:
    return [EffectRecord.from_dict(r) for r in load_state(eng.paths.state_file).effect_records]


def _k10(eng, target: str) -> EffectRecord:
    return next(r for r in _records(eng) if r.target.get("issue_url") == target)


def _post(gh: FakeGitHub, target: str, cid: int, body: str):
    return gh.add_comment(target, cid, body)


def _reviewer(seen: list[str], eng_ref: list):
    def handler(req):
        seen.append((read_context_of(req.prompt) / "operator-evidence.md").read_text())
        return block(review_result(2, eng_ref[0].state.current_head_sha, []))

    return handler


# -- the plan and the prefix pass ------------------------------------------------------


@pytest.mark.parametrize("policy", ["approve", "auto"])
def test_operator_actions_block_with_the_plan_saved_and_nothing_sent(tmp_state_dir, policy):
    """The plan is saved K10 first (result order), the push last; the prefix
    pass reads both targets, finds nothing, and blocks before anything else
    is sent. Neither policy makes a single K10 write: the operator posts."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH), policy=policy)
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert gh.effect_writes == []
    assert eng.origin.head(BRANCH) == reviewed == gh.prs[PR].head_sha
    s = load_state(eng.paths.state_file)
    assert [r["kind"] for r in s.effect_records] == ["operator_comment", "operator_comment", "push"]
    targets = s.entry_observation["operator_targets"]
    assert targets["numbers"] == [16, 158, 1]
    assert targets["allowed_by"] == {"16": ["issue_body"], "158": ["issue_body"], "1": ["epic"]}
    assert targets["issue_body_sha256"] == sha256_text(ISSUE_BODY)
    r16, r158 = _k10(eng, T16), _k10(eng, T158)
    assert r16.body == payload(T16, NOTE16) and r158.body == payload(T158, NOTE158)
    assert r16.precondition["binding"] is None and r16.stage is Stage.INTENDED
    reason = s.block_reason
    for record in (r16, r158):
        assert record.action_id.startswith("X-") and record.action_id in reason
        assert f"status --action {record.action_id}" in reason
        assert f"--bind-evidence {record.action_id}" in reason
    assert "--decline-action" in reason and "is held back" in reason
    assert NOTE16 not in reason


def test_operator_actions_complete_from_exact_payloads_without_relaunching(tmp_state_dir):
    """Partial completion is durable: #16's exact payload is saved observed
    while #158 still blocks; once #158 is posted too, FIX completes from the
    journal (one fixer launch in all), pushes, and hands REVIEW both
    comments as exact evidence."""
    gh = FakeGitHub()
    seen: list[str] = []
    ref: list = []
    eng = _operating(tmp_state_dir, gh, scripted(fixer(BOTH), _reviewer(seen, ref)))
    ref.append(eng)
    assert eng.step().next_phase == "BLOCKED"
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    c16 = _post(gh, T16, 9016, payload(T16, NOTE16))
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "FIX", out.message
    assert f"{x16} is on {c16.url} (exact)" in out.message
    assert eng.step().next_phase == "BLOCKED"
    assert _k10(eng, T16).stage is Stage.OBSERVED
    assert _k10(eng, T158).stage is Stage.INTENDED
    reason = load_state(eng.paths.state_file).block_reason
    assert f"Verified so far: {x16}" in reason and x158 in reason
    c158 = _post(gh, T158, 9158, payload(T158, NOTE158))
    assert eng.unblock(REASON).unblocked
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert len(eng.provider.calls) == 1
    assert gh.effect_writes == []  # the push is a git write, never a GitHub one
    s = load_state(eng.paths.state_file)
    assert s.effect_records == [] and s.open_findings == []
    [resolution] = s.last_fix_resolutions
    assert resolution["resolution"] == "operator_acceptance"
    assert [
        (e["action_id"], e["comment_url"], e["evidence"]) for e in resolution["operator_evidence"]
    ] == [(x16, c16.url, "exact"), (x158, c158.url, "exact")]
    assert [(o["action_id"], o["outcome"]) for o in s.operator_comment_outcomes] == [
        (x16, "verified"),
        (x158, "verified"),
    ]
    assert eng.step().next_phase == "READY_FOR_MERGE"
    [evidence] = seen
    assert c16.url in evidence and c158.url in evidence
    assert "verified, exact" in evidence and "has the observed digest" in evidence


def test_a_plain_unblock_on_an_assertion_is_refused_byte_identical(tmp_state_dir):
    """A reason, a comment on the run's issue saying "done" and an unmarked
    comment on a target are never evidence: the unblock is refused, the
    state file is byte-identical and no fixer launches."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    _post(gh, ISSUE, 7001, f"done: {T16}#issuecomment-1 and {T158}#issuecomment-2")
    _post(gh, T16, 7002, NOTE16)  # the text, without the header and marker
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(f"{REASON}: {T16} and {T158}")
    assert not out.unblocked
    assert "no operator action made progress" in out.message
    assert "awaiting the operator" in out.message
    assert eng.paths.state_file.read_text() == raw
    assert len(eng.provider.calls) == 1  # the fixer that asked, and no other


def test_the_dry_run_of_an_unblock_writes_nothing(tmp_state_dir):
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    _post(gh, T16, 9016, payload(T16, NOTE16))
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, dry_run=True)
    assert out.message.startswith("[dry-run] would re-enter FIX"), out.message
    assert eng.paths.state_file.read_text() == raw


def test_a_payload_whose_line_endings_changed_is_a_conflict_until_repaired(tmp_state_dir):
    """The web UI or an editor changed the bytes: a conflict naming the cause,
    which a plain unblock reopens once the comment holds the exact bytes."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(operator("R1-F1", (16, "line one\nline two"))))
    exact = payload(T16, "line one\nline two")
    _post(gh, T16, 9016, exact.replace("\n", "\r\n"))  # posted before the plan was saved
    assert eng.step().next_phase == "BLOCKED"
    record = _k10(eng, T16)
    assert record.stage is Stage.CONFLICT
    assert "line endings or trailing whitespace" in record.reason
    raw = eng.paths.state_file.read_text()
    assert not eng.unblock(REASON).unblocked  # the conflict still reproduces
    assert eng.paths.state_file.read_text() == raw
    gh.comments[T16] = [replace(gh.comments[T16][0], body=exact)]
    out = eng.unblock(REASON)
    assert out.unblocked, out.message
    assert _k10(eng, T16).stage is Stage.INTENDED
    assert eng.step().next_phase == "REVIEW"


def test_a_payload_posted_twice_or_on_the_wrong_issue_is_a_conflict(tmp_state_dir):
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    _post(gh, T16, 9016, payload(T16, NOTE16))
    _post(gh, T16, 9017, payload(T158, NOTE158))  # #158's payload, misplaced on #16
    assert eng.step().next_phase == "BLOCKED"
    record = _k10(eng, T16)
    assert record.stage is Stage.CONFLICT and "misplaced payload" in record.reason


def _conflicted(tmp_state_dir, gh: FakeGitHub):
    """A run blocked on #16's payload posted with other line endings (a conflict)."""
    eng = _operating(tmp_state_dir, gh, fixer(operator("R1-F1", (16, NOTE16 + "\nmore"))))
    _post(gh, T16, 9016, payload(T16, NOTE16 + "\nmore").replace("\n", "\r\n"))
    assert eng.step().next_phase == "BLOCKED"
    assert _k10(eng, T16).stage is Stage.CONFLICT
    return eng


@pytest.mark.parametrize("case", ["listing", "target", "conflicted listing", "conflicted target"])
def test_an_unreadable_target_refuses_a_plain_unblock_byte_identical(tmp_state_dir, case):
    """PR #254 R1-F1: a conclusive failure to read a target or its comments
    refuses the unblock naming both, even beside another action's progress;
    a conflict on a target that was not read is never progress."""
    gh = FakeGitHub()
    if case.startswith("conflicted"):
        eng, target = _conflicted(tmp_state_dir, gh), T16
        gh.comments[T16] = [replace(gh.comments[T16][0], body=_k10(eng, T16).body)]  # repaired
    else:
        eng, target = _operating(tmp_state_dir, gh, fixer(BOTH)), T158
        assert eng.step().next_phase == "BLOCKED"
        _post(gh, T16, 9016, payload(T16, NOTE16))  # #16 made progress
    if case.endswith("listing"):
        gh.comments_error = GitHubError("HTTP 403: Resource not accessible")
        target = T16  # every listing fails, #16's (the first read) included
    else:
        gh.get_issue_errors[target] = GitHubError("HTTP 403: Resource not accessible")
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked, out.message
    assert _k10(eng, target).action_id in out.message
    assert f"{target} or its comments cannot be read" in out.message and "HTTP 403" in out.message
    assert eng.paths.state_file.read_text() == raw


def test_a_conflict_on_a_target_that_is_gone_is_not_progress(tmp_state_dir):
    gh = FakeGitHub()
    eng = _conflicted(tmp_state_dir, gh)
    del gh.issues[T16]
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "no operator action made progress" in out.message
    assert "is not found" in out.message and "recorded conflict is not re-checked" in out.message
    assert eng.paths.state_file.read_text() == raw


@pytest.mark.parametrize("where", ["listing", "target"])
def test_a_transient_read_during_a_plain_unblock_propagates(tmp_state_dir, where):
    gh = FakeGitHub()
    eng = _conflicted(tmp_state_dir, gh)
    if where == "listing":
        gh.comments_error = GitHubUnavailableError("connection reset")
    else:
        gh.get_issue_errors[T16] = GitHubUnavailableError("connection reset")
    raw = eng.paths.state_file.read_text()
    with pytest.raises(GitHubUnavailableError):
        eng.unblock(REASON)
    assert eng.paths.state_file.read_text() == raw


def test_unblock_refusals_name_the_runs_own_commands(tmp_state_dir):
    """PR #254 R1-F2: a scheduler namespace's run (or one started with
    ``--config`` / ``--state-dir``) is told commands it can run as typed."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    eng.cli_global_flags = ("--config", "af.yaml", "--state-dir", "ns dir")
    eng.continue_command = ("--config", "af.yaml", "epic", "resume", EPIC)
    flags = "autoforge --config af.yaml --state-dir 'ns dir'"
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, decline_action=["X-" + "0" * 16])
    assert not out.unblocked and f"Run {flags} status for the current ones" in out.message
    out = eng.unblock(REASON)
    assert not out.unblocked and "no operator action made progress" in out.message
    for command in (
        f"{flags} status --action {x16}",
        f"{flags} status --action {x158}",
        f"{flags} unblock --bind-evidence X-...",
        f"{flags} unblock --decline-action X-...",
    ):
        assert command in out.message, out.message
    assert "'autoforge status" not in out.message and "'autoforge " not in out.message
    assert eng.paths.state_file.read_text() == raw


# -- the result check ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "expect"),
    [
        (operator("R1-F1", (99, NOTE16)), "#99, which is not an allowed target"),
        (operator("R1-F1", (42, NOTE16)), "#42, which is not an allowed target"),
        (operator("R1-F1", (2, NOTE16)), "#2, which is not an allowed target"),
        (
            operator("R1-F1", (16, "See https://example.com/notes for the details.")),
            "that is not 'https://github.com/owner/repo/...'",
        ),
    ],
)
def test_a_request_outside_the_rules_is_corrected_before_anything_is_saved(
    tmp_state_dir, bad, expect
):
    """Only the entry's allowed set (the issue body's references and the
    EPIC, minus the run's own issue and its PR) may be targeted, and text
    links only into the run's repository; a refusal is a correction."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, scripted(fixer(bad), fixer(BOTH)))
    assert eng.step().next_phase == "BLOCKED"
    first, corrected = eng.provider.calls
    assert expect in corrected.prompt
    assert "#16 (referenced by the run's issue body)" in first.prompt
    assert "#1 (the run's EPIC)" in first.prompt
    assert len(_records(eng)) == 3


@pytest.mark.parametrize(
    ("error", "expect"),
    [
        (GitHubWrongTargetError("answered with a PR", f"{REPO}/pull/16"), "is a pull request"),
        (None, "is not found"),
    ],
)
def test_a_target_that_is_a_pr_or_missing_is_a_correction(tmp_state_dir, error, expect):
    gh = FakeGitHub()
    eng = _operating(
        tmp_state_dir, gh, scripted(fixer(BOTH), fixer(operator("R1-F1", (158, NOTE158))))
    )
    if error is None:
        del gh.issues[T16]
    else:
        gh.get_issue_errors[T16] = error
    assert eng.step().next_phase == "BLOCKED"
    _, corrected = eng.provider.calls
    assert expect in corrected.prompt
    assert [r.target["issue_url"] for r in _records(eng)[:-1]] == [T158]


def test_a_target_that_cannot_be_read_conclusively_blocks_with_nothing_saved(tmp_state_dir):
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    gh.get_issue_errors[T158] = GitHubError("HTTP 403: Resource not accessible")
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    s = load_state(eng.paths.state_file)
    assert "cannot be read" in s.block_reason and "no operator action was planned" in s.block_reason
    assert s.effect_records == [] and s.completion_context == {}
    assert gh.effect_writes == []


def test_a_transient_target_read_propagates(tmp_state_dir):
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    gh.get_issue_errors[T158] = GitHubUnavailableError("connection reset")
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert load_state(eng.paths.state_file).effect_records == []


def test_operator_actions_are_refused_while_the_policy_is_off(tmp_state_dir):
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, scripted(fixer(BOTH), fixer(NO_CHANGE)), policy="off")
    assert eng.step().next_phase == "REVIEW"
    first, corrected = eng.provider.calls
    assert "`operator_acceptance` resolution is not available" in first.prompt
    assert "safety.acceptance_actions: off" in first.prompt
    assert "operator_acceptance, which this run does not offer" in corrected.prompt
    assert load_state(eng.paths.state_file).operator_comment_outcomes == []


def test_the_allowed_set_is_the_entrys_even_when_the_issue_body_changes(tmp_state_dir):
    """The prompt's list and the check use the set the entry persisted: an
    issue-body edit while the fixer runs widens nothing."""
    gh = FakeGitHub()

    def edits_the_body_then_asks(req):
        gh.issues[ISSUE] = replace(gh.issues[ISSUE], body=f"{ISSUE_BODY}\nAlso #77.")
        gh.add_issue(f"{REPO}/issues/77", "Planted")
        return fixer(operator("R1-F1", (77, NOTE16)))(req)

    eng = _operating(tmp_state_dir, gh, scripted(edits_the_body_then_asks, fixer(BOTH)))
    assert eng.step().next_phase == "BLOCKED"
    _, corrected = eng.provider.calls
    assert "#77, which is not an allowed target" in corrected.prompt
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and "changed since FIX read it" in out.message
    assert eng.paths.state_file.read_text() == raw


# GitHub numbers issues from 1: an incidental `#0` names no issue (PR #254 R3-F1).
ZERO_REFERENCES = [
    pytest.param(" It ran on worker #0.", id="shorthand"),
    pytest.param(" Padded: #000.", id="padded-shorthand"),
    pytest.param(f" See {REPO}/issues/0.", id="url"),
    pytest.param(f" Worker #0, also {REPO}/issues/00.", id="both"),
]


def _every_save_loads(monkeypatch) -> list[dict]:
    """Load each state the engine saves, as the next command would; their entry observations."""
    import autoforge.engine as engine_module

    real = engine_module.save_state
    observations: list[dict] = []

    def checked(state, path, **kwargs):
        real(state, path, **kwargs)
        loaded = load_state(path)
        loaded.phase_effects()
        observations.append(loaded.entry_observation)

    monkeypatch.setattr(engine_module, "save_state", checked)
    return observations


@pytest.mark.parametrize("extra", ZERO_REFERENCES)
def test_a_zero_reference_never_stops_an_ordinary_fix(tmp_state_dir, monkeypatch, extra):
    """PR #254 R3-F1: a FIX that requests no operator action proceeds to
    REVIEW over an issue body naming `#0`; every state it saves loads again,
    and the allowed set it recorded holds issue numbers only."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(NO_CHANGE, commit=False), body=ISSUE_BODY + extra)
    observations = _every_save_loads(monkeypatch)
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    targets = [o["operator_targets"] for o in observations if "operator_targets" in o]
    assert targets and all(t["numbers"] == [16, 158, 1] for t in targets)
    assert targets[0]["issue_body_sha256"] == sha256_text(ISSUE_BODY + extra)
    load_state(eng.paths.state_file).phase_effects()


@pytest.mark.parametrize("extra", ZERO_REFERENCES)
def test_a_zero_reference_is_no_allowed_target(tmp_state_dir, monkeypatch, extra):
    """The fixer is offered the real targets only, and the plan it asks for
    blocks on them with a state every later command can load."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH), body=ISSUE_BODY + extra)
    _every_save_loads(monkeypatch)
    assert eng.step().next_phase == "BLOCKED"
    [launch] = eng.provider.calls
    assert "- #16 (referenced by the run's issue body)" in launch.prompt
    assert "- #0 " not in launch.prompt
    s = load_state(eng.paths.state_file)
    assert s.phase_effects().observation.operator_targets.numbers == (16, 158, 1)
    assert [r.target["issue_url"] for r in _records(eng)[:2]] == [T16, T158]


def test_an_entry_observation_the_loader_refuses_is_never_saved(tmp_state_dir, monkeypatch):
    """PR #254 R3-F1: the FIX entry observation is validated as a later load
    validates it before the pre-launch save. One that fails is a controller
    defect, raised with state.json unchanged and nothing launched."""
    from autoforge.effects import OperatorTargets
    from autoforge.engine import ControllerEngine
    from autoforge.errors import StateError

    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    eng._save()
    raw = eng.paths.state_file.read_text()
    before = eng.state.entry_observation
    bad = OperatorTargets((0,), {0: ("issue_body",)}, sha256_text(ISSUE_BODY))
    monkeypatch.setattr(ControllerEngine, "_operator_targets_at_entry", lambda self: bad)
    with pytest.raises(StateError, match=r"operator_targets\.numbers\[0\] must be >= 1, got 0"):
        eng.step()
    assert eng.paths.state_file.read_text() == raw
    assert eng.state.entry_observation == before
    assert eng.provider.calls == []


# -- binding existing comments ---------------------------------------------------------


def test_bound_comments_are_verified_as_operator_bound_evidence(tmp_state_dir):
    """Two pairs in one unblock: each existing comment is digest-pinned and
    verified as ``bound``; REVIEW is told the controller did not judge
    adequacy."""
    gh = FakeGitHub()
    seen: list[str] = []
    ref: list = []
    eng = _operating(tmp_state_dir, gh, scripted(fixer(BOTH), _reviewer(seen, ref)))
    ref.append(eng)
    assert eng.step().next_phase == "BLOCKED"
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    c16 = _post(gh, T16, 5016, "Notes from 2026-10-05: the contract is in ADR 0008.")
    c158 = _post(gh, T158, 5158, "Notes from 2026-10-05: workers settle per issue.")
    out = eng.unblock(REASON, bind_evidence=[(x16, c16.url), (x158, c158.url)])
    assert out.unblocked and out.phase == "FIX", out.message
    s = load_state(eng.paths.state_file)
    assert s.unblock_history[-1]["action"] == f"bound {x16} {c16.url}; bound {x158} {c158.url}"
    assert _k10(eng, T16).binding == {
        "comment_url": c16.url,
        "comment_id": 5016,
        "body_sha256": sha256_text(c16.body),
    }
    assert eng.step().next_phase == "REVIEW"
    [resolution] = load_state(eng.paths.state_file).last_fix_resolutions
    assert {e["evidence"] for e in resolution["operator_evidence"]} == {"bound"}
    assert eng.step().next_phase == "READY_FOR_MERGE"
    [evidence] = seen
    assert "operator-bound; adequacy not verified by the controller" in evidence


def test_a_bound_comment_edited_before_it_is_observed_holds(tmp_state_dir):
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(operator("R1-F1", (16, NOTE16))))
    assert eng.step().next_phase == "BLOCKED"
    x16 = _k10(eng, T16).action_id
    c16 = _post(gh, T16, 5016, "Existing notes.")
    assert eng.unblock(REASON, bind_evidence=[(x16, c16.url)]).unblocked
    gh.comments[T16] = [replace(c16, body="Existing notes, edited.")]
    assert eng.step().next_phase == "BLOCKED"
    reason = load_state(eng.paths.state_file).block_reason
    assert "changed since it was bound" in reason and sha256_text(c16.body) in reason


def _status(eng) -> list[dict]:
    """``status --json``'s ``operator_actions``, from the state file alone (offline)."""
    from autoforge import cli

    return cli._operator_actions(load_state(eng.paths.state_file))


@pytest.mark.parametrize("change", ["edited", "deleted"])
def test_a_failed_verification_of_bound_evidence_is_kept_for_status(tmp_state_dir, change):
    """PR #254 R3-F3: the bound comment edited or deleted before FIX verifies
    it blocks with both digests, and the reloaded state reports the action
    ``verification_failed`` with that reason, never ``evidence_bound``. A
    new binding replaces the obsolete failure, and its verification
    completes FIX."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(operator("R1-F1", (16, NOTE16))))
    assert eng.step().next_phase == "BLOCKED"
    x16 = _k10(eng, T16).action_id
    c16 = _post(gh, T16, 5016, "Existing notes.")
    assert eng.unblock(REASON, bind_evidence=[(x16, c16.url)]).unblocked
    [entry] = _status(eng)
    assert (entry["state"], entry["reason"]) == ("evidence_bound", "")
    if change == "edited":
        gh.comments[T16] = [replace(c16, body="Existing notes, edited.")]
        now = sha256_text("Existing notes, edited.")
    else:
        gh.comments[T16] = []
        now = "none: the comment is gone"
    assert eng.step().next_phase == "BLOCKED"
    [entry] = _status(eng)
    assert entry["state"] == "verification_failed", entry
    assert f"the bound comment {c16.url} changed since it was bound" in entry["reason"]
    assert f"bound sha256 {sha256_text(c16.body)}, now {now}" in entry["reason"]
    assert entry["binding"]["comment_id"] == 5016 and entry["comment_url"] == ""
    record = _k10(eng, T16)
    assert (record.stage, record.attempts) == (Stage.INTENDED, 0)
    reason = load_state(eng.paths.state_file).block_reason
    assert f"{len(record.body)} characters, sha256 {sha256_text(record.body)}): verification " in (
        reason
    )
    fresh = _post(gh, T16, 5017, "Existing notes, restated.")
    assert eng.unblock(REASON, bind_evidence=[(x16, fresh.url)]).unblocked
    [entry] = _status(eng)
    assert (entry["state"], entry["reason"]) == ("evidence_bound", "")
    assert entry["binding"]["comment_id"] == 5017
    assert eng.step().next_phase == "REVIEW"
    [entry] = _status(eng)
    assert (entry["state"], entry["evidence"], entry["reason"]) == ("verified", "bound", "")
    assert entry["comment_url"] == fresh.url


@pytest.mark.parametrize("failure", ["unreadable", "listing", "gone"])
def test_a_conclusive_target_failure_is_kept_for_status_until_a_pass_clears_it(
    tmp_state_dir, monkeypatch, failure
):
    """PR #254 R3-F3: #16 cannot be verified (its issue or its comments
    cannot be read, or it is gone) while #158's payload is verified: the
    reloaded state reports #16 ``verification_failed`` naming the target,
    and the EPIC's untouched action plain ``pending``. Once #16 reads again
    and the EPIC's payload is posted, the next pass clears #16's failure:
    nothing is on #16 yet, so it is pending once more."""
    gh = FakeGitHub()
    note1 = "Coordination note for the EPIC."
    asks = operator("R1-F1", (16, NOTE16), (158, NOTE158), (1, note1))
    eng = _operating(tmp_state_dir, gh, fixer(asks))
    assert eng.step().next_phase == "BLOCKED"
    x16, x158, x1 = (_k10(eng, t).action_id for t in (T16, T158, EPIC))
    _post(gh, T158, 9158, payload(T158, NOTE158))
    assert eng.unblock(REASON).unblocked
    real_comments = gh.get_issue_comments
    issue16 = gh.issues[T16]
    if failure == "unreadable":
        gh.get_issue_errors[T16] = GitHubError("HTTP 403: Resource not accessible")
        says = f"issue {T16} cannot be read (HTTP 403"
    elif failure == "listing":

        def listing(url):
            if url == T16:
                raise GitHubError("HTTP 403: Resource not accessible")
            return real_comments(url)

        monkeypatch.setattr(gh, "get_issue_comments", listing)
        says = f"the comments of {T16} cannot be read (HTTP 403"
    else:
        del gh.issues[T16]
        says = f"issue {T16} is not found"
    assert eng.step().next_phase == "BLOCKED"
    states = {e["action_id"]: e for e in _status(eng)}
    assert states[x16]["state"] == "verification_failed", states[x16]
    assert says in states[x16]["reason"] and "nothing was verified" in states[x16]["reason"]
    assert (states[x158]["state"], states[x158]["reason"]) == ("verified", "")
    assert (states[x1]["state"], states[x1]["reason"]) == ("pending", "")
    gh.get_issue_errors.pop(T16, None)
    monkeypatch.setattr(gh, "get_issue_comments", real_comments)
    gh.issues[T16] = issue16
    _post(gh, EPIC, 9001, payload(EPIC, note1))
    assert eng.unblock(REASON).unblocked
    assert eng.step().next_phase == "BLOCKED"
    states = {e["action_id"]: e for e in _status(eng)}
    assert (states[x16]["state"], states[x16]["reason"]) == ("pending", "")
    assert states[x1]["state"] == "verified"
    reason = load_state(eng.paths.state_file).block_reason
    assert f"): pending: operator_comment effect 0 on {T16} ({x16}): awaiting" in reason


@pytest.mark.parametrize("case", ["wrong parent", "own key", "other repo", "same again"])
def test_a_binding_outside_the_rules_is_refused_byte_identical(tmp_state_dir, case):
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    x16 = _k10(eng, T16).action_id
    pairs: list[tuple[str, str]]
    if case == "wrong parent":
        c = _post(gh, ISSUE, 5002, "Notes on the run's own issue.")
        pairs, expect = [(x16, c.url)], "not on the action's target"
    elif case == "own key":
        c = _post(gh, T16, 5016, payload(T16, NOTE16).replace(NOTE16, "Other text."))
        pairs, expect = [(x16, c.url)], "this action's own marker"
    elif case == "other repo":
        pairs, expect = (
            [(x16, "https://github.com/other/repo/issues/16#issuecomment-1")],
            ("not in the run's repository"),
        )
    else:
        c = _post(gh, T16, 5016, "Notes.")
        assert eng.unblock(REASON, bind_evidence=[(x16, c.url)]).unblocked
        eng.state.phase = Phase.BLOCKED
        eng.state.block_reason = "held"
        eng._save()
        pairs, expect = [(x16, c.url)], "binding it again is no progress"
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, bind_evidence=pairs)
    assert not out.unblocked and expect in out.message, out.message
    assert eng.paths.state_file.read_text() == raw


def test_a_binding_whose_listing_cannot_be_read_is_refused_byte_identical(tmp_state_dir):
    """PR #254 R1-F1: a conclusive listing failure refuses, naming the action
    and the target; a transient one propagates. Either way nothing is saved."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    x16 = _k10(eng, T16).action_id
    c = _post(gh, T16, 5016, "Notes.")
    gh.comments_error = GitHubError("HTTP 403: Resource not accessible")
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, bind_evidence=[(x16, c.url)])
    assert not out.unblocked, out.message
    assert f"{x16} cannot be bound to {c.url}" in out.message
    assert f"the comments of {T16} cannot be read" in out.message
    assert eng.paths.state_file.read_text() == raw
    gh.comments_error = GitHubUnavailableError("connection reset")
    with pytest.raises(GitHubUnavailableError):
        eng.unblock(REASON, bind_evidence=[(x16, c.url)])
    assert eng.paths.state_file.read_text() == raw


def test_one_comment_is_never_evidence_for_two_actions(tmp_state_dir):
    """Rule 4: two findings each ask for a note on #16; one comment bound to
    both, in one unblock or after the other's binding, is refused."""
    gh = FakeGitHub()
    two = [operator("R1-F1", (16, NOTE16)), operator("R1-F2", (16, NOTE158))]
    eng = _operating(tmp_state_dir, gh, fixer(*two), findings=2)
    assert eng.step().next_phase == "BLOCKED", load_state(eng.paths.state_file).block_reason
    first, second = _records(eng)[:2]
    c = _post(gh, T16, 5016, "Notes for both findings.")
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, bind_evidence=[(first.action_id, c.url), (second.action_id, c.url)])
    assert not out.unblocked and "already bound to or observed for" in out.message
    assert eng.paths.state_file.read_text() == raw


def test_an_earlier_rounds_payload_may_be_bound(tmp_state_dir):
    """Rule 3: a comment carrying an earlier round's marker for the same
    issue, PR and target is bindable (its key is not this record's)."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(operator("R2-F1", (16, NOTE16))), round_=2)
    c = _post(gh, T16, 5016, payload(T16, NOTE16, fid="R1-F1", round_=1))
    marker = render_operator_comment_marker(ISSUE, PR, 1, "R1-F1", T16)
    assert (
        scan(OPERATOR_COMMENT, c.body).claims[0].key == scan(OPERATOR_COMMENT, marker).claims[0].key
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    x16 = _k10(eng, T16).action_id
    assert eng.unblock(REASON, bind_evidence=[(x16, c.url)]).unblocked
    assert eng.step().next_phase == "REVIEW"


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_binding_reads_every_pending_target_first(tmp_state_dir, dry_run):
    """PR #254 R2-F3: binding #16 reads #158's target as well. A conclusive
    failure to read it refuses, naming #158's action and target, with the
    state byte-identical, also as a dry run; a transient one propagates."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    c = _post(gh, T16, 5016, "Notes.")
    gh.get_issue_errors[T158] = GitHubError("HTTP 403: Resource not accessible")
    raw = eng.paths.state_file.read_text()
    calls = len(gh.calls)
    out = eng.unblock(REASON, bind_evidence=[(x16, c.url)], dry_run=dry_run)
    assert not out.unblocked, out.message
    assert f"({x158}) cannot be checked, because {T158} cannot be read" in out.message
    assert "HTTP 403" in out.message
    assert ("get_issue", T158) in gh.calls[calls:]
    assert eng.paths.state_file.read_text() == raw
    gh.get_issue_errors[T158] = GitHubUnavailableError("connection reset")
    with pytest.raises(GitHubUnavailableError):
        eng.unblock(REASON, bind_evidence=[(x16, c.url)], dry_run=dry_run)
    assert eng.paths.state_file.read_text() == raw


def test_a_binding_beside_a_target_that_is_gone_binds(tmp_state_dir):
    """An unnamed pending target that is not found holds its own action and
    refuses no binding of another (it can be declined)."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    x16 = _k10(eng, T16).action_id
    c = _post(gh, T16, 5016, "Notes.")
    del gh.issues[T158]
    out = eng.unblock(REASON, bind_evidence=[(x16, c.url)])
    assert out.unblocked, out.message
    assert _k10(eng, T16).binding is not None


TWO_ON_16 = [operator("R1-F1", (16, NOTE16)), operator("R1-F2", (16, NOTE158))]


def _bind_directly(eng, record: EffectRecord, comment) -> None:
    """Save ``record`` bound to ``comment`` as no ``unblock`` accepts it now, so
    that verification's own rule is what a test exercises."""
    bound = record.binding_to(comment.url, comment.id, sha256_text(comment.body))
    eng.state.effect_records[record.position] = bound.to_dict()
    eng._save()


def test_another_actions_exact_payload_is_never_bound(tmp_state_dir):
    """PR #254 R2-F1: two findings each ask for a note on #16. The first
    action's exact payload is on #16 while both are pending; binding the
    second to it is refused byte-identical, naming the first, and the first
    is then verified from it alone."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(*TWO_ON_16), findings=2)
    assert eng.step().next_phase == "BLOCKED"
    first, second = _records(eng)[:2]
    c = _post(gh, T16, 5016, first.body)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, bind_evidence=[(second.action_id, c.url)])
    assert not out.unblocked, out.message
    assert f"it carries the marker of {first.action_id}, another operator action" in out.message
    assert eng.paths.state_file.read_text() == raw
    assert eng.unblock(REASON).unblocked
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    first, second = _records(eng)[:2]
    assert first.stage is Stage.OBSERVED and first.observed["evidence"] == "exact"
    assert second.stage is Stage.INTENDED and second.binding is None


def test_a_comment_carrying_another_actions_marker_never_verifies_a_binding(tmp_state_dir):
    """PR #254 R2-F1, at verification: a saved binding to the comment that is
    a later action's exact evidence holds, whatever the plan order, and the
    comment is recorded for that action only."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(*TWO_ON_16), findings=2)
    assert eng.step().next_phase == "BLOCKED"
    first, second = _records(eng)[:2]
    c = _post(gh, T16, 5016, second.body)
    _bind_directly(eng, first, c)
    assert eng.unblock(REASON).unblocked
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    first, second = _records(eng)[:2]
    assert first.stage is Stage.INTENDED
    assert second.stage is Stage.OBSERVED and second.observed["comment_id"] == c.id
    reason = load_state(eng.paths.state_file).block_reason
    assert f"the bound comment {c.url} is the evidence of {second.action_id}" in reason


@pytest.mark.parametrize("route", ["prefix", "drift"])
def test_two_bindings_to_one_comment_verify_one_action(tmp_state_dir, route):
    """PR #254 R2-F1: should two actions be bound to one comment, the prefix
    pass and drift's read-only reconcile each verify the first only."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(*TWO_ON_16), findings=2)
    assert eng.step().next_phase == "BLOCKED"
    first, second = _records(eng)[:2]
    c = _post(gh, T16, 5016, "Notes for both findings.")
    _bind_directly(eng, first, c)
    _bind_directly(eng, _records(eng)[1], c)
    if route == "drift":
        gh.set_head(publish_pr_head(eng, parent=eng.state.reviewed_head_sha, message="Push (#2)"))
    assert eng.unblock(REASON).unblocked
    out = eng.step()
    s = load_state(eng.paths.state_file)
    if route == "drift":
        assert out.next_phase == "REVIEW", out.message
        assert [
            (o["action_id"], o["outcome"], o["comment_url"]) for o in s.operator_comment_outcomes
        ] == [(first.action_id, "verified", c.url), (second.action_id, "stale", "")]
        return
    assert out.next_phase == "BLOCKED", out.message
    first, second = _records(eng)[:2]
    assert first.stage is Stage.OBSERVED and first.observed["evidence"] == "bound"
    assert second.stage is Stage.INTENDED
    assert f"the bound comment {c.url} is the evidence of {first.action_id}" in s.block_reason


def test_an_exact_payload_on_a_comment_another_action_consumed_is_a_conflict(tmp_state_dir):
    """PR #254 R2-F1: the first action was verified on a bound comment, which
    was then edited into the second action's exact payload. The second is
    not verified from it: the unblock is refused, naming the first."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(*TWO_ON_16), findings=2)
    assert eng.step().next_phase == "BLOCKED"
    first, second = _records(eng)[:2]
    c = _post(gh, T16, 5016, "Notes for the first finding.")
    assert eng.unblock(REASON, bind_evidence=[(first.action_id, c.url)]).unblocked
    assert eng.step().next_phase == "BLOCKED"
    assert _records(eng)[0].stage is Stage.OBSERVED
    gh.comments[T16] = [replace(c, body=second.body)]
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked, out.message
    assert f"is already the evidence of {first.action_id}" in out.message
    assert eng.paths.state_file.read_text() == raw


def test_a_comment_verified_earlier_in_the_round_is_never_bound_again(tmp_state_dir):
    """PR #254 R2-F1: #16's comment was verified for R1-F1 before a decline
    dropped the plan; the relaunched fixer's action for R1-F2 on #16 is
    neither bound to it nor verified from it."""
    gh = FakeGitHub()
    eng = _operating(
        tmp_state_dir,
        gh,
        scripted(
            fixer(BOTH, dict(NO_CHANGE, finding_id="R1-F2")),
            fixer(NO_CHANGE, operator("R1-F2", (16, NOTE158)), commit=False),
        ),
        findings=2,
    )
    assert eng.step().next_phase == "BLOCKED"
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    c16 = _post(gh, T16, 9016, payload(T16, NOTE16))
    assert eng.unblock(REASON).unblocked
    assert eng.step().next_phase == "BLOCKED"
    assert eng.unblock("not on #158", decline_action=[x158]).unblocked
    assert eng.step().next_phase == "BLOCKED"
    again = _k10(eng, T16)
    assert again.action_id != x16
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, bind_evidence=[(again.action_id, c16.url)])
    assert not out.unblocked and f"already bound to or observed for {x16}" in out.message
    assert eng.paths.state_file.read_text() == raw
    _bind_directly(eng, again, c16)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON)
    assert not out.unblocked and f"is the evidence of {x16}" in out.message, out.message
    assert eng.paths.state_file.read_text() == raw


def test_approving_an_operator_action_is_refused(tmp_state_dir):
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    x16 = _k10(eng, T16).action_id
    with pytest.raises(ConfigurationError, match="operator-performed: nothing to approve"):
        eng.unblock(REASON, approve_action=x16)
    with pytest.raises(ConfigurationError, match="takes one of"):
        eng.unblock(REASON, decline_action=[x16], bind_evidence=[(x16, f"{T16}#issuecomment-1")])
    with pytest.raises(ConfigurationError, match="X-<16 hex digits>"):
        eng.unblock(REASON, bind_evidence=[("A-" + "0" * 16, f"{T16}#issuecomment-1")])


# -- decline and withdrawal ------------------------------------------------------------


def test_a_decline_after_partial_completion_keeps_the_verified_comment(tmp_state_dir):
    """#16 is verified, #158 declined: the plan is dropped, #16 survives as a
    verified outcome, and the relaunched fixer is told both; it may not ask
    for #16 again in this round, nor for #158 unchanged."""
    gh = FakeGitHub()
    eng = _operating(
        tmp_state_dir,
        gh,
        scripted(
            fixer(BOTH),
            fixer(BOTH, commit=False),
            fixer(NO_CHANGE, commit=False),
        ),
    )
    assert eng.step().next_phase == "BLOCKED"
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    c16 = _post(gh, T16, 9016, payload(T16, NOTE16))
    assert eng.unblock(REASON).unblocked
    assert eng.step().next_phase == "BLOCKED"
    out = eng.unblock("the operator will not post on #158", decline_action=[x158])
    assert out.unblocked and out.phase == "FIX", out.message
    s = load_state(eng.paths.state_file)
    assert s.effect_records == [] and s.unblock_history[-1]["action"] == f"declined {x158}"
    assert [(o["action_id"], o["outcome"]) for o in s.operator_comment_outcomes] == [
        (x16, "verified"),
        (x158, "declined"),
    ]
    assert s.round_declines() == 1
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    _, again, corrected = eng.provider.calls
    assert x158 in again.prompt and c16.url in again.prompt
    assert "verified work is never requested again" in corrected.prompt
    [resolution] = load_state(eng.paths.state_file).last_fix_resolutions
    assert [e["comment_url"] for e in resolution["operator_evidence"]] == [c16.url]


def test_a_decline_withdraws_the_other_pending_actions(tmp_state_dir):
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    out = eng.unblock(REASON, decline_action=[x16])
    assert out.unblocked, out.message
    s = load_state(eng.paths.state_file)
    assert s.unblock_history[-1]["action"] == f"declined {x16} (withdrawn: {x158})"
    assert [(o["action_id"], o["outcome"]) for o in s.operator_comment_outcomes] == [
        (x16, "declined"),
        (x158, "withdrawn"),
    ]
    assert s.round_declines() == 1


@pytest.mark.parametrize("case", ["posted", "conflict", "misplaced", "unreadable", "bound"])
def test_a_decline_is_refused_while_anything_of_the_plan_is_on_github(tmp_state_dir, case):
    """A decline drops the plan, so it is refused while a key of the plan
    sits on GitHub unsaved (it would conflict with a later request), while a
    conflict awaits repair, and while a target cannot be read."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    if case == "conflict":
        _post(gh, T16, 9016, payload(T16, NOTE16) + " ")  # other bytes, before the plan
    assert eng.step().next_phase == "BLOCKED"
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    if case == "posted":
        _post(gh, T16, 9016, payload(T16, NOTE16))
        expect = "is there"
    elif case == "conflict":
        assert _k10(eng, T16).stage is Stage.CONFLICT
        expect = "repaired, never declined"
    elif case == "misplaced":
        _post(gh, T158, 9016, payload(T16, NOTE16))
        expect = "misplaced payload"
    elif case == "unreadable":
        gh.comments_error = GitHubError("HTTP 403")
        expect = "cannot be read"
    else:
        c = _post(gh, T16, 5016, "Notes.")
        assert eng.unblock(REASON, bind_evidence=[(x16, c.url)]).unblocked
        eng.state.phase = Phase.BLOCKED
        eng.state.block_reason = "held"
        eng._save()
        expect = "is in place"
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, decline_action=[x158])
    assert not out.unblocked and expect in out.message, out.message
    assert eng.paths.state_file.read_text() == raw


def test_the_decline_bound_is_shared_with_acceptance_actions(tmp_state_dir):
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    eng.state.acceptance_outcomes = [
        {
            "action_id": f"A-{n:016x}",
            "round": 1,
            "finding_id": "R1-F1",
            "outcome": "declined",
            "at": "2026-10-21T00:00:00Z",
            "comment_url": "",
            "reason": "",
        }
        for n in range(MAX_DECLINES_PER_ROUND - 1)
    ]
    eng._save()
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, decline_action=[x16, x158])
    assert not out.unblocked and "acceptance and operator actions together" in out.message
    assert eng.paths.state_file.read_text() == raw
    assert eng.unblock(REASON, decline_action=[x16]).unblocked


# -- drift -----------------------------------------------------------------------------


def test_drift_records_each_action_verified_or_stale_and_reviews(tmp_state_dir):
    """The PR moved while the run waited: bind and decline are refused as
    stale; a plain unblock re-enters FIX, which reconciles each pending
    action read-only (#16 posted: verified; #158 not: stale) and routes the
    drift to REVIEW, sending nothing."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    c16 = _post(gh, T16, 9016, payload(T16, NOTE16))
    head = publish_pr_head(eng, parent=eng.state.reviewed_head_sha, message="A human's push (#2)")
    gh.set_head(head)
    raw = eng.paths.state_file.read_text()
    out = eng.unblock(REASON, decline_action=[x158])
    assert not out.unblocked and "refused as stale" in out.message
    assert eng.paths.state_file.read_text() == raw
    out = eng.unblock(REASON)
    assert out.unblocked and out.phase == "FIX", out.message
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    s = load_state(eng.paths.state_file)
    assert s.effect_records == [] and s.last_fix_resolutions == []
    assert [
        (o["action_id"], o["outcome"], o["comment_url"]) for o in s.operator_comment_outcomes
    ] == [
        (x16, "verified", c16.url),
        (x158, "stale", ""),
    ]
    assert gh.effect_writes == []


# -- the run guard -----------------------------------------------------------------------


def test_status_action_prints_the_exact_payload(tmp_state_dir, capsys):
    from autoforge import cli

    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    record = _k10(eng, T158)
    from autoforge.cli import _print_action_payload

    assert _print_action_payload(load_state(eng.paths.state_file), record.action_id) == 0
    assert capsys.readouterr().out == payload(T158, NOTE158)
    assert _print_action_payload(load_state(eng.paths.state_file), "X-" + "0" * 16) == 1
    actions = cli._operator_actions(load_state(eng.paths.state_file))
    assert [(a["action_id"], a["state"], a["performer"]) for a in actions] == [
        (_k10(eng, T16).action_id, "pending", "operator"),
        (record.action_id, "pending", "operator"),
    ]
