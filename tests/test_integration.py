"""Scripted end-to-end loop: INITIALIZING -> ... -> READY_FOR_MERGE.

Every agent call is a ScriptedProvider handler that acts the way the real
agent would (commit in its worktree, which the controller publishes as the
PR; return a review, whose comment the controller posts; commit a fix, which
the controller pushes). State is reloaded from disk after every step to prove
persistence.
"""

import dataclasses
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import autoforge.engine as engine_mod
from autoforge import cli
from autoforge.claims import (
    OPERATOR_COMMENT,
    render_implementation_marker,
    render_progress_marker,
    scan,
)
from autoforge.config import default_config
from autoforge.effects import EffectKind, EffectRecord, Stage
from autoforge.errors import StateTransitionError
from autoforge.state import load_state
from autoforge.transitions import Phase, decide_next_phase
from tests.conftest import (
    EPIC,
    ISSUE,
    PR,
    FakeGitHub,
    analyze_payload,
    assert_isolated_launch,
    assert_isolated_names,
    block,
    checks_refused,
    connect_origin,
    controller_review_comment,
    fix_commit,
    fixer,
    implement,
    launch_environment,
    make_engine,
    pollute_operator_environment,
    read_context_of,
    review_result,
)


def _sandboxed_as_configured(eng, mode: str) -> None:
    """#168: under ``required`` every launch carried the engine's facts and its
    description compiled; under ``disabled`` none did (ADR 0005 D15.2)."""
    calls = eng.provider.calls
    assert calls
    assert all((req.sandbox is not None) is (mode == "required") for req in calls)
    assert len(eng.provider.descriptions) == (len(calls) if mode == "required" else 0)


@pytest.mark.parametrize("sandbox_mode", ["disabled", "required"])
def test_full_loop_to_ready_for_merge(tmp_state_dir, sandbox_mode):
    gh = FakeGitHub()
    phases_seen = []
    fixed: list[str] = []

    def agent(req):
        phases_seen.append((req.phase, req.profile.model, req.profile.effort))
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "REVIEW" and "Round 1" in req.prompt:
            head = gh.prs[PR].head_sha  # the controller-published implementation commit
            finding = {
                "id": "R1-F1",
                "classification": "non-blocked",
                "title": "missing test",
                "location": "tests/",
                "required_resolution": "add a regression test",
            }
            return block(review_result(1, head, [finding]))
        if req.phase == "FIX":
            prev = gh.prs[PR].head_sha
            assert "R1-F1" in req.prompt and prev in req.prompt
            fixed.append(fix_commit(req))
            return block(
                {
                    "phase": "FIX",
                    "status": "success",
                    "previous_head_sha": prev,
                    "head_sha": fixed[0],
                    "resolutions": [
                        {"finding_id": "R1-F1", "resolution": "fixed", "commit_sha": fixed[0]}
                    ],
                }
            )
        if req.phase == "REVIEW" and "Round 2" in req.prompt:
            return block(review_result(2, gh.prs[PR].head_sha))
        raise AssertionError(f"unexpected call {req.phase}")

    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    eng.config.sandbox.mode = sandbox_mode
    eng._save()
    expected = ["ANALYZE_EXECUTE", "REVIEW", "FIX", "REVIEW", "READY_FOR_MERGE"]
    for exp in expected:
        out = eng.step()
        assert out.next_phase == exp, out.message
        persisted = load_state(eng.paths.state_file)
        assert persisted.phase.value == exp
    assert eng.run(max_steps=10) == []  # holds at READY_FOR_MERGE
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.READY_FOR_MERGE
    assert s.review_round == 2 and s.reviewed_head_sha == fixed[0] == s.current_head_sha
    assert s.last_review_result == "clean" and s.open_findings == []
    # The controller posted both rounds' comments; the last is the handoff.
    assert s.last_review_comment_url == controller_review_comment(eng, 2).url
    assert [w[0] for w in gh.effect_writes] == [
        "create_pull_request",
        "create_pr_comment",
        "create_pr_comment",
    ]
    assert [p[0] for p in phases_seen] == ["ANALYZE_EXECUTE", "REVIEW", "FIX", "REVIEW"]
    assert phases_seen[1][1] == "openai/gpt-5.6-luna"
    assert phases_seen[3][1] == "openai/gpt-5.6-terra"
    assert phases_seen[2][1] == "fable"
    assert s.step_count == 5
    # log dirs: one per agent invocation
    run_dir = eng.paths.logs_dir / s.run_id
    dirs = sorted(p.name for p in run_dir.iterdir() if p.is_dir())
    assert len(dirs) == 4
    assert [
        json.loads(line)["phase"] for line in (run_dir / "events.jsonl").read_text().splitlines()
    ] == ["ANALYZE_EXECUTE", "REVIEW", "FIX", "REVIEW"]
    _sandboxed_as_configured(eng, sandbox_mode)


