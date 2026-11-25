"""REV-R0: replay normalized review evidence, never a live AutoForge run."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections import Counter
from datetime import date
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from random import Random

FIXTURE = Path(__file__).parent / "fixtures" / "review_eval" / "synthetic.json"
MAX_PACKET_BYTES = 8 * 1024 * 1024
WEIGHT = {"critical": 8, "high": 4, "medium": 2, "low": 1}
CLASSES = {"correctness", "security", "reliability", "specification", "maintainability"}
ARMS = ("baseline", "treatment")
EXCLUSIONS = {"infrastructure", "credentials_unavailable", "service_unavailable", "skipped"}
OUTCOMES = {
    "ANALYZE_EXECUTE": {"retry", "published"},
    "REVIEW": {"retry", "clean", "needs_fix", "stale_clean", "stale_needs_fix"},
    "FIX": {"retry", "held", "no_change", "published", "partial"},
}
# A held FIX and a published partial FIX stop the run until an operator acts:
# the end of this first-stop slice, after which no further ordinal is admitted.
STOPPED = {"HELD", "PARTIAL"}


class EvidenceError(ValueError):
    """The packet contains contradictory or malformed evidence."""


def require(condition: object, rule: str) -> None:
    if not condition:
        raise EvidenceError(rule)


def object_fields(value, fields: str) -> None:
    require(isinstance(value, dict) and value.keys() == set(fields.split()), f"fields: {fields}")


def sha(value, size: int = 40) -> None:
    require(isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{size}}}", value), "digest shape")


def nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def source(value, case, run=None) -> bool:
    """Return eligibility at freeze, not the truth of an assertion."""
    object_fields(value, "adjudicator artifact discovered")
    require(nonempty(value["adjudicator"]) and nonempty(value["artifact"]), "label provenance")
    if run is not None:
        require(value["adjudicator"] not in run["actors"], "agent cannot adjudicate")
    return date.fromisoformat(value["discovered"]) <= date.fromisoformat(case["cutoff"])


def snapshot(case, head, tree):
    snap = case["snapshots"].get(head)
    require(
        snap is not None and snap["tree"] == tree and head != case["merge_base"], "exact candidate"
    )
    return snap


def check_case(case) -> None:
    object_fields(case, "base_ref merge_base start cutoff clean_negative seeds defects snapshots")
    date.fromisoformat(case["cutoff"])
    require(
        nonempty(case["base_ref"]) and type(case["clean_negative"]) is bool, "case registration"
    )
    seeds = case["seeds"]
    require(
        isinstance(seeds, list)
        and seeds
        and all(type(seed) is int and seed >= 0 for seed in seeds)
        and len(set(seeds)) == len(seeds),
        "registered seeds",
    )
    base, start = case["merge_base"], case["start"]
    snaps = case["snapshots"]
    require(base in snaps and start in snaps and start != base, "registered start and merge base")
    for head, snap in snaps.items():
        sha(head)
        object_fields(snap, "parent tree present oracle source")
        sha(snap["tree"])
        parent = snap["parent"]
        require(parent is None if head == base else parent in snaps, "snapshot parent")
        require(snap["oracle"] in ("pass", "fail", None), "independent oracle outcome")
        require(
            isinstance(snap["present"], list)
            and len(set(snap["present"])) == len(snap["present"])
            and set(snap["present"]) <= case["defects"].keys(),
            "snapshot defect inventory",
        )
        source(snap["source"], case)
        cursor, seen = head, set()
        while cursor is not None:
            require(cursor in snaps and cursor not in seen, "snapshot ancestry")
            seen.add(cursor)
            cursor = snaps[cursor]["parent"]
        require(base in seen, "snapshots must descend from the registered merge base")
    require(not snaps[base]["present"], "registered defects must be absent from the merge base")
    # The starting tip and its registered ancestors above the merge base. The
    # ancestry walk above has already proven this path finite and rooted.
    lineage, cursor = set(), start
    while cursor != base:
        lineage.add(cursor)
        cursor = snaps[cursor]["parent"]
    for key, defect in case["defects"].items():
        object_fields(defect, "severity class introduced source")
        require(defect["severity"] in WEIGHT and defect["class"] in CLASSES, "defect taxonomy")
        source(defect["source"], case)
        introduced = defect["introduced"]
        require(introduced in snaps and introduced != base, "defect introduction")
        # A starting defect keeps the SHA that actually introduced it: the
        # starting tip or any earlier commit of its registered lineage.
        require(
            key not in snaps[start]["present"] or introduced in lineage,
            "starting defect inventory",
        )
        for head, snap in snaps.items():
            if key not in snap["present"]:
                continue
            cursor = head
            while cursor is not None and cursor != introduced:
                cursor = snaps[cursor]["parent"]
            require(cursor == introduced, "defect present before or outside its introduction")
        require(key in snaps[introduced]["present"], "defect missing at its introduction")
    if case["clean_negative"]:
        require(not snaps[start]["present"] and snaps[start]["oracle"] == "pass", "clean negative")


def starting_inventory(case) -> set:
    """Defects present at the starting candidate, whichever lineage commit introduced them."""
    return set(case["snapshots"][case["start"]]["present"])


def start_evidenced(case) -> bool:
    """The starting candidate's inventory and oracle were evidenced by the cutoff."""
    return source(case["snapshots"][case["start"]]["source"], case)


