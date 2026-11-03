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
  ``execution.json``), the login GitHub names, its marker (its finding ids
  and defects bounded and counted), the digest and length of its body and
  a bounded excerpt of `gh`'s print of it. The digest is of GitHub's text,
  read exactly where `gh` may have rewritten it, and says so when that
  text is not known;
- which state directories on this checkout account for it: every state
  directory found in or directly under the directories the caller names
  (``status`` names the state directory it reads, the configured
  ``state_dir`` and, when the two differ, the directory the first lies in),
  and every scheduler namespace under the git common dir; a directory that
  could not be searched is named, and the search is then incomplete. Only
  a record of the comment itself accounts for it: a ``review_comment``
  effect record (K4) observed at it, or ``last_review_comment_url`` naming
  it. A K4 record for the comment's marker that is not observed at it is a
  plan for its round, which names no comment, and its ``review_history``
  recording the round at the comment's HEAD is a round too: both are
  listed as qualified evidence, never as accounting for the comment. The
  states bound to the PR are listed whatever they hold;
- the PR's other ``ai-review-result`` markers, with their rounds and HEADs;
- the commits GitHub lists for the PR committed after the earliest such
  comment, with author and committer identity and the time after it.
  GitHub records when a commit was made, never when it was pushed;
- the PR's ``ai-implementation`` marker (the issues it names and its
  defects bounded and counted), and its HEAD now. No owner of a
  PR is recorded on GitHub yet (#414 proposes one): the states bound to the
  PR are what this checkout knows of it.

It presents evidence and names no author, and nothing it reports is read by
anything that decides: the engine never imports it. It is not a remedy
either: the block's own reason says what to do.

**Read-only.** GitHub is read through the client handed in (the PR's
comments; when a comment is found, the PR and its commits, and the exact
read of a comment `gh` printed with a ``^``), and the local
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
from .github import (
    CommentInfo,
    CommitPerson,
    PRCommitInfo,
    PRInfo,
    gh_may_rewrite,
    gh_prints_as,
)
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
# How many of the PR's other review markers, of the commits after the
# comment, of the finding ids one marker names, of the defects one comment's
# markers have and of the issues the PR's implementation markers name, are
# shown; the report says how many there are.
MAX_LISTED_MARKERS = 40
MAX_LISTED_COMMITS = 40
MAX_LISTED_FINDING_IDS = 20
MAX_LISTED_DEFECTS = 10
MAX_LISTED_ISSUES = 10
# At most this many of the comments the round blocks on are read again
# exactly, when `gh` printed them with a ``^``.
MAX_EXACT_READS = 10
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

    def get_comment_exact(self, comment_url: str) -> CommentInfo: ...


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
    """Every state directory found on this checkout, and what could not be searched.

    ``unsearched`` is each directory that should have been searched and was
    not, with why: a root that could not be opened or listed, or a
    directory listed under one that could not be opened (no permission, or
    no longer a directory). A state directory may be in it, so a search with
    one, an unreadable state or a problem is not :attr:`complete`.
    """

    states: tuple[ScannedState, ...]
    roots: tuple[str, ...]  # the directories searched for standalone state directories
    namespaces_read: bool
    problems: tuple[str, ...]
    unsearched: tuple[tuple[str, str], ...] = ()

    @property
    def loaded(self) -> tuple[ScannedState, ...]:
        return tuple(s for s in self.states if s.state is not None)

    @property
    def unreadable(self) -> tuple[ScannedState, ...]:
        return tuple(s for s in self.states if s.state is None)

    @property
    def complete(self) -> bool:
        """Whether every state directory that could hold a journal was read."""
        return not (self.unreadable or self.unsearched or self.problems)


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
    never followed. A root that cannot be opened or listed, one holding
    more than :data:`MAX_DIR_ENTRIES` entries, and a directory listed under
    one that cannot be opened are reported as not searched, never skipped
    in silence: a state directory may be there. The scheduler
    namespaces are every issue namespace under ``coordination_root``
    (``None`` outside a git repository, ``coordination_problem`` saying
    why). ``current`` is the state already loaded from ``current_dir``: it is
    not read again, and a directory found again under another name is the
    same directory.
    """
    states: list[ScannedState] = []
    problems: list[str] = []
    unsearched: list[tuple[str, str]] = []
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
        except (StateError, OSError) as exc:
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
            unsearched.append((str(path), _problem(exc)))
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
                unsearched.append(
                    (
                        str(path),
                        f"it holds more than {MAX_DIR_ENTRIES} entries, so the directories in "
                        "it were not searched",
                    )
                )
            except (StateError, OSError) as exc:
                unsearched.append((str(path), f"it could not be listed: {_problem(exc)}"))
            for name in sorted(names):
                try:
                    with root.subroot(name) as sub:
                        consider(sub)
                except FileNotFoundError:
                    continue  # removed since it was listed: nothing is there to search
                except (StateError, OSError) as exc:
                    # Listed as a directory and not opened: no permission, or no
                    # longer a directory. A state directory may be there.
                    unsearched.append((str(root.path / name), _problem(exc)))

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
    return StateScan(
        tuple(states), tuple(searched), namespaces_read, tuple(problems), tuple(unsearched)
    )


# -- what one state holds about a comment -------------------------------------------------------


# How an effect record's payload compares with a comment's text (:meth:`BodyRead.compare`).
SAME = "same"
DIFFERS = "differs"
UNKNOWN = "unknown"


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BodyRead:
    """A comment's body: `gh`'s print of it, and GitHub's text when that is known.

    `gh` prints every body through its sanitizer, which rewrites a control
    character, and the literal text of its JSON escape, into caret notation
    and cannot be undone (#327, #328), so a digest or a comparison of the
    print is not one of GitHub's text. ``held`` is GitHub's text byte for
    byte: the print itself when it holds no ``^`` (:func:`gh_may_rewrite`),
    else the comment's exact read (``exact_read``) when the print is that
    text's (:func:`gh_prints_as`). It is ``None`` when the exact read was not
    made, failed, or found another version of the comment (``changed``),
    ``problem`` saying which: the digest shown is then the print's, and no
    text is judged equal to the comment's. Only the print is ever quoted;
    the exact text is digested and compared, never shown.
    """

    printed: str
    held: str | None
    exact_read: bool = False
    changed: bool = False
    problem: str = ""

    @classmethod
    def of_print(cls, printed: str, problem: str = "") -> BodyRead:
        """The body from `gh`'s print alone: GitHub's text when it holds no ``^``."""
        printed = printed or ""
        if not gh_may_rewrite(printed):
            return cls(printed, printed)
        return cls(printed, None, problem=problem or "it was not read exactly")

    @property
    def exact(self) -> bool:
        return self.held is not None

    @property
    def sha256(self) -> str:
        """The digest of GitHub's text, or of the print when that is not known."""
        return _sha256(self.held if self.held is not None else self.printed)

    @property
    def chars(self) -> int:
        return len(self.held if self.held is not None else self.printed)

    def compare(self, payload: str) -> str:
        """``payload`` against GitHub's text: :data:`SAME`, :data:`DIFFERS` or :data:`UNKNOWN`.

        Byte for byte when GitHub's text is known. Otherwise a payload that
        `gh` would not print as it printed the comment differs from it,
        and any other is not known to be the comment's text or not; so is
        every payload when the comment changed between the two reads.
        """
        if self.held is not None:
            return SAME if payload == self.held else DIFFERS
        if self.changed or gh_prints_as(payload, self.printed):
            return UNKNOWN
        return DIFFERS

    def document(self) -> dict:
        return {
            "exact": self.exact,
            "read": "exact_read" if self.exact_read else "listing",
            "sha256": self.sha256,
            "chars": self.chars,
            "problem": self.problem,
        }

    def text(self) -> str:
        """The body line's digest and what it is a digest of."""
        if self.held is None:
            return (
                f"{self.chars} characters, sha256 {self.sha256} of gh's print of it, which may "
                f"not be GitHub's text ({self.problem})"
            )
        how = "read exactly" if self.exact_read else "gh printed it unchanged"
        return f"{self.chars} characters, sha256 {self.sha256} (GitHub's text, {how})"


def read_body(github: ProvenanceReads, comment: CommentInfo, *, may_read: bool) -> BodyRead:
    """``comment``'s body, read again exactly when `gh` printed it with a ``^``.

    ``may_read`` False reads nothing more (the bound of
    :data:`MAX_EXACT_READS` is reached). A failed exact read is inconclusive,
    never raised: the report says so.
    """
    printed = comment.body or ""
    if not gh_may_rewrite(printed):
        return BodyRead(printed, printed)
    if not may_read:
        return BodyRead.of_print(
            printed,
            f"it was not read exactly: at most {MAX_EXACT_READS} comments are",
        )
    try:
        held = github.get_comment_exact(comment.url).body
    except AutoForgeError as exc:
        return BodyRead.of_print(printed, f"its exact read failed: {_problem(exc)}")
    if not gh_prints_as(held, printed):
        return BodyRead(
            printed,
            None,
            changed=True,
            problem="it changed between gh's listing and its exact read",
        )
    return BodyRead(printed, held, exact_read=True)


@dataclass(frozen=True)
class Evidence:
    """One thing a state holds about a comment.

    ``effect_record`` and ``last_review_comment_url`` name the comment
    itself and account for it (:attr:`journals`). ``effect_plan`` (a K4
    record for the comment's marker not observed at it) and
    ``review_history`` are about its round: another comment carrying the
    marker is just as consistent with them. ``payload`` is an effect
    record's payload against the comment's text (:meth:`BodyRead.compare`).
    """

    kind: str  # "effect_record" | "last_review_comment_url" | "effect_plan" | "review_history"
    detail: str
    payload: str = ""

    @property
    def journals(self) -> bool:
        """Whether this accounts for the comment itself, not only for its round."""
        return self.kind in ("effect_record", "last_review_comment_url")

    def document(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "payload": self.payload or None}


_PAYLOAD_TEXT = {
    SAME: "its payload is this comment's text byte for byte",
    DIFFERS: "its payload differs from this comment's text",
    UNKNOWN: "whether its payload is this comment's text is not known, since GitHub's text of "
    "the comment is not (see its body line)",
}


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
    body: BodyRead | None = None,
) -> tuple[Evidence, ...]:
    """What ``state`` holds about ``comment`` (``claim`` its marker, if readable).

    Only a record of the comment itself accounts for it: a K4 record the
    controller observed at it (its read-back found exactly this comment
    carrying the marker, with the planned body), or ``last_review_comment_url``
    naming it. A K4 record for the comment's marker that is not observed at
    it (``intended``, ``attempted``, a ``conflict``, or observed at another
    comment) is a plan for the comment's round and names no comment: a
    foreign comment posted after it was saved carries the same marker, with
    the planned body or another. It is listed as qualified evidence
    (``effect_plan``), as is ``review_history`` recording the round at the
    comment's HEAD, and neither accounts for the comment. A payload is
    compared with GitHub's text when ``body`` knows it (``None``: the
    comment's print alone, read no further).
    """
    body = body if body is not None else BodyRead.of_print(comment.body)
    found: list[Evidence] = []
    for raw in state.effect_records if isinstance(state.effect_records, list) else ():
        if not isinstance(raw, dict) or raw.get("kind") != _K4_KIND:
            continue
        identity = raw.get("identity")
        if not isinstance(identity, dict) or not _same_pr(identity.get("pr_url"), pr):
            continue
        observed = raw.get("observed")
        observed_url = observed.get("url", "") if isinstance(observed, dict) else ""
        if not isinstance(observed_url, str):
            observed_url = ""
        at_comment = bool(observed_url) and _same_comment(observed_url, comment.url)
        marker = identity.get("marker")
        keys = {c.key for c in scan(REVIEW, marker).claims} if isinstance(marker, str) else set()
        same_key = claim is not None and claim.key in keys
        if not (at_comment or same_key):
            continue
        stage = raw.get("stage")
        marker_text = "this comment's marker" if same_key else "another marker"
        if at_comment and stage == "observed":
            kind = "effect_record"
            detail = (
                f"a review_comment effect record (K4) for {marker_text}, observed at this "
                "comment: that run read it back as the comment it posted"
            )
        else:
            kind = "effect_plan"
            round_text = "this comment's round" if same_key else "another round"
            detail = (
                f"a review_comment effect record (K4) for {marker_text}, "
                f"{one_line(str(stage or '?'), 40)}: a plan for {round_text}, which names no "
                "comment"
            )
            if observed_url:
                where = "this comment" if at_comment else one_line(observed_url, MAX_PROBLEM_CHARS)
                detail += f"; observed at {where}"
        payload = raw.get("payload")
        planned = payload.get("body") if isinstance(payload, dict) else None
        verdict = ""
        if isinstance(planned, str):
            verdict = body.compare(planned)
            detail += "; " + _PAYLOAD_TEXT[verdict]
        found.append(Evidence(kind, detail, verdict))
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
            "evidence": [e.document() for e in self.evidence],
        }


