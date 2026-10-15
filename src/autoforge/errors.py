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


class StateError(AutoForgeError):
    """State file missing, unreadable, corrupted, or schema-invalid.

    Raised on read of a corrupted state; the controller must never silently
    re-initialize a fresh state over existing data.
    """


class StateTransitionError(AutoForgeError):
    """Illegal phase transition or step on a terminal phase."""


class LockError(AutoForgeError):
    """Another controller instance holds the repository lock."""


class ExecutionError(AutoForgeError):
    """Agent/CLI subprocess failed (non-zero exit, spawn failure, ...)."""


class ExecutionTimeoutError(ExecutionError):
    """Subprocess exceeded its timeout and was terminated."""


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
