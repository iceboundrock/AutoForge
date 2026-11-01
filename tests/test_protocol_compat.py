"""The controller/state protocol compatibility preflight (#265).

What the loader decides from a state file's ``protocol_version`` alone
(:func:`autoforge.state.protocol_support`), what its refusal says about the
running controller (:mod:`autoforge.provenance`), and that the controller it
names is the code actually imported, not the checkout the test runs from.
The commands' use of it is in ``tests/test_cli.py``, the scheduler's in
``tests/test_epic_commands.py`` and ``tests/test_worker.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import autoforge
from autoforge import __protocol_version__
from autoforge.errors import StateError, StateProtocolError
from autoforge.provenance import (
    MAX_PATH_CHARS,
    ControllerIdentity,
    command_words,
    mismatch_lines,
    protocol_ranges,
    refusal_headline,
    shown,
    shown_path,
)
from autoforge.state import (
    SUPPORTED_PROTOCOLS,
    AutoForgeState,
    ProtocolSupport,
    load_state,
    protocol_support,
    save_state,
)
from autoforge.transitions import Phase

from .conftest import GOOGLE_API_KEY, GOOGLE_KEY_BODY, git_repo

SRC = Path(autoforge.__file__).resolve().parent


def _state(**kw) -> AutoForgeState:
    base = dict(
        run_id="af-20261021T164241Z-60c7a5",
        repository="owner/repo",
        epic_url="https://github.com/owner/repo/issues/1",
        current_issue_url="https://github.com/owner/repo/issues/2",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        phase=Phase.REVIEW,
    )
    base.update(kw)
    return AutoForgeState(**base)


def _saved(tmp_path: Path, **changes: object) -> Path:
    """A state this controller saved, then ``changes`` written over its JSON."""
    path = tmp_path / "state.json"
    save_state(_state(), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# -- the classification ------------------------------------------------------------------


def test_the_supported_labels_are_the_loaders_rules_and_nothing_else():
    """Every label from_dict has a rule for, pinned: 1-5 and 7-13, 6 skipped
    for good. A new protocol moves this pin with its own bump."""
    assert sorted(SUPPORTED_PROTOCOLS, key=int) == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "7",
        "8",
        "9",
        "10",
        "11",
        "12",
        "13",
    ]
    assert __protocol_version__ == "13"
    assert protocol_ranges(SUPPORTED_PROTOCOLS) == "1-5 and 7-13"


@pytest.mark.parametrize(
    ("label", "support"),
    [
        ("13", ProtocolSupport.CURRENT),
        *((str(n), ProtocolSupport.LEGACY) for n in (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12)),
        ("14", ProtocolSupport.NEWER),
        ("99", ProtocolSupport.NEWER),
        ("999999999", ProtocolSupport.NEWER),
        ("6", ProtocolSupport.UNKNOWN),
        ("0", ProtocolSupport.UNKNOWN),
        ("013", ProtocolSupport.UNKNOWN),
        ("14.0", ProtocolSupport.UNKNOWN),
        ("999.0", ProtocolSupport.UNKNOWN),
        (" 14", ProtocolSupport.UNKNOWN),
        ("14\n", ProtocolSupport.UNKNOWN),
        ("-14", ProtocolSupport.UNKNOWN),
        ("1234567890", ProtocolSupport.UNKNOWN),
        ("", ProtocolSupport.UNKNOWN),
        (13, ProtocolSupport.UNKNOWN),
        (14, ProtocolSupport.UNKNOWN),
        (None, ProtocolSupport.UNKNOWN),
        (True, ProtocolSupport.UNKNOWN),
        (["13"], ProtocolSupport.UNKNOWN),
        ({"v": "13"}, ProtocolSupport.UNKNOWN),
    ],
    ids=repr,
)
def test_protocol_support_classifies_a_label_by_itself(label, support):
    assert protocol_support(label) is support
    assert support.supported is (isinstance(label, str) and label in SUPPORTED_PROTOCOLS)


# -- the refusal -------------------------------------------------------------------------


def test_a_newer_label_is_refused_before_its_phase_and_fields_are_read(tmp_path):
    """A newer controller may add a phase and fields: its file is reported as a
    newer protocol, never as an unknown phase or unknown fields, and the
    refusal names the file and this controller. The file is left byte for
    byte, and nothing is created beside it."""
    path = _saved(
        tmp_path,
        protocol_version="14",
        phase="A_PHASE_OF_PROTOCOL_14",
        a_field_of_protocol_14={"x": 1},
        controller_version="0.2.0",
    )
    before = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
    with pytest.raises(StateProtocolError) as refused:
        load_state(path)
    exc = refused.value
    assert isinstance(exc, StateError)
    assert str(exc).startswith(
        f"state file {path}: unsupported protocol_version '14' (controller speaks '13'): "
        "a newer AutoForge wrote it, and this controller, autoforge "
    )
    assert "unknown phase" not in str(exc) and "unknown field" not in str(exc)
    assert exc.headline == (
        "unsupported protocol_version '14' (controller speaks '13'): a newer AutoForge wrote it"
    )
    assert (exc.label, exc.support, exc.newer) == ("'14'", "unsupported-newer", "14")
    assert (exc.run_id, exc.written_by, exc.path) == (
        "af-20261021T164241Z-60c7a5",
        "0.2.0",
        str(path),
    )
    assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


@pytest.mark.parametrize("label", ["6", "0", "14.0", 14, None, ["14"]], ids=repr)
def test_an_unknown_label_is_refused_as_no_protocol_and_left_unchanged(tmp_path, label):
    path = _saved(tmp_path, protocol_version=label, phase="NOT_A_PHASE")
    before = path.read_bytes()
    with pytest.raises(StateProtocolError) as refused:
        load_state(path)
    exc = refused.value
    assert f"unsupported protocol_version {label!r} (controller speaks '13')" in str(exc)
    assert "no AutoForge protocol has that label" in str(exc)
    assert (exc.support, exc.newer) == ("unsupported-unknown", "")
    assert path.read_bytes() == before


@pytest.mark.parametrize("label", [None, "13", "12", "7", "1"], ids=repr)
def test_a_supported_label_still_meets_the_loaders_own_checks(tmp_path, label):
    """Classification only admits a label to the loader's rules: an unknown
    phase is still an unknown phase, and an unknown field still an unknown
    field, under the current, an older and no label."""
    changes: dict = {"phase": "NOT_A_PHASE"}
    if label is not None:
        changes["protocol_version"] = label
    path = _saved(tmp_path, **changes)
    if label is None:
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["protocol_version"]
        path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError, match="state file has unknown phase 'NOT_A_PHASE'") as refused:
        load_state(path)
    assert not isinstance(refused.value, StateProtocolError)
    path = _saved(tmp_path, protocol_version="13", a_field_of_protocol_14=1)
    with pytest.raises(StateError, match="unknown field") as refused:
        load_state(path)
    assert not isinstance(refused.value, StateProtocolError)


def test_the_mode_is_still_read_before_the_label(tmp_path):
    """ADR 0009 D4: a LOCAL file is an unknown mode under a newer label too."""
    path = _saved(tmp_path, protocol_version="14", mode="LOCAL")
    with pytest.raises(StateError) as refused:
        load_state(path)
    assert str(refused.value) == "state file has unknown mode 'LOCAL'"
    assert not isinstance(refused.value, StateProtocolError)


def test_a_refusal_shows_the_file_only_bounded_and_in_the_shape_a_controller_writes(tmp_path):
    """The label is shown as a bounded repr, so a control character or an
    escape sequence cannot forge a line; a run id or a writer version that a
    controller would not write is not shown at all."""
    path = _saved(
        tmp_path,
        protocol_version="14\n\x1b[31m" + "9" * 10_000,
        run_id="../../etc",
        controller_version="0.2.0\nautoforge: forged",
    )
    with pytest.raises(StateProtocolError) as refused:
        load_state(path)
    exc = refused.value
    assert len(exc.label) <= 64 and exc.label.endswith("[... cut]")
    assert "\n" not in str(exc) and "\x1b" not in str(exc)
    assert (exc.run_id, exc.written_by) == ("", "")
    assert len(str(exc)) < len(str(path)) + 2 * MAX_PATH_CHARS + 600


# -- the controller it names --------------------------------------------------------------


def test_the_identity_is_the_imported_package_and_the_running_interpreter():
    identity = ControllerIdentity.current()
    assert identity.module == os.path.abspath(autoforge.__file__)
    assert identity.python == sys.executable
    assert (identity.version, identity.protocol, identity.prompt_version) == (
        autoforge.__version__,
        autoforge.__protocol_version__,
        autoforge.__prompt_version__,
    )
    assert identity.summary() == (
        f"autoforge {autoforge.__version__} (protocol 13) imported from "
        f"{os.path.abspath(autoforge.__file__)}, python {sys.executable}"
    )


def test_a_refusal_names_whatever_package_was_imported(tmp_path, monkeypatch):
    """The import path shown is the package object's own ``__file__`` at the
    moment of the refusal, not a path the controller assumes."""
    elsewhere = "/opt/another-install/site-packages/autoforge/__init__.py"
    monkeypatch.setattr(sys.modules["autoforge"], "__file__", elsewhere)
    monkeypatch.setattr(sys, "executable", "/opt/another-install/bin/python3")
    path = _saved(tmp_path, protocol_version="14")
    with pytest.raises(StateProtocolError) as refused:
        load_state(path)
    assert f"imported from {elsewhere}, python /opt/another-install/bin/python3" in str(
        refused.value
    )


def test_shown_path_is_one_bounded_redacted_line():
    assert shown_path("") == shown_path(None) == "(unknown)"
    assert shown_path("/a/b") == "/a/b"
    assert shown_path("/a\nb") == repr("/a\nb")
    assert shown_path("/a‮b") == repr("/a‮b")
    long = "/" + "x" * (2 * MAX_PATH_CHARS)
    assert len(shown_path(long)) == MAX_PATH_CHARS and shown_path(long).endswith("[... cut]")
    assert GOOGLE_KEY_BODY not in shown_path(f"/home/{GOOGLE_API_KEY}/autoforge")


_CUT = " [... cut]"


def _crossing(limit: int, *, before: str = "", quoted: bool = False) -> str:
    """A text whose credential starts before ``limit``'s cut and ends after it."""
    lead = len(before) + (1 if quoted else 0)
    pad = (limit - len(_CUT)) - lead - 12  # the key's first 12 characters fall before the cut
    text = before + "x" * (pad - 1) + "/" + GOOGLE_API_KEY + "/state.json"
    shown_form = repr(text) if quoted else text
    start = shown_form.index("AIza")
    assert start < limit - len(_CUT) < start + len(GOOGLE_API_KEY)
    return text


