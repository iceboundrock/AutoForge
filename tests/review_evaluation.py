"""Review quality over adjudicated labels: the scorer of the REV-R0 contract (#355).

No test: ``tests/test_review_evaluation.py`` holds it to
``docs/review-evaluation-protocol.md``, the contract it implements. It reads the
files of ``tests/fixtures/review_evaluation/``:

- ``preregistration.json``: the taxonomy, the metric parameters, the decision
  rule and the baseline facts the code is held to;
- ``corpus.json``: the cases, each a pinned base and candidate with its
  ground-truth defects and the oracles that show them;
- ``fixture_runs.json``: scripted runs of the current review over the corpus,
  each with the labels an adjudicator gave its findings and fixes.

::

    uv run python -m tests.review_evaluation check
    uv run python -m tests.review_evaluation verify
    uv run python -m tests.review_evaluation score [--out FILE]
    uv run python -m tests.review_evaluation decide --baseline ARM --treatment ARM

``check`` refuses a file the contract does not allow, ``verify`` replays the
corpus (it builds every case's commits, whether or not a run holds the case, and
every run's own, in temporary repositories at a fixed date, holds each pinned SHA
to what it builds and each commit's tree to the files that built it, and runs
every oracle where the ground truth says it must fail or pass, on that commit's
tree as the controller's byte-exact materializer writes and proves it),
``score`` prints the report, and ``decide`` applies the pre-registered decision
to two arms.

It launches no agent, reads no credential and writes to no GitHub, and git runs
with every transport refused. An oracle is Python from the corpus or run file,
run against the case's own code: ``verify`` runs it as the caller, with the
caller's files and network, in a ``python -I`` that sees only the locale, which
keeps the host's variables and packages out and enforces no boundary. So it
runs the oracles of the registered files alone, the bytes of this checkout's
``corpus.json`` and ``fixture_runs.json``, unless the caller passes
``--trust-oracles``; ``--pins-only`` replays the pins of any files and runs
nothing they hold.

A label comes from an adjudicator's record or an oracle, never from what an
agent said of its own work: the reviewer's classification, its prose, its clean
reply and the fixer's resolution kinds are reported as claims and read by no
metric. Whether the controller certified a round's candidate is its own record
of the certificate it issued for that round and revision; which launches a run
made is its own record of the ordinals it spent, from the run's creation to the
stop the evaluation ended it at. Every comparison with a registered threshold
is made in exact arithmetic on unrounded values; a report rounds for display.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from autoforge.errors import AutoForgeError
from autoforge.executor import execute, select_environment
from autoforge.git_transport import NO_AUTO_MAINTENANCE
from autoforge.loop_guard import RESULTS
from autoforge.materialize import materialize, read_object_format
from autoforge.profiles import review_profile_name
from autoforge.result_parser import FIX_RESOLUTIONS, ReviewResult

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "review_evaluation"
PREREGISTRATION = FIXTURES / "preregistration.json"
CORPUS = FIXTURES / "corpus.json"
FIXTURE_RUNS = FIXTURES / "fixture_runs.json"
FIXTURE_REPORT = FIXTURES / "fixture_report.json"

PREREGISTRATION_SCHEMA = "autoforge.review-evaluation.preregistration/1"
CORPUS_SCHEMA = "autoforge.review-evaluation.corpus/1"
RUNS_SCHEMA = "autoforge.review-evaluation.runs/1"
REPORT_SCHEMA = "autoforge.review-evaluation.report/1"

CASE_KINDS = ("defect", "clean_negative")
# The controller's own record of a round, never the reviewer's reply: the result
# ``review_history`` keeps for it, or ``unrecorded`` when no record of it was found.
DISPOSITIONS = (*RESULTS, "unrecorded")
# The validation set the controller presented to the round: every record PASS, the
# deliberately empty set (no validation command configured), a record not PASS,
# or ``unrecorded`` when no record of it was found.
VALIDATION_OUTCOMES = ("passed", "empty_set", "failed", "unrecorded")
# The controller's record of a round's certificate: one issued for this round and
# revision, none issued for it, or ``unrecorded`` when no record of it was found.
CERTIFICATE_RECORDS = ("issued", "not_issued", "unrecorded")
# Whether the controller certified a round's candidate (section 3).
CERTIFIED = "certified"
NOT_CERTIFIED = "not_certified"
CERTIFICATION_UNKNOWN = "unknown"
LAUNCH_PHASES = ("ANALYZE_EXECUTE", "REVIEW", "FIX", "REPLAN_REEXECUTE")
# Where a run's launch ordinals start: the controller created the run for the case's
# issue, at ``launch_seq`` 0, so its first launch spent ordinal 1; or no record says.
WINDOW_STARTS = ("run_created", "unrecorded")
# Where they end: the stop phase the evaluation ended the run at, after which it
# launched nothing more for the case's issue; or no record says. DONE is none of
# them: it follows a merge, and an evaluation merges nothing.
WINDOW_ENDS = ("READY_FOR_MERGE", "BLOCKED", "FAILED", "unrecorded")
# The metrics a margin or the primary may name, in the order a report shows them.
RATE_METRICS = (
    "weighted_recall",
    "precision",
    "clean_negative_fpr",
    "harmful_fix_rate",
    "duplicate_ratio",
)
# The direction every decision criterion is registered in: a preregistration that
# turns one round is refused, since it would invert what the criterion protects.
CRITERION_DIRECTIONS = {
    "weighted_recall": "higher_is_better",
    "precision": "higher_is_better",
    "clean_negative_fpr": "lower_is_better",
    "harmful_fix_rate": "lower_is_better",
    "duplicate_ratio": "lower_is_better",
    "cost_per_validated_success": "lower_is_better",
}
NOT_EVIDENCE = (
    "a fixture run is scripted: its numbers check the scorer and say nothing about any "
    "reviewer, model or configuration"
)
# How long one oracle may run before verify refuses the corpus.
ORACLE_TIMEOUT_SECONDS = 60
# git and the oracle interpreter see only these of the host's variables.
_GIT_ENV_ALLOWLIST = ("PATH", "LANG", "LANGUAGE", "LC_*", "TMPDIR")
_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_LAZY_FETCH": "1",
}
_GIT_SWITCHES = (
    "--no-replace-objects",
    "-c",
    "protocol.allow=never",
    "-c",
    "credential.helper=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "commit.gpgsign=false",
    *NO_AUTO_MAINTENANCE,
)
_HEX = frozenset("0123456789abcdef")


class ReviewEvaluationError(Exception):
    """A file the contract refuses, or a replay that does not hold."""


# -- reading ------------------------------------------------------------------------------


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def file_digest(path: Path) -> str:
    """SHA-256 of the file's bytes: what the protocol's amendments section registers."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _obj(value: object, where: str, required: Sequence[str], optional: Sequence[str] = ()) -> dict:
    if not isinstance(value, dict):
        raise ReviewEvaluationError(f"{where} must be an object")
    unknown = sorted(set(value) - set(required) - set(optional))
    if unknown:
        raise ReviewEvaluationError(f"{where} has unregistered field(s) {unknown}")
    missing = [key for key in required if key not in value]
    if missing:
        raise ReviewEvaluationError(f"{where} is missing {missing}")
    return value


def _str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewEvaluationError(f"{where} must be a non-empty string")
    return value


def _int(value: object, where: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ReviewEvaluationError(f"{where} must be an integer >= {minimum}")
    return value


def _number(value: object, where: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool) or not value >= 0:
        raise ReviewEvaluationError(f"{where} must be a non-negative number or null")
    if not math.isfinite(value):
        raise ReviewEvaluationError(f"{where} must be finite")
    return float(value)


def _bounded(
    value: object, where: str, low: float, high: float, *, low_open: bool = False
) -> float:
    """A finite number in ``[low, high]``, or ``(low, high]`` with ``low_open``."""
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not (low < value if low_open else low <= value)
        or not value <= high
    ):
        span = f"{'(' if low_open else '['}{low}, {high}]"
        raise ReviewEvaluationError(f"{where} must be a number in {span}, not {value!r}")
    return float(value)


def exact(value: float) -> Fraction:
    """``value`` as the decimal a file wrote it as: what a threshold is compared with.

    A JSON number is read as the nearest binary float, whose shortest repr is the
    decimal written, for any number of up to 15 significant digits.
    """
    return Fraction(repr(value))


def _one_of(value: object, choices: Sequence[str], where: str) -> str:
    if value not in choices:
        raise ReviewEvaluationError(f"{where} must be one of {list(choices)}, not {value!r}")
    assert isinstance(value, str)
    return value


def _instant(value: object, where: str) -> datetime:
    text = _str(value, where)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ReviewEvaluationError(f"{where} is no ISO 8601 instant: {text!r}") from None
    if moment.tzinfo is None:
        raise ReviewEvaluationError(f"{where} must carry its UTC offset")
    return moment


