"""Redaction: common token patterns are masked, normal text untouched."""

import json
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


def test_redact_obj_redacts_each_string_never_the_serialized_document():
    """#206: a pattern run over a document's JSON text consumes the syntax
    around a value (an empty assignment takes the closing quote, a value
    before an escaped quote its backslash) and misses a secret right after an
    escape, which follows a letter or a digit there. Each string is redacted
    on its own."""
    token = "ghp_" + "e" * 36
    value = {
        "GH_TOKEN=": ["GITHUB_TOKEN=", "id_token:", 'GITHUB_TOKEN=abc"def'],
        "after_escape": f"line\n{token}",
        "after_non_ascii": f"—{token}",
    }
    assert redact_obj(value) == {
        "GH_TOKEN=": ["GITHUB_TOKEN=", "id_token:", 'GITHUB_TOKEN=***REDACTED***"def'],
        "after_escape": "line\n***REDACTED***",
        "after_non_ascii": "—***REDACTED***",
    }


_R = "***REDACTED***"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            {"id_token": "FAKE.ID.TOKEN", "chatgpt-account-id": "fake-account", "expires": 3600},
            {"id_token": _R, "chatgpt-account-id": _R, "expires": 3600},
        ),
        # In any case, at any depth, and the whole value: unlike the text
        # pattern, the key says where the value ends.
        (
            {"auth": {"Access_Token": "FAKE access"}, "tokens": [{"REFRESH": "rt_FAKE"}]},
            {"auth": {"Access_Token": _R}, "tokens": [{"REFRESH": _R}]},
        ),
    ],
)
def test_redact_obj_redacts_a_value_under_an_oauth_credential_key(document, expected):
    """#206: ``"refresh": "..."`` spans a key and its value, so `redact_obj`,
    which redacts them apart, recognises it by the key: a non-empty string
    under a key that names an OAuth credential field is replaced whole."""
    assert redact_obj(document) == expected


def _json_object(text):
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


@pytest.mark.parametrize(
    ("text", "expected"), [case for case in _OAUTH_FIELD_CASES if _json_object(case[0])]
)
def test_redact_obj_covers_every_oauth_field_document_redact_covers(text, expected):
    """#206: the run log redacted a parsed result as its JSON text until the
    text's syntax broke; it redacts the structure now, which must cover every
    ``oauth-field`` case that is a JSON document as the text pattern does."""
    assert redact_obj(json.loads(text)) == json.loads(expected)


def test_redact_obj_leaves_other_keys_and_empty_or_scalar_credential_values_alone():
    """The key must name the field whole: a longer key is ordinary data, as
    the quoted prose keys are for `redact`. An empty value has nothing to
    hide, and a number or a boolean is not a credential."""
    value = {
        "refreshed": "yes",
        "accessible": "no",
        "access_level": "write",
        "refresh_interval_seconds": "3600",
        "access": "",
        "id_token": 7,
        "refresh": True,
    }
    assert redact_obj(value) == value


# One dummy of every recognised class, what ``redact`` makes of it, and the
# class ``credential_classes`` reports it under.
_CLASS_SAMPLES = [
    ("env-assignment", "GH_TOKEN=abc123XYZ", "GH_TOKEN=***REDACTED***", "env-assignment"),
    (
        "authorization-header",
        "Authorization: Bearer abcdef123456",
        "Authorization: Bearer ***REDACTED***",
        "authorization-header",
    ),
    ("url-userinfo", "https://u:p@h/r", "https://***REDACTED***@h/r", "url-userinfo"),
    ("jwt", _JWT, "***REDACTED***", "jwt"),
    ("github-fine-grained-pat", _FINE_GRAINED_PAT, "***REDACTED***", "github-fine-grained-pat"),
    ("github-token", "ghp_abcdefghijklmnop", "***REDACTED***", "github-token"),
    ("openai-key", "sk-proj-abcdef123456", "***REDACTED***", "openai-key"),
    # Replaced by the ``sk-`` pattern first; its own passes are checked alone.
    ("anthropic-key", "sk-ant-abcdefghij123", "***REDACTED***", "openai-key"),
    (
        "secret-assignment",
        "password=hunter2hunter2",
        "password=***REDACTED***",
        "secret-assignment",
    ),
    (
        "oauth-field",
        '"access_token": "FAKE-access"',
        '"access_token": "***REDACTED***"',
        "oauth-field",
    ),
    (
        "oauth-assignment",
        "refresh_token=FAKE_REFRESH_TOKEN",
        "refresh_token=***REDACTED***",
        "oauth-assignment",
    ),
    (
        "oauth-bare-assignment",
        "access=FAKE_ACCESS_VALUE_0000",
        "access=***REDACTED***",
        "oauth-bare-assignment",
    ),
]

