"""Redaction: common token patterns are masked, normal text untouched."""

import ast
import itertools
import json
import random
import re
import sys
import time
from urllib.parse import urlencode

import pytest

from autoforge.redaction import (
    _ENV_NAMES,
    _PATTERNS,
    _YAML_DASH_CHAR,
    MAX_GROWTH_FACTOR,
    _redact_once,
    credential_classes,
    redact,
    redact_obj,
)
from tests.conftest import (
    AWS_ACCESS_KEY_ID,
    AWS_KEY_BODY,
    CR_CRLF_KEY,
    CR_KEY,
    CR_RUN_KEY,
    CR_SPACE_CR_KEY,
    CR_SPACE_LF_KEY,
    FORM_SECRET,
    GITLAB_PAT,
    GITLAB_PAT_BODY,
    GOOGLE_API_KEY,
    GOOGLE_KEY_BODY,
    GROQ_API_KEY,
    GROQ_KEY_BODY,
    HF_TOKEN_BODY,
    HUGGINGFACE_TOKEN,
    NPM_TOKEN,
    NPM_TOKEN_BODY,
    PEM_BEGIN,
    PEM_END,
    PRIVATE_KEY,
    PRIVATE_KEY_BODY,
    PUNCTUATION_SECRET,
    SHAPES_285,
    SHORT_WRAPPED_KEY,
    SHORT_WRAPPED_KEY_LINE,
    SHORT_WRAPPED_KEY_LINES,
    SPACED_KEY,
    SUFFIX_ASSIGNMENT,
    SUFFIX_SECRET,
    UNDERSCORE_KEYED_SECRETS_285,
    WHOLE_VALUES_285,
    WHOLE_VALUES_285_R3,
    WHOLE_VALUES_285_R3_REDACTED,
    WHOLE_VALUES_285_R4,
    WHOLE_VALUES_285_R4_REDACTED,
    WHOLE_VALUES_285_R5,
    WHOLE_VALUES_285_R5_REDACTED,
    WHOLE_VALUES_285_R6,
    WHOLE_VALUES_285_R6_REDACTED,
    WHOLE_VALUES_285_R7,
    WHOLE_VALUES_285_R7_REDACTED,
    WHOLE_VALUES_285_R8,
    WHOLE_VALUES_285_R8_REDACTED,
    XAI_API_KEY,
    XAI_KEY_BODY,
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


# The dummy key of ``tests/conftest.py`` (#282) in the shapes a log shows it.
_GOOGLE_KEY_TEXTS = [
    (GOOGLE_API_KEY, "***REDACTED***"),
    (f"GEMINI_API_KEY={GOOGLE_API_KEY}", "GEMINI_API_KEY=***REDACTED***"),
    (f"GOOGLE_API_KEY={GOOGLE_API_KEY}", "GOOGLE_API_KEY=***REDACTED***"),
    (f'export GOOGLE_API_KEY="{GOOGLE_API_KEY}"', 'export GOOGLE_API_KEY="***REDACTED***"'),
    (f"GEMINI_API_KEY: '{GOOGLE_API_KEY}'", "GEMINI_API_KEY: '***REDACTED***'"),
    (json.dumps({"apiKey": GOOGLE_API_KEY}), '{"apiKey": "***REDACTED***"}'),
    (
        json.dumps({"env": {"GEMINI_API_KEY": GOOGLE_API_KEY}}),
        '{"env": {"GEMINI_API_KEY": "***REDACTED***"}}',
    ),
    (
        json.dumps(json.dumps({"apiKey": GOOGLE_API_KEY})),
        json.dumps(json.dumps({"apiKey": "***REDACTED***"})),
    ),
    (
        f"https://generativelanguage.googleapis.com/v1beta/models?key={GOOGLE_API_KEY}",
        "https://generativelanguage.googleapis.com/v1beta/models?key=***REDACTED***",
    ),
    (
        f"GET /maps/api/geocode/json?address=x&key={GOOGLE_API_KEY}&sensor=false HTTP/1.1",
        "GET /maps/api/geocode/json?address=x&key=***REDACTED***&sensor=false HTTP/1.1",
    ),
    (f"x-goog-api-key: {GOOGLE_API_KEY}", "x-goog-api-key: ***REDACTED***"),
    (f"{GOOGLE_API_KEY}\nsecond line", "***REDACTED***\nsecond line"),
    (f"first line\n{GOOGLE_API_KEY}", "first line\n***REDACTED***"),
    (f"first\r\n{GOOGLE_API_KEY}\r\nlast", "first\r\n***REDACTED***\r\nlast"),
    # What ``env`` prints: one assignment a line, each key at a line's end.
    (
        f"HOME=/home/u\nGEMINI_API_KEY={GOOGLE_API_KEY}\nGOOGLE_API_KEY={GOOGLE_API_KEY}\n",
        "HOME=/home/u\nGEMINI_API_KEY=***REDACTED***\nGOOGLE_API_KEY=***REDACTED***\n",
    ),
]
_GOOGLE_KEY_TEXT_IDS = [
    "alone",
    "gemini-assignment",
    "google-assignment",
    "quoted-export",
    "single-quoted",
    "json",
    "nested-json",
    "json-in-json",
    "url-query-last",
    "url-query-middle",
    "header",
    "line-start",
    "line-end",
    "crlf",
    "env-dump",
]


@pytest.mark.parametrize(("text", "expected"), _GOOGLE_KEY_TEXTS, ids=_GOOGLE_KEY_TEXT_IDS)
def test_a_google_api_key_is_redacted_wherever_it_stands(text, expected):
    """#282: OpenCode launches inherit ``GEMINI_*`` and ``GOOGLE_*``, and an
    agent that printed its environment put the key in its log in the clear:
    no pattern knew the shape, and a prefixed name is no ``secret-assignment``
    (#285). The shape is recognised under any name and none, in JSON, in a
    URL's query string and at either end of a line, and reported by class."""
    assert redact(text) == expected
    assert credential_classes(text) == ("google-api-key",)
    assert redact(expected) == expected


# Not a Google API key: the wrong length either way (a run of the key's
# alphabet that goes on is no key), the wrong prefix (the shape is
# case-sensitive), a character outside the alphabet, and a shape inside a
# longer identifier (``-`` included, as for a JWT). A short value behind a
# prefixed name is redacted by the name since #285 (below).
_GOOGLE_KEY_NEAR_MISSES = [
    GOOGLE_API_KEY[:-1],
    GOOGLE_API_KEY + "0",
    GOOGLE_API_KEY + "-",
    GOOGLE_API_KEY + "_",
    "AIzb" + GOOGLE_KEY_BODY,
    "AIZA" + GOOGLE_KEY_BODY,
    "aiza" + GOOGLE_KEY_BODY,
    "AIz" + GOOGLE_KEY_BODY + "0",
    "AIza" + GOOGLE_KEY_BODY[:10] + "+" + GOOGLE_KEY_BODY[11:],
    "x" + GOOGLE_API_KEY,
    "_" + GOOGLE_API_KEY,
    "1" + GOOGLE_API_KEY,
    "-" + GOOGLE_API_KEY,
    "\x1b[0mx" + GOOGLE_API_KEY,
]


@pytest.mark.parametrize("text", _GOOGLE_KEY_NEAR_MISSES)
def test_a_near_miss_of_a_google_api_key_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


def test_a_google_api_key_is_replaced_whole_before_a_shorter_shape_takes_a_slice():
    """``-`` is in the key's alphabet and no token character for the GitHub
    and ``sk-`` shapes, so a ``ghp_``, ``github_pat_`` or ``sk-`` after one
    inside a key would start a match there and leave the key's head in the
    clear: the key is replaced first. A JWT, which comes before it, takes a
    key that is one of its segments with it, and a ``sk-`` key whose tail
    reads like a Google key stays the ``sk-`` key it is."""
    for body in ("ab-ghp_" + "0" * 28, "ab-github_pat_" + "0" * 21, "ab-sk-" + "0" * 29):
        key = "AIza" + body
        assert len(key) == 39
        assert redact(f"key={key} end") == "key=***REDACTED*** end"
        assert credential_classes(key) == ("google-api-key",)
    jwt = f"eyJhbGciOiJIUzI1NiJ9.{GOOGLE_API_KEY}.c2ln"
    assert redact(jwt) == "***REDACTED***" and credential_classes(jwt) == ("jwt",)
    sk = f"sk-proj-{GOOGLE_API_KEY}"
    assert redact(sk) == "***REDACTED***" and credential_classes(sk) == ("openai-key",)


def test_redact_obj_redacts_a_google_api_key_at_any_depth():
    """State and the CLI's documents cross ``redact_obj``: a key under a
    ``GEMINI_*`` name, in a list and as a mapping key is redacted too."""
    value = {
        "GEMINI_API_KEY": GOOGLE_API_KEY,
        "env": [f"GOOGLE_API_KEY={GOOGLE_API_KEY}", 7],
        f"key {GOOGLE_API_KEY}": "a key is redacted too",
    }
    assert redact_obj(value) == {
        "GEMINI_API_KEY": "***REDACTED***",
        "env": ["GOOGLE_API_KEY=***REDACTED***", 7],
        "key ***REDACTED***": "a key is redacted too",
    }


@pytest.mark.parametrize(
    "text",
    [
        "AIza" * 250_000,
        ("AIza" + "0" * 34 + " ") * 25_000,
        "-AIza" * 200_000,
        ("\\nAIza" + "0" * 36) * 20_000,
        "\x1b[0m" + "AIza" + "0" * 1_000_000,
    ],
    ids=["prefixes", "one-short", "dashed-prefixes", "after-escapes-one-long", "long-run"],
)
def test_the_google_api_key_pattern_stays_linear(text):
    """The body is fixed: each candidate scans at most 40 characters, so a
    megabyte of near-misses is scanned well inside the budget."""
    started = time.perf_counter()
    assert credential_classes(text) == ()
    assert time.perf_counter() - started < 3.0


# The token shapes #285 adds (every one but the private-key block and the
# name rule, which have tests of their own below).
_TOKENS_285 = [
    (name, token, body)
    for name, token, body in SHAPES_285
    if name not in ("private-key", "credential-suffix-assignment")
]


def _token_texts(token):
    """``token`` where a log or published text shows a credential, each with
    what ``redact`` makes of it: under no name or one no pattern knows, in
    JSON, in a URL's query, in a header, in an ``.npmrc`` and at either end
    of a line."""
    r = "***REDACTED***"
    return [
        (token, r),
        (f"the key {token} expired", f"the key {r} expired"),
        (f'"{token}"', f'"{r}"'),
        (json.dumps({"apiKey": token}), json.dumps({"apiKey": r})),
        (json.dumps(json.dumps({"apiKey": token})), json.dumps(json.dumps({"apiKey": r}))),
        (
            f"https://api.example.test/v1?key={token}&x=1",
            f"https://api.example.test/v1?key={r}&x=1",
        ),
        (f"x-api-key: {token}", f"x-api-key: {r}"),
        (f"run --api-key={token}", f"run --api-key={r}"),
        (
            f"//registry.example.test/:_authToken={token}",
            f"//registry.example.test/:_authToken={r}",
        ),
        (f"{token}\nsecond line", f"{r}\nsecond line"),
        (f"first\r\n{token}\r\nlast", f"first\r\n{r}\r\nlast"),
    ]


_TOKEN_TEXT_IDS = [
    "alone",
    "prose",
    "quoted",
    "json",
    "json-in-json",
    "url-query",
    "header",
    "flag",
    "npmrc",
    "line-start",
    "crlf",
]


@pytest.mark.parametrize("index", range(len(_TOKEN_TEXT_IDS)), ids=_TOKEN_TEXT_IDS)
@pytest.mark.parametrize(("name", "token", "body"), _TOKENS_285, ids=[t[0] for t in _TOKENS_285])
def test_a_285_token_shape_is_redacted_wherever_it_stands(name, token, body, index):
    """#285: an AWS access key id and a GitLab, Groq, xAI, npm or Hugging
    Face token matched no pattern, so an agent or a command that printed one
    put it in the run log in the clear, and agent text carrying one could be
    published. Each is recognised by its shape under any name or none and
    reported by its class."""
    text, expected = _token_texts(token)[index]
    assert redact(text) == expected
    assert body not in redact(text)
    assert credential_classes(text) == (name,)
    assert redact(expected) == expected


@pytest.mark.parametrize(
    ("text", "expected", "name"),
    [
        # ``~/.aws/credentials`` and a SigV4 credential scope.
        (
            f"aws_access_key_id = {AWS_ACCESS_KEY_ID}",
            "aws_access_key_id = ***REDACTED***",
            "aws-access-key-id",
        ),
        (
            f"Credential={AWS_ACCESS_KEY_ID}/20261031/us-east-1/s3/aws4_request",
            "Credential=***REDACTED***/20261031/us-east-1/s3/aws4_request",
            "aws-access-key-id",
        ),
        ("ASIA" + AWS_KEY_BODY, "***REDACTED***", "aws-access-key-id"),
        # GitLab's API header, whose name no pattern knows.
        (f"PRIVATE-TOKEN: {GITLAB_PAT}", "PRIVATE-TOKEN: ***REDACTED***", "gitlab-pat"),
        # Longer than the floor: the whole run goes.
        ("gl" + "pat-" + GITLAB_PAT_BODY + "-more_0", "***REDACTED***", "gitlab-pat"),
        (
            f"huggingface-cli login --token {HUGGINGFACE_TOKEN}",
            "huggingface-cli login --token ***REDACTED***",
            "huggingface-token",
        ),
    ],
    ids=["aws-credentials", "sigv4", "aws-temporary", "gitlab-header", "gitlab-long", "hf-cli"],
)
def test_a_285_token_shape_is_redacted_where_its_tools_print_it(text, expected, name):
    assert redact(text) == expected
    assert credential_classes(text) == (name,)


# Not one of the #285 token shapes: the wrong length (an AWS key id is 20
# characters whole, a run that goes on is none; a prefixed token has at
# least 20 after its prefix), the prefix in another case, another AWS prefix
# (an IAM user's unique id is no credential), a shape inside a longer
# identifier (a kebab-case one too, for the shapes with a ``-``), and the
# snake_case names npm and Hugging Face tools print.
_NEAR_MISSES_285 = [
    AWS_ACCESS_KEY_ID[:-1],
    AWS_ACCESS_KEY_ID + "0",
    AWS_ACCESS_KEY_ID + "x",
    AWS_ACCESS_KEY_ID + "_",
    "akia" + AWS_KEY_BODY,
    "AKIA" + AWS_KEY_BODY.lower(),
    "AIDA" + AWS_KEY_BODY,
    "x" + AWS_ACCESS_KEY_ID,
    "_" + AWS_ACCESS_KEY_ID,
    "gs" + "k_" + "a" * 19,
    "GSK_" + GROQ_KEY_BODY,
    "x" + GROQ_API_KEY,
    "_" + GROQ_API_KEY,
    "xa" + "i-" + "a" * 19,
    "XAI-" + XAI_KEY_BODY,
    "grok-" + XAI_API_KEY,
    "1" + XAI_API_KEY,
    "np" + "m_" + "a" * 19,
    "NPM_" + NPM_TOKEN_BODY,
    "1" + NPM_TOKEN,
    "npm_config_registry=https://registry.example.test/",
    "npm_lifecycle_event=test npm_package_json=/w/package.json",
    "gl" + "pat-" + "a" * 19,
    "GLPAT-" + GITLAB_PAT_BODY,
    "my-" + GITLAB_PAT,
    "x" + GITLAB_PAT,
    "h" + "f_" + "a" * 19,
    "HF_" + HF_TOKEN_BODY,
    "_" + HUGGINGFACE_TOKEN,
    "from huggingface_hub import hf_hub_download; hf_hub_enable_hf_transfer",
    "\x1b[0mx" + HUGGINGFACE_TOKEN,
]


@pytest.mark.parametrize("text", _NEAR_MISSES_285)
def test_a_near_miss_of_a_285_token_shape_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


def test_a_285_token_is_replaced_whole_before_a_shorter_shape_takes_a_slice():
    """``-`` is in a GitLab token's alphabet, and no token character for the
    GitHub, ``sk-``, AWS and prefixed shapes, so one of those starting after
    a ``-`` inside a GitLab token would replace a slice of it and leave its
    head in the clear: the GitLab token is replaced first. The shapes #285
    adds without a ``-`` come after the ``sk-`` ones, so an ``sk-`` key whose
    tail reads like one stays the ``sk-`` key it is, and a Google key the
    Google key it is."""
    added = [
        "AKI" + "A" + "0" * 16,
        "h" + "f_" + "0" * 20,
        "np" + "m_" + "0" * 20,
        "gs" + "k_" + "0" * 20,
    ]
    for tail in ["gh" + "p_" + "0" * 17, "sk-" + "0" * 14, *added]:
        token = "gl" + "pat-ab-" + tail
        assert redact(f"key={token} end") == "key=***REDACTED*** end"
        assert credential_classes(token) == ("gitlab-pat",)
    for tail in added:
        sk = "sk-proj-ab-" + tail
        assert redact(sk) == "***REDACTED***" and credential_classes(sk) == ("openai-key",)
    google = "AIza" + "ab-" + "AKI" + "A" + "0" * 16 + "0" * 12
    assert len(google) == 39
    assert redact(google) == "***REDACTED***" and credential_classes(google) == ("google-api-key",)


@pytest.mark.parametrize(
    "text",
    [
        "gs" + "k_" * 500_000,
        ("np" + "m_" + "a" * 19 + " ") * 40_000,
        ("xa" + "i-") * 250_000,
        "gl" + "pat-" + "a-" * 500_000,
        ("gl" + "pat-" + "a" * 19 + " ") * 40_000,
        ("AKI" + "A" + "0" * 15 + " ") * 50_000,
        "AKI" + "A" * 1_000_000,
        ("\\nh" + "f_" + "a" * 19) * 40_000,
    ],
    ids=[
        "groq-prefixes",
        "npm-one-short",
        "xai-prefixes",
        "gitlab-run",
        "gitlab-one-short",
        "aws-one-short",
        "aws-run",
        "hf-after-escapes",
    ],
)
def test_the_285_token_patterns_stay_linear(text):
    """Each candidate starts at a prefix after a non-token character and
    scans one run of its alphabet, so a megabyte of near-misses is redacted
    well inside the budget (CPU time, which other test workers do not
    stretch)."""
    started = time.process_time()
    redact(text)
    assert time.process_time() - started < 3.0


def _pem(label, body, *, end=True):
    """A PEM block of ``label`` around ``body``, built in pieces as the
    dummies of ``tests/conftest.py`` are."""
    text = "-----BEGIN" + f" {label}-----\n{body}"
    return text + "\n-----END" + f" {label}-----" if end else text


_PEM_BODY_LINES = f"{PRIVATE_KEY_BODY}\n{PRIVATE_KEY_BODY}\nFAKE+/=="
_PEM_LABELS = [
    "PRIVATE KEY",
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "DSA PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
    "ENCRYPTED PRIVATE KEY",
    "SSH2 ENCRYPTED PRIVATE KEY",
    "PGP PRIVATE KEY BLOCK",
]


@pytest.mark.parametrize("label", _PEM_LABELS)
def test_a_private_key_block_is_redacted_whole_under_every_label(label):
    """#285: a private key an agent printed (``cat ~/.ssh/id_ed25519``, a
    key in a configuration file) reached the run log in the clear. The block
    is replaced whole, BEGIN and END lines included, and the text around it
    is kept."""
    text = f"$ cat key.pem\n{_pem(label, _PEM_BODY_LINES)}\n$ next"
    assert redact(text) == "$ cat key.pem\n***REDACTED***\n$ next"
    assert credential_classes(text) == ("private-key",)


_ENCRYPTED_PKCS1 = _pem(
    "RSA PRIVATE KEY",
    f"Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,3F17F5316E2BAC89\n\n{_PEM_BODY_LINES}",
)
_PGP_WITH_HEADERS = _pem(
    "PGP PRIVATE KEY BLOCK",
    f"Version: GnuPG v2.0.22 (GNU/Linux)\nComment: https://example.test/key?id=7\n\n"
    f"{_PEM_BODY_LINES}\n=FAKE",
)
_SERVICE_ACCOUNT = {
    "type": "service_account",
    "private_key": PRIVATE_KEY + "\n",
    "client_email": "bot@example.test",
}
_REDACTED_ACCOUNT = dict(_SERVICE_ACCOUNT, private_key="***REDACTED***\n")
_PEM_TEXTS = [
    (PRIVATE_KEY, "***REDACTED***"),
    (PRIVATE_KEY.replace("\n", "\r\n") + "\r\n", "***REDACTED***\r\n"),
    (
        "ssh_key: |\n" + "".join(f"  {line}\n" for line in PRIVATE_KEY.split("\n")) + "other: 1",
        "ssh_key: |\n  ***REDACTED***\nother: 1",
    ),
    (f'SSH_PRIVATE_KEY="{PRIVATE_KEY}"', 'SSH_PRIVATE_KEY="***REDACTED***"'),
    (f"key: {PRIVATE_KEY} (rotated)", "key: ***REDACTED*** (rotated)"),
    (_ENCRYPTED_PKCS1, "***REDACTED***"),
    (_PGP_WITH_HEADERS, "***REDACTED***"),
    # Each line behind a log prefix.
    (
        "\n".join(f"stderr| {line}" for line in PRIVATE_KEY.split("\n")),
        "stderr| ***REDACTED***",
    ),
    # A JSON key file, as a Google service account's is, and the same
    # document inside a JSON string: the syntax around the key is kept.
    (json.dumps(_SERVICE_ACCOUNT), json.dumps(_REDACTED_ACCOUNT)),
    (json.dumps(json.dumps(_SERVICE_ACCOUNT)), json.dumps(json.dumps(_REDACTED_ACCOUNT))),
    (repr({"key": PRIVATE_KEY}), repr({"key": "***REDACTED***"})),
    # Directly after an escape and after a letter: no identifier holds a block.
    ("\x1b[0m" + PRIVATE_KEY, "\x1b[0m***REDACTED***"),
    ("x" + PRIVATE_KEY, "x***REDACTED***"),
]
_PEM_TEXT_IDS = [
    "alone",
    "crlf",
    "yaml-block",
    "quoted-env",
    "inline",
    "encrypted-pkcs1",
    "pgp-headers",
    "log-prefix",
    "service-account-json",
    "json-in-json",
    "python-repr",
    "after-escape",
    "after-letter",
]


@pytest.mark.parametrize(("text", "expected"), _PEM_TEXTS, ids=_PEM_TEXT_IDS)
def test_a_private_key_block_is_redacted_whole_wherever_it_stands(text, expected):
    assert redact(text) == expected
    assert PRIVATE_KEY_BODY not in redact(text)
    assert credential_classes(text) == ("private-key",)
    assert redact(expected) == expected


_OMITTED = "[autoforge: 4096 bytes of stdout omitted; the capture bound is 16 MiB]"
_HEAD_OF_KEY = f"{PEM_BEGIN}\n{PRIVATE_KEY_BODY}\n{PRIVATE_KEY_BODY[:20]}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # ``head`` of a key file, and a capture cut inside the key: the BEGIN
        # line and the body go; the omission marker after them stays.
        (f"{_HEAD_OF_KEY}\n", "***REDACTED***\n"),
        (f"{_HEAD_OF_KEY}\n{_OMITTED}\nlast line", f"***REDACTED***\n{_OMITTED}\nlast line"),
        # A capture cut inside the key with both ends kept is one block.
        (
            f"{_HEAD_OF_KEY}\n{_OMITTED}\n{PRIVATE_KEY_BODY}\n{PEM_END}\nlast",
            "***REDACTED***\nlast",
        ),
        # Escaped, as JSON and JSON in JSON write it: the line break and the
        # quote after the body are kept, so the text still decodes.
        (json.dumps({"key": f"{_HEAD_OF_KEY}\n"}), json.dumps({"key": "***REDACTED***\n"})),
        (
            json.dumps(json.dumps({"key": f"{_HEAD_OF_KEY}\n"})),
            json.dumps(json.dumps({"key": "***REDACTED***\n"})),
        ),
        # Two heads in a row: the line break the second starts after is no
        # part of the first.
        (
            json.dumps(f"{_HEAD_OF_KEY}\n{_HEAD_OF_KEY}"),
            json.dumps("***REDACTED***\n***REDACTED***"),
        ),
        # The encrypted header lines are body too.
        (
            f"{PEM_BEGIN}\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,3F17F5316E2BAC89\n\n"
            f"{PRIVATE_KEY_BODY}\n$ ",
            "***REDACTED***\n$ ",
        ),
    ],
    ids=["head", "cut", "cut-both-ends", "json", "json-in-json", "two-heads", "encrypted-head"],
)
def test_a_private_key_block_without_an_end_line_is_redacted_to_its_body(text, expected):
    """A capture bounded inside a key, or the first lines of a key file,
    hold no END line: the BEGIN line and the body after it go, as far as a
    key body goes, and the line break after the last run of it is kept."""
    assert redact(text) == expected
    assert PRIVATE_KEY_BODY not in redact(text) and PRIVATE_KEY_BODY[:20] not in redact(text)
    assert credential_classes(text) == ("private-key",)
    assert redact(expected) == expected


def test_a_private_key_block_without_an_end_line_errs_on_redacting():
    """A body no END line closes runs on to the first character a key body
    cannot hold (a quote, a period, a bracket), so a line of words after it
    goes with it."""
    text = f"{_HEAD_OF_KEY}\nError: unexpected end of file. Done"
    assert redact(text) == "***REDACTED***. Done"


def _rewrap(key, width):
    """``key`` with its body rewrapped at ``width`` characters a line, as
    ``fold -w`` writes it: a key OpenSSL still reads."""
    begin, *body, end = key.split("\n")
    joined = "".join(body)
    return "\n".join([begin, *(joined[i : i + width] for i in range(0, len(joined), width)), end])


_SHORT_WRAPPED_TEXTS = [
    (SHORT_WRAPPED_KEY, "***REDACTED***"),
    (_rewrap(PRIVATE_KEY, 4), "***REDACTED***"),
    (_rewrap(PRIVATE_KEY, 1), "***REDACTED***"),
    (SHORT_WRAPPED_KEY.replace("\n", "\r\n") + "\r\n", "***REDACTED***\r\n"),
    (
        "ssh_key: |\n" + "".join(f"  {line}\n" for line in SHORT_WRAPPED_KEY.split("\n")) + "x: 1",
        "ssh_key: |\n  ***REDACTED***\nx: 1",
    ),
    (json.dumps({"key": SHORT_WRAPPED_KEY + "\n"}), json.dumps({"key": "***REDACTED***\n"})),
    (
        json.dumps(json.dumps({"key": SHORT_WRAPPED_KEY})),
        json.dumps(json.dumps({"key": "***REDACTED***"})),
    ),
    (
        json.dumps({"key": SHORT_WRAPPED_KEY.replace("\n", "\r\n")}),
        json.dumps({"key": "***REDACTED***"}),
    ),
    # Its first lines alone, as ``head`` shows them or a capture cut them:
    # the BEGIN line and the body after it go.
    ("\n".join(SHORT_WRAPPED_KEY.split("\n")[:4]) + "\n$ ", "***REDACTED***\n$ "),
    (f'DEPLOY_SECRET="{SHORT_WRAPPED_KEY}" next', 'DEPLOY_SECRET="***REDACTED***" next'),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _SHORT_WRAPPED_TEXTS,
    ids=[
        "eight",
        "four",
        "one",
        "crlf",
        "yaml-block",
        "json",
        "json-in-json",
        "json-crlf",
        "head",
        "quoted-env",
    ],
)
def test_a_private_key_wrapped_in_short_lines_is_redacted_whole(text, expected):
    """#285 review: key material was 16 base64 characters on one line, so a
    valid key rewrapped at fewer a line (``fold -w 8``, which OpenSSL still
    reads) was no credential: it reached the run log whole and passed the
    published-content and commit-message checks. A line break and the
    indentation after it may stand between any two of the 16 now."""
    out = redact(text)
    assert out == expected
    assert "MIIEFAKE" not in out and SHORT_WRAPPED_KEY_LINE not in out
    assert credential_classes(text) == ("private-key",)
    assert redact(expected) == expected


