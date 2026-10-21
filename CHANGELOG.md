# Changelog

Notable changes for operators. A default changes only where an entry says
so.

## Unreleased

### Pi on a ChatGPT subscription (EPIC #127)

- `provider: pi` runs any phase through [Pi](docs/pi.md) 1.0.0 or newer on
  the ChatGPT sign-in you made in Pi yourself, with no OpenAI Platform API
  key in AutoForge's environment: the RPC adapter, read-only `doctor`
  checks and a per-launch auth check, and the unattended loading policy
  ([ADR 0003](docs/adr/0003-pi-agent-provider.md),
  [Pi policy](docs/pi-policy.md); #129 to #133). It is opt-in per profile:
  every default profile still runs Claude Code or OpenCode.
- The [Pi operator guide](docs/pi.md) (#135): installing Pi and signing in
  yourself, a profile to copy, model and thinking-level choices,
  subscription versus API key, each `doctor` Pi row with its remedy,
  security, LOCAL mode, limitations and unattended use.
- `doctor` gains a row for Pi profiles, `pi's detached tool processes are
  contained`: OK where the controller can make itself a child subreaper
  (Linux), a warning elsewhere, including a Linux host whose security
  policy denies it. There a process a Pi tool command backgrounds can
  outlive the launch and Pi is attended only (#135). A warning, never a
  failure.
- An opt-in live smoke test, `tests/test_live_pi.py` (#135): one LOCAL
  REVIEW through your own Pi and sign-in, then a credential scan of
  everything AutoForge wrote and checks for left-behind sessions and
  processes. It is marked `live` and deselected by default, so `make check`
  and CI do not run it; it needs `-m live` and `AUTOFORGE_LIVE_PI=1`
  ([Development](docs/development.md#the-live-pi-smoke-test)).
