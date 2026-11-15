"""ADR 0014's prototype of a remote execution sandbox, against a fake provider (#276).

Real git on real repositories in ``tmp_path``, and no provider: the store
stands in for the shared object store, and a repository built from nothing
but the uploaded seed pack stands in for the remote's workspace. The only
channel between the two is :class:`tests.remote_sandbox_prototype.FakeRemote`'s
uploads and downloads, and the candidate comes back through the package's
real strict import. Every other test injects one interruption, one expiry
or one recovery case and checks that it fails closed: nothing imported,
nothing left in the export directory, nothing removed that is not proven
the run's, no entry closed on an unverified removal.

The last tests keep ADR 0014 honest: the tests it names exist, it claims no
state protocol number, its links resolve, it and ADR 0013 agree that a
remote endpoint stays refused, and the package never imports this
prototype.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

import autoforge
from autoforge.agent_worktree import ObjectImportError
from autoforge.candidate_artifact import (
    ARTIFACT_MANIFEST,
    ARTIFACT_PACK,
    ArtifactLimits,
    CandidateManifest,
)
from autoforge.errors import (
    ConfigurationError,
    SandboxCleanupUncertainError,
    SandboxLaunchRefusedError,
)
from autoforge.git_input import SEED_MANIFEST_NAME, LaunchGitInput, seed_manifest_digest
from tests import remote_sandbox_prototype as proto
from tests.conftest import isolated_git
from tests.remote_sandbox_prototype import (
    CreateRequest,
    FakeRemote,
    RemoteGone,
    RemoteIdentity,
)
from tests.test_candidate_artifact import Repos, _input, commit, count_of, make_repos, run_import
from tests.test_container_backend_record import FENCE, LINK, PROTOCOL, _broken

ROOT = Path(__file__).resolve().parents[1]
ADR_0014 = ROOT / "docs" / "adr" / "0014-remote-execution-sandboxes-contract-and-no-go.md"
ADR_0013 = (
    ROOT
    / "docs"
    / "adr"
    / "0013-container-sandbox-backend-lifecycle-trust-boundaries-and-rollout.md"
)

HOUR = 3600.0
VERCEL_CREDENTIALS = ("VERCEL_TOKEN", "VERCEL_OIDC_TOKEN")


def git(repo: Path, *args: str, input: bytes | None = None) -> str:
    return isolated_git("-C", str(repo), *args, input=input).decode().strip()


# -- the scene: a bound seed, a remote, an export directory -----------------------------


class Scene:
    """One remote launch of ``ANALYZE_EXECUTE``: its bound seed, its resource and journal."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.repos: Repos = make_repos(tmp_path)
        self.seed_dir = tmp_path / "seed"
        self.seed_dir.mkdir(mode=0o700)
        pack = isolated_git(
            f"--git-dir={self.repos.common}", "pack-objects", "--revs", "--stdout", "-q",
            input=f"{self.repos.seed}\n".encode(),
        )  # fmt: skip
        (self.seed_dir / proto.SEED_PACK).write_bytes(pack)
        unbound = _input(self.repos)
        manifest = {
            "schema": 1,
            "identity": unbound.identity(),
            "shallow": [],
            "tags": {},
            "pack_sha256": hashlib.sha256(pack).hexdigest(),
        }
        (self.seed_dir / SEED_MANIFEST_NAME).write_text(json.dumps(manifest, sort_keys=True))
        self.git_input: LaunchGitInput = unbound.with_seed_manifest(
            seed_manifest_digest(str(self.seed_dir))
        )
        self.remote = FakeRemote()
        self.identity = RemoteIdentity.new(
            self.git_input,
            provider="fake",
            scope=self.remote.reported_scope,
            ceiling=self.remote.now + 2 * HOUR,
            window=600.0,
            nonce="0" * 31 + "1",
        )
        self.journal: list[bytes] = []
        self.export = tmp_path / "export"
        self.export.mkdir(mode=0o700)

    def create(self, request: CreateRequest | None = None) -> float:
        observed = proto.create_remote(
            self.remote,
            self.identity,
            request or CreateRequest(env=("ANTHROPIC_API_KEY",)),
            write_entry=self.journal.append,
            provider_credential_names=VERCEL_CREDENTIALS,
        )
        return observed.deadline

    def stage_and_work(self) -> tuple[Path, str]:
        """The remote side: a repository from the uploaded pack alone, two commits on it."""
        work = self.tmp / "remote-workspace"
        work.mkdir()
        git(work, "init", "-q")
        uploaded = self.remote.stored(self.identity.name, proto.SEED_PACK)
        git(work, "index-pack", "--stdin", "--strict", input=uploaded)
        git(work, "checkout", "-q", "--detach", self.repos.seed)
        commit(work, "feature.py", "print('remote')")
        head = commit(work, "tests.py", "assert True")
        return work, head

    def export_from(self, work: Path, head: str, **manifest_changes) -> bytes:
        """The remote exporter: the commits above the seed, and their manifest."""
        pack = isolated_git(
            "-C", str(work), "pack-objects", "--revs", "--stdout", "-q",
            input=f"{head}\n^{self.repos.seed}\n".encode(),
        )  # fmt: skip
        manifest = CandidateManifest.for_input(
            self.git_input,
            candidate=head,
            pack_bytes=len(pack),
            pack_objects=count_of(pack),
            pack_sha256=hashlib.sha256(pack).hexdigest(),
        )
        manifest = replace(manifest, **manifest_changes)
        self.remote.store(self.identity.name, ARTIFACT_MANIFEST, manifest.render())
        self.remote.store(self.identity.name, ARTIFACT_PACK, pack)
        return pack

    def exported(self) -> tuple[str, bytes]:
        """Create, send the seed, work and export: the scene up to the download."""
        deadline = self.create()
        proto.send_seed(
            self.remote, self.identity, self.seed_dir, self.git_input, deadline=deadline
        )
        work, head = self.stage_and_work()
        return head, self.export_from(work, head)

    def deadline(self) -> float:
        observed = self.remote.lookup(self.identity.name)
        assert observed is not None
        return observed.deadline

    def receive(self, **kwargs) -> CandidateManifest:
        return proto.receive_artifact(
            self.remote, self.identity, self.export, deadline=self.deadline(), **kwargs
        )

    def puts(self) -> list[tuple[str, ...]]:
        return [call for call in self.remote.calls if call[0] == "put"]

    def gets(self) -> list[tuple[str, ...]]:
        return [call for call in self.remote.calls if call[0] == "get"]


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return Scene(tmp_path)