def test_a_normally_wrapped_key_is_still_one_block():
    """The usual 64 and OpenSSH's 70 characters a line are unchanged."""
    for width in (64, 70, 76):
        text = f"$ cat key\n{_rewrap(SHORT_WRAPPED_KEY, width)}\n$ next"
        assert redact(text) == "$ cat key\n***REDACTED***\n$ next"
        assert credential_classes(text) == ("private-key",)


def _respace(key, width, sep):
    """``key`` with its body in runs of ``width`` characters parted by
    ``sep``, on one line: a key a base64 decoder, OpenSSL's, still reads."""
    begin, *body, end = key.split("\n")
    joined = "".join(body)
    runs = (joined[i : i + width] for i in range(0, len(joined), width))
    return "\n".join([begin, sep.join(runs), end])


# A key whose body has spaces or tabs between short runs (#285 review, round
# 5), each with what ``redact`` makes of it: runs of eight, four, two and one
# character, spaces, tabs and both, several spaces, a space at a line's end,
# the whole block on one line (as a progress line shows a short-wrapped key),
# in a YAML block, as JSON and JSON in JSON (a tab written out as ``\t``), its
# first lines alone, and under a quoted assignment.
_SPACED_KEY_TEXTS = [
    (SPACED_KEY, "***REDACTED***"),
    (_respace(PRIVATE_KEY, 8, " "), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 4, "\t"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 2, " \t"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 1, " "), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 8, "   "), "***REDACTED***"),
    (_rewrap(PRIVATE_KEY, 8).replace("\n", " \n"), "***REDACTED***"),
    (" ".join(SHORT_WRAPPED_KEY.split("\n")), "***REDACTED***"),
    (
        "ssh_key: |\n" + "".join(f"  {line}\n" for line in SPACED_KEY.split("\n")) + "x: 1",
        "ssh_key: |\n  ***REDACTED***\nx: 1",
    ),
    (json.dumps({"key": SPACED_KEY + "\n"}), json.dumps({"key": "***REDACTED***\n"})),
    (
        json.dumps(json.dumps({"key": SPACED_KEY})),
        json.dumps(json.dumps({"key": "***REDACTED***"})),
    ),
    ("\n".join(SPACED_KEY.split("\n")[:2]) + "\n$ ", "***REDACTED***\n$ "),
    (f'DEPLOY_SECRET="{SPACED_KEY}" next', 'DEPLOY_SECRET="***REDACTED***" next'),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _SPACED_KEY_TEXTS,
    ids=[
        "spaces-and-tabs",
        "eight",
        "four-tabs",
        "two-space-and-tab",
        "one",
        "several-spaces",
        "space-before-line-break",
        "one-line",
        "yaml-block",
        "json",
        "json-in-json",
        "head",
        "quoted-env",
    ],
)
def test_a_private_key_with_spaces_between_short_runs_is_redacted_whole(text, expected):
    """#285 review, round 5: a base64 decoder skips spaces and tabs, so a
    key whose body has one after every eight characters is a valid key, and
    one a progress line joined into one line is one still, but a space
    parted two runs of key material: the block reached the run log whole and
    passed every published-content and commit-message check. Sixteen
    characters parted by spaces and tabs are material where they mix case
    and hold a digit, a ``+`` or a ``/``."""
    out = redact(text)
    assert out == expected
    assert "MIIEFAKE" not in out and SHORT_WRAPPED_KEY_LINE not in out
    assert credential_classes(text) == ("private-key",)
    assert redact(expected) == expected


# Not key material though spaces part it (#285 review, round 5): the words of
# a placeholder, 16 letters and more, in lower case, in title case and with a
# digit but no upper-case letter, a hex dump in either case, and a key's
# runs parted by a blank line.
_SPACED_PEM_NEAR_MISSES = [
    f"{PEM_BEGIN}\npaste your key here\n{PEM_END}",
    f"{PEM_BEGIN}\nPaste Your Private Key Here\n{PEM_END}",
    f"{PEM_BEGIN}\nyour ed25519 key goes here\n{PEM_END}",
    f"{PEM_BEGIN}\n00 1a 2b 3c 4d 5e 6f 70 81 92 a3 b4\n{PEM_END}",
    f"{PEM_BEGIN}\n00 1A 2B 3C 4D 5E 6F 70 81 92 A3 B4\n{PEM_END}",
    f"{PEM_BEGIN} the key's lines go here, then the END line {PEM_END}",
    f"{PEM_BEGIN}\nMIIE FAKE\n\ndumm y0pr\n{PEM_END}",
]


@pytest.mark.parametrize("text", _SPACED_PEM_NEAR_MISSES)
def test_a_near_miss_of_a_spaced_private_key_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


# A key whose body's runs bare carriage returns part (#285 review, round 6),
# each with what ``redact`` makes of it: runs of eight, four and one
# character, the BEGIN and END lines parted by line feeds, by carriage
# returns too and by CRLF, a space or a tab beside each carriage return,
# the body's runs ending in CRLF, and written out as JSON three deep, its
# first lines alone, and under a quoted assignment.
_CR_KEY_TEXTS = [
    (CR_KEY, "***REDACTED***"),
    (_respace(PRIVATE_KEY, 8, "\r"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 4, "\r"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 1, "\r"), "***REDACTED***"),
    (CR_KEY.replace("\n", "\r"), "***REDACTED***"),
    (CR_KEY.replace("\n", "\r\n") + "\r\n", "***REDACTED***\r\n"),
    (_respace(PRIVATE_KEY, 8, " \r"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 8, "\r\t"), "***REDACTED***"),
    (json.dumps({"key": CR_KEY}), json.dumps({"key": "***REDACTED***"})),
    (json.dumps(json.dumps({"key": CR_KEY})), json.dumps(json.dumps({"key": "***REDACTED***"}))),
    (
        json.dumps(json.dumps(json.dumps({"key": CR_KEY}))),
        json.dumps(json.dumps(json.dumps({"key": "***REDACTED***"}))),
    ),
    ("\r".join(CR_KEY.split("\r")[:3]) + "\n$ ", "***REDACTED***\n$ "),
    (f'DEPLOY_SECRET="{CR_KEY}" next', 'DEPLOY_SECRET="***REDACTED***" next'),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _CR_KEY_TEXTS,
    ids=[
        "eight",
        "eight-respaced",
        "four",
        "one",
        "carriage-returns-only",
        "crlf-lines",
        "space-before",
        "tab-after",
        "json",
        "json-in-json",
        "three-deep",
        "head",
        "quoted-env",
    ],
)
def test_a_private_key_whose_runs_bare_carriage_returns_part_is_redacted_whole(text, expected):
    """#285 review, round 6: a base64 decoder skips a carriage return as it
    skips a line feed, so a key whose body's short runs bare carriage
    returns part is a valid key, OpenSSL reading it to the same public key,
    but only ``\\n`` and ``\\r\\n`` were line breaks between two runs of
    material: the whole block reached every sink and passed every check, as
    text and written out. A carriage return alone is a line break now, as
    written and written out, and the block is replaced whole."""
    out = redact(text)
    assert out == expected
    assert SHORT_WRAPPED_KEY_LINE not in out and "MIIE" not in out and "BEGIN" not in out
    assert credential_classes(text) == ("private-key",)
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert len(out) <= MAX_GROWTH_FACTOR * len(text)


def _cr_lines(text):
    """``text`` with each line feed a carriage return, but for a blank line,
    whose two line feeds stay, each after a carriage return: a run of
    carriage returns is one line break (#285 review, round 7), and only a
    blank line of two line feeds parts two runs of key material."""
    return "\r\n\r\n".join(part.replace("\n", "\r") for part in text.split("\n\n"))


# Not key material though carriage returns part its lines (#285 review,
# rounds 6 and 7): the near-misses above with each line feed a carriage
# return and a blank line as two CRLFs; 16 characters a blank line parts,
# its two line feeds after a carriage return, after two (a terminal's capture
# of a CRLF file) and before one; 15 characters a run of carriage returns
# parts; and the same written out.
_CR_PEM_NEAR_MISSES = [
    *(_cr_lines(text) for text in _SPACED_PEM_NEAR_MISSES),
    f"{PEM_BEGIN}\rMIIEFAKE\r\n\r\nAAAAAAAA\r{PEM_END}",
    f"{PEM_BEGIN}\rMIIEFAKE\r\r\n\r\r\nAAAAAAAA\r{PEM_END}",
    f"{PEM_BEGIN}\rMIIEFAKE\n\r\nAAAAAAAA\r{PEM_END}",
    f"{PEM_BEGIN}\rMIIEFAKE\r\r\rAAAAAAA\r{PEM_END}",
    f"{PEM_BEGIN}\r\nMIIEFAKE\r\n\r\nAAAAAAAA\r\n{PEM_END}",
    f"{PEM_BEGIN}\rMIIE\r...\r{PEM_END}",
    f"{PEM_BEGIN}\r<your key here>\r{PEM_END}",
    f"{PEM_BEGIN}\r{PEM_END}",
    f"{PEM_BEGIN}\\r\\nMIIEFAKE\\r\\n\\r\\nAAAAAAAA\\r\\n{PEM_END}",
]


@pytest.mark.parametrize("text", _CR_PEM_NEAR_MISSES)
def test_a_near_miss_of_a_key_parted_by_carriage_returns_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


def _one_line_key(width, sep):
    """The shortest key body a private key has, 64 characters, in runs of
    ``width`` parted by ``sep``, between its BEGIN and END lines."""
    runs = (PRIVATE_KEY_BODY[i : i + width] for i in range(0, len(PRIVATE_KEY_BODY), width))
    return f"{PEM_BEGIN}\n{sep.join(runs)}\n{PEM_END}"


# A key whose runs a run of carriage returns parts, alone or around a line
# feed (#285 review, round 7), each with what ``redact`` makes of it: two
# carriage returns between runs of eight, three, two between runs of four
# and of one, a carriage return before each CRLF of every line (a terminal's
# capture of a CRLF key file) and after it too, one after each line feed, an
# indentation after them, spaces beside them, the shortest body, written out
# as JSON three deep, its first lines alone, under a quoted assignment.
_CR_RUN_KEY_TEXTS = [
    (CR_RUN_KEY, "***REDACTED***"),
    (_respace(PRIVATE_KEY, 8, "\r\r\r"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 4, "\r\r"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 1, "\r\r"), "***REDACTED***"),
    (CR_CRLF_KEY + "\r\r\n", "***REDACTED***\r\r\n"),
    (_respace(PRIVATE_KEY, 8, "\r\n\r"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 8, "\n\r"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 8, "\r\r  "), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 8, " \r\r "), "***REDACTED***"),
    (_one_line_key(8, "\r\r"), "***REDACTED***"),
    (json.dumps({"key": CR_RUN_KEY}), json.dumps({"key": "***REDACTED***"})),
    (
        json.dumps(json.dumps({"key": CR_CRLF_KEY})),
        json.dumps(json.dumps({"key": "***REDACTED***"})),
    ),
    (
        json.dumps(json.dumps(json.dumps({"key": CR_RUN_KEY}))),
        json.dumps(json.dumps(json.dumps({"key": "***REDACTED***"}))),
    ),
    (
        json.dumps(json.dumps(json.dumps({"key": _one_line_key(8, "\r\r\r")}))),
        json.dumps(json.dumps(json.dumps({"key": "***REDACTED***"}))),
    ),
    ("\r\r".join(CR_RUN_KEY.split("\r\r")[:3]) + "\n$ ", "***REDACTED***\n$ "),
    (f'DEPLOY_SECRET="{CR_RUN_KEY}" next', 'DEPLOY_SECRET="***REDACTED***" next'),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _CR_RUN_KEY_TEXTS,
    ids=[
        "two",
        "three",
        "four-wide",
        "one-wide",
        "terminal-capture",
        "around-a-line-feed",
        "after-a-line-feed",
        "indented",
        "spaced",
        "shortest-body",
        "json",
        "json-in-json",
        "three-deep",
        "three-deep-shortest",
        "head",
        "quoted-env",
    ],
)
def test_a_private_key_whose_runs_repeated_carriage_returns_part_is_redacted_whole(text, expected):
    """#285 review, round 7: a carriage return advances no line, and a
    base64 decoder skips any number of them, so a key whose body's runs two
    carriage returns part, or a terminal's capture of a CRLF key file
    (``\\r\\r\\n``), is a valid key, OpenSSL reading it to the same public
    key; but both material matchers allowed one line break between two runs,
    and a second carriage return was a second, so the whole block reached
    every sink and passed every check, as text and written out. A run of
    carriage returns, alone or around a line feed, is one break now."""
    out = redact(text)
    assert out == expected
    assert SHORT_WRAPPED_KEY_LINE not in out and "MIIE" not in out and "BEGIN" not in out
    assert credential_classes(text) == ("private-key",)
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert len(out) <= MAX_GROWTH_FACTOR * len(text)


# A key whose runs horizontal space among a break's carriage returns and line
# feed parts (#285 review, round 8), each with what ``redact`` makes of it: a
# carriage return, a space and a line feed, and a carriage return, a space and
# a carriage return, between runs of eight; a space before a CRLF, after a
# line feed and before a carriage return, a tab among them; runs of four and
# of one; the shortest body; written out as JSON three deep; its first lines
# alone; under a quoted assignment.
_CR_SPACE_KEY_TEXTS = [
    (CR_SPACE_LF_KEY, "***REDACTED***"),
    (CR_SPACE_CR_KEY, "***REDACTED***"),
    (_respace(PRIVATE_KEY, 8, " \r\n"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 8, "\n \r"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 8, "\t\r\t\n\t"), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 4, "\r \r \n "), "***REDACTED***"),
    (_respace(PRIVATE_KEY, 1, "\r \n"), "***REDACTED***"),
    (_one_line_key(8, "\r \n"), "***REDACTED***"),
    (_one_line_key(8, "\r \r"), "***REDACTED***"),
    (json.dumps({"key": CR_SPACE_LF_KEY}), json.dumps({"key": "***REDACTED***"})),
    (
        json.dumps(json.dumps({"key": CR_SPACE_CR_KEY})),
        json.dumps(json.dumps({"key": "***REDACTED***"})),
    ),
    (
        json.dumps(json.dumps(json.dumps({"key": CR_SPACE_LF_KEY}))),
        json.dumps(json.dumps(json.dumps({"key": "***REDACTED***"}))),
    ),
    (
        json.dumps(json.dumps(json.dumps({"key": _one_line_key(8, "\t\r \n")}))),
        json.dumps(json.dumps(json.dumps({"key": "***REDACTED***"}))),
    ),
    ("\r \n".join(CR_SPACE_LF_KEY.split("\r \n")[:3]) + "\n$ ", "***REDACTED***\n$ "),
    (f'DEPLOY_SECRET="{CR_SPACE_CR_KEY}" next', 'DEPLOY_SECRET="***REDACTED***" next'),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _CR_SPACE_KEY_TEXTS,
    ids=[
        "cr-space-lf",
        "cr-space-cr",
        "space-before-crlf",
        "space-after-lf",
        "tabs-among-them",
        "four-wide",
        "one-wide",
        "shortest-body-cr-space-lf",
        "shortest-body-cr-space-cr",
        "json",
        "json-in-json",
        "three-deep",
        "three-deep-shortest",
        "head",
        "quoted-env",
    ],
)
def test_a_private_key_whose_breaks_hold_horizontal_space_is_redacted_whole(text, expected):
    """#285 review, round 8: a base64 decoder skips spaces, tabs and
    carriage returns in any order, so a key whose runs ``\\r \\n`` or
    ``\\r \\r`` part is one OpenSSL reads, to the same public key; but a line
    break was a run of carriage returns around one line feed, with an
    indentation after it only, so a space inside it parted two runs and the
    whole block reached every sink and passed every check, as text and
    written out. A break is any run of spaces, tabs and carriage returns
    holding a carriage return or a line feed, and one line feed at most."""
    out = redact(text)
    assert out == expected
    assert SHORT_WRAPPED_KEY_LINE not in out and "MIIE" not in out and "BEGIN" not in out
    assert credential_classes(text) == ("private-key",)
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert len(out) <= MAX_GROWTH_FACTOR * len(text)


# Not key material though horizontal space stands in its breaks (#285
# review, round 8): 16 characters a blank line parts, with a space, with a
# tab and a carriage return, with carriage returns and spaces around both
# line feeds, as written and written out (OpenSSL refuses a blank line in a
# key's body); 15 characters such breaks part; a placeholder and short words
# whose lines end in a space before a CRLF (14 letters joined); and a hex
# dump in either case whose lines do.
_R8_PEM_NEAR_MISSES = [
    f"{PEM_BEGIN}\nMIIEFAKE\n \nAAAAAAAA\n{PEM_END}",
    f"{PEM_BEGIN}\nMIIEFAKE\n\t\r\nAAAAAAAA\n{PEM_END}",
    f"{PEM_BEGIN}\nMIIEFAKE\r \r\n \r\nAAAAAAAA\n{PEM_END}",
    f"{PEM_BEGIN}\\nMIIEFAKE\\r \\n \\r\\nAAAAAAAA\\n{PEM_END}",
    f"{PEM_BEGIN}\rMIIEFAKE\r \rAAAAAAA\r \n{PEM_END}",
    f"{PEM_BEGIN}\r\n<your key here> \r\n{PEM_END}",
    f"{PEM_BEGIN} \r\nput \r\nyour \r\nkey \r\nhere \r\n{PEM_END}",
    f"{PEM_BEGIN}\n00 1a 2b 3c \r\n4d 5e 6f 70 \r\n81 92 a3 b4\n{PEM_END}",
    f"{PEM_BEGIN}\n00 1A 2B 3C\r \r4D 5E 6F 70\r \r81 92 A3 B4\n{PEM_END}",
]


@pytest.mark.parametrize("text", _R8_PEM_NEAR_MISSES)
def test_a_near_miss_of_a_key_whose_breaks_hold_horizontal_space_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


# Not a private key: a public key, a certificate, a label in lower case or
# behind four dashes, and a BEGIN line with no key material after it (alone,
# quoted by code or prose, an empty block, a placeholder). Material joined
# across line breaks is no more than a line's words: a space parts two runs,
# a blank line too, and a line of code or prose after a BEGIN line holds
# fewer than 16 base64 characters joined to the next.
_PEM_NEAR_MISSES = [
    _pem("PUBLIC KEY", "\n".join(SHORT_WRAPPED_KEY_LINES)),
    _pem("CERTIFICATE", "\n".join(SHORT_WRAPPED_KEY_LINES)),
    f"{PEM_BEGIN}\nput\nyour\nkey\nhere\n{PEM_END}",
    f"{PEM_BEGIN}\n<your key here>\n{PEM_END}",
    f"{PEM_BEGIN}\nMIIE\n...\n{PEM_END}",
    f"{PEM_BEGIN}\nMIIEFAKE\n\nAAAAAAA\n{PEM_END}",
    f'if line.startswith("{PEM_BEGIN}"):\n    in_key = True\nelif line.startswith("{PEM_END}"):',
    f"{PEM_BEGIN}\nthe key's lines follow here and the END line closes it",
    _pem("PUBLIC KEY", _PEM_BODY_LINES),
    _pem("RSA PUBLIC KEY", _PEM_BODY_LINES),
    _pem("CERTIFICATE", _PEM_BODY_LINES),
    _pem("rsa private key", _PEM_BODY_LINES),
    "----BEGIN" + f" RSA PRIVATE KEY----\n{_PEM_BODY_LINES}\n----END" + " RSA PRIVATE KEY----",
    PEM_BEGIN,
    f"{PEM_BEGIN}\n",
    f'if line.startswith("{PEM_BEGIN}"):',
    f"put the key between {PEM_BEGIN} and {PEM_END} lines",
    f"{PEM_BEGIN}\n{PEM_END}",
    f"{PEM_BEGIN}\n...\n{PEM_END}",
    "-----BEGIN ... PRIVATE KEY-----",
]


@pytest.mark.parametrize("text", _PEM_NEAR_MISSES)
def test_a_near_miss_of_a_private_key_block_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


def test_a_private_key_block_is_replaced_before_any_pattern_takes_a_slice():
    """The block runs first: a named assignment's value stops at the space
    in the BEGIN line, and a block can hold what another pattern takes, so
    either would leave the rest of the key behind."""
    text = f'DEPLOY_SECRET="{PRIVATE_KEY}" next'
    assert redact(text) == 'DEPLOY_SECRET="***REDACTED***" next'
    assert credential_classes(text) == ("private-key",)
    inner = f"{PEM_BEGIN}\n{GITLAB_PAT}\n{PRIVATE_KEY_BODY}\n{PEM_END}"
    assert redact(inner) == "***REDACTED***" and credential_classes(inner) == ("private-key",)


@pytest.mark.parametrize(
    "text",
    [
        PEM_BEGIN * 40_000,
        PEM_BEGIN + "A" * 1_000_000,
        PEM_BEGIN + ("a" * 15 + " ") * 62_500,
        (PEM_BEGIN + ("a" * 15 + " ") * 10) * 5_000,
        PEM_BEGIN + "a." * 500_000,
        ("\\n" + PEM_BEGIN) * 30_000,
        (PEM_BEGIN + "-") * 30_000,
        # A quarter-megabyte, as for the escape rule: every pattern's
        # ``after_escape`` pass tries each backslash.
        PEM_BEGIN + "\\" * 250_000,
        (PEM_BEGIN + "\n" + "A" * 16 + "\n" + "-" * 4) * 20_000,
        # #285 review: material joined across line breaks.
        PEM_BEGIN + ("a\n" * 15 + " ") * 30_000,
        (PEM_BEGIN + ("a\\n" * 15 + " ") * 10) * 3_000,
        PEM_BEGIN + "a\n" * 500_000,
        PEM_BEGIN + ("\\n" * 7 + "a") * 100_000,
        (PEM_BEGIN + "\n" + "aaaa\n" * 4 + "-" * 4) * 20_000,
        # Round 5: runs parted by spaces and tabs that are no material, each
        # failing at its third kind, the last of its 16, or a non-base64
        # character; and every run a candidate's start.
        PEM_BEGIN + "a1 " * 333_333,
        PEM_BEGIN + "aB\t" * 333_333,
        PEM_BEGIN + ("aB1 " * 3 + "aB1.") * 60_000,
        (PEM_BEGIN + " aB1" * 3 + "-" * 4) * 30_000,
        PEM_BEGIN + ("a" * 15 + "1 ") * 60_000,
        PEM_BEGIN + ("aB1\\t" * 3 + "!") * 50_000,
        # Round 6: runs parted by bare carriage returns, as written and
        # written out, short of 16 or each a candidate's start.
        PEM_BEGIN + ("aB1\r" * 3 + "!") * 50_000,
        PEM_BEGIN + "\\r" * 500_000,
        (PEM_BEGIN + "\r" + "aaaa\r" * 4 + "-" * 4) * 20_000,
        # Round 7: runs of carriage returns, alone and around line feeds, as
        # written and written out, short of 16, each a candidate's start, and
        # long runs of them between characters and after a BEGIN line.
        PEM_BEGIN + ("aB1\r\r" * 3 + "!") * 40_000,
        PEM_BEGIN + ("aB1 \r\r\n\r" * 3 + "!") * 25_000,
        PEM_BEGIN + "\r" * 1_000_000,
        PEM_BEGIN + "\\r\\r\\n" * 150_000,
        (PEM_BEGIN + "\r\r" + "aaaa\r\r\n" * 3 + "-" * 4) * 20_000,
        PEM_BEGIN + ("a" + "\r" * 1_000) * 1_000,
        # Round 8: spaces and tabs among a break's carriage returns and line
        # feed, as written and written out, short of 16, each a candidate's
        # start, long runs of them between characters and with no character,
        # and spaces before the break of every line.
        PEM_BEGIN + ("aB1\r \r \n" * 3 + "!") * 25_000,
        PEM_BEGIN + ("aB1 \t\r\n \r" * 3 + "!") * 20_000,
        (PEM_BEGIN + "\r \r" + "aaaa \r\n \r" * 3 + "-" * 4) * 15_000,
        PEM_BEGIN + ("a" + " \r" * 500) * 1_000,
        PEM_BEGIN + " \r\t" * 330_000,
        PEM_BEGIN + "\\r \\t\\n " * 100_000,
        PEM_BEGIN + ("aB1" + " " * 1_000 + "\r") * 1_000,
    ],
    ids=[
        "begin-lines",
        "long-body",
        "short-runs",
        "short-runs-per-begin",
        "no-key-material",
        "after-escapes",
        "dashes",
        "backslashes",
        "near-end-lines",
        "short-wrapped-runs",
        "written-out-wrapped-runs-per-begin",
        "one-character-lines",
        "written-out-breaks",
        "short-wrapped-near-end-lines",
        "spaced-no-upper-case",
        "tabbed-no-digit",
        "spaced-short-of-16",
        "spaced-near-end-lines",
        "spaced-digit-at-the-end",
        "written-out-tabs",
        "carriage-returns-short-of-16",
        "written-out-carriage-returns",
        "carriage-return-near-end-lines",
        "carriage-return-runs-short-of-16",
        "spaced-carriage-return-runs-short-of-16",
        "a-megabyte-of-carriage-returns",
        "written-out-carriage-return-runs",
        "carriage-return-runs-near-end-lines",
        "long-runs-between-characters",
        "spaced-breaks-short-of-16",
        "tabbed-breaks-short-of-16",
        "spaced-breaks-near-end-lines",
        "long-spaced-breaks-between-characters",
        "a-megabyte-of-spaced-breaks",
        "written-out-spaced-breaks",
        "spaces-before-each-break",
    ],
)
def test_the_private_key_pattern_stays_linear(text):
    """Each candidate starts at a BEGIN line and scans to the next run of
    five dashes at most three times, and the key-material lookahead steps
    over a short run whole, so a megabyte is redacted well inside the
    budget."""
    started = time.process_time()
    redact(text)
    assert time.process_time() - started < 3.0


