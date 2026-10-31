# Clean-room first run

A procedure for checking that the published documentation alone gets a
newcomer from nothing to a first AutoForge run: install, configure,
`doctor`, a dry run and a real run on a GitHub repository the tester
controls ([The target repository](#the-target-repository)). It is the
check behind two gates of the Public Alpha
([#255](https://github.com/iceboundrock/AutoForge/issues/255)): Gate 1
(fresh installation) and Gate 2 (first successful run). It is repeated at
each release candidate, and Gate 2 needs a run by someone other than the
maintainer.

`make dist-check` runs the half that needs no credential on every commit
([Development](development.md#the-built-distributions), step 7): installing
the wheel with `uv tool`, `--version`, `--help`, `doctor`'s rows that need
no sign-in, and a dry run with each starter configuration. This procedure
covers the rest: a person, real logins, real model calls, and the
documentation as a reader meets it.

## The rules

- **Only the published documents.** Follow [Getting started](getting-started.md)
  and what it links to, at the commit or version under test (the
  [revision under test](#the-revision-under-test)): read them at
  `https://github.com/iceboundrock/AutoForge/blob/<ref>/docs/getting-started.md`,
  never `main`'s copy. No source code, no issue history, no help from the
  maintainer. A question you had to ask, or an answer you found anywhere
  else, is a friction point.
- **Write down every friction point as it happens**, in the
  [format below](#the-friction-log), including the ones you worked around.
- **Do not fix anything on the way.** Record it, work around it if you can,
  and say how.

## The environment

Start from one of:

- a new virtual machine or cloud instance running Linux on x86_64;
- a new container from a Linux distribution image (the outer sandbox rows of
  `doctor` may differ inside a container: record them as they are);
- a new user account on a Linux host.

Before you start, it must hold none of these, and you record that it
did not:

- an `autoforge` command (`command -v autoforge` prints nothing);
- `uv`'s cache, tools or Pythons of an earlier install (`~/.cache/uv`,
  `~/.local/share/uv`);
- an AutoForge configuration, state directory or clone (`autoforge.yaml`,
  `.autoforge/`, `.git/autoforge/`).

The prerequisites (`uv`, Python, git, `gh`, Claude Code, OpenCode) are part
of the run: install them as the guide's
[prerequisites](getting-started.md#1-prerequisites) say, from each tool's
own instructions, and sign in to each. Say in the report which ones were
already there.

## The target repository

The rules above make the execution environment fresh; the repository the
run works on need not be new. It is one of:

- **a new private repository,** made as the guide's
  [step 3](getting-started.md#3-choose-and-prepare-a-repository) shows: the
  recommended choice, and the one the guide's example follows;
- **an existing repository,** private or public, that you control or are
  authorized to change.

With an existing repository, write in the report, before the real run:

- that you are authorized to change it, and that you accept the run's
  scope there: the issue you name, its branch `autoforge/<issue>`, its
  pull request and the reviews on it, and any follow-up issues the run
  opens;
- on a public repository, that you accept its further risks: anyone who
  can comment on the issue or the pull request can inject instructions
  into agents that run as you, with your files and model keys within
  reach; the code and the text go to the model vendors and, through the
  pull request, to everyone; and an agent may change more than the issue
  asks, which the repository's CI then runs with its workflows' secrets.
  The guide's [step 3](getting-started.md#3-choose-and-prepare-a-repository)
  says how to keep each small.

Nothing else changes with the choice. The environment is still fresh; the
installation, the documents and the starter still come from one
[revision under test](#the-revision-under-test); the merge gate stays
closed; and AutoForge's authority boundaries, credential protections and
merge safeguards are the same in every repository. At a release candidate,
Gate 2 still needs a run by someone other than the maintainer, whichever
repository it uses.

## The revision under test

Everything of AutoForge the run uses comes from one revision, its `ref`:
the command, the documents and the starter configuration. A starter of
another revision may name a key or a model the installed controller does
not know, and the guide of another revision describes another program.

| Run | `ref` | Install (step 2) | Starter (step 4) |
|---|---|---|---|
| on a pull request | its head commit, all 40 hexadecimal digits (the copy button of the last commit in the pull request's **Commits** tab gives them) | `uv tool install --python 3.12 "git+https://github.com/iceboundrock/AutoForge@$ref"` | the guide's `curl` with that `ref`, the starter at the head commit |
| at a release candidate | `v<version>`, the candidate's tag ([Releasing](releasing.md#version-scheme)), for example `v0.1.0a1` | the guide's PyPI command with the exact version, `autoforge-cli==<version>` in place of `autoforge-cli` | the guide's `curl` with that tag |

The head commit of a pull request whose branch is in this repository, as
AutoForge's own are, is served by `raw.githubusercontent.com` before the
pull request is merged, so the starter is read from the commit under test.
`main`'s copy is never the right one: before that pull request is merged
`main` may hold no starter at all, and after it a newer one. Record `ref`
in step 0, and check in step 4 that the starter's URL names it.
`make dist-check` copies each starter from its own checkout and never
downloads one, so the download is checked only here.

## The steps

Number the steps in your notes as below, and note the time at each.

0. **Record the environment:** the date (UTC), `cat /etc/os-release`,
   `uname -srm`, and the `ref` under test
   ([above](#the-revision-under-test)).
1. **Prerequisites** ([section 1](getting-started.md#1-prerequisites)):
   install and sign in to each tool.
2. **Install AutoForge** ([section 2](getting-started.md#2-install-autoforge))
   from `ref`, by the method [above](#the-revision-under-test) for this
   kind of run, and keep the exact command you ran and `uv`'s output.
   Run `autoforge --version` and `autoforge --help` from a directory that
   is not a repository; on a pull request the version is `main`'s `.devM`
   one, at a release candidate the candidate's. The version alone does
   not say which commit is installed; record that, with the Python
   AutoForge runs on and `uv`'s own version:

   ```bash
   uv --version
   uv tool list --show-version-specifiers --show-python
   ```

   The `autoforge-cli` line names what was installed and the Python it
   runs on. On a pull request it reads, for example,
   `autoforge-cli v0.1.0a1.dev0 [required:  git+https://github.com/iceboundrock/AutoForge@<ref>] [CPython 3.12.14]`,
   with `ref`'s 40 digits after the `@`; at a release candidate,
   `[required: ==<version>]` with the candidate's version. Anything else
   means the run is not of the revision under test: stop, and record it
   as a friction point.
3. **Choose and prepare the repository**
   ([section 3](getting-started.md#3-choose-and-prepare-a-repository)): a
   new private one, or an existing one with what
   [The target repository](#the-target-repository) asks written in the
   report. Use the guide's EPIC and issue, or your own and their text in
   the report.
4. **Write the configuration**
   ([section 4](getting-started.md#4-write-a-configuration)), with the
   starter downloaded at `ref`: name the starter, the URL you downloaded it
   from, and every change you made to it.
5. **Run `doctor`**
   ([section 5](getting-started.md#5-check-the-environment-doctor)),
   signed in, before the dry run and the real run: keep its output, and
   the versions file the guide writes. Every row that is not `OK` must be
   one the guide told you to expect; any other is a friction point.
6. **Dry run**, then **the real run**, then **`status`**
   ([section 6](getting-started.md#6-preview-run-and-read-where-it-stopped)),
   until `READY_FOR_MERGE` or `BLOCKED`. Note how long the run took and how
   many review rounds it made. If it is `BLOCKED`, follow the guide to
   leave it, and record whether the guide explained the reason.
7. **Finish as the guide says:** merge or close the pull request yourself.
   Then `uv tool uninstall autoforge-cli`. A repository made for the run
   can be deleted, unless you keep it for the record. An existing
   repository stays: close any follow-up issue the run opened that you do
   not want, and delete the branch `autoforge/<issue>` if you closed its
   pull request without merging.

## What to capture

Keep these, **redacted** (below):

- the environment of step 0, the installation record of step 2 (the
  exact install command, `uv`'s output, `uv --version` and the
  `uv tool list` line), and the versions file of step 5;
- the configuration of step 4: the starter's name, the URL it was
  downloaded from, and every change made to it (a `diff` against a fresh
  download shows them);
- `doctor`'s text output;
- the first lines of the dry run, to the `Command:` line;
- the run's terminal output, from the command to the `READY_FOR_MERGE`
  banner or the `BLOCKED` line;
- `autoforge status` at the end;
- the URLs of the repository, the EPIC, the issue and the pull request, if
  the repository is public or you make it so; otherwise say it was private;
- for an existing repository, what [The target repository](#the-target-repository)
  asks you to write;
- the friction log.

**Redact before you post.** AutoForge redacts what it prints and logs, but
only to a baseline ([Redaction is a baseline](security.md#redaction-is-a-baseline)),
and the tools you installed print their own output. Before posting anything:

- remove every token, key, password and cookie, whether or not it looks
  like one, and any `Authorization` header;
- replace your home directory with `~` and your user name with `<user>`;
- replace an e-mail address or a private repository's name you do not want
  public with a placeholder;
- never post a file from `.autoforge/` whole (the run logs hold every prompt
  and every agent's output), nor the files the CLIs keep your logins in
  (`gh`'s `hosts.yml`, `~/.claude`, OpenCode's data directory), nor your
  environment (`env`).

## The friction log

One header, then one row per friction point, in the order they happened:

```markdown
**Clean-room run** · <date, UTC> · <who> · <OS and version, architecture> ·
AutoForge <version and ref> · Python <version> · uv <version> · git <version> ·
gh <version> · Claude Code <version> · OpenCode <version, or "not used"> ·
starter <file> · repository: new private | existing private | existing
public · result: READY_FOR_MERGE | BLOCKED (<reason>) after
<n> review round(s) · time: <prerequisites> + <install to doctor> + <run>

| # | Step | What I did | What I expected | What happened (exact message) | Workaround | Cost | Severity |
|---|---|---|---|---|---|---|---|
| 1 | 2 | ... | ... | ... | ... | 10 min | major |
```

- **Step** is the procedure's step number above.
- **What happened** quotes the message exactly, redacted.
- **Cost** is the time it took, and money if a model call was wasted.
- **Severity** is one of: **blocker** (could not go on without help from
  outside the documents), **major** (went on after a workaround found
  outside the documents), **minor** (confusing or slow, but the documents
  got you through).

Post the log, with what you captured, as a comment on the issue that asked
for the run. Each friction point is then either fixed or documented, and
the issue says which, by row number: fixed, with the pull request or
commit, or documented, with the page and section.
