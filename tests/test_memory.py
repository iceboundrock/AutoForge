"""Protocol-20's advisory snapshot and its independent projection."""

import hashlib
import json
import os
from dataclasses import replace

import pytest

import autoforge.executor as executor_module
from autoforge.auto_recovery import StepKey
from autoforge.config import MemoryConfig, ProfileConfig, default_config
from autoforge.errors import (
    ConfigurationError,
    ControlResultValidationError,
    SandboxLaunchRefusedError,
    StateError,
)
from autoforge.evidence import Definition, WorktreeProvenance
from autoforge.memory import (
    COMPACT_SLACK_BYTES,
    MAX_EVENTS,
    MAX_FACTS,
    MAX_PROJECTION_BYTES,
    MAX_REASON_BYTES,
    MAX_STORE_BYTES,
    MAX_TEXT_BYTES,
    RETRACTION_RESERVE_BYTES,
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
    Retention,
    render_projection,
)
from autoforge.providers import (
    AgentRequest,
    ClaudeCodeProvider,
    FreshContextHost,
    PiProvider,
    ProviderRegistry,
    ScriptedProvider,
)
from autoforge.redaction import redact
from autoforge.sandbox import Access, Bind, LaunchDescription, PrivateTmpfs
from autoforge.sandbox_policy import ProviderRequirements
from autoforge.state import AutoForgeState, load_state, save_state
from autoforge.transitions import Phase
from tests.conftest import connect_origin, make_engine
from tests.test_evidence import RUN, RUN_LOG, policy, record, remote

ISSUE = "https://github.com/owner/repo/issues/2"
OTHER = "https://github.com/owner/repo/issues/3"
REPO = "owner/repo"
SHA, BASE = "a" * 40, "b" * 40
STEP = StepKey(Phase.ANALYZE_EXECUTE, ISSUE, "", 0, 1)
# The next launch of a store at launch_seq 128 (``empty()``).
REQ = ProjectionRequest(RUN, STEP, 129, SHA)
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
    kwargs.setdefault("issue", ISSUE)
    if actor is Actor.AGENT:
        # An accepted result's revision, made against the record as it is now.
        kwargs.setdefault("accepted", True)
        fact = store._facts.get(target)
        kwargs.setdefault("expected", fact.revision if fact is not None else 1)
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
    # R2-F7 of PR #526: each citation's typed retention result.
    retention = {
        "failure": Retention.ESTABLISHED,
        "run": Retention.UNAVAILABLE,
        "evicted": Retention.UNAVAILABLE,
    }.get(case, Retention.RETAINED)
    assert projection.retention == ((1, retention),)
    assert projection.freshness.downgraded == (retention is not Retention.ESTABLISHED)
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


def test_an_established_chain_keeps_its_claim_shown_through_withdrawals():
    records = [failure(i, exit_code=1) for i in (1, 2, 3)]
    st = empty()
    for i, rec in enumerate(records):
        st = apply(observation(st, rec, text=f"account {i}"))
    st = apply(revise(st, Action.SUPERSEDE, 1, replacement=2))
    st = apply(revise(st, Action.SUPERSEDE, 2, replacement=3))
    mid = reload(apply(revise(st, Action.RETRACT, 2)))
    end = reload(apply(revise(st, Action.RETRACT, 3)))
    assert render_projection(mid, REQ, validation_records=records).ids == (3,)
    # No shown record carries the claim M2's own receipt establishes, so M2
    # stays shown, marked as superseded; M1's claim is M2's and stays hidden.
    shown = render_projection(end, REQ, validation_records=records)
    assert shown.ids == (2,) and "Superseded by M3; shown because" in shown.text
    # Without its receipt M2 establishes nothing, and its supersession holds;
    # M1's receipt then establishes a claim no shown record carries.
    assert render_projection(end, REQ, validation_records=records[:1]).ids == (1,)
    assert render_projection(end, REQ).ids == ()
    a = apply(proposal(empty(), scope=replace(SCOPE, range=Range.EXACT, candidate=SHA)))
    b = apply(proposal(a, n=2, scope=replace(SCOPE, range=Range.EXACT, candidate=BASE)))
    assert revise(b, Action.SUPERSEDE, 1, replacement=2).status == "refused"


@pytest.mark.parametrize("withdrawn", [2, 3])
def test_a_superseded_proposal_stays_superseded_when_its_replacement_goes(withdrawn):
    st = empty()
    for n in (1, 2, 3):
        st = apply(proposal(st, text=f"proposal {n}", n=n))
    pair = apply(revise(st, Action.SUPERSEDE, 1, replacement=2))
    pair = reload(apply(revise(pair, Action.RETRACT, 2)))
    projected = render_projection(pair, REQ)
    assert 1 not in projected.ids and "proposal 1" not in projected.text
    assert [r["status"] for r in pair.catalog(ISSUE)["records"][:2]] == ["superseded", "retracted"]
    chain = apply(revise(st, Action.SUPERSEDE, 1, replacement=2))
    chain = apply(revise(chain, Action.SUPERSEDE, 2, replacement=3))
    chain = reload(apply(revise(chain, Action.RETRACT, withdrawn)))
    projected = render_projection(chain, REQ)
    assert projected.ids == (() if withdrawn == 3 else (3,))
    assert "proposal 1" not in projected.text and "proposal 2" not in projected.text
    assert [r["status"] for r in chain.catalog(ISSUE)["records"]] == [
        "superseded",
        "retracted" if withdrawn == 2 else "superseded",
        "retracted" if withdrawn == 3 else "current",
    ]


def test_a_superseded_proposal_stays_superseded_when_its_receipt_is_evicted():
    rec = failure(exit_code=1)
    st = apply(proposal(empty(), text="the suite passes"))
    st = apply(observation(st, rec, text="the suite failed"))
    st = reload(apply(revise(st, Action.SUPERSEDE, 1, replacement=2)))
    for receipts in ([rec], []):
        projected = render_projection(st, REQ, validation_records=receipts)
        assert projected.ids == (2,) and "the suite passes" not in projected.text
    assert render_projection(st, REQ).freshness.downgraded == 1


