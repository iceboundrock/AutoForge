# Memory retrieval: the FTS5 read path and the LTSearch feasibility (#324)

Written after the implementation, for #324 (EPIC #331), at `main`
`5cad948`. It records what the read path decides and why, what was
measured on one host, and the go/no-go on LTSearch. ADR 0015 is the
authority this builds on: layer 4 of its four memory layers (D8), the
"later retriever" of D12, and an implementation note in D10 for the
SQLite connection this adds.

## What exists, and what does not

`src/autoforge/memory_retrieval.py` is a library. Given a launch's binding
(the `ProjectionRequest` the projection is rendered for) and a query, it
returns bounded hits (`search_memory`), one record whole (`fetch_memory`),
or a catalog of labels and counts (`catalog`). Each answer is read again
from the current `MemoryStore` before it is returned.

Nothing in the package calls it yet. The projection a launch is shown is
still ADR 0015 D12's deterministic render, unchanged, and no agent can
search: the issue's live `search_memory` and `fetch_memory` would be a new
channel from an agent to the controller. That needs a provider capability
`AgentRequest` does not have (`src/autoforge/providers.py`), through ADR
0007's broker, bounded per launch, which nobody has designed or approved.
No configuration key, state field, protocol number, prompt, phase, effect
or default changes, so there is nothing to coordinate on #23.

## The decisions

### The store decides what is served; the index only ranks

An index holds copies of records' texts, never their authority. A backend
answers with candidates: a record id, and the revision and content digest
it indexed. Only `MemoryRetriever` turns a candidate into a `MemoryHit`,
and only after `MemoryStore.visibility` says the record is `current` for
that launch. That check is the projection's own, in its order: another
issue's record is `foreign`; one whose phases, candidate range or
environment do not take the launch is `inapplicable`; then `retracted`,
`withheld` (a withdrawal of the very text it carries) and `superseded`.
A superseded record is never served, even where its citation establishes
a claim: resolving citations stays the projection's job.

A hit's excerpt is read from the store's live text, never from the index.
So a record corrected, retracted or superseded since the index last saw it
is either not served or served as it is now, however far behind the index
is. A candidate whose revision or digest the store contradicts, at a
revision the index claims to have seen, is never served, and it marks the
index `failed`.

Rank is never authority. A backend that answers with ids of another
issue's records, a retracted record or an id the store has never held gets
none of them served (`test_an_optional_backend_falls_back_only_to_fts5_and_never_across_permissions`).

### One binding: the projection's

The read path binds a request exactly as the projection does
(`memory.bind_request`, factored out of `render_projection`): Memory on, the
store's run, an `ANALYZE_EXECUTE` step of the store's repository, the
snapshot's next launch ordinal, and each candidate, ancestor and environment
digest in bounds. A retriever is bound to one repository and run, and every
read refuses a store of another. Inside a run, each issue is its own FTS5
table, so one issue's scores do not depend on another issue's records,
and no row of another issue can come back from a search
(`test_partitions_keep_one_issues_scores_and_rows_apart`).

### Freshness is reported, never assumed

The index is eventually consistent with the store. It records two things:
the Memory revision it was synced through (`indexed_through_seq`) and the
digest of the audit rows up to that revision. The store's audit table only
grows, so a store whose rows up to the watermark differ is not a
continuation of what was indexed. That covers an older checkpoint, another
run's store, or a rewritten one. Such an index is `failed` (`diverged`): no
search reads it, and the next sync rebuilds it. `IndexStatus` reports the
backend, state (`empty`, `current`, `stale`, `failed`, `unavailable`),
generation, both sequence numbers, the records whose latest change the
index has not seen (`pending`), consecutive sync failures and a typed reason.

A search reads the records changed after the watermark on the store
itself. This is the hot tail: at most eight records, newest first, matched
on the same folded terms. Its hits are fused with the index's by
reciprocal rank fusion. `SearchResult.complete` holds only when every
current record of the issue was searched, by an index synced through it or
in the tail, and no index row contradicted the store. Past the tail's
bound, `unsearched` counts what was not read. `degraded` says the primary
backend did not answer.

