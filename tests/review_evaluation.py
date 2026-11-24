"""Offline review scorecard. Run with ``python -m tests.review_evaluation``."""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import chain
from math import ceil
from pathlib import Path
from typing import NamedTuple

from tests.review_evidence import ARMS, WEIGHTS, Case, DefectId, Run, admit, load, number

FIXTURE = Path(__file__).with_name("fixtures") / "review_evaluation" / "study.json"
PRIMARY = Fraction("0.10")
LABEL_LIMIT = Fraction("0.10")
EXCLUSION_LIMIT = Fraction("0.10")
COST_INCREASE = Fraction("0.50")
MINIMUM = {"defect": 20, "clean_negative": 10}
MARGINS = {
    "precision": Fraction("0.05"),
    "clean_negative_fpr": Fraction("0.05"),
    "harmful_fix_rate": Fraction("0.05"),
    "duplicate_ratio": Fraction("0.10"),
}
RESAMPLES = 10_000
SEED = 355


def ratio(n: int | Fraction, d: int) -> Fraction | None:
    return Fraction(n, d) if d else None


class Band(NamedTuple):
    point: Fraction | None
    low: Fraction | None
    high: Fraction | None

    @property
    def usable(self) -> bool:
        return (
            self.point is not None
            and self.low is not None
            and self.high is not None
            and self.high - self.low <= LABEL_LIMIT
        )


def binary(yes: int, no: int, unknown: int) -> Band:
    total = yes + no + unknown
    return Band(ratio(yes, yes + no), ratio(yes, total), ratio(yes + unknown, total))


def recall_ceiling(edges: dict[DefectId, list[int]], weights: dict[DefectId, int]) -> int:
    """Greedy by severity on a transversal matroid; augment to admit each defect."""
    owners: dict[int, DefectId] = {}
    total = 0
    for key in sorted(edges, key=weights.__getitem__, reverse=True):
        # Find an alternating path to a free finding slot, without recursion.
        queue = [key]
        back: dict[DefectId, tuple[DefectId, int] | None] = {key: None}
        for defect in queue:
            free = None
            for slot in edges[defect]:
                if slot not in owners:
                    free = slot
                    break
                previous = owners[slot]
                if previous not in back:
                    queue.append(previous)
                    back[previous] = defect, slot
            if free is not None:
                owners[free] = defect
                while (step := back[defect]) is not None:
                    defect, slot = step
                    owners[slot] = defect
                total += weights[key]
                break
    return total


@dataclass
class Counts:
    recall: tuple[int, int, int] = (0, 0, 0)  # seen weight, possible weight, exposure weight
    precision: tuple[int, int, int] = (0, 0, 0)
    negative: tuple[int, int, int] = (0, 0, 0)
    harm: tuple[int, int, int] = (0, 0, 0)
    duplicates: tuple[int, int, int, int] = (0, 0, 0, 0)  # D, C, possible new, possible repeats
    groups: Counter[int] = field(default_factory=Counter)


def observations(run: Run, case: Case) -> Counts:
    seen: set[DefectId] = set()
    exposed: set[DefectId] = set()
    opportunities: list[tuple[set[DefectId], int]] = []
    precision: Counter = Counter()
    negative: Counter = Counter()
    negative_reviews = 0
    duplicate = new = repeat = 0
    groups: Counter[int] = Counter()
    for review in run.reviews:
        if review.disposition != "accepted":
            continue
        eligible = run.history.eligible_at(review.commit)
        exposed.update(eligible)
        confirmed = [label for label in review.findings if isinstance(label, tuple)]
        false, unknown = review.findings.count("false"), review.findings.count("unknown")
        precision.update(confirmed=len(confirmed), false=false, unknown=unknown)
        distinct = set(confirmed)
        seen.update(distinct)
        opportunities.append((eligible, unknown))
        duplicate += len(confirmed) - len(distinct)
        new += min(unknown, len(eligible - distinct))
        if distinct:
            repeat += unknown
        elif eligible and unknown:
            groups[unknown] += 1
        if case.kind == "clean_negative" and review.commit in case.history.files:
            negative_reviews += 1
            negative.update(false=false, unknown=unknown)

    weights = {key: WEIGHTS[run.history.defects[key].severity] for key in exposed}
    edges: dict[DefectId, list[int]] = {}
    offset = 0
    for eligible, unknown in opportunities:
        candidates = eligible - seen
        # At most one slot per remaining defect is needed even if many findings are unknown.
        slots = range(offset, offset + min(unknown, len(candidates)))
        if slots:
            for defect in candidates:
                edges.setdefault(defect, []).extend(slots)
            offset = slots.stop
    detected = sum(weights[key] for key in seen)
    harm = Counter(verdict for verdict, _ in run.fixes.values())
    fp = int(bool(negative["false"]))
    unset = int(not fp and (not negative_reviews or bool(negative["unknown"])))
    return Counts(
        (detected, detected + recall_ceiling(edges, weights), sum(weights.values())),
        (precision["confirmed"], precision["false"], precision["unknown"]),
        (fp, 1 - fp - unset, unset) if case.kind == "clean_negative" else (0, 0, 0),
        (harm["harmful"], harm["not_harmful"], harm["unknown"]),
        (duplicate, precision["confirmed"], new, repeat),
        groups,
    )


