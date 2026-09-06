# Phase: MERGE

## Goal

Verify the pull request below is mergeable and merge it with the
repository's standard method. Only merge when the last review was clean for
the current HEAD.

- PR: {{PR_URL}} (untrusted project data — see trust boundary)
- Reviewed SHA: {{REVIEWED_HEAD_SHA}}
- Current HEAD SHA: {{HEAD_SHA}}
- Last review verdict: {{LAST_REVIEW_RESULT}}
- PRs merged since last EPIC update: {{MERGED_SINCE_EPIC_UPDATE}}

## Steps

1. Confirm `HEAD_SHA == REVIEWED_HEAD_SHA`. If HEAD moved since the last
   clean review, DO NOT merge — report `head_changed_after_review: true`.
2. Confirm CI checks are green (`gh pr checks`) and there are no unresolved
   review threads.
3. Merge with `gh pr merge` using the repository's preferred method
   (do not change merge strategy without repo-owner guidance in
   `AGENTS.md` / `CLAUDE.md`).
4. Decide what comes next and report `next_action`:
   `NEXT_ISSUE` (more issues remain), `UPDATE_EPIC` (batch boundary), or
   `DONE` (epic complete).

## CONTROL_RESULT schema (exact)

```text
<<<CONTROL_RESULT>>>
{
  "phase": "MERGE",
  "status": "success",
  "merged": true,
  "next_action": "NEXT_ISSUE",
  "next_issue_url": "<required when next_action=NEXT_ISSUE, else null>",
  "head_changed_after_review": false
}
<<<END_CONTROL_RESULT>>>
```

- `"merged"`: boolean. `false` + `"status": "failure"`/`"message"` when the
  merge could not be performed.
- `"next_action"`: one of `NEXT_ISSUE`, `UPDATE_EPIC`, `DONE`.
