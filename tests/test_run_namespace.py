"""Per-issue run identities and namespaces (#178, ADR 0008 D5, D17.3).

Every test runs offline against a temporary git repository: the
coordination root is its git common dir, exactly as the controller lock
derives it. The CLI's refusal of a state directory inside the scheduler root
is tested in ``test_cli.py``.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from autoforge import run_namespace as rn
from autoforge import state as state_mod
from autoforge.config import default_config
from autoforge.errors import NamespaceError, NamespaceMismatch, SchedulerStateDirError
from autoforge.run_namespace import (
    BINDING_SCHEMA,
    EpicRef,
    IssueNamespace,
    NamespaceKind,
    RunBinding,
    RunKey,
    StateStatus,
    archive_issue_state,
    check_binding,
    check_standalone_state_dir,
    issue_namespace,
    list_issue_namespaces,
    load_issue_namespace,
    write_binding,
)
from autoforge.runlog import ExecutionRecord
from autoforge.safefs import SafeRoot, UnsafePathError
from autoforge.state import AutoForgeState, load_state, save_state

from .conftest import EPIC, ISSUE, git_repo, make_engine

REPOSITORY_ID = 700_001
ISSUE_IDS = {11: 9_011, 12: 9_012, 13: 9_013, 2: 9_002}
EPIC5 = EpicRef(number=5, issue_id=8_005)
EPIC6 = EpicRef(number=6, issue_id=8_006)


def _issue_url(n: int) -> str:
    return f"https://github.com/owner/repo/issues/{n}"


@pytest.fixture
def common(tmp_path) -> Path:
    """The coordination root: a real repository's git common dir, resolved."""
    return Path(os.path.realpath(git_repo(tmp_path / "repo") / ".git"))


def _binding(n: int, *, epic: EpicRef = EPIC5, **overrides) -> RunBinding:
    fields = dict(
        run_key=RunKey(REPOSITORY_ID, n),
        issue_id=ISSUE_IDS.get(n, 9_000 + n),
        repository_owner="owner",
        repository_name="repo",
        epic=epic,
        worktree_path=f"/work/autoforge/worktrees/{n}",
        created_at="2026-10-17T00:00:00+00:00",
    )
    fields.update(overrides)
    return RunBinding(**fields)  # type: ignore[arg-type]


def _bind(common: Path, n: int, **kw) -> IssueNamespace:
    ns = issue_namespace(common, n)
    write_binding(ns, _binding(n, **kw))
    return ns


def _state(n: int, run_id: str | None = None, **kw) -> AutoForgeState:
    base = dict(
        run_id=run_id or f"af-issue-{n}",
        repository="owner/repo",
        epic_url=EPIC,
        current_issue_url=_issue_url(n),
        created_at="2026-10-17T00:00:00+00:00",
        updated_at="2026-10-17T00:00:00+00:00",
    )
    base.update(kw)
    return AutoForgeState(**base)  # type: ignore[arg-type]


def _save_in(ns: IssueNamespace, state: AutoForgeState) -> None:
    """Save ``state`` the way an engine bound to ``ns`` does: through the anchored root."""
    with ns.state_paths().open_root() as root:
        save_state(state, ns.state_file, root=root)


def _tree(root: Path) -> dict[str, tuple]:
    """Every entry under ``root``: kind, mode and, for a file, its bytes; links unfollowed."""
    out: dict[str, tuple] = {}
    if not os.path.lexists(root):
        return out
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            st = os.lstat(path)
            rel = os.path.relpath(path, root)
            if stat.S_ISREG(st.st_mode):
                with open(path, "rb") as fh:
                    out[rel] = ("file", stat.S_IMODE(st.st_mode), st.st_mtime_ns, fh.read())
            elif stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(path))
            else:
                out[rel] = ("dir" if stat.S_ISDIR(st.st_mode) else "other", st.st_mtime_ns)
    return out


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 17, 12, 0, 0, tzinfo=tz)


# -- RunKey and the layout ----------------------------------------------------------


def test_run_key_is_a_frozen_value_with_a_stable_rendering():
    key = RunKey(REPOSITORY_ID, 11)
    assert str(key) == f"{REPOSITORY_ID}/11"
    assert key == RunKey(REPOSITORY_ID, 11) and hash(key) == hash(RunKey(REPOSITORY_ID, 11))
    with pytest.raises(AttributeError):
        key.issue_number = 12  # type: ignore[misc]


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "11", None, 2**63])
def test_run_key_refuses_anything_but_a_positive_integer(bad):
    with pytest.raises(NamespaceError):
        RunKey(bad, 11)  # type: ignore[arg-type]
    with pytest.raises(NamespaceError):
        RunKey(REPOSITORY_ID, bad)  # type: ignore[arg-type]


