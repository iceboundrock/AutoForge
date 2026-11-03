"""Where an unjournaled review comment may have come from: ``status``'s read-only report (#416).

REVIEW blocks on a round comment the controller did not journal (ADR 0004
D9.6), and its reason can name only the comment: the controller cannot tell
who posted it (D9.3). The operator, every AutoForge controller they run
and an agent that still holds their credential post as the same GitHub
login, and the comment may be another controller's own journaled review.
Telling which applies took reading state files, run logs, agent
transcripts, git objects and GitHub timelines by hand (#387). Most of the
facts that settle it are ones the controller can read itself, and this
module gathers them for ``status``:

- each such comment: when GitHub says it was created, set against this
  run's creation and its most recent reviewer launches (from the run log's
  ``execution.json``), the login GitHub names, its marker, the digest and
  length of its body and a bounded excerpt;
- which state directories on this checkout account for it: every state
  directory found in or directly under the directories the caller names
  (``status`` names the state directory it reads, the configured
  ``state_dir`` and, when the two differ, the directory the first lies in),
  and every scheduler namespace under the git common dir. A state accounts
  for the comment with a ``review_comment`` effect record (K4) for the
  comment's marker or observed at it, or with ``last_review_comment_url``
  naming it; its ``review_history`` recording the comment's round at the
  comment's HEAD is listed too, as weaker evidence (a round, not the
  comment). The states bound to the PR are listed whatever they hold;
- the PR's other ``ai-review-result`` markers, with their rounds and HEADs;
- the commits GitHub lists for the PR committed after the earliest such
  comment, with author and committer identity and the time after it.
  GitHub records when a commit was made, never when it was pushed;
- the PR's ``ai-implementation`` marker, and its HEAD now. No owner of a
  PR is recorded on GitHub yet (#414 proposes one): the states bound to the
  PR are what this checkout knows of it.

It presents evidence and names no author, and nothing it reports is read by
anything that decides: the engine never imports it. It is not a remedy
either: the block's own reason says what to do.

**Read-only.** GitHub is read through the client handed in (the PR's
comments; when a comment is found, the PR and its commits), and the local
files through :class:`~autoforge.safefs.SafeRoot` and the strict loaders
(:func:`~autoforge.state.load_state`, #178's namespace lookup), which
create, quarantine, lock and write nothing. A read that fails is reported in
the report, never raised. Comment bodies, commit messages, names and e-mail
addresses are untrusted text: redacted, put on one line and bounded, as the
read context bounds them (``read_context.py``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .claims import IMPLEMENTATION, REVIEW, ReviewClaim, scan
from .errors import AutoForgeError, ConfigurationError, StateError
from .github import CommentInfo, CommitPerson, PRCommitInfo, PRInfo
from .redaction import redact
from .run_namespace import StateStatus, list_issue_namespaces, load_issue_namespace
from .runlog import MAX_RUN_LOG_ENTRIES
from .safefs import ReadLimitExceeded, SafeRoot, WalkBudgetExceeded
from .state import STATE_FILENAME, AutoForgeState, load_state
from .transitions import Phase
from .validation import GitHubPullRequestRef, parse_comment_url, parse_pr_url

# A comment body's excerpt, a commit's headline, a name, e-mail or login:
# untrusted text, shown on one line and cut here.
MAX_EXCERPT_CHARS = 240
MAX_HEADLINE_CHARS = 160
MAX_IDENTITY_CHARS = 120
# A read that failed, as the report quotes it.
MAX_PROBLEM_CHARS = 300
# How many of the PR's other review markers, and of the commits after the
# comment, are shown; the report says how many there are.
MAX_LISTED_MARKERS = 40
MAX_LISTED_COMMITS = 40
# The run's most recent reviewer launches shown.
MAX_REVIEW_LAUNCHES = 5
# Entries listed in one directory searched for state directories.
MAX_DIR_ENTRIES = 2000
# An ``execution.json`` larger than this is not read for its start time.
MAX_EXECUTION_BYTES = 4 * 1024 * 1024

_REVIEW_STEP_RE = re.compile(r"^(\d+)-review-(\d+)$")
_K4_KIND = "review_comment"

NOTE = (
    "Evidence only: the controller cannot tell who posted a comment (ADR 0004 D9.3), since the "
    "operator, every AutoForge controller they run and an agent that still holds their "
    "credential post as the same GitHub login. Nothing was adopted, deleted or written."
)


class ProvenanceReads(Protocol):
    """The GitHub reads the report makes; :class:`~autoforge.github.GitHubClient` has them."""

    def get_pr_comments(self, url: str) -> list[CommentInfo]: ...

    def get_pr(self, url: str) -> PRInfo: ...

    def get_pr_commits(self, url: str) -> list[PRCommitInfo]: ...


# -- untrusted text -----------------------------------------------------------------------


def one_line(text: str, limit: int) -> str:
    """``text`` redacted, on one line (controls are spaces, runs collapse), cut at ``limit``."""
    clean = redact(text or "")
    clean = "".join(ch if ch.isprintable() else " " for ch in clean)
    clean = " ".join(clean.split())
    if len(clean) <= limit:
        return clean
    return clean[:limit] + f" [... {len(clean) - limit} more characters]"


def _problem(exc: BaseException) -> str:
    return one_line(str(exc), MAX_PROBLEM_CHARS) or type(exc).__name__


def _person(person: CommitPerson) -> dict:
    return {
        "name": one_line(person.name, MAX_IDENTITY_CHARS),
        "email": one_line(person.email, MAX_IDENTITY_CHARS),
        "login": one_line(person.login, MAX_IDENTITY_CHARS),
    }


def _person_text(person: dict) -> str:
    who = person["name"] or "(no name)"
    if person["email"]:
        who += f" <{person['email']}>"
    return f"{who}, login {person['login'] or '(none)'}"


# -- time -----------------------------------------------------------------------------------


def seconds_between(earlier: str, later: str) -> int | None:
    """Whole seconds from ``earlier`` to ``later`` (ISO 8601 with a zone), else ``None``."""
    try:
        start = datetime.fromisoformat(earlier)
        end = datetime.fromisoformat(later)
    except (TypeError, ValueError):
        return None
    if start.tzinfo is None or end.tzinfo is None:
        return None
    return int((end - start).total_seconds())


def offset_text(seconds: int | None) -> str:
    """``+2h03m05s``, ``-41s``; ``?`` when either time could not be read."""
    if seconds is None:
        return "?"
    sign = "+" if seconds >= 0 else "-"
    rest = abs(seconds)
    days, rest = divmod(rest, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{sign}{days}d{hours:02d}h{minutes:02d}m{secs:02d}s"
    if hours:
        return f"{sign}{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{sign}{minutes}m{secs:02d}s"
    return f"{sign}{secs}s"


# -- the state directories of this checkout ---------------------------------------------------


@dataclass(frozen=True)
class ScannedState:
    """One state directory read: its state, or why it could not be read."""

    state_dir: str
    namespace: int | None  # the scheduler namespace's issue; None for a standalone one
    state: AutoForgeState | None
    problem: str = ""
    current: bool = False  # the state ``status`` reads

    @property
    def where(self) -> str:
        if self.current:
            return "the state status reads"
        if self.namespace is not None:
            return f"scheduler namespace of issue #{self.namespace}"
        return "standalone"


@dataclass(frozen=True)
class StateScan:
    """Every state directory found on this checkout, and what could not be searched."""

    states: tuple[ScannedState, ...]
    roots: tuple[str, ...]  # the directories searched for standalone state directories
    namespaces_read: bool
    problems: tuple[str, ...]

    @property
    def loaded(self) -> tuple[ScannedState, ...]:
        return tuple(s for s in self.states if s.state is not None)

    @property
    def unreadable(self) -> tuple[ScannedState, ...]:
        return tuple(s for s in self.states if s.state is None)


def _load_in(root: SafeRoot) -> tuple[AutoForgeState | None, str]:
    """The state held directly in ``root``: ``(None, "")`` when there is none."""
    if root.lstat(STATE_FILENAME) is None:
        return None, ""
    try:
        return load_state(root.path / STATE_FILENAME, root=root), ""
    except StateError as exc:
        return None, _problem(exc)


def scan_state_dirs(
    *,
    current_dir: Path,
    current: AutoForgeState,
    roots: Sequence[Path],
    coordination_root: Path | None,
    coordination_problem: str = "",
) -> StateScan:
    """Every state directory this checkout holds that ``status`` can find, read-only.

    The standalone ones are each of ``roots`` that holds a ``state.json``,
    and every directory directly under one of them that does; links are
    never followed, and a directory holding more than
    :data:`MAX_DIR_ENTRIES` entries is reported, not searched. The scheduler
    namespaces are every issue namespace under ``coordination_root``
    (``None`` outside a git repository, ``coordination_problem`` saying
    why). ``current`` is the state already loaded from ``current_dir``: it is
    not read again, and a directory found again under another name is the
    same directory.
    """
    states: list[ScannedState] = []
    problems: list[str] = []
    seen: set[tuple[int, int]] = set()
    seen_paths: set[str] = set()
    try:
        with SafeRoot.open(current_dir) as here:
            seen.add(here.identity)
    except (FileNotFoundError, StateError, OSError) as exc:  # pragma: no cover - status loaded it
        problems.append(f"{current_dir}: {_problem(exc)}")
    seen_paths.add(str(Path(current_dir).resolve()))
    states.append(ScannedState(str(current_dir), None, current, current=True))

    def consider(root: SafeRoot) -> None:
        if root.identity in seen:
            return
        seen.add(root.identity)
        try:
            state, problem = _load_in(root)
        except StateError as exc:
            state, problem = None, _problem(exc)
        if state is not None or problem:
            seen_paths.add(str(Path(root.path).resolve()))
            states.append(ScannedState(str(root.path), None, state, problem))

    searched: list[str] = []
    for given in roots:
        path = Path(given)
        if str(path) in searched:
            continue
        searched.append(str(path))
        try:
            root = SafeRoot.open(path)
        except FileNotFoundError:
            continue
        except (StateError, OSError) as exc:
            problems.append(f"{path}: {_problem(exc)}")
            continue
        with root:
            consider(root)
            names: list[str] = []
            try:
                for entry in root.walk(max_entries=MAX_DIR_ENTRIES):
                    entry.skip = True  # one level: a state directory's own contents are its own
                    if entry.is_dir:
                        names.append(entry.name)
            except WalkBudgetExceeded:
                problems.append(
                    f"{path} holds more than {MAX_DIR_ENTRIES} entries; the directories under "
                    "it were not searched"
                )
            except StateError as exc:
                problems.append(f"{path}: {_problem(exc)}")
            for name in sorted(names):
                try:
                    with root.subroot(name) as sub:
                        consider(sub)
                except FileNotFoundError:
                    continue
                except (StateError, OSError):
                    # Not a directory status could open: no state directory it can name.
                    continue

    namespaces_read = False
    if coordination_root is None:
        why = one_line(coordination_problem, MAX_PROBLEM_CHARS)
        problems.append("the scheduler namespaces were not read" + (f": {why}" if why else ""))
    else:
        try:
            listing = list_issue_namespaces(coordination_root)
        except (StateError, OSError) as exc:
            problems.append(f"the scheduler namespaces were not read: {_problem(exc)}")
        else:
            namespaces_read = True
            for rejected in listing.rejected:
                problems.append(
                    f"scheduler namespace entry {one_line(rejected.name, 80)!r} was not read: "
                    f"{one_line(rejected.reason, MAX_PROBLEM_CHARS)}"
                )
            for namespace in (*listing.bound, *listing.unbound):
                where = str(namespace.state_dir)
                if str(Path(where).resolve()) in seen_paths:
                    continue
                try:
                    lookup = load_issue_namespace(coordination_root, namespace.issue_number)
                except (StateError, OSError) as exc:
                    states.append(ScannedState(where, namespace.issue_number, None, _problem(exc)))
                    continue
                if lookup.state.status is StateStatus.LOADED:
                    states.append(ScannedState(where, namespace.issue_number, lookup.state.state))
                elif lookup.state.status is StateStatus.CORRUPT:
                    reason = one_line(lookup.state.reason, MAX_PROBLEM_CHARS)
                    states.append(ScannedState(where, namespace.issue_number, None, reason))
    return StateScan(tuple(states), tuple(searched), namespaces_read, tuple(problems))


# -- what one state holds about a comment -------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One thing a state holds about a comment."""

    kind: str  # "effect_record" | "last_review_comment_url" | "review_history"
    detail: str

    @property
    def journals(self) -> bool:
        """Whether this accounts for the comment itself, not only for its round."""
        return self.kind != "review_history"


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _same_comment(url: str, other: str) -> bool:
    try:
        return parse_comment_url(url).same_target(parse_comment_url(other))
    except ConfigurationError:
        return False


