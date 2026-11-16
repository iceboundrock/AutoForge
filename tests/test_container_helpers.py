"""#271: the container image's helpers, run on the host against real seeds (ADR 0013 D2, D5, D13).

The stager, the prover, the exporter and PID 1's watcher import nothing of
``autoforge`` and run in a container as plain Python. Here they run on the
host, on directories standing in for ``/source``, ``/workspace`` and the
stager container's ``/tmp``, against seeds built by the real seed step
(:func:`autoforge.seed_pack.build_seed`) from repositories under
``tmp_path``. What a container adds (the offline network, the agent's UID,
the read-only bind) is the real-runtime tests' (``test_container_backend_real.py``);
what is proved here is what the helpers themselves do:

- the hostile input of ADR 0013 §7: a ``.gitattributes`` naming a filter
  driver (``required``), ``ident``, ``eol=crlf``, ``text`` and
  ``working-tree-encoding``, an LFS pointer, a gitlink, and a git
  configuration defining that driver and LFS's as programs that write a
  sentinel and rewrite bytes; ``/workspace`` holds the blobs' bytes, the
  sentinel is never written, the proof passes. A negative control shows the
  same input does rewrite bytes when the checkout's override is removed;
- every fault ADR 0013 §7 injects into the seeded repository fails the
  prover; the ref set both ways; a byte changed between the stager and the
  prover fails the tree proof in both forms; the prover's expected tree is
  the request's, whatever the container's manifest says;
- an export command's workspace in both forms, with no ``.git``;
- an agent's commit exported by the exporter and imported strictly with
  the same id.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from autoforge import container_exporter, container_prover, container_stager, container_watch
from autoforge.candidate_artifact import (
    ARTIFACT_MANIFEST,
    ARTIFACT_PACK,
    CandidateManifest,
    import_artifact,
)
from autoforge.executor import execute
from autoforge.git_input import SEED_MANIFEST_NAME, LaunchGitInput
from autoforge.git_transport import GitRemote, GitTransport
from autoforge.materialize import materialize
from autoforge.seed_pack import SEED_PACK_NAME, SeedSettings, build_seed, discard

from .test_seed_pack import common_of, init_repo, out, run_git

FORMATS = ["sha1", "sha256"]
FORMS = ["bind", "copy"]

HOSTILE_ATTRIBUTES = (
    b"* text=auto\n"
    b"crlf.txt text eol=crlf\n"
    b"id.txt ident\n"
    b"*.txt filter=evil\n"
    b"enc.txt working-tree-encoding=UTF-16\n"
    b"big.bin filter=lfs diff=lfs merge=lfs -text\n"
)
LFS_POINTER = (
    b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"4" * 64 + b"\nsize 12345\n"
)


@pytest.fixture(autouse=True)
def temporary(tmp_path, monkeypatch) -> Path:
    where = tmp_path / "temporary"
    where.mkdir()
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(where))
    return where


def hostile_config(tmp_path: Path) -> tuple[Path, Path]:
    """A git configuration whose filters write a sentinel and rewrite bytes; the sentinel's path."""
    sentinel = tmp_path / "SENTINEL"
    script = tmp_path / "rewrite.sh"
    script.write_text(f"#!/bin/sh\ntouch {sentinel}\nsed 's/a/X/g'\n")
    script.chmod(0o755)
    config = tmp_path / "hostile.gitconfig"
    config.write_text(
        f'[filter "evil"]\n\tsmudge = {script}\n\tclean = cat\n\trequired = true\n'
        f'[filter "lfs"]\n\tsmudge = {script}\n\tclean = cat\n\trequired = true\n'
        f"[core]\n\tautocrlf = true\n\thooksPath = {tmp_path}\n"
    )
    return config, sentinel


def blob(repo: Path, content: bytes) -> str:
    return out(repo, "hash-object", "-w", "--no-filters", "--stdin", data=content)


def commit_tree(repo: Path, entries: dict[str, tuple[str, str]], message: str) -> str:
    """A commit whose tree is exactly ``entries`` (path: (mode, id)), built with plumbing only."""
    index = repo / ".git" / "index"
    if index.exists():
        index.unlink()
    for path, (mode, oid) in entries.items():
        run_git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}")
    tree = out(repo, "write-tree")
    parent = []
    with contextlib.suppress(AssertionError):
        parent = ["-p", out(repo, "rev-parse", "--verify", "-q", "HEAD")]
    sha = out(repo, "commit-tree", tree, *parent, "-m", message)
    run_git(repo, "update-ref", "HEAD", sha)
    return sha