### Failures leave the index where it was

A sync is one SQLite transaction: every upsert and delete, or none. A
failure mid-batch publishes nothing and keeps the old watermark, and the
store still refuses anything stale in it. So a failed or partial build
cannot reopen a fact retracted since
(`test_a_retracted_record_is_unservable_at_once_and_a_failed_sync_never_reopens_it`).
The index lives in memory, so a crash loses it, and the next process
rebuilds it from the checkpoint. Given the same snapshot, the rebuild gives
the same rows and the same answers, scores included
(`test_a_new_process_rebuilds_the_same_index_and_answers_identically`).
`verify` compares every row with the store at the watermark and runs FTS5's
integrity check.

### In memory, never on disk

ADR 0015 D10 rejected SQLite as authoritative storage because SQLite opens
its database, journal, WAL and shared-memory files by pathname, outside the
controller's `SafeRoot` guarantees. The index avoids every such open. It is
a `:memory:` database with `temp_store` in memory, so no sort or temporary
table spills to a file. With `SQLITE_LIMIT_ATTACHED` set to 0, no `ATTACH`
can open one either. The test that held D10's "one SQLite connection" now
holds exactly these two connections, and checks this one's arguments
(`test_the_package_opens_sqlite_only_for_the_opencode_probe_and_the_memory_index`).

The cost is a rebuild per process. On this host that is 1.8 ms for the
fixture's 29 records and 3.2 ms at Memory's 64-record bound. A persisted
index, for #325's repository-wide layer for example, would need a decision
of its own in D10.

### A SQLite without FTS5 is a documented, limited fallback

FTS5 is a compile-time option of the SQLite library an interpreter links,
so a supported interpreter may lack it (ADR 0015 D10). `fts5_available()`
probes for it. Without it, the backend reports `unavailable`
(`fts5-unavailable`). A search then reads only the hot tail, which with no
index is the issue's eight newest records. It is `degraded` and not
`complete`, and the excerpts still pass the store's check. Nothing broader
is ever read, and no launch fails for it, since none uses retrieval.
`doctor` has no row for it, because nothing yet depends on the answer.

### An optional backend falls back only to FTS5

A primary backend other than FTS5 (none ships) may be configured only with
an `Fts5Backend` as its fallback. When the primary cannot answer, the
fallback does: the result is `degraded` and names the backend that
answered, and the primary is rebuilt by the next sync. Either way, the
same check of the store applies to every candidate.

### Untrusted text stays data

A query becomes its letters and digits only: folded, deduplicated, at most
16 terms of at most 64 characters, from a query of at most 512 bytes. Each
term is quoted before it reaches FTS5, so no query is FTS5 syntax, a column
filter or a scope. `render_hits` fences every excerpt with a backtick run
longer than any inside it. A record that says "ignore previous
instructions" or closes a fence stays inside its block
(`test_query_syntax_never_reaches_fts5_and_indexed_text_stays_fenced`).
`RetrievalMetrics` keeps a fixed set of capped counters: searches, hits,
bytes served and drops by reason. It never holds a query, a record's text
or a prompt.

### Bounds

| What | Bound |
|---|---|
| hits per search (`k`) | 1 to 16, default 8 |
| one hit's excerpt | 512 bytes, cut with an omission marker |
| a search's excerpts together | 4,096 bytes (a conservative bound on tokens, as the projection's is) |
| records read in the hot tail | 8 |
| candidates one backend search returns | 64 (every record a store can hold) |
| the catalog's rendering | 1,024 bytes |
| documents one backend holds | 100,000 |

## The measured baseline: BM25 only

