# Releasing

How a version of AutoForge is numbered, built, tagged, published and
recorded. A release is a maintainer action from start to finish: the
maintainer of `iceboundrock/AutoForge` cuts and publishes it, and nothing
releases on its own. No workflow publishes on a push to a branch or on a
pull request, no agent tags or uploads, and AutoForge's controller has no
release step. The one workflow that publishes, `pypi-publish`, runs only
for a version tag the maintainer pushed and uploads only after the
maintainer approved that run ([Publishing to PyPI](#publishing-to-pypi)).

Cutting the Public Alpha release is the Go/No-Go step of the launch EPIC
([#255](https://github.com/iceboundrock/AutoForge/issues/255)): its version
bump, its `CHANGELOG.md` section and the refresh of the
[compatibility matrix](compatibility.md) are the EPIC's Stage 5
release-prep pull request, which follows this procedure.

**One exception, decided before the Go**
([#256 D2](https://github.com/iceboundrock/AutoForge/issues/256#issuecomment-6373954762)):
the early `autoforge-cli` prerelease of
[#295](https://github.com/iceboundrock/AutoForge/issues/295) may be
published before #255's Go/No-Go, to register the distribution name on
PyPI. It is a pre-Alpha snapshot, not validated for unattended production
use, and it is **not** the Go: the Public Alpha's release candidate,
security, demonstration and Go/No-Go gates still apply to the Public Alpha
release.

## Names

- The distribution is **`autoforge-cli`** (on PyPI, in wheel and sdist
  file names as `autoforge_cli`). The import package and the command it
  installs are both **`autoforge`**. `autoforge` on PyPI is an unrelated
  project: never publish, document or recommend `pip install autoforge`.
- A release's tag is **`v<version>`**, for example `v0.1.0a0`: the letter
  `v` and the exact version, nothing else.
- `README.md` is also the project description PyPI shows (`readme` in
  `pyproject.toml`), and PyPI resolves no relative link. Its Prerequisites
  and Installation sections therefore link absolutely, to `main`; the
  relative links of its other sections are broken on PyPI, a known
  limitation. Its PyPI method is conditional on a published version and
  points at the releases page for which ones exist; its statement that the
  first upload has not happened must be updated after that upload. The git
  commit and built wheel methods work from any commit.

## Version scheme

Versions follow [PEP 440](https://peps.python.org/pep-0440/), in its
canonical form. A release's version is `X.Y.Z` with an optional
pre-release suffix `aN`, `bN` or `rcN`. Between releases, `main` carries
the next version with a development suffix `.devM`, so that nothing built
from `main` bears the number of a published release; a release build
(`tests/dist_check.py --release-tag`) refuses a `.devM` version. No version
has a `.postN` or local `+...` part, and `tests/test_packaging.py` refuses
any other form.

| Version | What it is |
|---|---|
| `0.1.0a0` | the registration prerelease of #295: the first upload, pre-Alpha, not the Go |
| `0.1.0a1.dev0`, `0.1.0a1.dev1`, ... | `main` after `0.1.0a0` is published; never released |
| `0.1.0a1`, `0.1.0a2`, ... | Public Alpha prereleases; the first one after #255's Go is the Public Alpha |
| `0.1.0b1`, `0.1.0rc1`, `0.1.0` | later, when the project decides |

- A version is used once. PyPI never accepts a file name again, even
  after the release is deleted, so a broken release is fixed by releasing
  the next version, never by replacing files.
- The version lives in two places, `[project].version` in `pyproject.toml`
  and `__version__` in `src/autoforge/__init__.py`. They change together,
  in one commit; `tests/test_packaging.py` fails when they differ, and the
  installed `autoforge --version` prints it.
- The state file records the version that wrote it (`controller_version`)
  only to name the writer in a refusal: whether a state file loads is
  decided by its `protocol_version`, never by the release version.

## The changelog

[`CHANGELOG.md`](../CHANGELOG.md) keeps an `## Unreleased` section at the
top, where every change for operators is recorded as it lands. A release
renames that section to `## <version> (<date>)`, UTC date, and opens a new,
empty `## Unreleased` above it. The release notes are that section's text.
When the version is set before its release is cut, as `0.1.0a0` was
(#295), the section reads `## <version> (unreleased)` and collects the
changes until the release-prep commit the tag names replaces `unreleased`
with the date.

## Procedure

Run every step from your checkout of this repository, whose `origin` is
`iceboundrock/AutoForge` on GitHub: step 6 pushes the tag to `origin`, and
the `gh` commands of steps 3 and 8 find the repository from it.

1. **Release-prep pull request.** From a feature branch, never on `main`:
   set the version in `pyproject.toml` and `src/autoforge/__init__.py`
   (`main`'s development version without its `.devM` suffix);
   turn `## Unreleased` into the release's section; re-check every cell of
   [the compatibility matrix](compatibility.md) against this commit and the
   current releases of each tool, and record the date; update
   [`SECURITY.md`](../SECURITY.md) if the supported versions change. The
   maintainer reviews and merges it. A pull request that touches
   `.github/workflows/` is never merged by AutoForge's controller
   (`safety.protected_merge_paths`).
2. **Wait for a green `ci` on the merge commit of `main`**, including
   `dist (py3.11)` and `dist (py3.12)`.
3. **Security preflight.** No public release while a Critical or High
   security issue is open, unless a reviewed containment that actually
   prevents the affected unsafe path exists and is recorded on the issue,
   nor from a commit that lacks the fix of a closed one
   ([Security preflight](#security-preflight),
   [Security boundaries: open security issues](security.md#open-security-issues)).
4. **Tag locally.** An annotated tag on that merge commit, not pushed yet:

   ```bash
   git tag --annotate v0.1.0a0 --message "AutoForge 0.1.0a0" <commit of main>
   ```

5. **Rehearse the build, from the tag.** From a clean checkout of the
   tag, run exactly what the workflow's `build` job will run. The
   subshell leaves your shell in your checkout: the clone's `origin` is
   that local checkout, not GitHub.

   ```bash
   git clone --quiet --branch v0.1.0a0 --depth 1 "file://$PWD" ../release/src
   (cd ../release/src &&
     uv run --no-project --python 3.12 python -I tests/dist_check.py \
       --release-tag v0.1.0a0 --out-dir ../rehearsal --sha256sums ../rehearsal.sha256)
   ```

   This refuses a tag that does not name the version, builds the sdist and
   a wheel from that sdist, checks both, installs the wheel into a fresh
   environment outside the checkout, runs it, and prints the SHA-256 of
   both files ([Development](development.md#the-built-distributions)).
   These files are never published: a build is not byte-for-byte
   reproducible (file timestamps differ), so the files published and
   attached to the release are the ones the workflow builds, once.
6. **Push the tag,** once. A pushed tag is never moved or deleted; a
   mistake is fixed by the next version:

   ```bash
   git push origin v0.1.0a0
   ```

7. **Publish to PyPI** through the `pypi-publish` workflow run the tag
   started, after approving its deployment
   ([Publishing a version](#publishing-a-version)). No PyPI API token
   exists in this repository, in GitHub or in AutoForge.
8. **GitHub release.** A release for the tag, marked as a pre-release for
   any `aN`, `bN` or `rcN` version, whose notes are the changelog section
   and whose assets are the files that run built and published, with their
   digests (its `release` artifact):

   ```bash
   gh run download <run id> --name release --dir ../release/published
   gh release create v0.1.0a0 --verify-tag --prerelease \
     --title "AutoForge 0.1.0a0" --notes-file notes.md \
     ../release/published/dist/* ../release/published/SHA256SUMS
   ```

9. **Verify what was published,** from the index, in a clean environment
   outside any checkout ([After the upload](#after-the-upload)).
10. **Record** the tag, the commit, the workflow run, the SHA-256 of each
    file, the PyPI URL, the image digest the `publish` step's log names
    ([The workflow](#the-workflow)) and the verification on the release's
    issues (for `0.1.0a0`, #295, #256 and #255; for the Public Alpha, #255).
11. **Follow-up pull request, right after the upload.** From a feature
    branch:
    - set `main` to the next development version, in both places (after
      `0.1.0a0`, `0.1.0a1.dev0`), and open a new, empty `## Unreleased`
      section in `CHANGELOG.md`;
    - after the first upload, correct every statement that says
      `autoforge-cli` is not published yet: [`SECURITY.md`](../SECURITY.md),
      [Security boundaries](security.md) and
      [Compatibility](compatibility.md) and the README's Installation
      section;
    - in [Security boundaries: open security issues](security.md#open-security-issues),
      say which release carries the fixes of the rows marked *Fixed on
      `main`*, whose introduction says that no release does yet;
    - link the record of step 10 on #256 and #255 from the pull request.

## Publishing to PyPI

Publishing goes through GitHub Actions and PyPI Trusted Publishing (OIDC)
from a protected `pypi` environment, with no stored token
([#256 D4](https://github.com/iceboundrock/AutoForge/issues/256#issuecomment-6373954762),
[#295](https://github.com/iceboundrock/AutoForge/issues/295)). Nothing is
uploaded automatically: an upload needs a version tag the maintainer
pushed and the maintainer's approval of that run's deployment.

### The workflow

[`.github/workflows/pypi-publish.yml`](../.github/workflows/pypi-publish.yml)
runs only for a version tag: when a tag `v*` is pushed, or when it is
dispatched by hand for such a tag, from your checkout:

```bash
gh workflow run pypi-publish.yml --ref v0.1.0a0
```

`--ref` takes a branch or a tag, and so does the `ref` of the REST API's
[workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
(`POST /repos/iceboundrock/AutoForge/actions/workflows/pypi-publish.yml/dispatches`).
The procedure does not use the Actions tab's **Run workflow** form, whose
documented selector picks a branch, and `build` refuses a run for a branch.
The workflow never runs on a pull request or a branch push and takes no
input, so the workflow file, the code and the deployment ref of a run are
all the tag's.

1. **`build`**, with `contents: read` and no `id-token`: checks out the tag
   without keeping the token, checks that the tag is `v` followed by
   `[project].version`, and builds the sdist and the wheel once with the
   same check CI's `dist` job runs
   (`tests/dist_check.py --release-tag <tag>`,
   [Development](development.md#the-built-distributions)): build, inspect,
   install outside the checkout, run. It records the SHA-256 of both files
   in the job summary and the job's outputs, and uploads the files and
   `SHA256SUMS` as the run's `release` artifact.
2. **`publish`**, in the `pypi` environment, the only job with
   `id-token: write` and with no other permission: waits for the
   environment's required reviewer, downloads exactly that artifact, checks
   that it holds exactly the two files the version names and that their
   SHA-256 are the ones `build` recorded, and uploads them with
   `pypa/gh-action-pypi-publish`, which also generates and uploads a
   [PEP 740](https://peps.python.org/pep-0740/) attestation for each file,
   signed with the run's identity.

Every action is pinned by full commit SHA, with its release tag beside it.
The workflow uses no repository or environment secret.
`tests/test_publish_workflow.py` fails when any of this changes: a trigger,
a permission, the environment, a pin, or the steps of either job.

What `build` runs is pinned by content as well. uv is pinned to one version
and the SHA-256 of its x86_64 Linux archive (`version` and `checksum` of
`astral-sh/setup-uv`), the same pin as CI's `dist` job, so the release
builds with the uv CI checked. The build backend is pinned by
[`build-constraints.txt`](../build-constraints.txt): `uv build` installs
setuptools only in the version listed there and only if its SHA-256 is one
listed there (`--build-constraints`, `--require-hashes`), and the dist check
fails a wheel that another setuptools built. The runner's own Python 3.12
is the runner image's.

**Known residual: the publish action's container image.**
`pypa/gh-action-pypi-publish` runs in a container image it pulls from
`ghcr.io/pypa/gh-action-pypi-publish` by a tag named after the commit SHA
the workflow pins, and a registry tag can be moved, so the commit pin does
not fix the code that uploads. On 2026-10-23 that tag served the image
digest `sha256:411241df99067c98b6d08fd8e26d14e86740a22460599c2af571e6dc26e27ffc`,
the same as the action's `release-v1` tag and not that of its `v1.14.3`
tag (`sha256:a20ee5e9a490ada64fce0c9176418a4fd5afad3b80e70fb4745803c1309dcdfc`).
The action documents no way to pin the image by digest, so this stays as
it is: the image runs with the `publish` job's OIDC token, which PyPI
exchanges for an upload token for `autoforge-cli` only, valid for minutes,
and it also signs the PEP 740 attestations. The `publish` step's log names
the digest it pulled; step 10 of the [procedure](#procedure) records it.

To move a pin: uv's `version` and `checksum` change together in both
workflows (the checksum is the SHA-256 of `uv-x86_64-unknown-linux-gnu.tar.gz`
on that uv release's GitHub page), and `tests/test_publish_workflow.py`
fails when the two workflows differ. setuptools' version and hashes in
`build-constraints.txt` change together, to the SHA-256 that
`https://pypi.org/pypi/setuptools/<version>/json` lists for that release's
wheel and sdist, and `tests/test_packaging.py` fails when the file pins
anything but the build requirements, or a pin without a hash.

### One-time setup

Done once by the maintainer, before the first upload, in this order. None
of it is code, so none of it is in this repository; record on #295 when
each step was done. The order matters: a job that names an environment
which does not exist yet gets one created on the fly, without protection
rules, so the `pypi` environment must exist before PyPI trusts the
workflow.

1. **The `pypi` environment on GitHub.** In the repository's **Settings →
   Environments**, create `pypi` with:
   - **Required reviewers**: the maintainer. Leave **Prevent self-review**
     off, since the maintainer who pushes the tag is the one who approves.
   - **Deployment branches and tags**: **Selected branches and tags**, with
     one rule of type **Tag** and pattern `v*`. A run for any other ref
     cannot deploy to `pypi`.
   - **Allow administrators to bypass configured protection rules**: off.
   - No environment secrets and no environment variables.
2. **A tag ruleset for `v*`.** In **Settings → Rules → Rulesets**, a new
   **tag** ruleset, enforcement **Active**, targeting the tag pattern `v*`,
   with **Restrict creations**, **Restrict updates** and **Restrict
   deletions** and **Block force pushes**. Restrict bypass to authorized
   maintainers: the configured actors are the maintainer's account and the
   repository admin role, so check who holds that role. The bypass applies
   to all these rules, not only creation: the procedure never moves or
   deletes a release tag. AutoForge's controller pushes branches only,
   never a tag.
3. **No secret.** `gh secret list` and `gh secret list --env pypi` print
   nothing. Trusted Publishing needs no PyPI token, and none is created.
4. **A pending trusted publisher on PyPI,** last. Signed in to PyPI as the
   maintainer (with two-factor authentication), open
   <https://pypi.org/manage/account/publishing/>, choose **GitHub** under
   "Add a new pending publisher", and enter exactly:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `autoforge-cli` |
   | Owner | `iceboundrock` |
   | Repository name | `AutoForge` |
   | Workflow name | `pypi-publish.yml` |
   | Environment name | `pypi` |

   A pending publisher does not reserve the name: until the first upload
   creates the project, anyone can register `autoforge-cli`
   ([PyPI](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)).
   PyPI can also refuse a name nobody uses (a prohibited name, or one too
   similar to an existing project), so a missing project page proves
   nothing either: the name is AutoForge's only once the first upload has
   succeeded, and until then no document says it is registered. Just
   before the first upload, check that
   <https://pypi.org/project/autoforge-cli/> is still a 404; if the name is
   taken, or the upload is refused for its name, stop and reopen D2 on
   #256. The first upload turns the pending publisher into the project's
   trusted publisher, managed from then on at
   <https://pypi.org/manage/project/autoforge-cli/settings/publishing/>.

### Rehearsing the build

Required, before every release: the build job's own command on the tagged
commit, before the tag is pushed (step 5 of the [procedure](#procedure)).
It runs exactly what `build` will run, except that the files it builds are
not the ones published.

Optional, and not needed for `0.1.0a0`:

- **The workflow on a fork.** Push the tag to a fork of this repository;
  its `build` job runs as it will here. Create a `pypi` environment with a
  required reviewer in the fork first and reject the deployment there. If
  the fork's `publish` job runs anyway, the token exchange fails, because
  PyPI's publisher names `iceboundrock/AutoForge`: nothing is uploaded.
- **A full rehearsal on TestPyPI.** A pending publisher on
  <https://test.pypi.org/manage/account/publishing/> for the fork, with
  environment `testpypi`, and the fork's copy of the workflow changed to
  that environment and `repository-url: https://test.pypi.org/legacy/`.
  A version uploaded to TestPyPI cannot be uploaded there again either;
  it does not use up the version on PyPI.

### Publishing a version

1. Push the tag (step 6 of the [procedure](#procedure)). The
   `pypi-publish` run for it starts.
2. When `build` has passed, open the run and check that it is for the tag
   you pushed and the commit it names, and read the digests in its summary.
   Before approval, download that run's `release` artifact, check its
   SHA-256 against the build summary, and install its exact wheel
   non-editably in a fresh environment outside the checkout on CPython 3.11
   as well. Check
   `autoforge --version`, `--help` and every installed prompt template, as
   the build job did on 3.12. CI's 3.11 job builds different files and
   cannot prove the release wheel works there
   ([#256 D3](https://github.com/iceboundrock/AutoForge/issues/256#issuecomment-6375246718)).
3. Approve the `pypi` deployment (**Review deployments**). `publish`
   re-verifies the files and uploads them. Reject it to stop without
   uploading anything; the tag stays, and the next attempt is the next
   version.

### After the upload

1. <https://pypi.org/project/autoforge-cli/0.1.0a0/> exists, marked as a
   pre-release, and links to this repository.
2. **Download files** lists exactly `autoforge_cli-0.1.0a0.tar.gz` and
   `autoforge_cli-0.1.0a0-py3-none-any.whl`, whose SHA-256 are those of the
   run's `SHA256SUMS`.
3. Each file's provenance names this repository, `pypi-publish.yml` and the
   tag (the file's page on PyPI, or
   `https://pypi.org/integrity/autoforge-cli/0.1.0a0/<file name>/provenance`).
4. A clean install, outside any checkout, with no cache and an empty tool
   directory, prints the version:

   ```bash
   export UV_TOOL_DIR="$(mktemp -d)" UV_TOOL_BIN_DIR="$(mktemp -d)"
   uv tool install --no-cache --python 3.12 --prerelease allow 'autoforge-cli==0.1.0a0'
   "$UV_TOOL_BIN_DIR/autoforge" --version     # autoforge 0.1.0a0
   ```

5. The GitHub release attaches the files of the run's `release` artifact
   (`gh run download <run id> --name release`), the files PyPI serves.
6. The follow-up pull request of step 11 of the [procedure](#procedure)
   opens the next development version.

### Troubleshooting

| Symptom | Cause and what to do |
|---|---|
| `build` fails at "Refuse anything but a version tag" | The run is not for a tag `v*` (a dispatch for a branch, say). Dispatch it for the tag: `gh workflow run pypi-publish.yml --ref <tag>` ([The workflow](#the-workflow)). |
| `build` fails with "the tag ... does not name the package's version" | The tag is not `v` followed by `[project].version`, or the version is not in the [scheme](#version-scheme). Nothing was uploaded; release the next version from a corrected commit. |
| `build` fails with "a .devM version is never released" | The release-prep pull request kept `main`'s `.devM` suffix. Nothing was uploaded; release from a commit that drops it. |
| `build` fails with "Hash mismatch for `setuptools`", or setup-uv's "Checksum ... did not match" | A file the pins name is not the file the index or the release serves. Do not loosen a pin to get past it: find out why, then fix it through a pull request and release the next version. |
| `build` fails in the dist check | The same failure `make dist-check` shows locally; `main` should not have passed CI with it. Fix it through a pull request and release the next version. |
| The run waits at `publish` | It waits for the required reviewer's approval: **Review deployments**. |
| `publish` cannot deploy: the tag is not allowed to deploy to `pypi` | The ref does not match the environment's `v*` rule. |
| `publish` fails at the token exchange (`invalid-publisher`) | PyPI's (pending) publisher does not match the run: check the five fields above, the workflow's file name and the environment's name. |
| `publish` fails re-verifying the files | The artifact is not what `build` recorded. Do not upload by hand; find out why first. |
| PyPI answers `400 File already exists` | That version was uploaded before (perhaps by an earlier attempt). PyPI never accepts a file name twice: release the next version. Never set `skip-existing`. |
| The name `autoforge-cli` is taken, or PyPI refuses it | Someone registered it before the first upload, or PyPI does not allow it: stop, and reopen D2 on #256. |

### Not before the Go, and not the Go

- No upload happens on its own: the workflow runs only for a tag the
  maintainer pushed, and uploads only after the maintainer approved it.
  Before #255's Go/No-Go, the only upload is #295's registration
  prerelease `0.1.0a0`.
- **The first upload is not #255's Go.** `0.1.0a0` registers the name; it
  is a pre-Alpha snapshot, not validated for unattended production use.
  The Public Alpha's release candidate, security, demonstration and
  Go/No-Go gates all still apply to the Public Alpha release.

### Security preflight

No public upload while a Critical or High security issue is open, unless a
reviewed containment that actually prevents the affected unsafe path
exists and is recorded on the issue. For `0.1.0a0` that means each of
[#280](https://github.com/iceboundrock/AutoForge/issues/280),
[#281](https://github.com/iceboundrock/AutoForge/issues/281),
[#282](https://github.com/iceboundrock/AutoForge/issues/282),
[#283](https://github.com/iceboundrock/AutoForge/issues/283) and
[#284](https://github.com/iceboundrock/AutoForge/issues/284)
([Security boundaries: open security issues](security.md#open-security-issues))
is closed, or has such a containment recorded on it, before the tag is
pushed.

A fix counts only if the commit you tag carries it. Each of the five was
fixed on `main` by one squash-merged pull request:

| Issue | Severity | Fixed on `main` by |
|---|---|---|
| #280 | Critical | `f4fd5ef` (#305) |
| #281 | High | `d6fe4fd` (#334) |
| #282 | High | `1f6abad` (#336) |
| #283 | High | `9a7d8f9` (#337) |
| #284 | High | `e611475` (#314) |

Step 3 of the [procedure](#procedure) checks, for the commit step 4 will
tag, that it carries every fix and that every issue is closed on GitHub.
The first loop prints nothing when the commit carries all five fixes, and
`missing <fix>` for each it does not; the second prints each issue's
state:

```bash
commit=<commit of main>
for fix in \
  f4fd5efec6d5ffbf12397a0ba59560eeb2379148 \
  d6fe4fd4f37ddcc2d55fc53f01228b2a7d9dce3a \
  1f6abad5debe375594fc2ab6d13cbe5cfac96396 \
  9a7d8f9afc6e66992008ff211f58d1518be8a197 \
  e611475bca9862e6f15689c7e66d1a4b59626478; do
  git merge-base --is-ancestor "$fix" "$commit" || echo "missing $fix"
done
for issue in 280 281 282 283 284; do
  echo "#$issue $(gh issue view "$issue" --json state --jq .state)"
done
```

Each issue must print `CLOSED`. An issue that prints `OPEN` blocks the
upload unless its reviewed containment is recorded on it. Then check
for what the audit did not find: no other open issue is rated Critical or
High, and the repository's **Security** tab lists no advisory rated
Critical or High that is still in triage or a draft (a private
vulnerability report arrives there in triage,
[`SECURITY.md`](../SECURITY.md#reporting-a-vulnerability)). Record the
output on #295 with the commit it checked.