def _same_pr(url: object, pr: GitHubPullRequestRef) -> bool:
    if not isinstance(url, str) or not url:
        return False
    try:
        return parse_pr_url(url).same_target(pr)
    except ConfigurationError:
        return False


def journal_evidence(
    state: AutoForgeState,
    pr: GitHubPullRequestRef,
    comment: CommentInfo,
    claim: ReviewClaim | None,
) -> tuple[Evidence, ...]:
    """What ``state`` holds that accounts for ``comment`` (``claim`` its marker, if readable)."""
    found: list[Evidence] = []
    digest = _sha256(comment.body)
    for raw in state.effect_records if isinstance(state.effect_records, list) else ():
        if not isinstance(raw, dict) or raw.get("kind") != _K4_KIND:
            continue
        identity = raw.get("identity")
        if not isinstance(identity, dict) or not _same_pr(identity.get("pr_url"), pr):
            continue
        observed = raw.get("observed")
        observed_url = observed.get("url", "") if isinstance(observed, dict) else ""
        at_comment = isinstance(observed_url, str) and _same_comment(observed_url, comment.url)
        marker = identity.get("marker")
        keys = {c.key for c in scan(REVIEW, marker).claims} if isinstance(marker, str) else set()
        same_key = claim is not None and claim.key in keys
        if not (at_comment or same_key):
            continue
        stage = one_line(str(raw.get("stage", "?")), 40)
        detail = f"a review_comment effect record (K4), {stage}, for "
        detail += "this comment's marker" if same_key else "another marker"
        if at_comment:
            detail += ", observed at this comment"
        elif isinstance(observed_url, str) and observed_url:
            detail += f", observed at {one_line(observed_url, MAX_PROBLEM_CHARS)}"
        payload = raw.get("payload")
        body = payload.get("body") if isinstance(payload, dict) else None
        if isinstance(body, str):
            same = _sha256(body) == digest
            detail += "; its payload " + (
                "is this comment's body byte for byte"
                if same
                else "differs from this comment's body"
            )
        found.append(Evidence("effect_record", detail))
    if state.last_review_comment_url and _same_comment(state.last_review_comment_url, comment.url):
        found.append(
            Evidence(
                "last_review_comment_url",
                f"last_review_comment_url names this comment (review round {state.review_round} "
                "completed)",
            )
        )
    if claim is not None and _same_pr(state.current_pr_url, pr):
        for entry in state.review_history if isinstance(state.review_history, list) else ():
            if not isinstance(entry, dict) or entry.get("round") != claim.round:
                continue
            head = entry.get("reviewed_head_sha")
            if not isinstance(head, str) or head.lower() != claim.reviewed_head_sha:
                continue
            result = one_line(str(entry.get("result", "?")), 40)
            found.append(
                Evidence(
                    "review_history",
                    f"review_history records round {claim.round} at HEAD {head.lower()[:12]} "
                    f"({result}): the round, not this comment",
                )
            )
    return tuple(found)


