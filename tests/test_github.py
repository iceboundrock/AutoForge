"""GitHub client with injected fake `gh` runner (no network)."""

import json
import os
from dataclasses import replace

import pytest

from autoforge.errors import (
    ConfigurationError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
)
from autoforge.executor import ExecutionResult
from autoforge.github import (
    MAX_BODY_CHARS,
    MAX_TITLE_CHARS,
    STRICT_ISSUE_LIST_LIMIT,
    STRICT_PR_LIST_LIMIT,
    ActionsRunRef,
    ChangedFile,
    GitHubClient,
    WorkflowJob,
    is_access_denied_gh_failure,
    parse_actions_run_url,
)


def _res(payload: object, exit_code: int = 0, stderr: str = "") -> ExecutionResult:
    return ExecutionResult(
        command=["gh"],
        cwd=None,
        exit_code=exit_code,
        stdout=json.dumps(payload),
        stderr=stderr,
        started_at="t",
        finished_at="t",
    )


def _client(handler):
    return GitHubClient(runner=handler)


def test_get_issue():
    gh = _client(
        lambda req: _res(
            {
                "url": "https://github.com/o/r/issues/2",
                "number": 2,
                "title": "Bug",
                "state": "OPEN",
                "body": "details",
            }
        )
    )
    issue = gh.get_issue("https://github.com/o/r/issues/2")
    assert issue.number == 2 and issue.state == "OPEN" and issue.title == "Bug"


