"""``status``'s read-only report of an unjournaled review comment (#416).

REVIEW blocks on a round comment the controller did not journal (ADR 0004
D9.6). ``status`` then reports what GitHub and this checkout's state
directories hold about it: which state directory, if any, journaled it, the
PR's other review markers, the commits committed after it and the PR's
implementation marker. It names no author and writes nothing.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from pathlib import Path

import pytest

import autoforge.review_provenance as provenance
from autoforge import cli
from autoforge.engine import ControllerEngine
from autoforge.errors import GitHubUnavailableError
from autoforge.github import CommentInfo, CommitPerson, PRCommitInfo
from autoforge.review_provenance import (
    MAX_LISTED_COMMITS,
    MAX_LISTED_FINDING_IDS,
    BodyRead,
    journal_evidence,
    offset_text,
    one_line,
    seconds_between,
)
from autoforge.run_namespace import issue_namespace
from autoforge.state import AutoForgeState, load_state, save_state
from autoforge.transitions import Phase
from autoforge.validation import parse_pr_url
from tests.conftest import (
    BRANCH,
    EPIC,
    ISSUE,
    MERGE_BASE,
    PR,
    SHA_A,
    FakeGitHub,
    block,
    comment_url,
    gh_sanitized,
    git_repo,
    implementation_pr_body,
    make_engine,
    review_comment_body,
    review_result,
    state_tree,
)

# ScriptedProvider records every launch as started at this time.
LAUNCHED = "2026-01-01T00:00:00+00:00"
RUN_CREATED = "2025-12-31T22:00:00+00:00"
FINDING = {
    "id": "R1-F1",
    "classification": "nit",
    "title": "typo",
    "location": "src/x.py:1",
    "required_resolution": "fix the typo",
}


def _in_review(tmp_path: Path, name: str, gh: FakeGitHub, script) -> ControllerEngine:
    """A run in ``.autoforge/<name>`` about to review round 1 of the PR at SHA_A."""
    workdir = git_repo(tmp_path)
    eng = make_engine(tmp_path / ".autoforge" / name, script, github=gh, workdir=workdir)
    if PR not in gh.prs:
        gh.add_pr(head_sha=SHA_A, body=implementation_pr_body())
    state = eng.state
    state.phase = Phase.REVIEW
    state.current_pr_url = PR
    state.current_branch = BRANCH
    state.current_head_sha = SHA_A
    state.current_base_ref = "main"
    state.current_merge_base_sha = MERGE_BASE
    state.created_at = RUN_CREATED
    return eng


def _blocked(tmp_path: Path, name: str, gh: FakeGitHub, script=("never",)) -> Path:
    eng = _in_review(tmp_path, name, gh, script if callable(script) else list(script))
    assert eng.step().next_phase == "BLOCKED"
    eng.close()
    return tmp_path / ".autoforge" / name


def _oid(n: int) -> str:
    return f"{n:04x}" + "c" * 36


def _commit(n: int, committed_at: str, headline: str = "", login: str = "alice") -> PRCommitInfo:
    return PRCommitInfo(
        oid=_oid(n),
        authored_at=committed_at,
        committed_at=committed_at,
        author=CommitPerson("Alice", "alice@example.com", login),
        committer=CommitPerson("GitHub", "noreply@github.com", ""),
        headline=headline or f"commit {n}",
    )


@pytest.fixture
def status(monkeypatch, tmp_path):
    """``status`` of a state directory, against the test's FakeGitHub; returns its output."""
    monkeypatch.chdir(tmp_path)
    holder: dict = {}
    real = ControllerEngine.__init__

    def patched(self, *a, **k):
        if "gh" in holder:
            k.setdefault("github", holder["gh"])
        real(self, *a, **k)

    monkeypatch.setattr(ControllerEngine, "__init__", patched)

    def run(gh: FakeGitHub, state_dir: Path, *flags: str) -> str:
        holder["gh"] = gh
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            assert cli.main(["--state-dir", str(state_dir), "status", *flags]) == 0
        return out.getvalue()

    return run


def _report(text: str) -> dict:
    return json.loads(text)["unjournaled_review"]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _planned_then_died(tmp_path: Path, name: str, gh: FakeGitHub, result: dict) -> str:
    """A run in ``.autoforge/<name>`` that saved its K4 plan for round 1 and died before the
    comment landed; returns the planned body. GitHub holds no comment of it."""
    eng = _in_review(tmp_path, name, gh, [block(result)])
    gh.write_failures.append(("create_pr_comment", KeyboardInterrupt(), False))
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    eng.close()
    [record] = load_state(tmp_path / ".autoforge" / name / "state.json").effect_records
    assert record["kind"] == "review_comment" and record["stage"] == "attempted"
    return record["payload"]["body"]


# -- the acceptance criteria -----------------------------------------------------------------


