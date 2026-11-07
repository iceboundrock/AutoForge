"""The deterministic recovery action package (#374): ``status``'s projection of a stop.

Each scenario is a real engine run that stops (the fixtures of ADR 0011 D8
where one exists), read back by a fresh CLI engine as a later ``status``
would: the package must say what is proven, what is pending on which exact
target, what is forbidden, what must not be touched and the next commands,
each claim labelled with its freshness, and ``status --live`` must report
the decision ``unblock --dry-run`` prints for the same state and GitHub,
writing nothing and taking no lock.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from autoforge import cli
from autoforge.claims import render_follow_up_marker
from autoforge.effects import EffectKind, Stage, sha256_text
from autoforge.engine import ControllerEngine
from autoforge.errors import ExecutionTimeoutError, GitHubError, GitHubUnavailableError
from autoforge.locking import ControllerLock, IssueRunLock, repository_lock_path
from autoforge.providers import AgentExecutionResult, ProviderRegistry, ScriptedProvider
from autoforge.recovery_package import (
    DIAGNOSES,
    GUIDE_BLOCKED,
    GUIDE_PAUSE,
    GUIDE_PR_BODY,
    GUIDE_READY,
    GUIDE_REPLAN,
    GUIDE_RESUME,
    GUIDE_SCHEDULER,
    GUIDE_STATE,
    GUIDE_STOPPED,
    GUIDE_WAITS,
    MAX_TEXT_CHARS,
    REPLAN_POLICY_REFUSED,
    Commands,
    Policy,
    _command_line,
    build_package,
)
from autoforge.redaction import redact
from autoforge.run_namespace import issue_namespace
from autoforge.sandbox_pin import SOURCE_CREATED, SandboxPin
from autoforge.state import MAX_DECLINES_PER_ROUND, load_state, save_state
from autoforge.stop_classification import (
    ReasonSource,
    StopClassification,
    StopOutcome,
    StopReasonCode,
    classify_state,
)
from autoforge.transitions import Phase
from tests.conftest import (
    BRANCH,
    EPIC,
    GOOGLE_API_KEY,
    GOOGLE_KEY_BODY,
    ISSUE,
    ISSUE3,
    MERGE_BASE,
    PR,
    SHA_A,
    SHA_B,
    FakeGitHub,
    acceptance,
    block,
    checks_refused,
    deferred_to,
    fixed,
    fixer,
    implement,
    implementation_pr_body,
    make_engine,
    new_follow_up,
    pollute_operator_environment,
    pr_body_update,
    review_result,
    scripted,
    state_tree,
    unloadable_state,
)
from tests.test_engine import (
    ESCAPE_BODY,
    ISSUE43,
    REASON_211,
    _accepting,
    _analyzed,
    _bound_to_main,
    _crash,
    _disqualify,
    _finding,
    _in_fix,
    _in_merge,
    _k9,
    _loop_agent,
    _marked,
    _no_stagnation,
    _one_finding_per_round,
    _power_loss_at,
    _rename_main_to_trunk,
    _reports_head,
    _resumed,
)
from tests.test_operator_actions import (
    BOTH,
    ISSUE_BODY,
    NOTE16,
    T16,
    T158,
    _k10,
    _operating,
    _post,
    payload,
)
from tests.test_pr_body_update import (
    A_HUMAN_EDIT,
    _base,
    _edit_once_saved,
    _k12,
    _mixed,
    _payload,
    _updating,
)
from tests.test_troubleshooting_guide import ROOT, _anchors

SRC = Path(__file__).resolve().parent.parent / "src" / "autoforge"
FAKE_TOKEN = "ghp_" + "R" * 36


class _NoAgent(ScriptedProvider):
    """A provider a ``status`` engine must never launch."""

    def execute(self, req):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("status launched an agent")


@pytest.fixture
def status_cli(monkeypatch):
    """Run the CLI as a later process would, against a scenario's GitHub and configuration.

    Every engine the CLI builds loads the state from disk afresh, with the
    scenario engine's configuration, its FakeGitHub and a provider that
    fails if anything launches it; the working directory is the scenario's
    repository, so the scheduler view and the lock path resolve as for an
    operator in that checkout.
    """

    def run(eng, *argv: str, capsys, state_dir=None) -> tuple[int, str, str]:
        monkeypatch.chdir(eng.workdir)
        monkeypatch.setattr(cli, "_load_cfg", lambda args: copy.deepcopy(eng.config))
        real = ControllerEngine.__init__

        def patched(self, *a, **k):
            k.setdefault("github", eng.github)
            k.setdefault("workdir", eng.workdir)
            none = _NoAgent(None)
            k.setdefault(
                "providers", ProviderRegistry(overrides={"claude": none, "opencode": none})
            )
            real(self, *a, **k)

        monkeypatch.setattr(ControllerEngine, "__init__", patched)
        try:
            rc = cli.main(["--state-dir", str(state_dir or eng.paths.state_dir), *argv])
        finally:
            monkeypatch.setattr(ControllerEngine, "__init__", real)
        captured = capsys.readouterr()
        return rc, captured.out, captured.err

    return run


def _recovery(status_cli, eng, capsys, *flags: str) -> dict:
    rc, out, err = status_cli(eng, "status", "--json", *flags, capsys=capsys)
    assert rc == 0, err
    return json.loads(out)["recovery"]


def _commands(package: dict) -> list[str]:
    return [step["command"] for step in package["next"] if step["command"]]


def _keys(items: list[dict]) -> list[str]:
    return [item["key"] for item in items]


# -- the scenarios ------------------------------------------------------------------------


def _k10_blocked(tmp_state_dir):
    """K10, the shape of the reported #280 -> #23 case: FIX asks the operator to
    comment on two other issues, and the run waits with the push held back."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    assert eng.step().next_phase == "BLOCKED"
    return eng


def _k9_blocked(tmp_state_dir):
    """K9: FIX resolves a finding with an acceptance comment the operator approves."""
    gh = FakeGitHub()
    eng = _accepting(tmp_state_dir, gh, fixer(acceptance("R1-F1")))
    assert eng.step().next_phase == "BLOCKED"
    return eng


def _k12_blocked(tmp_state_dir):
    """K12 under ``safety.pr_body_updates: approve``: FIX would replace the PR's
    description, and the plan waits for the operator's approval of exactly it."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)), policy="approve"
    )
    assert eng.step().next_phase == "BLOCKED"
    return eng


def _false_identity_conflict(tmp_state_dir):
    """PR #314: a controller before #327 compared the body ``gh`` printed with the
    one it wrote, and blocked on its own PR as a conflict (ADR 0011 D8)."""
    gh = FakeGitHub()
    gh.gh_sanitizes = True
    eng = make_engine(
        tmp_state_dir, lambda req: implement(req, pr_body=ESCAPE_BODY), github=gh, origin=True
    )
    gh.get_pr_exact = gh.get_pr  # the read before #327
    assert _analyzed(eng).next_phase == "BLOCKED"
    del gh.get_pr_exact  # this controller reads the exact body again
    return eng


def _ambiguous(tmp_state_dir):
    """Two open PRs carry the issue's implementation marker: a true ambiguity."""
    gh = FakeGitHub()
    gh.add_pr(url=PR, head_sha=SHA_A, branch="autoforge/2-a", body=implementation_pr_body())
    gh.add_pr(
        url="https://github.com/owner/repo/pull/43", head_sha=SHA_B, body=implementation_pr_body()
    )
    eng = make_engine(tmp_state_dir, ["never"], github=gh)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    assert eng.step().next_phase == "BLOCKED"
    return eng


def _checks_refused(tmp_state_dir):
    """#291: GitHub refuses the PR's check runs to the credential, at the merge gate."""
    gh = FakeGitHub()
    gh.add_pr(head_sha=SHA_A)
    gh.checks_error = checks_refused()
    eng = _in_merge(tmp_state_dir, gh, phase=Phase.READY_FOR_MERGE)
    assert eng.step(allow_merge=True).next_phase == "BLOCKED"
    return eng


def _review_cap(tmp_state_dir):
    """An exhausted review loop: the round cap reached by a round with findings."""
    gh = FakeGitHub()
    eng = make_engine(
        tmp_state_dir, _loop_agent(gh, _one_finding_per_round), github=gh, origin=True
    )
    eng.config.workflow.max_review_rounds = 2
    _no_stagnation(eng.config)
    eng._save()
    assert eng.run(max_steps=50)[-1].next_phase == "BLOCKED"
    return eng


def _default_branch_after_the_plan(tmp_state_dir, *, pushed: bool = True):
    """PR #470 R6-F1: ANALYZE_EXECUTE saved its plan (the push, then the PR onto
    'main'), and sent its push or nothing; then GitHub's default branch was renamed,
    and the plan's completion in a later process blocks with code default-branch,
    sending nothing more (``tests/test_engine.py``'s rename after the plan)."""
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, implement, github=gh, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    if pushed:
        gh.write_failures.append(
            ("create_pull_request", GitHubUnavailableError("gh: HTTP 502"), False)
        )
        with pytest.raises(GitHubUnavailableError, match="'resume' reconciles it"):
            eng.step()
    else:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(ControllerEngine, "_complete_analyze", _crash)
            with pytest.raises(KeyboardInterrupt):
                eng.step()
    _rename_main_to_trunk(eng, gh)
    eng = _resumed(eng, tmp_state_dir, gh, "never")
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.stop_code == "default-branch"
    return eng


def _default_branch_before_the_plan(tmp_state_dir):
    """The default branch renamed while the agent ran: its result is refused before any
    plan is saved, with code default-branch (``tests/test_engine.py``)."""
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, None, github=gh, origin=True)

    def renames_meanwhile(req):
        reply = implement(req)
        _rename_main_to_trunk(eng, gh)
        return reply

    eng.provider._handler = scripted(renames_meanwhile, _reports_head)
    assert _analyzed(eng).next_phase == "BLOCKED"
    assert eng.state.stop_code == "default-branch"
    return eng


def _two_findings_in_fix(tmp_state_dir, gh, agent):
    return _in_fix(tmp_state_dir, gh, agent, [_finding(1, 1), _finding(1, 2)], origin=True)


def _fix_follow_up_closed_after_the_plan(tmp_state_dir):
    """PR #470 R6-F1: the saved FIX plan created R1-F2's follow-up issue, and the
    process died before the push; the issue was closed since, so the plan's completion
    in a later process blocks with code fix-plan-refused before the push
    (``tests/test_engine.py``'s replay of an observed follow-up)."""
    gh = FakeGitHub()
    eng = _two_findings_in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1"), new_follow_up("R1-F2")))
    _power_loss_at(eng, EffectKind.FOLLOW_UP_ISSUE, Stage.OBSERVED, saved=True)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    assert [w[0] for w in gh.effect_writes] == ["create_issue"]
    gh.issues[ISSUE43].state = "CLOSED"
    eng = _resumed(eng, tmp_state_dir, gh, "never")
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.stop_code == "fix-plan-refused"
    return eng


def _fix_plan_deferring_to_its_own_issue(tmp_state_dir):
    """PR #470 R6-F1: a saved FIX plan reusing the run's own issue as R1-F1's follow-up
    (one saved without the entry's check, written into the journal as
    ``tests/test_engine.py`` does): its completion refuses it whatever GitHub holds."""
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "follow-up", body=_marked("R1-F1"))
    eng = _two_findings_in_fix(
        tmp_state_dir, gh, fixer(deferred_to("R1-F1", ISSUE3), fixed("R1-F2"))
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ControllerEngine, "_complete_fix", _crash)
        with pytest.raises(KeyboardInterrupt):
            eng.step()
    eng.state.entry_observation["objects"][render_follow_up_marker(PR, "R1-F1")] = ISSUE
    eng._save()
    gh.issues[ISSUE3].body = "No marker any more."
    gh.issues[ISSUE].body = _marked("R1-F1", "The issue being implemented.")
    eng = _resumed(eng, tmp_state_dir, gh, "never")
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.stop_code == "fix-plan-refused"
    return eng


def _fix_refused_before_the_plan(tmp_state_dir):
    """FIX's entry refuses to launch the fixer while the run's own issue carries a
    finding's follow-up marker: code fix-plan-refused, and no plan is saved."""
    gh = FakeGitHub()
    eng = _two_findings_in_fix(tmp_state_dir, gh, fixer(fixed("R1-F1"), fixed("R1-F2")))
    _bound_to_main(eng)
    eng.state.reviewed_pr_url = PR
    eng.state.last_review_result = "needs_fix"
    gh.issues[ISSUE].body = _marked("R1-F1", "The issue being implemented.")
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.stop_code == "fix-plan-refused"
    return eng


BLOCKED_SCENARIOS = {
    "k10-operator-actions": _k10_blocked,
    "k9-acceptance-action": _k9_blocked,
    "k12-pr-body-update": _k12_blocked,
    "false-identity-conflict": _false_identity_conflict,
    "ambiguous-prs": _ambiguous,
    "checks-refused-291": _checks_refused,
    "review-round-cap": _review_cap,
    # One code, a stop before any plan and a saved plan's stop (PR #470 R6-F1).
    "default-branch-before-the-plan": _default_branch_before_the_plan,
    "default-branch-after-the-plan": _default_branch_after_the_plan,
    "fix-plan-refused-before-the-plan": _fix_refused_before_the_plan,
    "fix-plan-refused-after-a-write": _fix_follow_up_closed_after_the_plan,
    "fix-plan-refused-own-issue": _fix_plan_deferring_to_its_own_issue,
}


def _prefix(eng, state_dir=None) -> str:
    """How the package spells this run's commands: the flags the CLI was given."""
    return shlex.join(["autoforge", "--state-dir", str(state_dir or eng.paths.state_dir)])


def _push_sha(eng) -> str:
    [push] = [r for r in load_state(eng.paths.state_file).effect_records if r["kind"] == "push"]
    return push["payload"]["sha"]


class _TimingOut(ScriptedProvider):
    """An agent killed at its limit, as ADR 0011 D8's timeout fixture is."""

    def execute(self, req):
        self.calls.append(req)
        return AgentExecutionResult(
            command=["x"],
            exit_code=-1,
            stdout="",
            stderr="",
            started_at="t",
            finished_at="t",
            timed_out=True,
        )


# -- the scenario table -------------------------------------------------------------------


def test_k10_operator_actions_name_each_target_id_and_exact_command(
    tmp_state_dir, status_cli, capsys
):
    """#280 -> #23's shape: each operator action is pending on its exact target with
    its id and payload reference; approving one is forbidden; the worktree holding the
    unpushed FIX candidate must not be touched; the next steps are exactly the commands
    'status' and 'unblock' accept for those ids, and none approves anything."""
    eng = _k10_blocked(tmp_state_dir)
    x16, x158 = _k10(eng, T16), _k10(eng, T158)
    saved = load_state(eng.paths.state_file)
    package = _recovery(status_cli, eng, capsys)
    assert (package["outcome"], package["code"], package["view"]) == (
        "blocked",
        "operator-actions",
        "standalone",
    )
    assert package["diagnosis"]["code"] == "operator-actions"
    assert package["snapshot_at"] == saved.updated_at
    actions = [p for p in package["pending"] if p["action_id"]]
    assert [
        (p["action_id"], p["kind"], p["performer"], p["target"], p["state"]) for p in actions
    ] == [
        (x16.action_id, "operator_comment", "operator", T16, "pending"),
        (x158.action_id, "operator_comment", "operator", T158, "pending"),
    ]
    prefix = _prefix(eng)
    for item, record in zip(actions, (x16, x158), strict=True):
        assert item["payload_chars"] == len(record.body)
        assert item["payload_sha256"] == sha256_text(record.body)
        assert item["payload_command"] == f"{prefix} status --action {record.action_id}"
        assert (item["freshness"], item["observed_at"]) == ("snapshot", saved.updated_at)
    [push] = [p for p in package["pending"] if p["kind"] == "push"]
    assert (push["performer"], push["stage"]) == ("controller", "intended")
    forbidden = _keys(package["forbidden"])
    assert {"resume", f"approve-{x16.action_id}", f"approve-{x158.action_id}"} <= set(forbidden)
    worktree = next(c for c in package["preserve"] if c["key"] == "worktree")
    assert _push_sha(eng) in worktree["text"] and "do not reset" in worktree["text"]
    assert worktree["freshness"] == "snapshot"
    commands = _commands(package)
    for record in (x16, x158):
        target = record.target["issue_url"]
        assert (
            f"{prefix} status --action {record.action_id} | gh issue comment {target} --body-file -"
        ) in commands
    # One unblock binds every comment the operator binds (PR #470 R1-F2).
    assert [c for c in commands if "--bind-evidence" in c] == [
        f"{prefix} unblock --bind-evidence {x16.action_id} <comment URL> --bind-evidence "
        f'{x158.action_id} <comment URL> --reason "..."'
    ]
    assert f'{prefix} unblock --reason "..."' in commands and f"{prefix} resume" in commands
    assert (
        f"{prefix} unblock --decline-action {x16.action_id} --decline-action "
        f'{x158.action_id} --reason "..."'
    ) in commands
    assert not [c for c in commands if "--approve-action" in c]
    assert package["guide"] == GUIDE_WAITS