# -- the report -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Launch:
    """A reviewer launch of this run, from its step directory in the run log."""

    step: str
    started_at: str  # "" when the step directory holds no readable execution.json


def _claim_document(claim: ReviewClaim | None) -> dict | None:
    """A marker's fields, its finding ids bounded (:data:`MAX_LISTED_FINDING_IDS`) and counted.

    The decoder bounds each id and the base ref, never how many ids a marker
    names: an untrusted comment's whole list is not the report's to repeat.
    """
    if claim is None:
        return None
    ids = claim.finding_ids
    return {
        "round": claim.round,
        "reviewed_head_sha": claim.reviewed_head_sha,
        "reviewed_base_ref": claim.reviewed_base_ref,
        "reviewed_merge_base_sha": claim.reviewed_merge_base_sha,
        "needs_fix_round": claim.needs_fix_round,
        "finding_ids": list(ids[:MAX_LISTED_FINDING_IDS]) if ids is not None else None,
        "finding_ids_total": len(ids) if ids is not None else None,
    }


def _listed_text(items: Sequence[str], limit: int, sep: str, max_chars: int) -> str:
    """At most ``limit`` of ``items``, joined and cut at ``max_chars``, then how many there are.

    The count follows the cut, so a long list never hides its total.
    """
    shown = one_line(sep.join(items[:limit]), max_chars)
    if len(items) > limit:
        shown += f" (the first {limit} of {len(items)})"
    return shown