def test_the_report_names_the_state_directory_that_journaled_the_comment(tmp_path, status):
    """Two runs review the same PR. One posts round 1's comment, journaled in its own
    state; the other then blocks on it (D9.6). Its ``status`` names the first run's
    state directory and what it holds, and the tree is the same before and after."""
    gh = FakeGitHub()
    first = _in_review(tmp_path, "first", gh, [block(review_result(1, SHA_A, [FINDING]))])
    assert first.step().next_phase == "FIX"
    first.close()
    [posted] = gh.comments[PR]
    posted.created_at, posted.author = "2026-01-01T00:00:41Z", "operator"
    blocked = _blocked(tmp_path, "second", gh)
    reason = load_state(blocked / "state.json").block_reason
    assert posted.url in reason and "did not post" in reason

    before, writes = state_tree(tmp_path), list(gh.effect_writes)
    gh.calls.clear()
    out = status(gh, blocked)
    assert state_tree(tmp_path) == before

    lines = out.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("Unjournaled review"))
    assert lines[header].startswith(
        f"Unjournaled review comment(s) on PR #42 for round 1 at HEAD {SHA_A[:12]} (ADR 0004 D9.6)"
    )
    assert "the controller cannot tell who posted a comment (ADR 0004 D9.3)" in lines[header]
    assert "Nothing was adopted, deleted or written." in lines[header]
    assert f"  Comment:   {posted.url}" in lines
    assert any(
        line.startswith("    created:   2026-01-01T00:00:41Z, GitHub login operator; +2h00m41s")
        for line in lines
    )
    first_dir = tmp_path / ".autoforge" / "first"
    [journal] = [line for line in lines if line.startswith("    journal:")]
    assert journal.startswith(f"    journal:   {first_dir} (standalone; run ")
    assert "phase FIX" in journal
    assert "last_review_comment_url names this comment (review round 1 completed)" in journal
    assert "review_history records round 1 at HEAD" in journal
    assert f"bound to the PR: {first_dir} (standalone;" in out
    assert "  PR marker: ai-implementation names " + ISSUE in lines
    assert "  Markers:   the PR carries no other ai-review-result marker" in lines

    document = _report(status(gh, blocked, "--json"))
    assert state_tree(tmp_path) == before
    [comment] = document["comments"]
    assert comment["url"] == posted.url and comment["login"] == "operator"
    assert comment["seconds_after_run_created"] == 7241
    [match] = comment["journals"]
    assert match["state_dir"] == str(first_dir) and match["journaled"] is True
    assert match["current"] is False and match["phase"] == "FIX"
    assert [e["kind"] for e in match["evidence"]] == ["last_review_comment_url", "review_history"]
    assert document["state_directories"]["read"] == 2
    assert document["state_directories"]["namespaces_read"] is True
    assert document["implementation_marker"] == {
        "issues": [ISSUE],
        "issues_total": 1,
        "defects": [],
        "defects_total": 0,
    }
    # Only reads: the comments, then (a comment was found) the PR and its commits.
    assert {call[0] for call in gh.calls} <= {
        "get_issue_node",
        "get_pr_comments",
        "get_pr",
        "get_pr_commits",
    }
    assert gh.effect_writes == writes


def test_a_foreign_comment_has_no_journal_match_and_the_later_commits_are_listed(tmp_path, status):
    """A reviewer that still held a credential posts the round's comment while it runs,
    and someone pushes two commits after it. No state accounts for the comment; the
    report sets it against the reviewer's launch and lists the two later commits."""
    gh = FakeGitHub()

    def posts_its_own(req):
        gh.add_comment(
            PR,
            101,
            review_comment_body(1, SHA_A, True, ["R1-F1"]),
            author="operator",
            created_at="2026-01-01T00:00:41Z",
        )
        return block(review_result(1, SHA_A, [FINDING]))

    gh.pr_commits[PR] = [
        _commit(1, "2025-12-31T23:00:00Z"),
        _commit(2, "2026-01-01T00:03:43Z", "Fix R1-F1"),
        _commit(3, "2026-01-01T01:00:41Z"),
    ]
    other = tmp_path / ".autoforge" / "unrelated"
    other.mkdir(parents=True)
    save_state(_state("af-unrelated", Phase.DONE), other / "state.json")
    blocked = _blocked(tmp_path, "mine", gh, script=posts_its_own)

    before = state_tree(tmp_path)
    out = status(gh, blocked)
    assert state_tree(tmp_path) == before
    lines = out.splitlines()
    assert "    launches:  +41s after reviewer launch 001-review-1" in lines
    assert "    journal:   none of the 2 state directories read accounts for it" in lines
    assert not [line for line in lines if line.startswith("    journal:   /")]
    assert (
        "  Commits:   2 of the 3 commit(s) GitHub lists for the PR were committed after the "
        "comment (GitHub records when a commit was made, not when it was pushed)"
    ) in lines
    assert (
        f"    {_oid(2)[:12]} +3m02s: author Alice <alice@example.com>, login alice; "
        "committer GitHub <noreply@github.com>, login (none): 'Fix R1-F1'"
    ) in lines
    assert any(line.startswith(f"    {_oid(3)[:12]} +1h00m00s:") for line in lines)
    assert _oid(1)[:12] not in out

    document = _report(status(gh, blocked, "--json"))
    [comment] = document["comments"]
    assert comment["journals"] == []
    assert comment["seconds_after_launches"] == [{"step": "001-review-1", "seconds": 41}]
    assert document["reviewer_launches"] == [{"step": "001-review-1", "started_at": LAUNCHED}]
    assert document["commits"]["listed"] == 3 and document["commits"]["after_comment"] == 2
    assert [c["seconds_after_comment"] for c in document["commits"]["shown"]] == [182, 3600]
    assert document["commits"]["shown"][0]["author"] == {
        "name": "Alice",
        "email": "alice@example.com",
        "login": "alice",
    }
    assert comment["marker"]["finding_ids"] == ["R1-F1"]
    assert comment["marker"]["finding_ids_total"] == 1
    held = gh.comments[PR][0].body
    assert comment["body"] == {
        "exact": True,
        "read": "listing",
        "sha256": hashlib.sha256(held.encode()).hexdigest(),
        "chars": len(held),
        "problem": "",
    }
    assert ("get_comment_exact", comment["url"]) not in gh.calls  # gh printed it unchanged


