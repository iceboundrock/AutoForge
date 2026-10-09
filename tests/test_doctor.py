"""Doctor: read-only checks with a fake command runner."""

import json

import pytest

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
    """``bypass_actors=None`` is the field GitHub withholds from a token without
    write access to the ruleset (the read itself still succeeds)."""
    data = {
        "id": ruleset_id,
        "name": "main",
        "target": "branch",
        "source_type": "Repository",
        "source": "owner/repo",
        "enforcement": enforcement,
        "bypass_actors": None if bypass_actors is None else list(bypass_actors),
        "current_user_can_bypass": can_bypass,
        "_links": {"html": {"href": f"https://github.com/owner/repo/rules/{ruleset_id}"}},
    }
    if bypass_actors is None:
        del data["bypass_actors"]
    return data


# What a healthy repository answers: one active ruleset requiring `ci` on `main`.
HEALTHY_GITHUB = {
    RULES_ENDPOINT: [{"type": "deletion", "ruleset_id": 22792049}, _status_rule("ci")],
    RULESET_ENDPOINT: _ruleset(),
    RULESETS_ENDPOINT: [_ruleset()],
    PROTECTION_ENDPOINT: (1, "gh: Branch not protected (HTTP 404)"),
}


# What a recent enough `gh --version` prints (first line).
GH_VERSION_LINE = "gh version 2.48.0 (2024-04-09)"
# What `opencode --version` prints on a supported CLI (#186).
OPENCODE_VERSION_LINE = "opencode v2.0.23"
# The OpenCode commands the configs below name.
OPENCODE_COMMANDS = ("opencode", "epic-oc")


def _runner_factory(
    git_remote="https://github.com/owner/repo.git",
    fail=(),
    github=None,
    default_branch="main",
    calls=None,
    gh_version=GH_VERSION_LINE,
    opencode_version=OPENCODE_VERSION_LINE,
    opencode_code=0,
):
    """Fake runner. ``github`` maps a `gh api` endpoint to its JSON payload, or to
    an ``(exit_code, stderr)`` pair for a failed read; ``--paginate --slurp``
    listings are wrapped in one page the way `gh` does. ``opencode_version``
    is what an OpenCode command's ``--version`` prints (``None``: the binary
    is missing), with exit status ``opencode_code``."""
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
        elif argv == ["gh", "--version"]:
            out = gh_version + "\nhttps://github.com/cli/cli/releases/tag/v2.48.0"
        elif argv[0] in OPENCODE_COMMANDS and argv[1:] == ["--version"]:
            if opencode_version is None:
                raise FileNotFoundError(2, "No such file or directory", argv[0])
            return ExecutionResult(
                argv, req.cwd, opencode_code, opencode_version + "\n", "", "t", "t"
            )
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
        "agent 'claude' available (analyze_execute, fix)",
        "agent 'opencode' available (review_round_1, review_round_2_5, review_round_6_plus, "
        "replan_reexecute, update_epic)",
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


def test_doctor_reports_the_limits_of_every_reachable_profile(tmp_path):
    """#193: informational, both values for each profile a run can launch."""
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory())
    row = {r.name: r for r in d.run_all()}["profile limits"]
    assert row.ok and not row.required
    assert "analyze_execute: idle timeout 900s, max runtime unset" in row.detail
    assert "update_epic: idle timeout 900s, max runtime unset" in row.detail


def test_doctor_shows_the_loop_detection_mode_and_thresholds(tmp_path):
    """#194: informational; the default is kill (#199)."""
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory())
    row = {r.name: r for r in d.run_all()}["loop detection"]
    assert row.ok and not row.required
    assert row.detail == (
        "loop detection kill (cycles of up to 4 actions x8, no new action for 1800s, "
        "repeated lines x200)"
    )
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"version": 1, "execution": {"loop_detection": {"mode": "warn"}}}))
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    assert {r.name: r for r in d.run_all()}["loop detection"].detail.startswith(
        "loop detection warn ("
    )


def test_doctor_fails_a_post_merge_profile_a_run_would_refuse_to_launch(tmp_path):
    """`update_epic` is not a required profile, so the config check passes
    it; a text-mode Claude there with no ceiling would only fail after a
    merge, which is exactly what doctor exists to say beforehand."""
    cfg = tmp_path / "c.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "update_epic": {
                        "provider": "claude",
                        "model": "fable",
                        "effort": "high",
                        "command": "claude",
                        "options": {"output_format": "text"},
                    }
                },
            }
        )
    )
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    results = {r.name: r for r in d.run_all()}
    assert results["config"].ok
    row = results["profile limits"]
    assert not row.ok and row.required and "max_runtime_seconds" in row.detail


