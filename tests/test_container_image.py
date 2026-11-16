"""#271: the container image's inputs, key and build (ADR 0013 D7, §7).

The build runs on the fake runtime of ``tests/container_fake.py``, which
records the context it was given and names an image; no real image is
built. ADR 0013 §7 for #271: the image key changes with each input and not
otherwise, and no repository build file is read.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from autoforge import container_image
from autoforge.container_image import (
    CONTAINERFILE,
    HELPERS,
    KEY_LABEL,
    ImageBuilder,
    ImageInputs,
    KeyInputs,
    build_argv,
    image_key,
    parse_image_id,
    render_containerfile,
    render_context,
)
from autoforge.container_runtime import RuntimeClient, detect
from autoforge.errors import SandboxCapabilityError
from autoforge.sandbox_container import RuntimeProduct

from . import container_fake as fake

BASE = "registry.example/toolchain@sha256:" + "c" * 64
INPUTS = ImageInputs(
    base=BASE,
    claude_code="2.0.14",
    opencode="1.18.34",
    pi="1.0.1",
    mcp_servers=("@scope/server-files@1.2.3", "server-git@0.4.0"),
    skills=("review-skill@0.1.0",),
)
KEYED = KeyInputs(
    INPUTS, {"container_watch.py": "1" * 64, "container_stager.py": "2" * 64}, "0.1.0"
)


@pytest.mark.parametrize(
    "change",
    [
        {"base": "registry.example/toolchain@sha256:" + "d" * 64},
        {"claude_code": "2.0.15"},
        {"opencode": ""},
        {"pi": "1.0.2"},
        {"mcp_servers": ("@scope/server-files@1.2.4", "server-git@0.4.0")},
        {"skills": ()},
    ],
)
def test_the_key_changes_with_each_operator_input(change):
    changed = dataclasses.replace(KEYED, inputs=dataclasses.replace(INPUTS, **change))
    assert image_key(changed) != image_key(KEYED)


def test_the_key_changes_with_autoforges_layer_and_not_otherwise():
    assert image_key(dataclasses.replace(KEYED, controller="0.1.1")) != image_key(KEYED)
    helpers = {**KEYED.helpers, "container_stager.py": "3" * 64}
    assert image_key(dataclasses.replace(KEYED, helpers=helpers)) != image_key(KEYED)
    # Not otherwise: the same inputs, another order of a list or of the helpers.
    reordered = dataclasses.replace(
        KEYED,
        inputs=dataclasses.replace(INPUTS, mcp_servers=tuple(reversed(INPUTS.mcp_servers))),
        helpers=dict(reversed(list(KEYED.helpers.items()))),
    )
    assert image_key(reordered) == image_key(KEYED)
    assert image_key(KeyInputs.current(INPUTS)) == image_key(KeyInputs.current(INPUTS))


def test_the_current_key_covers_every_helper_by_its_bytes():
    current = KeyInputs.current(INPUTS)
    assert set(current.helpers) == set(HELPERS.values())
    assert current.controller == container_image._controller_version()  # noqa: SLF001


def test_the_containerfile_is_rendered_from_the_typed_inputs_alone():
    text = render_containerfile(INPUTS, "sha256:" + "e" * 64)
    assert text.startswith(f"FROM {BASE}\n")
    for word in ("ARG ", "ENV ", "ADD ", "--mount", "secret", "ssh"):
        assert word not in text
    assert "npm install --global" in text
    for package in INPUTS.packages():
        assert package in text
    assert "chown 10001:10001 /workspace /cache" in text
    assert f'{KEY_LABEL}="sha256:{"e" * 64}"' in text
    # The base's entrypoint is cleared: PID 1 is the create's command.
    assert "\nENTRYPOINT []\nCMD []\n" in text
    bare = render_containerfile(ImageInputs(base=BASE), "k")
    assert "npm" not in bare


def test_the_context_holds_the_containerfile_and_the_helpers_and_nothing_else():
    files = render_context(INPUTS, "k")
    assert set(files) == {CONTAINERFILE, *(f"helpers/{name}" for name in HELPERS.values())}
    for name in HELPERS.values():
        assert b"from autoforge" not in files[f"helpers/{name}"]


@pytest.mark.parametrize("product", list(RuntimeProduct))
def test_the_build_argv_has_no_argument_secret_mount_or_network_of_its_own(product):
    argv = build_argv(product, "/ctx", "/ctx/.iid", "k")
    assert argv[0] == "build" and argv[-1] == "/ctx"
    for word in argv:
        for forbidden in ("--build-arg", "--secret", "--ssh", "--volume", "-v", "--network",
                          "--add-host", "--privileged", "--cap-add", "--security-opt"):  # fmt: skip
            assert not word.startswith(forbidden), word
    assert ("--pull=never" in argv) is (product is RuntimeProduct.PODMAN)


@pytest.mark.parametrize(
    "value",
    [
        "ubuntu",
        "ubuntu:24.04",
        "ubuntu@sha256:abc",
        "UPPER@sha256:" + "c" * 64,
        "a b@sha256:" + "c" * 64,
    ],
)
def test_a_base_not_named_by_digest_is_refused(value):
    with pytest.raises(ValueError, match="named by digest"):
        ImageInputs(base=value)


def test_parse_image_id_takes_both_spellings_and_nothing_else():
    assert parse_image_id("sha256:" + "f" * 64 + "\n") == "f" * 64
    assert parse_image_id("f" * 64) == "f" * 64
    assert parse_image_id("f" * 12) == ""


def builder(tmp_path: Path, **state) -> tuple[ImageBuilder, Path]:
    bin_dir = tmp_path / "bin"
    fake.install(
        bin_dir, "podman", {"version": fake.PODMAN_VERSION, "info": fake.podman_info(), **state}
    )
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    return ImageBuilder(RuntimeClient(detect("podman", env), env), RuntimeProduct.PODMAN), bin_dir


def test_an_unchanged_configuration_reuses_its_image_and_a_change_builds_another(
    tmp_path, monkeypatch
):
    # A repository with build files of its own, as the working directory.
    repo = tmp_path / "repo"
    (repo / ".devcontainer").mkdir(parents=True)
    (repo / "Dockerfile").write_text("FROM evil\nRUN touch /pwned\n")
    (repo / ".devcontainer" / "devcontainer.json").write_text('{"initializeCommand": "x"}')
    monkeypatch.chdir(repo)
    build, bin_dir = builder(tmp_path)
    first = build.ensure(INPUTS)
    assert len(first.id) == 64 and first.digest == f"sha256:{first.id}"
    again = build.ensure(INPUTS)
    assert again == first
    builds = fake.state(bin_dir)["builds"]
    assert len(builds) == 1, "the unchanged configuration reused its image"
    # The context is the controller's: the Containerfile and the helpers, no
    # repository file, and the build was told of no other path.
    assert sorted(builds[0]["files"]) == sorted(
        [CONTAINERFILE, *(f"helpers/{name}" for name in HELPERS.values())]
    )
    assert "evil" not in builds[0]["containerfile"]
    assert not any(str(repo) in word for word in builds[0]["argv"])
    other = build.ensure(dataclasses.replace(INPUTS, pi="1.0.2"))
    assert other.id != first.id and len(fake.state(bin_dir)["builds"]) == 2


def test_a_failed_build_is_a_refusal_naming_the_image(tmp_path):
    build, _ = builder(tmp_path, fail={"build": [1, "Error: npm ERR! 404"]})
    with pytest.raises(SandboxCapabilityError, match="image build exited 1") as refused:
        build.ensure(INPUTS)
    assert refused.value.capability == "image"


def test_the_base_images_entrypoint_never_replaces_the_watcher(tmp_path):
    # The base has an entrypoint; the Containerfile clears it, so the built
    # image is used.
    build, bin_dir = builder(tmp_path, base_entrypoint=["/usr/local/bin/hijack"])
    image = build.ensure(INPUTS)
    assert fake.state(bin_dir).get("entrypoints", {}).get(image.id) is None
    # A cached image that carries the key and an entrypoint is not reused.
    labels = fake.state(bin_dir)["images"][image.id]
    fake.update(
        bin_dir,
        images={"c" * 64: labels},
        entrypoints={"c" * 64: ["/bin/sh", "-c", "exec evil"]},
    )
    again = build.ensure(INPUTS)
    assert again.id != "c" * 64
    # A build whose image keeps an entrypoint is refused.
    build, _ = builder(tmp_path / "forced", built_entrypoint=["/bin/sh"])
    with pytest.raises(SandboxCapabilityError, match="PID 1 in the watcher's place"):
        build.ensure(INPUTS)


@pytest.mark.parametrize(
    "shown, problem",
    [(None, False), ([], False), ("", False), (["/bin/sh"], True), ("/init", True)],
)
def test_entrypoint_problem_reads_the_images_own_configuration(tmp_path, shown, problem):
    build, bin_dir = builder(tmp_path, images={"d" * 64: {}}, entrypoints={"d" * 64: shown})
    found = container_image.entrypoint_problem(build._client, "d" * 64)  # noqa: SLF001
    assert bool(found) is problem
    assert container_image.entrypoint_problem(build._client, "e" * 64)  # noqa: SLF001
