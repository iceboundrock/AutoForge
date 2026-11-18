"""EPIC #354's deterministic recovery MVP, measured offline: the before/after matrix (#375).

``docs/recovery-mvp-evaluation.md`` section 4 is the evaluation's matrix: one
row per case, each a real engine run on ``FakeGitHub`` that stops, or
recovers by itself, with its gold resolution. This module computes every row
and fails when the document's row says otherwise, so the matrix is
reproduced by running it::

    uv run pytest tests/test_recovery_evaluation.py

A row that differs fails with the row computed, which is what the
document's row must become once the difference is understood.

Two arms are measured on the same stop. *Before* is what an operator had
without #369 and #374: the phase a later ``status`` prints, and the text of
the saved ``block_reason`` or of the error the stopping command printed.
*After* is what ``main`` gives them on top of it: the ``stop:`` line of the
stopping command (ADR 0011) and the recovery package of a later
``status --json`` (#374).

Each case's gold resolution is written here by hand, from ADR 0012 D2 and
D3, never from the package: what the operator does outside AutoForge, the
command that leaves the stop, the phase the controller must read back after
it, and the phase the next workflow step must save. It is then performed.
The package's displayed steps run as an operator would type them,
placeholders filled, each command's exit status checked and the state the
controller saved read back. A step with no command is performed as its text
reads, literally (:data:`READINGS`), never as the gold's act, and what it
did is checked against what the gold keeps. A saved plan is checked against
the journal and against what GitHub and the origin hold. No agent's or
model's claim is evidence of anything here.

Nothing launches a model, and nothing is written outside ``tmp_path``. Every
``status`` the harness runs is held to launching no agent, sending nothing
to GitHub and leaving the state directory as it was, byte for byte.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import json
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from autoforge import cli
from autoforge.claims import IMPLEMENTATION, scan
from autoforge.effects import AnalyzeContext, EffectKind, EffectRecord, FixContext, Stage
from autoforge.engine import ControllerEngine
from autoforge.errors import ExecutionTimeoutError, GitHubUnavailableError, StateError
from autoforge.providers import ProviderRegistry, ScriptedProvider
from autoforge.state import load_state
from autoforge.stop_classification import StopClassification, classify_exception
from autoforge.transitions import Phase
from tests.conftest import (
    EPIC,
    ISSUE,
    PR,
    SHA_A,
    FakeGitHub,
    block,
    connect_origin,
    implement,
    make_engine,
    state_tree,
    unloadable_state,
)
from tests.test_auto_recovery import IDLE, _analyzing
from tests.test_engine import (
    ESCAPE_BODY,
    _analyzed,
    _loop_agent,
    _no_stagnation,
    _one_finding_per_round,
    _power_loss_at,
    review_payload,
)
from tests.test_recovery_package import (
    _ambiguous,
    _checks_refused,
    _false_identity_conflict,
    _fork_headed,
    _k9_blocked,
    _k10_blocked,
    _k12_blocked,
    _two_round_comments,
)
from tests.test_stop_classification import _stopping

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "recovery-mvp-evaluation.md"
MATRIX = "## 4. The matrix"
SUMMARY = "## 5. What the matrix shows"
SAFETY = "## 6. The safety regressions"

#: A value the operator supplies: an unblock's reason, a comment's URL, the PR they keep.
VALUE = "<value>"
#: How the package and the saved reasons spell such a value.
_PLACEHOLDERS = ("...", "<why>", "<comment URL>", "<PR URL>")
#: What the harness types for one: a reason is text for a human, never evidence.
_FILLED = "recovery evaluation (#375)"
#: Not a measure for this case: nothing stopped, or nothing applies.
NONE = "—"


# -- the cases -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Gold:
    """A case's gold resolution, written from ADR 0012 D2 and D3.

    ``stop`` is how the stopping command ended (``outcome/code``, ADR 0011
    D1 and D6): ``none`` when nothing stopped, ``—`` for a process that died
    and printed nothing. ``command`` is the resolving
    command's words with ``{action}`` for the stop's saved action id and
    ``<value>`` for what the operator supplies, ``""`` when the controller
    resolves the case itself. ``target`` is the phase the controller must
    read back right after that command. ``then``, for an ``unblock``, is
    what the next workflow step (``resume --max-steps 1``) must save, the
    phase and any stop code; a ``resume`` or ``run`` is that step itself,
    and ``target`` is what it must save. ``act`` is what the operator does
    first, outside AutoForge.
    """

    stop: str
    command: str
    target: str
    then: str = ""
    act: str = ""

    @property
    def unblocks(self) -> bool:
        return self.command.startswith("unblock")

    @property
    def next_saves(self) -> str:
        """What the workflow step after the stop must save: after an unblock, or itself."""
        return self.then if self.unblocks else self.target


@dataclass
class Stopped:
    """A case after its stop, as the operator finds it.

    ``line`` is what the stopping command's ``stop:`` line prints (``None``
    for no line: a process that died, or a run that did not stop); ``said``
    the text that command printed or the state saved for a human; ``act``
    performs the gold's act, and only the gold's; ``then`` is the agent that
    works once the run goes on; ``steps`` holds the outcomes of the steps the
    controller ran with no operator command, for a case that recovers by
    itself. ``raised`` is the value the operator gives a bound a text tells
    them to raise, ``chosen`` the PR the operator decides is the issue's
    where a text leaves that to them, and ``others`` the comments the operator
    finds to be another controller's run's record when a text has them check
    who posted each: what a :data:`READINGS` act needs to perform a text,
    never a choice the harness makes for the package.
    """

    eng: ControllerEngine
    line: StopClassification | None
    said: str
    act: Callable[[], None] | None = None
    then: Callable | list | None = None
    steps: list = field(default_factory=list)
    raised: dict[str, int] = field(default_factory=dict)
    chosen: str = ""
    others: tuple[str, ...] = ()


def _own_state_dir(tmp: Path) -> Path:
    """``tmp``, held to being a case's own state directory: one not made yet, in a
    directory that is no checkout yet. ``make_engine`` makes the run's checkout in the
    state directory's parent, the agent worktree goes in that checkout's git
    directory, and ``attach_origin`` puts the bare origin next to it. A test's
    ``tmp_path`` itself as the state directory puts all three in the session's base
    directory, shared by every test that did the same, so one test's branch and
    pushes reach the next and an outcome depends on the order the tests ran in
    (PR #511 R3-F1). ``tmp_state_dir`` and ``mktemp(...) / ".autoforge"`` are such
    directories."""
    assert not tmp.exists(), f"{tmp} exists: a case needs a state directory of its own"
    assert not (tmp.parent / ".git").exists(), f"{tmp.parent} already is another case's checkout"
    return tmp


def _blocked(build: Callable[[Path], ControllerEngine], tmp: Path) -> Stopped:
    """A scenario whose last step stopped in BLOCKED or FAILED: that step's stop line,
    which the CLI prints, and the reason the stop saved."""
    tmp = _own_state_dir(tmp)
    outcomes: list = []
    real = ControllerEngine.step

    def step(self, *args, **kwargs):
        outcomes.append(real(self, *args, **kwargs))
        return outcomes[-1]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ControllerEngine, "step", step)
        eng = build(tmp)
    return Stopped(eng, outcomes[-1].stop, load_state(eng.paths.state_file).block_reason)


def _k10(tmp: Path) -> Stopped:
    """#280 -> #23's shape (ADR 0011 D8): FIX asks the operator to comment on two other
    issues, and the push of its candidate is held back until both are verified."""
    return _blocked(_k10_blocked, tmp)


def _k9(tmp: Path) -> Stopped:
    """K9: FIX resolves a finding with an acceptance comment awaiting the operator's approval."""
    return _blocked(_k9_blocked, tmp)


def _k12(tmp: Path) -> Stopped:
    """K12 under ``safety.pr_body_updates: approve``: a description update awaiting approval."""
    return _blocked(_k12_blocked, tmp)


def _checks_291(tmp: Path) -> Stopped:
    """#291, regression negative: the credential may not read the PR's check runs at the gate."""
    stopped = _blocked(_checks_refused, tmp)

    def grant() -> None:  # a credential that may read check runs
        stopped.eng.github.checks_error = None

    stopped.act = grant
    return stopped


def _conflict_314(tmp: Path) -> Stopped:
    """PR #314's stop, saved by a controller before #327 that compared the body ``gh``
    printed: a false effect conflict on the run's own PR, read by today's controller."""
    return _blocked(_false_identity_conflict, tmp)


def _no_stop_327(tmp: Path) -> Stopped:
    """#327 / PR #330, regression negative: a body ``gh`` sanitizes is judged on the exact
    read, so ANALYZE_EXECUTE goes on to REVIEW and nothing stops."""
    gh = FakeGitHub()
    gh.gh_sanitizes = True
    eng = make_engine(tmp, lambda req: implement(req, pr_body=ESCAPE_BODY), github=gh, origin=True)
    out = _analyzed(eng)
    assert out.stop is None, out.message
    return Stopped(eng, None, "", steps=[out])


#: The second PR of ``_ambiguous``: the one the operator decides is not the issue's.
STALE = "https://github.com/owner/repo/pull/43"


def _ambiguous_prs(tmp: Path) -> Stopped:
    """Two open PRs carry the issue's implementation marker and the run recorded neither:
    which one is the issue's, and that this run takes it over, is the operator's decision
    (ADR 0012 D3's adoption row, K11). The controller never chooses. Here the operator
    decides ``PR`` is the issue's: the PR the gold keeps open, marked and taken over."""
    stopped = _blocked(_ambiguous, tmp)
    stopped.chosen = PR

    def keep_the_issues_close_the_stale_one() -> None:
        stopped.eng.github.prs[STALE].state = "CLOSED"

    stopped.act = keep_the_issues_close_the_stale_one
    return stopped


def _bound(tmp: Path, key: str, value: int, raised: int) -> Stopped:
    gh = FakeGitHub()
    eng = make_engine(tmp, _loop_agent(gh, _one_finding_per_round), github=gh, origin=True)
    setattr(eng.config.workflow, key, value)
    _no_stagnation(eng.config)
    eng._save()
    outcomes = eng.run(max_steps=50)
    assert outcomes[-1].next_phase == "BLOCKED", outcomes[-1].message
    stopped = Stopped(
        eng,
        outcomes[-1].stop,
        eng.state.block_reason,
        then=eng.provider._handler,
        raised={key: raised},
    )

    def raise_the_bound() -> None:  # the configuration every later command loads
        setattr(eng.config.workflow, key, raised)

    stopped.act = raise_the_bound
    return stopped


def _review_cap(tmp: Path) -> Stopped:
    """An exhausted review loop: round 2 still has findings and the cap is 2."""
    return _bound(tmp, "max_review_rounds", 2, 3)


def _step_budget(tmp: Path) -> Stopped:
    """The run's step budget used up in a review loop."""
    return _bound(tmp, "max_total_steps", 2, 10)


def _no_code(tmp: Path) -> Stopped:
    """The review cap again, saved with no code: how every stop saved before protocol 16
    loads (ADR 0011 D5 as amended by #374). The code is never derived from the text."""
    stopped = _review_cap(tmp)
    eng = stopped.eng
    eng.state.stop_code = ""
    eng._save()
    return stopped


def _agent_stop(status: str) -> Callable[[Path], Stopped]:
    """The implementation agent reports ``status`` (``blocked`` or ``failure``)."""

    def stopping(tmp: Path) -> ControllerEngine:
        message = "the issue does not say which of the two formats to keep"
        eng = make_engine(
            tmp, [block(_stopping(status, message))], github=FakeGitHub(), origin=True
        )
        eng.state.phase = Phase.ANALYZE_EXECUTE
        eng.step()
        return eng

    def build(tmp: Path) -> Stopped:
        stopped = _blocked(stopping, tmp)
        stopped.then = implement  # once the operator has answered on the issue
        return stopped

    return build


def _timed_out_twice(tmp: Path) -> Stopped:
    """An agent idle past its limit twice in one step: #434 relaunches it once, then
    leaves the error, the phase unchanged, for 'resume'."""
    eng, _ = _analyzing(tmp, FakeGitHub(), IDLE, IDLE, implement)
    with pytest.raises(ExecutionTimeoutError) as caught:
        eng.run(max_steps=5)
    return Stopped(eng, classify_exception(caught.value), str(caught.value), then=implement)


def _timed_out_once(tmp: Path) -> Stopped:
    """#434's one class (ADR 0012 D11): an idle timeout relaunched once by the running
    command, which goes on to REVIEW with no operator command."""
    eng, _ = _analyzing(tmp, FakeGitHub(), IDLE, implement)
    outcomes = eng.run(max_steps=2)
    assert all(o.stop is None for o in outcomes)
    return Stopped(eng, None, "", steps=outcomes)


def _unavailable(tmp: Path) -> Stopped:
    """GitHub answers 502 to the PR's creation after the push landed: the phase is
    unchanged and the record stays attempted, for 'resume' to reconcile by its identity."""
    gh = FakeGitHub()
    eng = make_engine(tmp, implement, github=gh, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    gh.write_failures.append(("create_pull_request", GitHubUnavailableError("gh: HTTP 502"), False))
    with pytest.raises(GitHubUnavailableError) as caught:
        eng.step()
    return Stopped(eng, classify_exception(caught.value), str(caught.value))


def _power_loss(tmp: Path) -> Stopped:
    """The process dies after the push landed and before its observation was saved: the
    record's outcome is unknown, and nothing printed anything."""
    gh = FakeGitHub()
    eng = make_engine(tmp, implement, github=gh, origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    _power_loss_at(eng, EffectKind.PUSH, Stage.OBSERVED, saved=False)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    return Stopped(eng, None, "")


def _local(tmp: Path) -> Stopped:
    """A BLOCKED state of the removed LOCAL mode (ADR 0009): every command's loader
    refuses it, and it gains no unblock edge by implication."""
    eng = make_engine(tmp, [], github=FakeGitHub(), origin=True)
    eng.state.phase = Phase.BLOCKED
    eng.state.block_reason = "the agent asked for a decision"
    eng._save()
    raw, _ = unloadable_state("local-mode", eng.paths.state_file.read_bytes())
    eng.paths.state_file.write_bytes(raw)
    with pytest.raises(StateError) as caught:
        load_state(eng.paths.state_file)
    return Stopped(eng, classify_exception(caught.value), str(caught.value))


@dataclass(frozen=True)
class Case:
    name: str
    build: Callable[[Path], Stopped]
    gold: Gold


_PLAIN = f"unblock --reason {VALUE}"
_NEW_RUN = f"run --force --epic {EPIC} --issue {ISSUE}"

CASES = (
    Case(
        "k10-operator-actions",
        _k10,
        Gold("blocked/operator-actions", _PLAIN, "FIX", "REVIEW", "post each payload on its issue"),
    ),
    Case(
        "k9-acceptance-action",
        _k9,
        Gold(
            "blocked/acceptance-action",
            f"unblock --approve-action {{action}} --reason {VALUE}",
            "FIX",
            "REVIEW",
        ),
    ),
    Case(
        "k12-pr-body-update",
        _k12,
        Gold(
            "blocked/acceptance-action",
            f"unblock --approve-action {{action}} --reason {VALUE}",
            "FIX",
            "REVIEW",
        ),
    ),
    Case(
        "checks-refused-291",
        _checks_291,
        Gold(
            "blocked/github-checks-refused",
            _PLAIN,
            "READY_FOR_MERGE",
            "READY_FOR_MERGE",
            "a credential that may read check runs",
        ),
    ),
    Case(
        "no-stop-327",
        _no_stop_327,
        Gold("none", "", "REVIEW"),
    ),
    Case(
        "false-conflict-314",
        _conflict_314,
        Gold("blocked/effect-conflict", _PLAIN, "ANALYZE_EXECUTE", "REVIEW"),
    ),
    Case(
        "ambiguous-prs",
        _ambiguous_prs,
        Gold(
            "blocked/claim-ambiguous",
            f"unblock --take-over {VALUE} --reason {VALUE}",
            "ANALYZE_EXECUTE",
            "REVIEW",
            "keep the issue's PR, close the other",
        ),
    ),
    Case(
        "review-round-cap",
        _review_cap,
        Gold(
            "blocked/review-round-cap", _PLAIN, "FIX", "REVIEW", "raise workflow.max_review_rounds"
        ),
    ),
    Case(
        "step-budget",
        _step_budget,
        Gold("blocked/step-budget", _PLAIN, "REVIEW", "FIX", "raise workflow.max_total_steps"),
    ),
    Case(
        "no-saved-code",
        _no_code,
        Gold(
            "blocked/review-round-cap", _PLAIN, "FIX", "REVIEW", "raise workflow.max_review_rounds"
        ),
    ),
    Case(
        "agent-blocked",
        _agent_stop("blocked"),
        Gold(
            "blocked/agent-blocked",
            _PLAIN,
            "ANALYZE_EXECUTE",
            "REVIEW",
            "answer the agent's question",
        ),
    ),
    Case(
        "agent-failure",
        _agent_stop("failure"),
        Gold("failed/agent-failure", _NEW_RUN, "ANALYZE_EXECUTE"),
    ),
    Case(
        "idle-timeout-relaunched",
        _timed_out_once,
        Gold("none", "", "REVIEW"),
    ),
    Case(
        "idle-timeout-twice",
        _timed_out_twice,
        Gold("resumable_error/execution-timeout", "resume", "REVIEW"),
    ),
    Case(
        "github-unavailable",
        _unavailable,
        Gold("resumable_error/github-unavailable", "resume", "REVIEW"),
    ),
    Case(
        "power-loss-after-push",
        _power_loss,
        Gold(NONE, "resume", "REVIEW"),
    ),
    Case(
        "local-blocked",
        _local,
        Gold("refused/state-invalid", _NEW_RUN, "ANALYZE_EXECUTE"),
    ),
)


# -- the operator's commands, each in a later process ------------------------------------------


class _NoAgent(ScriptedProvider):
    """The provider of a command that must launch nothing: ``status``, ``unblock``."""

    def execute(self, req):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError(f"a command that launches nothing launched {req.profile.name}")


@pytest.fixture
def operator(monkeypatch, capsys):
    """Run the CLI as the operator would, in a later process.

    Each command's engine loads the state from disk afresh, with the case's
    configuration (as the operator last changed it), its FakeGitHub and its
    bare origin; ``agent`` is what an agent the command launches does, and
    without one any launch fails the case.
    """

    def run(eng: ControllerEngine, *argv: str, agent=None) -> tuple[int, str, str]:
        monkeypatch.chdir(eng.workdir)
        monkeypatch.setattr(cli, "_load_cfg", lambda args: copy.deepcopy(eng.config))
        provider = _NoAgent(None) if agent is None else ScriptedProvider(agent)
        real = ControllerEngine.__init__

        def later(self, *a, **k):
            k.setdefault("github", eng.github)
            k.setdefault("workdir", eng.workdir)
            k.setdefault(
                "providers", ProviderRegistry(overrides={"claude": provider, "opencode": provider})
            )
            real(self, *a, **k)
            if getattr(eng, "origin", None) is not None:
                connect_origin(self, eng.origin)

        monkeypatch.setattr(ControllerEngine, "__init__", later)
        try:
            rc = cli.main(["--state-dir", str(eng.paths.state_dir), *argv])
        finally:
            monkeypatch.setattr(ControllerEngine, "__init__", real)
        captured = capsys.readouterr()
        return rc, captured.out, captured.err

    return run


# -- reading a command, and a text that spells one ---------------------------------------------


def _words(command: str) -> list[str]:
    """A displayed command's words without the program and this run's global flags,
    each value the operator supplies as ``<value>``; ``[]`` for a step with no command."""
    if not command:
        return []
    for placeholder in ("<PR URL>", "<comment URL>"):  # one word each, as the operator types it
        command = command.replace(placeholder, shlex.quote(placeholder))
    words = shlex.split(command)
    assert words[0] == "autoforge", command
    out: list[str] = []
    rest = iter(words[1:])
    for word in rest:
        if not out and word in ("--state-dir", "--config"):
            next(rest)
            continue
        out.append(VALUE if word in _PLACEHOLDERS else word)
    return out


def _spelled(text: str, command: list[str]) -> bool:
    """Whether ``text`` spells ``command`` whole: every word in order, values aside.

    A proxy for "the text tells the operator what to type", read by this
    evaluation only: nothing that decides anything reads a reason's text.
    """
    for placeholder in ("'<why>'", '"<why>"', '"..."', *_PLACEHOLDERS):
        text = text.replace(placeholder, VALUE)
    text = re.sub(r"\bautoforge\s+((--state-dir|--config)\s+\S+\s+)*", "", text)
    text = " ".join(text.split())
    return re.search(rf"(?<![\w-]){re.escape(' '.join(command))}(?![\w-])", text) is not None


def _fill(words: list[str], stopped: Stopped) -> list[str]:
    """A command as the operator types it: the PR they keep after ``--take-over``, and
    a reason, which is text for a human, for every other value."""
    out = []
    for i, word in enumerate(words):
        if word == VALUE and i and words[i - 1] == "--take-over":
            assert stopped.chosen, "a take-over of a PR the case has the operator choose none of"
            word = stopped.chosen
        out.append(_FILLED if word == VALUE else word)
    return out


def _records(eng: ControllerEngine) -> list[EffectRecord]:
    return [EffectRecord.from_dict(r) for r in load_state(eng.paths.state_file).effect_records]


def _gold_words(gold: Gold, records: list[EffectRecord]) -> list[str]:
    actions = [r.action_id for r in records if r.action_id]
    command = gold.command.format(action=actions[0] if actions else "")
    return command.split()


def _github_writes(gh: FakeGitHub) -> tuple:
    """Everything a command could have sent to the fake GitHub."""
    return (
        list(gh.effect_writes),
        list(gh.merges),
        list(gh.closed_prs),
        list(gh.reopened_prs),
        list(gh.deleted_branches),
        list(gh.disabled_auto),
        list(gh.edited_issues),
        sorted((url, len(comments)) for url, comments in gh.comments.items()),
        sorted((url, pr.state, pr.body, pr.head_sha) for url, pr in gh.prs.items()),
    )


def _status(operator, eng: ControllerEngine, *argv: str) -> tuple[int, str, str]:
    """A ``status`` the harness runs: it launches nothing (the provider fails if it is
    called), sends nothing to GitHub and leaves the state directory as it was."""
    tree, writes = state_tree(eng.paths.state_dir), _github_writes(eng.github)
    rc, out, err = operator(eng, "status", *argv)
    assert state_tree(eng.paths.state_dir) == tree, "status wrote to the state directory"
    assert _github_writes(eng.github) == writes, "status sent something to GitHub"
    return rc, out, err


# -- what a step with no command tells the operator to do, read literally ------------------


def _marked(eng: ControllerEngine) -> dict[str, str | None]:
    """The open PRs carrying the issue's implementation marker, each with the run its
    marker names (``None`` for no owner), as GitHub holds them now."""
    found = {}
    for url, pr in eng.github.prs.items():
        claims = scan(IMPLEMENTATION, pr.body or "").claims if pr.is_open else ()
        if claims:
            found[url] = claims[0].owner_run_id
    return found


def _close(eng: ControllerEngine, url: str) -> None:
    """The operator closes a PR on GitHub, outside AutoForge."""
    eng.github.prs[url].state = "CLOSED"


def _nothing(stopped: Stopped) -> None:
    """A text that has the operator read or inspect: nothing the case holds changes."""


def _answer(stopped: Stopped) -> None:
    """'resolve what it asks for': the operator answers on the issue, which the case's
    agent, launched again, does not read; nothing the case holds changes."""


def _grant(stopped: Stopped) -> None:
    """'run AutoForge with a credential that may read check runs'."""
    stopped.eng.github.checks_error = None


def _raise(key: str) -> Callable[[Stopped], None]:
    """'raise workflow.<key>': the configuration every later command loads, to the value
    the case's operator gives it."""

    def act(stopped: Stopped) -> None:
        assert key in stopped.raised, f"a text says to raise {key}, which the case does not hold"
        setattr(stopped.eng.config.workflow, key, stopped.raised[key])

    return act


def _keep_the_chosen_of_several(stopped: Stopped) -> None:
    """'Two or more PRs with the issue's marker ...: decide whether one of them is the
    issue's ... If one is, keep that PR open with its marker ..., and close each other PR
    or remove its marker, never the one kept ... If none is, close each': only where two
    or more PRs carry the marker; the one kept is the case's ``chosen``, none if empty."""
    marked = _marked(stopped.eng)
    if len(marked) < 2:
        return
    for url in marked:
        if url != stopped.chosen:
            _close(stopped.eng, url)


def _close_the_unadoptable(stopped: Stopped) -> None:
    """'A marked PR headed in a fork or in another repository: close it or remove its
    marker': each open marked PR whose head, or which itself, is outside the run's
    repository."""
    repository = load_state(stopped.eng.paths.state_file).repository.lower()
    for url in _marked(stopped.eng):
        pr = stopped.eng.github.prs[url]
        if pr.repository.lower() != repository or pr.head_repository.lower() != repository:
            _close(stopped.eng, url)


def _round_comments(eng: ControllerEngine) -> list[str]:
    """The comments on the run's PR that carry a review round's marker, as GitHub holds them."""
    pr = load_state(eng.paths.state_file).current_pr_url
    return [c.url for c in eng.github.comments.get(pr, []) if "ai-review-result" in c.body]


def _delete_the_stray(stopped: Stopped) -> None:
    """'Review comments of one round ... Check who posted each ... If every one is stray or
    foreign, delete each ... If one is another controller's run's review, delete none':
    the case's operator found ``others`` to be another run's; every round comment goes
    when none is, and none goes when one is."""
    found = _round_comments(stopped.eng)
    assert found, "a text has the operator check round comments, and the case has none"
    if set(found) & set(stopped.others):
        return
    pr = load_state(stopped.eng.paths.state_file).current_pr_url
    comments = stopped.eng.github.comments[pr]
    comments[:] = [c for c in comments if c.url not in found]


def _close_every_unowned(stopped: Stopped) -> None:
    """'close, delete or unmark every one that is not the run's own': each marked PR but
    the one the run is bound to. With none bound, that is every one of them."""
    own = load_state(stopped.eng.paths.state_file).current_pr_url
    for url in _marked(stopped.eng):
        if url != own:
            _close(stopped.eng, url)


def _repair_unowned(stopped: Stopped) -> None:
    """'repair or remove it if it is not the run's own; the run's own object ... needs no
    change': a marked PR whose marker names another run, or none, is closed."""
    run_id = load_state(stopped.eng.paths.state_file).run_id
    for url, owner in _marked(stopped.eng).items():
        if owner != run_id:
            _close(stopped.eng, url)


@dataclass(frozen=True)
class Reading:
    """What a displayed text tells the operator to do outside AutoForge, read literally.

    ``phrase`` is matched in the text, case and spacing aside. A step with no
    command is performed by every reading whose phrase it holds, and a step
    no reading matches fails its case: the harness never stands the gold's
    act in for a text it has no reading of.
    """

    phrase: str
    act: Callable[[Stopped], None]


READINGS = (
    Reading("read the agent's message and its run log, and resolve what it asks for", _answer),
    Reading("read the agent's message and its run log; a new run starts over", _nothing),
    Reading("the reason is text for a human, never evidence", _nothing),
    Reading("run autoforge with a credential that may read check runs", _grant),
    Reading("raise workflow.max_review_rounds in the configuration", _raise("max_review_rounds")),
    Reading("raise workflow.max_total_steps in the configuration", _raise("max_total_steps")),
    # The live preview's detail, which the operator reads at 'status --live'.
    Reading("raise 'workflow.max_review_rounds' in the config", _raise("max_review_rounds")),
    Reading("raise 'workflow.max_total_steps' in the config", _raise("max_total_steps")),
    Reading("repair or remove it if it is not the run's own", _repair_unowned),
    # claim-ambiguous goes by what the reason names (PR #511 R2-F1, R2-F2): a clause per
    # kind, each read so that it acts only on objects of its kind.
    Reading(
        "two or more prs with the issue's marker, or one with a marker that cannot be read: "
        "decide whether one of them is the issue's",
        _keep_the_chosen_of_several,
    ),
    Reading(
        "a marked pr headed in a fork or in another repository: close it or remove its marker",
        _close_the_unadoptable,
    ),
    # No case has a failed read or a PR without a readable head: nothing to close.
    Reading("a failed read or a pr without a readable head: fix what the reason names", _nothing),
    Reading("review comments of one round: the controller never adopts one", _delete_the_stray),
    # No case has two follow-up issues of one finding or two progress comments.
    Reading("follow-up issues of one finding, or progress comments on the epic", _nothing),
    Reading(
        "prs with the issue's marker: keep the run's own, and close each other one",
        _close_every_unowned,
    ),
    # The text #374 first gave two marked PRs (PR #511 R1-F1): read so that a package
    # that gives it again is measured, and found unsafe, rather than left unread.
    Reading("close, delete or unmark every one that is not the run's own", _close_every_unowned),
)


def _readings(text: str) -> list[Reading]:
    plain = " ".join(text.lower().split())
    return [r for r in READINGS if r.phrase in plain]


@dataclass(frozen=True)
class Choice:
    """When an alternative step says the operator takes it, read literally (PR #511 R2-F1).

    ``phrase`` is matched as a :class:`Reading`'s is. An alternative whose text
    holds it is taken in place of the plain ``unblock`` when ``holds`` is true
    of the case as GitHub and the state read at that step. Every other
    alternative is a choice the steps leave aside, as before.
    """

    phrase: str
    holds: Callable[[Stopped], bool]


def _kept_a_marked_pr(stopped: Stopped) -> bool:
    """'where you kept a marked PR headed in this repository': the run is bound to no PR,
    and an open PR carrying the issue's marker is headed in the run's repository."""
    state = load_state(stopped.eng.paths.state_file)
    repository = state.repository.lower()
    return not state.current_pr_url and any(
        stopped.eng.github.prs[url].head_repository.lower() == repository
        for url in _marked(stopped.eng)
    )


CHOICES = (
    Choice(
        "in place of the plain unblock, where you kept a marked pr headed in this repository",
        _kept_a_marked_pr,
    ),
)


def _taken(stopped: Stopped, package: dict) -> dict | None:
    """The alternative the operator takes in place of the plain ``unblock``, or ``None``."""
    for step in package["next"]:
        plain = " ".join(step["text"].lower().split())
        if step["alternative"] and any(c.phrase in plain and c.holds(stopped) for c in CHOICES):
            return step
    return None


def _keeps(eng: ControllerEngine, stopped: Stopped) -> dict[str, tuple[bool, bool]]:
    """The PRs the gold resolution keeps, each as it was at the stop (open, marked): the
    one the operator chose, the one the run is bound to, and each whose marker names
    the run."""
    state = load_state(eng.paths.state_file)
    marked = _marked(eng)
    keep = {url for url, owner in marked.items() if owner == state.run_id}
    keep |= {u for u in (stopped.chosen, state.current_pr_url) if u}
    return {url: (eng.github.prs[url].is_open, url in marked) for url in keep}


def _act_on(
    stopped: Stopped, text: str, done: set[int], keep: dict[str, tuple[bool, bool]]
) -> list[str]:
    """Perform what ``text`` tells the operator to do, each reading once; what it broke.

    The act happens outside AutoForge, so the state directory must not change,
    and a PR the gold keeps must stay as open, and as marked, as it was before
    the act: ``keep`` holds that, and is updated to what the act left. A comment
    the case's operator found to be another controller's run's record
    (``Stopped.others``) must still be there after it (PR #511 R2-F2).
    """
    eng = stopped.eng
    tree = state_tree(eng.paths.state_dir)
    held = _comments_held(eng, stopped.others)
    for reading in _readings(text):
        if id(reading) not in done:
            done.add(id(reading))
            reading.act(stopped)
    assert state_tree(eng.paths.state_dir) == tree, "an operator's act wrote the state"
    marked = _marked(eng)
    broke = []
    for url, (was_open, was_marked) in sorted(keep.items()):
        now = (eng.github.prs[url].is_open, url in marked)
        if was_open and not now[0]:
            broke.append(f"{url} closed")
        elif was_marked and not now[1]:
            broke.append(f"{url} unmarked")
        keep[url] = now  # the next act is judged by what it changes
    broke += [f"{url} deleted" for url in held - _comments_held(eng, stopped.others)]
    return broke


def _comments_held(eng: ControllerEngine, urls: tuple[str, ...]) -> set[str]:
    """Which of the comments ``urls`` GitHub still holds."""
    held = {c.url for comments in eng.github.comments.values() for c in comments}
    return held & set(urls)


# -- the after arm's steps: what is unsafe, and what following them does -----------------------

#: The flags of ``unblock`` that name a saved action, and every flag it may be shown with.
_ACTION_FLAGS = ("--approve-action", "--decline-action", "--bind-evidence")
_UNBLOCK_FLAGS = (*_ACTION_FLAGS, "--reason", "--take-over", "--rereview")


def _unsafe(package: dict, records: list[EffectRecord]) -> list[str]:
    """The displayed commands ADR 0012 D2 denies for the stop, or D3 grants nobody.

    A ``resume`` or ``step`` before the run has left BLOCKED, any ``unblock``
    of a run that is not BLOCKED (an alternative step stands in place of the
    steps before it, so it meets the run as it stopped), an approval of an action that is not
    awaiting the operator's approval, a decline or a bind of an action the
    plan does not hold, an open merge gate, an accepted configuration change,
    and any command that is not the controller's or the documented ``gh``
    pipe of an operator action. A step with no command is judged by what
    performing it does (:func:`_act_on`), when the steps are followed.
    """
    at_stop = package["outcome"] == "blocked"
    blocked = at_stop  # as the steps in sequence leave it; an alternative replaces them
    approvable = {
        r.action_id
        for r in records
        if r.kind in (EffectKind.ACCEPTANCE_COMMENT, EffectKind.PR_BODY_UPDATE)
        and r.precondition.get("authorization") == "operator"
    }
    held = {r.action_id for r in records if r.action_id}
    bad: list[str] = []
    for step in package["next"]:
        words = _words(step["command"])
        if not words:
            continue
        verb = words[0]
        now = at_stop if step["alternative"] else blocked
        if "--allow-merge" in words or "--accept-config-change" in words:
            bad.append(step["command"])
        elif "|" in words:
            pipe = words.index("|")
            read, post = words[:pipe], words[pipe + 1 :]
            if not (
                read[:2] == ["status", "--action"]
                and read[2] in held
                and post[:3] == ["gh", "issue", "comment"]
                and post[4:] == ["--body-file", "-"]
            ):
                bad.append(step["command"])
        elif verb in ("status", "doctor", "run"):
            continue
        elif verb == "unblock":
            named = [(w, words[i + 1]) for i, w in enumerate(words) if w in _ACTION_FLAGS]
            flags = {w for w in words if w.startswith("--")}
            if (
                not now
                or not flags <= set(_UNBLOCK_FLAGS)
                or any(flag == "--approve-action" and a not in approvable for flag, a in named)
                or any(a not in held for _, a in named)
            ):
                bad.append(step["command"])
            elif not step["alternative"]:
                blocked = False  # the run is no longer BLOCKED once the unblock is applied
        elif verb in ("resume", "step"):
            if now or package["outcome"] == "failed":
                bad.append(step["command"])
        else:
            bad.append(step["command"])
    return bad


@dataclass(frozen=True)
class Ran:
    """One command the harness typed: its words, its exit status and what it saved."""

    words: tuple[str, ...]
    rc: int
    reached: str  # the phase, and "/code" when a stop code is saved


@dataclass
class Followed:
    """What following the displayed steps did, as the controller saved it."""

    ran: list[Ran] = field(default_factory=list)  # every controller command, in order
    gold: Ran | None = None  # the gold command, when the steps ran it
    next: Ran | None = None  # the workflow step after the gold (the gold itself for resume/run)
    preview: bool | None = None  # a plain unblock's live preview matched its decision
    unsafe: list[str] = field(default_factory=list)  # steps whose literal act broke a kept PR


def _phase(eng: ControllerEngine) -> str:
    return load_state(eng.paths.state_file).phase.value


def _reached(eng: ControllerEngine) -> str:
    state = load_state(eng.paths.state_file)
    return f"{state.phase.value}/{state.stop_code}" if state.stop_code else state.phase.value


def _resolved(done: Followed, gold: Gold) -> bool:
    """The steps resolve the stop: every command they ran exited 0, the gold command
    among them saved the gold phase, and the workflow step after it ran, exited 0 and
    saved what the gold says it must (PR #511 R1-F2)."""
    return (
        done.gold is not None
        and done.next is not None
        and all(r.rc == 0 for r in done.ran)
        and done.gold.reached.split("/")[0] == gold.target
        and done.next.reached == gold.next_saves
    )


def _post_payload(operator, eng: ControllerEngine, words: list[str]) -> None:
    """``status --action ID | gh issue comment TARGET --body-file -``: the operator's post."""
    pipe = words.index("|")
    rc, out, err = _status(operator, eng, *words[1:pipe])
    assert rc == 0, err
    target = words[pipe + 4]
    posted = sum(len(comments) for comments in eng.github.comments.values())
    eng.github.add_comment(target, 9000 + posted, out)


def _follow(operator, stopped: Stopped, package: dict, gold: list[str]) -> Followed:
    """Run the package's displayed steps in order, as the operator would type them.

    A step with no command is performed as its text reads (:func:`_act_on`),
    and so is the detail of a ``status --live`` preview, which the operator
    reads there. A ``doctor`` is not run: it checks this host's environment,
    which a case does not stand in for. Every other command runs and its exit
    status is kept. An alternative is left aside, unless its text says when
    it is taken in place of the plain ``unblock`` and that holds when the
    steps reach it (:data:`CHOICES`). The steps end with the first command
    that runs the workflow (``resume`` or ``run``), given ``--max-steps 1``
    so that the step right after the stop is the one read back.
    """
    eng = stopped.eng
    done, keep = Followed(), _keeps(eng, stopped)
    acted: set[int] = set()
    for step in package["next"]:
        if step["alternative"]:
            assert step["command"], f"an alternative with no command: {step['text']!r}"
            continue
        words = _words(step["command"])
        if not words:
            assert _readings(step["text"]), (
                f"no reading of the step {step['text']!r}: add one to READINGS, as it reads"
            )
            if _act_on(stopped, step["text"], acted, keep):
                done.unsafe.append(step["text"])
            continue
        if "|" in words:
            _post_payload(operator, eng, words)
        elif words[0] == "status" and "--live" in words:
            rc, out, err = _status(operator, eng, *words[1:], "--json")
            assert rc == 0, err
            live = json.loads(out)["recovery"]["live"]
            if _act_on(stopped, live.get("detail") or "", acted, keep):
                done.unsafe.append(step["command"])
        elif words[0] == "status":
            rc, _, err = _status(operator, eng, *words[1:])
            assert rc == 0, err
        elif words[0] == "unblock":
            taken = _taken(stopped, package) if words == ["unblock", "--reason", VALUE] else None
            if taken is not None:
                words = _words(taken["command"])
            preview = None
            if words == ["unblock", "--reason", VALUE]:
                rc, out, err = _status(operator, eng, "--json", "--live")
                assert rc == 0, err
                preview = json.loads(out)["recovery"]["live"]
            rc, _, _ = operator(eng, *_fill(words, stopped))
            ran = Ran(tuple(words), rc, _reached(eng))
            done.ran.append(ran)
            if preview is not None:
                done.preview = (preview["target_phase"] or "BLOCKED") == _phase(eng)
            if words == gold:
                done.gold = ran
        elif words[0] in ("resume", "run"):
            rc, _, _ = operator(eng, *_fill(words, stopped), "--max-steps", "1", agent=stopped.then)
            ran = Ran(tuple(words), rc, _reached(eng))
            done.ran.append(ran)
            if words == gold:
                done.gold = ran
            if done.gold is not None:
                done.next = ran
            break
    return done


# -- the saved plan: named, the relevant candidate, and kept (PR #511 R1-F3) -----------------

_STAGES = (Stage.INTENDED, Stage.ATTEMPTED, Stage.OBSERVED)


def _identity(record: EffectRecord) -> tuple:
    """A record's identity: what it writes, where, and with what, never how far it got."""
    return (
        record.position,
        record.kind,
        json.dumps(record.identity, sort_keys=True),
        json.dumps(record.target, sort_keys=True),
        json.dumps(record.payload, sort_keys=True),
    )


def _git(eng: ControllerEngine, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(eng.workdir), *args], capture_output=True, text=True)


@dataclass(frozen=True)
class Plan:
    """The saved plan at the stop, and what the repository and GitHub held of it then."""

    phase: Phase
    records: tuple[EffectRecord, ...]
    candidate: str  # the push's commit, "" for a plan with no push
    ref: str  # the ref the push targets
    problems: tuple[str, ...]  # what the package or the plan got wrong at the stop


def _plan_at_stop(stopped: Stopped, package: dict, records: list[EffectRecord]) -> Plan | None:
    """The plan the stop saved, checked as the stop left it; ``None`` when none is saved.

    The package names the journal and, for a push, its commit, as data not to
    touch. The push is the plan's only one, and its commit is the candidate:
    the commit the agent worktree holds, a descendant of what the push's
    precondition read (the reviewed head a FIX builds on, the default branch
    head an ANALYZE_EXECUTE builds on), and every fixed finding's commit is
    in it. A push not observed has not reached GitHub's PR, which is still at
    the precondition's head; an observed one is on the ref.
    """
    if not records:
        return None
    eng = stopped.eng
    state = load_state(eng.paths.state_file)
    effects = state.phase_effects()
    problems = []
    preserve = package["preserve"]
    if "journal" not in {c["key"] for c in preserve}:
        problems.append("the package does not name the journal")
    pushes = [r for r in records if r.kind is EffectKind.PUSH]
    if len(pushes) > 1:
        problems.append(f"{len(pushes)} pushes in one plan")
    push = pushes[0] if pushes else None
    candidate = push.payload["sha"] if push else ""
    ref = push.target["ref"] if push else ""
    if push is not None:
        texts = " ".join(c["text"] for c in preserve)
        if candidate not in texts:
            problems.append(f"the package does not name the candidate {candidate}")
        if push.identity.get("candidate_sha") != candidate:
            problems.append("the push's identity names another candidate")
        worktree = eng.agent_worktree_path()
        head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        if head != candidate:
            problems.append(f"the push is {candidate}, the worktree holds {head or 'nothing'}")
        base = push.precondition.get("expected_old") or push.precondition["base_sha"]
        if _git(eng, "merge-base", "--is-ancestor", base, candidate).returncode != 0:
            problems.append(f"the candidate {candidate} does not descend from {base}")
        if isinstance(effects.context, FixContext):
            for resolution in effects.context.resolutions:
                sha = resolution.get("commit_sha") or ""
                if sha and _git(eng, "merge-base", "--is-ancestor", sha, candidate).returncode:
                    problems.append(f"the fixed commit {sha} is not in the candidate")
        on_ref = eng.origin.head(ref.removeprefix("refs/heads/"))
        if push.stage is Stage.OBSERVED and on_ref != candidate:
            problems.append(f"an observed push, and {ref} is at {on_ref}")
        if push.stage is Stage.INTENDED and on_ref == candidate:
            problems.append(f"a push never sent, and {ref} is at it")
        if state.current_pr_url and push.stage is not Stage.OBSERVED:
            at = eng.github.get_pr(state.current_pr_url).head_sha
            if at == candidate:
                problems.append(f"an unpushed candidate, and the PR is already at {at}")
    elif not isinstance(effects.context, (AnalyzeContext, FixContext)):
        problems.append("a plan with no push and no context")
    return Plan(effects.phase, tuple(records), candidate, ref, tuple(problems))


def _read_back(eng: ControllerEngine, record: EffectRecord, plan: Plan) -> str:
    """Whether GitHub and the origin hold ``record``'s effect, exactly once; "" if they do.

    Only for a phase that went on: the save that commits it drops its
    records (ADR 0004 D2.4), so what they wrote is read where it was written.
    """
    state = load_state(eng.paths.state_file)
    body = record.payload.get("body")
    if record.kind is EffectKind.PUSH:
        on_ref = eng.origin.head(plan.ref.removeprefix("refs/heads/"))
        pr = eng.github.get_pr(state.current_pr_url).head_sha if state.current_pr_url else ""
        if not (on_ref == pr == state.current_head_sha == plan.candidate):
            return f"the candidate {plan.candidate}: {plan.ref} at {on_ref}, the PR at {pr}"
        return ""
    if record.kind is EffectKind.IMPLEMENTATION_PR:
        prs = [
            url
            for url, pr in eng.github.prs.items()
            if pr.is_open and pr.head_ref == record.target["head"] and pr.body == body
        ]
        if prs != [state.current_pr_url]:
            return f"the implementation PR: {prs} open, the run bound to {state.current_pr_url}"
        return ""
    if record.kind in (EffectKind.ACCEPTANCE_COMMENT, EffectKind.OPERATOR_COMMENT):
        target = record.target["issue_url"]
        posted = [c for c in eng.github.comments.get(target, []) if c.body.rstrip("\n") == body]
        return "" if len(posted) == 1 else f"{len(posted)} comments of {record.describe()}"
    if record.kind is EffectKind.PR_BODY_UPDATE:
        held = eng.github.get_pr(record.target["pr_url"]).body
        return "" if held == body else f"the PR description is not {record.describe()}'s"
    return f"no read-back for {record.kind.value}"


def _plan_problems(stopped: Stopped, plan: Plan) -> list[str]:
    """What of ``plan`` was lost once the steps were followed: ``[]`` when it was kept.

    The candidate is still a commit of the repository. While the run is still
    in the plan's entry (or BLOCKED or FAILED on it), every record of the
    stop is in the journal with its identity, at a stage no earlier than it
    was. Once the phase went on, every record's effect is read back from
    GitHub and the origin (:func:`_read_back`), and the run's PR is at the
    candidate.
    """
    eng = stopped.eng
    problems = list(plan.problems)
    if plan.candidate:
        if _git(eng, "cat-file", "-e", f"{plan.candidate}^{{commit}}").returncode != 0:
            problems.append(f"the candidate {plan.candidate} is gone")
    state = load_state(eng.paths.state_file)
    after = {_identity(r): r for r in _records(eng)}
    if after or state.phase in (plan.phase, Phase.BLOCKED, Phase.FAILED):
        for record in plan.records:
            kept = after.get(_identity(record))
            if kept is None:
                problems.append(f"{record.describe()} is no longer in the journal")
            elif record.stage in _STAGES and kept.stage in _STAGES:
                if _STAGES.index(kept.stage) < _STAGES.index(record.stage):
                    problems.append(f"{record.describe()} went back to {kept.stage.value}")
            elif record.stage is Stage.OBSERVED and kept.stage is not Stage.OBSERVED:
                problems.append(f"{record.describe()} went back to {kept.stage.value}")
        return problems
    for record in plan.records:
        lost = _read_back(eng, record, plan)
        if lost:
            problems.append(lost)
    return problems


# -- one row of the matrix ---------------------------------------------------------------------

#: The columns the harness computes, in the document's order; the others are annotations.
MEASURED = (
    "Gold stop",
    "Gold resolution",
    "Gold read-back",
    "Before: a stop",
    "Before: command",
    "After: stop line",
    "After: later status",
    "After: command",
    "After: unsafe",
    "After: steps resolve",
    "After: preview",
    "Plan kept",
)


def _yes(value: bool) -> str:
    return "yes" if value else "no"


#: How the matrix spells the scenario's own objects in a gold command.
_SHOWN = {EPIC: "<EPIC>", ISSUE: "<issue>", PR: "<PR>", VALUE: "…"}


def _gold_text(gold: Gold) -> str:
    if not gold.command:
        return "none: the controller recovers"
    words = gold.command.format(action="<id>").split()
    shown = [
        "<PR kept>" if w == VALUE and i and words[i - 1] == "--take-over" else _SHOWN.get(w, w)
        for i, w in enumerate(words)
    ]
    command = " ".join(shown)
    return f"{gold.act}, then `{command}`" if gold.act else f"`{command}`"


def _gold_read_back(gold: Gold) -> str:
    """The phases the gold resolution must leave saved: after its command, and after the
    workflow step that follows an unblock."""
    if gold.unblocks:
        return f"{gold.target}, then `resume`: {gold.then}"
    return gold.target


def _perform(operator, stopped: Stopped, gold: Gold, words: list[str]) -> tuple[Ran, Ran]:
    """The gold resolution itself: the act, the command, and the workflow step after an
    unblock. What the command saved, and what that step saved."""
    eng = stopped.eng
    if stopped.act is not None:
        stopped.act()
    filled = _fill(words, stopped)
    if words[0] == "unblock":
        rc, _, err = operator(eng, *filled)
        command = Ran(tuple(words), rc, _reached(eng))
        if rc != 0:
            return command, command
        rc, _, err = operator(eng, "resume", "--max-steps", "1", agent=stopped.then)
        return command, Ran(("resume",), rc, _reached(eng))
    rc, _, err = operator(eng, *filled, "--max-steps", "1", agent=stopped.then)
    command = Ran(tuple(words), rc, _reached(eng))
    return command, command


def _holds(gold: Gold, command: Ran, step: Ran) -> None:
    """The gold resolution, performed, saved what it says: every gold label is held by
    the controller's own read-back, with each command's exit status."""
    assert (command.rc, command.reached.split("/")[0]) == (0, gold.target), command
    assert (step.rc, step.reached) == (0, gold.next_saves), step


@dataclass
class Measured:
    row: dict[str, str]
    status_cpu_seconds: float | None = None
    unsafe: list[str] = field(default_factory=list)


def _measure(case: Case, operator, fresh: Callable[[], Path]) -> Measured:
    """Build the case, read both arms, follow the package, and perform the gold."""
    gold = case.gold
    stopped = case.build(_own_state_dir(fresh()))
    eng = stopped.eng
    row = dict.fromkeys(MEASURED, NONE)
    row["Gold stop"] = gold.stop
    row["Gold resolution"] = _gold_text(gold)
    row["Gold read-back"] = _gold_read_back(gold)
    if gold.stop == "none":
        # The controller resolved it itself: no operator command, and the run went on.
        assert _phase(eng) == gold.target, [o.message for o in stopped.steps]
        assert all(o.stop is None for o in stopped.steps)
        row["After: steps resolve"] = "yes"
        return Measured(row)

    try:
        records = _records(eng)
    except StateError:
        records = []
    words = _gold_words(gold, records)
    phase = _phase_or_refused(eng)
    row["Before: a stop"] = _yes(phase in ("BLOCKED", "FAILED", "refused"))
    row["Before: command"] = _yes(_spelled(stopped.said, words))
    if stopped.line is not None:
        row["After: stop line"] = f"{stopped.line.outcome.value}/{stopped.line.code.value}"

    tree, writes = state_tree(eng.paths.state_dir), _github_writes(eng.github)
    started = time.process_time()
    rc, out, err = operator(eng, "status", "--json")
    cpu = time.process_time() - started
    assert state_tree(eng.paths.state_dir) == tree, "status wrote to the state directory"
    assert _github_writes(eng.github) == writes, "status sent something to GitHub"
    measured = Measured(row, status_cpu_seconds=cpu)
    if rc != 0:
        row["After: later status"] = f"refused (exit {rc})"
        row["After: command"] = _yes(_spelled(err, words))
        _holds(gold, *_perform(operator, stopped, gold, words))
        return measured

    package = json.loads(out)["recovery"]
    row["After: later status"] = f"{package['outcome']}/{package['code']}"
    row["After: command"] = _yes(any(_words(s["command"]) == words for s in package["next"]))
    plan = _plan_at_stop(stopped, package, records)
    done = _follow(operator, stopped, package, words)
    measured.unsafe = _unsafe(package, records) + done.unsafe
    row["After: unsafe"] = str(len(measured.unsafe))
    resolved = _resolved(done, gold)
    row["After: steps resolve"] = _yes(resolved)
    if done.preview is not None:
        row["After: preview"] = _yes(done.preview)
    if plan is not None:
        row["Plan kept"] = _yes(not _plan_problems(stopped, plan))
    if not resolved:
        # The displayed steps did not resolve it: perform the gold on a fresh copy, so
        # that every gold label is held by the controller's own read-back.
        _holds(gold, *_perform(operator, case.build(_own_state_dir(fresh())), gold, words))
    return measured


def _phase_or_refused(eng: ControllerEngine) -> str:
    try:
        return _phase(eng)
    except StateError:
        return "refused"


# -- the document ------------------------------------------------------------------------------


def _section(text: str, heading: str) -> str:
    """The body of the section ``heading`` opens, up to the next heading of its level or above."""
    start = text.index(heading + "\n")
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading) + 1 :]
    stop = re.search(rf"^#{{1,{level}}} ", rest, re.MULTILINE)
    return rest[: stop.start()] if stop else rest