def test_failures_are_reported_not_raised(tmp_path):
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory(fail=("gh auth", "opencode")))
    results = {r.name: r for r in d.run_all()}
    assert not results["gh authenticated"].ok
    opencode = (
        "agent 'opencode' available (review_round_1, review_round_2_5, review_round_6_plus, "
        "replan_reexecute, update_epic)"
    )
    assert not results[opencode].ok
    assert results["git available"].ok


# -- `gh` minimum version (issue #43) ------------------------------------------------------
# Every paginated read (`gh api --paginate --slurp`) needs gh >= 2.48.0; an
# older gh would fail those reads conclusively at the merge gate instead.
def _gh_check(tmp_path, gh_version):
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory(gh_version=gh_version))
    return {r.name: r for r in d.run_all()}["gh available"]


@pytest.mark.parametrize(
    "line",
    ["gh version 2.48.0 (2024-04-09)", "gh version 2.100.0 (2026-09-03)", "gh version 3.0.0"],
)
def test_gh_at_or_above_the_minimum_passes(tmp_path, line):
    result = _gh_check(tmp_path, line)
    assert result.ok and result.detail == line


@pytest.mark.parametrize(
    "line",
    ["gh version 2.47.9 (2024-03-20)", "gh version 1.99.0", "gh version 2.9.0 (2022-01-01)"],
)
def test_gh_below_the_minimum_fails_and_names_it(tmp_path, line):
    result = _gh_check(tmp_path, line)
    assert not result.ok
    assert line in result.detail and "2.48.0" in result.detail and "--slurp" in result.detail


@pytest.mark.parametrize("line", ["", "gh 2.48.0", "version 2.48.0", "gh version two"])
def test_an_unreadable_gh_version_fails_rather_than_passes(tmp_path, line):
    result = _gh_check(tmp_path, line)
    assert not result.ok and "2.48.0" in result.detail


def test_a_missing_gh_is_reported_as_before(tmp_path):
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory(fail=("gh --version",)))
    result = {r.name: r for r in d.run_all()}["gh available"]
    assert not result.ok and "boom" in result.detail


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


def test_doctor_requires_the_same_profiles_as_a_remote_run(tmp_path):
    """#7 (item 3): `doctor` and the engine share one REQUIRED_PROFILES.

    The two copies had drifted: `doctor` did not require `replan_reexecute`,
    so it passed a config that `ControllerEngine.validate_config` refuses.
    """
    from autoforge.profiles import REQUIRED_PROFILES

    assert "replan_reexecute" in REQUIRED_PROFILES
    cfg = tmp_path / "c.json"
    cfg.write_text('{"version": 1, "profiles": {"replan_reexecute": {"model": ""}}}')
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    results = {r.name: r for r in d.run_all()}
    assert not results["config"].ok and "replan_reexecute" in results["config"].detail


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


def test_required_check_hidden_bypass_actors_is_skipped_never_ok(tmp_path):
    """A token without write access to the ruleset reads the rule but not who may bypass
    it: GitHub answers 200 with no `bypass_actors` field. That is not "no bypass actors"."""
    check = _required_checks(tmp_path, github={RULESET_ENDPOINT: _ruleset(bypass_actors=None)})
    assert check.skipped and not check.ok and not check.required, check
    assert check.label == "SKIP"
    assert check.detail.startswith("'main' requires: ci via ruleset #22792049")
    assert "bypass actors of ruleset 'main' (#22792049) are not visible to this token" in (
        check.detail
    )
    assert "write access to the ruleset" in check.detail
    assert "'no bypass actors' is unverified" in check.detail

    # The visible half still decides when it is a problem: hidden actors never
    # soften a missing context or a non-active ruleset into a SKIP.
    check = _required_checks(
        tmp_path,
        github={
            RULES_ENDPOINT: [_status_rule("build")],
            RULESET_ENDPOINT: _ruleset(bypass_actors=None),
        },
    )
    assert not check.ok and not check.skipped and check.required
    assert "required contexts do not include 'ci'" in check.detail
    assert "are not visible to this token" in check.detail

    check = _required_checks(
        tmp_path, github={RULESET_ENDPOINT: _ruleset(enforcement="evaluate", bypass_actors=None)}
    )
    assert not check.ok and not check.skipped
    assert "enforcement is 'evaluate', not 'active'" in check.detail


