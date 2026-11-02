"""Rebinding an existing standalone run to the issue's native parent: ``rebind-epic`` (#435).

A run records its EPIC once (``epic_url``), when ``run`` creates it (#293),
and every later phase acts on that record: ``UPDATE_EPIC`` posts its
progress comment and writes the roadmap section on it, and takes the next
issue only among its native sub-issues. A run created before #293, or whose
issue was moved to another parent after it started, carries an EPIC GitHub
no longer names. ``rebind-epic`` replaces that one field of such a run with
the issue's current native parent, and nothing else.

This module is the operation's decision, apart from its I/O: the request
and its local checks (:func:`parse_rebind_request`), what the run's state
allows (:func:`state_problems`), what GitHub says (:func:`observe_parent`
and :func:`parent_problems`), the report a dry run and an applied rebind
print (:class:`RebindReport`) and the run-log record of the decision
(:meth:`RebindReport.record`). The engine holds the lock, loads and saves;
the CLI guards scheduler ownership. Nothing here writes, locks or reads a
file.

**What may be rebound.** A standalone run (never a scheduler namespace's,
whose EPIC is the one whose graph admitted the issue, ADR 0008 D3.1) in
one of :data:`REBIND_PHASES`:

* ``READY_FOR_MERGE``: the review is done, nothing of the EPIC has been
  read or written for this issue, and with the merge gate closed the run
  holds there;
* ``BLOCKED``: the run waits for the operator.

and only when nothing persisted is tied to the EPIC it records: no effect
record, entry observation or completion context of any phase entry (they
are kept while a run is ``BLOCKED`` for ``unblock`` to re-enter, and
``UPDATE_EPIC``'s name the EPIC and its body; a rebind never migrates or
drops one), no ``REPLAN_REEXECUTE`` journal, no rejected ``UPDATE_EPIC``
selection (``next_issue_rejections``: choices among the recorded EPIC's
sub-issues, rendered into the next ``UPDATE_EPIC`` prompt), and no merge
counted for a roadmap update other than this issue's own PR (a batch of
earlier issues' merges belongs to the EPIC they were made under). Every
other phase, and every such record, refuses; there is no migration rule.

**What GitHub must say.** ``--to`` is the issue's direct native parent now,
in the run's repository, and open. The reads are
:meth:`~autoforge.github.GitHubClient.get_issue_node` of the issue, of
``--to`` and of the issue again (:data:`REBIND_READS`); an issue whose id,
state or parent changed between its two reads refuses, and a read that
fails is the client's :class:`~autoforge.errors.GitHubError`, never a
fallback. A parent named in an issue's text is never read.

**What changes.** ``epic_url`` alone (and the save's ``updated_at``). The
run id, issue, PR, branch, reviewed and current revisions, findings,
rounds, counters, budgets, evidence, configuration and sandbox pin,
operator history and the worktree are untouched: the prior state is the
new one with the recorded EPIC and ``updated_at`` of the run-log record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .errors import (
    ConfigurationError,
    EpicRebindError,
    ExecutionError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
)
from .github import IssueNode
from .runlog import RUN_ID_RE
from .transitions import Phase
from .validation import GitHubIssueRef, parse_issue_url

if TYPE_CHECKING:
    from .state import AutoForgeState


class ParentReader(Protocol):
    """The one kind of GitHub read a rebind makes."""

    def get_issue_node(self, url: str) -> IssueNode: ...


# Every client method this module may call (a structural test holds it to this).
REBIND_READS = frozenset({"get_issue_node"})

# The phases a run may be rebound in (see the module docstring).
REBIND_PHASES = frozenset({Phase.READY_FOR_MERGE, Phase.BLOCKED})

# The operator's --reason, as unblock bounds its own.
MAX_REBIND_REASON_CHARS = 1000

# What a refusal, a report or a status line quotes of a GitHub error.
_MAX_SHOWN_ERROR_CHARS = 300

COMMAND = "rebind-epic"


# -- the request ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RebindRequest:
    """What the operator asked for: the run's issue, the EPIC it records, the one it should.

    ``old`` is ``--from`` (the EPIC the operator expects the run to record),
    ``new`` is ``--to``, ``reason`` the operator's text (stripped, never
    empty) and ``run_id`` ``--run-id`` (``""`` when not given).
    """

    issue: GitHubIssueRef
    old: GitHubIssueRef
    new: GitHubIssueRef
    reason: str
    run_id: str = ""


def parse_rebind_request(
    *, issue_url: str, from_url: str, to_url: str, reason: str, run_id: str = ""
) -> RebindRequest:
    """The request, checked locally; nothing is read (ConfigurationError, exit 2).

    Three issue URLs of one repository; ``--from`` and ``--to`` differ and
    neither is the issue; a non-empty ``--reason`` of at most
    :data:`MAX_REBIND_REASON_CHARS`; a ``--run-id`` in a run id's shape.
    """
    issue = parse_issue_url(issue_url)
    old = parse_issue_url(from_url)
    new = parse_issue_url(to_url)
    for flag, ref in (("--from", old), ("--to", new)):
        if not ref.same_repository(issue):
            raise ConfigurationError(
                f"{COMMAND} {flag} {ref.canonical} is in {ref.repository!r}, not the issue's "
                f"repository {issue.repository!r}; a run's EPIC is in its issue's repository"
            )
        if ref.same_target(issue):
            raise ConfigurationError(
                f"{COMMAND} {flag} {ref.canonical} is the issue itself; a run's EPIC is "
                "another issue"
            )
    if old.same_target(new):
        raise ConfigurationError(
            f"{COMMAND} --from and --to both name {new.canonical}; a rebind moves the run to "
            "another EPIC"
        )
    reason = reason.strip()
    if not reason:
        raise ConfigurationError(f"{COMMAND} requires a non-empty --reason")
    if len(reason) > MAX_REBIND_REASON_CHARS:
        raise ConfigurationError(
            f"{COMMAND} --reason must be at most {MAX_REBIND_REASON_CHARS} characters "
            f"({len(reason)} given)"
        )
    run_id = run_id.strip()
    if run_id and not RUN_ID_RE.fullmatch(run_id):
        raise ConfigurationError(
            f"{COMMAND} --run-id {run_id[:80]!r} is not a run id ('autoforge status' prints it)"
        )
    return RebindRequest(issue=issue, old=old, new=new, reason=reason, run_id=run_id)


# -- what the run's state allows -----------------------------------------------------------


def _ref(url: str) -> GitHubIssueRef | None:
    try:
        return parse_issue_url(url)
    except ConfigurationError:
        return None


def is_already_bound(state: AutoForgeState, request: RebindRequest) -> bool:
    """The run already records ``--to``: a repeat of an applied rebind, nothing to do."""
    recorded = _ref(state.epic_url)
    return recorded is not None and recorded.same_target(request.new)


def identity_problems(state: AutoForgeState, request: RebindRequest) -> list[str]:
    """Where the request names another run, issue or repository than the loaded one."""
    problems: list[str] = []
    if not request.issue.same_repository(state.repository):
        problems.append(
            f"the run is of repository {state.repository!r}, and --issue "
            f"{request.issue.canonical} is of {request.issue.repository!r}"
        )
    current = _ref(state.current_issue_url)
    if current is None:
        problems.append(
            "the run names no current issue (current_issue_url is empty), so there is no "
            "issue whose native parent it could be bound to"
        )
    elif not current.same_target(request.issue):
        problems.append(
            f"the run's current issue is {current.canonical}, not --issue "
            f"{request.issue.canonical}: it moved on, or this is another run; read "
            "'autoforge status' and name the issue it shows"
        )
    if request.run_id and request.run_id != state.run_id:
        problems.append(
            f"the run in this state directory is {state.run_id}, not --run-id {request.run_id}"
        )
    return problems


def stale_problem(state: AutoForgeState, request: RebindRequest) -> str:
    """Why ``--from`` is not the EPIC the run records ("" when it is)."""
    recorded = _ref(state.epic_url)
    if recorded is not None and recorded.same_target(request.old):
        return ""
    return (
        f"the run records EPIC {state.epic_url or '(none)'}, not --from "
        f"{request.old.canonical}: the expected EPIC is stale, so nothing is changed; read "
        "'autoforge status' and repeat with the EPIC it shows"
    )


_PHASE_ADVICE: Mapping[Phase, str] = {
    Phase.INITIALIZING: (
        "it has not started: 'resume' binds nothing again, so continue it until it stops at "
        "READY_FOR_MERGE (the merge gate closed) or BLOCKED, then rebind"
    ),
    Phase.ANALYZE_EXECUTE: (
        "a working phase whose entry may hold a journal or a launch: continue it with "
        "'resume' until it stops at READY_FOR_MERGE (the merge gate closed) or BLOCKED, "
        "then rebind"
    ),
    Phase.REVIEW: (
        "a working phase whose entry may hold a journal or a launch: continue it with "
        "'resume' until it stops at READY_FOR_MERGE (the merge gate closed) or BLOCKED, "
        "then rebind"
    ),
    Phase.FIX: (
        "a working phase whose entry may hold a journal (a push, follow-up issues, actions on "
        "the EPIC) or an unpushed candidate: continue it with 'resume' until it stops at "
        "READY_FOR_MERGE (the merge gate closed) or BLOCKED, then rebind"
    ),
    Phase.REPLAN_REEXECUTE: (
        "a replan transaction is in flight and reads the EPIC: continue it with 'resume' "
        "until it stops at READY_FOR_MERGE (the merge gate closed) or BLOCKED, then rebind"
    ),
    Phase.MERGE: (
        "the merge may already have been sent, and UPDATE_EPIC follows it: continue with "
        "'resume'; if it reaches UPDATE_EPIC, see the manual fallback in docs/usage.md"
    ),
    Phase.UPDATE_EPIC: (
        "UPDATE_EPIC writes the recorded EPIC's progress comment and roadmap section, and a "
        "rebind here could split them between two EPICs; there is no safe rebind of this "
        "entry, see the manual fallback in docs/usage.md"
    ),
    Phase.DONE: (
        "the run is finished and nothing of it uses its EPIC again; a new 'autoforge run' "
        "binds an issue to its native parent (#293)"
    ),
    Phase.FAILED: (
        "the run is finished and nothing of it uses its EPIC again; a new 'autoforge run' "
        "binds an issue to its native parent (#293)"
    ),
}


def _owner_phase(piece: object) -> str:
    if isinstance(piece, Mapping):
        phase = piece.get("phase")
        owner = piece.get("owner")
        if isinstance(owner, Mapping) and isinstance(owner.get("phase"), str):
            phase = owner["phase"]
        if isinstance(phase, str) and phase:
            return phase
    return "an unknown phase"


def _record_target(record: Mapping[str, object]) -> str:
    target = record.get("target")
    if isinstance(target, Mapping):
        for key in ("epic_url", "issue_url", "pr_url", "repository"):
            value = target.get(key)
            if isinstance(value, str) and value:
                return value
    return "(no target)"


def describe_effect_record(record: object, epic: GitHubIssueRef | None) -> str:
    """One persisted effect record as a refusal names it: kind, stage, target."""
    if not isinstance(record, Mapping):
        return "an effect record that is not an object"
    kind = record.get("kind", "?")
    stage = record.get("stage", "?")
    target = _record_target(record)
    ref = _ref(target)
    on_epic = epic is not None and ref is not None and ref.same_target(epic)
    return (
        f"{kind} ({stage}) on {target}"
        + (" (the recorded EPIC)" if on_epic else "")
        + f", of {_owner_phase(record)}"
    )


def pending_batch(state: AutoForgeState) -> list[str]:
    """The merged PRs counted since the last roadmap write: the next update's batch."""
    n = state.merged_since_epic_update
    return list(state.counted_merged_prs[-n:]) if n > 0 else []


