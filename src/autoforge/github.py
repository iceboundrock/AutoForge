"""GitHub abstraction over the `gh` CLI (verification + controller-owned merge).

Business logic must use these typed objects, never parse raw `gh` JSON
inline. Agents perform content writes (branches, PRs, comments, issues)
under controller prompts, and the controller only *verifies* what they
claim through this client. The single exception is :meth:`GitHubClient.merge_pr`:
merging is owned by the controller (never by an agent), sits behind the
merge safety gate in the engine, and is always bound to the reviewed HEAD
via ``--match-head-commit``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import ConfigurationError, GitHubError
from .executor import ExecutionRequest, ExecutionResult, execute
from .validation import (
    GitHubCommentRef,
    GitHubIssueRef,
    GitHubPullRequestRef,
    parse_comment_url,
    parse_github_url,
    parse_issue_url,
    parse_pr_url,
)

_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "502",
    "503",
    "504",
    "tls handshake",
    "no such host",
)


@dataclass
class RepoInfo:
    name_with_owner: str
    default_branch: str = ""


@dataclass
class IssueInfo:
    url: str
    number: int
    title: str
    state: str  # OPEN | CLOSED
    body: str = ""
    repository: str = ""

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"


@dataclass
class CheckInfo:
    name: str
    state: str  # COMPLETED | IN_PROGRESS | ...
    conclusion: str = ""  # SUCCESS | FAILURE | ...


@dataclass
class CommentInfo:
    id: int
    url: str
    body: str
    author: str = ""
    created_at: str = ""
    parent_url: str = ""  # canonical issue/PR URL this comment belongs to


@dataclass
class PRInfo:
    url: str
    number: int
    title: str
    state: str  # OPEN | CLOSED | MERGED
    head_sha: str
    base_ref: str = ""
    head_ref: str = ""
    mergeable: str = ""  # MERGEABLE | CONFLICTING | UNKNOWN
    is_draft: bool = False
    body: str = ""
    repository: str = ""
    head_repository: str = ""
    linked_issue_numbers: list[int] = field(default_factory=list)
    checks: list[CheckInfo] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"


Runner = Callable[[ExecutionRequest], ExecutionResult]

_PR_FIELDS = (
    "url,number,title,state,headRefOid,baseRefName,headRefName,mergeable,isDraft,body,"
    "headRepository,headRepositoryOwner,closingIssuesReferences,statusCheckRollup"
)
_PR_LIST_FIELDS = (
    "url,number,title,state,headRefOid,baseRefName,headRefName,isDraft,body,"
    "headRepository,headRepositoryOwner,closingIssuesReferences"
)


def _repo_of(url: str) -> str:
    try:
        return parse_github_url(url).repository
    except Exception:
        return ""


MERGE_METHODS = ("squash", "merge", "rebase")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def build_merge_argv(
    pr_url: str,
    method: str = "squash",
    match_head_sha: str = "",
    delete_branch: bool = False,
) -> list[str]:
    """Pure argv builder for ``gh pr merge`` (no ``gh`` prefix, no execution).

    Shared by :meth:`GitHubClient.merge_pr` and the engine's dry-run plan so
    the command shown in dry-run is exactly the one that would run.
    ``match_head_sha`` must be the full 40-hex reviewed HEAD; GitHub then
    refuses the merge server-side if the PR HEAD moved.
    """
    if method not in MERGE_METHODS:
        raise ConfigurationError(f"merge.method must be one of {MERGE_METHODS}, got {method!r}")
    if not match_head_sha or not _SHA40_RE.match(match_head_sha.lower()):
        raise ConfigurationError(
            "merge requires the full 40-hex reviewed HEAD SHA for --match-head-commit, "
            f"got {match_head_sha!r}"
        )
    ref = parse_pr_url(pr_url)
    argv = ["pr", "merge", ref.canonical, f"--{method}", "--match-head-commit", match_head_sha]
    if delete_branch:
        argv.append("--delete-branch")
    return argv


class GitHubClient:
    """Thin typed wrapper around `gh`. Inject ``runner`` for tests."""

    def __init__(
        self,
        gh_command: str = "gh",
        timeout_seconds: int = 120,
        runner: Runner | None = None,
        transient_retries: int = 1,
        retry_delay_seconds: float = 2.0,
    ) -> None:
        self.gh_command = gh_command
        self.timeout_seconds = timeout_seconds
        self._runner: Runner = runner or execute
        self.transient_retries = max(0, transient_retries)
        self.retry_delay_seconds = retry_delay_seconds

    # -- low level ------------------------------------------------------
    def _run_gh(self, args: list[str], allow_fail: bool = False) -> ExecutionResult:
        attempts = self.transient_retries + 1
        last_error = ""
        for i in range(attempts):
            res = self._runner(
                ExecutionRequest(
                    command=[self.gh_command, *args],
                    timeout_seconds=self.timeout_seconds,
                )
            )
            if res.timed_out:
                last_error = f"`gh {' '.join(args)}` timed out after {self.timeout_seconds}s"
                transient = True
            elif res.exit_code != 0:
                tail = res.stderr.strip()[-1000:]
                last_error = f"`gh {' '.join(args)}` failed (exit {res.exit_code}): {tail}"
                transient = any(m in tail.lower() for m in _TRANSIENT_MARKERS)
            else:
                return res
            if allow_fail and not transient:
                return res
            if not transient or i == attempts - 1:
                break
            time.sleep(self.retry_delay_seconds)
        raise GitHubError(last_error)

    def _json(self, args: list[str]) -> object:
        res = self._run_gh(args)
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"`gh` returned invalid JSON: {exc}") from exc

    def _api_json(self, args: list[str]) -> dict:
        data = self._json(args)
        if not isinstance(data, dict):
            raise GitHubError("`gh` returned a non-object JSON payload")
        return data

    def _api_list(self, args: list[str]) -> list:
        data = self._json(args)
        if not isinstance(data, list):
            raise GitHubError("`gh` returned a non-array JSON payload")
        return data

    # -- environment / doctor --------------------------------------------
    def version(self) -> str:
        res = self._run_gh(["--version"])
        first = (res.stdout or "").strip().splitlines()
        return first[0] if first else ""

    def auth_status(self) -> tuple[bool, str]:
        """(authenticated?, human-readable detail). Never raises for non-auth."""
        try:
            res = self._runner(
                ExecutionRequest(
                    command=[self.gh_command, "auth", "status"],
                    timeout_seconds=self.timeout_seconds,
                )
            )
        except Exception as exc:  # spawn failure etc.
            return False, str(exc)
        text = (res.stdout or "") + (res.stderr or "")
        return res.exit_code == 0, text.strip()

    # -- repository -------------------------------------------------------
    def get_repo(self, repo: str) -> RepoInfo:
        """``repo`` as 'owner/name'."""
        data = self._api_json(["repo", "view", repo, "--json", "nameWithOwner,defaultBranchRef"])
        return RepoInfo(
            name_with_owner=data.get("nameWithOwner", repo),
            default_branch=(data.get("defaultBranchRef") or {}).get("name", ""),
        )

    def current_repo(self) -> RepoInfo:
        """Repository detected from the cwd's git remote (via gh)."""
        data = self._api_json(["repo", "view", "--json", "nameWithOwner,defaultBranchRef"])
        return RepoInfo(
            name_with_owner=data.get("nameWithOwner", ""),
            default_branch=(data.get("defaultBranchRef") or {}).get("name", ""),
        )

    # -- issues -----------------------------------------------------------
    def get_issue(self, url: str) -> IssueInfo:
        ref = parse_issue_url(url)
        data = self._api_json(
            ["issue", "view", ref.canonical, "--json", "url,number,title,state,body"]
        )
        return IssueInfo(
            url=data.get("url", ref.canonical),
            number=int(data.get("number", 0)),
            title=data.get("title", ""),
            state=str(data.get("state", "")).upper(),
            body=data.get("body", "") or "",
            repository=_repo_of(data.get("url", ref.canonical)) or ref.repository,
        )

    def get_issue_state(self, url: str) -> str:
        return self.get_issue(url).state

    def issue_exists(self, url: str) -> bool:
        try:
            self.get_issue(url)
            return True
        except GitHubError:
            return False

    # -- pull requests ------------------------------------------------------
    def _pr_from_data(self, data: dict, fallback_url: str = "") -> PRInfo:
        checks = []
        for c in data.get("statusCheckRollup") or []:
            if isinstance(c, dict):
                checks.append(
                    CheckInfo(
                        name=str(c.get("name", "") or c.get("context", "")),
                        state=str(c.get("status", "") or c.get("state", "")).upper(),
                        conclusion=str(c.get("conclusion", "")).upper(),
                    )
                )
        linked: list[int] = []
        for ref in data.get("closingIssuesReferences") or []:
            if isinstance(ref, dict) and isinstance(ref.get("number"), int):
                linked.append(int(ref["number"]))
        head_repo = ""
        hr = data.get("headRepository") or {}
        hro = data.get("headRepositoryOwner") or {}
        if isinstance(hr, dict) and hr.get("name"):
            owner = hro.get("login", "") if isinstance(hro, dict) else ""
            head_repo = f"{owner}/{hr['name']}" if owner else str(hr["name"])
        url = data.get("url", fallback_url) or fallback_url
        return PRInfo(
            url=url,
            number=int(data.get("number", 0)),
            title=data.get("title", ""),
            state=str(data.get("state", "")).upper(),
            head_sha=(data.get("headRefOid", "") or "").lower(),
            base_ref=data.get("baseRefName", "") or "",
            head_ref=data.get("headRefName", "") or "",
            mergeable=str(data.get("mergeable", "")).upper(),
            is_draft=bool(data.get("isDraft", False)),
            body=data.get("body", "") or "",
            repository=_repo_of(url),
            head_repository=head_repo,
            linked_issue_numbers=linked,
            checks=checks,
        )

    def get_pr(self, url: str) -> PRInfo:
        ref = parse_pr_url(url)
        data = self._api_json(["pr", "view", ref.canonical, "--json", _PR_FIELDS])
        return self._pr_from_data(data, ref.canonical)

    def get_pr_head_sha(self, url: str) -> str:
        return self.get_pr(url).head_sha

    def get_pr_state(self, url: str) -> str:
        return self.get_pr(url).state

    def get_pr_branch(self, url: str) -> str:
        return self.get_pr(url).head_ref

    def get_pr_checks(self, url: str) -> list[CheckInfo]:
        return self.get_pr(url).checks

    def pr_exists(self, url: str) -> bool:
        try:
            self.get_pr(url)
            return True
        except GitHubError:
            return False

    def list_open_prs(self, repo: str, limit: int = 100) -> list[PRInfo]:
        data = self._api_list(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                _PR_LIST_FIELDS,
            ]
        )
        return [self._pr_from_data(d) for d in data if isinstance(d, dict)]

    def find_open_prs_for_issue(self, issue: GitHubIssueRef | str) -> list[PRInfo]:
        """Open PRs that unambiguously belong to ``issue``.

        A PR matches when GitHub links it as closing the issue, or its head
        branch follows the controller's naming scheme ``autoforge/<n>-...``.
        """
        if isinstance(issue, str):
            issue = parse_issue_url(issue)
        prefix = re.compile(rf"^autoforge/{issue.number}(?:-|$)")
        out = []
        for pr in self.list_open_prs(issue.repository):
            if issue.number in pr.linked_issue_numbers or prefix.match(pr.head_ref or ""):
                out.append(pr)
        return out

    # -- merge (the only write; controller-owned, engine-gated) ------------------
    def merge_pr(
        self,
        url: str,
        method: str = "squash",
        match_head_sha: str = "",
        delete_branch: bool = False,
    ) -> None:
        """Run ``gh pr merge`` bound to ``match_head_sha``.

        Raises GitHubError when ``gh`` fails (conflict, branch protection,
        HEAD moved, permissions, ...). A normal return only means ``gh``
        exited 0: callers must re-read the PR and confirm ``state == MERGED``
        before treating the merge as done (merge queues / auto-merge may
        leave the PR open).
        """
        self._run_gh(build_merge_argv(url, method, match_head_sha, delete_branch))

    # -- comments -----------------------------------------------------------
    def get_pr_comments(self, url: str) -> list[CommentInfo]:
        ref = parse_pr_url(url)
        data = self._api_json(["pr", "view", ref.canonical, "--json", "comments"])
        out = []
        for c in data.get("comments") or []:
            if not isinstance(c, dict):
                continue
            curl = str(c.get("url", "") or "")
            cid = 0
            try:
                cid = parse_comment_url(curl).comment_id
            except Exception:
                pass
            author = c.get("author") or {}
            out.append(
                CommentInfo(
                    id=cid,
                    url=curl,
                    body=c.get("body", "") or "",
                    author=str(author.get("login", "")) if isinstance(author, dict) else "",
                    created_at=str(c.get("createdAt", "") or ""),
                    parent_url=ref.canonical,
                )
            )
        return out

    def get_comment(self, comment_url: str) -> CommentInfo:
        """Fetch one issue-style comment by its HTML URL (``#issuecomment-<id>``)."""
        ref: GitHubCommentRef = parse_comment_url(comment_url)
        data = self._api_json(
            ["api", f"repos/{ref.parent.owner}/{ref.parent.repo}/issues/comments/{ref.comment_id}"]
        )
        html_url = str(data.get("html_url", "") or "")
        parent_url = ""
        try:
            parent_url = parse_comment_url(html_url).parent.canonical
        except Exception:
            pass
        user = data.get("user") or {}
        return CommentInfo(
            id=int(data.get("id", ref.comment_id)),
            url=html_url or ref.canonical,
            body=data.get("body", "") or "",
            author=str(user.get("login", "")) if isinstance(user, dict) else "",
            created_at=str(data.get("created_at", "") or ""),
            parent_url=parent_url,
        )


__all__ = [
    "MERGE_METHODS",
    "CheckInfo",
    "CommentInfo",
    "GitHubClient",
    "GitHubIssueRef",
    "GitHubPullRequestRef",
    "IssueInfo",
    "PRInfo",
    "RepoInfo",
    "build_merge_argv",
]