def test_required_check_token_that_can_bypass_fails_even_when_actors_are_hidden(tmp_path):
    """`current_user_can_bypass` is returned to every caller; a token that may bypass the
    rule is a bypass actor whether or not GitHub shows the list."""
    for actors in (None, ()):
        check = _required_checks(
            tmp_path,
            github={RULESET_ENDPOINT: _ruleset(bypass_actors=actors, can_bypass="always")},
        )
        assert not check.ok and not check.skipped and check.required, (actors, check)
        assert "ruleset 'main' (#22792049) can be bypassed by this token (always)" in check.detail


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


def test_premerge_verification_reported_and_never_run(tmp_path):
    """#42: doctor names the controller's own pre-merge evidence without executing it."""
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory())
    check = {r.name: r for r in d.run_all()}["pre-merge verification"]
    assert check.ok and not check.required
    assert "check definition compared to the base branch's run (ci)" in check.detail
    assert "no merge.verification_commands" in check.detail
    assert "consider merge.verification_commands" not in check.detail  # gate closed

    cfg = tmp_path / "c.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "safety": {"allow_merge": True, "verify_check_definition": False},
                "merge": {"verification_commands": [["false"], ["make", "check"]]},
            }
        )
    )
    calls: list = []
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory(calls=calls))
    check = {r.name: r for r in d.run_all()}["pre-merge verification"]
    assert "check definition NOT verified" in check.detail
    assert "local commands: false; make check" in check.detail
    assert not any(argv[:1] in (["false"], ["make"]) for argv in calls)

    cfg.write_text('{"version": 1, "safety": {"allow_merge": true}}')
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    check = {r.name: r for r in d.run_all()}["pre-merge verification"]
    assert "consider merge.verification_commands" in check.detail

    cfg.write_text('{"version": 1, "execution": {"allow_merge": true}}')
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    check = {r.name: r for r in d.run_all()}["pre-merge verification"]
    assert check.detail == "(config check failed)" and not check.ok and not check.required


def test_state_dir_on_a_filesystem_without_hard_links_names_the_requirement(tmp_path, monkeypatch):
    """#120: the state-directory probe is an exclusive create, published by
    link(2) like every run artifact, so ``doctor`` fails it where a run's
    run-log probe would fail. The detail names the missing hard links and
    the remedy instead of only a refused write, and the probe is not left
    behind."""
    import errno
    import os

    import autoforge.safefs as safefs

    def no_hard_links(src, dst, *args, **kwargs):
        raise OSError(errno.EPERM, os.strerror(errno.EPERM), os.fspath(dst))

    monkeypatch.setattr(safefs, "_O_TMPFILE", 0)
    monkeypatch.setattr(safefs.os, "link", no_hard_links)
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory())
    result = d.check_state_dir()
    assert not result.ok
    assert result.detail.startswith(f"{tmp_path / '.autoforge'}: cannot create ")
    assert "makes no hard link here" in result.detail
    assert "state directory, run logs included, needs a filesystem with hard links" in (
        result.detail
    )
    assert list((tmp_path / ".autoforge").iterdir()) == []


