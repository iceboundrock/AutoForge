"""Controller configuration layer.

Model selection is controller configuration — never hard-coded shell
commands in the engine. Users edit a config file (YAML/TOML/JSON) to change
the real CLI command / model identifiers; logical profile names stay stable.

Logical execution profiles (routing semantics are fixed in ``profiles.py``)::

    analyze_execute      Claude Code  / Fable            / high
    fix                  Claude Code  / Fable            / high
    review_round_1       OpenCode     / GPT 5.6 Luna     / high
    review_round_2_5     OpenCode     / GPT 5.6 Terra    / high
    review_round_6_plus  OpenCode     / GPT 5.6 Sol      / medium
    update_epic          (future milestone; gated)

MERGE has no agent profile: the controller itself runs ``gh pr merge``
(see ``merge:`` below and ``GitHubClient.merge_pr``), behind the merge gate.

The *real* model identifiers below were checked against the locally
installed CLIs (``claude --help``, ``opencode models``); change them in the
config file, never by editing routing code.

Supported config file formats:
  .toml  — stdlib tomllib (always available)
  .json  — stdlib json    (always available)
  .yaml/.yml — PyYAML if installed, else a minimal built-in subset parser
               sufficient for the documented example file (nested maps with
               2-space indent, lists with "- ", scalars, quoted strings).

The two YAML backends must never read the same file differently: what the
subset parser accepts it resolves exactly as PyYAML's YAML 1.1 implicit
resolvers do, and what it cannot resolve that way it refuses outright rather
than keeping as the string it looks like. See the note above ``_scalar``.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import __prompt_version__
from .errors import ConfigurationError
from .executor import is_env_pattern
from .local_workspace import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ENTRIES,
    normalize_exclude_pattern,
)

CONFIG_VERSION = 1

DEFAULT_TIMEOUT_SECONDS = 1800

KNOWN_PROVIDERS = ("claude", "opencode", "scripted")


# Where a REMOTE run keeps its state, relative to the invocation directory.
# A LOCAL run defaults elsewhere -- see
# :meth:`autoforge.engine.ControllerEngine.bind_local_state_dir` -- because its
# fingerprint covers the whole working tree and runtime state must not be in it.
DEFAULT_STATE_DIR = ".autoforge"


@dataclass
class ProfileConfig:
    """One logical execution profile -> concrete provider/model/effort.

    ``options`` carries provider-specific knobs the adapter understands
    (e.g. ``permission_mode`` for Claude Code, ``auto_approve`` for
    OpenCode). ``extra_args`` are appended verbatim before the prompt.
    """

    name: str
    provider: str  # "claude" | "opencode" | "scripted"
    model: str = ""
    effort: str = "high"
    command: str = ""
    extra_args: list[str] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    options: dict[str, str] = field(default_factory=dict)

    def build_command(self, prompt: str) -> list[str]:
        """Render the real CLI argv via the provider adapter (see providers.py)."""
        from .providers import provider_for  # local import: providers depends on config

        return provider_for(self).build_command_for(self, prompt)


# The keys a profile mapping under `profiles:` may contain. `options` is
# itself a free mapping of provider-specific knobs; its *contents* are the
# provider adapter's to validate, its presence is checked here.
PROFILE_KEYS = (
    "provider",
    "model",
    "effort",
    "command",
    "extra_args",
    "timeout_seconds",
    "options",
)

EXECUTION_KEYS = (
    "default_timeout_seconds",
    "max_correction_attempts",
    "env_allowlist",
    "env_allowlist_extra",
    "worktree_dir",
)

# The environment an agent, a pre-merge verification command and a LOCAL
# validation command start from: these variables of the operator's
# environment and nothing else (see ``autoforge.executor.select_environment``).
# What a coding agent needs to find its tools, its home configuration, its
# locale and its temporary directory; what ``git`` needs to identify the
# author and reach a remote; what ``gh`` needs to authenticate; and what an
# HTTPS client needs behind a corporate proxy or a private CA. The API keys
# a provider's own CLI reads are contributed by the provider adapter
# (``AgentProvider.environment_names``), not listed here, so an OpenCode
# launch never carries an Anthropic key it has no use for. A trailing ``*``
# is a prefix. Anything else the operator's shell holds -- a cloud
# credential, a database password, another project's token -- is not
# inherited; ``env_allowlist_extra`` adds a name without restating this list.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TZ",
    "LANG",
    "LANGUAGE",
    "LC_*",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    "SSH_AUTH_SOCK",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_CONFIG_GLOBAL",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_HOST",
    "GH_CONFIG_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


@dataclass
class ExecutionConfig:
    default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    # How many times a *malformed CONTROL_RESULT* (exit 0) triggers a
    # correction prompt before the step fails. 0 disables correction.
    max_correction_attempts: int = 1
    # The environment agents and repository-defined commands start from
    # (``env_allowlist`` replaces the default list; ``env_allowlist_extra``
    # adds to whichever list is in force). See ``DEFAULT_ENV_ALLOWLIST``.
    env_allowlist: list[str] = field(default_factory=lambda: list(DEFAULT_ENV_ALLOWLIST))
    env_allowlist_extra: list[str] = field(default_factory=list)
    # Where a REMOTE run's per-issue agent worktrees are created. Empty (the
    # default): ``<git common dir>/autoforge/worktrees``, outside every
    # working tree of the checkout; otherwise a directory, relative to the
    # controller's working directory, that holds one worktree per issue.
    worktree_dir: str = ""

    def environment_names(self) -> tuple[str, ...]:
        """The allow-list in force: the base list plus the additions, deduplicated."""
        return tuple(dict.fromkeys([*self.env_allowlist, *self.env_allowlist_extra]))


# Paths whose contents define what the hosted checks actually run. See
# ``SafetyConfig.protected_merge_paths``.
DEFAULT_PROTECTED_MERGE_PATHS = (".github/workflows/",)


# The keys `safety:` may contain. Anything else is a hard error: a typo such
# as `allow_merges: true` must not fail closed *silently*, because the operator
# then believes the gate is in the state they wrote, not the state it is in.
# Every other section holds itself to the same rule (see `_section`): most of
# them carry loop bounds or merge behaviour, where a misspelled key keeps a
# looser built-in bound than the operator wrote.
SAFETY_KEYS = (
    "allow_merge",
    "protected_merge_paths",
    "required_checks",
    "verify_check_definition",
)

# `safety.required_checks` when no config file sets it: the aggregate check
# name of this repository's own `.github/workflows/ci.yml`.
DEFAULT_REQUIRED_CHECKS = ("ci",)

# `safety.allow_merge` when no config file sets it.
DEFAULT_ALLOW_MERGE_SOURCE = "built-in default"


@dataclass
class SafetyConfig:
    # Controller invariant: no real merge unless this is true AND the CLI
    # passes --allow-merge. Default off for this milestone. This is the ONLY
    # key that opens the gate: the historical `execution.allow_merge` is
    # rejected on load rather than read, so two keys can never disagree about
    # the most dangerous setting in the file.
    allow_merge: bool = False
    # Where `allow_merge` came from (`<config file>: safety.allow_merge`, or
    # the built-in default) so `doctor` can show the effective gate state and
    # the operator can find the line that set it.
    allow_merge_source: str = DEFAULT_ALLOW_MERGE_SOURCE
    # Paths a PR may not change and still be merged unattended. The hosted
    # checks the merge gate trusts ("every check on the PR succeeded") are
    # defined by the workflow files *in the PR itself*: GitHub runs the PR's
    # version of `.github/workflows/` and reports the result under the same
    # check name, so a PR that edits them also edits the meaning of its own
    # green result. The controller cannot verify that from the outside, so
    # it refuses to merge such a PR unattended and leaves it to a human.
    # Entries ending in "/" are directory prefixes; anything else is an exact
    # path or an fnmatch pattern (where "*" also spans "/"). An empty list
    # disables the gate.
    protected_merge_paths: list[str] = field(
        default_factory=lambda: list(DEFAULT_PROTECTED_MERGE_PATHS)
    )
    # Status-check contexts the default branch is expected to *require*
    # (repository ruleset or classic branch protection). The merge gate
    # trusts "every check on the PR succeeded", which says nothing about
    # whether any check had to exist: with the requirement gone, a PR with no
    # check runs is vacuously green. `autoforge doctor` reads the effective
    # branch rules and fails when one of these contexts is not required. An
    # empty list only requires that *some* status check is required.
    required_checks: list[str] = field(default_factory=lambda: list(DEFAULT_REQUIRED_CHECKS))
    # Whether READY_FOR_MERGE / MERGE also verify *where* each required check
    # came from. A green `ci` proves that the workflow defining it ran to
    # completion on the PR; it does not prove that the workflow was the one
    # the base branch defines. With this on, the controller resolves each
    # `required_checks` context to the GitHub Actions run that produced it,
    # requires that run to be at the reviewed HEAD, and requires its job and
    # step structure (job names, step names in order) to equal that of the
    # base branch's own most recent run of the same workflow at the base
    # branch's current tip. A redefined, extended or trimmed workflow is a
    # difference and BLOCKS the merge; so does a required context that is not
    # an Actions check run, appears more than once, or has no base-branch run
    # to compare against. This is a check on the *definition* that produced
    # the green result, complementary to `protected_merge_paths` (which reads
    # the PR's file list instead of GitHub's run record); neither proves what
    # the tests in the PR assert (see `merge.verification_commands`).
    verify_check_definition: bool = True

    def protects(self, path: str) -> bool:
        """Whether ``path`` (a POSIX repo-relative path) is protected."""
        for pattern in self.protected_merge_paths:
            if pattern.endswith("/"):
                if path == pattern[:-1] or path.startswith(pattern):
                    return True
            elif path == pattern or fnmatch.fnmatchcase(path, pattern):
                return True
        return False


GITHUB_KEYS = ("command", "timeout_seconds")


@dataclass
class GitHubConfig:
    command: str = "gh"
    timeout_seconds: int = 120


MERGE_METHODS = ("squash", "merge", "rebase")

MERGE_KEYS = ("method", "delete_branch", "max_verification_attempts", "verification_commands")


@dataclass
class MergeConfig:
    """How the *controller* merges once the gate is open (never an agent)."""

    method: str = "squash"  # squash | merge | rebase  -> gh pr merge --<method>
    delete_branch: bool = False  # gh pr merge --delete-branch
    # Inconclusive GitHub data (mergeability UNKNOWN, checks still running,
    # post-merge re-read failed) is re-checked on `resume` at most this many
    # times per phase; then the run is BLOCKED instead of retrying forever.
    max_verification_attempts: int = 5
    # Controller-owned verification of the reviewed HEAD *before* the merge,
    # on the operator's machine. The hosted check runs the PR's own code, so
    # a PR that weakens what its tests assert produces a genuine green check
    # without touching any protected path; nothing GitHub reports can tell
    # that apart from a real pass. These argv arrays (never shell strings)
    # are the controller's own evidence: once every GitHub-side check has
    # passed, the exact reviewed commit is exported -- `git read-tree` +
    # `git checkout-index` into a private temporary directory, never the
    # operator's checkout, never a worktree or branch -- and each command
    # runs there in order, under `execution.default_timeout_seconds`. Any
    # non-zero exit or timeout BLOCKS the merge for a human. A pass is
    # persisted against the HEAD and the command list, so MERGE does not
    # repeat it for the same commit. An empty list disables this
    # verification: the commands are the project's own (`pytest`, `make
    # check`, ...) and there is no build-system detection.
    verification_commands: list[list[str]] = field(default_factory=list)


REPLAN_KEYS = (
    "enabled",
    "soft_threshold",
    "hard_threshold",
    "stagnation_window",
    "max_findings_per_round",
    "max_replans_per_issue",
)


@dataclass
class ReplanConfig:
    """Controller policy for abandoning a non-converging implementation."""

    enabled: bool = True
    # First review round from which the controller may *replace* an
    # implementation instead of continuing to patch it. Every stagnation
    # trigger is gated behind it, including `workflow.stagnation_*`: below
    # this round a stagnant loop still ends in BLOCKED for a human, which is
    # far less destructive than discarding a PR after one ineffective FIX.
    soft_threshold: int = 12
    # Review round at which findings trigger a replan unconditionally.
    hard_threshold: int = 20
    # Window rule, from `soft_threshold`: this many trailing review rounds
    # with findings, each holding at most `max_findings_per_round`, trigger a
    # replan. It deliberately counts rounds of entirely new findings too --
    # recurrence is what `workflow.stagnation_*` detects, and that verdict is
    # already a replan trigger from the same round; this rule exists for the
    # long tail of small, fresh findings that never ends.
    stagnation_window: int = 3
    max_findings_per_round: int = 2
    # Counts only completed REPLAN_REEXECUTE lifecycles; initial implementation
    # is not a replan.
    max_replans_per_issue: int = 2


REVIEW_KEYS = ("replan",)


@dataclass
class ReviewConfig:
    replan: ReplanConfig = field(default_factory=ReplanConfig)


WORKFLOW_KEYS = (
    "max_review_rounds",
    "stagnation_identical_rounds",
    "stagnation_unchanged_count_rounds",
    "max_total_steps",
    "epic_update_every",
)


@dataclass
class WorkflowConfig:
    """Controller-owned bounds on the workflow loop (see ``loop_guard.py``).

    Every bound is enforced from persisted state, so ``resume`` continues
    the same budget instead of starting a fresh one. Reaching a bound enters
    BLOCKED with an explicit reason; nothing is merged, pushed or retried.
    """

    # Review rounds a single PR may consume. Round N with findings, where
    # N == max_review_rounds, is BLOCKED instead of starting another FIX
    # (a FIX whose result could never be reviewed is never invoked); a
    # clean round N still reaches READY_FOR_MERGE. Round N+1 never starts.
    max_review_rounds: int = 20
    # Consecutive review rounds with findings whose required resolutions are
    # identical (normalised text) before the loop is declared stagnant.
    # 0 disables this rule (the hard cap above still applies); 1 is rejected
    # because the rule compares rounds against each other.
    stagnation_identical_rounds: int = 2
    # Consecutive review rounds with findings whose finding *count* never
    # changed, while at least one required resolution recurs within those
    # rounds (A/B/A ping-pong), before the loop is declared stagnant. Rounds
    # of entirely new findings are progress and only meet the cap above.
    # 0 disables this rule; 1 is rejected (a one-round window can hold no
    # recurrence, so it would disable the rule while looking enabled).
    stagnation_unchanged_count_rounds: int = 3
    # Cumulative executed steps for the whole run (all issues, all phases,
    # across `resume`). Persisted as ``step_count``; the CLI's ``--max-steps``
    # is only a per-invocation slice of this budget.
    max_total_steps: int = 300
    # Merged PRs per EPIC roadmap update. UPDATE_EPIC still runs after every
    # MERGE (it is where the next issue is chosen), but the controller only
    # asks for, writes and reads back the EPIC's managed roadmap section, and
    # resets ``merged_since_epic_update``, once that many merges have
    # accumulated since the last verified write. 1 updates after every merge.
    epic_update_every: int = 1


LOCAL_KEYS = (
    "feature_dir",
    "validation_commands",
    "max_fix_rounds",
    "exclude",
    "max_workspace_entries",
    "max_workspace_bytes",
)


@dataclass
class LocalConfig:
    """LOCAL-mode settings (``local:`` in the config file).

    ``validation_commands`` are **controller-owned** checks: argv arrays, run
    through :mod:`autoforge.executor` with no shell, after every local
    implementation and fix phase. A non-zero exit means the phase is not
    successfully verified. There is no automatic build-system detection: what
    is not configured here is not run.

    ``exclude`` is the *only* way to remove anything from the workspace
    snapshot that binds a LOCAL review (the repository's own git directory
    aside, which is identified by inode). A LOCAL run hashes every entry of
    the working tree, ignored files included, because the alternative is
    letting a ``.gitignore`` edit decide which bytes a review covers. That
    completeness has a cost the operator has to pay explicitly: build output,
    virtual environments and caches are rewritten by the very validation
    commands the controller runs, so they must be declared unreviewed rather
    than silently tolerated. Each pattern is matched component-wise against
    repository-relative paths; ``*`` and ``?`` match within one component and
    ``**`` spans any number of them. The patterns are hashed into the
    fingerprint and named to the review agent in its prompt, so "what was not
    reviewed" is part of the review's identity rather than a local detail.

    ``max_workspace_entries`` / ``max_workspace_bytes`` bound the walk. They
    are refusal thresholds, not sampling thresholds: exceeding one fails the
    run with the largest subtrees named, because a fingerprint that fell back
    to metadata for the rest would accept an equal-sized replacement with a
    restored mtime.
    """

    # Where ``autoforge local init`` writes feature specifications. Project
    # data, never under the runtime state directory.
    feature_dir: str = "features"
    validation_commands: list[list[str]] = field(default_factory=list)
    # Local review/fix bound: the initial REVIEW, at most this many FIX
    # rounds, then a final REVIEW. 0 disables FIX entirely (one review pass).
    max_fix_rounds: int = 1
    # Declared-unreviewed regions of the working tree (see the class docstring).
    exclude: list[str] = field(default_factory=list)
    max_workspace_entries: int = DEFAULT_MAX_ENTRIES
    max_workspace_bytes: int = DEFAULT_MAX_BYTES

    @property
    def max_review_rounds(self) -> int:
        """Review passes a local run may complete (fix rounds + the first)."""
        return self.max_fix_rounds + 1


# The keys a config file may contain at top level: the scalars read directly
# below plus one entry per section. There is no `repository` key: the
# repository comes from the issue URLs on the command line.
TOP_LEVEL_KEYS = (
    "version",
    "state_dir",
    "prompt_version",
    "execution",
    "safety",
    "github",
    "merge",
    "review",
    "workflow",
    "local",
    "profiles",
)


@dataclass
class AutoForgeConfig:
    version: int = CONFIG_VERSION
    state_dir: str = DEFAULT_STATE_DIR
    prompt_version: str = __prompt_version__
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    local: LocalConfig = field(default_factory=LocalConfig)
    profiles: dict[str, ProfileConfig] = field(default_factory=dict)

    def profile(self, name: str) -> ProfileConfig:
        try:
            return self.profiles[name]
        except KeyError:
            raise ConfigurationError(f"unknown execution profile {name!r}") from None

    @property
    def merge_allowed_by_config(self) -> bool:
        """The config half of the merge gate: `safety.allow_merge` and nothing else."""
        return bool(self.safety.allow_merge)


def _claude_profile(name: str) -> ProfileConfig:
    return ProfileConfig(
        name=name,
        provider="claude",
        model="fable",
        effort="high",
        command="claude",
        extra_args=[],
        options={"permission_mode": "bypassPermissions", "output_format": "text"},
    )


def _opencode_profile(name: str, model: str, effort: str, timeout: int = 1800) -> ProfileConfig:
    return ProfileConfig(
        name=name,
        provider="opencode",
        model=model,
        effort=effort,
        command="opencode",
        extra_args=[],
        timeout_seconds=timeout,
        options={"output_format": "default", "auto_approve": "false"},
    )


def default_config() -> AutoForgeConfig:
    """Built-in defaults mirroring autoforge.example.yaml."""
    profiles = {
        "analyze_execute": _claude_profile("analyze_execute"),
        "fix": _claude_profile("fix"),
        "review_round_1": _opencode_profile("review_round_1", "openai/gpt-5.6-luna", "high"),
        "review_round_2_5": _opencode_profile("review_round_2_5", "openai/gpt-5.6-terra", "high"),
        "review_round_6_plus": _opencode_profile(
            "review_round_6_plus", "openai/gpt-5.6-sol", "medium", timeout=1200
        ),
        "replan_reexecute": _opencode_profile(
            "replan_reexecute", "openai/gpt-5.6-terra", "high", timeout=3600
        ),
        "update_epic": _opencode_profile("update_epic", "openai/gpt-5.6-sol", "high", timeout=1200),
    }
    return AutoForgeConfig(profiles=profiles)


# -- validation ------------------------------------------------------------
def validate_profile(profile: ProfileConfig) -> None:
    """Raise ConfigurationError when a profile cannot possibly be executed."""
    if profile.provider not in KNOWN_PROVIDERS:
        raise ConfigurationError(
            f"profile {profile.name!r}: unknown provider {profile.provider!r} "
            f"(expected one of {KNOWN_PROVIDERS})"
        )
    if profile.provider != "scripted" and not profile.model:
        raise ConfigurationError(f"profile {profile.name!r}: 'model' must be set")
    if profile.timeout_seconds <= 0:
        raise ConfigurationError(f"profile {profile.name!r}: timeout_seconds must be > 0")
    from .providers import provider_for

    provider_for(profile).validate_profile(profile)


def validate_required_profiles(cfg: AutoForgeConfig, names: list[str]) -> None:
    """Fail early (ConfigurationError) if any required profile is missing/invalid."""
    missing = [n for n in names if n not in cfg.profiles]
    if missing:
        raise ConfigurationError(
            f"required execution profile(s) not configured: {', '.join(missing)} "
            "— add them under 'profiles:' in the AutoForge config"
        )
    for n in names:
        validate_profile(cfg.profiles[n])


# -- file loading ----------------------------------------------------------
def load_config_file(path: str | Path | None) -> AutoForgeConfig:
    """Load config from file, or defaults when path is None.

    Raises ConfigurationError on any problem.
    """
    cfg = default_config()
    if path is None:
        return cfg
    p = Path(path)
    if not p.exists():
        raise ConfigurationError(f"config file not found: {p}")
    suffix = p.suffix.lower()
    data: object
    try:
        if suffix == ".toml":
            import tomllib

            data = tomllib.loads(p.read_text(encoding="utf-8"))
        elif suffix == ".json":
            import json

            data = json.loads(
                p.read_text(encoding="utf-8"), object_pairs_hook=_pairs_without_duplicates
            )
        elif suffix in (".yaml", ".yml"):
            data = _load_yaml(p)
        else:
            raise ConfigurationError(
                f"unsupported config extension {suffix!r} (use .yaml/.yml/.toml/.json)"
            )
    except ConfigurationError:
        raise
    except Exception as exc:  # parse errors -> ConfigurationError
        raise ConfigurationError(f"cannot parse config {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"config {p} must contain a mapping at top level")
    return _merge_config(cfg, data, source=str(p))


# Every parser backend must hand `_merge_config` the document as written: a
# key the operator wrote and the loader never saw is the silent no-op that
# `_reject_unknown_keys` exists to refuse, and a duplicate key is exactly
# that -- `json.loads`, PyYAML and the subset parser all keep the last value
# and drop the earlier one, typo included, before any validation runs. TOML
# (`tomllib`) rejects a duplicate on its own; the other three are told to.
def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``json.loads`` object hook: a repeated key is a parse error, not a merge."""
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _as_options(raw: object, source: str, name: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{source}: profile {name!r} 'options' must be a mapping")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(v, bool):
            out[str(k)] = "true" if v else "false"
        else:
            out[str(k)] = "" if v is None else str(v)
    return out


def _as_str_list(raw: object, source: str, key: str) -> list[str]:
    """A list of non-empty strings -- never a bare string silently split.

    An explicit ``null`` is rejected rather than read as ``[]``. The empty
    list is a deliberate opt-out (for ``safety.protected_merge_paths`` it
    turns the merge gate off), and a key whose value went missing -- a
    hand-edited config, a generator emitting nothing, a list the YAML subset
    parser could not read -- must not be able to disable a safety gate by
    looking like one.
    """
    if raw is None:
        raise ConfigurationError(
            f"{source}: {key} is null; write [] to set it empty deliberately, "
            "or remove the key to keep the default"
        )
    if isinstance(raw, str) or not isinstance(raw, list):
        raise ConfigurationError(f"{source}: {key} must be a list of strings, got {raw!r}")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigurationError(
                f"{source}: {key} must contain non-empty strings, got {item!r}"
            )
        out.append(item.strip())
    return out


def _as_argv_list(raw: object, source: str, key: str) -> list[list[str]]:
    """A list of argv arrays -- never a shell string.

    ``[["./gradlew", "test"]]`` is accepted; ``["./gradlew test"]`` is not.
    A command line is not parsed, split or handed to a shell anywhere in
    AutoForge, so accepting a string here would create the one place where a
    quoting bug turns into arbitrary command execution.
    """
    if raw is None:
        raise ConfigurationError(
            f"{source}: {key} is null; write [] to set it empty deliberately, "
            "or remove the key to keep the default"
        )
    if isinstance(raw, str) or not isinstance(raw, list):
        raise ConfigurationError(f"{source}: {key} must be a list of argv arrays, got {raw!r}")
    out: list[list[str]] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, str) or not isinstance(entry, list) or not entry:
            raise ConfigurationError(
                f"{source}: {key}[{index}] must be a non-empty argv array such as "
                f'["pytest", "-q"] -- a shell command string is never accepted, got {entry!r}'
            )
        argv: list[str] = []
        for item in entry:
            if not isinstance(item, str) or not item.strip():
                raise ConfigurationError(
                    f"{source}: {key}[{index}] must contain non-empty strings, got {item!r}"
                )
            # Stored verbatim, unlike `_as_str_list`: an argv element reaches
            # the process exactly as written, so surrounding whitespace is
            # part of the argument, not trimmed away. Only a blank element is
            # refused, since it can never be anything but a slip.
            argv.append(item)
        out.append(argv)
    return out


