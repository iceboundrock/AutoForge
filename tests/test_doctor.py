"""Doctor: read-only checks with a fake command runner."""

import json

import pytest

import autoforge.doctor as doctor_module
import autoforge.executor as executor_module
from autoforge.doctor import Doctor
from autoforge.executor import ExecutionResult, execute
from autoforge.profiles import local_required_profiles
from autoforge.redaction import redact
from tests.conftest import is_shell_probe

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
    write access to the ruleset (the read itself still succeeds);
    ``can_bypass=None`` is an answer without ``current_user_can_bypass``."""
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
    if can_bypass is None:
        del data["current_user_can_bypass"]
    return data


# What a healthy repository answers: one active ruleset requiring `ci` on `main`.
HEALTHY_GITHUB = {
    RULES_ENDPOINT: [{"type": "deletion", "ruleset_id": 22792049}, _status_rule("ci")],
    RULESET_ENDPOINT: _ruleset(),
    RULESETS_ENDPOINT: [_ruleset()],
    PROTECTION_ENDPOINT: (1, "gh: Branch not protected (HTTP 404)"),
}


# The open PR the checks-read row probes (#291), and what `gh` answers for it.
PROBE_PR = "https://github.com/owner/repo/pull/7"
CHECK_RUN = {
    "__typename": "CheckRun",
    "name": "ci",
    "status": "COMPLETED",
    "conclusion": "SUCCESS",
    "workflowName": "CI",
}
STATUS_CONTEXT = {"__typename": "StatusContext", "context": "deploy", "state": "SUCCESS"}


def _probe_listing(*prs):
    """The candidate listing's GraphQL answer: ``(number, totalCount)`` per open PR."""
    nodes = [
        {
            "url": f"https://github.com/owner/repo/pull/{number}",
            "number": number,
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "oid": "a" * 40,
                            "statusCheckRollup": {"contexts": {"totalCount": total}},
                        }
                    }
                ]
            },
        }
        for number, total in prs
    ]
    return {"data": {"repository": {"pullRequests": {"nodes": nodes}}}}


HEALTHY_PROBE = _probe_listing((7, 1))
HEALTHY_PR_VIEWS = {PROBE_PR: [CHECK_RUN]}
# GitHub's answer to a fine-grained token reading a private PR's check runs (#291).
ROLLUP_REFUSED = (
    "GraphQL: Resource not accessible by personal access token "
    "(repository.pullRequest.statusCheckRollup.nodes.0.commit.statusCheckRollup.contexts.nodes.0)"
)
PLAN_LIMITED = (
    "gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)"
)


# What a recent enough `gh --version` prints (first line).
GH_VERSION_LINE = "gh version 2.48.0 (2024-04-09)"
# What `opencode --version` prints on a supported CLI (#186).
OPENCODE_VERSION_LINE = "opencode v2.0.23"
# The OpenCode commands the configs below name.
OPENCODE_COMMANDS = ("opencode", "epic-oc")
# The row of the Claude isolation check for the default profiles (#165).
CLAUDE_ISOLATION_ROW = "agent 'claude' keeps integrations off (analyze_execute, fix)"
# The row of the Claude configuration check for the default profiles (#165).
CLAUDE_CONFIGURATION_ROW = (
    "agent 'claude' configuration keeps the launch environment (analyze_execute, fix)"
)
# The part of `claude --help` that check reads.
CLAUDE_HELP = (
    "  --safe-mode                           Start with all customizations ...\n"
    "  --setting-sources <sources>           Comma-separated list of setting sources\n"
    "  --strict-mcp-config                   Only use MCP servers from --mcp-config\n"
)


def _runner_factory(
    git_remote="https://github.com/owner/repo.git",
    fail=(),
    github=None,
    default_branch="main",
    calls=None,
    gh_version=GH_VERSION_LINE,
    opencode_version=OPENCODE_VERSION_LINE,
    opencode_code=0,
    git_config="",
    claude_help=CLAUDE_HELP,
    checks_probe=HEALTHY_PROBE,
    pr_views=None,
):
    """Fake runner. ``github`` maps a `gh api` endpoint to its JSON payload, or to
    an ``(exit_code, stderr)`` pair for a failed read; ``--paginate --slurp``
    listings are wrapped in one page the way `gh` does. ``opencode_version``
    is what an OpenCode command's ``--version`` prints (``None``: the binary
    is missing), with exit status ``opencode_code``. ``git_config`` is what
    the launch preflight's ``git config --list --show-scope -z`` prints, and
    ``claude_help`` what ``claude --help`` does. ``checks_probe`` is the
    answer to the checks-read row's candidate listing (``gh api graphql``)
    and ``pr_views`` maps a PR URL to its status-check rollup (``gh pr
    view``), each an answer or an ``(exit_code, stderr)`` pair (#291)."""
    responses = dict(HEALTHY_GITHUB)
    responses.update(github or {})
    views = HEALTHY_PR_VIEWS if pr_views is None else pr_views

    def failed(argv, cwd, payload):
        code, stderr = payload
        return ExecutionResult(argv, cwd, code, "", stderr, "t", "t")

    def runner(req):
        if is_shell_probe(req):  # the launch preflight's shell startup probe: real shells
            return execute(req)
        argv = req.command
        if calls is not None:
            calls.append(argv)
        key = " ".join(argv[:2])
        if any(key.startswith(f) for f in fail):
            return ExecutionResult(argv, req.cwd, 1, "", "boom", "t", "t")
        if argv[0] == "git" and "config" in argv and "--show-scope" in argv:
            return ExecutionResult(argv, req.cwd, 0, git_config, "", "t", "t")
        if argv[:3] == ["git", "remote", "get-url"]:
            out = git_remote
        elif argv[:2] == ["git", "rev-parse"]:
            out = "/repo"
        elif argv[:3] == ["gh", "repo", "view"]:
            out = json.dumps(
                {"nameWithOwner": "owner/repo", "defaultBranchRef": {"name": default_branch}}
            )
        elif argv[:3] == ["gh", "api", "graphql"]:
            if isinstance(checks_probe, tuple):
                return failed(argv, req.cwd, checks_probe)
            out = json.dumps(checks_probe)
        elif argv[:3] == ["gh", "pr", "view"]:
            url = argv[3]
            if url not in views:
                return ExecutionResult(argv, req.cwd, 1, "", f"unscripted {url}", "t", "t")
            if isinstance(views[url], tuple):
                return failed(argv, req.cwd, views[url])
            row = {
                "url": url,
                "number": int(url.rsplit("/", 1)[1]),
                "title": "PR",
                "state": "OPEN",
                "headRefOid": "a" * 40,
                "baseRefName": "main",
                "headRefName": "feature",
            }
            if "statusCheckRollup" in argv[argv.index("--json") + 1].split(","):
                row["statusCheckRollup"] = views[url]
            out = json.dumps(row)
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
        elif argv == ["claude", "--help"]:
            out = claude_help
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


