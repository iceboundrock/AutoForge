"""Error taxonomy for the AutoForge controller.

Callers must be able to distinguish transient execution failures from
malformed LLM output, controller invariant violations, and real GitHub
state conflicts — so every failure raises one of these, never a bare
``Exception("something failed")``.
"""


class AutoForgeError(Exception):
    """Base class for all controller errors."""


class ConfigurationError(AutoForgeError):
    """Invalid CLI args, config file, profile, or URL."""


class ConfigurationDriftError(ConfigurationError):
    """A REMOTE run's configuration differs from the one it was recorded under (#219).

    Raised by ``resume``, ``step`` and ``unblock`` before any phase executes,
    with nothing written; the message names each changed key, the command
    that restores the recorded configuration, and the opt-in that accepts
    the change.
    """


class SandboxCapabilityError(ConfigurationError):
    """This host cannot enforce the outer sandbox, and the mode refuses (ADR 0005 D12.2).

    Raised by the engine's pre-launch gate under ``sandbox.mode: required``,
    and under ``preferred`` in a REMOTE run, before anything is spawned,
    before the attempt counter is saved and before a LOCAL launch is
    charged: state is unchanged and nothing needs recovery. A configuration
    or capability error, never a phase failure. ``capability`` names what
    the host lacks (``autoforge.sandbox_bwrap.Capability``'s value).
    """

    def __init__(self, message: str, capability: str = "") -> None:
        super().__init__(message)
        self.capability = capability


class StateError(AutoForgeError):
    """State file missing, unreadable, corrupted, or schema-invalid.

    Raised on read of a corrupted state; the controller must never silently
    re-initialize a fresh state over existing data.
    """


class NamespaceError(StateError):
    """A per-issue scheduler namespace cannot be used as asked (ADR 0008 D5, #178).

    Raised by :mod:`autoforge.run_namespace` for a ``binding.json`` that is
    not a valid ``autoforge.run-binding/1`` document (an unknown field or
    schema version, a missing field, an out-of-bound string, an id that is
    not a positive integer), a binding written a second time, a namespace of
    another repository and an ``issues/`` directory beyond its listing
    bound. Nothing is created, moved or rewritten when it is raised.
    """


class NamespaceMismatch(NamespaceError):
    """A namespace's binding names another run than the caller expects (ADR 0008 D5.1, D5.2).

    ``field`` says which identity differs: ``repository_id`` (the scheduler's
    global uncertainty, D14.3), ``issue_number``, ``issue_id`` or ``epic``
    (the namespace's ``uncertain``, D14.2). The caller decides what the
    difference means; the binding is never rewritten to agree.
    """

    def __init__(self, message: str, field: str) -> None:
        super().__init__(message)
        self.field = field


class SchedulerStateError(StateError):
    """A scheduler file cannot be used as asked (ADR 0008 D6, D7, #181).

    Raised by :mod:`autoforge.scheduler_state` for a ``claim.json`` or a
    ``scheduler.json`` that is not a valid document of its schema (an unknown
    field or version, a missing field, an out-of-bound string or list, a
    broken generation order), for a claim operation its closing rule refuses,
    and for an operation called without the issue's run lock (L2). Nothing is
    written when it is raised. A corrupt ``scheduler.json`` is the
    scheduler's global uncertainty, a corrupt ``claim.json`` its issue's
    ``uncertain``.
    """


class ClaimConflict(SchedulerStateError):
    """``claim.json`` on disk is not what its writer expected (ADR 0008 D7.2, #181).

    The ``(claim_id, generation)`` the caller holds is not the open claim on
    disk, or the slot it means to fill is taken: a stale claimant, which can
    never overwrite or finalize a newer generation. Classifies as
    ``uncertain``; the file is left as it is, never overwritten to agree.
    Distinct from :class:`ClaimConflictError`, which is about GitHub claims.
    """


class OpenSchedulerClaimsError(SchedulerStateError):
    """``epic run`` found claims of its EPIC still open (ADR 0008 D14.4, #183).

    An earlier invocation ended without closing them (a crash, a kill, a
    second Ctrl-C, or an issue it held uncertain), so ``epic resume``, which
    reconciles them first, is the command to run. Raised holding L0, before
    ``scheduler.json`` is written: nothing of the invocation was written.
    """