def test_each_fix_round_receives_its_own_verified_review_comment(tmp_state_dir):
    """#80: round 1 -> FIX prompt carries comment A; round 2 -> FIX prompt
    carries comment B and not A. The handoff is the URL of the comment the
    controller posted and read back for the round (#162), so two fix rounds
    on one PR never share or confuse their review artifacts. State is
    reloaded from disk after every step."""
    gh = FakeGitHub()
    fix_prompts: list[str] = []
    heads: list[str] = []

    def finding(rnd: int) -> dict:
        return {
            "id": f"R{rnd}-F1",
            "classification": "nit",
            "title": f"round {rnd} finding",
            "location": "src/x.py:1",
            "required_resolution": f"resolve the round {rnd} finding",
        }

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "REVIEW":
            rnd = len(heads) + 1
            heads.append(gh.prs[PR].head_sha)
            return block(review_result(rnd, heads[-1], [finding(rnd)] if rnd < 3 else []))
        if req.phase == "FIX":
            fix_prompts.append(req.prompt)
            prev = gh.prs[PR].head_sha
            new = fix_commit(req)
            return block(
                {
                    "phase": "FIX",
                    "status": "success",
                    "previous_head_sha": prev,
                    "head_sha": new,
                    "resolutions": [{"finding_id": f"R{len(heads)}-F1", "resolution": "fixed"}],
                }
            )
        raise AssertionError(f"unexpected call {req.phase}")

    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    eng._save()
    expected = ["ANALYZE_EXECUTE", "REVIEW", "FIX", "REVIEW", "FIX", "REVIEW", "READY_FOR_MERGE"]
    handoffs = []
    for exp in expected:
        out = eng.step()
        assert out.next_phase == exp, out.message
        persisted = load_state(eng.paths.state_file)
        assert persisted.phase.value == exp
        if exp == "FIX":
            handoffs.append(persisted.last_review_comment_url)
    comment_a, comment_b, comment_c = (controller_review_comment(eng, n).url for n in (1, 2, 3))
    assert handoffs == [comment_a, comment_b]
    assert len(fix_prompts) == 2
    first, second = fix_prompts
    assert f"Verified review comment: {comment_a}" in first and comment_b not in first
    assert f"Verified review comment: {comment_b}" in second and comment_a not in second
    assert "Review round: 1" in first and "Review round: 2" in second
    assert "R1-F1" in first and "R2-F1" in second
    assert heads[0] in first and heads[1] in second  # each fix starts from its reviewed HEAD
    assert heads[2] == eng.origin.head("autoforge/2")
    s = load_state(eng.paths.state_file)
    assert s.last_review_comment_url == comment_c
    assert [r["review_comment_url"] for r in s.review_history] == [comment_a, comment_b, comment_c]


def test_run_loops_until_ready_for_merge(tmp_state_dir):
    gh = FakeGitHub()

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        head = gh.prs[PR].head_sha  # the controller-published implementation commit
        return block(review_result(1, head))

    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    eng._save()
    outcomes = eng.run(max_steps=50)
    assert [o.next_phase for o in outcomes] == ["ANALYZE_EXECUTE", "REVIEW", "READY_FOR_MERGE"]
    assert eng.state.phase == Phase.READY_FOR_MERGE


@pytest.mark.parametrize("sandbox_mode", ["disabled", "required"])
def test_gate_open_loop_merges_via_controller_then_update_epic_to_done(tmp_state_dir, sandbox_mode):
    """READY_FOR_MERGE -> MERGE (controller merges) -> UPDATE_EPIC (agent) -> DONE.

    No agent is invoked for MERGE; the fake records the exact `gh pr merge`
    binding and flips the PR to MERGED, which the controller re-reads before
    counting the merge.
    """
    gh = FakeGitHub()
    phases_seen = []

    def agent(req):
        phases_seen.append(req.phase)
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "REVIEW":
            head = gh.prs[PR].head_sha  # the controller-published implementation commit
            return block(review_result(1, head))
        if req.phase == "UPDATE_EPIC":
            assert "Never merge a pull request" in req.prompt
            assert "gh pr merge" not in req.prompt.replace("no `gh pr merge`", "")
            return block(
                {
                    "phase": "UPDATE_EPIC",
                    "status": "success",
                    "progress": "Issue done; the PR is merged.",
                    "roadmap_section": "- [x] done",
                    "next_issue_url": None,
                }
            )
        raise AssertionError(f"unexpected call {req.phase}")

    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    eng.config.safety.allow_merge = True
    eng.config.sandbox.mode = sandbox_mode
    eng._save()
    # CLI flag alone does not open the gate: run() holds at READY_FOR_MERGE
    outcomes = eng.run(max_steps=50)
    assert [o.next_phase for o in outcomes] == ["ANALYZE_EXECUTE", "REVIEW", "READY_FOR_MERGE"]
    assert gh.merges == []
    assert eng.run(max_steps=50) == []  # still holding: config alone is not enough

    # Gate open (config AND flag): run() continues through the controller-side
    # verification, MERGE and UPDATE_EPIC to DONE without a separate `step` each.
    outcomes = eng.run(max_steps=50, allow_merge=True)
    assert [o.next_phase for o in outcomes] == ["MERGE", "UPDATE_EPIC", "DONE"]
    assert load_state(eng.paths.state_file).phase == Phase.DONE

    implemented = eng.origin.head("autoforge/2")
    assert implemented is not None
    assert gh.merges == [(PR, "squash", implemented, False)]
    assert gh.prs[PR].state == "MERGED"
    assert phases_seen == ["ANALYZE_EXECUTE", "REVIEW", "UPDATE_EPIC"]  # no MERGE agent call
    s = load_state(eng.paths.state_file)
    assert s.counted_merged_prs == [PR] and s.merged_since_epic_update == 0  # reset by UPDATE_EPIC
    assert s.phase == Phase.DONE
    _sandboxed_as_configured(eng, sandbox_mode)


