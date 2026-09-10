# Phase: ANALYZE_EXECUTE (local)

## Goal

Implement the frozen feature specification `{{FEATURE_SPEC_PATH}}` (quoted in
full above) in this repository, leaving the result as changes in the local
working tree. There is no Issue, no branch to create, no PR and no commit.

- Repository root: {{REPO_ROOT}}
- Current working-tree state as the controller sees it: {{WORKSPACE_STATUS}}
- Validation commands the controller will run after you finish: {{VALIDATION_COMMANDS}}

## Steps

1. Read the repository's `AGENTS.md` and `CLAUDE.md` if they exist. Their
   build/test/style rules outrank the specification text on workflow matters.
2. **Inspect the repository before writing anything.** Find the modules,
   conventions, naming, error handling and test style this change must fit
   into. Do not guess at structure you can read.
3. Re-read the specification's Requirements and Acceptance Criteria and map
   each one to the concrete change that will satisfy it.
4. Implement the **smallest correct solution**:
   - preserve the existing architecture and conventions;
   - do not introduce abstractions, layers, config knobs or generality the
     specification does not call for;
   - do not add dependencies unless the specification requires one and the
     repository has no existing way to do the job;
   - do not perform unrelated refactors, reformatting or cleanups.
5. Add or update tests in the repository's existing style, covering the
   acceptance criteria and the failure behaviour of what you changed.
6. Run the repository's existing tests and lint/typecheck commands where they
   are useful and cheap. Fix what you broke. If the listed validation commands
   above exist, run them yourself first — the controller will run them again
   and a failure prevents the phase from being accepted.
7. Leave everything in the working tree. Do not commit, do not push, do not
   switch branches, do not `git stash`, and do not revert changes you did not
   make. Do not touch `{{FEATURE_SPEC_PATH}}`.

## CONTROL_RESULT schema (exact)

Emit exactly one block at the end of stdout:

```text
<<<CONTROL_RESULT>>>
{
  "phase": "ANALYZE_EXECUTE",
  "status": "success",
  "summary": "<what you implemented and why, a few sentences>",
  "changed_workspace": true,
  "tests_attempted": ["<command you ran>", "..."]
}
<<<END_CONTROL_RESULT>>>
```

- `"changed_workspace"` must be `true` if and only if you actually modified or
  created files. The controller fingerprints the working tree before and after
  this phase and rejects a result that disagrees with what it observed.
- `"tests_attempted"` lists the commands you ran verbatim (empty list if none).
- Do not report HEAD SHAs or changed-file lists: the controller observes those
  itself.
- If you cannot implement the feature (the specification is contradictory or
  under-specified, the repository is in an unusable state, ...), use
  `"status": "blocked"` (human decision needed) or `"status": "failure"` with a
  `"message"` explaining exactly what stopped you. Do not implement something
  else instead.
