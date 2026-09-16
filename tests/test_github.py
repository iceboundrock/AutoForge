"""GitHub client with injected fake `gh` runner (no network)."""

import json
from dataclasses import replace

import pytest

from autoforge.errors import GitHubError, GitHubNotFoundError, GitHubUnavailableError
from autoforge.executor import ExecutionResult
from autoforge.github import (
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
    seen = []

    def handler(req):
        seen.append(req.command)
        return ExecutionResult(req.command, None, 0, json.dumps(prs), "", "t", "t")

    def limit_of(cmd):
        return cmd[cmd.index("--limit") + 1]

    gh = _client(handler)
    found = gh.find_open_prs_for_issue("https://github.com/o/r/issues/2")
    assert sorted(p.number for p in found) == [1, 2]
    assert limit_of(seen[0]) == "100"

    # strict: reads far more before giving up, and refuses a set it cannot
    # prove complete rather than reporting "no candidate".
    found = gh.find_open_prs_for_issue("https://github.com/o/r/issues/2", strict=True)
    assert sorted(p.number for p in found) == [1, 2]
    assert limit_of(seen[1]) == str(STRICT_PR_LIST_LIMIT)

    full = [
        dict(prs[0], number=n, url=f"https://github.com/o/r/pull/{n}")
        for n in range(1, STRICT_PR_LIST_LIMIT + 1)
    ]
    truncating = _client(
        lambda req: ExecutionResult(req.command, None, 0, json.dumps(full), "", "t", "t")
    )
    with pytest.raises(GitHubError, match="truncated"):
        truncating.find_open_prs_for_issue("https://github.com/o/r/issues/2", strict=True)
    # Non-strict callers are unaffected: a full page is not an error for them.
    assert truncating.find_open_prs_for_issue("https://github.com/o/r/issues/2")


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


def test_list_open_prs_strict_refuses_a_possibly_truncated_listing():
    """A repository-wide candidate lookup must not report a limit as "none"."""
    one = [
        {
            "url": "https://github.com/o/r/pull/1",
            "number": 1,
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "headRefName": "feature/x",
            "baseRefName": "main",
            "body": "hello",
        }
    ]
    seen = []

    def handler(req):
        seen.append(req.command)
        return ExecutionResult(req.command, None, 0, json.dumps(one), "", "t", "t")

    gh = _client(handler)
    # Repository-wide: no issue linkage or branch-name filter is applied.
    assert [p.number for p in gh.list_open_prs("o/r")] == [1]
    assert seen[0][seen[0].index("--limit") + 1] == "100"
    assert [p.body for p in gh.list_open_prs("o/r", strict=True)] == ["hello"]
    assert seen[1][seen[1].index("--limit") + 1] == str(STRICT_PR_LIST_LIMIT)

    full = [
        dict(one[0], number=n, url=f"https://github.com/o/r/pull/{n}")
        for n in range(1, STRICT_PR_LIST_LIMIT + 1)
    ]
    truncating = _client(
        lambda req: ExecutionResult(req.command, None, 0, json.dumps(full), "", "t", "t")
    )
    with pytest.raises(GitHubError, match="truncated"):
        truncating.list_open_prs("o/r", strict=True)
    assert len(truncating.list_open_prs("o/r")) == STRICT_PR_LIST_LIMIT


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