def _as_bool(raw: object, source: str, key: str) -> bool:
    """Accept only a real boolean — never coerce strings like "false" to True."""
    if isinstance(raw, bool):
        return raw
    raise ConfigurationError(f"{source}: {key!r} must be a boolean (true/false), got {raw!r}")


def _as_int(raw: object, source: str, key: str) -> int:
    """Accept only a real integer (bool excluded); raise ConfigurationError otherwise."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    raise ConfigurationError(f"{source}: {key!r} must be an integer, got {raw!r}")


def _reject_unknown_keys(mapping: dict, known: tuple[str, ...], source: str, label: str) -> None:
    """Fail on any key of ``mapping`` that the loader would not read.

    Every known key is read with ``if key in section``, so a key that is not
    known is not a wrong value: it is a *silent no-op* that leaves the
    built-in default in force while ``doctor`` calls the file valid. For a
    loop bound or a merge setting that means a looser bound than the operator
    wrote, so the typo is an error, in the same shape for every section.
    """
    unknown = sorted(str(k) for k in mapping if k not in known)
    if unknown:
        raise ConfigurationError(
            f"{source}: unknown key(s) under '{label}': {', '.join(unknown)} "
            f"(known: {', '.join(known)})"
        )


def _section(
    data: dict,
    key: str,
    source: str,
    known: tuple[str, ...] | None,
    label: str | None = None,
    removed: dict[str, str] | None = None,
) -> dict:
    """A config section: absent or ``null`` is the built-in default, else a mapping.

    ``null`` is what the YAML reader produces for a section header with every
    child commented out (``safety:`` alone on its line), and an empty section
    yields the same fail-closed defaults as an omitted one. Any other
    non-mapping value (``[]``, ``false``, ``""``, ``0``, a string) is rejected
    rather than normalised into an omitted section: ``data.get(key, {}) or
    {}`` would turn ``safety: []`` into a silently closed gate and let ``doctor``
    call that configuration valid.

    ``known`` is the closed set of keys the section may contain; any other key
    is rejected (see ``_reject_unknown_keys``). ``None`` is for a section
    whose keys are *names* rather than a schema (``profiles``), where the
    check belongs on each named mapping instead. ``removed`` maps a key that
    used to be read to the message explaining where it went, so a config from
    an older controller gets that explanation rather than "unknown key".
    """
    raw = data.get(key)
    if raw is None:
        return {}
    name = label or key
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{source}: '{name}' must be a mapping, got {raw!r}")
    for old_key, message in (removed or {}).items():
        if old_key in raw:
            raise ConfigurationError(f"{source}: {message}")
    if known is not None:
        _reject_unknown_keys(raw, known, source, name)
    return raw


# The merge gate once had a second, deprecated location. It is not read any
# more -- not even as `false` -- because a config that sets both can be
# "disabled" in one place and still open in the other.
REMOVED_EXECUTION_KEYS = {
    "allow_merge": (
        "'execution.allow_merge' is no longer supported; the merge gate is "
        "'safety.allow_merge' only. Remove the key from 'execution' (moving it to "
        "'safety' if you meant to open the gate)"
    ),
}


def _merge_config(base: AutoForgeConfig, data: dict, source: str) -> AutoForgeConfig:
    _reject_unknown_keys(data, TOP_LEVEL_KEYS, source, "the top level")
    version = data.get("version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ConfigurationError(
            f"{source}: unsupported config version {version!r} (expected {CONFIG_VERSION})"
        )
    if "state_dir" in data:
        base.state_dir = str(data["state_dir"])
    if "prompt_version" in data:
        base.prompt_version = str(data["prompt_version"])
    exe = _section(data, "execution", source, EXECUTION_KEYS, removed=REMOVED_EXECUTION_KEYS)
    if "default_timeout_seconds" in exe:
        timeout = _as_int(
            exe["default_timeout_seconds"], source, "execution.default_timeout_seconds"
        )
        if timeout <= 0:
            # The executor treats a non-positive timeout as "no timeout at
            # all", and this value is what controller-owned work (a LOCAL
            # validation command, a profile that does not override it) runs
            # under. An unbounded subprocess would hold the repository lock
            # forever, so the bound is required rather than optional.
            raise ConfigurationError(
                f"{source}: 'execution.default_timeout_seconds' must be > 0, got {timeout} "
                "(a non-positive value would disable the timeout entirely)"
            )
        base.execution.default_timeout_seconds = timeout
    if "max_correction_attempts" in exe:
        base.execution.max_correction_attempts = _as_int(
            exe["max_correction_attempts"], source, "execution.max_correction_attempts"
        )
    for key in ("env_allowlist", "env_allowlist_extra"):
        if key in exe:
            names = _as_str_list(exe[key], source, f"execution.{key}")
            for name in names:
                if not is_env_pattern(name):
                    raise ConfigurationError(
                        f"{source}: 'execution.{key}' entry {name!r} is not an environment "
                        "variable name or a name prefix followed by '*'"
                    )
            setattr(base.execution, key, names)
    if "env_allowlist" in exe and not base.execution.env_allowlist:
        # An empty environment cannot even find the agent CLI on PATH; the
        # operator who wants a narrower list writes the narrower list.
        raise ConfigurationError(
            f"{source}: 'execution.env_allowlist' must name at least one variable "
            "(remove the key to keep the default list, or use env_allowlist_extra to add)"
        )
    if "worktree_dir" in exe:
        raw_dir = exe["worktree_dir"]
        if raw_dir is None or raw_dir == "":
            base.execution.worktree_dir = ""
        elif not isinstance(raw_dir, str) or not raw_dir.strip():
            raise ConfigurationError(
                f"{source}: 'execution.worktree_dir' must be a path or null, got {raw_dir!r}"
            )
        else:
            base.execution.worktree_dir = raw_dir.strip()
    safety = _section(data, "safety", source, SAFETY_KEYS)
    if "allow_merge" in safety:
        base.safety.allow_merge = _as_bool(safety["allow_merge"], source, "safety.allow_merge")
        base.safety.allow_merge_source = f"{source}: safety.allow_merge"
    if "protected_merge_paths" in safety:
        base.safety.protected_merge_paths = _as_str_list(
            safety["protected_merge_paths"], source, "safety.protected_merge_paths"
        )
    if "required_checks" in safety:
        base.safety.required_checks = _as_str_list(
            safety["required_checks"], source, "safety.required_checks"
        )
    if "verify_check_definition" in safety:
        base.safety.verify_check_definition = _as_bool(
            safety["verify_check_definition"], source, "safety.verify_check_definition"
        )
    gh = _section(data, "github", source, GITHUB_KEYS)
    if "command" in gh:
        base.github.command = str(gh["command"])
    if "timeout_seconds" in gh:
        base.github.timeout_seconds = _as_int(
            gh["timeout_seconds"], source, "github.timeout_seconds"
        )
    merge = _section(data, "merge", source, MERGE_KEYS)
    if "method" in merge:
        method = merge["method"]
        if not isinstance(method, str) or method not in MERGE_METHODS:
            raise ConfigurationError(
                f"{source}: 'merge.method' must be one of {MERGE_METHODS}, got {method!r}"
            )
        base.merge.method = method
    if "delete_branch" in merge:
        base.merge.delete_branch = _as_bool(merge["delete_branch"], source, "merge.delete_branch")
    if "max_verification_attempts" in merge:
        attempts = _as_int(
            merge["max_verification_attempts"], source, "merge.max_verification_attempts"
        )
        if attempts < 1:
            raise ConfigurationError(
                f"{source}: 'merge.max_verification_attempts' must be >= 1, got {attempts}"
            )
        base.merge.max_verification_attempts = attempts
    if "verification_commands" in merge:
        base.merge.verification_commands = _as_argv_list(
            merge["verification_commands"], source, "merge.verification_commands"
        )
    review = _section(data, "review", source, REVIEW_KEYS)
    replan = _section(review, "replan", source, REPLAN_KEYS, label="review.replan")
    rp = base.review.replan
    if "enabled" in replan:
        rp.enabled = _as_bool(replan["enabled"], source, "review.replan.enabled")
    for key, minimum in (
        ("soft_threshold", 1),
        ("hard_threshold", 1),
        ("stagnation_window", 1),
        ("max_findings_per_round", 0),
        ("max_replans_per_issue", 0),
    ):
        if key in replan:
            value = _as_int(replan[key], source, f"review.replan.{key}")
            if value < minimum:
                raise ConfigurationError(
                    f"{source}: 'review.replan.{key}' must be >= {minimum}, got {value}"
                )
            setattr(rp, key, value)
    if rp.hard_threshold < rp.soft_threshold:
        raise ConfigurationError(
            f"{source}: 'review.replan.hard_threshold' must be >= 'review.replan.soft_threshold'"
        )
    workflow = _section(data, "workflow", source, WORKFLOW_KEYS)
    for key, minimum in (
        ("max_review_rounds", 1),
        ("max_total_steps", 1),
        ("epic_update_every", 1),
    ):
        if key in workflow:
            value = _as_int(workflow[key], source, f"workflow.{key}")
            if value < minimum:
                raise ConfigurationError(
                    f"{source}: 'workflow.{key}' must be >= {minimum}, got {value}"
                )
            setattr(base.workflow, key, value)
    # Both stagnation rules compare consecutive rounds *against each other*, so
    # a window of 1 has no meaning: it would compare a round with nothing and
    # silently disable the unchanged-count rule (which needs a recurrence
    # inside the window) while making the identical-resolutions rule fire on
    # the first round that has findings at all. Only 0 disables a rule.
    for key in ("stagnation_identical_rounds", "stagnation_unchanged_count_rounds"):
        if key in workflow:
            value = _as_int(workflow[key], source, f"workflow.{key}")
            if value < 0 or value == 1:
                raise ConfigurationError(
                    f"{source}: 'workflow.{key}' must be 0 (rule disabled) or >= 2 "
                    f"(the rule compares consecutive review rounds), got {value}"
                )
            setattr(base.workflow, key, value)
    if base.review.replan.hard_threshold > base.workflow.max_review_rounds:
        raise ConfigurationError(
            f"{source}: 'review.replan.hard_threshold' must be <= 'workflow.max_review_rounds'"
        )
    local = _section(data, "local", source, LOCAL_KEYS)
    if "feature_dir" in local:
        feature_dir = str(local["feature_dir"]).strip()
        if not feature_dir:
            raise ConfigurationError(f"{source}: 'local.feature_dir' must not be empty")
        base.local.feature_dir = feature_dir
    if "validation_commands" in local:
        base.local.validation_commands = _as_argv_list(
            local["validation_commands"], source, "local.validation_commands"
        )
    if "max_fix_rounds" in local:
        value = _as_int(local["max_fix_rounds"], source, "local.max_fix_rounds")
        if value < 0:
            raise ConfigurationError(f"{source}: 'local.max_fix_rounds' must be >= 0, got {value}")
        base.local.max_fix_rounds = value
    if "exclude" in local:
        raw = local["exclude"]
        if isinstance(raw, str) or not isinstance(raw, list):
            raise ConfigurationError(
                f"{source}: 'local.exclude' must be a list of path patterns, e.g. "
                "['.venv', '**/__pycache__']"
            )
        patterns: list[str] = []
        for item in raw:
            try:
                patterns.append(normalize_exclude_pattern(item))
            except ConfigurationError as exc:
                raise ConfigurationError(f"{source}: {exc}") from None
        base.local.exclude = sorted(dict.fromkeys(patterns))
    for name, attr, minimum in (
        ("max_workspace_entries", "max_workspace_entries", 1),
        ("max_workspace_bytes", "max_workspace_bytes", 1),
    ):
        if name in local:
            value = _as_int(local[name], source, f"local.{name}")
            if value < minimum:
                raise ConfigurationError(
                    f"{source}: 'local.{name}' must be >= {minimum}, got {value}"
                )
            setattr(base.local, attr, value)
    # Keyed by profile *name*, so the closed key set applies to each profile
    # mapping rather than to the section.
    profiles = _section(data, "profiles", source, known=None)
    for name, p in profiles.items():
        # YAML keys are not necessarily strings (`1:`, `true:`, `null:`), and
        # a name is looked up, sorted and printed as one: a non-string would
        # load here and fail as a `TypeError` in `doctor` instead.
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError(
                f"{source}: profile names must be non-empty strings, got {name!r}"
            )
        if not isinstance(p, dict):
            raise ConfigurationError(f"{source}: profile {name!r} must be a mapping")
        _reject_unknown_keys(p, PROFILE_KEYS, source, f"profiles.{name}")
        if name in base.profiles:
            cur = base.profiles[name]
            if "provider" in p:
                cur.provider = str(p["provider"])
            if "model" in p:
                cur.model = str(p["model"])
            if "effort" in p:
                cur.effort = str(p["effort"])
            if "command" in p:
                cur.command = str(p["command"])
            if "extra_args" in p:
                cur.extra_args = [str(a) for a in (p["extra_args"] or [])]
            if "timeout_seconds" in p:
                cur.timeout_seconds = _as_int(
                    p["timeout_seconds"], source, f"profiles.{name}.timeout_seconds"
                )
            if "options" in p:
                cur.options.update(_as_options(p["options"], source, name))
        else:
            base.profiles[name] = ProfileConfig(
                name=name,
                provider=str(p.get("provider", "opencode")),
                model=str(p.get("model", "")),
                effort=str(p.get("effort", "high")),
                command=str(p.get("command", "")),
                extra_args=[str(a) for a in (p.get("extra_args") or [])],
                timeout_seconds=_as_int(
                    p.get("timeout_seconds", base.execution.default_timeout_seconds),
                    source,
                    f"profiles.{name}.timeout_seconds",
                ),
                options=_as_options(p.get("options"), source, name),
            )
    return base


def _load_yaml(path: Path) -> object:
    """The YAML document as parsed; ``load_config_file`` checks the root type.

    Only an *empty* document (no content, or comments only) reads as ``{}``:
    that is what the subset parser produces for it, and it means "all
    defaults" the same way an omitted file does. Any other non-mapping root
    (``false``, a list, a bare string) is returned as is so the root check
    rejects it on both backends alike, rather than PyYAML alone reading it as
    the built-in configuration.
    """
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ImportError:
        return _minimal_yaml_parse(text)

    class UniqueKeySafeLoader(yaml.SafeLoader):
        """``SafeLoader`` that refuses a key repeated within one mapping.

        PyYAML keeps the last value of a repeated key, so the earlier one --
        and any typo in it -- would vanish before ``_reject_unknown_keys``
        runs. Merge keys (``<<``) are left to PyYAML: overriding a merged
        key is what they are for, not a duplicate.
        """

        def construct_mapping(self, node: object, deep: bool = False) -> dict:
            seen: set[object] = set()
            for key_node, _value_node in node.value:  # type: ignore[attr-defined]
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=deep)
                try:
                    repeated = key in seen
                except TypeError:  # unhashable key: PyYAML reports it below
                    continue
                if repeated:
                    raise ValueError(f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
                seen.add(key)
            return super().construct_mapping(node, deep=deep)

    data = yaml.load(text, Loader=UniqueKeySafeLoader)  # a SafeLoader subclass
    return {} if data is None else data


def _minimal_yaml_parse(text: str) -> object:
    """Minimal YAML-subset parser for our config shape.

    Supports: nested maps via 2-space indentation, lists via "- " items,
    inline scalars (int/float/bool/null/quoted strings), each resolved
    exactly as PyYAML resolves it. Anything fancier raises ValueError telling
    the user to install PyYAML rather than reading it as the string it looks
    like (see the note below); like PyYAML's own errors it reaches the
    operator as ``cannot parse config <path>: ...``, and like PyYAML the root
    is returned as parsed for ``load_config_file`` to check.
    """
    return _parse_yaml_subset(text)


# -- the YAML subset parser ------------------------------------------------
#
# Two backends read the same `.yaml` file: PyYAML for an operator who
# installed `autoforge[yaml]`, this parser for one who did not. A scalar the
# two resolve *differently* is the failure mode the parser below is built
# against (#40). The file it feeds decides routing, loop bounds, timeouts and
# `safety.allow_merge`, and nothing downstream can tell that `on` was read as
# a string here and as `True` there, or `0600` as 600 here and 384 there.
#
# The rule is therefore: resolve exactly what PyYAML's YAML 1.1 implicit
# resolvers resolve, to exactly the same value, and refuse everything else
# outright. A refusal reaches the operator as `cannot parse config <path>`;
# a quietly different value reaches them as a different controller. Whatever
# this parser accepts is proven equal to PyYAML's reading of it, scalar by
# scalar and document by document, in `tests/test_config.py`.

# PyYAML's implicit resolvers (`yaml/resolver.py`), reproduced. Anything a
# resolver claims that this parser does not implement -- a timestamp -- is
# refused rather than kept as the string it looks like.
_YAML_NULL = frozenset({"", "~", "null", "Null", "NULL"})
_YAML_TRUE = frozenset({"yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"})
_YAML_FALSE = frozenset({"no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF"})

_YAML_INT_RE = re.compile(
    r"""^(?:[-+]?0b[0-1_]+
        |[-+]?0[0-7_]+
        |[-+]?(?:0|[1-9][0-9_]*)
        |[-+]?0x[0-9a-fA-F_]+
        |[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$""",
    re.X,
)

_YAML_FLOAT_RE = re.compile(
    r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
        |\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
        |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
        |[-+]?\.(?:inf|Inf|INF)
        |\.(?:nan|NaN|NAN))$""",
    re.X,
)

_YAML_TIMESTAMP_RE = re.compile(
    r"""^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]
        |[0-9][0-9][0-9][0-9] -[0-9][0-9]? -[0-9][0-9]?
         (?:[Tt]|[ \t]+)[0-9][0-9]?
         :[0-9][0-9] :[0-9][0-9] (?:\.[0-9]*)?
         (?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$""",
    re.X,
)

# A plain scalar never starts with one of these: each introduces YAML this
# parser does not implement (anchor, alias, tag, block scalar, flow mapping,
# directive, reserved indicator) or is a syntax error under PyYAML. Reading
# any of them as the string it looks like is exactly the silent disagreement
# to avoid, so they are refused.
_UNSUPPORTED_FIRST_CHARS = "&*!|>{}],%@`"

# YAML's whitespace, line breaks and printable set -- none of which is
# Python's. Between tokens PyYAML's scanner skips a space or a tab and nothing
# else, so every *other* character `str.strip()` treats as whitespace (U+00A0,
# U+1680, U+2000-U+200A, U+202F, U+205F, U+3000) is an ordinary character of
# the plain scalar it sits in. Stripping lines, keys, values and items with
# `str.strip()` dropped them silently: `safety.allow_merge: <NBSP>true`
# resolved to True and opened the merge gate here, while PyYAML read the
# string '\xa0true' and the loader refused the file. `str.splitlines()` is the
# same mistake one layer down -- it breaks on U+000B, U+000C, U+001C, U+001D
# and U+001E, which are not line breaks in YAML but characters PyYAML's
# *reader* refuses outright, so `version: 1<U+000C>safety:` was read here as
# two lines and opened the gate against a file the other backend cannot load.
_YAML_WHITESPACE = " \t"

# `\n`, `\r\n`, `\r`, U+0085, U+2028 and U+2029 -- `yaml.scanner.scan_line_break`.
_YAML_LINE_BREAK_RE = re.compile("\r\n|[\n\r\x85\u2028\u2029]")

# `yaml.reader.Reader.NON_PRINTABLE`, reproduced.
_YAML_NON_PRINTABLE_RE = re.compile(
    "[^\t\n\r\x20-\x7e\x85\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)


def _yaml_strip(text: str) -> str:
    """Strip YAML's whitespace -- a space or a tab -- and nothing else.

    A Unicode space is kept because PyYAML keeps it: it is a character of the
    scalar, and dropping it made the two backends read the same file
    differently (see the note above).
    """
    return text.strip(_YAML_WHITESPACE)


def _yaml_lines(text: str) -> list[str]:
    """Split a document on YAML's line breaks, not on Python's wider set."""
    return _YAML_LINE_BREAK_RE.split(text)


def _unsupported(what: str, text: str) -> ValueError:
    """The parser's one error shape: what it could not read, and the way out."""
    return ValueError(
        f"YAML subset parser: {what}: {text!r} — install PyYAML for full YAML support"
    )


def _parse_yaml_subset(text: str) -> object:
    """Small recursive indentation-based parser for the documented subset.

    Raises only ``ValueError``: this parser is one backend among four and
    knows nothing about the file it is reading, so the path that makes the
    message actionable is added by ``load_config_file`` for all of them.
    """
    lines = _yaml_lines(text)
    for line in lines:
        _reject_non_printable(line)
        if "\t" in line:
            _reject_tab(line)
    cleaned: list[tuple[int, str]] = []
    for line in lines:
        # Indentation is spaces alone: a tab cannot indent under PyYAML and
        # the pass above has already refused every bare one, and a Unicode
        # space is a character of the scalar rather than indentation.
        indent = len(line) - len(line.lstrip(" "))
        stripped = _yaml_strip(line)
        if not stripped or stripped.startswith("#"):
            continue
        content = _strip_inline_comment(stripped)
        if content:
            cleaned.append((indent, content))
    for _, content in cleaned:
        # `---` and `...` open and close a *document*; a file with more than
        # one is a PyYAML error, and this parser reads a single document, so
        # it never treats either marker as the content it looks like.
        if content in ("---", "...") or content[:4] in ("--- ", "... "):
            raise _unsupported("YAML document markers are not supported at", content)
    pos = 0

    def is_list_item(content: str) -> bool:
        return content == "-" or content.startswith("- ")

    def parse_block(min_indent: int) -> object:
        nonlocal pos
        # decide dict vs list by first line
        if pos >= len(cleaned) or cleaned[pos][0] < min_indent:
            return {}
        if is_list_item(cleaned[pos][1]):
            items: list[object] = []
            # Items of one sequence align exactly. A *deeper* `- x` line is
            # not a sibling to PyYAML but the continuation of the multi-line
            # plain scalar above it (`- a` then `  - b` is the one item
            # "a - b" there), so leave it to the caller, which refuses it.
            item_indent = cleaned[pos][0]
            while (
                pos < len(cleaned)
                and cleaned[pos][0] == item_indent
                and is_list_item(cleaned[pos][1])
            ):
                ind, content = cleaned[pos]
                item_text = _yaml_strip(content[1:])
                pos += 1
                if item_text == "":
                    # An item with nothing after the dash is a nested block,
                    # or -- with nothing indented under it -- null, which is
                    # what PyYAML builds for it; an empty mapping is not.
                    if pos < len(cleaned) and cleaned[pos][0] > ind:
                        items.append(parse_block(ind + 1))
                    else:
                        items.append(None)
                elif _split_key(item_text) is not None:
                    # `- key: value` is a one-key *mapping* to PyYAML, and
                    # `- key: value` + an aligned second key is a two-key one.
                    # Resolving the item as a scalar kept it as the string it
                    # looks like, so `required_checks:\n  - x: y` installed the
                    # check "x: y" here while PyYAML built {"x": "y"} and the
                    # loader refused the file. This parser implements no
                    # mapping inside a sequence, so it refuses the item.
                    raise _unsupported("a mapping inside a sequence item at", content)
                else:
                    items.append(_scalar(item_text))
            return items
        # Keys are resolved like any other scalar, because PyYAML resolves
        # them too: `1:` is the integer 1 and `~:` is None there, and the
        # loader's "profile names must be non-empty strings" check is written
        # for exactly that. A key kept as the string it looks like would pass
        # the check on this backend and fail it on the other.
        mapping: dict[object, object] = {}
        while pos < len(cleaned) and cleaned[pos][0] >= min_indent:
            ind, content = cleaned[pos]
            if ind != min_indent:
                # Over-indented stray line (mapping keys must align).
                raise _unsupported("bad indentation at", content)
            if is_list_item(content):
                break
            split = _split_key(content)
            if split is None:
                raise _unsupported("cannot parse line", content)
            raw_key, rest = split
            if raw_key == "":
                # `: 1` is an explicit-key mapping to PyYAML, which refuses
                # it in block context; only a *quoted* empty key is one.
                raise _unsupported("a mapping key is missing at", content)
            key = _scalar(raw_key)
            try:
                repeated = key in mapping
            except TypeError:
                raise _unsupported("unhashable key at", content) from None
            if repeated:
                raise ValueError(f"duplicate key {key!r} at: {content!r}")
            pos += 1
            if rest == "":
                # look ahead: deeper indent -> nested block, else None
                if pos < len(cleaned) and cleaned[pos][0] > ind:
                    mapping[key] = parse_block(cleaned[pos][0])
                else:
                    mapping[key] = None
            else:
                mapping[key] = _scalar(rest)
        return mapping

    root = parse_block(0)
    # `parse_block` returns at the first line that does not belong to the
    # block it is reading: a nested one leaves that line to its caller, which
    # refuses it, but the top-level call has no caller. So a document that
    # mixes a mapping and a sequence at the root -- `safety:\n  allow_merge:
    # true\n- x\n` -- used to be returned as the part read before the stray
    # line, silently dropping content PyYAML rejects outright. Reading *part*
    # of a malformed file is the one outcome this parser must never produce:
    # the dropped line could be the one that closes the merge gate.
    if pos != len(cleaned):
        raise _unsupported("unexpected content after the document at", cleaned[pos][1])
    return root


def _scan_line(content: str) -> Iterator[tuple[str, bool]]:
    r"""Yield ``(char, quoted)`` for the part of a line that is not a comment.

    One scan serves both callers below, because both need PyYAML's two
    boundary rules and must not disagree about them:

    * A quote *opens* a quoted scalar only where a scalar may **begin** -- at
      the start of the line, after the ``: `` or ``- `` that introduces a
      value or a block sequence item, or after a ``,``, ``[`` or ``{`` inside
      a flow collection. A quote anywhere else is an ordinary character of
      the plain scalar already being read, which is why ``model: don't # why``
      keeps its comment and ``model: a 'b # c'`` is the plain scalar ``a 'b``
      rather than a quoted scalar swallowing the ``#``. Treating *any* space
      as a scalar start read the first as PyYAML does and the second as a
      quoted scalar, and let ``model: a 'b\tc'`` past the tab rule below.
    * A ``#`` starts a comment only after whitespace (or at the start of the
      line), and everything from there on is comment, so the scan stops.
    """
    quote = ""
    at_start = True  # a scalar may begin at the start of a line
    depth = 0  # open flow collections: `,` separates items only inside one
    prev = " "
    for i, ch in enumerate(content):
        if quote:
            yield ch, True
            if ch == quote:
                quote = ""
                at_start = False
        elif ch in "'\"" and at_start:
            quote = ch
            yield ch, True
        elif ch == "#" and (at_start or prev in " \t"):
            return
        else:
            yield ch, False
            if ch in " \t":
                pass  # whitespace separates tokens; it does not end a scalar
            elif ch in "[{" and at_start:
                depth += 1  # a flow collection, whose first item begins next
            elif ch in "]}" and depth:
                depth -= 1
                at_start = False
            elif ch == "," and depth:
                at_start = True  # the next item of that flow collection
            elif ch == ":" and content[i + 1 : i + 2] in ("", " "):
                at_start = True  # a mapping value begins after the colon
            elif ch == "-" and at_start and content[i + 1 : i + 2] == " ":
                pass  # a block sequence indicator: the item begins after it
            else:
                at_start = False
        prev = ch


def _strip_inline_comment(content: str) -> str:
    """Drop a trailing ``#`` comment, ignoring one inside a quoted scalar."""
    return "".join(ch for ch, _ in _scan_line(content)).rstrip(_YAML_WHITESPACE)


def _reject_non_printable(line: str) -> None:
    r"""Refuse the characters ``yaml.reader.Reader`` refuses, as it does.

    PyYAML validates its raw stream before the scanner ever runs, so a
    non-printable character is an ``unacceptable character`` reader error
    wherever it sits -- in a comment, inside a quoted scalar, or in the middle
    of a plain one. Nothing below would have refused one: ``model: ab``
    was read here as the ordinary plain scalar ``ab``, and the five
    characters ``str.splitlines()`` breaks on but YAML does not (U+000B,
    U+000C, U+001C, U+001D, U+001E) turned one line into two, which is how
    ``version: 1<U+000C>safety:\n  allow_merge: true`` opened the merge gate
    on this backend against a file PyYAML refuses to load.

    This is the reader's rule, which is why it is checked over the raw line
    and not through ``_scan_line``: unlike a tab, no quoting makes it legal.
    """
    found = _YAML_NON_PRINTABLE_RE.search(line)
    if found is not None:
        raise _unsupported(f"unacceptable character #x{ord(found.group()):04x} at", line)


def _reject_tab(line: str) -> None:
    r"""Refuse a tab anywhere PyYAML's scanner refuses one.

    PyYAML skips only *spaces* between tokens and ends a plain scalar at a
    tab, so a tab that is neither inside a quoted scalar nor inside a comment
    reaches its scanner as a character that "cannot start any token" -- tab
    indentation, ``model: foo\tbar`` and even a trailing ``model: foo\t`` are
    all scanner errors there. The line cleaning above strips and splits on
    spaces alone, so it would read every one of them as an ordinary plain
    scalar. A tab inside a quoted scalar or a comment is legal under PyYAML
    and read identically here, so only the rest is refused.

    Which tab is inside a quoted scalar is ``_scan_line``'s decision, the
    same one ``_strip_inline_comment`` uses.
    """
    for ch, quoted in _scan_line(line):
        if ch == "\t" and not quoted:
            raise _unsupported("tab outside a quoted scalar at", line)


def _split_key(content: str) -> tuple[str, str] | None:
    """Split ``key: value`` at the colon that ends the key, or ``None``.

    In block context a plain ``:`` ends a key only when a space or the end of
    the line follows it, so ``model: gpt`` is a mapping while ``url:port`` is
    one plain scalar and ``a:b: 1`` is the key ``a:b``. Splitting on the first
    colon instead read ``a:b: 1`` as the key ``a`` with the value ``b: 1``,
    which PyYAML never agreed with. A quoted key is scanned to its closing
    quote first, so ``"a: b": 1`` is one key too.
    """
    i = 0
    if content[:1] in ("'", '"'):
        end = content.find(content[0], 1)
        if end < 0:
            return None
        i = end + 1
    while i < len(content):
        if content[i] == ":" and (i + 1 == len(content) or content[i + 1] == " "):
            return _yaml_strip(content[:i]), _yaml_strip(content[i + 1 :])
        i += 1
    return None


def _reject_plain_indicators(t: str, flow: bool) -> None:
    """Refuse the plain scalars PyYAML's scanner refuses in this context.

    PyYAML ends a plain scalar at a ``:`` that a space or the end of the line
    follows, and reads a leading ``- ``, ``? `` or ``: `` as the block
    indicator each one is, so it never builds the string these look like:
    ``model: x: y`` is `mapping values are not allowed here` there, ``model:
    - x`` is `sequence entries are not allowed here` and ``model: ? x`` is
    `mapping keys are not allowed here`. ``_split_key`` already encodes the
    colon half of that rule for keys; this is the same rule for what remains
    as a value, so the string a plain scalar looks like is never invented
    where PyYAML refuses the file outright.

    Inside a flow collection PyYAML also ends a plain scalar at ``?``
    wherever it appears (``[a?b]`` is a parser error there, while the block
    scalar ``a?b`` is an ordinary string).
    """
    if t == "-" or t.startswith("- "):
        raise _unsupported("sequence entries are not allowed in a plain scalar at", t)
    if t == "?" or t.startswith("? ") or (flow and "?" in t):
        raise _unsupported("mapping keys are not allowed in a plain scalar at", t)
    if t.endswith(":") or ": " in t or (flow and t.startswith(":")):
        raise _unsupported("mapping values are not allowed in a plain scalar at", t)


def _scalar(text: str, flow: bool = False) -> object:
    """One YAML scalar, resolved exactly as PyYAML's implicit resolvers do.

    Anything whose PyYAML reading this parser cannot reproduce is refused
    rather than guessed at (see the note above the resolvers). ``flow`` says
    whether the scalar is an item of a flow sequence, where PyYAML ends a
    plain scalar at more characters than in block context.
    """
    t = _yaml_strip(text)
    if t[:1] in ("'", '"'):
        return _quoted_scalar(t)
    if t.startswith("["):
        return _flow_sequence(t)
    if t[:1] and (t[0] in _UNSUPPORTED_FIRST_CHARS or t in ("=", "<<")):
        raise _unsupported("unsupported YAML at", t)
    _reject_plain_indicators(t, flow)
    if t in _YAML_NULL:
        return None
    if t in _YAML_TRUE:
        return True
    if t in _YAML_FALSE:
        return False
    if _YAML_INT_RE.match(t):
        return _yaml_int(t)
    if _YAML_FLOAT_RE.match(t):
        return _yaml_float(t)
    if _YAML_TIMESTAMP_RE.match(t):
        raise _unsupported("YAML timestamps are not supported at", t)
    return t


def _quoted_scalar(text: str) -> str:
    """A single- or double-quoted scalar, without decoding any escape.

    ``''`` inside a single-quoted scalar and ``\\n`` inside a double-quoted one
    mean something to PyYAML that this parser would keep verbatim, so a quoted
    scalar containing either is refused instead of being read differently.
    """
    quote = text[0]
    if len(text) < 2 or not text.endswith(quote):
        raise _unsupported("unterminated quoted string at", text)
    inner = text[1:-1]
    if quote in inner:
        raise _unsupported("quoted string with an embedded quote at", text)
    if quote == '"' and "\\" in inner:
        raise _unsupported("escape sequence in a double-quoted string at", text)
    return inner


def _flow_sequence(text: str) -> list[object]:
    """An inline ``[a, b]`` list; its items are scalars like any other."""
    if not text.endswith("]"):
        raise _unsupported("unterminated flow sequence at", text)
    inner = _yaml_strip(text[1:-1])
    if not inner:
        return []
    return [_scalar(part, flow=True) for part in _split_inline(inner)]


def _yaml_int(text: str) -> int:
    """``yaml.constructor.SafeConstructor.construct_yaml_int``, reproduced.

    YAML 1.1 has binary, octal (a leading ``0``), hexadecimal and sexagesimal
    integers, so ``0600`` is 384 and ``1:30`` is 90. Python's ``int()`` reads
    neither, which is where the two backends used to part ways.
    """
    value = text.replace("_", "")
    sign = -1 if value[0] == "-" else 1
    if value[0] in "+-":
        value = value[1:]
    if value == "0":
        return 0
    if value.startswith("0b"):
        return sign * int(value[2:], 2)
    if value.startswith("0x"):
        return sign * int(value[2:], 16)
    if value[0] == "0":
        return sign * int(value, 8)
    if ":" in value:
        digits = [int(part) for part in value.split(":")]
        digits.reverse()
        total, base = 0, 1
        for digit in digits:
            total += digit * base
            base *= 60
        return sign * total
    return sign * int(value)


def _yaml_float(text: str) -> float:
    """``yaml.constructor.SafeConstructor.construct_yaml_float``, reproduced."""
    value = text.replace("_", "").lower()
    sign = -1 if value[0] == "-" else 1
    if value[0] in "+-":
        value = value[1:]
    if value == ".inf":
        return sign * float("inf")
    if value == ".nan":
        return float("nan")
    if ":" in value:
        digits = [float(part) for part in value.split(":")]
        digits.reverse()
        total, base = 0.0, 1
        for digit in digits:
            total += digit * base
            base *= 60
        return sign * total
    return sign * float(value)


def _split_inline(inner: str) -> list[str]:
    """Split a flow sequence's items on the commas outside its quoted scalars.

    A nested flow collection is refused rather than split on the wrong comma:
    ``[[1, 2]]`` is two malformed items to this split and one list to PyYAML.

    A quote opens a quoted scalar only where an item may begin, which is
    ``_scan_line``'s rule one level down: ``[a 'b, c']`` is the two plain
    scalars ``a 'b`` and ``c'`` to PyYAML, not one quoted item, and a quote
    inside a plain item must not hide the ``]`` after it either.
    """
    parts: list[str] = []
    cur = ""
    quote = ""
    at_start = True
    for ch in inner:
        if quote:
            if ch == quote:
                quote = ""
                at_start = False
            cur += ch
            continue
        if ch in "'\"" and at_start:
            quote = ch
            cur += ch
            continue
        if ch in "[]{}":
            raise _unsupported("nested flow collection at", inner)
        if ch == ",":
            parts.append(_yaml_strip(cur))
            cur = ""
            at_start = True
            continue
        if ch not in " \t":
            at_start = False
        cur += ch
    if quote:
        raise _unsupported("unterminated quoted string at", inner)
    if _yaml_strip(cur):
        parts.append(_yaml_strip(cur))
    if not all(parts):
        raise _unsupported("empty item in a flow sequence at", inner)
    return parts
