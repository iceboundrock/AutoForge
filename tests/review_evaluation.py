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
to what it builds, and runs every oracle where the ground truth says it must fail
or pass), ``score`` prints the report, and ``decide`` applies the pre-registered
decision to two arms.

It launches no agent, reads no credential, contacts no network and writes to no
GitHub. A label comes from an adjudicator's record or an oracle, never from what
an agent said of its own work: the reviewer's classification, its prose and the
fixer's resolution kinds are reported as claims and read by no metric.
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
from pathlib import Path

from autoforge.errors import AutoForgeError
from autoforge.executor import select_environment
from autoforge.git_transport import NO_AUTO_MAINTENANCE
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
VALIDATION_STATUSES = ("passed", "failed", "absent", "not_configured")
LAUNCH_PHASES = ("ANALYZE_EXECUTE", "REVIEW", "FIX", "REPLAN_REEXECUTE")
# The metrics a margin or the primary may name, in the order a report shows them.
RATE_METRICS = (
    "weighted_recall",
    "precision",
    "clean_negative_fpr",
    "harmful_fix_rate",
    "duplicate_ratio",
)
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
    clean: bool
    validation: str
    findings: tuple[Finding, ...]


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
    cost_usd: float | None
    wall_clock_seconds: float | None
    context_tokens: float | None
    provenance: str


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
    stopped: str
    excluded: str
    oracles: Mapping[str, str]

    def present(self, defect: Defect, commit: str) -> bool:
        return _present(self.commits, defect, commit)

    def belongs_to(self, defect: str) -> str:
        """``case`` for a defect of the case's commits, ``run`` for one this run introduced."""
        return "case" if defect in self.case.defects else "run"

    def missing_launches(self) -> tuple[str, ...]:
        """The launches a complete record of this run holds and its own does not.

        A run made at least one ANALYZE_EXECUTE launch, one REVIEW launch for each of
        its rounds and one FIX launch for each of its fixes. Failed attempts add
        launches and are never missing; a record without one of these is incomplete,
        so no sum over its launches is the run's.
        """
        held = {(launch.phase, launch.round) for launch in self.launches}
        missing: list[str] = []
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


def check_preregistration(prereg: object) -> dict:
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
    if tuple(p["label_verdicts"]) != ("confirmed", "false", "unknown"):
        raise ReviewEvaluationError(
            "preregistration label_verdicts must be confirmed/false/unknown"
        )
    weights = p["severity_weights"]
    if not isinstance(weights, dict) or not weights:
        raise ReviewEvaluationError("preregistration severity_weights must be an object")
    for severity, weight in weights.items():
        _int(weight, f"preregistration severity_weights.{severity}", 1)
    if not set(p["success_blocking_severities"]) <= set(weights):
        raise ReviewEvaluationError("a success-blocking severity is not a registered severity")
    if set(p["adjudicator_roles"]) & set(p["refused_adjudicator_roles"]):
        raise ReviewEvaluationError("an adjudicator role is both allowed and refused")
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
    d = _obj(
        p["decision"],
        "preregistration decision",
        ("primary", "margins", "hard_gates", "max_excluded_case_share"),
    )
    primary = _obj(d["primary"], "preregistration decision.primary", ("metric", "min_difference"))
    _one_of(primary["metric"], RATE_METRICS, "preregistration decision.primary.metric")
    for metric, margin in d["margins"].items():
        _one_of(metric, (*RATE_METRICS, "cost_per_validated_success"), "a margin's metric")
        _obj(margin, f"preregistration margin {metric}", ("direction", "margin"))
        _one_of(margin["direction"], ("higher_is_better", "lower_is_better"), f"{metric} direction")
    if d["hard_gates"] != ["no_new_escaped_critical_defect"]:
        raise ReviewEvaluationError("preregistration hard_gates is not one this scorer has")
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
        if kind == "clean_negative" and defects:
            raise ReviewEvaluationError(
                f"{at} is a clean negative with defects: it is a defect case, and only an "
                "amendment moves it"
            )
        if kind == "defect" and not defects:
            raise ReviewEvaluationError(f"{at} is a defect case with no defect")
        cases[cid] = Case(
            id=cid,
            kind=kind,
            commits=commits,
            candidate=candidate,
            defects=defects,
            oracles=oracles,
        )
    return cases, cutoff, commit_date


