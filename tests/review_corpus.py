"""REV-R1: historical review corpus format, offline git verifier, and reviewer scorer (#356).

``describe`` validates a corpus against the closed version 1 schema, cutoff and
leakage rules, adjudication independence, and cross-case identity invariants;
``verify`` re-derives a historical corpus's pins from a local git object store
(read only); ``score`` computes offline scorecards and cluster-bootstrap
contrasts; ``export-input`` emits the strict reviewer-visible input projection.
Nothing here launches a model, reaches a network, writes to git/GitHub, or
changes production controller code. See ``docs/review-corpus.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from fractions import Fraction
from functools import cached_property, lru_cache
from hashlib import sha256
from pathlib import Path, PurePosixPath
from random import Random

from tests.review_eval import CLASSES, EXCLUSIONS, WEIGHT, rate

FIXTURES = Path(__file__).parent / "fixtures" / "review_corpus"
SEED = FIXTURES / "autoforge_seed.json"
SYNTHETIC_CORPUS = FIXTURES / "synthetic_corpus.json"
SYNTHETIC_SUBMISSIONS = FIXTURES / "synthetic_submissions.json"

CORPUS_FORMAT = "autoforge-review-corpus"
SUBMISSIONS_FORMAT = "autoforge-review-submissions"
INPUT_FORMAT = "autoforge-review-input"
VERSION = 1

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_CASES = 1_000
MAX_DEFECTS_PER_CASE = 200
MAX_CONTEXT_ITEMS_PER_CASE = 500
MAX_POST_CUTOFF_REFS = 64
MAX_ADJUDICATORS = 20
MAX_REVIEWER_TEXT_BYTES = 512 * 1024

MAX_VARIANTS = 8
MAX_PLANNED_PAIRS = 10_000
MAX_PLANNED_CLUSTERS = 64
MAX_FINDINGS_PER_RUN = 500
BOOTSTRAP_DRAWS = 10_000
BOOTSTRAP_SEED = 356
BOOTSTRAP_TABLES = 16
LABEL_BAND = Fraction(1, 10)
MIN_CLUSTERS = 2
MIN_NEGATIVE_WINDOW_DAYS = 30
GIT_TIMEOUT_SECONDS = 30

SEVERITIES = tuple(WEIGHT)
STRATA = {
    "reviewed_defect": True,
    "escape": True,
    "revert": True,
    "hard_negative": False,
    "clean": False,
}
SOURCES = {
    "synthetic": ("synthetic",),
    "historical": ("pr_branch", "squash", "merge_pr"),
}
SUBMISSION_MODES = {"synthetic": "synthetic", "historical": "recorded"}
DISCOVERY = ("same_review", "later_review", "post_merge", "revert")
AFTER_MERGE = ("post_merge", "revert")
STRATUM_SOURCES = {
    "reviewed_defect": {"same_review", "later_review"},
    "escape": {"post_merge"},
    "revert": {"revert"},
}
RISKS = ("routine", "elevated", "critical")
VISIBILITIES = ("public", "private", "synthetic")
BASE_EVIDENCES = ("recorded", "squash_parent", "merge_parent", "provisional", "synthetic")
EVIDENCE_STATUSES = ("recorded", "provisional", "synthetic")
CONTEXT_KINDS = ("issue", "issue_comment", "pr_body", "pr_comment", "document")
SINGULAR_KINDS = ("issue", "pr_body")
INVENTORY_STATUSES = ("complete", "not_captured")
THREAD_SOURCE = re.compile(r"(issue|pr)/([1-9][0-9]{0,9})(?:/comment/([a-z0-9][a-z0-9._-]{0,63}))?")
DOCUMENT_SOURCE = "document/"
THREAD_KINDS = {
    ("issue", False): "issue",
    ("issue", True): "issue_comment",
    ("pr", False): "pr_body",
    ("pr", True): "pr_comment",
}
ROLES = {"curator", "adjudicator", "arbiter"}
DEFECT_VERDICTS = {"confirmed", "rejected", "unknown"}
NEGATIVE_VERDICTS = {"clean", "defective", "unknown"}
MATCH_VERDICTS = {"match", "false", "unknown"}
ORACLE_STATUSES = ("pass", "fail", "not_run", "unknown")
CLASSIFICATIONS = ("blocked", "non-blocked", "nit")
CLAIM = "scorecards only: no benchmark decision, gate or rollout follows from this report"

IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
OBJECT_ID = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
HEX_RUN = re.compile(r"[0-9a-fA-F]{7,}")
HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", re.MULTILINE)


class CorpusError(ValueError):
    """A corpus or submission is malformed or violates an integrity invariant."""


def require(condition: object, rule: str) -> None:
    if not condition:
        raise CorpusError(rule)


def closed(value: object, names: str) -> None:
    require(isinstance(value, dict) and value.keys() == set(names.split()), f"fields: {names}")


def nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def object_id(value: object) -> str:
    require(isinstance(value, str) and OBJECT_ID.fullmatch(value), "object id shape")
    return value  # type: ignore[return-value]


def digest64(value: object, rule: str = "digest shape") -> str:
    require(isinstance(value, str) and DIGEST.fullmatch(value), rule)
    return value  # type: ignore[return-value]


def day(value: object) -> date:
    require(isinstance(value, str) and DAY.fullmatch(value), "date shape")
    try:
        return date.fromisoformat(value)  # type: ignore[arg-type]
    except ValueError as exc:
        raise CorpusError("date shape") from exc


def instant(value: object) -> datetime:
    require(isinstance(value, str) and INSTANT.fullmatch(value), "instant shape")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)  # type: ignore[arg-type]
    except ValueError as exc:
        raise CorpusError("instant shape") from exc


def relative_path(value: object) -> str:
    require(isinstance(value, str) and value and "\\" not in value, "repository path")
    parts = PurePosixPath(value).parts  # type: ignore[arg-type]
    require(
        not value.startswith("/") and ".." not in parts and "." not in value.split("/"),  # type: ignore[union-attr]
        "repository path",
    )
    require(str(PurePosixPath(value)) == value, "repository path")  # type: ignore[arg-type]
    return value  # type: ignore[return-value]


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def corpus_digest(corpus: dict) -> str:
    """Canonical JSON SHA-256 digest of a corpus."""
    return sha256(canonical(corpus)).hexdigest()


# -- reviewer-visible boundary & deterministic O(N) leakage checks ------------


def project_reviewer_input(case: dict) -> dict:
    """Strict allow-list projection of everything a reviewer is given.

    Excludes ``ref``, provenance notes, withheld commits, post-cutoff refs,
    actors, risk, exposure, stratum, defects, and negative outcomes.
    """
    return {
        "format": INPUT_FORMAT,
        "version": VERSION,
        "review": dict(case["review"]),
        "environment": dict(case["environment"]),
        "context": [
            {key: item[key] for key in ("id", "kind", "source", "sha256")}
            for item in case["context"]["items"]
        ],
    }


def input_digest(case: dict) -> str:
    """SHA-256 of the frozen reviewer-visible input projection."""
    return sha256(canonical(project_reviewer_input(case))).hexdigest()


def reviewer_text(case: dict) -> Iterable[str]:
    """Every free-text string exposed to a reviewer (bounded, excludes ``ref``)."""
    yield case["review"]["base_ref"]
    yield from case["environment"]["files"]
    yield from case["environment"]["validation"]
    for item in case["context"]["items"]:
        yield item["id"]
        yield item["kind"]
        if item["source"] is not None:
            yield item["source"]
        if item["text"] is not None:
            yield item["text"]


def check_text_leakage(
    texts: Sequence[str],
    forbidden_shas: Iterable[str],
    post_cutoff_refs: Sequence[str],
) -> None:
    """Verify in O(|text|) time that no reviewer-visible text names a withheld
    commit (7+ hex run containing any forbidden SHA's first 7 hex digits) or
    any explicit post-cutoff reference."""
    prefixes = frozenset(commit[:7].lower() for commit in forbidden_shas)
    lowered_refs = tuple(ref.strip().lower() for ref in post_cutoff_refs)
    for text in texts:
        if prefixes:
            for match in HEX_RUN.finditer(text):
                run = match.group(0).lower()
                if any(run[i : i + 7] in prefixes for i in range(len(run) - 6)):
                    raise CorpusError("the reviewer's input names a withheld commit")
        if lowered_refs:
            lowered = text.lower()
            for ref in lowered_refs:
                if ref in lowered:
                    raise CorpusError("the reviewer's input names a post-cutoff reference")


def source_kind(source: object) -> tuple[str, str | None, int | None]:
    """Parse a stable context source identity into ``(kind, thread, number)``."""
    require(isinstance(source, str), "context source")
    if source.startswith(DOCUMENT_SOURCE):  # type: ignore[union-attr]
        relative_path(source.removeprefix(DOCUMENT_SOURCE))  # type: ignore[union-attr]
        return "document", None, None
    match = THREAD_SOURCE.fullmatch(source)  # type: ignore[arg-type]
    require(match, "context source")
    thread, number, comment = match.groups()
    return THREAD_KINDS[(thread, comment is not None)], thread, int(number)


# -- adjudication labels ------------------------------------------------------

LABEL_FIELDS = "by role verdict decided artifact"


def counted(label: dict, role: str) -> bool:
    return label["role"] == role and label["verdict"] != "unknown"


def verdict_of(label: dict):
    return (label["verdict"], label["defect"]) if "defect" in label else label["verdict"]


def check_labels(
    labels: object,
    verdicts: set[str],
    *,
    frozen: date,
    barred: frozenset[str],
    match: dict | None = None,
    since: date | None = None,
) -> None:
    """Validate label provenance, independence, cutoff lower bound, and arbiter rules."""
    require(isinstance(labels, list) and len(labels) <= MAX_ADJUDICATORS, "labels")
    seen: set[str] = set()
    for label in labels:
        closed(label, LABEL_FIELDS + (" defect" if match is not None else ""))
        require(nonempty(label["by"]) and nonempty(label["artifact"]), "label provenance")
        require(label["role"] in ROLES and label["verdict"] in verdicts, "label verdict")
        decided = day(label["decided"])
        require(decided <= frozen, "label decided after the freeze")
        require(since is None or since <= decided, "label decided before its case's cutoff")
        who = label["by"].strip()
        require(who not in barred, "an actor cannot adjudicate")
        require(who not in seen, "one label per identity")
        seen.add(who)
        if match is not None:
            if label["verdict"] == "match":
                require(label["defect"] in match, "match names a registered defect")
            else:
                require(label["defect"] is None, "only a match names a defect")
    votes = {verdict_of(label) for label in labels if counted(label, "adjudicator")}
    if any(counted(label, "arbiter") for label in labels):
        require(len(votes) > 1, "an arbiter settles a disagreement only")


def settle(labels: Sequence[dict], need: Callable[[object], int] = lambda _: 1):
    """Return the settled verdict or ``None`` when left unknown.

    Curators propose and never settle. Adjudicators settle by unanimous
    agreement meeting ``need(verdict)`` (2 for critical defects/matches).
    Arbiters decide between existing adjudicator verdicts: all arbiters must
    agree, and the chosen verdict must have at least ``max(1, need(verdict))``
    adjudicator votes (preventing unvoted or single-vote critical outcomes).
    """
    votes = Counter(verdict_of(label) for label in labels if counted(label, "adjudicator"))
    arbiters = {verdict_of(label) for label in labels if counted(label, "arbiter")}
    if arbiters:
        if len(arbiters) != 1:
            return None
        verdict = next(iter(arbiters))
        required = max(1, need(verdict))
        return verdict if votes[verdict] >= required else None
    if len(votes) != 1:
        return None
    verdict, count = next(iter(votes.items()))
    return verdict if count >= need(verdict) else None


def critical_needs_two(severity: str) -> Callable[[object], int]:
    """A critical defect requires two independent adjudicators."""
    return lambda _: 2 if severity == "critical" else 1


# -- corpus data model & validation -------------------------------------------

CORPUS_FIELDS = "format version mode name release frozen repository cases"
REPOSITORY_FIELDS = "slug root refs visibility environment"
ENV_DECL_FIELDS = "files validation_scope validation"
CASE_FIELDS = (
    "stratum cluster provenance review environment context actors risk exposure"
    " defects negative withheld post_cutoff_refs"
)
PROVENANCE_FIELDS = "source issue pr round base_evidence merge_commit selection note"
REVIEW_FIELDS = "base_ref base_tip merge_base head base_tree merge_base_tree head_tree cutoff"
DEFECT_FIELDS = "severity class introduced summary location discovery labels"
DISCOVERY_FIELDS = "source round finding discovered fix merged evidence_status artifact"
EXPOSURE_FIELDS = "possible_from public_by artifact"


@dataclass(frozen=True)
class Defect:
    severity: str
    cls: str
    source: str
    discovered: date
    status: str  # confirmed, rejected, or unknown


@dataclass(frozen=True)
class Tally:
    """Per-case defect summary computed once and shared across all runs."""

    severity: dict[str, str]
    source: dict[str, str]
    cls: dict[str, str]
    conf_by_sev: Counter[str]
    unset_by_sev: Counter[str]
    conf_by_src: Counter[str]
    unset_by_src: Counter[str]
    conf_by_cls: Counter[str]
    unset_by_cls: Counter[str]
    conf_weights_desc: tuple[int, ...]
    unset_weights_desc: tuple[int, ...]

    @classmethod
    def of(cls, defects: dict[str, Defect]) -> Tally:
        confirmed = {k: d for k, d in defects.items() if d.status == "confirmed"}
        unsettled = {k: d for k, d in defects.items() if d.status == "unknown"}
        return cls(
            severity={k: d.severity for k, d in confirmed.items()},
            source={k: d.source for k, d in confirmed.items()},
            cls={k: d.cls for k, d in confirmed.items()},
            conf_by_sev=Counter(d.severity for d in confirmed.values()),
            unset_by_sev=Counter(d.severity for d in unsettled.values()),
            conf_by_src=Counter(d.source for d in confirmed.values()),
            unset_by_src=Counter(d.source for d in unsettled.values()),
            conf_by_cls=Counter(d.cls for d in confirmed.values()),
            unset_by_cls=Counter(d.cls for d in unsettled.values()),
            conf_weights_desc=tuple(
                sorted((WEIGHT[d.severity] for d in confirmed.values()), reverse=True)
            ),
            unset_weights_desc=tuple(
                sorted((WEIGHT[d.severity] for d in unsettled.values()), reverse=True)
            ),
        )


@dataclass(frozen=True)
class Case:
    id: str
    cluster: str
    stratum: str
    risk: str
    source: str
    issue: int | None
    pr: int | None
    round: int | None
    cutoff: datetime
    actors: frozenset[str]
    labelers: frozenset[str]
    defects: dict[str, Defect]
    negative: str | None  # settled, censored, unlabeled, unverified_oracle, refuted, or None
    runnable: bool
    input: str
    visibility: str
    possible_from: date | None = None
    public_by: date | None = None

    @cached_property
    def tally(self) -> Tally:
        return Tally.of(self.defects)

    @cached_property
    def public(self) -> date:
        """Earliest day by which part of the case's answer was certainly public.

        Uses only non-rejected defects (confirmed or unknown) or the case's own
        cutoff day for negatives/all-rejected cases, plus any earlier ``public_by``.
        """
        days = [d.discovered for d in self.defects.values() if d.status != "rejected"]
        if not days:
            days = [self.cutoff.date()]
        if self.public_by is not None:
            days.append(self.public_by)
        return min(days)


@dataclass(frozen=True)
class Corpus:
    name: str
    release: int
    mode: str
    frozen: date
    visibility: str
    digest: str
    cases: dict[str, Case] = field(default_factory=dict)


def required_context(origin: dict) -> set[str]:
    required: set[str] = set()
    if origin["issue"] is not None:
        required.add("issue")
    if origin["pr"] is not None or origin["source"] in ("pr_branch", "squash", "merge_pr"):
        required.add("pr_body")
    return required


def check_context(context: object, *, origin: dict, cutoff: datetime, frozen: date) -> bool:
    """Validate context items, cutoff/edit timestamps, digests, and inventory."""
    closed(context, "inventory items")
    items = context["items"]  # type: ignore[index]
    require(isinstance(items, list) and len(items) <= MAX_CONTEXT_ITEMS_PER_CASE, "context items")
    ids: set[str] = set()
    sources: set[str] = set()
    kinds: Counter[str] = Counter()
    captured = True
    for item in items:
        closed(item, "id kind source ref observed_at edited_at status text sha256")
        require(isinstance(item["id"], str) and IDENTIFIER.fullmatch(item["id"]), "context id")
        require(item["id"] not in ids, "context id repeated")
        ids.add(item["id"])
        require(item["kind"] in CONTEXT_KINDS and nonempty(item["ref"]), "context kind")
        kinds[item["kind"]] += 1
        source = item["source"]
        if source is None:
            require(item["status"] == "not_captured", "a captured context names its source")
        else:
            kind, thread, number = source_kind(source)
            require(kind == item["kind"], "a context item's kind is its source's")
            if thread is not None:
                require(number == origin[thread], f"a context source is on the case's {thread}")
            require(source not in sources, "context source repeated")
            sources.add(source)
        if item["status"] == "not_captured":
            require(
                item["observed_at"] is None
                and item["edited_at"] is None
                and item["text"] is None
                and item["sha256"] is None,
                "an uncaptured context holds nothing",
            )
            captured = False
            continue
        require(item["status"] == "captured", "context status")
        require(instant(item["observed_at"]) <= cutoff, "context observed after the cutoff")
        if item["edited_at"] is not None:
            require(instant(item["edited_at"]) <= cutoff, "context edited after the cutoff")
        require(nonempty(item["text"]), "captured context text")
        require(
            item["sha256"] == sha256(item["text"].encode("utf-8")).hexdigest(), "context digest"
        )
    require(all(kinds[kind] <= 1 for kind in SINGULAR_KINDS), "one issue and one PR body")
    for kind in sorted(required_context(origin)):
        require(kinds[kind] == 1, f"the context holds the case's {kind}")

    inventory = context["inventory"]  # type: ignore[index]
    closed(inventory, "status taken artifact edit_history_verified sources")
    require(inventory["status"] in INVENTORY_STATUSES, "context inventory status")
    require(type(inventory["edit_history_verified"]) is bool, "edit history flag")
    if inventory["status"] == "not_captured":
        require(
            all(inventory[k] is None for k in ("taken", "artifact", "sources"))
            and not inventory["edit_history_verified"],
            "an uncaptured inventory holds nothing",
        )
        return False
    taken = instant(inventory["taken"])
    require(cutoff <= taken and taken.date() <= frozen, "inventory taken between cutoff and freeze")
    require(nonempty(inventory["artifact"]), "inventory artifact")
    listed = inventory["sources"]
    require(isinstance(listed, list), "inventory sources")
    for source in listed:
        source_kind(source)
    require(len(set(listed)) == len(listed), "inventory source repeated")
    require(
        all(item["source"] is not None for item in items) and set(listed) == sources,
        "the inventory lists exactly the context's sources",
    )
    return captured


def check_defect(
    key: object,
    defect: object,
    *,
    case: dict,
    cutoff: datetime,
    frozen: date,
    withheld: set[str],
    barred: frozenset[str],
    mode: str,
) -> Defect:
    require(isinstance(key, str) and IDENTIFIER.fullmatch(key), "defect id")
    closed(defect, DEFECT_FIELDS)
    d: dict = defect  # type: ignore[assignment]
    require(d["severity"] in WEIGHT and d["class"] in CLASSES, "defect rubric")
    review = case["review"]
    introduced = object_id(d["introduced"])
    require(introduced != review["merge_base"], "a defect is introduced above the merge base")
    require(introduced not in withheld, "a withheld commit cannot introduce a present defect")
    require(nonempty(d["summary"]), "defect summary")
    require(isinstance(d["location"], list) and d["location"], "defect location")
    for place in d["location"]:
        closed(place, "path lines")
        relative_path(place["path"])
        lines = place["lines"]
        require(
            lines is None
            or (
                isinstance(lines, list)
                and len(lines) == 2
                and all(integer(n, 1) for n in lines)
                and lines[0] <= lines[1]
            ),
            "defect lines",
        )
    found = d["discovery"]
    closed(found, DISCOVERY_FIELDS)
    require(found["source"] in DISCOVERY, "discovery source")
    require(found["evidence_status"] in EVIDENCE_STATUSES, "discovery evidence status")
    require((mode == "synthetic") == (found["evidence_status"] == "synthetic"), "discovery mode")
    require(found["artifact"] is None or nonempty(found["artifact"]), "discovery artifact")
    discovered = day(found["discovered"])
    require(cutoff.date() <= discovered <= frozen, "discovered between cutoff and freeze")
    require(found["finding"] is None or nonempty(found["finding"]), "discovery finding")
    origin = case["provenance"]
    reviewed = origin["round"]
    if found["source"] in ("same_review", "later_review"):
        require(integer(found["round"], 1), "a review's discovery names its round")
        if reviewed is not None:
            same = found["round"] == reviewed
            require(same == (found["source"] == "same_review"), "discovery round")
            require(found["round"] >= reviewed, "discovery round")
    else:
        require(found["round"] is None, "only a review's discovery names a round")
    if found["fix"] is not None:
        fix = object_id(found["fix"])
        require(fix in withheld, "a fix is withheld from the reviewer")
    if found["merged"] is not None:
        merged = object_id(found["merged"])
        require(
            origin["source"] == "pr_branch" and origin["pr"] is not None,
            "a merged revision follows a review round of a PR",
        )
        require(
            all(place["lines"] is not None for place in d["location"]),
            "a merged defect is located by its lines",
        )
        require(found["fix"] is not None, "a merged revision leads to a fix")
        require(merged in withheld, "a merged revision is withheld from the reviewer")
        require(merged not in (introduced, found["fix"]), "a merged revision is its own commit")
    check_labels(d["labels"], DEFECT_VERDICTS, frozen=frozen, barred=barred, since=cutoff.date())
    status = settle(d["labels"], critical_needs_two(d["severity"]))
    return Defect(
        severity=d["severity"],
        cls=d["class"],
        source=found["source"],
        discovered=discovered,
        status=status if status in ("confirmed", "rejected") else "unknown",
    )


def _check_provenance(origin: object, *, mode: str, review: dict) -> None:
    closed(origin, PROVENANCE_FIELDS)
    p: dict = origin  # type: ignore[assignment]
    require(p["source"] in SOURCES[mode], "provenance source for the corpus mode")
    for number in ("issue", "pr"):
        require(p[number] is None or integer(p[number], 1), "provenance number")
    require(nonempty(p["selection"]) and isinstance(p["note"], str), "provenance")
    require(p["base_evidence"] in BASE_EVIDENCES, "base evidence")
    merge_commit = p["merge_commit"]
    if merge_commit is not None:
        object_id(merge_commit)
    src = p["source"]
    if src == "synthetic":
        require(p["base_evidence"] == "synthetic", "synthetic base evidence")
        require(p["round"] is None or integer(p["round"], 1), "provenance round")
    elif src == "pr_branch":
        require(p["issue"] is not None and integer(p["round"], 1), "a branch round")
        require(p["base_evidence"] in ("recorded", "provisional"), "branch base evidence")
        require(merge_commit is None, "branch round has no merge commit")
    elif src == "squash":
        require(p["round"] is None and p["pr"] is not None, "a squash candidate names its PR")
        require(p["base_evidence"] == "squash_parent", "squash base evidence")
        require(review["base_tip"] == review["merge_base"], "squash base is merge base")
        require(merge_commit in (None, review["head"]), "squash merge commit")
    else:  # merge_pr
        require(p["round"] is None and p["pr"] is not None, "a merge_pr candidate names its PR")
        require(p["base_evidence"] == "merge_parent", "merge_pr base evidence")
        require(merge_commit is not None, "merge_pr requires merge_commit")


def check_case(
    case_id: object,
    raw_case: object,
    *,
    mode: str,
    frozen: date,
    visibility: str,
    declared_env: dict,
) -> Case:
    require(isinstance(case_id, str) and IDENTIFIER.fullmatch(case_id), "case id")
    closed(raw_case, CASE_FIELDS)
    case: dict = raw_case  # type: ignore[assignment]
    require(case["stratum"] in STRATA, "case stratum")
    require(isinstance(case["cluster"], str) and IDENTIFIER.fullmatch(case["cluster"]), "cluster")

    review = case["review"]
    closed(review, REVIEW_FIELDS)
    require(nonempty(review["base_ref"]), "base ref")
    head, base = object_id(review["head"]), object_id(review["merge_base"])
    tip = object_id(review["base_tip"])
    for tree_key in ("base_tree", "merge_base_tree", "head_tree"):
        object_id(review[tree_key])
    require(head != base, "the candidate is above its merge base")
    require(tip != head, "the base revision is not the candidate")
    cutoff = instant(review["cutoff"])
    require(cutoff.date() <= frozen, "a candidate reviewed by the freeze")

    _check_provenance(case["provenance"], mode=mode, review=review)
    origin = case["provenance"]

    environment = case["environment"]
    closed(environment, ENV_DECL_FIELDS)
    require(isinstance(environment["files"], dict), "environment files")
    for path, blob in environment["files"].items():
        relative_path(path)
        if blob is not None:
            object_id(blob)
    require(environment["validation_scope"] == "full", "only FULL validation scope is admitted")
    require(
        isinstance(environment["validation"], list)
        and all(nonempty(cmd) for cmd in environment["validation"]),
        "environment validation",
    )
    require(
        set(environment["files"]) == set(declared_env["files"]),
        "a case pins each of the repository's environment files",
    )
    require(
        environment["validation"] == declared_env["validation"],
        "a case validates with the repository's declared commands",
    )

    withheld = case["withheld"]
    require(isinstance(withheld, list), "withheld commits")
    for commit in withheld:
        object_id(commit)
    require(len(set(withheld)) == len(withheld), "withheld commit repeated")
    forbidden_self = {head, base, tip}
    if origin["merge_commit"] is not None and origin["source"] == "merge_pr":
        forbidden_self.add(origin["merge_commit"])
    require(not forbidden_self & set(withheld), "the candidate is not withheld")

    post_refs = case["post_cutoff_refs"]
    require(
        isinstance(post_refs, list)
        and len(post_refs) <= MAX_POST_CUTOFF_REFS
        and all(nonempty(ref) for ref in post_refs),
        "post_cutoff_refs",
    )

    actors = case["actors"]
    require(
        isinstance(actors, list) and actors and all(nonempty(a) for a in actors),
        "historical actors",
    )
    barred = frozenset(a.strip() for a in actors)
    require(len(barred) == len(actors), "historical actor repeated")

    risk = case["risk"]
    closed(risk, "stratum rationale")
    require(risk["stratum"] in RISKS and nonempty(risk["rationale"]), "risk stratum")

    exposure_doc = case["exposure"]
    closed(exposure_doc, EXPOSURE_FIELDS)
    require(nonempty(exposure_doc["artifact"]), "exposure artifact")
    possible_from = (
        None if exposure_doc["possible_from"] is None else day(exposure_doc["possible_from"])
    )
    public_by = None if exposure_doc["public_by"] is None else day(exposure_doc["public_by"])
    require(
        possible_from is None or possible_from <= cutoff.date(), "exposure possible by the cutoff"
    )
    require(public_by is None or public_by <= frozen, "exposure public by the freeze")
    require(
        None in (possible_from, public_by) or possible_from <= public_by,
        "exposure possible before it was public",
    )

    on_context = check_context(case["context"], origin=origin, cutoff=cutoff, frozen=frozen)
    texts = list(reviewer_text(case))
    require(
        sum(len(t.encode("utf-8")) for t in texts) <= MAX_REVIEWER_TEXT_BYTES,
        "reviewer text size",
    )
    forbidden = set(withheld)
    check_text_leakage(texts, forbidden, post_refs)

    require(
        isinstance(case["defects"], dict) and len(case["defects"]) <= MAX_DEFECTS_PER_CASE,
        "defects",
    )
    if STRATA[case["stratum"]]:
        require(case["defects"] and case["negative"] is None, "a defect case registers its defects")
    else:
        require(not case["defects"], "a negative case registers no defect")

    defects = {
        key: check_defect(
            key,
            defect,
            case=case,
            cutoff=cutoff,
            frozen=frozen,
            withheld=forbidden,
            barred=barred,
            mode=mode,
        )
        for key, defect in case["defects"].items()
    }

    synthetic = mode == "synthetic"
    evidenced = (
        synthetic or origin["source"] != "pr_branch" or origin["base_evidence"] == "recorded"
    )
    lineaged = all(
        synthetic
        or (d["discovery"]["fix"] is None and d["discovery"]["source"] not in AFTER_MERGE)
        or d["discovery"]["evidence_status"] == "recorded"
        for d in case["defects"].values()
    ) and all(
        synthetic
        or d["discovery"]["source"] not in AFTER_MERGE
        or (
            d["discovery"]["fix"] is not None
            and (origin["source"] in ("squash", "merge_pr") or d["discovery"]["merged"] is not None)
        )
        for d in case["defects"].values()
    )
    pinned = (not declared_env["files"]) or any(
        blob is not None for blob in environment["files"].values()
    )

    negative: str | None = None
    all_labels = [lbl for d in case["defects"].values() for lbl in d["labels"]]
    if STRATA[case["stratum"]]:
        sources = {d.source for d in defects.values()}
        require(sources & STRATUM_SOURCES[case["stratum"]], "a defect of the case's stratum")
    else:
        neg = case["negative"]
        closed(neg, "observed_through oracle_status labels")
        through = day(neg["observed_through"])
        require(cutoff.date() <= through <= frozen, "observed between cutoff and freeze")
        require(neg["oracle_status"] in ORACLE_STATUSES, "oracle status")
        all_labels = neg["labels"]
        check_labels(
            all_labels, NEGATIVE_VERDICTS, frozen=frozen, barred=barred, since=cutoff.date()
        )
        verdict = settle(all_labels)
        if verdict == "defective":
            negative = "refuted"
        elif verdict != "clean":
            negative = "unlabeled"
        elif neg["oracle_status"] != "pass":
            negative = "unverified_oracle"
        elif (through - cutoff.date()).days < MIN_NEGATIVE_WINDOW_DAYS:
            negative = "censored"
        else:
            negative = "settled"

    runnable = on_context and evidenced and lineaged and pinned and (negative != "refuted")
    return Case(
        id=case_id,  # type: ignore[arg-type]
        cluster=case["cluster"],
        stratum=case["stratum"],
        risk=risk["stratum"],
        source=origin["source"],
        issue=origin["issue"],
        pr=origin["pr"],
        round=origin["round"],
        cutoff=cutoff,
        actors=barred,
        labelers=frozenset(lbl["by"].strip() for lbl in all_labels),
        defects=defects,
        negative=negative,
        runnable=runnable,
        input=input_digest(case),
        visibility=visibility,
        possible_from=possible_from,
        public_by=public_by,
    )


def check_clusters(corpus: dict, cases: dict[str, Case]) -> None:
    """Cases sharing an issue, PR, or defect/merge commit share one cluster."""
    owner: dict[tuple[str, str | int], str] = {}
    for case_id, case in sorted(corpus["cases"].items()):
        origin = case["provenance"]
        keys: list[tuple[str, str | int | None]] = [
            ("issue", origin["issue"]),
            ("pr", origin["pr"]),
            ("commit", origin["merge_commit"]),
        ]
        for defect in case["defects"].values():
            keys.append(("commit", defect["introduced"]))
            keys.append(("commit", defect["discovery"]["fix"]))
            keys.append(("commit", defect["discovery"]["merged"]))
        cluster = cases[case_id].cluster
        for kind, val in keys:
            if val is not None:
                require(
                    owner.setdefault((kind, val), cluster) == cluster,
                    "cases of one issue, PR or defect share a cluster",
                )


def check_defects(corpus: dict, cases: dict[str, Case]) -> None:
    """Enforce consistent corpus-wide defect identity and settled status."""
    seen: dict[str, tuple[tuple, tuple]] = {}
    for case_id, case in sorted(corpus["cases"].items()):
        origin = case["provenance"]
        scope = ("pr", origin["pr"]) if origin["pr"] is not None else ("issue", origin["issue"])
        for key, raw_d in sorted(case["defects"].items()):
            found = raw_d["discovery"]
            lineage_key = (
                scope,
                raw_d["introduced"],
                tuple(sorted({p["path"] for p in raw_d["location"]})),
                found["round"],
                found["finding"],
                found["discovered"],
                found["merged"],
                found["fix"],
            )
            identity = (
                raw_d["severity"],
                raw_d["class"],
                raw_d["summary"],
                cases[case_id].defects[key].status,
            )
            prev = seen.setdefault(key, (lineage_key, identity))
            require(prev == (lineage_key, identity), "a defect id names one defect")


def check_merges(corpus: dict) -> None:
    """One PR merges as at most one commit across squash, merge_pr, and lineages."""
    merges: dict[int, str] = {}
    for _, case in sorted(corpus["cases"].items()):
        origin = case["provenance"]
        pr = origin["pr"]
        if pr is None:
            continue
        named: list[str] = []
        if origin["source"] == "squash":
            named.append(case["review"]["head"])
        elif origin["source"] == "merge_pr":
            named.append(origin["merge_commit"])
        for _, defect in sorted(case["defects"].items()):
            if defect["discovery"]["merged"] is not None:
                named.append(defect["discovery"]["merged"])
        for merged in named:
            require(merges.setdefault(pr, merged) == merged, "one PR merged as one commit")


def check_rounds(corpus: dict) -> None:
    """Each review round of a PR (or issue when PR is null) is at most one case,
    and each (base_ref, base_tip, merge_base, head) candidate appears once."""
    pr_rounds: dict[tuple[int, int], str] = {}
    issue_rounds: dict[tuple[int, int], str] = {}
    targets: dict[tuple[str, str, str, str], str] = {}
    for case_id, case in sorted(corpus["cases"].items()):
        origin = case["provenance"]
        rev = case["review"]
        target = (rev["base_ref"], rev["base_tip"], rev["merge_base"], rev["head"])
        require(targets.setdefault(target, case_id) == case_id, "duplicate review candidate")
        if origin["round"] is not None:
            if origin["pr"] is not None:
                require(
                    pr_rounds.setdefault((origin["pr"], origin["round"]), case_id) == case_id,
                    "one review round of a PR is one case",
                )
            elif origin["issue"] is not None:
                require(
                    issue_rounds.setdefault((origin["issue"], origin["round"]), case_id) == case_id,
                    "one review round of an issue is one case",
                )


def check_discoveries(corpus: dict, cases: dict[str, Case]) -> None:
    """A review's discovery cannot predate the cutoff of the round it credits."""
    pr_starts: dict[tuple[int, int], datetime] = {}
    issue_starts: dict[tuple[int, int], datetime] = {}
    for case_id, case in sorted(corpus["cases"].items()):
        origin = case["provenance"]
        if origin["round"] is None:
            continue
        if origin["pr"] is not None:
            pr_starts[(origin["pr"], origin["round"])] = cases[case_id].cutoff
        elif origin["issue"] is not None:
            issue_starts[(origin["issue"], origin["round"])] = cases[case_id].cutoff

    for _, case in sorted(corpus["cases"].items()):
        origin = case["provenance"]
        for _, defect in sorted(case["defects"].items()):
            found = defect["discovery"]
            if found["round"] is None:
                continue
            start = (
                pr_starts.get((origin["pr"], found["round"]))
                if origin["pr"] is not None
                else (
                    issue_starts.get((origin["issue"], found["round"]))
                    if origin["issue"] is not None
                    else None
                )
            )
            require(
                start is None or start.date() <= day(found["discovered"]),
                "a review's discovery is no earlier than its round",
            )


def check_cross_case_leakage(corpus: dict, cases: dict[str, Case]) -> None:
    """An earlier round of the same PR (or issue when PR is null) cannot contain
    commits introduced/reviewed/withheld by a later round or merge."""
    raw_cases = corpus["cases"]
    items = sorted(raw_cases.items())
    for cid, raw_c in items:
        case = cases[cid]
        future_shas: set[str] = set()
        for other_id, other_raw in items:
            if other_id == cid:
                continue
            other = cases[other_id]
            same_thread = (case.pr is not None and other.pr == case.pr) or (
                (case.pr is None or other.pr is None)
                and case.issue is not None
                and other.issue == case.issue
            )
            if not same_thread or other.cutoff <= case.cutoff:
                continue
            future_shas.add(other_raw["review"]["head"])
            if other_raw["provenance"]["merge_commit"] is not None:
                future_shas.add(other_raw["provenance"]["merge_commit"])
            future_shas.update(other_raw["withheld"])
        if future_shas:
            check_text_leakage(list(reviewer_text(raw_c)), future_shas, ())


def check_corpus(corpus: object) -> Corpus:
    closed(corpus, CORPUS_FIELDS)
    doc: dict = corpus  # type: ignore[assignment]
    require(doc["format"] == CORPUS_FORMAT, "corpus format")
    require(type(doc["version"]) is int and doc["version"] == VERSION, "corpus version")
    require(doc["mode"] in SOURCES, "corpus mode")
    require(isinstance(doc["name"], str) and IDENTIFIER.fullmatch(doc["name"]), "name")
    require(integer(doc["release"], 1), "corpus release")
    frozen = day(doc["frozen"])

    repository = doc["repository"]
    closed(repository, REPOSITORY_FIELDS)
    require(nonempty(repository["slug"]), "repository slug")
    object_id(repository["root"])
    require(repository["visibility"] in VISIBILITIES, "repository visibility")
    require(
        (doc["mode"] == "synthetic") == (repository["visibility"] == "synthetic"), "visibility mode"
    )
    refs = repository["refs"]
    require(isinstance(refs, dict) and refs, "repository refs")
    for ref, tip in refs.items():
        require(nonempty(ref), "repository refs")
        object_id(tip)

    declared_env = repository["environment"]
    closed(declared_env, ENV_DECL_FIELDS)
    files, commands = declared_env["files"], declared_env["validation"]
    require(isinstance(files, list), "repository environment")
    for path in files:
        relative_path(path)
    require(len(set(files)) == len(files), "repository environment file repeated")
    require(declared_env["validation_scope"] == "full", "only FULL validation scope is admitted")
    require(
        isinstance(commands, list) and all(nonempty(c) for c in commands),
        "repository environment",
    )

    raw_cases = doc["cases"]
    require(isinstance(raw_cases, dict) and 1 <= len(raw_cases) <= MAX_CASES, "registered cases")
    cases = {
        cid: check_case(
            cid,
            c,
            mode=doc["mode"],
            frozen=frozen,
            visibility=repository["visibility"],
            declared_env=declared_env,
        )
        for cid, c in sorted(raw_cases.items())
    }
    for c in raw_cases.values():
        require(c["review"]["base_ref"] in refs, "a case's base ref is frozen")

    check_defects(doc, cases)
    check_merges(doc)
    check_rounds(doc)
    check_discoveries(doc, cases)
    check_cross_case_leakage(doc, cases)
    check_clusters(doc, cases)

    inputs = [c.input for c in cases.values()]
    require(len(set(inputs)) == len(inputs), "two cases with one review input")
    return Corpus(
        name=doc["name"],
        release=doc["release"],
        mode=doc["mode"],
        frozen=frozen,
        visibility=repository["visibility"],
        digest=corpus_digest(doc),
        cases=cases,
    )


def label_summary(cases: Iterable[Case]) -> dict:
    case_list = list(cases)
    defects = [d for c in case_list for d in c.defects.values()]

    def statuses(selected: Iterable[Defect]) -> dict[str, int]:
        tally = Counter(d.status for d in selected)
        return {s: tally[s] for s in ("confirmed", "rejected", "unknown")}

    negatives = Counter(c.negative for c in case_list if c.negative is not None)
    return {
        "defects": statuses(defects),
        "defects_by_severity": {
            s: statuses(d for d in defects if d.severity == s) for s in SEVERITIES
        },
        "defects_by_discovery": {
            s: statuses(d for d in defects if d.source == s) for s in DISCOVERY
        },
        "negatives": {
            s: negatives[s]
            for s in ("settled", "censored", "unlabeled", "unverified_oracle", "refuted")
        },
    }


def describe(corpus: dict) -> dict:
    """Validate and summarize a corpus without any submission."""
    checked = check_corpus(corpus)
    cases = list(checked.cases.values())
    runnable = sorted(c.id for c in cases if c.runnable)
    unplanned = sorted(c.id for c in cases if not c.runnable)
    return {
        "corpus": {
            "name": checked.name,
            "release": checked.release,
            "mode": checked.mode,
            "visibility": checked.visibility,
            "frozen": checked.frozen.isoformat(),
            "sha256": checked.digest,
        },
        "cases": len(checked.cases),
        "clusters": len({c.cluster for c in cases}),
        "strata": dict(sorted(Counter(c.stratum for c in cases).items())),
        "risk": dict(sorted(Counter(c.risk for c in cases).items())),
        "runnable": runnable,
        "unplanned": unplanned,
        "labels": label_summary(cases),
        "inputs": {c.id: c.input for c in cases},
    }


# -- read-only batched git verification ---------------------------------------

GIT_ENV_KEEP = ("PATH", "LANG", "LANGUAGE", "TMPDIR")
GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_OPTIONAL_LOCKS": "0",
}
GIT_SWITCHES = (
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
    "core.commitGraph=false",
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)


