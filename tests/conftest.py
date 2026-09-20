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
    ChangedFile,
    ChangedFiles,
    CheckInfo,
    CommentInfo,
    IssueInfo,
    MergeQueueStatus,
    PRInfo,
    RepoInfo,
    WorkflowJob,
    WorkflowRunInfo,
    WorkflowRunJobs,
    WorkflowRuns,
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
# The base branch's tip and the two GitHub Actions runs the check-definition
# gate reads: the PR's own `pull_request` run (the one its `ci` check names)
# and the base branch's `push` run at its tip (the reference definition).
MAIN_SHA = "e" * 40
# The merge base the fake reports for any (base branch, HEAD) unless a test
# rewrites the base (`FakeGitHub.merge_base`) or pins one pair
# (`FakeGitHub.merge_bases`): the commit the PR diff is computed from (#96).
MERGE_BASE = "d" * 40
MERGE_BASE_B = "f" * 40
CI_WORKFLOW_ID = 77
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
BASE_RUN_ID = 1000
CI_RUN_ID = 1001


def ci_check(
    name: str = "ci",
    state: str = "COMPLETED",
    conclusion: str = "SUCCESS",
    run_id: int = CI_RUN_ID,
    repo: str = "owner/repo",
) -> CheckInfo:
    """A check run as `gh pr view --json statusCheckRollup` reports an Actions job."""
    return CheckInfo(
        name=name,
        state=state,
        conclusion=conclusion,
        details_url=f"https://github.com/{repo}/actions/runs/{run_id}/job/{run_id * 10}",
    )


def ci_jobs() -> WorkflowRunJobs:
    """The job/step structure of one run of the default fake workflow."""
    return WorkflowRunJobs(
        jobs=(
            WorkflowJob(
                "lint", ("Set up job", "Run actions/checkout@v7", "Run ruff", "Complete job")
            ),
            WorkflowJob("test (3.12)", ("Set up job", "Run actions/checkout@v7", "Run pytest")),
            WorkflowJob("test (3.11)", ("Set up job", "Run actions/checkout@v7", "Run pytest")),
            WorkflowJob("ci", ("Set up job", "Run true", "Complete job")),
        ),
        total=4,
    )


def workflow_run(
    run_id: int,
    head_sha: str,
    *,
    event: str = "pull_request",
    head_branch: str = BRANCH,
    status: str = "completed",
    conclusion: str = "success",
    repo: str = "owner/repo",
    workflow_id: int = CI_WORKFLOW_ID,
    path: str = CI_WORKFLOW_PATH,
) -> WorkflowRunInfo:
    return WorkflowRunInfo(
        id=run_id,
        repository=repo,
        path=path,
        workflow_id=workflow_id,
        event=event,
        head_sha=head_sha,
        head_branch=head_branch,
        status=status,
        conclusion=conclusion,
    )


def block(payload: str | dict) -> str:
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    return f"some logs...\n{BEGIN}\n{payload}\n{END}\n"


def comment_url(pr_url: str, cid: int) -> str:
    return f"{pr_url}#issuecomment-{cid}"


def progress_comment_body(issue_url: str = ISSUE, pr_url: str = PR) -> str:
    """An UPDATE_EPIC progress comment carrying the (issue, PR) marker."""
    from autoforge.engine import render_progress_marker

    return (
        f"Progress: {issue_url} done via {pr_url}\n\n{render_progress_marker(issue_url, pr_url)}\n"
    )


def post_progress_comment(gh: FakeGitHub, cid: int = 300) -> None:
    """What a well-behaved UPDATE_EPIC agent does: post the progress comment once.

    An agent asked again (a rejected selection, a correction) adopts the one
    it already posted, so a second call posts nothing.
    """
    from autoforge.engine import render_progress_marker

    if any(render_progress_marker(ISSUE, PR) in c.body for c in gh.comments.get(EPIC, [])):
        return
    gh.add_comment(EPIC, cid, progress_comment_body())


def implementation_pr_body(issue_url: str = ISSUE) -> str:
    """A PR body carrying the issue's implementation marker (what the agent must write)."""
    from autoforge.engine import render_implementation_marker

    return f"Closes {issue_url}\n\n{render_implementation_marker(issue_url)}\n"


def follow_up_issue_body(finding_id: str, pr_url: str = PR) -> str:
    """A follow-up issue body carrying the (PR, finding) marker."""
    from autoforge.engine import render_follow_up_marker

    return (
        f"Follow-up for {finding_id} of {pr_url}\n\n{render_follow_up_marker(pr_url, finding_id)}\n"
    )