# What a credential can directly follow (#284): the start of the text; ANSI
# sequences as OpenCode writes them on stderr (``providers.py``) and as other
# tools do; the same sequences written out in JSON, C, a shell and caret
# notation; backslash escapes written out; and any other character that is
# no part of a token. A word precedes each, so only the lead is a boundary.
_LEADS = {
    "start": "",
    "sgr-reset": "\x1b[0m",
    "sgr-run": "\x1b[91m\x1b[1m",
    "opencode-error": "\x1b[91m\x1b[1mError: \x1b[0m",
    "opencode-tool-failed": "\x1b[0m✗ \x1b[0m",
    "charset-escape": "\x1b(B",
    "c1-csi": "\x9b1m",
    "json-newline": "\\n",
    "json-tab": "\\t",
    "json-unicode": "\\u00e9",
    "json-esc": "\\u001b[0m",
    "json-in-json-newline": "\\\\n",
    "c-hex-esc": "\\x1b[1m",
    "c-octal-esc": "\\033[0m",
    "shell-esc": "\\e[0m",
    "caret-esc": "^[[0m",
    "non-ascii-letter": "é",
    "equals": "=",
    "colon": ":",
    "double-quote": '"',
    "single-quote": "'",
    "space": " ",
    "newline": "\n",
    "tab": "\t",
}


def _after(lead, text):
    return f"word{lead}{text} done" if lead else f"{text} done"


@pytest.mark.parametrize("lead", list(_LEADS.values()), ids=list(_LEADS))
@pytest.mark.parametrize(
    ("name", "sample", "expected", "reported"), _CLASS_SAMPLES, ids=[c[0] for c in _CLASS_SAMPLES]
)
def test_a_credential_is_recognised_after_an_escape_or_any_non_token_character(
    name, sample, expected, reported, lead
):
    """#284: ``ESC[0m`` ends in ``m`` and a written-out ``\\n`` in ``n``, word
    characters both, so a word-boundary anchor missed a token right after
    one, and OpenCode's ``stderr.log`` carries such sequences. Every class is
    redacted after each lead, by ``redact`` and by its own pattern alone,
    named by ``credential_classes``, and the lead is kept as it was."""
    text = _after(lead, sample)
    assert redact(text) == _after(lead, expected)
    assert credential_classes(text) == (reported,)
    assert redact(redact(text)) == redact(text)
    entry = next(entry for entry in _PATTERNS if entry.name == name)
    alone = entry.regex.sub(entry.replacement, text)
    if entry.after_escape is not None:
        alone = entry.after_escape.sub(entry.escape_replacement, alone)
    assert alone == _after(lead, expected)


# A shape inside a longer identifier is no token, as under the word boundary
# before #284: an ASCII letter, a digit or ``_`` right before it (``-`` too
# for a JWT), an escape followed by more of the identifier, or a backslash
# before a letter that makes no escape. A known name behind a prefix
# (``GEMINI_API_KEY``) is #285's.
_INSIDE_IDENTIFIERS = [
    "xghp_abcdefghijklmnop",
    "1ghp_abcdefghijklmnop",
    "_ghp_abcdefghijklmnop",
    "laughs_together_forever",
    "risk-assessment-pipeline",
    "the task-scheduler-daemon",
    "xgithub_pat_11ABCDEFG0abcdefghij",
    "myeyJhbGciOiJIUzI1NiJ9.e30.c2ln",
    "my-eyJhbGciOiJIUzI1NiJ9.e30.c2ln",
    "MY_GITHUB_TOKEN=abc123XYZ",
    "GEMINI_API_KEY=abcdefghijklmnop",
    "xpassword=hunter2hunter2",
    "preaccess=FAKE_ACCESS_VALUE_0000",
    "XAuthorization: Bearer abcdef123456",
    "\x1b[0mxghp_abcdefghijklmnop",
    "\\nxghp_abcdefghijklmnop",
    "\\Nghp_abcdefghijklmnop",
    "\\qsk-abcdefghij123456",
]


@pytest.mark.parametrize("text", _INSIDE_IDENTIFIERS)
def test_a_shape_inside_a_longer_identifier_is_no_token(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("caféghp_abcdefghijklmnop", "café***REDACTED***"),
        ("Пароль:sk-abcdefghij123456", "Пароль:***REDACTED***"),
        ("C:\\Users\\nghp_abcdefghijklmnop", "C:\\Users\\n***REDACTED***"),
    ],
    ids=["non-ascii-letter", "non-ascii-word", "path-separator"],
)
def test_the_boundary_changes_documented_since_284(text, expected):
    """What #284 changed beyond escapes, as ``secrets-and-logging.md`` says:
    a non-ASCII letter is no part of an ASCII token (``\\b`` took it for a word
    character), and a backslash before an escape letter is an escape even
    where it separates a path: the rule errs on redacting."""
    assert redact(text) == expected


