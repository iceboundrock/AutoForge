# CONTROL_RESULT protocol and review result contract

Read this before changing `src/autoforge/result_parser.py`, the per-phase
required fields of a control result, any prompt template under
`src/autoforge/prompts/` that tells an agent what to emit, the correction
retry for malformed results, or how the engine consumes a parsed result.

---

## CONTROL_RESULT protocol

Agent stdout may contain normal logs and prose, but every successful phase invocation must end with exactly one machine-readable control block:

```text
<<<CONTROL_RESULT>>>
{"phase":"REVIEW","status":"success"}
<<<END_CONTROL_RESULT>>>
```

Controller behavior:

1. Find complete control-result blocks.
2. Use only the last complete block.
3. Reject a block larger than `MAX_CONTROL_RESULT_CHARS` by size alone.
4. Parse strict JSON.
5. Require a JSON object.
6. Validate required fields for the current phase.
7. Reject phase mismatch.
8. Reject schema/invariant mismatch.
9. Do not advance state when validation fails.

The stdout the parser searches is bounded and, when the agent wrote more
than the executor's capture bound, it is only the *tail* of that stdout: the
part captured contiguously up to EOF. See "Whole-block bound and truncated
stdout" below.

Never infer workflow state by searching natural-language output for phrases such as:

```text
done
LGTM
looks good
merged successfully
```

### Block markers, not fences

