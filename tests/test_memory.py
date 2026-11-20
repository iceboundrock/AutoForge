"""Protocol-20's advisory snapshot and its independent projection."""

import copy
import hashlib
import json
from dataclasses import replace

import pytest

from autoforge.auto_recovery import StepKey
from autoforge.errors import StateError
from autoforge.evidence import Definition, WorktreeProvenance
from autoforge.memory import (
    MAX_EVENTS,
    MAX_FACTS,
    MAX_PROJECTION_BYTES,
    MAX_REASON_BYTES,
    MAX_STORE_BYTES,
    MAX_TEXT_BYTES,
    Action,
    Actor,
    EvidenceRef,
    Kind,
    MemoryScope,
    MemoryStore,
    ProjectionRequest,
    ProposalKey,
    Range,
    Receipt,
    render_projection,
)
from autoforge.redaction import redact
from autoforge.state import AutoForgeState, load_state, save_state
from autoforge.transitions import Phase
from tests.test_evidence import RUN, RUN_LOG, policy, record, remote

ISSUE = "https://github.com/owner/repo/issues/2"
OTHER = "https://github.com/owner/repo/issues/3"
REPO = "owner/repo"
SHA, BASE = "a" * 40, "b" * 40
STEP = StepKey(Phase.ANALYZE_EXECUTE, ISSUE, "", 0, 1)
REQ = ProjectionRequest(RUN, STEP, 17, SHA)
SCOPE = MemoryScope(STEP)


def empty(seq=128):
    return MemoryStore(
        REPO, RUN, seq, dict(enabled=True, run_id=RUN, events=[], texts={}, refusals=0)
    )


def reload(store):
    return MemoryStore(REPO, RUN, store.launch_seq, json.loads(json.dumps(store.encode())))


def proposal(store, text="a hypothesis", *, n=1, scope=SCOPE, key=None, accepted=True):
    return store.add(
        actor=Actor.AGENT,
        kind=Kind.HYPOTHESIS,
        scope=scope,
        attempt=n,
        key=key or ProposalKey("idea", n, "ideator", SHA),
        text=text,
        accepted=accepted,
    )


def apply(out):
    assert out.status == "applied"
    return out.store


def revise(store, action, target, *, actor=Actor.OPERATOR, reason="reviewed", **kwargs):
    return store.revise(action, target, actor=actor, reason=reason, **kwargs)


def state(**kwargs):
    return AutoForgeState(
        run_id=RUN,
        repository=REPO,
        current_issue_url=ISSUE,
        epic_url="https://github.com/owner/repo/issues/1",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        **kwargs,
    )


@pytest.mark.parametrize("phase", list(Phase))
def test_previous_protocol_loads_without_memory_and_keeps_step_watermark(tmp_path, phase):
    path = tmp_path / "state.json"
    data = state(step_count=19, phase=phase).to_dict()
    data["protocol_version"] = "19"
    del data["launch_seq"], data["issue_memory"]
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    loaded = load_state(path)
    assert path.read_bytes() == before
    assert loaded.launch_seq == 19 and not loaded.memory().enabled and loaded.phase is phase
    save_state(loaded, path)
    assert load_state(path).protocol_version == "20"
    loaded.reset_for_new_issue(OTHER)
    assert loaded.launch_seq == 19


@pytest.mark.parametrize("field", ["issue_memory", "launch_seq"])
def test_outer_protocol_label_decides_the_presence_of_each_new_field(field):
    data = state().to_dict()
    del data[field]
    with pytest.raises(StateError, match=field):
        AutoForgeState.from_dict(data)
    data["protocol_version"] = "19"
    with pytest.raises(StateError, match="never wrote"):
        AutoForgeState.from_dict(data)


def test_checkpoint_cannot_adopt_a_store_from_another_run():
    st = apply(proposal(empty()))
    with pytest.raises(StateError):
        MemoryStore(REPO, "another-run", st.launch_seq, st.encode())


