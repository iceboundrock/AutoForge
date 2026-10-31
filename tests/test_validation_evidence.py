"""Candidate validation through the engine: records, markers and recovery (#172).

ADR 0006 Wave 2B end to end, offline: real git repositories under
``tmp_path``, real local Python validators, the in-memory GitHub and
scripted agents. REMOTE validation is the engine's
``validate_remote_candidate`` entry point, which no phase calls yet (#173);
LOCAL validation is the existing ``local.validation_commands`` path, which
now leaves a record for every command it runs. The pure value checks are in
``tests/test_evidence.py``.
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from autoforge import child_env, cli
from autoforge import engine as engine_module
from autoforge import git_transport as git_transport_module
from autoforge import materialize as materialize_module
from autoforge.config import default_config
from autoforge.errors import (
    CandidateNotMaterializableError,
    ConfigurationError,
    ExecutionError,
    SandboxLaunchRefusedError,
    StateError,
    ValidationInconclusiveError,
    VerificationError,
)
from autoforge.evidence import (
    PrHeadProvenance,
    WorktreeProvenance,
    applicable,
    attempt_marker,
    definitions,
    parse_candidate,
    parse_record,
)
from autoforge.state import load_state
from autoforge.transitions import Phase

from .conftest import (
    GIT_IDENT,
    FakeGitHub,
    assert_isolated_names,
    env_probe_command,
    env_probe_records,
    git_out,
    git_repo,
    implement,
    make_engine,
    make_local_engine,
    pollute_operator_environment,
)
from .local_helpers import impl_result, local_repo, review_result, scripted, touch_impl

PY = sys.executable
PROVENANCE = WorktreeProvenance(2, "2", True)
# Built at runtime: a literal would itself be credential-shaped text.
SECRET = "gh" + "p_" + "s3cretvalue123456"
# The last argv element of a validator a test's runner interrupts.
TAG = "--autoforge-test-validator"


def py(code: str, *extra: str) -> list[str]:
    return [PY, "-c", code, *extra]


def remote_engine(tmp_path, commands, *, timeout: int = 60, **files):
    """A REMOTE engine on a repository whose HEAD commits ``files``; (engine, commit)."""
    cfg = default_config()
    cfg.validation.commands = commands
    cfg.execution.command_timeout_seconds = timeout
    repo = git_repo(tmp_path / "repo")
    for name, text in (files or {"app.txt": "pristine\n"}).items():
        (repo / name).write_text(text, encoding="utf-8")
        git_out("-C", str(repo), "add", name)
    git_out("-C", str(repo), *GIT_IDENT, "commit", "-q", "-m", "candidate")
    eng = make_engine(tmp_path / "state", [], cfg=cfg, workdir=repo)
    return eng, git_out("-C", str(repo), "rev-parse", "HEAD")


def store_digest(repo: Path) -> str:
    """Every file of the object store and the checkout, by path and bytes."""
    h = hashlib.sha256()
    for path in sorted(p for p in repo.rglob("*") if p.is_file()):
        rel = path.relative_to(repo).as_posix()
        if rel.startswith(".git/") and not rel.startswith(".git/objects/"):
            continue
        h.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


def saved(eng):
    return load_state(eng.paths.state_file)


def run_logs(eng) -> str:
    root = eng.paths.logs_dir / eng.state.run_id
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in root.rglob("*") if p.is_file()
    )


def interrupting(monkeypatch, *, after_exit: bool = False) -> None:
    """Interrupt the validator whose argv ends with ``TAG``: at its launch, or after it exited."""
    real = engine_module.execute

    def runner(req):
        if req.command[-1:] == [TAG]:
            if after_exit:
                assert real(req).exit_code == 0
            raise KeyboardInterrupt
        return real(req)

    monkeypatch.setattr(engine_module, "execute", runner)


# -- REMOTE: the exact candidate (D2.1, D6, D10.1) -------------------------------------


def test_a_passing_set_binds_commit_tree_definitions_and_policy(tmp_path):
    eng, commit = remote_engine(tmp_path, [py("pass"), py("import sys; sys.exit(0)")])
    run = eng.validate_remote_candidate(commit, PROVENANCE)
    assert run.passed and len(run.records) == 2
    tree = git_out("-C", str(eng.workdir), "rev-parse", f"{commit}^{{tree}}")
    state = saved(eng)
    assert state.validation_records == list(run.records)
    assert state.validation_attempt == {}
    for index, (record, definition) in enumerate(
        zip(state.validation_records, definitions(eng.config.validation.commands), strict=True)
    ):
        assert record["outcome"] == "PASS" and record["seq"] == index + 1
        assert record["candidate"]["commit"] == commit and record["candidate"]["tree"] == tree
        assert record["candidate"]["provenance"] == PROVENANCE.to_dict()
        assert record["definition"]["definition_digest"] == definition.digest
        assert record["policy"]["policy_digest"] == run.policy.digest
        assert record["enforcement"]["enforced"] is False  # sandbox.mode: disabled
        step = eng.paths.logs_dir / record["run_log"]
        assert (step / "execution.json").is_file()
    assert parse_candidate(state.validation_records[0]["candidate"]) == run.candidate


def test_a_failing_definition_stops_the_set(tmp_path):
    ran = tmp_path / "second-ran"
    eng, commit = remote_engine(
        tmp_path,
        [py("import sys; print('lint failed'); sys.exit(3)"), py(f"open({str(ran)!r}, 'w')")],
    )
    run = eng.validate_remote_candidate(commit, PROVENANCE)
    assert not run.passed
    [record] = run.records
    assert record["outcome"] == "FAIL" and record["exit_code"] == 3
    assert record["output"]["stdout_tail"] == "lint failed\n"
    assert not ran.exists()


def test_exit_127_from_a_launched_shell_is_a_fail(tmp_path):
    eng, commit = remote_engine(tmp_path, [["sh", "-c", "exit 127"]])
    [record] = eng.validate_remote_candidate(commit, PROVENANCE).records
    assert record["outcome"] == "FAIL" and record["exit_code"] == 127


def test_a_validator_that_cannot_be_launched_is_inconclusive_with_no_record(tmp_path):
    eng, commit = remote_engine(tmp_path, [[str(tmp_path / "no-such-validator")]])
    with pytest.raises(ValidationInconclusiveError, match="could not be launched.*2 attempts"):
        eng.validate_remote_candidate(commit, PROVENANCE)
    state = saved(eng)
    assert state.validation_records == [] and state.validation_attempt == {}


def unlaunchable_git(monkeypatch, word: str, failures: int | None, *, module=engine_module):
    """The controller's git process naming ``word`` cannot be launched, ``failures`` times
    (always when None), as the executor reports it; returns the refused argvs. ``module``
    is whose executor: the engine's (the materializer's too), or the git transport's."""
    real = module.execute
    refused: list[list[str]] = []

    def runner(req):
        if word in req.command and (failures is None or len(refused) < failures):
            refused.append(list(req.command))
            raise ExecutionError(f"failed to spawn git: [Errno 24] Too many open files ({SECRET})")
        return real(req)

    monkeypatch.setattr(module, "execute", runner)
    return refused


