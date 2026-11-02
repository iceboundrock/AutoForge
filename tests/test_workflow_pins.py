"""Every action a workflow runs is pinned to a commit (#288, audit finding M8 of #257).

A tag or a branch is a name the action's owners can move: ``@v7`` runs
whatever commit ``v7`` points to on the day of the run, and in CI that code
produces the ``ci`` check the merge gate reads. A full commit SHA names one
tree. So every ``uses:`` in ``.github/workflows/``, a step's or a reusable
workflow's, is written as::

    uses: owner/repo[/path]@<40 lowercase hex> # vX.Y.Z

with the release that commit is as the comment, for the reader and for
whoever updates it. The check refuses anything else, and fails closed:

- a tag, a branch or a short SHA, and a SHA without its release beside it;
- a Docker image (``docker://``) or a local action (``./``): neither is an
  action repository's commit, and a local action's own ``action.yml`` could
  ``uses:`` a tag this check does not read. Allowing either takes a change
  here, made on purpose;
- a ``uses:`` the line scan cannot read, written in flow style, quoted,
  split across lines or merged in from elsewhere: PyYAML (which the dev
  group always installs) locates each job's and step's ``uses`` key, the
  line it is on is read for the comment YAML drops, and what those lines
  hold must be what YAML reads, in the same order. A line of a multi-line
  ``run`` script, of any other value, or a comment is no reference, however
  much it reads like one;
- two references to one action repository that name different commits or
  releases, in one workflow or across them, so an update moves every
  occurrence at once (``actions/cache/restore`` and ``actions/cache/save``
  are one repository).

Whether a SHA is really the commit of the release its comment names is not
something a test can know offline; docs/development.md, "Pinned actions",
says how to resolve one from the action's own repository and how to update
a pin. Nothing here runs a workflow.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
# The two extensions GitHub reads a workflow from.
WORKFLOW_SUFFIXES = (".yml", ".yaml")
# owner/repo, an optional path inside it (an action in a subdirectory, a
# reusable workflow's file), then a full commit SHA.
PINNED = re.compile(r"(?P<repo>[\w.-]+/[\w.-]+)(?:/[\w./-]+)?@[0-9a-f]{40}")
RELEASE = re.compile(r"v\d+\.\d+\.\d+")
# A `uses:` key at the start of its line, as a list item or not, and its
# value. YAML starts a comment only after whitespace, so the reference runs
# to it. Only the lines YAML locates a job's or step's `uses` key on are read.
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?:\s+#\s*(?P<comment>.*?))?\s*$")

# For the synthetic workflows below only: no test requires a real workflow
# to pin this commit, so updating a pin never touches this file.
CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
CHECKOUT = f"actions/checkout@{CHECKOUT_SHA}"
OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"


def workflow_files(directory: Path = WORKFLOWS_DIR) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.suffix in WORKFLOW_SUFFIXES)


def _yaml_references(text: str) -> list[str]:
    """Every ``uses:`` GitHub reads: a job's (a reusable workflow) and each step's."""
    data: Any = yaml.safe_load(text)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    references: list[str] = []
    for job in (jobs or {}).values():
        if "uses" in job:
            references.append(str(job["uses"]))
        for step in job.get("steps") or []:
            if "uses" in step:
                references.append(str(step["uses"]))
    return references


def _entries(node: yaml.Node | None, key: str) -> list[tuple[yaml.Node, yaml.Node]]:
    """Each ``key: value`` of ``node`` as YAML composes it, if ``node`` is a mapping."""
    if not isinstance(node, yaml.MappingNode):
        return []
    return [(k, v) for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == key]


def _uses_lines(text: str) -> list[int]:
    """The line (from 0) of each ``uses`` key GitHub reads: a job's and each step's.

    YAML's own parse locates them, in the order :func:`_yaml_references`
    reads them, so neither a line of a multi-line ``run`` script (or of any
    other value) nor a comment is one. A reference YAML reads but this does
    not locate, one merged in by ``<<`` say, fails the check in
    :func:`problems`.
    """
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    lines: list[int] = []
    for _, jobs in _entries(root, "jobs"):
        for _, job in jobs.value if isinstance(jobs, yaml.MappingNode) else []:
            lines += [key.start_mark.line for key, _ in _entries(job, "uses")]
            for _, steps in _entries(job, "steps"):
                for step in steps.value if isinstance(steps, yaml.SequenceNode) else []:
                    lines += [key.start_mark.line for key, _ in _entries(step, "uses")]
    return lines


def problems(workflows: Mapping[str, str]) -> list[str]:
    """What keeps ``workflows`` (file name to text) from being pinned, or ``[]``."""
    found: list[str] = []
    pins: dict[str, set[tuple[str, str]]] = {}
    for name, text in workflows.items():
        # YAML counts the same line breaks as splitlines in any text it parses.
        text_lines = text.splitlines()
        located = _uses_lines(text)
        lines: list[tuple[int, re.Match[str]]] = []
        for index in located:
            if match := USES_LINE.match(text_lines[index]):
                lines.append((index + 1, match))
            else:
                found.append(
                    f"{name}:{index + 1}: write each `uses:` on a line of its own as "
                    f"`uses: owner/repo@<sha> # vX.Y.Z`, not {text_lines[index].strip()!r}"
                )
        scanned = [match["ref"] for _, match in lines]
        read = _yaml_references(text)
        # A line found unreadable above already fails; otherwise the lines
        # must hold exactly what YAML reads (a quoted value, an alias, a
        # reference merged in from another mapping all differ).
        if len(lines) == len(located) and scanned != read:
            found.append(
                f"{name}: write each `uses:` on a line of its own as "
                f"`uses: owner/repo@<sha> # vX.Y.Z`; YAML reads {read}, the line scan {scanned}"
            )
        for number, match in lines:
            ref, comment = match["ref"], match["comment"]
            pinned = PINNED.fullmatch(ref)
            if not pinned:
                found.append(f"{name}:{number}: {ref} is not pinned to a full commit SHA")
                continue
            if comment is None or not RELEASE.fullmatch(comment):
                found.append(
                    f"{name}:{number}: {ref} needs its release as `# vX.Y.Z`, not {comment!r}"
                )
                continue
            sha = ref.rsplit("@", 1)[1]
            pins.setdefault(pinned["repo"], set()).add((sha, comment))
    for repo, seen in sorted(pins.items()):
        if len(seen) > 1:
            found.append(f"{repo} is pinned to more than one commit or release: {sorted(seen)}")
    return found


def _real() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in workflow_files()}


def _workflow(*steps: str, jobs: str = "") -> str:
    body = "".join(f"      {step}\n" for step in steps)
    return f"on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n{body}{jobs}"


def _each_pin_undone(name: str, text: str) -> list[tuple[str, str]]:
    """``text`` with each of its pins in turn replaced by its release's major tag.

    Each comes with the one problem the check must then report. The pins are
    the lines :func:`_uses_lines` locates, never a script line that only
    reads like one.
    """
    lines = text.splitlines(keepends=True)
    undone: list[tuple[str, str]] = []
    for index in _uses_lines(text):
        match = USES_LINE.match(lines[index])
        assert match, f"{name}:{index + 1} is not a `uses:` line the check reads"
        release = match["comment"]
        assert release, f"{name}:{index + 1} has no release comment"
        tag = f"{match['ref'].rsplit('@', 1)[0]}@{release.split('.')[0]}"
        line = lines[index]
        tagged = [*lines[:index], f"{line[: match.start('ref')]}{tag}\n", *lines[index + 1 :]]
        problem = f"{name}:{index + 1}: {tag} is not pinned to a full commit SHA"
        undone.append(("".join(tagged), problem))
    return undone


# A step whose multi-line script writes a composite action: the script's
# `uses:` lines are text the shell gets, not references GitHub reads.
SCRIPT = (
    "- run: {indicator}\n"
    "          cat > action.yml <<'EOF'\n"
    "          runs:\n"
    "            using: composite\n"
    "            steps:\n"
    "              - uses: actions/checkout@v7\n"
    "              - uses: actions/setup-python@{sha} # v6.0.0\n"
    "          EOF"
)
SCRIPTS = [SCRIPT.format(indicator=i, sha=OTHER_SHA) for i in ("|", "|-", ">")]


def test_the_check_reads_every_workflow_github_runs():
    """Discovery and parsing, whether or not a workflow uses an action at all."""
    names = [p.name for p in workflow_files()]
    assert {"ci.yml", "pypi-publish.yml"} <= set(names), names
    for name, text in _real().items():
        data: Any = yaml.safe_load(text)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        assert isinstance(jobs, dict) and jobs, f"{name} does not read as a workflow with jobs"


def test_discovery_takes_both_workflow_extensions_and_nothing_else(tmp_path: Path):
    for name in ("b.yaml", "a.yml", "notes.md", "c.yml.bak", "README"):
        (tmp_path / name).write_text("", encoding="utf-8")
    assert [p.name for p in workflow_files(tmp_path)] == ["a.yml", "b.yaml"]


def test_every_action_of_every_workflow_is_pinned_to_one_commit_with_its_release():
    assert problems(_real()) == []


def test_a_workflow_that_uses_no_action_has_nothing_to_pin():
    """`run` steps alone pass, in a workflow of their own and beside the real ones."""
    lint = "  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: ruff check .\n"
    action_free = _workflow("- run: make check", "- run: echo actions/checkout@v7", jobs=lint)
    assert _yaml_references(action_free) == []
    assert problems({"x.yml": action_free}) == []
    assert problems({**_real(), "x.yml": action_free}) == []


def test_a_tag_in_place_of_any_real_pin_fails_the_check():
    """The acceptance criterion on the files it is about: undo any one pin and the check fails.

    Each pin is read from the workflow as it stands and replaced by its
    release's major tag (``@v7`` for ``# v7.0.1``), so a pin updated as
    docs/development.md says leaves this test meaning what it meant.
    """
    undone = 0
    for name, text in _real().items():
        for tagged, problem in _each_pin_undone(name, text):
            assert problems({name: tagged}) == [problem]
            undone += 1
    assert undone, "no workflow pins an action, so there is no real pin to undo"


@pytest.mark.parametrize("script", SCRIPTS)
def test_a_multi_line_script_that_reads_like_a_uses_line_is_no_reference(script: str):
    """A `run: |` script's lines are its own, alone and beside real references."""
    action_free = _workflow(script)
    assert _yaml_references(action_free) == [] == _uses_lines(action_free)
    assert problems({"x.yml": action_free}) == []
    assert problems({**_real(), "x.yml": action_free}) == []
    pinned = f"- uses: {CHECKOUT} # v7.0.1"
    for steps in ((pinned, script), (script, pinned), (pinned, script, pinned)):
        assert problems({"x.yml": _workflow(*steps)}) == []
    tagged = _workflow(script, "- uses: actions/checkout@v7", script)
    (found,) = problems({"x.yml": tagged})
    assert found == "x.yml:14: actions/checkout@v7 is not pinned to a full commit SHA"