def test_the_layout_is_pure_and_deterministic(tmp_path, monkeypatch):
    """No file is read or created, and the working directory does not matter."""
    root = tmp_path / "no-such-repository" / ".git"
    first = issue_namespace(root, 11)
    monkeypatch.chdir(tmp_path)
    second = issue_namespace(str(root) + "/", 11)
    assert first == second
    assert not root.exists()
    assert first.directory == root / "autoforge" / "scheduler" / "issues" / "11"
    assert first.binding_file == first.directory / "binding.json"
    assert first.run_lock_file == first.directory / "run.lock"
    assert first.claim_file == first.directory / "claim.json"
    assert first.state_dir == first.directory / "state"
    assert first.state_file == first.directory / "state" / "state.json"
    assert first.workers_dir == first.directory / "workers"
    assert issue_namespace(root, 12).directory != first.directory


def test_the_state_paths_are_anchored_at_the_common_dir(common):
    paths = issue_namespace(common, 11).state_paths()
    assert paths.anchor == common
    assert paths.relative == ("autoforge", "scheduler", "issues", "11", "state")
    assert paths.state_file == common / "autoforge/scheduler/issues/11/state/state.json"
    assert paths.logs_dir == common / "autoforge/scheduler/issues/11/state/logs"


def test_the_namespace_ignores_state_dir_settings_and_the_working_directory(
    common, tmp_path, monkeypatch
):
    """``--state-dir``, ``config.state_dir`` and the cwd never enter the layout (D5.3)."""
    cfg = default_config()
    cfg.state_dir = str(tmp_path / "configured")
    eng = make_engine(tmp_path / "explicit", [], cfg=cfg, workdir=common.parent)
    monkeypatch.chdir(tmp_path)
    ns = issue_namespace(common, 11)
    eng.bind_issue_namespace(ns)
    assert eng.paths == ns.state_paths()
    monkeypatch.chdir(common.parent)
    assert issue_namespace(common, 11).state_paths() == eng.paths


@pytest.mark.parametrize("bad", ["relative/.git", ".git", ""])
def test_the_layout_refuses_a_relative_coordination_root(bad):
    with pytest.raises(NamespaceError, match="absolute"):
        issue_namespace(bad, 11)


@pytest.mark.parametrize("bad", [0, -3, False, "11"])
def test_the_layout_refuses_an_invalid_issue_number(common, bad):
    with pytest.raises(NamespaceError):
        issue_namespace(common, bad)  # type: ignore[arg-type]


# -- binding.json -------------------------------------------------------------------


def test_a_binding_is_written_once_and_read_back_strictly(common):
    ns = issue_namespace(common, 11)
    binding = _binding(11)
    assert write_binding(ns, binding) == ns.binding_file
    data = json.loads(ns.binding_file.read_text(encoding="utf-8"))
    assert data == {
        "schema": BINDING_SCHEMA,
        "run_key": {"repository_id": REPOSITORY_ID, "issue_number": 11},
        "issue_id": 9_011,
        "repository": {"owner": "owner", "name": "repo"},
        "epic": {"number": 5, "issue_id": 8_005},
        "worktree_path": "/work/autoforge/worktrees/11",
        "created_at": "2026-10-17T00:00:00+00:00",
    }
    assert stat.S_IMODE(ns.binding_file.stat().st_mode) == 0o600
    found = load_issue_namespace(common, 11)
    assert found.kind is NamespaceKind.BOUND and found.binding == binding
    assert found.state.status is StateStatus.ABSENT and not found.claim_present
    # Written only: no state directory, no claim, no lock, no worker logs.
    assert sorted(p.name for p in ns.directory.iterdir()) == ["binding.json"]


def test_a_new_binding_records_when_it_was_created(common, monkeypatch):
    monkeypatch.setattr(state_mod, "datetime", FrozenDatetime)
    binding = RunBinding(
        run_key=RunKey(REPOSITORY_ID, 11),
        issue_id=9_011,
        repository_owner="owner",
        repository_name="repo",
        epic=EPIC5,
        worktree_path="/w/11",
    )
    assert binding.created_at == "2026-10-17T12:00:00+00:00"
    assert binding.repository == "owner/repo"


