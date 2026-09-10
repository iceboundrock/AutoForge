"""Shared fixtures/helpers for AutoForge tests.

No test ever invokes a real Claude Code / OpenCode / GitHub write API:
agents are ``ScriptedProvider`` instances, GitHub is ``FakeGitHub``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from autoforge.config import default_config  # noqa: E402
from autoforge.engine import ControllerEngine  # noqa: E402
from autoforge.errors import (  # noqa: E402
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
)
from autoforge.executor import ExecutionResult  # noqa: E402
from autoforge.github import (  # noqa: E402
    CommentInfo,
    IssueInfo,
    MergeQueueStatus,
    PRInfo,
    RepoInfo,
)
from autoforge.providers import ProviderRegistry, ScriptedProvider  # noqa: E402
from autoforge.result_parser import BEGIN, END  # noqa: E402
from autoforge.validation import parse_issue_url  # noqa: E402

EPIC = "https://github.com/owner/repo/issues/1"
ISSUE = "https://github.com/owner/repo/issues/2"
ISSUE3 = "https://github.com/owner/repo/issues/3"
PR = "https://github.com/owner/repo/pull/42"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
BRANCH = "autoforge/2-feature"


def block(payload: str | dict) -> str:
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    return f"some logs...\n{BEGIN}\n{payload}\n{END}\n"


def comment_url(pr_url: str, cid: int) -> str:
    return f"{pr_url}#issuecomment-{cid}"


def review_comment_body(
    round: int, sha: str, needs_fix: bool, finding_ids: list[str] | None = None
) -> str:
    marker = json.dumps(
        {
            "round": round,
            "reviewed_head_sha": sha,
            "needs_fix_round": needs_fix,
            "finding_ids": finding_ids or [],
        }
    )
    return (
        f"# AI Code Review — Round {round}\n\nReviewed HEAD: `{sha}`\n\n"
        "## Findings\n...\n## Spec\n...\n## Standards\n...\n## Assessment\n...\n"
        "## Observations\n...\n## Verification\n...\n## Summary\n"
        f"Needs another fix round: {'YES' if needs_fix else 'NO'}\n"
        f"<!-- ai-review-result: {marker} -->\n"
    )


class FakeAgent:
    """Queued fake *runner* (ExecutionRequest -> ExecutionResult)."""

    def __init__(self, stdout_queue: list[str], exit_code: int = 0):
        self.queue = list(stdout_queue)
        self.exit_code = exit_code
        self.calls: list = []

    def __call__(self, req):
        self.calls.append(req)
        stdout = self.queue.pop(0) if self.queue else ""
        return ExecutionResult(
            command=list(req.command),
            cwd=req.cwd,
            exit_code=self.exit_code,
            stdout=stdout,
            stderr="",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
        )


class FakeGitHub:
    """In-memory GitHub read model. Tests mutate it to simulate agent actions."""

    def __init__(self, repo: str = "owner/repo"):
        self.repo = repo
        self.issues: dict[str, IssueInfo] = {}
        self.prs: dict[str, PRInfo] = {}
        self.comments: dict[str, list[CommentInfo]] = {}
        self.calls: list[tuple] = []
        # Controller-owned merge simulation (see merge_pr).
        self.merges: list[tuple[str, str, str, bool]] = []
        self.merge_error: str = ""  # non-empty -> merge_pr raises GitHubError
        self.merge_leaves_open: bool = False  # gh exits 0 but PR stays OPEN (merge queue)
        self.merge_arms_auto: bool = False  # ... and that call armed auto-merge on the PR
        self.merge_queue: dict[str, MergeQueueStatus] = {}  # per PR; default: no queue
        # non-empty -> get_pr_merge_queue_status raises: a str is a *conclusive*
        # GitHubError, an exception instance is raised as-is (GitHubUnavailableError
        # for a transient failure).
        self.merge_queue_error: str | GitHubError = ""
        self.disable_auto_error: str = ""  # non-empty -> disable_auto_merge raises
        self.disabled_auto: list[str] = []  # PRs on which disable_auto_merge ran
        self.closed_prs: list[tuple[str, str]] = []
        self.reopened_prs: list[tuple[str, str]] = []
        self.reopen_error: str | GitHubError = ""  # as close_error, for reopen_pr
        self.reopen_leaves_closed: bool = False  # gh exits 0 but the PR stays CLOSED
        # Called at the start of close_pr, before the close lands: a mutation
        # racing the destructive write (a push, a body edit, a human close).
        self.close_race = None
        # non-empty -> close_pr raises; an exception instance is raised as-is
        # (GitHubUnavailableError for a transient failure), a str is conclusive.
        self.close_error: str | GitHubError = ""
        self.close_leaves_open: bool = False  # gh exits 0 but the PR stays OPEN
        self.comment_error: str | GitHubError = ""  # non-empty -> comment_pr raises
        # Called at the start of comment_pr: a mutation racing a comment write.
        # The replan's post-close receipt is the interesting one -- it lands
        # between the controller's post-close read and its confirmation.
        self.comment_race = None
        self.commented_prs: list[tuple[str, str]] = []
        self.get_pr_failures: int = 0  # next N get_pr calls raise GitHubUnavailableError
        self.get_pr_error: GitHubError | None = None  # every get_pr call raises this
        self.get_issue_error: GitHubError | None = None  # every get_issue call raises this
        self.latest_pr_error: GitHubError | None = None  # every latest_pr_number call raises this
        self.pr_listing_truncated: bool = False  # a strict PR listing cannot be completed
        self.comments_error: GitHubError | None = None  # every get_pr_comments call raises this
        self.add_issue(EPIC, "EPIC")
        self.add_issue(ISSUE, "Feature")

    # -- test helpers ---------------------------------------------------------
    def add_issue(self, url: str, title: str = "t", state: str = "OPEN") -> IssueInfo:
        ref = parse_issue_url(url)
        info = IssueInfo(
            url=ref.canonical,
            number=ref.number,
            title=title,
            state=state,
            repository=ref.repository,
        )
        self.issues[ref.canonical] = info
        return info

    def add_pr(
        self,
        url: str = PR,
        head_sha: str = SHA_A,
        branch: str = BRANCH,
        state: str = "OPEN",
        linked: list[int] | None = None,
        body: str = "",
        base_ref: str = "main",
    ) -> PRInfo:
        from autoforge.validation import parse_pr_url

        ref = parse_pr_url(url)
        info = PRInfo(
            url=ref.canonical,
            number=ref.number,
            title="PR",
            state=state,
            head_sha=head_sha,
            base_ref=base_ref,
            head_ref=branch,
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            repository=ref.repository,
            linked_issue_numbers=list(linked or []),
            body=body,
        )
        self.prs[ref.canonical] = info
        return info

    def add_comment(self, pr_url: str, cid: int, body: str) -> CommentInfo:
        c = CommentInfo(id=cid, url=comment_url(pr_url, cid), body=body, parent_url=pr_url)
        self.comments.setdefault(pr_url, []).append(c)
        return c

    def set_head(self, sha: str, pr_url: str = PR) -> None:
        self.prs[pr_url].head_sha = sha

    # -- client API --------------------------------------------------------------
    def current_repo(self) -> RepoInfo:
        self.calls.append(("current_repo",))
        return RepoInfo(name_with_owner=self.repo, default_branch="main")

    def get_repo(self, repo: str) -> RepoInfo:
        return RepoInfo(name_with_owner=repo, default_branch="main")

    def get_issue(self, url: str) -> IssueInfo:
        self.calls.append(("get_issue", url))
        if self.get_issue_error is not None:
            raise self.get_issue_error
        ref = parse_issue_url(url)
        # Like GitHub, resolve owner/repo case-insensitively (identity = repo + number).
        for known, info in self.issues.items():
            if parse_issue_url(known).same_target(ref):
                return info
        raise GitHubNotFoundError(f"issue not found: {url}")

    def get_issue_state(self, url: str) -> str:
        return self.get_issue(url).state

    def issue_exists(self, url: str) -> bool:
        try:
            self.get_issue(url)
            return True
        except GitHubError:
            return False

    def _stored(self, url: str) -> PRInfo:
        """The live record, for the fake's own writes -- never handed to a caller."""
        from autoforge.validation import parse_pr_url

        ref = parse_pr_url(url)
        try:
            return self.prs[ref.canonical]
        except KeyError:
            raise GitHubError(f"pr not found: {url}") from None

    def get_pr(self, url: str) -> PRInfo:
        self.calls.append(("get_pr", url))
        if self.get_pr_error is not None:
            raise self.get_pr_error
        if self.get_pr_failures > 0:
            self.get_pr_failures -= 1
            raise GitHubUnavailableError("`gh pr view` failed (exit 1): connection reset")
        # A *snapshot*, exactly as `gh pr view` returns one: a caller holding a
        # PRInfo holds what GitHub said at that moment, and a later change on
        # the server cannot retroactively appear in it. Handing out the live
        # record instead would hide every staleness bug the controller must not
        # have -- a stale checkpoint comparison would silently self-heal.
        return replace(self._stored(url))

    def get_pr_head_sha(self, url: str) -> str:
        return self.get_pr(url).head_sha

    def get_pr_state(self, url: str) -> str:
        return self.get_pr(url).state

    def get_pr_branch(self, url: str) -> str:
        return self.get_pr(url).head_ref

    def get_pr_checks(self, url: str):
        return self.get_pr(url).checks

    def get_pr_merge_queue_status(self, url: str) -> MergeQueueStatus:
        from autoforge.validation import parse_pr_url

        canonical = parse_pr_url(url).canonical
        self.calls.append(("get_pr_merge_queue_status", canonical))
        if isinstance(self.merge_queue_error, GitHubError):
            raise self.merge_queue_error
        if self.merge_queue_error:
            raise GitHubError(self.merge_queue_error)
        return self.merge_queue.get(canonical, MergeQueueStatus(enabled=False, in_queue=False))

    def pr_exists(self, url: str) -> bool:
        try:
            self.get_pr(url)
            return True
        except GitHubError:
            return False

    def list_open_prs(self, repo: str, limit: int = 100, *, strict: bool = False) -> list[PRInfo]:
        self.calls.append(("list_open_prs", repo, strict))
        if strict and self.pr_listing_truncated:
            raise GitHubError(
                f"{repo} has at least 1000 open pull requests, so the listing may be "
                "truncated and the set of candidates cannot be established"
            )
        return [replace(p) for p in self.prs.values() if p.is_open and p.repository == repo]

    def list_all_prs(self, repo: str, *, strict: bool = False) -> list[PRInfo]:
        self.calls.append(("list_all_prs", repo, strict))
        if strict and self.pr_listing_truncated:
            raise GitHubError(
                f"{repo} has at least 1000 pull requests, so the listing may be "
                "truncated and the set of candidates cannot be established"
            )
        return [replace(p) for p in self.prs.values() if p.repository == repo]

    def latest_pr_number(self, repo: str) -> int:
        """Highest PR number in the repo, open or closed (the real watermark)."""
        self.calls.append(("latest_pr_number", repo))
        if self.latest_pr_error is not None:
            raise self.latest_pr_error
        return max((p.number for p in self.prs.values() if p.repository == repo), default=0)

    def find_open_prs_for_issue(self, issue, *, strict: bool = False) -> list[PRInfo]:
        self.calls.append(("find_open_prs_for_issue", issue.number, strict))
        out = []
        for pr in self.list_open_prs(issue.repository, strict=strict):
            if (
                issue.number in pr.linked_issue_numbers
                or pr.head_ref.startswith(f"autoforge/{issue.number}-")
                or pr.head_ref == f"autoforge/{issue.number}"
            ):
                out.append(pr)
        return out

    def get_pr_comments(self, url: str) -> list[CommentInfo]:
        self.calls.append(("get_pr_comments", url))
        if self.comments_error is not None:
            raise self.comments_error
        self.get_pr(url)
        return list(self.comments.get(url, []))

    def get_comment(self, url: str) -> CommentInfo:
        for cs in self.comments.values():
            for c in cs:
                if c.url == url:
                    return c
        raise GitHubError(f"comment not found: {url}")

    def merge_pr(
        self,
        url: str,
        method: str = "squash",
        match_head_sha: str = "",
        delete_branch: bool = False,
    ) -> None:
        """Mimic `gh pr merge --<method> --match-head-commit <sha>` on the fake."""
        from autoforge.validation import parse_pr_url

        canonical = parse_pr_url(url).canonical
        self.calls.append(("merge_pr", canonical, method, match_head_sha, delete_branch))
        self.merges.append((canonical, method, match_head_sha, delete_branch))
        if self.merge_error:
            raise GitHubError(self.merge_error)
        pr = self._stored(canonical)
        if not pr.is_open:
            raise GitHubError(f"`gh pr merge` failed: PR is {pr.state}")
        if match_head_sha and pr.head_sha != match_head_sha:
            raise GitHubError("`gh pr merge` failed: head commit does not match")
        if not self.merge_leaves_open:
            pr.state = "MERGED"
        elif self.merge_arms_auto:
            pr.auto_merge_enabled = True

    def disable_auto_merge(self, url: str) -> None:
        """Mimic `gh pr merge <pr> --disable-auto`."""
        from autoforge.validation import parse_pr_url

        canonical = parse_pr_url(url).canonical
        self.calls.append(("disable_auto_merge", canonical))
        self.disabled_auto.append(canonical)
        if self.disable_auto_error:
            raise GitHubError(self.disable_auto_error)
        self._stored(canonical).auto_merge_enabled = False

    def close_pr(self, url: str, comment: str) -> None:
        from autoforge.validation import parse_pr_url

        canonical = parse_pr_url(url).canonical
        self.calls.append(("close_pr", canonical, comment))
        self.closed_prs.append((canonical, comment))
        # `gh pr close --comment` posts the comment as part of the same
        # invocation, before the close; it must NOT carry the ownership
        # receipt (see GitHubClient.close_pr), which is posted afterwards
        # with `comment_pr` only after the close is observed.
        self.add_comment(canonical, 900_000 + len(self.closed_prs), comment)
        if self.close_race is not None:
            # Landed after the controller's last read, before the close.
            self.close_race(self)
        if isinstance(self.close_error, GitHubError):
            raise self.close_error
        if self.close_error:
            raise GitHubError(self.close_error)
        pr = self._stored(canonical)
        if not pr.is_open:
            raise GitHubError(f"cannot close PR {canonical}: it is {pr.state}")
        if not self.close_leaves_open:
            pr.state = "CLOSED"

    def comment_pr(self, url: str, body: str) -> None:
        from autoforge.validation import parse_pr_url

        canonical = parse_pr_url(url).canonical
        self.calls.append(("comment_pr", canonical, body))
        self.commented_prs.append((canonical, body))
        if isinstance(self.comment_error, GitHubError):
            raise self.comment_error
        if self.comment_error:
            raise GitHubError(self.comment_error)
        self._stored(canonical)  # fails closed on unknown PR, like `gh`
        self.add_comment(canonical, 920_000 + len(self.calls), body)
        if self.comment_race is not None:
            # Landed once the comment is durable: for the replan's post-close
            # receipt, that is after the controller's post-close read and
            # before it confirms the checkpoints.
            self.comment_race(self)

    def reopen_pr(self, url: str, comment: str) -> None:
        from autoforge.validation import parse_pr_url

        canonical = parse_pr_url(url).canonical
        self.calls.append(("reopen_pr", canonical, comment))
        self.reopened_prs.append((canonical, comment))
        self.add_comment(canonical, 910_000 + len(self.reopened_prs), comment)
        if isinstance(self.reopen_error, GitHubError):
            raise self.reopen_error
        if self.reopen_error:
            raise GitHubError(self.reopen_error)
        pr = self._stored(canonical)
        if pr.state == "MERGED":
            raise GitHubError(f"cannot reopen PR {canonical}: it is MERGED")
        if not self.reopen_leaves_closed:
            pr.state = "OPEN"