# A value under a prefixed name with a credential suffix (#285), each with
# what ``redact`` makes of it. Every value is a fake.
_SUFFIX_TEXTS = [
    (SUFFIX_ASSIGNMENT, "DEEPSEEK_API_KEY=***REDACTED***"),
    (f"GEMINI_API_KEY={SUFFIX_SECRET}", "GEMINI_API_KEY=***REDACTED***"),
    # A Google-shaped value one character short is no Google key, and still
    # a value under the name.
    (f"GEMINI_API_KEY={GOOGLE_API_KEY[:-1]}", "GEMINI_API_KEY=***REDACTED***"),
    ("MY_GITHUB_TOKEN=abc123XYZ", "MY_GITHUB_TOKEN=***REDACTED***"),
    ("CI_JOB_TOKEN: x", "CI_JOB_TOKEN: ***REDACTED***"),
    ("OAUTH_CLIENT_SECRET = 'p@ss!w0rd'", "OAUTH_CLIENT_SECRET = '***REDACTED***'"),
    ("DB_PASSWORD=Sup3r$ecret!", "DB_PASSWORD=***REDACTED***"),
    (f'export DEEPSEEK_API_KEY="{SUFFIX_SECRET}"', 'export DEEPSEEK_API_KEY="***REDACTED***"'),
    (
        f"HOME=/home/u\nGEMINI_API_KEY={SUFFIX_SECRET}\nGOOGLE_API_KEY={SUFFIX_SECRET}\n",
        "HOME=/home/u\nGEMINI_API_KEY=***REDACTED***\nGOOGLE_API_KEY=***REDACTED***\n",
    ),
    (
        json.dumps({"env": {"GEMINI_API_KEY": SUFFIX_SECRET}}),
        json.dumps({"env": {"GEMINI_API_KEY": "***REDACTED***"}}),
    ),
    (
        json.dumps(f'GEMINI_API_KEY="{SUFFIX_SECRET}"'),
        json.dumps('GEMINI_API_KEY="***REDACTED***"'),
    ),
    (f"GEMINI_API_KEY:\n  {SUFFIX_SECRET}", "GEMINI_API_KEY:\n  ***REDACTED***"),
    (f"\x1b[0mGEMINI_API_KEY={SUFFIX_SECRET}", "\x1b[0mGEMINI_API_KEY=***REDACTED***"),
    (f"line\\nGEMINI_API_KEY={SUFFIX_SECRET}", "line\\nGEMINI_API_KEY=***REDACTED***"),
]
_SUFFIX_TEXT_IDS = [
    "deepseek",
    "gemini",
    "gemini-short-google-key",
    "prefixed-known-name",
    "gitlab-ci",
    "quoted-with-space",
    "password",
    "export",
    "env-dump",
    "json-key",
    "escaped-quote",
    "yaml-next-line",
    "after-ansi",
    "after-written-out-newline",
]
# A lower- or mixed-case name (#285 review): a quoted value, or an unquoted
# word that does not read as code, as a configuration file, a form body, a
# query or a log line holds one.
_LOWER_SUFFIX_TEXTS = [
    (f"client_secret={SUFFIX_SECRET}", "client_secret=***REDACTED***"),
    (f"client_secret: {SUFFIX_SECRET}", "client_secret: ***REDACTED***"),
    ("my_GITHUB_TOKEN=abc123XYZ", "my_GITHUB_TOKEN=***REDACTED***"),
    ("Gemini_API_KEY = FAKE-0.opaque~value==", "Gemini_API_KEY = ***REDACTED***"),
    (
        f"grant_type=client_credentials&client_secret={SUFFIX_SECRET}&scope=read",
        "grant_type=client_credentials&client_secret=***REDACTED***&scope=read",
    ),
    (
        f"https://h.example/cb?api_token={SUFFIX_SECRET}&next=1",
        "https://h.example/cb?api_token=***REDACTED***&next=1",
    ),
    (f"db_password = '{SUFFIX_SECRET}'", "db_password = '***REDACTED***'"),
    ('client_secret = "correct horse; battery"', 'client_secret = "***REDACTED***"'),
    ("Client(api_token='x')", "Client(api_token='***REDACTED***')"),
    (
        json.dumps({"installed": {"client_id": "1", "client_secret": "two words"}}),
        json.dumps({"installed": {"client_id": "1", "client_secret": "***REDACTED***"}}),
    ),
    ("{'db_password': 'p w'}", "{'db_password': '***REDACTED***'}"),
    (
        f'client_secret = "{SUFFIX_SECRET}"  # rotated',
        'client_secret = "***REDACTED***"  # rotated',
    ),
    (f"\x1b[0mclient_secret={SUFFIX_SECRET}", "\x1b[0mclient_secret=***REDACTED***"),
    (f"line\\nclient_secret={SUFFIX_SECRET}", "line\\nclient_secret=***REDACTED***"),
    # Any word that does not read as code (#285 review, round 2): letters
    # alone, at any length, punctuation of every kind, a ``#`` and an ``&``
    # among it, and a form body ``urlencode`` wrote, its ``%`` escapes and
    # ``+`` spaces in the value. What ends the word stays: a space and what
    # follows it, the form's next field, the punctuation after a word.
    ("client_secret: FAKEalphaword", "client_secret: ***REDACTED***"),
    ("db_password: FAKEwords  # rotated", "db_password: ***REDACTED***  # rotated"),
    ("Gemini_API_KEY=abc", "Gemini_API_KEY=***REDACTED***"),
    ("x_token = v2_1", "x_token = ***REDACTED***"),
    ("x_token=abcdefgh", "x_token=***REDACTED***"),
    (f"db_password: {PUNCTUATION_SECRET}", "db_password: ***REDACTED***"),
    (f"db_password={PUNCTUATION_SECRET}\nnext=1", "db_password=***REDACTED***\nnext=1"),
    ("db_password=Sup3r$ecret!", "db_password=***REDACTED***"),
    ("db_password: p,ss)w.rd:x", "db_password: ***REDACTED***"),
    ("db_password: FAKE&words", "db_password: ***REDACTED***"),
    (
        urlencode({"grant_type": "client_credentials", "client_secret": FORM_SECRET, "x": "1"}),
        "grant_type=client_credentials&client_secret=***REDACTED***&x=1",
    ),
    ("client_secret=%2FFAKE%3D%3D&scope=read", "client_secret=***REDACTED***&scope=read"),
    ("Then db_password: FAKEword.", "Then db_password: ***REDACTED***."),
    ("connect(db_password=pw), retry", "connect(db_password=***REDACTED***), retry"),
    ("`db_password=FAKEword` in .env", "`db_password=***REDACTED***` in .env"),
    ("x_token=x", "x_token=***REDACTED***"),
    ("\x1b[0mdb_password=FAKEword", "\x1b[0mdb_password=***REDACTED***"),
]
_LOWER_SUFFIX_TEXT_IDS = [
    "ini",
    "yaml",
    "mixed-case",
    "mixed-case-alphabet",
    "form-body",
    "query",
    "single-quoted",
    "double-quoted-with-space",
    "keyword-argument",
    "oauth-client-json",
    "python-repr",
    "comment-after",
    "after-ansi",
    "after-written-out-newline",
    "yaml-letters",
    "yaml-comment-after",
    "short-letters",
    "identifier",
    "letters",
    "yaml-punctuation",
    "env-file-punctuation",
    "dollar-and-bang",
    "trailing-punctuation-inside",
    "ampersand-inside",
    "urlencode",
    "percent-escapes",
    "full-stop-after",
    "argument",
    "code-span",
    "one-character",
    "punctuation-after-ansi",
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _SUFFIX_TEXTS + _LOWER_SUFFIX_TEXTS,
    ids=_SUFFIX_TEXT_IDS + _LOWER_SUFFIX_TEXT_IDS,
)
def test_a_value_under_a_prefixed_credential_name_is_redacted(text, expected):
    """#285: ``GEMINI_API_KEY`` and ``DEEPSEEK_API_KEY`` are no well-known
    name, and ``_`` before ``API_KEY`` kept ``secret-assignment`` from
    seeing one, so a value of no known shape under such a name (an OpenCode
    launch inherits ``GEMINI_*`` and ``GOOGLE_*``) was printed in the clear.
    A name ending in ``_API_KEY``, ``_TOKEN``, ``_SECRET`` or ``_PASSWORD``
    is recognised: in upper case as a well-known one is, any value, in any
    quoting the named patterns take; in lower or mixed case (#285 review,
    ``client_secret``) with a quoted value or an unquoted word that does not
    read as code."""
    assert redact(text) == expected
    assert credential_classes(text) == ("credential-suffix-assignment",)
    assert redact(expected) == expected


@pytest.mark.parametrize(
    ("text", "body"), [c[1:] for c in WHOLE_VALUES_285], ids=[c[0] for c in WHOLE_VALUES_285]
)
def test_a_value_the_second_285_review_found_is_redacted_whole_as_text_and_as_json(text, body):
    """#285 review, round 2: a lower-case name's unquoted word of letters
    alone, of punctuation or form-encoded was kept whole, and a quoted value
    that spans lines or holds a doubled quote kept what followed the line
    break or the quote. Each value goes whole, as text and as JSON writes it
    (an agent's result on stdout, a record), and the JSON still decodes."""
    for depth in (0, 1, 2):
        escaped = _json_string(text, depth)
        out = redact(escaped)
        assert body not in out and "FAKE" not in out, out
        assert credential_classes(escaped) == ("credential-suffix-assignment",)
        assert redact(out) == out
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert "***REDACTED***" in decoded and "FAKE" not in decoded
    assert redact_obj({"env": {"text": text}}) == {"env": {"text": redact(text)}}


@pytest.mark.parametrize(
    ("text", "expected", "name"),
    [
        (f"GEMINI_API_KEY={GOOGLE_API_KEY}", "GEMINI_API_KEY=***REDACTED***", "google-api-key"),
        (f"MY_TOKEN={GROQ_API_KEY}", "MY_TOKEN=***REDACTED***", "groq-api-key"),
        (f"NPM_TOKEN={NPM_TOKEN}", "NPM_TOKEN=***REDACTED***", "npm-token"),
        (
            "GH_ENTERPRISE_TOKEN=gh" + "p_" + "0" * 36,
            "GH_ENTERPRISE_TOKEN=***REDACTED***",
            "github-token",
        ),
        (f'DEPLOY_SECRET="{PRIVATE_KEY}"', 'DEPLOY_SECRET="***REDACTED***"', "private-key"),
        # A well-known name is ``env-assignment``'s.
        (f"OPENAI_API_KEY={SUFFIX_SECRET}", "OPENAI_API_KEY=***REDACTED***", "env-assignment"),
    ],
    ids=["google", "groq", "npm", "github", "private-key", "well-known-name"],
)
def test_a_value_of_a_known_shape_under_a_prefixed_name_is_reported_by_its_shape(
    text, expected, name
):
    """The name rule runs after every token shape, so a value one of them
    knows is replaced, and reported, by that shape's class."""
    assert redact(text) == expected
    assert credential_classes(text) == (name,)


# Not a credential name: a suffix that does not end the name, a plural, no
# separator, no value, a suffix the rule does not take (``_KEY``), and a
# suffix with no prefix (the generic ``secret-assignment``'s, which wants a
# value of 12 characters).
_SUFFIX_NEAR_MISSES = [
    "MY_TOKEN_FILE=/run/secrets/token",
    "GITHUB_TOKEN_PATH: /x",
    "MAX_TOKENS=4096",
    "MY_TOKEN is set in the environment",
    "MY_TOKEN=",
    '{"MY_TOKEN": "", "next": 1}',
    "SSH_KEY=/home/u/.ssh/id_ed25519",
    "TOKEN=short",
    "API_KEY: short",
    "_TOKEN=x",
]
# A lower- or mixed-case name in code (#285 review): its unquoted value is
# an expression, not a literal: the same name, a dotted name, a type, a
# constant, a reference, a call, a subscript, a number; and a value on the
# next line is a statement, not the name's.
_LOWER_SUFFIX_NEAR_MISSES = [
    "max_token=5",
    "def fetch(api_token: str, page_token=None):",
    "api_token=api_token",
    "self.api_token = api_token",
    "client_secret=self.client_secret,",
    "page_token: Optional[str] = None",
    "client_secret: ${CLIENT_SECRET}",
    "api_token=$API_TOKEN",
    "x_token = sha256(data)",
    "x_token=hashlib.sha256(data).hexdigest()",
    'api_token = os.environ["API_TOKEN"]',
    "auth_token=base64.b64encode(raw)",
    "if api_token:\n    headers2 = build(1)",
    "if not client_secret:\n    raise ConfigError2",
    "num_token = 12345678",
    '{"page_token": null, "client_secret": ""}',
    "api_token=<value>",
    "client_secret = '' if x else None",
    # A word that reads as code (#285 review, round 2), now that a word of
    # any characters is a literal: a name that names a credential, alone,
    # dotted or in another case, a dotted name, a call, a subscript, a
    # generic, a type, a constant, a keyword, a reference, a placeholder, a
    # number and a word of no letter or digit; and the marker itself.
    "f(api_token=api_token), g(client_secret=CLIENT_SECRET)",
    "api_token=accessToken",
    "api_token = self.v2_token",
    "x_token=args.tok",
    "x_token: config.auth.tok,",
    "api_token = await fetch_token()",
    "api_token: Option<String>,",
    "api_token = Token::new(x)",
    "api_token: str | None = None",
    "client_secret: SecretStr",
    "use_token: true",
    "if not api_token: return None",
    "api_token := os.Getenv(name)",
    "if api_token == expected:",
    "url = f'https://h.example/?api_token={api_token}'",
    'body = "api_token=%s" % tok',
    "db_password: {{ vault_db_password }}",
    "client_secret: [REDACTED]",
    "set db_password=%DB_PASSWORD%",
    "client_secret: |",
    "x_token = -1.5",
    "api_token: ...",
    "client_secret=***REDACTED***",
]


@pytest.mark.parametrize("text", _SUFFIX_NEAR_MISSES + _LOWER_SUFFIX_NEAR_MISSES)
def test_a_near_miss_of_a_prefixed_credential_name_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


# A quoted value under a credential name (#285 review), each with what
# ``redact`` makes of it: the whole value goes, its spaces, semicolons, the
# other quote and an escaped quote of its own included, and the quotes
# around it stay.
_WHOLE_QUOTED_VALUES = [
    ('GEMINI_API_KEY="abc def;ghi"', 'GEMINI_API_KEY="***REDACTED***"'),
    (
        "export DB_PASSWORD='correct horse; battery' && run",
        "export DB_PASSWORD='***REDACTED***' && run",
    ),
    ('X_TOKEN="it\'s; here" next', 'X_TOKEN="***REDACTED***" next'),
    ("X_TOKEN='say \"hi\" now' next", "X_TOKEN='***REDACTED***' next"),
    ('export X_TOKEN="a\\"b c" tail', 'export X_TOKEN="***REDACTED***" tail'),
    ('X_TOKEN: "a b"\nnext: 1', 'X_TOKEN: "***REDACTED***"\nnext: 1'),
    ('X_TOKEN = "a b"  # TOML', 'X_TOKEN = "***REDACTED***"  # TOML'),
    ('GITHUB_TOKEN="a b; c"', 'GITHUB_TOKEN="***REDACTED***"'),
    ('"GH_TOKEN": "a b"', '"GH_TOKEN": "***REDACTED***"'),
    ('client_secret = "a b; c"', 'client_secret = "***REDACTED***"'),
    ('X_TOKEN="C:\\Program Files\\x" end', 'X_TOKEN="***REDACTED***" end'),
    # Unterminated: the value runs to the end of its line, or of the string
    # it stands in, and no further.
    ('X_TOKEN="cut short here\nnext line', 'X_TOKEN="***REDACTED***\nnext line'),
    ('{"cmd": "X_TOKEN=\\"cut short"}', '{"cmd": "X_TOKEN=\\"***REDACTED***"}'),
    # Across line breaks to the closing quote (#285 review, round 2), as a
    # shell string and a YAML flow scalar span lines, a CRLF one too, and
    # the line after it stays.
    ('export DB_PASSWORD="FAKEfirst\nFAKEsecond"\nrun', 'export DB_PASSWORD="***REDACTED***"\nrun'),
    (
        'client_secret: "FAKEfirst\n  FAKEsecond\n  FAKEthird"\nnext: 1',
        'client_secret: "***REDACTED***"\nnext: 1',
    ),
    ("db_password: 'FAKEfirst\n  FAKEsecond'\n", "db_password: '***REDACTED***'\n"),
    ('X_TOKEN="FAKEfirst\r\nFAKEsecond" tail', 'X_TOKEN="***REDACTED***" tail'),
    ('GITHUB_TOKEN="FAKEfirst\nFAKE \\"second\\""', 'GITHUB_TOKEN="***REDACTED***"'),
    # A doubled quote is one inside the value, as YAML and SQL escape it and
    # a shell joins two strings, on one line or across lines.
    ("db_password: 'FAKE''FAKEafter'", "db_password: '***REDACTED***'"),
    ("X_TOKEN='FAKE''FAKEafter' next", "X_TOKEN='***REDACTED***' next"),
    ("db_password: 'it''s\n  FAKEafter'", "db_password: '***REDACTED***'"),
    ('X_TOKEN="FAKE""FAKEafter"', 'X_TOKEN="***REDACTED***"'),
    ("X_TOKEN='FAKE'''", "X_TOKEN='***REDACTED***'"),
]


@pytest.mark.parametrize(("text", "expected"), _WHOLE_QUOTED_VALUES)
def test_a_quoted_value_is_redacted_whole(text, expected):
    """#285 review: a quoted value stopped at its first space or semicolon,
    and the rest of it stayed in the clear; round 2: and at a line break and
    a doubled quote, both of which a valid shell or YAML string holds. It
    runs to its closing quote."""
    assert redact(text) == expected
    assert redact(expected) == expected
    assert credential_classes(text) != ()
    assert credential_classes(expected) == ()
    assert len(expected) <= MAX_GROWTH_FACTOR * len(text)


# A quote that opens no value, or a value that does not read as one string
# across its lines (#285 review, round 2), each with what ``redact`` makes of
# it: the value is the rest of its line, as an unterminated one's was, and
# the lines after it stay. The next quote of the kind is followed by a word
# or a ``*`` (it opens the next string), or comes after a code fence; or the
# quote opens no value at all (round 3): the name stands right after a quote
# or a backtick, which starts a string literal or a code span that the quote
# after its separator closes, or the quote ends its line.
_QUOTES_THAT_SPAN_NO_LINES = [
    (
        'X_TOKEN="FAKEcut\n\nA paragraph says "this".',
        'X_TOKEN="***REDACTED***\n\nA paragraph says "this".',
    ),
    (
        'X_TOKEN="FAKEcut\n```\nsay "x"\n```',
        'X_TOKEN="***REDACTED***\n```\nsay "x"\n```',
    ),
    (
        'Set `DEPLOY_SECRET="...` first;\nthen "run" it.',
        'Set `DEPLOY_SECRET="***REDACTED***\nthen "run" it.',
    ),
    (
        "print('GH_TOKEN=' + read(\"f\"))\nlog('done')",
        "print('GH_TOKEN='***REDACTED***\nlog('done')",
    ),
    # Nothing on its own line: no value at all.
    (
        'field = "Set GITHUB_TOKEN="\nassert check("body", field)',
        'field = "Set GITHUB_TOKEN="\nassert check("body", field)',
    ),
    # Round 3: a blank line, the other quote and a backtick no longer stop a
    # value, so each of these reaches a quote of its kind that could close
    # it; what tells the quote that opens no value is where it stands.
    (
        "print('GH_TOKEN=' + read(\"f\"))\nsep = ', '",
        "print('GH_TOKEN='***REDACTED***\nsep = ', '",
    ),
    (
        "msg = 'X_TOKEN=' + v\n\nlog('%s done', name)",
        "msg = 'X_TOKEN='***REDACTED***\n\nlog('%s done', name)",
    ),
    (
        'Set `DEPLOY_SECRET="...` first;\n\nthen run "$HOME/x".',
        'Set `DEPLOY_SECRET="***REDACTED***\n\nthen run "$HOME/x".',
    ),
    (
        'field = "Set GITHUB_TOKEN="\nx = \'a\'\n\ny = "/tmp"',
        'field = "Set GITHUB_TOKEN="\nx = \'a\'\n\ny = "/tmp"',
    ),
    (
        'X_TOKEN="FAKEcut\nand `x` said "*bold*" here',
        'X_TOKEN="***REDACTED***\nand `x` said "*bold*" here',
    ),
]


@pytest.mark.parametrize(("text", "expected"), _QUOTES_THAT_SPAN_NO_LINES)
def test_a_quote_that_opens_no_value_takes_no_later_line(text, expected):
    """A quoted value runs across lines only while what it spans reads as one
    string, so the end of a string literal in code, or a quote in a Markdown
    code span, is no value that takes the lines after it from a log, a
    prompt or an agent's read context. What it takes is its own line, as an
    unterminated value did."""
    assert redact(text) == expected
    assert redact(expected) == expected


_SPACED_VALUE = "FAKE spaced; value \"x\" 'y'"
_SPACED_ASSIGNMENTS = [
    ("env-assignment", "GH_TOKEN={q}{v}{q}"),
    ("env-assignment", '"GITHUB_TOKEN": {q}{v}{q}'),
    ("credential-suffix-assignment", "GEMINI_API_KEY={q}{v}{q}"),
    ("credential-suffix-assignment", '"DB_PASSWORD": {q}{v}{q}'),
    ("credential-suffix-assignment", "client_secret = {q}{v}{q}"),
]


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
@pytest.mark.parametrize("depth", [0, 1, 2, 3], ids=["text", "json", "json-in-json", "three-deep"])
@pytest.mark.parametrize(
    ("name", "template"), _SPACED_ASSIGNMENTS, ids=[t for _, t in _SPACED_ASSIGNMENTS]
)
def test_a_quoted_value_with_spaces_is_redacted_whole_at_any_escaping(name, template, depth, quote):
    """The same whole value as shell-style text and as JSON, JSON in JSON and
    three deep write it: the value's own quotes, escaped one level deeper
    than its closing quote, stay inside it, and the redacted text decodes to
    what redacting the text itself gives."""
    inner = _SPACED_VALUE.replace(quote, "\\" + quote)
    text = template.format(q=quote, v=inner)
    assert redact(text) == template.format(q=quote, v="***REDACTED***")
    escaped = _json_string(text, depth)
    out = redact(escaped)
    for word in ("FAKE", "spaced", "value"):
        assert word not in out
    decoded = out
    for _ in range(depth):
        decoded = json.loads(decoded)
    assert decoded == redact(text)
    assert credential_classes(escaped) == (name,)
    assert redact(out) == out
    assert len(out) <= MAX_GROWTH_FACTOR * len(escaped)


# Quoted values the second review of #285 found cut short: one that spans
# lines, one that holds the quote doubled, and both, ``{q}`` the quote.
_SPANNING_VALUES = {
    "lines": "FAKEfirst\nFAKE second",
    "doubled": "FAKE{q}{q}FAKEafter",
    "doubled-on-lines": "FAKE{q}{q}s\n  FAKEafter",
}


@pytest.mark.parametrize("value", sorted(_SPANNING_VALUES), ids=str)
@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
@pytest.mark.parametrize("depth", [0, 1, 2, 3], ids=["text", "json", "json-in-json", "three-deep"])
@pytest.mark.parametrize(
    ("name", "template"), _SPACED_ASSIGNMENTS, ids=[t for _, t in _SPACED_ASSIGNMENTS]
)
def test_a_quoted_value_across_lines_or_a_doubled_quote_is_redacted_whole_at_any_escaping(
    name, template, depth, quote, value
):
    """#285 review, round 2: a quoted value stopped at a line break and at a
    doubled quote, and what followed it stayed in the clear. It is taken
    whole as text and as JSON, JSON in JSON and three deep write it, where
    the line break is written out and the doubled quote escaped, and the
    redacted text decodes to what redacting the text itself gives."""
    text = template.format(q=quote, v=_SPANNING_VALUES[value].format(q=quote))
    assert redact(text) == template.format(q=quote, v="***REDACTED***")
    escaped = _json_string(text, depth)
    out = redact(escaped)
    assert "FAKE" not in out
    decoded = out
    for _ in range(depth):
        decoded = json.loads(decoded)
    assert decoded == redact(text)
    assert credential_classes(escaped) == (name,)
    assert redact(out) == out
    assert len(out) <= MAX_GROWTH_FACTOR * len(escaped)


# Quoted values the third review of #285 found cut short at their first line
# (R3-F1), ``{q}`` the quote and ``{o}`` the other one: lines that hold a
# blank line, the other quote, a backtick, and all three. Each is valid in a
# shell string and a YAML flow scalar.
_SPANNING_VALUES_R3 = {
    "blank-line": "FAKEfirst\n\nFAKE second",
    "other-quote": "FAKE it{o}s\nFAKE {o}x{o} second",
    "backtick": "FAKE `cmd`\nFAKE second",
    "all": "FAKE{o}\n\n  `FAKE`\n  FAKE second",
}


@pytest.mark.parametrize("value", sorted(_SPANNING_VALUES_R3), ids=str)
@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
@pytest.mark.parametrize("depth", [0, 1, 2, 3], ids=["text", "json", "json-in-json", "three-deep"])
@pytest.mark.parametrize(
    ("name", "template"), _SPACED_ASSIGNMENTS, ids=[t for _, t in _SPACED_ASSIGNMENTS]
)
def test_a_quoted_value_whose_lines_hold_anything_is_redacted_whole_at_any_escaping(
    name, template, depth, quote, value
):
    """#285 review, round 3: a value that spanned lines was taken only while
    its lines held no blank line, no quote of the other kind and no
    backtick, all of which a valid shell or YAML string holds; it then
    stopped at its first line and the rest stayed in the clear. It is taken
    to its closing quote as text and as JSON, JSON in JSON and three deep
    write it, and the redacted text decodes to what redacting the text
    itself gives."""
    other = "'" if quote == '"' else '"'
    text = template.format(q=quote, v=_SPANNING_VALUES_R3[value].format(q=quote, o=other))
    assert redact(text) == template.format(q=quote, v="***REDACTED***")
    escaped = _json_string(text, depth)
    out = redact(escaped)
    assert "FAKE" not in out and "second" not in out
    decoded = out
    for _ in range(depth):
        decoded = json.loads(decoded)
    assert decoded == redact(text)
    assert credential_classes(escaped) == (name,)
    assert redact(out) == out
    assert len(out) <= MAX_GROWTH_FACTOR * len(escaped)


@pytest.mark.parametrize(
    ("text", "body"),
    [c[1:] for c in WHOLE_VALUES_285_R3],
    ids=[c[0] for c in WHOLE_VALUES_285_R3],
)
def test_a_value_the_third_285_review_found_is_redacted_whole_as_text_and_as_json(text, body):
    """#285 review, round 3: a quoted value whose lines held a blank line,
    the other quote or a backtick kept every line after its first, and a
    value under a lower-case credential name on the lines below it (a YAML
    plain or quoted scalar, a literal or a folded block) was kept whole. Each
    goes whole, as text and as JSON, JSON in JSON and three deep write it,
    and the JSON still decodes to what redacting the text gives."""
    name = next(name for name, case, _ in WHOLE_VALUES_285_R3 if case == text)
    assert redact(text) == WHOLE_VALUES_285_R3_REDACTED[name]
    for depth in (0, 1, 2, 3):
        escaped = _json_string(text, depth)
        out = redact(escaped)
        assert body not in out and "FAKE" not in out, out
        assert credential_classes(escaped) == ("credential-suffix-assignment",)
        assert redact(out) == out and credential_classes(out) == ()
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == WHOLE_VALUES_285_R3_REDACTED[name]
    assert redact_obj({"env": {"text": text}}) == {"env": {"text": redact(text)}}


# A credential name's value on the lines below it (#285 review, round 3),
# each with what ``redact`` makes of it, beyond the shared cases: a CRLF
# file, a comment after the key and after the value, the line after the
# value, a sequence entry, a quoted key, a block's indicators, an Ansible
# vault's tag, blank lines inside a block and after it, a deeper line in a
# block, an upper-case and a well-known name whose next line holds several
# words or a block, after OpenCode's ANSI escape and after a line break
# written out.
_YAML_VALUES = [
    ("client_secret:\r\n  FAKEcrlf\r\nnext: 1", "client_secret:\r\n  ***REDACTED***\r\nnext: 1"),
    (
        "client_secret: # rotated\n  FAKEvalue # old\nnext: 1",
        "client_secret: # rotated\n  ***REDACTED*** # old\nnext: 1",
    ),
    (
        "app:\n  api_token:\n    FAKEnested\n  next: 1",
        "app:\n  api_token:\n    ***REDACTED***\n  next: 1",
    ),
    ('"client_secret":\n  "FAKE"', '"client_secret":\n  "***REDACTED***"'),
    (
        "db_password: |-\n  FAKE1\n  FAKE2\n\nnext: 1",
        "db_password: |-\n  ***REDACTED***\n\nnext: 1",
    ),
    ("db_password: >2\n   FAKE1\n    FAKEdeeper\n", "db_password: >2\n   ***REDACTED***\n"),
    ("db_password: |\n\n  FAKEafterblank\n", "db_password: |\n\n  ***REDACTED***\n"),
    (
        "db_password: !vault |\n  FAKEcipher0\n  FAKEcipher1",
        "db_password: !vault |\n  ***REDACTED***",
    ),
    ("db_password: | # pinned\n  FAKE", "db_password: | # pinned\n  ***REDACTED***"),
    ("GEMINI_API_KEY:\n  FAKE two words", "GEMINI_API_KEY:\n  ***REDACTED***"),
    ("GEMINI_API_KEY: >\n  FAKE1\n  FAKE2", "GEMINI_API_KEY: >\n  ***REDACTED***"),
    ("GITHUB_TOKEN: |\n  FAKE1\n  FAKE2\nx: 1", "GITHUB_TOKEN: |\n  ***REDACTED***\nx: 1"),
    ("AWS_SECRET_ACCESS_KEY: |\n  FAKE1", "AWS_SECRET_ACCESS_KEY: |\n  ***REDACTED***"),
    ("refresh_token: |\n  FAKE1\n  FAKE2", "refresh_token: |\n  ***REDACTED***"),
    ("\x1b[0mclient_secret: |\n  FAKE", "\x1b[0mclient_secret: |\n  ***REDACTED***"),
    ("x\\nclient_secret:\\n  FAKE\\nnext: 1", "x\\nclient_secret:\\n  ***REDACTED***\\nnext: 1"),
]