def _table(section: str) -> list[dict[str, str]]:
    """The first Markdown table of ``section``, one dict per row, cells stripped."""
    lines = [line for line in section.splitlines() if line.startswith("|")]
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = []
    for line in lines[2:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        assert len(cells) == len(header), line
        rows.append(dict(zip(header, cells, strict=True)))
    return rows


def _documented() -> dict[str, dict[str, str]]:
    rows = _table(_section(DOC.read_text(encoding="utf-8"), MATRIX))
    return {row["Case"].strip("`"): row for row in rows}


# -- the tests ---------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_the_documented_row_is_the_row_the_case_measures(
    case, operator, tmp_path_factory, record_property
):
    """Each case's row, computed: what the document's matrix says of it.

    The CPU time of the later ``status --json`` is recorded as the test's
    ``status_cpu_seconds`` property, which ``--junitxml`` reports with
    ``-o junit_family=xunit1``; it is a measurement of this host, never a bound.
    """
    measured = _measure(case, operator, lambda: tmp_path_factory.mktemp("case") / ".autoforge")
    if measured.status_cpu_seconds is not None:
        record_property("status_cpu_seconds", round(measured.status_cpu_seconds, 4))
    documented = _documented().get(case.name)
    computed = json.dumps(measured.row, ensure_ascii=False, indent=1)
    assert documented is not None, f"no row for {case.name}; computed:\n{computed}"
    assert {k: documented.get(k) for k in MEASURED} == measured.row, f"computed:\n{computed}"
    assert measured.unsafe == [], measured.unsafe


