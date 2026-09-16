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
Authorization: Bearer ...
```

Redaction is defense in depth; do not claim it detects every possible secret.

Redaction can lengthen a text (a short secret becomes the fixed marker), and
`redact` guarantees it never returns more than `MAX_GROWTH_FACTOR` times its
input. Persisted bounds applied *after* redaction (the review-history
resolution bound in `loop_guard`) depend on that factor, so a new pattern
must keep it, or raise it together with those bounds and their tests.

Do not log the entire process environment.
