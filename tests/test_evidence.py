"""Candidate validation evidence: definitions, policy P, records and integrity (#172).

The pure half of ADR 0006 Wave 2B (``autoforge.evidence``): no git, no
subprocess, no engine. The engine paths that launch validators and save
these values are in ``tests/test_validation_evidence.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace

import pytest

from autoforge import evidence as ev
from autoforge.effects import MAX_EFFECT_STATE_CHARS
from autoforge.errors import (
    ConfigurationError,
    StateError,
    ValidationDefinitionError,
    ValidationInterruptedError,
)
from autoforge.evidence import (
    MAX_ARGV_CHARS,
    MAX_ARGV_ELEMENT_CHARS,
    MAX_ARGV_ELEMENTS,
    MAX_DEFINITIONS,
    MAX_RECORDS,
    MAX_TAIL_CHARS,
    NO_SANDBOX_TEMPLATE,
    Definition,
    EvidenceFields,
    Outcome,
    Policy,
    PrHeadProvenance,
    RemoteCandidate,
    WorktreeProvenance,
    appended,
    attempt_marker,
    bounded_tail,
    check_evidence,
    check_scope,
    definition_digest,
    definition_problem,
    definitions,
    enforcement_facts,
    interrupted_record,
    next_seq,
    observed_record,
    outcome_of,
    parse_candidate,
    parse_marker,
    parse_record,
    pinned_seqs,
    record_digest,
    record_summary,
    recover_attempt,
    set_digest,
    validation_set_digest,
)
from autoforge.executor import ExecutionResult
from autoforge.materialize import METHOD
from autoforge.sandbox import NetworkPosition, SandboxFacts
from autoforge.sandbox_policy import LaunchClass, LaunchFacts, launch_template, template_facts
from autoforge.state import MAX_STATE_FILE_BYTES

REPO = "owner/repo"
RUN = "af-test-1"
COMMIT = "a" * 40
TREE = "b" * 40
FINGERPRINT = "c" * 64
WORKSPACE_POLICY = "d" * 64
RUN_LOG = f"{RUN}/0004-analyze_execute-candidate-validation-1"
STARTED = "2026-10-15T00:00:00+00:00"
FINISHED = "2026-10-15T00:00:01.250000+00:00"
PR = "https://github.com/owner/repo/pull/42"
# Built at runtime: a literal would itself be credential-shaped text.
SECRET = "gh" + "p_" + "s3cretvalue123456"


def remote(commit: str = COMMIT, tree: str = TREE, provenance=None) -> RemoteCandidate:
    return RemoteCandidate(
        repository=REPO,
        object_format="sha1",
        commit=commit,
        tree=tree,
        provenance=provenance or WorktreeProvenance(7, "7", True),
    )


# -- the LOCAL shapes a protocol-12 file could hold (ADR 0009 D6, D8) ---------------
# The code that wrote them is removed, so they are built by hand, as
# ``git show 7df9917:src/autoforge/evidence.py`` wrote them: the ``local``
# candidate, the workspace record variant (the REMOTE record's keys plus the
# fingerprints of the walks before and after the run) and its marker.


def local_candidate(fingerprint: str = FINGERPRINT) -> dict:
    return {
        "kind": "local",
        "fingerprint": fingerprint,
        "workspace_policy_digest": WORKSPACE_POLICY,
        "run_id": RUN,
    }


def workspace_record(seq: int = 1, **kw) -> dict:
    """A workspace-variant ValidationRecord, digested as LOCAL digested one."""
    rec = record(seq, pol=policy(method="in-place"), **kw)
    rec["candidate"] = local_candidate()
    rec["start_fingerprint"] = FINGERPRINT
    rec["end_fingerprint"] = FINGERPRINT
    return redigest(rec)


def workspace_marker() -> dict:
    """A workspace-variant attempt marker: the candidate and its start fingerprint."""
    raw = marker(pol=policy(method="in-place"))
    raw["candidate"] = local_candidate()
    raw["start_fingerprint"] = FINGERPRINT
    return raw


def policy(**over) -> Policy:
    base = Policy(
        mode="disabled",
        template=NO_SANDBOX_TEMPLATE,
        env_names=("HOME", "PATH"),
        timeout_seconds=600,
        contain_orphans=True,
        method=METHOD,
    )
    return replace(base, **over)


def result(exit_code: int = 0, *, stdout: str = "", stderr: str = "", **over) -> ExecutionResult:
    over.setdefault("stdout_bytes", len(stdout.encode()))
    over.setdefault("stderr_bytes", len(stderr.encode()))
    return ExecutionResult(
        command=["make", "test"],
        cwd="/tmp/x",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        started_at=STARTED,
        finished_at=FINISHED,
        **over,
    )


DEF0 = Definition(0, ("make", "test"))
DEF1 = Definition(1, ("ruff", "check"))


def record(seq: int = 1, *, candidate=None, definition=DEF0, pol=None, run_log=RUN_LOG, **kw):
    res = kw.pop("res", None) or result(**kw)
    return observed_record(
        seq=seq,
        candidate=candidate or remote(),
        definition=definition,
        policy=pol or policy(),
        result=res,
        run_log=run_log,
    )


def marker(candidate=None, definition=DEF0, pol=None, run_log=RUN_LOG) -> dict:
    return attempt_marker(
        candidate=candidate or remote(),
        definition=definition,
        policy=pol or policy(),
        run_log=run_log,
        marked_at=STARTED,
    )


def check(records=(), **fields):
    return check_evidence(
        EvidenceFields(validation_records=list(records), **fields),
        repository=REPO,
        run_id=RUN,
    )


def redigest(rec: dict) -> dict:
    rec["digest"] = record_digest(rec)
    return rec


# -- definitions and digests (D3.5, D10.3) ----------------------------------------------


def test_a_definition_digest_is_stable_across_processes():
    """The digest is over a canonical encoding, never Python's hash or a path."""
    code = (
        "from autoforge.evidence import definition_digest; "
        "print(definition_digest(['make', 'test'], 0))"
    )
    other = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert other == definition_digest(["make", "test"], 0) == DEF0.digest
    assert len(other) == 64


def test_a_definition_digest_changes_with_the_argv_and_the_index():
    base = definition_digest(["make", "test"], 0)
    assert definition_digest(["make", "tests"], 0) != base
    assert definition_digest(["make", "test", "-j2"], 0) != base
    assert definition_digest(["make", "test"], 1) != base
    # Element boundaries are part of it: one element with a space is not two.
    assert definition_digest(["make test"], 0) != base


def test_a_definition_digest_is_over_the_configured_argv_not_the_redacted_one():
    """D3.5: a secret-shaped element changed is another definition, though it
    is stored and shown the same, redacted."""
    one = Definition(0, ("deploy", f"--token={SECRET}"))
    two = Definition(0, ("deploy", f"--token={SECRET}x"))
    assert one.digest != two.digest
    assert one.stored()["argv"] == two.stored()["argv"]
    assert SECRET not in json.dumps(one.stored()) and SECRET not in one.shown


