# Security policy

AutoForge drives coding agents that run as you, with your model
credentials, inside a repository you own, while it holds a GitHub
credential that those agents must never see. Before you run it, read
[Security boundaries](docs/security.md): what AutoForge protects, what it
does not, the GitHub token permissions it needs, and the settings we
recommend for a repository you care about.

## Supported versions

AutoForge is pre-release software (the pre-Alpha `0.x` line).
There are no release branches and no maintained older versions: security
fixes land on `main`, and only the latest commit of `main` is supported. Update your checkout
(`git pull`, then `uv sync --locked`), or reinstall the command at that
commit ([Installation](README.md#installation)), to receive a fix.

| Version | Supported |
|---|---|
| `main` (pre-Alpha, `0.x`) | yes, latest commit only |
| anything older | no |

AutoForge is installed from this repository: from a commit, from a wheel
built from one, or from a development clone
([Installation](README.md#installation)). Its distribution is named
`autoforge-cli`, which is not yet published on PyPI
([#295](https://github.com/iceboundrock/AutoForge/issues/295)). The
`autoforge` distribution on PyPI is an unrelated project, so
`pip install autoforge` or `uv tool install autoforge` installs someone
else's code.

## Reporting a vulnerability

Report a vulnerability privately, through GitHub's private vulnerability
reporting: open the repository's **Security** tab and choose **Report a
vulnerability**
(<https://github.com/iceboundrock/AutoForge/security/advisories/new>).

Do not open a public issue, pull request or discussion for a
vulnerability, and do not put exploit details in a comment.

Include, as far as you can:

- the commit of `main` you tested;
- the configuration involved (`sandbox.mode`, the providers and profiles,
  `safety.allow_merge`), with every credential removed;
- what an attacker controls (an issue or PR comment, repository content,
  configuration, the local machine) and what they gain;
- the smallest steps that reproduce it.

Never send a real credential, token or private key, including one you
found in a log AutoForge wrote: describe where it appeared instead. Use
dummy values in any proof of concept.

## What to expect

- An acknowledgement within 7 days.
- An assessment within 30 days: whether we accept it, its severity, and
  the plan.
- A fix on `main` and a GitHub security advisory for an accepted
  vulnerability, crediting you unless you ask us not to. We ask you not to
  disclose it publicly until the advisory is published or 90 days have
  passed, whichever comes first; we will agree a different date with you if
  a fix needs it.

AutoForge is an alpha maintained on a best-effort basis: these are targets,
not a service level agreement.

## Scope

In scope: a way, through anything AutoForge controls (the environment it
gives a launch, its configuration, the files it writes, the commands it
runs, the prompts it renders, the text it publishes), by which an agent, a
repository command or untrusted project data can

- obtain a GitHub or git credential, or make a GitHub write;
- merge a pull request, or write to the default branch, without the merge
  gate (`safety.allow_merge: true` and `--allow-merge`);
- get a secret into a log, the terminal, the state directory or GitHub in a
  documented configuration;
- reach what `sandbox.mode: required` says an enforced launch cannot reach;
- make the controller act on an agent's claim it did not verify, or repeat
  a GitHub write on resume.

Out of scope, because AutoForge documents them as outside its guarantees
([Security boundaries: what AutoForge does not protect](docs/security.md#what-autoforge-does-not-protect)),
unless the way runs through something AutoForge controls, which keeps it in
scope:

- what an agent can do as your user under the default `sandbox.mode:
  disabled`: read or change any file your user can (your `gh` login, SSH
  keys, git credentials, AutoForge's own configuration and state), use the
  network, and use the model credential it is given;
- prompt injection by anyone who can write the issues, comments or code an
  agent reads, on a public repository included, beyond the guarantees
  above;
- the network, and the model credential, that an agent keeps inside the
  outer sandbox;
- a secret the redaction baseline does not recognise (it is a baseline,
  with no claim of completeness), unless it is a credential class a
  supported provider uses;
- kernel, `bwrap`, Claude Code, OpenCode, Pi, `gh` or git vulnerabilities
  themselves (report those to their projects), a malicious operator, and
  the code of the pull requests AutoForge reviews.
