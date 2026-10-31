"""Redaction: common token patterns are masked, normal text untouched."""

import ast
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
from tests.conftest import (
    AWS_ACCESS_KEY_ID,
    AWS_KEY_BODY,
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
    SHAPES_285,
    SUFFIX_ASSIGNMENT,
    SUFFIX_SECRET,
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


# Not a private key: a public key, a certificate, a label in lower case or
# behind four dashes, and a BEGIN line with no key material after it (alone,
# quoted by code or prose, an empty block, a placeholder).
_PEM_NEAR_MISSES = [
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


@pytest.mark.parametrize(("text", "expected"), _SUFFIX_TEXTS, ids=_SUFFIX_TEXT_IDS)
def test_a_value_under_a_prefixed_credential_name_is_redacted(text, expected):
    """#285: ``GEMINI_API_KEY`` and ``DEEPSEEK_API_KEY`` are no well-known
    name, and ``_`` before ``API_KEY`` kept ``secret-assignment`` from
    seeing one, so a value of no known shape under such a name (an OpenCode
    launch inherits ``GEMINI_*`` and ``GOOGLE_*``) was printed in the clear.
    An upper-case name ending in ``_API_KEY``, ``_TOKEN``, ``_SECRET`` or
    ``_PASSWORD`` is recognised as a well-known one is, any value, in any
    quoting the named patterns take."""
    assert redact(text) == expected
    assert credential_classes(text) == ("credential-suffix-assignment",)
    assert redact(expected) == expected


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


# Not a credential name: lower or mixed case (a code identifier), a suffix
# that does not end the name, a plural, no separator, no value, a suffix the
# rule does not take (``_KEY``), and a suffix with no prefix (the generic
# ``secret-assignment``'s, which wants a value of 12 characters).
_SUFFIX_NEAR_MISSES = [
    "max_token=5",
    "def fetch(api_token: str, page_token=None):",
    "my_GITHUB_TOKEN=abc123XYZ",
    "Gemini_API_KEY=abc",
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


@pytest.mark.parametrize("text", _SUFFIX_NEAR_MISSES)
def test_a_near_miss_of_a_prefixed_credential_name_is_left(text):
    assert redact(text) == text
    assert credential_classes(text) == ()


@pytest.mark.parametrize(
    "text",
    [
        "A" * 1_000_000,
        "A_" * 500_000,
        "A_TOKEN" * 140_000,
        "ABC_DEF_TOKEN " * 70_000,
        ("\\nA_TOKEN" + "_X" * 10) * 20_000,
    ],
    ids=[
        "one-identifier",
        "underscores",
        "suffixes-no-separator",
        "names-no-separator",
        "after-escapes",
    ],
)
def test_the_credential_suffix_pattern_stays_linear(text):
    """A candidate starts only where an identifier does and scans it to its
    end, so a megabyte of identifiers is redacted well inside the budget."""
    started = time.process_time()
    assert redact(text) == text
    assert time.process_time() - started < 3.0


def test_redact_obj_redacts_a_285_shape_at_any_depth():
    """State and the CLI's documents cross ``redact_obj``: each #285 shape is
    redacted in a nested value, a list and a mapping key. A value under a
    prefixed name is redacted where the string holds the assignment; a
    mapping's key and value are redacted apart, as for a well-known name."""
    value = {
        "env": [text for _, text, _ in SHAPES_285],
        f"key {GROQ_API_KEY}": {"nested": PRIVATE_KEY},
    }
    out = redact_obj(value)
    rendered = repr(out)
    for _, _, body in SHAPES_285:
        assert body not in rendered
    assert out == {
        "env": ["***REDACTED***"] * 7 + ["DEEPSEEK_API_KEY=***REDACTED***"],
        "key ***REDACTED***": {"nested": "***REDACTED***"},
    }


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
# before a letter that makes no escape. A name behind a prefix is recognised
# only when the whole name is upper case (#285, ``credential-suffix-assignment``).
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
    "my_GITHUB_TOKEN=abc123XYZ",
    "gemini_API_KEY=abcdefghijklmnop",
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
        ("GH_TOKEN=\\nabc", "GH_TOKEN=***REDACTED***"),
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
    # A prefixed name in lower case is no credential name (#285).
    '{"my_github_token": "FAKE0opaque0value"}',
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
    # The BEGIN line and 16 characters of key material, its floor: 44 to 15.
    "private_key": "-----BEGIN" + " PRIVATE KEY-----" + "a" * 16 + " ",
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
        "credential-suffix-assignment",
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
    *(text for text, _ in _GOOGLE_KEY_TEXTS),
    *_GOOGLE_KEY_NEAR_MISSES,
    *(text for token in (t for _, t, _ in _TOKENS_285) for text, _ in _token_texts(token)),
    *_NEAR_MISSES_285,
    *(text for text, _ in _PEM_TEXTS),
    *_PEM_NEAR_MISSES,
    *(text for text, _ in _SUFFIX_TEXTS),
    *_SUFFIX_NEAR_MISSES,
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