def test_an_established_observation_is_not_hidden_by_a_replacement_without_its_claim():
    rec = failure(exit_code=1)
    st = apply(observation(empty(), rec, text="the suite failed"))
    st = st.add(
        actor=Actor.CONTROLLER,
        kind=Kind.LAUNCH_END,
        scope=SCOPE,
        attempt=2,
        text="the suite did not run",
        evidence=EvidenceRef(Receipt.LAUNCH, 2, "c" * 64),
    )
    st = apply(revise(apply(st), Action.SUPERSEDE, 1, replacement=2))
    for current in (st, apply(revise(st, Action.RETRACT, 2))):
        projected = render_projection(reload(current), REQ, validation_records=[rec])
        assert 1 in projected.ids and "Superseded by M2" in projected.text
        # The receipt gone, the observation establishes nothing and stays superseded.
        assert 1 not in render_projection(reload(current), REQ).ids


def test_an_operator_revision_acts_only_on_the_issue_it_names():
    st = apply(proposal(empty()))
    st = apply(proposal(st, n=2, scope=replace(SCOPE, step=replace(STEP, issue_url=OTHER))))
    for action, kwargs in ((Action.RETRACT, {}), (Action.CORRECT, {"text": "new"})):
        foreign = revise(st, action, 1, issue=OTHER, **kwargs)
        assert foreign.status == "foreign" and foreign.store is st
        assert revise(st, action, 2, issue=ISSUE, **kwargs).status == "foreign"
    # The operator's row names no step, so its issue is not optional.
    refused = revise(st, Action.RETRACT, 1, issue=None)
    assert refused.status == "refused" and refused.store.refusals == 1
    done = reload(apply(revise(st, Action.RETRACT, 2, issue=OTHER)))
    assert [r["status"] for r in done.catalog(OTHER)["records"]] == ["retracted"]
    assert [r["status"] for r in done.catalog(ISSUE)["records"]] == ["current"]


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


def test_a_record_past_the_count_is_refused_and_counted():
    st = empty()
    for n in range(1, MAX_FACTS + 1):
        st = apply(proposal(st, text=f"note {n}", n=n))
    refused = proposal(st, n=MAX_FACTS + 1, text="one more")
    assert refused.status == "refused" and refused.store.refusals == 1
    assert len(reload(refused.store).encode()["events"]) == MAX_FACTS


def fill(st, write):
    """Apply ``write(st, i)`` until the store refuses one; the last store and that refusal."""
    for i in range(MAX_EVENTS + 1):
        out = write(st, i)
        if out.status != "applied":
            return st, out
        st = out.store
    raise AssertionError("the store never refused an ordinary write")


def checkpoint_bytes(st):
    # As the state file renders the field: two more spaces on each line.
    rendered = json.dumps(st.encode(), indent=2, sort_keys=True)
    return len(rendered) + 2 * rendered.count("\n")


def withdrawn_everywhere(st, tmp_path, texts):
    st = reload(st)
    path = tmp_path / "state.json"
    save_state(state(issue_memory=st.encode(), launch_seq=st.launch_seq), path)
    assert load_state(path).memory().encode() == st.encode()
    assert checkpoint_bytes(st) <= MAX_STORE_BYTES
    projected = render_projection(st, REQ)
    for text in texts:
        assert text not in projected.text and text not in path.read_text()
    return st, projected


def test_an_operator_retraction_fits_once_ordinary_writes_fill_the_events(tmp_path):
    st = apply(proposal(empty(), text="a wrong lesson"))
    st = apply(proposal(st, n=2, text="filler"))
    st, refused = fill(st, lambda s, i: revise(s, Action.CORRECT, 2, text=f"filler {i}"))
    assert refused.status == "refused" and refused.store.refusals == 1
    # Each record not yet retracted keeps one event slot for its retraction.
    assert len(st.encode()["events"]) == MAX_EVENTS - 2
    for other in (
        proposal(st, n=3, text="new"),
        revise(st, Action.SUPERSEDE, 1, replacement=2),
        revise(st, Action.CORRECT, 1, text="corrected"),
    ):
        assert other.status == "refused"
    st = apply(revise(refused.store, Action.RETRACT, 1, reason="the lesson is wrong"))
    st, projected = withdrawn_everywhere(st, tmp_path, ["a wrong lesson"])
    assert projected.freshness.withdrawn == 1 and projected.ids == (2,)
    assert [r["status"] for r in st.catalog(ISSUE)["records"]] == ["retracted", "current"]
    st = apply(revise(st, Action.RETRACT, 2))
    st, projected = withdrawn_everywhere(st, tmp_path, ["a wrong lesson", "filler"])
    assert len(st.encode()["events"]) == MAX_EVENTS and projected.ids == ()
    assert "Applicable text withheld by withdrawal" in projected.text
    assert revise(st, Action.RETRACT, 2).status == "refused"
    # A hand edit that spends a retraction's slot on another write is refused.
    raw = reload(refused.store).encode()
    event = dict(raw["events"][-1], value=hashlib.sha256(b"spent").hexdigest())
    raw["texts"] = {k: v for k, v in raw["texts"].items() if k != raw["events"][-1]["value"]}
    raw["texts"][event["value"]] = "spent"
    raw["events"].append(event)
    with pytest.raises(StateError):
        MemoryStore(REPO, RUN, 128, raw)
    raw["events"].pop()
    raw["schema"] = True  # No independent nested schema label is accepted.
    with pytest.raises(StateError):
        MemoryStore(REPO, RUN, 128, raw)