Run with `uv run python -m tests.memory_retrieval_bench --sizes
1000,10000,50000` on one host: x86_64, 8 CPUs, Linux 7.0.14, Python 3.14.10,
SQLite 3.54.1. These are single runs of a local benchmark, not a
comparison across hosts. No embedding of any kind is computed, so nothing
below is evidence about semantic retrieval.

### Correctness on the pinned fixture

`tests/fixtures/memory_retrieval/corpus.json` (sha256
`ececa557…a89125`, pinned in `tests/test_memory_retrieval.py`) is one run's
Memory for issue 2 (trailing commas in config lists). It has 25 records of
issue 2 and four of issue 3. Among them are a retracted record, a withheld
duplicate, a superseded one, a corrected one, one bound to an earlier
candidate and one bound to a base's descendants. There are 15 queries with
hand-judged relevant records (21 judgments), six adversarial negatives
whose terms appear only in records the launch must not be served, and one
correction probe. Each served hit is checked against an oracle derived from
the fixture's declarations, not from `MemoryStore.visibility`.

| Condition | P@5 | R@5 | MRR | nDCG@5 | stale leaks | permission leaks | negatives with a hit | old text served | new text visible | complete searches |
|---|---|---|---|---|---|---|---|---|---|---|
| index synced | 0.28 | 1.00 | 1.00 | 0.990 | 0 / 56 | 0 / 56 | 0 / 6 | 0 | 1 / 1 | 23 / 23 |
| index stale (synced before the 4 revisions), hot tail on | 0.28 | 1.00 | 1.00 | 0.990 | 0 / 56 | 0 / 56 | 0 / 6 | 0 | 1 / 1 | 23 / 23 |
| index stale, hot tail off | 0.27 | 0.93 | 0.93 | 0.924 | 0 / 54 | 0 / 54 | 0 / 6 | 0 | 0 / 1 | 0 / 23 |
| no FTS5 (tail of 8 only) | 0.12 | 0.46 | 0.53 | 0.472 | 0 / 22 | 0 / 22 | 0 / 6 | 0 | 1 / 1 | 0 / 23 |
| a new process's rebuild | 0.28 | 1.00 | 1.00 | 0.990 | 0 / 56 | 0 / 56 | 0 / 6 | 0 | 1 / 1 | 23 / 23 |

P@5 of 0.28 is this fixture's ceiling: 21 relevant judgments over 15
queries of five places each. The rebuild's rankings equal the synced
index's, and an empty store gives no hit, `complete`. The test holds the
safety columns at zero in every condition. It also holds R@5 and MRR at
1.00 wherever the search is complete. It does not hold the quality figures
of the degraded conditions, which only show what each degradation costs.

Without the tail, the corrected record is not served at all until the next
sync, and every such search says it is incomplete. Without FTS5, recall
falls to what the eight newest records hold. Neither serves anything stale
or foreign.

### Cost on this host

| Corpus | Build | Search p50 / p95 | Index size |
|---|---|---|---|
| the fixture's store, 29 records (the whole read path) | 1.8 ms | 1.24 / 2.30 ms | 44 KiB |
| a store at Memory's bound, 64 records (the whole read path) | 3.2 ms | 1.90 / 3.17 ms | 76 KiB |
| 1,000 synthetic documents (the backend alone) | 9.5 ms | 0.23 / 0.55 ms | 0.8 MiB |
| 10,000 synthetic documents (the backend alone) | 82 ms | 0.79 / 3.09 ms | 7.3 MiB |
| 50,000 synthetic documents (the backend alone) | 453 ms | 2.34 / 10.1 ms | 32.8 MiB |

Notes on the table:

- **Synthetic corpora.** Their documents are 40 Zipf-distributed
  pseudo-words each. No store reaches these sizes: they bound what a
  larger, repository-wide layer would cost the backend.
- **Updates.** Updating one document is 0.04 ms at the median at every
  size. The 95th percentile is 1.7 ms at 1,000 documents, 13.9 ms at
  10,000 and 28.4 ms at 50,000, where FTS5 merges its segments.