@pytest.mark.parametrize(("text", "expected"), _YAML_VALUES, ids=[t for t, _ in _YAML_VALUES])
def test_a_credential_names_value_on_the_lines_below_it_is_redacted(text, expected):
    """#285 review, round 3: a value on the lines below its key, as YAML
    writes one, is replaced whole; the key, its comment, a block's header,
    the value's indentation and the lines after it stay."""
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)


# What stays (#285 review, round 3): a key with no value below it, or one
# that is no YAML key, and a value that reads as code. A statement that ends
# in a colon (``if api_token:``) is no key, since a key starts its line; a
# more indented line that is a mapping, a sequence, a comment or a fence is
# another node, never the value; and a lower-case name's value that reads as
# code (a reference, a placeholder, a dotted name, a call, a type, a
# constant) is none, as on the key's line.
_YAML_NEAR_MISSES = [
    "elif client_secret:\n    use_it(1)",
    "while api_token:\n    api_token = refresh()",
    "for x_token in tokens:\n    use(x_token)",
    "class Config:\n    api_token: str\n    db_password: SecretStr",
    "def f(\n    api_token:\n        str,\n):",
    "const cfg = {\n  client_secret:\n    process.env.CLIENT_SECRET,\n"
    "  api_token:\n    getToken(),\n};",
    "client_secret:\n  valueFrom:\n    secretKeyRef:\n      name: app",
    "client_secret:\n  - a\n  - b",
    "client_secret:\n  # set by CI\nnext: 1",
    "client_secret:\nnext: 1",
    "client_secret: |\nnext: 1",
    "client_secret:\n  ${{ secrets.CLIENT_SECRET }}",
    "db_password: |\n  {{ vault_db_password }}",
    "env:\n  api_token:\n    $API_TOKEN",
    "- `client_secret`:\n  the client's secret",
    "Set client_secret:\n  see below",
    "api_token:\n  None",
    "api_token:\n  ~",
    "client_secret:\n  [REDACTED]",
    "client_secret:\n  ```\n  x\n  ```",
    "client_secret:\n  ***REDACTED***",
    "client_secret: |\n  ***REDACTED***",
]


@pytest.mark.parametrize("text", _YAML_NEAR_MISSES)
def test_a_line_below_a_credential_name_that_is_no_value_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


@pytest.mark.parametrize(
    ("text", "body"),
    [c[1:] for c in WHOLE_VALUES_285_R4],
    ids=[c[0] for c in WHOLE_VALUES_285_R4],
)
def test_a_value_the_fourth_285_review_found_is_redacted_whole_as_text_and_as_json(text, body):
    """#285 review, round 4: a value below its YAML key after a blank line or
    a comment line, a plain scalar with a blank line among its lines or that
    starts on its key's line (R4-F1), a quoted value whose opening quote ends
    its line (R4-F2), and a block or a plain scalar of several lines that
    starts with a bracket or a number (R4-F3) were kept whole or in part.
    Each goes whole, as text and as JSON, JSON in JSON and three deep write
    it, the JSON still decodes to what redacting the text gives, and the
    output is stable and within the growth bound."""
    name = next(name for name, case, _ in WHOLE_VALUES_285_R4 if case == text)
    assert redact(text) == WHOLE_VALUES_285_R4_REDACTED[name]
    for depth in (0, 1, 2, 3):
        escaped = _json_string(text, depth)
        out = redact(escaped)
        assert body not in out and "FAKE" not in out, out
        assert credential_classes(escaped) == ("credential-suffix-assignment",)
        assert redact(out) == out and credential_classes(out) == ()
        assert len(out) <= MAX_GROWTH_FACTOR * len(escaped)
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == WHOLE_VALUES_285_R4_REDACTED[name]
    assert redact_obj({"env": {"text": text}}) == {"env": {"text": redact(text)}}


# Values below a YAML key past blank and comment lines, plain scalars of
# several lines and blocks (#285 review, round 4, R4-F1 and R4-F3), beyond
# the shared cases, each with what ``redact`` makes of it: under an
# upper-case, a well-known and a mixed-case name, every suffix, a comment at
# the first column and after the key, a CRLF file, a sequence entry, a
# quoted key, after OpenCode's ANSI escape and after a line break written
# out; a plain scalar that starts on its key's line, after a blank line and
# under a word the name's rule took in part; blocks that start with a JSON
# document, a bracket, a number, a word the code rule refuses on a line of
# its own, or a placeholder followed by more. An upper-case name's rule on
# its line takes any word across a line break, a comment's ``#`` too, as
# before, so under one the comment goes with the value.
_YAML_VALUES_R4 = [
    ("GEMINI_API_KEY:\n  # c\n  FAKE", "GEMINI_API_KEY:\n  ***REDACTED***"),
    ("GITHUB_TOKEN:\n\n  FAKE two\n\n  FAKE", "GITHUB_TOKEN:\n\n  ***REDACTED***"),
    ("Client_Secret:\n# c\n  FAKE", "Client_Secret:\n# c\n  ***REDACTED***"),
    (
        "app:\n  x_token:\n# c\n\n    FAKE\n  next: 1",
        "app:\n  x_token:\n# c\n\n    ***REDACTED***\n  next: 1",
    ),
    ("db_password: # old\n  # new\n  FAKE", "db_password: # old\n  # new\n  ***REDACTED***"),
    (
        "x_secret:\r\n\r\n  # c\r\n  FAKE\r\nnext: 1",
        "x_secret:\r\n\r\n  # c\r\n  ***REDACTED***\r\nnext: 1",
    ),
    ("- api_token:\n\n    FAKE", "- api_token:\n\n    ***REDACTED***"),
    ('"client_secret":\n  # c\n  "FAKE"', '"client_secret":\n  # c\n  "***REDACTED***"'),
    ("\x1b[0mapi_token:\n\n  FAKE", "\x1b[0mapi_token:\n\n  ***REDACTED***"),
    (
        "x\\napi_token:\\n  # c\\n  FAKE\\nnext: 1",
        "x\\napi_token:\\n  # c\\n  ***REDACTED***\\nnext: 1",
    ),
    (
        "api_token:\n  FAKE1\n\n\n  FAKE2 # old\nnext: 1",
        "api_token:\n  ***REDACTED*** # old\nnext: 1",
    ),
    ("client_secret: FAKE1\n\n  FAKE2\nnext: 1", "client_secret: ***REDACTED***\nnext: 1"),
    ("client_secret: FAKE one\n  FAKE two", "client_secret: ***REDACTED***"),
    ("client_secret: 12345\n  FAKE", "client_secret: ***REDACTED***"),
    ("GEMINI_API_KEY: FAKE1\n  FAKE2", "GEMINI_API_KEY: ***REDACTED***"),
    ("GH_TOKEN: FAKE1\n  FAKE2", "GH_TOKEN: ***REDACTED***"),
    ("\x1b[0mx_token: FAKE1\n  FAKE2", "\x1b[0mx_token: ***REDACTED***"),
    ("x\\nx_token: FAKE1\\n  FAKE2\\nnext: 1", "x\\nx_token: ***REDACTED***\\nnext: 1"),
    ('client_secret: |\n  {"web": "FAKE"}', "client_secret: |\n  ***REDACTED***"),
    ("client_secret: >\n  (FAKE)", "client_secret: >\n  ***REDACTED***"),
    ("client_secret: |\n  4096", "client_secret: |\n  ***REDACTED***"),
    ("client_secret: |\n  process.env.FAKE", "client_secret: |\n  ***REDACTED***"),
    ("client_secret: |\n  $X\n  FAKE", "client_secret: |\n  ***REDACTED***"),
    ("client_secret: |\n  ${X}FAKE", "client_secret: |\n  ***REDACTED***"),
]


@pytest.mark.parametrize(("text", "expected"), _YAML_VALUES_R4, ids=[t for t, _ in _YAML_VALUES_R4])
def test_a_yaml_value_past_blank_lines_comments_and_lines_is_redacted(text, expected):
    """#285 review, round 4: blank and comment lines before a value, blank
    lines among a plain scalar's lines, a plain scalar that starts on its
    key's line, and a block whatever its first line holds. The key, the
    lines before the value and the lines after it stay."""
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)


# A quoted value whose opening quote ends its line (#285 review, round 4,
# R4-F2), each with what ``redact`` makes of it: under a well-known, an
# upper-case, a lower-case and a mixed-case name, each quote, behind a
# shell's ``export``, ``declare -x`` and xtrace's ``+``, after a string that
# closes on its line, as a quoted key, after OpenCode's ANSI escape, in a
# CRLF file, holding blank lines, the other quote and backticks, and one
# whose closing quote never comes, which is its next line.
_QUOTED_FROM_NEXT_LINE = [
    ('GH_TOKEN="\nFAKE"', 'GH_TOKEN="***REDACTED***"'),
    ("GITHUB_TOKEN='\nFAKE1\nFAKE2'\nnext", "GITHUB_TOKEN='***REDACTED***'\nnext"),
    ('GEMINI_API_KEY="\n  FAKE"', 'GEMINI_API_KEY="***REDACTED***"'),
    ("x_api_key: '\n  FAKE'", "x_api_key: '***REDACTED***'"),
    ('Db_Password = "\nFAKE"', 'Db_Password = "***REDACTED***"'),
    ('export X_TOKEN="\nFAKE" && run', 'export X_TOKEN="***REDACTED***" && run'),
    ('declare -x X_SECRET="\nFAKE"', 'declare -x X_SECRET="***REDACTED***"'),
    ("+ x_token='\nFAKE'", "+ x_token='***REDACTED***'"),
    ('echo "a"; X_TOKEN="\nFAKE"', 'echo "a"; X_TOKEN="***REDACTED***"'),
    ('"client_secret": "\n  FAKE"', '"client_secret": "***REDACTED***"'),
    ('\x1b[0mGH_TOKEN="\nFAKE"', '\x1b[0mGH_TOKEN="***REDACTED***"'),
    ('X_TOKEN="\r\nFAKE"\r\nnext', 'X_TOKEN="***REDACTED***"\r\nnext'),
    ('x_token="\nFAKE it\'s\n\n`FAKE`"', 'x_token="***REDACTED***"'),
    ('x_token="\nFAKEcut', 'x_token="***REDACTED***'),
    ('x_token="\nFAKEcut\nnext', 'x_token="***REDACTED***\nnext'),
]


@pytest.mark.parametrize(
    ("text", "expected"), _QUOTED_FROM_NEXT_LINE, ids=[t for t, _ in _QUOTED_FROM_NEXT_LINE]
)
def test_a_quoted_value_whose_quote_ends_its_line_is_redacted_whole(text, expected):
    """#285 review, round 4: a value that starts with a line break was no
    value at all, so the whole of it passed every check. Where its name
    stands outside every string its line opens, it is replaced whole, its
    line break with it, and the quotes and the lines after it stay."""
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)


# What stays (#285 review, round 4): a quote that ends its line inside a
# string literal or a code span, which closes that string; a value of space
# alone; and below a YAML key, a key with nothing below its blank and
# comment lines, a block whose every line is a placeholder or a reference,
# and code on several lines, a comment line before it. A keyword or a type
# that starts a plain scalar of several lines still reads as code
# (``api_token: str = Field(`` on several lines), so such a scalar is left;
# a number there starts a string.
_R4_NEAR_MISSES = [
    'field = "Set GITHUB_TOKEN="\nother = f(" ")',
    'help = "Set GITHUB_TOKEN="\nparser.add_argument("--token")',
    "msg = 'export DB_PASSWORD='\nlog(' done')",
    'print("GH_TOKEN=\\"")\nx = " y"',
    'GH_TOKEN="\n  \n"',
    "client_secret:\n\nnext: 1",
    "client_secret:\n  # set by CI\n\nnext: 1",
    "client_secret: |\n  ${{ secrets.A }}\n  ${{ secrets.B }}",
    "client_secret: |\n  $(vault read -field=x secret/app)",
    "client_secret: >-\n  <client-secret>",
    "db_password: |\n  %DB_PASSWORD%",
    "db_password: |\n\n  ${DB_PASSWORD}\n",
    "client_secret:\n  process.env.X\n  || fallback",
    "    api_token: str = Field(\n        default=None,\n    )",
    "    api_token: Optional[str] = Field(\n        default=None)",
    "  client_secret: Secret\n    | null;",
    "api_token:\n  4096",
    "if api_token:\n\n    return",
    '{\n    "api_token":\n        # from env\n        os.environ["X"],\n}',
    "  client_secret:\n    // the secret\n    getSecret(),",
    "api_token:\n  true\n  FAKE",
]


@pytest.mark.parametrize("text", _R4_NEAR_MISSES)
def test_a_value_the_fourth_review_rules_leave_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


@pytest.mark.parametrize(
    ("text", "body"),
    [c[1:] for c in WHOLE_VALUES_285_R5],
    ids=[c[0] for c in WHOLE_VALUES_285_R5],
)
def test_a_value_the_fifth_285_review_found_is_redacted_whole_as_text_and_as_json(text, body):
    """#285 review, round 5: a quoted value whose opening quote ends its line
    and whose next line starts with the quote doubled (R5-F1), a plain
    scalar whose first line holds a double quote and that goes on below its
    key's line (R5-F2), and a scalar after an anchor or a tag (R5-F5) were
    kept whole or in part. Each goes whole, as text and as JSON, JSON in JSON
    and three deep write it, the JSON still decodes to what redacting the
    text gives, and the output is stable and within the growth bound."""
    name = next(name for name, case, _ in WHOLE_VALUES_285_R5 if case == text)
    assert redact(text) == WHOLE_VALUES_285_R5_REDACTED[name]
    for depth in (0, 1, 2, 3):
        escaped = _json_string(text, depth)
        out = redact(escaped)
        assert body not in out and "FAKE" not in out, out
        assert credential_classes(escaped) == ("credential-suffix-assignment",)
        assert redact(out) == out and credential_classes(out) == ()
        assert len(out) <= MAX_GROWTH_FACTOR * len(escaped)
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == WHOLE_VALUES_285_R5_REDACTED[name]
    assert redact_obj({"env": {"text": text}}) == {"env": {"text": redact(text)}}


# A quoted value whose opening quote ends its line and whose next line
# starts with that quote doubled (#285 review, round 5, R5-F1), each with
# what ``redact`` makes of it: under a well-known, an upper-case, a lower-case
# and a mixed-case name, each quote (a doubled double quote is a shell's two
# strings joined), indented, a doubled quote alone, going on to more lines,
# below a YAML key, after an anchor, a quote escaped with a backslash, and in
# a CRLF file.
_DOUBLED_QUOTE_AFTER_BREAK = [
    ("GH_TOKEN='\n''FAKE'", "GH_TOKEN='***REDACTED***'"),
    ('GITHUB_TOKEN="\n""FAKE"', 'GITHUB_TOKEN="***REDACTED***"'),
    ("GEMINI_API_KEY: '\n  ''FAKE'", "GEMINI_API_KEY: '***REDACTED***'"),
    ("client_secret: '\n''FAKE'", "client_secret: '***REDACTED***'"),
    ("Db_Password = '\n''FAKE it''s'", "Db_Password = '***REDACTED***'"),
    ("x_token: '\n  '''", "x_token: '***REDACTED***'"),
    ("x_token: '\n''FAKE\n  FAKE'\nnext: 1", "x_token: '***REDACTED***'\nnext: 1"),
    ("client_secret:\n  '\n  ''FAKE'", "client_secret:\n  '***REDACTED***'"),
    ("GH_TOKEN:\n  '\n  ''FAKE'", "GH_TOKEN:\n  '***REDACTED***'"),
    ("client_secret: &cs '\n''FAKE'", "client_secret: &cs '***REDACTED***'"),
    ('x_token="\n\\"FAKE"', 'x_token="***REDACTED***"'),
    ("X_SECRET='\r\n''FAKE'\r\nnext", "X_SECRET='***REDACTED***'\r\nnext"),
]


@pytest.mark.parametrize(
    ("text", "expected"), _DOUBLED_QUOTE_AFTER_BREAK, ids=[t for t, _ in _DOUBLED_QUOTE_AFTER_BREAK]
)
def test_a_value_whose_next_line_starts_with_a_doubled_quote_is_redacted_whole(text, expected):
    """#285 review, round 5: the doubled quote that starts the line after a
    value's opening quote was taken for the value's close, so the value held
    nothing and the whole of it passed every check. A doubled quote is an
    escaped one, so the value holds it and is replaced whole."""
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)


# A plain scalar whose first line holds a quote and that goes on below its
# key's line (#285 review, round 5, R5-F2), each with what ``redact`` makes
# of it: a double and a single quote, under a well-known, an upper-case and a
# mixed-case name, quotes and a backtick and a blank line, in a sequence
# entry, after OpenCode's ANSI escape, written out, after an anchor and in a
# CRLF file. A YAML text in a string that a quote opens before its key is
# read as before: that quote closes the string, so a line after it is no
# continuation, and the text around the value stays.
_QUOTE_IN_FIRST_LINE = [
    ('client_secret: FAKE"first\n  FAKE', "client_secret: ***REDACTED***"),
    ("client_secret: FAKE'first\n  FAKE", "client_secret: ***REDACTED***"),
    ('GH_TOKEN: FAKE"first\n  FAKE', "GH_TOKEN: ***REDACTED***"),
    ('GEMINI_API_KEY: FAKE"a"b\n  FAKE', "GEMINI_API_KEY: ***REDACTED***"),
    ('Db_Password: FAKE `x` "y"\n\n  FAKE\nnext: 1', "Db_Password: ***REDACTED***\nnext: 1"),
    ('- x_token: FAKE"first\n    FAKE\n  next: 1', "- x_token: ***REDACTED***\n  next: 1"),
    ('\x1b[0mx_token: FAKE"first\n  FAKE', "\x1b[0mx_token: ***REDACTED***"),
    ('x\\nx_token: FAKE\\"first\\n  FAKE\\nnext: 1', "x\\nx_token: ***REDACTED***\\nnext: 1"),
    ('client_secret: &a FAKE"first\n  FAKE', "client_secret: &a ***REDACTED***"),
    ('x_token: FAKE"first\r\n  FAKE\r\nnext: 1', "x_token: ***REDACTED***\r\nnext: 1"),
    ('[\n  "x_token: FAKE",\n  "other"\n]', '[\n  "x_token: ***REDACTED***",\n  "other"\n]'),
    ('{"a": "x_token: FAKE", "b": 1}\n  next', '{"a": "x_token: ***REDACTED***", "b": 1}\n  next'),
]


@pytest.mark.parametrize(
    ("text", "expected"), _QUOTE_IN_FIRST_LINE, ids=[t for t, _ in _QUOTE_IN_FIRST_LINE]
)
def test_a_plain_scalar_whose_first_line_holds_a_quote_is_redacted_whole(text, expected):
    """#285 review, round 5: a quote in a plain scalar is a character like
    any other, but its first line held none, so the name's rule took the
    word before the quote and the rest of the line and the lines below stayed
    in the clear. The first line's break is found first and the line read as
    the lines after it are, so the value goes whole."""
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)


# A block or a plain scalar below a key in a YAML sequence entry (#285
# review, round 5, R5-F4), each with what ``redact`` makes of it: a literal
# and a folded block, nested dashes, a dash with several spaces after it, an
# indented sequence, a plain scalar below the key and one that starts on its
# line, under a well-known name, as a quoted key, a block indented one more
# than the key, a next entry of the outer sequence, and written out. The
# sibling entry at the key's column stays.
_SEQUENCE_SIBLINGS = [
    (
        "- x_token: |\n    FAKE\n  endpoint: https://example.test\n",
        "- x_token: |\n    ***REDACTED***\n  endpoint: https://example.test\n",
    ),
    (
        "- x_token: >-\n    FAKE1\n    FAKE2\n  endpoint: e",
        "- x_token: >-\n    ***REDACTED***\n  endpoint: e",
    ),
    (
        "- - x_token: |\n      FAKE\n    endpoint: e",
        "- - x_token: |\n      ***REDACTED***\n    endpoint: e",
    ),
    (
        "-   x_token: |\n      FAKE\n    endpoint: e",
        "-   x_token: |\n      ***REDACTED***\n    endpoint: e",
    ),
    (
        "  - x_token: |\n      FAKE\n    endpoint: e",
        "  - x_token: |\n      ***REDACTED***\n    endpoint: e",
    ),
    ("- x_token:\n    FAKE\n  endpoint: e", "- x_token:\n    ***REDACTED***\n  endpoint: e"),
    ("- x_token: FAKE1\n    FAKE2\n  endpoint: e", "- x_token: ***REDACTED***\n  endpoint: e"),
    ("- GH_TOKEN: |\n    FAKE\n  endpoint: e", "- GH_TOKEN: |\n    ***REDACTED***\n  endpoint: e"),
    (
        '- "x_token": |\n    FAKE\n  endpoint: e',
        '- "x_token": |\n    ***REDACTED***\n  endpoint: e',
    ),
    ("- x_token: |\n   FAKE\n  endpoint: e", "- x_token: |\n   ***REDACTED***\n  endpoint: e"),
    (
        "- - x_token: |\n      FAKE\n  - endpoint: e",
        "- - x_token: |\n      ***REDACTED***\n  - endpoint: e",
    ),
    (
        json.dumps("- x_token: |\n    FAKE\n  endpoint: e"),
        json.dumps("- x_token: |\n    ***REDACTED***\n  endpoint: e"),
    ),
]


@pytest.mark.parametrize(
    ("text", "expected"), _SEQUENCE_SIBLINGS, ids=[t for t, _ in _SEQUENCE_SIBLINGS]
)
def test_a_value_in_a_sequence_entry_stops_at_its_siblings(text, expected):
    """#285 review, round 5: a value's lines had to be indented more than
    the whitespace before a sequence's dashes, not more than the key after
    them, so the next entry of the same mapping went with a block, the
    diagnostic lost. The key's column counts the dashes now."""
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)


# A scalar after an anchor or a tag (#285 review, round 5, R5-F5), each with
# what ``redact`` makes of it: quoted and plain on the key's line, a tag, a
# quoted value after one, both properties, a verbatim and a non-specific
# tag, under a well-known, an upper-case, an OAuth and a generic name; below
# the key, quoted and plain, a single character; properties alone on the
# key's line before a value below it, before a block, a plain scalar that
# goes on and a quote that ends its line; under a well-known name below the
# key, as a quoted key, after OpenCode's ANSI escape and in a sequence entry.
# The properties stay.
_YAML_PROPERTY_VALUES = [
    ("client_secret: &cs 'FAKE'", "client_secret: &cs '***REDACTED***'"),
    ("client_secret: &cs FAKE", "client_secret: &cs ***REDACTED***"),
    ("client_secret: !!str FAKE", "client_secret: !!str ***REDACTED***"),
    ("client_secret: !!str 'FAKE two'", "client_secret: !!str '***REDACTED***'"),
    ("client_secret: &a !!str FAKE", "client_secret: &a !!str ***REDACTED***"),
    (
        "client_secret: !<tag:yaml.org,2002:str> FAKE",
        "client_secret: !<tag:yaml.org,2002:str> ***REDACTED***",
    ),
    ("client_secret: ! FAKE", "client_secret: ! ***REDACTED***"),
    ("GH_TOKEN: &t 'FAKE'", "GH_TOKEN: &t '***REDACTED***'"),
    ("GEMINI_API_KEY: !!str FAKE", "GEMINI_API_KEY: !!str ***REDACTED***"),
    ("refresh_token: !!str FAKE", "refresh_token: !!str ***REDACTED***"),
    ("password: &p FAKE0opaque0value", "password: &p ***REDACTED***"),
    ("client_secret:\n  &cs 'FAKE'", "client_secret:\n  &cs '***REDACTED***'"),
    ("client_secret:\n  !!str FAKE", "client_secret:\n  !!str ***REDACTED***"),
    ("client_secret:\n  &anchor x", "client_secret:\n  &anchor ***REDACTED***"),
    ("client_secret: &a\n  FAKE", "client_secret: &a\n  ***REDACTED***"),
    ("client_secret: &a |\n  FAKE", "client_secret: &a |\n  ***REDACTED***"),
    ("client_secret: !!binary |\n  FAKE", "client_secret: !!binary |\n  ***REDACTED***"),
    ("client_secret: &a FAKE1\n  FAKE2", "client_secret: &a ***REDACTED***"),
    ("client_secret: &a '\n  FAKE'", "client_secret: &a '***REDACTED***'"),
    ("GH_TOKEN:\n  &t 'FAKE'", "GH_TOKEN:\n  &t '***REDACTED***'"),
    ('"client_secret": &cs "FAKE"', '"client_secret": &cs "***REDACTED***"'),
    ("\x1b[0mclient_secret: !!str FAKE", "\x1b[0mclient_secret: !!str ***REDACTED***"),
    ("- api_token: &t FAKE", "- api_token: &t ***REDACTED***"),
]


@pytest.mark.parametrize(
    ("text", "expected"), _YAML_PROPERTY_VALUES, ids=[t for t, _ in _YAML_PROPERTY_VALUES]
)
def test_a_scalar_after_an_anchor_or_a_tag_is_redacted_whole(text, expected):
    """#285 review, round 5: an anchor and a tag are properties of the
    scalar after them, but they were taken for another node, so the value
    after them passed every check, or, the tag taken for the value, stayed
    after its marker. The properties stay and the scalar is replaced."""
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)


# What stays (#285 review, round 5): a doubled quote that starts no value (a
# string that closes its line, then code on the next), a quote alone after
# the break; a placeholder block or scalar in a sequence entry with a sibling
# after it; a plain scalar of several lines whose first word is code, a quote
# after it; and after an anchor or a tag, a nested mapping or sequence, a
# reference to another file's entry or to the environment (Home Assistant's
# ``!secret``, a loader's ``!ENV``), a placeholder, and a Rust reference type,
# whose ``&`` is no anchor.
_R5_NEAR_MISSES = [
    "msg = 'export DB_PASSWORD='\n''.join(parts)",
    "x_token = '\n'",
    "GH_TOKEN='\n'\n'next'",
    "- x_token: |\n    ${{ secrets.X }}\n  endpoint: e",
    "- - x_token: >-\n      <x-token>\n    endpoint: e",
    "- x_token:\n    $X_TOKEN\n  endpoint: e",
    'api_token: str = f"{x}"\n  more',
    "client_secret: &defaults\n  key: value",
    "client_secret:\n  &a key: value",
    "client_secret: &cs\n  - a\n  - b",
    "client_secret: !secret app_client_secret",
    "client_secret:\n  !secret app_client_secret",
    "db_password: !env_var DB_PASSWORD",
    "api_token: !ENV ${API_TOKEN}",
    "client_secret: !!str ${{ secrets.X }}",
    "fn f(api_token: &str) -> bool",
    "fn f(api_token: &'a str, x: i32)",
    "fn f(api_token: &mut String)",
]