def test_a_second_write_is_refused_and_the_first_binding_kept(common):
    """Different EPICs, one issue: one namespace, bound under the first EPIC (D5.5)."""
    ns = _bind(common, 11, epic=EPIC5)
    before = ns.binding_file.read_bytes()
    for second in (_binding(11, epic=EPIC6), _binding(11, epic=EPIC5)):
        with pytest.raises(NamespaceError, match="already bound"):
            write_binding(ns, second)
    assert ns.binding_file.read_bytes() == before
    found = load_issue_namespace(common, 11)
    assert found.binding is not None and found.binding.epic == EPIC5
    # The scheduler for EPIC 6 holds it (held:other-epic); for EPIC 5 it is this run.
    check_binding(found.binding, run_key=RunKey(REPOSITORY_ID, 11), issue_id=9_011)
    check_binding(found.binding, run_key=RunKey(REPOSITORY_ID, 11), issue_id=9_011, epic=EPIC5)
    with pytest.raises(NamespaceMismatch) as info:
        check_binding(found.binding, run_key=RunKey(REPOSITORY_ID, 11), issue_id=9_011, epic=EPIC6)
    assert info.value.field == "epic"


@pytest.mark.parametrize(
    "expectation, field",
    [
        (dict(run_key=RunKey(REPOSITORY_ID + 1, 11), issue_id=9_011), "repository_id"),
        (dict(run_key=RunKey(REPOSITORY_ID, 12), issue_id=9_011), "issue_number"),
        (dict(run_key=RunKey(REPOSITORY_ID, 11), issue_id=9_999), "issue_id"),
        (
            dict(run_key=RunKey(REPOSITORY_ID, 11), issue_id=9_011, epic=EpicRef(5, 1)),
            "epic",
        ),
    ],
)
def test_a_mismatching_binding_is_a_typed_refusal(common, expectation, field):
    _bind(common, 11)
    binding = load_issue_namespace(common, 11).binding
    assert binding is not None
    with pytest.raises(NamespaceMismatch) as info:
        check_binding(binding, **expectation)
    assert info.value.field == field
    assert isinstance(info.value, NamespaceError)


def test_a_renamed_repository_keeps_its_namespace(common):
    """Identity is the numeric ids: the owner/name pair is display only (D5.1)."""
    _bind(common, 11)
    binding = load_issue_namespace(common, 11).binding
    assert binding is not None
    renamed = replace(binding, repository_owner="new-owner", repository_name="new-name")
    check_binding(renamed, run_key=RunKey(REPOSITORY_ID, 11), issue_id=9_011, epic=EPIC5)
    assert issue_namespace(common, 11) == load_issue_namespace(common, 11).namespace


def test_a_binding_for_another_issue_is_refused_on_write_and_on_read(common):
    with pytest.raises(NamespaceMismatch) as info:
        write_binding(issue_namespace(common, 11), _binding(12))
    assert info.value.field == "issue_number"
    assert not issue_namespace(common, 11).directory.exists()
    # Planted by hand: the directory says 11, the binding says 12.
    ns = issue_namespace(common, 11)
    ns.directory.mkdir(parents=True)
    ns.binding_file.write_text(json.dumps(_binding(12).to_dict()), encoding="utf-8")
    with pytest.raises(NamespaceMismatch) as info:
        load_issue_namespace(common, 11)
    assert info.value.field == "issue_number"


def _planted(common: Path, document: object, *, raw: bytes | None = None) -> None:
    ns = issue_namespace(common, 11)
    ns.directory.mkdir(parents=True, exist_ok=True)
    data = raw if raw is not None else json.dumps(document).encode("utf-8")
    ns.binding_file.write_bytes(data)


def _good() -> dict:
    return _binding(11).to_dict()


def _with(path: tuple[str, ...], value: object) -> dict:
    doc = _good()
    target = doc
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return doc


def _without(*path: str) -> dict:
    doc = _good()
    target = doc
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    return doc