class SchedulerStateDirError(ConfigurationError):
    """A standalone command was pointed at a state directory inside the scheduler root.

    ``run``, ``step``, ``resume`` and ``local run`` refuse a ``--state-dir``
    or ``config.state_dir`` that resolves inside
    ``<git common dir>/autoforge/scheduler/`` (ADR 0008 D17.3, #178), before
    the lock is taken or any state is read or written; the message names the
    scheduler commands instead. ``status`` reads such a directory and
    ``unblock`` acts on it.
    """


class StateTransitionError(AutoForgeError):
    """Illegal phase transition or step on a terminal phase."""


class LockError(AutoForgeError):
    """Another controller instance holds the repository lock."""


class IssueBusy(LockError):
    """An issue's run lock (L2) is held by another process (ADR 0008 D11.1, #180).

    L2 is only ever taken non-blocking, so a held one is this refusal at
    once, never a wait.
    """


class LockProofError(LockError):
    """An inherited lock descriptor was not proven to hold its lock (ADR 0008 D9.2, #180).

    The descriptor names another object than the expected one, or nothing
    holds the expected object's lock; nothing was acquired or released.
    """


class NetworkFilesystemError(LockError):
    """A scheduler lock lies on a filesystem whose ``flock`` is not a local kernel lock.

    NFS, CIFS/SMB, 9p, AFS, cluster filesystems and every FUSE filesystem
    (ADR 0008 D11.4, #180): the live-ownership proof depends on local locks,
    so scheduler mode refuses them. Standalone commands never check.
    """


class SchedulerOwnedIssueError(ConfigurationError):
    """A standalone command was asked to act on an issue a scheduler namespace owns.

    ADR 0008 D17.2's forward guard (#180): the namespace holds a state, a
    live worker holds its run lock, or the namespace cannot be read
    (fail-closed), so only the scheduler continues it until ``autoforge epic
    archive`` releases it. Nothing was read past the guard and nothing was
    written.
    """


class GitLockTimeout(AutoForgeError):
    """The shared-Git lock (L1) was not obtained within its bounded wait (ADR 0008 D11.3).

    Transient. Each operation L1 guards turns it into its own failure (a
    fetch's :class:`GitTransportError`, an import's failure, a worktree
    creation's :class:`VerificationError`) and is handled exactly as that
    operation failing; it is never a :class:`LockError` exit and never
    retried in place.
    """


class LockOrderError(AutoForgeError):
    """A lock was taken out of the order L0 -> L2 -> L1, or work was done under L1.

    ADR 0008 D11.2's debug assertion (#180): the holder of the shared-Git
    lock acquires no lock, invokes no agent, calls no GitHub and saves no
    state. Always a controller bug; never retried.
    """


class WorkerStartupError(AutoForgeError):
    """A scheduler worker refused to start (ADR 0008 D9.2, #182).

    Its argv does not parse, an inherited descriptor was not proven to hold
    its lock, or the namespace's binding, its open claim, ``scheduler.json``
    or the configuration digest does not match what the worker was started
    for. The worker exits 2 before it reads or writes any state.
    """


class ExecutionError(AutoForgeError):
    """Agent/CLI subprocess failed (non-zero exit, spawn failure, ...)."""


class ExecutionTimeoutError(ExecutionError):
    """Subprocess exceeded its timeout and was terminated."""


class InteractionDataError(ExecutionError):
    """A provider's interaction data is refused, never clipped or rewritten (#149).

    Raised by :mod:`autoforge.sdk_policy` for a value that decides what a
    tool may do or what the agent is told (a tool input, a question's
    option, an answer, a modified input) when it is past its bound, carries
    a control character or an escape sequence, or would be changed by
    redaction: approving a clipped or redacted rendering would approve
    different work. The message names the field and the rule, never the
    value.
    """


class InteractionRequiredError(ExecutionError):
    """The agent asked for a decision it did not get (ADR 0007 D7.6, D10.5; #134).

    Raised by the engine for an invocation whose
    ``AgentExecutionResult.interaction`` ended ``failed``: its policy is
    ``fail``, or a live wait could not be answered and no pause is
    available. Raised too for a ``paused`` one while no pending-interaction
    record exists to pause into (#126): nothing is pending. Never corrected
    and never relaunched as transient, since a relaunch would ask again; the
    phase is unchanged, and what was saved before the launch (the attempt
    counter, a LOCAL launch charge) stands.
    """


class ChildStdinClosedError(ExecutionError):
    """A duplex child closed its stdin while a record was being written (EPIPE)."""