def _review(
    raw: object,
    at: str,
    commits: Mapping[str, Commit],
) -> tuple[int, str, ReviewResult, str]:
    r = _obj(raw, at, ("round", "commit", "validation", "result"))
    rnd = _int(r["round"], f"{at}.round", 1)
    commit = r["commit"]
    if commit not in commits or commits[commit].parent is None:
        raise ReviewEvaluationError(f"{at}.commit {commit!r} is no reviewable commit of this run")
    validation = _one_of(r["validation"], VALIDATION_STATUSES, f"{at}.validation")
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
    return rnd, commit, result, validation


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
        ("phase", "cost_usd", "wall_clock_seconds", "context_tokens", "provenance"),
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
        rnd, commit, result, validation = _review(item, where, commits)
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
            validation=validation,
            findings=tuple(findings),
        )
        reviews.append(review)
        last = i == len(r["reviews"]) - 1
        fix_raw = fixes_by_round.pop(rnd, None)
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
    if reviews[-1].clean and stopped:
        raise ReviewEvaluationError(f"{at} ended on a clean round and says it was stopped")
    if not reviews[-1].clean:
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
        stopped=stopped,
        excluded=excluded,
        oracles=oracles,
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
    ev = check(read_json(preregistration), read_json(corpus), read_json(runs))
    return Evaluation(
        preregistration=ev.preregistration,
        cases=ev.cases,
        runs=ev.runs,
        commit_date=ev.commit_date,
        digests={
            "preregistration": file_digest(preregistration),
            "corpus": file_digest(corpus),
            "runs": file_digest(runs),
        },
    )


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


