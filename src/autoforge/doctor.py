"""`autoforge doctor`: read-only environment checks.

Every check is non-destructive: version queries, `gh auth status`,
`git rev-parse`, reading the config, and creating/removing one temp file in
the state directory to prove it is writable.

``autoforge local doctor`` runs the LOCAL subset: no `gh`, no `gh auth
status`, no `origin` remote. A machine with no GitHub CLI and no GitHub
credentials must pass it, so the checks it omits are omitted entirely rather
than reported as warnings.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import AutoForgeConfig, load_config_file, validate_required_profiles
from .errors import (
    ConfigurationError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
    StateError,
)
from .executor import ExecutionRequest, ExecutionResult, execute
from .github import (
    GH_MIN_VERSION,
    GitHubClient,
    is_access_denied_gh_failure,
    parse_gh_version,
)
from .local_workspace import DEFAULT_MAX_BYTES, DEFAULT_MAX_ENTRIES
from .profiles import local_required_profiles
from .safefs import SafeRoot
from .validation import parse_remote_repository

Runner = Callable[[ExecutionRequest], ExecutionResult]
# A fixed list of required profile names, or a function of the loaded config.
RequiredProfiles = list[str] | Callable[[AutoForgeConfig], list[str]]

# Fallback executable for a provider whose profile does not set `command`,
# matching the provider adapters' own defaults.
DEFAULT_AGENT_COMMANDS = {"claude": "claude", "opencode": "opencode"}

# Name of the `doctor` row that verifies the default branch's required checks.
REQUIRED_CHECKS_ROW = "default branch requires checks"

REQUIRED_PROFILES = [
    "analyze_execute",
    "fix",
    "review_round_1",
    "review_round_2_5",
    "review_round_6_plus",
]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True
    # The check could not be performed (a read this token is not allowed to
    # make, a transient GitHub failure, an earlier check it depends on
    # failed). Neither a pass nor a failure: it never fails `doctor`, and it
    # never claims the property holds.
    skipped: bool = False

    @property
    def label(self) -> str:
        if self.skipped:
            return "SKIP"
        if self.ok:
            return "OK  "
        return "FAIL" if self.required else "WARN"

    @classmethod
    def skip(cls, name: str, detail: str) -> CheckResult:
        return cls(name, False, detail, required=False, skipped=True)


class Doctor:
    def __init__(
        self,
        config_path: str | None = None,
        state_dir: str | None = None,
        cwd: str | None = None,
        runner: Runner | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.config_path = config_path
        self.state_dir = state_dir
        self.cwd = cwd or os.getcwd()
        self._runner: Runner = runner or execute
        self.timeout = timeout_seconds
        self.config: AutoForgeConfig | None = None
        # 'owner/repo' of the `origin` remote, set exactly when `check_remote` passes.
        self.repo: str | None = None

    # -- helpers -----------------------------------------------------------------
    def _run(self, argv: list[str]) -> tuple[bool, str]:
        try:
            res = self._runner(
                ExecutionRequest(command=argv, cwd=self.cwd, timeout_seconds=self.timeout)
            )
        except Exception as exc:  # spawn failure / missing binary
            return False, f"{type(exc).__name__}: {exc}"
        text = (res.stdout or "").strip() or (res.stderr or "").strip()
        first = text.splitlines()[0] if text else ""
        if res.timed_out:
            return False, "timed out"
        return res.exit_code == 0, first if res.exit_code == 0 else (text[-400:] or "non-zero exit")

    def _version_check(self, name: str, argv: list[str]) -> CheckResult:
        ok, detail = self._run(argv)
        return CheckResult(name, ok, detail)

    # -- checks --------------------------------------------------------------------
    def check_config(self, required_profiles: RequiredProfiles | None = None) -> CheckResult:
        """Load the config and require its profiles.

        ``required_profiles`` may be a fixed list or a callable, because a
        LOCAL run's required reviewer profiles are derived from the config that
        is only loaded here (see :func:`autoforge.profiles.local_required_profiles`).

        ``self.config`` is set exactly when this check passes, and to what
        *this* attempt accepted. Every later check reads it, so it must not
        describe a config a run would refuse: not a previous file (a `Doctor`
        reused after its config turned invalid), and not one that parsed but
        failed profile validation -- a run does not start on such a config
        (`ControllerEngine.validate_config`), so the merge gate must not claim
        what a run "WILL" do with it.
        """
        self.config = None
        try:
            cfg = load_config_file(self.config_path)
            required = required_profiles or REQUIRED_PROFILES
            names = required(cfg) if callable(required) else required
            validate_required_profiles(cfg, names)
        except ConfigurationError as exc:
            return CheckResult("config", False, str(exc))
        self.config = cfg
        src = self.config_path or "(built-in defaults)"
        return CheckResult("config", True, f"{src}; profiles: {', '.join(sorted(cfg.profiles))}")

    def check_merge_gate(self) -> CheckResult:
        """Report the effective merge gate and the file/key that set it (informational).

        The gate has two halves -- `safety.allow_merge` in config AND
        `--allow-merge` on the CLI -- and `doctor` cannot see the flag, so it
        states what the flag *would* do with this config. The source is named
        so an operator who believes the gate is closed can check the key that
        decides it rather than the one they last edited.
        """
        name = "merge gate"
        cfg = self.config
        if cfg is None:
            return CheckResult(name, False, "(config check failed)", required=False)
        source = cfg.safety.allow_merge_source
        if cfg.merge_allowed_by_config:
            detail = (
                f"config half OPEN: safety.allow_merge=true ({source}); "
                "a run with --allow-merge WILL merge PRs itself"
            )
        else:
            detail = (
                f"CLOSED: safety.allow_merge=false ({source}); --allow-merge alone cannot merge"
            )
        return CheckResult(name, True, detail, required=False)

    def check_premerge_verification(self) -> CheckResult:
        """Report the controller's own pre-merge evidence (informational; runs nothing).

        A green required check proves the PR's workflow ran, not that the
        base branch's definition ran (``safety.verify_check_definition``)
        and not what the tests assert (``merge.verification_commands``).
        With the config half of the merge gate open and neither in place,
        the hosted check alone decides what gets merged unattended, which
        is worth stating next to the gate rather than discovering later.
        """
        name = "pre-merge verification"
        cfg = self.config
        if cfg is None:
            return CheckResult(name, False, "(config check failed)", required=False)
        definition = bool(cfg.safety.verify_check_definition and cfg.safety.required_checks)
        commands = cfg.merge.verification_commands
        parts = [
            (
                "check definition compared to the base branch's run "
                f"({', '.join(cfg.safety.required_checks)})"
                if definition
                else "check definition NOT verified (safety.verify_check_definition)"
            ),
            (
                "local commands: " + "; ".join(" ".join(argv) for argv in commands)
                if commands
                else "no merge.verification_commands: nothing runs locally before a merge"
            ),
        ]
        detail = "; ".join(parts)
        if cfg.merge_allowed_by_config and not commands:
            detail += (
                " -- with safety.allow_merge=true a green check is the only evidence about "
                "what the PR's tests assert; consider merge.verification_commands"
            )
        return CheckResult(name, True, detail, required=False)

    def check_state_dir(self) -> CheckResult:
        d = self.state_dir or (self.config.state_dir if self.config else ".autoforge")
        path = Path(self.cwd) / d if not Path(d).is_absolute() else Path(d)
        problem = self._writable(lambda: SafeRoot.open(path, create=True))
        if problem is not None:
            return CheckResult("state dir writable", False, f"{path}: {problem}")
        return CheckResult("state dir writable", True, str(path))

    def check_git_repo(self) -> CheckResult:
        ok, detail = self._run(["git", "rev-parse", "--show-toplevel"])
        if not ok:
            return CheckResult("cwd is a git repository", False, detail)
        return CheckResult("cwd is a git repository", True, detail)

    def check_remote(self) -> CheckResult:
        self.repo = None
        ok, detail = self._run(["git", "remote", "get-url", "origin"])
        if not ok:
            return CheckResult("GitHub remote", False, f"no 'origin' remote: {detail}")
        try:
            repo = parse_remote_repository(detail)
        except Exception as exc:
            return CheckResult("GitHub remote", False, f"{detail!r}: {exc}")
        self.repo = repo
        return CheckResult("GitHub remote", True, f"{repo} ({detail})")

    def check_required_checks(self, gh: str) -> CheckResult:
        """Whether the default branch really *requires* `safety.required_checks`.

        The merge gate verifies that every check on the PR succeeded, which
        is a statement about check results, not about whether any check had
        to exist: a branch that requires nothing makes a PR with no check
        runs vacuously green. The requirement lives in repository settings,
        outside version control, so this is the only place drift is caught.

        Read-only, and skipped -- never failed -- when the answer cannot be
        read: no credentials, a token or plan that cannot see rulesets, or a
        transient GitHub failure say nothing about the branch. The same
        holds for a partial answer: GitHub returns a ruleset's bypass actors
        only to a token with write access to it, and a rule this token can
        see but whose bypass list it cannot is not shown to be unbypassable.
        A problem that *is* visible (a missing context, a non-active
        ruleset, a bypass actor) is a FAIL regardless of what stayed hidden.
        """
        name = REQUIRED_CHECKS_ROW
        cfg = self.config
        if cfg is None:
            return CheckResult.skip(name, "(config check failed)")
        if self.repo is None:
            return CheckResult.skip(name, "(GitHub remote check failed)")
        client = GitHubClient(gh_command=gh, timeout_seconds=self.timeout, runner=self._runner)
        try:
            return self._required_checks(client, self.repo, cfg.safety.required_checks)
        except GitHubUnavailableError as exc:
            return CheckResult.skip(name, f"GitHub could not be read (transient): {exc}")
        except GitHubError as exc:
            if isinstance(exc, GitHubNotFoundError) or is_access_denied_gh_failure(str(exc)):
                return CheckResult.skip(
                    name, f"this token cannot read the branch rules of {self.repo}: {exc}"
                )
            return CheckResult(name, False, str(exc))

    def _required_checks(self, client: GitHubClient, repo: str, expected: list[str]) -> CheckResult:
        name = REQUIRED_CHECKS_ROW
        branch = client.get_repo(repo).default_branch
        if not branch:
            return CheckResult(name, False, f"{repo}: GitHub reports no default branch")
        rules = client.get_required_status_check_rules(repo, branch)
        if not rules:
            return self._required_checks_without_ruleset(client, repo, branch, expected)
        contexts = sorted({context for rule in rules for context in rule.contexts})
        problems = _missing_contexts(contexts, expected)
        # The effective-rules read lists active rulesets only, but the
        # ruleset itself says who may bypass it, and that is read separately.
        # GitHub withholds `bypass_actors` from a token without write access
        # to the ruleset (the read still succeeds), so "no bypass actors" is
        # established only by an explicitly empty list; `current_user_can_
        # bypass` is returned to every caller and a token that may bypass the
        # rule is a bypass actor whether or not the list is visible.
        unseen: list[str] = []
        for ruleset_id in sorted({rule.ruleset_id for rule in rules}):
            ruleset = client.get_ruleset(repo, ruleset_id)
            label = f"ruleset '{ruleset.name}' (#{ruleset.id})"
            can_bypass = ruleset.current_user_can_bypass
            if not ruleset.is_active:
                problems.append(f"{label} enforcement is '{ruleset.enforcement}', not 'active'")
            if ruleset.bypass_actors:
                problems.append(
                    f"{label} can be bypassed by: {', '.join(ruleset.bypass_actors)}"
                    + (f" (this token: {can_bypass})" if can_bypass else "")
                )
            elif can_bypass not in ("", "never"):
                problems.append(f"{label} can be bypassed by this token ({can_bypass})")
            elif ruleset.bypass_actors is None:
                unseen.append(label)
        sources = ", ".join(
            f"ruleset #{rule.ruleset_id} ({rule.ruleset_source_type or '?'} {rule.ruleset_source})"
            for rule in rules
        )
        summary = f"'{branch}' requires: {', '.join(contexts) or '(no context)'} via {sources}"
        hidden = (
            [
                f"bypass actors of {', '.join(unseen)} are not visible to this token (GitHub "
                "returns them only with write access to the ruleset), so 'no bypass actors' "
                "is unverified"
            ]
            if unseen
            else []
        )
        if problems:
            # A visible problem is conclusive whatever else stayed hidden;
            # the hidden part is still named so the remedy is known to be partial.
            return CheckResult(name, False, "; ".join([summary, *problems, *hidden]))
        if hidden:
            return CheckResult.skip(name, f"{summary}; {hidden[0]}")
        return CheckResult(name, True, summary)

    def _required_checks_without_ruleset(
        self, client: GitHubClient, repo: str, branch: str, expected: list[str]
    ) -> CheckResult:
        """No active ruleset requires a check: classic branch protection is the last resort."""
        name = REQUIRED_CHECKS_ROW
        remedy = (
            f"add a ruleset at https://github.com/{repo}/settings/rules targeting '{branch}' "
            f"with a 'Require status checks to pass' rule naming "
            f"{', '.join(expected) or 'the CI check'}, enforcement 'active' and no bypass actors"
        )
        try:
            classic = client.get_branch_protection(repo, branch)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            if not is_access_denied_gh_failure(str(exc)):
                raise
            # Rulesets require nothing, and the only other mechanism cannot
            # be read: no conclusion either way.
            return CheckResult.skip(
                name,
                f"no active ruleset requires a status check on '{branch}', and this token "
                f"cannot read its classic branch protection: {exc}",
            )
        if classic is not None and classic.required_status_checks:
            contexts = sorted(classic.required_contexts)
            problems = _missing_contexts(contexts, expected)
            if not classic.enforce_admins:
                problems.append("'Do not allow bypassing the above settings' is off for admins")
            summary = (
                f"'{branch}' requires: {', '.join(contexts) or '(no context)'} "
                "via classic branch protection"
            )
            if problems:
                return CheckResult(name, False, f"{summary}; {'; '.join(problems)}")
            return CheckResult(name, True, summary)
        # A disabled ruleset is invisible to the effective-rules read, so the
        # listing is consulted to make the remedy concrete. It is a hint only:
        # a listing this token may not read changes nothing about the answer.
        try:
            rulesets = client.list_rulesets(repo)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            if not is_access_denied_gh_failure(str(exc)):
                raise
            rulesets = []
        hints = [
            f"ruleset '{r.name}' (#{r.id}) exists but its enforcement is '{r.enforcement}'"
            for r in rulesets
            if r.target == "branch" and not r.is_active
        ]
        detail = f"no rule requires a status check on '{branch}'"
        if classic is not None:
            detail += " (classic branch protection exists but requires no status check)"
        return CheckResult(name, False, "; ".join([detail, *hints, remedy]))

    def check_gh_version(self, gh: str) -> CheckResult:
        """`gh` runs and is at least :data:`GH_MIN_VERSION`.

        The client's paginated reads use `gh api --paginate --slurp`, which an
        older `gh` rejects. That would surface only as a conclusive read
        failure at the merge gate (BLOCKED) or as a SKIP of the branch-rule
        check, so the version is refused here, up front, with the minimum
        named. A `--version` line the check cannot parse is a FAIL too: an
        unknown version is not a known-good one.
        """
        name = "gh available"
        ok, detail = self._run([gh, "--version"])
        if not ok:
            return CheckResult(name, False, detail)
        minimum = ".".join(str(part) for part in GH_MIN_VERSION)
        found = parse_gh_version(detail)
        if found is None:
            return CheckResult(
                name, False, f"cannot read the gh version from {detail!r} (need >= {minimum})"
            )
        if found < GH_MIN_VERSION:
            return CheckResult(
                name,
                False,
                f"{detail}: gh >= {minimum} is required "
                "(`gh api --paginate --slurp`, used to list branch rules and a PR's changed files)",
            )
        return CheckResult(name, True, detail)

    def check_gh_auth(self, gh: str) -> CheckResult:
        ok, detail = self._run([gh, "auth", "status"])
        return CheckResult("gh authenticated", ok, detail)

    def check_feature_spec(self, spec_path: str) -> CheckResult:
        """Validate a feature specification path the way `local run` would."""
        from .local_workspace import read_feature_spec

        name = "feature specification"
        ws = self._workspace()
        try:
            spec = read_feature_spec(ws, spec_path)
        except Exception as exc:
            return CheckResult(name, False, f"{spec_path}: {exc}")
        return CheckResult(name, True, f"{spec.relative_path} (sha256 {spec.sha256[:16]}...)")

    def check_validation_commands(self) -> CheckResult:
        """Report the configured local validation commands (never runs them)."""
        cfg = self.config
        cmds = cfg.local.validation_commands if cfg else []
        name = "local validation commands"
        if not cmds:
            return CheckResult(name, True, "(none configured)", required=False)
        return CheckResult(name, True, "; ".join(" ".join(argv) for argv in cmds))

    def run_local(self, feature_spec_path: str | None = None) -> list[CheckResult]:
        """Checks a LOCAL run needs — and nothing that touches GitHub.

        No `gh` binary, no `gh auth status`, no `origin` remote: a local run
        makes zero GitHub calls, so requiring any of them here would be a
        false failure on exactly the machine local mode exists for.
        """
        # Which reviewer profiles a local run needs depends on its configured
        # review bound, so the requirement is derived from the loaded config
        # rather than from a fixed list (`local_required_profiles`). `doctor`
        # therefore fails on a missing `review_round_6_plus` exactly when a run
        # with this `local.max_fix_rounds` could actually ask for it.
        results = [self.check_config(local_required_profiles)]
        cfg = self.config
        results.append(self._version_check("git available", ["git", "--version"]))
        results.append(self.check_git_repo())
        results.extend(self._local_agent_checks(cfg))
        results.append(self.check_local_state_dir())
        results.append(self.check_validation_commands())
        if feature_spec_path:
            results.append(self.check_feature_spec(feature_spec_path))
        return results

    def _local_agent_checks(self, cfg: AutoForgeConfig | None) -> list[CheckResult]:
        """One `--version` check per external CLI a LOCAL run can actually reach.

        Derived from `local_required_profiles`, not from every configured
        profile: a machine only needs the binaries the *reachable* local
        profiles name. A local configuration that routes everything through
        OpenCode must not fail because an unrelated remote profile mentions
        `claude`, and one that uses only `scripted` profiles must not require
        an external CLI at all — `scripted` spawns the configured argv
        directly rather than an agent CLI, so there is no version to query.
        Each distinct command is checked once, labelled with the profiles that
        reach it.
        """
        if cfg is None:
            return []
        try:
            reachable = local_required_profiles(cfg)
        except ConfigurationError:
            return []
        commands: dict[str, list[str]] = {}
        for name in reachable:
            profile = cfg.profiles.get(name)
            if profile is None or profile.provider == "scripted":
                continue
            command = profile.command or DEFAULT_AGENT_COMMANDS.get(profile.provider, "")
            if not command:
                continue
            commands.setdefault(command, []).append(name)
        if not commands:
            return [
                CheckResult(
                    "agent CLI available",
                    True,
                    "(no external agent CLI is reachable for this local configuration)",
                    required=False,
                )
            ]
        return [
            self._version_check(
                f"agent '{command}' available ({', '.join(names)})", [command, "--version"]
            )
            for command, names in commands.items()
        ]

    def _workspace(self):
        """A LocalWorkspace over the doctor's cwd, configured like a real run."""
        from .local_workspace import LocalWorkspace

        cfg = self.config
        local = cfg.local if cfg else None
        return LocalWorkspace(
            workdir=self.cwd,
            runner=self._runner,
            exclude=local.exclude if local else (),
            max_entries=local.max_workspace_entries if local else DEFAULT_MAX_ENTRIES,
            max_bytes=local.max_workspace_bytes if local else DEFAULT_MAX_BYTES,
        )

    def check_local_state_dir(self) -> CheckResult:
        """Where this run would keep its state — outside the reviewed tree, writable.

        This replaces the generic `check_state_dir` for local mode rather than
        joining it: the generic check would create `.autoforge/` inside the
        working tree, and in LOCAL mode that directory is part of what gets
        fingerprinted. A read-only diagnostic must not change what a run would
        review.
        """
        from .engine import local_state_paths

        cfg = self.config
        name = "state dir outside the reviewed working tree"
        ws = self._workspace()
        try:
            paths = local_state_paths(
                ws, explicit=self.state_dir, configured=cfg.state_dir if cfg else None
            )
            ws.check_state_dir_location(paths.state_dir)
        except Exception as exc:
            return CheckResult(name, False, str(exc))
        # The probe goes through the same capability a run would hold
        # (``StatePaths.open_root``: descriptor-relative from the git dir,
        # refusing a symbolic link at any component), so the doctor cannot be
        # made to write where the run would refuse to -- a symlinked
        # ``<git dir>/autoforge`` fails this check instead of being followed.
        writable = self._writable(lambda: paths.open_root(create=True))
        if writable is not None:
            return CheckResult(name, False, f"{paths.state_dir}: {writable}")
        return CheckResult(name, True, str(paths.state_dir))

    @staticmethod
    def _writable(open_root: Callable[[], SafeRoot]) -> str | None:
        """None when a probe file can be created through ``open_root()``, else why not.

        The probe is a controller write like any other: it is created and
        removed through the opened root, never by pathname, so the check
        exercises exactly the boundary a run's writes would go through.
        """
        probe = f".doctor-probe-{secrets.token_hex(8)}"
        try:
            with open_root() as root:
                root.create_exclusive(probe, b"")
                root.unlink(probe)
        except (OSError, StateError) as exc:
            return str(exc)
        return None

    @staticmethod
    def _agent_commands(cfg: AutoForgeConfig | None) -> tuple[str, str]:
        claude_cmd, opencode_cmd = "claude", "opencode"
        if cfg:
            for p in cfg.profiles.values():
                if p.provider == "claude" and p.command:
                    claude_cmd = p.command
                if p.provider == "opencode" and p.command:
                    opencode_cmd = p.command
        return claude_cmd, opencode_cmd

    def run_all(self) -> list[CheckResult]:
        results = [self.check_config(), self.check_merge_gate(), self.check_premerge_verification()]
        cfg = self.config
        gh = cfg.github.command if cfg else "gh"
        claude_cmd, opencode_cmd = self._agent_commands(cfg)
        results.append(self._version_check("git available", ["git", "--version"]))
        results.append(self.check_gh_version(gh))
        results.append(self.check_gh_auth(gh))
        results.append(self._version_check("claude available", [claude_cmd, "--version"]))
        results.append(self._version_check("opencode available", [opencode_cmd, "--version"]))
        results.append(self.check_git_repo())
        results.append(self.check_remote())
        results.append(self.check_required_checks(gh))
        results.append(self.check_state_dir())
        return results


def _missing_contexts(contexts: list[str], expected: list[str]) -> list[str]:
    """The problems an expected-context list has with the required contexts (often none)."""
    if not contexts:
        return ["the rule names no status check context at all"]
    missing = [context for context in expected if context not in contexts]
    if missing:
        return [f"required contexts do not include {', '.join(repr(m) for m in missing)}"]
    return []


def run_doctor(
    config_path: str | None = None,
    state_dir: str | None = None,
    cwd: str | None = None,
    runner: Runner | None = None,
) -> list[CheckResult]:
    return Doctor(config_path=config_path, state_dir=state_dir, cwd=cwd, runner=runner).run_all()


def run_local_doctor(
    config_path: str | None = None,
    state_dir: str | None = None,
    cwd: str | None = None,
    runner: Runner | None = None,
    feature_spec_path: str | None = None,
) -> list[CheckResult]:
    """`autoforge local doctor`: the LOCAL checks only (never touches GitHub)."""
    return Doctor(config_path=config_path, state_dir=state_dir, cwd=cwd, runner=runner).run_local(
        feature_spec_path
    )
