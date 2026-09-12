"""Phase 11: attack the finished design, not the bugs it was built from.

Every scenario here was written by asking "what would the next reviewer try
that nobody has tried yet?" — a combination of symbolic link, hard link, git
flag, decoy name, in-place edit or config change that no previous round
listed. The design's claim is that each one is already handled by an existing
invariant rather than by a condition somebody remembered to add, so each test
states the invariant it is attacking before it attacks it.

A scenario that needed a new `if` in production code to pass would mean the
architecture is not finished. None of these did.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from autoforge.config import default_config
from autoforge.errors import ConfigurationError, StateError, VerificationError
from autoforge.local_workspace import KIND_EXCLUDED, LocalWorkspace, init_feature_file
from autoforge.safefs import SafeRoot, UnsafePathError
from autoforge.state import Phase

from .conftest import commit_all, write_feature


def repo(tmp_path, name: str = "repo") -> Path:
    root = Path(tmp_path) / name
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    write_feature(root)
    commit_all(root, "base")
    return root


# -- 1 -------------------------------------------------------------------------
def test_a_decoy_git_directory_does_not_buy_an_exclusion(tmp_path):
    """Invariant W. Exclusion is by inode; the name `.git` buys nothing.

    The attack: the gitdir is the one region excluded without the operator
    asking, so an agent creates its own `vendor/.git` and hides its edits
    under it. If exclusion were matched by name — which is how the first
    design excluded the state directory — the hidden tree would be unbound
    and a clean review would still be accepted.
    """
    root = repo(tmp_path)
    (root / "vendor").mkdir()
    (root / "vendor" / ".git").mkdir()
    (root / "vendor" / ".git" / "payload").write_text("x\n", encoding="utf-8")

    with pytest.raises(VerificationError) as exc:
        LocalWorkspace(workdir=root).snapshot()
    assert "nested git repository" in str(exc.value)
    assert "vendor" in str(exc.value)


# -- 2 -------------------------------------------------------------------------
def test_a_symlink_named_git_is_refused_rather_than_treated_as_the_gitdir(tmp_path):
    """Invariant W. The gitdir is `(st_dev, st_ino)`; a link to it is a link.

    The attack is the mirror of the previous one: instead of a decoy that
    should not be excluded, a real gitdir reached under a second name, hoping
    the walk descends into `.git` through the link and hashes the repository's
    own churn into the fingerprint (which would make every snapshot differ
    from every other and every review stale).
    """
    root = repo(tmp_path)
    (root / "src" / ".git").symlink_to(root / ".git")
    with pytest.raises(VerificationError) as exc:
        LocalWorkspace(workdir=root).snapshot()
    assert "src/.git" in str(exc.value)


# -- 3 -------------------------------------------------------------------------
def test_restoring_size_and_mtime_does_not_restore_the_fingerprint(tmp_path):
    """Invariant W. The digest cache is keyed by identity, not by mtime alone.

    The attack: hashing every file on every phase boundary is expensive, so
    the walk memoises by `(dev, ino, size, mtime_ns, ctime_ns)`. An agent
    edits a file in place to the same length and puts the mtime back with
    `os.utime` — the classic tar-bomb trick against mtime-based caches. It
    cannot put `ctime` back without being root, and the bytes are re-read.
    """
    root = repo(tmp_path)
    ws = LocalWorkspace(workdir=root)
    target = root / "src" / "app.py"
    before = ws.snapshot()
    stamp = os.stat(target)

    target.write_text("VALUE = 9\n", encoding="utf-8")  # identical length
    os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert os.stat(target).st_size == stamp.st_size
    assert os.stat(target).st_mtime_ns == stamp.st_mtime_ns

    after = ws.snapshot()
    assert after.fingerprint != before.fingerprint, (
        "the bytes changed and the fingerprint did not: a restored mtime bought a clean review"
    )


# -- 4 -------------------------------------------------------------------------
def test_widening_the_exclusions_mid_run_invalidates_the_review(tmp_path):
    """Invariant W + V. The rules are hashed, so loosening them is a change.

    The attack: the operator's config is an ordinary file under the same UID,
    so an agent that wants its work unreviewed does not have to hide a file —
    it can add the file's directory to `local.exclude`. If the exclusion rules
    were applied to the tree but not hashed into the identity, the fingerprint
    would be unchanged and the review would still be binding while covering
    strictly less.
    """
    root = repo(tmp_path)
    (root / "build").mkdir()
    (root / "build" / "out.o").write_text("compiled\n", encoding="utf-8")

    reviewed = LocalWorkspace(workdir=root).snapshot()
    loosened = LocalWorkspace(workdir=root, exclude=("build",)).snapshot()

    assert loosened.fingerprint != reviewed.fingerprint
    assert "exclude:build" in loosened.describe_exclusions()
    assert "exclude:" not in reviewed.describe_exclusions()


# -- 5 -------------------------------------------------------------------------
def test_a_file_and_a_directory_of_the_same_name_are_not_interchangeable(tmp_path):
    """Invariant W. The kind is part of the record, not implied by the digest.

    The attack: replace a directory with a regular file whose contents are
    chosen so the concatenated record happens to look the same — possible only
    if the record were "path + digest" with an empty digest for directories.
    """
    a = repo(tmp_path, "a")
    b = repo(tmp_path, "b")
    (a / "thing").mkdir()
    (b / "thing").write_text("", encoding="utf-8")
    os.chmod(b / "thing", 0o755)

    fa = LocalWorkspace(workdir=a).snapshot()
    fb = LocalWorkspace(workdir=b).snapshot()
    kinds = {e.path: e.kind for e in fa.entries} | {e.path: e.kind for e in fb.entries}
    assert kinds  # both trees classified something
    assert fa.fingerprint != fb.fingerprint, "a directory and an empty file hashed alike"


# -- 6 -------------------------------------------------------------------------
def test_a_hard_link_to_a_file_outside_the_tree_is_bound_by_its_contents(tmp_path):
    """Invariant W. A hard link has no second name to check — only bytes.

    The attack: a symbolic link out of the tree is refused because its target
    could change with the fingerprint standing still. A *hard* link has no
    target text to inspect and `lstat` says "regular file", so nothing
    distinguishes it. It does not need to: the bytes are hashed, so editing
    the file through its outside name moves the fingerprint exactly as editing
    it through the inside name would.
    """
    root = repo(tmp_path)
    outside = Path(tmp_path) / "outside.txt"
    outside.write_text("one\n", encoding="utf-8")
    os.link(outside, root / "src" / "linked.txt")

    ws = LocalWorkspace(workdir=root)
    before = ws.snapshot()
    outside.write_text("two\n", encoding="utf-8")
    assert ws.snapshot().fingerprint != before.fingerprint


# -- 7 -------------------------------------------------------------------------
def test_a_symlink_cycle_does_not_hang_or_recurse_the_walk(tmp_path):
    """Invariant W. The walk descends through descriptors, never through links.

    The attack: `a -> b`, `b -> a`, plus a link to the tree root itself. Any
    walk that resolved a link to decide whether to descend would either loop
    forever or blow the stack; either is a denial of service before the first
    agent runs.
    """
    root = repo(tmp_path)
    (root / "a").symlink_to("b")
    (root / "b").symlink_to("a")
    (root / "self").symlink_to(root)

    # The link to the root is refused (R9-F1: it reaches the git directory,
    # and every other exclusion, at an unexcluded path) -- but refused by a
    # check that ran *after* a complete, finite walk, not by a walk that
    # descended into it.
    with pytest.raises(VerificationError, match="self is a symbolic link to the working tree"):
        LocalWorkspace(workdir=root).snapshot()
    (root / "self").unlink()

    snap = LocalWorkspace(workdir=root).snapshot()
    paths = {e.path for e in snap.entries}
    assert {"a", "b"} <= paths
    assert not any(p.startswith(("a/", "b/")) for p in paths), "the walk followed a link"


# -- 8 -------------------------------------------------------------------------
def test_retargeting_a_link_moves_the_fingerprint_even_with_identical_contents(tmp_path):
    """Invariant W. A link is bound by its text, so where it points is bound.

    The attack: two files with identical bytes, and a link switched from one
    to the other. A design that hashed what a link *resolves to* would see no
    change at all; the reviewer, following the link, would be reading a
    different file than the one they reviewed.
    """
    root = repo(tmp_path)
    (root / "one.txt").write_text("same\n", encoding="utf-8")
    (root / "two.txt").write_text("same\n", encoding="utf-8")
    (root / "current").symlink_to("one.txt")

    ws = LocalWorkspace(workdir=root)
    before = ws.snapshot()
    (root / "current").unlink()
    (root / "current").symlink_to("two.txt")
    assert ws.snapshot().fingerprint != before.fingerprint


# -- 9 -------------------------------------------------------------------------
def test_replacing_a_directory_with_a_link_to_it_is_a_change_not_a_no_op(tmp_path):
    """Invariant W. Kind, mode and link text are all in the record.

    The attack: move `src` aside, then put a symbolic link at `src` pointing
    at the moved copy. The reviewer sees exactly the same paths with exactly
    the same bytes — but the bytes now live somewhere the snapshot binds
    differently, and a later `mv` swaps them all at once.
    """
    root = repo(tmp_path)
    ws = LocalWorkspace(workdir=root)
    before = ws.snapshot()

    os.rename(root / "src", root / "src_real")
    (root / "src").symlink_to("src_real")

    after = ws.snapshot()
    assert after.fingerprint != before.fingerprint
    kinds = {e.path: e.kind for e in after.entries}
    assert kinds["src"] == "link"


# -- 10 ------------------------------------------------------------------------
def test_an_excluded_region_is_never_read_and_never_hides_a_link_out_of_it(tmp_path):
    """Invariant W. An exclusion covers a region; a link *into* it is refused.

    The attack: exclude `build` (as any operator with a large artefact tree
    would), then put `src/generated` -> `../build/out` so reviewed-looking code
    actually lives in the unbound region. The exclusion must not become a
    laundering route for bytes inside the reviewed part of the tree.
    """
    root = repo(tmp_path)
    (root / "build").mkdir()
    (root / "build" / "out").write_text("payload\n", encoding="utf-8")
    (root / "src" / "generated").symlink_to("../build/out")

    with pytest.raises(VerificationError) as exc:
        LocalWorkspace(workdir=root, exclude=("build",)).snapshot()
    assert "src/generated" in str(exc.value)
    assert "exclude" in str(exc.value)

    # Without the link, the exclusion itself is honoured and disclosed.
    (root / "src" / "generated").unlink()
    snap = LocalWorkspace(workdir=root, exclude=("build",)).snapshot()
    excluded = [e.path for e in snap.entries if e.kind == KIND_EXCLUDED]
    assert "build" in excluded
    assert not any(e.path.startswith("build/") for e in snap.entries), "descended into it anyway"


# -- 11 ------------------------------------------------------------------------
def test_local_init_force_never_writes_through_an_operators_entry(tmp_path):
    """Invariant R. `--force` replaces a name AutoForge could have written.

    The attack is R5-F4 generalised past hard links: plant *each* kind of
    entry at `features/<slug>.md` and check the external sentinel afterwards.
    A link is refused (it is not a regular file); a hard link is replaced, so
    the linked inode keeps its bytes and its identity. In neither case does a
    single byte reach outside the checkout.
    """
    root = repo(tmp_path)
    sentinel = Path(tmp_path) / "precious.txt"
    sentinel.write_text("do not lose me\n", encoding="utf-8")
    before = sentinel.stat()
    ws = LocalWorkspace(workdir=root)
    spec = root / "features" / "new-thing.md"

    spec.symlink_to(sentinel)
    with pytest.raises(ConfigurationError, match="symbolic link"):
        init_feature_file(ws, "new-thing", overwrite=True)
    assert sentinel.read_text(encoding="utf-8") == "do not lose me\n"

    spec.unlink()
    os.link(sentinel, spec)
    init_feature_file(ws, "new-thing", overwrite=True)
    assert sentinel.read_text(encoding="utf-8") == "do not lose me\n"
    assert sentinel.stat().st_ino == before.st_ino
    assert spec.stat().st_ino != before.st_ino
    assert spec.read_text(encoding="utf-8").startswith("# Feature:")


# -- 12 ------------------------------------------------------------------------
def test_a_directory_planted_at_an_artifact_name_is_refused_not_merged(tmp_path):
    """Invariant R. A directory is the one shape `rename` cannot replace.

    The attack: everything else planted at a controller artefact's name gets
    replaced, so try the one that cannot be. The requirement is a clean
    refusal that names the path — not an `IsADirectoryError` escaping from the
    middle of a write, and not a partially written artefact.
    """
    with SafeRoot.open(tmp_path) as root:
        os.mkdir(Path(tmp_path) / "report.json")
        with pytest.raises(Exception) as exc:
            root.write_text("report.json", "{}")
        assert "report.json" in str(exc.value)
        assert (Path(tmp_path) / "report.json").is_dir()


# -- 13 ------------------------------------------------------------------------
def test_the_state_directory_cannot_be_placed_inside_the_reviewed_tree(tmp_path):
    """Invariant W. The excluded set is not extensible from the command line.

    The attack: the whole class of "runtime artefacts hide implementation
    changes" findings came from state living inside the tree. `--state-dir`
    still exists, so try to put it back — inside the tree, and through a
    symbolic link that resolves inside the tree.
    """
    root = repo(tmp_path)
    ws = LocalWorkspace(workdir=root)
    with pytest.raises(ConfigurationError, match="inside the LOCAL working tree"):
        ws.check_state_dir_location(root / ".autoforge")

    link = Path(tmp_path) / "elsewhere"
    link.symlink_to(root / "runtime")
    with pytest.raises(ConfigurationError, match="inside the LOCAL working tree"):
        ws.check_state_dir_location(link)


# -- 14 ------------------------------------------------------------------------
def test_the_cost_bound_cannot_be_reached_by_a_link_farm(tmp_path):
    """Invariant W. The bound counts entries, and a refusal names the culprit.

    The attack: make the tree expensive rather than unbindable. If the bound
    were on bytes only, a million empty files or symbolic links would cost
    nothing to `max_workspace_bytes` and still take unbounded time and memory.
    """
    root = repo(tmp_path)
    farm = root / "farm"
    farm.mkdir()
    for i in range(40):
        (farm / f"l{i}").symlink_to("../src/app.py")

    with pytest.raises(VerificationError) as exc:
        LocalWorkspace(workdir=root, max_entries=20).snapshot()
    message = str(exc.value)
    assert "more than 20 entries" in message
    assert "local.max_workspace_entries" in message
    assert "farm" in message


# -- 15 ------------------------------------------------------------------------
def test_git_cannot_be_used_to_hide_a_file_from_the_snapshot(tmp_path):
    """Invariant W. Git is not asked, so no git mechanism can answer for it.

    The attack combines every git-side hiding mechanism the review rounds
    found separately, on one file at once: ignored, `assume-unchanged`,
    `skip-worktree`, and `core.fileMode=false` over an executable bit. If any
    one of them still reached the enumerator, the fingerprint would not move.
    """
    root = repo(tmp_path)
    (root / ".gitignore").write_text("hidden.py\n", encoding="utf-8")
    (root / "hidden.py").write_text("SECRET = 1\n", encoding="utf-8")
    tracked = root / "src" / "app.py"
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    commit_all(root, "add ignore rule")
    for flag in ("--assume-unchanged", "--skip-worktree"):
        subprocess.run(["git", "-C", str(root), "update-index", flag, "src/app.py"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "core.fileMode", "false"], check=True)

    ws = LocalWorkspace(workdir=root)
    before = ws.snapshot()
    assert (
        subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == ""
    ), "git itself reports nothing: that is the point"

    (root / "hidden.py").write_text("SECRET = 2\n", encoding="utf-8")
    after_ignored = ws.snapshot()
    assert after_ignored.fingerprint != before.fingerprint

    tracked.write_text("VALUE = 2\n", encoding="utf-8")
    after_flagged = ws.snapshot()
    assert after_flagged.fingerprint != after_ignored.fingerprint

    os.chmod(tracked, 0o755)
    assert ws.snapshot().fingerprint != after_flagged.fingerprint


# -- 16 ------------------------------------------------------------------------
def test_the_review_binding_is_recomputed_and_never_read_back_from_state(tmp_path):
    """Invariant V. The controller compares its own snapshot, not a claim.

    The attack: the reviewer returns the fingerprint it was handed (it must,
    to prove which tree it read) — so the temptation is to trust it. Here the
    agent returns a *correct-looking* fingerprint for a tree it has already
    changed. The controller must fail, because the fingerprint it compares
    against is one it computes itself at that moment.
    """
    from .conftest import make_local_engine
    from .test_local import IMPL_FILE, impl_result, local_repo, review_result, scripted

    root = local_repo(tmp_path)
    eng = make_local_engine(root, root / "features" / "add-filter.md", cfg=default_config())

    def implement(r):
        (r / IMPL_FILE).write_text("def main():\n    return 1\n", encoding="utf-8")

    def review_after_editing(r):
        # The reviewer reads the tree, then edits it before answering.
        (r / IMPL_FILE).write_text("def main():\n    return 2\n", encoding="utf-8")

    eng.provider._handler = scripted(
        eng,
        root,
        [
            (implement, lambda e: impl_result()),
            (review_after_editing, lambda e: review_result(e.state.workspace_fingerprint)),
        ],
    )
    with pytest.raises(VerificationError, match="fingerprint"):
        eng.run(max_steps=6, dry_run=False, allow_merge=False)


# -- 17 ------------------------------------------------------------------------
def test_an_agent_replacing_the_state_directory_mid_run_redirects_nothing(tmp_path):
    """Invariant R. The run holds the state directory as a descriptor, not a path.

    R5-F3's concrete reproduction: an agent replaces the state directory with
    a symbolic link to somewhere outside the checkout, between its own phase
    and the controller's next write, so that the run's logs and `state.json`
    are created under the external directory. `ensure_directory` used to
    validate only the final component, so the `mkdir` followed the link.

    Two things now stop it independently: the state directory no longer lives
    inside the reviewed tree at all, and the controller opened it once as a
    capability -- so even a replacement performed between two writes reaches
    nothing, because no pathname is resolved again.
    """
    from .conftest import make_local_engine
    from .test_local import IMPL_FILE, impl_result, local_repo, review_result, scripted

    root = local_repo(tmp_path)
    outside = Path(tmp_path) / "outside"
    outside.mkdir()
    eng = make_local_engine(root, root / "features" / "add-filter.md", cfg=default_config())
    state_dir = Path(eng.paths.state_dir)
    # Outside the *reviewed* tree: inside the git directory, which the snapshot
    # excludes by inode and which the walk therefore never enters.
    assert _is_inside(state_dir, root / ".git"), "state must not live in the reviewed tree"

    def implement_then_redirect(r):
        (r / IMPL_FILE).write_text("def main():\n    return 1\n", encoding="utf-8")
        # The agent's phase is over; the controller has yet to log it.
        shutil.rmtree(state_dir)
        state_dir.symlink_to(outside)

    eng.provider._handler = scripted(
        eng,
        root,
        [
            (implement_then_redirect, lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint)),
        ],
    )
    with pytest.raises(UnsafePathError, match="symbolic link"):
        eng.run(max_steps=6, dry_run=False, allow_merge=False)

    assert list(outside.iterdir()) == [], "controller artefacts landed outside the checkout"


def _is_inside(path: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
    except ValueError:
        return False
    return True


# -- 18 ------------------------------------------------------------------------
def test_a_state_directory_replaced_by_an_ordinary_directory_redirects_nothing(tmp_path):
    """Invariant R, again: a link is not the only way to re-point a name.

    R6-F1. Attack 17 is defeated by `O_NOFOLLOW`, which is a statement about
    *symbolic links* — so the same attack with a prepared ordinary directory
    moved into the name passed every check: it is a real directory, it is not
    a link, and `open_root()` resolved the pathname afresh for each write.
    The controller's next checkpoint and its logs landed in an inode the
    controller never created, which a `mount --bind` or a rename can put
    anywhere on the machine.

    The fix is the one the descriptor was always standing in for: the state
    root's pathname is bound to its *inode* at first open, and every later
    open must still reach that inode.
    """
    from .conftest import make_local_engine
    from .test_local import IMPL_FILE, impl_result, local_repo, review_result, scripted

    # The repository is a subdirectory so the planted directory is genuinely
    # outside the reviewed tree rather than merely somewhere the walk ignores.
    root = local_repo(tmp_path / "repo")
    # A prepared ordinary directory somewhere else entirely, with a sentinel
    # so "nothing reached it" is checked as bytes, not as an absent exception.
    planted = Path(tmp_path) / "planted"
    planted.mkdir()
    sentinel = planted / "notes.txt"
    sentinel.write_text("operator's own file\n", encoding="utf-8")
    before = sentinel.stat()

    eng = make_local_engine(root, root / "features" / "add-filter.md", cfg=default_config())
    state_dir = Path(eng.paths.state_dir)

    def implement_then_swap(r):
        (r / IMPL_FILE).write_text("def main():\n    return 1\n", encoding="utf-8")
        # The agent's phase is over; the controller has yet to log it.
        shutil.rmtree(state_dir)
        os.rename(planted, state_dir)

    eng.provider._handler = scripted(
        eng,
        root,
        [
            (implement_then_swap, lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint)),
        ],
    )
    with pytest.raises(StateError, match="state directory .* replaced"):
        eng.run(max_steps=6, dry_run=False, allow_merge=False)

    # Nothing of the controller's reached the replacement, and the operator's
    # file inside it -- now reachable at the state path, which is the point --
    # is byte-, inode- and mtime-identical to the one prepared elsewhere.
    assert sorted(p.name for p in state_dir.iterdir()) == ["notes.txt"]
    moved = state_dir / "notes.txt"
    after = moved.stat()
    assert moved.read_text(encoding="utf-8") == "operator's own file\n"
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
    )


# -- 19 ------------------------------------------------------------------------
def test_a_link_to_a_linked_worktrees_git_pointer_is_refused_before_and_after_binding(tmp_path):
    """Invariant W in a linked worktree. R7-F2.

    In a linked worktree the top-level `.git` is a *regular file* whose one
    line names the real git directory. The walk excludes it (it is git's own
    metadata), and excluded means *unbound*: its bytes are not in the
    fingerprint. A reviewable link that resolves to it therefore exposes
    bytes that can change -- re-pointed at another worktree's gitdir with the
    same detached HEAD -- while the link text, and so the fingerprint, stands
    still. The previous link-target check knew about git *directories* by
    inode and let the pointer *file* through.

    Two moments, because the initial binding and the re-binding after an
    agent phase are different code paths with the same predicate: the link
    exists before the run starts (refused at the first snapshot), and the
    implementing agent creates it (refused at the snapshot that would have
    bound its work for the reviewer).
    """
    from .conftest import make_local_engine
    from .test_local import IMPL_FILE, impl_result, local_repo, review_result, scripted

    main = local_repo(tmp_path / "main")
    linked = Path(tmp_path) / "linked"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(linked)], check=True)
    assert (linked / ".git").is_file(), "not a linked worktree"

    # Before binding: the snapshot refuses the link outright.
    (linked / "src" / "peek").symlink_to("../.git")
    with pytest.raises(VerificationError) as exc:
        LocalWorkspace(workdir=linked).snapshot()
    assert "src/peek" in str(exc.value) and "gitdir" in str(exc.value)
    (linked / "src" / "peek").unlink()

    # After binding: the run starts on a clean tree, the agent plants the link
    # with its implementation, and the controller refuses to bind the result.
    eng = make_local_engine(linked, linked / "features" / "add-filter.md", cfg=default_config())

    def implement_and_plant(r):
        (r / IMPL_FILE).write_text("def main():\n    return 1\n", encoding="utf-8")
        (r / "src" / "peek").symlink_to("../.git")

    eng.provider._handler = scripted(
        eng,
        linked,
        [
            (implement_and_plant, lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint)),
        ],
    )
    with pytest.raises(VerificationError) as exc:
        eng.run(max_steps=6, dry_run=False, allow_merge=False)
    assert "src/peek" in str(exc.value) and "gitdir" in str(exc.value)
    assert eng.state.phase != Phase.REVIEW, "the planted tree was bound for review"


# -- 20 ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "link,target,reaches",
    [
        ("src/escape", "..", ".git"),  # the root: contains every exclusion
        ("src/escape", "../build", "build"),  # the excluded directory itself
        ("src/escape", "../build/out", "build"),  # a file inside it
        ("src/escape", "../vendor", "vendor/lib/gen"),  # an *ancestor* of a nested one
        ("src/escape", "../vendor/lib", "vendor/lib/gen"),  # its parent
        ("escape", "src", "src/__pycache__"),  # a sibling directory holding one
        ("src/escape", "../.git", "gitdir"),  # the git directory
        ("src/escape/deep", "../..", ".git"),  # the root, from deeper down
    ],
    ids=[
        "root",
        "the-exclusion",
        "inside-the-exclusion",
        "grandparent-of-a-nested-exclusion",
        "parent-of-a-nested-exclusion",
        "sibling-directory",
        "gitdir",
        "root-from-deeper",
    ],
)
def test_a_directory_link_cannot_reach_excluded_bytes_at_an_unexcluded_path(
    tmp_path, link, target, reaches
):
    """Invariant W (R9-F1). A link may not *contain* an exclusion either.

    The attack: `local.exclude: [build]`, `build/out` present, and
    `src/escape -> ..`. The link's target is the root, which no rule
    excludes, so the old check accepted it; but `src/escape/build/out`
    dereferences to `build/out`, whose bytes no entry binds. Changing
    `build/out` left the fingerprint still while a reviewer read the new
    bytes through the link. The same holds for a link to any directory whose
    subtree contains an exclusion, and a link to the root contains them all
    (the git directory included), so it is refused by the same rule and not
    by a special case.
    """
    root = repo(tmp_path)
    (root / "build").mkdir()
    (root / "build" / "out").write_text("one\n", encoding="utf-8")
    (root / "vendor" / "lib" / "gen").mkdir(parents=True)
    (root / "vendor" / "lib" / "gen" / "x.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "app.pyc").write_bytes(b"\x00")
    exclude = ("build", "vendor/lib/gen", "**/__pycache__")

    (root / link).parent.mkdir(parents=True, exist_ok=True)
    (root / link).symlink_to(target)
    with pytest.raises(VerificationError) as exc:
        LocalWorkspace(workdir=root, exclude=exclude).snapshot()
    message = str(exc.value)
    assert f"{link} is a symbolic link" in message, message
    assert reaches in message, message


def test_a_directory_link_below_every_exclusion_is_still_bound(tmp_path):
    """Invariant W (R9-F1). The reach rule refuses only what it must.

    A link to a directory that contains no exclusion is a link to bytes the
    snapshot binds in full, so it is accepted; and because an exclusion's
    *path* enters the fingerprint, one appearing under the target later
    moves the fingerprint *and* makes the next snapshot refuse the link --
    the reviewer can never read unbound bytes through it unnoticed.
    """
    root = repo(tmp_path)
    (root / "build").mkdir()
    (root / "build" / "out").write_text("one\n", encoding="utf-8")
    (root / "lib").mkdir()
    (root / "lib" / "util.py").write_text("U = 1\n", encoding="utf-8")
    (root / "src" / "shared").symlink_to("../lib")
    ws = LocalWorkspace(workdir=root, exclude=("build",))

    bound = ws.snapshot()
    assert "src/shared" in {e.path for e in bound.entries}
    # Bytes reachable through the link are bound at their own path.
    (root / "lib" / "util.py").write_text("U = 2\n", encoding="utf-8")
    assert ws.snapshot().fingerprint != bound.fingerprint

    # A `build` appearing *under* the link's target: the excluded entry's
    # path moves the fingerprint, and the link is refused from then on.
    (root / "lib" / "build").mkdir()
    (root / "lib" / "build" / "gen").write_text("g\n", encoding="utf-8")
    ws_nested = LocalWorkspace(workdir=root, exclude=("**/build",))
    with pytest.raises(VerificationError, match="src/shared is a symbolic link to lib"):
        ws_nested.snapshot()
