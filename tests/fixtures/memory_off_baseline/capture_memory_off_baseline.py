"""Capture the Memory-off baseline in this directory from the release before #333's handoff.

#333 (PR #536) delivers Issue Working Memory to ``ANALYZE_EXECUTE`` and says
that a run with Memory off renders and saves what the release before it did,
but for one thing: the prompt label, which ADR 0015 D19 moves from ``v18`` to
``v19`` with the prompt change that names ``memory.md``. A run of one revision
compared with itself cannot show that, so the comparison is held to what the
controller *before* the change really rendered and saved. This script makes
those captures. It is run by hand, once, never by the suite:

    git archive 70e8bb0199b80b58199730f4546456b5377cd555 | tar -x -C /tmp/af-base
    uv run python tests/fixtures/memory_off_baseline/capture_memory_off_baseline.py \\
        /tmp/af-base tests/fixtures/memory_off_baseline

It runs :func:`memory_off_run` *from that tree*, with that tree's own
``autoforge`` and ``tests/conftest.py`` (their fakes, the scripted provider and
a bare local origin: no real agent, no GitHub, no network), and writes what it
returns: the first ``ANALYZE_EXECUTE`` launch's prompt
(``analyze_execute_prompt.txt``) and the checkpoint after the step
(``state.json``), each normalized by :func:`normalize`. ``SOURCE_COMMIT`` is
PR #536's merge base. ``tests/test_memory_handoff.py`` runs the same function
on the tree under test and compares; the files are never written or edited by
hand. To change them, change this script and capture again from the same
commit.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_COMMIT = "70e8bb0199b80b58199730f4546456b5377cd555"
PROMPT_FILE = "analyze_execute_prompt.txt"
STATE_FILE = "state.json"
# Every commit of the run (the origin's seed, the agent's) is made at this
# date, so each SHA the prompt and the checkpoint name is the same on every
# host and on both revisions.
GIT_DATE = "2026-01-01T00:00:00+00:00"
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
# A launch's read-context directory ends in a random suffix (#165).
_READ_CONTEXT = re.compile(r"(/read-context/<RUN>/[a-z_]+-\d+-)[0-9a-f]{12}")


def normalize(text: str, root: Path, run_id: str) -> str:
    """``text`` with what differs between two runs replaced: the test's tree, the run, the times.

    The tree is replaced as given and as resolved, for a host whose
    temporary directory is reached through a link.
    """
    for name in sorted({str(root.resolve()), str(root)}, key=len, reverse=True):
        text = text.replace(name, "<TMP>")
    text = text.replace(run_id, "<RUN>")
    return _TIME.sub("<TIME>", _READ_CONTEXT.sub(r"\1<ID>", text))


def memory_off_run(tmp_path: Path, monkeypatch, extra: dict) -> tuple[str, str]:
    """One Memory-off run to REVIEW: its ANALYZE_EXECUTE prompt and its checkpoint, normalized.

    The default configuration (Memory off, the outer sandbox disabled), a
    REMOTE run recorded as ``run`` records it, and a scripted agent that makes
    one commit and reports it, with ``extra`` added to its result.
    """
    from tests.conftest import (
        FakeGitHub,
        analyze_payload,
        block,
        commit_in,
        make_engine,
        scripted,
    )

    monkeypatch.setenv("GIT_AUTHOR_DATE", GIT_DATE)
    monkeypatch.setenv("GIT_COMMITTER_DATE", GIT_DATE)
    state_dir = tmp_path / "repo" / ".autoforge"
    state_dir.parent.mkdir()
    prompts: list[str] = []

    def agent(req):
        prompts.append(req.prompt)
        return block(analyze_payload(commit_in(req.cwd), **extra))

    eng = make_engine(state_dir, scripted(agent), github=FakeGitHub(), origin=True)
    eng.record_run_configuration()
    eng._save()
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    assert eng.step().next_phase == "REVIEW"
    run_id = eng.state.run_id
    state = json.loads(Path(eng.paths.state_file).read_text(encoding="utf-8"))
    eng.close()
    (prompt,) = prompts
    saved = json.dumps(state, indent=2, sort_keys=True) + "\n"
    return normalize(prompt, tmp_path, run_id), normalize(saved, tmp_path, run_id)


_CAPTURE_TEST = """
import importlib.util, os
from pathlib import Path

def test_capture(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("capture", os.environ["CAPTURE_SCRIPT"])
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)
    prompt, state = capture.memory_off_run(tmp_path, monkeypatch, {})
    out = Path(os.environ["CAPTURE_OUT"])
    (out / capture.PROMPT_FILE).write_text(prompt, encoding="utf-8")
    (out / capture.STATE_FILE).write_text(state, encoding="utf-8")
"""


def main(source: Path, out: Path) -> int:
    head = (source / "src" / "autoforge" / "__init__.py").read_text(encoding="utf-8")
    if '__prompt_version__ = "v18"' not in head:
        sys.exit(f"{source} is not the tree of {SOURCE_COMMIT} (its prompt label is not v18)")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=source / "tests", prefix="_capture_") as scratch:
        module = Path(scratch) / "test_capture_memory_off_baseline.py"
        module.write_text(_CAPTURE_TEST, encoding="utf-8")
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(source / "src"), str(source)]),
            "CAPTURE_SCRIPT": str(Path(__file__).resolve()),
            "CAPTURE_OUT": str(out.resolve()),
        }
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(module)],
            cwd=source,
            env=env,
            check=False,
        )
    return done.returncode


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} SOURCE_TREE OUT_DIR")
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