- **Where the time goes.** At the store's sizes most of a search is
  Python: binding the request, the store's check of each candidate, and
  the digest of the audit rows.
- **Memory and CPU.** The index's memory is its database pages, the sizes
  above. Python's own allocations during a 50,000-document build peaked
  at 20 KiB, a figure that excludes SQLite's heap. CPU time per search is
  1.4 ms on the fixture's store and 2.1 ms at the bound.
- **Ingestion to visibility.** A record written after the last sync is
  served at the next search, from the tail, in 1.0 ms. An incremental sync
  takes 1.3 ms, and the index then serves the record itself.
- **Context.** Eight hits rendered by `render_hits` came to at most 1,555
  bytes (612 on average) on the fixture's queries. That is at most 1,555
  tokens for a byte-level tokenizer, against the projection's 16,384-byte
  budget.

Against the evaluation protocol's `memory_overhead` ceiling of 1,000 ms of
latency per launch, the read path at the store's bound costs under 4 ms per
search at p95.

## LTSearch feasibility: no-go

LTSearch's source was read, never built or run:
[Lychee-Technology/LTSearch](https://github.com/Lychee-Technology/LTSearch)
at commit `2f5f8d9383f72e9e8c41bca350ead29a05c3ec74` (2026-10-19), the
default branch's head when this was written. Paths below are in that tree.

### What the code does

- **License.** The tree has no LICENSE, COPYING or NOTICE file and no
  license header. `Cargo.toml` declares no `license` or `license-file`,
  and GitHub's API reports none. Code published without a license grants
  no right to copy, modify or redistribute it. The two crates under
  `vendor/` are shims: `protobuf-src` (Apache-2.0, a wrapper around
  `protoc-bin-vendored`) and `ltembed-stub`, which has no license and
  stands in for LTEmbed. `sam/local.Dockerfile` calls LTEmbed a private
  repository.
- **Transitive obligations: unknown.** `Cargo.lock` locks 680 packages
  and carries no license data. The direct dependencies include `tantivy
  =0.26.1`, `lance`/`lance-datafusion =10.0.0`, `lancedb 0.37.1`, `tokio`
  and `axum`, plus `ltembed` from a git revision. Nobody has audited the
  licenses of the whole graph.
- **Search.**
  - BM25 is Tantivy, behind a `QueryParser` over the caller's raw string
    (`src/query/keyword_searcher.rs`), so Tantivy's query syntax reaches
    callers.
  - Vector search is LanceDB with a flat dot-product scan and no ANN index
    (`src/query/vector_searcher.rs`).
  - RRF with k = 60 fuses only the dynamic vector and keyword results
    (`src/query/ranker.rs`, `src/query/router.rs:346-349`).
  - TurboQuant is a separate static path over an mmapped compressed
    corpus, built offline and activated by its own command, whose results
    come back in their own group. The project's own baseline shows the
    static build's default codec (`legacy_v3`, `src/app.rs`) at a recall@10
    of 0.0215 on 1,000 vectors, and both codecs scanning slower than exact
    f32 (`examples/turbo_bench/baseline.json`).
- **Metadata filtering runs after retrieval.**
  `apply_filters(results.dynamic_chunks, ...)` runs in the router
  (`src/query/router.rs:197-198`). The index stores metadata without
  indexing it, so it cannot filter. The router widens the retrieval window
  up to 100 to find enough matches (`router.rs:288-298`), so a filtered
  result can be silently short. The query path has no authentication,
  authorization or tenant code; the design documents describe some
  (`docs/design.md`), but none is implemented.
- **Local deployment.**
  - One binary with `write`, `build` and `query` roles, run as three
    processes. `docker-compose.local.yml` starts them as three containers
    sharing a volume and a SQLite control database (WAL, job queue,
    active-head table), with no FTS in SQLite.
  - The servers bind `0.0.0.0` (`src/http/mod.rs:60`) and talk HTTP/JSON.
    There is no Python binding or FFI.
  - Every build is a full rebuild of Tantivy and Lance from all write
    segments (`src/build_worker.rs`), activated by a compare-and-swap
    on the active head. The project's own documents claim 1 to 5 minutes
    of update latency.
- **Embeddings.**
  - The local compose file configures the `fixed` provider: one constant
    3-dimension vector for every input (`docker-compose.local.yml:16-20`).
    With it, every document gets the same vector score, so the vector list
    carries no relevance signal into the fusion.
  - Real inference is LTEmbed with llama.cpp and a GGUF model (Jina
    embeddings v5 nano), which compiles only on aarch64 Linux (`Cargo.toml`
    comment on the `ltembed` feature). The images are built and checked
    for arm64 only.
  - The model is downloaded from Hugging Face at image build and from S3
    at a Lambda's cold start, under the `aws` and `ltembed` features. The
    default `local` build compiles no AWS SDK.
- **Toolchain.** Rust 1.94.1 (`rust-toolchain.toml`), and a little-endian
  static index format (`src/index/mod.rs`).

### The three options

| Option | Verdict | Why |
|---|---|---|
| (a) a local service or IPC adapter | no-go | Three long-running HTTP processes, a shared volume and a SQLite control file: the daemon, port and lifecycle that AutoForge's single-process model and the agent sandbox do not have. A write is visible only after a full rebuild, while Memory's corrections must be visible at the next search. Its filters cannot be a security boundary, and its servers bind every interface. Nothing it adds is semantic: real embeddings exist only on arm64 Linux with a downloaded model and a private dependency. |
| (b) its query core as a library | no-go | No license, so no right to use, modify or redistribute it. It would also bring Rust, a 680-crate graph and a build of Lance and DataFusion into a Python package with no compiled dependency. |
| (c) no integration; borrow algorithms only | yes, as far as published algorithms go | The read path fuses its index's and its tail's lists by reciprocal rank fusion (k = 60), a published method (Cormack, Clarke and Büttcher, SIGIR 2009) written here from that description. No LTSearch code was copied or translated. |

**Decision: no-go for (a) and (b); FTS5 stays the default and only
backend.** The measurement half of the go/no-go is the native baseline
above. At Memory's scale of 64 records per run, BM25 serves every relevant
record of the fixture within five places, at under 4 ms per search, with
nothing stale or foreign. That leaves no measured gap for a hybrid backend
to close. LTSearch itself was not measured: running it would mean building
the Rust and arm64 stack this issue forbids as a dependency, from code
whose license does not allow it. Neither the licensing nor the integration
is cleared. Nobody has asked LTSearch's maintainers for a license; that is
a question for AutoForge's maintainer, if the decision is ever reopened.

**What would reopen it:**

- a license that permits redistribution, with the transitive graph
  audited;
- metadata and scope filters enforced inside the index;
- builds for the platforms AutoForge supports (x86_64 and arm64 Linux,
  Apple Silicon macOS);
- incremental updates visible within a search or two;
- a measured gain over this BM25 baseline from real embeddings, on a
  pinned set at least as adversarial as this one. Fixed embeddings are not
  such a gain.

Until then, an optional backend is a seam (`IndexBackend`) with a fallback
rule, not a dependency.

## What remains

- **Wiring into the projection.** Ordering or selecting what `memory.md`
  shows by a query would change what a launch sees. That needs a query
  source, the evaluation protocol's amendment rule (its section 11), and a
  decision on the Memory arm.
- **Agent-callable search and fetch.** A provider capability and ADR 0007's
  broker, as above.
- **Layer 3.** #325's repository-scoped store, and with it any persisted
  index, a D10 decision.
- **Semantic retrieval.** Embeddings stay out until they are consented to
  and configured, with their model, dimensionality, reproducibility,
  resource budget and provenance recorded, and no implicit credential,
  download or network request. None is designed.