@dataclass(frozen=True)
class JournalMatch:
    """A state directory holding something about a comment."""

    scanned: ScannedState
    evidence: tuple[Evidence, ...]

    @property
    def journaled(self) -> bool:
        return any(e.journals for e in self.evidence)

    def document(self) -> dict:
        state = self.scanned.state
        assert state is not None
        return {
            "state_dir": self.scanned.state_dir,
            "namespace_issue": self.scanned.namespace,
            "current": self.scanned.current,
            "run_id": state.run_id,
            "phase": state.phase.value,
            "journaled": self.journaled,
            "evidence": [{"kind": e.kind, "detail": e.detail} for e in self.evidence],
        }


# -- the report -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Launch:
    """A reviewer launch of this run, from its step directory in the run log."""

    step: str
    started_at: str  # "" when the step directory holds no readable execution.json


def _claim_document(claim: ReviewClaim | None) -> dict | None:
    if claim is None:
        return None
    return {
        "round": claim.round,
        "reviewed_head_sha": claim.reviewed_head_sha,
        "reviewed_base_ref": claim.reviewed_base_ref,
        "reviewed_merge_base_sha": claim.reviewed_merge_base_sha,
        "needs_fix_round": claim.needs_fix_round,
        "finding_ids": list(claim.finding_ids) if claim.finding_ids is not None else None,
    }