def test_the_matrix_has_one_row_per_case_and_no_other():
    assert list(_documented()) == [c.name for c in CASES]


def _count(rows: list[dict[str, str]], column: str, *, of: str = "yes") -> int:
    return sum(1 for row in rows if row[column] == of)


def _summary(rows: dict[str, dict[str, str]]) -> dict[str, tuple[str, str]]:
    """Section 5's table, recomputed from section 4's rows by the rules section 3.3 gives."""
    every = list(rows.values())
    stops = [r for r in every if r["Gold stop"] != "none"]
    lines = [r for r in stops if r["Gold stop"] != NONE]
    packages = [r for r in stops if r["After: unsafe"].isdigit()]
    previews = [r for r in stops if r["After: preview"] != NONE]
    plans = [r for r in stops if r["Plan kept"] != NONE]
    by_itself = sum(
        1 for r in every if r["Gold stop"] == "none" and r["After: steps resolve"] == "yes"
    )

    def of(count: int, total: list) -> str:
        return f"{count} of {len(total)}"

    gold_line = sum(1 for r in lines if r["After: stop line"] == r["Gold stop"])
    gold_status = sum(1 for r in stops if r["After: later status"] == r["Gold stop"])
    unknown = sum(1 for r in stops if r["After: later status"].endswith("/unknown"))
    unsafe = sum(int(r["After: unsafe"]) for r in packages)
    return {
        "A later `status` shows that the run stopped": (
            of(_count(stops, "Before: a stop"), stops),
            of(_count(stops, "Before: a stop"), stops),
        ),
        "The stopping command names the gold outcome and code": (
            of(0, lines),
            of(gold_line, lines),
        ),
        "A later `status` names the gold code": (of(0, stops), of(gold_status, stops)),
        "A later `status` says the code is unknown": (of(0, stops), of(unknown, stops)),
        "The text, or the package, spells the gold command": (
            of(_count(stops, "Before: command"), stops),
            of(_count(stops, "After: command"), stops),
        ),
        "Unsafe displayed steps, those without a command performed as they read": (
            "not measured",
            f"{unsafe} in {len(packages)} packages",
        ),
        "The displayed steps, followed as typed, resolve the stop, and the next step succeeds": (
            NONE,
            of(_count(packages, "After: steps resolve"), packages),
        ),
        "A plain `unblock`'s live preview was its decision": (
            NONE,
            of(_count(previews, "After: preview"), previews),
        ),
        "The saved plan was named, its push was the candidate, and every record held": (
            NONE,
            of(_count(plans, "Plan kept"), plans),
        ),
        "Resolved by the controller with no operator command": (
            of(by_itself, every),
            of(by_itself, every),
        ),
    }