def test_an_operator_retraction_fits_once_ordinary_writes_fill_the_bytes(tmp_path):
    # Every byte of these texts and reasons escapes to six characters. The
    # last records repeat earlier texts, so retracting them frees no text.
    worst = "\x01" * MAX_REASON_BYTES
    unique = [f"{i:02d}" + "\x01" * (MAX_TEXT_BYTES - 2) for i in range(MAX_FACTS - 8)]
    st = empty()
    for i, text in enumerate(unique + unique[:8]):
        st = apply(proposal(st, n=i + 1, text=text))
    st, refused = fill(st, lambda s, i: revise(s, Action.CORRECT, 1, text=f"{i}", reason=worst))
    events = len(st.encode()["events"])
    assert refused.status == "refused" and events + MAX_FACTS < MAX_EVENTS  # Bytes, not count.
    st = refused.store
    for target in range(MAX_FACTS, MAX_FACTS - 8, -1):
        st = reload(apply(revise(st, Action.RETRACT, target, reason=worst)))
    # These retractions took bytes that no ordinary write was allowed.
    assert checkpoint_bytes(st) > MAX_STORE_BYTES - MAX_FACTS * RETRACTION_RESERVE_BYTES
    for target in range(MAX_FACTS - 8, 0, -1):
        st = reload(apply(revise(st, Action.RETRACT, target, reason=worst)))
    st, projected = withdrawn_everywhere(st, tmp_path, [])
    assert len(st.encode()["events"]) == events + MAX_FACTS and projected.ids == ()
    assert st.encode()["texts"] == {}
    assert all(r["status"] == "retracted" for r in st.catalog(ISSUE)["records"])


def correct(store, target, text, *, attempt, expected, accepted=True):
    return store.revise(
        Action.CORRECT,
        target,
        actor=Actor.AGENT,
        reason="corrected",
        step=STEP,
        attempt=attempt,
        text=text,
        expected=expected,
        accepted=accepted,
    )


def test_a_late_or_stale_agent_revision_is_refused_and_the_newer_one_stays():
    """R2-F3 of PR #526: attempt 3 corrects M1, then attempt 2's correction,
    made against the revision attempt 3 replaced, arrives. It is refused,
    as is every stale revision, and M1 reloads with attempt 3's text."""
    # Concurrent proposals: distinct keys, admitted in any order.
    st = apply(proposal(empty(), text="first account", n=1))
    st = apply(proposal(st, text="attempt three's idea", n=3))
    st = apply(proposal(st, text="attempt two's idea", n=2))
    st = reload(apply(proposal(st, text="attempt five's idea", n=5)))
    newer = reload(apply(correct(st, 1, "attempt three", attempt=3, expected=1)))
    assert newer.catalog(ISSUE)["records"][0]["revision"] == 5
    stale = [
        correct(newer, 1, "attempt two", attempt=2, expected=1),
        # Naming the current revision does not make an earlier launch current.
        correct(newer, 1, "attempt two", attempt=2, expected=5),
        correct(newer, 1, "attempt four", attempt=4, expected=1),
        correct(newer, 1, "attempt four", attempt=4, expected=5, accepted=False),
        newer.revise(
            Action.CORRECT,
            1,
            actor=Actor.AGENT,
            reason="corrected",
            step=STEP,
            attempt=4,
            text="no revision named",
            accepted=True,
        ),
        revise(newer, Action.RETRACT, 1, actor=Actor.AGENT, step=STEP, attempt=2, expected=1),
        revise(newer, Action.RETRACT, 1, actor=Actor.AGENT, step=STEP, attempt=2, expected=5),
        revise(newer, Action.SUPERSEDE, 1, actor=Actor.AGENT, step=STEP, attempt=2, replacement=3),
        # A replacement a later launch added than the one superseding with it.
        revise(newer, Action.SUPERSEDE, 1, actor=Actor.AGENT, step=STEP, attempt=4, replacement=4),
    ]
    for out in stale:
        assert out.status == "refused"
        assert out.store.encode() == dict(newer.encode(), refusals=1)
        text = render_projection(reload(out.store), REQ).text
        assert "attempt three\n" in text and "attempt two\n" not in text
    # Replay of the applied correction changes nothing and counts nothing.
    again = correct(newer, 1, "attempt three", attempt=3, expected=1)
    assert again.status == "replay" and again.store is newer
    # The same rules hold on load: the stale row, written by hand, is refused.
    for attempt, expected in ((2, 1), (2, 5), (4, 1)):
        raw = newer.encode()
        digest = hashlib.sha256(b"attempt two").hexdigest()
        raw["events"].append(
            dict(
                action="correct",
                actor="agent",
                step=STEP.to_dict(),
                attempt=attempt,
                target=1,
                expected=expected,
                reason="late",
                value=digest,
            )
        )
        raw["texts"] = {k: v for k, v in raw["texts"].items() if v != "attempt three"} | {
            digest: "attempt two"
        }
        with pytest.raises(StateError):
            MemoryStore(REPO, RUN, newer.launch_seq, raw)
    # A current revision applies, and the operator's retraction acts on it.
    latest = reload(apply(correct(newer, 1, "attempt four", attempt=4, expected=5)))
    assert "attempt four\n" in render_projection(latest, REQ).text
    assert revise(latest, Action.RETRACT, 1, expected=5).status == "refused"
    done = reload(apply(revise(latest, Action.RETRACT, 1)))
    assert done.catalog(ISSUE)["records"][0]["status"] == "retracted"


