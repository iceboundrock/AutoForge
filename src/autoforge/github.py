"""GitHub abstraction over the `gh` CLI (verification + controller-owned merge).

Business logic must use these typed objects, never parse raw `gh` JSON
inline. Agents perform content writes (branches, PRs, comments, issues)
under controller prompts, and the controller only *verifies* what they
claim through this client. Controller-owned exceptions are
:meth:`GitHubClient.merge_pr`, :meth:`GitHubClient.disable_auto_merge`, and
:meth:`GitHubClient.close_pr` for a verified replacement lifecycle. Merging
is always behind the safety gate and bound to the reviewed HEAD via
``--match-head-commit``; closing a superseded PR never deletes its branch.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import (
    ConfigurationError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
)
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

# Transient `gh` failures: the same read may well succeed later, so the engine
# treats them as *inconclusive* (bounded re-checks) instead of conclusive.
# Phrase markers cover network-level errors (as the Go runtime behind `gh`
# reports them: ``dial tcp ...: connect: network is unreachable``, ``lookup
# api.github.com: temporary failure in name resolution``, ...) and the reason
# phrases GitHub attaches to server-side / throttling statuses; the HTTP
# status itself is matched as a whole class (every 5xx, plus 429) rather than
# an enumerated list, so e.g. ``HTTP 500`` is not silently conclusive. `gh`
# prints the status as ``HTTP 502: Bad Gateway`` (GraphQL) or ``gh: Bad
# Gateway (HTTP 502)`` (REST); both shapes are matched, bare numbers elsewhere
# in the message (PR numbers, SHAs) are not.
_TRANSIENT_MARKERS = (
    # -- connection / socket level (OS errno strings as surfaced by Go's net package)
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "connection aborted",
    "broken pipe",
    "unexpected eof",
    "network is unreachable",
    "network is down",
    "no route to host",
    "host is down",
    # Every `dial tcp ...` error is a failure to *open* the connection, never an
    # answer from GitHub, so the whole family is transient even when the
    # trailing errno phrase is one not listed here.
    "dial tcp",
    "temporarily unavailable",
    "tls handshake",
    # -- name resolution (Go resolver / glibc phrases)
    "no such host",
    "temporary failure in name resolution",
    "server misbehaving",
    # `gh` wraps any connection-level error (whatever host is configured) as
    # ``error connecting to <host>\ncheck your internet connection or ...``.
    "error connecting to",
    # -- GitHub-side throttling / server errors
    "rate limit",
    "too many requests",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "gateway time-out",
)
_TRANSIENT_HTTP_STATUS = re.compile(r"\bhttp\s+(?:5\d\d|429)\b")


def is_transient_gh_failure(stderr: str) -> bool:
    """Whether a failed `gh` invocation's stderr describes a transient failure."""
    text = stderr.lower()
    return any(m in text for m in _TRANSIENT_MARKERS) or bool(_TRANSIENT_HTTP_STATUS.search(text))


# Conclusive "does not exist" answers. GraphQL (`gh issue view`, `gh pr view`)
# says ``Could not resolve to an Issue with the number of 999.``; REST says
# ``HTTP 404: Not Found``; `gh pr` commands say ``could not find pull request``.
# A private object the token cannot see is reported the same way by GitHub.
_NOT_FOUND_MARKERS = (
    "could not resolve to a",
    "could not find",
)
_NOT_FOUND_HTTP_STATUS = re.compile(r"\bhttp\s+404\b")


def is_not_found_gh_failure(stderr: str) -> bool:
    """Whether a failed `gh` invocation's stderr says the object does not exist."""
    text = stderr.lower()
    return any(m in text for m in _NOT_FOUND_MARKERS) or bool(_NOT_FOUND_HTTP_STATUS.search(text))


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


_CHECK_OK_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_CHECK_PENDING_STATES = frozenset(
    {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}
)