def test_the_summary_is_what_the_matrix_adds_up_to():
    """Section 5's numbers are section 4's rows counted, never typed in on their own."""
    table = _table(_section(DOC.read_text(encoding="utf-8"), SUMMARY))
    documented = {row["Measure"]: (row["Before"], row["After"]) for row in table}
    assert documented == _summary(_documented())


#: A test named in the record: `tests/test_x.py::test_y`.
_TEST_REF = re.compile(r"`(tests/test_[a-z0-9_]+\.py)::(test_[a-z0-9_]+)`")


def test_every_test_the_evaluation_names_exists():
    """A test the record relies on that is renamed or deleted takes the record with it."""
    text = DOC.read_text(encoding="utf-8")
    named = _TEST_REF.findall(_section(text, SAFETY))
    assert len(named) >= 30, "section 6 names the tests that hold each regression"
    missing = []
    for module, test in set(_TEST_REF.findall(text)):
        tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        if test not in defined:
            missing.append(f"{module}::{test}")
    assert missing == []


def test_every_stratum_issue_375_names_is_a_case_or_says_why_not():
    """Section 3.1 takes #375's strata, and every case it names is a case of the matrix."""
    rows = _table(_section(DOC.read_text(encoding="utf-8"), "### 3.1 The sample"))
    cases = {c.name for c in CASES}
    named = set()
    for row in rows:
        named |= set(re.findall(r"`([a-z0-9-]+)`", row["Cases"]))
    assert named == cases
    strata = " ".join(row["Stratum (#375)"] for row in rows)
    for stratum in ("#291", "#327", "K9", "K10", "crash", "LOCAL", "FAILED", "timeouts"):
        assert stratum in strata, stratum
    assert "outside AutoForge" in strata and "review report" in strata


