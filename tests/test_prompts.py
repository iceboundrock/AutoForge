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
        # PR #89 F1: an earlier invocation's comment for this round is adopted,
        # never duplicated; the controller enforces one comment per (round, HEAD).
        "{{EXISTING_REVIEW_COMMENT_URL}}",
        "If a comment for this round already exists",
        "do NOT post a second one",
        "exactly one review comment at its HEAD and base",
        # PR #93 review: the round's comment is looked up by (round, HEAD,
        # base); a comment against another base is never this round's.
        "{{REVIEWED_BASE_REF}}",
        '"reviewed_base_ref": {{REVIEWED_BASE_REF_JSON}}',
        "another base branch or no base branch at all is a review of a different",
        # The marker layout holds placeholders that cannot be mistaken for
        # values: a copied `true|false` was invalid JSON that read as a
        # template to fill; `<...>` is the template's own placeholder form.
        '"needs_fix_round": <true or false>',
        "Every `<...>` above is a placeholder to replace",
        # PR #89 F2 (#90): a problem an earlier round deferred to a follow-up
        # issue is not re-raised under a new finding id.
        "{{EXISTING_FOLLOW_UP_ISSUES}}",
        "is not a finding of this round either",
        "Raise it as a finding only when the deferral is wrong",
        # #14 item 2: a stale round's findings are carried to the next round
        # to re-check, never dropped; each is re-raised or accounted for.
        "{{PRIOR_FINDINGS}}",
        "## Prior findings to re-check",
        "Never drop a prior finding silently",
    ):
        assert phrase in text, phrase


def test_review_prompts_state_the_parser_bounds(engine):
    """#34: the reviewer is told the exact bounds the parser rejects against."""
    from autoforge.result_parser import (
        MAX_FINDING_ID_CHARS,
        MAX_FINDING_LOCATION_CHARS,
        MAX_FINDING_RESOLUTION_CHARS,
        MAX_FINDING_TITLE_CHARS,
        MAX_FINDINGS_PER_REVIEW,
    )

    for template in ("review.md", "local_review.md"):
        text = prompts.load_template(template)
        # Template variables, never literal numbers, so the two cannot drift.
        for var in (
            "MAX_FINDINGS_PER_REVIEW",
            "MAX_FINDING_RESOLUTION_CHARS",
            "MAX_FINDING_TITLE_CHARS",
            "MAX_FINDING_LOCATION_CHARS",
            "MAX_FINDING_ID_CHARS",
        ):
            assert "{{" + var + "}}" in text, (template, var)
        assert "never clips findings" in text
    engine.state.phase = Phase.REVIEW
    rendered = engine.render_prompt_for(Phase.REVIEW)
    assert f"at most {MAX_FINDINGS_PER_REVIEW} findings per round" in rendered
    assert f"`required_resolution` at most {MAX_FINDING_RESOLUTION_CHARS} characters" in rendered
    assert f"`title` at most {MAX_FINDING_TITLE_CHARS}" in rendered
    assert f"`location` at most\n  {MAX_FINDING_LOCATION_CHARS}" in rendered
    assert f"`id` at most {MAX_FINDING_ID_CHARS}" in rendered


def test_finding_prompts_state_the_control_character_rule(engine):
    """#78: the reviewer and the fixer are told which fields are one line of
    printable text and which may carry newlines and tabs only."""
    for template in ("review.md", "local_review.md"):
        text = " ".join(prompts.load_template(template).split())
        assert (
            "`title` and `location` are one line of printable text: no newline, tab or "
            "other control character. `required_resolution` may contain newlines and tabs "
            "but no other control character." in text
        ), template
    for template in ("fix.md", "local_fix.md"):
        text = " ".join(prompts.load_template(template).split())
        assert "may contain newlines and tabs but no other control character" in text, template


