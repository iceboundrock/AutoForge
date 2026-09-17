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

    a controller write lands inside the inode opened as the root, on an inode
    the controller created or opened as a single-named regular file, and a
    name is published only over an inode found single-named after the write,
    or it does not happen at all --

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


def test_append_refuses_a_hard_link_instead_of_writing_the_shared_inode(tmp_path):
    """#51 restored the in-place append, so a hard link at the journal's name
    is refused on the descriptor -- the inode has two names, and appending
    through it would extend a file the controller did not create -- and
    nothing is written anywhere: not to the shared inode, not to a fresh
    one. The name keeps both its content and its identity for the operator
    to look at, as the refusal tells them to."""
    from autoforge.safefs import UnsafePathError

    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    os.link(sentinel.path, root_dir / "events.jsonl")
    before = (root_dir / "events.jsonl").stat()

    with SafeRoot.open(root_dir) as root:
        with pytest.raises(UnsafePathError, match="hard link: 2 directory entries"):
            root.append_text("events.jsonl", "controller\n")
        with pytest.raises(UnsafePathError, match="hard link: 2 directory entries"):
            root.verify_appendable("events.jsonl")

    sentinel.assert_untouched()
    after = (root_dir / "events.jsonl").stat()
    assert (after.st_ino, after.st_nlink, after.st_size) == (before.st_ino, 2, before.st_size)
    assert sorted(p.name for p in root_dir.iterdir()) == ["events.jsonl"], "no temporary left"


def test_a_temporary_hard_linked_out_during_the_write_is_never_published(tmp_path, monkeypatch):
    """R10-F2: the create-then-write window on the *named* temporary.

    ``_durable_tmp_at`` creates ``.af-tmp-*`` with ``O_EXCL``, which proves the
    inode is the controller's own -- but it has a name, and a same-user
    process can ``link(2)`` that name outside the root before the bytes are
    written. The race is made deterministic by planting the extra link inside
    the create call itself, so it exists before the first byte is written.

    What must hold: the write is refused, nothing is published under the
    final name, the temporary is gone, and the link the other process holds
    ends up as an *empty* file -- the controller's bytes were emptied through
    the descriptor they were written through, so the outside name keeps no
    copy of a state file it did not own.
    """
    import autoforge.safefs as safefs

    root_dir = tmp_path / "root"
    root_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    planted = outside / "stolen-tmp"
    real_open = safefs.open_regular_at

    def open_and_hard_link_the_temporary(parent, name, flags, **kwargs):
        fd = real_open(parent, name, flags, **kwargs)
        if name.startswith(".af-tmp-"):
            os.link(name, planted, src_dir_fd=parent)
        return fd

    monkeypatch.setattr(safefs, "open_regular_at", open_and_hard_link_the_temporary)

    with SafeRoot.open(root_dir) as root:
        with pytest.raises(UnsafePathError, match="linked the controller's temporary"):
            root.write_bytes("state.json", b"controller secret state\n")

    assert not (root_dir / "state.json").exists(), "the write was published anyway"
    assert [e.name for e in root_dir.iterdir()] == [], "the temporary was left behind"
    assert planted.exists(), "the test did not model the race"
    assert planted.stat().st_nlink == 1, "the controller's own name still exists"
    assert planted.read_bytes() == b"", "the controller's bytes reached the planted name"


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


# -- #55: the append reads the file, so the read is bounded like any other ------
def _reads_asked(monkeypatch) -> list[int]:
    """Every ``read(n)`` a ``SafeRoot`` file object is asked for."""
    import autoforge.safefs as safefs

    asked: list[int] = []
    real_fdopen = safefs.os.fdopen

    def counted(fd, *args, **kwargs):
        fh = real_fdopen(fd, *args, **kwargs)
        real_read = fh.read

        def read(n=-1):
            asked.append(n)
            return real_read(n)

        fh.read = read  # type: ignore[method-assign]
        return fh

    monkeypatch.setattr(safefs.os, "fdopen", counted)
    return asked


def test_a_bounded_append_refuses_an_oversized_file_without_reading_or_touching_it(
    tmp_path, monkeypatch
):
    """`append_text` writes in place through the opened descriptor, so the
    file's size is a single `fstat` and nothing is ever read. With a limit
    the refusal lands before anything is written: the oversized file keeps
    its name, its inode and its size, and no temporary is created."""
    from autoforge.safefs import ReadLimitExceeded

    root_dir = tmp_path / "root"
    root_dir.mkdir()
    target = root_dir / "events.jsonl"
    target.write_bytes(b"x" * 10)
    os.truncate(target, 1 << 20)  # sparse: cheap to plant, expensive to read
    before = target.stat()
    asked = _reads_asked(monkeypatch)
    with SafeRoot.open(root_dir) as root:
        with pytest.raises(ReadLimitExceeded) as exc:
            root.append_text("events.jsonl", "line\n", limit=100)
        with pytest.raises(ReadLimitExceeded):
            root.verify_appendable("events.jsonl", limit=100)
    assert exc.value.relpath == "events.jsonl" and exc.value.limit == 100
    assert asked == [], asked
    after = target.stat()
    assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)
    assert sorted(p.name for p in root_dir.iterdir()) == ["events.jsonl"], "no temporary left"


