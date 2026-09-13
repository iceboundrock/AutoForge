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
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from . import __prompt_version__
from .errors import ConfigurationError
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

EXECUTION_KEYS = ("default_timeout_seconds", "max_correction_attempts")


@dataclass
class ExecutionConfig:
    default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    # How many times a *malformed CONTROL_RESULT* (exit 0) triggers a
    # correction prompt before the step fails. 0 disables correction.
    max_correction_attempts: int = 1


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
    for key, minimum in (("max_review_rounds", 1), ("max_total_steps", 1)):
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


def _minimal_yaml_parse(text: str) -> dict:
    """Minimal YAML-subset parser for our config shape.

    Supports: nested maps via 2-space indentation, lists via "- " items,
    inline scalars (int/float/bool/null/quoted strings). Anything fancier
    raises ConfigurationError telling the user to install PyYAML.
    """
    return _parse_yaml_subset(text)


def _parse_yaml_subset(text: str) -> dict:
    """Small recursive indentation-based parser for the documented subset."""
    lines = [
        (len(line) - len(line.lstrip(" ")), line.strip())
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # strip inline comments
    cleaned: list[tuple[int, str]] = []
    for indent, content in lines:
        # remove trailing comment
        out: list[str] = []
        in_s = in_d = False
        i = 0
        while i < len(content):
            ch = content[i]
            if ch == "'" and not in_d:
                in_s = not in_s
            elif ch == '"' and not in_s:
                in_d = not in_d
            elif ch == "#" and not in_s and not in_d and i > 0 and content[i - 1] == " ":
                break
            out.append(ch)
            i += 1
        cleaned.append((indent, "".join(out).rstrip()))
    pos = 0

    def parse_block(min_indent: int) -> object:
        nonlocal pos
        # decide dict vs list by first line
        if pos >= len(cleaned) or cleaned[pos][0] < min_indent:
            return {}
        if cleaned[pos][1].startswith("- ") or cleaned[pos][1] == "-":
            items: list[object] = []
            while (
                pos < len(cleaned)
                and cleaned[pos][0] >= min_indent
                and (cleaned[pos][1].startswith("- ") or cleaned[pos][1] == "-")
            ):
                ind, content = cleaned[pos]
                item_text = content[1:].strip()
                pos += 1
                if item_text == "":
                    items.append(parse_block(ind + 1))
                else:
                    items.append(_scalar(item_text))
            return items
        mapping: dict[str, object] = {}
        while pos < len(cleaned) and cleaned[pos][0] >= min_indent:
            ind, content = cleaned[pos]
            if ind != min_indent:
                # Over-indented stray line (mapping keys must align).
                raise ConfigurationError(
                    f"YAML subset parser: bad indentation at: {content!r} — "
                    "install PyYAML for full YAML support"
                )
            if content.startswith("-"):
                break
            if ":" not in content:
                raise ConfigurationError(
                    f"YAML subset parser: cannot parse line: {content!r} — "
                    "install PyYAML for full YAML support"
                )
            key, _, rest = content.partition(":")
            key = key.strip().strip('"').strip("'")
            if key in mapping:
                raise ValueError(f"duplicate key {key!r} at: {content!r}")
            rest = rest.strip()
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

    result = parse_block(0)
    if not isinstance(result, dict):
        raise ConfigurationError("config must contain a mapping at top level")
    return result


def _scalar(text: str) -> object:
    t = text.strip()
    if t in ("", "~", "null", "Null", "NULL"):
        return None
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        return t[1:-1]
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in _split_inline(inner)]
    low = t.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _split_inline(inner: str) -> list[str]:
    parts, cur = [], ""
    in_s = in_d = False
    for ch in inner:
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        if ch == "," and not in_s and not in_d:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts
