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
from .errors import ConfigurationError, StateError
from .executor import ExecutionRequest, ExecutionResult, execute
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

    @property
    def label(self) -> str:
        if self.ok:
            return "OK  "
        return "FAIL" if self.required else "WARN"


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

        ``self.config`` is what *this* attempt loaded, never a previous one:
        every later check reads it, so a `Doctor` reused after its config file
        turned invalid must not keep describing the old file -- least of all
        the merge gate, which would otherwise claim the state of a config that
        was just rejected.
        """
        self.config = None
        try:
            self.config = load_config_file(self.config_path)
            required = required_profiles or REQUIRED_PROFILES
            names = required(self.config) if callable(required) else required
            validate_required_profiles(self.config, names)
        except ConfigurationError as exc:
            return CheckResult("config", False, str(exc))
        src = self.config_path or "(built-in defaults)"
        return CheckResult(
            "config", True, f"{src}; profiles: {', '.join(sorted(self.config.profiles))}"
        )

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
            return CheckResult(name, False, "(config not loaded)", required=False)
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
        ok, detail = self._run(["git", "remote", "get-url", "origin"])
        if not ok:
            return CheckResult("GitHub remote", False, f"no 'origin' remote: {detail}")
        try:
            repo = parse_remote_repository(detail)
        except Exception as exc:
            return CheckResult("GitHub remote", False, f"{detail!r}: {exc}")
        return CheckResult("GitHub remote", True, f"{repo} ({detail})")

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
        results = [self.check_config(), self.check_merge_gate()]
        cfg = self.config
        gh = cfg.github.command if cfg else "gh"
        claude_cmd, opencode_cmd = self._agent_commands(cfg)
        results.append(self._version_check("git available", ["git", "--version"]))
        results.append(self._version_check("gh available", [gh, "--version"]))
        results.append(self.check_gh_auth(gh))
        results.append(self._version_check("claude available", [claude_cmd, "--version"]))
        results.append(self._version_check("opencode available", [opencode_cmd, "--version"]))
        results.append(self.check_git_repo())
        results.append(self.check_remote())
        results.append(self.check_state_dir())
        return results


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
