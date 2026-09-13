"""Doctor: read-only checks with a fake command runner."""

import json

from autoforge.doctor import Doctor
from autoforge.executor import ExecutionResult

RULES_ENDPOINT = "repos/owner/repo/rules/branches/main"
RULESET_ENDPOINT = "repos/owner/repo/rulesets/22792049"
RULESETS_ENDPOINT = "repos/owner/repo/rulesets"
PROTECTION_ENDPOINT = "repos/owner/repo/branches/main/protection"


def _status_rule(*contexts, ruleset_id=22792049):
    return {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": False,
            "required_status_checks": [{"context": c, "integration_id": 15368} for c in contexts],
        },
        "ruleset_source_type": "Repository",
        "ruleset_source": "owner/repo",
        "ruleset_id": ruleset_id,
    }


def _ruleset(enforcement="active", bypass_actors=(), can_bypass="never", ruleset_id=22792049):
    return {
        "id": ruleset_id,
        "name": "main",
        "target": "branch",
        "source_type": "Repository",
        "source": "owner/repo",
        "enforcement": enforcement,
        "bypass_actors": list(bypass_actors),
        "current_user_can_bypass": can_bypass,
        "_links": {"html": {"href": f"https://github.com/owner/repo/rules/{ruleset_id}"}},
    }


# What a healthy repository answers: one active ruleset requiring `ci` on `main`.
HEALTHY_GITHUB = {
    RULES_ENDPOINT: [{"type": "deletion", "ruleset_id": 22792049}, _status_rule("ci")],
    RULESET_ENDPOINT: _ruleset(),
    RULESETS_ENDPOINT: [_ruleset()],
    PROTECTION_ENDPOINT: (1, "gh: Branch not protected (HTTP 404)"),
}


def _runner_factory(
    git_remote="https://github.com/owner/repo.git",
    fail=(),
    github=None,
    default_branch="main",
    calls=None,
):
    """Fake runner. ``github`` maps a `gh api` endpoint to its JSON payload, or to
    an ``(exit_code, stderr)`` pair for a failed read; ``--paginate --slurp``
    listings are wrapped in one page the way `gh` does."""
    responses = dict(HEALTHY_GITHUB)
    responses.update(github or {})

    def runner(req):
        argv = req.command
        if calls is not None:
            calls.append(argv)
        key = " ".join(argv[:2])
        if any(key.startswith(f) for f in fail):
            return ExecutionResult(argv, req.cwd, 1, "", "boom", "t", "t")
        if argv[:3] == ["git", "remote", "get-url"]:
            out = git_remote
        elif argv[:2] == ["git", "rev-parse"]:
            out = "/repo"
        elif argv[:3] == ["gh", "repo", "view"]:
            out = json.dumps(
                {"nameWithOwner": "owner/repo", "defaultBranchRef": {"name": default_branch}}
            )
        elif argv[:2] == ["gh", "api"]:
            endpoint = argv[-1]
            paginated = "--slurp" in argv
            if endpoint not in responses:
                return ExecutionResult(argv, req.cwd, 1, "", f"unscripted {endpoint}", "t", "t")
            payload = responses[endpoint]
            if isinstance(payload, tuple):
                code, stderr = payload
                return ExecutionResult(argv, req.cwd, code, "", stderr, "t", "t")
            out = json.dumps([payload] if paginated else payload)
        else:
            out = f"{argv[0]} version 1.0"
        return ExecutionResult(argv, req.cwd, 0, out + "\n", "", "t", "t")

    return runner


def _required_checks(tmp_path, **kwargs):
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory(**kwargs))
    return {r.name: r for r in d.run_all()}["default branch requires checks"]


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
        "default branch requires checks",
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


