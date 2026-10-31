# ADR 0010. Fast feedback and full validation: two tiers, one kind of evidence

- **Status:** proposed with #342's pull request; its review decides it.
  - *Implemented with it:* the repository tooling of D5 and D9
    (`make test-fast`, `tests/fast_targets.py`) and the tests of §5. Neither
    changes what the controller, the state file, a `CONTROL_RESULT`, a
    prompt or CI does.
  - *Designed, not implemented, and gated:* a controller-run FAST pre-check
    (§2.6). It needs its own issue, approval and measurement (D11, D12).
- **Decides:** #342 (TSP-M3), for EPIC #338.
- **Planned against:** `main` `1f6abad` (the issue's baseline, 2026-10-26).
  Written at `main` `a4bc60c` (2026-10-31), where every reference below was
  read.
- **Builds on:** ADR 0006 (candidate validation evidence and review
  certificates, implemented by #171 to #175); #340 (M1, the suite on
  several workers) and #341 (M2, where the serial suite's time goes), both
  on `main`.
- **Amends:** nothing. ADR 0006's decisions, types, encodings and state are
  unchanged, and so is every other ADR.
- **Related:** ADR 0004 (the authority boundary), ADR 0005 (the outer
  sandbox), #318 (§1.4).

## 1. Context

### 1.1 What decides "the tests passed" today

Four things, of which only the first is an agent's:

1. **The agent's own runs.** An `ANALYZE_EXECUTE` or `FIX` agent runs what
   it chooses in its worktree and says so in prose and in `tests`, a list
   of one-line strings (`result_parser.py` `AnalyzeExecuteResult`,
   `FixResult`) that the controller bounds and never reads. A
   `REPLAN_REEXECUTE` agent reports `tests_passed`, which can only reject
   (ADR 0006 D18.1). All of these are claims.
2. **`validation.commands`**: trusted argv the controller runs on a
   byte-exact export of the exact candidate, at the producer gate before
   the first effect intent (ADR 0006 D5.1) and at the REVIEW entry (D5.5).
   Each launch leaves a `ValidationRecord`; a clean review over complete
   evidence leaves a `ReviewCertificate` (D12), which `READY_FOR_MERGE`
   (D12.3) and every merge gate pass (D13.2) require. The set is empty by
   default (D3.6).
3. **`merge.verification_commands`**, run at every merge gate pass on an
   export of the reviewed HEAD (D3.3).
4. **The required `ci` check**, read live at the merge gate. It needs
   `test` (the whole default suite, `uv run pytest`, on Python 3.11 and
   3.12), `static` (ruff and mypy), `sandbox` (the real outer-sandbox
   backend, standalone) and `dist` (the built wheel, installed and run),
   and fails when any of them was skipped or cancelled. The gate also
   requires every check in the rollup to pass, compares the check
   definitions with the base branch's (`safety.verify_check_definition`),
   and never merges a PR that touches `.github/workflows/` unattended.

### 1.2 What the full suite costs

Measured by #340 and #341, and once more for this record:

| Host, Python, commit | Configuration | Wall seconds |
|---|---|---|
| A (4 CPUs, xfs `TMPDIR`), 3.14.8, `3259116` | serial | 932, 954 (#341) |
| B (8 of 12 threads, tmpfs `TMPDIR`), 3.11.18, `0255ca2` | serial | 501 median (#340) |
| B, 3.11.18, `0255ca2` | `-n 8 --dist=load` | 93 median (#340) |
| B, 3.14.10, `a4bc60c` | serial | 535 (this record: 12,483 passed, 85 skipped) |

The whole suite is therefore a run of one and a half to sixteen minutes,
depending on the host and the workers. Running it on workers is not a gate
yet: CI stays serial until #340's unexplained Ctrl-C failure is understood
and the real-backend sandbox tests have run clean on workers under 3.11
(`docs/development.md`, "The suite on several workers").

At `a4bc60c` on host B, one module, `test_engine.py`, took 74 of the 509
seconds the tests themselves took, and the slowest ten modules 330.

### 1.3 The problem

An agent iterating on a change wants feedback in a minute or two. The
controller's evidence must stay full. Narrowing what runs to get the first
creates the one risk this record exists to close: a narrower success taken
for the full one, whether by an agent's prose, a `ValidationRecord`, a
`ReviewCertificate`, `READY_FOR_MERGE` or a CI check.

### 1.4 What this record could not read

#342 names #318 as part of the trust boundary this design must respect.
Its text was not available while this record was written, and nothing at
`a4bc60c` refers to it. The record therefore claims nothing about #318
beyond what holds for every issue: it adds no authority and changes no
controller behaviour. Review should check the record against #318.

#342 also names M0 as a hard dependency. This record relies only on M1's
and M2's measurements (#340, #341), which are on `main`.

### 1.5 Terms

- **FULL:** what decides, for the controller or for GitHub, that a
  candidate's tests passed: ADR 0006's records and certificate under the
  set and policy in force, `merge.verification_commands`, and the required
  `ci` check. Nothing else is FULL.
- **FAST:** a narrower run whose only purpose is feedback to whoever is
  changing the code.
- **Promotion:** a result of a narrower or another run counting toward a
  FULL requirement.
- **Downgrade:** a FULL requirement met by less than it states: a FULL
  failure that stops counting other than through the operator's override
  (ADR 0006 D14.2), or a binding or certificate that survives the set it
  states being changed.
- **Provenance:** who ran what on which exact bytes: the controller or an
  agent, which definition, which candidate.

## 2. Decisions

### 2.1 FAST is feedback, never evidence

**D1.** No FAST outcome creates, replaces, satisfies or clears a
`ValidationRecord`, a `review_evidence` binding, a `ReviewCertificate`, the
producer failure counter or the override watermark. None enters
`READY_FOR_MERGE` or `MERGE`, is published as a GitHub status or check, or
is presented to a reviewer as validation.

**D2. Every `ValidationRecord` is FULL by construction.** FAST gets no
record type, no tier field in a record, no definition in
`validation.commands` and no place among ADR 0006's persisted fields
(D11.2: the records, the attempt marker, the binding, the certificate, the
counter and the watermark). In stage 1 it has no place in `state.json` at
all. ADR 0006 D1.1 stays exact: a record is what the controller observed
when it launched a definition of the set in force.

*Rationale.* With a tier field, "is this evidence?" becomes a question every
reader of a record has to remember to ask. When FAST cannot be a record at
all, promotion is unconstructible rather than forbidden, as ADR 0006 D2.7
does for the two candidate kinds.

### 2.2 The controller selects FULL

**D3.** Whether and when FULL runs is decided by the controller from
trusted configuration alone, exactly where ADR 0006 placed it: with a
non-empty `validation.commands`, at every producer gate before the first
intent (D5.1) and at every REVIEW entry (D5.5), and at every merge gate
pass the certificate check, `merge.verification_commands` and the live
`ci` read. No `CONTROL_RESULT` field, agent prose, FAST outcome, candidate
file or issue text skips, defers, narrows, reorders or replaces any of
them. An agent's `tests` and `tests_passed` stay claims (ADR 0006 D18.1).

**D4. The operator owns the FULL set, and every change to it shows and
invalidates.** `validation.commands` may hold the full suite or less; this
record makes no set mandatory, as ADR 0006 D3.6 did not. What it relies on:

- a record stands for its own definition at its own index on its own
  candidate under its own policy (ADR 0006 D3.5, D8.3), so a PASS of
  `pytest tests/test_x.py` is no evidence for a definition `pytest`, even
  at the same index of another set;
- a set's evidence is complete only when every definition of that set
  passed, and a binding is reused only under its own set digest (D5.5);
- a certificate states the set its review was presented (D12.1), and a set
  narrowed or widened after the review makes it inapplicable at issuance
  (D12.6) and at the merge gate (D13.2); the operator then restores the
  configuration or runs a new round under the new one (D12.3).

A narrower FULL set is the operator's decision and yields a certificate
that states it, a weaker one, never a FAST one. For this repository the
FULL set worth configuring is the suite as CI runs it (`uv run pytest`),
with lint and types if the certificate should cover them.

### 2.3 Stage 1: FAST as repository tooling

**D5.** FAST exists as a target that an agent or a human runs in their own
worktree, `make test-fast` (D9). What it reports is the runner's own
observation, and once an agent relays it, a claim like any other (D3). The
controller neither runs nor reads it.

**D6. It is never a gate.** `make test`, `make check`, `make check-matrix`
and every CI job run the full suite as before. `test-fast` is a
prerequisite of none of them and no workflow runs it;
`tests/test_fast_targets.py` fails otherwise. `AGENTS.md`'s rule to run the
configured verification before declaring work complete is unchanged.

### 2.4 CI stays as it is

**D7.** No CI change. `ci` keeps needing `test` on 3.11 and 3.12 (serial,
the whole default suite), `static`, `sandbox` and `dist`, and keeps failing
on a skipped or cancelled job. No FAST job is added. If one ever is, it is
one more check in the rollup that must pass, never in `ci`'s `needs` in
place of `test`, and the change is to a protected path that a human reviews
and merges. Running `test` on workers is #340's separate recommendation,
with its own preconditions.

**D8. What the full suite collects is PR-editable, and no tier changes
that.** `uv run pytest` collects what `pyproject.toml` (`testpaths`,
`addopts`) and the conftest let it. A PR can narrow that, with an added
`-m`, `-k` or `--ignore`, a collection hook or a deleted test, without
touching a protected path, and the check-definition comparison is
structural (job and step names), so it would not notice. A test pins
`testpaths = ["tests"]`, `addopts` deselecting only `live`, the set of
pytest options and the absence of collection hooks (§5). It is candidate
code itself: a tripwire for an honest mistake, not a control. The controls
remain review, `merge.verification_commands` and `safety.allow_merge` off
by default. A base-bound check comparing the collected tests with the base
branch's would close part of the gap; it is future work and is not
created here.

### 2.5 The coverage mapping

**D9.** `tests/fast_targets.py` holds the mapping and the selection, and
`make test-fast` runs it against `FAST_BASE` (default `main`).

- **Per changed path:** a test module selects itself (a removed one
  nothing of its own) and every test module that imports it directly; a
  path with an entry selects the entry's test modules and every test module
  that imports its module directly; an entry marked `FULL` selects the full
  suite; any other path selects the full suite. The run is the union, and
  the full suite as soon as one path selects it.
- **Never only deselected tests:** a selection holding a test module whose
  tests the default options all deselect (`test_live_pi.py`) holds that
  module's offline tests too (`test_live_pi_support.py`). pytest handed
  only deselected tests collects none and exits 5, which would read as a
  failure though nothing failed. FAST never adds `-m live` to make the live
  tests run.
- **Changed paths** are those whose commit (`HEAD`), staged version or
  worktree version differs from the merge base with `FAST_BASE`, each
  compared on its own so that one undoing another's change hides neither,
  both sides of a rename, and the untracked files git does not ignore.
- **Explicit:** every file under `src/autoforge/` and every helper module
  under `tests/` has an entry, an exact path or a directory prefix.
  `tests/test_fast_targets.py` fails when a file has none, when an entry
  names a path or test module that does not exist (or one whose tests are
  all deselected by default), when an entry selects nothing, or when a
  module marked `live` as a whole names no offline tests.
- **Three kinds of entry.** A module almost every test runs through is
  `FULL`: `engine.py`, `state.py`, `transitions.py`, `config.py`,
  `errors.py`, `executor.py`, `providers.py`, `github.py`, the prompt
  templates, the package's `__init__.py`, and the tests' conftest and
  `tmp_path` plugin. A module the engine calls on every scripted flow lists
  the loop's own tests, `test_engine.py` and `test_integration.py` (the
  scripted loop `testing.md` requires), plus its feature's. A module of one
  feature lists that feature's tests beyond its importers.
- **Not a coverage claim.** The selection is where a regression is likely
  to show first. A test that reaches a module through the engine
  (conftest's `make_engine`) does not import it, so a change can break a
  test outside the selection. A mistake in the mapping costs feedback,
  never safety, because nothing reads a FAST result (D1); and the mapping
  never narrows what it does not know.
- **It says what it did:** for each changed path, what it selected and why,
  and that a FAST run is not evidence.

What a selection cost on host B at `a4bc60c` (Python 3.14.10, serial, wall
seconds including pytest's start-up; the full suite took 535):

| Changed path | Selected | Wall seconds |
|---|---|---|
| `tests/test_evidence.py` | itself | 3 |
| `src/autoforge/scheduler.py` | 4 of 89 modules, which wait on purpose | 72 |
| `src/autoforge/sandbox_policy.py` | 11 of 89 modules | 108 |
| `src/autoforge/evidence.py` | 10 of 89 modules, `test_engine.py` among them | 125 |
| `src/autoforge/engine.py` (or any unmapped path) | the full suite | 535 |

The first row predates the rule that a test module selects the test
modules that import it: `tests/test_evidence.py` now selects
`test_certificate.py` too. Through the tool on an 8-CPU host under
Python 3.14.10, that took 3.5 seconds, against 3.4 for `test_evidence.py`
alone.

**D10. A change-based selection is base-bound.** A selection computed from
a diff depends on the candidate and on a base revision. In ADR 0006's terms
(D4.1) it is base-bound, a scope `validation.commands` refuses (D4.2), and
the controller's export has no `.git` to diff anyway (D6.2). A controller
FAST definition (§2.6) therefore names its selection statically (fixed
test modules, or `python -m tests.fast_targets --run` with fixed paths): a
candidate-tree definition like any other.

### 2.6 Stage 2: a controller FAST pre-check, designed and gated

**D11. Gated.** #342 does not implement it. It takes its own issue, an
approved review of D13 against the code of the day, and the measurement of
D12. If it persists anything, it makes its own state protocol bump under
`state-and-recovery.md`'s numbering rule.

**D12. The measurement that decides whether it is worth building.** A
pre-check runs FAST (f seconds) before FULL (F seconds) on every producer
candidate, and saves FULL only on a candidate FAST rejects. If a share p of
producer candidates fails FULL and FAST would reject a share q of those,
the expected time per candidate goes from F to f + (1 − pq)F, a saving only
while pq > f / F. On host B, a FAST set the size of the `evidence.py`
selection above (125 seconds) against the full suite (535) would have to
reject more than 23% of all producer candidates; one the size of a single
pure module (3 seconds), under 1%. Two things are unknown and decide it.
p is in the run logs already: each producer validation launch has a step
of its own (`<PHASE>-candidate-validation`) naming the command, the
candidate commit and the exit status. q needs FAST replayed on the
candidates that failed. Stage 1 may itself lower p, since an agent that
runs `make test-fast` commits fewer broken candidates, so p is worth
measuring after stage 1 has been in use.

**D13. What any implementation must meet.** The implementing issue may add
constraints, never relax these:

1. *Configuration.* A separate trusted key (for example
   `validation.fast_commands`), empty by default, bounded as
   `validation.commands` is, candidate-tree only (D10), and never read from
   the candidate, an agent or issue text. An argv equal to a FULL
   definition's is refused at load: it would run the same command twice.
2. *Placement.* Producer gates only (`ANALYZE_EXECUTE`, `FIX`), after the
   candidate check and before FULL, on the same exact candidate, in an
   export of its own. A FAST PASS is followed by FULL in the same step,
   before any effect intent, and FULL then decides exactly as now. Never at
   the REVIEW entry, the merge gate or in `REPLAN_REEXECUTE`, whose
   rejection path has no relaunch to inform.
3. *A FAST failure* ends the step without FULL, writes no
   `ValidationRecord`, and relaunches the agent with a controller-rendered
   FAST section (provenance as ADR 0006 D9.1, the output redacted, bounded
   and fenced as untrusted, as D5.4). It counts toward the producer bound
   (3, D5.2), so FAST adds no unbounded loop. ADR 0006's counter names a
   retained FULL record (D11.1), so counting a FAST failure needs a field
   of its own or a counter whose FAST entries no evidence reader resolves:
   the implementing issue chooses, under its own protocol bump.
4. *No promotion, by type.* A FAST outcome is no `ValidationRecord` (D2).
   Persisted at all, it lives in a field of its own that no evidence
   function reads, under a definition encoding with its own version label,
   so no FAST digest can equal a FULL `definition_digest` or
   `validation_set_digest`, even for the same argv at the same index. A
   binding, certificate or counter that names one is corruption at load.
5. *No downgrade.* A FAST PASS never clears, overrides or makes historical
   a FULL `FAIL` or `TIMEOUT` (only D14.2's override does), and never
   changes FULL's set, policy or reuse.
6. *Crash.* Nothing FAST is evidence, so a crash anywhere in FAST leaves
   ADR 0006's crash windows (§2.16) as they are: resume relaunches or
   validates as now, and a lost FAST result costs one FAST run.
7. *Visibility.* `status` and the run log show FAST runs as feedback; the
   REVIEW evidence section never shows them.
8. *Provider-neutral*, as ADR 0006 D17.1.

*Rejected.*

- **FAST records in `validation_records`, told apart by a tier field.**
  Every evidence reader would have to filter them, and one that forgot
  would promote (D2).
- **Sharing a record between tiers when argv, index and policy are equal.**
  The observation would be the same, but the saving exists only for an
  operator who lists one command in both tiers, which D13.1 refuses, and
  sharing reopens what D2 closes.
- **Deferring FULL from the producer gate to the REVIEW entry or the merge
  gate.** A candidate would be published over FAST alone, and with a
  deferral to the merge gate also reviewed over it. A FULL failure after a
  clean review has
  no legal way back to `FIX` without a reviewer finding (ADR 0006 D9.4
  makes the reviewer write one), so it would need new edges, for a saving
  that exists only when FULL fails.
- **Letting the agent choose the tier** (a `CONTROL_RESULT` field asking
  for FAST only). The agent would choose the check that clears it (D3).
- **A FAST CI job in place of `test`** (D7).

## 3. Decision table

What each event does to FAST, to FULL evidence and to the certificate, and
what the controller does. "FAST" is stage 1, the agent's own run; stage 2's
rows follow the table. Every FULL cell is ADR 0006's rule, unchanged.

| Event | FAST | FULL (`ValidationRecord`) | `ReviewCertificate` | Controller |
|---|---|---|---|---|
| **Candidate changes** | | | | |
| The agent commits after a FAST pass | a claim; nothing reads it | the producer gate validates the exact commit when the set is non-empty (D5.1) | none yet | all PASS: publication; otherwise nothing is pushed (next row) |
| FAST passed, FULL fails at the producer gate | a claim | `FAIL`, terminal for (C, definition, P); the counter counts it | none | relaunch with the controller's failure section (D5.4); at 3, `BLOCKED` |
| FAST failed, the agent commits anyway | a claim | FULL decides | none | as the first row |
| New commit, force-push or human push to the PR | none | the old commit's records do not apply to the new one | inapplicable | drift to `REVIEW`; the REVIEW entry validates the new head (D5.5) |
| Same tree, another commit | none | inapplicable: the commit is part of the key | inapplicable | as above |
| Base advances, is rewritten or the PR retargets | none | applicable (candidate-tree) | as ADR 0006 D14.1 | unchanged |
| **Failures** | | | | |
| FULL `FAIL` or `TIMEOUT` at the REVIEW entry (a head no producer validated) | none | presented to the reviewer | none: a clean result over it is refused (D9.4) | the reviewer's finding leads to `FIX` |
| FULL launch, setup or materialization failure | none | no record (D10.5) | none | a bounded re-check, then `BLOCKED` |
| The operator repairs the host and unblocks | none | a `FAIL` or `TIMEOUT` at or below the watermark is historical and runs again; a `PASS` stays | unchanged | D14.2 |
| A replan agent reports `tests_passed: true` after a FAST run | a claim | the replacement is validated (D5.3) | none | a non-PASS takes the rejection path |
| **Stale evidence** | | | | |
| The operator narrows `validation.commands` after a clean review (to a FAST-sized set, say) | none | records of the removed definitions are not consulted; the others stand for their own definition and index only | inapplicable: another set digest | issuance or the merge gate: `BLOCKED` naming the set; restore and unblock, or unblock under the narrower set for a new round, whose certificate states it |
| The operator widens the set | none | the added definitions have no record | inapplicable | as above; the new round validates them |
| The policy changes (timeout, allow-list, sandbox mode) | none | inapplicable | inapplicable | as above |
| The mapping goes stale (a new module, a renamed test) | an unmapped path selects the full suite | unaffected | unaffected | none; `test_fast_targets.py` fails in the full suite until the mapping covers it |
| **Resume** | | | | |
| A crash during a FAST run | the agent's process, which ends with its invocation (ADR 0002) | unaffected | unaffected | resume relaunches the agent as today (ADR 0004 W1) |
| A crash during FULL | none | `INTERRUPTED` from the marker, run again; past 2, `BLOCKED` (D10.7) | unaffected | unchanged |
| Resume with PASS records for the same candidate | none | reused, not run again (D10.6) | unaffected | unchanged |
| A review completed from the journal | none | nothing is validated | issued from the binding (D12.2) | unchanged |
| **Merge** | | | | |
| A `READY_FOR_MERGE` or `MERGE` gate pass | not consulted | its records must exist and be PASS | must apply to the live PR, HEAD, base and merge base and to the set and policy in force (D13.2) | plus every existing gate: the whole rollup with `ci`, check definitions, protected paths, `merge.verification_commands`, `safety.allow_merge` |
| A PR narrows what `pytest` collects | none | FULL runs the narrowed suite | as for any FULL run | not detected by any tier (D8); the drift test fails when `pyproject.toml`'s options change; review is the control |
| A PR edits `.github/workflows/` | none | unchanged | unchanged | `BLOCKED` for a human, as today |

With stage 2 (§2.6) built, these rows are added and none above changes:

| Event | FAST (controller) | FULL | Certificate | Controller |
|---|---|---|---|---|
| FAST passes at the producer gate | no record | runs in the same step, and decides | unchanged | as the rows above |
| FAST fails at the producer gate | no record; its section reaches the relaunch | not run | none | the step ends; counted toward the bound of 3 |
| A crash during FAST | lost | unaffected | unaffected | as a crash before the gate today |
| `validation.fast_commands` changes | another FAST digest | unaffected | unaffected | none |

## 4. What does not change

- ADR 0006 in full: records, bindings, certificates, their encodings and
  bounds, the gates and the invalidation matrix.
- No phase, legal edge, `CONTROL_RESULT` field, prompt template, state
  field or protocol number.
- `.github/workflows/`, and the Makefile's `test`, `check`, `check-matrix`,
  `test-parallel`, `test-sandbox`, `test-sandbox-measured` and
  `dist-check`.
- `pyproject.toml`'s pytest options.

## 5. Tests

These land with this record:

- `tests/test_fast_targets.py`: every package file and test helper has an
  entry, every entry names what exists and selects something; a module
  selects its listed tests and its importers, read from every spelling of
  an import; an unmapped path and a `FULL` entry select the full suite; a
  test module selects itself and the test modules that import it (a
  removed one its importers only); a change to the live tests alone hands
  pytest their offline tests, which a real pytest collects under the
  default options while the live ones stay deselected; the change is read
  from the merge base with untracked files and both sides of a rename,
  from the commit, the index and the worktree each (a change one of them
  undoes still counts), and not from what the base did since; `--run`
  hands pytest exactly the selection and returns its
  status; `make test-fast` is in no gate and in no CI job, and CI's `test`
  runs the whole suite; the default suite deselects only `live`, with no
  other pytest option and no collection hook.
- `tests/test_evidence.py`: a PASS of a narrower selection is no evidence
  for the full definition at the same index, nor for its own argv at
  another index; complete evidence for a narrower set leaves the full set
  undecided under another digest, and a binding is reused under its own
  set only, in both directions; a FULL `FAIL` stays terminal beside a
  narrower PASS and beside a PASS on another commit with the same tree,
  until the operator's override.
- `tests/test_certificate.py`: a certificate for a narrower set does not
  apply under the full set, and one for the full set does not apply once
  the set is narrowed.
- `tests/test_validation_gates.py`: an implementer reporting that the fast
  target and the full suite passed, whose candidate fails the set in force,
  is not published; the failure is recorded and counted, and the claim is
  in no record and nowhere in the state file.

## 6. Consequences and open questions

- **The value of stage 1 is unmeasured.** Whether agents use
  `make test-fast`, and whether it lowers the producer failure rate, is
  unknown until it has been in use; D12 says what to measure before
  stage 2.
- **The mapping needs upkeep.** A new module or helper fails the drift
  test until it has an entry, which is the point, and costs a line per
  module.
- **Engine-level coverage is outside most selections.** A change to a
  module the engine reaches indirectly can pass FAST and fail the full
  suite. That is the trade FAST makes; the full suite and the controller's
  FULL run catch it.
- **Open: the agent's own full run.** `AGENTS.md` asks an agent to run the
  configured verification before declaring work complete. When the
  operator's `validation.commands` already holds the full suite, the
  agent's own full run duplicates the controller's, and dropping it would
  save one full run per producer attempt. It also catches a failure before
  it costs a relaunch, and the repository cannot know the operator's
  configuration, so this record leaves the rule as it is; changing it is
  the maintainer's call.