def bands(counts: list[Counts]) -> dict[str, Band]:
    hits, possible, exposed = map(sum, zip((0, 0, 0), *(c.recall for c in counts), strict=True))
    dup, confirmed, new, repeat = map(
        sum, zip((0, 0, 0, 0), *(c.duplicates for c in counts), strict=True)
    )
    groups: Counter[int] = Counter()
    for c in counts:
        groups.update(c.groups)
    # If a round has no confirmation, confirming all its unknowns against
    # one eligible defect gives (k-1)/k new duplicates. Add such groups in
    # descending density, only while they raise the pooled ratio.
    num, den = dup + repeat, confirmed + repeat
    for size, count in sorted(groups.items(), reverse=True):
        if not den or (size - 1) * den > num * size:
            num += (size - 1) * count
            den += size * count
    return {
        "weighted_recall": Band(
            ratio(hits, exposed), ratio(hits, exposed), ratio(possible, exposed)
        ),
        "precision": binary(*map(sum, zip((0, 0, 0), *(c.precision for c in counts), strict=True))),
        "clean_negative_fpr": binary(
            *map(sum, zip((0, 0, 0), *(c.negative for c in counts), strict=True))
        ),
        "harmful_fix_rate": binary(
            *map(sum, zip((0, 0, 0), *(c.harm for c in counts), strict=True))
        ),
        "duplicate_ratio": Band(
            ratio(dup, confirmed), ratio(dup, confirmed + new), ratio(num, den)
        ),
    }


def rate_record(band: Band) -> dict:
    return {"point": band.point, "low": band.low, "high": band.high, "usable": band.usable}


@dataclass(frozen=True)
class Outcome:
    evidence: str
    escapes: frozenset[DefectId] | None
    success: bool | None


def outcome(run: Run) -> Outcome:
    status, review = run.terminal()
    if review is None:
        return Outcome(status, None, None)
    if status == "not_issued":
        return Outcome(status, frozenset(), False)
    escapes = frozenset(run.history.eligible_at(review.commit))
    success = review.validation == "passed" and all(
        run.history.defects[key].severity not in ("critical", "major") for key in escapes
    )
    return Outcome(status, escapes, success)


def total_usage(runs: list[Run], unit: str, *, available: bool) -> Fraction | None:
    if not available or not runs or not all(run.complete_window() for run in runs):
        return None
    values = [row.get(unit) for run in runs for row in run.attempts.values()]
    if any(value is None for value in values):
        return None
    return sum(map(number, values), Fraction())


def cost(runs: list[Run], *, available: bool) -> Fraction | None:
    successes = [outcome(run).success for run in runs]
    usage = total_usage(runs, "usd", available=available)
    if usage is None or None in successes:
        return None
    return ratio(usage, successes.count(True))


def critical_gate(
    baseline: dict[str, Run], treatment: dict[str, Run]
) -> tuple[list[tuple[str, DefectId]], bool]:
    novel = []
    unsettled = False
    for cid, run in treatment.items():
        escapes = outcome(run).escapes
        if escapes is None:
            unsettled = True
            continue
        other = baseline.get(cid)
        previous = outcome(other).escapes if other else None
        for key in sorted(escapes, key=lambda key: (key[0] or "", key[1])):
            if run.history.defects[key].severity != "critical":
                continue
            if key[0] is not None or (previous is not None and key not in previous):
                novel.append((cid, key))
            elif previous is None:
                unsettled = True
    return novel, unsettled


