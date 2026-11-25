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
                # An eligible regression introduced here makes the FIX harmful,
                # so no other label can settle it (unknown included); without
                # one, harmful is unsupported.
                known_regression = any(introduced[key].eligible for key in new)
                require(
                    (verdict == "harmful") == known_regression,
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
    base: str  # The registered merge base: every review of the case diffs from it.
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
    reply: str
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


def admit_reviews(
    rows: Any, history: History, attempts: dict[int, Json], case: Case
) -> list[Review]:
    reviews: list[Review] = []
    lineage = list(history.files)
    for row in items(rows):
        shape(row, "revision reply disposition findings seq source", "validation certificate")
        bound = revision(row["revision"])
        require(bound.commit in history.files, "review names an unverified commit")
        require(
            lineage.index(bound.commit) >= lineage.index(case.candidate),
            "review names a commit before the case pin",
        )
        # Both arms review the registered change: another merge base is another
        # diff (one that may already hold the defect). R0 supports no base change.
        require(bound.merge_base == case.base, "review merge base is not the case's base")
        text(row["source"])
        if reviews:
            # R0 observes one PR against one base branch: a retarget is a base
            # change, and a takeover or a replacement is another PR. On one PR
            # the controller's completed rounds only advance (`review_round`);
            # a skipped round is a missing row, a repeated or earlier one is not.
            last = reviews[-1].revision
            require(bound.pr == last.pr, "R0 observes one PR per run")
            require(bound.base == last.base, "R0 supports no base branch retarget")
            require(bound.round > last.round, "completed review rounds must advance")
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
        # The controller refuses a clean reply over presented validation that
        # did not pass, before publication (engine `_check_review_evidence`);
        # only an accepted reply can be found stale afterwards. Such a reply is
        # a `refused` launch, never a completed round of any disposition.
        require(
            not (reply == "clean" and validation == "failed"),
            "a completed clean review conflicts with its failed validation",
        )
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
        reviews.append(Review(bound, disposition, reply, (), validation, certificate, seq))
    return reviews


def bind_fixes(
    fixes: dict[str, tuple[str, int]],
    reviews: list[Review],
    attempts: dict[int, Json],
    history: History,
) -> bool:
    """Launch order agrees with lineage: a review, then a FIX, then reviews of its commit.

    The review a FIX follows is its source: the controller launches a fixer
    only from a needs-fix round it accepted, on that round's candidate. A
    clean or stale source, a FIX commit on another parent, a present launch
    row that disagrees, a shared ordinal or a reversed order is a
    contradiction. An absent launch row is missing evidence (the window
    stays incomplete). Returns False when a source's disposition is
    unknown: nothing contradicts, but the FIX's provenance is unproven.
    """
    lineage = list(history.files)
    order = {commit: index for index, commit in enumerate(lineage)}
    bound = True
    for commit, (_, seq) in fixes.items():
        require(
            all(review.seq != seq for review in reviews),
            "a FIX and a review share a launch ordinal",
        )
        earlier = [review for review in reviews if review.seq < seq]
        require(earlier, "a FIX launch must follow a review")
        require(
            all(review.seq > seq for review in reviews if order[review.commit] >= order[commit]),
            "a review precedes the FIX launch that produced its commit",
        )
        source = earlier[-1]
        require(
            source.reply == "needs_fix" and source.disposition != "stale",
            "a FIX launch must follow a needs-fix review the controller accepted",
        )
        require(
            lineage[order[commit] - 1] == source.commit,
            "a FIX commit must apply to the candidate of the review it follows",
        )
        bound = bound and source.disposition == "accepted"
        if seq in attempts:
            require(
                (attempts[seq]["phase"], attempts[seq]["round"], attempts[seq]["outcome"])
                == ("FIX", source.revision.round, "reply"),
                "launch contradicts its recorded FIX commit",
            )
    return bound


class Step(NamedTuple):
    """A recorded launch, as far as it constrains the launch after it."""

    phase: str
    round: int
    outcome: str
    origin: Review | None  # The review row a REVIEW is, or a FIX or replan chain follows.
    published: bool  # A FIX commit: the controller's next launch reviews its revision.

    def completed(self) -> set[int]:
        """The controller's possible completed-round counts once this launch ended."""
        ran = self.round - (self.phase == "REVIEW")  # Completed rounds when it ran.
        if self.outcome not in ("reply", "unknown"):
            return {ran}  # Refused, failed or abandoned: it completed nothing.
        if self.phase == "REVIEW":
            return {self.round} if self.outcome == "reply" else {ran, self.round}
        if self.phase == "REPLAN_REEXECUTE":
            # An accepted replan activates a replacement: its fresh round 1 is next.
            return {0} if self.outcome == "reply" else {ran, 0}
        return {ran}

    def refuses(self, phase: str, rnd: int) -> str | None:
        """Why no controller launches `phase` of round `rnd` right after this launch."""
        if rnd - (phase == "REVIEW") not in self.completed():
            return "launch rounds contradict the controller's completed-round count"
        if self.published and phase != "REVIEW":
            # A completed FIX routes to REVIEW. A published partial fix blocks,
            # then re-enters REVIEW at that revision: the PR is past the HEAD
            # the round's findings bind, so never another FIX of the round.
            return "a published FIX commit is followed by the review of its revision"
        if phase == "ANALYZE_EXECUTE" and self.phase != phase:
            return "ANALYZE_EXECUTE follows no other launch"
        if phase in ("FIX", "REPLAN_REEXECUTE"):
            if self.phase not in ("REVIEW", phase) or self.round != rnd:
                return f"{phase} must follow the review of its round or retry itself"
            if self.origin is not None and (
                self.origin.reply != "needs_fix" or self.origin.disposition == "stale"
            ):
                return f"{phase} must follow a needs-fix review the controller accepted"
        return None

    def fewest(self, phase: str, rnd: int) -> int | None:
        """The fewest launches between this one and `phase` of `rnd` (None: never).

        Any more fit as well: failed attempts of that launch right before it
        change nothing a later launch depends on.
        """
        if self.refuses(phase, rnd) is None:
            return 0
        if phase == "ANALYZE_EXECUTE":
            return None  # Only after itself, which needs nothing between.
        # A FIX or replan of round r follows a completed REVIEW of round r;
        # an unrecorded one may be the needs-fix source it needs.
        return self.until(rnd - 1) + (phase != "REVIEW")

    def until(self, target: int) -> int:
        """The fewest launches after this one with `target` rounds complete (0: now)."""
        counts = self.completed()
        if target in counts:
            return 0
        # Each REVIEW completes at most one round. Only an accepted replan
        # lowers the count, to zero, right after a completed REVIEW of its
        # round: this launch, or one more REVIEW.
        replan = 1 if self.refuses("REPLAN_REEXECUTE", self.round) is None else 2
        return min([target - count for count in counts if count < target] + [replan + target])


def check_lifecycle(
    attempts: dict[int, Json],
    reviews: list[Review],
    fixes: dict[str, tuple[str, int]],
    start: bool,
) -> None:
    """Replay the controller's completed-round count over the recorded ordinals.

    A launch row, a review row (an accepted REVIEW reply) or a FIX commit (a
    FIX reply of its source's round) records an ordinal's phase, round and
    outcome; admission already refused any two of them that disagree. A
    REVIEW of round r runs after r - 1 completed rounds and completes round r
    only with a `reply` (`unknown` may have). A FIX or REPLAN_REEXECUTE of
    round r runs after r, right after the REVIEW of round r or a retry of
    itself, and a published FIX commit only before a REVIEW; ANALYZE_EXECUTE
    runs before any round, first or after itself. Only a replacement can
    return the count to zero. Between consecutive recorded ordinals the count
    changes only as the earlier launch allows. Unrecorded ordinals between
    two recorded ones are missing evidence, not a contradiction, only while
    they can hold the launches the controller needs between those two. A
    start receipt records ordinal zero, before any launch or round.
    """
    events = {seq: (row["phase"], row["round"], row["outcome"]) for seq, row in attempts.items()}
    rows = {review.seq: review for review in reviews}
    for review in reviews:
        events.setdefault(review.seq, ("REVIEW", review.revision.round, "reply"))
    published = set()
    for _, seq in fixes.values():
        source = [review for review in reviews if review.seq < seq][-1]  # By `bind_fixes`.
        events.setdefault(seq, ("FIX", source.revision.round, "reply"))
        published.add(seq)
    # Ordinal zero: what a launch at ordinal one may follow (it may be a REVIEW).
    before = Step("ANALYZE_EXECUTE", 0, "reply", None, False) if start else None
    last = 0
    for seq in sorted(events):
        phase, rnd, outcome = events[seq]
        origin = rows.get(seq)
        if before is not None and seq == last + 1:
            problem = before.refuses(phase, rnd)
            require(problem is None, problem or "")
            if phase != "REVIEW":
                origin = before.origin  # A FIX or replan chain keeps its source.
        elif before is not None:
            fewest = before.fewest(phase, rnd)
            require(fewest is not None, before.refuses(phase, rnd) or "")
            require(
                fewest is not None and fewest < seq - last,
                "too few unrecorded ordinals for the launches the controller needs between",
            )
        before, last = Step(phase, rnd, outcome, origin, seq in published), seq


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
                review.reply,
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
    fixes_bound: bool  # Every FIX's source review is known to be accepted.

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
        if not self.fixes_bound:
            return "unbound_fix", None
        if not self.reviews or self.reviews[-1].revision != stop.revision:
            return "unbound_stop", None
        review = self.reviews[-1]
        if review.disposition != "accepted":
            return "unbound_stop", None
        if stop.through != review.seq:
            # Only the write of a REVIEW round issues a certificate, so even an
            # unlogged trailing reservation leaves that review's historical.
            # Non-issuance can still bind past it, across FIX launches of its
            # round, each recorded, that applied no commit (a held or failed
            # FIX blocks without publishing): they change neither the bound
            # revision nor issuance. The lifecycle replay tied them to it.
            held = not any(seq > review.seq for _, seq in self.fixes.values()) and all(
                self.attempts.get(seq, {}).get("phase") == "FIX"
                for seq in range(review.seq + 1, stop.through + 1)
            )
            if not held or review.certificate is not None:
                return "unbound_stop", None
            if stop.phase in ("BLOCKED", "FAILED") and stop.outcome == "not_issued":
                return "not_issued", review
            return "unresolved_stop", None
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
        # Admission refused any present row that contradicts its review or FIX.
        return all(review.seq in self.attempts for review in self.reviews) and all(
            seq in self.attempts for _, seq in self.fixes.values()
        )

    def complete_reviews(self) -> bool:
        """Every review the controller may have completed through the stop has its row.

        This is not the launch window, which accounts for usage: a complete
        window can lack a review row, and an incomplete one may lack none. On
        the run's one PR each completed review consumes the next round, so
        rows of rounds 1..n hold every review completed before the last of
        them. One is missing, or may be, when a REVIEW launch completed with
        no row, a launch ran after (or may have completed) a round past n, an
        ordinal after the last row has no record, there is no stop, or
        something is recorded past it. A row of unknown disposition may count
        or not.
        """
        stop = self.stop
        if stop is None:
            return False
        rows = {review.seq for review in self.reviews}
        recorded = rows | self.attempts.keys() | {seq for _, seq in self.fixes.values()}
        if any(seq > stop.through for seq in recorded):
            return False
        count = len(self.reviews)
        if [review.revision.round for review in self.reviews] != list(range(1, count + 1)):
            return False  # The round progression skips a completed review.
        if any(review.disposition == "unknown" for review in self.reviews):
            return False
        for seq, row in self.attempts.items():
            rnd = row["round"]
            if row["phase"] == "REVIEW":
                if row["outcome"] == "reply" and seq not in rows:
                    return False
                # One that completed nothing shows only that rnd - 1 had.
                rnd -= row["outcome"] not in ("reply", "unknown")
            if rnd > count:
                return False
        last = self.reviews[-1].seq if self.reviews else 0
        return all(seq in recorded for seq in range(last + 1, stop.through + 1))


def admit_attempts(rows: Any) -> dict[int, Json]:
    attempts = {}
    for row in items(rows):
        shape(row, "seq phase round outcome source", "usd seconds context_tokens")
        seq = integer(row["seq"], 1)
        require(seq not in attempts, "duplicate launch ordinal")
        phase = choice(row["phase"], PHASES)
        # The round is `review_round` at the launch, or the one a REVIEW
        # reviews: ANALYZE_EXECUTE runs before any, every other phase in one.
        rnd = integer(row["round"], int(phase != "ANALYZE_EXECUTE"))
        require(phase != "ANALYZE_EXECUTE" or rnd == 0, "ANALYZE_EXECUTE runs at round zero")
        # `refused`: a reply the controller refused before accepting it; a
        # correction launch follows, and no review row binds the refused one.
        choice(row["outcome"], ("reply", "refused", "failed", "abandoned", "unknown"))
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
        cases[key] = Case(kind, risk, next(iter(history.files)), candidate, history)
    require(cases, "empty case manifest")
    runs = []
    ids = set()
    assignments = set()
    targets: dict[str, str] = {}  # The base branch every review of a case names.
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
            row["reviews"],
            admit_reviews(row["reviews"], history, attempts, case),
            history,
            rid,
            cutoff,
        )
        start = row.get("start")
        if start is not None:
            shape(start, "source run initial_seq")
            require(
                start["run"] == rid and integer(start["initial_seq"]) == 0,
                "start receipt must identify creation of this run at ordinal zero",
            )
            text(start["source"])
        fixes_bound = bind_fixes(fixes, reviews, attempts, history)
        check_lifecycle(attempts, reviews, fixes, start is not None)
        excluded = text(row["exclusion"]) if "exclusion" in row else None
        if reviews:
            require(reviews[0].commit == case.candidate, "first review must start at the case pin")
            # Within a run `admit_reviews` refused a retarget; across the arms
            # one base branch too, so both review one diff against one target.
            target = reviews[0].revision.base
            require(
                targets.setdefault(cid, target) == target, "arms review different base branches"
            )
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
                fixes_bound,
            )
        )
    return mode, cases, runs