def test_get_pr_with_checks():
    gh = _client(
        lambda req: _res(
            {
                "url": "https://github.com/o/r/pull/42",
                "number": 42,
                "title": "Fix",
                "state": "OPEN",
                "headRefOid": "deadbeef",
                "baseRefName": "main",
                "headRefName": "autoforge/2-x",
                "mergeable": "MERGEABLE",
                "statusCheckRollup": [
                    {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
                ],
            }
        )
    )
    pr = gh.get_pr("https://github.com/o/r/pull/42")
    assert pr.head_sha == "deadbeef"
    assert gh.get_pr_head_sha("https://github.com/o/r/pull/42") == "deadbeef"
    assert gh.get_pr_state("https://github.com/o/r/pull/42") == "OPEN"
    assert gh.get_pr_checks("https://github.com/o/r/pull/42")[0].name == "ci"


def test_gh_failure_raises_github_error():
    gh = _client(lambda req: _res({}, exit_code=1, stderr="not found"))
    with pytest.raises(GitHubError, match="failed"):
        gh.get_issue("https://github.com/o/r/issues/999")
    assert gh.issue_exists("https://github.com/o/r/issues/999") is False
    assert gh.pr_exists("https://github.com/o/r/pull/999") is False


def test_get_repo():
    gh = _client(lambda req: _res({"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}}))
    repo = gh.get_repo("o/r")
    assert repo.name_with_owner == "o/r" and repo.default_branch == "main"


def test_get_pr_comments_and_get_comment():
    seen = []

    def handler(req):
        seen.append(req.command)
        if "graphql" in req.command:
            return _res(_comment_page([_comment_node("https://github.com/o/r/pull/42", 5)]))
        return _res(
            {
                "id": 5,
                "html_url": "https://github.com/o/r/pull/42#issuecomment-5",
                "body": "# AI Code Review — Round 1",
                "user": {"login": "bot"},
                "created_at": "2026-01-01T00:00:00Z",
            }
        )

    gh = _client(handler)
    comments = gh.get_pr_comments("https://github.com/o/r/pull/42")
    assert comments[0].id == 5 and comments[0].author == "bot"
    c = gh.get_comment("https://github.com/o/r/pull/42#issuecomment-5")
    assert c.id == 5 and "Round 1" in c.body
    assert c.url == "https://github.com/o/r/pull/42#issuecomment-5"
    assert c.parent_url == "https://github.com/o/r/pull/42"
    assert seen[-1][:2] == ["gh", "api"] and "repos/o/r/issues/comments/5" in seen[-1][2]


def _comment_node(parent_url: str, cid: int, body: str = "hello") -> dict:
    """One comment node of a comment page, as GraphQL returns it."""
    return {
        "url": f"{parent_url}#issuecomment-{cid}",
        "body": body,
        "author": {"login": "bot"},
        "createdAt": "2026-01-01T00:00:00Z",
    }


def _comment_page(
    nodes: list, *, parent: str = "pullRequest", has_next: bool = False, cursor: str | None = None
) -> dict:
    """One page of a comment listing's cursor walk, shaped as `gh api graphql` returns it."""
    return {
        "data": {
            "repository": {
                parent: {
                    "comments": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                        "nodes": nodes,
                    }
                }
            }
        }
    }


def _rest_comment(**overrides) -> dict:
    row = {
        "id": 5,
        "html_url": "https://github.com/o/r/pull/42#issuecomment-5",
        "body": "hello",
        "user": {"login": "bot"},
        "created_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def test_get_comment_reports_the_parent_github_says_not_the_one_asked_for():
    """#94: the comments API addresses a comment by id alone, so a URL naming
    the wrong PR is answered with the comment on its real parent, and the
    client hands that identity back rather than echoing the request."""
    gh = _client(
        lambda req: _res(_rest_comment(html_url="https://github.com/o/r/pull/7#issuecomment-5"))
    )
    c = gh.get_comment("https://github.com/o/r/pull/42#issuecomment-5")
    assert c.url == "https://github.com/o/r/pull/7#issuecomment-5"
    assert c.parent_url == "https://github.com/o/r/pull/7"


@pytest.mark.parametrize(
    ("row", "expect"),
    [
        pytest.param(_rest_comment(html_url=None), "not a GitHub comment URL", id="no-html-url"),
        pytest.param(
            _rest_comment(html_url="https://github.com/o/r/pull/42"),
            "not a GitHub comment URL",
            id="parent-url-only",
        ),
        pytest.param(_rest_comment(id=6), "is 6 but the url names #5", id="id-disagrees"),
        pytest.param(_rest_comment(id="5"), "not an integer", id="id-not-int"),
        pytest.param(_rest_comment(body=["x"]), "expected a string", id="body-not-text"),
        pytest.param([], "non-object", id="not-an-object"),
    ],
)
def test_get_comment_refuses_a_row_whose_identity_cannot_be_read(row, expect):
    """The merge gate re-reads the round's review comment through this read
    (#94); a row without a usable URL, or whose id disagrees with it, is a
    conclusive GitHubError, never a comment with a borrowed identity."""
    gh = _client(lambda req: _res(row))
    with pytest.raises(GitHubError, match=expect):
        gh.get_comment("https://github.com/o/r/pull/42#issuecomment-5")


def test_get_comment_of_a_deleted_comment_is_not_found():
    stderr = "HTTP 404: Not Found (https://api.github.com/repos/o/r/issues/comments/5)"
    gh = GitHubClient(
        runner=lambda req: _res({}, exit_code=1, stderr=stderr), retry_delay_seconds=0
    )
    with pytest.raises(GitHubNotFoundError):
        gh.get_comment("https://github.com/o/r/pull/42#issuecomment-5")


def test_get_issue_comments_reads_the_issue_not_a_pr():
    """PR #89 F2: the EPIC's progress comments are read from the issue, never a PR."""
    seen = []

    def handler(req):
        seen.append(req.command)
        node = _comment_node("https://github.com/o/r/issues/1", 7, "progress")
        return _res(_comment_page([node], parent="issue"))

    gh = _client(handler)
    comments = gh.get_issue_comments("https://github.com/o/r/issues/1")
    assert [(c.id, c.body, c.author) for c in comments] == [(7, "progress", "bot")]
    assert comments[0].parent_url == "https://github.com/o/r/issues/1"
    (command,) = seen
    assert command[:3] == ["gh", "api", "graphql"]
    assert "repository(owner: $owner, name: $name) { issue(number: $number) {" in command[4]
    assert command[5:] == ["-f", "owner=o", "-f", "name=r", "-F", "number=1"]
    with pytest.raises(ConfigurationError):
        gh.get_issue_comments("https://github.com/o/r/pull/42")


def test_list_open_issues_strict_refuses_a_possibly_truncated_listing():
    """PR #89 F3: "no follow-up issue exists" needs every open issue, bodies included."""
    one = [
        {
            "url": "https://github.com/o/r/issues/9",
            "number": 9,
            "title": "Follow-up",
            "state": "OPEN",
            "body": "<!-- ai-follow-up: {} -->",
        }
    ]
    seen = []

    def handler(req):
        seen.append(req.command)
        return _res(one)

    gh = _client(handler)
    issues = gh.list_open_issues("o/r")
    assert [(i.number, i.body, i.repository, i.is_open) for i in issues] == [
        (9, "<!-- ai-follow-up: {} -->", "o/r", True)
    ]
    cmd = seen[0]
    assert cmd[:6] == ["gh", "issue", "list", "--repo", "o/r", "--state"]
    assert cmd[cmd.index("--state") + 1] == "open"
    assert cmd[cmd.index("--limit") + 1] == "100"
    assert cmd[cmd.index("--json") + 1] == "url,number,title,state,body"
    assert [i.number for i in gh.list_open_issues("o/r", strict=True)] == [9]
    assert seen[1][seen[1].index("--limit") + 1] == str(STRICT_ISSUE_LIST_LIMIT)

    full = [
        dict(one[0], number=n, url=f"https://github.com/o/r/issues/{n}")
        for n in range(1, STRICT_ISSUE_LIST_LIMIT + 1)
    ]
    truncating = _client(lambda req: _res(full))
    with pytest.raises(GitHubError, match="truncated"):
        truncating.list_open_issues("o/r", strict=True)
    assert len(truncating.list_open_issues("o/r")) == STRICT_ISSUE_LIST_LIMIT


def test_transient_error_retried_once():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return _res({}, exit_code=1, stderr="error connecting to api.github.com: timeout")
        return _res(
            {"url": "https://github.com/o/r/issues/2", "number": 2, "title": "t", "state": "OPEN"}
        )

    gh = GitHubClient(runner=handler, retry_delay_seconds=0)
    assert gh.get_issue("https://github.com/o/r/issues/2").number == 2
    assert len(calls) == 2


@pytest.mark.parametrize(
    "stderr",
    [
        "error connecting to api.github.com: timeout",
        "read: connection reset by peer",
        "HTTP 500: Internal Server Error",  # R4-F1: whole 5xx class, not an enumerated list
        "HTTP 502: Bad Gateway",
        "HTTP 503: Service Unavailable",
        "HTTP 504: Gateway Timeout",
        "HTTP 599: Network Connect Timeout Error",
        "gh: Internal Server Error (HTTP 500)",  # REST (`gh api`) shape
        "gh: Bad Gateway (HTTP 502)",
        "API rate limit exceeded for user",
        "HTTP 429: Too Many Requests",
        "gh: Too Many Requests (HTTP 429)",
        # R5-F1: OS / DNS connectivity failures as Go's net package (behind `gh`) reports them
        'Post "https://api.github.com/graphql": dial tcp 140.82.112.6:443: connect: '
        "network is unreachable",
        'Post "https://api.github.com/graphql": dial tcp: lookup api.github.com: '
        "Temporary failure in name resolution",
        "error connecting to api.github.com\ncheck your internet connection or "
        "https://githubstatus.com\ndial tcp: lookup api.github.com on 127.0.0.53:53: "
        "server misbehaving",
        "dial tcp 140.82.112.6:443: connect: no route to host",
        "dial tcp 140.82.112.6:443: connect: network is down",
        "dial tcp 140.82.112.6:443: i/o timeout",
        "read tcp 10.0.0.2:51234->140.82.112.6:443: read: connection aborted",
        "write tcp 10.0.0.2:51234->140.82.112.6:443: write: broken pipe",
        'Post "https://api.github.com/graphql": unexpected EOF',
        "dial tcp 140.82.112.6:443: connect: host is down",
        "lookup api.github.com: no such host",
    ],
)
def test_transient_failure_raises_github_unavailable_error(stderr):
    """Transient `gh` failures are typed so the engine can bound re-checks instead of guessing."""
    calls = []

    def handler(req):
        calls.append(1)
        return _res({}, exit_code=1, stderr=stderr)

    gh = GitHubClient(runner=handler, retry_delay_seconds=0)
    with pytest.raises(GitHubUnavailableError, match="failed"):
        gh.get_pr("https://github.com/o/r/pull/42")
    assert len(calls) == 2  # retried once, then classified as unavailable


def test_timeout_raises_github_unavailable_error():
    def handler(req):
        res = _res({}, exit_code=-9)
        return replace(res, timed_out=True)

    gh = GitHubClient(runner=handler, retry_delay_seconds=0)
    with pytest.raises(GitHubUnavailableError, match="timed out"):
        gh.get_pr("https://github.com/o/r/pull/42")


@pytest.mark.parametrize(
    "stderr",
    [
        "HTTP 401: Bad credentials (https://api.github.com/graphql)",
        "HTTP 403: Resource not accessible by integration",
        "HTTP 404: Not Found",
        "gh: Not Found (HTTP 404)",
        "could not find pull request",
        # Bare numbers that merely *look* like a status are not a transient status.
        "GraphQL: Could not resolve to a PullRequest with the number of 5021.",
        "HTTP 422: No commit found for SHA: 503f4e1 (https://api.github.com/graphql)",
        # Not connectivity: gh reached GitHub (or never tried) and the answer is final.
        "To get started with GitHub CLI, please run:  gh auth login",
        "x509: certificate signed by unknown authority",
        "GraphQL: Resource protected by organization SAML enforcement.",
    ],
)
def test_conclusive_failure_is_plain_github_error(stderr):
    calls = []

    def handler(req):
        calls.append(1)
        return _res({}, exit_code=1, stderr=stderr)

    gh = GitHubClient(runner=handler, retry_delay_seconds=0)
    with pytest.raises(GitHubError) as info:
        gh.get_pr_merge_queue_status("https://github.com/o/r/pull/42")
    assert not isinstance(info.value, GitHubUnavailableError)
    assert len(calls) == 1  # never retried


_SECRET = "ghp_FakeSecretForGhStderrTest0123456789abcd"


@pytest.mark.parametrize(
    ("stderr", "expected_type"),
    [
        (f"HTTP 401: Bad credentials (Authorization: Bearer {_SECRET})", GitHubError),
        (
            f"HTTP 404: Not Found (https://x-access-token:{_SECRET}@api.github.com/x)",
            GitHubNotFoundError,
        ),
        (f"HTTP 502: Bad Gateway (Authorization: token {_SECRET})", GitHubUnavailableError),
    ],
)
def test_failed_gh_stderr_is_redacted_where_the_error_is_built(stderr, expected_type):
    """#98: `gh` stderr can echo the request's ``Authorization`` header or a
    credentialed URL, and the error text reaches `block_reason`,
    `verification_failures` and the run log. It is redacted once, where the
    error is built; the failure is still classified on the raw text."""
    gh = GitHubClient(
        runner=lambda req: _res({}, exit_code=1, stderr=stderr), retry_delay_seconds=0
    )
    with pytest.raises(expected_type) as info:
        gh.get_issue("https://github.com/o/r/issues/999")
    text = str(info.value)
    assert _SECRET not in text and "***REDACTED***" in text
    assert "failed (exit 1)" in text


@pytest.mark.parametrize(
    "stderr",
    [
        "GraphQL: Could not resolve to an Issue with the number of 999. (repository.issue)",
        "GraphQL: Could not resolve to a PullRequest with the number of 5021.",
        "HTTP 404: Not Found (https://api.github.com/repos/o/r/issues/999)",
        "gh: Not Found (HTTP 404)",
        "could not find pull request",
    ],
)
def test_not_found_is_typed_conclusive_error(stderr):
    """ "No such issue" is conclusive *and* about the object, so it gets its own type."""
    gh = GitHubClient(
        runner=lambda req: _res({}, exit_code=1, stderr=stderr), retry_delay_seconds=0
    )
    with pytest.raises(GitHubNotFoundError):
        gh.get_issue("https://github.com/o/r/issues/999")


@pytest.mark.parametrize(
    "stderr",
    [
        "HTTP 401: Bad credentials (https://api.github.com/graphql)",
        "HTTP 403: Resource not accessible by integration",
        "To get started with GitHub CLI, please run:  gh auth login",
        "HTTP 422: No commit found for SHA: 404abc1 (https://api.github.com/graphql)",
    ],
)
def test_auth_and_permission_failures_are_not_not_found(stderr):
    gh = GitHubClient(
        runner=lambda req: _res({}, exit_code=1, stderr=stderr), retry_delay_seconds=0
    )
    with pytest.raises(GitHubError) as info:
        gh.get_issue("https://github.com/o/r/issues/999")
    assert not isinstance(info.value, (GitHubNotFoundError, GitHubUnavailableError))


def test_current_repo_uses_repo_view():
    seen = []

    def handler(req):
        seen.append(req.command)
        return _res({"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}})

    assert _client(handler).current_repo().name_with_owner == "o/r"
    assert seen[0][:3] == ["gh", "repo", "view"]


def test_build_merge_argv_and_merge_pr():
    from autoforge.errors import ConfigurationError
    from autoforge.github import build_merge_argv

    url = "https://github.com/o/r/pull/42"
    sha = "a" * 40
    assert build_merge_argv(url, "squash", sha) == [
        "pr",
        "merge",
        url,
        "--squash",
        "--match-head-commit",
        sha,
    ]
    assert build_merge_argv(url, "rebase", sha, delete_branch=True) == [
        "pr",
        "merge",
        url,
        "--rebase",
        "--match-head-commit",
        sha,
        "--delete-branch",
    ]
    with pytest.raises(ConfigurationError, match="merge.method"):
        build_merge_argv(url, "fast-forward", sha)
    # The merge is always bound to a full reviewed HEAD SHA — never unbound.
    for bad in ("", "abc123", "g" * 40):
        with pytest.raises(ConfigurationError, match="match-head-commit"):
            build_merge_argv(url, "squash", bad)

    seen = []

    def runner(req):
        seen.append(req.command)
        return _res({}, exit_code=0)

    gh = _client(runner)
    gh.merge_pr(url, method="merge", match_head_sha=sha)
    assert seen == [["gh", "pr", "merge", url, "--merge", "--match-head-commit", sha]]


def test_merge_pr_failure_raises_github_error():
    gh = _client(lambda req: _res({}, exit_code=1, stderr="Pull request is not mergeable"))
    with pytest.raises(GitHubError, match="not mergeable"):
        gh.merge_pr("https://github.com/o/r/pull/42", match_head_sha="a" * 40)


# -- merge readiness reads (PR #24 review R1-F1 / R1-F2) -----------------------------------
def test_get_pr_parses_merge_state_and_auto_merge():
    seen = []

    def runner(req):
        seen.append(req.command)
        return _res(
            {
                "url": "https://github.com/o/r/pull/42",
                "number": 42,
                "state": "OPEN",
                "headRefOid": "A" * 40,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "clean",
                "autoMergeRequest": {"enabledBy": {"login": "someone"}, "mergeMethod": "SQUASH"},
                "isDraft": False,
            }
        )

    pr = _client(runner).get_pr("https://github.com/o/r/pull/42")
    assert pr.merge_state_status == "CLEAN" and pr.auto_merge_enabled is True
    assert "mergeStateStatus" in seen[0][-1] and "autoMergeRequest" in seen[0][-1]

    pr2 = _client(
        lambda req: _res({"url": "https://github.com/o/r/pull/42", "number": 42, "state": "OPEN"})
    )
    info = pr2.get_pr("https://github.com/o/r/pull/42")
    assert info.merge_state_status == "" and info.mergeable == ""
    assert info.auto_merge_enabled is False  # null / missing -> not armed


@pytest.mark.parametrize(
    "state, conclusion, outcome",
    [
        ("COMPLETED", "SUCCESS", "success"),
        ("COMPLETED", "NEUTRAL", "success"),
        ("COMPLETED", "SKIPPED", "success"),
        ("COMPLETED", "FAILURE", "failure"),
        ("COMPLETED", "CANCELLED", "failure"),
        ("COMPLETED", "TIMED_OUT", "failure"),
        ("COMPLETED", "ACTION_REQUIRED", "failure"),
        ("COMPLETED", "", "unknown"),
        ("IN_PROGRESS", "", "pending"),
        ("QUEUED", "", "pending"),
        ("SUCCESS", "", "success"),  # legacy StatusContext
        ("PENDING", "", "pending"),
        ("EXPECTED", "", "pending"),
        ("FAILURE", "", "failure"),
        ("ERROR", "", "failure"),
        ("", "", "unknown"),
        ("SOMETHING_NEW", "", "unknown"),
    ],
)
def test_check_outcome_classification(state, conclusion, outcome):
    from autoforge.github import CheckInfo

    assert CheckInfo(name="ci", state=state, conclusion=conclusion).outcome == outcome


def test_get_pr_checks_parses_check_runs_and_status_contexts():
    gh = _client(
        lambda req: _res(
            {
                "url": "https://github.com/o/r/pull/42",
                "number": 42,
                "state": "OPEN",
                "statusCheckRollup": [
                    {
                        "__typename": "CheckRun",
                        "name": "ci",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "detailsUrl": "https://github.com/o/r/actions/runs/123/job/456",
                    },
                    {
                        "__typename": "StatusContext",
                        "context": "legacy",
                        "state": "PENDING",
                        "targetUrl": "https://ci.example/build/9",
                    },
                    {"__typename": "CheckRun", "name": "odd", "status": "QUEUED", "detailsUrl": 7},
                ],
            }
        )
    )
    checks = gh.get_pr_checks("https://github.com/o/r/pull/42")
    assert [(c.name, c.outcome) for c in checks] == [
        ("ci", "success"),
        ("legacy", "pending"),
        ("odd", "pending"),
    ]
    assert checks[0].actions_run == ActionsRunRef(repository="o/r", run_id=123)
    assert checks[1].details_url == "https://ci.example/build/9"
    assert checks[1].actions_run is None
    assert checks[2].details_url == "" and checks[2].actions_run is None


def _changed_files_runner(files, total, *, pages=None):
    """Dispatch the REST files listing and the `gh pr view` count separately.

    ``files`` is served as the single page of a `--paginate --slurp` listing
    (``pages`` overrides it with an explicit page list); a non-list ``files``
    is printed raw, the way a broken `gh` would.
    """
    seen = []
    listing = pages if pages is not None else ([files] if isinstance(files, list) else files)

    def handler(req):
        seen.append(req.command)
        if req.command[1] == "api":
            return _res(listing)
        return _res({"changedFiles": total})

    return handler, seen


def test_get_pr_changed_files_reports_paths_and_truncation():
    handler, seen = _changed_files_runner(
        [{"filename": ".github/workflows/ci.yml"}, {"filename": "README.md"}], 2
    )
    changed = _client(handler).get_pr_changed_files("https://github.com/o/r/pull/42")
    assert changed.paths == (".github/workflows/ci.yml", "README.md")
    assert changed.complete is True
    # Every page is read, not the first one (issue #43).
    assert seen[0] == [
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/o/r/pulls/42/files?per_page=100",
    ]
    assert seen[1][:3] == ["gh", "pr", "view"] and "changedFiles" in seen[1]

    # GitHub stops the listing below its own count (past the 3000-file
    # ceiling of the endpoint): the listing proves nothing about the rest.
    handler, _ = _changed_files_runner([{"filename": "README.md"}], 137)
    short = _client(handler).get_pr_changed_files("https://github.com/o/r/pull/42")
    assert short.total == 137 and short.complete is False


def test_get_pr_changed_files_flattens_every_page():
    """A PR with more than one page of files is listed in full (issue #43).

    Before, only the first page was read, so a PR touching more than 100
    files could never pass the protected-path gate whatever it changed.
    """
    first = [{"filename": f"src/f{i}.py"} for i in range(100)]
    second = [{"filename": f"src/g{i}.py"} for i in range(50)]
    handler, _ = _changed_files_runner(None, 150, pages=[first, second])
    changed = _client(handler).get_pr_changed_files("https://github.com/o/r/pull/42")
    assert len(changed.files) == 150 and changed.complete is True
    assert changed.paths[:2] == ("src/f0.py", "src/f1.py")
    assert changed.paths[-1] == "src/g49.py"


def test_get_pr_changed_files_keeps_a_protected_path_from_a_later_page():
    """The page boundary must not hide a file: the second page is evidence too."""
    first = [{"filename": f"src/f{i}.py"} for i in range(100)]
    second = [{"filename": "docs/x.md"}, {"filename": ".github/workflows/ci.yml"}]
    handler, _ = _changed_files_runner(None, 102, pages=[first, second])
    changed = _client(handler).get_pr_changed_files("https://github.com/o/r/pull/42")
    assert changed.complete is True
    assert ".github/workflows/ci.yml" in changed.paths
    # A rename's former path on a later page is kept as well.
    second = [{"filename": "docs/ci.yml", "previous_filename": ".github/workflows/ci.yml"}]
    handler, _ = _changed_files_runner(None, 101, pages=[first, second])
    changed = _client(handler).get_pr_changed_files("https://github.com/o/r/pull/42")
    assert changed.complete is True and ".github/workflows/ci.yml" in changed.paths


def test_get_pr_changed_files_stays_incomplete_when_the_pages_stop_short():
    """Paging moves the fail-closed threshold; it does not remove it."""
    pages = [[{"filename": f"src/f{i}.py"} for i in range(100)] for _ in range(2)]
    handler, _ = _changed_files_runner(None, 3001, pages=pages)
    changed = _client(handler).get_pr_changed_files("https://github.com/o/r/pull/42")
    assert len(changed.files) == 200 and changed.total == 3001
    assert changed.complete is False


@pytest.mark.parametrize(
    "payload",
    [
        [{"filename": "README.md"}],  # one flat page, not a page of pages
        [[{"filename": "README.md"}], {"filename": "b"}],  # a page that is not an array
        "README.md",
    ],
)
def test_get_pr_changed_files_rejects_a_listing_that_is_not_a_page_of_pages(payload):
    handler, _ = _changed_files_runner(None, 1, pages=payload)
    with pytest.raises(GitHubError, match="non-paginated"):
        _client(handler).get_pr_changed_files("https://github.com/o/r/pull/42")


def test_get_pr_changed_files_reports_both_ends_of_a_rename():
    """`gh pr view --json files` shows only the new name; REST shows both."""
    handler, _ = _changed_files_runner(
        [
            {"filename": "docs/ci.yml", "previous_filename": ".github/workflows/ci.yml"},
            {"filename": "README.md"},
        ],
        2,
    )
    changed = _client(handler).get_pr_changed_files("https://github.com/o/r/pull/42")
    assert changed.files[0] == ChangedFile(
        path="docs/ci.yml", previous_path=".github/workflows/ci.yml"
    )
    assert changed.paths == ("docs/ci.yml", ".github/workflows/ci.yml", "README.md")
    # A rename is one *file* and two paths: counting paths would let a
    # truncated listing of renames pass as complete.
    assert changed.complete is True
    handler, _ = _changed_files_runner(
        [{"filename": "b", "previous_filename": "a"}, {"filename": "d", "previous_filename": "c"}],
        3,
    )
    assert _client(handler).get_pr_changed_files("https://github.com/o/r/pull/42").complete is False


@pytest.mark.parametrize(
    ("files", "total", "needle"),
    [
        (None, 1, "non-paginated"),  # not a list
        ({"filename": "README.md"}, 1, "non-paginated"),  # a bare object
        ([{"filename": ""}], 1, "unusable entry"),
        (["README.md"], 1, "unusable entry"),  # not a mapping
        ([{"path": "README.md"}], 1, "unusable entry"),  # GraphQL key, not REST's
        ([{"filename": "b", "previous_filename": ""}], 1, "unusable entry"),
        ([{"filename": "b", "previous_filename": 7}], 1, "unusable entry"),
        ([], "2", "not a count"),
        ([], True, "not a count"),  # bool is not a count
        ([], None, "not a count"),  # changedFiles absent
    ],
)
def test_get_pr_changed_files_fails_closed_on_unusable_data(files, total, needle):
    handler, _ = _changed_files_runner(files, total)
    with pytest.raises(GitHubError, match=needle):
        _client(handler).get_pr_changed_files("https://github.com/o/r/pull/42")


def test_get_pr_changed_files_propagates_gh_failure():
    with pytest.raises(GitHubError, match="failed"):
        _client(lambda req: _res({}, exit_code=1, stderr="boom")).get_pr_changed_files(
            "https://github.com/o/r/pull/42"
        )


def test_get_pr_merge_queue_status_uses_graphql_and_fails_closed():
    seen = []

    def ok(req):
        seen.append(req.command)
        return _res(
            {
                "data": {
                    "repository": {
                        "pullRequest": {"isMergeQueueEnabled": True, "isInMergeQueue": False}
                    }
                }
            }
        )

    q = _client(ok).get_pr_merge_queue_status("https://github.com/o/r/pull/42")
    assert q.enabled is True and q.in_queue is False
    cmd = seen[0]
    assert cmd[:3] == ["gh", "api", "graphql"]
    assert "owner=o" in cmd and "name=r" in cmd and "number=42" in cmd
    assert any("isMergeQueueEnabled" in part and "isInMergeQueue" in part for part in cmd)

    # missing PR node -> GitHubError
    with pytest.raises(GitHubError, match="unavailable"):
        _client(
            lambda req: _res({"data": {"repository": {"pullRequest": None}}})
        ).get_pr_merge_queue_status("https://github.com/o/r/pull/42")
    # non-boolean -> GitHubError (never coerced)
    bad = {
        "data": {
            "repository": {"pullRequest": {"isMergeQueueEnabled": "yes", "isInMergeQueue": None}}
        }
    }
    with pytest.raises(GitHubError, match="not boolean"):
        _client(lambda req: _res(bad)).get_pr_merge_queue_status("https://github.com/o/r/pull/42")
    # gh failure -> GitHubError
    with pytest.raises(GitHubError, match="failed"):
        _client(lambda req: _res({}, exit_code=1, stderr="boom")).get_pr_merge_queue_status(
            "https://github.com/o/r/pull/42"
        )


def test_latest_pr_number_is_a_proven_numeric_maximum_and_fails_closed():
    """R8-F3: the watermark is a numeric max over all states, not one time-ordered node."""
    seen = []

    def ok(req):
        seen.append(req.command)
        # Deliberately out of creation order: the first node carries the
        # *lower* number, as same-second CREATED_AT ties can. A watermark
        # inferred from position would return 41 and let PR 77 slip past.
        return ExecutionResult(
            req.command, None, 0, json.dumps([{"number": 41}, {"number": 77}]), "", "t", "t"
        )

    assert _client(ok).latest_pr_number("o/r") == 77
    cmd = seen[0]
    assert cmd[:3] == ["gh", "pr", "list"]
    assert "--state" in cmd and cmd[cmd.index("--state") + 1] == "all"
    assert cmd[cmd.index("--limit") + 1] == str(STRICT_PR_LIST_LIMIT)
    assert "number" in cmd[cmd.index("--json") + 1]

    # A repository with no pull requests at all has watermark 0.
    assert _client(lambda req: _res([])).latest_pr_number("o/r") == 0

    for payload, needle in [
        ([{"number": "7"}], "not a number"),
        ([{"number": 0}], "not a number"),
        ([{"number": True}], "not a number"),
        (["7"], "not an object"),
    ]:
        with pytest.raises(GitHubError, match=needle):
            _client(lambda req, p=payload: _res(p)).latest_pr_number("o/r")

    with pytest.raises(GitHubError, match="failed"):
        _client(lambda req: _res({}, exit_code=1, stderr="boom")).latest_pr_number("o/r")

    # A listing that reaches the ceiling cannot prove a maximum.
    full = [{"number": n} for n in range(1, STRICT_PR_LIST_LIMIT + 1)]
    truncating = _client(
        lambda req: ExecutionResult(req.command, None, 0, json.dumps(full), "", "t", "t")
    )
    with pytest.raises(GitHubError, match="cannot be established"):
        truncating.latest_pr_number("o/r")


def test_edit_issue_body_argv_and_body_file():
    """The EPIC body write (#13): ``gh issue edit <issue> --body-file <file>``
    with the body in a private temporary file (never in argv), removed once
    ``gh`` returns, whether or not it succeeded."""
    from autoforge.github import build_edit_issue_body_argv

    url = "https://github.com/o/r/issues/1"
    assert build_edit_issue_body_argv(url, "/tmp/x") == [
        "issue",
        "edit",
        url,
        "--body-file",
        "/tmp/x",
    ]
    seen = []

    def runner(req):
        seen.append(req.command)
        path = req.command[-1]
        seen.append(open(path, encoding="utf-8", newline="").read())
        return ExecutionResult(
            command=req.command,
            cwd=None,
            exit_code=0,
            stdout="",
            stderr="",
            started_at="t",
            finished_at="t",
        )

    body = (
        "# EPIC\n\n<!-- ai-controller-roadmap:start -->\n- a\n<!-- ai-controller-roadmap:end -->\n"
    )
    _client(runner).edit_issue_body(url, body)
    assert seen[0][:5] == ["gh", "issue", "edit", url, "--body-file"]
    assert seen[1] == body and body not in " ".join(seen[0])
    assert not os.path.exists(seen[0][-1])

    def failing(req):
        seen.append(req.command[-1])
        return ExecutionResult(
            command=req.command,
            cwd=None,
            exit_code=1,
            stdout="",
            stderr="HTTP 403",
            started_at="t",
            finished_at="t",
        )

    with pytest.raises(GitHubError, match="HTTP 403"):
        _client(failing).edit_issue_body(url, body)
    assert not os.path.exists(seen[-1])


def test_disable_auto_merge_argv():
    from autoforge.github import build_disable_auto_merge_argv

    url = "https://github.com/o/r/pull/42"
    assert build_disable_auto_merge_argv(url) == ["pr", "merge", url, "--disable-auto"]
    seen = []

    def runner(req):
        seen.append(req.command)
        return _res({}, exit_code=0)

    _client(runner).disable_auto_merge(url)
    assert seen == [["gh", "pr", "merge", url, "--disable-auto"]]
    with pytest.raises(GitHubError, match="denied"):
        _client(lambda req: _res({}, exit_code=1, stderr="denied")).disable_auto_merge(url)


def _pr_page(nodes: list, *, has_next: bool = False, cursor: str | None = None) -> dict:
    """One page of the open-PR cursor walk, shaped as `gh api graphql` returns it."""
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": nodes,
                }
            }
        }
    }


def _open_pr_pages(rows: list, page_size: int, page=_pr_page):
    """A runner serving ``rows`` page by page, keyed by the ``after`` cursor it
    is asked for, and recording every command it saw. ``page`` shapes one
    page of the connection walked (the open PRs by default)."""
    seen: list[list[str]] = []
    pages = [rows[i : i + page_size] for i in range(0, len(rows), page_size)] or [[]]

    def handler(req):
        seen.append(req.command)
        cursors = [arg for arg in req.command if arg.startswith("after=")]
        index = int(cursors[0].removeprefix("after=cursor-")) if cursors else 0
        last = index == len(pages) - 1
        cursor = None if last else f"cursor-{index + 1}"
        return _res(page(pages[index], has_next=not last, cursor=cursor))

    return handler, seen


def test_list_open_prs_walks_every_page_to_the_end():
    """Issue #20: the open-PR listing is complete, whatever the repository's
    size. `gh pr list --limit N` stops at N and says nothing about the rest;
    the cursor walk asks for the next page until GitHub reports none."""
    rows = [
        {
            "url": f"https://github.com/o/r/pull/{n}",
            "number": n,
            "title": f"PR {n}",
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "headRefName": f"feature/{n}",
            "baseRefName": "main",
            "isDraft": False,
            "body": f"body {n}",
            "headRepository": {"name": "r"},
            "headRepositoryOwner": {"login": "o"},
            "closingIssuesReferences": {"nodes": [{"number": n + 1000}]},
        }
        for n in range(1, 251)
    ]
    handler, seen = _open_pr_pages(rows, 100)

    prs = _client(handler).list_open_prs("o/r")

    assert [p.number for p in prs] == list(range(1, 251))
    assert prs[249].body == "body 250" and prs[249].head_ref == "feature/250"
    assert prs[0].repository == "o/r" and prs[0].head_repository == "o/r"
    assert prs[0].linked_issue_numbers == [1001]  # the connection is flattened for the decoder
    assert len(seen) == 3
    for command in seen:
        assert command[:3] == ["gh", "api", "graphql"]
        # Raw string fields: `-F` would coerce an all-digit owner or a
        # repository named null/true/false into a non-string GitHub refuses.
        assert command[5:9] == ["-f", "owner=o", "-f", "name=r"]
        assert "-F" not in command
        assert "pullRequests(first: 100, states: [OPEN], after: $after" in command[4]
    assert "after=" not in " ".join(seen[0])
    assert seen[1][-2:] == ["-f", "after=cursor-1"]
    assert seen[2][-2:] == ["-f", "after=cursor-2"]


def test_list_open_prs_of_an_empty_repository_is_one_page():
    handler, seen = _open_pr_pages([], 100)
    assert _client(handler).list_open_prs("o/r") == []
    assert len(seen) == 1


def test_list_open_prs_keeps_a_pr_seen_on_two_pages_once():
    """A PR that moved between pages while the walk ran is one PR, not two
    claimants for the same marker."""
    row = dict(_PR_ROW, number=7, url="https://github.com/o/r/pull/7")
    pages = iter(
        [
            _pr_page([_PR_ROW, row], has_next=True, cursor="c1"),
            _pr_page([row, dict(_PR_ROW, number=9, url="https://github.com/o/r/pull/9")]),
        ]
    )
    prs = _client(lambda req: _res(next(pages))).list_open_prs("o/r")
    assert [p.number for p in prs] == [1, 7, 9]


@pytest.mark.parametrize(
    "page, needle",
    [
        ({}, "page 1: GraphQL response has no data object"),
        ({"data": None}, "page 1: GraphQL response has no data object"),
        ({"data": "yes"}, "page 1: GraphQL response has no data object"),
        ({"data": []}, "page 1: GraphQL response has no data object"),
        ({"data": {}}, "page 1: repository is not readable"),
        ({"data": {"repository": None}}, "page 1: repository is not readable"),
        ({"data": {"repository": "o/r"}}, "page 1: repository is not readable"),
        ({"data": {"repository": {}}}, "page 1 is not a pull-request connection"),
        ({"data": {"repository": {"pullRequests": []}}}, "page 1 is not a pull-request connection"),
        ({"data": {"repository": {"pullRequests": {"nodes": []}}}}, "no usable nodes or pageInfo"),
        (
            {"data": {"repository": {"pullRequests": {"nodes": {}, "pageInfo": {}}}}},
            "no usable nodes or pageInfo",
        ),
        (
            _pr_page([], has_next=True, cursor=None),
            "cannot be read to its end: page 1 announces a next page but no cursor",
        ),
        (
            _pr_page([], has_next=True, cursor=""),
            "cannot be read to its end: page 1 announces a next page but no cursor",
        ),
        (
            {"data": {"repository": {"pullRequests": {"nodes": [], "pageInfo": {"endCursor": 1}}}}},
            "page 1 does not say whether a next page exists",
        ),
    ],
    ids=[
        "no-data",
        "null-data",
        "string-data",
        "list-data",
        "no-repository",
        "null-repository",
        "string-repository",
        "no-connection",
        "list-connection",
        "no-pageinfo",
        "wrong-types",
        "null-cursor",
        "empty-cursor",
        "no-hasnext",
    ],
)
def test_list_open_prs_refuses_a_page_it_cannot_walk_past(page, needle):
    """ "No PR exists" is decided on this listing, so a walk that cannot reach
    its end is an error, never the pages it did read. Every nested container
    is checked before it is read (R1-F3): a truthy value of the wrong type is
    a GitHubError, not an AttributeError."""
    with pytest.raises(GitHubError, match=needle):
        _client(lambda req: _res(page)).list_open_prs("o/r")


def _partial_page(nodes: list, errors: object) -> dict:
    """A GraphQL response that resolved part of the query and reports the rest
    under ``errors``: ``data`` is present and looks complete."""
    return dict(_pr_page(nodes), errors=errors)


@pytest.mark.parametrize(
    "errors",
    [
        [{"message": "Something went wrong while executing your query.", "path": ["repository"]}],
        [],
        None,
    ],
    ids=["resolver-error", "empty-errors", "null-errors"],
)
def test_list_open_prs_refuses_a_page_answered_with_errors(errors):
    """R1-F1: GraphQL answers a partially failed query with ``data`` *and*
    ``errors``, the unresolved part nulled or missing. Such a page is not a
    complete listing, whatever ``gh`` exited with, and a missing PR node
    would prove "no PR exists" and launch a duplicate implementation."""
    with pytest.raises(GitHubError, match="page 1: GraphQL reported errors"):
        _client(lambda req: _res(_partial_page([_PR_ROW], errors))).list_open_prs("o/r")


def test_list_open_prs_refuses_errors_on_a_later_page_too():
    pages = iter(
        [
            _pr_page([_PR_ROW], has_next=True, cursor="c1"),
            _partial_page([], [{"message": "timedout"}]),
        ]
    )
    with pytest.raises(GitHubError, match="page 2: GraphQL reported errors"):
        _client(lambda req: _res(next(pages))).list_open_prs("o/r")


def test_list_open_prs_refuses_a_cursor_that_does_not_advance():
    """A cursor equal to the one just used would walk the same page forever."""
    pages = iter(
        [
            _pr_page([_PR_ROW], has_next=True, cursor="same"),
            _pr_page([_PR_ROW], has_next=True, cursor="same"),
        ]
    )
    with pytest.raises(GitHubError, match="page 2 announces a next page behind a cursor"):
        _client(lambda req: _res(next(pages))).list_open_prs("o/r")


def test_list_open_prs_refuses_a_cursor_cycle():
    """R1-F2: a cycle longer than one page (A -> B -> A) is caught by the set
    of every cursor already used, not only by the previous one; the walk
    stops with an error after a bounded number of pages instead of looping."""
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        cursors = [arg.removeprefix("after=") for arg in req.command if arg.startswith("after=")]
        after = cursors[0] if cursors else None
        cursor = {None: "A", "A": "B", "B": "A"}[after]
        return _res(_pr_page([_PR_ROW], has_next=True, cursor=cursor))

    with pytest.raises(GitHubError, match="page 3 announces a next page behind a cursor .*'A'"):
        _client(handler).list_open_prs("o/r")
    assert calls == 3


def test_list_open_prs_failure_classification_is_the_usual_one():
    """A failed page is a `gh` failure like any other: transient when the
    text says so, conclusive otherwise; never a shorter listing."""
    pages = iter([_pr_page([_PR_ROW], has_next=True, cursor="c1")])

    def flaky(req):
        try:
            return _res(next(pages))
        except StopIteration:
            return _res({}, exit_code=1, stderr="HTTP 502: Bad Gateway")

    with pytest.raises(GitHubUnavailableError, match="502"):
        _client(flaky).list_open_prs("o/r")
    with pytest.raises(GitHubError, match="INVALID_CURSOR_ARGUMENTS"):
        _client(
            lambda req: _res({}, exit_code=1, stderr="INVALID_CURSOR_ARGUMENTS: bad cursor")
        ).list_open_prs("o/r")


def test_list_open_prs_decodes_linked_issues_without_overwriting_the_pr_reference():
    row = {
        "url": "https://github.com/o/r/pull/1",
        "number": 1,
        "state": "OPEN",
        "headRefOid": "a" * 40,
        "headRefName": "feature/x",
        "baseRefName": "main",
        "body": "hello",
        "closingIssuesReferences": {"nodes": [{"number": 9}]},
    }

    [pr] = _client(lambda req: _res(_pr_page([row]))).list_open_prs("o/r")

    assert pr.url == "https://github.com/o/r/pull/1"
    assert pr.repository == "o/r"
    assert pr.linked_issue_numbers == [9]


@pytest.mark.parametrize(
    "references, needle",
    [
        ("9", "closingIssuesReferences is not a connection"),
        ([{"number": 9}], "closingIssuesReferences is not a connection"),
        ({"nodes": "9"}, "closingIssuesReferences.nodes is not a list"),
        ({"nodes": {"number": 9}}, "closingIssuesReferences.nodes is not a list"),
        ({"nodes": [9]}, "a linked issue has no issue number"),
        ({"nodes": [{"number": "9"}]}, "a linked issue has no issue number"),
        ({"nodes": [{"number": True}]}, "a linked issue has no issue number"),
        ({"nodes": [{"number": 0}]}, "a linked issue has no issue number"),
        ({"nodes": [{"number": 9}, {}]}, "a linked issue has no issue number"),
        ({"nodes": [{"number": 9}, None]}, "a linked issue has no issue number"),
    ],
    ids=[
        "string-connection",
        "flattened",
        "string-nodes",
        "object-nodes",
        "bare-number",
        "string-number",
        "bool-number",
        "zero-number",
        "empty-node",
        "null-node",
    ],
)
def test_list_open_prs_refuses_malformed_linked_issues(references, needle):
    """R1-F3: a linked-issue connection that is not the shape the query asked
    for is a GitHubError, not "this PR closes no issue"; that silent reading
    would make the PR of an issue invisible to the issue's recovery."""
    row = dict(_PR_ROW, closingIssuesReferences=references)
    with pytest.raises(GitHubError, match=needle):
        _client(lambda req: _res(_pr_page([row]))).list_open_prs("o/r")


@pytest.mark.parametrize(
    "references",
    [None, {}, {"nodes": None}, {"nodes": []}],
    ids=["null", "empty", "null-nodes", "no-nodes"],
)
def test_list_open_prs_reads_an_absent_linked_issue_connection_as_no_linked_issue(references):
    """Like every other field, ``null``/absent is empty, not malformed."""
    row = dict(_PR_ROW, closingIssuesReferences=references)
    [pr] = _client(lambda req: _res(_pr_page([row]))).list_open_prs("o/r")
    assert pr.linked_issue_numbers == []


def test_list_all_prs_covers_every_state_and_refuses_truncation():
    """R8-F2: the closed-claimant check needs an exhaustive all-states listing."""
    rows = [
        {
            "url": "https://github.com/o/r/pull/1",
            "number": 1,
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "headRefName": "feature/x",
            "baseRefName": "main",
            "body": "open",
        },
        {
            "url": "https://github.com/o/r/pull/2",
            "number": 2,
            "state": "CLOSED",
            "headRefOid": "b" * 40,
            "headRefName": "feature/y",
            "baseRefName": "main",
            "body": "closed",
        },
        {
            "url": "https://github.com/o/r/pull/3",
            "number": 3,
            "state": "MERGED",
            "headRefOid": "c" * 40,
            "headRefName": "feature/z",
            "baseRefName": "main",
            "body": "merged",
        },
    ]
    seen = []

    def handler(req):
        seen.append(req.command)
        return ExecutionResult(req.command, None, 0, json.dumps(rows), "", "t", "t")

    gh = _client(handler)
    prs = gh.list_all_prs("o/r", strict=True)
    assert sorted(p.number for p in prs) == [1, 2, 3]
    assert seen[0][seen[0].index("--state") + 1] == "all"
    assert seen[0][seen[0].index("--limit") + 1] == str(STRICT_PR_LIST_LIMIT)

    full = [
        dict(rows[0], number=n, url=f"https://github.com/o/r/pull/{n}")
        for n in range(1, STRICT_PR_LIST_LIMIT + 1)
    ]
    truncating = _client(
        lambda req: ExecutionResult(req.command, None, 0, json.dumps(full), "", "t", "t")
    )
    with pytest.raises(GitHubError, match="truncated"):
        truncating.list_all_prs("o/r", strict=True)


def test_close_and_reopen_pr_argv():
    """``reopen_pr`` is the undo half of a checkpointed close; no branch is touched."""
    url = "https://github.com/o/r/pull/42"
    seen = []

    def runner(req):
        seen.append(req.command)
        return _res({}, exit_code=0)

    gh = _client(runner)
    gh.close_pr(url, "superseded")
    gh.comment_pr(url, "<!-- autoforge-replan-close: abc -->")
    gh.reopen_pr(url, "undone")
    assert seen == [
        ["gh", "pr", "close", url, "--comment", "superseded"],
        ["gh", "pr", "comment", url, "--body", "<!-- autoforge-replan-close: abc -->"],
        ["gh", "pr", "reopen", url, "--comment", "undone"],
    ]
    with pytest.raises(GitHubError, match="denied"):
        _client(lambda req: _res({}, exit_code=1, stderr="denied")).reopen_pr(url, "undone")
    with pytest.raises(GitHubError, match="denied"):
        _client(lambda req: _res({}, exit_code=1, stderr="denied")).comment_pr(url, "hi")


# -- branch rules (issue #41; read-only, used by `doctor`) ---------------------------------
def _paged(*pages):
    """What `gh api --paginate --slurp` prints: one outer array holding each page."""
    return _res(list(pages))


def test_get_required_status_check_rules_reads_every_page():
    calls = []

    def handler(req):
        calls.append(req.command)
        return _paged(
            [{"type": "deletion", "ruleset_id": 7}],
            [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [
                            {"context": "ci", "integration_id": 15368},
                            {"context": "lint"},
                        ],
                    },
                    "ruleset_source_type": "Organization",
                    "ruleset_source": "o",
                    "ruleset_id": 9,
                }
            ],
        )

    rules = _client(handler).get_required_status_check_rules("o/r", "main")
    assert calls == [["gh", "api", "--paginate", "--slurp", "repos/o/r/rules/branches/main"]]
    assert len(rules) == 1
    rule = rules[0]
    assert rule.contexts == ("ci", "lint") and rule.strict
    assert rule.ruleset_id == 9 and rule.ruleset_source_type == "Organization"
    assert rule.ruleset_source == "o"