def unanswered_git(monkeypatch, word: str, failures: int | None):
    """The engine's git process naming ``word`` times out, ``failures`` times (always
    when None), as the executor reports it; returns the argvs that timed out."""
    real = engine_module.execute
    timed_out: list[list[str]] = []

    def runner(req):
        result = real(req)
        if word in req.command and (failures is None or len(timed_out) < failures):
            timed_out.append(list(req.command))
            return dataclasses.replace(result, exit_code=-9, stdout="", timed_out=True)
        return result

    monkeypatch.setattr(engine_module, "execute", runner)
    return timed_out


def unwritable_export(monkeypatch, failures: int | None):
    """Creating the validation export's directory fails with ENOSPC, ``failures`` times
    (always when None); returns the refused attempts."""
    real = tempfile.mkdtemp
    refused: list[str] = []

    def mkdtemp(*args, prefix=None, **kwargs):
        if prefix == engine_module.VALIDATION_EXPORT_PREFIX and (
            failures is None or len(refused) < failures
        ):
            refused.append(prefix)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(*args, prefix=prefix, **kwargs)

    monkeypatch.setattr(materialize_module.tempfile, "mkdtemp", mkdtemp)
    return refused


# Each way setting the candidate up fails besides a missing candidate (D10.5):
# finding the shared store (its git process not launched, or timing out) and
# reading the candidate's commit there, the object-format read and the
# materializer's git plumbing not launched, the export directory not created;
# what the message names.
SETUP_FAILURES = {
    "store-discovery": (
        lambda mp, n: unlaunchable_git(mp, "--show-toplevel", n),
        "cannot be read from the shared object store",
    ),
    "store-discovery-timeout": (
        lambda mp, n: unanswered_git(mp, "--show-toplevel", n),
        "cannot be read from the shared object store",
    ),
    "commit-read": (
        lambda mp, n: unlaunchable_git(mp, "--batch", n, module=git_transport_module),
        "cannot be read from the shared object store",
    ),
    "object-format": (
        lambda mp, n: unlaunchable_git(mp, "--show-object-format", n),
        "object format of its store could not be read",
    ),
    "listing": (
        lambda mp, n: unlaunchable_git(mp, "ls-tree", n),
        "could not be materialized for validation definition",
    ),
    "blob-stream": (
        lambda mp, n: unlaunchable_git(mp, "--batch", n),
        "could not be materialized for validation definition",
    ),
    "export-directory": (
        unwritable_export,
        "could not be materialized for validation definition",
    ),
}


@pytest.mark.parametrize("how", sorted(SETUP_FAILURES))
def test_a_setup_failure_is_rechecked_once_then_inconclusive_with_no_record(
    tmp_path, monkeypatch, how
):
    """An ``OSError``, a git process the executor could not launch, or the
    store not found or its commit not read, is a setup failure of the
    candidate: one fresh attempt, then the typed inconclusive error, with
    nothing launched and nothing written."""
    ran = tmp_path / "ran"
    eng, commit = remote_engine(tmp_path, [py(f"open({str(ran)!r}, 'w')")])
    eng.save()
    before = eng.paths.state_file.read_bytes()
    inject, says = SETUP_FAILURES[how]
    tries = inject(monkeypatch, None)
    with pytest.raises(ValidationInconclusiveError, match=f"{says}.*2 attempts") as caught:
        eng.validate_remote_candidate(commit, PROVENANCE)
    assert len(tries) == 2
    assert SECRET not in str(caught.value)
    assert not ran.exists()
    assert eng.paths.state_file.read_bytes() == before
    assert eng.state.validation_records == [] and eng.state.validation_attempt == {}


@pytest.mark.parametrize("how", sorted(SETUP_FAILURES))
def test_a_setup_failure_once_is_recovered_by_the_fresh_attempt(tmp_path, monkeypatch, how):
    eng, commit = remote_engine(tmp_path, [py("pass")])
    inject, _ = SETUP_FAILURES[how]
    tries = inject(monkeypatch, 1)
    run = eng.validate_remote_candidate(commit, PROVENANCE)
    assert len(tries) == 1
    assert [r["outcome"] for r in saved(eng).validation_records] == ["PASS"]
    assert run.passed and run.records[0]["seq"] == 1


def counted_exports(monkeypatch) -> list[str]:
    """The commit of every export the engine starts, in order."""
    real = engine_module.materialize
    exports: list[str] = []

    def counted(runner, repo, commit, *args, **kwargs):
        exports.append(commit)
        return real(runner, repo, commit, *args, **kwargs)

    monkeypatch.setattr(engine_module, "materialize", counted)
    return exports


