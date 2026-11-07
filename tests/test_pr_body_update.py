"""K12 (#423): a FIX agent asks the controller to replace its own PR's description.

Fakes only: scripted fixers and reviewers, ``FakeGitHub``, a real origin for the
push. A crash test performs the side effect first, dies, and reads the state
back from disk in a new engine. What each test pins is GitHub's bytes and the
writes sent, never an internal call.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import replace

import pytest

from autoforge import cli
from autoforge.claims import IMPLEMENTATION
from autoforge.config import ConfigurationError, default_config, load_config_file
from autoforge.effect_ops import PrBodyUpdateOp
from autoforge.effects import (
    MAX_EFFECT_ATTEMPTS,
    Action,
    EffectKind,
    EffectRecord,
    Stage,
    decide_pr_body_update,
    implementation_closing_block,
    operator_comment_body,
    pr_body_update_body,
    protected_pr_body_block,
    sha256_text,
)
from autoforge.engine import render_follow_up_marker, render_implementation_marker
from autoforge.errors import (
    ControlResultValidationError,
    GitHubError,
    GitHubUnavailableError,
    StateError,
    VerificationError,
)
from autoforge.github import MAX_BODY_CHARS
from autoforge.read_context import MAX_BODY_CHARS as MAX_QUOTED_BODY_CHARS
from autoforge.result_parser import MAX_PR_BODY_CHARS, FixResult
from autoforge.state import MAX_DECLINES_PER_ROUND, load_state
from autoforge.stop_classification import StopReasonCode, classify_state
from autoforge.transitions import Phase
from tests.conftest import (
    BRANCH,
    ISSUE,
    PR,
    PR_BODY_TEXT,
    FakeGitHub,
    acceptance,
    block,
    comment_url,
    connect_origin,
    fix_payload,
    fixed,
    fixer,
    git_out,
    make_engine,
    new_follow_up,
    no_change,
    pr_body_update,
    publish_pr_head,
    read_context_of,
    review_result,
    scripted,
)

REASON = "the operator read the action"
PREFACE = "An early description of the change, written when the PR was opened."
FINDING = {
    "id": "R1-F1",
    "classification": "non-blocked",
    "title": "The PR description describes the superseded manifest behaviour",
    "location": "PR description",
    "required_resolution": "rewrite the PR description",
}
OTHER_ISSUE = "https://github.com/owner/repo/issues/9"
A_HUMAN_EDIT = "A human's edit."


def _finding(n: int = 1, round_: int = 1) -> dict:
    return dict(FINDING, id=f"R{round_}-F{n}")


def _base(run_id: str, preface: str = PREFACE) -> str:
    """A PR body as the controller opened it: the agent's text, then its closing block."""
    return f"{preface}\n\n{implementation_closing_block(ISSUE, run_id)}\n"


def _payload(run_id: str, text: str = PR_BODY_TEXT) -> str:
    """The exact bytes the controller composes for ``text``: its block, then the text."""
    return f"{implementation_closing_block(ISSUE, run_id)}\n\n{text}"


def _updating(
    tmp_state_dir,
    gh: FakeGitHub,
    script,
    *,
    policy: str = "auto",
    findings: int = 1,
    body=None,
):
    """A run in FIX after review round 1, at a real HEAD, whose marker names the run.

    ``body`` is the PR's body, or a function of the run id (default: :func:`_base`).
    """
    gh.add_issue(ISSUE, "Ship the feature")
    cfg = default_config()
    cfg.safety = replace(cfg.safety, pr_body_updates=policy)
    eng = make_engine(tmp_state_dir, script, github=gh, cfg=cfg, origin=True)
    head = publish_pr_head(eng)
    run = eng.state.run_id
    gh.add_pr(head_sha=head, body=(body or _base)(run))
    eng.state.phase = Phase.FIX
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = head
    eng.state.reviewed_pr_url = PR
    eng.state.reviewed_head_sha = head
    eng.state.reviewed_base_ref = "main"
    eng.state.reviewed_merge_base_sha = eng.origin.merge_base("main", head)
    eng.state.review_round = 1
    eng.state.last_review_comment_url = comment_url(PR, 100)
    eng.state.last_review_result = "needs_fix"
    eng.state.open_findings = [_finding(n) for n in range(1, findings + 1)]
    return eng


def _resumed(eng, tmp_state_dir, gh: FakeGitHub, script="never", policy: str | None = None):
    """A new controller process on the same state, git remote and GitHub."""
    eng.close()
    cfg = default_config()
    cfg.safety = replace(cfg.safety, pr_body_updates=policy or eng.config.safety.pr_body_updates)
    eng2 = make_engine(tmp_state_dir, script, github=gh, cfg=cfg)
    connect_origin(eng2, eng.origin)
    eng2.origin = eng.origin
    eng2.load()
    return eng2


def _status_text(state, eng) -> str:
    """The PR body update block and the Recovery block after it, as ``status`` prints them.

    The block describes the update and sends the operator to the Recovery block for
    the next steps, which names only those the stop allows (PR #470 R4-F1).
    """
    stop = classify_state(state, scheduler=False)
    package = cli._recovery_package(eng, argparse.Namespace(config=None), state, stop, None)
    return "\n".join(cli._render_pr_body_updates(state, eng) + package.lines())


def _power_loss_at(eng, kind: EffectKind, stage: Stage, *, saved: bool) -> None:
    """The process dies the first time a ``kind`` record moves to ``stage``."""
    persist = eng._persist_effect
    fired: list[int] = []

    def dying(record):
        if not fired and record.kind is kind and record.stage is stage:
            fired.append(record.position)
            if saved:
                persist(record)
            raise KeyboardInterrupt
        persist(record)

    eng._persist_effect = dying


def _k12(eng) -> EffectRecord:
    records = [EffectRecord.from_dict(r) for r in load_state(eng.paths.state_file).effect_records]
    [record] = [r for r in records if r.kind is EffectKind.PR_BODY_UPDATE]
    return record


def _body_writes(gh: FakeGitHub) -> list[str]:
    return [w[2] for w in gh.effect_writes if w[0] == "write_pr_body"]


def _other_writes(gh: FakeGitHub) -> list[str]:
    return [w[0] for w in gh.effect_writes if w[0] != "write_pr_body"]


def _edit(gh: FakeGitHub, body: str):
    """A human edit of the PR's description, made from a fixer's handler."""

    def edited(req):
        gh.prs[PR].body = body
        return fixer(pr_body_update("R1-F1"), commit=False)(req)

    return edited


# -- the reproduction: PR #767, round 2 ---------------------------------------------------------


def test_a_stale_description_reaches_ready_for_merge_with_no_commit_push_or_block(tmp_state_dir):
    """Forma PR #767 round 2: one actionable non-blocked finding about the description,
    at a HEAD the review found correct. The fixer returns a replacement and commits
    nothing; the controller writes it, reads it back, reviews the unchanged HEAD again,
    with the receipt and the new text in the prompt, and the run is ready for merge."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Ship the feature")
    reviews = []

    def first_review(req):
        return block(review_result(1, eng.state.current_head_sha, [FINDING]))

    def clean_review(req):
        reviews.append(req.prompt)
        return block(review_result(2, eng.state.current_head_sha, []))

    script = scripted(first_review, fixer(pr_body_update("R1-F1"), commit=False), clean_review)
    eng = make_engine(tmp_state_dir, script, github=gh, origin=True)
    head = publish_pr_head(eng)
    run = eng.state.run_id
    gh.add_pr(head_sha=head, body=_base(run))
    eng.state.phase = Phase.REVIEW
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = head
    eng.state.current_base_ref = "main"
    eng.state.current_merge_base_sha = eng.origin.merge_base("main", head)

    assert eng.step().next_phase == "FIX"
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert "PR description verified byte for byte" in out.message
    assert gh.prs[PR].body == _payload(run)
    assert _body_writes(gh) == [_payload(run)]
    assert eng.origin.head(BRANCH) == head == gh.prs[PR].head_sha

    s = load_state(eng.paths.state_file)
    assert s.current_head_sha == head and s.open_findings == []
    [resolution] = s.last_fix_resolutions
    assert resolution["resolution"] == "pr_body_update"
    assert resolution["commit_sha"] == ""
    assert resolution["pr_body_update"] == {
        "action_id": resolution["pr_body_update"]["action_id"],
        "pr_url": PR,
        "base_sha256": sha256_text(_base(run)),
        "body_sha256": sha256_text(_payload(run)),
        "reviewed_head_sha": head,
        "changed": True,
    }
    [outcome] = s.pr_body_outcomes
    assert outcome["outcome"] == "verified" and outcome["finding_ids"] == ["R1-F1"]

    out = eng.step()
    assert out.next_phase == "READY_FOR_MERGE", out.message
    [prompt] = reviews
    assert '"pr_body_update"' in prompt and "never that the description is right" in prompt
    assert PR_BODY_TEXT in (read_context_of(prompt) / "pull-request.md").read_text()
    assert eng.origin.head(BRANCH) == head
    assert _body_writes(gh) == [_payload(run)]
    assert load_state(eng.paths.state_file).block_reason == ""


def test_the_fixer_reads_the_current_description_and_is_told_what_it_may_do(tmp_state_dir):
    """The whole body is replaced, so the fixer is given it, with the offer in its prompt."""
    gh = FakeGitHub()
    prompts = []

    def recording(req):
        prompts.append(req.prompt)
        return fixer(pr_body_update("R1-F1"), commit=False)(req)

    eng = _updating(tmp_state_dir, gh, scripted(recording))
    assert eng.step().next_phase == "REVIEW"
    [prompt] = prompts
    assert "## PR description" in prompt and '"resolution": "pr_body_update"' in prompt
    assert "This run authorizes it by policy" in prompt
    assert PREFACE in (read_context_of(prompt) / "pull-request.md").read_text()


# -- a mixed fix --------------------------------------------------------------------------------


def test_a_mixed_fix_replaces_the_description_before_it_pushes_anything(tmp_state_dir):
    """A commit and a description: the description first, and verified, before the push."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir, gh, scripted(fixer(fixed("R1-F1"), pr_body_update("R1-F2"))), findings=2
    )
    reviewed = eng.state.reviewed_head_sha
    run = eng.state.run_id
    moves = []
    persist = eng._persist_effect

    def spying(record):
        moves.append((record.kind.value, record.stage.value, gh.prs[PR].body == _payload(run)))
        persist(record)

    eng._persist_effect = spying
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert moves == [
        ("pr_body_update", "attempted", False),
        ("pr_body_update", "observed", True),
        ("push", "attempted", True),
        ("push", "observed", True),
    ]
    candidate = eng.origin.head(BRANCH)
    assert candidate != reviewed and gh.prs[PR].head_sha == candidate
    assert gh.prs[PR].body == _payload(run)
    s = load_state(eng.paths.state_file)
    assert [r["resolution"] for r in s.last_fix_resolutions] == ["fixed", "pr_body_update"]
    assert s.last_fix_resolutions[0]["pr_body_update"] is None
    assert s.last_fix_resolutions[1]["pr_body_update"]["reviewed_head_sha"] == reviewed


def test_the_description_comes_before_a_follow_up_issue_and_a_failed_write_keeps_the_candidate(
    tmp_state_dir,
):
    """A conclusive failure of the description stops the plan: no issue, no push, and
    the unpushed candidate is still in the worktree; the block says so."""
    gh = FakeGitHub()
    gh.write_failures.append(("write_pr_body", GitHubError("HTTP 422: refused"), False))
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(fixer(fixed("R1-F1"), pr_body_update("R1-F2"), new_follow_up("R1-F3"))),
        findings=3,
    )
    reviewed = eng.state.reviewed_head_sha
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert _other_writes(gh) == []
    assert eng.origin.head(BRANCH) == reviewed
    assert gh.prs[PR].body == _base(eng.state.run_id)
    reason = load_state(eng.paths.state_file).block_reason
    assert "a refused write is not retried" in reason
    assert len(_body_writes(gh)) == 1
    candidate = git_out("-C", eng.provider.calls[0].cwd, "rev-parse", "HEAD")
    assert candidate != reviewed


# -- idempotence --------------------------------------------------------------------------------


def test_a_body_already_holding_the_text_sends_no_write_and_resolves_nothing_by_itself(
    tmp_state_dir,
):
    """Already matching: no write, a receipt that says so, and the next review still
    judges the finding, which it may raise again."""
    gh = FakeGitHub()

    def still_stale(req):
        return block(review_result(2, eng.state.current_head_sha, [_finding(1, 2)]))

    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(fixer(pr_body_update("R1-F1"), commit=False), still_stale),
        body=_payload,
    )
    run = eng.state.run_id
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert "which it already held; no write" in out.message
    assert _body_writes(gh) == []
    assert gh.prs[PR].body == _payload(run)
    s = load_state(eng.paths.state_file)
    assert s.last_fix_resolutions[0]["pr_body_update"]["changed"] is False
    assert s.pr_body_outcomes[0]["changed"] is False
    assert eng.step().next_phase == "FIX"
    assert load_state(eng.paths.state_file).open_findings[0]["id"] == "R2-F1"