def _findings_text(ids: tuple[str, ...] | None) -> str:
    if not ids:
        return "none"
    return _listed_text(ids, MAX_LISTED_FINDING_IDS, ", ", MAX_EXCERPT_CHARS)


def _defects_document(defects: Sequence[str]) -> list[str]:
    """At most :data:`MAX_LISTED_DEFECTS` of an object's marker defects, each one line.

    A body may carry any number of broken markers, and each is a defect: the
    report repeats a bounded few of them and gives their total beside them.
    """
    return [one_line(d, MAX_PROBLEM_CHARS) for d in defects[:MAX_LISTED_DEFECTS]]


def _defects_text(defects: Sequence[str]) -> str:
    """:func:`_defects_document` on one line, with their total when some are not shown."""
    shown = "; ".join(_defects_document(defects))
    if len(defects) > MAX_LISTED_DEFECTS:
        shown += f" (the first {MAX_LISTED_DEFECTS} of {len(defects)} defects)"
    return shown


def _claim_text(claim: ReviewClaim | None) -> str:
    if claim is None:
        return "no readable marker"
    base = repr(claim.reviewed_base_ref) if claim.reviewed_base_ref is not None else "(none)"
    merge_base = (claim.reviewed_merge_base_sha or "")[:12] or "(none)"
    findings = _findings_text(claim.finding_ids)
    return (
        f"round {claim.round} at HEAD {claim.reviewed_head_sha[:12]} on base {base} from merge "
        f"base {merge_base}, needs_fix_round {str(claim.needs_fix_round).lower()}, findings "
        f"{findings}"
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
            "defects": _defects_document(self.defects),
            "defects_total": len(self.defects),
        }


