# Memory retrieval: the FTS5 read path and the LTSearch feasibility (#324)

Written after the implementation, for #324 (EPIC #331), at `main`
`5cad948`, and revised after five review rounds on PR #537. It records what
the read path decides and why, what was measured on one host, and the
go/no-go on LTSearch, on which the maintainer narrowed #324's acceptance to
the source-based no-go recorded here. ADR 0015 is the
authority this builds on: layer 4 of its four memory layers (D8), the
"later retriever" of D12, and an implementation note in D10 for the
SQLite connection this adds.

## What exists, and what does not

`src/autoforge/memory_retrieval.py` is a library. Given a launch's binding
(the `ProjectionRequest` the projection is rendered for) and a query, it
returns bounded hits (`search_memory`), one record whole (`fetch_memory`),
or a catalog of labels and counts (`catalog`). Each read loads the
canonical `MemoryStore` snapshot from the retriever's source and answers
from it; a search loads it again after its backend answers. Every answer
names the launch it is for.

Nothing in the package calls it yet. The projection a launch is shown is
still ADR 0015 D12's deterministic render, unchanged, and no agent can
search: the issue's live `search_memory` and `fetch_memory` would be a new
channel from an agent to the controller. That needs a provider capability
`AgentRequest` does not have (`src/autoforge/providers.py`), through ADR
0007's broker, bounded per launch, which nobody has designed or approved.
No configuration key, state field, protocol number, prompt, phase, effect
or default changes, so there is nothing to coordinate on #23.

## The decisions

### The store decides what is served; the index only finds candidates

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
projection's job. The index does not rank what a launch sees either: a
hit's place is computed over the launch's own records ("One ranking"
below).

A hit's excerpt is read from the store's live text, never from the index.
So a record corrected, retracted or superseded since the index last saw it
is either not served or served as it is now, however far behind the index
is.

Some candidates are rows no sync of this store could have written, at a
revision the index claims to have seen. That covers an id the issue does
not hold, a record retracted or superseded before the watermark (the sync
deleted its row), another revision or content digest, one record answered
with two revisions or digests, and a record whose text holds none of the
query's terms, since a search matches by term. A record changed after the
watermark keeps the row that sync wrote until the next one, and the search
drops it as stale, for the store's reason. It is stale only if it is the
record as the sync left it: added by then, at the revision and content
digest it had then (replayed from the audit table), and holding a query
term wherever the store still holds that text. A row of a record first
added after the watermark, or at a revision past it, is a row no such sync
wrote. An earlier revision skipped every candidate whose record had changed
since the watermark, so such a row passed as ordinary staleness. An answer
can also be wrong by what it leaves out: a record the launch is served,
unchanged since the watermark, whose text holds a query term, must be among
the candidates. Any one of these means the index is not a projection of the
store. The search discards that backend's whole answer, marks the index
`failed` (`integrity`) and answers the same call from the fallback, or from
the tail alone, reported `degraded`. The index stays `failed` until a sync
rebuilds it (`test_a_row_that_contradicts_its_revision_marks_the_index_failed_and_is_never_served`,
`test_a_contradicted_primary_answer_goes_to_the_fts5_fallback_on_the_same_call`,
`test_a_served_match_the_index_omits_falls_back_on_the_same_search`,
`test_one_record_answered_with_two_rows_is_an_integrity_failure`,
`test_a_row_no_sync_through_the_watermark_wrote_falls_back_on_the_search_that_meets_it`,
`test_an_optional_backend_answering_past_its_watermark_goes_to_the_fallback`,
`test_a_row_the_sync_left_is_stale_however_its_record_changed_since`).

Rank is never authority. A backend that answers with ids of another
issue's records, a retracted record or an id the store has never held gets
none of them served (`test_an_optional_backend_falls_back_only_to_fts5_and_never_across_permissions`).

### Every read loads the canonical snapshot

