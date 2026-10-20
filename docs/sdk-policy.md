# SDK providers: unattended resource, tool, permission, root and authentication policy

- **Status:** the policy is implemented by #149 (EPIC #158). The providers
  it governs, `claude-sdk` and `opencode-sdk`, are configurable and
  validated, but they **cannot launch yet**: their helpers and transports
  belong to #151 (Claude) and #152 (OpenCode). Until those land, every
  launch is refused before anything starts, and `doctor` says so. The
  policy implements [ADR 0007](adr/0007-sdk-providers-isolated-helpers-and-interaction-contract.md)
  D2.7, D10.1 and D10.2.
- **Where:**
  - `src/autoforge/sdk_policy.py` holds the policy: tables, builders,
    roots, the environment, the authentication checks, the `system/init`
    check and the interaction-data bounds. It is pure: no process, file,
    network or SDK import, and no change to the controller's environment.
  - `src/autoforge/providers.py` holds the adapters `ClaudeSdkProvider`
    and `OpenCodeSdkProvider`: names, option keys, validation, the launch
    allow-list and the sandbox declaration.
  - `src/autoforge/config.py` holds the provider names and the
    interaction-wait check in `agent_limits`.
  - `src/autoforge/doctor.py` holds the "can launch" row.
  - `src/autoforge/redaction.py` holds the new credential names.
- **Evidence base.** Each decision below has one of these statuses:
  - **verified on CLI 2.1.304**: observed by running the Claude Code CLI
    2.1.304 installed on the authoring machine. This is *not* the pinned
    2.1.286 that `claude-agent-sdk` 0.2.163 bundles (ADR 0007 D2.4). Each
    run used a temporary `HOME` and `CLAUDE_CONFIG_DIR`, a synthetic key,
    and a loopback stand-in for the Messages API, so no model was called
    and no credential was read.
  - **verified on runtime 2.0.32**: observed by running the OpenCode
    runtime v2.0.32 installed on the authoring machine. This is *not* the
    pinned 1.18.34 (ADR 0007 D4.2). Each run used `opencode serve` under
    the policy's environment and roots.
  - **source/docs only**: read in ADR 0007 §1.2's artifacts, never run.
  - **gate**: a fact of the pinned pair that only #151's or #152's gates
    can prove (ADR 0007 §3). Until then, the policy states the
    requirement, and the `system/init` check or the bridge enforces it.

  The two "verified" kinds are repeatable: see the opt-in probes in
  [Tests](#tests).

The SDK providers are agents like the CLI providers: untrusted, running as
the same UID, and unconfined unless `sandbox.mode: required` puts them in
ADR 0005's outer sandbox. This policy decides four things:

- what a launch may **load**: settings, hooks, plugins, MCP servers,
  skills, agents, instructions and memory;
- which **tools** it gets, and which calls may ask a person;
- where its **state** lives;
- how it **authenticates**, and which **variables** it inherits.

None of it confines what an agent does with the tools it has. That is the
sandbox's job (ADR 0005 D1.1; ADR 0007 D10.1).

## 1. No pass-through

**Decision.** No SDK option, CLI flag, settings document, hook, MCP server,
plugin or environment variable reaches a helper unless a field of the
policy names it. A profile cannot carry free text into one.

- **`extra_args`** is refused for both providers. The refusal names how
  many arguments were given, never what they were.
- **`options`** accepts only the keys below. Any other key is a
  `ConfigurationError` at config load and before every launch. This
  includes the SDK's own names (`permission_mode`, `setting_sources`,
  `settings`, `mcp_servers`, `plugins`, `extra_args`), which are fixed.

| Provider | Option | Values |
|---|---|---|
| both | `interaction_policy` | `fail` (default), `prompt`. `pause` is refused until #126 lands and the provider's pause gates pass (ADR 0007 D7.1). |
| both | `max_wait_seconds` | a whole number from 1 to 86400; default 120. Under `prompt`, it must be below the profile's idle timeout and its ceiling (D7.3). |
| both | `max_interactions` | a whole number from 1 to 64; default 8. |
| both | `tools` | a comma-separated subset of the provider's inventory (§3, §8), with no spaces and no duplicates. The default depends on the profile (§3). |
| `claude-sdk` | `ask_tools` | a subset of `tools` whose calls ask a person. Needs `prompt`. |
| `opencode-sdk` | `server_command` | the OpenCode runtime binary of the pinned release (D2.5). Required. |
| `opencode-sdk` | `credential_env` | the one variable that holds the model key (§9). Required. |