def test_several_findings_share_one_description_and_one_write(tmp_state_dir):
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(fixer(pr_body_update("R1-F1"), pr_body_update("R1-F2"), commit=False)),
        findings=2,
    )
    assert eng.step().next_phase == "REVIEW"
    assert _body_writes(gh) == [_payload(eng.state.run_id)]
    s = load_state(eng.paths.state_file)
    receipts = [r["pr_body_update"] for r in s.last_fix_resolutions]
    assert receipts[0] == receipts[1] and receipts[0]["changed"] is True
    assert s.pr_body_outcomes[0]["finding_ids"] == ["R1-F1", "R1-F2"]


# -- the result's contract ----------------------------------------------------------------------

SHA = "1" * 40


def _parsed(*resolutions: dict) -> FixResult:
    return FixResult.from_payload(fix_payload(SHA, SHA, list(resolutions)))


def test_a_parsed_update_holds_exactly_the_text_stripped():
    res = _parsed(pr_body_update("R1-F1", "\n  ## Why\n\nBecause.  \n\n"))
    assert res.pr_body_text == "## Why\n\nBecause."
    assert res.resolutions[0].pr_body_update and res.resolutions[0].pr_body_text == res.pr_body_text


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"pr_body_update": {"body": "x", "url": PR}}, "url"),
        ({"pr_body_update": {"body": "x", "target": PR}}, "target"),
        ({"pr_body_update": {"body": "x", "pr": 42}}, "pr"),
        ({"pr_body_update": {"body": "x", "repository": "o/r"}}, "repository"),
        ({"pr_body_update": {"body": "x", "method": "PATCH"}}, "method"),
        ({"pr_body_update": {"body": "x", "path": "/repos"}}, "path"),
        ({"pr_body_update": {"body": "x", "command": "gh"}}, "command"),
        ({"pr_body_update": {"body": "x", "unknown": 1}}, "unknown"),
        ({"pr_body_update": {}}, "body"),
        ({"pr_body_update": "text"}, "pr_body_update"),
        ({"pr_body_update": None}, "pr_body_update"),
        ({"pr_body_update": {"body": 1}}, "body"),
        ({"pr_body_update": {"body": "  \n "}}, "body"),
        ({"pr_body_update": {"body": "x" * (MAX_PR_BODY_CHARS + 1)}}, str(MAX_PR_BODY_CHARS)),
        ({"pr_body_update": {"body": "<!-- ai-implementation -->"}}, "marker"),
        ({"pr_body_update": {"body": "<!--  AUTOFORGE-x -->"}}, "marker"),
        ({"pr_body_update": {"body": "Closes #2"}}, "closing keyword"),
        ({"pr_body_update": {"body": "fixes owner/repo#3"}}, "closing keyword"),
        ({"pr_body_update": {"body": "Notify @octocat"}}, "mention"),
        ({"pr_body_update": {"body": "ghp_" + "a" * 36}}, "credential"),
        ({"pr_body_update": {"body": "a\x1bb"}}, "control character"),
        ({"rationale": "short"}, "rationale"),
        ({"commit_sha": SHA}, "commit_sha"),
        ({"follow_up_issue_url": OTHER_ISSUE}, "follow-up"),
    ],
)
def test_a_request_outside_the_contract_is_refused(change, needle):
    with pytest.raises(ControlResultValidationError, match=needle):
        _parsed({**pr_body_update("R1-F1"), **change})


def test_a_request_with_no_update_is_refused_and_no_other_resolution_may_carry_one():
    bare = {k: v for k, v in pr_body_update("R1-F1").items() if k != "pr_body_update"}
    with pytest.raises(ControlResultValidationError, match="pr_body_update"):
        _parsed(bare)
    with pytest.raises(ControlResultValidationError, match="carries a pr_body_update"):
        _parsed({**no_change("R1-F1"), "pr_body_update": {"body": "x"}})


def test_two_findings_with_different_text_are_refused_never_composed():
    with pytest.raises(ControlResultValidationError, match="different text"):
        _parsed(pr_body_update("R1-F1", "one"), pr_body_update("R1-F2", "two"))


def test_the_longest_text_fits_and_the_composed_body_still_meets_githubs_limit():
    """The bound on the text leaves room for the controller's block; a body over GitHub's
    limit as a whole would be a correction, not a clipped write."""
    assert MAX_PR_BODY_CHARS + 2000 < MAX_BODY_CHARS
    assert _parsed(pr_body_update("R1-F1", "x" * MAX_PR_BODY_CHARS)).pr_body_text


# -- what the base must hold --------------------------------------------------------------------


def _marker(run: str, issue: str = ISSUE) -> str:
    return render_implementation_marker(issue, run)


_REFUSED_BASES = {
    "no-marker": lambda r: f"{PREFACE}\n\nCloses #2\n",
    "two-markers": lambda r: f"Closes #2\n\n{_marker(r)}\n\n{_marker(r)}\n",
    "marker-of-another-issue": lambda r: f"Closes #2\n\n{_marker(r, OTHER_ISSUE)}\n",
    "marker-naming-another-run": lambda r: (
        f"Closes #2\n\n{_marker('af-20261103T000000Z-0a1b2c')}\n"
    ),
    "marker-naming-no-run": lambda r: f"Closes #2\n\n{render_implementation_marker(ISSUE, None)}\n",
    "altered-marker": lambda r: (
        f"Closes #2\n\n{_marker(r).replace('ai-implementation', 'ai-implementation x')}\n"
    ),
    "no-closing-line": lambda r: f"{PREFACE}\n\n{_marker(r)}\n",
    "closing-another-issue": lambda r: f"Closes #9\n\n{_marker(r)}\n",
    "closing-this-and-another": lambda r: f"Closes #2\nCloses #9\n\n{_marker(r)}\n",
    "unclosed-controller-marker": lambda r: f"Closes #2\n\n{_marker(r)}\n\n<!-- ai-unknown\n",
}


@pytest.mark.parametrize("name", list(_REFUSED_BASES))
def test_a_body_without_exactly_the_closing_block_is_refused_by_the_block(name):
    block_, why = protected_pr_body_block(
        _REFUSED_BASES[name]("af-this-run-1"), ISSUE, "af-this-run-1"
    )
    assert block_ == "" and why


def test_the_block_keeps_the_markers_bytes_and_tolerates_a_web_edits_line_endings():
    run = "af-this-run-1"
    block_, why = protected_pr_body_block(
        f"Text\r\n\r\nCloses #2\r\n\r\n{_marker(run)}\r\n", ISSUE, run
    )
    assert why == "" and block_ == f"Closes #2\n\n{_marker(run)}"
    assert pr_body_update_body("Text", block_) == f"Closes #2\n\n{_marker(run)}\n\nText"


def test_a_second_controller_marker_is_kept_byte_for_byte_after_the_implementation_marker():
    """Any other controller marker the body holds is the controller's to keep, in order."""
    run = "af-this-run-1"
    other = '<!-- ai-replacement: {"txn": "a  b"} -->'
    block_, why = protected_pr_body_block(
        f"Text\n\n{other}\n\nCloses #2\n\n{_marker(run)}", ISSUE, run
    )
    assert why == "" and block_ == f"Closes #2\n\n{_marker(run)}\n\n{other}"


def test_a_marker_of_another_issue_beside_the_runs_own_stops_before_any_agent(tmp_state_dir):
    """Two implementation claims: the PR's ownership is ambiguous, so FIX launches nothing."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted("never"),
        body=lambda r: f"Closes #2\n\n{_marker(r)}\n\n{_marker(r, OTHER_ISSUE)}\n",
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert eng.provider.calls == [] and _body_writes(gh) == []


@pytest.mark.parametrize(
    "name", ["no-closing-line", "closing-another-issue", "unclosed-controller-marker"]
)
def test_a_base_that_does_not_qualify_is_not_offered_and_a_request_is_corrected(
    tmp_state_dir, name
):
    """The prompt says it is unavailable, and asking anyway is a correction: nothing written."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(
            fixer(pr_body_update("R1-F1"), commit=False),
            fixer(no_change("R1-F1"), commit=False),
        ),
        body=_REFUSED_BASES[name],
    )
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert _body_writes(gh) == []
    [first, second] = eng.provider.calls
    assert "resolution is not available in this FIX entry" in first.prompt
    assert "which this FIX entry does not offer" in second.prompt
    assert gh.prs[PR].body == _REFUSED_BASES[name](eng.state.run_id)


def test_an_injected_text_is_a_correction_and_the_published_payload_keeps_one_marker(
    tmp_state_dir,
):
    gh = FakeGitHub()
    injected = pr_body_update("R1-F1", f"Text\n\n{_marker('af-other-run-1')}")
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(
            fixer(injected, commit=False),
            fixer(pr_body_update("R1-F1"), commit=False),
        ),
    )
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    [written] = _body_writes(gh)
    assert written == _payload(eng.state.run_id)
    assert len(IMPLEMENTATION.pattern.findall(written)) == 1


def test_a_body_too_long_with_the_controllers_block_is_a_correction(tmp_state_dir, monkeypatch):
    monkeypatch.setattr("autoforge.engine.MAX_BODY_CHARS", 100)
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(
            fixer(pr_body_update("R1-F1"), commit=False),
            fixer(no_change("R1-F1"), commit=False),
        ),
    )
    assert eng.step().next_phase == "REVIEW"
    assert _body_writes(gh) == []
    assert "over GitHub's limit" in eng.provider.calls[1].prompt


# -- the PR moved, or is not this run's to edit ---------------------------------------------------


def _moving(gh: FakeGitHub, move):
    """A fixer asking for the update while ``move`` changes the PR under it."""

    def handler(req):
        move(gh.prs[PR])
        return fixer(pr_body_update("R1-F1"), commit=False)(req)

    return handler


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_a_pr_closed_or_merged_while_the_fixer_ran_is_never_written(tmp_state_dir, state):
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir, gh, scripted(_moving(gh, lambda pr: setattr(pr, "state", state)))
    )
    with pytest.raises(VerificationError, match=f"is {state}"):
        eng.step()
    assert _body_writes(gh) == []
    assert gh.prs[PR].body == _base(eng.state.run_id)
    assert load_state(eng.paths.state_file).effect_records == []


def test_a_head_moved_while_the_fixer_ran_goes_to_review_and_writes_nothing(tmp_state_dir):
    """The push lands while the fixer runs, which GitHub's head then shows."""
    gh = FakeGitHub()
    moved: list[str] = []

    def push_from_elsewhere(pr):
        moved.append(publish_pr_head(eng, message="A push from elsewhere (#2)"))

    eng = _updating(tmp_state_dir, gh, scripted(_moving(gh, push_from_elsewhere)))
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert len(eng.provider.calls) == 1
    assert _body_writes(gh) == []
    assert load_state(eng.paths.state_file).current_head_sha == moved[0]


def test_a_base_retargeted_after_the_plan_saved_is_not_written(tmp_state_dir):
    """An approval-held plan, then the PR is retargeted: the approval is refused as stale
    and a plain unblock routes to REVIEW; nothing was written."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)), policy="approve"
    )
    assert eng.step().next_phase == "BLOCKED"
    action = _k12(eng).action_id
    gh.prs[PR].base_ref = "release"
    refused = eng.unblock(REASON, approve_action=action)
    assert not refused.unblocked and "refused as stale" in refused.message
    assert _body_writes(gh) == []
    assert load_state(eng.paths.state_file).phase is Phase.BLOCKED


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        (lambda pr: setattr(pr, "state", "CLOSED"), "only an open PR"),
        (lambda pr: setattr(pr, "state", "MERGED"), "only an open PR"),
        (lambda pr: setattr(pr, "head_repository", "fork/repo"), "fork"),
        (lambda pr: setattr(pr, "head_sha", "b" * 40), "not the reviewed HEAD"),
        (lambda pr: setattr(pr, "base_ref", "release"), "not the reviewed base"),
    ],
    ids=["closed", "merged", "fork", "head", "base"],
)
def test_the_op_refuses_a_pr_that_left_the_binding_from_its_last_read(
    tmp_state_dir, change, needle
):
    """The reads immediately before the write: a conflict, whichever the operator
    approved or the policy allowed, and the write is never sent."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)), policy="approve"
    )
    assert eng.step().next_phase == "BLOCKED"
    record = _k12(eng).approving()
    change(gh.prs[PR])
    found = PrBodyUpdateOp(gh).reconcile(record)
    assert found.action is Action.CONFLICT and needle in found.reason