def _claim_text(claim: ReviewClaim | None) -> str:
    if claim is None:
        return "no readable marker"
    base = repr(claim.reviewed_base_ref) if claim.reviewed_base_ref is not None else "(none)"
    merge_base = (claim.reviewed_merge_base_sha or "")[:12] or "(none)"
    findings = ", ".join(claim.finding_ids or ()) or "none"
    return (
        f"round {claim.round} at HEAD {claim.reviewed_head_sha[:12]} on base {base} from merge "
        f"base {merge_base}, needs_fix_round {str(claim.needs_fix_round).lower()}, findings "
        f"{one_line(findings, MAX_EXCERPT_CHARS)}"
    )


@dataclass(frozen=True)
class MarkedComment:
    """A PR comment carrying an ``ai-review-result`` marker, readable or not."""

    comment: CommentInfo
    claim: ReviewClaim | None
    defects: tuple[str, ...]

    def document(self) -> dict:
        return {
            "url": self.comment.url,
            "created_at": one_line(self.comment.created_at, 64),
            "login": one_line(self.comment.author, MAX_IDENTITY_CHARS),
            "marker": _claim_document(self.claim),
            "defects": [one_line(d, MAX_PROBLEM_CHARS) for d in self.defects],
        }


@dataclass(frozen=True)
class RoundComment:
    """One comment the block is about, and what was found about it."""

    marked: MarkedComment
    journals: tuple[JournalMatch, ...]
    seconds_after_run_created: int | None
    seconds_after_launches: tuple[tuple[str, int | None], ...]

    def document(self) -> dict:
        comment = self.marked.comment
        return {
            **self.marked.document(),
            "body_sha256": _sha256(comment.body),
            "body_chars": len(comment.body or ""),
            "excerpt": one_line(comment.body, MAX_EXCERPT_CHARS),
            "seconds_after_run_created": self.seconds_after_run_created,
            "seconds_after_launches": [
                {"step": step, "seconds": seconds} for step, seconds in self.seconds_after_launches
            ],
            "journals": [m.document() for m in self.journals],
        }