def build_shas(commits: Mapping[str, Commit], date: str, label: str, root: Path) -> dict[str, str]:
    """The SHA each commit builds to, made in ``root`` at the fixed ``date``."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, date, "init", "-q")
    built: dict[str, str] = {}
    for commit in commits.values():
        if commit.parent is None:
            _git(repo, date, "checkout", "-q", "--orphan", f"line-{len(built)}")
        else:
            _git(repo, date, "checkout", "-q", "--detach", built[commit.parent])
        _git(repo, date, "rm", "-rq", "--ignore-unmatch", ".")
        for relpath, text in tree_at(commits, commit.name).items():
            path = repo / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        _git(repo, date, "add", "-A")
        _git(repo, date, "commit", "-q", "--allow-empty", "-m", f"{label}: {commit.name}")
        built[commit.name] = _git(repo, date, "rev-parse", "HEAD")
    return built


def run_oracle(source: str, files: Mapping[str, str], root: Path) -> str:
    """``pass``, ``fail`` (an ``AssertionError``) or ``error: <last line>``, run isolated."""
    tree = Path(tempfile.mkdtemp(prefix="tree-", dir=root))
    for relpath, text in files.items():
        path = tree / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
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
    out: list[tuple[str, str, str]] = []
    for d in defects:
        if d.oracle is None:
            continue
        expected: dict[str, str] = {d.introduced_at: "fail"}
        for name in (*d.passes_at, *_fixed_at(commits, d)):
            expected[name] = "pass"
        for name in sorted(decided):
            if d.introduced_at in _ancestors(commits, name):
                expected[name] = "fail" if _present(commits, d, name) else "pass"
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


def _pins(owner: str, commits: Mapping[str, Commit], built: Mapping[str, str]) -> list[str]:
    return [
        f"{owner}: commit {name!r} is pinned at {commit.sha} and builds as {built[name]}"
        for name, commit in commits.items()
        if built[name] != commit.sha
    ]


def verify(ev: Evaluation, root: Path) -> list[str]:
    """Replay every case of the corpus, then every run, under ``root``; the problems found.

    Each case is replayed on its own, whether or not a run holds it: its commits are
    held to their pins and its oracles run at its commits. A run adds what only it
    holds: its own commits and the oracles at them. An empty list of runs still
    replays the whole corpus.
    """
    problems: list[str] = []
    for index, case in enumerate(ev.cases.values()):
        where = Path(tempfile.mkdtemp(prefix=f"case-{index}-", dir=root))
        owner = f"case {case.id!r}"
        problems += _pins(
            owner, case.commits, build_shas(case.commits, ev.commit_date, case.id, where)
        )
        for did, name, outcome in case_oracle_expectations(case):
            source = case.oracles[str(case.defects[did].oracle)]
            got = run_oracle(source, tree_at(case.commits, name), where)
            if got != outcome:
                problems.append(
                    f"{owner}: {did}'s oracle should {outcome} at {name!r} and gave {got}"
                )
    for index, run in enumerate(ev.runs):
        owner = f"run {run.id!r}"
        replayed = {(did, name) for did, name, _ in case_oracle_expectations(run.case)}
        own = {name: c for name, c in run.commits.items() if name not in run.case.commits}
        expectations = [e for e in oracle_expectations(run) if (e[0], e[1]) not in replayed]
        if not own and not expectations:
            continue
        where = Path(tempfile.mkdtemp(prefix=f"run-{index}-", dir=root))
        if own:
            problems += _pins(
                owner, own, build_shas(run.commits, ev.commit_date, run.case.id, where)
            )
        for did, name, outcome in expectations:
            source = run.oracles[str(run.defects[did].oracle)]
            got = run_oracle(source, tree_at(run.commits, name), where)
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
    # The unknown findings of each round with two or more and no confirmed one: all but
    # one of them could be duplicates of the first.
    unknown_groups: tuple[int, ...]
    cn_outcome: str  # "", "true_negative", "false_positive", "unknown"
    harmful: int
    not_harmful: int
    harm_unknown: int
    escaped: tuple[tuple[str, str], ...]
    success: bool


def score_run(run: Run, prereg: Mapping) -> RunScore:
    registered = [d for d in run.defects.values() if d.registered]
    reviewed = [review.commit for review in run.reviews]
    exposed = [d for d in registered if any(run.present(d, c) for c in reviewed)]
    detected = {
        f.defect for review in run.reviews for f in review.findings if f.verdict == "confirmed"
    }
    uncertain_commits = {
        review.commit
        for review in run.reviews
        if any(f.verdict == "unknown" for f in review.findings)
    }
    hit = sum(d.weight for d in exposed if d.id in detected)
    weight = sum(d.weight for d in exposed)
    upper = hit + sum(
        d.weight
        for d in exposed
        if d.id not in detected and any(run.present(d, c) for c in uncertain_commits)
    )
    verdicts = [f.verdict for review in run.reviews for f in review.findings]
    duplicates = 0
    unknown_beside_confirmed = 0
    unknown_groups: list[int] = []
    for review in run.reviews:
        named = [f.defect for f in review.findings if f.verdict == "confirmed"]
        duplicates += len(named) - len(set(named))
        undecided = sum(1 for f in review.findings if f.verdict == "unknown")
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
    escaped: tuple[tuple[str, str], ...] = ()
    if final.clean:
        escaped = tuple(
            sorted((d.id, d.severity) for d in registered if run.present(d, final.commit))
        )
    blocking = set(prereg["success_blocking_severities"])
    success = (
        final.clean
        and final.validation == "passed"
        and not any(severity in blocking for _, severity in escaped)
    )
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
        cn_outcome=cn_outcome,
        harmful=fix_verdicts.count("harmful"),
        not_harmful=fix_verdicts.count("not_harmful"),
        harm_unknown=fix_verdicts.count("unknown"),
        escaped=escaped,
        success=success,
    )


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _duplicate_upper(scores: Sequence[RunScore]) -> float | None:
    """The highest duplicate ratio any settlement of the unknown labels allows.

    A duplicate is a later confirmed finding of a round naming the defect an earlier
    one of that round names. An unknown finding beside a confirmed one could be a
    duplicate of it, which adds one to both counts and so always raises the ratio
    (it is below 1). A round of ``k`` unknown findings and no confirmed one could
    hold ``k - 1`` duplicates of ``k`` confirmed findings: such rounds are taken,
    the highest ``(k - 1) / k`` first, while each raises the ratio, which is where
    the maximum of a ratio of sums lies. Every other unknown finding is put false.
    """
    dup = sum(x.duplicates + x.unknown_beside_confirmed for x in scores)
    confirmed = sum(x.confirmed + x.unknown_beside_confirmed for x in scores)
    for k in sorted((k for x in scores for k in x.unknown_groups), reverse=True):
        if confirmed and (k - 1) / k <= dup / confirmed:
            break
        dup, confirmed = dup + k - 1, confirmed + k
    if confirmed == 0 and any(x.unknown for x in scores):
        return 0.0  # a single unknown finding confirmed alone: no duplicate of one finding
    return _ratio(dup, confirmed)


def _bands(
    scores: Sequence[RunScore],
) -> dict[str, tuple[float | None, float | None, float | None]]:
    """Each rate metric's (point, lower, upper): the bounds put every unknown label each way."""
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
        # Lower: every unknown finding confirmed of a defect no other finding of its
        # round names, which adds to the confirmed count alone.
        "duplicate_ratio": (
            _ratio(s["duplicates"], s["confirmed"]),
            _ratio(s["duplicates"], labeled - s["false"]),
            _duplicate_upper(scores),
        ),
    }


