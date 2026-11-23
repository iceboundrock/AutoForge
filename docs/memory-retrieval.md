# Issue Working Memory retrieval

[#324](https://github.com/iceboundrock/AutoForge/issues/324) adds a controller-side
read path over the Memory ledger in `state.json`. It is not an agent tool or a
change to what `ANALYZE_EXECUTE` currently receives. No workflow, prompt,
configuration, persisted protocol, or dependency changes. The existing
`memory.md` projection still works without SQLite FTS5.

## Authority and coverage

Construct a `MemoryRetriever` for the next launch's `ProjectionRequest`, as
prepared by the controller, and give it a loader for the current committed
`Checkpoint` (the validated store plus the current citation inputs). Call
it serially under the run's existing lock. Replace the reader if the
binding changes, including a change of starting candidate without an
ordinal change. It is not a cache to share between issues or launches.

`search_memory` returns `MemoryHit` records with bounded excerpts,
applicability, citation and retention, and score provenance. `fetch_memory`
looks the ID up afresh and returns the current bounded item or `None`;
missing and forbidden IDs are indistinguishable. Both return `ReadScope`,
`ScopeFreshness` and the identity `revision`, even when there are no hits.
IDs remain run-local as in `memory.md`; they are not stable references
across runs. `catalog()` renders a fixed vocabulary of authorized kinds and
their counts, and the same freshness. It includes no record titles, IDs,
identity revision, ancestry proofs or digests of withheld text, and its
length has a fixed bound whatever the records. Metrics are saturating
aggregate counts, not queries, session data, raw backend errors or prompts.
All returned prose is untrusted; a future consumer must fence it as data,
not concatenate it into instructions.

Launch answers and controller maintenance report freshness in different
types, because the run's audit positions count writes the binding cannot
read. Launch answers carry two digests, and they answer different
questions:

- The **fingerprint**, `ScopeFreshness.fingerprint` on search, fetch and
  catalog, digests the authorized records' kinds and current text, and
  nothing else: no ID and no count of other records. It changes when the
  kinds or text the binding can read change. It is the same for every
  checkpoint that presents the same kinds and text, whatever withheld
  history led there: withdrawn and superseded records, inapplicable ones,
  and other issues' records, before or after the visible ones. It is the
  catalog's revision. It cannot see a same-text supersession, and neither
  can the catalog, which shows no ID.
- The identity **revision**, `revision` on search and fetch answers only,
  digests each authorized record's ID with its kind and current text: the
  records as those answers can show them (fetch answers any authorized
  ID). It also changes when a record is superseded by one of the same kind
  and text, since the replacement has an ID of its own, so a consumer that
  caches IDs can tell when one is obsolete. IDs are audit positions,
  assigned once: two histories that present the same kinds and text can
  number them differently, and the revision then differs, as the IDs a
  search shows do. The catalog does not carry it.

Every reader of the same checkpoint and binding reports the same values. A
write the binding cannot read, such as one to another issue or to an
inapplicable candidate range, changes neither digest. The fingerprint
depends on no record the binding cannot read. The revision depends on such
a record only through the IDs of the visible records, which the answers
show anyway, never through whether it applies to the binding. So an
earlier record withheld from the launch (withdrawn, or superseded) leaves
both digests as they would be had it been another issue's record.

`ScopeFreshness.pending` counts records: the authorized records the
answering generation does not verifiably hold at their current version,
plus held records no longer authorized. A record corrected twice counts
once. A generation known damaged, by a failed or contradictory lookup or
by `verify()`, verifies nothing whatever its manifest says: every
authorized record is pending until a replacement is published. Only the state says
whether the index is current. A current index has nothing pending, but
nothing pending does not make an index current: an index never built over
an empty corpus is `empty`, and a damaged one over an empty corpus is
`failed`, each with nothing pending.

`IndexStatus`, from `status()`, `sync()`, `rebuild()` and `verify()`,
carries the ledger's `source_seq` and the generation's
`indexed_through_seq`, and its `pending` counts audit rows, hidden ones
included. A damaged generation has no `indexed_through_seq`, so every
audit row is pending. Citation retention is resolved afresh for each
answer and is part of neither digest nor the index. The
`generation` counter is per reader.

The same store binding and applicability checks used by `memory.md` govern
retrieval, including phase, issue, candidate range, environment and
withdrawal of matching copies. This read path never returns superseded
prose, even where `memory.md` can still display an independently established
receipt's claim. Citations are checked against the receipts from that
authority read; carrying a citation does not make the prose authoritative.
A regressed launch ordinal or divergent/shorter audit is refused. Once a
reader observes opt-out, it can diagnose the disabled checkpoint but cannot
serve it or accept a later enabled checkpoint. Only the controller can
explicitly bind a new reader after opting in.

`sync()` projects only the currently authorized corpus for this binding.
FTS5 uses one private `:memory:` table, with parameterized `MATCH` and a
generation-local row ID allow-list. It returns IDs, not text, scores, or
revision fields. The retriever reads the current store again after each
lookup, including typed failures; it does not call backend health or
metadata hooks after that read. Results are built from the final
authoritative text and citation inputs. If any indexed version has since
been corrected, withdrawn, or otherwise become unservable, the entire
generation is not consulted. If that happens during lookup, the entire
answer, including an error, is ignored. No query-dependent state from old
postings is promoted to a launch diagnostic. This is deliberately stricter
than filtering individual candidate rows by current permissions.

Each index answer must account for every lexical match of its still usable
generation. The proof scans at most 64 authorized records. Ranking is
Okapi BM25 (`k1=1.2`, `b=0.75`, non-negative log IDF) computed by the
retriever over the full authorized corpus, with record ID breaking ties.
SQLite does not compute launch rank. Unicode normalization, case folding
and token boundaries are shared between the coverage proof, tail, and
index: the SQLite tokenizer sees only ASCII keys, one per normalized
Unicode term. Query terms are ORed, with no caller-provided FTS grammar.

The tail can add at most eight recent authorized records not covered by
the index. If no generation can answer, only that tail is searched. Tail
use for append-only lag does not make a readable index degraded; relying
on the tail alone does. `complete` is coverage of the current authorized
corpus for the query, not a guarantee about relevance or the number of
items returned. Later sync restores coverage after a correction or
retraction retires a generation. Inputs are limited to 512 query UTF-8
bytes and 16 requested hits. Oversized character counts and string
subclasses fail before encoding (and before source/backend calls); the
bounded remainder is encoded for the exact byte check.

Search excerpts share the request's `max_chars` and `max_bytes`: with `n`
hits, each is cut to `max_chars // n` characters and `min(512, max_bytes // n)`
UTF-8 bytes, its omission marker included, so together they fit both. An
intentionally tiny budget can give a ranked ID an empty excerpt; fetch
is bounded independently by the store's 2,048-byte record limit. That
budget is the launch projection's whole budget, and the excerpts may fill
it: a hit's typed metadata (ID, kind, applicability, citation, retention,
score) and the catalog are not rendered into it here and are not counted.
Nothing renders search answers into a launch today. A consumer that does
must fit its rendered metadata and the excerpts together into
`max_chars` and `max_bytes`, as `render_projection` fits its header, for
example by constructing the reader with a request whose budget leaves room
for the metadata.

