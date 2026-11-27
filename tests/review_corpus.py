"""REV-R1: a historical review corpus and an offline reviewer benchmark (#356).

``check`` holds a corpus to its closed version 1 format, its cutoff and leakage
rules and its label rules; ``verify`` re-derives a historical corpus's pins from a
local git object store, read only; ``score`` scores recorded reviewer submissions
against the corpus's settled labels. Nothing here launches a reviewer, calls a
model, reaches a network or writes to GitHub. See ``docs/review-corpus.md``.
"""

from __future__ import annotations

import argparse
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
SEVERITIES = tuple(WEIGHT)
# Each curation stratum and whether its cases register defects.
STRATA = {
    "reviewed_defect": True,
    "escape": True,
    "revert": True,
    "hard_negative": False,
    "clean": False,
}
SOURCES = {"synthetic": ("synthetic",), "historical": ("pr_branch", "squash")}
SUBMISSION_MODES = {"synthetic": "synthetic", "historical": "recorded"}
# Who first reported a defect, relative to the case's own historical review.
# A defect the case's own reviewer reported favours reviewers like it.
DISCOVERY = ("same_review", "later_review", "post_merge", "revert")
# A defect case holds at least one defect its stratum is named for.
STRATUM_SOURCES = {
    "reviewed_defect": {"same_review", "later_review"},
    "escape": {"post_merge"},
    "revert": {"revert"},
}
RISKS = ("routine", "elevated", "critical")
CONTEXT_KINDS = ("issue", "issue_comment", "pr_body", "pr_comment", "document")
# A case holds at most one of each; it must hold the issue when its provenance
# names one and the PR body when its candidate came from a PR, captured or not
# (see ``required_context``), since a reviewer of either was always given them.
SINGULAR_KINDS = ("issue", "pr_body")
INVENTORY_STATUSES = ("complete", "not_captured")
# A context item's stable source identity, from which its kind follows: the
# issue or PR body, a comment on either, or a document. The issue and PR are the
# ones the case's provenance names (see ``source_kind``).
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
CLASSIFICATIONS = ("blocked", "non-blocked", "nit")
# A negative is settled only after this many days without a registered defect,
# counted from its review cutoff to the end of its observation. Preregistered,
# not measured: see the protocol's "Censoring".
MIN_NEGATIVE_WINDOW_DAYS = 30
BOOTSTRAP_DRAWS = 10_000
BOOTSTRAP_SEED = 356
LABEL_BAND = Fraction(1, 10)
# One cluster resamples to itself: its interval is the point, not an uncertainty.
MIN_CLUSTERS = 2
# The work a score may expand to, refused before anything is expanded: the
# file-size cap bounds what is read, not the planned pairs or the bootstrap's
# tables (BOOTSTRAP_DRAWS draws of every cluster a metric observes), which grow
# with their product. Each run's observations are computed once, from its
# findings and its case's tally of confirmed defects (``Case.tally``, counted
# once per case), so no step scans a case's defects once per run. See
# ``docs/review-corpus.md`` section 5 for what a score at these bounds cost.
MAX_VARIANTS = 8
MAX_PLANNED_PAIRS = 10_000  # planned cases times seeds
MAX_PLANNED_CLUSTERS = 64
BOOTSTRAP_TABLES = 16  # resample tables kept at once, one per cluster count


@dataclass(frozen=True)
class Form:
    """A kind of retained record: what its one binding statement begins with
    and the fields of the JSON object that follows (``record_binding``)."""

    kind: str
    prefix: str
    fields: str


# A review round's record: what it reviewed, against which base revision, and
# when it read its input.
REVIEW = Form("review", "review-record: ", "pr round base_ref base_tip head merge_base input_read")
# A PR's merge record: the commit its PR merged into its base ref as.
MERGE = Form("merge", "merge-record: ", "pr base_ref merged")
# A defect's lineage record: the fix or revert that addresses it, and the
# merge it escaped through.
LINEAGE = Form("lineage", "lineage-record: ", "pr introduced paths merged fix")
CLAIM = "scorecards only: no benchmark decision, gate or rollout follows from this report"
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
OBJECT_ID = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
HEX_RUN = re.compile(r"[0-9a-fA-F]{7,}")
# A hunk header of a zero-context diff: the old side's first line and count.
HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", re.MULTILINE)


class CorpusError(ValueError):
    """A corpus or a submission is malformed or contradicts itself."""


def require(condition: object, rule: str) -> None:
    if not condition:
        raise CorpusError(rule)


def closed(value, names: str) -> None:
    require(isinstance(value, dict) and value.keys() == set(names.split()), f"fields: {names}")


def nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def integer(value, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def object_id(value) -> str:
    require(isinstance(value, str) and OBJECT_ID.fullmatch(value), "object id shape")
    return value


def day(value) -> date:
    require(isinstance(value, str) and DAY.fullmatch(value), "date shape")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CorpusError("date shape") from exc


def instant(value) -> datetime:
    require(isinstance(value, str) and INSTANT.fullmatch(value), "instant shape")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise CorpusError("instant shape") from exc


def relative_path(value) -> str:
    require(isinstance(value, str) and value and "\\" not in value, "repository path")
    parts = PurePosixPath(value).parts
    require(
        not value.startswith("/") and ".." not in parts and "." not in value.split("/"),
        "repository path",
    )
    require(str(PurePosixPath(value)) == value, "repository path")
    return value


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def corpus_digest(corpus) -> str:
    """The corpus's identity: its canonical JSON, whatever the file's layout."""
    return sha256(canonical(corpus)).hexdigest()


def input_digest(case) -> str:
    """Everything a reviewer of the case is given, and nothing about its answer."""
    return sha256(
        canonical(
            {
                "format": INPUT_FORMAT,
                "version": VERSION,
                "review": case["review"],
                "environment": case["environment"],
                "context": [
                    {key: item[key] for key in ("id", "kind", "source", "ref", "sha256")}
                    for item in case["context"]["items"]
                ],
            }
        )
    ).hexdigest()


def source_kind(source) -> tuple[str, str | None, int | None]:
    """The context kind a source identity names, and the issue or PR it is on.

    ``issue/<n>`` and ``pr/<n>`` are an issue and a PR body,
    ``issue/<n>/comment/<id>`` and ``pr/<n>/comment/<id>`` a comment on one, and
    ``document/<path>`` a document (another issue, a file, a page).
    """
    require(isinstance(source, str), "context source")
    if source.startswith(DOCUMENT_SOURCE):
        relative_path(source.removeprefix(DOCUMENT_SOURCE))
        return "document", None, None
    match = THREAD_SOURCE.fullmatch(source)
    require(match, "context source")
    thread, number, comment = match.groups()
    return THREAD_KINDS[(thread, comment is not None)], thread, int(number)


def names_commit(text: str, commits: Iterable[str]) -> bool:
    """Whether ``text`` names a commit, abbreviated to seven digits or more.

    A hexadecimal run of seven or more digits names a commit when it holds the
    commit's first seven digits anywhere in it: an abbreviation, the whole id,
    or either inside a longer run.
    """
    runs = [run.lower() for run in HEX_RUN.findall(text)]
    return any(commit[:7] in run for commit in commits for run in runs)


def reviewer_text(case) -> Iterable[str]:
    """Every free-text value of a case that its reviewer is given.

    The pinned object ids and digests are identifiers other rules hold; every
    other string of the review input is read here, context references and ids
    as much as captured text.
    """
    yield case["review"]["base_ref"]
    yield from case["environment"]["files"]
    yield from case["environment"]["validation"]
    for item in case["context"]["items"]:
        yield item["id"]
        yield item["kind"]
        if item["source"] is not None:
            yield item["source"]
        yield item["ref"]
        if item["text"] is not None:
            yield item["text"]


# -- labels -------------------------------------------------------------------

LABEL_FIELDS = "by role verdict decided artifact"


def check_labels(labels, verdicts, *, frozen: date, barred: frozenset, match=None) -> None:
    """One label per identity, decided by the freeze, never by a barred identity.

    ``barred`` holds the identities that acted on what is labeled: the case's
    historical producer, reviewer and fixer, and for a finding the reviewer
    variants. A curator proposes; only adjudicators and arbiters decide.
    """
    require(isinstance(labels, list), "labels")
    seen = set()
    for label in labels:
        closed(label, LABEL_FIELDS + (" defect" if match is not None else ""))
        require(nonempty(label["by"]) and nonempty(label["artifact"]), "label provenance")
        require(label["role"] in ROLES and label["verdict"] in verdicts, "label verdict")
        require(day(label["decided"]) <= frozen, "label decided after the freeze")
        require(label["by"].strip() not in barred, "an actor cannot adjudicate")
        require(label["by"].strip() not in seen, "one label per identity")
        seen.add(label["by"].strip())
        if match is not None:
            if label["verdict"] == "match":
                require(label["defect"] in match, "match names a registered defect")
            else:
                require(label["defect"] is None, "only a match names a defect")
    votes = {verdict_of(label) for label in labels if counted(label, "adjudicator")}
    if any(counted(label, "arbiter") for label in labels):
        require(len(votes) > 1, "an arbiter settles a disagreement only")


def counted(label, role: str) -> bool:
    return label["role"] == role and label["verdict"] != "unknown"


def verdict_of(label):
    return (label["verdict"], label["defect"]) if "defect" in label else label["verdict"]


def settle(labels, need: Callable[[object], int] = lambda _: 1):
    """The settled verdict, or ``None`` when the labels leave it unknown.

    A curator's proposal never settles anything. Adjudicators must agree, at
    least ``need(verdict)`` of them (two for a critical defect); a disagreement
    stays unknown unless arbiters, all of one verdict, settle it.
    """
    arbiters = {verdict_of(label) for label in labels if counted(label, "arbiter")}
    if arbiters:
        return next(iter(arbiters)) if len(arbiters) == 1 else None
    votes = [verdict_of(label) for label in labels if counted(label, "adjudicator")]
    if len(set(votes)) != 1:
        return None
    return votes[0] if len(votes) >= need(votes[0]) else None


def critical_needs_two(severity: str) -> Callable[[object], int]:
    """A critical defect is settled either way by two adjudicators, any other by one."""
    return lambda _: 2 if severity == "critical" else 1


# -- the corpus ---------------------------------------------------------------

CORPUS_FIELDS = "format version mode name release frozen repository cases"
REPOSITORY_FIELDS = "slug root refs environment"
CASE_FIELDS = (
    "stratum cluster provenance review environment context record actors risk exposure"
    " defects negative withheld"
)
REVIEW_FIELDS = "base_ref base_tip merge_base head tree cutoff"
DEFECT_FIELDS = "severity class introduced summary location discovery labels"
DISCOVERY_FIELDS = "source round finding discovered fix merged lineage"
RECORD_FIELDS = "status source started text sha256"
LINEAGE_FIELDS = "status source text sha256"
EXPOSURE_FIELDS = "possible_from public_by artifact"


@dataclass(frozen=True)
class Defect:
    severity: str
    cls: str
    source: str
    discovered: date
    status: str  # confirmed, rejected or unknown


@dataclass(frozen=True)
class Case:
    id: str
    cluster: str
    stratum: str
    risk: str
    cutoff: datetime
    actors: frozenset
    labelers: frozenset  # every identity that labeled a defect or the negative, any role
    defects: dict
    negative: str | None  # settled, censored, unlabeled, refuted; None for a defect case
    runnable: bool
    input: str
    # Before this day nothing of the case's answer can have been public; None
    # when no evidence rules any day out.
    possible_from: date | None = None
    # By this day part of the answer was certainly public, when that is earlier
    # than the case itself shows (a disclosure at a previous revision, say).
    public_by: date | None = None

    # Each of these reads every defect of the case, so each is computed once per
    # case, never once per run of it.
    @cached_property
    def confirmed(self) -> dict:
        return {key: d for key, d in self.defects.items() if d.status == "confirmed"}

    @cached_property
    def tally(self) -> Tally:
        return Tally.of(self.confirmed)

    @cached_property
    def public(self) -> date:
        """The first day by which part of the case's answer was certainly public.

        A registered defect's discovery day, or a negative's own cutoff (its
        answer is its later history), unless the exposure evidence names an
        earlier disclosure. Each bounds the first report from above only: the
        report may be earlier, never later.
        """
        days = [d.discovered for d in self.defects.values()] or [self.cutoff.date()]
        if self.public_by is not None:
            days.append(self.public_by)
        return min(days)


@dataclass(frozen=True)
class Tally:
    """A case's confirmed defects as every run of it is scored against them.

    A run's recall needs only how many confirmed defects there are of each
    severity and each discovery source, and the severity and source of each it
    found, so counting them once lets a run be scored from its findings alone.
    """

    severity: dict  # defect key -> severity, for the confirmed defects
    source: dict  # defect key -> discovery source
    by_severity: Counter
    by_source: Counter

    @classmethod
    def of(cls, confirmed: dict) -> Tally:
        return cls(
            severity={key: d.severity for key, d in confirmed.items()},
            source={key: d.source for key, d in confirmed.items()},
            by_severity=Counter(d.severity for d in confirmed.values()),
            by_source=Counter(d.source for d in confirmed.values()),
        )


@dataclass(frozen=True)
class Corpus:
    name: str
    release: int
    mode: str
    frozen: date
    digest: str
    cases: dict = field(default_factory=dict)


def required_context(origin) -> set[str]:
    """The context kinds a case must hold, from its provenance alone."""
    required = set()
    if origin["issue"] is not None:
        required.add("issue")
    if origin["pr"] is not None or origin["source"] in ("pr_branch", "squash"):
        required.add("pr_body")
    return required


def check_context(context, *, origin, cutoff: datetime, frozen: date) -> bool:
    """Admit only what existed by the cutoff; whether all of it was captured.

    Each item names its stable source identity (``source_kind``): the issue and
    PR its provenance names, a comment on one of them, or a document. The
    inventory lists, by those identities, what the reviewer was given at the
    cutoff, and says when and from what that listing was taken. A case is
    runnable only when its inventory is complete, lists exactly the sources of
    the items the case holds, each once, and every item is captured. The issue
    and the PR body its provenance names must be held either way, captured or
    not: leaving an item out, or holding a copy of one in another's place, can
    make a case refused or unrunnable, never runnable. Returns whether it is
    runnable on its context.
    """
    closed(context, "inventory items")
    items = context["items"]
    require(isinstance(items, list), "context items")
    ids = set()
    sources: set[str] = set()
    kinds: Counter = Counter()
    captured = True
    for item in items:
        closed(item, "id kind source ref observed_at status text sha256")
        require(isinstance(item["id"], str) and IDENTIFIER.fullmatch(item["id"]), "context id")
        require(item["id"] not in ids, "context id repeated")
        ids.add(item["id"])
        require(item["kind"] in CONTEXT_KINDS and nonempty(item["ref"]), "context kind")
        kinds[item["kind"]] += 1
        source = item["source"]
        if source is None:
            # An identity nobody established (a PR whose number no record
            # states, say) can be held only as uncaptured.
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
                item["observed_at"] is None and item["text"] is None and item["sha256"] is None,
                "an uncaptured context holds nothing",
            )
            captured = False
            continue
        require(item["status"] == "captured", "context status")
        require(instant(item["observed_at"]) <= cutoff, "context observed after the cutoff")
        require(nonempty(item["text"]), "captured context text")
        require(item["sha256"] == sha256(item["text"].encode()).hexdigest(), "context digest")
    require(all(kinds[kind] <= 1 for kind in SINGULAR_KINDS), "one issue and one PR body")
    for kind in sorted(required_context(origin)):
        require(kinds[kind] == 1, f"the context holds the case's {kind}")
    inventory = context["inventory"]
    closed(inventory, "status taken artifact sources")
    require(inventory["status"] in INVENTORY_STATUSES, "context inventory status")
    if inventory["status"] == "not_captured":
        require(
            all(inventory[key] is None for key in ("taken", "artifact", "sources")),
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


def record_binding(text: str, form: Form) -> tuple[str, dict]:
    """The one binding statement a retained record's text carries, and its line.

    It is the line that begins with ``form.prefix``, followed by a JSON object
    of exactly ``form.fields``. Nothing else of the text is read for the
    binding: an instant or a commit the text names elsewhere (when the record
    was published, say) is never taken for one.
    """
    lines = [line for line in text.splitlines() if line.startswith(form.prefix)]
    require(len(lines) == 1, f"a {form.kind} record states one binding")
    try:
        binding = json.loads(
            lines[0].removeprefix(form.prefix),
            object_pairs_hook=_unique_object,
            parse_constant=_no_constant,
        )
    except json.JSONDecodeError as exc:
        raise CorpusError(f"{form.kind} record binding") from exc
    closed(binding, form.fields)
    return lines[0], binding


def squeezed(text: str) -> str:
    """``text`` compared without case or whitespace."""
    return "".join(text.casefold().split())


def exposes(texts: Sequence[str], record, line: str) -> bool:
    """Whether the reviewer's input holds a retained record.

    ``texts`` is every free-text value of the input (``reviewer_text``): each
    context item's id, kind, source, reference and text, the environment's
    paths and commands and the base ref. One of them is the record (its digest
    is the record's), or one carries the record's binding statement, or
    together they hold every other line of the record, each compared without
    case or whitespace, wherever each line stands. A paraphrase, or a part of
    the record, cannot be told.
    """
    body = {squeezed(row) for row in record["text"].splitlines() if row != line} - {""}
    held: set[str] = set()
    for text in texts:
        if sha256(text.encode()).hexdigest() == record["sha256"] or squeezed(line) in squeezed(
            text
        ):
            return True
        held |= {squeezed(row) for row in text.splitlines()}
    return bool(body) and body <= held


def check_source(record, form: Form, *, origin, context) -> None:
    """A retained record is named by a stable source anyone can read again: a
    comment on the case's own PR, or a document. It is never the reviewer's
    context under that source."""
    if record["source"] is None:
        return
    kind, _, number = source_kind(record["source"])
    require(
        kind == "document" or (kind == "pr_comment" and number == origin["pr"]),
        f"a {form.kind} record is a comment on the case's PR or a document",
    )
    require(
        all(record["source"] != item["source"] for item in context),
        f"the {form.kind} record is not the reviewer's context",
    )


def retained(record, form: Form) -> tuple[str, dict]:
    """A captured record's text, its digest and its binding statement."""
    require(record["source"] is not None, f"a {form.kind} record names its source")
    text = record["text"]
    require(nonempty(text), f"{form.kind} record text")
    require(record["sha256"] == sha256(text.encode()).hexdigest(), f"{form.kind} record digest")
    return record_binding(text, form)


def pr_number(value) -> bool:
    return value is None or integer(value, 1)


def check_record(record, *, case, cutoff: datetime, context: list) -> str | None:
    """The retained record of a case's review time; its binding line, if captured.

    A review round's head is committed before its PR, and so its PR body, can
    exist, so the head's committer time can fall before the context the review
    read, and the base ref's tip at that time is a guess: commit times say
    when a commit was made, not when the ref reached it. A later boundary and
    the base revision the review read need evidence kept apart from the
    corpus: a record of the round (``REVIEW``), named by its stable source so
    anyone can read it again (``check_source``), whose digest matches its text
    and whose text carries one binding statement (``record_binding``) naming
    the case's PR, round, base ref, base revision, head and merge base and the
    instant the review read its input. That instant is then the cutoff.
    Without one the cutoff is the head's committer time and the base revision
    the ref's tip by committer times, both of which ``verify`` checks, the
    second only provisionally, and the case is never runnable.

    A squash candidate's record is its PR's merge record (``MERGE``): the
    commit the PR merged as, which its commit subject only suggests. Without
    one a squash candidate is never runnable either.

    Either record holds what the reviewer must not be given, so neither is
    ever part of the reviewer's input (``exposes``, in ``check_case``).
    """
    closed(record, RECORD_FIELDS)
    origin = case["provenance"]
    form = MERGE if origin["source"] == "squash" else REVIEW
    require(record["status"] in ("captured", "not_captured"), f"{form.kind} record status")
    check_source(record, form, origin=origin, context=context)
    if record["status"] == "not_captured":
        require(
            all(record[key] is None for key in ("started", "text", "sha256")),
            f"an uncaptured {form.kind} record holds nothing",
        )
        return None
    require(
        origin["source"] in ("pr_branch", "squash"),
        "only a review round or a squash holds its record",
    )
    review = case["review"]
    if form is MERGE:
        require(record["started"] is None, "a merge record states no review start")
        line, binding = retained(record, form)
        require(
            integer(binding["pr"], 1) and binding["pr"] == origin["pr"],
            "the merge record's binding names the case's PR",
        )
        require(
            binding["base_ref"] == review["base_ref"] and binding["merged"] == review["head"],
            "the merge record's binding names the case's merge",
        )
        return line
    started = instant(record["started"])
    line, binding = retained(record, form)
    require(
        pr_number(binding["pr"])
        and binding["pr"] == origin["pr"]
        and integer(binding["round"], 1)
        and binding["round"] == origin["round"],
        "the review record's binding names the case's PR and round",
    )
    require(
        all(binding[key] == review[key] for key in ("base_ref", "base_tip", "head", "merge_base")),
        "the review record's binding names the case's review",
    )
    require(
        binding["input_read"] == record["started"],
        "the review record's binding states its start",
    )
    require(cutoff == started, "the cutoff is the review record's start")
    return line


def check_lineage(defect, *, case) -> str | None:
    """A defect's lineage record; its binding line, if captured.

    That a later commit is a defect's fix or revert, and that a squash is the
    merge it escaped through, is what the record evidences: its binding names
    the case's PR, the defect's introducing commit and the files it is located
    in, the merged revision (or ``null``) and the fix. ``verify``'s checks of
    the same lineage against git (ancestry, the squash's subject, the lines it
    carried, the files and lines the fix changed) are consistency checks: a
    later commit can satisfy every one of them without fixing anything. A
    case is runnable only when each fix it names has its record.
    """
    found = defect["discovery"]
    lineage = found["lineage"]
    closed(lineage, LINEAGE_FIELDS)
    require(lineage["status"] in ("captured", "not_captured"), "lineage record status")
    origin = case["provenance"]
    check_source(lineage, LINEAGE, origin=origin, context=case["context"]["items"])
    if lineage["status"] == "not_captured":
        require(
            lineage["text"] is None and lineage["sha256"] is None,
            "an uncaptured lineage record holds nothing",
        )
        return None
    require(found["fix"] is not None, "a lineage record evidences a fix")
    line, binding = retained(lineage, LINEAGE)
    paths = sorted({place["path"] for place in defect["location"]})
    require(
        pr_number(binding["pr"])
        and binding["pr"] == origin["pr"]
        and binding["introduced"] == defect["introduced"]
        and binding["paths"] == paths,
        "the lineage record's binding names the defect",
    )
    require(
        binding["merged"] == found["merged"] and binding["fix"] == found["fix"],
        "the lineage record's binding names the defect's lineage",
    )
    return line


def check_defect(key, defect, *, case, cutoff: datetime, frozen: date, withheld, barred) -> Defect:
    require(isinstance(key, str) and IDENTIFIER.fullmatch(key), "defect id")
    closed(defect, DEFECT_FIELDS)
    require(defect["severity"] in WEIGHT and defect["class"] in CLASSES, "defect rubric")
    review = case["review"]
    introduced = object_id(defect["introduced"])
    require(introduced != review["merge_base"], "a defect is introduced above the merge base")
    require(introduced not in withheld, "a withheld commit cannot introduce a present defect")
    require(nonempty(defect["summary"]), "defect summary")
    # A defect is somewhere: ``verify`` holds its fix, and a squash that
    # carried it, to what it is located in, which an empty list would not bound.
    require(isinstance(defect["location"], list) and defect["location"], "defect location")
    for place in defect["location"]:
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
    found = defect["discovery"]
    closed(found, DISCOVERY_FIELDS)
    require(found["source"] in DISCOVERY, "discovery source")
    # Discovery is relative to the reviewed head: a report of this head or a
    # later one. A disclosure before it (at a previous revision, say) is
    # exposure evidence, the case's ``exposure``, never a discovery.
    discovered = day(found["discovered"])
    require(cutoff.date() <= discovered <= frozen, "discovered between cutoff and freeze")
    require(found["finding"] is None or nonempty(found["finding"]), "discovery finding")
    reviewed = case["provenance"]["round"]
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
    # A defect that escaped a PR branch through a squash reaches its fix or
    # revert through the merged revision, not through its introducing commit.
    # Its lineage record evidences that revision as the PR's merge
    # (``check_lineage``); ``verify`` checks it is consistent with git, the
    # PR's squash holding the defect's own lines, so a squash lineage names
    # the PR and every line.
    if found["merged"] is not None:
        merged = object_id(found["merged"])
        origin = case["provenance"]
        require(
            origin["source"] == "pr_branch" and origin["pr"] is not None,
            "a merged revision follows a review round of a PR",
        )
        require(
            all(place["lines"] is not None for place in defect["location"]),
            "a merged defect is located by its lines",
        )
        require(found["fix"] is not None, "a merged revision leads to a fix")
        require(merged in withheld, "a merged revision is withheld from the reviewer")
        require(merged not in (introduced, found["fix"]), "a merged revision is its own commit")
    check_labels(defect["labels"], DEFECT_VERDICTS, frozen=frozen, barred=barred)
    status = settle(defect["labels"], critical_needs_two(defect["severity"]))
    return Defect(
        severity=defect["severity"],
        cls=defect["class"],
        source=found["source"],
        discovered=discovered,
        status=status if status in ("confirmed", "rejected") else "unknown",
    )


def check_case(case_id, case, *, mode: str, frozen: date) -> Case:
    require(isinstance(case_id, str) and IDENTIFIER.fullmatch(case_id), "case id")
    closed(case, CASE_FIELDS)
    require(case["stratum"] in STRATA, "case stratum")
    require(isinstance(case["cluster"], str) and IDENTIFIER.fullmatch(case["cluster"]), "cluster")
    origin = case["provenance"]
    closed(origin, "source issue pr round selection note")
    require(origin["source"] in SOURCES[mode], "provenance source for the corpus mode")
    for number in ("issue", "pr"):
        require(origin[number] is None or integer(origin[number], 1), "provenance number")
    require(nonempty(origin["selection"]) and isinstance(origin["note"], str), "provenance")
    if origin["source"] == "pr_branch":
        require(origin["issue"] is not None and integer(origin["round"], 1), "a branch round")
    elif origin["source"] == "squash":
        require(origin["round"] is None, "a squash candidate is no review round")
        require(origin["pr"] is not None, "a squash candidate names its PR")
    else:
        require(origin["round"] is None or integer(origin["round"], 1), "provenance round")
    review = case["review"]
    closed(review, REVIEW_FIELDS)
    require(nonempty(review["base_ref"]), "base ref")
    head, base = object_id(review["head"]), object_id(review["merge_base"])
    tip = object_id(review["base_tip"])
    object_id(review["tree"])
    require(head != base, "the candidate is above its merge base")
    require(tip != head, "the base revision is not the candidate")
    if origin["source"] == "squash":
        require(tip == base, "a squash candidate's base revision is its merge base")
    cutoff = instant(review["cutoff"])
    require(cutoff.date() <= frozen, "a candidate reviewed by the freeze")
    environment = case["environment"]
    closed(environment, "files validation")
    # Each file the repository declares its test environment (``check_corpus``
    # holds the names to that list), as its blob at the head or ``None`` when
    # the head has none, and at least one command a runner may validate with:
    # leaving a pin out is never how a case says it has none. A case none of
    # whose declared files is at its head pins no environment at all, so it
    # never runs (``pinned`` below): an absent placeholder pins nothing.
    require(isinstance(environment["files"], dict), "environment files")
    for path, blob in environment["files"].items():
        relative_path(path)
        if blob is not None:
            object_id(blob)
    require(
        isinstance(environment["validation"], list)
        and environment["validation"]
        and all(nonempty(command) for command in environment["validation"]),
        "environment validation",
    )
    withheld = case["withheld"]
    require(isinstance(withheld, list), "withheld commits")
    for commit in withheld:
        object_id(commit)
    require(len(set(withheld)) == len(withheld), "withheld commit repeated")
    require(not {head, base, tip} & set(withheld), "the candidate is not withheld")
    actors = case["actors"]
    require(
        isinstance(actors, list) and actors and all(nonempty(actor) for actor in actors),
        "historical actors",
    )
    barred = frozenset(actor.strip() for actor in actors)
    require(len(barred) == len(actors), "historical actor repeated")
    risk = case["risk"]
    closed(risk, "stratum rationale")
    require(risk["stratum"] in RISKS and nonempty(risk["rationale"]), "risk stratum")
    exposure = case["exposure"]
    closed(exposure, EXPOSURE_FIELDS)
    require(nonempty(exposure["artifact"]), "exposure artifact")
    possible_from = None if exposure["possible_from"] is None else day(exposure["possible_from"])
    public_by = None if exposure["public_by"] is None else day(exposure["public_by"])
    # The candidate existed by its cutoff, so nothing can rule exposure out
    # after it; ``verify`` also holds the day to the commits the case pins.
    require(
        possible_from is None or possible_from <= cutoff.date(), "exposure possible by the cutoff"
    )
    require(public_by is None or public_by <= frozen, "exposure public by the freeze")
    require(
        None in (possible_from, public_by) or possible_from <= public_by,
        "exposure possible before it was public",
    )
    forbidden = set(withheld)
    on_context = check_context(case["context"], origin=origin, cutoff=cutoff, frozen=frozen)
    recorded = check_record(
        case["record"], case=case, cutoff=cutoff, context=case["context"]["items"]
    )
    texts = list(reviewer_text(case))
    require(
        not any(names_commit(text, forbidden) for text in texts),
        "the reviewer's input names a withheld commit",
    )
    require(isinstance(case["defects"], dict), "defects")
    if STRATA[case["stratum"]]:
        require(case["defects"] and case["negative"] is None, "a defect case registers its defects")
    else:
        require(not case["defects"], "a negative case registers no defect")
    defects = {
        key: check_defect(
            key, defect, case=case, cutoff=cutoff, frozen=frozen, withheld=forbidden, barred=barred
        )
        for key, defect in case["defects"].items()
    }
    lineages = {key: check_lineage(defect, case=case) for key, defect in case["defects"].items()}
    # Every record retained for the case holds what its reviewer must not be
    # given: the round's own result, a merge, a fix. None reaches the input,
    # through a context item's text or any other value the reviewer reads.
    held = [(REVIEW if origin["source"] != "squash" else MERGE, case["record"], recorded)]
    held += [
        (LINEAGE, defect["discovery"]["lineage"], lineages[key])
        for key, defect in sorted(case["defects"].items())
    ]
    for form, kept, line in held:
        require(
            line is None or not exposes(texts, kept, line),
            f"the {form.kind} record is not the reviewer's context",
        )
    # A case runs only on what its records evidence: a review round on the
    # cutoff and base revision its review record states, a squash on its PR's
    # merge record, and every defect's fix on its lineage record; and only on
    # a test environment it pins.
    evidenced = recorded is not None or origin["source"] == "synthetic"
    lineaged = all(
        lineages[key] is not None or defect["discovery"]["fix"] is None
        for key, defect in case["defects"].items()
    )
    pinned = any(blob is not None for blob in environment["files"].values())
    runnable = on_context and evidenced and lineaged and pinned
    negative = None
    labels = [label for defect in case["defects"].values() for label in defect["labels"]]
    if STRATA[case["stratum"]]:
        sources = {defect.source for defect in defects.values()}
        require(sources & STRATUM_SOURCES[case["stratum"]], "a defect of the case's stratum")
    else:
        closed(case["negative"], "observed_through labels")
        through = day(case["negative"]["observed_through"])
        require(cutoff.date() <= through <= frozen, "observed between cutoff and freeze")
        labels = case["negative"]["labels"]
        check_labels(labels, NEGATIVE_VERDICTS, frozen=frozen, barred=barred)
        verdict = settle(labels)
        if verdict == "defective":
            negative = "refuted"
        elif verdict != "clean":
            negative = "unlabeled"
        elif (through - cutoff.date()).days < MIN_NEGATIVE_WINDOW_DAYS:
            negative = "censored"
        else:
            negative = "settled"
    return Case(
        id=case_id,
        cluster=case["cluster"],
        stratum=case["stratum"],
        risk=risk["stratum"],
        cutoff=cutoff,
        actors=barred,
        labelers=frozenset(label["by"].strip() for label in labels),
        defects=defects,
        negative=negative,
        runnable=runnable,
        input=input_digest(case),
        possible_from=possible_from,
        public_by=public_by,
    )


def check_clusters(corpus, cases: dict) -> None:
    """Cases of one issue, of one PR or of one defect's commit share a cluster.

    Rounds of one PR share code and their reviewers' history, so counting them
    as separate clusters would overstate the evidence. A cluster may still hold
    several issues the curator judges related.
    """
    owner: dict[tuple[str, str | int], str] = {}
    for case_id, case in sorted(corpus["cases"].items()):
        origin = case["provenance"]
        keys = [("issue", origin["issue"]), ("pr", origin["pr"])]
        keys += [("commit", defect["introduced"]) for defect in case["defects"].values()]
        cluster = cases[case_id].cluster
        for key in keys:
            if key[1] is not None:
                require(
                    owner.setdefault(key, cluster) == cluster,
                    "cases of one issue, PR or defect share a cluster",
                )


def check_corpus(corpus) -> Corpus:
    closed(corpus, CORPUS_FIELDS)
    require(corpus["format"] == CORPUS_FORMAT, "corpus format")
    require(type(corpus["version"]) is int and corpus["version"] == VERSION, "corpus version")
    require(corpus["mode"] in SOURCES, "corpus mode")
    require(isinstance(corpus["name"], str) and IDENTIFIER.fullmatch(corpus["name"]), "name")
    require(integer(corpus["release"], 1), "corpus release")
    frozen = day(corpus["frozen"])
    repository = corpus["repository"]
    closed(repository, REPOSITORY_FIELDS)
    require(nonempty(repository["slug"]), "repository slug")
    object_id(repository["root"])
    # Each base ref's tip at the freeze: the line a case's base revision is on.
    refs = repository["refs"]
    require(isinstance(refs, dict) and refs, "repository refs")
    for ref, tip in refs.items():
        require(nonempty(ref), "repository refs")
        object_id(tip)
    # The files that define the test environment, which every case pins.
    declared = repository["environment"]
    require(isinstance(declared, list) and declared, "repository environment")
    for path in declared:
        relative_path(path)
    require(len(set(declared)) == len(declared), "repository environment file repeated")
    require(isinstance(corpus["cases"], dict) and corpus["cases"], "registered cases")
    cases = {
        case_id: check_case(case_id, case, mode=corpus["mode"], frozen=frozen)
        for case_id, case in sorted(corpus["cases"].items())
    }
    for case in corpus["cases"].values():
        require(case["review"]["base_ref"] in refs, "a case's base ref is frozen")
        require(
            set(case["environment"]["files"]) == set(declared),
            "a case pins each of the repository's environment files",
        )
    check_clusters(corpus, cases)
    inputs = [case.input for case in cases.values()]
    require(len(set(inputs)) == len(inputs), "two cases with one review input")
    return Corpus(
        name=corpus["name"],
        release=corpus["release"],
        mode=corpus["mode"],
        frozen=frozen,
        digest=corpus_digest(corpus),
        cases=cases,
    )


def label_summary(cases: Iterable[Case]) -> dict:
    cases = list(cases)
    defects = [d for case in cases for d in case.defects.values()]

    def statuses(selected) -> dict:
        tally = Counter(d.status for d in selected)
        return {status: tally[status] for status in ("confirmed", "rejected", "unknown")}

    negatives = Counter(case.negative for case in cases if case.negative is not None)
    return {
        "defects": statuses(defects),
        "defects_by_severity": {
            s: statuses(d for d in defects if d.severity == s) for s in SEVERITIES
        },
        "defects_by_discovery": {
            s: statuses(d for d in defects if d.source == s) for s in DISCOVERY
        },
        "negatives": {
            status: negatives[status] for status in ("settled", "censored", "unlabeled", "refuted")
        },
    }


def describe(corpus) -> dict:
    """What a corpus holds and how much of it is settled, without any submission."""
    checked = check_corpus(corpus)
    cases = checked.cases.values()
    return {
        "corpus": {
            "name": checked.name,
            "release": checked.release,
            "mode": checked.mode,
            "frozen": checked.frozen.isoformat(),
            "sha256": checked.digest,
        },
        "cases": len(checked.cases),
        "clusters": len({case.cluster for case in cases}),
        "strata": dict(sorted(Counter(case.stratum for case in cases).items())),
        "risk": dict(sorted(Counter(case.risk for case in cases).items())),
        "runnable": sorted(case.id for case in cases if case.runnable),
        "labels": label_summary(cases),
        "inputs": {case.id: case.input for case in cases},
    }


# -- git ----------------------------------------------------------------------

# A read-only git that reads none of the operator's system or global
# configuration or GIT_* variables, fetches nothing, and honours no replacement
# object, hook or file-system monitor of the repository's own configuration.
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
)