`opencode-sdk` has no `ask_tools`. A permission ask cannot bind until gate
G-O11 passes (D3.4), so the broker answers every OpenCode permission ask
`fail` (D7.1). Questions are unaffected.

The profile's `command` is required too. It names the helper's interpreter:
Python for `claude-sdk` (D2.3) and Node for `opencode-sdk` (D2.5). No
default could name the operator's separate runtime environment.

## 2. Claude: what a launch loads

The helper receives the policy as flat fields in its `start` record
(`ClaudeSdkPolicy.start_fields`, ADR 0007 §5). It passes each one to
`ClaudeAgentOptions` and sets nothing else.

| Resource | Decision | Mechanism | Status |
|---|---|---|---|
| user, project and local settings (`~/.claude/settings.json`, `.claude/settings*.json`), with their hooks, permission rules, `env` and `apiKeyHelper` | **disable** | `setting_sources=[]`, which becomes `--setting-sources=` (SC :729-730) | verified on CLI 2.1.304: no sentinel hook from any of the three ran. A control run without the flag ran them. |
| project `.mcp.json`, user and plugin MCP servers | **disable** | `strict_mcp_config=True`, `mcp_servers={}` | verified on CLI 2.1.304: no sentinel server started, and `init.mcp_servers` was empty. The control run started it. |
| installed and marketplace plugins | **disable** | `plugins=[]`, no settings source | verified on CLI 2.1.304: only built-in plugins were listed (next row) |
| built-in plugins, skills and agents | **inherit, but not usable** | cannot be unloaded. `Skill`, `Agent` and `Task` are in `disallowed_tools`, so none can be invoked. | verified on CLI 2.1.304: listed in `init` with `path: "builtin"` |
| project and user skills, agents and commands (`.claude/skills`, `.claude/agents`, `$CLAUDE_CONFIG_DIR/...`) | **disable** | no settings source | verified on CLI 2.1.304: no sentinel skill or agent was listed |
| `CLAUDE.md` and `AGENTS.md` auto-discovery: project, parent, nested, and user `$CLAUDE_CONFIG_DIR/CLAUDE.md` | **disable** (see §4 for repository instructions) | no settings source | verified on CLI 2.1.304: no sentinel string reached a model request. The control run sent the project, parent and user files. |
| auto memory | **disable** | `settings` = `{"autoMemoryEnabled": false, ...}` | verified on CLI 2.1.304: `init.memory_paths` is gone |
| hooks | **only the helper's own** | `hooks` = the helper's `PreToolUse` callback (D9.2). No hook from any settings file. | verified on CLI 2.1.304 (settings hooks). The callback itself is #151's gate. |
| managed (administrator) settings and managed `CLAUDE.md` | **inherit (unsupported to disable)** | no SDK option turns them off | source/docs only. They are the administrator's, outside any repository, and are recorded as a limit (§4). |
| startup scripts (`CLAUDE_ENV_FILE`, a shell's startup files) | **disable** | the helper's environment carries no `CLAUDE_ENV_FILE` (§5). The Bash tool's shell isolation is #151's, as for the CLI provider (#165). | gate (#151) |
| auto-update, telemetry, error reporting | **disable** | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | source/docs only |
| `extra_args`, a settings path, `add_dirs`, `agents`, `system_prompt.append` | **unsupported** | not fields of the policy; the helper sets none | — |

**The `system/init` check.** The pinned pair is fixed (D4.1), so a
difference is a provider failure, never something to adapt to.
`ClaudeSdkPolicy.init_problems` names each departure; it names fields, never
values. It is #151's to run on the first `init` record, failing the
invocation on any problem. It flags:

- a tool offered that the policy does not list, or a listed tool missing;
- an MCP server;
- a plugin that is not built in;
- a permission mode other than `default`;
- a key source other than `ANTHROPIC_API_KEY`;
- auto memory.

## 3. Claude: tools and permissions

| Profile | Default `tools` |
|---|---|
| `analyze_execute`, `fix`, `replan_reexecute` | `Read, Glob, Grep, Edit, Write, Bash` |
| every other profile (the reviewers, `update_epic`) | `Read, Glob, Grep, Bash` |

`tools` may also list `NotebookEdit`, `WebFetch` and `WebSearch`. The read
set is defence in depth, not a write barrier, because the shell stays (as
in Pi's policy, [pi-policy.md](pi-policy.md) §6).

- **The inventory is exact.** `tools` is the whole set the CLI offers.
  `allowed_tools` is that set minus `ask_tools`, so those calls run without
  a callback. `disallowed_tools` lists:
  - every inventory tool the profile did not choose;
  - `AskUserQuestion` unless the policy is `prompt`;
  - always: `Agent`, `Task` (attribution is gate G-C8), `Skill`,
    `SlashCommand` and `ExitPlanMode`.
- **Every call passes the `PreToolUse` hook** (`ClaudeSdkPolicy.disposition`):
  - `pass` for an allowed tool;
  - `ask` for `ask_tools`, and for `AskUserQuestion` under `prompt`;
  - `deny` for everything else, including every `mcp__*` name and any
    built-in tool the policy does not know.
- **The permission host (`can_use_tool`) denies** every call that reaches
  it with a fixed message (`CLAUDE_SDK_HOST_DENY`). Every call it could see
  is either pre-approved or asked through the hook, so a call that still
  reaches it is one the policy did not foresee. The host is required
  anyway: without one, the CLI does not offer `AskUserQuestion` (D9.2).
- **Permission mode `default`.** CLI 2.1.304 calls this mode `manual` in
  its help, accepts `default` too, and reports `default` in `init` for
  both. Never `bypassPermissions`, `acceptEdits`, `auto`, `dontAsk` or
  `plan`: under those, the callbacks this policy relies on would not run.
  The mode is checked against `init`, not trusted from the enum. Status:
  verified on CLI 2.1.304.
- **`AskUserQuestion` only under `prompt`.** The CLI offers it only with a
  permission host. Under `fail`, an ask could only fail the invocation.
  Status: verified on CLI 2.1.304 (not offered without a host). The
  hook-and-host pair is gate G-C3.
- **`TodoWrite` is not in the inventory.** CLI 2.1.304 does not offer it.
  Status: verified on CLI 2.1.304. On 2.1.286 it is unknown, and adding it
  back needs a gate run.
- **No approval outlives its call.** Approve-for-session is unsupported.
  The hook answers `allow` (with `updatedInput` for a modified input) or
  `deny`, and never returns `updated_permissions`. No rule is written to
  any settings file, and the provider's settings are unchanged after an
  approval (`start_fields()["session_approval"]` is `false`; ADR 0007 §3,
  "session approval").

## 4. Instructions: repository context stays, everything else is off

- **Repository instructions stay available** as task data. Every phase
  prompt tells the agent to read the repository's `AGENTS.md` and
  `CLAUDE.md` (`src/autoforge/prompts/`), and `Read` is in every
  inventory.
  - **Claude:** the policy injects nothing. `system_prompt` is the
    `claude_code` preset with nothing appended.
  - **OpenCode:** the policy sets `instructions: []`, so no file is
    *added*. The runtime's own project-root `AGENTS.md` lookup is project
    data, gated by #152 (source/docs only).
- **Parent-directory instructions are not loaded.** Above the repository,
  they are not project data and would not be visible in a PR.
  - Claude: verified on CLI 2.1.304, with a parent `CLAUDE.md`.
  - OpenCode: the global ones are outside the policy's `HOME` and XDG
    roots (§10). Source/docs only.
- **User instructions are not loaded.** For Claude this covers
  `$CLAUDE_CONFIG_DIR/CLAUDE.md` and auto memory (verified on CLI
  2.1.304). For OpenCode, `~/.config/opencode/AGENTS.md` lies outside the
  policy's roots.
- **Limit: managed instructions and settings still apply.** An
  administrator's managed policy (Claude Code's managed settings and
  managed `CLAUDE.md`) is read whatever the setting sources. The same
  holds for the CLI provider. Such a policy is the machine owner's, not a
  repository's, and no option turns it off. `doctor` does not read it.

## 5. Claude: authentication and environment

**Decision (D10.2).** `claude-sdk` authenticates with `ANTHROPIC_API_KEY`
and nothing else.

| Credential route | Decision | Why |
|---|---|---|
| `ANTHROPIC_API_KEY` | **load** | the documented API-key method |
| claude.ai login (`~/.claude/.credentials.json`, a keychain), `CLAUDE_CODE_OAUTH_TOKEN`, a `claude setup-token` token | **unsupported, refused** | D10.2: no reused subscription login. The sandbox declaration has no login file and no keyring item. |
| `ANTHROPIC_AUTH_TOKEN` (a gateway bearer token) | **unsupported, refused** | not an API key |
| Bedrock, Vertex AI, Foundry (`CLAUDE_CODE_USE_*` with the cloud's own credential chain) | **unsupported in this release** | Each route reads a credential chain of its own: profile files, application-default credentials or a CLI login. That chain would need its own projection and probe. Adding a route is a later change with its own evidence. |
| `ANTHROPIC_BASE_URL` | **inherit only when listed exactly** | where the API is (a gateway, or a stand-in for the gates). A `*` prefix never carries it. |

- **No fallback.** `claude_sdk_auth_problems` refuses a launch without the
  key, or one that would carry a refused route. Its messages name
  variables, never values. It is #151's to run before the helper starts.
- **The key's source is checked.** `init.apiKeySource` must be
  `ANTHROPIC_API_KEY`. Status: verified on CLI 2.1.304, which reports
  `"ANTHROPIC_API_KEY"` with a key and `"none"` without one.
- **The launch environment** is the configured allow-list, with:
  - every `*` prefix expanded against the *names* in the controller's
    environment, so the result holds exact names only;
  - every other provider's variables and every credential-shaped name
    removed (`sdk_policy.FOREIGN_PREFIXES`, `CREDENTIAL_SUFFIXES`);
  - `ANTHROPIC_API_KEY` added.

  Over that, the policy sets `CLAUDE_CONFIG_DIR` (§6) and
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. The controller's own
  environment is read for names only and never changed.
- **#165's refusals still apply.** No GitHub or git credential route can
  be allow-listed.

## 6. Configuration and session roots (D2.7)

| Provider | Root | Holds |
|---|---|---|
| `claude-sdk` | `<sandbox dir>/sdk/claude-sdk/config`, as `CLAUDE_CONFIG_DIR` | `.claude.json`, `backups/`, `sessions/`, and the transcripts `projects/<cwd>/<session id>.jsonl`. Verified on CLI 2.1.304. |
| `opencode-sdk` | `<sandbox dir>/sdk/opencode-sdk/{home,config,data,cache,state}` as `HOME` and `XDG_*_HOME`, plus `empty-config` as `OPENCODE_CONFIG_DIR` | the server's database and session history, logs and caches. Verified on runtime 2.0.32: the server wrote to the data, cache and state roots, and nothing else in the probe's directory changed. |

The `<sandbox dir>` is the issue's controller-owned sandbox directory from
`sandbox_policy`:

- REMOTE: `<git common dir>/autoforge/sandbox/<worktree>`;
- LOCAL: `<git dir>/autoforge/sandbox/local`.

That gives three properties:

- **Never in a working tree.** A root never pollutes the LOCAL reviewed
  tree, because a LOCAL fingerprint excludes the repository's own git
  directory by identity. Tested: writing every root leaves the fingerprint
  unchanged.
- **Per issue.**
- **Persistent.** A continuation runs on the same directory as its paused
  launch (D8.4).

Further rules:

- **AutoForge writes nothing into a root** and copies no credential into
  it.
- **Retention.** `cleanupPeriodDays` is 30 (`SESSION_RETENTION_DAYS`). A
  continuation whose transcript is gone ends `BLOCKED`, never a new
  session under the recorded id (D2.7, D9.3).
- **Sandbox projection.** Under the sandbox, each root is an engine
  carve-out projected on its own, writable (ADR 0005 C5). The OpenCode
  empty directory is projected read-only. Creating the roots and
  projecting them is #151's and #152's engine wiring. `claude-sdk`'s
  declaration already lists `CLAUDE_CONFIG_DIR` as a `dir-rw` item, and
  refuses one inside `~/.claude` (ADR 0005 D7.2).

## 7. OpenCode: what a launch loads

The bridge receives `OpenCodeSdkPolicy.start_fields`, and the server gets
the policy's environment (`OpenCodeSdkPolicy.environment`).

| Resource | Decision | Mechanism | Status |
|---|---|---|---|
| project and parent `opencode.json`, project `.opencode/` (agents, commands, plugins) | **disable** | `OPENCODE_DISABLE_PROJECT_CONFIG=1` and `OPENCODE_CONFIG_PROJECT_DISABLE=1` (both release lines' names) | verified on runtime 2.0.32: the reported sources were only the empty directory and the inline document. A control run without the switches loaded the sentinel project file. |
| global configuration (`~/.config/opencode`), its agents, plugins and `AGENTS.md` | **disable** | `HOME` and `XDG_*_HOME` are the policy's roots; `OPENCODE_CONFIG=""`; `OPENCODE_CONFIG_DIR` is an empty directory | verified on runtime 2.0.32 |
| the configuration document | **controller-owned** | `OPENCODE_CONFIG_CONTENT` = `config_document()`: `plugin: []`, `mcp: {}`, `instructions: []`, `lsp: false`, `formatter: false`, `autoupdate: false`, `share: "disabled"`, the ruleset, and the `autoforge` agent | verified on runtime 2.0.32: accepted, and the legacy `permission` object was translated to ordered v2 rules (`bash` became `shell`) |
| MCP servers | **disable** | `mcp: {}` and no project file | verified on runtime 2.0.32: the sentinel server never started |
| plugins, default plugins | **disable** | `plugin: []`; `OPENCODE_DISABLE_DEFAULT_PLUGINS=1` | the first verified on runtime 2.0.32. The 2.0.32 binary does not contain the second name; whether 1.18.34 reads it is a gate (#152). |
| Claude Code compatibility (`CLAUDE.md`, `~/.claude/skills`) | **disable** | `OPENCODE_DISABLE_CLAUDE_CODE`, `..._PROMPT`, `..._SKILLS` | the first and last names are in the 2.0.32 binary; `..._PROMPT` is not. Behaviour: gate (#152). |
| language servers and formatters, their downloads | **disable** | `lsp: false`, `formatter: false`, `OPENCODE_DISABLE_LSP_DOWNLOAD=1` | the first two verified on runtime 2.0.32. The variable is not in the 2.0.32 binary; whether 1.18.34 reads it is a gate (#152). |
| self-update | **disable** | `autoupdate: false`, `OPENCODE_DISABLE_AUTOUPDATE=1`; the bridge never calls the upgrade route (D2.6) | name present in the 2.0.32 binary |
| `OPENCODE_PERMISSION`, `OPENCODE_SERVER_USERNAME` | **never set** | the ruleset travels in the document only, and the username is the default | neither name is in the 2.0.32 binary |

**Which line reads which name.** Unknown variables are inert, so the
policy sets the names of both release lines and records what is known
about each:

- **In the 2.0.32 binary's strings:** `OPENCODE_CONFIG_DIR`,
  `OPENCODE_CONFIG_CONTENT`, `OPENCODE_DISABLE_PROJECT_CONFIG`,
  `OPENCODE_CONFIG_PROJECT_DISABLE`, `OPENCODE_DISABLE_AUTOUPDATE`,
  `OPENCODE_DISABLE_CLAUDE_CODE`, `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS` and
  `OPENCODE_SERVER_PASSWORD`.
- **Not in it:** `OPENCODE_DISABLE_DEFAULT_PLUGINS`,
  `OPENCODE_DISABLE_LSP_DOWNLOAD` and `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT`.
  These are names of the 1.x flag set and are set defensively. On 2.0.32
  the document's `plugin: []` and `lsp: false` do their work.

Whether 1.18.34 reads each name is not verified here. #152's pin check and
gates prove the pinned line, and the configuration read-back of its probe
(the one in [Tests](#tests)) must show only the empty directory and the
inline document.

## 8. OpenCode: tools and permissions

| Profile | Default `tools` |
|---|---|
| `analyze_execute`, `fix`, `replan_reexecute` | `read, glob, grep, list, edit, bash` |
| every other profile | `read, glob, grep, list, bash` |

`tools` may also list `todowrite`, `todoread`, `webfetch` and `websearch`.

- **The ruleset is complete and ordered** (`OpenCodeSdkPolicy.ruleset`):
  1. `*: deny`;
  2. each listed tool `allow`;
  3. `external_directory: allow`;
  4. `question`: `allow` under `prompt`, `deny` otherwise;
  5. fixed denials that overlap no allowed name: `task` (attribution is
     G-O9), `skill`, `codesearch`, `lsp`, and `doom_loop`. Nobody could
     answer the repeated-call ask, and the controller's loop detector
     covers that case.

  Because nothing overlaps, first-match and last-match evaluation agree.
- **`external_directory` is allowed** because the REMOTE read context lies
  outside the worktree. Paths are bounded by the outer sandbox, never by
  this ruleset (D3.4, D10.1).
- **A controller-owned agent.** Every session runs agent `autoforge`
  (`mode: primary`), which carries the same ruleset. A built-in agent is
  never patched: built-in agents carry rules of their own. Verified on
  runtime 2.0.32, where the built-in `build` agent's rules begin with
  `*: allow`.
- **Permission asks are advisory until G-O11** (D3.4). The broker answers
  every one `fail`, so nothing asks a person about a permission, and
  `ask_tools` does not exist for OpenCode.
- **Replies are `once` or `reject`, never `always`.** `always` would
  approve a pattern for the rest of the session. The server's rules stay
  the document's, so approve-for-session is unsupported
  (`start_fields()["session_approval"]` is `false`).

## 9. OpenCode: credentials and server authentication

- **Model credential.** The model key is the one variable named by
  `options.credential_env`. It must:
  - match `[A-Z][A-Z0-9_]*_API_KEY`, at most 64 characters;
  - not be a forbidden name (#165);
  - not start with `OPENCODE_`, except `OPENCODE_API_KEY`.

  That variable, and no other provider's, is added to the launch
  allow-list (§5's filter applies). A sign-in stored by `opencode auth
  login` is **unsupported** in this release: the data root starts empty,
  and no store is projected. `opencode_sdk_auth_problems` refuses a launch
  without the variable, and names it without its value.
- **Server password.** Each launch gets a fresh password from
  `secrets.token_urlsafe(32)`: 256 bits, over D3.4's 128. It is passed as
  `OPENCODE_SERVER_PASSWORD` to the server alone, never in an argv, a
  record, a log or state. The username is the runtime's default,
  `opencode`, and `OPENCODE_SERVER_USERNAME` is never set.
- **Unauthenticated requests are refused.** Verified on runtime 2.0.32,
  which answered 401.
- **The server's argv** is `<server_command> serve --hostname=127.0.0.1
  --port=0`: loopback, an assigned port, no mDNS and no CORS origin
  (D3.4).

## 10. Interaction data

A provider's ask carries data the broker shows a person and may send back.
Every piece is bounded **before** it is normalized, persisted or shown.
`InteractionDataError` is raised for data that is refused. It names the
field and the rule, never the value.

- **Refused, never clipped.** These values decide what a tool may do or
  what the agent is told:
  - a tool input (canonical JSON, at most 64 KiB, `input_digest`);
  - a question's option and an answer (`exact_text`: at most 200 and 2000
    characters);
  - a modified input (`exact_input`).

  Approving a clipped rendering would approve different work, so any of
  these is refused when it is past its bound, carries a control, format or
  separator character other than a newline or a tab (an escape sequence
  starts with one, and a carriage return, bare or in a CRLF pair, redraws
  the line it is shown on), or would be changed by redaction.
- **Bound by digest.** A decision is bound to the SHA-256 of the input's
  canonical JSON (sorted keys, no spaces, UTF-8, no NaN; D9.3). A value
  that has none, such as a lone surrogate (legal as a JSON escape, not in
  UTF-8), is refused. A modified input is sent back exactly as validated.
- **Shown apart from the identity.** An option keeps its raw label (the
  identity a reply carries) apart from what is shown (`question_options`).
  Two labels shown alike are refused, because a person could not tell them
  apart.
- **Shown narrowly.** A permission ask shows only allow-listed keys per tool
  (`DISPLAY_KEYS`): a command and its description, a path, a pattern, a
  query or a URL. It never shows a file's content, a diff, an edit's text
  or a web page. Every other key is counted, not shown. Every shown value
  goes through `progress.clean`, which strips escape sequences and control
  characters, redacts, then clips.

## 11. Redaction and logs

- **New credential names.** `ANTHROPIC_AUTH_TOKEN`,
  `CLAUDE_CODE_OAUTH_TOKEN` and `OPENCODE_SERVER_PASSWORD` join the
  named-assignment pattern. Each is longer than `HF_TOKEN`, so
  `MAX_GROWTH_FACTOR` (3) still holds; `tests/test_redaction.py` pins it.
- **Names only.** Every policy message (validation, the `init` check, the
  auth checks) names variables, fields and tools, never a value.
- **Nothing credential-shaped is persisted.** The policy digest
  (`digest()`, part of ADR 0007 D5.1's `SessionIdentity`) covers the flat
  fields and the fixed environment, never a root path or a credential.

## Residual risks and follow-ups

- **Neither runtime that was run is the pinned one.** The probes ran Claude
  Code CLI 2.1.304 (pinned: 2.1.286) and OpenCode runtime 2.0.32 (pinned:
  1.18.34). #151's and #152's gates re-run them on the pinned pairs, and
  the `init` check fails closed on any difference.
- **Built-in plugins, skills and agents** are listed by the CLI whatever
  the setting sources. They cannot be invoked, but the `cc-plugin-agents-md`
  built-in's behaviour under the SDK is part of G-C gating.
- **Managed settings** apply and cannot be disabled (§4).
- **Not wired yet:** the engine does not yet create the roots, project them
  in the sandbox, start a helper or run the auth and `init` checks. That
  is #151's and #152's work, and every launch is refused until then.

## Tests

`tests/test_sdk_policy.py` covers:

- the closed option sets and every refusal;
- the default inventories and dispositions;
- the start fields and digests;
- the `init` check;
- the authentication checks, including that no value appears in a message;
- the OpenCode ruleset, document and environment;
- the launch allow-list. A real subprocess, started with synthetic secrets
  for other providers in the controller's environment, receives none of
  them, and the controller's `os.environ` is unchanged;
- the roots: writing every root leaves a LOCAL fingerprint unchanged, and
  the REMOTE roots lie outside the issue worktree;
- the interaction-data bounds;
- the new redaction names and their growth bound;
- the adapters' refusals;
- the `doctor` row;
- the interaction-wait check in `agent_limits`.

Two pairs of opt-in probes run an installed runtime against sentinel
configuration: hooks, MCP servers, skills, agents, and `CLAUDE.md`,
`AGENTS.md` and `opencode.json` in the project, its parent and the user's
root. Each pair also runs a control without the policy, which must see the
sentinels load:

```sh
AUTOFORGE_CLAUDE_SDK_PROBE_CLI=$(command -v claude) uv run pytest tests/test_sdk_policy.py -k probe_installed_claude
AUTOFORGE_OPENCODE_SDK_PROBE_SERVER=$(command -v opencode) uv run pytest tests/test_sdk_policy.py -k probe_installed_opencode
```

The Claude probe answers from a loopback stand-in for the Messages API
with a synthetic key, so it calls no model and spends nothing. The OpenCode
probe starts `opencode serve` on loopback and reads its configuration
back. Neither reads a credential of the operator's.