def _required_checks(tmp_path, gate_open=False, **kwargs):
    """The required-check row of a full doctor run; ``gate_open`` sets
    ``safety.allow_merge: true`` (the configuration's half of the gate)."""
    config = None
    if gate_open:
        cfg = tmp_path / "gate-open.json"
        cfg.write_text('{"version": 1, "safety": {"allow_merge": true}}')
        config = str(cfg)
    d = Doctor(config_path=config, cwd=str(tmp_path), runner=_runner_factory(**kwargs))
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
        "agent 'claude' keeps integrations off (analyze_execute, fix)",
        CLAUDE_CONFIGURATION_ROW,
        "agent 'opencode' available (review_round_1, review_round_2_5, review_round_6_plus, "
        "replan_reexecute, update_epic)",
        "agent 'opencode' loads no organization configuration (review_round_1, "
        "review_round_2_5, review_round_6_plus, replan_reexecute, update_epic)",
        "cwd is a git repository",
        "GitHub remote",
        "default branch requires checks",
        "pull request check runs readable",
        "state dir writable",
    ):
        assert expected in names
    others = [r for r in results if not r.name.startswith("outer sandbox: ")]
    assert all(r.ok for r in others), [r for r in others if not r.ok]
    # The default sandbox.mode is disabled: reported as a risk, never a failure
    # (ADR 0005 D12.4), and nothing of the sandbox rows fails doctor.
    sandbox = {r.name: r for r in results if r.name.startswith("outer sandbox: ")}
    mode = sandbox["outer sandbox: mode (disabled)"]
    assert mode.label == "WARN" and "logical isolation only" in mode.detail
    assert not any(r.label == "FAIL" for r in sandbox.values()), sandbox
    assert "owner/repo" in next(r.detail for r in results if r.name == "GitHub remote")
    assert (tmp_path / ".autoforge").is_dir()
    assert not any(p.name.startswith(".doctor-") for p in (tmp_path / ".autoforge").iterdir())


@pytest.mark.parametrize(
    ("help_text", "missing"),
    [
        (
            "  --strict-mcp-config  Only use MCP servers from --mcp-config\n"
            "  --setting-sources <sources>\n",
            "--safe-mode",
        ),
        # A flag named only inside another's description is not offered.
        (
            "  --bare  Minimal mode, unlike --safe-mode-ish\n  --strict-mcp-config\n"
            "  --setting-sources <sources>\n",
            "--safe-mode",
        ),
        (
            "  --safe-mode  Start with all customizations disabled\n"
            "  --setting-sources <sources>\n",
            "--strict-mcp-config",
        ),
        (
            "  --safe-mode  Start with all customizations disabled\n  --strict-mcp-config\n",
            "--setting-sources",
        ),
    ],
)
def test_doctor_fails_a_claude_without_the_isolation_flags(tmp_path, help_text, missing):
    """#165: every Claude launch passes --safe-mode, --strict-mcp-config and
    --setting-sources=user; a CLI without one would refuse every launch, so
    doctor says so first, by the flag's name, without quoting the help."""
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory(claude_help=help_text))
    row = {r.name: r for r in d.run_all()}[CLAUDE_ISOLATION_ROW]
    assert not row.ok and row.required
    assert missing in row.detail and "update Claude Code" in row.detail
    assert "Minimal mode" not in row.detail and "customizations" not in row.detail


def test_doctor_fails_a_claude_whose_help_fails(tmp_path):
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory(fail=("claude --help",)))
    row = {r.name: r for r in d.run_all()}[CLAUDE_ISOLATION_ROW]
    assert not row.ok and "exited 1" in row.detail and "boom" not in row.detail


def test_doctor_reports_the_limits_of_every_reachable_profile(tmp_path):
    """#193: informational, both values for each profile a run can launch."""
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory())
    row = {r.name: r for r in d.run_all()}["profile limits"]
    assert row.ok and not row.required
    assert "analyze_execute: idle timeout 900s, max runtime unset" in row.detail
    assert "update_epic: idle timeout 900s, max runtime unset" in row.detail