def state_problems(state: AutoForgeState, request: RebindRequest) -> list[str]:
    """Every reason the loaded run may not be rebound by ``request``, each with what to do.

    Identity first (another run, issue or repository), then a stale
    ``--from``, then what the run holds (see the module docstring). Empty
    when the state allows the rebind; GitHub's answer is
    :func:`parent_problems`'.
    """
    problems = identity_problems(state, request)
    stale = stale_problem(state, request)
    if stale:
        problems.append(stale)
    recorded = _ref(state.epic_url)
    if state.phase not in REBIND_PHASES:
        advice = _PHASE_ADVICE.get(state.phase, "")
        problems.append(
            f"the run is in phase {state.phase.value}; a rebind is made only in "
            f"{' or '.join(sorted(p.value for p in REBIND_PHASES))}"
            + (f": {advice}" if advice else "")
        )
    records = state.effect_records if isinstance(state.effect_records, list) else []
    held: list[str] = []
    if records:
        held.append(
            f"{len(records)} effect record(s): "
            + "; ".join(describe_effect_record(r, recorded) for r in records)
        )
    if state.entry_observation:
        held.append(f"the entry observation of {_owner_phase(state.entry_observation)}")
    if state.completion_context:
        held.append(f"the completion context of {_owner_phase(state.completion_context)}")
    if held:
        problems.append(
            "the run keeps the persisted effect state of a phase entry ("
            + "; ".join(held)
            + "): a rebind never migrates, drops or relabels a journaled write or what an "
            "entry observed, and 'unblock' re-enters that entry with it; finish the entry "
            "first ('unblock', then 'resume' until READY_FOR_MERGE or a BLOCKED run that "
            "keeps none), or use the manual fallback in docs/usage.md"
        )
    if state.replan_transaction:
        stage = state.replan_transaction.get("stage", "(unknown)")
        problems.append(
            f"a REPLAN_REEXECUTE journal is recorded (stage {stage!r}); it reads the EPIC, "
            "and a rebind never rewrites a journal: settle the replan first"
        )
    if state.next_issue_rejections:
        problems.append(
            f"{len(state.next_issue_rejections)} rejected UPDATE_EPIC selection(s) are "
            "persisted (next_issue_rejections): they are choices among the recorded EPIC's "
            "sub-issues, rendered into the next UPDATE_EPIC prompt, and a rebind would hand "
            "them to another EPIC's selection; see the manual fallback in docs/usage.md"
        )
    batch = pending_batch(state)
    own = state.current_pr_url
    if batch and batch != [own]:
        others = [url for url in batch if url != own]
        problems.append(
            f"{state.merged_since_epic_update} merge(s) are counted for the next roadmap "
            f"update of the recorded EPIC, {len(others)} of them another issue's PR ("
            + ", ".join(others)
            + "): they were made under that EPIC, and a rebind would report them on "
            "another; update the recorded EPIC's roadmap first, or use the manual fallback "
            "in docs/usage.md"
        )
    return problems