# -- what else the report holds ------------------------------------------------------------------


def _state(run_id: str, phase: Phase, **fields) -> AutoForgeState:
    return AutoForgeState(
        run_id=run_id,
        repository="owner/repo",
        epic_url=EPIC,
        current_issue_url=ISSUE,
        phase=phase,
        created_at=RUN_CREATED,
        updated_at=RUN_CREATED,
        **fields,
    )


def test_a_scheduler_namespace_that_journaled_the_comment_is_named(tmp_path, status):
    """The scheduler's per-issue namespaces under the git common dir are read too."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False), created_at="2026-01-01T00:00:00Z")
    blocked = _blocked(tmp_path, "mine", gh)
    namespace = issue_namespace(tmp_path / ".git", 2)
    namespace.state_dir.mkdir(parents=True)
    held = _state(
        "af-worker",
        Phase.READY_FOR_MERGE,
        current_pr_url=PR,
        review_round=1,
        last_review_comment_url=comment_url(PR, 100),
    )
    save_state(held, namespace.state_file)
    document = _report(status(gh, blocked, "--json"))
    [match] = document["comments"][0]["journals"]
    assert match["state_dir"] == str(namespace.state_dir)
    assert match["namespace_issue"] == 2 and match["run_id"] == "af-worker"
    out = status(gh, blocked)
    assert f"    journal:   {namespace.state_dir} (scheduler namespace of issue #2;" in out


def test_unreadable_state_directories_and_other_markers_are_reported(tmp_path, status):
    gh = FakeGitHub()
    gh.add_comment(PR, 90, review_comment_body(1, "b" * 40, True, ["R1-F1"]))
    gh.add_comment(PR, 91, "<!-- ai-review-result: {not json} -->")
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
    blocked = _blocked(tmp_path, "mine", gh)
    broken = tmp_path / ".autoforge" / "broken"
    broken.mkdir()
    (broken / "state.json").write_text("{not json")
    out = status(gh, blocked)
    lines = out.splitlines()
    assert f"  Comment:   {comment_url(PR, 91)}" in lines  # an unreadable marker blocks too
    assert f"  Comment:   {comment_url(PR, 100)}" in lines
    assert any(line.startswith("    defect:    ai-review-result marker payload") for line in lines)
    assert any(line.startswith(f"    unreadable: {broken}: corrupted state file") for line in lines)
    assert "  Markers:   1 other comment(s) of the PR carry an ai-review-result marker" in lines
    assert any(
        line.startswith(f"    {comment_url(PR, 90)} ((unknown)): round 1 at HEAD bbbbbbbbbbbb")
        for line in lines
    )
    document = _report(status(gh, blocked, "--json"))
    assert [c["url"] for c in document["comments"]] == [comment_url(PR, 91), comment_url(PR, 100)]
    assert document["review_markers"]["total"] == 1
    [unreadable] = document["state_directories"]["unreadable"]
    assert unreadable["state_dir"] == str(broken)
    assert "the comment's creation time could not be read" in " ".join(document["problems"])


def test_untrusted_text_is_redacted_put_on_one_line_and_bounded(tmp_path, status):
    """Comment bodies, commit headlines, names and e-mail addresses are anyone's text."""
    gh = FakeGitHub()
    token = "ghp_" + "A" * 36
    body = (
        "\x1b[31mred\x1b[0m "
        + token
        + "\n"
        + "x" * 5000
        + "\n"
        + review_comment_body(1, SHA_A, False)
    )
    gh.add_comment(PR, 100, body, created_at="2026-01-01T00:00:00Z")
    gh.pr_commits[PR] = [
        PRCommitInfo(
            oid="e" * 40,
            authored_at="2026-01-01T00:01:00Z",
            committed_at="2026-01-01T00:01:00Z",
            author=CommitPerson("N\x1b]0;title\x07ame " + token, "a\nb@example.com", "x" * 500),
            committer=CommitPerson(),
            headline="\x1b[2Jcleared " + token + " " + "y" * 1000,
        )
    ]
    blocked = _blocked(tmp_path, "mine", gh)
    for out in (status(gh, blocked), status(gh, blocked, "--json")):
        assert token not in out
        assert "\x1b" not in out and "\x07" not in out
        assert "x" * 300 not in out and "y" * 300 not in out
    document = _report(status(gh, blocked, "--json"))
    excerpt = document["comments"][0]["excerpt"]
    assert excerpt.startswith("[31mred [0m ") and excerpt.endswith(" more characters]")
    [shown] = document["commits"]["shown"]
    assert "\n" not in shown["author"]["email"] and shown["author"]["email"] == "a b@example.com"
    assert len(shown["headline"]) < 200 and len(shown["author"]["login"]) < 160