# -- agent rows derived from the reachable profiles; Pi checks (#129) ---------------
PI_LABEL = "agent 'pi' available (review_round_2_5)"
PI_AUTH = "agent 'pi' auth ready (review_round_2_5)"
OAUTH_READY = '{"status":"ready","provider":"openai","authType":"oauth"}'
# A planted "secret" Pi might print on stderr; no row may ever quote it.
PLANTED = "sk-planted-SECRET-0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_real_pi_home(tmp_path, monkeypatch):
    """Pi's agent directory is a temp one: a test never looks at the operator's ``~/.pi``."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))


def _write_config(tmp_path, profiles, extra=None):
    data = {"version": 1, "profiles": profiles}
    data.update(extra or {})
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data))
    return str(path)


def _pi_profile(**kwargs):
    profile = {"provider": "pi", "model": "openai/gpt-5.6-terra", "effort": "high"}
    profile.update(kwargs)
    return profile


def _pi_runner(version="1.0.0", auth=(0, OAUTH_READY), requests=None, version_code=0):
    """The healthy fake, plus a scripted `pi`; ``requests`` records every Pi probe."""
    base = _runner_factory()

    def runner(req):
        argv = req.command
        if argv[0] != "pi":
            return base(req)
        if requests is not None:
            requests.append(req)
        if argv[1:] == ["--version"]:
            if version is None:
                raise FileNotFoundError("pi")
            return ExecutionResult(argv, req.cwd, version_code, version + "\n", PLANTED, "t", "t")
        code, stdout = auth
        return ExecutionResult(argv, req.cwd, code, stdout, PLANTED, "t", "t")

    return runner


def _pi_doctor(tmp_path, profile=None, **kwargs):
    config = _write_config(tmp_path, {"review_round_2_5": profile or _pi_profile()})
    d = Doctor(config_path=config, cwd=str(tmp_path), runner=_pi_runner(**kwargs))
    results = d.run_all()
    for r in results:
        assert PLANTED not in r.name and PLANTED not in r.detail, r
    return {r.name: r for r in results}


OPENCODE_LABEL = (
    "agent 'opencode' available (review_round_1, review_round_2_5, review_round_6_plus, "
    "replan_reexecute, update_epic)"
)


def _opencode_row(tmp_path, **kwargs):
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory(**kwargs))
    results = d.run_all()
    for r in results:
        assert PLANTED not in r.name and PLANTED not in r.detail, r
    return {r.name: r for r in results}[OPENCODE_LABEL]


@pytest.mark.parametrize(
    ("version", "shown"),
    [
        ("opencode v2.0.23", "opencode 2.0.23"),
        ("opencode v2.0.0", "opencode 2.0.0"),
        ("v2.1.0", "opencode 2.1.0"),
        ("2.0.23", "opencode 2.0.23"),
        ("opencode v3.0.0", "opencode 3.0.0"),
    ],
)
def test_opencode_at_or_above_the_minimum_passes(tmp_path, version, shown):
    row = _opencode_row(tmp_path, opencode_version=version)
    assert row.ok and row.detail == shown


@pytest.mark.parametrize("version", ["1.18.34", "opencode v1.99.0", "opencode v2.0.0-beta.1"])
def test_opencode_below_the_minimum_fails_and_names_it(tmp_path, version):
    row = _opencode_row(tmp_path, opencode_version=version)
    assert not row.ok and "opencode >= 2.0.0 is required" in row.detail


def test_an_opencode_1x_names_the_release_it_found(tmp_path):
    row = _opencode_row(tmp_path, opencode_version="1.18.34")
    assert row.detail.startswith("opencode 1.18.34: ")


@pytest.mark.parametrize("version", ["", "opencode", "opencode v2.0", "opencode version 1.0"])
def test_an_unreadable_opencode_version_fails_rather_than_passes(tmp_path, version):
    row = _opencode_row(tmp_path, opencode_version=version)
    assert not row.ok and "cannot read the opencode version" in row.detail


@pytest.mark.parametrize(
    "version", [f"token {PLANTED}", f"opencode v2.0.23\nOPENAI_API_KEY={PLANTED}", PLANTED]
)
def test_an_unreadable_opencode_version_is_not_quoted(tmp_path, version):
    # `_opencode_row` asserts PLANTED is absent from every row.
    row = _opencode_row(tmp_path, opencode_version=version)
    assert not row.ok and "output not shown" in row.detail and ">= 2.0.0" in row.detail


def test_an_opencode_pre_release_suffix_is_not_quoted(tmp_path):
    row = _opencode_row(tmp_path, opencode_version=f"opencode v2.0.0-{PLANTED}")
    assert not row.ok and row.detail.startswith("opencode 2.0.0 (pre-release): ")


def test_a_failing_opencode_version_does_not_quote_its_output(tmp_path):
    row = _opencode_row(tmp_path, opencode_version=PLANTED, opencode_code=3)
    assert not row.ok and "exited 3" in row.detail and "output not shown" in row.detail


def test_a_missing_opencode_fails(tmp_path):
    row = _opencode_row(tmp_path, opencode_version=None)
    assert not row.ok and "FileNotFoundError" in row.detail


def test_remote_rows_name_only_the_reachable_clis(tmp_path):
    """A REMOTE run routed entirely through OpenCode never needs `claude`."""
    oc = {"provider": "opencode", "model": "openai/gpt-5.6-luna"}
    config = _write_config(tmp_path, {"analyze_execute": oc, "fix": oc})
    calls = []
    d = Doctor(config_path=config, cwd=str(tmp_path), runner=_runner_factory(calls=calls))
    names = [r.name for r in d.run_all()]
    assert not any("claude" in n for n in names), names
    assert not any(argv[0] == "claude" for argv in calls), calls
    assert any(n.startswith("agent 'opencode' available (analyze_execute, fix,") for n in names)


def test_remote_rows_include_update_epic(tmp_path):
    config = _write_config(tmp_path, {"update_epic": {"command": "epic-oc"}})
    d = Doctor(config_path=config, cwd=str(tmp_path), runner=_runner_factory())
    results = {r.name: r for r in d.run_all()}
    assert results["agent 'epic-oc' available (update_epic)"].ok


def test_an_all_scripted_config_requires_no_agent_cli(tmp_path):
    from autoforge.profiles import REQUIRED_PROFILES

    scripted = {"provider": "scripted", "command": "/bin/true"}
    profiles = {name: scripted for name in REQUIRED_PROFILES + ["update_epic"]}
    calls = []
    d = Doctor(
        config_path=_write_config(tmp_path, profiles),
        cwd=str(tmp_path),
        runner=_runner_factory(calls=calls),
    )
    results = {r.name: r for r in d.run_all()}
    row = results["agent CLI available"]
    assert row.ok and not row.required
    assert not any(argv[0] in ("claude", "opencode", "/bin/true") for argv in calls), calls


def test_no_agent_rows_when_the_config_fails(tmp_path):
    config = _write_config(tmp_path, {"fix": {"effort": "ultra"}})
    d = Doctor(config_path=config, cwd=str(tmp_path), runner=_runner_factory())
    names = [r.name for r in d.run_all()]
    assert not any(n.startswith("agent ") for n in names), names


def test_a_healthy_pi_profile_passes_and_bounds_its_claim(tmp_path):
    results = _pi_doctor(tmp_path)
    assert results[PI_LABEL].ok and results[PI_LABEL].detail == "pi 1.0.0"
    auth = results[PI_AUTH]
    assert auth.ok and auth.required
    assert auth.detail.startswith("openai/gpt-5.6-terra: ")
    # `ready` proves a credential exists for the provider, not that the model is usable.
    assert "provider 'openai' only" in auth.detail
    assert "cannot be checked" in auth.detail
    assert all(r.ok for r in results.values()), [r for r in results.values() if not r.ok]


@pytest.mark.parametrize("version", ["1.0.0", "1.0.1", "1.12.0", "2.0.0", "10.0.0"])
def test_pi_at_or_above_the_minimum_passes(tmp_path, version):
    assert _pi_doctor(tmp_path, version=version)[PI_LABEL].ok


@pytest.mark.parametrize("version", ["0.99.0", "0.9.9", "1.0.0-rc.1", "1.0.0-alpha"])
def test_pi_below_the_minimum_fails_and_names_it(tmp_path, version):
    row = _pi_doctor(tmp_path, version=version)[PI_LABEL]
    assert not row.ok and ">= 1.0.0" in row.detail


@pytest.mark.parametrize("version", ["", "pi 1.0.0", "1.0", "unknown"])
def test_an_unreadable_pi_version_fails_rather_than_passes(tmp_path, version):
    row = _pi_doctor(tmp_path, version=version)[PI_LABEL]
    assert not row.ok and "cannot read the pi version" in row.detail


@pytest.mark.parametrize(
    "version", [f"token {PLANTED}", f"1.0.0\nOPENAI_API_KEY={PLANTED}", PLANTED]
)
def test_an_unreadable_pi_version_is_not_quoted(tmp_path, version):
    # `_pi_doctor` asserts PLANTED is absent from every row.
    row = _pi_doctor(tmp_path, version=version)[PI_LABEL]
    assert not row.ok and "cannot read the pi version" in row.detail
    assert "output not shown" in row.detail and ">= 1.0.0" in row.detail


def test_a_pi_pre_release_suffix_is_not_quoted(tmp_path):
    row = _pi_doctor(tmp_path, version=f"1.0.0-{PLANTED}")[PI_LABEL]
    assert not row.ok and row.detail.startswith("pi 1.0.0 (pre-release): ")


def test_a_failing_pi_version_does_not_quote_its_output(tmp_path):
    results = _pi_doctor(tmp_path, version_code=3)
    assert not results[PI_LABEL].ok and "exited 3" in results[PI_LABEL].detail
    assert results[PI_AUTH].skipped and not results[PI_AUTH].required


def test_a_missing_pi_fails_and_skips_the_auth_probe(tmp_path):
    requests = []
    results = _pi_doctor(tmp_path, version=None, requests=requests)
    assert not results[PI_LABEL].ok and "FileNotFoundError" in results[PI_LABEL].detail
    assert results[PI_AUTH].skipped
    assert [r.command[1:] for r in requests] == [["--version"]]


def test_the_auth_probe_is_read_only(tmp_path):
    requests = []
    _pi_doctor(tmp_path, requests=requests)
    argv = requests[-1].command
    assert argv == [
        "pi",
        "auth",
        "check",
        "--model",
        "openai/gpt-5.6-terra",
        "--json",
        "--no-refresh",
    ]
    for req in requests:
        assert "--credentials" not in req.command
        assert not any(a.startswith("print-") for a in req.command)
        assert "login" not in req.command


def test_the_auth_probe_runs_under_the_launch_allowlist(tmp_path, monkeypatch):
    """An `OPENAI_API_KEY` only the operator's shell holds must not make Pi report ready."""
    from autoforge.executor import select_environment

    monkeypatch.setenv("OPENAI_API_KEY", "sk-shell-only")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/tmp/pi-agent")
    requests = []
    _pi_doctor(tmp_path, requests=requests)
    for req in requests:
        env = select_environment(req.env_allowlist)
        assert "OPENAI_API_KEY" not in env
        assert env.get("PI_CODING_AGENT_DIR") == "/tmp/pi-agent"

    # ... unless the operator deliberately forwards it.
    requests.clear()
    config = _write_config(
        tmp_path,
        {"review_round_2_5": _pi_profile()},
        {"execution": {"env_allowlist_extra": ["OPENAI_API_KEY"]}},
    )
    Doctor(config_path=config, cwd=str(tmp_path), runner=_pi_runner(requests=requests)).run_all()
    assert requests
    for req in requests:
        assert select_environment(req.env_allowlist)["OPENAI_API_KEY"] == "sk-shell-only"