class SandboxPolicyError(AutoForgeError):
    """A sandbox launch cannot be described as asked (ADR 0005 D4.2, D4.3, D7.2).

    Raised by the policy compiler (``autoforge.sandbox``) before anything is
    spawned: a relative or missing source, a link in a controller-owned
    path, a socket or device, a forbidden source, a denied root left
    visible, an overlap, a keyring login. ``path`` and ``rule`` name what was
    refused and why; the message never quotes a file's contents.
    """

    def __init__(self, path: str, rule: str, detail: str) -> None:
        super().__init__(f"sandbox policy refuses {path}: {rule}: {detail}")
        self.path = path
        self.rule = rule


class SandboxLaunchRefusedError(ExecutionError):
    """The outer sandbox could not be established for a launch (ADR 0005 D10.8, D14.2).

    The platform is not Linux, the backend is missing or lacks a capability,
    or the backend failed to set the sandbox up (a bind source gone, an
    executable the sandbox cannot run, a namespace refused). It is a launch
    refusal on the path of a failure to spawn: the command never ran, and
    nothing it would have written is attributed to it. ``facts`` is the
    launch's ``autoforge.sandbox.SandboxFacts`` (``enforced`` False, the
    bounded, redacted reason) when the backend got as far as preparing it.
    """

    def __init__(self, message: str, facts: object = None) -> None:
        super().__init__(message)
        self.facts = facts


class ControlResultError(AutoForgeError):
    """CONTROL_RESULT block missing, ambiguous, or not valid JSON."""


class ControlResultValidationError(ControlResultError):
    """CONTROL_RESULT parsed but failed schema/phase validation."""


class GitHubError(AutoForgeError):
    """`gh` CLI invocation or GitHub state problem.

    Raised as-is for *conclusive* failures (authentication, permissions,
    a missing PR, malformed data); see :class:`GitHubUnavailableError` for
    the transient kind. Callers that must not guess distinguish the two.
    """


class GitHubUnavailableError(GitHubError):
    """GitHub could not be reached or answered with a transient error.

    Timeouts, connection resets, 5xx / rate limiting: the same read may well
    succeed later, so the controller treats the data as *inconclusive*
    (bounded re-checks) rather than as a conclusive verification failure.
    """


class GitHubNotFoundError(GitHubError):
    """The referenced issue / PR does not resolve on GitHub (conclusive).

    ``gh`` answered, and the answer is that the object is not there (or is
    not visible to the authenticated token, which GitHub reports the same
    way). Callers that verify an *untrusted selection* treat this as a bad
    selection; every other conclusive :class:`GitHubError` (authentication,
    permissions, malformed data) is not about the selection at all.
    """


class SnapshotRefused(AutoForgeError):
    """An EPIC's dependency graph cannot be taken as a complete snapshot (ADR 0008 D3, #179).

    Raised by :func:`autoforge.epic_graph.build_epic_snapshot` instead of
    returning a partial snapshot: a read that failed, a listing whose length
    differs from its summary count, a bound exceeded, an EPIC that is not
    open or not in the verified repository, an inconsistent answer, or a
    cycle among open children. ``reason`` is one of :data:`SNAPSHOT_REFUSALS`;
    ``cycle`` is the canonical cycle (issue numbers, starting at the lowest)
    when ``reason`` is ``cycle``; ``transient`` is whether the refusal came
    from a GitHub read that may well succeed later. Nothing is admitted
    from a refused snapshot, and the next pass takes a new one. The message
    names issue numbers, states and reasons, never a title or a body.
    """

    def __init__(
        self,
        message: str,
        reason: str,
        *,
        cycle: tuple[int, ...] = (),
        transient: bool = False,
    ) -> None:
        if reason not in SNAPSHOT_REFUSALS:
            raise ValueError(f"unknown snapshot refusal reason {reason!r}")
        super().__init__(message)
        self.reason = reason
        self.cycle = tuple(cycle)
        self.transient = transient


# The closed set of :class:`SnapshotRefused` reasons.
SNAPSHOT_REFUSALS = frozenset(
    {
        "read-failed",
        "repository-mismatch",
        "epic-not-open",
        "children-bound",
        "children-count",
        "blockers-bound",
        "blockers-count",
        "inconsistent",
        "cycle",
    }
)


class ClaimConflictError(AutoForgeError):
    """A durable-claim read is inconclusive or ambiguous (``autoforge.claims``).

    Raised when the objects carrying a marker kind cannot answer "which one
    is this phase's write": a marker that could not be read (defective
    object), several objects claiming the same key, or none where exactly
    one is required. A phase entry turns it into ``BLOCKED`` without
    launching an agent; a read-back turns it into :class:`VerificationError`.
    """


