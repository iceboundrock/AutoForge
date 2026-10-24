# Releasing

How a version of AutoForge is numbered, built, tagged, published and
recorded. A release is a maintainer action from start to finish: the
maintainer of `iceboundrock/AutoForge` cuts and publishes it, and nothing
releases on its own. No workflow publishes on a push to a branch or on a
pull request, no agent tags or uploads, and AutoForge's controller has no
release step.

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
  limitation. Text in the README stays true after an upload: it never says
  that nothing is published.

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
   ([Security boundaries: open security issues](security.md#open-security-issues)).
4. **Tag locally.** An annotated tag on that merge commit, not pushed yet:

   ```bash
   git tag --annotate v0.1.0a0 --message "AutoForge 0.1.0a0" <commit of main>
   ```

5. **Build once, from the tag.** The sdist and the wheel are built exactly
   once, from a clean checkout of the tag, by the same check CI runs; the
   files that check passed are the files that are published and attached
   to the GitHub release, and nothing is rebuilt for a later step:

   ```bash
   git clone --quiet --branch v0.1.0a0 --depth 1 "file://$PWD" ../release/src
   cd ../release/src
   uv run --no-project --python 3.12 python -I tests/dist_check.py \
     --out-dir ../dist --sha256sums ../SHA256SUMS
   ```

   This builds the sdist and a wheel from that sdist, checks both, installs
   the wheel into a fresh environment outside the checkout, runs it, and
   writes the SHA-256 of both files
   ([Development](development.md#the-built-distributions)).
6. **Push the tag,** once. A pushed tag is never moved or deleted; a
   mistake is fixed by the next version:

   ```bash
   git push origin v0.1.0a0
   ```

7. **Publish to PyPI** through Trusted Publishing only
   ([Publishing to PyPI](#publishing-to-pypi)). No PyPI API token exists in
   this repository, in GitHub or in AutoForge.
8. **GitHub release.** A release for the tag, marked as a pre-release for
   any `aN`, `bN` or `rcN` version, whose notes are the changelog section
   and whose assets are the same files and their digests:

   ```bash
   gh release create v0.1.0a0 --verify-tag --prerelease \
     --title "AutoForge 0.1.0a0" --notes-file notes.md \
     ../release/dist/* ../release/SHA256SUMS
   ```

9. **Verify what was published,** from the index, in a clean environment
   outside any checkout:

   ```bash
   uv tool install --python 3.12 --prerelease allow 'autoforge-cli==0.1.0a0'
   autoforge --version
   ```

   and compare the SHA-256 of the files PyPI serves with `SHA256SUMS`.
10. **Record** the tag, the commit, the SHA-256 of each file, the PyPI URL,
    the workflow run and the verification on the release's issue (for
    `0.1.0a0`, #295 and #256; for the Public Alpha, #255).

## Publishing to PyPI

Publishing goes through GitHub Actions and PyPI Trusted Publishing (OIDC)
from a protected `pypi` environment, with no stored token
([#256 D4](https://github.com/iceboundrock/AutoForge/issues/256#issuecomment-6373954762)).
The workflow and the one-time PyPI and GitHub setup are #295's; until they
land, nothing can be published and `autoforge-cli` does not exist on PyPI.