def test_doctor_shows_the_loop_detection_mode_and_thresholds(tmp_path):
    """#194: informational; the default is kill, and warn where no real run
    has calibrated it yet (#199)."""
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory())
    row = {r.name: r for r in d.run_all()}["loop detection"]
    assert row.ok and not row.required
    assert row.detail == (
        "loop detection kill (warn on provider pi and in UPDATE_EPIC: not calibrated yet) "
        "(cycles of up to 4 actions x8, no new action for 1800s, repeated lines x200)"
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


def test_doctor_fails_a_config_whose_routing_a_remote_run_could_not_record(tmp_path):
    """A REMOTE run records its routing in its state file (#219), and `run`
    refuses a value that record could not hold (R1-F1): so does doctor's
    config row. A LOCAL run records none, and its doctor passes it."""
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"version": 1, "profiles": {"fix": {"model": "m" * 513}}}))
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    row = {r.name: r for r in d.run_all()}["config"]
    assert not row.ok and row.required
    assert row.detail.startswith("profiles.fix.model must be a string of at most 512 characters")
    assert d.config is None
    local = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    assert local.check_config(local_required_profiles).ok


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
    # Every REST `gh api` call is a GET: `doctor` never mutates.
    api_calls = [argv for argv in calls if argv[:2] == ["gh", "api"]]
    rest = [argv for argv in api_calls if argv[2] != "graphql"]
    assert rest
    assert not any(
        flag in argv for argv in rest for flag in ("-X", "--method", "-f", "-F", "--input")
    )
    # The checks-read row's candidate listing (#291) is GraphQL, which `gh`
    # POSTs with `-f` fields: a query, never a mutation, and no other method.
    graphql = [argv for argv in api_calls if argv[2] == "graphql"]
    assert len(graphql) == 1
    (query,) = [a for a in graphql[0] if a.startswith("query=")]
    assert query.startswith("query=query(") and "mutation" not in query
    assert not any(flag in graphql[0] for flag in ("-X", "--method", "-F", "--input"))


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


@pytest.mark.parametrize("gate_open", [False, True], ids=["gate-closed", "gate-open"])
def test_required_check_hidden_bypass_actors_when_github_says_this_token_never_bypasses(
    tmp_path, gate_open
):
    """#289 (review R4): with ``current_user_can_bypass: never`` the controller's own login
    cannot skip the rule, so the hidden list is a SKIP whatever the gate, never an OK."""
    check = _required_checks(
        tmp_path, gate_open=gate_open, github={RULESET_ENDPOINT: _ruleset(bypass_actors=None)}
    )
    assert check.skipped and not check.ok and not check.required, check
    assert "'no bypass actors' is unverified" in check.detail


@pytest.mark.parametrize("gate_open", [False, True], ids=["gate-closed", "gate-open"])
def test_required_check_hidden_bypass_actors_without_a_word_on_this_token(tmp_path, gate_open):
    """#289 (review R4): GitHub withholds the bypass list *and* does not say whether this
    token may bypass the rule: the controller's own bypass is unverified. A FAIL with the
    merge gate open, a SKIP that says why with it closed; never an OK."""
    check = _required_checks(
        tmp_path,
        gate_open=gate_open,
        github={RULESET_ENDPOINT: _ruleset(bypass_actors=None, can_bypass=None)},
    )
    assert not check.ok
    assert check.detail.startswith("'main' requires: ci via ruleset #22792049")
    assert "GitHub did not say whether this token may bypass the rule" in check.detail
    assert "controller's own login can skip the required checks is unverified" in check.detail
    if gate_open:
        assert check.label == "FAIL" and check.required and not check.skipped
        assert "run doctor with a login that has write access to the ruleset" in check.detail
    else:
        assert check.label == "SKIP" and check.skipped
        assert "the merge gate is closed" in check.detail


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


DENIED_RULE_READS = (
    "gh: Bad credentials (HTTP 401)",
    "gh: Must have admin rights to Repository. (HTTP 403)",
    "gh: Not Found (HTTP 404)",
    "To get started with GitHub CLI, please run:  gh auth login",
)


def test_required_check_read_denied_is_skipped_not_failed(tmp_path):
    """With the merge gate closed, a token that may not read rulesets has not
    shown anything: SKIP, and doctor still passes."""
    for stderr in DENIED_RULE_READS:
        check = _required_checks(tmp_path, github={RULES_ENDPOINT: (1, stderr)})
        assert check.skipped and not check.ok and not check.required, (stderr, check)
        assert check.label == "SKIP"
        assert "cannot read the branch rules of owner/repo" in check.detail
        assert "the merge gate is closed" in check.detail


def test_required_check_read_denied_fails_with_the_merge_gate_open(tmp_path):
    """#289: with safety.allow_merge=true an unverifiable requirement is a FAIL, never a SKIP."""
    for stderr in DENIED_RULE_READS:
        check = _required_checks(tmp_path, gate_open=True, github={RULES_ENDPOINT: (1, stderr)})
        assert not check.ok and not check.skipped and check.required, (stderr, check)
        assert check.label == "FAIL"
        assert "cannot read the branch rules of owner/repo" in check.detail
        assert "safety.allow_merge=true" in check.detail


@pytest.mark.parametrize("gate_open", [False, True], ids=["gate-closed", "gate-open"])
@pytest.mark.parametrize(
    "github",
    [
        {RULES_ENDPOINT: (1, PLAN_LIMITED)},
        {RULES_ENDPOINT: [], PROTECTION_ENDPOINT: (1, PLAN_LIMITED)},
        {RULES_ENDPOINT: [_status_rule("ci")], RULESET_ENDPOINT: (1, PLAN_LIMITED)},
    ],
    ids=["rules", "classic-protection", "ruleset"],
)
def test_required_check_plan_limit_names_the_plan_never_the_token(tmp_path, github, gate_open):
    """#289: GitHub Free's 403 is the plan: named as such, never sent to token permissions.

    A FAIL with the merge gate open (nothing can require the check), a SKIP
    that says why with it closed; never an unverified OK.
    """
    check = _required_checks(tmp_path, gate_open=gate_open, github=github)
    assert not check.ok
    assert "does not offer rulesets or branch protection" in check.detail
    assert "this is the plan, not the token" in check.detail
    assert "GitHub Pro, Team or Enterprise" in check.detail
    # Never the token's permissions.
    assert "this token cannot read" not in check.detail
    assert "Administration" not in check.detail
    assert "readable" not in check.detail and "a login that may read" not in check.detail
    if gate_open:
        assert check.label == "FAIL" and check.required and not check.skipped
        # The way out is the plan's (#289 review R5).
        assert (
            "make the repository public or move it to a plan that offers rules, or close the gate"
            in check.detail
        )
    else:
        assert check.label == "SKIP" and check.skipped
        assert "the merge gate is closed" in check.detail