def test_runaway_review_fix_loop_is_bounded(tmp_state_dir):
    """Issue #9 evidence: reviewer always returns one finding, fixer always pushes.

    Before the loop bounds, ``run(max_steps=50)`` executed 50 steps (25 review
    rounds) and a second ``run`` continued. Now the run ends BLOCKED with an
    explicit reason, the findings stay persisted, and nothing is merged.
    """
    gh = FakeGitHub()
    phases_seen = []
    rounds = {"n": 0}

    def agent(req):
        phases_seen.append(req.phase)
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "REVIEW":
            rounds["n"] += 1
            rnd = rounds["n"]
            finding = {
                "id": f"R{rnd}-F1",
                "classification": "nit",
                "title": "prefer the other refactor",
                "location": "src/x.py:1",
                "required_resolution": "Undo the refactor and apply the other one",
            }
            return block(review_result(rnd, gh.prs[PR].head_sha, [finding]))
        if req.phase == "FIX":
            prev = gh.prs[PR].head_sha
            new = fix_commit(req)
            return block(
                {
                    "phase": "FIX",
                    "status": "success",
                    "previous_head_sha": prev,
                    "head_sha": new,
                    "resolutions": [
                        {"finding_id": f"R{rounds['n']}-F1", "resolution": "fixed"},
                    ],
                }
            )
        raise AssertionError(f"unexpected call {req.phase}")

    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    outcomes = eng.run(max_steps=50)
    assert outcomes[-1].next_phase == "BLOCKED"
    assert len(outcomes) < 50
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED
    assert s.review_round == 2  # identical resolutions in rounds 1 and 2 -> stagnant
    assert "identical resolutions" in s.block_reason and "nothing was merged" in s.block_reason
    assert s.open_findings[0]["id"] == "R2-F1"
    assert phases_seen == ["ANALYZE_EXECUTE", "REVIEW", "FIX", "REVIEW"]
    # a second run does not continue the loop: BLOCKED is terminal
    assert eng.run(max_steps=50) == []
    assert load_state(eng.paths.state_file).step_count == s.step_count
    assert gh.merges == [] and gh.prs[PR].state == "OPEN"


def _full_lifecycle_agent(gh: FakeGitHub):
    """One implementation, a review with a finding, a fix, a clean review, and
    the EPIC update that ends the run: every agent-driven lifecycle edge."""

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "REVIEW" and "Round 1" in req.prompt:
            head = gh.prs[PR].head_sha  # the controller-published implementation commit
            finding = {
                "id": "R1-F1",
                "classification": "non-blocked",
                "title": "missing test",
                "location": "tests/",
                "required_resolution": "add a regression test",
            }
            return block(review_result(1, head, [finding]))
        if req.phase == "FIX":
            prev = gh.prs[PR].head_sha
            new = fix_commit(req)
            return block(
                {
                    "phase": "FIX",
                    "status": "success",
                    "previous_head_sha": prev,
                    "head_sha": new,
                    "resolutions": [
                        {"finding_id": "R1-F1", "resolution": "fixed", "commit_sha": new}
                    ],
                }
            )
        if req.phase == "REVIEW" and "Round 2" in req.prompt:
            return block(review_result(2, gh.prs[PR].head_sha))
        if req.phase == "UPDATE_EPIC":
            return block(
                {
                    "phase": "UPDATE_EPIC",
                    "status": "success",
                    "progress": "Issue done; the PR is merged.",
                    "roadmap_section": "- [x] done",
                    "next_issue_url": None,
                }
            )
        raise AssertionError(f"unexpected call {req.phase}")

    return agent


def test_every_lifecycle_transition_is_decided_by_decide_next_phase(tmp_state_dir, monkeypatch):
    """Issue #2: the engine verifies and applies; ``transitions.decide_next_phase``
    is the one function that chooses the next phase.

    The decision function is wrapped, never replaced: the run below is the
    real lifecycle, and each step's transition must be exactly the decision
    the function returned for that step, from the inputs the engine verified
    (the reviewer's ``needs_fix_round``, the controller's own
    ``head_changed_after_review`` / ``replan`` observations, the verified
    ``next_issue_url``). A step that named its target phase itself would
    advance without a matching decision and fail here.
    """
    decisions: list[tuple[Phase, dict, Phase]] = []

    def recording(current, result, mode):
        nxt = decide_next_phase(current, result, mode)
        decisions.append((current, dict(result), nxt))
        return nxt

    monkeypatch.setattr(engine_mod, "decide_next_phase", recording)
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, _full_lifecycle_agent(gh), github=gh, origin=True)
    eng.config.safety.allow_merge = True
    eng._save()

    outcomes = eng.run(max_steps=50, allow_merge=True)
    transitions = [(Phase(o.previous_phase), Phase(o.next_phase)) for o in outcomes]
    assert transitions == [
        (Phase.INITIALIZING, Phase.ANALYZE_EXECUTE),
        (Phase.ANALYZE_EXECUTE, Phase.REVIEW),
        (Phase.REVIEW, Phase.FIX),
        (Phase.FIX, Phase.REVIEW),
        (Phase.REVIEW, Phase.READY_FOR_MERGE),
        (Phase.READY_FOR_MERGE, Phase.MERGE),
        (Phase.MERGE, Phase.UPDATE_EPIC),
        (Phase.UPDATE_EPIC, Phase.DONE),
    ]
    assert [(frm, nxt) for frm, _, nxt in decisions] == transitions
    assert [result for _, result, _ in decisions] == [
        {},
        {},
        {"needs_fix_round": True, "replan": False},
        {},
        {"needs_fix_round": False},
        {"head_changed_after_review": False},
        {"head_changed_after_review": False},
        {"next_issue_url": None},
    ]
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.DONE
    assert gh.merges == [(PR, "squash", s.reviewed_head_sha, False)]
    assert s.reviewed_head_sha == eng.origin.head("autoforge/2")