def test_status_reports_nothing_without_an_unjournaled_round_comment(tmp_path, status):
    """A BLOCKED run whose PR holds no such comment shows no report, and costs one read."""
    gh = FakeGitHub()
    gh.add_comment(PR, 90, review_comment_body(1, SHA_A, True, base_ref="other"))
    eng = _in_review(tmp_path, "mine", gh, ["never"])
    eng.state.phase = Phase.BLOCKED
    eng.state.block_reason = "something else"
    eng.save()
    eng.close()
    gh.calls.clear()
    out = status(gh, tmp_path / ".autoforge" / "mine")
    assert "Unjournaled review" not in out
    assert ("get_pr_comments", PR) in gh.calls and ("get_pr_commits", PR) not in gh.calls
    assert _report(status(gh, tmp_path / ".autoforge" / "mine", "--json")) is None


def test_a_run_that_is_not_blocked_reads_no_comment(tmp_path, status):
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False))
    eng = _in_review(tmp_path, "mine", gh, ["never"])
    eng.save()
    eng.close()
    gh.calls.clear()
    out = status(gh, tmp_path / ".autoforge" / "mine")
    assert "Unjournaled review" not in out
    assert ("get_pr_comments", PR) not in gh.calls


def test_failed_reads_are_reported_and_status_still_succeeds(tmp_path, status):
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False), created_at="2026-01-01T00:00:00Z")
    blocked = _blocked(tmp_path, "mine", gh)
    gh.pr_commits_error = GitHubUnavailableError("gh: connection reset")
    out = status(gh, blocked)
    assert "  Not read:  the PR's commits were not read: gh: connection reset" in out.splitlines()
    gh.comments_error = GitHubUnavailableError("gh: HTTP 502")
    out = status(gh, blocked)
    assert (
        "Unjournaled review comment: not checked (gh: HTTP 502); the PR's comments could not "
        "be read"
    ) in out.splitlines()
    assert _report(status(gh, blocked, "--json")) == {
        "checked": False,
        "problem": "gh: HTTP 502",
    }


def test_at_most_the_bound_number_of_later_commits_is_shown(tmp_path, status):
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False), created_at="2026-01-01T00:00:00Z")
    gh.pr_commits[PR] = [
        _commit(n, f"2026-01-01T{n // 60:02d}:{n % 60:02d}:00Z") for n in range(1, 61)
    ]
    blocked = _blocked(tmp_path, "mine", gh)
    document = _report(status(gh, blocked, "--json"))
    assert document["commits"]["after_comment"] == 60
    assert len(document["commits"]["shown"]) == MAX_LISTED_COMMITS
    assert f"; the first {MAX_LISTED_COMMITS} shown" in status(gh, blocked)


# -- a plan is not a journal (R1-F1) ----------------------------------------------------------


def test_a_foreign_comment_after_the_runs_own_plan_is_reported_with_the_plan(tmp_path, status):
    """The run saved its K4 plan for round 1 and died before the comment landed; someone
    then posted a comment carrying the round's marker with another body, and the plan's
    read-back blocks on it as a conflict. The plan names no comment, so it does not account
    for this one: the comment is reported, and the run's own plan is listed under it as a
    plan."""
    gh = FakeGitHub()
    _planned_then_died(tmp_path, "mine", gh, review_result(1, SHA_A, [FINDING]))
    assert gh.comments.get(PR, []) == []
    foreign = gh.add_comment(
        PR,
        101,
        review_comment_body(1, SHA_A, True, ["R1-F1"]),
        author="operator",
        created_at="2026-01-01T00:00:41Z",
    )
    mine = tmp_path / ".autoforge" / "mine"
    resumed = make_engine(mine, ["the reviewer must not run again"], github=gh, workdir=tmp_path)
    resumed.load()
    assert resumed.step().next_phase == "BLOCKED"
    assert resumed.provider.calls == []
    resumed.close()
    [record] = load_state(mine / "state.json").effect_records
    assert record["stage"] == "conflict" and foreign.url in record["reason"]

    out = status(gh, mine)
    lines = out.splitlines()
    assert f"  Comment:   {foreign.url}" in lines
    assert "    journal:   none of the 1 state directories read accounts for it" in lines
    [recorded] = [line for line in lines if line.startswith("    recorded:")]
    assert recorded.startswith(f"    recorded:  {mine} (the state status reads; run ")
    assert recorded.endswith(
        "a review_comment effect record (K4) for this comment's marker, conflict: a plan for "
        "this comment's round, which names no comment; its payload differs from this "
        "comment's text"
    )
    document = _report(status(gh, mine, "--json"))
    [comment] = document["comments"]
    [match] = comment["journals"]
    assert match["current"] is True and match["journaled"] is False
    assert [(e["kind"], e["payload"]) for e in match["evidence"]] == [("effect_plan", "differs")]