def test_required_check_plan_limited_ruleset_listing_is_a_hint_only(tmp_path):
    """The listing only makes the remedy concrete: its plan 403 changes no answer."""
    check = _required_checks(
        tmp_path, github={RULES_ENDPOINT: [], RULESETS_ENDPOINT: (1, PLAN_LIMITED)}
    )
    assert not check.ok and not check.skipped and check.required
    assert "no rule requires a status check on 'main'" in check.detail
    assert "Upgrade to GitHub" not in check.detail


@pytest.mark.parametrize(
    "github",
    [
        {RULES_ENDPOINT: (1, "error connecting to api.github.com")},
        {RULES_ENDPOINT: [_status_rule("ci")], RULESET_ENDPOINT: (1, "HTTP 502: Bad Gateway")},
        {RULES_ENDPOINT: [], PROTECTION_ENDPOINT: (1, "error connecting to api.github.com")},
    ],
    ids=["rules", "ruleset", "classic-protection"],
)
def test_required_check_transient_failure_fails_with_the_gate_open(tmp_path, github):
    """#289 (review R1): with safety.allow_merge=true a requirement doctor could not
    verify, for whatever reason, is a FAIL: a transient failure verified nothing."""
    check = _required_checks(tmp_path, gate_open=True, github=github)
    assert check.label == "FAIL" and check.required and not check.skipped, check
    assert "GitHub could not be read (transient; run doctor again)" in check.detail
    assert "safety.allow_merge=true" in check.detail and "run doctor again" in check.detail


def test_required_check_transient_failure_is_skipped(tmp_path):
    """With the merge gate closed, a transient failure is a SKIP that says why."""
    check = _required_checks(
        tmp_path, github={RULES_ENDPOINT: (1, "error connecting to api.github.com")}
    )
    assert check.skipped and "transient" in check.detail
    assert "the merge gate is closed" in check.detail


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
    # With the merge gate open the same answer is a FAIL (#289).
    check = _required_checks(
        tmp_path,
        gate_open=True,
        github={
            RULES_ENDPOINT: [],
            PROTECTION_ENDPOINT: (1, "gh: Must have admin rights to Repository. (HTTP 403)"),
        },
    )
    assert check.label == "FAIL" and "Administration: read" in check.detail


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


# -- the checks-read row (#291) ---------------------------------------------------
CHECKS_READ_ROW = "pull request check runs readable"
TOKEN = "ghp_" + "Q" * 36


def _checks_read(tmp_path, *, gate_open=False, **kwargs):
    """The checks-read row of a full doctor run, and every argv the run made."""
    calls: list = []
    config = None
    if gate_open:
        cfg = tmp_path / "gate.json"
        cfg.write_text('{"version": 1, "safety": {"allow_merge": true}}')
        config = str(cfg)
    d = Doctor(config_path=config, cwd=str(tmp_path), runner=_runner_factory(calls=calls, **kwargs))
    return {r.name: r for r in d.run_all()}[CHECKS_READ_ROW], calls


def _pr_views(calls):
    return [argv for argv in calls if argv[:3] == ["gh", "pr", "view"]]


def test_checks_read_passes_only_on_the_merge_gate_s_own_read_of_a_check_run(tmp_path):
    row, calls = _checks_read(tmp_path)
    assert row.ok and row.label == "OK  " and row.detail.startswith("PASS: ")
    assert PROBE_PR in row.detail and "1 of them check runs" in row.detail
    # The controller's own checks read: the same `gh`, the rollup requested.
    (view,) = _pr_views(calls)
    assert view[:4] == ["gh", "pr", "view", PROBE_PR]
    assert "statusCheckRollup" in view[view.index("--json") + 1].split(",")
    # Read-only and token-free: no write, no `gh auth token`.
    assert not any("token" in argv for argv in calls)
    assert not any(a in ("-X", "--method") for argv in calls for a in argv)


@pytest.mark.parametrize("gate_open", [False, True], ids=["gate-closed", "gate-open"])
def test_checks_read_denied_names_the_credential_and_its_remedy(tmp_path, gate_open):
    """GitHub's refusal is DENIED: a FAIL with the merge gate's config half open."""
    row, calls = _checks_read(
        tmp_path, gate_open=gate_open, pr_views={PROBE_PR: (1, ROLLUP_REFUSED)}
    )
    assert not row.ok and not row.skipped
    assert row.label == ("FAIL" if gate_open else "WARN")
    assert row.detail.startswith("DENIED: ")
    assert "fine-grained personal access token cannot be given Checks: read" in row.detail
    assert "`gh auth login`" in row.detail and "`repo` scope" in row.detail
    assert "READY_FOR_MERGE" in row.detail
    assert len(_pr_views(calls)) == 1  # a refusal is an answer: never retried


def test_checks_read_without_a_candidate_is_unknown_never_a_pass(tmp_path):
    row, calls = _checks_read(tmp_path, checks_probe=_probe_listing((8, 0)))
    assert row.skipped and not row.ok and row.label == "SKIP"
    assert row.detail.startswith("UNKNOWN: ") and "was not tried" in row.detail
    assert _pr_views(calls) == []