def _terms(scores: Sequence[RunScore], metric: str) -> tuple[list[float], list[float]]:
    """Each run's numerator and denominator of ``metric``'s point estimate, in order."""
    pick: dict[str, Callable[[RunScore], tuple[float, float]]] = {
        "weighted_recall": lambda x: (x.recall_hit, x.recall_weight),
        "precision": lambda x: (x.confirmed, x.confirmed + x.false),
        "clean_negative_fpr": lambda x: (
            float(x.cn_outcome == "false_positive"),
            float(x.cn_outcome in ("false_positive", "true_negative")),
        ),
        "harmful_fix_rate": lambda x: (x.harmful, x.harmful + x.not_harmful),
        "duplicate_ratio": lambda x: (x.duplicates, x.confirmed),
    }
    pairs = [pick[metric](x) for x in scores]
    return [float(n) for n, _ in pairs], [float(d) for _, d in pairs]


def _resampled(
    terms: tuple[list[float], list[float]], indices: list[list[int]]
) -> list[float | None]:
    """The pooled point estimate on every resample: ``None`` where its denominator is empty."""
    nums, dens = terms
    out: list[float | None] = []
    for sample in indices:
        den = sum(map(dens.__getitem__, sample))
        out.append(sum(map(nums.__getitem__, sample)) / den if den else None)
    return out


def bootstrap_indices(n: int, prereg: Mapping) -> list[list[int]]:
    """The resamples of ``n`` cases every interval of one arm, or one pair, reads."""
    u = prereg["uncertainty"]
    rng = random.Random(u["seed"])
    return [[rng.randrange(n) for _ in range(n)] for _ in range(u["resamples"])]


def percentile_interval(values: list[float], confidence: float) -> tuple[float, float] | None:
    """Nearest-rank percentile interval of ``values``, or ``None`` with none to rank."""
    if not values:
        return None
    ordered = sorted(values)
    tail = (1 - confidence) / 2
    last = len(ordered) - 1
    return ordered[math.floor(tail * last)], ordered[math.ceil((1 - tail) * last)]


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _usage(scores: Sequence[RunScore], attr: str) -> dict:
    """The sum of ``attr`` over every launch of every run, or ``None`` with why.

    The sum is unknown when a launch's value or provenance is, and when a run's
    launch record is incomplete (:meth:`Run.missing_launches`): a launch nobody
    recorded is no launch that cost nothing. The known part is shown apart.
    """
    values = [getattr(launch, attr) for x in scores for launch in x.run.launches]
    provenances = {launch.provenance for x in scores for launch in x.run.launches}
    known = sum((v for v in values if v is not None), 0.0)
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


