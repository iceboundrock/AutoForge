"""A launch's git input: its invariants, its entry, its persisted identity (ADR 0013 D4; #269).

No git and no runtime: the record is plain data, and the seed manifest's
digest is read from files in ``tmp_path``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os

import pytest

from autoforge.git_input import (
    DIGEST_PREFIX,
    MAX_SEED_MANIFEST_BYTES,
    SEED_MANIFEST_NAME,
    GitInputError,
    LaunchGitInput,
    seed_manifest_digest,
    seed_problem,
)

SEED = "1" * 40
BASE = "2" * 40
CARRIED = "3" * 40
DIGEST = DIGEST_PREFIX + "4" * 64


def _input(**changes) -> LaunchGitInput:
    fields = {
        "phase": "FIX",
        "run": "af-20261110T000000Z-abcdef",
        "issue": 269,
        "launch": "FIX-1",
        "repository": "owner/repo",
        "object_format": "sha1",
        "seed": SEED,
        "lower": BASE,
    }
    fields.update(changes)
    return LaunchGitInput(**fields)


def test_a_bound_input_holds_each_of_its_fields():
    git_input = _input(carried=CARRIED, seed_manifest_digest=DIGEST, entry_label="fix-round-1")
    assert git_input.identity() == {
        "phase": "FIX",
        "run": "af-20261110T000000Z-abcdef",
        "issue": 269,
        "launch": "FIX-1",
        "repository": "owner/repo",
        "object_format": "sha1",
        "seed": SEED,
        "lower": BASE,
        "carried": CARRIED,
        "seed_manifest_digest": DIGEST,
        "export_command": False,
        "entry_label": "fix-round-1",
    }
    assert LaunchGitInput.from_identity(git_input.identity()) == git_input


def test_the_input_is_immutable():
    git_input = _input()
    with pytest.raises(dataclasses.FrozenInstanceError):
        git_input.seed = BASE  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"phase": "MERGE"}, "not a phase that binds"),
        ({"phase": "ANALYZE_EXECUTE"}, "lower boundary is its base"),
        ({"run": ""}, "its run"),
        ({"run": "a b"}, "its run"),
        ({"launch": "../x"}, "its launch"),
        ({"entry_label": "round 1"}, "its entry label"),
        ({"issue": 0}, "its issue"),
        ({"issue": True}, "its issue"),
        ({"repository": "owner"}, "owner/name"),
        ({"repository": "owner/../x"}, "owner/name"),
        ({"object_format": "md5"}, "object format"),
        ({"seed": "abc"}, "its seed"),
        ({"seed": "A" * 40}, "its seed"),
        ({"object_format": "sha256"}, "its seed"),
        ({"lower": ""}, "lower boundary"),
        ({"phase": "REVIEW", "carried": CARRIED}, "carries no commit"),
        ({"phase": "UPDATE_EPIC", "lower": SEED, "carried": CARRIED}, "carries no commit"),
        ({"carried": SEED}, "the seed itself"),
        ({"carried": "x" * 40}, "carried commit"),
        ({"seed_manifest_digest": "md5:00"}, "sha256:<hex>"),
        ({"export_command": True}, "no lower boundary"),
        ({"export_command": True, "lower": "", "carried": CARRIED}, "no carried commit"),
    ],
)
def test_an_input_that_breaks_an_invariant_is_refused(changes, why):
    with pytest.raises(GitInputError, match=why):
        _input(**changes)


@pytest.mark.parametrize("phase", ["ANALYZE_EXECUTE", "REPLAN_REEXECUTE", "UPDATE_EPIC"])
def test_a_phase_that_seeds_from_its_base_binds_the_base_as_its_lower_boundary(phase):
    assert _input(phase=phase, lower=SEED).lower == SEED


def test_an_export_command_binds_the_commit_it_validates_with_no_history():
    git_input = _input(phase="REVIEW", lower="", export_command=True)
    assert git_input.seed == SEED and git_input.lower == "" and git_input.carried == ""


def test_the_seed_manifest_is_bound_once():
    git_input = _input()
    bound = git_input.with_seed_manifest(DIGEST)
    assert bound.seed_manifest_digest == DIGEST and git_input.seed_manifest_digest == ""
    assert bound.with_seed_manifest(DIGEST) == bound
    with pytest.raises(GitInputError, match="already binds the seed manifest"):
        bound.with_seed_manifest(DIGEST_PREFIX + "5" * 64)


def test_every_relaunch_of_an_entry_shares_the_entry_and_nothing_else_does():
    first = _input()
    relaunch = _input(launch="FIX-2", carried=CARRIED, seed_manifest_digest=DIGEST)
    assert relaunch.entry == first.entry
    for other in (
        _input(seed=CARRIED),
        _input(lower=CARRIED),
        _input(run="af-other"),
        _input(issue=270),
        _input(phase="REVIEW"),
        _input(phase="REVIEW", lower="", export_command=True),
        # Another entry that binds the same revision: a second fix round at a
        # HEAD the first did not move.
        _input(entry_label="fix-round-2"),
    ):
        assert other.entry != first.entry


def test_the_digest_covers_the_whole_identity():
    digests = {
        _input().digest,
        _input(launch="FIX-2").digest,
        _input(carried=CARRIED).digest,
        _input(seed_manifest_digest=DIGEST).digest,
    }
    assert len(digests) == 4 and all(d.startswith(DIGEST_PREFIX) for d in digests)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"phase": "FIX"},
        {**_input().identity(), "extra": 1},
        {**_input().identity(), "issue": "269"},
        {**_input().identity(), "export_command": 0},
    ],
)
def test_a_saved_identity_is_read_back_strictly(data):
    with pytest.raises(GitInputError):
        LaunchGitInput.from_identity(data)


# -- the seed manifest's digest (the hand-off) ------------------------------------------


def _seed_dir(tmp_path, content: bytes = b'{"seed": "1"}') -> str:
    directory = tmp_path / "seed"
    directory.mkdir()
    (directory / SEED_MANIFEST_NAME).write_bytes(content)
    return str(directory)


def test_the_digest_is_the_manifests_sha256(tmp_path):
    directory = _seed_dir(tmp_path)
    expected = DIGEST_PREFIX + hashlib.sha256(b'{"seed": "1"}').hexdigest()
    assert seed_manifest_digest(directory) == expected
    assert seed_problem(directory, _input(seed_manifest_digest=expected)) == ""


def test_a_swapped_manifest_is_not_the_bound_seed(tmp_path):
    directory = _seed_dir(tmp_path)
    bound = _input(seed_manifest_digest=seed_manifest_digest(directory))
    (tmp_path / "seed" / SEED_MANIFEST_NAME).write_bytes(b'{"seed": "1", "tags": ["v9"]}')
    assert "which the launch's git input binds" in seed_problem(directory, bound)


def test_an_input_without_a_manifest_digest_hands_on_no_seed(tmp_path):
    directory = _seed_dir(tmp_path)
    assert "binds no seed manifest" in seed_problem(directory, _input())
    assert "binds no seed manifest" in seed_problem(directory, None)


def test_a_manifest_reached_through_a_link_is_refused(tmp_path):
    directory = tmp_path / "seed"
    directory.mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (directory / SEED_MANIFEST_NAME).symlink_to(target)
    with pytest.raises(OSError):
        seed_manifest_digest(str(directory))
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(_seed_dir(real))
    with pytest.raises(OSError):
        seed_manifest_digest(str(linked))


def test_a_manifest_that_is_a_fifo_is_refused_without_blocking(tmp_path):
    directory = tmp_path / "seed"
    directory.mkdir()
    os.mkfifo(directory / SEED_MANIFEST_NAME)
    with pytest.raises(OSError, match="not a regular file"):
        seed_manifest_digest(str(directory))


def test_an_oversized_manifest_is_refused_before_it_is_read(tmp_path):
    directory = tmp_path / "seed"
    directory.mkdir()
    with open(directory / SEED_MANIFEST_NAME, "wb") as handle:
        handle.truncate(MAX_SEED_MANIFEST_BYTES + 1)
    with pytest.raises(OSError, match="over the bound"):
        seed_manifest_digest(str(directory))