@pytest.mark.parametrize("text", _R5_NEAR_MISSES)
def test_a_value_the_fifth_review_rules_leave_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


@pytest.mark.parametrize(
    ("text", "body"),
    [c[1:] for c in WHOLE_VALUES_285_R6],
    ids=[c[0] for c in WHOLE_VALUES_285_R6],
)
def test_a_value_the_sixth_285_review_found_is_redacted_whole_as_text_and_as_json(text, body):
    """#285 review, round 6: a value's properties on lines of their own
    below its key, and a block's header on a line of its own below it, were
    read by no pattern, so the value, valid YAML, was kept whole. Each goes
    whole, as text and as JSON, JSON in JSON and three deep write it, the
    JSON still decodes to what redacting the text gives, and the output is
    stable and within the growth bound."""
    name = next(name for name, case, _ in WHOLE_VALUES_285_R6 if case == text)
    assert redact(text) == WHOLE_VALUES_285_R6_REDACTED[name]
    for depth in (0, 1, 2, 3):
        escaped = _json_string(text, depth)
        out = redact(escaped)
        assert body not in out and "FAKE" not in out, out
        assert credential_classes(escaped) == ("credential-suffix-assignment",)
        assert redact(out) == out and credential_classes(out) == ()
        assert len(out) <= MAX_GROWTH_FACTOR * len(escaped)
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == WHOLE_VALUES_285_R6_REDACTED[name]
    assert redact_obj({"env": {"text": text}}) == {"env": {"text": redact(text)}}


# A value whose properties or block header stand on lines of their own
# below its key (#285 review, round 6, R6-F1), each with what ``redact``
# makes of it: an anchor alone, then a quoted scalar of either quote, a
# plain one and one that opens with its quote at its line's end; the anchor
# and a tag on one line below the key, then the scalar on the next; a
# comment after the anchor and blank and comment lines below it; a tag on
# the key's line and an anchor alone below it; a literal and a folded
# header alone below the key, with chomping and indentation indicators,
# a comment, a comment line above it and a blank line below it; a header
# after an anchor on its line, below an anchor alone on its line and below
# an anchor on the key's line; Ansible's ``!vault`` alone above its header;
# in a sequence entry with a sibling after it; after OpenCode's ANSI escape;
# as a quoted key; in a CRLF file.
_YAML_PROPERTY_AND_HEADER_LINES = [
    ('client_secret:\n  &cs\n  "FAKE"', 'client_secret:\n  &cs\n  "***REDACTED***"'),
    ("client_secret:\n  &cs\n  FAKE", "client_secret:\n  &cs\n  ***REDACTED***"),
    ("client_secret:\n  &cs\n  '\n  FAKE'", "client_secret:\n  &cs\n  '***REDACTED***'"),
    ("client_secret:\n  &cs !!str\n  FAKE", "client_secret:\n  &cs !!str\n  ***REDACTED***"),
    (
        "client_secret:\n  &cs # anchored\n\n  # rotated\n  'FAKE'",
        "client_secret:\n  &cs # anchored\n\n  # rotated\n  '***REDACTED***'",
    ),
    ("client_secret: !!str\n  &cs\n  'FAKE'", "client_secret: ***REDACTED***"),
    ("client_secret:\n  >-\n  FAKE1\n  FAKE2", "client_secret:\n  >-\n  ***REDACTED***"),
    ("client_secret:\n  |2\n    FAKE", "client_secret:\n  |2\n    ***REDACTED***"),
    ("client_secret:\n  |+ # kept\n\n  FAKE", "client_secret:\n  |+ # kept\n\n  ***REDACTED***"),
    ("client_secret:\n  # pem\n  |\n  FAKE", "client_secret:\n  # pem\n  |\n  ***REDACTED***"),
    ("client_secret:\n  &cs |\n    FAKE", "client_secret:\n  &cs |\n    ***REDACTED***"),
    ("client_secret:\n  &cs\n  |\n  FAKE", "client_secret:\n  &cs\n  |\n  ***REDACTED***"),
    ("client_secret: &cs\n  |\n  FAKE", "client_secret: &cs\n  |\n  ***REDACTED***"),
    (
        "ansible_become_password:\n  !vault\n  |\n    $ANSIBLE_VAULT;1.1;AES256\n    FAKE",
        "ansible_become_password:\n  !vault\n  |\n    ***REDACTED***",
    ),
    (
        "- x_token:\n    &t\n    |\n      FAKE\n  endpoint: e",
        "- x_token:\n    &t\n    |\n      ***REDACTED***\n  endpoint: e",
    ),
    ("\x1b[0mclient_secret:\n  |\n  FAKE", "\x1b[0mclient_secret:\n  |\n  ***REDACTED***"),
    ('"client_secret":\n  &cs\n  "FAKE"', '"client_secret":\n  &cs\n  "***REDACTED***"'),
    (
        "client_secret:\r\n  &cs\r\n  FAKE\r\nnext: 1",
        "client_secret:\r\n  &cs\r\n  ***REDACTED***\r\nnext: 1",
    ),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _YAML_PROPERTY_AND_HEADER_LINES,
    ids=[t for t, _ in _YAML_PROPERTY_AND_HEADER_LINES],
)
def test_a_value_whose_properties_or_header_end_their_line_is_redacted_whole(text, expected):
    """#285 review, round 6: YAML lets a value's properties and a block's
    header stand on lines of their own below the key, but only the
    properties on the key's line or right before the value, and a header
    beside the colon, were read, so the value was kept whole. The
    properties, the header and the lines between them stay, and the value
    goes, as text and written out."""
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)
    for depth in (1, 2, 3):
        out = redact(_json_string(text, depth))
        assert "FAKE" not in out and redact(out) == out
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == expected


# The same under an upper-case and a well-known name (#285 review, round
# 6), whose value is any value: a header alone below the key, after an
# anchor on its line and below an anchor on the key's line, and the
# properties alone on a line before a quoted or a plain scalar. The name's
# rule on its line may take a property alone on its line for the value, as
# it takes any word there; the value below it goes with it.
_UPPER_PROPERTY_AND_HEADER_LINES = [
    ("GH_TOKEN:\n  |\n  FAKE", "GH_TOKEN:\n  |\n  ***REDACTED***"),
    ("GITHUB_TOKEN:\n  &t |\n  FAKE", "GITHUB_TOKEN:\n  &t |\n  ***REDACTED***"),
    ("DB_PASSWORD: &pw\n  >\n  FAKE", "DB_PASSWORD: ***REDACTED***"),
    ("GH_TOKEN:\n  &t\n  'FAKE'", "GH_TOKEN:\n  ***REDACTED***"),
    ("DB_PASSWORD:\n  !!str\n  &a\n  FAKE", "DB_PASSWORD:\n  ***REDACTED***"),
    ("GH_TOKEN: &t\n  'FAKE'", "GH_TOKEN: ***REDACTED***"),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _UPPER_PROPERTY_AND_HEADER_LINES,
    ids=[t for t, _ in _UPPER_PROPERTY_AND_HEADER_LINES],
)
def test_an_upper_case_value_whose_properties_or_header_end_their_line_goes(text, expected):
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    for depth in (1, 2, 3):
        out = redact(_json_string(text, depth))
        assert "FAKE" not in out and redact(out) == out
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == expected


# A value below its name in JSON text (#285 review, round 6): the name's
# rule took the line break written out for the value, kept the line below
# in the clear and left the YAML patterns no line to read, under every
# upper-case, well-known and OAuth name. Each with what ``redact`` makes of
# it: a plain scalar, after a blank line, a quoted one, a block whose header
# is beside the colon and below it, after ``=``, an OAuth name, JSON in
# JSON. The break is kept.
_WRITTEN_OUT_BELOW_THE_NAME = [
    ('"GH_TOKEN:\\n  FAKE"', '"GH_TOKEN:\\n  ***REDACTED***"'),
    ('"GH_TOKEN:\\n\\n  FAKE"', '"GH_TOKEN:\\n\\n  ***REDACTED***"'),
    ("\"DB_PASSWORD:\\n  'FAKE'\"", "\"DB_PASSWORD:\\n  '***REDACTED***'\""),
    ('"DB_PASSWORD:\\n  \\"FAKE\\""', '"DB_PASSWORD:\\n  \\"***REDACTED***\\""'),
    ('"GH_TOKEN: |\\n  FAKE"', '"GH_TOKEN: |\\n  ***REDACTED***"'),
    ('"GH_TOKEN:\\n  |\\n  FAKE"', '"GH_TOKEN:\\n  |\\n  ***REDACTED***"'),
    ('"GH_TOKEN=\\n  FAKE"', '"GH_TOKEN=\\n  ***REDACTED***"'),
    ('"refresh_token:\\n  FAKE"', '"refresh_token:\\n  ***REDACTED***"'),
    ('"\\"GH_TOKEN:\\\\n  FAKE\\""', '"\\"GH_TOKEN:\\\\n  ***REDACTED***\\""'),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _WRITTEN_OUT_BELOW_THE_NAME,
    ids=[t for t, _ in _WRITTEN_OUT_BELOW_THE_NAME],
)
def test_a_line_break_written_out_starts_no_value_and_is_crossed(text, expected):
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)
    json.loads(redact(text))


# What stays (#285 review, round 6): after properties on a line of their
# own, a nested mapping, a sequence, a flow collection, a reference after a
# tag, a constant and a placeholder; a header alone below the key before a
# block of placeholders, before none (the mapping's next entry, the end of
# the text) and in a sequence entry with its sibling after it; properties
# alone before the next entry; and an empty assignment in JSON, whose line
# break written out is no value and whose string's closing quote opens none.
_R6_NEAR_MISSES = [
    "client_secret:\n  &cs\n  key: value",
    "client_secret:\n  &cs\n  !!map\n  key: value",
    "client_secret:\n  &cs\n  - a\n  - b",
    "client_secret:\n  &cs\n  [a, b]",
    "client_secret:\n  &cs\n  {a: b}",
    "client_secret:\n  !secret\n  app_client_secret",
    "api_token:\n  !!str\n  None",
    "api_token:\n  &t\n  ${{ secrets.API_TOKEN }}",
    "client_secret:\n  |\n  ${{ secrets.X }}",
    "client_secret:\n  &a\n  >-\n  {{ vault_secret }}\n  <client-secret>",
    "client_secret:\n  |\nnext: 1",
    "client_secret:\n  |",
    "client_secret:\n  &cs\nnext: 1",
    "- x_token:\n    |\n      ${{ secrets.X }}\n  endpoint: e",
    '{"a": "GH_TOKEN=\\n", "b": "x"}',
    '{"a": "GH_TOKEN:\\n", "b": "x"}',
]


@pytest.mark.parametrize("text", _R6_NEAR_MISSES)
def test_a_value_the_sixth_review_rules_leave_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


def test_a_dashes_character_is_a_dash_or_a_space_but_a_line_break():
    """``_YAML_DASH_CHAR`` is written as one class, which the regex engine
    repeats far faster than ``-|[^\\S\\r\\n]``; it must match the same
    characters, every code point checked."""
    dash = re.compile(_YAML_DASH_CHAR)
    space = re.compile(r"[^\S\r\n]")
    for point in range(sys.maxunicode + 1):
        char = chr(point)
        assert bool(dash.fullmatch(char)) == (char == "-" or bool(space.fullmatch(char))), point


def _wide_entry(width, value, sibling="endpoint: e"):
    """A block under ``x_token`` in a sequence entry whose dashes are
    ``width`` columns wide (a dash, then spaces), its line at ``width`` + 2,
    and the mapping's next entry at the key's column, ``width``."""
    return (
        "-"
        + " " * (width - 1)
        + "x_token: |\n"
        + " " * (width + 2)
        + value
        + "\n"
        + " " * width
        + sibling
    )


# Every width up to 40 columns, each side of the runs' boundaries (32,
# 1024) and past the 16 round 5 told apart; one past the top run's lowest
# bit (2**20) is pinned below, being a megabyte.
_DASH_WIDTHS = [*range(2, 41), 63, 64, 65, 1023, 1024, 1025]


@pytest.mark.parametrize("width", _DASH_WIDTHS)
def test_a_value_in_a_sequence_entry_stops_at_its_sibling_at_every_width(width):
    """#285 review, round 6: round 5 told the dashes' width apart up to 16
    columns and asked a value's line for 17 spaces past that, so a key
    behind nine nested sequences, or a dash and 17 spaces, at column 18,
    lost the mapping's next entry, at that column, to its block, and a block
    of placeholders became a value with it and the YAML was refused. The
    width is measured exactly now: the next entry stays, the value goes, and
    a placeholder block stays."""
    text = _wide_entry(width, "FAKE")
    assert redact(text) == _wide_entry(width, "***REDACTED***")
    assert credential_classes(text) == ("credential-suffix-assignment",)
    placeholder = _wide_entry(width, "${{ secrets.X_TOKEN }}")
    assert redact(placeholder) == placeholder and credential_classes(placeholder) == ()


def test_a_value_behind_a_megabyte_of_dashes_stops_at_its_sibling():
    width = (1 << 20) + 1
    text = _wide_entry(width, "FAKE")
    assert redact(text) == _wide_entry(width, "***REDACTED***")
    placeholder = _wide_entry(width, "<x-token>")
    assert redact(placeholder) == placeholder


# The reviewer's two shapes, each with its sibling (#285 review, round 6,
# R6-F3), and nested deeper: nine nested sequences and a dash with 17
# spaces, a key at column 18; forty nested sequences; a plain scalar below
# the key; a block's lines one past the key's column; each with a block of
# placeholders too, which stays.
_WIDE_SEQUENCE_SIBLINGS = [
    (
        "- " * 9 + "x_token: |\n" + " " * 20 + "FAKE\n" + " " * 18 + "endpoint: e",
        "- " * 9 + "x_token: |\n" + " " * 20 + "***REDACTED***\n" + " " * 18 + "endpoint: e",
    ),
    (
        "-" + " " * 17 + "x_token: |\n" + " " * 20 + "FAKE\n" + " " * 18 + "endpoint: e",
        "-" + " " * 17 + "x_token: |\n" + " " * 20 + "***REDACTED***\n" + " " * 18 + "endpoint: e",
    ),
    (
        "- " * 40 + "x_token: >-\n" + " " * 82 + "FAKE\n" + " " * 80 + "endpoint: e",
        "- " * 40 + "x_token: >-\n" + " " * 82 + "***REDACTED***\n" + " " * 80 + "endpoint: e",
    ),
    (
        "- " * 9 + "x_token:\n" + " " * 20 + "FAKE\n" + " " * 18 + "endpoint: e",
        "- " * 9 + "x_token:\n" + " " * 20 + "***REDACTED***\n" + " " * 18 + "endpoint: e",
    ),
    (
        "- " * 9 + "x_token: |\n" + " " * 19 + "FAKE\n" + " " * 18 + "endpoint: e",
        "- " * 9 + "x_token: |\n" + " " * 19 + "***REDACTED***\n" + " " * 18 + "endpoint: e",
    ),
]
_WIDE_SEQUENCE_PLACEHOLDERS = [
    "- " * 9 + "x_token: |\n" + " " * 20 + "${{ secrets.X }}\n" + " " * 18 + "endpoint: e",
    "-" + " " * 17 + "x_token: |\n" + " " * 20 + "<x-token>\n" + " " * 18 + "endpoint: e",
    "- " * 9 + "x_token:\n" + " " * 20 + "$X_TOKEN\n" + " " * 18 + "endpoint: e",
    "- " * 40 + "x_token: |\n" + " " * 82 + "{{ vault }}\n" + " " * 80 + "endpoint: e",
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _WIDE_SEQUENCE_SIBLINGS,
    ids=["nine-nested", "dash-and-17-spaces", "forty-nested", "plain-scalar", "one-past"],
)
def test_a_value_behind_wide_dashes_stops_at_its_sibling(text, expected):
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)
    out = redact(json.dumps(text))
    assert json.loads(out) == expected


@pytest.mark.parametrize("text", _WIDE_SEQUENCE_PLACEHOLDERS)
def test_a_placeholder_behind_wide_dashes_with_its_sibling_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


@pytest.mark.parametrize(
    ("text", "body"),
    [c[1:] for c in WHOLE_VALUES_285_R7],
    ids=[c[0] for c in WHOLE_VALUES_285_R7],
)
def test_a_value_the_seventh_285_review_found_is_redacted_whole_as_text_and_as_json(text, body):
    """#285 review, round 7: YAML breaks a line at a carriage return alone,
    but only ``\\n`` and ``\\r\\n`` ended a key's line, a value's or a
    block's, and a line started after a line feed only, so a value whose
    lines bare carriage returns end, valid YAML, was kept whole, as text and
    written out. Each goes whole, as text and as JSON, JSON in JSON and
    three deep write it, the JSON still decodes to what redacting the text
    gives, and the output is stable and within the growth bound."""
    name = next(name for name, case, _ in WHOLE_VALUES_285_R7 if case == text)
    assert redact(text) == WHOLE_VALUES_285_R7_REDACTED[name]
    for depth in (0, 1, 2, 3):
        escaped = _json_string(text, depth)
        out = redact(escaped)
        assert body not in out and "FAKE" not in out, out
        assert credential_classes(escaped) == ("credential-suffix-assignment",)
        assert redact(out) == out and credential_classes(out) == ()
        assert len(out) <= MAX_GROWTH_FACTOR * len(escaped)
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == WHOLE_VALUES_285_R7_REDACTED[name]
    assert redact_obj({"env": {"text": text}}) == {"env": {"text": redact(text)}}


# A value whose lines bare carriage returns end (#285 review, round 7,
# R7-F2), each with what ``redact`` makes of it: a scalar below its key with
# the next entry after it, a literal block with a blank line in it and a
# kept one, past a comment line and a blank line, properties on lines of
# their own (an anchor, then a quoted scalar; a tag and an anchor), on the
# key's line, a header below them, a quoted scalar below the key and one
# whose quote ends its line, a quote that ends the key's line, after a key
# on an earlier line, a plain scalar that goes on below the key's line, in
# sequence entries with their siblings, after OpenCode's ANSI escape, as a
# quoted key, a CRLF line and a CR line in one value, Ansible's ``!vault``,
# and under well-known and upper-case names.
_YAML_CR_LINES = [
    ("client_secret:\r  FAKE\rnext: 1\r", "client_secret:\r  ***REDACTED***\rnext: 1\r"),
    (
        "client_secret: |\r  FAKE1\r\r  FAKE2\rnext: 1",
        "client_secret: |\r  ***REDACTED***\rnext: 1",
    ),
    ("client_secret: |+\r  FAKE\r\r", "client_secret: |+\r  ***REDACTED***\r\r"),
    ("client_secret:\r  # rotated\r  FAKE", "client_secret:\r  # rotated\r  ***REDACTED***"),
    ("client_secret:\r\r  FAKE", "client_secret:\r\r  ***REDACTED***"),
    ("client_secret:\r  &cs\r  'FAKE'", "client_secret:\r  &cs\r  '***REDACTED***'"),
    ("client_secret:\r  !!str\r  &a\r  FAKE", "client_secret:\r  !!str\r  &a\r  ***REDACTED***"),
    ("client_secret: &cs\r  FAKE", "client_secret: &cs\r  ***REDACTED***"),
    ("client_secret:\r  &cs\r  |\r  FAKE", "client_secret:\r  &cs\r  |\r  ***REDACTED***"),
    ('client_secret:\r  "FAKE"', 'client_secret:\r  "***REDACTED***"'),
    ('client_secret:\r  "\r  FAKE"', 'client_secret:\r  "***REDACTED***"'),
    ("client_secret: '\r  FAKE'", "client_secret: '***REDACTED***'"),
    ("endpoint: e\rclient_secret: '\r  FAKE'", "endpoint: e\rclient_secret: '***REDACTED***'"),
    ("client_secret: FAKE1\r  FAKE2\r", "client_secret: ***REDACTED***\r"),
    ("- x_token: |\r    FAKE\r  endpoint: e", "- x_token: |\r    ***REDACTED***\r  endpoint: e"),
    (
        "- - x_token:\r      FAKE\r    endpoint: e",
        "- - x_token:\r      ***REDACTED***\r    endpoint: e",
    ),
    ("\x1b[0mclient_secret:\r  FAKE", "\x1b[0mclient_secret:\r  ***REDACTED***"),
    ('"client_secret":\r  "FAKE"', '"client_secret":\r  "***REDACTED***"'),
    ("client_secret:\r\n  FAKE1\r  FAKE2\n", "client_secret:\r\n  ***REDACTED***\n"),
    (
        "client_secret: !vault |\r  $ANSIBLE_VAULT;1.1;AES256\r  FAKE",
        "client_secret: !vault |\r  ***REDACTED***",
    ),
    ("GH_TOKEN:\r  |\r  FAKE", "GH_TOKEN:\r  |\r  ***REDACTED***"),
    ("GH_TOKEN:\r  &t\r  'FAKE'", "GH_TOKEN:\r  ***REDACTED***"),
    ("DEPLOY_SECRET:\r  FAKE", "DEPLOY_SECRET:\r  ***REDACTED***"),
]


@pytest.mark.parametrize(("text", "expected"), _YAML_CR_LINES, ids=[t for t, _ in _YAML_CR_LINES])
def test_a_yaml_value_whose_lines_carriage_returns_end_is_redacted_whole(text, expected):
    """#285 review, round 7: a key, a value's lines, its properties and a
    block's header end their lines at a bare carriage return as YAML reads
    one, and a key starts a line after it. The key, the properties, the
    header, the carriage returns and the lines after the value stay, and the
    value goes, as text and written out."""
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)
    for depth in (1, 2, 3):
        out = redact(_json_string(text, depth))
        assert "FAKE" not in out and redact(out) == out
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == expected


# What stays though carriage returns end its lines (#285 review, round 7):
# a statement that ends in a colon, a nested mapping, a sequence, a block
# and a scalar of placeholders, properties before a mapping, a reference
# after a tag, a constant, a placeholder block in a sequence entry with its
# sibling, a key with no value before the next entry, a function's typed
# parameter, a string literal that ends in an assignment, a progress line
# and a code fence; and an empty assignment in JSON, whose carriage return
# written out is no value and whose string's closing quote opens none.
_R7_NEAR_MISSES = [
    "if api_token:\r    return api_token\r",
    "client_secret:\r  key: value\r",
    "client_secret:\r  - a\r  - b",
    "client_secret: |\r  ${{ secrets.X }}\r",
    "client_secret: |\r  {{ a }}\r  <b>\rnext: 1",
    "client_secret:\r  {{ vault_secret }}\r",
    "client_secret:\r  &cs\r  key: value",
    "client_secret:\r  !secret\r  app_client_secret",
    "api_token:\r  None\r",
    "- x_token: |\r    ${{ secrets.X }}\r  endpoint: e",
    "client_secret:\rnext: 1",
    "def f(\r    api_token: str,\r):",
    'help = "Set GITHUB_TOKEN="\r    "and more"',
    "Downloading 10%\rDownloading 20%\r",
    "```yaml\rclient_secret:\r```",
    '{"a": "GH_TOKEN=\\r", "b": "x"}',
    '{"a": "GH_TOKEN:\\r", "b": "x"}',
]


@pytest.mark.parametrize("text", _R7_NEAR_MISSES)
def test_a_value_the_seventh_review_rules_leave_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


@pytest.mark.parametrize(
    ("text", "body"),
    [c[1:] for c in WHOLE_VALUES_285_R8],
    ids=[c[0] for c in WHOLE_VALUES_285_R8],
)
def test_a_value_the_eighth_285_review_found_is_redacted_whole_as_text_and_as_json(text, body):
    """#285 review, round 8: a value below a YAML flow mapping's key, which
    a YAML parser reads, and a value under a name whose prefix starts with
    underscores, a valid shell identifier, were kept whole, as text and
    written out. Each goes whole, as text and as JSON, JSON in JSON and three
    deep write it, the JSON still decodes to what redacting the text gives
    (the flow indicators and the next entry included), and the output is
    stable and within the growth bound."""
    name = next(name for name, case, _ in WHOLE_VALUES_285_R8 if case == text)
    assert redact(text) == WHOLE_VALUES_285_R8_REDACTED[name]
    for depth in (0, 1, 2, 3):
        escaped = _json_string(text, depth)
        out = redact(escaped)
        assert body not in out and "FAKE" not in out, out
        assert credential_classes(escaped) == ("credential-suffix-assignment",)
        assert redact(out) == out and credential_classes(out) == ()
        assert len(out) <= MAX_GROWTH_FACTOR * len(escaped)
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == WHOLE_VALUES_285_R8_REDACTED[name]
    assert redact_obj({"env": {"text": text}}) == {"env": {"text": redact(text)}}


# A value below a YAML flow collection's key (#285 review, round 8, R8-F2),
# each with what ``redact`` makes of it: spaces around the key and before the
# ``}``, a plain scalar of several lines and of several words with the next
# entry after it, past a blank line and a comment line, a comment after it,
# its anchor and its tag below the key, a quoted scalar holding flow
# indicators and one of several lines, a plain scalar that goes on below the
# key's line with the next entry after it and after an anchor, a key after a
# ``,`` and a line break whose value is no more indented than it, a flow
# mapping in a block mapping with the block's next entry after it, two in a
# flow sequence, after OpenCode's ANSI escape, CRLF and a bare carriage
# return, and under a well-known and an upper-case name, the next line
# indented, the next entry after it on its line and the next key after the
# marker.
_YAML_FLOW_VALUES = [
    ("{ client_secret :\n  FAKE }", "{ client_secret :\n  ***REDACTED*** }"),
    (
        "{client_secret:\n  FAKE1\n  FAKE2,\n  endpoint: e}",
        "{client_secret:\n  ***REDACTED***,\n  endpoint: e}",
    ),
    ("{client_secret:\n  FAKE1 FAKE2, next: 1}", "{client_secret:\n  ***REDACTED***, next: 1}"),
    (
        "{client_secret:\n\n  # rotated\n  FAKE}",
        "{client_secret:\n\n  # rotated\n  ***REDACTED***}",
    ),
    ("{client_secret:\n  FAKE # c\n}", "{client_secret:\n  ***REDACTED*** # c\n}"),
    ("{client_secret:\n  &cs\n  FAKE}", "{client_secret:\n  &cs\n  ***REDACTED***}"),
    ("{client_secret:\n  !!str FAKE}", "{client_secret:\n  !!str ***REDACTED***}"),
    ('{client_secret:\n  "FAKE, [still]"}', '{client_secret:\n  "***REDACTED***"}'),
    ("{client_secret:\n  'FAKE\n  more'}", "{client_secret:\n  '***REDACTED***'}"),
    (
        "{client_secret: FAKE1\n  FAKE2\n  FAKE3, b: c}",
        "{client_secret: ***REDACTED***, b: c}",
    ),
    ("{client_secret: &cs FAKE1\n  FAKE2}", "{client_secret: &cs ***REDACTED***}"),
    ("{a: 1,\n   client_secret:\n  FAKE}", "{a: 1,\n   client_secret:\n  ***REDACTED***}"),
    ("x: {client_secret:\n  FAKE}\ny: 2", "x: {client_secret:\n  ***REDACTED***}\ny: 2"),
    (
        "[{client_secret:\n  FAKE1}, {db_password:\n  FAKE2}]",
        "[{client_secret:\n  ***REDACTED***}, {db_password:\n  ***REDACTED***}]",
    ),
    ("\x1b[0m{client_secret:\n  FAKE}", "\x1b[0m{client_secret:\n  ***REDACTED***}"),
    ("{client_secret:\r\n  FAKE}", "{client_secret:\r\n  ***REDACTED***}"),
    ("{client_secret:\r  FAKE1\r  FAKE2}", "{client_secret:\r  ***REDACTED***}"),
    # An upper-case or a well-known name's value is anything, as on the
    # key's line, where ``{GH_TOKEN: FAKE}`` loses its ``}`` to the marker:
    # the ``}`` goes with it, and the next block entry stays.
    ("{GH_TOKEN:\n  FAKE1\n  FAKE2}", "{GH_TOKEN:\n  ***REDACTED***"),
    ("{DEPLOY_SECRET: FAKE1\n  FAKE2}\nnext: 1", "{DEPLOY_SECRET: ***REDACTED***\nnext: 1"),
    # Its value takes the ``,`` its next line ends at where the next entry's
    # key follows, which stays; and a key after such a value, whose ``,`` it
    # took, is read after the marker.
    ("{GH_TOKEN:\n  FAKE1\n  FAKE2, region: r}", "{GH_TOKEN:\n  ***REDACTED*** region: r}"),
    (
        "{GH_TOKEN: FAKE1, client_secret:\n  FAKEnext}",
        "{GH_TOKEN: ***REDACTED*** client_secret:\n  ***REDACTED***}",
    ),
    (
        "{DEPLOY_SECRET: FAKE1\n  FAKE2, client_secret: FAKE3\n  FAKE4}",
        "{DEPLOY_SECRET: ***REDACTED*** client_secret: ***REDACTED***}",
    ),
]


