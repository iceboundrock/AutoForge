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

### Important review invariant

For `REVIEW`:

```text
needs_fix_round == (number of actionable findings > 0)
```

Therefore both of these are invalid:

```text
findings=[] and needs_fix_round=true
findings=[...] and needs_fix_round=false
```

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
