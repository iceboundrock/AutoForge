"""The README's claims, held to the code and to the files they link (#262).

``README.md`` is the page a first-time reader meets, and the one PyPI
shows. Its first screen says what AutoForge is, its status and where to
start; its feature matrix gives each capability exactly one status and the
evidence for it; its provider matrix names each provider's share of the
built-in routing; its Prerequisites state the version floors ``doctor``
enforces. Each is a claim a later change to the code can make false
without touching the README. These tests keep them checkable, and are the
first offline item of the documentation accuracy checklist
(``docs/releasing.md``). They read files, and run ``doctor`` against the
fake runner of ``tests/test_doctor.py``; nothing reaches GitHub or an agent.
"""

from __future__ import annotations

import re
from pathlib import Path

from autoforge.config import KNOWN_PROVIDERS, default_config
from autoforge.doctor import Doctor
from autoforge.github import GH_MIN_VERSION
from autoforge.profiles import profile_for_phase, review_profile_name
from autoforge.providers import OPENCODE_MIN_VERSION
from autoforge.transitions import Phase
from tests.test_doctor import _runner_factory
from tests.test_troubleshooting_guide import _broken, _cells, _links, _markdown_files

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
INDEX = ROOT / "docs" / "README.md"
CONFIGURATION = ROOT / "docs" / "configuration.md"
COMPATIBILITY = ROOT / "docs" / "compatibility.md"
STATUSES = ("**Supported**", "**Experimental**", "**Not available**")
FEATURE_COLUMNS = ("Capability", "Status", "Evidence")
PROVIDER_COLUMNS = (
    "Provider (`provider:`)",
    "Agent",
    "Built-in routing",
    "Status",
    "Versions and sign-in",
)
# The test suite's stand-in for an agent: it runs a configured argv, and no
# operator routes a phase to it.
NOT_AN_AGENT = {"scripted"}
# How the README's absolute links name this repository's files on `main`.
REPOSITORY = "https://github.com/iceboundrock/AutoForge"
BLOB_MAIN = f"{REPOSITORY}/blob/main/"
CODE = re.compile(r"`([^`]+)`")
DOCTOR_AGENT_ROW = re.compile(r"agent '([^']+)' available \(([^)]*)\)")


def _text() -> str:
    return README.read_text(encoding="utf-8")


def _block(name: str) -> str:
    """What lies between ``<!-- name -->`` and ``<!-- /name -->``, which appear once."""
    text = _text()
    begin, end = f"<!-- {name} -->", f"<!-- /{name} -->"
    assert text.count(begin) == 1 and text.count(end) == 1, f"the {name} markers"
    return text.split(begin, 1)[1].split(end, 1)[0]


def _table(block: str, columns: tuple[str, ...]) -> list[list[str]]:
    """The rows of the one table in ``block``, header and separator checked."""
    header, separator, *rows = [line for line in block.splitlines() if line.startswith("|")]
    assert _cells(header) == list(columns)
    assert set(separator.replace("|", "")) == {"-"}
    assert rows, "the table is empty"
    rows_cells = [_cells(row) for row in rows]
    for cells in rows_cells:
        assert len(cells) == len(columns), cells
        assert all(cells), f"an empty cell in {cells[0]}"
    return rows_cells


def _features() -> list[list[str]]:
    return _table(_block("feature-matrix"), FEATURE_COLUMNS)


def _providers() -> dict[str, list[str]]:
    """The provider matrix, by the ``provider:`` value its first cell names."""
    rows: dict[str, list[str]] = {}
    for cells in _table(_block("provider-matrix"), PROVIDER_COLUMNS):
        names = CODE.findall(cells[0])
        assert len(names) == 1 and cells[0] == f"`{names[0]}`", cells[0]
        assert names[0] not in rows, f"two rows for {names[0]}"
        rows[names[0]] = cells
    return rows


