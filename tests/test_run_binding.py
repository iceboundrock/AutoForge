"""The EPIC a new run records: the issue's native parent, else --epic (#293).

Offline: the binding reads through ``FakeGitHub`` (its ``parents``) or a real
``GitHubClient`` whose ``gh`` is a scripted runner serving REST rows of the
shape ``tests/fixtures/github_graph/`` recorded. The regression is the live
incident: issue #257, whose native parent is #255, started with ``--epic
#158`` and recorded #158.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from autoforge.errors import (
    ConfigurationError,
    EpicBindingError,
    ExecutionError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
)
from autoforge.executor import ExecutionResult
from autoforge.github import GitHubClient
from autoforge.run_binding import BINDING_READS, EpicSource, bind_run_epic
from autoforge.validation import parse_issue_url
from tests.conftest import EPIC, ISSUE, ISSUE3, FakeGitHub

_SRC = Path(__file__).resolve().parents[1] / "src" / "autoforge"

# -- the #257 / #158 -> #255 regression, through the real client ---------------------------

_REPO = "iceboundrock/AutoForge"


def _issue_url(number: int) -> str:
    return f"https://github.com/{_REPO}/issues/{number}"


def _row(number: int, *, parent: int | None = None, state: str = "open", **over) -> dict:
    """``GET /repos/<repo>/issues/<n>`` as GitHub answers it (the graph fixtures' shape).

    GitHub leaves ``parent_issue_url`` out of an issue that has no parent
    (``tests/fixtures/github_graph/README.md``), so it is only there with one.
    """
    row: dict = {
        "id": 4_000_000 + number,
        "number": number,
        "html_url": _issue_url(number),
        "repository_url": f"https://api.github.com/repos/{_REPO}",
        "state": state,
        "state_reason": None if state == "open" else "completed",
        "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
        "issue_dependencies_summary": {
            "blocked_by": 0,
            "total_blocked_by": 0,
            "blocking": 0,
            "total_blocking": 0,
        },
        "title": "t",
        "body": "Parent: #158",
    }
    if parent is not None:
        row["parent_issue_url"] = f"https://api.github.com/repos/{_REPO}/issues/{parent}"
    row.update(over)
    return row


def _result(stdout: str = "", exit_code: int = 0, stderr: str = "", **flags) -> ExecutionResult:
    return ExecutionResult(
        command=["gh"],
        cwd=None,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        started_at="t",
        finished_at="t",
        **flags,
    )


def _client(answers: dict[int, ExecutionResult], calls: list[list[str]]) -> GitHubClient:
    """A real client whose ``gh api repos/<repo>/issues/<n>`` answers from ``answers``."""

    def runner(request):
        calls.append(request.command)
        assert request.command[:2] == ["gh", "api"], request.command  # reads only
        number = int(request.command[2].rsplit("/", 1)[1])
        return answers[number]

    return GitHubClient(runner=runner, transient_retries=0, retry_delay_seconds=0)


def _api(number: int) -> list[str]:
    return ["gh", "api", f"repos/{_REPO}/issues/{number}"]


def test_the_257_regression_binds_the_native_parent_and_names_all_three():
    """#257's native parent is #255; --epic #158 (closed) does not override it."""
    calls: list[list[str]] = []
    gh = _client(
        {
            257: _result(json.dumps(_row(257, parent=255))),
            255: _result(json.dumps(_row(255))),
            158: _result(json.dumps(_row(158, state="closed"))),
        },
        calls,
    )
    binding = bind_run_epic(gh, _issue_url(257), _issue_url(158))
    assert binding.epic_url == _issue_url(255)
    assert binding.issue_url == _issue_url(257)
    assert binding.source is EpicSource.NATIVE_PARENT
    assert binding.supplied is not None and binding.supplied.number == 158
    assert binding.native_parent is not None and binding.native_parent.number == 255
    assert binding.supplied_differs
    # The issue and the EPIC it binds to are read; the unused --epic is not.
    assert calls == [_api(257), _api(255)]
    assert binding.lines() == [
        "EPIC binding for issue #257:",
        f"  --epic:        #158 {_issue_url(158)}",
        f"  native parent: #255 {_issue_url(255)}",
        f"  used:          #255 {_issue_url(255)} (the issue's native parent; --epic names "
        "#158, which is not used)",
    ]
    assert binding.record() == {
        "epic_source": "native-parent",
        "epic_url": _issue_url(255),
        "issue_url": _issue_url(257),
        "supplied_epic_url": _issue_url(158),
        "native_parent_url": _issue_url(255),
        "supplied_epic_differs": True,
    }


def test_no_parent_is_only_an_answer_that_carries_the_sub_issue_summaries():
    """Genuine absence vs an API without native sub-issues.

    GitHub omits ``parent_issue_url`` for an issue with no parent; an answer
    with the summaries and no parent falls back to --epic. An answer without
    the summaries is an API that does not know sub-issues: it is refused,
    and never read as "no parent".
    """
    calls: list[list[str]] = []
    parentless = _client(
        {257: _result(json.dumps(_row(257))), 158: _result(json.dumps(_row(158)))}, calls
    )
    binding = bind_run_epic(parentless, _issue_url(257), _issue_url(158))
    assert binding.source is EpicSource.EPIC_OPTION and binding.epic_url == _issue_url(158)
    assert binding.native_parent is None
    assert calls == [_api(257), _api(158)]

    no_summaries = _row(257)
    del no_summaries["sub_issues_summary"]
    calls.clear()
    unsupported = _client({257: _result(json.dumps(no_summaries))}, calls)
    with pytest.raises(GitHubError, match="sub_issues_summary is not an object") as raised:
        bind_run_epic(unsupported, _issue_url(257), _issue_url(158))
    assert not isinstance(raised.value, GitHubUnavailableError)
    assert "never falls back to --epic" in str(raised.value)
    assert calls == [_api(257)]


@pytest.mark.parametrize(
    "answer, expected, match",
    [
        # GitHub refused the token: conclusive, and not "no parent".
        (
            _result(exit_code=1, stderr="gh: Resource not accessible by integration (HTTP 403)"),
            GitHubError,
            "HTTP 403",
        ),
        (_result(exit_code=1, stderr="gh: Bad credentials (HTTP 401)"), GitHubError, "HTTP 401"),
        (_result(exit_code=1, stderr="gh: Not Found (HTTP 404)"), GitHubNotFoundError, "404"),
        # Unavailable: may succeed later, still no fallback.
        (_result(exit_code=1, stderr="HTTP 502: Bad Gateway"), GitHubUnavailableError, "502"),
        (_result(timed_out=True, exit_code=-9), GitHubUnavailableError, "timed out"),
        # Truncated or malformed evidence is never a parentless issue.
        (
            _result(json.dumps(_row(257, parent=255))[:40], stdout_truncated=True),
            GitHubError,
            "truncated",
        ),
        (_result("{not json"), GitHubError, "invalid JSON"),
        (_result(json.dumps([_row(257)])), GitHubError, "non-object"),
        (
            _result(json.dumps(_row(257, parent_issue_url="https://github.com/x/y/issues/1"))),
            GitHubError,
            "parent_issue_url",
        ),
        # A pull request of that number, or a transferred issue's redirect.
        (
            _result(json.dumps(_row(257, html_url=f"https://github.com/{_REPO}/pull/257"))),
            GitHubError,
            "not a GitHub issue URL",
        ),
        (
            _result(
                json.dumps(
                    _row(
                        257,
                        html_url="https://github.com/other/place/issues/257",
                        repository_url="https://api.github.com/repos/other/place",
                    )
                )
            ),
            GitHubError,
            "answered with",
        ),
    ],
)
@pytest.mark.parametrize("which", ["issue", "parent"])
def test_a_read_that_fails_refuses_and_never_falls_back_to_the_epic_option(
    answer, expected, match, which
):
    """Unavailable, 401/403/404, malformed or truncated evidence: no binding, no --epic."""
    calls: list[list[str]] = []
    answers = {
        257: answer if which == "issue" else _result(json.dumps(_row(257, parent=255))),
        255: answer if which == "parent" else _result(json.dumps(_row(255))),
        158: _result(json.dumps(_row(158))),
    }
    if which == "parent" and "answered with" in match:
        answers[255] = _result(
            json.dumps(
                _row(
                    255,
                    html_url="https://github.com/other/place/issues/255",
                    repository_url="https://api.github.com/repos/other/place",
                )
            )
        )
    if which == "parent" and "not a GitHub issue URL" in match:
        answers[255] = _result(
            json.dumps(_row(255, html_url=f"https://github.com/{_REPO}/pull/255"))
        )
    gh = _client(answers, calls)
    with pytest.raises(expected, match=match) as raised:
        bind_run_epic(gh, _issue_url(257), _issue_url(158))
    message = str(raised.value)
    assert message.startswith("cannot bind the run's EPIC: reading ")
    assert "a failed read never falls back to --epic" in message
    if expected is GitHubError:
        assert not isinstance(raised.value, (GitHubUnavailableError, GitHubNotFoundError))
    # #158 is never read: a failed read decides nothing.
    assert _api(158) not in calls
    assert calls == ([_api(257)] if which == "issue" else [_api(257), _api(255)])


# -- the binding rules, through FakeGitHub ----------------------------------------------------

_PARENT = "https://github.com/owner/repo/issues/10"
_NESTED = "https://github.com/owner/repo/issues/11"


def test_a_correctly_parented_issue_binds_its_parent_with_or_without_the_epic_option():
    gh = FakeGitHub()  # ISSUE is a sub-issue of EPIC
    named = bind_run_epic(gh, ISSUE, EPIC)
    assert named.epic_url == EPIC and named.source is EpicSource.NATIVE_PARENT
    assert not named.supplied_differs
    assert named.lines()[-1] == (
        f"  used:          #1 {EPIC} (the issue's native parent, which --epic names too)"
    )
    bare = bind_run_epic(gh, ISSUE, None)
    assert bare.epic_url == EPIC and bare.supplied is None
    assert bare.lines()[1] == "  --epic:        (not given)"
    assert bare.lines()[-1] == f"  used:          #1 {EPIC} (the issue's native parent)"
    assert bare.record()["supplied_epic_url"] is None


def test_an_issue_without_a_native_parent_binds_the_epic_option():
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "orphan", parent=None)
    binding = bind_run_epic(gh, ISSUE3, EPIC)
    assert binding.epic_url == EPIC and binding.source is EpicSource.EPIC_OPTION
    assert binding.native_parent is None
    assert binding.lines()[2:] == [
        "  native parent: none (GitHub names no parent for #3)",
        f"  used:          #1 {EPIC} (--epic, since the issue has no native parent)",
    ]
    assert binding.record()["native_parent_url"] is None
    assert gh.calls == [("get_issue_node", ISSUE3), ("get_issue_node", EPIC)]


