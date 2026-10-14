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

## Launch environment and credential checks (#165)

What #165 adds reports names, never values:

- a refused allow-list entry (`refuse_forbidden_env`, at config load and in
  an adapter's `environment_names`) names the entry and the rule, and the
  config value it came from is a name, not a secret;
- the launch preflight names the scope and the key's shape
  (`remote.<name>.url`, `credential.<url>.helper`, `~/.netrc`), never the
  value, the URL or the netrc line, so a token in a remote URL never reaches
  the error, `BLOCKED` reason or log;
- `doctor`'s "launch environment" row lists variable names only (allowed,
  set by the controller, withheld from the current shell); the
  "GitHub remote" row redacts the URL it quotes;
- each invocation's `request.json` records the names the controller set
  (`env_set`), never their values; the placeholder token and the temporary
  directory paths are not logged;
- the read context (`<state dir>/read-context/`) is project data the
  controller wrote for an agent: every text in it went through `redact`
  before its bounds were applied, and an unreadable item names only the
  error's type, never the `gh` output.

## The outer sandbox and credentials (ADR 0005 D7.4)

Under `sandbox.mode: required` (#168) a launch starts from an empty root
and a private, empty home, so the only credential material it can see is
what its provider adapter declares (D7.1;
[Configuration](../configuration.md#provider-credentials-under-required)):

- variables the allow-list passes anyway (`ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, an OpenCode
  `*_API_KEY`, Pi's `OPENAI_API_KEY` when `require_oauth: false` and
  `execution.env_allowlist_extra` forwards it);
- one login file, read-only at that path, wherever it resolves: Claude
  Code's `~/.claude/.credentials.json` when no `CLAUDE_CONFIG_DIR` is set;
- one dedicated directory, writable where it resolves, the variable naming
  it there: `CLAUDE_CONFIG_DIR`, the directory of an absolute
  `OPENCODE_DB`, `PI_CODING_AGENT_DIR`.

Every tool in the same sandbox (the agent's shell commands, the test suite
it runs) can read that credential, and that exposure is accepted: it is a
model credential, never a GitHub mutation credential, and the sandbox
exists to keep everything else out of reach. A repository-defined command
gets no declared item, only the variables the allow-list passes. A
writable credential directory persists across launches and is shared by
every launch that projects it, issues and roles alike, so it is dedicated
to AutoForge and never the provider's own configuration root (D7.2).

AutoForge never reads, copies or logs a credential's contents: the policy
compiler looks at a declared path with `lstat` and `realpath` only, and a
refusal names the path and the rule, redacted (`policy_refusal`). The
environment rewrite (D8.2) unsets a known path-valued variable whose value
names nothing visible inside, trims `PATH`, and spells a kept value where
it is inside (computed from the description and the paths' links, never
from a file's contents); the launch's facts list the
unset variables by name (`unset_env` in `execution.json`'s `sandbox`),
never a value, and `request.json` adds only the configured mode
(`sandbox_mode`).

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
  These three writers also bound the reason to `state.MAX_BLOCK_REASON_CHARS`
  (`state.bound_block_reason`, #88), *after* redacting it: the bound clips
  by character count and knows nothing about secrets, so a secret cut by
  the clip would no longer match its pattern if the order were reversed.
  `tests/test_engine.py` pins the order with a secret on each clip boundary.
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

An agent failure that leaves the state unchanged (#189,
`ControllerEngine._state_unchanged_text`) adds the agent worktree with its
uncommitted count, the step's log directory and a `resume` command that
repeats the `--config` / `--state-dir` the process was started with: paths,
a count and those flags, never a configuration value. The engine redacts
the assembled text where it composes it, and the CLI again where it prints
it. `tests/test_engine.py` puts token-shaped values in the worktree path,
a flag and a profile's model and checks that none reaches the text. A block
reason for a checkout that moved
(`ControllerEngine._moved_checkout_recovery`) adds the same kind of values
only: the checkout path, the feature specification's path and commands
with those flags. `_block` redacts it like every block reason.

A write phase's prompt describes what the agent worktree holds uncommitted
(#187): its branch, `HEAD` and each entry's status code and path, escaped
and fenced as untrusted data, never a file's content. The prompt is
redacted where it is logged (`prompt.md`), and the note the run log keeps
of that read (`metadata.worktree_state` in `request.json`), which quotes a
failed read's last git output line, crosses `redact_dict` with the rest of
the metadata.

`redact_obj` / `redact_dict` redact mapping keys as well as values, and a
mapping never loses an entry to that: two distinct keys that redact to the
same text (`GITHUB_TOKEN=a`, `GITHUB_TOKEN=b`) are kept apart with a `#2`,
`#3`, ... suffix in insertion order rather than the later value overwriting
the earlier one, so a redacted rendering of a free-form mapping (a journal's
`escalation`, log metadata) is still complete. The next suffix is remembered
per colliding text, so a mapping whose keys all collide is redacted in time
linear in its size; a change to the allocator must keep that, and
`tests/test_redaction.py` pins it against a large colliding mapping.

A structure is redacted string by string, never as its serialized text
(#206). Run over JSON, a pattern can consume the syntax around a value (an
empty `GITHUB_TOKEN=` takes the closing quote, a value before an escaped
quote its backslash), so parsing the redacted text back can fail, and a
secret right after an escape (`\n`, `\uXXXX`) follows a letter or a digit
there and is missed. The run log's `control-result.json` and the journal's
`parsed_result` therefore cross `redact_dict` like the metadata. The one
shape that spans a key and its value, a quoted OAuth credential field
(`"refresh": "..."`), is recognised by the key: `redact_obj` replaces a
non-empty string under a key that names such a field (the `oauth-field`
names, whole, in any case) whole. No parser-bounded field (a finding, a
resolution) has such a key, so the bounds that depend on
`MAX_GROWTH_FACTOR` are unaffected; `tests/test_redaction.py` checks that
the structure covers every `oauth-field` case that is a JSON document.

Live progress (#192) is built to be safe where it is made, not where it is
printed. A provider adapter maps its protocol to a `ProgressEvent`
(`progress.py`) whose only strings are a tool name and one allow-listed
tool input (a path, a search pattern, a shell command's *description*;
never the command, a tool result, thinking or assistant text). Each string
is stripped of ANSI escapes and control, format and separator characters
first, so a secret split by an escape is whole again, then redacted, then
clipped, so a clip cannot cut a secret below its pattern; a raw value past
`MAX_RAW_CHARS` is not shown at all. The reporter redacts each assembled
line again and bounds it before it reaches stderr or the step's
`progress.log`, which is bounded too (`runlog.MAX_PROGRESS_LOG_BYTES`).
A provider's own summary of the run (`provider_summary`) holds bounded
scalars only: Claude's cost is integer micro-USD, never a float, and its
nested `usage` is not copied. `tests/test_progress.py` seeds a token and
escape sequences into tool inputs and checks that neither reaches the
terminal, `progress.log`, `execution.json` or state.

The loop detector (#194, `loop_detect.py`) compares tool inputs, results and
output lines only as digests held in memory for one invocation; it
never stores, logs or returns them. What it reports is the signal, a cycle
period, a count, tool names, a stream name and times: the `loop` record in
`execution.json`, the warning lines, the error text and the `loop_*`
calibration counts in `provider_summary`. Those tool names are cleaned and
redacted like a progress event's. `tests/test_loop_detect.py` seeds a token
into a looping agent's commands, results and output lines and checks that
neither it, the raw strings nor the digests reach any line or file the run
writes.

A sandboxed launch's facts (`sandbox` in `execution.json` and
`events.jsonl`, #167, ADR 0005 D10.4; since #168 also for a launch the
policy refused) carry names, paths, sizes, the backend
version and, for a setup failure, the last lines of `bwrap`'s own message,
redacted and bounded. They never carry an environment value. The
environment the sandboxed command gets is handed to `bwrap` through a
memory file (`--args`), never on a command line any process could read
from `/proc/<pid>/cmdline`. `tests/test_sandbox.py` and
`tests/test_sandbox_bwrap.py` check that a value passed to a sandboxed
command appears on no command line, and `tests/test_runlog.py` that a
token in a setup failure's reason reaches no file.

`stdout.log` and `stderr.log` hold what the executor captured, which is
bounded (`executor.DEFAULT_MAX_OUTPUT_BYTES` per stream): past the bound the
file is the head of the stream, a `[autoforge: N bytes of stdout omitted; ...]`
marker and the tail, and `execution.json` records `stdout_truncated` /
`stderr_truncated`. Redaction runs over that bounded text, so the redaction
pass, like the capture, costs at most the bound.