def test_the_op_refuses_a_second_marked_pr_and_a_moved_merge_base(tmp_state_dir):
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)), policy="approve"
    )
    assert eng.step().next_phase == "BLOCKED"
    record = _k12(eng).approving()
    head = gh.prs[PR].head_sha
    gh.merge_bases[("main", head.lower())] = "c" * 40
    found = PrBodyUpdateOp(gh).reconcile(record)
    assert found.action is Action.CONFLICT and "not the reviewed merge base" in found.reason
    del gh.merge_bases[("main", head.lower())]
    other = "https://github.com/owner/repo/pull/43"
    gh.add_pr(other, head_sha="d" * 40, branch="other", body=_base(eng.state.run_id))
    found = PrBodyUpdateOp(gh).reconcile(record)
    assert (
        found.action is Action.CONFLICT and "also carries the implementation marker" in found.reason
    )


# -- concurrent edits ---------------------------------------------------------------------------


def test_an_edit_made_while_the_fixer_ran_blocks_with_nothing_planned_or_sent(tmp_state_dir):
    """The fixer wrote from the body the entry read. A human changed it meanwhile: not
    written over, not adopted, nothing planned; the block names both digests and the
    ways on, and a plain unblock runs the fixer afresh over the edited body."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(
            lambda req: _edit(gh, _base(eng.state.run_id, A_HUMAN_EDIT))(req),
            fixer(pr_body_update("R1-F1"), commit=False),
        ),
    )
    run = eng.state.run_id
    edited = _base(run, A_HUMAN_EDIT)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.EFFECT_CONFLICT
    s = load_state(eng.paths.state_file)
    assert s.effect_records == [] and _body_writes(gh) == []
    assert gh.prs[PR].body == edited
    assert sha256_text(edited) in s.block_reason and sha256_text(_base(run)) in s.block_reason
    assert "changed while the fixer ran" in s.block_reason
    assert "--rereview" in s.block_reason
    assert eng.unblock(REASON).unblocked
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert len(eng.provider.calls) == 2
    assert _body_writes(gh) == [_payload(run)]
    assert A_HUMAN_EDIT not in gh.prs[PR].body


def _journaled(tmp_state_dir, gh: FakeGitHub):
    """A plan saved with its K12 intended, and the process gone before the write."""
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, Stage.ATTEMPTED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    assert _body_writes(gh) == [] and len(eng.provider.calls) == 1
    return eng


def test_an_edit_after_the_plan_saved_is_a_conflict_and_a_reason_is_not_evidence(tmp_state_dir):
    """Journaled, then a human edits: no write. An unblock reason does not move it. Putting
    the base back lets the journal finish with no fixer launched."""
    gh = FakeGitHub()
    eng = _journaled(tmp_state_dir, gh)
    run = eng.state.run_id
    edited = _base(run, A_HUMAN_EDIT)
    gh.prs[PR].body = edited

    eng2 = _resumed(eng, tmp_state_dir, gh)
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    assert _body_writes(gh) == [] and gh.prs[PR].body == edited
    s = load_state(eng2.paths.state_file)
    assert "the PR body changed outside the plan" in s.block_reason
    assert sha256_text(_base(run)) in s.block_reason
    assert f"--decline-action {_k12(eng2).action_id}" in s.block_reason

    refused = eng2.unblock("it looks fine to me")
    assert not refused.unblocked
    assert "An unblock reason is not evidence and the edit is not adopted" in refused.message
    assert gh.prs[PR].body == edited

    gh.prs[PR].body = _base(run)
    done = eng2.unblock("the base is back")
    assert done.unblocked and done.phase == "FIX", done.message
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert eng2.provider.calls == []
    assert _body_writes(gh) == [_payload(run)]


def test_an_operator_who_keeps_the_edit_declines_and_the_fixer_runs_over_it(tmp_state_dir):
    gh = FakeGitHub()
    eng = _journaled(tmp_state_dir, gh)
    run = eng.state.run_id
    edited = _base(run, A_HUMAN_EDIT)
    gh.prs[PR].body = edited
    prompts = []

    def afresh(req):
        prompts.append(req.prompt)
        return fixer(pr_body_update("R1-F1", "A better text."), commit=False)(req)

    eng2 = _resumed(eng, tmp_state_dir, gh, scripted(afresh))
    assert eng2.step().next_phase == "BLOCKED"
    action = _k12(eng2).action_id
    done = eng2.unblock("keep the edit", decline_action=[action])
    assert done.unblocked and done.phase == "FIX", done.message
    s = load_state(eng2.paths.state_file)
    assert s.effect_records == [] and s.pr_body_outcomes[-1]["outcome"] == "declined"
    assert s.unblock_history[-1]["action"] == f"declined {action}"
    assert eng2.step().next_phase == "REVIEW"
    assert action in prompts[0] and "keep the edit" in prompts[0]
    assert A_HUMAN_EDIT in (read_context_of(prompts[0]) / "pull-request.md").read_text()
    assert _body_writes(gh) == [_payload(run, "A better text.")]


def test_a_write_that_landed_though_github_answered_an_error_is_observed_with_one_write(
    tmp_state_dir,
):
    """Whatever GitHub answered, the read decides: the body reads as the payload."""
    gh = FakeGitHub()
    gh.write_failures.append(("write_pr_body", GitHubError("HTTP 500 after the edit"), True))
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert _body_writes(gh) == [_payload(eng.state.run_id)]


def test_a_write_github_refused_is_not_retried(tmp_state_dir):
    gh = FakeGitHub()
    gh.write_failures.append(("write_pr_body", GitHubError("HTTP 422: refused"), False))
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.EFFECT_CONFLICT
    assert len(_body_writes(gh)) == 1 and gh.prs[PR].body == _base(eng.state.run_id)
    assert "a refused write is not retried" in load_state(eng.paths.state_file).block_reason


def test_an_unknown_outcome_that_landed_is_read_back_with_no_second_write(tmp_state_dir):
    """The write's answer is lost but the body reads as the payload: observed, in this step."""
    gh = FakeGitHub()
    gh.write_failures.append(("write_pr_body", GitHubUnavailableError("gh: HTTP 502"), True))
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert _body_writes(gh) == [_payload(eng.state.run_id)]


def test_an_unknown_outcome_that_did_not_land_stops_and_the_next_process_reads_first(
    tmp_state_dir,
):
    """The attempt is on disk and nothing is retried in this process. The next one reads:
    the body is still the base, so the write is sent once more, within the bound."""
    gh = FakeGitHub()
    gh.write_failures.append(("write_pr_body", GitHubUnavailableError("gh: HTTP 502"), False))
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    run = eng.state.run_id
    with pytest.raises(GitHubUnavailableError):
        eng.step()
    assert len(_body_writes(gh)) == 1 and _k12(eng).stage is Stage.ATTEMPTED
    eng2 = _resumed(eng, tmp_state_dir, gh)
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert eng2.provider.calls == [] and gh.prs[PR].body == _payload(run)
    assert len(_body_writes(gh)) == 2


def test_unknown_outcomes_stop_at_the_attempt_bound_and_block(tmp_state_dir):
    gh = FakeGitHub()
    for _ in range(MAX_EFFECT_ATTEMPTS + 1):
        gh.write_failures.append(("write_pr_body", GitHubUnavailableError("gh: HTTP 502"), False))
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    for _ in range(MAX_EFFECT_ATTEMPTS):
        with pytest.raises(GitHubUnavailableError):
            eng.step()
        eng = _resumed(eng, tmp_state_dir, gh)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert len(_body_writes(gh)) == MAX_EFFECT_ATTEMPTS
    assert gh.prs[PR].body == _base(eng.state.run_id)


def test_an_unknown_outcome_with_a_concurrent_edit_is_a_conflict_not_a_retry(tmp_state_dir):
    """The answer is lost and the body is neither the base nor the payload: stop."""
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    run = eng.state.run_id
    raced = _base(run, "A human won the race.")

    def racing(url, body):
        gh.effect_writes.append(("write_pr_body", url, body))
        gh.prs[PR].body = raced
        raise GitHubUnavailableError("gh: HTTP 502")

    gh.write_pr_body = racing
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.EFFECT_CONFLICT
    assert len(_body_writes(gh)) == 1 and gh.prs[PR].body == raced
    assert "the PR body changed outside the plan" in load_state(eng.paths.state_file).block_reason


def test_a_human_edit_removing_the_marker_after_the_write_is_refused_as_ownership(tmp_state_dir):
    """A body with no marker at all is not this run's PR to drive: the owner rule stops it."""
    gh = FakeGitHub()
    eng = _journaled(tmp_state_dir, gh)
    gh.prs[PR].body = "A human wrote this, and no marker."
    eng2 = _resumed(eng, tmp_state_dir, gh)
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    assert _body_writes(gh) == [] and gh.prs[PR].body == "A human wrote this, and no marker."
    assert "carries no implementation marker" in load_state(eng2.paths.state_file).block_reason


def test_a_read_back_that_fails_conclusively_blocks_and_writes_nothing_more(tmp_state_dir):
    """The write landed; the only read that could prove its bytes fails: fail closed."""
    gh = FakeGitHub()
    text = "A literal ^G in the description."
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1", text), commit=False)))
    run = eng.state.run_id
    real = gh.write_pr_body

    def landing(url, body):
        real(url, body)
        gh.gh_sanitizes = True
        gh.exact_read_error = GitHubError("HTTP 403 on the exact read")

    gh.write_pr_body = landing
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert _body_writes(gh) == [_payload(run, text)]
    assert _k12(eng).stage is not Stage.OBSERVED


# -- `gh` rewrites what it prints -----------------------------------------------------------------


@pytest.mark.parametrize("text", ["A literal ^G inside.", "With \x7f inside."], ids=["^G", "del"])
def test_a_base_gh_prints_as_it_is_is_read_exactly_and_the_digests_are_of_githubs_bytes(
    tmp_state_dir, text
):
    """A `^` makes the print suspect, so the controller's digest and comparison use the
    exact read; a body that read shows to be the print is offered, and the plan binds the
    real bytes with no false conflict. A rewritten one is not offered (the view's table)."""
    gh = FakeGitHub()
    gh.gh_sanitizes = True
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(fixer(pr_body_update("R1-F1"), commit=False)),
        body=lambda r: _base(r, text),
    )
    run = eng.state.run_id
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    [outcome] = load_state(eng.paths.state_file).pr_body_outcomes
    assert outcome["base_sha256"] == sha256_text(_base(run, text))
    assert _body_writes(gh) == [_payload(run)] and gh.prs[PR].body == _payload(run)


def test_a_literal_caret_in_the_replacement_is_verified_on_the_exact_read(tmp_state_dir):
    gh = FakeGitHub()
    gh.gh_sanitizes = True
    text = "A literal ^G in the description."
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1", text), commit=False)))
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert gh.prs[PR].body == _payload(eng.state.run_id, text)


def test_a_body_only_the_print_of_the_payload_is_not_the_payload(tmp_state_dir):
    """The body holds a control character whose print equals the payload's literal
    caret: `gh` cannot tell them apart, the exact read can, and it is a conflict."""
    gh = FakeGitHub()
    gh.gh_sanitizes = True
    text = "A literal ^G in the description."
    eng = _journaled_text(tmp_state_dir, gh, text)
    run = eng.state.run_id
    gh.prs[PR].body = _payload(run, text.replace("^G", "\x07"))
    eng2 = _resumed(eng, tmp_state_dir, gh)
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    assert _body_writes(gh) == []


def _journaled_text(tmp_state_dir, gh: FakeGitHub, text: str):
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1", text), commit=False)))
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, Stage.ATTEMPTED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    return eng


def test_an_exact_read_that_fails_at_entry_offers_nothing_and_launches_with_that_said(
    tmp_state_dir,
):
    gh = FakeGitHub()
    gh.gh_sanitizes = True
    gh.exact_read_error = GitHubError("HTTP 403 on the exact read")
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(fixer(no_change("R1-F1"), commit=False)),
        body=lambda r: _base(r, "With \x07 inside."),
    )
    assert eng.step().next_phase == "REVIEW"
    assert "not available in this FIX entry" in eng.provider.calls[0].prompt


