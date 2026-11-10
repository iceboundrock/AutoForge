"""The engine's candidate source: the verify step, its receipts, the two reads (#269).

The verify step runs over real artifacts in real repositories
(:mod:`tests.test_candidate_artifact`'s helpers); the receipts live in a
``SafeRoot`` in ``tmp_path``, as the run's state directory holds them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoforge.agent_worktree import Candidate
from autoforge.candidate_source import (
    MAX_RECEIPT_LAUNCHES,
    NOT_EXPORTED,
    RECEIPTS_DIR,
    ArtifactCandidateSource,
    CandidateReceipts,
    ImportedArtifact,
    LaunchReceipt,
    WorktreeHead,
    bound_git_input,
    export_leaf,
    export_location,
)
from autoforge.errors import SandboxLaunchRefusedError, StateError
from autoforge.git_input import BoundRevision, LaunchGitInput
from autoforge.safefs import SafeRoot
from autoforge.sandbox_backend import (
    Artifact,
    LaunchHandle,
    LaunchInputs,
    LaunchOwner,
    LifecycleObject,
    ObjectKind,
    Seed,
)
from autoforge.sandbox_lifecycle import Exported, Verified
from tests.test_candidate_artifact import (
    Lock,
    Repos,
    commit,
    git,
    make_repos,
    make_work,
    pack_of,
    transport,
    write_artifact,
)

SEED = "1" * 40
BASE = "2" * 40
HEAD = "3" * 40


def _owner(phase: str = "FIX", launch: str = "FIX-1") -> LaunchOwner:
    return LaunchOwner(run="run-269", issue=269, phase=phase, launch=launch)


def _fix(launch: str = "FIX-1", carried: str = "") -> LaunchGitInput:
    return bound_git_input(
        "FIX",
        _owner(launch=launch),
        repository="owner/repo",
        object_format="sha1",
        revision=BoundRevision(SEED, BASE),
        carried=carried,
    )


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return make_repos(tmp_path)


@pytest.fixture
def work(repos: Repos) -> tuple[str, bytes]:
    return make_work(repos)


@pytest.fixture
def receipts(tmp_path: Path):
    root = SafeRoot.open(tmp_path / "state", create=True)
    try:
        yield CandidateReceipts(root)
    finally:
        root.close()


# -- each phase's bound revision ----------------------------------------------------------


def test_a_launch_whose_entry_bound_no_revision_is_refused():
    with pytest.raises(SandboxLaunchRefusedError, match="no revision its entry bound"):
        bound_git_input(
            "REVIEW", _owner("REVIEW"), repository="owner/repo", object_format="sha1", revision=None
        )


def test_a_revision_that_breaks_the_inputs_invariants_refuses_the_launch():
    with pytest.raises(SandboxLaunchRefusedError, match="lower boundary is its base"):
        bound_git_input(
            "ANALYZE_EXECUTE",
            _owner("ANALYZE_EXECUTE"),
            repository="owner/repo",
            object_format="sha1",
            revision=BoundRevision(SEED, BASE),
        )


@pytest.mark.parametrize("phase", ["REVIEW", "UPDATE_EPIC"])
def test_a_phase_that_exports_nothing_carries_no_commit(phase):
    lower = BASE if phase == "REVIEW" else SEED
    git_input = bound_git_input(
        phase,
        _owner(phase),
        repository="owner/repo",
        object_format="sha1",
        revision=BoundRevision(SEED, lower),
        carried=HEAD,
    )
    assert git_input.carried == "" and git_input.seed == SEED and git_input.lower == lower


def test_an_export_command_seeds_from_the_commit_it_validates_alone():
    git_input = bound_git_input(
        "REVIEW",
        _owner("REVIEW"),
        repository="owner/repo",
        object_format="sha1",
        revision=BoundRevision(HEAD, ""),
        carried=SEED,
        export_command=True,
    )
    assert (git_input.seed, git_input.lower, git_input.carried) == (HEAD, "", "")


# -- the receipts ---------------------------------------------------------------------------


def test_the_seed_identity_is_saved_and_read_back(receipts):
    git_input = _fix().with_seed_manifest("sha256:" + "4" * 64)
    receipts.bind(git_input)
    receipts.bind(git_input)  # idempotent
    assert receipts.launches(git_input.entry) == [LaunchReceipt(git_input)]
    assert receipts.carried(git_input.entry) == ""


def test_a_launch_id_bound_again_with_another_input_is_a_launch_of_its_own(receipts):
    """A launch is its whole input: the same id with another seed manifest (a
    launch id an earlier entry of the same revision used) never replaces it."""
    first = _fix()
    other = _fix().with_seed_manifest("sha256:" + "4" * 64)
    receipts.bind(first)
    receipts.bind(other)
    assert [item.git_input for item in receipts.launches(first.entry)] == [first, other]


def test_the_carried_commit_is_the_entrys_last_imported_candidate(receipts):
    first = _fix()
    receipts.bind(first)
    receipts.record(first, Verified(candidate=HEAD))
    receipts.record(first, Verified(candidate=HEAD))  # a save repeated after a crash
    assert receipts.carried(first.entry) == HEAD
    relaunch = _fix("FIX-2", carried=HEAD)
    assert relaunch.entry == first.entry
    later = "6" * 40
    receipts.record(relaunch, Verified(candidate=later))
    assert receipts.carried(first.entry) == later


def test_a_refused_launch_never_takes_away_the_commit_an_earlier_launch_imported(receipts):
    """R1-F6: an accepted launch, then a launch whose artifact is refused, then a
    relaunch: the relaunch carries the accepted launch's commit."""
    accepted = _fix()
    receipts.bind(accepted)
    receipts.record(accepted, Verified(candidate=HEAD))
    refused = _fix("FIX-2", carried=HEAD)
    receipts.bind(refused)
    receipts.record(refused, Verified(refusal="its pack fails --strict"))
    launches = receipts.launches(accepted.entry)
    assert [item.git_input.launch for item in launches] == ["FIX-1", "FIX-2"]
    assert launches[1].refusal == "its pack fails --strict" and not launches[1].accepted
    assert receipts.carried(accepted.entry) == HEAD
    relaunch = _fix("FIX-3", carried=receipts.carried(accepted.entry))
    assert relaunch.entry == accepted.entry and relaunch.carried == HEAD
    # A failed export is a refusal too, and takes nothing away either.
    receipts.record(relaunch, Verified(refusal="the launch's export failed: deadline"))
    assert receipts.carried(accepted.entry) == HEAD