@pytest.mark.parametrize(
    ("text", "expected"), _YAML_FLOW_VALUES, ids=[t for t, _ in _YAML_FLOW_VALUES]
)
def test_a_value_below_a_yaml_flow_key_is_redacted_whole(text, expected):
    """#285 review, round 8: inside a flow collection a node's lines need no
    indentation past its key, and a key there starts no line, so the value
    below a key right after a ``{``, a ``[`` or a ``,`` was kept whole, or
    its lines after the first. The value goes, as text and written out; the
    flow indicators, the next entry, the key, its properties and the comment
    and blank lines stay."""
    assert redact(text) == expected
    assert credential_classes(text) != ()
    assert redact(expected) == expected and credential_classes(expected) == ()
    assert "FAKE" not in redact(text)
    for depth in (1, 2, 3):
        out = redact(_json_string(text, depth))
        assert "FAKE" not in out and redact(out) == out
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == expected


# What the flow rules leave (#285 review, round 8): under a lower-case name
# a value that reads as code (a type, a constant, a number, a dotted name,
# the same name, a reference after a tag, a template, a call), a nested
# mapping, a sequence and a flow collection, no value before the ``}`` or
# the next entry, a code fence, a plural name, a call's closing bracket on
# the next line; in JSON text a key whose line break ends its string, the next
# string's quote no value's; the line after ``key: value`` lines that
# redaction left, at a line's start; and a suffix behind underscores alone.
_R8_NEAR_MISSES = [
    "{client_secret:\n  string}",
    "type T = {client_secret:\n  string;\n}",
    "{client_secret:\n  None}",
    "{client_secret:\n  12345}",
    "{client_secret:\n  process.env.CLIENT_SECRET,\n  b: 1}",
    "x = {api_token:\n  api_token}",
    "{client_secret:\n  !secret app_secret}",
    "{client_secret:\n  ${{ secrets.X }}}",
    "{client_secret:\n  {{ vault_secret }}}",
    "{client_secret:\n  get_secret(name)}",
    "{client_secret:\n  key: value}",
    "{client_secret:\n  - a}",
    "{client_secret:\n  [a, b]}",
    "{client_secret:\n}",
    "{client_secret:\n  , b: c}",
    "{client_secret:\n  ```\n  x\n  ```}",
    "{client_secrets:\n  value}",
    "foo({client_secret: secret\n  })",
    '{"a": "x, client_secret:\\n", "b": "y"}',
    '{"a": "{client_secret:\\n", "b": "y"}',
    '{"a": "client_secret:\\n  ", "b": "y"}',
    '["x,client_secret:\\r", "y"]',
    "api_token: ***REDACTED***\ndb_password: ***REDACTED***\nPATH=/usr/bin",
    "GH_TOKEN=***REDACTED***\nclient_secret: ***REDACTED***\nLoaded 3 modules, 2 plugins",
    "_TOKEN=x",
    "__TOKEN=x",
    "__API_KEY: x",
    "{_SECRET:\n  value}",
    "self._api_token = api_token",
    "_client_secret: Optional[str] = None",
]


@pytest.mark.parametrize("text", _R8_NEAR_MISSES)
def test_a_value_the_eighth_review_rules_leave_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


# Values whose redaction the rules for a YAML flow collection must agree on
# (#285 review, round 8), each with what ``redact`` makes of it: a value
# that a well-known or upper-case name's value took the ``,`` before, on the
# key's line, below it and through a block rule that takes a line whole, so
# the next key stands after the marker; values in a row, each of which
# takes the ``,`` the next one stands after; and lines that end in ``,``
# below such a name, which a lower-case name's value, ending at the ``,``,
# would have left for that name's rule to take, a line a round; and lines
# below a key that starts its line, indented past it and not by turns.
_YAML_FLOW_MEETINGS = [
    (
        "{a: 1,\n  _SERVICE_API_KEY:\n FAKE1,\n  __client_secret:\n &cs FAKE2}",
        "{a: 1,\n  _SERVICE_API_KEY:\n ***REDACTED***\n  __client_secret:\n &cs ***REDACTED***}",
    ),
    (
        "{\n  x_api_key:\n    FAKE1,\n  client_secret:\n  FAKE2}",
        "{\n  x_api_key:\n    ***REDACTED***\n  client_secret:\n  ***REDACTED***}",
    ),
    (
        "{DEPLOY_SECRET: !!str FAKE1,\n  db_password:\n &cs FAKE2,\n  name: n}",
        "{DEPLOY_SECRET: !!str ***REDACTED***\n  db_password:\n &cs ***REDACTED***,\n  name: n}",
    ),
    (
        "{A_TOKEN:\n  F1\n  F2,\n  B_TOKEN:\n  F3\n  F4,\n  C_TOKEN:\n  F5\n  F6}",
        "{A_TOKEN:\n  ***REDACTED***\n  B_TOKEN:\n  ***REDACTED***\n  C_TOKEN:\n  ***REDACTED***",
    ),
    ("{GH_TOKEN:\n  F1\n  F2,\n  F3,\n  F4,\n  F5}", "{GH_TOKEN:\n  ***REDACTED***"),
    ("{gh_token:\n  F1\n  F2,\n  F3,\n  F4,\n  F5}", "{gh_token:\n  ***REDACTED***"),
    # A key that starts its line, its value's lines below it indented past
    # the key and not, by turns: the block rule takes an indented line, and
    # a flow scalar that stopped at the ``,`` on one let each go on where
    # the other stopped, a line a round.
    (
        "{a: 1,\nk_token: FAKE1\nFAKE2\n  , FAKE3\nFAKE4\n  , FAKE5\nFAKE6}",
        "{a: 1,\nk_token: ***REDACTED***}",
    ),
]


@pytest.mark.parametrize(
    ("text", "expected"), _YAML_FLOW_MEETINGS, ids=[t for t, _ in _YAML_FLOW_MEETINGS]
)
def test_the_flow_rules_and_the_name_rules_agree_where_they_meet(text, expected):
    """#285 review, round 8: a rule that took a ``,`` or a ``}`` a flow
    collection's value stopped at let that value go on to the next line in
    the next round, a line a round, and a match that took a marker took the
    lead of the key after it, whose value then stayed. Each value goes
    within the rounds, the output is stable, and each depth written out
    decodes to it."""
    assert redact(text) == expected
    assert "FAKE" not in expected and "F2" not in expected
    assert redact(expected) == expected and credential_classes(expected) == ()
    for depth in (1, 2, 3):
        out = redact(_json_string(text, depth))
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == expected and redact(out) == out


def test_a_flow_value_line_below_an_upper_case_name_that_is_not_indented_is_left():
    """#285 review, round 8, a deliberately unfixed edge: under an
    upper-case or a well-known name a flow collection's value is anything,
    flow indicators included, as on the key's line, so only indentation
    tells its lines below from what follows the collection, and a line that
    is not indented stays (the first line is the name's own rule's), and so
    does one whose next entry's key holds a space. Going on
    at no indentation, or ending at the collection's ``}``, either took the
    lines after the collection or let the key's-line rule take the ``}`` and
    the next round go on, a line a round."""
    text = "{GH_TOKEN: FAKE1\nFAKEleft}"
    assert redact(text) == "{GH_TOKEN: ***REDACTED***\nFAKEleft}"
    assert redact(redact(text)) == redact(text)
    text = "{GH_TOKEN:\n  FAKE1\nFAKEleft,\nnext}"
    assert redact(text) == "{GH_TOKEN:\n  ***REDACTED***\nFAKEleft,\nnext}"
    # Nor is a line whose next entry's key holds a space: the ``,`` that
    # ends the value is told by a key of one word.
    text = "{CLIENT_SECRET:\n  FAKE1\n  FAKEleft, two words: x}"
    assert redact(text) == "{CLIENT_SECRET:\n  ***REDACTED***\n  FAKEleft, two words: x}"


_FLOW_LOWER_NAMES = ["client_secret", "db_password", "x_api_key", "_db_password", "Db_Password"]
_FLOW_UPPER_NAMES = ["CLIENT_SECRET", "_SERVICE_API_KEY", "GH_TOKEN", "gh_token"]


def _flow_value(rng, tag, upper):
    """A value for a flow collection's entry, its words and where they are:
    quoted on the key's line or below it, after a property, below the key
    on lines of their own, or going on below the key's line. Under an
    upper-case name the lines below are indented (the edge above)."""
    words = [f"FAKE{tag}w{i}" for i in range(rng.randint(1, 3))]
    ind = " " * (rng.choice([1, 2, 4]) if upper else rng.choice([0, 1, 2, 4]))
    kind = rng.random()
    if kind < 0.2:
        q = rng.choice(["'", '"'])
        return f"{rng.choice([' ', chr(10) + ind])}{q}{' '.join(words)}{q}", words
    if kind < 0.35:
        prop = rng.choice(["&a ", "!!str "])
        return f" {prop}{words[0]}" + "".join(f"\n{ind} {w}" for w in words[1:]), words
    if kind < 0.6:
        return "\n" + ind + f"\n{ind}".join(words), words
    if kind < 0.8:
        return f" {words[0]}" + "".join(f"\n{ind} {w}" for w in words[1:]), words
    return f"\n{ind}&cs {words[0]}", words[:1]


def _flow_collection(rng, it):
    entries, secrets = [], []
    for j in range(rng.randint(1, 4)):
        r = rng.random()
        if r < 0.3:
            entries.append(rng.choice(["endpoint: e", "region: r", "a: 1"]))
            continue
        upper = r >= 0.7
        name = rng.choice(_FLOW_UPPER_NAMES if upper else _FLOW_LOWER_NAMES)
        value, words = _flow_value(rng, f"{it}x{j}", upper)
        entries.append(f"{name}:{value}")
        secrets += words
    text = "{" + rng.choice([", ", ",\n  ", ",\n"]).join(entries) + "}"
    return (f"outer: {text}\nnext: 1" if rng.random() < 0.3 else text), secrets


def test_every_value_in_a_generated_yaml_flow_collection_is_redacted():
    """#285 review, round 8: flow collections that a YAML parser reads,
    their keys mixing lower-, mixed- and upper-case, well-known and
    underscore-led names with other entries, their values plain, quoted,
    after a property, below the key or going on below its line, entries on
    one line or on lines of their own. Every word of every credential value
    goes, as text and as JSON one, two and three deep, the output is
    stable, and within the growth bound."""
    yaml = pytest.importorskip("yaml")
    rng = random.Random(2858)
    checked = 0
    while checked < 400:
        text, secrets = _flow_collection(rng, checked)
        try:
            yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        checked += 1
        for depth in (0, 1, 2, 3):
            written = _json_string(text, depth)
            out = redact(written)
            assert [w for w in secrets if w in out] == [], text
            assert redact(out) == out and credential_classes(out) == (), text
            assert len(out) <= MAX_GROWTH_FACTOR * len(written)


# A name whose prefix starts with underscores (#285 review, round 8,
# R8-F3), each with what ``redact`` makes of it: in upper, lower and mixed
# case, behind one underscore and two, a digit after them, with ``=`` and
# ``:``, a quoted value, after ``export`` and in a sentence, after OpenCode's
# ANSI escape, as a quoted key, as a YAML key with a value below it and a
# block, and with a quoted value whose quote ends its line.
_UNDERSCORE_NAMES = [
    ("_SERVICE_API_KEY=FAKE", "_SERVICE_API_KEY=***REDACTED***"),
    ("_AUTH_TOKEN: FAKE", "_AUTH_TOKEN: ***REDACTED***"),
    ("__CLIENT_SECRET=FAKE", "__CLIENT_SECRET=***REDACTED***"),
    ("_DB_PASSWORD=FAKE", "_DB_PASSWORD=***REDACTED***"),
    ("_db_password=FAKE", "_db_password=***REDACTED***"),
    ("__Client_Secret: FAKE", "__Client_Secret: ***REDACTED***"),
    ("_1_TOKEN=FAKE", "_1_TOKEN=***REDACTED***"),
    ('_AUTH_TOKEN="FAKE spaced; value"', '_AUTH_TOKEN="***REDACTED***"'),
    ("export _SERVICE_API_KEY=FAKE", "export _SERVICE_API_KEY=***REDACTED***"),
    ("set _AUTH_TOKEN=FAKE then run", "set _AUTH_TOKEN=***REDACTED*** then run"),
    ("\x1b[0m_AUTH_TOKEN=FAKE", "\x1b[0m_AUTH_TOKEN=***REDACTED***"),
    ('{"_SERVICE_API_KEY": "FAKE"}', '{"_SERVICE_API_KEY": "***REDACTED***"}'),
    ("_AUTH_TOKEN:\n  FAKE", "_AUTH_TOKEN:\n  ***REDACTED***"),
    ("__client_secret: |\n  FAKE", "__client_secret: |\n  ***REDACTED***"),
    ("_auth_token: FAKE1\n  FAKE2", "_auth_token: ***REDACTED***"),
    ('_DB_PASSWORD="\nFAKE"', '_DB_PASSWORD="***REDACTED***"'),
]


@pytest.mark.parametrize(
    ("text", "expected"), _UNDERSCORE_NAMES, ids=[t for t, _ in _UNDERSCORE_NAMES]
)
def test_a_name_whose_prefix_starts_with_underscores_is_recognised(text, expected):
    """#285 review, round 8: a shell identifier may start with underscores,
    but a prefixed name had to start with a letter, so a value under
    ``_SERVICE_API_KEY``, ``_AUTH_TOKEN`` or ``__CLIENT_SECRET`` was kept
    whole by every rule that reads such a name, as text and written out.
    Each is replaced; the name stops where the identifier does."""
    assert redact(text) == expected
    assert credential_classes(text) == ("credential-suffix-assignment",)
    assert redact(expected) == expected and credential_classes(expected) == ()
    for depth in (1, 2, 3):
        out = redact(_json_string(text, depth))
        assert "FAKE" not in out and redact(out) == out
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == expected


def test_redact_obj_redacts_a_value_under_a_name_whose_prefix_starts_with_underscores():
    """#285 review, round 8: a mapping's key that names a credential behind
    underscores (``_SERVICE_API_KEY``, ``__CLIENT_SECRET``, ``_db_password``)
    was no credential's key, so its opaque value was kept at every depth. It
    is replaced and the entry kept; a suffix behind underscores alone
    (``_TOKEN``, ``__API_KEY``) still names none."""
    secrets = dict(UNDERSCORE_KEYED_SECRETS_285)
    value = {"env": dict(secrets, _TOKEN="x", __API_KEY="y"), "steps": [secrets]}
    out = redact_obj(value)
    redacted = dict.fromkeys(secrets, _R)
    assert out == {"env": redacted | {"_TOKEN": "x", "__API_KEY": "y"}, "steps": [redacted]}
    for secret in secrets.values():
        assert secret not in repr(out)
    assert redact_obj(out) == out


# An OAuth name is a prefixed name too (#285 review, round 3, R3-F3):
# ``oauth-assignment`` takes its value first and ``credential-suffix-
# assignment`` what it left. The OAuth value ran on over a full stop, a
# closing bracket or a backtick that the prefixed-name rule leaves after its
# marker as the punctuation after a word, so a second redaction took it and
# ``credential_classes`` found a credential in the first one's output. Each
# with what ``redact`` makes of it.
_OAUTH_AND_SUFFIX_VALUES = [
    ("refresh_token=,FAKEdummy.", "refresh_token=***REDACTED***."),
    ("refresh_token=FAKE,dummy.", "refresh_token=***REDACTED***."),
    ("Then access_token=,FAKE. ok", "Then access_token=***REDACTED***. ok"),
    ("`access_token=,FAKE` in .env", "`access_token=***REDACTED***` in .env"),
    ("id_token=FAKE)", "id_token=***REDACTED***)"),
    ("refresh_token=FAKE.)", "refresh_token=***REDACTED***.)"),
    ("oauth_refresh_token=FAKE:x,y.", "oauth_refresh_token=***REDACTED***."),
    # A line break written out ends the word as one written as such does
    # (round 8): the full stop before it is the punctuation after it.
    ("refresh_token=FAKE.\\nnext", "refresh_token=***REDACTED***.\\nnext"),
    ("REFRESH_TOKEN=,FAKE.", "REFRESH_TOKEN=***REDACTED***"),
    ("REFRESH_TOKEN=FAKE`", "REFRESH_TOKEN=***REDACTED***"),
]


@pytest.mark.parametrize(("text", "expected"), _OAUTH_AND_SUFFIX_VALUES, ids=str)
def test_an_oauth_value_ends_where_the_prefixed_name_rule_ends_it(text, expected):
    """One round of the patterns already gives text that a second round
    leaves: the OAuth and prefixed-name values end at the same characters, so
    neither takes what the other left after its marker."""
    once = _redact_once(text)
    assert once == expected == redact(text)
    assert _redact_once(once) == once
    assert credential_classes(once) == ()


# Every OAuth name, a prefixed and a well-known one with each separator and
# a value that starts, ends or is cut by a character one rule ends a value
# at and the other does not, alone, in a sentence, in a code span, before a
# line and in a quoted string (#285 review, round 3).
_CROSS_NAMES = [
    "refresh_token",
    "access_token",
    "id_token",
    "oauth_refresh_token",
    "REFRESH_TOKEN",
    "Refresh_Token",
    "client_secret",
    "Db_Password",
    "GH_TOKEN",
    "chatgpt-account-id",
]
_CROSS_SEPARATORS = ["=", ": ", " = ", ":\n  "]
_CROSS_VALUES = [
    ",FAKE.",
    "FAKE.",
    "FAKE,x.",
    "&FAKE)",
    "FAKE`",
    ",FAKE`x",
    "FAKE:x.",
    "FAKE]}",
    "FAKE.,",
    "FAKE\\n",
    'FAKE\\"',
    "FAKE;x",
    "FAKE&a=1.",
    "%2FFAKE.",
    "(FAKE).",
    ",.FAKE",
    '"FAKE x".',
    "FAKE.`x`",
]
_CROSS_CONTEXTS = ["{}", "Then {} ok", "`{}` in .env", "{}\nnext", '"{}"']


def test_one_round_of_the_patterns_is_stable_across_the_oauth_and_name_rules():
    """#285 review, round 3 (R3-F3): one round left text that a second one
    changed, for 240 of these at the reviewed head. None does now, and
    ``redact``'s output carries no credential class."""
    unstable = []
    for name, separator, value, context in itertools.product(
        _CROSS_NAMES, _CROSS_SEPARATORS, _CROSS_VALUES, _CROSS_CONTEXTS
    ):
        text = context.format(name + separator + value)
        once = _redact_once(text)
        if _redact_once(once) != once or credential_classes(redact(text)):
            unstable.append(text)
        assert len(redact(text)) <= MAX_GROWTH_FACTOR * len(text)
    assert unstable == []


# A seeded generator of text where the patterns meet (#285 review, round 3):
# names of every kind, separators that span lines, quotes, blocks, escapes
# and leads, and values of the characters one rule ends a value at and
# another does not. A value's extent depends on the text around it, which a
# later pattern can redact; ``redact`` repeats its round until nothing
# changes, so its output is stable whatever met.
_MEET_NAMES = [
    "refresh_token",
    "access_token",
    "client_secret",
    "db_password",
    "x_api_key",
    "REFRESH_TOKEN",
    "A_TOKEN",
    "GH_TOKEN",
    "my_GITHUB_TOKEN",
    "token",
    "password",
    "Authorization",
    "chatgpt-account-id",
    "refresh",
    "access",
    '"refresh_token"',
    "'client_secret'",
    '"GH_TOKEN"',
    "AWS_SECRET_ACCESS_KEY",
    "Db_Password",
    # Round 8: names whose prefix starts with underscores.
    "_SERVICE_API_KEY",
    "__client_secret",
]
_MEET_SEPARATORS = [
    "=",
    ": ",
    ":",
    " = ",
    ":\n  ",
    "=\n",
    ": |\n  ",
    "=\\n",
    ": >-\n  ",
    ":\n",
    ":\\n  ",
    ": |\\n  ",
    ':\n  "',
    ": !vault |\n  ",
    # Round 4: a quote that ends its line, blank and comment lines before
    # a value, a plain scalar that starts on its key's line and goes on.
    '="\n',
    ": '\n  ",
    ":\n\n  ",
    ":\n  # c\n  ",
    ": x\n  ",
    ": |\n  12\n  ",
    # Round 5: properties, a doubled quote after a quote's line break, a
    # quote in a plain scalar's first line.
    ": &a ",
    ": !!str ",
    ":\n  &a ",
    ": &a |\n  ",
    ": '\n''",
    ': x"y\n  ',
    # Round 6: properties and a block's header on lines of their own below
    # the key, as written and written out, and a line break written out
    # before a value after ``=``.
    ":\n  &a\n  ",
    ":\n  |\n  ",
    ":\n  &a\n  |\n  ",
    ": &a\n  !!str\n  ",
    ":\\n  &a\\n  ",
    ":\\n  |\\n  ",
    "=\\n  ",
    # Round 7: a key's line, a property's and a header's ended by a bare
    # carriage return, as written and written out, and a carriage return
    # written out before a value after ``=``.
    ":\r  ",
    ": |\r  ",
    ":\n  &a\r  ",
    ":\r  |\r  ",
    ":\\r  ",
    ": |\\r  ",
    "=\\r",
]
_MEET_PIECES = [
    *"aZ09,.:)]}`&;\\\"' \n#=%$!*-_/+|>{[",
    "\\n",
    '\\"',
    "FAKE",
    "&x=",
    "Bearer ",
    "\n\n",
    "```",
    "ghp_" + "a" * 11,
    "\n  ",
    "\n    ",
    ": ",
    " #",
    "\\n  ",
    GOOGLE_API_KEY,
    '"\n',
    "\n\n  ",
    "12345\n  ",
    "${{ s }}",
    "''",
    "&a ",
    "!!str ",
    "\t",
    "\n    ",
    # Round 6: a carriage return alone, as written and written out, and a
    # property or a header that ends its line.
    "\r",
    "\\r",
    "&a\n  ",
    "|\n  ",
    # Round 7: carriage returns ending a line before an indentation, two of
    # them, and before a line feed.
    "\r  ",
    "\r\r",
    "\r\r\n",
    "\\r  ",
    # Round 8: a flow collection's next entry and its end, on the value's
    # line and below it.
    ", b: 1}",
    "}\n",
    "\n  , ",
]
_MEET_LEADS = [
    "",
    "x ",
    "\x1b[0m",
    "\\n",
    "- ",
    "  ",
    "print('",
    "`",
    "\n",
    "\n  ",
    '"',
    "if ",
    "  - ",
    "- - ",
    # Round 6: dashes past 16 columns.
    "- " * 9,
    "-" + " " * 17,
    # Round 7: a key on a line a carriage return starts.
    "\r",
    "x: 1\r  ",
    # Round 8: a key in a flow collection, after its ``{``, a ``,``, a
    # ``[`` and a line break.
    "{",
    "x: {",
    "{a: 1, ",
    "[",
    "{\n  ",
    "a,\n",
]


def _meeting_text(rng):
    parts = []
    for _ in range(rng.randint(1, 3)):
        parts += [
            rng.choice(_MEET_LEADS),
            rng.choice(_MEET_NAMES),
            rng.choice(_MEET_SEPARATORS),
            rng.choice(["", "", '"', "'", '\\"']),
            "".join(rng.choice(_MEET_PIECES) for _ in range(rng.randint(0, 10))),
        ]
    text = "".join(parts)
    return json.dumps(text) if rng.random() < 0.2 else text


def test_redact_is_stable_where_the_patterns_meet():
    rng = random.Random(285)
    unstable = []
    for _ in range(3_000):
        text = _meeting_text(rng)
        out = redact(text)
        if redact(out) != out or credential_classes(out):
            unstable.append(text)
        assert len(out) <= MAX_GROWTH_FACTOR * len(text)
    assert unstable == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A later pattern redacted the quote of another value's kind that
        # had kept a cut value from spanning to the next one, so a second
        # redaction spanned it (found by the generator above).
        (
            'GH_TOKEN="FAKEcut\nA_TOKEN: \'FAKE "x" y\' more" tail',
            'GH_TOKEN="***REDACTED***" tail',
        ),
        # A YAML block, redacted last, held the quote behind fewer
        # backslashes that had ended an escaped value's string.
        (
            'x_token=\\"FAKEcut\nGITHUB_TOKEN: |\n  "FAKEquoted\n\\" tail',
            'x_token=\\"***REDACTED***\\" tail',
        ),
    ],
    ids=["quote-in-a-later-value", "quote-in-a-later-block"],
)
def test_a_value_whose_extent_a_later_pattern_changes_is_redacted_to_its_end(text, expected):
    """#285 review, round 3: where a pattern redacts part of the text that
    decides another value's extent, the round after it reads the text as a
    second call would, and ``redact`` returns what that call returns. One
    round alone does not."""
    once = _redact_once(text)
    assert _redact_once(once) != once
    assert redact(text) == expected
    assert redact(expected) == expected and credential_classes(expected) == ()


def test_a_quoted_value_is_redacted_whole_in_a_python_repr():
    """A Python repr writes ``'`` as ``\\'`` in a string that holds both
    quotes: the escaped quote opens and closes the value and stays."""
    value = """X_TOKEN='a b; c' beside "double" quotes"""
    out = redact(repr(value))
    assert "a b; c" not in out
    assert (
        ast.literal_eval(out)
        == redact(value)
        == """X_TOKEN='***REDACTED***' beside "double" quotes"""
    )


