"""Secret redaction for anything that lands in logs.

Baseline protection only — no detector claims to catch every secret shape.
Covers common env-var assignments and a name ending in a credential suffix
(``GEMINI_API_KEY=``, ``DB_PASSWORD:``, ``client_secret=`` with a value
that does not read as code), ``Authorization`` headers, private-key blocks
(also wrapped in short lines, or spaced), well-known token shapes (GitHub PATs, OpenAI /
Anthropic keys, Google API keys, JWTs, AWS access key ids, GitLab, Groq,
xAI, npm and Hugging Face tokens), OAuth credential fields (``refresh`` /
``access`` / ``id_token`` / ``chatgpt-account-id``) and credentials embedded
in URLs before stdout / stderr / commands are persisted by the run logger.
A shape is recognised where it starts a token: at the start of the text,
after a character that cannot be part of it, or directly after an escape
sequence (``ESC[0m``, ``\\n``, ``\\u00e9``; see ``_ESCAPE``). An assignment's
quoted value is recognised behind an escaped quote too
(``GH_TOKEN=\\"...\\"``, as JSON writes it; see ``_QUOTE``) and replaced
whole, to its closing quote, a later line's too (``_QUOTED_VALUE``), also
one whose opening quote ends its line where the name stands outside every
string (``_quoted_from_next_line``), and a named assignment or header under
a quoted key (``"GITHUB_TOKEN": "..."``, ``"Authorization": "Bearer ..."``).
A credential name's value on the lines below it, as YAML writes one (an
indented scalar past blank and comment lines, a ``|`` or ``>`` block), is
recognised too (``_yaml_value``), and a plain scalar that starts on the
key's line and goes on below it (``_yaml_continued``); a value's YAML anchor
and tag stay before it (``_YAML_PROPS``). :func:`redact_obj`
knows a credential's name as a mapping's key too
(``{"GEMINI_API_KEY": "..."}``).
A line that starts a code fence is never a value (``_SPACE_TO_VALUE``).
:func:`redact` applies the patterns in rounds until one changes nothing
(``_MAX_ROUNDS``), so what it returns is what redacting it again returns.

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
    through ``escape_replacement``; the others have none. ``hint``, when
    set, finds what every match of either pass holds (a token's literal
    prefix, a YAML key's colon before a line break): a text without it has
    no match, and the pattern's passes are skipped (#285 review, round 3);
    ``name_hint``, when set, is a second such hint that a match holds too
    (a YAML key's name).
    A hint is a literal search, which the regex engine runs far faster than
    a pattern that starts with a lookbehind, so a round costs less, and a
    text with something to redact takes a second round (``_MAX_ROUNDS``).
    """

    name: str
    regex: re.Pattern[str]
    replacement: str
    after_escape: re.Pattern[str] | None = None
    escape_replacement: str = ""
    hint: re.Pattern[str] | None = None
    name_hint: re.Pattern[str] | None = None


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
# before it, up to seven as ``_QUOTE`` takes, and ``vq``, the quote) optional:
# group ``open``.
_VALUE_QUOTE = r"(?P<open>(?P<vbs>\\{0,7})(?P<vq>[\"']))"
_VALUE_OPEN = rf"{_VALUE_QUOTE}?"
# A YAML node's properties (#285 review, round 5): an anchor (``&name``)
# and a tag (``!!str``, ``!local``, ``!<tag:yaml.org,2002:str>``), either or
# both, each followed by a space. They name or type the node after them, so
# the scalar after them is the value: ``client_secret: &cs 'FAKE'`` and
# ``client_secret: !!str FAKE`` hold a credential as ``client_secret:
# 'FAKE'`` does, and were kept whole (or, the tag taken for the value, in
# part) until round 5. An alias (``*name``) is a node of its own, no
# property. A property stops at a space, a backslash (the start of a line
# break written out) and a flow indicator, so a candidate reads it once.
_YAML_PROPERTY = r"(?:&[^\s\\,\[\]{}]++|!(?:<[^\s\\>]*+>|[^\s\\,\[\]{}]*+))"
_YAML_PROPS = rf"(?:{_YAML_PROPERTY}[^\S\r\n]++){{1,2}}"
# What stands between a ``:`` separator and its value: the space to it, as
# ``_SPACE_TO_VALUE`` takes it, and the value's properties, when it has
# any. Where properties follow the space they are taken, atomically, and the
# space alone is not tried (``(?!...)``): a value never starts with a
# property, and a lower-case name's code rule reads what follows them
# (``client_secret: !secret app_secret``, a reference to another file's
# entry, stays). A property needs the space before it, on the separator's
# line or across a line break.
_SPACE_TO_PROPS = r"(?:[^\S\r\n]++|\s*[\r\n][^\S\r\n]*(?!`{3}|~{3}))"
_COLON_PROPS = rf"{_SPACE_TO_PROPS}{_YAML_PROPS}"
_COLON_TO_VALUE = rf"(?:(?>{_COLON_PROPS})|(?!{_COLON_PROPS}){_SPACE_TO_VALUE})"
# The same on the separator's line only, a lower-case name's.
_LINE_PROPS = rf"[^\S\r\n]++{_YAML_PROPS}"
_COLON_TO_WORD = rf"(?:(?>{_LINE_PROPS})|(?!{_LINE_PROPS})[^\S\r\n]*+)"
# A YAML block scalar's header (#285 review, round 3): ``|`` (literal) or
# ``>`` (folded), its chomping and indentation indicators (``|-``, ``>+2``),
# after optional properties (Ansible's ``!vault |``, an anchor, round 5), and
# a comment. Its value is the more indented lines below it (``_yaml_value``),
# so an unquoted value never starts with a header that ends its line
# (``_BLOCK_AHEAD``): it took the ``|`` for the value and left the block's
# lines, the secret, in the clear.
_BLOCK_HEADER = rf"(?:{_YAML_PROPS})?[|>](?:[1-9][-+]?|[-+][1-9]?)?+"
_YAML_COMMENT = r"(?:[^\S\r\n]++#(?:[^\r\n\\]|\\(?!\\{0,6}[rn]))*+)?"
_BLOCK_AHEAD = rf"{_BLOCK_HEADER}{_YAML_COMMENT}[^\S\r\n]*+(?:[\r\n]|\\{{1,7}}[rn]|\Z)"
# A value no quote opens ends at a space, a quote, a semicolon, a backslash
# that starts an escaped closing quote and, after its first character, a line
# break or a tab written out.
_UNQUOTED_VALUE = (
    rf"(?!{_BLOCK_AHEAD})"
    rf"(?:[^\s\"';\\]|{_VALUE_BACKSLASH})(?:[^\s\"';\\]|{_WORD_BACKSLASH})*"
)
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
# #285 review), as a shell string and a YAML flow scalar do, whatever its
# lines hold: blank lines, the other quote character and backticks included
# (#285 review, round 3: a valid value holding one of those kept every line
# after its first in the clear). Its closing quote is the first quote of its
# kind that nothing escapes; what tells it from a quote that opens no value is
# where the quotes stand:
#
# - the closing quote is followed by no letter, digit, ``_`` or ``*`` (the
#   marker's first character, so that a value redacted after it reads the
#   same): a quote that a word follows opens the next string, as a quote in
#   code or prose after a cut value does (``say "this"``, ``log('done')``);
# - the value's first line holds something: a quote that ends its line closes
#   a string literal (``field = "Set GITHUB_TOKEN="``) as often as it opens a
#   value that starts with a line break, so such a value is recognised only
#   where its name stands outside every string its line opens before it
#   (``_quoted_from_next_line``, #285 review, round 4);
# - the name does not stand right after a quote or a backtick, unless it is a
#   quoted key (``_IN_STRING``): there it starts a string literal or a code
#   span (``print('GH_TOKEN=' + read("f"))``, a Markdown code span opened
#   right before ``DEPLOY_SECRET="...``), and the quote after its separator
#   closes that string, not a value;
# - no line it spans starts a code fence, which ``_SPACE_TO_VALUE`` never
#   takes either.
#
# A value that does not span lines, or whose closing quote never comes (the
# end of the text, or the same quote behind fewer backslashes, which closes
# the string an escaped value stands in: ``"GH_TOKEN=\"abc"``), is the rest of
# its line up to that quote (``_QUOTED_LINE``), as before: an unterminated
# value goes as far as its line or its string and no further.
# A run of backslashes is taken whole and every alternative is possessive,
# so each of the two reads a character once, and a candidate that fails does
# so at its first character.
_QUOTED_ESCAPE = (
    r"\\++(?![\"'])"
    r"|(?P=vbs)\\+[\"']"
    r"|(?P=vbs)(?P=vq)(?P=vbs)(?P=vq)"
)
# One character of a quoted value on its line: any but a line break and the
# closing quote, an escape and the other quote (behind any backslashes) included.
_QUOTED_CHAR = rf"(?:[^\\\"'\r\n]|{_QUOTED_ESCAPE}|\\*+(?!(?P=vq))[\"'])"
_QUOTED_LINE = rf"{_QUOTED_CHAR}++"
# A value's lines to its closing quote, its first line empty or not; a value
# on a name's line holds something on its first (``_QUOTED_SPAN``).
_QUOTED_LINES = (
    rf"(?:{_QUOTED_CHAR}|(?:\r\n?|\n)(?![^\S\r\n]*+(?:`{{3}}|~{{3}})))++"
    r"(?=(?P=vbs)(?P=vq)(?![\w*]))"
)
_QUOTED_SPAN = rf"(?![\r\n]){_QUOTED_LINES}"
_QUOTED_VALUE = rf"(?:{_QUOTED_SPAN}|{_QUOTED_LINE})"
# Whether the name stands right after a quote or a backtick (group ``inq``),
# and the closing quote of a quoted key (group ``kq``, escaped as ``_QUOTE``
# takes one). A name right after a quote that is no quoted key's starts a
# string literal, and its quoted value is its line only (``_QUOTED_VALUE``).
_IN_STRING = r"(?:(?<=[\"'`])(?P<inq>))?"
_KEY_QUOTE = r"(?P<kq>\\{0,7}[\"'])?"
_GUARDED_QUOTED_VALUE = rf"(?(inq)(?(kq){_QUOTED_VALUE}|{_QUOTED_LINE})|{_QUOTED_VALUE})"
# The closing quote of a value a quote opened, absent when the value was cut
# short; ``_QUOTE``, as before, after one no quote opened: group ``close``.
_VALUE_CLOSE = rf"(?P<close>(?(vq)(?:(?P=vbs)(?P=vq))?|{_QUOTE}))"
# A named assignment after its name: the separator (the name's closing quote,
# when it is a quoted key, kept with it, and after a ``:`` the value's YAML
# properties, ``_COLON_TO_VALUE``), the value's optional opening quote (its
# backslashes and quote are groups of their own), the value and its closing
# quote: groups ``sep``, ``open``, ``val`` and ``close`` of a pattern whose
# name is group ``name`` and that starts with ``_IN_STRING``.
_NAMED_VALUE = (
    rf"(?P<sep>{_KEY_QUOTE}\s*(?::{_COLON_TO_VALUE}|={_SPACE_TO_VALUE})){_VALUE_OPEN}"
    rf"(?P<val>(?(vq){_GUARDED_QUOTED_VALUE}|{_UNQUOTED_VALUE})){_VALUE_CLOSE}"
)
# What replaces a named assignment's value: the name, the separator and the
# quotes stay.
_NAMED_REPLACEMENT = r"\g<name>\g<sep>\g<open>***REDACTED***\g<close>"
# The well-known credential variable names ``env-assignment`` recognises, in
# any case; :func:`redact_obj` knows them as a mapping's keys too.
_ENV_NAMES = (
    r"GITHUB_TOKEN|GH_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|"
    r"OPENCODE_API_KEY|GITLAB_TOKEN|HF_TOKEN|SLACK_TOKEN|AWS_SECRET_ACCESS_KEY|"
    r"ANTHROPIC_AUTH_TOKEN|CLAUDE_CODE_OAUTH_TOKEN|OPENCODE_SERVER_PASSWORD"
)
# What every well-known name holds (``_Pattern.hint``): the last ``_``, the
# character before it and the segment after it (``B_TOKEN`` of
# ``GITHUB_TOKEN``), led by the literal ``_``, which the regex engine scans for
# far faster than for the names themselves; derived from the names, so it
# holds for every one of them.
_ENV_HINT = "(?i)_(?<=[{}]_)(?:{})".format(
    "".join(sorted({name.rsplit("_", 1)[0][-1].lower() for name in _ENV_NAMES.split("|")})),
    "|".join(sorted({name.rsplit("_", 1)[1].lower() for name in _ENV_NAMES.split("|")})),
)
# A name with a credential suffix behind a prefix (#285), the name of
# ``credential-suffix-assignment``. Upper case (``GEMINI_API_KEY``, group
# ``upper``) is an environment variable's, whose value is anything, as a
# well-known name's is. Lower or mixed case (``client_secret``,
# ``my_GITHUB_TOKEN``, ``Db_Password``) is a code identifier's as often as a
# configuration key's, so its value must not read as code
# (``_LITERAL_VALUE``). Both stop where the identifier does, so a candidate
# reads one identifier.
_SUFFIX_NAME = (
    r"(?:(?P<upper>[A-Z][A-Z0-9_]*_(?:API_KEY|TOKEN|SECRET|PASSWORD))"
    r"|[A-Za-z][A-Za-z0-9_]*_(?i:api_key|token|secret|password))"
)
# What every such name holds (``_Pattern.hint``): its suffix.
_SUFFIX_HINT = "(?i)_(?:api_key|token|secret|password)"
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
#
# A number reads as code only as a whole value (``_CODE_NUMBER``): a YAML
# plain scalar that goes on to a later line (``client_secret:``, then
# ``12345`` and ``FAKEopaque`` below it) is one string, whose first word is
# no constant (#285 review, round 4). Every other shape (``_CODE_WORD``) is
# code whatever follows it, there too.
_CODE_WORD = (
    rf"{_PATH}[A-Za-z0-9_]*?(?i:token|secret|password|passwd|api_?key){_WORD_END}"
    rf"|[A-Za-z_]++(?:(?:\.|::)[A-Za-z_]++)++{_WORD_END}"
    rf"|{_PATH}{_IDENT}[(\[<]"
    r"|(?i:str|string|bytes|int|float|bool|boolean|number|dict|list|tuple|object|any"
    r"|unknown|char|text|secretstr|secretbytes|none|null|nil|true|false|undefined"
    rf"|yes|no|on|off|return|raise|pass|continue|break|await|yield|lambda|not|new){_WORD_END}"
    rf"|\$[({{]|\$[A-Za-z0-9_]++{_WORD_END}|%{_IDENT}%{_WORD_END}"
    rf"|%(?:\([A-Za-z0-9_]*+\))?[-#0+.0-9]*+[A-Za-z]{_WORD_END}"
    r"|[(\[{<]"
    r"|[^\s\w\"'`;&\\]*+(?!\w)"
)
_CODE_NUMBER = rf"[-+]?[0-9]++(?:\.[0-9]++)?{_WORD_END}"
_CODE_VALUE = rf"{_CODE_WORD}|{_CODE_NUMBER}"
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
    rf"(?!{_CODE_VALUE}|{_BLOCK_AHEAD})"
    r"(?:[^\s\"'`;&\\,.:)\]}]"
    rf"|&(?!{_FIELD_NAME})"
    rf"|{_TRAIL}++(?=[^\s\"'`;&])"
    rf"|{_WORD_BACKSLASH}"
    r")++"
)

