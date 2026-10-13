One finding per result may be resolved as `acceptance_action`: you ask the
controller to publish one comment on this run's own issue ({{ISSUE_URL}}),
and the comment is the evidence the finding asks for. Use it only when a
finding or an acceptance criterion of the issue requires text to be
published on the issue itself (for example a summary of results the issue
asks to have posted there), and when you can write that text completely
from your work in this worktree. It is never a way to defer a finding, and
never a claim that any other external change was made: work that needs a
secret, a repository setting, hardware, a manual check or any change
outside this worktree stays `"status": "blocked"` with a `"message"`.

You give only the comment's text. The controller derives everything else:
the target is always the run's issue, and it adds its own header line
(naming this PR, the review round and the finding, and saying the text is
yours) above your text and the finding's marker below it. The request
names no URL, issue, repository, target, method, path or command; a result
carrying any of them is refused.

{{ACCEPTANCE_AUTHORIZATION}}

The finding counts as resolved only after the controller has read the
comment back from GitHub exactly as prepared, and the next review still
judges whether it is adequate.

Shape (in `resolutions`):

```json
{"finding_id": "<finding id>", "resolution": "acceptance_action",
 "rationale": "<why publishing this text resolves the finding>",
 "acceptance_action": {"kind": "issue_comment", "body": "<Markdown>"}}
```

- `kind` is `issue_comment`, the only kind there is.
- `body` is Markdown of at most {{MAX_ACCEPTANCE_BODY_CHARS}} characters,
  under the rules of "What published text may contain" below.
- `rationale` follows the rules of `no_change_with_rationale` (at least
  {{MIN_RATIONALE_CHARS}} characters of concrete reasoning).
- No `commit_sha`, `follow_up_issue_url` or `follow_up_issue` on this
  resolution, and at most one `acceptance_action` per result.
