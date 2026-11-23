# Memory retrieval: the FTS5 read path and the LTSearch feasibility (#324)

Written after the implementation, for #324 (EPIC #331), at `main`
`5cad948`, and revised after eight review rounds on PR #537. It records what
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

A search asks an index only about some records: those the launch is
served whose latest change is at or before the index's watermark. The
index saw each of them as it is now, so its row, if the index is a
projection of the store, holds the text the launch is served. FTS5's
query is filtered to those rows (`+rowid IN` the ids asked about), so
SQLite drops every other match before it reads a column of it or scores
it, and a candidate an optional backend gives for any other record is set
aside unread, counted for the controller (`unasked`). No other record's
row reaches a launch's search: not another candidate's, environment's or
issue's, not a retracted, withheld or superseded one's, and not the row of
a record corrected since the watermark, which holds its earlier text.
Records changed since the watermark are read by the tail, on the store
itself.

The answer about the records asked about is checked against the store
after the retrieval. A candidate must be the record as it is: at its
revision and content digest, and holding a query term in the store's text,
since a search matches by term. One record answered with two revisions or
digests is two rows of it. And none may be missing: each record asked about
whose text holds a query term must be among the candidates. Any of these
means the index is not a projection of the store. The search discards that
backend's whole answer, marks the index `failed` (`integrity`) and answers
the same call from the fallback, or from the tail alone, reported
`degraded`. The index stays `failed` until a sync rebuilds it
(`test_a_row_that_contradicts_its_revision_marks_the_index_failed_and_is_never_served`,
`test_a_contradicted_primary_answer_goes_to_the_fts5_fallback_on_the_same_call`,
`test_a_served_match_the_index_omits_falls_back_on_the_same_search`,
`test_one_record_answered_with_two_rows_is_an_integrity_failure`,
`test_the_same_damage_to_a_row_the_launch_is_served_falls_back_on_that_search`).

Until the eighth review round a search asked about every row of the
issue and judged each candidate it got. It tested the store's text of
any record of the issue the index returned, and a stale row against the
text it was indexed by, before it asked whether the launch is served that
record, and it took a malformed row the backend met while matching for a
failure. So the same damaged row, over the same served records, made a
search `current` or `failed` and `degraded`, and the next catalog said so,
by whether the text of a record bound to an earlier candidate held the
query's term: a channel, through the status and the fallback, to text the
launch cannot read. Now no row of a record the launch is not served, and
no row holding an earlier text, damaged or not, moves an answer, a status,
a flag or a catalog. The regression keeps everything the launch is
served the same, varies only that text (another candidate's record, one
superseded or retracted since the sync, the earlier text of one corrected
since), damages its row five ways (a body the query matches, a wrong
digest, a malformed revision, and a digest or a revision that is not
UTF-8, below), and compares every answer the launch could get, which must
be identical and not degraded
(`test_a_damaged_row_of_text_the_launch_cannot_read_moves_nothing_it_is_told`).
Those rows are the controller's to check: `verify` reads every row (below).
The filter is on rows. Damage to FTS5's own structures, its shadow tables,
can fail the match scan itself, whatever rows it is filtered to and by
which terms' postings it reads: that is FTS5's integrity check's to find,
which `verify` runs.

A record can also stop being served while the backend answers: retracted,
superseded or corrected inside the backend's call, after the search chose
the ids it asks about from the snapshot it loaded first. So the answer is
judged only once the store has been loaded after it, and only about the
records asked about that this store still serves as the index saw them.
A candidate of any other record asked about is set aside with its fields
unread, and counted as the store's refusal (`dropped_*`). For that to
hold, a backend answers each row it matched as the row holds it and never
raises for one: an error a search raises is the whole search's, whichever
row it met, and it is raised before anything can say whether the launch
is still served that row's record. Until the ninth review round, FTS5's
search raised for a row with a malformed column, and the retriever checked
every candidate it was asked about before it loaded the store again. So a
record served when the search asked about it, withdrawn inside the call,
whose row held a malformed revision, made the search and the catalog after
it `failed` and `degraded` when the query matched its text, and ordinary
`stale` when it did not. The regression withdraws the record inside the
call, before and after FTS5 reads its rows, three ways (retracted,
superseded, corrected), damages its row the same five ways, varies only
whether its text holds the query's term, and compares every answer,
which must be identical, `stale` and not degraded
(`test_a_damaged_row_of_a_record_withdrawn_while_the_index_answers_moves_nothing`).
The same damage to a record the store still serves falls back on that
search, with an FTS5 fallback and without one
(`test_damage_to_a_row_still_served_falls_back_on_the_search_that_withdrew_another`),
and an optional backend's malformed candidate of a record withdrawn
meanwhile is set aside while the same candidate of a served record is an
invalid answer
(`test_an_optional_backends_malformed_candidate_of_a_record_withdrawn_meanwhile_is_set_aside`).
`verify` still reads every row.