# -- crash windows ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "saved", "writes"),
    [
        (Stage.ATTEMPTED, False, 1),  # journaled; the attempt's save lost; nothing sent
        (Stage.ATTEMPTED, True, 1),  # the attempt saved; nothing sent
        (Stage.OBSERVED, False, 1),  # the write landed; its observation's save lost
        (Stage.OBSERVED, True, 1),  # all saved; the phase's save lost
    ],
    ids=["before-attempt", "after-attempt", "after-write", "after-observed"],
)
def test_every_crash_window_completes_from_the_journal_with_one_write_and_no_fixer(
    tmp_state_dir, stage, saved, writes
):
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    run = eng.state.run_id
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, stage, saved=saved)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    landed = stage is Stage.OBSERVED
    assert gh.prs[PR].body == (_payload(run) if landed else _base(run))
    eng2 = _resumed(eng, tmp_state_dir, gh)
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert "completed from the persisted FIX plan; no fixer launched" in out.message
    assert eng2.provider.calls == []
    assert len(_body_writes(gh)) == writes
    assert gh.prs[PR].body == _payload(run)
    s = load_state(eng2.paths.state_file)
    assert s.effect_records == [] and s.last_fix_resolutions[0]["pr_body_update"]["changed"] is True


def test_a_mixed_fix_crashed_after_the_description_pushes_the_candidate_once(tmp_state_dir):
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir, gh, scripted(fixer(fixed("R1-F1"), pr_body_update("R1-F2"))), findings=2
    )
    reviewed = eng.state.reviewed_head_sha
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, Stage.OBSERVED, saved=True)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    assert eng.origin.head(BRANCH) == reviewed
    eng2 = _resumed(eng, tmp_state_dir, gh)
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert eng2.provider.calls == [] and eng.origin.head(BRANCH) != reviewed
    assert len(_body_writes(gh)) == 1


def test_a_second_invocation_after_the_phase_saved_sends_nothing(tmp_state_dir):
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    eng.step()
    assert load_state(eng.paths.state_file).phase is Phase.REVIEW
    head = eng.state.current_head_sha

    def clean(req):
        return block(review_result(2, head, []))

    eng2 = _resumed(eng, tmp_state_dir, gh, scripted(clean))
    assert eng2.step().next_phase == "READY_FOR_MERGE"
    assert len(_body_writes(gh)) == 1


# -- a description verified, then edited (PR #467 R1-F1) -----------------------------------------


def _mixed(tmp_state_dir, gh: FakeGitHub, *resolutions, findings: int = 2):
    resolutions = resolutions or (fixed("R1-F1"), pr_body_update("R1-F2"))
    return _updating(tmp_state_dir, gh, scripted(fixer(*resolutions)), findings=findings)


def _edit_once_saved(eng, gh: FakeGitHub, kind: EffectKind, body: str) -> None:
    """A human edits the description right after a ``kind`` record is saved as observed."""
    persist = eng._persist_effect

    def editing(record):
        persist(record)
        if record.kind is kind and record.stage is Stage.OBSERVED:
            gh.prs[PR].body = body

    eng._persist_effect = editing


def test_an_edit_after_the_observation_save_stops_a_resumed_mixed_fix_before_the_push(
    tmp_state_dir,
):
    """The description was verified and saved, the process died, a human edited it. The
    resume neither pushes nor records the fix nor writes over the edit; a reason does not
    move it; with the payload back the plan finishes, with no fixer and no second write."""
    gh = FakeGitHub()
    eng = _mixed(tmp_state_dir, gh)
    reviewed = eng.state.reviewed_head_sha
    run = eng.state.run_id
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, Stage.OBSERVED, saved=True)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    assert _body_writes(gh) == [_payload(run)] and eng.origin.head(BRANCH) == reviewed
    edited = _payload(run, A_HUMAN_EDIT)
    gh.prs[PR].body = edited

    eng2 = _resumed(eng, tmp_state_dir, gh)
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.EFFECT_CONFLICT
    assert eng.origin.head(BRANCH) == reviewed and gh.prs[PR].head_sha == reviewed
    assert _body_writes(gh) == [_payload(run)] and gh.prs[PR].body == edited
    s = load_state(eng2.paths.state_file)
    assert [f["id"] for f in s.open_findings] == ["R1-F1", "R1-F2"]
    assert s.last_fix_resolutions == [] and s.pr_body_outcomes == []
    assert s.effect_records, "the plan stays saved, with the candidate it pushes"
    record = _k12(eng2)
    assert record.stage is Stage.OBSERVED, "the journal stays true: the payload was verified"
    for needle in (
        "changed after the controller verified it",
        sha256_text(edited),
        sha256_text(_payload(run)),
        f"--decline-action {record.action_id}",
        f"--action {record.action_id}",
        "was not sent",
    ):
        assert needle in s.block_reason, needle

    refused = eng2.unblock("it looks fine to me")
    assert not refused.unblocked and "not evidence and the edit is not adopted" in refused.message
    refused = eng2.unblock("approve it", approve_action=record.action_id)
    assert not refused.unblocked and sha256_text(edited) in refused.message

    gh.prs[PR].body = _payload(run)
    done = eng2.unblock("the payload is back")
    assert done.unblocked and done.phase == "FIX", done.message
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert eng2.provider.calls == [] and _body_writes(gh) == [_payload(run)]
    assert eng.origin.head(BRANCH) == gh.prs[PR].head_sha != reviewed
    s = load_state(eng2.paths.state_file)
    assert [r["resolution"] for r in s.last_fix_resolutions] == ["fixed", "pr_body_update"]
    assert [o["outcome"] for o in s.pr_body_outcomes] == ["verified"]


def test_an_edit_after_a_description_only_observation_never_reaches_review(tmp_state_dir):
    """Nothing is left to send, so only the read before the fix is recorded can see it."""
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    run = eng.state.run_id
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, Stage.OBSERVED, saved=True)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    gh.prs[PR].body = _base(run)  # even the plan's own base is not its payload

    eng2 = _resumed(eng, tmp_state_dir, gh)
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.EFFECT_CONFLICT
    assert "the plan pushes nothing" in out.message
    assert _body_writes(gh) == [_payload(run)] and gh.prs[PR].body == _base(run)
    s = load_state(eng2.paths.state_file)
    assert s.last_fix_resolutions == [] and s.open_findings


def test_an_edit_between_the_description_and_a_follow_up_sends_neither_it_nor_the_push(
    tmp_state_dir,
):
    """In one process too: each later write of the plan reads the body first."""
    gh = FakeGitHub()
    eng = _mixed(
        tmp_state_dir,
        gh,
        fixed("R1-F1"),
        pr_body_update("R1-F2"),
        new_follow_up("R1-F3"),
        findings=3,
    )
    reviewed = eng.state.reviewed_head_sha
    edited = _payload(eng.state.run_id, A_HUMAN_EDIT)
    _edit_once_saved(eng, gh, EffectKind.PR_BODY_UPDATE, edited)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert _other_writes(gh) == [] and eng.origin.head(BRANCH) == reviewed
    assert len(_body_writes(gh)) == 1 and gh.prs[PR].body == edited


def test_an_edit_after_a_follow_up_went_out_cannot_be_declined_and_holds_the_push(tmp_state_dir):
    """A decline would abandon the issue already created, so the block offers none and the
    flag is refused. With the payload back the plan pushes, creating nothing twice."""
    gh = FakeGitHub()
    eng = _mixed(
        tmp_state_dir,
        gh,
        fixed("R1-F1"),
        pr_body_update("R1-F2"),
        new_follow_up("R1-F3"),
        findings=3,
    )
    reviewed = eng.state.reviewed_head_sha
    run = eng.state.run_id
    edited = _payload(run, A_HUMAN_EDIT)
    _edit_once_saved(eng, gh, EffectKind.FOLLOW_UP_ISSUE, edited)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert _other_writes(gh) == ["create_issue"] and eng.origin.head(BRANCH) == reviewed
    assert gh.prs[PR].body == edited
    reason = load_state(eng.paths.state_file).block_reason
    assert "--decline-action" not in reason and "can no longer be declined" in reason

    action = _k12(eng).action_id
    refused = eng.unblock(REASON)
    assert not refused.unblocked and "--decline-action" not in refused.message
    refused = eng.unblock(REASON, decline_action=[action])
    assert not refused.unblocked and "would abandon" in refused.message
    assert load_state(eng.paths.state_file).effect_records

    gh.prs[PR].body = _payload(run)
    assert eng.unblock("the payload is back").unblocked
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert _other_writes(gh) == ["create_issue"] and eng.origin.head(BRANCH) != reviewed
    assert _body_writes(gh) == [_payload(run)]


def test_an_edit_after_the_push_blocks_the_record_of_the_fix_and_pushes_nothing_more(tmp_state_dir):
    """The candidate is on the branch and cannot be taken back, and the fix still is not
    recorded over a description the plan did not write. Its head is the plan's own push,
    never drift, so nothing routes to REVIEW until the payload is back."""
    gh = FakeGitHub()
    eng = _mixed(tmp_state_dir, gh)
    reviewed = eng.state.reviewed_head_sha
    run = eng.state.run_id
    edited = _payload(run, A_HUMAN_EDIT)
    _edit_once_saved(eng, gh, EffectKind.PUSH, edited)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.EFFECT_CONFLICT
    candidate = eng.origin.head(BRANCH)
    assert candidate != reviewed and f"{candidate[:12]} is pushed" in out.message
    s = load_state(eng.paths.state_file)
    assert s.last_fix_resolutions == [] and s.open_findings and s.effect_records

    action = _k12(eng).action_id
    assert not eng.unblock(REASON).unblocked
    refused = eng.unblock(REASON, decline_action=[action])
    assert not refused.unblocked and "would abandon" in refused.message

    gh.prs[PR].body = _payload(run)
    done = eng.unblock("the payload is back")
    assert done.unblocked and done.phase == "FIX", done.message
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert eng.origin.head(BRANCH) == candidate and eng.provider.calls[1:] == []
    assert _body_writes(gh) == [_payload(run)]
    assert [r["resolution"] for r in load_state(eng.paths.state_file).last_fix_resolutions] == [
        "fixed",
        "pr_body_update",
    ]


def test_an_operator_who_keeps_an_edit_made_after_verification_declines_and_the_fixer_runs_over_it(
    tmp_state_dir,
):
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    run = eng.state.run_id
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, Stage.OBSERVED, saved=True)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    edited = _payload(run, A_HUMAN_EDIT)
    gh.prs[PR].body = edited
    prompts = []

    def afresh(req):
        prompts.append(req.prompt)
        return fixer(pr_body_update("R1-F1", "A better text."), commit=False)(req)

    eng2 = _resumed(eng, tmp_state_dir, gh, scripted(afresh))
    assert eng2.step().next_phase == "BLOCKED"
    action = _k12(eng2).action_id
    done = eng2.unblock("keep the edit", decline_action=[action])
    assert done.unblocked and done.phase == "FIX", done.message
    s = load_state(eng2.paths.state_file)
    assert s.effect_records == [] and s.pr_body_outcomes[-1]["outcome"] == "declined"
    assert s.pr_body_outcomes[-1]["reason"] == "keep the edit"
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert action in prompts[0] and "keep the edit" in prompts[0]
    assert A_HUMAN_EDIT in (read_context_of(prompts[0]) / "pull-request.md").read_text()
    assert _body_writes(gh) == [_payload(run), _payload(run, "A better text.")]
    [_, verified] = load_state(eng2.paths.state_file).pr_body_outcomes
    assert verified["outcome"] == "verified" and verified["base_sha256"] == sha256_text(edited)


def test_a_body_only_the_print_of_the_verified_payload_is_an_edit(tmp_state_dir):
    """The re-read is exact too: `gh` cannot tell a control character from the literal
    caret it prints for it, and the exact read can."""
    gh = FakeGitHub()
    gh.gh_sanitizes = True
    text = "A literal ^G in the description."
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1", text), commit=False)))
    run = eng.state.run_id
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, Stage.OBSERVED, saved=True)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    gh.prs[PR].body = _payload(run, text.replace("^G", "\x07"))
    eng2 = _resumed(eng, tmp_state_dir, gh)
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.EFFECT_CONFLICT


