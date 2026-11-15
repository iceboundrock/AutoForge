# The `claude-sdk` helper environment

The `claude-sdk` provider runs its helper program
(`src/autoforge/sdk_helpers/claude_helper.py`) in a Python environment of
its own, never in AutoForge's (ADR 0007 D2.3). This directory pins that
environment: `claude-agent-sdk` 0.2.163, whose platform wheel bundles
Claude Code 2.1.286, and every dependency, each with its hashes.

- `requirements.in` names the one direct requirement.
- `requirements.txt` is the lock, compiled for every Python from 3.10 on and
  every platform:

  ```sh
  uv pip compile requirements.in --generate-hashes --python-version 3.10 \
      --universal --no-header -o requirements.txt
  ```

## Install

Make the environment outside the AutoForge checkout, on Python 3.10 or
newer, and install the lock from wheels only, hashes required and nothing
resolved:

```sh
python3 -m venv ~/.local/share/autoforge/claude-sdk
~/.local/share/autoforge/claude-sdk/bin/python -m pip install \
    --require-hashes --only-binary :all: --no-deps \
    -r helpers/claude-sdk/requirements.txt
```

`uv pip install --python ~/.local/share/autoforge/claude-sdk/bin/python
--require-hashes --only-binary :all: --no-deps -r
helpers/claude-sdk/requirements.txt` does the same.

Wheels only, because the 0.2.163 sdist carries no Claude Code: building it
would leave the helper without the pinned runtime (D2.4). The wheels are
platform wheels for manylinux 2.17 (x86_64, aarch64) and macOS 11 (arm64,
x86_64); there is no wheel, and so no `claude-sdk` backend, on any other
platform.

Then name the environment's interpreter as the profile's `command`, by
its absolute path. AutoForge runs `command` as written, with no shell, so
a `~` or a `$HOME` in it is not expanded and the launch would fail with
`executable not found`. For a user `you` on Linux (`/Users/you` on macOS):

```yaml
profiles:
  fix:
    provider: claude-sdk
    model: claude-opus-5-5
    command: /home/you/.local/share/autoforge/claude-sdk/bin/python
```

`echo ~/.local/share/autoforge/claude-sdk/bin/python` prints the path to
write there.

`autoforge doctor` checks the environment against the pinned pair without
running anything that writes (docs/configuration.md). A launch checks the
pair again from the helper's `hello`, and refuses any other.

## Changing the pair

The pair moves only by an amendment of ADR 0007 (D4.6): update
`requirements.in` and the lock, `src/autoforge/sdk_helpers/manifest.json`,
and re-run the gate probes on the new pair
(`tests/test_claude_sdk_probe.py`, with
`AUTOFORGE_CLAUDE_SDK_PROBE_PYTHON` naming the new environment's
interpreter), recording their results in the ADR.