def test_undoing_pins_takes_the_real_references_and_no_script_line():
    """The mutation test's own selection: the script's pin-like line is never undone."""
    text = _workflow(SCRIPTS[0], f"- uses: {CHECKOUT} # v7.0.1")
    ((tagged, problem),) = _each_pin_undone("x.yml", text)
    assert problem == "x.yml:14: actions/checkout@v7 is not pinned to a full commit SHA"
    assert problems({"x.yml": tagged}) == [problem]


@pytest.mark.parametrize(
    ("step", "complaint"),
    [
        ("- uses: actions/checkout@v7", "actions/checkout@v7 is not pinned"),
        ("- uses: actions/checkout@v7.0.1", "actions/checkout@v7.0.1 is not pinned"),
        ("- uses: actions/checkout@main", "actions/checkout@main is not pinned"),
        ("- uses: actions/checkout@3d3c42e", "actions/checkout@3d3c42e is not pinned"),
        (f"- uses: actions/checkout@{CHECKOUT_SHA.upper()} # v7.0.1", "is not pinned"),
        (f"- uses: {CHECKOUT}", "needs its release as `# vX.Y.Z`, not None"),
        (f"- uses: {CHECKOUT} # v7", "needs its release as `# vX.Y.Z`, not 'v7'"),
        (f"- uses: {CHECKOUT} # main", "needs its release"),
        ("- uses: docker://alpine:3.20", "docker://alpine:3.20 is not pinned"),
        ("- uses: ./.github/actions/setup", "./.github/actions/setup is not pinned"),
        ('- uses: "actions/checkout@v7"', '"actions/checkout@v7" is not pinned'),
        ("- {uses: actions/checkout@v7}", "write each `uses:` on a line of its own"),
        (f"- {{uses: {CHECKOUT}}}", "write each `uses:` on a line of its own"),
        (f"- uses:\n          {CHECKOUT} # v7.0.1", "write each `uses:` on a line of its own"),
        (f"- uses: |\n          {CHECKOUT}", "write each `uses:` on a line of its own"),
        (f"- uses: &pin {CHECKOUT} # v7.0.1", "write each `uses:` on a line of its own"),
    ],
)
def test_a_reference_that_is_not_a_pinned_commit_fails_the_check(step: str, complaint: str):
    found = problems({"x.yml": _workflow(step)})
    assert found, f"{step!r} passed the check"
    assert any(complaint in problem for problem in found), found