def test_proposals_have_immutable_origins_not_payload_identity():
    issue_case = replace(STEP, issue_url=ISSUE.replace("owner/repo", "Owner/Repo"))
    first = apply(proposal(empty(), scope=replace(SCOPE, step=issue_case), n=2))
    second = apply(proposal(first, n=1))
    assert render_projection(second, REQ).ids == (2, 1)
    assert proposal(reload(second), n=2).status == "replay"
    assert proposal(second, n=2, text="changed").status == "conflict"
    assert proposal(second, n=2, scope=replace(SCOPE, candidate=SHA)).status == "conflict"
    assert (
        proposal(
            second,
            n=2,
            scope=replace(SCOPE, step=replace(STEP, pr_url=ISSUE.replace("issues/2", "pull/12"))),
        ).status
        == "conflict"
    )
    assert proposal(second, n=3, accepted=False).status == "refused"
    for key in (
        ProposalKey("another", 2, "ideator", SHA),
        ProposalKey("idea", 2, "solver", SHA),
        ProposalKey("idea", 2, "ideator", BASE),
        ProposalKey("idea", 2, "ideator", SHA, slot=1),
    ):
        second = apply(proposal(second, n=2, key=key))
    withdrawn = apply(revise(second, Action.RETRACT, 1))
    assert proposal(reload(withdrawn), n=2).status == "replay"
    assert proposal(reload(withdrawn), n=7).status == "refused"


@pytest.mark.parametrize("action", [Action.CORRECT, Action.RETRACT])
def test_withdrawals_bind_duplicates_on_write_load_and_render(action):
    exact = replace(SCOPE, candidate=SHA, range=Range.EXACT)
    st = apply(proposal(empty(), scope=exact))
    st = apply(proposal(st, n=2, scope=exact))
    args = {"text": "updated note"} if action is Action.CORRECT else {}
    st = apply(revise(st, action, 1, actor=Actor.AGENT, step=STEP, attempt=2, **args))
    st = reload(st)
    projected = render_projection(st, REQ)
    assert "a hypothesis" not in projected.text and projected.freshness.withdrawn > 0
    assert "No current record applies" not in projected.text
    assert proposal(st, n=3, scope=exact).status == "refused"
    peer = replace(exact, candidate=BASE)
    st = apply(proposal(st, n=4, scope=peer))
    st = apply(proposal(st, n=5, scope=replace(exact, step=replace(STEP, issue_url=OTHER))))
    assert "a hypothesis" in render_projection(reload(st), replace(REQ, candidate=BASE)).text
    altered = st.encode()
    altered["events"][3]["scope"] = exact.encode()
    with pytest.raises(StateError):
        MemoryStore(REPO, RUN, st.launch_seq, altered)


@pytest.mark.parametrize(
    "action,kwargs",
    [
        (Action.RETRACT, {}),
        (Action.CORRECT, {"text": "updated"}),
        (Action.SUPERSEDE, {"replacement": 2}),
    ],
)
def test_agent_requires_issue_scope_and_valid_attempt(action, kwargs):
    st = apply(proposal(empty()))
    st = apply(proposal(st, text="another", n=2))
    for step, attempt in (
        (None, 2),
        (replace(STEP, issue_url=OTHER), 2),
        (STEP, 0),
        (STEP, 129),
        (STEP, True),
    ):
        assert (
            revise(st, action, 1, actor=Actor.AGENT, step=step, attempt=attempt, **kwargs).status
            == "refused"
        )
    ok = apply(revise(st, action, 1, actor=Actor.AGENT, step=STEP, attempt=2, **kwargs))
    data = ok.encode()
    data["events"][-1]["step"]["issue_url"] = OTHER
    with pytest.raises(StateError):
        MemoryStore(REPO, RUN, st.launch_seq, data)


def test_replay_cannot_create_observed_records_and_history_enforces_roles():
    st = apply(proposal(empty()))
    ok = apply(revise(st, Action.RETRACT, 1))
    raw = ok.encode()
    raw["events"][0]["actor"] = "operator"
    with pytest.raises(StateError):
        MemoryStore(REPO, RUN, st.launch_seq, raw)
    assert proposal(st, n=0).status == "refused"
    assert proposal(st, n=129).status == "refused"
    assert (
        proposal(st, scope=replace(SCOPE, step=replace(STEP, review_round=2**40))).status
        == "refused"
    )


def failure(seq=1, *, sha=SHA, issue=2, run_log=RUN_LOG, **kwargs):
    candidate = remote(commit=sha, provenance=WorktreeProvenance(issue, "issue-2", True, BASE))
    return record(seq, candidate=candidate, run_log=run_log, **kwargs)


def observation(st, receipt, *, text="unverified account", scope=None):
    return st.add(
        actor=Actor.CONTROLLER,
        kind=Kind.VALIDATION_FAILURE,
        scope=scope or replace(SCOPE, candidate=SHA, base=BASE),
        text=text,
        attempt=1,
        evidence=EvidenceRef(Receipt.VALIDATION, receipt["seq"], receipt["digest"]),
    )


