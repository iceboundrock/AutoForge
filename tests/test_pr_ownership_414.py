"""Behavioural tests for #414: who may drive an implementation PR.

Black-box: every test drives ``ControllerEngine.step`` / ``unblock`` or
``cli.main`` against ``FakeGitHub`` and the scripted provider, and asserts
only what an operator could observe: the phase, the BLOCKED reason, the
stop code on the outcome, the writes the fake GitHub recorded, the agent
launches the scripted provider recorded, and the PR body on the fake.

These tests were written from ``main`` before the implementation, as an
acceptance suite for the decision recorded in ADR 0004 D9.4a. Every
assumption they make about the implementation (the marker field's name,
how the marker is rendered, the takeover command, the stop code) is in the
ADAPTER section directly below, so the tests themselves state behaviour only.

Terms: "owner" is the run id an implementation marker names; OTHER is a
run of another controller (another clone, machine or state directory) that
this test never sees except through what it wrote on GitHub.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from autoforge import cli
from autoforge.claims import IMPLEMENTATION, REVIEW, marker_json
from autoforge.effects import EffectKind, Stage
from autoforge.engine import ControllerEngine
from autoforge.errors import (
    GitHubError,
    GitHubUnavailableError,
    StateError,
    StateTransitionError,
    VerificationError,
)
from autoforge.providers import ProviderRegistry, ScriptedProvider
from autoforge.state import load_state
from autoforge.transitions import Phase
from autoforge.validation import parse_issue_url, parse_pr_url
from tests.conftest import (
    EPIC,
    GIT_IDENT,
    ISSUE,
    PR,
    FakeGitHub,
    Origin,
    analyze_payload,
    block,
    commit_in,
    connect_origin,
    git_out,
    git_repo,
    github_origin,
    implementation_pr_body,
    make_engine,
    publish_pr_head,
    review_result,
)

# =============================================================================
# ADAPTER: every assumption about the implementation under test lives here.
# =============================================================================

#: The implementation marker's owner field (the #414 recommendation's name).
OWNER_FIELD = "owner_run_id"

#: Stop codes an ownership refusal may carry on its StepOutcome (ADR 0011).
#: ``None`` in the set accepts an outcome that carries no classification.
OWNER_STOP_CODES = {"pr-owner"}

#: Text an ownership BLOCKED reason (or unblock refusal) names as the way out.
TAKEOVER_HINT = "--take-over"

MISSING = object()  # an owner field left out of the marker payload


def owner_marker(issue_url: str, owner: object = MISSING) -> str:
    """The ``ai-implementation`` marker naming ``owner`` (any JSON value), as GitHub holds it.

    Built from the payload directly rather than through the renderer, so a
    malformed owner can be written too, the way another controller or a
    human editing the body could.
    """
    payload: dict[str, object] = {"issue": parse_issue_url(issue_url).canonical}
    if owner is not MISSING:
        payload[OWNER_FIELD] = owner
    return f"<!-- ai-implementation: {marker_json(payload)} -->"


def take_over(eng: ControllerEngine, pr_url: str, reason: str, *, dry_run: bool = False):
    """The operator's explicit takeover: ``autoforge unblock --take-over <PR> --reason ...``,
    then the ``resume`` step that performs the journaled K11 write and binds the PR."""
    outcome = eng.unblock(reason, take_over=pr_url, dry_run=dry_run)
    if outcome.unblocked and not dry_run and eng.state.phase == Phase.ANALYZE_EXECUTE:
        eng.step()
    return outcome


def cli_take_over(state_dir: str, pr_url: str, reason: str) -> list[str]:
    return ["--state-dir", state_dir, "unblock", "--take-over", pr_url, "--reason", reason]


# =============================================================================
# Helpers built only on the adapter and the repository's own test fixtures.
# =============================================================================

OTHER = "af-20261031T040500Z-0a0b0c"  # a run of another controller
PR41 = "https://github.com/owner/repo/pull/41"
REASON = "the operator stopped the other controller and inspected the PR"


def _stored_pr(gh: FakeGitHub, pr_url: str):
    """The fake's live PR record, its head following the origin as GitHub's does."""
    return gh._stored(pr_url)


def rewrite_owner(gh: FakeGitHub, pr_url: str, owner: object, issue_url: str = ISSUE) -> None:
    """Replace the PR's implementation marker by one naming ``owner`` (or add one).

    What another controller's takeover, or a human's edit, does on GitHub.
    """
    pr = _stored_pr(gh, pr_url)
    marker = owner_marker(issue_url, owner)
    found = list(IMPLEMENTATION.pattern.finditer(pr.body or ""))
    if found:
        body = pr.body
        for m in reversed(found):
            body = body[: m.start()] + marker + body[m.end() :]
        pr.body = body
    else:
        pr.body = f"{pr.body}\n\nCloses #{parse_issue_url(issue_url).number}\n\n{marker}"


def strip_marker(gh: FakeGitHub, pr_url: str) -> None:
    """A human removes the implementation marker from the PR body entirely."""
    pr = _stored_pr(gh, pr_url)
    pr.body = IMPLEMENTATION.pattern.sub("", pr.body or "")


def marker_owner(body: str) -> object:
    """The owner the body's one implementation marker names; MISSING when it names none."""
    found = list(IMPLEMENTATION.pattern.finditer(body or ""))
    assert len(found) == 1, f"{len(found)} implementation markers in {body!r}"
    payload = json.loads(found[0].group("payload"))
    return payload.get(OWNER_FIELD, MISSING)