@pytest.mark.parametrize(
    "text",
    [
        "A" * 1_000_000,
        "A_" * 500_000,
        "A_TOKEN" * 140_000,
        "ABC_DEF_TOKEN " * 70_000,
        ("\\nA_TOKEN" + "_X" * 10) * 20_000,
        # Lower case (#285 review): an identifier, and values the code rule
        # refuses, each of its alternatives, a candidate after the other
        # (round 2), and the separator's spaces.
        "a" * 1_000_000,
        ("a_token=" + "12345678" * 10 + " ") * 10_000,
        ("a_token=" + "abcdefg1" * 10 + "(") * 10_000,
        ("a_token=a_token ") * 70_000,
        ("a_token=" + "a." * 50 + "a_token ") * 10_000,
        ("a_token=" + "a" * 100 + "_token ") * 10_000,
        ("a_token=" + "a." * 50 + "b ") * 10_000,
        "a_token=" + "a." * 500_000 + "a(",
        ("a_token=" + "a::" * 30 + "b<") * 10_000,
        ("a_token=None, ") * 70_000,
        ("a_token=$" + "A" * 100 + " ") * 10_000,
        ("a_token=%(" + "a" * 100 + ")s ") * 10_000,
        ("a_token=" + "." * 100 + " ") * 10_000,
        ("a_token={" + "a" * 100 + " ") * 10_000,
        "a_token" + ":" * 1_000_000,
        "a_token=" + " " * 1_000_000,
        ("a_token " + " " * 100 + ":") * 10_000,
    ],
    ids=[
        "one-identifier",
        "underscores",
        "suffixes-no-separator",
        "names-no-separator",
        "after-escapes",
        "lower-identifier",
        "number",
        "call",
        "same-name",
        "dotted-credential-name",
        "long-credential-name",
        "dotted-name",
        "long-dotted-call",
        "generic-path",
        "constant",
        "shell-variable",
        "format-field",
        "no-letter",
        "bracket",
        "separators",
        "spaces-after-separator",
        "spaces-before-separator",
    ],
)
def test_the_credential_suffix_pattern_stays_linear(text):
    """A candidate starts only where an identifier does and scans it to its
    end and the first word of the value after it, so a megabyte of
    identifiers and values the code rule refuses is redacted well inside the
    budget."""
    started = time.process_time()
    assert redact(text) == text
    assert time.process_time() - started < 3.0


@pytest.mark.parametrize(
    "text",
    [
        "a_token=" * 125_000,
        ("a_token=" + "abcdefgh" * 10 + " ") * 10_000,
        "a_token=" + "a-" * 500_000,
        ("a_token:" + "a." * 50) * 10_000,
        "a_token=x" + "&a" * 500_000,
        "a_token=x&" + "a" * 1_000_000,
        "a_token=x" + ")" * 1_000_000 + "x",
        ("a_token=x" + ")" * 100 + " ") * 10_000,
        ("a_token=x&b_token=" + "%2F" * 30) * 10_000,
        ('a_token="' + '"' * 3) * 80_000,
    ],
    ids=[
        "names-no-space",
        "letters",
        "long-word",
        "dotted-then-name",
        "form-fields",
        "one-long-ampersand-run",
        "trailing-run-inside",
        "trailing-runs",
        "form-bodies",
        "doubled-quotes",
    ],
)
def test_a_lower_case_word_of_any_characters_stays_linear(text):
    """#285 review, round 2: a lower-case name's unquoted word is now any
    characters, so it can hold the next candidate's name and separator. It
    is then taken whole, never read again, and a ``&`` or a run of trailing
    punctuation is read once more at most."""
    started = time.process_time()
    assert "***REDACTED***" in redact(text)
    assert time.process_time() - started < 3.0


@pytest.mark.parametrize(
    "text",
    [
        'A_TOKEN="' + "x " * 500_000,
        ('A_TOKEN="' + "x\\" * 10 + "'") * 20_000,
        # JSON text: ``\"`` opens and closes, ``\\\"`` is one inside.
        ('A_TOKEN=\\"' + 'a\\\\\\"' * 10 + '\\"') * 20_000,
        ('a_token="' + "\\" * 1_000 + "\n") * 300,
        'A_TOKEN="' + "\\" * 500_000,
        # Across lines (#285 review, round 2): a value that never closes, and
        # one that a quote of the other kind, a word after its quote or a
        # blank line stops, many lines on, a candidate after the other;
        # doubled quotes; and a candidate at every escaping level, whose
        # quotes each level below it reads as its value's.
        'A_TOKEN="' + "x\n" * 500_000,
        ('A_TOKEN="' + "x\n" * 10 + "'") * 50_000,
        ('A_TOKEN="' + "x\n" * 10 + '"a') * 50_000,
        ('a_token="' + "x\n" * 10 + "\n") * 50_000,
        'A_TOKEN="' + '""' * 500_000,
        'A_TOKEN="' + '""\n' * 300_000,
        "".join("A_TOKEN=" + "\\" * level + '"' + "x\n" * 1_000 for level in range(8)) * 50,
        # Round 3: lines that hold blank lines, the other quote and
        # backticks, which no longer stop a value, a candidate after the
        # other and of each quote in turn, so that each reads the other's.
        ('A_TOKEN="x' + "\n'`" * 10 + "\n\n") * 12_500,
        ('A_TOKEN="x' + "\n\n" * 10 + '"a') * 25_000,
        ("A_TOKEN=\"x\nB_TOKEN='y\n") * 25_000,
        ("'A_TOKEN=\"x\n\n" + "a\n" * 10) * 25_000,
    ],
    ids=[
        "one-long-value",
        "other-quotes",
        "escaped-levels",
        "backslash-runs",
        "backslashes",
        "lines-never-closed",
        "lines-other-quote",
        "lines-word-after-quote",
        "lines-blank-line",
        "doubled-quotes",
        "doubled-quotes-on-lines",
        "lines-at-every-level",
        "lines-anything",
        "lines-blank-lines",
        "lines-both-quotes",
        "lines-in-a-string",
    ],
)
def test_a_quoted_value_stays_linear(text):
    """A quoted value is read once to its close, its line's end or its
    string's: a backslash run whole, every alternative possessive. A value
    that spans lines and does not close (#285 review, round 2) is read once
    more to its line's end; the next candidate's quote closes it, unless
    more backslashes escape that quote, which no more than eight levels of
    candidates in a row can do."""
    started = time.process_time()
    redact(text)
    assert time.process_time() - started < 3.0


@pytest.mark.parametrize(
    "text",
    [
        "a_token:\n" * 200_000,
        "a_token: |\n" + "\n" * 1_000_000,
        ("a_token: |\n" + "  \n" * 100 + "x\n") * 5_000,
        "a_token:\n  " + "a " * 500_000 + ": x",
        ("a_token:\n  " + "a" * 100 + ":\n") * 10_000,
        "- " * 500_000 + "a_token:\n  ${X}",
        ('"' + " " * 100) * 10_000,
        ('"' + "- " * 50 + "a_token") * 10_000,
        "\\n" * 500_000,
        ("x_token:\\n  " + "\\\\" * 50 + '"') * 10_000,
        ("a_token:\n" + " " * 100 + "\n") * 10_000,
        "a_token:\n" + " " * 1_000_000 + "{",
        ("\x1b[0mapi_token:\n  $X\n") * 50_000,
    ],
    ids=[
        "keys-without-values",
        "header-then-blank-lines",
        "blank-lines-then-a-key",
        "words-then-a-mapping",
        "mapping-lines",
        "sequence-dashes",
        "quotes-then-spaces",
        "quotes-then-dashes",
        "written-out-line-breaks",
        "written-out-backslash-runs",
        "blank-indented-lines",
        "long-indentation",
        "references-after-escapes",
    ],
)
def test_a_value_on_the_lines_below_a_key_stays_linear(text):
    """#285 review, round 3: a candidate starts at a line's start, an escape
    or a quote, and reads its key, one line break and its value's lines, each
    once, so a megabyte of keys, blank lines, mappings and indentation that
    hold no value is redacted well inside the budget."""
    started = time.process_time()
    assert redact(text) == text
    assert time.process_time() - started < 3.0


@pytest.mark.parametrize(
    "text",
    [
        ("a_token:\n  " + "x " * 50 + "\n") * 5_000,
        ("a_token: |\n" + "  x\n" * 50) * 2_500,
        ("A_TOKEN=x\n") * 50_000,
        ('GH_TOKEN="x\nA_TOKEN: \'a "b" c\' d" e\n') * 10_000,
    ],
    ids=["plain-scalars", "blocks", "assignments", "cascades"],
)
def test_a_text_whose_values_are_redacted_stays_linear(text):
    """#285 review, round 3: a text with something to redact takes a second
    round, which reads it as a second call would; the rounds are bounded, so
    half a megabyte of values, each a cascade among them, stays inside the
    budget."""
    started = time.process_time()
    out = redact(text)
    assert "***REDACTED***" in out and redact(out) == out
    assert time.process_time() - started < 3.0


@pytest.mark.parametrize(
    "text",
    [
        # A quote that ends its line (``_quoted_from_next_line``): every line
        # a candidate, closed strings and an open one before the name, a
        # value that never closes or that a word after a quote stops, space
        # before the value, and backslash pairs before the name.
        'a_token="\n' * 200_000,
        ("x = 'a' " * 100 + 'a_token="\nx"\n') * 5_000,
        ("'" + "a " * 100 + 'a_token="\nx"\n') * 5_000,
        'a_token="\n' + "x\n" * 500_000,
        ('a_token="\n' + "x\n" * 10 + '"a\n') * 50_000,
        'a_token="\n' + " " * 1_000_000,
        "\\" * 1_000_000 + 'a_token="\nx"',
        # Below a YAML key: comment and blank lines before no value and
        # before one, blank lines after a plain scalar, a block of
        # placeholders and one with a line that is none.
        ("a_token:\n" + "  # c\n" * 100) * 5_000,
        ("a_token:\n" + "  # c\n\n" * 50 + "  x\n") * 5_000,
        ("a_token:\n  x\n" + "\n" * 100) * 5_000,
        ("a_token: |\n" + "  $X\n" * 100) * 5_000,
        ("a_token: |\n" + "  $X\n" * 100 + "  x\n") * 5_000,
        # A plain scalar from its key's line: one that goes on, words and
        # blank lines with no line after them, code, a line written out
        # behind many backslashes.
        ("a_token: x\n" + "  y\n" * 100) * 5_000,
        ("a_token: " + "a " * 100 + "\n" + "\n" * 100) * 5_000,
        ("a_token: str\n  " + "a " * 50 + "\n") * 10_000,
        ("a_token: " + "\\" * 100 + "n\n  y\n") * 10_000,
    ],
    ids=[
        "quote-break-lines",
        "closed-strings-before",
        "open-string-before",
        "never-closed",
        "word-after-quote",
        "space-only",
        "backslash-pairs",
        "comments-no-value",
        "comments-then-value",
        "blank-lines-after",
        "placeholder-blocks",
        "placeholders-then-a-line",
        "continued",
        "words-then-blank-lines",
        "continued-code",
        "written-out-backslash-runs",
    ],
)
def test_the_fourth_review_patterns_stay_linear(text):
    """#285 review, round 4: a quoted value from the next line starts at a
    line's start and reads the line once to its name and the value once; a
    YAML key's candidate reads the blank and comment lines before its value
    and the blank lines among its lines once, and a block's placeholder
    check its lines once more. A megabyte of each is redacted well inside
    the budget, and the output is stable."""
    started = time.process_time()
    out = redact(text)
    assert time.process_time() - started < 3.0
    assert redact(out) == out


@pytest.mark.parametrize(
    "text",
    [
        # A plain scalar's first line holding quotes: the break's scan from
        # every quote-led key to the next one's ``: `` on one line, a line of
        # quotes that goes on and one that does not, a quote-led key whose
        # string closes, and a line written out behind many backslashes.
        ('"a_token: x' * 1_000 + "\n") * 100,
        ("a_token: " + 'x"' * 50 + "\n  y\n") * 10_000,
        ("a_token: " + 'x"' * 100 + "\n") * 5_000,
        ('"a_token: ' + "x" * 100 + '"\n  y\n') * 10_000,
        ("a_token: x" + "\\" * 100 + '"\\n  y\n') * 10_000,
        # Properties: many on a line, an anchor before nothing, a long tag
        # below a key, and a doubled quote after a quote's line break.
        ("a_token: &a !b " * 1_000 + "\n") * 100,
        ("a_token: &" + "a" * 100 + "\n") * 10_000,
        ("a_token:\n  !" + "x" * 100 + " y\n") * 10_000,
        ("a_token='\n''" + "\n") * 100_000,
        # Sequence dashes past the widths told apart, every width, and one
        # long line of dashes.
        ("- " * 20 + "a_token: |\n" + " " * 50 + "x\n") * 10_000,
        "".join("- " * n + "a_token: |\n" + "  " * (n + 1) + "x\n" for n in range(1, 20)) * 1_000,
        "- " * 500_000 + "a_token: |\n x",
    ],
    ids=[
        "quote-led-keys-on-a-line",
        "quotes-then-a-line",
        "quotes-and-no-line",
        "quote-led-closed-string",
        "written-out-backslash-runs",
        "properties-on-a-line",
        "anchor-before-nothing",
        "long-tag-below",
        "doubled-quote-after-break",
        "wide-dashes",
        "every-width",
        "a-line-of-dashes",
    ],
)
def test_the_fifth_review_patterns_stay_linear(text):
    """#285 review, round 5: a plain scalar's first line is scanned once to
    its break, which a mapping's ``: `` stops, so the next candidate's key
    ends it; a property is read once; the dashes before a key are told
    apart by their width once. A megabyte of each is redacted well inside
    the budget, and the output is stable."""
    started = time.process_time()
    out = redact(text)
    assert time.process_time() - started < 3.0
    assert redact(out) == out


@pytest.mark.parametrize(
    "text",
    [
        # Properties on lines of their own: comment lines after one before
        # no value, properties before no node, and before a mapping.
        ("a_token:\n  &a\n" + "  # c\n" * 100) * 2_000,
        "a_token:\n  &a\n  !b\n  &c\n" * 40_000,
        "a_token:\n  &a\n  !b\n  k: v\n" * 40_000,
        # A header below the key: blank lines and no block, a block of
        # placeholders below properties, and one of values.
        "a_token:\n  |\n" + "\n" * 1_000_000,
        ("a_token:\n  &a\n  |\n" + "  $X\n" * 100) * 2_000,
        ("a_token:\n  &a\n  |\n" + "  x\n" * 100) * 2_500,
        # Dashes measured in binary: a dash and a million spaces, siblings
        # behind a hundred, a run past the top run's lowest bit, and keys
        # nested ever deeper, each a block's line for the one before.
        "-" + " " * 1_000_000 + "a_token: |\n x",
        ("-" + " " * 100 + "a_token: |\n" + " " * 102 + "x\n" + " " * 101 + "e: 1\n") * 3_000,
        "- " * (1 << 19) + "a_token: |\n x",
        "".join(" " * (18 * i) + "- " * 9 + "a_token: |\n" for i in range(300)) + " " * 6000 + "x",
        # A line break written out crossed: many with no value, each with
        # an indentation, many before a value, and a quote after each.
        "GH_TOKEN=" + "\\n" * 500_000,
        ("GH_TOKEN=\\n" + " " * 100) * 10_000,
        "GH_TOKEN=" + "\\n  " * 250_000 + "x",
        'GH_TOKEN=\\n  "' * 50_000,
    ],
    ids=[
        "comments-after-a-property",
        "properties-before-no-node",
        "properties-before-a-mapping",
        "header-below-then-blank-lines",
        "header-below-placeholders",
        "header-below-values",
        "dash-then-spaces",
        "wide-dash-siblings",
        "top-run-of-bits",
        "nested-wide-entries",
        "written-out-breaks",
        "written-out-indentation",
        "written-out-breaks-then-a-value",
        "written-out-break-then-a-quote",
    ],
)
def test_the_sixth_review_patterns_stay_linear(text):
    """#285 review, round 6: properties and a header on lines of their own
    are read once by a key's candidate, a key's dashes are read a few times
    for their bits, and a separator crosses each line break written out
    once. A megabyte of each is redacted well inside the budget, and the
    output is stable."""
    started = time.process_time()
    out = redact(text)
    assert time.process_time() - started < 3.0
    assert redact(out) == out


@pytest.mark.parametrize(
    "text",
    [
        # A line starts after each carriage return: a megabyte of them with
        # no key, after a key with no value, keys with values, blocks of
        # values and of placeholders, a plain scalar going on below its key.
        "x\r" * 500_000,
        "a_token:\r" + "\r" * 1_000_000,
        "a_token:\r  x\r" * 40_000,
        ("a_token: |\r" + "  x\r" * 100) * 2_500,
        ("a_token: |\r" + "  $X\r" * 100) * 2_000,
        "a_token: x\r" + "  y\r" * 250_000,
        # Properties and comment lines ended by carriage returns, a quote
        # that ends its line, a key behind wide dashes, CRLF and CR mixed.
        ("a_token:\r  &a\r" + "  # c\r" * 100) * 2_000,
        "a_token: '\r" * 100_000,
        "- " * (1 << 19) + "a_token: |\r x",
        "a_token:\r\n  x\r  y\n" * 30_000,
        # Written out: a carriage return crossed after a separator, many
        # with no value, and a YAML text's lines ended by them.
        "GH_TOKEN=" + "\\r" * 500_000,
        ("GH_TOKEN=\\r" + " " * 100) * 10_000,
        "a_token:\\r  x\\r" * 35_000,
    ],
    ids=[
        "line-starts",
        "blank-lines-after-a-key",
        "keys-with-values",
        "blocks-of-values",
        "blocks-of-placeholders",
        "plain-scalar-going-on",
        "comments-after-a-property",
        "quotes-that-end-their-lines",
        "wide-dashes",
        "crlf-and-cr-mixed",
        "written-out-carriage-returns",
        "written-out-indentation",
        "written-out-yaml",
    ],
)
def test_the_seventh_review_patterns_stay_linear(text):
    """#285 review, round 7: a carriage return alone breaks a line, and a
    line starts after one, but each candidate still reads its key, one line
    break and its value's lines once, and a separator crosses each line
    break written out once. A megabyte of each (half of one where every
    line holds a value) is redacted well inside the budget, and the output
    is stable."""
    started = time.process_time()
    out = redact(text)
    assert time.process_time() - started < 3.0
    assert redact(out) == out


@pytest.mark.parametrize(
    "text",
    [
        # A flow collection's indicators with no name after them, names with
        # no colon, keys with no value below them, blank lines, comment
        # lines and indentation alone below one, and space before a key.
        "," * 1_000_000,
        ", a_token" * 100_000,
        "{a_token:\n" * 100_000,
        "{a_token:\n" + "\n" * 1_000_000,
        "{a_token:\n" + "  # c\n" * 150_000,
        (", a_token:\n" + " " * 100) * 10_000,
        "{" + " " * 1_000_000 + "a_token:\n x}",
        "[" + "\n" * 400_000 + "a_token:\n x]",
        # Values: plain scalars of many lines, of many words, words then
        # blank lines with no line after them, one long line with no break,
        # quoted ones that never close, and the same written out.
        ("{a_token:\n  x\n" + "  y\n" * 100 + "}") * 3_000,
        ("{a_token: x\n" + "  y\n" * 100 + "}") * 3_000,
        ("{a_token: " + "a " * 100 + "\n" + "\n" * 100) * 3_000,
        "{a_token: x" + " y" * 500_000,
        "{a_token:\n  '" + "x\n" * 500_000,
        "{a_token:\\n  x\\n" * 60_000,
        ("{a_token:\\n" + "\\n" * 100 + "  x}") * 3_000,
        # Names behind underscores: a megabyte of underscores, runs of them
        # before a letter, each after a space, and long names of them.
        "_" * 1_000_000,
        ("_" * 100 + "A") * 10_000,
        " _" * 500_000,
        ("__A" * 100 + "_TOKEN=x ") * 3_000,
    ],
    ids=[
        "commas",
        "names-no-colon",
        "keys-no-value",
        "blank-lines-below-a-key",
        "comment-lines-below-a-key",
        "indentation-alone",
        "spaces-before-a-key",
        "line-breaks-before-a-key",
        "plain-scalars-of-many-lines",
        "continued-scalars",
        "words-then-blank-lines",
        "one-long-line",
        "quoted-never-closed",
        "written-out-values",
        "written-out-blank-lines",
        "underscores",
        "underscore-runs-before-a-letter",
        "spaced-underscores",
        "long-underscore-names",
    ],
)
def test_the_eighth_review_patterns_stay_linear(text):
    """#285 review, round 8: a flow collection's candidate starts at a
    ``{``, a ``[`` or a ``,``, reads the space after it and one identifier,
    then one line break and its value's lines, each once; a name's run of
    underscores is read once. A megabyte of each is redacted well inside the
    budget, and the output is stable."""
    started = time.process_time()
    out = redact(text)
    assert time.process_time() - started < 3.0
    assert redact(out) == out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{client_secret: FAKE}", "{client_secret: ***REDACTED***}"),
        ("f(x_token=FAKE),", "f(x_token=***REDACTED***),"),
        ("refresh_token=FAKE}", "refresh_token=***REDACTED***}"),
        ("client_secret: None,", "client_secret: None,"),
        ("x_token: config.auth.tok,", "x_token: config.auth.tok,"),
        ("api_token: str):", "api_token: str):"),
    ],
    ids=str,
)
def test_punctuation_before_an_escaped_quote_ends_a_word_at_every_depth(text, expected):
    """#285 review, round 8: in JSON in JSON a string's closing quote is
    ``\\"``, and the punctuation before it (``}``, ``,``, ``)``) was read as
    a word's, since a backslash went on with it: a flow mapping's ``}`` went
    with the value, and code (``None,``) no longer read as code and was
    redacted. A backslash that starts an escaped quote or a line break
    written out ends the word, so each depth decodes to what redacting the
    text gives."""
    assert redact(text) == expected
    for depth in (1, 2, 3):
        out = redact(_json_string(text, depth))
        decoded = out
        for _ in range(depth):
            decoded = json.loads(decoded)
        assert decoded == expected
        assert redact(out) == out


def test_redact_obj_redacts_a_285_shape_at_any_depth():
    """State and the CLI's documents cross ``redact_obj``: each #285 shape is
    redacted in a nested value, a list and a mapping key. A value under a
    prefixed name is redacted where the string holds the assignment, and
    where a mapping's key names it (below)."""
    value = {
        "env": [text for _, text, _ in SHAPES_285],
        f"key {GROQ_API_KEY}": {"nested": PRIVATE_KEY},
        "vars": {"DEEPSEEK_API_KEY": SUFFIX_SECRET},
    }
    out = redact_obj(value)
    rendered = repr(out)
    for _, _, body in SHAPES_285:
        assert body not in rendered
    assert out == {
        "env": ["***REDACTED***"] * 7 + ["DEEPSEEK_API_KEY=***REDACTED***"],
        "key ***REDACTED***": {"nested": "***REDACTED***"},
        "vars": {"DEEPSEEK_API_KEY": "***REDACTED***"},
    }


def test_redact_obj_redacts_a_value_under_a_credential_name_key():
    """#285 review: ``redact_obj`` redacts a mapping's key and its value
    apart, and neither ``GEMINI_API_KEY`` alone nor an opaque value alone is
    a credential, so ``{"GEMINI_API_KEY": "..."}`` (an ``env`` mapping in a
    record, a free-form ``escalation``, a CLI document) kept the value in the
    clear. A key that names a credential, a prefixed name with a credential
    suffix in any case or a well-known name, has its non-empty string value
    replaced whole, at any depth; the key and every other entry stay."""
    value = {
        "env": {
            "GEMINI_API_KEY": SUFFIX_SECRET,
            "DEEPSEEK_API_KEY": "x",
            "MY_GITHUB_TOKEN": "two words; and more",
            "CI_JOB_TOKEN": "FAKE",
            "OAUTH_CLIENT_SECRET": "p@ss!",
            "DB_PASSWORD": "hunter2",
            "GITHUB_TOKEN": SUFFIX_SECRET,
            "aws_secret_access_key": SUFFIX_SECRET,
            "HOME": "/home/u",
        },
        "installed": {"client_id": "1.apps", "client_secret": "abc", "Db_Password": "p w"},
        "steps": [{"with": {"npm_token": "short"}}, {"run": "make"}],
        "count": 2,
    }
    out = redact_obj(value)
    assert out == {
        "env": {
            "GEMINI_API_KEY": _R,
            "DEEPSEEK_API_KEY": _R,
            "MY_GITHUB_TOKEN": _R,
            "CI_JOB_TOKEN": _R,
            "OAUTH_CLIENT_SECRET": _R,
            "DB_PASSWORD": _R,
            "GITHUB_TOKEN": _R,
            "aws_secret_access_key": _R,
            "HOME": "/home/u",
        },
        "installed": {"client_id": "1.apps", "client_secret": _R, "Db_Password": _R},
        "steps": [{"with": {"npm_token": _R}}, {"run": "make"}],
        "count": 2,
    }
    assert SUFFIX_SECRET not in repr(out) and "hunter2" not in repr(out)
    assert redact_obj(out) == out


def test_redact_obj_leaves_a_key_that_names_no_credential_and_keeps_every_entry():
    """The key must name the credential whole: a longer name, a plural, a
    suffix the rule does not take, a bare generic name (whose text rule wants
    a token-length value) and a key with no prefix are ordinary data, and an
    empty or non-string value has nothing to hide. A key that redacts to the
    same text as another is still suffixed, never dropped."""
    value = {
        "GITHUB_TOKEN_FILE": "/run/secrets/token",
        "MY_TOKEN_PATH": "/x",
        "max_tokens": "4096",
        "input_tokens": 12,
        "tokens": ["a", "b"],
        "token": "short",
        "SSH_KEY": "/home/u/.ssh/id_ed25519",
        "_TOKEN": "x",
        "api_key_id": "k-1",
        "page_token": "",
        "DB_PASSWORD": None,
        "MY_SECRET": 7,
        "CI_JOB_TOKEN": {"nested": "kept"},
    }
    assert redact_obj(value) == value
    out = redact_obj({"GITHUB_TOKEN=a": "1", "GITHUB_TOKEN=b": "2", "X_TOKEN": "3"})
    assert out == {
        "GITHUB_TOKEN=***REDACTED***": "1",
        "GITHUB_TOKEN=***REDACTED***#2": "2",
        "X_TOKEN": _R,
    }


def test_redact_obj_keeps_the_growth_bound_for_a_credential_key():
    """A value under a credential key becomes the 14-character marker, so a
    short one grows; the key comes with it, and the shortest key that names
    a credential is six characters (``access``), so an entry, key and value,
    still grows less than ``MAX_GROWTH_FACTOR``: 7 to 20 at worst."""
    for key in ("access", "a_token", "A_TOKEN", "GH_TOKEN", "id_token"):
        out = redact_obj({key: "x"})
        assert out == {key: _R}
        assert len(key) + len(_R) <= MAX_GROWTH_FACTOR * (len(key) + 1)
        serialized = json.dumps({key: "x"})
        assert len(json.dumps(out)) <= MAX_GROWTH_FACTOR * len(serialized)


