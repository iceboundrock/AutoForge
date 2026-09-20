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

Persisted state is not a redaction boundary of its own: the engine redacts
agent text before it writes `block_reason`, findings and resolutions, but
`state.json` can also be hand-edited or written by an older controller, and a
journal defect quotes the value it could not read, and a `BLOCKED` reason
can quote `gh` output. So anything the CLI prints *from* state (`status`,
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