def review_comment_body(
    round: int,
    sha: str,
    needs_fix: bool,
    finding_ids: list[str] | None = None,
    *,
    base_ref: str | None = "main",
    merge_base_sha: str | None = MERGE_BASE,
) -> str:
    """A well-formed round comment.

    ``base_ref=None`` writes a pre-base (protocol-2) marker and
    ``merge_base_sha=None`` a pre-merge-base (protocol-3) one.
    """
    payload: dict[str, object] = {
        "round": round,
        "reviewed_head_sha": sha,
        "needs_fix_round": needs_fix,
        "finding_ids": finding_ids or [],
    }
    if base_ref is not None:
        payload["reviewed_base_ref"] = base_ref
    if merge_base_sha is not None:
        payload["reviewed_merge_base_sha"] = merge_base_sha
    marker = json.dumps(payload)
    return (
        f"# AI Code Review — Round {round}\n\nReviewed HEAD: `{sha}` against base `{base_ref}` "
        f"(merge base `{merge_base_sha}`)\n\n"
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
        # Changed-file listing per PR (default: no files, listing complete).
        # Entries are paths, or a ChangedFile to model a rename's two ends.
        # ``changed_files_total`` overrides GitHub's own count to simulate a
        # truncated first page. Same error convention as merge_queue_error.
        self.changed_files: dict[str, list[str | ChangedFile]] = {}
        self.changed_files_total: dict[str, int] = {}
        self.changed_files_error: str | GitHubError = ""
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
        # get_issue calls for these URLs (canonical) raise; others answer.
        self.get_issue_errors: dict[str, GitHubError] = {}
        # Controller-owned EPIC body write (edit_issue_body): every write is
        # recorded as (canonical url, body). ``edit_issue_error`` raises like
        # close_error; ``edit_issue_race`` is called before the write lands (a
        # human edit racing the splice); ``edit_issue_leaves_body`` makes gh
        # exit 0 without the body changing.
        self.edited_issues: list[tuple[str, str]] = []
        self.edit_issue_error: str | GitHubError = ""
        self.edit_issue_race = None
        self.edit_issue_leaves_body: bool = False
        self.latest_pr_error: GitHubError | None = None  # every latest_pr_number call raises this
        # The open-PR cursor walk cannot reach its end (issue #20): the real
        # client raises instead of returning the pages it did read.
        self.open_pr_listing_incomplete: bool = False
        self.pr_listing_truncated: bool = False  # a strict all-states PR listing hit its ceiling
        self.issue_listing_truncated: bool = False  # a strict issue listing cannot be completed
        # every get_pr_comments / get_issue_comments call raises this
        self.comments_error: GitHubError | None = None
        self.get_comment_error: GitHubError | None = None  # every get_comment call raises this
        # GitHub Actions read model behind `safety.verify_check_definition`:
        # runs by id, their jobs, and where each branch points. `add_pr`
        # registers the PR's own run at its HEAD; the base branch has one
        # successful push run at its tip with the same structure.
        self.workflow_runs: dict[int, WorkflowRunInfo] = {
            BASE_RUN_ID: workflow_run(BASE_RUN_ID, MAIN_SHA, event="push", head_branch="main")
        }
        self.workflow_jobs: dict[int, WorkflowRunJobs] = {BASE_RUN_ID: ci_jobs()}
        self.branch_heads: dict[str, str] = {"main": MAIN_SHA}
        # The merge base of any base branch and HEAD (#96). A base rewritten
        # under its name is simulated by changing ``merge_base``; a pinned
        # ``merge_bases[(base_ref, head_sha)]`` answers one pair only.
        # ``merge_base_error`` raises on every read (same convention as
        # get_pr_error).
        self.merge_base: str = MERGE_BASE
        self.merge_bases: dict[tuple[str, str], str] = {}
        self.merge_base_error: GitHubError | None = None
        # GitHub claims this many more runs than find_workflow_runs lists: a
        # listing the controller could not complete.
        self.workflow_runs_unlisted: int = 0
        # non-empty -> every Actions read raises (same convention as merge_queue_error)
        self.actions_error: str | GitHubError = ""
        self.add_issue(EPIC, "EPIC")
        self.add_issue(ISSUE, "Feature")

    # -- test helpers ---------------------------------------------------------
    def add_issue(
        self, url: str, title: str = "t", state: str = "OPEN", body: str = ""
    ) -> IssueInfo:
        ref = parse_issue_url(url)
        info = IssueInfo(
            url=ref.canonical,
            number=ref.number,
            title=title,
            state=state,
            body=body,
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
        checks: list[CheckInfo] | None = None,
    ) -> PRInfo:
        from autoforge.validation import parse_pr_url

        ref = parse_pr_url(url)
        # The PR's own run is at its HEAD: a green `ci` names it, and the
        # check-definition gate reads it back.
        self.workflow_runs[CI_RUN_ID] = workflow_run(CI_RUN_ID, head_sha, head_branch=branch)
        self.workflow_jobs.setdefault(CI_RUN_ID, ci_jobs())
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
            checks=[ci_check()] if checks is None else list(checks),
        )
        self.prs[ref.canonical] = info
        return info

    def add_comment(self, pr_url: str, cid: int, body: str) -> CommentInfo:
        c = CommentInfo(id=cid, url=comment_url(pr_url, cid), body=body, parent_url=pr_url)
        self.comments.setdefault(pr_url, []).append(c)
        return c

    def set_head(self, sha: str, pr_url: str = PR) -> None:
        """A push to the PR: new HEAD, and a new run of its checks at that HEAD."""
        self.prs[pr_url].head_sha = sha
        self.workflow_runs[CI_RUN_ID] = replace(self.workflow_runs[CI_RUN_ID], head_sha=sha)

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
        if ref.canonical in self.get_issue_errors:
            raise self.get_issue_errors[ref.canonical]
        # Like GitHub, resolve owner/repo case-insensitively (identity = repo + number).
        for known, info in self.issues.items():
            if parse_issue_url(known).same_target(ref):
                return info
        raise GitHubNotFoundError(f"issue not found: {url}")

    def get_issue_state(self, url: str) -> str:
        return self.get_issue(url).state

    def edit_issue_body(self, url: str, body: str) -> None:
        canonical = parse_issue_url(url).canonical
        self.calls.append(("edit_issue_body", canonical, body))
        self.edited_issues.append((canonical, body))
        if self.edit_issue_race is not None:
            self.edit_issue_race(self)
        if isinstance(self.edit_issue_error, GitHubError):
            raise self.edit_issue_error
        if self.edit_issue_error:
            raise GitHubError(self.edit_issue_error)
        issue = self.get_issue(canonical)  # fails closed on an unknown issue, like `gh`
        if not self.edit_issue_leaves_body:
            issue.body = body

    def list_open_issues(self, repo: str, *, strict: bool = False) -> list[IssueInfo]:
        self.calls.append(("list_open_issues", repo, strict))
        if strict and self.issue_listing_truncated:
            raise GitHubError(
                f"{repo} has at least 1000 open issues, so the listing may be truncated "
                "and the set of follow-up issues cannot be established"
            )
        return [
            replace(i)
            for i in self.issues.values()
            if i.is_open and self._same_repo(i.repository, repo)
        ]

    def issue_exists(self, url: str) -> bool:
        try:
            self.get_issue(url)
            return True
        except GitHubError:
            return False

    def _stored(self, url: str) -> PRInfo:
        """The live record, for the fake's own writes -- never handed to a caller.

        Resolved as GitHub resolves a URL: owner/repo case-insensitively, then
        the number. The record keeps the spelling it was registered under, so
        a caller asking for ``owner/repo/pull/42`` reads back whatever spelling
        the fake's GitHub holds, exactly as ``gh pr view --json url`` does.
        """
        from autoforge.validation import parse_pr_url

        ref = parse_pr_url(url)
        for known, info in self.prs.items():
            if parse_pr_url(known).same_target(ref):
                return info
        raise GitHubError(f"pr not found: {url}")

    def _same_repo(self, repository: str, repo: str) -> bool:
        return repository.lower() == repo.lower()

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

    def get_pr_changed_files(self, url: str) -> ChangedFiles:
        from autoforge.validation import parse_pr_url

        canonical = parse_pr_url(url).canonical
        self.calls.append(("get_pr_changed_files", canonical))
        if isinstance(self.changed_files_error, GitHubError):
            raise self.changed_files_error
        if self.changed_files_error:
            raise GitHubError(self.changed_files_error)
        files = tuple(
            entry if isinstance(entry, ChangedFile) else ChangedFile(path=entry)
            for entry in self.changed_files.get(canonical, [])
        )
        return ChangedFiles(files=files, total=self.changed_files_total.get(canonical, len(files)))

    def get_pr_merge_queue_status(self, url: str) -> MergeQueueStatus:
        from autoforge.validation import parse_pr_url

        canonical = parse_pr_url(url).canonical
        self.calls.append(("get_pr_merge_queue_status", canonical))
        if isinstance(self.merge_queue_error, GitHubError):
            raise self.merge_queue_error
        if self.merge_queue_error:
            raise GitHubError(self.merge_queue_error)
        return self.merge_queue.get(canonical, MergeQueueStatus(enabled=False, in_queue=False))

    def _actions_failure(self) -> None:
        if isinstance(self.actions_error, GitHubError):
            raise self.actions_error
        if self.actions_error:
            raise GitHubError(self.actions_error)

    def get_workflow_run(self, repository: str, run_id: int) -> WorkflowRunInfo:
        self.calls.append(("get_workflow_run", repository, run_id))
        self._actions_failure()
        run = self.workflow_runs.get(run_id)
        if run is None or run.repository.lower() != repository.lower():
            raise GitHubNotFoundError(f"workflow run not found: {repository} {run_id}")
        return run

    def get_workflow_run_jobs(self, repository: str, run_id: int) -> WorkflowRunJobs:
        self.calls.append(("get_workflow_run_jobs", repository, run_id))
        self._actions_failure()
        self.get_workflow_run(repository, run_id)
        self.calls.pop()
        return self.workflow_jobs[run_id]

    def get_branch_head_sha(self, repository: str, branch: str) -> str:
        self.calls.append(("get_branch_head_sha", repository, branch))
        self._actions_failure()
        try:
            return self.branch_heads[branch]
        except KeyError:
            raise GitHubNotFoundError(f"branch not found: {branch}") from None

    def get_merge_base_sha(self, repository: str, base_ref: str, head_sha: str) -> str:
        self.calls.append(("get_merge_base_sha", repository, base_ref, head_sha))
        if self.merge_base_error is not None:
            raise self.merge_base_error
        return self.merge_bases.get((base_ref, head_sha.lower()), self.merge_base)

    def find_workflow_runs(
        self, repository: str, workflow_id: int, *, branch: str, event: str, head_sha: str
    ) -> WorkflowRuns:
        self.calls.append(("find_workflow_runs", repository, workflow_id, branch, event, head_sha))
        self._actions_failure()
        runs = tuple(
            run
            for run in self.workflow_runs.values()
            if run.repository.lower() == repository.lower()
            and run.workflow_id == workflow_id
            and run.head_branch == branch
            and run.event == event
            and run.head_sha == head_sha
        )
        return WorkflowRuns(runs=runs, total=len(runs) + self.workflow_runs_unlisted)

    def pr_exists(self, url: str) -> bool:
        try:
            self.get_pr(url)
            return True
        except GitHubError:
            return False

    def list_open_prs(self, repo: str) -> list[PRInfo]:
        self.calls.append(("list_open_prs", repo))
        if self.open_pr_listing_incomplete:
            raise GitHubError(
                f"open PR listing of {repo} cannot be read to its end: page 2 announces a "
                "next page but no cursor that reaches it (None)"
            )
        return [
            replace(p)
            for p in self.prs.values()
            if p.is_open and self._same_repo(p.repository, repo)
        ]

    def list_all_prs(self, repo: str, *, strict: bool = False) -> list[PRInfo]:
        self.calls.append(("list_all_prs", repo, strict))
        if strict and self.pr_listing_truncated:
            raise GitHubError(
                f"{repo} has at least 1000 pull requests, so the listing may be "
                "truncated and the set of candidates cannot be established"
            )
        return [replace(p) for p in self.prs.values() if self._same_repo(p.repository, repo)]

    def latest_pr_number(self, repo: str) -> int:
        """Highest PR number in the repo, open or closed (the real watermark)."""
        self.calls.append(("latest_pr_number", repo))
        if self.latest_pr_error is not None:
            raise self.latest_pr_error
        return max(
            (p.number for p in self.prs.values() if self._same_repo(p.repository, repo)), default=0
        )

    def get_pr_comments(self, url: str) -> list[CommentInfo]:
        self.calls.append(("get_pr_comments", url))
        if self.comments_error is not None:
            raise self.comments_error
        from autoforge.validation import parse_pr_url

        self.get_pr(url)
        return self._comments_of(parse_pr_url(url))

    def get_issue_comments(self, url: str) -> list[CommentInfo]:
        self.calls.append(("get_issue_comments", url))
        if self.comments_error is not None:
            raise self.comments_error
        return self._comments_of(parse_issue_url(url))

    def _comments_of(self, ref) -> list[CommentInfo]:
        from autoforge.validation import parse_github_url

        # Comments are keyed by the URL they were posted under; the PR or
        # issue they belong to is the same whatever spelling that URL used.
        return [
            c
            for known, cs in self.comments.items()
            if parse_github_url(known).same_target(ref)
            for c in cs
        ]

    def get_comment(self, url: str) -> CommentInfo:
        """One comment by id, as the REST API answers: whatever parent it is on.

        The real client addresses the comment by its id alone, so a URL
        naming the wrong parent still returns the comment with GitHub's own
        URL; the engine compares. A comment nobody posted is a conclusive
        GitHubNotFoundError, like a 404.
        """
        from autoforge.validation import parse_comment_url

        self.calls.append(("get_comment", url))
        if self.get_comment_error is not None:
            raise self.get_comment_error
        wanted = parse_comment_url(url)
        for cs in self.comments.values():
            for c in cs:
                posted = parse_comment_url(c.url)
                if posted.comment_id == wanted.comment_id and posted.parent.same_repository(
                    wanted.parent
                ):
                    return replace(c)
        raise GitHubNotFoundError(f"`gh api` failed (exit 1): comment not found: {url}")

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
    """Make ``path`` a git repository with one commit unless it already is one.

    The controller lock is keyed by the repository containing the engine's
    working directory, so every engine / CLI test that may take the lock
    needs a real repository, and a REMOTE agent launch adds a per-issue
    worktree detached at HEAD, which needs a commit to start from; ``git
    init`` plus an empty commit costs a few milliseconds.
    """
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=AutoForge tests",
                "-c",
                "user.email=tests@example.invalid",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "initial",
            ],
            check=True,
        )
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