## Publication and failure

At up to 64 records, every changed corpus is a full batch replacement:
build, commit and verify a new connection, then publish the connection,
watermark and manifest as one generation. Neither a failed build nor an
interrupted commit can mutate the previous connection. A failed rebuild
reports an error without making a healthy old generation unreadable;
`sync()` retries even with no new write. A generation already known damaged
stays unreadable until replaced. A process restart loses only derived data;
the controller rebuilds from the committed ledger.

There is no mutable partition map or separately maintained document count.
The native verifier checks the whole schema (including unexpected tables),
row count, IDs, bounded byte bodies against that generation's seals, and
FTS postings integrity. It can check a stale generation without retaining
a second copy of old prose in the manifest. `verify()` returns a failed
`IndexStatus` for index damage, rather than asking controller callers to
catch a SQLite exception. Maintenance status re-reads the store after
backend work. A write the binding cannot read advances the audit
watermark without forcing a healthy generation to be rebuilt: the next
`sync()` moves `indexed_through_seq` forward and keeps the generation.

Without FTS5 the index is unavailable and only the bounded tail is used.
An optional controller-supplied lexical backend has no output channel
besides bounded IDs. If explicitly configured, a separate, usable FTS5
fallback answers in the primary's place when the primary cannot answer,
on the same call, and when it holds more of the authorized records than
the primary. Both are refreshed from the same corpus, so only a failed
refresh of the primary, which keeps its last generation, leaves the
fallback ahead; on equal coverage the primary answers. If the preferred
generation cannot answer a search, the other is asked on the same call,
so an older generation still serves its records and the tail the rest.
Search, fetch and catalog report the answering fallback's status with
`degraded=true`; controller `status()` still reports the primary. Without
a usable fallback the tail reports the primary's status. Transient search
unavailability is retried on the next eligible query, whereas a
contradictory or failed lookup disqualifies that generation. Neither
availability nor failures are shared across readers of different launch
scopes.

## Offline evidence

```sh
uv run python -m tests.memory_retrieval_bench --sizes 10,29,64
```

The [pinned fixture](../tests/fixtures/memory_retrieval/corpus.json) keeps
the previous attempt's 29 human-judged records and queries unchanged.
The report hashes the input bytes it parsed. Its oracle derives scope,
withdrawals and each correction from the fixture declarations, not the
retriever. It judges the expected current UTF-8 excerpt, credits relevance
once per ID, counts duplicates separately and checks that a visible
correction shows changed text rather than a shorter prefix or a different
omission marker.