def test_checks_read_of_commit_statuses_only_is_unknown(tmp_path):
    """A commit status is not what GitHub refuses; only a check run proves the read."""
    prs = [f"https://github.com/owner/repo/pull/{n}" for n in (7, 8, 9, 10)]
    row, calls = _checks_read(
        tmp_path,
        checks_probe=_probe_listing((7, 1), (8, 2), (9, 1), (10, 1)),
        pr_views={url: [STATUS_CONTEXT] for url in prs},
    )
    assert row.skipped and row.detail.startswith("UNKNOWN: ")
    assert "no check run" in row.detail
    # At most doctor_module.CHECKS_READ_TRIES reads.
    assert [argv[3] for argv in _pr_views(calls)] == prs[: doctor_module.CHECKS_READ_TRIES]


def test_checks_read_tries_the_next_candidate_after_statuses_only(tmp_path):
    second = "https://github.com/owner/repo/pull/8"
    row, _ = _checks_read(
        tmp_path,
        checks_probe=_probe_listing((7, 1), (8, 3)),
        pr_views={PROBE_PR: [STATUS_CONTEXT], second: [STATUS_CONTEXT, CHECK_RUN]},
    )
    assert row.ok and second in row.detail and "2 check result(s)" in row.detail


@pytest.mark.parametrize("where", ["listing", "view"])
@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        (PLAN_LIMITED, "a GitHub plan limitation, not this login's permissions"),
        ("gh: Not Found (HTTP 404)", "was not found"),
        ("HTTP 502: Bad Gateway", "transient"),
    ],
    ids=["plan-limited", "not-found", "transient"],
)
def test_checks_read_that_cannot_answer_is_unknown(tmp_path, monkeypatch, where, stderr, reason):
    import autoforge.github as github

    monkeypatch.setattr(github.time, "sleep", lambda seconds: None)
    failure = (1, stderr)
    kwargs = {"checks_probe": failure} if where == "listing" else {"pr_views": {PROBE_PR: failure}}
    row, _ = _checks_read(tmp_path, gate_open=True, **kwargs)
    # Even with the gate open: none of these says what this login may read.
    assert row.skipped and row.label == "SKIP" and row.detail.startswith("UNKNOWN: ")
    assert reason in row.detail
    assert "token permissions" not in row.detail and "Checks: read" not in row.detail


def test_checks_read_refused_the_pr_itself_is_denied(tmp_path):
    row, _ = _checks_read(tmp_path, pr_views={PROBE_PR: (1, "HTTP 401: Bad credentials")})
    assert not row.ok and row.label == "WARN" and row.detail.startswith("DENIED: ")
    assert "'gh authenticated' row" in row.detail


def test_checks_read_never_prints_a_token(tmp_path):
    leaked = f"{ROLLUP_REFUSED}\nAuthorization: token {TOKEN}"
    for kwargs in (
        {"pr_views": {PROBE_PR: (1, leaked)}},
        {"checks_probe": (1, f"HTTP 401: Bad credentials {TOKEN}")},
    ):
        row, _ = _checks_read(tmp_path, gate_open=True, **kwargs)
        assert TOKEN not in row.detail, row


def test_checks_read_skipped_when_prerequisites_failed(tmp_path):
    row, calls = _checks_read(tmp_path, git_remote="git@gitlab.com:o/r.git")
    assert row.skipped and row.detail == "(GitHub remote check failed)"
    assert not any(argv[:3] == ["gh", "api", "graphql"] for argv in calls)


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
PI_CONTAINED = "pi's detached tool processes are contained (review_round_2_5)"
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
    # The sandbox rows and the containment row depend on the host's platform.
    others = [r for n, r in results.items() if not n.startswith(("outer sandbox: ", PI_CONTAINED))]
    assert all(r.ok for r in others), [r for r in others if not r.ok]


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


def test_a_ready_api_key_row_survives_the_redaction_doctor_prints_it_through(tmp_path):
    """#135: the DeepSeek route's OK row, as `autoforge doctor` prints it
    (redacted), still reads `authenticated`, not a redaction marker."""
    profile = _pi_profile(model="deepseek/deepseek-flash", options={"require_oauth": "false"})
    auth = (0, '{"status":"ready","provider":"deepseek","authType":"api_key"}')
    row = _pi_doctor(tmp_path, profile=profile, auth=auth)[PI_AUTH]
    assert row.ok, row.detail
    assert redact(row.detail) == row.detail
    assert "authType api_key; authenticated for provider 'deepseek' only" in row.detail


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
    names = [r.name for r in d.run_all()]
    assert not any("instructions" in n or n.startswith("pi's detached") for n in names)


# -- unattended Pi use needs orphan containment (#135, ADR 0002 §4b) ----------------
def test_pi_tool_processes_are_contained_where_a_subreaper_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor_module, "orphan_containment_available", lambda: True)
    row = _pi_doctor(tmp_path)[PI_CONTAINED]
    assert row.ok and not row.required
    assert "killed when the launch ends" in row.detail


def test_without_a_subreaper_unattended_pi_use_is_warned_against(tmp_path, monkeypatch):
    """A WARN, never a FAIL: attended use stays possible, and doctor passes."""
    monkeypatch.setattr(doctor_module, "orphan_containment_available", lambda: False)
    results = _pi_doctor(tmp_path)
    row = results[PI_CONTAINED]
    assert row.label == "WARN" and not row.required
    assert "do not run Pi unattended here (docs/pi.md#unattended-use)" in row.detail
    assert results[PI_LABEL].ok and results[PI_AUTH].ok


def test_a_subreaper_that_can_be_read_but_not_set_is_no_containment(tmp_path, monkeypatch):
    """R1-F1: a security policy that permits reading the subreaper setting
    but denies setting it leaves every launch uncontained, so doctor warns."""
    calls = []

    def prctl(option, arg, *_):
        calls.append(option)
        return 0 if option == executor_module._PR_GET_CHILD_SUBREAPER else -1

    monkeypatch.setattr(executor_module, "_prctl", lambda: prctl)
    row = _pi_doctor(tmp_path)[PI_CONTAINED]
    assert row.label == "WARN" and not row.required
    assert "cannot make itself a child subreaper" in row.detail
    assert "a security policy denies it" in row.detail
    assert executor_module._PR_SET_CHILD_SUBREAPER in calls


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