@pytest.mark.parametrize(
    "document, message",
    [
        ({**_binding(11).to_dict(), "extra": 1}, r"unknown field\(s\) \['extra'\]"),
        (_with(("run_key", "repo"), 1), r"run_key has unknown field"),
        (_with(("repository", "url"), "x"), r"repository has unknown field"),
        (_with(("epic", "title"), "x"), r"epic has unknown field"),
        (_with(("schema",), "autoforge.run-binding/2"), "this controller reads only"),
        (_without("schema"), "this controller reads only"),
        (_without("issue_id"), r"missing field\(s\) \['issue_id'\]"),
        (_without("epic", "issue_id"), r"epic is missing field"),
        (_with(("repository", "owner"), "o" * 40), "over its bound of 39"),
        (_with(("repository", "name"), "n" * 101), "over its bound of 100"),
        (_with(("repository", "name"), "a/b"), "not a GitHub owner or repository name"),
        (_with(("repository", "owner"), ""), "must not be empty"),
        (_with(("worktree_path",), "/" + "w" * 4096), "over its bound of 4096"),
        (_with(("worktree_path",), "relative/11"), "absolute path"),
        (_with(("worktree_path",), "/w/\n11"), "control characters"),
        (_with(("created_at",), "yesterday"), "not an ISO 8601 time"),
        (_with(("created_at",), "2026-10-17T00:00:00+02:00"), "UTC time"),
        (_with(("created_at",), "2026-10-17T00:00:00+00:00" + "0" * 10), "over its bound"),
        (_with(("issue_id",), 0), "positive integer"),
        (_with(("issue_id",), -4), "positive integer"),
        (_with(("issue_id",), True), "must be an integer"),
        (_with(("issue_id",), 9011.0), "must be an integer"),
        (_with(("issue_id",), "9011"), "must be an integer"),
        (_with(("run_key", "repository_id"), 2**63), "positive integer"),
        (_with(("epic", "number"), 0), "positive integer"),
        (_with(("run_key",), [REPOSITORY_ID, 11]), "must be an object"),
        ([_good()], "must be a JSON object"),
    ],
)
def test_binding_loading_is_strict(common, document, message):
    _planted(common, document)
    with pytest.raises(NamespaceError, match=message) as info:
        load_issue_namespace(common, 11)
    assert not isinstance(info.value, NamespaceMismatch)
    assert str(issue_namespace(common, 11).binding_file) in str(info.value)


@pytest.mark.parametrize(
    "raw, message",
    [
        (b'{"schema": "autoforge.run-binding/1", "schema": "x"}', "more than once"),
        (b'{"issue_id": NaN}', "non-JSON number NaN"),
        (b"{not json", "not valid JSON"),
        (b'{"schema": "\xff"}', "not valid UTF-8"),
        (b" " * (rn.MAX_BINDING_BYTES + 1), "larger than"),
    ],
)
def test_a_binding_that_is_not_one_json_document_is_refused(common, raw, message):
    _planted(common, None, raw=raw)
    with pytest.raises(NamespaceError, match=message) as info:
        load_issue_namespace(common, 11)
    assert str(issue_namespace(common, 11).binding_file) in str(info.value)


def test_a_binding_that_is_not_a_regular_file_is_refused_unfollowed(common, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(_good()), encoding="utf-8")
    ns = issue_namespace(common, 11)
    ns.directory.mkdir(parents=True)
    ns.binding_file.symlink_to(target)
    with pytest.raises(NamespaceError, match="symbolic link"):
        load_issue_namespace(common, 11)
    with pytest.raises(NamespaceError, match="already bound"):
        write_binding(ns, _binding(11))
    assert target.read_text(encoding="utf-8") == json.dumps(_good())


def test_a_crash_while_writing_leaves_no_binding(common, monkeypatch):
    """Create-exclusive: the name appears only once the bytes are complete (D5.2)."""
    ns = issue_namespace(common, 11)

    class Crash(BaseException):
        pass

    real = SafeRoot._publish_new_at

    def crash_before_the_name(self, parent, name, data, *, mode, where):
        # The bytes are written and fsynced into the temporary; the process
        # dies before link(2) gives it the final name.
        fd = self._unnamed_tmp_at(parent, data, mode=mode, where=where)
        if fd is not None:
            os.close(fd)
        else:
            self._durable_tmp_at(parent, data, mode=mode, where=where)
        raise Crash

    monkeypatch.setattr(SafeRoot, "_publish_new_at", crash_before_the_name)
    with pytest.raises(Crash):
        write_binding(ns, _binding(11))
    monkeypatch.setattr(SafeRoot, "_publish_new_at", real)
    assert not os.path.lexists(ns.binding_file)
    assert load_issue_namespace(common, 11).kind is NamespaceKind.ABSENT
    write_binding(ns, _binding(11))
    assert load_issue_namespace(common, 11).kind is NamespaceKind.BOUND


# -- lookup -------------------------------------------------------------------------


def test_lookup_classes(common):
    """Absent, unbound with content, and bound (D14.2's inputs)."""
    assert load_issue_namespace(common, 11).kind is NamespaceKind.ABSENT

    # A crash between taking L2 and writing the binding: run.lock only.
    bare = issue_namespace(common, 11)
    bare.directory.mkdir(parents=True)
    bare.run_lock_file.write_bytes(b"")
    (bare.workers_dir).mkdir()
    (bare.state_dir / "logs").mkdir(parents=True)
    found = load_issue_namespace(common, 11)
    assert found.kind is NamespaceKind.ABSENT and found.binding is None
    assert found.state.status is StateStatus.ABSENT and not found.claim_present

    claimed = issue_namespace(common, 12)
    claimed.directory.mkdir(parents=True)
    claimed.claim_file.write_text("{}", encoding="utf-8")
    found = load_issue_namespace(common, 12)
    assert found.kind is NamespaceKind.UNBOUND and found.claim_present
    assert found.state.status is StateStatus.ABSENT

    stated = issue_namespace(common, 13)
    _save_in(stated, _state(13))
    found = load_issue_namespace(common, 13)
    assert found.kind is NamespaceKind.UNBOUND and not found.claim_present
    assert found.state.status is StateStatus.LOADED

    bound = _bind(common, 2)
    bound.claim_file.write_text("not parsed here", encoding="utf-8")
    _save_in(bound, _state(2))
    found = load_issue_namespace(common, 2)
    assert found.kind is NamespaceKind.BOUND and found.claim_present
    assert found.state.status is StateStatus.LOADED
    assert found.state.state is not None and found.state.state.current_issue_url == _issue_url(2)


