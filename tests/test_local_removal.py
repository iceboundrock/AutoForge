"""LOCAL mode stays deleted: the structural half of ADR 0009 D12's S3 exit (#388).

S3 deleted LOCAL's runtime: its topology and ``WorkflowMode.LOCAL``, its
launch classes, its run contract and workspace walk, its candidate,
certificate and result models, its prompt templates and its configuration.
These tests hold the package to that by shape rather than by scenario. They
fail when a LOCAL topology, launch class or mode comes back, when a deleted
module can be imported again, or when a module of the package defines,
imports or refers to one of the deleted names. The half of the wheel and
the sdist is
``tests/test_packaging.py::test_no_artifact_carries_a_file_of_local_mode``.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path

import pytest

import autoforge
from autoforge import prompts, sandbox_policy, transitions
from autoforge.sandbox_policy import LaunchClass
from autoforge.transitions import WorkflowMode

SRC = Path(autoforge.__file__).parent

# The modules S3 deleted whole.
DELETED_MODULES = ("autoforge.run_contract", "autoforge.local_workspace")

# The names S3 deleted, each LOCAL's alone: no module of the package may
# define, import or refer to one again. ``git_transport``'s ``LOCAL_GIT_*``
# (git run on this host) and ``state``'s ``_LOCAL_FIELDS`` (the drop step of
# ADR 0009 D8) are not LOCAL mode's and are not listed.
DELETED_NAMES = frozenset(
    {
        # transitions.py
        "LOCAL_LEGAL_EDGES",
        "LOCAL_PHASES",
        "LOCAL_WRITE_PHASES",
        "stop_phases_for",
        # sandbox_policy.py
        "LOCAL_AGENT",
        "LOCAL_VALIDATION",
        "LOCAL_SANDBOX_KEY",
        # run_contract.py and local_workspace.py
        "LocalRunContract",
        "validate_local_run_contract",
        "LocalWorkspace",
        "WorkspaceSnapshot",
        "FeatureSpec",
        "read_feature_spec",
        "verify_feature_spec_unchanged",
        # evidence.py and certificate.py
        "LocalCandidate",
        "KIND_LOCAL",
        "METHOD_IN_PLACE",
        "local_certificate",
        "_local_binding_problem",
        # config.py, profiles.py, interaction.py
        "LocalConfig",
        "local_required_profiles",
        "LOCAL_BASE_PROFILES",
        "LocalBinding",
        # result_parser.py
        "LocalAnalyzeExecuteResult",
        "LocalReviewResult",
        "LocalFixResult",
        "LocalFindingResolution",
        "LOCAL_FIX_RESOLUTIONS",
        "LOCAL_UNSUPPORTED_PHASES",
        "_validate_local_phase",
        # prompts and the engine
        "LOCAL_COMMON_TEMPLATE",
        "LOCAL_PHASE_TEMPLATE",
        "MAX_LOCAL_PHASE_ATTEMPTS",
        "new_local_run",
        "_local_step_once",
        "local_contract",
        "local_workspace",
        "local_state_paths",
        "bind_local_state_dir",
        "is_local",
        # doctor.py
        "run_local_doctor",
        "check_local_state_dir",
    }
)


def _package_modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_modules(tree: ast.AST, module: str) -> set[str]:
    """Every module ``tree`` imports, absolute, relative ones resolved against ``module``."""
    package = module.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            found.add(target)
            # ``from . import run_contract`` imports the module by its alias.
            found |= {f"{target}.{alias.name}" for alias in node.names}
    return found


def _names(tree: ast.AST) -> set[str]:
    """Every identifier ``tree`` defines, imports or refers to."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.alias):
            found.add(node.asname or node.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.arg):
            found.add(node.arg)
    return found


def test_the_local_topology_is_gone():
    for name in ("LOCAL_LEGAL_EDGES", "LOCAL_PHASES", "LOCAL_WRITE_PHASES", "stop_phases_for"):
        assert not hasattr(transitions, name), name


def test_workflow_mode_has_only_remote_and_refuses_local():
    assert [m.name for m in WorkflowMode] == ["REMOTE"]
    assert not hasattr(WorkflowMode, "LOCAL")
    with pytest.raises(ValueError):
        WorkflowMode("LOCAL")


def test_no_launch_class_is_local():
    assert {m.name for m in LaunchClass} == {"REMOTE_AGENT", "EXPORT_COMMAND"}
    assert not [m for m in LaunchClass if "LOCAL" in m.name.upper() or "LOCAL" in m.value.upper()]
    assert not hasattr(sandbox_policy, "LOCAL_SANDBOX_KEY")


@pytest.mark.parametrize("module", DELETED_MODULES)
def test_a_deleted_module_cannot_be_imported(module):
    assert importlib.util.find_spec(module) is None
    assert not (SRC / f"{module.rsplit('.', 1)[1]}.py").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_no_module_imports_a_deleted_module():
    offenders = []
    for path in _package_modules():
        relative = path.relative_to(SRC.parent).with_suffix("")
        module = ".".join(relative.parts).removesuffix(".__init__")
        if path.name == "__init__.py":
            module = f"{module}.__init__"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hit = sorted(_imported_modules(tree, module) & set(DELETED_MODULES))
        if hit:
            offenders.append(f"{path.relative_to(SRC)}: {hit}")
    assert offenders == []


def test_no_module_defines_imports_or_refers_to_a_deleted_name():
    offenders = []
    for path in _package_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hit = sorted(_names(tree) & DELETED_NAMES)
        if hit:
            offenders.append(f"{path.relative_to(SRC)}: {hit}")
    assert offenders == []


def test_no_module_refers_to_workflow_mode_local():
    """``WorkflowMode.LOCAL`` spelled out, which the enum check alone would let a
    ``getattr`` fallback hide."""
    offenders = []
    for path in _package_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "LOCAL"
                and ast.unparse(node.value).endswith("WorkflowMode")
            ):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []


def test_no_local_prompt_template_is_shipped_or_registered():
    assert sorted(p.name for p in prompts.prompts_dir().glob("local_*.md")) == []
    assert [name for name in prompts.TEMPLATE_FILES if name.startswith("local_")] == []
    assert not hasattr(prompts, "LOCAL_COMMON_TEMPLATE")


def test_the_scans_see_what_they_look_for():
    """The scans are not vacuous: each finds its target in a module that has one."""
    tree = ast.parse(
        "from .run_contract import LocalRunContract\n"
        "from . import local_workspace\n"
        "import autoforge.run_contract\n"
        "x = WorkflowMode.LOCAL if LaunchClass.LOCAL_AGENT else stop_phases_for()\n"
    )
    assert _imported_modules(tree, "autoforge.engine") >= set(DELETED_MODULES)
    assert {"LocalRunContract", "LOCAL_AGENT", "stop_phases_for"} <= _names(tree) & DELETED_NAMES
    nested = ast.parse("from ..run_contract import x\n")
    assert "autoforge.run_contract" in _imported_modules(nested, "autoforge.sub.module")