def test_fix_prompts_state_the_parser_bounds(engine):
    """#77: the fixer is told the exact bounds the parser rejects against."""
    from autoforge.result_parser import (
        MAX_FIX_RATIONALE_CHARS,
        MAX_RESOLUTIONS_PER_FIX,
        MIN_RATIONALE_CHARS,
    )

    for template in ("fix.md", "local_fix.md"):
        text = prompts.load_template(template)
        # Template variables, never literal numbers, so the two cannot drift.
        for var in ("MAX_RESOLUTIONS_PER_FIX", "MAX_FIX_RATIONALE_CHARS", "MIN_RATIONALE_CHARS"):
            assert "{{" + var + "}}" in text, (template, var)
        assert "never clips a resolution" in " ".join(text.split())
    engine.state.phase = Phase.FIX
    rendered = " ".join(engine.render_prompt_for(Phase.FIX).split())
    assert f"at most {MAX_RESOLUTIONS_PER_FIX} resolutions" in rendered
    assert (
        f"`rationale` is between {MIN_RATIONALE_CHARS} and {MAX_FIX_RATIONALE_CHARS} characters"
        in rendered
    )


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
        "{{EXECUTION_ATTEMPT}}",
        "{{HISTORICAL_FINDING_COUNT}}",
        "Do not run `gh pr close`",
        "~~~~untrusted",
        # The replacement is the issue's implementation PR: it carries the
        # same marker ANALYZE_EXECUTE adopts, or no later entry could find it.
        "{{IMPLEMENTATION_MARKER}}",
        "the replacement PR body\ncarries both",
    ):
        assert phrase in text, phrase


def test_replan_prompt_forbids_agent_owned_close_and_local_cleanup():
    """R8-F4: the agent must neither close/mark the source nor touch worktrees."""
    text = prompts.load_template("replan_reexecute.md")
    for phrase in (
        "The controller, not you, owns the previous PR's lifecycle",
        "Do not run `gh pr close` on the previous PR",
        "Do not run `gh pr comment`",
        "Local branches and the other worktrees are operator-owned",
        "Do not run `git worktree add`",
        "Leave the old local branch alone and report it",
    ):
        assert phrase in text, phrase
    for forbidden in (
        "should be marked or closed as superseded",
        "Clean up the previous local branch/worktree only when safe",
    ):
        assert forbidden not in text, forbidden


def test_implementation_prompt_contract():
    text = prompts.load_template("analyze_execute.md")
    for phrase in (
        "AGENTS.md",
        "CLAUDE.md",
        "CONTROL_RESULT",
        "pr_url",
        "head_sha",
        "branch",
        # PR #89 F1: the PR body carries the controller's marker, verbatim; an
        # existing unmarked PR is given it rather than duplicated.
        "{{IMPLEMENTATION_MARKER}}",
        "verbatim",
        "gh pr edit",
        "Never put it in the body of any other PR",
        "a PR without it is rejected",
    ):
        assert phrase in text, phrase
    assert "create pr" in text.lower() and "do not merge" in text.lower()


def test_engine_implementation_prompt_carries_the_issues_marker(engine):
    from autoforge.engine import render_implementation_marker
    from tests.conftest import ISSUE

    engine.state.phase = Phase.ANALYZE_EXECUTE
    text = engine.render_prompt_for(Phase.ANALYZE_EXECUTE)
    marker = render_implementation_marker(ISSUE)
    assert marker == '<!-- ai-implementation: {"issue": "' + ISSUE + '"} -->'
    assert f"PR body marker (required, verbatim): `{marker}`" in text


def test_update_epic_prompt_contract():
    text = prompts.load_template("update_epic.md")
    for phrase in (
        "next_issue_url",
        "state OPEN",
        "never another repository",
        "neither the EPIC",
        "{{NEXT_ISSUE_REJECTION}}",
        "do not repeat",
        # PR #89 F2: the progress comment carries a marker; one already posted
        # is adopted, never duplicated; the controller reads back exactly one.
        "{{PROGRESS_MARKER}}",
        "{{EXISTING_PROGRESS_COMMENT_URL}}",
        "do NOT post a second one",
        "exactly one",
    ):
        assert phrase in text, phrase