# The unquoted value of an OAuth assignment (``oauth-assignment``), which
# ends where a lower-case prefixed name's word does (#285 review, round 3):
# also at a backtick, and before a run of full stops, colons and closing
# brackets that one of its ends follows; and, as before, at a comma and any
# ``&``. Every OAuth name but ``chatgpt-account-id`` is also a prefixed name
# (``refresh_token``), whose rule runs after it and replaces a value this one
# leaves; the value it took used to run on over what that rule leaves after
# the marker (``refresh_token=,word.`` lost its full stop), so a second
# redaction changed the first one's output.
_OAUTH_TRAIL = r"[.:)\]}]++(?=[^\s\"'`;,&])"
_OAUTH_VALUE = (
    rf"(?!{_BLOCK_AHEAD})"
    rf"(?:[^\s\"'`;,&\\.:)\]}}]|{_VALUE_BACKSLASH}|{_OAUTH_TRAIL})"
    rf"(?:[^\s\"'`;,&\\.:)\]}}]|{_WORD_BACKSLASH}|{_OAUTH_TRAIL})*+"
)

# A credential name's value on the lines below it, as YAML writes one (#285
# review, round 3): a key at the start of its line, alone on it, with an
# indented scalar under it, or a block scalar after its ``|`` or ``>``. A
# lower-case name's value on the same line had to be there, so a value on the
# next line, the opaque scalars and blocks a configuration file holds, was
# never recognised, in any sink or check:
#
#     client_secret:          db_password: |          x_api_key: >-
#       <value>                 <line>                  <line>
#                               <line>                  <line>
#
# The key starts its line (``ind`` is its indentation), after a YAML
# sequence's dashes and as a quoted key too, or right after an escape
# sequence (ANSI's, a ``\n`` written out) or a quote that starts a string (a
# YAML text in JSON); a statement does not (``if api_token:``), so code stays
# publishable. Its value is on the lines indented more than the key:
#
# - a block scalar's lines, blank lines among them, to the first line that is
#   not, replaced by one marker on its first line's indentation;
# - a quoted scalar, taken as on the key's line (``_QUOTED_VALUE``), its
#   first line empty too (round 4: ``"`` alone on its line, the value below);
# - a plain scalar: lines that start with no indicator of another node (a
#   sequence's dash, a flow collection, a comment, an anchor or a tag) and
#   hold no mapping's ``: ``, blank lines among them (round 4), to a comment
#   (`` #``); a line that does is the next node, never the value.
#
# Blank lines and comment lines may stand between the key and a quoted or a
# plain scalar (round 4: ``client_secret:``, a blank line or ``# rotated``,
# then the value), as YAML allows; between a block's header and its first
# line only blank lines may, a ``#`` line there being the block's text.
#
# A lower-case name's value must not read as code here too, but what that
# means depends on the node (#285 review, round 4):
#
# - a plain scalar of one line must not read as code (``_CODE_VALUE``):
#   ``${{ secrets.X }}``, ``process.env.API_TOKEN,``, ``None``, ``4096``;
# - a plain scalar of several lines is one string, so its first word is no
#   constant: only ``_CODE_WORD`` refuses it (``12345``, then ``FAKEopaque``
#   on the next line, is a value);
# - a block's lines are a string whatever they hold, so a block is refused
#   only when each of its lines is a placeholder or a reference and nothing
#   else (``_YAML_PLACEHOLDERS``: ``{{ vault_secret }}``,
#   ``${{ secrets.X }}``, ``$API_TOKEN``, ``<value>``); one that starts with
#   a bracket or a number (``[FAKE]opaque``, ``12345``) is a value.
#
# An upper-case or a well-known name's value is anything. A line break is the one
# the key's line ends in, as written or written out behind up to seven
# backslashes (group ``ybs``, as JSON, JSON in JSON and three deep write it);
# written out, a line also ends where the string does, at a quote behind
# fewer backslashes. Each candidate starts at a line's start, an escape or a
# quote and reads its key, one line break and the lines of its value, each
# once: every loop is possessive, so a candidate that fails does so at the
# start of its value.
#
# The key's column is its indentation (``ind``) and the width of a YAML
# sequence's dashes before it (#285 review, round 5): in ``- x_token: |``
# the key stands at the third column, and a value's line must be indented
# past it. Only ``ind`` counted until round 5, so a sibling entry of the
# same mapping (``  endpoint: ...`` below the block) was indented more and
# went with the block, a diagnostic lost, and under a placeholder block it
# made the block a value. A regex cannot measure a run, so the dashes'
# width is told by which of the empty groups ``dw2`` to ``dw16`` is set
# (``dwx`` past 16, where a line must be indented 17 more at least), and
# ``_YAML_IND`` asks for as many spaces again on each value line. The dashes
# are read one character at a time, each step asking whether they end
# there, so a key's dashes are read once. The lead quote, when a quote
# leads, is group ``yq``.
_YAML_DASH_MAX = 16
_YAML_DASH_CHAR = r"(?:-|[^\S\r\n])"


