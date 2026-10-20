"""``autoforge`` in a process of its own, for the ``epic`` crash tests (#183).

``python -P tests/epic_cli.py SPEC`` runs the production
:func:`autoforge.cli.main` on ``SPEC["argv"]`` in ``SPEC["cwd"]``, offline:
the EPIC's graph is the test's :class:`tests.test_scheduler.Graph`,
unpickled from ``SPEC["graph"]`` behind the real read-only client, and the
scheduler starts its workers with ``SPEC["worker_command"]``
(``tests/scheduler_worker.py``). A test kills this process, never the one
it runs in, so a scheduler (or ``epic archive``) dies with SIGKILL at the
exact point of an ADR 0008 D14.5 crash window while its workers live on.

Each entry of ``SPEC["crashes"]`` arms one SIGKILL of this process:

- ``target``: ``module:attribute`` (``autoforge.scheduler:open_claim``,
  ``autoforge.scheduler:EpicScheduler._reap``), patched where it is looked
  up; ``spawn`` is the scheduler's worker spawn;
- ``when``: ``before`` or ``after`` the real call;
- ``marker``: written first; an existing marker disarms the crash, so it
  fires once across the scheduler's re-runs;
- ``if_exists`` (optional): fire only once this file exists (a worker's
  ``started`` marker);
- ``match`` (optional): fire only when an argument's text is this one
  (``state.json`` for the archive's unlink).
"""

from __future__ import annotations

import importlib
import json
import os
import pickle
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # ``-P``: the tests package is imported from here only


def _armed(crash: dict, args: tuple) -> bool:
    if os.path.exists(crash["marker"]):
        return False
    if crash.get("if_exists") and not os.path.exists(crash["if_exists"]):
        return False
    if "match" in crash and crash["match"] not in [str(arg) for arg in args]:
        return False
    return True


def _crashing(real, crash: dict):
    def call(*args, **kwargs):
        if crash["when"] == "before" and _armed(crash, args):
            Path(crash["marker"]).write_text("before\n", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGKILL)
        result = real(*args, **kwargs)
        if crash["when"] == "after" and _armed(crash, args):
            Path(crash["marker"]).write_text("after\n", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGKILL)
        return result

    return call


def _arm(crash: dict, options: dict) -> None:
    if crash["target"] == "spawn":
        from autoforge.executor_supervised import spawn_supervised

        options["spawn"] = _crashing(spawn_supervised, crash)
        return
    module_name, _, attribute = crash["target"].partition(":")
    owner: object = importlib.import_module(module_name)
    *path, name = attribute.split(".")
    for part in path:
        owner = getattr(owner, part)
    setattr(owner, name, _crashing(getattr(owner, name), crash))


def main(argv: list[str]) -> int:
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    from autoforge import cli

    with open(spec["graph"], "rb") as f:
        graph = pickle.load(f)
    options: dict = {"worker_command": tuple(spec["worker_command"]), "tick_seconds": 0.02}
    for crash in spec.get("crashes", ()):
        _arm(crash, options)
    cli._epic_graph = lambda cfg: graph.client()  # type: ignore[assignment]
    cli._epic_scheduler_options = lambda: dict(options)  # type: ignore[assignment]
    os.chdir(spec["cwd"])
    return cli.main(spec["argv"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