def test_a_replacement_the_budget_cuts_does_not_hide_the_claim_it_carries():
    """R2-F4 of PR #526: the short original of two equivalent observations fits
    the budget, its long replacement does not, and the claim stays shown."""
    a, b = failure(1, exit_code=1), failure(2, exit_code=1)
    st = apply(observation(empty(), a, text="the suite failed"))
    st = apply(observation(st, b, text="x" * MAX_TEXT_BYTES))
    budget = replace(REQ, max_bytes=1400)
    before = render_projection(reload(st), budget, validation_records=[a, b])
    assert before.ids == (1,) and before.freshness.omitted == 1
    st = reload(apply(revise(st, Action.SUPERSEDE, 1, replacement=2)))
    cut = render_projection(st, budget, validation_records=[a, b])
    assert cut.ids == (1,) and cut.freshness.omitted == 1
    assert "Superseded by M2; shown because no record shown here" in cut.text
    assert cut.retention == ((1, Retention.ESTABLISHED),)
    # With room for the replacement, it carries the claim, and M1 stays hidden.
    assert render_projection(st, REQ, validation_records=[a, b]).ids == (2,)
    # A withdrawn replacement carries nothing either (R1-F1's rule).
    gone = reload(apply(revise(st, Action.RETRACT, 2)))
    assert render_projection(gone, budget, validation_records=[a, b]).ids == (1,)
    # Without its own receipt M1 establishes nothing, and supersession holds.
    assert render_projection(st, budget, validation_records=[b]).ids == ()
    # An unverified record is never exempt, whatever the budget cuts.
    plain = apply(proposal(empty(), text="the suite passes"))
    plain = apply(observation(plain, b, text="x" * MAX_TEXT_BYTES))
    plain = reload(apply(revise(plain, Action.SUPERSEDE, 1, replacement=2)))
    shown = render_projection(plain, budget, validation_records=[b])
    assert shown.ids == () and "the suite passes" not in shown.text


@pytest.mark.parametrize("padding", [0, 7], ids=["revision-2-to-3", "revision-9-to-10"])
def test_a_claim_that_fit_before_its_supersession_stays_shown_at_that_exact_budget(padding):
    """R3-F2 of PR #526: the budget is the exact size of the render before the
    supersession, which showed M1's established claim and left out its long
    equivalent replacement. After the supersession the replacement still
    does not fit, and M1's whole block, now with its supersession, does not
    either: its claim is shown alone, labeled as what it is. ``padding``
    adds another issue's records first, so that the supersession takes the
    Memory revision from 9 to 10, one more byte of header."""
    st = empty()
    for n in range(padding):
        other = replace(SCOPE, step=replace(STEP, issue_url=OTHER))
        st = apply(proposal(st, text=f"another issue's idea {n}", n=n + 1, scope=other))
    a, b = failure(1, exit_code=1), failure(2, exit_code=1)
    st = apply(observation(st, a, text="the suite failed"))
    st = apply(observation(st, b, text="x" * MAX_TEXT_BYTES))
    short, long = padding + 1, padding + 2
    loose = render_projection(reload(st), replace(REQ, max_bytes=1400), validation_records=[a, b])
    assert loose.ids == (short,)
    size = len(loose.text.encode())
    for exact in (
        replace(REQ, max_bytes=size),
        replace(REQ, max_chars=len(loose.text), max_bytes=size),
    ):
        before = render_projection(reload(st), exact, validation_records=[a, b])
        assert before.ids == (short,) and before.text == loose.text
        superseded = reload(apply(revise(st, Action.SUPERSEDE, short, replacement=long)))
        revisions = (len(st.encode()["events"]), len(superseded.encode()["events"]))
        assert revisions == ((2, 3) if not padding else (9, 10))
        after = render_projection(superseded, exact, validation_records=[a, b])
        assert after.ids == (short,) and after.retention == ((short, Retention.ESTABLISHED),)
        assert after.freshness.omitted == 1 and after.freshness.downgraded == 0
        assert (
            f"\nM{short} validation-failure — superseded by M{long}; claim only, text omitted\n"
            f"Observed validation 1 ({a['digest']}): FAIL on {SHA}"
        ) in after.text
        # Only the receipt's claim: none of the superseded prose.
        assert "the suite failed" not in after.text and "unverified" not in after.text
    # With room, the whole block, prose and supersession note included.
    roomy = render_projection(superseded, replace(REQ, max_bytes=1400), validation_records=[a, b])
    assert roomy.ids == (short,) and "the suite failed" in roomy.text
    assert f"Superseded by M{long}; shown because no record shown here" in roomy.text
    # The compact block is shorter than any full block of an observed record
    # by more than the supersession can add to the header and the notice.
    assert COMPACT_SLACK_BYTES > 3


def test_supersession_prose_never_displaces_another_established_claim():
    """R4-F2 of PR #526: two retained, non-equivalent observations (FAIL and
    TIMEOUT) both fit a budget of the exact size of their render. M1 is then
    superseded by M2, which does not carry M1's claim, so both stay shown:
    M1's whole block, now with its supersession note, and M2's no longer fit
    together, and the claims are chosen before the prose, so M1 is shown by
    its claim alone and M2 whole, within both limits."""
    a = failure(1, exit_code=1)
    b = failure(2, exit_code=1, timed_out=True)
    st = apply(observation(empty(), a, text="the suite failed"))
    st = apply(observation(st, b, text="the suite timed out"))
    loose = render_projection(reload(st), REQ, validation_records=[a, b])
    assert sorted(loose.ids) == [1, 2] and loose.freshness.omitted == 0
    size = len(loose.text.encode())
    superseded = reload(apply(revise(st, Action.SUPERSEDE, 1, replacement=2)))
    # With room, both whole blocks: more than the exact budget holds.
    roomy = render_projection(superseded, REQ, validation_records=[a, b])
    assert roomy.ids == (1, 2) and "Superseded by M2; shown because" in roomy.text
    assert len(roomy.text.encode()) > size
    for exact in (
        replace(REQ, max_bytes=size),
        replace(REQ, max_chars=len(loose.text), max_bytes=size),
    ):
        before = render_projection(reload(st), exact, validation_records=[a, b])
        assert before.text == loose.text
        after = render_projection(superseded, exact, validation_records=[a, b])
        assert after.ids == (1, 2) and after.freshness.omitted == 0
        assert dict(after.retention) == {1: Retention.ESTABLISHED, 2: Retention.ESTABLISHED}
        assert (
            "\nM1 validation-failure — superseded by M2; claim only, text omitted\n"
            f"Observed validation 1 ({a['digest']}): FAIL on {SHA}"
        ) in after.text
        assert f"Observed validation 2 ({b['digest']}): TIMEOUT on {SHA}" in after.text
        assert "the suite failed" not in after.text and "the suite timed out" in after.text
        assert len(after.text) <= exact.max_chars and len(after.text.encode()) <= exact.max_bytes
    # A budget with room for M1's whole block beside M2's restores its prose.
    whole = render_projection(
        superseded, replace(REQ, max_bytes=len(roomy.text.encode())), validation_records=[a, b]
    )
    assert whole.text == roomy.text


