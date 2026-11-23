"""Offline judged lexical fixture and launch-size timings."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import resource
import sqlite3
import statistics
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

from autoforge.auto_recovery import StepKey
from autoforge.memory import (
    MAX_FACTS,
    Action,
    Actor,
    EvidenceRef,
    Kind,
    MemoryScope,
    MemoryStore,
    ProjectionRequest,
    ProposalKey,
    Range,
    Receipt,
    admissible_text,
)
from autoforge.memory_retrieval import Checkpoint, IndexState, IndexUnavailable, MemoryRetriever
from autoforge.redaction import redact
from autoforge.transitions import Phase

FIXTURE = Path(__file__).with_name("fixtures") / "memory_retrieval" / "corpus.json"
PIN = "ececa5575e9326972ebfe5d7f3365c8e6c913f7d42623deac250ef7d38a89125"
_MARKER = "\n[omitted]"
_K = 5


def _step(issue):
    return StepKey(Phase.ANALYZE_EXECUTE, issue, "", 0, 1)


def _empty(repository, run, launch):
    return MemoryStore(
        repository,
        run,
        launch,
        {
            "run_id": run,
            "enabled": True,
            "texts": {},
            "events": [],
            "refusals": 0,
        },
    )


def _reload(store):
    return MemoryStore(store.repository, store.run_id, store.launch_seq, store.encode())


def _applied(result):
    if result.status != "applied":
        raise ValueError(f"fixture operation not admitted: {result.status}")
    return result.store


def _read_fixture(raw):
    fixture = json.loads(raw)
    for row in (*fixture["records"], *fixture["operations"]):
        if "text" in row and not admissible_text(row["text"]):
            raise ValueError("fixture must declare exactly the text the store would retain")
    return fixture


def _request(fixture):
    candidates = fixture["candidates"]
    return ProjectionRequest(
        fixture["run_id"],
        _step(fixture["issues"]["main"]),
        MAX_FACTS + 1,
        candidates["current"],
        (candidates["base"],),
    )


def _ledger(fixture):
    """Admit records, saving a snapshot immediately before each type of lag.

    Delay the last add not named by any operation. It lets the append-only lag
    arm exercise the tail without mixing in the revoked-generation policy.
    """
    rows = fixture["records"]
    operations = fixture["operations"]
    touched = {op[key] for op in operations for key in ("key", "by") if key in op}
    revised_text = {r["text"] for r in rows if r["key"] in touched}
    revised_text.update(op["text"] for op in operations if "text" in op)
    delayed = next(
        (
            r
            for r in reversed(rows)
            if r["key"] not in touched
            and r["text"] not in revised_text
            and r["issue"] == "main"
            and r["actor"] == "agent"
        ),
        None,
    )
    ids = {}
    store = _empty(fixture["repository"], fixture["run_id"], 0)
    candidates = fixture["candidates"]

    def add(store, row):
        actor = Actor(row["actor"])
        mode = Range(row.get("range", "any"))
        candidate = candidates[row.get("candidate", "current")]
        step = _step(fixture["issues"][row["issue"]])
        if actor is Actor.OPERATOR:
            step = StepKey(Phase.INITIALIZING, step.issue_url, "", 0, 1)
        scope = MemoryScope(
            step,
            range=mode,
            candidate=candidate if mode is Range.EXACT else "",
            base=candidates["base"] if mode is Range.DESCENDANTS else "",
        )
        attempt = len(ids) + 1
        evidence = (
            EvidenceRef(
                Receipt.VALIDATION, attempt, hashlib.sha256(row["key"].encode()).hexdigest()
            )
            if actor is Actor.CONTROLLER
            else None
        )
        store = _applied(
            store.add(
                actor=actor,
                kind=Kind(row["kind"]),
                scope=scope,
                text=row["text"],
                attempt=0 if actor is Actor.OPERATOR else attempt,
                key=ProposalKey("bench", attempt, "reader", candidate)
                if actor is Actor.AGENT
                else None,
                evidence=evidence,
                accepted=actor is Actor.AGENT,
            )
        )
        ids[row["key"]] = store.source_seq
        return store

    for row in rows:
        if row["actor"] == "operator":
            store = add(store, row)
    store = MemoryStore(store.repository, store.run_id, MAX_FACTS, store.encode())
    for row in rows:
        if row["actor"] != "operator" and row is not delayed:
            store = add(store, row)
    before_changes = store
    original = {r["key"]: r for r in rows}
    for op in operations:
        store = _applied(
            store.revise(
                op["op"],
                ids[op["key"]],
                actor=Actor.OPERATOR,
                reason=op["reason"],
                issue=fixture["issues"][original[op["key"]]["issue"]],
                text=op.get("text"),
                replacement=ids.get(op.get("by")),
            )
        )
    before_append = store
    if delayed:
        store = add(store, delayed)
    return ids, before_changes, before_append, store


@dataclass
class Expected:
    """Human declarations and operation history, independent of store visibility."""

    permitted: set[str]
    current: dict[str, str]
    old: dict[str, list[str]]


def _oracle(fixture):
    rows = {r["key"]: r for r in fixture["records"]}

    def applies(row):
        # The fixture's sole launch is main/current, already proved to
        # descend from base. Its only variable range condition is exact SHA.
        return row["issue"] == "main" and (
            row.get("range") != "exact" or row.get("candidate", "current") == "current"
        )

    permitted = {key for key, r in rows.items() if applies(r)}
    content = {key: r["text"] for key, r in rows.items()}
    ranks = {"agent": 0, "operator": 1, "controller": 2}
    absent = set()
    withdrawals = set()
    old = {}
    for op in fixture["operations"]:
        key = op["key"]
        if op["op"] in ("retract", "correct") and key in permitted:
            withdrawals.add((content[key], ranks[rows[key]["actor"]]))
        if op["op"] == "correct":
            old.setdefault(key, []).append(content[key])
            content[key] = op["text"]
        else:
            absent.add(key)
    current = {
        key: content[key]
        for key in permitted - absent
        if not any(
            content[key] == text and ranks[rows[key]["actor"]] <= rank for text, rank in withdrawals
        )
    }
    return Expected(permitted, current, old)


def _excerpt(text):
    encoded = text.encode()
    if len(encoded) <= 512:
        return text
    excerpt = encoded[: 512 - len(_MARKER)].decode(errors="ignore") + _MARKER
    return excerpt if redact(excerpt) == excerpt else "[details omitted]"


def _judged(ranked, relevant):
    seen = set()
    gains = []
    for key in ranked[:_K]:
        gains.append(int(key in relevant and key not in seen))
        seen.add(key)
    total = sum(gains)
    discounts = tuple(1 / math.log2(i + 2) for i in range(_K))
    return (
        total / _K,
        total / len(relevant),
        next((1 / (i + 1) for i, hit in enumerate(gains) if hit), 0),
        sum(a * b for a, b in zip(gains, discounts, strict=False))
        / sum(discounts[: len(relevant)]),
    )


def _new_visible(hit, current, old):
    if not old or hit != _excerpt(current):
        return False
    shown = hit.removesuffix(_MARKER)
    return all(hit != _excerpt(prior) and not prior.startswith(shown) for prior in old)


def _old_served(hit, current, old):
    return (current is None or hit != _excerpt(current)) and any(
        hit in (prior, _excerpt(prior)) for prior in old
    )


def _measure_quality(reader, fixture, expected, names):
    total = stale = wrong_scope = duplicate = negative_hits = visible = old_served = incomplete = 0
    quality = []

    def search(query):
        nonlocal total, stale, wrong_scope, duplicate, incomplete
        result = reader.search_memory(query, k=_K)
        incomplete += not result.complete
        hits = [(names.get(h.item.id), h.item.text) for h in result.hits]
        seen = set()
        for key, text in hits:
            total += 1
            duplicate += key in seen
            seen.add(key)
            wrong_scope += key not in expected.permitted
            stale += key not in expected.current or _excerpt(expected.current[key]) != text
        return hits

    for query in fixture["queries"]:
        ranked = [key for key, _ in search(query["query"])]
        quality.append(_judged(ranked, set(query["relevant"])))
    for query in fixture["negatives"]:
        negative_hits += bool(search(query["query"]))
    for probe in fixture["corrections"]:
        key = probe["key"]
        old = expected.old[key]
        current = expected.current.get(key)
        visible += any(
            k == key and current is not None and _new_visible(text, current, old)
            for k, text in search(probe["new_query"])
        )
        old_served += any(
            k == key and _old_served(text, current, old) for k, text in search(probe["old_query"])
        )
    metrics = dict(
        zip(
            ("precision_at_5", "recall_at_5", "mrr", "ndcg_at_5"),
            (statistics.mean(values) for values in zip(*quality, strict=True)),
            strict=True,
        )
    )
    return metrics | {
        "hits": total,
        "stale_leak_rate": stale / total if total else None,
        "permission_leak_rate": wrong_scope / total if total else None,
        "duplicate_hits": duplicate,
        "negative_queries_with_hits": negative_hits,
        "correction_new_text_visible": visible / len(fixture["corrections"]),
        "correction_old_text_served": old_served,
        "incomplete_queries": incomplete,
    }


def _no_index(docs):
    raise IndexUnavailable("disabled for benchmark")


def correctness(raw: bytes | None = None):
    raw = FIXTURE.read_bytes() if raw is None else raw
    data = _read_fixture(raw)
    ids, before_changes, before_append, current = _ledger(data)
    expected = _oracle(data)
    request = _request(data)
    names = {id_: name for name, id_ in ids.items()}
    result = {}
    for condition, start in {
        "synced": current,
        "stale_append": before_append,
        "stale_withdrawal": before_changes,
        "rebuilt_from_json": current,
        "no_index": current,
    }.items():
        source = [start]
        reader = MemoryRetriever(
            lambda source=source: Checkpoint(source[0]),
            request,
            backend=_no_index if condition == "no_index" else None,
        )
        if condition == "rebuilt_from_json":
            source[0] = MemoryStore(
                current.repository,
                current.run_id,
                current.launch_seq,
                json.loads(json.dumps(current.encode())),
            )
            status = reader.rebuild()
        else:
            status = reader.sync()
        if condition != "no_index" and not status.is_current:
            result[condition] = {"state": status.state.value}
            continue
        if condition != "rebuilt_from_json":
            source[0] = current
        result[condition] = _measure_quality(reader, data, expected, names)
    return {"fixture_sha256": hashlib.sha256(raw).hexdigest(), "conditions": result}


def _sample_ms(samples):
    samples = sorted(samples)
    return {
        "p50": round(statistics.median(samples) * 1000, 3),
        "p95": round(samples[math.ceil(len(samples) * 0.95) - 1] * 1000, 3),
        "n": len(samples),
    }


def _synthetic(size):
    store = _empty("owner/repo", "20260101T000000Z-abcdef", MAX_FACTS)
    step = _step("https://github.com/owner/repo/issues/2")
    request = ProjectionRequest(store.run_id, step, store.launch_seq + 1)
    for i in range(1, size + 1):
        store = _applied(
            store.add(
                actor=Actor.AGENT,
                kind=Kind.NOTE,
                scope=MemoryScope(step),
                attempt=i,
                key=ProposalKey("bench", i, "reader", "e" * 40),
                accepted=True,
                text=f"parser test build tools synthetic document {i} group {i % 7}",
            )
        )
    return store, request


def timing(size):
    store, binding = _synthetic(size)
    source = [store]
    held = MemoryRetriever(lambda: Checkpoint(source[0]), binding)
    reloaded = MemoryRetriever(lambda: Checkpoint(_reload(source[0])), binding)
    start = time.perf_counter()
    status = held.sync()
    build_ms = (time.perf_counter() - start) * 1000
    if status.state is IndexState.UNAVAILABLE:
        return {"state": status.state.value}
    if not status.is_current or not reloaded.sync().is_current:
        raise RuntimeError("benchmark failed to build FTS5")
    index = held._primary.published.index
    size_bytes = index.size_bytes
    modes = {}
    for label, reader in (("held", held), ("reconstructed", reloaded)):
        samples = []
        cpu = time.process_time()
        for i in range(40):
            start = time.perf_counter()
            result = reader.search_memory(f"parser test group {i % 7}")
            samples.append(time.perf_counter() - start)
        modes[label] = _sample_ms(samples) | {
            "cpu_ms_per_read": round((time.process_time() - cpu) * 1000 / len(samples), 3),
            "summary_utf8_bytes": sum(len(h.item.text.encode()) for h in result.hits),
            "catalog_utf8_bytes": len(reader.catalog().render().encode()),
        }
    tracemalloc.start()
    try:
        held.search_memory("parser test")
        _, python_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    update_samples = []
    visibility = []
    for i in range(1, min(size, 20) + 1):
        word = f"correctedrecord{i}"
        start = time.perf_counter()
        source[0] = _applied(
            source[0].revise(
                Action.CORRECT,
                i,
                actor=Actor.OPERATOR,
                issue=binding.step.issue_url,
                reason="benchmark correction",
                text=f"{word} new text",
            )
        )
        result = held.search_memory(word)
        if not any(h.item.id == i for h in result.hits):
            raise RuntimeError("benchmark correction is not served")
        visibility.append(time.perf_counter() - start)
        start = time.perf_counter()
        if not held.sync().is_current:
            raise RuntimeError("benchmark update failed")
        update_samples.append(time.perf_counter() - start)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "read_ms": modes,
        "build_ms": round(build_ms, 3),
        "index_bytes": size_bytes,
        "python_peak_bytes_one_held_read": python_peak,
        "process_peak_rss_bytes": rss if platform.system() == "Darwin" else rss * 1024,
        "batch_update_ms": _sample_ms(update_samples),
        "ingestion_to_tail_visibility_ms": _sample_ms(visibility),
    }


def _sizes(raw):
    try:
        values = [int(s) for s in raw.split(",")]
        if not all(1 <= n <= MAX_FACTS for n in values):
            raise ValueError
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"enter sizes from 1 to {MAX_FACTS}, comma separated"
        ) from error
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--correctness", action="store_true")
    parser.add_argument("--sizes", type=_sizes, default="29,64")
    args = parser.parse_args()
    data = correctness()
    if not args.correctness:
        data["timing"] = {size: timing(size) for size in args.sizes}
        data["host"] = {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "system": platform.system(),
            "arch": platform.machine(),
        }
    print(json.dumps(data, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