def _sha(value: object, where: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or not set(value) <= _HEX:
        raise ReviewEvaluationError(f"{where} must be a full lower-case 40-hex SHA")
    return value


def _names(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ReviewEvaluationError(f"{where} must be a list of names")
    if len(set(value)) != len(value):
        raise ReviewEvaluationError(f"{where} names one entry twice")
    return tuple(value)


def _files(value: object, where: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ReviewEvaluationError(f"{where} must be a non-empty object of path: text")
    for path, text in value.items():
        parts = path.split("/")
        if (
            not path
            or path.startswith("/")
            or any(part in ("", ".", "..", ".git") for part in parts)
            or not isinstance(text, str)
        ):
            raise ReviewEvaluationError(f"{where} holds an unusable entry {path!r}")
    return dict(value)


# -- the checked model --------------------------------------------------------------------


@dataclass(frozen=True)
class Commit:
    name: str
    parent: str | None
    files: Mapping[str, str]
    sha: str
    fixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Defect:
    id: str
    severity: str
    weight: int
    defect_class: str
    introduced_at: str
    fixed_at: tuple[str, ...]
    discovered_at: datetime
    discovered_by: str
    registered: bool
    oracle: str | None
    passes_at: tuple[str, ...]


@dataclass(frozen=True)
class Case:
    id: str
    kind: str
    commits: Mapping[str, Commit]
    candidate: str
    defects: Mapping[str, Defect]
    oracles: Mapping[str, str]


@dataclass(frozen=True)
class Finding:
    id: str
    classification: str
    verdict: str
    defect: str | None


@dataclass(frozen=True)
class Review:
    round: int
    commit: str
    # The reviewer's reply: a claim, read only through what the controller decided.
    clean: bool
    disposition: str
    validation: str
    # Whether the validation presented was the reviewed candidate's: None when no
    # validation was recorded.
    validated_at_candidate: bool | None
    # The controller's record of this round's certificate (CERTIFICATE_RECORDS).
    certificate: str
    findings: tuple[Finding, ...]

    @property
    def certification(self) -> str:
        """Whether the controller certified this round's candidate (section 3).

        Certified only on the controller's record of a certificate issued for this
        round and this revision (the check holds it to both). A record that it issued
        none rules one out, and so does what the controller never certifies: a reply
        that needs a fix, a round it sent to a fix or found stale, a failed validation
        and another candidate's validation. Otherwise no record of the certificate was
        found and it is unknown: a clean reply, a ``clean`` disposition and a passing
        validation of the candidate are what issuance needs, never proof that it
        happened, since the controller records the round before it issues.
        """
        if self.certificate == "issued":
            return CERTIFIED
        if (
            self.certificate == "not_issued"
            or not self.clean
            or self.disposition in ("needs_fix", "stale")
            or self.validation == "failed"
            or self.validated_at_candidate is False
        ):
            return NOT_CERTIFIED
        return CERTIFICATION_UNKNOWN


@dataclass(frozen=True)
class Fix:
    round: int
    commit: str | None
    resolutions: Mapping[str, str]
    verdict: str | None


@dataclass(frozen=True)
class Launch:
    phase: str
    round: int | None
    # The controller's launch ordinal the launch spent (``launch_seq``).
    ordinal: int
    cost_usd: float | None
    wall_clock_seconds: float | None
    context_tokens: float | None
    provenance: str


@dataclass(frozen=True)
class LaunchWindow:
    """The controller's record of the launch ordinals a run spent (``launch_seq``).

    ``start`` and ``end`` are how each boundary is known (WINDOW_STARTS,
    WINDOW_ENDS); ``last`` is the state's ``launch_seq`` at that end, the last
    ordinal the run spent, and ``None`` when the end is unrecorded. The state's
    ``launch_seq`` is a high-water mark: it gives the end of a run's ordinals, and
    only the run's creation gives their start.
    """

    start: str
    end: str
    last: int | None


def _ancestors(commits: Mapping[str, Commit], name: str) -> frozenset[str]:
    seen: list[str] = []
    current: str | None = name
    while current is not None:
        seen.append(current)
        current = commits[current].parent
    return frozenset(seen)


def _fixed_at(commits: Mapping[str, Commit], defect: Defect) -> tuple[str, ...]:
    """Where ``defect`` stops: the case's commits that fix it and any run commit that does."""
    by_run = tuple(c.name for c in commits.values() if defect.id in c.fixes)
    return defect.fixed_at + by_run


def _present(commits: Mapping[str, Commit], defect: Defect, commit: str) -> bool:
    line = _ancestors(commits, commit)
    return defect.introduced_at in line and not any(f in line for f in _fixed_at(commits, defect))


@dataclass(frozen=True)
class Run:
    id: str
    arm: str
    case: Case
    provenance: str
    commits: Mapping[str, Commit]
    defects: Mapping[str, Defect]
    reviews: tuple[Review, ...]
    fixes: tuple[Fix, ...]
    launches: tuple[Launch, ...]
    # The controller's record of the launch ordinals it spent on the run, read from
    # its state and run log, not from the launches; None when it was not found.
    launch_window: LaunchWindow | None
    stopped: str
    excluded: str
    oracles: Mapping[str, str]

    def present(self, defect: Defect, commit: str) -> bool:
        return _present(self.commits, defect, commit)

    def belongs_to(self, defect: str) -> str:
        """``case`` for a defect of the case's commits, ``run`` for one this run introduced."""
        return "case" if defect in self.case.defects else "run"

    def went_on(self) -> bool | None:
        """Whether the run went on past its record: ``None`` when that is unknown.

        True when the controller spent an ordinal after the record's last launch: a
        launch the record does not hold followed its last round, so that round is not
        where the run's outcome was decided. Unknown when the record's last round is
        stale and no recorded end shows the run stopped there: the controller fixes no
        stale round and reviews the moved revision next, so a stale round ends what a
        record can hold, never the run by itself.
        """
        w = self.launch_window
        held = max((launch.ordinal for launch in self.launches), default=0)
        if w is not None and w.last is not None and w.last > held:
            return True
        if self.reviews[-1].disposition == "stale" and (w is None or w.last is None):
            return None
        return False

    def certification(self) -> str:
        """Whether the controller certified the candidate the run ended on (section 3).

        The final round's, when the run ended on it; unknown when the run went on
        past its record, or may have.
        """
        if self.went_on() is not False:
            return CERTIFICATION_UNKNOWN
        return self.reviews[-1].certification

    def missing_launches(self) -> tuple[str, ...]:
        """The launches a complete record of this run holds and its own does not.

        Which launches the run made is the controller's record, not the launch list:
        the controller spends one launch ordinal (``launch_seq``) in the save before
        every launch, a relaunch and a failed attempt included, so the run made one
        launch for each ordinal from the first it spent to the last. The state's
        ``launch_seq`` gives only the last; the first is 1 when the controller
        created the run for the case's issue, which then held ``launch_seq`` 0, and
        unknown otherwise, so a record that cannot show the run's creation is
        incomplete whatever its launches cover. So is one whose end is not a stop
        the evaluation ended the run at. One without a launch of an ordinal of the
        window lost a launch, whatever phase and round the others cover, a leading
        or a trailing one included. It also holds an ANALYZE_EXECUTE launch, a REVIEW
        launch of each round and a FIX launch of each fix. A record short of any of
        these is incomplete, so no sum over its launches is the run's.
        """
        missing: list[str] = []
        ordinals = sorted(launch.ordinal for launch in self.launches)
        w = self.launch_window
        if w is None:
            missing.append("the controller's record of its launch window")
        else:
            if w.start != "run_created":
                missing.append(
                    "the controller's record of where its launches start: no record shows "
                    "it created the run for this case"
                )
            if w.last is None:
                missing.append(
                    "the controller's record of where its launches end: the run was not "
                    "found at a stop the evaluation ended it at"
                )
            first = 1 if w.start == "run_created" else (ordinals[0] if ordinals else 1)
            last = w.last if w.last is not None else (ordinals[-1] if ordinals else 0)
            expected = first
            for ordinal in [*ordinals, last + 1]:
                if ordinal - 1 == expected:
                    missing.append(f"the launch of ordinal {expected}")
                elif ordinal > expected:
                    missing.append(f"the launches of ordinals {expected} to {ordinal - 1}")
                expected = ordinal + 1
        held = {(launch.phase, launch.round) for launch in self.launches}
        if not any(launch.phase == "ANALYZE_EXECUTE" for launch in self.launches):
            missing.append("ANALYZE_EXECUTE")
        missing += [
            f"REVIEW of round {r.round}" for r in self.reviews if ("REVIEW", r.round) not in held
        ]
        missing += [f"FIX of round {f.round}" for f in self.fixes if ("FIX", f.round) not in held]
        return tuple(missing)


@dataclass(frozen=True)
class Evaluation:
    preregistration: dict
    cases: Mapping[str, Case]
    runs: tuple[Run, ...]
    commit_date: str
    digests: Mapping[str, str]


def _registered_names(p: Mapping, key: str, expected: Sequence[str]) -> None:
    """The registered vocabulary ``key`` the scorer's code reads by name, exactly."""
    if p[key] != list(expected):
        raise ReviewEvaluationError(
            f"preregistration {key} must be {list(expected)}: the scorer reads these names"
        )


def check_preregistration(prereg: object) -> dict:
    """The preregistration, every value the scorer reads present, typed and in range.

    The decision's criteria are all required: the primary and its effect, a margin
    for every metric of section 6 but the primary and for the cost per validated
    success, each in its registered direction, the hard gate, the minimum sample,
    the excluded share and the label-uncertainty limit. A file without one is
    refused, never read as a criterion waived: only an amendment changes them, and
    it changes them by registering other values, never by leaving one out.
    """
    p = _obj(
        prereg,
        "preregistration",
        (
            "schema",
            "issue",
            "registered",
            "label_verdicts",
            "severity_weights",
            "success_blocking_severities",
            "defect_classes",
            "discovered_by",
            "evidence_kinds",
            "adjudicator_roles",
            "refused_adjudicator_roles",
            "refused_label_fields",
            "fix_verdicts",
            "usage_provenance",
            "run_provenance",
            "max_label_uncertainty",
            "uncertainty",
            "minimum_sample",
            "decision",
            "baseline",
        ),
    )
    if p["schema"] != PREREGISTRATION_SCHEMA:
        raise ReviewEvaluationError(f"preregistration schema must be {PREREGISTRATION_SCHEMA!r}")
    _registered_names(p, "label_verdicts", ("confirmed", "false", "unknown"))
    _registered_names(p, "fix_verdicts", ("harmful", "not_harmful", "unknown"))
    _registered_names(p, "evidence_kinds", ("oracle", "adjudication"))
    _registered_names(p, "usage_provenance", ("measured", "estimated", "synthetic", "unknown"))
    _registered_names(p, "run_provenance", ("fixture", "real"))
    weights = p["severity_weights"]
    if not isinstance(weights, dict) or "critical" not in weights:
        raise ReviewEvaluationError(
            "preregistration severity_weights must be an object weighing `critical`, the "
            "severity the hard gate reads"
        )
    for severity, weight in weights.items():
        _int(weight, f"preregistration severity_weights.{severity}", 1)
    for key in (
        "success_blocking_severities",
        "defect_classes",
        "discovered_by",
        "adjudicator_roles",
        "refused_adjudicator_roles",
        "refused_label_fields",
    ):
        if not _names(p[key], f"preregistration {key}"):
            raise ReviewEvaluationError(f"preregistration {key} must name at least one entry")
    if not set(p["success_blocking_severities"]) <= set(weights):
        raise ReviewEvaluationError("a success-blocking severity is not a registered severity")
    if set(p["adjudicator_roles"]) & set(p["refused_adjudicator_roles"]):
        raise ReviewEvaluationError("an adjudicator role is both allowed and refused")
    _bounded(p["max_label_uncertainty"], "preregistration max_label_uncertainty", 0, 1)
    u = _obj(
        p["uncertainty"],
        "preregistration uncertainty",
        ("method", "resamples", "seed", "confidence"),
    )
    if u["method"] != "case_bootstrap_percentile":
        raise ReviewEvaluationError("preregistration uncertainty.method is not one this scorer has")
    _int(u["resamples"], "preregistration uncertainty.resamples", 1)
    _int(u["seed"], "preregistration uncertainty.seed")
    if not isinstance(u["confidence"], float) or not 0 < u["confidence"] < 1:
        raise ReviewEvaluationError("preregistration uncertainty.confidence must be in (0, 1)")
    minimum = _obj(
        p["minimum_sample"],
        "preregistration minimum_sample",
        ("defect_cases", "clean_negative_cases"),
    )
    _int(minimum["defect_cases"], "preregistration minimum_sample.defect_cases", 1)
    _int(minimum["clean_negative_cases"], "preregistration minimum_sample.clean_negative_cases", 1)
    d = _obj(
        p["decision"],
        "preregistration decision",
        ("primary", "margins", "hard_gates", "max_excluded_case_share"),
    )
    primary = _obj(d["primary"], "preregistration decision.primary", ("metric", "min_difference"))
    metric = _one_of(primary["metric"], RATE_METRICS, "preregistration decision.primary.metric")
    if CRITERION_DIRECTIONS[metric] != "higher_is_better":
        raise ReviewEvaluationError(
            f"preregistration decision.primary.metric {metric!r} is lower-is-better: the "
            "primary is a rise of a metric that is better higher"
        )
    _bounded(
        primary["min_difference"],
        "preregistration decision.primary.min_difference",
        0,
        1,
        low_open=True,
    )
    required = [m for m in (*RATE_METRICS, "cost_per_validated_success") if m != metric]
    margins = _obj(d["margins"], "preregistration decision.margins", required)
    for name in required:
        margin = _obj(margins[name], f"preregistration margin {name}", ("direction", "margin"))
        if margin["direction"] != CRITERION_DIRECTIONS[name]:
            raise ReviewEvaluationError(
                f"preregistration margin {name} must be {CRITERION_DIRECTIONS[name]}, "
                f"not {margin['direction']!r}"
            )
        high = math.inf if name == "cost_per_validated_success" else 1
        _bounded(margin["margin"], f"preregistration margin {name}.margin", 0, high)
    if d["hard_gates"] != ["no_new_escaped_critical_defect"]:
        raise ReviewEvaluationError("preregistration hard_gates is not one this scorer has")
    _bounded(d["max_excluded_case_share"], "preregistration decision.max_excluded_case_share", 0, 1)
    if not isinstance(p["baseline"], dict):
        raise ReviewEvaluationError("preregistration baseline must be an object")
    return p


def _adjudicator(value: object, where: str, prereg: Mapping) -> str:
    a = _obj(value, where, ("id", "role"))
    _str(a["id"], f"{where}.id")
    if a["role"] in prereg["refused_adjudicator_roles"]:
        raise ReviewEvaluationError(
            f"{where} is a {a['role']}: an agent's word on its own work is no label "
            "(self-reported correctness is refused)"
        )
    _one_of(a["role"], prereg["adjudicator_roles"], f"{where}.role")
    return str(a["id"])


def _commits(
    raw: object, where: str, known: Mapping[str, Commit], *, run: bool
) -> dict[str, Commit]:
    if not isinstance(raw, list) or (not run and not raw):
        raise ReviewEvaluationError(f"{where} must be a list of commits")
    out: dict[str, Commit] = {}
    for i, item in enumerate(raw):
        at = f"{where}[{i}]"
        c = _obj(item, at, ("name", "parent", "files", "sha"), ("fixes",) if run else ())
        name = _str(c["name"], f"{at}.name")
        if name in known or name in out:
            raise ReviewEvaluationError(f"{at} names commit {name!r} twice")
        parent = c["parent"]
        if parent is not None and parent not in known and parent not in out:
            raise ReviewEvaluationError(f"{at} has parent {parent!r}, which no earlier commit is")
        if parent is None and (run or out):
            raise ReviewEvaluationError(f"{at}: only a case's first commit has no parent")
        out[name] = Commit(
            name=name,
            parent=parent,
            files=_files(c["files"], f"{at}.files"),
            sha=_sha(c["sha"], f"{at}.sha"),
            fixes=_names(c.get("fixes", []), f"{at}.fixes"),
        )
    return out


def _defects(
    raw: object,
    where: str,
    prereg: Mapping,
    commits: Mapping[str, Commit],
    introduced_in: Iterable[str],
    oracles: Mapping[str, str],
    cutoff: datetime,
) -> dict[str, Defect]:
    if not isinstance(raw, list):
        raise ReviewEvaluationError(f"{where} must be a list")
    allowed = set(introduced_in)
    out: dict[str, Defect] = {}
    for i, item in enumerate(raw):
        at = f"{where}[{i}]"
        d = _obj(
            item,
            at,
            (
                "id",
                "defect_class",
                "severity",
                "location",
                "introduced_at",
                "fixed_at",
                "discovered_at",
                "discovered_by",
                "summary",
                "evidence",
                "adjudicator",
            ),
        )
        did = _str(d["id"], f"{at}.id")
        if did in out:
            raise ReviewEvaluationError(f"{at} names defect {did!r} twice")
        _one_of(d["defect_class"], prereg["defect_classes"], f"{at}.defect_class")
        severity = _one_of(d["severity"], list(prereg["severity_weights"]), f"{at}.severity")
        _str(d["location"], f"{at}.location")
        _str(d["summary"], f"{at}.summary")
        introduced = d["introduced_at"]
        if introduced not in allowed:
            raise ReviewEvaluationError(
                f"{at}.introduced_at {introduced!r} is no commit it may be introduced at: a "
                "case's defect at a case commit after the base, a run's at a commit of the run"
            )
        fixed = _names(d["fixed_at"], f"{at}.fixed_at")
        for name in fixed:
            if name not in commits:
                raise ReviewEvaluationError(f"{at}.fixed_at names {name!r}, which no commit is")
        _one_of(d["discovered_by"], prereg["discovered_by"], f"{at}.discovered_by")
        discovered = _instant(d["discovered_at"], f"{at}.discovered_at")
        _adjudicator(d["adjudicator"], f"{at}.adjudicator", prereg)
        ev = d["evidence"]
        kind = _one_of(
            ev.get("kind") if isinstance(ev, dict) else None,
            prereg["evidence_kinds"],
            f"{at}.evidence.kind",
        )
        oracle: str | None = None
        passes_at: tuple[str, ...] = ()
        if kind == "oracle":
            e = _obj(ev, f"{at}.evidence", ("kind", "oracle", "passes_at"))
            oracle = _str(e["oracle"], f"{at}.evidence.oracle")
            if oracle not in oracles:
                raise ReviewEvaluationError(
                    f"{at}.evidence names oracle {oracle!r}, which is not held"
                )
            passes_at = _names(e["passes_at"], f"{at}.evidence.passes_at")
            if not passes_at and not fixed:
                raise ReviewEvaluationError(
                    f"{at}: an oracle that passes nowhere shows nothing; name a commit it passes at"
                )
            for name in passes_at:
                if name not in commits:
                    raise ReviewEvaluationError(
                        f"{at}.evidence.passes_at names {name!r}, no commit"
                    )
        else:
            e = _obj(ev, f"{at}.evidence", ("kind", "rationale"))
            _str(e["rationale"], f"{at}.evidence.rationale")
        out[did] = Defect(
            id=did,
            severity=severity,
            weight=int(prereg["severity_weights"][severity]),
            defect_class=d["defect_class"],
            introduced_at=introduced,
            fixed_at=fixed,
            discovered_at=discovered,
            discovered_by=d["discovered_by"],
            registered=discovered <= cutoff,
            oracle=oracle,
            passes_at=passes_at,
        )
    return out


def _check_ground_truth(
    where: str, commits: Mapping[str, Commit], defects: Mapping[str, Defect]
) -> None:
    """Every fix of a defect follows its introduction, and every oracle pass is consistent.

    A defect's ``fixed_at`` and a run commit's ``fixes`` each name a commit that fixes
    it: one that strictly descends from the commit it was introduced at and whose parent
    still holds it. A fix at or before the introduction, on another line, or after an
    earlier fix of the same line would leave the defect nowhere, its introduction
    included, and so erase the failure its oracle must show there. An oracle's
    ``passes_at`` names commits that do not hold the defect: one that does, the
    introduction included, contradicts it.
    """
    for d in defects.values():
        for name in _fixed_at(commits, d):
            parent = commits[name].parent
            if name == d.introduced_at or d.introduced_at not in _ancestors(commits, name):
                raise ReviewEvaluationError(
                    f"{where}: {name!r} is said to fix {d.id} and does not descend from "
                    f"{d.introduced_at!r}, where it was introduced"
                )
            if parent is None or not _present(commits, d, parent):
                raise ReviewEvaluationError(
                    f"{where}: {name!r} is said to fix {d.id}, which its parent {parent!r} "
                    "does not hold: an earlier commit of its line fixed it"
                )
        for name in d.passes_at:
            if _present(commits, d, name):
                raise ReviewEvaluationError(
                    f"{where}: {d.id}'s oracle is said to pass at {name!r}, which holds it, "
                    "so it would both fail and pass there"
                )


def _oracles(raw: object, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ReviewEvaluationError(f"{where} must be an object of name: source")
    for name, source in raw.items():
        _str(name, f"{where} name")
        _str(source, f"{where}.{name}")
    return dict(raw)


def check_corpus(corpus: object, prereg: Mapping) -> tuple[dict[str, Case], datetime, str]:
    c = _obj(corpus, "corpus", ("schema", "commit_date", "discovery_cutoff", "cases"))
    if c["schema"] != CORPUS_SCHEMA:
        raise ReviewEvaluationError(f"corpus schema must be {CORPUS_SCHEMA!r}")
    commit_date = _instant(c["commit_date"], "corpus commit_date").isoformat()
    cutoff = _instant(c["discovery_cutoff"], "corpus discovery_cutoff")
    if not isinstance(c["cases"], list) or not c["cases"]:
        raise ReviewEvaluationError("corpus cases must be a non-empty list")
    cases: dict[str, Case] = {}
    for i, item in enumerate(c["cases"]):
        at = f"corpus case[{i}]"
        k = _obj(
            item,
            at,
            ("id", "kind", "summary", "commits", "candidate", "defects", "oracles"),
        )
        cid = _str(k["id"], f"{at}.id")
        at = f"corpus case {cid!r}"
        if cid in cases:
            raise ReviewEvaluationError(f"{at} is listed twice")
        kind = _one_of(k["kind"], CASE_KINDS, f"{at}.kind")
        _str(k["summary"], f"{at}.summary")
        commits = _commits(k["commits"], f"{at} commits", {}, run=False)
        candidate = _str(k["candidate"], f"{at}.candidate")
        if candidate not in commits or commits[candidate].parent is None:
            raise ReviewEvaluationError(f"{at}.candidate must name a commit after the base")
        oracles = _oracles(k["oracles"], f"{at}.oracles")
        after_base = [name for name, commit in commits.items() if commit.parent is not None]
        defects = _defects(
            k["defects"], f"case {cid!r} defects", prereg, commits, after_base, oracles, cutoff
        )
        _check_ground_truth(at, commits, defects)
        if kind == "clean_negative" and defects:
            raise ReviewEvaluationError(
                f"{at} is a clean negative with defects: it is a defect case, and only an "
                "amendment moves it"
            )
        if kind == "defect" and not any(
            d.registered and _present(commits, d, candidate) for d in defects.values()
        ):
            raise ReviewEvaluationError(
                f"{at} is a defect case with no registered defect present at its candidate: "
                "one discovered after the cutoff, or confined to another commit, gives a "
                "review of the candidate nothing to find, so the case counts toward no "
                "minimum and only an amendment moves it"
            )
        cases[cid] = Case(
            id=cid,
            kind=kind,
            commits=commits,
            candidate=candidate,
            defects=defects,
            oracles=oracles,
        )
    return cases, cutoff, commit_date


def _controller(
    raw: object, at: str, result: ReviewResult, rnd: int, sha: str
) -> tuple[str, str, bool | None, str]:
    """The controller's record of a round: (disposition, validation, validated at it,
    certificate).

    It is read from the controller's state, never from the reply, and has to agree
    with what the controller does. It records ``clean`` only for a clean reply and
    ``needs_fix`` only for one that needs a fix. It records the round before it
    issues the round's certificate, so a ``clean`` round over a failed validation,
    or over a binding of another round or candidate, is one it blocked on with no
    certificate (ADR 0006 D12.2), and is kept. It issues a certificate only for a
    ``clean`` round presented its own candidate's validation, every record ``PASS``
    or the set deliberately empty (D9.4, D5.5, D12.2), and a certificate names the
    round and the revision it was issued for: an issued one is held to both.
    """
    c = _obj(
        raw,
        at,
        ("disposition", "validation", "validated_sha", "certificate", "certified_as"),
    )
    disposition = _one_of(c["disposition"], DISPOSITIONS, f"{at}.disposition")
    validation = _one_of(c["validation"], VALIDATION_OUTCOMES, f"{at}.validation")
    certificate = _one_of(c["certificate"], CERTIFICATE_RECORDS, f"{at}.certificate")
    at_candidate: bool | None = None
    if validation == "unrecorded":
        if c["validated_sha"] is not None:
            raise ReviewEvaluationError(f"{at} records no validation and names a validated SHA")
    else:
        at_candidate = _sha(c["validated_sha"], f"{at}.validated_sha") == sha
    if disposition == "clean" and result.needs_fix_round:
        raise ReviewEvaluationError(f"{at} is clean over a reply that needs a fix")
    if disposition == "needs_fix" and not result.needs_fix_round:
        raise ReviewEvaluationError(f"{at} needs a fix over a clean reply")
    if certificate != "issued":
        if c["certified_as"] is not None:
            raise ReviewEvaluationError(f"{at} records no issued certificate and names one")
        return disposition, validation, at_candidate, certificate
    if disposition != "clean" or validation not in ("passed", "empty_set") or not at_candidate:
        raise ReviewEvaluationError(
            f"{at} records a certificate issued over a round the controller certifies "
            "nothing for: it issues one only for a clean round presented its own "
            "candidate's validation, every record PASS or the set empty (ADR 0006 D12.2)"
        )
    issued = _obj(c["certified_as"], f"{at}.certified_as", ("review_round", "reviewed_head_sha"))
    bound = (
        _int(issued["review_round"], f"{at}.certified_as.review_round", 1),
        _sha(issued["reviewed_head_sha"], f"{at}.certified_as.reviewed_head_sha"),
    )
    if bound != (rnd, sha):
        raise ReviewEvaluationError(
            f"{at} records a certificate of round {bound[0]} at {bound[1]}, not of round "
            f"{rnd} at the revision it reviewed: a certificate certifies only the round and "
            "revision it was issued for, and the controller keeps an earlier round's when it "
            "issues none, so this round's is not_issued"
        )
    return disposition, validation, at_candidate, certificate


def _review(
    raw: object,
    at: str,
    commits: Mapping[str, Commit],
) -> tuple[int, str, ReviewResult, tuple[str, str, bool | None, str]]:
    r = _obj(raw, at, ("round", "commit", "controller", "result"))
    rnd = _int(r["round"], f"{at}.round", 1)
    commit = r["commit"]
    if commit not in commits or commits[commit].parent is None:
        raise ReviewEvaluationError(f"{at}.commit {commit!r} is no reviewable commit of this run")
    payload = r["result"]
    if (
        not isinstance(payload, dict)
        or payload.get("phase") != "REVIEW"
        or payload.get("status") != "success"
    ):
        raise ReviewEvaluationError(f"{at}.result must be a successful REVIEW result")
    try:
        result = ReviewResult.from_payload(payload)
    except AutoForgeError as exc:
        raise ReviewEvaluationError(
            f"{at}.result is no REVIEW result the controller accepts: {exc}"
        ) from None
    if result.round != rnd:
        raise ReviewEvaluationError(f"{at}.result is of round {result.round}, not {rnd}")
    if result.reviewed_head_sha != commits[commit].sha:
        raise ReviewEvaluationError(f"{at}.result reviewed another HEAD than commit {commit!r}")
    controller = _controller(r["controller"], f"{at}.controller", result, rnd, commits[commit].sha)
    return rnd, commit, result, controller


def _labels(
    raw: object, where: str, prereg: Mapping, cutoff: datetime
) -> dict[tuple[int, str], dict]:
    if not isinstance(raw, list):
        raise ReviewEvaluationError(f"{where} must be a list")
    out: dict[tuple[int, str], dict] = {}
    refused = set(prereg["refused_label_fields"])
    for i, item in enumerate(raw):
        at = f"{where}[{i}]"
        if isinstance(item, dict) and refused & set(item):
            raise ReviewEvaluationError(
                f"{at} carries {sorted(refused & set(item))}: an agent's confidence, "
                "classification or verdict on its own work is never a label"
            )
        lab = _obj(
            item,
            at,
            ("finding", "round", "candidate_sha", "verdict", "adjudicator", "labeled_at"),
            ("defect", "reason"),
        )
        key = (_int(lab["round"], f"{at}.round", 1), _str(lab["finding"], f"{at}.finding"))
        if key in out:
            raise ReviewEvaluationError(f"{at} labels finding {key[1]} of round {key[0]} twice")
        _sha(lab["candidate_sha"], f"{at}.candidate_sha")
        verdict = _one_of(lab["verdict"], prereg["label_verdicts"], f"{at}.verdict")
        _adjudicator(lab["adjudicator"], f"{at}.adjudicator", prereg)
        if _instant(lab["labeled_at"], f"{at}.labeled_at") > cutoff:
            raise ReviewEvaluationError(
                f"{at} was labeled after the discovery cutoff: the labels are frozen there"
            )
        if verdict == "confirmed":
            _str(lab.get("defect"), f"{at}.defect")
        else:
            if "defect" in lab:
                raise ReviewEvaluationError(f"{at} is {verdict} and names a defect")
            _str(lab.get("reason"), f"{at}.reason")
        out[key] = lab
    return out


def _fix_label(raw: object, at: str, prereg: Mapping, cutoff: datetime) -> str:
    lab = _obj(raw, at, ("verdict", "reason", "adjudicator", "labeled_at"))
    verdict = _one_of(lab["verdict"], prereg["fix_verdicts"], f"{at}.verdict")
    _str(lab["reason"], f"{at}.reason")
    _adjudicator(lab["adjudicator"], f"{at}.adjudicator", prereg)
    if _instant(lab["labeled_at"], f"{at}.labeled_at") > cutoff:
        raise ReviewEvaluationError(f"{at} was labeled after the discovery cutoff")
    return verdict


def _launch(raw: object, at: str, prereg: Mapping, run_provenance: str) -> Launch:
    lc = _obj(
        raw,
        at,
        ("phase", "ordinal", "cost_usd", "wall_clock_seconds", "context_tokens", "provenance"),
        ("round",),
    )
    provenance = _one_of(lc["provenance"], prereg["usage_provenance"], f"{at}.provenance")
    if run_provenance == "fixture" and provenance == "measured":
        raise ReviewEvaluationError(f"{at}: a fixture run may not say it measured anything")
    phase = _one_of(lc["phase"], LAUNCH_PHASES, f"{at}.phase")
    rnd = lc.get("round")
    if rnd is not None:
        rnd = _int(rnd, f"{at}.round", 1)
    if phase in ("REVIEW", "FIX") and rnd is None:
        raise ReviewEvaluationError(f"{at} is a {phase} launch and names no round")
    if phase == "ANALYZE_EXECUTE" and rnd is not None:
        raise ReviewEvaluationError(
            f"{at} is an ANALYZE_EXECUTE launch, which precedes every round"
        )
    return Launch(
        phase=phase,
        round=rnd,
        ordinal=_int(lc["ordinal"], f"{at}.ordinal", 1),
        cost_usd=_number(lc["cost_usd"], f"{at}.cost_usd"),
        wall_clock_seconds=_number(lc["wall_clock_seconds"], f"{at}.wall_clock_seconds"),
        context_tokens=_number(lc["context_tokens"], f"{at}.context_tokens"),
        provenance=provenance,
    )


def _check_run(raw: object, prereg: Mapping, cases: Mapping[str, Case], cutoff: datetime) -> Run:
    r = _obj(
        raw,
        "run",
        (
            "id",
            "arm",
            "case",
            "provenance",
            "commits",
            "defects",
            "oracles",
            "reviews",
            "fixes",
            "labels",
            "launches",
            "launch_window",
        ),
        ("stopped", "excluded"),
    )
    rid = _str(r["id"], "run id")
    at = f"run {rid!r}"
    arm = _str(r["arm"], f"{at}.arm")
    case = cases.get(r["case"])  # type: ignore[arg-type]
    if case is None:
        raise ReviewEvaluationError(f"{at} names case {r['case']!r}, which the corpus has not")
    provenance = _one_of(r["provenance"], prereg["run_provenance"], f"{at}.provenance")
    run_commits = _commits(r["commits"], f"{at} commits", case.commits, run=True)
    commits = {**case.commits, **run_commits}
    oracles = {**case.oracles}
    for name, source in _oracles(r["oracles"], f"{at}.oracles").items():
        if name in oracles:
            raise ReviewEvaluationError(f"{at}.oracles names {name!r}, which the case holds")
        oracles[name] = source
    run_defects = _defects(
        r["defects"], f"run {rid!r} defects", prereg, commits, run_commits, oracles, cutoff
    )
    if set(run_defects) & set(case.defects):
        raise ReviewEvaluationError(f"{at} defines a defect id its case already has")
    defects = {**case.defects, **run_defects}
    for made in run_commits.values():
        for did in made.fixes:
            if did not in defects:
                raise ReviewEvaluationError(f"{at} commit {made.name!r} fixes no defect {did!r}")
    _check_ground_truth(at, commits, defects)
    labels = _labels(r["labels"], f"{at} labels", prereg, cutoff)

    if not isinstance(r["reviews"], list) or not r["reviews"]:
        raise ReviewEvaluationError(f"{at}.reviews must be a non-empty list")
    raw_fixes = r["fixes"]
    if not isinstance(raw_fixes, list):
        raise ReviewEvaluationError(f"{at}.fixes must be a list")
    fixes_by_round: dict[int, dict] = {}
    for i, item in enumerate(raw_fixes):
        f = _obj(item, f"{at} fixes[{i}]", ("round", "commit", "resolutions"), ("label",))
        rnd = _int(f["round"], f"{at} fixes[{i}].round", 1)
        if rnd in fixes_by_round:
            raise ReviewEvaluationError(f"{at} has two fixes of round {rnd}")
        fixes_by_round[rnd] = f

    reviews: list[Review] = []
    fixes: list[Fix] = []
    used: set[tuple[int, str]] = set()
    expected_commit = case.candidate
    for i, item in enumerate(r["reviews"]):
        where = f"{at} review[{i}]"
        rnd, commit, result, (disposition, validation, at_candidate, certificate) = _review(
            item, where, commits
        )
        if rnd != i + 1:
            raise ReviewEvaluationError(f"{where} is round {rnd}; rounds run 1, 2, ... in order")
        if commit != expected_commit:
            raise ReviewEvaluationError(
                f"{where} reviews {commit!r}; the run's line says {expected_commit!r}"
            )
        findings: list[Finding] = []
        for finding in result.findings:
            key = (rnd, finding.id)
            lab = labels.get(key)
            if lab is None:
                raise ReviewEvaluationError(
                    f"{where} finding {finding.id} has no label: every finding is adjudicated, "
                    "and an undecided one is labeled unknown"
                )
            used.add(key)
            if lab["candidate_sha"] != commits[commit].sha:
                raise ReviewEvaluationError(
                    f"{at} label of {finding.id} names another candidate than round {rnd} reviewed"
                )
            defect = lab.get("defect")
            if defect is not None:
                d = defects.get(defect)
                if d is None:
                    raise ReviewEvaluationError(
                        f"{at} label of {finding.id} names no defect {defect!r}"
                    )
                if _instant(lab["labeled_at"], "labeled_at") < d.discovered_at:
                    raise ReviewEvaluationError(
                        f"{at} label of {finding.id} confirms {defect} before it was discovered"
                    )
            findings.append(Finding(finding.id, finding.classification, lab["verdict"], defect))
        review = Review(
            round=rnd,
            commit=commit,
            clean=not result.needs_fix_round,
            disposition=disposition,
            validation=validation,
            validated_at_candidate=at_candidate,
            certificate=certificate,
            findings=tuple(findings),
        )
        reviews.append(review)
        last = i == len(r["reviews"]) - 1
        fix_raw = fixes_by_round.pop(rnd, None)
        if disposition == "stale":
            # The controller fixes no stale round and reviews the moved revision next,
            # which no commit of a run record is: the record ends there.
            if not last:
                raise ReviewEvaluationError(
                    f"{where} is stale: the controller reviews the moved revision next, "
                    "which this record does not hold, so a run's record ends at a stale round"
                )
            if fix_raw is not None:
                raise ReviewEvaluationError(f"{where} is stale, so no fix follows it")
        if review.clean:
            if not last:
                raise ReviewEvaluationError(f"{where} is clean, so no round follows it")
            if fix_raw is not None:
                raise ReviewEvaluationError(f"{where} is clean, so no fix follows it")
            continue
        if fix_raw is None:
            if not last:
                raise ReviewEvaluationError(f"{where} needs a fix and another round follows it")
            continue
        fat = f"{at} fix of round {rnd}"
        resolutions = fix_raw["resolutions"]
        if not isinstance(resolutions, dict) or set(resolutions) != {f.id for f in findings}:
            raise ReviewEvaluationError(f"{fat} must resolve exactly round {rnd}'s findings")
        for fid, kind in resolutions.items():
            _one_of(kind, FIX_RESOLUTIONS, f"{fat} resolution of {fid}")
        fix_commit = fix_raw["commit"]
        verdict: str | None = None
        if fix_commit is None:
            if "label" in fix_raw:
                raise ReviewEvaluationError(f"{fat} made no commit, so it has nothing to label")
            expected_commit = commit
        else:
            if fix_commit not in run_commits or run_commits[fix_commit].parent != commit:
                raise ReviewEvaluationError(
                    f"{fat} commit {fix_commit!r} must be a run commit on the reviewed {commit!r}"
                )
            if "label" not in fix_raw:
                raise ReviewEvaluationError(f"{fat} made a commit and has no label")
            verdict = _fix_label(fix_raw["label"], f"{fat} label", prereg, cutoff)
            introduced = [
                d.id for d in run_defects.values() if d.introduced_at == fix_commit and d.registered
            ]
            if verdict == "harmful" and not introduced:
                raise ReviewEvaluationError(
                    f"{fat} is labeled harmful and introduces no registered defect"
                )
            if verdict != "harmful" and introduced:
                raise ReviewEvaluationError(
                    f"{fat} introduces {introduced}, so it is harmful, not {verdict}"
                )
            expected_commit = fix_commit
        fixes.append(Fix(rnd, fix_commit, dict(resolutions), verdict))
    if fixes_by_round:
        raise ReviewEvaluationError(
            f"{at} has a fix of round(s) {sorted(fixes_by_round)} no review needs"
        )
    unused = sorted(set(labels) - used)
    if unused:
        raise ReviewEvaluationError(f"{at} labels finding(s) no review raised: {unused}")
    stopped = r.get("stopped", "")
    ending = reviews[-1].certification
    if ending == CERTIFIED and stopped:
        raise ReviewEvaluationError(
            f"{at} ended on a round the controller certified and says it was stopped"
        )
    if ending == NOT_CERTIFIED:
        _str(stopped, f"{at}.stopped")
    excluded = r.get("excluded", "")
    if excluded:
        _str(excluded, f"{at}.excluded")
    if not isinstance(r["launches"], list):
        raise ReviewEvaluationError(f"{at}.launches must be a list")
    launches = tuple(
        _launch(item, f"{at} launch[{i}]", prereg, provenance)
        for i, item in enumerate(r["launches"])
    )
    rounds = {review.round for review in reviews}
    needing_fix = {review.round for review in reviews if not review.clean}
    for i, launch in enumerate(launches):
        if launch.round is None:
            continue
        if launch.phase == "FIX" and launch.round not in needing_fix:
            raise ReviewEvaluationError(
                f"{at} launch[{i}] is a FIX launch of round {launch.round}, which needed no fix"
            )
        if launch.round not in rounds:
            raise ReviewEvaluationError(
                f"{at} launch[{i}] is a {launch.phase} launch of round {launch.round}, "
                "a round the run does not have"
            )
    window: LaunchWindow | None = None
    if r["launch_window"] is not None:
        w = _obj(r["launch_window"], f"{at}.launch_window", ("start", "end", "last"))
        end = _one_of(w["end"], WINDOW_ENDS, f"{at}.launch_window.end")
        if end == "unrecorded":
            if w["last"] is not None:
                raise ReviewEvaluationError(
                    f"{at}.launch_window records no end and names a last ordinal: the "
                    "state's launch_seq is the run's last ordinal only at its end"
                )
            last_ordinal = None
        else:
            last_ordinal = _int(w["last"], f"{at}.launch_window.last")
        window = LaunchWindow(
            start=_one_of(w["start"], WINDOW_STARTS, f"{at}.launch_window.start"),
            end=end,
            last=last_ordinal,
        )
    spent: dict[int, int] = {}
    for i, launch in enumerate(launches):
        if launch.ordinal in spent:
            raise ReviewEvaluationError(
                f"{at} launch[{spent[launch.ordinal]}] and launch[{i}] name ordinal "
                f"{launch.ordinal}: the controller spends each ordinal on one launch"
            )
        spent[launch.ordinal] = i
        if window and window.last is not None and launch.ordinal > window.last:
            raise ReviewEvaluationError(
                f"{at} launch[{i}] names ordinal {launch.ordinal}, past the last ordinal "
                f"{window.last} the controller spent on the run"
            )
    run = Run(
        id=rid,
        arm=arm,
        case=case,
        provenance=provenance,
        commits=commits,
        defects=defects,
        reviews=tuple(reviews),
        fixes=tuple(fixes),
        launches=launches,
        launch_window=window,
        stopped=stopped,
        excluded=excluded,
        oracles=oracles,
    )
    if (
        window is not None
        and window.end == "READY_FOR_MERGE"
        and run.went_on() is False
        and ending == NOT_CERTIFIED
    ):
        raise ReviewEvaluationError(
            f"{at} ended at READY_FOR_MERGE on a round the controller certified nothing "
            "for: it enters READY_FOR_MERGE only over the completed round's certificate"
        )
    for review in run.reviews:
        for finding in review.findings:
            if finding.defect is not None and not run.present(
                defects[finding.defect], review.commit
            ):
                raise ReviewEvaluationError(
                    f"{at} confirms {finding.defect} at round {review.round}, whose commit does "
                    "not hold it"
                )
    return run


def check(prereg: object, corpus: object, runs: object) -> Evaluation:
    """The checked contract, or :class:`ReviewEvaluationError` naming what it refuses."""
    p = check_preregistration(prereg)
    cases, cutoff, commit_date = check_corpus(corpus, p)
    rr = _obj(runs, "runs", ("schema", "runs"))
    if rr["schema"] != RUNS_SCHEMA:
        raise ReviewEvaluationError(f"runs schema must be {RUNS_SCHEMA!r}")
    if not isinstance(rr["runs"], list):
        raise ReviewEvaluationError("runs.runs must be a list")
    checked = [_check_run(item, p, cases, cutoff) for item in rr["runs"]]
    seen: set[tuple[str, str]] = set()
    ids: set[str] = set()
    for run in checked:
        if run.id in ids:
            raise ReviewEvaluationError(f"run id {run.id!r} is used twice")
        ids.add(run.id)
        key = (run.arm, run.case.id)
        if key in seen:
            raise ReviewEvaluationError(
                f"arm {run.arm!r} has two runs of case {run.case.id!r}: a planned run is not "
                "repeated, so neither may stand in for the other"
            )
        seen.add(key)
    return Evaluation(
        preregistration=p,
        cases=cases,
        runs=tuple(checked),
        commit_date=commit_date,
        digests={},
    )


def load(
    preregistration: Path = PREREGISTRATION, corpus: Path = CORPUS, runs: Path = FIXTURE_RUNS
) -> Evaluation:
    """The checked files, each digested from the very bytes that were checked."""
    raw = {
        name: path.read_bytes()
        for name, path in (("preregistration", preregistration), ("corpus", corpus), ("runs", runs))
    }
    ev = check(*(json.loads(data.decode("utf-8")) for data in raw.values()))
    return Evaluation(
        preregistration=ev.preregistration,
        cases=ev.cases,
        runs=ev.runs,
        commit_date=ev.commit_date,
        digests={name: hashlib.sha256(data).hexdigest() for name, data in raw.items()},
    )


def registered_inputs(ev: Evaluation) -> bool:
    """Whether ``ev`` was loaded from the registered corpus and fixture runs.

    Their oracles are code reviewed with this repository; another file's are not,
    and ``verify`` runs those only when the caller says it trusts them.
    """
    registered = {"corpus": file_digest(CORPUS), "runs": file_digest(FIXTURE_RUNS)}
    return all(ev.digests.get(name) == digest for name, digest in registered.items())


# -- replay -------------------------------------------------------------------------------


def _git(cwd: Path, date: str, *args: str) -> str:
    env = {
        **select_environment(_GIT_ENV_ALLOWLIST),
        **_GIT_ENV,
        "GIT_AUTHOR_NAME": "AutoForge review fixture",
        "GIT_AUTHOR_EMAIL": "review-fixture@example.invalid",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": "AutoForge review fixture",
        "GIT_COMMITTER_EMAIL": "review-fixture@example.invalid",
        "GIT_COMMITTER_DATE": date,
    }
    res = subprocess.run(
        ["git", *_GIT_SWITCHES, "-C", str(cwd), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise ReviewEvaluationError(f"git {args[0]}: {res.stderr.strip()}")
    return res.stdout.strip()


def tree_at(commits: Mapping[str, Commit], name: str) -> dict[str, str]:
    """Every file of commit ``name``: its line's overlays, the oldest first."""
    line: list[Commit] = []
    current: str | None = name
    while current is not None:
        line.append(commits[current])
        current = commits[current].parent
    files: dict[str, str] = {}
    for commit in reversed(line):
        files.update(commit.files)
    return files


@dataclass(frozen=True)
class Built:
    """The commits of one line built in ``repo``: what each built to, and how faithfully."""

    repo: Path
    object_format: str
    shas: Mapping[str, str]
    # The commits whose tree is not byte for byte the files that built them, each with
    # the paths that differ: a file git ignored or normalized, or one it added.
    unfaithful: Mapping[str, tuple[str, ...]]


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _tree_blobs(repo: Path, date: str, sha: str) -> dict[str, str]:
    """``path: blob id`` of every entry of ``sha``'s tree; a non-file entry reads as its mode."""
    entries: dict[str, str] = {}
    for record in _git(repo, date, "ls-tree", "-r", "-z", "--full-tree", sha).split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, kind, oid = meta.split(" ")
        entries[path] = oid if (mode, kind) == ("100644", "blob") else f"{mode} {kind}"
    return entries


def build(commits: Mapping[str, Commit], date: str, label: str, root: Path) -> Built:
    """Every commit of ``commits`` made in ``root`` at the fixed ``date``.

    Each commit's tree is then held to the files that built it, byte for byte: git
    leaves out an ignored file and may rewrite a file's bytes as an attribute says,
    and the pinned SHA is of what git committed, never of the files.
    """
    repo = root / "repo"
    repo.mkdir()
    _git(repo, date, "init", "-q")
    built: dict[str, str] = {}
    unfaithful: dict[str, tuple[str, ...]] = {}
    for commit in commits.values():
        if commit.parent is None:
            _git(repo, date, "checkout", "-q", "--orphan", f"line-{len(built)}")
        else:
            _git(repo, date, "checkout", "-q", "--detach", built[commit.parent])
        _git(repo, date, "rm", "-rq", "--ignore-unmatch", ".")
        files = tree_at(commits, commit.name)
        for relpath, text in files.items():
            path = repo / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        _git(repo, date, "add", "-A")
        _git(repo, date, "commit", "-q", "--allow-empty", "-m", f"{label}: {commit.name}")
        sha = _git(repo, date, "rev-parse", "HEAD")
        built[commit.name] = sha
        wanted = {path: _blob_id(text.encode("utf-8")) for path, text in files.items()}
        held = _tree_blobs(repo, date, sha)
        differ = sorted(p for p in wanted.keys() | held.keys() if wanted.get(p) != held.get(p))
        if differ:
            unfaithful[commit.name] = tuple(differ)
    return Built(
        repo=repo,
        object_format=read_object_format(execute, str(repo)),
        shas=built,
        unfaithful=unfaithful,
    )


def oracle_at(source: str, built: Built, name: str, root: Path) -> str:
    """:func:`run_oracle` on the tree of the commit ``name`` built to, and nothing else.

    The tree is the controller's byte-exact export (ADR 0006 D6.2, D6.3): every blob
    of exactly that commit written as its committed bytes, the directory proven to
    hash back to the commit's tree before the oracle runs. So the oracle reads what
    the SHA names: two inputs that build the same commit give it the same bytes,
    whatever files built it.
    """
    try:
        with materialize(
            execute, str(built.repo), built.shas[name], built.object_format, prefix="review-tree-"
        ) as tree:
            return run_oracle(source, tree.root, root)
    except AutoForgeError as exc:
        return f"error: the tree of {name!r} was not materialized: {exc}"


def run_oracle(source: str, tree: Path, root: Path) -> str:
    """``pass``, ``fail`` (an ``AssertionError``) or ``error: <last line>``.

    ``source`` runs in the directory ``tree``, which it imports from, as the caller:
    ``python -I`` and the locale-only environment keep the host's variables, user
    site and working directory out, and nothing here confines what the code reads,
    writes or connects to. Run only what the caller trusts (:func:`registered_inputs`).
    ``verify`` gives it a commit's tree (:func:`oracle_at`).
    """
    script_dir = Path(tempfile.mkdtemp(prefix="oracle-", dir=root))
    script = script_dir / "oracle.py"
    script.write_text("import sys\nsys.path.insert(0, sys.argv[1])\n" + source, encoding="utf-8")
    try:
        res = subprocess.run(
            [sys.executable, "-I", "-B", str(script), str(tree)],
            cwd=tree,
            env=select_environment(("LANG", "LC_*")),
            capture_output=True,
            text=True,
            timeout=ORACLE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "error: timed out"
    if res.returncode == 0:
        return "pass"
    last = (res.stderr.strip().splitlines() or [""])[-1]
    if res.returncode == 1 and last.startswith("AssertionError"):
        return "fail"
    return f"error: {last or f'exit {res.returncode}'}"


def _expectations(
    owner: str,
    defects: Iterable[Defect],
    commits: Mapping[str, Commit],
    decided: Iterable[str],
) -> list[tuple[str, str, str]]:
    """(defect, commit, pass|fail) wherever ``defects``' ground truth decides an oracle.

    It fails where the defect was introduced and at every ``decided`` commit that
    holds it, and passes at every commit that fixes it, every other its evidence
    names and every ``decided`` commit of its line that does not hold it. A commit
    given both is a contradiction, refused rather than settled either way: the
    failure at the introduction is what shows the defect at all.
    """
    out: list[tuple[str, str, str]] = []
    for d in defects:
        if d.oracle is None:
            continue
        expected: dict[str, str] = {}

        def expect(name: str, outcome: str, d: Defect = d, expected: dict = expected) -> None:
            if expected.setdefault(name, outcome) != outcome:
                raise ReviewEvaluationError(
                    f"{owner}: {d.id}'s oracle is expected to both fail and pass at {name!r}"
                )

        expect(d.introduced_at, "fail")
        for name in (*d.passes_at, *_fixed_at(commits, d)):
            expect(name, "pass")
        for name in sorted(decided):
            if d.introduced_at in _ancestors(commits, name):
                expect(name, "fail" if _present(commits, d, name) else "pass")
        for name, outcome in expected.items():
            if outcome == "pass" and _present(commits, d, name):
                raise ReviewEvaluationError(
                    f"{owner}: {d.id} is said to pass at {name!r}, which holds it"
                )
            out.append((d.id, name, outcome))
    return sorted(out)


def case_oracle_expectations(case: Case) -> list[tuple[str, str, str]]:
    """(defect, commit, pass|fail) at the case's own commits, whether or not a run holds it."""
    return _expectations(
        f"case {case.id!r}", case.defects.values(), case.commits, (case.candidate,)
    )


def oracle_expectations(run: Run) -> list[tuple[str, str, str]]:
    """(defect, commit, pass|fail): every place the ground truth says an oracle decides."""
    reviewed = {review.commit for review in run.reviews}
    return _expectations(f"run {run.id!r}", run.defects.values(), run.commits, reviewed)


def _pins(owner: str, commits: Mapping[str, Commit], built: Built) -> list[str]:
    problems: list[str] = []
    for name, commit in commits.items():
        if built.shas[name] != commit.sha:
            problems.append(
                f"{owner}: commit {name!r} is pinned at {commit.sha} and builds as "
                f"{built.shas[name]}"
            )
        if name in built.unfaithful:
            problems.append(
                f"{owner}: commit {name!r}'s tree is not the files that built it, at "
                f"{list(built.unfaithful[name])}: git left out or rewrote what the files hold"
            )
    return problems


def verify(ev: Evaluation, root: Path, *, oracles: bool) -> list[str]:
    """Replay every case of the corpus, then every run, under ``root``; the problems found.

    Each case is replayed on its own, whether or not a run holds it: its commits are
    held to their pins and their trees to the files that built them, and its oracles
    run at its commits. A run adds what only it holds: its own commits and the oracles
    at them. An empty list of runs still replays the whole corpus. Every oracle reads
    the tree of the commit it runs at (:func:`oracle_at`), never the files. Building a
    commit runs nothing the files hold; with ``oracles`` false no oracle runs either,
    so only the pins are replayed. The caller passes ``oracles`` true only for inputs
    it trusts (:func:`run_oracle`).
    """
    problems: list[str] = []
    for index, case in enumerate(ev.cases.values()):
        where = Path(tempfile.mkdtemp(prefix=f"case-{index}-", dir=root))
        owner = f"case {case.id!r}"
        built = build(case.commits, ev.commit_date, case.id, where)
        problems += _pins(owner, case.commits, built)
        expectations = case_oracle_expectations(case)
        if not oracles:
            continue
        for did, name, outcome in expectations:
            source = case.oracles[str(case.defects[did].oracle)]
            got = oracle_at(source, built, name, where)
            if got != outcome:
                problems.append(
                    f"{owner}: {did}'s oracle should {outcome} at {name!r} and gave {got}"
                )
    for index, run in enumerate(ev.runs):
        owner = f"run {run.id!r}"
        replayed = {(did, name) for did, name, _ in case_oracle_expectations(run.case)}
        own = {name: c for name, c in run.commits.items() if name not in run.case.commits}
        expectations = [
            e for e in oracle_expectations(run) if oracles and (e[0], e[1]) not in replayed
        ]
        if not own and not expectations:
            continue
        where = Path(tempfile.mkdtemp(prefix=f"run-{index}-", dir=root))
        built = build(run.commits, ev.commit_date, run.case.id, where)
        problems += _pins(owner, own, built)
        for did, name, outcome in expectations:
            source = run.oracles[str(run.defects[did].oracle)]
            got = oracle_at(source, built, name, where)
            if got != outcome:
                problems.append(
                    f"{owner}: {did}'s oracle should {outcome} at {name!r} and gave {got}"
                )
    return problems


# -- scoring ------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunScore:
    """One run's contribution to each pooled metric; every count is the run's own."""

    run: Run
    recall_hit: int
    recall_weight: int
    recall_upper: int
    confirmed: int
    false: int
    unknown: int
    duplicates: int
    # Unknown findings of rounds with a confirmed one: each could be a duplicate.
    unknown_beside_confirmed: int
    # The unknown findings of each round with two or more, no confirmed one and a
    # registered defect at its commit: all but one could be duplicates of the first.
    unknown_groups: tuple[int, ...]
    # Unknown findings that could each be the first report of a registered defect at
    # their round's commit that no other finding of the round names.
    unknown_first_reports: int
    # Unknown findings of rounds whose commit holds a registered defect: the only ones
    # a label the check accepts could confirm.
    unknown_confirmable: int
    cn_outcome: str  # "", "true_negative", "false_positive", "unknown"
    harmful: int
    not_harmful: int
    harm_unknown: int
    # Whether the controller certified the candidate the run ended on.
    certification: str
    # Registered defects at the final round's candidate when it is certified, and when
    # its certification is unknown (no record of its certificate, or a run that went on
    # past its record).
    escaped: tuple[tuple[str, str], ...]
    possible_escapes: tuple[tuple[str, str], ...]
    success: bool | None


def _augment(
    defect: str, rounds: Sequence[tuple[int, frozenset[str]]], taken: list[list[str]], seen: set
) -> bool:
    """Give ``defect`` an unknown finding of a round that could name it, moving others."""
    for i, (capacity, names) in enumerate(rounds):
        if defect not in names or i in seen:
            continue
        seen.add(i)
        if len(taken[i]) < capacity:
            taken[i].append(defect)
            return True
        for j, other in enumerate(taken[i]):
            if _augment(other, rounds, taken, seen):
                taken[i][j] = defect
                return True
    return False


def _nameable_weight(
    rounds: Sequence[tuple[int, frozenset[str]]], defects: Sequence[Defect]
) -> int:
    """The heaviest set of ``defects`` the unknown findings could confirm, one each.

    ``rounds`` holds each round's unknown-finding count and the defects a label of
    that round could name. A finding names one defect, so this is a maximum-weight
    matching; the sets a matching can cover form a matroid, where taking the
    heaviest defect first while one still fits is optimal.
    """
    taken: list[list[str]] = [[] for _ in rounds]
    total = 0
    for d in sorted(defects, key=lambda d: (-d.weight, d.id)):
        if _augment(d.id, rounds, taken, set()):
            total += d.weight
    return total


def _success(
    certification: str, validation: str, escapes: Iterable[tuple[str, str]], blocking: set
) -> bool | None:
    """A validated success, or ``None`` while the controller's record leaves it open."""
    if certification == NOT_CERTIFIED:
        return False
    if validation == "empty_set" or any(severity in blocking for _, severity in escapes):
        return False
    return True if certification == CERTIFIED else None


def score_run(run: Run, prereg: Mapping) -> RunScore:
    registered = [d for d in run.defects.values() if d.registered]
    reviewed = [review.commit for review in run.reviews]
    exposed = [d for d in registered if any(run.present(d, c) for c in reviewed)]
    detected = {
        f.defect for review in run.reviews for f in review.findings if f.verdict == "confirmed"
    }
    hit = sum(d.weight for d in exposed if d.id in detected)
    weight = sum(d.weight for d in exposed)
    # A label the check accepts confirms a registered defect present at its round's
    # commit, one per finding: the bounds settle unknown findings no other way.
    eligible = [
        frozenset(d.id for d in registered if run.present(d, review.commit))
        for review in run.reviews
    ]
    undetected = [d for d in exposed if d.id not in detected]
    upper = hit + _nameable_weight(
        [
            (sum(1 for f in review.findings if f.verdict == "unknown"), names)
            for review, names in zip(run.reviews, eligible, strict=True)
        ],
        undetected,
    )
    verdicts = [f.verdict for review in run.reviews for f in review.findings]
    duplicates = 0
    unknown_beside_confirmed = 0
    unknown_groups: list[int] = []
    unknown_first_reports = 0
    unknown_confirmable = 0
    for review, names in zip(run.reviews, eligible, strict=True):
        named = [f.defect for f in review.findings if f.verdict == "confirmed"]
        duplicates += len(named) - len(set(named))
        undecided = sum(1 for f in review.findings if f.verdict == "unknown")
        if not names:
            continue  # nothing registered at this commit: an unknown finding is false here
        unknown_confirmable += undecided
        unknown_first_reports += min(undecided, len(names - set(named)))
        if named:
            unknown_beside_confirmed += undecided
        elif undecided >= 2:
            unknown_groups.append(undecided)
    cn_outcome = ""
    if run.case.kind == "clean_negative":
        on_candidate = [
            f
            for review in run.reviews
            if review.commit in run.case.commits
            for f in review.findings
        ]
        if not on_candidate:
            cn_outcome = "true_negative"
        elif any(f.verdict == "false" for f in on_candidate):
            cn_outcome = "false_positive"
        else:
            cn_outcome = "unknown"
    fix_verdicts = [fix.verdict for fix in run.fixes if fix.commit is not None]
    final = run.reviews[-1]
    at_final = tuple(sorted((d.id, d.severity) for d in registered if run.present(d, final.commit)))
    certification = run.certification()
    blocking = set(prereg["success_blocking_severities"])
    return RunScore(
        run=run,
        recall_hit=hit,
        recall_weight=weight,
        recall_upper=upper,
        confirmed=verdicts.count("confirmed"),
        false=verdicts.count("false"),
        unknown=verdicts.count("unknown"),
        duplicates=duplicates,
        unknown_beside_confirmed=unknown_beside_confirmed,
        unknown_groups=tuple(unknown_groups),
        unknown_first_reports=unknown_first_reports,
        unknown_confirmable=unknown_confirmable,
        cn_outcome=cn_outcome,
        harmful=fix_verdicts.count("harmful"),
        not_harmful=fix_verdicts.count("not_harmful"),
        harm_unknown=fix_verdicts.count("unknown"),
        certification=certification,
        escaped=at_final if certification == CERTIFIED else (),
        possible_escapes=at_final if certification == CERTIFICATION_UNKNOWN else (),
        success=_success(certification, final.validation, at_final, blocking),
    )


def _ratio(num: int, den: int) -> Fraction | None:
    """``num / den`` exactly, or ``None`` over an empty denominator."""
    return Fraction(num, den) if den else None


def _duplicate_upper(scores: Sequence[RunScore]) -> Fraction | None:
    """The highest duplicate ratio any settlement of the unknown labels allows.

    A settlement is one the check accepts: an unknown finding becomes ``false``, or
    ``confirmed`` of a registered defect present at its round's commit. A duplicate
    is a later confirmed finding of a round naming the defect an earlier one of that
    round names. An unknown finding beside a confirmed one could be a duplicate of
    it, which adds one to both counts and so always raises the ratio (it is below 1).
    A round of ``k`` unknown findings, no confirmed one and a registered defect at
    its commit could hold ``k - 1`` duplicates of ``k`` confirmed findings: such
    rounds are taken, the highest ``(k - 1) / k`` first, while each raises the
    ratio, which is where the maximum of a ratio of sums lies. Every other unknown
    finding is put false; one of a round whose commit holds no registered defect can
    be nothing else. ``None`` when no settlement confirms any finding.
    """
    dup = sum(x.duplicates + x.unknown_beside_confirmed for x in scores)
    confirmed = sum(x.confirmed + x.unknown_beside_confirmed for x in scores)
    for k in sorted((k for x in scores for k in x.unknown_groups), reverse=True):
        if confirmed and Fraction(k - 1, k) <= Fraction(dup, confirmed):
            break
        dup, confirmed = dup + k - 1, confirmed + k
    if confirmed == 0 and any(x.unknown_confirmable for x in scores):
        return Fraction(0)  # a single unknown finding confirmed alone: no duplicate of one
    return _ratio(dup, confirmed)


def _bands(
    scores: Sequence[RunScore],
) -> dict[str, tuple[Fraction | None, Fraction | None, Fraction | None]]:
    """Each rate metric's (point, lower, upper), exactly: the bounds put every unknown
    label each way."""
    s = {
        key: sum(getattr(x, key) for x in scores)
        for key in (
            "recall_hit",
            "recall_weight",
            "recall_upper",
            "confirmed",
            "false",
            "unknown",
            "duplicates",
            "unknown_first_reports",
            "harmful",
            "not_harmful",
            "harm_unknown",
        )
    }
    cn = [x.cn_outcome for x in scores if x.cn_outcome]
    tn, fp, cu = cn.count("true_negative"), cn.count("false_positive"), cn.count("unknown")
    labeled = s["confirmed"] + s["false"] + s["unknown"]
    fixes = s["harmful"] + s["not_harmful"] + s["harm_unknown"]
    return {
        "weighted_recall": (
            _ratio(s["recall_hit"], s["recall_weight"]),
            _ratio(s["recall_hit"], s["recall_weight"]),
            _ratio(s["recall_upper"], s["recall_weight"]),
        ),
        "precision": (
            _ratio(s["confirmed"], s["confirmed"] + s["false"]),
            _ratio(s["confirmed"], labeled),
            _ratio(s["confirmed"] + s["unknown"], labeled),
        ),
        "clean_negative_fpr": (
            _ratio(fp, fp + tn),
            _ratio(fp, fp + tn + cu),
            _ratio(fp + cu, fp + tn + cu),
        ),
        "harmful_fix_rate": (
            _ratio(s["harmful"], s["harmful"] + s["not_harmful"]),
            _ratio(s["harmful"], fixes),
            _ratio(s["harmful"] + s["harm_unknown"], fixes),
        ),
        # Lower: as many unknown findings as there are registered defects at their
        # round's commit that no finding of the round names, each confirmed the first
        # report of one, which adds to the confirmed count alone; the rest put false.
        "duplicate_ratio": (
            _ratio(s["duplicates"], s["confirmed"]),
            _ratio(s["duplicates"], s["confirmed"] + s["unknown_first_reports"]),
            _duplicate_upper(scores),
        ),
    }


def _terms(scores: Sequence[RunScore], metric: str) -> tuple[list[int], list[int]]:
    """Each run's numerator and denominator of ``metric``'s point estimate, in order."""
    pick: dict[str, Callable[[RunScore], tuple[int, int]]] = {
        "weighted_recall": lambda x: (x.recall_hit, x.recall_weight),
        "precision": lambda x: (x.confirmed, x.confirmed + x.false),
        "clean_negative_fpr": lambda x: (
            int(x.cn_outcome == "false_positive"),
            int(x.cn_outcome in ("false_positive", "true_negative")),
        ),
        "harmful_fix_rate": lambda x: (x.harmful, x.harmful + x.not_harmful),
        "duplicate_ratio": lambda x: (x.duplicates, x.confirmed),
    }
    pairs = [pick[metric](x) for x in scores]
    return [n for n, _ in pairs], [d for _, d in pairs]


def _resampled(
    terms: tuple[list[int], list[int]], indices: list[list[int]], *, exactly: bool = False
) -> list:
    """The pooled point estimate on every resample: ``None`` where its denominator is empty.

    A float for a report's interval, which is shown rounded; with ``exactly``, the
    :class:`~fractions.Fraction` a decision compares with its registered threshold.
    """
    nums, dens = terms
    out: list = []
    for sample in indices:
        den = sum(map(dens.__getitem__, sample))
        num = sum(map(nums.__getitem__, sample))
        out.append(None if not den else Fraction(num, den) if exactly else num / den)
    return out


def bootstrap_indices(n: int, prereg: Mapping) -> list[list[int]]:
    """The resamples of ``n`` cases every interval of one arm, or one pair, reads."""
    u = prereg["uncertainty"]
    rng = random.Random(u["seed"])
    return [[rng.randrange(n) for _ in range(n)] for _ in range(u["resamples"])]


def percentile_interval(values: list, confidence: float) -> tuple | None:
    """Nearest-rank percentile interval of ``values``, or ``None`` with none to rank.

    Each end is the value of one-based rank ``ceil(p * n)`` among the ``n`` ordered
    values, ``p`` being ``(1 - confidence) / 2`` for the lower end and ``1 - p`` for
    the upper: ranks 1 and 39 of 40 at 95%. ``p * n`` is computed exactly, from the
    confidence as written, so no rounding of a binary fraction moves a rank.
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    tail = (1 - Fraction(repr(confidence))) / 2
    low = max(1, math.ceil(tail * n))
    high = max(1, math.ceil((1 - tail) * n))
    return ordered[low - 1], ordered[high - 1]


def _rounded(value: float | Fraction | None) -> float | None:
    """A value as a report shows it: six decimals. No decision reads a rounded value."""
    return None if value is None else round(float(value), 6)


def exact_sum(scores: Sequence[RunScore], attr: str) -> Fraction:
    """The sum of ``attr`` over the launches that recorded it, as the decimals written."""
    return sum(
        (
            exact(v)
            for x in scores
            for launch in x.run.launches
            if (v := getattr(launch, attr)) is not None
        ),
        Fraction(0),
    )


def _usage(scores: Sequence[RunScore], attr: str) -> dict:
    """The sum of ``attr`` over every launch of every run, or ``None`` with why.

    The sum is unknown when a launch's value or provenance is, and when a run's
    launch record is incomplete (:meth:`Run.missing_launches`): a launch nobody
    recorded is no launch that cost nothing. The known part is shown apart.
    """
    values = [getattr(launch, attr) for x in scores for launch in x.run.launches]
    provenances = {launch.provenance for x in scores for launch in x.run.launches}
    known = exact_sum(scores, attr)
    incomplete = sorted(x.run.id for x in scores if x.run.missing_launches())
    why = ""
    if not values:
        why = "no launch is recorded"
    elif incomplete:
        why = f"the launch record of run(s) {incomplete} is incomplete"
    elif any(v is None for v in values) or "unknown" in provenances:
        why = "a launch's value is unknown"
    if why:
        return {
            "value": None,
            "known_part": _rounded(known),
            "provenance": "unknown",
            "why_unknown": why,
        }
    for weakest in ("synthetic", "estimated"):
        if weakest in provenances:
            return {"value": _rounded(known), "provenance": weakest}
    return {"value": _rounded(known), "provenance": "measured"}


def arm_metrics(
    scores: Sequence[RunScore], prereg: Mapping, indices: list[list[int]] | None
) -> dict:
    """An arm's metrics as a report shows them, every status decided exactly.

    With ``indices`` ``None`` no interval is computed: what a decision reads of an
    arm's own metrics is their status and provenance, never an interval or a
    rounded value.
    """
    limit = prereg["max_label_uncertainty"]
    confidence = prereg["uncertainty"]["confidence"]
    bands = _bands(scores)
    metrics: dict[str, dict] = {}
    for metric in RATE_METRICS:
        point, lower, upper = bands[metric]
        interval = None
        dropped = None
        if indices is not None:
            resampled = _resampled(_terms(scores, metric), indices)
            values = [v for v in resampled if v is not None]
            dropped = len(resampled) - len(values)
            interval = percentile_interval(values, confidence)
        status = "known"
        why = ""
        if point is None:
            status, why = "unknown", "its denominator is empty"
        elif lower is not None and upper is not None and upper - lower > exact(limit):
            status, why = "unknown", f"unknown labels leave a band wider than {limit}"
        metrics[metric] = {
            "value": _rounded(point),
            "label_bounds": [_rounded(lower), _rounded(upper)],
            "interval": None if interval is None else [_rounded(v) for v in interval],
            "resamples_without_value": dropped,
            "status": status,
            "why_unknown": why,
        }

    def critical(attr: str) -> list[dict]:
        return [
            {"case": x.run.case.id, "defect": did, "belongs_to": x.run.belongs_to(did)}
            for x in scores
            for did, sev in getattr(x, attr)
            if sev == "critical"
        ]

    escaped, possible = critical("escaped"), critical("possible_escapes")
    unrecorded = sorted(
        {x.run.id for x in scores for _, sev in x.possible_escapes if sev == "critical"}
    )
    escapes: dict = {"value": len(escaped), "defects": escaped, "possible": possible}
    if possible:
        escapes.update(
            value=None,
            known_part=len(escaped),
            why_unknown=f"the controller's record does not settle the outcome of run(s) "
            f"{unrecorded}: no record of the certificate of the round each ended on, or a "
            "run that went on past its record, so whether a candidate was certified is "
            "unknown",
        )
    successes = sum(1 for x in scores if x.success)
    open_successes = sorted(x.run.id for x in scores if x.success is None)
    cost = _usage(scores, "cost_usd")
    per_success: dict = {"provenance": cost["provenance"], "validated_successes": successes}
    if open_successes:
        per_success.update(
            value=None,
            why_unknown=f"whether run(s) {open_successes} are validated successes is unknown: "
            "the controller's record does not settle their outcome",
        )
    elif successes == 0:
        per_success.update(value=None, why_unknown="no validated success")
    elif cost["value"] is None:
        per_success.update(
            value=None, why_unknown=f"the total cost is unknown: {cost['why_unknown']}"
        )
    else:
        per_success.update(value=_rounded(exact_sum(scores, "cost_usd") / successes))
    metrics["escaped_critical_defects"] = escapes
    metrics["cost_per_validated_success"] = per_success
    metrics["total_cost_usd"] = cost
    metrics["launch_wall_clock_seconds"] = _usage(scores, "wall_clock_seconds")
    metrics["context_tokens"] = _usage(scores, "context_tokens")
    return metrics


def _run_row(x: RunScore) -> dict:
    run = x.run
    resolutions: dict[str, int] = {}
    for fix in run.fixes:
        for kind in fix.resolutions.values():
            resolutions[kind] = resolutions.get(kind, 0) + 1
    classifications: dict[str, int] = {}
    for review in run.reviews:
        for f in review.findings:
            classifications[f.classification] = classifications.get(f.classification, 0) + 1
    return {
        "run": run.id,
        "case": run.case.id,
        "kind": run.case.kind,
        "review_rounds": len(run.reviews),
        "profiles": [review_profile_name(review.round) for review in run.reviews],
        "fix_rounds": len(run.fixes),
        "fix_commits": sum(1 for fix in run.fixes if fix.commit is not None),
        "ended": {CERTIFIED: "certified", NOT_CERTIFIED: "stopped"}.get(x.certification, "unknown"),
        "stopped": run.stopped or None,
        "validated_success": x.success,
        "findings": {
            "confirmed": x.confirmed,
            "false": x.false,
            "unknown": x.unknown,
            "duplicates": x.duplicates,
        },
        "clean_negative": x.cn_outcome or None,
        "fixes": {"harmful": x.harmful, "not_harmful": x.not_harmful, "unknown": x.harm_unknown},
        "escaped": [
            {"defect": did, "severity": sev, "belongs_to": run.belongs_to(did)}
            for did, sev in x.escaped
        ],
        "possible_escapes": [
            {"defect": did, "severity": sev, "belongs_to": run.belongs_to(did)}
            for did, sev in x.possible_escapes
        ],
        "missing_launches": list(run.missing_launches()),
        "late_defects": sorted(d.id for d in run.defects.values() if not d.registered),
        # What the agents said, shown as claims: no metric reads any of them.
        "claims": {
            "reviewer_classifications": dict(sorted(classifications.items())),
            "final_reply_clean": run.reviews[-1].clean,
            "fixer_resolutions": dict(sorted(resolutions.items())),
        },
    }


def _scores_by_arm(ev: Evaluation) -> dict[str, list[RunScore]]:
    out: dict[str, list[RunScore]] = {}
    for run in ev.runs:
        if not run.excluded:
            out.setdefault(run.arm, []).append(score_run(run, ev.preregistration))
    for scores in out.values():
        scores.sort(key=lambda x: x.run.case.id)
    return out


def report(ev: Evaluation) -> dict:
    """The deterministic report of every arm: same files, same bytes."""
    prereg = ev.preregistration
    arms: dict[str, dict] = {}
    for arm, scores in sorted(_scores_by_arm(ev).items()):
        indices = bootstrap_indices(len(scores), prereg)
        provenances = sorted({x.run.provenance for x in scores})
        rows = [_run_row(x) for x in scores]
        planned = sorted(ev.cases)
        held = {x.run.case.id for x in scores}
        arms[arm] = {
            "provenance": provenances[0] if len(provenances) == 1 else "mixed",
            "cases": len(scores),
            "missing_cases": [c for c in planned if c not in held],
            "excluded": [
                {"run": r.id, "case": r.case.id, "reason": r.excluded}
                for r in sorted(ev.runs, key=lambda r: r.id)
                if r.arm == arm and r.excluded
            ],
            "metrics": arm_metrics(scores, prereg, indices),
            "loops": {
                "mean_review_rounds": _rounded(sum(r["review_rounds"] for r in rows) / len(rows)),
                "mean_fix_rounds": _rounded(sum(r["fix_rounds"] for r in rows) / len(rows)),
                "rounds_by_profile": _count(p for r in rows for p in r["profiles"]),
                "ended": _count(r["ended"] for r in rows),
            },
            "runs": rows,
        }
    any_fixture = any(a["provenance"] != "real" for a in arms.values())
    return {
        "schema": REPORT_SCHEMA,
        "not_evidence": NOT_EVIDENCE if any_fixture else "",
        "digests": dict(sorted(ev.digests.items())),
        "arms": arms,
    }


def _count(items: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return dict(sorted(out.items()))


def dumps(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


# -- the pre-registered decision ----------------------------------------------------------

NOT_EVALUATED = "NOT_EVALUATED"
NO_GO = "NO_GO"
INCONCLUSIVE = "INCONCLUSIVE"
GO = "GO"


def _paired(
    base: Sequence[RunScore],
    treat: Sequence[RunScore],
    metric: str,
    indices: list[list[int]],
    confidence: float,
) -> tuple[Fraction, Fraction] | None:
    """The paired interval of treatment minus baseline, its ends exact."""
    b = _resampled(_terms(base, metric), indices, exactly=True)
    t = _resampled(_terms(treat, metric), indices, exactly=True)
    values = [tv - bv for bv, tv in zip(b, t, strict=True) if bv is not None and tv is not None]
    return percentile_interval(values, confidence)


def _margin(
    metric: str,
    margin: Mapping,
    base: Sequence[RunScore],
    treat: Sequence[RunScore],
    metrics: tuple[Mapping, Mapping],
    indices: list[list[int]],
    confidence: float,
) -> tuple[str, str]:
    """(harm, open): why ``metric`` is shown worse than its margin, or not shown within it.

    Every comparison is exact, on unrounded values: the cost per validated success
    as the decimals the launches wrote over the count of successes, the margin as the
    decimal registered, and a rate's interval ends as the ratios they are.
    """
    b, t = metrics[0][metric], metrics[1][metric]
    bound = exact(margin["margin"])
    if metric == "cost_per_validated_success":
        if b["provenance"] != "measured" or t["provenance"] != "measured":
            return "", f"{metric} is not measured on both arms"
        if b["value"] is None or t["value"] is None:
            return "", f"{metric} has no relative change to judge"
        before = exact_sum(base, "cost_usd") / b["validated_successes"]
        after = exact_sum(treat, "cost_usd") / t["validated_successes"]
        if before == 0:
            return "", f"{metric} has no relative change to judge"
        if after > before * (1 + bound):
            return f"{metric} rose past +{margin['margin']:.0%} of the baseline's", ""
        return "", ""
    if "unknown" in (b["status"], t["status"]):
        return "", f"{metric} is unknown on an arm"
    interval = _paired(base, treat, metric, indices, confidence)
    if interval is None:
        return "", f"{metric} has no paired interval"
    # The change in the metric's good direction: its worst and best ends.
    worst, best = (
        interval if margin["direction"] == "higher_is_better" else (-interval[1], -interval[0])
    )
    if best < -bound:
        return f"{metric}'s whole interval is worse than its margin {margin['margin']}", ""
    if worst < -bound:
        return "", f"{metric}'s interval reaches past its margin {margin['margin']}"
    return "", ""


def _point(scores: Sequence[RunScore], metric: str) -> Fraction:
    """``metric``'s point estimate exactly, on an arm where it is known."""
    point = _bands(scores)[metric][0]
    assert point is not None
    return point


def escape_identity(run: Run, defect: str) -> tuple[str, str, str]:
    """Which defect an escape is: one of the case's, the same in every arm's run of the
    case, or one the run introduced, which is that run's alone whatever its id."""
    if run.belongs_to(defect) == "case":
        return ("case", run.case.id, defect)
    return ("run", run.id, defect)


def decide(ev: Evaluation, baseline: str, treatment: str) -> dict:
    """The pre-registered decision for ``treatment`` against ``baseline``, with its reasons.

    In this order: ``NOT_EVALUATED`` when the comparison has nothing real to stand
    on; ``NO_GO`` on measured harm, whatever else is missing; ``INCONCLUSIVE`` on an
    incomplete sample; ``NO_GO`` when the primary is shown short of its effect;
    ``INCONCLUSIVE`` while anything is left open; ``GO``.
    """
    prereg = ev.preregistration
    rule = prereg["decision"]
    by_arm = {arm: [r for r in ev.runs if r.arm == arm] for arm in (baseline, treatment)}
    absent = [f"arm {arm!r} has no run" for arm, runs in by_arm.items() if not runs]
    if absent:
        return {"decision": NOT_EVALUATED, "reasons": absent}
    fixture = sorted({r.arm for runs in by_arm.values() for r in runs if r.provenance != "real"})
    if fixture:
        return {
            "decision": NOT_EVALUATED,
            "reasons": [f"arm {arm!r} holds fixture runs: {NOT_EVIDENCE}" for arm in fixture],
        }
    excluded = sorted({r.case.id for runs in by_arm.values() for r in runs if r.excluded})
    held = {arm: {r.case.id: r for r in runs if not r.excluded} for arm, runs in by_arm.items()}
    paired = sorted(set(held[baseline]) & set(held[treatment]))
    base = [score_run(held[baseline][c], prereg) for c in paired]
    treat = [score_run(held[treatment][c], prereg) for c in paired]
    indices = bootstrap_indices(len(paired), prereg) if paired else []
    confidence = prereg["uncertainty"]["confidence"]

    # Measured harm first: the gate reads every treatment run, paired or not. An
    # escape is at a candidate the controller certified; where its record does not
    # settle that, an escape is possible and decides nothing either way.
    harm: list[str] = []
    open_: list[str] = []
    for case_id, run in sorted(held[treatment].items()):
        counterpart = held[baseline].get(case_id)
        escaped_before: set[tuple[str, str, str]] = set()
        possible_before: set[tuple[str, str, str]] = set()
        if counterpart is not None:
            before = score_run(counterpart, prereg)
            escaped_before = {escape_identity(counterpart, did) for did, _ in before.escaped}
            possible_before = {
                escape_identity(counterpart, did) for did, _ in before.possible_escapes
            }
        after = score_run(run, prereg)
        for did, severity in after.escaped:
            identity = escape_identity(run, did)
            if severity != "critical" or identity in escaped_before:
                continue
            what = (
                f"critical defect {did} of case {case_id!r}"
                if run.belongs_to(did) == "case"
                else f"critical defect {did} that treatment run {run.id!r} introduced"
            )
            if identity in possible_before:
                open_.append(
                    f"{what} escaped the treatment's review, and whether it escaped the "
                    "baseline's is unknown"
                )
            elif run.belongs_to(did) == "case":
                harm.append(f"{what} escaped the treatment's review")
            else:
                harm.append(f"{what} escaped its review")
        for did, severity in after.possible_escapes:
            if severity != "critical" or escape_identity(run, did) in escaped_before:
                continue
            open_.append(
                f"critical defect {did} of run {run.id!r} may have escaped the treatment's "
                "review: the controller's record does not settle the run's outcome"
            )
    if paired:
        metrics = (arm_metrics(base, prereg, None), arm_metrics(treat, prereg, None))
        for metric, margin in rule["margins"].items():
            shown, left = _margin(metric, margin, base, treat, metrics, indices, confidence)
            if shown:
                harm.append(shown)
            if left:
                open_.append(left)
    if harm:
        return {"decision": NO_GO, "reasons": harm}

    incomplete: list[str] = []
    missing = [c for c in sorted(ev.cases) if c not in paired and c not in excluded]
    if missing:
        incomplete.append(f"case(s) {missing} have no pair of runs")
    if len(excluded) > rule["max_excluded_case_share"] * len(ev.cases):
        incomplete.append(f"{len(excluded)} excluded case(s) pass the registered share")
    defect_cases = sum(1 for c in paired if ev.cases[c].kind == "defect")
    negatives = len(paired) - defect_cases
    minimum = prereg["minimum_sample"]
    if defect_cases < minimum["defect_cases"] or negatives < minimum["clean_negative_cases"]:
        incomplete.append(
            f"the sample pairs {defect_cases} defect case(s) and {negatives} clean negative(s), "
            f"below the registered {minimum['defect_cases']} and {minimum['clean_negative_cases']}"
        )
    if incomplete:
        return {"decision": INCONCLUSIVE, "reasons": incomplete + open_}

    primary = rule["primary"]
    metric, effect = primary["metric"], primary["min_difference"]
    b, t = metrics[0][metric], metrics[1][metric]
    if "unknown" in (b["status"], t["status"]):
        open_.append(f"{metric} is unknown on an arm")
    else:
        interval = _paired(base, treat, metric, indices, confidence)
        if interval is not None and interval[1] < exact(effect):
            return {
                "decision": NO_GO,
                "reasons": [f"{metric}'s interval lies wholly below +{effect}"],
            }
        gain = _point(treat, metric) - _point(base, metric)
        if interval is None or gain < exact(effect) or interval[0] <= 0:
            open_.append(f"{metric} does not show +{effect} with its interval above 0")
    if open_:
        return {"decision": INCONCLUSIVE, "reasons": open_}
    return {"decision": GO, "reasons": [f"{metric} rose by +{effect} or more and every bound held"]}


# -- command line -------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.review_evaluation", description=__doc__)
    parser.add_argument("--preregistration", type=Path, default=PREREGISTRATION)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--runs", type=Path, default=FIXTURE_RUNS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    replay = sub.add_parser("verify")
    trust = replay.add_mutually_exclusive_group()
    trust.add_argument(
        "--trust-oracles",
        action="store_true",
        help="run the oracles of files other than the registered ones; they run as you, "
        "with your files and network, and nothing confines them",
    )
    trust.add_argument(
        "--pins-only",
        action="store_true",
        help="build the commits and hold them to their pins, running no oracle",
    )
    score = sub.add_parser("score")
    score.add_argument("--out", type=Path)
    dec = sub.add_parser("decide")
    dec.add_argument("--baseline", required=True)
    dec.add_argument("--treatment", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            ev = load(args.preregistration, args.corpus, args.runs)
            if not (args.pins_only or args.trust_oracles or registered_inputs(ev)):
                print(
                    "refused: these are not the registered corpus and fixture runs, and an "
                    "oracle is code that runs as you, with your files and network: pass "
                    "--trust-oracles to run it, or --pins-only to run nothing they hold",
                    file=sys.stderr,
                )
                return 2
            with tempfile.TemporaryDirectory(prefix="review-eval-") as root:
                problems = verify(ev, Path(root), oracles=not args.pins_only)
            for problem in problems:
                print(problem, file=sys.stderr)
            if not problems:
                ran = "the pins alone, no oracle" if args.pins_only else "pins and oracles"
                print(f"verified {len(ev.runs)} run(s) over {len(ev.cases)} case(s): {ran}")
            return 1 if problems else 0
        ev = load(args.preregistration, args.corpus, args.runs)
    except (ReviewEvaluationError, json.JSONDecodeError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.command == "check":
        print(f"checked {len(ev.runs)} run(s) over {len(ev.cases)} case(s)")
        return 0
    if args.command == "score":
        text = dumps(report(ev))
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0
    sys.stdout.write(dumps(decide(ev, args.baseline, args.treatment)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