# -- LOCAL mode -----------------------------------------------------------------
class ExplodingGitHub:
    """A GitHub client that fails the test on any use.

    A LOCAL run must make zero GitHub calls, so the strongest available
    assertion is to hand the engine a client that cannot be touched at all.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"LOCAL mode touched GitHub: {name!r}")


FEATURE_MD = """# Feature: Add Transaction Filter

## Problem

Users cannot filter transactions.

## Requirements

- [ ] Add a `--since` filter.

## Acceptance Criteria

- [ ] Filtering by date returns only later transactions.

## Non-goals

- Pagination.

## Notes / Decisions

None.
"""


def sample_contract(
    *,
    repository_root: str = "/repo",
    state_root: str = "/repo/.git/autoforge/state",
    exclude: tuple[str, ...] = (),
    max_entries: int = 50_000,
    max_bytes: int = 512 * 1024 * 1024,
    validation_commands: tuple[tuple[str, ...], ...] = (),
    max_fix_rounds: int = 1,
    max_total_steps: int = 300,
    prompt_version: str = "v1",
) -> dict:
    """A well-formed persisted LOCAL run contract (see ``autoforge.run_contract``)."""
    from autoforge.local_workspace import SNAPSHOT_TAG
    from autoforge.run_contract import LocalRunContract, WorkspacePolicy

    return LocalRunContract(
        repository_root=repository_root,
        state_root=state_root,
        workspace_policy=WorkspacePolicy(
            exclude=exclude,
            max_entries=max_entries,
            max_bytes=max_bytes,
            snapshot_tag=SNAPSHOT_TAG,
        ),
        validation_commands=validation_commands,
        max_fix_rounds=max_fix_rounds,
        max_total_steps=max_total_steps,
        prompt_version=prompt_version,
    ).to_dict()


def write_feature(repo, name: str = "add-filter", body: str = FEATURE_MD) -> Path:
    """Write ``features/<name>.md`` inside ``repo`` and return the path."""
    path = Path(repo) / "features" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def commit_all(repo, message: str = "wip") -> None:
    """Commit everything in ``repo`` (tests need a clean baseline tree)."""
    root = Path(repo)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
    )


def make_local_engine(
    workdir,
    feature,
    script=None,
    cfg=None,
    exit_code: int = 0,
    state_dir=None,
    allow_dirty: bool = False,
    start: bool = True,
):
    """Engine wired for a LOCAL run: scripted agents, exploding GitHub.

    No ``runner`` is injected: ``LocalWorkspace`` must run real ``git`` against
    the real temporary repository, which is the whole point of the local trust
    boundary.

    ``state_dir`` is left unset by default so the engine resolves it the way
    the CLI does -- ``<git dir>/autoforge/state``, outside the reviewed tree.
    Tests that exercise state-dir placement pass one explicitly.
    """
    cfg = cfg or default_config()
    provider = ScriptedProvider(script, exit_code=exit_code)
    registry = ProviderRegistry(
        overrides={"claude": provider, "opencode": provider, "scripted": provider}
    )
    eng = ControllerEngine(
        config=cfg,
        state_dir=state_dir,
        workdir=workdir,
        github=ExplodingGitHub(),  # type: ignore[arg-type]
        providers=registry,
    )
    eng.bind_local_state_dir(state_dir)
    if start:
        eng.new_local_run(feature, allow_dirty=allow_dirty)
    eng.provider = provider  # type: ignore[attr-defined]
    return eng