@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_k9_approval_or_decline_names_exactly_the_saved_id(
    tmp_state_dir, status_cli, capsys, decision
):
    """K9: the action awaits approval of exactly its id; any other id is forbidden.
    After the operator's approval the run is in FIX with the action approved and
    'resume' publishes it; after a decline the plan is gone and 'resume' runs the
    fixer again. Neither later package offers 'unblock', which a FIX run refuses."""
    eng = _k9_blocked(tmp_state_dir)
    action = _k9(eng).action_id
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    [k9] = [p for p in package["pending"] if p["action_id"]]
    assert (k9["action_id"], k9["state"], k9["authorization"], k9["target"]) == (
        action,
        "awaiting_approval",
        "operator",
        ISSUE,
    )
    # Approval or the payload, and the issue's eligibility either way (PR #470 R3-F3).
    assert k9["required"].startswith("an approval of exactly this action id (none is recorded)")
    assert "the issue open in the run's repository owner/repo" in k9["required"]
    commands = _commands(package)
    assert f'{prefix} unblock --approve-action {action} --reason "..."' in commands
    assert f'{prefix} unblock --decline-action {action} --reason "..."' in commands
    assert f"stale-{action}" in _keys(package["forbidden"])
    assert package["guide"] == GUIDE_WAITS

    if decision == "approve":
        assert eng.unblock(REASON_211, approve_action=action).unblocked
    else:
        assert eng.unblock(REASON_211, decline_action=action).unblocked
    package = _recovery(status_cli, eng, capsys)
    assert (package["outcome"], package["guide"]) == ("in_progress", GUIDE_RESUME)
    assert _commands(package) == [f"{prefix} resume"]
    assert "unblock" in _keys(package["forbidden"])
    actions = [(p["action_id"], p["state"]) for p in package["pending"] if p["action_id"]]
    assert actions == ([(action, "approved")] if decision == "approve" else [])
    if decision == "approve":
        # The approval given is reported satisfied, never asked for again (R3-F3).
        [k9] = [p for p in package["pending"] if p["action_id"]]
        assert k9["required"].startswith(
            "no further approval: one of exactly this action id is recorded"
        )


def test_k12_a_description_update_awaits_approval_of_exactly_its_id(
    tmp_state_dir, status_cli, capsys
):
    """K12: the update is pending on the run's own PR, over the description at the
    digest the plan recorded; approving it, setting the description by hand and
    declining it are the exact commands, and an approval of another id is forbidden."""
    eng = _k12_blocked(tmp_state_dir)
    record = _k12(eng)
    action = record.action_id
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    [k12] = [p for p in package["pending"] if p["action_id"]]
    assert (k12["kind"], k12["state"], k12["authorization"], k12["target"]) == (
        "pr_body_update",
        "awaiting_approval",
        "operator",
        PR,
    )
    assert k12["finding_id"] == "R1-F1"
    assert record.precondition["base_sha256"] in k12["required"]
    commands = _commands(package)
    assert f'{prefix} unblock --approve-action {action} --reason "..."' in commands
    assert f'{prefix} unblock --decline-action {action} --reason "..."' in commands
    assert f"stale-{action}" in _keys(package["forbidden"])
    assert package["guide"] == GUIDE_PR_BODY
    assert k12["required"].startswith("an approval of exactly this action id (none is recorded)")
    assert eng.unblock("approved", approve_action=action).unblocked
    package = _recovery(status_cli, eng, capsys)
    [k12] = [p for p in package["pending"] if p["action_id"]]
    assert k12["state"] == "approved"
    # Satisfied, never asked for again (PR #470 R3-F3).
    assert k12["required"] == (
        "no further approval: one of exactly this action id is recorded; the description "
        f"must still be at sha256 {record.precondition['base_sha256']}"
    )


@pytest.mark.parametrize("conflict", ["false", "authentic"])
def test_a_conflict_is_named_and_its_own_object_never_advised_away(
    tmp_state_dir, status_cli, capsys, conflict
):
    """PR #314's false identity conflict (a controller before #327 misread the run's own
    PR) and an authentic one (someone rewrote the PR's body): the package names the
    conflicting record and its reason in both, says the run's own object needs no
    change, and previews unblock's re-entry. Then the phase reconciles: it adopts the
    run's own PR and blocks again on the other, as the step says it would."""
    eng = _false_identity_conflict(tmp_state_dir)
    if conflict == "authentic":
        eng.github.prs[PR].body = "Rewritten by hand."
    package = _recovery(status_cli, eng, capsys, "--live")
    [record] = [p for p in package["pending"] if p["stage"] == "conflict"]
    assert record["kind"] == "implementation_pr" and record["target"] == "owner/repo"
    assert "carries its identity but is not the object" in record["observed"]
    [inspect] = [s for s in package["next"] if s["text"].startswith("inspect the object")]
    assert (
        "needs no change" in inspect["text"]
        and "blocks again on a true conflict" in (inspect["text"])
    )
    live = package["live"]
    assert (live["decision"], live["target_phase"]) == ("would_reenter", "ANALYZE_EXECUTE")
    assert eng.unblock("checked PR #42").unblocked
    out = eng.step()
    if conflict == "false":
        assert out.next_phase == "REVIEW", out.message
    else:
        assert out.next_phase == "BLOCKED"
        assert "carries its identity but is not the object" in eng.state.block_reason


def test_true_ambiguous_prs_are_never_chosen_between(tmp_state_dir, status_cli, capsys):
    """Two PRs carry the issue's marker: nothing is pending, no command takes either
    over or approves anything, and the live preview stays BLOCKED naming both."""
    eng = _ambiguous(tmp_state_dir)
    package = _recovery(status_cli, eng, capsys, "--live")
    assert package["pending"] == [] and package["guide"] == GUIDE_BLOCKED
    commands = _commands(package)
    assert not [c for c in commands if "--take-over" in c or "--approve-action" in c]
    live = package["live"]
    assert (live["decision"], live["target_phase"]) == ("would_stay_blocked", None)
    assert PR in live["detail"] and "https://github.com/owner/repo/pull/43" in live["detail"]


def test_refused_checks_291_are_previewed_and_never_claimed_passing(
    tmp_state_dir, status_cli, capsys
):
    """#291: no claim says the checks passed; the preview names the merge gate's own
    re-verification, and nothing is merged."""
    eng = _checks_refused(tmp_state_dir)
    package = _recovery(status_cli, eng, capsys, "--live")
    assert package["outcome"] == "blocked" and package["guide"] == GUIDE_BLOCKED
    assert package["diagnosis"]["code"] == "github-checks-refused"
    assert not [c for c in package["proven"] if "check" in c["text"]]
    live = package["live"]
    assert live["target_phase"] == "READY_FOR_MERGE"
    assert "re-verifies it on GitHub before any merge" in live["detail"]
    assert eng.github.merges == []


def test_a_resumable_timeout_routes_to_resume_with_its_budget(tmp_state_dir, status_cli, capsys):
    """A killed agent leaves the phase unchanged: the package is resume's, with its
    reconciliation and budget caveats; 'unblock' is forbidden, and --live reads nothing
    for a run that is not BLOCKED."""
    eng = make_engine(tmp_state_dir, [], github=FakeGitHub(), origin=True)
    eng.providers._overrides = {"claude": _TimingOut(), "opencode": _TimingOut()}
    eng.state.phase = Phase.ANALYZE_EXECUTE
    with pytest.raises(ExecutionTimeoutError):
        eng.step()
    reads = len(eng.github.calls)
    package = _recovery(status_cli, eng, capsys, "--live")
    without = len(eng.github.calls) - reads
    _recovery(status_cli, eng, capsys)
    assert len(eng.github.calls) - reads == 2 * without  # --live read nothing more
    assert (package["outcome"], package["guide"]) == ("in_progress", GUIDE_RESUME)
    [resume] = package["next"]
    assert resume["command"] == f"{_prefix(eng)} resume"
    assert "re-enters ANALYZE_EXECUTE" in resume["text"] and "reconciles" in resume["text"]
    assert "unblock" in _keys(package["forbidden"])
    assert "steps" in _keys(package["proven"])
    live = package["live"]
    assert (live["requested"], live["applies"], live["decision"]) == (True, False, "not_decided")
    assert "BLOCKED runs only" in live["problem"]


def test_an_exhausted_review_loop_names_the_bound_unblock_refuses(
    tmp_state_dir, status_cli, capsys
):
    eng = _review_cap(tmp_state_dir)
    package = _recovery(status_cli, eng, capsys, "--live")
    assert package["diagnosis"]["code"] == "review-round-cap"
    rounds = next(c for c in package["proven"] if c["key"] == "review-rounds")
    assert rounds["freshness"] == "configuration" and rounds["observed_at"] is None
    assert "workflow.max_review_rounds is 2 (reached)" in rounds["text"]
    live = package["live"]
    assert live["decision"] == "would_stay_blocked"
    assert "Raise 'workflow.max_review_rounds'" in live["detail"]


@pytest.mark.parametrize("flags", [(), ("--json",), ("--live",), ("--json", "--live")])
def test_a_local_blocked_state_gets_no_package_and_no_unblock(
    tmp_state_dir, status_cli, capsys, flags
):
    """LOCAL is removed (ADR 0009): its BLOCKED state is refused by the loader, so
    status prints nothing of it and never an 'unblock' edge, and reads no GitHub."""
    eng = make_engine(tmp_state_dir, [])
    eng.state.phase = Phase.BLOCKED
    eng.state.block_reason = "the agent asked for a decision"
    eng._save()
    raw, why = unloadable_state("local-mode", eng.paths.state_file.read_bytes())
    eng.paths.state_file.write_bytes(raw)
    rc, out, err = status_cli(eng, "status", *flags, capsys=capsys)
    assert (rc, out) == (2, "")
    assert why in err and "unblock" not in err
    assert eng.github.calls == []
    assert eng.paths.state_file.read_bytes() == raw


def test_a_failed_run_routes_to_a_new_run_never_unblock_or_resume(
    tmp_state_dir, status_cli, capsys
):
    eng = make_engine(
        tmp_state_dir,
        [block({"phase": "ANALYZE_EXECUTE", "status": "failure", "message": "tests red"})],
        github=FakeGitHub(),
        origin=True,
    )
    eng.state.phase = Phase.ANALYZE_EXECUTE
    assert eng.step().next_phase == "FAILED"
    package = _recovery(status_cli, eng, capsys, "--live")
    assert (package["outcome"], package["guide"]) == ("failed", GUIDE_STOPPED)
    assert _commands(package) == [
        f"{_prefix(eng)} run --force --epic {EPIC} --issue {ISSUE}",
    ]
    assert "unblock" in _keys(package["forbidden"])
    assert package["live"]["applies"] is False


# -- the live preview -----------------------------------------------------------------------


@pytest.mark.parametrize("scenario", sorted(BLOCKED_SCENARIOS))
def test_the_live_preview_is_the_decision_unblock_dry_run_prints(
    tmp_state_dir, status_cli, capsys, scenario
):
    """Dry-run parity: 'status --live' reports exactly the decision and the
    configuration notice 'unblock --dry-run' prints for the same state and GitHub."""
    eng = BLOCKED_SCENARIOS[scenario](tmp_state_dir)
    live = _recovery(status_cli, eng, capsys, "--live")["live"]
    assert (live["requested"], live["applies"], live["freshness"]) == (True, True, "live")
    assert live["observed_at"] and live["problem"] == ""
    rc, out, err = status_cli(eng, "unblock", "--dry-run", "--reason", "preview", capsys=capsys)
    assert rc == 0, err
    if live["target_phase"]:
        expected = f"[dry-run] would re-enter {live['target_phase']}: {live['detail']}"
    else:
        expected = f"[dry-run] would stay BLOCKED: {live['detail']}"
    assert out == redact(f"[{eng.state.run_id}] {expected}") + "\n"
    assert err == redact(f"autoforge: {live['configuration']}") + "\n"


def test_status_live_writes_nothing_takes_no_lock_and_launches_nothing(
    tmp_state_dir, status_cli, capsys
):
    """Read-only, with another controller holding the repository lock: both output
    modes print, the state directory is byte- and inode-identical, nothing is sent
    to GitHub, and no agent is launched (the CLI's provider fails if one is)."""
    eng = _k10_blocked(tmp_state_dir)
    before = state_tree(tmp_state_dir)
    writes = list(eng.github.effect_writes)
    with ControllerLock(repository_lock_path(Path(eng.workdir))):
        for flags in ((), ("--json",)):
            rc, out, err = status_cli(eng, "status", "--live", *flags, capsys=capsys)
            assert rc == 0, err
            assert "would stay BLOCKED" in out or '"would_stay_blocked"' in out
    assert state_tree(tmp_state_dir) == before
    assert eng.github.effect_writes == writes


def test_live_drift_is_reported_against_the_snapshot(tmp_state_dir, status_cli, capsys):
    """A push to the PR after the stop: the snapshot keeps the HEAD it recorded, the
    live read shows the new one, and the drift names both."""
    eng = _checks_refused(tmp_state_dir)
    eng.github.prs[PR].head_sha = SHA_B
    package = _recovery(status_cli, eng, capsys, "--live")
    pr = next(c for c in package["proven"] if c["key"] == "pr")
    assert SHA_A in pr["text"] and pr["freshness"] == "snapshot"
    live = package["live"]
    assert live["pr"]["head_sha"] == SHA_B
    assert live["drift"] == [{"key": "pr_head_sha", "snapshot": SHA_A, "live": SHA_B}]
    rc, out, _ = status_cli(eng, "status", "--live", capsys=capsys)
    assert f"drift: [live] pr_head_sha is {SHA_B}; the snapshot says {SHA_A}" in out