# -- default branch requires checks (issue #41) ----------------------------------------
def test_required_check_present_and_enforced(tmp_path):
    """The healthy state: an active ruleset with no bypass actors requires `ci` on main."""
    calls = []
    check = _required_checks(tmp_path, calls=calls)
    assert check.ok and check.required and not check.skipped, check
    assert "'main' requires: ci via ruleset #22792049 (Repository owner/repo)" == check.detail
    # Effective rules are read with every page, and the ruleset once for its enforcement.
    assert ["gh", "api", "--paginate", "--slurp", RULES_ENDPOINT] in calls
    assert ["gh", "api", RULESET_ENDPOINT] in calls
    # Every `gh api` call is a GET: `doctor` never mutates.
    api_calls = [argv for argv in calls if argv[:2] == ["gh", "api"]]
    assert api_calls
    assert not any(
        flag in argv for argv in api_calls for flag in ("-X", "--method", "-f", "-F", "--input")
    )


def test_required_check_not_required_fails_with_remedy(tmp_path):
    """No ruleset and no classic protection: FAIL, with the settings URL and rule named."""
    check = _required_checks(
        tmp_path, github={RULES_ENDPOINT: [{"type": "deletion", "ruleset_id": 1}]}
    )
    assert not check.ok and check.required and not check.skipped
    assert check.detail.startswith("no rule requires a status check on 'main'")
    assert "https://github.com/owner/repo/settings/rules" in check.detail
    assert "naming ci" in check.detail and "no bypass actors" in check.detail


def test_required_check_renamed_context_fails(tmp_path):
    """A rule that requires a differently named check does not satisfy `safety.required_checks`."""
    check = _required_checks(tmp_path, github={RULES_ENDPOINT: [_status_rule("build", "lint")]})
    assert not check.ok and not check.skipped
    assert "'main' requires: build, lint" in check.detail
    assert "required contexts do not include 'ci'" in check.detail


def test_required_check_rule_without_context_fails(tmp_path):
    check = _required_checks(tmp_path, github={RULES_ENDPOINT: [_status_rule()]})
    assert not check.ok and "names no status check context" in check.detail


def test_required_check_enforcement_disabled_fails_and_names_the_ruleset(tmp_path):
    """GitHub lists only active rules, so a disabled ruleset shows as "nothing required";
    the listing is consulted so the remedy can say the ruleset exists but is off."""
    check = _required_checks(
        tmp_path,
        github={RULES_ENDPOINT: [], RULESETS_ENDPOINT: [_ruleset(enforcement="disabled")]},
    )
    assert not check.ok and not check.skipped
    assert "no rule requires a status check on 'main'" in check.detail
    assert "ruleset 'main' (#22792049) exists but its enforcement is 'disabled'" in check.detail


def test_required_check_missing_rule_stays_a_failure_when_the_listing_is_denied(tmp_path):
    """The ruleset listing only enriches the remedy; a denied listing is not a SKIP."""
    check = _required_checks(
        tmp_path,
        github={
            RULES_ENDPOINT: [],
            RULESETS_ENDPOINT: (1, "gh: Resource not accessible by integration (HTTP 403)"),
        },
    )
    assert not check.ok and check.required and not check.skipped
    assert "no rule requires a status check on 'main'" in check.detail
    assert "settings/rules" in check.detail


def test_required_check_ruleset_read_as_not_active_fails(tmp_path):
    """Belt and braces: the ruleset's own enforcement field is checked too."""
    check = _required_checks(tmp_path, github={RULESET_ENDPOINT: _ruleset(enforcement="evaluate")})
    assert not check.ok
    assert "ruleset 'main' (#22792049) enforcement is 'evaluate', not 'active'" in check.detail


