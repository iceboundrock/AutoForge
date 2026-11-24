"""Data-only replay and evidence admission for the REV-R0 study."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, NamedTuple

PHASES = ("ANALYZE_EXECUTE", "REVIEW", "FIX", "REPLAN_REEXECUTE")
ARMS = ("baseline", "treatment")
WEIGHTS = {"critical": 8, "major": 4, "minor": 2, "informational": 1}
CLASSES = ("correctness", "safety", "recovery", "evidence", "compatibility", "other")
Json = dict[str, Any]
DefectId = tuple[str | None, str]  # None is shared within this case; otherwise the run ID.


class InvalidEvidence(ValueError):
    """The evidence contradicts the registered record contract."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise InvalidEvidence(message)


def shape(value: Any, required: str, optional: str = "") -> Json:
    keys = set(required.split())
    require(
        isinstance(value, dict) and keys <= value.keys() <= keys | set(optional.split()),
        f"expected fields: {required}; optional: {optional or '(none)'}",
    )
    return value


def items(value: Any) -> list:
    require(isinstance(value, list), "expected a list")
    return value


def text(value: Any) -> str:
    require(isinstance(value, str) and value.strip(), "expected nonempty text")
    return value


def choice(value: Any, choices: tuple | dict) -> Any:
    require(isinstance(value, str) and value in choices, f"expected one of {tuple(choices)}")
    return value


def integer(value: Any, minimum: int = 0) -> int:
    require(type(value) is int and value >= minimum, f"expected an integer >= {minimum}")
    return value


def sha(value: Any) -> str:
    require(re.fullmatch(r"[0-9a-f]{40}", text(value)), "expected a full Git SHA-1")
    return value


