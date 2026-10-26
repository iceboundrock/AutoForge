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
ANTHROPIC_AUTH_TOKEN
CLAUDE_CODE_OAUTH_TOKEN
OPENCODE_SERVER_PASSWORD
Authorization: Bearer ... / Token ... / Basic ...
```

and the well-known token shapes on their own (`ghp_*` and the other classic
GitHub prefixes, `github_pat_*`, `sk-*`, `sk-ant-*`, Google API keys
`AIza...`, three-segment `eyJ...` JWTs) and the whole userinfo of a URL
(`https://x-access-token:...@github.com/...`), each where it starts a token
([below](#where-a-recognised-credential-starts-284)).

Redaction is defense in depth; do not claim it detects every possible secret.

Redaction can lengthen a text (a short secret becomes the fixed marker), and
`redact` guarantees it never returns more than `MAX_GROWTH_FACTOR` times its
input. Persisted bounds applied *after* redaction (the review-history
resolution bound in `loop_guard`) depend on that factor, so a new pattern
must keep it, or raise it together with those bounds and their tests.

Do not log the entire process environment.

## Where a recognised credential starts (#284)

A pattern recognises a credential only where it starts a token, so that a
shape inside a longer identifier is not taken for one (`risk-assessment`
holds `sk-assessment`, `laughs_together` holds `ghs_together`). Until #284
that start was a regex word boundary (`\b`), and there is none after the
`m` of an ANSI colour code (`ESC[0m`) or the `n` of a `\n` written out as
two characters: a token right after one was kept in the clear, and
OpenCode's `stderr.log` carries such sequences
(`ESC[91mESC[1mError: ESC[0m<message>`, `providers.py`). Every pattern of
`redaction.py` is now built on one rule (`_token`; the quoted `oauth-field`
shape, which a quote starts, needs none). A credential starts:

- at the start of the text;
- after any character that cannot be part of it: anything but an ASCII
  letter, a digit or `_`. A JWT and a Google API key exclude `-` as well,
  which base64url admits; the OAuth assignment names (`refresh_token=`,
  `id_token:`) do not exclude `_`, so a prefixed `oauth_refresh_token=` is
  caught;
- directly after an escape sequence: an ANSI / VT sequence (a CSI, after
  `ESC[` or the C1 byte, or a two-character escape such as `ESC(B`), its
  ESC also as JSON, C, a shell and caret notation write it (`\u001b[0m`,
  `\x1b[1m`, `\033[0m`, `\e[0m`, `^[[0m`); or a backslash escape written
  out (`\n`, `\t` and the other single letters, `\xHH`, `\uHHHH`,
  `\UHHHHHHHH`, octal). The escape is kept; only the credential after it
  is replaced.

Every sink that redacts goes through these patterns (the run log and its
`stdout.log` and `stderr.log`, state, the CLI's output), and so does the
content policy's `credential_classes`, so the rule holds in all of them.
What changed against `\b`, beyond the escapes, each pinned in
`tests/test_redaction.py`:

- a non-ASCII letter or digit right before a token is a boundary now,
  where `\b` took it for a word character: `caféghp_...` is redacted;
- a backslash before one of the escape letters counts as an escape
  wherever it stands, a path separator (`C:\Users\nghp_...`) or a
  written-out escaped backslash (`\\n`) included: the rule errs on
  redacting.

Unchanged: a shape right after an ASCII letter, a digit or `_` is still no
token (`xghp_...`, `MY_GITHUB_TOKEN=`), nor is one after an escape that
more of an identifier follows (`ESC[0mxghp_...`). A known name behind a
prefix (`GEMINI_API_KEY=`) is #285's to recognise; a new pattern is built
with `_token`, never on `\b`, as the Google API key class
([below](#google-api-keys-282)) is.

A pattern that starts a token runs as two passes, the second, which takes
the escape in and puts it back, only at an ESC, a backslash, a caret or the
C1 byte. Both are linear. `MAX_GROWTH_FACTOR` is unchanged: the escape is
no part of the replaced run, and at two characters or more it separates
repeated matches no tighter than a one-character separator does.

## Google API keys (#282)

OpenCode launches inherit the `GEMINI_*` and `GOOGLE_*` variables
(`OpenCodeProvider.environment_names`), and no pattern knew a Google API
key: the prefixed names are no `secret-assignment` (`_` before `API_KEY`
is a token character, #285), and the key matched no token shape. An agent
that printed its environment, or an error that quoted the key, put it in
`stdout.log` or `stderr.log` in the clear, and agent text carrying one
passed the published-content check.

The `google-api-key` class recognises the key by its shape, under any name
or none: `AIza` and 35 characters of `[0-9A-Za-z_-]`, 39 in all,
case-sensitive. The whole token must be the key: `-` is a token character
on both sides, as for a JWT, and a run of the key's alphabet that goes on
past the 39 is no key, so a near-miss of either length, a wrong prefix and
a shape inside a longer identifier are left (each pinned in
`tests/test_redaction.py`). It is built with `_token`, so it is recognised
after an escape sequence too (`ESC[0mAIza...`, `\nAIza...`). It runs after
the JWT pattern, which takes a key that is one of its segments with it, and
before the GitHub and `sk-` shapes: `-` is no token character for those, so
one starting after a `-` inside a key would otherwise replace a slice of it
and leave the key's first characters in the clear.

Like every class it reaches each redacting sink (the run log, its
`stdout.log` and `stderr.log`, state and the CLI's output) and
`credential_classes`, so the published-content, payload and commit-message
checks refuse a text carrying one by name, never rewriting it. The body is
fixed, so the pattern stays linear, and a key shrinks under redaction (39
characters to the 14 of the marker), so `MAX_GROWTH_FACTOR` is unchanged.
Not recognised still: a value of no known shape under a prefixed name
(`GEMINI_API_KEY=<anything else>`) and the other shapes #285 lists.

## Escaped text: redact the structure first (#281)

Until #281 the CLI printed a step's agent result as
`redact(json.dumps(result))`, a line that reaches the terminal and, through
`worker.py`, a scheduler worker's persisted log. JSON writes a quote as
`\"`, so a quoted assignment in a result string (`GH_TOKEN="..."`) read
`GH_TOKEN=\"...\"` there, and the value pattern stopped at the backslash
and printed the secret after it in the clear. (A token after a written-out
`\n`, `\t` or `\uXXXX` was recognised since
[#284](#where-a-recognised-credential-starts-284).) Two rules now hold:

- **A structure the controller serializes is redacted first, string by
  string** (`redact_obj`,
  [below](#what-is-redacted-before-it-is-persisted)), never as its
  serialized text: the step-outcome line (`print_step_outcome`, for `run`,
  `step`, `resume` and the worker), the replan `escalation` in `status`'s
  journal lines, in a dry run's `replan policy:` note and in the
  `ESCALATION_REASON` prompt variable (the prompt is logged as `prompt.md`
  and previewed by a dry run as text). A dry run's notes, which quote state
  such as a recorded refusal, are redacted like its variables.
- **Text no one can redact first is matched as escaped.** An agent's stdout
  and stderr are captured whole into `stdout.log` and `stderr.log`, its
  result among them as the JSON it printed, and an error can quote an
  excerpt. The assignment patterns (`env-assignment`, `secret-assignment`,
  `oauth-assignment`, `oauth-bare-assignment`) take their optional quote
  behind up to seven backslashes too (`\"`, `\'` as a Python repr writes it,
  `\\\"` for JSON inside a JSON string, three levels deep at most, the
  bound `oauth-field` has), and keep a backslash that starts the closing
  escaped quote out of the value: `GH_TOKEN=\"***REDACTED***\"`, so JSON
  redacted as text still parses. A quote behind eight backslashes or more
  is not recognised, nor, as before, an `Authorization` header whose value
  is quoted, escaped or not.

Every place that serializes and redacts was checked for #281: the JSON
documents `status --json`, `doctor --json` and `epic status --json` print
are redacted before they are serialized, as are the run log's JSON files
and journal and the scheduler's claim `detail`; the engine redacts agent
text field by field as it writes it to state, so the prompt variables
serialized from state (the FIX resolutions, operator and acceptance
outcomes) carry redacted strings; the other `json.dumps` calls build
digests, canonical forms, internal files or a process's input, none of
them passed to `redact`.

`MAX_GROWTH_FACTOR` is unchanged: an escaped quote, like a plain one, is
kept and no part of the replaced run, and a value is still at least one
character (`HF_TOKEN=\"x\"`, 14 to 27, is pinned with the other growth
units). The backslash runs are bounded, so the patterns stay linear, and
`redact` stays idempotent.

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

## SDK providers (#149)

The SDK providers' policy ([SDK policy](../sdk-policy.md)) adds rules of
its own, all enforced in `src/autoforge/sdk_policy.py`:

- **No other provider's credential reaches an SDK launch.** Its allow-list
  is expanded to exact names against the *names* in the controller's
  environment, and every other provider's variables and every
  credential-shaped name (`*_API_KEY`, `*_TOKEN`, `*_SECRET`,
  `*_PASSWORD` and the like) are removed. Only then is the provider's own
  credential added: `ANTHROPIC_API_KEY`, or the variable an `opencode-sdk`
  profile names in `options.credential_env`. The controller's own
  environment is never changed.
- **No login is reused.** `claude-sdk` has no login-file or keyring item,
  and refuses `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_AUTH_TOKEN` by name.
  `opencode-sdk` reads no stored sign-in.
- **The OpenCode server password** is generated per launch and given to
  the server's environment alone. It never appears in an argv, a record, a
  log or state, and its name is in the redaction list above.
- **Names, never values.** Every policy message names a variable, a field
  or a tool and never quotes a value: config validation, the
  authentication checks, the `system/init` check and the
  interaction-data refusals.
- **Interaction data is bounded before it is shown or persisted.** The
  run log's `interaction` record (`execution.json`, #134) names each
  request, its offers, the directive and the kinds of decision delivered.
  It never carries an answer's text, guidance or a modified input. It
  keeps an entry for at most `max_interactions` requests and the first one
  past them; a provider that keeps asking is failed and only counted.
  - A value a reply depends on (a tool input, an option, an answer, a
    modified input) is *refused* rather than redacted or clipped when it
    carries a credential shape, a control or escape character, or exceeds
    its bound. A redacted or clipped value would no longer be the one
    approved.
  - What an operator is shown goes through `progress.clean`, which strips
    escape sequences and control characters, redacts, and clips, in that
    order. It shows only allow-listed input keys, never a file's content
    or a diff.
- **The session roots** (`<sandbox dir>/sdk/<provider>/`) hold the
  provider's transcripts. They are controller state, never a working tree
  and never published. AutoForge copies no credential into them.

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
- the validation-record writers (`evidence.py`, #172): a record's or a
  marker's argv (element by element, `redact_argv`) and each stream's
  output tail. The tail is redacted over the whole captured stream before
  it is cut to 2,000 characters, so a secret straddling the cut is not
  half-kept, then control characters are escaped; the enforcement facts
  are redacted and clipped. A definition's digest is over the argv as
  configured, a one-way SHA-256 that stores no argv, which is also why a
  secret belongs in the allow-listed environment and never in a validation
  argv ([Configuration](../configuration.md#candidate-validation)).

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
empty `GITHUB_TOKEN=` takes the closing quote), so parsing the redacted
text back can fail. (A secret right after an escape there, `\n` or
`\uXXXX`, follows a letter or a digit; it was missed until #284 and is
recognised now, [above](#where-a-recognised-credential-starts-284). A
quoted value behind JSON's `\"` lost the backslash and kept the secret
until #281, [above](#escaped-text-redact-the-structure-first-281).) The run
log's `control-result.json` and the journal's `parsed_result` therefore
cross `redact_dict` like the metadata, and the CLI's step outcome
`redact_obj`. The one
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