def test_fix_prompt_names_the_verified_review_comment_as_authoritative():
    """#80: the linked comment is the controller-verified review for the round
    at the reviewed HEAD, and the fixer may not substitute another PR comment
    or round. The PR URL stays, for context only."""
    text = prompts.load_template("fix.md")
    for phrase in (
        "- Verified review comment: {{REVIEW_COMMENT_URL}}",
        "authoritative review for round {{REVIEW_ROUND}} at HEAD\n`{{REVIEWED_HEAD_SHA}}`",
        "Do not substitute a different PR comment or\nreview round",
        "The PR URL is given for context only",
        "Read the verified review comment ({{REVIEW_COMMENT_URL}})",
    ):
        assert phrase in text, phrase
    assert "- PR: {{PR_URL}}" in text


def test_engine_fix_prompt_renders_the_persisted_review_comment_url(engine):
    from tests.conftest import PR, SHA_A, comment_url

    engine.state.phase = Phase.FIX
    engine.state.current_pr_url = PR
    engine.state.review_round = 2
    engine.state.reviewed_head_sha = SHA_A
    engine.state.last_review_comment_url = comment_url(PR, 101)
    engine.state.open_findings = [
        {"id": "R2-F1", "classification": "nit", "title": "t", "location": "l"}
    ]
    text = engine.render_prompt_for(Phase.FIX)
    assert f"- Verified review comment: {comment_url(PR, 101)}" in text
    assert f"authoritative review for round 2 at HEAD\n`{SHA_A}`" in text
    assert f"Read the verified review comment ({comment_url(PR, 101)})" in text


def test_fix_prompt_hands_over_existing_follow_up_issues():
    """PR #89 F3: a follow-up issue an earlier fixer created is reported, not recreated."""
    text = prompts.load_template("fix.md")
    for phrase in (
        "{{FOLLOW_UP_ISSUES}}",
        "line verbatim in its body",
        "do NOT create a\nsecond one",
        "the one open issue carrying its finding's",
        "no open issue\ncarrying its marker",
        # PR #89 F2 (#90): earlier rounds' deferrals are listed so a re-raised
        # problem is recorded on the existing issue, never in a second one.
        "{{EXISTING_FOLLOW_UP_ISSUES}}",
        "do not open a second issue",
        "two markers is the follow-up of both findings",
    ):
        assert phrase in text, phrase


def test_engine_fix_prompt_lists_a_marker_and_the_existing_issue_per_finding(engine):
    from autoforge.engine import render_follow_up_marker
    from tests.conftest import ISSUE3, PR

    engine.state.phase = Phase.FIX
    engine.state.current_pr_url = PR
    engine.state.open_findings = [
        {"id": "R1-F1", "classification": "nit", "title": "t", "location": "l"},
        {"id": "R1-F2", "classification": "nit", "title": "t", "location": "l"},
    ]
    engine._existing_follow_ups = {"R1-F2": ISSUE3}
    text = engine.render_prompt_for(Phase.FIX)
    m1, m2 = render_follow_up_marker(PR, "R1-F1"), render_follow_up_marker(PR, "R1-F2")
    assert f"- R1-F1: marker `{m1}`; existing issue: (none)" in text
    assert f"- R1-F2: marker `{m2}`; existing issue: {ISSUE3}" in text


def test_engine_update_epic_prompt_carries_the_progress_marker_and_existing_comment(engine):
    from autoforge.engine import render_progress_marker
    from tests.conftest import EPIC, ISSUE, PR, comment_url

    engine.state.phase = Phase.UPDATE_EPIC
    engine.state.current_pr_url = PR
    text = engine.render_prompt_for(Phase.UPDATE_EPIC)
    assert f"`{render_progress_marker(ISSUE, PR)}`" in text
    assert "for this issue (if any):\n  (none)" in text
    engine._existing_progress_comment_url = comment_url(EPIC, 300)
    text = engine.render_prompt_for(Phase.UPDATE_EPIC)
    assert f"for this issue (if any):\n  {comment_url(EPIC, 300)}" in text


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
    assert "{{PREVIOUS_ERROR_BLOCK}}" in text


