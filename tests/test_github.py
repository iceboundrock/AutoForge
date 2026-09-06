"""GitHub client with injected fake `gh` runner (no network)."""

import json

import pytest

from autoforge.errors import GitHubError
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


def test_current_repo_uses_repo_view():
    seen = []

    def handler(req):
        seen.append(req.command)
        return _res({"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}})

    assert _client(handler).current_repo().name_with_owner == "o/r"
    assert seen[0][:3] == ["gh", "repo", "view"]