def test_the_set_digest_is_ordered_and_the_empty_set_has_one():
    a, b = DEF0.digest, DEF1.digest
    assert set_digest([a, b]) != set_digest([b, a])
    assert len(set_digest([])) == 64
    assert validation_set_digest(definitions([["make", "test"], ["ruff", "check"]])) == set_digest(
        [a, b]
    )


@pytest.mark.parametrize(
    ("commands", "needle"),
    [
        ([["x"]] * (MAX_DEFINITIONS + 1), "17 definitions, over the bound of 16"),
        ([[]], "definition [0] is empty"),
        ([["x"] * (MAX_ARGV_ELEMENTS + 1)], "33 argv elements, over the bound of 32"),
        ([["ok"], ["x", " "]], "definition [1] element 1 is blank"),
        ([["x" * (MAX_ARGV_ELEMENT_CHARS + 1)]], "1025 characters, over the bound of 1024"),
        (
            [["x" * MAX_ARGV_ELEMENT_CHARS] * (MAX_ARGV_CHARS // MAX_ARGV_ELEMENT_CHARS) + ["y"]],
            "4097 characters in all, over the bound of 4096",
        ),
    ],
)
def test_a_set_beyond_the_bounds_is_refused_by_index_never_by_argv(commands, needle):
    problem = definition_problem(commands)
    assert needle in problem
    with pytest.raises(ValidationDefinitionError, match="definition|definitions"):
        definitions(commands)
    assert "xxxx" not in problem


def test_a_set_at_the_bounds_is_accepted():
    at = [["x" * MAX_ARGV_ELEMENT_CHARS] * 4] + [["y"] * MAX_ARGV_ELEMENTS] * (MAX_DEFINITIONS - 1)
    assert definition_problem(at) == ""
    assert len(definitions(at)) == MAX_DEFINITIONS


def test_only_the_candidate_tree_scope_exists():
    check_scope("candidate-tree")
    for scope in ("base-tree", "merge-result", "", None):
        with pytest.raises(ConfigurationError, match="only scope there is"):
            check_scope(scope)


# -- policy P (D8.1) -------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "required"},
        {"template": {"outer_sandbox": "other"}},
        {"env_names": ("HOME", "PATH", "LANG")},
        {"timeout_seconds": 601},
        {"contain_orphans": False},
        {"method": "in-place"},
    ],
    ids=["mode", "template", "allow-list", "timeout", "contain_orphans", "method"],
)
def test_the_policy_digest_changes_with_each_input(change):
    assert policy(**change).digest != policy().digest


def test_the_allow_list_is_a_set_of_names():
    assert policy(env_names=("PATH", "HOME", "PATH")).digest == policy().digest


def test_the_disabled_template_is_the_fixed_no_sandbox_one():
    assert dict(NO_SANDBOX_TEMPLATE) == {"outer_sandbox": "none"}
    assert not policy().requires_enforcement
    assert policy(mode="required").requires_enforcement


def test_preferred_requires_enforcement_as_required_does():
    """ADR 0005 D12.2, ADR 0009 D7: ``preferred`` refuses as ``required`` does,
    whatever the method, so its evidence must be enforced. The two modes stay
    two policies."""
    assert policy(mode="preferred").requires_enforcement
    assert policy(mode="preferred", method="in-place").requires_enforcement
    assert policy(mode="required", method="in-place").requires_enforcement
    assert policy(mode="preferred").digest != policy(mode="required").digest


def test_the_template_is_the_concrete_request_with_every_path_a_role(tmp_path):
    """Two concrete launches, in two directories, are one template once their
    paths are spelled as roles; the template itself names no concrete path."""
    launch_class = LaunchClass.EXPORT_COMMAND
    template = launch_template(template_facts(launch_class, NetworkPosition.HOST))
    for name in ("one", "two"):
        root = tmp_path / name
        paths = {
            "/@candidate-root": str(root / "export"),
            "/@home": str(root / "home"),
            "/@controller-config": str(root / "cfg"),
            "/@denied-roots": str(root / "denied"),
            "/@cache": str(root / "cache"),
        }
        facts = LaunchFacts(
            launch_class=launch_class,
            workspace=paths["/@candidate-root"],
            home=paths["/@home"],
            controller_env={},
            controller_config=paths["/@controller-config"],
            denied_roots=(paths["/@denied-roots"],),
        )
        text = json.dumps(launch_template(facts))
        for role, concrete in sorted(paths.items(), key=lambda kv: -len(kv[1])):
            text = text.replace(concrete, role)
        assert json.loads(text) == template
    assert str(tmp_path) not in json.dumps(template)


def test_the_template_changes_with_the_network_and_only_the_export_command_has_one():
    host = launch_template(template_facts(LaunchClass.EXPORT_COMMAND, NetworkPosition.HOST))
    isolated = launch_template(template_facts(LaunchClass.EXPORT_COMMAND, NetworkPosition.ISOLATED))
    assert json.dumps(host, sort_keys=True) != json.dumps(isolated, sort_keys=True)
    with pytest.raises(ValueError, match="no policy template"):
        template_facts(LaunchClass.REMOTE_AGENT, NetworkPosition.HOST)


# -- outcomes (D10.4) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        (0, Outcome.PASS),
        (1, Outcome.FAIL),
        (126, Outcome.FAIL),
        (127, Outcome.FAIL),
        (-9, Outcome.FAIL),
    ],
)
def test_an_exit_status_decides_pass_or_fail(code, outcome):
    assert outcome_of(result(code)) is outcome
    assert parse_record(record(exit_code=code)).outcome is outcome


def test_a_timeout_is_timeout_with_no_exit_code_whatever_the_status():
    rec = record(exit_code=0, timed_out=True)
    assert rec["outcome"] == "TIMEOUT" and rec["exit_code"] is None
    assert parse_record(rec).exit_code is None


@pytest.mark.parametrize(
    ("outcome", "code"),
    [("PASS", 1), ("FAIL", 0), ("TIMEOUT", 0), ("PASS", None)],
)
def test_an_outcome_its_exit_code_contradicts_is_refused(outcome, code):
    rec = record()
    rec["outcome"], rec["exit_code"] = outcome, code
    with pytest.raises(StateError, match="exit_code"):
        parse_record(redigest(rec))


# -- records: round trip, digest, shape (D10.1, D11.1) ---------------------------------