def _section(text: str, heading: str) -> str:
    """The body of the ``## heading`` section of ``text``."""
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def _local(target: str) -> str | None:
    """An absolute link to this repository's ``main`` as the README-relative
    target it names; ``None`` for a link elsewhere."""
    if target.startswith(BLOB_MAIN):
        return target[len(BLOB_MAIN) :]
    if target.startswith(f"{REPOSITORY}#"):
        return target[len(REPOSITORY) :]
    return None


def _links_of(text: str) -> list[str]:
    return [m[1] for m in re.finditer(r"\[[^\]]*\]\(([^)\s]+)\)", text)]


def test_the_first_screen_says_what_autoforge_is_its_status_and_where_to_start():
    """Everything before the first ``##`` heading is what a reader sees
    first: the project, its status, and the way to a first run."""
    first = _text().split("\n## ", 1)[0]
    assert first.startswith("# AutoForge")
    assert "AutoForge is not another coding agent" in first
    assert "**Status:" in first and "not production-ready" in first
    assert "docs/getting-started.md" in _links_of(first)
    assert "#what-works-today" in _links_of(first)


def test_every_feature_row_has_one_status_and_an_evidence_link():
    rows = _features()
    capabilities = [cells[0] for cells in rows]
    assert len(set(capabilities)) == len(capabilities), "a capability listed twice"
    broken = []
    for capability, status, evidence in rows:
        assert status in STATUSES, f"{capability}: {status!r} is not one status of three"
        targets = _links_of(evidence)
        assert targets, f"{capability}: no evidence link"
        broken += [
            f"{capability}: {problem}"
            for target in targets
            if "://" not in target
            for problem in [_broken(README, target)]
            if problem
        ]
    assert broken == []
    assert {status for _, status, _ in rows} == set(STATUSES)


def test_a_status_is_claimed_only_in_the_matrices():
    """The matrices are where a status is decided. A status word in bold
    anywhere else on the page would be a second claim that can drift."""
    text = _text()
    outside = text
    for name in ("feature-matrix", "provider-matrix"):
        outside = outside.replace(_block(name), "")
    assert [status for status in STATUSES if status in outside] == []


def test_the_provider_matrix_takes_every_provider_with_its_built_in_routing():
    """A row per provider the configuration accepts, and each row's
    built-in routing exactly the profiles ``default_config()`` gives it."""
    rows = _providers()
    assert set(rows) == set(KNOWN_PROVIDERS) - NOT_AN_AGENT
    profiles = default_config().profiles
    for provider, (_, _, routing, status, _) in rows.items():
        assert status in STATUSES, f"{provider}: {status!r}"
        expected = {name for name, profile in profiles.items() if profile.provider == provider}
        if expected:
            assert set(CODE.findall(routing)) == expected, provider
        else:
            assert routing == "none", provider


def test_the_provider_matrix_agrees_with_doctors_agent_rows(tmp_path):
    """``doctor`` checks one agent CLI per command the reachable profiles
    name. With the built-in configuration, its rows are the README's: the
    same commands, each for the same profiles."""
    results = Doctor(cwd=str(tmp_path), runner=_runner_factory()).run_all()
    checked = {
        match[1]: set(match[2].split(", "))
        for result in results
        for match in [DOCTOR_AGENT_ROW.fullmatch(result.name)]
        if match
    }
    readme = {}
    for provider, (_, agent, routing, _, _) in _providers().items():
        if routing == "none":
            continue
        commands = CODE.findall(agent)
        assert len(commands) == 1, f"{provider}: the agent cell names its command once"
        readme[commands[0]] = set(CODE.findall(routing))
    assert checked == readme


def test_the_provider_matrix_links_compatibility_and_restates_no_version():
    """Floors, verified versions and sign-in live in the compatibility
    matrix, which records the evidence for each; the README links it."""
    for provider, cells in _providers().items():
        targets = _links_of(cells[-1])
        assert any(
            (README.parent / t.partition("#")[0]).resolve() == COMPATIBILITY for t in targets
        ), provider
        assert all(not _broken(README, t) for t in targets), provider
        assert not re.search(r"\d+\.\d+", " ".join(cells)), f"{provider} restates a version"