@pytest.mark.parametrize(
    "auth, needle",
    [
        (
            (1, '{"status":"not_ready","provider":"openai","reason":"credentials_not_configured"}'),
            "/login openai",
        ),
        (
            (1, '{"status":"not_ready","provider":"openai","reason":"provider_not_found"}'),
            "provider_not_found",
        ),
        (
            (1, '{"status":"not_ready","provider":"openai","reason":"credential_not_available"}'),
            "credential_not_available",
        ),
        ((2, '{"status":"invalid","provider":"openai","reason":"invalid_state"}'), "invalid"),
        ((2, '{"status":"invalid","provider":"openai/gpt-5.6-terra"}'), "invalid"),
    ],
)
def test_a_pi_that_is_not_ready_fails_with_a_remedy(tmp_path, auth, needle):
    row = _pi_doctor(tmp_path, auth=auth)[PI_AUTH]
    assert not row.ok and row.required
    assert needle in row.detail, row.detail


def test_the_login_remedy_names_the_configured_provider(tmp_path):
    profile = _pi_profile(model="openai-codex/gpt-5.6")
    auth = (
        1,
        '{"status":"not_ready","provider":"openai-codex","reason":"credentials_not_configured"}',
    )
    row = _pi_doctor(tmp_path, profile=profile, auth=auth)[PI_AUTH]
    assert not row.ok and "/login openai-codex" in row.detail
    assert "never logs in for you" in row.detail


