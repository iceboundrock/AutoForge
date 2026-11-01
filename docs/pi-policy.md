# Pi: unattended resource and tool-loading policy

- **Status:** implemented by #132 (EPIC #127). Builds on
  [ADR 0003](adr/0003-pi-agent-provider.md) (the RPC adapter) and amends
  [ADR 0002](adr/0002-executor-nothing-outlives-the-invocation.md) (§4b).
- **Where:** `src/autoforge/providers.py` (`PiProvider`: the argv,
  `tools` / `context_files` options, environment names, API-key guard,
  per-launch auth preflight), `src/autoforge/doctor.py` (the outside-instruction
  warning, and #135's containment row), `src/autoforge/redaction.py` (OAuth
  fields), `src/autoforge/executor.py` and `executor_duplex.py` (orphan
  containment).
- **Operator guide:** [Pi: running phases on DeepSeek or a ChatGPT subscription](pi.md)
  (#135) is the how-to; this page is the policy and its evidence.
- **Evidence base.** Each item below names its evidence and how it was
  checked. "Verified on installed Pi 1.0.1" means it was observed by running
  `@earendil-works/pi-coding-agent` 1.0.1 from npm, with a temporary
  `HOME` and `PI_CODING_AGENT_DIR`, never the operator's own. "Docs/source
  only" means it was read in that package's `docs/` or `dist/` and not run.
  Source paths are relative to the package's `dist/`.

Pi has **no approval, permission or sandbox layer**. Its security guide says
it "does not ask for approval before every tool call", and that watching the
transcript or project trust "do not create a security boundary". AutoForge
therefore treats a Pi profile like every other agent: untrusted, same UID,
and unconfined unless `sandbox.mode: required` puts it in ADR 0005's outer
sandbox (§7). The policy below decides what Pi may *load*, which tools it
gets, and what it may see. It does not confine what an agent does with the
tools it has.

## 1. Project trust: always `--no-approve`

**Decision.** Every Pi argv carries `--no-approve`. `--approve` never
appears. A Pi profile cannot have `extra_args` (#129), and no option removes
the flag.

**Why.** Pi gates a project's `.pi/settings.json`, `.pi/mcp.json`,
`.pi/extensions`, `.pi/skills`, `.pi/prompts`, `.pi/themes`, `.pi/SYSTEM.md`,
`.pi/APPEND_SYSTEM.md` and `.agents/skills` behind project trust. RPC mode
never prompts, but a trust decision the operator *saved* for a path
(`$PI_CODING_AGENT_DIR/trust.json`, nearest ancestor wins) still applies.
`--no-approve` is the only switch that overrides a saved decision.

**Evidence.** `core/project-trust.js`, `core/trust-manager.js`
(`ProjectTrustStore`, `findNearestTrustEntry`),
`core/resource-loader.js:939-960`. Verified on installed Pi 1.0.1. With a
saved trust entry and every resource flag except `--no-approve`, the
project's `.pi/SYSTEM.md` and `.pi/APPEND_SYSTEM.md` were loaded and its
`packages` entry ran `npm install`. With `--no-approve`, none of that
happened. The opt-in test below runs against a saved trust entry.

## 2. Executable resources: always off

**Decision.** Every Pi argv carries `--no-extensions --no-skills
--no-prompt-templates --no-themes`. There is no per-profile opt-in. If one
is ever added, it must be an explicit, documented controller option and
never something the project can switch on.

**Why.** `--no-extensions` disables project and global extension
discovery, extensions from packages, and Pi's built-in extensions:
`mcp` (native MCP, so no MCP server process starts, global or project),
`codemode`, `tool-search` and `llama`. Only an explicit `-e <path>` would
still load, and AutoForge never passes one.

**Evidence.** `core/resource-loader.js:363, 403-405`, `docs/extensions.md`,
`docs/mcp.md`. Verified on installed Pi 1.0.1:

- Under the bare `pi --mode rpc --no-session` argv, a trusted temp project
  loaded a project extension, a global extension, a project package's
  extension, project and global `mcp.json` servers, a skill and a prompt
  template. Each one wrote a marker file or showed up in `get_commands`.
- Under the adapter's argv, the same setup gave `get_commands` → `[]` and
  no marker. No MCP server was started.

`tests/test_pi_rpc.py::test_an_installed_pi_loads_nothing_from_a_trusted_hostile_project`
repeats both runs (see "Tests").

## 3. Packages: always `--offline`

**Decision.** Every Pi argv carries `--offline`.

**Why.** At startup Pi installs or updates the packages listed in the
*global* `$PI_CODING_AGENT_DIR/settings.json` (`npm install`,
`git clone`), even with all the resource flags above. Only `--offline` or
`PI_OFFLINE=1` stops this. `--offline` also skips the pi.dev model-catalog
refresh, the version check, install telemetry and the `rg`/`fd` downloads.
It sets `PI_OFFLINE=1` and `PI_SKIP_VERSION_CHECK=1` for Pi's children.
It does **not** block model calls or the request-path OAuth token refresh.

**Evidence.** `core/package-manager.js:38-43, 704-746, 1005-1060`,
`main.js:452-456, 764-771`, and `providers/openai-codex.js:123-140, 318`
(refresh). Verified on installed Pi 1.0.1:

- a configured global npm package installed at startup without
  `--offline` and did not install with it;
- a model call and a `bash` tool call through the full adapter, under
  `--offline`, against a local OpenAI-compatible stand-in, succeeded.

A real ChatGPT sign-in and its token refresh were not exercised here.
#135's opt-in live smoke test (`tests/test_live_pi.py`,
[Pi operator guide](pi.md#prove-it-on-your-machine-the-live-smoke-test))
runs one REVIEW under this argv through a real credential: a DeepSeek API
key (the acceptance run) or, optionally, a real sign-in. Its evidence is
posted on #135 when a maintainer runs it; a DeepSeek run is no evidence
for the sign-in.

## 4. Context files: on by default, `context_files: false` to turn off

**Decision.** Context files stay **on** by default, the same as for Claude
Code and OpenCode. The repository's `AGENTS.md`/`CLAUDE.md` are the task
context those agents read too, and AutoForge's prompts already treat
repository content as untrusted data. `options.context_files: false` maps
to `--no-context-files` and removes all of them, the agent directory's
included. `doctor` warns about the ones that come from outside the checkout
(§5).

**What loads.** Pi loads one context file from the agent directory, then
one from every directory from `/` down to the cwd. In each directory it
takes the first of `AGENTS.override.md`, `AGENTS.md`, `AGENTS.MD`,
`CLAUDE.md`, `CLAUDE.MD`. Trust does not gate them.
`~/AGENTS.md` and files above home load too.

**The REMOTE worktree.** By default the agent's worktree is
`<checkout>/.git/autoforge/worktrees/<n>`, so Pi's parent walk passes
through the operator's main checkout. Pi's worktree shadowing drops the
main checkout's file only when the worktree has a file **of the same
name**. A worktree whose branch has only `CLAUDE.md`, or no file at all,
therefore also gets the main checkout's `AGENTS.md`, which can differ from
the branch under work (Claude Code's own parent walk behaves alike). In
this repository both have `AGENTS.md`, so the main checkout's copy is
shadowed. Mitigations, in order of strength:

- `options.context_files: false`;
- `execution.worktree_dir` outside the checkout, so the walk never meets it;
- keep context files out of the directories above the checkout. `doctor`
  names them.

**Evidence.** `core/resource-loader.js:115-192` (the walk) and `:145-164`
(worktree shadowing). Verified on installed Pi 1.0.1.

## 5. Operator-owned global files: `doctor` warns

**Decision.** Files in Pi's agent directory change every AutoForge run, so
`doctor` reports them in a WARN row (never FAIL), next to #129's Pi checks:

- `SYSTEM.md` and `APPEND_SYSTEM.md` there **always** load. They are not
  trust-gated and no flag turns them off;
- its context file, and one from each directory above the checkout,
  unless every Pi profile sets `context_files: false`.

The row checks whether each path exists. It never opens a file, and never
`auth.json`. The agent directory is `$PI_CODING_AGENT_DIR` when the launch
allow-list passes it, otherwise `~/.pi/agent`.

**Evidence.** `core/resource-loader.js` (system-prompt discovery).
Verified on installed Pi 1.0.1.

## 6. Tools: always an explicit `--tools`

**Decision.** Every Pi argv carries `--tools <list>`.
`options.tools` sets the list: comma-separated, no spaces, no duplicates,
each one of Pi's built-in tools `read, bash, edit, write, grep, find, ls,
powershell`. Anything else is a configuration error, because Pi does not
reject an unknown name: under `--tools bogus`, Pi 1.0.1 starts and answers
RPC without an error. Without the option, the default depends on the profile:

| Profile | Default `--tools` |
|---|---|
| `analyze_execute`, `fix`, `replan_reexecute` | `read,bash,edit,write` (Pi's own default) |
| every reviewer profile, `update_epic` | `read,bash` |

The read set is defence in depth, **not a write barrier**: `bash` stays,
because a reviewer needs `git` and `gh`. The real guard is unchanged:
independent GitHub verification. Because the list is always explicit, a
`defaultTools` setting in Pi's settings cannot widen it.

**Evidence.** `core/tools/index.js:19-28`, `core/sdk.js:144-148`,
`core/agent-session.js:1099-1124`. Verified on installed Pi 1.0.1: through
the adapter, an `analyze_execute` request declared exactly
`read, bash, edit, write` to the model and a `review_round_1` request
declared `read, bash`.

## 7. Approvals and sandboxing

Pi has no approval layer. A Pi profile is therefore equivalent to Claude
Code under `--permission-mode bypassPermissions`. Under the default
`sandbox.mode: disabled`, run unattended Pi phases in a container, a VM or
under a dedicated UID ([security.md](security.md#agents-run-as-you); Pi's
`docs/containerization.md` calls a container "usually the strongest
practical option"). #132 implemented no
sandbox; `sandbox.mode: required` (#168,
[ADR 0005](adr/0005-outer-sandbox-and-host-resource-capability-model.md))
now runs Pi's RPC child and its per-launch auth preflight inside the outer
sandbox, with the same declaration (§8), like every other provider: a
model-generated command sees the system runtime, a private home and
`/tmp`, its workspace with the repository's git metadata read-only, a
REMOTE launch's read context and what Pi declares; the rest of the
operator's home and files is absent. It still runs as the operator's UID
on the host network
([Configuration: outer sandbox](configuration.md#outer-sandbox)).

## 8. Environment and credentials

**Decision.**

- `PiProvider.environment_names` lists explicit names only, with no
  `PI_*` wildcard: `PI_CODING_AGENT_DIR`, `PI_OFFLINE`,
  `PI_SKIP_VERSION_CHECK`, `PI_TELEMETRY`.
  - `PI_PACKAGE_DIR` is dropped. It overrides Pi's own package
    directory for Nix/Guix, and a wrapper that needs it sets it itself.
  - `PI_CACHE_RETENTION` is dropped. It is a prompt-cache tuning knob,
    not something a run needs.
  - `PI_CODING_AGENT_SESSION_DIR`, `PI_SHARE_VIEWER_URL` and the TUI
    variables are not needed.
  - No `OPENAI_*`, `ANTHROPIC_*`, `GEMINI_*` or `GOOGLE_*` reaches Pi
    by default. A key reaches it only through
    `execution.env_allowlist_extra`.
- The adapter does not set `PI_OFFLINE` or `PI_TELEMETRY` itself.
  `--offline` covers the first. Install telemetry is interactive-only, and
  an operator who also wants the OpenRouter/NVIDIA/Cloudflare attribution
  headers off sets `PI_TELEMETRY=0`, which is passed through.
- **API-key guard.** With `options.require_oauth` (default `true`), a
  launch whose environment would carry a set `OPENAI_API_KEY` is refused
  before anything is spawned. The error names the variable, never its
  value. Pi prefers a stored sign-in, so the key would sit in every tool
  command's environment for nothing, or bill the run once the sign-in
  lapsed. `doctor`'s auth row fails the same way.
- **Per-launch auth preflight.** With `require_oauth`, every launch first
  runs `pi auth check --model M --json --no-refresh` in the same cwd and
  environment, with #129's strict parser and verdict. The launch goes
  ahead only on `ready` for the configured provider with `authType`
  `oauth`. Otherwise the phase fails with a `provider_failure` that quotes
  the check's four fields and never its output, and Pi is never started.
  The check is read-only and cheap, and it catches a sign-in that lapsed
  after `doctor` ran.
- **Under `sandbox.mode: required`** (#168, ADR 0005 D7.1, D7.2), Pi
  declares an absolute `PI_CODING_AGENT_DIR` as a directory writable where
  it resolves, the variable naming it there inside (a link to it, which
  the sandbox does not show, is replaced by the directory), since Pi
  rewrites `auth.json` and `models-store.json` there by rename at startup,
  which a single-file bind refuses; `OPENAI_API_KEY` is a variable, when it
  passes at all. A `PI_CODING_AGENT_DIR` that is,
  contains or lies inside `~/.pi/agent` is refused, because a sandboxed
  agent could then plant what the operator's next unsandboxed `pi` loads.
  Without one, the private home holds no sign-in, and with `require_oauth`
  the auth preflight, run in the same sandbox, refuses every launch. So
  give AutoForge a directory of its own: set `PI_CODING_AGENT_DIR` to its
  absolute path in the shell that runs `autoforge`, and run `pi` and
  `/login openai` there once with it set.

**Credential exposure that remains.** Pi, its tools and everything they
run share the operator's UID. A model-generated command can read
`$PI_CODING_AGENT_DIR/auth.json` (mode 0600, same user), which Pi needs for
its own sign-in. It gets no GitHub or git credential through anything
AutoForge controls (#165): no `GH_TOKEN` or other variable `gh` or git
reads a credential from can be allow-listed (a token the operator forwards
under a name of their own, `MY_GITHUB_PAT` say, is not recognised and
reaches every launch if it is listed), and Pi and its auth preflight get,
like every agent, an empty `gh` configuration, a placeholder `GH_TOKEN`, an
identity-only git configuration and git settings that disable credential
helpers, askpass and the SSH agent and keys. A deliberate read of the
operator's own credential files by absolute path is not stopped
([GitHub safety](agent-guides/github-safety.md#threat-model)). A tool
command gets Pi's whole environment, with `$PI_CODING_AGENT_DIR/bin`
prepended to `PATH` and `AI_AGENT=pi` and `PI_CODING_AGENT=true` added. When
the bash tool is set to expose the session, it also gets `PI_SESSION_ID`,
`PI_PROVIDER`, `PI_MODEL` and `PI_REASONING_LEVEL`. Pi writes `auth.json` and `models-store.json`
into the agent directory at startup.

Under `sandbox.mode: required` the operator's own files are out of reach,
but `$PI_CODING_AGENT_DIR/auth.json` stays readable by every command Pi runs
(ADR 0005 D7.4), and the directory stays writable by every Pi launch that
projects it, across issues and roles: a launch can leave there what a later
one loads (`SYSTEM.md`, `APPEND_SYSTEM.md`, the agent directory's context
file, §4-5), which `doctor`'s §5 row reports.

**Evidence.** `docs/environment-variables.md`, `utils/shell.js:115-126`,
`core/tools/bash.js:135-157`, `rpc-entry.js:6-7`,
`core/auth/helpers.js:16-28` (a stored credential wins over an environment
key, docs/source only), `main.js:764-771`. The allow-list behaviour is
verified by unit tests; the agent-directory writes by installed Pi 1.0.1.

## 9. Redaction

**Decision.** `redaction.py` gains three provider-neutral, anchored
patterns:

- a quoted key and a quoted value for `refresh`, `access`,
  `refresh_token`, `access_token`, `id_token` and
  `chatgpt-account-id`/`chatgpt_account_id`. This covers JSON, Pi's
  `auth.json` shape and a Python repr, including JSON escaped inside a
  JSON string;
- an assignment or header form, such as `refresh_token=…`,
  `id_token: …` or `chatgpt-account-id: …`;
- a bare `refresh=`/`access=` followed by a value of at least 16
  characters.

Prose such as "refresh the page" or "access: denied" is left alone. The
growth bound (`MAX_GROWTH_FACTOR = 3`) still holds, and the tests pin each
pattern's worst case.

**Evidence.** `tests/test_redaction.py`. Every existing test is unchanged
and passes.

## 10. Detached tool children (ADR 0002)

**Decision.** Pi is not exempted. Option (a) of #132 is implemented:
provider-neutral orphan containment in the executor layer, recorded in
[ADR 0002 §4b](adr/0002-executor-nothing-outlives-the-invocation.md).

**Why.** Pi's `bash` tool starts every command detached (`setsid`). Pi
tracks a command's process group only while the command runs. A process
that a command backgrounded (`server &`) survives Pi's normal exit and its
SIGTERM handler. A tool still running survives SIGINT and
`killpg(pi, SIGKILL)`. Such a process holds none of AutoForge's pipes and
is in no group AutoForge created, so before this change neither ADR 0002
check could see it, and it was reported nowhere.

**Evidence.** `core/tools/bash.js:63-69`, `modes/rpc/rpc-mode.js:275-288`.
Verified on installed Pi 1.0.1, both before the change (the backgrounded
process survived) and after it. Through the adapter, a `bash` tool call
running `sleep 313 &` was killed at teardown, reported as `orphans_killed`
and left no process behind.

**Gate for #135.** On a platform without a child subreaper (anything but
Linux), every agent result carries `orphans_unchecked`, and its leftovers
sentence says a process may still be running. The prompt rule in
`common.md` is the only mitigation there. #135 gates
unattended Pi use on it: the [Pi operator guide](pi.md#unattended-use)
states that Pi is attended only on such a platform, and `doctor` adds a
row for the Pi profiles, `pi's detached tool processes are contained`,
which passes where the controller can make itself a child subreaper
and warns elsewhere. `executor.orphan_containment_available` sets the
attribute and restores it, as each contained launch sets it, so a
security policy that permits reading it but denies setting it is a
warning too. It is a warning, never a failure: an attended run that
reads each result's leftovers sentence stays possible, and no default
changes.

## Residual risks and follow-ups

- **Startup writes to the worktree.** Pi still renames a cwd
  `.pi/commands/` to `.pi/prompts/` at startup (a settings migration)
  under the full policy argv, when `.pi/prompts/` does not exist. Verified
  on installed Pi 1.0.1. It changes the agent's worktree before the agent
  runs. REMOTE verification sees the change only if it is committed.
  Candidate follow-up: refuse a Pi launch in a tree that has
  `.pi/commands/`.
- **Global system prompt files** cannot be disabled by any flag. `doctor`
  only warns.
- **Context files** from the main checkout and from directories above it
  load by default (§4).
- **Same UID.** Everything in §8 under "Credential exposure that remains".
  `sandbox.mode: required` narrows it to the sign-in in the agent
  directory (§7, §8).

## Tests

- `tests/test_pi_rpc.py`:
  - argv: every argv has the always-on flags and never `--approve`, `-a`
    or `--api-key`; the tools default per profile; `tools` validation;
    `context_files: false` → `--no-context-files`;
  - environment: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `PI_FOO` and
    `PI_PACKAGE_DIR` in the controller's environment never reach Pi;
  - the API-key guard refuses before anything is spawned, and
    `require_oauth: false` lifts it;
  - the auth preflight refuses `api_key`, a missing credential, another
    provider and unreadable output, without starting Pi;
  - **opt-in, real Pi:** set `AUTOFORGE_PI_E2E_BIN` to a `pi` binary to run
    `test_an_installed_pi_loads_nothing_from_a_trusted_hostile_project`.
    It uses a temp `HOME` and agent directory, needs no login and makes no
    model call. It first proves the fixture loads under Pi's bare argv,
    then that nothing loads under the adapter's.
- `tests/test_providers.py`, `tests/test_engine.py`: the argv, option keys
  and the preflight runner pinned.
- `tests/test_doctor.py`: the outside-instruction WARN row, the auth
  row's API-key refusal, and the containment row (#135: OK with a child
  subreaper, WARN without one, and WARN where the setting can be read
  but not set); `tests/test_executor.py` pins the probe behind it.
- **opt-in, real Pi and a real credential (#135):** `tests/test_live_pi.py`,
  marked `live` and deselected by default, runs under `-m live` with
  `AUTOFORGE_LIVE_PI=1` and a route, `AUTOFORGE_LIVE_PI_AUTH=api_key`
  (DeepSeek, `require_oauth: false`) or `oauth` (the ChatGPT sign-in): one
  REMOTE REVIEW through the operator's Pi, against an in-memory GitHub
  stand-in, then a credential scan of everything AutoForge wrote and the
  side-effect checks
  ([Pi operator guide](pi.md#prove-it-on-your-machine-the-live-smoke-test)).
  `tests/test_live_pi_support.py` checks offline that it is deselected by
  default, its settings, its scan and its other checks, and rehearses its
  whole flow on both routes against the fake `pi`.
- `tests/test_redaction.py`: the OAuth fields and the prose cases.
- `tests/test_executor.py`, `tests/test_executor_duplex.py`: orphan
  containment (ADR 0002 §5).