def test_freshness_names_the_candidate_and_launch_receipts_the_render_resolved():
    """R3-F4 of PR #526: holding only the candidate receipt, or only the launch
    receipt, downgrades a different record, and the freshness says so; the
    same receipts in another order give the same freshness and text."""
    candidate = EvidenceRef(Receipt.CANDIDATE, 2, "c" * 64)
    launch = EvidenceRef(Receipt.LAUNCH, 3, "d" * 64)
    st = apply(
        empty().add(
            actor=Actor.CONTROLLER,
            kind=Kind.REFUSED_CANDIDATE,
            scope=SCOPE,
            attempt=2,
            text="the candidate was refused",
            evidence=candidate,
        )
    )
    st = reload(
        apply(
            st.add(
                actor=Actor.CONTROLLER,
                kind=Kind.LAUNCH_END,
                scope=SCOPE,
                attempt=3,
                text="the launch ended without a result",
                evidence=launch,
            )
        )
    )
    only_candidate = render_projection(st, REQ, retained=[candidate])
    only_launch = render_projection(st, REQ, retained=[launch])
    assert only_candidate.retention == ((2, Retention.UNAVAILABLE), (1, Retention.RETAINED)) or (
        dict(only_candidate.retention) == {1: Retention.RETAINED, 2: Retention.UNAVAILABLE}
    )
    assert dict(only_launch.retention) == {1: Retention.UNAVAILABLE, 2: Retention.RETAINED}
    assert only_candidate.freshness.downgraded == only_launch.freshness.downgraded == 1
    assert only_candidate.freshness != only_launch.freshness
    assert only_candidate.freshness.evidence_digest != only_launch.freshness.evidence_digest
    # Neither nor both differ again; the order of the receipts does not.
    none = render_projection(st, REQ)
    both = render_projection(st, REQ, retained=[candidate, launch])
    digests = {p.freshness.evidence_digest for p in (none, only_candidate, only_launch, both)}
    assert len(digests) == 4
    for order in ([launch, candidate], (launch, candidate), {candidate, launch}):
        again = render_projection(st, REQ, retained=order)
        assert again.freshness == both.freshness and again.text == both.text
    assert f"Retained evidence digest: {both.freshness.evidence_digest}" in both.text


def test_validation_receipts_in_any_order_give_the_same_projection():
    """R4-F4 of PR #526: the same two valid validation records, supplied in
    checkpoint order or reversed, resolve every citation alike: equal
    freshness, text, IDs and retention. Two records sharing one sequence
    number are refused, in either order."""
    a = failure(1, exit_code=1)
    b = failure(2, exit_code=1, timed_out=True)
    st = apply(observation(empty(), a, text="the suite failed"))
    st = reload(apply(observation(st, b, text="the suite timed out")))
    forward = render_projection(st, REQ, validation_records=[a, b])
    assert sorted(forward.ids) == [1, 2]
    assert dict(forward.retention) == {1: Retention.ESTABLISHED, 2: Retention.ESTABLISHED}
    for order in ([b, a], (b, a)):
        again = render_projection(st, REQ, validation_records=order)
        assert again == forward
    # One receipt only is a different input, and a different freshness.
    assert render_projection(st, REQ, validation_records=[b]).freshness != forward.freshness
    twin = failure(1, exit_code=1, timed_out=True)
    for order in ([a, twin], [twin, a], [b, a, a]):
        with pytest.raises(StateError, match="invalid Issue Working Memory data"):
            render_projection(st, REQ, validation_records=order)


def test_an_identical_agent_revision_spelled_with_mixed_case_urls_is_a_replay():
    """R3-F5 of PR #526: an accepted correction whose step spells the issue and
    PR URLs in mixed case is saved with canonical lower-case URLs. The same
    input again, after a reload, is a replay: nothing changes and nothing is
    counted, where it used to fail its expected-revision check."""
    mixed = StepKey(
        Phase.ANALYZE_EXECUTE,
        "https://github.com/Owner/REPO/issues/2",
        "https://github.com/OWNER/Repo/pull/7",
        0,
        3,
    )
    st = reload(apply(proposal(empty(), text="first account", n=1)))

    def correct_mixed(store):
        return store.revise(
            Action.CORRECT,
            1,
            actor=Actor.AGENT,
            reason="corrected",
            step=mixed,
            attempt=3,
            text="attempt three",
            expected=1,
            accepted=True,
        )

    applied = reload(apply(correct_mixed(st)))
    saved = applied.encode()
    assert saved["events"][-1]["step"]["issue_url"] == ISSUE
    assert saved["events"][-1]["step"]["pr_url"] == "https://github.com/owner/repo/pull/7"
    again = correct_mixed(applied)
    assert (again.status, again.id) == ("replay", 1) and again.store is applied
    assert reload(again.store).encode() == saved and saved["refusals"] == 0
    # The same holds of a supersession and a retraction by the same launch.
    st = reload(apply(proposal(applied, text="attempt three's idea", n=3)))

    def by_launch_three(store, action, target, expected, **kwargs):
        return store.revise(
            action,
            target,
            actor=Actor.AGENT,
            reason="replaced",
            step=mixed,
            attempt=3,
            expected=expected,
            accepted=True,
            **kwargs,
        )

    for action, target, expected, kwargs in (
        (Action.SUPERSEDE, 1, 2, {"replacement": 3}),
        (Action.RETRACT, 3, 3, {}),
    ):
        st = reload(apply(by_launch_three(st, action, target, expected, **kwargs)))
        replayed = by_launch_three(st, action, target, expected, **kwargs)
        assert replayed.status == "replay" and replayed.store is st
        assert st.refusals == 0


