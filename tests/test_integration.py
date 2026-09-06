"""Scripted end-to-end loop: INITIALIZING -> ... -> READY_FOR_MERGE.

Every agent call is a ScriptedProvider handler that mutates FakeGitHub the
way the real agent would (create PR, post comment, push fix). State is
reloaded from disk after every step to prove persistence.
"""

import json

from autoforge.state import load_state
from autoforge.transitions import Phase
from tests.conftest import (
    BRANCH,
    ISSUE,
    PR,
    SHA_A,
    SHA_B,
    FakeGitHub,
    block,
    comment_url,
    make_engine,
    review_comment_body,
)


def test_full_loop_to_ready_for_merge(tmp_state_dir):
    gh = FakeGitHub()
    phases_seen = []

    def agent(req):
        phases_seen.append((req.phase, req.profile.model, req.profile.effort))
        if req.phase == "ANALYZE_EXECUTE":
            gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
            return block(
                {
                    "phase": "ANALYZE_EXECUTE",
                    "status": "success",
                    "issue_url": ISSUE,
                    "pr_url": PR,
                    "head_sha": SHA_A,
                    "branch": BRANCH,
                }
            )
        if req.phase == "REVIEW" and "Round 1" in req.prompt:
            gh.add_comment(PR, 100, review_comment_body(1, SHA_A, True, ["R1-F1"]))
            return block(
                {
                    "phase": "REVIEW",
                    "status": "success",
                    "round": 1,
                    "reviewed_head_sha": SHA_A,
                    "review_comment_url": comment_url(PR, 100),
                    "needs_fix_round": True,
                    "findings": [
                        {
                            "id": "R1-F1",
                            "classification": "non-blocked",
                            "title": "missing test",
                            "location": "tests/",
                            "required_resolution": "add a regression test",
                        }
                    ],
                }
            )
        if req.phase == "FIX":
            assert "R1-F1" in req.prompt and SHA_A in req.prompt
            gh.set_head(SHA_B)
            return block(
                {
                    "phase": "FIX",
                    "status": "success",
                    "previous_head_sha": SHA_A,
                    "new_head_sha": SHA_B,
                    "resolutions": [
                        {"finding_id": "R1-F1", "resolution": "fixed", "commit_sha": SHA_B}
                    ],
                }
            )
        if req.phase == "REVIEW" and "Round 2" in req.prompt:
            gh.add_comment(PR, 101, review_comment_body(2, SHA_B, False))
            return block(
                {
                    "phase": "REVIEW",
                    "status": "success",
                    "round": 2,
                    "reviewed_head_sha": SHA_B,
                    "review_comment_url": comment_url(PR, 101),
                    "needs_fix_round": False,
                    "findings": [],
                }
            )
        raise AssertionError(f"unexpected call {req.phase}")

    eng = make_engine(tmp_state_dir, agent, github=gh)
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
    assert s.review_round == 2 and s.reviewed_head_sha == SHA_B == s.current_head_sha
    assert s.last_review_result == "clean" and s.open_findings == []
    assert s.last_review_comment_url == comment_url(PR, 101)
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


def test_run_loops_until_ready_for_merge(tmp_state_dir):
    gh = FakeGitHub()

    def agent(req):
        if req.phase == "ANALYZE_EXECUTE":
            gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
            return block(
                {
                    "phase": "ANALYZE_EXECUTE",
                    "status": "success",
                    "issue_url": ISSUE,
                    "pr_url": PR,
                    "head_sha": SHA_A,
                    "branch": BRANCH,
                }
            )
        gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
        return block(
            {
                "phase": "REVIEW",
                "status": "success",
                "round": 1,
                "reviewed_head_sha": SHA_A,
                "review_comment_url": comment_url(PR, 100),
                "needs_fix_round": False,
                "findings": [],
            }
        )

    eng = make_engine(tmp_state_dir, agent, github=gh)
    eng._save()
    outcomes = eng.run(max_steps=50)
    assert [o.next_phase for o in outcomes] == ["ANALYZE_EXECUTE", "REVIEW", "READY_FOR_MERGE"]
    assert eng.state.phase == Phase.READY_FOR_MERGE


def test_gate_open_loop_merges_via_controller_then_update_epic_to_done(tmp_state_dir):
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
            gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
            return block(
                {
                    "phase": "ANALYZE_EXECUTE",
                    "status": "success",
                    "issue_url": ISSUE,
                    "pr_url": PR,
                    "head_sha": SHA_A,
                    "branch": BRANCH,
                }
            )
        if req.phase == "REVIEW":
            gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
            return block(
                {
                    "phase": "REVIEW",
                    "status": "success",
                    "round": 1,
                    "reviewed_head_sha": SHA_A,
                    "review_comment_url": comment_url(PR, 100),
                    "needs_fix_round": False,
                    "findings": [],
                }
            )
        if req.phase == "UPDATE_EPIC":
            assert "Never merge a pull request" in req.prompt
            assert "gh pr merge" not in req.prompt.replace("no `gh pr merge`", "")
            return block({"phase": "UPDATE_EPIC", "status": "success", "next_issue_url": None})
        raise AssertionError(f"unexpected call {req.phase}")

    eng = make_engine(tmp_state_dir, agent, github=gh)
    eng.config.safety.allow_merge = True
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

    assert gh.merges == [(PR, "squash", SHA_A, False)]
    assert gh.prs[PR].state == "MERGED"
    assert phases_seen == ["ANALYZE_EXECUTE", "REVIEW", "UPDATE_EPIC"]  # no MERGE agent call
    s = load_state(eng.paths.state_file)
    assert s.counted_merged_prs == [PR] and s.merged_since_epic_update == 0  # reset by UPDATE_EPIC
    assert s.phase == Phase.DONE


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
            gh.add_pr(head_sha=SHA_A, branch=BRANCH, linked=[2])
            return block(
                {
                    "phase": "ANALYZE_EXECUTE",
                    "status": "success",
                    "issue_url": ISSUE,
                    "pr_url": PR,
                    "head_sha": SHA_A,
                    "branch": BRANCH,
                }
            )
        if req.phase == "REVIEW":
            rounds["n"] += 1
            rnd = rounds["n"]
            sha = gh.prs[PR].head_sha
            gh.add_comment(PR, 100 + rnd, review_comment_body(rnd, sha, True, [f"R{rnd}-F1"]))
            return block(
                {
                    "phase": "REVIEW",
                    "status": "success",
                    "round": rnd,
                    "reviewed_head_sha": sha,
                    "review_comment_url": comment_url(PR, 100 + rnd),
                    "needs_fix_round": True,
                    "findings": [
                        {
                            "id": f"R{rnd}-F1",
                            "classification": "nit",
                            "title": "prefer the other refactor",
                            "location": "src/x.py:1",
                            "required_resolution": "Undo the refactor and apply the other one",
                        }
                    ],
                }
            )
        if req.phase == "FIX":
            prev = gh.prs[PR].head_sha
            new = f"{rounds['n']:040x}"
            gh.set_head(new)
            return block(
                {
                    "phase": "FIX",
                    "status": "success",
                    "previous_head_sha": prev,
                    "new_head_sha": new,
                    "resolutions": [
                        {"finding_id": f"R{rounds['n']}-F1", "resolution": "fixed"},
                    ],
                }
            )
        raise AssertionError(f"unexpected call {req.phase}")

    eng = make_engine(tmp_state_dir, agent, github=gh)
    eng._save()
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