def arm_metrics(scores: Sequence[RunScore], prereg: Mapping, indices: list[list[int]]) -> dict:
    limit = prereg["max_label_uncertainty"]
    confidence = prereg["uncertainty"]["confidence"]
    bands = _bands(scores)
    metrics: dict[str, dict] = {}
    for metric in RATE_METRICS:
        point, lower, upper = bands[metric]
        resampled = _resampled(_terms(scores, metric), indices)
        values = [v for v in resampled if v is not None]
        dropped = len(resampled) - len(values)
        interval = percentile_interval(values, confidence)
        status = "known"
        why = ""
        if point is None:
            status, why = "unknown", "its denominator is empty"
        elif lower is not None and upper is not None and upper - lower > limit:
            status, why = "unknown", f"unknown labels leave a band wider than {limit}"
        metrics[metric] = {
            "value": _rounded(point),
            "label_bounds": [_rounded(lower), _rounded(upper)],
            "interval": None if interval is None else [_rounded(v) for v in interval],
            "resamples_without_value": dropped,
            "status": status,
            "why_unknown": why,
        }
    critical = [
        {"case": x.run.case.id, "defect": did, "belongs_to": x.run.belongs_to(did)}
        for x in scores
        for did, sev in x.escaped
        if sev == "critical"
    ]
    successes = sum(1 for x in scores if x.success)
    cost = _usage(scores, "cost_usd")
    per_success: dict = {"provenance": cost["provenance"], "validated_successes": successes}
    if successes == 0:
        per_success.update(value=None, why_unknown="no validated success")
    elif cost["value"] is None:
        per_success.update(
            value=None, why_unknown=f"the total cost is unknown: {cost['why_unknown']}"
        )
    else:
        per_success.update(value=_rounded(cost["value"] / successes))
    metrics["escaped_critical_defects"] = {"value": len(critical), "defects": critical}
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
        "ended": "clean" if run.reviews[-1].clean else "stopped",
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
        "missing_launches": list(run.missing_launches()),
        "late_defects": sorted(d.id for d in run.defects.values() if not d.registered),
        # What the agents said, shown as claims: no metric reads either.
        "claims": {
            "reviewer_classifications": dict(sorted(classifications.items())),
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
) -> tuple[float, float] | None:
    b = _resampled(_terms(base, metric), indices)
    t = _resampled(_terms(treat, metric), indices)
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
    """(harm, open): why ``metric`` is shown worse than its margin, or not shown within it."""
    b, t = metrics[0][metric], metrics[1][metric]
    bound = margin["margin"]
    if metric == "cost_per_validated_success":
        if b["provenance"] != "measured" or t["provenance"] != "measured":
            return "", f"{metric} is not measured on both arms"
        if b["value"] is None or t["value"] is None or b["value"] == 0:
            return "", f"{metric} has no relative change to judge"
        if t["value"] > b["value"] * (1 + bound):
            return f"{metric} rose past +{bound:.0%} of the baseline's", ""
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
        return f"{metric}'s whole interval is worse than its margin {bound}", ""
    if worst < -bound:
        return "", f"{metric}'s interval reaches past its margin {bound}"
    return "", ""


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

    # Measured harm first: the gate reads every treatment run, paired or not.
    harm: list[str] = []
    open_: list[str] = []
    for case_id, run in sorted(held[treatment].items()):
        counterpart = held[baseline].get(case_id)
        before = (
            {escape_identity(counterpart, did) for did, _ in score_run(counterpart, prereg).escaped}
            if counterpart
            else set()
        )
        for did, severity in score_run(run, prereg).escaped:
            if severity != "critical" or escape_identity(run, did) in before:
                continue
            if run.belongs_to(did) == "case":
                harm.append(
                    f"critical defect {did} of case {case_id!r} escaped the treatment's review"
                )
            else:
                harm.append(
                    f"critical defect {did} that treatment run {run.id!r} introduced escaped "
                    "its review"
                )
    if paired:
        metrics = (arm_metrics(base, prereg, indices), arm_metrics(treat, prereg, indices))
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
        if interval is not None and interval[1] < effect:
            return {
                "decision": NO_GO,
                "reasons": [f"{metric}'s interval lies wholly below +{effect}"],
            }
        if interval is None or t["value"] - b["value"] < effect or interval[0] <= 0:
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
    sub.add_parser("verify")
    score = sub.add_parser("score")
    score.add_argument("--out", type=Path)
    dec = sub.add_parser("decide")
    dec.add_argument("--baseline", required=True)
    dec.add_argument("--treatment", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            ev = load(args.preregistration, args.corpus, args.runs)
            with tempfile.TemporaryDirectory(prefix="review-eval-") as root:
                problems = verify(ev, Path(root))
            for problem in problems:
                print(problem, file=sys.stderr)
            if not problems:
                print(f"verified {len(ev.runs)} run(s) over {len(ev.cases)} case(s)")
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
