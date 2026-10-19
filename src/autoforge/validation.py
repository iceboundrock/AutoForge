"""Typed GitHub URL parsing + run-argument validation.

Every module that needs owner/repo/number from a GitHub URL goes through
these parsers — no ad-hoc ``url.split("/")`` anywhere else.

Accepted shapes (HTTPS, ``github.com`` host only)::

    https://github.com/<owner>/<repo>/issues/<number>
    https://github.com/<owner>/<repo>/pull/<number>
    https://github.com/<owner>/<repo>/pull/<number>#issuecomment-<id>
    https://github.com/<owner>/<repo>/issues/<number>#issuecomment-<id>

The REST API names an issue's parent and repository by API URL instead
(``parent_issue_url``, ``repository_url``); :func:`parse_api_issue_url` and
:func:`parse_api_repository_url` read those two shapes, host
``api.github.com`` only::

    https://api.github.com/repos/<owner>/<repo>/issues/<number>
    https://api.github.com/repos/<owner>/<repo>
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
_API_REPOSITORY_RE = re.compile(rf"^/repos/(?P<owner>{_NAME})/(?P<repo>{_NAME})/?$")
_API_ISSUE_RE = re.compile(
    rf"^/repos/(?P<owner>{_NAME})/(?P<repo>{_NAME})/issues/(?P<number>[0-9]+)/?$"
)

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

    @property
    def identity(self) -> tuple[str, str, str, int]:
        """The issue / PR as GitHub identifies it: kind, owner, repo, number.

        Owner and repository are folded to lower case because GitHub treats
        them case-insensitively. This is the one definition of identity:
        :meth:`same_target` compares it, and a set or dict keyed by it (a
        snapshot of PRs, a membership check) applies the same rule as a
        pairwise comparison, so the two can never drift.
        """
        return (self.kind, self.owner.lower(), self.repo.lower(), self.number)

    def same_repository(self, other: GitHubRef | str) -> bool:
        other_repo = other if isinstance(other, str) else other.repository
        return self.repository.lower() == other_repo.strip().lower()

    def same_target(self, other: GitHubRef) -> bool:
        """Whether both refs name the same issue / PR.

        GitHub owner and repository names are case-insensitive, so
        ``.../Owner/Repo/issues/23`` and ``.../owner/repo/issues/23`` are the
        same issue although their canonical URLs differ. Identity checks must
        use this (or :attr:`identity`), never a string comparison of URLs.
        """
        return self.identity == other.identity


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
    """An issue-style comment (``#issuecomment-<id>``) on an issue or PR.

    GitHub serves one such comment under both paths of its parent: a pull
    request is an issue to the comments API, issues and pull requests share
    one number space, and ``.../issues/<n>#issuecomment-<id>`` and
    ``.../pull/<n>#issuecomment-<id>`` open the same comment. The parent's
    path kind is therefore part of the spelling, not of the comment's
    identity: :attr:`identity` and :meth:`same_target` compare the
    repository (case-folded), the parent number and the comment id, and
    :meth:`on` says whether the comment is on a given issue or PR under
    either spelling. The kind the URL used is still kept in :attr:`parent`
    and rendered by :attr:`canonical`.
    """

    parent: GitHubIssueRef | GitHubPullRequestRef
    comment_id: int

    @property
    def repository(self) -> str:
        return self.parent.repository

    @property
    def canonical(self) -> str:
        return f"{self.parent.canonical}#issuecomment-{self.comment_id}"

    @property
    def identity(self) -> tuple[str, str, int, int]:
        """The comment as GitHub identifies it: owner, repo, parent number, comment id."""
        return (
            self.parent.owner.lower(),
            self.parent.repo.lower(),
            self.parent.number,
            self.comment_id,
        )

    def same_target(self, other: GitHubCommentRef) -> bool:
        """Same comment as GitHub sees it, whichever parent path either URL used."""
        return self.identity == other.identity

    def on(self, target: GitHubRef) -> bool:
        """Whether this comment is on ``target``, under either of GitHub's paths.

        A reviewer naming a PR comment by its ``issues/<n>`` URL names this
        PR's comment; a comment on another number or repository does not.
        """
        return self.parent.same_repository(target) and self.parent.number == target.number


# Backwards-compatible alias used by Phase-1 callers/tests.
ParsedURL = GitHubRef


def same_issue_url(observed: str, expected: str) -> bool:
    """Issue identity as GitHub sees it, never vacuous: an unusable URL matches nothing.

    Agent-claimed and persisted URLs are compared with this, never by string
    equality of their canonical forms: the canonical form keeps the owner and
    repository spelling its author used, and GitHub treats ``Owner/Repo`` and
    ``owner/repo`` as one repository (see :meth:`GitHubRef.same_target`).
    """
    try:
        return parse_issue_url(observed).same_target(parse_issue_url(expected))
    except ConfigurationError:
        return False


def same_pr_url(observed: str, expected: str) -> bool:
    """PR identity as GitHub sees it (see :func:`same_issue_url`)."""
    try:
        return parse_pr_url(observed).same_target(parse_pr_url(expected))
    except ConfigurationError:
        return False


def _split(url: str):
    if not isinstance(url, str) or not url.strip():
        raise ConfigurationError(f"not a valid HTTPS GitHub URL: {url!r} ({_EXPECTED})")
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:  # an unbalanced or invalid bracketed host, an NFKC netloc
        raise ConfigurationError(f"not a valid HTTPS GitHub URL: {url!r} ({exc})") from None
    if parts.scheme != "https":
        raise ConfigurationError(
            f"GitHub URL must use https, got {parts.scheme or 'no'} scheme: {url!r}"
        )
    if parts.netloc.lower() != "github.com":
        raise ConfigurationError(f"GitHub URL host must be github.com: {url!r}")
    if parts.query:
        raise ConfigurationError(f"GitHub URL must not carry a query string: {url!r}")
    return parts


def _number(digits: str, what: str, url: str) -> int:
    """``digits`` (ASCII, matched by a pattern) as an int; too many of them are malformed."""
    try:
        return int(digits)
    except ValueError:  # over the interpreter's integer string conversion limit
        raise ConfigurationError(
            f"GitHub {what} has {len(digits)} digits, too many for a number: {url!r}"
        ) from None


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
    number = _number(m.group("number"), "issue/PR number", url)
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
    return GitHubCommentRef(parent=parent, comment_id=_number(fm.group("id"), "comment id", url))


def _split_api(url: str):
    """``url`` as an ``https://api.github.com`` URL with no query or fragment."""
    if not isinstance(url, str) or not url.strip():
        raise ConfigurationError(f"not a valid GitHub API URL: {url!r}")
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise ConfigurationError(f"not a valid GitHub API URL: {url!r} ({exc})") from None
    if parts.scheme != "https" or parts.netloc.lower() != "api.github.com":
        raise ConfigurationError(f"GitHub API URL must be https://api.github.com/...: {url!r}")
    if parts.query or parts.fragment:
        raise ConfigurationError(f"GitHub API URL must not carry a query or fragment: {url!r}")
    return parts


def parse_api_issue_url(url: str) -> GitHubIssueRef:
    """The issue an API URL (``.../repos/<owner>/<repo>/issues/<n>``) names.

    The REST spelling of an issue reference, such as ``parent_issue_url``;
    the result is the same typed reference :func:`parse_issue_url` returns
    for the issue's HTML URL, so the two compare with ``same_target``.
    """
    m = _API_ISSUE_RE.match(_split_api(url).path)
    if not m:
        raise ConfigurationError(f"malformed GitHub API issue URL: {url!r}")
    number = _number(m.group("number"), "issue number", url)
    if number < 1:
        raise ConfigurationError(f"GitHub issue number must be >= 1: {url!r}")
    return GitHubIssueRef(owner=m.group("owner"), repo=m.group("repo"), number=number)


def parse_api_repository_url(url: str) -> str:
    """``owner/repo`` of an API repository URL (``.../repos/<owner>/<repo>``)."""
    m = _API_REPOSITORY_RE.match(_split_api(url).path)
    if not m:
        raise ConfigurationError(f"malformed GitHub API repository URL: {url!r}")
    return f"{m.group('owner')}/{m.group('repo')}"


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