@dataclass(frozen=True)
class ProvenanceReport:
    """``status``'s report of the unjournaled round comments, or why it could not be made.

    ``checked`` is False when the PR's comments could not be read, and
    ``problem`` says why; nothing else is then known.
    """

    checked: bool
    problem: str = ""
    pr_url: str = ""
    round: int = 0
    head: str = ""
    base: str = ""
    merge_base: str = ""
    run_created_at: str = ""
    launches: tuple[Launch, ...] = ()
    comments: tuple[RoundComment, ...] = ()
    markers: tuple[MarkedComment, ...] = ()
    scan: StateScan | None = None
    states_on_pr: tuple[ScannedState, ...] = ()
    commits_listed: int | None = None  # None: not read
    commits_after: tuple[tuple[PRCommitInfo, int | None], ...] = ()
    commits_after_total: int = 0
    pr_head_sha: str = ""
    implementation_issues: tuple[str, ...] = ()
    implementation_defects: tuple[str, ...] = ()
    implementation_read: bool = False
    problems: tuple[str, ...] = field(default_factory=tuple)

    # -- status --json ----------------------------------------------------------------------
    def document(self) -> dict:
        if not self.checked:
            return {"checked": False, "problem": self.problem}
        scan_ = self.scan
        return {
            "checked": True,
            "problem": "",
            "pr_url": self.pr_url,
            "round": self.round,
            "head": self.head,
            "base": self.base,
            "merge_base": self.merge_base,
            "pr_head_sha": self.pr_head_sha,
            "run_created_at": self.run_created_at,
            "reviewer_launches": [
                {"step": launch.step, "started_at": launch.started_at} for launch in self.launches
            ],
            "comments": [c.document() for c in self.comments],
            "state_directories": {
                "read": len(scan_.loaded) if scan_ else 0,
                "searched": list(scan_.roots) if scan_ else [],
                "namespaces_read": scan_.namespaces_read if scan_ else False,
                "unreadable": [
                    {"state_dir": s.state_dir, "problem": s.problem}
                    for s in (scan_.unreadable if scan_ else ())
                ],
                "problems": list(scan_.problems) if scan_ else [],
                "bound_to_pr": [_state_on_pr(s) for s in self.states_on_pr],
            },
            "review_markers": {
                "total": len(self.markers),
                "shown": [m.document() for m in self.markers[-MAX_LISTED_MARKERS:]],
            },
            "commits": {
                "listed": self.commits_listed,
                "after_comment": self.commits_after_total,
                "shown": [
                    {
                        "oid": commit.oid,
                        "authored_at": one_line(commit.authored_at, 64),
                        "committed_at": one_line(commit.committed_at, 64),
                        "seconds_after_comment": seconds,
                        "author": _person(commit.author),
                        "committer": _person(commit.committer),
                        "headline": one_line(commit.headline, MAX_HEADLINE_CHARS),
                    }
                    for commit, seconds in self.commits_after
                ],
            },
            "implementation_marker": (
                {
                    "issues": list(self.implementation_issues),
                    "defects": [
                        one_line(d, MAX_PROBLEM_CHARS) for d in self.implementation_defects
                    ],
                }
                if self.implementation_read
                else None
            ),
            "problems": list(self.problems),
            "note": NOTE,
        }

    # -- status -------------------------------------------------------------------------------
    def lines(self) -> list[str]:
        if not self.checked:
            return [
                "",
                f"Unjournaled review comment: not checked ({self.problem}); the PR's comments "
                "could not be read",
            ]
        pr_number = self.pr_url.rstrip("/").rsplit("/", 1)[-1]
        lines = [
            "",
            f"Unjournaled review comment(s) on PR #{pr_number} for round {self.round} at HEAD "
            f"{self.head[:12]} (ADR 0004 D9.6): what GitHub and this checkout's state "
            f"directories hold. {NOTE}",
        ]
        for item in self.comments:
            lines += self._comment_lines(item)
        lines += self._scan_lines()
        lines += self._marker_lines()
        lines += self._commit_lines()
        lines += self._pr_lines()
        for problem in self.problems:
            lines.append(f"  Not read:  {problem}")
        return lines

    def _comment_lines(self, item: RoundComment) -> list[str]:
        comment = item.marked.comment
        created = one_line(comment.created_at, 64) or "(unknown)"
        login = one_line(comment.author, MAX_IDENTITY_CHARS) or "(none)"
        lines = [
            f"  Comment:   {comment.url}",
            f"    created:   {created}, GitHub login {login}; "
            f"{offset_text(item.seconds_after_run_created)} after this run was created "
            f"({self.run_created_at or '(unknown)'})",
        ]
        if item.seconds_after_launches:
            launches = "; ".join(
                f"{offset_text(seconds)} after reviewer launch {step}"
                for step, seconds in item.seconds_after_launches
            )
            lines.append(f"    launches:  {launches}")
        else:
            lines.append("    launches:  no reviewer launch of this run is in its run log")
        lines.append(f"    marker:    {_claim_text(item.marked.claim)}")
        for defect in item.marked.defects:
            lines.append(f"    defect:    {one_line(defect, MAX_PROBLEM_CHARS)}")
        lines.append(
            f"    body:      {len(comment.body or '')} characters, sha256 "
            f"{_sha256(comment.body)}; begins: {one_line(comment.body, MAX_EXCERPT_CHARS)!r}"
        )
        journaled = [m for m in item.journals if m.journaled]
        if not journaled:
            read = len(self.scan.loaded) if self.scan else 0
            lines.append(
                f"    journal:   none of the {read} state directories read accounts for it"
            )
        for match in item.journals:
            state = match.scanned.state
            assert state is not None
            label = "journal:  " if match.journaled else "recorded: "
            lines.append(
                f"    {label} {match.scanned.state_dir} ({match.scanned.where}; run "
                f"{state.run_id}, phase {state.phase.value}): "
                + "; ".join(e.detail for e in match.evidence)
            )
        return lines

    def _scan_lines(self) -> list[str]:
        scan_ = self.scan
        if scan_ is None:
            return []
        namespaces = "and the scheduler namespaces"
        if not scan_.namespaces_read:
            namespaces = "(the scheduler namespaces were not read)"
        lines = [
            f"  States:    {len(scan_.loaded)} state directories read, under "
            f"{', '.join(scan_.roots) or '(none)'} {namespaces}"
        ]
        for scanned in scan_.unreadable:
            lines.append(f"    unreadable: {scanned.state_dir}: {scanned.problem}")
        for problem in scan_.problems:
            lines.append(f"    not searched: {problem}")
        for scanned in self.states_on_pr:
            state = scanned.state
            assert state is not None
            lines.append(
                f"    bound to the PR: {scanned.state_dir} ({scanned.where}; run "
                f"{state.run_id}, phase {state.phase.value}, {state.review_round} review "
                f"round(s) completed, HEAD {state.current_head_sha[:12] or '-'})"
            )
        return lines

    def _marker_lines(self) -> list[str]:
        if not self.markers:
            return ["  Markers:   the PR carries no other ai-review-result marker"]
        shown = self.markers[-MAX_LISTED_MARKERS:]
        lines = [
            f"  Markers:   {len(self.markers)} other comment(s) of the PR carry an "
            "ai-review-result marker"
            + (f" (the {len(shown)} newest shown)" if len(shown) < len(self.markers) else "")
        ]
        for marked in shown:
            created = one_line(marked.comment.created_at, 64) or "(unknown)"
            text = _claim_text(marked.claim)
            if marked.defects:
                text += "; " + "; ".join(one_line(d, MAX_PROBLEM_CHARS) for d in marked.defects)
            lines.append(f"    {marked.comment.url} ({created}): {text}")
        return lines

    def _commit_lines(self) -> list[str]:
        if self.commits_listed is None:
            return []
        lines = [
            f"  Commits:   {self.commits_after_total} of the {self.commits_listed} commit(s) "
            "GitHub lists for the PR were committed after the comment (GitHub records when a "
            "commit was made, not when it was pushed)"
            + (
                f"; the first {len(self.commits_after)} shown"
                if len(self.commits_after) < self.commits_after_total
                else ""
            )
        ]
        for commit, seconds in self.commits_after:
            author = _person_text(_person(commit.author))
            committer = _person_text(_person(commit.committer))
            lines.append(
                f"    {commit.oid[:12]} {offset_text(seconds)}: author {author}; committer "
                f"{committer}: {one_line(commit.headline, MAX_HEADLINE_CHARS)!r}"
            )
        return lines

    def _pr_lines(self) -> list[str]:
        lines = []
        if self.implementation_read:
            if self.implementation_issues:
                text = "names " + ", ".join(self.implementation_issues)
            else:
                text = "none"
            if self.implementation_defects:
                text += "; " + "; ".join(
                    one_line(d, MAX_PROBLEM_CHARS) for d in self.implementation_defects
                )
            lines.append(f"  PR marker: ai-implementation {text}")
        if self.pr_head_sha:
            lines.append(f"  PR HEAD:   {self.pr_head_sha} now (the round is bound to {self.head})")
        return lines