def test_a_token_after_an_escape_in_serialized_json_is_redacted():
    """The boundary rule F2 (#281) relies on: a result printed as JSON text
    writes a newline as ``\\n``, a tab as ``\\t`` and a non-ASCII character as
    ``\\uXXXX``, and a token right after any of them is redacted there,
    leaving the JSON syntax intact."""
    token = "ghp_" + "e" * 36
    line = redact(json.dumps({"message": f"push failed:\n{token}\t{token}—{token}"}))
    assert token not in line
    assert json.loads(line) == {
        "message": "push failed:\n***REDACTED***\t***REDACTED***—***REDACTED***"
    }


def test_redact_obj_redacts_a_token_after_an_ansi_escape_at_any_depth():
    """State and the CLI's documents cross ``redact_obj``: the same rule
    holds in every string of a structure."""
    token = "ghp_" + "f" * 36
    value = {"block_reason": f"\x1b[91m\x1b[1mError: \x1b[0m{token}", "tail": [f"\x1b[1m{token}"]}
    assert redact_obj(value) == {
        "block_reason": "\x1b[91m\x1b[1mError: \x1b[0m***REDACTED***",
        "tail": ["\x1b[1m***REDACTED***"],
    }


def test_every_pattern_but_the_quoted_oauth_field_starts_a_token_after_an_escape():
    """A new pattern is built with ``_token``, never anchored on ``\\b``: only
    ``oauth-field``, which a quote starts, has no ``after_escape`` pass."""
    assert [entry.name for entry in _PATTERNS if entry.after_escape is None] == ["oauth-field"]
    assert not [entry.name for entry in _PATTERNS if r"\b" in entry.regex.pattern]


@pytest.mark.parametrize(
    "text",
    [
        "\x1b[0ma" * 50_000,
        "\\n" * 125_000,
        "\x1b[" + "0;" * 125_000,
        "\\u001b[" + "1" * 250_000 + "mghp_",
        "^[" * 125_000,
        "\\" * 250_000,
    ],
    ids=[
        "ansi-before-letters",
        "backslash-escapes",
        "long-csi",
        "long-written-out-csi",
        "carets",
        "backslashes",
    ],
)
def test_the_escape_rule_stays_linear(text):
    """Every candidate starts at an ESC, a backslash, a caret or the C1 byte
    and scans one escape; a quarter-megabyte of escape-heavy text, which a
    scan restarted at every candidate would take minutes over, is redacted
    well inside the budget."""
    started = time.perf_counter()
    redact(text)
    assert time.perf_counter() - started < 3.0


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

# The same units right after an escape (#284), so that every ``after_escape``
# pass is pinned as well: ``ESC[0m``, inside which no plain pass can start a
# match (after ``ESC[m`` a URL scheme could start at the ``m``), and the two
# shortest escapes, two characters each: a written-out ``\n`` and the C1
# CSI. The escape is kept and separates repeats, so no unit grows faster
# than its plain one.
_ESCAPE_LEADS = {"ansi": "\x1b[0m", "backslash": "\\n", "c1": "\x9bm"}
_ESCAPED_UNITS = {
    f"{lead}_{unit}": prefix + text
    for lead, prefix in _ESCAPE_LEADS.items()
    for unit, text in _WORST_CASE_UNITS.items()
}
_GROWTH_UNITS = {**_WORST_CASE_UNITS, **_ESCAPED_UNITS}


@pytest.mark.parametrize("unit", sorted(_GROWTH_UNITS), ids=str)
def test_redaction_growth_is_bounded(unit):
    """``redact`` never more than ``MAX_GROWTH_FACTOR``-uples a text.

    A secret shorter than the marker grows under redaction, and the persisted
    review evidence is redacted *after* the parser bounded it, so the factor
    is what lets ``loop_guard.MAX_REQUIRED_RESOLUTION_CHARS`` stand above
    every accepted resolution (#33). A new pattern must keep the factor or
    raise both together.
    """
    text = _GROWTH_UNITS[unit]
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


def test_every_after_escape_pass_has_a_growth_pin():
    """The same guard for the second pass of every pattern that starts a
    token: each finds one of the escaped units on its own."""
    units = list(_ESCAPED_UNITS.values())
    unpinned = [
        entry.name
        for entry in _PATTERNS
        if entry.after_escape is not None and not any(entry.after_escape.search(u) for u in units)
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
    *_GROWTH_UNITS.values(),
    *(unit * 50 for unit in _GROWTH_UNITS.values()),
    *(_after(lead, sample) for _, sample, _, _ in _CLASS_SAMPLES for lead in _LEADS.values()),
    *_INSIDE_IDENTIFIERS,
    "Authorization: Bearer HF_TOKEN=x GH_TOKEN=ghp_aaaaaaaa token=sk-aaaaaaaaaaaa",
    'https://HF_TOKEN="x"@h',
    # Already redacted text is clean: no pattern changes it again.
    "HF_TOKEN=***REDACTED***",
    "Authorization: Bearer ***REDACTED***",
    "https://***REDACTED***@github.com/o/r.git",
    "\x1b[91m\x1b[1mError: \x1b[0m***REDACTED***",
    "line\\n***REDACTED***",
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
