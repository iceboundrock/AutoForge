"""The runtime-filesystem contract, as a matrix rather than a list of cases.

`safefs` exists because PR #44's earlier rounds kept finding one more cell of
the same matrix: *entry kind* (regular, symbolic link, hard link, FIFO,
directory, missing) x *path position* (the final component, a parent
component) x *operation* (whole-file write, exclusive create, append, read,
rename, unlink). Each round closed a cell with another condition in another
module.

These tests do not check that a particular exception is raised, because that
is an implementation detail that changed once already and will change again.
They check the guarantee:

    a controller write lands inside the inode opened as the root, on a regular
    file with exactly one name, or it does not happen at all --

by planting an *external sentinel* outside the root and asserting, in every
cell, that it is byte-for-byte and inode-for-inode what it was before.
"""

import errno
import os
import stat

import pytest

from autoforge.errors import StateError
from autoforge.safefs import SafeRoot, UnreadableEntryError, UnsafePathError, entry_kind

SENTINEL = "outside content\n"


class Sentinel:
    """A file outside the root that no operation below it may ever touch."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "outside" / "sentinel.txt"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(SENTINEL, encoding="utf-8")
        st = self.path.stat()
        self.ino, self.dev, self.mtime = st.st_ino, st.st_dev, st.st_mtime_ns

    def assert_untouched(self) -> None:
        st = self.path.stat()
        assert self.path.read_text(encoding="utf-8") == SENTINEL, "content changed"
        assert (st.st_ino, st.st_dev) == (self.ino, self.dev), "replaced by another inode"
        assert st.st_mtime_ns == self.mtime, "written to in place"


# -- the operations under test -------------------------------------------------
# Each takes an open root and a relative path, and is a *controller write* or
# a controller read of its own artifact. Together they are every way this
# module touches the filesystem.
OPERATIONS = {
    "write_bytes": lambda root, rel: root.write_bytes(rel, b"controller\n"),
    "write_text": lambda root, rel: root.write_text(rel, "controller\n"),
    "create_exclusive": lambda root, rel: root.create_exclusive(rel, b"controller\n"),
    "append_text": lambda root, rel: root.append_text(rel, "controller\n"),
    "read_bytes": lambda root, rel: root.read_bytes(rel),
    "read_text": lambda root, rel: root.read_text(rel),
    "unlink": lambda root, rel: root.unlink(rel),
    "rename_from": lambda root, rel: root.rename(rel, "moved.txt"),
    "rename_to": lambda root, rel: root.rename("source.txt", rel),
    "ensure_dir": lambda root, rel: root.ensure_dir(rel),
    "lstat": lambda root, rel: root.lstat(rel),
    "subroot": lambda root, rel: root.subroot(rel, create=True).close(),
}

# Every entry kind the final component can hold, planted so that following it
# (or writing through it) would reach the sentinel.
FINAL_ENTRIES = ("symlink", "hardlink", "fifo", "directory", "regular", "missing")

# Every shape a *parent* component can have. "symlink" is the case that was
# missed for three review rounds: the target's own kind was checked, the
# directory above it was not.
PARENT_SHAPES = ("plain", "symlink", "missing")


def plant_final(root_dir, rel: str, kind: str, sentinel: Sentinel) -> None:
    target = root_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        target.symlink_to(sentinel.path)
    elif kind == "hardlink":
        os.link(sentinel.path, target)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()
    elif kind == "regular":
        target.write_text("controller's own older file\n", encoding="utf-8")
    elif kind == "missing":
        pass
    else:  # pragma: no cover - guard against a typo in the matrix
        raise AssertionError(kind)


@pytest.mark.parametrize("op_name", sorted(OPERATIONS))
@pytest.mark.parametrize("kind", FINAL_ENTRIES)
def test_no_operation_reaches_outside_through_the_final_component(tmp_path, op_name, kind):
    """(entry kind x operation) over the last path component."""
    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (root_dir / "source.txt").write_text("source\n", encoding="utf-8")
    plant_final(root_dir, "artifact.txt", kind, sentinel)

    with SafeRoot.open(root_dir) as root:
        try:
            OPERATIONS[op_name](root, "artifact.txt")
        except (StateError, OSError):
            # A refusal is one acceptable outcome. The other is doing the work
            # somewhere that is not the sentinel. Both are checked below.
            pass
    sentinel.assert_untouched()


@pytest.mark.parametrize("op_name", sorted(OPERATIONS))
@pytest.mark.parametrize("shape", PARENT_SHAPES)
def test_no_operation_reaches_outside_through_a_parent_component(tmp_path, op_name, shape):
    """(parent shape x operation) over an intermediate component."""
    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (root_dir / "source.txt").write_text("source\n", encoding="utf-8")
    if shape == "plain":
        (root_dir / "sub").mkdir()
        (root_dir / "sub" / "artifact.txt").write_text("old\n", encoding="utf-8")
    elif shape == "symlink":
        (root_dir / "sub").symlink_to(sentinel.path.parent)
    elif shape == "missing":
        pass

    with SafeRoot.open(root_dir) as root:
        try:
            OPERATIONS[op_name](root, "sub/artifact.txt")
        except (StateError, OSError):
            pass
    sentinel.assert_untouched()
    if shape == "symlink":
        assert not (sentinel.path.parent / "artifact.txt").exists()
        assert sorted(p.name for p in sentinel.path.parent.iterdir()) == ["sentinel.txt"]


@pytest.mark.parametrize("op_name", sorted(OPERATIONS))
def test_no_operation_escapes_through_a_traversal_component(tmp_path, op_name):
    """`..` is not a name the controller may traverse, in any operation."""
    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (root_dir / "source.txt").write_text("source\n", encoding="utf-8")
    with SafeRoot.open(root_dir) as root:
        for rel in ("../outside/sentinel.txt", "sub/../../outside/sentinel.txt", "/etc/passwd"):
            with pytest.raises(UnsafePathError):
                OPERATIONS[op_name](root, rel)
    sentinel.assert_untouched()


# -- the positive half: the work still happens ---------------------------------
def test_a_write_over_a_planted_entry_still_produces_the_controllers_own_file(tmp_path):
    """Refusing is not the only requirement: the artifact must exist afterwards.

    A boundary that only ever refused would be trivially safe and useless.
    For the kinds that a whole-file write *replaces* rather than refuses, the
    name must end up holding a fresh single-linked regular file.
    """
    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    for kind in ("symlink", "hardlink", "regular", "missing"):
        rel = f"{kind}.txt"
        plant_final(root_dir, rel, kind, sentinel)
        with SafeRoot.open(root_dir) as root:
            root.write_text(rel, "controller\n")
        written = root_dir / rel
        assert not written.is_symlink()
        st = written.lstat()
        assert stat.S_ISREG(st.st_mode)
        assert st.st_nlink == 1
        assert written.read_text(encoding="utf-8") == "controller\n"
    sentinel.assert_untouched()


def test_append_replaces_a_hard_link_instead_of_writing_the_shared_inode(tmp_path):
    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    os.link(sentinel.path, root_dir / "events.jsonl")

    with SafeRoot.open(root_dir) as root:
        root.append_text("events.jsonl", "controller\n")

    sentinel.assert_untouched()
    assert (root_dir / "events.jsonl").read_text(encoding="utf-8") == SENTINEL + "controller\n"
    assert (root_dir / "events.jsonl").stat().st_nlink == 1


def test_a_directory_at_the_target_is_refused_while_a_special_entry_is_replaced(tmp_path):
    """Two different answers, because they are two different facts.

    A whole-file write publishes a *name*: it never opens whatever the name
    currently reaches. So a FIFO, socket or device planted at a controller
    artifact's name is simply replaced by the controller's own file -- nothing
    was read from it, nothing was written through it, and a special file holds
    no data to lose. A directory is refused, because `rename` will not replace
    one and because a directory is an operator's object with contents in it.
    """
    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    plant_final(root_dir, "dir.txt", "directory", sentinel)
    plant_final(root_dir, "fifo.txt", "fifo", sentinel)
    with SafeRoot.open(root_dir) as root:
        with pytest.raises(StateError):
            root.write_text("dir.txt", "controller\n")
        root.write_text("fifo.txt", "controller\n")
    assert (root_dir / "dir.txt").is_dir()
    assert list((root_dir / "dir.txt").iterdir()) == []
    replaced = (root_dir / "fifo.txt").lstat()
    assert stat.S_ISREG(replaced.st_mode) and replaced.st_nlink == 1
    assert (root_dir / "fifo.txt").read_text(encoding="utf-8") == "controller\n"
    sentinel.assert_untouched()


def test_a_fifo_never_blocks_the_controller(tmp_path):
    """O_NONBLOCK: an in-place open of a FIFO with no peer must fail, not hang.

    `read_text` and `append_text` are the operations that must open the entry
    that is there (there is nothing to rename into place), so they are the
    ones a FIFO could stall. A whole-file write never opens it at all.
    """
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    os.mkfifo(root_dir / "pipe")
    with SafeRoot.open(root_dir) as root:
        for op in ("read_text", "append_text"):
            with pytest.raises((StateError, OSError)):
                OPERATIONS[op](root, "pipe")


# -- identity: a root is an inode, not a pathname ------------------------------
def test_a_root_renamed_after_it_was_opened_keeps_receiving_the_writes(tmp_path):
    """The capability is the descriptor. Renaming the directory cannot redirect it."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    with SafeRoot.open(root_dir) as root:
        os.rename(root_dir, tmp_path / "moved")
        os.rename(decoy, root_dir)  # the *name* now points somewhere else
        root.write_text("artifact.txt", "controller\n")
    assert (tmp_path / "moved" / "artifact.txt").read_text(encoding="utf-8") == "controller\n"
    assert not (root_dir / "artifact.txt").exists()