def test_a_credential_crossing_the_cut_is_redacted_whole_before_it(tmp_path):
    """#265 R1-F2: a value is redacted whole, then cut. Cutting first would
    leave the key's first characters, no longer a shape the redaction knows,
    on the line: a path, a protocol label and a refusal's message alike."""
    path = _crossing(MAX_PATH_CHARS, before="/home/")
    for text in (shown_path(path), shown(path, MAX_PATH_CHARS)):
        assert len(text) <= MAX_PATH_CHARS and text.endswith("[... cut]")
        assert "AIza" not in text and "SyFAKE" not in text
    label = _crossing(64, before="14 ", quoted=True)
    exc = _refusal(tmp_path, protocol_version=label)
    assert len(exc.label) <= 64 and exc.label.endswith("[... cut]")
    assert exc.label.startswith("'14 xxx")
    for text in (exc.label, exc.headline, exc.reason, str(exc)):
        assert "AIza" not in text and "SyFAKE" not in text
    lines = mismatch_lines(
        exc, identity=ControllerIdentity.current(), reads="1-5 and 7-13", cwd=path, flags=()
    )
    text = "\n".join([refusal_headline(exc), *lines])
    assert "AIza" not in text and "SyFAKE" not in text


def test_the_headline_names_the_state_file_on_one_bounded_line(tmp_path):
    """#265 R1-F3: the headline is the diagnostic's first line, and the state
    file's path in it, newline, escape or length and all, is shown like every
    other path (``shown_path``): it cannot start a forged line, emit a
    terminal control sequence or run on."""
    exc = _refusal(tmp_path, protocol_version="14")
    for path in (
        "/srv/state\nautoforge: error: forged/state.json",
        "/srv/\x1b]0;title\x07\x1b[2J/state.json",
        "/srv/" + "p" * (3 * MAX_PATH_CHARS) + "/state.json",
    ):
        headline = refusal_headline(exc.at(path))
        assert headline.startswith("autoforge: error: state file ")
        assert headline.isprintable() and "\x1b" not in headline and "\n" not in headline
        assert len(headline) <= MAX_PATH_CHARS + 200
        assert shown_path(path) in headline
        assert headline.endswith(exc.headline)
        assert shown_path(path) in str(exc.at(path, shown=shown_path(path)))