def test_a_reusable_workflow_and_an_action_in_a_subdirectory_are_checked_too():
    reusable = "  release:\n    uses: octo/tools/.github/workflows/release.yml@{ref}\n"
    pinned = _workflow(
        f"- uses: actions/cache/restore@{OTHER_SHA} # v6.1.0",
        f"- uses: actions/cache/save@{OTHER_SHA} # v6.1.0",
        jobs=reusable.format(ref=f"{OTHER_SHA} # v2.0.0"),
    )
    assert problems({"x.yml": pinned}) == []
    tagged = pinned.replace(f"release.yml@{OTHER_SHA} # v2.0.0", "release.yml@main")
    (found,) = problems({"x.yml": tagged})
    assert "octo/tools/.github/workflows/release.yml@main is not pinned" in found


def test_one_action_repository_is_pinned_to_one_commit_and_release_across_workflows():
    ci = _workflow(f"- uses: {CHECKOUT} # v7.0.1")
    moved = _workflow(f"- uses: actions/checkout@{OTHER_SHA} # v7.0.1")
    relabelled = _workflow(f"- uses: {CHECKOUT} # v7.0.0")
    assert problems({"a.yml": ci, "b.yml": ci}) == []
    for other in (moved, relabelled):
        (found,) = problems({"a.yml": ci, "b.yml": other})
        assert "actions/checkout is pinned to more than one commit or release" in found
    restore = _workflow(
        f"- uses: actions/cache/restore@{OTHER_SHA} # v6.1.0",
        "- uses: actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0",
    )
    (found,) = problems({"a.yml": restore})
    assert "actions/cache is pinned to more than one commit or release" in found


