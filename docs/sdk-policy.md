# SDK providers: unattended resource, tool, permission, root and authentication policy

- **Status:** the policy is implemented by #149 (EPIC #158). The providers
  it governs, `claude-sdk` and `opencode-sdk`, are configurable and
  validated. `opencode-sdk` launches through #152's bridge, under this
  policy, with two amendments that #152's probe of the pinned runtime
  required (§6 and §7, both dated 2026-11-11). `claude-sdk` cannot launch
  yet: its helper and transport belong to #151, so every launch of it is
  refused before anything starts, and `doctor` says so. The
  policy implements [ADR 0007](adr/0007-sdk-providers-isolated-helpers-and-interaction-contract.md)
  D2.7, D10.1 and D10.2.
- **Where:**
  - `src/autoforge/sdk_policy.py` holds the policy: tables, builders,
    roots, the environment, the authentication checks, the `system/init`
    check and the interaction-data bounds. It is pure: no process, file,
    network or SDK import, and no change to the controller's environment.
  - `src/autoforge/providers.py` holds the adapters `ClaudeSdkProvider`
    and `OpenCodeSdkProvider`: names, option keys, validation, the launch
    allow-list, the sandbox declaration, and (#150) the helper's argv, a
    dry run's plan notes and the commands of `doctor`'s version probes.
  - `src/autoforge/sdk_assets.py` (#150) reads the packaged helper
    manifest, `src/autoforge/sdk_helpers/manifest.json`: each helper
    program, its envelope and protocol, whether this release ships it,
    and the pinned pair (ADR 0007 D2.2, D4.1, D4.2, D4.4).
  - `src/autoforge/config.py` holds the provider names and the
    interaction-wait check in `agent_limits`.
  - `src/autoforge/doctor.py` holds the SDK rows: "can launch", the
    helper environment against the pinned pair, the credential by name,
    model access and interactions ([Configuration](configuration.md#what-doctor-checks)).
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
  - **verified on runtime 1.18.34** (#152): observed by running the pinned
    `opencode-ai` 1.18.34, installed from npm into a scratch directory,
    under the policy's environment and roots. The gate runs (ADR 0007 §3)
    went through the real adapter and bridge under `sandbox.mode:
    required`, against a loopback stand-in model, so no model was called
    and no credential was read.
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
`fail` (D7.1). Questions are unaffected: under `prompt` they are answered
live, gate G-O3 having passed on the pinned pair (ADR 0007 §3).

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
- **The launch environment** is the configured allow-list of an agent launch
  (`execution.env_allowlist`, `env_allowlist_extra` and
  `agent_env_allowlist_extra`), with:
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
| `opencode-sdk` | `<sandbox dir>/sdk/opencode-sdk/{home,config,data,cache,state}` as `HOME` and `XDG_*_HOME`, plus `empty-config` as `OPENCODE_CONFIG_DIR` | the server's database and session history, logs and caches. Verified on runtime 2.0.32: the server wrote to the data, cache and state roots, and nothing else in the probe's directory changed. Verified on runtime 1.18.34: the same, but for the project id it caches in the project's git directory (§7). |

The `<sandbox dir>` is the issue's controller-owned sandbox directory from
`sandbox_policy`, `<git common dir>/autoforge/sandbox/<worktree>`.

That gives three properties:

- **Never in a working tree.** A root lies under the git common dir, never
  inside the issue worktree, so it never pollutes the reviewed tree.
  Tested: no root lies inside the issue worktree.
- **Per issue.**
- **Persistent.** A continuation runs on the same directory as its paused
  launch (D8.4).

Further rules:

- **AutoForge writes nothing into a root** and copies no credential into
  it, with one exception (next item).
- **OpenCode's configuration directories are read-only and hold one file.**
  *Amendment (2026-11-11, #152).* Runtime 1.18.34 treats both
  configuration directories it reads as its own: the global one
  (`<config root>/opencode`) and `OPENCODE_CONFIG_DIR` (`empty-config`).
  Before it serves a directory, it writes an ignore file into each and
  fails the instance when it cannot (the server's `/event` answers 500). It
  then installs `@opencode-ai/plugin` 1.18.34 from the npm registry into
  each, in the background. Both behaviours were observed by running it, and
  `plugin: []` and `OPENCODE_DISABLE_DEFAULT_PLUGINS=1` change neither.
  Before every launch the adapter therefore does three things
  (`OpenCodeSdkProvider.prepare_config`):
  - it writes the runtime's own ignore file (`.gitignore`, the five names
    the runtime writes) into each directory that lacks it;
  - it refuses the launch when either directory holds anything else,
    naming what it found, and deletes nothing;
  - it makes both directories mode 0555.

  The configuration root is projected read-only under the sandbox, as the
  empty directory always was. The instance starts, the install fails (it
  is a download at launch, which ADR 0007 D2.6 forbids), and so does any
  write a launch's agent makes there: the runtime loads agents, commands
  and plugins from both directories, so a writable one would let one launch
  plant code for the next. Under `sandbox.mode: disabled` the mode is all
  that holds them, and the refusal is what notices a write.
- **Retention.** `cleanupPeriodDays` is 30 (`SESSION_RETENTION_DAYS`). A
  continuation whose transcript is gone ends `BLOCKED`, never a new
  session under the recorded id (D2.7, D9.3).
- **Creation and sandbox projection.** The engine creates a provider's
  roots before every launch, in every `sandbox.mode`, from the adapter's
  `state_layout` (`sandbox_policy.StateRoot`), through the controller's
  filesystem boundary under L1. It names their parent to the adapter
  (`AgentRequest.state_dir`). Under the sandbox each root is an engine
  carve-out (ADR 0005 C5). The compiler sets `HOME` and `XDG_*` to the
  private home, so an OpenCode root that the runtime finds through one of
  them is projected at that place inside:
  - the data root (writable) at `$HOME/.local/share`;
  - the state root (writable) at `$HOME/.local/state`;
  - the configuration root (read-only) at `$HOME/.config`.

  The empty directory is projected read-only at its host path, which
  `OPENCODE_CONFIG_DIR` names. The home root is the private home's own and
  the cache root the launch's cache, so neither is projected: nothing a
  continuation needs lives there. `claude-sdk`'s layout is #151's; its
  declaration already lists `CLAUDE_CONFIG_DIR` as a `dir-rw` item and
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
| plugins, default plugins | **disable** | `plugin: []`; `OPENCODE_DISABLE_DEFAULT_PLUGINS=1` | the first verified on runtime 2.0.32. The 2.0.32 binary does not contain the second name; the 1.18.34 binary does. On 1.18.34 no sentinel plugin ran, and the plugin package the runtime installs into its configuration directories is kept out by §6's read-only layout. |
| Claude Code compatibility (`CLAUDE.md`, `~/.claude/skills`) | **disable** | `OPENCODE_DISABLE_CLAUDE_CODE`, `..._PROMPT`, `..._SKILLS` | the first and last names are in the 2.0.32 binary; `..._PROMPT` is not; all three are in the 1.18.34 binary. Verified on runtime 1.18.34: no sentinel `CLAUDE.md` or skill reached the configuration read back. |
| language servers and formatters, their downloads | **disable** | `lsp: false`, `formatter: false`, `OPENCODE_DISABLE_LSP_DOWNLOAD=1` | the first two verified on runtime 2.0.32. The variable is not in the 2.0.32 binary; it is in the 1.18.34 binary, whose log says "all LSPs are disabled". |
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

- **In the 1.18.34 binary's strings** (#152): every name the policy sets but
  `OPENCODE_CONFIG_PROJECT_DISABLE`, the 2.x name.

*Amendment (2026-11-11, #152): the pinned line.* The probe in
[Tests](#tests) ran on runtime 1.18.34 under the policy and §6's layout.
The configuration it read back held only the inline document, no sentinel
of the project, its parent or the user's root, and no marker was left. A
control run without the project switches loaded the sentinels. Three more
behaviours of 1.18.34 were observed:

- **Configuration directories.** It writes into both configuration
  directories, and installs a package there when it can. §6 holds both
  read-only.
- **A project's `.opencode/`.** Under `OPENCODE_DISABLE_PROJECT_CONFIG=1`
  it is neither loaded nor written: no sentinel agent or command was
  listed, and a sentinel plugin did not run.
- **The project id.** In a repository with a commit, it caches the
  project id as `opencode` in the git common dir, also for a linked
  worktree such as the issue's, outside every root. The file is in git's
  own directory, so it is never part of a tree, a diff or a review. Under
  the sandbox the common dir is read-only, and the write fails without
  harm: the gates ran in a linked worktree so. Under `disabled` it lands in
  the operator's repository's common dir. It is not a configuration load;
  it is recorded here as the one write outside the roots.

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
- **Server password.** Each launch gets a fresh password of 32 random bytes
  from the operating system's source, base64url: 256 bits, over D3.4's 128.
  *Amendment (2026-11-11, #152).* The bridge makes it
  (`crypto.randomBytes(32)`), so it never exists in the controller's
  process, its request or the executor's environment. It is passed as
  `OPENCODE_SERVER_PASSWORD` to the server alone, never in an argv, a
  record, a log or state, and the bridge sends it as the `Authorization:
  Basic` header of every request. `server_password()`
  (`secrets.token_urlsafe(32)`) is what a controller-side probe that starts
  a server itself uses. The username is the runtime's default, `opencode`,
  and `OPENCODE_SERVER_USERNAME` is never set: the bridge removes it from
  the server's environment. A test checks that the password the server got
  appears nowhere the controller keeps (`tests/test_opencode_sdk_provider.py`).
- **Unauthenticated requests are refused.** Verified on runtime 2.0.32 and
  on runtime 1.18.34, which both answered 401.
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
  UTF-8), is refused. A modified input is sent back exactly as validated:
  the decision keeps a read-only snapshot of the whole input, nested
  values included, taken through its canonical JSON when it is built. That
  snapshot is what is validated and what an adapter sends
  (`Allow.input_to_send()`), so changing the caller's object afterwards
  changes nothing.
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
- **Held where it is built.** The contract types of
  `src/autoforge/interaction.py` (#134) apply these rules on construction.
  A question's text is refused past its bound too, so an operator never
  decides on part of a question. An identity (a request id, a subagent
  name, a session id) is exact text on one line. The run log keeps the
  kinds of each decision, never an answer's text, guidance or a modified
  input.

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

- **The Claude probes did not run the pinned CLI.** They ran Claude Code
  CLI 2.1.304 (pinned: 2.1.286); #151's gates re-run them on the pinned
  pair, and the `init` check fails closed on any difference. The OpenCode
  probes ran the pinned runtime 1.18.34 too (#152; §7).
- **Built-in plugins, skills and agents** are listed by the CLI whatever
  the setting sources. They cannot be invoked, but the `cc-plugin-agents-md`
  built-in's behaviour under the SDK is part of G-C gating.
- **Managed settings** apply and cannot be disabled (§4).
- **The OpenCode runtime still reaches the network for its own purposes.**
  1.18.34 refreshed its model catalog during the probe (its
  `catalog.updated` events). The policy does not set
  `OPENCODE_DISABLE_MODELS_FETCH`, which the 1.18.34 binary contains,
  because nobody has verified whether a real provider's model still
  resolves without the catalog. That is a follow-up with its own probe. The
  plugin install of §6 is kept out by the read-only layout, not by a switch.
- **The project id cache** of §7 is a write outside the roots, into git's
  own directory, under `disabled`.
- **Not wired yet for `claude-sdk`:** the engine creates and projects a
  provider's roots from its `state_layout` (#152). `claude-sdk`'s layout,
  its helper, and the auth and `init` checks are #151's, and every
  `claude-sdk` launch is refused until then.

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
- the roots lie outside the issue worktree;
- the interaction-data bounds;
- the new redaction names and their growth bound;
- the adapters' refusals;
- the `doctor` row;
- the interaction-wait check in `agent_limits`.

The OpenCode bridge (#152) is covered by:

- `tests/test_opencode_sdk.py`, its envelope as a pure reducer;
- `tests/test_opencode_sdk_provider.py`, the adapter and the real packaged
  bridge against a fake OpenCode server (`tests/opencode_fake.py`), with
  the startup, the binding, asks and decisions, the final answer, the
  failures and the teardown. It needs Node 24 or later and skips without
  it; `AUTOFORGE_REQUIRE_NODE_BRIDGE=1` makes it fail instead;
- `tests/test_engine_sandbox.py`, the engine's state roots and their
  projection.

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
probe starts `opencode serve` on loopback, under §6's layout, and reads its
configuration back. Neither reads a credential of the operator's.

ADR 0007 §3's OpenCode gates run through the real adapter and bridge, under
`sandbox.mode: required`, against a loopback stand-in model
(`tests/opencode_standin.py`). They need the pinned runtime, Node 24 or
later and a host that can enforce the sandbox:

```sh
AUTOFORGE_OPENCODE_SDK_GATE_RUNTIME=/path/to/node_modules/opencode-ai/bin/opencode.exe \
    uv run pytest -rs tests/test_opencode_sdk_gates.py
```
