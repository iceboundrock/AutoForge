"""Secret redaction for anything that lands in logs.

Baseline protection only — no detector claims to catch every secret shape.
Covers common env-var assignments and bearer-style tokens before stdout /
stderr / commands are persisted by the run logger.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # VAR=secret / VAR: secret assignments for well-known names.
    (
        re.compile(
            r"(?i)\b(GITHUB_TOKEN|GH_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|"
            r"OPENCODE_API_KEY|GITLAB_TOKEN|HF_TOKEN|SLACK_TOKEN|AWS_SECRET_ACCESS_KEY)"
            r"(\s*[:=]\s*)([\"']?)([^\s\"';]+)([\"']?)"
        ),
        r"\1\2\3***REDACTED***\5",
    ),
    # Authorization: Bearer <token> / Token <token>
    (
        re.compile(r"(?i)\b(Authorization\s*:\s*(?:Bearer|Token)\s+)([^\s\"';]+)"),
        r"\1***REDACTED***",
    ),
    # GitHub PATs
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}"), "***REDACTED***"),
    # OpenAI-style sk- keys (incl. project variant sk-proj-...)
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{8,}"), "***REDACTED***"),
    # Anthropic keys
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), "***REDACTED***"),
    # Generic long bearer-ish assignments: token/password/secret = <value>
    (
        re.compile(
            r"(?i)\b(token|passwd|password|secret|api[_-]?key)(\s*[:=]\s*)([\"']?)"
            r"([A-Za-z0-9_\-./+]{12,})([\"']?)"
        ),
        r"\1\2\3***REDACTED***\5",
    ),
]

_REDACTED = "***REDACTED***"

# :func:`redact` never returns more than this many times its input's length.
# Every pattern replaces a run of at least one character with the 14-character
# marker, so a text can *grow* under redaction; the worst shape is a one
# character secret behind the shortest recognised name: ``HF_TOKEN=x`` is 10
# characters and becomes 23, a factor of 2.3, and repeating it needs a
# separator (``HF_TOKEN=x;``, 11 to 24), so no longer text reaches that
# ratio. Growth does not compound
# across patterns: a later pattern can only lengthen the text by matching a
# run *shorter* than the marker, and a marker is never part of such a run.
# The value classes that admit ``*`` (the named-assignment and bearer values)
# can only take a marker in whole, so a match containing one is already at
# least as long as its replacement and shrinks or keeps the length; every
# other value class excludes ``*``, so a marker is never part of those
# matches at all. The characters around a replaced run are untouched, so no
# new short match appears beside it. ``tests/test_redaction.py`` pins the
# factor against the worst-case shape of every pattern and the wrapping case;
# a new pattern must keep it, or raise it together with the persisted bound
# in :mod:`autoforge.loop_guard` that depends on it.
MAX_GROWTH_FACTOR = 3


def redact(text: str | None) -> str:
    """Redact known secret shapes. Never raises; non-str input -> str()."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_argv(argv: list[str]) -> list[str]:
    return [redact(a) for a in argv]


def redact_obj(value: object) -> object:
    """Recursively redact every string inside a JSON-shaped structure.

    Log metadata and agent-supplied CONTROL_RESULT fields are persisted as
    nested dicts/lists, so redacting only the top-level text would leave a
    secret sitting one level down (``metadata["validation_command"]``, a
    finding's ``required_resolution``). Mapping *keys* are redacted too: a key
    is as capable of carrying a token as a value. Non-string scalars are
    returned unchanged; anything exotic is stringified through `redact`, which
    never raises.

    A mapping never loses an entry to redaction. Two distinct keys can
    redact to the same text (``GITHUB_TOKEN=a`` and ``GITHUB_TOKEN=b`` both
    become ``GITHUB_TOKEN=***REDACTED***``), and a hand-edited journal or a
    free-form ``escalation`` mapping can carry such keys; letting the later
    entry overwrite the earlier one would drop a value from the diagnostic
    output while the file on disk still holds it. A colliding key is instead
    suffixed ``#2``, ``#3``, ... in insertion order, so every value is kept,
    every key stays redacted, and the collision is visible in the output.
    """
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        result: dict = {}
        for k, v in value.items():
            key = redact(str(k))
            if key in result:
                n = 2
                while f"{key}#{n}" in result:
                    n += 1
                key = f"{key}#{n}"
            result[key] = redact_obj(v)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return redact(str(value))


def redact_dict(value: dict) -> dict:
    """`redact_obj` for a mapping, typed for callers that persist dicts."""
    result = redact_obj(value)
    return result if isinstance(result, dict) else {}