def test_get_required_status_check_rules_empty_and_malformed():
    assert _client(lambda req: _paged([])).get_required_status_check_rules("o/r", "main") == []
    # Not the page-of-pages shape `--slurp` produces: malformed, never "empty".
    with pytest.raises(GitHubError, match="non-paginated"):
        _client(lambda req: _res([{"type": "deletion"}])).get_required_status_check_rules(
            "o/r", "main"
        )
    with pytest.raises(GitHubError, match="non-paginated"):
        _client(lambda req: _res({"type": "deletion"})).get_required_status_check_rules(
            "o/r", "main"
        )
    # A rule that names a check without a context is malformed evidence.
    bad = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"integration_id": 1}]},
            "ruleset_id": 1,
        }
    ]
    with pytest.raises(GitHubError, match="names no context"):
        _client(lambda req: _paged(bad)).get_required_status_check_rules("o/r", "main")
    no_id = [{"type": "required_status_checks", "parameters": {"required_status_checks": []}}]
    with pytest.raises(GitHubError, match="non-integer ruleset_id"):
        _client(lambda req: _paged(no_id)).get_required_status_check_rules("o/r", "main")


def test_get_ruleset_and_list_rulesets():
    detail = {
        "id": 22792049,
        "name": "main",
        "target": "branch",
        "source_type": "Repository",
        "source": "o/r",
        "enforcement": "active",
        "bypass_actors": [
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"},
            {"actor_type": "OrganizationAdmin"},
        ],
        "current_user_can_bypass": "pull_requests_only",
        "_links": {"html": {"href": "https://github.com/o/r/rules/22792049"}},
    }
    calls = []

    def handler(req):
        calls.append(req.command)
        if "--slurp" in req.command:
            return _paged([{k: v for k, v in detail.items() if k != "bypass_actors"}])
        return _res(detail)

    gh = _client(handler)
    ruleset = gh.get_ruleset("o/r", 22792049)
    assert calls[-1] == ["gh", "api", "repos/o/r/rulesets/22792049"]
    assert ruleset.id == 22792049 and ruleset.name == "main" and ruleset.is_active
    assert ruleset.bypass_actors == (
        "RepositoryRole 5 (pull_request)",
        "OrganizationAdmin (always)",
    )
    assert ruleset.current_user_can_bypass == "pull_requests_only"
    assert ruleset.html_url == "https://github.com/o/r/rules/22792049"

    # The listing never carries `bypass_actors`: unknown, not none.
    listed = gh.list_rulesets("o/r")
    assert calls[-1] == ["gh", "api", "--paginate", "--slurp", "repos/o/r/rulesets"]
    assert [r.id for r in listed] == [22792049] and listed[0].bypass_actors is None

    with pytest.raises(GitHubError, match="non-integer ruleset id"):
        _client(lambda req: _res({"name": "x", "enforcement": "active"})).get_ruleset("o/r", 1)