# -- what GitHub says ------------------------------------------------------------------------


@dataclass(frozen=True)
class ParentObservation:
    """The issue's native parent and ``--to``'s state, as GitHub answered all three reads.

    ``issue_state`` and ``new_epic_state`` are GitHub's words (``open`` or
    ``closed``); ``parent`` is the issue's direct native parent (None when
    GitHub names none).
    """

    issue_state: str
    parent: GitHubIssueRef | None
    new_epic_state: str


def _shown_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    if len(text) > _MAX_SHOWN_ERROR_CHARS:
        text = text[: _MAX_SHOWN_ERROR_CHARS - 3] + "..."
    return text


def _read(reader: ParentReader, ref: GitHubIssueRef, role: str) -> IssueNode:
    """``ref``'s node, or the read's failure (its class kept) with the rebind's context."""
    try:
        return reader.get_issue_node(ref.canonical)
    except (GitHubError, ExecutionError) as exc:
        kind: type[GitHubError]
        if isinstance(exc, GitHubUnavailableError):
            kind = GitHubUnavailableError
        elif isinstance(exc, GitHubNotFoundError):
            kind = GitHubNotFoundError
        else:
            kind = GitHubError
        raise kind(
            f"{COMMAND}: reading {role} {ref.canonical} failed ({exc}). Nothing was changed; "
            "a rebind is decided only on GitHub's complete answer"
        ) from exc