def test_a_flag_value_not_safe_to_repeat_is_a_labelled_placeholder():
    """#265 R1-F3: a recovery command repeats a flag value only when it is
    the value typed, printable, bounded and nothing the redaction changes;
    shell quoting alone keeps a word one argument, not one safe line."""
    words, held = command_words(("--config", "my config.yaml", "--state-dir", ".autoforge/x"))
    assert (words, held) == (["--config", "'my config.yaml'", "--state-dir", ".autoforge/x"], False)
    for bad in (
        "cfg\nautoforge: error: forged",
        "cfg\x1b[31m.yaml",
        "cfg\u202e.yaml",
        "c" * (MAX_PATH_CHARS + 1),
        f"/home/{GOOGLE_API_KEY}.yaml",
    ):
        words, held = command_words(("--config", bad, "--state-dir", "sd"))
        assert held
        assert words == [
            "--config",
            "<the --config value this command was given>",
            "--state-dir",
            "sd",
        ]
        words, held = command_words(("--config", "c.yaml", "--state-dir", bad))
        assert held and words[-1] == "<the --state-dir value this command was given>"


@pytest.mark.parametrize(
    "value",
    [
        "sd\nautoforge: error: forged",
        "sd\x1b[2J\x1b[31m",
        "s" * (4 * MAX_PATH_CHARS),
        f"/home/{GOOGLE_API_KEY}",
    ],
    ids=["newline", "escape", "long", "credential"],
)
def test_the_recovery_guidance_is_bounded_and_escaped_line_by_line(tmp_path, value):
    exc = _refusal(tmp_path, protocol_version="14")
    identity = ControllerIdentity("0.1.0", "13", "v14", "/src/autoforge/__init__.py", "/py")
    lines = mismatch_lines(
        exc.at(value),
        identity=identity,
        reads="1-5 and 7-13",
        cwd=value,
        flags=("--config", value, "--state-dir", value),
    )
    for line in lines:
        assert line.isprintable() and "\x1b" not in line, line
        assert len(line) <= 2 * MAX_PATH_CHARS + 300, len(line)
        assert not line.startswith("autoforge: error: forged")
        assert "AIza" not in line and GOOGLE_KEY_BODY not in line
    command = next(line for line in lines if "<an autoforge that speaks protocol 14>" in line)
    assert command.strip() == (
        "<an autoforge that speaks protocol 14> --config <the --config value this command was "
        "given> --state-dir <the --state-dir value this command was given> status"
    )
    assert any("put back the value you typed" in line for line in lines)


