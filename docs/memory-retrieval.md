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
or a catalog of labels and counts (`catalog`). Each read loads the
canonical `MemoryStore` snapshot from the retriever's source and answers
from that snapshot alone.

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

An index holds a copy of records' tokens, never their authority. A backend
answers with candidates: a record id, and the revision and content digest
it indexed. Only `MemoryRetriever` turns a candidate into a `MemoryHit`,
and only after the snapshot's verdict (`MemoryStore.visibilities`) says the
record is `current` for that launch. That verdict is the projection's own
check, in its order: another issue's record is `foreign`; one whose
phases, candidate range or environment do not take the launch is
`inapplicable`; then `retracted`, `withheld` (a withdrawal of the very
text it carries) and `superseded`. A superseded record is never served,
even where its citation establishes a claim: resolving citations stays the
projection's job.

A hit's excerpt is read from the store's live text, never from the index.
So a record corrected, retracted or superseded since the index last saw it
is either not served or served as it is now, however far behind the index
is. A candidate whose revision or digest the store contradicts, at a
revision the index claims to have seen, is never served, and it marks the
index `failed`.

Rank is never authority. A backend that answers with ids of another
issue's records, a retracted record or an id the store has never held gets
none of them served (`test_an_optional_backend_falls_back_only_to_fts5_and_never_across_permissions`).

### Every read loads the canonical snapshot

A retriever is bound to the store's source: a callable that returns the
canonical snapshot as it is now. For the controller that is
`AutoForgeState.memory`, the committed checkpoint's value reconstructed and
validated, as `prepare_launch_projection`'s `load_store` is. No read takes a
snapshot from its caller. `search_memory`, `fetch_memory`, `catalog`,
`sync`, `rebuild`, `status` and `verify` each call the source once, and
that snapshot is the authority for everything the read answers: its
candidates' check, its excerpts and its freshness. A retraction or a
correction made between a search and a fetch is what the fetch is judged by
(`test_every_read_loads_the_canonical_snapshot_so_a_change_between_search_and_fetch_counts`).

The retriever also proves the snapshot is the newest one. The store's
audit table only grows, so a write's snapshot continues the one before it,
row for row. The retriever keeps the revision of the newest snapshot it
has read and the digest of its rows. A snapshot with fewer rows, or whose
rows up to that revision differ, is refused by every read
(`RetrievalError`). That covers an older checkpoint, a copy kept from
before a write, or another history. Without this check, such a snapshot
could still serve a record a later write retracted, including one whose
revision is exactly the index's watermark, since the index was synced
through it
(`test_an_older_snapshot_is_refused_even_where_the_index_watermark_matches_it`).
A rollback of the checkpoint is therefore a new retriever's to read. That
means a new process's, since the index is in memory. A source that kept
returning one stale value from the start is beyond any check: the source is
the definition of current, which is why it is the controller's state and
not a value a caller hands in.

Loading costs what reconstructing the store costs: about 9 ms at the
fixture's 29 records and 21 ms at Memory's 64-record bound on this host
(see the cost table below). That is the same load the projection pays before
every Memory launch.

### One binding: the projection's

The read path binds a request exactly as the projection does
(`memory.bind_request`, factored out of `render_projection`): Memory on, the
store's run, an `ANALYZE_EXECUTE` step of the store's repository, the
snapshot's next launch ordinal, and each candidate, ancestor and environment
digest in bounds. A retriever is bound to one repository and run, and every
read refuses a snapshot of another. Inside a run, each issue is its own
FTS5 table, so one issue's scores do not depend on another issue's records,
and no row of another issue can come back from a search
(`test_partitions_keep_one_issues_scores_and_rows_apart`).

### A launch is told only of its scope

A launch's scope is its issue's records that apply to it: neither
`foreign` nor `inapplicable`. What the read path tells a launch is computed
from that scope alone, so adding, correcting or retracting another issue's,
another candidate's or another environment's record changes no answer the
launch gets, before or after the index syncs it
(`test_records_outside_the_scope_move_nothing_a_launch_is_told`).

- **Fetch** gives one answer, `item` `None`, for every record it does not
  serve: an id that never existed, another issue's or another candidate's
  record, and a retracted, withheld or superseded one are
  indistinguishable (`test_fetch_gives_one_answer_for_every_record_it_does_not_serve`).
- **The catalog** counts the scope's current records by kind. It names the
  scope's revision (the latest revision of a record of the scope) and a
  digest of the scope's records, never the store's revision or digest.
- **Freshness** shown to a launch is over the scope's records too
  (`IndexStatus` below): its revision, watermark, pending count, and a
  search's `unsearched` and `complete`.
- **A search** reports no drops. A hit's `rank` is its place among the
  records its list served, so a candidate refused before it leaves no gap.
  The backend's own score is not reported, since BM25's statistics are over
  the issue's whole partition.

Why each record was refused is counted in `RetrievalMetrics` (`dropped_*`),
the controller's in-process diagnostic, which no read returns.

