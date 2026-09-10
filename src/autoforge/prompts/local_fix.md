# Phase: FIX (local, after review round {{REVIEW_ROUND}})

## Goal

Resolve every finding from local review round {{REVIEW_ROUND}} in the working
tree. There is no PR to push to, no commit to make and no GitHub Issue to
open.

- Repository root: {{REPO_ROOT}}
- Feature specification (frozen): {{FEATURE_SPEC_PATH}}
- Working-tree state as the controller sees it: {{WORKSPACE_STATUS}}
- Workspace fingerprint before your changes: `{{WORKSPACE_FINGERPRINT}}`
- Validation commands the controller will run after you finish: {{VALIDATION_COMMANDS}}

## Findings to resolve (finding IDs are authoritative)

{{FINDINGS}}

## Steps

1. Read the repository's `AGENTS.md` / `CLAUDE.md` if present.
2. For each finding, read the code it points at before changing anything.
3. Address EVERY finding ID listed above. For each one choose exactly one
   resolution:
   - `fixed` — you changed code/tests/docs in the working tree to resolve it.
   - `no_change_with_rationale` — after investigation the finding does not
     require a change. Give a concrete technical rationale (what you checked
     and why the current code is correct). A bare "won't fix" is not
     acceptable.
   - `unresolved` — the finding is real but you could not resolve it in this
     run (it needs a human decision, contradicts the specification, or is
     genuinely out of scope). Explain concretely in the rationale. This is an
     honest outcome; the controller surfaces it to the operator. Do not
     pretend a finding is fixed.
4. Keep fixes minimal and targeted. Do not refactor unrelated code, and do not
   redesign the implementation to dodge a finding.
5. Run the repository's tests and the listed validation commands. A failing
   validation command prevents this phase from being accepted.
6. Leave everything in the working tree: no commit, no push, no branch switch,
   no `git stash`, no reverting of changes you did not make. Do not touch
   `{{FEATURE_SPEC_PATH}}`.

There is no GitHub in this run, so `follow_up_created` is not a valid
resolution and creating a follow-up Issue is not an option.

## CONTROL_RESULT schema (exact)

```text
<<<CONTROL_RESULT>>>
{
  "phase": "FIX",
  "status": "success",
  "changed_workspace": true,
  "resolutions": [
    {"finding_id": "R{{REVIEW_ROUND}}-F1", "resolution": "fixed"},
    {"finding_id": "R{{REVIEW_ROUND}}-F2", "resolution": "no_change_with_rationale",
     "rationale": "<concrete technical reasoning, at least a couple of sentences>"},
    {"finding_id": "R{{REVIEW_ROUND}}-F3", "resolution": "unresolved",
     "rationale": "<what you tried and why it needs a human decision>"}
  ],
  "blocked_reason": ""
}
<<<END_CONTROL_RESULT>>>
```

- `resolutions` must contain exactly one entry per finding ID listed above.
- `"changed_workspace"` must be `true` if and only if you actually modified or
  created files. The controller fingerprints the working tree before and after
  this phase and rejects a result that disagrees with what it observed.
- `"blocked_reason"` is optional; use it for a run-level obstacle, not for a
  per-finding explanation.
- On failure: `"status": "failure"` plus `"message"`.
