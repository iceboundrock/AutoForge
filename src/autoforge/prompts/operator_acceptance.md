A finding, or an acceptance criterion of the issue, may need text on *other*
issues of this repository (for example coordination notes on the issues the
run's issue names). You hold no GitHub credential and the controller does
not post such text either: a finding may be resolved as
`operator_acceptance`, which asks the **operator** to post comments you
prepare (at most {{MAX_OPERATOR_ACTIONS_PER_FIX}} in one result). The controller only
verifies them: it reads each target issue's complete comment listing and
counts an action as done only when exactly one comment carries the action's
marker with exactly the prepared bytes, or when the operator binds an
existing comment as the evidence. Until then the run waits and sends nothing
else; your commits stay unpushed in this worktree.

Use it only when you can write each note completely from your work in this
worktree. It is never a deferral and never a claim that anything was posted:
work that needs a secret, a repository setting, hardware, a manual check or
any other change outside this worktree stays `"status": "blocked"` with a
`"message"`.

The issues this FIX entry allows, read by the controller from the run's
issue body (and the run's EPIC) when the phase was entered; any other number
is refused:

{{OPERATOR_TARGETS}}

You give only each target's issue **number** and the note's text. The
controller derives everything else: the issue URL, and its own header line
(naming this PR, the review round, the finding and the target, and saying
the text is yours) above your text and the action's marker below it. An
action names no URL, repository, owner, target, method, path or command; a
result carrying any of them is refused.

Shape (in `resolutions`):

```json
{"finding_id": "<finding id>", "resolution": "operator_acceptance",
 "rationale": "<why these notes resolve the finding>",
 "operator_acceptance": {"actions": [
   {"kind": "related_issue_comment", "issue": 16, "body": "<Markdown>"},
   {"kind": "related_issue_comment", "issue": 158, "body": "<Markdown>"}
 ]}}
```

- `kind` is `related_issue_comment`, the only kind there is.
- `issue` is a JSON integer from the list above: never a string, a URL,
  `#16` or `owner/repo#16`. At most one action per issue for a finding.
- `body` is Markdown of at most {{MAX_OPERATOR_COMMENT_CHARS}} characters,
  under the rules of "What published text may contain" below, and also: no
  image, no raw HTML tag, and no link or URL other than
  `https://github.com/{{REPOSITORY}}/...` (no other host or repository, no
  relative link, no `www.` form, no e-mail address).
- `rationale` follows the rules of `no_change_with_rationale` (at least
  {{MIN_RATIONALE_CHARS}} characters of concrete reasoning).
- No `commit_sha`, `follow_up_issue_url`, `follow_up_issue` or
  `acceptance_action` on this resolution; a result carries
  `acceptance_action` or `operator_acceptance` resolutions, never both, and
  at most {{MAX_OPERATOR_ACTIONS_PER_FIX}} actions in total.
- Never request again a note the controller already verified in this round
  (listed below): the finding already has that evidence.

The finding counts as resolved only after the controller has verified every
action, and the next review still judges whether the notes are adequate.
