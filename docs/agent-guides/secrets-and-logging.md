# Secrets and logging

Read this before changing what gets written to run logs or state
(`src/autoforge/runlog.py`), the redaction patterns (`src/autoforge/redaction.py`),
error messages that may embed command output or environment values, or
anything that prints subprocess arguments, headers or configuration.

---

## Secrets and logs

Never print or commit:

- GitHub tokens
- API keys
- SSH private keys
- credential files
- authorization headers
- environment dumps containing secrets

Redact common patterns before persisting logs, including the values of:

```text
GITHUB_TOKEN
GH_TOKEN
OPENAI_API_KEY
ANTHROPIC_API_KEY
ANTHROPIC_AUTH_TOKEN
CLAUDE_CODE_OAUTH_TOKEN
OPENCODE_SERVER_PASSWORD
Authorization: Bearer ... / Token ... / Basic ...
```

the value of any name that ends in `_API_KEY`, `_TOKEN`, `_SECRET` or
`_PASSWORD` behind a prefix (`GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, any
value; `client_secret`, a quoted value or a word that does not read as
code), on its line or on the lines below it as a YAML key, also as a
mapping's key, the well-known token shapes on their own (`ghp_*` and
the other classic GitHub prefixes, `github_pat_*`, `sk-*`, `sk-ant-*`,
Google API keys `AIza...`, three-segment `eyJ...` JWTs, AWS access key ids
`AKIA...` / `ASIA...`, `glpat-*`, `gsk_*`, `xai-*`, `npm_*`, `hf_*`), a
private-key block whole, and the whole userinfo of a URL
(`https://x-access-token:...@github.com/...`), each where it starts a token
([below](#where-a-recognised-credential-starts-284); the shapes #285 added
are [further below](#the-remaining-credential-shapes-285)).

Redaction is defense in depth; do not claim it detects every possible secret.

Redaction can lengthen a text (a short secret becomes the fixed marker), and
`redact` guarantees it never returns more than `MAX_GROWTH_FACTOR` times its
input. Persisted bounds applied *after* redaction (the review-history
resolution bound in `loop_guard`) depend on that factor, so a new pattern
must keep it, or raise it together with those bounds and their tests.

Do not log the entire process environment.

## Where a recognised credential starts (#284)

A pattern recognises a credential only where it starts a token, so that a
shape inside a longer identifier is not taken for one (`risk-assessment`
holds `sk-assessment`, `laughs_together` holds `ghs_together`). Until #284
that start was a regex word boundary (`\b`), and there is none after the
`m` of an ANSI colour code (`ESC[0m`) or the `n` of a `\n` written out as
two characters: a token right after one was kept in the clear, and
OpenCode's `stderr.log` carries such sequences
(`ESC[91mESC[1mError: ESC[0m<message>`, `providers.py`). Every pattern of
`redaction.py` is now built on one rule (`_token`; the quoted `oauth-field`
shape, which a quote starts, needs none, nor do the six that start at a
line's start: the two that find a value on the lines below a YAML key and
the two that find a plain scalar going on below its key's line, whose key
starts its line, and the two that find a quoted value whose quote ends its
line, whose name stands outside every string its line opens; a line starts
after a line feed or a carriage return, and each takes an escape before the
name as a line's start or a token's, #285; nor do the four that find a
value below a key in a YAML flow collection, whose key stands after the
collection's `{`, `[` or a `,`, eighth review of #285, nor the six of
the ninth review: the four that find an explicit key's value, whose `?`
starts its line as a key does, and the two that find a flow collection's
value closed by its indicator). A credential
starts:

- at the start of the text;
- after any character that cannot be part of it: anything but an ASCII
  letter, a digit or `_`. A JWT and a Google API key exclude `-` as well,
  which base64url admits, and so do a GitLab token and an xAI key (#285);
  the OAuth assignment names (`refresh_token=`, `id_token:`) do not exclude
  `_`, so a prefixed `oauth_refresh_token=` is caught; a private-key block
  excludes only `-`, since no identifier holds one (#285);
- directly after an escape sequence: an ANSI / VT sequence (a CSI, after
  `ESC[` or the C1 byte, or a two-character escape such as `ESC(B`), its
  ESC also as JSON, C, a shell and caret notation write it (`\u001b[0m`,
  `\x1b[1m`, `\033[0m`, `\e[0m`, `^[[0m`); or a backslash escape written
  out (`\n`, `\t` and the other single letters, `\xHH`, `\uHHHH`,
  `\UHHHHHHHH`, octal). The escape is kept; only the credential after it
  is replaced.

Every sink that redacts goes through these patterns (the run log and its
`stdout.log` and `stderr.log`, state, the CLI's output), and so does the
content policy's `credential_classes`, so the rule holds in all of them.
What changed against `\b`, beyond the escapes, each pinned in
`tests/test_redaction.py`:

- a non-ASCII letter or digit right before a token is a boundary now,
  where `\b` took it for a word character: `caféghp_...` is redacted;
- a backslash before one of the escape letters counts as an escape
  wherever it stands, a path separator (`C:\Users\nghp_...`) or a
  written-out escaped backslash (`\\n`) included: the rule errs on
  redacting.

Unchanged: a shape right after an ASCII letter, a digit or `_` is still no
token (`xghp_...`, `my_GITHUB_TOKEN=`), nor is one after an escape that
more of an identifier follows (`ESC[0mxghp_...`). A name behind a prefix
(`GEMINI_API_KEY=`, `MY_GITHUB_TOKEN=`, `client_secret=`) is recognised
since #285 when it ends in a credential suffix
([below](#the-remaining-credential-shapes-285)). A new pattern is built
with `_token`, never on `\b`, as the Google API key class
([below](#google-api-keys-282)) and the classes #285 added are.

A pattern that starts a token runs as two passes, the second, which takes
the escape in and puts it back, only at an ESC, a backslash, a caret or the
C1 byte. Both are linear. `MAX_GROWTH_FACTOR` is unchanged: the escape is
no part of the replaced run, and at two characters or more it separates
repeated matches no tighter than a one-character separator does.

## Google API keys (#282)

OpenCode launches inherit the `GEMINI_*` and `GOOGLE_*` variables
(`OpenCodeProvider.environment_names`), and no pattern knew a Google API
key: the prefixed names are no `secret-assignment` (`_` before `API_KEY`
is a token character, #285), and the key matched no token shape. An agent
that printed its environment, or an error that quoted the key, put it in
`stdout.log` or `stderr.log` in the clear, and agent text carrying one
passed the published-content check.

The `google-api-key` class recognises the key by its shape, under any name
or none: `AIza` and 35 characters of `[0-9A-Za-z_-]`, 39 in all,
case-sensitive. The whole token must be the key: `-` is a token character
on both sides, as for a JWT, and a run of the key's alphabet that goes on
past the 39 is no key, so a near-miss of either length, a wrong prefix and
a shape inside a longer identifier are left (each pinned in
`tests/test_redaction.py`). It is built with `_token`, so it is recognised
after an escape sequence too (`ESC[0mAIza...`, `\nAIza...`). It runs after
the JWT pattern, which takes a key that is one of its segments with it, and
before the GitHub and `sk-` shapes: `-` is no token character for those, so
one starting after a `-` inside a key would otherwise replace a slice of it
and leave the key's first characters in the clear.

Like every class it reaches each redacting sink (the run log, its
`stdout.log` and `stderr.log`, state and the CLI's output) and
`credential_classes`, so the published-content, payload and commit-message
checks refuse a text carrying one by name, never rewriting it. The body is
fixed, so the pattern stays linear, and a key shrinks under redaction (39
characters to the 14 of the marker), so `MAX_GROWTH_FACTOR` is unchanged.
A value of no known shape under a prefixed name
(`GEMINI_API_KEY=<anything else>`) is recognised by the name since #285
([below](#the-remaining-credential-shapes-285)); a Google key under one is
still reported as `google-api-key`, the shape running first.

## The remaining credential shapes (#285)

The #257 audit (finding M4) listed the shapes no pattern knew after #282:
AWS access key ids, private-key blocks, GitLab, Groq, xAI, npm and Hugging
Face tokens, and a value of no known shape under a prefixed name such as
`GEMINI_API_KEY` or `DEEPSEEK_API_KEY`, which an OpenCode launch inherits
and an agent printing its environment prints. Any of them reached a run log
in the clear and passed the published-content checks. Each is now a class
of `redaction.py`, so it reaches every redacting sink (the run log, its
`stdout.log` and `stderr.log`, state, the CLI's output) and
`credential_classes`, whose published-content, payload and commit-message
checks refuse it by name, never rewriting it.

| Class | What it recognises |
|---|---|
| `private-key` | a PEM block whose label names a private key, replaced whole |
| `gitlab-pat` | `glpat-` and at least 20 characters of `[A-Za-z0-9_-]` |
| `aws-access-key-id` | `AKIA` or `ASIA` and 16 characters of `[A-Z0-9]`, the whole token |
| `groq-api-key` | `gsk_` and at least 20 characters of `[A-Za-z0-9]` |
| `xai-api-key` | `xai-` and at least 20 characters of `[A-Za-z0-9_]` |
| `npm-token` | `npm_` and at least 20 characters of `[A-Za-z0-9]` |
| `huggingface-token` | `hf_` and at least 20 characters of `[A-Za-z0-9]` |
| `credential-suffix-assignment` | a name ending in `_API_KEY`, `_TOKEN`, `_SECRET` or `_PASSWORD` behind a prefix: in upper case with any value, in lower or mixed case with a quoted value or a word that does not read as code; on its line, or on the lines below it as a YAML key |

**The token shapes** are case-sensitive. A floor of 20 characters is below
the length of every real token of each kind (52 for Groq, 80 for xAI, 36
for npm, 34 for Hugging Face, 20 for GitLab's), so a token cut short is
still taken. The short prefixes' alphabets have no `_` (xAI's excepted), so
the snake_case names npm and Hugging Face tools print (`npm_config_registry`,
`npm_lifecycle_event`, `hf_hub_download`) are none. An AWS key id is the
whole 20-character token: a run that goes on is none, nor is an IAM unique
id (`AIDA...`), and the secret key that goes with an id has no shape of its
own (`AWS_SECRET_ACCESS_KEY` names it). The GitLab and xAI shapes take `-`
as a token character before them, so a kebab-case word holding `-glpat-` or
`-xai-` is none. Order matters where `-` is in an alphabet: the GitLab
token runs right after the Google key, before the GitHub, `sk-`, AWS and
prefixed shapes, any of which could start after a `-` inside it and leave
its head in the clear; the shapes without a `-` run after the `sk-` keys
for the same reason, so an `sk-` key whose tail reads like one stays an
`openai-key`.

**A private-key block** is a `-----BEGIN <label>-----` line, the key's body
and an `-----END <label>-----` line, as PEM (RFC 7468) writes one and
OpenSSH and OpenPGP armour borrow. The label names a private key: up to
three upper-case words before `PRIVATE KEY` (`RSA`, `EC`, `DSA`, `OPENSSH`,
`ENCRYPTED`, `SSH2 ENCRYPTED`) or none, or OpenPGP's `PGP PRIVATE KEY
BLOCK`; a public key, a certificate and a label in lower case are left. The
block is replaced whole, its BEGIN and END lines with it, whatever stands
between them (RFC 1421's `Proc-Type:` and `DEK-Info:` headers, OpenPGP's
`Version:` and `Comment:`, a log line's prefix, line breaks written out in
JSON), so a key in a JSON key file keeps the JSON around it intact. It
needs key material, 16 base64 characters in a row: a BEGIN line alone, one
quoted by code (`startswith("-----BEGIN ...")`) or prose, an empty block and
a placeholder (`...`) are no credential, so text about the format can be
published. A line break and the indentation after it may stand between any
two of the 16, as written or written out (`\n`, `\r\n`, a carriage return
alone, `\r`, and a run of carriage returns alone or around a line feed,
each behind up to seven backslashes): a valid key rewrapped
at fewer than 16 characters a line (`fold -w 8`, which OpenSSL still reads)
has no run of 16 on any line, and was missed until the review of #285. A
bare carriage return counted as no line break until the sixth review: a
base64 decoder skips it as it skips a line feed, so an Ed25519 key whose
body's runs of one, four or eight characters bare carriage returns part
(a classic Mac OS file, a terminal's capture) is one OpenSSL reads, to the
same public key, but the whole block reached every sink and passed every
check, as text and in JSON three deep. A run of carriage returns is one
line break too, alone or around a line feed (seventh review): a carriage
return goes back to the start of a line and advances none, so a key whose
runs two carriage returns part (`\r\r`), or a terminal's capture of a CRLF
key file (`\r\r\n`, each line feed made CRLF once more), is one OpenSSL
reads, to the same public key, but the sixth review's rule took the second
carriage return for a second line break, so that, like a blank line, it
parted two runs, and the whole block reached every sink and passed every
check, as text and in JSON three deep. Only a line feed advances a line, so
what still parts two runs is a blank line of two line feeds, carriage
returns among them or not (`\n\n`, `\r\n\r\n`, `\r\r\n\r\r\n`); two
carriage returns alone, a classic Mac OS file's blank line, no longer do.
Spaces and tabs may stand among a line break's carriage returns and line
feed too (eighth review): a base64 decoder skips each of them in any order,
so a key whose runs a carriage return, a space and a line feed part
(`\r \n`), or a carriage return, a space and a carriage return (`\r \r`),
is one OpenSSL reads, to the same public key, but only an indentation after
the break was read, so the space parted two runs and the whole block
reached every sink and passed every check, as text and in JSON three deep.
A line break is now any run of spaces, tabs and carriage returns that holds
a carriage return or a line feed, and one line feed at most, as written and
written out (a tab also as `\t`): a space before a CRLF or after a line
feed, a tab among them. What parts two runs is still a blank line, two line
feeds whatever stands between them (`\n \n`, `\r \r\n \r\n`), which
OpenSSL refuses in a key's body as it refuses `\n\n`. The run is read in
one order by possessive loops, so it is read once.
Spaces and tabs may part the runs too
(fifth review of #285): a base64 decoder skips them, so a key with a space
after every eight characters of its body is one OpenSSL reads, to the same
public key, and so is a short-wrapped key whose line breaks a progress line
made spaces; but a space parted two runs, so the whole block reached every
sink and passed every check. The words of prose are parted by spaces as
well (`paste your key here` is 16 letters), so 16 characters parted by
spaces and tabs (a tab also written out, `\t`), a line break among them,
are key material only where they hold a digit, a `+` or a `/`, an
upper-case and a lower-case letter. A key's body has such 16: a key's
header often does (an Ed25519 key's `MC4CAQAw`, OpenSSH's `b3BlbnNzaC1`),
and all but a few in a million random bodies of 64 characters, the
shortest a private key has, do too (one in 200,000 parted every eight
characters did not, in testing), a longer one fewer still. A
placeholder's words seldom do (`Paste Your Private Key Here`, `your
ed25519 key goes here` and a hex dump in either case stay). The cost: 16
letters of prose between the BEGIN and END lines that mix case and hold a
digit (`Put the RSA 2048 key here`) read as a key. A blank line of two
line feeds still parts two runs, and a short placeholder on lines of its own (`put`,
`your`, `key`, `here`: 14 joined) is no material either. It starts anywhere but after a dash, a letter included (no
identifier holds a BEGIN line), and it runs first, before every other
pattern: a named assignment's value stops at the space in the BEGIN line,
or a quoted one at that line's end (`DEPLOY_SECRET="-----BEGIN RSA ...`),
which would leave the label or the key behind it unrecognised.

A block with no END line before the next run of five dashes (the first
lines of a key file, a capture whose bound cut the key) loses its BEGIN line
and the body after it: runs of base64 parted by spaces, line breaks (also
written out behind up to seven backslashes, as JSON and JSON in JSON write
them) and its headers' colons, commas and single dashes. The line break
after the last run is kept, so escaped JSON still decodes and the escape a
next BEGIN line stands after is not taken. That body runs on to the first
character no key body holds, so a line of words right after a cut key (an
`Error: ...` line) goes with it: the rule errs on redacting. A capture that
kept both ends of a key, the omission marker between them, is one block.
Not recognised: the end of a key whose BEGIN line is not in the text (a
capture that kept only the key's tail, `tail` of a key file).

**A prefixed credential name** (`credential-suffix-assignment`) is a name
of letters, digits and `_`, starting with a letter, or with underscores and
then a letter or a digit, and ending in `_API_KEY`, `_TOKEN`, `_SECRET` or
`_PASSWORD` after a prefix, the suffix in any case. A shell identifier may
start with underscores (`_SERVICE_API_KEY`, `_AUTH_TOKEN`,
`__CLIENT_SECRET`, `_DB_PASSWORD`), and so may a private attribute or a
variable a script keeps to itself, but the name had to start with a letter
until the eighth review of #285, so a value under such a name, as text and
as a mapping's key, was kept in every sink and passed every check. A suffix
behind underscores alone (`_TOKEN`, `__API_KEY`) is still no prefixed name,
and the name still stops where the identifier does. What its value must be
depends on the name's case (an upper-case name's underscores before its
first letter change nothing):

- **Upper case** (`GEMINI_API_KEY`, `MY_GITHUB_TOKEN`, `CI_JOB_TOKEN`,
  `DB_PASSWORD`), an environment variable's: its value is taken as a
  well-known name's is (`env-assignment`), any value, quoted or not,
  behind an escaped quote, under a quoted key, on the next line, and
  directly after an escape.
- **Lower or mixed case** (`client_secret`, `db_password`,
  `my_GITHUB_TOKEN`), which a configuration file, a form body
  (`client_secret=...&grant_type=...`), a query and Google's OAuth client
  JSON hold, but code as often (`def f(api_token: str)`,
  `api_token=api_token`): its value, on the same line, is a quoted one (a
  string literal, a JSON or YAML string) or an unquoted word that does not
  read as code. The word is of any characters, so letters alone
  (`client_secret: changeme`), punctuation (`db_password=Sup3r$ecret!`, a
  `#` or an `&` inside it) and a form body `urlencode` wrote
  (`client_secret=a%2Fb%2Bc&grant_type=...`) are each taken whole. It ends
  at a space, a quote, a backtick, a semicolon, the `&` that starts a form's
  next field (`&name=`, as `urlencode` writes an `&` inside a value as
  `%26`) and the end of the text; a run of `,`, `.`, `:` and closing
  brackets right before one of those is the punctuation after the word
  (`f(x_token=v),`, a sentence's full stop), and part of it anywhere else.
  A backslash that starts an escaped quote or a line break or tab written
  out is such an end too, as it ends the word itself (eighth review): in
  JSON in JSON a string's closing quote is `\"`, and the punctuation before
  it was the word's, so a flow mapping written out two deep lost its `}` to
  the marker, and code before it (`client_secret: None,`) was redacted,
  where one deep left each.
  Until the second review of #285 the word had to be 8 characters of a
  token's alphabet with a digit and a letter, so a word of letters, one with
  `$` or `!`, and a `%` escape were kept whole, in every sink, and passed
  every published-content and commit-message check.

  A word that reads as code is no value, so the code an agent quotes stays
  publishable. That is an identifier that names a credential itself, alone
  or the last of a dotted name (`api_token=api_token`,
  `client_secret=self.client_secret`, `api_token=accessToken`); a dotted name
  with no digit (`api_token=args.tok`); a call, a subscript or a generic
  (`x_token = sha256(data)`, `os.environ["X"]`, `Optional[str]`,
  `Option<String>`); a type, a constant or a keyword a statement starts with
  (`api_token: str`, `client_secret: SecretStr`, `page_token=None`,
  `use_token: true`, `if not api_token: return`); a reference or a
  placeholder (`$API_TOKEN`, `${X}`, `$(cat f)`, `%API_TOKEN%`, `%s`, a word
  that opens a bracket: `{token}`, `{{ vault_secret }}`, `<value>`,
  `[REDACTED]`); a number (`max_token: 4096`); and a word with no letter or
  digit before it ends (`...`, `|`, the second character of `:=` and `==`).
  On the lines below the name, a value is the name's only under a YAML key
  ([below](#a-value-on-the-lines-below-a-yaml-key)): a statement that ends
  in a colon is none (`if api_token:`).
  The costs, both deliberate: a literal of one of those shapes is not
  recognised under a lower-case name (`db_password=password`,
  `db_password: hello.world`, a PIN of digits alone), as it is under an
  upper-case one; and a variable that names no credential is taken for a
  literal, redacted in a log and refused in published text
  (`client_secret=cs`, `{"client_secret": value}`), since it reads exactly
  as a password of letters does.

**A quoted value is replaced whole**, for this rule and `env-assignment`:
it runs to the quote that closes it, its spaces, semicolons and the other
quote character included (`DB_PASSWORD="correct horse; battery"`), where it
stopped at the first space or semicolon and left the rest in the clear
until the review of #285. The closing quote is the opening one behind as
many backslashes; the same quote behind more is escaped inside the value
(`"a\"b"` as a shell writes it, `\"a\\\"b\"` as JSON writes that), so
the value is found whole as text and as JSON, JSON in JSON and three deep
write it, and the escaped closing quote stays, so the JSON still decodes.
The opening quote doubled is one inside the value (`'it''s'`, as YAML and
SQL escape a single quote and a shell joins two strings). A value runs
across line breaks to its closing quote, as a shell string and a YAML flow
scalar do (`DB_PASSWORD="first` on one line, `second"` on the next). The
second review of #285 found both kept in the clear: the value stopped at the
line break, or at the doubled quote, and what followed reached
`stdout.log` and `stderr.log`.

A value spans its lines whatever they hold: blank lines, the other quote
character and backticks included (a single-quoted shell string holds a
literal backtick). Until the third review of #285 a value spanned lines
only while they held none of those, so a valid shell or YAML value holding
one was redacted to the end of its first line and its other lines reached
`stdout.log`, `stderr.log` and `control-result.json`. Its closing quote is
the first quote of its kind that nothing escapes. What tells a quote that
opens no value (the end of a string literal in code, a quote in a Markdown
code span, in a log, a prompt or the read context the controller redacts
for an agent) from one that opens a value is where the quotes stand, not
what lies between them:

- the closing quote is followed by no letter, digit, `_` or `*`: a quote
  that a word follows opens the next string (`say "this"`,
  `log('done')`), and `*`, the marker's first character, is one so that a
  value redacted after the quote reads the same;
- the value's first line holds something, on the name's line: a quote
  that ends its line closes a string literal (`field = "Set
  GITHUB_TOKEN="`) as often as it opens a value that starts with a line
  break, so such a value is recognised only where its name stands outside
  every string its line opens before it
  ([below](#a-quoted-value-whose-quote-ends-its-line));
- the name does not stand right after a quote or a backtick, unless it is a
  quoted key (`"GITHUB_TOKEN": "..."`): there it starts a string literal or
  a code span (`print('GH_TOKEN=' + read("f"))`, a code span opened right
  before `DEPLOY_SECRET="...`), and the quote after its separator closes
  that string;
- no line it spans starts a code fence.

A value that does not span lines, or whose closing quote never comes (the
end of the text, or the same quote behind fewer backslashes, which closes
the string it stands in: `"X_TOKEN=\"cut"`), runs to its closing quote on
its line, or to the end of its line or string, and no further, as before.
Not recognised: the lines after the first of a value in a Markdown code
span opened right before its name, and of one whose closing quote a word
follows (a shell joins `'a'b` into one word).

### A quoted value whose quote ends its line

A shell string and a YAML flow scalar may start with a line break
(`GH_TOKEN="` on one line, `FAKE"` on the next, valid in both). Until the
fourth review of #285 such a value was no value at all: the rule above
takes a value only when its first line holds something, so the whole of it
reached `stdout.log`, `stderr.log` and `control-result.json` and passed
every published-content, payload and commit-message check, under every kind
of name. A quote that ends its line closes a string literal as often
(`field = "Set GITHUB_TOKEN="`), and what tells the two apart is where the
name stands, not what follows it: two patterns, run after the name's rule
(`_quoted_from_next_line`, reported as `env-assignment` under a well-known
name and as `credential-suffix-assignment` under a prefixed one, in any
case), take such a value where the name stands **outside every string its
line opens before it**:

- the name starts its line, or stands after text whose quotes and
  backticks each close on that line: a shell's `export`, `declare -x` or
  xtrace's `+`, a log line's prefix, `echo "a";`, a YAML key's indentation
  and dashes, or as a quoted key (`"client_secret": "`);
- it starts a token there, or stands right after an escape sequence
  (OpenCode's `ESC[0m`);
- a name inside a string literal or a code span, whose quote after the
  separator closes that string (`help = "Set GITHUB_TOKEN="`,
  ``Set `GH_TOKEN="` ``), is none, and neither is a value of space alone.

After a `:` the value's YAML properties, an anchor or a tag, may stand
before the quote (`client_secret: &cs '`;
[below](#a-value-on-the-lines-below-a-yaml-key)). A value whose next line
starts with the quote doubled holds that quote: `''FAKE'` on the line after
`client_secret: '` is `'FAKE` (with the line break folded), as YAML escapes
a quote and a shell joins two strings. Until the fifth review of #285 the
doubled quote was taken for the value's close, so the value held nothing
and the whole of it passed every check.

The value is the lines after the quote to the quote that closes it, read as
a value on its name's line that spans lines is (above); one whose closing quote never comes is its next line, as an unterminated
value on its name's line is that line. The name, the separator and the
quotes stay, and one marker replaces the value with its first line break
(`GH_TOKEN="***REDACTED***"`). Written out (JSON text), the line break is a
`\n` and the quotes stand behind backslashes, where the name's rule on its
line already takes the value whole. Each candidate starts at a line's
start and reads the line once to the name, each character a string's or
not one way only, and the value once.

**An unquoted value ends at a line break written out** (`\n`, `\r` and
`\t`, as JSON writes them), for this rule, `env-assignment` and
`oauth-assignment`, as it ends at one written as such. It used to run on
into the next line, and where that line was `NAME: value` in JSON text (an
agent's result quoting a YAML file: `"A_TOKEN: x\nB_TOKEN: y"`), it took
the name and left the value in the clear. The name now starts a token after
the escape ([above](#where-a-recognised-credential-starts-284)), and its
value is replaced in turn.

**A line break written out starts no value; it is crossed** (sixth review
of #285). A value that started with one was taken to be that break and the
rest of its line, so in JSON text a value below its name lost the break to
the marker and kept the line below in the clear (`"GH_TOKEN:\n  FAKE"`
became `"GH_TOKEN:***REDACTED***  FAKE"`), and a YAML value's pattern no
longer found the line it is told by (a block's header below an upper-case
key, `"GH_TOKEN:\n  |\n  FAKE"`, kept its block). The separator now crosses
a line break written out, behind up to seven backslashes, with the blank
lines and the indentation after it, as it crosses one written as such:
`GH_TOKEN=\nabc` in JSON text is `GH_TOKEN=\n***REDACTED***`. A carriage
return written out alone (`\r`) is such a line break too (seventh review):
only `\n` and `\r\n` were crossed until then, and a value never starts with
`\r` either, so in JSON text a value below its name after a bare carriage
return (`"GH_TOKEN:\r  FAKE"`, `"DEPLOY_SECRET:\r  FAKE"`) was kept whole,
though the same text, as written, was redacted. It crosses
to an unquoted value only: a quote right after the break may close the
string the text is written in (`"GH_TOKEN=\n"`, then the document's next
string), so a quoted value below its name in JSON text is the YAML
patterns', which tell that quote by its backslashes
([below](#a-value-on-the-lines-below-a-yaml-key)), and after `=` it is not
recognised.

**An OAuth value ends where a lower-case name's word does.** Every OAuth
name but `chatgpt-account-id` is a prefixed name too (`refresh_token`,
`oauth_refresh_token`): `oauth-assignment` takes its value first, and this
rule what it left. Until the third review of #285 the OAuth value ran on
over a backtick and over a run of `.`, `:` and closing brackets before the
value's end, which this rule leaves after its marker as the punctuation
after a word, so a second redaction removed it: `refresh_token=,word.`
lost its full stop to the second call, and `credential_classes` found a
credential in the first call's output. The OAuth value now also ends at a
backtick and before such a run (and, as before, at a comma and any `&`),
so one round of the patterns gives text that a second leaves, over every
OAuth, prefixed and well-known name, separator and value of the characters
one rule ends a value at and the other does not
(`tests/test_redaction.py`).

The rule runs last, after every other pattern, so a value one of them knows
is reported by that pattern's class (`GEMINI_API_KEY=AIza...` stays
`google-api-key`, `NPM_TOKEN=npm_...` `npm-token`), a well-known name is
still `env-assignment`'s, and an OAuth name (`access_token`,
`oauth_refresh_token`) an OAuth pattern's; what such a pattern left of a
quoted value, after a space, is replaced here and reported as this class
too. Not recognised: a suffix the rule does not take (`SSH_KEY`,
`*_CREDENTIALS`), a camelCase name (`clientSecret`), a bare generic name
(`password=`), which stays `secret-assignment`'s, with its value of 12
token characters, and, for this rule and `env-assignment` alike, the words
after the first of an unquoted value on the name's line that holds spaces:
an unquoted value is one word, as a shell reads one, so a YAML plain scalar
or an INI value of several words on the key's line (`DB_PASSWORD: two
words`) keeps all but its first, and one with a quote inside it keeps what
follows the quote (`client_secret: FAKE"rest`; a quote ends an unquoted
value, as it ends one in JSON text). Quoted, on the lines below a YAML key,
or going on to a more indented line below its key's
([below](#a-value-on-the-lines-below-a-yaml-key)), such a value is taken
whole.

**As a mapping's key.** `redact_obj` redacts a mapping's key and its value
apart, and neither `GEMINI_API_KEY` nor an opaque value is a credential on
its own, so `{"GEMINI_API_KEY": "..."}` (an `env` mapping in a record's
metadata, a free-form `escalation`, a CLI document) kept the value in the
clear until the review of #285. A key that names a credential, whole and in
any case, now has its non-empty string value replaced whole, at any depth:
a prefixed name with a credential suffix (`GEMINI_API_KEY`,
`client_secret`, `_SERVICE_API_KEY` and `__CLIENT_SECRET` since the eighth
review), a well-known name (`GITHUB_TOKEN`) and, as before, an
OAuth field (`refresh`, `id_token`). A mapping's string value is a literal,
as a quoted value is in a text, so a lower-case key needs no more than an
upper-case one. The entry and its key stay, a non-string or empty value is
left, and a bare generic key (`token`, `password`) is not recognised.

`MAX_GROWTH_FACTOR` is unchanged. The shortest recognised name is now a
one-letter prefix and a suffix: `A_TOKEN=x`, 9 to 22 characters (2.44),
below the URL shape's 2.86, and `a_token=x` in lower case as well; a quoted
value taken whole, across lines and doubled quotes too, is still at least
one character (`a_token="x"`, 11 to 24), and one that spans lines a
character and a line break. A value on the lines below a YAML key has the
key's colon, a line break and an indentation besides (`a_token:\n x\n`, 12
to 25, 2.08), and a block's header, and the blank and comment lines before
it, which stay, make it longer (`a_token:\n\n x\n`, 13 to 26). A quoted
value whose quote ends its line holds that line break and a character
(`a_token="\nx"`, 12 to 24), and a plain scalar going on below its key's
line a line break and an indentation besides its first character
(`a_token: x\n y`, 13 to 23). The fifth review's shapes are longer still:
an anchor before a value stays (`a_token: &a x`, 13 to 26), a doubled quote
after a quote's line break is two characters of the value (`a_token='\n'''`,
13 to 24), a quote in a plain scalar's first line one more (`a_token:
x"\n y`, 14 to 23), and a sequence's dashes stay with the key. The sixth
review's keep more and replace no less: properties or a block's header on
lines of their own stay (`a_token:\n &a\n x\n`, 15 to 28), and so does a
line break written out before a value (`A_TOKEN=\nx` in JSON text, 11 to
24, where the break went with the value, 11 to 22). The seventh review's
are as long as the same with line feeds: a value whose lines bare carriage
returns end (`a_token:\r x\r`, 12 to 25) and a carriage return written out
before a value (`A_TOKEN=\rx`, 11 to 24). The eighth review's are no
worse: a value below a key in a YAML flow collection (`{a_token:\nx}`, 12
to 25; `,a_token:\nx`, 11 to 24), a plain scalar going on below such a
key's line (`{a_token: x\ny}`, 14 to 25) and a name behind an underscore
(`_A_TOKEN=x`, 10 to 23); a flow key's lead may hold an earlier marker's
last `*`, which it keeps. Every token
shape is longer than the marker and only shrinks, a private-key block (43
characters or more) included, and what a block holds between its BEGIN and
END lines, like a quoted value, can take a marker only whole. A mapping's
value under a credential key grows from one character to the marker's 14,
but its key comes with it, the shortest (`access`) six characters, so an
entry grows 7 to 20 at worst; no parser-bounded field (a finding, a
resolution) has such a key, so the per-string bounds that depend on the
factor hold.

Every pattern stays linear. A token shape scans one run of its alphabet,
and the name rule one identifier and its value. A quoted value is read once
to its close, and once more to its line's end when it spans lines and never
closes; a later candidate's quote closes it, unless more backslashes escape
that quote, which no more than eight candidates in a row can do; a blank
line, the other quote and a backtick no longer stop it, so a candidate of
one quote reads the lines of a candidate of the other, each line twice at
most. A lower-case name's unquoted word is read once: one that holds the
next candidate's name is taken whole and not read again, and the code rule
reads no further than the first word of a value it refuses. A private-key
candidate scans to the next run of five dashes, where the next candidate
starts, at most three times (its key-material lookahead steps over a run
shorter than 16 whole, line breaks included), and where a short run starts
it reads the 16 characters that spaces may part four times at most, once
for each kind of character they must hold and once for the run. A YAML
key's candidate reads its key, the dashes before it a few times (a
lookahead for each run of bits whose lowest the width reaches, one chunk
for each bit, the larger ones failing at the dashes' end; a dash or two are
read about 20 times, a megabyte of them about three), one line break, the
properties and headers on lines of their own, the blank and comment lines
before its value and its value's lines once, a block's lines once
more for its placeholder check, and a plain scalar's first line once more
to its break, which a mapping's `: ` stops, so the next candidate's key
ends it ([below](#a-value-on-the-lines-below-a-yaml-key)); a quoted value whose
quote ends its line is read from its line's start once
([above](#a-quoted-value-whose-quote-ends-its-line)); and the rounds are
four at most ([below](#redaction-in-rounds-and-the-patterns-hints)).
`tests/test_redaction.py` pins each shape, its near-misses, the order, the
growth units and the linear bounds, and `test_result_parser.py`, `test_runlog.py`, `test_engine.py` and
`test_cli.py` each class in every check and sink.

### A value on the lines below a YAML key

Until the third review of #285 a lower-case name's value had to be on the
name's line, so a value on the lines below a YAML key (an indented scalar,
a literal `|` or a folded `>` block) was never recognised, under any of the
four suffixes: it reached every sink and passed every published-content and
commit-message check. Two patterns now find one, near the last of all:
under a well-known name, reported as `env-assignment`, any value; under a
prefixed name, reported as `credential-suffix-assignment`, any value in
upper case, and in lower case one that does not read as code. The fourth
review of #285 found three ways round them, each valid YAML that reached
every sink and passed every check: a blank or a comment line between the
key and its value, a blank line among a plain scalar's lines (the lines
after it were kept), and a block or a plain scalar of several lines whose
first line the code rule refused (`[FAKE]opaque`, `12345` then
`FAKEopaque`). Each is now taken whole. The fifth review found two more
and a boundary: a scalar after an anchor or a tag, which were taken for
another node (`client_secret: &cs 'FAKE'`, `api_token:` then
`  !!str FAKE`), and a plain scalar whose first line holds a quote
(`client_secret: FAKE"first` then `  FAKEcontinued`) kept their values,
and a value in a sequence entry (`- x_token: |`) took the mapping's next
entry with it. Each is now taken whole, and the next entry stays. The
sixth review found the rest of those: the properties alone on a line below
the key, or split over two (`client_secret:`, then `  &cs`, then
`  'FAKE'`), and a block's header on a line of its own below the key (`  |`
under `client_secret:`, under `GH_TOKEN:` in JSON text too) kept their
values whole, live progress lines included; and past 16 columns of dashes
(nine nested sequences, or a dash and 17 spaces) the next entry still went
with the value. Each is now taken whole, and the next entry stays at any
width.

```yaml
client_secret:          db_password: |          x_api_key: >-
  <value>                 <line>                  <line>
                          <line>                  <line>

api_token:              Db_Password:            client_secret: <word>
                        # rotated                 <line>
  <line>                  <value>
                                                  <line>
  <line>
```

- **The key starts its line**: after its indentation, a YAML sequence's
  dashes (`- client_secret:`), or the `?` and `:` of compact notation that
  stand where they do (`? client_secret:`, an explicit key that is itself a
  mapping, ninth review of #285), as a quoted key (`"client_secret":`), or
  right after an escape sequence (OpenCode's `ESC[0m`, a `\n` written out)
  or a quote that starts a string (YAML in JSON). A statement that ends in
  a colon does not (`if api_token:`, `while client_secret:`), so code
  stays publishable. Nothing but a block's header, the value's properties
  (Ansible's `!vault |`, `&cs`, `!!str`) and a comment follows its colon
  on its line.
- **The key's column counts a sequence's dashes**: in `- x_token: |` the
  key stands at the third column, and a value's line must be indented past
  it. Until the fifth review of #285 only the indentation before the dashes
  counted, so the mapping's next entry (`  endpoint: ...` below the block)
  was indented more and went with the value, the diagnostic lost, and
  after a block of placeholders it made the block a value and the YAML was
  refused. The fifth review's fix told the dashes' width apart up to 16
  columns and asked for 17 spaces past that, so nine nested sequences (a
  key at column 18) still lost their next entry. A regex cannot compare
  the lengths of two runs, so the width is now written in binary (sixth
  review): one empty group for each bit the width holds, read high bit
  first, each bit a run of its many characters or none, and a value's line
  is asked for that many spaces again, for every bit set, and one more.
  It is exact up to `2**32 - 1` columns, the most a regex repeat counts and
  more than any text holds in a line; a wider run of dashes is no key's,
  and its value is not read below it.
- **The value is on the lines indented more than the key**, past blank
  lines and, before a quoted or a plain scalar, comment lines at any
  indentation, as YAML allows (between a block's header and its first line
  only blank lines may stand: a `#` line there is the block's text): a
  block's lines, blank lines among them, to the first line that is not; a
  quoted scalar, taken as on the key's line, its first line empty too (`"`
  alone on its line, the value on the next); a plain scalar's lines, blank
  lines among them, each with no mapping's `: ` in it, to a comment
  (` #`). A more indented line that is a mapping (`valueFrom:`), a
  sequence, a comment or a code fence is another node, never the value,
  and a key with nothing below its blank and comment lines has none.
- **The properties and a block's header may end their line** (sixth review
  of #285), as YAML allows wherever a node may stand apart from what comes
  before it: below the key, the properties alone on a line, or split over
  two (`&cs`, then `!!str`, then the scalar), a comment after them and
  blank and comment lines below them; and a block's header (`|`, `>-`,
  `|2`) on a line of its own below the key, below the properties or after
  them on their line (`&cs |`), then the block's lines, blank lines before
  them. Only the properties on the key's line or right before the value,
  and a header beside the colon, were read until then, so such a value was
  kept whole, live progress lines included. The properties, the header and
  the lines between them stay. What follows them is the value as above, so
  a nested mapping, a sequence or a flow collection after properties on
  their own line (`&cs`, then `key: value`) is still none, a reference
  after a tag on its own line (`!secret`, then `app_client_secret`) still
  reads as code, and a block of placeholders below a header on its own
  line still stays. An upper-case or a well-known name's rule on its line
  may take a property alone on its line for the value, as it takes any
  word there, and the value below it goes with it: the property is lost,
  never the value.
- **An anchor and a tag are the scalar's properties**, not another node
  (fifth review of #285): `&cs` names the node after it and `!!str` types
  it, so `client_secret: &cs 'FAKE'`, `client_secret: !!str FAKE`, and the
  same below the key, hold a credential as `client_secret: 'FAKE'` does.
  The properties (an anchor, a tag, or both, either order; a verbatim tag
  `!<...>` and the non-specific `!` too) stay, and the scalar after them
  is the value, on the key's line (under every name rule there, the OAuth
  and generic ones included), below it, and before a block's header
  (`&pw |`). A property that a name's rule could take for a value is never
  tried without it, so what follows the properties is what the code rule
  reads: `client_secret: !secret app_client_secret` (Home Assistant's
  reference to its secrets file), `api_token: !ENV ${API_TOKEN}` and
  `!!str ${{ secrets.X }}` stay. A nested mapping or sequence after an
  anchor or a tag (`defaults: &defaults` then `  key: value`) is no value,
  and a Rust reference type is no anchor (`fn f(api_token: &str)`,
  `&'a str` and `&mut String` stay: what follows them reads as code or
  ends no property). An alias (`*name`) is a node of its own and, under a
  lower-case name, a word like any other: it is taken for a value, which
  errs on redacting. The cost: after a `:`, a value that starts with `&` or
  `!` and goes on after a space reads as a property and a value, as YAML
  reads it, so `DB_PASSWORD: !abc def` keeps `!abc` (after `=` nothing is
  a property).
- **A plain scalar may start on the key's line** and go on, past blank
  lines, on more indented lines below it (`client_secret: FAKEfirst` then
  `  FAKEcontinued`, one string): two more patterns, the last of all
  (`_yaml_continued`), take it whole, where the name's rule on its line had
  taken its first word and kept the lines below. Its first line may hold a
  quote, a character like any other in a plain scalar (fifth review of
  #285: `FAKE"first`, where the name's rule took the word before the quote
  and the rest stayed): the first line's break is found first, which says
  how the lines are written, and the line is then read as the lines after
  it are, so a quote written out behind as many backslashes as the line
  break, or more, is the value's, and one behind fewer ends the string it
  stands in. As written, the quote a YAML text in a string opened before
  its key (a JSON array's `"x_token: abc",` line) closes that string, so
  such a line is no continuation and the JSON around it stays.
- **Under a lower-case name it does not read as code**, but what reads as
  code depends on the node. A plain scalar of one line is refused as on
  the key's line: `${{ secrets.X }}`, `$API_TOKEN`,
  `process.env.API_TOKEN,`, `getToken(),`, `str,`, `None`, `4096` stay. A
  plain scalar of several lines is one string, so a number that starts it
  is no constant (`12345` then `FAKEopaque` is a value); every other shape
  still refuses it (`api_token: str = Field(` on several lines,
  `process.env.X` then `|| fallback`, and a keyword or a type such as
  `true` then a word, which is the cost). A block's lines are a string
  whatever they hold, so a block is refused only when every line of it is
  a placeholder or a reference and nothing else (`{{ vault_secret }}`,
  `${{ secrets.A }}` and `${{ secrets.B }}`, `${X}`, `$(vault read ...)`,
  `$X`, `%X%`, `<client-secret>`); one that starts with a bracket, a JSON
  document or a number, or holds a line besides its placeholders, is a
  value. Under an upper-case or a well-known name any value is one.
- **What stays**: the key, its comment, a block's header, the line break,
  the blank and comment lines before the value and its indentation; one
  marker replaces the value (`db_password: |` then `  ***REDACTED***`;
  `client_secret: ***REDACTED***` for a plain scalar that starts on the
  key's line), so the text still reads as YAML, and the lines after the
  value stay. An upper-case name's rule on its line takes any word across
  a line break, a comment's `#` too, as before, so under one a comment
  line before the value goes with it.
- **Written out**, as JSON, JSON in JSON and three deep write a YAML text,
  a line break is the one the key's line ends in, behind as many
  backslashes, and a line also ends where the string does, at a quote
  behind fewer backslashes, so the JSON still decodes. A value's opening
  double quote below its key needs as many backslashes too (eighth review):
  one behind fewer closes the string the text is written in
  (`{"a": "client_secret:\n  ", "b": "x"}`), and was taken for a value's,
  so the text between that string and the next was replaced and the JSON
  no longer parsed. A single quote, which JSON does not escape, opens a
  value behind any.
- **A line breaks where YAML breaks it**: at `\n`, `\r\n` and a carriage
  return alone, `\r` (seventh review of #285), as written and written out,
  and a key starts a line after any of them. Only `\n` and `\r\n` ended a
  line until then, and a key's line started after a line feed only, so a
  value whose lines bare carriage returns end (a classic Mac OS file, a
  terminal's capture) was no key's: a scalar below `client_secret:`, a
  literal or folded block, a property line or a block's header that ends in
  one, even in an otherwise LF document (`&cs`, then a carriage return,
  then the scalar), and the key on a line after one. A YAML parser reads
  each, and each was kept whole in every sink and passed every check, as
  text and in JSON three deep, on a live progress line too. `\r\n` is read
  whole, one break; two carriage returns are two, a blank line, which
  YAML allows where the rules above allow one. The node boundaries hold as
  they do for a line feed: a line no more indented than the key is the
  mapping's next entry and stays, and a statement, a nested mapping and a
  block of placeholders are still no value. NEL, LS and PS (`\x85`,
  `\u2028`, `\u2029`) are no line break here, as YAML 1.2 reads them, and
  count as spaces: a scalar after one on the key's line is that line's
  value and redacted, but a `|` or `>` block whose lines they end, which a
  YAML 1.1 parser such as PyYAML reads, is not recognised.
- **A key in a flow collection** (eighth review of #285): a flow mapping's
  key right after its `{`, after the `,` that ends the entry before it, or
  a single-pair mapping's after a flow sequence's `[`, with its value on the
  line below (`{client_secret:`, then `  FAKE}`), plain, quoted or after its
  properties, or a plain scalar that starts on the key's line and goes on
  below it (`{client_secret: FAKE1`, then `  FAKE2}`). A YAML parser reads
  each, but such a key starts no line, the rules above want one that does,
  and a lower-case name's rule on its line takes a word on that line only,
  so the value was kept whole, or its lines after the first, in every sink
  and check, as text and written out. The key may follow the indicator
  after spaces and line breaks, as written or written out, and be quoted.
  Inside a flow collection a node's lines need no indentation past its key,
  so the value is the first line below it that is no blank or comment line,
  at any indentation. Under a lower-case name a plain scalar ends where a
  flow scalar does: at a flow indicator (`,`, `[`, `]`, `{`, `}`), a
  mapping's `: ` and a comment, and the indicator and what follows it stay
  (`{client_secret:\n  ***REDACTED***, region: r}`); it goes on to the
  lines below while each holds one, and only from a line that it ends. On
  the key's line the name's own rule ends a word only before punctuation
  that an end follows, so a scalar that starts there ends at a `,`, a `]`
  or a `}` only where that rule would end its word too. Under an upper-case
  or a well-known name the value is anything, as on the key's line, where
  `{GH_TOKEN: FAKE}` loses its `}` to the marker: the `}` goes with it, and
  the scalar goes on to indented lines below that hold no mapping's `: `,
  and to the `,` on such a line that the next entry's key and `:` follow
  (`FAKE2, region: r`), which stay. Such a name is read whole, so an
  upper-case one never falls back to a lower-case name's value where its
  own is refused, and a well-known name in lower case (`gh_token`) is read
  as an upper-case one. Such a value takes the `,` that ends its entry, and
  so does a block rule that takes a line whole, so the next entry's key is
  also read after a marker and a space or a line break; after a line break
  a key whose value goes on from its line is read so only where it is
  indented, since at a line's start the line after `key: value` lines that
  redaction left (`db_password: ***REDACTED***`, then `PATH=/usr/bin`) would
  be taken for its scalar's. Each rule thus
  takes, on every line it reads, at least what any other rule could take
  there after its marker: a rule that took a `}` the flow rule stopped at
  would let it go on to the next line in the next round, a line a round. A
  value that is a marker alone is already redacted and no match: a match
  is never read again in its pass, so one that took a marker took the lead
  of the key after it with it, and that key's value stayed for good.
  Where the key starts its line, a value line indented past it is the
  rules above's, which take a line whole, so these read only a value whose
  first line is indented no more than the key, as a flow collection allows
  and a block mapping does not, and read a later line indented past the
  key whole, as those rules do. Under a lower-case name
  a value that reads as code stays, as below a block key (`string`, `None`,
  `12345` alone, `process.env.X`, `!secret app_secret`, `${{ secrets.X }}`),
  and so do a nested mapping, a sequence, an empty value and a code fence.
  The key may follow a `?` (`{? client_secret:`, then the value, ninth
  review of #285), and the next entry's key that tells a `,` may hold
  spaces, be quoted or follow a `?` (`FAKE2, two words: x`, which kept
  `FAKE2` until the ninth review); its words end at the next `,`, so a
  candidate reads them once.
- **A flow collection's value closed by its indicator** (ninth review of
  #285): a plain scalar of several lines in a flow collection, on the key's
  line and below it or below the key alone, at any indentation, up to the
  `,`, `]` or `}` that ends it, on its last line or after blank and comment
  lines. Inside a flow collection a scalar's lines need no indentation and
  a YAML parser reads each as the value's, but under an upper-case or a
  well-known name the rule above went on to indented lines only (its value
  is anything, flow indicators included, so only indentation told its lines
  from what follows the collection), and below a key that starts its line a
  line indented no more than the key, after one indented past it, was no
  rule's: `{CLIENT_SECRET: FAKE1`, then `FAKE2}` at the line's start, kept
  `FAKE2` in every sink and check, as text, written out and on a live
  progress line, and the eighth review's documentation called it a
  deliberate edge. The indicator that closes the value tells its lines
  here: under any name the value is a flow scalar's lines, which hold no
  flow indicator, under a lower-case name when its first word does not read
  as code (`_CODE_WORD`), and it is replaced whole, the indicator and what
  follows it kept; a value of one line is read so too where it stands below
  its `:`, under a lower-case name when it does not read as code. The rule
  runs before every other but the private key's, so it reads the value as
  written, and a value that starts with a marker is none: the rules after
  it may take the `}` after its marker, as `{GH_TOKEN: FAKE}` loses its `}`
  on the key's line, and the marker then stands alone, so no round goes on
  where another stopped. A token of a known shape in such a value is
  reported under the name's class. Each candidate starts at a `{`, a `[` or
  a `,`, reads its key's line once to its first line break (stopping at a
  flow indicator, a second `: ` and a comment) and the value's lines once.
- **An explicit key** (ninth review of #285): YAML writes a key after a `?`
  and its value after a `:` that stands on a later line at the `?`'s
  column (`? client_secret`, then `: FAKE`), blank and comment lines between
  them, the key quoted or not, in a sequence's entry too, and in a flow
  collection the `:` on the line below the key or on its line
  (`{? client_secret:`, then the value). A parser reads it as
  `client_secret: FAKE`, but every rule wanted the key's `:` on the key's
  line, so the value was kept whole in every sink and check. The key, its
  comment, the lines down to the `:` and the `:` are now the separator, and
  the value after it is read as after a key's own `:`: on the `:`'s line
  (plain, going on below it, quoted, after its properties) and below it (a
  scalar, a block, after properties on lines of their own), with the same
  code and placeholder exclusions under a lower-case name and the same node
  boundaries (a `:` at another column, a nested mapping or a sequence is no
  value). An upper-case or a well-known name's `\s*` before a `:` crossed a
  line break as written already, and crosses one written out now too
  (`_TO_SEPARATOR`), so a text reads alike as written and as JSON writes it.
  A candidate that finds no `:` and value matches what it read, its key, its
  comment and the blank and comment lines below it, and changes nothing:
  another explicit key written in such a comment is a candidate of its
  own, and had the first failed each would read the comment lines below it
  again, a run of such lines once for each.

They run last (but the rule for a flow collection's value closed by its
indicator, above, which runs first and reads a value as written), after
every pattern that can redact a part of such a value's lines: whether a line is a plain scalar depends on all of it, so a
later pattern could leave a line that a second redaction would take whole.
A value of a known shape on the next line is therefore reported by its own
class, and a well-known or upper-case name's first word on the next line by
the name's own pattern, which these complete: an unquoted value of several
words is taken whole below a YAML key, where it is still one word on the
key's line. No pattern before them takes a block's header for a value
(`_BLOCK_AHEAD`), which left the block's lines in the clear. Each
candidate starts at a line's start, an escape or a quote (a flow
collection's at its `{`, `[` or `,`, or an earlier marker's last `*`, one
set of characters the regex engine scans for fast) and reads its key,
one line break, the blank and comment lines before its value and its
value's lines once (a block's once more, to tell whether each is a
placeholder), and a candidate that fails does so at the start of its value
or at the first line that is none of its, so the pass is linear.

### Redaction in rounds, and the patterns' hints

A value's extent depends on the text around it: a quoted value spans lines
to the first quote of its kind that closes it, and a YAML plain scalar is a
line with no mapping's `: ` in it. A later pattern can redact a part of
that text (a quote inside another value, a block that held one, a line a
YAML value is on), and a second redaction then read differently what the
first one left and changed it again, so `credential_classes` found a
credential in text `redact` had already redacted; a generator of text where
the patterns meet found such cases until the third review of #285, rare,
and each a different meeting. `redact` now applies its patterns in rounds:
a round that changed the text is followed by another, until one changes
nothing (`_MAX_ROUNDS`, four at most), and `credential_classes` runs the
same rounds, so what `redact` returns is what redacting it again returns. A
text with nothing to redact takes one round, as before, and one with a
credential takes a second, which changes nothing unless such a meeting
occurred; no text met in testing needed more than two. The rounds compound
no growth: each replaces a run of text that is no marker, beside text it
keeps, as one round does, so `MAX_GROWTH_FACTOR` holds over them. The
generator (`test_redact_is_stable_where_the_patterns_meet`) and the cases
it found are pinned, and the OAuth and prefixed-name values agree within
one round already ([above](#the-remaining-credential-shapes-285)).

So that a second round costs no more than the patterns used to, a pattern
may name a hint (`_Pattern.hint`, and `name_hint` for a second one): what
every match of its passes holds, a token shape's literal prefix
(`-----BEGIN `, `eyJ`, `AIza`, `glpat-`, `gh[pousr]_`, `sk-`, `A[KS]IA`,
...), a URL's `://`, `Authorization`, a prefixed name's suffix
(`_token`, in any case), a well-known name's last `_` with the character
before it and the segment after it (derived from the names), a YAML
key's colon before what ends its line, and a line break, as written or
written out, for a plain scalar going on below a flow key's line. A text without it has no match, and
the pattern's passes are skipped; a search led by a literal runs far faster
than a pattern that starts with a lookbehind, so a log with no credential
is redacted in a fraction of the time it took, and one with a credential,
two rounds and all, in about the time one round took. Every hint is checked against the
test corpus, its JSON and its escaped repeats, and against every growth
unit: no pass of a pattern finds anything where its hint is not found. A
new pattern needs no hint; one it names must be what each of its matches
holds.

## Escaped text: redact the structure first (#281)

Until #281 the CLI printed a step's agent result as
`redact(json.dumps(result))`, a line that reaches the terminal and, through
`worker.py`, a scheduler worker's persisted log. JSON writes a quote as
`\"`, so a quoted assignment in a result string (`GH_TOKEN="..."`) read
`GH_TOKEN=\"...\"` there, and the value pattern stopped at the backslash
and printed the secret after it in the clear. (A token after a written-out
`\n`, `\t` or `\uXXXX` was recognised since
[#284](#where-a-recognised-credential-starts-284).) Two rules now hold:

- **A structure the controller serializes is redacted first, string by
  string** (`redact_obj`,
  [below](#what-is-redacted-before-it-is-persisted)), never as its
  serialized text: the step-outcome line (`print_step_outcome`, for `run`,
  `step`, `resume` and the worker), the replan `escalation` in `status`'s
  journal lines, in a dry run's `replan policy:` note and in the
  `ESCALATION_REASON` prompt variable (the prompt is logged as `prompt.md`
  and previewed by a dry run as text). A dry run's notes, which quote state
  such as a recorded refusal, are redacted like its variables.
- **Text no one can redact first is matched as escaped.** An agent's stdout
  and stderr are captured whole into `stdout.log` and `stderr.log`, its
  result among them as the JSON it printed, and an error can quote an
  excerpt. The assignment patterns (`env-assignment`, `secret-assignment`,
  `oauth-assignment`, `oauth-bare-assignment`) take their optional quote
  behind up to seven backslashes too (`\"`, `\'` as a Python repr writes it,
  `\\\"` for JSON inside a JSON string, three levels deep at most, the
  bound `oauth-field` has), and keep a backslash that starts the closing
  escaped quote out of the value: `GH_TOKEN=\"***REDACTED***\"`, so JSON
  redacted as text still parses. A quote behind eight backslashes or more
  is not recognised. An `Authorization` header whose value is quoted,
  escaped or not, was not recognised either until #393
  ([below](#quoted-keys-and-a-code-fence-after-a-separator-393)).

Every place that serializes and redacts was checked for #281: the JSON
documents `status --json`, `doctor --json` and `epic status --json` print
are redacted before they are serialized, as are the run log's JSON files
and journal and the scheduler's claim `detail`; the engine redacts agent
text field by field as it writes it to state, so the prompt variables
serialized from state (the FIX resolutions, operator and acceptance
outcomes) carry redacted strings; the other `json.dumps` calls build
digests, canonical forms, internal files or a process's input, none of
them passed to `redact`.

`MAX_GROWTH_FACTOR` is unchanged: an escaped quote, like a plain one, is
kept and no part of the replaced run, and a value is still at least one
character (`HF_TOKEN=\"x\"`, 14 to 27, is pinned with the other growth
units). The backslash runs are bounded, so the patterns stay linear, and
`redact` stays idempotent.

## Quoted keys, and a code fence after a separator (#393)

A correction's prompt quotes the end of the refused reply, so whatever
`redact` misses in a reply goes to the next agent and into its
`prompt.md`, as it already went into the refused launch's `stdout.log`.
That quote exposed two gaps in the named patterns. Both are closed in
`redaction.py` itself, so every sink and `credential_classes` get them.

- **A credential's name as a quoted key.** JSON, a Python mapping's repr,
  a TOML quoted key and YAML write `"GITHUB_TOKEN": "..."`. The key's
  closing quote stood between the name and the separator, so
  `env-assignment` never matched, and a value of no known token shape was
  kept in the clear. `env-assignment`, `secret-assignment` (`"api_key":`,
  `"password":`, `"token":`) and `authorization-header`
  (`"Authorization": "Bearer ..."`, a headers mapping) now take an optional
  quote after the name, escaped as `_QUOTE` takes one, and keep it with
  the separator. The header's value keeps an escaped closing quote whole,
  as an assignment's does. The OAuth names already had `oauth-field` for
  this shape. Unchanged: an empty value is left. A prefixed name
  (`"MY_GITHUB_TOKEN"`, `"client_secret"`) is recognised under a quoted key
  too since #285, and `redact_obj`, which redacts a key and its value
  apart, recognises a well-known or prefixed credential name as a key, as
  it did the OAuth keys
  ([above](#the-remaining-credential-shapes-285)).
- **A code fence on a later line is never a value.** The space after an
  assignment's separator may cross a line break, so that a YAML value on
  the next line is still caught. A prompt quotes untrusted text in a fence,
  and a quoted reply that ended in an empty `GITHUB_TOKEN=` (or
  `Authorization: Bearer`, `id_token:`) had its closing fence taken for the
  value when the run log redacted the prompt again. `prompt.md` then
  showed the quote unterminated, and so differed from the prompt that was
  sent. The space after a separator (`_SPACE_TO_VALUE`) now never runs onto
  a line that starts with three backticks or tildes, indented or not. This
  gives up no protection: the fence line was all that was ever replaced,
  and a secret written in a code block under the name was left as before.
  A value on the same line is a value whatever it starts with, and a value
  on a later line that starts no fence is still redacted.

Both keep `MAX_GROWTH_FACTOR`: a quoted key only lengthens the matched
text (`"HF_TOKEN":"x"`, 14 to 27, is pinned with the other growth units).
Both stay linear: the key's quote is bounded like a value's, and each line
break after a separator is tried once.

## Launch environment and credential checks (#165)

What #165 adds reports names, never values:

- a refused allow-list entry (`refuse_forbidden_env`, at config load and in
  an adapter's `environment_names`) names the entry and the rule, and the
  config value it came from is a name, not a secret;
- the launch preflight names the scope and the key's shape
  (`remote.<name>.url`, `credential.<url>.helper`, `~/.netrc`), never the
  value, the URL or the netrc line, so a token in a remote URL never reaches
  the error, `BLOCKED` reason or log;
- `doctor`'s "launch environment" row lists variable names only (allowed,
  set by the controller, withheld from the current shell); the
  "GitHub remote" row redacts the URL it quotes;
- each invocation's `request.json` records the names the controller set
  (`env_set`), never their values; the placeholder token and the temporary
  directory paths are not logged;
- the read context (`<state dir>/read-context/`) is project data the
  controller wrote for an agent: every text in it went through `redact`
  before its bounds were applied, and an unreadable item names only the
  error's type, never the `gh` output.

## SDK providers (#149)

The SDK providers' policy ([SDK policy](../sdk-policy.md)) adds rules of
its own, all enforced in `src/autoforge/sdk_policy.py`:

- **No other provider's credential reaches an SDK launch.** Its allow-list
  is expanded to exact names against the *names* in the controller's
  environment, and every other provider's variables and every
  credential-shaped name (`*_API_KEY`, `*_TOKEN`, `*_SECRET`,
  `*_PASSWORD` and the like) are removed. Only then is the provider's own
  credential added: `ANTHROPIC_API_KEY`, or the variable an `opencode-sdk`
  profile names in `options.credential_env`. The controller's own
  environment is never changed.
- **No login is reused.** `claude-sdk` has no login-file or keyring item,
  and refuses `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_AUTH_TOKEN` by name.
  `opencode-sdk` reads no stored sign-in.
- **The OpenCode server password** is generated per launch and given to
  the server's environment alone. It never appears in an argv, a record, a
  log or state, and its name is in the redaction list above.
- **Names, never values.** Every policy message names a variable, a field
  or a tool and never quotes a value: config validation, the
  authentication checks, the `system/init` check and the
  interaction-data refusals.
- **Interaction data is bounded before it is shown or persisted.** The
  run log's `interaction` record (`execution.json`, #134) names each
  request, its offers, the directive and the kinds of decision delivered.
  It never carries an answer's text, guidance or a modified input. It
  keeps an entry for at most `max_interactions` requests and the first one
  past them; a provider that keeps asking is failed and only counted.
  - A value a reply depends on (a tool input, an option, an answer, a
    modified input) is *refused* rather than redacted or clipped when it
    carries a credential shape, a control or escape character, or exceeds
    its bound. A redacted or clipped value would no longer be the one
    approved.
  - What an operator is shown goes through `progress.clean`, which strips
    escape sequences and control characters, redacts, and clips, in that
    order. It shows only allow-listed input keys, never a file's content
    or a diff.
- **The session roots** (`<sandbox dir>/sdk/<provider>/`) hold the
  provider's transcripts. They are controller state, never a working tree
  and never published. AutoForge copies no credential into them.

## The outer sandbox and credentials (ADR 0005 D7.4)

Under `sandbox.mode: required` (#168) a launch starts from an empty root
and a private, empty home, so the only credential material it can see is
what its provider adapter declares (D7.1;
[Configuration](../configuration.md#provider-credentials-under-required)):

- variables the allow-list passes anyway (`ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, an OpenCode
  `*_API_KEY`, Pi's `OPENAI_API_KEY` when `require_oauth: false` and
  `execution.env_allowlist_extra` forwards it);
- one login file, read-only at that path, wherever it resolves: Claude
  Code's `~/.claude/.credentials.json` when no `CLAUDE_CONFIG_DIR` is set;
- one dedicated directory, writable where it resolves, the variable naming
  it there: `CLAUDE_CONFIG_DIR`, the directory of an absolute
  `OPENCODE_DB`, `PI_CODING_AGENT_DIR`.

Every tool in the same sandbox (the agent's shell commands, the test suite
it runs) can read that credential, and that exposure is accepted: it is a
model credential, never a GitHub mutation credential, and the sandbox
exists to keep everything else out of reach. A repository-defined command
gets no declared item, only the variables the allow-list passes. A
writable credential directory persists across launches and is shared by
every launch that projects it, issues and roles alike, so it is dedicated
to AutoForge and never the provider's own configuration root (D7.2).

AutoForge never reads, copies or logs a credential's contents: the policy
compiler looks at a declared path with `lstat` and `realpath` only, and a
refusal names the path and the rule, redacted (`policy_refusal`). The
environment rewrite (D8.2) unsets a known path-valued variable whose value
names nothing visible inside, trims `PATH`, and spells a kept value where
it is inside (computed from the description and the paths' links, never
from a file's contents); the launch's facts list the
unset variables by name (`unset_env` in `execution.json`'s `sandbox`),
never a value, and `request.json` adds only the configured mode
(`sandbox_mode`).

## What is redacted before it is persisted

`state.json` is stored in the clear, so the engine redacts at the writer,
once, wherever a persisted string can quote external text; a call site never
has to remember to. The writers that redact before persistence are:

- `ControllerEngine._block`: every controller-composed `block_reason`, and
  the `StepOutcome.message` it returns. Its call sites embed `GitHubError`
  messages (`gh` stderr), PR bodies and command output; they pass the raw
  text and `_block` redacts it at the sink.
- the two agent-message writers (`status: failure` / `blocked` in the
  REMOTE and LOCAL step paths): `block_reason` from the agent's `message`.
  These three writers also bound the reason to `state.MAX_BLOCK_REASON_CHARS`
  (`state.bound_block_reason`, #88), *after* redacting it: the bound clips
  by character count and knows nothing about secrets, so a secret cut by
  the clip would no longer match its pattern if the order were reversed.
  `tests/test_engine.py` pins the order with a secret on each clip boundary.
- `_record_verification_failure`: each `verification_failures` entry, which
  quotes a `VerificationError` (a validation command's output, a PR body,
  `gh` output). `REPLAN_REEXECUTE` renders that list into its prompt, so the
  prompt inherits the redaction.
- `_reject_next_issue`: each `next_issue_rejections` entry (rendered into
  the correction prompt and, once the bound is hit, into `block_reason`).
- the REVIEW and FIX appliers (REMOTE and LOCAL): each agent finding and
  resolution crosses `redact_dict` before it reaches `open_findings`,
  `review_history` or `last_fix_resolutions`.
- the LOCAL validation and pre-merge verification paths: the output tail of
  a command they quote.
- the validation-record writers (`evidence.py`, #172): a record's or a
  marker's argv (element by element, `redact_argv`) and each stream's
  output tail. The tail is redacted over the whole captured stream before
  it is cut to 2,000 characters, so a secret straddling the cut is not
  half-kept, then control characters are escaped; the enforcement facts
  are redacted and clipped. A definition's digest is over the argv as
  configured, a one-way SHA-256 that stores no argv, which is also why a
  secret belongs in the allow-listed environment and never in a validation
  argv ([Configuration](../configuration.md#candidate-validation)).

`GitHubClient._run_gh` additionally redacts the `gh` stderr tail where it
builds a `GitHubError` / `GitHubNotFoundError` / `GitHubUnavailableError`
message, because `gh` can echo the request it made (an `Authorization`
header, a credentialed URL). The failure is classified (transient, not
found, access denied) on the raw tail and only quoted redacted, so the
message is safe in whatever it reaches (`block_reason`,
`verification_failures`, the run log's `error.txt`) without each consumer
redacting it again.

That list is the engine's boundary; it is defense in depth, not a promise
about the file. `state.json` can also be hand-edited or written by an older
controller, a journal defect quotes the value it could not read, and a
`BLOCKED` reason written before this boundary existed can quote `gh`
output raw. So anything the CLI prints *from* state (`status`,
`status --json`, the step outcome line, the terminal line of `run` / `step` /
`resume`, the existing-run refusal, the `READY_FOR_MERGE` banner) crosses
`redact` / `redact_dict` on the way out, in one pass over the whole rendered
text or document rather than per field, so a new state field is covered by
construction. That includes `run_id`: its validation checks that it is a
safe path component, not that it is free of secret shapes, so a line that
names the run is redacted assembled, never with the id interpolated outside
the pass. The file on disk is never rewritten by that pass.

An agent failure that leaves the state unchanged (#189,
`ControllerEngine._state_unchanged_text`) adds the agent worktree with its
uncommitted count, the step's log directory and a `resume` command that
repeats the `--config` / `--state-dir` the process was started with: paths,
a count and those flags, never a configuration value. The engine redacts
the assembled text where it composes it, and the CLI again where it prints
it. `tests/test_engine.py` puts token-shaped values in the worktree path,
a flag and a profile's model and checks that none reaches the text. A block
reason for a checkout that moved
(`ControllerEngine._moved_checkout_recovery`) adds the same kind of values
only: the checkout path, the feature specification's path and commands
with those flags. `_block` redacts it like every block reason.

A write phase's prompt describes what the agent worktree holds uncommitted
(#187): its branch, `HEAD` and each entry's status code and path, escaped
and fenced as untrusted data, never a file's content. The prompt is
redacted where it is logged (`prompt.md`), and the note the run log keeps
of that read (`metadata.worktree_state` in `request.json`), which quotes a
failed read's last git output line, crosses `redact_dict` with the rest of
the metadata.

A correction's prompt quotes the end of the refused launch's final reply
(#393). `ReplyExcerpt.of` redacts the whole reply and only then keeps its
last `MAX_CORRECTION_REPLY_CHARS` characters, the order the block-reason
writers follow, so a secret the bound would cut in half is replaced before
the cut. The quote is redacted before the agent sees it, not only where
`prompt.md` logs it, and state never holds it; the run log records its
sizes alone (`metadata.correction_reply` in `request.json`).
`tests/test_engine.py` puts a secret, and a credential under a quoted key,
across the bound. `prompt.md` redacts the prompt again and shows the quote
as sent, closing fence included
([above](#quoted-keys-and-a-code-fence-after-a-separator-393)). The one
difference left errs toward redacting: when the cut puts a token shape at
the start of the quote that the character before it hid (`xghp_...` cut
after the `x`), `prompt.md` redacts it and the prompt does not. The prompt
then holds only what redacting the whole reply left.

`redact_obj` / `redact_dict` redact mapping keys as well as values, and a
mapping never loses an entry to that: two distinct keys that redact to the
same text (`GITHUB_TOKEN=a`, `GITHUB_TOKEN=b`) are kept apart with a `#2`,
`#3`, ... suffix in insertion order rather than the later value overwriting
the earlier one, so a redacted rendering of a free-form mapping (a journal's
`escalation`, log metadata) is still complete. The next suffix is remembered
per colliding text, so a mapping whose keys all collide is redacted in time
linear in its size; a change to the allocator must keep that, and
`tests/test_redaction.py` pins it against a large colliding mapping.

A structure is redacted string by string, never as its serialized text
(#206). Run over JSON, a pattern can consume the syntax around a value (an
empty `GITHUB_TOKEN=` takes the closing quote), so parsing the redacted
text back can fail. (A secret right after an escape there, `\n` or
`\uXXXX`, follows a letter or a digit; it was missed until #284 and is
recognised now, [above](#where-a-recognised-credential-starts-284). A
quoted value behind JSON's `\"` lost the backslash and kept the secret
until #281, [above](#escaped-text-redact-the-structure-first-281).) The run
log's `control-result.json` and the journal's `parsed_result` therefore
cross `redact_dict` like the metadata, and the CLI's step outcome
`redact_obj`. The shapes that span a key and its value, a quoted OAuth
credential field (`"refresh": "..."`) and a well-known or prefixed
credential name (`"GITHUB_TOKEN": "..."`, `"client_secret": "..."`, #285),
are recognised by the key: `redact_obj` replaces a non-empty string under
a key that names one, whole and in any case, whole. No parser-bounded
field (a finding, a resolution) has such a key, so the bounds that depend
on `MAX_GROWTH_FACTOR` are unaffected; `tests/test_redaction.py` pins that,
and checks that the structure covers every `oauth-field` case that is a
JSON document.

Live progress (#192) is built to be safe where it is made, not where it is
printed. A provider adapter maps its protocol to a `ProgressEvent`
(`progress.py`) whose only strings are a tool name and one allow-listed
tool input (a path, a search pattern, a shell command's *description*;
never the command, a tool result, thinking or assistant text). Each string
is stripped of ANSI escapes and of control and format characters first,
so a secret split by an escape is whole again, then redacted with its line
breaks, made one line, redacted again, then clipped, so a clip cannot cut
a secret below its pattern. Until the fifth review of #285 its line breaks
became spaces before it was redacted, so a private key wrapped in short
lines became words of prose, and a YAML value below its key or in a block
lost the lines it is told by, and a progress line showed them; a raw value past
`MAX_RAW_CHARS` is not shown at all. The reporter redacts each assembled
line again and bounds it before it reaches stderr or the step's
`progress.log`, which is bounded too (`runlog.MAX_PROGRESS_LOG_BYTES`).
A provider's own summary of the run (`provider_summary`) holds bounded
scalars only: Claude's cost is integer micro-USD, never a float, and its
nested `usage` is not copied. `tests/test_progress.py` seeds a token and
escape sequences into tool inputs and checks that neither reaches the
terminal, `progress.log`, `execution.json` or state.

The loop detector (#194, `loop_detect.py`) compares tool inputs, results and
output lines only as digests held in memory for one invocation; it
never stores, logs or returns them. What it reports is the signal, a cycle
period, a count, tool names, a stream name and times: the `loop` record in
`execution.json`, the warning lines, the error text and the `loop_*`
calibration counts in `provider_summary`. Those tool names are cleaned and
redacted like a progress event's. `tests/test_loop_detect.py` seeds a token
into a looping agent's commands, results and output lines and checks that
neither it, the raw strings nor the digests reach any line or file the run
writes.

A sandboxed launch's facts (`sandbox` in `execution.json` and
`events.jsonl`, #167, ADR 0005 D10.4; since #168 also for a launch the
policy refused) carry names, paths, sizes, the backend
version and, for a setup failure, the last lines of `bwrap`'s own message,
redacted and bounded. They never carry an environment value. The
environment the sandboxed command gets is handed to `bwrap` through a
memory file (`--args`), never on a command line any process could read
from `/proc/<pid>/cmdline`. `tests/test_sandbox.py` and
`tests/test_sandbox_bwrap.py` check that a value passed to a sandboxed
command appears on no command line, and `tests/test_runlog.py` that a
token in a setup failure's reason reaches no file.

`stdout.log` and `stderr.log` hold what the executor captured, which is
bounded (`executor.DEFAULT_MAX_OUTPUT_BYTES` per stream): past the bound the
file is the head of the stream, a `[autoforge: N bytes of stdout omitted; ...]`
marker and the tail, and `execution.json` records `stdout_truncated` /
`stderr_truncated`. Redaction runs over that bounded text, so the redaction
pass, like the capture, costs at most the bound.