def test_an_outcome_is_recorded_for_exactly_the_launch_its_input_names(receipts):
    bound = _fix()
    receipts.bind(bound)
    carrying = _fix(carried="5" * 40)
    receipts.record(carrying, Verified(candidate=HEAD))
    launches = receipts.launches(bound.entry)
    assert [item.git_input for item in launches] == [bound, carrying]
    assert not launches[0].verified and launches[1].accepted
    # Another outcome for the same launch replaces it, as the entry's latest.
    receipts.record(carrying, Verified(refusal="its pack fails --strict"))
    assert receipts.carried(bound.entry) == ""
    assert len(receipts.launches(bound.entry)) == 2


def test_entries_that_bind_the_same_revision_never_share_a_carried_commit(receipts):
    round_one = bound_git_input(
        "FIX",
        _owner(),
        repository="owner/repo",
        object_format="sha1",
        revision=BoundRevision(SEED, BASE),
        entry_label="fix-round-1",
    )
    receipts.record(round_one, Verified(candidate=HEAD))
    round_two = bound_git_input(
        "FIX",
        _owner(),
        repository="owner/repo",
        object_format="sha1",
        revision=BoundRevision(SEED, BASE),
        entry_label="fix-round-2",
    )
    assert round_two.entry != round_one.entry
    assert receipts.carried(round_one.entry) == HEAD
    assert receipts.carried(round_two.entry) == ""


def test_a_receipt_keeps_a_bounded_number_of_launches(receipts):
    for index in range(MAX_RECEIPT_LAUNCHES + 5):
        receipts.record(_fix(f"FIX-{index}"), Verified(candidate=HEAD))
    launches = receipts.launches(_fix().entry)
    assert len(launches) == MAX_RECEIPT_LAUNCHES
    assert launches[-1].git_input.launch == f"FIX-{MAX_RECEIPT_LAUNCHES + 4}"