def test_without_a_native_parent_or_the_epic_option_the_run_is_refused():
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "orphan", parent=None)
    with pytest.raises(EpicBindingError, match="has no native parent on GitHub and no --epic"):
        bind_run_epic(gh, ISSUE3, None)
    assert gh.calls == [("get_issue_node", ISSUE3)]


def test_a_nested_issue_binds_its_direct_parent_never_an_ancestor():
    gh = FakeGitHub()
    gh.add_issue(_PARENT, "a sub-EPIC")  # itself a sub-issue of EPIC
    gh.add_issue(_NESTED, "nested", parent=_PARENT)
    for supplied in (None, EPIC):
        binding = bind_run_epic(gh, _NESTED, supplied)
        assert binding.epic_url == _PARENT and binding.source is EpicSource.NATIVE_PARENT


def test_a_parent_named_in_the_issue_text_never_counts():
    """The body, a task list on the EPIC and the title are never read."""
    gh = FakeGitHub()
    gh.add_issue(
        ISSUE3,
        "Part of #10",
        body="Parent: https://github.com/owner/repo/issues/10\n- [ ] #10",
        parent=None,
    )
    gh.add_issue(_PARENT, "EPIC", body=f"- [ ] {ISSUE3}", parent=None)
    with pytest.raises(EpicBindingError, match="never read"):
        bind_run_epic(gh, ISSUE3, None)
    assert bind_run_epic(gh, ISSUE3, EPIC).epic_url == EPIC
    # Only the graph read: no issue body, comment or listing was asked for.
    assert {call[0] for call in gh.calls} == {"get_issue_node"}