def _parent_shown(parent: GitHubIssueRef | None) -> str:
    return "none" if parent is None else parent.canonical


def _same_parent(first: GitHubIssueRef | None, again: GitHubIssueRef | None) -> bool:
    if first is None or again is None:
        return first is None and again is None
    return first.same_target(again)


def observe_parent(reader: ParentReader, request: RebindRequest) -> ParentObservation:
    """Read the issue, ``--to`` and the issue again; refuse an issue that changed between.

    A failed read raises the client's error (:class:`GitHubError`, transient
    ones :class:`GitHubUnavailableError`). A difference in the issue's id,
    state or native parent between its two reads raises
    :class:`EpicRebindError`: neither answer is acted on, and the operator
    runs the command again.
    """
    first = _read(reader, request.issue, "the issue")
    epic = _read(reader, request.new, "the EPIC --to names")
    again = _read(reader, request.issue, "the issue again")
    changed: list[str] = []
    if again.id != first.id:
        changed.append(f"GitHub's id for it was {first.id} and is now {again.id}")
    if again.state != first.state:
        changed.append(f"it was {first.state} and is now {again.state}")
    if not _same_parent(first.parent, again.parent):
        changed.append(
            f"its native parent was {_parent_shown(first.parent)} and is now "
            f"{_parent_shown(again.parent)}"
        )
    if changed:
        raise EpicRebindError(
            f"{COMMAND} refused: issue {request.issue.canonical} changed on GitHub while it "
            "was being read: " + "; ".join(changed) + ". Nothing was changed; run the command "
            "again to decide on what GitHub says then"
        )
    return ParentObservation(
        issue_state=again.state, parent=again.parent, new_epic_state=epic.state
    )