def body_without_marker(body: str) -> str:
    return IMPLEMENTATION.pattern.sub("<marker>", body or "")


def writes(gh: FakeGitHub, name: str) -> list[tuple]:
    return [w for w in gh.effect_writes if w[0] == name]


def review_comments(gh: FakeGitHub, pr_url: str = PR) -> list:
    return [c for c in gh.comments.get(pr_url, []) if REVIEW.pattern.search(c.body)]


def state_of(eng: ControllerEngine):
    return load_state(eng.paths.state_file)


def assert_owner_stop(out, owner: str) -> None:
    """The step blocked, its reason names ``owner``, and its stop code is an ownership one."""
    assert out.next_phase == "BLOCKED", out.message
    assert owner in out.message, out.message
    code = out.stop.code.value if out.stop is not None else None
    assert code in OWNER_STOP_CODES, f"stop code {code!r} for: {out.message}"


def attempt(eng: ControllerEngine):
    """One step as `resume` would take it: the outcome, or the refusal it raised."""
    try:
        return eng.step()
    except (
        StateTransitionError,
        GitHubUnavailableError,
        GitHubError,
        VerificationError,
        StateError,
    ) as exc:
        return exc


def drive(eng: ControllerEngine, steps: int = 3) -> None:
    """What an operator would do next: `unblock` a BLOCKED run, then `resume` it."""
    for _ in range(steps):
        if eng.state.phase == Phase.BLOCKED:
            try:
                eng.unblock(REASON)
            except (GitHubError, VerificationError, StateError):
                return
            if eng.state.phase == Phase.BLOCKED:
                return
        result = attempt(eng)
        if isinstance(result, Exception) or eng.state.phase == Phase.BLOCKED:
            return


def reviewer(gh: FakeGitHub, round_: int = 1, pr_url: str = PR, during=None):
    """A reviewer that finds nothing at the PR's current HEAD; ``during`` runs first."""

    def handler(req) -> str:
        if during is not None:
            during()
        return block(review_result(round_, _stored_pr(gh, pr_url).head_sha))

    return handler


def owning_run(tmp_path: Path, gh: FakeGitHub, script=None) -> ControllerEngine:
    """Run A: it opened PR through K2 (the controller's own create) and is now in REVIEW."""
    handlers = [lambda req: block(analyze_payload(commit_in(req.cwd)))]
    if script is not None:
        handlers.append(script)

    def handler(req):
        step = handlers.pop(0) if len(handlers) > 1 else handlers[0]
        return step(req)

    a = make_engine(tmp_path / "a", handler, github=gh, origin=True)
    assert a.step().next_phase == "ANALYZE_EXECUTE"
    out = a.step()
    assert out.next_phase == "REVIEW", out.message
    assert parse_pr_url(a.state.current_pr_url).same_target(parse_pr_url(PR))
    return a


def second_run(
    tmp_path: Path, gh: FakeGitHub, first: ControllerEngine, script=None, name: str = "b"
) -> ControllerEngine:
    """A fresh state on the same checkout and GitHub: #387's own --state-dir per run."""
    b = make_engine(tmp_path / name, script, github=gh, workdir=first.workdir)
    connect_origin(b, first.origin)
    b.origin = first.origin
    return b


def restarted(eng: ControllerEngine, gh: FakeGitHub, script=None) -> ControllerEngine:
    """A new controller process on the same state directory, checkout and GitHub."""
    eng.close()
    fresh = make_engine(eng.paths.state_dir, script, github=gh, workdir=eng.workdir)
    connect_origin(fresh, eng.origin)
    fresh.origin = eng.origin
    fresh.load()
    return fresh


def fresh_run_blocked_on(tmp_path, gh, first, owner: str, script=None) -> ControllerEngine:
    """A fresh run that met ``first``'s PR owned by ``owner`` and blocked at its entry."""
    rewrite_owner(gh, PR, owner)
    b = second_run(tmp_path, gh, first, script)
    assert b.step().next_phase == "ANALYZE_EXECUTE"
    out = b.step()
    assert_owner_stop(out, owner)
    return b


def power_loss_at(eng, kind: EffectKind, stage: Stage, *, saved: bool) -> None:
    """The process dies the first time a ``kind`` record moves to ``stage`` (as in test_engine)."""
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


# =============================================================================
# Acceptance criterion 1: a fresh run facing a PR another run owns blocks
# before any launch and names the owner.
# =============================================================================