A retriever is bound to the store's source: a callable that returns the
canonical snapshot as it is now. For the controller that is
`AutoForgeState.memory`, the committed checkpoint's value reconstructed and
validated, as `prepare_launch_projection`'s `load_store` is. No read takes a
snapshot from its caller. `fetch_memory`, `catalog`, `sync`, `rebuild`,
`status` and `verify` each call the source once, and that snapshot is the
authority for everything the read answers. A retraction or a correction
made between a search and a fetch is what the fetch is judged by
(`test_every_read_loads_the_canonical_snapshot_so_a_change_between_search_and_fetch_counts`).

Every read first asks each backend whether it can run (`available`), and
only then loads the store. A backend's health check is a call into code the
retriever does not control, like its search, so it comes before the
snapshot that judges the read, never after it. Only `True` counts as
healthy. A check that raises a `RetrievalBackendError`, or answers anything
else, makes that backend unavailable for this read: the fallback or the
tail answers, `degraded`, and the next read asks again
(`test_a_backend_whose_health_check_fails_is_unavailable_and_the_fallback_answers`).

A search calls the source again after each backend answers, and binds the
request to that snapshot again. The snapshot loaded after the retrieval is
the one that judges the candidates, gives the excerpts, picks the hot tail
and reports the freshness, so one store decides all of them. A write made
while a backend answers is in force for that search: a record retracted
then is not served, and a record corrected then is served as the
correction left it, from the tail. A launch counted meanwhile makes the
request an earlier launch's, and the search is refused
(`test_a_retraction_while_the_index_answers_is_judged_by_the_store_after_it`,
`test_a_correction_while_the_index_answers_is_served_as_the_store_holds_it_after`,
`test_a_scripted_backend_that_retracts_before_it_answers_gets_nothing_served`,
`test_a_launch_counted_while_a_backend_answers_refuses_the_search`).
Nothing runs between that load and the answer but computation on the
snapshot, which is a value: no backend, no source, no write. The status a
search reports comes from the health it read before its first load, not
from a new check. An earlier revision asked the backend again for the
status, after the last load: a retraction made during that call left the
search serving the record, which the next fetch refused
(`test_no_backend_is_called_after_a_reads_last_load_of_the_store`,
`test_a_retraction_while_a_backend_reports_its_health_is_never_served`).

Reading a backend's attribute is a call too: a property, or a
`__getattribute__` of the backend's own, runs its code. So what a read
reports of a backend is captured when the retriever is made: its name, read
once and checked (lower-case words joined by hyphens, at most 32
characters, never `store-tail`), and whether it is FTS5, decided from its
type, since an instance check can read an object's own `__class__`. The
fourth review round found the status and the result still reading the name
after the last load: a backend whose name retracted a record had the search
and the fetch serve it. Now every read of a backend's attributes, for a call
or for its metadata, comes before the read's last load
(`test_a_retraction_while_a_backend_name_is_read_is_never_served`,
`test_no_attribute_of_a_backend_is_read_after_a_reads_last_load`,
`test_a_backend_name_is_checked_once_when_the_retriever_is_made`). The
guarantee covers what a read runs. Code a backend starts on its own, a
thread or a finalizer, runs outside any read's order.

The rejected alternative was a lock held across the retrieval. Nothing in
the controller holds one around a read, and a lock would only prove what
reloading shows directly.

When the source returns the very store object it returned last, the
retriever uses the snapshot it already holds. A store is never changed once
made, since a write returns a new one, and the retriever keeps a reference
to the one it read, so the object's identity cannot be reused for another.

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
The launch ordinal only grows too, and a launch adds no row, so the
retriever keeps the newest ordinal it has read as well. A checkpoint from
before a launch has the same rows and an earlier ordinal; it would bind
that launch's request again, after the launch had been counted. It is
refused alike
(`test_a_checkpoint_from_before_a_launch_is_refused_once_the_launch_was_read`).

