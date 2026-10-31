# Pi: running phases on DeepSeek or a ChatGPT subscription

This page is the operator's guide to `provider: pi`: how to run any
AutoForge phase through [Pi](https://pi.dev/docs/latest), an optional
multi-model provider. Its first target is the **DeepSeek API**, on an API
key you give Pi yourself ([DeepSeek on an API key](#deepseek-on-an-api-key)).
The **ChatGPT subscription** you signed in to Pi with stays supported, with
**no OpenAI Platform API key anywhere in AutoForge's environment**. The page
covers installing Pi, giving it a credential, a profile to copy for each
route, the choices a profile makes, `doctor`'s Pi rows and what each
failure means, the security model, the limitations, and the opt-in smoke
test that proves the setup on your machine.

The decisions behind it, with their evidence, are in
[ADR 0003](adr/0003-pi-agent-provider.md) (the RPC adapter) and the
[Pi policy](pi-policy.md) (what an unattended Pi run may load, its tools
and credentials). Every Pi key is listed under
[Configuration: Pi profiles](configuration.md#pi-profiles).

Nothing on this page changes a default: every profile stays on Claude Code
or OpenCode until you route it to Pi.

## Install Pi yourself

Install Pi from its own instructions (the npm package
`@earendil-works/pi-coding-agent`; see [Pi's docs](https://pi.dev/docs/latest)).
AutoForge needs **Pi 1.0.0 or newer**; its behaviour was verified on Pi
1.0.1. `autoforge doctor` checks the version of the `command` each Pi
profile names (`pi` on `PATH` by default) and fails below the minimum.

AutoForge never installs, updates or configures Pi, and every launch passes
`--offline`, so Pi does not install or update packages at startup either
([Pi policy](pi-policy.md) §3). Pi runs on POSIX systems only, like
AutoForge's executor.

## DeepSeek on an API key

A DeepSeek profile authenticates with an API key, so it sets
`require_oauth: false`:

<!-- pi-guide-deepseek-example -->
```yaml
profiles:
  review_round_1:
    provider: pi
    model: deepseek/deepseek-flash   # exact id, as Pi's model list shows it
    effort: low                      # deepseek-flash: low, high or max (no clamp)
    command: pi
    idle_timeout_seconds: 900
    max_runtime_seconds: 3600
    options:
      require_oauth: false           # an API key, not the ChatGPT sign-in
```
<!-- /pi-guide-deepseek-example -->

`tests/test_config.py` loads and validates this block too. Use the id
Pi's own model list shows: `deepseek/deepseek-flash` is the id in Pi
1.3.0's DeepSeek catalog, where the retired `deepseek/deepseek-v4-flash`
alias no longer resolves (Pi's auth check answers it `status invalid`,
reason `invalid_state`). The effort must be one the model supports:
`deepseek-flash` takes `low`, `high` and `max`, and AutoForge fails a
level Pi would clamp ([Provider and model](#provider-model-and-thinking-level)).

**Where the key lives.** Either place works; the first is preferred:

- **In Pi's own credential store.** Give the key to Pi itself, as
  [Pi's docs](https://pi.dev/docs/latest) describe for an API-key
  provider. Pi keeps it in `auth.json` in its agent directory, which
  AutoForge never reads, copies or prints, and uses a stored credential
  before an environment variable. No variable is forwarded, so the key
  reaches no other agent.
- **As `DEEPSEEK_API_KEY`, forwarded on purpose.** No provider key reaches
  Pi by default. Listing the name in `execution.env_allowlist_extra`
  forwards it from the shell that runs `autoforge`:

  ```yaml
  execution:
    env_allowlist_extra: [DEEPSEEK_API_KEY]
  ```

  That list is **global**: it reaches every agent launch, of every
  profile and every provider (Claude Code and OpenCode too), and through
  Pi's `bash` tool every command Pi runs. It is **not a per-profile
  credential boundary**, which is why the store is preferred.

**Under the outer sandbox.** With `sandbox.mode: required`, or `preferred`
(its alias), the sandbox hides your home and refuses an agent directory
that is, contains or lies inside `~/.pi/agent`
([Pi policy](pi-policy.md) §8). A sandboxed Pi launch therefore needs a
**dedicated `PI_CODING_AGENT_DIR` holding its own credential**: export
`PI_CODING_AGENT_DIR=/absolute/path` in the shell that runs `autoforge`,
and give Pi the key once with that variable set. Run `doctor` with it set
as well: its auth row runs outside the sandbox, so without the variable it
checks `~/.pi/agent` and can report `ready` for a credential a sandboxed
launch cannot see. A forwarded `DEEPSEEK_API_KEY` is an ordinary
allow-listed variable and passes into the sandbox too. Neither sandboxed
route has been run against the real DeepSeek API yet.

**What is checked, and when.** `doctor`'s auth row passes on
`status ready` with `authType api_key` for provider `deepseek`. With
`require_oauth: false` a launch runs no auth check of its own (that check
enforces the ChatGPT sign-in), so a key that is missing or no longer valid
fails the phase with Pi's provider failure
(`pi: prompt rejected before acceptance`, or
`pi: model error after acceptance`). DeepSeek bills
the account the key belongs to, and AutoForge cannot check its balance or
the model's availability before a run: the first real run is the proof.

## Sign in yourself

For the ChatGPT subscription, the sign-in is yours, made once, in Pi:

```text
pi                      # start Pi interactively, in any directory
/login openai           # choose "Sign in with ChatGPT"
```

`/login openai-codex` signs in to the legacy `openai-codex` provider
instead; Pi's own changelog says the `openai` sign-in supersedes it. Follow
[Pi's docs](https://pi.dev/docs/latest) for the login screens themselves.

AutoForge never initiates a login, never reads, copies or prints Pi's
credential store (`auth.json` in Pi's agent directory), and never refreshes
the sign-in outside Pi:

- `doctor` and the check before every launch run
  `pi auth check --model M --json --no-refresh`. That is Pi's own read-only
  check: AutoForge reads only its four result fields
  (`status`, `provider`, `reason`, `authType`) and never quotes Pi's output.
  `--credentials`, which would print the credential, is never passed.
- During a run, Pi itself refreshes its access token when a model request
  needs it, as it does when you use Pi directly.

Pi's agent directory is `$PI_CODING_AGENT_DIR` when that variable is set in
the shell that runs `autoforge` (the launch passes it on), otherwise
`~/.pi/agent`. Under `sandbox.mode: required` the sandbox hides your home,
so give AutoForge an agent directory of its own: export
`PI_CODING_AGENT_DIR=/absolute/path`, then run `pi` and `/login openai`
once with it set ([Pi policy](pi-policy.md) §8).

## A profile to copy

Route any profile to Pi. This one puts the first reviewers on it with the
ChatGPT sign-in (the DeepSeek one is under
[DeepSeek on an API key](#deepseek-on-an-api-key)); the same shape works
for `analyze_execute`, `fix` and every other profile:

<!-- pi-guide-example -->
```yaml
profiles:
  review_round_1:
    provider: pi
    model: openai/gpt-5.6-terra   # <pi-provider>/<model-id>, exact, no :<thinking>
    effort: high                  # Pi's --thinking: off|minimal|low|medium|high|xhigh|max
    command: pi
    idle_timeout_seconds: 900     # killed after 15 minutes without output
    max_runtime_seconds: 3600     # and after one hour in any case
    options:
      require_oauth: true         # the ChatGPT sign-in, never an API key (the default)
  review_round_2_5:
    provider: pi
    model: openai/gpt-5.6-terra
    effort: high
    command: pi
    idle_timeout_seconds: 900
    max_runtime_seconds: 3600
    options:
      require_oauth: true
```
<!-- /pi-guide-example -->

`tests/test_config.py` loads and validates this block. A Pi profile
takes no `extra_args`: AutoForge owns the whole Pi argv. `options.tools`
narrows Pi's tools (reviewers default to `read,bash`, the implementation
phases to `read,bash,edit,write`) and `options.context_files: false` turns
off `AGENTS.md` / `CLAUDE.md`
([Configuration: Pi profiles](configuration.md#pi-profiles)). The two
limits replace the removed `timeout_seconds`. `autoforge.example.yaml`
carries the same profile, commented, for `review_round_2_5`.

## Provider, model and thinking level

- **`<pi-provider>/<model-id>`.** `model` names Pi's provider and the
  model's id, with exactly one `/`: `deepseek/<id>` uses a DeepSeek API
  key ([DeepSeek on an API key](#deepseek-on-an-api-key)), `openai/<id>`
  the ChatGPT sign-in, `openai-codex/<id>` the legacy one. Other Pi
  providers are allowed, but they are outside this guide and, like
  DeepSeek, need `require_oauth: false` when they authenticate with an API
  key.
- **Use exact ids.** Pi resolves `--model` by exact id, then by a fuzzy
  match, and can even fall back to a placeholder for an id it does not
  know. Before sending the prompt AutoForge reads back what Pi resolved and
  fails the phase unless it is exactly the configured provider and id, and
  among Pi's available models: `pi: model mismatch` or
  `pi: model unavailable`. Copy the id from Pi's own model list.
- **No `:<thinking>` suffix.** Pi accepts `provider/id:high`; AutoForge
  refuses it when it validates the profile (`doctor`'s config row, and the
  start of every run), because the thinking level comes from `effort`
  alone. Two sources for one value could disagree.
- **`effort` is Pi's thinking level** (`--thinking`), and it is required.
  Pi silently clamps a level the model does not support (a non-reasoning
  model runs at `off`). AutoForge fails closed instead: it reads the level
  back before the prompt and fails the phase with
  `pi: thinking mismatch: configured high, Pi resolved medium`. Set a level
  the model supports.
- **Model availability is your account's.** `openai/gpt-5.6-terra` is an
  example from Pi's catalog for the `openai` provider, and
  `deepseek/deepseek-flash` one for `deepseek`, not a promise: which models
  a subscription or an API account may use depends on the plan and can
  change at any time.

## Subscription or API key

- **Pi prefers a stored sign-in.** For a provider you are signed in to, Pi
  uses the sign-in even when `OPENAI_API_KEY` is exported in its
  environment; the key is consulted only when nothing is stored.
- **AutoForge does not hand Pi a provider key.** No `OPENAI_*`,
  `DEEPSEEK_*`, `ANTHROPIC_*`, `GEMINI_*` or `GOOGLE_*` variable reaches Pi
  unless you list it in `execution.env_allowlist_extra`, a list every
  agent launch gets ([Pi policy](pi-policy.md) §8).
- **`require_oauth: true`** (the default) makes it a rule. `doctor`'s auth
  row fails, and every launch is refused before anything starts, when
  `OPENAI_API_KEY` would reach Pi. Every launch first runs Pi's auth check
  and starts Pi only on a `ready` result with `authType oauth` for the
  model's provider. To run on an API key instead, set
  `require_oauth: false` and keep the key in Pi's credential store, or
  forward it with `execution.env_allowlist_extra`
  ([DeepSeek on an API key](#deepseek-on-an-api-key)).
- **They are different products.** The ChatGPT subscription and the OpenAI
  API are separate products with their own terms, usage limits and model
  availability. Subscription usage limits apply to AutoForge's runs as to
  your own use of Pi: a run that hits one fails with
  `pi: subscription usage limit reached`, which is not retried. A plan or a
  model may not be available to every account. **AutoForge cannot check
  entitlement before a run**: a `ready` sign-in shows that Pi holds a
  credential for the provider, not that this model may be used with it.
  The first real run is the proof.

## Doctor's Pi rows

`autoforge doctor` adds these rows for the Pi profiles the run can reach.
Every probe runs under the environment an agent launch gets, never your
whole shell, and none prints Pi's output.

| Row | Result | Meaning and remedy |
|---|---|---|
| `agent 'pi' available (...)` | FAIL: not found, exited, or the version is unreadable | Pi is not installed under the profile's `command`, or does not answer `--version` as 1.x does. Install it ([Install](#install-pi-yourself)), or point `command` at it. |
| | FAIL: `pi >= 1.0.0 is required` | Upgrade Pi. |
| `agent 'pi' auth ready (...)` | OK: `status ready`, `authType oauth`, or `authType api_key` with `require_oauth: false` | Pi holds a credential: the sign-in, or an API key ([DeepSeek on an API key](#deepseek-on-an-api-key)). The detail adds that the credential is proven for the provider only, and that whether it may use this model "cannot be checked read-only": model access is unproven until a run ([Subscription or API key](#subscription-or-api-key)). |
| | FAIL: `OPENAI_API_KEY is set and would reach Pi through the environment allow-list` | The key is forwarded by `execution.env_allowlist_extra` while the profile has `require_oauth`. Remove it there, or set `require_oauth: false`. Pi was not asked. |
| | FAIL: `status ready`, `authType api_key` | Pi has an API key, not a sign-in, for the provider. Sign in ([Sign in](#sign-in-yourself)), or set `require_oauth: false`. |
| | FAIL: `reason credentials_not_configured` | Pi has no credential for the provider. Sign in ([Sign in](#sign-in-yourself)), or for an API-key provider give Pi the key or forward it ([DeepSeek on an API key](#deepseek-on-an-api-key)), in the agent directory the launch uses (`PI_CODING_AGENT_DIR`, or `~/.pi/agent`). |
| | FAIL: `reason provider_not_found` | The provider prefix of `model` is not one of Pi's providers. Fix `model` ([Provider and model](#provider-model-and-thinking-level)). |
| | FAIL: `reason credential_not_available` | Pi holds a sign-in it cannot use as stored. Run the check yourself (`pi auth check --model <model> --json --no-refresh`) and sign in again if it persists. What this read-only check reports for a sign-in whose access token has expired is not verified yet ([#129](https://github.com/iceboundrock/AutoForge/issues/129)); the [live smoke test](#prove-it-on-your-machine-the-live-smoke-test)'s report records what doctor printed. |
| | FAIL: `reason invalid_state` | Pi finds its model or credential configuration invalid; Pi 1.3.0 answers so for a model id it does not resolve (a retired alias). Run the same check yourself to see Pi's own message, and copy the id from Pi's model list. |
| | FAIL: `Pi resolved model ... to provider ..., not the configured ...` | Pi maps the model to another provider. Use the exact id ([Provider and model](#provider-model-and-thinking-level)). |
| | FAIL: `exited N without a result AutoForge can read` | The output did not have Pi 1.x's shape: a wrapper printing extra lines, or a Pi whose contract changed. AutoForge fails closed. |
| `pi loads no instructions from outside the checkout (...)` | WARN, naming the files | `SYSTEM.md` / `APPEND_SYSTEM.md` in Pi's agent directory (always loaded), or context files above the checkout. Remove or move them, or set `context_files: false` for the context files ([Pi policy](pi-policy.md) §4-5). |
| `pi's detached tool processes are contained (...)` | OK on Linux, WARN elsewhere | Whether a process a Pi tool command leaves running is killed when the launch ends: OK only where the controller can make itself a child subreaper, as each launch does. It warns on a Linux host whose security policy (a seccomp filter, say) denies that too. The WARN means: do not run Pi unattended here ([Unattended use](#unattended-use)). |

The same auth check runs before every launch. When it refuses, the phase
fails with `pi: the auth preflight refused the launch: ...` and the same
fields and remedies, Pi is never started, and the run's state is
unchanged: fix the cause and `resume`.

## Security and trust

The [Pi policy](pi-policy.md) is authoritative. In short:

- **What Pi loads, and what AutoForge turns off.** Every launch is
  `pi --mode rpc --no-session --no-approve --no-extensions --no-skills
  --no-prompt-templates --no-themes --offline --tools <list>`: no project
  trust decision you saved, no extension (so no MCP server), package,
  skill, prompt template or theme, nothing installed at startup, and an
  explicit tool list. Context files (`AGENTS.md`, `CLAUDE.md`) stay on by
  default, as for the other providers; the agent directory's `SYSTEM.md`
  and `APPEND_SYSTEM.md` always load, which `doctor` warns about.
- **Pi's tools run as you, unconfined.** Pi has no approval or permission
  layer: a Pi profile is like Claude Code under `bypassPermissions`. Under
  the default `sandbox.mode: disabled` its tools run with your UID and your
  files, as every provider's do
  ([Security boundaries: agents run as you](security.md#agents-run-as-you)):
  run unattended phases in a container, a VM or under a dedicated user, or
  set `sandbox.mode: required` on Linux
  ([Configuration: outer sandbox](configuration.md#outer-sandbox)). A tool
  command can read `auth.json` in Pi's agent directory, which Pi needs for
  its own credential, and the value of any key you forward; it gets no
  GitHub or git credential.
- **Detached tool processes.** Pi's `bash` tool starts every command
  detached, so whatever a command backgrounds outlives Pi itself. The
  decision ([ADR 0002](adr/0002-executor-nothing-outlives-the-invocation.md)
  §4b, #132's option (a)): on Linux the controller is a child subreaper for
  each launch, finds those processes and kills them when the launch ends,
  and reports it (`orphans_killed`, or `orphan_survived_kill` if one
  survives SIGKILL) in the step's `execution.json` and the leftovers
  sentence. Its limits: Linux only (elsewhere every result carries
  `orphans_unchecked` and says a process may still be running), and a
  process that makes itself a subreaper or is handed to a service manager,
  `at`, a container runtime or an SSH host still escapes.
- **Run logs.** Run logs, progress lines and block reasons are redacted:
  tokens, API keys, `Authorization` headers, JWTs and the OAuth fields of
  Pi's credential shape (`refresh`, `access`, `*_token`,
  `chatgpt-account-id`) are masked ([Pi policy](pi-policy.md) §9;
  redaction is a baseline, with no claim of completeness). Pi's failure
  text is bounded and redacted before it is kept, and the auth check's
  output is never kept at all. `request.json` lists the environment by
  name only.
- **Never run unattended on an untrusted repository.** The repository's
  content reaches the model as task data, and an agent with `bash` can do
  whatever your user can.

## Limitations

- **RPC only.** Every launch is one fresh `pi --mode rpc` process with one
  prompt and a session that is never saved (`--no-session`).
- **Corrections are new processes.** A malformed `CONTROL_RESULT` is
  corrected by a fresh Pi process with the correction prompt, never by
  continuing a hidden conversation.
- **No pause or resume for questions.** Pi cannot ask you something
  mid-run: the interaction contract (#134) has no Pi adoption yet, so a
  dialog is cancelled and the phase fails.
- **Interactive Pi extensions are disabled**, with every other extension.
- **POSIX only**, like the executor.
- **Pinned to Pi 1.0.0 or newer.** A newer Pi that changes the RPC or the
  auth check's contract fails closed (`pi: protocol violation`, an
  unreadable auth result) rather than being guessed at.

## Unattended use

AutoForge as a whole does not claim unattended production operation yet
([README](../README.md#current-maturity)). For Pi the decision is the one
ADR 0002 §4b and the [Pi policy](pi-policy.md) §10 record:

- **On Linux**, Pi meets the same bar as the other providers: what its
  tools leave running is killed at the end of each launch, and an
  unattended Pi run is supported under the conditions every unattended run
  needs: a container, a VM, a dedicated user or `sandbox.mode: required`,
  and a repository you trust.
- **On every other platform**, and on a Linux host whose security policy
  denies the controller the child subreaper (no child subreaper either
  way), Pi is **attended only**. A process a Pi tool command backgrounds
  can outlive the launch; every result says so (`orphans_unchecked`),
  and `doctor` warns. The prompt rule asking agents not to leave
  processes running is then the only mitigation.

## Prove it on your machine: the live smoke test

`tests/test_live_pi.py::test_one_remote_review_runs_on_the_operators_real_pi`
is an opt-in test that runs one real REVIEW through your Pi and your
credential, on a REMOTE run against a GitHub stand-in that never reaches
GitHub ([The GitHub stand-in](#the-github-stand-in)). It does not run in
`make check` or CI: the `live` marker deselects it unless you pass
`-m live`, and it then skips unless `AUTOFORGE_LIVE_PI=1`. You name the
auth route, which has no default, so a report always says which route it
proves:

- `AUTOFORGE_LIVE_PI_AUTH=api_key`: an API key, with
  `require_oauth: false`; the model defaults to `deepseek/deepseek-flash`.
  This is the route #135's acceptance run uses.
- `AUTOFORGE_LIVE_PI_AUTH=oauth`: the ChatGPT sign-in, with
  `require_oauth: true`; the model defaults to `openai/gpt-5.6-terra`. An
  optional regression run. A run on an API key is no evidence for this
  route, and until a report from this route is posted, the ChatGPT route
  is untested against a real subscription.

```bash
unset OPENAI_API_KEY DEEPSEEK_API_KEY  # the test fails, and does not unset them for you

# DeepSeek, its key in Pi's own credential store
AUTOFORGE_LIVE_PI=1 AUTOFORGE_LIVE_PI_AUTH=api_key uv run pytest -m live \
  tests/test_live_pi.py::test_one_remote_review_runs_on_the_operators_real_pi

# the ChatGPT sign-in (once before: `pi`, then /login openai)
AUTOFORGE_LIVE_PI=1 AUTOFORGE_LIVE_PI_AUTH=oauth uv run pytest -m live \
  tests/test_live_pi.py::test_one_remote_review_runs_on_the_operators_real_pi
```

To forward the key instead of keeping it in Pi's store, export it and add
`AUTOFORGE_LIVE_PI_FORWARD_KEY=DEEPSEEK_API_KEY` (the `api_key` route
only): the test's configuration then lists that name in
`execution.env_allowlist_extra`, and the report names the route, never the
value.

The test costs one small REVIEW of the account's quota (a few-line diff, at
thinking level `low`). `AUTOFORGE_LIVE_PI_MODEL` and
`AUTOFORGE_LIVE_PI_EFFORT` (default `low`) choose another model. In a
temporary directory the test:

1. checks, and never performs, the preconditions: `pi` on `PATH`, at least
   1.0.0, and Pi's read-only `pi auth check --model M --json --no-refresh`
   reporting `ready` for the model's provider with the route's `authType`
   (`api_key` or `oauth`); Pi 1.3.0 answers a model id it does not
   resolve with `status invalid`, which fails here;
2. checks that no provider key (`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`) is in
   its environment, so the credential reaches Pi through Pi's own store,
   except a key forwarded on purpose, which must then be set;
3. runs `autoforge doctor` with a configuration whose reviewers are Pi with
   the route's `require_oauth`, and checks the "available" and
   "auth ready" rows, with the route's `authType`. Its rows that need
   GitHub (`gh available`, `gh authenticated`, the default branch's
   required checks, the check runs read) fail or are skipped, as they must
   without your login: the configuration's `github.command` is the
   stand-in's `gh`, which answers nothing. The report lists them apart
   from any other failure, and only the Pi rows decide the step;
4. drives a REMOTE run on a throwaway repository: a scripted stand-in
   commits a one-line feature in the issue's worktree, the controller
   pushes it and opens the PR on the stand-in, then **one REVIEW runs on
   Pi** in that worktree, of the PR's HEAD, through AutoForge's own Pi
   adapter (no correction relaunch), and must return a valid
   `CONTROL_RESULT`; the launch fails with `pi: model unavailable` if the
   model is not among Pi's available ones. The controller posts the review
   comment itself. A clean review leaves the run at `READY_FOR_MERGE`, and
   nothing is merged: the merge gate stays closed;
5. scans everything AutoForge wrote (the run's state directory, which this
   test keeps outside the checkout, with its state, journal, run logs and
   each step's execution records; the git directory's `autoforge/`, with
   the lock and the issue's worktree; every request the controller sent to
   the stand-in, the PR's title and body and the review comment among
   them; doctor's output) for JWTs, `Bearer`, `refresh_token`,
   `"refresh"`, `"access"`, `chatgpt-account-id` / `chatgpt_account_id`
   and `sk-` keys (DeepSeek's keys have that shape), and for the forwarded
   key's own value, naming only the file and the pattern;
6. checks that the review left the checkout, the issue's worktree and the
   worktree's HEAD unchanged, created no entry in Pi's session directory
   (`<agent dir>/sessions`, compared by name) and left no process running
   in or naming its directory;
7. prints a report: the date, the AutoForge commit under test (and whether
   its tracked files differ from it), the auth route, the `authType` Pi
   resolved and where the credential lives, Pi's version, the model and
   thinking level (as configured and as Pi resolved them), the review's
   duration and outcome, doctor's Pi rows, what the controller wrote to
   the stand-in, and each step's PASS or FAIL.

A failed review says who refused it: `provider refused (<reason>)` (the
credential, a usage limit or the account's balance, the model's
availability) or `AutoForge failure (<reason>)`, with Pi's failure
categories and never their text. The test never starts `/login` or Pi
interactively, never opens a file in Pi's agent directory, never passes
`--credentials` or a `print-*` subcommand, and prints no key, token,
account id, email or plan name: its report is the evidence to post on the
issue. Run it on the commit under review, so that the report's commit is
the one reviewed.

A REMOTE review is bound to the PR's HEAD commit, not to the files in its
worktree, so the controller accepts a review whose reviewer wrote into the
worktree without committing: step 4 passes, and step 6 is what reports it.

**More evidence, by hand.** To see the write phases too, route
`analyze_execute` and `fix` to Pi as well and run `autoforge run` on an
issue of a throwaway repository you create for it. A full run spends
several model calls. Check that the run reaches `READY_FOR_MERGE` or a
review's findings, that each step's `execution.json` shows the model Pi
resolved, and that the PR's diff holds only the implementation.

### The GitHub stand-in

The stand-in is the test suite's in-memory fake of GitHub, its git data in
a bare repository in the test's temporary directory, which the
controller's own git transport pushes to over `file://`. Nothing reaches
GitHub, and the run needs no `gh` login. The test seeds that repository
itself, over no git transport and with none of your git configuration or
`GIT_*` variables, so a URL rewrite (`insteadOf`) in your `~/.gitconfig`
cannot send the seed anywhere else. Its other git (the checkout's
`origin`, the scripted implementation's commit, the HEADs it reports)
ignores your `GIT_*` variables too, so a `GIT_DIR` set in your shell
cannot make it write to another checkout, and it runs none of your git
hooks. A throwaway repository on github.com would exercise GitHub too, but
it would need your login and the network, and leave a branch, a PR and a
comment to clean up, while what this test proves is Pi on a REMOTE review.
The cost: the run proves nothing about `gh` or GitHub's API, and what the
controller sends to GitHub is checked against the fake's model of it.