def test_no_parser_bounded_field_is_a_credential_key():
    """The persisted bounds that depend on ``MAX_GROWTH_FACTOR``
    (``loop_guard.MAX_REQUIRED_RESOLUTION_CHARS``, the stored rationale) hold
    for a finding's and a resolution's strings because no key of theirs
    names a credential: such a key's value would be replaced by the marker,
    which no factor bounds for a one-character value."""
    import dataclasses

    from autoforge import redaction
    from autoforge.result_parser import Finding, FindingResolution, LocalFindingResolution

    keys = {
        *Finding(id="R1-F1", classification="blocked", required_resolution="x").to_dict(),
        *FindingResolution(finding_id="R1-F1", resolution="fixed").to_dict(),
        *LocalFindingResolution(finding_id="R1-F1", resolution="fixed").to_dict(),
    }
    for cls in (Finding, FindingResolution, LocalFindingResolution):
        keys.update(f.name for f in dataclasses.fields(cls))
    assert {"required_resolution", "rationale", "title"} <= keys
    assert [k for k in keys if redaction._CREDENTIAL_KEY.fullmatch(k)] == []


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
    keys; a per-text counter keeps the whole pass well under the budget.

    The budget is CPU time, the work this process did: other processes on
    the host (pytest-xdist's other workers) stretch the wall time without
    changing it, to three times its serial second under ``-n 4`` (#340)."""
    count = 20_000
    value = {f"GITHUB_TOKEN=secret{i}": i for i in range(count)}
    started = time.process_time()
    out = redact_obj(value)
    assert time.process_time() - started < 3.0
    assert isinstance(out, dict) and len(out) == count
    assert list(out.values()) == list(range(count))
    assert all(key.startswith("GITHUB_TOKEN=***REDACTED***") for key in out)
    assert "secret" not in "".join(out)


def test_redact_obj_redacts_each_string_never_the_serialized_document():
    """#206: a pattern run over a document's JSON text consumes the syntax
    around a value (an empty assignment takes the closing quote; until #281 a
    value before an escaped quote took its backslash) and, until #284, missed
    a secret right after an escape, which follows a letter or a digit there.
    Each string is redacted on its own."""
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
    ("google-api-key", GOOGLE_API_KEY, "***REDACTED***", "google-api-key"),
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
    # The classes #285 adds.
    *(
        (name, text, "***REDACTED***", name)
        for name, text, _ in SHAPES_285
        if name != "credential-suffix-assignment"
    ),
    (
        "credential-suffix-assignment",
        SUFFIX_ASSIGNMENT,
        "DEEPSEEK_API_KEY=***REDACTED***",
        "credential-suffix-assignment",
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
# before a letter that makes no escape. A name behind a prefix is an
# identifier of its own in any case (#285, ``credential-suffix-assignment``),
# so none is listed here.
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


# Each assignment shape that takes a quoted value, with a dummy one (#281).
_QUOTED_VALUE = "dummy-F2-value-0123456789"
_QUOTED_ASSIGNMENTS = [
    ("env-assignment", f'GH_TOKEN="{_QUOTED_VALUE}"'),
    ("env-assignment", f"OPENAI_API_KEY = '{_QUOTED_VALUE}'"),
    ("secret-assignment", f'password: "{_QUOTED_VALUE}"'),
    ("oauth-assignment", f'refresh_token="{_QUOTED_VALUE}"'),
    ("oauth-bare-assignment", f'access="{_QUOTED_VALUE}"'),
    # The name as a quoted key (#393).
    ("env-assignment", f'"GITHUB_TOKEN": "{_QUOTED_VALUE}"'),
    ("secret-assignment", f'"api_key": "{_QUOTED_VALUE}"'),
    ("authorization-header", f'"Authorization": "Bearer {_QUOTED_VALUE}"'),
    # A prefixed credential name (#285), as an assignment and a quoted key.
    ("credential-suffix-assignment", f'GEMINI_API_KEY="{_QUOTED_VALUE}"'),
    ("credential-suffix-assignment", f'"DB_PASSWORD": "{_QUOTED_VALUE}"'),
]


_ENV_NAME_LIST = _ENV_NAMES.split("|")


def _json_string(text, depth):
    for _ in range(depth):
        text = json.dumps(text)
    return text


@pytest.mark.parametrize("prefix", ["", "push failed:\n"], ids=["start", "after-newline"])
@pytest.mark.parametrize("depth", [1, 2, 3], ids=["json", "json-in-json", "three-deep"])
@pytest.mark.parametrize(
    ("name", "text"), _QUOTED_ASSIGNMENTS, ids=[text for _, text in _QUOTED_ASSIGNMENTS]
)
def test_a_quoted_value_is_redacted_behind_an_escaped_quote(name, text, depth, prefix):
    """F2 (#281): JSON writes a quote as ``\\"``, and a result printed or
    captured as JSON text read ``GH_TOKEN=\\"...\\"``: the value stopped at
    the backslash and the secret after it was kept. The quote is taken behind
    the backslashes of up to three levels of JSON in JSON and kept, so the
    redacted text still decodes, to what redacting the string itself gives."""
    escaped = _json_string(prefix + text, depth)
    out = redact(escaped)
    assert _QUOTED_VALUE not in out
    decoded = out
    for _ in range(depth):
        decoded = json.loads(decoded)
    assert decoded == redact(prefix + text)
    assert credential_classes(escaped) == (name,)
    assert redact(out) == out


@pytest.mark.parametrize(
    ("name", "text"), _QUOTED_ASSIGNMENTS, ids=[text for _, text in _QUOTED_ASSIGNMENTS]
)
def test_a_quoted_value_is_redacted_in_a_python_repr(name, text):
    """A Python repr of a string that holds both quotes writes ``'`` as
    ``\\'``; that escape is taken and kept the same way."""
    value = f"""{text} beside "double" and 'single' quotes"""
    out = redact(repr(value))
    assert _QUOTED_VALUE not in out
    assert ast.literal_eval(out) == redact(value)
    assert credential_classes(repr(value)) == (name,)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A backslash inside a value is still part of it.
        ("GH_TOKEN=C:\\dir\\x1 done", "GH_TOKEN=***REDACTED*** done"),
        # One that starts a line break written out starts no value (#285
        # review, round 6): the break is crossed, kept, and the value after
        # it replaced, as after a line break written as such.
        ("GH_TOKEN=\\nabc", "GH_TOKEN=\\n***REDACTED***"),
        # One that starts an escaped quote ends the value and is kept.
        ('GH_TOKEN=abc\\"rest', 'GH_TOKEN=***REDACTED***\\"rest'),
        ('"GH_TOKEN=abc\\\\\\"rest"', '"GH_TOKEN=***REDACTED***\\\\\\"rest"'),
        # An escaped quote with no value has nothing to redact, and the JSON
        # around it is no value either.
        ('{"a": "GH_TOKEN=\\"", "b": 1}', '{"a": "GH_TOKEN=\\"", "b": 1}'),
    ],
    ids=["path", "written-out-newline", "escaped-quote", "json-in-json-quote", "empty"],
)
def test_an_escaped_quote_ends_a_value_and_a_backslash_inside_one_stays(text, expected):
    assert redact(text) == expected
    assert redact(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "GH_TOKEN=" + "\\" * 250_000,
        'GH_TOKEN=\\"' * 50_000,
        "access=" + "\\" * 250_000,
        "refresh_token=" + "\\" * 6 + "x" * 50_000 + "\\" * 6 + "y" * 50_000,
        "password=" + ("\\" * 7 + '"') * 30_000,
    ],
    ids=["value-backslashes", "escaped-quotes", "bare-backslashes", "runs", "quote-runs"],
)
def test_the_escaped_quote_rule_stays_linear(text):
    """The backslashes before a quote and the lookahead past one inside a
    value are bounded, so a quarter-megabyte of them is redacted well inside
    the budget."""
    started = time.perf_counter()
    redact(text)
    assert time.perf_counter() - started < 3.0


# A credential's name as a quoted key (#393), each with what ``redact`` makes of
# it and the class ``credential_classes`` reports. Every value is a fake.
_QUOTED_KEY_CASES = [
    (
        '{"GITHUB_TOKEN": "FAKE0opaque0value"}',
        '{"GITHUB_TOKEN": "***REDACTED***"}',
        "env-assignment",
    ),
    ("{'GH_TOKEN': 'FAKE0opaque0value'}", "{'GH_TOKEN': '***REDACTED***'}", "env-assignment"),
    # A TOML quoted key, and a key in any case with space before its colon.
    ('"OPENAI_API_KEY" = "FAKE0opaque"', '"OPENAI_API_KEY" = "***REDACTED***"', "env-assignment"),
    ('{"github_token" : "FAKE0opaque"}', '{"github_token" : "***REDACTED***"}', "env-assignment"),
    # YAML: a quoted key, a plain value.
    ('"HF_TOKEN": FAKE0opaque', '"HF_TOKEN": ***REDACTED***', "env-assignment"),
    ('{"api_key": "FAKE0opaque0value"}', '{"api_key": "***REDACTED***"}', "secret-assignment"),
    ('{"password":"FAKE0opaque0value"}', '{"password":"***REDACTED***"}', "secret-assignment"),
    (
        '{"Authorization": "Bearer FAKE0opaque"}',
        '{"Authorization": "Bearer ***REDACTED***"}',
        "authorization-header",
    ),
    (
        "{'authorization': 'Basic RkFLRTpGQUtF'}",
        "{'authorization': 'Basic ***REDACTED***'}",
        "authorization-header",
    ),
    # A prefixed name (#285), which ``credential-suffix-assignment`` takes.
    (
        '{"MY_GITHUB_TOKEN": "FAKE0opaque0value"}',
        '{"MY_GITHUB_TOKEN": "***REDACTED***"}',
        "credential-suffix-assignment",
    ),
    (
        "{'GEMINI_API_KEY': 'FAKE0opaque'}",
        "{'GEMINI_API_KEY': '***REDACTED***'}",
        "credential-suffix-assignment",
    ),
    # In lower case too since #285's review: a quoted value is a literal.
    (
        '{"my_github_token": "FAKE0opaque0value"}',
        '{"my_github_token": "***REDACTED***"}',
        "credential-suffix-assignment",
    ),
]


@pytest.mark.parametrize(
    ("text", "expected", "name"), _QUOTED_KEY_CASES, ids=[c[0] for c in _QUOTED_KEY_CASES]
)
def test_a_credential_under_a_quoted_key_is_redacted(text, expected, name):
    """#393: JSON, a Python mapping's repr, a TOML quoted key and YAML write
    a credential's name as a quoted key, and the key's closing quote stood
    between the name and the separator: a value of no known shape was kept
    in the clear, and a correction prompt that quoted such a reply passed it
    to the next agent. The quote is taken with the separator and kept."""
    assert redact(text) == expected
    assert credential_classes(text) == (name,)
    assert redact(expected) == expected


_QUOTED_KEY_NEAR_MISSES = [
    '"GITHUB_TOKEN" is set in the environment',
    '{"GITHUB_TOKEN_FILE": "/run/secrets/token"}',
    # A prefixed name in lower case with no literal value (#285 review).
    '{"my_github_token": null, "page_token": ""}',
    '{"GITHUB_TOKEN": "", "next": 1}',
    '{"token": "short"}',
    '{"Authorization": "none"}',
]


@pytest.mark.parametrize("text", _QUOTED_KEY_NEAR_MISSES)
def test_a_quoted_key_with_no_value_of_its_own_is_left(text):
    assert redact(text) == text


# An empty assignment at the end of a fenced text, as a correction prompt
# quotes a reply that ended in one (#393).
_EMPTY_ASSIGNMENT_ENDINGS = [
    "GITHUB_TOKEN=",
    "GH_TOKEN: ",
    '"GITHUB_TOKEN": ',
    "HF_TOKEN=\r",
    "Authorization: Bearer",
    '"Authorization": "Bearer ',
    "id_token=",
    "password:",
]


@pytest.mark.parametrize("ending", _EMPTY_ASSIGNMENT_ENDINGS)
def test_a_code_fence_on_a_later_line_is_never_a_value(ending):
    """#393: the space after an assignment's separator ran across the line
    break, so redacting a prompt that quoted a reply ending in an empty
    ``GITHUB_TOKEN=`` took the closing fence for its value, and ``prompt.md``
    no longer showed the prompt as sent. A line that starts a code fence,
    indented or not, of backticks or tildes, is never a value; the fenced
    text is a fixed point of ``redact``."""
    from autoforge.prompts import fenced_untrusted_block

    block = fenced_untrusted_block(f"Done.\n{ending}", "text")
    assert redact(block) == block
    for fence in ("```", "````", "   ```", "~~~"):
        text = f"{ending}\n{fence}\nmore"
        assert redact(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A value on a later line is still a value.
        ("GITHUB_TOKEN:\n  FAKE0opaque", "GITHUB_TOKEN:\n  ***REDACTED***"),
        ("Authorization: Bearer\n  FAKE0opaque", "Authorization: Bearer\n  ***REDACTED***"),
        ("id_token:\r\n\tFAKE0opaque", "id_token:\r\n\t***REDACTED***"),
        # Two backticks start no fence.
        ("GITHUB_TOKEN:\n``FAKE0opaque", "GITHUB_TOKEN:\n***REDACTED***"),
        # On the same line a value is a value, whatever it starts with.
        ("GITHUB_TOKEN=```FAKE0opaque", "GITHUB_TOKEN=***REDACTED***"),
    ],
    ids=["later-line", "header-later-line", "crlf-tab", "two-backticks", "same-line"],
)
def test_a_value_that_starts_no_fence_line_is_still_redacted(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "GH_TOKEN=" + "\n " * 125_000 + "```",
        "Authorization: Bearer" + "\r\n" * 125_000 + "~~~",
        ("id_token:\n```" + "\n") * 25_000,
        '"GITHUB_TOKEN"\\\\\\\\' * 25_000,
    ],
    ids=["one-long-run", "header-crlf-run", "many-fences", "many-escaped-quoted-keys"],
)
def test_the_quoted_key_and_fence_rules_stay_linear(text):
    """Each line break after a separator is tried once, and the key's quote
    is bounded like a value's, so a quarter-megabyte is redacted well inside
    the budget."""
    started = time.perf_counter()
    assert redact(text) == text
    assert time.perf_counter() - started < 3.0


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
    ``oauth-field``, which a quote starts, the six patterns that start at a
    line's start, the two YAML value patterns (#285 review, round 3) and the
    two of round 4 on each side of them, which take an escape before a name
    as a line's start or a token's, and the four that start at a YAML flow
    collection's ``{``, ``[`` or ``,`` (round 8), which no escape ends, have
    no ``after_escape`` pass."""
    line_patterns = ["env-assignment", "credential-suffix-assignment"] * 5
    assert [
        (index, entry.name) for index, entry in enumerate(_PATTERNS) if entry.after_escape is None
    ] == [
        (17, "oauth-field"),
        *((len(_PATTERNS) - 10 + i, name) for i, name in enumerate(line_patterns)),
    ]
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
# the separator has a marginally higher ratio (``A_TOKEN=x``, 9 to 22, and
# ``HF_TOKEN=x``, 10 to 23, are pinned by the wrapping tests); the URL shape
# needs no separator (``@`` ends a word) and is the worst text of all, 7 to
# 20; no text grows past it.
_WORST_CASE_UNITS = {
    "named_assignment": "HF_TOKEN=x;",
    "named_assignment_quoted": 'HF_TOKEN="x"',
    # The quote as JSON writes it (#281): kept like a plain one, 14 to 27.
    "named_assignment_escaped_quote": 'HF_TOKEN=\\"x\\"',
    # The name as a quoted key (#393), longer still: 14 to 27.
    "named_assignment_quoted_key": '"HF_TOKEN":"x"',
    "bearer_header": "Authorization:Token x ",
    "bearer_header_quoted_key": '"Authorization":"Token x"',
    "basic_header": "Authorization:Basic x ",
    "url_credential": "ab://x@",
    # Header at its ten-character floor, empty payload and signature; ``.``
    # ends the shape, so it repeats without a separator.
    "jwt": "eyJaaaaaaa..",
    # Fixed at 39 characters, so it only shrinks: 40 to 15.
    "google_api_key": "AIza" + "a" * 35 + " ",
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
    # #285: the shortest name of all, a one-letter prefix and a credential
    # suffix, 10 to 23 (``A_TOKEN=x`` alone, 9 to 22, is the bare shape);
    # every token shape is longer than the marker and only shrinks.
    "credential_suffix_assignment": "A_TOKEN=x;",
    "credential_suffix_assignment_quoted_key": '"A_TOKEN":"x"',
    # A quoted value is taken whole (#285 review): at one character it is
    # the worst, 11 to 24, whatever the quote; a lower-case name with a
    # quoted value is as long. Its unquoted word is any word that does not
    # read as code (round 2), one character too: 10 to 23, as for an
    # upper-case name. A quoted value that is one line break, or a doubled
    # quote, is no shorter.
    "credential_suffix_assignment_quoted_lower": 'a_token="x"',
    "credential_suffix_assignment_escaped_lower": "a_token=\\'x\\'",
    "credential_suffix_assignment_word_lower": "a_token=x;",
    # A value spans lines whatever they hold (round 3), but its first line
    # holds something: one character and a line break, 13 to 25.
    "credential_suffix_assignment_quoted_line_break": 'A_TOKEN="x\n" ',
    "credential_suffix_assignment_quoted_blank_line": "a_token='x\n\n' ",
    # A value on the lines below its key (round 3): a plain scalar of one
    # character, 12 to 25, a quoted one, 14 to 27, a block's one line, 14 to
    # 27, and a well-known name's plain scalar, longer still.
    "yaml_value": "a_token:\n x\n",
    "yaml_value_quoted": 'a_token:\n "x"\n',
    "yaml_value_block": "A_TOKEN: |\n x\n",
    "yaml_value_well_known_name": "HF_TOKEN:\n x y\n",
    # Round 4: a value below a blank line, 13 to 26, a comment line making
    # it longer; a quoted value whose quote ends its line, one character
    # and the line break, 13 to 25, and under a well-known name; a plain
    # scalar that starts on its key's line and goes on below it, 14 to 24.
    "yaml_value_after_blank_line": "a_token:\n\n x\n",
    "quoted_from_next_line": 'a_token="\nx"\n',
    "quoted_from_next_line_well_known_name": 'HF_TOKEN="\nx"\n',
    "yaml_value_continued": "a_token: x\n y\n",
    "yaml_value_continued_well_known_name": "HF_TOKEN: x\n y\n",
    "credential_suffix_assignment_doubled_quote": "a_token='''' ",
    # Round 5: a scalar after an anchor, which stays, 14 to 27; a quoted
    # value whose next line starts with the quote doubled, 14 to 25; a plain
    # scalar whose first line holds a quote, 15 to 24; a value in a sequence
    # entry, its dashes and indentation making it longer, 16 to 29.
    "yaml_property_value": "a_token: &a x\n",
    "quoted_from_next_line_doubled_quote": "a_token='\n'''\n",
    "yaml_value_continued_quote": 'a_token: x"\n y\n',
    "yaml_value_in_a_sequence": "- a_token:\n   x\n",
    # Round 6: properties and a block's header on lines of their own stay,
    # 15 to 28; a line break written out before a value stays, 12 to 25.
    "yaml_property_line": "a_token:\n &a\n x\n",
    "yaml_header_below": "a_token:\n |\n x\n",
    "named_assignment_written_out_break": "A_TOKEN=\\nx;",
    # Round 7: a value whose lines bare carriage returns end, below its key
    # and below a block's header, as long as the same with line feeds, 12 to
    # 25 and 15 to 28; a carriage return written out before a value, 12 to
    # 25.
    "yaml_value_carriage_return": "a_token:\r x\r",
    "yaml_header_below_carriage_return": "a_token:\r |\r x\r",
    "named_assignment_written_out_carriage_return": "A_TOKEN=\\rx;",
    # Round 8: a value below a YAML flow collection's key, 12 to 25 after a
    # ``{`` and 11 to 24 after a ``,``, the shortest; a plain scalar that
    # goes on below such a key's line, 14 to 25; and under a well-known
    # name, its next line indented, longer still. A name behind an underscore, 11 to 24, is one
    # character longer than the shortest name.
    "yaml_flow_value": "{a_token:\nx}",
    "yaml_flow_value_after_comma": ",a_token:\nx",
    "yaml_flow_value_well_known_name": "{HF_TOKEN:\nx y}",
    "yaml_flow_continued": "{a_token: x\ny}",
    "yaml_flow_continued_well_known_name": "{HF_TOKEN: x\n y z}",
    "credential_suffix_assignment_underscore": "_A_TOKEN=x;",
    # The BEGIN line and 16 characters of key material, its floor: 44 to 15.
    "private_key": "-----BEGIN" + " PRIVATE KEY-----" + "a" * 16 + " ",
    # Its material parted by spaces (round 5), longer still: 48 to 16.
    "private_key_spaced": "-----BEGIN" + " PRIVATE KEY-----" + "aB1a " * 4 + " ",
    # Parted by bare carriage returns (round 6): 59 to 15.
    "private_key_carriage_returns": "-----BEGIN" + " PRIVATE KEY-----" + "a\r" * 15 + "a ",
    # Parted by runs of carriage returns (round 7): 74 to 15.
    "private_key_carriage_return_runs": "-----BEGIN" + " PRIVATE KEY-----" + "a\r\r" * 15 + "a ",
    # Parted by a carriage return, a space and a line feed (round 8): 89 to 15.
    "private_key_spaced_breaks": "-----BEGIN" + " PRIVATE KEY-----" + "a\r \n" * 15 + "a ",
    "gitlab_pat": "gl" + "pat-" + "a" * 20 + " ",
    "aws_access_key_id": "AKI" + "A" * 17 + " ",
    "groq_api_key": "gs" + "k_" + "a" * 20 + " ",
    "xai_api_key": "xa" + "i-" + "a" * 20 + " ",
    "npm_token": "np" + "m_" + "a" * 20 + " ",
    "huggingface_token": "h" + "f_" + "a" * 20 + " ",
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
    grown = redact("A_TOKEN=x")  # 9 -> 22, the shortest name of all (#285)
    assert grown == "A_TOKEN=***REDACTED***"
    assert len(grown) <= MAX_GROWTH_FACTOR * len("A_TOKEN=x")
    text = "Authorization: Bearer A_TOKEN=x"
    assert redact(text) == "Authorization: Bearer ***REDACTED***"
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
        "private-key",
        "env-assignment",
        "authorization-header",
        "url-userinfo",
        "jwt",
        "google-api-key",
        "gitlab-pat",
        "github-fine-grained-pat",
        "github-token",
        "openai-key",
        "anthropic-key",
        "aws-access-key-id",
        "groq-api-key",
        "xai-api-key",
        "npm-token",
        "huggingface-token",
        "secret-assignment",
        "oauth-field",
        "oauth-assignment",
        "oauth-bare-assignment",
        "credential-suffix-assignment",
        # A quoted value whose quote ends its line (#285 review, round 4), a
        # credential name's value on the lines below it (round 3), a value
        # below a YAML flow collection's key and a plain scalar that goes on
        # below such a key's line (round 8), and a plain scalar that starts
        # on its key's line and goes on below it (round 4), each under the
        # class of the name's own pattern.
        "env-assignment",
        "credential-suffix-assignment",
        "env-assignment",
        "credential-suffix-assignment",
        "env-assignment",
        "credential-suffix-assignment",
        "env-assignment",
        "credential-suffix-assignment",
        "env-assignment",
        "credential-suffix-assignment",
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
    *(text for text, _ in _GOOGLE_KEY_TEXTS),
    *_GOOGLE_KEY_NEAR_MISSES,
    *(text for token in (t for _, t, _ in _TOKENS_285) for text, _ in _token_texts(token)),
    *_NEAR_MISSES_285,
    *(text for text, _ in _PEM_TEXTS),
    *_PEM_NEAR_MISSES,
    *(text for text, _ in _SUFFIX_TEXTS),
    *_SUFFIX_NEAR_MISSES,
    *(text for text, _ in _LOWER_SUFFIX_TEXTS),
    *_LOWER_SUFFIX_NEAR_MISSES,
    *(text for text, _ in _WHOLE_QUOTED_VALUES),
    *(text for text, _ in _QUOTES_THAT_SPAN_NO_LINES),
    *(text for _, text, _ in WHOLE_VALUES_285),
    *(text for _, text, _ in WHOLE_VALUES_285_R3),
    *(text for text, _ in _YAML_VALUES),
    *_YAML_NEAR_MISSES,
    *(text for _, text, _ in WHOLE_VALUES_285_R4),
    *(text for text, _ in _YAML_VALUES_R4),
    *(text for text, _ in _QUOTED_FROM_NEXT_LINE),
    *_R4_NEAR_MISSES,
    *(text for _, text, _ in WHOLE_VALUES_285_R5),
    *(text for text, _ in _DOUBLED_QUOTE_AFTER_BREAK),
    *(text for text, _ in _QUOTE_IN_FIRST_LINE),
    *(text for text, _ in _SEQUENCE_SIBLINGS),
    *(text for text, _ in _YAML_PROPERTY_VALUES),
    *_R5_NEAR_MISSES,
    *(text for text, _ in _SPACED_KEY_TEXTS),
    *_SPACED_PEM_NEAR_MISSES,
    *(text for _, text, _ in WHOLE_VALUES_285_R6),
    *(text for text, _ in _YAML_PROPERTY_AND_HEADER_LINES),
    *(text for text, _ in _UPPER_PROPERTY_AND_HEADER_LINES),
    *(text for text, _ in _WRITTEN_OUT_BELOW_THE_NAME),
    *_R6_NEAR_MISSES,
    *(text for text, _ in _WIDE_SEQUENCE_SIBLINGS),
    *_WIDE_SEQUENCE_PLACEHOLDERS,
    *(text for text, _ in _CR_KEY_TEXTS),
    *_CR_PEM_NEAR_MISSES,
    *(text for text, _ in _OAUTH_AND_SUFFIX_VALUES),
    *(text for text, _ in _URL_CREDENTIAL_CASES),
    *_URLS_WITHOUT_USERINFO,
    _NORMAL_TEXT,
    *(text for text, _ in _OAUTH_FIELD_CASES),
    *_OAUTH_PROSE,
    *(text for text, _, _ in _QUOTED_KEY_CASES),
    *_QUOTED_KEY_NEAR_MISSES,
    *(f"{ending}\n```" for ending in _EMPTY_ASSIGNMENT_ENDINGS),
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


@pytest.mark.parametrize("text", _CREDENTIAL_CORPUS)
def test_a_pattern_with_a_hint_matches_no_text_without_it(text):
    """A hint lets ``redact`` skip a pattern on a text that lacks it (#285
    review, round 3), so it must be what every match holds: no pass of a
    pattern finds anything in a text, its JSON or its escaped repeats where
    the hint is not found."""
    for variant in (text, json.dumps(text), f"x\\n{text}\x1b[0m{text}"):
        for entry in _PATTERNS:
            for hint in (entry.hint, entry.name_hint):
                if hint is None or hint.search(variant):
                    continue
                assert entry.regex.search(variant) is None, (entry.name, variant)
                assert entry.after_escape is None or entry.after_escape.search(variant) is None


def test_every_hint_is_a_literal_a_match_must_hold():
    """The hints are the literals the patterns start with, and a YAML key's
    colon before what ends its line: each one is in every unit that pins the
    pattern's growth, so none could hide a pattern from the growth bound."""
    units = [*_WORST_CASE_UNITS.values(), *_ESCAPED_UNITS.values()]
    for entry in _PATTERNS:
        for hint in (entry.hint, entry.name_hint):
            if hint is None:
                continue
            for unit in units:
                found = entry.regex.search(unit) or (
                    entry.after_escape and entry.after_escape.search(unit)
                )
                if found:
                    assert hint.search(unit), (entry.name, unit)


def test_the_well_known_names_hint_holds_for_every_name_in_any_case():
    """``env-assignment``'s hint is derived from the names it knows, so it
    holds for each of them, as written, in lower case and mixed, on the
    name's line and as the second hint of each pattern that starts at a
    line's start (#285 review, rounds 3 and 4) or at a flow collection's
    indicator (round 8)."""
    line, *starts = [entry for entry in _PATTERNS if entry.name == "env-assignment"]
    assert line.hint is not None and len(starts) == 5
    for name in _ENV_NAME_LIST:
        for variant in (name, name.lower(), name.title()):
            assert line.hint.search(variant), variant
            for entry in starts:
                assert entry.name_hint is not None and entry.name_hint.search(variant), variant


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
        (f"GEMINI_API_KEY={GOOGLE_API_KEY}", ("google-api-key",)),
        (_FINE_GRAINED_PAT, ("github-fine-grained-pat",)),
        ("ghp_abcdefghijklmnop", ("github-token",)),
        ("sk-proj-abcdef123456", ("openai-key",)),
        # ``sk-ant-`` keys are replaced by the earlier ``sk-`` pattern.
        ("sk-ant-abcdefghij123", ("openai-key",)),
        ("password=hunter2hunter2", ("secret-assignment",)),
        ('{"access_token": "FAKE-access"}', ("oauth-field",)),
        ("refresh_token=FAKE_REFRESH_TOKEN", ("oauth-assignment",)),
        ("access=FAKE_ACCESS_VALUE_0000", ("oauth-bare-assignment",)),
        *((text, (name,)) for name, text, _ in SHAPES_285),
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
