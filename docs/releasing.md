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

## Version scheme

Versions follow [PEP 440](https://peps.python.org/pep-0440/), in its
canonical form `X.Y.Z` with an optional pre-release suffix `aN`, `bN` or
`rcN` (no `.devN`, `.postN` or local `+...` part).
`tests/test_packaging.py` refuses any other form.

| Version | What it is |
|---|---|
| `0.1.0a0` | the registration prerelease of #295: the first upload, pre-Alpha, not the Go |
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

1. **Release-prep pull request.** From a feature branch, never on `main`:
   set the version in `pyproject.toml` and `src/autoforge/__init__.py`;
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
   prevents the affected unsafe path exists and is recorded on the issue
   ([Security preflight](#security-preflight),
   [Security boundaries: open security issues](security.md#open-security-issues)).
4. **Tag locally.** An annotated tag on that merge commit, not pushed yet:

   ```bash
   git tag --annotate v0.1.0a0 --message "AutoForge 0.1.0a0" <commit of main>
   ```

5. **Rehearse the build, from the tag.** From a clean checkout of the
   tag, run exactly what the workflow's `build` job will run:

   ```bash
   git clone --quiet --branch v0.1.0a0 --depth 1 "file://$PWD" ../release/src
   cd ../release/src
   uv run --no-project --python 3.12 python -I tests/dist_check.py \
     --release-tag v0.1.0a0 --out-dir ../rehearsal --sha256sums ../rehearsal.sha256
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
    file, the PyPI URL and the verification on the release's issues (for
    `0.1.0a0`, #295, #256 and #255; for the Public Alpha, #255).

## Publishing to PyPI

Publishing goes through GitHub Actions and PyPI Trusted Publishing (OIDC)
from a protected `pypi` environment, with no stored token
([#256 D4](https://github.com/iceboundrock/AutoForge/issues/256#issuecomment-6373954762),
[#295](https://github.com/iceboundrock/AutoForge/issues/295)). Nothing is
uploaded automatically: an upload needs a version tag the maintainer
pushed and the maintainer's approval of that run's deployment.

### The workflow

[`.github/workflows/pypi-publish.yml`](../.github/workflows/pypi-publish.yml)
runs only for a version tag: when a tag `v*` is pushed, or by hand from the
Actions tab with **Use workflow from** set to such a tag. It never runs on
a pull request or a branch push and takes no input, so the workflow file,
the code and the deployment ref of a run are all the tag's.

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
   deletions** and **Block force pushes**, and the repository admin role
   (the maintainer) as the only bypass actor. Only the maintainer can then
   create, move or delete a release tag, and the procedure never moves or
   deletes one. AutoForge's controller pushes branches only, never a tag.
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

### Troubleshooting

| Symptom | Cause and what to do |
|---|---|
| `build` fails at "Refuse anything but a version tag" | The run is not for a tag `v*` (a dispatch from a branch, say). Dispatch it from the tag. |
| `build` fails with "the tag ... does not name the package's version" | The tag is not `v` followed by `[project].version`, or the version is not in the [scheme](#version-scheme). Nothing was uploaded; release the next version from a corrected commit. |
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