Turning Memory off changes neither. `AutoForgeState.memory_setting` flips
the store's `enabled` and keeps its rows and the launch ordinal, so a
checkpoint from before an opt-out continues the opted-out one row for row,
at the same ordinal. The fifth review round found a retriever that refused
the opted-out snapshot (no launch binds with Memory off) and then read the
older one again and served its records. The retriever now keeps whether
Memory was on in the newest snapshot it read, and two snapshots are refused,
differently:

- **The opted-out snapshot itself**, Memory off, binds no launch, so
  search, fetch and catalog refuse it (`StateError`, from `bind_request`).
  The controller's own operations on the index, `status()`, `sync()`,
  `rebuild()` and `verify()`, still read it, since they serve no record:
  they report on the index and keep it, and the regression checks that
  they do.
- **A snapshot with Memory on that nothing proves newer than the
  opt-out.** After the retriever has read an opt-out, such a snapshot is
  read only with that proof: a row past the opt-out or a later launch
  ordinal, which no checkpoint from before the opt-out can have. Without
  it, every operation refuses it, the four above included
  (`RetrievalError`), until the controller calls
  `MemoryRetriever.rebind()`, once it has turned Memory on itself.

The controller's own opt-in gives back the very value the checkpoint from
before the opt-out held, so only the controller can tell the two apart. A
rebind still requires a snapshot that continues the newest one read, with
Memory on
(`test_a_checkpoint_from_before_an_opt_out_never_reopens_the_read_path`,
`test_a_snapshot_proven_newer_than_an_opt_out_is_read_without_a_rebind`).

A rollback of the checkpoint is therefore a new retriever's to read. That
means a new process's, since the index is in memory. A source that kept
returning one stale value from the start is beyond any check: the source is
the definition of current, which is why it is the controller's state and
not a value a caller hands in.

Loading costs what reconstructing the store costs: about 8.6 ms at the
fixture's 29 records and 21 ms at Memory's 64-record bound on this host,
and a search under a source that reconstructs it pays that twice (see the
cost table below). It is the same load the projection pays before every
Memory launch.

### One binding: the projection's

The read path binds a request exactly as the projection does
(`memory.bind_request`, factored out of `render_projection`): Memory on, the
store's run, an `ANALYZE_EXECUTE` step of the store's repository, the
snapshot's next launch ordinal, and each candidate, ancestor and environment
digest in bounds. A retriever is bound to one repository and run, and every
read refuses a snapshot of another. Inside a run, each issue is its own
FTS5 table, so no row of another issue can come back from a search
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
  scope's revision and a digest of what the launch is served, never the
  store's revision or digest. The digest covers the scope's revision and,
  for each record the launch is served, its add's and its latest change's
  place in the scope's sequence (not its run id) and its content digest,
  which a fetch gives. So it too is what a store that never had the writes
  outside the scope would give. A retracted, withheld or superseded record
  of the scope adds its changes to the revision and nothing else. An
  earlier revision put every scope record's content digest and verdict in
  it: the hash of text the store refuses to serve, which a reader could
  test a guess at that text against, and which refusal it was, which a
  fetch does not tell
  (`test_the_catalog_says_nothing_of_text_the_store_refuses_to_serve`).
- **Every sequence number a launch is told is the scope's own.** The
  scope's records' changes are numbered 1, 2, ... in the order the store
  made them. The scope's revision is how many there are. A hit's or a
  fetched record's revision is its latest change's number, and the index's
  watermark is how many of them it had seen when it was synced. The lag is
  the difference, and `pending` counts the records whose latest change the
  index has not seen. None of these is a position in the store's audit
  table. A write outside the scope, made between two writes in it, leaves no
  gap: every figure, the whole catalog, and every hit's place and score
  are what a store that never had that write would give
  (`test_writes_outside_the_scope_leave_no_gap_in_any_sequence_a_launch_is_told`).
  The store's own revision is reported only to the controller (`status`,
  `sync`).
- **A search** reports no drops and no backend number. A hit's place and
  score come from the one ranking below, over the records the launch is
  served, so a record it is not served moves neither
  (`test_records_the_launch_is_not_served_move_no_hit_place_or_score`).