The parser locates the block by its `<<<CONTROL_RESULT>>>` /
`<<<END_CONTROL_RESULT>>>` markers alone (`_BLOCK_RE`), so a Markdown code
fence around the block is neither required nor harmful: the schema examples
in every phase prompt are shown inside a fence, and an agent that copies
that shape emits a block the parser reads. The common prompts say so rather
than forbid the fence (#19); `tests/test_result_parser.py` pins that a
fenced block is accepted, so the prompt and the parser cannot drift on it
again.

### SHA fields

Every SHA field (`head_sha`, `reviewed_head_sha`, `previous_head_sha`,
`replacement_head_sha`, a FIX resolution's optional `commit_sha`) must be the **full 40-character** hexadecimal object id, and
is lower-cased on acceptance (`_SHA_RE`). The parser holds the agent to the
same rule the prompts state and `autoforge.claims` applies to a marker: the
controller compares each accepted SHA by equality with one read from GitHub
(`git rev-parse HEAD`, `gh pr view --json headRefOid`), so an abbreviated
SHA could only pass the schema and then fail that comparison as a
"SHA mismatch", which is a verification error about the wrong thing. It is
refused at parse time instead, as the schema error it is, through the
ordinary correction retry.

### Review required fields

A `REVIEW` result carries, besides `phase` and `status`:

```text
round                 integer; must equal the round the controller launched
reviewed_head_sha     40-hex SHA; must equal the HEAD the controller bound
needs_fix_round       boolean; see the review invariant
findings              list of findings (bounded; see below)
spec                  prose: does the change do what the issue asks
standards             prose: does it follow the repository's conventions
assessment            prose: the overall assessment
observations          prose: what is worth noting that is not a finding
verification          prose: what the reviewer ran or read, and its outcome
summary               prose: the verdict in a few lines
```

A REMOTE reviewer publishes nothing (#162): the controller renders the
round's review comment from the result and posts it itself
([github-safety.md](github-safety.md), "After REVIEW"), so the result
names no comment. `round` and `reviewed_head_sha` are cross-checks, never
targets: a value other than the round and HEAD the controller bound is a
validation error the reviewer is asked to correct (ADR 0004 D8.1), not a
verification failure. Each prose section is non-blank, multi-line
printable text bounded by `MAX_REVIEW_SECTION_CHARS` (`summary` by
`MAX_REVIEW_SUMMARY_CHARS`), and is rejected, never clipped. Every prose
section and every finding's `title`, `location` (judged as the code span
the comment renders it in) and `required_resolution` pass the
published-content policy (`published_text_problem`;
[github-safety.md](github-safety.md), "Published content"), and the rendered
comment as a whole must pass the credential rule, pass the mention rule
before its marker (`published_markdown_problem`: one field's unclosed fence
or raw HTML can leave another field's mention outside code), and fit
GitHub's comment limit (ADR 0004 D8.2, D8.3, D8.6); a refusal is a
correction with nothing posted. A LOCAL `REVIEW` is unchanged: it carries no prose sections and
publishes nothing.

### Review invariant

For `REVIEW`:

```text
needs_fix_round == (number of actionable findings > 0)
```

Therefore both of these are invalid:

```text
findings=[] and needs_fix_round=true
findings=[...] and needs_fix_round=false
```

### Review payload bounds

Review output is untrusted project data, and every accepted finding is
persisted in full in `state.open_findings` (a full atomic rewrite of
`state.json`) and rendered verbatim into the next FIX prompt, or, when the
round went stale, in `state.prior_findings` and rendered into the next
REVIEW prompt as findings to re-check (same shape, same bounds). The parser
therefore bounds what a `REVIEW` result may carry (`src/autoforge/result_parser.py`):

```text
MAX_FINDINGS_PER_REVIEW         findings per review round
MAX_FINDING_RESOLUTION_CHARS    required_resolution length (after stripping)
MAX_FINDING_TITLE_CHARS         title length
MAX_FINDING_LOCATION_CHARS      location length
MAX_FINDING_ID_CHARS            id length (checked before the id's shape)
```

The id bound exists because the id's shape (`R<round>-F<n>`) does not bound
its length, and the id is persisted and rendered like every other finding
field. It is checked before the shape and round checks so an oversized id is
never quoted in a rejection message. A `FIX` resolution's `finding_id` must
equal an accepted finding's id, so the same bound is applied to it at parse
time rather than after the engine's coverage check.

The failure mode is **rejection, never clipping**: the findings are the work
the FIX round has to act on, so clipping them would silently drop work,
whereas a rejected `CONTROL_RESULT` is correctable (the reviewer re-emits a
bounded result through the ordinary correction retry) and leaves state
unchanged. The rejection message states the offending size and the limit,
never the oversized text. The count is checked before any element is parsed.

Each parser bound is at or below the corresponding persisted-evidence bound
in `loop_guard` (`MAX_PERSISTED_FINDINGS_PER_ROUND`,
`MAX_PERSISTED_RESOLUTION_DIGESTS`, `MAX_REQUIRED_RESOLUTION_CHARS`), so a
round the parser accepted is always retained complete in `review_history`;
the `loop_guard` clipping and its truncation markers remain as a defence for
state persisted before the parser bounds existed or edited outside the
controller. The resolution bound is not merely *equal* to the parser's: the
engine redacts every finding between the parser and the history, and
redaction can lengthen a text (a one-character secret becomes the
14-character marker), so `MAX_REQUIRED_RESOLUTION_CHARS` is computed as the
parser bound times `redaction.MAX_GROWTH_FACTOR`, not restated as a number,
so neither input can drift from it. Otherwise a resolution at the parser
bound that quotes a token would be clipped after it was accepted, the round
marked truncated, and every later replan of that PR refused (#33). The
review prompts state every parser bound (the id bound included) through
template variables that the engine fills from the same constants, so the
number the reviewer is told is the number it is held to.

### Control characters in finding text

A finding's one-line fields (`title`, `location`) may carry **no control
character at all**; `required_resolution` and a `FIX` `rationale` may carry a
newline or a tab and nothing else (#78). The class is
`prompts.CONTROL_CHARS` -- C0 and C1 controls, DEL, and the Unicode line and
paragraph separators -- defined once: `escape_inline` renders exactly those
characters as `\xNN` / `\uNNNN` escapes when a one-line field is placed in
a FIX prompt, and the parser imports the same class to refuse them at parse
time, so the set a reviewer is held to is the set the renderer would
otherwise have to rewrite. The reason is the same as for the size bounds: a
value the controller must alter before it can show it is not the reviewer's
finding any more, and the renderer's escape was a containment, not a
rendering the fixer was meant to read. A tab is refused in a one-line field
(it is inside the escaped class, and a title has no use for one) and kept in
a multi-line field (it is ordinary indentation in a quoted snippet). A
rejection names the field, the code point (`U+XXXX`) and its index into the
stripped value, never the text, and the length bound is checked first so
that index is always into a value of accepted size. `escape_inline` and the
resolution indentation stay in the renderer unchanged, as the defence for
state persisted by a controller without this rule or edited by hand. Fields
that are not rendered on one line of a prompt (`summary`, `message`,
`tests_attempted`, LOCAL `observations`) keep their existing rules; the
one-line `ANALYZE_EXECUTE` fields are under the rule ("ANALYZE_EXECUTE
fields" below).

### Fix payload bounds

A `FIX` result is untrusted in the same way and is persisted the same way:
every accepted resolution is stored whole (after redaction) in
`state.last_fix_resolutions`, and in LOCAL mode an `unresolved` rationale is
echoed into the persisted `block_reason` that `status` shows the operator
(the echo of every unresolved rationale together is bounded again at the
sink, by `state.MAX_BLOCK_REASON_CHARS`, since the parser bounds each
rationale but not their join; `last_fix_resolutions` keeps them whole).
The parser therefore bounds the `FIX` payload with the same policy
(rejection, never clipping; the message states the size and the limit,
never the text; the count is checked before any element is parsed):

```text
MAX_RESOLUTIONS_PER_FIX         resolutions per FIX (== MAX_FINDINGS_PER_REVIEW)
MAX_FIX_RATIONALE_CHARS         rationale length (after stripping), both modes
MAX_URL_CHARS                   any URL field, checked before the URL is parsed
```

The resolution count is equal to the finding count by construction: the
engine already refuses a resolution whose `finding_id` is not an open
finding, so a `FIX` can never legitimately carry more resolutions than the
controller accepted findings, and the count bound only moves that refusal
before the elements are parsed. The rationale bound is pinned below the
persisted `MAX_REQUIRED_RESOLUTION_CHARS` through `redaction.MAX_GROWTH_FACTOR`
in the same test that pins the review bounds, so a redacted rationale is
never larger than a redacted resolution. A `commit_sha`, when present, must
have the shape of a git SHA (`_SHA_RE`, the same rule as every required SHA
field; see "SHA fields" above); a SHA rejection quotes the value only when
it is short enough to be one and otherwise states its length. Every URL
field (`follow_up_issue_url`,
`issue_url`, `next_issue_url`) is bounded by
length before `validation.parse_*` sees it, because those parsers quote the
URL in their error and that error reaches the correction prompt and the run
log; a malformed `follow_up_issue_url` is thereby a validation error of the
`FIX` result rather than a `ConfigurationError` raised later by the engine,
and a malformed `next_issue_url` a validation error of the `UPDATE_EPIC`
result rather than a selection for the engine to reject: the shape of the
field is the parser's, which issue it names is the engine's
([github-safety.md](github-safety.md), "After FIX" for the issues a
deferral may name, "After UPDATE_EPIC"), and an
oversized value is never quoted into `next_issue_rejections`, the
re-selection prompt or the run log. The fix prompts state the bounds
through template variables filled from the same constants.

### ANALYZE_EXECUTE fields

The agent publishes nothing in `ANALYZE_EXECUTE` (ADR 0004, #161): it
commits on its worktree's detached `HEAD`, and the controller pushes that
commit to `autoforge/<n>` and opens (or adopts) the PR with the agent's
text. The result therefore names no PR, branch or push target; `pr_url`
and `branch` are not keys of this phase any more.

| Key | Rule |
|---|---|
| `issue_url` | required issue URL, bounded by `MAX_URL_CHARS`; the engine compares it with the run's issue by identity (repository and number), never as a string |
| `head_sha` | required SHA ("SHA fields" above); a cross-check only: the engine reads the worktree's `HEAD` itself and refuses a value that differs from it |
| `pr_title` | required, one line (no control character), at most `MAX_PR_TITLE_CHARS` (256) |
| `pr_body` | required, multi-line (newline and tab only), at most `MAX_PR_BODY_CHARS` (60000), leaving room for the two lines the controller appends under GitHub's body limit |
| `tests` | optional list of one-line strings, at most `MAX_TESTS_REPORTED` (50) entries of `MAX_TEST_CHARS` (500); recorded, never published |

Every bound rejects, never clips, and the message states the size and the
limit, never the text. `pr_title` and `pr_body` pass the published-content
policy (`published_text_problem`; [github-safety.md](github-safety.md),
"Published content"): no controller marker opener, no credential class, no
closing keyword followed by an issue reference (the controller writes
`Closes #n` itself, after the body), and no `@`-mention outside code. What
the parser cannot see is the engine's: the worktree's `HEAD` (detached,
equal to `head_sha`, descending from the recorded base and branch head),
the commit messages of the published range (`commit_message_problem`), and
the composed body (the agent's body, `Closes #n` and the marker) judged as a
whole with `published_payload_problem`. Each refusal is an ordinary
correction with nothing pushed or created
([github-safety.md](github-safety.md), "After ANALYZE_EXECUTE").

### FIX fields

The fixer publishes nothing in REMOTE `FIX` (ADR 0004, #163): it commits on
its worktree's detached `HEAD`, and the controller creates the follow-up
issues it asks for, appends the deferral markers and pushes that commit to
the PR branch. The result therefore names no push target; `new_head_sha` is
not a key of this phase any more.

| Key | Rule |
|---|---|
| `previous_head_sha` | required SHA; a cross-check only: the engine refuses a value other than the reviewed HEAD the open findings are bound to |
| `head_sha` | required SHA; a cross-check only: the engine reads the worktree's `HEAD` itself and refuses a value that differs from it |
| `resolutions` | required list, at most `MAX_RESOLUTIONS_PER_FIX`, distinct `finding_id`s; the engine refuses any set other than exactly the open findings |
| `resolutions[].resolution` | `fixed`, `follow_up_created` or `no_change_with_rationale` |
| `resolutions[].commit_sha` | optional SHA, on `fixed` only; the engine refuses one outside the commits after the reviewed HEAD up to `head_sha` |
| `resolutions[].follow_up_issue_url` | on `follow_up_created` only, exclusive with `follow_up_issue`: an issue URL bounded by `MAX_URL_CHARS`; the engine refuses one it did not hand over and the current issue |
| `resolutions[].follow_up_issue` | on `follow_up_created` only, exclusive with `follow_up_issue_url`: an object with `title` (one line, at most `MAX_FOLLOW_UP_TITLE_CHARS`, 256) and `body` (multi-line, at most `MAX_FOLLOW_UP_BODY_CHARS`, 60000), both non-blank |
| `resolutions[].rationale` | at most `MAX_FIX_RATIONALE_CHARS`; at least `MIN_RATIONALE_CHARS` on `no_change_with_rationale` |
| `tests` | optional list of one-line strings, at most `MAX_TESTS_REPORTED` (50) entries of `MAX_TEST_CHARS` (500); recorded, never published |

A new follow-up issue's `title` and `body` pass the published-content
policy (`published_text_problem`), so a body carries no marker: the
controller follows it with its own reference to the PR and finding and the
finding's marker, and judges that composed body as a whole with
`published_payload_problem`. What the parser cannot see is the engine's:
the worktree's `HEAD` (detached, equal to `head_sha`, the reviewed HEAD or a
descendant of it), the commit messages of the published range
(`commit_message_problem`), a `fixed` finding with no commit, and which
issue a deferral names. Each refusal is an ordinary correction with nothing
created, appended or pushed ([github-safety.md](github-safety.md), "After
FIX"). The persisted `last_fix_resolutions` keeps its shape: a deferral is
recorded with the URL of the issue GitHub read back, whichever form asked
for it. LOCAL `FIX` is unchanged: it has no follow-ups and its own
resolutions (`LOCAL_FIX_RESOLUTIONS`).

### REPLAN_REEXECUTE fields

The replan agent publishes nothing (ADR 0004 K1 and K7, #164): it commits on
its worktree's detached `HEAD`, built on the default branch head the
controller fetched and named, and the controller pushes that commit to the
branch it derives from the issue and the transaction id and creates the
replacement PR with the agent's text. The result therefore names no
replacement PR, branch or push target; `replacement_pr_url`,
`replacement_branch` and `replacement_head_sha` are not keys of this phase
any more.

| Key | Rule |
|---|---|
| `issue_url`, `previous_pr_url`, `previous_branch`, `previous_head_sha`, `execution_attempt` | required; cross-checks only: the engine refuses any value other than the transaction's checkpoint (URLs by identity) |
| `head_sha` | required SHA; a cross-check only: the engine reads the worktree's `HEAD` itself and refuses a value that differs from it |
| `historical_findings_considered`, `unique_failure_constraints` | required non-negative integers, the second at most the first; the engine refuses fewer findings than the controller preserved, and renders both into the transaction marker it publishes |
| `verification` | required object: `tests_run` (list of strings) and `tests_passed` (boolean); the engine rejects `false` |
| `previous_pr_disposition`, `fresh_review_round` | required, exactly `superseded` and `1` |
| `pr_title`, `pr_body` | the bounds, line rules and published-content policy of `ANALYZE_EXECUTE`'s (above), shared through `validate_pr_title` / `validate_pr_body` |

`pr_body` carries no marker: the controller follows it with `Closes #n`,
the issue's implementation marker and the transaction marker it renders
from the validated counts (`replacement_pr_body`), and judges that composed
body as a whole with `published_payload_problem`. What the parser cannot
see is the engine's (`_check_replan_result`), and it splits two ways. What
the agent can repair is an ordinary correction with nothing pushed or
created: the composed body, a `HEAD` attached to a local branch or not
readable, and the commit messages of the published range
(`commit_message_problem`). What contradicts the checkpoint is a rejection,
persisted as `REJECTED` before any effect: a cross-check that does not
match, `tests_passed: false`, too few findings considered, a `head_sha`
that is not the worktree's `HEAD`, and a `HEAD` that is the recorded base
itself or does not descend from it
([replan-transaction.md](replan-transaction.md), "The controller publishes
the replacement").

### UPDATE_EPIC fields

The agent publishes nothing in `UPDATE_EPIC` (ADR 0004): the controller posts
`progress` as the EPIC progress comment and writes `roadmap_section` into the
EPIC body, so both are validated as text that will be published under the
operator's identity.

`progress` is required in the phase's launch result (the `FULL` request of
`result_parser.UpdateEpicRequest`): non-blank, multi-line text with no other
control character, at most `MAX_PROGRESS_CHARS` (16384, rejected by size,
never clipped), with no marker and no URL at all: `://` of any scheme or a
`www.` host, refused even inside code (`_PROGRESS_URL_RE`, #160). The
controller appends the `ai-epic-progress` marker itself, and the marker
names the issue and the PR; the prompt asks for `#n` references instead of
links. `roadmap_section` is not under this rule and may link PRs.

`progress` and `roadmap_section` both pass the published-content policy
(`published_text_problem`; [github-safety.md](github-safety.md),
"Published content"): no controller marker opener (`<!-- ai-` or
`<!-- autoforge-`, any spacing or case, `AGENT_MARKER_OPEN_RE`), no
credential class (named, never quoted), no closing keyword followed by an
issue reference, and no `@`-mention outside code. The engine also judges the
comment exactly as it would be posted (the text, a blank line, the marker)
with `published_payload_problem` before anything is saved or posted, and a
refusal is an ordinary correction.

A re-request (D4.7, `prompts/update_epic_rerequest.md`) is launched only
after the progress comment is published, for an input a persisted rejection
voided. Its result carries exactly the keys of its request, and any other
key is refused, `progress` included, even as `null`. The refusal counts the
unknown keys without quoting them:

| Request | Keys |
|---|---|
| `SELECTION` | `next_issue_url` (required key) |
| `SELECTION_WITH_ROADMAP` | `next_issue_url` (required key), `roadmap_section` (optional) |
| `ROADMAP` | `roadmap_section` (required, non-blank) |

`next_issue_url` is required (`null` when the EPIC is complete) and shaped
as above. `roadmap_section` is optional at parse time: absent, `null` or
blank is "none returned"; a present value must be a string, is bounded by
`MAX_ROADMAP_SECTION_CHARS` (rejected by size, never clipped: the controller
writes it verbatim into the EPIC body, so a truncated roadmap would be
published), may contain newlines and tabs but no other control character,
and must not contain any `<!-- ai-` controller marker (the roadmap markers
are written by the controller around the section; any other marker would
plant a durable claim in an open issue the controller scans). The refusal
is the scanner's own opening, `CONTROLLER_MARKER_OPEN_RE` (`<!--`, any
whitespace or none, `ai-`), on which every `autoforge.claims` marker
pattern is built, so `<!--ai-` and `<!--\nai-` are refused exactly because
the scanner would read them. Whether a
section is *required* is the engine's decision from persisted state
(`workflow.epic_update_every`, or the EPIC being reported complete), made
after the result parses ([github-safety.md](github-safety.md), "EPIC
updates"). A required section that is missing is a
`ControlResultValidationError` ("missing required field 'roadmap_section'"),
so it takes the correction path before anything is posted.

### Whole-block bound and truncated stdout

The accepted payload is persisted whole (`control-result.json` and one
`events.jsonl` line per invocation) and its fields are what the next phase
acts on, so the block as a whole is bounded where it is accepted:
`MAX_CONTROL_RESULT_CHARS` (`src/autoforge/result_parser.py`) is the most
raw JSON text the parser will decode. It is checked by size alone, before
`json.loads`, and an oversized block is rejected, never clipped, through the
same correction retry as any other malformed result; the rejection states the
size and the limit, never the text. The bound sits above the largest `REVIEW`
the field bounds admit in the worst JSON encoding (every character escaped as
`\uXXXX`), and `tests/test_result_parser.py` pins that relation, so the
field bounds -- not the block bound -- are what a reviewer is held to. The
common prompt states the block bound through a template variable filled from
the same constant.

Agent stdout is itself bounded before the parser sees it. The executor keeps
at most `DEFAULT_MAX_OUTPUT_BYTES` (`src/autoforge/executor.py`) of each
stream -- the first half and the last half of what the agent wrote, with an
omission marker between -- so a runaway or adversarial transcript costs the
controller a bounded amount of memory, not its size. The block is the last
thing the agent writes, so the kept tail preserves a legitimate one. The
engine therefore searches only `stdout_tail`, the part captured contiguously
up to EOF, never the head: a block before the cut is either stale (the agent
wrote more after it) or spans the cut (head, marker and tail could assemble
into a document the agent never wrote), and neither is the agent's final
result. When the search of a truncated stdout fails, the rejection says so,
so the correction prompt tells the agent to keep its transcript short and end
it with the block. The whole marked stdout still reaches `stdout.log`, and
`execution.json` records `stdout_truncated` / `stderr_truncated`.

The FIX prompt these findings are rendered into is bounded by a constant
factor of the parser bounds, not by their sum: the renderer's safety measures
each cost characters (a control character in a one-line field becomes its
`\xNN` or `\uNNNN` escape, a newline inside a `required_resolution` is
indented under its finding, and the fence grows one past the longest backtick
run in the findings). The engine test for the largest round covers each of
those shapes, not only plain text; the shapes the parser now refuses (#78)
are seeded into state directly, standing for state an older controller
persisted, and the bound must still hold for them.

---

## Findings versus observations

A **Finding** means the current PR lifecycle still requires an explicit action.

Finding severity may be:

- blocked
- non-blocked
- nit

Severity does not change workflow behavior. If there is any actionable finding, another FIX round is required.

Use **Observations** for:

- optional improvements
- future ideas
- educational notes
- informational comments
- preferences that do not require action in the current lifecycle

Do not create endless review loops by labeling every optional suggestion as a Finding.

Finding IDs should remain stable and explicit, for example:

```text
R1-F1
R1-F2
R2-F1
```

---

A `CONTROL_RESULT` that validates is still only a *claim*: what the controller
must independently re-verify on GitHub after each phase is in
[github-safety.md](github-safety.md).