def _state_on_pr(scanned: ScannedState) -> dict:
    state = scanned.state
    assert state is not None
    return {
        "state_dir": scanned.state_dir,
        "namespace_issue": scanned.namespace,
        "run_id": state.run_id,
        "phase": state.phase.value,
        "review_round": state.review_round,
        "current_head_sha": state.current_head_sha,
    }


# -- reading it -------------------------------------------------------------------------------


def applies(state: AutoForgeState) -> bool:
    """Whether ``status`` looks for unjournaled round comments: a BLOCKED run with a bound round."""
    return (
        state.phase is Phase.BLOCKED
        and bool(state.current_pr_url)
        and bool(state.current_head_sha)
        and bool(state.current_base_ref)
        and bool(state.current_merge_base_sha)
    )


def reviewer_launches(logs_dir: Path, run_id: str) -> tuple[tuple[Launch, ...], str]:
    """The run's most recent reviewer launches in its run log, and a problem reading it.

    A step directory ``<seq>-review-<attempt>`` is one launch; its start is
    ``started_at`` of its ``execution.json``, "" when that is absent (an
    interrupted launch) or unreadable. Read-only; at most
    :data:`MAX_REVIEW_LAUNCHES`, the newest last.
    """
    try:
        logs = SafeRoot.open(logs_dir)
    except FileNotFoundError:
        return (), ""
    except (StateError, OSError) as exc:
        return (), f"the run log was not read: {_problem(exc)}"
    with logs:
        try:
            run = logs.subroot(run_id)
        except FileNotFoundError:
            return (), ""
        except (StateError, OSError) as exc:
            return (), f"the run log was not read: {_problem(exc)}"
    steps: list[tuple[int, str]] = []
    with run:
        try:
            for entry in run.walk(max_entries=MAX_RUN_LOG_ENTRIES):
                entry.skip = True
                match = _REVIEW_STEP_RE.match(entry.name)
                if match and entry.is_dir:
                    steps.append((int(match.group(1)), entry.name))
        except StateError as exc:
            return (), f"the run log was not read: {_problem(exc)}"
        launches: list[Launch] = []
        for _, name in sorted(steps)[-MAX_REVIEW_LAUNCHES:]:
            launches.append(Launch(name, _started_at(run, name)))
    return tuple(launches), ""


