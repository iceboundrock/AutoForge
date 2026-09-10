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

CONFIG_VERSION = 1

DEFAULT_TIMEOUT_SECONDS = 1800

KNOWN_PROVIDERS = ("claude", "opencode", "scripted")


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


@dataclass
class ExecutionConfig:
    default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    # How many times a *malformed CONTROL_RESULT* (exit 0) triggers a
    # correction prompt before the step fails. 0 disables correction.
    max_correction_attempts: int = 1
    # Deprecated location for the merge gate; mirrored into safety.allow_merge.
    allow_merge: bool = False


# Paths whose contents define what the hosted checks actually run. See
# ``SafetyConfig.protected_merge_paths``.
DEFAULT_PROTECTED_MERGE_PATHS = (".github/workflows/",)


@dataclass
class SafetyConfig:
    # Controller invariant: no real merge unless this is true AND the CLI
    # passes --allow-merge. Default off for this milestone.
    allow_merge: bool = False
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

    def protects(self, path: str) -> bool:
        """Whether ``path`` (a POSIX repo-relative path) is protected."""
        for pattern in self.protected_merge_paths:
            if pattern.endswith("/"):
                if path == pattern[:-1] or path.startswith(pattern):
                    return True
            elif path == pattern or fnmatch.fnmatchcase(path, pattern):
                return True
        return False


@dataclass
class GitHubConfig:
    command: str = "gh"
    timeout_seconds: int = 120


MERGE_METHODS = ("squash", "merge", "rebase")


@dataclass
class MergeConfig:
    """How the *controller* merges once the gate is open (never an agent)."""

    method: str = "squash"  # squash | merge | rebase  -> gh pr merge --<method>
    delete_branch: bool = False  # gh pr merge --delete-branch
    # Inconclusive GitHub data (mergeability UNKNOWN, checks still running,
    # post-merge re-read failed) is re-checked on `resume` at most this many
    # times per phase; then the run is BLOCKED instead of retrying forever.
    max_verification_attempts: int = 5


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


@dataclass
class ReviewConfig:
    replan: ReplanConfig = field(default_factory=ReplanConfig)


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


@dataclass
class AutoForgeConfig:
    version: int = CONFIG_VERSION
    state_dir: str = ".autoforge"
    prompt_version: str = __prompt_version__
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    profiles: dict[str, ProfileConfig] = field(default_factory=dict)

    def profile(self, name: str) -> ProfileConfig:
        try:
            return self.profiles[name]
        except KeyError:
            raise ConfigurationError(f"unknown execution profile {name!r}") from None

    @property
    def merge_allowed_by_config(self) -> bool:
        return bool(self.safety.allow_merge or self.execution.allow_merge)


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
    try:
        if suffix == ".toml":
            import tomllib

            data = tomllib.loads(p.read_text(encoding="utf-8"))
        elif suffix == ".json":
            import json

            data = json.loads(p.read_text(encoding="utf-8"))
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
    """A list of non-empty strings -- never a bare string silently split."""
    if raw is None:
        return []
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


def _merge_config(base: AutoForgeConfig, data: dict, source: str) -> AutoForgeConfig:
    version = data.get("version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ConfigurationError(
            f"{source}: unsupported config version {version!r} (expected {CONFIG_VERSION})"
        )
    if "state_dir" in data:
        base.state_dir = str(data["state_dir"])
    if "prompt_version" in data:
        base.prompt_version = str(data["prompt_version"])
    exe = data.get("execution", {}) or {}
    if not isinstance(exe, dict):
        raise ConfigurationError(f"{source}: 'execution' must be a mapping")
    if "default_timeout_seconds" in exe:
        base.execution.default_timeout_seconds = _as_int(
            exe["default_timeout_seconds"], source, "execution.default_timeout_seconds"
        )
    if "max_correction_attempts" in exe:
        base.execution.max_correction_attempts = _as_int(
            exe["max_correction_attempts"], source, "execution.max_correction_attempts"
        )
    if "allow_merge" in exe:
        base.execution.allow_merge = _as_bool(exe["allow_merge"], source, "execution.allow_merge")
    safety = data.get("safety", {}) or {}
    if not isinstance(safety, dict):
        raise ConfigurationError(f"{source}: 'safety' must be a mapping")
    if "allow_merge" in safety:
        base.safety.allow_merge = _as_bool(safety["allow_merge"], source, "safety.allow_merge")
    if "protected_merge_paths" in safety:
        base.safety.protected_merge_paths = _as_str_list(
            safety["protected_merge_paths"], source, "safety.protected_merge_paths"
        )
    gh = data.get("github", {}) or {}
    if not isinstance(gh, dict):
        raise ConfigurationError(f"{source}: 'github' must be a mapping")
    if "command" in gh:
        base.github.command = str(gh["command"])
    if "timeout_seconds" in gh:
        base.github.timeout_seconds = _as_int(
            gh["timeout_seconds"], source, "github.timeout_seconds"
        )
    merge = data.get("merge", {}) or {}
    if not isinstance(merge, dict):
        raise ConfigurationError(f"{source}: 'merge' must be a mapping")
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
    review = data.get("review", {}) or {}
    if not isinstance(review, dict):
        raise ConfigurationError(f"{source}: 'review' must be a mapping")
    replan = review.get("replan", {}) or {}
    if not isinstance(replan, dict):
        raise ConfigurationError(f"{source}: 'review.replan' must be a mapping")
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
    workflow = data.get("workflow", {}) or {}
    if not isinstance(workflow, dict):
        raise ConfigurationError(f"{source}: 'workflow' must be a mapping")
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
    profiles = data.get("profiles", {}) or {}
    if not isinstance(profiles, dict):
        raise ConfigurationError(f"{source}: 'profiles' must be a mapping")
    for name, p in profiles.items():
        if not isinstance(p, dict):
            raise ConfigurationError(f"{source}: profile {name!r} must be a mapping")
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


def _load_yaml(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except ImportError:
        return _minimal_yaml_parse(text)


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
