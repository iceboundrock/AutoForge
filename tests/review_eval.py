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
COMPLETED_REVIEW = {"clean", "needs_fix", "stale_clean", "stale_needs_fix"}
# Retained dispositions that end this first-stop slice whatever precedes them,
# each with the stop phases it can enter: an accepted clean round enters
# READY_FOR_MERGE or BLOCKED (drift during the review is recorded as stale
# instead), and a held or published partial FIX blocks until an operator acts.
# No later ordinal, retained or reserved, is admitted after one, and the stop
# enters no other phase, even across a gap in the ledger before it.
TERMINAL = {
    ("REVIEW", "clean"): ("READY_FOR_MERGE", "BLOCKED"),
    ("FIX", "held"): ("BLOCKED",),
    ("FIX", "partial"): ("BLOCKED",),
}
FIRST_STOP = "launch after the first stop"
FIRST_STOP_PHASE = "stop phase its first stop cannot enter"
STOP_BINDING = "stop disagrees with completed round or live revision"
# Round r is reviewed after the producer's launch and each earlier round's
# review, each at an ordinal of its own, so it needs an ordinal above r.
ROUND_ORDINAL = "round its ordinal cannot hold"
# Issuance is recorded as observed, apart from where the run stopped. The write
# completing a clean round issues the certificate and enters READY_FOR_MERGE,
# or BLOCKED when the validation configuration in force differs from the one
# presented (ADR 0006 D12.6); READY_FOR_MERGE is never entered without one
# (D12.3). An unknown issuance constrains nothing.
STOP_ISSUANCE = {
    "issued": ("READY_FOR_MERGE", "BLOCKED"),
    "not_issued": ("BLOCKED", "FAILED"),
    None: ("READY_FOR_MERGE", "BLOCKED", "FAILED"),
}


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


def candidate(case, graph, head) -> bool:
    """A revision this PR can bind: registered, above and descending from the merge base."""
    base = case["merge_base"]
    return head in case["snapshots"] and head != base and base in graph[head]


def snapshot(case, graph, head, tree):
    snap = case["snapshots"].get(head)
    require(
        snap is not None and snap["tree"] == tree and candidate(case, graph, head),
        "exact candidate",
    )
    return snap


def ancestry(case) -> dict:
    """Each registered commit's ancestors through every parent, itself included.

    Merge commits keep all their parents, so a side-branch commit keeps its
    own place in the graph. A side branch forked below the merge base keeps
    its real parent, an attested base ancestor: the merge base's history
    holds every one, so a range ending at a revision above it excludes them,
    as ``git rev-list`` does. A cycle is refused, never walked forever.
    """
    snaps, base = case["snapshots"], case["merge_base"]
    below = frozenset(case["base_ancestors"])
    ancestors = {commit: frozenset({commit}) for commit in below}
    active = set()
    for root in snaps:
        stack = [(root, False)]
        while stack:
            head, finished = stack.pop()
            if head in ancestors:
                continue
            parents = snaps[head]["parents"]
            if finished:
                active.discard(head)
                history = below if head == base else ()
                ancestors[head] = frozenset({head}).union(history, *(ancestors[p] for p in parents))
                continue
            active.add(head)
            stack.append((head, True))
            for parent in parents:
                # A parent still being expanded lies on this walk's own path.
                require(parent not in active, "snapshot ancestry")
                if parent not in ancestors:
                    stack.append((parent, False))
    return ancestors


