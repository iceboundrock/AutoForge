"""Redaction: common token patterns are masked, normal text untouched."""

import time

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


_FINE_GRAINED_PAT = (
    "github_pat_11ABCDEFG0abcdefghijklmn_"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)


def test_github_fine_grained_pat():
    """``github_pat_...`` is not a ``gh?_`` shape and needs its own pattern."""
    assert redact(f"cloning with {_FINE_GRAINED_PAT} done") == "cloning with ***REDACTED*** done"
    assert _FINE_GRAINED_PAT[11:] not in redact(f"GH_TOKEN={_FINE_GRAINED_PAT}")
    assert _FINE_GRAINED_PAT[11:] not in redact(f"https://{_FINE_GRAINED_PAT}@github.com/o/r")


def test_basic_auth_header():
    out = redact("Authorization: Basic dXNlcjpwYXNzd29yZA==")
    assert out == "Authorization: Basic ***REDACTED***"
    assert redact("authorization:basic dXNlcjpwYXNz") == "authorization:basic ***REDACTED***"


_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


@pytest.mark.parametrize(
    "text",
    [
        f"Bearer {_JWT}",
        f"Cookie: session={_JWT}; Path=/",
        f'{{"id_token": "{_JWT}"}}',
        f"gh api -H 'Authorization: Bearer {_JWT}' /user",
    ],
    ids=["bare", "cookie", "json", "argv"],
)
def test_jwt_is_redacted_whole(text):
    """Every segment goes, not only the signature: header and payload carry
    claims (subject, email, scopes) that are as sensitive as the signature is
    reusable."""
    out = redact(text)
    for segment in _JWT.split("."):
        assert segment not in out
    assert "***REDACTED***" in out
    assert "Path=/" in out or "Path=/" not in text


def test_jwt_needs_three_segments():
    """Two base64url runs around one dot are a version string or a file name
    as often as a token; only the three-segment shape is a JWT."""
    text = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    assert redact(text) == text


@pytest.mark.parametrize(
    "token",
    [
        # ``{}`` payload: ``e30`` is three characters.
        "eyJhbGciOiJIUzI1NiJ9.e30.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        # Unsecured ``alg: none`` token (RFC 7519 §6): empty signature.
        "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxMjM0NTY3ODkwIn0.",
        # Detached content (RFC 7515 App. F): empty payload.
        "eyJhbGciOiJIUzI1NiJ9..SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        # The smallest header the shape admits, ``{"a":0}``.
        "eyJhIjowfQ.e30.c2ln",
    ],
    ids=["short-payload", "empty-signature", "empty-payload", "shortest-header"],
)
def test_jwt_short_and_empty_segments_are_redacted(token):
    """Only the header has a length floor. A payload is as short as ``e30``
    and, for detached content, empty; an unsecured token has no signature.
    Those are valid compact serializations and carry the same claims."""
    for text in (f"Bearer {token}", f"id_token={token}&state=1"):
        out = redact(text)
        assert "***REDACTED***" in out
        for segment in token.split("."):
            assert not segment or segment not in out
        assert "state=1" in out or "state=1" not in text


def test_jwt_header_shorter_than_a_json_object_is_not_a_token():
    """``eyJ`` decodes to ``{"`` plus a key character, so a real header is at
    least the ten characters of ``{"a":0}``; a shorter first run is base64
    that happens to start ``eyJ``, not a JWT, whatever follows its dots."""
    text = "eyJhIjow.e30.c2ln"
    assert redact(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "git remote add origin https://x-access-token:ghs_abcdefghijklmnop@github.com/o/r.git",
            "git remote add origin https://***REDACTED***@github.com/o/r.git",
        ),
        (
            "https://oauth2:glpat-abcdef123456@gitlab.example.com/o/r.git",
            "https://***REDACTED***@gitlab.example.com/o/r.git",
        ),
        (
            "postgresql://app:s3cr3t@db.internal:5432/app",
            "postgresql://***REDACTED***@db.internal:5432/app",
        ),
        ("HTTPS://TOKEN@GITHUB.COM/O/R", "HTTPS://***REDACTED***@GITHUB.COM/O/R"),
        (
            "fatal: could not read from 'https://u:p@h/r'",
            "fatal: could not read from 'https://***REDACTED***@h/r'",
        ),
        (
            "https://u:p@h?to=a@b#c@d",
            "https://***REDACTED***@h?to=a@b#c@d",
        ),
    ],
    ids=["x-access-token", "oauth2", "dsn", "bare-userinfo", "quoted", "query-after"],
)
def test_url_credentials(text, expected):
    """Everything between ``scheme://`` and ``@`` is the credential; the
    host and path after it stay so the line still says which remote."""
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/o/r/pull/3",
        "see https://example.com and mail admin@example.com",
        "git@github.com:o/r.git",
        "https://host/path?to=a@b",
        # ``?`` and ``#`` end the authority as ``/`` does (RFC 3986 §3.2):
        # an ``@`` in the query or fragment is not a credential delimiter.
        "https://host?to=a@b",
        "https://host:8443?u=a@b&v=c@d",
        "https://host#frag@x",
        "https://host?q=1#frag@x",
    ],
    ids=["path", "mail", "scp", "path-query", "query", "port-query", "fragment", "query-fragment"],
)
def test_url_without_userinfo_untouched(text):
    assert redact(text) == text


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