def published_range(case, reviewed, endpoint) -> set:
    """Commits after the reviewed revision up to a FIX's published endpoint."""
    snaps = case["snapshots"]
    require(
        endpoint in snaps and endpoint != reviewed,
        "FIX successor must descend from its reviewed candidate",
    )
    # check_case has proven every parent chain finite and rooted at the merge base.
    commits, cursor = set(), endpoint
    while cursor is not None and cursor != reviewed:
        commits.add(cursor)
        cursor = snaps[cursor]["parent"]
    require(cursor == reviewed, "FIX successor must descend from its reviewed candidate")
    return commits


def validation(value):
    if value is None:
        return None
    object_fields(value, "scope set policy result")
    require(value["scope"] == "full", "only FULL validation is evidence")
    require(isinstance(value["set"], list), "validation set")
    for definition in value["set"]:
        sha(definition, 64)
    sha(value["policy"], 64)
    require(
        value["result"] in ("pass", "fail", None) if value["set"] else value["result"] == "empty",
        "validation result",
    )
    return tuple(value["set"]), value["policy"]


def findings(case, run, row):
    """Return known items even if the independent label or full list is missing."""
    payload = row["findings"]
    if payload is None:
        return []
    object_fields(payload, "complete items")
    require(
        type(payload["complete"]) is bool and isinstance(payload["items"], list), "finding coverage"
    )
    clean = row["outcome"] in ("clean", "stale_clean")
    require(
        not payload["items"] if clean else payload["items"] or not payload["complete"], "verdict"
    )
    observed, seen = [], set()
    snap = case["snapshots"][row["head"]]
    for item in payload["items"]:
        object_fields(item, "id classification label")
        key = item["id"]
        require(
            isinstance(key, str)
            and re.fullmatch(rf"R{row['round']}-F[1-9][0-9]*", key)
            and key not in seen,
            "finding identity",
        )
        seen.add(key)
        require(
            item["classification"] in ("blocked", "non-blocked", "nit"), "review classification"
        )
        label = item["label"]
        status, defect_id = "unknown", None
        if label is not None:
            object_fields(label, "status defect source")
            status, defect_id = label["status"], label["defect"]
            require(status in ("confirmed", "false", "unknown"), "label verdict")
            eligible = source(label["source"], case, run)
            if status == "confirmed":
                require(defect_id in snap["present"], "confirmed finding off its candidate")
                eligible &= source(case["defects"][defect_id]["source"], case, run)
                eligible &= source(snap["source"], case, run)
            else:
                require(defect_id is None, "only a confirmed finding maps to a defect")
            if not eligible:
                status, defect_id = "unknown", None
        observed.append((key, status, defect_id))
    return observed


def check_binding(case, run, row) -> None:
    require(row["pr"] == run["pr"], "outside one-PR scope")
    require(
        (row["base_ref"], row["merge_base"]) == (case["base_ref"], case["merge_base"]),
        "retarget or different merge base",
    )
    snapshot(case, row["head"], row["tree"])


BINDING = "pr base_ref merge_base head tree"
KNOWN_RECEIPT = ("scope", "set", "policy")
RUN_FIELDS = "case seed arm pr actors creation launches stop exclusion"