def test_a_remote_record_binds_commit_tree_definition_and_policy():
    rec = record()
    assert rec["candidate"]["commit"] == COMMIT and rec["candidate"]["tree"] == TREE
    assert rec["definition"]["definition_digest"] == DEF0.digest
    assert rec["policy"]["policy_digest"] == policy().digest
    assert rec["enforcement"]["enforced"] is False
    view = parse_record(json.loads(json.dumps(rec)))
    assert view.candidate == remote() and view.seq == 1 and view.outcome is Outcome.PASS


def test_identity_excludes_provenance():
    a = remote(provenance=WorktreeProvenance(7, "7", True))
    b = remote(provenance=PrHeadProvenance(PR))
    assert a.identity == b.identity
    assert remote().identity != remote(tree="e" * 40).identity
    assert RemoteCandidate("Owner/Repo", "sha1", COMMIT, TREE, a.provenance).identity == a.identity


def test_a_record_carries_no_workspace_fingerprint():
    rec = record()
    assert "start_fingerprint" not in rec and "end_fingerprint" not in rec
    assert not hasattr(parse_record(rec), "end_fingerprint")


def test_a_record_edited_by_hand_fails_its_digest():
    rec = record(exit_code=1)
    rec["output"]["stdout_tail"] = "all good"
    with pytest.raises(StateError, match="digest.*edited or corrupted"):
        parse_record(rec)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("leftovers"),
        lambda r: r.update(extra=1),
        lambda r: r.update(marked_at=STARTED),
        lambda r: r["candidate"].update(fingerprint=FINGERPRINT),
        lambda r: r["candidate"].update(kind="other"),
        lambda r: r["candidate"].update(commit="A" * 40),
        lambda r: r["candidate"].update(commit="a" * 39),
        lambda r: r["candidate"].update(object_format="sha256"),
        lambda r: r["definition"].update(definition_digest="0" * 63),
        lambda r: r["policy"].update(mode="sometimes"),
        lambda r: r["policy"].update(timeout_seconds=0),
        lambda r: r.update(outcome="MAYBE"),
        lambda r: r.update(schema=2),
        lambda r: r.update(seq=0),
        lambda r: r.update(seq=True),
        lambda r: r.update(started_at="yesterday"),
        lambda r: r.update(started_at="2026-10-15T00:00:00+02:00"),
        lambda r: r.update(started_at="2026-10-15"),
        lambda r: r.update(run_log="elsewhere"),
        lambda r: r.update(run_log=f"{RUN}/../x"),
        lambda r: r["enforcement"].update(network="everywhere"),
        lambda r: r["enforcement"].update(enforced="yes"),
        lambda r: r["leftovers"].update(orphans_killed=1),
        lambda r: r["output"].update(stdout_bytes=-1),
        lambda r: r["output"].update(stdout_tail="bell\x07"),
        lambda r: r["output"].update(stderr_tail="sep "),
    ],
)
def test_a_malformed_record_is_refused(mutate):
    rec = record()
    mutate(rec)
    with pytest.raises(StateError):
        parse_record(redigest(rec))


def test_a_mixed_candidate_is_refused():
    raw = remote().to_dict() | {"fingerprint": FINGERPRINT}
    with pytest.raises(StateError, match="unknown key"):
        parse_candidate(raw)


# -- the LOCAL kinds are unknown kinds (ADR 0009 D6, D8; ADR 0006 D2.7) ---------------


def test_a_local_candidate_is_refused_as_an_unknown_kind():
    with pytest.raises(StateError, match=r"candidate\.kind must be 'remote', got 'local'"):
        parse_candidate(local_candidate())
    with pytest.raises(StateError, match="must be 'remote'"):
        parse_candidate(local_candidate() | {"commit": COMMIT})


def test_a_workspace_variant_record_is_refused():
    """The record LOCAL wrote, whole and digested: its fingerprint keys are not a
    record's, and without them its candidate is still of an unknown kind."""
    rec = workspace_record()
    with pytest.raises(StateError, match="unknown key.*end_fingerprint.*start_fingerprint"):
        parse_record(rec)
    del rec["start_fingerprint"], rec["end_fingerprint"]
    with pytest.raises(StateError, match="candidate.kind must be 'remote', got 'local'"):
        parse_record(redigest(rec))


def test_a_workspace_variant_marker_is_refused():
    raw = workspace_marker()
    with pytest.raises(StateError, match="unknown key.*start_fingerprint"):
        parse_marker(raw)
    del raw["start_fingerprint"]
    with pytest.raises(StateError, match="candidate.kind must be 'remote', got 'local'"):
        parse_marker(raw)


def test_evidence_naming_a_workspace_record_is_refused_whole():
    """In a REMOTE state's validation fields: the records, the marker and a
    record beside a valid one are each refused, never skipped."""
    with pytest.raises(StateError, match="end_fingerprint"):
        check([workspace_record()])
    with pytest.raises(StateError, match="end_fingerprint"):
        check([record(1), workspace_record(2)])
    with pytest.raises(StateError, match="start_fingerprint"):
        check(validation_attempt=workspace_marker())


def test_a_sha256_candidate_round_trips():
    cand = RemoteCandidate(REPO, "sha256", "1" * 64, "2" * 64, PrHeadProvenance(PR))
    assert parse_candidate(json.loads(json.dumps(cand.to_dict()))) == cand
    raw = cand.to_dict() | {"tree": "2" * 40}
    with pytest.raises(StateError, match="64-character"):
        parse_candidate(raw)


@pytest.mark.parametrize(
    ("field", "at", "over"),
    [
        ("stdout_tail", "x" * MAX_TAIL_CHARS, "x" * (MAX_TAIL_CHARS + 1)),
        ("stdout_bytes", ev.MAX_BYTE_COUNT, ev.MAX_BYTE_COUNT + 1),
    ],
)
def test_output_fields_load_at_their_bound_and_not_past_it(field, at, over):
    rec = record()
    rec["output"][field] = at
    parse_record(redigest(rec))
    rec["output"][field] = over
    with pytest.raises(StateError, match="bound|must be in"):
        parse_record(redigest(rec))


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (ev.MIN_EXIT_CODE, True),
        (ev.MAX_EXIT_CODE, True),
        (ev.MIN_EXIT_CODE - 1, False),
        (256, False),
    ],
)
def test_an_exit_code_loads_within_its_bounds(value, ok):
    rec = record(exit_code=1)
    rec["exit_code"] = value
    if ok:
        parse_record(redigest(rec))
    else:
        with pytest.raises(StateError, match="must be in"):
            parse_record(redigest(rec))


def test_a_stored_argv_loads_at_its_bound_and_not_past_it():
    rec = record()
    rec["definition"]["argv"] = ["x" * ev.MAX_STORED_ARGV_ELEMENT_CHARS] * 4
    parse_record(redigest(rec))
    rec["definition"]["argv"] = ["x" * ev.MAX_STORED_ARGV_ELEMENT_CHARS] * 4 + ["y"]
    with pytest.raises(StateError, match="in all, over its bound"):
        parse_record(redigest(rec))
    rec["definition"]["argv"] = ["y"] * (MAX_ARGV_ELEMENTS + 1)
    with pytest.raises(StateError, match="elements, over its bound"):
        parse_record(redigest(rec))