| Condition on the judged fixture | P at 5 | Recall at 5 | MRR | nDCG at 5 |
|---|---:|---:|---:|---:|
| Synced, append-only lag, or rebuilt | 0.280 | 1.000 | 1.000 | 0.990 |
| Withdrawal lag or no index | 0.120 | 0.456 | 0.533 | 0.472 |

Across the five conditions, the run observed no stale or permission leaks,
duplicate hits, or hits on the six negative queries. The corrected wording
was visible in every condition and the old excerpt was not served. In
withdrawal lag and no-index case, all 23 queries report incomplete coverage.
Rebuild here means a new retriever reconstructing a JSON-round-tripped
checkpoint in the **same process**, not an observed process restart.

Timing uses separate synthetic corpora (not relevance judgments) within the
store's 64-record limit. One run of the replacement on x86_64 Linux,
Python 3.14.10 and SQLite 3.54.1, with other local validation active:

| Synthetic records | Search held p50 / p95 (ms) | Search reconstructed p50 / p95 (ms) | Initial sync (ms) | Batch replacement p50 / p95 (ms) | Ingestion to tail p50 / p95 (ms) | SQLite pages |
|---:|---:|---:|---:|---:|---:|---:|
| 29 | 1.37 / 2.80 | 21.78 / 33.59 | 1.56 | 1.75 / 2.63 | 16.22 / 20.78 | 24 KiB |
| 64 | 2.03 / 4.12 | 43.91 / 61.00 | 2.85 | 2.59 / 4.62 | 26.66 / 40.47 | 32 KiB |

At 64 records the mean CPU cost was 2.45 ms per held read and 47.82 ms per
reconstructed read. A separately traced held read peaked at about 65 KiB
of Python allocations; process-wide peak RSS was about 34 MiB. The sample
returns 264 summary bytes and a 190-byte catalog. Those counts are a
conservative upper bound for byte-level tokenizers, not measured model
tokens. No hybrid or model measurements are implied.

## LTSearch: source-based no-go

This decision carries forward the source inspection recorded on
[#537](https://github.com/iceboundrock/AutoForge/pull/537) at LTSearch commit
[`2f5f8d9383f72e9e8c41bca350ead29a05c3ec74`](https://github.com/Lychee-Technology/LTSearch/tree/2f5f8d9383f72e9e8c41bca350ead29a05c3ec74).
That inspection did not build or run LTSearch. Its findings:

- **Query path:** Tantivy BM25 (`src/query/keyword_searcher.rs`) and flat
  LanceDB vector search (`src/query/vector_searcher.rs`). Dynamic results fuse
  with reciprocal rank fusion (`src/query/ranker.rs`, `router.rs`). TurboQuant
  indexes static data in a separate path; it does not participate in that
  fusion. Metadata filters run in the router after retrieval, not as an
  authorization or tenant boundary.
- **Deployment and activation:** local Compose starts write, build and query
  roles sharing a volume and SQLite control file; HTTP listens on
  `0.0.0.0` (`docker-compose.local.yml`, `src/http/mod.rs`). Builds reload
  upstream segments into Tantivy and Lance and switch the active head
  (`src/build_worker.rs`). The documented update latency is not a measured
  property of this report.
- **Embeddings and platform:** local Compose's `fixed` provider supplies
  a constant three-dimensional vector, not semantic retrieval. The inspected
  LTEmbed build targets aarch64 Linux, uses a private dependency and a
  downloaded GGUF model (`Cargo.toml`, `sam/local.Dockerfile`); AWS
  features have separate model acquisition. No sandbox-network or
  x86_64/macOS runtime support was demonstrated.
- **Redistribution:** the inspected tree and its top-level `Cargo.toml`
  contained no license grant; public source is not permission to vendor.
  Licenses of individual direct dependencies do not clear that code or
  its locked transitive graph.

| Option | Decision |
|---|---|
| Adapt the local service or IPC boundary | No: process lifecycle, network exposure, model provisioning and index activation add obligations the native read path does not have. Post-retrieval metadata filters cannot replace the store's authority barrier. |
| Reuse its query core as a library | No: no confirmed redistribution rights, no Python API and a large Rust/Lance toolchain footprint. |
| Use native FTS5; consider published algorithms independently | Yes: no LTSearch code, service, dependency, model download, or credentials. BM25 is the lexical baseline, not a claim about hybrid benefit. |

The [maintainer's decision](https://github.com/iceboundrock/AutoForge/issues/324#issuecomment-7089522504)
accepts this source-based no-go. LTSearch was not measured, the maintainers
were not contacted for a license clarification, and the 680 locked crates'
licenses were not audited. Those are limitations, not requirements remaining
for this issue and not licensing or integration clearance. Reopening would
need licensing and portability evidence, an explicit authority and network
design, and measured benefit from real, opt-in embeddings.