def test_a_re_read_that_cannot_decide_pushes_nothing(tmp_state_dir):
    """A conclusive failure blocks and a transient one propagates for 'resume': neither is
    read as the body still holding the payload, and the candidate stays unpushed."""
    gh = FakeGitHub()
    gh.gh_sanitizes = True
    text = "A literal ^G in the description."
    eng = _mixed(tmp_state_dir, gh, fixed("R1-F1"), pr_body_update("R1-F2", text))
    reviewed = eng.state.reviewed_head_sha
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, Stage.OBSERVED, saved=True)
    with pytest.raises(KeyboardInterrupt):
        eng.step()

    gh.exact_read_error = GitHubUnavailableError("gh: HTTP 502")
    eng2 = _resumed(eng, tmp_state_dir, gh)
    with pytest.raises(GitHubUnavailableError):
        eng2.step()
    assert eng.origin.head(BRANCH) == reviewed

    gh.exact_read_error = GitHubError("HTTP 403 on the exact read")
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.GITHUB_READ_FAILED
    assert eng.origin.head(BRANCH) == reviewed
    refused = eng2.unblock(REASON)
    assert not refused.unblocked and "could not be read" in refused.message

    gh.exact_read_error = None
    assert eng2.unblock(REASON).unblocked
    assert eng2.step().next_phase == "REVIEW"
    assert eng.origin.head(BRANCH) != reviewed and len(_body_writes(gh)) == 1


def test_a_plain_unblock_over_an_edit_on_a_moved_pr_routes_the_drift_to_review(tmp_state_dir):
    """A push the plan did not make: the findings no longer describe the PR, so the edit
    is no reason to hold it. Any flag is refused as stale, and nothing is written."""
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    run = eng.state.run_id
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, Stage.OBSERVED, saved=True)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    edited = _payload(run, A_HUMAN_EDIT)
    gh.prs[PR].body = edited
    eng2 = _resumed(eng, tmp_state_dir, gh)
    assert eng2.step().next_phase == "BLOCKED"
    action = _k12(eng2).action_id
    gh.prs[PR].head_sha = "d" * 40
    refused = eng2.unblock(REASON, decline_action=[action])
    assert not refused.unblocked and "refused as stale" in refused.message
    done = eng2.unblock(REASON)
    assert done.unblocked and done.phase == "FIX", done.message
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert _body_writes(gh) == [_payload(run)] and gh.prs[PR].body == edited
    s = load_state(eng2.paths.state_file)
    assert s.last_fix_resolutions == [] and s.effect_records == []


def test_a_dry_run_and_status_say_a_verified_update_is_read_again(tmp_state_dir):
    gh = FakeGitHub()
    eng = _mixed(tmp_state_dir, gh)
    _power_loss_at(eng, EffectKind.PR_BODY_UPDATE, Stage.OBSERVED, saved=True)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    eng2 = _resumed(eng, tmp_state_dir, gh)
    action = _k12(eng2).action_id
    notes = "\n".join(eng2.step(dry_run=True).plan.notes)
    assert f"PR body update {action} is verified: would read the PR's exact body again" in notes
    state = load_state(eng2.paths.state_file)
    assert "read again before FIX completes" in "\n".join(cli._render_pr_body_updates(state, eng2))


# -- what the fixer was shown of the description (PR #467 R1-F2) ----------------------------------


def _at_the_launch(eng, gh: FakeGitHub, change) -> None:
    """``change(gh)`` holds while the launch's read context is read, and only then."""
    collect = eng._collect_read_context

    def collecting(phase):
        undo = change(gh)
        try:
            return collect(phase)
        finally:
            if undo is not None:
                undo()

    eng._collect_read_context = collecting


def _failing_pr_read(error: GitHubError):
    def change(gh: FakeGitHub):
        gh.get_pr_error = error

        def undo():
            gh.get_pr_error = None

        return undo

    return change


def _failing_comment_read(gh: FakeGitHub):
    gh.comments_error = GitHubError("HTTP 500 on the comments")

    def undo():
        gh.comments_error = None

    return undo


def _edited_before_the_launch(gh: FakeGitHub):
    gh.prs[PR].body = gh.prs[PR].body.replace(PREFACE, A_HUMAN_EDIT)


A_CREDENTIAL = "ghp_" + "a1B2" * 9

_NOT_SHOWN = {
    "cut-by-the-body-bound": {
        "preface": "x" * MAX_QUOTED_BODY_CHARS,
        "needle": f"the read context quotes {MAX_QUOTED_BODY_CHARS}",
    },
    "redacted": {
        "preface": f"The token {A_CREDENTIAL} was pasted here.",
        "needle": "the read context redacts part of it",
    },
    "gh-rewrites-a-control-character": {
        "preface": "With \x07 inside.",
        "sanitizes": True,
        "needle": "a control character `gh` does not print",
    },
    "pr-read-failed": {
        "launch": _failing_pr_read(GitHubError("HTTP 500 on the PR")),
        "needle": "could not be read for this launch",
    },
    "pr-read-unavailable": {
        "launch": _failing_pr_read(GitHubUnavailableError("gh: HTTP 502")),
        "needle": "could not be read for this launch",
    },
    "comment-read-failed": {
        "launch": _failing_comment_read,
        "needle": "could not be read for this launch",
    },
    "edited-since-the-entry": {
        "launch": _edited_before_the_launch,
        "needle": "it was edited since",
    },
}


@pytest.mark.parametrize("policy", ["auto", "approve"])
@pytest.mark.parametrize("name", list(_NOT_SHOWN))
def test_a_description_the_fixer_could_not_read_whole_is_never_replaced(
    tmp_state_dir, name, policy
):
    """The digest of an unseen body authorizes nothing. The prompt says the resolution is
    not available and why, a request is a correction naming the reason, nothing is held or
    planned, and the fixer can still resolve the finding another way."""
    case = _NOT_SHOWN[name]
    gh = FakeGitHub()
    gh.gh_sanitizes = case.get("sanitizes", False)
    prompts = []

    def recording(step):
        def handler(req):
            prompts.append(req.prompt)
            return step(req)

        return handler

    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(
            recording(fixer(pr_body_update("R1-F1"), commit=False)),
            recording(fixer(no_change("R1-F1"), commit=False)),
        ),
        policy=policy,
        body=lambda r: _base(r, case.get("preface", PREFACE)),
    )
    if "launch" in case:
        _at_the_launch(eng, gh, case["launch"])
    run = eng.state.run_id
    before = _base(run, case.get("preface", PREFACE))
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert _body_writes(gh) == []
    if name == "edited-since-the-entry":
        before = _base(run, A_HUMAN_EDIT)
    assert gh.prs[PR].body == before

    first, second = prompts
    assert "not available in this FIX entry" in first and case["needle"] in first
    assert "This run authorizes it by policy" not in first
    assert "could not show you the whole description" in second and case["needle"] in second
    s = load_state(eng.paths.state_file)
    assert s.pr_body_outcomes == []
    assert [r["resolution"] for r in s.last_fix_resolutions] == ["no_change_with_rationale"]
    assert A_CREDENTIAL not in first + second


def test_a_description_a_bound_on_the_files_cut_is_not_offered(tmp_state_dir, monkeypatch):
    """Not the body's own bound: the file's, which the renderer does not know of."""
    monkeypatch.setattr("autoforge.read_context.MAX_FILE_CHARS", 200)
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(
            fixer(pr_body_update("R1-F1"), commit=False),
            fixer(no_change("R1-F1"), commit=False),
        ),
    )
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert _body_writes(gh) == []
    first = eng.provider.calls[0].prompt
    assert "a bound on the read context's files cut it" in first
    assert "[AutoForge omitted" in (read_context_of(first) / "pull-request.md").read_text()


def test_a_description_at_exactly_the_quoted_bound_is_offered_and_replaced(tmp_state_dir):
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    run = eng.state.run_id
    preface = "x" * (MAX_QUOTED_BODY_CHARS - len(_base(run, "")))
    gh.prs[PR].body = _base(run, preface)
    assert len(gh.prs[PR].body) == MAX_QUOTED_BODY_CHARS
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert _body_writes(gh) == [_payload(run)]
    view = (read_context_of(eng.provider.calls[0].prompt) / "pull-request.md").read_text()
    assert "[AutoForge omitted" not in view and preface in view


def test_a_relaunch_decides_again_on_what_it_shows(tmp_state_dir):
    """Each launch has its own view: a read that failed for the first offers nothing to it
    and does not count against the next, which reads the body whole."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(
            fixer(pr_body_update("R1-F1"), commit=False),
            fixer(pr_body_update("R1-F1"), commit=False),
        ),
    )
    fails = [_failing_pr_read(GitHubError("HTTP 500 on the PR"))]
    _at_the_launch(eng, gh, lambda g: fails.pop()(g) if fails else None)
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    first, second = (c.prompt for c in eng.provider.calls)
    assert "not available in this FIX entry" in first
    assert "not available in this FIX entry" not in second
    assert _body_writes(gh) == [_payload(eng.state.run_id)]


# -- Markdown the text leaves open (PR #467 R1-F3) ------------------------------------------------


_LEFT_OPEN = {
    "backtick-fence": "A description.\n\n```python\nprint('never closed')",
    "tilde-fence": "A description.\n\n~~~\nnever closed",
    "html-comment": "A description.\n\n<!-- never closed",
    "pre": "A description.\n\n<pre>\nnever closed",
    "script": "A description.\n\n<script>\nnever closed",
    "style": "A description.\n\n<style>\nnever closed",
    "textarea": "A description.\n\n<textarea>\nnever closed",
    "details": "A description.\n\n<details>\n<summary>More</summary>\n\nnever closed",
    "table": "A description.\n\n<table>\n<tr><td>never closed",
    "cdata": "A description.\n\n<![CDATA[ never closed",
    "processing-instruction": "A description.\n\n<?php never closed",
    "declaration": "A description.\n\n<!DOCTYPE never closed",
    "only-a-fence": "```",
}


@pytest.mark.parametrize("name", list(_LEFT_OPEN))
def test_markdown_the_text_leaves_open_cannot_reach_the_controllers_block(tmp_state_dir, name):
    """The full path: the fixer's text is accepted, planned, journaled, written and read
    back, and every byte before it is the controller's. CommonMark reads blocks forward,
    so each block is closed before the text starts, whatever the text leaves open."""
    text = _LEFT_OPEN[name]
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1", text), commit=False)))
    run = eng.state.run_id
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    block_ = implementation_closing_block(ISSUE, run)
    assert gh.prs[PR].body == f"{block_}\n\n{text}"
    assert block_ == f"Closes #2\n\n{_marker(run)}"
    assert _body_writes(gh) == [gh.prs[PR].body]
    assert protected_pr_body_block(gh.prs[PR].body, ISSUE, run) == (block_, "")


def test_a_later_marker_stays_in_the_block_above_text_left_open(tmp_state_dir):
    """Every controller marker the base holds keeps ahead of the text, in its order."""
    other = '<!-- ai-replacement: {"txn": "a  b"} -->'
    gh = FakeGitHub()
    text = "```\nnever closed"
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(fixer(pr_body_update("R1-F1", text), commit=False)),
        body=lambda r: f"{_base(r)}\n{other}\n",
    )
    run = eng.state.run_id
    assert eng.step().next_phase == "REVIEW"
    assert gh.prs[PR].body == f"Closes #2\n\n{_marker(run)}\n\n{other}\n\n{text}"


def test_a_body_whose_old_text_swallowed_the_block_can_be_replaced_and_the_next_round_too(
    tmp_state_dir,
):
    """A body as ANALYZE_EXECUTE composes it, its block already inside the text's fence,
    still qualifies by its bytes and comes out with the block in front. The result is a
    base like any other: the next round's update of it composes the same way."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(
            fixer(pr_body_update("R1-F1", "```\nopen"), commit=False),
            fixer(pr_body_update("R2-F1"), commit=False),
        ),
        body=lambda r: _base(r, "```\nopen before"),
    )
    run = eng.state.run_id
    assert eng.step().next_phase == "REVIEW"
    assert gh.prs[PR].body == _payload(run, "```\nopen")

    eng.state.phase = Phase.FIX
    eng.state.review_round = 2
    eng.state.last_review_result = "needs_fix"
    eng.state.last_review_comment_url = comment_url(PR, 101)
    eng.state.open_findings = [_finding(1, 2)]
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert gh.prs[PR].body == _payload(run)
    assert _body_writes(gh) == [_payload(run, "```\nopen"), _payload(run)]


# -- policy and approval ------------------------------------------------------------------------


def _held(tmp_state_dir, gh: FakeGitHub, *resolutions, findings: int = 1, commit: bool = False):
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(fixer(*(resolutions or (pr_body_update("R1-F1"),)), commit=commit)),
        policy="approve",
        findings=findings,
    )
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.ACCEPTANCE_ACTION
    return eng


def test_policy_off_refuses_the_resolution_and_the_prompt_says_so(tmp_state_dir):
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(
            fixer(pr_body_update("R1-F1"), commit=False),
            fixer(no_change("R1-F1"), commit=False),
        ),
        policy="off",
    )
    assert eng.step().next_phase == "REVIEW"
    assert _body_writes(gh) == []
    [first, second] = eng.provider.calls
    assert "safety.pr_body_updates: off" in first.prompt
    assert "which this run does not offer" in second.prompt