def replay(case, run, *, synthetic: bool) -> dict:
    object_fields(run, RUN_FIELDS)
    require(nonempty(run["creation"]), "fresh-run creation receipt")
    require(run["arm"] in ARMS and type(run["seed"]) is int and run["seed"] >= 0, "planned run key")
    require(
        nonempty(run["pr"]) and isinstance(run["actors"], list) and len(run["actors"]) == 3,
        "run identity",
    )
    require(
        all(map(nonempty, run["actors"]))
        and run["actors"][1]
        not in (
            run["actors"][0],
            run["actors"][2],
        ),
        "reviewer must be independent of producer and fixer",
    )
    require(run["exclusion"] is None or run["exclusion"] in EXCLUSIONS, "exclusion reason")
    slots = run["launches"]
    require(isinstance(slots, list), "launch window")
    for snap in case["snapshots"].values():
        source(snap["source"], case, run)
    for defect in case["defects"].values():
        source(defect["source"], case, run)

    expected = "ANALYZE_EXECUTE"
    ledger_complete = True
    latest, completed, head = None, 0, None
    latest_row = None
    start_validation, fixed_policy = None, None
    first_review = True
    starting_accepted = False
    review_details = True
    observations, fixes = [], []
    fix_regressions = set()
    known_phases = Counter()
    known_ms = 0
    time_complete = True
    costs, tokens = [], []
    for ordinal, row in enumerate(slots, 1):
        # Even a reserved ordinal with no record contradicts a proven stop.
        require(
            not (ledger_complete and expected in STOPPED), "launch after a FIX that stops the run"
        )
        if row is None:
            ledger_complete = False
            time_complete = False
            continue
        require(isinstance(row, dict), "launch row")
        phase, outcome = row.get("phase"), row.get("outcome")
        require(phase in OUTCOMES, "outside registered launch phases")
        require(outcome is None or outcome in OUTCOMES[phase], "launch outcome for wrong phase")
        fields = "phase outcome elapsed_ms cost_microusd context_tokens"
        if phase == "REVIEW":
            fields += f" {BINDING} round validation findings comment transition next_head"
        if phase == "FIX":
            fields += f" {BINDING} round source published judgment"
        object_fields(row, fields)
        known_phases[phase] += 1
        elapsed = row["elapsed_ms"]
        require(elapsed is None or (type(elapsed) is int and elapsed >= 0), "launch elapsed time")
        time_complete &= elapsed is not None
        known_ms += elapsed or 0
        for key, values in (("cost_microusd", costs), ("context_tokens", tokens)):
            value = row[key]
            require(
                value is None or (synthetic and type(value) is int and value >= 0),
                "only synthetic resource amounts are supported",
            )
            values.append(value)

        if phase != "ANALYZE_EXECUTE":
            check_binding(case, run, row)
            require(type(row["round"]) is int and row["round"] > 0, "launch round")
        if phase == "REVIEW":
            policy = validation(row["validation"])
            require(
                fixed_policy is None or policy is None or policy == fixed_policy,
                "changed validation set or policy",
            )
            if policy is not None:
                fixed_policy = policy
            if first_review and ledger_complete:
                # The start stays pinned: a review of a moved revision needs an
                # explicit drift and is never starting evidence (checked below).
                require(
                    row["head"] == case["start"] or row["transition"] == "drift",
                    "first review candidate",
                )
            first_review = False
            # Any retained receipt bound to the starting candidate states its
            # validation, including an accepted review after a retry that had
            # none. The check above refuses a conflicting receipt; nothing
            # fills an absent one from a later candidate or the certificate.
            if row["head"] == case["start"] and policy is not None:
                start_validation = policy
            if outcome in ("clean", "stale_clean") and row["validation"] is not None:
                require(row["validation"]["result"] != "fail", "clean reply over failed validation")
            if outcome in (None, "retry"):
                require(
                    row["findings"] is None and row["comment"] is None, "uncompleted review payload"
                )
            else:
                require(nonempty(row["comment"]), "completed review comment identity")
                items = findings(case, run, row)
            require(
                row["transition"] in (None, "drift")
                and (row["next_head"] is not None)
                == (outcome in ("stale_clean", "stale_needs_fix")),
                "review transition",
            )
            if row["next_head"] is not None:
                require(
                    row["next_head"] in case["snapshots"]
                    and row["next_head"] not in (row["head"], case["merge_base"]),
                    "stale review requires a different observed candidate",
                )
        if phase == "FIX":
            require(
                type(row["source"]) is int and 0 < row["source"] < ordinal, "FIX source ordinal"
            )
            origin = slots[row["source"] - 1]
            if origin is not None:
                require(
                    origin["phase"] == "REVIEW"
                    and origin["outcome"] in ("needs_fix", None)
                    and (origin["round"], origin["head"]) == (row["round"], row["head"]),
                    "FIX requires an accepted needs-fix review of its revision",
                )
            new = row["published"]
            require((new is not None) == (outcome in ("published", "partial")), "FIX publication")
            # A FIX may publish several commits; each keeps its own place in
            # the observed graph between the reviewed revision and the endpoint.
            commits = published_range(case, row["head"], new) if new is not None else set()
            judgment = row["judgment"]
            if judgment is not None:
                require(new is not None, "FIX judgment without publication")
                object_fields(judgment, "status source")
                require(judgment["status"] in ("safe", "harmful", "unknown"), "FIX judgment")
                eligible = source(judgment["source"], case, run)
            if ledger_complete and new is not None:
                endpoint = case["snapshots"][new]
                # An eligible defect still present at the endpoint and introduced
                # by any commit of the published range is this FIX's regression.
                regressions = {
                    key
                    for key in endpoint["present"]
                    if source(endpoint["source"], case, run)
                    and case["defects"][key]["introduced"] in commits
                    and source(case["defects"][key]["source"], case, run)
                }
                fix_regressions.update(regressions)
                fixes.append(
                    True
                    if regressions
                    else (
                        None
                        if judgment is None or not eligible or judgment["status"] == "unknown"
                        else judgment["status"] == "harmful"
                    )
                )

        # Never invent transitions across a missing ordinal or disposition. Local
        # shape/binding checks above still apply to the suffix.
        if ledger_complete:
            if phase == "REVIEW" and row["transition"] == "drift":
                require(
                    expected in ("FIX", "CLEAN", "REVIEW") and row["head"] != head,
                    "drift requires an intervening revision change",
                )
                head, expected = row["head"], "REVIEW"
            require(phase == expected, "unexpected launch phase")
            if phase == "ANALYZE_EXECUTE" and outcome == "published":
                head, expected = case["start"], "REVIEW"
            if phase == "REVIEW":
                require(
                    row["round"] == completed + 1 and row["head"] == head,
                    "next review round/revision",
                )
                if outcome not in (None, "retry"):
                    latest, latest_row, completed = ordinal, row, row["round"]
                    if outcome.startswith("stale_"):
                        # A stale round consumes its number and its retained
                        # payload was checked above, but it is never scored, so
                        # its absence leaves accepted finding coverage intact.
                        head = row["next_head"]
                    else:
                        review_details &= (
                            row["findings"] is not None and row["findings"]["complete"]
                        )
                        starting_accepted |= head == case["start"]
                        observations.extend(items)
                        expected = "FIX" if outcome == "needs_fix" else "CLEAN"
            if phase == "FIX":
                require(
                    (row["source"], row["round"], row["head"]) == (latest, completed, head),
                    "FIX is not of the current review",
                )
                if outcome in ("no_change", "published"):
                    head, expected = row["published"] or head, "REVIEW"
                elif outcome == "partial":
                    # The controller publishes a partial fix, then stops BLOCKED
                    # until an explicit unblock: the end of this first-stop slice.
                    head, expected = row["published"], "PARTIAL"
                elif outcome == "held":
                    expected = "HELD"
            ledger_complete &= outcome is not None

    stop = run["stop"]
    bound, certified = False, None
    if stop is not None:
        object_fields(stop, f"{BINDING} through phase round review issuance proof wall_ms source")
        require(
            type(stop["through"]) is int and stop["through"] >= len(slots), "stop launch watermark"
        )
        require(stop["phase"] in ("READY_FOR_MERGE", "BLOCKED", "FAILED"), "stop phase")
        require(stop["issuance"] in ("issued", "not_issued", None), "stop issuance")
        require(
            (
                stop["issuance"] is None
                or (stop["phase"] == "READY_FOR_MERGE") == (stop["issuance"] == "issued")
            )
            and (stop["proof"] is not None) == (stop["issuance"] == "issued"),
            "stop certificate shape",
        )
        require(type(stop["round"]) is int and stop["round"] >= 0, "stop round")
        ordinal = stop["review"]
        require(
            ordinal is None or (type(ordinal) is int and 0 < ordinal <= stop["through"]),
            "stop review ordinal",
        )
        wall = stop["wall_ms"]
        require(wall is None or (type(wall) is int and wall >= known_ms), "wall time")
        require(nonempty(stop["source"]), "controller stop provenance")
        if stop["head"] is not None:
            check_binding(case, run, stop)
        else:
            require(all(stop[field] is None for field in BINDING.split()), "pre-PR stop binding")
        if stop["issuance"] == "issued":
            proof = stop["proof"]
            object_fields(proof, "validation comment receipt")
            validation(proof["validation"])
            require(nonempty(proof["receipt"]), "certificate receipt identity")
            require(
                proof["validation"] is not None
                and proof["validation"]["result"] in ("pass", "empty"),
                "certificate needs presented passing or empty FULL validation",
            )
        if ledger_complete and expected in STOPPED:
            require(stop["through"] == len(slots), "launch after a FIX that stops the run")
            require(
                expected != "PARTIAL" or stop["phase"] == "BLOCKED",
                "a published partial FIX stops BLOCKED",
            )
        if ledger_complete and stop["through"] == len(slots):
            require(
                (stop["review"], stop["round"], stop["head"]) == (latest, completed, head),
                "stop disagrees with completed round or live revision",
            )
            bound = stop["issuance"] is not None
            if stop["issuance"] == "issued":
                require(
                    expected == "CLEAN" and latest == len(slots) and latest_row is not None,
                    "certificate does not belong to the terminal clean review",
                )
                require(proof["comment"] == latest_row["comment"], "certificate comment binding")
                presented = latest_row["validation"]
                # Known fields must agree. An unknown presented result leaves the
                # certificate unproven; it never borrows the certificate's result.
                require(
                    presented is None
                    or (
                        all(presented[key] == proof["validation"][key] for key in KNOWN_RECEIPT)
                        and presented["result"] in (None, proof["validation"]["result"])
                    ),
                    "certificate validation binding",
                )
                bound = presented is not None and presented["result"] is not None
                certified = bound or None
            elif bound:
                certified = False
    if run["exclusion"] == "skipped":
        # A skip is documented by a pre-PR stop before ordinal one; any retained
        # or reserved launch, or a later watermark, shows the run executed.
        require(
            not slots
            and stop is not None
            and stop["through"] == 0
            and stop["head"] is None
            and stop["phase"] != "READY_FOR_MERGE",
            "skipped exclusion needs a documented pre-launch stop",
        )

    launch_complete = (
        stop is not None and stop["through"] == len(slots) and all(row is not None for row in slots)
    )
    complete = bound and ledger_complete and review_details
    snap = case["snapshots"].get(head) if bound else None
    final_inventory = snap is not None and source(snap["source"], case, run)
    eligible = {
        key for key, defect in case["defects"].items() if source(defect["source"], case, run)
    }
    critical = sorted(key for key in eligible if case["defects"][key]["severity"] == "critical")
    escapes = {
        key: False
        if certified is False
        else key in snap["present"]
        if certified and final_inventory
        else None
        for key in critical
    }
    success = None
    if certified is False:
        success = False
    elif certified:
        if stop["proof"]["validation"]["result"] == "empty":
            success = False
        elif final_inventory:
            major = any(
                case["defects"][key]["severity"] in ("critical", "high")
                for key in eligible.intersection(snap["present"])
            )
            oracle = snap["oracle"]
            success = False if major or oracle == "fail" else True if oracle == "pass" else None

    counts = Counter(status for _, status, _ in observations)
    detected = {key for _, status, key in observations if status == "confirmed"}
    exposed = eligible & starting_inventory(case)
    weight = sum(WEIGHT[case["defects"][key]["severity"]] for key in exposed)
    detected_weight = sum(WEIGHT[case["defects"][key]["severity"]] for key in exposed & detected)
    # Ignoring candidate-specific eligibility only widens this outer bound.
    detail = complete and starting_accepted
    unknown = counts["unknown"]
    remaining_weights = sorted(
        (WEIGHT[case["defects"][key]["severity"]] for key in exposed - detected), reverse=True
    )
    alarm = True if counts["false"] else False if detail and not unknown else None
    negative = case["clean_negative"] and start_evidenced(case)
    return {
        "exclusion": run["exclusion"],
        "launch_window_complete": launch_complete,
        "lifecycle_complete": launch_complete and ledger_complete,
        "review_record_complete": complete,
        "starting_review_accepted": starting_accepted,
        "starting_validation": start_validation,
        "stop": stop["phase"] if stop else None,
        "terminal_bound": bound,
        "certificate_issued": certified,
        "validated_success": success,
        "critical_escapes": escapes,
        "fix_regressions": sorted(fix_regressions),
        "known_findings": {key: status for key, status, _ in observations},
        "known_harmful_fixes": fixes.count(True),
        "clean_negative_false_alarm": alarm if negative else None,
        "launches_observed": dict(known_phases),
        "fix_launches": known_phases["FIX"] if launch_complete else None,
        "completed_rounds": completed if launch_complete and ledger_complete else None,
        "known_launch_ms": known_ms,
        "launch_ms": known_ms if launch_complete and time_complete else None,
        # The stop observation captures creation-to-stop time on its own clock,
        # so a missing launch leaves it known; total launch duration is not.
        "wall_ms": stop["wall_ms"] if stop else None,
        "cost_microusd": total_usage(costs, launch_complete, synthetic),
        "context_tokens": total_usage(tokens, launch_complete, synthetic),
        "counts": {
            # Tuples are (positive known, unsettled, total eligible).
            "recall": (detected_weight, sum(remaining_weights[:unknown]), weight)
            if detail and start_evidenced(case)
            else None,
            "precision": (counts["confirmed"], unknown, len(observations)) if detail else None,
            "false_positive_rate": (int(alarm is True), int(alarm is None), 1)
            if negative
            else (0, 0, 0),
            "harmful_fix_rate": (fixes.count(True), fixes.count(None), len(fixes))
            if launch_complete and ledger_complete
            else None,
            "duplicate_ratio": (
                counts["confirmed"] - len(detected),
                unknown,
                counts["confirmed"] + unknown,
            )
            if detail
            else None,
        },
    }