def test_the_bound_never_drops_the_latest_accepted_launch(receipts):
    """Refused launches past the bound drop the oldest refusals, never the commit
    the entry carries."""
    receipts.record(_fix("FIX-0"), Verified(candidate=HEAD))
    for index in range(1, MAX_RECEIPT_LAUNCHES + 5):
        receipts.record(_fix(f"FIX-{index}", carried=HEAD), Verified(refusal="refused"))
    launches = receipts.launches(_fix().entry)
    assert len(launches) == MAX_RECEIPT_LAUNCHES
    assert launches[0].git_input.launch == "FIX-0" and launches[0].accepted
    assert launches[-1].git_input.launch == f"FIX-{MAX_RECEIPT_LAUNCHES + 4}"
    assert receipts.carried(_fix().entry) == HEAD


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"{}",
        json.dumps({"format": 2, "entry": "x", "launches": []}).encode(),
        json.dumps({"format": 1, "entry": "other", "launches": []}).encode(),
    ],
)
def test_a_receipt_that_does_not_parse_is_refused_never_read_as_none(receipts, tmp_path, content):
    entry = _fix().entry
    (tmp_path / "state" / RECEIPTS_DIR).mkdir(parents=True)
    (tmp_path / "state" / RECEIPTS_DIR / f"{entry}.json").write_bytes(content)
    with pytest.raises(StateError):
        receipts.carried(entry)


def test_a_receipt_holding_another_entrys_launch_is_refused(receipts, tmp_path):
    other = _fix()
    receipts.record(other, Verified(candidate=HEAD))
    path = tmp_path / "state" / RECEIPTS_DIR / f"{other.entry}.json"
    data = json.loads(path.read_text())
    data["launches"][0]["git_input"]["seed"] = "5" * 40
    path.write_text(json.dumps(data))
    with pytest.raises(StateError, match="another entry's launch"):
        receipts.launches(other.entry)


def test_a_receipt_reached_through_a_link_is_refused(receipts, tmp_path):
    entry = _fix().entry
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}")
    (tmp_path / "state" / RECEIPTS_DIR).mkdir(parents=True)
    (tmp_path / "state" / RECEIPTS_DIR / f"{entry}.json").symlink_to(elsewhere)
    with pytest.raises(StateError):
        receipts.launches(entry)


# -- the verify step ------------------------------------------------------------------------


def _artifact_input(repos: Repos, launch: str = "ANALYZE_EXECUTE-1", carried: str = ""):
    return bound_git_input(
        "ANALYZE_EXECUTE",
        _owner("ANALYZE_EXECUTE", launch),
        repository="owner/repo",
        object_format="sha1",
        revision=BoundRevision(repos.seed, repos.seed),
        carried=carried,
    )


EXPORT_ROOT = "autoforge/sandbox/269/export"


def _source(repos: Repos, receipts: CandidateReceipts, lock: Lock | None = None):
    return ArtifactCandidateSource(
        repos.common,
        None,
        read_commit=transport(repos).read_local_commit,
        receipts=receipts,
        export_root=EXPORT_ROOT,
        git_lock=lock or Lock(),
    )


def _export_dir(repos: Repos, launch: str) -> Path:
    """A fresh export directory for ``launch``, where the engine makes one (#269)."""
    root = repos.common / EXPORT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return Path(export_location(root, launch))


def _handle(
    git_input: LaunchGitInput | None, owner: LaunchOwner | None = None, export_dir: str = ""
) -> LaunchHandle:
    owner = owner or _owner("ANALYZE_EXECUTE", git_input.launch if git_input else "x")
    inputs = LaunchInputs(owner, workspace="/unused", git_input=git_input, export_dir=export_dir)
    return LaunchHandle(inputs, Seed())


def _journaled(owner: LaunchOwner, directory: str) -> LifecycleObject:
    """The export directory the lifecycle journaled for ``owner``'s launch."""
    return LifecycleObject(owner, ObjectKind.EXPORT_DIR, directory, "n-out")


