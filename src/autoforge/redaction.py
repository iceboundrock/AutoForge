"""Secret redaction for anything that lands in logs.

Baseline protection only — no detector claims to catch every secret shape.
Covers common env-var assignments and a name ending in a credential suffix
(``GEMINI_API_KEY=``, ``DB_PASSWORD:``, ``client_secret=`` with a value
that does not read as code), ``Authorization`` headers, private-key blocks
(also wrapped in short lines), well-known token shapes (GitHub PATs, OpenAI /
Anthropic keys, Google API keys, JWTs, AWS access key ids, GitLab, Groq,
xAI, npm and Hugging Face tokens), OAuth credential fields (``refresh`` /
``access`` / ``id_token`` / ``chatgpt-account-id``) and credentials embedded
in URLs before stdout / stderr / commands are persisted by the run logger.
A shape is recognised where it starts a token: at the start of the text,
after a character that cannot be part of it, or directly after an escape
sequence (``ESC[0m``, ``\\n``, ``\\u00e9``; see ``_ESCAPE``). An assignment's
quoted value is recognised behind an escaped quote too
(``GH_TOKEN=\\"...\\"``, as JSON writes it; see ``_QUOTE``) and replaced
whole, to its closing quote, a later line's too (``_QUOTED_VALUE``), and a
named assignment or header under a quoted key (``"GITHUB_TOKEN": "..."``,
``"Authorization": "Bearer ..."``). :func:`redact_obj` knows a credential's
name as a mapping's key too (``{"GEMINI_API_KEY": "..."}``).
A line that starts a code fence is never a value (``_SPACE_TO_VALUE``).

Every pattern has a stable class name. :func:`credential_classes` reports
which classes a text carries without saying where, so a refusal of agent
text that would be published (``result_parser``'s content policy) can name
what to remove without quoting the secret.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class _Pattern(NamedTuple):
    """One secret shape: its stable class name, the regexes and the replacements.

    ``regex`` finds the shape and ``replacement`` rewrites it. A shape that
    starts a token (:func:`_token`) has a second pass, ``after_escape``,
    which finds it directly after an escape sequence and keeps that sequence
    through ``escape_replacement``; the others have none.
    """

    name: str
    regex: re.Pattern[str]
    replacement: str
    after_escape: re.Pattern[str] | None = None
    escape_replacement: str = ""


# Where a recognised credential may start (#284). There is no regex word
# boundary (``\b``) after the ``m`` of an ANSI colour code (``ESC[0m``, which
# OpenCode writes right before an error's text on stderr) or after the ``n``
# of a ``\n`` written out as two characters (JSON text, a C or Python string
# literal): both are word characters, so a token directly after one was
# missed. A token therefore starts at the start of the text, after any
# character that cannot be part of it (any but an ASCII letter, a digit or
# ``_``, unless a pattern names its own set; a non-ASCII letter is no part of
# an ASCII token), or directly after one of these escape sequences:
#
# - an ANSI / VT escape sequence: a CSI (``ESC[``, or the C1 byte, then
#   parameter, intermediate and final bytes) or a two-character escape
#   (``ESC(B``, ``ESC7``), its ESC also as JSON, C, a shell and caret
#   notation spell it (``\u001b``, ``\x1b``, ``\033``, ``\e``, ``^[``); an OSC
#   or DCS string ends in BEL or ``ESC\``, already no token character;
# - a backslash escape as JSON, C and Python write one: a single letter
#   (``\n``, ``\t``, ``\b``, ...), ``\xHH``, ``\uHHHH``, ``\UHHHHHHHH`` and octal.
#
# A backslash and such a letter count as an escape wherever they occur, a
# path separator before a token included (``C:\Users\nghp_...``): the rule
# errs on redacting. Only the escape right before a token matters, so a run
# of them (``ESC[91mESC[1m``) needs nothing more. The escapes are matched
# case-sensitively inside a ``(?i)`` pattern too: ``\N`` is no escape.
#
# Each alternative below starts with its literal ESC, backslash, caret or C1
# byte, so the regex engine scans for those characters and tries an
# ``after_escape`` pass nowhere else: on text with few of them the second
# pass costs a fraction of the first, and each candidate scans one escape,
# so both stay linear.
_ESCAPE_TAIL = r"(?:\[[0-?]*[ -/]*[@-~]|[ -/]*[0-~])"
_ESCAPE = (
    r"(?-i:"
    rf"\x1b{_ESCAPE_TAIL}"
    rf"|\\(?:(?:u001[bB]|U0000001[bB]|x1[bB]|0?33|e){_ESCAPE_TAIL}"
    r"|[abefnrtv]|x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|[0-7]{1,3})"
    rf"|\^\[{_ESCAPE_TAIL}"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r")"
)


# The optional quote around an assignment's value, also as escaped text
# writes it (#281): JSON and a C or Python string literal write a quote as
# ``\"`` or ``\'``, so in a serialized document (an agent's CONTROL_RESULT on
# stdout, a structure printed as JSON) ``GH_TOKEN="..."`` reads
# ``GH_TOKEN=\"...\"``. A plain quote alone stopped the value at the
# backslash and left the secret after it in the clear. Up to seven
# backslashes (JSON carried in a JSON string, three deep) are taken with the
# quote, the bound ``oauth-field`` uses, so a run of them is never rescanned.
_QUOTE = r"(?:\\{0,7}[\"'])?"
# A backslash inside an assignment's value, unless it starts the escaped
# closing quote, which is kept in its own group with the backslashes before
# it: the replacement leaves ``\"`` whole and the JSON around it intact. The
# lookahead is bounded like ``_QUOTE``.
_VALUE_BACKSLASH = r"\\(?!\\{0,6}[\"'])"
# The same after a named or OAuth assignment's first character, unless it
# starts a line break or a tab written out (``\n``, ``\r``, ``\t``, as JSON
# writes one): an unquoted value ends there, as it ends at one written as
# such (#285 review, round 2). It used to take the next line's start with
# it, and where that line was ``NAME: value`` in JSON text
# (``"A_TOKEN: x\nB_TOKEN: y"``), its name went and its value stayed in the
# clear; the name now starts a token after the escape (#284), and its value
# is replaced in turn. A value that starts with one is the next line's, as
# ``_SPACE_TO_VALUE`` takes it from the next line written as such.
_WORD_BACKSLASH = r"\\(?!\\{0,6}[\"'nrt])"
# The space between an assignment's separator and its value (#393): a run on
# the same line, or one across a line break, but never onto a line that
# starts a code fence (three backticks or tildes, indented or not). A prompt
# quotes untrusted text in a fence, and a quoted reply that ended in an empty
# ``GITHUB_TOKEN=`` had its closing fence taken for the value when the run
# log redacted the prompt again, so ``prompt.md`` no longer showed the prompt
# as sent. A fence on a later line is never the value: a secret written in a
# code block under the name was never the replaced run, only the fence was.
# Each line break is tried once, so the scan stays linear.
_SPACE_TO_VALUE = r"(?:[^\S\r\n]*|\s*[\r\n][^\S\r\n]*(?!`{3}|~{3}))"
# A named assignment's value, its opening quote (``vbs``, the backslashes
# before it, up to seven as ``_QUOTE`` takes, and ``vq``, the quote) optional.
_VALUE_OPEN = r"((?P<vbs>\\{0,7})(?P<vq>[\"']))?"
# A value no quote opens ends at a space, a quote, a semicolon, a backslash
# that starts an escaped closing quote and, after its first character, a line
# break or a tab written out.
_UNQUOTED_VALUE = rf"(?:[^\s\"';\\]|{_VALUE_BACKSLASH})(?:[^\s\"';\\]|{_WORD_BACKSLASH})*"
# A value a quote opens is taken whole (#285): it runs to the quote that
# closes it, spaces, semicolons and the other quote character included, so
# ``DB_PASSWORD="correct horse; battery"`` is replaced entire where the value
# used to stop at the first space and leave the rest in the clear. The
# closing quote is the opening one behind exactly as many backslashes; the
# same quote behind more is escaped inside the value (``"a\"b"`` as a shell
# writes it, ``\"a\\\"b\"`` as JSON writes that in a string), and so is the
# opening quote doubled (``'it''s'``, as YAML and SQL escape a single quote
# and a shell joins two strings; #285 review).
#
# A value runs across line breaks to its closing quote (``_QUOTED_SPAN``,
# #285 review), as a shell string and a YAML flow scalar do, as long as what
# it spans reads as one string: no blank line, no line that starts a code
# fence (which ``_SPACE_TO_VALUE`` never takes either), no backtick, no
# unescaped quote of the other kind, and a closing quote that no letter,
# digit or ``_`` follows. A quote that opens no value does not take the lines
# after it: the end of a string literal in code
# (``print('GH_TOKEN=' + read("f"))``), one in a Markdown code span
# (``DEPLOY_SECRET="-----BEGIN ...``), each meets one of those before a quote
# of its kind closes it, and so does the code a prompt, a log or an agent's
# read context quotes. A value that does not span lines, or whose closing
# quote never comes (the end of the text, or the same quote behind fewer
# backslashes, which closes the string an escaped value stands in:
# ``"GH_TOKEN=\"abc"``), is the rest of its line up to that quote
# (``_QUOTED_LINE``), the other quote and a backtick included, as before: an
# unterminated value goes as far as its line or its string and no further.
# A run of backslashes is taken whole and every alternative is possessive,
# so each of the two reads a character once, and a candidate that fails does
# so at its first character.
_QUOTED_ESCAPE = (
    r"\\++(?![\"'])"
    r"|(?P=vbs)\\+[\"']"
    r"|(?P=vbs)(?P=vq)(?P=vbs)(?P=vq)"
)
_QUOTED_SPAN = (
    r"(?:[^\\\"'`\r\n]"
    r"|(?:\r\n?|\n)(?![^\S\r\n]*+(?:[\r\n]|`{3}|~{3}))"
    rf"|{_QUOTED_ESCAPE}"
    r")++(?=(?P=vbs)(?P=vq)(?!\w))"
)
_QUOTED_LINE = rf"(?:[^\\\"'\r\n]|{_QUOTED_ESCAPE}|\\*+(?!(?P=vq))[\"'])++"
_QUOTED_VALUE = rf"(?:{_QUOTED_SPAN}|{_QUOTED_LINE})"
# The closing quote of a value a quote opened, absent when the value was cut
# short; ``_QUOTE``, as before, after one no quote opened.
_VALUE_CLOSE = rf"((?(vq)(?:(?P=vbs)(?P=vq))?|{_QUOTE}))"
# A named assignment after its name: the separator (the name's closing quote,
# when it is a quoted key, kept with it), the value's optional opening quote
# (its backslashes and quote are groups of their own), the value and its
# closing quote: groups 2, 3, 6 and 7 of a pattern whose name is group 1.
_NAMED_VALUE = (
    rf"({_QUOTE}\s*[:=]{_SPACE_TO_VALUE}){_VALUE_OPEN}"
    rf"((?(vq){_QUOTED_VALUE}|{_UNQUOTED_VALUE})){_VALUE_CLOSE}"
)
# The well-known credential variable names ``env-assignment`` recognises, in
# any case; :func:`redact_obj` knows them as a mapping's keys too.
_ENV_NAMES = (
    r"GITHUB_TOKEN|GH_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|"
    r"OPENCODE_API_KEY|GITLAB_TOKEN|HF_TOKEN|SLACK_TOKEN|AWS_SECRET_ACCESS_KEY|"
    r"ANTHROPIC_AUTH_TOKEN|CLAUDE_CODE_OAUTH_TOKEN|OPENCODE_SERVER_PASSWORD"
)
# A name with a credential suffix behind a prefix (#285), group 1 of
# ``credential-suffix-assignment``. Upper case (``GEMINI_API_KEY``, group
# ``upper``) is an environment variable's, whose value is anything, as a
# well-known name's is. Lower or mixed case (``client_secret``,
# ``my_GITHUB_TOKEN``, ``Db_Password``) is a code identifier's as often as a
# configuration key's, so its value must not read as code
# (``_LITERAL_VALUE``). Both stop where the identifier does, so a candidate
# reads one identifier.
_SUFFIX_NAME = (
    r"((?P<upper>[A-Z][A-Z0-9_]*_(?:API_KEY|TOKEN|SECRET|PASSWORD))"
    r"|[A-Za-z][A-Za-z0-9_]*_(?i:api_key|token|secret|password))"
)
# Where a lower- or mixed-case name's unquoted value ends (#285 review): at a
# space, a quote, a backtick, a semicolon, the ``&`` that starts a form
# body's next field (``_FIELD_NAME``) and the end of the text. A run of
# commas, full stops, colons and closing brackets right before one of those
# is the punctuation after a word, not part of it (``f(x_token=v),``,
# ``api_token: str):``, a sentence's full stop); anywhere else it is the
# value's (``p,ss)w.rd``). ``_WORD_END`` is that end, read after a word.
_TRAIL = r"[,.:)\]}]"
_WORD_END = rf"{_TRAIL}*+(?![^\s\"'`;&])"
# An identifier, and the identifiers joined by ``.`` or ``::`` before the
# last one of a dotted name.
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*+"
_PATH = rf"(?:{_IDENT}(?:\.|::))*+"
# An unquoted value that reads as code, not as a literal (#285 review), so
# that the code an agent quotes stays publishable. Each alternative reads no
# further than the value's first word:
#
# - an identifier that names a credential itself, alone or the last of a
#   dotted name: the variable or attribute that holds it
#   (``api_token=api_token``, ``client_secret=self.client_secret``,
#   ``api_token=accessToken``, ``client_secret: CLIENT_SECRET``);
# - a dotted name with no digit (``api_token=args.tok``);
# - a call, a subscript or a generic type (``x_token=sha256(data)``,
#   ``os.environ["X"]``, ``Optional[str]``, ``Option<String>``);
# - a type, a constant, or a keyword a statement starts with (``api_token:
#   str``, ``page_token=None``, ``use_token: true``, ``if not api_token:
#   return``);
# - a reference or a placeholder: a shell, batch or ``%`` format variable
#   (``$API_TOKEN``, ``${X}``, ``$(cat f)``, ``%API_TOKEN%``, ``%s``), or a
#   word that opens a bracket (``{token}``, ``{{ vault_secret }}``,
#   ``<value>``, ``[REDACTED]``);
# - a number (``max_token=4096``), and a word with no letter or digit before
#   it ends (``...``, ``|``, ``~``, the second character of ``:=`` and
#   ``==``).
#
# What this costs: a literal of one of these shapes is not recognised
# (``db_password=password``, ``db_password: hello.world``, a numeric PIN),
# and a variable that names no credential is taken for a literal
# (``api_token=tok``).
_CODE_VALUE = (
    rf"{_PATH}[A-Za-z0-9_]*?(?i:token|secret|password|passwd|api_?key){_WORD_END}"
    rf"|[A-Za-z_]++(?:(?:\.|::)[A-Za-z_]++)++{_WORD_END}"
    rf"|{_PATH}{_IDENT}[(\[<]"
    r"|(?i:str|string|bytes|int|float|bool|boolean|number|dict|list|tuple|object|any"
    r"|unknown|char|text|secretstr|secretbytes|none|null|nil|true|false|undefined"
    rf"|yes|no|on|off|return|raise|pass|continue|break|await|yield|lambda|not|new){_WORD_END}"
    rf"|\$[({{]|\$[A-Za-z0-9_]++{_WORD_END}|%{_IDENT}%{_WORD_END}"
    rf"|%(?:\([A-Za-z0-9_]*+\))?[-#0+.0-9]*+[A-Za-z]{_WORD_END}"
    r"|[(\[{<]"
    rf"|[-+]?[0-9]++(?:\.[0-9]++)?{_WORD_END}"
    r"|[^\s\w\"'`;&\\]*+(?!\w)"
)
# What follows the ``&`` that starts a form body's next field: its name and
# ``=`` (``&grant_type=``), as ``urlencode`` writes one; ``urlencode`` writes
# an ``&`` inside a value as ``%26``. Any other ``&`` is the value's.
_FIELD_NAME = r"[A-Za-z0-9_.~%+\[\]-]*+="
# The unquoted value a lower- or mixed-case prefixed name takes (#285
# review): one word on the name's line, of any character but what ends it,
# so that a word of letters alone (``client_secret: changeme``), one with
# punctuation (``db_password=Sup3r$ecret!``) and a form-encoded one
# (``client_secret=a%2Fb%2Bc&grant_type=...``) are each taken whole, unless
# it reads as code (``_CODE_VALUE``). A backslash is the word's unless it
# starts an escaped quote or a line break written out (``_WORD_BACKSLASH``),
# as for an upper-case name. The word can hold the next candidate's name and
# separator, when no space parts them, and is then taken whole and never read
# again; only a candidate the code rule refuses fails, at the first word of
# its value, so the pass stays linear.
_LITERAL_VALUE = (
    rf"(?!{_CODE_VALUE})"
    r"(?:[^\s\"'`;&\\,.:)\]}]"
    rf"|&(?!{_FIELD_NAME})"
    rf"|{_TRAIL}++(?=[^\s\"'`;&])"
    rf"|{_WORD_BACKSLASH}"
    r")++"
)

# A private-key block (#285), as PEM (RFC 7468) writes one and OpenSSH and
# OpenPGP's armour borrow: a ``-----BEGIN <label>-----`` line, the key's
# base64 body, an ``-----END <label>-----`` line. The label names a private
# key: up to three upper-case words (``RSA``, ``EC``, ``OPENSSH``,
# ``ENCRYPTED``) before ``PRIVATE KEY``, or none, and OpenPGP's ends in
# ``PRIVATE KEY BLOCK``; a public key or a certificate is no credential, and
# the labels are case-sensitive.
_PEM_LABEL = r"(?:[A-Z0-9]{1,16} ){0,3}PRIVATE KEY(?: BLOCK)?"
# What stands between a block's BEGIN and END lines: anything up to the next
# run of five dashes (base64, line breaks as written or escaped, RFC 1421's
# ``Proc-Type:`` and ``DEK-Info:`` headers, OpenPGP's ``Version:`` and
# ``Comment:``, a log line's prefix), never the dashes that start the next
# line of either kind.
_PEM_ANY = r"(?:[^-]++|-(?!----))"
# A body no END line closes (a capture cut inside the key, ``head`` of a key
# file): runs of base64 parted by the space, the line breaks (written out
# behind up to seven backslashes too, as JSON and JSON in JSON write them),
# the colons, commas and single dashes of its headers. Each run is taken
# whole and the body ends at the last one, so a line break after it, which
# can be the escape the next BEGIN line starts after, is left.
_PEM_GAP = r"(?:[\s:,]|\\{1,7}[nrt]|-(?!----))"
_PEM_BODY = rf"(?:{_PEM_GAP}*+[A-Za-z0-9+/=]++)*+"
# Key material: 16 base64 characters in a row, which a block needs to be a
# key. A BEGIN line alone, or one that prose or code quotes
# (``startswith("-----BEGIN ...")``), is no credential. A line break, as
# written or written out (``\n`` and ``\r\n``, behind up to seven
# backslashes, as JSON and JSON in JSON write them), and the indentation
# after it may stand between any two of the 16 (``_PEM_WRAP``): a valid key
# wrapped at fewer than 16 characters a line (``fold -w 8``) has no run of 16
# on any line, and was no credential until #285's review. A space between
# two runs still parts them, so the words of prose are none. The lookahead
# for it steps over a shorter run whole (``_PEM_SHORT_RUN``, the same
# wrapping allowed), so it reads each character once and a run of 16 at most
# twice.
_PEM_B64 = r"[A-Za-z0-9+/]"
_PEM_WRAP = r"(?:\r?\n|\\{1,7}r\\{1,7}n|\\{1,7}n)[ \t]*"
_PEM_KEY = rf"(?:{_PEM_B64}(?:{_PEM_WRAP})?+){{15}}{_PEM_B64}"
_PEM_SHORT_RUN = rf"(?:{_PEM_B64}(?:{_PEM_WRAP})?+){{1,15}}+(?!{_PEM_B64})"


def _token(
    name: str, body: str, replacement: str, *, flags: str = "", token_chars: str = "A-Za-z0-9_"
) -> _Pattern:
    """The pattern for a shape that starts a token, by the rule above.

    ``regex`` finds ``body`` at the start of the text or after any character
    but ``token_chars``. ``after_escape`` finds it directly after an escape
    sequence, taken in as the ``lead`` group, and ``escape_replacement``
    puts that back before ``replacement``, whose ``\\N`` references move one
    group on past it. A new pattern is built here, never on ``\\b``.
    """
    return _Pattern(
        name,
        re.compile(rf"{flags}(?<![{token_chars}]){body}"),
        replacement,
        re.compile(rf"{flags}(?P<lead>{_ESCAPE}){body}"),
        r"\g<lead>" + re.sub(r"\\(\d+)", lambda m: rf"\g<{int(m[1]) + 1}>", replacement),
    )


# The OAuth credential fields a quoted key names: the ``oauth-field`` pattern
# below, and :func:`redact_obj` for a mapping entry under such a key.
_OAUTH_FIELD_NAMES = r"refresh|access|(?:refresh|access|id)_token|chatgpt[-_]account[-_]id"

# Applied in this order, each to the previous one's output, a pattern's
# ``after_escape`` pass right after its ``regex``; the order is part of
# :func:`redact`'s output and of the class :func:`credential_classes`
# reports. An ``sk-ant-`` key with at least eight characters after ``sk-`` is
# replaced by ``openai-key`` before ``anthropic-key`` sees it, so it is
# reported under that class.
_PATTERNS: tuple[_Pattern, ...] = (
    # A private-key block (#285), replaced whole: BEGIN line, body and END
    # line. Without an END line before the next run of five dashes, the BEGIN
    # line and the body that follows it (``_PEM_BODY``) are. Either needs key
    # material (``_PEM_KEY``) in what it takes. A block starts anywhere but
    # after a dash: no identifier holds a BEGIN line, so a letter before one
    # (the ``n`` of a ``\n`` written out) is no reason to leave it. First,
    # because a block holds what other patterns would take a slice of: a
    # named assignment's value stops at the space in the BEGIN line
    # (``DEPLOY_SECRET=-----BEGIN``), a quoted one at the end of that line
    # (``DEPLOY_SECRET="-----BEGIN RSA ...``), and either would leave the
    # label or the key behind it unrecognised. Each candidate starts at a
    # BEGIN line and scans to the next run of five dashes, where the next
    # candidate starts, at most three times (the lookahead, the block, the
    # body), so the pass is linear.
    _token(
        "private-key",
        rf"-----BEGIN {_PEM_LABEL}-----(?:"
        rf"(?=(?:[^-A-Za-z0-9+/]++|-(?!----)|{_PEM_SHORT_RUN})*+{_PEM_KEY})"
        rf"{_PEM_ANY}*+-----END {_PEM_LABEL}-----"
        rf"|(?=(?:{_PEM_GAP}|=|{_PEM_SHORT_RUN})*+{_PEM_KEY}){_PEM_BODY})",
        "***REDACTED***",
        token_chars="-",
    ),
    # VAR=secret / VAR: secret assignments for well-known names. The name may
    # be a quoted key (#393): JSON, a Python mapping's repr and a TOML quoted
    # key write ``"GITHUB_TOKEN": "..."``, where the key's closing quote
    # stands between the name and the separator, and a value of no known
    # shape was kept in the clear. That quote, escaped as ``_QUOTE`` takes
    # one, is kept with the separator. A quoted value is replaced whole
    # (#285, ``_QUOTED_VALUE``).
    _token(
        "env-assignment",
        rf"({_ENV_NAMES}){_NAMED_VALUE}",
        r"\1\2\3***REDACTED***\7",
        flags="(?i)",
    ),
    # Authorization: Bearer <token> / Token <token> / Basic <base64>, also as
    # a quoted key and value (``"Authorization": "Bearer ..."``, a headers
    # mapping, #393), whose escaped closing quote stays whole as an
    # assignment's does.
    _token(
        "authorization-header",
        rf"(Authorization{_QUOTE}\s*:\s*{_QUOTE}(?:Bearer|Token|Basic)(?=\s){_SPACE_TO_VALUE})"
        rf"((?:[^\s\"';\\]|{_VALUE_BACKSLASH})+)",
        r"\1***REDACTED***",
        flags="(?i)",
    ),
    # Credentials embedded in a URL: everything between ``scheme://`` and
    # ``@`` (``https://x-access-token:<token>@github.com/...``,
    # ``https://<token>@...``, ``postgresql://user:password@...``). The whole
    # userinfo goes, username included: a bare userinfo is as often a token
    # as a name, and the host and path that follow keep the line readable.
    # The userinfo class stops at the characters that end a URL's authority
    # (``/``, ``?``, ``#``; RFC 3986 §3.2), so an ``@`` in a path, query or
    # fragment (``https://host?to=a@b``) is not mistaken for a credential
    # delimiter. The scheme is bounded and the class excludes ``/``, so a
    # candidate never scans past the ``://`` of the next one: the pass stays
    # linear over the 16 MiB an executor capture can be.
    _token(
        "url-userinfo",
        r"([a-z][a-z0-9+.-]{1,31}://)[^\s/?#@]+@",
        r"\1***REDACTED***@",
        flags="(?i)",
    ),
    # JWTs: three base64url segments, the first a ``{"`` JSON header. Only
    # the header has a floor: ``eyJ`` is ``{"`` plus the first key character,
    # and the smallest such object (``{"a":0}``) encodes to ten characters.
    # The payload and signature may be short or empty, as the compact
    # serialization permits: ``{}`` is ``e30``, an unsecured ``alg: none``
    # token (RFC 7519 §6) has an empty signature, and detached content
    # (RFC 7515 App. F) an empty payload. ``-`` is a token character here,
    # so a ``-eyJ`` inside a segment is not a fresh candidate that rescans
    # the run (base64url admits ``-``, and no escape starts inside one); each
    # run is then scanned by at most three candidates and the pass stays
    # linear. Placed before the shorter token shapes so one of them cannot
    # replace a slice of the token and leave the rest unrecognised.
    _token(
        "jwt",
        r"eyJ[A-Za-z0-9_-]{7,}\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*",
        "***REDACTED***",
        token_chars="A-Za-z0-9_-",
    ),
    # Google API keys (#282): ``AIza`` and 35 base64url characters, 39 in all,
    # the key OpenCode launches inherit under ``GEMINI_*`` and ``GOOGLE_*``
    # names; the shape recognises the value under any name, in a URL's
    # ``?key=`` and alone, and is reported before the name rule
    # (``credential-suffix-assignment``) that runs after it. Case-sensitive
    # and the whole token: ``-`` is a token character on both sides, as
    # for a JWT, and a run of the key's characters that goes on past the 39
    # is no key, so a near-miss of either length is left. The body is fixed,
    # so each candidate scans at most 40 characters and the pass is linear.
    # Placed before the GitHub and ``sk-`` shapes so that one starting after a
    # ``-`` inside a key cannot replace a slice of it and leave the rest.
    _token(
        "google-api-key",
        r"AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])",
        "***REDACTED***",
        token_chars="A-Za-z0-9_-",
    ),
    # GitLab personal, project and group access tokens (#285): ``glpat-`` and
    # at least 20 characters of ``[A-Za-z0-9_-]``, the length of the legacy
    # token. ``-`` is in the alphabet, so it is a token character on both
    # sides, as for a JWT and a Google API key: a ``glpat-`` inside a
    # kebab-case word or another token is none. Placed right after the
    # Google key, before every shape that can start after a ``-``, so that
    # none replaces a slice of a token and leaves the rest.
    _token(
        "gitlab-pat",
        r"glpat-[A-Za-z0-9_-]{20,}",
        "***REDACTED***",
        token_chars="A-Za-z0-9_-",
    ),
    # GitHub fine-grained PATs
    _token("github-fine-grained-pat", r"github_pat_[A-Za-z0-9_]{8,}", "***REDACTED***"),
    # GitHub classic PATs and app / OAuth tokens
    _token("github-token", r"gh[pousr]_[A-Za-z0-9_]{8,}", "***REDACTED***"),
    # OpenAI-style sk- keys (incl. project variant sk-proj-...)
    _token("openai-key", r"sk-(?:proj-)?[A-Za-z0-9_-]{8,}", "***REDACTED***"),
    # Anthropic keys
    _token("anthropic-key", r"sk-ant-[A-Za-z0-9_-]{8,}", "***REDACTED***"),
    # The shapes below (#285) run after the ``sk-`` ones, whose alphabet holds
    # ``-``: a shape that may start after a ``-`` would otherwise replace a
    # slice of such a key. Each is case-sensitive. A prefixed one takes at
    # least 20 characters, fewer than any real token of its kind holds, of an
    # alphabet that, but for xAI's, has no ``_``, so that a snake_case name
    # (``npm_config_registry``, ``hf_hub_download``) is none.
    #
    # AWS access key ids: ``AKIA`` (a long-term key) or ``ASIA`` (a temporary
    # one) and 16 characters of ``[A-Z0-9]``, 20 in all, the whole token: a
    # run that goes on is no key id. The secret key that goes with one has no
    # shape of its own; ``AWS_SECRET_ACCESS_KEY`` names it.
    _token("aws-access-key-id", r"A[KS]IA[A-Z0-9]{16}(?![A-Za-z0-9_])", "***REDACTED***"),
    # Groq API keys: ``gsk_`` and 52 characters of ``[A-Za-z0-9]``.
    _token("groq-api-key", r"gsk_[A-Za-z0-9]{20,}", "***REDACTED***"),
    # xAI API keys: ``xai-`` and 80 characters of ``[A-Za-z0-9_]``. ``-`` is a
    # token character before it, as for a GitLab token, so that a kebab-case
    # word (``grok-xai-...``) holds none.
    _token(
        "xai-api-key",
        r"xai-[A-Za-z0-9_]{20,}",
        "***REDACTED***",
        token_chars="A-Za-z0-9_-",
    ),
    # npm access tokens: ``npm_`` and 36 characters of ``[A-Za-z0-9]``.
    _token("npm-token", r"npm_[A-Za-z0-9]{20,}", "***REDACTED***"),
    # Hugging Face access tokens: ``hf_`` and 34 letters.
    _token("huggingface-token", r"hf_[A-Za-z0-9]{20,}", "***REDACTED***"),
    # Generic long bearer-ish assignments: token/password/secret = <value>,
    # the name also as a quoted key (``"api_key": "..."``, #393).
    # ``_`` is a token character before the name, so a prefixed one
    # (``client_secret=``) is not recognised here; it is
    # ``credential-suffix-assignment``'s (#285), last below.
    _token(
        "secret-assignment",
        rf"(token|passwd|password|secret|api[_-]?key)({_QUOTE}\s*[:=]{_SPACE_TO_VALUE})"
        rf"({_QUOTE})([A-Za-z0-9_\-./+]{{12,}})({_QUOTE})",
        r"\1\2\3***REDACTED***\5",
        flags="(?i)",
    ),
    # OAuth credential fields as a quoted key and value, the shape of an OAuth
    # token response, Pi's ``auth.json`` and a Python mapping's repr:
    # ``"refresh": "..."``, ``'access_token': '...'``, ``"id_token": "..."``,
    # ``"chatgpt_account_id": "..."``. Only a quoted key followed by a quoted
    # value matches, so prose ("refresh the page", "access: denied") is
    # untouched. A quote starts it, so it needs no ``after_escape`` pass. The
    # same escaping before both quotes (``\"refresh\": \"...``)
    # covers JSON carried inside a JSON string; the bound on it keeps a run of
    # backslashes from being rescanned from every position.
    _Pattern(
        "oauth-field",
        re.compile(
            rf"(?i)(\\{{0,7}})([\"'])({_OAUTH_FIELD_NAMES})\1\2(\s*:\s*\\{{0,7}}[\"'])[^\"'\s\\]+"
        ),
        r"\1\2\3\1\2\4***REDACTED***",
    ),
    # The same credentials as an assignment or header: ``refresh_token=...``
    # (a query string, a form body, a shell variable), ``id_token: ...`` and
    # the ``chatgpt-account-id: ...`` request header. ``_`` is no token
    # character before the name, so a prefixed one (``oauth_refresh_token=``)
    # is caught too.
    _token(
        "oauth-assignment",
        r"((?:refresh|access|id)_token|chatgpt[-_]account[-_]id)"
        rf"(\s*[:=]{_SPACE_TO_VALUE})({_QUOTE})(?:[^\s\"';,&\\]|{_VALUE_BACKSLASH})"
        rf"(?:[^\s\"';,&\\]|{_WORD_BACKSLASH})*",
        r"\1\2\3***REDACTED***",
        flags="(?i)",
        token_chars="A-Za-z0-9",
    ),
    # Bare ``refresh=`` / ``access=`` with a token-length value. Those words
    # are ordinary prose, so only an ``=`` assignment of at least 16
    # characters counts; ``access: denied`` and ``refresh=true`` stay.
    _token(
        "oauth-bare-assignment",
        rf"(refresh|access)(=)({_QUOTE})(?:[^\s\"';,&\\]|{_VALUE_BACKSLASH}){{16,}}",
        r"\1\2\3***REDACTED***",
        flags="(?i)",
    ),
    # VAR=secret / VAR: secret for a name with a credential suffix behind a
    # prefix (#285): ``GEMINI_API_KEY``, ``DEEPSEEK_API_KEY``,
    # ``MY_GITHUB_TOKEN``, ``client_secret``, ``db_password``, the name also as
    # a quoted key. An OpenCode launch inherits ``GEMINI_*`` and ``GOOGLE_*``,
    # an agent that prints its environment prints such a name with its value,
    # and a configuration file or a form body names one in lower case
    # (``client_secret: ...``, ``client_secret=...&grant_type=...``).
    #
    # - An upper-case name, an environment variable's, takes any value, as a
    #   well-known name does, on the same line or the next.
    # - A lower- or mixed-case name is a code identifier's as often
    #   (``def f(api_token: str)``, ``api_token=api_token``), so its value,
    #   on the same line, is a quoted one (a string literal, a JSON or YAML
    #   string) or an unquoted word that does not read as code
    #   (``_LITERAL_VALUE``), whatever its characters.
    #
    # A quoted value is replaced whole (``_QUOTED_VALUE``) either way. Last,
    # so a value of a known shape is reported by its own class, a well-known
    # name is ``env-assignment``'s and an OAuth name (``access_token``,
    # ``oauth_refresh_token``) an OAuth pattern's; what such a pattern left of
    # a quoted value (the part after a space) is replaced here. A candidate
    # starts only where an identifier does and scans no further than its end
    # and the value after it, so the pass is linear.
    _token(
        "credential-suffix-assignment",
        rf"{_SUFFIX_NAME}"
        rf"({_QUOTE}(?(upper)\s*[:=]{_SPACE_TO_VALUE}|[^\S\r\n]*+[:=][^\S\r\n]*+))"
        rf"{_VALUE_OPEN}"
        rf"((?(vq){_QUOTED_VALUE}|(?(upper){_UNQUOTED_VALUE}|{_LITERAL_VALUE})))"
        rf"{_VALUE_CLOSE}",
        r"\1\3\4***REDACTED***\8",
    ),
)

_REDACTED = "***REDACTED***"

# A mapping key that names a credential, whole (``fullmatch``) and in any
# case: an OAuth credential field (``refresh``, ``id_token``), a well-known
# variable name (``GITHUB_TOKEN``) and a name with a credential suffix behind
# a prefix (``GEMINI_API_KEY``, ``client_secret``, #285). ``{"refresh":
# "..."}`` and ``{"GEMINI_API_KEY": "..."}`` are shapes ``oauth-field``,
# ``env-assignment`` and ``credential-suffix-assignment`` find in a text, but
# :func:`redact_obj` hands :func:`redact` the key and its value apart, and
# neither matches on its own; this is how it still sees them. A mapping's
# string value is a literal, as a quoted value is in a text, so a key in
# lower case needs no more than one in upper case.
_CREDENTIAL_KEY = re.compile(
    rf"(?i:{_OAUTH_FIELD_NAMES}|{_ENV_NAMES}|[a-z][a-z0-9_]*_(?:api_key|token|secret|password))"
)

# :func:`redact` never returns more than this many times its input's length.
# Every pattern replaces a run of at least one character with the 14-character
# marker, so a text can *grow* under redaction; the worst shape is a one
# character credential in the shortest URL that carries one: ``ab://x@`` is 7
# characters and becomes 20, a factor of 2.86, and it repeats without a
# separator (``@`` ends a word), so a text of that shape and no other reaches
# the ratio and no text exceeds it. The next worst is a one character secret
# behind the shortest recognised name, a one-letter prefix and a credential
# suffix (#285): ``A_TOKEN=x``, 9 to 22, a factor of 2.44 (``HF_TOKEN=x`` and
# ``id_token=x``, 10 to 23, follow), and ``a_token=x`` in lower case as well
# (#285 review: a lower-case name's unquoted value is any word that does not
# read as code). Every token shape #285 adds is longer than the marker and
# only shrinks, a private-key block (43 characters or more) included. A
# quoted value is taken whole (#285), across line breaks and doubled quotes
# too, so a quoted value of one character (``A_TOKEN="x"``, 11 to 24) is
# still the worst it does.
# Growth does not compound across patterns: a later pattern can only lengthen
# the text by matching a run *shorter* than the marker, and a marker is never
# part of such a run. The value classes that admit ``*`` (the named-assignment
# values, quoted or not, a lower-case name's unquoted word among them, the
# header, URL-userinfo and OAuth-field values, and what a private-key block
# holds between its BEGIN and END lines) can only take a marker in whole,
# since none of the characters that end such a run (a quote, a line break, a
# space) occurs in the marker, nor does the separator such a run starts
# after, so a match containing one is already at least as long as its
# replacement and shrinks or keeps the length; every other value class
# excludes ``*``, so a marker is never part of those matches at all. The
# characters around a replaced run are untouched, so no new short match
# appears beside it.
# An ``after_escape`` pass is one more pattern in that order, and the escape
# sequence it starts at (#284) is put back unchanged, no part of the replaced
# run; at two characters or more it separates repeated matches no tighter
# than the one-character separators above, so it raises no ratio either.
# The escaped quote an assignment's value may stand in (#281, ``_QUOTE``) is
# kept like a plain one, no part of the replaced run either.
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
    for entry in _PATTERNS:
        text = _apply(entry, text)
    return text


def _apply(entry: _Pattern, text: str) -> str:
    """``text`` with one pattern's passes applied, ``after_escape`` second."""
    text = entry.regex.sub(entry.replacement, text)
    if entry.after_escape is not None:
        text = entry.after_escape.sub(entry.escape_replacement, text)
    return text


