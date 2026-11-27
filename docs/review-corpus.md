# Review corpus and offline reviewer benchmark (REV-R1)

**Status:** corpus format version 1, its checker, git verifier and scorer, a synthetic fixture, and
an unadjudicated seed of eight real candidates from this repository (#356). **No label is
adjudicated, no reviewer has been run on any case, and no benchmark result exists.** Nothing here
changes a prompt, a default, the state file, a controller decision or a GitHub effect.

[REV-R0](review-evaluation-protocol.md) replays the controller's REVIEW/FIX loop to judge a change
to that loop. Comparing reviewers (another model, prompt or effort) needs something REV-R0 cannot
give: the same candidates for every reviewer, each with defects known independently of any
reviewer, shown as they stood when they were reviewed. This page defines that corpus and how a
reviewer's findings on it are scored. It keeps REV-R0's label meanings, severity weights
(critical 8, high 4, medium 2, low 1), defect classes and exclusion reasons, and leaves every real
run to [#318's harness rules](evaluation-protocol.md).

## 1. A case

A case is one candidate a reviewer is asked to review, pinned so that anyone holding the
repository's objects can rebuild exactly what the reviewer was given:

- **Review binding:** base ref, base revision (the base ref's commit the review read), merge base,
  head, the head's tree, and the review-time cutoff (a UTC instant). The reviewer reads the diff
  from the merge base to the head. The corpus pins each base ref's tip at the freeze
  (`repository.refs`), whose first-parent line the base revision is on.
- **Environment:** the corpus declares its repository's test environment (`repository.environment`):
  the files that define it and the validation commands a runner runs (the seed's are
  `pyproject.toml` and `uv.lock`, and `make check`). A historical corpus does not choose it: it
  must declare exactly what `ENVIRONMENTS` in `tests/review_corpus.py` registers for its
  repository's slug and root commit, a declaration in reviewed source, independent of every case's
  pins. A corpus declaring anything else for a registered repository is refused, and no case of a
  repository that is not registered is runnable. So an existing but unrelated file (a source file,
  say), declared as the whole environment and pinned truly at every head, cannot stand in for the
  real environment files, and a case cannot validate with commands of its own. Each case pins
  every declared file and no other, each as its blob id at the head or as `null` when the head has
  no such file, and validates with exactly the declared commands. Leaving a pin out is never how a
  case says it has none. A case none of whose declared files is at its head pins no environment at
  all and is never runnable, so a declared path that no head has, pinned `null`, cannot stand in for
  the real pins; `null` stays for a declared file one head lacks beside others it has. Version 1
  has no exception for a candidate that genuinely has no environment file: such a case stays
  unrunnable. A repository with no validation command cannot be expressed in version 1.
- **Context:** what the reviewer reads besides the diff (the issue, the PR body, issue and PR
  comments, documents), and an **inventory** of it. Each item names its stable **source**:
  `issue/<n>` and `pr/<n>` for the issue and PR body, `issue/<n>/comment/<id>` and
  `pr/<n>/comment/<id>` for a comment on them, `document/<path>` for anything else (another issue,
  a file). The source fixes the item's kind, and `<n>` must be the issue or PR the provenance
  names. Each item is either captured, as text with its SHA-256 and the instant it reached that
  state, or explicitly `not_captured`; an item whose source nobody established (a PR no record
  numbers) has none and can only be `not_captured`. The inventory is either `complete`, listing by
  source every item the reviewer was given at the cutoff and saying when and from what that listing
  was taken, or `not_captured`.
- **Retained records**, each kept apart from the corpus, named by a stable source (a comment on the
  case's own PR, or a document), held as its text and SHA-256 or as `not_captured`, and never part
  of the reviewer's input (section 2): a `pr_branch` case's **review record** of its historical
  round; a `squash` case's **merge record**, naming the commit its PR merged as; and, in each
  defect's discovery that names a fix or revert, its **lineage record**, naming the defect (its
  id, and the summary, round and finding that tell it from another), how it was found, that fix
  and the squash the defect escaped through, if any. Each record is one case's or one defect's: no
  source holds two records, no record is held under two sources, and no review or merge record
  serves two cases, nor a lineage record two defects.
- **Defects** of a defect case, each located in at least one file, by its lines or as the whole
  file, or the **negative** record of a negative case (sections 3 and 4). A defect's id is its
  identity across the corpus: one defect may be registered at several heads of its PR under one
  id (its lines and its `same_review` or `later_review` source being each head's own), and
  registrations of one id along one lineage (one PR, introduction, set of paths, way of being
  found, merge and fix) must be one defect, of one severity, class and summary, found by one
  round's finding on one day. Another defect under that id is refused, with a lineage record of its
  own or without one.
- **Withheld commits:** commits that would reveal an answer (the fixes, later rounds, the later
  merge). They are never part of the reviewer's input.
- **Actors:** who produced, reviewed, fixed or merged the candidate. None may adjudicate it.
- **Risk stratum:** REV-R0's routine, elevated or critical surface, decided from the diff before
  any outcome is known, with its rationale.
- **Exposure evidence:** the first day anything of the case's answer could have been public, and a
  day by which part of it already was when that precedes what the case shows, each a day or
  `null`, with the artifact that evidences them (section 2).
- **Cluster:** the unit resampled together. Cases of one issue share code and history, so treating
  them as independent would overstate the evidence: cases of one issue, of one PR, or registering a
  defect of one commit must share a cluster, and the corpus check refuses any that do not. A
  cluster may hold several issues the curator judges related.
- **Provenance:** `pr_branch` (a review round's exact head on a PR branch), `squash` (a squash
  commit on main) or `synthetic`; the issue and PR as git states them; the round; the selection
  rule; a note on what is not known.

| Stratum | A case of it | Needs a defect discovered |
|---|---|---|
| `reviewed_defect` | a head a review round read, holding defects some review found | `same_review` or `later_review` |
| `escape` | a candidate whose defect was found only after merge | `post_merge` |
| `revert` | a candidate later reverted for a defect | `revert` |
| `hard_negative` | a candidate on a defect case's surface, with no registered defect | none registered |
| `clean` | a candidate with no registered defect | none registered |

`same_review` means the case's own historical reviewer reported the defect at that head;
`later_review`, that a later round reported it while it was already present there.

## 2. Cutoff, look-ahead and leakage

A historical benchmark is only as good as its separation of what the reviewer could have known
from what was learned afterwards. These rules hold that line; each is enforced where it says.

- A captured context item was observed at or before the cutoff (inclusive) and its digest matches
  its text. Nothing the reviewer is given names a withheld commit: no free-text value of its input
  (the base ref, the environment's paths and validation commands, and every context item's id,
  kind, source, reference and text, captured or not) holds a hexadecimal run of seven or more
  digits that contains a withheld commit's first seven, whether as an abbreviation, the whole id or
  inside a longer run. The corpus check refuses any other.
- A case holds its issue when its provenance names one, and its PR body when its candidate came
  from a PR (every `pr_branch` and `squash` case), captured or not, and at most one of each. No two
  items share a source. A complete inventory was taken at or after the cutoff and by the freeze,
  lists each source once, and lists exactly the sources of the items the case holds, so every item
  must have one. The corpus check refuses any other, so leaving an item out, or holding another
  copy of a comment in place of one the listing names, makes a case refused, never runnable. The
  check cannot tell a copy given the right source; reading each source again at capture is #592's.
- A case is **runnable** only when its inventory is complete, every item it holds is captured, its
  repository's environment is registered (section 1), at least one of its environment pins is a
  file at its head, and every record it needs is captured: a `pr_branch` case's review record, a
  `squash` case's merge record, and the lineage record of each defect that names a fix or revert.
  In a historical corpus every defect found after its merge (`post_merge` or `revert`) must also
  name the commit that fixed or reverted it and hold that commit's lineage record: a review's
  defect is found by a round, but an escape or a revert has no round to evidence it, only that
  commit, and its lineage record states how the defect was found. It must also have escaped
  through a revision that landed: the candidate itself when it is a `squash` case (its merge
  record evidences that it landed), otherwise the merged revision its lineage record binds. A
  revert of a branch commit before its PR merged reverts nothing that landed, however truly its
  record states it. So a case claiming a revert, or an escape, without all of this stays
  representable and unplanned, and adds nothing to revert or escape recall. And it was found no
  earlier than it landed: a `squash` case's discovery day is no earlier than its cutoff, the
  squash's committer time, and `verify` refuses a `pr_branch` case whose `post_merge` or `revert`
  discovery day precedes the committer day of the merged revision it names. A fix descending from
  the merge does not make an earlier report a post-merge one; a disclosure before the merge is
  exposure evidence (below), beside a discovery after it. Reviewing
  a case without its issue or with part of its discussion would be a different task, and scoring
  one whose boundary, base, PR or lineage only git's timestamps and subjects suggest would rest its
  labels on a guess, so the scorer refuses a plan naming any other case.
- The cutoff is the review-time boundary below. The base revision is the base ref's commit the
  review read, on the first-parent line of the ref as frozen, and the merge base is exactly the
  merge base of the base revision and the head: an older ancestor of the head would be another
  diff. Git keeps when a commit was made, not when a ref reached it, so committer times cannot
  establish the base revision: a commit dated before the review can have reached the ref after it,
  and its merge base with the head can be the same as the true base's. A `pr_branch` case's review
  record therefore states its base revision (below), and `verify` checks only that git is
  consistent with it: a commit of the ref's line, committed by the time the review read its input.
  Without a record, `verify` takes the ref's newest commit by the cutoff by committer time, refuses
  the case when times out of order across the cutoff (a rebase, a skewed clock) leave more than one
  such commit, and reports the base revision as **`provisional`**. A case without its record is
  never runnable, so a provisional base revision never reaches a scorecard; every seed round's is
  provisional. A `squash` case's base revision is the squash's parent, and `verify` reports it as
  `squash_parent`: the candidate is a commit of the ref's line with exactly one parent, that parent
  is its merge base, and its subject names the case's PR as a squash does (`(#<n>)` at its end). An
  ordinary merge commit is no squash. That the commit is the PR's merge is what its merge record
  states; the subject is a consistency check, and another PR's context can never be scored against
  it.
- Every withheld commit exists, is reachable from neither the head nor the base revision, and was
  committed after the cutoff. A defect's introducing commit is reachable from the head and not from
  the merge base. Which later commit fixed or reverted it, and, when it escaped a PR branch through
  a squash, which revision **merged** it, is what its lineage record states. `verify` checks that
  lineage against git for consistency, never as proof: the fix descends from the introducing
  commit, or from the merged revision, and in a `squash` case from the squash itself, and
  **addresses** the defect: the fix commit itself changes a
  file the defect is located in, and for a place located by its lines, changes one of those lines
  or inserts among or beside them, as they stand, exactly once, at the fix's parent. A descendant
  that changed something else fixes nothing, however late. The merged revision is one commit (one
  parent) of the base ref's line whose subject names the case's PR as a squash does, that changed
  every file the defect is located in, and that still holds each of the defect's lines exactly once
  as the head had them. A later commit can pass every one of those checks without being the merge
  or the fix (a commit after the squash whose subject names the same PR and leaves the defect's
  lines alone, then a comment added beside the defect), so only a lineage record makes a fix count,
  and a case naming a fix without one is never runnable. A squash lineage needs a PR and every place
  located by its lines. The corpus check refuses a defect located nowhere, a fix or merged revision
  that is not withheld, a withheld head, base revision or merge base, and a withheld commit named as
  a present defect's introduction.
- **One PR merged once, as one commit.** A `squash` case's candidate is the commit its PR merged
  as, and a defect's merged revision is the commit its case's PR merged as. The corpus check
  refuses two of them naming different commits for one PR, between a squash case and a lineage,
  between two lineages or between two squash cases, whatever records each holds: each can be
  consistent with git on its own (a later commit whose subject also names the PR, keeping the
  defect's lines, then a descendant changing them), and they cannot all be right. Any number of
  cases naming the one merge agree.
- A reviewer's input is identified by its digest: the review binding, the environment and each
  context item's id, kind, source, reference and text digest. Nothing about the answer (defects,
  labels, the retained records, withheld commits, actors, risk, exposure, case ids) enters it, and
  every run states the digest of the input it was given. A run naming another digest is refused.
- A model may have been trained on the answer. Each completed run counts its variant's exposure to
  the case: `possible` when the variant's stated knowledge cutoff falls on or after a day by which
  part of the answer was public (a registered defect's discovery day, a negative's own cutoff, as a
  negative's answer is its later history, or an earlier disclosure the exposure evidence names);
  `none` only when the evidence rules exposure out, the knowledge cutoff falling before the first
  day anything of the answer could have been public; `unknown` otherwise: no knowledge cutoff, no
  such evidence, or a cutoff between the two days. A newer candidate proves nothing on its own,
  since its head can carry code, and a finding about it, from a revision public before it. The first
  such day is no later than the committer day of the head and of every defect's introducing commit,
  which `verify` checks; an earlier copy of the code (a rebase, another branch) is the curator's to
  trace. A discovery is relative to the reviewed head, at or after its cutoff; a disclosure before
  it is exposure evidence, not a discovery. A review's discovery is no earlier than the round it
  credits either: when the corpus holds a case of that round of the PR, a discovery dated before
  that case's cutoff day would date the answer, and so a variant's exposure to it, before any
  round reported it, and the corpus is refused. A discovery day bounds the first report from above
  only (the seed's is the FIX commit's day). The counts are reported, not corrected for.

**The review-time boundary.** A review round's head is committed and pushed before its PR, and so
its PR body, can exist, so the head's committer time can fall before context its review read. A
`pr_branch` case may therefore take its cutoff from its **review record**: a record of the round
retained apart from the corpus (a record the controller kept of the REVIEW launch, say; which record
serves for this repository's history is #592's to establish), named by its stable source so anyone
can read it again, with its text and SHA-256. The source is a comment on the case's own PR
(`pr/<n>/comment/<id>`) or a document; a comment on another PR or on the issue is refused. The text
carries exactly one **binding statement**, a line that begins `review-record: ` followed by a JSON
object of exactly these fields:

```text
review-record: {"pr": 539, "issue": 329, "round": 1, "base_ref": "main", "base_tip": "<40 hex>", "head": "<40 hex>", "merge_base": "<40 hex>", "input_read": "2026-11-23T19:05:15Z"}
```

`pr`, `issue` and `round` must be the case's (`pr` is `null` when its provenance names none),
`base_ref`, `base_tip`, `head` and `merge_base` its review binding's, and `input_read` the
record's `started`, which is the cutoff. The issue binds the case's context to the round: a case
holding another issue's text, issue item and inventory entry, all agreeing with one another, is
refused under the round's record, since nothing else offline or in git names the issue a round was
for. Nothing else of the text is read: an instant it names elsewhere, such as when
it was published, is never taken for its start, and prose that names one is no binding. A record
carrying no such line, or two, is refused; whether any record of this repository's history carries
one is #592's to establish, and a round none does stays unplanned. `verify` bounds the cutoff by
git: the head was committed by it, and every withheld commit, the round's own FIX among them,
after it. Without a captured record the cutoff is exactly the head's committer time, the earliest
the candidate existed, so what it admits is conservative, its base revision is provisional, and
the case is never runnable: a round whose boundary is not evidenced stays unplanned. A `squash`
case takes the merge time, the squash commit's committer time; its diff is the merged change,
which can differ from what the last review read. `verify` refuses any other cutoff, later as much
as earlier: a later one would admit context from after the review started, such as the round's
own findings.

The other two records follow the same rules, each with its own binding statement:

```text
merge-record: {"pr": 540, "issue": 329, "base_ref": "main", "merged": "<40 hex>"}
lineage-record: {"defect": "r1-f1-reply-text", "pr": 539, "introduced": "<40 hex>", "paths": ["src/autoforge/doctor.py"], "summary": "<the defect's summary>", "round": 1, "finding": "R1-F1", "found": "review", "merged": null, "fix": "<40 hex>"}
```

A `squash` case's merge record names its PR, its issue (`null` when its provenance names none, so
the absence is stated, not assumed), its base ref and its head as the commit the PR merged as, and
states no start (`started` is `null`). A defect's lineage record names the defect's id, the case's
PR (`null` when its provenance names none), the defect's introducing commit, the paths it is
located in, sorted and each once, the defect's summary, the round and finding that reported it
(each `null` when none did), how it was found (`review` for a `same_review` or `later_review`
discovery, else `post_merge` or `revert`), its merged revision or `null`, and its fix or revert; a
defect that names no fix holds none. The id is what tells apart defects that share a PR, an
introduction, a file and a fix (round 1's R1-F2, R1-F3 and R1-F4 of #459's branch in the seed, say),
which would otherwise share one binding. The finding and the summary are what the record says the
defect is, so no other finding or defect can take a registration's place under its id and
unchanged record (round 1's R1-F2, its summary and span, in R1-F1's place, say); two defects of one
finding (#539's round 2 R2-F1 in the seed) are told apart by their ids and summaries. Lines are not
bound, so one defect registered at several heads of its PR, its lines each head's own, holds the
same record at each. A record of either kind that names anything else is refused.

No retained record is ever part of the reviewer's input, since each holds what the reviewer must not
be given: the round's own result, the merge, the fix. Not under its own source, and not in any value
the reviewer is given (each context item's id, kind, source, reference and text, the environment's
paths and validation commands, and the base ref): none of them may be the record (its digest), carry
its binding statement, or hold, between them, every other line of its text, wherever each line
stands. Each is compared by its letters and digits alone, without case, after decoding HTML entities
and compatibility forms, both as written and as a Markdown renderer shows it. The rendered form
drops what adds letters no reader of it sees: a link's or image's destination and title, a defined
reference's label and its definition, a defined footnote's marker, raw HTML tags and comments, and a
code fence's info string, while code spans and fenced code read as written, as in CommonMark. Its
blocks are read line by line as cmark reads them: the block quotes and list items each line
continues (a quote by its `>`, an item by its indentation), and in them fenced and indented code,
HTML blocks, headings and paragraphs, a paragraph alone continuing lazily. A fence ends at its
closing fence, read only past the fence's own quotes and items, or where they end: a fence a block
quote or a list item opens ends with it, and what follows is prose. A line in an HTML block or
indented as code opens no fence; an HTML block shows its text but its tags and comments; and inline
markup (a link, a code span, a tag or a comment) never runs from one paragraph or heading into
another. A tab stops at every fourth column, only a line feed or a carriage return ends a line, and
a byte order mark the value starts with is skipped. A link reference definition is read as
CommonMark defines one, on one line or several: its label (over lines, matched without case or
spacing), then its destination and an optional title, each after at most one line break, the title
over lines too, and nothing after either on its line; a title that does not end its line leaves the
destination alone the definition when a line break comes between them. A definition starts a
paragraph and never continues one: it may follow a blank line, a heading or its underline, a fence,
or start a list item (a bullet, or the number 1) or a block quote that interrupts a paragraph, and
it runs on over the paragraph's lines, their indentation stripped and lazily continued in a block
quote, up to a blank line or a line that opens another block; an underline after definitions alone
is the paragraph's text, not a heading. So a copy that only presents the record otherwise (each line
as a Markdown block quote, a list item or a table cell, emphasised, linked inline or by reference,
or in a code span, reflowed or joined into one line, its spacing or punctuation changed) is refused
as the record itself, and so is one below a fence its block quote or list item has ended. The
rendered form is a model of CommonMark, not a renderer: markup it does not model (a table's cells, a
footnote's indented continuation, an HTML block's text as a browser would parse it) reads as prose,
and a value whose link destinations would have it read more than four times the value's length (plus
4096 characters) is refused rather than read on.

The same holds across cases for every record that is demonstrably later than a case's cutoff,
whichever case retains it. A record cannot exist before what it records: a review record before
its round read its input, a merge record before its merge, a lineage record before the round that
found its defect (no earlier than the cutoff of any case of that round of the PR) or before its
fix, which each case registering the defect withholds, and no record before a commit its binding
names. A record of another case whose own cutoff, or whose finding round's, is no earlier than a
case's cutoff, or whose binding names a commit the case withholds (which `verify` holds to after
the cutoff), was never what that case's reviewer read, and the case's input may not hold it: the
corpus check refuses it. Nor may the input hold a record whose binding names a commit that git
dates after the case's cutoff, whether or not the case withholds it: `verify` dates each record
no earlier than the newest committer time of the commits its binding names, and fails the case
whose input holds one dated after its cutoff, so a later round need not list another round's fix
to keep that round's lineage record out. An earlier record may have been read: a later round's
reviewer can read an earlier round's review, whose commits all precede its cutoff, which stays
admitted. A paraphrase, a look-alike character from another script or a part of a record
cannot be told offline. What the format cannot check offline either is that a record is what its
source holds; capturing records is #592's.

What a future runner must still provide, and nothing here implements: a disposable repository whose
history ends at the head (as #318's runs start from a base's tree), so the reviewer cannot read
the withheld commits from the repository itself; no network route to the repository's later
history; and adjudicators who cannot see which variant produced a finding.

## 3. Labels and adjudication

Every label names who decided, their role, the verdict, the UTC day it was decided and an
independently reproducible artifact (REV-R0's examples: an oracle assertion, a minimized input, a
reviewed trace). A **curator** proposes and never settles anything. **Adjudicators** settle a label
when they agree: one suffices, two for a critical defect, either way. A disagreement stays `unknown`
unless **arbiters**, all of one verdict, settle it; an arbiter label without a disagreement is
refused. An arbiter decides between adjudicators and never stands in for one: a label that needs two
adjudicators (a critical defect's, either way, or a match to one) is settled by arbitration only
when two adjudicators gave the arbiters' verdict, and stays `unknown` otherwise, while any other
label's disagreement is the arbiters' to settle. An `unknown` verdict counts for nothing. One
identity labels a record once, no actor of the case may label it, and no label is decided after the
corpus's freeze. No reviewer variant may label any record of the corpus, in any role: the corpus is
checked before its variants are known, so the scorer refuses submissions with a variant whose
identity labeled a defect or a negative of any case, planned or not. An identity is a string, not an
authentication, as in REV-R0. No label of a case's defect or negative, and no label matching a
finding of a run of it, is decided before the day of the case's cutoff: a verdict on a candidate, or
on a finding about it, cannot precede the candidate as it was reviewed.

A finding is matched against the case's registered defects by adjudicators who are neither a
reviewer variant nor an actor of the case: `match` to one defect, `false`, or `unknown`, two
adjudicators for a match to a critical defect. A match to a confirmed defect is a confirmed
finding, a match to a rejected one is false, and a match to an unsettled one is unknown. A real
defect the corpus does not register is `unknown` in this release, as in REV-R0, and belongs in the
next one. Similar wording, severity or a finding id never makes two findings one defect.

## 4. Censoring

Absence of evidence is not a label, and the corpus keeps the difference:

- A negative is **settled** only when an independent pass labels it clean and its observation
  window, from the cutoff to `observed_through`, is at least 30 days. A shorter window is
  **censored**, no settled pass is **unlabeled**, and a pass that finds a defect **refutes** it.
  A clean pass dated before the case's cutoff day is refused however long the window, since the
  window cannot make a pass on a candidate that did not yet stand valid.
  Only settled negatives enter the false-positive rate. The 30 days is a preregistered choice, not
  a measurement: it was fixed before any reviewer ran, and changes only by section 9.
- A defect discovered after the freeze is outside the release. No case is ever "clean" because
  nothing was registered.
- An inventory is what was found, not what exists. Recall is recall of registered, confirmed
  defects; a finding outside the inventory is unknown, which bounds precision rather than
  lowering it.
- **Discovery bias.** A `same_review` defect is one the case's own historical reviewer found, so a
  reviewer resembling it is favoured on those defects. `later_review`, `post_merge` and `revert`
  defects are ones the historical reviews missed. Recall is reported per discovery source so a
  contrast resting only on `same_review` defects can be seen for what it is.

## 5. Submissions

A submissions file names the corpus by its digest (SHA-256 of its canonical JSON, so the file's
layout does not matter but every value does) and its own freeze for finding labels. It pins each
variant: identity, provider and its version, model, effort, stated knowledge cutoff, prompt
template and its SHA-256, the AutoForge commit and the sandbox. Its plan is a list of cases and
seeds; a seed is an independent repetition in a fresh session. Each planned run is `completed`
(with its findings, each classified `blocked`, `non-blocked` or `nit` as a REVIEW result is),
`failed` (no findings: missing evidence, never zero findings), or `excluded` for one of REV-R0's
reasons. One run per variant, case and seed. Each run states `pins_sha256`, the SHA-256 of its
variant's pins (the whole variant record, canonical JSON) as it was launched, and a run whose
digest is not its variant's is refused: changing a model, prompt, effort or any other pin turns
every earlier run into stale evidence rather than crediting it to the new configuration. Each
scorecard names that digest. Synthetic submissions are scored only against the
synthetic corpus and recorded ones only against a historical corpus, and a historical corpus is
scored only after `verify` accepts that exact corpus against the object store `score --repo`
names: a cutoff, merge base, lineage or environment pin git contradicts never reaches a recorded
scorecard, whose report names the verified corpus's digest.

The file-size cap (8 MiB) bounds what is read, not what a score expands to, so the plan is held to
work limits before anything is expanded: at most 8 variants, 10,000 planned pairs (cases times
seeds) and 64 clusters among the planned cases, since each bootstrap table holds 10,000 draws of
every cluster a metric observes; at most 16 such tables are kept at once. A plan beyond any limit is
refused. They are bounds on cost, not statistical choices. Within them nothing is computed once per
run and defect: each planned case's confirmed defects are tallied once, by severity and discovery
source, and each completed run's observations are computed once, from its findings and that tally,
for all 17 metrics of its scorecard and every contrast it is paired in. So the work grows with the
registered defects plus the runs and their findings, never with their product, and the bootstrap
(17 metrics for each of 8 variants, and for each of 7 contrasts on both arms) grows with the
clusters, not with the runs. Measured on one host (an Intel i7-8700K core, Python 3.14), CPU time:
a synthetic score at the limits, 8 variants and 64 clusters with all 17 metrics observed in every
cluster, took 62 seconds, nearly all of it the bootstrap, with each cluster count's tables under
11 MB; one cluster holding 12,000 registered defects with 20,000 completed runs, each file under
8 MiB, took about 3 seconds, its checks included. Neither is a benchmark result.

## 6. Scorecards

Each variant gets a scorecard over its completed runs, and each non-baseline variant a contrast
with the baseline over the pairs both completed.

| Metric | Observations, pooled over runs | Label bounds |
|---|---|---|
| Weighted recall | weight of distinct confirmed defects found / weight of the case's confirmed defects | the upper bound adds, per run, the heaviest missed defects, at most one per unknown finding: an outer bound, not an attribution |
| Precision | confirmed findings / (confirmed + false) | unknown findings counted first as false, then as confirmed |
| Clean-negative FPR | settled-negative runs with a false finding / settled-negative runs | a run with only unknown findings counted first as no alarm, then as an alarm |
| Recall by severity, discovery source, risk | as recall, by defect count within a stratum (by weight for risk) | each stratum bounded on its own |
| Precision by classification | as precision, within `blocked`, `non-blocked`, `nit` | as precision |

A scorecard also counts runs by status, confirmed, false and unknown findings, duplicates
(confirmed findings after the first of their defect in a run), training exposure (`none`,
`possible`, `unknown`, by section 2) and total cost
(unknown when any launched run's cost is). An empty denominator is undefined, never zero.

Uncertainty is reported two ways and never combined. Label bounds come from the unknowns above.
Sampling uncertainty comes from a cluster bootstrap: 10,000 resamples of the clusters that observe
the metric (which section 1 holds to the corpus's issues, PRs and defect commits), seed 356, every
case and seed of a drawn cluster together; the draws depend only on how many clusters a metric
observes, so both arms of a contrast share them. The report gives the nearest-rank 2.5th and 97.5th
percentiles of the point estimate (a contrast's of the point difference), a separate outer envelope
(variant low minus baseline high, variant high minus baseline low), and the number of undefined
draws, which are never replaced. Values are exact fractions.

A contrast is `usable` only when every planned pair either completed on both arms or was
excluded, it observes at least two clusters (one cluster resamples to itself), both arms' label
bands are at most 0.10 wide, both points are defined and no draw is undefined. An excluded pair
leaves the contrast; a missing or failed one leaves it incomplete.

There is no decision rule. A scorecard is a measurement, not a `GO`: deciding to change a
reviewer needs its own preregistered thresholds and REV-R0's or #318's approval steps, and a
synthetic report is never evidence for anything.

## 7. The seed, release 1

`tests/fixtures/review_corpus/autoforge_seed.json` holds eight real candidates from this
repository, frozen on 2026-11-27.

**How they were chosen.** Rule 1: on a PR branch in the local object store
(`refs/remotes/origin/autoforge/*` on 2026-11-27), a FIX commit whose subject names round *n*'s
findings (`R<n>-F<m>`) has round *n*'s reviewed head as its first parent. Rule 2: the squash
commit on main that merged the issue of a seed defect case is a hard negative on the same surface.
Rule 1 finds many more rounds than were taken (issue #317's branch reaches round 11, #324's
round 19). Release 1 took the four rounds of issue #329's branch, because its FIX messages
describe each finding precisely enough to trace in the code, plus the first round of #414's and
#262's branches for a second critical surface and a documentation-only case. That choice is the
curator's, not a rule.

| Case | Head | Registered defects (same / later review) | Stratum, risk |
|---|---|---|---|
| `pr539-round1` | `89126296` | 1 / 4 | reviewed defect, critical |
| `pr539-round2` | `c8d96429` | 3 / 2 | reviewed defect, critical |
| `pr539-round3` | `046bbb12` | 1 / 2 | reviewed defect, critical |
| `pr539-round4` | `f47a59a3` | 2 / 0 | reviewed defect, critical |
| `pr459-round1` | `0b8eeb75` | 4 / 0 | reviewed defect, critical |
| `issue262-round1` | `fd257b28` | 1 / 0 | reviewed defect, routine |
| `pr540-squash` | `e86e606b` | none | hard negative, critical |
| `pr460-squash` | `08a2e075` | none | hard negative, critical |

**What each case registers.** Every finding a round's FIX commits name is registered at the head
that round reviewed. For #329's branch it is also registered at each earlier head into whose
history the curator traced the defective code, as a `later_review` defect: four of the seven
defects there were already in the producer's commit and missed by round 1. Two came from FIX
commits: round 1's FIX narrowed doctor's exact-read handler so that `gh_workdir`'s error escaped
it (`r2-f1-workdir`), and round 2's FIX added the caller inventory that left out K13
(`r4-f2-k13-inventory`). R2-F1 is registered as two defects because its two failures have
different causes and different introducing commits. That makes 20 registrations of 12 distinct
defects. The discovery day is the FIX commit's committer day, an upper bound on the review that
reported it. The seed states no exposure evidence: git shows each defect's introducing commit, but
not whether its code, or a report of it, was public earlier (another branch, an earlier attempt,
the branch before a squash), and the curator has not traced that. So no seed case is ever counted
`none`: a variant whose knowledge cutoff falls before the first discovery day is `unknown`.

**What the seed does not cover.** It has no `escape`, `revert` or `clean` case. No commit on main
through `e3443d30` reverts a merged change: the one revert in its history, inside #73's squash,
undid a commit of that PR's own branch. Post-merge fixes exist (`d6fe4fd4`, for example, fixes how
a step's result was redacted), but the defect each fixes has no single introducing commit that git
shows, and tracing it is curator work release 1 did not do. No round is known clean from git
alone, since a clean round leaves no FIX commit. The strata exist in the format, and the synthetic
fixture covers them, but this seed measures none of them.

**What is judgment and what is unknown.** Severity, class, summaries, risk strata, the split of
R2-F1 and the tracing to earlier heads are the curator's proposals, recorded as curator labels;
none is settled. The two negatives have no independent pass, so they are unlabeled, and by the
freeze their windows (3 and 21 days) would leave them censored anyway. Offline, git does not hold:
the issue and PR text at each cutoff, nor the listing of the comments that stood at it, so every
case holds its issue (`issue/<n>`) and PR body (`pr/<n>`) as `not_captured` items and an inventory
that is `not_captured`; #262's branch names no PR, so its PR body has no source, and no listing can
name it until one is established. Nor does git hold any round's review record, so every round's
cutoff is its head's committer time and its base revision is provisional; nor either squash's merge
record, nor any defect's lineage record: the FIX commits' subjects name the findings they fix and
the squashes' subjects their PRs, but a subject is no evidence. No case is runnable yet, on any of
these counts. Git does not hold
the full finding list of each round either (a finding resolved without a commit, deferred or
carried forward does not appear), which rounds were clean, and which agents and models acted. PR
numbers are as the commits state them (main merged those issues as #540 and #460), not checked
against GitHub. The actors are role names for the same reason. The base ref is `main`, pinned at
`e3443d30`; by committer time main had not moved past a round's merge base at its cutoff, so each
round's base revision is pinned as its merge base, provisionally, since no record states which
revision the review read. The clusters follow the issues: #329's four rounds and
#540's squash, #414's round and #460's squash, and #262's round.

**Verification.** `uv run python -m tests.review_corpus verify --repo .` was run on 2026-11-27
against this repository's object store, with git 2.47.3, after the round-5 checks were added: all
eight cases passed every check of section 2. The seed declares its repository's environment as
`ENVIRONMENTS` registers it for `iceboundrock/AutoForge` at root `e5d562e8` (`pyproject.toml`,
`uv.lock`, `make check`). Each cutoff equals its head's committer time. For each
round main's first-parent line splits cleanly at its cutoff and the base revision is main's tip
there by committer time, which `verify` reports as `provisional`. Each squash is a one-parent
commit of main whose parent is its merge base and whose subject names its PR (`squash_parent`).
Each merge base is exactly that of its base revision and head, each environment pin its head's
blob, and each fix changes what its defect is located in. With `pr539-round1`'s merge base
replaced by the repository root, or its `uv.lock` pinned as absent, or `pr540-squash`'s PR given as
another, the case fails. The PR branches are not on main, so rebuilding these cases needs their
objects (the branches, or the PRs' head refs on GitHub, which were not checked); CI's shallow
checkout does not hold them, so no test reads this repository's history.

**What would make it golden:** capturing each case's context as it stood at its cutoff (a GitHub
read of the issue, PR body and comments with their edit history, and a complete inventory taken
from that read), each round's review record, each squash's merge record and each defect's lineage
record, exposure evidence traced where it can be,
independent adjudication of every defect (two for the critical one) and both
negatives by people who are neither its actors nor any variant to be scored, escape, revert and
clean cases traced as carefully as the defect cases, and only then planned runs, matched blind.

## 8. Commands and tests

```sh
uv run python -m tests.review_corpus describe           # the seed: cases, strata, label states
uv run python -m tests.review_corpus verify --repo .    # its pins against this object store
uv run python -m tests.review_corpus score              # the synthetic fixture's scorecards
uv run python -m tests.review_corpus score CORPUS SUBMISSIONS --repo .  # a historical corpus
uv run pytest tests/test_review_corpus.py
```

The synthetic fixture (`synthetic_corpus.json`, `synthetic_submissions.json`) covers every stratum
and label outcome, with a complete inventory for every case: a disagreement, a critical defect with
one adjudicator, a rejected defect, a censored and two settled negatives, a duplicate, a match to a
rejected defect, unknown findings and a variant without a knowledge cutoff. The tests check its
scorecards against values computed by hand, refuse each malformed or leaking record, settle labels
and change windows to show each rule move a number (a critical defect, and a match to one, one
adjudicator each way and an arbiter of either verdict, staying unknown and out of recall and
precision until two adjudicators give the arbiters' verdict, while a disputed match to a
non-critical defect is the arbiter's), and run `verify` on a throwaway repository with
fixed commit dates, including against the operator's `GIT_*` variables and a replacement object, and
refuse a cutoff later than the head together with the review comment it would admit. They refuse a
case missing its issue or PR body, an inventory that leaves an item out or lists one too many, a
copy of a comment held in place of another or a source repeated, a source of another kind or issue,
a withheld commit named in a reference, a source, an id, a validation command or the base ref, a
case that leaves an environment pin or its validation out, a defect located nowhere, a variant that
labeled the corpus in any role, a run whose pins digest is not its variant's (a model and prompt
changed under unchanged runs), and a plan beyond the work limits while under the file-size cap (six
cases and a million seeds). On the throwaway repository they admit a PR body opened after the head
and before the review its record evidences, refuse context after that boundary and a cutoff moved
past it, refuse a record whose binding states an input read earlier than the cutoff its publication
time would give, a record on another PR, a binding of another PR, round, base ref, head or merge
base, prose or two bindings in its place, and a copy of the record held as context under another
source, whole, reformatted, without its binding or as its binding alone; they bound the record by
the head and the round's fix, refuse a recorded score without a verified corpus or with a cutoff git
contradicts, refuse an ancestor that is not the merge base, a base revision that was not the ref's
tip, and both of two base revisions on a line dated out of order, hold each environment pin, `null`
included, to the head, follow a squashed defect through its merge to its post-merge fix and refuse
an unrelated fix, a later rewrite of the same file and a documentation-only descendant in their
place, and a fix that changed none of its defect's lines, and hold exposure evidence to the commits.
They refuse a retained record copied into any value the reviewer reads, the context's own text and
digests left as they were: whole in a reference or a validation command, its lines split over two
references, its binding alone, and the body of a lineage or merge record. They show a delayed ref
update: a base-only commit dated before the review that reached main after it, which committer
times take for the base revision and `verify` reports as `provisional`, refused under the review
record that names the base the review read, as is a recorded base revision off main or committed
after the review read its input. They refuse a squash candidate whose PR is not its commit's (the
provenance, PR body, inventory and merge record of another PR, all agreeing), an ordinary merge
commit as a squash candidate or a merged revision, and false lineage chains that pass every git
check (after the genuine squash, a commit whose subject names the same PR and leaves the defect's
line alone, then a comment beside the defect; off the head, a comment beside a round's defect in
place of its fix): each is refused under the genuine chain's lineage record and never runnable
without one, and a lineage record naming another PR, introduction, path, merge or fix is refused.
They refuse an absent placeholder pinned in place of every environment file, and score a plan of
one cluster with 12,000 registered defects and 20,000 completed runs, each file within the size
cap and the plan within its limits, computing each run's observations once and its case's tally
once.
They refuse a review record's answer, without its binding, in one reference as a Markdown block
quote, nested in a quote, as an emphasised list, as a table row, reflowed into one line or broken
elsewhere, in code spans with its punctuation changed, escaped as HTML entities or in full-width
forms, and with its file made a Markdown link (inline, titled, in angle brackets, with nested
parentheses, an image, a reference link and its definition), an HTML link, a tag whose attribute
holds a `>`, a footnote's marker or an HTML comment inside the line, and a line of it quoted in
each of two references; each in a reference as a document's text alike. The reference link's
definition is also refused over several lines: its destination on the line after its label, its
destination in angle brackets and a title over two lines each on lines of their own, its label
broken over lines and in another case, in a block quote continued lazily, opening a list item
that interrupts a paragraph, and after a heading's underline; and the answer, linked by reference,
below a fence a block quote or a list item opened (on the item's line or below it) and ended, below
a deeper quote's fence line inside a fence, and below an HTML block holding a fence's line. They
check that a fence ends at its closing fence, read past its own quotes and items only, or where
they end, a legitimate quoted code block reading as code; that no fence opens in an HTML block,
indented as code or past a list marker's four spaces; tab stops, line endings, a leading byte
order mark, a comment or tag a blank line cuts, a closing tag with attributes, a link a heading
opens, an HTML block's text and its comment over blank lines. They check what the rendered form
drops and keeps on CommonMark's own cases (code spans and fences as written, no link inside a
link, an undefined reference and a definition inside a paragraph as text, and the definitions of
its examples 193 to 218, those it reads as text included: a blank line inside a title, text after
a title, a missing destination, a title not set apart from it, a definition past a list item or a
block quote that interrupts it, or where a list item cannot interrupt), and that a backslash
escapes only punctuation and a destination ends only at an ASCII space or control character. They
admit a PR body that links its file inline and by a reference defined over two lines and holds no
record, and refuse a value whose link destinations would be read past the bound. They admit round
2's input holding round 1's review, published before round 2 read it, and refuse round 1's input
holding round 2's review record, whole as a document or its answer alone in a reference, and round
2's holding the answer of round 1's lineage record, whose fix round 2 withholds; and when round 2
does not list that fix, the corpus check admits it, `verify` fails round 2 by the fix's committer
time and the score refuses it, while without the copy round 2 verifies and scores, the fix
unlisted. They refuse a round or a
squash given issue 99's context, issue item and inventory entry under its unchanged review or merge
record, and a squash of no issue under a record naming one, or the reverse. They refuse a second
finding of a round, at another span of the same file with the same introduction and fix, under the
first finding's lineage record or under its source, and score it, the confirmed weight going from
one to two, only with a record of its own; refuse round 1's R1-F2, its summary and its span in
the same file, in R1-F1's place under R1-F1's unchanged record, as they refuse either the finding
or the summary alone replaced, while R1-F1's lines moved within its file still score under it;
let one defect registered at two heads of its PR share one lineage record, its lines differing,
and refuse another finding, summary, severity or discovery day under its id, with a record of its
own or without; and show that the seed's three defects of #459's one FIX share everything a
lineage binds but their ids, findings and summaries, and #539's two defects of round 2's R2-F1
are told apart by their ids and summaries. They count a revert of the squash on main for revert
recall only under the revert's own lineage record, leave unplanned a revert claimed without any
revert or without its record, a revert made on the branch before its squash landed, under a record
that states it truly, and an escape without its fix's record, count a squash candidate's own
revert on main, and refuse the branch's earlier revert named as the squash's, which follows neither
the squash nor its cutoff, and a review's FIX claimed as a revert under its own record. They
count an escape found the day after its squash for post-merge recall, refuse it in `verify` and
the score when found two days before the squash landed, though its fix descends from the squash,
accept it found on the squash's own day, and count the earlier report as exposure evidence beside
the later discovery. They score round 1's defect found by round 2's R2-F1 the day round 2 read
its input, refuse it in every command dated the day before, when a knowledge cutoff of that day
would read as `possible`, and keep that day's disclosure as exposure evidence beside round 2's
discovery. They score a squash case's merge record and a branch round's lineage record
naming the same merge of PR 2, and refuse, in every command, the lineage record naming instead a
later commit whose subject also names PR 2, which keeps the defect's line and whose descendant
changes it (consistent with git on its own, which `verify` shows without the squash case), as
they refuse two lineage records of the PR naming two merges. They
refuse a clean pass dated five days before the squash it judges, under a window long enough to
settle it, which settles once dated on the squash's day, a defect label dated before its case's
cutoff, and a finding judged false five days before the squash its run reviewed, which once dated
on the squash's day is the settled negative's false alarm. They refuse an existing, unrelated file declared as the whole environment and
pinned truly at every head, the real environment files present but unpinned, as well as the
registered files validated otherwise or one of them left out, and the seed declaring another
environment; a repository nobody registered runs nothing, and a registered file absent at every
head stays `null` beside the real pins while the case runs.
They show exposure `possible` for a defect disclosed at an earlier revision, `unknown` between a
candidate and its first discovery day and without evidence or a stated cutoff, refuse one issue, PR
or defect commit split over two clusters, and let related issues share one. They pin the seed's
state: eight historical cases, 20 unsettled registrations, no review, merge or lineage record, no
exposure evidence, no runnable case.

## 9. Coordination and amendments

- REV-R0 (#355) keeps controller-loop interventions; this corpus is for reviewer variants and
  reuses REV-R0's definitions rather than restating them differently.
- #318 governs any real run: fresh sessions, paired order, preflight and separate cost approval.
  Its contamination rule (no run on this repository) becomes section 2's disposable repository,
  since this repository's own history is the corpus.
- No state protocol, schema or ADR changes, so nothing is coordinated with #23.
- Tests and CI call no model and no GitHub API.

A rule change bumps the format `version`; a content change bumps the corpus `release`. Either
updates the digests below and the tests in one reviewed change, and says whether any run preceded
it. Version 1 and the seed's release 1 were amended in place during their own review, before
either was merged; no run preceded any amendment.

Fixture SHA-256:

- `autoforge_seed.json`: `47ae2f21d33a42f6d0cb7bdea8e6823ca638695d66714edf16d7d0fbfcbac4d0`
- `synthetic_corpus.json`: `dae6af40dfbf04060fd0300be5ea540eb2963b2e1d2f69aa5d429964d95ebe64`
- `synthetic_submissions.json`: `5cc7bdfbf7d293d982b8e0e4a1c57a5446bccb1dc430d3c206a751f6b937ce30`