def parent_problems(request: RebindRequest, observation: ParentObservation) -> list[str]:
    """Why GitHub's answer refuses ``--to``: it must be the issue's open native parent."""
    parent = observation.parent
    problems: list[str] = []
    if parent is None:
        problems.append(
            f"GitHub names no native parent for issue #{request.issue.number}; a run is "
            f"rebound only to its issue's native parent: attach the issue to "
            f"{request.new.canonical} as a sub-issue on GitHub first (a parent named in the "
            "issue's text is never read)"
        )
    elif not parent.same_repository(request.issue):
        problems.append(
            f"the issue's native parent {parent.canonical} is in another repository; "
            "AutoForge runs within one repository"
        )
    elif not parent.same_target(request.new):
        recorded = " (the EPIC --from names)" if parent.same_target(request.old) else ""
        problems.append(
            f"GitHub names #{parent.number} {parent.canonical}{recorded} as the issue's "
            f"native parent, not --to #{request.new.number}: a run is rebound only to its "
            "issue's native parent; re-parent the issue on GitHub, or name its parent with "
            "--to"
        )
    if observation.new_epic_state != "open":
        problems.append(
            f"--to {request.new.canonical} is {observation.new_epic_state} on GitHub; a run "
            "records only an open EPIC: reopen it first"
        )
    return problems


# -- the report -------------------------------------------------------------------------