@dataclass
class Scenario:
    repo: Path
    object_format: str
    first: str
    seed: str
    files: dict[str, bytes]
    gitlink: str


def scenario(tmp_path: Path, object_format: str) -> Scenario:
    repo = init_repo(tmp_path / "repo", object_format)
    files = {
        ".gitattributes": HOSTILE_ATTRIBUTES,
        "crlf.txt": b"a\nb\n",
        "id.txt": b"$Id$\nhello a\n",
        "enc.txt": "héllo a\n".encode(),
        "big.bin": LFS_POINTER,
        "run.sh": b"#!/bin/sh\necho a\n",
        "dir/nested.txt": b"nested a\n",
    }
    first = commit_tree(repo, {"crlf.txt": ("100644", blob(repo, b"first\n"))}, "first")
    entries = {path: ("100644", blob(repo, data)) for path, data in files.items()}
    entries["run.sh"] = ("100755", entries["run.sh"][1])
    entries["link"] = ("120000", blob(repo, b"crlf.txt"))
    gitlink = "1" * (64 if object_format == "sha256" else 40)
    entries["mod"] = ("160000", gitlink)
    seed = commit_tree(repo, entries, "second")
    run_git(repo, "tag", "-a", "-m", "v1", "v1", first)
    run_git(repo, "tag", "light", seed)
    return Scenario(repo, object_format, first, seed, files, gitlink)


def launch_input(
    sc: Scenario, *, export: bool = False, phase: str = "ANALYZE_EXECUTE"
) -> LaunchGitInput:
    return LaunchGitInput(
        phase=phase,
        run="run-1",
        issue=271,
        launch="L-1",
        repository="owner/repo",
        object_format=sc.object_format,
        seed=sc.seed,
        lower="" if export else sc.seed,
        carried="",
        export_command=export,
        entry_label="entry-1",
    )


@dataclass
class Staged:
    sc: Scenario
    git_input: LaunchGitInput
    seed_dir: Path
    manifest: bytes
    paths: container_stager.Paths
    form: str
    role: str

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.manifest).hexdigest()

    def stage_request(self, manifest: bytes | None = None) -> bytes:
        manifest = self.manifest if manifest is None else manifest
        return json.dumps(
            {
                "role": self.role,
                "form": self.form,
                "object_format": self.sc.object_format,
                "manifest": manifest.decode(),
                "manifest_digest": "sha256:" + hashlib.sha256(manifest).hexdigest(),
            }
        ).encode()

    def prove_request(self, **changes: object) -> bytes:
        parsed = json.loads(self.manifest)
        tree = out(self.sc.repo, "rev-parse", f"{self.git_input.carried or self.sc.seed}^{{tree}}")
        data: dict[str, object] = {
            "role": self.role,
            "form": self.form,
            "object_format": self.sc.object_format,
            "tree": tree,
        }
        if self.role == "agent":
            data.update(
                head=self.git_input.carried or self.sc.seed,
                tags=parsed["tags"],
                shallow=parsed["boundaries"],
            )
        data.update(changes)
        return json.dumps(data).encode()


def prepare(
    tmp_path: Path,
    sc: Scenario,
    form: str,
    *,
    export: bool = False,
    settings: SeedSettings | None = None,
) -> Staged:
    """The seed built by the real seed step, and ``/source`` or the received pack in place."""
    git_input = launch_input(sc, export=export)
    built = build_seed(common_of(sc.repo), git_input, settings or SeedSettings())
    root = tmp_path / "container"
    source, workspace, tmp = root / "source", root / "workspace", root / "tmp"
    for path in (source, workspace, tmp):
        path.mkdir(parents=True)
    paths = container_stager.Paths(
        source=str(source),
        workspace=str(workspace),
        received_pack=str(tmp / "autoforge-seed" / "seed.pack"),
        tmp=str(tmp),
    )
    manifest = (built.directory / SEED_MANIFEST_NAME).read_bytes()
    if form == "bind":
        (source / "seed").mkdir()
        shutil.copy(built.directory / SEED_MANIFEST_NAME, source / "seed" / SEED_MANIFEST_NAME)
        shutil.copy(built.directory / SEED_PACK_NAME, source / "seed" / SEED_PACK_NAME)
        with materialize(
            execute, str(common_of(sc.repo)), sc.seed, sc.object_format, prefix="t-"
        ) as tree:
            shutil.copytree(tree.root, source / "tree", symlinks=True)
    else:
        with open(built.directory / SEED_PACK_NAME, "rb") as stream:
            verdict = run_helper(container_stager, ["receive"], stream.read(), paths)
        assert verdict["stager"] == "received"
    staged = Staged(
        sc, git_input, built.directory, manifest, paths, form, "export" if export else "agent"
    )
    return staged