# -- the agent environment and the launch preflight (#165) ----------------------

FAKE_TOKEN = "ghp_" + "D" * 36
TOKEN_ORIGIN = f"https://x-access-token:{FAKE_TOKEN}@github.com/owner/repo.git"


def _all_text(results) -> str:
    return "\n".join(f"{r.name} {r.detail}" for r in results)


def test_doctor_refuses_a_token_bearing_origin_and_never_prints_it(tmp_path):
    listing = f"local\0remote.origin.url\n{TOKEN_ORIGIN}\0"
    runner = _runner_factory(git_remote=TOKEN_ORIGIN, git_config=listing)
    d = Doctor(cwd=str(tmp_path), runner=runner)
    results = d.run_all()
    rows = {r.name: r for r in results}
    row = rows["no credential reaches a launch"]
    assert not row.ok and row.required and row.label == "FAIL"
    assert "remote URL with an embedded credential" in row.detail
    assert "remote.origin.url" in row.detail
    # The remote row quotes the URL it cannot parse, redacted.
    assert not rows["GitHub remote"].ok and "REDACTED" in rows["GitHub remote"].detail
    assert FAKE_TOKEN not in _all_text(results)


@pytest.mark.parametrize(
    ("listing", "needle"),
    [
        ("local\0credential.helper\nstore\0", "credential helper in local-scope"),
        ("worktree\0credential.https://github.com.helper\ncache\0", "(credential.<url>.helper)"),
        ("system\0http.extraheader\nAuthorization: basic s3cr3t\0", "(http.extraHeader)"),
    ],
)
def test_doctor_refuses_a_credential_route_in_the_git_configuration(tmp_path, listing, needle):
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory(git_config=listing))
    row = {r.name: r for r in d.run_all()}["no credential reaches a launch"]
    assert not row.ok and needle in row.detail
    assert "s3cr3t" not in row.detail and "store" not in row.detail.split("(")[0][-8:]


@pytest.mark.parametrize(
    "entry",
    [
        f"machine github.com login x password {FAKE_TOKEN}\n",
        # The syntax curl reads too (#165 R2-F1): quoted, upper-case.
        f'machine "github.com" login x password {FAKE_TOKEN}\n',
        f"MACHINE github.com LOGIN x PASSWORD {FAKE_TOKEN}\n",
    ],
)
def test_doctor_refuses_a_netrc_entry_for_github(tmp_path, monkeypatch, entry):
    import autoforge.child_env as child_env_module

    home = tmp_path / "home"
    home.mkdir()
    (home / ".netrc").write_text(entry)
    monkeypatch.setattr(child_env_module, "operator_home", lambda: str(home))
    results = Doctor(cwd=str(tmp_path), runner=_runner_factory()).run_all()
    row = {r.name: r for r in results}["no credential reaches a launch"]
    assert not row.ok and "~/.netrc" in row.detail
    assert FAKE_TOKEN not in _all_text(results)


def test_doctor_refuses_a_login_shell_startup_file_that_exports_a_credential(tmp_path, monkeypatch):
    """R3-F1: what a launch's login shell runs at startup is part of what
    reaches it; doctor runs the same probe as a launch and names the shell
    and the variable, never the value."""
    import autoforge.child_env as child_env_module

    home = tmp_path / "home"
    home.mkdir()
    (home / ".profile").write_text(f"export GH_TOKEN={FAKE_TOKEN}\n")
    monkeypatch.setattr(child_env_module, "operator_home", lambda: str(home))
    results = Doctor(cwd=str(tmp_path), runner=_runner_factory()).run_all()
    row = {r.name: r for r in results}["no credential reaches a launch"]
    assert not row.ok and "the startup files of a login sh" in row.detail
    assert "which change GH_TOKEN" in row.detail
    assert FAKE_TOKEN not in _all_text(results)


def test_doctor_names_the_shell_a_launch_gets(tmp_path):
    from autoforge.child_env import child_shell

    row = {r.name: r for r in Doctor(cwd=str(tmp_path), runner=_runner_factory()).run_all()}
    detail = row["no credential reaches a launch"].detail
    assert f"a launch's shell is {child_shell()}" in detail
    assert "zsh reading none of yours" in detail


def test_doctor_fails_an_opencode_connected_to_an_organization(tmp_path, monkeypatch):
    """#165: the launch refuses an OpenCode that would load an organization's
    configuration, and doctor says so exactly; the origin is never shown."""
    import sqlite3
    from contextlib import closing

    import autoforge.providers as providers_module

    origin = "https://org.example.invalid"
    data_dir = tmp_path / "opencode-data"
    data_dir.mkdir()
    with closing(sqlite3.connect(data_dir / "opencode.db")) as db:
        db.execute("CREATE TABLE kv (key text PRIMARY KEY, value text NOT NULL)")
        db.execute("INSERT INTO kv VALUES ('wellknown:sources', ?)", (json.dumps([origin]),))
        db.commit()
    monkeypatch.setattr(providers_module, "opencode_data_dir", lambda env: data_dir)
    results = Doctor(cwd=str(tmp_path), runner=_runner_factory()).run_all()
    rows = {r.name: r for r in results}
    row = rows[
        "agent 'opencode' loads no organization configuration (review_round_1, "
        "review_round_2_5, review_round_6_plus, replan_reexecute, update_epic)"
    ]
    assert not row.ok and row.detail.startswith("refusing to launch opencode")
    assert f"1 organization(s) connected to OpenCode in {data_dir / 'opencode.db'}" in row.detail
    assert origin not in _all_text(results)
    (data_dir / "opencode.db").unlink()
    rows = {r.name: r for r in Doctor(cwd=str(tmp_path), runner=_runner_factory()).run_all()}
    assert rows[row.name].ok and str(data_dir) in rows[row.name].detail