@pytest.mark.parametrize("case", ["failure", "pass", "candidate", "issue", "run", "evicted"])
def test_only_matching_current_typed_receipts_establish_a_claim(case):
    kwargs = {
        "pass": {"exit_code": 0},
        "candidate": {"sha": BASE},
        "issue": {"issue": 3},
        "run": {"run_log": RUN_LOG.replace(RUN, "another-run")},
    }
    rec = failure(**({"exit_code": 1} | kwargs.get(case, {})))
    st = reload(apply(observation(empty(), rec, text="validation passed despite the receipt")))
    projection = render_projection(st, REQ, validation_records=[] if case == "evicted" else [rec])
    assert ("Observed validation" in projection.text) == (case == "failure")
    assert "unverified controller account" in projection.text
    assert redact(projection.text) == projection.text
    assert (
        render_projection(
            st, replace(REQ, candidate=BASE), validation_records=[rec]
        ).freshness.downgraded
        == 1
    )
    assert revise(st, Action.CORRECT, 1, text="passed").status == "refused"


@pytest.mark.parametrize(
    "kind", ["equivalent", "check", "policy", "timeout", "fail", "launch", "missing"]
)
def test_supersession_compares_claim_identity_after_reload(kind):
    a = failure(exit_code=1, timed_out=(kind == "fail"))
    kwargs = {
        "check": {"definition": Definition(0, ("make", "lint"))},
        "policy": {"pol": policy(timeout_seconds=300)},
        "timeout": {"timed_out": True},
    }
    b = failure(2, exit_code=1, **kwargs.get(kind, {}))
    st = apply(observation(empty(), a))
    if kind == "launch":
        st = apply(
            st.add(
                actor=Actor.CONTROLLER,
                kind=Kind.LAUNCH_END,
                scope=SCOPE,
                attempt=2,
                text="tests did not run",
                evidence=EvidenceRef(Receipt.LAUNCH, 2, "c" * 64),
            )
        )
    else:
        st = apply(observation(st, b, text="a second account"))
    st = reload(apply(revise(st, Action.SUPERSEDE, 1, replacement=2)))
    records = [a] if kind in ("missing", "launch") else [a, b]
    projection = render_projection(st, REQ, validation_records=records)
    assert (1 not in projection.ids) == (kind == "equivalent")
    assert 2 in projection.ids
    if kind == "equivalent":
        assert render_projection(st, REQ, validation_records=[a]).ids[0] == 1
    assert (
        revise(st, Action.RETRACT, 1, actor=Actor.AGENT, step=STEP, attempt=5).status == "refused"
    )


def test_supersession_chain_survives_intermediate_but_not_terminal_withdrawal():
    records = [failure(i, exit_code=1) for i in (1, 2, 3)]
    st = empty()
    for i, rec in enumerate(records):
        st = apply(observation(st, rec, text=f"account {i}"))
    st = apply(revise(st, Action.SUPERSEDE, 1, replacement=2))
    st = apply(revise(st, Action.SUPERSEDE, 2, replacement=3))
    mid = reload(apply(revise(st, Action.RETRACT, 2)))
    end = reload(apply(revise(st, Action.RETRACT, 3)))
    assert render_projection(mid, REQ, validation_records=records).ids == (3,)
    assert render_projection(end, REQ, validation_records=records).ids == (2,)
    a = apply(proposal(empty(), scope=replace(SCOPE, range=Range.EXACT, candidate=SHA)))
    b = apply(proposal(a, n=2, scope=replace(SCOPE, range=Range.EXACT, candidate=BASE)))
    assert revise(b, Action.SUPERSEDE, 1, replacement=2).status == "refused"


def test_projection_has_one_scope_and_no_ancestry_without_candidate():
    st = reload(apply(proposal(empty(), scope=replace(SCOPE, range=Range.DESCENDANTS, base=BASE))))
    for req in (
        replace(REQ, run_id="another-run"),
        replace(REQ, step=replace(STEP, issue_url=ISSUE.replace("owner", "another"))),
        replace(REQ, step=replace(STEP, phase=Phase.REVIEW)),
        replace(REQ, candidate="", ancestors=(BASE,)),
        replace(REQ, max_chars=100_000),
        replace(REQ, max_bytes=100_000),
    ):
        with pytest.raises(StateError):
            render_projection(st, req)
    with pytest.raises(StateError):
        render_projection(MemoryStore.empty(REPO, RUN, 1), REQ)
    assert render_projection(st, replace(REQ, ancestors=(BASE,))).ids == (1,)
    assert render_projection(st, REQ).freshness.inapplicable == 1
    assert "No current record applies" in render_projection(st, replace(REQ, candidate="")).text
    assert render_projection(st, replace(REQ, step=replace(STEP, issue_url=OTHER))).ids == ()


