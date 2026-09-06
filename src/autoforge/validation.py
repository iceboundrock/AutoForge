"""Typed GitHub URL parsing + run-argument validation.

Every module that needs owner/repo/number from a GitHub URL goes through
these parsers — no ad-hoc ``url.split("/")`` anywhere else.

Accepted shapes (HTTPS, ``github.com`` host only)::

    https://github.com/<owner>/<repo>/issues/<number>
    https://github.com/<owner>/<repo>/pull/<number>
    https://github.com/<owner>/<repo>/pull/<number>#issuecomment-<id>
    https://github.com/<owner>/<repo>/issues/<number>#issuecomment-<id>
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlsplit

from .errors import ConfigurationError

_NAME = r"[A-Za-z0-9_.-]+"
_PATH_RE = re.compile(
    rf"^/(?P<owner>{_NAME})/(?P<repo>{_NAME})/(?P<kind>issues|pull)/(?P<number>[0-9]+)/?$"
)
_COMMENT_FRAGMENT_RE = re.compile(r"^issuecomment-(?P<id>[0-9]+)$")

_EXPECTED = "expected https://github.com/<owner>/<repo>/(issues|pull)/<n>"


@dataclass(frozen=True)
class GitHubRef:
    """Base for typed references to an issue / pull request."""

    kind: ClassVar[str] = ""
    _path_segment: ClassVar[str] = ""

    owner: str
    repo: str
    number: int

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def canonical(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/{self._path_segment}/{self.number}"

    def same_repository(self, other: GitHubRef | str) -> bool:
        other_repo = other if isinstance(other, str) else other.repository
        return self.repository.lower() == other_repo.strip().lower()


@dataclass(frozen=True)
class GitHubIssueRef(GitHubRef):
    kind: ClassVar[str] = "issue"
    _path_segment: ClassVar[str] = "issues"


@dataclass(frozen=True)
class GitHubPullRequestRef(GitHubRef):
    kind: ClassVar[str] = "pr"
    _path_segment: ClassVar[str] = "pull"


@dataclass(frozen=True)
class GitHubCommentRef:
    """An issue-style comment (``#issuecomment-<id>``) on an issue or PR."""

    parent: GitHubIssueRef | GitHubPullRequestRef
    comment_id: int

    @property
    def repository(self) -> str:
        return self.parent.repository

    @property
    def canonical(self) -> str:
        return f"{self.parent.canonical}#issuecomment-{self.comment_id}"


# Backwards-compatible alias used by Phase-1 callers/tests.
ParsedURL = GitHubRef


def _split(url: str):
    if not isinstance(url, str) or not url.strip():
        raise ConfigurationError(f"not a valid HTTPS GitHub URL: {url!r} ({_EXPECTED})")
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise ConfigurationError(
            f"GitHub URL must use https, got {parts.scheme or 'no'} scheme: {url!r}"
        )
    if parts.netloc.lower() != "github.com":
        raise ConfigurationError(f"GitHub URL host must be github.com: {url!r}")
    if parts.query:
        raise ConfigurationError(f"GitHub URL must not carry a query string: {url!r}")
    return parts


def parse_github_url(url: str, expect: str | None = None) -> GitHubIssueRef | GitHubPullRequestRef:
    """Parse + validate an issue or PR URL into a typed reference.

    ``expect`` optionally constrains to ``"issue"`` or ``"pr"``. Any
    malformed input raises ConfigurationError with a precise reason.
    """
    parts = _split(url)
    if parts.fragment:
        raise ConfigurationError(f"unexpected fragment in issue/PR URL: {url!r}")
    m = _PATH_RE.match(parts.path)
    if not m:
        raise ConfigurationError(f"malformed GitHub URL: {url!r} ({_EXPECTED})")
    number = int(m.group("number"))
    if number < 1:
        raise ConfigurationError(f"GitHub issue/PR number must be >= 1: {url!r}")
    ref: GitHubIssueRef | GitHubPullRequestRef
    if m.group("kind") == "issues":
        ref = GitHubIssueRef(owner=m.group("owner"), repo=m.group("repo"), number=number)
    else:
        ref = GitHubPullRequestRef(owner=m.group("owner"), repo=m.group("repo"), number=number)
    if expect and ref.kind != expect:
        raise ConfigurationError(f"expected a GitHub {expect} URL, got {ref.kind}: {url!r}")
    return ref


def parse_issue_url(url: str) -> GitHubIssueRef:
    ref = parse_github_url(url, expect="issue")
    assert isinstance(ref, GitHubIssueRef)
    return ref


def parse_pr_url(url: str) -> GitHubPullRequestRef:
    ref = parse_github_url(url, expect="pr")
    assert isinstance(ref, GitHubPullRequestRef)
    return ref


def parse_comment_url(url: str) -> GitHubCommentRef:
    """Parse ``.../pull/<n>#issuecomment-<id>`` (or the issues variant)."""
    parts = _split(url)
    m = _PATH_RE.match(parts.path)
    if not m:
        raise ConfigurationError(f"malformed GitHub comment URL: {url!r}")
    fm = _COMMENT_FRAGMENT_RE.match(parts.fragment or "")
    if not fm:
        raise ConfigurationError(f"GitHub comment URL must end with '#issuecomment-<id>': {url!r}")
    parent = parse_github_url(url.split("#", 1)[0])
    return GitHubCommentRef(parent=parent, comment_id=int(fm.group("id")))


def validate_epic_and_issue(epic_url: str, issue_url: str) -> tuple[GitHubIssueRef, GitHubIssueRef]:
    """Validate run URLs; epic and issue must live in the same repository."""
    epic = parse_issue_url(epic_url)
    issue = parse_issue_url(issue_url)
    if not epic.same_repository(issue):
        raise ConfigurationError(
            f"EPIC repo {epic.repository!r} != issue repo {issue.repository!r}; "
            "cross-repository runs are not supported"
        )
    return epic, issue


_REMOTE_HTTPS_RE = re.compile(
    r"^(?:https?://|ssh://git@)github\.com[/:](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_REMOTE_SCP_RE = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$"
)


def parse_remote_repository(remote: str) -> str:
    """Return ``owner/repo`` for a GitHub remote URL (https, ssh://, or scp-like).

    Raises ConfigurationError for non-GitHub or malformed remotes.
    """
    text = (remote or "").strip()
    m = _REMOTE_HTTPS_RE.match(text) or _REMOTE_SCP_RE.match(text)
    if not m:
        raise ConfigurationError(f"remote {remote!r} is not a recognizable github.com remote")
    return f"{m.group('owner')}/{m.group('repo')}"
