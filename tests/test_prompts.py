"""Prompt rendering: substitution, missing vars, special chars/URLs."""

import pytest

from autoforge import prompts
from autoforge.errors import ConfigurationError
from autoforge.prompts import render, required_variables
from autoforge.transitions import Phase


def test_correct_substitution():
    out = render(
        "EPIC={{EPIC_URL}} N={{REVIEW_ROUND}}",
        {"EPIC_URL": "https://github.com/o/r/issues/1", "REVIEW_ROUND": 3},
    )
    assert out == "EPIC=https://github.com/o/r/issues/1 N=3"


def test_missing_required_variable_fails():
    with pytest.raises(ConfigurationError, match="EPIC_URL"):
        render("EPIC={{EPIC_URL}}", {})
    with pytest.raises(ConfigurationError, match="EPIC_URL"):
        render("EPIC={{EPIC_URL}}", {"EPIC_URL": None})


def test_special_characters_and_urls_survive():
    tricky = "https://github.com/o/r/issues/1?x=$HOME&y=`id` * [brackets] {single}"
    out = render("U={{ISSUE_URL}}", {"ISSUE_URL": tricky})
    assert out == f"U={tricky}"


AGENT_TEMPLATES = [
    (Phase.ANALYZE_EXECUTE, "analyze_execute.md"),
    (Phase.REVIEW, "review.md"),
    (Phase.FIX, "fix.md"),
    (Phase.REPLAN_REEXECUTE, "replan_reexecute.md"),
    (Phase.UPDATE_EPIC, "update_epic.md"),
]


def test_all_templates_render_for_all_agent_phases(engine):
    for phase, template in AGENT_TEMPLATES:
        engine.state.phase = phase
        text = engine.render_prompt_for(phase)
        assert "{{" not in text, f"unresolved placeholder in {template}"
        assert "<<<CONTROL_RESULT>>>" in text


def test_merge_is_never_delegated_to_an_agent(engine):
    """Issue #8: common.md forbids merging; no prompt may instruct `gh pr merge`."""
    from autoforge.errors import StateTransitionError
    from autoforge.transitions import AGENT_PHASES

    assert "merge.md" not in prompts.TEMPLATE_FILES
    assert not (prompts.prompts_dir() / "merge.md").exists()
    assert Phase.MERGE not in AGENT_PHASES
    common = prompts.load_template("common.md")
    assert "Never merge a pull request" in common
    for phase, _template in AGENT_TEMPLATES:
        engine.state.phase = phase
        text = engine.render_prompt_for(phase)
        assert "Never merge a pull request" in text
        assert "gh pr merge" not in text.replace("no `gh pr merge`", "")
    engine.state.phase = Phase.MERGE
    with pytest.raises(StateTransitionError, match="no agent prompt"):
        engine.render_prompt_for(Phase.MERGE)


def test_common_template_has_trust_boundary():
    common = prompts.load_template("common.md")
    lowered = common.lower()
    assert "untrusted" in lowered
    assert "ignore previous instructions" in lowered
    assert "CONTROL_RESULT" in common


def test_required_variables_detected():
    assert required_variables("a {{FOO}} b {{BAR}} c") == {"FOO", "BAR"}


def test_review_prompt_contract():
    text = prompts.load_template("review.md")
    for phrase in (
        "Findings",
        "Observations",
        "needs_fix_round",
        "reviewed HEAD",
        "single PR comment",
        "AI Code Review",
        "ai-review-result",
        "Needs another fix round",
        "blocked",
        "non-blocked",
        "nit",
    ):
        assert phrase in text, phrase


def test_fix_prompt_contract():
    text = prompts.load_template("fix.md")
    for phrase in (
        "finding IDs",
        "follow-up issues",
        "no_change_with_rationale",
        "follow_up_created",
        "fixed",
        "previous_head_sha",
        "new_head_sha",
    ):
        assert phrase in text, phrase


def test_replan_prompt_contract():
    text = prompts.load_template("replan_reexecute.md")
    for phrase in (
        "fresh implementation",
        "latest default branch",
        "Previous PR",
        "Historical review findings",
        "do not inherit the previous PR's solution",
        "replacement PR",
        "CONTROL_RESULT",
        "fresh_review_round",
    ):
        assert phrase in text, phrase


def test_implementation_prompt_contract():
    text = prompts.load_template("analyze_execute.md")
    for phrase in ("AGENTS.md", "CLAUDE.md", "CONTROL_RESULT", "pr_url", "head_sha", "branch"):
        assert phrase in text, phrase
    assert "create pr" in text.lower() and "do not merge" in text.lower()


def test_update_epic_prompt_contract():
    text = prompts.load_template("update_epic.md")
    for phrase in (
        "next_issue_url",
        "state OPEN",
        "never another repository",
        "neither the EPIC",
        "{{NEXT_ISSUE_REJECTION}}",
        "do not repeat",
    ):
        assert phrase in text, phrase


def test_engine_prompt_carries_last_next_issue_rejection(engine):
    engine.state.phase = Phase.UPDATE_EPIC
    assert "rejected by the controller: (none)" in engine.render_prompt_for(Phase.UPDATE_EPIC)
    engine.state.next_issue_rejections = ["first reason", "next issue X is CLOSED"]
    text = engine.render_prompt_for(Phase.UPDATE_EPIC)
    assert "rejected by the controller: next issue X is CLOSED" in text
    assert "first reason" not in text


def test_correction_prompt_exact_text():
    text = prompts.load_template("correction.md")
    assert "Your previous execution did not return a valid CONTROL_RESULT." in text
    assert "Do not blindly repeat GitHub or repository operations" in text
    assert "{{PREVIOUS_ERROR}}" in text


def test_engine_prompt_variables_review_and_fix(engine):
    from tests.conftest import PR, SHA_A, SHA_B

    engine.state.phase = Phase.REVIEW
    engine.state.current_pr_url = PR
    engine.state.current_head_sha = SHA_B
    engine.state.reviewed_head_sha = SHA_A
    engine.state.review_round = 1
    text = engine.render_prompt_for(Phase.REVIEW)
    assert "Round 2" in text and SHA_B in text  # reviews the *current* HEAD
    engine.state.phase = Phase.FIX
    engine.state.open_findings = [
        {"id": "R1-F1", "classification": "nit", "required_resolution": "do x"}
    ]
    text = engine.render_prompt_for(Phase.FIX)
    assert "R1-F1" in text and SHA_A in text and "do x" in text
    corr = engine.render_prompt_for(Phase.FIX, correction_error="boom")
    assert "did not return a valid CONTROL_RESULT" in corr and "boom" in corr
