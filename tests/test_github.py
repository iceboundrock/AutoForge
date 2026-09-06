"""GitHub client with injected fake `gh` runner (no network)."""

import json
from dataclasses import replace

import pytest

from autoforge.errors import GitHubError, GitHubUnavailableError
from autoforge.executor import ExecutionResult
from autoforge.github import GitHubClient


def _res(payload: dict, exit_code: int = 0, stderr: str = "") -> ExecutionResult:
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
        if "api" in req.command:
            return _res(
                {
                    "id": 5,
                    "html_url": "https://github.com/o/r/pull/42#issuecomment-5",
                    "body": "# AI Code Review — Round 1",
                    "user": {"login": "bot"},
                    "created_at": "2026-01-01T00:00:00Z",
                }
            )
        return _res(
            {
                "url": "https://github.com/o/r/pull/42",
                "comments": [
                    {
                        "id": 5,
                        "url": "https://github.com/o/r/pull/42#issuecomment-5",
                        "body": "hello",
                        "author": {"login": "bot"},
                        "createdAt": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        )

    gh = _client(handler)
    comments = gh.get_pr_comments("https://github.com/o/r/pull/42")
    assert comments[0].id == 5 and comments[0].author == "bot"
    c = gh.get_comment("https://github.com/o/r/pull/42#issuecomment-5")
    assert c.id == 5 and "Round 1" in c.body
    assert seen[-1][:2] == ["gh", "api"] and "repos/o/r/issues/comments/5" in seen[-1][2]


def test_find_open_prs_for_issue():
    prs = [
        {
            "url": "https://github.com/o/r/pull/1",
            "number": 1,
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "headRefName": "autoforge/2-x",
            "baseRefName": "main",
        },
        {
            "url": "https://github.com/o/r/pull/2",
            "number": 2,
            "state": "OPEN",
            "headRefOid": "b" * 40,
            "headRefName": "feature/other",
            "baseRefName": "main",
            "closingIssuesReferences": [{"number": 2}],
        },
        {
            "url": "https://github.com/o/r/pull/3",
            "number": 3,
            "state": "OPEN",
            "headRefOid": "c" * 40,
            "headRefName": "autoforge/20",
            "baseRefName": "main",
        },
    ]
    gh = _client(lambda req: ExecutionResult(req.command, None, 0, json.dumps(prs), "", "t", "t"))
    found = gh.find_open_prs_for_issue("https://github.com/o/r/issues/2")
    assert sorted(p.number for p in found) == [1, 2]


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

    pr2 = _client(lambda req: _res({"url": "https://github.com/o/r/pull/42", "state": "OPEN"}))
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
                "state": "OPEN",
                "statusCheckRollup": [
                    {
                        "__typename": "CheckRun",
                        "name": "ci",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    },
                    {"__typename": "StatusContext", "context": "legacy", "state": "PENDING"},
                ],
            }
        )
    )
    checks = gh.get_pr_checks("https://github.com/o/r/pull/42")
    assert [(c.name, c.outcome) for c in checks] == [("ci", "success"), ("legacy", "pending")]


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