@dataclass
class CheckInfo:
    name: str
    state: str  # CheckRun: COMPLETED | IN_PROGRESS | ... ; StatusContext: SUCCESS | PENDING | ...
    conclusion: str = ""  # CheckRun only: SUCCESS | FAILURE | ...

    @property
    def outcome(self) -> str:
        """``success`` | ``pending`` | ``failure`` | ``unknown`` (fail closed).

        Covers both GitHub check runs (``status``/``conclusion``) and legacy
        commit statuses (``state`` only). Anything unrecognised is ``unknown``
        and callers must treat it as not passing.
        """
        state = (self.state or "").upper()
        conclusion = (self.conclusion or "").upper()
        if state == "COMPLETED":
            if conclusion in _CHECK_OK_CONCLUSIONS:
                return "success"
            return "failure" if conclusion else "unknown"
        if state in _CHECK_PENDING_STATES:
            return "pending"
        if state == "SUCCESS" and not conclusion:
            return "success"
        if state in ("FAILURE", "ERROR") and not conclusion:
            return "failure"
        return "unknown"


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
    # CLEAN | HAS_HOOKS | UNSTABLE | BLOCKED | BEHIND | DIRTY | DRAFT | UNKNOWN
    merge_state_status: str = ""
    auto_merge_enabled: bool = False  # GitHub auto-merge is armed on this PR
    is_draft: bool = False
    body: str = ""
    repository: str = ""
    head_repository: str = ""
    linked_issue_numbers: list[int] = field(default_factory=list)
    checks: list[CheckInfo] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"


@dataclass(frozen=True)
class MergeQueueStatus:
    """Merge-queue facts for a PR (GraphQL only; not exposed by ``gh pr view``)."""

    enabled: bool  # the base branch requires a merge queue
    in_queue: bool  # the PR is currently enqueued


Runner = Callable[[ExecutionRequest], ExecutionResult]

