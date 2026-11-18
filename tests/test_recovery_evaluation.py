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
command that leaves the stop, and the phase the controller must read back
after it. It is then performed. The package's displayed steps run as an
operator would type them, placeholders filled, and the state the controller
saved is read back. No agent's or model's claim is evidence of anything here.

Nothing launches a model, and nothing is written outside ``tmp_path``. Every
``status`` the harness runs is held to launching no agent, sending nothing
to GitHub and leaving the state directory as it was, byte for byte.
"""

from __future__ import annotations

import ast
import copy
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
from autoforge.effects import EffectKind, EffectRecord, Stage
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
)
from tests.test_recovery_package import (
    _ambiguous,
    _checks_refused,
    _false_identity_conflict,
    _k9_blocked,
    _k10_blocked,
    _k12_blocked,
)
from tests.test_stop_classification import _stopping

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "recovery-mvp-evaluation.md"
MATRIX = "## 4. The matrix"
SUMMARY = "## 5. What the matrix shows"
SAFETY = "## 6. The safety regressions"

#: A value the operator supplies: an unblock's reason, a comment's URL.
VALUE = "<value>"
#: How the package and the saved reasons spell such a value.
_PLACEHOLDERS = ("...", "<why>", "<comment URL>")
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
    read back right after that command, and ``act`` what the operator does
    first, outside AutoForge.
    """

    stop: str
    command: str
    target: str
    act: str = ""


@dataclass
class Stopped:
    """A case after its stop, as the operator finds it.

    ``line`` is what the stopping command's ``stop:`` line prints (``None``
    for no line: a process that died, or a run that did not stop); ``said``
    the text that command printed or the state saved for a human; ``act``
    performs the gold's act; ``then`` is the agent that works once the run
    goes on; ``steps`` holds the outcomes of the steps the controller ran
    with no operator command, for a case that recovers by itself.
    """

    eng: ControllerEngine
    line: StopClassification | None
    said: str
    act: Callable[[], None] | None = None
    then: Callable | list | None = None
    steps: list = field(default_factory=list)