# -- seed pack in, candidate pack and manifest out --------------------------------------


def test_a_seed_pack_goes_in_and_a_candidate_comes_back_through_the_strict_import(scene):
    """The prototype's whole path: the bound seed is uploaded, a repository is
    built from the uploaded pack alone, the remote's two commits come back as
    an artifact, and the package's strict import makes the candidate."""
    head, pack = scene.exported()
    assert not scene.repos.has(head)
    manifest = scene.receive()
    assert manifest.candidate == head
    assert sorted(p.name for p in scene.export.iterdir()) == [ARTIFACT_PACK, ARTIFACT_MANIFEST]
    imported = run_import(scene.repos, scene.export, scene.git_input)
    assert imported.candidate == head and imported.published
    assert imported.objects == count_of(pack)
    assert scene.repos.has(head)
    # The journal entry was written before anything was asked of the provider,
    # and the only paths that crossed are the seed's two files and the
    # artifact's two.
    assert scene.journal == [scene.identity.render()]
    assert [c[2] for c in scene.puts()] == [SEED_MANIFEST_NAME, proto.SEED_PACK]
    assert [c[2] for c in scene.gets()] == [ARTIFACT_MANIFEST, ARTIFACT_PACK]


def test_an_artifact_the_transfer_accepts_is_still_refused_by_the_strict_import(scene):
    """The transfer is never the content boundary: a well-transferred artifact
    whose manifest names another launch is refused by the import, and the
    store receives nothing."""
    deadline = scene.create()
    proto.send_seed(
        scene.remote, scene.identity, scene.seed_dir, scene.git_input, deadline=deadline
    )
    work, head = scene.stage_and_work()
    scene.export_from(work, head, launch="ANALYZE_EXECUTE-2")
    scene.receive()
    packs = scene.repos.packs()
    with pytest.raises(ObjectImportError, match="launch"):
        run_import(scene.repos, scene.export, scene.git_input)
    assert scene.repos.packs() == packs and not scene.repos.has(head)