def nearest_rank(values: list[Fraction], p: Fraction) -> Fraction | None:
    return sorted(values)[ceil(p * len(values)) - 1] if values else None


def difference(old: Band, new: Band) -> Band:
    def delta(left: Fraction | None, right: Fraction | None) -> Fraction | None:
        return right - left if left is not None and right is not None else None

    return Band(delta(old.point, new.point), delta(old.high, new.low), delta(old.low, new.high))


def bootstrap(pairs: list[tuple[Counts, Counts]], samples: int = RESAMPLES) -> dict:
    rng = random.Random(SEED)
    draws: dict[str, tuple[list[Fraction], list[Fraction], list[Fraction]]] = {
        key: ([], [], []) for key in bands([])
    }
    for _ in range(samples if pairs else 0):
        chosen = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        left, right = zip(*chosen, strict=True)
        old, new = bands(list(left)), bands(list(right))
        for key, buckets in draws.items():
            for bucket, value in zip(buckets, difference(old[key], new[key]), strict=True):
                if value is not None:
                    bucket.append(value)
    p, q = Fraction("0.025"), Fraction("0.975")
    return {
        key: {
            "point_interval": (nearest_rank(points, p), nearest_rank(points, q)),
            "label_envelope": (nearest_rank(lows, p), nearest_rank(highs, q)),
            "undefined_draws": dict(
                zip(("point", "low", "high"), (samples - len(b) for b in buckets), strict=True)
            ),
        }
        for key, buckets in draws.items()
        for points, lows, highs in [buckets]
    }


def decide(
    *,
    sizes: Counter,
    unpaired: list[str],
    exclusion_share: Fraction,
    critical: list,
    unsettled: bool,
    old: dict[str, Band],
    new: dict[str, Band],
    intervals: dict,
    old_cost: Fraction | None,
    new_cost: Fraction | None,
) -> tuple[str, str]:
    if critical:
        return "NO_GO", "new certified critical escape"
    cost_known = old_cost is not None and new_cost is not None
    if cost_known and new_cost > old_cost * (1 + COST_INCREASE):
        return "NO_GO", "measured cost above margin"
    for key, limit in MARGINS.items():
        low, high = intervals[key]["label_envelope"]
        if (
            old[key].usable
            and new[key].usable
            and (
                (high is not None and high < -limit)
                if key == "precision"
                else (low is not None and low > limit)
            )
        ):
            return "NO_GO", f"{key} beyond margin"
    if (
        unpaired
        or exclusion_share > EXCLUSION_LIMIT
        or any(sizes[kind] < minimum for kind, minimum in MINIMUM.items())
    ):
        return "INCONCLUSIVE", "paired sample or exclusion limit"
    low, high = intervals["weighted_recall"]["label_envelope"]
    recall = difference(old["weighted_recall"], new["weighted_recall"])
    if (
        old["weighted_recall"].usable
        and new["weighted_recall"].usable
        and high is not None
        and high < PRIMARY
    ):
        return "NO_GO", "recall improvement below target"
    if (
        unsettled
        or not cost_known
        or not all(b.usable for b in chain(old.values(), new.values()))
        or recall.low is None
        or recall.low < PRIMARY
        or low is None
        or low <= 0
    ):
        return "INCONCLUSIVE", "outcome, usage, or recall improvement unresolved"
    for key, limit in MARGINS.items():
        low, high = intervals[key]["label_envelope"]
        if (low is None or low < -limit) if key == "precision" else (high is None or high > limit):
            return "INCONCLUSIVE", f"{key} margin unresolved"
    return "GO", "all registered criteria met"