def test_a_providers_status_is_the_feature_matrix_status():
    """A provider that is not supported has a feature row naming it as
    ``provider: <name>``, with the same status."""
    features = {cells[0]: cells[1] for cells in _features()}
    for provider, cells in _providers().items():
        named = [
            status
            for capability, status in features.items()
            if f"`provider: {provider}`" in capability
        ]
        if cells[3] != "**Supported**":
            assert named, f"no feature row for {provider}"
        assert all(status == cells[3] for status in named), provider


def test_the_prerequisites_state_the_floors_doctor_enforces():
    prerequisites = _section(_text(), "Prerequisites")
    floors = {
        GH_MIN_VERSION: r"`gh` (\d+\.\d+\.\d+) or newer",
        OPENCODE_MIN_VERSION: r"\(`opencode`\) (\d+\.\d+\.\d+) or newer",
    }
    for floor, pattern in floors.items():
        stated = re.findall(pattern, prerequisites)
        assert stated == [".".join(map(str, floor))], pattern


def test_the_configuration_guides_built_in_profiles_are_default_config():
    """Configuration's table of the built-in profiles, which the README's
    provider matrix sends a reader to for models: provider, model and effort
    as ``default_config()`` has them, and each profile on the phase and
    review rounds the routing gives it."""
    text = CONFIGURATION.read_text(encoding="utf-8")
    block = text.split("The built-in defaults are:", 1)[1].split("\n### ", 1)[0]
    rows = _table(block, ("Profile", "Phase", "Provider", "Model", "Effort"))
    displayed = {"Claude Code": "claude", "OpenCode": "opencode"}
    cfg = default_config()
    listed = {}
    for profile_cell, phase_cell, provider, model, effort in rows:
        assert re.fullmatch(r"`\w+`", profile_cell) and phase_cell.startswith("`"), profile_cell
        name = profile_cell.strip("`")
        listed[name] = (displayed[provider], model.strip("`"), effort)
        phase = Phase[CODE.findall(phase_cell)[0]]
        if phase is Phase.REVIEW:
            rounds = [int(n) for n in re.findall(r"\b\d+\b", phase_cell)]
            assert rounds and {review_profile_name(r) for r in rounds} == {name}, phase_cell
        else:
            assert profile_for_phase(cfg, phase).name == name, phase_cell
    expected = {n: (p.provider, p.model, p.effort) for n, p in cfg.profiles.items()}
    assert listed == expected


def test_the_sections_pypi_shows_link_absolutely():
    """PyPI shows the README and resolves no relative link, so Prerequisites
    and Installation link absolutely (``docs/releasing.md``, "Names")."""
    text = _text()
    for heading in ("Prerequisites", "Installation"):
        relative = [t for t in _links_of(_section(text, heading)) if "://" not in t]
        assert relative == [], heading


def test_the_readme_and_the_indexs_links_resolve():
    """Every relative link of the README and of the documentation index,
    and every absolute one to this repository's ``main``, reaches a file and
    a heading that exist."""
    broken = []
    for source in (README, INDEX):
        for target in _links(source):
            if target.startswith("mailto:"):
                continue
            local = _local(target)
            if local is not None:
                broken += [f"{target}: {p}" for p in [_broken(README, local)] if p]
            elif "://" not in target:
                broken += [p for p in [_broken(source, target)] if p]
    assert broken == []


def test_every_link_into_the_readme_resolves():
    """Other documents link to the README's sections by anchor; a renamed
    heading would leave them pointing at the top of the page."""
    broken = []
    for source in _markdown_files():
        for target in _links(source):
            file_part = target.partition("#")[0]
            if "://" in target or not file_part:
                continue
            if (source.parent / file_part).resolve() == README:
                broken += [p for p in [_broken(source, target)] if p]
    assert broken == []