@pytest.mark.parametrize(
    "auth",
    [
        (0, ""),
        (2, ""),  # an AuthCommandError: stderr only
        (0, "not json"),
        (0, OAUTH_READY + "\n" + OAUTH_READY),
        (1, OAUTH_READY),  # exit code disagrees with the status
        (
            0,
            '{"status":"ready","provider":"openai","authType":"oauth","credentials":"'
            + PLANTED
            + '"}',
        ),
        (0, '{"status":"ready","provider":"openai","authType":"oauth","' + PLANTED + '":1}'),
        (0, '{"status":"ready","provider":"' + PLANTED + ' x","authType":"oauth"}'),
    ],
)
def test_unreadable_pi_auth_output_is_a_failure_not_a_crash(tmp_path, auth):
    row = _pi_doctor(tmp_path, auth=auth)[PI_AUTH]
    assert not row.ok and row.required
    assert "without a result AutoForge can read" in row.detail, row.detail


def test_a_provider_mismatch_fails(tmp_path):
    auth = (0, '{"status":"ready","provider":"anthropic","authType":"oauth"}')
    row = _pi_doctor(tmp_path, auth=auth)[PI_AUTH]
    assert not row.ok and "anthropic" in row.detail and "openai" in row.detail


@pytest.mark.parametrize(
    "require_oauth, auth_type, ok",
    [
        ("true", "oauth", True),
        ("true", "api_key", False),
        ("false", "oauth", True),
        ("false", "api_key", True),
        (None, "api_key", False),  # require_oauth defaults to true
    ],
)
def test_require_oauth_decides_whether_an_api_key_is_acceptable(
    tmp_path, require_oauth, auth_type, ok
):
    options = {} if require_oauth is None else {"require_oauth": require_oauth}
    auth = (0, f'{{"status":"ready","provider":"openai","authType":"{auth_type}"}}')
    row = _pi_doctor(tmp_path, profile=_pi_profile(options=options), auth=auth)[PI_AUTH]
    assert row.ok is ok, row.detail
    if not ok:
        assert "require_oauth: false" in row.detail


