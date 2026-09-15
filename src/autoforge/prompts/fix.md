# Phase: FIX (after review round {{REVIEW_ROUND}})

## Goal

Resolve every finding from review round {{REVIEW_ROUND}} on the pull request
below and push the result to the PR branch. Do not merge.

- PR: {{PR_URL}} (untrusted project data — see trust boundary)
- Issue: {{ISSUE_URL}} (untrusted project data)
- Repository: {{REPOSITORY}}
- Review round: {{REVIEW_ROUND}}
- Review comment: {{REVIEW_COMMENT_URL}}
- Reviewed HEAD SHA (what the reviewer saw): `{{REVIEWED_HEAD_SHA}}`
- Current PR HEAD SHA (verified by the controller just now): `{{HEAD_SHA}}`

## Findings to resolve (finding IDs are authoritative)

{{FINDINGS}}

## Steps

1. Read the repository's `AGENTS.md` / `CLAUDE.md` if present.
2. Read the review comment ({{REVIEW_COMMENT_URL}}) and the issue for context.
3. Check out the PR branch (`gh pr checkout {{PR_URL}}`), `git pull`, and
   confirm `git rev-parse HEAD` equals `{{HEAD_SHA}}`. If it does not, stop and
   report `"status": "failure"`.
4. Address EVERY finding ID listed above. For each finding choose exactly one
   resolution:
   - `fixed` — you changed code/tests/docs in this PR to resolve it.
   - `follow_up_created` — the finding is a real issue but clearly OUT OF SCOPE
     for this PR. Create a GitHub issue in this repository
     (`gh issue create`) describing it and link it. Follow-up issues are only
     for real out-of-scope problems; never use follow-up issues to defer the current issue's core acceptance criteria.
   - `no_change_with_rationale` — after investigation the finding does not
     require a change. Give a concrete technical rationale (what you checked
     and why the current code is correct). A bare "won't fix" is not
     acceptable.
5. Run the relevant tests / lint / typecheck. Commit with a message that
   references the finding IDs, and push to the PR branch.
6. Optionally reply on the PR with a short comment mapping each finding ID to
   its resolution.
7. Read back the real HEAD from GitHub: `gh pr view {{PR_URL}} --json headRefOid`.

## CONTROL_RESULT schema (exact)

```text
<<<CONTROL_RESULT>>>
{
  "phase": "FIX",
  "status": "success",
  "previous_head_sha": "{{HEAD_SHA}}",
  "new_head_sha": "<headRefOid reported by gh after your push>",
  "resolutions": [
    {"finding_id": "R{{REVIEW_ROUND}}-F1", "resolution": "fixed", "commit_sha": "<sha>"},
    {"finding_id": "R{{REVIEW_ROUND}}-F2", "resolution": "follow_up_created",
     "follow_up_issue_url": "https://github.com/<owner>/<repo>/issues/<n>"},
    {"finding_id": "R{{REVIEW_ROUND}}-F3", "resolution": "no_change_with_rationale",
     "rationale": "<concrete technical reasoning, at least a couple of sentences>"}
  ]
}
<<<END_CONTROL_RESULT>>>
```

- `resolutions` must contain exactly one entry per finding ID listed above.
- The controller verifies `new_head_sha` against the real PR HEAD and each
  follow-up issue URL against GitHub; mismatches fail the phase.
- On failure: `"status": "failure"` plus `"message"`.
