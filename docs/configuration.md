# Configuration

[`autoforge.example.yaml`](../autoforge.example.yaml) is the authoritative,
exhaustive reference: it lists every key the controller reads, with its
default and a comment on what it enforces. This page explains how a config
file is created, loaded and validated, and gives an overview of the
sections. It does not repeat every option.

## Creating and passing a config file

```bash
cp autoforge.example.yaml autoforge.yaml     # git-ignored: local to one checkout
uv run autoforge --config autoforge.yaml doctor
uv run autoforge --config autoforge.yaml run --epic <EPIC URL> --issue <issue URL>
```

`--config` and `--state-dir` are options of the `autoforge` command itself,
so they go before the subcommand. Without `--config` the controller uses its
built-in defaults. `autoforge.yaml` and `autoforge.local.*` files are
git-ignored; do not commit a config that names credentials or local paths.

Edit the file to change model identifiers, effort, agent limits and provider
options without touching controller source. Provider-specific flags are
built by the adapters in `src/autoforge/providers.py`; the engine never
hard-codes CLI syntax. The CLI flag syntax in `autoforge.example.yaml` was
checked against the locally installed CLIs (Claude Code 2.1.263, OpenCode
2.0.23, gh 2.100.0).

## Formats

YAML (`uv sync --extra yaml` for PyYAML, else a minimal built-in subset
parser), TOML (stdlib), and JSON (stdlib) are accepted; the file extension
(`.yaml` / `.yml`, `.toml`, `.json`) selects the parser. A file that does not
parse is reported as `cannot parse config <path>: ...` whichever parser read
it, the built-in subset parser included.

The two YAML backends never read the same file differently. Whatever the
subset parser accepts, it resolves exactly as PyYAML's YAML 1.1 implicit
resolvers do -- so `safety.allow_merge: on` is `true` either way, `0600` is
octal (384) either way, `1:30` is sexagesimal (90) either way, and `1e3` is
the string `1e3` either way -- and anything it cannot resolve that way it
refuses outright with `install PyYAML for full YAML support` rather than
keeping it as the string it looks like. Refused, among others: anchors,
aliases, tags, block scalars (`|`, `>`), flow mappings (`{a: 1}`), nested
flow sequences, escape sequences inside quoted scalars, timestamps, a
mapping inside a sequence item (`- key: value`, which is a mapping to PyYAML
and never the string it looks like), a plain scalar holding what PyYAML's
scanner reads as an indicator rather than text (`model: x: y`, `model: - x`,
`model: ? x`), document markers (`---`), tabs outside a quoted scalar or a
comment (PyYAML's scanner refuses those too), the non-printable characters
PyYAML's reader refuses anywhere in a file, comments and quoted scalars
included (a stray `U+000C` or `U+001F`, say), and a document with a line the
top-level block does not contain, such as a mapping followed by a sequence
item. The last one matters because the alternative is not a different value
but a *partial* file: the parser would otherwise read the lines it
understood and drop the rest, and the dropped line could be the one that
closes the merge gate.

A quote is read as a quoted scalar only where a scalar may *begin* -- the
start of a value or an item, or after a `,`, `[` or `{` -- exactly as PyYAML
reads one, so `model: don't # why` keeps its comment and `model: a 'b # c'`
is the plain scalar `a 'b` on both backends. Whitespace is YAML's, not
Python's: only a space and a tab separate tokens, so a Unicode space such as
`U+00A0` is an ordinary character of the scalar it sits in on both backends
rather than something either one trims away -- `safety.allow_merge:` written
with an invisible non-breaking space before `true` is a string, and the
configuration error it causes is the same one on both.
Installing the extra therefore widens what parses; it never changes what an
already-parsing file means.

One ordinary YAML shape the subset parser refuses is a block sequence
indented to its key's own column (`required_checks:` at indent 2 followed by
`- ci` at indent 2). Indent the items one level deeper than the key, or
install the extra.

## Validation: every key must be one the controller reads

