# ADR 0003. Pi agent provider: RPC boundary, model mapping, failure channel

- **Status:** accepted for implementation. Nothing in it is implemented yet.
  Decided in #128 for EPIC #127. Implemented by #129 (config, doctor),
  #130 (duplex child primitive), #131 (adapter), #132 (resource policy).
- **Where (planned):** `src/autoforge/providers.py` (`PiProvider`),
  `src/autoforge/pi_rpc.py` (Pi wire protocol), `src/autoforge/executor.py`
  (duplex child handle), `src/autoforge/engine.py` (`_invoke_phase`, one
  provider-neutral check), `docs/agent-guides/architecture.md`
- **Related:** ADR 0001 (LOCAL workspace boundary), ADR 0002 (nothing an
  agent starts outlives its invocation), #126 (OpenCode interactive handoff;
  read, not modified)

## 1. Context

AutoForge drives Claude Code and OpenCode as one-shot CLI children: the
prompt is one argv element, `executor.execute` runs the child with
`stdin=DEVNULL` in its own process group, and the engine reads the
transcript from stdout. `ControllerEngine._invoke_phase` decides in this
order: `timed_out` raises `ExecutionTimeoutError`, `exit_code != 0` raises
`ExecutionError`, and otherwise `parse_control_result(result.stdout_tail, ...)`
runs. A malformed block is corrected by a fresh invocation
(`execution.max_correction_attempts`). Nothing else can report "the agent
failed": the only failure signals are a non-zero exit and a missing or
invalid block.

EPIC #127 adds Pi (npm `@earendil-works/pi-coding-agent`) as a third
provider. The main reason is Pi-managed ChatGPT subscription sign-in with no
`OPENAI_API_KEY` in AutoForge's environment. Pi fits the one-shot shape
poorly. It reports failure inside its output stream rather than through its
exit code, it can resolve a model other than the one named, and it clamps
the reasoning level without saying so. This ADR fixes the public shape of
the integration before #129 to #132 build it.

### Evidence base and how it was verified

All Pi facts are from `earendil-works/pi` tag `v1.0.0` (commit `a13d35a`,
published to npm 2026-10-01). Paths are relative to that repository. Docs
are under `packages/coding-agent/docs/`, and the same pages are on
`pi.dev/docs/latest`.

**Pi is not installed on the machine this ADR was written on.** No `pi`
command, login or model call was run, and no credential file was read.
Every decision below that depends on Pi is therefore marked **verified
from docs/source only**. #131 must re-verify each one against an installed
Pi before relying on it, and record the result there. No decision is marked
"verified against installed Pi". Decisions 2.9 and 2.10 do not depend on
Pi behaviour and say so where they are made.

Pi 1.0.1 (2026-10-03) was compared against 1.0.0. Its changelog and diff
touch none of the RPC commands, events, flags or print-mode behaviour this
ADR depends on. Its one nearby change is that "model is at capacity" became
a retryable error, which only means `willRetry` and `auto_retry_*` events
occur more often.

## 2. Decisions

Each item gives the decision, the rejected alternatives, and the Pi
evidence.

### 2.1 Protocol: RPC (`pi --mode rpc`)

**Decision.** AutoForge speaks Pi's stdio RPC protocol: one
`pi --mode rpc` child per invocation, JSONL commands on stdin, JSONL
responses and events on stdout.

What RPC gives the controller that the alternatives do not:

- **Pre-prompt verification.** `get_state` reports the resolved `model`
  (full Model object with `provider` and `id`) and `thinkingLevel` before
  any model work happens. With RPC, a wrong model or a clamped level fails
  before any tokens are spent or tools run (§2.2, §2.3).
- **An explicit acceptance result.** The `prompt` response carries
  `success` and `data.disposition` (`started` / `queued` / `handled`), and
  `success: false` means "rejected before acceptance" (no model, no API
  key, authentication failed).