def _exported(
    repos: Repos,
    git_input: LaunchGitInput | None,
    head: str,
    pack: bytes,
    *,
    owner: LaunchOwner | None = None,
    written_for: LaunchGitInput | None = None,
) -> tuple[Exported, LaunchHandle]:
    """An artifact the launch's exporter left in its own export directory, and its handle.

    ``written_for`` is the input the manifest names, the launch's own by default.
    """
    owner = owner or _owner("ANALYZE_EXECUTE", git_input.launch if git_input else "x")
    directory = _export_dir(repos, owner.launch)
    manifest_input = written_for or git_input
    assert manifest_input is not None
    write_artifact(directory, manifest_input, head, pack)
    exported = Exported(Artifact(str(directory)), origin=_journaled(owner, str(directory)))
    return exported, _handle(git_input, owner, str(directory))


def test_a_valid_artifact_is_the_candidate_and_its_outcome_is_saved(
    repos, work, receipts, tmp_path
):
    head, pack = work
    git_input = _artifact_input(repos)
    exported, handle = _exported(repos, git_input, head, pack)
    source = _source(repos, receipts)
    outcome = source.verify(exported, handle)
    assert outcome == Verified(candidate=head)
    source.save(outcome)
    assert receipts.carried(git_input.entry) == head
    # The candidate read takes that commit and no other.
    assert ImportedArtifact(outcome).read() == Candidate(False, head, "")


def test_a_crash_before_the_save_reverifies_to_the_same_outcome(repos, work, receipts, tmp_path):
    """The volume and the artifact stay until the outcome is saved: verifying
    again after a crash before the save imports nothing new and accepts the same
    commit; saving it twice changes nothing."""
    head, pack = work
    git_input = _artifact_input(repos)
    exported, handle = _exported(repos, git_input, head, pack)
    first = _source(repos, receipts).verify(exported, handle)
    # (the controller dies here, before the save)
    lock = Lock()
    source = _source(repos, receipts, lock)
    again = source.verify(exported, handle)
    assert again == first == Verified(candidate=head) and lock.taken == []
    source.save(again)
    source.save(again)
    assert len(receipts.launches(git_input.entry)) == 1


def test_a_refused_artifact_is_saved_as_a_refusal_and_never_carried(
    repos, work, receipts, tmp_path
):
    head, pack = work
    git_input = _artifact_input(repos)
    other = _artifact_input(repos, "ANALYZE_EXECUTE-7")
    exported, handle = _exported(repos, git_input, head, pack, written_for=other)
    source = _source(repos, receipts)
    outcome = source.verify(exported, handle)
    assert not outcome.accepted and "its manifest names launch" in outcome.refusal
    source.save(outcome)
    assert receipts.carried(git_input.entry) == ""
    read = ImportedArtifact(outcome).read()
    assert read.head == "" and "its manifest names launch" in read.problem


def test_a_handle_carrying_another_launchs_input_is_refused(repos, work, receipts, tmp_path):
    head, pack = work
    git_input = _artifact_input(repos)
    owner = _owner("ANALYZE_EXECUTE", "ANALYZE_EXECUTE-2")
    exported, handle = _exported(repos, git_input, head, pack, owner=owner)
    outcome = _source(repos, receipts).verify(exported, handle)
    assert "carries the git input of launch ANALYZE_EXECUTE-1" in outcome.refusal
    assert not repos.has(head)


def test_a_launch_with_no_git_input_has_its_export_refused(repos, work, receipts, tmp_path):
    head, pack = work
    exported, handle = _exported(
        repos, None, head, pack, written_for=_artifact_input(repos), owner=_owner("ANALYZE_EXECUTE")
    )
    source = _source(repos, receipts)
    outcome = source.verify(exported, handle)
    assert "bound no git input" in outcome.refusal and not repos.has(head)
    source.save(outcome)  # nothing to bind it to


# -- the artifact's origin (R1-F2) ------------------------------------------------------


def _refused_unread(repos: Repos, receipts, exported, handle, match: str, head: str) -> None:
    """Refused before the artifact was read: nothing published, the refusal saved."""
    source = _source(repos, receipts)
    outcome = source.verify(exported, handle)
    assert not outcome.accepted and match in outcome.refusal, outcome.refusal
    assert "nothing was read or published" in outcome.refusal
    assert not repos.has(head)
    source.save(outcome)
    git_input = handle.inputs.git_input
    (saved,) = receipts.launches(git_input.entry)
    assert saved.verified and saved.refusal == outcome.refusal
    assert receipts.carried(git_input.entry) == ""