def check_case(case) -> dict:
    object_fields(
        case,
        "base_ref merge_base base_ancestors start cutoff clean_negative seeds defects snapshots",
    )
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
    # Commits attested, like the rest of the graph, to lie in the merge base's
    # own history: the boundary a side branch forked below it reaches. They
    # are no snapshot, candidate or introduction, only a parent's real name.
    below = case["base_ancestors"]
    require(isinstance(below, list) and len(set(below)) == len(below), "base ancestors")
    for commit in below:
        sha(commit)
        require(commit not in snaps, "base ancestors")
    for head, snap in snaps.items():
        sha(head)
        object_fields(snap, "parents tree present oracle source")
        sha(snap["tree"])
        parents = snap["parents"]
        # The merge base is the only snapshot without parents; any other, a
        # merge included, lists each of its parents once, each registered or
        # a base ancestor.
        require(
            isinstance(parents, list)
            and len(set(parents)) == len(parents)
            and (
                not parents
                if head == base
                else parents and set(parents) <= snaps.keys() | set(below)
            ),
            "snapshot parent",
        )
        require(snap["oracle"] in ("pass", "fail", None), "independent oracle outcome")
        require(
            isinstance(snap["present"], list)
            and len(set(snap["present"])) == len(snap["present"])
            and set(snap["present"]) <= case["defects"].keys(),
            "snapshot defect inventory",
        )
        source(snap["source"], case)
    graph = ancestry(case)
    # Every snapshot descends from the merge base, or is a side-branch commit
    # in the history of one that does; the start is a bindable candidate.
    above = [head for head in snaps if base in graph[head]]
    require(
        candidate(case, graph, start) and snaps.keys() <= set().union(*(graph[h] for h in above)),
        "snapshots must lie in the history of the registered merge base's descendants",
    )
    require(not snaps[base]["present"], "registered defects must be absent from the merge base")
    # The starting tip and every registered ancestor outside the merge base's
    # history, through each parent of a merge.
    lineage = graph[start] - graph[base]
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
        carriers = (head for head, snap in snaps.items() if key in snap["present"])
        require(
            all(introduced in graph[head] for head in carriers),
            "defect present before or outside its introduction",
        )
        require(key in snaps[introduced]["present"], "defect missing at its introduction")
    if case["clean_negative"]:
        require(not snaps[start]["present"] and snaps[start]["oracle"] == "pass", "clean negative")
    return graph


def starting_inventory(case) -> set:
    """Defects present at the starting candidate, whichever lineage commit introduced them."""
    return set(case["snapshots"][case["start"]]["present"])


def start_evidenced(case) -> bool:
    """The starting candidate's inventory and oracle were evidenced by the cutoff."""
    return source(case["snapshots"][case["start"]]["source"], case)


def exposure(case) -> set:
    """Eligible starting defects: the case's recall population, fixed at registration.

    A starting snapshot evidenced after the cutoff makes none eligible, so the
    case has no recall observation whatever its reviews retained.
    """
    if not start_evidenced(case):
        return set()
    return {key for key in starting_inventory(case) if source(case["defects"][key]["source"], case)}


def published_range(case, graph, reviewed, endpoint) -> set:
    """Commits a FIX published: reachable from its endpoint but not from its reviewed revision.

    This is ``git rev-list reviewed..endpoint`` over the registered graph, so a
    side branch merged by the FIX belongs to it, every parent walked, and its
    walk ends in the reviewed revision's history, the merge base's included.
    """
    snaps = case["snapshots"]
    require(endpoint in snaps, "FIX successor must descend from its reviewed candidate")
    require(
        endpoint != reviewed and reviewed in graph[endpoint],
        "FIX successor must descend from its reviewed candidate",
    )
    return set(graph[endpoint] - graph[reviewed])


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


def check_binding(case, graph, run, row) -> None:
    require(row["pr"] == run["pr"], "outside one-PR scope")
    require(
        (row["base_ref"], row["merge_base"]) == (case["base_ref"], case["merge_base"]),
        "retarget or different merge base",
    )
    snapshot(case, graph, row["head"], row["tree"])


def revision_after(row):
    """The revision a retained launch leaves current, when its disposition says which."""
    if row["phase"] == "ANALYZE_EXECUTE" or row["outcome"] is None:
        return None
    if row["phase"] == "REVIEW":
        return row["next_head"] or row["head"]
    return row["published"] or row["head"]


def check_stop_against_launches(slots, stop) -> None:
    """Refuse a stop that a retained launch directly contradicts, gap or not.

    The stop names the last completed round and its review ordinal. Each
    retained launch observes how far the run had come, so it is checked
    against that naming on its own, whatever is missing around it: no round
    completes after the named one, a REVIEW launch before it is of at most
    its round and one after it of the next, and a FIX follows the named round
    or precedes it as a FIX of an earlier one. The launch at the watermark,
    when retained with a known disposition, leaves the stop's revision. A
    pre-PR stop binds no PR, so no retained launch published or bound one.
    """
    named, last = stop["review"], stop["round"]
    for position, row in enumerate(slots, 1):
        if row is not None and stop["head"] is None:
            require(
                row["phase"] == "ANALYZE_EXECUTE" and row["outcome"] != "published", STOP_BINDING
            )
            continue
        if row is None or position == named:
            continue
        after = named is None or position > named
        if row["phase"] == "REVIEW":
            if row["outcome"] in COMPLETED_REVIEW:
                consistent = not after and row["round"] < last
            else:
                consistent = row["round"] == last + 1 if after else row["round"] <= last
        elif row["phase"] == "FIX":
            consistent = named is not None and (
                (row["source"] == named and row["round"] == last)
                or (row["source"] < named and row["round"] < last and not after)
            )
        else:
            continue
        require(consistent, STOP_BINDING)
    if slots and stop["through"] == len(slots) and slots[-1] is not None:
        live = revision_after(slots[-1])
        require(live is None or stop["head"] == live, STOP_BINDING)