def total_usage(values, complete, synthetic):
    return (
        sum(values)
        if synthetic and complete and all(value is not None for value in values)
        else None
    )


METRICS = ("recall", "precision", "false_positive_rate", "harmful_fix_rate", "duplicate_ratio")
FINDING_METRICS = set(METRICS) - {"harmful_fix_rate"}
MARGINS = {
    "recall": Fraction(1, 10),
    "precision": Fraction(-1, 20),
    "false_positive_rate": Fraction(1, 20),
    "harmful_fix_rate": Fraction(1, 20),
    "duplicate_ratio": Fraction(1, 10),
}
BOOTSTRAP_DRAWS = 10_000
LABEL_BAND = Fraction(1, 10)


def rate(counts, *, recall=False):
    if not counts or any(part is None for part in counts):
        return None, None, None
    yes, unknown, total = map(sum, zip(*counts, strict=True))
    denominator = total if recall else total - unknown
    return (
        Fraction(yes, denominator) if denominator else None,
        Fraction(yes, total) if total else None,
        Fraction(yes + unknown, total) if total else None,
    )


def cost_per_success(runs):
    if not runs or any(r["cost_microusd"] is None or r["validated_success"] is None for r in runs):
        return None
    successes = sum(r["validated_success"] for r in runs)
    return (
        Fraction(sum(r["cost_microusd"] for r in runs), successes * 1_000_000)
        if successes
        else None
    )