def _refusal(tmp_path: Path, **changes: object) -> StateProtocolError:
    with pytest.raises(StateProtocolError) as refused:
        load_state(_saved(tmp_path, **changes))
    return refused.value


def test_the_diagnostic_of_a_newer_label_names_a_controller_to_use_by_placeholder(tmp_path):
    exc = _refusal(tmp_path, protocol_version="14", controller_version="0.2.0")
    identity = ControllerIdentity("0.1.0", "13", "v14", "/src/autoforge/__init__.py", "/py")
    lines = mismatch_lines(
        exc,
        identity=identity,
        reads="1-5 and 7-13",
        cwd="/work/repo",
        flags=("--config", "my config.yaml", "--state-dir", ".autoforge/x"),
    )
    text = "\n".join(lines)
    assert lines[0] == "AutoForge controller/state protocol mismatch"
    assert f"  state:      {exc.path} (protocol_version '14')" in lines
    assert (
        "  recorded:   run af-20261021T164241Z-60c7a5, written by autoforge 0.2.0 "
        "(as the file says; it was not loaded)"
    ) in lines
    assert "  controller: autoforge 0.1.0, protocol 13 (reads 1-5 and 7-13), prompt v14" in lines
    assert "  import:     /src/autoforge/__init__.py" in lines
    assert "  python:     /py" in lines
    assert "  cwd:        /work/repo" in lines
    assert (
        "                <an autoforge that speaks protocol 14> --config 'my config.yaml' "
        "--state-dir .autoforge/x status"
    ) in lines
    for advice in ("--force", "edit the", "downgrade", "install", "checkout", "PYTHONPATH"):
        assert advice not in text, advice
    assert "Leave the state file as it is" in text
    assert "<the --" not in text and "put back" not in text  # every flag was shown as typed