class Git:
    def __init__(self, repo: Path):
        self.repo = repo
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key in GIT_ENV_KEEP or key.startswith("LC_")
        } | GIT_ENV
        self.lines: dict[str, list[tuple[str, int]] | None] = {}

    def run(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *GIT_SWITCHES, "-C", str(self.repo), *args],
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            check=False,
        )

    def out(self, *args: str) -> str | None:
        done = self.run(*args)
        return done.stdout.decode().strip() if done.returncode == 0 else None

    def kind(self, oid: str) -> str | None:
        return self.out("cat-file", "-t", oid)

    def resolve(self, spec: str) -> str | None:
        return self.out("rev-parse", "--verify", "--quiet", "--end-of-options", spec)

    def is_ancestor(self, older: str, newer: str) -> bool | None:
        code = self.run("merge-base", "--is-ancestor", older, newer).returncode
        return {0: True, 1: False}.get(code)

    def committed(self, commit: str) -> int | None:
        stamp = self.out("show", "-s", "--format=%ct", commit)
        return int(stamp) if stamp and stamp.isdigit() else None

    def parents(self, commit: str) -> list[str] | None:
        found = self.out("show", "-s", "--format=%P", commit)
        return None if found is None else found.split()

    def line(self, tip: str) -> list[tuple[str, int]] | None:
        """``tip``'s first-parent history, newest first, with committer times."""
        if tip not in self.lines:
            self.lines[tip] = self._line(tip)
        return self.lines[tip]

    def _line(self, tip: str) -> list[tuple[str, int]] | None:
        listed = self.out("rev-list", "--first-parent", "--timestamp", tip)
        if not listed:
            return None
        line = []
        for row in listed.splitlines():
            stamp, _, commit = row.partition(" ")
            if not stamp.isdigit() or not OBJECT_ID.fullmatch(commit):
                return None
            line.append((commit, int(stamp)))
        return line

    def merge_bases(self, one: str, other: str) -> list[str] | None:
        found = self.out("merge-base", "--all", one, other)
        return sorted(found.split()) if found else None

    def changed(self, commit: str) -> set[str] | None:
        """The paths ``commit`` changed against its first parent."""
        done = self.run(
            "diff-tree", "-r", "-z", "--name-only", "--no-renames", f"{commit}^1", commit
        )
        if done.returncode:
            return None
        return {path.decode() for path in done.stdout.split(b"\0") if path}

    def subject(self, commit: str) -> str | None:
        """The first line of ``commit``'s message, read from the raw object."""
        done = self.run("cat-file", "commit", commit)
        if done.returncode:
            return None
        _, _, message = done.stdout.partition(b"\n\n")
        return message.decode("utf-8", "replace").partition("\n")[0]

    def file_lines(self, commit: str, path: str) -> list[bytes] | None:
        """``path``'s lines at ``commit``, as git numbers them."""
        done = self.run("cat-file", "blob", f"{commit}:{path}")
        if done.returncode:
            return None
        body = done.stdout
        lines = body.split(b"\n")
        if body.endswith(b"\n") or not body:
            lines.pop()
        return lines

    def line_count(self, commit: str, path: str) -> int | None:
        lines = self.file_lines(commit, path)
        return None if lines is None else len(lines)

    def hunks(self, commit: str, path: str) -> list[tuple[int, int]] | None:
        """``commit``'s changes to ``path`` against its first parent, by old line.

        Each is the first line of the parent's file it replaces and how many it
        replaces; an insertion replaces none and follows the line it names.
        """
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
        if done.returncode:
            return None
        # An omitted count is one line; ``findall`` gives it as empty.
        return [(int(start), int(count or b"1")) for start, count in HUNK.findall(done.stdout)]


