# Review corpus and offline reviewer benchmark (REV-R1)

**Status:** corpus format version 1, its validator, batched read-only git verifier, and offline scorer, a synthetic fixture, and an **unadjudicated** seed of eight real candidates from this repository (#356). **No label is settled, no reviewer has been run on any case, and no benchmark result exists.** Nothing here changes `src/`, prompts, defaults, state schema, controller authority, `ReviewCertificate`, merge gates, or GitHub effects.

[REV-R0](review-evaluation-protocol.md) replays the controller's REVIEW/FIX loop to evaluate loop changes. Comparing *reviewers* (model, prompt, or effort) requires frozen candidates shown at their exact review-time inputs alongside defects known independently of any reviewer. This document specifies that format (`autoforge-review-corpus` v1) and offline evaluator, reusing REV-R0's severity weights (`critical: 8`, `high: 4`, `medium: 2`, `low: 1`), defect classes, and exclusion reasons, and leaving any future real model runs to [#318](evaluation-protocol.md).

---

## 1. Corpus Schema & Identities (`autoforge-review-corpus` v1)

A corpus JSON file pins repository metadata, frozen ref tips, declared validation environment, and keyed cases. All objects use closed schemas (unknown or duplicate JSON keys and non-finite numbers are refused).

- **Repository & Environment (`repository.environment`):** Declares `files`, `validation_scope` (must be `"full"`; `"fast"` is always refused per ADR 0006/0010), and `validation` commands (a non-empty command list or `[]` for explicitly unconfigured validation, never treated as a validation pass). Works for any repository without hardcoding repository roots in Python source. Each case pins the exact blob OID for each declared file at `head` (or `null` when absent at that head); a case whose declared file list is non-empty but pins every file to `null` is never `runnable`.
- **Topologies & Provenance (`provenance` + `review`):**
  - `pr_branch`: a review round (`issue >= 1`, `round >= 1`) on a PR branch. `review` pins `base_ref`, `base_tip`, `merge_base`, `head`, `base_tree`, `merge_base_tree`, `head_tree`, and UTC `cutoff`. `base_evidence` is `"recorded"` (evidenced review-time boundary) or `"provisional"` (derived from git committer times; never runnable).
  - `squash`: a one-parent squash commit on `base_ref` (`pr >= 1`, `round: null`, `base_evidence: "squash_parent"`, `base_tip == merge_base`, `merge_commit in (null, head)`).
  - `merge_pr`: a two-parent merge commit on `base_ref` (`pr >= 1`, `round: null`, `base_evidence: "merge_parent"`, `merge_commit` non-null with parents `(base_tip, head)`). Every defect of a `merge_pr` case is located by its lines, and `verify` requires each located block of the head to appear exactly once in the merge commit's own tree: parent topology alone does not show that a conflict resolution kept the defect, so a defect the merge resolved away never counts as landed with the PR, even when a later commit reintroduces the lines a still-later fix changes.
  - `synthetic`: stand-in hashes for offline arithmetic tests (`mode: "synthetic"`, `base_evidence: "synthetic"`).
- **Strata:**
  - `reviewed_defect`: candidate reviewed at a round holding at least one `same_review` or `later_review` defect.
  - `escape`: landed candidate with at least one `post_merge` defect.
  - `revert`: landed candidate with at least one `revert` defect.
  - `hard_negative`: candidate on a defect surface with `defects: {}`.
  - `clean`: candidate with `defects: {}`.
- **Corpus-wide identity invariants:**
  - Every `(base_ref, base_tip, merge_base, head)` target appears at most once; every `input_digest` is unique.
  - One review round of a PR `(pr, round)` — or of an issue `(issue, round)` when `pr` is `null` — is at most one case.
  - One PR merges as at most one commit across `squash` heads, `merge_pr` merge commits, and `merged` lineage pointers.
  - A `defect_id` names one canonical defect across the corpus (same PR/issue scope, `introduced` commit, paths, discovery round/finding/day/fix/merge, severity, class, summary, and settled status); only line spans and `same_review`/`later_review` classification may vary across heads of one PR.
  - Cases sharing `issue`, `pr`, `introduced`, `fix`, `merged`, or `merge_commit` must share one `cluster`.
  - A defect's `discovered` day cannot precede the cutoff day of the case matching the round it credits.

---

## 2. Offline Trust Boundary: Cutoff, Leakage & Provenance

This specification separates **what offline tooling enforces deterministically** from **what requires external capture and human adjudication (#592)**:

1. **Reviewer-visible allow-list projection (`project_reviewer_input`):** Returns only `format`, `version`, `review`, `environment`, and `{id, kind, source, sha256}` per context item. Curator metadata (`ref`, provenance notes, `withheld`, `post_cutoff_refs`, `actors`, `risk`, `exposure`, `stratum`, `defects`, `negative`) is strictly excluded from `project_reviewer_input` and `input_digest`.
2. **Deterministic O(N) leakage checks (`check_text_leakage`):**
   - `observed_at <= cutoff` and (if present) `edited_at <= cutoff` for every captured context item; `sha256` must match item `text`.
   - Required singular items (`issue` when `provenance.issue` is set; `pr_body` when `provenance.pr` is set or source is `pr_branch`/`squash`/`merge_pr`) must be present; when `inventory.status == "complete"`, `sources` must match the exact set of captured item sources and `cutoff <= taken`. A case is `runnable` only when every context item is captured and the inventory records `edit_history_verified: true`; a complete inventory with unverified edit history validates but leaves the case unrunnable, because a null `edited_at` does not show that no edit happened after the cutoff.
   - Single-pass linear scan over reviewer-visible text (`base_ref`, env paths, validation commands, item `id`/`kind`/`source`/`text`) rejects any 7+ hex run containing the first 7 hex digits of any `withheld` commit or cross-case future-round SHA on the same PR/issue (`check_cross_case_leakage`), plus any explicit `post_cutoff_refs` substring.
   - **No Markdown renderer or prose-similarity matching is attempted offline.** Authentic review-time GitHub snapshots and edit histories belong to #592.
3. **Read-only batched Git provenance verification (`verify`):**
   - Runs read-only `git` with minimal allow-listed environment (`GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_NO_LAZY_FETCH=1`) and hardened flags (`--no-replace-objects`, `protocol.allow=never`, `core.hooksPath=/dev/null`, `core.fsmonitor=false`, `core.commitGraph=false`, `gc.auto=0`, `maintenance.auto=false`).
   - Loads and SHA-1-authenticates all referenced commits and environment blobs in a batched `git cat-file --batch` pass, checking `head_tree`, `base_tree`, `merge_base_tree`, `repository.root` ancestry, exact `merge_bases(base_tip, head) == [merge_base]`, topology (`pr_branch`, `squash`, `merge_pr`), blob matches at `head`, non-ancestry of `withheld` commits in `head` and `base_tip`, `introduced` in `merge_base..head`, defect line bounds, and fix/merge lineage (`addresses`, `found_once`, `utc_day(landed) <= discovered` for `post_merge`/`revert`).
   - `verify` checks **local git object integrity and structural consistency**, not GitHub authenticity (e.g., whether a PR body was edited after capture without `edit_history_verified: true`, or whether a curator paraphrased a fix without citing a SHA or `post_cutoff_refs` entry).
4. **Structured JSON pins (no synthetic prose records):** Provenance rests on structured JSON fields with explicit `base_evidence` (`recorded` vs. `provisional`) and `discovery.evidence_status` (`recorded` vs. `provisional`). Future #592 capture populates real GitHub context and flips `base_evidence`/`evidence_status` to `"recorded"` without synthesizing bespoke prose comment lines.

---

## 3. Adjudication, Training Exposure & Censoring

- **Independence:** No case actor (`actors`) and no registered reviewer variant identity/name (`variants` in submissions) may adjudicate any corpus defect, corpus negative, or finding match in any role. Self-reported model correctness is refused.
- **Curators vs. Adjudicators vs. Arbiters:** `curator` labels propose and never settle anything. `adjudicator` labels settle by unanimous agreement (1 required normally; **2 distinct independent adjudicators required for any `"critical"` defect or finding match to a critical defect**). `arbiter` labels are allowed only on a disagreement between adjudicators; all arbiters must agree, and the chosen verdict must already have at least `max(1, need(verdict))` adjudicator votes (arbiters cannot invent an unvoted 3rd verdict or bypass the 2-adjudicator rule for critical defects). Unresolvable outcomes stay `unknown` and are never coerced.
- **Cutoffs & clean negatives:** Every verdict `decided` date must satisfy `case.cutoff.date() <= decided <= frozen`. A negative case is `"settled"` only when `defects == {}`, settled verdict is `"clean"`, `oracle_status == "pass"`, and `observed_through - cutoff.date() >= 30` days. A `"defective"` verdict marks the negative `"refuted"` and `runnable: false`.
- **Training exposure (`exposure`):** Reported per completed run as `"none"` only when `knowledge_cutoff` is non-null and strictly before `possible_from`; `"possible"` when `knowledge_cutoff >= case.public` (computed from non-rejected defects or `cutoff.date()`, bounded by `public_by` and verified against `head`/`introduced` commit days); and `"unknown"` otherwise. Missing dates never default to `"none"`.

---

## 4. Scoring, Option 3A Recall Bounds & Work Limits

- **Scorecards (`score`):** Historical corpora require `verify` to pass before scoring. Each variant gets exact `Fraction` metrics (`recall`, `precision`, `false_positive_rate`, `recall/severity/*`, `recall/discovery/*`, `recall/class/*`, `recall/risk/*`, `precision/classification/*`), `safety` tallies (`missed_critical_defects`, `clean_verdict_critical_escapes`), `training_exposure`, and `cost_microusd` (`None` if any launched run lacks cost).
- **Option 3A Recall Uncertainty Bounds:** Unlike naive filtering that drops unsettled (`unknown`/`disputed`) registered defects, `recall_rate` represents each run's recall as a 5-tuple `(det, extra_c, extra_u, conf_total, unset_total)`:
  - `point = det / conf_total` (undefined when `conf_total == 0`)
  - `low = det / (conf_total + unset_total)` (worst case: all unsettled defects are real and missed)
  - `high = (det + extra_c + extra_u) / (conf_total + extra_u)` when `conf_total > 0` (`1` if `extra_u > 0` else `0` when `conf_total == 0`)
  - When `unset_total == 0`, this identically matches REV-R0 `rate(..., recall=True)`. Whenever unsettled critical/high defects widen `high - low > 1/10`, `label_bands_within` is `False` and `usable` is `False`.
- **Cluster Bootstrap:** Deterministic 10,000-draw paired cluster bootstrap (`seed = 356`, nearest-rank 2.5th/97.5th percentiles). Undefined draws are counted and never replaced with zero. A contrast is `usable` only when `incomplete == []`, `clusters >= 2`, `difference` is defined, `label_bands_within` is `True`, and `undefined_draws == undefined_envelope_draws == 0`.
- **Work Limits:** Pre-expansion caps refuse oversized inputs before bootstrap or git work: `MAX_FILE_BYTES = 8 MiB`, `MAX_CASES = 1,000`, `MAX_VARIANTS = 8`, `MAX_PLANNED_PAIRS = 10,000`, `MAX_PLANNED_CLUSTERS = 64`, `MAX_FINDINGS_PER_RUN = 500`, `MAX_REVIEWER_TEXT_BYTES = 512 KiB`. Leakage scanning is strictly linear $O(|\text{text}|)$.

---

## 5. Unadjudicated Seed (`autoforge_seed.json`, Release 1) & #592 Handoff

`tests/fixtures/review_corpus/autoforge_seed.json` pins **8 real historical candidates** from this repository (frozen `2026-11-27`, `mode: "historical"`, 3 clusters, 20 defect registrations of 12 distinct defects, 0 runnable cases):

| Case | Topology | Cluster | Defects (same / later) | Risk | Status |
|---|---|---|---|---|---|
| `issue262-round1` | `pr_branch` | `issue-262` | 1 / 0 | `routine` | Unadjudicated (`provisional`, `not_captured`) |
| `pr459-round1` | `pr_branch` | `issue-414` | 4 / 0 (1 critical) | `critical` | Unadjudicated (`provisional`, `not_captured`) |
| `pr460-squash` | `squash` | `issue-414` | 0 (`hard_negative`) | `critical` | Unlabeled (`oracle_status: not_run`, `not_captured`) |
| `pr539-round1` | `pr_branch` | `issue-329` | 1 / 4 | `critical` | Unadjudicated (`provisional`, `not_captured`) |
| `pr539-round2` | `pr_branch` | `issue-329` | 3 / 2 | `critical` | Unadjudicated (`provisional`, `not_captured`) |
| `pr539-round3` | `pr_branch` | `issue-329` | 1 / 2 | `critical` | Unadjudicated (`provisional`, `not_captured`) |
| `pr539-round4` | `pr_branch` | `issue-329` | 2 / 0 | `critical` | Unadjudicated (`provisional`, `not_captured`) |
| `pr540-squash` | `squash` | `issue-329` | 0 (`hard_negative`) | `critical` | Unlabeled (`oracle_status: not_run`, `not_captured`) |

**Known limitations & #592 scope:** Every seed defect carries `curator` proposals only; `context` items and inventories are `not_captured`; no `escape`, `revert`, or `clean` historical case is curated yet; `pr_branch` cutoffs/base revisions are `provisional` (head committer time); and `exposure.possible_from` is `null` (`unknown` exposure). Capturing review-time GitHub snapshots with edit history, settling labels independently (two adjudicators for `r1-f1-owner-removed`), and curating escape/revert/clean cases belong to **#592 (REV-R1b)**.

---

## 6. CLI & Pinned Fixture Digests

```sh
uv run python -m tests.review_corpus describe
uv run python -m tests.review_corpus verify --repo .
uv run python -m tests.review_corpus score
uv run python -m tests.review_corpus export-input tests/fixtures/review_corpus/synthetic_corpus.json alpha-defect
uv run pytest tests/test_review_corpus.py
```

Fixture SHA-256:

- `autoforge_seed.json`: `3ada1901e6daec2616133cb26d20d81f6425bb182f6597e9e44b4b2dfe0099e0`
- `synthetic_corpus.json`: `0241b055e229ab522d7be6b75e9a3e7742dba54f1b3dc561bc359370ea178feb`
- `synthetic_submissions.json`: `16a0e088fe9efa4e6eced4b142ef822bbf3a4616512cd60d851365e58d974edf`
