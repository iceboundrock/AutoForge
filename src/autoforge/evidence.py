"""Candidate validation evidence: definitions, policy, records (ADR 0006, #172).

The pure part of candidate validation. A **definition** is one trusted argv
the operator configured (``validation.commands`` for REMOTE, the run
contract's ``local.validation_commands`` for LOCAL), bound by a
``definition_digest`` over what runs (D3.5). **Policy P** is how it runs: the
sandbox mode, a path-independent template of the outer sandbox, the
environment allow-list's names, the timeout, ``contain_orphans`` and the
materialization method, bound by a ``policy_digest`` (D8.1). A **candidate**
is exactly one of two identities, :class:`RemoteCandidate` (repository,
object format, commit and tree, plus its provenance) or
:class:`LocalCandidate` (workspace fingerprint, workspace-policy digest and
run id), under a ``kind`` discriminator (D2.1-D2.7).

A ``ValidationRecord`` is what the controller observed when it ran one
definition against one candidate under one policy (D10.1). It has one of
two shapes, chosen by its ``outcome``:

- **observed** (``PASS``, ``FAIL``, ``TIMEOUT``): the enforcement facts the
  executor returned, the exit status, the timing, ADR 0002's leftovers and
  the bounded output, and for LOCAL the fingerprints the walks before and
  after the command read;
- **interrupted** (``INTERRUPTED``): built by the resume path from a
  leftover attempt marker alone (D10.7). It claims no launch and is never
  evidence.

A launch, setup or materialization failure is no record at all: it is
inconclusive (D10.5), and the caller re-checks within its bound.

Everything here is persisted in ``state.json`` (D11.1), validated on load
field by field against the bounds of D10.3, digest-checked, and checked for
referential integrity (:func:`check_evidence`). A value that fails is
corruption (:class:`~autoforge.errors.StateError`): never clipped, defaulted
or repaired. The writers build values through this module only, so a
writer never produces one the loader would refuse. Loading is read-only
(D11.3): converting a leftover marker (:func:`recover_attempt`) is the
engine's, under the repository lock.

This module does no I/O and knows no provider; the engine launches the
validators and saves the state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import NoReturn

from .errors import (
    ConfigurationError,
    StateError,
    ValidationDefinitionError,
    ValidationInterruptedError,
)
from .executor import MAX_DEADLINE_SECONDS, ExecutionResult
from .materialize import METHOD as METHOD_BYTE_EXACT
from .redaction import MAX_GROWTH_FACTOR, redact, redact_argv
from .result_parser import MAX_URL_CHARS
from .runlog import RUN_ID_RE, STEP_DIR_RE
from .sandbox import SandboxFacts
from .validation import parse_comment_url, parse_pr_url

# -- bounds (D10.3) --------------------------------------------------------------------
# The one bound definition for every Wave 2B persisted field; the writers and
# the loader use the same constants. Once merged they are part of the state
# protocol: lowering one is a protocol change.

SCHEMA = 1

# Lists.
MAX_DEFINITIONS = 16
MAX_RECORDS = 64
MAX_BOUND_RECORDS = MAX_DEFINITIONS
MAX_ARGV_ELEMENTS = 32

# An argv as configured, and as a record or a marker stores it: redacted, at
# most the configured bounds times redaction's growth factor.
MAX_ARGV_ELEMENT_CHARS = 1024
MAX_ARGV_CHARS = 4096
MAX_STORED_ARGV_ELEMENT_CHARS = MAX_ARGV_ELEMENT_CHARS * MAX_GROWTH_FACTOR
MAX_STORED_ARGV_CHARS = MAX_ARGV_CHARS * MAX_GROWTH_FACTOR

# Each stored output tail, after escaping: today's VerificationError tail.
MAX_TAIL_CHARS = 2000

MAX_REPOSITORY_CHARS = 200
MAX_REF_CHARS = 255
MAX_WORKTREE_ID_CHARS = 255
MAX_RUN_LOG_CHARS = 256
MAX_BACKEND_CHARS = 64
MAX_TEARDOWN_CHARS = 128
MAX_TIMESTAMP_CHARS = 32

# Integers.
MAX_SEQ = 2**31 - 1
MAX_BYTE_COUNT = 2**53 - 1
MAX_COMMENT_ID = 2**53 - 1
MIN_EXIT_CODE = -128
MAX_EXIT_CODE = 255
# D5.2's bound on a producer's validation failures (#173 counts them).
MAX_FAILURE_COUNT = 3
# More INTERRUPTED records than this for one key enters BLOCKED (D10.7).
MAX_INTERRUPTIONS = 2

# Enumerations.
KIND_REMOTE = "remote"
KIND_LOCAL = "local"
PROVENANCE_WORKTREE = "worktree"
PROVENANCE_PR_HEAD = "pr-head"
OBJECT_FORMATS = {"sha1": 40, "sha256": 64}
SCOPE = "candidate-tree"
CWD_ROLE = "candidate-root"
MODES = ("disabled", "preferred", "required")
NETWORKS = ("host", "isolated", "")
FAILURE_PHASES = ("ANALYZE_EXECUTE", "FIX")
METHOD_IN_PLACE = "in-place"
METHODS = (METHOD_BYTE_EXACT, METHOD_IN_PLACE)

# The canonical encodings' version labels (D3.5, D8.1).
DEFINITION_ENCODING = "autoforge-validation-definition-v1"
SET_ENCODING = "autoforge-validation-set-v1"
POLICY_ENCODING = "autoforge-validation-policy-v1"
ALLOWLIST_ENCODING = "autoforge-validation-env-names-v1"

# The template under ``sandbox.mode: disabled``: there is no outer sandbox,
# so there is nothing of one to describe (D8.1).
NO_SANDBOX_TEMPLATE: Mapping[str, object] = {"outer_sandbox": "none"}

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_REF_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//|/\.|\.lock(?:/|\Z)")
_COMPONENT_FORBIDDEN_RE = re.compile(r"[/\x00-\x1f\x7f-\x9f]")
# What a stored tail never holds unescaped: every C0 and C1 control
# character but newline and tab, DEL, and the two Unicode line separators.
_UNESCAPED_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")


class Outcome(StrEnum):
    """D10.4. ``INTERRUPTED`` is never observed: only recovery writes it."""

    PASS = "PASS"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    INTERRUPTED = "INTERRUPTED"


OBSERVED_OUTCOMES = (Outcome.PASS, Outcome.FAIL, Outcome.TIMEOUT)


# -- canonical encoding ----------------------------------------------------------------


def canonical_json(value: object) -> str:
    """Sorted keys, no whitespace, ASCII: the encoding every digest here is over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


# -- definitions (D3.1-D3.6, D4.2) -----------------------------------------------------