BINDING = "pr base_ref merge_base head tree"
KNOWN_RECEIPT = ("scope", "set", "policy")
RUN_FIELDS = "case seed arm pr actors creation launches stop exclusion"


def replay(case, graph, run, *, synthetic: bool) -> dict:
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
    terminal = None
    for ordinal, row in enumerate(slots, 1):
        # Even a reserved ordinal with no record contradicts a retained first
        # stop, and an unrelated gap before that stop does not excuse it.
        require(terminal is None, FIRST_STOP)
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
            check_binding(case, graph, run, row)
            require(type(row["round"]) is int and row["round"] > 0, "launch round")
        if phase == "REVIEW":
            require(row["round"] < ordinal, ROUND_ORDINAL)
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
                    candidate(case, graph, row["next_head"]) and row["next_head"] != row["head"],
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
            # Its source review, of the same round, holds an ordinal of its own.
            require(row["source"] > row["round"], ROUND_ORDINAL)
            # Only retries of this FIX lie between its source review and it: a
            # retained review or a completed FIX in between supersedes the
            # source, whatever else is missing.
            require(
                all(
                    between is None
                    or (
                        between["phase"] == "FIX"
                        and between["source"] == row["source"]
                        and between["outcome"] in ("retry", None)
                    )
                    for between in slots[row["source"] : ordinal - 1]
                ),
                "FIX is not of the current review",
            )
            new = row["published"]
            require((new is not None) == (outcome in ("published", "partial")), "FIX publication")
            # A FIX may publish several commits; each keeps its own place in
            # the observed graph between the reviewed revision and the endpoint.
            commits = published_range(case, graph, row["head"], new) if new is not None else set()
            judgment = row["judgment"]
            if judgment is not None:
                require(new is not None, "FIX judgment without publication")
                object_fields(judgment, "status source")
                require(judgment["status"] in ("safe", "harmful", "unknown"), "FIX judgment")
                eligible = source(judgment["source"], case, run)

        # Never invent transitions across a missing ordinal or disposition. Local
        # shape/binding checks above still apply to the suffix.
        if ledger_complete:
            if phase == "REVIEW" and row["transition"] == "drift":
                # An accepted clean round is a first stop (TERMINAL), so no
                # drift follows one in this slice.
                require(
                    expected in ("FIX", "REVIEW") and row["head"] != head,
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
                    latest, completed = ordinal, row["round"]
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
                if new is not None:
                    endpoint = case["snapshots"][new]
                    # An eligible defect still present at the endpoint and
                    # introduced by any commit of the published range is this
                    # FIX's regression. Only a complete ledger reports it.
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
                if outcome in ("no_change", "published"):
                    head, expected = row["published"] or head, "REVIEW"
                elif outcome == "partial":
                    # The controller publishes a partial fix, then stops BLOCKED
                    # until an explicit unblock: the end of this first-stop slice.
                    head = row["published"]
            ledger_complete &= outcome is not None
        if (phase, outcome) in TERMINAL:
            terminal = ordinal

    stop = run["stop"]
    reconciled, bound, certified = False, False, None
    proven = False
    if stop is not None:
        object_fields(stop, f"{BINDING} through phase round review issuance proof wall_ms source")
        require(
            type(stop["through"]) is int and stop["through"] >= len(slots), "stop launch watermark"
        )
        require(stop["phase"] in ("READY_FOR_MERGE", "BLOCKED", "FAILED"), "stop phase")
        require(stop["issuance"] in ("issued", "not_issued", None), "stop issuance")
        issued = stop["issuance"] == "issued"
        # Only the write completing a clean round enters READY_FOR_MERGE (D12.3),
        # so that phase binds the clean round even when issuance is unknown.
        clean_stop = issued or stop["phase"] == "READY_FOR_MERGE"
        require(
            stop["phase"] in STOP_ISSUANCE[stop["issuance"]]
            and (stop["proof"] is not None) == issued,
            "stop certificate shape",
        )
        require(type(stop["round"]) is int and stop["round"] >= 0, "stop round")
        ordinal = stop["review"]
        require(
            (ordinal is None) == (stop["round"] == 0)
            and (ordinal is None or (type(ordinal) is int and 0 < ordinal <= stop["through"])),
            "stop review ordinal",
        )
        require(ordinal is None or stop["round"] < ordinal, ROUND_ORDINAL)
        wall = stop["wall_ms"]
        require(wall is None or (type(wall) is int and wall >= known_ms), "wall time")
        require(nonempty(stop["source"]), "controller stop provenance")
        if stop["head"] is not None:
            check_binding(case, graph, run, stop)
        else:
            # Before a PR exists no round has completed.
            require(
                all(stop[field] is None for field in BINDING.split()) and stop["round"] == 0,
                "pre-PR stop binding",
            )
        if issued:
            proof = stop["proof"]
            object_fields(proof, "validation comment receipt")
            # The certificate's own identities are checked on their own, whether
            # or not the review it names is retained to compare them with.
            require(nonempty(proof["comment"]), "certificate comment identity")
            # The certificate presents the run's one validation set and policy,
            # which every retained receipt fixes, whichever review it names.
            certified_policy = validation(proof["validation"])
            require(
                fixed_policy is None or certified_policy in (None, fixed_policy),
                "certificate validation binding",
            )
            require(nonempty(proof["receipt"]), "certificate receipt identity")
            require(
                proof["validation"] is not None
                and proof["validation"]["result"] in ("pass", "empty"),
                "certificate needs presented passing or empty FULL validation",
            )
        if clean_stop:
            # The certificate is written when its clean round completes, which
            # ends the slice: no later ordinal was reserved.
            require(
                ordinal is not None and ordinal == stop["through"],
                "certificate does not belong to the terminal clean review",
            )
        # The review the stop names is an explicit binding: a retained record of
        # it is cross-checked even when other ordinals are missing. A gap still
        # leaves the terminal outcome unproven below.
        named = slots[ordinal - 1] if ordinal is not None and ordinal <= len(slots) else None
        if named is not None:
            require(
                named["phase"] == "REVIEW"
                and named["outcome"] != "retry"
                and named["round"] == stop["round"],
                STOP_BINDING,
            )
            if clean_stop:
                require(
                    named["outcome"] in ("clean", None),
                    "certificate does not belong to the terminal clean review",
                )
                require(
                    (named["head"], named["tree"]) == (stop["head"], stop["tree"]),
                    STOP_BINDING,
                )
                # A certificate needs presented passing or empty validation, so a
                # known failure refutes the clean round a READY_FOR_MERGE stop or
                # an issuance names, even when its disposition or the issuance is
                # unknown.
                require(
                    named["validation"] is None or named["validation"]["result"] != "fail",
                    "certificate needs presented passing or empty FULL validation",
                )
            if issued:
                require(
                    named["comment"] is None or proof["comment"] == named["comment"],
                    "certificate comment binding",
                )
                presented = named["validation"]
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
                # The certificate presented exactly the stop's candidate when the
                # exact terminal clean review it names is retained at the
                # watermark with its disposition and a known presented result;
                # its comment, revision and known receipt fields were checked
                # above. These records need no other ordinal and no transition
                # between them, so they hold across a gap elsewhere: there they
                # establish a critical escape for the safety report alone (see
                # below), while issuance as the terminal outcome stays unproven.
                proven = (
                    named["outcome"] == "clean"
                    and presented is not None
                    and presented["result"] is not None
                )
        # Every other retained launch is checked against the stop the same way.
        check_stop_against_launches(slots, stop)
        if terminal is not None:
            # A retained first stop binds the stop whatever precedes it: its
            # watermark, the phase its disposition enters, its round, review
            # ordinal and revision.
            row = slots[terminal - 1]
            require(stop["through"] == terminal, FIRST_STOP)
            if row["phase"] == "FIX":
                require(not clean_stop, "certificate does not belong to the terminal clean review")
                last = (row["source"], row["round"], row["published"] or row["head"])
            else:
                last = (terminal, row["round"], row["head"])
            require(stop["phase"] in TERMINAL[row["phase"], row["outcome"]], FIRST_STOP_PHASE)
            require((stop["review"], stop["round"], stop["head"]) == last, STOP_BINDING)
        if ledger_complete and stop["through"] == len(slots):
            # The stop's launch window and the ledger reconcile: what finding
            # coverage needs, whether or not issuance is proven.
            reconciled = True
            require(
                (stop["review"], stop["round"], stop["head"]) == (latest, completed, head),
                STOP_BINDING,
            )
            require(
                not clean_stop or (expected == "CLEAN" and latest == len(slots)),
                "certificate does not belong to the terminal clean review",
            )
            if issued:
                # The named review is the terminal clean one, cross-checked above,
                # so the reconciled ledger binds the certificate it proves.
                bound = proven
                certified = bound or None
            elif stop["issuance"] == "not_issued":
                bound, certified = True, False
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
    # A complete ledger is a precondition for scoring: every ordinal up to the
    # stop's watermark retains its launch and disposition, and the stop is
    # retained. A gap leaves the run unresolved, contributing to no metric and
    # reporting no finding, false alarm or harmful FIX, even where part of the
    # evidence for one is retained: the replay does not reason across a gap.
    missing = missing_ranges(slots, stop["through"] if stop is not None else len(slots))
    resolved = stop is not None and not missing
    # Finding coverage needs the stop's launch window reconciled with the
    # ledger and every accepted payload complete; proof of issuance is a
    # separate question and an unknown certificate leaves coverage intact.
    complete = reconciled and review_details
    snap = case["snapshots"].get(head) if bound else None
    final_inventory = snap is not None and source(snap["source"], case, run)
    eligible = {
        key for key, defect in case["defects"].items() if source(defect["source"], case, run)
    }
    critical = sorted(key for key in eligible if case["defects"][key]["severity"] == "critical")
    # Critical escapes read the cutoff-eligible inventory of the candidate a
    # proven certificate presented. In a complete ledger that certificate is the
    # bound terminal outcome. Across a gap, the safety report's one exception,
    # the same retained records still show a listed defect escaped without
    # reconstructing any missing transition; the gap only keeps a defect's
    # absence from proving a non-escape, as it keeps the run out of every rate,
    # FIX attribution and validated success.
    certified_snap = case["snapshots"][stop["head"]] if proven else None
    inventory = certified_snap is not None and source(certified_snap["source"], case, run)
    escapes = {
        key: False
        if certified is False
        else True
        if inventory and key in certified_snap["present"]
        else False
        if inventory and resolved
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
        if stop["phase"] != "READY_FOR_MERGE" and success:
            # A certificate issued into BLOCKED (ADR 0006 D12.6) shows the
            # validation configuration in force moved after presentation, which
            # the fixed-policy comparison does not hold: issuance and escapes
            # stand, but a success is unresolved rather than filled either way.
            success = None
    applicable = None if certified is None else certified and stop["phase"] == "READY_FOR_MERGE"

    counts = Counter(status for _, status, _ in observations)
    detected = {key for _, status, key in observations if status == "confirmed"}
    # A starting snapshot evidenced after the cutoff makes no starting defect
    # eligible: the case has zero recall observations, which is not missing
    # coverage, so it drops out of the pool instead of making it undefined.
    exposed = exposure(case)
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
    report = {
        "exclusion": run["exclusion"],
        "ledger": "complete" if resolved else "unresolved: incomplete ledger",
        "missing_ordinals": missing,
        "launch_window_complete": launch_complete,
        "review_record_complete": complete,
        "starting_review_accepted": starting_accepted,
        "starting_validation": start_validation,
        "stop": stop["phase"] if stop else None,
        "terminal_bound": bound,
        "certificate_issued": certified,
        "certificate_applicable": applicable,
        "validated_success": success,
        "critical_escapes": escapes,
        "fix_regressions": sorted(fix_regressions),
        "known_findings": {key: status for key, status, _ in observations},
        "known_harmful_fixes": fixes.count(True),
        "clean_negative_false_alarm": alarm if negative else None,
        "launches_observed": dict(known_phases),
        "fix_launches": known_phases["FIX"] if launch_complete else None,
        "completed_rounds": completed if resolved else None,
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
            if detail or not weight
            else None,
            "precision": (counts["confirmed"], unknown, len(observations)) if detail else None,
            "false_positive_rate": (int(alarm is True), int(alarm is None), 1)
            if negative
            else (0, 0, 0),
            "harmful_fix_rate": (fixes.count(True), fixes.count(None), len(fixes)),
            "duplicate_ratio": (
                counts["confirmed"] - len(detected),
                unknown,
                counts["confirmed"] + unknown,
            )
            if detail
            else None,
        },
    }
    if not resolved:
        # An unresolved run is scored nowhere: only the safety report reads it,
        # through its critical escapes, which a gap leaves unknown, not removed,
        # unless the retained certificate evidence proves one escaped.
        report.update(
            fix_regressions=None,
            known_findings=None,
            known_harmful_fixes=None,
            clean_negative_false_alarm=None,
            counts=dict.fromkeys(report["counts"]),
        )
    return report


def missing_ranges(slots, through) -> list:
    """Inclusive ``[first, last]`` runs of ordinals up to ``through`` without a disposition.

    An ordinal is missing when it has no retained launch record (a ``null``
    slot, or one the watermark reserves past the last slot) or no known
    disposition. The reserved tail is one run, so work and output are bounded
    by the retained slots, never by the watermark's magnitude, which the
    packet's byte cap does not limit.
    """
    ranges = []

    def add(first, last):
        if ranges and ranges[-1][1] == first - 1:
            ranges[-1][1] = last
        else:
            ranges.append([first, last])

    for position, row in enumerate(slots, 1):
        if row is None or row["outcome"] is None:
            add(position, position)
    if through > len(slots):
        add(len(slots) + 1, through)
    return ranges


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


def observes(metric, case) -> bool:
    """Whether a case's registration admits observations of a finding metric.

    Recall observes eligible starting defects and FPR an evidenced clean
    negative, both fixed before any review; precision and duplicates observe
    whatever an accepted review retained, so every case can hold some.
    """
    if metric == "recall":
        return bool(exposure(case))
    if metric == "false_positive_rate":
        return case["clean_negative"] and start_evidenced(case)
    return True


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
    planned, graphs = {}, {}
    for case_id, case in sorted(cases.items()):
        require(nonempty(case_id), "case identity")
        graphs[case_id] = check_case(case)
        for seed in sorted(case["seeds"]):
            planned[case_id, seed] = {}
    require(isinstance(packet["runs"], list), "run packets")
    used_prs = set()
    for run in packet["runs"]:
        object_fields(run, RUN_FIELDS)
        key, arm = (run["case"], run["seed"]), run["arm"]
        require(key in planned and arm not in planned[key], "unplanned or repeated run")
        report = replay(cases[key[0]], graphs[key[0]], run, synthetic=synthetic)
        require(run["pr"] not in used_prs, "PR reused across runs")
        used_prs.add(run["pr"])
        planned[key][arm] = report

    pairs, matched, gate, unresolved = [], [], [], []
    by_case = {}
    for (case_id, seed), arms in planned.items():
        b, t = (arms.get(arm) for arm in ARMS)
        if t:
            # A defect is shared only on cutoff-eligible evidence that it was
            # present at the registered start: the starting snapshot's
            # inventory and its own label, both evidenced by the cutoff. A late
            # starting inventory leaves the escape listed and, without an
            # attributed FIX regression, its comparison unresolved.
            started = exposure(cases[case_id])
            for key, escaped in t["critical_escapes"].items():
                fix_introduced = key in (t["fix_regressions"] or ())
                shared = key in started
                # The counterpart is evidence only from a complete ledger: an
                # escape it proves across a gap stays listed in its own run but
                # never clears or confirms the treatment's.
                comparable_baseline = (
                    b
                    and not b["exclusion"]
                    and b["ledger"] == "complete"
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
            # A documented exclusion precedes every other status, whatever the
            # pair's ledgers, starting validation or missing arm: the pair
            # leaves the rates and the coverage condition of the decision alike,
            # and counts only against the primary rule's exclusion budget. Its
            # runs still reach the safety report above.
            status = "excluded"
        elif not b or not t:
            status = "missing"
        elif b["ledger"] != "complete" or t["ledger"] != "complete":
            # Neither arm of the pair enters a rate, cost, bootstrap draw or
            # sample floor; like a missing or incomparable pair, it keeps the
            # decision from relying on complete coverage.
            status = "incomplete_ledger"
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
        # The population comes first: a case the registration leaves without
        # an observation of this metric neither enters its pool nor, through
        # its own coverage, suppresses the contrast of the cases that do.
        finding_gap = metric in FINDING_METRICS and any(
            not (row["review_record_complete"] and row["starting_review_accepted"])
            for case_id, seeds in by_case.items()
            if observes(metric, cases[case_id])
            for pair in seeds
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
        and observes(
            "false_positive_rate" if cases[key]["clean_negative"] else "recall", cases[key]
        )
    )
    # Ordinary harm and the primary rule rely on complete coverage: no missing,
    # incomparable or incomplete-ledger pair other than an excluded one. An
    # excluded pair is outside the population whatever its ledgers, so a gap in
    # it bears no more on coverage than on a rate.
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
