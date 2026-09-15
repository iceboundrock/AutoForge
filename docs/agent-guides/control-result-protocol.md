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
3. Parse strict JSON.
4. Require a JSON object.
5. Validate required fields for the current phase.
6. Reject phase mismatch.
7. Reject schema/invariant mismatch.
8. Do not advance state when validation fails.

Never infer workflow state by searching natural-language output for phrases such as:

```text
done
LGTM
looks good
merged successfully
```

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
`state.json`) and rendered verbatim into the next FIX prompt. The parser
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
controller. The review prompts state every parser bound (the id bound
included) through template variables that the engine fills from the same
constants, so the number the reviewer is told is the number it is held to.

The FIX prompt these findings are rendered into is bounded by a constant
factor of the parser bounds, not by their sum: the renderer's safety measures
each cost characters (a control character in a one-line field becomes its
`\xNN` or `\uNNNN` escape, a newline inside a `required_resolution` is
indented under its finding, and the fence grows one past the longest backtick
run in the findings). The engine test for the largest accepted round covers
each of those shapes, not only plain text.

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
