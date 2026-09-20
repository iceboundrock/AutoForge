# Secrets and logging

Read this before changing what gets written to run logs or state
(`src/autoforge/runlog.py`), the redaction patterns (`src/autoforge/redaction.py`),
error messages that may embed command output or environment values, or
anything that prints subprocess arguments, headers or configuration.

---

## Secrets and logs

Never print or commit:

- GitHub tokens
- API keys
- SSH private keys
- credential files
- authorization headers
- environment dumps containing secrets

Redact common patterns before persisting logs, including the values of:

```text
GITHUB_TOKEN
GH_TOKEN
OPENAI_API_KEY
ANTHROPIC_API_KEY
Authorization: Bearer ... / Token ... / Basic ...
```

and the well-known token shapes on their own (`ghp_*` and the other classic
GitHub prefixes, `github_pat_*`, `sk-*`, `sk-ant-*`, three-segment `eyJ...`
JWTs) and the whole userinfo of a URL (`https://x-access-token:...@github.com/...`).

Redaction is defense in depth; do not claim it detects every possible secret.

Redaction can lengthen a text (a short secret becomes the fixed marker), and
`redact` guarantees it never returns more than `MAX_GROWTH_FACTOR` times its
input. Persisted bounds applied *after* redaction (the review-history
resolution bound in `loop_guard`) depend on that factor, so a new pattern
must keep it, or raise it together with those bounds and their tests.

Do not log the entire process environment.

## What is redacted before it is persisted

`state.json` is stored in the clear, so the engine redacts at the writer,
once, wherever a persisted string can quote external text; a call site never
has to remember to. The writers that redact before persistence are:

- `ControllerEngine._block`: every controller-composed `block_reason`, and
  the `StepOutcome.message` it returns. Its call sites embed `GitHubError`
  messages (`gh` stderr), PR bodies and command output; they pass the raw
  text and `_block` redacts it at the sink.
- the two agent-message writers (`status: failure` / `blocked` in the
  REMOTE and LOCAL step paths): `block_reason` from the agent's `message`.
- `_record_verification_failure`: each `verification_failures` entry, which
  quotes a `VerificationError` (a validation command's output, a PR body,
  `gh` output). `REPLAN_REEXECUTE` renders that list into its prompt, so the
  prompt inherits the redaction.
- `_reject_next_issue`: each `next_issue_rejections` entry (rendered into
  the correction prompt and, once the bound is hit, into `block_reason`).
- the REVIEW and FIX appliers (REMOTE and LOCAL): each agent finding and
  resolution crosses `redact_dict` before it reaches `open_findings`,
  `review_history` or `last_fix_resolutions`.
- the LOCAL validation and pre-merge verification paths: the output tail of
  a command they quote.

`GitHubClient._run_gh` additionally redacts the `gh` stderr tail where it
builds a `GitHubError` / `GitHubNotFoundError` / `GitHubUnavailableError`
message, because `gh` can echo the request it made (an `Authorization`
header, a credentialed URL). The failure is classified (transient, not
found, access denied) on the raw tail and only quoted redacted, so the
message is safe in whatever it reaches (`block_reason`,
`verification_failures`, the run log's `error.txt`) without each consumer
redacting it again.

That list is the engine's boundary; it is defense in depth, not a promise
about the file. `state.json` can also be hand-edited or written by an older
controller, a journal defect quotes the value it could not read, and a
`BLOCKED` reason written before this boundary existed can quote `gh`
output raw. So anything the CLI prints *from* state (`status`,
`status --json`, the step outcome line, the terminal line of `run` / `step` /
`resume`, the existing-run refusal, the `READY_FOR_MERGE` banner) crosses
`redact` / `redact_dict` on the way out, in one pass over the whole rendered
text or document rather than per field, so a new state field is covered by
construction. That includes `run_id`: its validation checks that it is a
safe path component, not that it is free of secret shapes, so a line that
names the run is redacted assembled, never with the id interpolated outside
the pass. The file on disk is never rewritten by that pass.

`redact_obj` / `redact_dict` redact mapping keys as well as values, and a
mapping never loses an entry to that: two distinct keys that redact to the
same text (`GITHUB_TOKEN=a`, `GITHUB_TOKEN=b`) are kept apart with a `#2`,
`#3`, ... suffix in insertion order rather than the later value overwriting
the earlier one, so a redacted rendering of a free-form mapping (a journal's
`escalation`, log metadata) is still complete. The next suffix is remembered
per colliding text, so a mapping whose keys all collide is redacted in time
linear in its size; a change to the allocator must keep that, and
`tests/test_redaction.py` pins it against a large colliding mapping.

`stdout.log` and `stderr.log` hold what the executor captured, which is
bounded (`executor.DEFAULT_MAX_OUTPUT_BYTES` per stream): past the bound the
file is the head of the stream, a `[autoforge: N bytes of stdout omitted; ...]`
marker and the tail, and `execution.json` records `stdout_truncated` /
`stderr_truncated`. Redaction runs over that bounded text, so the redaction
pass, like the capture, costs at most the bound.