def run_helper(module, argv: list[str], data: bytes, paths) -> dict:
    stdout = io.BytesIO()
    code = module.main(argv, io.BytesIO(data), stdout, paths)
    verdict = json.loads(stdout.getvalue())
    assert (code == 0) == (
        verdict.get("stager", verdict.get("prover")) in ("received", "staged", "proved")
    )
    return verdict


def stage(staged: Staged) -> dict:
    return run_helper(container_stager, ["stage"], staged.stage_request(), staged.paths)


def prover_paths(staged: Staged) -> container_prover.Paths:
    p = staged.paths
    return container_prover.Paths(p.source, p.workspace, p.received_pack, p.tmp)


def prove(staged: Staged, request: bytes | None = None) -> dict:
    return run_helper(
        container_prover, ["prove"], request or staged.prove_request(), prover_paths(staged)
    )


def assert_blob_bytes(sc: Scenario, workspace: Path) -> None:
    for path, data in sc.files.items():
        assert (workspace / path).read_bytes() == data, path
    assert os.readlink(workspace / "link") == "crlf.txt"
    assert (workspace / "run.sh").stat().st_mode & stat.S_IXUSR
    assert not (workspace / "crlf.txt").stat().st_mode & stat.S_IXUSR
    assert (workspace / "mod").is_dir() and not list((workspace / "mod").iterdir())


# -- the agent's repository ---------------------------------------------------------------


@pytest.mark.parametrize("object_format", FORMATS)
@pytest.mark.parametrize("form", FORMS)
def test_an_agent_workspace_holds_the_blobs_bytes_and_the_proof_passes(
    tmp_path, object_format, form, monkeypatch
):
    config, sentinel = hostile_config(tmp_path)
    # A hostile system and global configuration in the stager's own
    # environment: never read, since every git process gets a fresh one.
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    sc = scenario(tmp_path, object_format)
    staged = prepare(tmp_path, sc, form)
    verdict = stage(staged)
    assert verdict == {"stager": "staged", "role": "agent", "head": sc.seed, "tags": 0}
    workspace = Path(staged.paths.workspace)
    assert_blob_bytes(sc, workspace)
    assert not sentinel.exists()
    git_dir = workspace / ".git"
    assert sorted(p.name for p in git_dir.iterdir()) == [
        "HEAD",
        "config",
        "index",
        "info",
        "objects",
        "refs",
        "shallow",
    ]
    assert (git_dir / "shallow").read_text().split() == json.loads(staged.manifest)["boundaries"]
    assert (git_dir / "HEAD").read_text() == sc.seed + "\n"
    assert not list((git_dir / "info").iterdir()), "the checkout's override was removed"
    assert (git_dir / "config").read_text() == container_stager.config_text(object_format)
    assert prove(staged) == {
        "prover": "proved",
        "tree": out(sc.repo, "rev-parse", f"{sc.seed}^{{tree}}"),
    }
    discard(staged.seed_dir)


@pytest.mark.parametrize("form", FORMS)
def test_the_checkout_override_holds_even_where_the_hostile_configuration_is_read(
    tmp_path, form, monkeypatch
):
    """Defense in depth: were the image's system configuration read, the override still holds.

    The negative control runs the same checkout without the override and
    sees the sentinel written and the bytes rewritten, so the test can fail.
    """
    config, sentinel = hostile_config(tmp_path)
    real = container_stager.git_environment

    def reading_hostile(home: str) -> dict[str, str]:
        env = real(home)
        del env["GIT_CONFIG_NOSYSTEM"]
        env["GIT_CONFIG_SYSTEM"] = str(config)
        return env

    monkeypatch.setattr(container_stager, "git_environment", reading_hostile)
    sc = scenario(tmp_path, "sha1")
    staged = prepare(tmp_path, sc, form)
    assert stage(staged)["stager"] == "staged"
    assert_blob_bytes(sc, Path(staged.paths.workspace))
    assert not sentinel.exists()
    assert prove(staged)["prover"] == "proved"

    # The negative control: no override, the same configuration.
    monkeypatch.setattr(container_stager, "CHECKOUT_ATTRIBUTES", b"")
    control = tmp_path / "control"
    control.mkdir()
    staged = prepare(control, scenario(control, "sha1"), form)
    assert stage(staged)["stager"] == "staged"
    assert sentinel.exists()
    assert (Path(staged.paths.workspace) / "crlf.txt").read_bytes() != b"a\nb\n"
    assert prove(staged)["prover"] == "failed"