def test_a_seq_loads_at_its_bound_and_not_past_it():
    rec = record(seq=ev.MAX_SEQ)
    parse_record(rec)
    rec["seq"] = ev.MAX_SEQ + 1
    with pytest.raises(StateError, match="must be in"):
        parse_record(redigest(rec))


# -- enforcement facts (D8.2) ----------------------------------------------------------


def test_enforcement_facts_are_copied_redacted_and_clipped():
    facts = SandboxFacts(
        enforced=True,
        backend="bwrap " + "b" * 100,
        backend_version="0.9",
        teardown=f"removed {SECRET} " + "t" * 200,
        network="isolated",
    )
    out = enforcement_facts(facts)
    assert out["enforced"] is True and out["network"] == "isolated"
    assert len(out["backend"]) == ev.MAX_BACKEND_CHARS and out["backend"].endswith("…")
    assert len(out["teardown"]) == ev.MAX_TEARDOWN_CHARS and out["teardown"].endswith("…")
    assert SECRET not in json.dumps(out)
    assert enforcement_facts(None) == {
        "enforced": False,
        "backend": "",
        "backend_version": "",
        "network": "",
        "teardown": "",
    }


def test_a_record_under_disabled_says_unenforced():
    rec = record(res=result(0, sandbox=None))
    assert rec["enforcement"]["enforced"] is False
    assert "unsandboxed" in record_summary(parse_record(rec))


# -- output tails (D10.2) --------------------------------------------------------------


def test_a_short_output_is_kept_whole_and_not_truncated():
    assert bounded_tail("ok\n", False) == ("ok\n", False)
    assert bounded_tail("ok\n", True) == ("ok\n", True)


def test_a_long_output_keeps_its_last_characters_and_says_so():
    text = "".join(f"{i:05d}\n" for i in range(2000))
    tail, cut = bounded_tail(text, False)
    assert cut is True and len(tail) == MAX_TAIL_CHARS and text.endswith(tail)