@pytest.mark.parametrize(
    "issue, epic",
    [
        ("https://github.com/OWNER/Repo/issues/2", EPIC),
        ("https://github.com/owner/repo/issues/2/", "https://github.com/Owner/REPO/issues/1/"),
        (ISSUE, " https://github.com/owner/repo/issues/1 "),
    ],
)
def test_url_aliases_bind_the_same_issues(issue, epic):
    """GitHub compares owner and repository case-insensitively; the binding does too."""
    gh = FakeGitHub()
    binding = bind_run_epic(gh, issue, epic)
    assert binding.source is EpicSource.NATIVE_PARENT and not binding.supplied_differs
    assert binding.epic.same_target(parse_issue_url(EPIC))
    assert binding.issue.same_target(parse_issue_url(ISSUE))
    # The run's URLs keep one repository spelling: the issue's.
    assert binding.epic.repository == binding.issue.repository


def test_a_parent_spelled_in_another_case_is_the_same_repository():
    """GitHub names the parent by its own spelling; it is not another repository."""
    calls: list[list[str]] = []
    upper = f"https://api.github.com/repos/{_REPO.upper()}/issues/255"
    gh = _client(
        {
            257: _result(json.dumps(_row(257, parent_issue_url=upper))),
            255: _result(json.dumps(_row(255))),
        },
        calls,
    )
    binding = bind_run_epic(gh, _issue_url(257), None)
    assert binding.epic_url == _issue_url(255)
    assert binding.native_parent is not None and binding.native_parent.repository == _REPO.upper()