@pytest.mark.parametrize("same_body", [False, True], ids=["another body", "the planned body"])
def test_another_runs_plan_for_the_round_does_not_account_for_a_foreign_comment(
    tmp_path, status, same_body
):
    """Another run on this checkout saved its K4 plan for the same round and its comment
    never landed; a comment carrying the same marker then appears and this run blocks on
    it. The other run's plan is listed as a plan for the round, never as the journal of
    the comment, whether the comment holds the planned body or another."""
    gh = FakeGitHub()
    planned = _planned_then_died(tmp_path, "other", gh, review_result(1, SHA_A, [FINDING]))
    body = planned if same_body else review_comment_body(1, SHA_A, True, ["R1-F1"])
    gh.add_comment(PR, 101, body, author="operator", created_at="2026-01-01T00:00:41Z")
    blocked = _blocked(tmp_path, "mine", gh)
    other = tmp_path / ".autoforge" / "other"

    out = status(gh, blocked)
    lines = out.splitlines()
    assert "    journal:   none of the 2 state directories read accounts for it" in lines
    assert not [line for line in lines if line.startswith("    journal:   /")]
    [recorded] = [line for line in lines if line.startswith("    recorded:")]
    assert recorded.startswith(f"    recorded:  {other} (standalone; run ")
    verdict = (
        "is this comment's text byte for byte" if same_body else "differs from this comment's text"
    )
    assert recorded.endswith(
        "a review_comment effect record (K4) for this comment's marker, attempted: a plan for "
        f"this comment's round, which names no comment; its payload {verdict}"
    )
    document = _report(status(gh, blocked, "--json"))
    [match] = document["comments"][0]["journals"]
    assert match["state_dir"] == str(other) and match["journaled"] is False
    assert [(e["kind"], e["payload"]) for e in match["evidence"]] == [
        ("effect_plan", "same" if same_body else "differs")
    ]


# -- GitHub's text, not gh's print (R1-F2) ------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "on_github", "verdict"),
    [
        # The planned body holds the six characters \u001b, and GitHub holds it byte for
        # byte; gh prints them as \^[, so its print is not the payload.
        ("reset with \\u001b[0m", lambda planned: planned, "same"),
        # The planned body holds the text ^[, and GitHub holds an ESC there, which gh
        # prints as ^[: its print is the payload, and GitHub's text is not.
        ("press ^[ to leave", lambda planned: planned.replace("^[", "\x1b"), "differs"),
    ],
    ids=["literal escape text", "control character"],
)
def test_a_body_gh_rewrites_is_digested_and_compared_on_githubs_text(
    tmp_path, status, title, on_github, verdict
):
    """gh rewrites a control character, and the text of its JSON escape, into caret
    notation, which cannot be undone: the print's digest is not GitHub's, and a payload
    equal to the print is not GitHub's text. The comment is read again exactly; its digest
    and the plan's comparison are of GitHub's text, and only the print is quoted."""
    gh = FakeGitHub()
    finding = {**FINDING, "title": title}
    planned = _planned_then_died(tmp_path, "other", gh, review_result(1, SHA_A, [finding]))
    held = on_github(planned)
    gh.gh_sanitizes = True
    foreign = gh.add_comment(PR, 101, held, created_at="2026-01-01T00:00:41Z")
    blocked = _blocked(tmp_path, "mine", gh)
    printed = gh_sanitized(held)
    assert printed != held and (printed == planned) is (verdict == "differs")

    gh.calls.clear()
    document = _report(status(gh, blocked, "--json"))
    assert ("get_comment_exact", foreign.url) in gh.calls
    [comment] = document["comments"]
    assert comment["body"] == {
        "exact": True,
        "read": "exact_read",
        "sha256": _sha256(held),
        "chars": len(held),
        "problem": "",
    }
    [match] = comment["journals"]
    assert [(e["kind"], e["payload"]) for e in match["evidence"]] == [("effect_plan", verdict)]
    out = status(gh, blocked)
    assert (
        f"    body:      {len(held)} characters, sha256 {_sha256(held)} (GitHub's text, read "
        "exactly); begins (as gh printed it): "
    ) in out
    # Only gh's print is quoted: the exact text is digested and compared, never shown.
    shown = one_line(printed, provenance.MAX_EXCERPT_CHARS)
    assert comment["excerpt"] == shown and "^[" in shown
    for text in (out, json.dumps(document)):
        assert "u001b" not in text and "\x1b" not in text