def _yaml_dashes() -> str:
    """The dashes before a YAML key, their width set as a group (above)."""
    tail = rf"(?P<dwx>){_YAML_DASH_CHAR}*+"
    for n in range(_YAML_DASH_MAX, 1, -1):
        tail = rf"(?:(?P<dw{n}>)(?!{_YAML_DASH_CHAR})|{_YAML_DASH_CHAR}{tail})"
    return rf"(?:(?=(?:-[^\S\r\n]++)++(?!-)){_YAML_DASH_CHAR}{{2}}{tail})?"


_YAML_DASHES = _yaml_dashes()
_YAML_LEAD = (
    rf"(?P<lead>(?:^|{_ESCAPE}|(?<=(?P<yq>[\"'])))"
    rf"(?P<ind>[^\S\r\n]*+){_YAML_DASHES}(?P<kq>\\{{0,7}}[\"'])?)"
)
# A value line's indentation: past the key's column.
_YAML_IND = (
    r"(?P=ind)"
    + "".join(rf"(?(dw{n})[^\S\r\n]{{{n}}})" for n in range(2, _YAML_DASH_MAX + 1))
    + rf"(?(dwx)[^\S\r\n]{{{_YAML_DASH_MAX + 1}}})[^\S\r\n]++"
)
_YAML_BREAK = r"(?(ybs)(?P=ybs)(?:r(?P=ybs))?n|\r?\n)"
# A character of a line written out (``ybs``): any but a backslash and a
# quote, a quote behind ``ybs`` backslashes or more, and a run of backslashes
# that is neither the line break nor the start of the string's closing quote.
_YAML_WRITTEN = r"(?:[^\\\"\r\n]|(?P=ybs)\\*+\"|(?!(?P=ybs)[nr])\\++(?!\"))"
_YAML_UNIT = rf"(?(ybs){_YAML_WRITTEN}|[^\r\n])"
_YAML_EOL = rf"(?!{_YAML_UNIT})"
_YAML_CHAR = rf"(?:(?![^\S\r\n]|:(?:[^\S\r\n]|{_YAML_EOL})){_YAML_UNIT})"
_YAML_WORDS = rf"{_YAML_CHAR}++(?:[^\S\r\n]++(?!#){_YAML_CHAR}++)*+"
_YAML_LINE_END = rf"(?=(?:[^\S\r\n]++#{_YAML_UNIT}*+)?[^\S\r\n]*+{_YAML_EOL})"
# What a plain scalar cannot start with, after its properties: another
# node's indicator (a sequence's dash, a flow collection, a comment, a
# block's header, an alias is none), a property its properties did not
# take, a quote (a quoted scalar is matched as one), a code fence.
_YAML_NOT_PLAIN = rf"[-?:](?:[^\S\r\n]|{_YAML_EOL})|[\[\]{{}},#&!|>%@`\"']|\\++[\"']|~{{3}}"
# Blank lines (a block's, before its first line), and blank lines and
# comment lines (before a quoted or a plain scalar), each with its break.
_YAML_BLANKS = rf"(?:[^\S\r\n]*+{_YAML_BREAK})*+"
_YAML_SKIPPED = rf"(?:[^\S\r\n]*+(?:#{_YAML_UNIT}*+)?{_YAML_BREAK})*+"
# The break to the next line of a value, over blank lines, and that line's
# indentation, more than the key's.
_YAML_NEXT = rf"(?:{_YAML_BREAK}[^\S\r\n]*+(?={_YAML_BREAK}))*+{_YAML_BREAK}{_YAML_IND}"
# A plain scalar's line after its indentation, and a line after its first.
_YAML_PLAIN_LINE = rf"(?!#){_YAML_WORDS}{_YAML_LINE_END}"
_YAML_PLAIN_MORE = rf"(?:{_YAML_NEXT}{_YAML_PLAIN_LINE})"
_YAML_BLOCK_LINES = rf"{_YAML_UNIT}++(?:{_YAML_NEXT}{_YAML_UNIT}++)*+"
# A block line that is a placeholder or a reference and nothing else: a
# template's (``{{ x }}``, ``${{ secrets.X }}``), a shell's (``${X}``,
# ``$(cmd)``, ``$X``), a batch file's (``%X%``) or a document's (``<value>``);
# and a block whose every line is one (round 4). Each reads its line once.
_YAML_PLACEHOLDER = (
    rf"(?:\$?\{{\{{(?:(?!\}}\}}){_YAML_UNIT})*+\}}\}}"
    rf"|\$\{{(?:(?![{{}}]){_YAML_UNIT})*+\}}"
    rf"|\$\((?:(?![()]){_YAML_UNIT})*+\)"
    r"|\$[A-Za-z_][A-Za-z0-9_]*+"
    r"|%[A-Za-z_][A-Za-z0-9_]*+%"
    rf"|<(?:(?![<>]){_YAML_UNIT})*+>"
    rf")[^\S\r\n]*+{_YAML_EOL}"
)
_YAML_PLACEHOLDERS = (
    rf"{_YAML_PLACEHOLDER}(?:{_YAML_NEXT}{_YAML_PLACEHOLDER})*+(?!{_YAML_NEXT}{_YAML_UNIT})"
)