def test_approve_holds_with_the_action_the_pr_the_finding_the_digest_and_the_remedy(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    run = eng.state.run_id
    s = load_state(eng.paths.state_file)
    record = _k12(eng)
    assert not record.authorized and _body_writes(gh) == []
    reason = s.block_reason
    for needle in (
        record.action_id,
        PR,
        "R1-F1",
        FINDING["title"],
        sha256_text(_base(run)),
        sha256_text(_payload(run)),
        "--approve-action",
        "--decline-action",
        f"status --action {record.action_id}",
    ):
        assert needle in reason, needle
    assert PR_BODY_TEXT not in reason


def test_approval_writes_exactly_the_payload_with_no_fixer_and_is_recorded(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    run = eng.state.run_id
    action = _k12(eng).action_id
    refused = eng.unblock(REASON)
    assert not refused.unblocked and "an unblock reason is not evidence" in refused.message
    wrong = eng.unblock(REASON, approve_action="B-" + "0" * 16)
    assert not wrong.unblocked and "names no PR body update" in wrong.message
    assert _body_writes(gh) == []

    done = eng.unblock(REASON, approve_action=action)
    assert done.unblocked and done.phase == "FIX", done.message
    assert _body_writes(gh) == []
    s = load_state(eng.paths.state_file)
    assert s.unblock_history[-1]["action"] == f"approved {action}"
    calls = len(eng.provider.calls)
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert len(eng.provider.calls) == calls
    assert _body_writes(gh) == [_payload(run)]


def test_a_configuration_change_never_widens_a_persisted_authorization(tmp_state_dir):
    """Planned under `approve`, the setting then moved to `auto`: the plan still waits."""
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    action = _k12(eng).action_id
    eng2 = _resumed(eng, tmp_state_dir, gh, policy="auto")
    assert eng2.unblock(REASON).unblocked is False
    assert _body_writes(gh) == []
    assert _k12(eng2).precondition["authorization"] == "operator"
    assert _k12(eng2).action_id == action


def test_an_approval_is_refused_when_the_setting_is_off(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    action = _k12(eng).action_id
    eng2 = _resumed(eng, tmp_state_dir, gh, policy="off")
    refused = eng2.unblock(REASON, approve_action=action)
    assert not refused.unblocked and "cannot be approved while safety.pr_body_updates is off" in (
        refused.message
    )
    assert _body_writes(gh) == []


def test_an_authorized_update_needs_no_approval(tmp_state_dir):
    gh = FakeGitHub()
    eng = _journaled(tmp_state_dir, gh)
    gh.prs[PR].body = _base(eng.state.run_id, A_HUMAN_EDIT)
    eng2 = _resumed(eng, tmp_state_dir, gh)
    assert eng2.step().next_phase == "BLOCKED"
    refused = eng2.unblock(REASON, approve_action=_k12(eng2).action_id)
    assert not refused.unblocked and "needs no approval" in refused.message


def test_an_operator_who_sets_the_exact_payload_is_adopted_without_a_write(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    run = eng.state.run_id
    gh.prs[PR].body = _payload(run)
    done = eng.unblock("I pasted it")
    assert done.unblocked and done.phase == "FIX", done.message
    assert eng.step().next_phase == "REVIEW"
    assert _body_writes(gh) == []
    assert load_state(eng.paths.state_file).pr_body_outcomes[0]["outcome"] == "verified"


def test_a_decline_drops_the_plan_and_the_same_text_is_refused_for_the_round(tmp_state_dir):
    gh = FakeGitHub()
    prompts = []

    def second(req):
        prompts.append(req.prompt)
        return fixer(pr_body_update("R1-F1"), commit=False)(req)

    def third(req):
        prompts.append(req.prompt)
        return fixer(pr_body_update("R1-F1", "A revised description."), commit=False)(req)

    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(fixer(pr_body_update("R1-F1"), commit=False), second, third),
        policy="approve",
    )
    assert eng.step().next_phase == "BLOCKED"
    action = _k12(eng).action_id
    done = eng.unblock("it omits the risks", decline_action=[action])
    assert done.unblocked and done.phase == "FIX", done.message
    s = load_state(eng.paths.state_file)
    assert s.effect_records == [] and s.pr_body_outcomes[-1]["outcome"] == "declined"
    assert s.pr_body_outcomes[-1]["reason"] == "it omits the risks"
    assert eng.step().next_phase == "BLOCKED"
    assert action in prompts[0] and "it omits the risks" in prompts[0]
    assert f"action {action}, which the operator declined" in prompts[1]
    assert _body_writes(gh) == []


def test_the_declines_of_a_round_are_bounded(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    action = _k12(eng).action_id
    state = eng.state
    state.pr_body_outcomes = [
        {
            "action_id": "B-" + f"{n:016x}",
            "round": 1,
            "finding_ids": ["R1-F1"],
            "outcome": "declined",
            "at": "2026-11-06T00:00:00Z",
            "base_sha256": "a" * 64,
            "body_sha256": "b" * 64,
            "changed": True,
            "reason": "no",
        }
        for n in range(MAX_DECLINES_PER_ROUND)
    ]
    eng._save()
    refused = eng.unblock(REASON, decline_action=[action])
    assert (
        not refused.unblocked and "the most the controller keeps for one round" in refused.message
    )


def test_a_held_mixed_fix_keeps_its_candidate_in_the_worktree_and_says_so(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh, fixed("R1-F1"), pr_body_update("R1-F2"), findings=2, commit=True)
    reviewed = eng.state.reviewed_head_sha
    assert eng.origin.head(BRANCH) == reviewed
    reason = load_state(eng.paths.state_file).block_reason
    assert "held back until the body is verified" in reason
    assert "unpushed candidate lives only in the agent worktree" in reason
    done = eng.unblock(REASON, approve_action=_k12(eng).action_id)
    assert done.unblocked, done.message
    assert eng.step().next_phase == "REVIEW"
    assert eng.origin.head(BRANCH) != reviewed


def test_a_moved_pr_makes_a_plain_unblock_route_to_review_with_the_update_stale(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    gh.prs[PR].base_ref = "release"
    done = eng.unblock(REASON)
    assert done.unblocked, done.message
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    s = load_state(eng.paths.state_file)
    assert s.pr_body_outcomes[-1]["outcome"] == "stale" and _body_writes(gh) == []


# -- dry-run, status, redaction -------------------------------------------------------------------


def test_a_dry_run_with_a_saved_plan_names_the_update_and_writes_nothing(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    eng.unblock(REASON, approve_action=_k12(eng).action_id)
    before = eng.paths.state_file.read_bytes()
    writes = list(gh.effect_writes)
    out = eng.step(dry_run=True)
    notes = "\n".join(out.plan.notes)
    assert f"PR body update {_k12(eng).action_id} is authorized" in notes
    assert sha256_text(_base(eng.state.run_id)) in notes
    assert gh.effect_writes == writes and eng.paths.state_file.read_bytes() == before


def test_a_dry_run_unblock_writes_nothing(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    before = eng.paths.state_file.read_bytes()
    preview = eng.unblock(REASON, approve_action=_k12(eng).action_id, dry_run=True)
    assert preview.dry_run and eng.paths.state_file.read_bytes() == before
    assert _body_writes(gh) == []


def test_status_names_the_pending_action_and_prints_exactly_its_payload(tmp_state_dir, capsys):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh, fixed("R1-F1"), pr_body_update("R1-F2"), findings=2, commit=True)
    run = eng.state.run_id
    state = load_state(eng.paths.state_file)
    record = _k12(eng)
    block = "\n".join(cli._render_pr_body_updates(state, eng))
    lines = _status_text(state, eng)
    for needle in (
        f"PR body update {record.action_id}",
        PR,
        "R1-F2",
        FINDING["title"],
        f"expected body: sha256 {sha256_text(_base(run))}",
        sha256_text(_payload(run)),
        "held back",
        "awaiting approval",
        f"next:          {cli.SEE_RECOVERY}",
    ):
        assert needle in block, needle
    # The commands are the Recovery block's alone (PR #470 R4-F1).
    assert eng.cli_command("unblock") not in block
    for flag in ("--approve-action", "--decline-action"):
        assert eng.cli_command("unblock", flag, record.action_id, "--reason") in lines, flag
    [entry] = cli._pr_body_updates(state)
    assert entry["state"] == "awaiting_approval" and entry["finding_ids"] == ["R1-F2"]
    assert entry["pr"] == PR and entry["expected_body_sha256"] == sha256_text(_base(run))
    assert cli._print_action_payload(state, record.action_id) == 0
    assert capsys.readouterr().out == _payload(run)
    assert cli._print_action_payload(state, "B-" + "0" * 16) == 1


def test_status_lists_a_finished_update_with_its_outcome(tmp_state_dir):
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    eng.step()
    state = load_state(eng.paths.state_file)
    [entry] = cli._pr_body_updates(state)
    assert entry["state"] == "verified" and entry["changed"] is True
    assert "state:         verified" in "\n".join(cli._render_pr_body_updates(state, eng))


def test_the_body_never_reaches_the_run_log_or_a_block_reason(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    state_dir = eng.paths.state_file.parent
    for path in (state_dir / "logs").rglob("*.log"):
        assert PR_BODY_TEXT not in path.read_text(encoding="utf-8", errors="replace")
    assert PR_BODY_TEXT not in load_state(eng.paths.state_file).block_reason


def test_a_decline_reason_with_a_credential_is_stored_redacted(tmp_state_dir):
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(fixer(pr_body_update("R1-F1"), commit=False), "never"),
        policy="approve",
    )
    eng.step()
    token = "ghp_" + "a" * 36
    eng.unblock(f"it leaks {token}", decline_action=[_k12(eng).action_id])
    raw = eng.paths.state_file.read_text(encoding="utf-8")
    assert token not in raw


# -- the configuration --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "policy"),
    [('"auto"', "auto"), ('"approve"', "approve"), ('"off"', "off"), ("false", "off")],
)
def test_the_setting_reads_each_policy(tmp_path, raw, policy):
    p = tmp_path / "cfg.json"
    p.write_text(f'{{"version": 1, "safety": {{"pr_body_updates": {raw}}}}}', encoding="utf-8")
    assert load_config_file(str(p)).safety.pr_body_updates == policy


@pytest.mark.parametrize("raw", ["true", '"on"', "1", "null", '"AUTO"'])
def test_the_setting_refuses_anything_else(tmp_path, raw):
    p = tmp_path / "cfg.json"
    p.write_text(f'{{"version": 1, "safety": {{"pr_body_updates": {raw}}}}}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="pr_body_updates"):
        load_config_file(str(p))


def test_the_default_is_the_narrow_automation_the_adr_reasons(tmp_path):
    assert default_config().safety.pr_body_updates == "auto"


# -- the decision -------------------------------------------------------------------------------


def test_the_decision_table(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    run = eng.state.run_id
    held = _k12(eng)
    approved = held.approving()
    payload, base = _payload(run), _base(run)
    assert decide_pr_body_update(held, payload).action is Action.OBSERVE
    assert decide_pr_body_update(held, base).action is Action.HOLD
    assert decide_pr_body_update(approved, base).action is Action.ISSUE
    assert decide_pr_body_update(approved, "other").action is Action.CONFLICT
    assert "changed outside the plan" in decide_pr_body_update(approved, "other").reason
    moved = decide_pr_body_update(approved, base, problem="PR HEAD moved")
    assert moved.action is Action.CONFLICT and moved.reason.endswith(": PR HEAD moved")
    written = decide_pr_body_update(approved, payload, problem="PR HEAD moved")
    assert written.action is Action.CONFLICT and "the body is written, but" in written.reason
    assert decide_pr_body_update(held, payload).observed == {
        "url": PR,
        "body_sha256": sha256_text(payload),
    }


def test_the_observed_body_is_the_exact_payload_and_not_a_trailing_newline_off(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    approved = _k12(eng).approving()
    found = decide_pr_body_update(approved, _payload(eng.state.run_id) + "\n")
    assert found.action is Action.CONFLICT


def test_saved_state_carries_the_body_only_inside_the_record(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    [record] = data["effect_records"]
    assert record["kind"] == "pr_body_update"
    assert record["payload"]["body"] == _payload(eng.state.run_id)
    assert record["target"] == {"pr_url": PR}
    assert record["identity"]["pr_url"] == PR


# -- a persisted plan that does not hold together -----------------------------------------------


def _saved(tmp_state_dir, change, **kw) -> object:
    """A held plan's state file with ``change`` applied, under another name."""
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh, **kw)
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    change(data, eng.state.run_id)
    path = eng.paths.state_file.with_name("tampered.json")
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _in_record(key: str, field: str, value) -> object:
    def change(data, run):
        data["effect_records"][0][key][field] = value

    return change


def _body(make) -> object:
    def change(data, run):
        data["effect_records"][0]["payload"]["body"] = make(run)

    return change


def _source(value) -> object:
    def change(data, run):
        data["completion_context"]["resolutions"][0]["pr_body_source"] = value

    return change


def _entry_digest(value) -> object:
    def change(data, run):
        data["entry_observation"]["pr_body_sha256"] = value

    return change


def _no_entry_digest(data, run):
    del data["entry_observation"]["pr_body_sha256"]


def _second_record(data, run):
    data["effect_records"].append({**data["effect_records"][0], "position": 1})


_TAMPERED = {
    "marker-dropped": (_body(lambda r: "Closes #2\n\nText"), "closing block"),
    "closing-dropped": (_body(lambda r: f"{_marker(r)}\n\nText"), "closing block"),
    "text-before-the-block": (
        _body(lambda r: f"Text\n\nCloses #2\n\n{_marker(r)}"),
        "does not open with the controller's closing block",
    ),
    "no-text": (_body(lambda r: f"Closes #2\n\n{_marker(r)}"), "no replacement text"),
    "text-glued-to-the-block": (
        _body(lambda r: f"Closes #2\n\n{_marker(r)}Text"),
        "no replacement text",
    ),
    "unclosed-kept-marker": (
        _body(lambda r: f"Closes #2\n\n{_marker(r)}\n\n<!-- ai-unknown\n\nText"),
        "never closes",
    ),
    "marker-in-the-text": (
        _body(lambda r: f"Closes #2\n\n{_marker(r)}\n\nText {_marker('af-other-1')}"),
        "invalid replacement text",
    ),
    "closing-reference-in-the-text": (
        _body(lambda r: f"Closes #2\n\n{_marker(r)}\n\nCloses #9"),
        "invalid replacement text",
    ),
    "text-not-stored-form": (
        _body(lambda r: f"Closes #2\n\n{_marker(r)}\n\n  Text  "),
        "stored form",
    ),
    "another-target": (
        _in_record("target", "pr_url", "https://github.com/owner/repo/pull/43"),
        "differs between its identity and its target",
    ),
    "another-identity": (
        _in_record("identity", "pr_url", "https://github.com/owner/repo/pull/43"),
        "differs between its identity and its target",
    ),
    "marker-of-another-run": (
        _in_record("identity", "marker", render_implementation_marker(ISSUE, "af-other-1")),
        "does not name its owner's run",
    ),
    "marker-of-another-issue": (
        _in_record("identity", "marker", render_implementation_marker(OTHER_ISSUE, "af-other-1")),
        "for another issue",
    ),
    "base-not-a-digest": (_in_record("precondition", "base_sha256", "abc"), "base_sha256"),
    "base-not-the-entry": (
        _in_record("precondition", "base_sha256", "a" * 64),
        "another PR body than the one the FIX entry read",
    ),
    "head-not-a-sha": (_in_record("precondition", "reviewed_head_sha", "abc"), "reviewed_head_sha"),
    "merge-base-not-a-sha": (
        _in_record("precondition", "reviewed_merge_base_sha", "abc"),
        "reviewed_merge_base_sha",
    ),
    "base-ref-empty": (_in_record("precondition", "reviewed_base_ref", ""), "reviewed_base_ref"),
    "policy-without-an-approval": (
        _in_record("precondition", "authorization", "policy"),
        "authorized by policy but records no approved action id",
    ),
    "approval-of-another-action": (
        _in_record("precondition", "approved_action_id", "B-" + "0" * 16),
        "is not the record's action id",
    ),
    "named-by-no-resolution": (_source(None), "is set exactly when"),
    "source-past-the-plan": (_source(5), "names plan position 5"),
    "entry-digest-of-another-body": (
        _entry_digest("b" * 64),
        "another PR body than the one the FIX entry read",
    ),
    "entry-digest-malformed": (_entry_digest("xyz"), "pr_body_sha256"),
    "no-entry-digest": (_no_entry_digest, "another PR body than the one the FIX entry read"),
    "two-records": (_second_record, "more than one PR body update"),
}


@pytest.mark.parametrize("name", list(_TAMPERED))
def test_a_saved_plan_that_does_not_hold_together_is_refused_at_load(tmp_state_dir, name):
    change, needle = _TAMPERED[name]
    path = _saved(tmp_state_dir, change)
    with pytest.raises(StateError, match=re.escape(needle)):
        load_state(path)


def test_a_saved_plan_of_a_mixed_fix_loads_with_each_resolutions_source(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh, fixed("R1-F1"), pr_body_update("R1-F2"), findings=2, commit=True)
    s = load_state(eng.paths.state_file)
    assert [r["pr_body_source"] for r in s.phase_effects().context.resolutions] == [None, 0]
    assert s.phase_effects().observation.pr_body_sha256 == sha256_text(_base(eng.state.run_id))


@pytest.mark.parametrize("label", ["14", "13", "12", "8", "1"])
def test_an_older_label_never_carries_protocol_15s_state(tmp_state_dir, label):
    """No older controller wrote it, so a file carrying it is a hand edit or foreign."""
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    data["protocol_version"] = label
    path = eng.paths.state_file.with_name("older.json")
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(StateError, match="never wrote"):
        load_state(path)
    assert path.read_bytes() == before


def test_a_current_file_missing_the_field_is_refused(tmp_state_dir):
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    eng.step()
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    del data["pr_body_outcomes"]
    path = eng.paths.state_file.with_name("missing.json")
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="pr_body_outcomes"):
        load_state(path)


def test_a_protocol_14_fix_entry_loads_and_offers_nothing(tmp_state_dir):
    """A FIX entered under protocol 14 recorded no digest: the fixer launched from that
    entry is told the resolution is unavailable, and asking anyway is a correction."""
    gh = FakeGitHub()

    def dying(req):
        raise KeyboardInterrupt

    eng = _updating(tmp_state_dir, gh, scripted(dying))
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    assert data["entry_observation"]["pr_body_sha256"]
    del data["entry_observation"]["pr_body_sha256"]
    del data["pr_body_outcomes"], data["stop_code"]
    data["protocol_version"] = "14"
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")

    eng2 = _resumed(
        eng,
        tmp_state_dir,
        gh,
        scripted(
            fixer(pr_body_update("R1-F1"), commit=False),
            fixer(no_change("R1-F1"), commit=False),
        ),
    )
    assert eng2.state.protocol_version == "16" and eng2.state.pr_body_outcomes == []
    out = eng2.step()
    assert out.next_phase == "REVIEW", out.message
    assert _body_writes(gh) == []
    [first, second] = eng2.provider.calls
    assert "not available in this FIX entry" in first.prompt
    assert "which this FIX entry does not offer" in second.prompt


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"action_id": "A-" + "0" * 16}, "B-<16 hex>"),
        ({"round": 0}, "round"),
        ({"finding_ids": []}, "finding_ids"),
        ({"finding_ids": ["R9-F1"]}, "of its round"),
        ({"outcome": "maybe"}, "outcome"),
        ({"base_sha256": "x"}, "base_sha256"),
        ({"body_sha256": "x"}, "body_sha256"),
        ({"changed": False}, "changed"),
        ({"reason": "x" * 5000}, "reason"),
        ({"extra": 1}, "must hold exactly"),
    ],
)
def test_a_malformed_outcome_is_refused(tmp_state_dir, change, needle):
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    eng.step()
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    data["pr_body_outcomes"][0].update(change)
    path = eng.paths.state_file.with_name("bad.json")
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match=re.escape(needle)):
        load_state(path)


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"action_id": "x"}, "B-<16 hex>"),
        ({"pr_url": "https://github.com/owner/repo/pull/43"}, "the current PR"),
        ({"base_sha256": "x"}, "base_sha256"),
        ({"body_sha256": "x"}, "body_sha256"),
        ({"changed": False}, "changed"),
        ({"reviewed_head_sha": "x"}, "reviewed_head_sha"),
        ({"extra": 1}, "must hold exactly"),
    ],
)
def test_a_malformed_receipt_is_refused(tmp_state_dir, change, needle):
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    eng.step()
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    data["last_fix_resolutions"][0]["pr_body_update"].update(change)
    path = eng.paths.state_file.with_name("bad.json")
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match=re.escape(needle)):
        load_state(path)


