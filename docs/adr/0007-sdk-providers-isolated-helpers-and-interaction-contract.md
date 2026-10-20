# ADR 0007. SDK providers: isolated helpers, interaction capabilities, compatibility pins and the #126 / #134 contract

- **Status:** accepted for implementation. #149's part (D2.7, D10.1, D10.2
  and the configuration rows of D6.3) is implemented, with two dated
  amendments (D6.3 and D10.2 below); its record is
  [docs/sdk-policy.md](../sdk-policy.md). The rest is not implemented yet.
  It is implemented by #134 (the provider-neutral
  interaction contract and its broker), #126 (the one state bump, `pause`
  and continuation in the engine, the operator commands, crash recovery
  including OpenCode's), #149 (SDK policy and configuration), #151 (the
  Claude SDK helper and its adapter) and #152 (the OpenCode SDK bridge, the
  server it owns and its adapter). The number is assigned when the record
  merges: if another ADR merges first, this record takes the next free
  number, and its index entries move with it.
- **Decides:** #148, for EPIC #147 (Workstream A). #148 expected the number
  0004; ADRs 0004 to 0006 merged first.
- **Amends:** ADR 0003 §2.9, with a dated note: the shared session and
  interaction foundation moves from #126 to #134, and #126 consumes it
  (D6.1). No other ADR's text changes.
- **Where:** placement is decided in D2.2, D6.3 and §5. Existing code this
  record builds on: `src/autoforge/executor_duplex.py`, `providers.py`,
  `claude_stream.py`, `pi_rpc.py`, `progress.py`, `loop_detect.py`,
  `child_env.py`, `sandbox_policy.py`, `engine.py`, `state.py`,
  `run_contract.py`, `config_fingerprint.py`, `locking.py`, `cli.py`,
  `errors.py`.
- **Related:** ADR 0002 (nothing an agent starts outlives its invocation),
  ADR 0003 (Pi provider; §2.9 coordination with #126), ADR 0004 (D1.3: an
  approval never grants a controller-only operation), ADR 0005 (D3.1 and
  D10.7: every SDK helper is sandboxed, with one containment owner), ADR
  0006 (protocol numbering); #23 (state protocol numbering); #154 (shared
  lifecycle events); #155 (provider parity tests).

## 1. Context

### 1.1 What the code does today

#148 was planned against `main` `2fff4b9`. The references below are re-read
on `main` `af439c2`.

**Three providers, all CLIs, none of which can ask a human anything.**

