"""Doctor: read-only checks with a fake command runner."""

from autoforge.doctor import Doctor
from autoforge.executor import ExecutionResult


def _runner_factory(git_remote="https://github.com/owner/repo.git", fail=()):
    def runner(req):
        argv = req.command
        key = " ".join(argv[:2])
        if any(key.startswith(f) for f in fail):
            return ExecutionResult(argv, req.cwd, 1, "", "boom", "t", "t")
        if argv[:3] == ["git", "remote", "get-url"]:
            out = git_remote
        elif argv[:2] == ["git", "rev-parse"]:
            out = "/repo"
        else:
            out = f"{argv[0]} version 1.0"
        return ExecutionResult(argv, req.cwd, 0, out + "\n", "", "t", "t")

    return runner


def test_all_checks_pass(tmp_path):
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory())
    results = d.run_all()
    names = [r.name for r in results]
    for expected in (
        "config",
        "merge gate",
        "git available",
        "gh available",
        "gh authenticated",
        "claude available",
        "opencode available",
        "cwd is a git repository",
        "GitHub remote",
        "state dir writable",
    ):
        assert expected in names
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    assert "owner/repo" in next(r.detail for r in results if r.name == "GitHub remote")
    assert (tmp_path / ".autoforge").is_dir()
    assert not any(p.name.startswith(".doctor-") for p in (tmp_path / ".autoforge").iterdir())


def test_failures_are_reported_not_raised(tmp_path):
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory(fail=("gh auth", "opencode")))
    results = {r.name: r for r in d.run_all()}
    assert not results["gh authenticated"].ok
    assert not results["opencode available"].ok
    assert results["git available"].ok


def test_non_github_remote_fails(tmp_path):
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory(git_remote="git@gitlab.com:o/r.git"))
    results = {r.name: r for r in d.run_all()}
    assert not results["GitHub remote"].ok


def test_bad_config_reported(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text('{"version": 1, "profiles": {"fix": {"effort": "ultra"}}}')
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    results = {r.name: r for r in d.run_all()}
    assert not results["config"].ok and "effort" in results["config"].detail


def test_merge_gate_reported_with_its_source(tmp_path):
    """AF-SEC-001: doctor shows the effective gate state and which line set it."""
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory())
    gate = {r.name: r for r in d.run_all()}["merge gate"]
    assert gate.ok and not gate.required  # informational, never a failure
    assert gate.detail.startswith("CLOSED: safety.allow_merge=false (built-in default)")

    cfg = tmp_path / "c.json"
    cfg.write_text('{"version": 1, "safety": {"allow_merge": true}}')
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    gate = {r.name: r for r in d.run_all()}["merge gate"]
    assert gate.detail.startswith("config half OPEN: safety.allow_merge=true")
    assert f"{cfg}: safety.allow_merge" in gate.detail
    assert "--allow-merge" in gate.detail

    cfg.write_text('{"version": 1, "safety": {"allow_merge": false}}')
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    gate = {r.name: r for r in d.run_all()}["merge gate"]
    assert gate.detail.startswith("CLOSED: safety.allow_merge=false")
    assert f"{cfg}: safety.allow_merge" in gate.detail


def test_merge_gate_not_claimed_when_config_fails_to_load(tmp_path):
    """A rejected config (e.g. the deprecated execution.allow_merge) reports no gate state."""
    cfg = tmp_path / "c.json"
    cfg.write_text('{"version": 1, "execution": {"allow_merge": true}}')
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    results = {r.name: r for r in d.run_all()}
    assert not results["config"].ok and "execution.allow_merge" in results["config"].detail
    gate = results["merge gate"]
    assert gate.detail == "(config check failed)"
    assert not gate.ok and not gate.required  # a WARN, never a claim about the gate


def test_reused_doctor_forgets_a_config_that_turned_invalid(tmp_path):
    """PR #58 review: a stale cached config must not describe a rejected file.

    A `Doctor` that loaded a valid open-gate config and is then run again after
    the file gained the deprecated `execution.allow_merge` key must report the
    gate as unknown, not replay the previous OPEN state next to a failed
    `config` row.
    """
    cfg = tmp_path / "c.json"
    cfg.write_text('{"version": 1, "safety": {"allow_merge": true}, "state_dir": "custom"}')
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    first = {r.name: r for r in d.run_all()}
    assert first["config"].ok
    assert first["merge gate"].ok and first["merge gate"].detail.startswith("config half OPEN")
    assert d.config is not None and d.config.state_dir == "custom"

    cfg.write_text('{"version": 1, "execution": {"allow_merge": true}}')
    second = {r.name: r for r in d.run_all()}
    assert not second["config"].ok and "execution.allow_merge" in second["config"].detail
    gate = second["merge gate"]
    assert gate.detail == "(config check failed)"
    assert not gate.ok and not gate.required
    # Every other check reads the same cache: none may keep using the old file.
    assert d.config is None
    assert "custom" not in second["state dir writable"].detail


def test_merge_gate_not_claimed_when_a_required_profile_is_invalid(tmp_path):
    """PR #58 review: a config that parses but fails profile validation is not accepted.

    `safety.allow_merge` is read before the profiles are validated, so the
    gate row could say "a run with --allow-merge WILL merge" about a config no
    run would start on. The config is retained only once the whole `config`
    check passes, so every later row -- the gate first -- describes an
    accepted config or none.
    """
    cfg = tmp_path / "c.json"
    cfg.write_text(
        '{"version": 1, "safety": {"allow_merge": true}, "state_dir": "custom",'
        ' "profiles": {"fix": {"provider": "nope"}}}'
    )
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    results = {r.name: r for r in d.run_all()}
    assert not results["config"].ok and "unknown provider 'nope'" in results["config"].detail
    gate = results["merge gate"]
    assert gate.detail == "(config check failed)"
    assert not gate.ok and not gate.required
    assert d.config is None
    assert "custom" not in results["state dir writable"].detail