def counted_store_reads(monkeypatch) -> list[str]:
    """The commit of every read of a candidate from the shared store, in order."""
    real = git_transport_module.GitTransport.read_local_commit
    reads: list[str] = []

    def counted(self, sha):
        reads.append(sha)
        return real(self, sha)

    monkeypatch.setattr(git_transport_module.GitTransport, "read_local_commit", counted)
    return reads


def stdin_git(repo: str, *args: str, data: bytes) -> str:
    """``git -C repo`` with ``args`` and ``data`` on stdin; its stdout, stripped."""
    res = subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True, check=True)
    return res.stdout.decode().strip()


def test_a_blob_missing_from_the_store_is_refused_without_a_fresh_export(tmp_path, monkeypatch):
    """The materializer's own refusal of a candidate (D6.1) is not retried."""
    ran = tmp_path / "ran"
    eng, _ = remote_engine(tmp_path, [py(f"open({str(ran)!r}, 'w')")])
    repo = str(eng.workdir)
    ghost = stdin_git(repo, "hash-object", "--stdin", data=b"only hashed\n")
    tree = stdin_git(repo, "mktree", "--missing", data=f"100644 blob {ghost}\tghost.txt\n".encode())
    commit = git_out("-C", repo, *GIT_IDENT, "commit-tree", "-m", "ghost", tree)
    exports = counted_exports(monkeypatch)
    with pytest.raises(CandidateNotMaterializableError, match=f"{ghost} of ghost.txt"):
        eng.validate_remote_candidate(commit, PROVENANCE)
    assert exports == [commit]
    assert not ran.exists()
    assert eng.state.validation_records == [] and eng.state.validation_attempt == {}


def test_a_sandbox_refusal_is_not_a_materialization_failure(tmp_path, monkeypatch):
    """The refusal comes from the validator's launch, inside the export: it
    passes through after one export, never a fresh one."""
    eng, commit = remote_engine(tmp_path, [py("pass")])
    eng.config.sandbox.mode = "required"
    exports = counted_exports(monkeypatch)

    def refuse(*_a, **_k):
        raise SandboxLaunchRefusedError("bwrap is not installed")

    monkeypatch.setattr(eng, "_command_sandbox", refuse)
    reads = counted_store_reads(monkeypatch)
    with pytest.raises(SandboxLaunchRefusedError, match="bwrap is not installed"):
        eng.validate_remote_candidate(commit, PROVENANCE)
    assert reads == [commit] and exports == [commit]
    assert eng.state.validation_records == [] and eng.state.validation_attempt == {}


def test_a_timeout_is_a_timeout_record(tmp_path):
    eng, commit = remote_engine(tmp_path, [py("import time; time.sleep(30)")], timeout=1)
    [record] = eng.validate_remote_candidate(commit, PROVENANCE).records
    assert record["outcome"] == "TIMEOUT" and record["exit_code"] is None


def test_a_commit_not_in_the_store_is_refused_before_any_launch(tmp_path, monkeypatch):
    """Read once, never again and never exported (D10.5): a candidate the store
    does not hold is refused, not a setup failure."""
    ran = tmp_path / "ran"
    eng, _ = remote_engine(tmp_path, [py(f"open({str(ran)!r}, 'w')")])
    reads = counted_store_reads(monkeypatch)
    exports = counted_exports(monkeypatch)
    with pytest.raises(CandidateNotMaterializableError, match="not in the shared object store"):
        eng.validate_remote_candidate("0123456789" * 4, PROVENANCE)
    assert reads == ["0123456789" * 4] and exports == []
    assert not ran.exists()
    assert eng.state.validation_records == [] and eng.state.validation_attempt == {}
    assert not eng.paths.state_file.exists()  # nothing was saved


def test_a_mutating_validator_leaves_the_candidate_and_the_next_export_pristine(tmp_path):
    """Each definition gets a fresh export of the commit's bytes; the original
    repository, its object store and its checkout are untouched."""
    mutate = py(
        "import pathlib; pathlib.Path('app.txt').write_text('mutated'); "
        "pathlib.Path('planted.txt').write_text('x')"
    )
    check = py(
        "import pathlib, sys; "
        "sys.exit(0 if pathlib.Path('app.txt').read_text() == 'pristine\\n' "
        "and not pathlib.Path('planted.txt').exists() else 1)"
    )
    eng, commit = remote_engine(tmp_path, [mutate, check])
    before = store_digest(Path(eng.workdir))
    run = eng.validate_remote_candidate(commit, PROVENANCE)
    assert [r["outcome"] for r in run.records] == ["PASS", "PASS"]
    assert {r["candidate"]["commit"] for r in run.records} == {commit}
    assert store_digest(Path(eng.workdir)) == before
    assert (Path(eng.workdir) / "app.txt").read_text() == "pristine\n"


def test_two_exports_of_one_candidate_give_one_policy_digest(tmp_path):
    eng, commit = remote_engine(tmp_path, [py("import os; print(os.getcwd())")])
    one = eng.validate_remote_candidate(commit, PROVENANCE).records[0]
    two = eng.validate_remote_candidate(commit, PrHeadProvenance(_pr(eng))).records[0]
    assert one["output"]["stdout_tail"] != two["output"]["stdout_tail"]  # two directories
    assert one["policy"] == two["policy"]
    assert one["definition"] == two["definition"]
    assert one["run_log"] != two["run_log"] and two["seq"] == one["seq"] + 1
    views = [parse_record(r) for r in (one, two)]
    assert views[0].candidate.identity == views[1].candidate.identity


def _pr(eng) -> str:
    return f"https://github.com/{eng.state.repository}/pull/42"


