"""Issue #324's retrieval benchmark: correctness on a pinned fixture, cost on this host.

No test: the measurement behind ``docs/memory-retrieval.md``. Its correctness
half also backs ``tests/test_memory_retrieval.py``, which holds the safety
rates to zero and the quality figures to the values the fixture pins::

    uv run python -m tests.memory_retrieval_bench              # both halves, JSON
    uv run python -m tests.memory_retrieval_bench --correctness

**Correctness** runs the fixture's queries (``tests/fixtures/memory_retrieval/
corpus.json``) against the real store and the real FTS5 index under six
conditions: the index synced, the index synced before the fixture's
retractions, supersession and correction (stale, with and without the hot
tail), no FTS5 at all, a fresh process's rebuild, and an empty store. Each
served hit is checked against an oracle derived from the fixture's own
declarations, never from :meth:`MemoryStore.visibility`, so a stale or
permission leak is counted however the code decides.

**Performance** times the same index on the fixture's store, on a store at
Memory's record bound, and on synthetic corpora of the backend alone, which
no store reaches (a store holds at most 64 records). Only BM25 is measured:
no embedding of any kind is computed here, so nothing of this is evidence
for semantic retrieval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import sqlite3
import statistics
import sys
import time
import tracemalloc
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from unittest import mock

from autoforge import memory_retrieval
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
)
from autoforge.memory_retrieval import (
    Fts5Backend,
    IndexDocument,
    MemoryRetriever,
    SearchResult,
    query_terms,
    render_hits,
)
from autoforge.transitions import Phase

FIXTURE = Path(__file__).parent / "fixtures" / "memory_retrieval" / "corpus.json"
K = 5
LAUNCH_SEQ = 128


def load_fixture(path: Path = FIXTURE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_digest(path: Path = FIXTURE) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class World:
    fixture: dict
    store: MemoryStore
    ids: dict[str, int]
    request: ProjectionRequest

    def key(self, record: int) -> str:
        return {v: k for k, v in self.ids.items()}[record]


def _applied(outcome) -> MemoryStore:
    if outcome.status != "applied":
        raise AssertionError(f"the fixture's write was {outcome.status}")
    return outcome.store


def _scope(fixture: dict, record: dict, phase: Phase = Phase.ANALYZE_EXECUTE) -> MemoryScope:
    issue = fixture["issues"][record["issue"]]
    candidates = fixture["candidates"]
    step = StepKey(phase, issue, "", 0, 1)
    mode = Range(record.get("range", "any"))
    if mode is Range.EXACT:
        return MemoryScope(step, range=mode, candidate=candidates[record["candidate"]])
    if mode is Range.DESCENDANTS:
        return MemoryScope(step, range=mode, base=candidates["base"])
    return MemoryScope(step)


def build(fixture: dict, *, operations: bool = True) -> World:
    """The fixture's store, written through the store's own API, as a run would."""
    repo, run = fixture["repository"], fixture["run_id"]
    records = fixture["records"]
    store = MemoryStore(
        repo, run, 0, dict(enabled=True, run_id=run, events=[], texts={}, refusals=0)
    )
    ids: dict[str, int] = {}
    seeds = [r for r in records if r["actor"] == "operator"]
    if seeds != records[: len(seeds)] or len(seeds) > 1:
        raise AssertionError("the operator's seed is the fixture's one first record")
    for record in seeds:
        out = store.add(
            actor=Actor.OPERATOR,
            kind=Kind.SEED,
            scope=_scope(fixture, record, Phase.INITIALIZING),
            text=record["text"],
        )
        store, ids[record["key"]] = _applied(out), out.id
    store = MemoryStore(repo, run, LAUNCH_SEQ, store.encode())
    current = fixture["candidates"]["current"]
    for n, record in enumerate(records[len(seeds) :], 1):
        scope = _scope(fixture, record)
        if record["actor"] == "controller":
            digest = hashlib.sha256(record["key"].encode()).hexdigest()
            out = store.add(
                actor=Actor.CONTROLLER,
                kind=Kind(record["kind"]),
                scope=scope,
                text=record["text"],
                attempt=n,
                evidence=EvidenceRef(Receipt.VALIDATION, n, digest),
            )
        else:
            out = store.add(
                actor=Actor.AGENT,
                kind=Kind(record["kind"]),
                scope=scope,
                text=record["text"],
                attempt=n,
                key=ProposalKey(f"t{n}", n, "worker", current),
                accepted=True,
            )
        store, ids[record["key"]] = _applied(out), out.id
    main = fixture["issues"]["main"]
    step = StepKey(Phase.ANALYZE_EXECUTE, main, "", 0, 1)
    request = ProjectionRequest(
        run, step, LAUNCH_SEQ + 1, current, ancestors=(fixture["candidates"]["base"],)
    )
    world = World(fixture, store, ids, request)
    if operations:
        apply_operations(world)
    return world


def apply_operations(world: World) -> None:
    main = world.fixture["issues"]["main"]
    store = world.store
    for op in world.fixture["operations"]:
        target = world.ids[op["key"]]
        kwargs: dict = dict(actor=Actor.OPERATOR, reason=op["reason"], issue=main)
        if op["op"] == "retract":
            out = store.revise(Action.RETRACT, target, **kwargs)
        elif op["op"] == "supersede":
            out = store.revise(Action.SUPERSEDE, target, replacement=world.ids[op["by"]], **kwargs)
        else:
            out = store.revise(Action.CORRECT, target, text=op["text"], **kwargs)
        store = _applied(out)
    world.store = store


def oracle(fixture: dict) -> dict[str, tuple[str, str]]:
    """Each record's expected state and text, from the fixture's declarations alone."""
    ops = fixture["operations"]
    retracted = {op["key"] for op in ops if op["op"] == "retract"}
    superseded = {op["key"] for op in ops if op["op"] == "supersede"}
    corrected = {op["key"]: op["text"] for op in ops if op["op"] == "correct"}
    original = {r["key"]: r["text"] for r in fixture["records"]}
    withdrawn_texts = {original[k] for k in retracted}
    current = fixture["candidates"]["current"]
    result = {}
    for record in fixture["records"]:
        key = record["key"]
        if record["issue"] != "main":
            state = "foreign"
        elif (
            record.get("range") == "exact" and fixture["candidates"][record["candidate"]] != current
        ):
            state = "inapplicable"
        elif key in retracted:
            state = "retracted"
        elif original[key] in withdrawn_texts:
            state = "withheld"
        elif key in superseded:
            state = "superseded"
        else:
            state = "current"
        result[key] = (state, corrected.get(key, original[key]))
    return result


def _dcg(gains: Sequence[int]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def quality(results: Sequence[tuple[Sequence[str], set[str]]], k: int = K) -> dict:
    """Precision@K, Recall@K, MRR and nDCG@K (binary gains) over ranked keys."""
    precision, recall, rr, ndcg = [], [], [], []
    for ranked, relevant in results:
        top = list(ranked)[:k]
        found = [key in relevant for key in top]
        precision.append(sum(found) / k)
        recall.append(sum(found) / len(relevant))
        rr.append(next((1 / (i + 1) for i, hit in enumerate(found) if hit), 0.0))
        ideal = _dcg([1] * min(len(relevant), k))
        ndcg.append(_dcg([int(hit) for hit in found]) / ideal)
    return {
        f"precision_at_{k}": round(statistics.fmean(precision), 4),
        f"recall_at_{k}": round(statistics.fmean(recall), 4),
        "mrr": round(statistics.fmean(rr), 4),
        f"ndcg_at_{k}": round(statistics.fmean(ndcg), 4),
    }


def _condition(world: World, retriever: MemoryRetriever, *, k: int = K) -> dict:
    fixture, truth = world.fixture, oracle(world.fixture)
    served = stale = permission = 0
    ranked: list[tuple[list[str], set[str]]] = []
    complete = []

    def search(query: str) -> tuple[SearchResult, list[str]]:
        nonlocal served, stale, permission
        result = retriever.search_memory(world.request, query, k=k)
        keys = [world.key(hit.id) for hit in result.hits]
        for hit, key in zip(result.hits, keys, strict=True):
            state, text = truth[key]
            served += 1
            stale += state not in ("current", "foreign", "inapplicable") or hit.summary != text
            permission += state in ("foreign", "inapplicable")
        complete.append(result.complete)
        return result, keys

    for query in fixture["queries"]:
        ranked.append((search(query["query"])[1], set(query["relevant"])))
    negative_hits = sum(bool(search(q["query"])[1]) for q in fixture["negatives"])
    new_visible = old_served = 0
    for correction in fixture["corrections"]:
        old_text = next(r["text"] for r in fixture["records"] if r["key"] == correction["key"])
        new_visible += correction["key"] in search(correction["new_query"])[1]
        result, _ = search(correction["old_query"])
        old_served += any(old_text in hit.summary for hit in result.hits)
    corrections = len(fixture["corrections"])
    return {
        **quality(ranked, k),
        "queries": len(ranked),
        "served": served,
        "stale_leak_rate": stale / served if served else 0.0,
        "permission_leak_rate": permission / served if served else 0.0,
        "adversarial_negative_hit_rate": negative_hits / len(fixture["negatives"]),
        "correction_old_text_served": old_served,
        "correction_new_text_visible": new_visible / corrections,
        "complete_searches": sum(complete),
        "searches": len(complete),
        "status": retriever.status().encode(),
        "ranked": [keys for keys, _ in ranked],
    }


def correctness(fixture: dict | None = None) -> dict:
    """Every condition's metrics on the pinned fixture."""
    fixture = fixture or load_fixture()
    repo, run = fixture["repository"], fixture["run_id"]
    report: dict = {"fixture_sha256": fixture_digest(), "k": K}

    world = build(fixture)
    synced = MemoryRetriever(repo, run, lambda: world.store)
    synced.sync()
    report["synced"] = _condition(world, synced)

    for name, tail in (("stale", memory_retrieval.MAX_TAIL), ("stale_without_tail", 0)):
        lagged = build(fixture, operations=False)
        lagging = MemoryRetriever(repo, run, lambda w=lagged: w.store, tail=tail)
        lagging.sync()
        apply_operations(lagged)
        report[name] = _condition(lagged, lagging)

    world = build(fixture)
    with mock.patch.object(memory_retrieval, "_open_memory_database", return_value=None):
        no_index = MemoryRetriever(repo, run, lambda: world.store)
    no_index.sync()
    report["no_fts5"] = _condition(world, no_index)

    # A new process: the checkpoint read back from its JSON, the index rebuilt.
    reloaded = MemoryStore(repo, run, LAUNCH_SEQ, json.loads(json.dumps(world.store.encode())))
    rebuilt = MemoryRetriever(repo, run, lambda: reloaded)
    rebuilt.rebuild()
    report["rebuilt"] = _condition(replace(world, store=reloaded), rebuilt)
    report["rebuilt_matches_synced"] = report["rebuilt"]["ranked"] == report["synced"]["ranked"]

    empty = MemoryStore(
        repo, run, LAUNCH_SEQ, dict(enabled=True, run_id=run, events=[], texts={}, refusals=0)
    )
    blank = MemoryRetriever(repo, run, lambda: empty)
    blank.sync()
    result = blank.search_memory(world.request, fixture["queries"][0]["query"])
    report["empty_store"] = {
        "hits": len(result.hits),
        "complete": result.complete,
        "status": result.status.encode(),
    }
    return report


