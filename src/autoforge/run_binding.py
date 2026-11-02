"""The EPIC a new standalone run records: the issue's native parent, else ``--epic`` (#293).

``autoforge run`` records an EPIC in the run's state (``epic_url``) when it
creates the run, and every later phase acts on that record: ``UPDATE_EPIC``
posts its progress comment and writes its roadmap section on it, and takes
the next issue only among its native sub-issues (#287). This module decides
which issue that is, from GitHub's native parent link alone:

* An issue with a direct native parent binds to it: the issue
  ``parent_issue_url`` names in ``GET .../issues/<n>``. A ``--epic`` naming
  another issue is shown beside it and not used. A nested issue binds to
  its direct parent, never to an ancestor.
* An issue GitHub returns with no parent binds to ``--epic``; with neither,
  the run is refused.
* A parent written in the issue's body, a task list, a roadmap or a title
  never counts: none of them is read.

**Fail closed.** The only reads are
:meth:`~autoforge.github.GitHubClient.get_issue_node` of the issue and of
the EPIC it binds to (:data:`BINDING_READS`). A read that fails, times out,
is truncated, is refused or answers with another object stays a
:class:`~autoforge.errors.GitHubError` (transient ones a
:class:`~autoforge.errors.GitHubUnavailableError`), with this binding's
context, and never falls back to ``--epic``. Only an issue object GitHub
returned whole is a confirmed absence of a parent: one that carries the
native sub-issue summaries an API without sub-issues lacks (the client
refuses an answer without them) and no ``parent_issue_url``, which GitHub
leaves out of an issue that has no parent (#179's observation,
``tests/fixtures/github_graph/README.md``). A binding the facts refuse is
an :class:`~autoforge.errors.EpicBindingError`: no parent and no
``--epic``, a parent in another repository (AutoForge runs within one
repository), ``--epic`` naming the issue itself or another repository, an
issue or EPIC that is not open.

Nothing here writes, locks or reads a file. ``run`` binds once, under the
controller lock, after its existing-run and scheduler-ownership guards and
immediately before the run's first save: the state records what GitHub
answered then, with no earlier answer that could have gone stale, and a
refusal leaves no state, quarantine or run log. Its dry run binds through
the same function, without the lock, and persists nothing. A recorded run
is never bound again: ``resume``, ``step``, ``unblock`` and ``status`` read
``epic_url`` from the state, never the issue's current parent (rebinding an
existing run is #435), and a scheduler worker records the EPIC whose
snapshot admitted the issue as its direct child (ADR 0008 D3.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .errors import (
    EpicBindingError,
    ExecutionError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
)
from .github import IssueNode
from .validation import GitHubIssueRef, parse_issue_url, validate_epic_and_issue


class BindingReader(Protocol):
    """The one GitHub read a binding makes."""

    def get_issue_node(self, url: str) -> IssueNode: ...


# Every client method this module may call (a structural test holds it to this).
BINDING_READS = frozenset({"get_issue_node"})


class EpicSource(Enum):
    """Where a new run's EPIC came from."""

    NATIVE_PARENT = "native-parent"
    EPIC_OPTION = "epic-option"


@dataclass(frozen=True)
class EpicBinding:
    """A new run's issue and EPIC, and what they were decided from.

    ``issue`` is the issue as ``--issue`` spelled it; ``epic`` the EPIC the
    run records, in the issue's repository spelling (GitHub compares owner
    and repository case-insensitively, so it is the same issue whichever
    spelling GitHub answered with). ``supplied`` is ``--epic`` as given
    (None without one) and ``native_parent`` the parent GitHub names for
    the issue (None when it names none).
    """

    issue: GitHubIssueRef
    epic: GitHubIssueRef
    source: EpicSource
    supplied: GitHubIssueRef | None
    native_parent: GitHubIssueRef | None

    @property
    def issue_url(self) -> str:
        return self.issue.canonical

    @property
    def epic_url(self) -> str:
        return self.epic.canonical

    @property
    def supplied_differs(self) -> bool:
        """``--epic`` was given and names another issue than the one used."""
        return self.supplied is not None and not self.supplied.same_target(self.epic)

    def lines(self) -> list[str]:
        """What ``run`` and its dry run print: ``--epic``, the native parent, the EPIC used."""
        supplied = _shown(self.supplied) if self.supplied is not None else "(not given)"
        parent = (
            _shown(self.native_parent)
            if self.native_parent is not None
            else f"none (GitHub names no parent for #{self.issue.number})"
        )
        if self.source is EpicSource.EPIC_OPTION:
            why = "--epic, since the issue has no native parent"
        elif self.supplied is None:
            why = "the issue's native parent"
        elif self.supplied_differs:
            why = (
                f"the issue's native parent; --epic names #{self.supplied.number}, "
                "which is not used"
            )
        else:
            why = "the issue's native parent, which --epic names too"
        return [
            f"EPIC binding for issue #{self.issue.number}:",
            f"  --epic:        {supplied}",
            f"  native parent: {parent}",
            f"  used:          {_shown(self.epic)} ({why})",
        ]

    def record(self) -> dict[str, object]:
        """The run log's record of the binding: URLs and the source, nothing else."""
        return {
            "epic_source": self.source.value,
            "epic_url": self.epic_url,
            "issue_url": self.issue_url,
            "supplied_epic_url": self.supplied.canonical if self.supplied is not None else None,
            "native_parent_url": (
                self.native_parent.canonical if self.native_parent is not None else None
            ),
            "supplied_epic_differs": self.supplied_differs,
        }