One effect remains, documented rather than removed. The order of a
launch's hits is BM25's over its issue's partition, and the partition also
holds the issue's records of other candidates and environments. Their term
statistics can reorder two hits of the scope, but never add, remove or count
one. Removing the effect would mean an index per applicability class, or
scoring outside FTS5. ADR 0015's projection, which this read path leaves
as it is, still counts the issue's inapplicable records in its header.

### Freshness is reported, never assumed

The index is eventually consistent with the store. It records the Memory
revision it was synced through (`indexed_through_seq`). `IndexStatus`
reports the backend, its state (`empty`, `current`, `stale`, `failed`,
`unavailable`), its generation, both sequence numbers, the records whose
latest change the index has not seen (`pending`), consecutive sync failures
and a typed reason. For the controller (`status`, `sync`) the records are
the whole store's: `source_seq` is the store's revision. For a launch's
search, fetch or catalog they are the scope's. There `source_seq` is the
latest revision of a record of the scope, `indexed_through_seq` the latest
of those the index has seen, and the index is `current` when it has seen
them all.

A search reads the scope's current records changed after the watermark on
the store itself. This is the hot tail: at most eight records, newest
first, matched on the same tokens. Its hits are fused with the index's by
reciprocal rank fusion. `SearchResult.complete` holds only when every
current record of the scope was searched, by an index synced through it or
in the tail, and no index row contradicted the store. Past the tail's
bound, `unsearched` counts what was not read. `degraded` says the primary
backend did not answer.

A fetch reads no index, but it reports the same freshness: the status, over
the scope at the fetch's snapshot, of the index a search would read now,
and whether that is not the primary (`degraded`). Index lag, a failed or
unavailable primary, a fallback in its place and a store written since the
last sync are each reported
(`test_fetch_reports_the_index_freshness_of_its_scope`,
`test_fetch_reports_a_failed_backend_and_its_fallback`). The catalog
carries the same status and flag.

### Failures leave the index where it was

A sync is one SQLite transaction: every upsert and delete, or none. A
failure mid-batch publishes nothing and keeps the old watermark, and the
store still refuses anything stale in it. So a failed or partial build
cannot reopen a fact retracted since
(`test_a_retracted_record_is_unservable_at_once_and_a_failed_sync_never_reopens_it`).
The index lives in memory, so a crash loses it, and the next process
rebuilds it from the checkpoint. Given the same snapshot, the rebuild gives
the same rows, the same backend scores and the same answers
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

The cost is a rebuild per process. On this host that is 1.5 ms for the
fixture's 29 records and 3.1 ms at Memory's 64-record bound. A persisted
index, for #325's repository-wide layer for example, would need a decision
of its own in D10.

### A SQLite without FTS5 is a documented, limited fallback

FTS5 is a compile-time option of the SQLite library an interpreter links,
so a supported interpreter may lack it (ADR 0015 D10). `fts5_available()`
probes for it. Without it, the backend reports `unavailable`
(`fts5-unavailable`). A search then reads only the hot tail, which with no
index is the scope's eight newest current records. It is `degraded`, and
`complete` only when the scope has no more current records than that. The
excerpts still pass the store's check. Nothing broader is ever read, and no
launch fails for it, since none uses retrieval. `doctor` has no row for it,
because nothing yet depends on the answer.

### An optional backend falls back only to FTS5

A primary backend other than FTS5 (none ships) may be configured only with
an `Fts5Backend` as its fallback. When the primary cannot answer, the
fallback does: the result is `degraded` and names the backend that
answered, and the primary is rebuilt by the next sync. Either way, the
same check of the store applies to every candidate.

A backend's answer is checked before it is used. It must be a list of at
most 64 candidates, each with a positive integer id and revision, a content
digest, a finite score and a rank from 1 to 64. Anything else is a backend
failure like a raised error (`invalid-answer`), with the same fallback:
another type, a rank of 0 or -60, a NaN score, or 65 candidates. The
candidates are ordered by rank, then id, and the first of each id is kept,
so a backend that repeats a record fills one place with it
(`test_a_backend_repeating_a_candidate_fills_one_place`,
`test_a_malformed_backend_answer_is_a_backend_failure_and_falls_back`).
Fusion ranks each list by the places of its served records, never by a
backend's own numbers.

### One tokenizer, in every script

The index, the hot tail and a query are tokenized by one function,
`text_tokens`. It applies compatibility decomposition (NFKD) and case
folding, then removes every combining mark with a nonzero combining class:
`é` becomes `e`, and Arabic's vowel signs and Devanagari's virama go. A
token is a run of letters, digits and the marks that remain, such as a
Devanagari vowel sign, which stays in its word. Every other character,
the underscore included, separates tokens. No script is segmented further,
so a run of CJK ideographs is one token, as it is to FTS5's own tokenizers.

FTS5 does no tokenizing of its own here. It is given each token as one
ASCII word: the token itself if it is lower-case ASCII letters and digits
not starting with `x`, otherwise `x` and the token's UTF-8 in hex. Under
FTS5's `ascii` tokenizer, which then only splits at the spaces, the mapping
is one to one, so BM25 counts the tokens' own frequencies.