def test_a_changed_definition_or_policy_gets_new_digests_and_old_records_do_not_apply(tmp_path):
    eng, commit = remote_engine(tmp_path, [py("pass")])
    old = eng.validate_remote_candidate(commit, PROVENANCE)
    view = parse_record(old.records[0])
    eng.config.validation.commands = [py("pass", "-v")]
    changed = eng.validate_remote_candidate(commit, PROVENANCE)
    assert changed.records[0]["definition"]["definition_digest"] != view.definition_digest
    assert not applicable(
        view,
        changed.candidate,
        changed.records[0]["definition"]["definition_digest"],
        changed.policy,
    )
    eng.config.validation.commands = [py("pass")]
    eng.config.execution.command_timeout_seconds = 61
    slower = eng.validate_remote_candidate(commit, PROVENANCE)
    assert slower.policy.digest != old.policy.digest
    assert not applicable(view, slower.candidate, view.definition_digest, slower.policy)
    assert applicable(view, old.candidate, view.definition_digest, old.policy)


def test_an_output_flood_is_bounded_in_state(tmp_path):
    flood = py("import sys; sys.stdout.write('z' * 3_000_000); sys.exit(1)")
    eng, commit = remote_engine(tmp_path, [flood])
    [record] = eng.validate_remote_candidate(commit, PROVENANCE).records
    out = record["output"]
    assert out["stdout_bytes"] == 3_000_000
    assert out["stdout_tail_truncated"] is True and out["stdout_tail"] == "z" * 2000
    assert eng.paths.state_file.stat().st_size < 100_000


def test_secret_shaped_argv_and_output_are_redacted_in_state_status_and_logs(
    tmp_path, monkeypatch, capsys
):
    leak = py(
        "import sys; print('GITHUB_TOKEN=' + sys.argv[1]); "
        "print('token', sys.argv[1], file=sys.stderr); sys.exit(1)",
        SECRET,
    )
    eng, commit = remote_engine(tmp_path, [leak])
    [record] = eng.validate_remote_candidate(commit, PROVENANCE).records
    assert "REDACTED" in record["output"]["stdout_tail"]
    assert "REDACTED" in " ".join(record["definition"]["argv"])
    assert SECRET not in eng.paths.state_file.read_text(encoding="utf-8")
    assert SECRET not in run_logs(eng)
    monkeypatch.chdir(eng.workdir)
    capsys.readouterr()
    assert cli.main(["--state-dir", str(eng.paths.state_dir), "status"]) == 0
    out = capsys.readouterr().out
    assert "Validation evidence:" in out and "FAIL (exit 1, unsandboxed)" in out
    assert SECRET not in out


def test_control_characters_in_output_are_escaped(tmp_path):
    eng, commit = remote_engine(
        tmp_path,
        [py("import sys; sys.stdout.write('\\x1b[31mred\\x1b[0m\\r\\n\\u2028'); sys.exit(2)")],
    )
    [record] = eng.validate_remote_candidate(commit, PROVENANCE).records
    assert record["output"]["stdout_tail"] == "\\x1b[31mred\\x1b[0m\\r\n\\u2028"


def test_a_sandbox_refusal_under_required_writes_nothing(tmp_path, monkeypatch):
    """A capability error at the gate: no marker, no record, the state file
    byte-identical; the refusal is in the run log."""
    eng, commit = remote_engine(tmp_path, [py("pass")])
    eng.config.sandbox.mode = "required"
    eng.save()
    before = eng.paths.state_file.read_bytes()

    def refuse(*_a, **_k):
        raise SandboxLaunchRefusedError("bwrap is not installed")

    monkeypatch.setattr(eng, "_command_sandbox", refuse)
    with pytest.raises(SandboxLaunchRefusedError, match="bwrap is not installed"):
        eng.validate_remote_candidate(commit, PROVENANCE)
    assert eng.paths.state_file.read_bytes() == before
    assert "bwrap is not installed" in run_logs(eng)


def test_a_local_run_has_no_remote_entry_point(tmp_path):
    root = local_repo(tmp_path)
    eng = make_local_engine(root, "features/add-filter.md")
    with pytest.raises(StateError, match="LOCAL run validates in place"):
        eng.validate_remote_candidate("a" * 40, PROVENANCE)


# -- REMOTE: a credential source planted during the producer's launch (#165) -----------
#
# ADR 0009 A.2.3 H (#386): the implementation agent writes it after its own
# launch's preflight passed and before the controller validates the candidate
# it produced, through ``validate_remote_candidate`` (the producer gate).

# What a planted credential source holds: no token shape, so only the
# preflight's own care keeps it out of a message, never the redaction.
PLANTED = "planted-credential-value-7f3a"
IMPLEMENTATION_BRANCH = "autoforge/2"

# A validation command that records, from inside its export, every
# ``credential.helper`` git reads there and the scope of everything git reads
# there (``--show-scope``: system, global, local, worktree, command).
_GIT_CONFIG_PROBE = (
    "import json, subprocess, sys; "
    "git = lambda *a: subprocess.run(['git', 'config', '--show-scope', '--show-origin', *a], "
    "capture_output=True, text=True).stdout.splitlines(); "
    "open(sys.argv[1], 'a').write(json.dumps({'helpers': git('--get-all', 'credential.helper'), "
    "'listing': git('--list')}) + chr(10))"
)


def git_config_probe(record: Path) -> list[str]:
    return py(_GIT_CONFIG_PROBE, str(record))


def planting(plant):
    """The implementation agent, which plants with ``plant(req)`` before its commit."""

    def agent(req):
        plant(req)
        return implement(req)

    return agent


def plant_helpers(req) -> None:
    """A credential helper in the common dir's ``config`` and the worktree's ``config.worktree``."""
    git_out("-C", req.cwd, "config", "--local", "extensions.worktreeConfig", "true")
    git_out("-C", req.cwd, "config", "--local", "credential.helper", "store")
    git_out("-C", req.cwd, "config", "--worktree", "credential.helper", f"!echo {PLANTED}")


def plant_in_home(name: str, text: str):
    """A plant writing ``text`` to ``name`` in the home the preflight reads (``~``)."""

    def plant(req) -> None:
        (Path(child_env.operator_home()) / name).write_text(text, encoding="utf-8")

    return plant