def test_lookup_and_listing_never_create_quarantine_or_modify(common, tmp_path):
    for n in (11, 12):
        _bind(common, n)
    _save_in(issue_namespace(common, 11), _state(11))
    corrupt = issue_namespace(common, 12)
    corrupt.state_dir.mkdir()
    corrupt.state_file.write_text("{broken", encoding="utf-8")
    (common / "autoforge" / "scheduler" / "issues" / "notes").write_text("x", encoding="utf-8")
    before = _tree(common / "autoforge")
    for n in (11, 12, 13, 99):
        load_issue_namespace(common, n)
    list_issue_namespaces(common)
    assert _tree(common / "autoforge") == before

    # A repository without any scheduler tree: nothing is created either.
    fresh = Path(os.path.realpath(git_repo(tmp_path / "fresh") / ".git"))
    before = _tree(fresh)
    assert load_issue_namespace(fresh, 11).kind is NamespaceKind.ABSENT
    assert list_issue_namespaces(fresh) == rn.NamespaceListing((), (), ())
    assert _tree(fresh) == before


def test_two_issue_states_at_once(common, tmp_path):
    """Bind 11 and 12, save different states through two engines; nothing is shared."""
    engines = {}
    for n in (11, 12):
        ns = _bind(common, n)
        eng = make_engine(tmp_path / f"unused-{n}", [], workdir=common.parent)
        eng.bind_issue_namespace(ns)
        eng.new_run(EPIC, _issue_url(n))
        assert eng.state is not None
        eng.state.review_round = n
        eng.state.block_reason = f"only in {n}"
        eng.save()
        eng._logger().log_execution(
            ExecutionRecord(
                run_id=eng.state.run_id,
                seq=1,
                phase="ANALYZE_EXECUTE",
                issue_url=eng.state.current_issue_url,
            )
        )
        engines[n] = eng
    assert not (tmp_path / "unused-11").exists() and not (tmp_path / "unused-12").exists()

    files = {}
    for n, eng in engines.items():
        ns = issue_namespace(common, n)
        found = load_issue_namespace(common, n)
        assert found.state.status is StateStatus.LOADED
        loaded = found.state.state
        assert loaded is not None
        assert loaded.current_issue_url == _issue_url(n)
        assert loaded.review_round == n and loaded.block_reason == f"only in {n}"
        assert eng.load().run_id == loaded.run_id
        assert (ns.state_dir / "logs" / loaded.run_id / "events.jsonl").is_file()
        files[n] = {
            os.path.realpath(os.path.join(base, name))
            for base, _, names in os.walk(ns.directory)
            for name in names
        }
        assert all(path.startswith(str(ns.directory) + os.sep) for path in files[n])
    assert files[11] and files[12] and not files[11] & files[12]
    assert engines[11].state.run_id != engines[12].state.run_id  # type: ignore[union-attr]
    for eng in engines.values():
        eng.close()


def test_a_corrupt_state_is_reported_for_its_namespace_only(common):
    for n in (11, 12):
        _bind(common, n)
    _save_in(issue_namespace(common, 12), _state(12))
    corrupt = issue_namespace(common, 11)
    corrupt.state_dir.mkdir()
    corrupt.state_file.write_text('{"phase": "NO_SUCH_PHASE"}', encoding="utf-8")
    eleven = load_issue_namespace(common, 11)
    assert eleven.kind is NamespaceKind.BOUND
    assert eleven.state.status is StateStatus.CORRUPT and eleven.state.state is None
    assert "NO_SUCH_PHASE" in eleven.state.reason or "phase" in eleven.state.reason
    twelve = load_issue_namespace(common, 12)
    assert twelve.state.status is StateStatus.LOADED
    # Reported, never quarantined.
    assert sorted(p.name for p in corrupt.state_dir.iterdir()) == ["state.json"]