def test_a_bounded_append_at_exactly_the_limit_still_appends(tmp_path):
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (root_dir / "events.jsonl").write_bytes(b"x" * 100)
    with SafeRoot.open(root_dir) as root:
        root.append_text("events.jsonl", "line\n", limit=100)
        root.append_text("missing.jsonl", "first\n", limit=0)
    assert (root_dir / "events.jsonl").read_bytes() == b"x" * 100 + b"line\n"
    assert (root_dir / "missing.jsonl").read_bytes() == b"first\n"


def test_an_append_extends_the_inode_in_place_at_the_cost_of_the_line(tmp_path, monkeypatch):
    """#51: the journal is appended to, not rewritten. Across many appends
    the name keeps one inode, every byte written to it is a byte of a line
    (no copy of the existing content), nothing is read, and no temporary is
    published over the name."""
    import autoforge.safefs as safefs

    root_dir = tmp_path / "root"
    root_dir.mkdir()
    target = root_dir / "events.jsonl"
    written: list[int] = []
    replaced: list[str] = []
    real_write = safefs.os.write
    real_replace = safefs.os.replace

    def counted_write(fd, data):
        n = real_write(fd, data)
        written.append(n)
        return n

    def counted_replace(src, dst, **kwargs):
        replaced.append(dst)
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(safefs.os, "write", counted_write)
    monkeypatch.setattr(safefs.os, "replace", counted_replace)
    asked = _reads_asked(monkeypatch)
    lines = [f"line {i} " + "x" * (1000 * i) + "\n" for i in range(1, 9)]
    with SafeRoot.open(root_dir) as root:
        root.append_text("events.jsonl", lines[0])
        first = target.stat()
        for line in lines[1:]:
            root.append_text("events.jsonl", line)
    assert target.read_text(encoding="utf-8") == "".join(lines)
    assert target.stat().st_ino == first.st_ino, "the same inode throughout"
    assert sum(written) == sum(len(line.encode()) for line in lines), "only the lines were written"
    assert replaced == [] and asked == []
    assert sorted(p.name for p in root_dir.iterdir()) == ["events.jsonl"]


def test_verify_appendable_reports_absence_and_creates_nothing(tmp_path):
    """The pre-launch check answers whether the journal *can* be appended to,
    so an absent journal is appendable (the first append creates it) and the
    check must not be the thing that creates it -- nor its parent."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    with SafeRoot.open(root_dir) as root:
        assert root.verify_appendable("run-1/events.jsonl") is False
        assert not (root_dir / "run-1").exists(), "the parent was not created"
        root.ensure_dir("run-1")
        assert root.verify_appendable("run-1/events.jsonl") is False
        assert sorted(p.name for p in (root_dir / "run-1").iterdir()) == []
        root.append_text("run-1/events.jsonl", "first\n")
        assert root.verify_appendable("run-1/events.jsonl", limit=6) is True
    assert (root_dir / "run-1" / "events.jsonl").read_bytes() == b"first\n"


def test_verify_appendable_refuses_every_entry_the_append_would_refuse(tmp_path):
    """Whatever the append after the agent would refuse, the check before the
    launch refuses too, so the refusal lands before any work is done."""
    from autoforge.safefs import UnsafePathError

    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    for kind in ("symlink", "hardlink", "fifo", "directory"):
        rel = f"{kind}.jsonl"
        plant_final(root_dir, rel, kind, sentinel)
        with SafeRoot.open(root_dir) as root:
            with pytest.raises(UnsafePathError) as by_check:
                root.verify_appendable(rel)
            with pytest.raises(UnsafePathError) as by_append:
                root.append_text(rel, "controller\n")
        assert str(by_check.value) == str(by_append.value), kind
    sentinel.assert_untouched()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_an_unwritable_file_is_refused_by_the_append_and_by_the_check_alike(tmp_path):
    """A regular file the controller may not open for writing is an access
    fact, not an unsafe path: both opens report it as such, with the same
    message, and neither writes, replaces or creates anything."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    target = root_dir / "events.jsonl"
    target.write_bytes(b"theirs\n")
    target.chmod(0o444)
    before = target.stat()
    try:
        with SafeRoot.open(root_dir) as root:
            with pytest.raises(UnreadableEntryError) as by_check:
                root.verify_appendable("events.jsonl")
            with pytest.raises(UnreadableEntryError) as by_append:
                root.append_text("events.jsonl", "controller\n")
    finally:
        target.chmod(0o600)
    assert str(by_check.value) == str(by_append.value)
    assert by_append.value.path.endswith("events.jsonl")
    assert "Permission denied" in str(by_append.value)
    assert target.read_bytes() == b"theirs\n"
    assert (target.stat().st_ino, target.stat().st_size) == (before.st_ino, before.st_size)
    assert sorted(p.name for p in root_dir.iterdir()) == ["events.jsonl"], "no temporary left"


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