# -- the measure, against what it must not count (PR #511 R1-F1 to R1-F3) --------------------


def _case(name: str) -> Case:
    [case] = [c for c in CASES if c.name == name]
    return case


def _stopped_with_package(name: str, operator, tmp: Path) -> tuple[Stopped, dict, list]:
    stopped = _case(name).build(_own_state_dir(tmp))
    records = _records(stopped.eng)
    rc, out, err = _status(operator, stopped.eng, "--json")
    assert rc == 0, err
    return stopped, json.loads(out)["recovery"], records


def test_a_case_keeps_its_checkout_origin_and_worktree_in_the_tests_own_directory(tmp_path):
    """R3-F1: a case's checkout, bare origin and agent worktree are all in the test's own
    directory, never in the session's base directory, where the next test to build a
    case would find them and resume on their branch. A state directory that would put
    them anywhere shared is refused before anything is built."""
    with pytest.raises(AssertionError, match="needs a state directory of its own"):
        _blocked(_fork_headed, tmp_path)
    own = tmp_path.resolve()
    eng = _case("agent-blocked").build(tmp_path / ".autoforge").eng
    assert Path(eng.workdir).resolve() == own
    assert eng.origin.bare.resolve().parent == own
    assert eng.agent_worktree_path().resolve().is_relative_to(own)
    with pytest.raises(AssertionError, match="already is another case's checkout"):
        _blocked(_fork_headed, tmp_path / "again")


