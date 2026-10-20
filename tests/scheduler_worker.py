"""The worker process of the scheduler tests (#182): the production ``run_worker``, offline.

``python -P tests/scheduler_worker.py FIXTURES epic _worker ARGS...`` is the
``worker_command`` a test gives the scheduler, so each worker is a real
subprocess started through ``spawn_supervised`` with the inherited L0 and
L2 descriptors, exactly as ``autoforge epic _worker`` is. Before it calls
:func:`autoforge.worker.run_worker` it does in its own process what
``tests/conftest.py``'s autouse fixtures do in the test's (an empty operator
home and agent configuration, a sandbox probe that answers without a
launch) and builds what the engine talks to: a :class:`FakeGitHub` seeded
from ``FIXTURES/<issue>.json``, with the test's bare origin as its git data
and the engine's git remote, and a :class:`ScriptedProvider` playing the
fixture's ``steps``, one per agent launch. Each worker writes only its own
issue's PR, so one FakeGitHub per worker is faithful (#182's harness).

A step is ``{"do": ...}``:

- ``implement``: one commit in the agent's worktree, reported as success;
- ``review``: a clean review of the PR head, of round ``round`` (1 by default);
- ``blocked``: the agent reports the issue blocked;
- ``garbage``: no ``CONTROL_RESULT`` at all (a failing provider);
- ``hold``: write ``started``, then wait for ``gate``; with ``stubborn`` the
  first ``KeyboardInterrupt`` (the scheduler's first SIGINT) is noted in
  ``interrupted`` and outlived, the second is not.

``started`` and ``gate`` may be given on any step: the step first creates
``started`` and waits for ``gate`` to exist. Synchronization is by files.
With ``"real_claude": true`` no provider is replaced for ``claude``: its
profiles run the real adapter on the command the configuration names.

``"world": PATH`` (#183) makes the issue's GitHub outlive its worker: the
FakeGitHub is unpickled from ``PATH`` when it exists and pickled back after
every call the engine makes, so the next worker of the issue (a resume, a
re-admission after ``epic archive``) sees the PRs and comments the last one
left, and a test counts them. ``"gh_crash": {"method": ..., "marker": ...}``
kills the worker with SIGKILL right after that call's effect lands and is
saved, once (``marker`` records it): a worker killed between a GitHub write
and the state that records it (ADR 0008 D14.5).
"""

from __future__ import annotations

import json
import os
import pickle
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # ``-P``: the tests package is imported from here only

GATE_SECONDS = 120.0


def _touch(path: str | None) -> None:
    if path:
        Path(path).write_text(f"{os.getpid()}\n", encoding="utf-8")


def _wait_for(path: str | None, *, stubborn: bool = False, noted: str | None = None) -> None:
    if not path:
        return
    deadline = time.monotonic() + GATE_SECONDS
    interrupted = False
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise SystemExit(f"scheduler_worker: the gate {path} never opened")
        try:
            time.sleep(0.02)
        except KeyboardInterrupt:
            if not stubborn or interrupted:
                raise
            interrupted = True
            _touch(noted)


class _World:
    """The issue's FakeGitHub, saved to ``path`` after every call the engine makes."""

    def __init__(self, inner: object, path: str, crash: dict | None) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_crash", crash or {})

    def save(self) -> None:
        tmp = f"{self._path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            pickle.dump(self._inner, f)
        os.replace(tmp, self._path)

    def __getattr__(self, name: str):
        value = getattr(self._inner, name)
        if name.startswith("_") or not callable(value):
            return value

        def call(*args, **kwargs):
            try:
                return value(*args, **kwargs)
            finally:
                self.save()
                crash = self._crash
                if crash.get("method") == name and not os.path.exists(crash["marker"]):
                    _touch(crash["marker"])
                    os.kill(os.getpid(), signal.SIGKILL)

        return call

    def __setattr__(self, name: str, value: object) -> None:
        setattr(self._inner, name, value)


def main(argv: list[str]) -> int:
    fixtures = Path(argv[0])
    words = argv[1:]
    assert words[:2] == ["epic", "_worker"], words
    worker_args = words[2:]
    issue = int(worker_args[worker_args.index("--issue") + 1])
    fixture = json.loads((fixtures / f"{issue}.json").read_text(encoding="utf-8"))

    import autoforge.child_env as child_env
    import autoforge.engine as engine_module
    import autoforge.providers as providers_module
    from autoforge.engine import ControllerEngine
    from autoforge.providers import ProviderRegistry, ScriptedProvider
    from autoforge.sandbox_bwrap import ProbeResult
    from autoforge.worker import run_worker
    from tests.conftest import (
        GIT_IDENT,
        FakeGitHub,
        Origin,
        analyze_payload,
        block,
        connect_origin,
        git_out,
        review_result,
    )

    # What tests/conftest.py's autouse fixtures patch in the test's process.
    home = fixture["home"]
    child_env.operator_home = lambda: home  # type: ignore[assignment]
    data_dir = Path(fixture["opencode_data"])
    providers_module.opencode_data_dir = lambda env: data_dir  # type: ignore[assignment]
    os.environ.pop("OPENCODE_DB", None)
    claude_dir = Path(fixture["claude_config"])
    claude_files = [claude_dir / "settings.json", claude_dir / ".claude.json"]
    providers_module.claude_config_files = lambda env: claude_files  # type: ignore[assignment]
    engine_module.probe_sandbox = lambda: ProbeResult(True, "", "0.0-test")  # type: ignore[assignment]

    repository = fixture["repository"]
    url = f"https://github.com/{repository}/issues/{issue}"
    world = fixture.get("world")
    if world and os.path.exists(world):
        with open(world, "rb") as f:
            gh = pickle.load(f)
    else:
        gh = FakeGitHub(repository)
        gh.add_issue(url, fixture.get("title", f"Issue {issue}"))
    origin = Origin(Path(fixture["origin"]["bare"]), Path(fixture["origin"]["gh"]))
    gh.attach_origin(origin)
    client = _World(gh, world, fixture.get("gh_crash")) if world else gh
    real_init = ControllerEngine.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        connect_origin(self, origin)

    ControllerEngine.__init__ = init  # type: ignore[method-assign]

    steps = list(fixture["steps"])

    def agent(req) -> str:
        step = steps.pop(0) if steps else {"do": "garbage"}
        _touch(step.get("started"))
        _wait_for(step.get("gate"), stubborn=step.get("stubborn", False), noted=step.get("noted"))
        kind = step["do"]
        if kind == "implement":
            git_out("-C", req.cwd, *GIT_IDENT, "commit", "-q", "--allow-empty", "-m",
                    f"Implement the feature (#{issue})")  # fmt: skip
            head = git_out("-C", req.cwd, "rev-parse", "HEAD")
            return block(analyze_payload(head, issue_url=url))
        if kind == "review":
            (pr,) = gh.prs.values()
            return block(review_result(step.get("round", 1), pr.head_sha))
        if kind == "blocked":
            return block(
                {"phase": req.phase, "status": "blocked", "issue_url": url, "message": "stuck"}
            )
        if kind == "hold":
            return ""
        assert kind == "garbage", kind
        return "this agent never prints a control result"

    provider = ScriptedProvider(agent)
    overrides = {"opencode": provider}
    if not fixture.get("real_claude"):
        overrides["claude"] = provider
    return run_worker(
        worker_args,
        github_factory=lambda cfg: client,
        provider_factory=lambda cfg: ProviderRegistry(overrides=overrides),
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