def test_a_receipt_only_on_a_pr_body_update_resolution(tmp_state_dir):
    gh = FakeGitHub()
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)))
    eng.step()
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    data["last_fix_resolutions"][0]["pr_body_update"] = None
    path = eng.paths.state_file.with_name("bad.json")
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="set exactly when"):
        load_state(path)


# -- beside the other effects of a plan -----------------------------------------------------------


def _with_acceptance(tmp_state_dir, gh: FakeGitHub, *resolutions, policy: str = "auto", **kw):
    """A run whose ``safety.acceptance_actions`` holds a K9 for approval."""
    eng = _updating(tmp_state_dir, gh, scripted(*resolutions), policy=policy, **kw)
    eng.config.safety = replace(eng.config.safety, acceptance_actions="approve")
    return eng


def test_a_held_acceptance_comment_holds_the_description_too_and_then_both_land(tmp_state_dir):
    gh = FakeGitHub()
    eng = _with_acceptance(
        tmp_state_dir,
        gh,
        fixer(acceptance("R1-F1"), pr_body_update("R1-F2"), commit=False),
        findings=2,
    )
    run = eng.state.run_id
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.ACCEPTANCE_ACTION
    assert gh.effect_writes == []
    s = load_state(eng.paths.state_file)
    assert "the replacement of the PR description is held back as well" in s.block_reason
    kinds = [r["kind"] for r in s.effect_records]
    assert kinds == ["acceptance_comment", "pr_body_update"]
    action = s.effect_records and EffectRecord.from_dict(s.effect_records[0]).action_id
    done = eng.unblock(REASON, approve_action=action)
    assert done.unblocked, done.message
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert [w[0] for w in gh.effect_writes] == ["create_issue_comment", "write_pr_body"]
    assert gh.prs[PR].body == _payload(run)
    assert len(eng.provider.calls) == 1


