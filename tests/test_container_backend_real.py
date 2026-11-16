"""#271 on a real rootless runtime (ADR 0013 §7, real-runtime rows). Opt-in.

What these prove that the fake runtime cannot: the runtime's own identity
and store; the shared-filesystem probe's nonce read back through a real
read-only bind, and ``transfer: copy``; and one agent launch end to end in
each staging form, with AutoForge's image built from the operator's base:
the real stager and prover in the stager container, the command run as the
agent in the launch container, its commit exported by the real exporter
and imported strictly with the same id, and nothing of the launch left on
the runtime.

**Opt-in**, like ``test_container_security_real.py``: they run only when
``AUTOFORGE_CONTAINER_TEST_RUNTIME`` names a runtime (``podman`` or
``docker``) and ``AUTOFORGE_CONTAINER_TEST_BASE`` a base image by digest
(``name@sha256:...``), already in the runtime's store, with ``python3``,
``git`` and ``sh``. Otherwise, and on a runtime that is not rootless, each
test skips with the reason, never passes;
``AUTOFORGE_REQUIRE_CONTAINER_RUNTIME=1`` makes those a failure instead.
Every object a test creates carries its launch's names and is removed by
the lifecycle; the image AutoForge builds is left in the store, keyed.

The entrypoint test (R1-F8 of PR #506) builds a base of its own from
``AUTOFORGE_CONTAINER_TEST_BASE`` with a nontrivial ``ENTRYPOINT`` and
names it by digest, which a local build has only on Podman: on Docker it
skips, untested.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import pytest

from autoforge.candidate_artifact import import_artifact
from autoforge.container_backend import ContainerBackend
from autoforge.container_image import ImageBuilder, ImageInputs
from autoforge.container_runtime import RuntimeClient, detect
from autoforge.executor import ExecutionRequest, execute
from autoforge.git_input import LaunchGitInput
from autoforge.git_transport import GitRemote, GitTransport
from autoforge.sandbox_backend import bind
from autoforge.sandbox_container import HostContext, ProbeRoot, RuntimeProduct, StagingMethod
from autoforge.sandbox_lifecycle import LaunchLifecycle
from autoforge.seed_pack import SeedSettings

from .test_container_backend import PLANNED, Journal, ProbeJournal, inputs_for, planner
from .test_seed_pack import out, review_repo
from .test_source_staging import KEY, RecordingLock, staging_for

RUNTIME = os.environ.get("AUTOFORGE_CONTAINER_TEST_RUNTIME", "")
BASE = os.environ.get("AUTOFORGE_CONTAINER_TEST_BASE", "")
REQUIRED = os.environ.get("AUTOFORGE_REQUIRE_CONTAINER_RUNTIME") == "1"


def _unavailable(reason: str) -> None:
    if REQUIRED:
        pytest.fail(f"a real container runtime is required: {reason}")
    pytest.skip(f"untested on this host: {reason}")


@pytest.fixture
def client() -> RuntimeClient:
    if not RUNTIME or not BASE:
        _unavailable("AUTOFORGE_CONTAINER_TEST_RUNTIME and AUTOFORGE_CONTAINER_TEST_BASE are unset")
    env = dict(os.environ)
    try:
        chosen = detect(RUNTIME, env)
        runtime = RuntimeClient(chosen, env)
        identity = runtime.identity()
    except Exception as exc:  # noqa: BLE001 - any refusal makes the host unusable here
        _unavailable(f"the runtime check refused: {exc}")
    if identity.rootless is not True:
        _unavailable(f"{identity.describe()} is not rootless")
    return runtime


@pytest.fixture
def world(tmp_path, client):
    review = review_repo(tmp_path)
    common = out(review.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    identity = client.identity()
    image = ImageBuilder(client, identity.product).ensure(ImageInputs(base=BASE))
    host = HostContext(
        home=os.path.expanduser("~"),
        environ={},
        uid=os.getuid(),
        git_common_dir=os.path.realpath(common),
        checkouts=(str(review.repo),),
    )

    def backend(transfer: str, built=None) -> ContainerBackend:
        made = ContainerBackend(
            client,
            host=host,
            common_dir=common,
            sandbox_key=KEY,
            seed_settings=SeedSettings(),
            image=built or image,
            transfer=transfer,
            credentials=planner(),
            staging=lambda method: staging_for(review.repo, method, lock=RecordingLock()),
        )
        runtime = made.runtime_check()
        made._probe_journal = ProbeJournal(  # noqa: SLF001
            ProbeRoot(host.git_common_dir, f"probe-{transfer}"), runtime
        )
        return made

    return review, common, backend, tmp_path


def test_the_runtime_reports_its_identity_and_its_store(client):
    identity = client.identity()
    assert identity.rootless is True
    assert all(value for _, value in identity.store)
    assert identity.report().store == client.identity().report().store


@pytest.mark.parametrize(
    "transfer, expected", [("auto", StagingMethod.BIND), ("copy", StagingMethod.COPY)]
)
def test_the_nonce_bind_proof_on_a_local_runtime_and_transfer_copy(world, transfer, expected):
    _, _, backend, _ = world
    assert backend(transfer).staging_method() is expected


@pytest.mark.parametrize("transfer", ["auto", "copy"])
def test_an_agent_commit_is_exported_and_imported_with_the_same_id(world, transfer):
    review, common, make_backend, tmp_path = world
    backend = make_backend(transfer)
    runtime = backend.runtime_check()
    backend.staging_method()
    gi = LaunchGitInput(
        phase="FIX", run="run-real", issue=271, launch=f"L-{transfer}", repository="owner/repo",
        object_format="sha1", seed=review.head, lower=review.merge_base, carried="",
        export_command=False, entry_label="entry-1",
    )  # fmt: skip

    class _World:
        tmp = tmp_path

    inputs = inputs_for(_World, gi)  # type: ignore[arg-type]
    journal = Journal()
    lifecycle = LaunchLifecycle(backend, journal, inputs, runtime, change_producing=True)
    script = (
        'set -e; test "$(git rev-parse HEAD)" = ' + review.head + "; "
        "echo work > agent.txt; git add agent.txt; "
        "git -c user.name=agent -c user.email=agent@example.invalid commit -q -m work; "
        "git rev-parse HEAD"
    )
    with lifecycle:

        def run():
            handle = lifecycle.establish(PLANNED)
            return execute(
                ExecutionRequest(
                    command=["sh", "-c", script],
                    env_allowlist=(),
                    sandbox=bind(PLANNED, backend, handle),
                    timeout_seconds=300,
                )
            )

        result = lifecycle.launch(run)
        assert result.exit_code == 0, result.stderr
        candidate = result.stdout.strip().splitlines()[-1]
        exported = lifecycle.export()
        assert exported is not None and exported.artifact is not None, exported
        reader = GitTransport(
            object_directory=Path(common) / "objects",
            remote=GitRemote("file:///nonexistent"),
            in_base_history=lambda base, sha: False,
            git_lock=lambda operation: contextlib.nullcontext(),
        )
        imported = import_artifact(
            Path(common), Path(exported.artifact.directory), lifecycle.git_input, execute,
            read_commit=reader.read_local_commit,
            git_lock=lambda operation: contextlib.nullcontext(),
        )  # fmt: skip
        assert imported.candidate == candidate
        lifecycle.saved()
    assert journal.open == []
    listing = backend._client.run(  # noqa: SLF001
        ["ps", "--all", "--filter", "label=io.autoforge.run", "--format", "{{json .Names}}"]
    )
    assert inputs.owner.launch not in listing.stdout
    assert json.loads(json.dumps(result.sandbox.record()))["backend"] == "container"


def _launch_output(backend, review, tmp_path, launch: str, command: list[str]):
    """One agent launch's command output through the lifecycle, on a real runtime."""
    runtime = backend.runtime_check()
    backend.staging_method()
    gi = LaunchGitInput(
        phase="FIX", run="run-real", issue=271, launch=launch, repository="owner/repo",
        object_format="sha1", seed=review.head, lower=review.merge_base, carried="",
        export_command=False, entry_label="entry-1",
    )  # fmt: skip

    class _World:
        tmp = tmp_path

    inputs = inputs_for(_World, gi, export=False)  # type: ignore[arg-type]
    lifecycle = LaunchLifecycle(backend, Journal(), inputs, runtime, change_producing=False)
    with lifecycle:

        def run():
            handle = lifecycle.establish(PLANNED)
            return execute(
                ExecutionRequest(
                    command=command,
                    env_allowlist=(),
                    sandbox=bind(PLANNED, backend, handle),
                    timeout_seconds=300,
                )
            )

        return lifecycle.launch(run)