def encoded(value):
    return str(value) if value is not None else None


@lru_cache(maxsize=128)
def bootstrap(groups, *, recall):
    """Resample entire cases, retaining all their paired seeds together."""
    rng = Random(355)
    points, lows, highs = [], [], []
    for _ in range(BOOTSTRAP_DRAWS if len(groups) > 1 else int(bool(groups))):
        selected = [groups[rng.randrange(len(groups))] for _ in groups]
        b, t = (rate([group[i] for group in selected], recall=recall) for i in range(2))
        if b[0] is not None and t[0] is not None:
            points.append(t[0] - b[0])
        if b[1] is not None and t[1] is not None:
            lows.append(t[1] - b[2])
            highs.append(t[2] - b[1])

    def percentile(values, rank):
        if not values:
            return None
        index = (len(values) * rank + 39) // 40 - 1
        return sorted(values)[index]

    draws = BOOTSTRAP_DRAWS if groups else 0
    multiplier = BOOTSTRAP_DRAWS if len(groups) == 1 else 1
    return (
        (percentile(points, 1), percentile(points, 39)),
        (percentile(lows, 1), percentile(highs, 39)),
        draws - len(points) * multiplier,
        draws - len(lows) * multiplier,
    )


def pool(parts):
    return (
        tuple(map(sum, zip(*parts, strict=True)))
        if all(part is not None for part in parts)
        else None
    )