def test_a_correctly_labelled_artifact_from_another_location_is_refused(
    repos, work, receipts, tmp_path
):
    """The manifest names exactly this launch, but the artifact is not in the launch's
    export directory: it is never read."""
    head, pack = work
    git_input = _artifact_input(repos)
    owner = _owner("ANALYZE_EXECUTE", git_input.launch)
    elsewhere = write_artifact(tmp_path / "planted", git_input, head, pack)
    export_dir = _export_dir(repos, owner.launch)
    export_dir.mkdir()
    exported = Exported(Artifact(str(elsewhere)), origin=_journaled(owner, str(export_dir)))
    handle = _handle(git_input, owner, str(export_dir))
    _refused_unread(repos, receipts, exported, handle, "not in the launch's export directory", head)


def test_a_correctly_labelled_artifact_in_another_launchs_export_directory_is_refused(
    repos, work, receipts
):
    """Launch 2's manifest, planted in launch 1's export directory: the journaled
    origin is launch 1's, not the directory the controller chose for launch 2."""
    head, pack = work
    first = _artifact_input(repos, "ANALYZE_EXECUTE-1")
    second = _artifact_input(repos, "ANALYZE_EXECUTE-2")
    planted, _ = _exported(repos, first, head, pack, written_for=second)
    owner = _owner("ANALYZE_EXECUTE", second.launch)
    own = _export_dir(repos, owner.launch)
    own.mkdir()
    handle = _handle(second, owner, str(own))
    # As the export step reported it: launch 1's directory, journaled by launch 1.
    _refused_unread(
        repos, receipts, planted, handle, "not in an export directory the launch's lifecycle", head
    )
    # Journaled for launch 2, but named after launch 1's directory.
    renamed = Exported(planted.artifact, origin=_journaled(owner, planted.artifact.directory))
    outcome = _source(repos, receipts).verify(renamed, handle)
    assert "not the one the controller chose for it" in outcome.refusal and not repos.has(head)


def test_an_export_directory_the_controller_never_makes_for_the_launch_is_refused(
    repos, work, receipts
):
    """A handle whose export directory is launch 1's (an engine or backend defect)
    still never reads launch 1's artifact for launch 2."""
    head, pack = work
    first = _artifact_input(repos, "ANALYZE_EXECUTE-1")
    second = _artifact_input(repos, "ANALYZE_EXECUTE-2")
    planted, _ = _exported(repos, first, head, pack, written_for=second)
    owner = _owner("ANALYZE_EXECUTE", second.launch)
    directory = planted.artifact.directory
    exported = Exported(planted.artifact, origin=_journaled(owner, directory))
    handle = _handle(second, owner, directory)
    _refused_unread(repos, receipts, exported, handle, "is not one the controller makes", head)


def test_an_artifact_with_no_journaled_origin_is_refused(repos, work, receipts):
    head, pack = work
    git_input = _artifact_input(repos)
    exported, handle = _exported(repos, git_input, head, pack)
    unjournaled = Exported(exported.artifact)
    _refused_unread(repos, receipts, unjournaled, handle, "lifecycle journaled for it", head)


def test_an_export_directory_reached_through_a_link_is_refused(repos, work, receipts, tmp_path):
    """The export root is a link to a directory holding a correctly labelled artifact
    under the right name: opened one component at a time, it is refused."""
    head, pack = work
    git_input = _artifact_input(repos)
    owner = _owner("ANALYZE_EXECUTE", git_input.launch)
    root = repos.common / EXPORT_ROOT
    root.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root.symlink_to(elsewhere, target_is_directory=True)
    directory = export_location(root, owner.launch)
    write_artifact(Path(directory), git_input, head, pack)
    assert export_leaf(root, directory, owner.launch)
    exported = Exported(Artifact(directory), origin=_journaled(owner, directory))
    handle = _handle(git_input, owner, directory)
    _refused_unread(repos, receipts, exported, handle, "without following a link", head)


