"""Redaction: common token patterns are masked, normal text untouched."""

import pytest

from autoforge.redaction import _PATTERNS, MAX_GROWTH_FACTOR, redact, redact_obj


def test_github_token_assignment():
    assert "***REDACTED***" in redact("export GITHUB_TOKEN=ghp_secretvalue123")
    assert "ghp_secretvalue123" not in redact("GITHUB_TOKEN=ghp_secretvalue123")


def test_gh_token_and_api_keys():
    assert "abc123XYZ" not in redact("GH_TOKEN=abc123XYZ")
    assert "sk-abcdefghij123456" not in redact("OPENAI_API_KEY=sk-abcdefghij123456")
    assert "sk-ant-abcdefghij123" not in redact("ANTHROPIC_API_KEY=sk-ant-abcdefghij123")


def test_bearer_header():
    out = redact("Authorization: Bearer abcdef123456")
    assert "abcdef123456" not in out
    assert "Authorization" in out


def test_github_pat_and_sk_shapes():
    assert redact("token ghp_abcdefghijklmnop") == "token ***REDACTED***"
    assert "sk-proj-abcdef123456" not in redact("key=sk-proj-abcdef123456")


def test_normal_text_untouched():
    text = "review round 3 passed, PR #42 merged cleanly"
    assert redact(text) == text


def test_none_and_non_str_safe():
    assert redact(None) == ""
    assert isinstance(redact(123), str)


def test_redact_obj_keeps_every_entry_when_redacted_keys_collide():
    """Distinct keys that redact to the same text must not overwrite each
    other: the value under the dropped key would vanish from the redacted
    output while the source still holds it. Later collisions are suffixed in
    insertion order, and a key that already reads like the marker takes part
    in the same numbering rather than swallowing an earlier entry."""
    out = redact_obj(
        {
            "GITHUB_TOKEN=firstsecret": 1,
            "GITHUB_TOKEN=***REDACTED***": 2,
            "GITHUB_TOKEN=secondsecret": 3,
            "plain": {"ghp_aaaaaaaaaaaa": "x", "ghp_bbbbbbbbbbbb": "y"},
        }
    )
    assert out == {
        "GITHUB_TOKEN=***REDACTED***": 1,
        "GITHUB_TOKEN=***REDACTED***#2": 2,
        "GITHUB_TOKEN=***REDACTED***#3": 3,
        "plain": {"***REDACTED***": "x", "***REDACTED***#2": "y"},
    }
    for secret in ("firstsecret", "secondsecret", "ghp_aaaaaaaaaaaa", "ghp_bbbbbbbbbbbb"):
        assert secret not in repr(out)


def test_redact_obj_leaves_distinct_keys_unsuffixed():
    """The suffix appears only on a collision; ordinary mappings round-trip."""
    value = {"a": 1, "b": [1, {"c": None}], "d": True}
    assert redact_obj(value) == value


# The shortest text each pattern recognises, with the separator that lets the
# shape repeat, so the filled case below is meaningful. The bare shape without
# the separator has a marginally higher ratio (``HF_TOKEN=x``, 10 to 23, is the
# worst text of all and is pinned by the wrapping test); no text grows past it.
_WORST_CASE_UNITS = {
    "named_assignment": "HF_TOKEN=x;",
    "named_assignment_quoted": 'HF_TOKEN="x"',
    "bearer_header": "Authorization:Token x ",
    "github_pat": "ghp_aaaaaaaa ",
    "sk_key": "sk-aaaaaaaa ",
    # Matched by the ``sk-`` pattern before its own; listed so a narrower
    # ``sk-`` pattern could not leave it unpinned.
    "sk_ant_key": "sk-ant-aaaaaaaa ",
    "generic_assignment": "token=aaaaaaaaaaaa ",
}


@pytest.mark.parametrize("unit", sorted(_WORST_CASE_UNITS), ids=str)
def test_redaction_growth_is_bounded(unit):
    """``redact`` never more than ``MAX_GROWTH_FACTOR``-uples a text.

    A secret shorter than the marker grows under redaction, and the persisted
    review evidence is redacted *after* the parser bounded it, so the factor
    is what lets ``loop_guard.MAX_REQUIRED_RESOLUTION_CHARS`` stand above
    every accepted resolution (#33). A new pattern must keep the factor or
    raise both together.
    """
    text = _WORST_CASE_UNITS[unit]
    single = redact(text)
    assert "***REDACTED***" in single  # the unit really is recognised
    assert len(single) <= MAX_GROWTH_FACTOR * len(text)
    filled = text * (2000 // len(text) + 1)
    assert len(redact(filled)) <= MAX_GROWTH_FACTOR * len(filled)


def test_every_redaction_pattern_has_a_growth_pin():
    """A pattern with no matrix unit would escape the growth bound unnoticed.

    The units are representative shapes, not a property test, so the guard
    against a new pattern is that it cannot be added without a unit that it
    matches on its own. ``sk-ant-`` is matched through ``redact`` by the
    ``sk-`` pattern first, so each pattern is checked directly, not via the
    marker ``redact`` leaves.
    """
    units = list(_WORST_CASE_UNITS.values())
    unpinned = [
        pattern.pattern
        for pattern, _ in _PATTERNS
        if not any(pattern.search(unit) for unit in units)
    ]
    assert unpinned == []


def test_redaction_growth_does_not_compound_across_patterns():
    """A marker one pattern wrote is never a shorter run a later one grows."""
    text = "Authorization: Bearer HF_TOKEN=x GH_TOKEN=ghp_aaaaaaaa token=sk-aaaaaaaaaaaa"
    out = redact(text)
    assert "ghp_aaaaaaaa" not in out and "sk-aaaaaaaaaaaa" not in out
    assert len(out) <= MAX_GROWTH_FACTOR * len(text)


def test_redaction_marker_wrapped_by_a_later_pattern_never_grows():
    """The bearer value class admits ``*``, so it can match a marker the
    assignment pattern wrote; that match holds the whole marker, so it is at
    least as long as its replacement and the second pass cannot grow it."""
    grown = redact("HF_TOKEN=x")  # 10 -> 23, the first pass grew it
    assert grown == "HF_TOKEN=***REDACTED***"
    text = "Authorization: Bearer HF_TOKEN=x"
    out = redact(text)
    assert out == "Authorization: Bearer ***REDACTED***"
    assert len(out) < len("Authorization: Bearer ") + len(grown)