@dataclass(frozen=True)
class CommitObj:
    oid: str
    tree: str
    parents: tuple[str, ...]
    committed: int
    subject: str


class Git:
    """Hardened read-only local git inspector with batched object authentication."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.env = {
            k: v for k, v in os.environ.items() if k in GIT_ENV_KEEP or k.startswith("LC_")
        } | GIT_ENV
        self.commits: dict[str, CommitObj] = {}
        self.blobs: dict[str, bytes] = {}
        self.lines: dict[str, list[tuple[str, int]] | None] = {}
        self._ancestors: dict[tuple[str, str], bool | None] = {}
        self._path_cache: dict[tuple[str, str], str | None] = {}
        self._file_lines_cache: dict[tuple[str, str], list[bytes] | None] = {}

    def run(
        self, *args: str, stdin_bytes: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *GIT_SWITCHES, "-C", str(self.repo), *args],
            env=self.env,
            input=stdin_bytes,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )

    def out(self, *args: str) -> str | None:
        done = self.run(*args)
        return done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else None

    def load_objects(self, specs: Iterable[str]) -> dict[str, tuple[str, bytes] | None]:
        """Batch-load and SHA-1-authenticate git objects via one ``cat-file --batch``."""
        unique = list(dict.fromkeys(specs))
        if not unique:
            return {}
        payload = ("\n".join(unique) + "\n").encode("utf-8")
        done = self.run("cat-file", "--batch", stdin_bytes=payload)
        if done.returncode != 0:
            return dict.fromkeys(unique, None)
        buf = done.stdout
        pos = 0
        results: dict[str, tuple[str, bytes] | None] = {}
        for spec in unique:
            nl = buf.find(b"\n", pos)
            if nl == -1:
                results[spec] = None
                continue
            header = buf[pos:nl].decode("utf-8", "replace")
            pos = nl + 1
            if header.endswith(" missing") or header.endswith(" ambiguous"):
                results[spec] = None
                continue
            parts = header.rsplit(" ", 2)
            if len(parts) != 3 or not parts[2].isdigit():
                results[spec] = None
                continue
            oid, kind, size_s = parts
            size = int(size_s)
            body = buf[pos : pos + size]
            if len(body) != size:
                results[spec] = None
                continue
            pos += size + 1  # skip trailing LF
            # Authenticate object bytes against OID to defeat corrupted/substituted stores.
            actual_oid = hashlib.sha1(f"{kind} {size}\0".encode() + body).hexdigest()
            if actual_oid != oid or (OBJECT_ID.fullmatch(spec) and oid != spec):
                results[spec] = None
                continue
            results[spec] = (kind, body)
            if kind == "commit":
                self._index_commit(oid, body)
            elif kind == "blob":
                self.blobs[oid] = body
        return results

    def _index_commit(self, oid: str, body: bytes) -> None:
        if oid in self.commits:
            return
        hdr_bytes, _, msg_bytes = body.partition(b"\n\n")
        tree = ""
        parents: list[str] = []
        committed = -1
        for line in hdr_bytes.decode("utf-8", "replace").splitlines():
            if line.startswith("tree "):
                tree = line[5:].strip()
            elif line.startswith("parent "):
                parents.append(line[7:].strip())
            elif line.startswith("committer "):
                tokens = line.rsplit(" ", 2)
                if len(tokens) >= 2 and tokens[-2].isdigit():
                    committed = int(tokens[-2])
        subject = msg_bytes.decode("utf-8", "replace").partition("\n")[0]
        if OBJECT_ID.fullmatch(tree) and committed >= 0:
            self.commits[oid] = CommitObj(
                oid=oid,
                tree=tree,
                parents=tuple(parents),
                committed=committed,
                subject=subject,
            )

    def commit(self, oid: str) -> CommitObj | None:
        if oid not in self.commits:
            self.load_objects([oid])
        return self.commits.get(oid)

    def is_ancestor(self, older: str, newer: str) -> bool | None:
        if older == newer:
            return True
        newer_obj = self.commits.get(newer)
        if newer_obj is not None and older in newer_obj.parents:
            return True
        key = (older, newer)
        if key not in self._ancestors:
            code = self.run("merge-base", "--is-ancestor", older, newer).returncode
            self._ancestors[key] = {0: True, 1: False}.get(code)
        return self._ancestors[key]

    def line(self, tip: str) -> list[tuple[str, int]] | None:
        if tip not in self.lines:
            listed = self.out("rev-list", "--first-parent", "--timestamp", tip)
            if not listed:
                self.lines[tip] = None
            else:
                out: list[tuple[str, int]] = []
                for row in listed.splitlines():
                    stamp, _, oid = row.partition(" ")
                    if not stamp.isdigit() or not OBJECT_ID.fullmatch(oid):
                        out = []
                        break
                    out.append((oid, int(stamp)))
                self.lines[tip] = out or None
        return self.lines[tip]

    def merge_bases(self, one: str, other: str) -> list[str] | None:
        found = self.out("merge-base", "--all", one, other)
        return sorted(found.split()) if found else None

    def changed(self, commit: str) -> set[str] | None:
        done = self.run(
            "diff-tree", "-r", "-z", "--name-only", "--no-renames", f"{commit}^1", commit
        )
        if done.returncode != 0:
            return None
        return {p.decode("utf-8", "replace") for p in done.stdout.split(b"\0") if p}

    def resolve_path(self, commit: str, path: str) -> str | None:
        key = (commit, path)
        if key not in self._path_cache:
            done = self.run(
                "rev-parse", "--verify", "--quiet", "--end-of-options", f"{commit}:{path}"
            )
            self._path_cache[key] = done.stdout.decode().strip() if done.returncode == 0 else None
        return self._path_cache[key]

    def file_lines(self, commit: str, path: str) -> list[bytes] | None:
        key = (commit, path)
        if key not in self._file_lines_cache:
            done = self.run("cat-file", "blob", f"{commit}:{path}")
            if done.returncode != 0:
                self._file_lines_cache[key] = None
            else:
                body = done.stdout
                lines = body.split(b"\n")
                if body.endswith(b"\n") or not body:
                    lines.pop()
                self._file_lines_cache[key] = lines
        return self._file_lines_cache[key]

    def hunks(self, commit: str, path: str) -> list[tuple[int, int]] | None:
        done = self.run(
            "diff-tree",
            "-p",
            "-U0",
            "--no-renames",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            f"{commit}^1",
            commit,
            "--",
            path,
        )
        if done.returncode != 0:
            return None
        return [(int(start), int(count or b"1")) for start, count in HUNK.findall(done.stdout)]


def utc_day(stamp: int) -> date:
    return datetime.fromtimestamp(stamp, UTC).date()


def line_tip(line: list[tuple[str, int]], cutoff: int) -> str | None:
    """Newest commit on ``line`` at or before ``cutoff`` when committer times split cleanly."""
    newer = 0
    while newer < len(line) and line[newer][1] > cutoff:
        newer += 1
    if newer == len(line) or any(stamp > cutoff for _, stamp in line[newer:]):
        return None
    return line[newer][0]


def found_once(lines: list[bytes] | None, block: list[bytes]) -> tuple[int, int] | None:
    if lines is None or not block:
        return None
    size = len(block)
    starts = [at for at in range(len(lines) - size + 1) if lines[at : at + size] == block]
    return (starts[0] + 1, starts[0] + size) if len(starts) == 1 else None


def touches(hunk: tuple[int, int], span: tuple[int, int]) -> bool:
    start, count = hunk
    first, last = span
    if count:
        return start <= last and start + count - 1 >= first
    return first - 1 <= start <= last


def located(git: Git, head: str, place: dict) -> list[bytes] | None:
    lines = git.file_lines(head, place["path"])
    first, last = place["lines"]
    if lines is None or last > len(lines):
        return None
    return lines[first - 1 : last]


def addresses(git: Git, head: str, fix: str, location: list[dict]) -> bool:
    changed = git.changed(fix)
    if changed is None:
        return False
    for place in location:
        if place["path"] not in changed:
            continue
        if place["lines"] is None:
            return True
        block = located(git, head, place)
        span = found_once(git.file_lines(f"{fix}^1", place["path"]), block or [])
        hunks = git.hunks(fix, place["path"]) if span else None
        if span and hunks and any(touches(hunk, span) for hunk in hunks):
            return True
    return False


def squashes(subject: str | None, pr: int | None) -> bool:
    return pr is not None and subject is not None and subject.endswith(f" (#{pr})")


def _collect_corpus_oids(corpus: dict) -> list[str]:
    repo = corpus["repository"]
    oids: list[str] = [repo["root"], *repo["refs"].values()]
    for case in corpus["cases"].values():
        rev = case["review"]
        oids.extend((rev["head"], rev["base_tip"], rev["merge_base"]))
        if case["provenance"]["merge_commit"] is not None:
            oids.append(case["provenance"]["merge_commit"])
        oids.extend(b for b in case["environment"]["files"].values() if b is not None)
        oids.extend(case["withheld"])
        for defect in case["defects"].values():
            oids.append(defect["introduced"])
            if defect["discovery"]["fix"] is not None:
                oids.append(defect["discovery"]["fix"])
            if defect["discovery"]["merged"] is not None:
                oids.append(defect["discovery"]["merged"])
    return oids


def verify_case(git: Git, case: dict, repository: dict) -> list[str]:
    failed: list[str] = []

    def check(ok: object, rule: str) -> None:
        if ok is not True:
            failed.append(rule)

    review = case["review"]
    origin = case["provenance"]
    source = origin["source"]
    head, tip, base = review["head"], review["base_tip"], review["merge_base"]
    cutoff = int(instant(review["cutoff"]).timestamp())

    head_obj = git.commit(head)
    base_obj = git.commit(base)
    tip_obj = git.commit(tip)
    check(head_obj is not None, "head is a commit")
    check(base_obj is not None, "merge base is a commit")
    check(tip_obj is not None, "base revision is a commit")
    line = git.line(repository["refs"][review["base_ref"]])
    check(line is not None, "the base ref's frozen tip is a commit")
    if failed or head_obj is None or base_obj is None or tip_obj is None or line is None:
        return failed

    on_line = {commit for commit, _ in line}
    check(head_obj.tree == review["head_tree"], "head tree")
    check(tip_obj.tree == review["base_tree"], "base tree")
    check(base_obj.tree == review["merge_base_tree"], "merge base tree")
    check(git.is_ancestor(repository["root"], head), "repository root is an ancestor of head")

    if source == "squash":
        check(head_obj.committed == cutoff, "the cutoff is the head's committer time")
        check(head in on_line, "a squash candidate is on its base ref's line")
        check(len(head_obj.parents) == 1, "a squash candidate has one parent")
        check(head_obj.parents[:1] == (base,), "a squash candidate's parent is its merge base")
        check(squashes(head_obj.subject, origin["pr"]), "a squash candidate is its PR's squash")
    elif source == "merge_pr":
        merge_oid = origin["merge_commit"]
        merge_obj = git.commit(merge_oid)
        check(merge_obj is not None, "merge_commit is a commit")
        if merge_obj is not None:
            check(merge_oid in on_line, "merge_commit is on its base ref's line")
            check(
                merge_obj.parents == (tip, head),
                "merge_commit has base_tip and head as parents",
            )
            check(merge_obj.committed == cutoff, "the cutoff is the merge_commit's committer time")
            check(head_obj.committed <= cutoff, "head was committed by cutoff")
    elif origin["base_evidence"] == "recorded":
        check(head_obj.committed <= cutoff, "the head was committed by the review's start")
        check(tip in on_line, "the base revision is on its ref's line")
        check(tip_obj.committed <= cutoff, "the base revision was committed by the review's start")
    else:
        check(head_obj.committed == cutoff, "the cutoff is the head's committer time")
        at = line_tip(line, cutoff)
        if at is None:
            failed.append("the base ref's line splits at the cutoff")
        else:
            check(at == tip, "the base revision is its ref's tip at the cutoff")

    check(git.merge_bases(tip, head) == [base], "the merge base is the base revision's and head's")

    # Batch-check <head>:<path> blobs via rev-parse / cat-file
    for index, (path, blob) in enumerate(sorted(case["environment"]["files"].items())):
        found = git.resolve_path(head, path)
        if blob is None:
            check(found is None, f"environment file {index} is absent at the head")
        else:
            check(found == blob and blob in git.blobs, f"environment file {index}")

    for commit in case["withheld"]:
        if git.commit(commit) is None:
            failed.append(f"withheld {commit[:12]} is a commit")
            continue
        check(git.is_ancestor(commit, head) is False, f"withheld {commit[:12]} is not in head")
        check(git.is_ancestor(commit, tip) is False, f"withheld {commit[:12]} is not in the base")

    introduced_stamps: list[int] = []
    for key, defect in sorted(case["defects"].items()):
        introduced = defect["introduced"]
        intro_obj = git.commit(introduced)
        if intro_obj is None:
            failed.append(f"defect {key} introduced by a commit")
            continue
        introduced_stamps.append(intro_obj.committed)
        check(git.is_ancestor(introduced, head), f"defect {key} introduced in head")
        check(git.is_ancestor(introduced, base) is False, f"defect {key} above the merge base")

        fix, merged = defect["discovery"]["fix"], defect["discovery"]["merged"]
        if fix is not None:
            fix_obj = git.commit(fix)
            if fix_obj is None:
                failed.append(f"defect {key} fix is a commit")
            else:
                if merged is None:
                    check(
                        git.is_ancestor(introduced, fix),
                        f"defect {key} fix follows its introduction",
                    )
                    if source == "squash":
                        check(git.is_ancestor(head, fix), f"defect {key} fix follows its merge")
                        if defect["discovery"]["source"] in AFTER_MERGE:
                            check(
                                utc_day(head_obj.committed)
                                <= day(defect["discovery"]["discovered"]),
                                f"defect {key} found no earlier than its merge",
                            )
                    elif source == "merge_pr":
                        merge_oid = origin["merge_commit"]
                        check(
                            git.is_ancestor(merge_oid, fix), f"defect {key} fix follows its merge"
                        )
                        m_obj = git.commit(merge_oid)
                        if m_obj is not None and defect["discovery"]["source"] in AFTER_MERGE:
                            check(
                                utc_day(m_obj.committed) <= day(defect["discovery"]["discovered"]),
                                f"defect {key} found no earlier than its merge",
                            )
                else:
                    merged_obj = git.commit(merged)
                    if merged_obj is None:
                        failed.append(f"defect {key} merge is a commit")
                    else:
                        paths = {place["path"] for place in defect["location"]}
                        changed = git.changed(merged)
                        check(merged in on_line, f"defect {key} merged on its base ref's line")
                        check(
                            squashes(merged_obj.subject, origin["pr"])
                            and len(merged_obj.parents) == 1,
                            f"defect {key} merge is its PR's squash",
                        )
                        check(
                            changed is not None and paths <= changed,
                            f"defect {key} merge carries it",
                        )
                        check(
                            changed is not None
                            and all(
                                found_once(
                                    git.file_lines(merged, place["path"]),
                                    located(git, head, place) or [],
                                )
                                for place in defect["location"]
                            ),
                            f"defect {key} survives its merge",
                        )
                        check(git.is_ancestor(merged, fix), f"defect {key} fix follows its merge")
                        if defect["discovery"]["source"] in AFTER_MERGE:
                            check(
                                utc_day(merged_obj.committed)
                                <= day(defect["discovery"]["discovered"]),
                                f"defect {key} found no earlier than its merge",
                            )
                check(
                    addresses(git, head, fix, defect["location"]),
                    f"defect {key} fix changes what it is located in",
                )

        for index, place in enumerate(defect["location"]):
            lines = git.file_lines(head, place["path"])
            span = place["lines"]
            check(
                lines is not None and (span is None or span[1] <= len(lines)),
                f"defect {key} location {index}",
            )

    possible = case["exposure"]["possible_from"]
    if possible is not None:
        stamps = [head_obj.committed, *introduced_stamps]
        check(
            all(day(possible) <= utc_day(s) for s in stamps),
            "exposure possible no later than the commits it is about",
        )
    return failed


def verify(corpus: dict, repo: Path, *, checked: Corpus | None = None) -> dict:
    """Re-derive every pin of a historical corpus from a local git object store."""
    if checked is None:
        checked = check_corpus(corpus)
    require(checked.mode == "historical", "only a historical corpus names real objects")
    git = Git(repo)
    git.load_objects(_collect_corpus_oids(corpus))
    repository = corpus["repository"]
    cases = {cid: verify_case(git, corpus["cases"][cid], repository) for cid in checked.cases}
    return {
        "corpus": checked.digest,
        "ok": not any(cases.values()),
        "cases": {
            cid: {
                "ok": not failed,
                "failed": failed,
                "base_evidence": corpus["cases"][cid]["provenance"]["base_evidence"],
            }
            for cid, failed in cases.items()
        },
    }


# -- submissions --------------------------------------------------------------

SUBMISSION_FIELDS = "format version mode corpus frozen baseline plan variants runs"
VARIANT_FIELDS = (
    "identity provider provider_version model effort knowledge_cutoff prompt controller sandbox"
)
RUN_FIELDS = "variant case seed input pins_sha256 status exclusion cost_microusd findings"
FINDING_FIELDS = "id classification title location labels"


@dataclass(frozen=True)
class Run:
    variant: str
    case: Case
    seed: int
    status: str
    cost: int | None
    findings: tuple[tuple[str, str, str | None], ...]


def pins_digest(variant: dict) -> str:
    return sha256(canonical(variant)).hexdigest()


def check_variant(name: object, variant: object) -> str:
    require(isinstance(name, str) and IDENTIFIER.fullmatch(name), "variant id")
    closed(variant, VARIANT_FIELDS)
    v: dict = variant  # type: ignore[assignment]
    for key in ("identity", "provider", "provider_version", "model", "effort", "sandbox"):
        require(nonempty(v[key]), f"variant {key}")
    if v["knowledge_cutoff"] is not None:
        day(v["knowledge_cutoff"])
    closed(v["prompt"], "template sha256")
    relative_path(v["prompt"]["template"])
    digest64(v["prompt"]["sha256"], "prompt digest")
    object_id(v["controller"])
    return v["identity"].strip()


def finding_verdict(finding: dict, case: Case) -> tuple[str, str, str | None]:
    def need(verdict: object) -> int:
        kind, key = verdict  # type: ignore[misc]
        return 2 if kind == "match" and case.defects[key].severity == "critical" else 1

    settled = settle(finding["labels"], need)
    if settled is None:
        return finding["classification"], "unknown", None
    if settled[0] == "false":
        return finding["classification"], "false", None
    status = case.defects[settled[1]].status
    verdict = {"confirmed": "confirmed", "rejected": "false"}.get(status, "unknown")
    return finding["classification"], verdict, settled[1] if verdict == "confirmed" else None


def check_run(
    raw_run: object,
    *,
    corpus: Corpus,
    pins: dict[str, str],
    plan: dict,
    frozen: date,
    barred: frozenset[str],
) -> Run:
    closed(raw_run, RUN_FIELDS)
    run: dict = raw_run  # type: ignore[assignment]
    require(run["variant"] in pins, "run of a registered variant")
    require(run["case"] in plan["cases"] and integer(run["seed"], 1), "planned case")
    require(run["seed"] in plan["seeds"], "planned seed")
    case = corpus.cases[run["case"]]
    require(run["input"] == case.input, "a run reviews the case's frozen input")
    require(
        run["pins_sha256"] == pins[run["variant"]], "a run was launched with its variant's pins"
    )
    require(run["cost_microusd"] is None or integer(run["cost_microusd"]), "run cost")
    status = run["status"]
    require(status in ("completed", "failed", "excluded"), "run status")
    if status == "excluded":
        require(run["exclusion"] in EXCLUSIONS, "documented exclusion")
    else:
        require(run["exclusion"] is None, "only an excluded run names an exclusion")
    if status != "completed":
        require(run["findings"] is None, "only a completed run holds findings")
        return Run(run["variant"], case, run["seed"], status, run["cost_microusd"], ())
    require(
        isinstance(run["findings"], list) and len(run["findings"]) <= MAX_FINDINGS_PER_RUN,
        "a completed run holds its findings",
    )
    ids: set[str] = set()
    findings: list[tuple[str, str, str | None]] = []
    for finding in run["findings"]:
        closed(finding, FINDING_FIELDS)
        require(nonempty(finding["id"]) and finding["id"] not in ids, "finding id")
        ids.add(finding["id"])
        require(finding["classification"] in CLASSIFICATIONS, "finding classification")
        require(nonempty(finding["title"]) and nonempty(finding["location"]), "finding text")
        check_labels(
            finding["labels"],
            MATCH_VERDICTS,
            frozen=frozen,
            barred=barred | case.actors,
            match=case.defects,
            since=case.cutoff.date(),
        )
        findings.append(finding_verdict(finding, case))
    return Run(run["variant"], case, run["seed"], status, run["cost_microusd"], tuple(findings))


def check_submissions(corpus: Corpus, submissions: object) -> list[Run]:
    closed(submissions, SUBMISSION_FIELDS)
    sub: dict = submissions  # type: ignore[assignment]
    require(sub["format"] == SUBMISSIONS_FORMAT, "submissions format")
    require(type(sub["version"]) is int and sub["version"] == VERSION, "submissions version")
    require(sub["mode"] == SUBMISSION_MODES[corpus.mode], "submissions mode for corpus")
    require(sub["corpus"] == corpus.digest, "submissions name this exact corpus")
    frozen = day(sub["frozen"])
    plan = sub["plan"]
    closed(plan, "cases seeds")
    seeds = plan["seeds"]
    require(isinstance(plan["cases"], list) and plan["cases"], "planned cases")
    require(isinstance(seeds, list) and seeds, "seeds")
    variants = sub["variants"]
    require(isinstance(variants, dict) and len(variants) >= 2, "a baseline and a variant")
    require(
        len(variants) <= MAX_VARIANTS and len(plan["cases"]) * len(seeds) <= MAX_PLANNED_PAIRS,
        "the planned work is within its limits",
    )
    require(len(set(plan["cases"])) == len(plan["cases"]), "planned case repeated")
    for cid in plan["cases"]:
        require(cid in corpus.cases, "planned case registered")
        require(corpus.cases[cid].runnable, "a planned case is runnable")
    require(
        len({corpus.cases[cid].cluster for cid in plan["cases"]}) <= MAX_PLANNED_CLUSTERS,
        "the planned work is within its limits",
    )
    require(all(integer(s, 1) for s in seeds), "seeds")
    require(len(set(seeds)) == len(seeds), "planned seed repeated")
    identities = [check_variant(name, variant) for name, variant in sorted(variants.items())]
    require(len(set(identities)) == len(identities), "variant identity repeated")
    require(sub["baseline"] in variants, "the baseline is a registered variant")
    require(isinstance(sub["runs"], list), "runs")
    barred = frozenset(identities)
    for case in corpus.cases.values():
        require(not barred & case.labelers, "a reviewer variant cannot label the corpus")
    pins = {name: pins_digest(variant) for name, variant in variants.items()}
    runs = [
        check_run(r, corpus=corpus, pins=pins, plan=plan, frozen=frozen, barred=barred)
        for r in sub["runs"]
    ]
    keys = [(r.variant, r.case.id, r.seed) for r in runs]
    require(len(set(keys)) == len(keys), "one run per variant, case and seed")
    return runs


# -- scoring & cluster bootstrap ----------------------------------------------

RateCounts = tuple[int, int, int]  # (yes, unknown, total) for precision / FPR
RecallCounts = tuple[int, int, int, int, int]  # (det, extra_c, extra_u, conf_total, unset_total)


def recall_rate(
    counts: Sequence[RecallCounts],
) -> tuple[Fraction | None, Fraction | None, Fraction | None]:
    """Compute exact point estimate and conservative [low, high] bounds for recall.

    Option 3A: unsettled (``unknown``/``disputed``) registered defects widen
    ``low = det / (conf + unset)`` and allow ``high = (det + extra_c + extra_u) / (conf + extra_u)``
    (or ``[0, 1]`` when ``conf == 0 < unset`` and ``extra_u > 0``). When ``unset == 0``,
    identically matches ``tests.review_eval.rate(..., recall=True)``.
    """
    if not counts:
        return None, None, None
    det = extra_c = extra_u = conf_total = unset_total = 0
    for d, ec, eu, ct, ut in counts:
        det += d
        extra_c += ec
        extra_u += eu
        conf_total += ct
        unset_total += ut
    if conf_total == 0 and unset_total == 0:
        return None, None, None
    point = Fraction(det, conf_total) if conf_total > 0 else None
    low = Fraction(det, conf_total + unset_total)
    if conf_total > 0:
        high = Fraction(det + extra_c + extra_u, conf_total + extra_u)
    else:
        high = Fraction(1) if extra_u > 0 else Fraction(0)
    return point, low, high


def _eval_metric(groups: Sequence[tuple[int, ...]], *, is_recall: bool):
    if is_recall:
        return recall_rate(groups)  # type: ignore[arg-type]
    return rate(groups)  # type: ignore[arg-type]


def run_counts(run: Run) -> dict[str, tuple[int, ...]]:
    case = run.case
    tally = case.tally
    detected = {
        key for _, verdict, key in run.findings if verdict == "confirmed" and key is not None
    }
    verdicts = Counter(verdict for _, verdict, _ in run.findings)
    unknown = verdicts["unknown"]
    out: dict[str, tuple[int, ...]] = {
        "precision": (verdicts["confirmed"], unknown, len(run.findings)),
    }
    for classification in CLASSIFICATIONS:
        mine = [verdict for c, verdict, _ in run.findings if c == classification]
        if mine:
            out[f"precision/classification/{classification}"] = (
                mine.count("confirmed"),
                mine.count("unknown"),
                len(mine),
            )
    if tally.conf_weights_desc or tally.unset_weights_desc:
        det_weights = [WEIGHT[tally.severity[k]] for k in detected]
        det_sum = sum(det_weights)
        conf_sum = sum(tally.conf_weights_desc)
        unset_sum = sum(tally.unset_weights_desc)
        missed_weights = sorted(
            (WEIGHT[s] for k, s in tally.severity.items() if k not in detected),
            reverse=True,
        )
        extra_c = sum(missed_weights[:unknown])
        extra_u = sum(tally.unset_weights_desc[:unknown])
        weighted_tuple = (det_sum, extra_c, extra_u, conf_sum, unset_sum)
        out["recall"] = weighted_tuple
        out[f"recall/risk/{case.risk}"] = weighted_tuple

        hit_sev = Counter(tally.severity[k] for k in detected)
        hit_src = Counter(tally.source[k] for k in detected)
        hit_cls = Counter(tally.cls[k] for k in detected)
        for prefix, hit, conf_c, unset_c, values in (
            ("severity", hit_sev, tally.conf_by_sev, tally.unset_by_sev, SEVERITIES),
            ("discovery", hit_src, tally.conf_by_src, tally.unset_by_src, DISCOVERY),
            ("class", hit_cls, tally.conf_by_cls, tally.unset_by_cls, sorted(CLASSES)),
        ):
            for value in values:
                ct, ut = conf_c[value], unset_c[value]
                if ct or ut:
                    got = hit[value]
                    out[f"recall/{prefix}/{value}"] = (
                        got,
                        min(unknown, ct - got),
                        min(unknown, ut),
                        ct,
                        ut,
                    )
    if case.negative == "settled":
        alarm = verdicts["false"] > 0
        out["false_positive_rate"] = (int(alarm), int(not alarm and unknown > 0), 1)
    return out


def metric_keys() -> list[str]:
    keys = ["recall", "precision", "false_positive_rate"]
    keys += [f"recall/severity/{s}" for s in SEVERITIES]
    keys += [f"recall/discovery/{s}" for s in DISCOVERY]
    keys += [f"recall/class/{c}" for c in sorted(CLASSES)]
    keys += [f"recall/risk/{r}" for r in RISKS]
    keys += [f"precision/classification/{c}" for c in CLASSIFICATIONS]
    return keys


@lru_cache(maxsize=BOOTSTRAP_TABLES)
def multiplicities(clusters: int) -> tuple[tuple[int, ...], ...]:
    rng = Random(BOOTSTRAP_SEED)
    draws = []
    for _ in range(BOOTSTRAP_DRAWS):
        drawn = [0] * clusters
        for _ in range(clusters):
            drawn[rng.randrange(clusters)] += 1
        draws.append(tuple(drawn))
    return tuple(draws)


@lru_cache(maxsize=BOOTSTRAP_TABLES)
def distinct_draws(clusters: int) -> tuple[tuple[tuple[int, ...], int], ...]:
    return tuple(sorted(Counter(multiplicities(clusters)).items()))


def weighted_tuple(
    groups: Sequence[tuple[int, ...]], drawn: Sequence[int], width: int
) -> tuple[int, ...]:
    totals = [0] * width
    for m, g in zip(drawn, groups, strict=True):
        if m:
            for idx in range(width):
                totals[idx] += m * g[idx]
    return tuple(totals)


def pooled_tuple(groups: Iterable[tuple[int, ...]], width: int) -> tuple[int, ...]:
    totals = [0] * width
    for g in groups:
        for idx in range(width):
            totals[idx] += g[idx]
    return tuple(totals)


def nearest_rank(tally: Counter, rank: int):
    values = sorted((value, n) for value, n in tally.items() if value is not None)
    size = sum(n for _, n in values)
    if not size:
        return None
    position, seen = (size * rank + 39) // 40, 0
    for value, n in values:
        seen += n
        if seen >= position:
            return value
    raise AssertionError("unreachable")


def encoded(value: object) -> str | None:
    return str(value) if value is not None else None


def interval(tally: Counter) -> list[str | None]:
    return [encoded(nearest_rank(tally, 1)), encoded(nearest_rank(tally, 39))]


Observed = list[tuple[str, dict[str, tuple[int, ...]]]]
RunKey = tuple[str, str, int]


def run_key(run: Run) -> RunKey:
    return run.variant, run.case.id, run.seed


def observed(runs: Iterable[Run], counts: dict[RunKey, dict[str, tuple[int, ...]]]) -> Observed:
    return [(run.case.cluster, counts[run_key(run)]) for run in runs]


def by_cluster(runs: Observed, key: str, width: int) -> dict[str, tuple[int, ...]]:
    clusters: dict[str, list[tuple[int, ...]]] = {}
    for cluster, counts in runs:
        if key in counts:
            clusters.setdefault(cluster, []).append(counts[key])
    return {cluster: pooled_tuple(groups, width) for cluster, groups in sorted(clusters.items())}


def band_ok(bounds: tuple[Fraction | None, Fraction | None, Fraction | None]) -> bool:
    return (
        bounds[0] is not None
        and bounds[1] is not None
        and bounds[2] is not None
        and bounds[2] - bounds[1] <= LABEL_BAND
    )


def marginal(runs: Observed, key: str) -> dict:
    is_recall = key.startswith("recall")
    width = 5 if is_recall else 3
    groups = by_cluster(runs, key, width)
    total = pooled_tuple(groups.values(), width)
    point, low, high = _eval_metric([total], is_recall=is_recall) if groups else (None, None, None)
    points, lows, highs = Counter(), Counter(), Counter()
    values = list(groups.values())
    for drawn, times in distinct_draws(len(values)) if values else ():
        p, lo, hi = _eval_metric([weighted_tuple(values, drawn, width)], is_recall=is_recall)
        points[p] += times
        lows[lo] += times
        highs[hi] += times
    return {
        "observations": list(total),
        "clusters": len(groups),
        "point": encoded(point),
        "bounds": [encoded(low), encoded(high)],
        "interval": interval(points),
        "envelope": [encoded(nearest_rank(lows, 1)), encoded(nearest_rank(highs, 39))],
        "undefined_draws": points[None] if values else None,
    }


def contrast(base_runs: Observed, other_runs: Observed, key: str) -> dict:
    is_recall = key.startswith("recall")
    width = 5 if is_recall else 3
    b_groups = by_cluster(base_runs, key, width)
    t_groups = by_cluster(other_runs, key, width)
    clusters = sorted(set(b_groups) | set(t_groups))
    zero = (0,) * width
    b_values = [b_groups.get(c, zero) for c in clusters]
    t_values = [t_groups.get(c, zero) for c in clusters]
    b = (
        _eval_metric([pooled_tuple(b_values, width)], is_recall=is_recall)
        if clusters
        else (None, None, None)
    )
    t = (
        _eval_metric([pooled_tuple(t_values, width)], is_recall=is_recall)
        if clusters
        else (None, None, None)
    )
    points, lows, highs = Counter(), Counter(), Counter()
    for drawn, times in distinct_draws(len(clusters)) if clusters else ():
        bd = _eval_metric([weighted_tuple(b_values, drawn, width)], is_recall=is_recall)
        td = _eval_metric([weighted_tuple(t_values, drawn, width)], is_recall=is_recall)
        points[td[0] - bd[0] if None not in (td[0], bd[0]) else None] += times
        lows[td[1] - bd[2] if None not in (td[1], bd[2]) else None] += times
        highs[td[2] - bd[1] if None not in (td[2], bd[1]) else None] += times
    defined = None not in (b[0], t[0])
    undefined = (points[None], lows[None]) if clusters else (None, None)
    return {
        "clusters": len(clusters),
        "baseline": encoded(b[0]),
        "variant": encoded(t[0]),
        "difference": encoded(t[0] - b[0]) if defined else None,
        "bounds": [
            encoded(t[1] - b[2]) if None not in (t[1], b[2]) else None,
            encoded(t[2] - b[1]) if None not in (t[2], b[1]) else None,
        ],
        "interval": interval(points),
        "envelope": [encoded(nearest_rank(lows, 1)), encoded(nearest_rank(highs, 39))],
        "undefined_draws": undefined[0],
        "undefined_envelope_draws": undefined[1],
        "label_bands_within": band_ok(b) and band_ok(t),
    }


def exposure(case: Case, knowledge_cutoff: str | None) -> str:
    if knowledge_cutoff is None:
        return "unknown"
    known = day(knowledge_cutoff)
    if known >= case.public:
        return "possible"
    if case.possible_from is not None and known < case.possible_from:
        return "none"
    return "unknown"


def _safety_summary(runs: Iterable[Run]) -> dict[str, int]:
    missed_critical = 0
    clean_escapes = 0
    for run in runs:
        if run.status != "completed":
            continue
        crit_keys = {
            k
            for k, d in run.case.defects.items()
            if d.status == "confirmed" and d.severity == "critical"
        }
        if not crit_keys:
            continue
        hit_keys = {k for _, v, k in run.findings if v == "confirmed" and k in crit_keys}
        missed = len(crit_keys - hit_keys)
        missed_critical += missed
        if missed and not run.findings:
            clean_escapes += 1
    return {
        "missed_critical_defects": missed_critical,
        "clean_verdict_critical_escapes": clean_escapes,
    }


def score(corpus: dict, submissions: dict, repo: Path | None = None) -> dict:
    checked = check_corpus(corpus)
    verified = None
    if checked.mode == "historical":
        if repo is None:
            raise CorpusError("a recorded score verifies its corpus")
        require(verify(corpus, repo, checked=checked)["ok"], "a recorded score verifies its corpus")
        verified = {"corpus": checked.digest, "ok": True}
    runs = check_submissions(checked, submissions)
    counts = {run_key(r): run_counts(r) for r in runs if r.status == "completed"}
    plan = submissions["plan"]
    planned = [(cid, seed) for cid in plan["cases"] for seed in plan["seeds"]]
    by_variant = {
        name: {(r.case.id, r.seed): r for r in runs if r.variant == name}
        for name in submissions["variants"]
    }
    cards = {}
    for name, variant in sorted(submissions["variants"].items()):
        mine = by_variant[name]
        completed = [mine[k] for k in planned if k in mine and mine[k].status == "completed"]
        statuses = Counter(mine[k].status if k in mine else "missing" for k in planned)
        verdicts = Counter(v for r in completed for _, v, _ in r.findings)
        duplicates = 0
        for r in completed:
            hits = [k for _, v, k in r.findings if v == "confirmed" and k is not None]
            duplicates += len(hits) - len(set(hits))
        launched = [r for r in mine.values() if r.status != "excluded"]
        obs = observed(completed, counts)
        exposed = Counter(exposure(r.case, variant["knowledge_cutoff"]) for r in completed)
        costs = [r.cost for r in launched]
        cards[name] = {
            "pins": variant,
            "pins_sha256": pins_digest(variant),
            "runs": {s: statuses[s] for s in ("completed", "failed", "excluded", "missing")},
            "coverage_complete": statuses["failed"] + statuses["missing"] == 0,
            "findings": {
                "confirmed": verdicts["confirmed"],
                "false": verdicts["false"],
                "unknown": verdicts["unknown"],
                "duplicates": duplicates,
            },
            "safety": _safety_summary(mine.values()),
            "training_exposure": {lvl: exposed[lvl] for lvl in ("none", "possible", "unknown")},
            "cost_microusd": sum(costs) if None not in costs else None,
            "metrics": {key: marginal(obs, key) for key in metric_keys()},
        }

    baseline = submissions["baseline"]
    contrasts = {}
    for name in sorted(submissions["variants"]):
        if name == baseline:
            continue
        excluded = [
            k
            for k in planned
            if any(
                k in by_variant[arm] and by_variant[arm][k].status == "excluded"
                for arm in (baseline, name)
            )
        ]
        paired = [
            k
            for k in planned
            if k not in excluded
            and all(
                k in by_variant[arm] and by_variant[arm][k].status == "completed"
                for arm in (baseline, name)
            )
        ]
        incomplete = [k for k in planned if k not in excluded and k not in paired]
        base_runs = observed((by_variant[baseline][k] for k in paired), counts)
        other_runs = observed((by_variant[name][k] for k in paired), counts)
        metrics = {key: contrast(base_runs, other_runs, key) for key in metric_keys()}
        for item in metrics.values():
            item["usable"] = (
                not incomplete
                and item["clusters"] >= MIN_CLUSTERS
                and item["difference"] is not None
                and item["label_bands_within"]
                and item["undefined_draws"] == 0
                and item["undefined_envelope_draws"] == 0
            )
        contrasts[name] = {
            "pairs": {
                "paired": len(paired),
                "excluded": len(excluded),
                "incomplete": [list(k) for k in incomplete],
            },
            "metrics": metrics,
        }

    return {
        "evidence": submissions["mode"],
        "claim": CLAIM,
        "corpus": {"name": checked.name, "release": checked.release, "sha256": checked.digest},
        "verified": verified,
        "plan": {
            "cases": len(plan["cases"]),
            "seeds": sorted(plan["seeds"]),
            "clusters": len({checked.cases[c].cluster for c in plan["cases"]}),
        },
        "labels": label_summary(checked.cases[c] for c in plan["cases"]),
        "baseline": baseline,
        "variants": cards,
        "contrasts": contrasts,
    }


# -- files & CLI --------------------------------------------------------------


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        require(key not in value, "duplicate JSON key")
        value[key] = item
    return value


def _no_constant(_name: str) -> None:
    raise CorpusError("non-finite number")


def read_json(path: Path):
    """Read one regular JSON file without following a final symlink or blocking on a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), "regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            body = stream.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    require(len(body) <= MAX_FILE_BYTES, "file size")
    return json.loads(
        body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_no_constant
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.review_corpus", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    shown = commands.add_parser("describe", help="validate and summarize a corpus")
    shown.add_argument("corpus", type=Path, nargs="?", default=SEED)
    checked = commands.add_parser("verify", help="re-derive a historical corpus's pins from git")
    checked.add_argument("corpus", type=Path, nargs="?", default=SEED)
    checked.add_argument("--repo", type=Path, default=Path.cwd())
    scored = commands.add_parser("score", help="score submissions against a corpus")
    scored.add_argument("corpus", type=Path, nargs="?", default=SYNTHETIC_CORPUS)
    scored.add_argument("submissions", type=Path, nargs="?", default=SYNTHETIC_SUBMISSIONS)
    scored.add_argument("--repo", type=Path, help="object store for historical corpus verification")
    exported = commands.add_parser(
        "export-input", help="emit only the reviewer-visible input projection"
    )
    exported.add_argument("corpus", type=Path)
    exported.add_argument("case_id", type=str)
    args = parser.parse_args(argv)
    try:
        corpus = read_json(args.corpus)
        if args.command == "describe":
            report = describe(corpus)
        elif args.command == "verify":
            report = verify(corpus, args.repo)
        elif args.command == "export-input":
            check_corpus(corpus)
            require(args.case_id in corpus["cases"], "unknown case id")
            report = project_reviewer_input(corpus["cases"][args.case_id])
        else:
            report = score(corpus, read_json(args.submissions), args.repo)
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        RecursionError,
    ):
        parser.exit(2, "Invalid review corpus or submissions.\n")
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