def utc_day(stamp: int) -> date:
    return datetime.fromtimestamp(stamp, UTC).date()


def line_tip(line: list[tuple[str, int]], cutoff: int) -> str | None:
    """The newest commit of ``line`` by ``cutoff``, or None when git cannot tell.

    Committer times name one only when they split the line at the cutoff:
    every commit after it on the line committed after the cutoff, it and every
    commit before it by then. Times out of order across the cutoff leave more
    than one commit that could have been the tip, so none is taken. Even a
    clean split is provisional: a commit is made before its ref reaches it, so
    one dated before the cutoff can have landed after it, and only a review
    record states which revision the review read.
    """
    newer = 0
    while newer < len(line) and line[newer][1] > cutoff:
        newer += 1
    if newer == len(line) or any(stamp > cutoff for _, stamp in line[newer:]):
        return None
    return line[newer][0]


def found_once(lines: list[bytes] | None, block: list[bytes]) -> tuple[int, int] | None:
    """Where ``block`` stands in ``lines``, by line, when it stands there exactly once."""
    if lines is None or not block:
        return None
    size = len(block)
    starts = [at for at in range(len(lines) - size + 1) if lines[at : at + size] == block]
    return (starts[0] + 1, starts[0] + size) if len(starts) == 1 else None


def touches(hunk: tuple[int, int], span: tuple[int, int]) -> bool:
    """Whether a hunk replaces a line of ``span``, or inserts among or beside them."""
    start, count = hunk
    first, last = span
    if count:
        return start <= last and start + count - 1 >= first
    return first - 1 <= start <= last