def test_export_locations_are_fresh_per_launch_and_named_for_it(tmp_path):
    first = export_location(tmp_path, "FIX-1")
    again = export_location(tmp_path, "FIX-1")
    assert first != again
    assert export_leaf(tmp_path, first, "FIX-1") == Path(first).name
    assert export_leaf(tmp_path, first, "FIX-2") == ""
    assert export_leaf(tmp_path / "other", first, "FIX-1") == ""
    assert export_leaf(tmp_path, str(tmp_path / "FIX-1"), "FIX-1") == ""
    assert export_leaf(tmp_path, str(tmp_path / "FIX-1-x" / "y"), "FIX-1") == ""


# -- a failed export (R1-F7) -------------------------------------------------------------


def test_a_failed_export_is_saved_as_the_launchs_refusal(repos, receipts, tmp_path):
    """The verify step is asked about a failed export too, and its refusal is bound
    to the launch's git input, so the save records it before the volume goes."""
    git_input = _artifact_input(repos)
    owner = _owner("ANALYZE_EXECUTE", git_input.launch)
    directory = str(_export_dir(repos, owner.launch))
    exported = Exported(
        failure="fake: the exporter overran the export bound", origin=_journaled(owner, directory)
    )
    source = _source(repos, receipts)
    outcome = source.verify(exported, _handle(git_input, owner, directory))
    assert outcome.refusal == (
        "the launch's export failed: fake: the exporter overran the export bound"
    )
    source.save(outcome)
    with SafeRoot.open(tmp_path / "state") as root:  # read back as a new process would
        (saved,) = CandidateReceipts(root).launches(git_input.entry)
    assert saved.git_input == git_input and saved.verified and saved.refusal == outcome.refusal
    assert ImportedArtifact(outcome).read().problem == outcome.refusal


def test_a_save_of_another_outcome_than_the_verified_one_is_refused(
    repos, work, receipts, tmp_path
):
    head, pack = work
    git_input = _artifact_input(repos)
    exported, handle = _exported(repos, git_input, head, pack)
    source = _source(repos, receipts)
    source.verify(exported, handle)
    with pytest.raises(StateError, match="not the one the verify step returned"):
        source.save(Verified(candidate=repos.seed))


def test_a_relaunch_carries_the_committed_head_never_uncommitted_files(repos, receipts, tmp_path):
    """The agent commits once and leaves a file uncommitted: the relaunch's
    carried commit is the imported commit, and the file is in no artifact."""
    committed = commit(repos.agent, "done.py")
    (repos.agent / "scratch.txt").write_text("uncommitted")
    git_input = _artifact_input(repos)
    pack = pack_of(repos, [committed, f"^{repos.seed}"])
    exported, handle = _exported(repos, git_input, committed, pack)
    source = _source(repos, receipts)
    source.save(source.verify(exported, handle))
    carried = receipts.carried(git_input.entry)
    assert carried == committed
    relaunch = _artifact_input(repos, "ANALYZE_EXECUTE-2", carried=carried)
    assert relaunch.entry == git_input.entry and relaunch.carried == committed
    listing = git(repos.store, "ls-tree", "-r", "--name-only", carried).split()
    assert "done.py" in listing and "scratch.txt" not in listing


# -- the candidate read -------------------------------------------------------------------


def test_the_worktree_read_is_bwraps_unchanged():
    calls: list[str] = []

    def read_head() -> Candidate:
        calls.append("read")
        return Candidate(False, HEAD)

    assert WorktreeHead("", read_head).read() == Candidate(False, HEAD)
    failed = WorktreeHead("the strict import failed", read_head).read()
    assert failed == Candidate(False, "", "the strict import failed")
    assert calls == ["read"]


def test_the_imported_read_is_the_verified_commit_and_no_other():
    assert ImportedArtifact(Verified(candidate=HEAD)).read() == Candidate(False, HEAD, "")
    refused = ImportedArtifact(Verified(refusal="over the bound")).read()
    assert refused == Candidate(False, "", "over the bound")
    unnamed = ImportedArtifact(Verified()).read()
    assert unnamed.head == "" and unnamed.problem == "the verify step named no candidate"


def test_a_seeded_launch_no_verify_step_judged_has_no_candidate():
    """R3-F1: no outcome is NOT_EXPORTED's refusal, never a read of the worktree."""
    missing = ImportedArtifact(None).read()
    assert missing == Candidate(False, "", NOT_EXPORTED.refusal)
    assert not NOT_EXPORTED.accepted and NOT_EXPORTED.candidate == ""