def test_pi_profiles_sharing_a_model_share_one_probe(tmp_path):
    pi = _pi_profile()
    config = _write_config(tmp_path, {"review_round_2_5": pi, "review_round_6_plus": pi})
    requests = []
    d = Doctor(config_path=config, cwd=str(tmp_path), runner=_pi_runner(requests=requests))
    results = {r.name: r for r in d.run_all()}
    assert results["agent 'pi' auth ready (review_round_2_5, review_round_6_plus)"].ok
    assert [r.command[1] for r in requests] == ["--version", "auth"]


@pytest.mark.parametrize(("version", "opencode_ok"), [("1.18.34", False), ("2.0.23", True)])
def test_a_command_shared_by_pi_and_opencode_must_pass_both_floors(tmp_path, version, opencode_ok):
    """The PR #188 review: a wrapper that a Pi and an OpenCode profile both
    name is launched by both adapters, so it gets both providers' checks,
    each labelled with its own profiles; Pi's floor passing does not stand
    in for OpenCode's."""
    wrapper = {"command": "agent-wrapper"}
    config = _write_config(
        tmp_path, {"review_round_1": wrapper, "review_round_2_5": _pi_profile(**wrapper)}
    )
    base = _runner_factory()
    probes = []

    def runner(req):
        argv = req.command
        if argv[0] != "agent-wrapper":
            return base(req)
        probes.append(argv[1])
        out = version if argv[1:] == ["--version"] else OAUTH_READY
        return ExecutionResult(argv, req.cwd, 0, out + "\n", "", "t", "t")

    d = Doctor(config_path=config, cwd=str(tmp_path), runner=runner)
    results = {r.name: r for r in d.run_all()}
    pi_row = results["agent 'agent-wrapper' available (review_round_2_5)"]
    assert pi_row.ok and pi_row.detail == f"pi {version}"
    assert results["agent 'agent-wrapper' auth ready (review_round_2_5)"].ok
    opencode_row = results["agent 'agent-wrapper' available (review_round_1)"]
    assert opencode_row.ok is opencode_ok
    if not opencode_ok:
        assert "opencode >= 2.0.0 is required" in opencode_row.detail
    assert sorted(probes) == ["--version", "--version", "auth"]


