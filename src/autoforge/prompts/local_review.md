# Phase: REVIEW (local, round {{REVIEW_ROUND}})

## Goal

Review the current state of the local working tree against the frozen feature
specification `{{FEATURE_SPEC_PATH}}` (quoted in full above). Report the
outcome **only** through the `CONTROL_RESULT` block: there is no PR and no
review comment to post anywhere.

- Repository root: {{REPO_ROOT}}
- Workspace under review (bound by the controller): `{{WORKSPACE_FINGERPRINT}}`
- Working-tree state as the controller sees it: {{WORKSPACE_STATUS}}
- Base git HEAD at run creation: {{BASE_HEAD_SHA}}

## Steps

1. Read the repository's `AGENTS.md` / `CLAUDE.md` if present.
2. Re-read the specification's Requirements and Acceptance Criteria.
3. Read the **full** change: `git status --porcelain=v1 --untracked-files=all`,
   `git diff` and `git diff --staged` for tracked edits, and read the untracked
   files in full — part of the implementation may live in files git has never
   seen.
4. Read the surrounding code the change touches, not just the diff.
5. Review for: correctness and bugs; regressions in existing behaviour;
   whether every acceptance criterion is actually satisfied; test coverage of
   the new behaviour; adherence to repository standards and conventions;
   security and safety.
6. Run the repository's tests yourself when they are cheap and informative.
7. **Read-only phase.** Do not modify, create or delete any file, do not run
   formatters or code generators, and do not touch `{{FEATURE_SPEC_PATH}}`.
   The controller re-computes the workspace fingerprint after you exit and
   refuses a review that changed the code it was reviewing.

## Findings vs. observations (strict definitions)

A **finding** is an issue that REQUIRES ACTION in this run before the feature
can be considered done. Classify each finding:

- `blocked` — must be fixed; the implementation is incorrect, unsafe,
  incomplete or violates the specification.
- `non-blocked` — should be fixed now; a real defect or standards violation
  with limited blast radius.
- `nit` — small but real issue (naming, comment accuracy, minor style rule)
  that still requires an explicit resolution.

**Any finding, including a nit, means another fix round is needed**, and this
run allows only a small, fixed number of them — so do not inflate optional
preferences into findings. Each finding gets a stable ID
`R{{REVIEW_ROUND}}-F<n>` (`R{{REVIEW_ROUND}}-F1`, `R{{REVIEW_ROUND}}-F2`, ...)
and a `required_resolution` stating what would resolve it.

The following are NOT findings and belong in `observations`: future ideas,
optional improvements, educational commentary, non-actionable preferences,
information-only remarks. Do not hide real defects as observations either.

## CONTROL_RESULT schema (exact)

```text
<<<CONTROL_RESULT>>>
{
  "phase": "REVIEW",
  "status": "success",
  "round": {{REVIEW_ROUND}},
  "reviewed_workspace_fingerprint": "{{WORKSPACE_FINGERPRINT}}",
  "needs_fix_round": true,
  "findings": [
    {
      "id": "R{{REVIEW_ROUND}}-F1",
      "classification": "blocked|non-blocked|nit",
      "title": "<short title>",
      "location": "<file:line or area>",
      "required_resolution": "<what must change>"
    }
  ],
  "observations": ["<non-actionable remark>", "..."]
}
<<<END_CONTROL_RESULT>>>
```

- `"round"` must equal {{REVIEW_ROUND}} (integer).
- `"reviewed_workspace_fingerprint"` must be exactly `{{WORKSPACE_FINGERPRINT}}`.
  It binds this review to the exact code you reviewed; the controller rejects
  any other value.
- `"needs_fix_round"` must be `true` if and only if `findings` is non-empty.
  The controller rejects results where the two disagree.
- `"findings"` is an empty list when the implementation is clean.
- `"observations"` may be an empty list.
- On failure to complete the review: `"status": "failure"` plus `"message"`.