An unknown key, whether at the top level, in any section (`execution`,
`safety`, `github`, `merge`, `review`, `review.replan`, `workflow`, `local`)
or in a profile mapping, is a configuration error naming the section, the
offending key and the keys that section knows, e.g.
`unknown key(s) under 'workflow': max_review_round (known: max_review_rounds, ...)`.
A typo cannot be a silent no-op that leaves the built-in default in force:
several of these keys are loop bounds or merge behaviour, where "ignored"
means a looser bound than the operator wrote while `autoforge doctor` calls
the file valid.

- Provider-specific `options` under a profile are checked by the profile's
  provider adapter in `providers.py`, against the keys that adapter reads:
  `claude` accepts `permission_mode`, `output_format`, `session_persistence`;
  `opencode` accepts `output_format`, `auto_approve`; `pi` accepts
  `require_oauth`, `tools`, `context_files`. Any other key is an error naming the profile and the
  accepted keys, so an option meant for another provider is refused rather
  than silently ignored.
- The same contract covers what a parser would otherwise settle before the
  controller looks: a key written twice in one mapping is a parse error on
  every format (PyYAML and `json.loads` would keep the last copy and drop the
  first, typo included).
- A YAML document whose root is not a mapping is refused on both YAML
  backends (only an empty or comment-only file means "all defaults").
- A profile name must be a non-empty string (PyYAML types unquoted `1:` as
  an integer).
- `safety.allow_merge` is the *only* key that opens the merge gate. The
  historical `execution.allow_merge` is rejected on load rather than read,
  and an unknown key under `safety` (a typo such as `allow_merges`) is a
  configuration error, so the gate can never be "disabled" in one place while
  still open in another. `autoforge doctor` prints the effective gate state
  and the file that set it.
- Both `workflow.stagnation_*` settings are `0` (rule disabled) or `>= 2`:
  they compare consecutive rounds, so a window of `1` is rejected by the
  config loader rather than silently disabling the rule.
- `safety.protected_merge_paths: []` disables that gate; leaving the key
  empty (`null`) is a configuration error rather than a silent opt-out.

`autoforge doctor` (and `autoforge local doctor`) reports whether the file
loads and validates. Doctor is read-only apart from creating the state
directory if it is missing and a probe file it creates and removes there.

## Sections at a glance

