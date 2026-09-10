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
# Placeholders are first replaced by this NUL-delimited marker, which no
# template or value can contain in practice, and only then by the real
# value. That keeps substitution strictly single-pass: a value carrying
# "{{...}}" text (a feature specification is untrusted project data and may
# well quote one) is inserted verbatim instead of being re-expanded or
# tripping the "unresolved placeholder" guard below.
_MARKER_RE = re.compile(r"\x00AF:([A-Z][A-Z0-9_]*)\x00")

TEMPLATE_FILES = (
    "common.md",
    "analyze_execute.md",
    "review.md",
    "fix.md",
    "replan_reexecute.md",
    "update_epic.md",
    "correction.md",
    # LOCAL mode: separate templates rather than the GitHub ones fed fake
    # Issue/PR values. Nothing here may mention gh, PRs or merging.
    "local_common.md",
    "local_analyze_execute.md",
    "local_review.md",
    "local_fix.md",
)

# The trusted header each mode prepends to its phase template.
COMMON_TEMPLATE = "common.md"
LOCAL_COMMON_TEMPLATE = "local_common.md"


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
    placeholder the *template* leaves unresolved is an error; placeholder-like
    text inside a substituted value is content, not a placeholder, and is
    inserted verbatim (see ``_MARKER_RE``).
    """
    str_vars: dict[str, str] = {}
    for var in required_variables(template):
        if var not in variables or variables[var] is None:
            raise ConfigurationError(
                f"prompt template requires variable {var!r} but it was not provided"
            )
        str_vars[var] = str(variables[var])

    marked = PLACEHOLDER_RE.sub(lambda m: f"\x00AF:{m.group(1)}\x00", template)
    leftover = PLACEHOLDER_RE.findall(marked)
    if leftover:
        raise ConfigurationError(
            f"prompt rendering left placeholders unresolved: {sorted(set(leftover))}"
        )
    return _MARKER_RE.sub(lambda m: str_vars[m.group(1)], marked)


def render_phase(
    phase_template: str,
    variables: dict[str, str | int | None],
    include_common: bool = True,
    common_template: str = COMMON_TEMPLATE,
) -> str:
    """Render the trusted common header + phase template with one variable set.

    ``common_template`` selects the header: ``common.md`` for REMOTE runs,
    ``local_common.md`` for LOCAL ones.
    """
    parts = []
    if include_common:
        parts.append(render(load_template(common_template), variables))
    parts.append(render(load_template(phase_template), variables))
    return "\n\n---\n\n".join(parts)