def test_get_ruleset_keeps_absent_bypass_actors_apart_from_empty():
    """GitHub omits `bypass_actors` for a token without write access to the ruleset --
    the read still succeeds -- so absence must not be read as "nobody may bypass"."""
    base = {"id": 7, "name": "main", "enforcement": "active", "current_user_can_bypass": "never"}
    withheld = _client(lambda req: _res(base)).get_ruleset("o/r", 7)
    assert withheld.bypass_actors is None
    assert withheld.current_user_can_bypass == "never"

    explicit = _client(lambda req: _res({**base, "bypass_actors": []})).get_ruleset("o/r", 7)
    assert explicit.bypass_actors == ()

    with pytest.raises(GitHubError, match="bypass_actors is not an array"):
        _client(lambda req: _res({**base, "bypass_actors": None})).get_ruleset("o/r", 7)
    with pytest.raises(GitHubError, match="bypass_actors entry is not an object"):
        _client(lambda req: _res({**base, "bypass_actors": ["admin"]})).get_ruleset("o/r", 7)


def test_get_branch_protection():
    calls = []

    def handler(req):
        calls.append(req.command)
        return _res(
            {
                "required_status_checks": {
                    "strict": True,
                    "contexts": ["ci"],
                    "checks": [{"context": "ci", "app_id": 1}, {"context": "lint"}],
                },
                "enforce_admins": {"enabled": True},
            }
        )

    protection = _client(handler).get_branch_protection("o/r", "main")
    assert calls == [["gh", "api", "repos/o/r/branches/main/protection"]]
    assert protection is not None
    assert protection.required_status_checks
    assert protection.required_contexts == ("ci", "lint") and protection.enforce_admins

    # "Branch not protected" is the conclusive negative answer.
    not_protected = _client(
        lambda req: _res({}, exit_code=1, stderr="gh: Branch not protected (HTTP 404)")
    )
    assert not_protected.get_branch_protection("o/r", "main") is None
    # Protection without a status-check requirement: exists, requires nothing.
    bare = _client(lambda req: _res({"enforce_admins": {"enabled": False}}))
    protection = bare.get_branch_protection("o/r", "main")
    assert protection is not None and not protection.required_status_checks
    assert protection.required_contexts == () and not protection.enforce_admins
    # An access failure is not a negative answer: it propagates.
    denied = _client(
        lambda req: _res({}, exit_code=1, stderr="gh: Must have admin rights (HTTP 403)")
    )
    with pytest.raises(GitHubError, match="HTTP 403"):
        denied.get_branch_protection("o/r", "main")