def producing(tmp_state_dir, gh, agent, *commands):
    """A REMOTE run at ANALYZE_EXECUTE with ``commands`` configured, on a local origin."""
    cfg = default_config()
    cfg.validation.commands = [list(c) for c in commands]
    eng = make_engine(tmp_state_dir, agent, github=gh, cfg=cfg, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    return eng


def watched_validation(monkeypatch, eng) -> list[tuple]:
    """Each call of ``eng.validate_remote_candidate``: (commit, provenance, run or error)."""
    real = eng.validate_remote_candidate
    calls: list[tuple] = []

    def watched(commit, provenance, **kwargs):
        try:
            run = real(commit, provenance, **kwargs)
        except Exception as exc:
            calls.append((commit, provenance, exc))
            raise
        calls.append((commit, provenance, run))
        return run

    monkeypatch.setattr(eng, "validate_remote_candidate", watched)
    return calls


def test_a_helper_planted_by_the_producer_never_reaches_its_validation_command(
    tmp_state_dir, tmp_path_factory, monkeypatch
):
    """REMOTE prevents the unsafe launch by where it runs the command, not by
    refusing it: each command runs in a fresh export outside the repository,
    so a helper the agent planted in the common dir's ``config`` and its
    worktree's ``config.worktree`` is not among what git reads there, and
    the command gets no credential variable of the operator's. The next
    agent launch in that worktree refuses the helper before it starts.

    ADR 0009 A.2.3 H-A (#386): the REMOTE replacement of
    tests/test_local.py::test_a_credential_helper_planted_before_validation_leaves_the_phase_unverified
    (case i, the helper).
    """
    scratch = tmp_path_factory.mktemp("probe")
    pollute_operator_environment(monkeypatch, scratch)
    git_record, env_record = scratch / "git.jsonl", scratch / "env.jsonl"
    gh = FakeGitHub()
    eng = producing(
        tmp_state_dir,
        gh,
        planting(plant_helpers),
        git_config_probe(git_record),
        env_probe_command(env_record),
    )
    calls = watched_validation(monkeypatch, eng)
    assert eng.step().next_phase == "REVIEW"
    [launch] = eng.provider.calls
    head = git_out("-C", launch.cwd, "rev-parse", "HEAD")
    [(commit, provenance, run)] = calls
    assert commit == head and provenance.worktree_id == "2"
    assert run.passed and [r["outcome"] for r in run.records] == ["PASS", "PASS"]
    assert saved(eng).validation_records == list(run.records)
    # Every credential.helper git reads in the export is the controller's
    # command-scope reset (the host's /etc/gitconfig, which no agent writes,
    # aside), and git reads no repository scope there at all.
    [seen] = env_probe_records(git_record)
    helpers = [line.split("\t") for line in seen["helpers"]]
    assert [h for h in helpers if h[0] != "system"] == [["command", "command line:", ""]]
    assert not [line for line in seen["listing"] if line.split("\t")[0] in ("local", "worktree")]
    assert PLANTED not in json.dumps(seen)
    [env] = env_probe_records(env_record)
    assert_isolated_names(env["names"])
    assert env["gh_token_placeholder"] and env["gh_config_dir_empty"]
    # The helpers are in force in the worktree: the next launch there refuses
    # both, before the reviewer is launched.
    with pytest.raises(ConfigurationError) as refused:
        eng.step()
    message = str(refused.value)
    assert "credential helper in local-scope git configuration" in message
    assert "credential helper in worktree-scope git configuration" in message
    assert PLANTED not in message
    assert eng.provider.calls == [launch]
    assert saved(eng).phase == Phase.REVIEW


def refused_validation(tmp_state_dir, tmp_path_factory, monkeypatch, plant):
    """A producer that plants with ``plant``, its one probe's record, and the step's
    outcome: (engine, FakeGitHub, record, validate_remote_candidate's calls, outcome)."""
    record = tmp_path_factory.mktemp("probe") / "env.jsonl"
    gh = FakeGitHub()
    eng = producing(tmp_state_dir, gh, planting(plant), env_probe_command(record))
    calls = watched_validation(monkeypatch, eng)
    return eng, gh, record, calls, eng.step()


def assert_refused_before_the_probe(eng, gh, record, calls, out, *named: str) -> None:
    """The entry point raised naming each of ``named`` and no value; the probe
    never ran, no record, ``BLOCKED``, nothing pushed or written to GitHub."""
    assert not record.exists()
    [(_, _, refused)] = calls
    assert isinstance(refused, ValidationInconclusiveError)
    assert "could not be launched" in str(refused) and "the launch was refused" in str(refused)
    assert out.next_phase == "BLOCKED"
    state = saved(eng)
    assert state.phase == Phase.BLOCKED
    assert state.validation_records == [] and state.validation_attempt == {}
    for text in (str(refused), state.block_reason):
        for name in named:
            assert name in text
        assert PLANTED not in text
    assert "Nothing was pushed or published" in state.block_reason
    assert PLANTED not in eng.paths.state_file.read_text(encoding="utf-8")
    assert eng.origin.head(IMPLEMENTATION_BRANCH) is None
    assert gh.effect_writes == [] and gh.prs == {}


def test_a_netrc_entry_planted_by_the_producer_refuses_its_validation_and_blocks(
    tmp_state_dir, tmp_path_factory, monkeypatch
):
    """A credential source the export does reach: git, curl and Python's
    ``netrc`` read ``$HOME/.netrc`` wherever the command runs. The preflight
    before the command refuses it, the re-check refuses again, and the
    candidate stays unvalidated and unpublished.

    ADR 0009 A.2.3 H-A (#386): the REMOTE replacement of
    tests/test_local.py::test_a_credential_helper_planted_before_validation_leaves_the_phase_unverified
    (case ii, ``~/.netrc``).
    """
    plant = plant_in_home(".netrc", f"machine github.com login x password {PLANTED}\n")
    outcome = refused_validation(tmp_state_dir, tmp_path_factory, monkeypatch, plant)
    assert_refused_before_the_probe(
        *outcome, "an entry for GitHub (or a default entry) in ~/.netrc"
    )


def test_a_startup_file_planted_by_the_producer_refuses_its_validation_and_blocks(
    tmp_state_dir, tmp_path_factory, monkeypatch
):
    """R3-F1 in REMOTE: a login shell the validation command starts would run
    what ``~/.profile`` exports, which the agent can write; the login-shell
    probe reads ``HOME``, not the export, so the command's own preflight
    refuses, naming the shell and the variable, never the value.

    ADR 0009 A.2.3 H-B (#386): the REMOTE replacement of
    tests/test_local.py::test_a_startup_file_planted_before_validation_leaves_the_phase_unverified
    """
    plant = plant_in_home(".profile", f"export GITHUB_TOKEN={PLANTED}\n")
    outcome = refused_validation(tmp_state_dir, tmp_path_factory, monkeypatch, plant)
    assert_refused_before_the_probe(
        *outcome, "the startup files of a login sh ", "which change GITHUB_TOKEN"
    )


# -- REMOTE: crashes and recovery (D10.7, D11.2, D11.3, §2.16) -------------------------


def test_an_interrupted_validator_leaves_its_marker_and_the_next_step_records_it(
    tmp_path, monkeypatch
):
    eng, commit = remote_engine(tmp_path, [py("pass", TAG)])
    interrupting(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        eng.validate_remote_candidate(commit, PROVENANCE)
    state = saved(eng)
    assert state.validation_records == []
    marker = state.validation_attempt
    assert marker["candidate"]["commit"] == commit
    monkeypatch.undo()

    eng.load()
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    state = saved(eng)
    assert state.validation_attempt == {}
    [interrupted] = state.validation_records
    assert interrupted["outcome"] == "INTERRUPTED"
    assert interrupted["run_log"] == marker["run_log"]
    assert interrupted["marked_at"] == marker["marked_at"]
    # The definition runs again, as a record of its own.
    run = eng.validate_remote_candidate(commit, PROVENANCE)
    assert run.passed
    assert [r["outcome"] for r in saved(eng).validation_records] == ["INTERRUPTED", "PASS"]


def test_a_crash_before_the_marker_records_nothing(tmp_path, monkeypatch):
    """Interrupted while the export is written: no marker, no record, and
    the next step has nothing to recover."""
    eng, commit = remote_engine(tmp_path, [py("pass")])
    eng.save()
    before = eng.paths.state_file.read_bytes()

    def crash(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(engine_module, "materialize", crash)
    with pytest.raises(KeyboardInterrupt):
        eng.validate_remote_candidate(commit, PROVENANCE)
    assert eng.paths.state_file.read_bytes() == before
    monkeypatch.undo()
    eng.load()
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    assert saved(eng).validation_records == []


def test_a_crash_after_the_validator_exited_is_never_a_pass(tmp_path, monkeypatch):
    """The validator exited 0, then the controller died before the record:
    recovery records INTERRUPTED, never infers PASS, and the definition reruns."""
    eng, commit = remote_engine(tmp_path, [py("pass", TAG)])
    interrupting(monkeypatch, after_exit=True)
    with pytest.raises(KeyboardInterrupt):
        eng.validate_remote_candidate(commit, PROVENANCE)
    monkeypatch.undo()
    eng.load()
    eng.step()
    outcomes = [r["outcome"] for r in saved(eng).validation_records]
    assert outcomes == ["INTERRUPTED"]


def test_the_third_interruption_blocks_in_the_same_write(tmp_path, monkeypatch):
    eng, commit = remote_engine(tmp_path, [py("pass", TAG)])
    for attempt in range(1, 4):
        interrupting(monkeypatch)
        with pytest.raises(KeyboardInterrupt):
            eng.validate_remote_candidate(commit, PROVENANCE)
        monkeypatch.undo()
        eng.load()
        eng.state.phase = Phase.INITIALIZING  # rewind: the phase gate is #173's
        eng.save()
        out = eng.step()
        if attempt < 3:
            assert out.next_phase == "ANALYZE_EXECUTE"
    assert out.next_phase == "BLOCKED"
    state = saved(eng)
    assert state.phase == Phase.BLOCKED and state.validation_attempt == {}
    assert [r["outcome"] for r in state.validation_records] == ["INTERRUPTED"] * 3
    assert "interrupted 3 times" in state.block_reason
    assert SECRET not in state.block_reason


def test_a_duplicate_resume_appends_nothing(tmp_path, monkeypatch):
    eng, commit = remote_engine(tmp_path, [py("pass", TAG)])
    interrupting(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        eng.validate_remote_candidate(commit, PROVENANCE)
    monkeypatch.undo()
    eng.load()
    eng.step()
    after = eng.paths.state_file.read_bytes()
    eng.load()
    assert eng._recover_validation_attempt(eng.state.phase) is None
    assert eng.paths.state_file.read_bytes() == after


def test_a_stale_marker_whose_record_exists_is_cleared_and_nothing_appended(tmp_path):
    eng, commit = remote_engine(tmp_path, [py("pass")])
    run = eng.validate_remote_candidate(commit, PROVENANCE)
    [record] = run.records
    [definition] = definitions(eng.config.validation.commands)
    eng.state.validation_attempt = attempt_marker(
        candidate=run.candidate,
        definition=definition,
        policy=run.policy,
        run_log=record["run_log"],
        marked_at=record["started_at"],
    )
    eng.save()
    eng.load()
    eng.step()
    state = saved(eng)
    assert state.validation_attempt == {} and state.validation_records == [record]


def test_status_and_dry_run_report_a_pending_marker_and_write_nothing(
    tmp_path, monkeypatch, capsys
):
    eng, commit = remote_engine(tmp_path, [py("pass", TAG)])
    interrupting(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        eng.validate_remote_candidate(commit, PROVENANCE)
    monkeypatch.undo()
    before = eng.paths.state_file.read_bytes()
    monkeypatch.chdir(eng.workdir)
    config = tmp_path / "autoforge.json"
    config.write_text(json.dumps({"validation": {"commands": [py("pass", TAG)]}}))
    flags = ["--config", str(config), "--state-dir", str(eng.paths.state_dir)]
    capsys.readouterr()
    assert cli.main([*flags, "status"]) == 0
    out = capsys.readouterr().out
    assert "Pending attempt: definition [0]" in out and commit[:12] in out
    assert "1 definition(s) in validation.commands" in out
    assert cli.main([*flags, "step", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "candidate validation (validation.commands): 1 definition(s)" in out
    assert "a validation attempt marker is pending: definition [0]" in out
    assert eng.paths.state_file.read_bytes() == before


def test_a_hand_edited_record_fails_the_load(tmp_path):
    eng, commit = remote_engine(tmp_path, [py("import sys; sys.exit(1)")])
    eng.validate_remote_candidate(commit, PROVENANCE)
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    data["validation_records"][0]["outcome"] = "PASS"
    data["validation_records"][0]["exit_code"] = 0
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    before = eng.paths.state_file.read_bytes()
    with pytest.raises(StateError, match="digest.*edited or corrupted"):
        load_state(eng.paths.state_file)
    assert eng.paths.state_file.read_bytes() == before


def test_a_binding_naming_a_missing_record_fails_the_load(tmp_path):
    eng, commit = remote_engine(tmp_path, [py("pass")])
    run = eng.validate_remote_candidate(commit, PROVENANCE)
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    data["review_evidence"] = {
        "review_round": 1,
        "candidate": run.candidate.to_dict(),
        "records": [{"seq": 9, "digest": run.records[0]["digest"]}],
        "validation_set_digest": run.validation_set_digest,
        "policy_digest": run.policy.digest,
    }
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="names record seq 9, which is not retained"):
        load_state(eng.paths.state_file)


def test_a_binding_whose_set_digest_is_not_its_records_fails_the_load(tmp_path):
    eng, commit = remote_engine(tmp_path, [py("pass"), py("pass")])
    run = eng.validate_remote_candidate(commit, PROVENANCE)
    assert run.passed
    data = json.loads(eng.paths.state_file.read_text(encoding="utf-8"))
    binding = {
        "review_round": 1,
        "candidate": run.candidate.to_dict(),
        "records": [{"seq": r["seq"], "digest": r["digest"]} for r in run.records],
        "validation_set_digest": run.validation_set_digest,
        "policy_digest": run.policy.digest,
    }
    data["review_evidence"] = binding
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    assert load_state(eng.paths.state_file).review_evidence == binding
    data["review_evidence"] = binding | {"validation_set_digest": "0" * 64}
    eng.paths.state_file.write_text(json.dumps(data), encoding="utf-8")
    before = eng.paths.state_file.read_bytes()
    with pytest.raises(StateError, match="not the digest of the records' definitions"):
        load_state(eng.paths.state_file)
    assert eng.paths.state_file.read_bytes() == before


# -- REMOTE surfaces -------------------------------------------------------------------


def test_the_dry_run_plan_lists_the_set_and_runs_nothing(tmp_path):
    ran = tmp_path / "ran"
    eng, _ = remote_engine(tmp_path, [py(f"open({str(ran)!r}, 'w')"), ["make", "lint"]])
    [outcome] = eng.run(max_steps=1, dry_run=True)
    notes = "\n".join(outcome.plan.notes)
    assert "candidate validation (validation.commands): 2 definition(s)" in notes
    assert "validation definition [1]: 'make lint'" in notes
    assert "mode disabled" in notes and "unsandboxed" in notes
    # #173: the plan says where the set gates.
    assert "validation.commands gate every REMOTE candidate" in notes
    assert "before anything is pushed or published" in notes
    assert not ran.exists()


# -- LOCAL: records of the in-place validation (D2.6, D7.1, D7.2) ----------------------


def test_a_local_validation_leaves_records_with_start_and_end_fingerprints(tmp_path):
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.validation_commands = [
        py("import pathlib; pathlib.Path('generated.txt').write_text('made by the validator')"),
        ["true"],
    ]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = scripted(
        eng, root, [(lambda r: touch_impl(r, "v1\n"), lambda e: impl_result())]
    )
    eng.run(max_steps=2)
    state = saved(eng)
    assert state.phase == Phase.REVIEW
    first, second, confirming = state.validation_records
    assert first["candidate"]["kind"] == "local" and first["outcome"] == "PASS"
    assert first["start_fingerprint"] != first["end_fingerprint"]  # the validator wrote a file
    assert second["start_fingerprint"] == first["end_fingerprint"]
    assert second["end_fingerprint"] == state.workspace_fingerprint
    # D7.2 (#173): the first command changed covered bytes, so the set ran once
    # more from the tree it left. The second command started and ended there,
    # so its record is reused; the generator runs again and stays there too.
    assert confirming["definition"]["index"] == 0 and confirming["outcome"] == "PASS"
    assert confirming["start_fingerprint"] == confirming["end_fingerprint"]
    assert confirming["end_fingerprint"] == state.workspace_fingerprint
    assert first["candidate"]["run_id"] == state.run_id
    assert first["candidate"]["workspace_policy_digest"] == (
        eng.local_contract().workspace_policy.digest()
    )
    assert first["enforcement"]["enforced"] is False
    assert state.validation_attempt == {}


def test_a_failing_local_validation_is_a_fail_record_and_the_message_is_unchanged(tmp_path):
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.validation_commands = [["false"]]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = scripted(
        eng, root, [(lambda r: touch_impl(r, "v1\n"), lambda e: impl_result())]
    )
    with pytest.raises(VerificationError, match="validation command 'false' failed with exit 1"):
        eng.run(max_steps=3)
    [record] = saved(eng).validation_records
    assert record["outcome"] == "FAIL" and record["exit_code"] == 1


def test_a_local_contract_beyond_the_bounds_blocks_before_the_agent(tmp_path):
    """A contract persisted before the bounds existed (the loader refuses such
    a configuration now): BLOCKED, with no agent and no validator launched."""
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.validation_commands = [["true"]] * 17
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    outcomes = eng.run(max_steps=3)
    assert outcomes[-1].next_phase == "BLOCKED"
    state = saved(eng)
    assert "17 definitions, over the bound of 16" in state.block_reason
    assert eng.provider.calls == [] and state.validation_records == []


def test_an_interrupted_local_validation_is_recorded_on_resume_and_rerun(tmp_path, monkeypatch):
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.validation_commands = [py("pass", TAG)]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: impl_result(changed=False)),
        ],
    )
    interrupting(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        eng.run(max_steps=2)
    marker = saved(eng).validation_attempt
    assert marker["candidate"]["kind"] == "local" and marker["start_fingerprint"]
    monkeypatch.undo()
    eng.load()
    eng.step()
    state = saved(eng)
    assert [r["outcome"] for r in state.validation_records] == ["INTERRUPTED", "PASS"]
    assert state.validation_attempt == {} and state.phase == Phase.REVIEW
    assert "end_fingerprint" not in state.validation_records[0]


def _leave_fifo(root: Path) -> None:
    os.mkfifo(root / "leftover.pipe")


def _leave_too_many_entries(root: Path) -> None:
    junk = root / "junk"
    junk.mkdir()
    for i in range(100):
        (junk / str(i)).write_text("x", encoding="utf-8")


@pytest.mark.parametrize(
    ("leave", "refusal"),
    [
        (_leave_fifo, "leftover.pipe"),
        (_leave_too_many_entries, "refusing to fingerprint the working tree"),
    ],
    ids=["fifo", "over-budget"],
)
def test_a_marker_is_recorded_before_a_plan_that_refuses_the_working_tree(
    tmp_path, monkeypatch, leave, refusal
):
    """R2-F1: the interrupted validator left a tree the LOCAL plan's snapshot
    refuses. The dry run stays read-only and leaves the marker pending; the
    real step records it INTERRUPTED under the lock before it plans, so the
    refusal is persisted with the record, and a second resume appends nothing."""
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.max_workspace_entries = 40
    cfg.local.validation_commands = [py("pass", TAG)]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: impl_result(changed=False)),
        ],
    )
    interrupting(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        eng.run(max_steps=2)
    monkeypatch.undo()
    marker = saved(eng).validation_attempt
    assert marker
    leave(root)
    calls = len(eng.provider.calls)

    eng.load()
    before = eng.paths.state_file.read_bytes()
    with pytest.raises(VerificationError, match=refusal):
        eng.step(dry_run=True)
    assert eng.paths.state_file.read_bytes() == before

    with pytest.raises(VerificationError, match=refusal):
        eng.step()
    state = saved(eng)
    assert state.validation_attempt == {} and state.phase == Phase.ANALYZE_EXECUTE
    [interrupted] = state.validation_records
    assert interrupted["outcome"] == "INTERRUPTED"
    assert interrupted["marked_at"] == marker["marked_at"]
    after = eng.paths.state_file.read_bytes()

    eng.load()
    with pytest.raises(VerificationError, match=refusal):
        eng.step()
    assert eng.paths.state_file.read_bytes() == after
    assert len(eng.provider.calls) == calls


def test_a_changed_local_validation_command_is_refused_before_anything_runs(tmp_path):
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.validation_commands = [["true"]]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.run(max_steps=1)
    before = eng.paths.state_file.read_bytes()
    cfg2 = default_config()
    cfg2.local.validation_commands = [["true", "--changed"]]
    again = make_local_engine(root, "features/add-filter.md", cfg=cfg2, start=False)
    with pytest.raises(VerificationError, match="validation_commands"):
        again.load()
        again.step()
    assert again.provider.calls == []
    assert eng.paths.state_file.read_bytes() == before


def test_the_local_review_binding_passes_with_validation_records(tmp_path):
    """The records ride along with the existing LOCAL loop unchanged."""
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.validation_commands = [["true"]]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint)),
        ],
    )
    eng.run(max_steps=5)
    state = saved(eng)
    assert state.phase == Phase.DONE
    assert [r["outcome"] for r in state.validation_records] == ["PASS"]
    assert os.path.basename(state.validation_records[0]["run_log"]).endswith("-1")