def score(packet) -> dict:
    """Pool registered, comparable pairs; inspect certified harm independently."""
    object_fields(packet, "version mode cases runs")
    require(type(packet["version"]) is int and packet["version"] == 1, "fixture version")
    require(packet["mode"] in ("synthetic", "recorded"), "evidence provenance mode")
    synthetic = packet["mode"] == "synthetic"
    cases = packet["cases"]
    require(isinstance(cases, dict) and cases, "registered cases")
    planned = {}
    for case_id, case in sorted(cases.items()):
        require(nonempty(case_id), "case identity")
        check_case(case)
        for seed in sorted(case["seeds"]):
            planned[case_id, seed] = {}
    require(isinstance(packet["runs"], list), "run packets")
    used_prs = set()
    for run in packet["runs"]:
        object_fields(run, RUN_FIELDS)
        key, arm = (run["case"], run["seed"]), run["arm"]
        require(key in planned and arm not in planned[key], "unplanned or repeated run")
        report = replay(cases[key[0]], run, synthetic=synthetic)
        require(run["pr"] not in used_prs, "PR reused across runs")
        used_prs.add(run["pr"])
        planned[key][arm] = report

    pairs, matched, gate, unresolved = [], [], [], []
    by_case = {}
    for (case_id, seed), arms in planned.items():
        b, t = (arms.get(arm) for arm in ARMS)
        if t:
            for key, escaped in t["critical_escapes"].items():
                fix_introduced = key in t["fix_regressions"]
                shared = key in starting_inventory(cases[case_id])
                comparable_baseline = (
                    b
                    and not b["exclusion"]
                    and shared
                    and b["starting_validation"] is not None
                    and b["starting_validation"] == t["starting_validation"]
                )
                baseline = b["critical_escapes"].get(key) if comparable_baseline else None
                identity = [case_id, seed, key]
                if escaped is True and (fix_introduced or baseline is False):
                    gate.append(identity)
                elif (escaped is True and baseline is None) or (
                    escaped is None and not t["exclusion"]
                ):
                    unresolved.append(identity)
        if any(run["exclusion"] for run in arms.values()):
            status = "excluded"
        elif not b or not t:
            status = "missing"
        elif b["starting_validation"] is None or t["starting_validation"] is None:
            status = "validation_unknown"
        elif b["starting_validation"] != t["starting_validation"]:
            status = "validation_mismatch"
        else:
            status = "matched"
            matched.append(arms)
            by_case.setdefault(case_id, []).append(arms)
        pairs.append({"case": case_id, "seed": seed, "status": status, "runs": arms})

    paired_rates, rate_harm = {}, []
    for metric in METRICS:
        finding_gap = metric in FINDING_METRICS and any(
            not (row["review_record_complete"] and row["starting_review_accepted"])
            for pair in matched
            for row in pair.values()
        )
        groups = (
            ()
            if finding_gap
            else tuple(
                tuple(pool([pair[arm]["counts"][metric] for pair in seeds]) for arm in ARMS)
                for seeds in by_case.values()
            )
        )
        groups = tuple(group for group in groups if group != ((0, 0, 0), (0, 0, 0)))
        by_arm = {
            arm: rate([group[i] for group in groups], recall=metric == "recall")
            for i, arm in enumerate(ARMS)
        }
        bp, bl, bh = by_arm["baseline"]
        tp, tl, th = by_arm["treatment"]
        interval = (tl - bh, th - bl) if None not in (tl, th, bl, bh) else (None, None)
        points, envelope, undefined_points, undefined_bounds = bootstrap(
            groups, recall=metric == "recall"
        )
        usable = (
            None not in (bp, tp, bl, bh, tl, th, *envelope)
            and bh - bl <= LABEL_BAND
            and th - tl <= LABEL_BAND
            and undefined_points == undefined_bounds == 0
        )
        if metric != "recall" and usable:
            if (metric == "precision" and envelope[1] < MARGINS[metric]) or (
                metric != "precision" and envelope[0] > MARGINS[metric]
            ):
                rate_harm.append(metric)
        paired_rates[metric] = {
            **{
                arm: dict(zip(("point", "low", "high"), map(encoded, value), strict=True))
                for arm, value in by_arm.items()
            },
            "difference": encoded(tp - bp) if tp is not None and bp is not None else None,
            "difference_bounds": list(map(encoded, interval)),
            "bootstrap_point_interval": list(map(encoded, points)),
            "bootstrap_label_envelope": list(map(encoded, envelope)),
            "undefined_bootstrap_points": undefined_points,
            "undefined_bootstrap_bounds": undefined_bounds,
            "usable": usable,
        }
    arm_cost = {arm: cost_per_success([pair[arm] for pair in matched]) for arm in ARMS}
    # A case enters the sample floor only with the cutoff-eligible starting
    # evidence its recall or clean-negative FPR observations need.
    sample = Counter(
        "negative" if cases[key]["clean_negative"] else "defect"
        for key, seeds in by_case.items()
        if all(row["starting_review_accepted"] for pair in seeds for row in pair.values())
        and start_evidenced(cases[key])
        and (
            cases[key]["clean_negative"]
            or any(
                source(cases[key]["defects"][defect]["source"], cases[key])
                for defect in starting_inventory(cases[key])
            )
        )
    )
    covered = all(pair["status"] in ("matched", "excluded") for pair in pairs)
    exclusion_fraction = Fraction(
        sum(pair["status"] == "excluded" for pair in pairs),
        len(pairs),
    )
    recall = paired_rates["recall"]
    upper = recall["bootstrap_label_envelope"][1]
    decision = "INCONCLUSIVE"
    if synthetic:
        decision = "NOT_EVALUATED"
    elif gate or (rate_harm and covered):
        decision = "NO_GO"
    elif not all(any(arm in pair["runs"] for pair in pairs) for arm in ARMS):
        decision = "NOT_EVALUATED"
    elif (
        covered
        and exclusion_fraction <= Fraction(1, 10)
        and sample["defect"] >= 20
        and sample["negative"] >= 10
        and recall["usable"]
        and Fraction(upper) < MARGINS["recall"]
    ):
        decision = "NO_GO"
    return {
        "mode": packet["mode"],
        "decision": decision,
        "matched_pairs": len(matched),
        "sample": {key: sample[key] for key in ("defect", "negative")},
        "exclusion_fraction": encoded(exclusion_fraction),
        "rate_harm": rate_harm,
        "pair_status": dict(Counter(pair["status"] for pair in pairs)),
        "pairs": pairs,
        "new_critical_escapes": gate,
        "unresolved_critical_escapes": unresolved,
        "critical_escape_totals": {
            arm: {
                "known": sum(
                    value is True
                    for pair in matched
                    for value in pair[arm]["critical_escapes"].values()
                ),
                "unresolved": sum(
                    value is None
                    for pair in matched
                    for value in pair[arm]["critical_escapes"].values()
                ),
            }
            for arm in ARMS
        },
        "rates": paired_rates,
        "cost_usd_per_validated_success": {arm: encoded(value) for arm, value in arm_cost.items()},
    }


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate JSON key")
        value[key] = item
    return value


def _read_packet(path: Path) -> dict:
    # Never follow a final symlink or wait on a FIFO supplied as a packet.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), "regular packet")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            body = stream.read(MAX_PACKET_BYTES + 1)
    finally:
        os.close(fd)
    require(len(body) <= MAX_PACKET_BYTES, "packet size")
    return json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path, nargs="?", default=FIXTURE)
    args = parser.parse_args()
    try:
        report = score(_read_packet(args.packet))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError):
        # Packet content is evidence, never an error message to echo.
        parser.exit(2, "Invalid review evidence packet.\n")
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
