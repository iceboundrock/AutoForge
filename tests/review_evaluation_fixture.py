"""Build the synthetic study used by the offline review scorecard."""

import json

from tests.review_evidence import commit_id

NOW = "2026-01-01T00:00:00Z"
HUMAN = {
    "role": "independent_human",
    "adjudicator": "Fixture adjudicator",
    "source": "fixture",
    "at": NOW,
}


def snapshot(files, parent=None, resolves=(), **extra):
    return {"sha": commit_id(files, parent), "files": files, "resolves": list(resolves), **extra}


def defect(cid, commit, path, expected="ok\n", severity="critical", *, evidence=HUMAN):
    return {
        "id": cid,
        "introduced": commit,
        "class": "correctness",
        "severity": severity,
        "oracle": {"path": path, "expected": expected},
        "evidence": evidence,
    }


def label(commit, verdict, **extra):
    return {"sha": commit, "verdict": verdict, "evidence": HUMAN, **extra}


def ref(did="D", scope="case"):
    return {"id": did, "scope": scope}


def finding(fid, commit, verdict, **extra):
    return {
        "id": fid,
        "claim": "illustrative reviewer text",
        "label": label(commit, verdict, **extra),
    }


def case(cid, *, clean=False, risk="standard"):
    base = snapshot({"result.txt": "ok\n"})
    candidate = snapshot(
        {"result.txt": "ok\n" if clean else "wrong\n", "case.txt": cid}, base["sha"]
    )
    return {
        "id": cid,
        "kind": "clean_negative" if clean else "defect",
        "risk": risk,
        "snapshots": [base, candidate],
        "defects": [] if clean else [defect("D", candidate["sha"], "result.txt")],
    }


def review(
    case_,
    commit,
    rnd,
    seq,
    *,
    findings=(),
    validation=None,
    certificate=None,
    disposition="accepted",
):
    rev = {
        "round": rnd,
        "sha": commit,
        "pr": "fixture-pr",
        "base": "main",
        "merge_base": case_["snapshots"][0]["sha"],
    }
    row = {
        "revision": rev,
        "reply": "needs_fix" if findings else "clean",
        "disposition": disposition,
        "seq": seq,
        "findings": list(findings),
        "source": "fixture controller review observation",
    }
    if validation is not None:
        row["validation"] = {
            "result": validation,
            "set": f"fixture set {validation}",
            "policy": "fixture policy",
            "revision": rev.copy(),
            "source": "fixture validation",
        }
    if certificate is not None:
        row["certificate"] = {
            "id": certificate,
            "set": f"fixture set {validation}",
            "policy": "fixture policy",
            "revision": rev.copy(),
            "source": "fixture issuance",
        }
    return row


def attempt(seq, phase, rnd=0, outcome="reply", **extra):
    return {
        "seq": seq,
        "phase": phase,
        "round": rnd,
        "outcome": outcome,
        "source": "fixture attempt",
        "usd": 1,
        "seconds": 10,
        "context_tokens": 50,
        **extra,
    }


def stop(review_, *, phase="READY_FOR_MERGE", outcome="certified"):
    row = {
        "phase": phase,
        "outcome": outcome,
        "revision": review_["revision"].copy(),
        "through": review_["seq"],
        "source": "fixture controller stop",
        "reason": "closed fixture observation window",
    }
    if outcome == "certified" and "certificate" in review_:
        row["certificate"] = review_["certificate"]["id"]
    return row


def runner(case_, arm="baseline", **extra):
    rid = f"{case_['id']}-{arm}"
    return {
        "id": rid,
        "case": case_["id"],
        "arm": arm,
        "start": {
            "source": "fixture run creation",
            "run": rid,
            "initial_seq": 0,
        },
        "snapshots": [],
        "defects": [],
        "reviews": [],
        "attempts": [],
        **extra,
    }


def study(cases, runs, *, mode="fixture"):
    return {"schema": 1, "mode": mode, "cutoff": NOW, "cases": cases, "runs": runs}