def test_a_native_parent_in_another_repository_is_refused_and_not_overridden():
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "child", parent="https://github.com/other/place/issues/7")
    for supplied in (None, EPIC):
        with pytest.raises(EpicBindingError, match="in another repository"):
            bind_run_epic(gh, ISSUE3, supplied)
    assert ("get_issue_node", "https://github.com/other/place/issues/7") not in gh.calls


def test_a_closed_issue_is_refused_before_its_parent_is_read():
    gh = FakeGitHub()
    gh.add_issue(ISSUE3, "done", state="CLOSED")
    with pytest.raises(EpicBindingError, match="is closed on GitHub; only an open issue"):
        bind_run_epic(gh, ISSUE3, EPIC)
    assert gh.calls == [("get_issue_node", ISSUE3)]


def test_a_closed_native_parent_is_refused_and_never_falls_back_to_the_epic_option():
    gh = FakeGitHub()
    gh.add_issue(_PARENT, "closed EPIC", state="CLOSED", parent=None)
    gh.add_issue(ISSUE3, "child", parent=_PARENT)
    with pytest.raises(EpicBindingError, match="native parent .* is closed") as raised:
        bind_run_epic(gh, ISSUE3, EPIC)
    assert "attach the issue to an open EPIC on GitHub" in str(raised.value)
    assert ("get_issue_node", EPIC) not in gh.calls


def test_a_closed_epic_option_is_refused():
    gh = FakeGitHub()
    gh.add_issue(_PARENT, "closed EPIC", state="CLOSED", parent=None)
    gh.add_issue(ISSUE3, "orphan", parent=None)
    with pytest.raises(EpicBindingError, match="--epic names .* is closed") as raised:
        bind_run_epic(gh, ISSUE3, _PARENT)
    assert "name an open EPIC with --epic" in str(raised.value)


@pytest.mark.parametrize(
    "epic, error, match",
    [
        ("https://github.com/other/place/issues/1", ConfigurationError, "cross-repository"),
        (ISSUE, EpicBindingError, "is the issue itself"),
        ("https://github.com/owner/repo/issues/2/", EpicBindingError, "is the issue itself"),
        ("https://github.com/owner/repo/pull/1", ConfigurationError, "expected a GitHub issue"),
        ("", ConfigurationError, "not a valid HTTPS GitHub URL"),
    ],
)
def test_a_bad_epic_option_is_refused_before_any_read(epic, error, match):
    gh = FakeGitHub()
    with pytest.raises(error, match=match):
        bind_run_epic(gh, ISSUE, epic)
    assert gh.calls == []


def test_a_bad_issue_url_is_refused_before_any_read():
    gh = FakeGitHub()
    with pytest.raises(ConfigurationError, match="expected a GitHub issue"):
        bind_run_epic(gh, "https://github.com/owner/repo/pull/2", EPIC)
    assert gh.calls == []


def test_a_missing_issue_or_parent_is_not_found_and_never_falls_back():
    gh = FakeGitHub()
    with pytest.raises(GitHubNotFoundError, match="reading the issue .*/issues/99 failed"):
        bind_run_epic(gh, "https://github.com/owner/repo/issues/99", EPIC)
    gh.add_issue(ISSUE3, "child", parent=_PARENT)  # #10 does not exist
    with pytest.raises(GitHubNotFoundError, match="reading the issue's native parent .*/10"):
        bind_run_epic(gh, ISSUE3, EPIC)


@pytest.mark.parametrize(
    "error",
    [
        GitHubUnavailableError("HTTP 503"),
        GitHubNotFoundError("HTTP 404"),
        GitHubError("HTTP 403: Resource not accessible"),
        ExecutionError("executable not found: gh"),
    ],
)
def test_a_failed_parent_read_keeps_its_class_and_never_falls_back(error):
    gh = FakeGitHub()
    gh.issue_node_errors[EPIC] = error
    expected = GitHubError if isinstance(error, ExecutionError) else type(error)
    with pytest.raises(expected) as raised:
        bind_run_epic(gh, ISSUE, "https://github.com/owner/repo/issues/3")
    assert type(raised.value) is expected
    assert raised.value.__cause__ is error
    assert str(error) in str(raised.value)
    assert ("get_issue_node", ISSUE3) not in gh.calls


# -- structure ----------------------------------------------------------------------------


def test_the_binding_makes_only_its_declared_read():
    """Like the graph module's GRAPH_READS: no write, no other GitHub read, no file."""
    tree = ast.parse((_SRC / "run_binding.py").read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "reader"
    }
    assert called == set(BINDING_READS) == {"get_issue_node"}
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not imported & {"save_state", "SafeRoot", "ControllerLock", "execute", "subprocess"}
    assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))