# What every key with a value below it holds: its colon, then a block's
# header, a property, a comment or the line break, as written or written out.
_YAML_HINT = re.compile(r":[^\S\r\n]*+[|>!&#\r\n\\]")


def _literal_checks(literal_only: bool) -> tuple[str, str, str]:
    """What a lower-case name's YAML value must not be, as three lookaheads.

    For a plain scalar of one line, one of several lines and a block (see
    above), each applied only where the name is not upper case (group
    ``upper`` of ``_SUFFIX_NAME``); all empty for a name whose value is
    anything (``literal_only`` false).
    """
    if not literal_only:
        return "", "", ""
    return (
        rf"(?(upper)|(?!{_CODE_VALUE}))",
        rf"(?(upper)|(?!{_CODE_WORD}))",
        rf"(?(upper)|(?!{_YAML_PLACEHOLDERS}))",
    )


def _yaml_value(name: str, names: str, name_hint: str, *, literal_only: bool) -> _Pattern:
    """The pattern for a credential name's value on the lines below it.

    ``names`` is the name's regex, ``name_hint`` what every name holds
    (``_Pattern.hint``), ``literal_only`` whether a lower-case name's value
    must not read as code (``_literal_checks``). The key, its separator, a
    block's header, the line break, the blank and comment lines before the
    value and its indentation stay; the value is replaced.
    """
    one_line, lines, block = _literal_checks(literal_only)
    regex = (
        rf"(?m){_YAML_LEAD}(?P<name>{names})(?P<sep>(?(kq)(?P=kq))[^\S\r\n]*+:)"
        rf"(?P<hdr>(?P<blk>[^\S\r\n]++{_BLOCK_HEADER})"
        rf"|[^\S\r\n]++{_YAML_PROPERTY}(?:[^\S\r\n]++{_YAML_PROPERTY})?)?"
        rf"(?P<gap>{_YAML_COMMENT}[^\S\r\n]*+(?:\r?\n|(?P<ybs>\\{{1,7}})(?:r(?P=ybs))?n))"
        rf"(?P<blank>(?(blk){_YAML_BLANKS}|{_YAML_SKIPPED}))"
        rf"(?P<vind>{_YAML_IND})"
        rf"(?(blk){block}{_YAML_BLOCK_LINES}"
        rf"|(?P<vprops>{_YAML_PROPS})?(?:{_VALUE_QUOTE}(?:{_QUOTED_LINES}|{_QUOTED_LINE})"
        r"(?P<close>(?:(?P=vbs)(?P=vq))?)"
        rf"|(?!{_YAML_NOT_PLAIN})"
        rf"(?:{lines}{_YAML_PLAIN_LINE}{_YAML_PLAIN_MORE}++|{one_line}{_YAML_PLAIN_LINE})))"
    )
    return _Pattern(
        name,
        re.compile(regex),
        r"\g<lead>\g<name>\g<sep>\g<hdr>\g<gap>\g<blank>\g<vind>\g<vprops>\g<open>"
        r"***REDACTED***\g<close>",
        hint=_YAML_HINT,
        name_hint=re.compile(name_hint),
    )