class VerificationError(AutoForgeError):
    """Post-execution verification failed (HEAD mismatch, PR state, guards).

    Also used for Phase-1 safety gates (e.g. merge not explicitly allowed).
    """


class CheckoutDriftError(VerificationError):
    """The operator's checkout (HEAD or branch) moved while an agent ran.

    Raised by the engine around a REMOTE agent invocation; the step enters
    BLOCKED with the message as its reason. The invocation's own outcome, if
    it failed, is the ``__cause__`` and is quoted in the message.
    """


class SandboxTamperError(VerificationError):
    """An issue's private object directory holds what no git object store does.

    A directory that is not a real one reached from the common dir through
    real directories (a link, another file type, one that cannot be
    checked), an ``info/alternates`` or ``info/http-alternates`` file, or an
    entry that is neither a directory, a regular file nor an overlay
    whiteout (ADR 0005 D6.5): git would read another repository's objects
    with the operator's authority. Raised before anything is read from it;
    the step enters BLOCKED with the message, which names the entry.
    """


class CandidateNotMaterializableError(VerificationError):
    """A candidate's objects are not in the operator's shared object store.

    The commit, or a blob its tree names, is missing from the store the
    controller's own git reads (ADR 0006 D6.1): a candidate still only in a
    private object directory, before the strict import, is one. Raised by
    :mod:`autoforge.materialize` before anything is written for a missing
    commit; a refusal of that candidate, not a reason to materialize it again.
    """


class MaterializationError(VerificationError):
    """A candidate's tree was not written out, or not proven to be what was written.

    Raised by :mod:`autoforge.materialize` (ADR 0006 D6.2, D6.3) for a
    listing it refuses (an entry name that is ``.``, ``..`` or ``.git``, a
    name holding ``/``, an unknown mode), a write that failed (a full disk,
    a name the filesystem merged with another) or a directory whose bytes
    and modes do not hash back to the commit's tree. Inconclusive: no
    evidence, and the message names the commit and the failing entry.
    """


class ValidationDefinitionError(AutoForgeError):
    """A validation definition is beyond the bounds a record can hold (ADR 0006 D10.3).

    More than 16 definitions in a set, more than 32 elements in an argv, a
    blank element, an element over 1,024 characters or an argv over 4,096
    in all. The configuration loader turns it into a
    :class:`ConfigurationError`; a LOCAL run contract persisted before the
    bounds existed that holds one enters ``BLOCKED`` before any validator is
    launched. The message names the definition and the bound, never the argv.
    """


class ValidationInconclusiveError(VerificationError):
    """Candidate validation observed no result (ADR 0006 D10.5).

    A launch or setup failure the executor or the sandbox backend reported,
    or #165's preflight refusing the launch, after the bounded in-process
    re-check; or a materialization failure (an ``OSError`` or a git process
    the executor could not launch among them), or failing to find the
    shared store or read the candidate's commit there, after one fresh
    attempt. No ``ValidationRecord`` is written. The caller enters
    ``BLOCKED`` with the message, which names the command or the candidate
    and the failing entry.
    """


class ValidationInterruptedError(AutoForgeError):
    """A definition was interrupted more often than recovery may rerun it (ADR 0006 D10.7).

    Raised when the resume path's ``INTERRUPTED`` record is the third, or
    later, for one candidate, definition and policy (records at or below the
    operator's override watermark not counted). The record is still written:
    ``records`` is the list with it appended, which the caller persists in
    the write that enters ``BLOCKED`` with the message, naming the candidate
    and the definition.
    """

    def __init__(self, message: str, records: list | None = None) -> None:
        super().__init__(message)
        self.records = list(records or [])


class GitTransportError(AutoForgeError):
    """A controller git operation could not establish what it needs (fails closed).

    Raised by :mod:`autoforge.git_transport` when a fetch or push fails, a
    commit's bytes do not hash to its id, a range walk exceeds its bound or
    never reaches its base, or GitHub's ancestry answer is unavailable. It is
    never read as "allowed" (ADR 0004 D7.5).
    """


class EffectConflictError(AutoForgeError):
    """A controller-owned effect cannot be completed without guessing (ADR 0004 D4.2).

    The effect's identity resolves to an object whose payload or state differs
    from the record, to two objects, or the precondition no longer holds and
    the attempt bound leaves no issue. The message names the object and the
    rule, never the payload; the phase enters ``BLOCKED`` with it.
    """