def test_is_access_denied_gh_failure():
    assert is_access_denied_gh_failure("gh: Bad credentials (HTTP 401)")
    assert is_access_denied_gh_failure("gh: Must have admin rights to Repository. (HTTP 403)")
    assert is_access_denied_gh_failure("gh: Resource not accessible by integration (HTTP 403)")
    assert is_access_denied_gh_failure(
        "To get started with GitHub CLI, please run:  gh auth login\n"
        "Alternatively, populate the GH_TOKEN environment variable"
    )
    assert not is_access_denied_gh_failure("gh: Not Found (HTTP 404)")
    assert not is_access_denied_gh_failure("HTTP 502: Bad Gateway")
    assert not is_access_denied_gh_failure("PR #401 could not be found")


# -- #42: GitHub Actions reads behind the check-definition gate ---------------------------
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/o/r/actions/runs/123/job/456", ActionsRunRef("o/r", 123)),
        ("https://github.com/o/r/actions/runs/123", ActionsRunRef("o/r", 123)),
        ("https://github.com/o/r/actions/runs/123/attempts/2", ActionsRunRef("o/r", 123)),
        ("https://github.com/o/r/actions/runs/0", None),
        ("https://github.com/o/r/actions/runs/abc", None),
        ("https://github.com/o/r/actions/workflows/ci.yml", None),
        ("https://ghe.example.com/o/r/actions/runs/123", None),
        ("http://github.com/o/r/actions/runs/123", None),
        ("https://github.com/o/r/pull/42/checks", None),
        ("https://ci.example/build/9", None),
        ("", None),
    ],
)
def test_parse_actions_run_url(url, expected):
    assert parse_actions_run_url(url) == expected


_RUN = {
    "id": 123,
    "name": "ci",
    "path": ".github/workflows/ci.yml",
    "workflow_id": 77,
    "event": "pull_request",
    "head_sha": "ABCDEF" + "0" * 34,
    "head_branch": "feature",
    "status": "completed",
    "conclusion": "success",
    "run_attempt": 2,
    "repository": {"full_name": "o/r"},
    "head_repository": {"full_name": "fork/r"},
}


def test_get_workflow_run_parses_the_rest_shape():
    seen = []

    def handler(req):
        seen.append(req.command)
        return _res(_RUN)

    run = _client(handler).get_workflow_run("o/r", 123)
    assert seen == [["gh", "api", "repos/o/r/actions/runs/123"]]
    assert run.id == 123 and run.workflow_id == 77 and run.run_attempt == 2
    assert run.repository == "o/r" and run.path == ".github/workflows/ci.yml"
    assert run.event == "pull_request" and run.head_branch == "feature"
    assert run.head_sha == "abcdef" + "0" * 34  # normalised like PRInfo.head_sha
    assert run.completed and run.conclusion == "success"


def test_get_workflow_run_in_progress_has_no_conclusion():
    run = _client(lambda req: _res({**_RUN, "status": "in_progress", "conclusion": None}))
    info = run.get_workflow_run("o/r", 123)
    assert not info.completed and info.conclusion == ""


@pytest.mark.parametrize(
    "broken",
    [
        [],
        {**_RUN, "id": "123"},
        {**_RUN, "workflow_id": None},
        {**_RUN, "run_attempt": 0},
        {**_RUN, "repository": {}},
        {**_RUN, "head_sha": ""},
        {**_RUN, "path": ""},
        {**_RUN, "status": None},
    ],
)
def test_get_workflow_run_rejects_unusable_data(broken):
    with pytest.raises(GitHubError):
        _client(lambda req: _res(broken)).get_workflow_run("o/r", 123)


def _job(name, *steps):
    return {
        "name": name,
        "status": "completed",
        "conclusion": "success",
        "steps": [{"name": s, "number": i + 1} for i, s in enumerate(steps)],
    }


def test_get_workflow_run_jobs_reads_every_page_and_keeps_step_order():
    seen = []
    pages = [
        {"total_count": 3, "jobs": [_job("test (3.12)", "Set up job", "Run pytest")]},
        {"total_count": 3, "jobs": [_job("lint", "Set up job", "Run ruff"), _job("ci")]},
    ]

    def handler(req):
        seen.append(req.command)
        return _res(pages)

    jobs = _client(handler).get_workflow_run_jobs("o/r", 123)
    assert seen == [
        ["gh", "api", "--paginate", "--slurp", "repos/o/r/actions/runs/123/jobs?per_page=100"]
    ]
    assert jobs.complete and jobs.total == 3
    assert jobs.jobs == (
        WorkflowJob("test (3.12)", ("Set up job", "Run pytest")),
        WorkflowJob("lint", ("Set up job", "Run ruff")),
        WorkflowJob("ci", ()),
    )
    # The comparison shape is order-independent for jobs, ordered for steps.
    assert jobs.structure() == (
        ("ci", ()),
        ("lint", ("Set up job", "Run ruff")),
        ("test (3.12)", ("Set up job", "Run pytest")),
    )


def test_get_workflow_run_jobs_reports_a_short_listing_as_incomplete():
    page = {"total_count": 5, "jobs": [_job("ci", "Run true")]}
    jobs = _client(lambda req: _res([page])).get_workflow_run_jobs("o/r", 123)
    assert not jobs.complete and len(jobs.jobs) == 1 and jobs.total == 5


@pytest.mark.parametrize(
    "pages",
    [
        {"total_count": 1, "jobs": [_job("ci")]},  # not slurped
        [[_job("ci")]],  # array pages, not object pages
        [{"total_count": "1", "jobs": [_job("ci")]}],
        [{"total_count": 1, "jobs": None}],
        [{"total_count": 1, "jobs": [{"name": "", "steps": []}]}],
        [{"total_count": 1, "jobs": [{"name": "ci", "steps": [{"number": 1}]}]}],
        [{"total_count": 1, "jobs": [{"name": "ci", "steps": [{"name": ""}]}]}],
        [],
    ],
)
def test_get_workflow_run_jobs_rejects_unusable_data(pages):
    with pytest.raises(GitHubError):
        _client(lambda req: _res(pages)).get_workflow_run_jobs("o/r", 123)


def test_get_branch_head_sha():
    seen = []

    def handler(req):
        seen.append(req.command)
        return _res({"name": "main", "commit": {"sha": "ABC" + "0" * 37}})

    assert _client(handler).get_branch_head_sha("o/r", "main") == "abc" + "0" * 37
    assert seen == [["gh", "api", "repos/o/r/branches/main"]]
    with pytest.raises(GitHubError, match="head SHA"):
        _client(lambda req: _res({"name": "main", "commit": {}})).get_branch_head_sha("o/r", "main")


def test_get_merge_base_sha():
    """#96: the merge base is read through the compare endpoint, projected
    with --jq (the full compare answer carries every commit and file of the
    diff), the base percent-encoded so a slash in its name stays one path
    segment, and the answer lower-cased."""
    seen = []

    def handler(req):
        seen.append(req.command)
        return _res({"merge_base_sha": "ABC" + "0" * 37})

    head = "f" * 40
    assert _client(handler).get_merge_base_sha("o/r", "release/1.x", head.upper()) == (
        "abc" + "0" * 37
    )
    assert seen == [
        [
            "gh",
            "api",
            f"repos/o/r/compare/release%2F1.x...{head}",
            "--jq",
            "{merge_base_sha: .merge_base_commit.sha}",
        ]
    ]


@pytest.mark.parametrize("answer", [{}, {"merge_base_sha": None}, {"merge_base_sha": "abc"}])
def test_get_merge_base_sha_refuses_an_unreadable_answer(answer):
    with pytest.raises(GitHubError, match="merge base of 'main' and ffffffffffff of o/r is unread"):
        _client(lambda req: _res(answer)).get_merge_base_sha("o/r", "main", "f" * 40)


def test_get_merge_base_sha_refuses_a_partial_head():
    """A short or malformed head would make GitHub resolve *something*; the
    controller only ever asks about the full SHA it bound."""
    seen = []

    def handler(req):
        seen.append(req.command)
        return _res({"merge_base_sha": "a" * 40})

    with pytest.raises(GitHubError, match="head 'abc' is not a full SHA"):
        _client(handler).get_merge_base_sha("o/r", "main", "abc")
    assert seen == []


def test_get_pr_close_event_count_reads_every_page_of_the_issue_events():
    """#69: the replan close watermark is the number of ``closed`` events on
    the PR's issue timeline, read whole through the paginated events listing
    (a PR is an issue to that endpoint), counting nothing else."""
    seen = []

    def handler(req):
        seen.append(req.command)
        return _paged(
            [
                {"id": 1, "event": "referenced"},
                {"id": 2, "event": "closed"},
                {"id": 3, "event": "reopened"},
            ],
            [
                {"id": 4, "event": "head_ref_deleted"},
                {"id": 5, "event": "closed"},
                {"id": 6, "event": "merged"},
            ],
        )

    count = _client(handler).get_pr_close_event_count("https://github.com/O/R/pull/42")
    assert count == 2
    assert seen == [
        ["gh", "api", "--paginate", "--slurp", "repos/O/R/issues/42/events?per_page=100"]
    ]


def test_get_pr_close_event_count_of_a_pr_never_closed_is_zero():
    url = "https://github.com/o/r/pull/1"
    assert _client(lambda req: _paged([])).get_pr_close_event_count(url) == 0
    handler = lambda req: _paged([{"id": 1, "event": "referenced"}])  # noqa: E731
    assert _client(handler).get_pr_close_event_count(url) == 0


@pytest.mark.parametrize(
    "entry",
    [{"id": 2}, {"id": 2, "event": ""}, {"id": 2, "event": 7}, "closed", None],
)
def test_get_pr_close_event_count_refuses_an_unusable_event(entry):
    """An entry whose kind cannot be read is not silently "not a close": the
    count licenses a destructive write, so a listing that cannot be read whole
    is refused."""
    handler = lambda req: _paged([{"id": 1, "event": "closed"}, entry])  # noqa: E731
    with pytest.raises(GitHubError, match="issue events of https://github.com/o/r/pull/1 contain"):
        _client(handler).get_pr_close_event_count("https://github.com/o/r/pull/1")


def test_get_pr_close_event_count_refuses_a_non_paginated_answer():
    with pytest.raises(GitHubError, match="non-paginated"):
        _client(lambda req: _res([{"id": 1, "event": "closed"}])).get_pr_close_event_count(
            "https://github.com/o/r/pull/1"
        )
    with pytest.raises(GitHubError, match="non-paginated"):
        _client(lambda req: _res({"id": 1, "event": "closed"})).get_pr_close_event_count(
            "https://github.com/o/r/pull/1"
        )


def test_get_pr_close_event_count_keeps_the_transient_conclusive_split():
    gh = GitHubClient(
        runner=lambda req: _res({}, exit_code=1, stderr="error connecting to api.github.com"),
        retry_delay_seconds=0,
    )
    with pytest.raises(GitHubUnavailableError):
        gh.get_pr_close_event_count("https://github.com/o/r/pull/1")
    with pytest.raises(GitHubError, match="failed"):
        _client(lambda req: _res({}, exit_code=1, stderr="HTTP 404")).get_pr_close_event_count(
            "https://github.com/o/r/pull/1"
        )


def test_find_workflow_runs_filters_through_the_query_string_and_reads_every_page():
    seen = []
    first = {**_RUN, "id": 99, "event": "push", "head_branch": "main"}
    second = {**_RUN, "id": 100, "event": "push", "head_branch": "main"}
    pages = [
        {"total_count": 2, "workflow_runs": [first]},
        {"total_count": 2, "workflow_runs": [second]},
    ]

    def handler(req):
        seen.append(req.command)
        return _res(pages)

    runs = _client(handler).find_workflow_runs(
        "o/r", 77, branch="release/1.x", event="push", head_sha="abc"
    )
    assert seen == [
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "repos/o/r/actions/workflows/77/runs?"
            "branch=release%2F1.x&event=push&head_sha=abc&per_page=100",
        ]
    ]
    assert runs.complete and runs.total == 2
    assert [(r.id, r.event, r.head_branch) for r in runs.runs] == [
        (99, "push", "main"),
        (100, "push", "main"),
    ]


def test_find_workflow_runs_reports_a_short_listing_as_incomplete():
    """A listing that stops before GitHub's own count cannot pick a reference (#63 R1-F2)."""
    page = {"total_count": 101, "workflow_runs": [{**_RUN, "id": 99}]}
    runs = _client(lambda req: _res([page])).find_workflow_runs(
        "o/r", 77, branch="main", event="push", head_sha="abc"
    )
    assert not runs.complete and len(runs.runs) == 1 and runs.total == 101


@pytest.mark.parametrize(
    "pages",
    [
        {"total_count": 1, "workflow_runs": [_RUN]},  # not slurped
        [[_RUN]],  # array pages, not object pages
        [{"total_count": "1", "workflow_runs": [_RUN]}],
        [{"total_count": 0}],
        [{"total_count": 1, "workflow_runs": None}],
        [{"total_count": 1, "workflow_runs": [{**_RUN, "id": "99"}]}],
        [],
    ],
)
def test_find_workflow_runs_rejects_unusable_data(pages):
    with pytest.raises(GitHubError):
        _client(lambda req: _res(pages)).find_workflow_runs(
            "o/r", 77, branch="main", event="push", head_sha="abc"
        )