def located(git: Git, head: str, place) -> list[bytes] | None:
    """The lines a defect is located at, as they stand at the head."""
    lines = git.file_lines(head, place["path"])
    first, last = place["lines"]
    if lines is None or last > len(lines):
        return None
    return lines[first - 1 : last]


def addresses(git: Git, head: str, fix: str, location) -> bool:
    """Whether ``fix`` itself changed what a defect is located in, at one place at least.

    A whole-file place is addressed when the fix changes its file. A place with
    lines is addressed when the fix replaces one of those lines, or inserts
    among or beside them, as they stand, exactly once, at the fix's parent. A
    descendant that changes something else, however late, fixes nothing.
    """
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


def squashes(subject: str | None, pr) -> bool:
    """Whether a commit subject names ``pr`` as a squash merge's does, at its end."""
    return subject is not None and subject.endswith(f" (#{pr})")


def base_evidence(case) -> str:
    """What establishes a case's base revision: its review record, a squash's
    own parent, or, provisionally, its ref's line by committer times."""
    if case["provenance"]["source"] == "squash":
        return "squash_parent"
    return "review_record" if case["record"]["status"] == "captured" else "provisional"


def verify_case(git: Git, case, repository) -> list[str]:
    """The rules a historical case's pins fail against ``git``'s object store."""
    failed: list[str] = []

    def check(ok: object, rule: str) -> None:
        if ok is not True:
            failed.append(rule)

    review = case["review"]
    source = case["provenance"]["source"]
    head, tip, base = review["head"], review["base_tip"], review["merge_base"]
    cutoff = int(instant(review["cutoff"]).timestamp())
    check(git.kind(head) == "commit", "head is a commit")
    check(git.kind(base) == "commit", "merge base is a commit")
    check(git.kind(tip) == "commit", "base revision is a commit")
    line = git.line(repository["refs"][review["base_ref"]])
    check(line is not None, "the base ref's frozen tip is a commit")
    if failed or line is None:
        return failed
    on_line = {commit for commit, _ in line}
    check(git.resolve(f"{head}^{{tree}}") == review["tree"], "head tree")
    check(git.is_ancestor(repository["root"], head), "repository root is an ancestor of head")
    stamp = git.committed(head)
    if source == "pr_branch" and case["record"]["status"] == "captured":
        # The record's start, which the corpus check holds the cutoff to; the
        # withheld commits below bound it from above, the head from below.
        check(stamp is not None and stamp <= cutoff, "the head was committed by the review's start")
    else:
        # Without a record, the head's committer time: a review round's head,
        # or a squash commit, committed when it merged. A later cutoff would
        # admit context from after the review, such as its own findings.
        check(stamp is not None and stamp == cutoff, "the cutoff is the head's committer time")
    evidence = base_evidence(case)
    if source == "squash":
        # A squash is one commit on its parent, and its subject names the PR
        # it merged, as the merge record, which a runnable case holds, states.
        parents = git.parents(head)
        check(head in on_line, "a squash candidate is on its base ref's line")
        check(parents is not None and len(parents) == 1, "a squash candidate has one parent")
        check(
            parents is not None and parents[:1] == [base],
            "a squash candidate's parent is its merge base",
        )
        check(
            squashes(git.subject(head), case["provenance"]["pr"]),
            "a squash candidate is its PR's squash",
        )
    elif evidence == "review_record":
        # The base revision the record states, which the corpus check binds:
        # git can only show it consistent, a commit of the ref's line made by
        # the time the review read its input. A later commit dated before
        # then can have reached the ref after it, so the line's order by
        # committer time is no evidence either way.
        when = git.committed(tip)
        check(tip in on_line, "the base revision is on its ref's line")
        check(
            when is not None and when <= cutoff,
            "the base revision was committed by the review's start",
        )
    else:
        # Provisional: only a line whose committer times split at the cutoff
        # names one tip, and otherwise two base revisions, and so two diffs,
        # would both pass. Such a case is never runnable.
        at = line_tip(line, cutoff)
        if at is None:
            failed.append("the base ref's line splits at the cutoff")
        else:
            check(at == tip, "the base revision is its ref's tip at the cutoff")
    # The merge base the diff starts from is the base revision's with the head,
    # not merely some ancestor of the head: an older one is another diff.
    check(git.merge_bases(tip, head) == [base], "the merge base is the base revision's and head's")
    for index, (path, blob) in enumerate(sorted(case["environment"]["files"].items())):
        found = git.resolve(f"{head}:{path}")
        if blob is None:
            check(found is None, f"environment file {index} is absent at the head")
        else:
            check(found == blob and git.kind(blob) == "blob", f"environment file {index}")
    for commit in case["withheld"]:
        if git.kind(commit) != "commit":
            failed.append(f"withheld {commit[:12]} is a commit")
            continue
        check(git.is_ancestor(commit, head) is False, f"withheld {commit[:12]} is not in head")
        check(git.is_ancestor(commit, tip) is False, f"withheld {commit[:12]} is not in the base")
        stamp = git.committed(commit)
        check(stamp is not None and stamp > cutoff, f"withheld {commit[:12]} follows the cutoff")
    introduced_days = []
    for key, defect in sorted(case["defects"].items()):
        introduced = defect["introduced"]
        if git.kind(introduced) != "commit":
            failed.append(f"defect {key} introduced by a commit")
            continue
        introduced_days.append(git.committed(introduced))
        check(git.is_ancestor(introduced, head), f"defect {key} introduced in head")
        check(git.is_ancestor(introduced, base) is False, f"defect {key} above the merge base")
        fix, merged = defect["discovery"]["fix"], defect["discovery"]["merged"]
        if fix is not None and merged is None:
            check(git.is_ancestor(introduced, fix), f"defect {key} fix follows its introduction")
        elif fix is not None:
            # Squashed: the merged revision, which the lineage record states,
            # is consistent with git when it is one commit of the base line
            # whose subject names the case's PR as a squash does, that changed
            # every file the defect is located in and still holds each of its
            # lines as the head had them, and the fix or revert descends from
            # it. Consistency is no proof: a later commit can pass each check.
            pr = case["provenance"]["pr"]
            paths = {place["path"] for place in defect["location"]}
            changed = git.changed(merged) if git.kind(merged) == "commit" else None
            subject = git.subject(merged) if changed is not None else None
            parents = git.parents(merged) if changed is not None else None
            check(merged in on_line, f"defect {key} merged on its base ref's line")
            check(
                squashes(subject, pr) and parents is not None and len(parents) == 1,
                f"defect {key} merge is its PR's squash",
            )
            check(changed is not None and paths <= changed, f"defect {key} merge carries it")
            check(
                changed is not None
                and all(
                    found_once(
                        git.file_lines(merged, place["path"]), located(git, head, place) or []
                    )
                    for place in defect["location"]
                ),
                f"defect {key} survives its merge",
            )
            check(git.is_ancestor(merged, fix), f"defect {key} fix follows its merge")
        if fix is not None:
            # The fix or revert itself changed what the defect is located in:
            # consistent with its lineage record, though a comment beside the
            # defect passes as well as a fix of it.
            check(
                git.kind(fix) == "commit" and addresses(git, head, fix, defect["location"]),
                f"defect {key} fix changes what it is located in",
            )
        for index, place in enumerate(defect["location"]):
            count = git.line_count(head, place["path"])
            lines = place["lines"]
            check(
                count is not None and (lines is None or lines[1] <= count),
                f"defect {key} location {index}",
            )
    possible = case["exposure"]["possible_from"]
    if possible is not None:
        # Nothing of the answer can be public before the code it is about, but
        # the code is public no later than its commits: a later day is refused.
        stamps = [git.committed(head), *introduced_days]
        check(
            all(s is not None and day(possible) <= utc_day(s) for s in stamps),
            "exposure possible no later than the commits it is about",
        )
    return failed


