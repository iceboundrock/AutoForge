"""Redaction: common token patterns are masked, normal text untouched."""

import time

import pytest

from autoforge.redaction import (
    _PATTERNS,
    MAX_GROWTH_FACTOR,
    credential_classes,
    redact,
    redact_obj,
)


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


_JWT_TEXTS = [
    f"Bearer {_JWT}",
    f"Cookie: session={_JWT}; Path=/",
    f'{{"id_token": "{_JWT}"}}',
    f"gh api -H 'Authorization: Bearer {_JWT}' /user",
]


@pytest.mark.parametrize("text", _JWT_TEXTS, ids=["bare", "cookie", "json", "argv"])
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


_SHORT_JWTS = [
    # ``{}`` payload: ``e30`` is three characters.
    "eyJhbGciOiJIUzI1NiJ9.e30.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    # Unsecured ``alg: none`` token (RFC 7519 §6): empty signature.
    "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxMjM0NTY3ODkwIn0.",
    # Detached content (RFC 7515 App. F): empty payload.
    "eyJhbGciOiJIUzI1NiJ9..SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    # The smallest header the shape admits, ``{"a":0}``.
    "eyJhIjowfQ.e30.c2ln",
]


@pytest.mark.parametrize(
    "token",
    _SHORT_JWTS,
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


_URL_CREDENTIAL_CASES = [
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
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _URL_CREDENTIAL_CASES,
    ids=["x-access-token", "oauth2", "dsn", "bare-userinfo", "quoted", "query-after"],
)
def test_url_credentials(text, expected):
    """Everything between ``scheme://`` and ``@`` is the credential; the
    host and path after it stay so the line still says which remote."""
    assert redact(text) == expected


_URLS_WITHOUT_USERINFO = [
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
]


@pytest.mark.parametrize(
    "text",
    _URLS_WITHOUT_USERINFO,
    ids=["path", "mail", "scp", "path-query", "query", "port-query", "fragment", "query-fragment"],
)
def test_url_without_userinfo_untouched(text):
    assert redact(text) == text


_NORMAL_TEXT = "review round 3 passed, PR #42 merged cleanly"


def test_normal_text_untouched():
    assert redact(_NORMAL_TEXT) == _NORMAL_TEXT


# Every value below is an obviously fake placeholder.
_OAUTH_FIELD_CASES = [
    (
        '{"refresh": "rt_FAKE-not-a-real-token", "access": "at_FAKE-not-real", '
        '"expires": 1700000000}',
        '{"refresh": "***REDACTED***", "access": "***REDACTED***", "expires": 1700000000}',
    ),
    ('{"refresh_token": "FAKE-refresh"}', '{"refresh_token": "***REDACTED***"}'),
    ("{'access_token': 'FAKE-access'}", "{'access_token': '***REDACTED***'}"),
    ('"id_token": "FAKE.ID.TOKEN"', '"id_token": "***REDACTED***"'),
    ('"chatgpt_account_id":"fake-account"', '"chatgpt_account_id":"***REDACTED***"'),
    # JSON inside a JSON string keeps its escaping.
    ('{\\"refresh\\": \\"rt_FAKE\\"}', '{\\"refresh\\": \\"***REDACTED***\\"}'),
    ("refresh_token=FAKE_REFRESH_TOKEN", "refresh_token=***REDACTED***"),
    ("?oauth_refresh_token=FAKE&next=1", "?oauth_refresh_token=***REDACTED***&next=1"),
    ("ACCESS_TOKEN: 'FAKE-access'", "ACCESS_TOKEN: '***REDACTED***'"),
    ("chatgpt-account-id: 00000000-fake-0000", "chatgpt-account-id: ***REDACTED***"),
    ("access=FAKE_ACCESS_VALUE_0000", "access=***REDACTED***"),
]


@pytest.mark.parametrize(("text", "expected"), _OAUTH_FIELD_CASES)
def test_oauth_credential_fields(text, expected):
    assert redact(text) == expected


_OAUTH_PROSE = [
    "refresh the page and check access to the repo",
    "access: denied; refresh: on demand",
    "refresh=true access=read",
    "the token refresh failed; see the access log",
    '{"refreshed": "yes", "accessible": "no"}',
]


@pytest.mark.parametrize("text", _OAUTH_PROSE)
def test_oauth_words_in_prose_untouched(text):
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
    "oauth_json_field": '"access":"x"',
    "oauth_assignment": "id_token=x;",
    "oauth_bare_assignment": "access=aaaaaaaaaaaaaaaa ",
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
    unpinned = [entry.name for entry in _PATTERNS if not any(entry.regex.search(u) for u in units)]
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


def test_every_redaction_pattern_has_its_stable_class_name():
    """The names are part of the contract: a content-policy refusal names
    the class, so a rename changes what an agent is told to remove."""
    assert [entry.name for entry in _PATTERNS] == [
        "env-assignment",
        "authorization-header",
        "url-userinfo",
        "jwt",
        "github-fine-grained-pat",
        "github-token",
        "openai-key",
        "anthropic-key",
        "secret-assignment",
        "oauth-field",
        "oauth-assignment",
        "oauth-bare-assignment",
    ]


# Every secret-bearing and every clean text the tests above use, plus each
# worst-case unit alone and repeated.
_CREDENTIAL_CORPUS = [
    "export GITHUB_TOKEN=ghp_secretvalue123",
    "GH_TOKEN=abc123XYZ",
    "OPENAI_API_KEY=sk-abcdefghij123456",
    "ANTHROPIC_API_KEY=sk-ant-abcdefghij123",
    "Authorization: Bearer abcdef123456",
    "Authorization: Basic dXNlcjpwYXNzd29yZA==",
    "authorization:basic dXNlcjpwYXNz",
    "token ghp_abcdefghijklmnop",
    "key=sk-proj-abcdef123456",
    f"cloning with {_FINE_GRAINED_PAT} done",
    _JWT,
    *_JWT_TEXTS,
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
    *_SHORT_JWTS,
    "eyJhIjow.e30.c2ln",
    *(text for text, _ in _URL_CREDENTIAL_CASES),
    *_URLS_WITHOUT_USERINFO,
    _NORMAL_TEXT,
    *(text for text, _ in _OAUTH_FIELD_CASES),
    *_OAUTH_PROSE,
    *_WORST_CASE_UNITS.values(),
    *(unit * 50 for unit in _WORST_CASE_UNITS.values()),
    "Authorization: Bearer HF_TOKEN=x GH_TOKEN=ghp_aaaaaaaa token=sk-aaaaaaaaaaaa",
    'https://HF_TOKEN="x"@h',
    # Already redacted text is clean: no pattern changes it again.
    "HF_TOKEN=***REDACTED***",
    "Authorization: Bearer ***REDACTED***",
    "https://***REDACTED***@github.com/o/r.git",
    "",
]


@pytest.mark.parametrize("text", _CREDENTIAL_CORPUS)
def test_credential_classes_is_empty_exactly_when_redact_changes_nothing(text):
    classes = credential_classes(text)
    assert (classes == ()) == (redact(text) == text)


def test_credential_classes_corpus_has_both_sides():
    """The equivalence above is meaningful only if the corpus holds both."""
    flagged = [text for text in _CREDENTIAL_CORPUS if credential_classes(text)]
    assert flagged and len(flagged) < len(_CREDENTIAL_CORPUS)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GH_TOKEN=abc123XYZ", ("env-assignment",)),
        ("Authorization: Bearer abcdef123456", ("authorization-header",)),
        ("postgresql://app:s3cr3t@db.internal:5432/app", ("url-userinfo",)),
        (_JWT, ("jwt",)),
        (_FINE_GRAINED_PAT, ("github-fine-grained-pat",)),
        ("ghp_abcdefghijklmnop", ("github-token",)),
        ("sk-proj-abcdef123456", ("openai-key",)),
        # ``sk-ant-`` keys are replaced by the earlier ``sk-`` pattern.
        ("sk-ant-abcdefghij123", ("openai-key",)),
        ("password=hunter2hunter2", ("secret-assignment",)),
        ('{"access_token": "FAKE-access"}', ("oauth-field",)),
        ("refresh_token=FAKE_REFRESH_TOKEN", ("oauth-assignment",)),
        ("access=FAKE_ACCESS_VALUE_0000", ("oauth-bare-assignment",)),
    ],
    ids=str,
)
def test_credential_classes_names_the_pattern(text, expected):
    assert credential_classes(text) == expected


def test_credential_classes_are_in_pattern_order_each_once():
    """Order is the pattern order, not the order in the text, and a class
    found many times is named once."""
    text = (
        "access=FAKE_ACCESS_VALUE_0000 ghp_abcdefghijklmnop "
        "GH_TOKEN=abc123XYZ ghp_zyxwvutsrqponmlk GITHUB_TOKEN=other "
        "access=FAKE_ACCESS_VALUE_1111"
    )
    assert credential_classes(text) == (
        "env-assignment",
        "github-token",
        "oauth-bare-assignment",
    )


def test_credential_classes_records_a_pattern_that_rewrites_a_marker():
    """A later pattern that replaces an earlier pattern's marker (and the
    text around it) changed the text too, so it is named as well."""
    assert credential_classes("Authorization: Bearer HF_TOKEN=x") == (
        "env-assignment",
        "authorization-header",
    )