def _blocked(build: Callable[[Path], ControllerEngine], tmp: Path) -> Stopped:
    """A scenario whose last step stopped in BLOCKED or FAILED: that step's stop line,
    which the CLI prints, and the reason the stop saved."""
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
    (ADR 0012 D3's adoption row, K11). The controller never chooses."""
    stopped = _blocked(_ambiguous, tmp)

    def close_the_stale_one() -> None:
        stopped.eng.github.prs[STALE].state = "CLOSED"

    stopped.act = close_the_stale_one
    return stopped


def _bound(tmp: Path, key: str, value: int, raised: int) -> Stopped:
    gh = FakeGitHub()
    eng = make_engine(tmp, _loop_agent(gh, _one_finding_per_round), github=gh, origin=True)
    setattr(eng.config.workflow, key, value)
    _no_stagnation(eng.config)
    eng._save()
    outcomes = eng.run(max_steps=50)
    assert outcomes[-1].next_phase == "BLOCKED", outcomes[-1].message
    stopped = Stopped(eng, outcomes[-1].stop, eng.state.block_reason, then=eng.provider._handler)

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
        Gold("blocked/operator-actions", _PLAIN, "FIX", "post each payload on its issue"),
    ),
    Case(
        "k9-acceptance-action",
        _k9,
        Gold(
            "blocked/acceptance-action",
            f"unblock --approve-action {{action}} --reason {VALUE}",
            "FIX",
        ),
    ),
    Case(
        "k12-pr-body-update",
        _k12,
        Gold(
            "blocked/acceptance-action",
            f"unblock --approve-action {{action}} --reason {VALUE}",
            "FIX",
        ),
    ),
    Case(
        "checks-refused-291",
        _checks_291,
        Gold(
            "blocked/github-checks-refused",
            _PLAIN,
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
        Gold("blocked/effect-conflict", _PLAIN, "ANALYZE_EXECUTE"),
    ),
    Case(
        "ambiguous-prs",
        _ambiguous_prs,
        Gold(
            "blocked/claim-ambiguous",
            f"unblock --take-over {PR} --reason {VALUE}",
            "ANALYZE_EXECUTE",
            "close the stale PR",
        ),
    ),
    Case(
        "review-round-cap",
        _review_cap,
        Gold("blocked/review-round-cap", _PLAIN, "FIX", "raise workflow.max_review_rounds"),
    ),
    Case(
        "step-budget",
        _step_budget,
        Gold("blocked/step-budget", _PLAIN, "REVIEW", "raise workflow.max_total_steps"),
    ),
    Case(
        "no-saved-code",
        _no_code,
        Gold("blocked/review-round-cap", _PLAIN, "FIX", "raise workflow.max_review_rounds"),
    ),
    Case(
        "agent-blocked",
        _agent_stop("blocked"),
        Gold("blocked/agent-blocked", _PLAIN, "ANALYZE_EXECUTE", "answer the agent's question"),
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


# -- the after arm's steps: what is unsafe, and what following them does -----------------------

#: The flags of ``unblock`` that name a saved action, and every flag it may be shown with.
_ACTION_FLAGS = ("--approve-action", "--decline-action", "--bind-evidence")
_UNBLOCK_FLAGS = (*_ACTION_FLAGS, "--reason", "--take-over", "--rereview")


def _unsafe(package: dict, records: list[EffectRecord]) -> list[str]:
    """The displayed steps ADR 0012 D2 denies for the stop, or D3 grants nobody.

    A ``resume`` or ``step`` before the run has left BLOCKED, any ``unblock``
    of a run that is not BLOCKED (an alternative step stands in place of the
    steps before it, so it meets the run as it stopped), an approval of an action that is not
    awaiting the operator's approval, a decline or a bind of an action the
    plan does not hold, an open merge gate, an accepted configuration change,
    and any command that is not the controller's or the documented ``gh``
    pipe of an operator action.
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


@dataclass
class Followed:
    """What following the displayed steps did, as the controller saved it."""

    gold: str = ""  # the phase read back right after the gold command, "" if never run
    after: str = ""  # the phase and saved code after the first step that followed it
    preview: bool | None = None  # a plain unblock's live preview matched its decision


def _phase(eng: ControllerEngine) -> str:
    return load_state(eng.paths.state_file).phase.value


def _post_payload(operator, eng: ControllerEngine, words: list[str]) -> None:
    """``status --action ID | gh issue comment TARGET --body-file -``: the operator's post."""
    pipe = words.index("|")
    rc, out, err = operator(eng, *words[:pipe])
    assert rc == 0, err
    target = words[pipe + 4]
    posted = sum(len(comments) for comments in eng.github.comments.values())
    eng.github.add_comment(target, 9000 + posted, out)


def _follow(operator, stopped: Stopped, package: dict, gold: list[str]) -> Followed:
    """Run the package's displayed steps in order, as the operator would type them.

    A step with no command is the operator's own act, performed once. A
    ``doctor`` is not run: it checks this host's environment, which a case
    does not stand in for. The steps end with the first command that runs
    the workflow (``resume`` or ``run``), given ``--max-steps 1`` so that the
    step right after the stop is the one read back.
    """
    eng = stopped.eng
    done = Followed()
    acted = False
    for step in package["next"]:
        if step["alternative"]:
            continue
        words = _words(step["command"])
        if not words:
            if stopped.act is not None and not acted:
                stopped.act()
                acted = True
            continue
        if "|" in words:
            _post_payload(operator, eng, words)
        elif words[0] == "status":
            rc, _, err = operator(eng, *words)
            assert rc == 0, err
        elif words[0] == "unblock":
            live = None
            if words == ["unblock", "--reason", VALUE]:
                rc, out, err = operator(eng, "status", "--json", "--live")
                assert rc == 0, err
                live = json.loads(out)["recovery"]["live"]
            filled = [_FILLED if w == VALUE else w for w in words]
            operator(eng, *filled)
            phase = _phase(eng)
            if live is not None:
                done.preview = (live["target_phase"] or "BLOCKED") == phase
            if words == gold:
                done.gold = phase
        elif words[0] in ("resume", "run"):
            operator(eng, *words, "--max-steps", "1", agent=stopped.then)
            state = load_state(eng.paths.state_file)
            reached = (
                f"{state.phase.value}/{state.stop_code}" if state.stop_code else state.phase.value
            )
            if words == gold:
                done.gold = state.phase.value
            else:
                done.after = reached
            break
    return done


def _kept(eng: ControllerEngine, package: dict, records: list[EffectRecord], end: str) -> str:
    """Whether the candidate and the saved plan survived: named as not to be touched at
    the stop, the candidate still a commit of the repository once the steps were
    followed, and the run's PR at it when the run went on."""
    if not records:
        return NONE
    preserve = package["preserve"]
    texts = " ".join(c["text"] for c in preserve)
    pushes = [r for r in records if r.kind is EffectKind.PUSH]
    candidate = pushes[0].payload["sha"] if pushes else ""
    named = "journal" in {c["key"] for c in preserve} and (not candidate or candidate in texts)
    if candidate:
        exists = subprocess.run(
            ["git", "-C", str(eng.workdir), "cat-file", "-e", f"{candidate}^{{commit}}"],
            capture_output=True,
        )
        named = named and exists.returncode == 0
        state = load_state(eng.paths.state_file)
        if end not in ("", "BLOCKED", "FAILED") and not end.startswith(("BLOCKED/", "FAILED/")):
            named = named and state.current_head_sha == candidate
    return "yes" if named else "no"


# -- one row of the matrix ---------------------------------------------------------------------

#: The columns the harness computes, in the document's order; the others are annotations.
MEASURED = (
    "Gold stop",
    "Gold resolution",
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
    command = " ".join(_SHOWN.get(word, word) for word in words)
    return f"{gold.act}, then `{command}`" if gold.act else f"`{command}`"


def _perform(operator, stopped: Stopped, gold: Gold, words: list[str]) -> tuple[str, str]:
    """The gold resolution itself: the act, the command, and the step after an unblock.
    The phase after the command, and the phase (and saved code) after that step."""
    eng = stopped.eng
    if stopped.act is not None:
        stopped.act()
    filled = [_FILLED if w == VALUE else w for w in words]
    if words[0] == "unblock":
        operator(eng, *filled)
        phase = _phase(eng)
        if phase in ("BLOCKED", "FAILED"):
            return phase, ""
        operator(eng, "resume", "--max-steps", "1", agent=stopped.then)
    else:
        operator(eng, *filled, "--max-steps", "1", agent=stopped.then)
        phase = _phase(eng)
    state = load_state(eng.paths.state_file)
    return phase, f"{state.phase.value}/{state.stop_code}" if state.stop_code else state.phase.value


@dataclass
class Measured:
    row: dict[str, str]
    status_cpu_seconds: float | None = None
    unsafe: list[str] = field(default_factory=list)


def _measure(case: Case, operator, fresh: Callable[[], Path]) -> Measured:
    """Build the case, read both arms, follow the package, and perform the gold."""
    gold = case.gold
    stopped = case.build(fresh())
    eng = stopped.eng
    row = dict.fromkeys(MEASURED, NONE)
    row["Gold stop"] = gold.stop
    row["Gold resolution"] = _gold_text(gold)
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
        reached, _ = _perform(operator, stopped, gold, words)
        assert reached == gold.target, f"the gold resolution does not hold: {reached}"
        return measured

    package = json.loads(out)["recovery"]
    row["After: later status"] = f"{package['outcome']}/{package['code']}"
    row["After: command"] = _yes(any(_words(s["command"]) == words for s in package["next"]))
    measured.unsafe = _unsafe(package, records)
    row["After: unsafe"] = str(len(measured.unsafe))
    done = _follow(operator, stopped, package, words)
    resolved = done.gold == gold.target and not done.after.startswith(("BLOCKED", "FAILED"))
    row["After: steps resolve"] = _yes(resolved)
    if done.preview is not None:
        row["After: preview"] = _yes(done.preview)
    row["Plan kept"] = _kept(eng, package, records, done.after or done.gold)
    if not done.gold:
        # The displayed steps never ran the gold command: perform it on a fresh copy,
        # so that every gold label is held by the controller's own read-back.
        again = case.build(fresh())
        reached, after = _perform(operator, again, gold, words)
        assert reached == gold.target, f"the gold resolution does not hold: {reached}"
        assert not after.startswith(("BLOCKED", "FAILED")), after
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
    ``status_cpu_seconds`` property (``--junitxml`` reports it); it is a
    measurement of this host, never a bound.
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
        "Unsafe displayed steps": ("not measured", f"{unsafe} in {len(packages)} packages"),
        "The displayed steps, followed as typed, resolve the stop": (
            NONE,
            of(_count(packages, "After: steps resolve"), packages),
        ),
        "A plain `unblock`'s live preview was its decision": (
            NONE,
            of(_count(previews, "After: preview"), previews),
        ),
        "The saved plan and its candidate were named and kept": (
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