def verify(corpus, repo: Path) -> dict:
    """Re-derive every pin of a historical corpus from a local object store."""
    checked = check_corpus(corpus)
    require(checked.mode == "historical", "only a historical corpus names real objects")
    git = Git(repo)
    repository = corpus["repository"]
    cases = {
        case_id: verify_case(git, corpus["cases"][case_id], repository) for case_id in checked.cases
    }
    # Each case says what its base revision rests on; ``provisional`` is a
    # guess from committer times that no record confirms.
    return {
        "corpus": checked.digest,
        "ok": not any(cases.values()),
        "cases": {
            case_id: {
                "ok": not failed,
                "failed": failed,
                "base_tip": base_evidence(corpus["cases"][case_id]),
            }
            for case_id, failed in cases.items()
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
    findings: (
        tuple  # (classification, verdict, defect) per finding; verdict confirmed/false/unknown
    )


def pins_digest(variant) -> str:
    """A variant as it was launched: the SHA-256 of its pins' canonical JSON.

    Every pin is in it (identity, provider and version, model, effort, stated
    knowledge cutoff, prompt template and digest, controller, sandbox), so a
    run recorded under one configuration is never credited to another.
    """
    return sha256(canonical(variant)).hexdigest()


def check_variant(name, variant) -> str:
    require(isinstance(name, str) and IDENTIFIER.fullmatch(name), "variant id")
    closed(variant, VARIANT_FIELDS)
    for key in ("identity", "provider", "provider_version", "model", "effort", "sandbox"):
        require(nonempty(variant[key]), f"variant {key}")
    if variant["knowledge_cutoff"] is not None:
        day(variant["knowledge_cutoff"])
    closed(variant["prompt"], "template sha256")
    relative_path(variant["prompt"]["template"])
    require(
        isinstance(variant["prompt"]["sha256"], str)
        and DIGEST.fullmatch(variant["prompt"]["sha256"]),
        "prompt digest",
    )
    object_id(variant["controller"])
    return variant["identity"].strip()


def finding_verdict(finding, case: Case) -> tuple:
    """A finding's settled label against the case's settled defects."""

    def need(verdict) -> int:
        kind, key = verdict
        return 2 if kind == "match" and case.defects[key].severity == "critical" else 1

    settled = settle(finding["labels"], need)
    if settled is None:
        return finding["classification"], "unknown", None
    if settled[0] == "false":
        return finding["classification"], "false", None
    status = case.defects[settled[1]].status
    verdict = {"confirmed": "confirmed", "rejected": "false"}.get(status, "unknown")
    return finding["classification"], verdict, settled[1] if verdict == "confirmed" else None


def check_run(run, *, corpus: Corpus, pins: dict, plan, frozen: date, barred) -> Run:
    """One run of a registered variant, ``pins`` holding each variant's pins digest."""
    closed(run, RUN_FIELDS)
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
    require(isinstance(run["findings"], list), "a completed run holds its findings")
    ids = set()
    findings = []
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
        )
        findings.append(finding_verdict(finding, case))
    return Run(run["variant"], case, run["seed"], status, run["cost_microusd"], tuple(findings))


def check_submissions(corpus: Corpus, submissions) -> list[Run]:
    closed(submissions, SUBMISSION_FIELDS)
    require(submissions["format"] == SUBMISSIONS_FORMAT, "submissions format")
    version = submissions["version"]
    require(type(version) is int and version == VERSION, "submissions version")
    require(submissions["mode"] == SUBMISSION_MODES[corpus.mode], "submissions mode for corpus")
    require(submissions["corpus"] == corpus.digest, "submissions name this exact corpus")
    frozen = day(submissions["frozen"])
    plan = submissions["plan"]
    closed(plan, "cases seeds")
    seeds = plan["seeds"]
    require(isinstance(plan["cases"], list) and plan["cases"], "planned cases")
    require(isinstance(seeds, list) and seeds, "seeds")
    variants = submissions["variants"]
    require(isinstance(variants, dict) and len(variants) >= 2, "a baseline and a variant")
    # The work a score expands to, bounded before any of it is expanded.
    require(
        len(variants) <= MAX_VARIANTS and len(plan["cases"]) * len(seeds) <= MAX_PLANNED_PAIRS,
        "the planned work is within its limits",
    )
    require(len(set(plan["cases"])) == len(plan["cases"]), "planned case repeated")
    for case_id in plan["cases"]:
        require(case_id in corpus.cases, "planned case registered")
        require(corpus.cases[case_id].runnable, "a planned case is runnable")
    require(
        len({corpus.cases[case_id].cluster for case_id in plan["cases"]}) <= MAX_PLANNED_CLUSTERS,
        "the planned work is within its limits",
    )
    require(all(integer(s, 1) for s in seeds), "seeds")
    require(len(set(seeds)) == len(seeds), "planned seed repeated")
    identities = [check_variant(name, variant) for name, variant in sorted(variants.items())]
    require(len(set(identities)) == len(identities), "variant identity repeated")
    require(submissions["baseline"] in variants, "the baseline is a registered variant")
    require(isinstance(submissions["runs"], list), "runs")
    barred = frozenset(identities)
    # The corpus is checked before any variant is known: a variant that labeled
    # any of its records, in any role, has seen its answers and cannot be scored
    # against them.
    for case in corpus.cases.values():
        require(not barred & case.labelers, "a reviewer variant cannot label the corpus")
    pins = {name: pins_digest(variant) for name, variant in variants.items()}
    runs = [
        check_run(run, corpus=corpus, pins=pins, plan=plan, frozen=frozen, barred=barred)
        for run in submissions["runs"]
    ]
    keys = [(run.variant, run.case.id, run.seed) for run in runs]
    require(len(set(keys)) == len(keys), "one run per variant, case and seed")
    return runs


# -- scoring ------------------------------------------------------------------

Counts = tuple[int, int, int]  # (yes, unknown, total), as review_eval.rate reads them


def run_counts(run: Run) -> dict[str, Counts]:
    """Each metric's observations in one completed run, keyed by metric.

    Recall counts the weight of the case's confirmed defects; an unknown
    finding may at most have found the heaviest one still undetected (an outer
    bound, not an attribution). Strata count defects, each bounded on its own.
    The run is read through its findings and its case's ``Tally`` only, so its
    cost grows with its findings, never with the case's defects.
    """
    case = run.case
    tally = case.tally
    detected = {key for _, verdict, key in run.findings if verdict == "confirmed"}
    verdicts = Counter(verdict for _, verdict, _ in run.findings)
    unknown = verdicts["unknown"]
    out: dict[str, Counts] = {
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
    if tally.by_severity:
        hit = {
            "severity": Counter(tally.severity[key] for key in detected),
            "discovery": Counter(tally.source[key] for key in detected),
        }
        # The heaviest missed defects first, one per unknown finding.
        extra, left = 0, unknown
        for severity in sorted(SEVERITIES, key=WEIGHT.__getitem__, reverse=True):
            taken = min(left, tally.by_severity[severity] - hit["severity"][severity])
            extra, left = extra + taken * WEIGHT[severity], left - taken
        found = sum(WEIGHT[s] * n for s, n in hit["severity"].items())
        total = sum(WEIGHT[s] * n for s, n in tally.by_severity.items())
        out["recall"] = (found, extra, total)
        out[f"recall/risk/{case.risk}"] = (found, extra, total)
        for name, counts, values in (
            ("severity", tally.by_severity, SEVERITIES),
            ("discovery", tally.by_source, DISCOVERY),
        ):
            for value in values:
                if counts[value]:
                    got = hit[name][value]
                    out[f"recall/{name}/{value}"] = (
                        got,
                        min(unknown, counts[value] - got),
                        counts[value],
                    )
    if case.negative == "settled":
        alarm = verdicts["false"] > 0
        out["false_positive_rate"] = (int(alarm), int(not alarm and unknown > 0), 1)
    return out


def metric_keys() -> list[str]:
    keys = ["recall", "precision", "false_positive_rate"]
    keys += [f"recall/severity/{s}" for s in SEVERITIES]
    keys += [f"recall/discovery/{s}" for s in DISCOVERY]
    keys += [f"recall/risk/{r}" for r in RISKS]
    keys += [f"precision/classification/{c}" for c in CLASSIFICATIONS]
    return keys


@lru_cache(maxsize=BOOTSTRAP_TABLES)
def multiplicities(clusters: int) -> tuple[tuple[int, ...], ...]:
    """How often each cluster is drawn in each bootstrap resample.

    The draws depend only on how many clusters a metric observes, so every
    metric and both arms of a contrast over the same clusters share them.
    """
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
    """Each distinct resample of :func:`multiplicities` with how often it was drawn."""
    return tuple(sorted(Counter(multiplicities(clusters)).items()))


def weighted(groups: Sequence[Counts], drawn: Sequence[int]) -> Counts:
    return (
        sum(m * g[0] for m, g in zip(drawn, groups, strict=True)),
        sum(m * g[1] for m, g in zip(drawn, groups, strict=True)),
        sum(m * g[2] for m, g in zip(drawn, groups, strict=True)),
    )


def nearest_rank(tally: Counter, rank: int):
    """The ``rank``/40th nearest-rank percentile of the defined values in ``tally``."""
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


def interval(tally: Counter) -> list:
    return [encoded(nearest_rank(tally, 1)), encoded(nearest_rank(tally, 39))]


def encoded(value):
    return str(value) if value is not None else None


def pooled(groups: Iterable[Counts]) -> Counts:
    yes = unknown = total = 0
    for group in groups:
        yes, unknown, total = yes + group[0], unknown + group[1], total + group[2]
    return yes, unknown, total


Observed = list[tuple[str, dict[str, Counts]]]  # (cluster, run_counts) per completed run
RunKey = tuple[str, str, int]  # variant, case, seed


def run_key(run: Run) -> RunKey:
    return run.variant, run.case.id, run.seed


def observed(runs: Iterable[Run], counts: dict[RunKey, dict[str, Counts]]) -> Observed:
    """The runs' observations, each computed once (``counts``) for every metric
    and every contrast that reads it."""
    return [(run.case.cluster, counts[run_key(run)]) for run in runs]


def by_cluster(runs: Observed, key: str) -> dict[str, Counts]:
    """A metric's pooled observations per cluster, for the clusters that hold any."""
    clusters: dict[str, list[Counts]] = {}
    for cluster, counts in runs:
        if key in counts:
            clusters.setdefault(cluster, []).append(counts[key])
    return {cluster: pooled(groups) for cluster, groups in sorted(clusters.items())}


def band_ok(bounds) -> bool:
    return bounds[0] is not None and bounds[2] is not None and bounds[2] - bounds[1] <= LABEL_BAND


def marginal(runs: Observed, key: str) -> dict:
    groups = by_cluster(runs, key)
    recall = key.startswith("recall")
    total = pooled(groups.values())
    point, low, high = rate([total], recall=recall) if groups else (None, None, None)
    points, lows, highs = Counter(), Counter(), Counter()
    values = list(groups.values())
    for drawn, times in distinct_draws(len(values)) if values else ():
        p, lo, hi = rate([weighted(values, drawn)], recall=recall)
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
    """Variant minus baseline over paired runs, resampling clusters together."""
    recall = key.startswith("recall")
    b_groups, t_groups = by_cluster(base_runs, key), by_cluster(other_runs, key)
    clusters = sorted(set(b_groups) | set(t_groups))
    b_values = [b_groups.get(c, (0, 0, 0)) for c in clusters]
    t_values = [t_groups.get(c, (0, 0, 0)) for c in clusters]
    b = rate([pooled(b_values)], recall=recall) if clusters else (None, None, None)
    t = rate([pooled(t_values)], recall=recall) if clusters else (None, None, None)
    points, lows, highs = Counter(), Counter(), Counter()
    for drawn, times in distinct_draws(len(clusters)) if clusters else ():
        bd = rate([weighted(b_values, drawn)], recall=recall)
        td = rate([weighted(t_values, drawn)], recall=recall)
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
    """Whether a variant's training data may hold the case's answer.

    ``possible`` when the variant's stated knowledge cutoff falls on or after a
    day by which part of the answer was public (``Case.public``). ``none`` only
    when the case's exposure evidence rules every earlier day out: the cutoff
    falls before the first day the answer could have been public. The candidate
    being newer proves nothing on its own, since its head can carry code, and a
    finding about it, from a revision public before it. ``unknown`` otherwise:
    no knowledge cutoff, no evidence, or a cutoff between the two days.
    """
    if knowledge_cutoff is None:
        return "unknown"
    known = day(knowledge_cutoff)
    if known >= case.public:
        return "possible"
    if case.possible_from is not None and known < case.possible_from:
        return "none"
    return "unknown"


def score(corpus, submissions, repo: Path | None = None) -> dict:
    """Scorecards of every variant and its contrast with the baseline.

    A historical corpus is scored only once ``verify`` accepts that exact
    corpus against the object store at ``repo``: a cutoff or merge base the
    object store contradicts never reaches a recorded scorecard.
    """
    checked = check_corpus(corpus)
    verified = None
    if checked.mode == "historical":
        if repo is None:
            raise CorpusError("a recorded score verifies its corpus")
        require(verify(corpus, repo)["ok"], "a recorded score verifies its corpus")
        verified = {"corpus": checked.digest, "ok": True}
    runs = check_submissions(checked, submissions)
    # Each completed run's observations, once: every metric of its scorecard
    # and of each contrast it is paired in reads the same counts.
    counts = {run_key(run): run_counts(run) for run in runs if run.status == "completed"}
    plan = submissions["plan"]
    planned = [(case_id, seed) for case_id in plan["cases"] for seed in plan["seeds"]]
    by_variant = {
        name: {(run.case.id, run.seed): run for run in runs if run.variant == name}
        for name in submissions["variants"]
    }
    cards = {}
    for name, variant in sorted(submissions["variants"].items()):
        mine = by_variant[name]
        completed = [
            mine[key] for key in planned if key in mine and mine[key].status == "completed"
        ]
        statuses = Counter(mine[key].status if key in mine else "missing" for key in planned)
        verdicts = Counter(verdict for run in completed for _, verdict, _ in run.findings)
        duplicates = 0
        for run in completed:
            hits = [key for _, verdict, key in run.findings if verdict == "confirmed"]
            duplicates += len(hits) - len(set(hits))
        launched = [r for r in mine.values() if r.status != "excluded"]
        observations = observed(completed, counts)
        exposed = Counter(exposure(r.case, variant["knowledge_cutoff"]) for r in completed)
        costs = [r.cost for r in launched]
        cards[name] = {
            "pins": variant,
            "pins_sha256": pins_digest(variant),
            "runs": {
                status: statuses[status]
                for status in ("completed", "failed", "excluded", "missing")
            },
            "coverage_complete": statuses["failed"] + statuses["missing"] == 0,
            "findings": {
                "confirmed": verdicts["confirmed"],
                "false": verdicts["false"],
                "unknown": verdicts["unknown"],
                "duplicates": duplicates,
            },
            "training_exposure": {
                level: exposed[level] for level in ("none", "possible", "unknown")
            },
            "cost_microusd": sum(costs) if None not in costs else None,
            "metrics": {key: marginal(observations, key) for key in metric_keys()},
        }
    baseline = submissions["baseline"]
    contrasts = {}
    for name in sorted(submissions["variants"]):
        if name == baseline:
            continue
        excluded = [
            key
            for key in planned
            if any(
                key in by_variant[arm] and by_variant[arm][key].status == "excluded"
                for arm in (baseline, name)
            )
        ]
        paired = [
            key
            for key in planned
            if key not in excluded
            and all(
                key in by_variant[arm] and by_variant[arm][key].status == "completed"
                for arm in (baseline, name)
            )
        ]
        incomplete = [key for key in planned if key not in excluded and key not in paired]
        base_runs = observed((by_variant[baseline][key] for key in paired), counts)
        other_runs = observed((by_variant[name][key] for key in paired), counts)
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
                "incomplete": [list(key) for key in incomplete],
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


# -- files and the command line -----------------------------------------------


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate JSON key")
        value[key] = item
    return value


def _no_constant(name):
    raise CorpusError("non-finite number")


def read_json(path: Path):
    """Read one regular JSON file, never through a final link or from a FIFO."""
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
    shown = commands.add_parser("describe", help="what a corpus holds and how much is settled")
    shown.add_argument("corpus", type=Path, nargs="?", default=SEED)
    checked = commands.add_parser("verify", help="re-derive a historical corpus's pins from git")
    checked.add_argument("corpus", type=Path, nargs="?", default=SEED)
    checked.add_argument("--repo", type=Path, default=Path.cwd())
    scored = commands.add_parser("score", help="score submissions against a corpus")
    scored.add_argument("corpus", type=Path, nargs="?", default=SYNTHETIC_CORPUS)
    scored.add_argument("submissions", type=Path, nargs="?", default=SYNTHETIC_SUBMISSIONS)
    scored.add_argument(
        "--repo", type=Path, help="the object store a historical corpus verifies in"
    )
    args = parser.parse_args(argv)
    try:
        corpus = read_json(args.corpus)
        if args.command == "describe":
            report = describe(corpus)
        elif args.command == "verify":
            report = verify(corpus, args.repo)
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
        # The files are evidence, never an error message to echo.
        parser.exit(2, "Invalid review corpus or submissions.\n")
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