def test_projection_budgets_exact_final_counts_and_distinguishes_empty_reasons():
    st = apply(proposal(empty(), text="short"))
    for i in range(2, 11):
        st = apply(proposal(st, n=i, text=chr(0x1F680 + i) * (MAX_TEXT_BYTES // 4)))
    st = reload(st)
    partial = render_projection(st, replace(REQ, max_bytes=1800))
    assert partial.ids == (1,) and partial.freshness.omitted == 9
    exact = replace(REQ, max_chars=len(partial.text), max_bytes=len(partial.text.encode()))
    assert render_projection(st, exact) == partial
    nothing = render_projection(st, replace(exact, max_bytes=exact.max_bytes - 1))
    assert not nothing.ids and "omitted for the budget" in nothing.text
    assert "No current record applies" not in nothing.text
    full = render_projection(st, REQ)
    assert len(full.text.encode()) <= MAX_PROJECTION_BYTES
    assert len(full.text) < len(full.text.encode())
    assert render_projection(empty(), replace(REQ, max_bytes=700)).ids == ()


def test_strict_load_redaction_even_when_a_content_digest_is_consistent():
    candidate = apply(proposal(empty()))
    raw = candidate.encode()
    text = "GH_" + 'TOKEN="fixture only"'
    assert redact(text) != text
    digest = hashlib.sha256(text.encode()).hexdigest()
    raw["texts"] = {digest: text}
    raw["events"][0]["content"] = digest
    with pytest.raises(StateError, match="invalid Issue Working Memory data"):
        MemoryStore(REPO, RUN, 128, raw)
    # The shape is legal in a GitHub repository name but not in persisted metadata.
    name = "gh" + "p_" + "A" * 36
    for field in ("issue_url", "pr_url"):
        bad = candidate.encode()
        step = bad["events"][0]["scope"]["step"]
        step[field] = (ISSUE if field == "issue_url" else ISSUE.replace("issues", "pull")).replace(
            "repo", name
        )
        with pytest.raises(StateError) as exc:
            MemoryStore(REPO, RUN, 128, bad)
        assert name not in str(exc.value)
        assert (
            proposal(
                candidate, n=2, scope=replace(SCOPE, step=replace(STEP, **{field: step[field]}))
            ).status
            == "refused"
        )


@pytest.mark.parametrize("short", [False, True])
def test_redacting_then_formatting_still_yields_a_loadable_reason_and_observation(short):
    if short:
        original = "https://pass" + "word=/" + "fixture_value" + chr(64) + "example.test"
        once = redact(original)
        assert once != original and redact(once) != once
    else:
        suffix = "\n[omitted]"
        assignment = " GH_" + "TOKEN="
        retained = "x" * (MAX_REASON_BYTES - len(suffix) - len(assignment)) + assignment
        original = retained + "\n```" + "x" * 255
        assert redact(original) == original and redact(retained + suffix) != retained + suffix
    st = apply(proposal(empty()))
    st = apply(observation(st, failure(exit_code=1), text=original if short else "x" * 3000))
    st = reload(apply(revise(st, Action.RETRACT, 1, reason=original)))
    assert 1 not in render_projection(st, REQ).ids
    reason = st.encode()["events"][-1]["reason"]
    assert len(reason.encode()) <= MAX_REASON_BYTES and redact(reason) == reason


def test_full_history_refuses_without_eviction_or_an_unloadable_counter(tmp_path):
    st = empty()
    for i in range(MAX_FACTS):
        st = apply(proposal(st, n=i + 1, text=chr(0x1F680 + i) * (MAX_TEXT_BYTES // 4)))
    refused = proposal(st, n=MAX_FACTS + 1, text="new")
    assert refused.status == "refused" and refused.store.refusals == 1
    st = refused.store
    for i in range(MAX_EVENTS - MAX_FACTS):
        st = apply(revise(st, Action.CORRECT, 1, text=f"revision {i}", reason="long " * 100))
    refused = revise(st, Action.RETRACT, 1)
    assert refused.status == "refused" and refused.store.refusals == 2
    assert len(refused.store.encode()["events"]) == MAX_EVENTS
    path = tmp_path / "state.json"
    save_state(state(issue_memory=refused.store.encode(), launch_seq=128), path)
    assert load_state(path).memory().encode() == refused.store.encode()
    assert len(json.dumps(refused.store.encode(), indent=2).encode()) <= MAX_STORE_BYTES
    altered = copy.deepcopy(st.encode())
    altered["schema"] = True  # No independent nested schema label is accepted.
    with pytest.raises(StateError):
        MemoryStore(REPO, RUN, 128, altered)
