"""#292: a container launch's seed, built and checked on the host (ADR 0013 D4, D11).

Every repository is built under ``tmp_path`` by an isolated git (no system
or global configuration, no hook, no network), and every seed is read back
the way a stager would: indexed into a fresh repository whose ``shallow``
file names the manifest's boundaries, with ``HEAD`` detached at the seed.
No container runtime is needed. The seed's own checks are driven with
packs the tests build from the same store, each wrong in one way, against
the selection the shared store gives.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

import pytest

from autoforge.errors import SandboxLaunchRefusedError
from autoforge.executor import execute
from autoforge.git_input import SEED_MANIFEST_NAME, LaunchGitInput, seed_manifest_digest
from autoforge.git_transport import NO_AUTO_MAINTENANCE
from autoforge.seed_pack import (
    FULL_HISTORY,
    SEED_PACK_NAME,
    SETTING_DEPTH,
    SETTING_MAX_BYTES,
    SETTING_MAX_OBJECTS,
    SETTING_TAGS,
    BuiltSeed,
    SeedManifest,
    SeedSettings,
    SeedTag,
    build_seed,
    check_seed,
    discard,
    parse_manifest,
    select_seed,
)

from .conftest import GIT_IDENT

FORMATS = ["sha1", "sha256"]

# The test's own git: no system or global configuration, no hook, no
# network, no automatic maintenance, a fixed identity.
_ISOLATED = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_LAZY_FETCH": "1",
}


@pytest.fixture(autouse=True)
def seeds(tmp_path, monkeypatch) -> Path:
    """Where ``tempfile.mkdtemp`` creates the seeds and their scratch directories."""
    where = tmp_path / "temporary"
    where.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(where))
    return where


def _env() -> dict[str, str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {**clean, **_ISOLATED}


def run_git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    """The test's isolated git in ``repo``; its stdout. A failure fails the test."""
    res = subprocess.run(
        ["git", "-C", str(repo), *GIT_IDENT, *NO_AUTO_MAINTENANCE, *args],
        input=data,
        capture_output=True,
        env=_env(),
    )
    assert res.returncode == 0, f"git {' '.join(args)}: {res.stderr.decode(errors='replace')}"
    return res.stdout


def out(repo: Path, *args: str, data: bytes | None = None) -> str:
    return run_git(repo, *args, data=data).decode().strip()


def init_repo(path: Path, object_format: str = "sha1") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "-q", "-b", "main", f"--object-format={object_format}")
    return path