def test_a_decision_outside_the_topology_is_refused_before_it_is_applied(
    tmp_state_dir, monkeypatch
):
    """The decision is still validated where it is applied: an edge the
    topology does not declare raises and leaves the persisted phase alone."""

    def rogue(current, result, mode):
        if current is Phase.REVIEW:
            return Phase.DONE  # legal LOCAL edge, illegal REMOTE edge
        return decide_next_phase(current, result, mode)

    monkeypatch.setattr(engine_mod, "decide_next_phase", rogue)
    gh = FakeGitHub()
    eng = make_engine(tmp_state_dir, _full_lifecycle_agent(gh), github=gh, origin=True)
    eng._save()
    assert [o.next_phase for o in (eng.step(), eng.step())] == ["ANALYZE_EXECUTE", "REVIEW"]
    with pytest.raises(StateTransitionError, match="REVIEW -> DONE"):
        eng.step()
    assert load_state(eng.paths.state_file).phase == Phase.REVIEW


# --- Pi provider parity at the engine boundary (#133) -----------------------


def _six_round_agent(gh: FakeGitHub, clean_round: int = 6):
    """An implementation, ``clean_round - 1`` review rounds that each raise one
    genuinely new finding (a distinct required resolution, so no stagnation),
    a fix after each, a clean review in ``clean_round`` and the EPIC update.

    Six rounds reach every review profile: round 1, rounds 2-5 and 6+.
    """

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "REVIEW":
            # The controller posts one comment per completed round.
            rnd = len(gh.comments.get(PR, [])) + 1
            ids = [] if rnd == clean_round else [f"R{rnd}-F1"]
            findings = [
                {
                    "id": i,
                    "classification": "non-blocked",
                    "title": f"gap {rnd}",
                    "location": f"src/x{rnd}.py:1",
                    "required_resolution": f"cover case {rnd}",
                }
                for i in ids
            ]
            return block(review_result(rnd, gh.prs[PR].head_sha, findings))
        if req.phase == "FIX":
            rnd = len(gh.comments.get(PR, []))
            prev = gh.prs[PR].head_sha
            new = fix_commit(req, f"Fix round {rnd} (#2)")
            return block(
                {
                    "phase": "FIX",
                    "status": "success",
                    "previous_head_sha": prev,
                    "head_sha": new,
                    "resolutions": [
                        {"finding_id": f"R{rnd}-F1", "resolution": "fixed", "commit_sha": new}
                    ],
                }
            )
        if req.phase == "UPDATE_EPIC":
            return block(
                {
                    "phase": "UPDATE_EPIC",
                    "status": "success",
                    "progress": "Issue done; the PR is merged.",
                    "roadmap_section": "- [x] done",
                    "next_issue_url": None,
                }
            )
        raise AssertionError(f"unexpected call {req.phase}")

    return agent


def test_an_all_pi_run_routes_every_launch_through_its_profile(tmp_state_dir, tmp_path_factory):
    """Every profile on ``provider: pi``: ANALYZE -> REVIEW r1 -> FIX -> REVIEW
    rounds 2-5 and 6 -> READY_FOR_MERGE -> MERGE -> UPDATE_EPIC -> DONE.

    Each RPC child's ``--model`` / ``--thinking`` names the profile the engine
    routed that launch through, every launch runs in the per-issue worktree,
    nothing reaches the Claude Code provider, and the controller (never the
    agent) merges once the gate is open in this test's config.
    """
    from tests.pi_fake import PI_PROFILES, PiFake, flag, make_pi_engine

    fake = PiFake(tmp_path_factory.mktemp("pi"))
    gh = FakeGitHub()
    eng = make_pi_engine(tmp_state_dir, fake, _six_round_agent(gh), github=gh, origin=True)
    eng.config.safety.allow_merge = True
    eng._save()

    outcomes = eng.run(max_steps=50, allow_merge=True)

    assert [o.next_phase for o in outcomes] == [
        "ANALYZE_EXECUTE",
        *["REVIEW", "FIX"] * 5,
        "REVIEW",
        "READY_FOR_MERGE",
        "MERGE",
        "UPDATE_EPIC",
        "DONE",
    ]
    expected = [
        "analyze_execute",
        "review_round_1",
        "fix",
        *["review_round_2_5", "fix"] * 4,
        "review_round_6_plus",
        "update_epic",
    ]
    assert [req.profile.name for req in eng.pi.calls] == expected
    launches = fake.launches()
    assert [(flag(e["argv"], "--model"), flag(e["argv"], "--thinking")) for e in launches] == [
        PI_PROFILES[name] for name in expected
    ]
    worktree = str(eng.agent_worktree_path())
    assert {e["cwd"] for e in launches} == {worktree}
    assert len(fake.auth_checks()) == len(expected)  # one OAuth preflight per launch
    assert eng.provider.calls == []
    assert gh.merges == [(PR, "squash", eng.origin.head("autoforge/2"), False)]
    # ADR 0004 parity: the Pi agent returned the PR text, each round's review
    # and the progress text; the controller opened the PR (#161), posted each
    # round's review comment (#162) and posted the progress on the EPIC with
    # the marker, once each.
    pr_body = analyze_payload("")["pr_body"]
    closing = f"Closes #2\n\n{render_implementation_marker(ISSUE)}"
    marker = render_progress_marker(ISSUE, PR)
    reviews = [controller_review_comment(eng, n).body for n in range(1, 7)]
    assert gh.effect_writes == [
        ("create_pull_request", "owner/repo", f"{pr_body}\n\n{closing}"),
        *(("create_pr_comment", PR, body) for body in reviews),
        ("create_issue_comment", EPIC, f"Issue done; the PR is merged.\n\n{marker}"),
    ]
    assert all("## Summary\n\nReady." in body for body in reviews)
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.DONE and s.counted_merged_prs == [PR]