def test_a_corrupt_state_reason_is_redacted_and_bounded(common, monkeypatch):
    _bind(common, 11)
    secret = "ghp_" + "A" * 36

    def fails(path, *, root=None):
        raise state_mod.StateError(f"token {secret} " + "x" * 10_000)

    _save_in(issue_namespace(common, 11), _state(11))
    monkeypatch.setattr(rn, "load_state", fails)
    found = load_issue_namespace(common, 11)
    assert found.state.status is StateStatus.CORRUPT
    assert secret not in found.state.reason
    assert len(found.state.reason) <= rn.MAX_STATE_REASON_CHARS


def test_legacy_standalone_state_is_never_read_moved_or_modified(common):
    """A standalone `.autoforge/` and LOCAL's `<git dir>/autoforge/state` stay as they are."""
    repo = common.parent
    standalone = repo / ".autoforge"
    standalone.mkdir()
    save_state(_state(11, run_id="af-standalone"), standalone / "state.json")
    local = common / "autoforge" / "state"
    local.mkdir(parents=True)
    save_state(_state(11, run_id="af-local"), local / "state.json")
    before = (_tree(standalone), _tree(local))

    assert load_issue_namespace(common, 11).kind is NamespaceKind.ABSENT
    ns = _bind(common, 11)
    _save_in(ns, _state(11, run_id="af-namespace"))
    found = load_issue_namespace(common, 11)
    assert found.state.state is not None and found.state.state.run_id == "af-namespace"
    assert [n.issue_number for n in list_issue_namespaces(common).bound] == [11]
    assert archive_issue_state(ns) is not None

    assert (_tree(standalone), _tree(local)) == before
    assert load_state(standalone / "state.json").run_id == "af-standalone"
    assert load_state(local / "state.json").run_id == "af-local"


# -- hostile trees ------------------------------------------------------------------


@pytest.mark.parametrize("component", ["autoforge", "scheduler", "issues", "11"])
def test_a_link_planted_at_any_component_is_refused_and_never_written_through(
    common, tmp_path, component
):
    target = tmp_path / "target"
    target.mkdir()
    chain = ["autoforge", "scheduler", "issues", "11"]
    parent = common.joinpath(*chain[: chain.index(component)])
    parent.mkdir(parents=True, exist_ok=True)
    (parent / component).symlink_to(target, target_is_directory=True)
    ns = issue_namespace(common, 11)

    with pytest.raises(UnsafePathError):
        write_binding(ns, _binding(11))
    with pytest.raises(UnsafePathError):
        load_issue_namespace(common, 11)
    with pytest.raises(UnsafePathError):
        archive_issue_state(ns)
    eng = make_engine(tmp_path / "unused", [], workdir=common.parent)
    eng.bind_issue_namespace(ns)
    eng.new_run(EPIC, _issue_url(11))
    with pytest.raises(UnsafePathError):
        eng.save()
    if component != "11":
        with pytest.raises(UnsafePathError):
            list_issue_namespaces(common)
    assert list(target.iterdir()) == []


def test_a_linked_state_directory_is_reported_and_never_followed(common, tmp_path):
    ns = _bind(common, 11)
    target = tmp_path / "target"
    target.mkdir()
    save_state(_state(11, run_id="af-elsewhere"), target / "state.json")
    ns.state_dir.symlink_to(target, target_is_directory=True)
    found = load_issue_namespace(common, 11)
    assert found.kind is NamespaceKind.BOUND
    assert found.state.status is StateStatus.CORRUPT and "symbolic link" in found.state.reason
    before = _tree(target)
    with pytest.raises(UnsafePathError):
        archive_issue_state(ns)
    assert _tree(target) == before


def test_listing_reports_what_is_not_an_issue_directory(common, tmp_path):
    for n in (2, 11, 10):
        _bind(common, n)
    issues = common / "autoforge" / "scheduler" / "issues"
    (issues / "12").mkdir()  # an issue directory without a binding
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "binding.json").write_text(json.dumps(_binding(13).to_dict()), "utf-8")
    (issues / "13").symlink_to(elsewhere, target_is_directory=True)
    (issues / "14").write_text("a regular file named like an issue", encoding="utf-8")
    for name in ("abc", "011", "0", "-5", "1e3", "12a"):
        (issues / name).mkdir()
    os.mkfifo(issues / "15")

    listing = list_issue_namespaces(common)
    assert [ns.issue_number for ns in listing.bound] == [2, 10, 11]
    assert listing.bound[0] == issue_namespace(common, 2)
    assert [ns.issue_number for ns in listing.unbound] == [12]
    rejected = {r.name: r.reason for r in listing.rejected}
    assert rejected["13"] == "a symbolic link, not a directory"
    assert rejected["14"] == "a regular file, not a directory"
    assert rejected["15"] == "a FIFO, not a directory"
    for name in ("abc", "011", "0", "-5", "1e3", "12a"):
        assert rejected[name] == "not a decimal issue number"
    assert [r.name for r in listing.rejected] == sorted(rejected)

    # Never followed or written through.
    with pytest.raises(UnsafePathError):
        load_issue_namespace(common, 13)
    with pytest.raises(UnsafePathError):
        write_binding(issue_namespace(common, 13), _binding(13))
    with pytest.raises(UnsafePathError):
        load_issue_namespace(common, 14)
    with pytest.raises(UnsafePathError):
        write_binding(issue_namespace(common, 14), _binding(14))
    assert sorted(p.name for p in elsewhere.iterdir()) == ["binding.json"]
    assert (issues / "14").read_text(encoding="utf-8") == "a regular file named like an issue"


