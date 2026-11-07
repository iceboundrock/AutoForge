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
import shlex
from pathlib import Path

import pytest

from autoforge import cli
from autoforge.effects import sha256_text
from autoforge.engine import ControllerEngine
from autoforge.errors import ExecutionTimeoutError, GitHubError, GitHubUnavailableError
from autoforge.locking import ControllerLock, IssueRunLock, repository_lock_path
from autoforge.providers import AgentExecutionResult, ProviderRegistry, ScriptedProvider
from autoforge.recovery_package import (
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
    Commands,
    Policy,
    build_package,
)
from autoforge.redaction import redact
from autoforge.run_namespace import issue_namespace
from autoforge.sandbox_pin import SOURCE_CREATED, SandboxPin
from autoforge.state import load_state, save_state
from autoforge.stop_classification import (
    ReasonSource,
    StopClassification,
    StopOutcome,
    StopReasonCode,
    classify_state,
)
from autoforge.transitions import Phase
from tests.conftest import (
    EPIC,
    ISSUE,
    PR,
    SHA_A,
    SHA_B,
    FakeGitHub,
    acceptance,
    block,
    checks_refused,
    fixer,
    implement,
    implementation_pr_body,
    make_engine,
    pollute_operator_environment,
    pr_body_update,
    scripted,
    state_tree,
    unloadable_state,
)
from tests.test_engine import (
    ESCAPE_BODY,
    REASON_211,
    _accepting,
    _analyzed,
    _in_merge,
    _k9,
    _loop_agent,
    _no_stagnation,
    _one_finding_per_round,
)
from tests.test_operator_actions import BOTH, T16, T158, _k10, _operating
from tests.test_pr_body_update import _k12, _updating
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


BLOCKED_SCENARIOS = {
    "k10-operator-actions": _k10_blocked,
    "k9-acceptance-action": _k9_blocked,
    "k12-pr-body-update": _k12_blocked,
    "false-identity-conflict": _false_identity_conflict,
    "ambiguous-prs": _ambiguous,
    "checks-refused-291": _checks_refused,
    "review-round-cap": _review_cap,
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
        "unknown",
        "standalone",
    )
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
        assert (
            f'{prefix} unblock --bind-evidence {record.action_id} <comment URL> --reason "..."'
        ) in commands
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

_POLICY = Policy(max_total_steps=300, max_review_rounds=20, allow_merge=False)


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
    for title in ("proven:", "unknown:", "pending:", "forbidden:", "do not touch:", "next:"):
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
    CLI imports it (``test_only_the_sinks_and_the_cli_import_the_classification``)."""
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
        ".state",
        ".stop_classification",
        ".transitions",
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