#: The step #374 first gave two marked PRs that the run recorded neither of.
_CLOSE_EVERY_UNOWNED = "close, delete or unmark every one that is not the run's own, then 'unblock'"


def test_a_manual_step_that_closes_the_pr_the_gold_keeps_is_unsafe(operator, tmp_state_dir):
    """R1-F1: the text first measured for two marked PRs, performed as it reads. Neither
    PR is the run's own, so it closes both, the PR the operator chose among them: an
    unsafe step, counted as one, and the steps no longer resolve the stop. The act is
    the text's, never the gold's, which closes only the other PR. With no marked PR
    left, no take-over is taken, and the plain unblock and 'resume' start a second
    implementation (here an agent that stops), not the gold's take-over of the PR."""
    stopped, package, records = _stopped_with_package("ambiguous-prs", operator, tmp_state_dir)
    first = package["next"][0]
    assert first["command"] == "" and "never the one kept" in first["text"]
    first["text"] = _CLOSE_EVERY_UNOWNED
    stopped.then = [block(_stopping("blocked", "both PRs of the issue were closed"))]
    gold = _gold_words(_case("ambiguous-prs").gold, records)
    done = _follow(operator, stopped, package, gold)
    assert done.unsafe == [_CLOSE_EVERY_UNOWNED]
    assert not stopped.eng.github.prs[PR].is_open and not stopped.eng.github.prs[STALE].is_open
    assert done.gold is None and [r.words[0] for r in done.ran] == ["unblock", "resume"]
    assert not _resolved(done, _case("ambiguous-prs").gold)


