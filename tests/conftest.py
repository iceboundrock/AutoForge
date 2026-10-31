"""Shared fixtures/helpers for AutoForge tests.

No test ever invokes a real Claude Code / OpenCode / GitHub write API:
agents are ``ScriptedProvider`` instances, GitHub is ``FakeGitHub``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from autoforge.certificate import Revision, remote_certificate  # noqa: E402
from autoforge.config import default_config  # noqa: E402
from autoforge.engine import ControllerEngine  # noqa: E402
from autoforge.errors import (  # noqa: E402
    ExecutionError,
    GitHubChecksAccessError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
)
from autoforge.evidence import (  # noqa: E402
    PrHeadProvenance,
    RemoteCandidate,
    binding,
    find_evidence,
)
from autoforge.executor import ExecutionResult, select_environment  # noqa: E402
from autoforge.git_transport import (  # noqa: E402
    NO_AUTO_MAINTENANCE,
    CommitRecord,
    GitRemote,
    GitTransport,
)
from autoforge.github import (  # noqa: E402
    ChangedFile,
    ChangedFiles,
    CheckInfo,
    CommentInfo,
    CreatedObject,
    IssueInfo,
    IssueNode,
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
from autoforge.state import utcnow_iso  # noqa: E402
from autoforge.validation import parse_issue_url  # noqa: E402
from tests import tmp_path_retention  # noqa: E402

EPIC = "https://github.com/owner/repo/issues/1"
ISSUE = "https://github.com/owner/repo/issues/2"
ISSUE3 = "https://github.com/owner/repo/issues/3"
PR = "https://github.com/owner/repo/pull/42"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
BRANCH = "autoforge/2-feature"
# A dummy Google API key (#282): ``AIza`` and 35 characters of the key's
# alphabet, ``-`` and ``_`` among them. Built in pieces, so that no literal
# key-shaped string stands in the repository for a secret scanner to flag;
# ``GOOGLE_KEY_BODY`` is what must not appear where the key was redacted or
# refused.
GOOGLE_KEY_BODY = "SyFAKE-dummy_" + "0" * 22
GOOGLE_API_KEY = "AIza" + GOOGLE_KEY_BODY
# Dummies of the shapes #285 adds, each at its real length and built in
# pieces as the Google key is; each ``*_BODY`` is what must not appear where
# its credential was redacted or refused.
AWS_KEY_BODY = "FAKE0DUMMY000000"
AWS_ACCESS_KEY_ID = "AKI" + "A" + AWS_KEY_BODY
GROQ_KEY_BODY = "FAKEdummy" + "0" * 43
GROQ_API_KEY = "gs" + "k_" + GROQ_KEY_BODY
XAI_KEY_BODY = "FAKE_dummy" + "0" * 70
XAI_API_KEY = "xa" + "i-" + XAI_KEY_BODY
NPM_TOKEN_BODY = "FAKEdummy" + "0" * 27
NPM_TOKEN = "np" + "m_" + NPM_TOKEN_BODY
GITLAB_PAT_BODY = "FAKE-dummy_" + "0" * 9
GITLAB_PAT = "gl" + "pat-" + GITLAB_PAT_BODY
HF_TOKEN_BODY = "FAKEdummy" + "x" * 25
HUGGINGFACE_TOKEN = "h" + "f_" + HF_TOKEN_BODY
# A dummy RSA private key in PEM: two 64-character base64 lines and a short
# last one. It decodes to no key.
PRIVATE_KEY_BODY = "MIIEFAKEdummy0private0key0body0" + "A" * 33
PEM_BEGIN = "-----BEGIN" + " RSA PRIVATE KEY-----"
PEM_END = "-----END" + " RSA PRIVATE KEY-----"
PRIVATE_KEY = f"{PEM_BEGIN}\n{PRIVATE_KEY_BODY}\n{PRIVATE_KEY_BODY}\nFAKE+/==\n{PEM_END}"
# The same body rewrapped at eight characters a line, as ``fold -w 8`` writes
# a key OpenSSL still reads (#285 review): no line holds 16 base64 characters
# in a row. ``SHORT_WRAPPED_KEY_LINE`` is one of its lines, what must not
# appear once it is redacted or refused.
SHORT_WRAPPED_KEY_LINES = [
    (PRIVATE_KEY_BODY * 2)[i : i + 8] for i in range(0, 2 * len(PRIVATE_KEY_BODY), 8)
]
SHORT_WRAPPED_KEY_LINE = SHORT_WRAPPED_KEY_LINES[1]
SHORT_WRAPPED_KEY = "\n".join([PEM_BEGIN, *SHORT_WRAPPED_KEY_LINES, "FAKE+/==", PEM_END])
# A value of no known shape under a prefixed name (#285).
SUFFIX_SECRET = "FAKE0opaque0value"
SUFFIX_ASSIGNMENT = f"DEEPSEEK_API_KEY={SUFFIX_SECRET}"
# Every #285 dummy: the class ``credential_classes`` names it under, the
# text, and what must not appear once it is redacted or refused.
SHAPES_285 = [
    ("private-key", PRIVATE_KEY, PRIVATE_KEY_BODY),
    ("gitlab-pat", GITLAB_PAT, GITLAB_PAT_BODY),
    ("aws-access-key-id", AWS_ACCESS_KEY_ID, AWS_KEY_BODY),
    ("groq-api-key", GROQ_API_KEY, GROQ_KEY_BODY),
    ("xai-api-key", XAI_API_KEY, XAI_KEY_BODY),
    ("npm-token", NPM_TOKEN, NPM_TOKEN_BODY),
    ("huggingface-token", HUGGINGFACE_TOKEN, HF_TOKEN_BODY),
    ("credential-suffix-assignment", SUFFIX_ASSIGNMENT, SUFFIX_SECRET),
]
# Values under a lower-case prefixed name that the review of #285 (round 2)
# found kept in the clear, each valid YAML, shell or form syntax: an unquoted
# word of letters alone, one with punctuation, a form body ``urlencode``
# wrote, and quoted values that span lines or hold a doubled quote. Each with
# its id, the text and what must not appear once it is redacted or refused:
# the part of the value a partial redaction left.
PUNCTUATION_SECRET = "FAKE$p&ss!w#rd%^*(x)-+=[]{}|<>?/~,.:FAKEtail"
FORM_SECRET = "FAKE form/value+with=signs&FAKEtail"
WHOLE_VALUES_285 = [
    ("alphabetic", "client_secret: FAKEalphaword", "FAKEalphaword"),
    ("punctuation", f"db_password: {PUNCTUATION_SECRET}", "FAKEtail"),
    (
        "form-encoded",
        urlencode({"grant_type": "client_credentials", "client_secret": FORM_SECRET}),
        "FAKEtail",
    ),
    ("multi-line-shell", 'export DB_PASSWORD="FAKEfirst\nFAKEsecond"', "FAKEsecond"),
    ("multi-line-yaml", 'client_secret: "FAKEfirst\n  FAKEsecond"', "FAKEsecond"),
    ("yaml-doubled-quote", "db_password: 'FAKE''FAKEafter'", "FAKEafter"),
]
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
# Provider stdout records (and state files) the JSON decoder refuses with
# ValueError or RecursionError, not JSONDecodeError, on every supported Python
# and far inside any per-record bound: an integer past the default 4300-digit
# conversion limit (well-formed, and followed by a stray comma), and nesting
# no interpreter decodes (3.11 stops near 1,000 levels, 3.12+ go deeper,
# none to 100,000).
DECODER_LIMIT_RECORDS = {
    "huge-int": b'{"type": "x", "n": ' + b"1" * 5000 + b"}",
    "huge-int-then-comma": b'{"type": "x", "n": ' + b"1" * 5000 + b",}",
    "deep-nesting": b'{"type": "x", "n": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
}

# -- the state pins of LOCAL's removal (ADR 0009 D8, D12 S1b; #385) ---------------------
# The twelve state fields LOCAL mode added (ADR 0009 §1.1), at the empty value
# every REMOTE save writes them with. A protocol-1 file carries none, some or
# all of them, every later save all twelve. Each refusal the removal keeps is
# pinned on a file with all twelve at these values and on one with none of
# them, so that dropping the fields (#388) cannot turn either into a file that
# loads, or refuse it for another reason.
LOCAL_STATE_FIELDS = {
    "feature_spec_path": "",
    "feature_spec_sha256": "",
    "base_head_sha": "",
    "base_branch": "",
    "local_run_contract": {},
    "workspace_fingerprint": "",
    "reviewed_workspace_fingerprint": "",
    "local_fix_rounds": 0,
    "local_pending_phase": "",
    "local_pending_fingerprint": "",
    "local_pending_attempts": 0,
    "baseline_dirty_paths": [],
}


@pytest.fixture(params=["with-local-fields", "without-local-fields"])
def local_fields(request) -> Callable[[dict], dict]:
    """Give a state file's dict LOCAL's twelve fields at their empty values, or none of them.

    The callable changes the dict in place and returns it: a test applies it
    to the dict it is about to write, and so runs once on each file.
    """
    present = request.param == "with-local-fields"

    def shape(data: dict) -> dict:
        for name, empty in LOCAL_STATE_FIELDS.items():
            if present:
                data[name] = type(empty)()
            else:
                data.pop(name, None)
        return data

    return shape


def state_tree(root: Path) -> dict[str, tuple]:
    """Every entry under ``root``, ``root`` included, links unfollowed.

    Each is its kind, inode, modification time and, for a regular file, the
    SHA-256 of its bytes (a link's target for a link). Two equal snapshots
    mean nothing under ``root`` was written: no file changed or was replaced,
    and no entry was created, renamed or removed, not even one created and
    removed again in between (its directory's modification time moves).
    """
    import hashlib
    import stat as stat_mod

    out: dict[str, tuple] = {}
    for base, dirs, files in os.walk(root):
        for path in [base] + [os.path.join(base, name) for name in dirs + files]:
            st = os.lstat(path)
            if stat_mod.S_ISLNK(st.st_mode):
                content = os.readlink(path)
            elif stat_mod.S_ISREG(st.st_mode):
                content = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            else:
                content = None
            kind = stat_mod.S_IFMT(st.st_mode)
            out[os.path.relpath(path, root)] = (kind, st.st_ino, st.st_mtime_ns, content)
    return out


# The states that do not load whose handling ADR 0009 D4 audits: an unknown
# mode, an unknown protocol label and invalid JSON (#385), and a LOCAL state,
# which the loader refuses as an unknown mode since LOCAL was removed (#387).
UNLOADABLE_STATES = ("unknown-mode", "unknown-label", "invalid-json", "local-mode")


def unloadable_state(kind: str, saved: bytes) -> tuple[bytes, str]:
    """``saved``, a state file that loads, made one of :data:`UNLOADABLE_STATES`.

    Returns its bytes and the part of the loader's refusal that names why.
    The label is one no controller will reach; the invalid JSON is the file
    torn one byte short of its closing brace.
    """
    if kind == "invalid-json":
        whole = saved.rstrip()  # a save ends the file with a newline
        assert whole.endswith(b"}")
        return whole[:-1], "invalid JSON"
    data = json.loads(saved)
    if kind == "unknown-mode":
        data["mode"] = "SATELLITE"
        return json.dumps(data).encode("utf-8"), "state file has unknown mode 'SATELLITE'"
    if kind == "local-mode":
        data["mode"] = "LOCAL"
        return json.dumps(data).encode("utf-8"), "state file has unknown mode 'LOCAL'"
    assert kind == "unknown-label", kind
    data["protocol_version"] = "99"
    return json.dumps(data).encode("utf-8"), "unsupported protocol_version '99'"


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
    """What an UPDATE_EPIC agent did before ADR 0004: post the progress comment.

    The controller now posts it (D9.7), so this mimics either a misbehaving
    agent or the comment a previous-protocol run left behind (D13.7). A
    second call posts nothing.
    """
    from autoforge.engine import render_progress_marker

    if any(render_progress_marker(ISSUE, PR) in c.body for c in gh.comments.get(EPIC, [])):
        return
    gh.add_comment(EPIC, cid, progress_comment_body())


def implementation_pr_body(issue_url: str = ISSUE) -> str:
    """A PR body carrying the issue's implementation marker (an existing implementation PR)."""
    from autoforge.engine import render_implementation_marker

    return f"Closes {issue_url}\n\n{render_implementation_marker(issue_url)}\n"


def follow_up_issue_body(finding_id: str, pr_url: str = PR) -> str:
    """A follow-up issue body carrying the (PR, finding) marker."""
    from autoforge.engine import render_follow_up_marker

    return (
        f"Follow-up for {finding_id} of {pr_url}\n\n{render_follow_up_marker(pr_url, finding_id)}\n"
    )


# The prose sections of a REMOTE REVIEW result (#162): the controller renders
# them, with the findings and its own binding line and marker, into the
# round's comment.
REVIEW_PROSE: dict[str, str] = {
    "spec": "The change does what the issue asks.",
    "standards": "It follows the repository conventions.",
    "assessment": "Sound.",
    "observations": "None.",
    "verification": "Ran `pytest`.",
    "summary": "Ready.",
}


def review_result(round: int, sha: str, findings: list[dict] | None = None, **overrides) -> dict:
    """A REMOTE REVIEW success result of ``round`` at ``sha`` (#162 schema).

    ``needs_fix_round`` follows the findings unless overridden. The reviewer
    posts nothing: the controller renders and posts the round's comment.
    """
    findings = list(findings or [])
    return {
        "phase": "REVIEW",
        "status": "success",
        "round": round,
        "reviewed_head_sha": sha,
        "needs_fix_round": bool(findings),
        "findings": findings,
        **REVIEW_PROSE,
        **overrides,
    }


def controller_review_comment(eng: ControllerEngine, round: int, pr_url: str = PR) -> CommentInfo:
    """The one comment carrying round ``round``'s marker that the controller posted."""
    from autoforge.claims import REVIEW, scan

    gh = eng.github
    assert isinstance(gh, FakeGitHub)
    held = [
        c
        for c in gh.comments.get(pr_url, [])
        if any(claim.round == round for claim in scan(REVIEW, c.body).claims)
    ]
    assert len(held) == 1, f"{len(held)} comments carry round {round}'s marker"
    return held[0]


def review_comment_body(
    round: int,
    sha: str,
    needs_fix: bool,
    finding_ids: list[str] | None = None,
    *,
    base_ref: str | None = "main",
    merge_base_sha: str | None = MERGE_BASE,
) -> str:
    """A well-formed round comment someone else posted.

    The controller posts every round's comment itself (#162), so in a test
    that reaches REVIEW this is a comment it did not journal (a human's, or
    one a previous-protocol reviewer left) and stops the round; elsewhere it
    is the earlier round comment the merge gate and ``unblock`` re-read.
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


# GitHub's answer to a fine-grained token reading a private PR's check runs
# (#291), as `gh pr view --json ...,statusCheckRollup` prints it; a GitHub
# App installation token without Checks: read gets it worded "by integration".
CHECKS_REFUSAL = (
    "GraphQL: Resource not accessible by {credential} "
    "(repository.pullRequest.statusCheckRollup.nodes.0.commit.statusCheckRollup.contexts.nodes.0)"
)


def checks_refused(url: str = PR, credential: str = "personal access token"):
    """The error the real client raises for :data:`CHECKS_REFUSAL` on ``url``'s checks read."""
    answer = CHECKS_REFUSAL.format(credential=credential)
    return GitHubChecksAccessError(
        f"`gh pr view {url} --json ...,statusCheckRollup` failed (exit 1): {answer}",
        credential=credential,
    )


# `gh`'s JSON sanitizer: go-gh's asciisanitizer (v2.17.0, the version gh 2.103
# links), which `gh` runs on every JSON reply before printing it. A C0 or C1
# control character, and a JSON escape of one, become caret notation. It reads
# the reply's bytes, not JSON, so the *text* of an escape written in a body
# (the six characters `\u001b`, sent as `\\u001b`) is rewritten too: to `\^[`,
# with a backslash added to keep the JSON valid. `tests/test_github.py` pins
# this port to upstream's own test vectors.
_GH_CARET = {
    c: "^" + ("\\\\" if c % 0x80 == 0x1C else chr(0x40 + c % 0x80))
    for c in [*range(0x20), *range(0x80, 0xA0)]
    if c not in (0x09, 0x0A, 0x0B, 0x0D)
}
_GH_JSON_CARET = {f"\\u{c:04x}": caret for c, caret in _GH_CARET.items()}


def gh_sanitized_json(wire: str) -> str:
    """The JSON text ``wire`` as go-gh's sanitizer (JSON mode) rewrites it."""
    out: list[str] = []
    add_escape = False
    i = 0
    while i < len(wire):
        char = wire[i]
        caret = _GH_CARET.get(ord(char))
        if caret is not None:
            out.append(caret)
            i += 1
            continue
        caret = _GH_JSON_CARET.get(wire[i : i + 6].lower())
        if caret is not None:
            out.append("\\" + caret if add_escape else caret)
            add_escape = False
            i += 6
            continue
        out.append(char)
        add_escape = not add_escape if char == "\\" else False
        i += 1
    return "".join(out)


def gh_sanitized(text: str) -> str:
    """``text`` as `gh` prints it: GitHub's JSON of it through the sanitizer, decoded.

    GitHub's encoder is modelled by ``json.dumps(..., ensure_ascii=False)``:
    a control character as its ``\\u00XX`` escape (``\\b``, ``\\f``, ``\\n``,
    ``\\r`` and ``\\t`` as the short ones), everything else as UTF-8.
    """
    return json.loads(gh_sanitized_json(json.dumps(text, ensure_ascii=False)))


class FakeGitHub:
    """In-memory GitHub read model. Tests mutate it to simulate agent actions.

    The ``create_*`` / ``write_*_body`` methods are the controller's own typed
    effect writes (ADR 0004 D12.2), as opposed to helpers such as
    ``post_progress_comment`` or a scripted handler calling ``add_pr``, which
    mimic what an agent publishes.
    """

    def __init__(self, repo: str = "owner/repo"):
        self.repo = repo
        self.issues: dict[str, IssueInfo] = {}
        self.prs: dict[str, PRInfo] = {}
        self.comments: dict[str, list[CommentInfo]] = {}
        self.calls: list[tuple] = []
        # Controller-owned merge simulation (see merge_pr).
        self.merges: list[tuple[str, str, str]] = []
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
        # merge.delete_branch (#283): every remote branch deletion the client
        # would send, as (repository, branch); ``delete_branch_error`` makes it
        # raise, after deleting when ``delete_branch_lands`` (the reply was
        # lost), before otherwise. ``branch_ref_error`` makes get_branch_ref
        # raise, ``get_repo_error`` get_repo. An ExecutionError is the real
        # client's when `gh` cannot be started.
        self.deleted_branches: list[tuple[str, str]] = []
        self.delete_branch_error: GitHubError | ExecutionError | None = None
        self.delete_branch_lands: bool = False
        self.branch_ref_error: GitHubError | ExecutionError | None = None
        self.get_repo_error: GitHubError | ExecutionError | None = None
        # GitHub's current name for a repository it renamed, by old name.
        self.repo_names: dict[str, str] = {}
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
        # The `closed` issue events of each PR, keyed as `_stored` resolves a
        # URL; appended by every close that lands (the fake's `close_pr`, or
        # `add_close_event` for a human's) and never removed, as GitHub's
        # timeline is append-only. Read by `get_pr_close_event_count`, the
        # replan close watermark (#69). ``close_events_error`` makes that read
        # raise instead (GitHubUnavailableError for a transient failure).
        self.close_events: dict[tuple[str, int], int] = {}
        self.close_events_error: GitHubError | None = None
        self.comment_error: str | GitHubError = ""  # non-empty -> comment_pr raises
        # Called at the start of comment_pr: a mutation racing a comment write.
        # The replan's post-close receipt is the interesting one -- it lands
        # between the controller's post-close read and its confirmation.
        self.comment_race = None
        # Called once, then cleared, as create_issue_comment returns or raises:
        # a change between the write and its read-back (PR #221 R2-F1).
        self.after_issue_comment: Callable[[], None] | None = None
        self.commented_prs: list[tuple[str, str]] = []
        self.get_pr_failures: int = 0  # next N get_pr calls raise GitHubUnavailableError
        self.get_pr_error: GitHubError | None = None  # every get_pr call raises this
        # Every checks read (get_pr_with_checks) raises this, the identity
        # read (get_pr) answers: GitHub refusing a fine-grained token a
        # private PR's check runs (#291) is ``checks_refused()``.
        self.checks_error: GitHubError | None = None
        self.get_issue_error: GitHubError | None = None  # every get_issue call raises this
        # get_issue calls for these URLs (canonical) raise; others answer.
        self.get_issue_errors: dict[str, GitHubError] = {}
        # Each issue's native parent, as GitHub's ``parent_issue_url`` names it
        # (#287), by the canonical URL ``add_issue`` registered: an issue a test
        # adds is a sub-issue of EPIC unless ``parent`` says otherwise; the EPIC
        # and an issue the controller creates (a follow-up) have none.
        # ``graph_error`` makes get_issue_node and both sub-issue listings
        # raise; ``sub_issues_error`` the listings alone. ``hidden_sub_issues``
        # are sub-issues GitHub counts for an issue (by canonical URL) but
        # leaves out of its listing, as it does one the token may not read.
        self.parents: dict[str, str | None] = {}
        self.graph_error: GitHubError | None = None
        self.sub_issues_error: GitHubError | None = None
        self.hidden_sub_issues: dict[str, int] = {}
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
        # Every read through `gh` returns titles and bodies as `gh` prints them,
        # through its JSON sanitizer (:func:`gh_sanitized`); the exact reads
        # (get_pr_exact, get_issue_exact, get_comment_exact) return what the
        # fake's GitHub holds. Off, every read returns what it holds.
        self.gh_sanitizes: bool = False
        self.exact_read_error: GitHubError | None = None  # every exact read raises this
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
        # The controller's typed effect writes (ADR 0004 D12.2): every attempt
        # that passes the client's own bound checks is recorded as
        # (method name, target, body), landed or not; the target is the
        # canonical issue/PR URL, or the repository for create_issue and
        # create_pull_request.
        self.effect_writes: list[tuple] = []
        # Failure injection for those writes: (method name, error, lands).
        # Each write pops the first entry naming it: ``lands=False`` raises
        # without writing (W3, the write never reached GitHub), ``lands=True``
        # performs the write and then raises (W4, it landed but the reply was
        # lost). A GitHubUnavailableError models the ambiguous outcome.
        self.write_failures: list[tuple[str, BaseException, bool]] = []
        # commit_in_history(repository, base_sha, commit_sha) answers from this;
        # a test that reaches it without setting it fails (AssertionError).
        self.history: Callable[[str, str, str], bool] | None = None
        # Every call of the named effect identity read (commit_in_history,
        # latest_issue_number, list_issues_above, list_prs_for_head) raises this.
        self.effect_read_errors: dict[str, BaseException] = {}
        # The bare repository standing in for GitHub's git data (attach_origin).
        # Once attached, a branch head ``branch_heads`` does not override is
        # read from it, an open same-repository PR's head follows each move of
        # its branch there (unless ``pr_heads_lag``: GitHub has not caught up
        # with a push yet), and ``commit_in_history`` asks it when ``history``
        # is unset.
        self.origin: Origin | None = None
        self.pr_heads_lag: bool = False
        # The repository's default branch as ``get_repo`` names it; a test
        # renames it under a run (#161).
        self.default_branch: str = "main"
        self._origin_seen: dict[str, str] = {}
        # Issues and PRs up to this number exist beyond the fake's listings
        # (GitHub's one shared sequence): a created PR or issue gets the next.
        self.last_number: int = 0
        self.add_issue(EPIC, "EPIC", parent=None)
        self.add_issue(ISSUE, "Feature")

    # -- test helpers ---------------------------------------------------------
    def attach_origin(self, origin: Origin) -> None:
        """Back branch heads, PR heads and ancestry with ``origin`` (#161).

        Its ``main`` becomes the default branch head (and the base branch's
        `push` run moves there with it); the next PR a controller creates is
        ``PR`` (#42), as the rest of the fake assumes.
        """
        main = origin.head("main")
        assert main is not None, "the origin has no main branch"
        self.origin = origin
        self.branch_heads.pop("main", None)
        self.workflow_runs[BASE_RUN_ID] = workflow_run(
            BASE_RUN_ID, main, event="push", head_branch="main"
        )
        self.last_number = max(self.last_number, 41)

    def _follow_origin(self) -> None:
        """An open same-repository PR's head moves when its origin branch does, as on GitHub.

        Only a move is followed: a head a test set with :meth:`set_head`
        stands until the branch itself moves again.
        """
        if self.origin is None or self.pr_heads_lag:
            return
        for url, p in self.prs.items():
            if not p.is_open or (
                p.head_repository and not self._same_repo(p.head_repository, p.repository)
            ):
                continue
            sha = self.origin.head(p.head_ref)
            if sha is None or self._origin_seen.get(p.head_ref) == sha:
                continue
            self._origin_seen[p.head_ref] = sha
            if sha != p.head_sha:
                self.set_head(sha, url)

    def add_issue(
        self,
        url: str,
        title: str = "t",
        state: str = "OPEN",
        body: str = "",
        parent: str | None = EPIC,
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
        self.parents[ref.canonical] = parse_issue_url(parent).canonical if parent else None
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
            # As GitHub reports a PR headed in its own repository; a test
            # sets another (a fork) or "" (a deleted head repository).
            head_repository=ref.repository,
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
    def get_repo(self, repo: str) -> RepoInfo:
        self.calls.append(("get_repo", repo))
        if self.get_repo_error is not None:
            raise self.get_repo_error
        return RepoInfo(
            name_with_owner=self.repo_names.get(repo.lower(), repo),
            default_branch=self.default_branch,
        )

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
                return self._gh_prints(info)
        raise GitHubNotFoundError(f"issue not found: {url}")

    def get_issue_state(self, url: str) -> str:
        return self.get_issue(url).state

    def get_issue_node(self, url: str) -> IssueNode:
        self.calls.append(("get_issue_node", url))
        if self.graph_error is not None:
            raise self.graph_error
        ref = parse_issue_url(url)
        for known in self.issues:
            if parse_issue_url(known).same_target(ref):
                return self._issue_node(known)
        raise GitHubNotFoundError(f"issue not found: {url}")

    def list_sub_issues(self, url: str) -> tuple[IssueNode, ...]:
        self.calls.append(("list_sub_issues", url))
        if (error := self.graph_error or self.sub_issues_error) is not None:
            raise error
        return tuple(self._issue_node(known) for known in self._children(url))

    def list_titled_sub_issues(self, url: str) -> tuple[tuple[IssueNode, str], ...]:
        self.calls.append(("list_titled_sub_issues", url))
        if (error := self.graph_error or self.sub_issues_error) is not None:
            raise error
        return tuple(
            (self._issue_node(known), self.issues[known].title) for known in self._children(url)
        )

    def _children(self, url: str) -> list[str]:
        ref = parse_issue_url(url)
        return [
            known
            for known, parent in self.parents.items()
            if parent is not None and parse_issue_url(parent).same_target(ref)
        ]

    def _issue_node(self, known: str) -> IssueNode:
        """The graph facts of a stored issue: no blockers, a closed one ``completed``."""
        info = self.issues[known]
        parent = self.parents.get(known)
        return IssueNode(
            id=100_000 + info.number,
            number=info.number,
            url=info.url,
            repository=parse_issue_url(known).repository,
            state="open" if info.is_open else "closed",
            state_reason=None if info.is_open else "completed",
            parent=parse_issue_url(parent) if parent else None,
            sub_issues_total=len(self._children(known)) + self.hidden_sub_issues.get(known, 0),
            blocked_by_total=0,
            blocked_by_open=0,
        )

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
            self._gh_prints(replace(i))
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
        self._follow_origin()
        for known, info in self.prs.items():
            if parse_pr_url(known).same_target(ref):
                return info
        raise GitHubError(f"pr not found: {url}")

    def _same_repo(self, repository: str, repo: str) -> bool:
        return repository.lower() == repo.lower()

    def _events_key(self, url: str) -> tuple[str, int]:
        pr = self._stored(url)  # fails closed on unknown PR, like `gh api`
        return (pr.repository.lower(), pr.number)

    def add_close_event(self, url: str) -> None:
        """Record a close that landed outside the fake's `close_pr` (a human's).

        Tests that set a PR's state to CLOSED by hand call this too, so the
        PR's timeline says what its state says.
        """
        key = self._events_key(url)
        self.close_events[key] = self.close_events.get(key, 0) + 1

    def get_pr_close_event_count(self, url: str) -> int:
        self.calls.append(("get_pr_close_event_count", url))
        if self.close_events_error is not None:
            raise self.close_events_error
        return self.close_events.get(self._events_key(url), 0)

    def _pr_view(self, url: str) -> PRInfo:
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

    def get_pr(self, url: str) -> PRInfo:
        """The identity read: no ``statusCheckRollup``, so ``checks`` is None (#291)."""
        self.calls.append(("get_pr", url))
        return self._gh_prints(replace(self._pr_view(url), checks=None))

    def get_pr_with_checks(self, url: str) -> PRInfo:
        """The checks read: the identity fields and the head's checks (a list, maybe [])."""
        self.calls.append(("get_pr_with_checks", url))
        if self.checks_error is not None:
            raise self.checks_error
        pr = self._pr_view(url)
        return self._gh_prints(replace(pr, checks=list(pr.checks or [])))

    def get_pr_head_sha(self, url: str) -> str:
        return self.get_pr(url).head_sha

    def get_pr_state(self, url: str) -> str:
        return self.get_pr(url).state

    def get_pr_branch(self, url: str) -> str:
        return self.get_pr(url).head_ref

    def get_pr_checks(self, url: str):
        return self.get_pr_with_checks(url).checks

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
        return self._branch_head(branch)

    def get_branch_ref(self, repository: str, branch: str) -> str | None:
        """Mimic the exact ref read: the branch's commit, None once it is gone (#283)."""
        self.calls.append(("get_branch_ref", repository, branch))
        if self.branch_ref_error is not None:
            raise self.branch_ref_error
        if branch in self.branch_heads:
            return self.branch_heads[branch]
        return self.origin.head(branch) if self.origin is not None else None

    def delete_branch_ref(self, repository: str, branch: str, *, default_branch: str) -> None:
        """Mimic ``DELETE repos/<repo>/git/refs/heads/<branch>``, refused as the client refuses."""
        self.calls.append(("delete_branch_ref", repository, branch, default_branch))
        if not default_branch or branch.lower() == default_branch.lower():
            raise GitHubError(f"deleting branch {branch!r}: refused, it is the default branch")
        self.deleted_branches.append((repository, branch))
        if self.delete_branch_error is not None and not self.delete_branch_lands:
            raise self.delete_branch_error
        self.branch_heads.pop(branch, None)
        if self.origin is not None and self.origin.head(branch) is not None:
            self.origin.delete(branch)
        if self.delete_branch_error is not None:
            raise self.delete_branch_error

    def _branch_head(self, branch: str) -> str:
        if branch in self.branch_heads:
            return self.branch_heads[branch]
        sha = self.origin.head(branch) if self.origin is not None else None
        if sha is None:
            raise GitHubNotFoundError(f"branch not found: {branch}")
        return sha

    def get_merge_base_sha(self, repository: str, base_ref: str, head_sha: str) -> str:
        self.calls.append(("get_merge_base_sha", repository, base_ref, head_sha))
        if self.merge_base_error is not None:
            raise self.merge_base_error
        pinned = self.merge_bases.get((base_ref, head_sha.lower()))
        if pinned is not None:
            return pinned
        if self.origin is not None and self.merge_base == MERGE_BASE:
            # GitHub computes it from its git data; a rewritten ``merge_base``
            # still simulates a moved base.
            computed = self.origin.merge_base(base_ref, head_sha.lower())
            if computed is not None:
                return computed
        return self.merge_base

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
        # As the real client: only a conclusive not-found is absence.
        try:
            self.get_pr(url)
            return True
        except GitHubNotFoundError:
            return False

    def list_open_prs(self, repo: str) -> list[PRInfo]:
        self.calls.append(("list_open_prs", repo))
        self._follow_origin()
        if self.open_pr_listing_incomplete:
            raise GitHubError(
                f"open PR listing of {repo} cannot be read to its end: page 2 announces a "
                "next page but no cursor that reaches it (None)"
            )
        return [
            self._gh_prints(replace(p))
            for p in self.prs.values()
            if p.is_open and self._same_repo(p.repository, repo)
        ]

    def list_all_prs(self, repo: str, *, strict: bool = False) -> list[PRInfo]:
        self.calls.append(("list_all_prs", repo, strict))
        self._follow_origin()
        if strict and self.pr_listing_truncated:
            raise GitHubError(
                f"{repo} has at least 1000 pull requests, so the listing may be "
                "truncated and the set of candidates cannot be established"
            )
        return [
            self._gh_prints(replace(p))
            for p in self.prs.values()
            if self._same_repo(p.repository, repo)
        ]

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
            self._gh_prints(c)
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
                    return self._gh_prints(replace(c))
        raise GitHubNotFoundError(f"`gh api` failed (exit 1): comment not found: {url}")

    # -- the exact read: GitHub's text byte for byte, past `gh`'s sanitizer -------
    def _gh_prints(self, obj):
        """``obj`` as `gh` prints it: its title and body sanitized while ``gh_sanitizes``."""
        if not self.gh_sanitizes:
            return obj
        fields = [name for name in ("title", "body") if hasattr(obj, name)]
        return replace(obj, **{name: gh_sanitized(getattr(obj, name)) for name in fields})

    def _exact_read(self, name: str, url: str) -> None:
        self.calls.append((name, url))
        if self.exact_read_error is not None:
            raise self.exact_read_error

    def get_pr_exact(self, url: str) -> PRInfo:
        self._exact_read("get_pr_exact", url)
        return replace(self._stored(url), checks=None)

    def get_issue_exact(self, url: str) -> IssueInfo:
        self._exact_read("get_issue_exact", url)
        ref = parse_issue_url(url)
        for known, info in self.issues.items():
            if parse_issue_url(known).same_target(ref):
                return replace(info)
        raise GitHubNotFoundError(f"exact read of {url}: GitHub answered HTTP 404")

    def get_comment_exact(self, url: str) -> CommentInfo:
        from autoforge.validation import parse_comment_url

        self._exact_read("get_comment_exact", url)
        wanted = parse_comment_url(url)
        for cs in self.comments.values():
            for c in cs:
                if parse_comment_url(c.url).same_target(wanted):
                    return replace(c)
        raise GitHubNotFoundError(f"exact read of {url}: GitHub answered HTTP 404")

    def merge_pr(self, url: str, method: str = "squash", match_head_sha: str = "") -> None:
        """Mimic `gh pr merge --<method> --match-head-commit <sha>` on the fake."""
        from autoforge.validation import parse_pr_url

        canonical = parse_pr_url(url).canonical
        self.calls.append(("merge_pr", canonical, method, match_head_sha))
        self.merges.append((canonical, method, match_head_sha))
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
            self.add_close_event(canonical)

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

    # -- controller effect writes (ADR 0004 D12.2) -----------------------------
    def _injected_failure(self, name: str) -> tuple[BaseException, bool] | None:
        """Pop the first ``write_failures`` entry for ``name``: (error, lands)."""
        for i, (method, error, lands) in enumerate(self.write_failures):
            if method == name:
                del self.write_failures[i]
                return error, lands
        return None

    def _effect_write(self, name: str, target: str, body: str, perform: Callable[[], object]):
        """Record one write attempt, then fail or land it as ``write_failures`` says."""
        self.effect_writes.append((name, target, body))
        injected = self._injected_failure(name)
        if injected is not None and not injected[1]:
            raise injected[0]
        result = perform()
        if injected is not None:
            raise injected[0]
        return result

    @staticmethod
    def _write_bounds(name: str, body: str, title: str | None = None) -> None:
        """The real client's refusals before any process (no attempt recorded)."""
        from autoforge.github import MAX_BODY_CHARS, MAX_TITLE_CHARS

        if len(body) > MAX_BODY_CHARS:
            raise GitHubError(f"{name}: the body is over {MAX_BODY_CHARS} characters")
        if title is not None and len(title) > MAX_TITLE_CHARS:
            raise GitHubError(f"{name}: the title is over {MAX_TITLE_CHARS} characters")

    def _stored_issue(self, url: str) -> IssueInfo:
        ref = parse_issue_url(url)
        for known, info in self.issues.items():
            if parse_issue_url(known).same_target(ref):
                return info
        raise GitHubNotFoundError(f"`gh api` failed (exit 1): gh: Not Found (HTTP 404): {url}")

    def _next_number(self, repository: str) -> int:
        """Issues and PRs share one number sequence, as on GitHub."""
        numbers = [
            i.number for i in self.issues.values() if self._same_repo(i.repository, repository)
        ]
        numbers += [
            p.number for p in self.prs.values() if self._same_repo(p.repository, repository)
        ]
        return max([*numbers, self.last_number]) + 1

    def _new_comment(self, parent_url: str, body: str) -> CreatedObject:
        cid = 950_000 + len(self.effect_writes)
        c = self.add_comment(parent_url, cid, body)
        return CreatedObject(url=c.url, number=None)

    def create_issue_comment(self, issue_url: str, body: str) -> CreatedObject:
        canonical = parse_issue_url(issue_url).canonical
        self.calls.append(("create_issue_comment", canonical, body))
        self._write_bounds("create_issue_comment", body)

        def perform() -> CreatedObject:
            self._stored_issue(canonical)  # a 404 on an unknown issue, like GitHub
            return self._new_comment(canonical, body)

        try:
            return self._effect_write("create_issue_comment", canonical, body, perform)
        finally:
            after, self.after_issue_comment = self.after_issue_comment, None
            if after is not None:
                after()

    def create_pr_comment(self, pr_url: str, body: str) -> CreatedObject:
        from autoforge.validation import parse_pr_url

        canonical = parse_pr_url(pr_url).canonical
        self.calls.append(("create_pr_comment", canonical, body))
        self._write_bounds("create_pr_comment", body)

        def perform() -> CreatedObject:
            self._stored(canonical)  # fails closed on an unknown PR, like `gh`
            return self._new_comment(canonical, body)

        return self._effect_write("create_pr_comment", canonical, body, perform)

    def create_pull_request(
        self, repository: str, *, base: str, head: str, title: str, body: str
    ) -> CreatedObject:
        """Open a PR from ``head`` (a branch in ``branch_heads`` or the origin) onto ``base``.

        As on GitHub: an unknown head branch, or an open PR already headed at
        it onto the same base, is a conclusive 422; ``Closes #n`` style
        keywords in the body link the issue.
        """
        import re

        self.calls.append(("create_pull_request", repository, base, head, title, body))
        if ":" in head or not head or not base:
            raise GitHubError(f"create_pull_request: {head!r} is not a branch name")
        self._write_bounds("create_pull_request", body, title)

        def perform() -> CreatedObject:
            try:
                head_sha = self._branch_head(head)
            except GitHubNotFoundError:
                raise GitHubError("gh: Validation Failed (HTTP 422): head invalid") from None
            for p in self.prs.values():
                if (
                    p.is_open
                    and self._same_repo(p.repository, repository)
                    and p.head_ref == head
                    and p.base_ref == base
                ):
                    raise GitHubError(
                        "gh: Validation Failed (HTTP 422): A pull request already exists "
                        f"for {head}"
                    )
            number = self._next_number(repository)
            linked = [
                int(n)
                for n in re.findall(
                    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", body
                )
            ]
            info = self.add_pr(
                f"https://github.com/{repository}/pull/{number}",
                head_sha=head_sha,
                branch=head,
                body=body,
                base_ref=base,
                linked=linked,
            )
            info.title = title
            info.head_repository = info.repository
            return CreatedObject(url=info.url, number=number)

        return self._effect_write("create_pull_request", repository, body, perform)

    def write_pr_body(self, pr_url: str, body: str) -> None:
        from autoforge.validation import parse_pr_url

        canonical = parse_pr_url(pr_url).canonical
        self.calls.append(("write_pr_body", canonical, body))
        self._write_bounds("write_pr_body", body)

        def perform() -> None:
            self._stored(canonical).body = body

        self._effect_write("write_pr_body", canonical, body, perform)

    def create_issue(self, repository: str, *, title: str, body: str) -> CreatedObject:
        self.calls.append(("create_issue", repository, title, body))
        self._write_bounds("create_issue", body, title)

        def perform() -> CreatedObject:
            number = self._next_number(repository)
            info = self.add_issue(
                f"https://github.com/{repository}/issues/{number}",
                title=title,
                body=body,
                parent=None,
            )
            return CreatedObject(url=info.url, number=number)

        return self._effect_write("create_issue", repository, body, perform)

    def write_issue_body(self, issue_url: str, body: str) -> None:
        canonical = parse_issue_url(issue_url).canonical
        self.calls.append(("write_issue_body", canonical, body))
        self._write_bounds("write_issue_body", body)

        def perform() -> None:
            self._stored_issue(canonical).body = body

        self._effect_write("write_issue_body", canonical, body, perform)

    # -- effect identity reads (ADR 0004 D4.5, D7.5) ---------------------------
    def _effect_read(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name in self.effect_read_errors:
            raise self.effect_read_errors[name]

    def commit_in_history(self, repository: str, base_sha: str, commit_sha: str) -> bool:
        import re

        self._effect_read("commit_in_history", repository, base_sha, commit_sha)
        for sha in (base_sha, commit_sha):
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha.lower()):
                raise GitHubError(f"commit_in_history: {sha!r} is not a full commit SHA")
        if self.history is None and self.origin is not None:
            return self.origin.is_ancestor(commit_sha.lower(), base_sha.lower())
        assert self.history is not None, "FakeGitHub.history is not set"
        return self.history(repository, base_sha.lower(), commit_sha.lower())

    def latest_issue_number(self, repository: str) -> int:
        """Highest issue or PR number in the repo (the K5 watermark)."""
        self._effect_read("latest_issue_number", repository)
        return self._next_number(repository) - 1

    def list_issues_above(self, repository: str, watermark: int) -> list[IssueInfo]:
        self._effect_read("list_issues_above", repository, watermark)
        if isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0:
            raise GitHubError(f"list_issues_above: watermark {watermark!r} is not a number")
        issues = [
            self._gh_prints(replace(i))
            for i in self.issues.values()
            if self._same_repo(i.repository, repository) and i.number > watermark
        ]
        return sorted(issues, key=lambda i: i.number, reverse=True)

    def list_prs_for_head(self, repository: str, branch: str) -> list[PRInfo]:
        self._effect_read("list_prs_for_head", repository, branch)
        self._follow_origin()
        prs = [
            self._gh_prints(replace(p))
            for p in self.prs.values()
            if self._same_repo(p.repository, repository)
            and p.head_ref == branch
            and (not p.head_repository or self._same_repo(p.head_repository, repository))
        ]
        return sorted(prs, key=lambda p: p.number, reverse=True)


def scripted_config():
    """Default config with every profile routed to the scripted provider.

    Model/effort routing is preserved so tests can assert on it.
    """
    cfg = default_config()
    for p in cfg.profiles.values():
        p.provider = "scripted"
    return cfg


def github_origin(repo, repository: str = "owner/repo") -> Path:
    """Give the checkout ``repo`` an ``origin`` naming ``repository`` on github.com.

    INITIALIZING reads the checkout's repository from its ``origin``, locally
    (#283): an engine's checkout needs one, as an operator's has. Nothing
    ever fetches from it: the controller's transport uses its own URL. An
    ``origin`` that already names ``repository`` is left alone, so engines
    built on one checkout at once (``tests/test_shared_git.py``) read it
    without racing each other for ``.git/config``.

    Its git is :func:`isolated_git`, so the ``origin`` is ``repo``'s: no
    ``GIT_DIR`` or ``GIT_COMMON_DIR`` of the test's environment can name
    another repository's configuration to write it into, and no hook of the
    operator's runs (#386 R2-F1).
    """
    root = Path(repo)
    url = f"https://github.com/{repository}.git"
    has = isolated_git_run("-C", str(root), "remote", "get-url", "origin")
    if has.returncode == 0 and has.stdout.decode().strip() == url:
        return root
    verb = "set-url" if has.returncode == 0 else "add"
    isolated_git("-C", str(root), "remote", verb, "origin", url)
    return root


def git_repo(path) -> Path:
    """Make ``path`` a git repository with one commit unless it already is one.

    The controller lock is keyed by the repository containing the engine's
    working directory, so every engine / CLI test that may take the lock
    needs a real repository, and a REMOTE agent launch adds a per-issue
    worktree detached at HEAD, which needs a commit to start from; ``git
    init`` plus an empty commit costs a few milliseconds.

    The commit starts no automatic maintenance: since Git 2.47 ``git commit``
    leaves ``git maintenance run --auto --detach`` running, which writes
    ``objects/maintenance.lock`` after the commit has returned, so a test
    that snapshots the repository raced with it and lost under load (#340).
    The ``-c`` options are the command's own, never the repository's
    configuration, which the controller's own maintenance tests depend on.
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
                *NO_AUTO_MAINTENANCE,
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


# The ``--config`` spellings of :func:`dotdot_config_paths` (#280, R2-F1).
DOTDOT_SPELLINGS = (
    "absolute-link",
    "relative-link",
    "relative-path",
    "leaves-the-checkout",
    "leaves-it-relative",
)


def dotdot_config_paths(checkout: Path, operator: Path) -> dict[str, str]:
    """``--config`` paths that open ``checkout``'s file or pass through it (#280, R2-F1).

    ``operator/absolute`` and ``operator/relative`` link to ``checkout/conf``,
    so ``operator/<link>/../autoforge.json`` opens ``checkout/autoforge.json``;
    a ``conf/..`` path looks up ``conf`` in the checkout and leaves it again.
    The relative ones are relative to ``checkout``. Collapsed by
    ``os.path.abspath``, every one names ``operator/autoforge.json``.
    """
    (checkout / "conf").mkdir(exist_ok=True)
    (operator / "absolute").symlink_to(checkout / "conf")
    (operator / "relative").symlink_to(os.path.relpath(checkout / "conf", operator))
    up = os.path.relpath(operator, checkout)
    return {
        "absolute-link": f"{operator}/absolute/../autoforge.json",
        "relative-link": f"{operator}/relative/../autoforge.json",
        "relative-path": f"{up}/absolute/../autoforge.json",
        "leaves-the-checkout": f"{checkout}/conf/../{up}/autoforge.json",
        "leaves-it-relative": f"conf/../{up}/autoforge.json",
    }


GIT_IDENT = ("-c", "user.name=AutoForge tests", "-c", "user.email=tests@example.invalid")


def git_out(*args: str) -> str:
    """``git`` with ``args``; its stdout, stripped. A failure fails the test."""
    res = subprocess.run(["git", *args], capture_output=True, text=True)
    assert res.returncode == 0, f"git {' '.join(args)}: {res.stderr}"
    return res.stdout.strip()


# The git of an :class:`Origin`, of :func:`github_origin` and of the live
# smoke test's own writes (#386 R1-F1, R2-F1). It starts from this
# allow-list of the environment: no ``GIT_*`` variable of the operator's
# shell (``GIT_DIR``, ``GIT_COMMON_DIR``, ``GIT_INDEX_FILE``,
# ``GIT_OBJECT_DIRECTORY``, ``GIT_CONFIG_*``, ``GIT_SSH_COMMAND``,
# ``GIT_ASKPASS``), no ``HOME``, no token, no SSH agent. It reads no system
# or global configuration, so no ``url.*.insteadOf``, ``pushInsteadOf``,
# credential helper, hook or signing program of the operator's applies, and
# fetches no missing object lazily. The switches turn off every transport, the
# credential helpers, hooks, the file-system monitor and replacement objects
# again at command-line precedence, over the repository's own configuration.
ISOLATED_GIT_ENV_ALLOWLIST = ("PATH", "LANG", "LANGUAGE", "LC_*", "TMPDIR")
ISOLATED_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_LAZY_FETCH": "1",
}
ISOLATED_GIT_SWITCHES = (
    "--no-replace-objects",
    "-c",
    "protocol.allow=never",
    "-c",
    "credential.helper=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
)


def isolated_git_run(*args: str, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """``git`` with ``args``, isolated as :data:`ISOLATED_GIT_ENV_ALLOWLIST` says."""
    return subprocess.run(
        ["git", *ISOLATED_GIT_SWITCHES, *args],
        env={**select_environment(ISOLATED_GIT_ENV_ALLOWLIST), **ISOLATED_GIT_ENV},
        input=input,
        capture_output=True,
    )


def isolated_git(*args: str, input: bytes | None = None) -> bytes:
    """:func:`isolated_git_run`'s stdout, whole. A failure fails the test."""
    res = isolated_git_run(*args, input=input)
    assert res.returncode == 0, f"git {' '.join(args)}: {res.stderr.decode(errors='replace')}"
    return res.stdout


# A full object id, SHA-1 or SHA-256, as a loose ref file holds one.
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class Origin:
    """A bare repository standing in for GitHub's copy of the repository (#161).

    The controller's own git transport fetches from and pushes to it over
    ``file://``, never over the network. Attached to a :class:`FakeGitHub`
    (:meth:`FakeGitHub.attach_origin`) it backs branch heads, PR heads and
    ancestry reads, as GitHub's API reflects its git data. ``gh`` is the
    stand-in the transport resolves before any network git runs: a
    ``file://`` remote never asks it for a credential.

    Its own git, seeding and :meth:`publish` included, is
    :func:`isolated_git` and uses no transport (#386 R1-F1): it writes into
    :attr:`bare` alone, whatever URL rewrite, ``pushurl``, credential helper
    or SSH command the operator's or the repository's configuration holds.
    """

    def __init__(self, bare: Path, gh: Path) -> None:
        self.bare = bare
        self.gh = gh

    @classmethod
    def seeded(cls, where, checkout) -> Origin:
        """A bare origin in ``where`` whose ``main`` is ``checkout``'s HEAD.

        ``origin.git`` is a directory in ``where``, never a link to one
        elsewhere; seeded again (a test's second engine), it is re-seeded,
        as ``git init`` re-initializes a repository.
        """
        where = Path(where).absolute()
        bare = where / "origin.git"
        assert not bare.is_symlink(), f"{bare} is a link, not the origin's directory"
        bare.mkdir(parents=True, exist_ok=True)
        isolated_git("init", "-q", "--bare", "-b", "main", str(bare))
        gh = where / "fake-gh"
        gh.write_text("#!/bin/sh\nexit 1\n")
        gh.chmod(0o755)
        origin = cls(bare, gh)
        origin.publish(checkout, "HEAD", "main")
        return origin

    @property
    def url(self) -> str:
        return f"file://{self.bare}"

    def head(self, branch: str) -> str | None:
        """What ``refs/heads/<branch>`` names, or None when there is no such branch.

        The fake reads it on every PR read (``_follow_origin``), about 10,000
        times a run, so the loose ref a push wrote is read as a file rather
        than through a git process (#341). Anything else (a packed or
        symbolic ref, the reftable format, a ref being replaced) is git's to
        resolve.
        """
        try:
            loose = (self.bare / "refs" / "heads" / branch).read_text().strip()
        except OSError:
            loose = ""
        if _OBJECT_ID.fullmatch(loose):
            return loose
        res = isolated_git_run(
            f"--git-dir={self.bare}", "rev-parse", "-q", "--verify", f"refs/heads/{branch}"
        )
        return res.stdout.decode().strip() if res.returncode == 0 else None

    def is_ancestor(self, ancestor: str, sha: str) -> bool:
        """GitHub's compare: False for a commit it does not hold."""
        res = isolated_git_run(
            f"--git-dir={self.bare}", "merge-base", "--is-ancestor", ancestor, sha
        )
        return res.returncode == 0

    def merge_base(self, branch: str, sha: str) -> str | None:
        """GitHub's merge base of ``sha`` with ``branch``; None when either is unknown."""
        res = isolated_git_run(f"--git-dir={self.bare}", "merge-base", f"refs/heads/{branch}", sha)
        return res.stdout.decode().strip() if res.returncode == 0 else None

    def publish(self, repo, rev: str, branch: str) -> str:
        """A push that is not the controller's (a test's, an agent's, a human's).

        Forced, like ``git push -f``, but over no transport and to no URL:
        the commit's objects are packed from ``repo``'s store and unpacked
        into :attr:`bare`, whose branch is then set and read back. So no URL
        rewrite or ``pushurl`` can send it anywhere else (#386 R1-F1).
        """
        sha = isolated_git("-C", str(repo), "rev-parse", f"{rev}^{{commit}}").decode().strip()
        pack = isolated_git(
            "-C", str(repo), "pack-objects", "--revs", "--stdout", "-q", input=f"{sha}\n".encode()
        )
        isolated_git(f"--git-dir={self.bare}", "unpack-objects", "-q", input=pack)
        isolated_git(f"--git-dir={self.bare}", "update-ref", f"refs/heads/{branch}", sha)
        assert self.head(branch) == sha, f"{self.bare}'s {branch} is not {sha}"
        return sha

    def delete(self, branch: str) -> None:
        isolated_git(f"--git-dir={self.bare}", "update-ref", "-d", f"refs/heads/{branch}")


def attach_origin(eng: ControllerEngine, where=None) -> Origin:
    """Give ``eng`` a seeded bare origin as its git remote and its GitHub's git data.

    ``where`` defaults to the parent of the engine's state directory (the
    test's tmp_path). The engine's ``gh`` becomes the origin's stand-in.
    """
    root = Path(where) if where is not None else Path(eng.paths.state_dir).parent
    origin = Origin.seeded(root, eng.workdir)
    connect_origin(eng, origin)
    gh = eng.github
    assert isinstance(gh, FakeGitHub)
    gh.attach_origin(origin)
    return origin


def connect_origin(eng: ControllerEngine, origin: Origin) -> None:
    """Point ``eng``'s own git transport at ``origin`` (an engine built later, e.g. by the CLI)."""
    eng._git_remote = GitRemote(url=origin.url)
    eng.config.github.command = str(origin.gh)


def commit_in(cwd, message: str = "Implement the feature (#2)") -> str:
    """One (empty) commit on the agent worktree's detached HEAD; the new HEAD.

    No automatic maintenance, for the reason :func:`git_repo` gives.
    """
    git_out(
        "-C",
        str(cwd),
        *NO_AUTO_MAINTENANCE,
        *GIT_IDENT,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
    )
    return git_out("-C", str(cwd), "rev-parse", "HEAD")


_LOCAL_GIT_GH = """#!/bin/sh
# A `gh` that does in its working directory what gh 2.103.2 does there for
# `gh pr merge --delete-branch` (pkg/cmd/pr/merge/merge.go): ask git for the
# local head branch, move the checkout off it, delete it. It does so for every
# command, and exits 0 with no output.
log={log}
printf 'cwd %s\\n' "$(pwd -P)" >> "$log"
git rev-parse --verify refs/heads/{branch} >/dev/null 2>&1
printf 'rev-parse %s\\n' "$?" >> "$log"
git checkout -q --detach >/dev/null 2>&1
printf 'checkout %s\\n' "$?" >> "$log"
git branch -D {branch} >/dev/null 2>&1
printf 'branch-D %s\\n' "$?" >> "$log"
exit 0
"""


def local_git_gh(where, branch: str) -> tuple[Path, Path]:
    """A fake `gh` that runs git on ``branch`` where it is started (#283), and its log.

    The log has a ``cwd <dir>`` line per start, then ``<op> <exit>`` for each
    git it ran (``rev-parse``, ``checkout``, ``branch-D``): 128 is git finding
    no repository.
    """
    root = Path(where)
    root.mkdir(parents=True, exist_ok=True)
    log = root / "gh.log"
    gh = root / "gh"
    gh.write_text(_LOCAL_GIT_GH.format(log=shlex.quote(str(log)), branch=shlex.quote(branch)))
    gh.chmod(0o755)
    return gh, log


def local_git_gh_runs(log: Path) -> tuple[list[str], list[str]]:
    """``(working directories, git outcomes)`` from a :func:`local_git_gh` log."""
    lines = log.read_text().splitlines() if log.exists() else []
    cwds = [line.split(" ", 1)[1] for line in lines if line.startswith("cwd ")]
    return cwds, [line for line in lines if not line.startswith("cwd ")]


@contextmanager
def watched_fifo(path: Path) -> Iterator[list[str]]:
    """A FIFO made at ``path``; the list it yields records each opening of it to read.

    A reader of a FIFO blocks until a writer comes, so a controller read
    that follows a planted link to one would stall. A watchdog tries every
    few milliseconds to open it for writing without blocking, which succeeds
    only while a reader has it open: it records the opening and closes it
    again, so the reader sees an end of file instead of hanging the test.
    The list stays empty unless something opened ``path`` (#223).
    """
    os.mkfifo(path)
    opened: list[str] = []
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            try:
                fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:  # ENXIO: no reader has it open
                stop.wait(0.01)
                continue
            opened.append(str(path))
            os.close(fd)

    watchdog = threading.Thread(target=watch, daemon=True)
    watchdog.start()
    try:
        yield opened
    finally:
        stop.set()
        watchdog.join()


def bind_unix(sock, path: Path) -> None:
    """Bind the AF_UNIX ``sock`` at ``path``, however long ``path`` is.

    ``sun_path`` holds 107 bytes, and a path under ``tmp_path`` can be
    longer: under a long ``TMPDIR``, or under pytest-xdist, whose workers
    add ``popen-gw<N>`` to every one (#340). Bound by its name from its
    directory, the socket is at the same place. A probe that connects to it
    does the same (``os.chdir`` to the directory, then the name).
    """
    cwd = os.getcwd()
    os.chdir(path.parent)
    try:
        sock.bind(path.name)
    finally:
        os.chdir(cwd)


def publish_pr_head(
    eng: ControllerEngine,
    parent: str = "HEAD",
    message: str = "Implement the feature (#2)",
    branch: str = BRANCH,
) -> str:
    """A commit on ``parent``, published to ``branch`` on the engine's origin; its SHA.

    Written with ``commit-tree`` into the checkout's object store, so the
    checkout's HEAD, index and working tree are not moved: a PR head (or a
    human's push to it) that a test starts from, with no agent involved.
    """
    origin = getattr(eng, "origin", None)
    assert origin is not None, "publish_pr_head needs an origin-backed engine"
    sha = git_out(
        "-C",
        str(eng.workdir),
        *GIT_IDENT,
        "commit-tree",
        f"{parent}^{{tree}}",
        "-p",
        parent,
        "-m",
        message,
    )
    origin.publish(eng.workdir, sha, branch)
    return sha


_REVIEWED_HEAD_RE = re.compile(r'"previous_head_sha": "([0-9a-f]{40})"')


def reviewed_head_in(prompt: str) -> str:
    """The reviewed HEAD a REMOTE FIX prompt names (its result's cross-check)."""
    m = _REVIEWED_HEAD_RE.search(prompt)
    assert m is not None, "not a REMOTE FIX prompt"
    return m.group(1)


def fix_commit(req, message: str = "Fix the review findings (#2)") -> str:
    """A fixer's work since #163: one commit on the reviewed HEAD; the new HEAD.

    The reviewed HEAD the prompt names is checked out detached in the
    fixer's worktree (the controller fetched it before the launch) and the
    commit is made on it. Nothing is pushed: the controller pushes it.
    """
    git_out("-C", str(req.cwd), "checkout", "-q", "--detach", reviewed_head_in(req.prompt))
    return commit_in(req.cwd, message)


def fix_payload(prev: str, head: str, resolutions: list[dict], **overrides) -> dict:
    """A REMOTE FIX success result (#163 schema)."""
    return {
        "phase": "FIX",
        "status": "success",
        "previous_head_sha": prev,
        "head_sha": head,
        "resolutions": resolutions,
        **overrides,
    }


def fixer(
    *resolutions: dict,
    commit: bool = True,
    message: str = "Fix the review findings (#2)",
    **overrides,
) -> Callable:
    """A FIX agent: a local commit (unless ``commit`` is False), then ``resolutions``.

    Without a commit the worktree is left at the reviewed HEAD, which the
    result reports as its ``head_sha``.
    """

    def handler(req) -> str:
        reviewed = reviewed_head_in(req.prompt)
        if commit:
            head = fix_commit(req, message)
        else:
            git_out("-C", str(req.cwd), "checkout", "-q", "--detach", reviewed)
            head = reviewed
        return block(fix_payload(reviewed, head, list(resolutions), **overrides))

    return handler


FOLLOW_UP_TITLE = "Cache the parsed configuration"
FOLLOW_UP_TEXT = "The configuration is parsed twice per run; parse it once and pass it down."
NO_CHANGE_RATIONALE = (
    "The loader already validates this path in `load_config`, and the test "
    "`test_config_rejects_unknown_keys` covers it; no change is needed."
)


def fixed(finding_id: str, **extra) -> dict:
    return {"finding_id": finding_id, "resolution": "fixed", **extra}


def new_follow_up(finding_id: str, title: str = FOLLOW_UP_TITLE, text: str = FOLLOW_UP_TEXT):
    """A deferral to a new follow-up issue the controller creates."""
    return {
        "finding_id": finding_id,
        "resolution": "follow_up_created",
        "follow_up_issue": {"title": title, "body": text},
    }


def deferred_to(finding_id: str, url: str) -> dict:
    """A deferral to an issue the controller handed over."""
    return {"finding_id": finding_id, "resolution": "follow_up_created", "follow_up_issue_url": url}


def no_change(finding_id: str, rationale: str = NO_CHANGE_RATIONALE) -> dict:
    return {
        "finding_id": finding_id,
        "resolution": "no_change_with_rationale",
        "rationale": rationale,
    }


ACCEPTANCE_TEXT = (
    "Ran `autoforge doctor` against the reviewed HEAD on a clean host; every check passed, "
    "which is the evidence the finding asks for."
)
ACCEPTANCE_RATIONALE = (
    "The finding asks for evidence on the issue that the operator check passed, not for a "
    "code change."
)


def acceptance(
    finding_id: str, text: str = ACCEPTANCE_TEXT, rationale: str = ACCEPTANCE_RATIONALE
) -> dict:
    """An acceptance action (#211): the controller comments ``text`` on the run's issue."""
    return {
        "finding_id": finding_id,
        "resolution": "acceptance_action",
        "rationale": rationale,
        "acceptance_action": {"kind": "issue_comment", "body": text},
    }


def analyze_payload(head_sha: str, **overrides) -> dict:
    """An ANALYZE_EXECUTE success result reporting ``head_sha`` (#161 schema)."""
    return {
        "phase": "ANALYZE_EXECUTE",
        "status": "success",
        "issue_url": ISSUE,
        "head_sha": head_sha,
        "pr_title": "Add the feature",
        "pr_body": "Adds the feature.\n\nTested with `pytest`.",
        "tests": ["pytest: passed"],
        **overrides,
    }


def implement(req, **overrides) -> str:
    """An implementation agent: one commit in its worktree, reported in a CONTROL_RESULT."""
    return block(analyze_payload(commit_in(req.cwd), **overrides))


def scripted(*steps) -> Callable:
    """A ScriptedProvider handler answering call N with ``steps[N]``.

    A step is a stdout string, or a handler called with the request (such as
    :func:`implement`); once the steps run out every call gets "".
    """
    queue = list(steps)

    def handler(req) -> str:
        if not queue:
            return ""
        step = queue.pop(0)
        return step(req) if callable(step) else step

    return handler


def make_engine(
    state_dir,
    script=None,
    github: FakeGitHub | None = None,
    cfg=None,
    exit_code: int = 0,
    workdir=None,
    origin: bool = False,
):
    """Engine wired to a ScriptedProvider (agents) and FakeGitHub (verification).

    ``workdir`` defaults to the parent of ``state_dir`` (the test's tmp_path),
    made a git repository so the engine can derive its repository lock, with
    an ``origin`` naming the fake's repository (:func:`github_origin`).
    ``origin`` attaches a seeded bare origin (:func:`attach_origin`), which
    an ANALYZE_EXECUTE entry needs: the controller fetches from and pushes
    to it (#161). The engine keeps it as ``eng.origin``. The engine never
    sleeps: each pause of a read-back waiting for GitHub to move a PR head
    (#403) returns at once and is recorded in ``eng.sleeps``.
    """
    cfg = cfg or default_config()
    if workdir is None:
        workdir = git_repo(Path(state_dir).parent)
    # As FakeGitHub's ``repo`` (the repository the run's checkout belongs to);
    # a workdir that is no repository (the lock tests) has no remote to set.
    if (Path(workdir) / ".git").exists():
        github_origin(workdir, getattr(github, "repo", "owner/repo"))
    provider = ScriptedProvider(script, exit_code=exit_code)
    registry = ProviderRegistry(overrides={"claude": provider, "opencode": provider})
    gh = github or FakeGitHub()
    sleeps: list[float] = []
    eng = ControllerEngine(
        config=cfg,
        state_dir=state_dir,
        workdir=workdir,
        github=gh,
        providers=registry,
        sleep=sleeps.append,
    )
    eng.new_run(EPIC, ISSUE)
    eng.provider = provider  # type: ignore[attr-defined]
    eng.sleeps = sleeps  # type: ignore[attr-defined]
    if origin:
        eng.origin = attach_origin(eng)  # type: ignore[attr-defined]
    return eng


def certify_review(eng: ControllerEngine, tree: str = "e" * 40) -> dict:
    """Hold the certificate the REVIEW write of a hand-built clean review issues (#174).

    A test that sets the review fields of a clean round directly, rather than
    stepping a reviewer through REVIEW, holds no certificate; READY_FOR_MERGE
    then refuses it, as ADR 0006 D12.3 requires. This binds the configured
    validation set (empty by default) to the reviewed HEAD at ``tree`` and
    issues the certificate the controller would have issued, exactly as
    :meth:`ControllerEngine._complete_review` does, then saves the state.
    """
    state = eng.state
    configured = eng._configured_validation()
    candidate = RemoteCandidate(
        state.repository,
        "sha1",
        state.reviewed_head_sha.lower(),
        tree,
        PrHeadProvenance(state.reviewed_pr_url),
    )
    records = state.validation_evidence()
    evidence = find_evidence(records, candidate, configured.definitions, configured.policy, None)
    state.review_evidence = binding(state.review_round, evidence)
    state.review_certificate = remote_certificate(
        state.review_evidence,
        records,
        repository=state.repository,
        run_id=state.run_id,
        review_round=state.review_round,
        needs_fix_round=False,
        actionable_findings=0,
        reviewed=Revision(
            state.reviewed_pr_url,
            state.reviewed_head_sha,
            state.reviewed_base_ref,
            state.reviewed_merge_base_sha,
        ),
        comment_url=state.last_review_comment_url,
        marker_key={
            "review_round": state.review_round,
            "reviewed_head_sha": state.reviewed_head_sha,
            "reviewed_base_ref": state.reviewed_base_ref,
            "reviewed_merge_base_sha": state.reviewed_merge_base_sha,
        },
        issued_at=utcnow_iso(),
    )
    eng._save()
    return state.review_certificate


class OfflineRemote(GitRemote):
    """The network remote of an engine whose test gave it no origin."""


# Every revision an engine without an origin fetched, in order (see
# ``_no_network_git_remote``); a test reads it through ``offline_fetches``.
_OFFLINE_FETCHES: list[list[str]] = []


@pytest.fixture(autouse=True)
def _no_network_git_remote(monkeypatch):
    """No test reaches GitHub through the controller's own git transport (#161).

    An engine whose test did not give it a local origin would talk to
    ``https://github.com/<repository>.git``. Its fetch is recorded and does
    nothing: a REVIEW entry fetches the reviewed revisions for a scripted
    reviewer that never reads them (#162). Any push to it fails the test.
    """
    import autoforge.engine as engine_module

    class LocalOnly(GitRemote):
        @classmethod
        def https(cls, repository: str) -> GitRemote:
            return OfflineRemote(url=GitRemote.https(repository).url)

    class OfflineAware(GitTransport):
        def fetch(self, revisions) -> None:
            if not isinstance(self.remote, OfflineRemote):
                return super().fetch(revisions)
            _OFFLINE_FETCHES.append(list(revisions))
            return None

        def read_local_commit(self, sha: str):
            # A revision a test without an origin "fetched" (a scripted
            # reviewer's PR head, never read) is not in the store. Its REVIEW
            # entry still names it as a candidate, commit and tree (ADR 0006
            # D5.5, #173): the stand-in's tree is derived from the SHA, so it
            # is stable per head. A commit the store holds is read as it is.
            found = super().read_local_commit(sha)
            if found is not None or not isinstance(self.remote, OfflineRemote):
                return found
            if not any(sha in fetched for fetched in _OFFLINE_FETCHES):
                return None
            return CommitRecord(sha=sha, parents=(), message="", tree=offline_tree(sha))

        def push(self, *args, **kwargs):
            if isinstance(self.remote, OfflineRemote):
                raise AssertionError(
                    f"a test pushed to the network git remote {self.remote.url}; build the "
                    "engine with make_engine(..., origin=True) or attach_origin(eng)"
                )
            return super().push(*args, **kwargs)

    _OFFLINE_FETCHES.clear()
    monkeypatch.setattr(engine_module, "GitRemote", LocalOnly)
    monkeypatch.setattr(engine_module, "GitTransport", OfflineAware)


@pytest.fixture(autouse=True)
def _no_network_exact_read(monkeypatch):
    """No test reaches GitHub through the exact read's HTTPS transport.

    A ``GitHubClient`` built without ``https=`` takes
    ``autoforge.github.https_get`` when it is constructed, so every client a
    test builds that way gets this stand-in, which fails the test (with
    pytest's own outcome, a BaseException the client's classification of
    transport failures never catches). The tests of the transport itself
    call the function they imported, against a loopback server.
    """
    import autoforge.github as github_module

    def offline(request):
        pytest.fail(
            f"a test reached the network exact read of {request.url}; give the client "
            "https=... or use FakeGitHub"
        )

    monkeypatch.setattr(github_module, "https_get", offline)


@pytest.fixture(scope="session")
def _isolation_root(tmp_path_factory) -> Path:
    """Where the three fixtures below make each test's empty directories.

    ``tmp_path_factory.mktemp`` lists the whole basetemp to number every
    directory it makes, so three of them for every test cost time that grows
    with the number of tests already run (#341). ``tempfile.mkdtemp`` names
    one under this root at random instead of listing it; each directory is
    still new, empty, 0700 and the test's own.
    """
    return tmp_path_factory.mktemp("isolation")


@pytest.fixture(autouse=True)
def _no_operator_netrc(monkeypatch, _isolation_root):
    """The launch preflight (#165) reads an empty home, not the developer's.

    ``~/.netrc`` with a GitHub entry, or a ``~/.profile`` that exports a
    token, refuses every launch, so a test run must not depend on whether
    the machine running it has one. A test of either refusal points
    ``operator_home`` at a home of its own.
    """
    import autoforge.child_env as child_env_module

    home = Path(tempfile.mkdtemp(prefix="home-", dir=_isolation_root))
    monkeypatch.setattr(child_env_module, "operator_home", lambda: str(home))


# The login-shell probe's answers, by what an answer depends on (below).
_SHELL_PROBE_ANSWERS: dict[tuple, list[str]] = {}


def _home_contents(home: str | None) -> tuple:
    """Each entry of ``home`` by name, a regular file with a digest of its bytes."""
    import hashlib
    import os

    if not home or not os.path.isdir(home):
        return ()
    entries = []
    for entry in sorted(os.scandir(home), key=lambda e: e.name):
        digest = (
            hashlib.sha256(Path(entry.path).read_bytes()).hexdigest()
            if entry.is_file(follow_symlinks=False)
            else None
        )
        entries.append((entry.name, digest))
    return tuple(entries)


@pytest.fixture(autouse=True)
def _one_shell_probe_per_answer(monkeypatch):
    """The preflight's login-shell probe (#165 R3-F1) starts the real ``sh``,
    ``bash`` and ``zsh`` before every launch, about a quarter of a second,
    which over the engine tests' 1,200-odd launches would add half again to
    the suite. What it answers depends on the operator's home, the
    environment a launch gets and the system's startup files, which no test
    changes; so a launch without a fake runner whose home holds the same
    files (by content; an empty home whatever its path) and whose
    environment is the same reuses the answer. A fake runner (it may observe
    the probe), a startup file a test writes or changes, another
    environment: the shells run again. ``tests/test_child_env.py`` calls the
    probe itself, which always runs them.
    """
    import autoforge.child_env as child_env_module
    from autoforge.executor import select_environment

    real = child_env_module.shell_startup_problems

    def probe(cwd, env_allowlist, runner=None, *, home=None):
        allowlist = tuple(env_allowlist)
        if runner is not None:
            return real(cwd, allowlist, runner, home=home)
        home = home if home is not None else child_env_module.operator_home()
        contents = _home_contents(home)
        key = (
            home if contents else None,
            contents,
            allowlist,
            tuple(sorted(select_environment(allowlist).items())),
            child_env_module.child_shell(),
        )
        if key not in _SHELL_PROBE_ANSWERS:
            _SHELL_PROBE_ANSWERS[key] = real(cwd, allowlist, home=home)
        return list(_SHELL_PROBE_ANSWERS[key])

    monkeypatch.setattr(child_env_module, "shell_startup_problems", probe)


@pytest.fixture(autouse=True)
def _no_operator_opencode_data(monkeypatch, _isolation_root):
    """An OpenCode launch's organization check (#165) reads an empty data
    directory, not the developer's.

    An organization connected to the developer's OpenCode refuses every
    OpenCode launch, so a test run must not depend on it. A test of the
    refusal points ``opencode_data_dir`` back at the real resolution (its
    module imported it before this fixture ran) over a home of its own.
    """
    import autoforge.providers as providers_module

    data_dir = Path(tempfile.mkdtemp(prefix="opencode-", dir=_isolation_root))
    monkeypatch.setattr(providers_module, "opencode_data_dir", lambda env: data_dir)
    monkeypatch.delenv("OPENCODE_DB", raising=False)


@pytest.fixture(autouse=True)
def _no_operator_claude_config(monkeypatch, _isolation_root):
    """A Claude Code launch's configuration check (#165) reads files in an
    empty directory, not the developer's settings and global configuration.

    A variable of the developer's Claude Code configuration that a launch
    refuses would refuse every Claude launch, so a test run must not depend
    on it. A test of the refusal points ``claude_config_files`` back at the
    real resolution (its module imported it before this fixture ran).
    """
    import autoforge.providers as providers_module

    config_dir = Path(tempfile.mkdtemp(prefix="claude-", dir=_isolation_root))
    files = [config_dir / "settings.json", config_dir / ".claude.json"]
    monkeypatch.setattr(providers_module, "claude_config_files", lambda env: files)


# Set to 1 where the real backend must run (a capable development host, a
# runner known to provide one): a real-backend module that would skip fails
# instead, so a host that lost its capability cannot pass silently (#169).
# Set to auto where whether it can is measured (the hosted `sandbox` CI job,
# `make test-sandbox-measured`): it fails instead of skipping exactly when
# tests/sandbox_host.py, independently of the backend's probe, measures the
# host capable. Any other non-empty value is refused, never read as "skip".
REQUIRE_SANDBOX_BACKEND = "AUTOFORGE_REQUIRE_SANDBOX_BACKEND"
# What the real-backend tests run their fake commands with, inside.
SYSTEM_PYTHON = "/usr/bin/python3"


def real_sandbox_backend(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The real backend's version, after its own probe; skip, with the reason, without one.

    For the module- or session-scoped ``backend`` fixture of every test that
    launches real ``bwrap``. The skip reason is the probe's typed result
    (``pytest -rs`` prints it), so a CI log says which capability the runner
    lacks; with :data:`REQUIRE_SANDBOX_BACKEND` set to 1 the test fails
    instead, and set to ``auto`` it fails when the host measures capable
    (:mod:`tests.sandbox_host`) and skips with the measurement otherwise.
    """
    import os

    from autoforge import sandbox_bwrap
    from tests import sandbox_host

    def unavailable(reason: str) -> None:
        require = os.environ.get(REQUIRE_SANDBOX_BACKEND, "")
        if require == "1":
            pytest.fail(f"{REQUIRE_SANDBOX_BACKEND}=1, but {reason}")
        if require == "auto":
            host = sandbox_host.measure()
            if host.capable:
                pytest.fail(
                    f"{REQUIRE_SANDBOX_BACKEND}=auto, and tests/sandbox_host.py measures that "
                    f"{host.verdict()}, but {reason}"
                )
            pytest.skip(f"{reason}; measured: {host.verdict()}")
        if require:
            pytest.fail(f"{REQUIRE_SANDBOX_BACKEND}={require!r} is neither 1 nor auto; {reason}")
        pytest.skip(reason)

    if not sys.platform.startswith("linux"):
        unavailable("the outer sandbox's backend is Linux-only")
    if not Path(SYSTEM_PYTHON).exists():
        unavailable(f"{SYSTEM_PYTHON} is not installed, so no fake command can run inside")
    home = tmp_path_factory.mktemp("probe-home")
    result = sandbox_bwrap.probe(home=str(home), environ={})
    if not result.supported:
        unavailable(f"this host cannot enforce the outer sandbox: {result.describe()}")
    return result.version


class SandboxProbe:
    """What this test's capability probe answers, and how often it was asked (#169).

    ``result`` is returned by every call; ``calls`` counts them.
    """

    def __init__(self) -> None:
        from autoforge.sandbox_bwrap import ProbeResult

        self.result = ProbeResult(True, "", "0.0-test")
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.result

    def unsupported(self, capability=None, reason: str = "") -> None:
        """Answer that this host lacks ``capability`` (the user namespace by default)."""
        from autoforge.sandbox_bwrap import Capability, ProbeResult

        capability = capability or Capability.USER_NAMESPACE
        self.result = ProbeResult(
            False, reason or f"test: no {capability.key.replace('_', ' ')}", "", capability
        )


@pytest.fixture(autouse=True)
def sandbox_probe(monkeypatch) -> SandboxProbe:
    """The outer sandbox's capability probe answers "supported" without a launch (#169).

    The engine's pre-launch gate and ``doctor`` ask
    :func:`autoforge.engine.probe_sandbox`, which launches a trivial command
    through the real backend: a test of a mode must not depend on whether the
    machine running it has user namespaces. A test of the gate calls
    :meth:`SandboxProbe.unsupported`; a test of the real probe calls
    :func:`autoforge.sandbox_bwrap.probe` itself.
    """
    import autoforge.engine as engine_module

    probe = SandboxProbe()
    monkeypatch.setattr(engine_module, "probe_sandbox", probe)
    return probe


# The modules that test durability itself, and so write through the real
# ``os.fsync`` (see ``_fsync_without_the_disk``).
REAL_FSYNC_MODULES = frozenset(
    {
        "test_durable_run_contract.py",
        "test_run_namespace.py",
        "test_runlog.py",
        "test_safefs.py",
        "test_scheduler_state.py",
        "test_state.py",
    }
)


def fsync_without_the_disk(fd) -> None:
    """``os.fsync`` without the wait for the disk: it refuses what fsync refuses.

    An open descriptor of a regular file, a directory or a block device is
    accepted and nothing is written back; a closed descriptor or an
    ``O_PATH`` one raises ``EBADF``, and a pipe, a socket or a character
    device ``EINVAL``, as ``fsync(2)`` does (``tests/test_safefs.py`` pins
    each answer to the real call's).
    """
    import errno
    import fcntl
    import stat

    fd = fd if isinstance(fd, int) else fd.fileno()
    if fcntl.fcntl(fd, fcntl.F_GETFL) & getattr(os, "O_PATH", 0):
        raise OSError(errno.EBADF, os.strerror(errno.EBADF))
    mode = os.fstat(fd).st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISBLK(mode)):
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))


@pytest.fixture(autouse=True)
def _fsync_without_the_disk(request, monkeypatch):
    """The controller's ``fsync`` calls do not wait for the disk (#341).

    A state save, a run log entry and every safe write fsync a file and its
    directory, about 40 calls for an engine test: where the temporary
    directory is on a disk, waiting for them took about half the wall time
    of the ``tests/test_engine.py`` tests measured. No test can observe what
    they buy, which is the bytes surviving a power loss: a process a test
    kills, or a crash it raises, leaves the same files whether they were
    flushed or not. So ``os.fsync`` here refuses what the real call refuses
    and flushes nothing (:func:`fsync_without_the_disk`), except in the
    modules of :data:`REAL_FSYNC_MODULES`, which test the durable writes
    themselves and keep it. A test that patches ``os.fsync`` (to fail it,
    or to hook a step after it) patches over this one. Only this process is
    affected: git, a scheduler worker and any other child fsync as they
    always do.
    """
    if request.path.name in REAL_FSYNC_MODULES:
        return
    monkeypatch.setattr(os, "fsync", fsync_without_the_disk)


@pytest.fixture
def offline_fetches() -> list[list[str]]:
    """The fetches of this test's engines that have no origin, in order."""
    return _OFFLINE_FETCHES


def offline_tree(sha: str) -> str:
    """The tree an offline-fetched stand-in commit names (see ``_no_network_git_remote``)."""
    import hashlib

    digest = hashlib.sha256(f"tree of {sha}".encode()).hexdigest()
    return digest[: len(sha)]


# Every test's tmp_path, and its removal once the test has passed (#396). pytest's
# own keeps a test's directory only when its call phase failed, not when one of
# its fixtures failed in setup or teardown; a fixture here overrides pytest's.
tmp_path = tmp_path_retention.tmp_path
pytest_runtest_makereport = tmp_path_retention.pytest_runtest_makereport


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
    """Commit everything in ``repo`` (tests need a clean baseline tree).

    No automatic maintenance, for the reason :func:`git_repo` gives.
    """
    root = Path(repo)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            *NO_AUTO_MAINTENANCE,
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


# -- what a launched process sees (#165) ----------------------------------------

# A command that records, from inside the child, what the controller handed
# it: the names of its environment (never a value), whether the gh
# configuration and token are the controller's, whether curl and wget
# find the controller's empty configuration files, whether zsh's startup
# directory is the controller's empty one, and the shell's name.
_ENV_PROBE = (
    "import json, os, sys; "
    "d = os.environ.get('GH_CONFIG_DIR', ''); "
    "c = os.path.join(os.environ.get('CURL_HOME', ''), '.curlrc'); "
    "w = os.environ.get('WGETRC', ''); "
    "open(sys.argv[1], 'a').write(json.dumps({'names': sorted(os.environ), "
    "'gh_token_placeholder': os.environ.get('GH_TOKEN') == 'autoforge-no-github-credential', "
    "'gh_config_dir_empty': bool(d) and os.path.isdir(d) and not os.listdir(d), "
    "'http_client_config_empty': os.environ.get('WGET2RC') == w and all("
    "os.path.isfile(f) and 'header' not in open(f).read() for f in (c, w)), "
    "'git_terminal_prompt': os.environ.get('GIT_TERMINAL_PROMPT'), "
    "'zdotdir_empty': os.path.isdir(os.environ.get('ZDOTDIR', '')) "
    "and not os.listdir(os.environ['ZDOTDIR']), "
    "'shell': os.path.basename(os.environ.get('SHELL', ''))}) + chr(10))"
)


def env_probe_command(record: Path) -> list[str]:
    """A validation or verification command that appends what it saw to ``record``."""
    return [sys.executable, "-c", _ENV_PROBE, str(record)]


def env_probe_records(record: Path) -> list[dict]:
    return [json.loads(line) for line in record.read_text().splitlines() if line]


def is_shell_probe(req) -> bool:
    """Whether ``req`` is one of the launch preflight's login shells (#165,
    ``child_env.shell_startup_problems``). A fake runner hands it to the real
    executor: it reads the empty operator home of ``_no_operator_netrc``."""
    from autoforge.child_env import SHELL_PROBE_MARKER

    return list(req.command[1:3]) == ["-l", "-c"] and SHELL_PROBE_MARKER in req.command[-1]


def pollute_operator_environment(monkeypatch, tmp_path: Path) -> None:
    """Every GitHub or git credential route an operator's shell may hold, with fake values."""
    token = "ghp_" + "E" * 36
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, token)
    monkeypatch.setenv("GH_HOST", "github.com")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "operator-gh"))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    monkeypatch.setenv("GIT_ASKPASS", "/bin/false")
    monkeypatch.setenv("SSH_ASKPASS", "/bin/false")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /operator/key")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={tmp_path / 'bus'}")
    # curl's and wget's configuration, each adding the token as a header.
    curl_home = tmp_path / "operator-curl"
    curl_home.mkdir(exist_ok=True)
    (curl_home / ".curlrc").write_text(f'header = "Authorization: Bearer {token}"\n')
    (tmp_path / "operator-wgetrc").write_text(f"header = Authorization: Bearer {token}\n")
    monkeypatch.setenv("CURL_HOME", str(curl_home))
    monkeypatch.setenv("WGETRC", str(tmp_path / "operator-wgetrc"))
    monkeypatch.setenv("WGET2RC", str(tmp_path / "operator-wgetrc"))
    # A .netrc curl, requests and Go read in place of ~/.netrc.
    (tmp_path / "operator-netrc").write_text(f"machine github.com login x password {token}\n")
    monkeypatch.setenv("NETRC", str(tmp_path / "operator-netrc"))
    # Where a shell finds a startup file, and a shell that reads its
    # configuration on every start.
    monkeypatch.setenv("BASH_ENV", str(tmp_path / "operator-bash-env"))
    monkeypatch.setenv("ENV", str(tmp_path / "operator-env"))
    monkeypatch.setenv("ZDOTDIR", str(tmp_path / "operator-zsh"))
    monkeypatch.setenv("SHELL", "/usr/bin/fish")


def assert_isolated_names(names) -> None:
    """``names`` (a child's environment) hold no credential route of the
    operator's: every forbidden name present is one the controller sets."""
    from autoforge.child_env import forbidden_env_entry, override_names

    held = {name for name in names if forbidden_env_entry(name)}
    assert held == {name for name in override_names() if forbidden_env_entry(name)}, held
    for name in (
        "GITHUB_TOKEN",
        "GH_HOST",
        "SSH_AUTH_SOCK",
        "GIT_ASKPASS",
        "XDG_RUNTIME_DIR",
        "BASH_ENV",
        "ENV",
    ):
        assert name not in names, name


def launch_environment(eng, req) -> dict[str, str]:
    """What the executor hands the CLI of ``req``: allow-list plus the controller's overrides."""
    from autoforge.executor import select_environment

    provider = eng.providers.get(req.profile)
    env = select_environment(provider.environment_allowlist(req))
    env.update(req.env or {})
    return env


def assert_isolated_launch(eng, req) -> None:
    """Called from a scripted agent while it runs: the launch carries no
    credential route, and the controller's gh, git, curl and wget
    configuration exist."""
    from autoforge.child_env import GH_PLACEHOLDER_TOKEN, HTTP_CLIENT_CONFIG, POSIX_SHELLS

    env = launch_environment(eng, req)
    assert_isolated_names(env)
    assert env["GH_TOKEN"] == env["GH_ENTERPRISE_TOKEN"] == GH_PLACEHOLDER_TOKEN
    gh_dir = Path(env["GH_CONFIG_DIR"])
    assert gh_dir.is_dir() and list(gh_dir.iterdir()) == []
    assert Path(env["GIT_CONFIG_GLOBAL"]).is_file()
    assert (Path(env["CURL_HOME"]) / ".curlrc").read_text() == HTTP_CLIENT_CONFIG
    assert env["WGET2RC"] == env["WGETRC"]
    assert Path(env["WGETRC"]).read_text() == HTTP_CLIENT_CONFIG
    # zsh finds no startup file, and the shell is one that reads none.
    zdotdir = Path(env["ZDOTDIR"])
    assert zdotdir.is_dir() and list(zdotdir.iterdir()) == []
    assert Path(env["SHELL"]).name in POSIX_SHELLS, env["SHELL"]
    helpers = subprocess.run(
        ["git", "config", "--get-all", "credential.helper"],
        cwd=req.cwd,
        env=env,
        capture_output=True,
        text=True,
    ).stdout.split("\n")[:-1]
    assert helpers[-1:] == [""], helpers


_READ_CONTEXT_RE = re.compile(r"outside your worktree:\n\n`([^`]+)`")


def read_context_of(prompt: str) -> Path:
    """The read-context directory a REMOTE prompt names (#165)."""
    match = _READ_CONTEXT_RE.search(prompt)
    assert match, "the prompt names no read context"
    return Path(match.group(1))