def test_required_check_bypass_actor_fails(tmp_path):
    check = _required_checks(
        tmp_path,
        github={
            RULESET_ENDPOINT: _ruleset(
                bypass_actors=[
                    {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
                ],
                can_bypass="always",
            )
        },
    )
    assert not check.ok and not check.skipped
    assert "can be bypassed by: RepositoryRole 5 (always) (this token: always)" in check.detail
    assert check.detail.startswith("'main' requires: ci via")


def test_required_check_read_denied_is_skipped_not_failed(tmp_path):
    """A token that may not read rulesets has not shown anything: SKIP, and doctor still passes."""
    for stderr in (
        "gh: Bad credentials (HTTP 401)",
        "gh: Must have admin rights to Repository. (HTTP 403)",
        "gh: Not Found (HTTP 404)",
        "To get started with GitHub CLI, please run:  gh auth login",
    ):
        check = _required_checks(tmp_path, github={RULES_ENDPOINT: (1, stderr)})
        assert check.skipped and not check.ok and not check.required, (stderr, check)
        assert check.label == "SKIP"
        assert "cannot read the branch rules of owner/repo" in check.detail


def test_required_check_transient_failure_is_skipped(tmp_path):
    check = _required_checks(
        tmp_path, github={RULES_ENDPOINT: (1, "error connecting to api.github.com")}
    )
    assert check.skipped and "transient" in check.detail


def test_required_check_other_conclusive_failure_is_reported(tmp_path):
    """A malformed answer is neither a pass nor a permission problem: FAIL with the reason."""
    check = _required_checks(tmp_path, github={RULES_ENDPOINT: (1, "gh: something odd")})
    assert not check.ok and not check.skipped and "something odd" in check.detail


def test_required_check_classic_branch_protection_counts(tmp_path):
    """A repository still on classic branch protection is not a false alarm."""
    classic = {
        "required_status_checks": {"strict": False, "contexts": ["ci"], "checks": []},
        "enforce_admins": {"enabled": True},
    }
    check = _required_checks(tmp_path, github={RULES_ENDPOINT: [], PROTECTION_ENDPOINT: classic})
    assert check.ok, check
    assert check.detail == "'main' requires: ci via classic branch protection"

    classic["enforce_admins"] = {"enabled": False}
    classic["required_status_checks"]["contexts"] = ["build"]
    check = _required_checks(tmp_path, github={RULES_ENDPOINT: [], PROTECTION_ENDPOINT: classic})
    assert not check.ok
    assert "required contexts do not include 'ci'" in check.detail
    assert "bypassing" in check.detail

    # Protection that exists but requires no check is a conclusive negative.
    check = _required_checks(
        tmp_path,
        github={RULES_ENDPOINT: [], PROTECTION_ENDPOINT: {"enforce_admins": {"enabled": True}}},
    )
    assert not check.ok and not check.skipped
    assert "classic branch protection exists but requires no status check" in check.detail


def test_required_check_classic_protection_unreadable_is_skipped(tmp_path):
    """Rulesets require nothing and the classic read is forbidden: no conclusion either way."""
    check = _required_checks(
        tmp_path,
        github={
            RULES_ENDPOINT: [],
            PROTECTION_ENDPOINT: (1, "gh: Must have admin rights to Repository. (HTTP 403)"),
        },
    )
    assert check.skipped
    assert "cannot read its classic branch protection" in check.detail


def test_required_check_uses_configured_contexts(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text('{"version": 1, "safety": {"required_checks": ["build", "ci"]}}')
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    check = {r.name: r for r in d.run_all()}["default branch requires checks"]
    assert not check.ok and "do not include 'build'" in check.detail

    # An empty list requires only that *some* check is required.
    cfg.write_text('{"version": 1, "safety": {"required_checks": []}}')
    d = Doctor(
        config_path=str(cfg),
        cwd=str(tmp_path),
        runner=_runner_factory(github={RULES_ENDPOINT: [_status_rule("build")]}),
    )
    check = {r.name: r for r in d.run_all()}["default branch requires checks"]
    assert check.ok and "'main' requires: build" in check.detail


def test_required_check_reads_the_default_branch_not_main(tmp_path):
    calls = []
    check = _required_checks(
        tmp_path,
        default_branch="trunk",
        calls=calls,
        github={"repos/owner/repo/rules/branches/trunk": [_status_rule("ci")]},
    )
    assert check.ok and "'trunk' requires: ci" in check.detail
    assert not any(argv[-1] == RULES_ENDPOINT for argv in calls)


def test_required_check_skipped_when_prerequisites_failed(tmp_path):
    check = _required_checks(tmp_path, git_remote="git@gitlab.com:o/r.git")
    assert check.skipped and check.detail == "(GitHub remote check failed)"

    cfg = tmp_path / "c.json"
    cfg.write_text('{"version": 1, "execution": {"allow_merge": true}}')
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    check = {r.name: r for r in d.run_all()}["default branch requires checks"]
    assert check.skipped and check.detail == "(config check failed)"