def test_doctor_fails_a_claude_whose_settings_set_a_variable_over_the_launch(tmp_path, monkeypatch):
    """#165 (R4-F1): Claude Code applies the env of its settings over a
    launch's, so the launch refuses a GH_TOKEN there and doctor says so,
    naming the file and the variable, never the value."""
    import autoforge.providers as providers_module

    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {"GH_TOKEN": FAKE_TOKEN, "FOO": "bar"}}))
    monkeypatch.setattr(
        providers_module, "claude_config_files", lambda env: [settings, tmp_path / "none.json"]
    )
    results = Doctor(cwd=str(tmp_path), runner=_runner_factory()).run_all()
    row = {r.name: r for r in results}[CLAUDE_CONFIGURATION_ROW]
    assert not row.ok and row.required and row.detail.startswith("refusing to launch claude")
    assert f"GH_TOKEN in the env of {settings}" in row.detail and "FOO" not in row.detail
    assert FAKE_TOKEN not in _all_text(results)
    settings.write_text(json.dumps({"env": {"FOO": "bar"}}))
    rows = {r.name: r for r in Doctor(cwd=str(tmp_path), runner=_runner_factory()).run_all()}
    assert rows[CLAUDE_CONFIGURATION_ROW].ok


def test_doctor_reports_the_agent_environment_by_name_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/agent.sock")
    listing = "global\0user.name\nOperator Name\0global\0user.email\nop@example.invalid\0"
    results = Doctor(cwd=str(tmp_path), runner=_runner_factory(git_config=listing)).run_all()
    rows = {r.name: r for r in results}
    env_row = rows["launch environment"]
    assert env_row.ok and not env_row.required
    assert "withheld from this shell: " in env_row.detail
    withheld = env_row.detail.split("withheld from this shell: ")[1]
    assert "GH_TOKEN" in withheld and "SSH_AUTH_SOCK" in withheld
    assert (
        "set by the controller: CURL_HOME, GH_CONFIG_DIR, GH_ENTERPRISE_TOKEN, GH_TOKEN"
        in env_row.detail
    )
    assert "allow-listed: PATH, HOME" in env_row.detail
    credentials = rows["no credential reaches a launch"]
    assert credentials.ok and "identity supplied: user.name, user.email" in credentials.detail
    text = _all_text(results)
    assert FAKE_TOKEN not in text and "agent.sock" not in text and "Operator Name" not in text


def test_local_doctor_runs_the_environment_checks_and_never_gh(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)
    calls: list[list[str]] = []
    listing = "local\0credential.helper\nstore\0"
    d = Doctor(
        cwd=str(tmp_path),
        state_dir=str(tmp_path / "state"),
        runner=_runner_factory(calls=calls, git_config=listing),
    )
    rows = {r.name: r for r in d.run_local()}
    assert rows["launch environment"].ok and "GH_TOKEN" in rows["launch environment"].detail
    assert not rows["no credential reaches a launch"].ok
    assert all(argv[0] != "gh" for argv in calls), calls
    assert "GitHub remote" not in rows and "gh authenticated" not in rows


def test_doctors_pi_probes_get_the_launch_environment(tmp_path):
    from autoforge.child_env import GH_PLACEHOLDER_TOKEN, override_names

    requests: list = []
    _pi_doctor(tmp_path, requests=requests)
    assert requests
    for req in requests:
        assert tuple(sorted(req.env)) == override_names()
        assert req.env["GH_TOKEN"] == GH_PLACEHOLDER_TOKEN


def test_candidate_validation_is_listed_redacted_and_never_run(tmp_path):
    """#172: doctor names validation.commands and the mode its records are
    made under, without launching any of them."""
    d = Doctor(cwd=str(tmp_path), runner=_runner_factory())
    check = {r.name: r for r in d.run_all()}["candidate validation (validation.commands)"]
    assert check.ok and not check.required
    assert "(none configured: an empty set)" in check.detail
    assert "unsandboxed as the operator's UID" in check.detail

    secret = "gh" + "p_" + "s3cretvalue123456"
    cfg = tmp_path / "c.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "validation": {"commands": [["false"], ["make", "check", f"TOKEN={secret}"]]},
            }
        )
    )
    calls: list = []
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory(calls=calls))
    check = {r.name: r for r in d.run_all()}["candidate validation (validation.commands)"]
    assert check.detail.startswith("2 definition(s): false; make check TOKEN=")
    assert secret not in check.detail
    assert not any(argv[:1] in (["false"], ["make"]) for argv in calls)


# -- the parallel EPIC scheduler's rows (ADR 0008, #183) --------------------------
SCHEDULER_ROWS = (
    "scheduler settings",
    "scheduler coordination root",
    "EPIC sub-issues and dependencies readable",
)
DOCTOR_EPIC = "https://github.com/owner/repo/issues/1"


def _scheduler_doctor(tmp_path, graph, **kwargs):
    """A doctor on a real checkout: git's common dir is read for real, the
    EPIC's graph from ``graph``, everything else from the healthy fake."""
    from tests.conftest import git_repo

    repo = git_repo(tmp_path / "repo")
    base = _runner_factory()

    def runner(req):
        argv = req.command
        if argv[0] == "git" and "--git-common-dir" in argv:
            return execute(req)
        if argv[:2] == ["gh", "api"] and (
            argv[-1] == "repos/owner/repo" or argv[-1].startswith("repos/owner/repo/issues/")
        ):
            return graph(req)
        return base(req)

    return repo, Doctor(cwd=str(repo), runner=runner, epic_url=DOCTOR_EPIC, **kwargs)