def test_a_manual_step_with_no_reading_fails_the_case(operator, tmp_state_dir):
    """R1-F1: a step with no command that no reading covers is never performed as the
    gold's act: the case fails, naming the text, and nothing on GitHub changed."""
    stopped, package, records = _stopped_with_package("ambiguous-prs", operator, tmp_state_dir)
    package["next"][0]["text"] = "put things right"
    before = _github_writes(stopped.eng.github)
    gold = _gold_words(_case("ambiguous-prs").gold, records)
    with pytest.raises(AssertionError, match="no reading of the step 'put things right'"):
        _follow(operator, stopped, package, gold)
    assert _github_writes(stopped.eng.github) == before


def test_a_package_that_omits_the_next_step_does_not_resolve(operator, tmp_state_dir):
    """R1-F2: the unblock runs and saves the gold phase, but the package shows no
    'resume', so no workflow step runs after it: not a resolution."""
    stopped, package, records = _stopped_with_package("agent-blocked", operator, tmp_state_dir)
    gold = _case("agent-blocked").gold
    package["next"] = [s for s in package["next"] if _words(s["command"])[:1] != ["resume"]]
    done = _follow(operator, stopped, package, _gold_words(gold, records))
    assert done.gold is not None and (done.gold.rc, done.gold.reached) == (0, gold.target)
    assert done.next is None and not _resolved(done, gold)


def test_a_resume_that_fails_with_the_phase_unchanged_does_not_resolve(operator, tmp_state_dir):
    """R1-F2: GitHub answers 502 to the PR the resumed step opens: 'resume' exits 1 and
    leaves ANALYZE_EXECUTE, which is neither BLOCKED nor FAILED. Its exit status and the
    phase it saved, not REVIEW, make it no resolution; so does any failed command."""
    stopped, package, records = _stopped_with_package("agent-blocked", operator, tmp_state_dir)
    gold = _case("agent-blocked").gold
    stopped.eng.github.write_failures.append(
        ("create_pull_request", GitHubUnavailableError("gh: HTTP 502"), False)
    )
    done = _follow(operator, stopped, package, _gold_words(gold, records))
    assert done.next is not None and done.next.rc == 1
    assert done.next.reached == gold.target == "ANALYZE_EXECUTE"
    assert not _resolved(done, gold)
    # Even where the phase it leaves is the one a gold names, a failed step is no
    # resolution, and neither is a failed command before it.
    unchanged = Gold(gold.stop, gold.command, gold.target, then="ANALYZE_EXECUTE")
    assert not _resolved(done, unchanged)
    ran = Ran(("resume",), 0, "ANALYZE_EXECUTE")
    refused = Followed(ran=[Ran(("unblock",), 1, "BLOCKED"), ran], gold=ran, next=ran)
    assert not _resolved(refused, unchanged)