def test_engine_prompt_variables_review_and_fix(engine):
    from tests.conftest import PR, SHA_A, SHA_B

    engine.state.phase = Phase.REVIEW
    engine.state.current_pr_url = PR
    engine.state.current_head_sha = SHA_B
    engine.state.reviewed_head_sha = SHA_A
    engine.state.review_round = 1
    engine.state.current_base_ref = 'rel"1'
    engine.state.reviewed_base_ref = "main"
    text = engine.render_prompt_for(Phase.REVIEW)
    assert "Round 2" in text and SHA_B in text  # reviews the *current* HEAD
    # ... against the *current* base, given as a JSON literal inside the
    # marker so a quote in the branch name cannot break the marker's JSON.
    assert 'Reviewed base branch (bound by the controller): `rel"1`' in text
    assert '"reviewed_base_ref": "rel\\"1", "needs_fix_round"' in text
    # A comment delimiter in the branch name is escaped the same way (as the
    # JSON escapes `\u003c` / `\u003e`), so the marker line the reviewer
    # copies holds exactly one `-->`: the template's own.
    engine.state.current_base_ref = "x-->y"
    text = engine.render_prompt_for(Phase.REVIEW)
    assert '"reviewed_base_ref": "x--\\u003ey", "needs_fix_round"' in text
    (marker_line,) = [line for line in text.splitlines() if "<!-- ai-review-result:" in line]
    assert marker_line.count("-->") == 1 and marker_line.endswith("-->")
    # Rendered outside a step, no PR was read: no existing comment is named.
    assert (
        "Comment already posted for THIS round at THIS HEAD against THIS base (if\n  any): (none)"
        in text
    )
    engine._existing_review_comment_url = f"{PR}#issuecomment-7"
    text = engine.render_prompt_for(Phase.REVIEW)
    assert f"THIS HEAD against THIS base (if\n  any): {PR}#issuecomment-7" in text
    engine._existing_review_comment_url = ""
    engine.state.phase = Phase.FIX
    engine.state.open_findings = [
        {"id": "R1-F1", "classification": "nit", "required_resolution": "do x"}
    ]
    text = engine.render_prompt_for(Phase.FIX)
    assert "R1-F1" in text and SHA_A in text and "do x" in text
    corr = engine.render_prompt_for(Phase.FIX, correction_error="boom")
    assert "did not return a valid CONTROL_RESULT" in corr and "boom" in corr


def test_common_prompts_state_the_whole_block_bound(engine, tmp_path):
    """#53: every agent is told the size the parser holds the whole
    CONTROL_RESULT block to, through the same constant the parser rejects against."""
    from autoforge.result_parser import MAX_CONTROL_RESULT_CHARS
    from tests.conftest import commit_all, git_repo, make_local_engine, write_feature

    for template in ("common.md", "local_common.md"):
        assert "{{MAX_CONTROL_RESULT_CHARS}}" in prompts.load_template(template), template
    engine.state.phase = Phase.ANALYZE_EXECUTE
    rendered = engine.render_prompt_for(Phase.ANALYZE_EXECUTE)
    assert f"at most\n  {MAX_CONTROL_RESULT_CHARS} characters" in rendered

    root = git_repo(tmp_path)
    write_feature(root)
    commit_all(root, "spec")
    local = make_local_engine(root, "features/add-filter.md")
    rendered = local.render_prompt_for(Phase.ANALYZE_EXECUTE)
    assert f"at most\n  {MAX_CONTROL_RESULT_CHARS} characters" in rendered


def test_common_prompt_states_the_worktree_and_environment_isolation():
    """The agent is told where it runs and what it must never touch (#10)."""
    common = prompts.load_template("common.md")
    for phrase in (
        "git worktree",
        "operator",
        "never run `git worktree add`",
        "allow-listed",
    ):
        assert phrase in common, phrase
    assert "{{AGENT_WORKTREE}}" not in common
    for template in ("analyze_execute.md", "fix.md", "review.md"):
        assert "in this worktree" in prompts.load_template(template), template
