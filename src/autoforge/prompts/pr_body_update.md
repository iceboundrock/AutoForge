A finding whose only remedy is a change to the description of this pull
request may be resolved as `pr_body_update`: you ask the controller to replace
the description of this run's own PR ({{PR_URL}}). Use it only when the review
says the description is stale or wrong, or the finding is otherwise resolved by
what the description says. It is never a way to defer a finding, and never a
claim that any code, test or other external change was made.

You write the whole new description, as it should read. The current one is
`pull-request.md` of the read context. The controller derives everything else:
the target is always this PR, and it keeps its own closing reference to issue
#{{ISSUE_NUMBER}} and its own markers below your text, byte for byte. So do not
write them: your text holds no marker and no closing reference, and the request
names no URL, PR, issue, repository, target, method, path or command. A result
carrying any of them is refused.

{{PR_BODY_AUTHORIZATION}}

The controller replaces the description only if it is still exactly what you
read when this phase began. Otherwise it writes nothing and stops, and a human
decides. If the description already reads exactly as you ask, nothing is
written. Either way the finding is resolved only after the controller has read
the description back from GitHub byte for byte, and the next review still reads
it and judges whether it is right.

No commit is needed for this resolution. When every finding is resolved this
way, or otherwise without a commit, report `{{REVIEWED_HEAD_SHA}}` as `head_sha`:
the controller replaces the description and returns to review at the same HEAD,
pushing nothing. When you also commit fixes for other findings, describe the
commit you report: the controller replaces the description first and pushes
only after that is verified.

Shape (in `resolutions`):

```json
{"finding_id": "<finding id>", "resolution": "pr_body_update",
 "rationale": "<why this description resolves the finding>",
 "pr_body_update": {"body": "<the whole new description, Markdown>"}}
```

- `body` is Markdown of at most {{MAX_PR_BODY_UPDATE_CHARS}} characters, under
  the rules of "What published text may contain" below.
- `rationale` follows the rules of `no_change_with_rationale` (at least
  {{MIN_RATIONALE_CHARS}} characters of concrete reasoning).
- No `commit_sha`, `follow_up_issue_url` or `follow_up_issue` on this
  resolution.
- A result holds one description. When several findings are resolved this way,
  each carries the same `body`, byte for byte; a result whose bodies differ is
  refused. Write one description that resolves them all.

Replacements of this round the operator declined:

{{DECLINED_PR_BODY_UPDATES}}