# -- the seed: hand-off, interruption, expiry -------------------------------------------


def test_a_seed_the_git_input_does_not_bind_is_never_uploaded(scene):
    deadline = scene.create()
    (scene.seed_dir / SEED_MANIFEST_NAME).write_text("{}")
    with pytest.raises(SandboxLaunchRefusedError, match="git input binds"):
        proto.send_seed(
            scene.remote, scene.identity, scene.seed_dir, scene.git_input, deadline=deadline
        )
    assert scene.puts() == []


def test_a_seed_upload_cut_mid_stream_refuses_the_launch(scene):
    deadline = scene.create()
    scene.remote.cut_after = 10
    with pytest.raises(SandboxLaunchRefusedError, match="did not reach the remote whole"):
        proto.send_seed(
            scene.remote, scene.identity, scene.seed_dir, scene.git_input, deadline=deadline
        )
    assert SEED_MANIFEST_NAME not in scene.remote.resources[scene.identity.name].files


def test_a_seed_the_remote_stored_otherwise_refuses_the_launch(scene):
    deadline = scene.create()
    scene.remote.corrupt_uploads = True
    with pytest.raises(SandboxLaunchRefusedError, match="other than the one sent"):
        proto.send_seed(
            scene.remote, scene.identity, scene.seed_dir, scene.git_input, deadline=deadline
        )


def test_a_seed_upload_too_close_to_the_deadline_never_starts(scene):
    deadline = scene.create()
    scene.remote.advance(deadline - scene.remote.now - 10)
    with pytest.raises(SandboxLaunchRefusedError, match="margin"):
        proto.send_seed(
            scene.remote, scene.identity, scene.seed_dir, scene.git_input, deadline=deadline
        )
    assert scene.puts() == []


def test_a_seed_upload_the_deadline_cuts_refuses_the_launch(scene):
    """The resource expires while the pack is uploaded: the provider ends it,
    and the upload fails closed."""
    deadline = scene.create()
    scene.remote.chunk_seconds = (deadline - scene.remote.now) / 2
    (scene.seed_dir / proto.SEED_PACK).write_bytes(b"P" * (5 * proto.CHUNK_BYTES))
    with pytest.raises(SandboxLaunchRefusedError):
        proto.send_seed(
            scene.remote, scene.identity, scene.seed_dir, scene.git_input, deadline=deadline
        )
    assert scene.remote.lookup(scene.identity.name).state == "stopped"


def test_a_seed_file_over_its_bound_is_refused_before_it_is_sent(scene):
    deadline = scene.create()
    with pytest.raises(SandboxLaunchRefusedError, match="over the bound"):
        proto.send_seed(
            scene.remote, scene.identity, scene.seed_dir, scene.git_input,
            deadline=deadline, max_bytes=16,
        )  # fmt: skip
    assert scene.puts() == []


# -- the artifact: bounds, interruption, expiry -----------------------------------------