def test_only_a_real_uses_counts_and_one_the_scan_cannot_read_fails():
    """A comment, a script or an input is no reference; one spelled another way fails closed."""
    commented_tag = _workflow("# - uses: actions/checkout@v7", f"- uses: {CHECKOUT} # v7.0.1")
    assert problems({"x.yml": commented_tag}) == []
    commented_pin = _workflow(f"# - uses: {CHECKOUT} # v7.0.1", "- uses: actions/checkout@v7")
    (found,) = problems({"x.yml": commented_pin})
    assert "actions/checkout@v7 is not pinned" in found
    script = _workflow(f"- uses: {CHECKOUT} # v7.0.1", "- run: echo actions/checkout@v7")
    assert problems({"x.yml": script}) == []
    quoted_script = _workflow(f"- uses: {CHECKOUT} # v7.0.1", '- run: "echo\n          uses: x@v7"')
    assert problems({"x.yml": quoted_script}) == []
    an_input = _workflow(f"- uses: {CHECKOUT} # v7.0.1\n        with:\n          uses: x@v7")
    assert problems({"x.yml": an_input}) == []
    quoted_key = _workflow(f"- uses: {CHECKOUT} # v7.0.1", '- "uses": actions/checkout@v7')
    (found,) = problems({"x.yml": quoted_key})
    assert "write each `uses:` on a line of its own" in found
    # A `uses` key the line scan cannot read fails even when the other lines
    # hold all YAML reads (of two keys, safe_load keeps the second).
    duplicate = _workflow(f'- "uses": actions/checkout@v7\n        uses: {CHECKOUT} # v7.0.1')
    (found,) = problems({"x.yml": duplicate})
    assert found.startswith("x.yml:6: write each `uses:` on a line of its own"), found
    # A reference merged in from another step is on no line of its own.
    anchored = f"- &checkout\n        uses: {CHECKOUT} # v7.0.1"
    merged = _workflow(anchored, "- <<: *checkout\n        name: again")
    (found,) = problems({"x.yml": merged})
    assert "write each `uses:` on a line of its own" in found
    assert problems({"x.yml": _workflow(anchored, "- *checkout")}) == []