def test_a_base_images_entrypoint_never_replaces_the_watcher(world, client):
    """PID 1 of a launch container is the watcher, though the base has an entrypoint."""
    review, _, make_backend, tmp_path = world
    identity = client.identity()
    if identity.product is not RuntimeProduct.PODMAN:
        _unavailable("a locally built base has a digest to name it by only on Podman")
    context = tmp_path / "hijacking-base"
    context.mkdir()
    (context / "Containerfile").write_text(
        f"FROM {BASE}\n"
        'ENTRYPOINT ["/bin/sh", "-c", "echo hijacked > /tmp/hijacked; exec sleep 100000"]\n'
    )
    iid = tmp_path / "hijacking.iid"
    built = client.run(
        ["build", "--pull=never", f"--iidfile={iid}", f"--file={context / 'Containerfile'}",
         "--tag=localhost/autoforge-test-entrypoint:latest", str(context)],
        timeout_seconds=1800,
    )  # fmt: skip
    assert built.exit_code == 0, built.stderr
    digest = client.run(
        ["image", "inspect", "--format", "{{.Digest}}", iid.read_text().strip()]
    ).stdout.strip()
    base = f"localhost/autoforge-test-entrypoint@{digest}"
    image = ImageBuilder(client, identity.product).ensure(ImageInputs(base=base))
    backend = make_backend("copy", image)
    result = _launch_output(
        backend, review, tmp_path, "L-entrypoint",
        ["sh", "-c", "tr '\\0' ' ' < /proc/1/cmdline; test ! -e /tmp/hijacked && echo clean"],
    )  # fmt: skip
    assert result.exit_code == 0, result.stderr
    assert "container_watch.py" in result.stdout and "clean" in result.stdout