def test_listing_is_bounded(common, monkeypatch):
    monkeypatch.setattr(rn, "MAX_ISSUE_NAMESPACES", 3)
    for n in (1, 2, 3):
        _bind(common, n)
    assert len(list_issue_namespaces(common).bound) == 3
    (common / "autoforge" / "scheduler" / "issues" / "junk").mkdir()
    with pytest.raises(NamespaceError, match="more than 3 entries"):
        list_issue_namespaces(common)


def test_the_default_listing_bound_is_ten_thousand():
    assert rn.MAX_ISSUE_NAMESPACES == 10_000


# -- archive ------------------------------------------------------------------------


def _archived_names(ns: IssueNamespace) -> list[str]:
    return sorted(p.name for p in ns.state_dir.iterdir() if ".archived-" in p.name)


def test_archive_moves_the_state_aside_and_keeps_everything_else(common, monkeypatch):
    monkeypatch.setattr(state_mod, "datetime", FrozenDatetime)
    ns = _bind(common, 11)
    ns.claim_file.write_text("{}", encoding="utf-8")
    ns.run_lock_file.write_bytes(b"")
    ns.workers_dir.mkdir()
    (ns.workers_dir / "1.log").write_text("worker", encoding="utf-8")
    _save_in(ns, _state(11))
    (ns.state_dir / "logs" / "af-issue-11").mkdir(parents=True)
    (ns.state_dir / "logs" / "af-issue-11" / "events.jsonl").write_text("{}\n", "utf-8")
    worktree = common / "autoforge" / "worktrees" / "11"
    worktree.mkdir(parents=True)
    (worktree / "file").write_text("work", encoding="utf-8")
    state_bytes = ns.state_file.read_bytes()

    def kept() -> dict[str, tuple]:
        """The logs, worker logs, binding, claim, lock and worktree: every other file."""
        tree = _tree(common / "autoforge")
        return {k: v for k, v in tree.items() if v[0] == "file" and "state.json" not in k}

    before = kept()
    assert len(before) == 6
    moved = archive_issue_state(ns)
    assert moved == ns.state_dir / "state.json.archived-20261017T120000Z"
    assert moved.read_bytes() == state_bytes
    assert not os.path.lexists(ns.state_file)
    assert kept() == before
    found = load_issue_namespace(common, 11)
    assert found.kind is NamespaceKind.BOUND and found.state.status is StateStatus.ABSENT

    # Idempotent: nothing left to archive, nothing changed.
    before = _tree(common / "autoforge")
    assert archive_issue_state(ns) is None
    assert _tree(common / "autoforge") == before


def test_archive_with_no_state_directory_creates_nothing(common):
    ns = _bind(common, 11)
    before = _tree(common / "autoforge")
    assert archive_issue_state(ns) is None
    assert archive_issue_state(issue_namespace(common, 12)) is None
    assert _tree(common / "autoforge") == before


def test_a_crash_between_link_and_unlink_keeps_both_names_and_a_rerun_completes(
    common, monkeypatch
):
    monkeypatch.setattr(state_mod, "datetime", FrozenDatetime)
    ns = _bind(common, 11)
    _save_in(ns, _state(11))
    state_bytes = ns.state_file.read_bytes()

    class Crash(BaseException):
        pass

    real_unlink = SafeRoot.unlink

    def crash_on_the_state(self, relpath):
        if relpath == "state.json":
            raise Crash
        return real_unlink(self, relpath)

    monkeypatch.setattr(SafeRoot, "unlink", crash_on_the_state)
    with pytest.raises(Crash):
        archive_issue_state(ns)
    assert ns.state_file.read_bytes() == state_bytes
    assert _archived_names(ns) == ["state.json.archived-20261017T120000Z"]

    monkeypatch.setattr(SafeRoot, "unlink", real_unlink)
    moved = archive_issue_state(ns)
    assert moved == ns.state_dir / "state.json.archived-20261017T120000Z.1"
    assert not os.path.lexists(ns.state_file)
    assert _archived_names(ns) == [
        "state.json.archived-20261017T120000Z",
        "state.json.archived-20261017T120000Z.1",
    ]
    for name in _archived_names(ns):
        assert (ns.state_dir / name).read_bytes() == state_bytes
    assert archive_issue_state(ns) is None