def test_truncated_gh_output_is_an_error_never_parsed():
    """A `gh` reply past the executor's capture bound is head + marker + tail:
    parsing it would read a JSON document that was never returned (#53)."""

    def handler(req):
        return replace(_res({}), stdout_truncated=True)

    gh = GitHubClient(runner=handler, retry_delay_seconds=0)
    with pytest.raises(GitHubError, match="truncated"):
        gh.get_pr("https://github.com/o/r/pull/42")


# -- strict row decoding: a row that is not the object asked for is an error, never
# an object with a manufactured identity (PR #89 re-review, shared claim protocol) ----
_ISSUE_ROW = {
    "url": "https://github.com/o/r/issues/9",
    "number": 9,
    "title": "Follow-up",
    "state": "OPEN",
    "body": "<!-- ai-follow-up: {} -->",
}
_PR_ROW = {
    "url": "https://github.com/o/r/pull/1",
    "number": 1,
    "state": "OPEN",
    "headRefOid": "a" * 40,
    "headRefName": "feature/x",
    "baseRefName": "main",
    "body": "hello",
}


@pytest.mark.parametrize(
    "row, needle",
    [
        ("not an object", "non-object"),
        (dict(_ISSUE_ROW, url=None), "not a GitHub issue URL"),
        ({k: v for k, v in _ISSUE_ROW.items() if k != "url"}, "not a GitHub issue URL"),
        (dict(_ISSUE_ROW, url="https://github.com/o/r/pull/9"), "not a GitHub issue URL"),
        (dict(_ISSUE_ROW, url="https://github.com/o/r/issues/9?x=1"), "not a GitHub issue URL"),
        (dict(_ISSUE_ROW, number=10), "field 'number' is 10 but the url names #9"),
        (dict(_ISSUE_ROW, number="9"), "field 'number' is not an integer"),
        ({k: v for k, v in _ISSUE_ROW.items() if k != "number"}, "not an integer"),
        (dict(_ISSUE_ROW, number=True), "field 'number' is not an integer"),
        (dict(_ISSUE_ROW, body=7), "field 'body' is int, expected a string"),
        (dict(_ISSUE_ROW, state=["OPEN"]), "field 'state' is list, expected a string"),
    ],
    ids=[
        "non-object",
        "null-url",
        "missing-url",
        "pr-url",
        "url-with-query",
        "number-disagrees",
        "string-number",
        "missing-number",
        "bool-number",
        "int-body",
        "list-state",
    ],
)
def test_a_malformed_issue_row_is_a_github_error_in_every_listing_and_view(row, needle):
    """A malformed row would otherwise decode to an issue nothing matches
    (``url=""``, ``number=0``), making a listing "prove" the absence of an
    issue that exists. Both the strict and the plain listing refuse, and so
    does the single-object view."""
    gh = _client(lambda req: _res([_ISSUE_ROW, row]))
    for strict in (False, True):
        with pytest.raises(GitHubError, match=needle) as exc:
            gh.list_open_issues("o/r", strict=strict)
        assert "open issue listing of o/r" in str(exc.value)
    single = _client(lambda req: _res(row))
    with pytest.raises(GitHubError, match=needle):
        single.get_issue("https://github.com/o/r/issues/9")


@pytest.mark.parametrize(
    "row, needle",
    [
        (["url"], "non-object"),
        ({k: v for k, v in _PR_ROW.items() if k != "url"}, "not a GitHub pull request URL"),
        (dict(_PR_ROW, url="https://github.com/o/r/issues/1"), "not a GitHub pull request URL"),
        (dict(_PR_ROW, url="https://github.com/o/r/pull/1/files"), "not a GitHub pull request"),
        (dict(_PR_ROW, number=2), "field 'number' is 2 but the url names #1"),
        (dict(_PR_ROW, number=1.0), "field 'number' is not an integer"),
        (dict(_PR_ROW, headRefOid=["a" * 40]), "field 'headRefOid' is list, expected a string"),
        (dict(_PR_ROW, body={"text": "hello"}), "field 'body' is dict, expected a string"),
        (dict(_PR_ROW, headRefName=5), "field 'headRefName' is int"),
    ],
    ids=[
        "non-object",
        "missing-url",
        "issue-url",
        "url-with-path",
        "number-disagrees",
        "float-number",
        "list-sha",
        "object-body",
        "int-branch",
    ],
)
def test_a_malformed_pr_row_is_a_github_error_in_every_listing_and_view(row, needle):
    with pytest.raises(GitHubError, match=needle):
        _client(lambda req: _res(_pr_page([_PR_ROW, row]))).list_open_prs("o/r")
    gh = _client(lambda req: _res([_PR_ROW, row]))
    for strict in (False, True):
        with pytest.raises(GitHubError, match=needle):
            gh.list_all_prs("o/r", strict=strict)
    single = _client(lambda req: _res(row))
    with pytest.raises(GitHubError, match=needle):
        single.get_pr("https://github.com/o/r/pull/1")


def test_a_view_that_answers_with_another_object_is_refused():
    """``gh`` answering `pull/43` when `pull/42` was asked for (a redirect, a
    moved repository, a stale cache) is not PR 42; the same for an issue."""
    gh = _client(lambda req: _res(dict(_PR_ROW, url="https://github.com/o/r/pull/43", number=43)))
    with pytest.raises(GitHubError, match="answered with .*pull/43"):
        gh.get_pr("https://github.com/o/r/pull/42")
    gh = _client(lambda req: _res(dict(_ISSUE_ROW, url="https://github.com/o/x/issues/9")))
    with pytest.raises(GitHubError, match="answered with .*o/x/issues/9"):
        gh.get_issue("https://github.com/o/r/issues/9")
    # A casing variant of the requested URL is the same object.
    gh = _client(lambda req: _res(dict(_PR_ROW, url="https://github.com/O/R/pull/1")))
    assert gh.get_pr("https://github.com/o/r/pull/1").url == "https://github.com/O/R/pull/1"


def test_truncation_is_judged_on_the_raw_row_count_before_any_row_is_decoded():
    """A listing of ``limit`` rows is "may be truncated" even when one of
    them is malformed: the incomplete-set answer is the one that matters to
    a strict caller, and it must not be masked by a row-level error."""
    prs = [
        dict(_PR_ROW, number=n, url=f"https://github.com/o/r/pull/{n}")
        for n in range(1, STRICT_PR_LIST_LIMIT + 1)
    ]
    prs[-1] = "garbage"
    with pytest.raises(GitHubError, match="truncated"):
        _client(lambda req: _res(prs)).list_all_prs("o/r", strict=True)
    issues = [
        dict(_ISSUE_ROW, number=n, url=f"https://github.com/o/r/issues/{n}")
        for n in range(1, STRICT_ISSUE_LIST_LIMIT + 1)
    ]
    issues[0] = None
    with pytest.raises(GitHubError, match="truncated"):
        _client(lambda req: _res(issues)).list_open_issues("o/r", strict=True)


@pytest.mark.parametrize(
    "row, needle",
    [
        ("text", "non-object entry"),
        ({"id": 5, "body": "x"}, "not a GitHub comment URL"),
        ({"id": 5, "url": "https://github.com/o/r/pull/42", "body": "x"}, "not a GitHub comment"),
        (
            {"id": 5, "url": "https://github.com/o/r/pull/42#issuecomment-5", "body": 5},
            "'body' is int",
        ),
        (
            {"id": 5, "url": "https://github.com/o/r/pull/42#issuecomment-5", "createdAt": 1},
            "'createdAt' is int",
        ),
    ],
    ids=["non-object", "missing-url", "parent-url", "int-body", "int-created"],
)
def test_a_malformed_comment_row_is_a_github_error(row, needle):
    good = {"id": 5, "url": "https://github.com/o/r/pull/42#issuecomment-5", "body": "ok"}
    gh = _client(lambda req: _res(_comment_page([good, row])))
    with pytest.raises(GitHubError, match=needle) as exc:
        gh.get_pr_comments("https://github.com/o/r/pull/42")
    assert "comment on https://github.com/o/r/pull/42" in str(exc.value)
    page = _comment_page([])
    page["data"]["repository"]["pullRequest"]["comments"]["nodes"] = "none"
    gh = _client(lambda req: _res(page))
    with pytest.raises(GitHubError, match="no usable nodes or pageInfo"):
        gh.get_pr_comments("https://github.com/o/r/pull/42")


def test_a_comment_row_identity_is_its_url_and_its_author_may_be_absent():
    row = {
        "id": 99,  # disagrees with the URL: the URL is the identity
        "url": "https://github.com/O/R/pull/42#issuecomment-5",
        "body": "ok",
        "author": None,
    }
    gh = _client(lambda req: _res(_comment_page([row])))
    (c,) = gh.get_pr_comments("https://github.com/o/r/pull/42")
    assert c.id == 5 and c.url == "https://github.com/O/R/pull/42#issuecomment-5"
    assert c.author == "" and c.parent_url == "https://github.com/o/r/pull/42"


_COMMENT_READS = [
    pytest.param("get_pr_comments", "pullRequest", "https://github.com/o/r/pull/42", id="pr"),
    pytest.param("get_issue_comments", "issue", "https://github.com/o/r/issues/1", id="issue"),
]


@pytest.mark.parametrize(("read", "parent", "parent_url"), _COMMENT_READS)
def test_comment_listings_walk_every_page_to_the_end(read, parent, parent_url):
    """#160 R1-F1: `gh pr view` / `gh issue view --json comments` read the first
    100 comments and never a second page, so every identity read built on them
    (K4, K8, the replan close receipt) reported a marker comment past the
    hundredth as absent. The listing walks GitHub's pages by cursor until it
    reports none."""
    rows = [_comment_node(parent_url, n, f"comment {n}") for n in range(1, 251)]
    handler, seen = _open_pr_pages(
        rows, 100, page=lambda nodes, **info: _comment_page(nodes, parent=parent, **info)
    )

    comments = getattr(_client(handler), read)(parent_url)

    assert [c.id for c in comments] == list(range(1, 251))
    assert comments[249].body == "comment 250" and comments[249].parent_url == parent_url
    assert len(seen) == 3
    number = parent_url.rsplit("/", 1)[1]
    for command in seen:
        assert command[:3] == ["gh", "api", "graphql"]
        assert f"{{ {parent}(number: $number) {{" in command[4]
        assert "comments(first: 100, after: $after)" in command[4]
        assert command[5:11] == ["-f", "owner=o", "-f", "name=r", "-F", f"number={number}"]
    assert "after=" not in " ".join(seen[0])
    assert seen[1][-2:] == ["-f", "after=cursor-1"]
    assert seen[2][-2:] == ["-f", "after=cursor-2"]


def test_a_comment_listing_keeps_a_comment_seen_on_two_pages_once():
    """A comment that moved between pages while the walk ran is one comment, not
    two claimants for the same marker."""
    url = "https://github.com/o/r/issues/1"
    pages = iter(
        [
            _comment_page(
                [_comment_node(url, 1), _comment_node(url, 2)],
                parent="issue",
                has_next=True,
                cursor="c1",
            ),
            _comment_page([_comment_node(url, 2), _comment_node(url, 3)], parent="issue"),
        ]
    )
    comments = _client(lambda req: _res(next(pages))).get_issue_comments(url)
    assert [c.id for c in comments] == [1, 2, 3]


def _issue_comments_payload(comments: object) -> dict:
    return {"data": {"repository": {"issue": comments}}}


@pytest.mark.parametrize(
    "pages, needle",
    [
        ([{"data": {"repository": None}}], "page 1: repository is not readable"),
        ([{"data": {"repository": {}}}], "page 1: issue is not readable"),
        ([_issue_comments_payload("x")], "page 1: issue is not readable"),
        ([_issue_comments_payload({})], "page 1 is not a comment connection"),
        ([_issue_comments_payload({"comments": []})], "page 1 is not a comment connection"),
        (
            [_issue_comments_payload({"comments": {"nodes": []}})],
            "page 1 has no usable nodes or pageInfo",
        ),
        (
            [_issue_comments_payload({"comments": {"nodes": [], "pageInfo": {}}})],
            "page 1 does not say whether a next page exists",
        ),
        (
            [_comment_page([], parent="issue", has_next=True, cursor=None)],
            "cannot be read to its end: page 1 announces a next page but no cursor",
        ),
        (
            [dict(_comment_page([], parent="issue"), errors=[{"message": "timedout"}])],
            "page 1: GraphQL reported errors",
        ),
        (
            [
                _comment_page(
                    [_comment_node("https://github.com/o/r/issues/1", 1)],
                    parent="issue",
                    has_next=True,
                    cursor="c1",
                ),
                _comment_page([], parent="issue", has_next=True, cursor="c1"),
            ],
            "page 2 announces a next page behind a cursor the walk already used",
        ),
        (
            [
                _comment_page(
                    [_comment_node("https://github.com/o/r/issues/1", 1)],
                    parent="issue",
                    has_next=True,
                    cursor="c1",
                ),
                dict(_comment_page([], parent="issue"), errors=[{"message": "timedout"}]),
            ],
            "page 2: GraphQL reported errors",
        ),
    ],
    ids=[
        "null-repository",
        "no-issue",
        "string-issue",
        "no-connection",
        "list-connection",
        "no-pageinfo",
        "no-hasnext",
        "null-cursor",
        "errors",
        "cursor-reused",
        "errors-on-page-2",
    ],
)
def test_a_comment_listing_that_cannot_be_read_to_its_end_is_an_error(pages, needle):
    """#160 R1-F1: an identity read decides "no comment carries the marker" on
    this listing, so a page that cannot be read or walked past is a
    conclusive GitHubError naming the listing, never the comments it did
    read."""
    replies = iter(pages)
    with pytest.raises(GitHubError, match=needle) as exc:
        _client(lambda req: _res(next(replies))).get_issue_comments(
            "https://github.com/o/r/issues/1"
        )
    assert "comments of https://github.com/o/r/issues/1" in str(exc.value)