def test_a_failing_local_validation_whose_tree_cannot_be_walked_keeps_its_message(tmp_path):
    """A command that failed and left a tree the walk refuses: no record (no
    end fingerprint to give it), the launch still logged, and the failure
    reported as before, not the walk's refusal."""
    root = local_repo(tmp_path)
    cfg = default_config()
    cfg.local.max_workspace_entries = 40
    cfg.local.validation_commands = [
        py(
            "import pathlib, sys; d = pathlib.Path('junk'); d.mkdir(); "
            "[(d / str(i)).write_text('x') for i in range(100)]; "
            "print('lint failed'); sys.exit(3)"
        )
    ]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = scripted(
        eng, root, [(lambda r: touch_impl(r, "v1\n"), lambda e: impl_result())]
    )
    with pytest.raises(VerificationError, match="failed with exit 3.*Output tail: lint failed"):
        eng.run(max_steps=3)
    state = saved(eng)
    assert state.validation_records == [] and state.validation_attempt == {}
    step = next(p for p in (eng.paths.logs_dir / state.run_id).iterdir() if "validation" in p.name)
    execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
    assert execution["exit_code"] == 3
    request = json.loads((step / "request.json").read_text(encoding="utf-8"))
    assert request["metadata"]["validation_record_seq"] is None
