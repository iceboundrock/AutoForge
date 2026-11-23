"""The judged benchmark must detect faulty answers without using store verdicts."""

import argparse
import json
from dataclasses import replace
from types import SimpleNamespace as Entry

import pytest

from tests import memory_retrieval_bench as bench


def _fixture():
    return json.loads(bench.FIXTURE.read_bytes())


def _row(fixture, key):
    return next(r for r in fixture["records"] if r["key"] == key)


def _measure(fixture, injected=None):
    ids, _, _, store = bench._ledger(fixture)
    reader = bench.MemoryRetriever(
        lambda: bench.Checkpoint(store), bench._request(fixture), backend=bench._no_index
    )
    reader.sync()

    class Simulated:
        def search_memory(self, query, *, k):
            real = reader.search_memory(query, k=k)
            if injected is None or query != injected[0]:
                return real
            key, text = injected[1:]
            # Plant independently invalid content or identity, even on a zero-hit query.
            fake = Entry(item=Entry(id=ids[key], text=text))
            hits = [h for h in real.hits if h.item.id != ids[key]]
            return replace(real, hits=(fake, *hits[: k - 1]))

    return bench._measure_quality(
        Simulated(), fixture, bench._oracle(fixture), {v: k for k, v in ids.items()}
    )


def test_pin_is_the_bytes_parsed_and_honest_baseline_stays_unchanged():
    raw = bench.FIXTURE.read_bytes()
    default = bench.correctness()
    assert default["fixture_sha256"] == bench.PIN
    assert bench.correctness(raw + b"\n")["fixture_sha256"] != bench.PIN
    fixture = json.loads(raw)
    fixture["queries"][0]["query"] = "nonexistingkey"
    modified = bench.correctness(json.dumps(fixture).encode())
    assert modified["fixture_sha256"] != bench.PIN
    for label, metrics in default["conditions"].items():
        if metrics == {"state": "unavailable"}:
            continue
        assert metrics["permission_leak_rate"] == metrics["stale_leak_rate"] == 0
        assert metrics["negative_queries_with_hits"] == metrics["duplicate_hits"] == 0
        assert metrics["correction_new_text_visible"] == 1
        assert metrics["correction_old_text_served"] == 0
        if label in ("synced", "stale_append", "rebuilt_from_json"):
            assert metrics["recall_at_5"] == metrics["mrr"] == 1
            assert metrics["precision_at_5"] == pytest.approx(0.28)
            assert metrics["ndcg_at_5"] == pytest.approx(0.990195)
            assert modified["conditions"][label]["recall_at_5"] < 1
        else:
            assert metrics["incomplete_queries"] == 23


def test_duplicate_with_missing_relevant_record_cannot_inflate_metrics():
    assert bench._judged(["m5", "m5"], {"m5", "m23"}) == pytest.approx((0.2, 0.5, 1, 0.613147))
    assert bench._judged(["m5"] * 5, {"m5", "m23"}) == bench._judged(["m5"], {"m5", "m23"})

    fixture = _fixture()

    class Repeating:
        def search_memory(self, query, *, k):
            hit = Entry(item=Entry(id=1, text=_row(fixture, "m5")["text"]))
            hits = (hit, hit) if query == "ruff line length lint" else ()
            return Entry(hits=hits, complete=True)

    result = bench._measure_quality(Repeating(), fixture, bench._oracle(fixture), {1: "m5"})
    assert result["duplicate_hits"] == 1


@pytest.mark.parametrize("sizes", ["", ",", "10,,20", "10,", "-1", "0", "65"])
def test_bad_size_arguments_are_refused(sizes):
    with pytest.raises(argparse.ArgumentTypeError):
        bench._sizes(sizes)


def test_update_sampler_handles_small_corpus():
    assert bench._sizes("1,10,64") == [1, 10, 64]
    timing = bench.timing(1)
    if timing != {"state": "unavailable"}:
        assert timing["batch_update_ms"]["n"] == timing["ingestion_to_tail_visibility_ms"]["n"] == 1


def test_fixture_refuses_text_that_would_be_normalized_on_admission():
    fixture = _fixture()
    _row(fixture, "m3")["text"] += "\0"
    with pytest.raises(ValueError, match="exactly the text"):
        bench.correctness(json.dumps(fixture).encode())


def test_authorized_long_summary_is_compared_to_its_bounded_excerpt():
    fixture = _fixture()
    correction = next(op for op in fixture["operations"] if op["op"] == "correct")
    correction["text"] += " " + "界" * 200
    honest = _measure(fixture)
    assert honest["stale_leak_rate"] == 0
    assert honest["correction_new_text_visible"] == 1
    invalid = _measure(fixture, ("config_lists", "m15", correction["text"]))
    assert invalid["stale_leak_rate"] > 0 and invalid["correction_new_text_visible"] == 0


def test_oracle_observes_withdrawal_scope_and_text_at_correction_time():
    fixture = _fixture()
    current = _row(fixture, "m10")
    old_candidate = current | {"key": "copy", "candidate": "old"}
    fixture["records"].append(old_candidate)
    fixture["operations"].append({"op": "retract", "key": "copy", "reason": "old candidate"})
    assert bench._oracle(fixture).current["m10"] == current["text"]
    assert _measure(fixture)["stale_leak_rate"] == 0
    old_candidate["candidate"] = "current"
    fixture["operations"][-1] = {
        "op": "correct",
        "key": "m10",
        "text": "replacement parser note",
        "reason": "new evidence",
    }
    assert "copy" not in bench._oracle(fixture).current
    assert _measure(fixture, ("config_lists", "copy", old_candidate["text"]))["stale_leak_rate"] > 0


@pytest.mark.parametrize(
    "old,new",
    [
        ("x" * 511, "x" * 511 + " corrected"),
        ("x " * 300, "x " * 300 + "corrected"),
        ("x " * 300, bench._excerpt("x " * 300)),
        ("x " * 300, "x " * 50 + "\n[omitted]"),
    ],
)
def test_truncation_or_reused_rendering_cannot_count_as_visible_correction(old, new):
    excerpt = bench._excerpt(new)
    assert not bench._new_visible(excerpt, new, [old])
    assert not bench._old_served(excerpt, new, [old])


def test_append_only_and_intermediate_corrections_are_judged_by_rendered_text():
    fixture = _fixture()
    prior = _row(fixture, "m15")["text"]
    correction = next(op for op in fixture["operations"] if op["op"] == "correct")
    intermediate = prior + " intermediateword"
    correction["text"] = intermediate
    fixture["negatives"] = [q for q in fixture["negatives"] if q["query"] != "samples directory"]
    probe = fixture["corrections"][0]
    probe["new_query"] = "intermediateword"
    honest = _measure(fixture)
    assert honest["correction_new_text_visible"] == 1 and honest["correction_old_text_served"] == 0
    stale = _measure(fixture, ("intermediateword", "m15", prior))
    assert stale["stale_leak_rate"] > 0 and stale["correction_new_text_visible"] == 0
    fixture["operations"].append(correction | {"text": "finalword replaced it"})
    probe.update(new_query="finalword", old_query="intermediateword")
    mid = _measure(fixture, ("intermediateword", "m15", intermediate))
    assert mid["correction_old_text_served"] == 1 and mid["stale_leak_rate"] > 0