# -- performance -------------------------------------------------------------------


def _percentiles(samples_ns: Sequence[int]) -> dict:
    ordered = sorted(samples_ns)

    def at(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))] / 1e6, 4)

    return {"p50_ms": at(0.50), "p95_ms": at(0.95), "samples": len(ordered)}


def _timed(fn) -> tuple[int, object]:
    start = time.perf_counter_ns()
    value = fn()
    return time.perf_counter_ns() - start, value


def _words(rng: random.Random, n: int) -> list[str]:
    syllables = ["ka", "lo", "mi", "ne", "ru", "ta", "sho", "vi", "den", "par", "qua", "zel"]
    vocab: set[str] = set()
    while len(vocab) < n:
        vocab.add("".join(rng.choice(syllables) for _ in range(rng.randint(2, 4))))
    return sorted(vocab)


def synthetic(n: int, *, seed: int = 324, words: int = 40) -> tuple[list[IndexDocument], list[str]]:
    """``n`` documents of ``words`` Zipf-distributed pseudo-words, and the vocabulary."""
    rng = random.Random(seed)
    vocab = _words(rng, 4000)
    weights = [1 / (rank + 1) for rank in range(len(vocab))]
    docs = [
        IndexDocument(
            "corpus",
            i,
            1,
            hashlib.sha256(str(i).encode()).hexdigest(),
            " ".join(rng.choices(vocab, weights, k=words)),
        )
        for i in range(1, n + 1)
    ]
    return docs, vocab