def test_a_body_not_read_exactly_is_reported_as_gh_printed_it(tmp_path, status, monkeypatch):
    """A failed exact read, or one that finds another version of the comment, leaves
    GitHub's text unknown: the digest is the print's and says so, and status succeeds."""
    gh = FakeGitHub()
    gh.gh_sanitizes = True
    held = "Press \x1b and \\u001b to leave\n" + review_comment_body(1, SHA_A, False)
    gh.add_comment(PR, 100, held, created_at="2026-01-01T00:00:00Z")
    blocked = _blocked(tmp_path, "mine", gh)
    printed = gh_sanitized(held)
    assert printed.startswith("Press ^[ and \\^[ to leave")

    gh.exact_read_error = GitHubUnavailableError("api.github.com: timed out")
    problem = "its exact read failed: api.github.com: timed out"
    document = _report(status(gh, blocked, "--json"))
    assert document["comments"][0]["body"] == {
        "exact": False,
        "read": "listing",
        "sha256": _sha256(printed),
        "chars": len(printed),
        "problem": problem,
    }
    assert (
        f"    body:      {len(printed)} characters, sha256 {_sha256(printed)} of gh's print of "
        f"it, which may not be GitHub's text ({problem}); begins (as gh printed it): "
    ) in status(gh, blocked)

    gh.exact_read_error = None
    edited = CommentInfo(id=100, url=comment_url(PR, 100), body=held + "edited")
    monkeypatch.setattr(gh, "get_comment_exact", lambda url: edited)
    body = _report(status(gh, blocked, "--json"))["comments"][0]["body"]
    assert body["exact"] is False and body["sha256"] == _sha256(printed)
    assert body["problem"] == "it changed between gh's listing and its exact read"


def test_a_payload_is_compared_with_githubs_text_and_only_with_it():
    held = "Press \x1b to leave"
    printed = gh_sanitized(held)
    assert printed == "Press ^[ to leave"
    exact = BodyRead(printed, held, exact_read=True)
    assert exact.compare(held) == "same"
    assert exact.compare(printed) == "differs"  # printed alike, another text
    unknown = BodyRead.of_print(printed, "its exact read failed: x")
    assert not unknown.exact and unknown.sha256 == _sha256(printed)
    assert unknown.compare(held) == "unknown" and unknown.compare(printed) == "unknown"
    assert unknown.compare("Press q to leave") == "differs"  # gh never prints it so
    changed = BodyRead(printed, None, changed=True, problem="changed")
    assert changed.compare("Press q to leave") == "unknown"
    plain = BodyRead.of_print("no caret")
    assert plain.exact and plain.held == "no caret" and plain.compare("no caret") == "same"

    body = review_comment_body(1, SHA_A, False)
    state = _state("af-other", Phase.REVIEW, current_pr_url=PR)
    state.effect_records = [_k4_record("attempted", body, None, body)]
    comment = CommentInfo(id=100, url=comment_url(PR, 100), body=body + "^[")
    [evidence] = journal_evidence(state, parse_pr_url(PR), comment, _claim(body))
    assert evidence.payload == "differs"
    [evidence] = journal_evidence(
        state, parse_pr_url(PR), comment, _claim(body), BodyRead(comment.body, None, changed=True)
    )
    assert evidence.payload == "unknown"
    assert evidence.detail.endswith(
        "whether its payload is this comment's text is not known, since GitHub's text of the "
        "comment is not (see its body line)"
    )


# -- an incomplete search is said (R1-F3) -------------------------------------------------------


def test_a_directory_that_cannot_be_opened_is_reported_as_not_searched(tmp_path, status):
    """A directory listed beside the state directory that cannot be opened may hold the
    journal: it is named as not searched, the report says its search was incomplete, and
    status still succeeds."""
    gh = FakeGitHub()
    gh.add_comment(PR, 100, review_comment_body(1, SHA_A, False), created_at="2026-01-01T00:00:00Z")
    blocked = _blocked(tmp_path, "mine", gh)
    locked = tmp_path / ".autoforge" / "locked"
    locked.mkdir()
    held = _state(
        "af-hidden",
        Phase.FIX,
        current_pr_url=PR,
        review_round=1,
        last_review_comment_url=comment_url(PR, 100),
    )
    save_state(held, locked / "state.json")
    locked.chmod(0)
    try:
        try:
            os.close(os.open(locked, os.O_RDONLY | os.O_DIRECTORY))
        except PermissionError:
            pass
        else:
            pytest.skip("this user opens a directory whatever its mode")
        out = status(gh, blocked)
        document = _report(status(gh, blocked, "--json"))
    finally:
        locked.chmod(0o700)
    lines = out.splitlines()
    [line] = [line for line in lines if line.startswith(f"    not searched: {locked}: ")]
    assert "Permission denied" in line
    assert (
        "    journal:   none of the 1 state directories read accounts for it; the search was "
        "incomplete (see 'unreadable' and 'not searched' below), so a state directory that "
        "does may not have been read"
    ) in lines
    directories = document["state_directories"]
    assert directories["complete"] is False
    [unsearched] = directories["unsearched"]
    assert unsearched["path"] == str(locked) and "Permission denied" in unsearched["problem"]
    assert document["comments"][0]["journals"] == []

    # Readable again, the same directory is the one that accounts for it.
    document = _report(status(gh, blocked, "--json"))
    assert document["state_directories"]["complete"] is True
    assert document["state_directories"]["unsearched"] == []
    [match] = document["comments"][0]["journals"]
    assert match["state_dir"] == str(locked) and match["journaled"] is True