def scripted_config():
    """Default config with every profile routed to the scripted provider.

    Model/effort routing is preserved so tests can assert on it.
    """
    cfg = default_config()
    for p in cfg.profiles.values():
        p.provider = "scripted"
    return cfg


def git_repo(path) -> Path:
    """Make ``path`` a git repository (``git init``) unless it already is one.

    The controller lock is keyed by the repository containing the engine's
    working directory, so every engine / CLI test that may take the lock
    needs a real (empty) repository; ``git init`` costs a few milliseconds.
    """
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def make_engine(
    state_dir,
    script=None,
    github: FakeGitHub | None = None,
    cfg=None,
    exit_code: int = 0,
    workdir=None,
):
    """Engine wired to a ScriptedProvider (agents) and FakeGitHub (verification).

    ``workdir`` defaults to the parent of ``state_dir`` (the test's tmp_path),
    made a git repository so the engine can derive its repository lock.
    """
    cfg = cfg or default_config()
    if workdir is None:
        workdir = git_repo(Path(state_dir).parent)
    provider = ScriptedProvider(script, exit_code=exit_code)
    registry = ProviderRegistry(overrides={"claude": provider, "opencode": provider})
    gh = github or FakeGitHub()
    eng = ControllerEngine(
        config=cfg, state_dir=state_dir, workdir=workdir, github=gh, providers=registry
    )
    eng.new_run(EPIC, ISSUE)
    eng.provider = provider  # type: ignore[attr-defined]
    return eng


@pytest.fixture
def tmp_state_dir(tmp_path):
    return tmp_path / ".autoforge"


@pytest.fixture
def repo(tmp_path) -> Path:
    """``tmp_path`` as a git repository (the checkout the controller locks)."""
    return git_repo(tmp_path)


@pytest.fixture
def fake_github():
    return FakeGitHub()


@pytest.fixture
def engine(tmp_state_dir, fake_github):
    return make_engine(tmp_state_dir, [], github=fake_github)
