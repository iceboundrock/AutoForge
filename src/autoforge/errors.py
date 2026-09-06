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


class VerificationError(AutoForgeError):
    """Post-execution verification failed (HEAD mismatch, PR state, guards).

    Also used for Phase-1 safety gates (e.g. merge not explicitly allowed).
    """
