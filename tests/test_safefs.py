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
from collections.abc import Iterator
from contextlib import contextmanager

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


def _append(root: SafeRoot, rel: str, text: str, **kwargs) -> None:
    """Open, append once and close: the append as a single controller write."""
    with root.open_append(rel, **kwargs) as handle:
        root.append_to(handle, text)


# -- the operations under test -------------------------------------------------
# Each takes an open root and a relative path, and is a *controller write* or
# a controller read of its own artifact. Together they are every way this
# module touches the filesystem.
OPERATIONS = {
    "write_bytes": lambda root, rel: root.write_bytes(rel, b"controller\n"),
    "write_text": lambda root, rel: root.write_text(rel, "controller\n"),
    "create_exclusive": lambda root, rel: root.create_exclusive(rel, b"controller\n"),
    "append": lambda root, rel: _append(root, rel, "controller\n"),
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
    to look at, as the refusal tells them to. The same holds for a second
    name given to the journal's own inode *after* it was opened: the append
    re-inspects the held descriptor and refuses on the link count."""
    from autoforge.safefs import UnsafePathError

    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    os.link(sentinel.path, root_dir / "events.jsonl")
    before = (root_dir / "events.jsonl").stat()

    # The refusal describes what an append would do to the shared inode
    # (alter it through its other name), not the replacement it never does.
    refusal = "hard link: 2 directory entries name this file, so writing here would alter"
    with SafeRoot.open(root_dir) as root:
        with pytest.raises(UnsafePathError, match=refusal):
            root.open_append("events.jsonl")

    sentinel.assert_untouched()
    after = (root_dir / "events.jsonl").stat()
    assert (after.st_ino, after.st_nlink, after.st_size) == (before.st_ino, 2, before.st_size)
    assert sorted(p.name for p in root_dir.iterdir()) == ["events.jsonl"], "no temporary left"

    # The controller's own journal, given a second name while it was held.
    (root_dir / "events.jsonl").unlink()
    (root_dir / "events.jsonl").write_text("old\n", encoding="utf-8")
    with SafeRoot.open(root_dir) as root, root.open_append("events.jsonl") as journal:
        os.link(root_dir / "events.jsonl", tmp_path / "second-name")
        with pytest.raises(UnsafePathError, match=refusal):
            root.append_to(journal, "controller\n")
    assert (root_dir / "events.jsonl").read_text(encoding="utf-8") == "old\n"
    assert (tmp_path / "second-name").stat().st_nlink == 2


def _rename_a_foreign_file_over(root_dir, journal, foreign) -> None:
    os.rename(foreign, journal)


def _unlink_and_create_afresh(root_dir, journal, foreign) -> None:
    journal.unlink()
    journal.write_text("planted\n", encoding="utf-8")


def _unlink_and_link_the_foreign_file(root_dir, journal, foreign) -> None:
    journal.unlink()
    os.link(foreign, journal)


def _unlink_and_plant_a_symlink(root_dir, journal, foreign) -> None:
    journal.unlink()
    journal.symlink_to(foreign)


def _link_out_and_unlink(root_dir, journal, foreign) -> None:
    os.link(journal, root_dir.parent / "stolen-journal")
    journal.unlink()


def _rename_out(root_dir, journal, foreign) -> None:
    os.rename(journal, root_dir.parent / "stolen-journal")


def _unlink(root_dir, journal, foreign) -> None:
    journal.unlink()


def _replace_the_directory(root_dir, journal, foreign) -> None:
    os.rename(journal.parent, root_dir.parent / "stolen-run")
    journal.parent.mkdir()
    os.rename(foreign, journal)


def _remove_the_directory(root_dir, journal, foreign) -> None:
    os.rename(journal.parent, root_dir.parent / "stolen-run")


@pytest.mark.parametrize(
    "tamper",
    [
        _rename_a_foreign_file_over,
        _unlink_and_create_afresh,
        _unlink_and_link_the_foreign_file,
        _unlink_and_plant_a_symlink,
        _link_out_and_unlink,
        _rename_out,
        _unlink,
        _replace_the_directory,
        _remove_the_directory,
    ],
    ids=[
        "foreign file renamed over",
        "unlinked, fresh file created",
        "unlinked, foreign file linked",
        "unlinked, symlink planted",
        "linked out and unlinked",
        "renamed out",
        "unlinked",
        "directory replaced",
        "directory removed",
    ],
)
def test_an_append_refuses_a_name_that_no_longer_holds_the_opened_inode(tmp_path, tamper):
    """PR #91 review, fifth round: the append is bound to the inode that was
    inspected at the open, not to whatever is at the name later.

    The handle is opened at one moment and appended through at another
    (the run logger opens before the launch and appends after the agent
    returned), and between the two a same-user process can put anything at
    the name: rename a small single-link regular file of the operator's
    over it -- which passes every per-open check, since it *is* a regular
    file with one name -- create a fresh file after unlinking the journal,
    link or symlink a foreign file there, move the journal out, or replace
    the whole directory. The append must refuse every one of them, and the
    refusal must cost nothing: no byte reaches the file now at the name, no
    byte reaches the inode that was opened, the sentinel outside the root
    is untouched, and the controller neither creates nor removes an entry.

    What makes this possible is the held descriptor: the opened inode stays
    allocated while it is held, so the fresh file cannot reuse its number
    and an identity comparison of the name against the handle is exact.
    """
    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    run_dir = root_dir / "run"
    run_dir.mkdir(parents=True)
    journal = run_dir / "events.jsonl"
    journal.write_text("old\n", encoding="utf-8")
    foreign = tmp_path / "operator-notes.txt"
    foreign.write_text("operator data\n", encoding="utf-8")
    foreign_before = foreign.stat()

    with SafeRoot.open(root_dir) as root, root.open_append("run/events.jsonl") as handle:
        opened = os.fstat(handle.fd)
        tamper(root_dir, journal, foreign)
        listing = sorted(os.listdir(run_dir)) if run_dir.exists() else None
        with pytest.raises(UnsafePathError, match="refusing to append to") as exc:
            root.append_to(handle, "controller\n")
        assert os.fstat(handle.fd).st_size == opened.st_size, "the opened inode received the line"
        assert "written" in str(exc.value), "the refusal says nothing was written"

    sentinel.assert_untouched()
    assert (sorted(os.listdir(run_dir)) if run_dir.exists() else None) == listing, (
        "the controller created or removed an entry"
    )
    if journal.exists() or journal.is_symlink():
        st = journal.lstat()
        assert (st.st_ino, st.st_size) != (opened.st_ino, opened.st_size + len("controller\n"))
    for planted in (journal, foreign, root_dir.parent / "stolen-journal"):
        if planted.is_file() and not planted.is_symlink():
            assert "controller" not in planted.read_text(encoding="utf-8"), planted
    if foreign.exists():
        assert foreign.read_text(encoding="utf-8") == "operator data\n"
        assert foreign.stat().st_ino == foreign_before.st_ino
    elif journal.is_file() and not journal.is_symlink():
        # The operator's file now carries the journal's name: still theirs.
        assert journal.read_text(encoding="utf-8") == "operator data\n"
        assert journal.stat().st_ino == foreign_before.st_ino


def _link_out_and_unlink_by_path(journal, stolen) -> None:
    os.link(journal, stolen)
    journal.unlink()


def _rename_out_by_path(journal, stolen) -> None:
    os.rename(journal, stolen)


@pytest.mark.parametrize(
    "move", [_link_out_and_unlink_by_path, _rename_out_by_path], ids=["link then unlink", "rename"]
)
def test_an_append_follows_its_inode_when_the_name_is_moved_after_the_re_inspection(
    tmp_path, monkeypatch, move
):
    """The stated limit of the in-place append: the window is one syscall.

    The append re-inspects the name and the held inode immediately before
    the ``write(2)``, and a move that lands between the two is not seen: a
    second name outside the root plus an unlink of this one, or a plain
    rename, which no link count ever sees. The line then lands in the held
    inode under its new name and the append reports success. The race is
    made deterministic by moving the name inside the write call itself,
    after the real re-inspection has passed.

    What must hold, and is all the module promises: the bytes reach the
    *same inode* the controller inspected (its own journal, now under a
    name of the other process's choosing), a foreign file outside the root
    is untouched, no other inode receives the line, and no temporary or
    replacement appears under the root. A file the other process puts at
    the name in that window receives nothing, because the write goes
    through the held descriptor and not through the name.
    """
    import autoforge.safefs as safefs

    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    journal = root_dir / "events.jsonl"
    journal.write_text("old\n", encoding="utf-8")
    inode = journal.stat().st_ino
    stolen = tmp_path / "stolen-journal"
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("decoy\n", encoding="utf-8")
    real_write = safefs.os.write
    moved: list[int] = []

    def move_the_name_then_write(fd, data):
        if os.fstat(fd).st_ino == inode and not moved:
            moved.append(fd)
            move(journal, stolen)
            os.rename(decoy, journal)  # and something else takes the name
        return real_write(fd, data)

    monkeypatch.setattr(safefs.os, "write", move_the_name_then_write)

    with SafeRoot.open(root_dir) as root:
        _append(root, "events.jsonl", "controller\n")

    assert moved, "the race point was never reached"
    sentinel.assert_untouched()
    assert stolen.read_text(encoding="utf-8") == "old\ncontroller\n"
    assert stolen.stat().st_ino == inode, "the line reached an inode that was not inspected"
    assert stolen.stat().st_nlink == 1
    assert journal.read_text(encoding="utf-8") == "decoy\n", "the file at the name got the line"
    assert [e.name for e in root_dir.iterdir()] == ["events.jsonl"]


def test_an_append_refuses_an_inode_unlinked_before_the_inspection(tmp_path, monkeypatch):
    """PR #91 review, fourth round: the inspection must find exactly one name.

    A name unlinked between the ``open`` and the ``fstat`` leaves a
    descriptor on an inode with ``st_nlink == 0``, and a write through it
    would go into a file nothing names and report success, with no journal
    left under the root to show for it. The race is made deterministic by
    unlinking the name inside the open call, before the real inspection.

    What must hold: the open is refused on the descriptor, no handle is
    handed back, the bytes reach no inode at all (the unlinked one
    included), the sentinel is untouched and nothing is created under the
    root.
    """
    import autoforge.safefs as safefs

    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    journal = root_dir / "events.jsonl"
    journal.write_text("old\n", encoding="utf-8")
    keep = os.open(journal, os.O_RDONLY)  # the unlinked inode, for inspection afterwards
    real_open = os.open

    def open_then_unlink_the_name(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        if name == "events.jsonl":
            os.unlink(name, dir_fd=kwargs["dir_fd"])
        return fd

    monkeypatch.setattr(safefs.os, "open", open_then_unlink_the_name)

    try:
        with SafeRoot.open(root_dir) as root:
            with pytest.raises(UnsafePathError, match="no directory entry names"):
                root.open_append("events.jsonl")
        sentinel.assert_untouched()
        assert os.fstat(keep).st_size == len("old\n"), "the unlinked inode received the line"
        assert os.fstat(keep).st_nlink == 0
    finally:
        os.close(keep)
    assert [e.name for e in root_dir.iterdir()] == [], "a journal or a temporary was created"


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

    `read_text` and `open_append` are the operations that must open the entry
    that is there (there is nothing to rename into place), so they are the
    ones a FIFO could stall. A whole-file write never opens it at all.
    """
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    os.mkfifo(root_dir / "pipe")
    with SafeRoot.open(root_dir) as root:
        for op in ("read_text", "append"):
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
    """The append writes in place through the opened descriptor, so the
    file's size is a single `fstat` and nothing is ever read. With a limit
    the refusal lands at the open, before anything is written: the
    oversized file keeps its name, its inode and its size, no handle is
    handed back, and no temporary is created. A file that grows past the
    limit while the handle is held is refused again at the append, on the
    same descriptor."""
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
            root.open_append("events.jsonl", limit=100)
    assert exc.value.relpath == "events.jsonl" and exc.value.limit == 100
    assert asked == [], asked
    after = target.stat()
    assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)
    assert sorted(p.name for p in root_dir.iterdir()) == ["events.jsonl"], "no temporary left"

    # Enlarged while held: the append refuses on the descriptor.
    os.truncate(target, 10)
    with SafeRoot.open(root_dir) as root, root.open_append("events.jsonl", limit=100) as handle:
        os.truncate(target, 1 << 20)
        with pytest.raises(ReadLimitExceeded):
            root.append_to(handle, "line\n")
    assert asked == [] and target.stat().st_size == 1 << 20


def test_a_bounded_append_at_exactly_the_limit_still_appends(tmp_path):
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (root_dir / "events.jsonl").write_bytes(b"x" * 100)
    with SafeRoot.open(root_dir) as root:
        _append(root, "events.jsonl", "line\n", limit=100)
        _append(root, "missing.jsonl", "first\n", limit=0)
    assert (root_dir / "events.jsonl").read_bytes() == b"x" * 100 + b"line\n"
    assert (root_dir / "missing.jsonl").read_bytes() == b"first\n"


def test_an_append_extends_the_inode_in_place_at_the_cost_of_the_line(tmp_path, monkeypatch):
    """#51: the journal is appended to, not rewritten. Across many appends
    the name keeps one inode, every byte written to it is a byte of a line
    (no copy of the existing content), nothing is read, and no temporary is
    published over the name. This holds whether each line has its own
    open or many lines go through one held handle."""
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
        _append(root, "events.jsonl", lines[0])
        first = target.stat()
        for line in lines[1:4]:
            _append(root, "events.jsonl", line)
        with root.open_append("events.jsonl") as handle:
            for line in lines[4:]:
                root.append_to(handle, line)
    assert target.read_text(encoding="utf-8") == "".join(lines)
    assert target.stat().st_ino == first.st_ino, "the same inode throughout"
    assert sum(written) == sum(len(line.encode()) for line in lines), "only the lines were written"
    assert replaced == [] and asked == []
    assert sorted(p.name for p in root_dir.iterdir()) == ["events.jsonl"]


def test_open_append_creates_the_file_and_its_parent_and_nothing_else(tmp_path):
    """The open is the moment the journal comes to exist: an absent file is
    created empty, its parent with it, and a second open finds the same
    inode. The handle knows what it holds."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    with SafeRoot.open(root_dir) as root:
        with root.open_append("run-1/events.jsonl") as handle:
            created = (root_dir / "run-1" / "events.jsonl").stat()
            assert created.st_size == 0
            assert handle.identity == (created.st_dev, created.st_ino)
            assert handle.relpath == "run-1/events.jsonl" and handle.limit is None
            assert "run-1/events.jsonl" in repr(handle)
            root.append_to(handle, "first\n")
        with root.open_append("run-1/events.jsonl", limit=6) as again:
            assert again.identity == handle.identity and again.limit == 6
    assert sorted(p.name for p in (root_dir / "run-1").iterdir()) == ["events.jsonl"]
    assert (root_dir / "run-1" / "events.jsonl").read_bytes() == b"first\n"


def test_a_closed_append_handle_refuses_to_be_appended_through(tmp_path):
    """Closing releases the descriptor and with it the binding; a later append
    through the handle is a programming error, reported as such rather than
    as a write to whatever number the kernel handed out since."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    with SafeRoot.open(root_dir) as root:
        handle = root.open_append("events.jsonl")
        handle.close()
        handle.close()  # idempotent
        with pytest.raises(StateError, match="already closed"):
            root.append_to(handle, "late\n")
        with pytest.raises(StateError, match="already closed"):
            os.fstat(handle.fd)
    assert (root_dir / "events.jsonl").read_bytes() == b""


def test_open_append_refuses_every_entry_the_append_would_have_to_refuse(tmp_path):
    """Whatever cannot be a journal is refused at the open, before the launch,
    so the refusal lands before any work is done and no handle exists."""
    from autoforge.safefs import UnsafePathError

    sentinel = Sentinel(tmp_path)
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    for kind in ("symlink", "hardlink", "fifo", "directory"):
        rel = f"{kind}.jsonl"
        plant_final(root_dir, rel, kind, sentinel)
        with SafeRoot.open(root_dir) as root:
            with pytest.raises(UnsafePathError):
                root.open_append(rel)
    sentinel.assert_untouched()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_an_unwritable_file_is_refused_at_the_open(tmp_path):
    """A regular file the controller may not open for writing is an access
    fact, not an unsafe path: the open reports it as such and neither
    writes, replaces nor creates anything. A file made unwritable *after*
    the open is another matter: the held descriptor was granted write
    access when it was opened and keeps it, as any descriptor does, so the
    append still lands; the next open refuses."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    target = root_dir / "events.jsonl"
    target.write_bytes(b"theirs\n")
    target.chmod(0o444)
    before = target.stat()
    try:
        with SafeRoot.open(root_dir) as root:
            with pytest.raises(UnreadableEntryError) as by_open:
                root.open_append("events.jsonl")
    finally:
        target.chmod(0o600)
    assert by_open.value.path.endswith("events.jsonl")
    assert "Permission denied" in str(by_open.value)
    assert target.read_bytes() == b"theirs\n"
    assert (target.stat().st_ino, target.stat().st_size) == (before.st_ino, before.st_size)
    assert sorted(p.name for p in root_dir.iterdir()) == ["events.jsonl"], "no temporary left"

    try:
        with SafeRoot.open(root_dir) as root, root.open_append("events.jsonl") as handle:
            target.chmod(0o444)
            root.append_to(handle, "controller\n")
            with pytest.raises(UnreadableEntryError):
                root.open_append("events.jsonl")
    finally:
        target.chmod(0o600)
    assert target.read_bytes() == b"theirs\ncontroller\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
@contextmanager
def _descriptors_left_open(monkeypatch) -> Iterator[list[int]]:
    """Yield a list that, once the block has run, holds every descriptor the
    block opened (or duplicated) and never closed -- whether it returned or
    raised."""
    real_open, real_dup, real_close = os.open, os.dup, os.close
    opened: list[int] = []
    closed: list[int] = []
    left: list[int] = []

    def counting_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def counting_dup(fd):
        fd = real_dup(fd)
        opened.append(fd)
        return fd

    def counting_close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", counting_open)
    monkeypatch.setattr(os, "dup", counting_dup)
    monkeypatch.setattr(os, "close", counting_close)
    try:
        yield left
    finally:
        monkeypatch.setattr(os, "open", real_open)
        monkeypatch.setattr(os, "dup", real_dup)
        monkeypatch.setattr(os, "close", real_close)
        left[:] = opened
        for fd in closed:
            left.remove(fd)


def test_open_append_releases_the_descriptor_when_the_directory_fsync_fails(tmp_path, monkeypatch):
    """PR #91 review, sixth round: a refused open owns nothing afterwards.

    The directory ``fsync`` that makes a created name durable runs after
    the journal is open and before the handle that would own it exists. A
    fatal error there (``EIO``: the entry may not be durable, so the open
    cannot report success) used to leave the journal descriptor open with
    nothing to close it -- one leaked descriptor per refused open -- and
    escaped as a raw ``OSError``, past the typed boundary every other
    failure of this open reports through.

    What must hold: every descriptor the call opened is closed by the time
    it raises; the error is a ``StateError`` naming the file and the cause,
    as an ``open`` failure is; the journal, which this open did not create,
    is exactly what it was; and once the directory can be fsynced again the
    same open succeeds and appends.
    """
    import autoforge.safefs as safefs

    root_dir = tmp_path / "root"
    root_dir.mkdir()
    journal = root_dir / "events.jsonl"
    journal.write_text("old\n", encoding="utf-8")
    before = journal.stat()
    real_fsync = os.fsync

    def failing_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(safefs.os, "fsync", failing_directory_fsync)
    with SafeRoot.open(root_dir) as root:
        with (
            pytest.raises(StateError, match="cannot open .*events.jsonl.*injected fsync") as exc,
            _descriptors_left_open(monkeypatch) as left,
        ):
            root.open_append("events.jsonl", limit=1 << 20)
        assert left == [], "a refused open leaked a descriptor"
        assert not isinstance(exc.value, (UnsafePathError, UnreadableEntryError))
        assert isinstance(exc.value.__cause__, OSError)
        after = journal.stat()
        assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        assert [e.name for e in root_dir.iterdir()] == ["events.jsonl"]

        monkeypatch.setattr(safefs.os, "fsync", real_fsync)
        with _descriptors_left_open(monkeypatch) as left:
            _append(root, "events.jsonl", "new\n")
        assert left == []
    assert journal.read_bytes() == b"old\nnew\n"


def test_open_append_treats_a_directory_that_cannot_be_fsynced_as_best_effort(
    tmp_path, monkeypatch
):
    """The same tolerance every published name has: a filesystem that
    rejects ``fsync`` on a directory (``EINVAL``) weakens durability and
    does not refuse the open, so the handle comes back and appends."""
    import autoforge.safefs as safefs

    root_dir = tmp_path / "root"
    root_dir.mkdir()
    real_fsync = os.fsync

    def refusing_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "no directory fsync here")
        return real_fsync(fd)

    monkeypatch.setattr(safefs.os, "fsync", refusing_directory_fsync)
    with SafeRoot.open(root_dir) as root:
        _append(root, "run-1/events.jsonl", "first\n")
    assert (root_dir / "run-1" / "events.jsonl").read_bytes() == b"first\n"


def test_open_append_discovers_a_directory_that_cannot_take_the_file(tmp_path):
    """Creating the journal is part of the open, so a run directory that
    cannot take a new entry is found at the open -- before the launch -- as
    an access fact on the journal's path, and nothing is created."""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    run_dir = root_dir / "run-1"
    run_dir.mkdir()
    run_dir.chmod(0o500)
    try:
        with SafeRoot.open(root_dir) as root:
            with pytest.raises(UnreadableEntryError) as by_open:
                root.open_append("run-1/events.jsonl")
    finally:
        run_dir.chmod(0o700)
    assert "Permission denied" in str(by_open.value)
    assert sorted(p.name for p in run_dir.iterdir()) == [], "nothing was created"


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