_PR_FIELDS = (
    "url,number,title,state,headRefOid,baseRefName,headRefName,mergeable,mergeStateStatus,"
    "autoMergeRequest,isDraft,body,headRepository,headRepositoryOwner,"
    "closingIssuesReferences,statusCheckRollup"
)
_MERGE_QUEUE_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequest(number: $number) { isMergeQueueEnabled isInMergeQueue } } }"
)
# The highest PR number that exists in a repository right now. GitHub allocates
# issue/PR numbers from one monotonic per-repository counter at creation time,
# so the newest-created PR carries the largest number, and *every* PR created
# later carries a larger one. That makes a single-node read a complete
# "existed before now" watermark -- no pagination, and unlike a listing it also
# covers closed and unlinked PRs.
_LATEST_PR_QUERY = (
    "query($owner: String!, $name: String!) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequests(first: 1, orderBy: {field: CREATED_AT, direction: DESC})"
    " { nodes { number } } } }"
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


# `gh pr list` paginates internally to satisfy --limit; this is the ceiling a
# strict caller is willing to read before declaring the set unknowable.
STRICT_PR_LIST_LIMIT = 1000

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


def build_disable_auto_merge_argv(pr_url: str) -> list[str]:
    """``gh pr merge <pr> --disable-auto``: disarm GitHub auto-merge on a PR."""
    return ["pr", "merge", parse_pr_url(pr_url).canonical, "--disable-auto"]


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
        transient = False
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
                transient = is_transient_gh_failure(tail)
            else:
                return res
            if allow_fail and not transient:
                return res
            if not transient or i == attempts - 1:
                break
            time.sleep(self.retry_delay_seconds)
        if transient:
            raise GitHubUnavailableError(last_error)
        if is_not_found_gh_failure(last_error):
            raise GitHubNotFoundError(last_error)
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
            mergeable=str(data.get("mergeable", "") or "").upper(),
            merge_state_status=str(data.get("mergeStateStatus", "") or "").upper(),
            auto_merge_enabled=data.get("autoMergeRequest") is not None,
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

    def get_pr_merge_queue_status(self, url: str) -> MergeQueueStatus:
        """Whether the PR's base branch requires a merge queue / the PR is enqueued.

        ``gh pr merge`` silently switches to auto-merge or queue insertion on
        such branches, so the engine must know this *before* writing. Missing
        or non-boolean data raises GitHubError (fail closed).
        """
        ref = parse_pr_url(url)
        owner, _, name = ref.repository.partition("/")
        data = self._api_json(
            [
                "api",
                "graphql",
                "-f",
                f"query={_MERGE_QUEUE_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={ref.number}",
            ]
        )
        pr = ((data.get("data") or {}).get("repository") or {}).get("pullRequest")
        if not isinstance(pr, dict):
            raise GitHubError(f"merge-queue status for {ref.canonical} unavailable: {data}")
        enabled = pr.get("isMergeQueueEnabled")
        in_queue = pr.get("isInMergeQueue")
        if not isinstance(enabled, bool) or not isinstance(in_queue, bool):
            raise GitHubError(f"merge-queue status for {ref.canonical} is not boolean: {pr}")
        return MergeQueueStatus(enabled=enabled, in_queue=in_queue)

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

    def latest_pr_number(self, repo: str) -> int:
        """The largest PR number that currently exists in ``repo`` (0 if none).

        Used as a provenance watermark: a PR whose number is <= the value read
        before a replan transaction id was generated cannot have been created
        after it, so it can never be that transaction's replacement even if the
        marker is later copied into its body. Malformed data raises
        GitHubError (fail closed) rather than returning an under-estimate.
        """
        owner, _, name = repo.partition("/")
        data = self._api_json(
            [
                "api",
                "graphql",
                "-f",
                f"query={_LATEST_PR_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
            ]
        )
        connection = ((data.get("data") or {}).get("repository") or {}).get("pullRequests")
        if not isinstance(connection, dict):
            raise GitHubError(f"latest pull-request number for {repo} unavailable: {data}")
        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise GitHubError(f"latest pull-request number for {repo} is not a node list: {nodes}")
        if not nodes:
            return 0
        number = nodes[0].get("number") if isinstance(nodes[0], dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise GitHubError(f"latest pull-request number for {repo} is not a number: {nodes[0]}")
        return number

    def find_open_prs_for_issue(
        self, issue: GitHubIssueRef | str, *, strict: bool = False
    ) -> list[PRInfo]:
        """Open PRs that unambiguously belong to ``issue``.

        A PR matches when GitHub links it as closing the issue, or its head
        branch follows the controller's naming scheme ``autoforge/<n>-...``.

        ``strict`` raises GitHubError instead of returning a set that may be
        incomplete: the underlying listing is bounded, and a caller deciding a
        destructive action on "no candidate exists" must not confuse that with
        "the candidate was past the limit".
        """
        if isinstance(issue, str):
            issue = parse_issue_url(issue)
        limit = STRICT_PR_LIST_LIMIT if strict else 100
        open_prs = self.list_open_prs(issue.repository, limit=limit)
        if strict and len(open_prs) >= limit:
            raise GitHubError(
                f"{issue.repository} has at least {limit} open pull requests, so the listing may "
                "be truncated and the set of candidates cannot be established"
            )
        prefix = re.compile(rf"^autoforge/{issue.number}(?:-|$)")
        out = []
        for pr in open_prs:
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

    def disable_auto_merge(self, url: str) -> None:
        """Disarm GitHub auto-merge on ``url`` (``gh pr merge --disable-auto``).

        Used only to undo an auto-merge that a controller ``merge_pr`` call
        left armed, so no unreviewed HEAD can merge later on its own.
        """
        self._run_gh(build_disable_auto_merge_argv(url))

    def close_pr(self, url: str, comment: str) -> None:
        """Close an obsolete PR without merging or deleting any branch."""
        ref = parse_pr_url(url)
        self._run_gh(["pr", "close", ref.canonical, "--comment", comment])

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
    "MergeQueueStatus",
    "PRInfo",
    "RepoInfo",
    "build_disable_auto_merge_argv",
    "build_merge_argv",
]