def _epic_graph():
    from tests.test_scheduler import Graph

    graph = Graph()
    graph.child(2)
    graph.child(3, 2)
    return graph


def test_every_doctor_shows_the_scheduler_settings_and_only_epic_adds_the_rest(tmp_path):
    results = {r.name: r for r in Doctor(cwd=str(tmp_path), runner=_runner_factory()).run_all()}
    settings = results["scheduler settings"]
    assert settings.ok
    assert settings.detail == (
        "max_parallel 2 (1-4), poll_interval_seconds 60 (15-3600), stop_grace_seconds 30 (10-600)"
    )
    assert SCHEDULER_ROWS[1] not in results and SCHEDULER_ROWS[2] not in results


def test_the_scheduler_settings_row_shows_the_configured_values(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"version": 1, "scheduler": {"max_parallel": 4}}))
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    assert d.check_config().ok
    assert "max_parallel 4 (1-4)" in d.check_scheduler_settings().detail
    # A value out of bounds fails the configuration itself; the row is skipped.
    cfg.write_text(json.dumps({"version": 1, "scheduler": {"max_parallel": 5}}))
    d = Doctor(config_path=str(cfg), cwd=str(tmp_path), runner=_runner_factory())
    results = {r.name: r for r in d.run_all()}
    assert not results["config"].ok
    assert results["scheduler settings"].detail == "(config check failed)"


def test_doctor_epic_checks_the_root_and_the_graph_and_creates_nothing(tmp_path):
    graph = _epic_graph()
    repo, d = _scheduler_doctor(tmp_path, graph)
    results = {r.name: r for r in d.run_all()}
    for name in SCHEDULER_ROWS:
        assert results[name].ok, results[name]
    root = results["scheduler coordination root"].detail
    assert root.startswith(f"{(repo / '.git').resolve()}: local flock; the scheduler root ")
    assert root.endswith("is absent (the first 'epic run' makes it)")
    assert results["EPIC sub-issues and dependencies readable"].detail.startswith(
        "EPIC #1: 2 sub-issues and 1 blocked-by links read; 1 ready by the graph alone"
    )
    assert not (repo / ".git" / "autoforge").exists(), "doctor --epic creates nothing"
    assert all("/dependencies/" in c or c.startswith("repos/owner/repo") for c in graph.calls)


def test_an_existing_scheduler_root_is_accepted_when_it_is_this_users(tmp_path):
    repo, d = _scheduler_doctor(tmp_path, _epic_graph())
    (repo / ".git" / "autoforge" / "scheduler").mkdir(parents=True, mode=0o700)
    result = d.check_scheduler_root()
    assert result.ok and result.detail.endswith("scheduler exists")


def test_a_world_writable_scheduler_root_fails(tmp_path):
    repo, d = _scheduler_doctor(tmp_path, _epic_graph())
    scheduler = repo / ".git" / "autoforge" / "scheduler"
    scheduler.mkdir(parents=True)
    scheduler.chmod(0o777)
    result = d.check_scheduler_root()
    assert not result.ok and result.detail == f"{scheduler.resolve()} is world-writable"


def test_a_linked_scheduler_root_fails_and_is_not_followed(tmp_path):
    repo, d = _scheduler_doctor(tmp_path, _epic_graph())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / ".git" / "autoforge").symlink_to(elsewhere)
    result = d.check_scheduler_root()
    assert not result.ok
    assert result.detail.endswith("autoforge is not a directory (a link or another entry)")
    assert list(elsewhere.iterdir()) == []


def test_a_coordination_root_on_a_network_filesystem_fails(tmp_path, monkeypatch):
    from autoforge import locking

    monkeypatch.setattr(
        locking, "filesystem_type", lambda target: locking.FilesystemType(magic=0x6969)
    )
    _, d = _scheduler_doctor(tmp_path, _epic_graph())
    result = d.check_scheduler_root()
    assert not result.ok and "is on a nfs filesystem" in result.detail


def test_a_graph_the_scheduler_would_refuse_fails_the_row(tmp_path):
    graph = _epic_graph()
    graph.blocked(2, 3)  # #2 -> #3 -> #2
    _, d = _scheduler_doctor(tmp_path, graph)
    result = d.check_epic_graph("gh")
    assert not result.ok and "#2 -> #3 -> #2" in result.detail


def test_a_transient_graph_read_is_skipped_not_failed(tmp_path, monkeypatch):
    import autoforge.github as github

    monkeypatch.setattr(github.time, "sleep", lambda seconds: None)
    graph = _epic_graph()
    graph.fail["/sub_issues"] = (1, "HTTP 502: Bad Gateway")
    _, d = _scheduler_doctor(tmp_path, graph)
    result = d.check_epic_graph("gh")
    assert result.label == "SKIP" and "transient" in result.detail


def test_an_epic_of_another_repository_than_origin_fails(tmp_path):
    graph = _epic_graph()
    _, d = _scheduler_doctor(tmp_path, graph)
    d.repo = "owner/other"  # what the 'GitHub remote' row found
    result = d.check_epic_graph("gh")
    assert not result.ok
    assert result.detail == "the EPIC is in owner/repo, and this checkout's origin is owner/other"
    assert graph.calls == []


def test_doctor_epic_takes_an_issue_url_only(tmp_path):
    _, d = _scheduler_doctor(tmp_path, _epic_graph())
    d.epic_url = "owner/repo#1"
    assert not d.check_epic_graph("gh").ok


def test_the_cli_passes_the_epic_to_doctor(monkeypatch):
    from autoforge import cli

    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(cli, "run_doctor", fake)
    assert cli.main(["doctor", "--epic", DOCTOR_EPIC]) == 0
    assert seen["epic_url"] == DOCTOR_EPIC