def test_verify_identity_notices_the_root_it_holds_is_no_longer_the_named_one(tmp_path):
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    with SafeRoot.open(root_dir) as root:
        root.verify_identity()
        os.rename(root_dir, tmp_path / "moved")
        (tmp_path / "root").mkdir()
        with pytest.raises(StateError):
            root.verify_identity()


def test_a_subroot_is_a_capability_of_its_own(tmp_path):
    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    with SafeRoot.open(root_dir) as root:
        with root.subroot("logs", create=True) as logs:
            os.rename(root_dir / "logs", tmp_path / "logs-moved")
            root_dir.joinpath("logs").symlink_to(sentinel.path.parent)
            logs.write_text("events.jsonl", "line\n")
    assert (tmp_path / "logs-moved" / "events.jsonl").read_text(encoding="utf-8") == "line\n"
    sentinel.assert_untouched()


# -- the two facts the module reports separately -------------------------------
def test_absence_is_reported_as_absence_not_as_a_refusal(tmp_path):
    """`None` means "not there". It must never be produced by a failed look."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    with SafeRoot.open(root_dir) as root:
        assert root.lstat("nothing.txt") is None
        assert root.read_bytes("nothing.txt") is None
        assert root.read_text("nothing.txt") is None
        root.unlink("nothing.txt")  # idempotent


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_unreadable_is_its_own_fact_and_carries_the_path(tmp_path):
    """EACCES is not "absent" and not "unsafe": it is an access fact, typed."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (root_dir / "closed.txt").write_text("x\n", encoding="utf-8")
    (root_dir / "closed.txt").chmod(0o000)
    (root_dir / "shut").mkdir()
    (root_dir / "shut" / "inner.txt").write_text("x\n", encoding="utf-8")
    (root_dir / "shut").chmod(0o000)
    try:
        with SafeRoot.open(root_dir) as root:
            with pytest.raises(UnreadableEntryError) as file_exc:
                root.read_bytes("closed.txt")
            assert file_exc.value.path.endswith("closed.txt")
            with pytest.raises(UnreadableEntryError) as dir_exc:
                list(root.walk())
            assert dir_exc.value.path == "shut"
    finally:
        (root_dir / "closed.txt").chmod(0o600)
        (root_dir / "shut").chmod(0o700)