@dataclass
class RebindReport:
    """What ``rebind-epic`` decided, for its output and its run-log record.

    ``decision`` is ``would-rebind`` / ``rebound`` / ``already-bound`` /
    ``refused`` (``would-refuse`` for a dry run). ``observation`` is GitHub's
    answer when it was read, ``read_error`` the failed read otherwise
    (``""`` when no read was made).
    """

    request: RebindRequest
    run_id: str
    phase: str
    recorded_epic_url: str
    issue_url: str
    pr_url: str
    branch: str
    worktree: str
    state_file: str
    prior_updated_at: str
    dry_run: bool
    decision: str = ""
    problems: list[str] = field(default_factory=list)
    observation: ParentObservation | None = None
    read_error: str = ""
    notes: list[str] = field(default_factory=list)
    apply_command: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.problems)

    @property
    def exit_code(self) -> int:
        """0 applied, already bound or would apply; 2 refused; 1 a read failed (nothing else)."""
        if self.problems:
            return 2
        if self.read_error:
            return 1
        return 0

    def lines(self) -> list[str]:
        """What the command prints: the facts, then the decision and every refusal."""
        req = self.request
        observed = self.observation
        if observed is not None:
            parent = (
                f"#{observed.parent.number} {observed.parent.canonical}"
                if observed.parent is not None
                else f"none (GitHub names no parent for #{req.issue.number})"
            )
            issue_state = f" ({observed.issue_state} on GitHub)"
            to_state = f" ({observed.new_epic_state} on GitHub)"
        elif self.read_error:
            parent = f"not read: {self.read_error}"
            issue_state = to_state = ""
        elif self.decision == "already-bound":
            parent = "not read (the run already records --to: there is nothing to decide)"
            issue_state = to_state = ""
        else:
            parent = "not read (the run's state refuses the rebind first)"
            issue_state = to_state = ""
        recorded = _ref(self.recorded_epic_url)
        expected = (
            "matches --from"
            if recorded and recorded.same_target(req.old)
            else "differs from --from"
        )
        if recorded and recorded.same_target(req.new):
            expected = "= --to"
        out = [
            f"EPIC rebind of run {self.run_id}"
            + (" (dry run: nothing written)" if self.dry_run else ""),
            f"  state file:    {self.state_file}",
            f"  phase:         {self.phase}",
            f"  issue:         #{req.issue.number} {self.issue_url or req.issue.canonical}"
            + issue_state,
            f"  PR:            {self.pr_url or '(none)'}"
            + (f" (branch {self.branch})" if self.branch else ""),
            f"  worktree:      {self.worktree}",
            f"  recorded EPIC: {self.recorded_epic_url or '(none)'} ({expected})",
            f"  --to:          #{req.new.number} {req.new.canonical}" + to_state,
            f"  native parent: {parent}",
            f"  reason:        {req.reason}",
        ]
        for note in self.notes:
            out.append(f"  note:          {note}")
        out.append(f"  decision:      {self._decision_text()}")
        for problem in self.problems:
            out.append(f"  - refused: {problem}")
        if self.apply_command:
            out.append(f"  apply with:    {self.apply_command}")
        return out

    def _decision_text(self) -> str:
        old, new = self.recorded_epic_url, self.request.new.canonical
        if self.decision == "already-bound":
            return f"already bound to {new}: nothing to do, nothing written"
        if self.decision == "rebound":
            return (
                f"rebound from {old} to {new}; recorded in the run log, then saved. "
                "Nothing else of the run changed"
            )
        if self.decision == "would-rebind":
            return f"would rebind from {old} to {new}"
        if self.read_error and not self.problems:
            return "undecided: a GitHub read failed; nothing was changed"
        verb = "would refuse" if self.dry_run else "refused"
        return f"{verb} ({len(self.problems)} reason(s)); nothing was changed"

    def record(self) -> dict[str, object]:
        """The run log's record of the decision: identities, URLs, reason, outcome."""
        observed = self.observation
        return {
            "decision": self.decision,
            "operator_reason": self.request.reason,
            "run_id": self.run_id,
            "phase": self.phase,
            "issue_url": self.issue_url,
            "pr_url": self.pr_url,
            "branch": self.branch,
            "recorded_epic_url": self.recorded_epic_url,
            "from_epic_url": self.request.old.canonical,
            "to_epic_url": self.request.new.canonical,
            "native_parent_url": (
                observed.parent.canonical if observed and observed.parent is not None else None
            ),
            "issue_state": observed.issue_state if observed else None,
            "to_epic_state": observed.new_epic_state if observed else None,
            "read_error": self.read_error,
            "problems": list(self.problems),
            "prior_updated_at": self.prior_updated_at,
        }