def test_a_projection_is_bound_to_the_next_launch_of_its_snapshot():
    """R2-F5 of PR #526: a snapshot holding attempt 4's record is never
    labeled as what launch 2, or any launch but the next, was shown."""
    st = reload(apply(proposal(empty(seq=4), text="from attempt four", n=4)))
    for attempt in (1, 2, 4, 6):
        with pytest.raises(StateError):
            render_projection(st, replace(REQ, attempt=attempt))
    projected = render_projection(st, replace(REQ, attempt=5))
    assert projected.ids == (1,) and "launch: 5" in projected.text


@pytest.mark.parametrize("receipt", [Receipt.CANDIDATE, Receipt.LAUNCH])
def test_candidate_and_launch_citations_resolve_against_supplied_receipts(receipt):
    """R2-F7 of PR #526: a retained candidate or launch receipt is told apart
    from an unavailable one, and only a missing one downgrades its record."""
    kind = {Receipt.CANDIDATE: Kind.REFUSED_CANDIDATE, Receipt.LAUNCH: Kind.LAUNCH_END}[receipt]
    ref = EvidenceRef(receipt, 2, "c" * 64)
    st = empty().add(
        actor=Actor.CONTROLLER,
        kind=kind,
        scope=SCOPE,
        attempt=2,
        text="the launch ended without a result",
        evidence=ref,
    )
    st = reload(apply(st))
    held = render_projection(st, REQ, retained=[ref])
    assert held.retention == ((1, Retention.RETAINED),) and held.freshness.downgraded == 0
    assert "retained; it does not verify the text" in held.text
    assert "Observed" not in held.text and "unverified controller account" in held.text
    for supplied in ([], [EvidenceRef(receipt, 2, "d" * 64)], [EvidenceRef(receipt, 3, "c" * 64)]):
        gone = render_projection(st, REQ, retained=supplied)
        assert gone.retention == ((1, Retention.UNAVAILABLE),)
        assert gone.freshness.downgraded == 1 and "unavailable; needs revalidation" in gone.text
    # A validation receipt is resolved from its record only, never a bare reference.
    with pytest.raises(StateError):
        render_projection(st, REQ, retained=[EvidenceRef(Receipt.VALIDATION, 1, "c" * 64)])
    with pytest.raises(StateError):
        render_projection(st, REQ, retained=[ref] * 2 + ["not a reference"])


@pytest.mark.parametrize("name", ["review", "effect", "git"])
def test_a_citation_of_a_kind_this_slice_does_not_admit_is_refused(name):
    """R2-F7 of PR #526: review, effect and Git references have no record of
    this slice, so a checkpoint citing one does not load."""
    ref = EvidenceRef(Receipt.LAUNCH, 2, "c" * 64)
    st = empty().add(
        actor=Actor.CONTROLLER,
        kind=Kind.LAUNCH_END,
        scope=SCOPE,
        attempt=2,
        text="the launch ended",
        evidence=ref,
    )
    raw = apply(st).encode()
    raw["events"][0]["evidence"][0] = name
    with pytest.raises(StateError):
        MemoryStore(REPO, RUN, 128, raw)
    with pytest.raises(ValueError):
        Receipt(name)


def pi_profile(**options):
    return ProfileConfig(
        "analyze_execute",
        "pi",
        "openai/fixture-model",
        "high",
        options={"context_files": "false", "require_oauth": "false", **options},
    )


def _host(home, *, enforced=True, paths=()):
    return FreshContextHost(
        enforced=enforced, home=str(home), operator_paths=tuple(str(path) for path in paths)
    )


def test_pi_fresh_context_is_proved_only_by_the_launchs_private_agent_directory(
    tmp_path, monkeypatch
):
    """R2-F1, R3-F1 of PR #526: no check of a directory that outlives the
    launch keeps Pi's system prompt files out of it, so the proof is the
    launch's own: enforced, with Pi's agent directory in its private home,
    whatever the host's directory holds and whatever the host can contain."""
    # Whether this host could contain a launch's leftovers proves nothing.
    monkeypatch.setattr(executor_module, "orphan_containment_available", lambda: True)
    pi = PiProvider()
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # Enforced, the variable unset, no operator path near it: proved.
    assert pi.fresh_context_refusal(pi_profile(), {}, _host(home)) == ""
    # A clean, empty host directory is no proof without the sandbox: a
    # process an earlier launch left could write there after the check.
    for env in ({}, {"PI_CODING_AGENT_DIR": str(home / ".pi" / "agent")}):
        refusal = pi.fresh_context_refusal(pi_profile(), env, _host(home, enforced=False))
        assert "without the outer sandbox" in refusal and "sandbox.mode: required" in refusal
        assert str(home) not in refusal
    # A configured directory is the host's, shared by every launch given it.
    agent = tmp_path / "agent"
    agent.mkdir()
    refusal = pi.fresh_context_refusal(
        pi_profile(), {"PI_CODING_AGENT_DIR": str(agent)}, _host(home)
    )
    assert "$PI_CODING_AGENT_DIR" in refusal and "unset it" in refusal
    assert str(agent) not in refusal
    # An operator path that would show the host's directory in the private home.
    (tmp_path / "pi-link").symlink_to(home / ".pi")
    shown = [
        home / ".pi",
        home / ".pi" / "agent",
        home / ".pi" / "agent" / "SYSTEM.md",
        home,
        tmp_path / "pi-link",
    ]
    for path in shown:
        refusal = pi.fresh_context_refusal(pi_profile(), {}, _host(home, paths=[path]))
        assert "sandbox.read_only_paths" in refusal and str(path) not in refusal
    # Paths that show nothing there: elsewhere in the home, or above it,
    # where the private home is mounted over them.
    for path in (home / "tools", tmp_path, tmp_path / "elsewhere"):
        assert pi.fresh_context_refusal(pi_profile(), {}, _host(home, paths=[path])) == ""
    # The profile's half: context files off, and no ChatGPT sign-in, which
    # lives in a shared agent directory.
    for options in ({"context_files": "true"}, {"require_oauth": "true"}):
        refusal = pi.fresh_context_refusal(pi_profile(**options), {}, _host(home))
        assert "context_files: false and options.require_oauth: false" in refusal
        assert not pi.proves_fresh_context(pi_profile(**options))
    # An adapter without a proof has no host half to complete it.
    claude = ProfileConfig("analyze_execute", "claude", "opus", "high")
    assert ClaudeCodeProvider().fresh_context_refusal(claude, {}, _host(home)) == (
        "proves no fresh context"
    )