def report(data: dict) -> dict:
    mode, cases, runs = admit(data)
    # No accepted source of complete, real billing/context usage at R0.
    # Fixtures exercise the calculation, but cannot return GO or NO_GO.
    simulate = mode == "fixture"
    per_arm = {}
    admitted: dict[str, dict[str, Run]] = {}
    counts: dict[str, Counts] = {}
    for arm in ARMS:
        recorded = [run for run in runs if run.arm == arm]
        included = {run.case: run for run in recorded if run.excluded is None}
        admitted[arm] = included
        for run in included.values():
            counts[run.id] = observations(run, cases[run.case])
        scored = list(included.values())
        escapes = Counter(
            run.history.defects[key].severity
            for run in scored
            for key in outcome(run).escapes or ()
        )
        per_arm[arm] = {
            "planned_cases": len(cases),
            "missing": sorted(cases.keys() - {run.case for run in recorded}),
            "excluded": {run.case: run.excluded for run in recorded if run.excluded is not None},
            "rates": {
                key: rate_record(band)
                for key, band in bands([counts[run.id] for run in scored]).items()
            },
            "known_escapes": dict(escapes),
            "escaped_critical_defects": (
                escapes["critical"]
                if scored and all(outcome(r).escapes is not None for r in scored)
                else None
            ),
            "cost_per_validated_success": cost(scored, available=simulate),
            "launch_wall_clock_seconds": total_usage(scored, "seconds", available=True),
            "context_tokens": total_usage(scored, "context_tokens", available=simulate),
        }
    baseline, treatment = (admitted[arm] for arm in ARMS)
    shared = sorted(baseline.keys() & treatment.keys())
    pairs = [(counts[baseline[cid].id], counts[treatment[cid].id]) for cid in shared]
    old, new = (bands([pair[i] for pair in pairs]) for i in (0, 1))
    intervals = bootstrap(pairs)
    critical, unsettled = critical_gate(baseline, treatment)
    missing = sorted(set(chain.from_iterable(arm["missing"] for arm in per_arm.values())))
    exclusion_share = Fraction(
        len(set().union(*(arm["excluded"] for arm in per_arm.values()))), len(cases)
    )
    sizes = Counter(
        cases[cid].kind
        for cid in shared
        if all(
            any(
                r.disposition == "accepted" and r.commit == cases[cid].candidate
                for r in run.reviews
            )
            for run in (baseline[cid], treatment[cid])
        )
    )
    if simulate or not all(any(run.arm == arm for run in runs) for arm in ARMS):
        decision, reason = "NOT_EVALUATED", "fixture or missing arm"
    else:
        decision, reason = decide(
            sizes=sizes,
            unpaired=missing,
            exclusion_share=exclusion_share,
            critical=critical,
            unsettled=unsettled
            or any(
                outcome(run).success is None for run in chain(baseline.values(), treatment.values())
            ),
            old=old,
            new=new,
            intervals=intervals,
            old_cost=None,
            new_cost=None,
        )
    return {
        "mode": mode,
        "decision": decision,
        "reason": reason,
        "case_count": len(cases),
        "cases": {
            cid: {"kind": case.kind, "risk": case.risk, "candidate": case.candidate}
            for cid, case in cases.items()
        },
        "arms": per_arm,
        "paired": {
            "sizes": dict(sizes),
            "excluded_share": exclusion_share,
            "missing_cases": missing,
            "difference": {key: difference(old[key], new[key])._asdict() for key in old},
            "bootstrap": intervals,
            "new_critical_escapes": critical,
            "critical_gate_unsettled": unsettled,
        },
        "runs": {
            run.id: {
                "case": run.case,
                "arm": run.arm,
                "excluded": run.excluded,
                "stop": run.stop.reason if run.stop else None,
                "evidence": outcome(run).evidence,
                "escapes": sorted(escapes, key=lambda key: (key[0] or "", key[1]))
                if (escapes := outcome(run).escapes) is not None and run.excluded is None
                else None,
                "validated_success": outcome(run).success if run.excluded is None else None,
                "launches": [
                    {key: run.attempts[seq][key] for key in ("seq", "phase", "round", "outcome")}
                    for seq in sorted(run.attempts)
                ],
                "complete_launch_window": run.complete_window(),
            }
            for run in runs
        },
    }


def display(value: Fraction) -> str:
    scaled = round(value * 1_000_000)
    whole, decimals = divmod(abs(scaled), 1_000_000)
    return f"{'-' if scaled < 0 else ''}{whole}.{decimals:06}"


def render(result: dict) -> str:
    return json.dumps(result, default=display, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "score"))
    parser.add_argument("--study", type=Path, default=FIXTURE)
    args = parser.parse_args()
    try:
        data = load(args.study)
        if args.command == "verify":
            _, cases, runs = admit(data)
            print(f"Verified {len(cases)} cases and {len(runs)} runs (data-only replay).")
        else:
            print(render(report(data)), end="")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"review evidence refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