# A plain scalar that starts on its key's line and goes on below it (#285
# review, round 4): ``client_secret: FAKEfirst``, then ``FAKEcont`` on a
# more indented line, blank lines between them or not, is one string. The
# name's rule on its line took its first word and kept the lines below in
# the clear. Its first word must not read as code under a lower-case name,
# as for a plain scalar of several lines below its key (``_CODE_WORD``:
# ``api_token: str = Field(`` stays). The key, its separator, the space and
# the properties after it stay, and one marker replaces the value on the
# key's line.
#
# The first line may hold a quote (#285 review, round 5): in a plain scalar
# a quote is a character like any other (``FAKE"first``), but the first line
# held none until round 5, so the name's rule took the word before it and the
# rest of the line and the lines below stayed in the clear. A YAML text in
# JSON writes such a quote behind as many backslashes as its line break and
# more (``\"`` before ``\n``), and its string's closing quote behind fewer,
# so the first line's break is found first (``_YAML_FIRST_BREAK``, setting
# group ``ybs``) and the line is then read as the lines after it are
# (``_YAML_UNIT``). The scan stops at a mapping's ``: ``, a comment, a line
# break and a backslash before an ``n`` or an ``r``, which is the line's
# break written out, so it reads no further than the value's first line or
# the next candidate's name, and a candidate that it finds no break for
# fails there. As written, a quote is the line's, but the one that leads the
# key (group ``yq``, a YAML text in a string that opened before it) closes
# that string.
_YAML_FIRST_BREAK = (
    r"(?=(?:[^\\:\s]++|:(?!\s)|[^\S\r\n]++(?!#)|\\++(?![nr]))*+"
    r"(?:\r?\n|(?P<ybs>\\{1,7})(?:r(?P=ybs))?n))"
)
_YAML_FIRST_UNIT = rf"(?(ybs){_YAML_WRITTEN}|(?(yq)(?!(?P=yq)))[^\r\n])"
_YAML_FIRST_CHAR = rf"(?:(?![^\S\r\n]|:(?:[^\S\r\n]|(?!{_YAML_FIRST_UNIT}))){_YAML_FIRST_UNIT})"
_YAML_FIRST_WORDS = rf"{_YAML_FIRST_CHAR}++(?:[^\S\r\n]++(?!#){_YAML_FIRST_CHAR}++)*+"
# What every such value holds: a line break, as written or written out,
# before a line's indentation.
_YAML_CONTINUED_HINT = re.compile(r"\n[^\S\r\n]|\\n[^\S\r\n]")


def _yaml_continued(name: str, names: str, name_hint: str, *, literal_only: bool) -> _Pattern:
    """The pattern for a plain scalar that starts on its key's line and goes
    on below it, the arguments as for :func:`_yaml_value`."""
    lines = _literal_checks(literal_only)[1]
    regex = (
        rf"(?m){_YAML_LEAD}(?P<name>{names})"
        rf"(?P<sep>(?(kq)(?P=kq))[^\S\r\n]*+:[^\S\r\n]++(?:{_YAML_PROPS})?)"
        rf"{_YAML_FIRST_BREAK}(?!{_YAML_NOT_PLAIN}){lines}{_YAML_FIRST_WORDS}[^\S\r\n]*+"
        rf"{_YAML_BREAK}{_YAML_BLANKS}{_YAML_IND}{_YAML_PLAIN_LINE}{_YAML_PLAIN_MORE}*+"
    )
    return _Pattern(
        name,
        re.compile(regex),
        r"\g<lead>\g<name>\g<sep>***REDACTED***",
        hint=_YAML_CONTINUED_HINT,
        name_hint=re.compile(name_hint),
    )