def _launch(home, mounts, **env):
    return LaunchDescription(tuple(mounts)), {"HOME": str(home), **env}


def test_the_launch_as_compiled_must_keep_pis_agent_directory_its_own(tmp_path):
    """R3-F1 of PR #526: the spawn-time half, on the description the launch
    actually runs in: Pi's agent directory is in the private home, and
    nothing is mounted at, above or inside it there."""
    pi = PiProvider()
    home = tmp_path / "home"
    home.mkdir()
    private = PrivateTmpfs(str(home), 0o700, None)
    req = AgentRequest("analyze_execute", "", str(tmp_path), pi_profile(), None, None)

    def problem(description, env):
        return pi.fresh_context_launch_problem(req, description, env)

    assert problem(*_launch(home, [private])) == ""
    cache = Bind(str(tmp_path / "cache"), str(home / ".cache"), Access.WRITABLE, True)
    assert problem(*_launch(home, [private, cache])) == ""
    for target in (".pi", ".pi/agent", ".pi/agent/SYSTEM.md"):
        bind = Bind(str(tmp_path / "x"), str(home / target), Access.READ_ONLY, True)
        assert "mounts something at, above or inside" in problem(*_launch(home, [private, bind]))
    assert "not the launch's private home" in problem(*_launch(home, []))
    assert "projected from the host" in problem(
        *_launch(home, [private], PI_CODING_AGENT_DIR=str(tmp_path / "agent"))
    )
    assert "no absolute $HOME" in problem(LaunchDescription((private,)), {})
    assert "not enforced" in problem(None, {"HOME": str(home)})
    # Through the adapter's compile: an unenforced Memory launch is refused
    # before anything is spawned; a Memory-off one is unchanged.
    with pytest.raises(SandboxLaunchRefusedError, match="not enforced"):
        pi.sandboxed(replace(req, fresh_context=True), ["pi"], {})
    assert pi.sandboxed(req, ["pi"], {}) == (["pi"], None)


class PiShaped(ScriptedProvider):
    """A scripted agent that holds Pi's fresh-context proof, all three halves.

    Each launch compiles its sandbox through the adapter's own path, as
    Pi's does (:meth:`AgentProvider.sandboxed`), and keeps the description.
    """

    name = "pi"
    option_keys = PiProvider.option_keys
    environment_names = PiProvider.environment_names
    context_files = staticmethod(PiProvider.context_files)
    require_oauth = staticmethod(PiProvider.require_oauth)
    proves_fresh_context = PiProvider.proves_fresh_context
    fresh_context_refusal = PiProvider.fresh_context_refusal
    fresh_context_launch_problem = PiProvider.fresh_context_launch_problem

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.compiled = []
        self.during_launch = None

    def sandbox_requirements(self, profile, command, env, home):
        return ProviderRequirements()

    def execute(self, req):
        if self.during_launch is not None:
            self.during_launch()
        self.compiled.append(self.sandboxed(req, ["pi"], req.env))
        return super().execute(req)


def pi_engine(state_dir, github, *, memory, sandbox="disabled", workdir=None, read_only=()):
    cfg = default_config()
    cfg.memory = MemoryConfig(memory)
    cfg.sandbox.mode = sandbox
    cfg.sandbox.read_only_paths = [str(path) for path in read_only]
    cfg.profiles["analyze_execute"] = pi_profile()
    eng = make_engine(
        state_dir, "never", github=github, cfg=cfg, origin=workdir is None, workdir=workdir
    )
    pi = PiShaped("never")
    eng.providers = ProviderRegistry(
        overrides={"pi": pi, "claude": eng.provider, "opencode": eng.provider}
    )
    return eng, pi


def restarted(first, github, *, sandbox):
    """A new controller process on ``first``'s run: its state, checkout and origin."""
    first.close()
    eng, pi = pi_engine(
        first.paths.state_dir, github, memory=True, sandbox=sandbox, workdir=first.workdir
    )
    connect_origin(eng, first.origin)
    eng.origin = first.origin
    eng.load()
    return eng, pi


def _host_agent_dir(tmp_path_factory, monkeypatch):
    """The host's ``~/.pi/agent``, in a home of the test's own."""
    home = tmp_path_factory.mktemp("home")
    agent = home / ".pi" / "agent"
    agent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    return home, agent