def test_control_characters_are_escaped_and_an_escape_is_never_split():
    tail, cut = bounded_tail("\x1b[31mred\x1b[0m\r\n\ttab end", False)
    assert tail == "\\x1b[31mred\\x1b[0m\\r\n\ttab\\u2028end" and cut is False
    # A tail of escapes: the cut keeps whole escapes only.
    tail, cut = bounded_tail("\x07" * 1000, False)
    assert cut is True and tail == "\\x07" * (MAX_TAIL_CHARS // 4)
    parse_record(record(stdout="\x1b" * 5000))


def test_a_secret_is_redacted_before_the_cut_even_when_it_straddles_it():
    """Redacting the stored tail alone would leave the secret's own tail, which
    no pattern then recognises, in state."""
    after = "\n" + "y" * (MAX_TAIL_CHARS - 11)
    text = "x" * 3000 + " " + SECRET + after
    tail, cut = bounded_tail(text, False)
    assert cut is True
    assert tail == "***REDACTED***"[-10:] + after
    assert SECRET[-10:] not in tail
    rec = record(stdout=f"token {SECRET}\n", stderr=f"{SECRET}")
    assert SECRET not in json.dumps(rec)


def test_replacement_characters_survive_the_tail():
    tail, _ = bounded_tail("bad � byte", False)
    assert tail == "bad � byte"


def test_an_output_flood_is_bounded_with_its_truncation_facts():
    flood = "z" * 50_000
    rec = record(
        exit_code=1, res=result(1, stdout=flood, stdout_truncated=True, stdout_bytes=10**9)
    )
    out = rec["output"]
    assert len(out["stdout_tail"]) == MAX_TAIL_CHARS
    assert out["stdout_tail_truncated"] is True and out["stdout_bytes"] == 10**9
    assert out["stderr_tail"] == "" and out["stderr_tail_truncated"] is False


# -- markers and interrupted records (D10.7, D11.2) ------------------------------------


def test_an_interrupted_record_is_built_from_the_marker_alone():
    raw = marker()
    rec = interrupted_record(seq=3, marker=raw, recovered_at=FINISHED)
    view = parse_record(rec)
    assert view.outcome is Outcome.INTERRUPTED and not view.is_observed
    assert "enforcement" not in rec and "exit_code" not in rec and "output" not in rec
    assert "no launch claimed" in record_summary(view)


def test_an_interrupted_record_carrying_observations_is_refused():
    rec = interrupted_record(seq=1, marker=marker(), recovered_at=FINISHED)
    rec["exit_code"] = 0
    with pytest.raises(StateError, match="unknown key"):
        parse_record(redigest(rec))


def _recover(records, mark, watermark=None, pinned=frozenset()):
    return recover_attempt(
        records, mark, watermark=watermark, pinned=set(pinned), recovered_at=FINISHED
    )


def _attempt(n: int, **kw) -> dict:
    """The marker of attempt ``n``: each launch has a step directory of its own."""
    return marker(run_log=f"{RUN}/{n:04d}-analyze_execute-candidate-validation-1", **kw)


def _interrupted_twice() -> list[dict]:
    return _recover(_recover([], _attempt(1)), _attempt(2))


def test_recovery_appends_one_interrupted_record_and_the_third_is_refused():
    records = _interrupted_twice()
    assert [r["outcome"] for r in records] == ["INTERRUPTED", "INTERRUPTED"]
    assert [r["seq"] for r in records] == [1, 2]
    with pytest.raises(ValidationInterruptedError, match="interrupted 3 times") as exc:
        _recover(records, _attempt(3))
    assert len(exc.value.records) == 3 and exc.value.records[-1]["seq"] == 3
    assert "make test" in str(exc.value) and COMMIT[:12] in str(exc.value)


def test_recovering_the_same_marker_twice_appends_once():
    """A duplicate resume: the marker's record exists, so nothing is added."""
    once = _recover([], _attempt(1))
    assert _recover(once, _attempt(1)) == once


def test_the_interruption_bound_is_per_candidate_definition_and_policy():
    records = _interrupted_twice()
    for other in (
        _attempt(3, candidate=remote(commit="e" * 40)),
        _attempt(3, definition=DEF1),
        _attempt(3, pol=policy(timeout_seconds=5)),
    ):
        assert _recover(records, other)[-1]["outcome"] == "INTERRUPTED"


def test_records_at_or_below_the_watermark_are_not_counted():
    records = _interrupted_twice()
    # Above watermark 2 only the new one counts; above 1, it and seq 2.
    for watermark in (2, 1):
        assert len(_recover(records, _attempt(3), watermark=watermark)) == 3
    with pytest.raises(ValidationInterruptedError):
        _recover(records, _attempt(3), watermark=0)


def test_a_stale_marker_whose_record_exists_changes_nothing():
    rec = record()
    assert _recover([rec], marker()) == [rec]
    # Another run log is another attempt.
    other = marker(run_log=f"{RUN}/0009-analyze_execute-candidate-validation-1")
    assert len(_recover([rec], other)) == 2


def test_a_recovered_interruption_is_never_evidence_and_never_pass():
    records = _recover([], marker())
    view = parse_record(records[0])
    assert not ev.applicable(view, remote(), DEF0.digest, policy())
    assert view.outcome is not Outcome.PASS


# -- applicability (D8.3) --------------------------------------------------------------


def test_a_record_applies_only_to_its_candidate_definition_and_policy():
    view = parse_record(record())
    assert ev.applicable(view, remote(provenance=PrHeadProvenance(PR)), DEF0.digest, policy())
    assert not ev.applicable(view, remote(tree="e" * 40), DEF0.digest, policy())
    assert not ev.applicable(view, remote(), DEF1.digest, policy())
    assert not ev.applicable(view, remote(), DEF0.digest, policy(timeout_seconds=1))
    assert not ev.applicable(view, remote(), Definition(0, ("make", "tests")).digest, policy())


def test_under_required_an_unenforced_record_does_not_apply():
    required = policy(mode="required")
    unenforced = parse_record(record(pol=required))
    assert not ev.applicable(unenforced, remote(), DEF0.digest, required)
    facts = SandboxFacts(enforced=True, backend="bwrap", network="host")
    enforced = parse_record(record(pol=required, res=result(0, sandbox=facts)))
    assert ev.applicable(enforced, remote(), DEF0.digest, required)


# -- integrity across fields (D11.1) ---------------------------------------------------


def _binding(records, candidate=None, pol=None) -> dict:
    return {
        "review_round": 1,
        "candidate": (candidate or remote()).to_dict(),
        "records": [{"seq": r["seq"], "digest": r["digest"]} for r in records],
        "validation_set_digest": set_digest(
            [r["definition"]["definition_digest"] for r in records]
        ),
        "policy_digest": (pol or policy()).digest,
    }


def _certificate(records, **over) -> dict:
    cert = {
        "schema": 1,
        "kind": "remote",
        "candidate": remote().to_dict(),
        "review_round": 2,
        "records": [{"seq": r["seq"], "digest": r["digest"]} for r in records],
        "validation_set_digest": set_digest(
            [r["definition"]["definition_digest"] for r in records]
        ),
        "policy_digest": policy().digest,
        "clean": {"needs_fix_round": False, "actionable_findings": 0},
        "issued_at": FINISHED,
        "reviewed_pr_url": PR,
        "reviewed_head_sha": COMMIT,
        "reviewed_base_ref": "main",
        "reviewed_merge_base_sha": "f" * 40,
        "comment": {
            "url": f"{PR}#issuecomment-99",
            "id": 99,
            "marker_key": {
                "review_round": 2,
                "reviewed_head_sha": COMMIT,
                "reviewed_base_ref": "main",
                "reviewed_merge_base_sha": "f" * 40,
            },
        },
    }
    cert.update(over)
    cert["digest"] = ev.certificate_digest(cert)
    return cert


def test_empty_evidence_is_valid():
    assert check() == []


def test_records_load_in_increasing_seq_only():
    one, two = record(1), record(2, definition=DEF1)
    assert [v.seq for v in check([one, two])] == [1, 2]
    with pytest.raises(StateError, match="increase strictly"):
        check([two, one])
    with pytest.raises(StateError, match="increase strictly"):
        check([one, one])


def test_a_record_of_another_repository_or_run_is_refused():
    foreign = RemoteCandidate("other/repo", "sha1", COMMIT, TREE, WorktreeProvenance(7, "7", True))
    with pytest.raises(StateError, match="names repository"):
        check([record(candidate=foreign)])
    with pytest.raises(StateError, match="this run's log directory"):
        check([record(run_log="af-2/0001-review-validation-1")])


def test_the_record_list_loads_at_its_bound_and_not_past_it():
    records = [record(i) for i in range(1, MAX_RECORDS + 1)]
    assert len(check(records)) == MAX_RECORDS
    with pytest.raises(StateError, match="over its bound of 64"):
        check([*records, record(MAX_RECORDS + 1)])


def test_a_marker_is_checked_like_a_record():
    check(validation_attempt=marker())
    with pytest.raises(StateError, match="validation_attempt"):
        check(validation_attempt=marker() | {"exit_code": 0})
    with pytest.raises(StateError, match="this run's log directory"):
        check(validation_attempt=marker(run_log="af-2/0001-review-validation-1"))


def test_a_binding_must_name_retained_matching_observed_records():
    one, two = record(1), record(2, definition=DEF1)
    check([one, two], review_evidence=_binding([one, two]))
    with pytest.raises(StateError, match="not retained"):
        check([one], review_evidence=_binding([one, two]))
    bad = _binding([one])
    bad["records"][0]["digest"] = "0" * 64
    with pytest.raises(StateError, match="under another digest"):
        check([one], review_evidence=bad)
    with pytest.raises(StateError, match="definition 1, not 0"):
        check([one, two], review_evidence=_binding([two]))
    with pytest.raises(StateError, match="another candidate"):
        check([one], review_evidence=_binding([one], candidate=remote(tree="e" * 40)))
    with pytest.raises(StateError, match="another policy"):
        check([one], review_evidence=_binding([one], pol=policy(timeout_seconds=9)))
    interrupted = interrupted_record(seq=3, marker=marker(), recovered_at=FINISHED)
    with pytest.raises(StateError, match="INTERRUPTED and never evidence"):
        check([one, interrupted], review_evidence=_binding([interrupted]))


def test_a_binding_of_pass_records_carries_their_set_digest():
    one, two = record(1), record(2, definition=DEF1)
    tampered = _binding([one, two])
    tampered["validation_set_digest"] = set_digest([DEF1.digest, DEF0.digest])
    with pytest.raises(StateError, match="validation_set_digest.*not the digest of the records"):
        check([one, two], review_evidence=tampered)
    # Two PASS records are the whole set: a set digest of three definitions is not theirs.
    tampered["validation_set_digest"] = set_digest(
        [DEF0.digest, DEF1.digest, Definition(2, ("x",)).digest]
    )
    with pytest.raises(StateError, match="validation_set_digest.*not the digest of the records"):
        check([one, two], review_evidence=tampered)


def test_an_empty_binding_carries_the_empty_sets_digest():
    empty = _binding([])
    assert empty["validation_set_digest"] == set_digest([])
    check(review_evidence=empty)
    with pytest.raises(StateError, match="validation_set_digest.*not the digest of the records"):
        check(review_evidence=empty | {"validation_set_digest": set_digest([DEF0.digest])})
    with pytest.raises(StateError, match="validation_set_digest.*not the digest of the records"):
        check(review_evidence=empty | {"validation_set_digest": "0" * 64})


@pytest.mark.parametrize("stop", [{"exit_code": 1}, {"timed_out": True}])
def test_a_binding_of_a_set_stopped_at_a_non_pass_ends_with_it(stop):
    """D10.6: the definitions after the first non-PASS get no record, so the
    binding's set digest covers definitions it does not name and is not
    recomputed on load (D5.5 compares it with the set in force, D12.2 issues
    no certificate from it); the non-PASS must be the last record it names."""
    one, failed = record(1), record(2, definition=DEF1, **stop)
    stopped = _binding([one, failed])
    stopped["validation_set_digest"] = set_digest(
        [DEF0.digest, DEF1.digest, Definition(2, ("x",)).digest]
    )
    check([one, failed], review_evidence=stopped)
    first = record(1, **stop)
    after = record(2, definition=DEF1)
    with pytest.raises(StateError, match=r"records\[0\].*before the last.*first non-PASS"):
        check([first, after], review_evidence=_binding([first, after]))


def test_a_binding_holds_at_most_one_set():
    records = [record(i) for i in range(1, 18)]
    binding = _binding(records)
    with pytest.raises(StateError, match="over its bound of 16"):
        check(records, review_evidence=binding)


def test_a_certificate_names_pass_records_of_its_set_and_its_comment():
    one = record(1)
    check([one], review_certificate=_certificate([one]))
    failed = record(2, exit_code=1)
    with pytest.raises(StateError, match="FAIL record"):
        check([failed], review_certificate=_certificate([failed]))
    with pytest.raises(StateError, match="digest.*edited or corrupted"):
        cert = _certificate([one])
        cert["issued_at"] = STARTED
        check([one], review_certificate=cert)
    with pytest.raises(StateError, match="not the digest of the records' definitions"):
        check([one], review_certificate=_certificate([one], validation_set_digest="0" * 64))
    with pytest.raises(StateError, match="differs from the candidate's commit"):
        check([one], review_certificate=_certificate([one], reviewed_head_sha="e" * 40))
    other_pr = {
        "url": "https://github.com/owner/repo/pull/43#issuecomment-99",
        "id": 99,
        "marker_key": _certificate([one])["comment"]["marker_key"],
    }
    with pytest.raises(StateError, match="not on the reviewed pull request"):
        check([one], review_certificate=_certificate([one], comment=other_pr))
    wrong_id = dict(_certificate([one])["comment"], id=98)
    with pytest.raises(StateError, match="differs from the comment URL's id"):
        check([one], review_certificate=_certificate([one], comment=wrong_id))
    key = dict(_certificate([one])["comment"]["marker_key"], review_round=1)
    wrong_key = dict(_certificate([one])["comment"], marker_key=key)
    with pytest.raises(StateError, match="marker_key.review_round"):
        check([one], review_certificate=_certificate([one], comment=wrong_key))
    with pytest.raises(StateError, match="needs_fix_round"):
        clean = {"needs_fix_round": True, "actionable_findings": 0}
        check([one], review_certificate=_certificate([one], clean=clean))


@pytest.mark.parametrize(
    ("review_round", "stored", "kind"),
    [(2, "2.0", "float"), (1, "true", "bool"), (2, '"2"', "str"), (2, "null", "NoneType")],
)
def test_a_certificate_marker_key_round_must_be_an_integer(review_round, stored, kind):
    """R2-F2: JSON ``2.0`` equals the certificate's round 2, and ``true``
    round 1, so the key's round is parsed by type, not only compared. The
    state is read back from JSON text and the digest is recomputed: the
    type alone is what fails the load."""
    one = record(1)
    cert = _certificate([one], review_round=review_round)
    text = json.dumps(cert).replace(
        '"marker_key": {"review_round": 2,', f'"marker_key": {{"review_round": {stored},'
    )
    cert = json.loads(text)
    assert type(cert["comment"]["marker_key"]["review_round"]).__name__ == kind
    cert["digest"] = ev.certificate_digest(cert)
    with pytest.raises(StateError, match=f"marker_key.review_round must be an integer, got {kind}"):
        check([one], review_certificate=cert)


def local_certificate(record_: dict | None = None) -> dict:
    """A ``local`` certificate as LOCAL issued one (the removed ``local_certificate``)."""
    one = record_ or workspace_record()
    cert = {
        "schema": 1,
        "kind": "local",
        "candidate": local_candidate(),
        "review_round": 1,
        "records": [{"seq": one["seq"], "digest": one["digest"]}],
        "validation_set_digest": set_digest([DEF0.digest]),
        "policy_digest": policy(method="in-place").digest,
        "clean": {"needs_fix_round": False, "actionable_findings": 0},
        "issued_at": FINISHED,
        "reviewed_workspace_fingerprint": FINGERPRINT,
    }
    cert["digest"] = ev.certificate_digest(cert)
    return cert


def test_a_local_certificate_is_refused_as_an_unknown_kind():
    """ADR 0009 D6: whatever it names, a REMOTE record included, a ``local``
    certificate does not load; nor does a REMOTE one relabelled ``local``."""
    one = record(1)
    with pytest.raises(StateError, match=r"review_certificate'\.kind must be 'remote'"):
        check([one], review_certificate=local_certificate(one))
    relabelled = _certificate([one], kind="local")
    with pytest.raises(StateError, match=r"review_certificate'\.kind must be 'remote'"):
        check([one], review_certificate=relabelled)
    with pytest.raises(StateError, match="must be 'remote'"):
        ev.check_certificate(local_certificate(one), check([one]), repository=REPO)


def test_a_binding_naming_a_local_candidate_is_refused():
    one = record(1)
    bound = _binding([one]) | {"candidate": local_candidate()}
    with pytest.raises(StateError, match="must be 'remote', got 'local'"):
        check([one], review_evidence=bound)


def test_the_failure_counter_names_a_retained_fail_or_timeout_of_its_commit():
    failed = record(1, exit_code=2)
    counter = {"phase": "FIX", "count": 1, "candidate_commit": COMMIT, "last_record_seq": 1}
    check([failed], producer_validation_failure=counter)
    with pytest.raises(StateError, match="not a FAIL or TIMEOUT"):
        check([record(1)], producer_validation_failure=counter)
    with pytest.raises(StateError, match="not retained"):
        check([], producer_validation_failure=counter)
    with pytest.raises(StateError, match="differs from the commit"):
        check([failed], producer_validation_failure=counter | {"candidate_commit": "e" * 40})
    with pytest.raises(StateError, match="must be in"):
        check([failed], producer_validation_failure=counter | {"count": 4})


@pytest.mark.parametrize(("watermark", "ok"), [(0, True), (2, True), (3, False), (-1, False)])
def test_the_override_watermark_is_between_zero_and_the_highest_seq(watermark, ok):
    records = [record(1), record(2, definition=DEF1)]
    if ok:
        check(records, validation_override_seq=watermark)
    else:
        with pytest.raises(StateError, match="validation_override_seq"):
            check(records, validation_override_seq=watermark)


# -- retention (D11.4) -----------------------------------------------------------------


def test_eviction_removes_the_oldest_unpinned_record_only():
    records = [record(i, definition=Definition(i % 2, ("x",))) for i in range(1, MAX_RECORDS + 1)]
    binding = _binding(records[1:3])  # seqs 2 and 3: definitions 0 and 1
    pinned = pinned_seqs(binding, {}, {"last_record_seq": 4})
    assert pinned == {2, 3, 4}
    out = appended(records, record(MAX_RECORDS + 1), pinned)
    assert len(out) == MAX_RECORDS
    assert [r["seq"] for r in out[:4]] == [2, 3, 4, 5]
    out = appended(out, record(MAX_RECORDS + 2), pinned)
    assert [r["seq"] for r in out[:4]] == [2, 3, 4, 6]
    check(out, review_evidence=binding)
    assert next_seq(out) == MAX_RECORDS + 3


def test_next_seq_never_reuses_an_evicted_seq():
    records = [record(i) for i in range(1, MAX_RECORDS + 1)]
    for _ in range(3):
        records = appended(records, record(next_seq(records)), set())
    assert records[0]["seq"] == 4 and records[-1]["seq"] == MAX_RECORDS + 3


# -- state size (D10.3) ----------------------------------------------------------------


def _worst_record(seq: int) -> dict:
    """The largest record the writers can produce: every bounded field at its bound."""
    argv = tuple("é" * MAX_ARGV_ELEMENT_CHARS for _ in range(4))
    noisy = "\x01" * 10_000
    facts = SandboxFacts(
        enforced=True, backend="b" * 200, backend_version="v" * 64, teardown="t" * 300
    )
    return observed_record(
        seq=seq,
        candidate=RemoteCandidate(
            "o" * 99 + "/" + "r" * 100,
            "sha256",
            "1" * 64,
            "2" * 64,
            WorktreeProvenance(ev.MAX_SEQ, "w" * 255, True, "3" * 64),
        ),
        definition=Definition(15, argv),
        policy=policy(mode="required"),
        result=result(1, stdout=noisy, stderr=noisy, sandbox=facts),
        run_log=f"{RUN}/" + "9" * 200 + "-x-1",
    )


def test_the_validation_fields_stay_well_inside_the_state_file_bound():
    worst = _worst_record(1)
    one = len(json.dumps(worst))
    assert one < 40_000, one
    records = [_worst_record(i) for i in range(1, MAX_RECORDS + 1)]
    total = len(json.dumps(records))
    assert total < 2_600_000, total
    # Every record, the marker, both bindings and the counter, beside the
    # largest effect state, with the JSON escaping of the whole file.
    assert 6 * (total + 3 * one) + 6 * MAX_EFFECT_STATE_CHARS < MAX_STATE_FILE_BYTES


# -- eligibility, reuse and the binding (D5.5, D7.2, D10.6; #173) ----------------------


def _views(*records: dict):
    return [parse_record(r) for r in records]


def test_a_pass_is_evidence_and_stays_so_across_the_override():
    (view,) = _views(record(1))
    assert ev.eligible(view, remote(), DEF0, policy(), None)
    assert ev.eligible(view, remote(), DEF0, policy(), 1)


def test_a_failure_is_terminal_until_the_override_makes_it_historical():
    (fail,) = _views(record(3, exit_code=1))
    (timeout,) = _views(record(4, res=result(-9, timed_out=True)))
    for view in (fail, timeout):
        assert ev.eligible(view, remote(), DEF0, policy(), None)
        assert ev.eligible(view, remote(), DEF0, policy(), view.seq - 1)
        assert not ev.eligible(view, remote(), DEF0, policy(), view.seq)


def test_eligibility_is_applicability_first_and_never_provenance():
    (view,) = _views(record(1))
    assert not ev.eligible(view, remote(commit="e" * 40), DEF0, policy(), None)
    assert not ev.eligible(view, remote(), DEF1, policy(), None)
    assert not ev.eligible(view, remote(), DEF0, policy(timeout_seconds=601), None)
    assert ev.eligible(view, remote(provenance=PrHeadProvenance(PR)), DEF0, policy(), None)
    interrupted = parse_record(interrupted_record(seq=2, marker=marker(), recovered_at=FINISHED))
    assert not ev.eligible(interrupted, remote(), DEF0, policy(), None)


def test_the_newest_eligible_record_is_the_one_reused():
    records = _views(record(1, exit_code=1), record(2), record(3, candidate=remote("e" * 40)))
    assert ev.eligible_record(records, remote(), DEF0, policy(), None).seq == 2
    assert ev.eligible_record(records, remote(), DEF1, policy(), None) is None


def test_evidence_is_complete_failed_or_undecided():
    defs = (DEF0, DEF1)
    passed = _views(record(1), record(2, definition=DEF1))
    complete = ev.find_evidence(passed, remote(), defs, policy(), None)
    assert complete.complete and complete.decided and complete.failed is None
    assert [r.seq for r in complete.records] == [1, 2]
    assert complete.validation_set_digest == validation_set_digest(defs)

    # The set stops at its first non-PASS, as a run of it does.
    failing = _views(record(1, exit_code=1), record(2, definition=DEF1))
    failed = ev.find_evidence(failing, remote(), defs, policy(), None)
    assert not failed.complete and failed.decided and failed.failed.seq == 1
    assert [r.seq for r in failed.records] == [1]

    # Overridden, the failure no longer decides anything: DEF0 must run.
    undecided = ev.find_evidence(failing, remote(), defs, policy(), 1)
    assert not undecided.decided and undecided.records == ()

    partial = ev.find_evidence(passed[:1], remote(), defs, policy(), None)
    assert not partial.decided and [r.seq for r in partial.records] == [1]

    empty = ev.find_evidence(passed, remote(), (), policy(), None)
    assert empty.complete and empty.records == ()


def _bound(records, round_=2, defs=(DEF0, DEF1), watermark=None):
    found = ev.find_evidence(records, remote(), defs, policy(), watermark)
    return ev.binding(round_, found), found


def test_a_binding_names_decided_evidence_only():
    views = _views(record(1), record(2, definition=DEF1))
    raw, found = _bound(views)
    assert raw == {
        "review_round": 2,
        "candidate": remote().to_dict(),
        "records": [{"seq": r.seq, "digest": r.digest} for r in found.records],
        "validation_set_digest": validation_set_digest((DEF0, DEF1)),
        "policy_digest": policy().digest,
    }
    assert ev.bound_records(raw, views) == list(found.records)
    with pytest.raises(ValueError, match="complete or failed"):
        _bound(views[:1])


def test_a_binding_is_reused_only_for_its_round_candidate_set_and_policy():
    views = _views(record(1), record(2, definition=DEF1))
    raw, _ = _bound(views)
    same = {
        "review_round": 2,
        "candidate": remote(provenance=PrHeadProvenance(PR)),
        "validation_set_digest": validation_set_digest((DEF0, DEF1)),
        "policy_digest": policy().digest,
        "watermark": None,
    }
    assert ev.binding_reusable(raw, views, **same)
    assert not ev.binding_reusable({}, views, **same)
    for change in (
        {"review_round": 3},
        {"candidate": remote(commit="e" * 40)},
        {"validation_set_digest": validation_set_digest((DEF0,))},
        {"policy_digest": policy(timeout_seconds=601).digest},
    ):
        assert not ev.binding_reusable(raw, views, **(same | change))
    # A PASS survives the operator's override.
    assert ev.binding_reusable(raw, views, **(same | {"watermark": 2}))


def test_a_binding_naming_an_overridden_failure_is_not_reused():
    views = _views(record(1, exit_code=1))
    raw, _ = _bound(views)
    same = {
        "review_round": 2,
        "candidate": remote(),
        "validation_set_digest": validation_set_digest((DEF0, DEF1)),
        "policy_digest": policy().digest,
    }
    assert ev.binding_reusable(raw, views, watermark=None, **same)
    assert not ev.binding_reusable(raw, views, watermark=1, **same)


def test_a_binding_naming_a_record_not_retained_under_its_digest_is_refused():
    views = _views(record(1), record(2, definition=DEF1))
    raw, _ = _bound(views)
    with pytest.raises(StateError, match="seq 2"):
        ev.bound_records(raw, views[:1])
    raw["records"][0]["digest"] = "0" * 64
    with pytest.raises(StateError, match="seq 1"):
        ev.bound_records(raw, views)


def test_the_failure_counter_counts_one_phase_entry_up_to_the_bound():
    first, second = _views(
        record(1, exit_code=1), record(2, exit_code=1, candidate=remote("e" * 40))
    )
    counter = ev.failure_counter("ANALYZE_EXECUTE", {}, first)
    assert counter == {
        "phase": "ANALYZE_EXECUTE",
        "count": 1,
        "candidate_commit": COMMIT,
        "last_record_seq": 1,
    }
    # Whichever candidate failed: the count goes on within the phase.
    counter = ev.failure_counter("ANALYZE_EXECUTE", counter, second)
    assert (counter["count"], counter["candidate_commit"]) == (2, "e" * 40)
    # Another phase's counter is not this one's.
    assert ev.failure_counter("FIX", counter, first)["count"] == 1
    for _ in range(5):
        counter = ev.failure_counter("ANALYZE_EXECUTE", counter, first)
    assert counter["count"] == ev.MAX_FAILURE_COUNT


def test_the_failure_counter_names_a_failure_only():
    (passed,) = _views(record(1))
    with pytest.raises(ValueError, match="REMOTE FAIL or TIMEOUT"):
        ev.failure_counter("FIX", {}, passed)


# -- a narrower run never stands for the full one (ADR 0010, #342) ---------------------

# A FAST-sized selection and the full suite, as an operator could configure
# either at the same index of `validation.commands`.
NARROW = Definition(0, ("uv", "run", "pytest", "tests/test_evidence.py"))
WHOLE = Definition(0, ("uv", "run", "pytest"))


def test_a_narrower_selections_pass_never_stands_for_the_full_definition():
    """ADR 0010 D2, D4 (no promotion): a PASS of a narrower selection is a
    record of that selection only. On the same candidate, under the same
    policy, at the same index, it is no evidence for the full suite's
    definition; and a record stands for its own argv at its own index, so a
    set that lists the same command elsewhere does not reuse it either."""
    (narrow,) = _views(record(1, definition=NARROW))
    assert ev.eligible(narrow, remote(), NARROW, policy(), None)
    assert not ev.eligible(narrow, remote(), WHOLE, policy(), None)
    assert not ev.eligible(narrow, remote(), Definition(1, NARROW.argv), policy(), None)
    assert ev.find_evidence([narrow], remote(), (WHOLE,), policy(), None).records == ()


def test_complete_evidence_for_a_narrower_set_is_not_evidence_for_the_full_set():
    """ADR 0010 D4: a set's evidence and its binding are for that set only.

    Every definition of a narrower set passing leaves the full set
    undecided, under another set digest, and a binding naming the narrower
    set's evidence is not reused for a review under the full set (no
    promotion). The other way, complete evidence for the full set bound for
    a review is not reused once the set in force is narrowed (no downgrade):
    the next entry binds again, under the narrower set's own digest.
    """
    narrow_set, full_set = (DEF0,), (DEF0, DEF1)
    one = _views(record(1))
    narrow = ev.find_evidence(one, remote(), narrow_set, policy(), None)
    full = ev.find_evidence(one, remote(), full_set, policy(), None)
    assert narrow.complete and not full.decided
    assert narrow.validation_set_digest != full.validation_set_digest

    def reusable(raw, records, defs) -> bool:
        return ev.binding_reusable(
            raw,
            records,
            review_round=2,
            candidate=remote(),
            validation_set_digest=ev.validation_set_digest(defs),
            policy_digest=policy().digest,
            watermark=None,
        )

    assert reusable(ev.binding(2, narrow), one, narrow_set)
    assert not reusable(ev.binding(2, narrow), one, full_set)

    both = _views(record(1), record(2, definition=DEF1))
    whole = ev.find_evidence(both, remote(), full_set, policy(), None)
    assert whole.complete
    assert reusable(ev.binding(2, whole), both, full_set)
    assert not reusable(ev.binding(2, whole), both, narrow_set)


def test_a_full_failure_stays_terminal_whatever_passes_beside_it():
    """ADR 0010 D1, D13.5 (no downgrade): a FAIL of the full suite on a
    candidate stays the set's verdict for that candidate. A later PASS of a
    narrower selection on it, or of the full suite on another commit with
    the same tree, does not clear it or make it historical; only the
    operator's override does (ADR 0006 D14.2), and the definition then runs
    again rather than taking the other PASS."""
    records = _views(
        record(1, definition=WHOLE, exit_code=1),
        record(2, definition=NARROW),
        record(3, definition=WHOLE, candidate=remote(commit="e" * 40)),
    )
    found = ev.find_evidence(records, remote(), (WHOLE,), policy(), None)
    assert found.failed is not None and found.failed.seq == 1 and not found.complete
    overridden = ev.find_evidence(records, remote(), (WHOLE,), policy(), 1)
    assert overridden.records == () and not overridden.decided