def test_the_root_pathname_is_resolved_normally_and_that_is_the_stated_limit(tmp_path):
    """The one thing descriptor-relative addressing cannot cover: its own start.

    `SafeRoot.open` resolves an ordinary pathname, symbolic links included --
    an operator whose checkout or state directory *is* a symlink is doing
    something normal, and refusing it would be theatre, since a process that
    can redirect that pathname can redirect the checkout itself. The module
    docstring says so; this test pins the behaviour so it is a decision rather
    than an accident, and pins the part that *is* enforced: everything below.
    """
    sentinel = Sentinel(tmp_path)
    with pytest.raises(FileNotFoundError):
        SafeRoot.open(tmp_path / "nope", create=False)

    real = tmp_path / "real-root"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with SafeRoot.open(tmp_path / "link", create=False) as root:
        root.write_text("artifact.txt", "controller\n")
    assert (real / "artifact.txt").read_text(encoding="utf-8") == "controller\n"

    # A root that is not a directory at all is still refused.
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(UnsafePathError):
        SafeRoot.open(tmp_path / "file", create=False)
    sentinel.assert_untouched()


# -- kind reporting ------------------------------------------------------------
def test_entry_kind_names_every_kind_it_can_be_handed(tmp_path):
    """The classification is total: no mode falls through to "I do not know"."""
    assert entry_kind(stat.S_IFREG | 0o644) is None
    for bits, name in (
        (stat.S_IFDIR, "directory"),
        (stat.S_IFIFO, "FIFO"),
        (stat.S_IFSOCK, "socket"),
        (stat.S_IFCHR, "character device"),
        (stat.S_IFBLK, "block device"),
        (stat.S_IFLNK, "symbolic link"),
    ):
        assert entry_kind(bits | 0o644) == name
    assert entry_kind(0o644) == "special file"


def test_a_symlinked_directory_component_reports_the_link_not_enotdir(tmp_path):
    """Linux answers O_DIRECTORY|O_NOFOLLOW on a link with ENOTDIR, not ELOOP.

    Reporting that verbatim would tell the operator their directory "is not a
    directory", which is both confusing and wrong; the entry is re-inspected
    so the message names what is actually there.
    """
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (root_dir / "sub").symlink_to(tmp_path / "elsewhere")
    with SafeRoot.open(root_dir) as root:
        with pytest.raises(UnsafePathError, match="symbolic link"):
            root.write_text("sub/artifact.txt", "x")


def test_errno_constants_used_by_the_walk_exist(tmp_path):
    """A typo'd errno name would silently turn a refusal into a generic error."""
    for name in ("ELOOP", "ENOTDIR", "EACCES", "EPERM", "EISDIR", "ENXIO", "EEXIST"):
        assert isinstance(getattr(errno, name), int)