def definition_problem(commands: Sequence[Sequence[str]]) -> str:
    """Why ``commands`` cannot be a validation set (D10.3), or "".

    Names the definition by its index and the bound it is beyond, never the
    argv (it may hold what the operator should not have put there).
    """
    if len(commands) > MAX_DEFINITIONS:
        return f"{len(commands)} definitions, over the bound of {MAX_DEFINITIONS} per set"
    for index, argv in enumerate(commands):
        if not argv:
            return f"definition [{index}] is empty"
        if len(argv) > MAX_ARGV_ELEMENTS:
            return (
                f"definition [{index}] has {len(argv)} argv elements, over the bound of "
                f"{MAX_ARGV_ELEMENTS}"
            )
        for position, element in enumerate(argv):
            if not isinstance(element, str) or not element.strip():
                return f"definition [{index}] element {position} is blank"
            if len(element) > MAX_ARGV_ELEMENT_CHARS:
                return (
                    f"definition [{index}] element {position} is {len(element)} characters, "
                    f"over the bound of {MAX_ARGV_ELEMENT_CHARS}"
                )
        total = sum(len(element) for element in argv)
        if total > MAX_ARGV_CHARS:
            return (
                f"definition [{index}] is {total} characters in all, over the bound of "
                f"{MAX_ARGV_CHARS}"
            )
    return ""


def check_definitions(commands: Sequence[Sequence[str]]) -> None:
    """Raise :class:`ValidationDefinitionError` when ``commands`` is beyond D10.3."""
    problem = definition_problem(commands)
    if problem:
        raise ValidationDefinitionError(problem)


def check_scope(scope: object) -> None:
    """D4.2: ``candidate-tree`` is the only scope Wave 2B implements."""
    if scope != SCOPE:
        raise ConfigurationError(
            f"validation.scope must be {SCOPE!r}, the only scope there is, got {scope!r}: "
            "base-bound and merge-result-bound validation do not exist"
        )


def definition_digest(argv: Sequence[str], index: int) -> str:
    """D3.5: SHA-256 over what runs, the configured (unredacted) argv included."""
    return _sha256(
        {
            "version": DEFINITION_ENCODING,
            "argv": list(argv),
            "cwd": CWD_ROLE,
            "scope": SCOPE,
            "index": index,
        }
    )


def set_digest(digests: Sequence[str]) -> str:
    """D3.5, D3.6: the ordered definition digests; the empty set has one too."""
    return _sha256({"version": SET_ENCODING, "definitions": list(digests)})


@dataclass(frozen=True)
class Definition:
    """One trusted definition of a set, at its index (D3.5)."""

    index: int
    argv: tuple[str, ...]

    @property
    def digest(self) -> str:
        return definition_digest(self.argv, self.index)

    @property
    def shown(self) -> str:
        """The argv as it may be displayed: redacted, space-joined."""
        return " ".join(redact_argv(list(self.argv)))

    def stored(self) -> dict:
        """The form a record and a marker hold: the argv redacted."""
        return {
            "index": self.index,
            "argv": redact_argv(list(self.argv)),
            "definition_digest": self.digest,
            "scope": SCOPE,
        }


def definitions(commands: Sequence[Sequence[str]]) -> tuple[Definition, ...]:
    """The set ``commands`` configures, checked against D10.3."""
    check_definitions(commands)
    return tuple(Definition(i, tuple(argv)) for i, argv in enumerate(commands))


def validation_set_digest(defs: Sequence[Definition]) -> str:
    return set_digest([d.digest for d in defs])


# -- policy P (D8.1, D8.2) -------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """How a set runs. Path-independent, so two exports agree (D8.1)."""

    mode: str
    template: Mapping[str, object]
    env_names: tuple[str, ...]
    timeout_seconds: int
    contain_orphans: bool
    method: str

    @property
    def template_digest(self) -> str:
        return _sha256(self.template)

    @property
    def env_names_digest(self) -> str:
        return _sha256({"version": ALLOWLIST_ENCODING, "names": sorted(set(self.env_names))})

    @property
    def digest(self) -> str:
        return _sha256(
            {
                "version": POLICY_ENCODING,
                "mode": self.mode,
                "template": self.template_digest,
                "env_allowlist": self.env_names_digest,
                "timeout_seconds": self.timeout_seconds,
                "contain_orphans": self.contain_orphans,
                "method": self.method,
            }
        )

    @property
    def requires_enforcement(self) -> bool:
        """D8.3: a record applies under this policy only when it was enforced."""
        return self.mode in ("required", "preferred")

    def stored(self) -> dict:
        return {
            "policy_digest": self.digest,
            "mode": self.mode,
            "timeout_seconds": self.timeout_seconds,
        }

    def describe(self) -> str:
        """One line for the dry-run plan and ``status``."""
        return (
            f"mode {self.mode}, digest {self.digest[:16]}..., timeout {self.timeout_seconds}s, "
            f"method {self.method}"
        )


# -- candidates (D2.1-D2.7) ------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeProvenance:
    """An issue worktree's candidate (D2.3); displayed, never compared."""

    issue_number: int
    worktree_id: str
    strict_import: bool
    # The base revision the phase's entry observation required; "" for none.
    base_revision: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": PROVENANCE_WORKTREE,
            "issue_number": self.issue_number,
            "worktree_id": self.worktree_id,
            "strict_import": self.strict_import,
            "base_revision": self.base_revision,
        }


@dataclass(frozen=True)
class PrHeadProvenance:
    """A published candidate: the head of the canonical PR URL (D2.3)."""

    pr_url: str

    def to_dict(self) -> dict:
        return {"kind": PROVENANCE_PR_HEAD, "pr_url": self.pr_url}


Provenance = WorktreeProvenance | PrHeadProvenance


@dataclass(frozen=True)
class RemoteCandidate:
    """REMOTE's validation identity: a commit and the tree it names (D2.1)."""

    repository: str
    object_format: str
    commit: str
    tree: str
    provenance: Provenance

    kind = KIND_REMOTE

    @property
    def identity(self) -> tuple[str, ...]:
        """The key records are compared under: provenance is not part of it."""
        return (KIND_REMOTE, self.repository.lower(), self.object_format, self.commit, self.tree)

    def to_dict(self) -> dict:
        return {
            "kind": KIND_REMOTE,
            "repository": self.repository,
            "object_format": self.object_format,
            "commit": self.commit,
            "tree": self.tree,
            "provenance": self.provenance.to_dict(),
        }

    def describe(self) -> str:
        return f"commit {self.commit[:12]} (tree {self.tree[:12]}) of {self.repository}"


@dataclass(frozen=True)
class LocalCandidate:
    """LOCAL's validation identity: the workspace as ADR 0001 fingerprints it (D2.6)."""

    fingerprint: str
    workspace_policy_digest: str
    run_id: str

    kind = KIND_LOCAL

    @property
    def identity(self) -> tuple[str, ...]:
        return (KIND_LOCAL, self.fingerprint, self.workspace_policy_digest, self.run_id)

    def to_dict(self) -> dict:
        return {
            "kind": KIND_LOCAL,
            "fingerprint": self.fingerprint,
            "workspace_policy_digest": self.workspace_policy_digest,
            "run_id": self.run_id,
        }

    def describe(self) -> str:
        return f"workspace {self.fingerprint[:16]}..."


Candidate = RemoteCandidate | LocalCandidate


# -- field checks ----------------------------------------------------------------------


def _fail(what: str, message: str) -> NoReturn:
    raise StateError(f"{what} {message}")