@pytest.mark.parametrize(
    "fault, match",
    [
        ("cut_after", "interrupted"),
        ("short_after", "not the one its manifest describes"),
        ("trailing", "runs past"),
    ],
)
def test_an_artifact_download_that_breaks_leaves_nothing_to_import(scene, fault, match):
    """A pack stream that errors, ends short or runs long is a candidate
    refusal, and every file the download wrote, the manifest included, is
    deleted."""
    head, pack = scene.exported()
    scene.remote.fault_path = ARTIFACT_PACK
    if fault == "trailing":
        scene.remote.trailing = b"X"
    else:
        setattr(scene.remote, fault, len(pack) // 2)
    with pytest.raises(ObjectImportError, match=match):
        scene.receive()
    assert [c[2] for c in scene.gets()] == [ARTIFACT_MANIFEST, ARTIFACT_PACK]
    assert list(scene.export.iterdir()) == []
    assert not scene.repos.has(head)


def test_a_manifest_claiming_a_pack_over_the_bound_is_refused_before_the_pack_is_read(scene):
    scene.exported()
    with pytest.raises(ObjectImportError, match="over the bound"):
        scene.receive(limits=ArtifactLimits(max_pack_bytes=16))
    assert [c[2] for c in scene.gets()] == [ARTIFACT_MANIFEST]
    assert list(scene.export.iterdir()) == []


def test_a_manifest_over_its_own_bound_is_refused(scene):
    scene.exported()
    with pytest.raises(ObjectImportError, match="runs past"):
        scene.receive(limits=ArtifactLimits(max_manifest_bytes=8))
    assert list(scene.export.iterdir()) == []


def test_an_artifact_download_the_deadline_cuts_is_refused(scene):
    """The controller stops reading once the deadline it renewed has passed,
    whatever the provider still sends."""
    head, pack = scene.exported()
    big = pack + b"\0" * (3 * proto.CHUNK_BYTES)
    scene.remote.store(scene.identity.name, ARTIFACT_PACK, big)
    manifest = CandidateManifest.for_input(
        scene.git_input, candidate=head, pack_bytes=len(big), pack_objects=count_of(pack),
        pack_sha256=hashlib.sha256(big).hexdigest(),
    )  # fmt: skip
    scene.remote.store(scene.identity.name, ARTIFACT_MANIFEST, manifest.render())
    deadline = scene.deadline()
    scene.remote.chunk_seconds = (deadline - scene.remote.now - proto.TRANSFER_MARGIN_SECONDS) / 2
    with pytest.raises(ObjectImportError, match="deadline"):
        proto.receive_artifact(scene.remote, scene.identity, scene.export, deadline=deadline)
    assert list(scene.export.iterdir()) == []


def test_an_expired_resource_has_nothing_to_download(scene):
    """At its deadline the provider ends the resource and discards its files:
    the download fails closed, and nothing is left for an import."""
    scene.exported()
    scene.remote.advance(scene.deadline() - scene.remote.now + 1)
    assert scene.remote.lookup(scene.identity.name).state == "stopped"
    with pytest.raises(ObjectImportError):
        proto.receive_artifact(
            scene.remote, scene.identity, scene.export, deadline=scene.remote.now + HOUR
        )
    assert list(scene.export.iterdir()) == []


def test_an_artifact_is_never_merged_with_leftovers_in_the_export_directory(scene):
    scene.exported()
    (scene.export / "stale").write_bytes(b"")
    with pytest.raises(ObjectImportError, match="not empty"):
        scene.receive()
    assert scene.gets() == []


# -- R1: identity and the ambiguous create ----------------------------------------------


def test_the_journal_entry_round_trips_and_is_read_strictly(scene):
    entry = scene.identity
    assert RemoteIdentity.parse(entry.render()) == entry
    data = json.loads(entry.render())
    for broken in (
        {**data, "extra": 1},
        {k: v for k, v in data.items() if k != "nonce"},
        {**data, "issue": "276"},
        {**data, "nonce": "not-a-nonce"},
        {**data, "schema": 2},
        {**data, "ceiling": 7},
    ):
        with pytest.raises(ValueError):
            RemoteIdentity.parse(json.dumps(broken).encode())


def test_the_name_and_tags_derive_from_the_entry_alone(scene):
    entry = scene.identity
    assert proto.NAME_RE.fullmatch(entry.name) and len(entry.name) <= 63
    assert RemoteIdentity.parse(entry.render()).name == entry.name
    assert replace(entry, nonce="f" * 32).name != entry.name
    assert tuple(entry.tags()) == proto.TAG_KEYS
    assert all(len(value) <= proto.MAX_TAG_VALUE for value in entry.tags().values())


def test_an_ambiguous_create_is_resolved_by_name_and_never_makes_a_second_resource(scene):
    scene.remote.lose_create_response = True
    scene.create()
    assert list(scene.remote.resources) == [scene.identity.name]
    assert [c[0] for c in scene.remote.calls if c[0] == "create"] == ["create"]


def test_a_create_lost_before_it_happened_is_issued_once_more_under_the_same_name(scene):
    scene.remote.fail_create_before = True
    scene.create()
    assert list(scene.remote.resources) == [scene.identity.name]
    creates = [c for c in scene.remote.calls if c[0] == "create"]
    assert creates == [("create", scene.identity.name)] * 2


def test_a_name_holding_another_resource_refuses_the_launch_and_removes_nothing(scene):
    scene.remote.create(
        scene.identity.name, {"autoforge.run": "someone-else"}, scene.remote.now + 60
    )
    with pytest.raises(SandboxLaunchRefusedError, match="tags are not its journal entry's"):
        scene.create()
    assert scene.identity.name in scene.remote.resources
    assert not [c for c in scene.remote.calls if c[0] == "destroy"]


def test_a_scope_other_than_the_entry_refuses_the_create_before_the_journal(scene):
    scene.remote.reported_scope = "team/other-project/iad1"
    with pytest.raises(ConfigurationError, match="other-project"):
        scene.create()
    assert scene.journal == [] and scene.remote.resources == {}


def test_a_ceiling_past_the_providers_longest_session_is_refused_before_anything(scene):
    scene.remote.max_session = HOUR
    with pytest.raises(SandboxLaunchRefusedError, match="longest session"):
        scene.create()
    assert scene.journal == [] and scene.remote.resources == {}


# -- R3 and R7: the renewed deadline ----------------------------------------------------


def test_the_deadline_is_renewed_within_its_window_and_never_past_the_ceiling(scene):
    deadline = scene.create()
    assert deadline == scene.remote.now + scene.identity.window
    # More than half a window left: nothing is asked.
    assert proto.keep_deadline(scene.remote, scene.identity, deadline) == deadline
    assert not [c for c in scene.remote.calls if c[0] == "extend"]
    seen = [deadline]
    while scene.remote.now < scene.identity.ceiling - 1:
        scene.remote.advance(scene.identity.window / 2 + 1)
        if scene.remote.lookup(scene.identity.name).state != "running":
            break
        seen.append(proto.keep_deadline(scene.remote, scene.identity, seen[-1]))
    assert max(seen) == scene.identity.ceiling
    assert all(d - t <= scene.identity.window for d, t in zip(seen[1:], seen, strict=False))


def test_a_dead_controller_leaves_a_resource_the_provider_ends_within_one_window(scene):
    """The controller renews twice, then dies (it renews no more): the
    provider ends the resource at the last deadline it was given, at most one
    window after the controller's last renewal, long before the ceiling."""
    deadline = scene.create()
    for _ in range(2):
        scene.remote.advance(scene.identity.window / 2 + 1)
        deadline = proto.keep_deadline(scene.remote, scene.identity, deadline)
    died = scene.remote.now
    assert deadline <= died + scene.identity.window < scene.identity.ceiling
    scene.remote.advance(deadline - scene.remote.now - 1)
    assert scene.remote.lookup(scene.identity.name).state == "running"
    scene.remote.advance(1)
    observed = scene.remote.lookup(scene.identity.name)
    assert observed.state == "stopped" and scene.remote.resources[scene.identity.name].files == {}
    with pytest.raises(RemoteGone):
        proto.keep_deadline(scene.remote, scene.identity, deadline)


# -- R6 and R8: termination and reconciliation ------------------------------------------


def test_termination_closes_only_on_a_lookup_that_finds_nothing(scene):
    scene.create()
    assert proto.terminate(scene.remote, scene.identity) == "removed"
    assert scene.remote.resources == {}
    assert proto.terminate(scene.remote, scene.identity) == "gone"


def test_a_destroy_that_returns_and_leaves_the_resource_keeps_the_entry_open(scene):
    scene.create()
    scene.remote.destroys_ignored = proto.REMOVAL_ATTEMPTS
    with pytest.raises(SandboxCleanupUncertainError, match="still listed") as caught:
        proto.terminate(scene.remote, scene.identity)
    assert caught.value.objects == (f"remote {scene.identity.name}",)
    assert scene.remote.lookup(scene.identity.name).state == "stopping"


def test_a_scope_that_changes_during_a_removal_closes_nothing(scene):
    scene.create()
    scene.remote.scope_after = [scene.identity.scope, "team/other-project/iad1"]
    with pytest.raises(SandboxCleanupUncertainError, match="scope changed"):
        proto.terminate(scene.remote, scene.identity)


def test_reconciliation_after_a_crash_removes_the_runs_resource_and_never_reads_its_files(scene):
    """The controller died after the export was written remotely: the
    resource is removed without an export, and the work is lost (ADR 0013 D4)."""
    scene.exported()
    entry = RemoteIdentity.parse(scene.journal[0])
    gets = len(scene.gets())
    report = proto.reconcile(scene.remote, scene.identity.run, [entry])
    assert report.closed == (entry.name,) and report.blocked == ()
    assert scene.remote.resources == {}
    assert len(scene.gets()) == gets
    assert list(scene.export.iterdir()) == []


def test_reconciliation_closes_an_entry_whose_resource_never_existed_or_is_gone(scene):
    report = proto.reconcile(scene.remote, scene.identity.run, [scene.identity])
    assert report == proto.Reconciliation((scene.identity.name,), ())


def test_a_resource_with_the_runs_tag_and_no_entry_blocks_and_is_never_removed(scene):
    stranger = replace(scene.identity, nonce="a" * 32)
    scene.remote.create(stranger.name, stranger.tags(), scene.remote.now + 60)
    report = proto.reconcile(scene.remote, scene.identity.run, [scene.identity])
    assert report.closed == (scene.identity.name,)
    assert [b.split(":")[0] for b in report.blocked] == [stranger.name]
    assert stranger.name in scene.remote.resources


def test_reconciliation_in_a_scope_other_than_the_entries_lists_and_removes_nothing(scene):
    scene.create()
    scene.remote.reported_scope = "team/other-project/iad1"
    calls = len(scene.remote.calls)
    with pytest.raises(ConfigurationError, match="nothing is listed or removed"):
        proto.reconcile(scene.remote, scene.identity.run, [scene.identity])
    assert [c[0] for c in scene.remote.calls[calls:]] == ["scope"]
    assert scene.identity.name in scene.remote.resources


def test_a_provider_that_keeps_failing_to_answer_blocks_rather_than_reads_gone(scene):
    scene.create()
    scene.remote.lookup_failures = 2 * proto.LOOKUP_ATTEMPTS
    report = proto.reconcile(scene.remote, scene.identity.run, [scene.identity])
    assert report.closed == ()
    assert len(report.blocked) == 2
    assert scene.identity.name in scene.remote.resources


def test_a_scope_that_changes_during_reconciliation_closes_nothing(scene):
    scene.create()
    scene.remote.scope_after = [scene.identity.scope] * 3 + ["team/other-project/iad1"]
    report = proto.reconcile(scene.remote, scene.identity.run, [scene.identity])
    assert report.closed == () and "nothing is closed" in report.blocked[-1]


# -- R2 and R9: no GitHub write authority reaches the remote ---------------------------


@pytest.mark.parametrize(
    "request_, match",
    [
        (CreateRequest(env=("GH_TOKEN",)), "GH_TOKEN"),
        (CreateRequest(env=("GIT_ASKPASS",)), "GIT_ASKPASS"),
        (CreateRequest(env=("SSH_AUTH_SOCK",)), "SSH_AUTH_SOCK"),
        (CreateRequest(env=("VERCEL_TOKEN",)), "the provider's own credential"),
        (CreateRequest(brokered={"*.github.com": ("Authorization",)}), "api.github.com"),
        (CreateRequest(brokered={"github.com": ("Authorization",)}), "GitHub write authority"),
        (CreateRequest(brokered={"*": ("Authorization",)}), "github.com"),
        (CreateRequest(brokered={"raw.*.com": ("Authorization",)}), "githubusercontent"),
        (CreateRequest(managed=("npm",)), "'npm'"),
    ],
)
def test_a_create_that_would_hand_the_remote_a_github_or_provider_credential_is_refused(
    scene, request_, match
):
    with pytest.raises(SandboxLaunchRefusedError, match=match):
        scene.create(request_)
    assert scene.journal == [] and scene.remote.resources == {}


def test_a_model_credential_brokered_for_its_own_api_is_admitted(scene):
    """What R2 admits on paper: the provider injects the model key into
    requests to the model's API, and nothing names a GitHub host."""
    request_ = CreateRequest(env=("HOME", "PATH"), brokered={"api.anthropic.com": ("x-api-key",)})
    assert proto.create_request_problem(request_, VERCEL_CREDENTIALS) == ""
    scene.create(request_)


# -- the record and the package ---------------------------------------------------------


def test_adr_0014_names_only_tests_that_exist():
    text = ADR_0014.read_text(encoding="utf-8")
    named = set(re.findall(r"\btest_[a-z0-9_]+", text))
    assert named
    defined = set()
    for path in (ROOT / "tests").glob("test_*.py"):
        defined |= set(re.findall(r"^def (test_[a-z0-9_]+)", path.read_text(), re.MULTILINE))
        defined.add(path.stem)
    assert sorted(named - defined) == []


def test_adr_0014_claims_no_state_protocol_number_and_its_links_resolve():
    text = ADR_0014.read_text(encoding="utf-8")
    current = int(autoforge.__protocol_version__)
    assert [n for n in PROTOCOL.findall(text) if int(n) > current] == []
    broken, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        for target in [] if fenced else LINK.findall(line):
            if "://" not in target:
                broken += [p for p in [_broken(ADR_0014, target)] if p]
    assert broken == []


def test_adr_0014s_no_go_and_adr_0013s_refusal_of_a_remote_endpoint_agree():
    """While ADR 0014's decision is a no-go, ADR 0013's capability matrix
    keeps refusing a runtime endpoint on another machine, and says why."""
    assert "**no-go**" in ADR_0014.read_text(encoding="utf-8")
    rows = [
        line
        for line in ADR_0013.read_text(encoding="utf-8").splitlines()
        if line.startswith("| a `DOCKER_HOST`")
    ]
    assert rows == [
        "| a `DOCKER_HOST`, `CONTAINER_HOST` or Docker context naming a TCP or SSH endpoint "
        "| not in the MVP; refused (#276, #277) |"
    ]


def test_the_package_never_imports_the_prototype():
    """A no-go ships nothing: the prototype stays test code."""
    package = Path(autoforge.__file__).parent
    users = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "remote_sandbox_prototype" in path.read_text(encoding="utf-8")
        or re.search(r"^\s*(from|import) tests\b", path.read_text(encoding="utf-8"), re.M)
    ]
    assert users == []