@pytest.mark.parametrize("error", [GitHubUnavailableError, GitHubError])
def test_a_failed_live_read_is_reported_redacted_and_status_still_prints(
    tmp_state_dir, status_cli, capsys, error
):
    """As for 'unblock': a transient failure decides nothing, and the live section says
    why with the error's class; a conclusive one is unblock's refusal naming it. The
    credential the error quoted is redacted either way, and status exits 0."""
    eng = _checks_refused(tmp_state_dir)
    eng.github.get_pr_error = error(f"HTTP 401: bad credentials GH_TOKEN={FAKE_TOKEN}")
    rc, out, err = status_cli(eng, "status", "--json", "--live", capsys=capsys)
    assert rc == 0, err
    assert FAKE_TOKEN not in out
    live = json.loads(out)["recovery"]["live"]
    if error is GitHubUnavailableError:
        assert (live["decision"], live["freshness"], live["observed_at"]) == (
            "not_decided",
            "unknown",
            None,
        )
        assert live["problem"].startswith(
            "not decided: reading GitHub failed (GitHubUnavailableError: "
        )
        shown = "live: [unknown] not decided: reading GitHub failed"
    else:
        assert (live["decision"], live["freshness"]) == ("would_stay_blocked", "live")
        assert "HTTP 401" in live["detail"] and live["problem"] == ""
        shown = "decision: [live] would stay BLOCKED: "
    rc, out, err = status_cli(eng, "status", "--live", capsys=capsys)
    assert rc == 0 and FAKE_TOKEN not in out + err
    assert shown in out


def test_a_configuration_unblock_refuses_is_reported_not_decided(tmp_state_dir, status_cli, capsys):
    """The recorded-configuration gate 'unblock --dry-run' runs first refuses a
    weaker sandbox.mode (#280): the preview says so and decides nothing."""
    eng = _checks_refused(tmp_state_dir)
    eng.state.sandbox_pin = SandboxPin("required", SOURCE_CREATED, eng.state.updated_at).to_dict()
    eng._save()
    live = _recovery(status_cli, eng, capsys, "--live")["live"]
    assert live["decision"] == "not_decided"
    assert live["problem"].startswith("not decided: 'unblock' refuses this command's configuration")
    rc, _, err = status_cli(eng, "unblock", "--dry-run", "--reason", "x", capsys=capsys)
    assert rc == 2 and "sandbox.mode" in err


def test_status_action_takes_no_live(tmp_state_dir, status_cli, capsys):
    eng = _k10_blocked(tmp_state_dir)
    action = _k10(eng, T16).action_id
    rc, out, err = status_cli(eng, "status", "--action", action, "--live", capsys=capsys)
    assert (rc, out) == (2, "") and "takes no --live" in err


# -- byte safety, redaction, credentials --------------------------------------------------


def test_action_ids_are_byte_exact_and_their_payloads_print_unchanged(
    tmp_state_dir, status_cli, capsys
):
    """Every id the package names is the record's recomputed id, survives redaction
    unchanged, and 'status --action' with it prints exactly the record's payload."""
    eng = _k10_blocked(tmp_state_dir)
    package = _recovery(status_cli, eng, capsys)
    records = {r.action_id: r for r in (_k10(eng, T16), _k10(eng, T158))}
    named = [p["action_id"] for p in package["pending"] if p["action_id"]]
    assert sorted(named) == sorted(records)
    for action, record in records.items():
        assert redact(action) == action
        rc, out, _ = status_cli(eng, "status", "--action", action, capsys=capsys)
        assert rc == 0 and out == record.body


def test_the_package_never_carries_an_environment_credential(
    tmp_state_dir, tmp_path, status_cli, capsys, monkeypatch
):
    """Provider and GitHub credentials stay where they are: an operator's shell full
    of them changes nothing the package says, and no agent is launched to read one."""
    eng = _k10_blocked(tmp_state_dir)
    _, clean, _ = status_cli(eng, "status", "--json", capsys=capsys)
    pollute_operator_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "Z" * 40)
    rc, out, err = status_cli(eng, "status", "--json", "--live", capsys=capsys)
    assert rc == 0, err
    for value in os.environ.values():
        if len(value) >= 20 and ("ghp_" in value or "sk-ant-" in value):
            assert value not in out + err
    assert json.loads(out)["recovery"]["next"] == json.loads(clean)["recovery"]["next"]


# -- views ----------------------------------------------------------------------------------


def test_a_scheduler_namespaces_package_names_the_schedulers_commands(
    tmp_state_dir, status_cli, capsys
):
    """A namespace's BLOCKED run is unblocked in its state directory once the scheduler
    has exited, then continued or released by 'epic' commands; a standalone 'resume' or
    'run' is forbidden. The preview takes no lock, the scheduler's included."""
    eng = make_engine(tmp_state_dir, [])
    eng.github.add_issue(ISSUE, "Feature")
    namespace = issue_namespace(Path(eng.workdir) / ".git", 2)
    namespace.state_dir.mkdir(parents=True)
    blocked = copy.deepcopy(eng.state)
    blocked.phase = Phase.BLOCKED
    blocked.block_reason = "the agent asked for a decision"
    save_state(blocked, namespace.state_dir / "state.json")
    with ControllerLock(repository_lock_path(Path(eng.workdir))), IssueRunLock(namespace):
        rc, out, err = status_cli(
            eng, "status", "--json", "--live", capsys=capsys, state_dir=namespace.state_dir
        )
    assert rc == 0, err
    package = json.loads(out)["recovery"]
    assert package["view"] == "scheduler"
    prefix = _prefix(eng, namespace.state_dir)
    commands = _commands(package)
    assert f'{prefix} unblock --reason "..."' in commands
    assert f"autoforge epic resume {EPIC}" in commands
    assert f"autoforge epic archive {EPIC} 2" in commands
    assert not [c for c in commands if c.endswith(" resume") and "epic" not in c]
    [unblock] = [s for s in package["next"] if s["command"].endswith('unblock --reason "..."')]
    assert "once the scheduler has exited" in unblock["text"]
    assert "standalone-commands" in _keys(package["forbidden"])
    assert package["live"]["decision"] == "would_reenter"


def test_an_unknown_view_names_only_what_both_views_share(tmp_state_dir):
    """Outside a git repository the view is unknown (ADR 0011 D6): the package names
    neither 'resume' nor 'epic resume' as a command, and says why."""
    eng = make_engine(tmp_state_dir, [])
    eng.state.phase = Phase.BLOCKED
    stop = classify_state(eng.state, scheduler=None)
    package = build_package(stop, eng.state, commands=_commands_of(eng, resume=""), policy=_POLICY)
    assert "view" in _keys([c.as_dict() for c in package.unknown])
    texts = [s.text for s in package.next if not s.command]
    assert any("'epic resume' for a scheduler's issue" in t for t in texts)
    assert not [s for s in package.next if s.command.endswith(" resume")]


def test_a_pause_defers_to_its_interaction_owner(tmp_state_dir):
    """#126's pause, which nothing produces yet: no command and no route are invented."""
    eng = make_engine(tmp_state_dir, [])
    stop = StopClassification(
        StopOutcome.AWAITING_INPUT, StopReasonCode.AWAITING_INPUT, ReasonSource.STATE, "x"
    )
    package = build_package(stop, eng.state, commands=_commands_of(eng), policy=_POLICY)
    assert [s.command for s in package.next] == [""]
    assert "#126" in package.next[0].text and package.guide == GUIDE_PAUSE
    assert not [c for c in package.forbidden if c.key == "unblock"]


# -- what the package is --------------------------------------------------------------------

_POLICY = Policy(
    max_total_steps=300,
    max_review_rounds=20,
    allow_merge=False,
    pr_body_approvals=True,
    acceptance_approvals=True,
)


def _commands_of(eng, resume: str | None = None) -> Commands:
    return Commands(
        cli=eng.cli_command,
        scheduler=lambda *words: shlex.join(["autoforge", *words]),
        resume=eng.resume_command() if resume is None else resume,
        worktree=eng.agent_worktree_description(),
    )


def test_the_json_keys_are_stable(tmp_state_dir, status_cli, capsys):
    """Consumers read these names: every section and item has exactly these keys,
    whether --live was asked for or not, and the raw state fields are unchanged."""
    eng = _k10_blocked(tmp_state_dir)
    rc, plain, _ = status_cli(eng, "status", "--json", capsys=capsys)
    rc, live, _ = status_cli(eng, "status", "--json", "--live", capsys=capsys)
    for text in (plain, live):
        document = json.loads(text)
        package = document["recovery"]
        assert set(package) == {
            "schema",
            "outcome",
            "code",
            "view",
            "snapshot_at",
            "diagnosis",
            "proven",
            "unknown",
            "pending",
            "forbidden",
            "preserve",
            "next",
            "live",
            "guide",
        }
        assert package["schema"] == "autoforge.recovery/1"
        assert set(package["diagnosis"]) == {
            "code",
            "family",
            "meaning",
            "remedy",
            "source",
            "freshness",
            "observed_at",
        }
        for section in ("proven", "unknown", "forbidden", "preserve"):
            for claim in package[section]:
                assert set(claim) == {"key", "text", "freshness", "observed_at"}
                assert claim["freshness"] in ("snapshot", "live", "configuration", "unknown")
        for step in package["next"]:
            assert set(step) == {"command", "text", "alternative"}
        assert set(package["pending"][0]) == {
            "position",
            "kind",
            "action_id",
            "performer",
            "target",
            "stage",
            "attempts",
            "max_attempts",
            "state",
            "authorization",
            "finding_id",
            "required",
            "observed",
            "payload_chars",
            "payload_sha256",
            "payload_command",
            "freshness",
            "observed_at",
        }
        assert set(package["live"]) == {
            "requested",
            "applies",
            "freshness",
            "observed_at",
            "decision",
            "target_phase",
            "detail",
            "pr",
            "drift",
            "configuration",
            "problem",
            "note",
        }
    raw = {k: v for k, v in json.loads(plain).items() if k != "recovery"}
    assert raw == {k: v for k, v in json.loads(live).items() if k != "recovery"}


def test_two_status_runs_over_one_state_print_the_same(tmp_state_dir, status_cli, capsys):
    """Only a live read carries this command's clock: without --live, the same state
    and configuration print the same package, in both output modes."""
    eng = _k9_blocked(tmp_state_dir)
    for flags in ((), ("--json",)):
        first = status_cli(eng, "status", *flags, capsys=capsys)
        assert first == status_cli(eng, "status", *flags, capsys=capsys)


def test_the_human_block_labels_every_claim(tmp_state_dir, status_cli, capsys):
    eng = _k10_blocked(tmp_state_dir)
    rc, out, _ = status_cli(eng, "status", capsys=capsys)
    block = out[out.index("\nRecovery (blocked; ") : out.index("\nOuter sandbox:")]
    assert "\n  diagnosis: [snapshot] operator-actions (operator_wait): " in block
    for title in ("proven:", "pending:", "forbidden:", "do not touch:", "next:"):
        assert f"\n  {title}\n" in block
    claims = [line for line in block.splitlines() if line.startswith("    - ")]
    assert claims and all(
        line[6:].startswith(("[snapshot] ", "[configuration] ", "[unknown] ", "[live] "))
        for line in claims
    )
    assert "live: not read; 'status --live'" in block
    assert f"guide: {GUIDE_WAITS}" in block


def test_building_a_package_changes_nothing_of_the_state(tmp_state_dir):
    eng = _k10_blocked(tmp_state_dir)
    state = load_state(eng.paths.state_file)
    before = state.to_dict()
    build_package(classify_state(state), state, commands=_commands_of(eng), policy=_POLICY)
    assert state.to_dict() == before