def test_ac1_fresh_run_blocks_before_any_launch_and_names_the_owner(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    writes_before = list(gh.effect_writes)
    rewrite_owner(gh, PR, OTHER)
    body_before = _stored_pr(gh, PR).body
    b = second_run(tmp_path, gh, a, "never launched")
    assert b.step().next_phase == "ANALYZE_EXECUTE"
    out = b.step()
    assert_owner_stop(out, OTHER)
    assert b.provider.calls == []
    assert gh.effect_writes == writes_before
    assert _stored_pr(gh, PR).body == body_before
    s = state_of(b)
    assert s.phase == Phase.BLOCKED and s.current_pr_url == ""
    assert OTHER in s.block_reason and TAKEOVER_HINT in s.block_reason


def test_ac1_a_pr_on_the_derived_branch_owned_elsewhere_is_no_k3_bypass(tmp_path):
    """K3 adopts an *unmarked* PR on autoforge/<n>; one whose marker names
    another run is that run's, and the fresh run stops before its agent runs,
    pushes nothing and writes no body."""
    gh = FakeGitHub()
    eng = make_engine(tmp_path / "a", "never launched", github=gh, origin=True)
    head = publish_pr_head(eng, parent="HEAD", branch="autoforge/2")
    gh.add_pr(
        PR41,
        head_sha=head,
        branch="autoforge/2",
        body=f"Started elsewhere.\n\nCloses #2\n\n{owner_marker(ISSUE, OTHER)}",
    )
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    out = eng.step()
    assert_owner_stop(out, OTHER)
    assert eng.provider.calls == [] and gh.effect_writes == []
    assert eng.origin.head("autoforge/2") == head


# =============================================================================
# The owner is written by the controller's own publication (K2, K3).
# =============================================================================


def test_k2_writes_the_creating_run_as_owner_outside_the_marker_identity(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    body = _stored_pr(gh, PR).body
    assert marker_owner(body) == a.state.run_id
    # The identity is still the issue alone: the closing line and the issue
    # URL are as before, and the marker is the only one in the body.
    payload = json.loads(next(IMPLEMENTATION.pattern.finditer(body)).group("payload"))
    assert set(payload) == {"issue", OWNER_FIELD}
    assert payload["issue"] == ISSUE
    assert "Closes #2" in body
    assert [w[0] for w in gh.effect_writes] == ["create_pull_request"]


def _from_start(req) -> str:
    start = re.search(r"Start commit: `([0-9a-f]{40})`", req.prompt)
    assert start, "the prompt names no start commit"
    git_out("-C", req.cwd, "checkout", "-q", "--detach", start.group(1))
    return block(analyze_payload(commit_in(req.cwd)))


def test_k3_adoption_of_an_unmarked_pr_records_the_adopting_run_as_owner(tmp_path):
    """K3 stays: an unmarked PR on the derived branch claims no owner, and the
    controller's append names the run that adopted it."""
    gh = FakeGitHub()
    eng = make_engine(tmp_path / "a", _from_start, github=gh, origin=True)
    main = eng.origin.head("main")
    tree = git_out("-C", str(eng.workdir), "rev-parse", f"{main}^{{tree}}")
    earlier = git_out(
        "-C", str(eng.workdir), *GIT_IDENT, "commit-tree", tree, "-p", main, "-m", "Earlier (#2)"
    )
    eng.origin.publish(eng.workdir, earlier, "autoforge/2")
    gh.add_pr(PR41, head_sha=earlier, branch="autoforge/2", body="Started by hand.")
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    body = _stored_pr(gh, PR41).body
    assert body.startswith("Started by hand.\n\nCloses #2\n\n")
    assert marker_owner(body) == eng.state.run_id
    assert [w[0] for w in gh.effect_writes] == ["write_pr_body"]


# =============================================================================
# Acceptance criterion 4: the owning run's crash recovery after K2 is unchanged.
# =============================================================================


@pytest.mark.parametrize(
    "crash",
    ["plan-saved-nothing-sent", "attempt-saved", "create-landed-save-lost"],
)
def test_ac4_owner_crash_recovery_after_k2_is_unchanged(tmp_path, monkeypatch, crash):
    gh = FakeGitHub()
    a = make_engine(
        tmp_path / "a",
        lambda req: block(analyze_payload(commit_in(req.cwd))),
        github=gh,
        origin=True,
    )
    assert a.step().next_phase == "ANALYZE_EXECUTE"
    if crash == "plan-saved-nothing-sent":

        def dies(self):
            raise KeyboardInterrupt

        monkeypatch.setattr(ControllerEngine, "_complete_analyze", dies)
    elif crash == "attempt-saved":
        power_loss_at(a, EffectKind.IMPLEMENTATION_PR, Stage.ATTEMPTED, saved=True)
    else:
        power_loss_at(a, EffectKind.IMPLEMENTATION_PR, Stage.OBSERVED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        a.step()
    monkeypatch.undo()
    a2 = restarted(a, gh, reviewer(gh))
    out = a2.step()
    assert out.next_phase == "REVIEW", out.message
    assert a2.provider.calls == []  # no second implementation agent
    assert len(writes(gh, "create_pull_request")) == 1 and len(gh.prs) == 1
    # Its own PR does not stop it: the next step reviews and posts round 1.
    out = a2.step()
    assert out.next_phase != "BLOCKED", out.message
    assert len(a2.provider.calls) == 1 and len(review_comments(gh)) == 1


# =============================================================================
# Acceptance criterion 2: after a confirmed takeover, the displaced engine
# blocks at its next phase entry and launches nothing.
# =============================================================================


def test_ac2_displaced_run_blocks_at_its_next_entry(tmp_path):
    """Another controller took the PR over (all this run can see of it is the
    marker naming that run): the next entry blocks before the reviewer."""
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh, reviewer(gh))
    launched = len(a.provider.calls)
    rewrite_owner(gh, PR, OTHER)
    out = a.step()
    assert_owner_stop(out, OTHER)
    assert len(a.provider.calls) == launched
    assert review_comments(gh) == [] and writes(gh, "create_pr_comment") == []


def test_ac2_takeover_rewrites_the_owner_binds_the_pr_and_displaces_the_old_run(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()  # the operator stopped A first
    b = second_run(tmp_path, gh, a, reviewer(gh))
    assert b.step().next_phase == "ANALYZE_EXECUTE"
    assert_owner_stop(b.step(), a.state.run_id)
    before = _stored_pr(gh, PR).body
    edits = len(writes(gh, "write_pr_body"))

    outcome = take_over(b, PR, REASON)
    assert outcome.unblocked, outcome.message
    after = _stored_pr(gh, PR).body
    assert marker_owner(after) == b.state.run_id
    assert body_without_marker(after) == body_without_marker(before)  # only the marker changed
    assert len(writes(gh, "write_pr_body")) == edits + 1
    s = state_of(b)
    assert parse_pr_url(s.current_pr_url).same_target(parse_pr_url(PR))
    assert s.phase == Phase.REVIEW

    out = b.step()  # B now drives the PR
    assert out.next_phase != "BLOCKED", out.message
    assert len(b.provider.calls) == 1 and len(review_comments(gh)) == 1

    a2 = restarted(a, gh, reviewer(gh))
    assert_owner_stop(a2.step(), b.state.run_id)
    assert a2.provider.calls == [] and len(review_comments(gh)) == 1


def test_ac2_a_run_displaced_while_its_reviewer_runs_posts_nothing(tmp_path):
    """The takeover lands while A's reviewer works: A's verdict is never
    published, the round is not consumed, and A blocks naming the new owner."""
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh, reviewer(gh, during=lambda: rewrite_owner(gh, PR, OTHER)))
    out = a.step()
    assert_owner_stop(out, OTHER)
    assert review_comments(gh) == [] and writes(gh, "create_pr_comment") == []
    assert state_of(a).review_round == 0


# =============================================================================
# Acceptance criterion 3: a run whose PR another controller pushed past the
# bound HEAD launches nothing on `unblock`.
# =============================================================================


def test_ac3_fresh_run_launches_nothing_on_unblock_after_the_owner_pushed(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    b = fresh_run_blocked_on(tmp_path, gh, a, a.state.run_id, "never launched")
    rewrite_owner(gh, PR, a.state.run_id)
    publish_pr_head(a, parent=_stored_pr(gh, PR).head_sha, branch="autoforge/2")
    comments = len(review_comments(gh))
    drive(b)
    assert b.provider.calls == []
    assert len(review_comments(gh)) == comments
    assert state_of(b).phase == Phase.BLOCKED
    assert writes(gh, "write_pr_body") == []


def test_ac3_legacy_bound_run_launches_nothing_on_unblock_once_another_run_owns_the_pr(
    tmp_path,
):
    """#387's second run, as an old version left it: bound to the PR by the
    marker adoption, then BLOCKED by D9.6. The other controller now owns the
    PR and pushed its fixes; an `unblock` must not review the new HEAD as
    round 1."""
    gh = FakeGitHub()
    y = owning_run(tmp_path, gh)
    old_head = _stored_pr(gh, PR).head_sha
    rewrite_owner(gh, PR, OTHER)
    x = second_run(tmp_path, gh, y, reviewer(gh), name="x")
    x.state.phase = Phase.BLOCKED
    x.state.block_reason = "a round 1 comment this controller did not journal (D9.6)"
    x.state.step_count = 3
    x.state.current_pr_url = PR
    x.state.current_head_sha = old_head
    x.state.current_base_ref = "main"
    x.state.current_branch = "autoforge/2"
    x._save()
    publish_pr_head(y, parent=old_head, branch="autoforge/2")
    assert _stored_pr(gh, PR).head_sha != old_head
    comments = len(review_comments(gh))
    raw = x.paths.state_file.read_bytes()
    outcome = x.unblock(REASON)
    assert not outcome.unblocked and OTHER in outcome.message
    assert x.paths.state_file.read_bytes() == raw  # a refusal writes nothing
    drive(x)
    assert x.provider.calls == []
    assert len(review_comments(gh)) == comments
    assert state_of(x).phase == Phase.BLOCKED


# =============================================================================
# Acceptance criterion 5: a forged or unreadable owner value only blocks.
# =============================================================================

BAD_OWNERS = [
    pytest.param(123, id="number"),
    pytest.param(None, id="null"),
    pytest.param("", id="empty"),
    pytest.param("../../etc/passwd", id="path"),
    pytest.param("a" * 300, id="oversized"),
    pytest.param("af-20261104T000000Z-aaaaaa\u0007", id="control-char"),
    pytest.param(["af-20261104T000000Z-aaaaaa"], id="list"),
    pytest.param({"run": "af"}, id="object"),
]


@pytest.mark.parametrize("bad", BAD_OWNERS)
def test_ac5_a_malformed_owner_blocks_a_fresh_run_before_launch(tmp_path, bad):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    rewrite_owner(gh, PR, bad)
    b = second_run(tmp_path, gh, a, "never launched")
    assert b.step().next_phase == "ANALYZE_EXECUTE"
    out = b.step()
    assert out.next_phase == "BLOCKED", out.message
    assert b.provider.calls == [] and state_of(b).current_pr_url == ""
    assert [w[0] for w in gh.effect_writes] == ["create_pull_request"]


@pytest.mark.parametrize("bad", BAD_OWNERS)
def test_ac5_a_malformed_owner_blocks_the_owning_run_too(tmp_path, bad):
    """Unreadable is never "mine": the owner itself stops rather than guess."""
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh, reviewer(gh))
    launched = len(a.provider.calls)
    rewrite_owner(gh, PR, bad)
    out = a.step()
    assert out.next_phase == "BLOCKED", out.message
    assert len(a.provider.calls) == launched and review_comments(gh) == []


def test_ac5_two_implementation_markers_naming_two_owners_block_the_owner(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh, reviewer(gh))
    launched = len(a.provider.calls)
    pr = _stored_pr(gh, PR)
    pr.body = f"{pr.body}\n{owner_marker(ISSUE, OTHER)}"
    out = a.step()
    assert out.next_phase == "BLOCKED", out.message
    assert len(a.provider.calls) == launched and review_comments(gh) == []


def test_ac5_forged_owner_naming_this_run_without_a_local_record_blocks(tmp_path):
    """The fresh run's id is in its own block reason and logs, so anyone can
    write it into the marker. Without this run's record of opening, adopting
    or taking over the PR, it is not ownership: no bind, no launch."""
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    b = fresh_run_blocked_on(tmp_path, gh, a, OTHER, "never launched")
    rewrite_owner(gh, PR, b.state.run_id)  # forged
    drive(b)
    assert b.provider.calls == []
    assert state_of(b).phase == Phase.BLOCKED
    assert writes(gh, "write_pr_body") == []


def test_ac5_forged_owner_written_before_the_fresh_run_reaches_its_entry_blocks(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    b = second_run(tmp_path, gh, a, "never launched")
    assert b.step().next_phase == "ANALYZE_EXECUTE"
    rewrite_owner(gh, PR, b.state.run_id)  # forged
    out = b.step()
    assert out.next_phase == "BLOCKED", out.message
    assert b.provider.calls == [] and state_of(b).current_pr_url == ""


def test_ac5_a_marker_naming_this_run_stops_with_the_route_that_works(tmp_path):
    """PR #460: this run's own --take-over is refused for a marker that already
    names it, so neither the entry's stop nor a plain unblock may send the
    operator there. The route they do name, a fresh run, takes the PR over."""
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    b = second_run(tmp_path, gh, a, "never launched")
    assert b.step().next_phase == "ANALYZE_EXECUTE"
    forged = b.state.run_id
    rewrite_owner(gh, PR, forged)
    stopped = b.step()
    assert stopped.next_phase == "BLOCKED", stopped.message
    for text in (stopped.message, b.unblock(REASON).message):
        assert "fresh run" in text, text
        assert "writes this run into the marker" not in text, text
        assert "stop the run that owns it" not in text, text
    refused = b.unblock(REASON, take_over=PR)
    assert not refused.unblocked
    assert "already names this run" in refused.message
    assert "fresh run" in refused.message
    assert writes(gh, "write_pr_body") == [] and state_of(b).current_pr_url == ""
    b.close()

    c = second_run(tmp_path, gh, a, "never launched", name="c")
    assert c.step().next_phase == "ANALYZE_EXECUTE"
    assert_owner_stop(c.step(), forged)
    outcome = take_over(c, PR, REASON)
    assert outcome.unblocked, outcome.message
    assert marker_owner(_stored_pr(gh, PR).body) == c.state.run_id
    assert state_of(c).phase == Phase.REVIEW
    assert c.provider.calls == []


def test_ac5_an_unreadable_pr_body_never_lets_the_owner_launch(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh, reviewer(gh))
    launched = len(a.provider.calls)
    gh.get_pr_error = GitHubError("HTTP 403: Resource not accessible by integration")
    attempt(a)
    assert len(a.provider.calls) == launched and review_comments(gh) == []


# =============================================================================
# Legacy (ownerless) PRs and the upgrade.
# =============================================================================


def test_legacy_ownerless_pr_is_not_adopted_by_a_fresh_state(tmp_path):
    """Today's marker names no owner. A new state cannot tell whose it is, so
    it refuses and names the takeover as the way out."""
    gh = FakeGitHub()
    eng = make_engine(tmp_path / "a", "never launched", github=gh, origin=True)
    head = publish_pr_head(eng, parent="HEAD", branch="autoforge/2")
    gh.add_pr(PR, head_sha=head, branch="autoforge/2", body=implementation_pr_body())
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    out = eng.step()
    assert out.next_phase == "BLOCKED", out.message
    assert TAKEOVER_HINT in out.message
    assert eng.provider.calls == [] and state_of(eng).current_pr_url == ""
    assert gh.effect_writes == []


def test_legacy_ownerless_pr_can_be_taken_over_explicitly(tmp_path):
    gh = FakeGitHub()
    eng = make_engine(tmp_path / "a", None, github=gh, origin=True)
    head = publish_pr_head(eng, parent="HEAD", branch="autoforge/2")
    gh.add_pr(PR, head_sha=head, branch="autoforge/2", body=implementation_pr_body())
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    assert eng.step().next_phase == "BLOCKED"
    outcome = take_over(eng, PR, REASON)
    assert outcome.unblocked, outcome.message
    assert marker_owner(_stored_pr(gh, PR).body) == eng.state.run_id
    assert state_of(eng).phase == Phase.REVIEW


def _legacy_bound_in_review(tmp_path, gh, script):
    """A state bound by the previous version, with no ownership record, in REVIEW."""
    x = make_engine(tmp_path / "x", script, github=gh, origin=True)
    head = publish_pr_head(x, parent="HEAD", branch="autoforge/2")
    gh.add_pr(PR, head_sha=head, branch="autoforge/2", body=implementation_pr_body())
    x.state.phase = Phase.REVIEW
    x.state.current_pr_url = PR
    x.state.current_branch = "autoforge/2"
    x.state.current_head_sha = head
    x.state.current_base_ref = "main"
    x.state.current_merge_base_sha = x.origin.merge_base("main", head)
    x._save()
    return x


def test_legacy_bound_in_flight_state_blocks_once_and_continues_after_takeover(tmp_path):
    """A state bound before #414 to a PR whose marker names no run stops at its
    next step with the takeover command, and drives the PR once taken over."""
    gh = FakeGitHub()
    x = _legacy_bound_in_review(tmp_path, gh, reviewer(gh))
    out = x.step()
    assert out.next_phase == "BLOCKED", out.message
    assert TAKEOVER_HINT in out.message and out.stop is not None
    assert out.stop.code.value in OWNER_STOP_CODES
    assert x.provider.calls == [] and review_comments(gh) == []
    assert not x.unblock(REASON).unblocked  # a plain unblock does not take it over
    assert take_over(x, PR, REASON).unblocked
    assert marker_owner(_stored_pr(gh, PR).body) == x.state.run_id
    out = x.step()
    assert out.next_phase != "BLOCKED", out.message
    assert len(x.provider.calls) == 1 and len(review_comments(gh)) == 1


def test_legacy_bound_state_stops_once_another_run_owns_its_pr(tmp_path):
    gh = FakeGitHub()
    x = _legacy_bound_in_review(tmp_path, gh, reviewer(gh))
    rewrite_owner(gh, PR, OTHER)
    out = x.step()
    assert_owner_stop(out, OTHER)
    assert x.provider.calls == [] and review_comments(gh) == []


# =============================================================================
# An owner removed from the PR a run owns is not "legacy".
# =============================================================================


def test_owner_removed_from_the_marker_of_an_owned_pr_blocks(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh, reviewer(gh))
    launched = len(a.provider.calls)
    rewrite_owner(gh, PR, MISSING)  # today's ownerless shape
    out = a.step()
    assert out.next_phase == "BLOCKED", out.message
    assert len(a.provider.calls) == launched and review_comments(gh) == []


def test_marker_removed_entirely_from_an_owned_pr_blocks(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh, reviewer(gh))
    launched = len(a.provider.calls)
    strip_marker(gh, PR)
    out = a.step()
    assert out.next_phase == "BLOCKED", out.message
    assert len(a.provider.calls) == launched and review_comments(gh) == []


def test_owner_removed_then_restart_still_blocks(tmp_path):
    """The ownership record is the run's own persisted fact, not something the
    process remembers: a new process facing the ownerless marker blocks too."""
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    rewrite_owner(gh, PR, MISSING)
    a2 = restarted(a, gh, reviewer(gh))
    out = attempt(a2)
    assert a2.provider.calls == [] and review_comments(gh) == []
    assert state_of(a2).phase == Phase.BLOCKED, out


def test_owner_removed_block_then_restart_and_unblock_still_launches_nothing(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh, reviewer(gh))
    launched = len(a.provider.calls)
    rewrite_owner(gh, PR, MISSING)
    assert a.step().next_phase == "BLOCKED"
    a2 = restarted(a, gh, reviewer(gh))
    drive(a2)
    assert a2.provider.calls == [] and len(a.provider.calls) == launched
    assert review_comments(gh) == [] and state_of(a2).phase == Phase.BLOCKED


def test_a_read_failure_right_after_an_owner_change_then_restart_never_launches(tmp_path):
    """The first PR read of the step sees another owner; every GitHub read
    after it fails transiently, and the process is restarted on a healthy
    GitHub. Whatever the step did with the observation, nothing is launched
    and the run ends BLOCKED naming the owner."""
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    rewrite_owner(gh, PR, OTHER)
    real_get_pr = gh.get_pr

    def first_read_then_outage(url):
        pr = real_get_pr(url)
        gh.get_pr = real_get_pr
        gh.get_pr_failures = 10**6
        gh.merge_base_error = GitHubUnavailableError("connection reset")
        gh.comments_error = GitHubUnavailableError("connection reset")
        return pr

    gh.get_pr = first_read_then_outage
    attempt(a)
    gh.get_pr_failures = 0
    gh.merge_base_error = None
    gh.comments_error = None
    a2 = restarted(a, gh, reviewer(gh))
    drive(a2)
    assert a2.provider.calls == [] and review_comments(gh) == []
    s = state_of(a2)
    assert s.phase == Phase.BLOCKED and OTHER in s.block_reason


# =============================================================================
# Takeover: explicit, operator-authorized, crash-safe, nothing implicit.
# =============================================================================


def test_plain_unblock_never_takes_over(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    b = fresh_run_blocked_on(tmp_path, gh, a, OTHER, "never launched")
    outcome = b.unblock(REASON)
    assert not outcome.unblocked or state_of(b).phase != Phase.REVIEW
    drive(b)
    assert b.provider.calls == [] and writes(gh, "write_pr_body") == []
    assert marker_owner(_stored_pr(gh, PR).body) == OTHER
    assert state_of(b).phase == Phase.BLOCKED


def test_takeover_must_name_the_pr_it_takes(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    b = fresh_run_blocked_on(tmp_path, gh, a, OTHER, "never launched")
    raw = b.paths.state_file.read_bytes()
    try:
        outcome = take_over(b, "https://github.com/owner/repo/pull/43", REASON)
        assert not outcome.unblocked
    except (GitHubError, VerificationError, StateError) as exc:
        assert "43" in str(exc)
    assert writes(gh, "write_pr_body") == []
    assert marker_owner(_stored_pr(gh, PR).body) == OTHER
    assert load_state(b.paths.state_file).phase == Phase.BLOCKED
    assert b.paths.state_file.read_bytes() == raw or state_of(b).current_pr_url == ""


def test_takeover_whose_reply_was_lost_is_read_back_in_the_same_step(tmp_path):
    """K11 reads the PR back right after an ambiguous reply: a write that landed is
    observed there and then, with no second write."""
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    b = fresh_run_blocked_on(tmp_path, gh, a, OTHER, reviewer(gh))
    gh.write_failures = [("write_pr_body", GitHubUnavailableError("`gh api` timed out"), True)]
    outcome = take_over(b, PR, REASON)
    assert outcome.unblocked, outcome.message
    assert len(writes(gh, "write_pr_body")) == 1
    assert marker_owner(_stored_pr(gh, PR).body) == b.state.run_id
    assert state_of(b).phase == Phase.REVIEW


def test_takeover_killed_after_its_write_landed_completes_without_a_second_write(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    b = fresh_run_blocked_on(tmp_path, gh, a, OTHER, reviewer(gh))
    gh.write_failures = [("write_pr_body", KeyboardInterrupt(), True)]
    with pytest.raises(KeyboardInterrupt):
        take_over(b, PR, REASON)
    assert len(writes(gh, "write_pr_body")) == 1
    saved = state_of(b)
    assert saved.phase == Phase.ANALYZE_EXECUTE and saved.current_pr_url == ""  # not bound yet
    b2 = restarted(b, gh, reviewer(gh))
    out = b2.step()  # `resume`
    assert out.next_phase == "REVIEW", out.message
    assert len(writes(gh, "write_pr_body")) == 1  # read back, never re-sent
    assert marker_owner(_stored_pr(gh, PR).body) == b2.state.run_id
    assert b2.provider.calls == []


def test_takeover_whose_write_did_not_land_is_sent_again_on_resume_and_never_twice(tmp_path):
    """A K11 write the read-back does not show is pending: the next `resume` sends
    it again within its attempt bound, against the same base body."""
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    b = fresh_run_blocked_on(tmp_path, gh, a, OTHER, reviewer(gh))
    gh.write_failures = [("write_pr_body", GitHubUnavailableError("`gh api` timed out"), False)]
    with pytest.raises(GitHubError):
        take_over(b, PR, REASON)
    assert marker_owner(_stored_pr(gh, PR).body) == OTHER
    assert len(writes(gh, "write_pr_body")) == 1
    saved = state_of(b)
    assert saved.phase == Phase.ANALYZE_EXECUTE and saved.current_pr_url == ""
    assert b.provider.calls == []
    b = restarted(b, gh, reviewer(gh))
    out = b.step()  # `resume`
    assert out.next_phase == "REVIEW", out.message
    assert len(writes(gh, "write_pr_body")) == 2
    assert marker_owner(_stored_pr(gh, PR).body) == b.state.run_id
    assert b.provider.calls == []


def test_a_takeover_overtaken_after_its_write_blocks_and_can_be_taken_over_again(tmp_path):
    """The K11 body was written and read back, and then a third run rewrote the
    marker before the PR was bound. The run binds nothing and stops with the
    takeover command, and a new `--take-over` of the same PR plans K11 again
    on the body as it is now."""
    third = "af-20261031T050500Z-0c0d0e"
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    b = fresh_run_blocked_on(tmp_path, gh, a, OTHER, reviewer(gh))
    assert b.unblock(REASON, take_over=PR).unblocked
    persist = b._persist_effect

    def overtaken(record):
        persist(record)
        if record.stage is Stage.OBSERVED:
            rewrite_owner(gh, PR, third)

    b._persist_effect = overtaken  # type: ignore[method-assign]
    assert_owner_stop(b.step(), third)
    b._persist_effect = persist  # type: ignore[method-assign]
    assert state_of(b).current_pr_url == "" and b.provider.calls == []
    assert TAKEOVER_HINT in state_of(b).block_reason
    outcome = take_over(b, PR, REASON)
    assert outcome.unblocked, outcome.message
    assert marker_owner(_stored_pr(gh, PR).body) == b.state.run_id
    assert state_of(b).phase == Phase.REVIEW
    assert len(writes(gh, "write_pr_body")) == 2


def test_takeover_keeps_d96_no_second_round_one_comment_at_the_same_revision(tmp_path):
    """A took round 1 at HEAD H; B takes the PR over. B must not publish a
    second, conflicting round-1 review of H (ADR 0004 D9.6 is unchanged)."""
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh, reviewer(gh))
    assert a.step().next_phase != "BLOCKED"
    assert len(review_comments(gh)) == 1
    a.close()
    b = fresh_run_blocked_on(tmp_path, gh, a, a.state.run_id, reviewer(gh))
    assert take_over(b, PR, REASON).unblocked
    attempt(b)
    assert len(review_comments(gh)) == 1
    assert len(writes(gh, "create_pr_comment")) == 1


def test_dry_runs_write_nothing(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    a.close()
    fresh = second_run(tmp_path, gh, a, "never launched", name="c")
    fresh.run(max_steps=2, dry_run=True)
    assert not fresh.paths.state_file.exists() and fresh.provider.calls == []
    assert [w[0] for w in gh.effect_writes] == ["create_pull_request"]
    b = fresh_run_blocked_on(tmp_path, gh, a, OTHER, "never launched")
    raw = b.paths.state_file.read_bytes()
    body = _stored_pr(gh, PR).body
    count = len(gh.effect_writes)
    b.unblock(REASON, dry_run=True)
    outcome = take_over(b, PR, REASON, dry_run=True)
    assert outcome.dry_run and not outcome.unblocked
    assert b.paths.state_file.read_bytes() == raw
    assert _stored_pr(gh, PR).body == body and len(gh.effect_writes) == count
    assert b.provider.calls == []


# =============================================================================
# `epic archive` releases the local namespace, never the remote owner.
# =============================================================================


def test_archived_state_does_not_hand_its_pr_to_the_next_fresh_run(tmp_path):
    """ADR 0008 D17.4: `epic archive` moves state/state.json aside and the next
    admission starts fresh in the same namespace. The fresh run is not the
    archived one: it blocks naming the archived run's id until the operator
    takes the PR over."""
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh)
    old_run = a.state.run_id
    rewrite_owner(gh, PR, old_run)
    a.close()
    state_file = a.paths.state_file
    os.replace(state_file, state_file.with_name("state.json.archived-20261104T000000Z"))
    fresh = make_engine(a.paths.state_dir, "never launched", github=gh, workdir=a.workdir)
    connect_origin(fresh, a.origin)
    assert fresh.state.run_id != old_run
    assert fresh.step().next_phase == "ANALYZE_EXECUTE"
    out = fresh.step()
    assert_owner_stop(out, old_run)
    assert fresh.provider.calls == [] and state_of(fresh).current_pr_url == ""


# =============================================================================
# The review comment's identity gains no run id.
# =============================================================================


def test_review_marker_identity_is_unchanged(tmp_path):
    gh = FakeGitHub()
    a = owning_run(tmp_path, gh, reviewer(gh))
    assert a.step().next_phase != "BLOCKED"
    (comment,) = review_comments(gh)
    payload = json.loads(REVIEW.pattern.search(comment.body).group("payload"))
    assert set(payload) == {
        "finding_ids",
        "needs_fix_round",
        "reviewed_base_ref",
        "reviewed_head_sha",
        "reviewed_merge_base_sha",
        "round",
    }
    assert a.state.run_id not in comment.body


# =============================================================================
# The CLI: `run --force` is a fresh state and takes nothing over;
# `unblock --take-over` is the operator's form.
# =============================================================================


@pytest.fixture
def cli_fakes(monkeypatch, tmp_path, tmp_path_factory):
    """tests/test_cli.py's ``fakes``: every engine the CLI builds uses one FakeGitHub."""
    gh = FakeGitHub()
    origin = Origin.seeded(tmp_path_factory.mktemp("cli-origin"), git_repo(tmp_path))
    github_origin(tmp_path)
    gh.attach_origin(origin)
    provider = ScriptedProvider(lambda req: "")
    real = ControllerEngine.__init__

    def patched(self, *a, **k):
        k.setdefault("github", gh)
        k.setdefault(
            "providers", ProviderRegistry(overrides={"claude": provider, "opencode": provider})
        )
        real(self, *a, **k)
        connect_origin(self, origin)

    monkeypatch.setattr(ControllerEngine, "__init__", patched)
    return {"gh": gh, "origin": origin, "provider": provider}


def test_cli_run_force_does_not_take_over_and_unblock_take_over_does(
    tmp_path, capsys, monkeypatch, cli_fakes
):
    monkeypatch.chdir(tmp_path)
    gh, origin = cli_fakes["gh"], cli_fakes["origin"]
    main = origin.head("main")
    tree = git_out("-C", str(tmp_path), "rev-parse", f"{main}^{{tree}}")
    head = git_out("-C", str(tmp_path), *GIT_IDENT, "commit-tree", tree, "-p", main, "-m", "w")
    origin.publish(tmp_path, head, "autoforge/2")
    gh.add_pr(
        PR,
        head_sha=head,
        branch="autoforge/2",
        body=f"Elsewhere.\n\nCloses #2\n\n{owner_marker(ISSUE, OTHER)}",
    )
    sd = str(tmp_path / ".autoforge")
    run = ["--state-dir", sd, "run", "--epic", EPIC, "--issue", ISSUE]
    rc = cli.main(run)
    printed = capsys.readouterr()
    assert (Path(sd) / "state.json").exists(), (rc, printed.out[-2000:], printed.err[-2000:])
    first = load_state(Path(sd) / "state.json")
    assert first.phase == Phase.BLOCKED and OTHER in first.block_reason

    cli.main([*run, "--force"])
    capsys.readouterr()
    second = load_state(Path(sd) / "state.json")
    assert second.run_id != first.run_id
    assert second.phase == Phase.BLOCKED and OTHER in second.block_reason
    assert writes(gh, "write_pr_body") == [] and cli_fakes["provider"].calls == []
    assert marker_owner(_stored_pr(gh, PR).body) == OTHER

    rc = cli.main(cli_take_over(sd, PR, REASON))
    capsys.readouterr()
    assert rc == 0
    assert load_state(Path(sd) / "state.json").phase == Phase.ANALYZE_EXECUTE
    assert writes(gh, "write_pr_body") == []  # the write is the next step's
    rc = cli.main(["--state-dir", sd, "resume", "--max-steps", "1"])
    capsys.readouterr()
    assert rc == 0
    third = load_state(Path(sd) / "state.json")
    assert third.run_id == second.run_id and third.phase == Phase.REVIEW
    assert marker_owner(_stored_pr(gh, PR).body) == second.run_id