def test_a_plan_whose_effect_is_lost_or_doubled_is_not_kept(operator, tmp_path_factory):
    """R1-F3: K9's plan followed to REVIEW, then read back from GitHub: the acceptance
    comment removed, or posted twice, is a receipt the plan does not hold."""
    for doubled in (False, True):
        tmp = tmp_path_factory.mktemp("k9") / ".autoforge"
        stopped, package, records = _stopped_with_package("k9-acceptance-action", operator, tmp)
        plan = _plan_at_stop(stopped, package, records)
        assert plan is not None and plan.problems == ()
        gold = _case("k9-acceptance-action").gold
        assert _resolved(_follow(operator, stopped, package, _gold_words(gold, records)), gold)
        assert _plan_problems(stopped, plan) == []
        [k9] = [r for r in records if r.kind is EffectKind.ACCEPTANCE_COMMENT]
        comments = stopped.eng.github.comments[k9.target["issue_url"]]
        [posted] = [c for c in comments if c.body.rstrip("\n") == k9.payload["body"]]
        if doubled:
            stopped.eng.github.add_comment(k9.target["issue_url"], 99_001, posted.body)
        else:
            comments.remove(posted)
        assert _plan_problems(stopped, plan) == [
            f"{2 if doubled else 0} comments of {k9.describe()}"
        ]


def test_a_pr_head_moved_off_the_candidate_is_not_kept(operator, tmp_state_dir):
    """R1-F3: the plan completed and the run went on; then another push puts the PR's
    branch back on the reviewed head. The PR head GitHub reports, the origin's ref and
    the candidate disagree, so the plan is not kept."""
    stopped, package, records = _stopped_with_package(
        "k10-operator-actions", operator, tmp_state_dir
    )
    plan = _plan_at_stop(stopped, package, records)
    gold = _case("k10-operator-actions").gold
    assert _resolved(_follow(operator, stopped, package, _gold_words(gold, records)), gold)
    assert plan is not None and _plan_problems(stopped, plan) == []
    [push] = [r for r in records if r.kind is EffectKind.PUSH]
    base = push.precondition["expected_old"]
    stopped.eng.origin.publish(stopped.eng.workdir, base, plan.ref.removeprefix("refs/heads/"))
    [lost] = _plan_problems(stopped, plan)
    assert lost.startswith(f"the candidate {plan.candidate}: {plan.ref} at {base}")


def test_a_push_that_is_not_the_relevant_candidate_is_not_kept(operator, tmp_path_factory):
    """R1-F3: the plan's push is checked against the candidate at the stop. A package
    that names another commit, or a worktree that holds another, fails it; and a
    journal that no longer holds a record of the stop, by its identity, fails the plan
    while the run is still on it."""
    tmp = tmp_path_factory.mktemp("k9") / ".autoforge"
    stopped, package, records = _stopped_with_package("k9-acceptance-action", operator, tmp)
    [push] = [r for r in records if r.kind is EffectKind.PUSH]
    reviewed = push.precondition["expected_old"]
    candidate = push.payload["sha"]
    named = copy.deepcopy(package)
    for claim in named["preserve"]:
        claim["text"] = claim["text"].replace(candidate, reviewed)
    plan = _plan_at_stop(stopped, named, records)
    assert plan is not None
    assert plan.problems == (f"the package does not name the candidate {candidate}",)

    worktree = stopped.eng.agent_worktree_path()
    git = ["git", "-C", str(worktree), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "later"], check=True)
    moved = _plan_at_stop(stopped, package, records)
    assert moved is not None and len(moved.problems) == 1
    assert moved.problems[0].startswith(f"the push is {candidate}, the worktree holds ")
    subprocess.run([*git, "reset", "-q", "--hard", candidate], check=True)

    kept = _plan_at_stop(stopped, package, records)
    assert kept is not None and kept.problems == () and _plan_problems(stopped, kept) == []
    [k9] = [r for r in records if r.kind is EffectKind.ACCEPTANCE_COMMENT]
    other = dataclasses.replace(k9, payload={"body": f"{k9.payload['body']} (another)"})
    swapped = dataclasses.replace(kept, records=(other, push))
    assert _plan_problems(stopped, swapped) == [f"{other.describe()} is no longer in the journal"]


# -- claim-ambiguous beyond the matrix's two PRs (PR #511 R2-F1, R2-F2) ------------------------


def _followed(operator, stopped: Stopped, gold: Gold) -> tuple[dict, list, Followed]:
    """A stop outside the matrix, its package's steps followed as the matrix's are."""
    records = _records(stopped.eng)
    rc, out, err = _status(operator, stopped.eng, "--json")
    assert rc == 0, err
    package = json.loads(out)["recovery"]
    assert f"{package['outcome']}/{package['code']}" == gold.stop
    words = _gold_words(gold, records)
    assert any(_words(s["command"]) == words for s in package["next"])
    return package, records, _follow(operator, stopped, package, words)


def test_a_fork_headed_pr_followed_as_displayed_is_closed_and_the_plain_unblock_resolves(
    operator, tmp_state_dir
):
    """R2-F1: one marked PR headed in a fork stops with claim-ambiguous and no PR bound,
    as two marked PRs do. Its steps, followed as typed, close the fork's PR as the
    reason says, take no take-over (none is kept, and the controller refuses a fork's),
    and the plain unblock re-enters ANALYZE_EXECUTE, whose next step implements the
    issue afresh: every command exits 0 and the step saves REVIEW."""
    stopped = _blocked(_fork_headed, tmp_state_dir)
    stopped.then = implement
    gold = Gold(
        "blocked/claim-ambiguous", _PLAIN, "ANALYZE_EXECUTE", "REVIEW", "close the fork's PR"
    )
    package, records, done = _followed(operator, stopped, gold)
    assert _unsafe(package, records) == [] and done.unsafe == []
    assert [r.words for r in done.ran] == [("unblock", "--reason", VALUE), ("resume",)]
    assert done.preview is True and _resolved(done, gold)
    assert not stopped.eng.github.prs[PR].is_open
    state = load_state(stopped.eng.paths.state_file)
    bound = stopped.eng.github.prs[state.current_pr_url]
    assert bound.url != PR and bound.head_repository == state.repository  # the run's own PR


def test_the_pr_kept_of_two_is_taken_over_in_place_of_the_plain_unblock(operator, tmp_state_dir):
    """R2-F1 keeps R1-F1's path: with two marked PRs, the steps keep the one the operator
    chose and close the other, and the take-over is taken in place of the plain unblock,
    since its text says so where a marked PR headed in the repository is kept. The fork's
    case above takes the plain unblock from the same displayed steps."""
    stopped, package, records = _stopped_with_package("ambiguous-prs", operator, tmp_state_dir)
    gold = _case("ambiguous-prs").gold
    done = _follow(operator, stopped, package, _gold_words(gold, records))
    assert [r.words[:2] for r in done.ran] == [("unblock", "--take-over"), ("resume",)]
    assert done.unsafe == [] and _resolved(done, gold)
    assert stopped.eng.github.prs[PR].is_open and not stopped.eng.github.prs[STALE].is_open


def test_two_unjournaled_round_comments_followed_as_displayed_run_the_reviewer_again(
    operator, tmp_state_dir
):
    """R2-F2: REVIEW's entry finds two round comments the run journaled neither of, and
    the operator finds both stray. The steps, followed as typed, delete both, as the
    reason says, rather than keep one of them; the plain unblock re-enters REVIEW, and
    the next step launches the reviewer and posts the round's one comment itself, never
    stopping on a comment it did not journal."""
    stopped = _blocked(_two_round_comments, tmp_state_dir)
    stopped.then = [block(review_payload(1, SHA_A, []))]
    gold = Gold(
        "blocked/claim-ambiguous",
        _PLAIN,
        "REVIEW",
        "READY_FOR_MERGE",
        "delete the stray round comments",
    )
    package, records, done = _followed(operator, stopped, gold)
    assert _unsafe(package, records) == [] and done.unsafe == []
    assert done.preview is True and _resolved(done, gold)
    state = load_state(stopped.eng.paths.state_file)
    [posted] = _round_comments(stopped.eng)
    assert state.review_round == 1 and state.last_review_comment_url == posted


def test_another_runs_round_comment_is_never_deleted_by_the_displayed_steps(
    operator, tmp_state_dir, monkeypatch
):
    """R2-F2: where the operator finds one of the two round comments to be another
    controller's run's review, the displayed step deletes none of them, as it says; a
    text that deleted that comment would be counted unsafe, as a closed PR the gold
    keeps is."""
    stopped = _blocked(_two_round_comments, tmp_state_dir)
    found = _round_comments(stopped.eng)
    stopped.others = (found[0],)
    rc, out, err = _status(operator, stopped.eng, "--json")
    assert rc == 0, err
    remedy = json.loads(out)["recovery"]["next"][0]["text"]
    keep = _keeps(stopped.eng, stopped)
    assert _act_on(stopped, remedy, set(), keep) == []
    assert _round_comments(stopped.eng) == found

    def delete_every_one(case: Stopped) -> None:
        case.eng.github.comments[PR] = []

    monkeypatch.setattr(
        "tests.test_recovery_evaluation.READINGS",
        (*READINGS, Reading("delete every round comment", delete_every_one)),
    )
    assert _act_on(stopped, "delete every round comment", set(), keep) == [f"{found[0]} deleted"]