def credential_classes(text: str) -> tuple[str, ...]:
    """The class names of the patterns :func:`redact` would apply to ``text``.

    Runs the same sequential substitution as :func:`redact` and records each
    pattern that changed the text, in pattern order and each name once. The
    result is empty exactly when ``redact(text) == text``: a text with a
    credential shape is refused by name, never redacted and published.
    """
    found: list[str] = []
    redacted = text
    for entry in _PATTERNS:
        replaced = _apply(entry, redacted)
        if replaced != redacted:
            found.append(entry.name)
        redacted = replaced
    # Exact by construction: should a later pattern ever restore what an
    # earlier one changed, the text is still what ``redact`` returns.
    return tuple(found) if redacted != text else ()


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

    Each string is redacted on its own, never as part of a serialized
    document: a pattern run over JSON text can consume the syntax around a
    value (an empty ``GITHUB_TOKEN=`` takes the closing quote, #206), so the
    redacted text may no longer parse. (A secret right after an escape there,
    ``\\n`` or ``\\u00e9``, follows a letter or a digit, and a quoted value
    stands behind ``\\"``; they are recognised since #284 and #281, but the
    syntax is still at risk.) The shapes that span a key and its value are
    recognised by the key instead: a non-empty string under a key that
    names a credential, whole and in any case, is replaced whole. That is
    an OAuth credential field (``"refresh": "..."``, ``oauth-field``), a
    well-known variable name (``{"GITHUB_TOKEN": "..."}``) and a name with a
    credential suffix behind a prefix (``{"GEMINI_API_KEY": "..."}``,
    ``{"client_secret": "..."}``, #285), at any depth (an ``env`` mapping in
    a record, a free-form ``escalation``). The entry keeps its key, so it is
    never dropped. No key of a parser-bounded field (a finding, a
    resolution) names a credential, so the bounds that depend on
    :data:`MAX_GROWTH_FACTOR` still hold; and an entry, key and value, grows
    less than that factor too, since the shortest such key (``access``) is
    six characters and the marker 14.

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
            if isinstance(k, str) and isinstance(v, str) and v and _CREDENTIAL_KEY.fullmatch(k):
                result[key] = _REDACTED
            else:
                result[key] = redact_obj(v)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return redact(str(value))


def redact_dict(value: dict) -> dict:
    """`redact_obj` for a mapping, typed for callers that persist dicts."""
    result = redact_obj(value)
    return result if isinstance(result, dict) else {}