def test_archive_leaves_corrupt_quarantine_behaviour_unchanged(tmp_path, monkeypatch):
    """The shared primitive keeps `run --force`'s suffix and wording."""
    monkeypatch.setattr(state_mod, "datetime", FrozenDatetime)
    p = tmp_path / "state.json"
    p.write_text("garbage", encoding="utf-8")
    assert state_mod.quarantine_state_file(p) == tmp_path / "state.json.corrupt-20261017T120000Z"
    with pytest.raises(state_mod.StateError, match="cannot move corrupted state file .* gone"):
        state_mod.quarantine_state_file(p)
    with pytest.raises(state_mod.StateError, match=r"cannot move state file .* gone"):
        state_mod.archive_state_file(p)


# -- the engine entry point ---------------------------------------------------------


def test_binding_an_engine_to_a_namespace_runs_it_there(common, tmp_path):
    ns = _bind(common, 11)
    eng = make_engine(tmp_path / ".autoforge", [], workdir=common.parent)
    eng.bind_issue_namespace(ns)
    assert eng.paths == ns.state_paths()
    eng.new_run(EPIC, _issue_url(11))
    eng.save()
    assert ns.state_file.is_file() and not (tmp_path / ".autoforge").exists()
    assert eng.coordination_root() == common
    eng.close()


def test_an_engine_refuses_another_repositorys_namespace(common, tmp_path):
    other = Path(os.path.realpath(git_repo(tmp_path / "other") / ".git"))
    eng = make_engine(tmp_path / ".autoforge", [], workdir=common.parent)
    before = eng.paths
    with pytest.raises(NamespaceError, match="not under this repository"):
        eng.bind_issue_namespace(issue_namespace(other, 11))
    assert eng.paths == before


def test_an_engine_that_is_not_bound_is_unchanged(tmp_path):
    eng = make_engine(tmp_path / ".autoforge", [])
    assert eng.paths.state_dir == tmp_path / ".autoforge"
    assert eng.paths.anchor == tmp_path and eng.paths.relative == (".autoforge",)


def test_the_issue_worktree_keeps_the_engines_derivation(common, tmp_path):
    eng = make_engine(tmp_path / ".autoforge", [], workdir=common.parent)
    assert eng.issue_worktree_path(2) == eng.agent_worktree_path()  # the run is on ISSUE (#2)
    assert ISSUE.endswith("/2")
    assert eng.issue_worktree_path(11) == common / "autoforge" / "worktrees" / "11"
    cfg = default_config()
    cfg.execution.worktree_dir = str(tmp_path / "trees")
    configured = make_engine(tmp_path / ".autoforge", [], cfg=cfg, workdir=common.parent)
    assert configured.issue_worktree_path(11) == Path(os.path.realpath(tmp_path / "trees")) / "11"
    for bad in (0, -1, True):
        with pytest.raises(NamespaceError):
            eng.issue_worktree_path(bad)  # type: ignore[arg-type]


# -- the standalone refusal (the CLI's is in test_cli.py) ---------------------------


def test_a_state_dir_inside_the_scheduler_root_is_refused(common, tmp_path, monkeypatch):
    ns = issue_namespace(common, 1)
    for inside in (
        ns.state_dir,
        ns.directory,
        common / "autoforge" / "scheduler",
        common / "autoforge" / "scheduler" / "epics" / "5",
    ):
        with pytest.raises(SchedulerStateDirError, match="epic resume"):
            check_standalone_state_dir(inside, common, "run")
    link = tmp_path / "link"
    (common / "autoforge" / "scheduler").mkdir(parents=True)
    link.symlink_to(common / "autoforge" / "scheduler", target_is_directory=True)
    with pytest.raises(SchedulerStateDirError):
        check_standalone_state_dir(link / "issues" / "1" / "state", common, "resume")
    monkeypatch.chdir(common.parent)
    with pytest.raises(SchedulerStateDirError):
        check_standalone_state_dir(".git/autoforge/scheduler/issues/1/state", common, "step")
    for outside in (
        common.parent / ".autoforge",
        common / "autoforge" / "state",
        common / "autoforge" / "schedulers",
        common / "autoforge" / "worktrees" / "1",
    ):
        check_standalone_state_dir(outside, common, "run")
