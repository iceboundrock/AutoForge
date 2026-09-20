"""Secret redaction for anything that lands in logs.

Baseline protection only — no detector claims to catch every secret shape.
Covers common env-var assignments, ``Authorization`` headers, well-known
token shapes (GitHub PATs, OpenAI / Anthropic keys, JWTs) and credentials
embedded in URLs before stdout / stderr / commands are persisted by the run
logger.
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
    # Authorization: Bearer <token> / Token <token> / Basic <base64>
    (
        re.compile(r"(?i)\b(Authorization\s*:\s*(?:Bearer|Token|Basic)\s+)([^\s\"';]+)"),
        r"\1***REDACTED***",
    ),
    # Credentials embedded in a URL: everything between ``scheme://`` and
    # ``@`` (``https://x-access-token:<token>@github.com/...``,
    # ``https://<token>@...``, ``postgresql://user:password@...``). The whole
    # userinfo goes, username included: a bare userinfo is as often a token
    # as a name, and the host and path that follow keep the line readable.
    # The scheme is bounded and the userinfo class excludes ``/``, so a
    # candidate never scans past the ``://`` of the next one: the pass stays
    # linear over the 16 MiB an executor capture can be.
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,31}://)[^\s/@]+@"),
        r"\1***REDACTED***@",
    ),
    # JWTs: three base64url segments, the first a ``{"`` JSON header. A
    # lookbehind rather than ``\b`` so a ``-eyJ`` inside a segment is not a
    # fresh candidate that rescans the run (base64url admits ``-``); each
    # run is then scanned by at most three candidates and the pass stays
    # linear. Placed before the shorter token shapes so one of them cannot
    # replace a slice of the token and leave the rest unrecognised.
    (
        re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        "***REDACTED***",
    ),
    # GitHub fine-grained PATs
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}"), "***REDACTED***"),
    # GitHub classic PATs and app / OAuth tokens
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
# character credential in the shortest URL that carries one: ``ab://x@`` is 7
# characters and becomes 20, a factor of 2.86, and it repeats without a
# separator (``@`` ends a word), so a text of that shape and no other reaches
# the ratio and no text exceeds it. The next worst is a one character secret
# behind the shortest recognised name: ``HF_TOKEN=x``, 10 to 23. Growth does
# not compound across patterns: a later pattern can only lengthen the text by
# matching a run *shorter* than the marker, and a marker is never part of
# such a run. The value classes that admit ``*`` (the named-assignment,
# header and URL-userinfo values) can only take a marker in whole, since none
# of the characters that end such a run occurs in the marker, so a match
# containing one is already at least as long as its replacement and shrinks
# or keeps the length; every other value class excludes ``*``, so a marker is
# never part of those matches at all. The characters around a replaced run
# are untouched, so no new short match appears beside it.
# ``tests/test_redaction.py`` pins the factor against the worst-case shape of
# every pattern and the wrapping case; a new pattern must keep it, or raise
# it together with the persisted bound in :mod:`autoforge.loop_guard` that
# depends on it.
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
    The next suffix is remembered per colliding text, so a mapping whose keys
    all collide (an ``escalation`` mapping is free-form and the state file can
    be tens of megabytes) is redacted in time linear in its size rather than
    rescanning the taken suffixes from ``#2`` for every entry.
    """
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        result: dict = {}
        # Suffixes are handed out in increasing order per colliding text, so
        # each taken suffix is stepped over at most once: an input key that
        # already reads ``text#n`` is skipped, never rescanned.
        next_suffix: dict[str, int] = {}
        for k, v in value.items():
            key = redact(str(k))
            if key in result:
                n = next_suffix.get(key, 2)
                while f"{key}#{n}" in result:
                    n += 1
                next_suffix[key] = n + 1
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