def test_a_seed_changed_after_the_hand_off_is_refused_by_the_stager(tmp_path):
    sc = scenario(tmp_path, "sha1")
    staged = prepare(tmp_path, sc, "bind")
    pack = Path(staged.paths.source) / "seed" / SEED_PACK_NAME
    pack.write_bytes(pack.read_bytes() + b"x")
    verdict = stage(staged)
    assert verdict["stager"] == "failed" and "size" in verdict["problem"]
    # A manifest other than the one the controller bound is refused too.
    staged = prepare(tmp_path / "again", scenario(tmp_path / "again", "sha1"), "bind")
    other = json.loads(staged.manifest)
    other["boundaries"] = []
    tampered = json.dumps(other).encode()
    (Path(staged.paths.source) / "seed" / SEED_MANIFEST_NAME).write_bytes(tampered)
    verdict = run_helper(container_stager, ["stage"], staged.stage_request(), staged.paths)
    assert verdict["stager"] == "failed" and "not the one the controller" in verdict["problem"]
    # A request whose manifest does not hash to its digest is refused.
    request = json.loads(staged.stage_request())
    request["manifest_digest"] = "sha256:" + "0" * 64
    verdict = run_helper(container_stager, ["stage"], json.dumps(request).encode(), staged.paths)
    assert verdict["stager"] == "failed" and "digest" in verdict["problem"]


def test_a_workspace_that_is_not_empty_is_refused(tmp_path):
    staged = prepare(tmp_path, scenario(tmp_path, "sha1"), "copy")
    (Path(staged.paths.workspace) / "left-over").write_text("x")
    verdict = stage(staged)
    assert verdict["stager"] == "failed" and "not empty" in verdict["problem"]


def test_a_request_with_other_fields_is_refused(tmp_path):
    staged = prepare(tmp_path, scenario(tmp_path, "sha1"), "copy")
    request = json.loads(staged.stage_request())
    request["extra"] = 1
    assert (
        run_helper(container_stager, ["stage"], json.dumps(request).encode(), staged.paths)[
            "stager"
        ]
        == "failed"
    )
    request = json.loads(staged.prove_request())
    request["extra"] = 1
    assert prove(staged, json.dumps(request).encode())["prover"] == "failed"
    assert run_helper(container_stager, ["stage", "--x"], b"", staged.paths)["stager"] == "failed"


# -- the seeded repository's checks (ADR 0013 §7) ----------------------------------------


def _remote(git_dir: Path) -> None:
    with open(git_dir / "config", "a") as handle:
        handle.write('[remote "origin"]\n\turl = https://example.invalid/r.git\n')


def _unknown_key(git_dir: Path) -> None:
    with open(git_dir / "config", "a") as handle:
        handle.write("[user]\n\temail = someone@example.invalid\n")


def _include(git_dir: Path) -> None:
    with open(git_dir / "config", "a") as handle:
        handle.write("[include]\n\tpath = /etc/hostile\n")


def _hooks_path(git_dir: Path) -> None:
    with open(git_dir / "config", "a") as handle:
        handle.write("[core]\n\thooksPath = /tmp\n")


def _extra_ref(git_dir: Path) -> None:
    (git_dir / "refs" / "heads" / "main").write_text((git_dir / "HEAD").read_text())


def _reflog(git_dir: Path) -> None:
    (git_dir / "logs").mkdir()
    (git_dir / "logs" / "HEAD").write_text("x\n")


def _alternates(git_dir: Path) -> None:
    (git_dir / "objects" / "info" / "alternates").write_text("/elsewhere/objects\n")


def _hook(git_dir: Path) -> None:
    (git_dir / "hooks").mkdir()
    (git_dir / "hooks" / "post-checkout").write_text("#!/bin/sh\n")