@pytest.mark.parametrize(
    "pi_profiles",
    [
        ["review_round_1", "review_round_2_5", "review_round_6_plus"],
        ["analyze_execute", "fix", "update_epic"],
    ],
    ids=["claude-writers-pi-reviewers", "pi-writers-claude-reviewers"],
)
def test_a_mixed_config_routes_each_profile_to_its_own_provider(
    tmp_state_dir, tmp_path_factory, pi_profiles
):
    """Claude Code and Pi profiles in one run: each launch goes to the provider
    its profile names, and the run's phases are those of an all-Claude run."""
    from tests.pi_fake import PI_PROFILES, PiFake, flag, make_pi_engine

    fake = PiFake(tmp_path_factory.mktemp("pi"))
    gh = FakeGitHub()
    cfg = default_config()
    for name in set(PI_PROFILES) - set(pi_profiles):  # everything else on Claude Code
        cfg.profiles[name] = dataclasses.replace(cfg.profile("analyze_execute"), name=name)
    eng = make_pi_engine(
        tmp_state_dir,
        fake,
        _full_lifecycle_agent(gh),
        github=gh,
        cfg=cfg,
        names=pi_profiles,
        origin=True,
    )
    eng.config.safety.allow_merge = True
    eng._save()

    outcomes = eng.run(max_steps=50, allow_merge=True)

    assert [o.next_phase for o in outcomes][-4:] == [
        "READY_FOR_MERGE",
        "MERGE",
        "UPDATE_EPIC",
        "DONE",
    ]
    order = ["analyze_execute", "review_round_1", "fix", "review_round_2_5", "update_epic"]
    on_pi = [name for name in order if name in pi_profiles]
    assert [req.profile.name for req in eng.pi.calls] == on_pi
    assert [req.profile.name for req in eng.provider.calls] == [
        name for name in order if name not in pi_profiles
    ]
    assert [
        (flag(e["argv"], "--model"), flag(e["argv"], "--thinking")) for e in fake.launches()
    ] == [PI_PROFILES[name] for name in on_pi]
    assert all(req.profile.provider == "claude" for req in eng.provider.calls)
    fixed = eng.origin.head("autoforge/2")
    assert gh.merges == [(PR, "squash", fixed, False)]
    assert load_state(eng.paths.state_file).reviewed_head_sha == fixed


# -- no credential route, and a read context, for every launch (#165) -----------

EXPECTED_READ_CONTEXT = {
    "ANALYZE_EXECUTE": {"README.md", "issue.md"},
    "REVIEW": {
        "README.md",
        "issue.md",
        "pull-request.md",
        "checks.md",
        "follow-up-issues.md",
        "operator-evidence.md",
    },
    "FIX": {"README.md", "issue.md", "review-comment.md", "follow-up-issues.md"},
    "UPDATE_EPIC": {"README.md", "epic.md", "epic-issues.md"},
}