"As the row holds it" reaches down to how Python reads a value. SQLite
stores TEXT as the bytes it is given, and Python's `sqlite3` decodes every
TEXT value it fetches as UTF-8 and, by default, raises for one it cannot
decode, inside `fetchall()`: an error of the whole query, whichever row
held the value, before the retriever can tell whose row it was. Until the
tenth review round that error was FTS5's search failing, so a row of a
withdrawn record whose digest or revision held bytes that are not UTF-8
made the search that matched it `failed` (`search-failed`) and
`degraded`, and the catalog after it said so, while the same damage in a
row the query did not match left ordinary `stale`: the eighth and ninth
rounds' channel again, one level lower. The index's connection now decodes
each TEXT value alone, strictly, as `sqlite3` itself does, and one that is
not UTF-8 is an `UndecodableText` value of its own row, never an error of
the query. A candidate holding one is malformed like any other: set aside
unread when its record was withdrawn meanwhile, an integrity failure that
falls back on the same search when the store still serves it, and found
by `verify`'s read-back wherever it is. The regressions above damage a row
those two ways too (a digest and a revision that are not UTF-8), among
records the launch is not served, records withdrawn inside the call and
records still served
(`test_text_that_is_not_utf8_is_a_malformed_value_of_its_row_never_a_query_error`).

The rows a search no longer reads include those the fourth review round
found taken for ordinary staleness: a row of a record first added after
the watermark, or at a revision past it. A search never asks about such a
record, so the tail serves it as the store holds it, and `verify` finds
the row (`test_a_row_the_sync_left_is_stale_however_its_record_changed_since`,
`test_a_row_no_sync_through_the_watermark_wrote_is_read_by_no_search_and_verify_finds_it`,
`test_an_optional_backend_answering_past_its_watermark_is_set_aside`).

Rank is never authority. A backend that answers with ids of another
issue's records, a retracted record or an id the store has never held gets
none of them served, and none of them decides anything: they are not
records the search asked about
(`test_an_optional_backend_falls_back_only_to_fts5_and_never_across_permissions`).

### Every read loads the canonical snapshot