def _shallow(git_dir: Path) -> None:
    (git_dir / "shallow").write_text("")
    (git_dir / "shallow").unlink()


def _packed_refs(git_dir: Path) -> None:
    (git_dir / "packed-refs").write_text("")


def _symbolic_head(git_dir: Path) -> None:
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")


def _info_attributes(git_dir: Path) -> None:
    (git_dir / "info" / "attributes").write_text("* filter=evil\n")


def _gitdir_file(git_dir: Path) -> None:
    shutil.rmtree(git_dir)
    git_dir.write_text("gitdir: /elsewhere\n")


FAULTS = {
    "an extra ref": (_extra_ref, "an extra ref"),
    "a reflog": (_reflog, "a reflog"),
    "an alternates file": (_alternates, "an alternates file"),
    "a remote": (_remote, "a remote"),
    "a hook": (_hook, "a hook"),
    "a hooks path": (_hooks_path, "a hook"),
    "an unknown configuration key": (_unknown_key, "other keys than the stager's"),
    "an include": (_include, "other keys than the stager's"),
    "a different shallow file": (_shallow, ".git/shallow"),
    "packed refs": (_packed_refs, "an extra ref"),
    "HEAD on a branch": (_symbolic_head, "HEAD is not detached"),
    "an attributes file": (_info_attributes, ".git/info"),
    "a gitdir file": (_gitdir_file, ".git is not a directory"),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_each_fault_a_stager_could_leave_fails_the_prover(tmp_path, fault):
    inject, problem = FAULTS[fault]
    sc = scenario(tmp_path, "sha1")
    staged = prepare(tmp_path, sc, "copy")
    assert stage(staged)["stager"] == "staged"
    git_dir = Path(staged.paths.workspace) / ".git"
    if fault == "a different shallow file":
        # The seed of an ANALYZE_EXECUTE launch is bounded below by itself, so
        # its shallow file names the seed: removing it is a mismatch.
        assert (git_dir / "shallow").exists()
    inject(git_dir)
    verdict = prove(staged)
    assert verdict["prover"] == "failed"
    assert any(problem in text for text in verdict["problems"]), verdict


# -- the ref set, both ways ----------------------------------------------------------------


def test_with_history_tags_off_head_alone_passes_and_any_tag_fails(tmp_path):
    sc = scenario(tmp_path, "sha1")
    staged = prepare(tmp_path, sc, "bind")
    assert stage(staged)["stager"] == "staged"
    assert prove(staged)["prover"] == "proved"
    tags = Path(staged.paths.workspace) / ".git" / "refs" / "tags"
    (tags / "light").write_text(sc.seed + "\n")
    verdict = prove(staged)
    assert verdict["prover"] == "failed" and "an extra ref: refs/tags/light" in verdict["problems"]


@pytest.mark.parametrize("form", FORMS)
def test_with_history_tags_on_exactly_the_manifest_tags_pass(tmp_path, form):
    sc = scenario(tmp_path, "sha1")
    staged = prepare(tmp_path, sc, form, settings=SeedSettings(history_depth=1, history_tags=True))
    manifest = json.loads(staged.manifest)
    assert {tag["ref"] for tag in manifest["tags"]} == {"refs/tags/light", "refs/tags/v1"}
    verdict = stage(staged)
    assert verdict["stager"] == "staged" and verdict["tags"] == 2
    assert prove(staged)["prover"] == "proved"
    tags = Path(staged.paths.workspace) / ".git" / "refs" / "tags"
    # A missing tag.
    saved = (tags / "v1").read_text()
    (tags / "v1").unlink()
    assert "a missing ref: refs/tags/v1" in prove(staged)["problems"]
    # A moved tag.
    (tags / "v1").write_text(sc.seed + "\n")
    assert "a moved ref: refs/tags/v1 is not at the expected object" in prove(staged)["problems"]
    (tags / "v1").write_text(saved)
    # An extra tag.
    (tags / "v2").write_text(sc.seed + "\n")
    assert "an extra ref: refs/tags/v2" in prove(staged)["problems"]
    (tags / "v2").unlink()
    assert prove(staged)["prover"] == "proved"


def test_the_expected_set_is_the_requests_and_a_manifest_changed_inside_changes_nothing(tmp_path):
    sc = scenario(tmp_path, "sha1")
    staged = prepare(
        tmp_path, sc, "bind", settings=SeedSettings(history_depth=1, history_tags=True)
    )
    assert stage(staged)["stager"] == "staged"
    # The container's copy of the manifest, rewritten to name no tag: the
    # prover never reads it, so the engine's set still decides.
    container_manifest = Path(staged.paths.source) / "seed" / SEED_MANIFEST_NAME
    changed = json.loads(container_manifest.read_bytes())
    changed["tags"] = []
    container_manifest.write_text(json.dumps(changed))
    assert prove(staged)["prover"] == "proved"
    # The engine expecting no tag fails the staged tags, whatever the manifest says.
    verdict = prove(staged, staged.prove_request(tags=[]))
    assert verdict["prover"] == "failed"


# -- the tree proof in each form ----------------------------------------------------------


@pytest.mark.parametrize("export", [False, True], ids=["agent", "export"])
@pytest.mark.parametrize("form", FORMS)
def test_a_byte_changed_between_the_stager_and_the_prover_fails_the_proof(tmp_path, form, export):
    sc = scenario(tmp_path, "sha1")
    staged = prepare(tmp_path, sc, form, export=export)
    assert stage(staged)["stager"] == "staged"
    assert prove(staged)["prover"] == "proved"
    target = Path(staged.paths.workspace) / "dir" / "nested.txt"
    target.write_bytes(b"nested b\n")
    verdict = prove(staged)
    assert (
        verdict["prover"] == "failed"
        and "dir/nested.txt's bytes are not its blob's" in verdict["problems"]
    )


@pytest.mark.parametrize(
    "change, problem",
    [
        (lambda w: (w / "extra.txt").write_text("x"), "extra.txt is not in the tree"),
        (lambda w: (w / "run.sh").chmod(0o644), "run.sh's executable bit is not the tree's"),
        (
            lambda w: (w / "mod" / "f").write_text("x"),
            "mod is a submodule, and is not an empty directory",
        ),
        (lambda w: (w / "id.txt").unlink(), "id.txt is missing"),
        (
            lambda w: ((w / "link").unlink(), (w / "link").symlink_to("id.txt")),
            "link's target is not the tree's",
        ),
    ],
)
def test_each_difference_from_the_tree_fails_the_proof(tmp_path, change, problem):
    sc = scenario(tmp_path, "sha1")
    staged = prepare(tmp_path, sc, "copy")
    assert stage(staged)["stager"] == "staged"
    change(Path(staged.paths.workspace))
    verdict = prove(staged)
    assert verdict["prover"] == "failed" and problem in verdict["problems"]


def test_the_expected_tree_is_the_requests_whatever_the_seed_names(tmp_path):
    sc = scenario(tmp_path, "sha1")
    staged = prepare(tmp_path, sc, "bind")
    assert stage(staged)["stager"] == "staged"
    other = out(sc.repo, "rev-parse", f"{sc.first}^{{tree}}")
    verdict = prove(staged, staged.prove_request(tree=other))
    assert verdict["prover"] == "failed"
    # A tree id that no object of the seed has is a failure too, never a pass.
    verdict = prove(staged, staged.prove_request(tree="2" * 40))
    assert verdict["prover"] == "failed"


# -- an export command's workspace ---------------------------------------------------------


@pytest.mark.parametrize("object_format", FORMATS)
@pytest.mark.parametrize("form", FORMS)
def test_an_export_commands_workspace_is_the_tree_with_no_git(tmp_path, form, object_format):
    config, sentinel = hostile_config(tmp_path)
    sc = scenario(tmp_path, object_format)
    staged = prepare(tmp_path, sc, form, export=True)
    if form == "copy":
        # The copy-in form stages no tree on the host.
        assert not any(Path(staged.paths.source).iterdir())
    verdict = stage(staged)
    assert verdict["stager"] == "staged" and verdict["form"] == form
    workspace = Path(staged.paths.workspace)
    assert not (workspace / ".git").exists()
    assert_blob_bytes(sc, workspace)
    assert not sentinel.exists()
    assert prove(staged)["prover"] == "proved"
    (workspace / ".git").mkdir()
    verdict = prove(staged)
    assert "an export command's workspace holds a .git" in verdict["problems"]


# -- the exporter --------------------------------------------------------------------------


def agent_commit(workspace: Path, name: str, content: bytes) -> str:
    """A commit the agent makes in its repository, as an agent's git would."""
    (workspace / name).write_bytes(content)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    ident = ["-c", "user.name=agent", "-c", "user.email=agent@example.invalid"]
    subprocess.run(["git", *ident, "add", name], cwd=workspace, env=env, check=True)
    subprocess.run(
        ["git", *ident, "commit", "-q", "-m", "work"], cwd=workspace, env=env, check=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def export(workspace: Path, git_input: LaunchGitInput, target: Path) -> str:
    """The exporter's two execs, the artifact written as the backend writes it."""
    candidate = container_exporter.resolve_head(str(workspace))
    pack_out = io.BytesIO()
    container_exporter.pack(
        git_input.object_format,
        candidate,
        tuple(oid for oid in (git_input.seed, git_input.carried) if oid),
        pack_out,
        workspace=str(workspace),
        tmp=str(target.parent),
    )
    data = pack_out.getvalue()
    target.mkdir()
    (target / ARTIFACT_PACK).write_bytes(data)
    manifest = CandidateManifest.for_input(
        git_input,
        candidate=candidate,
        pack_bytes=len(data),
        pack_objects=int.from_bytes(data[8:12], "big"),
        pack_sha256=hashlib.sha256(data).hexdigest(),
    )
    (target / ARTIFACT_MANIFEST).write_bytes(manifest.render())
    return candidate


@pytest.mark.parametrize("object_format", FORMATS)
def test_an_agents_commit_is_exported_and_imported_with_the_same_id(tmp_path, object_format):
    sc = scenario(tmp_path, object_format)
    staged = prepare(tmp_path, sc, "copy")
    assert stage(staged)["stager"] == "staged"
    workspace = Path(staged.paths.workspace)
    head = agent_commit(workspace, "feature.py", b"print('feature')\n")
    git_input = staged.git_input.with_seed_manifest(staged.digest)
    candidate = export(workspace, git_input, tmp_path / "artifact")
    assert candidate == head
    common = common_of(sc.repo)
    reader = GitTransport(
        object_directory=common / "objects",
        remote=GitRemote("file:///nonexistent"),
        in_base_history=lambda base, sha: False,
        git_lock=lambda operation: contextlib.nullcontext(),
    )
    imported = import_artifact(
        common,
        tmp_path / "artifact",
        git_input,
        execute,
        read_commit=reader.read_local_commit,
        git_lock=lambda operation: contextlib.nullcontext(),
    )
    assert imported.candidate == head
    assert out(sc.repo, "cat-file", "-t", head) == "commit"


def test_the_exporter_resolves_head_from_files_only(tmp_path):
    workspace = tmp_path / "w"
    git_dir = workspace / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    oid = "a" * 40
    (git_dir / "HEAD").write_text(oid + "\n")
    assert container_exporter.resolve_head(str(workspace)) == oid
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    with pytest.raises(container_exporter.ExportError, match="does not exist"):
        container_exporter.resolve_head(str(workspace))
    (git_dir / "packed-refs").write_text(f"# pack-refs with: peeled\n{oid} refs/heads/main\n")
    assert container_exporter.resolve_head(str(workspace)) == oid
    (git_dir / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    assert container_exporter.resolve_head(str(workspace)) == "b" * 40
    (git_dir / "HEAD").write_text("ref: refs/../../etc/passwd\n")
    with pytest.raises(container_exporter.ExportError, match="not a plain ref"):
        container_exporter.resolve_head(str(workspace))
    stderr = io.BytesIO()
    assert container_exporter.main(["nonsense"], io.BytesIO(), io.BytesIO(), stderr) == 1
    assert json.loads(stderr.getvalue())["exporter"] == "failed"


# -- PID 1 ---------------------------------------------------------------------------------


def _watch(*args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-I", container_watch.__file__, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_the_watcher_ends_at_its_deadline_and_at_once_on_sigterm():
    started = time.monotonic()
    proc = _watch("1")
    assert proc.wait(timeout=30) == container_watch.DEADLINE_STATUS
    assert time.monotonic() - started < 25
    proc = _watch("600")
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 0
    proc = _watch("not-a-number")
    assert proc.wait(timeout=10) == 2


def test_the_helpers_import_nothing_of_autoforge():
    """The image carries each helper file alone (ADR 0013 D7)."""
    for module in (container_stager, container_prover, container_exporter, container_watch):
        source = Path(module.__file__).read_text()
        assert "from autoforge" not in source and "import autoforge" not in source
        assert "from ." not in source, module.__name__