def fixture():
    repaired = case("repaired", risk="high")
    cand = repaired["snapshots"][-1]
    repaired_files = {**cand["files"], "result.txt": "ok\n"}
    fixed = snapshot(repaired_files, cand["sha"], [ref()], action="fix", seq=3)
    fixed["label"] = label(fixed["sha"], "not_harmful")
    first = review(
        repaired,
        cand["sha"],
        1,
        2,
        findings=[
            finding("F1", cand["sha"], "confirmed", defect=ref()),
            finding("F2", cand["sha"], "confirmed", defect=ref()),
            finding("F3", cand["sha"], "unknown"),
        ],
    )
    final = review(repaired, fixed["sha"], 2, 4, validation="passed", certificate="issued")
    repaired_run = runner(
        repaired,
        snapshots=[fixed],
        reviews=[first, final],
        stop=stop(final),
        attempts=[
            attempt(1, "ANALYZE_EXECUTE"),
            attempt(2, "REVIEW", 1),
            attempt(3, "FIX", 1),
            attempt(4, "REVIEW", 2),
        ],
    )
    regression = snapshot(
        {**repaired_files, "new.txt": "wrong\n"},
        cand["sha"],
        [ref()],
        action="fix",
        seq=4,
    )
    regression["label"] = label(regression["sha"], "harmful")
    first_again = {**first, "seq": 3}
    escaped = review(repaired, regression["sha"], 2, 5, validation="empty", certificate="issued")
    regression_run = runner(
        repaired,
        "treatment",
        snapshots=[regression],
        reviews=[first_again, escaped],
        defects=[defect("D", regression["sha"], "new.txt", expected=None)],
        stop=stop(escaped),
        attempts=[
            attempt(1, "ANALYZE_EXECUTE"),
            attempt(2, "REVIEW", 1, "failed"),
            attempt(3, "REVIEW", 1),
            attempt(4, "FIX", 1),
            attempt(5, "REVIEW", 2),
        ],
    )

    missed = case("missed", risk="high")
    missed_sha = missed["snapshots"][-1]["sha"]
    missed_review = review(missed, missed_sha, 1, 2, validation="passed", certificate="issued")
    missed_run = runner(
        missed,
        reviews=[missed_review],
        stop=stop(missed_review),
        attempts=[attempt(1, "ANALYZE_EXECUTE"), attempt(2, "REVIEW", 1)],
    )

    stopped = case("stagnant")
    stopped_sha = stopped["snapshots"][-1]["sha"]
    blocked = review(
        stopped,
        stopped_sha,
        1,
        2,
        validation="passed",
        findings=[finding("F1", stopped_sha, "confirmed", defect=ref())],
    )
    blocked_again = {**blocked, "revision": {**blocked["revision"], "round": 2}, "seq": 4}
    blocked_again["validation"] = {
        **blocked["validation"],
        "revision": blocked_again["revision"].copy(),
    }
    stopped_run = runner(
        stopped,
        reviews=[blocked, blocked_again],
        stop={**stop(blocked_again, phase="BLOCKED", outcome="not_issued"), "reason": "stagnation"},
        attempts=[
            attempt(1, "ANALYZE_EXECUTE"),
            attempt(2, "REVIEW", 1),
            attempt(3, "FIX", 1),
            attempt(4, "REVIEW", 2),
        ],
    )

    negatives = [case(cid, clean=True, risk="low") for cid in ("quiet", "false_alarm", "unsettled")]
    negative_runs = []
    for index, (c, verdict) in enumerate(zip(negatives, (None, "false", "unknown"), strict=True)):
        commit = c["snapshots"][-1]["sha"]
        findings = [finding("F1", commit, verdict)] if verdict else []
        r = review(
            c,
            commit,
            1,
            2,
            findings=findings,
            validation="passed",
            certificate=None if findings else "issued",
        )
        terminal = stop(r, phase="BLOCKED", outcome="not_issued") if findings else stop(r)
        negative_runs.append(
            runner(
                c,
                "treatment" if index == 2 else "baseline",
                reviews=[r],
                stop=terminal,
                attempts=[attempt(1, "ANALYZE_EXECUTE"), attempt(2, "REVIEW", 1)],
            ),
        )
    return study(
        [repaired, missed, stopped, *negatives],
        [repaired_run, regression_run, missed_run, stopped_run, *negative_runs],
    )


if __name__ == "__main__":
    print(json.dumps(fixture(), indent=2, sort_keys=True))