def test_a_comment_listing_failure_is_classified_as_usual():
    """A page `gh` could not fetch is transient or conclusive by its text, as
    every read is; a parent GitHub cannot resolve is not found. Never a
    shorter listing."""
    url = "https://github.com/o/r/issues/1"
    pages = iter(
        [_comment_page([_comment_node(url, 1)], parent="issue", has_next=True, cursor="c1")]
    )

    def flaky(req):
        try:
            return _res(next(pages))
        except StopIteration:
            return _res({}, exit_code=1, stderr="HTTP 502: Bad Gateway")

    with pytest.raises(GitHubUnavailableError, match="502"):
        GitHubClient(runner=flaky, retry_delay_seconds=0).get_issue_comments(url)
    missing = _res(
        _issue_comments_payload(None),
        exit_code=1,
        stderr="gh: Could not resolve to an Issue with the number of 1.",
    )
    with pytest.raises(GitHubNotFoundError):
        _client(lambda req: missing).get_issue_comments(url)


# -- typed effect writes (ADR 0004 D12.2) ----------------------------------------

_W_ISSUE = "https://github.com/o/r/issues/2"
_W_PR = "https://github.com/o/r/pull/42"
# A body that would be dangerous in argv: a newline, an option-looking line, a quote.
_W_BODY = "BODY-SENTINEL line one\n--jq .token\n'quoted'"
_W_TITLE = "TITLE-SENTINEL follow-up"

# name -> (call, method, path, the payload on stdin, a good reply, the typed result)
_WRITES = {
    "create_issue_comment": (
        lambda gh, body: gh.create_issue_comment(_W_ISSUE, body),
        "POST",
        "repos/o/r/issues/2/comments",
        {"body": _W_BODY},
        {"id": 950001, "html_url": f"{_W_ISSUE}#issuecomment-950001", "body": _W_BODY},
        ("https://github.com/o/r/issues/2#issuecomment-950001", None),
    ),
    "create_pr_comment": (
        lambda gh, body: gh.create_pr_comment(_W_PR, body),
        "POST",
        "repos/o/r/issues/42/comments",
        {"body": _W_BODY},
        {"id": 950002, "html_url": f"{_W_PR}#issuecomment-950002"},
        ("https://github.com/o/r/pull/42#issuecomment-950002", None),
    ),
    "create_pull_request": (
        lambda gh, body: gh.create_pull_request(
            "o/r", base="main", head="autoforge/2-feature", title=_W_TITLE, body=body
        ),
        "POST",
        "repos/o/r/pulls",
        {"title": _W_TITLE, "head": "autoforge/2-feature", "base": "main", "body": _W_BODY},
        {"number": 43, "html_url": "https://github.com/o/r/pull/43"},
        ("https://github.com/o/r/pull/43", 43),
    ),
    "write_pr_body": (
        lambda gh, body: gh.write_pr_body(_W_PR, body),
        "PATCH",
        "repos/o/r/pulls/42",
        {"body": _W_BODY},
        {"number": 42, "html_url": _W_PR},
        None,
    ),
    "create_issue": (
        lambda gh, body: gh.create_issue("o/r", title=_W_TITLE, body=body),
        "POST",
        "repos/o/r/issues",
        {"title": _W_TITLE, "body": _W_BODY},
        {"number": 7, "html_url": "https://github.com/o/r/issues/7"},
        ("https://github.com/o/r/issues/7", 7),
    ),
    "write_issue_body": (
        lambda gh, body: gh.write_issue_body(_W_ISSUE, body),
        "PATCH",
        "repos/o/r/issues/2",
        {"body": _W_BODY},
        {"number": 2, "html_url": _W_ISSUE},
        None,
    ),
}


def _raw(stdout: str, exit_code: int = 0, stderr: str = "") -> ExecutionResult:
    """A `gh` reply printed as-is (``--jq`` prints a string result unquoted)."""
    return replace(_res(None, exit_code=exit_code, stderr=stderr), stdout=stdout)


def _recording(reply_for):
    seen = []

    def handler(req):
        seen.append(req)
        return reply_for(req)

    return seen, GitHubClient(runner=handler, retry_delay_seconds=0)


@pytest.mark.parametrize("name", list(_WRITES))
def test_write_sends_its_payload_on_stdin_to_the_named_repository(name):
    call, method, path, payload, reply, result = _WRITES[name]
    seen, gh = _recording(lambda req: _res(reply))
    created = call(gh, _W_BODY)
    (req,) = seen
    assert req.command == ["gh", "api", "--method", method, path, "--input", "-"]
    assert json.loads(req.stdin_data.decode("utf-8")) == payload
    for arg in req.command:
        assert "SENTINEL" not in arg
    if result is None:
        assert created is None
    else:
        assert (created.url, created.number) == result


_TRANSIENT_WRITE_FAILURES = {
    "http-502": lambda: _res({}, exit_code=1, stderr="HTTP 502: Bad Gateway"),
    "connect": lambda: _res({}, exit_code=1, stderr="error connecting to api.github.com: timeout"),
    "timeout": lambda: replace(_res({}, exit_code=-9), timed_out=True),
}


@pytest.mark.parametrize("failure", list(_TRANSIENT_WRITE_FAILURES))
@pytest.mark.parametrize("name", list(_WRITES))
def test_write_is_never_retried_on_an_ambiguous_failure(name, failure):
    """D4.3: the first attempt may have landed, so a write is issued once and an
    ambiguous outcome is GitHubUnavailableError; a read with the same failure is
    retried."""
    seen, gh = _recording(lambda req: _TRANSIENT_WRITE_FAILURES[failure]())
    with pytest.raises(GitHubUnavailableError, match="outcome is unknown") as info:
        _WRITES[name][0](gh, _W_BODY)
    assert len(seen) == 1
    assert "SENTINEL" not in str(info.value)

    seen.clear()
    with pytest.raises(GitHubUnavailableError):
        gh.get_pr(_W_PR)
    assert len(seen) == 2  # the read keeps its retry


@pytest.mark.parametrize("name", list(_WRITES))
def test_write_refused_with_422_is_a_plain_github_error(name):
    stderr = "gh: Validation Failed (HTTP 422)"
    seen, gh = _recording(lambda req: _res({}, exit_code=1, stderr=stderr))
    with pytest.raises(GitHubError) as info:
        _WRITES[name][0](gh, _W_BODY)
    assert not isinstance(info.value, (GitHubUnavailableError, GitHubNotFoundError))
    assert len(seen) == 1


@pytest.mark.parametrize("name", list(_WRITES))
def test_write_to_a_missing_object_is_not_found(name):
    seen, gh = _recording(lambda req: _res({}, exit_code=1, stderr="gh: Not Found (HTTP 404)"))
    with pytest.raises(GitHubNotFoundError):
        _WRITES[name][0](gh, _W_BODY)
    assert len(seen) == 1


def test_write_failure_text_is_redacted_and_omits_the_payload():
    stderr = f"HTTP 502: Bad Gateway (Authorization: token {_SECRET})"
    seen, gh = _recording(lambda req: _res({}, exit_code=1, stderr=stderr))
    with pytest.raises(GitHubUnavailableError) as info:
        gh.create_issue_comment(_W_ISSUE, _W_BODY)
    text = str(info.value)
    assert _SECRET not in text and "***REDACTED***" in text
    assert "SENTINEL" not in text


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        json.dumps([{"number": 7}]),
        json.dumps({"message": "ok"}),
        json.dumps({"number": 7, "html_url": 7}),
    ],
    ids=["empty", "not-json", "array", "no-url", "int-url"],
)
@pytest.mark.parametrize("name", list(_WRITES))
def test_write_with_an_unparsable_reply_has_an_unknown_outcome(name, stdout):
    seen, gh = _recording(lambda req: _raw(stdout))
    with pytest.raises(GitHubUnavailableError, match="outcome is unknown") as info:
        _WRITES[name][0](gh, _W_BODY)
    assert len(seen) == 1
    assert "SENTINEL" not in str(info.value)


@pytest.mark.parametrize(
    ("name", "reply"),
    [
        # A comment on another issue, or whose id is not the one its URL names.
        (
            "create_issue_comment",
            {"id": 9, "html_url": "https://github.com/o/r/issues/3#issuecomment-9"},
        ),
        ("create_issue_comment", {"id": 8, "html_url": f"{_W_ISSUE}#issuecomment-9"}),
        (
            "create_pr_comment",
            {"id": 9, "html_url": "https://github.com/o/x/pull/42#issuecomment-9"},
        ),
        # A PR where an issue was asked for, another repository, another number.
        ("create_issue", {"number": 7, "html_url": "https://github.com/o/r/pull/7"}),
        ("create_issue", {"number": 7, "html_url": "https://github.com/o/x/issues/7"}),
        ("create_pull_request", {"number": 43, "html_url": "https://github.com/o/r/issues/43"}),
        ("create_pull_request", {"number": 44, "html_url": "https://github.com/o/r/pull/43"}),
        ("write_pr_body", {"number": 41, "html_url": "https://github.com/o/r/pull/41"}),
        ("write_issue_body", {"number": 3, "html_url": "https://github.com/o/r/issues/3"}),
    ],
)
def test_write_reply_naming_another_object_has_an_unknown_outcome(name, reply):
    seen, gh = _recording(lambda req: _res(reply))
    with pytest.raises(GitHubUnavailableError, match="outcome is unknown"):
        _WRITES[name][0](gh, _W_BODY)
    assert len(seen) == 1


def test_write_reply_spelling_the_repository_differently_is_the_same_object():
    seen, gh = _recording(
        lambda req: _res({"id": 5, "html_url": "https://github.com/O/R/issues/2#issuecomment-5"})
    )
    created = gh.create_issue_comment(_W_ISSUE, "hi")
    assert created.url == "https://github.com/O/R/issues/2#issuecomment-5"


@pytest.mark.parametrize("name", list(_WRITES))
def test_write_with_a_truncated_reply_has_an_unknown_outcome(name):
    """Unlike a read, a write `gh` accepted is not refused for an unreadable reply."""
    reply = _WRITES[name][4]
    seen, gh = _recording(lambda req: replace(_res(reply), stdout_truncated=True))
    with pytest.raises(GitHubUnavailableError, match="truncated"):
        _WRITES[name][0](gh, _W_BODY)
    assert len(seen) == 1


@pytest.mark.parametrize("name", list(_WRITES))
def test_write_bounds_refuse_an_oversize_body_before_any_process(name):
    call, *_, reply, _result = _WRITES[name]
    seen, gh = _recording(lambda req: _res(reply))
    with pytest.raises(GitHubError, match="65536") as info:
        call(gh, "x" * (MAX_BODY_CHARS + 1))
    assert not isinstance(info.value, GitHubUnavailableError)
    assert seen == []
    call(gh, "é" * MAX_BODY_CHARS)  # the bound counts characters, not bytes
    assert len(seen) == 1


@pytest.mark.parametrize(
    "call",
    [
        lambda gh, title: gh.create_issue("o/r", title=title, body="b"),
        lambda gh, title: gh.create_pull_request(
            "o/r", base="main", head="autoforge/2-feature", title=title, body="b"
        ),
    ],
    ids=["create_issue", "create_pull_request"],
)
def test_write_bounds_refuse_an_oversize_title_before_any_process(call):
    seen, gh = _recording(lambda req: _res({}))
    with pytest.raises(GitHubError, match="256"):
        call(gh, "t" * (MAX_TITLE_CHARS + 1))
    assert seen == []


@pytest.mark.parametrize(
    ("base", "head"),
    [("main", "fork-owner:feature"), ("main", ""), ("", "feature"), ("main", 5)],
)
def test_create_pull_request_refuses_a_head_outside_the_repository(base, head):
    seen, gh = _recording(lambda req: _res({}))
    with pytest.raises(GitHubError, match="not a branch name"):
        gh.create_pull_request("o/r", base=base, head=head, title="t", body="b")
    assert seen == []


@pytest.mark.parametrize("repository", ["o", "o/r/x", "../r", "o/..", "{owner}/{repo}", "o/r?x=1"])
def test_writes_and_reads_refuse_a_repository_that_is_not_owner_repo(repository):
    seen, gh = _recording(lambda req: _res({}))
    with pytest.raises(GitHubError, match="owner/repo"):
        gh.create_issue(repository, title="t", body="b")
    with pytest.raises(GitHubError, match="owner/repo"):
        gh.list_prs_for_head(repository, "feature")
    assert seen == []


# -- effect identity reads (ADR 0004 D4.5, D7.5) ---------------------------------

_BASE = "a" * 40
_COMMIT = "b" * 40


@pytest.mark.parametrize(
    ("status", "expected"),
    [("behind", True), ("identical", True), ("ahead", False), ("diverged", False)],
)
def test_commit_in_history_is_the_compare_status(status, expected):
    seen, gh = _recording(lambda req: _raw(f"{status}\n"))
    assert gh.commit_in_history("o/r", _BASE.upper(), _COMMIT) is expected
    (req,) = seen
    assert req.command == ["gh", "api", f"repos/o/r/compare/{_BASE}...{_COMMIT}", "--jq", ".status"]


def test_commit_in_history_reads_404_as_not_in_history():
    seen, gh = _recording(lambda req: _res({}, exit_code=1, stderr="gh: Not Found (HTTP 404)"))
    assert gh.commit_in_history("o/r", _BASE, _COMMIT) is False
    assert len(seen) == 1  # definitive: not retried