- **An explicit terminal event.** `agent_settled` means "Pi will not
  continue automatically through retries, compaction recovery, or queued
  messages". It is emitted from a `finally` block, so it arrives on success,
  error and abort. `agent_end` is never terminal: retries, compaction or
  follow-ups can continue after it, whatever `willRetry` says.
- **Structured failure.** The last assistant `message_end` carries
  `stopReason` and `errorMessage`. `auto_retry_end` carries `success` and
  `finalError`. No prose exit message has to be parsed.
- **Graceful cancellation.** `abort` before the process-group kill.
- **Visible dialogs.** An extension dialog arrives as an
  `extension_ui_request` that the client must answer. It is never silently
  resolved.
- **A path to post-MVP interaction** (#134) over the same transport.

**Rejected: the TypeScript SDK.** The official SDK "embeds Pi in a Node.js
or Bun process" and gives "direct TypeScript access". There is no Python
SDK. Using it would mean a Node sidecar that AutoForge writes, ships and
versions, which is a second agent runtime inside a Python controller.

**Rejected for now, recorded as the fallback: one-shot print mode
(`pi -p`).** Print mode would run through the unchanged
`executor.execute`. It prints each text block of the last assistant
message, and exits 1 when that message's stop reason is `error` or
`aborted` or when the invocation throws (including startup failures such
as an unresolvable `--model`). It is not adopted because the controller
would lose three things:

- model and thinking verification before the run (the clamp is silent);
- the distinction between "prompt rejected" and "failed after acceptance";
- graceful abort.

It also exits 0 with no output when an extension handles the prompt.

**Rejected: `--mode json`.** It streams the same events as RPC but in one
shot, and "a failed or aborted assistant response ... does not by itself
produce a nonzero exit status". The controller would need RPC's whole
reducer without RPC's `get_state`, `abort` or dialog answering.

**Fallback trigger.** AutoForge switches the Pi adapter to `pi -p` (with
the same model and thinking flags, `--no-session`, and #132's resource
flags) only if one of these holds:

- the duplex child primitive (#130) cannot meet ADR 0002's bounds (every
  wait bounded, the group empty or the leftover reported) without weakening
  them;
- an installed Pi release changes the RPC contract this ADR relies on
  (§2.8) in a way the adapter cannot follow.

Such a switch is an amendment to this ADR. It records which verification
is lost, at least the pre-prompt model and thinking check. It does not
happen silently at runtime: a Pi profile never "falls back" by itself.

Evidence: `docs/sdk.md` (opening paragraph); `docs/cli-integration.md`
(print mode exit status; JSON mode "does not by itself produce a nonzero
exit status"); `src/modes/print-mode.ts:139-161`; `docs/rpc.md` ("Protocol
records", "Run lifecycle"); `docs/rpc-commands.md` (`prompt`, `abort`,
`get_state`); `docs/json.md` ("Agent and turn events"),
`src/core/agent-session.ts:1796-1803` (`agent_settled` in `finally`).
Verified from docs/source only.

### 2.2 Naming: `provider: pi`, `model: <pi-provider>/<model-id>`

**Decision.**

- `provider: pi` names the AutoForge adapter. The default `command` is
  `pi`.
- `model` names Pi's upstream provider and model as
  `<pi-provider>/<model-id>`, for example `openai/gpt-5.6-terra`. This is
  the convention OpenCode profiles already use. No new top-level profile key
  is added, and no `options` key names the upstream provider.
- Config-time validation (#129) requires:
  - exactly one `/`;
  - a non-empty provider of `[a-z0-9-]` characters;
  - a non-empty id;
  - no whitespace anywhere.

  It rejects a model whose text after the last `:` is a thinking level
  (`off|minimal|low|medium|high|xhigh|max`): reasoning comes from `effort`
  (§2.3). Other colons are allowed, because some upstream ids contain them
  and Pi treats an unrecognised suffix as part of the id.
- The model is passed as `--model <pi-provider>/<model-id>`.
- At launch, the adapter (#131) checks the resolved model before sending
  the prompt and **fails closed** unless all of these hold:
  1. `get_state.data.model` is present;
  2. its `provider` equals the configured provider exactly;
  3. its `id` equals the configured id exactly (case-sensitive);
  4. the pair `(provider, id)` is in the `get_available_models` response.

  Check 4 is needed because of how Pi resolves `--model`. With a provider
  prefix, Pi first tries an exact id match (case-insensitive), then a fuzzy
  substring match within that provider. If both fail and the provider is
  known, it builds a placeholder "custom model id" from the provider's
  default model, writes only a stderr warning, and starts anyway. Checks 1
  to 3 catch a fuzzy match. Only check 4 catches a typo, because the
  placeholder's id equals the configured string. `get_available_models`
  lists the catalog models of providers with configured auth, and the
  placeholder is not in it. Passing check 4 shows that the model exists and
  that Pi has a credential for its provider. It does not show that a
  ChatGPT credential may call that model; #135's smoke run is the proof of
  that.
- The adapter records only `provider`, `id` and `thinkingLevel`, never the
  whole Model object or model list.
- **Upstream providers.** The MVP supports and documents two:
  - `openai`: "Sign in with ChatGPT", current;
  - `openai-codex`: "OpenAI Codex (legacy)", which Pi's 0.99.0 changelog
    says the `openai` sign-in supersedes.

  Other Pi providers are **allowed but undocumented and outside the
  subscription acceptance criteria** of #127. They need `require_oauth:
  false` (#129) when they authenticate with an API key, and their keys
  reach Pi only through `execution.env_allowlist_extra`, under #132's
  rules.

**Rejected.**

- A separate `pi_provider` key or `options.provider`: it duplicates what
  the `model` prefix already carries, and it diverges from OpenCode for no
  gain.
- Passing `--provider`: the prefix in `--model` is enough, and one source
  for the provider is easier to verify.
- Trusting `--model` resolution without reading it back: it is fuzzy, and
  it can invent a model.

Evidence: `docs/cli.md` (`--model`: "Selects by exact ID or fuzzy ID/name
match. It accepts `provider/id` and an optional `:<thinking>` suffix");
`src/core/model-resolver.ts:88-166` (exact, then fuzzy),
`:175-189` and `:570-596` (fallback "Using custom model id"),
`:204-257` (suffix parsing); `docs/rpc-commands.md` (`get_state`: `model`
"omitted when no model is selected"; `get_available_models`);
`src/core/model-runtime.ts:330` (available = models of configured
providers); `packages/ai/src/providers/openai.ts:14-17`
(`loginLabel: "Sign in with ChatGPT"`),
`packages/ai/src/providers/openai-codex.ts:10`; `CHANGELOG.md` 0.99.0
entry. Verified from docs/source only.

### 2.3 Reasoning: `effort` → `--thinking`, a clamp is a hard failure

**Decision.**

- `effort` maps to `--thinking <effort>`. The valid set is Pi's:
  `off|minimal|low|medium|high|xhigh|max`. #129 validates it at config
  time.
- `effort` is **required** for a Pi profile. An empty `effort` is a
  `ConfigurationError`. Pi's own default would depend on the operator's Pi
  settings, and the launch check needs an expected value.
- The adapter compares `get_state.data.thinkingLevel` with `effort`. A
  difference is a **hard failure** before the prompt is sent ("thinking
  mismatch: configured high, Pi resolved medium"). Pi clamps an unsupported
  level silently. It first rounds up to the next level the model supports,
  then down, and a non-reasoning model gets `off`. A profile that asks for
  `minimal` could therefore run at `low` or `off` without anyone noticing.
  An operator who hits this sets the level the model supports.

**Rejected.** Recording the clamp and accepting it: that is a silent change
to a configured value, which is the drift this ADR exists to prevent, and
the fix (correct the profile) is cheap.

Evidence: `src/cli/args.ts:60` (`VALID_THINKING_LEVELS`);
`src/cli/args.ts:157-166` and `src/main.ts:621-628` (an invalid value is
only a warning); `docs/cli.md` (`--thinking` "is clamped to the model's
capabilities"); `packages/ai/src/models.ts:1228-1247`
(`clampThinkingLevel`); `src/core/agent-session.ts:2565-2567`,
`:2636-2637`; `src/main.ts:488-494` (explicit `--thinking` beats a
`:suffix`). Verified from docs/source only.

### 2.4 Session policy: `--no-session`, corrections are fresh processes

**Decision.**

- Every launch passes `--no-session`. With it, `createSessionManager`
  returns `SessionManager.inMemory(...)`, which does not persist. No session
  file is written under `~/.pi/agent/sessions`, and `get_state.sessionFile`
  is absent.
- One invocation sends exactly one `prompt`. It never sends `steer`,
  `follow_up` or `new_session`.
- A correction for a malformed `CONTROL_RESULT` stays what it is today: a
  **new Pi process** with the correction prompt, under the existing
  `execution.max_correction_attempts` bound and the LOCAL launch charge.
  **Continuing a hidden conversation is not adopted**: not with a second
  `prompt` into the live process, and not with `--session-id` or
  `--continue`. A correction must see only what the controller rendered,
  exactly as it does for Claude Code and OpenCode.
- No state-schema change and no state-protocol change.

**Rejected.** In-process or persisted-session corrections: they would make
the result of a correction depend on context the controller neither renders
nor records, and they need the session state #126 owns (§2.9). Persisted
sessions are #134's question.

Evidence: `src/main.ts:358-366` (`createSessionManager`),
`src/core/session-manager.ts:1803-1806` (`inMemory` sets `persist=false`);
`docs/sessions.md`. Verified from docs/source only.

### 2.5 Final text: the last assistant message, never raw JSONL

**Decision.**

- `AgentExecutionResult.stdout` is the text of the last assistant message,
  read with `get_last_assistant_text` after `agent_settled`. It is decoded
  as UTF-8 and otherwise passed through unchanged. This matches Claude
  Code's `--output-format text`, which also returns only the final result
  text. `stdout_tail_offset` is 0. A text larger than
  `DEFAULT_MAX_OUTPUT_BYTES` keeps its tail and sets `stdout_truncated`, the
  same semantics as today.
- **Cross-check.** The adapter derives the same text from the last
  assistant `message_end` it saw and requires the two to be equal.
  Otherwise the run is a protocol failure. Pi computes the text by joining
  that message's `text` blocks with no separator and trimming the result,
  so the derivation does the same.
- **No text.** Pi sends `data: {}` with the `text` key absent when there
  is no assistant text, although the docs say `null`. An absent key, `null`
  or a non-string value is "no assistant text", a provider failure (§2.6).
  An empty or whitespace-only message trims to absent too.
- **Stop reasons.**

  | `stopReason` | Outcome |
  |---|---|
  | `stop` | Success: the text goes to the parser. |
  | `length` | Success: the text goes to the parser and `length` is recorded. A cut block fails parsing and enters the correction loop, which is the right outcome for a model that ran out of tokens. |
  | `error` | Provider failure, with a bounded, redacted `errorMessage`. |
  | `aborted` | Provider failure, unless the abort came from the deadline, which is a timeout. |
  | `toolUse`, `deferred`, `pending`, anything unknown | Protocol failure. After `agent_settled` the last message should be a completed answer. `deferred` responses (provider-side asynchronous retrieval) are not supported in the MVP. |

- **What is never parsed or logged.** Raw JSONL never reaches
  `parse_control_result` and is never written to `stdout.log`. stderr is
  Pi's diagnostics: it is bounded, logged as `stderr.log`, and never
  parsed.

**Rejected.**

- Concatenating every assistant message: earlier turns are narration, and
  the prompt contract is "end with the block". It would also differ from
  Claude Code text mode.
- Reconstructing text from `message_update` deltas: the deltas are partial
  and do not carry the final message.
- Treating `length` as a failure: it hides a usable block. The parser is
  the authority on whether the text contains one.

Evidence: `docs/rpc-commands.md` (`get_last_assistant_text`, which says
`null`); `src/modes/rpc/rpc-mode.ts:654-657` and
`src/core/agent-session.ts:4297-4318` (returns `undefined`, so the key is
absent; joins text blocks and trims; skips an aborted empty message);
`packages/ai/src/types.ts:450` (`StopReason`); `docs/message-types.md`
(assistant message, `pending`, `deferred`); `docs/rpc.md` ("Stdout is
reserved for protocol records; diagnostics and application logging go to
stderr"). Verified from docs/source only.

### 2.6 Failure channel: an optional `provider_failure` field

**Decision: candidate (a).**

- `AgentExecutionResult` gains `provider_failure: str | None = None`. It
  is a short, bounded, already-redacted reason. No existing provider sets
  it.
- `_invoke_phase` checks the result in this order:
  1. `timed_out` raises `ExecutionTimeoutError`, as today. A timeout wins
     over a provider failure reported in the same result.
  2. `provider_failure` set: record `record.error = provider_failure`
     (plus leftovers), record stdout and stderr as for a non-zero exit, and
     raise `ExecutionError` with "State unchanged". The phase does not
     advance and the correction loop does not run.
  3. `exit_code != 0` raises `ExecutionError`, as today.
  4. Otherwise `parse_control_result`, as today.

  The check sits before the exit-code check because it says more. The
  reason names the exit code when the process exited abnormally.
- The engine change mentions no provider, event or flag. Its tests cover
  "field absent" with the existing Claude, OpenCode and scripted paths
  unchanged.
- **Companion field.** `AgentExecutionResult` also gains
  `provider_summary`: a small flat mapping of scalar values, bounded and
  redacted by the adapter, that the engine writes into `execution.json`
  under that key without interpreting it. For Pi it holds:
  - the resolved provider, id and thinking level;
  - the prompt disposition;
  - the final `stopReason`;
  - retry counts and the tool-execution count;
  - whether a dialog was cancelled.

  Raw events, Model objects and message contents never go into it.
- **What counts as a Pi provider failure (#131):**
  - prompt rejected (`success: false`);
  - a disposition other than `started`;
  - final `stopReason` `error` or `aborted`, or `auto_retry_end` with
    `success: false`;
  - a model or thinking mismatch (§2.2, §2.3);
  - a protocol violation: an unknown response id, a `parse` error, a
    non-JSON stdout record, an oversize record, or a missing required
    field;
  - Pi exiting before `agent_settled`;
  - no assistant text;
  - a cross-check mismatch;
  - a cancelled extension dialog (§2.8).

  A valid run whose text has no valid `CONTROL_RESULT` is **not** a
  provider failure. It goes to the unchanged correction loop.

**Rejected.**

- **(b) The adapter raises `ExecutionError`.** `_invoke_phase` records
  empty stdout and stderr on that path (`engine.py`, the `except
  ExecutionError` around `provider.execute`), so the run log would lose
  Pi's diagnostics and the leftover facts. It would also make the adapter
  responsible for the engine's error wording.
- **(c) Synthesizing a non-zero `exit_code`.** It misreports the process
  status: Pi exits 0 after an orderly shutdown whether the run failed or
  not, and the run log must say what the process really did.

Evidence: AutoForge `src/autoforge/engine.py` (`_invoke_phase`: the
`except ExecutionError` recording `""`, `""`; the `timed_out` and
`exit_code` checks), `src/autoforge/providers.py` (`AgentExecutionResult`),
`src/autoforge/runlog.py` (`record.error` is redacted before it is
written). Pi: `src/modes/rpc/rpc-mode.ts:726-743`, `:802-805` (stdin EOF
calls `shutdown()`, exit code 0). Verified from docs/source only.

### 2.7 Module placement

**Decision.**

| Piece | Location | Knows |
|---|---|---|
| `PiProvider`: argv, model and thinking mapping, `environment_names`, the conversation driver, the conversion to `AgentExecutionResult` | `src/autoforge/providers.py` | Pi CLI flags |
| Pi wire protocol: JSON encode and decode, request ids, the event reducer, outcome classification | `src/autoforge/pi_rpc.py` (provider layer) | Pi command and event names; no flags, no processes |
| Duplex child handle (#130) | executor layer: `src/autoforge/executor.py`, or a sibling `executor_duplex.py` if it outgrows readability | bytes and LF-framed records; no JSON, no provider, no workflow |
| `provider_failure` / `provider_summary` check | `src/autoforge/engine.py` | nothing about Pi |

- `pi_rpc.py` is a pure reducer, `(records in) -> (commands out, outcome)`,
  so most of #131's tests drive it from synthetic fixtures without a
  process.
- The rule in `src/autoforge/AGENTS.md` widens from "no CLI flags outside
  `providers.py`" to "no CLI flag or provider wire-protocol name outside
  the provider layer", where the provider layer is `providers.py` plus the
  protocol modules it owns. #126 already proposes such a sibling
  (`opencode_session.py`).
- `executor.execute` stays one-shot with `stdin=DEVNULL`, and
  `test_stdin_is_closed_not_interactive` stays.
- The prompt reaches Pi as one JSON-encoded `prompt` record on stdin, never
  in argv and never through a shell. The recorded argv therefore contains
  no prompt. `prompt.md` records it as today.

**Rejected.** All Pi code in `providers.py`, because the reducer is large
and is tested on its own. A Pi module in the executor layer, because it
would put protocol semantics in a module whose contract is "no workflow
semantics".

### 2.8 Upstream compatibility

**Decision.**

- **Minimum version: Pi `>= 1.0.0`**, the version these facts were read
  from. #129's doctor checks it with `pi --version`, which prints the bare
  version, following `check_gh_version`. There is no upper bound. The
  adapter does not re-run `pi --version` at every launch. The fail-closed
  rules below are the runtime backstop, and an older Pi that lacks
  `agent_settled` ends in a bounded timeout, not a false success.
- **Pi RPC has no protocol version and no documented compatibility
  policy.** There is no version field in `get_state`, no hello record, and
  no stability statement in `rpc.md`, `rpc-commands.md`, `json.md` or
  `cli-integration.md`. The installed version is the only handle.
- **Forward compatibility rules for the reducer:**
  - unknown **event** types are ignored, counted, and recorded in
    `provider_summary`;
  - unknown fields on known records are ignored;
  - an unknown `disposition`, an unknown `stopReason`, a missing required
    field (for example `success` or `data.disposition` on a `prompt`
    response, `model.provider`/`model.id`/`thinkingLevel` in `get_state`),
    or a response to an id AutoForge never sent all **fail closed**;
  - an `extension_ui_request` dialog (`select`, `confirm`, `input`,
    `editor`) is answered at once with
    `{"type": "extension_ui_response", "id": <same>, "cancelled": true}`
    and makes the run a provider failure ("an extension asked for input").
    Under #132's policy no extension should be loaded, so a dialog means
    the policy did not hold. `editor` has no Pi-side timeout and would
    block forever unanswered. Fire-and-forget methods (`notify`,
    `setStatus`, `setWidget`, `setTitle`, `set_editor_text`) are ignored.
- **Framing** (owned by #130, required here): records are split on LF
  (0x0A) only, one trailing CR is stripped, and U+2028/U+2029 are never
  record boundaries. The reducer correlates responses by `id`, never by
  order, because Pi handles commands concurrently. A `parse` error
  response carries no id.
- **Shutdown.**
  - Normal path: close stdin. Pi disposes and exits 0.
  - Deadline or error path: send `abort` within a short sub-deadline, then
    close stdin (EOF during a run also aborts through dispose), then the
    #130 escalation of SIGTERM to the group and then SIGKILL. SIGTERM runs
    Pi's handler, which kills the detached tool groups it still tracks
    (exit 143).
  - AutoForge never sends SIGINT: Pi has no SIGINT handler in RPC mode, so
    SIGINT would skip that cleanup. `clear_queue` is not needed, because
    AutoForge never queues a message.
  - The detached-tool-children gap against ADR 0002 is #132's to close.

Evidence: `src/main.ts:632-635` (`--version`), `src/modes/rpc/rpc-types.ts`
(no version field), `docs/rpc.md` ("Framing", "Correlate commands and
responses"), `src/modes/rpc/jsonl.ts`, `src/modes/rpc/rpc-mode.ts:750-764`
(parse error without id), `:366-380` (SIGTERM/SIGHUP handlers),
`docs/rpc-extension-ui.md`, `src/modes/rpc/rpc-types.ts:263` (`editor` has
no `timeout`), `src/core/tools/bash.ts:114-160` (detached tool children).
Verified from docs/source only.

### 2.9 Coordination with #126

#126 (OpenCode interactive handoff) is open. This work does not edit it.
The Pi MVP needs neither durable sessions nor human interaction: one
`--no-session` process takes one prompt to a final text, and a correction
is a new process. #126 is therefore **off the MVP critical path**.

| Part | Classification | Owner |
|---|---|---|
| RPC JSONL client, `--mode rpc --no-session`, event reduction, `agent_settled`, `abort`, model and thinking mapping and verification, Pi trust and resource flags, `pi auth check` preflight, explicit `PI_*` names | Pi-specific (**independent** of #126) | #129, #131, #132 |
| `opencode serve`, its HTTP v1/v2 API, server password, `OPENCODE_ENABLE_QUESTION_TOOL`, rulesets, `/doc` probe, version pin, permission pre-authorization, question delivery | OpenCode-specific | #126 |
| Invocation-lifetime child primitive in the executor layer | Shared foundation | #130 builds it. If #126's primitive lands first, #130 extends it instead of adding a second one, and vice versa. Its interface must allow `stdin=DEVNULL` and plain bounded capture, which is what #126's server child needs. |
| ADR 0002 amendment for duplex or server-backed children | Shared foundation | Whichever of #132 and #126 lands second extends the amendment the first one wrote |
| Strict per-provider `options` keys | Shared foundation | #129 adds the generic mechanism and the `pi` key set. #126 adds `interaction_policy` through it |
| `provider_failure` / `provider_summary` on `AgentExecutionResult` | Shared foundation | #131 adds them. #126's server adapter can use the same fields for a server-side failure |
| Generic `ProviderSession`, `PendingInteraction`, state protocol 5→6, pending/answer/approve/reject commands, interaction policy, crash recovery and locking for a paused run | Shared foundation, **owned by #126** | #126. Pi consumes it post-MVP in #134 |

**Rejected.**

- **Making #126 a prerequisite of the Pi MVP** (#129 to #132 wait for it).
  The MVP uses none of what #126 adds: no durable session, no paused run,
  no answer or approve command, no state protocol 6. Waiting would tie an
  independent provider to an open design it does not consume.
- **Building the generic session and interaction layer in the Pi MVP.**
  The MVP has no use case for it, so it would be a speculative abstraction
  shaped by one provider that never pauses. #126 has the use case: OpenCode
  permission and question requests. Pi adopts the layer after it exists,
  in #134.
- **Pi building its own child primitive, failure channel and options
  mechanism without regard to #126.** The executor layer would get two
  invocation-lifetime primitives with overlapping contracts, ADR 0002 two
  unrelated amendments, and the profile schema two ways to validate
  per-provider options. The "whichever lands second extends the first"
  rule keeps one of each.
- **Editing #126 in this work to record the split there.** #126 is open
  under its own scope, and this issue excludes changing it. The split is
  recorded here, and the dependent Pi issues (#129 to #132) carry the parts
  they own.

Evidence: #126's issue text (read in full, not modified) for the
OpenCode-specific and #126-owned rows; ADR 0002 and
`src/autoforge/executor.py` (`execute` is one-shot with `stdin=DEVNULL`)
for the shared executor rows; `src/autoforge/providers.py`
(`AgentExecutionResult`) for the failure-channel row. Verification status:
the classification is a project-coordination decision and does not depend
on Pi behaviour, so Pi evidence is not applicable to it. The Pi-specific
row does depend on Pi. Its protocol, session, model, thinking and
event-reduction parts restate decisions 2.1 to 2.5 and 2.8 and keep their
status: verified from docs/source only, to be re-verified in #131. Its
trust and resource flags, `pi auth check` preflight and `PI_*` names are
only assigned here; #129 and #132 decide them and verify them against an
installed Pi.

### 2.10 Why an ADR

The provider boundary gains three things a later provider will reuse:

- a duplex transport;
- a provider-neutral failure channel;
- a convention for provider-layer protocol modules.

The session and correction semantics are also an explicit decision rather
than an accident of the adapter. Those warrant a record. A section in
`docs/agent-guides/architecture.md` alone would describe the boundary but
not why the alternatives were rejected. The guide summarises the placement
and links here.

Verification status: whether to write an ADR is a documentation decision
and does not depend on Pi behaviour, so Pi evidence is not applicable. The
three additions it lists come from decisions 2.1, 2.6 and 2.7, which carry
their own status.

## 3. Consequences

- `provider: pi` profiles behave like the other providers from the
  engine's point of view: one `AgentRequest` in, one `AgentExecutionResult`
  out, the same parser, correction loop and GitHub or LOCAL verification.
- Every Pi run costs two extra stdio round-trips before the prompt
  (`get_state`, `get_available_models`) and one after it
  (`get_last_assistant_text`). Each is bounded by a few-second sub-deadline
  inside the invocation's timeout (#131 picks the values).
- An operator whose model id is misspelled, not in Pi's catalog, or has no
  Pi credential gets a typed failure before any model call. One whose
  model cannot run the configured `effort` gets a "thinking mismatch"
  failure, not a quieter run.
- No state change and no prompt-template change.
- Pi's RPC contract is young and unversioned. A Pi upgrade can break the
  adapter. The fail-closed rules turn that into a typed failure, never into
  an accepted result.

## 4. Deltas from the planning text of #127 to #132

Reading the source turned up these differences from the planning text.
They are decided here, and the child issues are updated to match.

1. `get_last_assistant_text` with no text sends `data: {}` (key absent),
   not `text: null`, and its text is trimmed. #131's fixtures and its
   cross-check must use both facts (§2.5).
2. `--model <provider>/<unknown-id>` does not fail: Pi starts with a
   placeholder model. Model verification therefore adds the
   `get_available_models` membership check (§2.2, #131). For the same
   reason, #129's doctor cannot treat `pi auth check --model ...` reporting
   `ready` as proof that the model exists. That check resolves the same
   placeholder.
3. `effort` is required for Pi profiles, and an empty effort is a config
   error (§2.3, #129).
4. `stopReason` has two more values, `pending` and `deferred`. Only `stop`
   and `length` are successes (§2.5, #131).
5. A cancelled extension dialog is a provider failure, not only a recorded
   fact (§2.8, #131).
6. `AgentExecutionResult` gains `provider_summary` next to
   `provider_failure`, so the Pi summary reaches `execution.json` without a
   Pi name in the engine (§2.6, #131).
7. "`openai-codex` is superseded" is stated in Pi's changelog (0.99.0),
   not in `docs/providers.md` (§2.2).
8. Pi has no SIGINT handler in RPC mode, so the duplex handle's escalation
   must start with SIGTERM, as `_terminate_group` already does (§2.8,
   #130).