Why each record was refused is counted in `RetrievalMetrics` (`dropped_*`),
the controller's in-process diagnostic, which no read returns.

A record's id is the one thing an answer carries that is not of the scope
alone. It is the run's id, the projection's `M<id>`: the place of the
record's add in the run's audit table, which a fetch needs. So, as in the
projection, two ids show how many of the run's records were added between
them. Numbering records per scope would give one record two ids, one in
`memory.md` and one here, which no citation (`Superseded by M12`) could
resolve. ADR 0015's projection, which this read path leaves as it is, also
counts the issue's inapplicable records in its header.

### One ranking, over the records the launch is served

A hit's place is Okapi BM25 (Robertson and others, "Okapi at TREC-3")
over the store's own text of the records the search found, with k1 = 1.2
and b = 0.75 (FTS5's constants). Its statistics are the launch's corpus:
every record the launch is served, and nothing else. N is how many records
that is, a term's n is how many of them hold it, and a record's length is
normalized by their mean length (avgdl). A term's IDF is the non-negative
form Lucene uses, ln(1 + (N − n + 0.5) / (n + 0.5)), rather than the
Robertson–Spärck Jones weight ln((N − n + 0.5) / (n + 0.5)), which is
negative for a term more than half the records hold. Ties go to the lower
id. The index's and the tail's records are scored by this one function,
over one corpus whichever of them found a record, so a record's place does
not depend on whether the index has synced it. With the tail on, a stale
index ranks every query of the pinned fixture exactly as the synced index
does.

The sixth review round found the lengths normalized by the mean of the
records the search found, not of the corpus, so a long record the query
does not match left the matches' order as it was. Of two records of one
word and of five, holding the term once and twice, the shorter ranks
first; a served record of 30 words that does not hold it raises the mean
from 3 to 12, and the longer then ranks first, as Okapi BM25 orders them.
The regression holds each score to the formula written out, through the
index and through the tail, and the first order again once the long record
is retracted
(`test_bm25_normalizes_lengths_by_every_record_the_launch_is_served`). The
corpus is at most Memory's 64 records, and a search at a synced index
already reads the text of every one the index has seen, for the coverage
check. On the pinned fixture the change moved one pair of places: in the
no-FTS5 condition, the third and fourth hits of one query traded places;
every figure of the table below is as it was.

*Rejected: FTS5's own `bm25()`.* Its statistics are over the issue's whole
table, which also holds the issue's records of other candidates and
environments, withheld duplicates, and, until the next sync, records
retracted since. Adding one record bound to an earlier candidate could turn
the one hit at k = 1 from one record of the launch to another. FTS5 cannot
restrict its statistics to a subset of rows. A table per applicability class would not help either: a launch's
scope is a union of classes (any candidate, its exact candidate, the
descendants of each ancestor, its environment), so no single table's
statistics are the launch's.

*Rejected: reciprocal rank fusion of the index's and the tail's lists.*
An earlier revision of this read path fused them (k = 60). With one function over both, a
record's place no longer depends on which list found it, and a backend's
own ranks and scores are no longer used for anything. They are still
checked, as below.

The cost is tokenizing each found record's text at every search, at most
64 records of at most 2,048 bytes each. The table below includes it.

### Freshness is reported, never assumed

The index is eventually consistent with the store. It records the Memory
revision it was synced through (`indexed_through_seq`). `IndexStatus`
reports the backend, its state (`empty`, `current`, `stale`, `failed`,
`unavailable`), its generation, both sequence numbers, the records whose
latest change the index has not seen (`pending`), consecutive sync failures
and a typed reason. For the controller (`status`, `sync`) the records are
the whole store's: `source_seq` is the store's revision. For a launch's
search, fetch or catalog they are the scope's, in the scope's sequence:
`source_seq` is how many changes its records have had,
`indexed_through_seq` how many of them the index has seen, and the index
is `current` when it has seen them all.

A search reads the scope's current records changed after the watermark on
the store itself. This is the hot tail: at most eight records, newest
first, matched on the same tokens and ranked with the index's by the one
function above. `SearchResult.complete` holds only when every current
record of the scope was searched: by an index synced through it whose
answer was checked to leave no matching record out ("Failures leave the
index where it was" below), or in the tail. Past the tail's bound,
`unsearched` counts what was not read. `degraded` says the primary backend
could not run or did not answer, or answered with what the store
contradicts or without a match it holds, and something else answered in
its place.

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

A rebuild asked for explicitly is one transaction too, and the index it
would replace stays in service until the new one commits. If it fails,
SQLite keeps the old rows, and the retriever keeps the old index's
watermark and generation, so searches still read it and are as complete as
before; the status reports the attempt (`failed` 1, `sync-failed`). An
earlier revision marked the index unbuilt before trying, so a failed
rebuild turned a healthy index `empty` at watermark 0, and searches fell
back to the eight-record tail. An index already found `failed` stays
unreadable through a failed rebuild. A rebuild asked for stays owed until
one commits, as does one asked for while the backend could not run: the
next sync makes it, even of an index still current, and only its commit
clears the failure. An earlier revision left that to the index's state, so
a sync of a current index after a failed rebuild did nothing, and `failed`
1 and `sync-failed` stayed until another rebuild was asked for
(`test_a_failed_rebuild_keeps_the_index_it_would_replace_in_service`,
`test_a_rebuild_that_failed_or_could_not_run_is_made_by_the_next_sync`).
The index lives in memory, so a crash loses it, and the next process
rebuilds it from the checkpoint. Given the same snapshot, the rebuild gives
the same rows, the same backend scores and the same answers
(`test_a_new_process_rebuilds_the_same_index_and_answers_identically`).
`verify` reads every row back and compares it with what a sync through the
watermark wrote, replayed from the store's audit table: one row for each
record indexable then, of its issue, at the revision and content digest it
had then, indexed by the tokens of that text, decoded from what FTS5 holds.
A record changed since keeps that row, stale, until the next sync. A row of
a record added after the watermark, at a revision past it or at a digest
the record did not have then, a missing or second row, and a row indexed by
other tokens are each an integrity failure. So is a read-back that is not
exactly a list of one row per record expected, which is checked before
anything iterates it, as a search's answer is
(`test_verify_checks_the_read_backs_container_and_size_before_it_iterates_it`).
The store keeps only the texts of live records, so the stale row of a
corrected or retracted record is checked by its revision and digest alone
(`test_verify_finds_a_row_no_sync_through_the_watermark_wrote`). `verify`
also runs FTS5's own integrity check, which proves FTS5's inverted index
matches the rows it holds. Once `verify` marks the index `failed`, searches
answer from the tail or the fallback until a sync rebuilds it
(`test_verify_finds_a_row_indexed_by_other_tokens_and_a_rebuild_restores_it`).

A search does not wait for `verify` to find a wrong row. A row whose text
was replaced so that it matches a term its record lacks is a candidate the
store contradicts. A row whose text was replaced so that it no longer
matches is a served match the answer omits. Each is found by the search
that meets it, which falls back on the same call
(`test_a_row_matched_by_a_term_its_text_lacks_falls_back_on_the_first_search`,
`test_a_served_match_the_index_omits_falls_back_on_the_same_search`).
To find omissions, a search reads the store's text of every record the
launch is served that the index has seen, at most 64, and checks that each
one holding a query term was answered. That is what lets it say `complete`:
the index's answer is checked against the store, not trusted. It costs
about 0.4 ms per search at Memory's 64-record bound (the cost table below).
Only records the launch is served are checked. Leaving out another
candidate's record changes nothing the launch is answered, and checking
those would let records outside the scope decide whether the launch's
search is `degraded`.

A row no sync could have written is a typed failure, never a Python
exception: a revision that is not an integer, a digest that is not text, or
a body holding a form `_fts5_token` never produces. Each is
`RetrievalIntegrityError`, or a served match left out. Searching marks the
index `failed` (`integrity`) and falls back on the same call; `verify`
reports the same
(`test_a_malformed_fts5_row_is_an_integrity_failure_never_an_exception`).

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
(`fts5-unavailable`). A search then looks for matches only in the hot
tail, which with no index is the scope's eight newest current records
(its ranking's statistics are still those of every record the launch is
served, as at every search). It is `degraded`, and
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

A backend's answer is checked before it is used. It must be exactly a
list or a tuple, never a subclass, of at most 64 candidates. Its type and
length are checked before anything iterates it, because a subclass's
`__len__` and `__iter__` are the backend's code: the fifth review round
found one that reported one candidate while it yielded 65, read whole with
its backend reported healthy, and one whose iteration raised a `ValueError`
that escaped the fallback
(`test_a_backend_answers_container_is_checked_before_anything_iterates_it`).
Each candidate must be exactly an `IndexCandidate` (not a subclass, whose
fields could be computed when read), with a positive integer id and
revision, a content digest, a finite score and a rank from 1 to 64. A
score is checked without converting it: an integer is compared with 2^53,
so `10**400` is refused rather than overflowing. Anything else is a backend
failure like a raised error (`invalid-answer`), with the same fallback:
another type, a rank of 0 or -60, a NaN score, a score past a float, a
bool, or 65 candidates. A record answered twice with different revisions
or digests is two rows of one record, an integrity failure, found before
anything is deduplicated, whichever row ranks first. Otherwise the
candidates are ordered by rank, then id, and the first of each id is kept,
so a backend that repeats a record fills one place with it
(`test_a_backend_repeating_a_candidate_fills_one_place`,
`test_a_malformed_backend_answer_is_a_backend_failure_and_falls_back`,
`test_one_record_answered_with_two_rows_is_an_integrity_failure`). Beyond
that check, a backend's scores and ranks are never used.

A backend must return every record of the partition that holds a query
term, up to 64. A store holds at most 64 records, so FTS5 always does, and
which records are candidates never depends on a backend's own ranking.
Each search checks this for the records the launch is served, as above: a
backend that cut its answer by its own ranking, or lost a row, is found by
the search it would mislead. The check is affordable because a store holds
at most 64 records; a larger corpus, such as #325's repository-wide layer,
would need another way to prove coverage before it could say `complete`. A
semantic backend, whose candidates need not hold a query term, would need a
contract of its own.

### An answer says whose it is

A search's, a fetch's and a catalog's answer carries a `ReadScope`: the
repository, the run, the issue, the launch ordinal, the candidate and the
environment digest of the binding `bind_request` checked. Every id in the
answer is the run's, and every revision and count is in the scope's
sequence, so they mean something only with it. Each hit and fetched record
carries its `Applicability`: its range as the typed `Range`, the candidate
an `exact` record applies to, the base a `descendants` record applies from,
its environment and its phases. With these, an answer handed on still says
why each record is the launch's
(`test_answers_carry_the_launch_binding_and_each_records_applicability`).
`render_hits` names the launch in its header and each record's
applicability on its line.

The binding's ancestors are not in the answer. The controller chooses them
(`prepare_launch_projection`) from the bases of every range-bound record of
the issue that the candidate descends from, before it is known which of
those records apply to the launch. A record of another environment bound to
a base's descendants therefore adds its base to them, and an earlier
revision repeated them in `ReadScope`, so the catalog named a base that
only an unserved record had. A served `descendants` record's own base, in
its `Applicability`, is all that reading it needs
(`test_the_projections_own_binding_tells_no_base_of_a_record_the_launch_is_not_served`).

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
comparison across hosts. Every figure below is from one full run after
the sixth review round's changes (the ranking's statistics over the whole
served corpus, the query bound checked before encoding), except two costs
labelled with the sessions they come from: the coverage check's (the third
round's) and the corpus statistics' (the sixth round's, run back to back
against the fifth round's code). The correctness figures are identical to
those measured after the third, fourth and fifth rounds: the new
statistics moved one pair of places, in the no-FTS5 condition, which no
figure shows. No embedding of any kind is computed, so nothing below is
evidence about semantic retrieval.

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
| a fresh retriever's rebuild from the checkpoint read back from its JSON, in the same process | 0.28 | 1.00 | 1.00 | 0.990 | 0 / 56 | 0 / 56 | 0 / 6 | 0 | 1 / 1 | 23 / 23 |

P@5 of 0.28 is this fixture's ceiling: 21 relevant judgments over 15
queries of five places each. The rebuild's rankings equal the synced
index's, and so do the stale index's with the tail on. The rebuild is
measured in the benchmark's own process: a second retriever, with a new
in-memory index, over a store decoded from the checkpoint's JSON. That
shows the rebuild is deterministic from the checkpoint alone, which is
what a new process would start from; no other interpreter is started, so
it is not an observed restart. An empty store gives no hit, `complete`.
The test holds the safety columns at zero in every condition. It also
holds R@5 and MRR at 1.00 wherever the search is complete, and the stale
and rebuilt rankings equal to the synced one. It does not hold the
quality figures of the degraded conditions, which only show what each
degradation costs.

Without the tail, the corrected record is not served at all until the next
sync, and every such search says it is incomplete. Without FTS5, recall
falls to what the eight newest records hold. Neither serves anything stale
or foreign.

### Cost on this host

| Corpus | Build | Search p50 / p95, snapshot held | Search p50 / p95, snapshot loaded per read | Index size |
|---|---|---|---|---|
| the fixture's store, 29 records (the whole read path) | 1.5 ms | 0.73 / 1.29 ms | 19.5 / 20.3 ms | 44 KiB |
| a store at Memory's bound, 64 records (the whole read path) | 3.0 ms | 1.27 / 1.79 ms | 46.1 / 47.7 ms | 76 KiB |
| 1,000 synthetic documents (the backend alone) | 27 ms | 0.22 / 0.50 ms | | 0.8 MiB |
| 10,000 synthetic documents (the backend alone) | 271 ms | 0.77 / 3.09 ms | | 7.3 MiB |
| 50,000 synthetic documents (the backend alone) | 1,412 ms | 2.23 / 10.4 ms | | 32.8 MiB |

Notes on the table:

- **Two sources.** "Snapshot held" is a source that returns the validated
  store object it already has, so the reload after the retrieval reuses the
  snapshot: the read path's own cost, the ranking included. "Loaded per
  read" is a source that reconstructs and validates the store from the
  checkpoint's value at every call, as `AutoForgeState.memory` does. A
  search calls it twice, before and after its backend answers. That
  reconstruction alone is 8.6 / 8.8 ms at 29 records and 21.0 / 21.8 ms
  at 64, so it dominates. It is Memory's own fold, the same load the
  projection pays before every launch. A caller that searches several
  times between two writes could keep the store object and hand back the
  same one; none exists yet.
- **Synthetic corpora.** Their documents are 40 Zipf-distributed
  pseudo-words each. No store reaches these sizes: they bound what a
  larger, repository-wide layer would cost the backend. A build tokenizes
  every document in Python (`text_tokens`) before FTS5 indexes it, which
  is most of the build time at these sizes.
- **The coverage check.** Measured in the third review round's session,
  not in this run: there the head before the check (`11f15bb`) searched
  in 0.63 / 1.17 ms on the fixture's store and 0.75 / 1.31 ms at the
  bound with the snapshot held. Reading the store's text of every served
  record the index has seen, to prove its answer left none out, added
  about 0.06 ms at 29 records and 0.37 ms at 64 at the median.
- **The corpus statistics.** Measured in the sixth review round's session,
  not in this run: two runs of the cost half each, back to back, of the
  fifth round's code and of this one, snapshot held. The fifth round's
  searched in 0.70 and 0.71 ms at the fixture's median and 1.15 and 1.17
  ms at the bound; this one in 0.72 and 0.73 ms and 1.23 ms in both. Every
  served record's lengths and terms, rather than only the found records',
  add about 0.02 ms at 29 records and 0.07 ms at 64 at the median: most
  of the text is already tokenized for the coverage check.
- **Updates.** Updating one document is 0.06 ms at the median at every
  size. The 95th percentile is 1.7 ms at 1,000 documents, 13.4 ms at
  10,000 and 31.6 ms at 50,000, where FTS5 merges its segments.
- **Where the time goes.** At the store's sizes, with the snapshot held,
  most of a search is Python: binding the request, the snapshot's verdict
  on every record, the scope's sequence, tokenizing the served records to
  check the index's coverage and for the ranking's statistics, and
  ranking the found ones. With a snapshot loaded per call, the two
  reconstructions and the digest of the audit rows that proves each
  snapshot continues the last one are most of it.
- **Memory and CPU.** The index's memory is its database pages, the sizes
  above. Python's own allocations during a 50,000-document build peaked
  at 24 KiB, a figure that excludes SQLite's heap. CPU time per search,
  snapshot held, is 0.8 ms on the fixture's store and 1.3 ms at the bound.
- **Ingestion to visibility.** A record written after the last sync is
  served at the next search, from the tail, in 1.4 ms. An incremental sync
  takes 0.1 ms, and the index then serves the record itself. Each is one
  sample.
- **Context.** Eight hits rendered by `render_hits` came to at most 1,917
  bytes (816 on average) on the fixture's queries, the launch's binding and
  each record's applicability included. That is at most 1,917 tokens for a
  byte-level tokenizer, against the projection's 16,384-byte budget.

Against the evaluation protocol's `memory_overhead` ceiling of 1,000 ms of
latency per launch, the read path at the store's bound costs under 1.8 ms
per search at p95 with the snapshot held, and about 48 ms at p95 loading it
twice per search.

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
| (c) no integration; borrow algorithms only | yes, and nothing is borrowed from LTSearch | The read path ranks by Okapi BM25 (Robertson and others, "Okapi at TREC-3") computed over the store's text, written from the published formula with FTS5's constants. An earlier revision fused its index's and tail's lists by reciprocal rank fusion (k = 60, Cormack, Clarke and Büttcher, SIGIR 2009), the method LTSearch's router uses; one ranking over both lists replaced it. No LTSearch code was copied or translated. |

**Decision: no-go for (a) and (b); FTS5 stays the default and only
backend.** The measurement half of the go/no-go is the native baseline
above. At Memory's scale of 64 records per run, BM25 serves every relevant
record of the fixture within five places, at under 1.7 ms per search
(about 47 ms loading the snapshot twice per search), with nothing stale or
foreign. That leaves no measured gap for a hybrid backend to close.

### What this report does not settle, and the maintainer's decision

#324's acceptance asked for a measured comparison and for licensing to be
confirmed, with the maintainers' clarification where a license is absent.
Three of those checks were not done, and the no-go above rests on the
source alone:

- **LTSearch was not measured.** Running it would mean building its Rust
  stack, and for real embeddings an arm64 Linux host with a downloaded
  model, which the issue forbids as a dependency, from code whose license
  does not permit copying it.
- **Its maintainers were not asked for a license.** Asking another project
  is for AutoForge's maintainer to do, and has not been done.
- **The licenses of its 680 locked crates were not audited.**

AutoForge's maintainer has decided this
([#324, 2026-11-23](https://github.com/iceboundrock/AutoForge/issues/324#issuecomment-7089522504)):
#324's LTSearch criterion is narrowed to this source-based no-go, and the
three checks are not required for it. They stay open, as limitations of
this report: they are neither done nor owed by #324, and leaving them out
of its scope clears nothing. LTSearch's licensing and its integration are
not cleared, and nothing here claims either. LTSearch stays out of the
package, its dependencies and its runtime until the conditions below are
met, and whoever reopens it starts with these three checks.

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

- **LTSearch's three open checks**, outside #324 by the maintainer's
  decision above; the first work of any reopening.
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