def _operator_identity(monkeypatch, tmp_path) -> None:
    config = tmp_path / "operator-gitconfig"
    config.write_text(
        "[user]\n\tname = Operator Name\n\temail = op@example.invalid\n"
        "[credential]\n\thelper = store\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))


def test_every_remote_launch_gets_no_credential_route_and_its_read_context(
    tmp_state_dir, tmp_path, monkeypatch
):
    """The whole lifecycle with an operator shell holding every credential
    route: each launch's environment carries none of them (gh isolated,
    helpers reset, the SSH agent withheld, the operator's identity
    supplied), and each prompt names a read context the controller wrote
    under the state directory, outside the worktree, redacted."""
    pollute_operator_environment(monkeypatch, tmp_path)
    _operator_identity(monkeypatch, tmp_path)
    gh = FakeGitHub()
    gh.issues[ISSUE].body = "Implement the feature. Token: ghp_" + "Q" * 36
    gh.issues[EPIC].body = "Roadmap: #2 then #3 then https://github.com/owner/repo/issues/9"
    gh.add_issue("https://github.com/owner/repo/issues/3", "Next thing")
    inner = _full_lifecycle_agent(gh)
    seen: list[tuple[str, set[str]]] = []

    def agent(req):
        assert_isolated_launch(eng, req)
        identity = subprocess.run(
            ["git", "config", "user.name"],
            cwd=req.cwd,
            env=launch_environment(eng, req),
            capture_output=True,
            text=True,
        )
        assert identity.stdout.strip() == "Operator Name"
        context = read_context_of(req.prompt)
        assert context.is_dir() and context.is_relative_to(eng.paths.state_dir.resolve())
        assert not context.is_relative_to(Path(req.cwd).resolve())
        files = {p.name for p in context.iterdir()}
        seen.append((req.phase, files))
        text = "".join(p.read_text() for p in context.iterdir())
        assert "Q" * 36 not in text
        if req.phase == "ANALYZE_EXECUTE":
            assert "Implement the feature." in (context / "issue.md").read_text()
        if req.phase == "UPDATE_EPIC":
            states = (context / "epic-issues.md").read_text()
            assert "issues/2: OPEN: Feature" in states and "issues/3: OPEN: Next thing" in states
            assert "issues/9: (could not be read)" in states
        return inner(req)

    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    eng.config.safety.allow_merge = True
    eng._save()
    outcomes = eng.run(max_steps=50, allow_merge=True)
    assert outcomes[-1].next_phase == "DONE", outcomes[-1].message
    assert [phase for phase, _ in seen] == [
        "ANALYZE_EXECUTE",
        "REVIEW",
        "FIX",
        "REVIEW",
        "UPDATE_EPIC",
    ]
    for phase, files in seen:
        assert EXPECTED_READ_CONTEXT[phase] <= files, (phase, files)
    # Round 2 also reads round 1's comment.
    assert "previous-review-comment.md" in seen[3][1]
    # Each launch's record names what the controller set, by name only.
    run_dir = eng.paths.logs_dir / eng.state.run_id
    for line in (run_dir / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if "metadata" in event:
            assert "GH_TOKEN" in event["metadata"]["env_set"]


def test_a_refused_checks_read_lets_review_and_fix_run_and_blocks_the_merge(tmp_state_dir):
    """#291: GitHub refuses the controller's fine-grained token a private PR's
    check runs. Every identity read still answers, so REVIEW and FIX run on
    the trusted PR and HEAD (the reviewer told the checks are unknown, not
    absent), and the merge gate, which needs the checks, BLOCKS at
    READY_FOR_MERGE on the first refusal, naming the credential and its
    remedy: nothing merged, nothing counted, the refusal read once."""
    gh = FakeGitHub()
    gh.checks_error = checks_refused()
    inner = _full_lifecycle_agent(gh)
    reviewed_checks: list[str] = []

    def agent(req):
        if req.phase == "REVIEW":
            reviewed_checks.append((read_context_of(req.prompt) / "checks.md").read_text())
        return inner(req)

    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    eng.config.safety.allow_merge = True
    eng._save()
    outcomes = eng.run(max_steps=50, allow_merge=True)

    assert [req.phase for req in eng.provider.calls] == [
        "ANALYZE_EXECUTE",
        "REVIEW",
        "FIX",
        "REVIEW",
    ]
    assert [o.next_phase for o in outcomes][-2:] == ["READY_FOR_MERGE", "BLOCKED"]
    assert len(reviewed_checks) == 2
    for text in reviewed_checks:
        assert "GitHub refused the controller's credential" in text and "unknown" in text
        assert "| " not in text  # no check listed: none was read
    s = load_state(eng.paths.state_file)
    assert s.phase == Phase.BLOCKED
    reason = s.block_reason or ""
    assert f"the check results of PR {PR} could not be read" in reason
    assert "Resource not accessible by personal access token" in reason
    assert "cannot be given Checks: read" in reason and "`gh auth login`" in reason
    assert reason.endswith("then 'unblock'")
    assert gh.merges == [] and s.counted_merged_prs == []
    # Two REVIEW launches and one merge-gate read: a refusal is never re-checked.
    assert gh.calls.count(("get_pr_with_checks", PR)) == 3


def test_an_all_pi_run_gets_the_same_isolation_in_every_child(
    tmp_state_dir, tmp_path_factory, monkeypatch
):
    """Provider parity: Pi's RPC child and its auth preflight see exactly what
    a scripted provider's launch would, checked inside the fake `pi`."""
    from tests.pi_fake import PiFake, make_pi_engine

    tmp_path = tmp_path_factory.mktemp("operator")
    pollute_operator_environment(monkeypatch, tmp_path)
    _operator_identity(monkeypatch, tmp_path)
    fake = PiFake(tmp_path_factory.mktemp("pi"))
    gh = FakeGitHub()
    eng = make_pi_engine(tmp_state_dir, fake, _full_lifecycle_agent(gh), github=gh, origin=True)
    eng.config.safety.allow_merge = True
    eng._save()
    outcomes = eng.run(max_steps=50, allow_merge=True)
    assert outcomes[-1].next_phase == "DONE", outcomes[-1].message
    starts = [e for e in fake.entries() if "isolation" in e]
    assert len(fake.launches()) == 5 and len(starts) == 10  # each launch and its preflight
    for entry in starts:
        assert_isolated_names(entry["env"])
        isolation = entry["isolation"]
        # The command scope's reset is the last helper git sees.
        assert isolation.pop("credential_helpers")[-1:] == [""], entry
        assert isolation == {
            "gh_token_placeholder": True,
            "gh_config_dir_empty": True,
            "http_client_config_empty": True,
            "git_terminal_prompt": "0",
            "git_identity": "Operator Name",
        }, entry


# -- operator actions end to end: PR #242's reproduction (#243, section 12.1) -----------
T16 = "https://github.com/owner/repo/issues/16"
T158 = "https://github.com/owner/repo/issues/158"
NOTES = {
    T16: "Coordination note from #2: the scheduler contract is recorded in ADR 0008.",
    T158: "Coordination note from #2: workers settle at their single-issue boundary.",
}
NOTES_FINDING = {
    "id": "R1-F1",
    "classification": "blocked",
    "title": "coordination notes missing",
    "location": "docs/adr/0008.md:1",
    "required_resolution": "the acceptance criteria require coordination notes on #16 and #158",
}


def _notes_resolution(*targets: str) -> dict:
    return {
        "finding_id": "R1-F1",
        "resolution": "operator_acceptance",
        "rationale": "The acceptance criteria require coordination notes on #16 and #158, "
        "which only the operator can post.",
        "operator_acceptance": {
            "actions": [
                {
                    "kind": "related_issue_comment",
                    "issue": int(t.rsplit("/", 1)[1]),
                    "body": NOTES[t],
                }
                for t in targets
            ]
        },
    }


def _notes_run(tmp_state_dir, reviews: list[str], fixes: list[dict] | None = None):
    """A run to BLOCKED on two operator actions: (engine, GitHub, fixer prompts).

    The issue body references #16 and #158, both closed; REVIEW round 1
    raises R1-F1 [blocked]; the fixer commits nothing and asks the operator
    for both notes. ``reviews`` collects each later round's operator
    evidence; a later FIX launch takes the next of ``fixes``.
    """
    gh = FakeGitHub()
    gh.issues[ISSUE].body = "Implement the feature; post coordination notes on #16 and #158."
    gh.add_issue(T16, "Scheduler", state="CLOSED")
    gh.add_issue(T158, "Worker", state="CLOSED")
    fix_prompts: list[str] = []
    queue = [_notes_resolution(T16, T158), *(fixes or [])]
    run = SimpleNamespace(eng=None, agent=None)

    def agent(req):
        eng = run.eng
        if req.phase == "ANALYZE_EXECUTE":
            return implement(req)
        if req.phase == "REVIEW":
            head = gh.prs[eng.state.current_pr_url].head_sha
            if "Round 1" in req.prompt:
                return block(review_result(1, head, [NOTES_FINDING]))
            reviews.append((read_context_of(req.prompt) / "operator-evidence.md").read_text())
            assert "is a claim, not evidence" in req.prompt
            return block(review_result(2, head))
        if req.phase == "FIX":
            assert_isolated_launch(eng, req)
            fix_prompts.append(req.prompt)
            return fixer(queue.pop(0), commit=False)(req)
        raise AssertionError(f"unexpected call {req.phase}")

    eng = make_engine(tmp_state_dir, agent, github=gh, origin=True)
    run.eng, run.agent = eng, agent
    eng._save()
    for expected in ("ANALYZE_EXECUTE", "REVIEW", "FIX", "BLOCKED"):
        out = eng.step()
        assert out.next_phase == expected, out.message
    return eng, gh, fix_prompts, run


def _restarted(eng, tmp_state_dir, gh, run):
    """A new controller process on the same state, git remote, GitHub and agent."""
    eng.close()
    again = make_engine(tmp_state_dir, run.agent, github=gh)
    connect_origin(again, eng.origin)
    again.origin = eng.origin
    again.load()
    run.eng = again
    return again


def _operator_records(eng) -> dict[str, EffectRecord]:
    records = [EffectRecord.from_dict(r) for r in load_state(eng.paths.state_file).effect_records]
    return {r.target["issue_url"]: r for r in records if r.kind is EffectKind.OPERATOR_COMMENT}


def _marked(gh: FakeGitHub, target: str) -> list:
    return [c for c in gh.comments.get(target, []) if scan(OPERATOR_COMMENT, c.body).claims]


def _die_after_call(fn, n: int):
    """``fn``, whose ``n``-th call completes and then the process dies (a power loss)."""
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == n:
            raise RuntimeError("power loss")
        return result

    return wrapper


def test_operator_notes_on_related_issues_end_to_end(tmp_state_dir):
    """PR #242's reproduction: two notes the fixer cannot post. The plan is
    saved before BLOCKED and nothing is posted by the controller; assertions
    (a ticked checkbox, a "done" comment, a reason with URLs) fulfil nothing;
    each exact payload is verified as posted, partial completion survives a
    restart, and REVIEW round 2 reads both as exact evidence. One fixer
    launch in all, no K1, no merge."""
    reviews: list[str] = []
    eng, gh, fix_prompts, run = _notes_run(tmp_state_dir, reviews)
    reviewed = gh.prs[eng.state.current_pr_url].head_sha
    records = _operator_records(eng)
    assert list(records) == [T16, T158]
    assert [r.stage for r in records.values()] == [Stage.INTENDED, Stage.INTENDED]
    assert all(r.action_id.startswith("X-") for r in records.values())
    assert [w[0] for w in gh.effect_writes] == ["create_pull_request", "create_pr_comment"]
    x16, x158 = records[T16].action_id, records[T158].action_id
    listed = cli._operator_actions(load_state(eng.paths.state_file))
    assert [(a["action_id"], a["state"]) for a in listed] == [(x16, "pending"), (x158, "pending")]

    # Assertion is not evidence: nothing is read as fulfilment, nothing is written.
    gh.prs[eng.state.current_pr_url].body += "\n- [x] coordination notes posted on #16 and #158"
    gh.add_comment(ISSUE, 7001, f"done: {T16}#issuecomment-1 and {T158}#issuecomment-2")
    raw = eng.paths.state_file.read_bytes()
    out = eng.unblock(f"posted them: {T16}#issuecomment-1 {T158}#issuecomment-2")
    assert not out.unblocked and out.phase == "BLOCKED", out.message
    assert eng.paths.state_file.read_bytes() == raw and len(fix_prompts) == 1

    # #16 first: verified exact, saved, and the run blocks on #158 alone.
    gh.add_comment(T16, 9016, records[T16].body)
    assert eng.unblock("posted #16's note").phase == "FIX"
    assert eng.step().next_phase == "BLOCKED"
    eng = _restarted(eng, tmp_state_dir, gh, run)
    records = _operator_records(eng)
    assert records[T16].stage is Stage.OBSERVED and records[T16].observed["evidence"] == "exact"
    assert records[T158].stage is Stage.INTENDED
    assert f"Verified so far: {x16}" in eng.state.block_reason

    gh.add_comment(T158, 9158, records[T158].body)
    assert eng.unblock("posted #158's note").phase == "FIX"
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    assert x16 in out.message and x158 in out.message
    state = load_state(eng.paths.state_file)
    assert state.review_round == 1 and state.current_head_sha == reviewed
    assert [
        (o["action_id"], o["outcome"], o["evidence"]) for o in state.operator_comment_outcomes
    ] == [
        (x16, "verified", "exact"),
        (x158, "verified", "exact"),
    ]
    assert eng.step().next_phase == "READY_FOR_MERGE"
    [evidence] = reviews
    assert evidence.count("verified, exact") == 2 and NOTES[T16] in evidence
    state = load_state(eng.paths.state_file)
    assert state.phase == Phase.READY_FOR_MERGE and state.last_review_result == "clean"
    assert len(fix_prompts) == 1 and gh.prs[eng.state.current_pr_url].head_sha == reviewed
    assert [len(_marked(gh, t)) for t in (T16, T158)] == [1, 1]
    writes = [w[0] for w in gh.effect_writes]
    assert "create_issue_comment" not in writes and not any("merge" in w for w in writes)


@pytest.mark.parametrize(
    "die", ["after the first observation", "after the second observation", "before completion"]
)
def test_operator_notes_survive_a_restart_at_every_window(tmp_state_dir, die):
    """Both payloads posted at once; the FIX step dies after each observation
    save, or before the completion save. A new process completes FIX from the
    journal: one marker comment per target, two verified outcomes, the fixer
    launched once, the PR still at the reviewed HEAD."""
    reviews: list[str] = []
    eng, gh, fix_prompts, run = _notes_run(tmp_state_dir, reviews)
    reviewed = gh.prs[eng.state.current_pr_url].head_sha
    for target, record in _operator_records(eng).items():
        gh.add_comment(target, 9000 + int(target.rsplit("/", 1)[1]), record.body)
    assert eng.unblock("posted both notes").phase == "FIX"
    if die == "before completion":
        eng._record_operator_outcomes = _die_after_call(eng._record_operator_outcomes, 1)
    else:
        nth = 1 if die == "after the first observation" else 2
        eng._persist_effect = _die_after_call(eng._persist_effect, nth)
    with pytest.raises(RuntimeError, match="power loss"):
        eng.step()
    assert load_state(eng.paths.state_file).phase == Phase.FIX
    eng = _restarted(eng, tmp_state_dir, gh, run)
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    state = load_state(eng.paths.state_file)
    outcomes = [(o["outcome"], o["evidence"]) for o in state.operator_comment_outcomes]
    assert outcomes == [("verified", "exact"), ("verified", "exact")]
    assert [len(_marked(gh, t)) for t in (T16, T158)] == [1, 1]
    assert len(fix_prompts) == 1 and gh.prs[eng.state.current_pr_url].head_sha == reviewed
    assert "create_issue_comment" not in [w[0] for w in gh.effect_writes]


def test_operator_notes_bound_to_existing_comments(tmp_state_dir):
    """Variant: unmarked notes already on #16 and #158 are bound in one
    unblock; both are verified ``bound`` and REVIEW is told the controller
    did not judge their adequacy."""
    reviews: list[str] = []
    eng, gh, fix_prompts, _ = _notes_run(tmp_state_dir, reviews)
    records = _operator_records(eng)
    c16 = gh.add_comment(T16, 5016, "Coordination note (2026-10-05): see ADR 0008.")
    c158 = gh.add_comment(T158, 5158, "Coordination note (2026-10-05): workers settle.")
    out = eng.unblock(
        "the notes were posted earlier",
        bind_evidence=[(records[T16].action_id, c16.url), (records[T158].action_id, c158.url)],
    )
    assert out.unblocked and out.phase == "FIX", out.message
    assert eng.step().next_phase == "REVIEW"
    assert eng.step().next_phase == "READY_FOR_MERGE"
    [evidence] = reviews
    assert evidence.count("operator-bound; adequacy not verified by the controller") == 2
    state = load_state(eng.paths.state_file)
    assert [o["evidence"] for o in state.operator_comment_outcomes] == ["bound", "bound"]
    assert len(fix_prompts) == 1 and "create_issue_comment" not in str(gh.effect_writes)


def test_operator_notes_withdrawn_after_one_is_verified(tmp_state_dir):
    """Variant: #16 is verified, then #158's action is declined. #16 stays a
    verified outcome, #158 is declined, and the fixer runs once more with
    both listed in its prompt."""
    reviews: list[str] = []
    no_change = {
        "finding_id": "R1-F1",
        "resolution": "no_change_with_rationale",
        "rationale": "The #16 note covers the contract; #158 needs no separate note, as "
        "the operator decided when declining it.",
    }
    eng, gh, fix_prompts, _ = _notes_run(tmp_state_dir, reviews, fixes=[no_change])
    records = _operator_records(eng)
    gh.add_comment(T16, 9016, records[T16].body)
    assert eng.unblock("posted #16's note").phase == "FIX"
    assert eng.step().next_phase == "BLOCKED"
    x16, x158 = records[T16].action_id, records[T158].action_id
    out = eng.unblock("#158 needs no note", decline_action=[x158])
    assert out.unblocked and out.phase == "FIX", out.message
    state = load_state(eng.paths.state_file)
    assert [(o["action_id"], o["outcome"]) for o in state.operator_comment_outcomes] == [
        (x16, "verified"),
        (x158, "declined"),
    ]
    assert eng.step().next_phase == "REVIEW"
    assert len(fix_prompts) == 2
    assert x16 in fix_prompts[1] and x158 in fix_prompts[1]
    assert "#158 needs no note" in fix_prompts[1]