| Section | What it controls | Where the behaviour is specified |
|---|---|---|
| top level | `version`, `state_dir` (default `.autoforge`), `prompt_version` | [State and recovery](agent-guides/state-and-recovery.md) |
| `execution` | the default agent limits (`default_idle_timeout_seconds` 900, `default_max_runtime_seconds` unset), loop detection (`loop_detection`, mode `warn`), the wall-clock bound on controller-run commands (`command_timeout_seconds` 1800), `max_correction_attempts` (default 1), the agent environment allow-list (`env_allowlist`, `env_allowlist_extra`), the per-issue agent worktree location (`worktree_dir`) | [Architecture](agent-guides/architecture.md), [Running the remote workflow](usage.md) |
| `safety` | the merge gate (`allow_merge`, default `false`), `protected_merge_paths` (default `.github/workflows/`), `required_checks` (default `ci`), `verify_check_definition` (default `true`) | [GitHub safety](agent-guides/github-safety.md#merge-safety) |
| `merge` | how the controller merges once the gate is open: `method` (default `squash`), `delete_branch`, `max_verification_attempts` (default 5), `verification_commands` (argv lists, empty by default) | [GitHub safety](agent-guides/github-safety.md#merge-safety) |
| `workflow` | loop bounds: `max_review_rounds` (20), `stagnation_identical_rounds` (2), `stagnation_unchanged_count_rounds` (3), `max_total_steps` (300); `epic_update_every` (1) | [Workflow](agent-guides/workflow.md#loop-bounds) |
| `review.replan` | replan policy: `enabled`, `soft_threshold` (12), `hard_threshold` (20), `stagnation_window` (3), `max_findings_per_round` (2), `max_replans_per_issue` (2) | [Workflow](agent-guides/workflow.md#loop-bounds), [Replan transaction](agent-guides/replan-transaction.md) |
| `profiles` | provider, model, effort, command, idle timeout, maximum runtime and provider options per logical profile | [Profiles](#profiles) below |
| `github` | the `gh` binary and its timeout | [GitHub safety](agent-guides/github-safety.md) |
| `local` | Local Mode: `feature_dir`, `max_fix_rounds`, `validation_commands`, `exclude`, `max_workspace_entries`, `max_workspace_bytes` | [Local mode configuration](#local-mode-configuration) below |

## Profiles

Logical profile names (`analyze_execute`, `fix`, `review_round_1`,
`review_round_2_5`, `review_round_6_plus`, `replan_reexecute`, `update_epic`)
are stable. There is no `merge` profile: the controller merges, see `merge:`
in the example file. Which profile serves which phase and review round is
specified in [workflow.md](agent-guides/workflow.md#review-round-routing);
the example file carries the default provider, model, effort and limits
for each. The built-in defaults are:

| Profile | Phase | Provider | Model | Effort |
|---|---|---|---|---|
| `analyze_execute` | `ANALYZE_EXECUTE` | Claude Code | `fable` | high |
| `fix` | `FIX` | Claude Code | `fable` | high |
| `review_round_1` | `REVIEW`, round 1 | OpenCode | `openai/gpt-5.6-luna` | high |
| `review_round_2_5` | `REVIEW`, rounds 2 to 5 | OpenCode | `openai/gpt-5.6-terra` | high |
| `review_round_6_plus` | `REVIEW`, round 6 up to `workflow.max_review_rounds` (default 20) | OpenCode | `openai/gpt-5.6-sol` | medium |
| `replan_reexecute` | `REPLAN_REEXECUTE` | OpenCode | `openai/gpt-5.6-terra` | high |
| `update_epic` | `UPDATE_EPIC` | OpenCode | `openai/gpt-5.6-sol` | high |

### Overriding a profile

A profile in your file is merged over the built-in default of the same name,
field by field, as long as it keeps the default's provider. If it names a
*different* `provider`, it is a different CLI, so it is built from your
mapping alone: `command` falls back to that provider's binary (`claude`,
`opencode`, `pi`), `effort` to `high`, the limits to the `execution`
defaults (see [Agent limits](#agent-limits)), and nothing is inherited from the
default's `options` or `extra_args` (an OpenCode `auto_approve` on a Claude
profile, or a Claude flag in a Pi argv, would be wrong). Before #129 a
provider switch kept the default's `command`, `options` and `extra_args`;
restate any of them you relied on.

### Agent limits

An agent is killed when it makes **no progress**: nothing on its stdout or
stderr for `idle_timeout_seconds`. Any output resets the timer, so a long
task that keeps working is never killed for taking long, and a stuck one is
killed after the idle timeout rather than after a fixed wall-clock budget
sized for the slowest task (#193). `max_runtime_seconds` is an optional
wall-clock ceiling measured from the launch; whichever limit is due first
fires.

| Key | On a profile | Default (`execution`) |
|---|---|---|
| idle timeout | `idle_timeout_seconds` | `default_idle_timeout_seconds`: 900 |
| maximum runtime | `max_runtime_seconds` | `default_max_runtime_seconds`: unset (`null`) |

Both are whole seconds, greater than 0 and at most one week (604800); a
profile value wins over the default. With no ceiling, the executor's
one-week backstop is the only wall-clock bound.

- **What counts as progress.** Output, not intent: Claude's stream-json
  records (one per tool call and per piece of thinking), Pi's RPC events,
  OpenCode's text. A tool call that prints nothing for longer than the idle
  timeout, such as a long silent test run inside one shell command, is no
  progress and is killed; raise `idle_timeout_seconds` on that profile.
- **The trade-off.** The idle timeout catches a silent hang, not a busy
  one: output that keeps flowing resets it. [Loop detection](#loop-detection)
  catches an agent that keeps repeating itself, and kills it in `kill` mode;
  in the default `warn` mode it only warns, so set `max_runtime_seconds`
  when an unattended run needs a hard bound on an active agent.
- **Claude `output_format: text`** prints nothing until it exits, so the
  idle timeout cannot tell a working agent from a stuck one. Such a profile
  runs under its ceiling alone, and a ceiling is required: without
  `max_runtime_seconds` (on the profile or as
  `execution.default_max_runtime_seconds`) it is a configuration error: a
  run refuses to start when a profile it requires has none, `autoforge
  doctor` fails any profile the run can reach (including `update_epic`),
  and the controller never launches it.
- **Not agents.** `merge.verification_commands`, `local.validation_commands`
  and the controller's own `git` plumbing keep a wall-clock limit,
  `execution.command_timeout_seconds` (default 1800); `gh` calls use
  `github.timeout_seconds`.

`autoforge doctor`, the dry-run plan and the pre-launch progress line show
both values for each profile (`idle timeout 900s, max runtime unset`). A
timeout's error names the limit that fired and when the agent last wrote
anything, and the step's `execution.json` records the same
([Run logs](agent-guides/state-and-recovery.md#runtime-artifacts)).

The keys are strict. A profile's `timeout_seconds` (the old wall-clock
limit) and `execution.default_timeout_seconds` are load errors that name
their replacements.

### Loop detection

An agent that keeps producing output while it repeats itself resets the
idle timer forever. `execution.loop_detection` (#194) watches what the
agent *does* instead: a loop is the same action, with the same input and
the same result, repeated with no new action in between, never repeated
text alone.

```yaml
execution:
  loop_detection:
    mode: warn                    # kill | warn | off
    max_cycle_period: 4           # 1-16
    max_cycle_repeats: 8          # 2-100000
    novelty_window_seconds: 1800  # 1-604800
    max_line_repeats: 200         # 2-100000
```

The detector looks for four signals:

- **Action cycle.** A completed tool call is one action, fingerprinted by
  its tool name, its full input and its full result. A cycle of up to
  `max_cycle_period` actions repeated `max_cycle_repeats` times in a row is
  a loop, for example `Bash, Read, Bash, Read, …` with the same command,
  the same file and the same answers each time.
- **No novelty.** No action the invocation had not already made for
  `novelty_window_seconds`, while at least `max_cycle_period ×
  max_cycle_repeats` replayed actions fell inside that window. Below that
  floor the window is inconclusive, so one long silent tool call is never
  a loop; the idle timeout handles that.
- **Repeated lines.** Output that is not a structured stream (OpenCode's
  stderr, a Claude `output_format: text` profile's stdout) is cut into
  lines at `\n`, `\r\n` and a bare `\r`, with digits and hex masked and
  whitespace collapsed. A line, or a cycle of up to `max_cycle_period`
  lines, repeated `max_line_repeats` times in a row is a loop, so a retry
  message redrawn in place with `\r` counts like any other line. A line
  with no letters (a pytest dot line, a rule) is skipped, and so is a line
  a bare `\r` ends that shows a progress mark (a percentage, a bar or
  spinner glyph, an ASCII bar such as `[====>   ]`, or a `|`, `/`, `-` or
  `\` spinner frame at either end): a `\r` progress bar or spinner redraws
  one display rather than writing new lines.
- **Retry storm.** `max_cycle_repeats` provider retries in a row over
  `novelty_window_seconds` with no completed turn or tool call in between;
  the failed attempt a provider reports before each retry is not a
  completed turn. This needs a provider that reports its retries (Claude
  stream-json, Pi).

The action cycle, no-novelty and retry-storm signals come from the
structured events of Claude's stream-json and Pi's RPC. OpenCode and a
Claude text profile have no such events and get the repeated-lines signal
only.

The modes:

| `mode` | From half a threshold | At a threshold |
|---|---|---|
| `warn` (default) | warns | warns that the loop is conclusive; the agent runs on |
| `kill` | warns | kills the agent as on a timeout |
| `off` | nothing | nothing |

A warning is a progress line, such as `possible loop: 2-step cycle (Bash,
Read) repeated 4×`. It repeats at most every two minutes while the loop
goes on, plus once when the loop becomes conclusive. In every mode, `off`
included, `provider_summary` in `execution.json` records how close the
invocation came to each threshold. These are the `loop_*` calibration
fields, and #199 sets the thresholds and switches the default to `kill`
from them. What a kill looks like, and how to resume after one, is in
[Running the remote workflow](usage.md#loop-detection).

Work that repeats legitimately stays below every threshold:

- polling CI a few times;
- the same test after each edit (the edits differ);
- `git status` / `git diff` between edits;
- pytest progress output;
- one long silent tool call.

`autoforge doctor`, the dry-run plan (`Loops:`) and the launch line show
the mode, and the doctor and dry-run also show the thresholds. The keys
are strict, like everywhere else. An unknown key, an unknown mode or a
value out of range is a load error. A repeat count of 1, or a period of
0, is refused rather than read as "off"; `mode: off` is the way to turn
detection off.

### Claude profiles

`provider: claude` runs Claude Code non-interactively. `model` is a
`claude --model` alias or id, `effort` is sent as `--effort`
(`low|medium|high|xhigh|max`), `options.permission_mode` as
`--permission-mode`, and `extra_args` are appended after the adapter's own
flags:

```text
claude -p --output-format stream-json --verbose --model <model> --effort <effort>
   --permission-mode <mode> --no-session-persistence [extra_args] -- <prompt>
```

`options.output_format` is `stream-json` (the default) or `text`:

- `stream-json` makes the CLI print one JSON record per event while the agent
  works. The adapter reads them as they arrive and turns them into the live
  progress lines described in [usage.md](usage.md#watching-an-agent-run);
  the final `result` record's text is what the controller parses for the
  `CONTROL_RESULT` block, exactly as with `text`. The CLI ends every turn
  with a `result` record, and runs another turn when a background task or a
  scheduled wakeup the agent left pending fires, so one run can carry
  several; the last one is the run's. An error result (`is_error`, a
  subtype other than `success`) in any turn, a line that is not JSON, or a
  turn without its `result` record when the CLI exits fails the step as a
  provider failure. A record larger than the per-line bound is counted and skipped
  unless it is the `result` record, which must arrive whole.
- `text` is the opt-out: the CLI prints the final text once, at exit, so
  the run shows no progress and gives no sign of activity while it works
  (the pre-launch line, the heartbeat and the end line still appear). It
  requires `max_runtime_seconds`, which is then its only limit
  ([Agent limits](#agent-limits)).

Any other value is a configuration error.

### OpenCode profiles

`provider: opencode` runs the OpenCode CLI, 2.0.0 or newer (#186); `autoforge
doctor` fails a 1.x CLI, or a version it cannot read, without quoting the
CLI's output. It checks every `command` an OpenCode profile names, a wrapper
that profiles of another provider also name included: that command then has
to pass each provider's check. The profile's fields map as follows:

- `model` is `provider/model`, for example `openai/gpt-5.6-luna`, and carries
  no `#variant` suffix of its own.
- `effort`, when set, is sent as the model's `#<variant>` suffix
  (`-m openai/gpt-5.6-luna#high`); OpenCode 2 has no `--variant` flag. An
  effort the model does not offer makes OpenCode exit 1.
- `options.output_format` must be `default` (the final assistant text on
  stdout, tool traces on stderr), and `options.auto_approve: true` adds
  `--auto`.
- `extra_args` are appended after the adapter's own flags.

The prompt travels on stdin, never in argv: OpenCode 2 duplicates or
re-quotes a message given in argv, and reads one that starts with `-` as a
flag. A CLI that exits 0 without having read the whole prompt from its
stdin is reported as a failed run, not read as an answer.
`--standalone` is always passed, so the agent runs on a private server that
is the CLI's own child rather than on a shared background service whose
tools would outlive the invocation:

```text
opencode run --standalone -m <provider/model>[#<effort>] --format default
   [--auto] [extra_args]                                  (prompt on stdin)
```

### Pi profiles

`provider: pi` runs [Pi](adr/0003-pi-agent-provider.md) (`pi` 1.0.0 or
newer). The profile's fields map as follows:

- `model` is `<pi-provider>/<model-id>`, for example
  `openai/gpt-5.6-terra` or `openai-codex/gpt-5.6`: exactly one `/`, a
  lowercase provider name, and no `:<thinking>` suffix (the thinking level
  comes from `effort`).
- `effort` is required and is one of `off`, `minimal`, `low`, `medium`,
  `high`, `xhigh`, `max` (`pi --thinking`).
- `extra_args` must be empty: AutoForge owns the whole Pi argv, and the
  prompt travels on stdin, never in argv:

  ```text
  pi --mode rpc --no-session --no-approve --no-extensions --no-skills
     --no-prompt-templates --no-themes --offline --tools <list>
     [--no-context-files] --model M --thinking E
  ```

  No project trust decision, extension, package, MCP server, skill,
  prompt template or theme is loaded, and nothing is installed at startup.
  [Pi policy](pi-policy.md) gives the reasons and the evidence.
- `options.tools` is the comma-separated list of Pi built-in tools the
  agent gets (`read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`,
  `powershell`; no spaces, no duplicates). Without it, `analyze_execute`,
  `fix` and `replan_reexecute` get `read,bash,edit,write`, and every other
  profile `read,bash`. This is defence in depth, not a write barrier:
  `bash` can still write.
- `options.context_files` (`true` by default) lets Pi read `AGENTS.md` /
  `CLAUDE.md` from the agent directory and from every directory from `/`
  down to the worktree; `false` turns all of them off
  (`--no-context-files`).
- `options.require_oauth` (`true` by default) requires the ChatGPT sign-in.
  `doctor` fails, and a launch is refused before anything starts, when
  `OPENAI_API_KEY` would reach Pi through the allow-list. Every launch first
  runs `pi auth check` (below), and Pi is started only when it reports a
  ready OAuth sign-in for the model's provider.

`autoforge doctor` checks the Pi version and runs
`pi auth check --model M --json --no-refresh` under the same environment
allow-list an agent launch gets. Pi's own `OPENAI_API_KEY` therefore counts
only if `execution.env_allowlist_extra` forwards it. "Ready" means Pi holds a
credential for the model's provider. It does not prove that this model is
available to that credential; the first real run does. To sign in, run `pi`
and `/login openai` yourself; AutoForge never starts a login. A real Pi
phase runs `pi --mode rpc --no-session` and sends the prompt as a JSON
record on stdin. Before the prompt it checks that Pi resolved exactly the
configured model and thinking level, and it fails the phase otherwise.
A dry run shows the argv and launches nothing. A Pi failure inside the
protocol (a rejected prompt, a model error, an extension asking for input)
fails the phase with state unchanged; the reason and a short summary are in
the step's `execution.json`. The example file carries a commented Pi block
for `review_round_2_5`.

`doctor` also warns (it never fails) when Pi would follow instructions
from outside the checkout on every run: `SYSTEM.md` or `APPEND_SYSTEM.md`
in Pi's agent directory (no flag turns those off), or, unless every Pi
profile sets `context_files: false`, a context file there or in a directory
above the checkout. With the default `execution.worktree_dir` that includes
the operator's own checkout. Pi has no approval layer: run unattended Pi
phases in a container, a VM or under a dedicated user
([Pi policy](pi-policy.md) §7).

## Local mode configuration

The `local:` block configures [Local mode](local-mode.md): `feature_dir`,
`max_fix_rounds` and the argv-array `validation_commands`, plus the
workspace policy (`exclude`, `max_workspace_entries`, `max_workspace_bytes`)
that decides what the workspace fingerprint covers and when a tree is too
large to bind.

A local run needs only the profiles its configured bound can reach: with the
default `max_fix_rounds: 1` that is `analyze_execute`, `fix`,
`review_round_1` and `review_round_2_5`. Local review rounds are routed
exactly like remote ones, so `max_fix_rounds: 5` or more also requires
`review_round_6_plus`. `local doctor` and the start of a `local run` check
that, rather than leaving it to fail five fix rounds in. `replan_reexecute`
and `update_epic` belong to the remote lifecycle only.

A local run freezes part of its configuration as its run contract when it
starts (`local.exclude`, both cost bounds, `local.validation_commands`,
`local.max_fix_rounds`, `workflow.max_total_steps`, the prompt version); a
resumed run refuses a changed value rather than adopting it. See
[Local mode](local-mode.md#the-run-contract).