A retriever is bound to the store's source: a callable that returns the
canonical snapshot as it is now. For the controller that is
`AutoForgeState.memory`, the committed checkpoint's value reconstructed and
validated, as `prepare_launch_projection`'s `load_store` is. No read takes a
snapshot from its caller. `fetch_memory`, `catalog` and `status` each
call the source once, and that snapshot is the authority for everything
the read answers. `sync`, `rebuild` and `verify` call it before their
backend work, which indexes or checks that snapshot, and again after it,
for the freshness they report ("Freshness is reported" below). A
retraction or a correction made between a search and a fetch is what the
fetch is judged by
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
  It asks the index only about records the launch is served ("The store
  decides" above), so no row of any other record, damaged or not, moves
  its status, whether it is `complete` or `degraded`, or a later catalog
  (`test_a_damaged_row_of_text_the_launch_cannot_read_moves_nothing_it_is_told`).

Why each record was refused is counted in `RetrievalMetrics` (`dropped_*`),
the controller's in-process diagnostic, which no read returns. A fetch
counts every refusal. A search counts only the candidates the store refused
after the backend answered (a record retracted or corrected meanwhile),
since it asks about no other, and an optional backend's candidates for
records it was not asked about as `unasked`.

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

*Rejected: FTS5's own `bm25()`, for anything a launch is shown.*
`Fts5Backend.search` still computes it, and orders its bounded answer by
it (then by id); that answer holds every record asked about that a term
matches, so the order decides no candidate, and the retriever discards it
with the backend's scores and ranks. No hit's place or score comes from it.
Its statistics are over the issue's whole
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

A sync, a rebuild and a verify each call the backend after they load the
store, and the store may be written while the backend runs. The watermark
is the snapshot the update indexed. The status each returns is computed
against the store loaded again after the backend's work: once every
backend's update has returned, for a sync or a rebuild, and once the
backend has read its rows back and run its check, for a verify, whose rows
are still compared with the snapshot loaded before. Until the eighth
review round all three reported against the snapshot loaded before. A
record written during the update or the check was reported `current`,
source and index at revision 1 and nothing pending, while `status()` at
once said `stale`, source 2, index 1, one pending. Now each returns what
`status()` would, and the next sync indexes the write
(`test_a_write_while_maintenance_runs_the_backend_is_reported_pending`).

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
That holds whatever stops the batch. Until the fifteenth review round only
SQLite's own errors rolled it back: one Python raises binding a parameter,
such as `OverflowError` for an id past SQLite's 64-bit integers, escaped
with the transaction still open, so a search on the same connection read
the batch's earlier replacement instead of the original row. Now any error
rolls the batch back and is reported as the backend's typed failure, and
an interrupt is rolled back and raised as it is
(`test_an_update_python_cannot_bind_is_rolled_back_whole_and_the_next_one_commits`,
`test_an_interrupted_update_is_rolled_back_and_the_interrupt_raised_as_it_is`).
A store's own records never carry such an id; the bound is the backend's.

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
The index lives in memory, so a crash loses it, and the next process must
rebuild it from the checkpoint. The test holds that the rebuild depends on
the checkpoint alone, not that a restart works: two retrievers in one
process, the second over a store decoded from the first's checkpoint after
a JSON round trip and rebuilt from it alone, hold the same rows and give
the same backend scores and the same answers
(`test_a_retriever_rebuilt_from_a_json_round_tripped_checkpoint_answers_identically`).
No other interpreter is started, so no restart is observed.
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
(`test_verify_finds_a_row_no_sync_through_the_watermark_wrote`). Its
tokens cannot be compared, but their digest is still checked for its form.
Every row read back must be well formed before any is compared with the
store: a partition no longer than an issue URL (192 characters), an id and
a revision from 1 to the store's counter bound, and a content digest and a
token digest of 64 lower-case hex digits. Until the seventh review round
only the fields' types were checked, so a stale row whose text was gone
passed with `not-a-digest` as its token digest, taken for ordinary
staleness rather than an integrity failure. Any well-formed digest is all
a lost text lets `verify` check, so such a row stays stale
(`test_verify_rejects_a_malformed_stale_row_whose_text_the_store_no_longer_holds`).
`verify`
also runs FTS5's own integrity check, which proves FTS5's inverted index
matches the rows it holds. Once `verify` marks the index `failed`, searches
answer from the tail or the fallback until a sync rebuilds it
(`test_verify_finds_a_row_indexed_by_other_tokens_and_a_rebuild_restores_it`).

A search does not wait for `verify` to find a wrong row of a record the
launch is served. A row whose text was replaced so that it matches a term
its record lacks is a candidate the store contradicts. A row whose text was
replaced so that it no longer matches is a served match the answer omits.
Each is found by the search that meets it, which falls back on the same
call
(`test_a_row_matched_by_a_term_its_text_lacks_falls_back_on_the_first_search`,
`test_a_served_match_the_index_omits_falls_back_on_the_same_search`).
To find omissions, a search reads the store's text of every record it
asked the index about, at most 64, and checks that each one holding a
query term was answered. That is what lets it say `complete`: the index's
answer is checked against the store, not trusted. It costs about 0.4 ms per
search at Memory's 64-record bound (the cost table below). Only records the
launch is served are checked, and only their text is read, since those are
the only records a search asks about. A row of any other record is
`verify`'s alone: a search that judged one would let text outside what the
launch is served decide whether its search is `degraded` ("The store
decides" above).

A row no sync could have written is a typed failure, never a Python
exception: a revision that is not an integer, a digest that is not text,
a column holding TEXT that is not UTF-8 (read as an `UndecodableText`, not
raised inside `fetchall()`), or a body holding a form `_fts5_token` never
produces. FTS5's search answers such a row as it holds it, never
converted; a body no term matches is a served match left out. A search that meets such a row of a record the
store still serves as the index saw it marks the index `failed`
(`integrity`: an FTS5 candidate is its row) and falls back on the same
call, and one of a record withdrawn while the index answered decides
nothing (above). `verify` reads every row back, whatever record it is
of, and two layers check what it reads. `Fts5Backend.entries()`, the
read-back, raises `RetrievalIntegrityError` for a row it cannot read as an
entry: a column of another type than the index writes, TEXT that is not
UTF-8, or a body holding a form `_fts5_token` never produces. A row of the
right types it returns with its values as they are, whatever they hold:
an integer revision of `0` or past the store's counter, or a digest that
is not 64 lower-case hex digits. `MemoryRetriever.verify()` checks every
row it is given against those bounds before it compares any with the
store. Either way `verify()` returns a failed `IndexStatus`, reason
`integrity`, and raises nothing: it catches the error `entries()` raises.
So a caller of `verify` learns of a malformed row, like every other
integrity failure, from the status it returns, never from an exception:
handling exceptions alone misses it
(`test_a_malformed_fts5_row_is_an_integrity_failure_never_an_exception`,
`test_fts5_reads_back_a_row_of_its_types_whose_values_only_verify_refuses`).

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
fixture's 29 records and 3.2 ms at Memory's 64-record bound: the build
column of the cost table below, from the same run (the sixth round's run
gave 3.0 ms). A persisted index,
for #325's repository-wide layer for example, would need a decision of its
own in D10.

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

Its regression runs on every interpreter: with FTS5 simulated absent where
SQLite has it, and with the interpreter's own SQLite where it does not
(`test_without_fts5_only_the_bounded_tail_is_searched_and_the_result_says_so`).
Only the tests that read a real index skip where FTS5 is missing; until the
ninth review round a skip on the whole test module skipped this regression
too, on the very builds it covers.

### An optional backend falls back only to FTS5

A primary backend other than FTS5 (none ships) may be given a fallback,
and only a separate `Fts5Backend` can be one. The fallback is optional. A
search reads the primary when it is readable: built, not marked `failed`,
and available. When it is not, or when its answer is discarded, the search
reads the fallback, if one was given and it is readable itself. Otherwise
it answers from the bounded store tail alone, at most the scope's eight
newest current records, as without FTS5. An answer from the fallback or
the tail alone is `degraded` and names what answered. Whatever answers,
the same check of the store applies to every candidate.

The primary can be out of service in two ways, and they differ in what
brings it back:

- **Its search failed.** It raised a `RetrievalBackendError`
  (`search-failed`), whichever row it met. Or, judged by the store loaded
  after the answer, about the records that store still serves as the index
  saw them, it gave an answer the checks below refuse (`invalid-answer`),
  or one the store contradicts or one that omits a served match
  (`integrity`). The primary is marked `failed`, and no
  search reads it again until a sync or a rebuild, made while the backend
  can run, commits a new index of it. A sync that fails leaves it
  `failed`.
- **It is unavailable.** Its health check (`available`) answered anything
  but `True`, or raised a `RetrievalBackendError`. That makes it
  unavailable for that read only, and marks nothing. The next read asks
  again, and once the check answers `True`, searches read the primary's
  index as it was, at the same generation, with no rebuild. A sync skips a
  backend while it is unavailable, so its index may come back stale, which
  the tail covers as it does for any stale index.

The regressions hold both, with a fallback and without one
(`test_an_optional_backend_falls_back_only_to_fts5_and_never_across_permissions`,
`test_a_backend_whose_health_check_fails_is_unavailable_and_the_fallback_answers`,
`test_an_unavailable_primary_is_asked_again_and_a_failed_one_waits_for_a_rebuild`).

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
so `10**400` is refused rather than overflowing. Beyond its type and id, a
candidate is checked only when the store loaded after the answer still
serves its record as the index saw it: one of a record withdrawn while the
backend answered is set aside (above). Anything else is a backend
failure like a raised error (`invalid-answer`; `integrity` for FTS5, whose
candidates are its rows as it holds them), with the same fallback:
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

A backend is asked about at most 64 records, and must return every one of
them that holds a query term. A store holds at most 64 records, so FTS5
always does, and which records are candidates never depends on a backend's
own ranking. Each search checks this, as above: a backend that cut its
answer by its own ranking, or lost a row, is found by the search it would
mislead. A candidate for a record the search did not ask about is set
aside before any of its fields but its id is read, counted (`unasked`),
and decides nothing: neither a hit, nor a failure, nor a fallback. The
check is affordable because a store holds at most 64 records; a larger
corpus, such as #325's repository-wide layer, would need another way to
prove coverage before it could say `complete`. A semantic backend, whose
candidates need not hold a query term, would need a contract of its own.

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
64 characters, from a query of at most 512 bytes. A search checks that
bound once it has read every backend's health, loaded the store and bound
the request, as every read does first, and before it asks any backend. It
checks in two steps. A query of more than 512 characters is past the bound
whatever its encoding, since a character is at least one byte in UTF-8: it
is refused before anything of it is copied or encoded, so it costs those
reads and nothing more. A query of at most 512 characters is copied as its
exact `str` value and encoded, and refused if its UTF-8 is longer than 512
bytes: 257 copies of `é` are 257 characters and 514 bytes, refused at the
byte check. Neither refusal reaches a backend's search
(`test_a_query_past_the_byte_bound_is_refused_before_it_is_encoded`,
`test_no_backend_is_called_after_a_reads_last_load_of_the_store`).
Each term is quoted
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
comparison across hosts. Every figure below is from one full run on
`d8ab97a`, the code after the eighth review round's changes (a search asks
the index only about the records the launch is served; maintenance reports
freshness against the store after its backend work), except four costs
labelled with the sessions they come from: the coverage check's (the third
round's), the corpus statistics' (the sixth round's), the eighth
round's search filter, the ninth round's answer check and the tenth
round's decoding, each run back to back against the code before it. The
ninth and tenth rounds changed the search's code after that run; their
back-to-back runs (the cost notes below) show no change past the runs'
own spread, and the table is not run again.
The correctness table, every ranking included, is what the reviewed head
of the eighth round, `086aa34`, gives, run beside it, what `e16071d`
and `ecb5718` give in the ninth round's session, and what `9ac3a68` and
`94113e7` give in the tenth's; its figures are
those measured after the third to sixth rounds too (the sixth round's
statistics moved one pair of places in the no-FTS5 condition, which no
figure shows). No embedding of any kind is computed, so nothing below is
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
the fixture's declarations, not from `MemoryStore.visibility`. The oracle
replays the fixture's operations in order. A retraction and a correction
each withdraw their record's text as it is at that operation: the declared
text, or an earlier correction's. A withdrawal withholds another record of
the same issue that carries that very text, by an author no higher than
the withdrawn record's, and only when the withdrawn record's own scope
takes the benchmark's launch: retracting a copy bound to another candidate
leaves the launch's own record current. A hit is a
stale leak when the oracle judges its record retracted, withheld or
superseded, or when its summary is anything but the excerpt a hit shows of
the text the oracle judges current: that text redacted, then cut to 512
bytes with an omission marker. "Old text served" counts a probe search
with a hit that shows the earlier text whole or as its excerpt, except a
hit of a record the oracle judges current whose summary is exactly the
excerpt of its current text. That hit shows the record as it is now, which
may still hold the earlier text: a correction that only appends to it, or
a long text whose excerpt the correction left unchanged. Every other hit
showing the earlier text counts: the corrected record's earlier text or
its excerpt, and a withheld copy of it. "New text visible" counts a probe
whose new-text search has a hit of the corrected record that shows it as
it is now: a record the oracle judges current, whose summary is exactly
the excerpt of its current text. A hit of it that shows anything else,
its earlier text's excerpt included, shows no correction, and neither does
any hit of it while the oracle does not judge it current. Nor does a hit
whose excerpt any earlier text of the record shows too: the declared text,
or an earlier correction's. Finding the current record is not showing its
correction, and a correction that only changes what lies past the 512-byte
excerpt is not visible in any hit. Such a hit still shows the record as it
is now, so it is neither a stale leak nor old text served.

Until the fourteenth review round "new text visible" counted any hit of
the corrected record on the new-text search, whatever it showed: on the
pinned fixture, `m15`'s hit made to show its original excerpt on that
search still gave 1 of 1, while the stale-leak rate rose to 1 of 56
(`test_a_corrected_record_shown_with_other_text_is_no_new_text_visible`,
`test_a_corrected_record_the_oracle_does_not_judge_current_is_no_new_text_visible`).
Until the fifteenth it counted a hit showing the corrected text's excerpt
even where that excerpt was its earlier text's too. With `m15` lengthened
past 512 bytes and its correction appending a term past that bound, which
the new-text query asked for, it gave 1 of 1 in every condition that
served `m15` (synced, stale with the tail, without FTS5, rebuilt), though
each hit showed exactly the excerpt it had shown before the correction
(`test_a_correction_past_a_hits_bound_finds_its_record_but_shows_no_new_text`).
The pinned fixture's correctness report is unchanged by either, byte for
byte: its correction of `m15` rewrites the excerpt itself.

Until the thirteenth review round the oracle withdrew only retracted
texts, by their declared text and whatever their scope. Retracting an
old-candidate copy of `m10` made it count the current `m10` as a stale
leak (2 of 56 in the synced condition), and correcting `m10` beside a copy
of it left the copy current to the oracle, though the store withholds it,
so serving the copy would have gone uncounted
(`test_the_oracle_withholds_a_text_only_where_its_withdrawal_applies`,
`test_a_withheld_copy_of_a_corrected_text_is_judged_by_the_oracle_never_the_store`).
Old text served counted any hit holding the earlier text, so a
correction that only appended to `m10`'s text gave 1 in every condition
that served `m10`, with no stale leak
(`test_an_append_only_correction_shown_as_it_is_now_is_never_old_text`).
The pinned fixture has none of these cases, and its correctness report is
unchanged, byte for byte.

Until the twelfth review
round the stale-leak check and the probe judged a summary by whole texts,
so a correct excerpt of a current record longer than 512 bytes counted as
a stale leak: the fixture with only `m10` lengthened to 691 bytes gave
2 of 56 in the synced condition. The pinned fixture's longest record is
114 bytes, so its correctness report is unchanged, byte for byte
(`test_a_current_record_past_a_hits_bound_is_judged_by_its_excerpt_never_its_whole_text`,
`test_a_hit_past_a_hits_bound_showing_anything_but_its_excerpt_is_still_a_leak`). The report
names its input by the SHA-256 of the bytes it parsed (`fixture_sha256`),
so a run on any other input, one query changed or the same data written
out again, carries that input's digest, never the pinned one. Until the
eleventh review round the correctness half took a parsed fixture and
named it by the pinned file's digest whatever it held: one query changed
gave R@5 0.93 under the pinned digest
(`test_a_correctness_report_names_the_bytes_it_measured_never_the_pinned_fixture`).

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
| the fixture's store, 29 records (the whole read path) | 1.5 ms | 0.76 / 1.37 ms | 19.8 / 25.0 ms | 44 KiB |
| a store at Memory's bound, 64 records (the whole read path) | 3.2 ms | 1.42 / 1.98 ms | 48.2 / 52.8 ms | 76 KiB |
| 1,000 synthetic documents (the backend alone, 64 asked about) | 29 ms | 0.11 / 0.25 ms | | 0.8 MiB |
| 10,000 synthetic documents (the backend alone, 64 asked about) | 297 ms | 0.28 / 0.82 ms | | 7.3 MiB |
| 50,000 synthetic documents (the backend alone, 64 asked about) | 1,475 ms | 0.58 / 2.13 ms | | 32.8 MiB |

Notes on the table:

- **Two sources.** "Snapshot held" is a source that returns the validated
  store object it already has, so the reload after the retrieval reuses the
  snapshot: the read path's own cost, the ranking included. "Loaded per
  read" is a source that reconstructs and validates the store from the
  checkpoint's value at every call, as `AutoForgeState.memory` does. A
  search calls it twice, before and after its backend answers. That
  reconstruction alone is 8.7 / 9.3 ms at 29 records and 22.1 / 24.1 ms
  at 64, so it dominates. It is Memory's own fold, the same load the
  projection pays before every launch. A caller that searches several
  times between two writes could keep the store object and hand back the
  same one; none exists yet.
- **The store at the bound.** It is the fixture's 29 records, written
  without the fixture's four revisions (two retractions, a supersession
  and a correction), and 35 synthetic notes of 40 pseudo-words each, to
  reach 64. It is timed, not judged: the correctness table above is the
  fixture's alone.
- **Synthetic corpora.** Their documents are 40 Zipf-distributed
  pseudo-words each. No store reaches these sizes: they bound what a
  larger, repository-wide layer would cost the backend. Each search asks
  about 64 of the corpus's documents, drawn at random, as a launch's search
  asks about at most the records it is served. Until the eighth review
  round it searched the whole corpus for the 64 best-ranked matches, 0.77
  / 3.09 ms at 10,000 documents and 2.23 / 10.4 ms at 50,000, a different
  measurement. A build tokenizes every document in Python (`text_tokens`)
  before FTS5 indexes it, which is most of the build time at these sizes.
- **The coverage check.** Measured in the third review round's session,
  not in this run: there the head before the check (`11f15bb`) searched
  in 0.63 / 1.17 ms on the fixture's store and 0.75 / 1.31 ms at the
  bound with the snapshot held. Reading the store's text of every served
  record the index has seen, to prove its answer left none out, added
  about 0.06 ms at 29 records and 0.37 ms at 64 at the median.
- **The corpus statistics.** Measured in the sixth review round's session,
  not in this run: two runs of the cost half each, back to back, of the
  fifth round's code and of the sixth's, snapshot held. The fifth round's
  searched in 0.70 and 0.71 ms at the fixture's median and 1.15 and 1.17
  ms at the bound; the sixth's in 0.72 and 0.73 ms and 1.23 ms in both. Every
  served record's lengths and terms, rather than only the found records',
  add about 0.02 ms at 29 records and 0.07 ms at 64 at the median: most
  of the text is already tokenized for the coverage check.
- **The search filter.** Measured in the eighth review round's session,
  beside this run: two runs of the cost half each, back to back, of the
  reviewed head's code (`086aa34`) and of `d8ab97a`, snapshot held. The
  reviewed head searched in 0.74 and 0.72 ms at the fixture's median and
  1.23 ms in both at the bound; `d8ab97a` in 0.75 ms in both and 1.30 and
  1.28 ms. Asking the index about the served records alone costs about
  0.05 ms at the bound at the median. The full run's 1.42 ms at the bound
  is higher than either of those runs of the same code; the comparison is
  the back-to-back pair. The filter's first form, `rowid IN (...)`, made
  SQLite give FTS5 one rowid lookup per id, a full-text query each: a full
  run on `3aa3366` searched in 3.54 / 5.41 ms at the bound, and the backend
  alone in 3.47 / 10.9 ms at 10,000 documents. `+rowid IN (...)` keeps the
  filter out of FTS5's own lookups: one match scan, which SQLite filters
  before it computes a column or `bm25()` of a row.
- **Judging the answer after the store's second load.** Measured in the
  ninth review round's session: two runs of the cost half each, back to
  back, of the reviewed head's code (`e16071d`) and of `ecb5718`, whose
  module is the ninth round's, snapshot held. The reviewed head searched in
  0.73 and 0.76 ms at the fixture's median and 1.27 and 1.31 ms at the
  bound; `ecb5718` in 0.74 and 0.73 ms and 1.26 and 1.31 ms. Moving the
  check of a backend's answer after that load, and FTS5's row check into
  it, costs nothing these runs can tell from their own spread. The
  correctness half gives the same output at both, every ranking included.
- **Decoding each TEXT value alone.** Measured in the tenth review round's
  session: four runs of the benchmark each, alternating the reviewed
  head's code (`9ac3a68`) and `94113e7`, whose module is the tenth
  round's, snapshot held. The reviewed head searched in 0.73 to 0.79 ms
  at the fixture's median (0.76 on average) and 1.24 to 1.27 ms at the
  bound (1.26); `94113e7` in 0.76 to 0.82 ms (0.78) and 1.23 to 1.33 ms
  (1.28). A Python call per TEXT value a search reads, one digest per row
  it answers, costs about 0.02 ms at the median, inside the runs' own
  spread. The correctness half gives the same output in all eight runs.
- **Updates.** Each of a corpus's first twenty documents is updated once,
  timed alone. Updating one document is 0.07 ms at the median at every
  size. The 95th percentile is 1.8 ms at 1,000 documents, 14.6 ms at
  10,000 and 28.3 ms at 50,000, where FTS5 merges its segments. A corpus
  smaller than twenty documents updates each of its own once: until the
  tenth review round the benchmark indexed twenty whatever the size, and
  raised `IndexError` below it. `--sizes` takes 1 to 100,000 documents
  per corpus, the bound a backend holds, and refuses any other size when
  it parses its arguments, before either half runs. Every field of the
  list is a size, so an empty one (`10,,20`, `10,`, `,`) is refused too,
  and so is a list with no size: the cost half has no mode without a
  synthetic corpus. Until the eleventh review round an empty field was
  dropped, so `10,,20` ran as `10,20` and `,` exited successfully with no
  synthetic corpus measured
  (`test_the_benchmarks_cost_half_runs_on_a_small_corpus`,
  `test_the_benchmark_refuses_a_corpus_size_it_cannot_run_before_it_starts`,
  `test_the_benchmarks_cost_half_refuses_a_size_it_cannot_run_or_none`).
  The published sizes' twenty samples are unchanged.
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
  snapshot held, is 0.8 ms on the fixture's store and 1.4 ms at the bound.
- **Ingestion to visibility.** A record written after the last sync is
  served at the next search, from the tail, in 1.5 ms. An incremental sync
  takes 0.1 ms, and the index then serves the record itself. Each is one
  sample.
- **Context.** Eight hits rendered by `render_hits` came to at most 1,917
  bytes (816 on average) on the fixture's queries, the launch's binding and
  each record's applicability included. That is at most 1,917 tokens for a
  byte-level tokenizer, against the projection's 16,384-byte budget.

Against the evaluation protocol's `memory_overhead` ceiling of 1,000 ms of
latency per launch, the read path at the store's bound costs under 2.0 ms
per search at p95 with the snapshot held, and about 53 ms at p95 loading it
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
backend.** The no-go for each rests on what LTSearch's source shows, the
reasons in the table. The measurement half of the go/no-go is the native
baseline above, and its two results come from two different corpora:

- **Quality and safety: the judged fixture, 29 records.** BM25 serves
  every judged relevant record within five places (R@5 = MRR = 1.00) with
  the index synced, stale with the tail, or rebuilt. No condition served
  anything stale or foreign, and none served the corrected-away text. On
  this fixture, that leaves no measured gap for a hybrid backend to close.
- **Cost: a store at Memory's bound, 64 records.** In the run above, a
  search takes 1.42 ms at p50 and 1.98 ms at p95 when the source returns
  the store object it holds. It takes 48.2 ms at p50 and 52.8 ms at p95
  when the source reconstructs the store at every call, as
  `AutoForgeState.memory` does, which a search calls twice. The sixth
  round's run gave 1.27 / 1.79 ms and 46.1 / 47.7 ms.

The 64-record store is a timing corpus, not a judged one (the notes on
the cost table). The benchmark measures neither its quality nor its
safety. A check in the seventh review round's session scored it by the
fixture's judgments anyway: R@5 0.98 and MRR 0.93. Two records that the
fixture retracts or supersedes are current in this store, and each ranks
first for one query; one of those queries' judged relevant records falls
past five places. With the fixture's four revisions applied to the same
store, the same check gave 1.00 for both, so the miss comes from the
revisions the corpus leaves out, not from its size. Neither check is part
of the benchmark or held by a test, and neither counted stale or foreign
hits. Retrieval quality and the absence of stale or foreign hits are
established at 29 records only, and at 64 only the cost.

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