def _store_at_bound(fixture: dict) -> World:
    world = build(fixture, operations=False)
    store = world.store
    current = fixture["candidates"]["current"]
    step = world.request.step
    rng = random.Random(64)
    vocab = _words(rng, 400)
    n = len(world.ids)
    while len(store.facts()) < MAX_FACTS:
        n += 1
        out = store.add(
            actor=Actor.AGENT,
            kind=Kind.NOTE,
            scope=MemoryScope(step),
            text=" ".join(rng.choices(vocab, k=40)),
            attempt=n,
            key=ProposalKey(f"fill{n}", n, "worker", current),
            accepted=True,
        )
        store = _applied(out)
    world.store = store
    return world


def _searches(retriever: MemoryRetriever, request, queries: Sequence[str], rounds: int):
    for query in queries:  # warm
        retriever.search_memory(request, query)
    cpu = time.process_time()
    samples = []
    for _ in range(rounds):
        for query in queries:
            ns, _ = _timed(lambda q=query: retriever.search_memory(request, q))
            samples.append(ns)
    return samples, (time.process_time() - cpu) * 1000 / len(samples)


def _store_costs(world: World, queries: Sequence[str], *, rounds: int = 20) -> dict:
    """One store's index: its build, its searches, its size and what k=8 hits render to.

    Searched twice: with the snapshot held, a source that returns the
    validated store it has (the read path's own cost), and with a source
    that reconstructs and validates the store from the checkpoint's value
    at every read, as ``AutoForgeState.memory`` does (the cost of loading
    the canonical snapshot included). ``snapshot_load`` is that
    reconstruction alone.
    """
    store, request = world.store, world.request
    repo, run, checkpoint = store.repository, store.run_id, store.encode()

    def reload() -> MemoryStore:
        return MemoryStore(repo, run, LAUNCH_SEQ, checkpoint)

    held = MemoryRetriever(repo, run, lambda: store)
    build_ns, _ = _timed(held.sync)
    samples, cpu_ms = _searches(held, request, queries, rounds)
    loading = MemoryRetriever(repo, run, reload)
    loading.sync()
    loaded, loaded_cpu_ms = _searches(loading, request, queries, max(rounds // 4, 1))
    loads = [_timed(reload)[0] for _ in range(max(rounds * 5, 5))]
    rendered = [len(render_hits(held.search_memory(request, q, k=8)).encode()) for q in queries]
    return {
        "records": len(store.facts()),
        "build_ms": round(build_ns / 1e6, 4),
        "search": _percentiles(samples),
        "search_cpu_ms_per_query": round(cpu_ms, 4),
        "search_loading_the_snapshot": _percentiles(loaded),
        "search_loading_the_snapshot_cpu_ms_per_query": round(loaded_cpu_ms, 4),
        "snapshot_load": _percentiles(loads),
        "index_bytes": held.backend.size_bytes(),
        "rendered_hits_bytes_k8": {
            "max": max(rendered),
            "mean": round(statistics.fmean(rendered), 1),
        },
    }


def performance(
    fixture: dict | None = None, *, sizes: Sequence[int] = (1_000, 10_000), rounds: int = 20
) -> dict:
    fixture = fixture or load_fixture()
    repo, run = fixture["repository"], fixture["run_id"]
    queries = [q["query"] for q in fixture["queries"]] + [q["query"] for q in fixture["negatives"]]
    report: dict = {}

    report["fixture_store"] = _store_costs(build(fixture), queries, rounds=rounds)
    report["store_at_bound"] = _store_costs(_store_at_bound(fixture), queries, rounds=rounds)

    # Ingestion to visibility: a record added after the index was synced is
    # served by the tail at once, and by the index after the next sync.
    world = build(fixture)
    retriever = MemoryRetriever(repo, run, lambda: world.store)
    retriever.sync()
    marker = "zephyrquill"
    n = len(world.ids) + 1
    out = world.store.add(
        actor=Actor.AGENT,
        kind=Kind.NOTE,
        scope=MemoryScope(world.request.step),
        text=f"A fresh lesson about {marker}.",
        attempt=n,
        key=ProposalKey(f"fresh{n}", n, "worker", fixture["candidates"]["current"]),
        accepted=True,
    )
    world.store = _applied(out)
    tail_ns, tail_result = _timed(lambda: retriever.search_memory(world.request, marker))
    sync_ns, _ = _timed(retriever.sync)
    index_ns, index_result = _timed(lambda: retriever.search_memory(world.request, marker))
    report["ingestion_to_visibility"] = {
        "tail_search_ms": round(tail_ns / 1e6, 4),
        "tail_served": [h.provenance.backend for h in tail_result.hits],
        "incremental_sync_ms": round(sync_ns / 1e6, 4),
        "index_search_ms": round(index_ns / 1e6, 4),
        "index_served": [h.provenance.backend for h in index_result.hits],
    }

    scale = {}
    for n in sizes:
        docs, vocab = synthetic(n)
        rng = random.Random(n)
        probes = [" ".join(rng.sample(vocab[:500], 3)) for _ in range(200)]
        backend = Fts5Backend()
        build_ns, _ = _timed(lambda b=backend, d=docs: b.apply(d, (), clear=True))
        cpu = time.process_time()
        samples = []
        for probe in probes:
            terms = query_terms(probe)
            ns, _ = _timed(
                lambda t=terms, b=backend: b.search("corpus", t, memory_retrieval.MAX_CANDIDATES)
            )
            samples.append(ns)
        search_cpu = (time.process_time() - cpu) * 1000 / len(samples)
        updates = []
        for i in range(20):
            doc = replace(docs[i], revision=2, text=docs[i].text + " revised")
            ns, _ = _timed(lambda d=doc, b=backend: b.apply([d], ()))
            updates.append(ns)
        size = backend.size_bytes()
        backend.close()
        tracemalloc.start()
        traced = Fts5Backend()
        traced.apply(docs, (), clear=True)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        traced.close()
        scale[str(n)] = {
            "build_ms": round(build_ns / 1e6, 2),
            "index_bytes": size,
            "search": _percentiles(samples),
            "search_cpu_ms_per_query": round(search_cpu, 4),
            "update_one": _percentiles(updates),
            "python_peak_alloc_bytes_during_build": peak,
            "corpus_text_bytes": sum(len(d.text.encode()) for d in docs),
        }
    report["backend_scale"] = scale
    return report


def host() -> dict:
    return {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--correctness", action="store_true", help="the correctness half only")
    parser.add_argument("--sizes", default="1000,10000", help="synthetic corpus sizes")
    args = parser.parse_args(argv)
    report: dict = {"host": host(), "correctness": correctness()}
    if not args.correctness:
        sizes = tuple(int(s) for s in args.sizes.split(",") if s)
        report["performance"] = performance(sizes=sizes)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