@dataclass(frozen=True)
class RoundComment:
    """One comment the block is about, and what was found about it."""

    marked: MarkedComment
    journals: tuple[JournalMatch, ...]
    seconds_after_run_created: int | None
    seconds_after_launches: tuple[tuple[str, int | None], ...]
    body: BodyRead

    def document(self) -> dict:
        return {
            **self.marked.document(),
            "body": self.body.document(),
            "excerpt": one_line(self.body.printed, MAX_EXCERPT_CHARS),
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
                "complete": scan_.complete if scan_ else False,
                "unreadable": [
                    {"state_dir": s.state_dir, "problem": s.problem}
                    for s in (scan_.unreadable if scan_ else ())
                ],
                "unsearched": [
                    {"path": where, "problem": why}
                    for where, why in (scan_.unsearched if scan_ else ())
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
                    "issues": list(self.implementation_issues[:MAX_LISTED_ISSUES]),
                    "issues_total": len(self.implementation_issues),
                    "defects": _defects_document(self.implementation_defects),
                    "defects_total": len(self.implementation_defects),
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
        defects = item.marked.defects
        for defect in _defects_document(defects):
            lines.append(f"    defect:    {defect}")
        if len(defects) > MAX_LISTED_DEFECTS:
            lines.append(f"    defects:   the first {MAX_LISTED_DEFECTS} of {len(defects)} shown")
        lines.append(
            f"    body:      {item.body.text()}; begins (as gh printed it): "
            f"{one_line(item.body.printed, MAX_EXCERPT_CHARS)!r}"
        )
        journaled = [m for m in item.journals if m.journaled]
        if not journaled:
            read = len(self.scan.loaded) if self.scan else 0
            text = f"    journal:   none of the {read} state directories read accounts for it"
            if self.scan is not None and not self.scan.complete:
                text += (
                    "; the search was incomplete (see 'unreadable' and 'not searched' below), so "
                    "a state directory that does may not have been read"
                )
            lines.append(text)
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
        for where, why in scan_.unsearched:
            lines.append(f"    not searched: {where}: {why}")
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
                text += "; " + _defects_text(marked.defects)
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
            issues = self.implementation_issues
            if issues:
                shown = ", ".join(issues[:MAX_LISTED_ISSUES])
                if len(issues) > MAX_LISTED_ISSUES:
                    shown += f" (the first {MAX_LISTED_ISSUES} of {len(issues)})"
                text = "names " + shown
            else:
                text = "none"
            if self.implementation_defects:
                text += "; " + _defects_text(self.implementation_defects)
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
    not account for (:func:`journal_evidence`). Those are the comments the
    REVIEW entry and its post-review read block on (ADR 0004 D9.6), and one
    the run's own saved plan for the round reads back as a conflict: a plan
    names no comment, so it accounts for none, and the report lists it as a
    plan. A body `gh` printed with a ``^`` is read again exactly (at most
    :data:`MAX_EXACT_READS` of them). A failed comment read is a report that
    says so.
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
    exact_reads = 0
    for m in blocking:
        may_read = exact_reads < MAX_EXACT_READS
        if may_read and gh_may_rewrite(m.comment.body or ""):
            exact_reads += 1  # read_body makes it, whether it succeeds or not
        body = read_body(github, m.comment, may_read=may_read)
        matches = []
        for scanned in scan_.loaded:
            assert scanned.state is not None
            evidence = journal_evidence(scanned.state, pr, m.comment, m.claim, body)
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
                body,
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
    "MAX_EXACT_READS",
    "MAX_LISTED_COMMITS",
    "MAX_LISTED_DEFECTS",
    "MAX_LISTED_FINDING_IDS",
    "MAX_LISTED_ISSUES",
    "MAX_LISTED_MARKERS",
    "MAX_REVIEW_LAUNCHES",
    "NOTE",
    "BodyRead",
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
    "read_body",
    "reviewer_launches",
    "scan_state_dirs",
    "seconds_between",
    "unjournaled_review_report",
]