def test_the_package_module_reads_nothing_and_decides_nothing():
    """Pure: it imports no I/O, process, clock, GitHub or engine module, and only the
    CLI imports it (``test_only_the_sinks_and_the_cli_import_the_classification``).
    ``validation`` parses GitHub URLs and reads nothing: a saved FIX plan's deferral to
    the run's own issue is told by issue identity, as FIX tells it (PR #470 R6-F1)."""
    tree = ast.parse((SRC / "recovery_package.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert imported == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "enum",
        ".claims",
        ".effects",
        ".loop_guard",
        ".redaction",
        ".state",
        ".stop_classification",
        ".transitions",
        ".validation",
    }


@pytest.mark.parametrize(
    "guide",
    [
        GUIDE_BLOCKED,
        GUIDE_WAITS,
        GUIDE_REPLAN,
        GUIDE_STOPPED,
        GUIDE_RESUME,
        GUIDE_STATE,
        GUIDE_READY,
        GUIDE_PR_BODY,
        GUIDE_SCHEDULER,
        GUIDE_PAUSE,
    ],
)
def test_every_guide_reference_resolves(guide):
    """The symptom-indexed guide (#260) stays the source of truth: the package only
    points into it, and every anchor it points to exists."""
    file_part, _, anchor = guide.partition("#")
    assert anchor in _anchors(ROOT / file_part), guide


# -- the diagnosis of a loaded stop (PR #470 R1-F1) ------------------------------------------


def _remedy_steps(package: dict) -> list[str]:
    return [s["text"] for s in package["next"] if not s["command"]]


def test_refused_checks_291_are_diagnosed_from_cached_status_as_a_permission_problem(
    tmp_state_dir, status_cli, capsys
):
    """A later 'status', reading nothing from GitHub, says what #291's stop is: GitHub
    refused the credential the check runs, a permission problem, never a pass; the
    next steps are the credential's remedy and 'doctor''s checks row, then unblock,
    never "fix the cause the reason names"."""
    eng = _checks_refused(tmp_state_dir)
    assert load_state(eng.paths.state_file).stop_code == "github-checks-refused"
    reads = len(eng.github.calls)
    package = _recovery(status_cli, eng, capsys)
    diagnosis = package["diagnosis"]
    assert (diagnosis["code"], diagnosis["family"], diagnosis["freshness"]) == (
        "github-checks-refused",
        "github",
        "snapshot",
    )
    assert diagnosis["observed_at"] == package["snapshot_at"]
    assert "refused this credential the PR's check runs" in diagnosis["meaning"]
    assert "permission problem" in diagnosis["meaning"] and "never a pass" in diagnosis["meaning"]
    assert "'gh auth login'" in diagnosis["remedy"] and "'repo' scope" in diagnosis["remedy"]
    assert "Checks: read" in diagnosis["remedy"]
    assert diagnosis["remedy"] in _remedy_steps(package)
    assert "fix the cause the reason names" not in json.dumps(package)
    prefix = _prefix(eng)
    commands = _commands(package)
    assert commands[:3] == [
        f"{prefix} doctor",
        f"{prefix} status --live",
        f'{prefix} unblock --reason "..."',
    ]
    assert "unknown" not in [c["key"] for c in package["unknown"]]
    rc, out, _ = status_cli(eng, "status", capsys=capsys)
    assert "  diagnosis: [snapshot] github-checks-refused (github): GitHub refused" in out
    assert eng.github.calls[reads:] == [c for c in eng.github.calls[reads:] if "checks" not in c]


def test_an_exhausted_review_loop_is_diagnosed_from_cached_status_with_its_remedy(
    tmp_state_dir, status_cli, capsys
):
    """The review cap's remedy without --live: raise workflow.max_review_rounds first,
    since a plain 'unblock' refuses while the cap holds, which the package forbids
    under the configuration loaded; under a raised one it says a round is allowed."""
    eng = _review_cap(tmp_state_dir)
    package = _recovery(status_cli, eng, capsys)
    diagnosis = package["diagnosis"]
    assert (diagnosis["code"], diagnosis["freshness"]) == ("review-round-cap", "snapshot")
    assert "reached workflow.max_review_rounds with findings still open" in diagnosis["meaning"]
    remedy = diagnosis["remedy"]
    assert "raise workflow.max_review_rounds in the configuration first" in remedy
    assert remedy in _remedy_steps(package)
    [cap] = [c for c in package["forbidden"] if c["key"] == "unblock-review-cap"]
    assert cap["freshness"] == "configuration" and "it refuses" in cap["text"]
    rc, out, _ = status_cli(eng, "unblock", "--dry-run", "--reason", "x", capsys=capsys)
    assert "would stay BLOCKED" in out and "Raise 'workflow.max_review_rounds'" in out

    state = load_state(eng.paths.state_file)
    raised = Policy(
        max_total_steps=300,
        max_review_rounds=5,
        allow_merge=False,
        pr_body_approvals=True,
        acceptance_approvals=True,
    )
    package = build_package(
        classify_state(state), state, commands=_commands_of(eng), policy=raised
    ).as_dict()
    assert "unblock-review-cap" not in _keys(package["forbidden"])
    assert any("allows another round" in t for t in _remedy_steps(package))


@pytest.mark.parametrize("saved", ["", "unknown"])
def test_a_stop_saved_with_no_code_is_diagnosed_unknown_and_says_why(tmp_state_dir, saved):
    """Before protocol 16 no code was saved, and a sink may name none: the diagnosis is
    'unknown', with which of the two it is, and the reason's text is never read."""
    eng = make_engine(tmp_state_dir, [])
    eng.state.phase = Phase.BLOCKED
    eng.state.block_reason = "github-checks-refused: the review-round-cap was reached"
    eng.state.stop_code = saved
    stop = classify_state(eng.state)
    package = build_package(stop, eng.state, commands=_commands_of(eng), policy=_POLICY)
    diagnosis = package.diagnosis
    assert (diagnosis["code"], diagnosis["freshness"], diagnosis["observed_at"]) == (
        "unknown",
        "unknown",
        None,
    )
    why = "before protocol 16" if not saved else "the sink that entered it named none"
    [code] = [c for c in package.unknown if c.key == "code"]
    assert why in code.text
    assert ("protocol 16" in diagnosis["source"]) is (not saved)


def test_every_code_an_engine_stop_sink_names_has_a_diagnosis():
    """A new code a BLOCKED or FAILED sink names gets its meaning and remedy here, or
    this fails: the package never falls back to 'fix the cause the reason names'."""
    named = {
        StopReasonCode[name]
        for name in re.findall(
            r"StopReasonCode\.([A-Z_]+)", (SRC / "engine.py").read_text(encoding="utf-8")
        )
    }
    named |= {StopReasonCode.AGENT_BLOCKED, StopReasonCode.AGENT_FAILURE}
    assert named - set(DIAGNOSES) == set()
    for code, diagnosis in DIAGNOSES.items():
        assert diagnosis.meaning and diagnosis.remedy, code
        assert "the reason names" not in diagnosis.meaning, code


# -- replan-refused, with a journal or without one (PR #470 R2-F3) ------------------------------


def _replan_policy_refused(tmp_state_dir):
    """``tests/test_replan.py::test_replan_limit_blocks_through_engine``: the issue used
    every replan it may, and the next round with findings blocks in REVIEW, before any
    replan transaction begins."""
    from autoforge.loop_guard import RESULT_NEEDS_FIX, review_record

    gh = FakeGitHub()
    finding = {
        "id": "R2-F1",
        "classification": "non-blocked",
        "required_resolution": "add a regression test",
    }

    def reviewer(req):
        assert req.phase == "REVIEW"
        return block(review_result(2, SHA_A, [finding]))

    eng = make_engine(tmp_state_dir, reviewer, github=gh)
    eng.config.review.replan.soft_threshold = 2
    gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
    eng.state.phase = Phase.REVIEW
    eng.state.current_pr_url = PR
    eng.state.current_head_sha = SHA_A
    eng.state.review_round = 1
    eng.state.escalation_count = eng.config.review.replan.max_replans_per_issue
    eng.state.review_history = [
        review_record(1, SHA_A, RESULT_NEEDS_FIX, [dict(finding, id="R1-F1")])
    ]
    eng._save()
    assert eng.step().next_phase == "BLOCKED"
    assert "replan_limit_exceeded" in eng.state.block_reason
    assert (eng.state.stop_code, eng.state.replan_transaction) == ("replan-refused", {})
    return eng


def test_a_replan_policy_refusal_with_no_journal_is_not_diagnosed_as_one(
    tmp_state_dir, status_cli, capsys
):
    """The policy refused the replacement before any transaction began: the diagnosis
    names no journal, nothing forbids the plain unblock a journal would refuse, and the
    steps lead to it. Run as displayed, it re-enters FIX as 'status --live' previews."""
    eng = _replan_policy_refused(tmp_state_dir)
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys, "--live")
    diagnosis = package["diagnosis"]
    assert (diagnosis["code"], diagnosis["family"]) == ("replan-refused", "bound")
    assert diagnosis["meaning"] == REPLAN_POLICY_REFUSED.meaning
    assert diagnosis["remedy"] == REPLAN_POLICY_REFUSED.remedy
    text = diagnosis["meaning"] + diagnosis["remedy"]
    assert "recorded in the replan journal" not in text and "'unblock' refuses" not in text
    assert "unblock-replan" not in _keys(package["forbidden"])
    assert "replan-journal" not in _keys(package["preserve"])
    assert package["guide"] == GUIDE_BLOCKED
    assert (package["live"]["decision"], package["live"]["target_phase"]) == (
        "would_reenter",
        "FIX",
    )
    sequence = _sequence(package)
    assert sequence[:4] == [
        ("", False),
        (f"{prefix} status --live", False),
        (f'{prefix} unblock --reason "..."', False),
        (f"{prefix} resume", False),
    ]
    assert package["next"][0]["text"] == REPLAN_POLICY_REFUSED.remedy
    rc, out, err = _run_displayed(status_cli, eng, sequence[2][0], capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err


def test_a_replan_refusal_with_its_journal_keeps_the_journals_diagnosis(tmp_state_dir):
    """The same code with a rejected transaction on record: the journal's diagnosis, and
    the plain unblock it refuses is forbidden."""
    from autoforge.replan_txn import ReplanStage, ReplanTransaction

    eng = _replan_policy_refused(tmp_state_dir)
    state = load_state(eng.paths.state_file)
    state.replan_transaction = ReplanTransaction(
        stage=ReplanStage.REJECTED,
        issue_url=ISSUE,
        decision_pr_url=PR,
        decision_head_sha=SHA_A,
        decision_branch=BRANCH,
        decision_base_ref="main",
        decision_merge_base_sha=MERGE_BASE,
        escalation={"trigger": "hard_review_round_threshold"},
        rejection_reason="refused by verification",
    ).to_dict()
    package = build_package(
        classify_state(state), state, commands=_commands_of(eng), policy=_POLICY
    )
    assert package.diagnosis["meaning"] == DIAGNOSES[StopReasonCode.REPLAN_REFUSED].meaning
    assert "recorded in the replan journal" in package.diagnosis["meaning"]
    assert "unblock-replan" in [c.key for c in package.forbidden]
    assert package.guide == GUIDE_REPLAN


def test_an_agents_blocked_fix_offers_a_rereview_and_protects_its_unsaved_candidate(
    tmp_state_dir, status_cli, capsys
):
    """The fixer's own 'blocked' after it committed: the diagnosis is the agent's claim,
    the candidate is unknown and its worktree protected, and 'unblock --rereview' is the
    way out for findings resolved outside the repository."""
    gh = FakeGitHub()
    stopped = {"phase": "FIX", "status": "blocked", "message": "need a decision"}

    def committing_then_blocked(req):
        from tests.conftest import fix_commit

        fix_commit(req, "work in progress")
        return block(stopped)

    eng = _operating(tmp_state_dir, gh, committing_then_blocked)
    assert eng.step().next_phase == "BLOCKED"
    package = _recovery(status_cli, eng, capsys)
    assert package["diagnosis"]["code"] == "agent-blocked"
    assert "candidate" in _keys(package["unknown"])
    prefix = _prefix(eng)
    [rereview] = [s for s in package["next"] if "--rereview" in s["command"]]
    assert rereview["command"] == f'{prefix} unblock --rereview --reason "..."'
    assert rereview["alternative"]


# -- one sequence the engine accepts, for every operator action (PR #470 R1-F2) -------------


def _run_displayed(status_cli, eng, command: str, capsys) -> tuple[int, str, str]:
    """Run a command exactly as the package displays it, as the operator would type it."""
    words = shlex.split(command)
    prefix = shlex.split(_prefix(eng))
    assert words[: len(prefix)] == prefix, command
    return status_cli(eng, *words[len(prefix) :], capsys=capsys)


def _sequence(package: dict) -> list[tuple[str, bool]]:
    return [(s["command"], s["alternative"]) for s in package["next"]]


def test_k10_steps_are_one_sequence_whose_bindings_go_in_one_unblock(
    tmp_state_dir, status_cli, capsys
):
    """Each action is read and posted; then one unblock, the plain one or, in its place,
    one that binds every comment bound; then 'resume'; withdrawing is the alternative
    to all of it. No unblock follows another, since an applied one leaves BLOCKED."""
    eng = _k10_blocked(tmp_state_dir)
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    sequence = [c for c in _sequence(package) if not c[0].startswith(f"{prefix} run ")]
    assert sequence == [
        (f"{prefix} status --action {x16}", False),
        (f"{prefix} status --action {x16} | gh issue comment {T16} --body-file -", False),
        (f"{prefix} status --action {x158}", False),
        (f"{prefix} status --action {x158} | gh issue comment {T158} --body-file -", False),
        (f'{prefix} unblock --reason "..."', False),
        (
            f"{prefix} unblock --bind-evidence {x16} <comment URL> --bind-evidence {x158} "
            '<comment URL> --reason "..."',
            True,
        ),
        (f"{prefix} resume", False),
        (
            f'{prefix} unblock --decline-action {x16} --decline-action {x158} --reason "..."',
            True,
        ),
    ]
    bind = next(s for s in package["next"] if "--bind-evidence" in s["command"])
    assert "in place of that unblock" in bind["text"] and "one command" in bind["text"]


def test_the_displayed_k10_sequence_runs_against_the_engine_posting_one_binding_another(
    tmp_state_dir, status_cli, capsys
):
    """Mixed posting and binding, exactly as displayed: the operator posts one payload
    and binds an existing comment for the other in the one bind command; FIX then
    verifies both and completes the plan. The sequence shown before (a bind, then a
    plain unblock) is refused at its second unblock, which is why it is no longer shown."""
    eng = _k10_blocked(tmp_state_dir)
    x16 = _k10(eng, T16)
    package = _recovery(status_cli, eng, capsys)
    eng.github.add_comment(T16, 7016, x16.body)  # what the displayed pipe posts
    c158 = eng.github.add_comment(T158, 7158, "Notes of 2026-11-01: workers settle per issue.")
    [bind] = [s["command"] for s in package["next"] if "--bind-evidence" in s["command"]]
    command = bind.replace(f" --bind-evidence {x16.action_id} <comment URL>", "").replace(
        "<comment URL>", c158.url
    )
    rc, out, err = _run_displayed(status_cli, eng, command, capsys)
    assert rc == 0, err
    assert "BLOCKED -> FIX" in out
    [plain] = [
        s["command"] for s in package["next"] if s["command"].endswith('unblock --reason "..."')
    ]
    rc, _, err = _run_displayed(status_cli, eng, plain, capsys)
    assert rc == 2 and "not BLOCKED" in err
    eng.load()
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    s = load_state(eng.paths.state_file)
    [resolution] = s.last_fix_resolutions
    assert {e["evidence"] for e in resolution["operator_evidence"]} == {"exact", "bound"}


def test_the_displayed_k10_sequence_runs_against_the_engine_posting_both(
    tmp_state_dir, status_cli, capsys
):
    eng = _k10_blocked(tmp_state_dir)
    package = _recovery(status_cli, eng, capsys)
    for cid, target in ((8016, T16), (8158, T158)):
        eng.github.add_comment(target, cid, _k10(eng, target).body)
    [plain] = [
        s["command"]
        for s in package["next"]
        if s["command"].endswith('unblock --reason "..."') and not s["alternative"]
    ]
    rc, out, err = _run_displayed(status_cli, eng, plain, capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    eng.load()
    assert eng.step().next_phase == "REVIEW"


def _k10_then_k12(tmp_state_dir):
    """Operator actions for R1-F1 and a PR description update awaiting approval for
    R1-F2 in one FIX plan: the K10 prefix first, the K12 after it."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir,
        gh,
        scripted(fixer(BOTH, pr_body_update("R1-F2"))),
        policy="approve",
        findings=2,
    )
    gh.add_issue(ISSUE, "Ship the feature", body=ISSUE_BODY)
    gh.add_issue(T16, "Scheduler", state="CLOSED")
    gh.add_issue(T158, "Worker")
    gh.add_issue(EPIC, "EPIC")
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    return eng


def test_a_wait_after_the_operator_actions_is_left_to_the_next_stop(
    tmp_state_dir, status_cli, capsys
):
    """K10 then K12: the steps are the operator actions' sequence, up to its 'resume',
    then 'status' again for the update's stop; no approval or decline of the update is
    offered before, and its decline is forbidden until the actions are verified."""
    eng = _k10_then_k12(tmp_state_dir)
    update = _k12(eng).action_id
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    commands = _commands(package)
    assert not [c for c in commands if update in c and "status --action" not in c]
    resume = commands.index(f"{prefix} resume")
    assert commands[resume + 2] == f"{prefix} status" or commands[resume + 1] == f"{prefix} status"
    [later] = [s for s in package["next"] if s["command"] == f"{prefix} status"]
    assert update in later["text"] and "none of its commands applies before" in later["text"]
    assert f"decline-{update}" in _keys(package["forbidden"])
    assert [p["action_id"] for p in package["pending"] if p["kind"] == "pr_body_update"] == [update]


# -- a FIX candidate no record names (PR #470 R1-F3) ----------------------------------------


def test_a_fix_that_stopped_before_its_plan_protects_the_committed_candidate(
    tmp_state_dir, status_cli, capsys
):
    """_apply_fix blocks on an operator action target it cannot read, after the fixer
    committed and before anything is planned: no push record names the candidate, so
    the package says it is unknown, keeps the worktree from being reset, and warns a
    new run that it works in the same worktree."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    reviewed = eng.state.reviewed_head_sha
    gh.get_issue_errors[T158] = GitHubError("HTTP 403: Resource not accessible")
    assert eng.step().next_phase == "BLOCKED"
    saved = load_state(eng.paths.state_file)
    assert saved.effect_records == [] and saved.completion_context == {}
    worktree = eng.agent_worktree_path()
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head != reviewed, "the fixer's commit is in the worktree and nowhere else"

    package = _recovery(status_cli, eng, capsys)
    assert package["diagnosis"]["code"] == "github-read-failed"
    [candidate] = [c for c in package["unknown"] if c["key"] == "candidate"]
    assert candidate["freshness"] == "unknown" and reviewed in candidate["text"]
    [kept] = [c for c in package["preserve"] if c["key"] == "worktree"]
    assert kept["freshness"] == "snapshot"
    assert "may hold a FIX candidate no saved record names" in kept["text"]
    assert "do not reset, clean or remove it" in kept["text"]
    assert f"git log --oneline {reviewed}..HEAD" in kept["text"]
    [fresh] = [s for s in package["next"] if " run --force " in s["command"]]
    assert f"keep whatever the fixer committed past the reviewed HEAD {reviewed}" in fresh["text"]


def test_a_saved_fix_plan_names_its_candidate_and_no_unknown_one(tmp_state_dir, status_cli, capsys):
    eng = _k10_blocked(tmp_state_dir)
    package = _recovery(status_cli, eng, capsys)
    assert "candidate" not in _keys(package["unknown"])
    [kept] = [c for c in package["preserve"] if c["key"] == "worktree"]
    assert _push_sha(eng) in kept["text"]


# -- redaction before every bound (PR #470 R1-F4) ------------------------------------------


# A credential whose shape needs its full length (a Google API key, "AIza" and 35
# characters): cut short it no longer matches the redactor (PR #470 R1-F4).
_TOKEN = GOOGLE_API_KEY
_TOKEN_BODY = GOOGLE_KEY_BODY
#: How many of the key's 39 characters fall before a cut: at least eight of its body,
#: so a cut made before redacting would leave that much in the output.
_INSIDE = [12, 20, 30, 38]


def _no_token_part(text: str) -> None:
    """No run of the token's body long enough to identify it survives anywhere."""
    assert _TOKEN_BODY[:8] not in text, text[text.find(_TOKEN_BODY[:8]) - 40 :][:120]


def _straddling(prefix_chars: int, cut: int, inside: int, fill: str) -> str:
    """A message whose token starts ``inside`` characters before ``cut``, counted in a
    text with ``prefix_chars`` characters before the message."""
    filler = fill * (cut - inside - prefix_chars - 1) + " "
    return f"{filler}{_TOKEN} and more text after it"


@pytest.mark.parametrize("inside", _INSIDE)
@pytest.mark.parametrize("flags", [(), ("--json",)])
def test_a_credential_across_the_live_errors_cut_is_redacted_whole(
    tmp_state_dir, status_cli, capsys, inside, flags
):
    """A transient read failure is quoted up to cli.MAX_LIVE_ERROR_CHARS: a credential
    crossing that cut is redacted before it, in both output modes."""
    eng = _checks_refused(tmp_state_dir)
    message = _straddling(0, cli.MAX_LIVE_ERROR_CHARS, inside, "x")
    assert message.index(_TOKEN) + inside == cli.MAX_LIVE_ERROR_CHARS
    eng.github.get_pr_error = GitHubUnavailableError(message)
    rc, out, err = status_cli(eng, "status", "--live", *flags, capsys=capsys)
    assert rc == 0, err
    assert "not decided: reading GitHub failed" in out
    _no_token_part(out + err)


@pytest.mark.parametrize("inside", _INSIDE)
@pytest.mark.parametrize("flags", [(), ("--json",)])
def test_a_credential_across_the_packages_cut_is_redacted_whole(
    tmp_state_dir, status_cli, capsys, inside, flags
):
    """A conclusive read failure is unblock's refusal, quoted in the decision's detail up
    to MAX_TEXT_CHARS: a credential crossing that cut is redacted before it, in both
    output modes."""
    eng = _checks_refused(tmp_state_dir)
    head = f"PR {PR} cannot be read: "
    message = _straddling(len(head), MAX_TEXT_CHARS - 1, inside, "y")
    assert len(head) + message.index(_TOKEN) + inside == MAX_TEXT_CHARS - 1
    eng.github.get_pr_error = GitHubError(message)
    rc, out, err = status_cli(eng, "status", "--live", *flags, capsys=capsys)
    assert rc == 0, err
    assert "would stay BLOCKED" in out or '"would_stay_blocked"' in out
    _no_token_part(out + err)


def test_the_bounds_redact_before_they_cut():
    """Both bounds, directly: a token cut anywhere inside leaves none of its body."""
    from autoforge.recovery_package import _clip

    for inside in range(12, 39):
        clipped = _clip(_straddling(0, MAX_TEXT_CHARS - 1, inside, "z"))
        assert len(clipped) <= MAX_TEXT_CHARS
        _no_token_part(clipped)
        bounded = cli._bounded_error(
            GitHubError(_straddling(0, cli.MAX_LIVE_ERROR_CHARS, inside, "z"))
        )
        _no_token_part(bounded)


# Where the planted key starts in the refused code's ``repr``: across the 80 characters
# the loader's refusal once cut that ``repr`` to before redacting it (PR #470 R2-F1).
_STOP_CODE_KEY_STARTS = [42, 50, 60, 68]


@pytest.mark.parametrize("start", _STOP_CODE_KEY_STARTS)
@pytest.mark.parametrize(
    "flags", [(), ("--json",), ("--live",), ("--live", "--json")], ids=" ".join
)
def test_a_credential_in_a_refused_stop_code_never_reaches_status(
    tmp_state_dir, status_cli, capsys, start, flags
):
    """A hand-planted ``stop_code`` of no code's shape is refused at load by every status
    variant, and the refusal on stderr quotes it redacted whole before it is bounded: a
    fixed-length credential crossing the bound leaves none of its body. The refused state
    is left byte for byte as it was, and nothing is read from GitHub."""
    eng = _checks_refused(tmp_state_dir)
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    code = "x" * (start - 2) + " " + _TOKEN + " and more after it" * 4
    assert repr(code).index(_TOKEN) == start
    data["stop_code"] = code
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    before = state_tree(tmp_state_dir)
    calls = len(eng.github.calls)
    rc, out, err = status_cli(eng, "status", *flags, capsys=capsys)
    assert rc == 2 and out == "", out
    assert "state field 'stop_code' must be a reason code" in err
    assert "AIza" not in err, err
    _no_token_part(out + err)
    assert state_tree(tmp_state_dir) == before
    assert len(eng.github.calls) == calls


# -- a verified PR description edited since (PR #470 R1-F5) --------------------------------


def _k12_verified_then_edited(tmp_state_dir, *, later_write: bool = False):
    gh = FakeGitHub()
    if later_write:
        eng = _mixed(
            tmp_state_dir,
            gh,
            fixed("R1-F1"),
            pr_body_update("R1-F2"),
            new_follow_up("R1-F3"),
            findings=3,
        )
        kind = EffectKind.FOLLOW_UP_ISSUE
    else:
        eng = _mixed(tmp_state_dir, gh)
        kind = EffectKind.PR_BODY_UPDATE
    edited = _payload(eng.state.run_id, A_HUMAN_EDIT)
    _edit_once_saved(eng, gh, kind, edited)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    return eng


def test_a_verified_description_edited_since_offers_restore_or_decline(
    tmp_state_dir, status_cli, capsys
):
    """The update is observed, the description edited after it, and the run stopped with
    effect-conflict: the package lists the update as pending again, with the restore
    (its payload, a plain unblock, 'resume') and the decline 'unblock' accepts; and the
    decline, run as displayed, keeps the edit and re-enters FIX."""
    eng = _k12_verified_then_edited(tmp_state_dir)
    record = _k12(eng)
    action = record.action_id
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    assert package["diagnosis"]["code"] == "effect-conflict"
    [pending] = [p for p in package["pending"] if p["action_id"] == action]
    assert (pending["stage"], pending["state"]) == ("observed", "changed_after_verification")
    sequence = [c for c in _sequence(package) if not c[0].startswith(f"{prefix} run ")]
    assert sequence == [
        (f"{prefix} status --action {action}", False),
        ("", False),
        (f'{prefix} unblock --reason "..."', False),
        (f"{prefix} resume", False),
        (f'{prefix} unblock --decline-action {action} --reason "..."', True),
    ]
    assert package["guide"] == GUIDE_PR_BODY
    [kept] = [c for c in package["preserve"] if c["key"] == f"evidence-{action}"]
    assert "it changed since" in kept["text"]
    decline = sequence[-1][0]
    rc, out, err = _run_displayed(status_cli, eng, decline, capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    assert eng.github.prs[PR].body == _payload(eng.state.run_id, A_HUMAN_EDIT)


def test_a_verified_description_edited_since_restored_completes_as_displayed(
    tmp_state_dir, status_cli, capsys
):
    eng = _k12_verified_then_edited(tmp_state_dir)
    package = _recovery(status_cli, eng, capsys)
    rc, out, _ = status_cli(eng, "status", "--action", _k12(eng).action_id, capsys=capsys)
    eng.github.prs[PR].body = out  # set back to exactly the payload
    [plain] = [
        s["command"]
        for s in package["next"]
        if s["command"].endswith('unblock --reason "..."') and not s["alternative"]
    ]
    rc, out, err = _run_displayed(status_cli, eng, plain, capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    eng.load()
    assert eng.step().next_phase == "REVIEW"


def test_an_edited_description_cannot_be_declined_once_a_later_write_is_out(
    tmp_state_dir, status_cli, capsys
):
    """A follow-up issue of the plan went out after the update was verified: a decline
    would abandon it, so the package forbids it and offers only the restore."""
    eng = _k12_verified_then_edited(tmp_state_dir, later_write=True)
    action = _k12(eng).action_id
    package = _recovery(status_cli, eng, capsys)
    assert not [c for c in _commands(package) if "--decline-action" in c]
    [forbidden] = [c for c in package["forbidden"] if c["key"] == f"decline-{action}"]
    assert "later write of its plan is out" in forbidden["text"]
    rc, out, err = status_cli(
        eng, "unblock", "--dry-run", "--decline-action", action, "--reason", "x", capsys=capsys
    )
    assert rc == 0, err
    assert "would stay BLOCKED" in out and f"action {action} cannot be declined" in out


# -- a description update put in conflict, approved or not (PR #470 R2-F2) ---------------------


def _k12_conflicted(tmp_state_dir, policy: str, *, off_before_the_stop: bool = False):
    """K12 in conflict through an edit after its plan was saved: the process dies once the
    plan is on disk, a human edits the description, and the resumed FIX finds it neither
    the plan's base nor its payload. Under ``approve`` the update was never approved;
    under ``auto`` the policy recorded with the plan authorized it. With
    ``off_before_the_stop`` the operator set ``safety.pr_body_updates: off`` before the
    resumed FIX stopped."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)), policy=policy
    )
    save = eng._save

    def dying(*args, **kwargs):
        save(*args, **kwargs)
        records = load_state(eng.paths.state_file).effect_records
        if any(r["kind"] == EffectKind.PR_BODY_UPDATE.value for r in records):
            eng._save = save
            raise KeyboardInterrupt

    eng._save = dying
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    gh.prs[PR].body = _base(eng.state.run_id, A_HUMAN_EDIT)
    if off_before_the_stop:
        _pr_body_off(eng)
    eng.load()
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    record = _k12(eng)
    assert record.stage is Stage.CONFLICT
    assert record.authorized is (policy == "auto")
    return eng


def _pr_body_off(eng) -> None:
    """The operator sets ``safety.pr_body_updates: off`` after the plan was saved."""
    from dataclasses import replace

    eng.config.safety = replace(eng.config.safety, pr_body_updates="off")


def test_an_unapproved_update_in_conflict_offers_its_payload_or_its_base_and_approval(
    tmp_state_dir, status_cli, capsys
):
    """Never approved, so a description put back to the base still awaits approval and a
    plain unblock over it is refused. The plain path is the payload's; the base's is
    the approval of exactly this id, in place of it. Run as displayed: back to the base,
    the approval re-enters FIX, which writes the payload and completes the plan."""
    eng = _k12_conflicted(tmp_state_dir, "approve")
    action = _k12(eng).action_id
    base = _k12(eng).precondition["base_sha256"]
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    assert package["diagnosis"]["code"] == "effect-conflict"
    sequence = [c for c in _sequence(package) if not c[0].startswith(f"{prefix} run ")]
    assert sequence == [
        (f"{prefix} status --action {action}", False),
        ("", False),
        (f'{prefix} unblock --reason "..."', False),
        (f"{prefix} resume", False),
        (f'{prefix} unblock --approve-action {action} --reason "..."', True),
        (f'{prefix} unblock --decline-action {action} --reason "..."', True),
    ]
    steps = package["next"]
    assert steps[1]["text"] == "set the PR description to exactly that payload"
    assert f"put the PR description back to sha256 {base} and then approve" in steps[4]["text"]
    assert not [s for s in steps if base in s["text"] and not s["alternative"]]
    run = eng.state.run_id
    eng.github.prs[PR].body = _base(run)
    rc, out, err = _run_displayed(status_cli, eng, sequence[2][0], capsys)
    assert rc != 0 or "BLOCKED -> FIX" not in out, out
    assert f"action {action} awaits approval" in out + err
    rc, out, err = _run_displayed(status_cli, eng, sequence[4][0], capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    eng.load()
    assert eng.step().next_phase == "REVIEW"
    assert eng.github.prs[PR].body == _payload(run)


def test_an_unapproved_update_in_conflict_set_to_its_payload_completes_as_displayed(
    tmp_state_dir, status_cli, capsys
):
    eng = _k12_conflicted(tmp_state_dir, "approve")
    package = _recovery(status_cli, eng, capsys)
    [read, _, plain, _] = [s["command"] for s in package["next"] if not s["alternative"]]
    rc, out, _ = _run_displayed(status_cli, eng, read, capsys)
    eng.github.prs[PR].body = out  # set to exactly the payload
    writes = list(eng.github.effect_writes)
    rc, out, err = _run_displayed(status_cli, eng, plain, capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    eng.load()
    assert eng.step().next_phase == "REVIEW"
    assert eng.github.effect_writes == writes  # adopted, never written again


def test_an_authorized_update_in_conflict_restored_to_its_base_completes_as_displayed(
    tmp_state_dir, status_cli, capsys
):
    """Authorized by the policy saved with the plan: put back to the base, the plain
    unblock re-enters FIX, which writes the payload; no approval is named."""
    eng = _k12_conflicted(tmp_state_dir, "auto")
    action = _k12(eng).action_id
    base = _k12(eng).precondition["base_sha256"]
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    sequence = [c for c in _sequence(package) if not c[0].startswith(f"{prefix} run ")]
    assert sequence == [
        (f"{prefix} status --action {action}", False),
        ("", False),
        (f'{prefix} unblock --reason "..."', False),
        (f"{prefix} resume", False),
        (f'{prefix} unblock --decline-action {action} --reason "..."', True),
    ]
    assert base in package["next"][1]["text"]
    run = eng.state.run_id
    eng.github.prs[PR].body = _base(run)
    rc, out, err = _run_displayed(status_cli, eng, sequence[2][0], capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    eng.load()
    assert eng.step().next_phase == "REVIEW"
    assert eng.github.prs[PR].body == _payload(run)


@pytest.mark.parametrize("stage", ["awaiting_approval", "conflict"])
def test_no_approval_is_offered_while_pr_body_updates_is_off(
    tmp_state_dir, status_cli, capsys, stage
):
    """'unblock' records no approval of a description update under ``off``: the package
    names none and forbids it, and the payload set by hand, then a plain unblock, is
    the way on (or the decline, or the configuration change that makes the approval
    possible, PR #470 R3-F1), as the engine accepts."""
    if stage == "conflict":
        eng = _k12_conflicted(tmp_state_dir, "approve")
    else:
        eng = _k12_blocked(tmp_state_dir)
    _pr_body_off(eng)
    action = _k12(eng).action_id
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    [pending] = [p for p in package["pending"] if p["action_id"] == action]
    assert pending["state"] == stage
    assert not [c for c in _commands(package) if "--approve-action" in c]
    [forbidden] = [c for c in package["forbidden"] if c["key"] == f"approve-{action}"]
    assert "safety.pr_body_updates is off" in forbidden["text"]
    assert forbidden["freshness"] == "configuration"
    sequence = [c for c in _sequence(package) if not c[0].startswith(f"{prefix} run ")]
    assert sequence == [
        (f"{prefix} status --action {action}", False),
        ("", False),
        (f'{prefix} unblock --reason "..."', False),
        (f"{prefix} resume", False),
        (f"{prefix} status", True),
        (f'{prefix} unblock --decline-action {action} --reason "..."', True),
    ]
    configure = package["next"][4]["text"]
    assert "set safety.pr_body_updates to 'approve' in the configuration" in configure
    if stage == "conflict":
        eng.github.prs[PR].body = _base(eng.state.run_id)
    rc, out, err = status_cli(
        eng, "unblock", "--dry-run", "--approve-action", action, "--reason", "x", capsys=capsys
    )
    assert "would stay BLOCKED" in out and "cannot be approved while" in out, out + err
    rc, out, _ = _run_displayed(status_cli, eng, sequence[0][0], capsys)
    eng.github.prs[PR].body = out
    rc, out, err = _run_displayed(status_cli, eng, sequence[2][0], capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err


def test_a_verified_update_with_no_saved_code_is_offered_conditionally(tmp_state_dir):
    """Before protocol 16 nothing says the edit is why it stopped: the step is conditional
    on what 'status --live' reads, and offers the decline only where unblock allows it."""
    eng = _k12_verified_then_edited(tmp_state_dir)
    state = load_state(eng.paths.state_file)
    state.stop_code = ""
    package = build_package(
        classify_state(state), state, commands=_commands_of(eng), policy=_POLICY
    )
    action = _k12(eng).action_id
    assert not [p for p in package.pending if p.action_id == action]
    [step] = [s for s in package.next if "if 'status --live' says" in s.text]
    assert f"--decline-action {action}" in step.text


# -- an acceptance action under 'off' (PR #470 R3-F1) ------------------------------------------


def _acceptance_off(eng) -> None:
    """The operator sets ``safety.acceptance_actions: off`` after the plan was saved."""
    eng.config.safety = replace(eng.config.safety, acceptance_actions="off")


@pytest.mark.parametrize("way", ["post", "decline", "configure"])
def test_no_acceptance_approval_is_offered_while_acceptance_actions_is_off(
    tmp_state_dir, status_cli, capsys, way
):
    """'unblock' records no approval of an acceptance action under ``off``: the package
    names none and forbids it. Each way it displays instead runs against the engine:
    the payload posted by the displayed pipe, then the plain unblock, completes the
    plan with no comment of the controller's; the decline drops it; and the
    configuration change makes 'status' name the approval, which then applies."""
    eng = _k9_blocked(tmp_state_dir)
    _acceptance_off(eng)
    record = _k9(eng)
    action = record.action_id
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    [pending] = [p for p in package["pending"] if p["action_id"] == action]
    assert pending["state"] == "awaiting_approval"
    assert not [c for c in _commands(package) if "--approve-action" in c]
    [forbidden] = [c for c in package["forbidden"] if c["key"] == f"approve-{action}"]
    assert "safety.acceptance_actions is off" in forbidden["text"]
    assert forbidden["freshness"] == "configuration"
    read = f"{prefix} status --action {action}"
    plain = f'{prefix} unblock --reason "..."'
    decline = f'{prefix} unblock --decline-action {action} --reason "..."'
    sequence = [c for c in _sequence(package) if not c[0].startswith(f"{prefix} run ")]
    assert sequence == [
        (read, False),
        (f"{read} | gh issue comment {ISSUE} --body-file -", False),
        (plain, False),
        (f"{prefix} resume", False),
        (f"{prefix} status", True),
        (decline, True),
    ]
    configure = package["next"][4]["text"]
    assert "set safety.acceptance_actions to 'approve' in the configuration" in configure
    rc, out, err = status_cli(
        eng, "unblock", "--dry-run", "--approve-action", action, "--reason", "x", capsys=capsys
    )
    assert "would stay BLOCKED" in out and "cannot be approved while" in out, out + err
    if way == "post":
        rc, out, _ = _run_displayed(status_cli, eng, read, capsys)
        posted = eng.github.add_comment(ISSUE, 600, out)  # what the displayed pipe posts
        rc, out, err = _run_displayed(status_cli, eng, plain, capsys)
        assert rc == 0 and "BLOCKED -> FIX" in out, err
        eng.load()
        assert eng.step().next_phase == "REVIEW"
        assert eng.github.comments[ISSUE] == [posted] and eng.github.effect_writes == []
    elif way == "decline":
        rc, out, err = _run_displayed(status_cli, eng, decline, capsys)
        assert rc == 0 and "BLOCKED -> FIX" in out, err
        assert load_state(eng.paths.state_file).effect_records == []
    else:
        eng.config.safety = replace(eng.config.safety, acceptance_actions="approve")
        approve = f'{prefix} unblock --approve-action {action} --reason "..."'
        package = _recovery(status_cli, eng, capsys)
        assert approve in _commands(package)
        assert f"approve-{action}" not in _keys(package["forbidden"])
        rc, out, err = _run_displayed(status_cli, eng, approve, capsys)
        assert rc == 0 and "BLOCKED -> FIX" in out, err
        eng.load()
        assert eng.step().next_phase == "REVIEW"
        [comment] = eng.github.comments[ISSUE]
        assert comment.body == record.body


# -- operator actions with one in conflict (PR #470 R3-F2) -----------------------------------


def _k10_conflicted(tmp_state_dir):
    """K10 for #16 and #158, #16's posted with other bytes before the plan was saved."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    _post(gh, T16, 9016, payload(T16, NOTE16) + " ")
    assert eng.step().next_phase == "BLOCKED"
    assert _k10(eng, T16).stage is Stage.CONFLICT
    return eng


@pytest.mark.parametrize("repair", ["exact", "removed"])
def test_conflicted_operator_actions_are_offered_no_decline(
    tmp_state_dir, status_cli, capsys, repair
):
    """'unblock' refuses every decline of the plan's operator actions while one is in
    conflict, even once its comment is repaired or removed, so the package names none
    and forbids it. The repair it displays runs against the engine: the conflict
    repaired to the exact payload and the other action posted, the plain unblock
    completes the plan; removed, the plain unblock clears the saved conflict, the run
    stops on the actions again, and that stop's package offers the decline, which
    applies."""
    eng = _k10_conflicted(tmp_state_dir)
    x16, x158 = _k10(eng, T16), _k10(eng, T158)
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    assert not [c for c in _commands(package) if "--decline-action" in c]
    [forbidden] = [c for c in package["forbidden"] if c["key"] == "decline-operator-actions"]
    assert x16.action_id in forbidden["text"] and "repaired, never declined" in forbidden["text"]
    assert f"bind-{x16.action_id}" in _keys(package["forbidden"])
    pipe158 = f"{prefix} status --action {x158.action_id} | gh issue comment {T158} --body-file -"
    plain = f'{prefix} unblock --reason "..."'
    assert pipe158 in _commands(package) and plain in _commands(package)
    [comment] = eng.github.comments[T16]
    if repair == "exact":
        eng.github.comments[T16] = [replace(comment, body=x16.body)]
    else:
        eng.github.comments[T16] = []
    declines = ("--decline-action", x16.action_id, "--decline-action", x158.action_id)
    rc, out, err = status_cli(
        eng, "unblock", "--dry-run", *declines, "--reason", "x", capsys=capsys
    )
    assert "would stay BLOCKED" in out and "repaired, never declined" in out, out + err
    if repair == "exact":
        rc, out, _ = _run_displayed(
            status_cli, eng, f"{prefix} status --action {x158.action_id}", capsys
        )
        eng.github.add_comment(T158, 9158, out)  # what the displayed pipe posts
        rc, out, err = _run_displayed(status_cli, eng, plain, capsys)
        assert rc == 0 and "BLOCKED -> FIX" in out, err
        eng.load()
        assert eng.step().next_phase == "REVIEW"
        return
    rc, out, err = _run_displayed(status_cli, eng, plain, capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    eng.load()
    assert eng.step().next_phase == "BLOCKED"
    package = _recovery(status_cli, eng, capsys)
    assert "decline-operator-actions" not in _keys(package["forbidden"])
    decline = (
        f"{prefix} unblock --decline-action {x16.action_id} --decline-action "
        f'{x158.action_id} --reason "..."'
    )
    assert decline in _commands(package)
    rc, out, err = _run_displayed(status_cli, eng, decline, capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err


# -- an authorized acceptance action its issue holds (PR #470 R3-F3) --------------------------


def _k9_held_by_issue(tmp_state_dir, authorization: str, how: str):
    """An authorized K9 held because the run's issue no longer qualifies: by the policy
    (``auto``), or by the operator's approval given before the issue stopped qualifying."""
    gh = FakeGitHub()
    if authorization == "policy":
        eng = _accepting(tmp_state_dir, gh, fixer(acceptance("R1-F1")), policy="auto")
    else:
        eng = _k9_blocked(tmp_state_dir)
        gh = eng.github
        assert eng.unblock(REASON_211, approve_action=_k9(eng).action_id).unblocked
    why = _disqualify(gh, how)
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert why in load_state(eng.paths.state_file).block_reason
    record = _k9(eng)
    assert record.authorized and record.stage is Stage.INTENDED
    return eng


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize(
    ("authorization", "how"), [("policy", "CLOSED"), ("operator", "OPEN-ELSEWHERE")]
)
def test_an_authorized_acceptance_held_by_its_issue_names_eligibility_not_approval(
    tmp_state_dir, status_cli, capsys, authorization, how, live
):
    """Its authorization is satisfied, so the package asks for none and forbids an
    approval or a decline, which 'unblock' refuses; what it asks for is the issue's
    eligibility, before the plain unblock, since a FIX re-entered before stops on the
    same hold. Cached or live alike: the live preview re-enters FIX on the saved plan
    without reading the issue, so whether it qualifies now stays unknown. Run as
    displayed, with the issue qualifying again, the plan completes."""
    eng = _k9_held_by_issue(tmp_state_dir, authorization, how)
    record = _k9(eng)
    action = record.action_id
    prefix = _prefix(eng)
    flags = ("--live",) if live else ()
    package = _recovery(status_cli, eng, capsys, *flags)
    assert (package["outcome"], package["code"]) == ("blocked", "acceptance-action")
    [pending] = [p for p in package["pending"] if p["action_id"] == action]
    granted = (
        "no approval: the policy recorded with the plan authorized it"
        if authorization == "policy"
        else "no further approval: one of exactly this action id is recorded"
    )
    assert pending["state"] == ("authorized_by_policy" if authorization == "policy" else "approved")
    assert pending["required"].startswith(granted)
    assert "none is recorded" not in pending["required"]
    assert "the issue open in the run's repository owner/repo" in pending["required"]
    forbidden = {c["key"]: c for c in package["forbidden"]}
    for verb in ("approve", "decline"):
        assert granted in forbidden[f"{verb}-{action}"]["text"]
    [held] = [c for c in package["proven"] if c["key"] == f"held-{action}"]
    assert ISSUE in held["text"] and held["freshness"] == "snapshot"
    [eligible] = [c for c in package["unknown"] if c["key"] == f"eligible-{action}"]
    assert eligible["freshness"] == "unknown" and "without reading the issue" in eligible["text"]
    assert not [c for c in _commands(package) if f"-action {action}" in c]
    plain = f'{prefix} unblock --reason "..."'
    sequence = [c for c in _sequence(package) if not c[0].startswith(f"{prefix} run ")]
    assert sequence == [("", False), (plain, False), (f"{prefix} resume", False)]
    first = package["next"][0]["text"]
    assert f"reopen {ISSUE} if it is closed, or move it back into owner/repo" in first
    assert "once" in package["next"][1]["text"] and "same hold" in package["next"][1]["text"]
    if live:
        # The preview's ordinary saved-plan decision: no sign of the issue either way.
        assert (package["live"]["decision"], package["live"]["target_phase"]) == (
            "would_reenter",
            "FIX",
        )
    for verb in ("approve", "decline"):
        rc, out, err = status_cli(
            eng, "unblock", "--dry-run", f"--{verb}-action", action, "--reason", "x", capsys=capsys
        )
        assert "would stay BLOCKED" in out and "awaiting: none" in out, out + err
    eng.github.issues[ISSUE] = replace(
        eng.github.issues[ISSUE], state="OPEN", repository="owner/repo"
    )
    rc, out, err = _run_displayed(status_cli, eng, plain, capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    eng.load()
    assert eng.step().next_phase == "REVIEW"
    [comment] = eng.github.comments[ISSUE]
    assert comment.body == record.body


def test_an_authorized_acceptance_with_no_saved_code_is_offered_conditionally(tmp_state_dir):
    """Before protocol 16 nothing says the issue is why it stopped: the step is conditional
    on the reason, and no approval or decline is named for an action already authorized."""
    eng = _k9_held_by_issue(tmp_state_dir, "policy", "CLOSED")
    state = load_state(eng.paths.state_file)
    state.stop_code = ""
    package = build_package(
        classify_state(state), state, commands=_commands_of(eng), policy=_POLICY
    )
    action = _k9(eng).action_id
    [step] = [s for s in package.next if "if the reason says" in s.text]
    assert f"{ISSUE} is closed or not in the run's repository" in step.text
    assert f"reopen {ISSUE} if it is closed" in step.text
    assert not [s for s in package.next if f"-action {action}" in s.command]
    assert not [c for c in package.unknown if c.key == f"eligible-{action}"]


# -- the whole human output agrees with the package (PR #470 R4-F1) -------------------------


def _k12_saved_in_fix(tmp_state_dir):
    """FIX saved its plan, a K12 awaiting approval, and the process died before it
    blocked: the run is in FIX, which 'unblock' refuses, so only 'resume' continues it."""
    gh = FakeGitHub()
    eng = _updating(
        tmp_state_dir, gh, scripted(fixer(pr_body_update("R1-F1"), commit=False)), policy="approve"
    )
    save = eng._save

    def dying(*args, **kwargs):
        save(*args, **kwargs)
        records = load_state(eng.paths.state_file).effect_records
        if any(r["kind"] == EffectKind.PR_BODY_UPDATE.value for r in records):
            eng._save = save
            raise KeyboardInterrupt

    eng._save = dying
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    state = load_state(eng.paths.state_file)
    assert state.phase is Phase.FIX
    assert not _k12(eng).authorized and _k12(eng).stage is Stage.INTENDED
    return eng


def _k10_failed(tmp_state_dir):
    """A FAILED run that kept its FIX plan (operator actions pending): 'unblock' and
    'resume' both refuse FAILED, and only a new run goes on."""
    eng = _k10_blocked(tmp_state_dir)
    state = load_state(eng.paths.state_file)
    state.phase = Phase.FAILED
    state.stop_code = ""
    save_state(state, eng.paths.state_file)
    return eng


def _k9_off(tmp_state_dir):
    eng = _k9_blocked(tmp_state_dir)
    _acceptance_off(eng)
    return eng


def _k12_off(tmp_state_dir):
    eng = _k12_blocked(tmp_state_dir)
    _pr_body_off(eng)
    return eng


def _k12_conflict_off(tmp_state_dir):
    eng = _k12_conflicted(tmp_state_dir, "approve")
    _pr_body_off(eng)
    return eng


def _declines(count: int) -> list[dict]:
    """``count`` acceptance-action declines of review round 1, as 'unblock' records them."""
    return [
        {
            "action_id": f"A-{n:016x}",
            "round": 1,
            "finding_id": "R1-F1",
            "outcome": "declined",
            "at": "2026-10-21T00:00:00Z",
            "comment_url": "",
            "reason": "",
        }
        for n in range(count)
    ]


def _k10_at_the_decline_bound(tmp_state_dir):
    """K10 for #16 and #158 in a round that already records 15 declines: one more fits
    under the bound of 16, two do not (PR #470 R5-F1)."""
    gh = FakeGitHub()
    eng = _operating(tmp_state_dir, gh, fixer(BOTH))
    eng.state.acceptance_outcomes = _declines(MAX_DECLINES_PER_ROUND - 1)
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.decline_room() == 1
    return eng


def _k10_with_no_decline_left(tmp_state_dir):
    """The K10 of :func:`_k10_at_the_decline_bound` with #16's payload posted and the
    round's 16th decline recorded since (an edited state: 'unblock' drops the plan with
    every decline): FIX verifies #16 and stops again on #158 with no room to decline."""
    eng = _k10_at_the_decline_bound(tmp_state_dir)
    eng.github.add_comment(T16, 7016, _k10(eng, T16).body)
    eng.state.acceptance_outcomes = _declines(MAX_DECLINES_PER_ROUND)
    eng._save()
    assert eng.unblock("posted #16").unblocked
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.decline_room() == 0
    assert _k10(eng, T16).stage is Stage.OBSERVED
    return eng


#: Every stop that holds an acceptance, operator or PR body action, and the scenario
#: table's others: what a whole 'status' prints must agree with its package.
HUMAN_SCENARIOS = {
    **BLOCKED_SCENARIOS,
    "k9-acceptance-actions-off": _k9_off,
    "k9-held-by-its-issue": lambda d: _k9_held_by_issue(d, "policy", "CLOSED"),
    "k10-one-in-conflict": _k10_conflicted,
    "k10-at-the-decline-bound": _k10_at_the_decline_bound,
    "k10-with-no-decline-left": _k10_with_no_decline_left,
    "k10-kept-by-a-failed-run": _k10_failed,
    "k10-then-k12": _k10_then_k12,
    "k12-pr-body-updates-off": _k12_off,
    "k12-unapproved-conflict": lambda d: _k12_conflicted(d, "approve"),
    "k12-authorized-conflict": lambda d: _k12_conflicted(d, "auto"),
    "k12-unapproved-conflict-off": _k12_conflict_off,
    "k12-verified-then-edited": _k12_verified_then_edited,
    "k12-saved-in-a-fix-that-died": _k12_saved_in_fix,
}
#: The scenarios whose saved reason is not of the stop as 'status' reads it: the
#: configuration changed after the stop, or the phase did (a FAILED run that kept a
#: plan its BLOCKED stop named). 'status' says the reason is as of the stop.
STALE_REASONS = {
    "k9-acceptance-actions-off",
    "k10-kept-by-a-failed-run",
    "k12-pr-body-updates-off",
    "k12-unapproved-conflict-off",
}


def _human_status(status_cli, eng, capsys) -> tuple[str, str, str]:
    """``status``'s whole human output, its Recovery block, and everything but that block."""
    rc, out, err = status_cli(eng, "status", capsys=capsys)
    assert rc == 0, err
    start = out.index("\nRecovery (")
    end = out.index("\nOuter sandbox:", start)
    return out, out[start:end], out[:start] + out[end:]


def _displayed_commands(block: str) -> list[str]:
    """The command of each step the human Recovery block numbers or offers with 'or:'.

    A step prints ``<command>  (<text>)``, or its text alone when no command performs
    it. The command is split off at the first two spaces before a parenthesis: a
    command keeps the spaces of its quoted paths (PR #470 R5-F2), and no path of
    these scenarios holds that sequence.
    """
    commands = []
    for line in block.splitlines():
        step = re.match(r"(?:    \d+\. |      or: )(.*)$", line)
        if step and "  (" in step.group(1):
            commands.append(step.group(1).split("  (", 1)[0])
    return commands


def _offered_unblocks(text: str) -> list[str]:
    """Every 'unblock' ``text`` spells for the operator to type, up to its reason: as
    'status' spells it (``--reason "..."``) or as a reason saved with a stop does
    (``--reason '<why>'``), with whatever global flags."""
    return re.findall(
        r"autoforge(?: --[\w-]+ \S+)* unblock .*?--reason (?:\"\.\.\.\"|'<why>')", text
    )


@pytest.mark.parametrize("scenario", sorted(HUMAN_SCENARIOS))
def test_the_whole_human_status_names_only_the_packages_commands(
    tmp_state_dir, status_cli, capsys, scenario
):
    """The Recovery block is the one part of the human output that names a command which
    leaves or continues the stop: an action block describes its action and sends the
    operator there, so no line before it can offer what the package forbids (an
    approval under 'off', a decline of operator actions in conflict, a restored base
    and a plain unblock for an update never approved, a 'resume' of a BLOCKED run, an
    'unblock' of a run in FIX or FAILED). The human block's commands are exactly the
    package's, and no command anywhere in the output is one the package forbids."""
    eng = HUMAN_SCENARIOS[scenario](tmp_state_dir)
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    out, block, rest = _human_status(status_cli, eng, capsys)
    for command in (f"{prefix} unblock", f"{prefix} resume", f"{prefix} run ", "gh issue comment"):
        assert command not in rest, (command, rest)
    assert " epic resume " not in rest and " epic archive " not in rest
    waiting = [
        p["action_id"]
        for p in package["pending"]
        if p["action_id"] and p["kind"] != "push" and p["stage"] != "observed"
    ]
    if waiting:
        assert f"next:          {cli.SEE_RECOVERY}" in rest or (
            f"next:      {cli.SEE_RECOVERY}" in rest
        )
    assert _displayed_commands(block) == _commands(package)
    reason = next((line for line in out.splitlines() if line.startswith("Reason:")), "")
    if reason and package["outcome"] in ("blocked", "failed"):
        assert f"{reason}\n            ({cli.REASON_NOTE})\n" in out
    # The saved reason's commands, too, unless it predates what 'status' reads now.
    offered = _offered_unblocks(out.replace(reason, "") if scenario in STALE_REASONS else out)
    for claim in package["forbidden"]:
        for flag, action in re.findall(
            r"'unblock (--approve-action|--decline-action|--bind-evidence) ([A-Z]-[0-9a-f]{16})'",
            claim["text"],
        ):
            assert not [c for c in offered if f"{flag} {action}" in c], (claim, offered)
    if "decline-operator-actions" in _keys(package["forbidden"]):
        operator = [p["action_id"] for p in package["pending"] if p["kind"] == "operator_comment"]
        assert not [c for c in offered for a in operator if f"--decline-action {a}" in c]
    if package["outcome"] != "blocked":
        assert not offered, offered


def test_acceptance_actions_off_the_whole_status_offers_no_approval(
    tmp_state_dir, status_cli, capsys
):
    """R4-F1's first probe: under 'off' no line of 'status' offers the approval 'unblock'
    refuses; the action block says why none is recorded, and sends the operator to the
    Recovery block, whose first way is the payload posted by hand."""
    eng = _k9_off(tmp_state_dir)
    action = _k9(eng).action_id
    prefix = _prefix(eng)
    out, block, rest = _human_status(status_cli, eng, capsys)
    assert f"{prefix} unblock --approve-action" not in out
    assert (
        "authorization: operator approval required (safety.acceptance_actions: off, under "
        "which 'unblock' records none)"
    ) in rest
    # The reason was saved under 'approve', before the key changed: status says so.
    [reason] = [line for line in out.splitlines() if line.startswith("Reason:")]
    assert f"{reason}\n            ({cli.REASON_NOTE})\n" in out
    assert f"next:          {cli.SEE_RECOVERY}" in rest
    assert f"{prefix} status --action {action} | gh issue comment {ISSUE} --body-file -" in block
    rc, out, err = status_cli(
        eng, "unblock", "--dry-run", "--approve-action", action, "--reason", "x", capsys=capsys
    )
    assert "would stay BLOCKED" in out and "cannot be approved while" in out, out + err


def test_operator_actions_in_conflict_the_whole_status_offers_no_withdrawal(
    tmp_state_dir, status_cli, capsys
):
    """R4-F1's second probe: with one operator action in conflict no line of 'status'
    offers the withdrawal 'unblock' refuses, nor a binding of the action in conflict;
    its block says it is repaired, never bound over."""
    eng = _k10_conflicted(tmp_state_dir)
    x16, x158 = _k10(eng, T16), _k10(eng, T158)
    prefix = _prefix(eng)
    out, block, rest = _human_status(status_cli, eng, capsys)
    assert f"{prefix} unblock --decline-action" not in out
    # Nor the reason saved with the stop, which names the repair instead.
    assert not [c for c in _offered_unblocks(out) if "--decline-action" in c]
    assert not [c for c in _offered_unblocks(out) if f"--bind-evidence {x16.action_id}" in c]
    [reason] = [line for line in out.splitlines() if line.startswith("Reason:")]
    assert f"No withdrawal is possible while {x16.action_id} is in conflict" in reason
    assert f"--bind-evidence {x158.action_id} <comment URL>" in reason
    entry = rest[rest.index(f"  {x16.action_id}  comment on {T16}") :]
    entry = entry[: entry.index(f"  {x158.action_id}  comment on {T158}")]
    assert "and exactly these bytes; in conflict, so repaired, never bound over" in entry
    assert "an existing comment you bind" not in entry
    assert f"next:      {cli.SEE_RECOVERY}" in rest
    declines = ("--decline-action", x16.action_id, "--decline-action", x158.action_id)
    rc, out, err = status_cli(
        eng, "unblock", "--dry-run", *declines, "--reason", "x", capsys=capsys
    )
    assert "would stay BLOCKED" in out and "repaired, never declined" in out, out + err


def test_an_unapproved_update_in_conflict_the_whole_status_never_restores_then_unblocks(
    tmp_state_dir, status_cli, capsys
):
    """R4-F1's third probe: for an update never approved, no line of 'status' (the
    action block, the reason saved with the stop, the Recovery block) says to put the
    description back to its base and then run a plain unblock, which refuses until
    the approval: the base is named only with the approval of exactly this id, and
    a plain unblock's own refusal says the same. Restored to the base, a plain unblock
    stays BLOCKED, awaiting that approval."""
    eng = _k12_conflicted(tmp_state_dir, "approve")
    action = _k12(eng).action_id
    base = _k12(eng).precondition["base_sha256"]
    prefix = _prefix(eng)
    out, block, rest = _human_status(status_cli, eng, capsys)
    assert "put the body back to the expected sha256" not in out
    assert f"next:          {cli.SEE_RECOVERY}" in rest
    assert f"{prefix} unblock" not in rest[: rest.index("\nReason:")]
    restores = [
        line for line in out.splitlines() if re.search(rf"back to (exactly )?sha256 {base}", line)
    ]
    assert len(restores) == 2 and all(f"--approve-action {action}" in r for r in restores)
    [reason] = [line for line in out.splitlines() if line.startswith("Reason:")]
    assert "Next: set the body to exactly the payload" in reason
    assert "its base included, is refused. Or put the body back to exactly sha256" in reason
    [restore] = [line for line in block.splitlines() if f"back to sha256 {base}" in line]
    assert restore.startswith(
        f'      or: {prefix} unblock --approve-action {action} --reason "..."  ('
    )
    rc, out, err = status_cli(eng, "unblock", "--dry-run", "--reason", "x", capsys=capsys)
    assert "would stay BLOCKED" in out and "set the body to exactly the payload" in out
    assert f"or put the body back to exactly sha256 {base}" in out
    assert f"--approve-action {action}" in out, out + err
    eng.github.prs[PR].body = _base(eng.state.run_id)
    rc, out, err = status_cli(eng, "unblock", "--dry-run", "--reason", "x", capsys=capsys)
    assert "would stay BLOCKED" in out and f"action {action} awaits approval" in out, out + err


def test_an_update_never_approved_names_no_approval_in_its_reason_under_off(tmp_state_dir):
    """Stopped on the conflict while safety.pr_body_updates is off, the reason saved with
    the stop and a plain unblock's refusal name the payload and the decline, and neither
    the approval 'unblock' would refuse nor a way back to the base, which needs it."""
    eng = _k12_conflicted(tmp_state_dir, "approve", off_before_the_stop=True)
    action = _k12(eng).action_id
    reason = load_state(eng.paths.state_file).block_reason
    refused = eng.unblock("x")
    assert not refused.unblocked
    for text in (reason, refused.message):
        assert "set the body to exactly the payload" in text
        assert f"--decline-action {action}" in text
        assert "--approve-action" not in text and "put the body back" not in text


def test_a_k10_stops_reason_names_the_sequence_unblock_accepts(tmp_state_dir):
    """The reason saved with an operator-actions stop names the package's sequence (PR #470
    R1-F2, R4-F1): a plain unblock once every action is posted, or in its place one unblock
    that binds every comment bound (one bind per action, each followed by an unblock,
    would leave the second refused); withdrawing them all is the other way."""
    eng = _k10_blocked(tmp_state_dir)
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    reason = load_state(eng.paths.state_file).block_reason
    binds = re.findall(r"autoforge unblock (?:--bind-evidence [^ ]+ <comment URL> )+", reason)
    assert binds == [
        f"autoforge unblock --bind-evidence {x16} <comment URL> --bind-evidence {x158} "
        "<comment URL> "
    ]
    assert "Once every action is posted" in reason and "in place of that unblock" in reason
    assert (
        f"To withdraw them instead: autoforge unblock --decline-action {x16} "
        f"--decline-action {x158} --reason '<why>'"
    ) in reason


# -- a withdrawal within the round's decline bound (PR #470 R5-F1) -------------------------


def test_a_withdrawal_at_the_decline_bound_names_only_the_declines_it_has_room_for(
    tmp_state_dir, status_cli, capsys
):
    """With 15 declines recorded and two operator actions pending, 'unblock' refuses a
    command naming both (17 would pass 16), so neither the Recovery block nor the reason
    saved with the stop offers it: both name #16's action alone and say #158's is
    withdrawn with it. The command, run exactly as the whole human output displays it,
    is applied: #16's action is declined, #158's withdrawn, and the round holds 16."""
    eng = _k10_at_the_decline_bound(tmp_state_dir)
    x16, x158 = _k10(eng, T16).action_id, _k10(eng, T158).action_id
    prefix = _prefix(eng)
    package = _recovery(status_cli, eng, capsys)
    decline = f'{prefix} unblock --decline-action {x16} --reason "..."'
    [step] = [s for s in package["next"] if "--decline-action" in s["command"]]
    assert step["command"] == decline and step["alternative"]
    assert (
        f"review round 1 records 15 declined actions and 'unblock' records at most 16, so "
        f"this declines only {x16} and withdraws {x158} with it, uncounted and not declined"
    ) in step["text"]
    out, block, rest = _human_status(status_cli, eng, capsys)
    assert _displayed_commands(block) == _commands(package)
    assert f"      or: {decline}  (or withdraw them all in place of the steps above: " in block
    [reason] = [line for line in out.splitlines() if line.startswith("Reason:")]
    assert (
        f"To withdraw them instead: autoforge unblock --decline-action {x16} --reason '<why>' "
        f"(review round 1 records 15 declined actions and 'unblock' records at most 16, so "
        f"this declines only {x16} and withdraws {x158} with it, uncounted and not declined; "
        "the fixer may ask for a withdrawn action again)"
    ) in reason
    offered = [c for c in _offered_unblocks(out) if "--decline-action" in c]
    assert len(offered) == 2 and not [c for c in offered if f"--decline-action {x158}" in c]
    both = ("--decline-action", x16, "--decline-action", x158)
    rc, out, err = status_cli(eng, "unblock", "--dry-run", *both, "--reason", "x", capsys=capsys)
    assert "would stay BLOCKED" in out and "2 more would pass 16" in out, out + err
    rc, out, err = _run_displayed(status_cli, eng, decline, capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    s = load_state(eng.paths.state_file)
    assert [(o["action_id"], o["outcome"]) for o in s.operator_comment_outcomes] == [
        (x16, "declined"),
        (x158, "withdrawn"),
    ]
    assert s.round_declines() == MAX_DECLINES_PER_ROUND and s.effect_records == []


def test_no_withdrawal_is_offered_once_the_round_holds_its_declines(
    tmp_state_dir, status_cli, capsys
):
    """With the round's 16 declines recorded, 'unblock' refuses any decline: the package
    forbids it and names none, and the reason FIX saved says no withdrawal is possible."""
    eng = _k10_with_no_decline_left(tmp_state_dir)
    x158 = _k10(eng, T158).action_id
    package = _recovery(status_cli, eng, capsys)
    assert not [c for c in _commands(package) if "--decline-action" in c]
    [forbidden] = [c for c in package["forbidden"] if c["key"] == "decline-operator-actions"]
    assert "review round 1 already records 16 declined actions" in forbidden["text"]
    out, block, rest = _human_status(status_cli, eng, capsys)
    assert not [c for c in _offered_unblocks(out) if "--decline-action" in c]
    [reason] = [line for line in out.splitlines() if line.startswith("Reason:")]
    assert (
        "No withdrawal is possible: review round 1 already records 16 declined actions"
    ) in reason
    rc, out, err = status_cli(
        eng, "unblock", "--dry-run", "--decline-action", x158, "--reason", "x", capsys=capsys
    )
    assert "would stay BLOCKED" in out and "would pass 16" in out, out + err


# -- commands print byte for byte (PR #470 R5-F2) ------------------------------------------


def test_repeated_spaces_in_the_state_and_configuration_paths_survive_the_whole_output(
    tmp_path, status_cli, capsys
):
    """A state directory and a configuration file whose names hold runs of spaces: every
    command the whole human output displays is the one '--json' holds, byte for byte, and
    names those paths unchanged, never with the spaces collapsed (which would target
    another directory). The displayed commands find the run by the state directory they
    name, not by the one this test's own invocation gives."""
    state_dir = tmp_path / "state  with  spaces"
    config = tmp_path / "conf   dir" / "autoforge  config.yaml"
    config.parent.mkdir()
    config.write_text("# read through the scenario's configuration\n")
    eng = _k10_blocked(state_dir)
    x16 = _k10(eng, T16)
    flags = ("--config", str(config))
    rc, out, err = status_cli(eng, *flags, "status", "--json", capsys=capsys)
    assert rc == 0, err
    package = json.loads(out)["recovery"]
    prefix = shlex.join(["autoforge", *flags, "--state-dir", str(state_dir)])
    commands = _commands(package)
    assert commands and all(c.startswith(f"{prefix} ") for c in commands), commands
    rc, out, err = status_cli(eng, *flags, "status", capsys=capsys)
    assert rc == 0, err
    start = out.index("\nRecovery (")
    block = out[start : out.index("\nOuter sandbox:", start)]
    assert _displayed_commands(block) == commands
    assert "state with spaces" not in out and "conf dir" not in out
    assert "autoforge config.yaml" not in out
    for command in _displayed_commands(block):
        words = shlex.split(command)
        assert words[1:5] == [*flags, "--state-dir", str(state_dir)], command
    [read] = [c for c in commands if c == f"{prefix} status --action {x16.action_id}"]
    rc, out, err = status_cli(
        eng, *shlex.split(read)[1:], capsys=capsys, state_dir=tmp_path / "no run here"
    )
    assert rc == 0 and out == x16.body, err


_UNPRINTABLE = [
    "state\tdir",
    "state\ndir",
    "it's\r\nhere",
    "esc\x1b[31mred",
    "line\u2028sep",
    "byte\udcffhere",
]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to read the line back")
@pytest.mark.parametrize("name", _UNPRINTABLE)
def test_a_path_that_cannot_be_printed_is_spelled_on_one_line_with_its_bytes(name):
    """A path holding a character that cannot be printed on one line (a line break, a
    tab, an escape, a byte Python read as a surrogate) is shown as a dollar-single-quoted
    part: the line is one printable line, and the shell reads back the path's bytes."""
    argv = ["autoforge", "--state-dir", f"/tmp/{name}  x", "unblock", "--reason", "..."]
    line = _command_line(shlex.join(argv))
    assert line.isprintable(), line
    read = subprocess.run(
        ["bash", "-c", f"printf '%s\\0' {line}"], capture_output=True, check=True
    ).stdout
    assert read.split(b"\0")[:-1] == [os.fsencode(a) for a in argv]
    printable = shlex.join(["autoforge", "--state-dir", "/tmp/a  b"])
    assert _command_line(printable) == printable


# -- a saved plan's stop is replayed, never entered afresh (PR #470 R6-F1) ------------------

#: What a diagnosis says only of a stop before any plan was saved.
_FRESH = ("fresh entry", "afresh", "nothing was sent", "nothing was pushed", "nothing was created")


def _no_fresh_promise(text: str) -> None:
    lowered = text.lower()
    assert not [p for p in _FRESH if p in lowered], text


def _plain_and_resume(eng) -> list[tuple[str, bool]]:
    prefix = _prefix(eng)
    return [
        (f"{prefix} status --live", False),
        (f'{prefix} unblock --reason "..."', False),
        (f"{prefix} resume", False),
    ]


@pytest.mark.parametrize("pushed", [True, False], ids=["push-sent", "nothing-sent"])
def test_a_default_branch_stop_of_a_saved_plan_names_its_repair_and_replays_it(
    tmp_state_dir, status_cli, capsys, pushed
):
    """A saved ANALYZE_EXECUTE plan opens its PR onto 'main', which is no longer the
    default branch. The package, cached and live, says the plan stays saved and what of
    it is on GitHub, asks for 'main' to be the default again, and promises no fresh
    entry; the whole 'status' says the same. Run as displayed: 'unblock' before that
    repair is accepted and the re-entry stops here again, sending nothing; after it, the
    re-entry completes the plan without launching the agent or pushing again."""
    eng = _default_branch_after_the_plan(tmp_state_dir, pushed=pushed)
    gh = eng.github
    candidate = _push_sha(eng)
    branch_head = eng.origin.head("autoforge/2")
    assert branch_head == (candidate if pushed else None)
    writes = list(gh.effect_writes)
    for flags in ((), ("--live",)):
        package = _recovery(status_cli, eng, capsys, *flags)
        diagnosis = package["diagnosis"]
        assert (diagnosis["code"], diagnosis["freshness"]) == ("default-branch", "snapshot")
        _no_fresh_promise(diagnosis["meaning"] + diagnosis["remedy"])
        assert (
            "the repository's default branch is no longer 'main', the branch the saved "
            "ANALYZE_EXECUTE plan opens the implementation PR onto"
        ) in diagnosis["meaning"]
        assert "'unblock' keeps it" in diagnosis["meaning"]
        if pushed:
            assert (
                "Of the plan, written and verified: push effect 0 on refs/heads/autoforge/2; "
                "sent, its outcome not read back yet: implementation_pr effect 1"
            ) in diagnosis["meaning"]
        else:
            assert "Nothing of the plan was sent yet" in diagnosis["meaning"]
        assert diagnosis["remedy"].startswith(
            "make 'main' the default branch of the repository again, then 'unblock'"
        )
        assert "completes the saved plan without launching the agent" in diagnosis["remedy"]
        assert "but the re-entry stops here again" in diagnosis["remedy"]
        sequence = _sequence(package)
        assert sequence[:4] == [("", False), *_plain_and_resume(eng)]
        assert package["next"][0]["text"] == diagnosis["remedy"]
        [journal] = [c for c in package["preserve"] if c["key"] == "journal"]
        assert "'unblock' and 'resume' complete it from the journal" in journal["text"]
        written = [c for c in package["preserve"] if c["key"] == "written-0"]
        if pushed:
            [push] = written
            assert (
                f"refs/heads/autoforge/2 at {candidate}, pushed by the saved plan (push effect "
                "0) and verified: do not delete, reset or force-push it"
            ) in push["text"]
        else:
            assert written == []
            [kept] = [c for c in package["preserve"] if c["key"] == "worktree"]
            assert f"holds the unpushed candidate {candidate}" in kept["text"]
    live = package["live"]
    assert (live["decision"], live["target_phase"]) == ("would_reenter", "ANALYZE_EXECUTE")
    assert "completes from its persisted plan without launching the agent" in live["detail"]
    out, block, rest = _human_status(status_cli, eng, capsys)
    _no_fresh_promise(out)
    assert "    1. make 'main' the default branch of the repository again" in block

    plain = f'{_prefix(eng)} unblock --reason "..."'
    rc, out, err = _run_displayed(status_cli, eng, plain, capsys)
    assert rc == 0 and "BLOCKED -> ANALYZE_EXECUTE" in out, err
    eng.load()
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.stop_code == "default-branch"
    assert gh.effect_writes == writes and eng.origin.head("autoforge/2") == branch_head
    assert _recovery(status_cli, eng, capsys)["next"][0]["text"] == diagnosis["remedy"]

    gh.default_branch = "main"
    rc, out, err = _run_displayed(status_cli, eng, plain, capsys)
    assert rc == 0 and "BLOCKED -> ANALYZE_EXECUTE" in out, err
    eng.load()
    outcome = eng.step()
    assert outcome.next_phase == "REVIEW", outcome.message
    assert eng.provider.calls == []
    assert eng.origin.head("autoforge/2") == candidate == eng.state.current_head_sha
    assert gh.prs[PR].base_ref == "main"


def test_a_fix_plan_refused_after_its_follow_up_was_created_names_it_and_replays_the_plan(
    tmp_state_dir, status_cli, capsys
):
    """The saved FIX plan created R1-F2's follow-up, which was closed before the push.
    The package, cached and live, names that issue as written (never "nothing was
    sent"), keeps it under do-not-touch, asks for it to be reopened, and promises no
    fresh fixer; the whole 'status' says the same. Run as displayed: 'unblock' before
    the repair is accepted and FIX stops here again, sending nothing; after it, FIX
    completes the plan without launching the fixer and creates no second issue."""
    eng = _fix_follow_up_closed_after_the_plan(tmp_state_dir)
    gh = eng.github
    candidate = _push_sha(eng)
    reviewed = eng.state.reviewed_head_sha
    for flags in ((), ("--live",)):
        package = _recovery(status_cli, eng, capsys, *flags)
        diagnosis = package["diagnosis"]
        assert (diagnosis["code"], diagnosis["freshness"]) == ("fix-plan-refused", "snapshot")
        _no_fresh_promise(diagnosis["meaning"] + diagnosis["remedy"])
        assert diagnosis["meaning"].startswith(
            "the open issues contradict the saved FIX plan of review round 1"
        )
        assert (
            f"Of the plan, written and verified: follow_up_issue effect 0 on owner/repo "
            f"({ISSUE43}). What is written stays on GitHub"
        ) in diagnosis["meaning"]
        assert "reopen, with its marker as written, each one it did" in diagnosis["remedy"]
        assert "completes the saved plan without launching the fixer" in diagnosis["remedy"]
        assert _sequence(package)[:4] == [("", False), *_plain_and_resume(eng)]
        [issue] = [c for c in package["preserve"] if c["key"] == "written-0"]
        assert (
            f"issue {ISSUE43}, created by the saved plan as the follow-up of R1-F2 "
            "(follow_up_issue effect 0) and verified: keep it open"
        ) in issue["text"]
        [kept] = [c for c in package["preserve"] if c["key"] == "worktree"]
        assert f"holds the unpushed candidate {candidate}" in kept["text"]
    live = package["live"]
    assert (live["decision"], live["target_phase"]) == ("would_reenter", "FIX")
    assert "completes from its persisted plan without launching the fixer" in live["detail"]
    out, block, rest = _human_status(status_cli, eng, capsys)
    _no_fresh_promise(out)
    assert f"issue {ISSUE43}, created by the saved plan" in block

    plain = f'{_prefix(eng)} unblock --reason "..."'
    rc, out, err = _run_displayed(status_cli, eng, plain, capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    eng.load()
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.stop_code == "fix-plan-refused"
    assert [w[0] for w in gh.effect_writes] == ["create_issue"]
    assert eng.origin.head(BRANCH) == reviewed and eng.provider.calls == []

    gh.issues[ISSUE43].state = "OPEN"
    rc, out, err = _run_displayed(status_cli, eng, plain, capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    eng.load()
    outcome = eng.step()
    assert outcome.next_phase == "REVIEW", outcome.message
    assert eng.provider.calls == []
    assert [w[0] for w in gh.effect_writes] == ["create_issue"]
    assert eng.origin.head(BRANCH) == candidate == gh.prs[PR].head_sha


def test_a_saved_fix_plan_deferring_to_its_own_issue_routes_to_a_new_run(
    tmp_state_dir, status_cli, capsys
):
    """A saved plan FIX refuses whatever GitHub holds: the package says it cannot
    complete as saved, offers no unblock or resume, and leads to a new run. The plain
    unblock it advises against is accepted, as 'status --live' previews, and FIX
    refuses the plan again, launching nothing."""
    eng = _fix_plan_deferring_to_its_own_issue(tmp_state_dir)
    prefix = _prefix(eng)
    for flags in ((), ("--live",)):
        package = _recovery(status_cli, eng, capsys, *flags)
        diagnosis = package["diagnosis"]
        _no_fresh_promise(diagnosis["meaning"] + diagnosis["remedy"])
        assert diagnosis["meaning"].startswith(
            "the saved FIX plan of review round 1 defers R1-F1 to the run's own issue"
        )
        assert "Nothing of the plan was sent yet" in diagnosis["meaning"]
        assert diagnosis["remedy"].endswith("and start a new run")
        assert _sequence(package) == [
            ("", False),
            (f"{prefix} run --force --epic {EPIC} --issue {ISSUE}", False),
        ]
        assert "unblock-saved-plan" in _keys(package["forbidden"])
        [journal] = [c for c in package["preserve"] if c["key"] == "journal"]
        assert "it cannot complete as saved, so a new run replaces it" in journal["text"]
        assert not [c for c in package["preserve"] if c["key"].startswith("written-")]
    assert package["live"]["target_phase"] == "FIX"
    out, block, rest = _human_status(status_cli, eng, capsys)
    assert f"{prefix} unblock" not in out and f"{prefix} resume" not in out

    rc, out, err = status_cli(eng, "unblock", "--reason", "trying anyway", capsys=capsys)
    assert rc == 0 and "BLOCKED -> FIX" in out, err
    eng.load()
    assert eng.step().next_phase == "BLOCKED"
    assert eng.state.stop_code == "fix-plan-refused"
    assert eng.provider.calls == [] and eng.github.effect_writes == []


def test_a_context_only_fix_plan_is_kept_and_named_as_saved(tmp_state_dir):
    """A FIX plan whose resolutions write nothing is saved as its context alone: it is
    a saved plan all the same, so its stop keeps the journal and names no fresh launch."""
    eng = _fix_plan_deferring_to_its_own_issue(tmp_state_dir)
    state = load_state(eng.paths.state_file)
    state.effect_records = []
    context = state.completion_context
    context["resolutions"] = [r for r in context["resolutions"] if r["finding_id"] == "R1-F1"]
    state.open_findings = [f for f in state.open_findings if f["id"] == "R1-F1"]
    state.phase_effects()  # still a valid saved plan
    package = build_package(
        classify_state(state), state, commands=_commands_of(eng), policy=_POLICY
    )
    [journal] = [c for c in package.preserve if c.key == "journal"]
    assert "(its completion context; no effect record)" in journal.text
    assert package.diagnosis["meaning"].startswith("the saved FIX plan of review round 1")


@pytest.mark.parametrize(
    ("scenario", "launch"),
    [
        ("default-branch-before-the-plan", "ANALYZE_EXECUTE will launch the implementation agent"),
        (
            "fix-plan-refused-before-the-plan",
            "FIX re-reads the HEAD, base and merge base before launching the fixer",
        ),
    ],
)
def test_a_stop_before_any_plan_keeps_its_fresh_launch_advice(
    tmp_state_dir, status_cli, capsys, scenario, launch
):
    """The same codes before any plan was saved: nothing was sent and 'unblock' starts
    afresh, as the table says and 'status --live' previews; no journal is kept."""
    eng = BLOCKED_SCENARIOS[scenario](tmp_state_dir)
    saved = load_state(eng.paths.state_file)
    assert saved.effect_records == [] and saved.completion_context == {}
    package = _recovery(status_cli, eng, capsys, "--live")
    diagnosis = package["diagnosis"]
    table = DIAGNOSES[StopReasonCode(diagnosis["code"])]
    assert (diagnosis["meaning"], diagnosis["remedy"]) == (table.meaning, table.remedy)
    assert "fresh entry" in table.remedy or "afresh" in table.remedy
    assert package["next"][0]["text"] == table.remedy
    assert _sequence(package)[1:4] == _plain_and_resume(eng)
    assert not [c for c in package["preserve"] if c["key"] == "journal"]
    assert launch in package["live"]["detail"]