def timestamp(value: Any) -> datetime:
    require(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", text(value)), "expected UTC time")
    return datetime.fromisoformat(value)


def number(value: Any) -> Fraction:
    require(
        type(value) is int or (isinstance(value, Decimal) and value.is_finite()),
        "expected a finite JSON number",
    )
    require(value >= 0, "usage must not be negative")
    return Fraction(value)


def _unique(pairs: list[tuple[str, Any]]) -> Json:
    obj: Json = {}
    for key, value in pairs:
        require(key not in obj, "duplicate JSON key")
        obj[key] = value
    return obj


def load(path: Path) -> Json:
    return json.loads(
        path.read_text(encoding="utf-8"), parse_float=Decimal, object_pairs_hook=_unique
    )


def adjudication(value: Any) -> datetime:
    row = shape(value, "role adjudicator source at")
    # Not configurable and not an authentication mechanism. The custodian must
    # establish who actually adjudicated, and independence from the two arms.
    choice(row["role"], ("independent_human",))
    text(row["adjudicator"])
    text(row["source"])
    return timestamp(row["at"])


def _object_id(kind: bytes, body: bytes) -> bytes:
    return hashlib.sha1(kind + b" " + str(len(body)).encode() + b"\0" + body).digest()


def commit_id(files: Json, parent: str | None) -> str:
    """Git SHA-1 of a flat UTF-8 snapshot, without checkout, filters or execution."""
    require(isinstance(files, dict), "expected a snapshot's file map")
    entries = []
    for name, contents in sorted(files.items()):
        require(
            re.fullmatch(r"[a-zA-Z0-9_.-]+", name) and name.lower() not in {".", "..", ".git"},
            "snapshot paths must be flat, regular file names",
        )
        require(isinstance(contents, str), "snapshot contents must be UTF-8 text")
        blob = _object_id(b"blob", contents.encode("utf-8"))
        entries.append(b"100644 " + name.encode() + b"\0" + blob)
    tree = _object_id(b"tree", b"".join(entries)).hex()
    lineage = f"parent {sha(parent)}\n" if parent is not None else ""
    identity = "Review fixture <fixture@example.invalid> 1700000000 +0000"
    body = f"tree {tree}\n{lineage}author {identity}\ncommitter {identity}\n\nSnapshot\n"
    return _object_id(b"commit", body.encode()).hex()


@dataclass(frozen=True)
class Defect:
    identity: DefectId
    severity: str
    introduced: str
    path: str
    expected: str | None
    eligible: bool


@dataclass
class History:
    files: dict[str, Json]
    defects: dict[DefectId, Defect]
    present: dict[str, set[DefectId]]

    def eligible_at(self, commit: str) -> set[DefectId]:
        return {key for key in self.present[commit] if self.defects[key].eligible}


def definitions(rows: Any, owner: str | None, cutoff: datetime) -> dict[DefectId, Defect]:
    result = {}
    for row in items(rows):
        shape(row, "id severity class introduced oracle evidence")
        key = owner, text(row["id"])
        require(key not in result, "duplicate defect identity")
        choice(row["class"], CLASSES)
        oracle = shape(row["oracle"], "path expected")
        expected = oracle["expected"]
        require(expected is None or isinstance(expected, str), "invalid oracle expected bytes")
        result[key] = Defect(
            key,
            choice(row["severity"], WEIGHTS),
            sha(row["introduced"]),
            text(oracle["path"]),
            expected,
            adjudication(row["evidence"]) <= cutoff,
        )
    return result


def defect_id(row: Any, owner: str | None) -> DefectId:
    shape(row, "scope id")
    scope = choice(row["scope"], ("case", "run"))
    require(scope == "case" or owner is not None, "run defect outside a run")
    return (owner if scope == "run" else None), text(row["id"])


def extend(
    history: History,
    rows: Any,
    introduced: dict[DefectId, Defect],
    owner: str | None,
    start: str | None,
    cutoff: datetime,
) -> dict[str, tuple[str, int]]:
    """Replay one linear lineage. A removal can only act on its parent's set."""
    parent = start
    fixes = {}
    pending = set(introduced)
    by_commit: dict[str, set[DefectId]] = {}
    for key, defect in introduced.items():
        by_commit.setdefault(defect.introduced, set()).add(key)
    for row in items(rows):
        shape(row, "sha files resolves", "action label seq" if owner is not None else "")
        commit = sha(row["sha"])
        require(commit not in history.files, "repeated commit on a lineage")
        files = row["files"]
        require(commit_id(files, parent) == commit, "snapshot does not match pinned commit")
        before = history.present[parent] if parent else set()
        resolved = [defect_id(ref, owner) for ref in items(row["resolves"])]
        require(
            len(set(resolved)) == len(resolved) and set(resolved) <= before,
            "a resolution must name a defect present at the parent",
        )
        new = by_commit.get(commit, set())
        pending -= new
        after = (before - set(resolved)) | new
        history.files[commit] = files
        history.present[commit] = after
        if owner is not None:
            action = choice(row.get("action"), ("fix", "other"))
            require(not new or action == "fix", "run regressions must originate at a FIX")
            if action == "fix":
                label = label_at(row.get("label"), commit, cutoff)
                verdict = choice(label.get("verdict"), ("harmful", "not_harmful", "unknown"))
                known_regression = any(introduced[key].eligible for key in new)
                require(
                    verdict == "unknown" or (verdict == "harmful") == known_regression,
                    "FIX verdict conflicts with frozen regressions",
                )
                seq = integer(row.get("seq"), 1)
                require(not fixes or seq > next(reversed(fixes.values()))[1], "FIX launch order")
                fixes[commit] = verdict, seq
            else:
                require(not {"label", "seq"} & row.keys(), "only FIX commits bind FIX launches")
        parent = commit
    require(not pending, "defect introduction is not on its registered lineage")
    history.defects.update(introduced)
    return fixes


def replay(history: History) -> None:
    # Both sides of every predicate are checked at every snapshot: a FIX
    # cannot override the necessary failure at introduction.
    for commit, files in history.files.items():
        for key, defect in history.defects.items():
            failed = files.get(defect.path) != defect.expected
            require(failed == (key in history.present[commit]), "oracle contradicts lineage")


def label_at(row: Any, commit: str, cutoff: datetime, optional: str = "") -> Json:
    shape(row, "verdict sha evidence", optional)
    require(sha(row["sha"]) == commit, "label does not bind the observed commit")
    require(adjudication(row["evidence"]) <= cutoff, "label is after the frozen cutoff")
    return row


@dataclass(frozen=True)
class Case:
    kind: str
    risk: str
    candidate: str
    history: History


class Revision(NamedTuple):
    round: int
    commit: str
    pr: str
    base: str
    merge_base: str


@dataclass(frozen=True)
class Review:
    revision: Revision
    disposition: str
    findings: tuple[str | DefectId, ...]
    validation: str | None
    certificate: str | None
    seq: int

    @property
    def commit(self) -> str:
        return self.revision.commit


def revision(row: Any) -> Revision:
    shape(row, "round sha pr base merge_base")
    return Revision(
        integer(row["round"], 1),
        sha(row["sha"]),
        text(row["pr"]),
        text(row["base"]),
        sha(row["merge_base"]),
    )


def bound_receipt(row: Any, bound: Revision, field: str) -> tuple[str, str, str]:
    shape(row, f"revision source set policy {field}")
    require(revision(row["revision"]) == bound, "receipt does not bind the reviewed revision")
    text(row["source"])
    return text(row[field]), text(row["set"]), text(row["policy"])


def admit_reviews(rows: Any, history: History, attempts: dict[int, Json]) -> list[Review]:
    reviews = []
    rounds = set()
    for row in items(rows):
        shape(row, "revision reply disposition findings seq source", "validation certificate")
        bound = revision(row["revision"])
        require(bound.commit in history.files, "review names an unverified commit")
        lineage = list(history.files)
        require(
            bound.merge_base in lineage[: lineage.index(bound.commit)],
            "review merge base is not on the verified lineage before its candidate",
        )
        text(row["source"])
        round_key = bound.pr, bound.round
        require(round_key not in rounds, "duplicate completed PR round")
        rounds.add(round_key)
        disposition = choice(row["disposition"], ("accepted", "stale", "unknown"))
        reply = choice(row["reply"], ("clean", "needs_fix"))
        seq = integer(row["seq"], 1)
        require(not reviews or seq > reviews[-1].seq, "review launch order must advance")
        if seq in attempts:
            require(
                (attempts[seq]["phase"], attempts[seq]["round"], attempts[seq]["outcome"])
                == ("REVIEW", bound.round, "reply"),
                "launch contradicts its recorded review reply",
            )
        validation = None
        basis = None
        if row.get("validation") is not None:
            result, set_id, policy = bound_receipt(row["validation"], bound, "result")
            validation = choice(result, ("passed", "empty", "failed"))
            basis = set_id, policy
        certificate = None
        if row.get("certificate") is not None:
            certificate, set_id, policy = bound_receipt(row["certificate"], bound, "id")
            require(
                reply == "clean"
                and disposition == "accepted"
                and basis == (set_id, policy)
                and validation in ("passed", "empty"),
                "certificate conflicts with review or validation",
            )
        require(bool(items(row["findings"])) == (reply == "needs_fix"), "reply/finding mismatch")
        reviews.append(Review(bound, disposition, (), validation, certificate, seq))
    return reviews


def label_reviews(
    rows: list, reviews: list[Review], history: History, owner: str, cutoff: datetime
) -> list[Review]:
    result = []
    for row, review in zip(rows, reviews, strict=True):
        findings = []
        ids: set[str] = set()
        for finding in row["findings"]:
            shape(finding, "id", "claim label")
            fid = text(finding["id"])
            require(fid not in ids, "duplicate finding in a review")
            ids.add(fid)
            if "claim" in finding:
                text(finding["claim"])  # Recorded as data, never used as a label.
            label = finding.get("label")
            if label is None:
                findings.append("unknown")
                continue
            label_at(label, review.commit, cutoff, "defect")
            verdict = choice(label["verdict"], ("confirmed", "false", "unknown"))
            if verdict == "confirmed":
                key = defect_id(label.get("defect"), owner)
                require(
                    key in history.eligible_at(review.commit), "confirmation outside frozen truth"
                )
                findings.append(key)
            else:
                require("defect" not in label, "only a confirmation names a defect")
                findings.append(verdict)
        result.append(
            Review(
                review.revision,
                review.disposition,
                tuple(findings),
                review.validation,
                review.certificate,
                review.seq,
            )
        )
    return result


@dataclass(frozen=True)
class Stop:
    through: int
    phase: str
    revision: Revision | None
    outcome: str
    certificate: str | None
    reason: str


def admit_stop(row: Any) -> Stop | None:
    if row is None:
        return None
    shape(row, "through phase source reason", "revision outcome certificate")
    phase = choice(row["phase"], ("READY_FOR_MERGE", "BLOCKED", "FAILED"))
    outcome = choice(row.get("outcome", "unresolved"), ("certified", "not_issued", "unresolved"))
    bound = revision(row["revision"]) if row.get("revision") is not None else None
    cert = row.get("certificate")
    require(
        cert is None or (outcome == "certified" and bool(text(cert))), "invalid stop certificate"
    )
    text(row["source"])
    return Stop(integer(row["through"]), phase, bound, outcome, cert, text(row["reason"]))


@dataclass(frozen=True)
class Run:
    id: str
    case: str
    arm: str
    history: History
    reviews: list[Review]
    fixes: dict[str, tuple[str, int]]
    excluded: str | None
    start: bool
    stop: Stop | None
    attempts: dict[int, Json]

    def terminal(self) -> tuple[str, Review | None]:
        """Select a terminal observation from a stop, never from review history alone."""
        stop = self.stop
        if stop is None:
            return "missing_stop", None
        if (
            any(seq > stop.through for seq in self.attempts)
            or any(seq > stop.through for _, seq in self.fixes.values())
            or (self.reviews and self.reviews[-1].seq > stop.through)
        ):
            return "continued", None
        if not self.reviews or self.reviews[-1].revision != stop.revision:
            return "unbound_stop", None
        review = self.reviews[-1]
        # Even an unlogged trailing reservation leaves this review historical.
        if stop.through != review.seq:
            return "unbound_stop", None
        if review.disposition != "accepted":
            return "unbound_stop", None
        if (
            stop.phase == "READY_FOR_MERGE"
            and stop.outcome == "certified"
            and stop.certificate is not None
            and stop.certificate == review.certificate
        ):
            return "certified", review
        if (
            stop.phase in ("BLOCKED", "FAILED")
            and stop.outcome == "not_issued"
            and review.certificate is None
        ):
            return "not_issued", review
        return "unresolved_stop", None

    def complete_window(self) -> bool:
        stop = self.stop
        if not self.start or stop is None or len(self.attempts) != stop.through:
            return False
        if any(seq > stop.through for seq in self.attempts):
            return False  # Distinct positive ordinals with this count prove 1..through.
        if not self.attempts or self.attempts[1]["phase"] != "ANALYZE_EXECUTE":
            return False
        return all(review.seq in self.attempts for review in self.reviews) and all(
            (attempt := self.attempts.get(seq)) is not None
            and attempt["phase"] == "FIX"
            and attempt["outcome"] == "reply"
            for _, seq in self.fixes.values()
        )


def admit_attempts(rows: Any) -> dict[int, Json]:
    attempts = {}
    for row in items(rows):
        shape(row, "seq phase round outcome source", "usd seconds context_tokens")
        seq = integer(row["seq"], 1)
        require(seq not in attempts, "duplicate launch ordinal")
        phase = choice(row["phase"], PHASES)
        integer(row["round"], int(phase in ("REVIEW", "FIX")))
        choice(row["outcome"], ("reply", "failed", "abandoned", "unknown"))
        text(row["source"])
        for key in ("usd", "seconds", "context_tokens"):
            value = row.get(key)
            if value is not None:
                (integer if key == "context_tokens" else number)(value)
        attempts[seq] = row
    return attempts


def admit(data: Any) -> tuple[str, dict[str, Case], list[Run]]:
    shape(data, "schema mode cutoff cases runs")
    require(integer(data["schema"]) == 1, "unsupported study schema")
    mode = choice(data["mode"], ("fixture", "recorded"))
    cutoff = timestamp(data["cutoff"])
    cases: dict[str, Case] = {}
    for row in items(data["cases"]):
        shape(row, "id kind risk snapshots defects")
        key = text(row["id"])
        require(key not in cases, "duplicate case ID")
        kind = choice(row["kind"], ("defect", "clean_negative"))
        risk = choice(row["risk"], ("low", "standard", "high"))
        require(len(items(row["snapshots"])) >= 2, "case must pin base and candidate")
        history = History({}, {}, {})
        extend(
            history, row["snapshots"], definitions(row["defects"], None, cutoff), None, None, cutoff
        )
        require(not next(iter(history.present.values())), "base defects are outside the study")
        replay(history)
        candidate = next(reversed(history.files))
        require(
            bool(history.eligible_at(candidate)) == (kind == "defect"),
            "case kind conflicts with frozen truth at its pinned candidate",
        )
        if kind == "clean_negative":
            require(
                not any(map(history.eligible_at, history.files)), "negative lineage has a defect"
            )
        cases[key] = Case(kind, risk, candidate, history)
    require(cases, "empty case manifest")
    runs = []
    ids = set()
    assignments = set()
    for row in items(data["runs"]):
        shape(
            row,
            "id arm case snapshots defects reviews attempts",
            "start stop exclusion",
        )
        rid, cid, arm = text(row["id"]), text(row["case"]), choice(row["arm"], ARMS)
        require(rid not in ids and (cid, arm) not in assignments, "duplicate run or case/arm")
        require(cid in cases, "run does not name a registered case")
        ids.add(rid)
        assignments.add((cid, arm))
        case = cases[cid]
        h = case.history
        history = History(dict(h.files), dict(h.defects), dict(h.present))
        fixes = extend(
            history,
            row["snapshots"],
            definitions(row["defects"], rid, cutoff),
            rid,
            case.candidate,
            cutoff,
        )
        replay(history)
        attempts = admit_attempts(row["attempts"])
        reviews = label_reviews(
            row["reviews"], admit_reviews(row["reviews"], history, attempts), history, rid, cutoff
        )
        excluded = text(row["exclusion"]) if "exclusion" in row else None
        if reviews:
            require(reviews[0].commit == case.candidate, "first review must start at the case pin")
        start = row.get("start")
        if start is not None:
            shape(start, "source run initial_seq")
            require(
                start["run"] == rid and integer(start["initial_seq"]) == 0,
                "start receipt must identify creation of this run at ordinal zero",
            )
            text(start["source"])
        runs.append(
            Run(
                rid,
                cid,
                arm,
                history,
                reviews,
                fixes,
                excluded,
                start is not None,
                admit_stop(row.get("stop")),
                attempts,
            )
        )
    return mode, cases, runs