def test_redact_obj_steps_over_a_suffix_the_input_already_carries():
    """A key that already reads ``text#n`` is not overwritten by the n-th
    collision and does not restart the numbering: the allocator skips the
    taken suffix once and keeps counting up from there. The reverse order,
    where the literal ``#2`` arrives after the suffix was handed out,
    collides on that text and is suffixed in turn, so no entry is lost."""
    out = redact_obj(
        {
            "ghp_aaaaaaaaaaaa": 1,
            "***REDACTED***#2": 2,
            "ghp_bbbbbbbbbbbb": 3,
            "ghp_cccccccccccc": 4,
        }
    )
    assert out == {
        "***REDACTED***": 1,
        "***REDACTED***#2": 2,
        "***REDACTED***#3": 3,
        "***REDACTED***#4": 4,
    }
    out = redact_obj({"ghp_aaaaaaaaaaaa": 1, "ghp_bbbbbbbbbbbb": 2, "***REDACTED***#2": 3})
    assert out == {"***REDACTED***": 1, "***REDACTED***#2": 2, "***REDACTED***#2#2": 3}


def test_redact_obj_suffix_allocation_is_linear_in_the_colliding_keys():
    """A journal's ``escalation`` mapping is free-form and the state loader
    admits files of tens of megabytes, so `status --json` must not turn a
    mapping whose keys all redact to one text into a quadratic scan of the
    taken suffixes. Rescanning from ``#2`` per entry took seconds at 10,000
    keys; a per-text counter keeps the whole pass well under the budget."""
    count = 20_000
    value = {f"GITHUB_TOKEN=secret{i}": i for i in range(count)}
    started = time.perf_counter()
    out = redact_obj(value)
    assert time.perf_counter() - started < 3.0
    assert isinstance(out, dict) and len(out) == count
    assert list(out.values()) == list(range(count))
    assert all(key.startswith("GITHUB_TOKEN=***REDACTED***") for key in out)
    assert "secret" not in "".join(out)


# The shortest text each pattern recognises, with the separator that lets the
# shape repeat, so the filled case below is meaningful. The bare shape without
# the separator has a marginally higher ratio (``HF_TOKEN=x``, 10 to 23, is
# pinned by the wrapping test); the URL shape needs no separator (``@`` ends a
# word) and is the worst text of all, 7 to 20; no text grows past it.
_WORST_CASE_UNITS = {
    "named_assignment": "HF_TOKEN=x;",
    "named_assignment_quoted": 'HF_TOKEN="x"',
    "bearer_header": "Authorization:Token x ",
    "basic_header": "Authorization:Basic x ",
    "url_credential": "ab://x@",
    # Header at its ten-character floor, empty payload and signature; ``.``
    # ends the shape, so it repeats without a separator.
    "jwt": "eyJaaaaaaa..",
    "github_fine_grained_pat": "github_pat_aaaaaaaa ",
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
    """The header and URL-userinfo value classes admit ``*``, so they can
    match a marker the assignment pattern wrote; that match holds the whole
    marker, so it is at least as long as its replacement and the second pass
    cannot grow it."""
    grown = redact("HF_TOKEN=x")  # 10 -> 23, the first pass grew it
    assert grown == "HF_TOKEN=***REDACTED***"
    text = "Authorization: Bearer HF_TOKEN=x"
    out = redact(text)
    assert out == "Authorization: Bearer ***REDACTED***"
    assert len(out) < len("Authorization: Bearer ") + len(grown)
    grown = redact('HF_TOKEN="x"')  # 12 -> 25; the quote ends the value before ``@``
    assert grown == 'HF_TOKEN="***REDACTED***"'
    text = 'https://HF_TOKEN="x"@h'
    out = redact(text)
    assert out == "https://***REDACTED***@h"
    assert len(out) < len("https://") + len(grown) + len("@h")