*Rejected: FTS5's `unicode61` tokenizer, with a Python fold for the tail
and the query.* `unicode61` keeps Hangul syllables composed where NFD splits them into
jamo, and treats Arabic and Devanagari marks as separators, so no Python
fold matches it in every script. A Korean, Arabic or Hindi record found in
the tail was missed once the index synced it, and the search still said
`complete`. With one tokenizer, the index, the tail and a rebuild find the
same records in Korean, Arabic, Hindi, Chinese, Greek, Cyrillic,
Vietnamese, accented Latin and full-width forms
(`test_non_latin_text_is_found_alike_by_the_index_the_tail_and_a_rebuild`).

### Untrusted text stays data

A query becomes its tokens only: deduplicated, at most 16 terms of at most
64 characters, from a query of at most 512 bytes. Each term is quoted
before it reaches FTS5, so no query is FTS5 syntax, a column filter or a
scope. `render_hits` fences every excerpt with a backtick run longer than
any inside it. A record that says "ignore previous instructions" or closes
a fence stays inside its block
(`test_query_syntax_never_reaches_fts5_and_indexed_text_stays_fenced`).
`RetrievalMetrics` keeps a fixed set of capped counters: searches, hits,
bytes served, backend failures and drops by reason. It never holds a query,
a record's text or a prompt.

### Bounds

| What | Bound |
|---|---|
| hits per search (`k`) | 1 to 16, default 8 |
| one hit's excerpt | 512 bytes, cut with an omission marker |
| a search's excerpts together | 4,096 bytes (a conservative bound on tokens, as the projection's is) |
| records read in the hot tail | 8 |
| candidates one backend answer holds | 64 (every record a store can hold); more is a backend failure |
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

| Corpus | Build | Search p50 / p95, snapshot held | Search p50 / p95, snapshot loaded per read | Index size |
|---|---|---|---|---|
| the fixture's store, 29 records (the whole read path) | 1.5 ms | 1.09 / 1.60 ms | 9.80 / 10.4 ms | 44 KiB |
| a store at Memory's bound, 64 records (the whole read path) | 3.1 ms | 1.81 / 2.28 ms | 22.8 / 23.8 ms | 76 KiB |
| 1,000 synthetic documents (the backend alone) | 27 ms | 0.22 / 0.52 ms | | 0.8 MiB |
| 10,000 synthetic documents (the backend alone) | 271 ms | 0.81 / 3.19 ms | | 7.3 MiB |
| 50,000 synthetic documents (the backend alone) | 1,479 ms | 2.30 / 10.4 ms | | 32.8 MiB |

Notes on the table:

- **Two sources.** "Snapshot held" is a source that returns the validated
  store it already has: the read path's own cost. "Loaded per read" is a
  source that reconstructs and validates the store from the checkpoint's
  value at every read, as `AutoForgeState.memory` does. That
  reconstruction alone is 8.7 / 9.3 ms at 29 records and 21.0 / 23.0 ms at
  64, so it dominates. It is Memory's own fold, the same load the
  projection pays before every launch. A caller that searches several
  times between two writes could keep the snapshot and hand back the same
  one, which passes the continuity check; none exists yet.
- **Synthetic corpora.** Their documents are 40 Zipf-distributed
  pseudo-words each. No store reaches these sizes: they bound what a
  larger, repository-wide layer would cost the backend. A build tokenizes
  every document in Python (`text_tokens`) before FTS5 indexes it, which
  is most of the build time at these sizes.
- **Updates.** Updating one document is 0.06 ms at the median at every
  size. The 95th percentile is 1.7 ms at 1,000 documents, 13.7 ms at
  10,000 and 29.0 ms at 50,000, where FTS5 merges its segments.
- **Where the time goes.** At the store's sizes, with the snapshot held,
  most of a search is Python: binding the request, the snapshot's verdict
  on every record, and the digest of the audit rows that proves the
  snapshot continues the last one.
- **Memory and CPU.** The index's memory is its database pages, the sizes
  above. Python's own allocations during a 50,000-document build peaked
  at 24 KiB, a figure that excludes SQLite's heap. CPU time per search,
  snapshot held, is 1.2 ms on the fixture's store and 1.9 ms at the bound.
- **Ingestion to visibility.** A record written after the last sync is
  served at the next search, from the tail, in 1.2 ms. An incremental sync
  takes 0.6 ms, and the index then serves the record itself.
- **Context.** Eight hits rendered by `render_hits` came to at most 1,555
  bytes (612 on average) on the fixture's queries. That is at most 1,555
  tokens for a byte-level tokenizer, against the projection's 16,384-byte
  budget.

Against the evaluation protocol's `memory_overhead` ceiling of 1,000 ms of
latency per launch, the read path at the store's bound costs under 3 ms per
search at p95 with the snapshot held, and under 25 ms loading it per read.

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
record of the fixture within five places, at under 3 ms per search (under
25 ms loading the snapshot at every read), with nothing stale or foreign. That leaves no measured gap for a hybrid backend
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