@pytest.mark.parametrize("stdout", ["", "unknown\n", '"behind"\n', "null\n"])
def test_commit_in_history_refuses_an_unknown_status(stdout):
    gh = _client(lambda req: _raw(stdout))
    with pytest.raises(GitHubError, match="unknown status") as info:
        gh.commit_in_history("o/r", _BASE, _COMMIT)
    assert not isinstance(info.value, GitHubUnavailableError)


def test_commit_in_history_is_a_read_that_keeps_its_retry():
    seen, gh = _recording(lambda req: _res({}, exit_code=1, stderr="HTTP 502: Bad Gateway"))
    with pytest.raises(GitHubUnavailableError):
        gh.commit_in_history("o/r", _BASE, _COMMIT)
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("base", "commit"),
    [("main", _COMMIT), (_BASE, "b" * 12), (_BASE, "g" * 40), (_BASE, "b" * 41), (_BASE, None)],
)
def test_commit_in_history_requires_full_shas_before_any_process(base, commit):
    seen, gh = _recording(lambda req: _raw("behind\n"))
    with pytest.raises(GitHubError, match="full commit SHA"):
        gh.commit_in_history("o/r", base, commit)
    assert seen == []


def test_commit_in_history_accepts_sha256_object_ids():
    seen, gh = _recording(lambda req: _raw("behind\n"))
    assert gh.commit_in_history("o/r", "c" * 64, "d" * 64) is True


def test_latest_issue_number_is_the_numeric_maximum_over_issues_and_prs():
    seen, gh = _recording(lambda req: _raw("5\n120\n\n7\n"))
    assert gh.latest_issue_number("o/r") == 120
    (req,) = seen
    assert req.command == [
        "gh",
        "api",
        "--paginate",
        "repos/o/r/issues?state=all&per_page=100",
        "--jq",
        ".[].number",
    ]
    assert _client(lambda req: _raw("")).latest_issue_number("o/r") == 0


@pytest.mark.parametrize("stdout", ["5\nnull\n", "0\n", "-3\n", "4.5\n", "abc\n"])
def test_latest_issue_number_refuses_a_malformed_number(stdout):
    with pytest.raises(GitHubError, match="not a positive integer"):
        _client(lambda req: _raw(stdout)).latest_issue_number("o/r")


def _listing_row(number: int, minute: int, *, pr: bool = False, second: int = 0) -> dict:
    row = {
        "number": number,
        "html_url": f"https://github.com/o/r/issues/{number}",
        "url": f"https://api.github.com/repos/o/r/issues/{number}",
        "title": f"issue {number}",
        "state": "closed" if number % 5 == 0 else "open",
        "body": None,
        "created_at": f"2026-01-01T{minute // 60:02d}:{minute % 60:02d}:{second:02d}Z",
    }
    if pr:
        row["html_url"] = f"https://github.com/o/r/pull/{number}"
        row["pull_request"] = {"url": f"https://api.github.com/repos/o/r/pulls/{number}"}
    return row


def _paged_listing(rows: list[dict]):
    """A handler serving ``rows`` 100 per page by the endpoint's ``page`` parameter."""
    from urllib.parse import parse_qs, urlsplit

    def reply(req):
        query = parse_qs(urlsplit(req.command[-1]).query)
        page = int(query["page"][0])
        return _res(rows[(page - 1) * 100 : page * 100])

    return reply


def test_list_issues_above_reads_newest_first_and_stops_at_the_watermark():
    # #300 created last, one per minute; every third number is a PR.
    rows = [_listing_row(n, n, pr=n % 3 == 0) for n in range(300, 0, -1)]
    seen, gh = _recording(_paged_listing(rows))
    issues = gh.list_issues_above("o/r", 150)
    assert [i.number for i in issues] == [n for n in range(300, 150, -1) if n % 3]
    assert issues[0].url == "https://github.com/o/r/issues/299"
    assert {i.state for i in issues} == {"OPEN", "CLOSED"}
    assert len(seen) == 2  # page 3 lies wholly below the watermark and is never read
    endpoint = seen[0].command[-1]
    assert seen[0].command[:2] == ["gh", "api"]
    assert endpoint.startswith("repos/o/r/issues?")
    for part in ("state=all", "sort=created", "direction=desc", "per_page=100", "page=1"):
        assert part in endpoint


def test_list_issues_above_reads_on_through_the_watermarks_second():
    """GitHub orders by creation time to the second: a row above the watermark
    created in the watermark row's second may come after it, even on the next page."""
    rows = [_listing_row(n, n) for n in range(400, 301, -1)]  # 99 rows, page 1
    rows.append(_listing_row(250, 200))  # the watermark row, last on page 1
    rows.append(_listing_row(251, 200))  # same second, first on page 2
    rows.append(_listing_row(249, 199))  # older: the walk ends on this page
    rows.extend(_listing_row(n, 100, second=1) for n in range(248, 140, -1))
    seen, gh = _recording(_paged_listing(rows))
    numbers = [i.number for i in gh.list_issues_above("o/r", 250)]
    assert numbers == [*range(400, 301, -1), 251]
    assert len(seen) == 2


def test_list_issues_above_reads_to_the_end_when_the_watermark_is_not_reached():
    rows = [_listing_row(n, n) for n in range(130, 0, -1)]
    seen, gh = _recording(_paged_listing(rows))
    assert len(gh.list_issues_above("o/r", 0)) == 130
    assert len(seen) == 2
    seen, gh = _recording(lambda req: _res([]))
    assert gh.list_issues_above("o/r", 0) == []


def test_list_issues_above_refuses_a_listing_it_cannot_read_down_to_the_watermark(
    monkeypatch,
):
    import autoforge.github as github_module

    monkeypatch.setattr(github_module, "_ISSUE_WALK_MAX_PAGES", 3)
    rows = [_listing_row(n, n) for n in range(1000, 0, -1)]
    seen, gh = _recording(_paged_listing(rows))
    with pytest.raises(GitHubError, match="cannot be read down to the watermark"):
        gh.list_issues_above("o/r", 10)
    assert len(seen) == 3


@pytest.mark.parametrize(
    "patch",
    [
        {"created_at": None},
        {"created_at": "2026-01-01T00:00:00"},  # no time zone
        {"created_at": "yesterday"},
        {"number": True},
        {"number": "7"},
        {"html_url": "https://github.com/o/x/issues/7"},
        {"title": 5},
    ],
)
def test_list_issues_above_refuses_a_malformed_row(patch):
    row = {**_listing_row(7, 7), **patch}
    gh = _client(lambda req: _res([row]))
    with pytest.raises(GitHubError) as info:
        gh.list_issues_above("o/r", 1)
    assert not isinstance(info.value, GitHubUnavailableError)


@pytest.mark.parametrize("watermark", [-1, True, "5", None])
def test_list_issues_above_refuses_a_watermark_that_is_not_a_number(watermark):
    seen, gh = _recording(lambda req: _res([]))
    with pytest.raises(GitHubError, match="watermark"):
        gh.list_issues_above("o/r", watermark)
    assert seen == []


def _rest_pr(number: int, *, state: str = "open", merged: bool = False, **head) -> dict:
    head_repo = head.pop("repo", {"name": "r", "owner": {"login": "o"}})
    return {
        "number": number,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "title": f"PR {number}",
        "state": state,
        "merged_at": "2026-01-02T00:00:00Z" if merged else None,
        "draft": False,
        "body": "b",
        "auto_merge": None,
        "head": {
            "ref": head.pop("ref", "autoforge/2-feature"),
            "sha": "ABC" + "0" * 37,
            "repo": head_repo,
        },
        "base": {"ref": "main"},
    }


def test_list_prs_for_head_filters_by_head_and_sees_every_state():
    rows = [
        _rest_pr(40, state="closed", merged=True),
        _rest_pr(41, state="closed"),
        _rest_pr(42),
        # Not headed at this repository's branch: a fork, a deleted head
        # repository, another branch.
        _rest_pr(43, repo={"name": "r", "owner": {"login": "fork"}}),
        _rest_pr(44, repo=None),
        _rest_pr(45, ref="autoforge/2-feature-2"),
    ]
    seen, gh = _recording(lambda req: _res([rows[:3], rows[3:]]))
    prs = gh.list_prs_for_head("o/r", "autoforge/2-feature")
    assert [(p.number, p.state) for p in prs] == [(42, "OPEN"), (41, "CLOSED"), (40, "MERGED")]
    pr = prs[0]
    assert pr.head_sha == "abc" + "0" * 37 and pr.base_ref == "main"
    assert pr.head_ref == "autoforge/2-feature" and pr.head_repository == "o/r"
    assert pr.linked_issue_numbers == [] and pr.checks == []
    (req,) = seen
    assert req.command[:4] == ["gh", "api", "--paginate", "--slurp"]
    from urllib.parse import parse_qs, urlsplit

    endpoint = urlsplit(req.command[4])
    assert endpoint.path == "repos/o/r/pulls"
    assert parse_qs(endpoint.query) == {
        "state": ["all"],
        "head": ["o:autoforge/2-feature"],
        "per_page": ["100"],
    }


@pytest.mark.parametrize(
    "row",
    [
        {**_rest_pr(42), "head": None},
        {**_rest_pr(42), "base": "main"},
        {**_rest_pr(42), "html_url": "https://github.com/o/r/issues/42"},
        {**_rest_pr(42), "html_url": "https://github.com/o/x/pull/42"},
        "not-an-object",
    ],
    ids=["no-head", "base-not-object", "issue-url", "other-repo", "non-object"],
)
def test_list_prs_for_head_refuses_a_malformed_row(row):
    gh = _client(lambda req: _res([[row]]))
    with pytest.raises(GitHubError):
        gh.list_prs_for_head("o/r", "autoforge/2-feature")


@pytest.mark.parametrize("branch", ["", "o:feature", None])
def test_list_prs_for_head_refuses_a_branch_that_is_not_a_branch_name(branch):
    seen, gh = _recording(lambda req: _res([]))
    with pytest.raises(GitHubError, match="not a branch name"):
        gh.list_prs_for_head("o/r", branch)
    assert seen == []


# -- FakeGitHub's typed effect writes (the in-memory model engine tests use) -----


def _fake():
    from tests.conftest import BRANCH, FakeGitHub

    fake = FakeGitHub()
    fake.branch_heads[BRANCH] = "a" * 40
    return fake


def test_fake_effect_writes_mutate_the_stores_and_are_recorded():
    from tests.conftest import BRANCH, ISSUE

    fake = _fake()
    pr = fake.create_pull_request(
        "owner/repo", base="main", head=BRANCH, title="Feature", body="Closes #2"
    )
    assert pr.url == "https://github.com/owner/repo/pull/3" and pr.number == 3
    stored = fake.get_pr(pr.url)
    assert stored.title == "Feature" and stored.linked_issue_numbers == [2]
    assert stored.head_ref == BRANCH and stored.head_repository == "owner/repo"
    issue = fake.create_issue("owner/repo", title="Follow-up", body="b")
    assert issue.number == 4 and fake.get_issue(issue.url).body == "b"
    comment = fake.create_issue_comment(ISSUE, "progress")
    assert comment.url.startswith(f"{ISSUE}#issuecomment-95")
    assert [c.url for c in fake.get_issue_comments(ISSUE)] == [comment.url]
    fake.write_pr_body(pr.url, "new body")
    fake.write_issue_body(ISSUE, "issue body")
    assert fake.get_pr(pr.url).body == "new body" and fake.get_issue(ISSUE).body == "issue body"
    assert [w[0] for w in fake.effect_writes] == [
        "create_pull_request",
        "create_issue",
        "create_issue_comment",
        "write_pr_body",
        "write_issue_body",
    ]
    assert fake.effect_writes[2] == ("create_issue_comment", ISSUE, "progress")
    assert fake.latest_issue_number("owner/repo") == 4
    assert [i.number for i in fake.list_issues_above("owner/repo", 1)] == [4, 2]
    assert [p.number for p in fake.list_prs_for_head("owner/repo", BRANCH)] == [3]
    with pytest.raises(GitHubError, match="already exists"):
        fake.create_pull_request("owner/repo", base="main", head=BRANCH, title="t", body="b")
    with pytest.raises(GitHubNotFoundError):
        fake.create_issue_comment("https://github.com/owner/repo/issues/99", "x")
    with pytest.raises(GitHubError, match="not found"):
        fake.create_pr_comment("https://github.com/owner/repo/pull/99", "x")
    assert fake.create_pr_comment(pr.url, "review").url.startswith(f"{pr.url}#issuecomment-95")


@pytest.mark.parametrize("lands", [False, True])
def test_fake_write_failures_model_a_lost_write_and_a_lost_reply(lands):
    from tests.conftest import ISSUE

    fake = _fake()
    error = GitHubUnavailableError("HTTP 502: Bad Gateway; the outcome is unknown")
    fake.write_failures.append(("create_issue_comment", error, lands))
    with pytest.raises(GitHubUnavailableError):
        fake.create_issue_comment(ISSUE, "progress")
    assert len(fake.get_issue_comments(ISSUE)) == (1 if lands else 0)
    assert fake.write_failures == [] and len(fake.effect_writes) == 1
    fake.create_issue_comment(ISSUE, "again")  # the entry was consumed
    assert len(fake.get_issue_comments(ISSUE)) == (2 if lands else 1)


def test_fake_write_bounds_refuse_before_any_attempt_and_history_must_be_set():
    from tests.conftest import ISSUE

    fake = _fake()
    with pytest.raises(GitHubError):
        fake.create_issue_comment(ISSUE, "x" * (MAX_BODY_CHARS + 1))
    assert fake.effect_writes == []
    with pytest.raises(AssertionError):
        fake.commit_in_history("owner/repo", "a" * 40, "b" * 40)
    fake.history = lambda repository, base, commit: commit == "b" * 40
    assert fake.commit_in_history("owner/repo", "a" * 40, "B" * 40) is True
