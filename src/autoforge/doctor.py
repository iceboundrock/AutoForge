"""`autoforge doctor`: read-only environment checks.

Every check is non-destructive: version queries, `gh auth status`,
`git rev-parse`, `pi auth check --no-refresh`, reading the config, and
creating/removing one temp file in the state directory to prove it is
writable.

The agent CLIs checked are the ones the run can reach: each distinct
command named by a reachable profile, once, labelled with those profiles.
A Pi profile adds a version floor and a read-only credential probe, run
under the same environment allow-list as the agent launch (never the
operator's whole environment) and reported without Pi's raw output, and a
warning naming the instruction files Pi would load from outside the
checkout (docs/pi-policy.md §4-5); those are found by path, never read.
It also warns where the controller cannot contain Pi's detached tool
processes (it cannot make itself a child subreaper: not Linux, or a
security policy denies it), where unattended Pi use is not supported
(docs/pi.md, #135). A Claude Code command must list the flags every
launch keeps the operator's and the repository's integrations off with in
its ``--help`` (#165) and fails while its settings or global
configuration set a variable over a launch's isolation, and an OpenCode
profile fails while OpenCode would load an organization's configuration;
a launch refuses either (#165,
:meth:`autoforge.providers.ClaudeCodeProvider.integration_preflight`,
:meth:`autoforge.providers.OpenCodeProvider.integration_preflight`).

Both report the environment an agent and a repository-defined command are
launched with, by name only (never a value), and run the launch's own
credential preflight (#165, :func:`autoforge.child_env.preflight`): one
local ``git config`` read, ``~/.netrc`` and one login shell per shell a
launch may start; a credential helper in the repository's configuration, a
URL with an embedded credential, an HTTP header for git, a GitHub entry in
``~/.netrc`` or a shell startup file that changes a launch's variables
fails ``doctor`` exactly as it would refuse the launch. ``~/.curlrc``,
wget's configuration and zsh's startup files need no check: a launch's
curl, wget and zsh read the controller's empty ones instead. The
``origin`` URL is shown redacted.

A REMOTE run's doctor also makes the merge gate's checks read on one open
pull request whose head has check results (#291): GitHub refuses a
fine-grained personal access token the check runs of a private repository,
which no permission it can be given changes. PASS needs a check run read;
no such pull request is UNKNOWN, never a PASS.

Every run shows the parallel EPIC scheduler's settings (``scheduler.*``,
bounded by the configuration loader). ``doctor --epic <url>`` adds the
scheduler's own checks (ADR 0008, #183): the coordination root (the git
common dir) is on a filesystem whose ``flock`` is a local kernel lock
(:func:`autoforge.locking.check_local_lock_filesystem`); the scheduler root
is absent or a real directory owned by this user and not world-writable
(``lstat`` only); and ``gh`` can read the EPIC's sub-issues and
dependencies (#179's snapshot, its four reads). They create nothing.

``autoforge local doctor`` runs the LOCAL subset: no `gh`, no `gh auth
status`, no `origin` remote. A machine with no GitHub CLI and no GitHub
credentials must pass it, so the checks it omits are omitted entirely rather
than reported as warnings.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .child_env import (
    LaunchIdentity,
    child_shell,
    describe_overrides,
    forbidden_env_entry,
    isolated_overrides,
    preflight,
)
from .config import (
    MAX_PARALLEL_BOUNDS,
    POLL_INTERVAL_BOUNDS,
    STOP_GRACE_BOUNDS,
    AutoForgeConfig,
    ProfileConfig,
    load_config_file,
    validate_required_profiles,
)
from .config_fingerprint import fingerprint_routing
from .epic_graph import build_epic_snapshot, evaluate, ready_set
from .errors import (
    ConfigurationError,
    ExecutionError,
    GitHubChecksAccessError,
    GitHubError,
    GitHubNotFoundError,
    GitHubPlanLimitedError,
    GitHubUnavailableError,
    LockError,
    SnapshotRefused,
    StateError,
)
from .executor import (
    ExecutionRequest,
    ExecutionResult,
    execute,
    orphan_containment_available,
)
from .github import (
    GH_MIN_VERSION,
    GitHubClient,
    checks_access_remedy,
    gh_request,
    gh_workdir,
    is_access_denied_gh_failure,
    parse_gh_version,
)
from .local_workspace import DEFAULT_MAX_BYTES, DEFAULT_MAX_ENTRIES
from .locking import check_local_lock_filesystem, repository_lock_path
from .profiles import REQUIRED_PROFILES, local_required_profiles
from .providers import (
    CLAUDE_ISOLATION_FLAGS,
    OPENCODE_MIN_VERSION,
    PI_CONTEXT_FILES,
    PI_DEFAULT_AGENT_DIR,
    PI_MIN_VERSION,
    PI_SYSTEM_PROMPT_FILES,
    ClaudeCodeProvider,
    OpenCodeProvider,
    PiProvider,
    parse_opencode_version,
    parse_pi_version,
    provider_for,
)
from .redaction import redact, redact_argv
from .run_namespace import AUTOFORGE_DIRNAME, SCHEDULER_DIRNAME
from .safefs import STATE_DIR_NEEDS_HARD_LINKS, HardLinksUnavailable, SafeRoot
from .sdk_policy import SDK_PROVIDERS
from .validation import parse_issue_url, parse_remote_repository

Runner = Callable[[ExecutionRequest], ExecutionResult]
# A fixed list of required profile names, or a function of the loaded config.
RequiredProfiles = list[str] | Callable[[AutoForgeConfig], list[str]]

# Fallback executable for a provider whose profile does not set `command`,
# matching the provider adapters' own defaults.
DEFAULT_AGENT_COMMANDS = {"claude": "claude", "opencode": "opencode", "pi": "pi"}

# The profile a REMOTE run reaches only after a merge, on top of
# `REQUIRED_PROFILES`; checked when it is configured.
REMOTE_POST_MERGE_PROFILES = ["update_epic"]

# Name of the `doctor` row that verifies the default branch's required checks.
REQUIRED_CHECKS_ROW = "default branch requires checks"
# Name of the `doctor` row that tries the merge gate's checks read (#291).
CHECKS_READ_ROW = "pull request check runs readable"
# How many candidate PRs that row reads before it gives up on finding a check
# run: each is one `gh pr view`, and a candidate whose results are commit
# statuses only proves nothing about check runs.
CHECKS_READ_TRIES = 3


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
        epic_url: str | None = None,
    ) -> None:
        self.config_path = config_path
        # ``doctor --epic``: the EPIC whose graph the scheduler rows read.
        self.epic_url = epic_url
        self.state_dir = state_dir
        self.cwd = cwd or os.getcwd()
        self._runner: Runner = runner or execute
        self.timeout = timeout_seconds
        self.config: AutoForgeConfig | None = None
        # 'owner/repo' of the `origin` remote, set exactly when `check_remote` passes.
        self.repo: str | None = None
        # Where the TCP listeners are read from (ADR 0005 D9.3); a test points
        # it at its own files.
        self.proc_net = "/proc/net"

    @property
    def runner(self) -> Runner:
        """What every check's process runs through (a test's fake, or the executor)."""
        return self._runner

    # -- helpers -----------------------------------------------------------------
    def _run(self, argv: list[str]) -> tuple[bool, str]:
        return self._run_request(
            ExecutionRequest(command=argv, cwd=self.cwd, timeout_seconds=self.timeout)
        )

    def _run_gh(self, argv: list[str]) -> tuple[bool, str]:
        """:meth:`_run` for a `gh` argv: in an empty directory of its own, never the checkout.

        The controller runs every `gh` that way (#283), and so does doctor.
        """
        try:
            with gh_workdir() as workdir:
                return self._run_request(gh_request(argv, workdir, timeout_seconds=self.timeout))
        except GitHubError as exc:  # no empty directory could be made
            return False, str(exc)

    def _run_request(self, request: ExecutionRequest) -> tuple[bool, str]:
        try:
            res = self._runner(request)
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
            if required_profiles is None:
                # A REMOTE run: its routing is recorded in its state file (#219).
                fingerprint_routing(cfg)
        except ConfigurationError as exc:
            return CheckResult("config", False, str(exc))
        # #280: a file an enforced launch could have rewritten is refused
        # here as every command refuses it, and since ``self.config`` stays
        # unset, no check runs a command it names (a profile's ``command``,
        # ``github.command``).
        from .engine import config_file_placement_problem

        problem = config_file_placement_problem(cfg, self.config_path or "", self.cwd, self.runner)
        if problem:
            return CheckResult(
                "config", False, f"{problem}; doctor runs no command this file names"
            )
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

    def check_candidate_validation(self) -> CheckResult:
        """List ``validation.commands`` (informational; runs nothing, ADR 0006 D3.1).

        The definitions REMOTE candidate validation runs in a candidate's
        byte-exact export, redacted, and the mode their records are made
        under; none is launched here.
        """
        name = "candidate validation (validation.commands)"
        cfg = self.config
        if cfg is None:
            return CheckResult(name, False, "(config check failed)", required=False)
        commands = cfg.validation.commands
        mode = f"records made under sandbox.mode {cfg.sandbox.mode}"
        if not cfg.sandbox.enabled:
            mode += " (validators run unsandboxed as the operator's UID: unenforced evidence)"
        if not commands:
            return CheckResult(
                name, True, f"(none configured: an empty set); {mode}", required=False
            )
        listed = "; ".join(" ".join(redact_argv(list(argv))) for argv in commands)
        return CheckResult(
            name, True, f"{len(commands)} definition(s): {listed}; {mode}", required=False
        )

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
            return CheckResult("GitHub remote", False, f"no 'origin' remote: {redact(detail)}")
        # The URL is shown redacted: one carrying a token is refused by the
        # credential row, and never printed.
        try:
            repo = parse_remote_repository(detail)
        except Exception as exc:
            return CheckResult("GitHub remote", False, redact(f"{detail!r}: {exc}"))
        self.repo = repo
        return CheckResult("GitHub remote", True, f"{repo} ({redact(detail)})")

    def check_agent_environment(self) -> CheckResult:
        """What an agent and a repository-defined command inherit, by name only.

        Informational: the configured allow-list (each provider adds its own
        names on top), the names the controller sets for every launch, and
        the credential-route names this shell holds that no launch receives.
        """
        cfg = self.config if self.config is not None else AutoForgeConfig()
        names = cfg.execution.environment_names()
        withheld = sorted(name for name in os.environ if forbidden_env_entry(name))
        detail = (
            f"allow-listed: {', '.join(names)} (plus each provider's own names); "
            f"set by the controller: {describe_overrides()}; "
            f"withheld from this shell: {', '.join(withheld) or '(none present)'}"
        )
        return CheckResult("launch environment", True, detail, required=False)

    def check_launch_credentials(self) -> CheckResult:
        """The launch preflight, run here: no GitHub or git credential reaches a child."""
        name = "no credential reaches a launch"
        cfg = self.config if self.config is not None else AutoForgeConfig()
        try:
            identity = preflight(
                self.cwd, self._runner, env_allowlist=cfg.execution.environment_names()
            )
        except ConfigurationError as exc:
            return CheckResult(name, False, redact(str(exc)))
        return CheckResult(
            name,
            True,
            "no credential helper, credential-bearing URL or HTTP header in the repository's "
            "git configuration, and no GitHub entry in ~/.netrc; a launch's curl and wget "
            "read an empty configuration of the controller's, never ~/.curlrc or ~/.wgetrc; "
            f"a launch's shell is {child_shell()} and no login shell's startup files change "
            "its variables, zsh reading none of yours; "
            f"author identity supplied: {identity.describe()}",
        )

    def check_required_checks(self, gh: str) -> CheckResult:
        """Whether the default branch really *requires* `safety.required_checks`.

        The merge gate verifies that every check on the PR succeeded, which
        is a statement about check results, not about whether any check had
        to exist: a branch that requires nothing makes a PR with no check
        runs vacuously green. The requirement lives in repository settings,
        outside version control, so this is the only place drift is caught.

        Read-only. When the branch rules cannot be verified -- GitHub's
        plan offers none on this repository (#289), the token may not read
        them, GitHub reports the repository not found, or GitHub could not
        be read at all (transient) -- nothing shows that the merge gate's
        "every check succeeded" has a required check behind it. With the
        configuration's half of the merge gate open (``safety.allow_merge:
        true``) that is a FAIL; with it closed a SKIP that says why. A plan
        limitation is named as one and never sent to the token's
        permissions: no permission changes it. The same holds for a rule
        whose bypass actors GitHub withholds (it returns them only with
        write access to the ruleset) when it also does not say whether this
        token may bypass it: whether the controller's own login can skip the
        rule is then unverified. When GitHub does say that this token may
        never bypass it, the hidden list is a SKIP either way: the required
        checks were read and the controller cannot bypass them. A problem
        that *is* visible (a missing context, a non-active ruleset, a bypass
        actor) is a FAIL regardless of what stayed hidden. Never an
        unverified OK.
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
            return self._required_checks_unverifiable(
                f"GitHub could not be read (transient; run doctor again): {exc}",
                "run doctor again",
            )
        except GitHubPlanLimitedError as exc:
            return self._required_checks_unverifiable(
                _plan_limited_text(self.repo, exc), _PLAN_REMEDY
            )
        except GitHubError as exc:
            if isinstance(exc, GitHubNotFoundError) or is_access_denied_gh_failure(str(exc)):
                return self._required_checks_unverifiable(
                    f"this token cannot read the branch rules of {self.repo}: {exc}",
                    _READ_RULES_REMEDY,
                )
            return CheckResult(name, False, str(exc))

    def _required_checks_unverifiable(self, why: str, remedy: str) -> CheckResult:
        """No verified answer about the default branch's required checks (#289).

        A FAIL with the gate open, a SKIP that says why with it closed. The
        merge gate's own rollup check refuses a rollup in which nothing
        succeeded, but only a branch rule makes a check *required*: with
        ``safety.allow_merge: true`` and nothing doctor can verify requiring
        one, the unattended merge rests on a property no one verified.
        ``remedy`` is the way out for this cause, never a permission for a
        plan limitation.
        """
        name = REQUIRED_CHECKS_ROW
        if self.config is not None and self.config.merge_allowed_by_config:
            return CheckResult(
                name,
                False,
                redact(
                    f"{why}; with safety.allow_merge=true the merge gate would merge without "
                    f"a branch rule doctor could verify requires a check: {remedy}, or close "
                    "the gate (safety.allow_merge: false)"
                ),
            )
        return CheckResult.skip(
            name,
            redact(f"{why} (the merge gate is closed, so no unattended merge relies on it)"),
        )

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
        unseen: list[str] = []  # list hidden; GitHub says this token may never bypass
        unverified: list[str] = []  # list hidden, and nothing said about this token
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
                (unverified if can_bypass == "" else unseen).append(label)
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
        if unverified:
            hidden.append(
                f"bypass actors of {', '.join(unverified)} are not visible to this token, and "
                "GitHub did not say whether this token may bypass the rule, so whether the "
                "controller's own login can skip the required checks is unverified"
            )
        if problems:
            # A visible problem is conclusive whatever else stayed hidden;
            # the hidden part is still named so the remedy is known to be partial.
            return CheckResult(name, False, "; ".join([summary, *problems, *hidden]))
        if unverified:
            return self._required_checks_unverifiable(
                "; ".join([summary, *hidden]),
                "run doctor with a login that has write access to the ruleset",
            )
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
        except GitHubPlanLimitedError as exc:
            return self._required_checks_unverifiable(
                f"no active ruleset requires a status check on '{branch}', and "
                + _plan_limited_text(repo, exc),
                _PLAN_REMEDY,
            )
        except GitHubError as exc:
            if not is_access_denied_gh_failure(str(exc)):
                raise
            # Rulesets require nothing, and the only other mechanism cannot
            # be read: no conclusion either way.
            return self._required_checks_unverifiable(
                f"no active ruleset requires a status check on '{branch}', and this token "
                f"cannot read its classic branch protection (Administration: read): {exc}",
                _READ_RULES_REMEDY,
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
            if not (
                isinstance(exc, GitHubPlanLimitedError) or is_access_denied_gh_failure(str(exc))
            ):
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

    def check_checks_read(self, gh: str) -> CheckResult:
        """Whether this `gh` login may read a pull request's check runs (#291).

        The merge gate reads a PR's check results with
        :meth:`GitHubClient.get_pr_with_checks`, and GitHub refuses a
        fine-grained personal access token the check runs of a private
        repository's PR: no permission it can be given grants them. This row
        makes that same read, with the same `gh` command and login, on an
        open PR whose head has at least one check result (selected by count,
        :meth:`GitHubClient.list_checks_probe_candidates`), up to
        :data:`CHECKS_READ_TRIES` of them. Read-only; it never reads, prints
        or stores a token.

        PASS only when the read returned at least one check *run*, the
        result GitHub was seen to refuse (#257). Whether it refuses a PR
        whose results are commit statuses only was not tried, so reading
        one proves nothing about check runs: the next candidate is read,
        and none with a check run is UNKNOWN. DENIED when GitHub refused the
        read (whatever the PR's results), a FAIL while the
        configuration's half of the merge gate is open (every merge would be
        BLOCKED) and a WARN otherwise. UNKNOWN (a SKIP, never a PASS) when no
        PR could be tried or the answer was a plan limitation, a not-found or
        a transient failure: none of those says what this login may read.
        """
        name = CHECKS_READ_ROW
        cfg = self.config
        if cfg is None:
            return CheckResult.skip(name, "(config check failed)")
        if self.repo is None:
            return CheckResult.skip(name, "(GitHub remote check failed)")
        repo = self.repo
        client = GitHubClient(gh_command=gh, timeout_seconds=self.timeout, runner=self._runner)
        try:
            candidates = client.list_checks_probe_candidates(repo)
        except GitHubChecksAccessError as exc:
            # Even the count was refused: that is the refusal itself.
            return self._checks_read_denied(
                f"GitHub refused this gh login the check results of {repo}'s open pull "
                f"requests: {checks_access_remedy(exc.credential)}",
                exc,
            )
        except GitHubError as exc:
            return self._checks_read_unknown(
                f"the open pull requests of {repo} could not be listed to pick one to read", exc
            )
        if not candidates:
            return CheckResult.skip(
                name,
                f"UNKNOWN: no recently updated open pull request of {repo} has a check result "
                "on its head, so the checks read the merge gate makes was not tried; run doctor "
                "again while one does",
            )
        tried: list[str] = []
        for url, _count in candidates[:CHECKS_READ_TRIES]:
            try:
                pr = client.get_pr_with_checks(url)
            except GitHubChecksAccessError as exc:
                return self._checks_read_denied(
                    f"GitHub refused this gh login the check results of {url}: "
                    f"{checks_access_remedy(exc.credential)}",
                    exc,
                )
            except GitHubError as exc:
                if _pr_read_refused(exc):
                    return self._checks_read_denied(
                        f"GitHub refused this gh login the pull request {url} itself (see the "
                        "'gh authenticated' row)",
                        exc,
                    )
                return self._checks_read_unknown(f"the checks read of {url} failed", exc)
            runs = [check for check in pr.checks or [] if check.kind == "CheckRun"]
            if runs:
                return CheckResult(
                    name,
                    True,
                    f"PASS: this gh login read {len(pr.checks or [])} check result(s) of {url}, "
                    f"{len(runs)} of them check runs, with the merge gate's own read",
                )
            tried.append(url)
        return CheckResult.skip(
            name,
            f"UNKNOWN: the check results of {', '.join(tried)} hold no check run (commit "
            "statuses only), which proves nothing about the check runs GitHub may refuse",
        )

    def _checks_read_denied(self, what: str, exc: GitHubError) -> CheckResult:
        gate_open = self.config is not None and self.config.merge_allowed_by_config
        consequence = (
            "with safety.allow_merge=true every merge would be BLOCKED at READY_FOR_MERGE"
            if gate_open
            else "a run with the merge gate open would be BLOCKED at READY_FOR_MERGE; "
            "REVIEW and FIX proceed without the check results"
        )
        return CheckResult(
            CHECKS_READ_ROW,
            False,
            redact(f"DENIED: {what}; {consequence}. GitHub's answer: {exc}"),
            required=gate_open,
        )

    @staticmethod
    def _checks_read_unknown(what: str, exc: GitHubError) -> CheckResult:
        # The client's typed answers first; only a plain GitHubError is
        # classified by its text.
        if isinstance(exc, GitHubUnavailableError):
            why = "a transient GitHub failure; run doctor again"
        elif isinstance(exc, GitHubNotFoundError):
            why = "the repository or pull request was not found by this login"
        elif isinstance(exc, GitHubPlanLimitedError):
            why = "a GitHub plan limitation, not this login's permissions"
        elif type(exc) is GitHubError and is_access_denied_gh_failure(str(exc)):
            why = "this login may not make that read (see the 'gh authenticated' row)"
        else:
            why = "an answer this check cannot classify"
        return CheckResult.skip(CHECKS_READ_ROW, redact(f"UNKNOWN: {what} ({why}): {exc}"))

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
        ok, detail = self._run_gh([gh, "--version"])
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
        ok, detail = self._run_gh([gh, "auth", "status"])
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
        results.append(self.check_agent_environment())
        results.append(self.check_launch_credentials())
        results.extend(self._local_agent_checks(cfg))
        results.append(self.check_local_state_dir())
        results.append(self.check_validation_commands())
        if feature_spec_path:
            results.append(self.check_feature_spec(feature_spec_path))
        results.extend(self.sandbox_checks(local=True))
        return results

    def sandbox_checks(self, local: bool) -> list[CheckResult]:
        """The outer sandbox's rows (ADR 0005 D12.4, D14.1; :mod:`autoforge.doctor_sandbox`)."""
        from .doctor_sandbox import sandbox_checks

        return sandbox_checks(self, local)

    def _local_agent_checks(self, cfg: AutoForgeConfig | None) -> list[CheckResult]:
        """The agent CLIs a LOCAL run can actually reach.

        Derived from `local_required_profiles`, not from every configured
        profile: a machine only needs the binaries the *reachable* local
        profiles name. A local configuration that routes everything through
        OpenCode must not fail because an unrelated remote profile mentions
        `claude`, and one that uses only `scripted` profiles must not require
        an external CLI at all.
        """
        if cfg is None:
            return []
        try:
            reachable = local_required_profiles(cfg)
        except ConfigurationError:
            return []
        return [
            *self._agent_checks(cfg, reachable, "this local configuration"),
            self.check_agent_limits(cfg, reachable),
            self.check_loop_detection(cfg),
        ]

    def check_agent_limits(self, cfg: AutoForgeConfig, reachable: list[str]) -> CheckResult:
        """Report the limits each reachable profile's agent runs under (#193).

        Informational: an agent is killed after writing nothing for its idle
        timeout, and once it has run for its max runtime when one is set.
        Fails only for a profile a run would refuse to launch (a CLI that
        reports no progress and has no wall-clock limit), which the config
        check does not already cover for a profile reachable only after a
        merge.
        """
        name = "profile limits"
        shown: list[str] = []
        for profile_name in reachable:
            profile = cfg.profiles.get(profile_name)
            if profile is None:
                continue
            try:
                limits = cfg.agent_limits(profile)
            except ConfigurationError as exc:
                return CheckResult(name, False, str(exc))
            shown.append(f"{profile_name}: {limits.describe()}")
        return CheckResult(name, True, "; ".join(shown) or "(no profile)", required=False)

    @staticmethod
    def check_loop_detection(cfg: AutoForgeConfig) -> CheckResult:
        """Report how every agent is watched for a loop (#194). Informational."""
        return CheckResult(
            "loop detection", True, cfg.execution.loop_detection.describe(), required=False
        )

    def _agent_checks(
        self, cfg: AutoForgeConfig, reachable: list[str], scope: str
    ) -> list[CheckResult]:
        """One availability check per external CLI the ``reachable`` profiles name.

        Each distinct command is checked once per provider that names it,
        labelled with that provider's profiles: what a command must be
        depends on the adapter that launches it, so a command that profiles of
        two providers share (a wrapper) must pass both providers' checks.
        `scripted` spawns the configured argv directly rather than an agent
        CLI, so there is no version to query. A command a Pi profile names
        gets Pi's version floor instead of a bare `--version`, and each Pi
        profile a read-only credential probe; one an OpenCode profile names
        gets OpenCode's floor.
        """
        groups: dict[tuple[str, str], list[ProfileConfig]] = {}
        for name in reachable:
            profile = cfg.profiles.get(name)
            if profile is None or profile.provider == "scripted":
                continue
            command = profile.command or DEFAULT_AGENT_COMMANDS.get(profile.provider, "")
            if not command:
                continue
            groups.setdefault((command, profile.provider), []).append(profile)
        if not groups:
            return [
                CheckResult(
                    "agent CLI available",
                    True,
                    f"(no external agent CLI is reachable for {scope})",
                    required=False,
                )
            ]
        results: list[CheckResult] = []
        for (command, provider), profiles in groups.items():
            label = f"agent '{command}' available ({', '.join(p.name for p in profiles)})"
            if provider == "pi":
                available = self.check_pi_version(label, profiles[0])
                results.append(available)
                results.extend(self.check_pi_auth(command, profiles, available.ok))
            elif provider == "opencode":
                results.append(self.check_opencode_version(label, command))
                results.append(self.check_opencode_isolation(cfg, command, profiles))
            elif provider in SDK_PROVIDERS:
                results.append(self._version_check(label, [command, "--version"]))
                results.append(self.check_sdk_launch(provider, profiles))
            else:
                results.append(self._version_check(label, [command, "--version"]))
                if provider == "claude":
                    results.append(self.check_claude_isolation(command, profiles))
                    results.append(self.check_claude_configuration(cfg, command, profiles))
        pi_profiles = [p for (_, provider), ps in groups.items() if provider == "pi" for p in ps]
        if pi_profiles:
            results.append(self.check_pi_outside_instructions(pi_profiles))
            results.append(self.check_pi_orphan_containment(pi_profiles))
        return results

    def check_sdk_launch(self, provider: str, profiles: list[ProfileConfig]) -> CheckResult:
        """FAIL for an SDK provider's profiles: their policy validates (#149),
        but the helper that would launch them has not landed (#151, #152), so
        every launch is refused before it starts."""
        name = f"provider {provider} can launch ({', '.join(p.name for p in profiles)})"
        try:
            provider_for(profiles[0]).integration_preflight(None)
        except ConfigurationError as exc:
            return CheckResult(name, False, redact(str(exc)))
        return CheckResult(name, True, "its helper is available")

    def check_pi_outside_instructions(self, profiles: list[ProfileConfig]) -> CheckResult:
        """WARN when Pi would load instruction files from outside the checkout.

        Pi always appends its agent directory's ``SYSTEM.md`` /
        ``APPEND_SYSTEM.md`` (no flag turns them off), and, unless every Pi
        profile sets ``context_files: false``, the agent directory's context
        file and one from each directory above the checkout. None of them is
        project data under review, and none is visible in the PR. Only
        whether each path exists is checked; no file is read.
        """
        names = ", ".join(p.name for p in profiles)
        name = f"pi loads no instructions from outside the checkout ({names})"
        env = os.environ.get("PI_CODING_AGENT_DIR", "") if self._pi_passes_agent_dir() else ""
        agent_dir = Path(env or PI_DEFAULT_AGENT_DIR).expanduser()
        found = [agent_dir / f for f in PI_SYSTEM_PROMPT_FILES if (agent_dir / f).is_file()]
        if any(PiProvider.context_files(p) for p in profiles):
            for directory in [agent_dir, *Path(self.cwd).resolve().parents]:
                first = next((f for f in PI_CONTEXT_FILES if (directory / f).is_file()), None)
                if first is not None:
                    found.append(directory / first)
        if not found:
            return CheckResult(name, True, "(none found)", required=False)
        return CheckResult(
            name,
            False,
            f"every Pi run will also follow {', '.join(str(p) for p in found)}; remove "
            "or move them, or set options.context_files: false for context files "
            "(docs/pi-policy.md)",
            required=False,
        )

    def check_pi_orphan_containment(self, profiles: list[ProfileConfig]) -> CheckResult:
        """WARN where Pi's detached tool processes cannot be contained.

        Pi's `bash` tool starts every command detached, so what a command
        backgrounds outlives Pi; only a child subreaper (Linux) lets the
        executor find and kill it (ADR 0002 §4b). The row passes only where
        the controller can make itself one, as each contained launch does,
        not merely read the setting. Without one, unattended Pi use is not
        supported (docs/pi.md#unattended-use, #135). Never a FAIL: an
        attended run that reads each result's leftovers sentence is still
        possible.
        """
        name = f"pi's detached tool processes are contained ({', '.join(p.name for p in profiles)})"
        if orphan_containment_available():
            return CheckResult(
                name,
                True,
                "a process a Pi tool command leaves running is killed when the launch ends "
                "(Linux child subreaper, ADR 0002 §4b)",
                required=False,
            )
        return CheckResult(
            name,
            False,
            "the controller cannot make itself a child subreaper here (not Linux, or a "
            "security policy denies it): a process a Pi tool command leaves "
            "running can outlive the launch, and each result says so (orphans_unchecked); "
            "do not run Pi unattended here (docs/pi.md#unattended-use)",
            required=False,
        )

    def _pi_passes_agent_dir(self) -> bool:
        """Whether the launch allow-list hands ``PI_CODING_AGENT_DIR`` to Pi."""
        return "PI_CODING_AGENT_DIR" in self._pi_allowlist()

    def _pi_allowlist(self) -> tuple[str, ...]:
        """The environment a Pi agent is launched with, so the probes see what it would."""
        cfg = self.config if self.config is not None else AutoForgeConfig()
        return PiProvider().launch_allowlist(cfg.execution.environment_names())

    def _pi_probe(self, argv: list[str]) -> ExecutionResult | str:
        """Run one Pi probe as the launch would (its allow-list and the variables
        the controller sets, #165); the result, or why it did not run."""
        try:
            with isolated_overrides(LaunchIdentity()) as overrides:
                res = self._runner(
                    ExecutionRequest(
                        command=argv,
                        cwd=self.cwd,
                        timeout_seconds=self.timeout,
                        env_allowlist=self._pi_allowlist(),
                        env=overrides,
                    )
                )
        except Exception as exc:  # spawn failure / missing binary
            return f"{type(exc).__name__}: {exc}"
        return "timed out" if res.timed_out else res

    def check_pi_version(self, name: str, profile: ProfileConfig) -> CheckResult:
        """`pi` runs and is at least :data:`PI_MIN_VERSION` (ADR 0003 §2.8).

        A version line the check cannot parse is a FAIL, as for `gh`: an
        unknown version is not a known-good one. Pi's output is never quoted:
        a detail names only the parsed release number, so whatever a wrapper
        prints there (or appends as a pre-release suffix) stays out of it.
        """
        argv = PiProvider().version_command(profile)
        res = self._pi_probe(argv)
        if isinstance(res, str):
            return CheckResult(name, False, res)
        if res.exit_code != 0:
            return CheckResult(
                name,
                False,
                f"`{' '.join(argv)}` exited {res.exit_code} (output not shown; run it yourself)",
            )
        minimum = ".".join(str(part) for part in PI_MIN_VERSION)
        text = (res.stdout or "").strip()
        found = parse_pi_version(text)
        if found is None:
            return CheckResult(
                name,
                False,
                f"cannot read the pi version from `{' '.join(argv)}` (output not shown; "
                f"need >= {minimum})",
            )
        release, _, suffix = text.partition("-")
        shown = f"pi {release} (pre-release)" if suffix else f"pi {release}"
        if found < PI_MIN_VERSION:
            return CheckResult(
                name,
                False,
                f"{shown}: pi >= {minimum} is required (the RPC and auth contract "
                "AutoForge relies on, ADR 0003)",
            )
        return CheckResult(name, True, shown)

    def check_claude_isolation(self, command: str, profiles: list[ProfileConfig]) -> CheckResult:
        """`claude --help` lists every flag a launch keeps integrations off with (#165).

        Every Claude Code launch passes :data:`CLAUDE_ISOLATION_FLAGS` so that
        no hook, plugin or MCP server of the operator's or the repository's
        configuration runs and no project or local settings apply; a CLI
        without one would refuse every launch. The help text is never quoted.
        """
        name = f"agent '{command}' keeps integrations off ({', '.join(p.name for p in profiles)})"
        argv = [command, "--help"]
        try:
            res = self._runner(
                ExecutionRequest(command=argv, cwd=self.cwd, timeout_seconds=self.timeout)
            )
        except Exception as exc:  # spawn failure / missing binary
            return CheckResult(name, False, f"{type(exc).__name__}: {exc}")
        if res.timed_out:
            return CheckResult(name, False, "timed out")
        if res.exit_code != 0:
            return CheckResult(
                name,
                False,
                f"`{' '.join(argv)}` exited {res.exit_code} (output not shown; run it yourself)",
            )
        listed = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", res.stdout or ""))
        # A flag is offered by its name; its value (``=user``) is the launch's.
        flags = [flag.partition("=")[0] for flag in CLAUDE_ISOLATION_FLAGS]
        missing = [flag for flag in flags if flag not in listed]
        if missing:
            return CheckResult(
                name,
                False,
                f"`{' '.join(argv)}` does not list {', '.join(missing)}, which every launch "
                "passes so that no hook, plugin or MCP server of the operator's or the "
                "repository's configuration runs and no setting of the repository's "
                "applies; update Claude Code",
            )
        return CheckResult(
            name,
            True,
            f"launched with {' '.join(CLAUDE_ISOLATION_FLAGS)}: no hook, plugin or MCP server "
            "of the operator's or the repository's configuration runs, and no setting of "
            "the repository's applies",
        )

    def check_claude_configuration(
        self, cfg: AutoForgeConfig, command: str, profiles: list[ProfileConfig]
    ) -> CheckResult:
        """The launch's own refusal of Claude Code configuration that sets a
        variable over its isolation (#165).

        Run under the agent launch's allow-list, so the configuration files
        looked at are the launch's; only paths and variable names are
        reported, never a value.
        """
        name = (
            f"agent '{command}' configuration keeps the launch environment "
            f"({', '.join(p.name for p in profiles)})"
        )
        try:
            ClaudeCodeProvider().integration_preflight(cfg.execution.environment_names())
        except (ConfigurationError, ExecutionError) as exc:
            return CheckResult(name, False, redact(str(exc)))
        return CheckResult(
            name,
            True,
            "no variable of Claude Code's settings or global configuration replaces one "
            "AutoForge sets; its Bash tool runs no shell startup file's function or alias",
        )

    def check_opencode_isolation(
        self, cfg: AutoForgeConfig, command: str, profiles: list[ProfileConfig]
    ) -> CheckResult:
        """The launch's own refusal of an organization OpenCode would load (#165).

        Run under the agent launch's allow-list, so the data directory and
        database looked at are the launch's; only paths and counts are
        reported, never an origin or a credential.
        """
        name = (
            f"agent '{command}' loads no organization configuration "
            f"({', '.join(p.name for p in profiles)})"
        )
        provider = OpenCodeProvider()
        allowlist = cfg.execution.environment_names()
        try:
            provider.integration_preflight(allowlist)
            data_dir = provider.data_dir(allowlist)
        except (ConfigurationError, ExecutionError) as exc:
            return CheckResult(name, False, redact(str(exc)))
        return CheckResult(
            name,
            True,
            f"no organization connected to OpenCode (data directory {data_dir}); a launch "
            "reads no configuration file of the operator's or the repository's",
        )

    def check_opencode_version(self, name: str, command: str) -> CheckResult:
        """`opencode` runs and is at least :data:`OPENCODE_MIN_VERSION` (#186).

        The adapter speaks v2's argv (``--standalone``, ``-m model#variant``)
        and hands the prompt over on stdin; 1.x rejects that argv. As for Pi,
        an unreadable version is a FAIL and the CLI's output is never quoted.
        """
        argv = [command, "--version"]
        try:
            res = self._runner(
                ExecutionRequest(command=argv, cwd=self.cwd, timeout_seconds=self.timeout)
            )
        except Exception as exc:  # spawn failure / missing binary
            return CheckResult(name, False, f"{type(exc).__name__}: {exc}")
        if res.timed_out:
            return CheckResult(name, False, "timed out")
        if res.exit_code != 0:
            return CheckResult(
                name,
                False,
                f"`{' '.join(argv)}` exited {res.exit_code} (output not shown; run it yourself)",
            )
        minimum = ".".join(str(part) for part in OPENCODE_MIN_VERSION)
        text = (res.stdout or "").strip()
        found = parse_opencode_version(text)
        if found is None:
            return CheckResult(
                name,
                False,
                f"cannot read the opencode version from `{' '.join(argv)}` (output not "
                f"shown; need >= {minimum})",
            )
        # The text matched the version pattern whole, so its only `-` opens a
        # pre-release suffix.
        head, _, suffix = text.partition("-")
        release = head.removeprefix("opencode ").removeprefix("v")
        shown = f"opencode {release} (pre-release)" if suffix else f"opencode {release}"
        if found < OPENCODE_MIN_VERSION:
            return CheckResult(
                name,
                False,
                f"{shown}: opencode >= {minimum} is required (the argv and stdin "
                "delivery the adapter speaks, #186)",
            )
        return CheckResult(name, True, shown)

    def check_pi_auth(
        self, command: str, profiles: list[ProfileConfig], available: bool
    ) -> list[CheckResult]:
        """``pi auth check --no-refresh`` for each distinct (model, OAuth policy) pair.

        Run under the agent launch's allow-list, so a key only the
        operator's shell holds (an `OPENAI_API_KEY` the launch would not
        see) cannot make it report "ready". Pi's stdout and stderr are never
        quoted: the verdict names the four fields of its JSON result only.
        """
        provider = PiProvider()
        groups: dict[tuple[str, bool], list[ProfileConfig]] = {}
        for profile in profiles:
            groups.setdefault((profile.model, provider.require_oauth(profile)), []).append(profile)
        results: list[CheckResult] = []
        for members in groups.values():
            profile = members[0]
            name = f"agent '{command}' auth ready ({', '.join(p.name for p in members)})"
            if not available:
                results.append(CheckResult.skip(name, f"(agent '{command}' is not available)"))
                continue
            results.append(self._pi_auth(name, provider, profile))
        return results

    def _pi_auth(self, name: str, provider: PiProvider, profile: ProfileConfig) -> CheckResult:
        refused = provider.api_key_problem(profile, self._pi_allowlist())
        if refused:
            return CheckResult(name, False, refused)
        res = self._pi_probe(provider.auth_check_command(profile))
        if isinstance(res, str):
            return CheckResult(name, False, res)
        ok, detail = provider.auth_verdict(profile, res.exit_code, res.stdout or "")
        return CheckResult(name, ok, f"{profile.model}: {detail}")

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
        exercises exactly the boundary a run's writes would go through. A
        filesystem without hard links fails it as it fails every run's
        run-log probe, and the reason names that requirement (#120).
        """
        probe = f".doctor-probe-{secrets.token_hex(8)}"
        try:
            with open_root() as root:
                root.create_exclusive(probe, b"")
                root.unlink(probe)
        except HardLinksUnavailable as exc:
            return f"{exc}. {STATE_DIR_NEEDS_HARD_LINKS}"
        except (OSError, StateError) as exc:
            return str(exc)
        return None

    def _remote_agent_checks(self, cfg: AutoForgeConfig | None) -> list[CheckResult]:
        """The agent CLIs a REMOTE run can reach: `REQUIRED_PROFILES`, plus
        `update_epic` (reachable after a merge) when it is configured.

        Nothing is checked when the config failed: that row already fails
        `doctor`, and a guess at the routing would check the wrong binaries.
        """
        if cfg is None:
            return []
        reachable = REQUIRED_PROFILES + [
            name for name in REMOTE_POST_MERGE_PROFILES if name in cfg.profiles
        ]
        return [
            *self._agent_checks(cfg, reachable, "this configuration"),
            self.check_agent_limits(cfg, reachable),
            self.check_loop_detection(cfg),
        ]

    # -- the parallel EPIC scheduler (ADR 0008, #183) -------------------------------
    def check_scheduler_settings(self) -> CheckResult:
        """``scheduler.*`` as a scheduler would run with them; the loader enforces the bounds."""
        name = "scheduler settings"
        cfg = self.config
        if cfg is None:
            return CheckResult.skip(name, "(config check failed)")
        settings = cfg.scheduler
        values = (
            ("max_parallel", settings.max_parallel, MAX_PARALLEL_BOUNDS),
            ("poll_interval_seconds", settings.poll_interval_seconds, POLL_INTERVAL_BOUNDS),
            ("stop_grace_seconds", settings.stop_grace_seconds, STOP_GRACE_BOUNDS),
        )
        outside = [
            f"{key} {value}" for key, value, (low, high) in values if not low <= value <= high
        ]
        detail = ", ".join(f"{key} {value} ({low}-{high})" for key, value, (low, high) in values)
        if outside:
            return CheckResult(name, False, f"out of bounds: {', '.join(outside)}; {detail}")
        return CheckResult(name, True, detail)

    def check_scheduler_root(self) -> CheckResult:
        """The coordination root's filesystem and the scheduler root's entry; read-only."""
        name = "scheduler coordination root"
        try:
            root = repository_lock_path(self.cwd, runner=self._runner).parent.parent
            check_local_lock_filesystem(root, what=f"the coordination root {root}")
        except LockError as exc:
            return CheckResult(name, False, str(exc))
        problems = []
        path = Path(root)
        for component in (AUTOFORGE_DIRNAME, SCHEDULER_DIRNAME):
            path = path / component
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                break
            except OSError as exc:
                problems.append(f"{path} cannot be read ({exc})")
                break
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                problems.append(f"{path} is not a directory (a link or another entry)")
                break
            if st.st_uid != os.getuid():
                problems.append(f"{path} is owned by uid {st.st_uid}, not this user")
            if st.st_mode & stat.S_IWOTH:
                problems.append(f"{path} is world-writable")
        if problems:
            return CheckResult(name, False, "; ".join(problems))
        scheduler = Path(root) / AUTOFORGE_DIRNAME / SCHEDULER_DIRNAME
        found = (
            "exists" if os.path.lexists(scheduler) else "is absent (the first 'epic run' makes it)"
        )
        return CheckResult(
            name, True, f"{root}: local flock; the scheduler root {scheduler} {found}"
        )

    def check_epic_graph(self, gh: str) -> CheckResult:
        """Whether ``gh`` reads the EPIC's graph: #179's snapshot, read-only."""
        name = "EPIC sub-issues and dependencies readable"
        if self.epic_url is None:
            return CheckResult.skip(name, "(no --epic)")
        try:
            epic = parse_issue_url(self.epic_url)
        except ConfigurationError as exc:
            return CheckResult(name, False, str(exc))
        if self.repo is not None and self.repo.lower() != epic.repository.lower():
            return CheckResult(
                name,
                False,
                f"the EPIC is in {epic.repository}, and this checkout's origin is {self.repo}",
            )
        client = GitHubClient(gh_command=gh, timeout_seconds=self.timeout, runner=self._runner)
        try:
            repository_id = client.get_repository_id(epic.repository)
            snapshot = build_epic_snapshot(client, epic.canonical, repository_id=repository_id)
        except GitHubUnavailableError as exc:
            return CheckResult.skip(name, f"GitHub could not be read (transient): {exc}")
        except SnapshotRefused as exc:
            if exc.transient:
                return CheckResult.skip(name, f"GitHub could not be read (transient): {exc}")
            return CheckResult(name, False, str(exc))
        except GitHubError as exc:
            return CheckResult(name, False, str(exc))
        ready = ready_set(evaluate(snapshot))
        return CheckResult(
            name,
            True,
            f"EPIC #{epic.number}: {len(snapshot.children)} sub-issues and "
            f"{len(snapshot.edges)} blocked-by links read; {len(ready)} ready by the graph "
            f"alone (snapshot {snapshot.digest[:12]})",
        )

    def scheduler_checks(self, gh: str) -> list[CheckResult]:
        """The scheduler rows: its settings always, the rest for ``doctor --epic``."""
        results = [self.check_scheduler_settings()]
        if self.epic_url is not None:
            results.append(self.check_scheduler_root())
            results.append(self.check_epic_graph(gh))
        return results

    def run_all(self) -> list[CheckResult]:
        results = [
            self.check_config(),
            self.check_merge_gate(),
            self.check_premerge_verification(),
            self.check_candidate_validation(),
        ]
        cfg = self.config
        gh = cfg.github.command if cfg else "gh"
        results.append(self._version_check("git available", ["git", "--version"]))
        results.append(self.check_gh_version(gh))
        results.append(self.check_gh_auth(gh))
        results.extend(self._remote_agent_checks(cfg))
        results.append(self.check_git_repo())
        results.append(self.check_remote())
        results.append(self.check_agent_environment())
        results.append(self.check_launch_credentials())
        results.append(self.check_required_checks(gh))
        results.append(self.check_checks_read(gh))
        # The probe creates the state directory and writes in it, so it runs
        # only for a configuration that loaded: doctor writes nothing for one
        # it refused, whose ``state_dir`` it has not read either.
        if cfg is None:
            results.append(CheckResult.skip("state dir writable", "(config check failed)"))
        else:
            results.append(self.check_state_dir())
        results.extend(self.scheduler_checks(gh))
        results.extend(self.sandbox_checks(local=False))
        return results


# The way out of an unverifiable required-check row, by cause (#289). A plan
# limitation is never sent to the token's permissions.
_PLAN_REMEDY = "make the repository public or move it to a plan that offers rules"
_READ_RULES_REMEDY = "run doctor with a login that may read the branch rules"


def _plan_limited_text(repo: str, exc: GitHubError) -> str:
    """What doctor says about GitHub's plan-entitlement 403 (#289): the plan, never the token."""
    return (
        f"GitHub's plan for {repo} does not offer rulesets or branch protection on this "
        "repository (GitHub Free offers neither on a private repository), so nothing can "
        "require a check on its default branch; this is the plan, not the token, and no "
        "token permission changes it: make the repository public or move it to a plan "
        f"that offers them (GitHub Pro, Team or Enterprise). GitHub's answer: {exc}"
    )


def _pr_read_refused(exc: GitHubError) -> bool:
    """Whether GitHub refused this login the pull request itself (#291).

    Classified by the client's types: only a plain conclusive
    :class:`GitHubError` can be such a refusal. Every typed answer says
    something else (transient, not found, a refused checks read, another
    PR, a plan limitation, which is about the plan, not the login).
    """
    return type(exc) is GitHubError and is_access_denied_gh_failure(str(exc))


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
    epic_url: str | None = None,
) -> list[CheckResult]:
    return Doctor(
        config_path=config_path, state_dir=state_dir, cwd=cwd, runner=runner, epic_url=epic_url
    ).run_all()


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
