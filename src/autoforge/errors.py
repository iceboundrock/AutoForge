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