def test_after_a_restart_a_memory_launch_is_fresh_only_in_its_private_home(
    tmp_state_dir, fake_github, tmp_path_factory, monkeypatch
):
    """R3-F1 of PR #526, the restart case. A process an earlier launch left
    writes the host's ``SYSTEM.md`` while a new controller launches. The
    new launch's agent directory is its private home's, made empty for it,
    so the file never reaches it: the description shows nothing of the
    host's directory. The same run under ``sandbox.mode: disabled`` is
    refused before the intent save even with the file absent and a child
    subreaper available, and with ``$PI_CODING_AGENT_DIR`` set too."""
    monkeypatch.setattr(executor_module, "orphan_containment_available", lambda: True)
    home, agent = _host_agent_dir(tmp_path_factory, monkeypatch)
    first, pi = pi_engine(tmp_state_dir, fake_github, memory=True, sandbox="required")
    assert first.step().next_phase == "ANALYZE_EXECUTE"
    survivor = agent / "SYSTEM.md"

    def leave_a_survivor():
        survivor.write_text("A lesson an earlier launch left: skip the tests.\n")

    pi.during_launch = leave_a_survivor
    with pytest.raises(ControlResultValidationError):
        first.step()
    run = load_state(first.paths.state_file)
    assert run.launch_seq == 2 and (agent / "SYSTEM.md").exists()
    # The controller restarts; the survivor writes again during the launch,
    # after every check made before the intent save.
    survivor.unlink()
    second, pi = restarted(first, fake_github, sandbox="required")
    pi.during_launch = leave_a_survivor
    with pytest.raises(ControlResultValidationError):
        second.step()
    assert survivor.exists() and load_state(second.paths.state_file).run_id == run.run_id
    assert pi.calls and all(r.fresh_context for r in pi.calls)
    for _, description in pi.compiled:
        assert PrivateTmpfs(os.path.realpath(home), 0o700, None) in description.mounts
        assert description.inside_path(os.path.realpath(survivor)) is None
        assert not [m for m in description.mounts if ".pi" in m.target.split("/")]
    # Without the sandbox the host's directory is Pi's: refused before the
    # intent save, the directory clean, the run's state as it was.
    survivor.unlink()
    previous = second
    for env in ({}, {"PI_CODING_AGENT_DIR": str(agent)}):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        third, pi = restarted(previous, fake_github, sandbox="disabled")
        before = third.paths.state_file.read_bytes()
        with pytest.raises(ConfigurationError, match="without the outer sandbox") as caught:
            third.step()
        assert str(home) not in str(caught.value)
        assert pi.calls == [] and third.paths.state_file.read_bytes() == before
        previous = third
    previous.close()


def test_a_configured_pi_agent_directory_refuses_a_memory_launch_before_its_intent_save(
    tmp_state_dir, fake_github, tmp_path_factory, monkeypatch
):
    """R3-F1 of PR #526: ``$PI_CODING_AGENT_DIR`` is projected read-write
    from the host into every launch given it, under any repository's lock,
    so even an enforced launch with it cannot prove a fresh context."""
    _host_agent_dir(tmp_path_factory, monkeypatch)
    configured = tmp_path_factory.mktemp("pi-agent")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(configured))
    eng, pi = pi_engine(tmp_state_dir, fake_github, memory=True, sandbox="required")
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    before = eng.paths.state_file.read_bytes()
    with pytest.raises(ConfigurationError, match=r"\$PI_CODING_AGENT_DIR names a host") as caught:
        eng.step()
    assert str(configured) not in str(caught.value)
    assert pi.calls == [] and eng.paths.state_file.read_bytes() == before
    assert not (eng.paths.state_dir / "read-context").exists()
    # Unset, the same launch goes ahead in the private home.
    monkeypatch.delenv("PI_CODING_AGENT_DIR")
    with pytest.raises(ControlResultValidationError):
        eng.step()
    assert pi.calls and load_state(eng.paths.state_file).launch_seq == 2


def test_a_pi_directory_linked_out_of_the_home_is_refused_before_the_intent_save(
    tmp_state_dir, fake_github, tmp_path_factory, monkeypatch
):
    """R4-F3 of PR #526: ``~/.pi`` is a link to a directory outside the home,
    and ``sandbox.read_only_paths`` names it. Resolved, it lies outside the
    home; the launch shows it at its spelling, over Pi's agent directory in
    the private home. The adapter decides on that destination, so the launch
    is refused before its intent save: no ordinal spent, nothing launched,
    the checkpoint as it was. The compiled check stays the backstop."""
    home = tmp_path_factory.mktemp("home")
    store = tmp_path_factory.mktemp("pi-store")
    (store / "agent").mkdir()
    (home / ".pi").symlink_to(store)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    linked = home / ".pi"
    host = _host(home, paths=[linked])
    assert PiProvider().fresh_context_refusal(pi_profile(), {}, host) != ""
    eng, pi = pi_engine(
        tmp_state_dir, fake_github, memory=True, sandbox="required", read_only=[linked]
    )
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    before = eng.paths.state_file.read_bytes()
    spent = load_state(eng.paths.state_file).launch_seq
    with pytest.raises(ConfigurationError, match="sandbox.read_only_paths shows a host") as caught:
        eng.step()
    assert str(home) not in str(caught.value) and str(store) not in str(caught.value)
    assert pi.calls == [] and eng.paths.state_file.read_bytes() == before
    assert not (eng.paths.state_dir / "read-context").exists()
    # Without the adapter's check, the compiled description refuses the
    # launch before anything is spawned, but only after its intent save.
    monkeypatch.setattr(PiShaped, "fresh_context_refusal", lambda *args: "")
    with pytest.raises(SandboxLaunchRefusedError, match="at, above or inside"):
        eng.step()
    assert pi.compiled == []
    assert load_state(eng.paths.state_file).launch_seq == spent + 1
    eng.close()


@pytest.mark.parametrize("sandbox", ["disabled", "required"])
def test_a_memory_off_pi_launch_does_not_consult_the_fresh_context_proof(
    tmp_state_dir, fake_github, tmp_path_factory, monkeypatch, sandbox
):
    """R2-F1, R3-F1 of PR #526: with Memory off nothing of the proof runs,
    a configured agent directory holding a system prompt included."""
    agent = tmp_path_factory.mktemp("pi-agent")
    (agent / "SYSTEM.md").write_text("Operator-owned instructions.\n")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))

    def consulted(*args):
        raise AssertionError("a Memory-off launch consulted the fresh-context proof")

    for name in ("fresh_context_refusal", "proves_fresh_context", "fresh_context_launch_problem"):
        monkeypatch.setattr(PiShaped, name, consulted)
    eng, pi = pi_engine(tmp_state_dir, fake_github, memory=False, sandbox=sandbox)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    with pytest.raises(ControlResultValidationError):
        eng.step()
    assert pi.calls and not any(r.fresh_context for r in pi.calls)
    assert load_state(eng.paths.state_file).issue_memory == {}