# -- untrusted marker data is bounded (R1-F4) -----------------------------------------------------


def test_a_markers_finding_ids_are_bounded_in_both_output_modes(tmp_path, status):
    """The marker decoder bounds each finding id, never how many one marker names."""
    gh = FakeGitHub()
    ids = [f"R1-F{n}" for n in range(1, 1001)]
    gh.add_comment(PR, 90, review_comment_body(1, "b" * 40, True, ids))
    gh.add_comment(
        PR, 100, review_comment_body(1, SHA_A, True, ids), created_at="2026-01-01T00:00:00Z"
    )
    blocked = _blocked(tmp_path, "mine", gh)
    hidden = f"R1-F{MAX_LISTED_FINDING_IDS + 1}"

    document = _report(status(gh, blocked, "--json"))
    [comment] = document["comments"]
    [other] = document["review_markers"]["shown"]
    for marker in (comment["marker"], other["marker"]):
        assert marker["finding_ids"] == ids[:MAX_LISTED_FINDING_IDS]
        assert marker["finding_ids_total"] == 1000
    assert hidden not in json.dumps(document)

    out = status(gh, blocked)
    shown = (
        ", ".join(ids[:MAX_LISTED_FINDING_IDS]) + f" (the first {MAX_LISTED_FINDING_IDS} of 1000)"
    )
    assert out.count(f"findings {shown}") == 2
    assert hidden not in out


def test_marker_defects_and_implementation_issues_are_bounded_in_both_output_modes(
    tmp_path, status
):
    """A body may carry any number of broken markers, and a PR body any number of
    implementation markers: the report shows a bounded few of each and says how many."""
    from dataclasses import replace

    from autoforge.engine import render_implementation_marker

    gh = FakeGitHub()
    broken = "".join(f"<!-- ai-review-result: broken-{n} -->\n" for n in range(50))
    gh.add_comment(PR, 100, broken, created_at="2026-01-01T00:00:00Z")
    blocked = _blocked(tmp_path, "mine", gh)
    issues = [f"https://github.com/o/r/issues/{n}" for n in range(1, 31)]
    gh.prs[PR] = replace(
        gh.prs[PR], body="".join(render_implementation_marker(i) + "\n" for i in issues)
    )

    document = _report(status(gh, blocked, "--json"))
    [comment] = document["comments"]
    assert comment["defects_total"] == 50
    assert len(comment["defects"]) == provenance.MAX_LISTED_DEFECTS
    marker = document["implementation_marker"]
    assert marker["issues"] == issues[: provenance.MAX_LISTED_ISSUES]
    assert marker["issues_total"] == 30
    assert len(marker["defects"]) == marker["defects_total"] == 1  # it carries 30 markers
    assert issues[provenance.MAX_LISTED_ISSUES] not in json.dumps(document)

    lines = status(gh, blocked).splitlines()
    defects = [line for line in lines if line.startswith("    defect:    ")]
    assert len(defects) == provenance.MAX_LISTED_DEFECTS
    assert f"    defects:   the first {provenance.MAX_LISTED_DEFECTS} of 50 shown" in lines
    [pr_marker] = [line for line in lines if line.startswith("  PR marker: ")]
    assert pr_marker.startswith(
        "  PR marker: ai-implementation names "
        + ", ".join(issues[: provenance.MAX_LISTED_ISSUES])
        + f" (the first {provenance.MAX_LISTED_ISSUES} of 30); "
    )
    assert issues[provenance.MAX_LISTED_ISSUES] not in "\n".join(lines)


# -- the pieces --------------------------------------------------------------------------------


def _claim(body: str):
    from autoforge.claims import REVIEW, scan

    [claim] = scan(REVIEW, body).claims
    return claim


def _k4_record(stage: str, marker_body: str, observed_url: str | None, body: str) -> dict:
    marker = re.search(r"<!-- ai-review-result: .* -->", marker_body)
    assert marker is not None
    return {
        "kind": "review_comment",
        "stage": stage,
        "identity": {"pr_url": PR, "marker": marker.group(0)},
        "observed": {"url": observed_url} if observed_url else None,
        "payload": {"body": body},
    }


def test_an_effect_record_observed_at_the_comment_accounts_for_it():
    """The controller's read-back found exactly this comment carrying its marker with the
    planned body: a record of the comment itself."""
    body = review_comment_body(1, SHA_A, False)
    comment = CommentInfo(id=100, url=comment_url(PR, 100), body=body)
    state = _state("af-other", Phase.REVIEW, current_pr_url=PR)
    state.effect_records = [_k4_record("observed", body, comment_url(PR, 100), body)]
    [evidence] = journal_evidence(state, parse_pr_url(PR), comment, _claim(body))
    assert evidence.kind == "effect_record" and evidence.journals
    assert evidence.payload == "same"
    assert evidence.detail == (
        "a review_comment effect record (K4) for this comment's marker, observed at this "
        "comment: that run read it back as the comment it posted; its payload is this "
        "comment's text byte for byte"
    )