def test_the_diagnostic_of_an_unknown_label_names_no_controller_to_use(tmp_path):
    """#265 R1-F4: nor any way to set the file aside: no ``run --force``, no
    repair and no edit, for a label no protocol has either."""
    exc = _refusal(tmp_path, protocol_version="6")
    lines = mismatch_lines(
        exc, identity=ControllerIdentity.current(), reads="1-5 and 7-13", cwd="/w", flags=()
    )
    text = "\n".join(lines)
    assert "speaks protocol" not in text and "<an autoforge" not in text
    assert "no AutoForge protocol has this label" in text
    assert "recorded:" in text  # the run id has a controller's shape
    for advice in ("--force", "repair", "edit the", "unreadable", "downgrade", "install"):
        assert advice not in text, advice
    assert "Leave the state file as it is" in text


@pytest.mark.parametrize("label", ["14", "6"])
def test_the_result_line_claims_only_what_holds_before_and_under_the_lock(tmp_path, label):
    """#265 R1-F5: the same diagnostic is printed for a refusal before the
    lock and for one under it, where taking the lock may already have
    written its PID record, so it claims no more than both established."""
    exc = _refusal(tmp_path, protocol_version=label)
    lines = mismatch_lines(
        exc, identity=ControllerIdentity.current(), reads="1-5 and 7-13", cwd="/w", flags=()
    )
    assert (
        "  result:     refused: the state file and its run logs were not rewritten, the agent "
        "worktree was not touched, no agent was launched and nothing was written to GitHub"
    ) in lines
    assert "before anything was written" not in "\n".join(lines)


def test_a_secret_shaped_run_id_or_flag_never_reaches_the_diagnostic(tmp_path):
    exc = _refusal(tmp_path, protocol_version="14", run_id=GOOGLE_API_KEY)
    lines = mismatch_lines(
        exc,
        identity=ControllerIdentity.current(),
        reads="1-5 and 7-13",
        cwd="/w",
        flags=("--config", f"/home/{GOOGLE_API_KEY}.yaml"),
    )
    assert GOOGLE_KEY_BODY not in "\n".join(lines)


# -- a controller imported from somewhere else ---------------------------------------------


def _older_copy(tmp_path: Path) -> Path:
    """This package copied outside the checkout and labelled one protocol older.

    The #243/#254 incident in miniature: a controller that speaks protocol
    12 meets a state this one (13) wrote. The copy's own loader classifies 13
    as newer, exactly as a protocol-10 controller classified 11.
    """
    root = tmp_path / "older-install"
    shutil.copytree(SRC, root / "autoforge", ignore=shutil.ignore_patterns("__pycache__"))
    init = root / "autoforge" / "__init__.py"
    text = init.read_text(encoding="utf-8")
    assert '__protocol_version__ = "13"' in text
    init.write_text(text.replace('"13"', '"12"'), encoding="utf-8")
    return root


def test_an_older_controller_imported_from_elsewhere_names_itself_and_writes_nothing(tmp_path):
    """End to end in a process of its own: ``status`` and ``resume`` under the
    older copy refuse the protocol-13 state with exit 2, name the copy's
    ``__init__.py`` and the interpreter, not this checkout, and leave the
    state directory and the git directory as they were."""
    copy = _older_copy(tmp_path)
    repo = git_repo(tmp_path / "repo")
    state_dir = repo / ".autoforge"
    state_dir.mkdir()
    save_state(_state(), state_dir / "state.json")

    def tree() -> dict[str, bytes]:
        out = {}
        for base in (state_dir, repo / ".git"):
            for directory, _, files in os.walk(base):
                for name in files:
                    p = Path(directory) / name
                    out[str(p)] = p.read_bytes()
        return out

    before = tree()
    for words in (["status"], ["resume"], ["resume", "--dry-run"]):
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from autoforge.cli import main; sys.exit(main(sys.argv[2:]))",
                str(copy),
                "--state-dir",
                ".autoforge",
                *words,
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 2, proc.stderr
        assert proc.stdout == ""
        err = proc.stderr
        assert (
            "unsupported protocol_version '13' (controller speaks '12'): a newer AutoForge wrote it"
        ) in err
        assert f"  import:     {copy / 'autoforge' / '__init__.py'}" in err
        assert str(SRC) not in err
        assert f"  python:     {sys.executable}" in err
        assert f"  cwd:        {repo}" in err
        assert "  controller: autoforge" in err and "protocol 12 (reads 1-5 and 7-12)" in err
        assert "<an autoforge that speaks protocol 13> --state-dir .autoforge status" in err
        assert tree() == before, words