| Provider | Launch | Executor path | When the agent wants a decision |
|---|---|---|---|
| Claude Code | `claude -p`, text or `stream-json` | one-shot, or duplex with stdin on `/dev/null` (`claude_stream.py`, #192) | nothing asks: `permission_mode` defaults to `bypassPermissions` (`providers.py` `ClaudeCodeProvider.validate_profile`) |
| OpenCode | `opencode run`, prompt on stdin (#186) | one-shot | nothing asks: `--auto` only with `options.auto_approve: true` (`OpenCodeProvider.build_command_for`) |
| Pi | `pi --mode rpc`, one `prompt` record | duplex (`pi_rpc.py`) | an extension dialog is answered `cancelled: true` and fails the run (ADR 0003 §2.8) |

**What an SDK provider needs, and where AutoForge already has it.**

- A duplex child: `executor_duplex.py` (#130, ADR 0002 §4a). It frames LF
  records, drains both pipes continuously, bounds its queue, puts every
  operation under the idle timeout and the ceiling, and tears down on every
  exit, `KeyboardInterrupt` included. It knows no JSON.
- Provider-layer reducers: `pi_rpc.py` and `claude_stream.py` are pure
  reducers an adapter drives (ADR 0003 §2.7).
- A failure channel: `AgentExecutionResult.provider_failure` and
  `provider_summary` (ADR 0003 §2.6), and `transient_failure` (#164).
- Strict per-provider `options` keys (#129).
- Logical isolation and the sandbox: `child_env.py` (#165) and
  `sandbox_policy.py` with each adapter's `sandbox_requirements` (#168).
- Containment: `contain_orphans` (ADR 0002 §4b). At most one contained
  invocation runs per controller process.

**What does not exist.**

- No interaction type, broker, resolver or pending-interaction record. ADR
  0003 §2.9 assigned `ProviderSession`, `PendingInteraction`, the state
  bump, the `pending` / `answer` / `approve` / `reject` commands, the
  interaction policy and crash recovery to #126, and Pi's adoption to #134.
  Neither is implemented.
- The state protocol is `9` (`autoforge.__protocol_version__`). Label `6` is
  reserved for #126 and refused as unknown (`state.py`, the comment above
  `_LEGACY_PROTOCOLS`; `tests/test_state.py`,
  `test_protocol_6_and_other_unknown_labels_are_refused_and_left_unchanged`).
- The CLI exits 0, 1 (`BLOCKED`, a failed step) or 2 (configuration, state
  or configuration drift). No status means "waiting for a human".
- The repository lock (`locking.py`) is held for a whole CLI command
  (`ControllerEngine.locked()`), so every agent launch runs under it.
- An agent launch has an idle timeout (default 900 s,
  `DEFAULT_IDLE_TIMEOUT_SECONDS`) and an optional ceiling. ADR 0002 §4c
  already says a wait for a human answer is not activity and never extends a
  deadline.
- LOCAL charges each write-capable launch before it starts, at most three per
  phase entry (`MAX_LOCAL_PHASE_ATTEMPTS`, `_charge_local_launch`). Its run
  contract classifies provider, model, effort, command and timeouts as
  DYNAMIC (`run_contract.py`).
- REMOTE records provider, model, effort and limits per profile
  (`config_fingerprint.py`, #219). `--accept-config-change` replaces the
  record.
- The controller has no runtime dependency (`pyproject.toml`:
  `dependencies = []`).

**Why an SDK cannot be imported as another controller dependency.**

- The Python SDK builds the CLI's environment from the whole `os.environ`
  and merges `options.env` on top (§1.2: SC :819-825). `env` can add or
  override a variable but never remove one. Imported into the controller, it
  would hand the CLI the operator's whole environment and undo #165.
- Both SDKs start children of their own outside the executor: the Claude
  CLI, and `opencode serve`. Those children get no allow-list, no group
  kill, no containment and no sandbox, and ADR 0005 D3.1 requires a sandbox
  for every launch under `sandbox.mode: required`.
- The SDKs bring dependency trees the controller does not have: `anyio`,
  `jsonschema` and `mcp` for Python; Node and `cross-spawn` for OpenCode.

### 1.2 Evidence base and how it was verified

Two kinds of evidence are kept apart everywhere in this record.

**Published source and docs, read and never run.** #147's source ledger is
not quoted; every fact below was re-read from these artifacts on
2026-10-14. Where a citation gives a short prefix and line numbers, the
prefix names a file of the artifact on the same row.

| Artifact | Identity | Files read |
|---|---|---|
| `claude-agent-sdk` 0.2.163, wheel `py3-none-manylinux_2_17_x86_64` (PyPI, uploaded 2026-09-30) | sha256 `260cb955d237b7e1897f1dbf9ee5efbf815d8622d0c7459a05409c56432d8501` | `claude_agent_sdk/_internal/transport/subprocess_cli.py` (SC), `_internal/query.py` (IQ), `_internal/client.py` (IC), `client.py` (CL), `types.py` (TY), `_internal/message_parser.py` (MP), `query.py`, `_cli_version.py`, the version strings of `_bundled/claude`, `dist-info/METADATA` |
| `@anthropic-ai/claude-agent-sdk` 0.3.289 (npm, published 2026-10-03) | registry tarball | `package.json`, `manifest.json`, `sdk.d.ts`, `sdk.mjs` |
| `@opencode-ai/sdk` 1.18.34 (npm, published 2026-09-30) | tarball sha256 `308bf267188fa0e114ff8ed3bbb2e1a09ef5ad327e863fb9f17f8d35a65a1dcf`; its sha512 matched the registry's integrity value | `package.json`, `dist/index.js`, `dist/server.js`, `dist/process.js`, `dist/client.js`, `dist/v2/client.js`, `dist/gen/*`, `dist/v2/gen/*` (`v2/gen/types.gen.d.ts` is VT, `v2/gen/sdk.gen.d.ts` is VS, `gen/types.gen.d.ts` is GT) |
| `opencode-ai` 1.18.34 (npm, published 2026-09-30) | tarball sha256 `279923bb5754e810b173a8a7401cf17699fccad3c1159500c10c477887f0b53b` | `package.json`, `postinstall.mjs` |
| OpenCode runtime source, GitHub `anomalyco/opencode` tag `v1.18.34` | tag | `packages/opencode/src/permission/index.ts`, `question/index.ts`, `server/auth.ts`, `server/server.ts`, `cli/cmd/serve.ts`, `cli/network.ts`, `tool/registry.ts`, `effect/runtime-flags.ts`; `packages/core/src/permission.ts`, `question.ts`, `permission/saved.ts`, `v1/permission.ts` |
| Claude docs | fetched 2026-10-14 | `code.claude.com/docs/en/agent-sdk/overview`, `.../agent-sdk/quickstart`, `.../agent-sdk/python`, `.../agent-sdk/typescript`, `.../agent-sdk/typescript-v2-preview`, `.../agent-sdk/sessions`, `.../agent-sdk/streaming-vs-single-mode`, `.../agent-sdk/hooks`, `.../hooks#defer-a-tool-call-for-later` |
| OpenCode docs | fetched 2026-10-14 | `opencode.ai/docs/sdk/`, `opencode.ai/docs/server/` |

The research baseline of #148 exists as stated: Python SDK 0.2.163 bundling
Claude Code 2.1.286 (`_cli_version.py:3`), TypeScript SDK 0.3.289, and
`@opencode-ai/sdk` and `opencode-ai` 1.18.34. Newer releases at the time of
writing are not adopted here:

- Python SDK 0.2.167 (2026-10-14), which bundles CLI 2.1.300;
- TypeScript SDK 0.3.300 (2026-10-14), whose `claudeCodeVersion` is 2.1.300;
- `@opencode-ai/sdk` and `opencode-ai` 1.18.35 and 1.19.0, the latter the
  `latest` tag of each.

npm has no 2.x of either OpenCode package.

**Installed runtime probes: none.** No SDK was imported, no helper or server
was started, no model was called and no credential was read. The machine
this record was written on has Claude Code 2.1.300 and OpenCode `v2.0.27`,
a Homebrew `opencode-v2` build rather than the npm `opencode-ai` line. Only
their `--help` and `--version` were read. Neither is a pair this record
pins, so neither is runtime evidence for one.

Every capability below is therefore **source-only** until the isolated
runtime probe its gate names in §3 passes. Documentation alone never makes
a capability supported.

### 1.3 Upstream decisions this record builds on

- **ADR 0002.** An invocation is complete when the child exited, both pipes
  reached EOF and the group is empty. §4a holds the duplex handle to that.
  §4b: one contained invocation per controller process. §4c: a human wait is
  not activity. §4e: a sandbox's namespace teardown.
- **ADR 0003.** §2.1 rejected Pi's TypeScript SDK because it means a Node
  sidecar AutoForge writes and ships. §2.4: a correction is a fresh process.
  §2.6: the failure channel. §2.9: the split with #126, which this record
  amends.
- **ADR 0004.** D1.3: an approval changes what a provider tool may attempt,
  never which credential the process holds. D10: the agent's read context.
- **ADR 0005.**
  - D3.1 names the SDK helpers among the launches the sandbox covers.
  - D7 covers auth projection.
  - D8.3: #149's configuration and session roots are declared.
  - D10.7: an SDK helper and the OpenCode server it starts share one
    sandbox and one containment owner.
  - D11.1: a provider-native sandbox is never the boundary.

### 1.4 Terms

- **Helper.** The process AutoForge launches for an SDK provider through
  the duplex handle: the *Claude helper* (Python, imports the Claude Agent
  SDK) or the *OpenCode bridge* (Node, imports the OpenCode SDK's client).
- **Provider runtime.** What a helper starts: the Claude Code CLI, or the
  `opencode serve` server.
- **Live writer.** A helper whose provider runtime may still change the
  workspace.
- **Interaction.** A provider's request for a human decision: a *question*
  or a *permission*.
- **Pause.** Ending an invocation with a durable pending interaction and no
  live process.
- **Continuation.** The launch that delivers a paused interaction's
  decision into the same provider session.
- **Logical invocation.** A launch together with its continuations. A
  correction starts a new one.

## 2. Decisions

Each decision names the child that implements it, or says it is
documentation only. A decision naming two children is implemented by each
for its own helper.

### 2.1 Claude primitive (item 1)

**D1.1 (#151).** **The Python `ClaudeSDKClient`, inside an isolated helper
process.** One helper per launch makes these calls in order:

1. `connect()`;
2. at most one `query()`, with the rendered prompt; a continuation sends
   none (D9.4);
3. `receive_response()` until the `ResultMessage`;
4. `interrupt()` on a cancellation;
5. `disconnect()` at the end.

`connect` creates one transport and one CLI process (CL :163-167), and
`query` writes a user message to that process's stdin (CL :295-306). So the
invocation has one CLI process and one live session context from start to
end.

| | Python `query()` | Python `ClaudeSDKClient` | TypeScript streaming `query()` |
|---|---|---|---|
| Process and session | a new CLI process per call (IC :92-98); "Creates a new session by default" (Python docs) | one process from `connect` to `disconnect`; "Reuses same session" (Python docs) | one process per `query()`, with an `AsyncIterable` prompt (`sdk.d.ts` :3246-3249) |
| Interrupt | "❌ Not supported" (Python docs) | `interrupt()` (CL :308) | `Query.interrupt()` (`sdk.d.ts` :2860), a control request "only supported when streaming input/output is used" (:2847-2850) |
| Permission callback and hooks | yes: it always streams internally (IC :138-147) | yes | yes |
| Hook events | 10 (TY :284-295) | 10 | 33 (`HOOK_EVENTS`, `sdk.d.ts` :959), adding for example `SessionStart`, `SessionEnd`, `PermissionDenied`, `Elicitation` |
| `env` option | merged over the inherited `os.environ` (SC :819-825) | same | replaces the child's environment when set (`sdk.d.ts` :1647-1660) |
| Runtime | Python `>=3.10` (`METADATA`) | same | Node, `engines` `>=18.0.0`; the native CLI comes from one of eight platform `optionalDependencies` |
| CLI it is paired with | 2.1.286, bundled in the platform wheel | same | 2.1.289 (`package.json` `claudeCodeVersion`), from the platform package |

*Rationale.* The client has the explicit lifecycle a continuation needs: it
can connect to a resumed session without sending a prompt. It interrupts,
and it keeps one live context. Python `>=3.10` fits AutoForge's `>=3.11`, so
a Claude launch adds no Node runtime, and the helper is tested with the
repository's own tooling. The TypeScript `env` replacement is the one
property the Python SDK lacks. It is not needed, because the helper's own
environment is already the isolated one (D2.1).

*Rejected.*

- **Python `query()`.** It cannot interrupt, and it combines connecting and
  sending, a split that a no-prompt continuation needs kept apart.
- **The TypeScript streaming `query()`.** It has more hook events, but none
  of them is needed: the interaction design uses `PreToolUse` and the
  permission host only (§2.9). It would add Node to every Claude launch, and
  its CLI pairing (2.1.289) differs from the Python wheel's (2.1.286).
- **The TypeScript V2 preview** (`unstable_v2_createSession`,
  `unstable_v2_resumeSession`, `unstable_v2_prompt`). It was marked
  `@deprecated` ("Use `query()` instead. The V2 session API will be removed
  in a future release") from 0.2.133 (2026-05-07) to 0.2.141, the last
  0.2.x release, and removed in 0.3.142 (2026-05-14). 0.3.289's
  `sdk.d.ts` has no `unstable_v2` symbol, and the docs page
  `agent-sdk/typescript-v2-preview` is marked "(removed)".
- **Speaking the CLI's control protocol without an SDK.** `control_request`
  and `control_response` over `--permission-prompt-tool stdio` (IQ
  :577-751) are the SDK's private protocol with the CLI. The SDK is its
  documented client, and AutoForge would otherwise own an undocumented
  protocol.

**D1.2 (documentation only).** **What would justify changing the
language.** Only an amendment to this record moves the Claude helper to
TypeScript, and only if one of these holds:

- A capability that §3 needs for a supported feature exists only in the
  TypeScript SDK, and an isolated probe shows it. One example is a hook
  event the Python `HookEvent` lacks that the interaction design comes to
  need.
- The Python SDK stops shipping a platform wheel with a bundled CLI for a
  platform AutoForge supports.
- The Python SDK falls behind the CLI on a protocol change that a gate
  depends on.
- Anthropic deprecates the Python SDK.

Richer events or convenience alone do not justify it.

### 2.2 Process isolation and dependencies (item 2)

**D2.1 (#151, #152).** **No SDK is imported in the controller process.**

- An SDK runs only inside a helper, and AutoForge launches the helper like
  every other agent:
  - through `executor_duplex.start_duplex`;
  - with the allow-listed environment and #165's overrides
    (`AgentRequest.env`);
  - with `contain_orphans` and the outer sandbox plan;
  - under the idle timeout and the ceiling.
- The provider runtime a helper starts inherits the helper's environment,
  which is already the isolated one. **Isolation happens before the SDK is
  imported or starts anything.**
- An SDK `env` option is an overlay, never a filter:
  - the Python SDK builds `{**os.environ without CLAUDECODE,
    "CLAUDE_CODE_ENTRYPOINT": "sdk-py", **options.env,
    "CLAUDE_AGENT_SDK_VERSION": ...}` (SC :819-825). It then adds
    `CLAUDE_CODE_SDK_READS_SESSION_STATE=1` unless set (:44, :858-861),
    `PWD` when `cwd` is set (:867-868),
    `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING` when requested (:863-865),
    and, when `opentelemetry` is importable and a span is active,
    `TRACEPARENT` and `TRACESTATE` (:827-851);
  - the OpenCode server launcher builds `{...process.env,
    OPENCODE_CONFIG_CONTENT}` (`dist/server.js` :12-17).
- A helper itself sets no variable it did not receive in its launch
  request, except where this record names one:
  `CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK` (D4.1) and the server password
  (D3.4). It never reads a credential file. What the SDK adds on its own,
  listed above, carries no credential and no operator data. The helper
  requests no file checkpointing, and #151 records whether the locked
  environment (D2.3) makes `opentelemetry` importable.
- A test pins that no module of the `autoforge` package outside
  `sdk_helpers/` imports `claude_agent_sdk`, that nothing imports
  `autoforge.sdk_helpers`, and that no Node program starts during an
  `autoforge` import. The helper programs are data to the controller (D2.2),
  never imported by it.

*Rejected.*

- **Importing the SDK in the controller with a scrubbed `os.environ`.**
  Mutating the controller's own environment around a call is global state
  in a threaded process. It would still leave the SDK's children outside
  the executor.
- **The TypeScript SDK's `spawnClaudeCodeProcess` hook as the isolation
  point** (`sdk.d.ts` :2444, `SpawnOptions` :9489-9523). Isolation would
  then depend on a per-SDK callback, and the Claude decision is the Python
  SDK anyway (D1.1).

**D2.2 (#151, #152).** **Helper programs are AutoForge package data,
pinned by digest and staged per launch.**

- **The Claude helper:** `src/autoforge/sdk_helpers/claude_helper.py`, a
  single file that imports only the standard library and `claude_agent_sdk`.
  It imports nothing from `autoforge`, and nothing in `autoforge` imports it.
  The repository's checks never install the SDK, so #151 keeps `mypy src`
  and the tests passing without it, for example with a stub of the few SDK
  names the helper uses.
- **The OpenCode bridge:** `src/autoforge/sdk_helpers/opencode_bridge.mjs`,
  one self-contained, precompiled ES module with the SDK client inlined.
  - It is built from `helpers/opencode-bridge/`. That directory's
    `package.json` pins exact versions, including `@opencode-ai/sdk`
    `1.18.34` and the bundler, and its `package-lock.json` is committed.
  - CI checks that the committed bundle's digest equals the manifest's.
    #152 decides whether CI also rebuilds the bundle to prove that.
  - The SDK's license notice ships with the bundle.
- **A manifest,** `src/autoforge/sdk_helpers/manifest.json`, records for
  each helper:
  - the file;
  - its sha256;
  - the envelope protocol version (D4.5);
  - the SDK version it was built and tested against.
- **At every launch** the adapter checks the file's digest against the
  manifest. It then writes the file into a controller-owned per-launch
  directory, through `safefs`, and that directory is projected read-only,
  as #165's overrides directory is (ADR 0005 D8.3). The helper runs from
  there by absolute path. A digest mismatch refuses the launch.
- **Why staging and not a direct path:** a launch never runs a program
  from the controller's checkout or its virtual environment. In REMOTE,
  ADR 0005 denies the controller checkout to the launch (its D13.2), and
  an editable install and its `.venv` usually lie inside it.

**D2.3 (#151).** **The Claude runtime environment is optional,
operator-installed and separate from the controller's.**

- It is a Python `>=3.10` environment, outside the controller checkout.
- It contains exactly `claude-agent-sdk==0.2.163`, installed from
  `helpers/claude-sdk/`, whose lock is committed with hashes. It is never
  resolved at install time against an index range, and it is installed
  from wheels only: the 0.2.163 sdist carries no CLI, so building it
  would leave the helper without the pinned runtime (D2.4).
- The `claude-sdk` profile's `command` names that environment's
  interpreter. It is resolved the way every provider command is (ADR 0005
  §2.5): the environment's prefix is the install root, projected
  read-only.
- The argv is `[<interpreter>, "-I", <staged claude_helper.py>]`. `-I`
  keeps out `PYTHON*` variables, the user site directory and the script's
  own directory.
- The controller's `pyproject.toml` stays `dependencies = []`, and no extra
  is added for it.

**D2.4 (#151).** **The Claude Code binary is the one bundled in the
platform wheel, never another.**

- 0.2.163's wheels are platform wheels only: manylinux 2.17 x86_64 and
  aarch64, and macOS 11 arm64 and x86_64. There is no `py3-none-any`
  wheel and no Windows wheel. Its sdist bundles no CLI.
- The wheel carries `claude_agent_sdk/_bundled/claude`, an executable of
  241,667,256 bytes on x86_64 Linux, whose embedded `VERSION` is `2.1.286`.
- The helper passes that file's absolute path as `cli_path`. The SDK's own
  lookup therefore never runs. That lookup tries the bundled file, then
  `shutil.which("claude")`, then `~/.npm-global/bin/claude`,
  `/usr/local/bin/claude`, `~/.local/bin/claude` and other fixed paths
  (SC :255-339).
- A platform without the wheel has no Claude SDK backend. `doctor` says so,
  and a launch is refused.

*Rejected.* **An operator-named `claude` executable.** The SDK and the CLI
speak a private control protocol that is versioned together, so a separate
CLI would no longer be the tested pair, and D4.1 would mean nothing.

**D2.5 (#152).** **The OpenCode runtime and Node are operator-installed and
named in the profile.**

- The `opencode-sdk` profile's `command` names Node, resolved like any
  provider command.
- `options.server_command` names the OpenCode runtime binary of the pinned
  release (D4.2). The adapter resolves it on the launch's `PATH` and passes
  the absolute path to the bridge. The bridge never searches `PATH` itself
  (D3.1).
- The npm package `opencode-ai` places its platform binary at install time
  (`postinstall.mjs` :96-117, :146-155, :167-175), replacing the package's
  `bin/opencode.exe` shell stub, so the runtime is a native binary, not a
  Node wrapper. When the platform package is missing, the postinstall runs
  `npm install` of it (:126-144). That is the operator's install, never a
  launch's (D2.6).
- Both executables are declared in the adapter's sandbox requirements, as
  data, never as a mount. The adapter's requirements are extended for a
  second executable where needed.
- This provider is separate from the `opencode` CLI provider, whose
  runtime may be another release line, such as the `v2.0.27` on the
  authoring machine.

**D2.6 (#151, #152).** **Nothing is installed, downloaded or resolved at
launch or in `doctor`.**

- No `pip`, `uv`, `npm`, `npx` or `bunx` runs, and no package manager
  resolves anything.
- Neither SDK's discovery runs: the Claude CLI comes from an explicit
  `cli_path`, and the server from an absolute path.
- The OpenCode SDK has no install code (`dist/*`), and the bridge never
  calls the server's `global.upgrade` route (VS :315-322; D3.5).
- `doctor` reads versions and digests only:
  - the helper digests;
  - the interpreter's `claude-agent-sdk` version, from package metadata in
    a subprocess;
  - the bundled CLI's `_cli_version.py`;
  - `node --version`;
  - the runtime's `--version`.

**D2.7 (#149).** **Configuration and session roots are #149's.**

- A Claude continuation needs the session transcript the paused launch
  wrote ("The SDK writes it to disk automatically", sessions docs). The
  directory that holds it is therefore a persistent `dir-rw` dedicated to
  AutoForge (ADR 0005 D7.1), never the operator's `~/.claude` (ADR 0005
  D7.2).
- A deferred session stays on disk "until you resume it, subject to the
  `cleanupPeriodDays` retention sweep, which deletes session files after 30
  days by default" (hooks docs). #149 sets the retention for the dedicated
  root. A continuation whose transcript is gone ends `BLOCKED`, never as a
  new session under the recorded id (D9.3).
- An OpenCode continuation needs the session history the paused server
  wrote, so the server's data directory is one too.
- #149 chooses these directories and what the provider may load from them.
  This record requires only that a continuation runs on the same directory
  as its paused launch (D8.4). #149's choice: per issue, under the issue's
  sandbox directory, `<sandbox dir>/sdk/<provider>/`
  (docs/sdk-policy.md §6).

### 2.3 OpenCode ownership (item 3)

**D3.1 (#152).** **The bridge starts `opencode serve` itself and talks to it
through `createOpencodeClient`. The SDK's `createOpencode` and
`createOpencodeServer` are never used.**

The SDK's launcher, from published source:

- it runs the hard-coded command `opencode`, resolved on `PATH` by
  `cross-spawn`, with no option to name another binary (`dist/server.js`
  :12-17);
- it gives the server the whole `process.env` plus `OPENCODE_CONFIG_CONTENT`
  (:12-17);
- its default port is the fixed 4096 (:4-8);
- it waits at most 5 s for a stdout line starting
  `opencode server listening` (:20-34);
- it appends stderr to a buffer that is never bounded (:49-51);
- `close()` calls `proc.kill()`, which on POSIX sends SIGTERM to the direct
  child only (`dist/process.js` :7-12). It uses no `detached` and no process
  group;
- `createOpencode` gives its client no authentication header
  (`dist/index.js` :5-16).

The bridge's own launch of the server:

- The runtime runs by the absolute path from `start` (D2.5), with `serve
  --hostname=127.0.0.1 --port=0`. `--port` is always passed, because a
  configured `server.port` would otherwise replace the default
  (`cli/network.ts` :6-11, :69).
- With port 0, the pinned runtime binds 4096 when it is free and otherwise
  a port the operating system assigns (`server/server.ts` :117-122). The
  bridge therefore never assumes a port. It reads the bound one back from
  the readiness line, which the runtime prints as `opencode server
  listening on http://<host>:<port>` (`serve.ts` :20). Whichever port it
  gets, the per-launch password (D3.4) keeps every other local client out.
  Gate G-O12 proves the read-back on the pinned pair.
- Its environment is exactly the bridge's own, which already carries
  #149's configuration variables (`AgentRequest.env`), plus the server
  password (D3.4). The SDK's default `OPENCODE_CONFIG_CONTENT` is not
  set.
- It is not `detached`, so it stays in the bridge's process group, where
  the executor's group signals reach it (ADR 0002 §3). Its stdin is ignored.
- Its stdout and stderr are drained continuously. Readiness is read from
  stdout. Everything else is copied to the bridge's stderr, which the
  controller captures and bounds, so no buffer grows without bound.
- Readiness has a sub-deadline inside the invocation's limits.
- The bridge never uses `shell` and never uses `cross-spawn`.

*Rejected.*

- **SDK-owned `createOpencode`.** The command is hard-coded, the
  environment is inherited, and only the direct child is stopped, as listed
  above.
- **The controller starting the server as its own duplex child, next to the
  bridge.** That makes two contained invocations at once, which ADR 0002
  §4b refuses, or one uncontained server.
- **The bridge picking a free port itself.** A port found free can be
  taken before the server binds it, so this needs a retry loop, and it
  protects nothing the runtime's own fallback does not: the password, not
  the port number, keeps other clients out.
- **`opencode serve --stdio`.** The installed `v2.0.27` help lists it, but
  the research pair's `cli/cmd/serve.ts` declares no such option, and
  nothing about its protocol was verified. It can be reconsidered by amendment.

**D3.2 (#151, #152).** **One containment owner per helper: the
controller's duplex invocation of it.**

- The helper is the duplex child. Its provider runtime is the helper's
  child, in the same process group. So is everything the runtime starts:
  the CLI's tools, and the server's language servers, MCP servers and
  shells.
- ADR 0002 applies to the whole tree: the subreaper catches what left the
  group, and the outer sandbox, where enforced, holds all of it (ADR 0005
  D10.7).
- The Python SDK starts the CLI with `anyio.open_process` and neither a new
  session nor a process group (SC :873-881), so the CLI stays in the
  helper's group.
- A helper sets up no containment of its own. The controller never starts
  a provider runtime as a second contained invocation. No `contain_orphans`
  context is nested in, or concurrent with, another.

**D3.3 (#151, #152).** **Cleanup ordering, draining and bounds.**

1. **Normal end.** The helper writes its outcome record, and the adapter
   reads it and closes the helper's stdin. Then:
   - the Claude helper calls `disconnect()`, which closes the CLI's stdin,
     waits up to 5 s, then sends `terminate()`, waits 5 s, `kill()`s and
     waits 5 s, signalling the direct child only (SC :962-1061);
   - the bridge aborts the session if it is still busy, closes the event
     stream, sends SIGTERM to the server, waits within a sub-deadline,
     sends SIGKILL and waits.

   Each helper then exits 0. The duplex handle's `finish()` bounds all of it
   by the invocation's deadline.
2. **Stdin EOF at any time** is an orderly-shutdown request. A running turn
   is interrupted first (Claude `interrupt()`, OpenCode `session.abort`),
   then cleanup continues as in step 1.
3. **SIGTERM to the helper** is the executor's group kill, which every
   member gets at once. The helper exits promptly and installs no handler
   that delays it past the kill grace.
4. **A controller timeout, stop, exception or `KeyboardInterrupt`** runs the
   duplex teardown (ADR 0002 §4a): stdin is closed, the helper gets a
   bounded wait (at most the exit grace on an exception), the group is
   checked for emptiness, and the group is killed with SIGTERM and then
   SIGKILL. Under a sandbox the namespace teardown follows (ADR 0002 §4e).
5. **A helper crash** (an uncaught exception): the helper stops its runtime
   first, writes one bounded `failure` record if it can, and exits non-zero.

Draining:

- the controller drains the helper's stdout and stderr (the duplex handle);
- the bridge drains the server's stdout and stderr (D3.1);
- the Python SDK drains the CLI's stdout. The helper passes no `stderr`
  callback, so the CLI inherits the helper's stderr (SC :871), which the
  controller drains and bounds.

Bounds:

- every record either side writes fits `max_record_bytes`. A helper reports
  an oversize outcome as a failure instead of writing it;
- every HTTP request the bridge makes, except the event stream, has its own
  timeout. The SDK client's default fetch disables timeouts
  (`dist/client.js`: `req.timeout = false`), so the bridge passes its own
  `fetch`;
- the Python SDK's control requests to the CLI time out after 60 s by
  default (IQ :706-751).

Neither helper writes heartbeats. Only provider activity reaches the
controller as output, so the idle timeout keeps its meaning (ADR 0002 §4c).

**D3.4 (#152).** **Server authentication.**

- Each launch gets a fresh password of at least 128 bits from the
  operating system's random source. The server receives it as
  `OPENCODE_SERVER_PASSWORD`, which turns on HTTP Basic authentication
  (`server/auth.ts` :17-34).
- The bridge sends it as an explicit `Authorization: Basic` header through
  the client's `headers`, with the username `opencode`: the server's
  default while `OPENCODE_SERVER_USERNAME` is unset (:17-20), and the
  bridge never sets that variable. The generated operations declare no security
  scheme, so the client's `auth` option is never applied.
- The password is never written to a record, a log or an argv.
- Reason: the loopback port is in the shared network namespace (ADR 0005
  D9.1), so any local process can reach it.
- **A tool that obtains the password can answer its own asks.** The
  password opens every route of the server, not only those D3.5 lets the
  bridge call. A tool holding it can therefore reply `once` or `always` to
  a pending ask, before the bridge's reject arrives or while a paused ask
  waits for the stop (D9.6). That breaks no authority boundary, because an
  approval never carries a credential (ADR 0004 D1.3). But the operator's
  decision is then not the one the server applies. A tool has two ways to
  the password that the source read here leaves open:
  - its own environment. It inherits the server's unless the runtime
    removes the password, and the source does not say whether it does;
  - the server's process, or any other process that started with the
    server's environment. `/proc/<pid>/environ` holds "the initial
    environment that was set when the currently executing program was
    started", whatever the program later removes, behind a
    `PTRACE_MODE_READ_FSCREDS` check (proc_pid_environ(5)). A same-user
    process passes that check unless the target is not dumpable or a
    security module refuses: Yama restricts only `PTRACE_MODE_ATTACH`
    (ptrace(2)). The outer sandbox does not separate a tool from those
    processes, because the bridge, the server and its tools share one
    sandbox and its PID namespace (D3.2; ADR 0005 D10.7).

  Nothing read here closes the second way, so the first release plans for
  the advisory outcome below.
- **An OpenCode permission decision is an enforcement control only after
  gate G-O11 passes** on the pinned pair (§3). The gate passes when a probe
  tool, under #149's policy and the sandbox, cannot obtain the password by
  any of these routes:
  - its environment;
  - `/proc/<pid>/environ`, `cmdline` and `mem` of every other process of
    the invocation: the bridge, the server and what the server starts,
    which inherits the server's environment;
  - any file it can read.

  A route that is open under any host setting AutoForge supports, such as
  Yama's `ptrace_scope` for `mem`, counts as open.
- **Until G-O11 passes, and for good if it fails, OpenCode permission asks
  are advisory and unavailable as controls.**
  - `live_permissions` is false for `opencode-sdk`, whatever G-O4 shows.
    So is the pause capability for a permission ask, whatever G-O6 shows
    (D5.3).
  - With neither true, the broker has no live answer and no pause to
    give. It answers every OpenCode permission ask `Fail`, under every
    policy (D7.1, D7.4). The bridge rejects the ask, and the invocation is
    a provider failure (D7.6).
  - That reject is not what keeps the action from running, since a tool
    may have approved the ask first. #149's policy relies on no OpenCode
    permission ask as a control. What an OpenCode tool must never do is
    kept from it by logical isolation and the outer sandbox, which bound
    what a child reaches when it deliberately tries (ADR 0005 D1.1; ADR
    0004 D1.3). OpenCode's own permission rules are at most defence in
    depth (D10.1).
- **Questions are not gated by G-O11.** An answer changes what the agent
  was told, never what a tool may do, so no policy treats a question as a
  control.

**D3.5 (#152).** **The bridge calls a fixed set of routes and no other**:
session create, prompt or admission, abort or interrupt, message history,
status, the event stream, the version read-back (D4.2), and permission and
question list, reply and reject. In particular it never calls
`global.upgrade`, configuration writes, authentication routes or PTY routes.

**D3.6 (documentation only).** **Externally owned servers are not a
supported backend, and no server lives between invocations.**

- The client can connect to any server: "If you already have a running
  instance of opencode, you can create a client instance to connect to it"
  (SDK docs). But a server AutoForge did not start has the operator's
  environment and configuration, an unknown lifetime and credential, and no
  sandbox or containment.
- No profile option names a base URL in the first release.
- A pause keeps no server or daemon alive: ADR 0002 has no exemption for
  one. A continuation starts a new server on the same data directory
  (D2.7).

### 2.4 Compatibility pairs (item 4)

**D4.1 (#151).** **Claude: `claude-agent-sdk` `==0.2.163` with its bundled
CLI `2.1.286`, exactly.**

- The pin is in the helper environment's lock (D2.3).
- The helper sets `CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK`, because the SDK's
  own check is weaker than this one. It only logs a warning below `2.0.0`,
  and its `-v` subprocess inherits the environment (SC :37, :809-810,
  :1164-1197).
- The helper's `hello` reports `claude_agent_sdk.__version__` and
  `__cli_version__`. The adapter compares both with the manifest before it
  sends `start`.
- A mismatch is a provider failure ("SDK pair mismatch"). It never falls
  back to the `claude` CLI provider or to another binary.

**D4.2 (#152).** **OpenCode: the bridge is built against `@opencode-ai/sdk`
`1.18.34` and talks to runtime `opencode-ai` `1.18.34`, both exactly.**

- After readiness and before it creates any session, the bridge reads the
  runtime's version from the server itself, through the health route.
  The bridge's `server` record (§5) then carries it for the adapter to
  compare. Gate G-O13 proves the route reports it.
- This read-back replaces the `/doc` probe ADR 0003 §2.9 listed for #126:
  once the version is pinned exactly, the API it serves is the pinned
  source's, so no API description is read at launch.
- A mismatch is a provider failure.
- The research pair is older than the OpenCode `v2.0.27` on the authoring
  machine, and npm publishes no 2.x. #152 re-verifies which release line it
  pins when it is implemented. If runtime evidence moves the pair, for
  example to the v2 line, #152 amends this decision and comments on #148
  and #152 (D4.6).

**D4.3 (#152).** **Know which namespace is which.**

- `@opencode-ai/sdk` (v1, `dist/gen/*`) has no `v2` namespace. Its client
  exposes `session`, `event`, `config` and others, and its calls take
  `{path, body, query}` (GT; `gen/sdk.gen.d.ts` :377-402).
- `@opencode-ai/sdk/v2` (`dist/v2/gen/*`) has a different `OpencodeClient`,
  whose calls take flat `parameters`.
  - It exposes the classic routes (`session`, `permission`, `question`,
    `event`, ...).
  - It also has `get v2(): V2`, whose class covers the `/api/*` routes (VS
    :2205-2302).
- The same concept differs by route family:
  - a classic `permission.asked` event versus an `/api` `permission.v2.asked`
    event (VT :5887-5904, :5767);
  - questions exist only in the v2 module.

The bridge imports only `@opencode-ai/sdk/v2`, and uses one route family
per concept, never a classic ask with an `/api` reply. #152 chooses the
family per concept from its probes and records the choice here as an
amendment. Two constraints already hold:

- creating a session under a caller-supplied id is possible only through
  `POST /api/session` (D9.5);
- an `/api` ask that carries a `save` list is never answered "always",
  because that reply writes a rule to the project's store (D9.6).

The envelope (§5) does not expose the choice.

**D4.4 (#152).** **Node 24 or later; no Bun.**

- `@opencode-ai/sdk` declares no `engines`. Its only hints are the
  devDependencies `@tsconfig/node22` and `@types/node` 24.12.2. Its
  exports are ESM-only, with `import` and `types` conditions only.
- The floor is Node 24, a supported LTS line. `doctor` and the bridge's
  `hello` check it.
- #152's probe proves on Node 24:
  - global `fetch`;
  - that the bundle imports as an ES module;
  - the client's behaviour, including its server-sent-events client, which
    resends `Last-Event-ID` and retries with backoff
    (`dist/v2/gen/core/serverSentEvents.gen.js` :16-17, :119-127).
- Bun is not required and not supported. Nothing in the bundle needs it.

**D4.5 (#151, #152).** **Each helper envelope carries a protocol version.**

- Each helper's `hello` names its envelope and `protocol: 1` (§5).
- The adapter supports exactly one version per helper and refuses any
  other before `start`.
- The version moves whenever a record or field changes meaning.
- Helper programs ship with the controller (D2.2). A mismatch therefore
  means a corrupted or foreign file, which the digest check normally
  catches first.

**D4.6 (documentation only).** **Updating a pair is an amendment.** A pair
moves only through a change that does all of these:

- updates the lock or the bundle and the manifest;
- re-runs §3's probes on the new pair;
- records the results here, as ADR 0003 §1.1 did for Pi.

When runtime evidence changes a decision, the implementing child comments
on #148 and on the affected issues. #126 and #134 are never closed or
superseded silently (§8).

### 2.5 The provider-neutral surface (item 5)

**D5.1 (#134).** **The contract** is `src/autoforge/interaction.py`: plain
types, no I/O, no provider name and no SDK type.

- **`InteractionRequest`:**
  - `request_id`: controller-assigned, stable within the logical
    invocation;
  - `provider_request_id`: an opaque string;
  - `kind`: `question` or `permission`;
  - a bounded, redacted rendering. For a question: its text, header, option
    labels and descriptions, and whether it is `multiple` and whether a
    custom answer is allowed. For a permission: the tool name, a bounded
    summary of the input or patterns, and the input digest;
  - the agent it came from (main, or a named subagent);
  - `offers`: the decisions the provider can carry for this request,
    derived from the capabilities (D5.3).
- **`InteractionDecision`:** one of these. Deferral is not among them: it
  is the broker's directive (D5.2), never an operator's decision.
  - `answer(choices, custom_text)`;
  - `allow(scope, updated_input)`, where `scope` is `once` or `session` and
    `updated_input` is optional;
  - `deny(guidance)`, with optional guidance;
  - `cancel`.
- **`SessionIdentity`:**
  - the provider, and the pair's versions (D4.1, D4.2);
  - the policy digest (#149);
  - the profile name and the digest of its routing fields and options;
  - the working directory and the session root (D2.7);
  - the provider session id;
  - the workflow identity the engine already binds a launch to:
    - REMOTE: repository, issue, phase, attempt, review round, and the PR
      and HEAD binding where the phase has one;
    - LOCAL: the state root, the phase, and the launch index of the
      logical invocation's first launch.
- **`Resolver`:** `resolve(request, deadline) -> InteractionDecision |
  Unavailable`. The engine supplies one: the terminal resolver in `cli.py`,
  or a scripted one in tests.
- **`InteractionPolicy`:** `prompt`, `pause` or `fail` (D7.1).
- **`Capabilities`:** declared by the adapter per provider and pair:
  - `live_questions`, `live_permissions`;
  - `durable_replay` (the provider replays the same structured request
    after a restart);
  - `continuation_recovery` (the same session continues after an explicit
    message);
  - `interrupt`;
  - and the set of decision kinds the provider cannot carry.
- **The lifecycle discriminant** of an invocation's outcome: `completed`,
  `failed`, `interrupted` or `paused`.

**D5.2 (#134).** **The broker.**

- The engine creates one per launch and hands it to the adapter as
  `AgentRequest.interaction`, the way it hands over `progress`. `None`
  keeps today's behaviour for every existing provider.
- The adapter maps a provider ask to an `InteractionRequest` and calls
  `broker.handle(request, invocation_due)`, where `invocation_due` is the
  duplex handle's `limit_due()` (D7.3). That returns a directive:
  `Deliver(decision)`, `Pause` or `Fail(reason)`.
- The broker owns:
  - the policy;
  - the per-wait and aggregate limits (D7.3);
  - the capability check: a decision the provider cannot carry is never
    offered;
  - a bounded, redacted record of each request and decision for the run log.
- The broker never sees a provider record, and the adapter never sees a
  policy.

**D5.3 (#151, #152).** **A capability is declared true only after its gate
passes.**

- An adapter declares `Capabilities` per pair. A value is true only when
  every §3 gate of its row passed on that pair. OpenCode's permission rows
  need G-O11 besides their own gate (D3.4).
- Everything else is false, and the broker treats false as unavailable.
- `offers` is derived from the capabilities.

**D5.4 (documentation only).** **What the surface is not.**

- It is not a transport. The broker carries decisions, not bytes, and each
  helper keeps its own envelope (§5).
- No provider SDK type crosses into the engine.
- The requirements come from Claude and OpenCode, not from Pi's optional
  dialogs. Those stay cancelled (ADR 0003 §2.8) until #134's second part
  adopts the contract for Pi.

### 2.6 Ownership (item 6)

**D6.1 (documentation only; amends ADR 0003 §2.9).** **Who delivers what.**

- **#134, part 1** delivers the contract and the broker (D5.1, D5.2), the
  `prompt` and `fail` policies with their limits (§2.7), the terminal
  resolver, and the two named type extensions (D10.5). It is in-memory
  only: no state.
- **#126** consumes #134 part 1. It delivers:
  - the one state bump (D6.2);
  - the pending-interaction record and its binding (§2.8);
  - `pause` and exit status 3 (D7.5);
  - the operator commands (`pending`, `answer`, `approve`, `reject`; the
    final names are #126's);
  - crash and receipt recovery (D9.8, D9.9);
  - OpenCode recovery by continuation message (D9.6).
- **#151 and #152** deliver the transports: the helpers, the envelopes and
  the adapters, with live interaction through the broker. #152's bridge is
  the official client #126 uses for OpenCode, in place of a server child
  and HTTP client of #126's own.
- **#149** delivers SDK policy and configuration (§2.10): provider names,
  option keys, tool and permission policy, configuration and session roots,
  and auth.
- **#134, part 2,** Pi's adoption, stays post-MVP and depends on #126.
- The old edge #126 → #134, where #134 waited for #126, narrows to that
  optional Pi adoption. #126 now depends on #134 part 1, and only #134
  part 2 depends on #126, so there is no cycle (§4).

**D6.2 (#126).** **Exactly one state bump, owned by #126.**

- It goes from the protocol current when #126 merges (9 today, so 10 if it
  merged now), and its number is settled on #23 first, under ADR 0004
  D13.1's rule.
- Label `6` stays skipped for good: never written, and refused as unknown,
  as `tests/test_state.py` pins. #126 updates the comments that call it
  "reserved for #126".
- #134, #149, #151 and #152 add no persisted field and no bump. A
  provider's session data is part of #126's record, and no protocol version
  is reserved per provider.

**D6.3 (#134, #126, #149, #151, #152; each for its own rows).**
**Placement.**

| Piece | Location | Owner | Knows |
|---|---|---|---|
| contract and broker | `src/autoforge/interaction.py` | #134 | no provider, no I/O |
| terminal resolver | `src/autoforge/cli.py` | #134 | the terminal |
| `InteractionRequiredError` | `src/autoforge/errors.py` | #134 | — |
| Claude helper program | `src/autoforge/sdk_helpers/claude_helper.py` (package data, D2.2) | #151 | the Python SDK; nothing from `autoforge` |
| Claude envelope codec and reducer | `src/autoforge/claude_sdk.py`, provider layer | #151 | the envelope's record names; reuses `claude_stream.py`'s turn and result rules where the shapes match |
| OpenCode bridge source and lock | `helpers/opencode-bridge/` | #152 | the OpenCode SDK |
| OpenCode bridge bundle and manifest | `src/autoforge/sdk_helpers/` (package data) | #152 | — |
| OpenCode envelope codec and reducer | `src/autoforge/opencode_sdk.py`, provider layer | #152 | the envelope's record names |
| adapters `ClaudeSdkProvider`, `OpenCodeSdkProvider` | `src/autoforge/providers.py` | #151, #152 | argv, environment names, sandbox requirements |
| provider names `claude-sdk`, `opencode-sdk`, their option keys, `interaction_policy` and its limits | `src/autoforge/config.py` | #149 | — |
| pending-interaction record, pause, continuation, exit status 3 | `src/autoforge/state.py`, `engine.py`, `cli.py` | #126 | provider-neutral |
| the continuation message template | `src/autoforge/prompts/` | #126 | — |

Once implemented, `src/autoforge/AGENTS.md`'s boundary rule names the two
new protocol modules beside `pi_rpc.py` and `claude_stream.py`. The helper
programs are separate executables, not modules of the provider layer.

*Amendment (2026-10-20, #149).* The configuration row is split between two
modules:

- **`src/autoforge/config.py`** holds the provider names
  (`KNOWN_PROVIDERS`) and the check that a live wait ends before the
  profile's limits (`agent_limits`).
- **`src/autoforge/sdk_policy.py`** (provider layer) holds the option keys,
  `interaction_policy` and its limits. Config validation reaches them
  through the adapters' `validate_profile`, as it reaches every provider's
  option keys (#129). That module holds the rest of #149's policy too:
  [docs/sdk-policy.md](../sdk-policy.md).

*Rejected.*

- **The SDK as an optional extra of `autoforge`, run with the controller's
  own interpreter.** The controller's environment would carry the SDK and
  a 241 MB binary. In REMOTE, the interpreter and the helper would usually
  lie inside the masked controller checkout (D2.2).
- **A separately versioned helper package.** The envelope would then drift
  apart from the adapter that speaks it. Staging package data (D2.2) keeps
  them in one release.

### 2.7 Interaction policy, lock and deadlines (item 7)

**D7.1 (#134).** **Three policies, per profile:**
`options.interaction_policy` (#149 validates it). The default is `fail`,
which keeps today's behaviour, where nothing can ask.

- **`fail`.** Every interaction fails the invocation (D7.6).
- **`prompt`.** The broker asks the resolver live. The helper and its
  runtime stay alive while it waits.
- **`pause`.** The broker answers `Pause`, and the run stops durably
  (D7.5). It is accepted only once #126 has landed, and only for a
  profile whose provider's pause capability is true (D5.3). Otherwise
  config validation refuses it.

**D7.2 (#134).** **There is never a live writer without the lock.**

- The controller holds the repository lock for the whole CLI command that
  launches a helper (`ControllerEngine.locked()`, `locking.py`), so a live
  helper always runs under it.
- Nothing releases the lock while a helper lives. A pause releases it only
  when the command ends, after the teardown.
- No design hands a live session to another controller process.
- This holds as far as ADR 0005 D10.6 does. Under `sandbox.mode: required`
  a helper dies with its controller. Under `disabled`, a helper left behind
  by a controller killed with SIGKILL is the same residual risk every
  provider has today.

**D7.3 (#134).** **Limits.**

- **Per wait:** `max_wait_seconds`, default 120.
- **Aggregate:** `max_interactions` per logical invocation, default 8. It
  counts every request the broker handled, answered or not. One more
  request after the limit is `Fail("interaction limit reached")`.
- **Both run inside the existing limits.** The executor's idle timeout and
  ceiling keep running through a wait, which neither stops nor resets them
  (ADR 0002 §4c).
  - The ask itself is output and resets the idle timer.
  - Config validation (#149) refuses a per-wait limit that is not strictly
    below the profile's idle timeout, and below its ceiling when one is set.
    A wait that uses its whole allowance therefore ends before the idle
    timeout fires.
  - The resolver's deadline is the earlier of `max_wait_seconds` after the
    ask and `invocation_due` (D5.2). Only the first is D7.4's expired wait.
    When the ceiling falls due first, the invocation ends at it as the
    timeout it is today, never as a pause: no time is left for the
    teardown a pause needs.
- **Claude's hook timeout.** The CLI enforces a `PreToolUse` hook's
  `HookMatcher.timeout` (default 60 s; TY :618-619, IQ :335-336). The
  helper sets it to the per-wait limit plus a margin, so the CLI never
  times a waiting hook out first. A hook timeout that happens anyway is a
  provider failure (gate G-C10).

**D7.4 (#134).** **When a live wait degrades.** Under `prompt`, the broker
turns each of these into `Pause`:

- the controller's stdin is not a terminal (checked before it asks);
- end of file on the operator's input;
- the per-wait limit expires.

It does so only when the provider's pause capability for that request kind
has passed its gate (§3) and #126 has landed. Otherwise the result is
`Fail`. Nothing else degrades.

**D7.5 (#126).** **`pause`: a safe checkpoint, then teardown, then exit
status 3.**

1. The adapter ends the provider's turn the provider's way:
   - Claude: `defer` (D9.2);
   - OpenCode: the bridge leaves the ask unanswered, and the adapter closes
     its stdin (D9.6).
2. The adapter checks that the provider's account of the paused request
   matches the request the broker paused on:
   - Claude: the `deferred_tool_use` name, id and input digest;
   - OpenCode: the ask's id, permission and patterns, or its questions.
3. The teardown completes: the helper has exited and the group is empty.
4. The engine saves the pending-interaction record atomically (D8.4).
5. `run`, `local run`, `step` and `resume` print how to answer and exit
   with status 3,
   "paused, waiting for a decision". The phase is unchanged and not
   `BLOCKED`.

If the controller crashes between steps 3 and 4, nothing is pending. The
next `resume` relaunches the phase as a fresh, charged launch. The
abandoned session is never resumed, because only a recorded binding permits
a continuation (D8.4). The deferred tool did not run.

**D7.6 (#134).** **`fail` is a typed failure that is never corrected.**

- The adapter reports `provider_failure` (for example "the agent asked for
  a decision (permission: Bash); interaction_policy is fail") with the
  lifecycle discriminant `failed` on `AgentExecutionResult.interaction`.
- The engine raises `InteractionRequiredError`, an `ExecutionError`. The
  phase is unchanged; what the engine saved before the launch (the attempt
  counter and, for a LOCAL write phase, the launch charge) stands, as
  today.
- There is no correction and no transient relaunch: a relaunch would ask
  again.
- The ask itself is denied, or rejected. That keeps the action from
  running only under D10.4's conditions.

**D7.7 (#134).** **Ctrl-C and a denial with guidance are neither of them a
deferral.**

- Ctrl-C interrupts:
  - the duplex teardown closes the helper's stdin, and the helper
    interrupts the turn first (D3.3, step 2);
  - the step ends interrupted, with the phase unchanged, as today;
  - nothing is written as pending.
- A denial with guidance is a decision. The live session goes on with it.

**D7.8 (#126).** **A routine wait is not `BLOCKED`.**

- A paused run stays in its phase with a pending record.
- A `resume` with no decision recorded exits 3 again. It launches and
  charges nothing.
- `BLOCKED` is reserved for a recovery that cannot decide (§2.9).

### 2.8 Accounting and binding (item 8)

**D8.1 (#126).** **A continuation belongs to the same logical attempt.**

- It has the same review round and the same charged workflow step.
  `step_count` is charged once, for the step that launched the original,
  and never again for its continuations.
- It is not a correction and not a transient relaunch. `state.attempt`
  counts launches and is saved before each one (`engine.py`,
  `_invoke_phase`), so it still advances for a continuation, as for every
  launch. It is a launch counter, not the logical attempt.
- The REMOTE correction and transient-relaunch counters of the step carry
  over.

**D8.2 (#126).** **Every new write-capable LOCAL process is charged.**

- A continuation of a write-capable LOCAL phase is a launch, so
  `_charge_local_launch` charges it before its helper starts.
- The three-launch bound per phase entry therefore allows a LOCAL write
  phase at most two continuations before `BLOCKED`.
- REMOTE has no launch budget beyond the step budget, and D8.1 applies.

**D8.3 (#126).** **A correction is a new logical invocation.**

- A malformed `CONTROL_RESULT` after a continuation is corrected by a new
  helper with a new session (ADR 0003 §2.4), never by a message into the
  continued session.
- The pending record persists the logical invocation's interaction count
  and the step's correction count. A restart therefore neither resets the
  aggregate limit nor grants a fresh correction allowance.

**D8.4 (#126).** **The pending record binds the session, not the current
configuration.**

- It records the `SessionIdentity` (D5.1).
- A continuation launches only if the current launch would produce an
  identical identity. Otherwise the continuation is refused.
- The controller never resumes "the most recent session" (Claude
  `continue_conversation` / `--continue` is never used) and never
  re-records a binding from the current configuration.
- REMOTE's `--accept-config-change` (#219) does not rebind a pending
  session. The operator restores the configuration, or discards the
  pending interaction with #126's command. Discarding ends the logical
  invocation, and the phase relaunches fresh, charged as a new launch.

**D8.5 (#126).** **LOCAL needs no new run-contract field.**

- `LocalRunContract` is unchanged: provider, model and effort stay DYNAMIC
  for the run, and `CONTRACT_SCHEMA` does not move.
- The pending record is a session-specific binding. While a session is
  pending, those inputs are fixed for it. Once it ends, they are DYNAMIC
  again.

### 2.9 Recovery proof obligations (item 9)

**Claude.**

**D9.1 (#151).** **The caller chooses the session UUID before the launch.**

- The engine generates the UUID. Once #126 has landed, the engine persists
  it in #126's launch checkpoint before the helper starts. Before that,
  nothing is persisted and no continuation exists.
- The helper passes it as `session_id`, which becomes `--session-id`
  (SC :651-655). TY :2043-2048: "Must be a valid UUID. Cannot be used with
  `continue_conversation` or `resume` unless `fork_session` is also set".
- The session id of `system/init` and of every `ResultMessage` must equal
  it. A difference is a provider failure.
- A continuation passes `resume=<uuid>` (`--resume=`, SC :647-649), never
  `continue_conversation` and never `fork_session`, and its `system/init`
  must report the same id.

**D9.2 (#151).** **Deferral is single-tool, through `PreToolUse`, with a
permission host.**

- **How the helper defers.** Interaction-eligible calls are routed through
  the helper's `PreToolUse` hook: `AskUserQuestion` always, and the tools
  #149's policy marks `ask`. The hook sends `ask` and waits for the broker
  (D7.3). The decision or directive maps to the hook output:

  | Decision or directive | Hook output |
  |---|---|
  | `answer` | `permissionDecision: "allow"`, with the answers in `updatedInput` |
  | `allow` | `"allow"`, with `updatedInput` for a modified input |
  | `deny` | `"deny"` with `permissionDecisionReason`, carrying the guidance |
  | `cancel` | `"deny"` |
  | `Fail` | `"deny"` with the failure reason |
  | `Pause` | `"defer"` (TY :441) |

- **The docs:**
  - "`"defer"` exits gracefully so the tool can be resumed later."
  - "Claude Code honors this value only in non-interactive mode with the
    `-p` flag."
  - "`"defer"` only works when Claude makes a single tool call in the turn.
    If Claude makes several tool calls at once, `"defer"` is ignored with a
    warning and the tool proceeds through the normal permission flow."
  - `updatedInput`: "For `"defer"`, ignored."
  - The deferred result: `"stop_reason":"tool_deferred"` with
    `"deferred_tool_use":{"id","name","input"}`, which `ResultMessage` types
    (TY :1300-1311, :1340-1377). The TypeScript types put `tool_deferred`
    under `terminal_reason` (`sdk.d.ts` :9685); gate G-C6 records where the
    pinned pair puts it.
- **No `-p`.** The SDK never passes `-p` (SC :570-795). The CLI's help says
  non-interactive mode also applies when stdout is not a terminal. Whether
  the CLI honours `defer` under the SDK's argv is gate G-C6.
- **Several tool calls in one turn.** The ignored call reaches the
  permission host (`can_use_tool`, which the SDK wires as
  `--permission-prompt-tool stdio`, TY :1940-1949). The helper denies it,
  so it never runs, and the invocation is a provider failure: "the provider
  ignored a deferral (several tool calls in one turn)".
- **A permission host is required.** In non-interactive mode the CLI
  offers `AskUserQuestion` "only when the run has a permission host to
  receive the prompt, such as an Agent SDK `canUseTool` callback" (hooks
  docs). The helper therefore always passes `can_use_tool`.
- **The continuation needs the same permission host.** "The calling
  process runs `claude -p --resume <session-id>` with the same permission
  host. The same tool call fires `PreToolUse` again." The continuation
  therefore runs with the same `can_use_tool` host. It passes `permission_mode` again, because
  resuming "doesn't restore any other stored permission mode".
- **A modified input is applied on the continuation,** when the same call
  fires `PreToolUse` again and the hook returns `allow` with
  `updatedInput`. A deferral ignores `updatedInput`.
- **The permission host's other calls.** Every other `can_use_tool` call is
  answered by #149's static policy, which denies by default with a fixed
  message.

**D9.3 (#151).** **A continuation must match exactly.**

- The re-fired `PreToolUse` must carry the recorded tool name and
  `tool_use_id`, and an input whose canonical-JSON SHA-256 equals the
  recorded digest.
- Anything else is answered `deny`, and the outcome is `BLOCKED`, naming the
  request: the session is not the one that paused.
- `stop_reason: "tool_deferred_unavailable"` (hooks docs: the tool is gone
  "before the hook fires") is `BLOCKED` with that reason.
- AutoForge never resumes without a decision. It never re-defers on
  purpose, although the docs allow it ("the hook can return `"defer"`
  again").

**D9.4 (#151).** **A no-prompt resume through the streaming client must be
proved.**

- A continuation calls `connect()` with `resume` and never calls `query()`.
- The docs describe `claude -p --resume`. The SDK keeps stdin open in
  `--input-format stream-json` and passes no `-p`. Whether the CLI re-fires
  the deferred call before any new user message is not determinable from
  source. That is gate G-C7.
- Until G-C6 and G-C7 pass, Claude's pause capability is false. Under
  `prompt`, no-TTY, end of file and an expired wait then mean `Fail`
  (D7.4).

**OpenCode.**

**D9.5 (#152).** **Caller ids and durable admission.**

- The session is created with a caller-supplied id. Once #126 has landed,
  the id is persisted before the call, as for Claude (D9.1).
  `POST /api/session` takes `{id?, agent?, model?, location?}` (VT
  :9703-9712), and no classic route takes an id.
- Each input is admitted with a caller-supplied input id through `POST
  /api/session/{id}/prompt`: "Durably admit one session input" (VS :1688;
  body `{id?, prompt, delivery?, resume?}`, VT :9858).
- Whether re-admitting the same input id after a crash is refused or
  de-duplicated is gate G-O2. The history persists in the data directory
  (D2.7); gate G-O8 proves it.

**D9.6 (#152 for the bridge, #126 for recovery).** **Pending asks do not
outlive the server, so recovery is an explicit continuation message.**

- **Source.** Pending permissions and questions live in an in-memory `Map`
  of `Deferred`s, and a finalizer fails each at shutdown:
  - `permission/index.ts` :23-24, :50, :56-59 (`RejectedError`);
  - `question/index.ts` :42-43, :71-79 (`RejectedError`);
  - the same in `packages/core`: `permission.ts` :117-129 (`DeclinedError`)
    and `question.ts` :79-91 (`RejectedError`).

  Classic "always" approvals are in memory too (`approved: []`). In the
  `/api` family an ask may carry a `save` list, and replying "always" to
  such an ask writes the rule to the project's SQL store
  (`core/permission.ts` :250-256, `permission/saved.ts`).
- **Pause.** The bridge has already sent the ask (its id; the permission
  and patterns with the tool's message and call ids, or the questions; and
  its digest). The adapter closes the bridge's stdin, and the bridge stops
  the server (D3.3). An orderly stop runs the finalizer, which fails the
  ask, so the tool does not run, unless a tool holding the server password
  approved the ask before the stop. That is why a permission ask's pause
  also needs G-O11 (D3.4). What the history then shows for the call, and
  what a server killed by SIGKILL leaves, is part of gate G-O6.
- **Continuation.**
  - A new server starts on the same data directory, with the same session
    id.
  - The bridge admits exactly one **continuation message**, with a caller
    id. #126 renders it from a template under `src/autoforge/prompts/`,
    never from free text.
  - The message names the earlier request and what was decided: the answers
    to a question, or for a permission an approval or a denial with the
    operator's guidance. It also says that an approved action must be
    issued again.
- **Reissue.** When the session asks again, the bridge replies `once` only
  to an ask whose permission and patterns equal the recorded ones exactly,
  and only once. Any other ask goes to the broker as a new request.
- **What is never sent:**
  - an "always" to an `/api` ask that carries a `save` list, because the
    rule would outlive the logical invocation in the provider's store;
  - a modified input, which no reply carries (VT :7906, :10891).
- **Rejections.** A reject with `message` reaches the model as "The user
  rejected permission … with the following feedback: …"
  (`permission/index.ts` :121-127; `core/v1/permission.ts` :13-18). It
  also rejects the session's other pending asks (:129-138). The bridge
  reports those as rejected and never answers them afterwards.
- **`cancel` and `Fail`** are a `reject` for a permission, and a question
  reject for a question (VT :7797-7864).

**D9.7 (#152).** **No provider metadata is assumed.**

- v1 `PATCH /session/{id}` accepts only `{title?}` (GT :1916-1927).
- The v2 module's classic `Session` has `metadata`, and its `PATCH`
  accepts it (VT :99-101, :8205).
- `SessionV2Info` has no metadata, and no `PATCH` exists under
  `/api/session/{id}` (VT :3184-3209).
- AutoForge keeps every binding in its own state, never in a provider's
  session metadata.

**Both providers.**

**D9.8 (#126).** **Crash and receipt ambiguity.**

- Before delivering a decision, #126 records `delivering`. It records
  `delivered` once the provider has acknowledged it: Claude's hook answer
  was followed by the call's tool result; OpenCode's reply or admission was
  answered successfully.
- On recovery, a `delivering` with no `delivered` is read from the
  provider's history where the provider can show it: the tool part's
  state in OpenCode (gate G-O7). Where it cannot, the state is `BLOCKED`,
  naming the request.
- A decision is never re-sent for an action whose execution is unknown.

**D9.9 (#126).** **Recovering a final result.**

- **OpenCode.** If a session produced its final answer before a crash, a
  recovery launch reads the last assistant message from the durable
  history (bounded) and admits no new input (gate G-O8).
- **Claude.** There is no SDK read of a session's result without resuming
  it, so recovery is a fresh logical invocation and the old session is
  abandoned, as for every provider today.
- Either way, the text goes through the parser and the phase's
  verification.

**D9.10 (documentation only).** **Never exactly-once.**

- AutoForge never claims that a tool ran exactly once. A continuation, a
  reissue or a fresh relaunch may repeat a tool's effect.
- What a phase did is verified from GitHub and the worktree, as today (ADR
  0004 and the per-phase checks), never from the provider's account.

### 2.10 Auth, resources and logs (item 10)

**D10.1 (#149).** **Policy is #149's.** It covers:

- tool and permission policy, including which calls are
  interaction-eligible (D9.2);
- setting sources (`setting_sources`, which becomes `--setting-sources=`,
  SC :729-730);
- MCP servers and plugins: none by default. The helper's only hooks are its
  own callbacks.
- configuration and session roots (D2.7);
- the environment names each SDK provider adds.

The sandbox enforces what #149 selects (ADR 0005 D8.3). #149 may map ADR
0004 D1.2 into deny rules as defence in depth.

**D10.2 (#149).** **The Claude SDK authenticates with an explicit API
method only, never a reused subscription login.**

- The docs: "Unless previously approved, Anthropic does not allow third
  party developers to offer claude.ai login or rate limits for their
  products, including agents built on the Claude Agent SDK. Use the API key
  authentication methods described in this document instead" (overview,
  quickstart). The quickstart's methods are `ANTHROPIC_API_KEY`, Amazon
  Bedrock, Claude Platform on AWS, Google Vertex AI and Microsoft Foundry.
- The `claude-sdk` provider's `environment_names` are those methods' names
  (#149 lists them).
- Its sandbox declaration has no `~/.claude/.credentials.json` item and no
  `keyring` item.
- A launch whose environment carries none of those credentials is refused
  before the helper starts.
- The `claude` CLI provider's login is unchanged.
- OpenCode SDK credentials stay OpenCode's own: an authentication store in
  the isolated data directory, or environment names that #149 declares.

*Amendment (2026-10-20, #149).* #149 narrows the methods it lists.

- **`claude-sdk`.** The only credential is `ANTHROPIC_API_KEY`. The cloud
  routes (Bedrock, Claude Platform on AWS, Vertex AI, Foundry) are
  unsupported in this release. Each reads a credential chain of its own
  (profile files, application-default credentials, a cloud CLI's login)
  that would need its own sandbox projection and probe. Adding one is a
  later change with that evidence. `CLAUDE_CODE_OAUTH_TOKEN` and
  `ANTHROPIC_AUTH_TOKEN` are refused by name.
- **`opencode-sdk`.** The credential is one environment variable that the
  profile names (`options.credential_env`). An authentication store in
  the data directory is unsupported in this release: the directory starts
  empty, and no store is copied into it.

The rest of D10.2 stands. docs/sdk-policy.md §5 and §9 record the
decision and its evidence.

**D10.3 (#151, #152).** **Only the final accepted result text reaches the
parser.**

- `stdout` is:
  - for Claude, the `result` of the last turn's `ResultMessage`, as
    `claude_stream.py` takes it;
  - for OpenCode, the text parts of the final assistant message for the
    admitted input.
- It reaches `parse_control_result` unchanged.
- `structured_output` (TY :1340-1377; `output_format` becomes
  `--json-schema`, SC :782-789) is ancillary. It is at most a presence flag
  in `provider_summary` and is never parsed as a result.
- Raw records never reach the parser or `stdout.log`.

**D10.4 (#151, #152).** **Unknown observations are counted; an unknown
outcome or interaction fails closed.**

- An observational event of an unknown type is counted in
  `provider_summary` and otherwise ignored.
- An unknown outcome fails closed: an unknown result subtype, stop reason,
  terminal reason or session status.
- So does an unknown interaction: a kind or field the broker cannot
  render. The ask is denied or rejected, and the invocation is a provider
  failure.
- Fail-closed means the controller never approves, and the invocation
  fails. Whether the action then never runs depends on two things:
  - the provider honours the deny or reject (G-C4, G-O4);
  - no other client answered the ask first. For OpenCode that is G-O11.
    Until it passes, a reject keeps nothing from running, because a tool
    holding the server password may have approved the ask already (D3.4).

  Under every provider, what a tool must never do is kept from it by
  logical isolation and the outer sandbox, never by an interaction reply
  (ADR 0005 D1.1).
- Two Python SDK facts limit what the helper can see. The exact pin (D4.1)
  and the probes (§3) are the mitigation:
  - it skips an unknown top-level message type silently (MP :391-395);
  - it answers an unknown control-request subtype with an error itself (IQ
    :577-704).

**D10.5 (#134).** **The channels stay, and two bounded type extensions are
named here and nowhere else.**

- `provider_failure` and `provider_summary` are unchanged. Progress goes
  through `ProgressEvent`, whose kinds #154 extends, and loop detection is
  fed as for the other providers.
- The two extensions:
  - `AgentExecutionResult.interaction`: the lifecycle discriminant and, on
    a pause, the pending request. #134 defines the type, and #126 persists
    it.
  - `InteractionRequiredError`, an `ExecutionError` (D7.6).
- No other field is added.

## 3. Capability and recovery gate matrix

**How to read it.**

- **Source** is the published evidence of §1.2.
- **Probe** is the isolated runtime probe. None was run for this record.
- **Initial** is what the first release supports:
  - *candidate*: supportable once the gate passes;
  - *unsupported*: no evidence makes it possible.
- **Gate** is the probe its implementing child runs.

**What a probe is.** A scripted launch through the real adapter and helper:

- under `sandbox.mode: required`;
- with a dedicated configuration and data directory;
- against a local stand-in model endpoint, as #131 did for Pi (§1.1 of ADR
  0003), so no paid call and no operator credential is needed.

Results are recorded in this record as an amendment. A capability is true
in an adapter only after its gate passes on the pinned pair (D5.3).

**Claude** (#151; the pause rows are wired by #126):

| Capability | Source | Probe | Initial | Gate |
|---|---|---|---|---|
| session creation binding | `session_id` becomes `--session-id` (SC :651-655, TY :2043-2048); `session_id` on `ResultMessage` (TY :1340-1377) | not run | candidate | G-C1: `system/init` and the result report the caller's UUID; the transcript lies under #149's session root |
| initial prompt admission | `query()` writes one user message to stdin (CL :295-306); no acceptance record | not run | candidate | G-C2: the first `system/init` after the query is the admission; a refused prompt surfaces as an error result |
| live single question | `AskUserQuestion` answered by `PreToolUse` `allow` with "the answer in `updatedInput`": the original `questions` echoed back plus an `answers` object; "Returning `"allow"` alone is not sufficient" (hooks docs); `updatedInput` (TY :441-443) | not run | candidate | G-C3 |
| live multiple questions | one to four `questions`, each with an optional `multiSelect`; `answers` maps each question's text to the selected label, several labels joined by commas (hooks docs, Python docs) | not run | candidate | G-C3 |
| custom answer | the docs map an answer to an option label only; free text appears only as a per-question `annotations` note (Python docs); whether an answer outside `options` is accepted is not determinable | not run | candidate | G-C3, including a custom answer, or `offers` carries none |
| once approval | `PreToolUse` `allow` (TY :441) | not run | candidate | G-C4 |
| session approval | `PermissionResultAllow.updated_permissions` (IQ :617-630), only through `can_use_tool`, not the hook | not run | unsupported: interactions use the hook (D9.2) | an amendment, then G-C4 |
| modified input | `PreToolUse` `allow` with `updatedInput` (TY :441-443); ignored on `defer` (docs) | not run | candidate | G-C4 |
| deny guidance | `PreToolUse` `deny` with `permissionDecisionReason` (TY :441-442); `PermissionResultDeny.message` (IQ :631-634) | not run | candidate | G-C4 |
| graceful cancellation | `interrupt()` (CL :308); control subtype `interrupt` (IQ) | not run | candidate | G-C5: an interrupt ends the turn; `disconnect()` leaves nothing behind |
| deferred replay after restart | `defer`, `tool_deferred`, `deferred_tool_use`, `--resume` re-fires `PreToolUse` (hooks docs; TY :441, :1300-1311); single tool call only; `-p` only | not run | candidate | G-C6: `defer` honoured under the SDK's argv, and where `tool_deferred` appears; G-C7: no-prompt resume through `ClaudeSDKClient` |
| delivered-before-crash recovery | no SDK read of the transcript | not run | unsupported: `BLOCKED` (D9.8) | — |
| final-result recovery | none without resuming | not run | unsupported: a fresh logical invocation (D9.9) | — |
| subagent attribution | the CLI's records carry `parent_tool_use_id` (verified for `stream-json` on CLI 2.1.293, `claude_stream.py`); whether a hook call names the subagent is not determinable from the SDK source | not run | candidate | G-C8: a subagent's ask is attributed, or the invocation fails closed |
| cleanup | `disconnect()`: stdin, 5 s, terminate, 5 s, kill, direct child only (SC :962-1061); no new session (SC :873-881); `atexit` SIGTERM (SC :58-68) | not run | candidate | G-C9: after a normal end, a timeout, a stop, Ctrl-C and a helper crash the group is empty |
| hook wait | `HookMatcher.timeout`, default 60 s, enforced by the CLI (TY :618-619, IQ :335-336) | not run | candidate | G-C10: a hook waiting `max_wait_seconds` is not timed out when its timeout is that plus a margin |

**OpenCode** (#152; the recovery rows are wired by #126):

| Capability | Source | Probe | Initial | Gate |
|---|---|---|---|---|
| session creation binding | `POST /api/session {id?}` (VT :9703-9712); no classic route takes an id | not run | candidate | G-O1 |
| initial prompt admission | `POST /api/session/{id}/prompt` "Durably admit…", `SessionInputAdmitted` (VS :1688, VT :9858); classic `prompt_async` answers 204 "Prompt accepted" (GT :2366-2371) | not run | candidate | G-O2: a re-admitted input id is refused or de-duplicated |
| live single question | `question.asked` with `QuestionInfo {question, header, options, multiple?, custom?}` (VT :532-547, :5968); `POST /question/{id}/reply {answers: string[][]}` and `/reject` (VT :7797-7864); on by default while `OPENCODE_CLIENT` is `cli` (`tool/registry.ts` :207, `runtime-flags.ts` :41, :56) | not run | candidate | G-O3 |
| live multiple questions | `questions[]`, `multiple?` | not run | candidate | G-O3 |
| custom answer | `custom?` | not run | candidate | G-O3 |
| once approval | `reply: "once"` (VT :7906) | not run | candidate, as a control only once G-O11 passes (D3.4) | G-O4 and G-O11 |
| session approval | classic `reply: "always"`, kept in memory (`approved: []`); an `/api` "always" to an ask with a `save` list persists to SQL (`core/permission.ts` :250-256) | not run | candidate, in-memory `always` only; never to an ask with a `save` list; as a control only once G-O11 passes (D3.4) | G-O4: an `always` dies with the server; and G-O11 |
| modified input | no reply carries one (VT :7906, :10891) | — | unsupported | — |
| deny guidance | `reject` with `message` becomes `CorrectedError({feedback})` (`permission/index.ts` :121-127); it rejects the session's other pending asks (:129-138) | not run | candidate, as a control only once G-O11 passes (D3.4) | G-O4, including how the sibling rejections surface; and G-O11 |
| graceful cancellation | `POST /session/{id}/abort`; `/api/.../interrupt` "Idle interruption is a no-op" (VS :1743) | not run | candidate | G-O5 |
| deferred or request replay after restart | pending asks are in-memory `Deferred`s, rejected at shutdown (D9.6) | not run | unsupported as replay; recovery by continuation message (D9.6) | G-O6: a new server on the same data directory continues the same session from its history, and an exact reissue is answered once; and what the history shows for the failed ask after an orderly stop and after SIGKILL; for a permission ask, also G-O11 (D3.4, D9.6) |
| delivered-before-crash recovery | history `GET /session/{id}/message` (VT :8327) with each tool part's state | not run | candidate | G-O7: after a crash, a part's state tells ran, not run or unknown |
| final-result recovery | durable history | not run | candidate | G-O8 |
| subagent attribution | child sessions carry `parentID` (GT `Session` :465-469; VT `Session` :64-71, `SessionV2Info` :3186); asks carry `sessionID` | not run | candidate | G-O9: a child session's ask is attributed to the root logical invocation, or the invocation fails closed |
| cleanup | the bridge owns the server (D3.1); the group kill; whether the runtime stops its language servers, MCP servers and shells on SIGTERM is not determinable from source | not run | candidate | G-O10: after a normal end, a timeout, a stop, Ctrl-C and a bridge crash the group is empty |
| server authentication and no self-answer | Basic authentication from `OPENCODE_SERVER_PASSWORD` (`server/auth.ts` :17-34); whether tools see the variable is not determinable | not run | candidate | G-O11: a probe tool cannot obtain the password from its environment, from `/proc/<pid>/environ`, `cmdline` or `mem` of any other process of the invocation, or from a file it can read. Until it passes, OpenCode permission asks are advisory, and every permission row is false (D3.4) |
| assigned port | `--port=0` binds 4096 when free, otherwise any free port (`server/server.ts` :117-122); the readiness line reports the bound port (`serve.ts` :20) | not run | candidate | G-O12: the readiness line names the bound port, also when 4096 is taken |
| runtime version read-back | the health routes (VS) | not run | candidate | G-O13 |

**Rules.**

- A row whose gates have not all passed is false in `Capabilities`,
  whatever its source column says.
- A pause capability needs every gate of its provider's "… replay after
  restart" row, plus #126.
- G-O11 is a prerequisite of every OpenCode permission row: once approval,
  session approval, deny guidance, and a permission ask's pause. A pass of
  G-O4 or G-O6 alone never makes one true. If G-O11 fails, the amendment
  turns those rows *unsupported* as controls: an OpenCode permission ask
  stays advisory and fails the invocation (D3.4). The question rows do not
  need G-O11.
- A gate that fails turns its row into *unsupported* by amendment. The
  implementing child comments on #148 and on the affected issue (D4.6).

## 4. Ownership and dependency plan

```text
                         #148  this record
            ┌──────────────┼──────────────────┐
            v              v                  v
  #134 part 1          #149 SDK policy       (no other prerequisite)
  contract, broker,    and configuration
  prompt, fail
     │   │                 │
     │   └───────┬─────────┤
     │           v         v
     │      #151 Claude    #152 OpenCode bridge,
     │      helper,        server and adapter
     │      adapter        (the client #126 uses)
     │           ┆         ┆
     v           v         v
  #126  one state bump, pending record, pause and exit 3,
        operator commands, crash and receipt recovery,
        OpenCode continuation
     │
     v
  #134 part 2 (post-MVP): Pi adoption
```

An arrow points from a prerequisite to the issue that depends on it. A
dotted line is not a dependency. The same plan as edges:

- #134 part 1 and #149 depend on this record only.
- #151 and #152 depend on #134 part 1 and #149.
- #126 depends on #134 part 1. It wires `pause` for each provider whose
  transport child (#151 or #152) has landed and whose gates passed, so its
  merge waits for neither. For OpenCode it consumes #152's bridge.
- Whichever of #126 and a transport child lands second wires that
  provider's `pause`, continuation and recovery. The decisions stay
  #126's.
- #134 part 2 depends on #126.
- Nothing depends on #134 part 2. The graph has no cycle.

There is one session architecture: #126's record over #134's contract. The
transport is the existing duplex handle, with no new executor transport.

## 5. Bridge envelopes

Both envelopes use the duplex handle's framing: one JSON object per
LF-terminated line, UTF-8, at most `max_record_bytes`. Every record has a
`type`. A record that replies to another carries that record's `request_id`.
The adapter's reducer is pure, like `pi_rpc.py`: it fails closed on an
unknown `type` from the helper, a missing field, or a `request_id` it never
issued. End of file on the helper's stdin is the shutdown request.

**`autoforge-claude-helper`, protocol 1 (#151).**

| Direction | `type` | Fields | When |
|---|---|---|---|
| helper → controller | `hello` | `envelope`, `protocol`, `sdk_version`, `cli_version`, `python_version` | first, before `start` is read (D4.1, D4.5) |
| controller → helper | `start` | `session_id`; `mode` (`new` or `continue`); `prompt` (only for `new`); `cwd`; `model`; `effort`; `permission_mode`; `policy` (#149's flat fields); `hook_timeout_seconds`; `interactions` (`live`, `defer`); `continuation` (for `continue`: the recorded `tool_use_id`, tool name, input digest, and the decision with its `updated_input` or guidance) | once, after `hello` checks out |
| helper → controller | `message` | `data`: one SDK message as the CLI record it was parsed from, carrying the fields `claude_stream.py`'s turn, result, progress and loop rules read. `SystemMessage` keeps its raw `data`; #151 re-serializes the other types | every message |
| helper → controller | `ask` | `request_id`, `kind`, `tool_name`, `tool_use_id`, `input` (bounded), `input_sha256`, `agent` | from `PreToolUse` for an interaction-eligible call |
| controller → helper | `decision` | `request_id`, `directive` (`allow`, `deny`, `pause`, `fail`), `updated_input`, `guidance` | the reply to an `ask` |
| controller → helper | `interrupt` | — | a cancellation |
| helper → controller | `end` | `skipped_unknown` (D10.4), `outcome` (`result` or `interrupted`) | last, before exit 0 |
| helper → controller | `failure` | `reason` (bounded; never a secret) | a fatal helper error, before a non-zero exit |

**`autoforge-opencode-bridge`, protocol 1 (#152).**

| Direction | `type` | Fields | When |
|---|---|---|---|
| bridge → controller | `hello` | `envelope`, `protocol`, `sdk_version`, `node_version` | first |
| controller → bridge | `start` | `server_command` (absolute path); `session_id`; `mode` (`new`, `continue` or `recover`); `input_id`; `prompt` (for `new`) or `continuation` (the rendered message, for `continue`); `reissue` (for `continue`: the permission, patterns and reply to give once); `cwd`; `agent`; `model` | once |
| bridge → controller | `server` | `runtime_version` | after readiness, before any session call (D4.2) |
| bridge → controller | `event` | `data`: a filtered observation (session status, tool part states by name and digest) | as they arrive |
| bridge → controller | `ask` | `request_id`, `kind`, `provider_request_id`, `session_id` (a child session's own), `permission` and `patterns` with `tool` (`message_id`, `call_id`), or `questions` | on `permission.asked` or `question.asked`, or their `/api` counterparts (D4.3) |
| controller → bridge | `decision` | `request_id`, `directive` (`once`, `always`, `reject`, `answer`, `pause`, `fail`), `message`, `answers` | the reply to an `ask` |
| controller → bridge | `interrupt` | — | a cancellation |
| bridge → controller | `result` | `text`, `message_id`, `finish` | the final assistant message of the admitted input |
| bridge → controller | `end`, `failure` | as for Claude | |

**No common envelope.** The two share seven record names: `hello`,
`start`, `ask`, `decision`, `interrupt`, `end` and `failure`. That is the
only duplication. Every payload differs:

- Claude defers inside a hook, and identifies a call by `tool_use_id` and
  an input digest;
- OpenCode identifies an ask by permission and patterns, owns a server
  lifecycle, and recovers by message.

A common envelope would be a generic transport (D5.4) shaped by two
providers. It is reconsidered only when a third helper repeats the same
payloads.

## 6. Composition with other decisions

- **ADR 0002.** Unchanged, with no exemption. A helper and its runtime are
  one invocation (D3.2). §4b's single contained invocation is why the
  server is the bridge's child. §4c's rule for waits holds (D7.3). §4e's
  teardown covers both helpers.
- **ADR 0003.** §2.9 is amended (D6.1); its Pi, executor and
  failure-channel rows stand.
  - §2.4 is unchanged: a correction is a fresh process.
  - §2.1's rejection of Pi's TypeScript SDK stands. Pi offers an RPC that
    needs no sidecar. OpenCode has no Python SDK, and its documented
    interaction surface is the server API, so a Node bridge is the only
    supported client. The bridge is held to every rule a CLI child is held
    to.
- **ADR 0004.**
  - D1.3 holds: an answer or approval changes what a tool may attempt,
    never which credential the process holds.
  - Interaction records are not effect records (its §4). An answer reaches
    a payload only through a validated `CONTROL_RESULT`.
  - The read context is unchanged.
- **ADR 0005.**
  - D3.1 and D10.7 are realized by D2.1 and D3.2.
  - D7 and D8.3 cover #149's roots (D2.7).
  - The staged helper's directory is a read-only projection, like #165's
    overrides directory (D2.2).
  - The helper environment's interpreter, Node and the OpenCode runtime are
    declared executables (D2.3, D2.5).
  - The server's port is reachable in the shared network namespace, which
    is why D3.4 exists.
- **ADR 0006.** An interaction answer is never validation evidence. Only
  protocol numbering is shared (D6.2).

## 7. Decision ownership

| Child | Decisions |
|---|---|
| #134 | D5.1, D5.2, D7.1, D7.2, D7.3, D7.4, D7.6, D7.7, D10.5 |
| #126 | D6.2, D7.5, D7.8, D8.1–D8.5, D9.8, D9.9 |
| #149 | D2.7, D10.1, D10.2 |
| #151 | D1.1, D2.3, D2.4, D4.1, D9.1–D9.4; with #152, each for its own helper: D2.1, D2.2, D2.6, D3.2, D3.3, D4.5, D5.3, D10.3, D10.4 |
| #152 | D2.5, D3.1, D3.4, D3.5, D4.2, D4.3, D4.4, D9.5, D9.7; and the shared ones listed for #151 |
| #152 (the bridge) and #126 (recovery) | D9.6 |
| the owner named on each row of its table | D6.3 |
| documentation only | D1.2, D3.6, D4.6, D5.4, D6.1, D9.10 |


## 8. Issue maintenance

#148 asks that the original issues be maintained by comments and
cross-references, with no replacement issue. Once this record is accepted,
these two comments are posted. Their text is kept here so that the record
and the comments agree. Neither issue is closed or superseded.

**On #126:**

> Coordination from ADR 0007 (#148). This issue keeps its number and scope,
> narrowed as follows.
>
> 1. The provider-neutral contract (request, decision and session identity,
>    the resolver, the policy, capabilities and the lifecycle discriminant)
>    and its broker are #134 part 1. This issue consumes them.
> 2. This issue owns:
>    - the one state bump, from the protocol current at merge (9 today),
>      settled on #23 first; label 6 stays skipped;
>    - the pending-interaction record and its session binding (ADR 0007
>      D8.4, D8.5);
>    - `pause` with exit status 3 (D7.5);
>    - the operator commands;
>    - crash and receipt recovery (D9.8, D9.9);
>    - OpenCode recovery by an explicit continuation message (D9.6).
> 3. The OpenCode transport is #152's bridge over `@opencode-ai/sdk`, with
>    a server the bridge starts inside its own invocation (D3.1–D3.5). It
>    replaces a server child and HTTP client of this issue's own.
> 4. Pending OpenCode asks do not survive a server exit (ADR 0007 D9.6). No
>    server or daemon is kept between a pause and its continuation.
> 5. Dependencies: this issue depends on #134 part 1. #134 part 2, Pi's
>    adoption, which stays post-MVP, depends on this issue. Nothing else
>    remains of the old edge between the two.

**On #134:**

> Coordination from ADR 0007 (#148). This issue keeps its number, split into
> two parts.
>
> - **Part 1 (MVP):** the provider-neutral interaction contract and broker,
>   `src/autoforge/interaction.py` (ADR 0007 D5.1, D5.2, D5.4). It includes:
>   - the `prompt` and `fail` policies, with the per-wait limit (120 s) and
>     the aggregate limit (8) inside the executor's limits, and the lock
>     rule (D7.1–D7.4, D7.6, D7.7);
>   - the terminal resolver;
>   - `AgentExecutionResult.interaction` and `InteractionRequiredError`
>     (D10.5).
>
>   Its requirements come from Claude (#151) and OpenCode (#152), not from
>   Pi's optional dialogs. It has no generic transport, puts no SDK type in
>   the engine and persists nothing.
> - **Part 2 (post-MVP):** Pi's adoption. It depends on #126, which owns
>   the single state bump.

When a probe changes a decision, D4.6 applies: the implementing child
amends this record and comments on #148 and on the affected issues.

## 9. Non-goals

- No adapter, helper, bridge, configuration key, state field, prompt or
  test in this change. It is documentation only.
- No default switch. The `claude` and `opencode` CLI providers stay as they
  are, and the SDK providers are opt-in.
- No provider pool, cloud session store, auth automation or transport
  framework.
- No SDK installation as part of this planning task.
- No externally owned OpenCode server (D3.6).
- No change to Pi.

## 10. Consequences

- **Two optional providers, under every existing guarantee.** Each is a
  helper process under the allow-list, containment, the sandbox and the
  limits. The controller's install and dependencies are unchanged.
- **What an operator installs to use them.** Either a Claude helper
  environment from the committed lock (its platform wheel carries a 241 MB
  CLI), or Node 24 or later with the pinned OpenCode runtime. `doctor` checks
  both without installing anything.
- **Interactions become possible, bounded and explicit.** The default,
  `fail`, behaves like today: nothing can ask.
- **A pause turns a wait into a durable stop.** The run exits 3, releases
  the lock, and leaves no live process. A routine wait is never `BLOCKED`.
- **LOCAL bounds continuations.** A LOCAL write phase can be continued at
  most twice, within the three-launch bound.
- **Pins are exact.** Moving to a newer SDK, CLI or runtime is an amendment
  backed by probes, never a silent upgrade.
- **OpenCode interactions have limits.** They carry no modified input. A
  pending ask dies with its server, so continuing is a message, not a
  replay. A tool may be able to read the server password and answer its
  own asks, so OpenCode permission asks are advisory and unavailable as
  controls until G-O11 proves no tool can obtain it. Until then, an OpenCode
  permission ask fails the invocation under every policy, and only logical
  isolation and the outer sandbox keep an action from running (D3.4).
  Questions are unaffected.
- **Claude's pause rests on two facts not yet observed.** `defer` must be
  honoured under the SDK's argv (G-C6), and a resume must work without a
  prompt (G-C7). Until both are proved, a Claude wait under `prompt` that
  cannot be answered live (no terminal, end of file, an expired wait)
  fails instead of pausing, and `pause` is refused for Claude profiles.
- **Each provider ships with explicit gaps.** Session approval is
  unsupported on Claude in the first release. Delivered-before-crash and
  final-result recovery are unsupported on Claude.
