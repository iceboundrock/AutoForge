"""`autoforge doctor`: read-only environment checks.

Every check is non-destructive: version queries, `gh auth status`,
`git rev-parse`, reading the config, and creating/removing one temp file in
the state directory to prove it is writable.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import AutoForgeConfig, load_config_file, validate_required_profiles
from .errors import ConfigurationError
from .executor import ExecutionRequest, ExecutionResult, execute
from .validation import parse_remote_repository

Runner = Callable[[ExecutionRequest], ExecutionResult]

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
    def check_config(self) -> CheckResult:
        try:
            self.config = load_config_file(self.config_path)
            validate_required_profiles(self.config, REQUIRED_PROFILES)
        except ConfigurationError as exc:
            return CheckResult("config", False, str(exc))
        src = self.config_path or "(built-in defaults)"
        return CheckResult(
            "config", True, f"{src}; profiles: {', '.join(sorted(self.config.profiles))}"
        )

    def check_state_dir(self) -> CheckResult:
        d = self.state_dir or (self.config.state_dir if self.config else ".autoforge")
        path = Path(self.cwd) / d if not Path(d).is_absolute() else Path(d)
        try:
            path.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".doctor-", dir=str(path))
            os.close(fd)
            os.unlink(tmp)
        except OSError as exc:
            return CheckResult("state dir writable", False, f"{path}: {exc}")
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

    def run_all(self) -> list[CheckResult]:
        results = [self.check_config()]
        cfg = self.config
        gh = cfg.github.command if cfg else "gh"
        claude_cmd = "claude"
        opencode_cmd = "opencode"
        if cfg:
            for p in cfg.profiles.values():
                if p.provider == "claude" and p.command:
                    claude_cmd = p.command
                if p.provider == "opencode" and p.command:
                    opencode_cmd = p.command
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