def commit_files(repo: Path, message: str, files: dict[str, bytes | None]) -> str:
    """One commit on the current branch writing (or, for ``None``, deleting) ``files``."""
    for name, content in files.items():
        target = repo / name
        if content is None:
            run_git(repo, "rm", "-q", name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        run_git(repo, "add", name)
    run_git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return out(repo, "rev-parse", "HEAD")


def common_of(repo: Path) -> Path:
    return Path(out(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def git_input(
    phase: str,
    seed: str,
    lower: str,
    *,
    carried: str = "",
    export_command: bool = False,
    object_format: str = "sha1",
    launch: str = "L-1",
) -> LaunchGitInput:
    return LaunchGitInput(
        phase=phase,
        run="run-1",
        issue=292,
        launch=launch,
        repository="owner/repo",
        object_format=object_format,
        seed=seed,
        lower="" if export_command else lower,
        carried=carried,
        export_command=export_command,
        entry_label="entry-1",
    )


@dataclasses.dataclass
class Review:
    """A PR branch off ``main``: ``base`` its fork point, ``head`` its tip, ``merge_base``."""

    repo: Path
    base: str
    head: str
    merge_base: str
    pr_commits: tuple[str, ...]
    main_tip: str


def review_repo(tmp_path: Path, object_format: str = "sha1", *, merged: bool = False) -> Review:
    """``main`` with history, a PR branch of two commits, ``main`` moved on after the fork.

    ``merged``: the PR branch merged ``main`` between its two commits, so its
    merge base is ``main``'s tip and its first commit's parent lies outside
    the range.
    """
    repo = init_repo(tmp_path / "repo", object_format)
    commit_files(repo, "c0", {"README": b"zero\n"})
    commit_files(repo, "c1", {"src/a.py": b"print('a')\n"})
    base = commit_files(repo, "c2", {"src/b.py": b"print('b')\n"})
    run_git(repo, "checkout", "-q", "-b", "pr")
    p1 = commit_files(repo, "p1", {"src/a.py": b"print('a, changed')\n"})
    run_git(repo, "checkout", "-q", "main")
    main_tip = commit_files(repo, "m1", {"docs/m.md": b"main moved\n"})
    run_git(repo, "checkout", "-q", "pr")
    commits = [p1]
    if merged:
        run_git(repo, "merge", "-q", "--no-edit", "main")
        commits.append(out(repo, "rev-parse", "HEAD"))
    head = commit_files(repo, "p2", {"src/c.py": b"print('c')\n", "src/b.py": None})
    commits.append(head)
    merge_base = out(repo, "merge-base", "HEAD", "main")
    return Review(repo, base, head, merge_base, tuple(commits), main_tip)


def stage_seed(seed_dir: Path, where: Path) -> Path:
    """What a stager does with a seed (#271's), in a fresh repository: index it, set the
    shallow boundaries, detach ``HEAD`` at the seed (or the carried) commit."""
    manifest = parse_manifest((seed_dir / SEED_MANIFEST_NAME).read_bytes())
    init_repo(where, manifest.object_format)
    git_dir = where / ".git"
    if manifest.boundaries:
        (git_dir / "shallow").write_text("".join(f"{oid}\n" for oid in manifest.boundaries))
    run_git(
        where, "index-pack", "--stdin", "--strict", data=(seed_dir / SEED_PACK_NAME).read_bytes()
    )
    (git_dir / "HEAD").write_text(f"{manifest.carried or manifest.seed}\n")
    return where


def pack_objects(staged: Path) -> dict[str, str]:
    """Every object the staged repository holds: id to type."""
    listing = out(
        staged, "cat-file", "--batch-all-objects", "--batch-check=%(objecttype) %(objectname)"
    )
    return {oid: kind for kind, oid in (line.split() for line in listing.splitlines())}


def commits_of(staged: Path) -> set[str]:
    return {oid for oid, kind in pack_objects(staged).items() if kind == "commit"}


def build(repo: Path, gi: LaunchGitInput, settings: SeedSettings | None = None) -> BuiltSeed:
    return build_seed(common_of(repo), gi, settings or SeedSettings())


def snapshot(directory: Path) -> dict[str, bytes]:
    """Every file below ``directory``, by relative path, with its bytes (links as their text)."""
    found: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        rel = str(path.relative_to(directory))
        if path.is_symlink():
            found[rel] = b"-> " + os.readlink(path).encode()
        elif path.is_file():
            found[rel] = path.read_bytes()
        else:
            found[rel] = b"<dir>"
    return found


# -- what the seed holds (D4, D11) --------------------------------------------------------


@pytest.mark.parametrize("object_format", FORMATS)
def test_a_base_seed_is_the_base_at_depth_one_with_no_tag_ref_or_reflog(tmp_path, object_format):
    """ANALYZE_EXECUTE: the verified base alone, its whole tree, nothing else of the history.

    The store has tags (lightweight and annotated) on the base and branches
    besides ``main``; with the settings unset none of them reaches the seed.
    """
    review = review_repo(tmp_path, object_format)
    run_git(review.repo, "tag", "light", review.base)
    run_git(review.repo, "tag", "-a", "-m", "annotated", "annotated", review.base)
    built = build(
        review.repo,
        git_input("ANALYZE_EXECUTE", review.base, review.base, object_format=object_format),
    )

    manifest = built.manifest
    assert (manifest.seed, manifest.lower, manifest.carried) == (review.base, review.base, "")
    assert manifest.boundaries == (review.base,)
    assert manifest.tags == ()
    assert (manifest.history_depth, manifest.history_tags) == (0, False)
    assert manifest.object_format == object_format
    assert sorted(os.listdir(built.directory)) == sorted([SEED_MANIFEST_NAME, SEED_PACK_NAME])
    assert built.digest == seed_manifest_digest(str(built.directory))

    staged = stage_seed(built.directory, tmp_path / "staged")
    objects = pack_objects(staged)
    assert commits_of(staged) == {review.base}
    assert "tag" not in objects.values()
    assert out(staged, "for-each-ref") == ""
    assert not (staged / ".git" / "logs").exists()
    assert out(staged, "rev-parse", "HEAD") == review.base
    assert out(staged, "rev-parse", "HEAD^{tree}") == out(
        review.repo, "rev-parse", f"{review.base}^{{tree}}"
    )
    # The whole tree, and every blob of it.
    assert out(staged, "ls-tree", "-r", "HEAD") == out(review.repo, "ls-tree", "-r", review.base)
    assert out(staged, "fsck", "--strict", "--no-dangling") == ""


@pytest.mark.parametrize("object_format", FORMATS)
def test_a_review_seed_holds_the_bound_head_the_merge_base_and_the_prs_diff(
    tmp_path, object_format
):
    review = review_repo(tmp_path, object_format)
    gi = git_input("REVIEW", review.head, review.merge_base, object_format=object_format)
    built = build(review.repo, gi)

    assert (built.manifest.seed, built.manifest.lower) == (review.head, review.merge_base)
    assert built.manifest.boundaries == (review.merge_base,)
    staged = stage_seed(built.directory, tmp_path / "staged")
    assert commits_of(staged) == {*review.pr_commits, review.merge_base}
    # Inside the seed, the PR's diff is the host's.
    host = run_git(review.repo, "diff", "--binary", review.merge_base, review.head)
    assert run_git(staged, "diff", "--binary", review.merge_base, review.head) == host
    assert host  # the diff is not empty: the comparison is meaningful
    # main's later commit is not the PR's and is not seeded.
    assert review.main_tip not in commits_of(staged)


def test_a_pr_branch_that_merged_its_base_has_each_outside_parent_as_a_boundary(tmp_path):
    review = review_repo(tmp_path, merged=True)
    assert review.merge_base == review.main_tip
    gi = git_input("REVIEW", review.head, review.merge_base)
    built = build(review.repo, gi)

    # The merge base, and the PR's first commit's parent: the fork point, which
    # lies outside the range since main's tip reaches it.
    assert set(built.manifest.boundaries) == {review.merge_base, review.base}
    staged = stage_seed(built.directory, tmp_path / "staged")
    assert commits_of(staged) == {*review.pr_commits, review.merge_base, review.base}
    # No commit's parent tree is missing: every commit above the boundaries
    # diffs against each of its parents.
    for commit in review.pr_commits:
        parents = out(review.repo, "rev-list", "--parents", "-n1", commit).split()[1:]
        for parent in parents:
            assert run_git(staged, "diff", "--binary", parent, commit) == run_git(
                review.repo, "diff", "--binary", parent, commit
            )
    host = run_git(review.repo, "diff", "--binary", review.merge_base, review.head)
    assert run_git(staged, "diff", "--binary", review.merge_base, review.head) == host
    assert out(staged, "fsck", "--strict", "--no-dangling") == ""


def test_a_force_pushed_head_gets_a_seed_of_its_own_revision(tmp_path):
    """A head that does not descend from the run's last candidate: none of that candidate's
    commits it does not reach are seeded."""
    review = review_repo(tmp_path)
    earlier = review.head
    run_git(review.repo, "checkout", "-q", "--detach", review.base)
    rebased = commit_files(review.repo, "rebased", {"src/d.py": b"print('d')\n"})
    merge_base = out(review.repo, "merge-base", rebased, "main")
    built = build(review.repo, git_input("REVIEW", rebased, merge_base))

    staged = stage_seed(built.directory, tmp_path / "staged")
    seeded = commits_of(staged)
    assert seeded == {rebased, merge_base}
    assert earlier not in seeded and not set(review.pr_commits) & seeded
    assert built.manifest.seed == rebased


@pytest.mark.parametrize("phase", ["FIX", "ANALYZE_EXECUTE"])
def test_a_relaunch_carries_its_carried_commit_and_the_commits_below_it(tmp_path, phase):
    review = review_repo(tmp_path)
    seed, lower = (review.head, review.merge_base) if phase == "FIX" else (review.base, review.base)
    run_git(review.repo, "checkout", "-q", "--detach", seed)
    first = commit_files(review.repo, "earlier launch 1", {"fix.txt": b"one\n"})
    carried = commit_files(review.repo, "earlier launch 2", {"fix.txt": b"two\n"})
    built = build(review.repo, git_input(phase, seed, lower, carried=carried))

    assert built.manifest.carried == carried
    staged = stage_seed(built.directory, tmp_path / "staged")
    expected = {first, carried, lower} | (set(review.pr_commits) if phase == "FIX" else set())
    assert commits_of(staged) == expected
    # The stager checks out the carried commit; its tree is whole.
    assert out(staged, "rev-parse", "HEAD") == carried
    assert out(staged, "ls-tree", "-r", "HEAD") == out(review.repo, "ls-tree", "-r", carried)


def test_an_export_commands_seed_is_its_commit_and_its_tree_with_no_history(tmp_path):
    review = review_repo(tmp_path)
    settings = SeedSettings(history_depth=FULL_HISTORY, history_tags=True)
    run_git(review.repo, "tag", "v1", review.head)
    built = build(review.repo, git_input("REVIEW", review.head, "", export_command=True), settings)

    assert built.manifest.boundaries == (review.head,)
    assert (built.manifest.lower, built.manifest.tags) == ("", ())
    # Export commands are unaffected by the history settings (D11).
    assert (built.manifest.history_depth, built.manifest.history_tags) == (0, False)
    staged = stage_seed(built.directory, tmp_path / "staged")
    assert commits_of(staged) == {review.head}


def test_more_history_comes_only_from_the_operators_depth_setting(tmp_path):
    review = review_repo(tmp_path)
    gi = git_input("REVIEW", review.head, review.merge_base)
    c1 = out(review.repo, "rev-parse", f"{review.merge_base}~1")
    c0 = out(review.repo, "rev-parse", f"{review.merge_base}~2")

    one = build(review.repo, gi, SeedSettings(history_depth=1))
    assert one.selection.commits == {*review.pr_commits, review.merge_base, c1}
    assert one.manifest.boundaries == (c1,)
    assert one.manifest.history_depth == 1
    staged = stage_seed(one.directory, tmp_path / "one")
    assert out(staged, "rev-list", "--count", "HEAD") == str(len(review.pr_commits) + 2)

    full = build(review.repo, gi, SeedSettings(history_depth=FULL_HISTORY))
    assert full.selection.commits == {*review.pr_commits, review.merge_base, c1, c0}
    assert full.manifest.boundaries == ()
    staged = stage_seed(full.directory, tmp_path / "full")
    assert out(staged, "rev-parse", "--is-shallow-repository") == "false"
    assert out(staged, "fsck", "--strict", "--no-dangling") == ""


def test_building_a_seed_writes_nothing_to_the_shared_store(tmp_path):
    review = review_repo(tmp_path, merged=True)
    run_git(review.repo, "tag", "-a", "-m", "m", "v1", review.head)
    common = common_of(review.repo)
    before = snapshot(common)
    settings = SeedSettings(history_depth=2, history_tags=True)
    built = build(review.repo, git_input("REVIEW", review.head, review.merge_base), settings)
    assert snapshot(common) == before
    discard(built.directory)
    assert not built.directory.exists()


def test_a_missing_bound_revision_is_refused_before_anything_is_written(tmp_path, seeds):
    review = review_repo(tmp_path)
    absent = "0" * 40
    for gi, why in (
        (git_input("REVIEW", absent, review.merge_base), "is not a commit the shared store holds"),
        (git_input("REVIEW", review.head, review.main_tip), "is not an ancestor of the seed"),
        (
            git_input("FIX", review.head, review.merge_base, carried=review.main_tip),
            "does not descend",
        ),
    ):
        with pytest.raises(SandboxLaunchRefusedError, match=why):
            build(review.repo, gi)
    assert list(seeds.iterdir()) == []


# -- tags (D11) ---------------------------------------------------------------------------


@dataclasses.dataclass
class Tagged:
    review: Review
    light: str
    annotated: str
    of_tag: str
    of_tag_inner: str


def tagged_repo(tmp_path: Path) -> Tagged:
    """Tags on seeded commits, on a commit outside the seed, on a tree and on a blob."""
    review = review_repo(tmp_path)
    repo = review.repo
    run_git(repo, "tag", "light", review.merge_base)
    run_git(repo, "tag", "-a", "-m", "release", "annotated", review.head)
    run_git(repo, "tag", "-a", "-m", "inner", "inner", review.pr_commits[0])
    run_git(repo, "tag", "-a", "-m", "outer", "outer", "inner")
    run_git(repo, "tag", "-d", "inner")  # the chain survives in "outer" alone
    run_git(repo, "tag", "-a", "-m", "old", "outside", f"{review.merge_base}~1")
    run_git(repo, "tag", "main-tip", review.main_tip)
    run_git(repo, "tag", "-a", "-m", "tree", "a-tree", f"{review.head}^{{tree}}")
    run_git(repo, "tag", "a-blob", f"{review.head}:src/c.py")
    of_tag = out(repo, "rev-parse", "refs/tags/outer")
    inner = out(repo, "cat-file", "tag", of_tag).splitlines()[0].split()[1]
    return Tagged(
        review,
        light=out(repo, "rev-parse", "refs/tags/light"),
        annotated=out(repo, "rev-parse", "refs/tags/annotated"),
        of_tag=of_tag,
        of_tag_inner=inner,
    )


def test_with_tags_off_the_seed_holds_no_tag_even_on_seeded_commits(tmp_path):
    tagged = tagged_repo(tmp_path)
    review = tagged.review
    built = build(review.repo, git_input("REVIEW", review.head, review.merge_base))
    assert built.manifest.tags == ()
    staged = stage_seed(built.directory, tmp_path / "staged")
    assert "tag" not in pack_objects(staged).values()


def test_with_tags_on_the_seed_holds_exactly_the_tags_that_peel_to_a_seeded_commit(tmp_path):
    tagged = tagged_repo(tmp_path)
    review = tagged.review
    settings = SeedSettings(history_tags=True)
    built = build(review.repo, git_input("REVIEW", review.head, review.merge_base), settings)

    assert built.manifest.tags == (
        SeedTag("refs/tags/annotated", tagged.annotated),
        SeedTag("refs/tags/light", tagged.light),
        SeedTag("refs/tags/outer", tagged.of_tag),
    )
    assert built.manifest.history_tags is True
    staged = stage_seed(built.directory, tmp_path / "staged")
    tags_in_pack = {oid for oid, kind in pack_objects(staged).items() if kind == "tag"}
    # Every tag object on the selected chains, a tag of a tag included, and no other.
    assert tags_in_pack == {tagged.annotated, tagged.of_tag, tagged.of_tag_inner}
    # "outside" peels to a commit below the merge base; "main-tip" to main's
    # later commit; "a-tree" and "a-blob" to no commit. None is seeded.
    refs = {tag.ref for tag in built.manifest.tags}
    assert not refs & {
        "refs/tags/outside",
        "refs/tags/main-tip",
        "refs/tags/a-tree",
        "refs/tags/a-blob",
    }
    for tag in built.manifest.tags:
        assert out(staged, "rev-parse", f"{tag.object}^{{commit}}") in commits_of(staged)


def test_the_tags_count_towards_the_seeds_bounds_and_a_refusal_names_the_setting(tmp_path):
    tagged = tagged_repo(tmp_path)
    review = tagged.review
    gi = git_input("REVIEW", review.head, review.merge_base)
    plain = build(review.repo, gi)
    bound = SeedSettings(history_tags=True, max_objects=plain.manifest.pack_objects)
    with pytest.raises(SandboxLaunchRefusedError) as refused:
        build(review.repo, gi, bound)
    assert SETTING_MAX_OBJECTS in str(refused.value)
    assert SETTING_TAGS in str(refused.value)


# -- the bounds (D4) ------------------------------------------------------------------------


def test_a_seed_over_its_object_bound_is_refused_naming_the_setting(tmp_path, seeds):
    review = review_repo(tmp_path)
    gi = git_input("REVIEW", review.head, review.merge_base)
    with pytest.raises(SandboxLaunchRefusedError, match=SETTING_MAX_OBJECTS) as refused:
        build(review.repo, gi, SeedSettings(max_objects=3))
    assert SETTING_DEPTH not in str(refused.value)
    assert list(seeds.iterdir()) == []


def test_a_seed_over_its_byte_bound_is_refused_naming_the_setting(tmp_path, seeds):
    review = review_repo(tmp_path)
    gi = git_input("REVIEW", review.head, review.merge_base)
    with pytest.raises(SandboxLaunchRefusedError, match=SETTING_MAX_BYTES):
        build(review.repo, gi, SeedSettings(max_bytes=64))
    assert list(seeds.iterdir()) == []


def test_a_history_setting_that_makes_the_seed_exceed_its_bound_is_named(tmp_path):
    review = review_repo(tmp_path)
    gi = git_input("REVIEW", review.head, review.merge_base)
    plain = build(review.repo, gi)
    for settings in (
        SeedSettings(history_depth=2, max_objects=plain.manifest.pack_objects),
        SeedSettings(history_depth=FULL_HISTORY, max_bytes=plain.manifest.pack_bytes),
    ):
        with pytest.raises(SandboxLaunchRefusedError) as refused:
            build(review.repo, gi, settings)
        assert SETTING_DEPTH in str(refused.value)


# -- the seed's own checks (D4) -----------------------------------------------------------------


def _repack(
    built: BuiltSeed, where: Path, repo: Path, ids: Iterable[str], **manifest_changes
) -> Path:
    """A copy of ``built`` whose pack holds exactly ``ids`` (from ``repo``'s store), its
    manifest describing that pack, and ``manifest_changes`` applied."""
    pack = run_git(
        repo, "pack-objects", "--stdout", "-q", data="".join(f"{oid}\n" for oid in ids).encode()
    )
    where.mkdir()
    (where / SEED_PACK_NAME).write_bytes(pack)
    count = struct.unpack(">4sII", pack[:12])[2]
    manifest = dataclasses.replace(
        built.manifest,
        pack_bytes=len(pack),
        pack_objects=count,
        pack_sha256=hashlib.sha256(pack).hexdigest(),
        **manifest_changes,
    )
    (where / SEED_MANIFEST_NAME).write_bytes(manifest.render())
    return where


def _objects_of(built: BuiltSeed, tmp_path: Path, name: str) -> dict[str, str]:
    return pack_objects(stage_seed(built.directory, tmp_path / name))


@pytest.fixture
def checked(tmp_path):
    """A REVIEW seed with a carried commit and tags on, and what its checks read."""
    tagged = tagged_repo(tmp_path)
    review = tagged.review
    run_git(review.repo, "checkout", "-q", "--detach", review.head)
    carried = commit_files(review.repo, "carried", {"fix.txt": b"fixed\n"})
    gi = git_input("FIX", review.head, review.merge_base, carried=carried)
    settings = SeedSettings(history_tags=True)
    built = build(review.repo, gi, settings)
    objects = _objects_of(built, tmp_path, "reference")
    return review, gi, settings, built, objects, carried, tagged


def test_the_seeds_checks_pass_the_seed_as_built(checked, tmp_path):
    review, gi, settings, built, objects, _, _ = checked
    again = _repack(built, tmp_path / "again", review.repo, objects)
    assert check_seed(again, gi, settings, built.selection) == parse_manifest(
        (again / SEED_MANIFEST_NAME).read_bytes()
    )


def test_a_pack_with_a_corrupt_object_fails_index_pack_strict(checked, tmp_path):
    review, gi, settings, built, objects, _, _ = checked
    corrupt = out(
        review.repo,
        "hash-object",
        "-t",
        "commit",
        "--literally",
        "-w",
        "--stdin",
        data=b"no tree here\n",
    )
    bad = _repack(built, tmp_path / "bad", review.repo, [*objects, corrupt])
    with pytest.raises(SandboxLaunchRefusedError, match="index-pack --strict"):
        check_seed(bad, gi, settings, built.selection)


def test_a_pack_missing_a_tree_or_a_blob_above_a_boundary_is_refused(checked, tmp_path):
    review, gi, settings, built, objects, carried, _ = checked
    blob = out(review.repo, "rev-parse", f"{carried}:fix.txt")
    tree = out(review.repo, "rev-parse", f"{review.head}:src")
    for name, missing in (("blob", blob), ("tree", tree)):
        bad = _repack(
            built, tmp_path / name, review.repo, [oid for oid in objects if oid != missing]
        )
        with pytest.raises(SandboxLaunchRefusedError, match="index-pack --strict"):
            check_seed(bad, gi, settings, built.selection)


@pytest.mark.parametrize("which", ["seed commit", "carried commit"])
def test_a_pack_missing_the_seed_or_the_carried_commit_is_refused(checked, tmp_path, which):
    review, gi, settings, built, objects, carried, tagged = checked
    missing = gi.seed if which == "seed commit" else carried
    keep = [oid for oid in objects if oid != missing]
    if which == "seed commit":
        # The carried commit names the seed as its parent and a tag names it:
        # leave both out too, so the pack is still whole and the check, not
        # index-pack, refuses.
        keep = [oid for oid in keep if oid not in (carried, tagged.annotated)]
        gi = dataclasses.replace(gi, carried="")
    bad = _repack(built, tmp_path / "bad", review.repo, keep, carried=gi.carried)
    with pytest.raises(SandboxLaunchRefusedError, match=f"lacks the {which}"):
        check_seed(bad, gi, settings, built.selection)


def test_a_pack_holding_a_commit_d11_does_not_name_is_refused(checked, tmp_path):
    review, gi, settings, built, objects, _, _ = checked
    # A root commit of a tree the pack holds: whole, so index-pack accepts it.
    stray = out(review.repo, "commit-tree", f"{review.head}^{{tree}}", "-m", "stray")
    bad = _repack(built, tmp_path / "bad", review.repo, [*objects, stray])
    with pytest.raises(
        SandboxLaunchRefusedError, match=f"holds the commit {stray}, which D11 does not name"
    ):
        check_seed(bad, gi, settings, built.selection)


def test_a_pack_lacking_a_commit_d11_names_is_refused(checked, tmp_path):
    review, gi, settings, built, objects, _, _ = checked
    selection = dataclasses.replace(
        built.selection, commits=built.selection.commits | {review.main_tip}
    )
    with pytest.raises(SandboxLaunchRefusedError, match=f"lacks the commit {review.main_tip}"):
        check_seed(built.directory, gi, settings, selection)


def test_tag_objects_the_tag_set_does_not_reach_or_misses_are_refused(checked, tmp_path):
    review, gi, settings, built, objects, _, tagged = checked
    stray = out(
        review.repo,
        "mktag",
        data=(
            f"object {review.head}\ntype commit\ntag stray\n"
            "tagger T <t@example.invalid> 0 +0000\n\nstray\n"
        ).encode(),
    )
    extra = _repack(built, tmp_path / "extra", review.repo, [*objects, stray])
    with pytest.raises(SandboxLaunchRefusedError, match="the manifest's tag set does not reach"):
        check_seed(extra, gi, settings, built.selection)
    lacking = _repack(
        built, tmp_path / "lacking", review.repo, [o for o in objects if o != tagged.annotated]
    )
    with pytest.raises(SandboxLaunchRefusedError, match="which the manifest's tag set reaches"):
        check_seed(lacking, gi, settings, built.selection)


def test_a_tag_that_peels_to_no_commit_in_the_pack_is_refused(checked, tmp_path):
    review, gi, settings, built, objects, _, _ = checked
    tree = out(review.repo, "rev-parse", f"{review.head}^{{tree}}")
    to_tree = out(
        review.repo,
        "mktag",
        data=(
            f"object {tree}\ntype tree\ntag to-tree\ntagger T <t@example.invalid> 0 +0000\n\nt\n"
        ).encode(),
    )
    tags = tuple(
        sorted((*built.selection.tags, SeedTag("refs/tags/to-tree", to_tree)), key=lambda t: t.ref)
    )
    selection = dataclasses.replace(
        built.selection, tags=tags, tag_objects=built.selection.tag_objects | {to_tree}
    )
    bad = _repack(built, tmp_path / "bad", review.repo, [*objects, to_tree], tags=tags)
    with pytest.raises(SandboxLaunchRefusedError, match="to-tree peels to no commit in its pack"):
        check_seed(bad, gi, settings, selection)


def test_a_pack_over_its_bounds_is_refused_by_the_check_naming_the_setting(checked):
    _, gi, settings, built, _, _, _ = checked
    for bounded, setting in (
        (dataclasses.replace(settings, max_bytes=built.manifest.pack_bytes - 1), SETTING_MAX_BYTES),
        (
            dataclasses.replace(settings, max_objects=built.manifest.pack_objects - 1),
            SETTING_MAX_OBJECTS,
        ),
    ):
        with pytest.raises(SandboxLaunchRefusedError, match=setting) as refused:
            check_seed(built.directory, gi, bounded, built.selection)
        assert SETTING_TAGS in str(refused.value)


def test_a_manifest_that_disagrees_or_a_directory_holding_more_is_refused(checked, tmp_path):
    review, gi, settings, built, objects, _, _ = checked
    other = dataclasses.replace(gi, launch="L-2")
    with pytest.raises(SandboxLaunchRefusedError, match="names launch"):
        check_seed(built.directory, other, settings, built.selection)
    with pytest.raises(SandboxLaunchRefusedError, match="history settings"):
        check_seed(built.directory, gi, SeedSettings(), built.selection)
    moved = dataclasses.replace(built.selection, boundaries=(review.main_tip,))
    with pytest.raises(SandboxLaunchRefusedError, match="shallow boundaries"):
        check_seed(built.directory, gi, settings, moved)
    copy = tmp_path / "copy"
    shutil.copytree(built.directory, copy)
    (copy / "extra").write_text("x")
    with pytest.raises(SandboxLaunchRefusedError, match="not exactly"):
        check_seed(copy, gi, settings, built.selection)
    (copy / "extra").unlink()
    (copy / SEED_PACK_NAME).rename(copy / "real.pack")
    (copy / SEED_PACK_NAME).symlink_to(copy / "real.pack")
    with pytest.raises(SandboxLaunchRefusedError):
        check_seed(copy, gi, settings, built.selection)


def test_a_pack_whose_bytes_are_not_the_manifests_is_refused(checked, tmp_path):
    _, gi, settings, built, _, _, _ = checked
    copy = tmp_path / "copy"
    shutil.copytree(built.directory, copy)
    data = bytearray((copy / SEED_PACK_NAME).read_bytes())
    data[-30] ^= 0xFF
    (copy / SEED_PACK_NAME).write_bytes(bytes(data))
    with pytest.raises(
        SandboxLaunchRefusedError, match="SHA-256 is not the one its manifest names"
    ):
        check_seed(copy, gi, settings, built.selection)


def test_the_manifest_parses_strictly(checked):
    _, _, _, built, _, _, _ = checked
    raw = built.manifest.render()
    assert parse_manifest(raw) == built.manifest
    data = json.loads(raw)
    for change in (
        {"extra": 1},
        {"issue": "292"},
        {"history_depth": -1},
        {"history_depth": True},
        {"boundaries": ["nope"]},
        {"tags": [{"ref": "refs/heads/main", "object": built.manifest.seed}]},
        {"pack_sha256": "x"},
        {"schema": 2},
    ):
        with pytest.raises(SandboxLaunchRefusedError):
            parse_manifest(json.dumps({**data, **change}).encode())
    with pytest.raises(SandboxLaunchRefusedError):
        parse_manifest(b"\xff")
    assert isinstance(built.manifest, SeedManifest)


def test_select_seed_reads_the_same_selection_the_build_checks(tmp_path):
    review = review_repo(tmp_path, merged=True)
    gi = git_input("REVIEW", review.head, review.merge_base)
    selection = select_seed(common_of(review.repo), gi, SeedSettings(), execute)
    built = build(review.repo, gi)
    assert selection == built.selection


def test_the_settings_refuse_what_is_not_a_depth_tags_or_bound():
    for bad in (
        {"history_depth": -1},
        {"history_depth": "deep"},
        {"history_depth": True},
        {"history_tags": "yes"},
        {"max_bytes": 0},
        {"max_objects": -5},
    ):
        with pytest.raises(ValueError):
            SeedSettings(**bad)  # type: ignore[arg-type]
