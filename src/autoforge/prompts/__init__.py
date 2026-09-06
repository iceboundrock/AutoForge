"""Prompt renderer: file-based templates with strict variable binding.

Templates live in ``autoforge/prompts/*.md`` and use ``{{VAR}}`` placeholders.
Rendering fails loudly when a required variable is missing — never silently
substitutes an empty string — and fails if any placeholder is left
unrendered. Retrieved project text (issues/PRs/code/logs) is referenced only
as untrusted data (see common.md); it is never interpolated into policy.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import ConfigurationError

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Z][A-Z0-9_]*)\s*\}\}")

TEMPLATE_FILES = (
    "common.md",
    "analyze_execute.md",
    "review.md",
    "fix.md",
    "merge.md",
    "update_epic.md",
    "correction.md",
)


def prompts_dir() -> Path:
    return Path(__file__).parent


def load_template(name: str) -> str:
    path = prompts_dir() / name
    if not path.exists():
        raise ConfigurationError(f"prompt template missing: {path}")
    return path.read_text(encoding="utf-8")


def required_variables(template: str) -> set[str]:
    return set(PLACEHOLDER_RE.findall(template))


def render(template: str, variables: dict[str, str | int | None]) -> str:
    """Substitute all {{VARS}}; raise if any required var is missing/empty.

    ``None`` values count as missing. Integers are stringified. Any
    placeholder left over after substitution is an error.
    """
    str_vars: dict[str, str] = {}
    for var in required_variables(template):
        if var not in variables or variables[var] is None:
            raise ConfigurationError(
                f"prompt template requires variable {var!r} but it was not provided"
            )
        str_vars[var] = str(variables[var])

    def _sub(match: re.Match[str]) -> str:
        return str_vars[match.group(1)]

    rendered = PLACEHOLDER_RE.sub(_sub, template)
    leftover = PLACEHOLDER_RE.findall(rendered)
    if leftover:
        raise ConfigurationError(
            f"prompt rendering left placeholders unresolved: {sorted(set(leftover))}"
        )
    return rendered


def render_phase(
    phase_template: str,
    variables: dict[str, str | int | None],
    include_common: bool = True,
) -> str:
    """Render ``common.md`` header + phase template with the same variables."""
    parts = []
    if include_common:
        parts.append(render(load_template("common.md"), variables))
    parts.append(render(load_template(phase_template), variables))
    return "\n\n---\n\n".join(parts)