def test_a_decline_of_the_description_keeps_a_verified_comment_and_runs_the_fixer_again(
    tmp_state_dir,
):
    """K9 is on GitHub and K12 is declined: the comment's outcome is recorded as verified,
    the plan is dropped, and the fixer runs again with the decline in its prompt."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(
            fixer(acceptance("R1-F1"), pr_body_update("R1-F2"), commit=False),
            fixer(no_change("R1-F1"), no_change("R1-F2"), commit=False),
        ),
        policy="approve",
        findings=2,
    )
    eng.config.safety = replace(eng.config.safety, acceptance_actions="auto")
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert [w[0] for w in gh.effect_writes] == ["create_issue_comment"]
    action = _k12(eng).action_id
    done = eng.unblock("not this text", decline_action=[action])
    assert done.unblocked, done.message
    s = load_state(eng.paths.state_file)
    assert s.effect_records == []
    assert [o["outcome"] for o in s.acceptance_outcomes] == ["verified"]
    assert [o["outcome"] for o in s.pr_body_outcomes] == ["declined"]
    assert s.pr_body_outcomes[0]["finding_ids"] == ["R1-F2"]
    assert eng.step().next_phase == "REVIEW"
    assert action in eng.provider.calls[1].prompt
    assert _body_writes(gh) == []


# -- a decline of the description waits for what the plan orders before it (PR #467 R2-F1) --------


NOTE_TARGET = "https://github.com/owner/repo/issues/16"
NOTE = "Coordination note for #16: the description now says what shipped."
NOTE_RATIONALE = "The issue asks for a coordination note on #16, which only the operator can post."
SECOND_FIXER = fixer(no_change("R1-F1"), no_change("R1-F2"), commit=False)


def _note_request(fid: str = "R1-F1") -> dict:
    return {
        "finding_id": fid,
        "resolution": "operator_acceptance",
        "rationale": NOTE_RATIONALE,
        "operator_acceptance": {
            "actions": [{"kind": "related_issue_comment", "issue": 16, "body": NOTE}]
        },
    }


def _note_payload() -> str:
    return operator_comment_body(NOTE, ISSUE, PR, 1, "R1-F1", NOTE_TARGET)


def _with_note(tmp_state_dir, gh: FakeGitHub, first=None):
    """A run with a K10 on #16 ahead of a K12 awaiting approval, stopped at the K10.

    ``first`` is the first fixer's handler, by default one that asks for both.
    """
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(
            first or fixer(_note_request(), pr_body_update("R1-F2"), commit=False), SECOND_FIXER
        ),
        policy="approve",
        findings=2,
    )
    gh.add_issue(ISSUE, "Ship the feature", body="Acceptance: a coordination note on #16.")
    gh.add_issue(NOTE_TARGET, "Scheduler")
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.OPERATOR_ACTIONS
    assert gh.effect_writes == []
    return eng


def _record_of(eng, kind: EffectKind) -> EffectRecord:
    records = [EffectRecord.from_dict(r) for r in load_state(eng.paths.state_file).effect_records]
    [record] = [r for r in records if r.kind is kind]
    return record


def _assert_refused_and_nothing_saved(eng, action: str, ahead: str) -> str:
    """The decline of ``action`` names ``ahead``, keeps the journal as it was and writes nothing."""
    saved = eng.paths.state_file.read_bytes()
    writes = list(eng.github.effect_writes)
    previewed = eng.unblock(REASON, decline_action=[action], dry_run=True)
    assert "would stay BLOCKED" in previewed.message and ahead in previewed.message
    refused = eng.unblock(REASON, decline_action=[action])
    assert not refused.unblocked, refused.message
    assert "cannot be declined yet" in refused.message and ahead in refused.message
    assert eng.paths.state_file.read_bytes() == saved
    assert eng.github.effect_writes == writes
    return refused.message


def test_a_decline_of_the_description_never_forgets_a_comment_a_crash_left_unsaved(tmp_state_dir):
    """The K9 comment landed and the crash lost its observation; a block that reconciles nothing
    then lets the operator name K12. Refused with the journal as it was, and once FIX has adopted
    the comment, with no second write, the decline holds and the comment's URL is kept."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(fixer(acceptance("R1-F1"), pr_body_update("R1-F2"), commit=False)),
        policy="approve",
        findings=2,
    )
    eng.config.safety = replace(eng.config.safety, acceptance_actions="auto")
    _power_loss_at(eng, EffectKind.ACCEPTANCE_COMMENT, Stage.OBSERVED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    [posted] = gh.comments[ISSUE]
    gh.add_issue(OTHER_ISSUE, "Unplanned", body=render_follow_up_marker(PR, "R1-F2"))
    eng2 = _resumed(eng, tmp_state_dir, gh, scripted(SECOND_FIXER))
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    assert "carries the follow-up marker of R1-F2" in out.message
    k9 = _record_of(eng2, EffectKind.ACCEPTANCE_COMMENT)
    assert k9.stage is Stage.ATTEMPTED
    action = _k12(eng2).action_id

    _assert_refused_and_nothing_saved(eng2, action, k9.action_id)

    gh.add_issue(OTHER_ISSUE, "Unplanned", state="CLOSED")
    assert eng2.unblock(REASON).unblocked
    out = eng2.step()
    assert out.next_phase == "BLOCKED", out.message
    assert _record_of(eng2, EffectKind.ACCEPTANCE_COMMENT).stage is Stage.OBSERVED
    assert eng2.unblock("not this text", decline_action=[action]).unblocked
    s = load_state(eng2.paths.state_file)
    assert s.effect_records == []
    assert [(o["action_id"], o["outcome"], o["comment_url"]) for o in s.acceptance_outcomes] == [
        (k9.action_id, "verified", posted.url)
    ]
    assert [o["outcome"] for o in s.pr_body_outcomes] == ["declined"]
    assert [w[0] for w in gh.effect_writes] == ["create_issue_comment"]
    assert eng2.step().next_phase == "REVIEW"
    assert action in eng2.provider.calls[0].prompt


def test_a_decline_of_the_description_never_withdraws_a_posted_operator_comment(tmp_state_dir):
    """The exact K10 comment went up while the plan was held. Refused, as declining that K10
    itself is; once a plain unblock saved the comment, the decline holds and keeps its URL."""
    gh = FakeGitHub()
    eng = _with_note(tmp_state_dir, gh)
    k10 = _record_of(eng, EffectKind.OPERATOR_COMMENT)
    action = _k12(eng).action_id
    posted = gh.add_comment(NOTE_TARGET, 7, _note_payload())

    _assert_refused_and_nothing_saved(eng, action, k10.action_id)
    assert _record_of(eng, EffectKind.OPERATOR_COMMENT).stage is Stage.INTENDED

    assert eng.unblock(REASON).unblocked
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert out.stop.code is StopReasonCode.ACCEPTANCE_ACTION
    assert eng.unblock("not this text", decline_action=[action]).unblocked
    s = load_state(eng.paths.state_file)
    assert s.effect_records == []
    assert [
        (o["action_id"], o["outcome"], o["comment_url"]) for o in s.operator_comment_outcomes
    ] == [(k10.action_id, "verified", posted.url)]
    assert [o["outcome"] for o in s.pr_body_outcomes] == ["declined"]
    assert eng.step().next_phase == "REVIEW"
    assert gh.effect_writes == []


def test_a_decline_of_the_description_keeps_an_operator_action_whose_target_cannot_be_read(
    tmp_state_dir,
):
    """Inconclusive: what is on the target is unknown, so nothing of the plan is given up."""
    gh = FakeGitHub()
    eng = _with_note(tmp_state_dir, gh)
    k10 = _record_of(eng, EffectKind.OPERATOR_COMMENT)
    gh.comments_error = GitHubError("HTTP 403: the comments of #16 cannot be read")
    _assert_refused_and_nothing_saved(eng, _k12(eng).action_id, k10.action_id)


def test_a_decline_of_the_description_keeps_an_operator_action_in_conflict(tmp_state_dir):
    """Conflicting: a comment carrying the K10's marker with other bytes is repaired first."""
    gh = FakeGitHub()
    plan = fixer(_note_request(), pr_body_update("R1-F2"), commit=False)

    def posting_other_bytes(req):
        gh.add_comment(NOTE_TARGET, 7, _note_payload().replace(NOTE, "A note someone changed."))
        return plan(req)

    eng = _with_note(tmp_state_dir, gh, first=posting_other_bytes)
    k10 = _record_of(eng, EffectKind.OPERATOR_COMMENT)
    assert k10.stage is Stage.CONFLICT
    _assert_refused_and_nothing_saved(eng, _k12(eng).action_id, k10.action_id)
    assert load_state(eng.paths.state_file).operator_comment_outcomes == []


def test_a_refused_decline_names_the_way_on_and_declining_the_operator_action_takes_it(
    tmp_state_dir,
):
    """Nothing posted: the K10 still decides first, and its own decline drops the whole plan."""
    gh = FakeGitHub()
    eng = _with_note(tmp_state_dir, gh)
    k10 = _record_of(eng, EffectKind.OPERATOR_COMMENT)
    message = _assert_refused_and_nothing_saved(eng, _k12(eng).action_id, k10.action_id)
    assert eng.cli_command("status") in message
    assert eng.cli_command("unblock", "--reason") in message
    assert eng.unblock("not on #16", decline_action=[k10.action_id]).unblocked
    s = load_state(eng.paths.state_file)
    assert s.effect_records == []
    assert [o["outcome"] for o in s.operator_comment_outcomes] == ["declined"]


@pytest.mark.parametrize("posted", [False, True], ids=["nothing-posted", "exact-payload-posted"])
def test_a_decline_of_the_description_waits_for_an_acceptance_action_awaiting_approval(
    tmp_state_dir, posted
):
    gh = FakeGitHub()
    eng = _with_acceptance(
        tmp_state_dir,
        gh,
        fixer(acceptance("R1-F1"), pr_body_update("R1-F2"), commit=False),
        policy="approve",
        findings=2,
    )
    assert eng.step().next_phase == "BLOCKED"
    k9 = _record_of(eng, EffectKind.ACCEPTANCE_COMMENT)
    if posted:
        gh.add_comment(ISSUE, 7, k9.body)
    _assert_refused_and_nothing_saved(eng, _k12(eng).action_id, k9.action_id)
    assert load_state(eng.paths.state_file).acceptance_outcomes == []


def test_status_offers_the_decline_of_the_description_once_nothing_is_ahead_of_it(tmp_state_dir):
    gh = FakeGitHub()
    eng = _with_note(tmp_state_dir, gh)
    k10 = _record_of(eng, EffectKind.OPERATOR_COMMENT)
    action = _k12(eng).action_id
    declining = eng.cli_command("unblock", "--decline-action", action, "--reason")
    refused = f"'unblock --decline-action {action}' until {k10.action_id} is verified"
    lines = _status_text(load_state(eng.paths.state_file), eng)
    assert declining not in lines
    assert f"[snapshot] {refused}: it is refused" in lines
    gh.add_comment(NOTE_TARGET, 7, _note_payload())
    assert eng.unblock(REASON).unblocked
    assert eng.step().next_phase == "BLOCKED"
    lines = _status_text(load_state(eng.paths.state_file), eng)
    assert declining in lines and refused not in lines


def test_a_head_moved_between_the_description_and_the_push_keeps_the_verified_outcome(
    tmp_state_dir,
):
    """The description landed; the fix cannot be pushed over a moved HEAD: REVIEW of the
    actual revision, the outcome verified, the description not rewritten."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir, gh, scripted(fixer(fixed("R1-F1"), pr_body_update("R1-F2"))), findings=2
    )
    run = eng.state.run_id
    moved: list[str] = []
    persist = eng._persist_effect

    def moving(record):
        persist(record)
        if record.kind is EffectKind.PR_BODY_UPDATE and record.stage is Stage.OBSERVED:
            moved.append(publish_pr_head(eng, message="A push from elsewhere (#2)"))

    eng._persist_effect = moving
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    s = load_state(eng.paths.state_file)
    assert s.current_head_sha == moved[0] == eng.origin.head(BRANCH)
    assert [o["outcome"] for o in s.pr_body_outcomes] == ["verified"]
    assert _body_writes(gh) == [_payload(run)]


def test_a_rereview_stays_refused_while_the_plan_is_saved(tmp_state_dir):
    gh = FakeGitHub()
    eng = _held(tmp_state_dir, gh)
    refused = eng.unblock(REASON, rereview=True)
    assert not refused.unblocked
    assert load_state(eng.paths.state_file).phase is Phase.BLOCKED
    assert _body_writes(gh) == []


def test_status_names_a_conflict_with_its_remedy(tmp_state_dir):
    gh = FakeGitHub()
    eng = _journaled(tmp_state_dir, gh)
    gh.prs[PR].body = _base(eng.state.run_id, A_HUMAN_EDIT)
    eng2 = _resumed(eng, tmp_state_dir, gh)
    assert eng2.step().next_phase == "BLOCKED"
    state = load_state(eng2.paths.state_file)
    base = _k12(eng2).precondition["base_sha256"]
    lines = _status_text(state, eng2)
    assert "state:         conflict:" in lines
    # Authorized by the policy saved with the plan: back to the base, or the payload.
    assert f"put the PR description back to sha256 {base}, or set it to exactly" in lines
    assert "keep the description as it is and decline the update" in lines
    assert cli._pr_body_updates(state)[0]["state"] == "conflict"


def test_a_replacement_of_the_longest_allowed_text_fits_every_bound(tmp_state_dir):
    gh = FakeGitHub()
    text = "x" * MAX_PR_BODY_CHARS
    eng = _updating(tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1", text), commit=False)))
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert gh.prs[PR].body == _payload(eng.state.run_id, text)


def test_validation_that_fails_stops_a_mixed_fix_before_the_description_is_sent(tmp_state_dir):
    """The configured commands run against the candidate before the plan's first write."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE, "Ship the feature")
    cfg = default_config()
    cfg.validation.commands = [["false"]]
    cfg.validation.max_failure_loops = 1
    eng = make_engine(
        tmp_state_dir,
        scripted(
            fixer(fixed("R1-F1"), pr_body_update("R1-F2")),
            fixer(fixed("R1-F1"), pr_body_update("R1-F2")),
        ),
        github=gh,
        cfg=cfg,
        origin=True,
    )
    head = publish_pr_head(eng)
    gh.add_pr(head_sha=head, body=_base(eng.state.run_id))
    eng.state.phase = Phase.FIX
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = head
    eng.state.reviewed_pr_url = PR
    eng.state.reviewed_head_sha = head
    eng.state.reviewed_base_ref = "main"
    eng.state.reviewed_merge_base_sha = eng.origin.merge_base("main", head)
    eng.state.review_round = 1
    eng.state.last_review_comment_url = comment_url(PR, 100)
    eng.state.last_review_result = "needs_fix"
    eng.state.open_findings = [_finding(1), _finding(2)]
    eng.step()
    assert _body_writes(gh) == [] and eng.origin.head(BRANCH) == head