def refusal_message(report: RebindReport) -> str:
    """The error ``rebind-epic`` exits 2 with: every reason, then that nothing changed."""
    reasons = "\n".join(f"  - {p}" for p in report.problems)
    return (
        f"{COMMAND} refused for run {report.run_id} ({len(report.problems)} reason(s)):\n"
        f"{reasons}\nNothing was changed: no state write, and nothing written to GitHub."
    )


def apply_words(request: RebindRequest, run_id: str) -> list[str]:
    """The words after ``autoforge`` and its global flags that apply what a dry run previewed."""
    return [
        COMMAND,
        "--issue",
        request.issue.canonical,
        "--from",
        request.old.canonical,
        "--to",
        request.new.canonical,
        "--run-id",
        run_id,
        "--reason",
        request.reason,
    ]


def preview_words(issue_url: str, recorded_epic_url: str, parent_url: str) -> list[str]:
    """The words of the dry run ``status`` points to when the parent differs."""
    return [
        COMMAND,
        "--issue",
        issue_url,
        "--from",
        recorded_epic_url,
        "--to",
        parent_url,
        "--reason",
        "<why>",
        "--dry-run",
    ]


def informational_notes(state: AutoForgeState, request: RebindRequest) -> list[str]:
    """What the rebind leaves as it is and the operator may want to know; never a refusal."""
    notes: list[str] = []
    recorded = _ref(state.epic_url)
    on_epic = [
        o
        for o in state.operator_comment_outcomes
        if isinstance(o, Mapping)
        and recorded is not None
        and (target := _ref(str(o.get("target", "")))) is not None
        and target.same_target(recorded)
    ]
    if on_epic:
        notes.append(
            f"{len(on_epic)} operator action outcome(s) name comments on the recorded EPIC; "
            "they are evidence of what was verified there and stay as they are"
        )
    batch = pending_batch(state)
    if batch and batch == [state.current_pr_url]:
        notes.append(
            "this issue's merged PR is counted for the next roadmap update; after the rebind "
            "UPDATE_EPIC reports it on --to"
        )
    return notes


def parent_drift(recorded_epic_url: str, parent: GitHubIssueRef | None) -> bool:
    """Whether GitHub's native parent of the run's issue is another issue than its EPIC."""
    recorded = _ref(recorded_epic_url)
    return parent is not None and (recorded is None or not parent.same_target(recorded))


def status_lines(
    *,
    recorded_epic_url: str,
    issue_url: str,
    parent: GitHubIssueRef | None,
    read_error: str,
    rebind_hint: str,
) -> list[str]:
    """``status``'s read-only check of the recorded EPIC against the issue's native parent.

    A warning when GitHub names another parent (with ``rebind_hint``, the
    command that previews a rebind); a note when it names none or the read
    failed; nothing when the two agree. It never changes anything.
    """
    issue = _ref(issue_url)
    number = f"#{issue.number}" if issue is not None else "the issue"
    if read_error:
        return [
            f"Parent:     not read ({read_error}); the recorded EPIC is shown unchecked",
        ]
    if parent is None:
        return [
            f"Parent:     GitHub names no native parent for {number}; the recorded EPIC came "
            "from 'run --epic', or the issue was detached since"
        ]
    if not parent_drift(recorded_epic_url, parent):
        return []
    return [
        f"WARNING:    GitHub names #{parent.number} {parent.canonical} as the native parent of "
        f"{number}, not the recorded EPIC; UPDATE_EPIC would write to the recorded one. "
        "Nothing was changed. " + rebind_hint
    ]