def test_local_doctor_checks_a_pi_reviewer(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    config = _write_config(
        tmp_path,
        {"review_round_1": _pi_profile()},
        {"local": {"max_fix_rounds": 0}},
    )
    base = _pi_runner()

    def runner(req):
        if req.command[:2] == ["git", "rev-parse"]:
            return ExecutionResult(req.command, req.cwd, 0, str(root) + "\n", "", "t", "t")
        return base(req)

    d = Doctor(config_path=config, cwd=str(root), state_dir=str(tmp_path / "state"), runner=runner)
    results = {r.name: r for r in d.run_local()}
    assert results["agent 'pi' available (review_round_1)"].ok
    assert results["agent 'pi' auth ready (review_round_1)"].ok
    assert results["loop detection"].detail.startswith("loop detection kill (")


# -- instructions Pi loads from outside the checkout (#132) -----------------------
PI_OUTSIDE = "pi loads no instructions from outside the checkout (review_round_2_5)"


def _pi_outside_row(tmp_path, profile=None):
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    config = _write_config(tmp_path, {"review_round_2_5": profile or _pi_profile()})
    d = Doctor(config_path=config, cwd=str(repo), runner=_pi_runner())
    return {r.name: r for r in d.run_all()}[PI_OUTSIDE]


def test_no_outside_instructions_is_ok(tmp_path):
    row = _pi_outside_row(tmp_path)
    assert row.ok and not row.required and row.detail == "(none found)"


@pytest.mark.parametrize("name", ["SYSTEM.md", "APPEND_SYSTEM.md", "AGENTS.md", "CLAUDE.md"])
def test_agent_dir_instructions_are_a_warning_naming_the_path(tmp_path, name):
    agent = tmp_path / "pi-agent"
    agent.mkdir()
    (agent / name).write_text("SECRET-INSTRUCTION")
    row = _pi_outside_row(tmp_path)
    assert row.label == "WARN" and str(agent / name) in row.detail
    assert "SECRET-INSTRUCTION" not in row.detail


def test_a_context_file_above_the_checkout_is_a_warning_and_the_first_name_wins(tmp_path):
    (tmp_path / "work" / "AGENTS.md").parent.mkdir(parents=True)
    (tmp_path / "work" / "AGENTS.md").write_text("x")
    (tmp_path / "work" / "CLAUDE.md").write_text("x")
    row = _pi_outside_row(tmp_path)
    assert row.label == "WARN" and str(tmp_path / "work" / "AGENTS.md") in row.detail
    assert "CLAUDE.md" not in row.detail
    # The checkout's own file is project data under review, not an outside one.
    (tmp_path / "work" / "AGENTS.md").unlink()
    (tmp_path / "work" / "CLAUDE.md").unlink()
    (tmp_path / "work" / "repo" / "AGENTS.md").write_text("x")
    assert _pi_outside_row(tmp_path).ok


def test_context_files_false_leaves_only_the_system_prompt_files(tmp_path):
    agent = tmp_path / "pi-agent"
    agent.mkdir()
    (agent / "AGENTS.md").write_text("x")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "AGENTS.md").write_text("x")
    profile = _pi_profile(options={"context_files": "false"})
    assert _pi_outside_row(tmp_path, profile).ok
    (agent / "SYSTEM.md").write_text("x")
    row = _pi_outside_row(tmp_path, profile)
    assert row.label == "WARN" and str(agent / "SYSTEM.md") in row.detail


def test_the_default_agent_dir_is_used_when_the_launch_does_not_pass_one(tmp_path, monkeypatch):
    monkeypatch.delenv("PI_CODING_AGENT_DIR")
    default = tmp_path / "home" / ".pi" / "agent"
    default.mkdir(parents=True)
    (default / "APPEND_SYSTEM.md").write_text("x")
    row = _pi_outside_row(tmp_path)
    assert row.label == "WARN" and str(default / "APPEND_SYSTEM.md") in row.detail


def test_no_outside_instructions_row_without_a_pi_profile(tmp_path):
    config = _write_config(tmp_path, {"analyze_execute": {"provider": "opencode"}})
    d = Doctor(config_path=config, cwd=str(tmp_path), runner=_runner_factory())
    assert not any("instructions" in r.name for r in d.run_all())


def test_a_forwarded_api_key_fails_an_oauth_profile_without_probing(tmp_path, monkeypatch):
    """The launch would refuse it (#132), so `doctor` does too; the value is never shown."""
    monkeypatch.setenv("OPENAI_API_KEY", PLANTED)
    requests = []
    config = _write_config(
        tmp_path,
        {"review_round_2_5": _pi_profile()},
        {"execution": {"env_allowlist_extra": ["OPENAI_API_KEY"]}},
    )
    d = Doctor(config_path=config, cwd=str(tmp_path), runner=_pi_runner(requests=requests))
    row = {r.name: r for r in d.run_all()}[PI_AUTH]
    assert not row.ok and row.required and "OPENAI_API_KEY is set" in row.detail
    assert PLANTED not in row.detail
    assert [r.command[1] for r in requests] == ["--version"]