def _shown(ref: GitHubIssueRef) -> str:
    return f"#{ref.number} {ref.canonical}"


def _read(reader: BindingReader, ref: GitHubIssueRef, role: str) -> IssueNode:
    """``ref``'s node, or the read's failure with the binding's context; never a fallback.

    The failure keeps its class where it matters to a caller: transient
    (:class:`GitHubUnavailableError`), not found (:class:`GitHubNotFoundError`)
    or conclusive (:class:`GitHubError`). A ``gh`` that could not be run is
    a conclusive failure too.
    """
    try:
        return reader.get_issue_node(ref.canonical)
    except (GitHubError, ExecutionError) as exc:
        kind: type[GitHubError]
        if isinstance(exc, GitHubUnavailableError):
            kind = GitHubUnavailableError
        elif isinstance(exc, GitHubNotFoundError):
            kind = GitHubNotFoundError
        else:
            kind = GitHubError
        raise kind(
            f"cannot bind the run's EPIC: reading {role} {ref.canonical} failed ({exc}). "
            "Nothing was written, and a failed read never falls back to --epic"
        ) from exc


def bind_run_epic(reader: BindingReader, issue_url: str, epic_url: str | None) -> EpicBinding:
    """The EPIC a new run of ``issue_url`` records (see the module docstring).

    ``epic_url`` is ``--epic`` (None when not given). The local checks come
    first, and a URL they refuse is never read: ``--issue`` and ``--epic``
    must be issue URLs of one repository, and ``--epic`` must not name the
    issue itself. Then the issue's node is read; it must be open. Its
    native parent, else ``--epic``, is the EPIC, whose node is read too: it
    must be open, and in the issue's repository.
    """
    issue = parse_issue_url(issue_url)
    supplied: GitHubIssueRef | None = None
    if epic_url is not None:
        supplied, issue = validate_epic_and_issue(epic_url, issue_url)
        if supplied.same_target(issue):
            raise EpicBindingError(
                f"--epic {supplied.canonical} is the issue itself; a run's EPIC is another issue"
            )
    node = _read(reader, issue, "the issue")
    if not node.is_open:
        raise EpicBindingError(
            f"issue {issue.canonical} is {node.state} on GitHub; only an open issue starts a run"
        )
    parent = node.parent
    if parent is not None:
        if parent.same_target(issue):
            raise GitHubError(
                f"cannot bind the run's EPIC: GitHub names issue {issue.canonical} as its own "
                "parent, which is not a parent link"
            )
        if not parent.same_repository(issue):
            raise EpicBindingError(
                f"issue {issue.canonical} has its native parent {parent.canonical} in another "
                f"repository than {issue.repository}; AutoForge runs within one repository, so "
                "the run is refused (--epic does not override a native parent)"
            )
        # The same issue in the spelling the run's other URLs use.
        epic = GitHubIssueRef(owner=issue.owner, repo=issue.repo, number=parent.number)
        source = EpicSource.NATIVE_PARENT
        role = "the issue's native parent"
    elif supplied is not None:
        epic = supplied
        source = EpicSource.EPIC_OPTION
        role = "the EPIC --epic names"
    else:
        raise EpicBindingError(
            f"issue {issue.canonical} has no native parent on GitHub and no --epic was given: "
            "attach the issue to its EPIC as a sub-issue on GitHub, or name the EPIC with "
            "--epic. A parent named in the issue's text is never read"
        )
    epic_node = _read(reader, epic, role)
    if not epic_node.is_open:
        raise EpicBindingError(
            f"{role} {epic.canonical} is {epic_node.state} on GitHub; a run records only an open "
            "EPIC (reopen it, or "
            + (
                "attach the issue to an open EPIC on GitHub)"
                if source is EpicSource.NATIVE_PARENT
                else "name an open EPIC with --epic)"
            )
        )
    return EpicBinding(
        issue=issue,
        epic=epic,
        source=source,
        supplied=supplied,
        native_parent=parent,
    )