# A quoted value whose opening quote ends its line (#285 review, round 4): a
# shell string and a YAML flow scalar may start with a line break
# (``GH_TOKEN="``, then ``FAKE"`` on the next line), and the name's rule,
# which takes a value only when its first line holds something
# (``_QUOTED_SPAN``), kept the whole value in the clear. A quote that ends its
# line closes a string literal as often (``field = "Set GITHUB_TOKEN="``), so
# what tells them apart is where the name stands: outside every string its
# line opens before it (``_OUTSIDE_STRINGS``). The name starts its line, or
# stands after text whose quotes and backticks each close on the line (a
# shell's ``export``, ``declare -x``, a log line's prefix, a YAML key's
# indentation and dashes, a quoted key), at a token's start or right after an
# escape (``_ESCAPE``); a name inside a string literal or a code span is
# none. After a ``:`` the value's YAML properties may stand before the quote
# (``client_secret: &cs '``, round 5). The value is the lines after the
# quote to the quote that closes it, as ``_QUOTED_LINES`` reads a value that
# spans lines; it holds something but space, and the opening quote doubled
# is something (round 5: ``''FAKE'`` on the next line is ``'FAKE``, as YAML
# escapes a quote and a shell joins two strings, where the doubled quote was
# taken for the value's close and the value for none); one whose closing
# quote never comes is its next line, up to the line's end, as an
# unterminated value on its name's line is that line. The name, the
# separator and the quotes stay, and one marker replaces the value with its
# first line break.
#
# Each candidate starts at a line's start and reads the line once to the
# name (each character is a string's or not, one way only), and the value
# once; a line break written out is no line break here, since the value's
# quotes then stand behind backslashes, where the name's rule already finds
# it whole.
_LINE_STRING = r"\"(?:[^\"\\\r\n]|\\[^\r\n])*+\"|'[^'\r\n]*+'|`[^`\r\n]*+`"
_OUTSIDE_STRINGS = rf"(?:[^\"'`\\\r\n]|\\[^\r\n]|{_LINE_STRING})*?"
# What every such value holds: a separator, the value's properties if any
# (tried only where a property's ``&`` or ``!`` starts, so that a text with
# none is searched at the pace it was), a quote and a line break.
_QUOTE_BREAK_HINT = re.compile(rf"[:=][^\S\r\n]*+(?:(?=[&!]){_YAML_PROPS})?[\"'][\r\n]")