def _exact(raw: object, keys: Iterable[str], what: str) -> dict:
    if not isinstance(raw, dict):
        _fail(what, f"must be an object, got {type(raw).__name__}")
    wanted = tuple(keys)
    unknown = sorted(str(k) for k in raw if k not in wanted)
    if unknown:
        _fail(what, f"has unknown key(s) {unknown}")
    missing = [k for k in wanted if k not in raw]
    if missing:
        _fail(what, f"is missing key(s) {missing}")
    return raw


def _str(value: object, what: str, max_chars: int) -> str:
    if not isinstance(value, str):
        _fail(what, f"must be a string, got {type(value).__name__}")
    if len(value) > max_chars:
        _fail(what, f"is {len(value)} characters, over its bound of {max_chars}")
    return value


def _int(value: object, what: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(what, f"must be an integer, got {type(value).__name__}")
    if value < minimum or value > maximum:
        _fail(what, f"must be in [{minimum}, {maximum}], got {value}")
    return value


def _bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        _fail(what, f"must be a boolean, got {type(value).__name__}")
    return value


def _enum(value: object, what: str, allowed: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(what, f"must be one of {list(allowed)}, got {value!r:.80}")
    return value


def _hex64(value: object, what: str) -> str:
    if not isinstance(value, str) or not _HEX64_RE.match(value):
        _fail(what, "must be a 64-character lowercase SHA-256 hex digest")
    return value


def _oid(value: object, what: str, object_format: str) -> str:
    width = OBJECT_FORMATS[object_format]
    if (
        not isinstance(value, str)
        or len(value) != width
        or not all(c in "0123456789abcdef" for c in value)
    ):
        _fail(what, f"must be a full {width}-character lowercase {object_format} object id")
    return value


def _timestamp(value: object, what: str) -> str:
    text = _str(value, what, MAX_TIMESTAMP_CHARS)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        _fail(what, f"is not an ISO 8601 time: {text!r}")
    offset = moment.utcoffset()
    if offset is None or offset.total_seconds() != 0 or "T" not in text:
        _fail(what, f"must be an ISO 8601 UTC time as utcnow_iso writes it, got {text!r}")
    return text


def _repository(value: object, what: str) -> str:
    text = _str(value, what, MAX_REPOSITORY_CHARS)
    if not _REPOSITORY_RE.match(text):
        _fail(what, "must be an <owner>/<repo> repository name")
    return text


def _ref(value: object, what: str) -> str:
    text = _str(value, what, MAX_REF_CHARS)
    branch = text.removeprefix("refs/heads/")
    if (
        not branch
        or branch.startswith(("-", "/", "refs/"))
        or branch.endswith(("/", "."))
        or _REF_FORBIDDEN_RE.search(branch)
    ):
        _fail(what, "must be a plain branch name")
    return text


def _pr_url(value: object, what: str, repository: str) -> str:
    text = _str(value, what, MAX_URL_CHARS)
    try:
        ref = parse_pr_url(text)
    except ConfigurationError:
        _fail(what, "is not a GitHub pull request URL")
    if ref.canonical != text:
        _fail(what, "must be stored in its canonical form")
    if not ref.same_repository(repository):
        _fail(what, f"is not a pull request of {repository}")
    return text


def _run_id(value: object, what: str) -> str:
    text = _str(value, what, 128)
    if not RUN_ID_RE.match(text) or text in (".", ".."):
        _fail(what, "must be a run id (a single safe path component)")
    return text


def _run_log(value: object, what: str) -> str:
    text = _str(value, what, MAX_RUN_LOG_CHARS)
    run_id, slash, step = text.partition("/")
    if (
        not slash
        or not RUN_ID_RE.match(run_id)
        or run_id in (".", "..")
        or not (STEP_DIR_RE.match(step))
    ):
        _fail(what, "must be <run id>/<step directory>")
    return text


def _worktree_id(value: object, what: str) -> str:
    text = _str(value, what, MAX_WORKTREE_ID_CHARS)
    if not text or text in (".", "..") or _COMPONENT_FORBIDDEN_RE.search(text):
        _fail(what, "must be one path component with no '/', NUL or control character")
    return text


def _stored_argv(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not value:
        _fail(what, "must be a non-empty list of strings")
    if len(value) > MAX_ARGV_ELEMENTS:
        _fail(what, f"holds {len(value)} elements, over its bound of {MAX_ARGV_ELEMENTS}")
    for i, item in enumerate(value):
        _str(item, f"{what}[{i}]", MAX_STORED_ARGV_ELEMENT_CHARS)
    total = sum(len(item) for item in value)
    if total > MAX_STORED_ARGV_CHARS:
        _fail(what, f"is {total} characters in all, over its bound of {MAX_STORED_ARGV_CHARS}")
    return value


def _tail(value: object, what: str) -> str:
    text = _str(value, what, MAX_TAIL_CHARS)
    if _UNESCAPED_RE.search(text):
        _fail(what, "holds an unescaped control character")
    return text


# -- parsing ---------------------------------------------------------------------------

_REMOTE_KEYS = ("kind", "repository", "object_format", "commit", "tree", "provenance")
_LOCAL_KEYS = ("kind", "fingerprint", "workspace_policy_digest", "run_id")
_WORKTREE_KEYS = ("kind", "issue_number", "worktree_id", "strict_import", "base_revision")
_PR_HEAD_KEYS = ("kind", "pr_url")
_DEFINITION_KEYS = ("index", "argv", "definition_digest", "scope")
_POLICY_KEYS = ("policy_digest", "mode", "timeout_seconds")
_ENFORCEMENT_KEYS = ("enforced", "backend", "backend_version", "network", "teardown")
_LEFTOVER_KEYS = (
    "descendants_killed",
    "group_survived_kill",
    "orphans_killed",
    "orphan_survived_kill",
    "orphans_unchecked",
)
_OUTPUT_KEYS = (
    "stdout_tail",
    "stderr_tail",
    "stdout_bytes",
    "stderr_bytes",
    "stdout_tail_truncated",
    "stderr_tail_truncated",
)
_BOTH_KEYS = ("schema", "seq", "digest", "candidate", "definition", "policy", "outcome", "run_log")
_OBSERVED_KEYS = ("enforcement", "exit_code", "started_at", "finished_at", "leftovers", "output")
_INTERRUPTED_KEYS = ("marked_at", "recovered_at")
_MARKER_KEYS = ("candidate", "definition", "policy", "run_log", "marked_at")
_BINDING_KEYS = ("review_round", "candidate", "records", "validation_set_digest", "policy_digest")
_PAIR_KEYS = ("seq", "digest")
_COUNTER_KEYS = ("phase", "count", "candidate_commit", "last_record_seq")
_CERTIFICATE_BOTH = (
    "schema",
    "digest",
    "kind",
    "candidate",
    "review_round",
    "records",
    "validation_set_digest",
    "policy_digest",
    "clean",
    "issued_at",
)
_CERTIFICATE_REMOTE = (
    "reviewed_pr_url",
    "reviewed_head_sha",
    "reviewed_base_ref",
    "reviewed_merge_base_sha",
    "comment",
)
_CERTIFICATE_LOCAL = ("reviewed_workspace_fingerprint",)
_COMMENT_KEYS = ("url", "id", "marker_key")
_MARKER_KEY_KEYS = (
    "review_round",
    "reviewed_head_sha",
    "reviewed_base_ref",
    "reviewed_merge_base_sha",
)
_CLEAN_KEYS = ("needs_fix_round", "actionable_findings")


def _parse_provenance(raw: object, what: str, repository: str) -> Provenance:
    if not isinstance(raw, dict):
        _fail(what, f"must be an object, got {type(raw).__name__}")
    kind = raw.get("kind")
    if kind == PROVENANCE_WORKTREE:
        _exact(raw, _WORKTREE_KEYS, what)
        base = _str(raw["base_revision"], f"{what}.base_revision", 64)
        if base and not (len(base) in (40, 64) and _hex(base)):
            _fail(f"{what}.base_revision", "must be a full lowercase object id or empty")
        return WorktreeProvenance(
            issue_number=_int(raw["issue_number"], f"{what}.issue_number", 1, MAX_SEQ),
            worktree_id=_worktree_id(raw["worktree_id"], f"{what}.worktree_id"),
            strict_import=_bool(raw["strict_import"], f"{what}.strict_import"),
            base_revision=base,
        )
    if kind == PROVENANCE_PR_HEAD:
        _exact(raw, _PR_HEAD_KEYS, what)
        return PrHeadProvenance(_pr_url(raw["pr_url"], f"{what}.pr_url", repository))
    _fail(f"{what}.kind", f"must be {PROVENANCE_WORKTREE!r} or {PROVENANCE_PR_HEAD!r}")


def _hex(text: str) -> bool:
    return all(c in "0123456789abcdef" for c in text)


def parse_candidate(raw: object, what: str = "candidate") -> Candidate:
    """A candidate of exactly one kind (D2.7): mixed fields are refused."""
    if not isinstance(raw, dict):
        _fail(what, f"must be an object, got {type(raw).__name__}")
    kind = raw.get("kind")
    if kind == KIND_REMOTE:
        _exact(raw, _REMOTE_KEYS, what)
        repository = _repository(raw["repository"], f"{what}.repository")
        object_format = _enum(raw["object_format"], f"{what}.object_format", tuple(OBJECT_FORMATS))
        commit = _oid(raw["commit"], f"{what}.commit", object_format)
        tree = _oid(raw["tree"], f"{what}.tree", object_format)
        provenance = _parse_provenance(raw["provenance"], f"{what}.provenance", repository)
        if isinstance(provenance, WorktreeProvenance) and provenance.base_revision:
            _oid(provenance.base_revision, f"{what}.provenance.base_revision", object_format)
        return RemoteCandidate(repository, object_format, commit, tree, provenance)
    if kind == KIND_LOCAL:
        _exact(raw, _LOCAL_KEYS, what)
        return LocalCandidate(
            fingerprint=_hex64(raw["fingerprint"], f"{what}.fingerprint"),
            workspace_policy_digest=_hex64(
                raw["workspace_policy_digest"], f"{what}.workspace_policy_digest"
            ),
            run_id=_run_id(raw["run_id"], f"{what}.run_id"),
        )
    _fail(f"{what}.kind", f"must be {KIND_REMOTE!r} or {KIND_LOCAL!r}, got {kind!r:.40}")


def _parse_definition(raw: object, what: str) -> dict:
    _exact(raw, _DEFINITION_KEYS, what)
    assert isinstance(raw, dict)
    _int(raw["index"], f"{what}.index", 0, MAX_DEFINITIONS - 1)
    _stored_argv(raw["argv"], f"{what}.argv")
    _hex64(raw["definition_digest"], f"{what}.definition_digest")
    _enum(raw["scope"], f"{what}.scope", (SCOPE,))
    return raw


def _parse_policy(raw: object, what: str) -> dict:
    _exact(raw, _POLICY_KEYS, what)
    assert isinstance(raw, dict)
    _hex64(raw["policy_digest"], f"{what}.policy_digest")
    _enum(raw["mode"], f"{what}.mode", MODES)
    _int(raw["timeout_seconds"], f"{what}.timeout_seconds", 1, MAX_DEADLINE_SECONDS)
    return raw


def _parse_enforcement(raw: object, what: str) -> None:
    _exact(raw, _ENFORCEMENT_KEYS, what)
    assert isinstance(raw, dict)
    _bool(raw["enforced"], f"{what}.enforced")
    _str(raw["backend"], f"{what}.backend", MAX_BACKEND_CHARS)
    _str(raw["backend_version"], f"{what}.backend_version", MAX_BACKEND_CHARS)
    _enum(raw["network"], f"{what}.network", NETWORKS)
    _str(raw["teardown"], f"{what}.teardown", MAX_TEARDOWN_CHARS)


def _parse_output(raw: object, what: str) -> None:
    _exact(raw, _OUTPUT_KEYS, what)
    assert isinstance(raw, dict)
    for stream in ("stdout", "stderr"):
        _tail(raw[f"{stream}_tail"], f"{what}.{stream}_tail")
        _int(raw[f"{stream}_bytes"], f"{what}.{stream}_bytes", 0, MAX_BYTE_COUNT)
        _bool(raw[f"{stream}_tail_truncated"], f"{what}.{stream}_tail_truncated")


def record_digest(record: Mapping[str, object]) -> str:
    """SHA-256 over the canonical encoding of every field but ``digest`` (D10.1)."""
    return _sha256({k: v for k, v in record.items() if k != "digest"})


@dataclass(frozen=True)
class RecordView:
    """A loaded record's fields the integrity checks and the readers compare."""

    raw: dict
    seq: int
    digest: str
    candidate: Candidate
    index: int
    argv: tuple[str, ...]
    definition_digest: str
    policy_digest: str
    mode: str
    outcome: Outcome
    run_log: str
    enforced: bool = False
    exit_code: int | None = None
    start_fingerprint: str = ""
    end_fingerprint: str = ""

    @property
    def is_observed(self) -> bool:
        return self.outcome is not Outcome.INTERRUPTED


def parse_record(raw: object, what: str = "validation record") -> RecordView:
    """One record of exactly one shape and one kind, digest-checked (D10.1, D11.1)."""
    if not isinstance(raw, dict):
        _fail(what, f"must be an object, got {type(raw).__name__}")
    outcome_value = raw.get("outcome")
    if outcome_value not in tuple(Outcome):
        _fail(f"{what}.outcome", f"must be one of {[o.value for o in Outcome]}")
    outcome = Outcome(str(outcome_value))
    candidate_raw = raw.get("candidate")
    local = isinstance(candidate_raw, dict) and candidate_raw.get("kind") == KIND_LOCAL
    keys = list(_BOTH_KEYS)
    keys += _INTERRUPTED_KEYS if outcome is Outcome.INTERRUPTED else _OBSERVED_KEYS
    if local:
        keys.append("start_fingerprint")
        if outcome is not Outcome.INTERRUPTED:
            keys.append("end_fingerprint")
    _exact(raw, keys, what)
    _int(raw["schema"], f"{what}.schema", SCHEMA, SCHEMA)
    seq = _int(raw["seq"], f"{what}.seq", 1, MAX_SEQ)
    digest = _hex64(raw["digest"], f"{what}.digest")
    candidate = parse_candidate(candidate_raw, f"{what}.candidate")
    definition = _parse_definition(raw["definition"], f"{what}.definition")
    policy = _parse_policy(raw["policy"], f"{what}.policy")
    run_log = _run_log(raw["run_log"], f"{what}.run_log")
    start = end = ""
    if local:
        start = _hex64(raw["start_fingerprint"], f"{what}.start_fingerprint")
        assert isinstance(candidate, LocalCandidate)
        if start != candidate.fingerprint:
            _fail(f"{what}.start_fingerprint", "must equal the candidate's fingerprint")
    enforced = False
    exit_code: int | None = None
    if outcome is Outcome.INTERRUPTED:
        _timestamp(raw["marked_at"], f"{what}.marked_at")
        _timestamp(raw["recovered_at"], f"{what}.recovered_at")
    else:
        _parse_enforcement(raw["enforcement"], f"{what}.enforcement")
        enforced = raw["enforcement"]["enforced"]
        code = raw["exit_code"]
        if outcome is Outcome.TIMEOUT:
            if code is not None:
                _fail(f"{what}.exit_code", "must be null for a TIMEOUT")
        else:
            exit_code = _int(code, f"{what}.exit_code", MIN_EXIT_CODE, MAX_EXIT_CODE)
            if (exit_code == 0) != (outcome is Outcome.PASS):
                _fail(f"{what}.exit_code", f"{exit_code} contradicts the outcome {outcome.value}")
        _timestamp(raw["started_at"], f"{what}.started_at")
        _timestamp(raw["finished_at"], f"{what}.finished_at")
        _exact(raw["leftovers"], _LEFTOVER_KEYS, f"{what}.leftovers")
        for key in _LEFTOVER_KEYS:
            _bool(raw["leftovers"][key], f"{what}.leftovers.{key}")
        _parse_output(raw["output"], f"{what}.output")
        if local:
            end = _hex64(raw["end_fingerprint"], f"{what}.end_fingerprint")
    if record_digest(raw) != digest:
        _fail(f"{what}.digest", "does not match the record: it was edited or corrupted")
    return RecordView(
        raw=raw,
        seq=seq,
        digest=digest,
        candidate=candidate,
        index=definition["index"],
        argv=tuple(definition["argv"]),
        definition_digest=definition["definition_digest"],
        policy_digest=policy["policy_digest"],
        mode=policy["mode"],
        outcome=outcome,
        run_log=run_log,
        enforced=enforced,
        exit_code=exit_code,
        start_fingerprint=start,
        end_fingerprint=end,
    )


@dataclass(frozen=True)
class MarkerView:
    raw: dict
    candidate: Candidate
    definition_digest: str
    policy_digest: str
    run_log: str
    index: int
    argv: tuple[str, ...]


def parse_marker(raw: object, what: str = "validation_attempt") -> MarkerView:
    """The attempt marker (D11.2): what an interrupted record copies, nothing observed."""
    if not isinstance(raw, dict):
        _fail(what, f"must be an object, got {type(raw).__name__}")
    candidate_raw = raw.get("candidate")
    local = isinstance(candidate_raw, dict) and candidate_raw.get("kind") == KIND_LOCAL
    _exact(raw, (*_MARKER_KEYS, *(("start_fingerprint",) if local else ())), what)
    candidate = parse_candidate(candidate_raw, f"{what}.candidate")
    definition = _parse_definition(raw["definition"], f"{what}.definition")
    policy = _parse_policy(raw["policy"], f"{what}.policy")
    run_log = _run_log(raw["run_log"], f"{what}.run_log")
    _timestamp(raw["marked_at"], f"{what}.marked_at")
    if local:
        assert isinstance(candidate, LocalCandidate)
        if _hex64(raw["start_fingerprint"], f"{what}.start_fingerprint") != candidate.fingerprint:
            _fail(f"{what}.start_fingerprint", "must equal the candidate's fingerprint")
    return MarkerView(
        raw=raw,
        candidate=candidate,
        definition_digest=definition["definition_digest"],
        policy_digest=policy["policy_digest"],
        run_log=run_log,
        index=definition["index"],
        argv=tuple(definition["argv"]),
    )


def _parse_pairs(raw: object, what: str) -> list[tuple[int, str]]:
    if not isinstance(raw, list):
        _fail(what, "must be a list")
    if len(raw) > MAX_BOUND_RECORDS:
        _fail(what, f"holds {len(raw)} records, over its bound of {MAX_BOUND_RECORDS}")
    pairs: list[tuple[int, str]] = []
    for i, item in enumerate(raw):
        _exact(item, _PAIR_KEYS, f"{what}[{i}]")
        pairs.append(
            (
                _int(item["seq"], f"{what}[{i}].seq", 1, MAX_SEQ),
                _hex64(item["digest"], f"{what}[{i}].digest"),
            )
        )
    return pairs


# -- integrity (D11.1) -----------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceFields:
    """The six persisted additions of the Wave 2B migration (D11.2)."""

    validation_records: list = field(default_factory=list)
    validation_attempt: dict = field(default_factory=dict)
    review_evidence: dict = field(default_factory=dict)
    review_certificate: dict = field(default_factory=dict)
    producer_validation_failure: dict = field(default_factory=dict)
    validation_override_seq: int | None = None


def check_evidence(
    fields_: EvidenceFields, *, local: bool, repository: str, run_id: str
) -> list[RecordView]:
    """Validate the six fields together; return the records, parsed. Read-only.

    ``local`` is the state's mode, ``repository`` its REMOTE repository and
    ``run_id`` its run: a record of the other mode's kind, of another
    repository or run, or a run log of another run is refused (D2.7).
    """
    records_raw = fields_.validation_records
    if not isinstance(records_raw, list):
        _fail("state field 'validation_records'", "must be a list")
    if len(records_raw) > MAX_RECORDS:
        _fail(
            "state field 'validation_records'",
            f"holds {len(records_raw)} records, over its bound of {MAX_RECORDS}",
        )
    records: list[RecordView] = []
    for i, raw in enumerate(records_raw):
        where = f"state field 'validation_records'[{i}]"
        view = parse_record(raw, where)
        _check_candidate(view.candidate, f"{where}.candidate", local, repository, run_id)
        _check_run_log(view.run_log, f"{where}.run_log", run_id)
        if records and view.seq <= records[-1].seq:
            _fail(f"{where}.seq", "must increase strictly")
        records.append(view)
    by_seq = {r.seq: r for r in records}

    marker = fields_.validation_attempt
    if not isinstance(marker, dict):
        _fail("state field 'validation_attempt'", "must be an object")
    if marker:
        where = "state field 'validation_attempt'"
        pending = parse_marker(marker, where)
        _check_candidate(pending.candidate, f"{where}.candidate", local, repository, run_id)
        _check_run_log(pending.run_log, f"{where}.run_log", run_id)

    binding = fields_.review_evidence
    if not isinstance(binding, dict):
        _fail("state field 'review_evidence'", "must be an object")
    if binding:
        _check_binding(binding, by_seq, local, repository, run_id)

    certificate = fields_.review_certificate
    if not isinstance(certificate, dict):
        _fail("state field 'review_certificate'", "must be an object")
    if certificate:
        _check_certificate(certificate, by_seq, local, repository, run_id)

    counter = fields_.producer_validation_failure
    if not isinstance(counter, dict):
        _fail("state field 'producer_validation_failure'", "must be an object")
    if counter:
        if local:
            _fail("state field 'producer_validation_failure'", "must be empty for a LOCAL run")
        _check_counter(counter, by_seq)

    watermark = fields_.validation_override_seq
    if watermark is not None:
        what = "state field 'validation_override_seq'"
        if local:
            _fail(what, "must be null for a LOCAL run: LOCAL has no unblock")
        _int(watermark, what, 0, MAX_SEQ)
        highest = records[-1].seq if records else 0
        if watermark > highest:
            _fail(what, f"is {watermark}, above the highest retained record seq {highest}")
    return records


def _check_candidate(
    candidate: Candidate, where: str, local: bool, repository: str, run_id: str
) -> None:
    """D2.7: the state's own kind, repository and run."""
    if local != isinstance(candidate, LocalCandidate):
        mode = "LOCAL" if local else "REMOTE"
        _fail(where, f"holds a {candidate.kind} candidate in a {mode} run")
    if isinstance(candidate, LocalCandidate):
        if candidate.run_id != run_id:
            _fail(where, f"names run {candidate.run_id!r}, not this run {run_id!r}")
    elif repository and candidate.repository.lower() != repository.lower():
        _fail(where, f"names repository {candidate.repository!r}, not {repository!r}")


def _check_run_log(run_log: str, where: str, run_id: str) -> None:
    if run_log.partition("/")[0] != run_id:
        _fail(where, f"is not in this run's log directory ({run_id})")


def _check_named(
    pairs: list[tuple[int, str]],
    by_seq: Mapping[int, RecordView],
    candidate: Candidate,
    policy_digest: str,
    what: str,
) -> list[RecordView]:
    named: list[RecordView] = []
    for i, (seq, digest) in enumerate(pairs):
        where = f"{what}.records[{i}]"
        record = by_seq.get(seq)
        if record is None:
            _fail(where, f"names record seq {seq}, which is not retained")
        if record.digest != digest:
            _fail(where, f"names record seq {seq} under another digest")
        if record.outcome is Outcome.INTERRUPTED:
            _fail(where, f"names record seq {seq}, which is INTERRUPTED and never evidence")
        if record.candidate.identity != candidate.identity:
            _fail(where, f"names record seq {seq}, which is for another candidate")
        if record.policy_digest != policy_digest:
            _fail(where, f"names record seq {seq}, which ran under another policy")
        if record.index != i:
            _fail(where, f"names record seq {seq}, which is definition {record.index}, not {i}")
        named.append(record)
    return named


def _check_binding(
    raw: dict, by_seq: Mapping[int, RecordView], local: bool, repository: str, run_id: str
) -> None:
    what = "state field 'review_evidence'"
    _exact(raw, _BINDING_KEYS, what)
    _int(raw["review_round"], f"{what}.review_round", 1, MAX_SEQ)
    candidate = parse_candidate(raw["candidate"], f"{what}.candidate")
    _check_candidate(candidate, f"{what}.candidate", local, repository, run_id)
    pairs = _parse_pairs(raw["records"], f"{what}.records")
    set_value = _hex64(raw["validation_set_digest"], f"{what}.validation_set_digest")
    policy_digest = _hex64(raw["policy_digest"], f"{what}.policy_digest")
    named = _check_named(pairs, by_seq, candidate, policy_digest, what)
    # D10.6: the set stops at its first non-PASS, so only the last record named
    # may be one. A binding naming PASS records only (none, for an empty set)
    # names every definition of its set, and their digests encode to its set
    # digest (D3.5). After a non-PASS the definitions that got no record are
    # not in the binding, so that set digest cannot be recomputed here: D5.5
    # compares it with the set in force, and D12.2 issues no certificate from it.
    for i, record in enumerate(named[:-1]):
        if record.outcome is not Outcome.PASS:
            _fail(
                f"{what}.records[{i}]",
                f"names a {record.outcome.value} record before the last; "
                "the set stops at its first non-PASS",
            )
    if all(record.outcome is Outcome.PASS for record in named):
        if set_digest([r.definition_digest for r in named]) != set_value:
            _fail(f"{what}.validation_set_digest", "is not the digest of the records' definitions")


def certificate_digest(certificate: Mapping[str, object]) -> str:
    return _sha256({k: v for k, v in certificate.items() if k != "digest"})


def _check_certificate(
    raw: dict, by_seq: Mapping[int, RecordView], local: bool, repository: str, run_id: str
) -> None:
    what = "state field 'review_certificate'"
    kind = raw.get("kind")
    if kind not in (KIND_REMOTE, KIND_LOCAL):
        _fail(f"{what}.kind", f"must be {KIND_REMOTE!r} or {KIND_LOCAL!r}")
    extra = _CERTIFICATE_LOCAL if kind == KIND_LOCAL else _CERTIFICATE_REMOTE
    _exact(raw, (*_CERTIFICATE_BOTH, *extra), what)
    _int(raw["schema"], f"{what}.schema", SCHEMA, SCHEMA)
    digest = _hex64(raw["digest"], f"{what}.digest")
    candidate = parse_candidate(raw["candidate"], f"{what}.candidate")
    if candidate.kind != kind:
        _fail(f"{what}.kind", "differs from its candidate's kind")
    _check_candidate(candidate, f"{what}.candidate", local, repository, run_id)
    review_round = _int(raw["review_round"], f"{what}.review_round", 1, MAX_SEQ)
    pairs = _parse_pairs(raw["records"], f"{what}.records")
    set_value = _hex64(raw["validation_set_digest"], f"{what}.validation_set_digest")
    policy_digest = _hex64(raw["policy_digest"], f"{what}.policy_digest")
    named = _check_named(pairs, by_seq, candidate, policy_digest, what)
    for i, record in enumerate(named):
        if record.outcome is not Outcome.PASS:
            _fail(
                f"{what}.records[{i}]",
                f"names a {record.outcome.value} record; a certificate names PASS records only",
            )
    if set_digest([r.definition_digest for r in named]) != set_value:
        _fail(f"{what}.validation_set_digest", "is not the digest of the records' definitions")
    clean = _exact(raw["clean"], _CLEAN_KEYS, f"{what}.clean")
    if clean["needs_fix_round"] is not False:
        _fail(f"{what}.clean.needs_fix_round", "must be false")
    if _int(clean["actionable_findings"], f"{what}.clean.actionable_findings", 0, 0) != 0:
        _fail(f"{what}.clean.actionable_findings", "must be 0")
    _timestamp(raw["issued_at"], f"{what}.issued_at")
    if isinstance(candidate, LocalCandidate):
        name = f"{what}.reviewed_workspace_fingerprint"
        if _hex64(raw["reviewed_workspace_fingerprint"], name) != candidate.fingerprint:
            _fail(name, "differs from the candidate's")
    else:
        pr_url = _pr_url(raw["reviewed_pr_url"], f"{what}.reviewed_pr_url", candidate.repository)
        head = _oid(raw["reviewed_head_sha"], f"{what}.reviewed_head_sha", candidate.object_format)
        if head != candidate.commit:
            _fail(f"{what}.reviewed_head_sha", "differs from the candidate's commit")
        base_ref = _ref(raw["reviewed_base_ref"], f"{what}.reviewed_base_ref")
        merge_base = _oid(
            raw["reviewed_merge_base_sha"],
            f"{what}.reviewed_merge_base_sha",
            candidate.object_format,
        )
        comment = _exact(raw["comment"], _COMMENT_KEYS, f"{what}.comment")
        url = _str(comment["url"], f"{what}.comment.url", MAX_URL_CHARS)
        try:
            ref = parse_comment_url(url)
        except ConfigurationError:
            _fail(f"{what}.comment.url", "is not a GitHub comment URL")
        if ref.canonical != url:
            _fail(f"{what}.comment.url", "must be stored in its canonical form")
        if not ref.on(parse_pr_url(pr_url)):
            _fail(f"{what}.comment.url", "is not on the reviewed pull request")
        if _int(comment["id"], f"{what}.comment.id", 1, MAX_COMMENT_ID) != ref.comment_id:
            _fail(f"{what}.comment.id", "differs from the comment URL's id")
        key_what = f"{what}.comment.marker_key"
        key = _exact(comment["marker_key"], _MARKER_KEY_KEYS, key_what)
        # The round is parsed strictly first: equality alone takes JSON 2.0
        # (or true) for the integer it equals. The other three are compared
        # with strings the certificate's own fields were parsed into, which
        # only an equal string equals.
        _int(key["review_round"], f"{key_what}.review_round", 1, MAX_SEQ)
        expected = {
            "review_round": review_round,
            "reviewed_head_sha": head,
            "reviewed_base_ref": base_ref,
            "reviewed_merge_base_sha": merge_base,
        }
        for name, value in expected.items():
            if key[name] != value:
                _fail(f"{key_what}.{name}", "differs from the certificate's")
    if certificate_digest(raw) != digest:
        _fail(f"{what}.digest", "does not match the certificate: it was edited or corrupted")


def _check_counter(raw: dict, by_seq: Mapping[int, RecordView]) -> None:
    what = "state field 'producer_validation_failure'"
    _exact(raw, _COUNTER_KEYS, what)
    _enum(raw["phase"], f"{what}.phase", FAILURE_PHASES)
    _int(raw["count"], f"{what}.count", 1, MAX_FAILURE_COUNT)
    commit = _str(raw["candidate_commit"], f"{what}.candidate_commit", 64)
    seq = _int(raw["last_record_seq"], f"{what}.last_record_seq", 1, MAX_SEQ)
    record = by_seq.get(seq)
    if record is None:
        _fail(f"{what}.last_record_seq", f"names record seq {seq}, which is not retained")
    if record.outcome not in (Outcome.FAIL, Outcome.TIMEOUT):
        _fail(
            f"{what}.last_record_seq",
            f"names a {record.outcome.value} record, not a FAIL or TIMEOUT",
        )
    candidate = record.candidate
    assert isinstance(candidate, RemoteCandidate)
    if commit != candidate.commit:
        _fail(f"{what}.candidate_commit", "differs from the commit of the record it names")


def pinned_seqs(
    review_evidence: Mapping[str, object],
    review_certificate: Mapping[str, object],
    producer_validation_failure: Mapping[str, object],
) -> set[int]:
    """The seqs no eviction may remove (D11.4)."""
    pinned: set[int] = set()
    for holder in (review_evidence, review_certificate):
        pairs = holder.get("records")
        for pair in pairs if isinstance(pairs, list) else ():
            if isinstance(pair, dict) and isinstance(pair.get("seq"), int):
                pinned.add(pair["seq"])
    seq = producer_validation_failure.get("last_record_seq")
    if isinstance(seq, int):
        pinned.add(seq)
    return pinned


# -- writers ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    """``SandboxFacts``' rule for an over-long display fact: cut, with an ellipsis."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def enforcement_facts(facts: SandboxFacts | None) -> dict:
    """D8.2: what of the executor's facts a record copies; a launch with none is unenforced."""
    if facts is None:
        return {
            "enforced": False,
            "backend": "",
            "backend_version": "",
            "network": "",
            "teardown": "",
        }
    return {
        "enforced": bool(facts.enforced),
        "backend": _clip(redact(facts.backend), MAX_BACKEND_CHARS),
        "backend_version": _clip(redact(facts.backend_version), MAX_BACKEND_CHARS),
        "network": facts.network if facts.network in NETWORKS else "",
        "teardown": _clip(redact(facts.teardown), MAX_TEARDOWN_CHARS),
    }


def _escape(char: str) -> str:
    if char in "\n\t" or not _UNESCAPED_RE.match(char):
        return char
    return char.encode("unicode_escape").decode("ascii")


def bounded_tail(text: str, captured_truncated: bool) -> tuple[str, bool]:
    """D10.2 on one decoded stream: redact it whole, escape, cut, say whether it was cut.

    ``text`` is the executor's capture, already decoded with replacement
    characters under its byte caps. The cut keeps the longest suffix of at
    most :data:`MAX_TAIL_CHARS` escaped characters, so it never splits an
    escape. The flag is True when the capture omitted bytes or the cut did.
    """
    redacted = redact(text)
    pieces: list[str] = []
    size = 0
    # Each character escapes to at least one, so the last MAX_TAIL_CHARS
    # characters always hold the suffix.
    for char in reversed(redacted[-MAX_TAIL_CHARS:]):
        piece = _escape(char)
        if size + len(piece) > MAX_TAIL_CHARS:
            break
        pieces.append(piece)
        size += len(piece)
    kept = len(pieces)
    return "".join(reversed(pieces)), captured_truncated or kept < len(redacted)


def outcome_of(result: ExecutionResult) -> Outcome:
    """D10.4: a timeout is TIMEOUT, exit 0 is PASS, anything else FAIL."""
    if result.timed_out:
        return Outcome.TIMEOUT
    return Outcome.PASS if result.exit_code == 0 else Outcome.FAIL


def observed_record(
    *,
    seq: int,
    candidate: Candidate,
    definition: Definition,
    policy: Policy,
    result: ExecutionResult,
    run_log: str,
    end_fingerprint: str = "",
) -> dict:
    """The record of one launched definition (D10.1), digest included."""
    outcome = outcome_of(result)
    stdout_tail, stdout_cut = bounded_tail(result.stdout or "", result.stdout_truncated)
    stderr_tail, stderr_cut = bounded_tail(result.stderr or "", result.stderr_truncated)
    record: dict = {
        "schema": SCHEMA,
        "seq": seq,
        "candidate": candidate.to_dict(),
        "definition": definition.stored(),
        "policy": policy.stored(),
        "outcome": outcome.value,
        "run_log": run_log,
        "enforcement": enforcement_facts(result.sandbox),
        "exit_code": None if outcome is Outcome.TIMEOUT else result.exit_code,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "leftovers": {
            "descendants_killed": bool(result.descendants_killed),
            "group_survived_kill": bool(result.group_survived_kill),
            "orphans_killed": bool(result.orphans_killed),
            "orphan_survived_kill": bool(result.orphan_survived_kill),
            "orphans_unchecked": bool(result.orphans_unchecked),
        },
        "output": {
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "stdout_bytes": min(max(result.stdout_bytes, 0), MAX_BYTE_COUNT),
            "stderr_bytes": min(max(result.stderr_bytes, 0), MAX_BYTE_COUNT),
            "stdout_tail_truncated": stdout_cut,
            "stderr_tail_truncated": stderr_cut,
        },
    }
    if isinstance(candidate, LocalCandidate):
        record["start_fingerprint"] = candidate.fingerprint
        record["end_fingerprint"] = end_fingerprint
    record["digest"] = record_digest(record)
    # A writer never produces a value the loader would refuse (D10.3): a
    # runner that reported a malformed time, say, fails here, before the save.
    parse_record(record, "the new validation record")
    return record


def attempt_marker(
    *,
    candidate: Candidate,
    definition: Definition,
    policy: Policy,
    run_log: str,
    marked_at: str,
) -> dict:
    """The marker written immediately before a launch (D11.2)."""
    marker: dict = {
        "candidate": candidate.to_dict(),
        "definition": definition.stored(),
        "policy": policy.stored(),
        "run_log": run_log,
        "marked_at": marked_at,
    }
    if isinstance(candidate, LocalCandidate):
        marker["start_fingerprint"] = candidate.fingerprint
    parse_marker(marker, "the new validation attempt marker")
    return marker


def interrupted_record(*, seq: int, marker: Mapping[str, object], recovered_at: str) -> dict:
    """D10.7: built from the marker alone; claims no launch."""
    record: dict = {
        "schema": SCHEMA,
        "seq": seq,
        "candidate": marker["candidate"],
        "definition": marker["definition"],
        "policy": marker["policy"],
        "outcome": Outcome.INTERRUPTED.value,
        "run_log": marker["run_log"],
        "marked_at": marker["marked_at"],
        "recovered_at": recovered_at,
    }
    if "start_fingerprint" in marker:
        record["start_fingerprint"] = marker["start_fingerprint"]
    record["digest"] = record_digest(record)
    parse_record(record, "the new interrupted record")
    return record


def next_seq(records: Sequence[Mapping[str, object]]) -> int:
    """The seq the next record takes: one above the highest retained."""
    highest = 0
    for record in records:
        seq = record.get("seq")
        if isinstance(seq, int) and seq > highest:
            highest = seq
    return highest + 1


def appended(records: Sequence[dict], record: dict, pinned: set[int]) -> list[dict]:
    """``records`` with ``record`` appended, evicted oldest first to the bound (D11.4)."""
    out = [*records, record]
    while len(out) > MAX_RECORDS:
        for i, old in enumerate(out):
            if old.get("seq") not in pinned:
                del out[i]
                break
        else:  # pragma: no cover - pinned records are at most 2 x 16 + 1
            break
    return out


# -- readers ---------------------------------------------------------------------------


def applicable(record: RecordView, candidate: Candidate, definition: str, policy: Policy) -> bool:
    """D8.3: an observed record for this candidate, definition and policy.

    Equal candidate identity (never provenance), definition digest and
    policy digest; under ``required`` (or ``preferred``) the record must
    also show the launch enforced. An ``INTERRUPTED`` record never applies.
    """
    return (
        record.is_observed
        and record.candidate.identity == candidate.identity
        and record.definition_digest == definition
        and record.policy_digest == policy.digest
        and (record.enforced or not policy.requires_enforcement)
    )


def interruptions(
    records: Sequence[RecordView],
    identity: tuple[str, ...],
    definition: str,
    policy: str,
    watermark: int | None,
) -> int:
    """D10.7: the retained INTERRUPTED records of one key above the watermark."""
    floor = watermark or 0
    return sum(
        1
        for r in records
        if r.outcome is Outcome.INTERRUPTED
        and r.seq > floor
        and r.candidate.identity == identity
        and r.definition_digest == definition
        and r.policy_digest == policy
    )


def recover_attempt(
    records_raw: Sequence[dict],
    marker_raw: Mapping[str, object],
    *,
    watermark: int | None,
    pinned: set[int],
    recovered_at: str,
) -> list[dict]:
    """D10.7, D11.3: the records the resume path writes for a leftover marker.

    The caller clears the marker in the same save. A record that matches the
    marker (its candidate identity, definition, policy and run log) means
    the marker is stale, a hand edit: the record wins, and the records come
    back unchanged. Otherwise an ``INTERRUPTED`` record is appended; more
    than :data:`MAX_INTERRUPTIONS` of them for the key (those at or below
    the watermark not counted) raise :class:`ValidationInterruptedError`
    carrying the records, which the caller saves in its ``BLOCKED`` write.
    """
    marker = parse_marker(marker_raw)
    for view in (parse_record(r) for r in records_raw):
        if (
            view.candidate.identity == marker.candidate.identity
            and view.definition_digest == marker.definition_digest
            and view.policy_digest == marker.policy_digest
            and view.run_log == marker.run_log
        ):
            return list(records_raw)
    record = interrupted_record(
        seq=next_seq(records_raw), marker=marker_raw, recovered_at=recovered_at
    )
    records = appended(list(records_raw), record, pinned)
    count = interruptions(
        [parse_record(r) for r in records],
        marker.candidate.identity,
        marker.definition_digest,
        marker.policy_digest,
        watermark,
    )
    if count > MAX_INTERRUPTIONS:
        raise ValidationInterruptedError(
            f"candidate validation of {marker.candidate.describe()} was interrupted {count} "
            f"times for definition [{marker.index}] {' '.join(marker.argv)!r} under policy "
            f"{marker.policy_digest[:16]}..., over the bound of {MAX_INTERRUPTIONS}, so the "
            "controller will not run it again unattended. Find what ended the validator (a "
            "crash, an interrupt, a kill), then resume",
            records,
        )
    return records


def record_summary(view: RecordView) -> str:
    """One display line for ``status``: redacted, bounded by the stored bounds."""
    argv = redact(" ".join(view.argv))
    if len(argv) > 200:
        argv = argv[:199] + "…"
    if view.outcome is Outcome.INTERRUPTED:
        detail = "interrupted (no launch claimed)"
    elif view.outcome is Outcome.TIMEOUT:
        detail = "timed out"
    else:
        detail = f"exit {view.exit_code}"
    enforced = ""
    if view.is_observed:
        enforced = ", sandbox enforced" if view.enforced else ", unsandboxed"
    return f"#{view.seq} [{view.index}] {argv!r}: {view.outcome.value} ({detail}{enforced})"


@dataclass(frozen=True)
class ValidationRun:
    """What one call of the REMOTE entry point observed, in set order."""

    candidate: Candidate
    validation_set_digest: str
    policy: Policy
    records: tuple[dict, ...]
    definitions: int

    @property
    def passed(self) -> bool:
        """Every definition of the set ran and passed (D10.6 stops at a non-PASS)."""
        return len(self.records) == self.definitions and all(
            r["outcome"] == Outcome.PASS.value for r in self.records
        )
