"""Redaction: common token patterns are masked, normal text untouched."""

import pytest

from autoforge.redaction import MAX_GROWTH_FACTOR, redact


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


# The shortest text each pattern recognises, with the separator that lets the
# shape repeat: the most a pattern can grow a text is this unit's ratio.
_WORST_CASE_UNITS = {
    "named_assignment": "HF_TOKEN=x;",
    "named_assignment_quoted": 'HF_TOKEN="x"',
    "bearer_header": "Authorization:Token x ",
    "github_pat": "ghp_aaaaaaaa ",
    "sk_key": "sk-aaaaaaaa ",
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


def test_redaction_growth_does_not_compound_across_patterns():
    """A marker one pattern wrote is never a shorter run a later one grows."""
    text = "Authorization: Bearer HF_TOKEN=x GH_TOKEN=ghp_aaaaaaaa token=sk-aaaaaaaaaaaa"
    out = redact(text)
    assert "ghp_aaaaaaaa" not in out and "sk-aaaaaaaaaaaa" not in out
    assert len(out) <= MAX_GROWTH_FACTOR * len(text)