def _started_at(run: SafeRoot, step: str) -> str:
    try:
        raw = run.read_bytes(f"{step}/execution.json", limit=MAX_EXECUTION_BYTES)
    except (ReadLimitExceeded, StateError, OSError):
        return ""
    if raw is None:
        return ""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return ""
    started = data.get("started_at") if isinstance(data, dict) else None
    return one_line(started, 64) if isinstance(started, str) else ""


def _marked(comments: Sequence[CommentInfo]) -> list[MarkedComment]:
    found: list[MarkedComment] = []
    for comment in comments:
        result = scan(REVIEW, comment.body)
        if not result.claims and not result.defects:
            continue
        claim = result.claims[0] if len(result.claims) == 1 else None
        found.append(MarkedComment(comment, claim, result.defects))
    return found


def unjournaled_review_report(
    state: AutoForgeState,
    *,
    github: ProvenanceReads,
    state_dir: Path,
    logs_dir: Path,
    standalone_roots: Sequence[Path],
    coordination_root: Path | None,
    coordination_problem: str = "",
) -> ProvenanceReport | None:
    """The report for ``state``, ``None`` when there is nothing to report.

    Nothing to report: the run is not :func:`applies`, or no comment of the
    PR carries this round's marker at the bound HEAD, base and merge base,
    or an unreadable ``ai-review-result`` marker, that ``state`` itself does
    not account for. Those are the comments the REVIEW entry and its
    post-review read block on (ADR 0004 D9.6). A failed comment read is a
    report that says so.
    """
    if not applies(state):
        return None
    pr = parse_pr_url(state.current_pr_url)
    round_ = state.review_round + 1
    head = state.current_head_sha.lower()
    key = (round_, head, state.current_base_ref, state.current_merge_base_sha.lower())
    try:
        comments = github.get_pr_comments(pr.canonical)
    except AutoForgeError as exc:
        return ProvenanceReport(checked=False, problem=_problem(exc))
    marked = _marked(comments)
    candidates = [m for m in marked if m.defects or (m.claim is not None and m.claim.key == key)]
    blocking = [
        m
        for m in candidates
        if not any(e.journals for e in journal_evidence(state, pr, m.comment, m.claim))
    ]
    if not blocking:
        return None
    others = [m for m in marked if m not in blocking]
    problems: list[str] = []

    launches, launch_problem = reviewer_launches(logs_dir, state.run_id)
    if launch_problem:
        problems.append(launch_problem)
    scan_ = scan_state_dirs(
        current_dir=state_dir,
        current=state,
        roots=standalone_roots,
        coordination_root=coordination_root,
        coordination_problem=coordination_problem,
    )
    round_comments = []
    for m in blocking:
        matches = []
        for scanned in scan_.loaded:
            assert scanned.state is not None
            evidence = journal_evidence(scanned.state, pr, m.comment, m.claim)
            if evidence:
                matches.append(JournalMatch(scanned, evidence))
        round_comments.append(
            RoundComment(
                m,
                tuple(matches),
                seconds_between(state.created_at, m.comment.created_at),
                tuple(
                    (launch.step, seconds_between(launch.started_at, m.comment.created_at))
                    for launch in launches
                ),
            )
        )
    on_pr = tuple(
        s
        for s in scan_.loaded
        if not s.current and s.state is not None and _same_pr(s.state.current_pr_url, pr)
    )

    pr_head = ""
    issues: tuple[str, ...] = ()
    defects: tuple[str, ...] = ()
    implementation_read = False
    try:
        info = github.get_pr(pr.canonical)
    except AutoForgeError as exc:
        problems.append(f"the PR was not read: {_problem(exc)}")
    else:
        pr_head = one_line(info.head_sha, 64)
        found = scan(IMPLEMENTATION, info.body)
        issues = tuple(claim.issue.canonical for claim in found.claims)
        defects = found.defects
        implementation_read = True

    listed: int | None = None
    after: list[tuple[PRCommitInfo, int | None]] = []
    earliest = _earliest(m.comment.created_at for m in blocking)
    try:
        commits = github.get_pr_commits(pr.canonical)
    except AutoForgeError as exc:
        problems.append(f"the PR's commits were not read: {_problem(exc)}")
    else:
        listed = len(commits)
        if not earliest:
            problems.append(
                "the comment's creation time could not be read, so no commit is placed after it"
            )
        for commit in commits:
            seconds = seconds_between(earliest, commit.committed_at) if earliest else None
            if seconds is not None and seconds >= 0:
                after.append((commit, seconds))
    return ProvenanceReport(
        checked=True,
        pr_url=pr.canonical,
        round=round_,
        head=head,
        base=state.current_base_ref,
        merge_base=state.current_merge_base_sha.lower(),
        run_created_at=state.created_at,
        launches=launches,
        comments=tuple(round_comments),
        markers=tuple(others),
        scan=scan_,
        states_on_pr=on_pr,
        commits_listed=listed,
        commits_after=tuple(after[:MAX_LISTED_COMMITS]),
        commits_after_total=len(after),
        pr_head_sha=pr_head,
        implementation_issues=issues,
        implementation_defects=defects,
        implementation_read=implementation_read,
        problems=tuple(problems),
    )


def _earliest(times: Iterable[str]) -> str:
    """The earliest of ISO 8601 times with a zone; "" when none can be read."""
    best = ""
    for at in times:
        if seconds_between(at, at) is None:
            continue
        if not best or (seconds_between(best, at) or 0) < 0:
            best = at
    return best


__all__ = [
    "MAX_DIR_ENTRIES",
    "MAX_EXCERPT_CHARS",
    "MAX_HEADLINE_CHARS",
    "MAX_IDENTITY_CHARS",
    "MAX_LISTED_COMMITS",
    "MAX_LISTED_MARKERS",
    "MAX_REVIEW_LAUNCHES",
    "NOTE",
    "Evidence",
    "JournalMatch",
    "Launch",
    "MarkedComment",
    "ProvenanceReads",
    "ProvenanceReport",
    "RoundComment",
    "ScannedState",
    "StateScan",
    "applies",
    "journal_evidence",
    "offset_text",
    "one_line",
    "reviewer_launches",
    "scan_state_dirs",
    "seconds_between",
    "unjournaled_review_report",
]
