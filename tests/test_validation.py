"""Typed GitHub URL parsing."""

import pytest

from autoforge.errors import ConfigurationError
from autoforge.validation import (
    GitHubIssueRef,
    GitHubPullRequestRef,
    parse_comment_url,
    parse_github_url,
    parse_issue_url,
    parse_pr_url,
    parse_remote_repository,
)


def test_issue_and_pr_refs():
    i = parse_issue_url("https://github.com/Owner/Repo/issues/12")
    assert isinstance(i, GitHubIssueRef)
    assert (i.owner, i.repo, i.number) == ("Owner", "Repo", 12)
    assert i.repository == "Owner/Repo"
    assert i.canonical == "https://github.com/Owner/Repo/issues/12"
    p = parse_pr_url("https://github.com/Owner/Repo/pull/7/")
    assert isinstance(p, GitHubPullRequestRef) and p.number == 7
    assert i.same_repository(p)
    assert parse_github_url("https://github.com/o/r/pull/7").kind == "pr"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r/issues/1",
        "https://gitlab.com/o/r/issues/1",
        "https://github.com/o/issues/1",
        "https://github.com/o/r/issues/abc",
        "https://github.com/o/r/issues/",
        "https://github.com/o/r/commits/1",
        "https://github.com/o/r/pull/1?x=1",
        "not-a-url",
        "",
    ],
)
def test_malformed_urls_rejected(url):
    with pytest.raises(ConfigurationError):
        parse_github_url(url)


def test_wrong_kind_rejected():
    with pytest.raises(ConfigurationError):
        parse_issue_url("https://github.com/o/r/pull/1")
    with pytest.raises(ConfigurationError):
        parse_pr_url("https://github.com/o/r/issues/1")


def test_comment_url():
    c = parse_comment_url("https://github.com/o/r/pull/42#issuecomment-123456")
    assert c.comment_id == 123456 and c.parent.number == 42 and c.parent.kind == "pr"
    with pytest.raises(ConfigurationError):
        parse_comment_url("https://github.com/o/r/pull/42")


def test_remote_parsing():
    assert parse_remote_repository("https://github.com/o/r.git") == "o/r"
    assert parse_remote_repository("git@github.com:o/r.git") == "o/r"
    assert parse_remote_repository("ssh://git@github.com/o/r") == "o/r"
    with pytest.raises(ConfigurationError):
        parse_remote_repository("https://example.com/o/r.git")