def _quoted_from_next_line(name: str, names: str, name_hint: str) -> _Pattern:
    """The pattern for a quoted value whose opening quote ends its line,
    the arguments as for :func:`_yaml_value`."""
    regex = (
        rf"(?m)(?P<lead>^{_OUTSIDE_STRINGS}(?:{_ESCAPE}|(?<![A-Za-z0-9_]))(?P<kq>[\"'])?)"
        rf"(?P<name>{names})(?P<sep>(?(kq)(?P=kq))[^\S\r\n]*+"
        rf"(?::[^\S\r\n]*+(?:{_YAML_PROPS})?|=[^\S\r\n]*+))"
        r"(?P<open>(?P<vbs>)(?P<vq>[\"']))(?=[\r\n])"
        r"(?=\s*+(?:(?P=vq)(?P=vq)|(?!(?P=vq))\S))"
        rf"(?:{_QUOTED_LINES}(?P<close>(?P=vq))"
        rf"|(?:\r\n?|\n)(?![^\S\r\n]*+(?:`{{3}}|~{{3}})){_QUOTED_LINE}(?!(?P=vq)))"
    )
    return _Pattern(
        name,
        re.compile(regex),
        r"\g<lead>\g<name>\g<sep>\g<open>***REDACTED***\g<close>",
        hint=_QUOTE_BREAK_HINT,
        name_hint=re.compile(name_hint),
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
# on any line, and was no credential until #285's review. The lookahead for
# it steps over a shorter run whole (``_PEM_SHORT_RUN``, the same wrapping
# allowed), so it reads each character once and a run of 16 at most twice.
#
# Spaces and tabs may stand between the runs too (#285 review, round 5,
# ``_PEM_SPACED_KEY``): a base64 decoder skips them, so a key whose body has
# a space after every eight characters is a valid key, and a short-wrapped
# key whose line breaks were made spaces (as a progress line makes them) is
# one still, but a space parted two runs, so neither was key material. The
# words of prose are parted by spaces too (``paste your key here`` is 16
# letters), so 16 characters parted by spaces and tabs (written out as
# ``\t`` too), and a line break among them, are key material only where they
# hold a digit, a ``+`` or a ``/``, an upper-case and a lower-case letter: a
# key's header often has such 16 (Ed25519's ``MC4CAQAw``, OpenSSH's
# ``b3BlbnNzaC1``), as do all but a few in a million random bodies of 64
# characters, the shortest a key has, and words seldom do. Each
# candidate of it reads 16 characters, four times at most (three lookaheads,
# one for each kind, and the run), and is tried where a short run starts.
_PEM_B64 = r"[A-Za-z0-9+/]"
_PEM_WRAP = r"(?:\r?\n|\\{1,7}r\\{1,7}n|\\{1,7}n)[ \t]*"
_PEM_KEY = rf"(?:{_PEM_B64}(?:{_PEM_WRAP})?+){{15}}{_PEM_B64}"
# The space between two of its characters: spaces and tabs, a line break
# among them. Where neither a space nor a backslash follows a character the
# lookahead fails at once, so reading a run costs little more than its
# characters.
_PEM_HSPACE = r"(?:[ \t]|\\{1,7}t)"
_PEM_SPACE = (
    rf"(?:(?=[\s\\]){_PEM_HSPACE}*+"
    rf"(?:(?:\r?\n|\\{{1,7}}r\\{{1,7}}n|\\{{1,7}}n){_PEM_HSPACE}*+)?+)?+"
)
_PEM_SPACED_KEY = (
    rf"(?=(?:[A-Za-z]{_PEM_SPACE}){{0,15}}+[0-9+/])"
    rf"(?=(?:[a-z0-9+/]{_PEM_SPACE}){{0,15}}+[A-Z])"
    rf"(?=(?:[A-Z0-9+/]{_PEM_SPACE}){{0,15}}+[a-z])"
    rf"(?:{_PEM_B64}{_PEM_SPACE}){{15}}{_PEM_B64}"
)
_PEM_SHORT_RUN = rf"(?!{_PEM_SPACED_KEY})(?:{_PEM_B64}(?:{_PEM_WRAP})?+){{1,15}}+(?!{_PEM_B64})"
_PEM_MATERIAL = rf"(?:{_PEM_KEY}|{_PEM_SPACED_KEY})"


def _token(
    name: str,
    body: str,
    replacement: str,
    *,
    flags: str = "",
    token_chars: str = "A-Za-z0-9_",
    hint: str | None = None,
) -> _Pattern:
    """The pattern for a shape that starts a token, by the rule above.

    ``regex`` finds ``body`` at the start of the text or after any character
    but ``token_chars``. ``after_escape`` finds it directly after an escape
    sequence, taken in as the ``lead`` group, and ``escape_replacement``
    puts that back before ``replacement``, whose ``\\N`` references move one
    group on past it. A new pattern is built here, never on ``\\b``.
    ``hint`` is a regex for what ``body`` always holds, matched with the
    same case as ``body`` (``_Pattern``).
    """
    return _Pattern(
        name,
        re.compile(rf"{flags}(?<![{token_chars}]){body}"),
        replacement,
        re.compile(rf"{flags}(?P<lead>{_ESCAPE}){body}"),
        r"\g<lead>" + re.sub(r"\\(\d+)", lambda m: rf"\g<{int(m[1]) + 1}>", replacement),
        None if hint is None else re.compile(hint),
    )


# The OAuth credential fields a quoted key names: the ``oauth-field`` pattern
# below, and :func:`redact_obj` for a mapping entry under such a key.
_OAUTH_FIELD_NAMES = r"refresh|access|(?:refresh|access|id)_token|chatgpt[-_]account[-_]id"

# Applied in this order, each to the previous one's output, a pattern's
# ``after_escape`` pass right after its ``regex``, in rounds until one
# changes nothing (``_MAX_ROUNDS``); the order is part of :func:`redact`'s
# output and of the class :func:`credential_classes` reports. Two classes
# have four patterns each: a name's value on its line, a quoted value whose
# quote ends that line (``_quoted_from_next_line``), a value on the lines
# below it (``_yaml_value``) and a plain scalar that starts on the line and
# goes on below it (``_yaml_continued``), the last three last of all. An
# ``sk-ant-`` key with at least eight characters after ``sk-`` is replaced
# by ``openai-key`` before ``anthropic-key`` sees it, so it is reported
# under that class.
_PATTERNS: tuple[_Pattern, ...] = (
    # A private-key block (#285), replaced whole: BEGIN line, body and END
    # line. Without an END line before the next run of five dashes, the BEGIN
    # line and the body that follows it (``_PEM_BODY``) are. Either needs key
    # material (``_PEM_MATERIAL``) in what it takes. A block starts anywhere but
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
        rf"(?=(?:[^-A-Za-z0-9+/]++|-(?!----)|{_PEM_SHORT_RUN})*+{_PEM_MATERIAL})"
        rf"{_PEM_ANY}*+-----END {_PEM_LABEL}-----"
        rf"|(?=(?:{_PEM_GAP}|=|{_PEM_SHORT_RUN})*+{_PEM_MATERIAL}){_PEM_BODY})",
        "***REDACTED***",
        token_chars="-",
        hint="-----BEGIN ",
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
        rf"{_IN_STRING}(?P<name>{_ENV_NAMES}){_NAMED_VALUE}",
        _NAMED_REPLACEMENT,
        flags="(?i)",
        hint=_ENV_HINT,
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
        hint="(?i)authorization",
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
        hint="://",
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
        hint="eyJ",
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
        hint="AIza",
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
        hint="glpat-",
    ),
    # GitHub fine-grained PATs
    _token(
        "github-fine-grained-pat",
        r"github_pat_[A-Za-z0-9_]{8,}",
        "***REDACTED***",
        hint="github_pat_",
    ),
    # GitHub classic PATs and app / OAuth tokens
    _token("github-token", r"gh[pousr]_[A-Za-z0-9_]{8,}", "***REDACTED***", hint="gh[pousr]_"),
    # OpenAI-style sk- keys (incl. project variant sk-proj-...)
    _token("openai-key", r"sk-(?:proj-)?[A-Za-z0-9_-]{8,}", "***REDACTED***", hint="sk-"),
    # Anthropic keys
    _token("anthropic-key", r"sk-ant-[A-Za-z0-9_-]{8,}", "***REDACTED***", hint="sk-ant-"),
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
    _token(
        "aws-access-key-id",
        r"A[KS]IA[A-Z0-9]{16}(?![A-Za-z0-9_])",
        "***REDACTED***",
        hint="A[KS]IA",
    ),
    # Groq API keys: ``gsk_`` and 52 characters of ``[A-Za-z0-9]``.
    _token("groq-api-key", r"gsk_[A-Za-z0-9]{20,}", "***REDACTED***", hint="gsk_"),
    # xAI API keys: ``xai-`` and 80 characters of ``[A-Za-z0-9_]``. ``-`` is a
    # token character before it, as for a GitLab token, so that a kebab-case
    # word (``grok-xai-...``) holds none.
    _token(
        "xai-api-key",
        r"xai-[A-Za-z0-9_]{20,}",
        "***REDACTED***",
        token_chars="A-Za-z0-9_-",
        hint="xai-",
    ),
    # npm access tokens: ``npm_`` and 36 characters of ``[A-Za-z0-9]``.
    _token("npm-token", r"npm_[A-Za-z0-9]{20,}", "***REDACTED***", hint="npm_"),
    # Hugging Face access tokens: ``hf_`` and 34 letters.
    _token("huggingface-token", r"hf_[A-Za-z0-9]{20,}", "***REDACTED***", hint="hf_"),
    # Generic long bearer-ish assignments: token/password/secret = <value>,
    # the name also as a quoted key (``"api_key": "..."``, #393).
    # ``_`` is a token character before the name, so a prefixed one
    # (``client_secret=``) is not recognised here; it is
    # ``credential-suffix-assignment``'s (#285), last below.
    _token(
        "secret-assignment",
        r"(token|passwd|password|secret|api[_-]?key)"
        rf"({_QUOTE}\s*(?::{_COLON_TO_VALUE}|={_SPACE_TO_VALUE}))"
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
        rf"(\s*(?::{_COLON_TO_VALUE}|={_SPACE_TO_VALUE}))({_QUOTE}){_OAUTH_VALUE}",
        r"\1\2\3***REDACTED***",
        flags="(?i)",
        token_chars="A-Za-z0-9",
        hint="(?i)_token|chatgpt",
    ),
    # Bare ``refresh=`` / ``access=`` with a token-length value. Those words
    # are ordinary prose, so only an ``=`` assignment of at least 16
    # characters counts; ``access: denied`` and ``refresh=true`` stay.
    _token(
        "oauth-bare-assignment",
        rf"(refresh|access)(=)({_QUOTE})(?:[^\s\"';,&\\*]|{_VALUE_BACKSLASH}){{16,}}",
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
    #   (``_LITERAL_VALUE``), whatever its characters. Its value on the lines
    #   below a YAML key is the last pattern's (``_yaml_value``).
    #
    # A quoted value is replaced whole (``_QUOTED_VALUE``) either way. After
    # every pattern but the YAML ones, so a value of a known shape is
    # reported by its own class, a well-known name is ``env-assignment``'s
    # and an OAuth name (``access_token``, ``oauth_refresh_token``) an OAuth
    # pattern's; what such a pattern left of a quoted value (the part after a
    # space, or after its first line) is replaced here. A candidate
    # starts only where an identifier does and scans no further than its end
    # and the value after it, so the pass is linear.
    _token(
        "credential-suffix-assignment",
        rf"{_IN_STRING}(?P<name>{_SUFFIX_NAME})"
        rf"(?P<sep>{_KEY_QUOTE}(?(upper)\s*(?::{_COLON_TO_VALUE}|={_SPACE_TO_VALUE})"
        rf"|[^\S\r\n]*+(?::{_COLON_TO_WORD}|=[^\S\r\n]*+)))"
        rf"{_VALUE_OPEN}"
        rf"(?P<val>(?(vq){_GUARDED_QUOTED_VALUE}|(?(upper){_UNQUOTED_VALUE}|{_LITERAL_VALUE})))"
        rf"{_VALUE_CLOSE}",
        _NAMED_REPLACEMENT,
        hint=_SUFFIX_HINT,
    ),
    # A credential name's value on the lines below it, as YAML writes one
    # (#285 review, round 3; ``_yaml_value``): a well-known name's under its
    # class, any value, and a prefixed name's under
    # ``credential-suffix-assignment``'s, a lower-case one's when it does not
    # read as code. Last, after every pattern that can replace a part of such
    # a value's lines: whether a line is a plain scalar depends on all of it
    # (a mapping's ``: `` in it makes it none), so a pattern after these could
    # leave a line that a second redaction would take whole. A value of a
    # known shape on the next line is therefore reported by its own class, a
    # well-known or upper-case name's word on the next line by the name's
    # pattern, which this one completes. No pattern before takes a block's
    # header for a value (``_BLOCK_AHEAD``).
    #
    # Before them, a quoted value whose opening quote ends its line, under
    # either kind of name (#285 review, round 4; ``_quoted_from_next_line``),
    # which the name's rule on its line leaves whole; and after them, a plain
    # scalar that starts on its key's line and goes on below it
    # (``_yaml_continued``), whose first word the name's rule took.
    _quoted_from_next_line("env-assignment", rf"(?i:{_ENV_NAMES})", _ENV_HINT),
    _quoted_from_next_line("credential-suffix-assignment", _SUFFIX_NAME, _SUFFIX_HINT),
    _yaml_value("env-assignment", rf"(?i:{_ENV_NAMES})", _ENV_HINT, literal_only=False),
    _yaml_value("credential-suffix-assignment", _SUFFIX_NAME, _SUFFIX_HINT, literal_only=True),
    _yaml_continued("env-assignment", rf"(?i:{_ENV_NAMES})", _ENV_HINT, literal_only=False),
    _yaml_continued("credential-suffix-assignment", _SUFFIX_NAME, _SUFFIX_HINT, literal_only=True),
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
# still the worst it does; one that spans lines holds a character and a line
# break at least (round 3). A value on the lines below its key (round 3,
# ``_yaml_value``) has the key's colon, a line break and an indentation
# besides: ``a_token:\n x\n``, 12 to 25, a factor of 2.08, at one
# character, and a block's header makes it longer; the blank and comment
# lines before it (round 4) stay and make it longer still
# (``a_token:\n\n x\n``, 13 to 26). A quoted value whose quote ends its
# line (round 4) holds that line break and a character (``a_token="\nx"``,
# 12 to 24, the break replaced with the value), and a plain scalar that
# starts on its key's line and goes on (round 4) a character, a line break,
# an indentation and a character (``a_token: x\n y``, 13 to 23). The
# rounds of :func:`redact` (``_MAX_ROUNDS``) are more patterns in that
# order, and compound nothing either, by the argument below.
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


# How many rounds of the patterns :func:`redact` applies at most (#285
# review, round 3). One round applies every pattern in order; a round that
# changed the text is followed by another, until one changes nothing. A
# value's extent depends on the text around it (a quoted value spans lines
# to the first quote of its kind that closes it, a YAML plain scalar is a
# line with no mapping's ``: `` in it), and a later pattern can redact a part
# of that text: a quote inside another value, the line a YAML value is on. A
# second redaction then read differently what the first one left and changed
# it again, and ``credential_classes`` found a credential in text ``redact``
# had already redacted. The round after such a change reads the text as a
# second call would, so :func:`redact` returns what redacting it again
# returns. A text with nothing to redact takes one round, as before; one with
# a credential takes a second, which changes nothing unless such a value was
# found. The rounds compound no growth: each replaces a run of text that is
# no marker, beside text it keeps, as one round does (``MAX_GROWTH_FACTOR``).
# The bound keeps the work linear for a text built so that every round
# enables the next; no text met in testing needed more than two.
_MAX_ROUNDS = 4


def redact(text: str | None) -> str:
    """Redact known secret shapes. Never raises; non-str input -> str()."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    for _ in range(_MAX_ROUNDS):
        redacted = _redact_once(text)
        if redacted == text:
            break
        text = redacted
    return text


def _redact_once(text: str) -> str:
    """``text`` after one round of every pattern, in order."""
    for entry in _PATTERNS:
        text = _apply(entry, text)
    return text


def _apply(entry: _Pattern, text: str) -> str:
    """``text`` with one pattern's passes applied, ``after_escape`` second."""
    if entry.hint is not None and entry.hint.search(text) is None:
        return text
    if entry.name_hint is not None and entry.name_hint.search(text) is None:
        return text
    text = entry.regex.sub(entry.replacement, text)
    if entry.after_escape is not None:
        text = entry.after_escape.sub(entry.escape_replacement, text)
    return text


def credential_classes(text: str) -> tuple[str, ...]:
    """The class names of the patterns :func:`redact` would apply to ``text``.

    Runs the same rounds of sequential substitution as :func:`redact` and
    records each pattern that changed the text, in the order in which a
    pattern of each class first did (pattern order, round by round) and each
    name once. The result is empty exactly when ``redact(text) == text``: a
    text with a credential shape is refused by name, never redacted and
    published.
    """
    found: list[str] = []
    redacted = text
    for _ in range(_MAX_ROUNDS):
        start = redacted
        for entry in _PATTERNS:
            replaced = _apply(entry, redacted)
            if replaced != redacted and entry.name not in found:
                found.append(entry.name)
            redacted = replaced
        if redacted == start:
            break
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