@pytest.mark.parametrize(
    ("stage", "observed", "same_body", "expected"),
    [
        (
            "intended",
            None,
            True,
            "a review_comment effect record (K4) for this comment's marker, intended: a plan for "
            "this comment's round, which names no comment; its payload is this comment's text "
            "byte for byte",
        ),
        (
            "attempted",
            None,
            True,
            "a review_comment effect record (K4) for this comment's marker, attempted: a plan "
            "for this comment's round, which names no comment; its payload is this comment's "
            "text byte for byte",
        ),
        (
            "attempted",
            None,
            False,
            "a review_comment effect record (K4) for this comment's marker, attempted: a plan "
            "for this comment's round, which names no comment; its payload differs from this "
            "comment's text",
        ),
        (
            "conflict",
            None,
            False,
            "a review_comment effect record (K4) for this comment's marker, conflict: a plan for "
            "this comment's round, which names no comment; its payload differs from this "
            "comment's text",
        ),
        (
            "observed",
            comment_url(PR, 99),
            True,
            "a review_comment effect record (K4) for this comment's marker, observed: a plan for "
            f"this comment's round, which names no comment; observed at {comment_url(PR, 99)}; "
            "its payload is this comment's text byte for byte",
        ),
    ],
)
def test_a_plan_for_the_comments_marker_does_not_account_for_it(
    stage, observed, same_body, expected
):
    """R1-F1: a K4 record for the comment's marker that was not observed at it names no
    comment. A foreign comment posted after the plan was saved carries the same marker,
    with the planned body or another, so the plan is listed as what it is and never as
    accounting for the comment, whatever its payload."""
    body = review_comment_body(1, SHA_A, False)
    comment = CommentInfo(id=100, url=comment_url(PR, 100), body=body)
    record = _k4_record(stage, body, observed, body if same_body else body + "x")
    state = _state("af-other", Phase.REVIEW, current_pr_url=PR)
    state.effect_records = [record]
    [evidence] = journal_evidence(state, parse_pr_url(PR), comment, _claim(body))
    assert evidence.kind == "effect_plan" and not evidence.journals
    assert evidence.payload == ("same" if same_body else "differs")
    assert evidence.detail == expected


def test_nothing_else_accounts_for_a_comment():
    body = review_comment_body(1, SHA_A, False)
    comment = CommentInfo(id=100, url=comment_url(PR, 100), body=body)
    other_round = review_comment_body(2, SHA_A, False)
    state = _state(
        "af-other",
        Phase.FIX,
        current_pr_url="https://github.com/owner/repo/pull/43",
        last_review_comment_url=comment_url(PR, 99),
        review_history=[{"round": 1, "reviewed_head_sha": SHA_A, "result": "needs_fix"}],
    )
    state.effect_records = [
        _k4_record("observed", other_round, comment_url(PR, 99), other_round),
        {**_k4_record("attempted", body, None, body), "kind": "follow_up_issue"},
    ]
    assert journal_evidence(state, parse_pr_url(PR), comment, _claim(body)) == ()


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (None, "?"),
        (0, "+0s"),
        (41, "+41s"),
        (182, "+3m02s"),
        (7241, "+2h00m41s"),
        (-90061, "-1d01h01m01s"),
    ],
)
def test_offsets_read_as_signed_durations(seconds, text):
    assert offset_text(seconds) == text


def test_times_are_compared_only_with_a_zone():
    assert seconds_between("2026-01-01T00:00:00Z", "2026-01-01T00:00:41+00:00") == 41
    assert seconds_between("2026-01-01T00:00:00", "2026-01-01T00:00:41Z") is None
    assert seconds_between("", "2026-01-01T00:00:41Z") is None
    assert seconds_between("yesterday", "2026-01-01T00:00:41Z") is None


def test_one_line_redacts_flattens_and_bounds():
    assert one_line("a\n\tb\x1b[0m  c", 100) == "a b [0m c"
    assert one_line("x" * 20, 5) == "xxxxx [... 15 more characters]"
    assert "ghp_" not in one_line("token ghp_" + "B" * 36, 100)


def test_the_module_reads_github_and_never_writes():
    """Structural: the report calls only its four reads, imports no engine, and the engine
    does not import it (nothing that decides reads the report)."""
    tree = ast.parse(Path(provenance.__file__).read_text(encoding="utf-8"))
    on_github = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "github"
    }
    assert on_github == {"get_pr_comments", "get_pr", "get_pr_commits", "get_comment_exact"}
    declared = {name for name in vars(provenance.ProvenanceReads) if not name.startswith("_")}
    assert declared == on_github
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not imported & {"engine", "effect_ops", "git_transport", "executor", "locking"}
    engine_source = Path(provenance.__file__).with_name("engine.py").read_text(encoding="utf-8")
    assert "review_provenance" not in engine_source
    for write in (
        "write_text",
        "write_bytes",
        "create_exclusive",
        "rename",
        "unlink",
        "ensure_dir",
    ):
        assert write not in Path(provenance.__file__).read_text(encoding="utf-8")
