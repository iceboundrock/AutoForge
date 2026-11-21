# Antigravity CLI (`agy`) security spike: authentication, permissions and sandbox isolation (#529)

- **Status:** proposed, for the maintainer to decide on #529 and EPIC #527
  (G0). Proposes a **CONDITIONAL GO** for designing an adapter in G1 **only**
  on Linux under `sandbox.mode: required`, on the `GEMINI_API_KEY` route, and
  behind the five conditions of §10 (S1–S5); **NO-GO** for unsandboxed
  unattended runs, macOS unattended runs, Google OAuth sign-in, Google Cloud
  ADC, and repositories carrying `.agents/`, `.agent/`, `_agents/`, or
  `_agent/` unless `agy` ships an explicit switch to disable hooks, MCP
  servers, and plugins. No production adapter, configuration, redaction rule,
  or policy change is made here (#529's gate).
- **Measured:** `agy` 1.5.13 (`agy --version`; binary sha256
  `c3fd2c848abf88a243936f33e25bf5970802a7651840b5199318fcd8eb3fdb65`,
  270,103,240 bytes, ELF 64-bit x86-64 dynamically linked only against
  glibc: `libresolv`, `libpthread`, `libm`, `libdl`, `librt`, `libc`),
  installed at `~/.local/bin/agy`, on Linux 7.0.14 x86_64 (Debian 13),
  `bwrap` 0.12.0, Landlock ABI 8, on 2026-11-21.
- **Evidence:** 50 scenarios recorded by `tests/antigravity_security_probe.py`
  under `tests/fixtures/antigravity-security/1.5.13/`, one JSON file per
  scenario named in backticks (`custom-dir-dot-agents` is
  `custom-dir-dot-agents.json`), each run in the same private-root `bwrap`
  sandbox with its own anonymous session keyring (`isolation: private-root`,
  `session_keyring: own`) and loopback Gemini API stand-in as #528
  (`docs/antigravity-cli-spike.md`), plus real-backend verification of `agy`
  1.5.13 inside AutoForge's `sandbox_policy` + `sandbox_bwrap` + `executor`
  (`contain_orphans=True`) and offline checks of `autoforge.child_env`,
  `autoforge.sandbox_policy`, and `autoforge.redaction`.
  `tests/test_antigravity_security_probe.py` holds the committed records and
  modules to every claim below.
- **No model was called and no real credential was used:** every model
  request carried `synthetic-agy-probe-key` to `127.0.0.1`, and every secret
  in the redaction and sandbox tests is synthetic.
- **Not measured:** live Gemini API calls (real key validation, entitlement
  errors, real 429/403 bodies), interactive Google OAuth login flow and OS
  keyring storage layout, live Google Cloud ADC token exchange, macOS and
  Windows `agy` binaries, rootless container backend execution (#271/#273),
  `agy acp` security surface, and `agy remote-control` daemon operation. Each
  is marked unsupported or untested where it matters.
- **Re-measure:**
  ```sh
  AUTOFORGE_AGY_PROBE_BIN=/path/to/agy uv run pytest tests/test_antigravity_security_probe.py
  ```
  (Linux with `bwrap`, `keyctl(2)`, Landlock ABI 6+, and `/usr/bin/python3`;
  no model call). To rewrite records for a new build in a directory of its
  own:
  ```sh
  python3 -I tests/antigravity_security_probe.py record --agy /path/to/agy \
      --out tests/fixtures/antigravity-security/<version>
  ```

---

## 1. Threat model and isolation inventory (deliverable 1)

AutoForge treats the agent, its tool subprocesses, repository files, issue
and PR text, hooks, MCP servers, plugins, skills, and rules as untrusted.
Only the controller holds GitHub credentials and performs GitHub writes,
pushes, and merges (`docs/security.md`, ADR 0004).

### Credential, configuration, state, and runtime surfaces (`agy` 1.5.13)

| Surface | Path / mechanism | Who reads | Who writes | Security consequence |
|---|---|---|---|---|
| Binary | `~/.local/bin/agy` (single 270 MB ELF; glibc only) | Controller, launch | Operator / `agy update` | Must be pinned by SHA-256 (#528 C3); no extra runtime tree needed under `bwrap` |
| API key route | `GEMINI_API_KEY` env var + `--settings '{"modelProvider":"gemini"}'` | `agy`, every `run_command` child, every hook, every stdio MCP server | Controller allow-list | Required for headless API-key launches; unavoidably exposed to all child processes inside the launch (`env-child-overrides-and-shell-startup`, `env-hook-and-mcp-inherit-key`) |
| Google OAuth login | OS keyring (when present) or `~/.gemini/` (unmeasured layout); `access_type=offline` | `agy` (default provider) | `agy` (refreshes in place) | Rotating/keyring login; **NO-GO** under ADR 0005 D7.1/D7.2 and ADR 0013 D14 |
| Google Cloud ADC | `AGY_ADC_AUTH=true` + `GOOGLE_APPLICATION_CREDENTIALS` / `~/.config/gcloud/application_default_credentials.json` | `agy` (when enabled) | `gcloud` / operator | Failed offline with `authentication required` on synthetic `authorized_user` JSON (`auth-adc-synthetic`); carries refresh tokens or PEM private keys; **NO-GO / UNSUPPORTED** in MVP |
| User CLI state & settings | `~/.gemini/antigravity-cli/` (`settings.json`, `conversations/*.db`, `conversation_summaries.db`, `brain/<id>/...`, `cli.log`, `log/cli-*.log`, `presence/*.lock`, `cache/*`, `updater/*`, `installation_id`, `jetski_state.pbtxt`, `bin/`) | `agy`, same-UID processes | `agy` on every session and on `agent` / `models` / `-p /config` | `agy` ignores `XDG_*_HOME` and writes exclusively under `$HOME/.gemini/` (`state-xdg-vs-home`). Transcripts persist across launches unless each launch gets a private empty `HOME` |
| User global customizations | `~/.gemini/config/` (`hooks.json`, `mcp_config.json`, `plugins/`, `plugins.json`, `skills/`, `AGENTS.md`, `config.json`, `projects/`) and `~/.gemini/GEMINI.md` | `agy` at session startup | Operator / `agy` | Global hooks, MCP servers, and rules execute unconditionally when present (`custom-global-home`); isolated only by giving each launch an empty `HOME` |
| Workspace customizations | `.agents/`, `.agent/`, `_agents/`, `_agent/` (`hooks.json`, `mcp_config.json`, `plugins/`, `plugins.json`, `skills/`, `skills.json`, `rules/`, `rules.json`, `agents/`), plus `AGENTS.md`, `GEMINI.md` | `agy` walking from `cwd` up to git root | Repository / untrusted PR / agent tools | **Primary blocker (S1):** hooks execute shell commands and MCP configs start local processes or make outbound HTTP requests before the first model call; no flag or setting in 1.5.13 disables them (§4) |
| Internal Language Server IPC | `ANTIGRAVITY_LS_ADDRESS` (`localhost:<ephemeral>`) + `ANTIGRAVITY_CSRF_TOKEN` | Every `run_command` subprocess; any same-host process on host network | `agy` per launch | Injected into every `run_command` environment (`env-child-overrides-and-shell-startup`). On `NetworkPosition.HOST`, reachable over loopback by the agent's tools and same-host processes; token is not redacted today (§7) |
| Remote-control daemon | `systemd --user` (`antigravity-cli-daemon.service`) via `$DBUS_SESSION_BUS_ADDRESS` / `$XDG_RUNTIME_DIR` | `agy remote-control` / `--remote-control` | Operator / daemon | Never pass `--remote-control` or `--remote-agent`; `child_env.py` already forbids both D-Bus/runtime-dir variables (`ro-remote-control-status`) |
| Tool subprocesses | `run_command` spawns `bash -c` in a new session (`setsid`); `PATH` has `$HOME/.gemini/antigravity-cli/bin` prepended | Agent tools | Agent tools | Detached children survive `agy`'s exit/SIGTERM (#528 §7); contained by Linux `bwrap` PID namespace or AutoForge's ADR 0002 subreaper (§6) |

---

## 2. Authentication, state layout and read-only probing

### Tested authentication routes

| Route | Trigger | Observed on `agy` 1.5.13 (offline) | Verdict |
|---|---|---|---|
| Gemini API key | `GEMINI_API_KEY` + `--settings '{"modelProvider":"gemini"}'` | Works cleanly against loopback stand-in; sends `x-goog-api-key` header (`env-child-overrides-and-shell-startup`) | **Supported** (only MVP route) |
| `GOOGLE_API_KEY` only | `GOOGLE_API_KEY` without `GEMINI_API_KEY` + `modelProvider: "gemini"` | Exit 1 before any request: `GEMINI_API_KEY environment variable is not set` (`auth-google-api-key-only`) | Not read by `agy` |
| `GEMINI_API_KEY` without `modelProvider` | `GEMINI_API_KEY` alone, no `--settings` | Exit 1 (`ERROR` result): `authentication required. Run 'agy' to log in` (`auth-gemini-key-without-provider`) | Ignored without `modelProvider: "gemini"` |
| Google Cloud ADC | `AGY_ADC_AUTH=true` + `GOOGLE_APPLICATION_CREDENTIALS` (synthetic `authorized_user`) | Exit 1 (`ERROR` result): `authentication required` (`auth-adc-synthetic`) | **NO-GO / UNSUPPORTED** |
| Google OAuth sign-in | Default provider (no `modelProvider`) | Interactive 60 s OAuth code prompt on argv or immediate failure on stdin (#528 `no-login-*`); uses `access_type=offline` + OS keyring | **NO-GO / REFUSED** |

### Filesystem roots and XDG behavior

Setting custom `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, and
`XDG_STATE_HOME` left zero files in all four XDG directories (`state-xdg-vs-home`).
`agy` 1.5.13 writes exclusively beneath `$HOME/.gemini/antigravity-cli/` and
`$HOME/.gemini/config/`. Print mode refuses the `acp` subcommand's
`--gemini-dir` and `--app-data-dir` flags (#528 `acp-flag-in-print-mode`), so
`$HOME` is the only supported mechanism to isolate `agy`'s state and global
configuration.

### Disk writes by subcommand and read-only auth probing

| Command | Writes to `$HOME`? | Network / model call? | Safe for `doctor`? | Evidence |
|---|---|---|---|---|
| `agy --version` | No (0 files) | No | Yes | `ro-version` |
| `agy --help` | No (0 files) | No | Yes | `ro-help` |
| `agy mcp list` | No (0 files) | No | Yes | `ro-mcp-list` |
| `agy plugin list` | No (0 files) | No | Yes | `ro-plugin-list` |
| `agy remote-control status` | No (0 files) | Local D-Bus only | Informational only | `ro-remote-control-status` |
| `agy agent` | **Yes (15 files under `~/.gemini/`)** | No | Only with throwaway `HOME` | `rw-agent-list` |
| `agy -p /config --output-format json` | **Yes (14 files under `~/.gemini/`)** | No | Only with throwaway `HOME` | `rw-config-catalogue` |
| `agy models` (with saved `modelProvider: "gemini"`) | **Yes (15 new files under `~/.gemini/`)** | No | **False positive for auth** | `auth-models-no-key`, `auth-models-bogus-key-offline` |

**Auth-probing false positive (`auth-models-bogus-key-offline`):** with
`{"modelProvider":"gemini"}` saved in `settings.json` and a synthetic,
non-working `GEMINI_API_KEY` inside a network-isolated sandbox, `agy models`
exited 0 and printed 25 models without making any network request. It verifies
only that `GEMINI_API_KEY` is non-empty (`auth-models-no-key` exits 1 when
unset). **No read-only `agy` command proves that an API key is valid, active,
or entitled to a given model without a live API call.** `doctor` must
therefore check `GEMINI_API_KEY` presence only (never its value) and report
live model/entitlement access as `SKIP`, matching `claude-sdk` and
`opencode-sdk`.

---

## 3. Environment inheritance and `child_env.py` compatibility

`env-child-overrides-and-shell-startup` launched `agy` with the exact
variables `autoforge.child_env.child_overrides` sets (`GH_TOKEN`,
`GH_ENTERPRISE_TOKEN`, `GH_CONFIG_DIR`, `GIT_CONFIG_GLOBAL`,
`GIT_TERMINAL_PROMPT`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0..3`,
`GIT_CONFIG_VALUE_0..3`, `CURL_HOME`, `WGETRC`, `WGET2RC`, `ZDOTDIR`,
`SHELL=/bin/bash`), plus host-style startup canaries in `~/.bashrc`,
`~/.bash_profile`, `~/.profile`, and `~/.zshenv` that export `CANARY_*` and
overwrite `GH_TOKEN`.

Observed inside `run_command`:

1. **Shell invocation:** `ARG0=bash`, `FLAGS=hBc` (non-interactive, non-login
   `bash -c`). None of `~/.bashrc`, `~/.bash_profile`, `~/.profile`, or
   `~/.zshenv` executed (`CANARY_*` absent).
2. **Verbatim `child_env.py` preservation:** every controller override reached
   the tool subprocess unchanged (`GH_TOKEN=autoforge-no-github-credential`,
   `GIT_CONFIG_COUNT=4`, hardened `core.sshCommand`, empty `GH_CONFIG_DIR`,
   `CURL_HOME`, `WGETRC`, `WGET2RC`, `ZDOTDIR`, `SHELL`).
3. **`agy`-injected variables:** `agy` prepended
   `$HOME/.gemini/antigravity-cli/bin` to `PATH` and injected
   `GIT_EDITOR=false`, `GIT_PAGER=cat`, `HGEDITOR=false`, `JJ_EDITOR=false`,
   `JJ_PAGER=cat`, `PAGER=cat`, and ten `ANTIGRAVITY_*` variables including
   `ANTIGRAVITY_LS_ADDRESS` (`localhost:<port>`) and `ANTIGRAVITY_CSRF_TOKEN`.
4. **Credential propagation:** `GEMINI_API_KEY` reached `run_command`
   (`env-child-overrides-and-shell-startup`), repository hooks, and stdio MCP
   servers (`env-hook-and-mcp-inherit-key`). This is unavoidable for
   `run_command` (same process tree as `agy`, matching Claude Code, OpenCode,
   and Pi), and unacceptable for hooks/MCP servers (which must not run).
5. **Allow-list hygiene:** `GEMINI_API_KEY` is not in `FORBIDDEN_ENV_NAMES`
   and does not trigger `credential_looking_entry`. An adapter must add only
   the exact name `("GEMINI_API_KEY",)` to `environment_names` — never
   `GEMINI_*`, `GOOGLE_*`, or `AGY_*` wildcards, which would widen exposure to
   unrelated operator variables (`GOOGLE_APPLICATION_CREDENTIALS`,
   `GOOGLE_GEMINI_BASE_URL`, or custom `GEMINI_GITHUB_TOKEN`).

---

## 4. Integration isolation canaries

This section completes the unmeasured matrix flagged by #528 C1 and tests all
customization surfaces listed in #529 under the fixed headless argv.

| Canary | Observed on `agy` 1.5.13 | Evidence |
|---|---|---|
| `.agents/`, `.agent/`, `_agents/`, `_agent/` at `cwd` | **All four execute** `hooks.json` (`SessionStart`), start `mcp_config.json` stdio servers, and inject `rules/*.md` into the system prompt | `custom-dir-dot-agents`, `custom-dir-dot-agent`, `custom-dir-under-agents`, `custom-dir-under-agent` |
| Bundled plugins (`plugins/<name>/`) | **Executes** `hooks.json`, starts `mcp_config.json`, injects `rules/AGENTS.md` | `custom-plugins-manifests-and-http-mcp` |
| JSON manifests (`plugins.json`, `skills.json`, `rules.json`) | **All three load** external directories outside `.agents/` (`ext_plugins` hook executed; `ext_skills` and `ext_rules` injected into system prompt) | `custom-plugins-manifests-and-http-mcp` |
| Custom agents (`.agents/agents/custom.md`) | **Loaded** into system prompt (`CUSTOM-AGENT-MARKER`) | `custom-plugins-manifests-and-http-mcp` |
| Streamable HTTP MCP (`mcp_config.json` with `url`) | **Sends 4 HTTP requests** (`POST /mcp-canary` `server/discover` & `initialize`, `GET /.well-known/oauth-protected-resource/...`) before the first model call | `custom-plugins-manifests-and-http-mcp` |
| `cwd` inside subdirectory of git repo (`{work}/a/b`) | **Walks up to git root:** runs hooks in `{work}/a/.agents/` and `{work}/.agents/`, starts `{work}/.agents/mcp_config.json`, loads `AGENTS.md` and rules from each ancestor up to git root | `custom-traversal-cwd-below-repo-root` |
| Ancestor above git root (`{base}/.agents/`, `{base}/AGENTS.md`) | **Not loaded:** discovery stopped at `{work}/.git` | `custom-traversal-above-repo-root` |
| Real `git worktree` at `parent_repo/.git/autoforge/worktrees/1` | **Not loaded:** discovery stopped at the worktree's `.git` file and did not cross into `parent_repo/.agents/` or `parent_repo/AGENTS.md` | `custom-traversal-git-worktree` |
| Subdirectory below `cwd` (`{work}/sub/.agents/`) | **Not loaded:** discovery did not descend below `cwd` | `custom-traversal-below-cwd` |
| Symlink inside workspace (`.agents -> real_agents`, `.agents/hooks.json -> ../real_hooks.json`) | **Followed and executed** | `custom-symlink-inside-workspace`, `custom-symlink-file-inside-workspace` |
| Symlink pointing outside workspace (`.agents -> {base}/elsewhere`) | **Not followed** (0 markers) | `custom-symlink-outside-workspace` |
| Global home (`~/.gemini/config/`, `~/.gemini/GEMINI.md`, `~/.agents/`) | `~/.gemini/config/hooks.json`, `mcp_config.json`, `AGENTS.md`, and `~/.gemini/GEMINI.md` **all loaded/executed**; `~/.agents/hooks.json` did not | `custom-global-home` |
| `--add-dir {base}/extra` | **Widens discovery:** executes `{base}/extra/.agents/hooks.json` and loads `{base}/extra/AGENTS.md` | `custom-add-dir-widening` |
| Mid-session write of `.agents/hooks.json` during a run | **Not hot-reloaded:** after `write_file` created `.agents/hooks.json` and a subsequent `run_command` executed, 0 of 5 lifecycle hooks (`PreToolUse`, `PostToolUse`, `PreInvocation`, `PostInvocation`, `Stop`) fired in that session | `custom-mid-session-write-hooks` |
| Foreign configs (`.mcp.json`, `.gemini/hooks.json`, `.antigravity/settings.json`, `.claude/settings.json`, `CLAUDE.md`) | **Ignored** (0 markers, 0 system markers) | `custom-foreign-configs-ignored` |
| Prompt `/config` without vs with `--disable-slash-commands` | Without flag: exit 2 (`unavailable with --input-format stream-json`); with flag: sent verbatim to model | `custom-slash-config-without-disable`, `custom-slash-config-with-disable` |

### Isolation verdict

`agy` 1.5.13 has **no CLI flag or `--settings` key that disables workspace
hooks, MCP servers, or plugins**. However, the measurements above establish
the exact boundary on the pinned build:

1. **Global customizations** (`~/.gemini/config/`, `~/.gemini/GEMINI.md`) are
   100% eliminated by giving every launch an empty per-launch `HOME`.
2. **Workspace customizations** are discovered only through the four root
   names (`.agents`, `.agent`, `_agents`, `_agent`) on the path from `cwd` to
   the git root (including symlinks pointing inside the workspace) and through
   `--add-dir`. They are loaded at startup only, not mid-session.
3. **Fail-closed mitigation (until `agy` adds a native switch):** refuse any
   launch in `execute(req)` whenever `.agents`, `.agent`, `_agents`, or
   `_agent` (directory, file, or symlink, whatever it contains) exists at
   `cwd`, in any ancestor directory up to `/`, or anywhere below `cwd`, and
   never pass `--add-dir` or `extra_args`. Scanning before every launch also
   blocks a hook or MCP file written by an earlier phase (`ANALYZE_EXECUTE`)
   from executing on the next phase (`REVIEW`/`FIX`).

If the maintainer considers refusing repositories with `.agents/` unacceptable
(or prefers not to rely on pre-launch directory refusal instead of a CLI
switch), the decision is **NO-GO** until `agy` adds an explicit switch.

---

## 5. Tools, permissions, modes and native `--sandbox`

| Configuration | File ops outside workspace | `run_command` | Denied-action observability | Evidence |
|---|---|---|---|---|
| Default (`toolPermission: request-review`, `allowNonWorkspaceAccess: false`) | `read_file`, `write_file`, and `edit_file` **all succeed outside workspace** without prompting | Auto-denied; terminates run at exit 0, `status: SUCCESS`, empty response, `denied_actions: [{"action":"command","display_name":"Bash"}]` | Observable via `denied_actions` (must be treated as failure by reducer) | `perm-default-read-outside`, `perm-default-write-outside`, `perm-default-edit-outside`, `perm-default-cmd-denied` |
| `--mode plan` | `write_file` **succeeds outside workspace** | Auto-denied (`denied_actions`) | Same as default | `perm-mode-plan-write-outside` |
| `--mode bogus` | Warns on stderr and **continues running** (exit 0) | Same as default | **Does not fail closed** | `perm-mode-bogus-warns-and-runs` |
| `toolPermission: strict` (bare vs with `permissions.allow: ["command(git status --short)", "command(*)"]`) | Auto-denies `write_file` (`denied_actions: [{"action":"write_file","display_name":"Edit"}]`) | Honors explicit `command(...)` allow-rules (`git status --short` ran); auto-denies unlisted commands | Observable via `denied_actions` | `perm-strict-denies-write`, `perm-strict-honors-allow` |
| `request-review` + `permissions.allow: ["command(regex:^git (status\|diff\|add\|commit)\\b)"]` | Still unrestricted outside workspace | Allows `git status --short`; auto-denies `git push` **and** `git status --short && echo CHAINED` (parses `&&` chains) | Unlisted commands produce `denied_actions` | `perm-rr-allow-git-local`, `perm-rr-deny-unlisted-git-push`, `perm-rr-blocks-shell-chain` |
| `toolPermission: always-proceed` + `permissions.deny: ["command(curl)"]` | Unrestricted unless denied | Runs allowed commands; **honors `permissions.deny`** on `curl` | **Important difference:** a `permissions.deny` hit returns a **tool error** to the model and lets the run finish `status: SUCCESS`, `denied_actions: null`, exit 0 | `perm-always-proceed-honors-deny` |
| `--dangerously-skip-permissions` + `permissions.deny: ["command(curl)"]` | Unrestricted unless denied | **Honors `permissions.deny`** on `curl` (contrary to blanket bypass assumption) | Same tool-error path (`denied_actions: null`) | `perm-skip-flag-honors-deny` |
| `permissions.deny: ["command(curl)"]` + `sh -c '...'` | — | **Trivially bypassed** by wrapping commands in `sh -c` | Not caught by command denylist | `perm-deny-bypassed-by-subshell` |
| Invalid RE2 regex in `permissions.deny` (`write_file(regex:^(?!...))`) | **Silently ignored** (no error); `write_file` outside workspace succeeds | — | **Fails open** | `perm-invalid-re2-regex-silently-ignored` |
| Unknown key inside `permissions` (`{"noSuchRuleList": [...]}`) | **Silently ignored** (no error; unlike top-level `--settings` keys which exit 1) | — | **Fails open** | `perm-unknown-nested-key-silently-ignored` |
| `--sandbox` (native terminal sandbox inside `bwrap`) | — | Executed `echo sandboxed > sbx-ran.txt` inside nested `bwrap` | Does not disable hooks/MCP (#528 `repository-customizations-sandbox`) | `perm-native-sandbox-flag` |

### Permission conclusions

1. **CLI permission modes are NOT a containment boundary.** Default
   `request-review` with `allowNonWorkspaceAccess: false` and `--mode plan`
   both allow reading, writing, and editing files outside the workspace;
   invalid RE2 regexes and unknown nested keys under `permissions` fail open
   silently; and shell denylists are bypassed by `sh -c`.
2. **Blanket `--dangerously-skip-permissions` is not required.**
   `--settings '{"modelProvider":"gemini","toolPermission":"always-proceed","allowNonWorkspaceAccess":false,"permissions":{"deny":[...]}}'`
   runs read/edit/write/shell tools unattended while keeping explicit
   `permissions.deny` rules active (e.g., denying `write_file(*)` on read-only
   `REVIEW` profiles as defence in depth, though shell commands still require
   outer-sandbox containment). For phases that only need a small fixed set of
   commands, `request-review` + `permissions.allow` works and surfaces any
   unlisted command via `denied_actions`.
3. **Two distinct refusal channels:** an auto-denied `request-review` ask ends
   the run immediately with `status: SUCCESS` and non-empty `denied_actions`
   (the reducer MUST fail the run); an explicit `permissions.deny` hit returns
   a tool error to the model and continues with `denied_actions: null`.

---

## 6. AutoForge outer sandbox, container mapping and process containment

### Minimal `ProviderRequirements` for Linux `sandbox_bwrap` (ADR 0005)

Because `agy` 1.5.13 is a single dynamically linked ELF binary depending only on
glibc (`/usr`, `/lib*`) and writes only under `$HOME/.gemini/`:

- `executable`: resolved path to `agy` (via `executable_requirements`, pinned
  by SHA-256 before execution; see #528 C3).
- `install_roots`, `path_dirs`, `spelled_paths`: from `executable_requirements`
  alone (read-only).
- `config`, `home_config`, `state`: **empty** under `sandbox.mode: required`.
  The compiler's private tmpfs `HOME` gives every launch an empty, writable
  `~/.gemini/` that vanishes on exit and satisfies ADR 0015's fresh-context
  proof automatically.
- `auth`: `(AuthItem(AuthMode.ENV, "GEMINI_API_KEY"),)` only. Google OAuth
  sign-in (`AuthMode.KEYRING`) and ADC are refused.
- `operator_config_roots`: `(os.path.join(home, ".gemini"),)` (blocks any
  projection from touching the operator's real `~/.gemini/`).
- `path_names`: `()` (no path-valued env var passed).
- **Container backend (`CredentialRoutes`, ADR 0013 D14, future #271/#273):**
  `scope="google"`, `routes=(EnvRoute("GEMINI_API_KEY", CredentialKind.API_KEY),)`,
  `unsupported=(UnsupportedRoute("AGY_ADC_AUTH", CredentialKind.CLOUD, ...), UnsupportedRoute("GOOGLE_APPLICATION_CREDENTIALS", CredentialKind.CLOUD, ...), UnsupportedRoute("GOOGLE_GEMINI_BASE_URL", ...))`,
  `stores=()` (private tmpfs `HOME` isolates `~/.gemini/`).

### Real-backend verification (`agy` 1.5.13 inside AutoForge `sandbox_bwrap` + `executor`)

Verified on this host (Linux 7.0.14, `bwrap` 0.12.0, Landlock ABI 8) with
`autoforge.sandbox_policy.describe_launch`, `autoforge.sandbox_bwrap`,
`autoforge.child_env.isolated_overrides`, and `autoforge.executor.execute`
against the loopback stand-in:

| Check | Result |
|---|---|
| Launch outcome | `exit_code=0`, `ok=True`, `sandbox.enforced=True` |
| Host `~/.ssh/id_rsa`, `~/.config/gh/hosts.yml`, `~/.netrc` | Absent (`ENOENT`) |
| Host `~/.gemini/config/hooks.json` | Absent (`ENOENT`); host hook did not run |
| Controller `state.json`, `--config` file, operator checkout | Absent (`ENOENT`) |
| Shared git common dir (`HEAD`) and worktree `.git` pointer | Read-only (`EROFS`, errno 30) |
| Host abstract Unix socket canary | Blocked by Landlock ABI 6+ (`PermissionError`, `EPERM`) |
| `GH_TOKEN` inside tool subprocess | `autoforge-no-github-credential` |
| Local `git add && git commit` inside worktree | Succeeded; 3 git objects written to `private_objects` overlay |
| Detached child (`setsid -f sleep`) under `sandbox_bwrap` | 0 survivors (killed by PID namespace teardown) |
| Detached child (`setsid -f sleep`) unsandboxed under Linux `executor` (`contain_orphans=True`) | `orphans_killed=True`, `orphan_survived_kill=False`, 0 survivors (ADR 0002 child subreaper) |

**Host dependencies and macOS limitations:**

- **Linux enforced (`sandbox.mode: required`):** requires `bwrap` with
  unprivileged user, PID, and IPC namespaces, overlay mount, `os.memfd_create`,
  `keyctl(2)`, and Landlock ABI 6+ (Linux 6.12+).
- **macOS:** has no outer sandbox (`sandbox.mode: required` is refused) and no
  Linux child subreaper (`orphans_unchecked=True`). Because `agy` runs every
  `run_command` in its own session (`setsid`) and leaves detached/background
  tasks running on SIGTERM and under `--print-timeout` (#528 §7), macOS cannot
  contain either filesystem access or orphaned descendants. **macOS is marked
  UNSUPPORTED / UNTESTED for unattended `agy` runs.**

---

## 7. Redaction baseline audit (`src/autoforge/redaction.py`)

Tested `autoforge.redaction.redact` and `credential_classes` against synthetic
canaries in `agy` native output formats (`stream-json`, `AGY_ERROR`, tool
output; pinned offline in `tests/test_antigravity_security_probe.py`):

| Secret shape / context | Current `redaction.py` result | Pattern matched | Action for G1 |
|---|---|---|---|
| `AIza` + 35 chars (bare, in `GEMINI_API_KEY=AIza...`, in `x-goog-api-key`, in `AGY_ERROR`) | **Redacted** | `google-api-key` | Keep |
| `x-goog-api-key: <non-AIza>` | **Redacted** | `secret-assignment` | Keep |
| JSON `"access_token": "ya29..."`, `"refresh_token": "1//..."` | **Redacted** | `oauth-field` | Keep |
| `Authorization: Bearer ya29...`, JWT `id_token` | **Redacted** | `authorization-header`, `jwt` | Keep |
| `stream-json` escaped `GH_TOKEN=\"ghp_...\"` | **Redacted** | `env-assignment` | Keep |
| `GEMINI_API_KEY=<non-AIza>` / `GOOGLE_API_KEY=<non-AIza>` (plain or JSON-escaped) | **LEAK** | none (`_` blocks `secret-assignment`; #285) | **Required in G1:** add `GEMINI_API_KEY` and `GOOGLE_API_KEY` to `env-assignment` |
| `ANTIGRAVITY_CSRF_TOKEN=<value>` | **LEAK** | none | **Required in G1:** add `ANTIGRAVITY_CSRF_TOKEN` to `env-assignment` |
| Bare `ya29.<...>`, bare `1//<...>`, `GOCSPX-<...>` (bare and `"client_secret": "GOCSPX-..."`) | **LEAK** | none | **Recommended in G1** (defence in depth if operator ever leaks OAuth/ADC text; keep `MAX_GROWTH_FACTOR = 3`) |
| ADC `service_account` PEM private key block | **LEAK** | none (#285) | Tracked under #285; ADC stays refused in MVP |

---

## 8. Proposed fail-closed launch policy and Doctor preflight plan (deliverables 3 & 4)

### Fail-closed launch policy (for G1 design)

1. **Sandbox requirement:** `sandbox.mode: required` on Linux (bwrap +
   Landlock) is mandatory for unattended runs; fail closed on macOS and on any
   host that cannot enforce. (If an explicit opt-in for `disabled` is ever
   considered for attended local testing, it must still supply a fresh empty
   per-launch `HOME` and warn loudly on the same-UID residual.)
2. **Build-digest gate:** resolve the configured binary path, copy or lock it
   in a controller-owned read-only staging location (closing the TOCTOU window
   of #528 C3), verify its SHA-256 against the approved table
   (`c3fd2c848abf88a243936f33e25bf5970802a7651840b5199318fcd8eb3fdb65` for
   1.5.13), and refuse any unapproved binary before spawning.
3. **Fixed argv, no `extra_args`:**
   ```sh
   agy --input-format stream-json --output-format stream-json \
       --settings '{"modelProvider":"gemini","toolPermission":"always-proceed","allowNonWorkspaceAccess":false}' \
       --model <allowlisted-id> --effort <low|medium|high|xhigh|max> \
       --disable-slash-commands
   ```
   Never pass `--add-dir`, `-c`, `--continue`, `--conversation`,
   `--remote-agent`, `--remote-control`, `--agent`, `--mode`, `--project`,
   `--new-project`, `--log-file`, `-i`, or user `extra_args`. (Optional
   phase-scoped `permissions.deny` such as `write_file(*)` on `REVIEW` may be
   added in `--settings` as defence in depth after testing each rule.)
4. **Environment:** allow-list adds only `("GEMINI_API_KEY",)`. Refuse
   `AGY_ADC_AUTH` or `GOOGLE_APPLICATION_CREDENTIALS` if present in the
   profile's launch environment. Apply all `child_env.py` overrides and
   preflight unchanged.
5. **Pre-launch workspace customization scan (in `execute(req)`):** resolve
   `req.cwd` and refuse with `ConfigurationError` if `.agents`, `.agent`,
   `_agents`, or `_agent` (directory, file, or symlink) exists at `cwd`, in
   any ancestor up to `/`, or anywhere below `cwd`.
6. **Prompt and stream gates:** enforce #528's 160,000-byte UTF-8 bound,
   `dropped.txt` table check, strict NDJSON/UTF-8/LF-only reducer (including
   G1 carry-forward `R5-F2` unpaired-surrogate check and `R5-F7`
   `code_kind == "http"` check), and treat any non-empty `denied_actions` or
   `unknown` step as `provider_failure`.

### Doctor preflight plan (deliverable 4)

| `doctor` row | Read-only provable? | Method (never prints secret values) | Failure / warning text |
|---|---|---|---|
| `antigravity binary digest` | Yes (0 writes) | Resolve executable, hash SHA-256, run `agy --version` (0 home writes; inside `bwrap` when sandbox enabled) | `FAIL`: unapproved binary SHA-256 (names digest and version printed; points to re-measurement probe) |
| `antigravity required flags` | Yes (0 writes) | Run `agy --help` (0 home writes); assert `--input-format`, `--output-format`, `--settings`, `--model`, `--effort`, `--disable-slash-commands` are listed | `FAIL`: missing required CLI flag(s) |
| `antigravity customization scan` | Yes (filesystem walk only) | Scan checkout and ancestors/descendants for `.agents`, `.agent`, `_agents`, `_agent` (dir/file/symlink) | `FAIL`: forbidden repository customization directory found (names path; explains hooks/MCP cannot be disabled in `agy` 1.5.13) |
| `antigravity credential` | Presence only (not validity) | Check `GEMINI_API_KEY` is non-empty in launch env; fail if `AGY_ADC_AUTH` is set | `FAIL`: `GEMINI_API_KEY` not set (or unsupported `AGY_ADC_AUTH` set) |
| `antigravity model access / entitlement` | **No** (requires live API call; `agy models` is a false positive) | Do not invoke `agy models` or any API endpoint | `SKIP`: cannot verify API key validity or model entitlement without a live model call |
| `outer sandbox` rows (existing) | Yes | Existing `doctor_sandbox.py` capability, mode, compiled `ProviderRequirements`, and sandboxed `agy --version` | `FAIL` if `sandbox.mode: required` cannot be enforced on this host |
| `no credential reaches a launch` (existing) | Yes | Existing `child_env.preflight` (`git config`, `~/.netrc`, login shell startup) | `FAIL` on any forbidden git/netrc/shell route |

---

## 9. Residual risks and environment matrix (deliverable 5)

### Environment support matrix

| Environment | Support status | Containment & residuals |
|---|---|---|
| **Linux (`sandbox.mode: required`, `bwrap` + Landlock ABI 6+)** | **Proposed CONDITIONAL GO** (behind S1–S5) | Filesystem, host home, `~/.gemini`, `~/.ssh`, `gh`, state dir, config file, session keyring, abstract sockets, and PID teardown all enforced. **Residuals:** same UID, host network (including host loopback services and `ANTIGRAVITY_LS_ADDRESS`), `GEMINI_API_KEY` readable by tool subprocesses inside the launch, outbound traffic unrestricted (#294) |
| **Linux (`sandbox.mode: disabled`)** | **NO-GO for unattended** | Same-UID residual: `agy`'s tools (`write_file`, `edit_file`, `run_command`) can read/write any operator file (`~/.ssh`, `~/.config/gh/hosts.yml`, AutoForge state/config) despite `allowNonWorkspaceAccess: false`; subreaper catches detached `setsid` orphans, but not self-subreapers or services |
| **Linux rootless container backend (#271/#273)** | **Untested** (design-compatible via `CredentialRoutes`) | Would close host-loopback and same-UID residuals once #271/#273 land; requires container-image pin and runtime verification |
| **macOS** | **NO-GO / UNSUPPORTED / UNTESTED** | No outer sandbox (`required` refused), no Linux subreaper (`orphans_unchecked`), `agy` macOS binary not measured, Keychain is same-UID |
| **Windows** | **NO-GO / UNSUPPORTED / UNTESTED** | Not supported by AutoForge outer sandbox; `agy` Windows binary not measured |

### Residual risks even under Linux `sandbox.mode: required`

1. **Host network & loopback exposure:** agents run with
   `NetworkPosition.HOST`, so an agent's tool subprocess can reach any
   service listening on host loopback or LAN and can exfiltrate
   `GEMINI_API_KEY` or repository code until egress control (#294) or the
   container backend (#271/#273) lands.
2. **Internal Language Server (`ANTIGRAVITY_LS_ADDRESS`):** `agy` exposes an
   ephemeral loopback HTTP/gRPC server with `ANTIGRAVITY_CSRF_TOKEN` in every
   tool subprocess's environment. Other same-host processes can also reach that
   loopback port; its API surface was not audited.
3. **Pre-launch scan vs native CLI switch:** refusing `.agents`/`.agent`/
   `_agents`/`_agent` is proven only for `agy` 1.5.13's measured discovery
   rules; a future `agy` release could add a new customization root name. The
   SHA-256 build gate (S5 / #528 C3) prevents silently running an unmeasured
   release.
4. **Live API gap:** real Gemini API error payloads, real quota/entitlement
   denials, and real model tool-calling behavior remain unverified until #528
   C2's bounded live smoke test runs.

---

## 10. Recommendation: CONDITIONAL GO (deliverable 6)

**Proposed decision:** **CONDITIONAL GO** to proceed to EPIC #527 **G1
(Architecture decision)** for a Linux-only, `sandbox.mode: required`,
`GEMINI_API_KEY`-only `antigravity` provider, **subject to all five conditions
below** (and #528's C2/C3). If any condition is declined, the verdict is
**NO-GO**.

- **S1 (Blocker — Maintainer decision required):** Accept the pre-launch
  refusal of `.agents`, `.agent`, `_agents`, and `_agent` (at `cwd`, any
  ancestor, or any descendant) combined with a fresh per-launch empty `HOME`
  and fixed argv (no `--add-dir`, no `extra_args`) as the MVP substitute for
  a missing CLI hooks/MCP switch — **knowing** that repositories committing
  those directories cannot run `agy`. If a native CLI switch is required
  instead, this spike's verdict is **NO-GO** until `agy` ships one.
- **S2 (Auth scope):** Support `GEMINI_API_KEY` with
  `--settings '{"modelProvider":"gemini"}'` ONLY; explicitly refuse Google
  OAuth sign-in and `AGY_ADC_AUTH` / `GOOGLE_APPLICATION_CREDENTIALS`, and
  add only `("GEMINI_API_KEY",)` to `environment_names`.
- **S3 (Sandbox & permissions):** Require `sandbox.mode: required` (Linux
  `bwrap` + Landlock) for unattended `agy` launches; mark `disabled`
  unattended and macOS unsupported; pin `toolPermission` explicitly in
  `--settings` rather than relying on CLI defaults or `--mode`.
- **S4 (Redaction):** Before shipping the adapter, add `GEMINI_API_KEY`,
  `GOOGLE_API_KEY`, and `ANTIGRAVITY_CSRF_TOKEN` to `src/autoforge/redaction.py`
  (and evaluate Google OAuth token patterns `ya29.`, `1//`, `GOCSPX-` under
  `MAX_GROWTH_FACTOR = 3`).
- **S5 (Build gate & read-only doctor):** Gate launches by executable SHA-256
  (#528 C3), and restrict `doctor` to zero-write commands (`--version`,
  `--help`, filesystem customization scan, env-var presence check), reporting
  live model/entitlement access as `SKIP`.

### Suggested follow-up work (for G1 / G2, only after G0 sign-off)

1. **G1 architecture decision record** reconciling #528 and #529 (transport,
   duplex byte-level CR/UTF-8 handling, `R5-F2`/`R5-F7`, S1–S5 policy,
   supported-build table).
2. **Redaction hardening PR** (`src/autoforge/redaction.py`: `GEMINI_API_KEY`,
   `GOOGLE_API_KEY`, `ANTIGRAVITY_CSRF_TOKEN`, and Google OAuth shapes;
   coordinated with #285).
3. **Antigravity provider MVP PRs** (stream reducer `agy_stream.py`;
   `AntigravityProvider` + `config.py` + pre-launch SHA-256/customization/prompt
   gates + `sandbox_requirements` / `credential_routes`; `doctor.py` rows;
   offline fake + opt-in live smoke test for #528 C2; operator docs and
   `docs/compatibility.md`).
