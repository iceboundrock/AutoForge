"""The review certificate: issuance and applicability (ADR 0006 D12, #174).

The pure half (``autoforge.certificate``): no git, no subprocess, no engine.
Issuance builds the certificate from the persisted binding, the review
binding and the comment identity, or refuses naming the input; applicability
(:func:`certificate_problem`) is checked one field at a time, with and
without the live revision. The engine paths that issue the certificate in
the transition's write, the unblock and ``status`` are in
``tests/test_review_certificate.py``.
"""

from __future__ import annotations

import builtins
import socket
import subprocess
from dataclasses import replace

import pytest

from autoforge import evidence as ev
from autoforge.certificate import (
    NOT_A_PERMISSION,
    CertificateIssuanceError,
    Configured,
    Revision,
    certificate_problem,
    configuration_drift,
    describe,
    remote_certificate,
)
from autoforge.evidence import (
    Definition,
    PrHeadProvenance,
    RemoteCandidate,
    set_digest,
)
from autoforge.sandbox import SandboxFacts
from autoforge.state import AutoForgeState
from autoforge.transitions import Phase

from .test_evidence import (
    COMMIT,
    DEF0,
    DEF1,
    FINISHED,
    PR,
    REPO,
    RUN,
    TREE,
    local_candidate,
    local_certificate,
    policy,
    record,
    result,
    workspace_record,
)

MERGE_BASE = "f" * 40
COMMENT = f"{PR}#issuecomment-99"
ISSUED = "2026-10-17T00:00:00+00:00"
REVIEWED = Revision(PR, COMMIT, "main", MERGE_BASE)


def candidate(commit: str = COMMIT, tree: str = TREE) -> RemoteCandidate:
    return RemoteCandidate(REPO, "sha1", commit, tree, PrHeadProvenance(PR))


def binding(records, *, review_round: int = 2, cand=None, pol=None, set_value=None) -> dict:
    return {
        "review_round": review_round,
        "candidate": (cand or candidate()).to_dict(),
        "records": [{"seq": r["seq"], "digest": r["digest"]} for r in records],
        "validation_set_digest": set_value
        or set_digest([r["definition"]["definition_digest"] for r in records]),
        "policy_digest": (pol or policy()).digest,
    }


def remote_state(records=(), **over) -> AutoForgeState:
    """A REMOTE state whose completed round 2 is clean, at ``REVIEWED``."""
    fields = dict(
        run_id=RUN,
        repository=REPO,
        phase=Phase.READY_FOR_MERGE,
        current_pr_url=PR,
        review_round=2,
        reviewed_pr_url=PR,
        reviewed_head_sha=COMMIT,
        reviewed_base_ref="main",
        reviewed_merge_base_sha=MERGE_BASE,
        last_review_result="clean",
        last_review_comment_url=COMMENT,
        validation_records=list(records),
    )
    fields.update(over)
    return AutoForgeState(**fields)


def views(records):
    return [ev.parse_record(r) for r in records]


def issue(records, bound=None, **over) -> dict:
    """The REMOTE certificate of ``remote_state(records)``'s round, from ``bound``."""
    kw = dict(
        repository=REPO,
        review_round=2,
        needs_fix_round=False,
        actionable_findings=0,
        reviewed=REVIEWED,
        comment_url=COMMENT,
        marker_key={
            "review_round": 2,
            "reviewed_head_sha": COMMIT,
            "reviewed_base_ref": "main",
            "reviewed_merge_base_sha": MERGE_BASE,
        },
        issued_at=ISSUED,
    )
    kw.update(over)
    return remote_certificate(
        binding(records) if bound is None else bound,
        views(records),
        **kw,  # type: ignore[arg-type]
    )


def configured(*defs, pol=None) -> Configured:
    return Configured(tuple(defs), pol or policy())


def resealed(cert: dict, **over) -> dict:
    """``cert`` edited and digested again, as a careful hand edit would."""
    out = {**cert, **over}
    out["digest"] = ev.certificate_digest(out)
    return out


def remote_records(cand=None, pol=None):
    cand = cand or candidate()
    return [
        record(1, candidate=cand, pol=pol),
        record(2, candidate=cand, definition=DEF1, pol=pol),
    ]


# -- issuance (D12.2) ------------------------------------------------------------------


def test_the_certificate_names_the_exact_candidate():
    records = remote_records()
    cert = issue(records)
    assert cert["kind"] == "remote"
    assert cert["candidate"] == candidate().to_dict(), "commit, tree and provenance, copied"
    assert cert["reviewed_head_sha"] == cert["candidate"]["commit"] == COMMIT
    assert cert["candidate"]["tree"] == TREE


def test_the_certificate_copies_the_exact_validation_set_from_the_binding():
    """Its records (order and digests), set digest and policy digest are the
    binding's: issuance takes no configuration at all."""
    records = remote_records()
    bound = binding(records)
    cert = issue(records, bound)
    assert (
        cert["records"]
        == bound["records"]
        == [
            {"seq": 1, "digest": records[0]["digest"]},
            {"seq": 2, "digest": records[1]["digest"]},
        ]
    )
    assert cert["validation_set_digest"] == bound["validation_set_digest"]
    assert cert["validation_set_digest"] == set_digest([DEF0.digest, DEF1.digest])
    assert cert["policy_digest"] == bound["policy_digest"] == policy().digest
    assert cert["clean"] == {"needs_fix_round": False, "actionable_findings": 0}
    assert cert["issued_at"] == ISSUED


def test_the_certificate_names_the_exact_review_binding_and_comment():
    cert = issue(remote_records())
    assert (
        cert["reviewed_pr_url"],
        cert["reviewed_head_sha"],
        cert["reviewed_base_ref"],
        cert["reviewed_merge_base_sha"],
    ) == (PR, COMMIT, "main", MERGE_BASE)
    assert cert["comment"] == {
        "url": COMMENT,
        "id": 99,
        "marker_key": {
            "review_round": 2,
            "reviewed_head_sha": COMMIT,
            "reviewed_base_ref": "main",
            "reviewed_merge_base_sha": MERGE_BASE,
        },
    }
    assert cert["review_round"] == 2


def test_the_certificate_loads_as_issued():
    records = remote_records()
    state = remote_state(records, review_certificate=issue(records))
    assert len(state.validation_evidence()) == 2


def test_an_empty_set_is_certified_as_no_validation_configured():
    cert = issue([])
    assert cert["records"] == []
    assert cert["validation_set_digest"] == set_digest([])
    assert cert["policy_digest"] == policy().digest
    state = remote_state(review_certificate=cert)
    assert certificate_problem(cert, state, configured()) == ""
    assert "0 record(s)" in describe(cert)


def test_the_certificate_holds_no_review_prose():
    cert = issue(remote_records())
    assert set(cert) == {
        "schema",
        "digest",
        "kind",
        "candidate",
        "review_round",
        "records",
        "validation_set_digest",
        "policy_digest",
        "clean",
        "issued_at",
        "reviewed_pr_url",
        "reviewed_head_sha",
        "reviewed_base_ref",
        "reviewed_merge_base_sha",
        "comment",
    }


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"needs_fix_round": True}, "is not clean"),
        ({"actionable_findings": 1}, "is not clean"),
        ({"review_round": 3}, "binding is of review round 2, not of the completed round 3"),
        (
            {"reviewed": replace(REVIEWED, head_sha="e" * 40)},
            "names commit aaaaaaaaaaaa, not the reviewed HEAD eeeeeeeeeeee",
        ),
        ({"comment_url": "https://github.com/owner/repo/pull/42"}, "comment identity"),
        (
            {"comment_url": "https://github.com/owner/repo/pull/43#issuecomment-99"},
            "not on the reviewed pull request",
        ),
    ],
)
def test_issuance_is_refused_for_an_inconsistent_input(over, needle):
    with pytest.raises(CertificateIssuanceError, match=needle):
        issue(remote_records(), **over)


def test_issuance_is_refused_without_a_binding():
    with pytest.raises(CertificateIssuanceError, match="no review_evidence binding"):
        issue([], {})


def test_issuance_is_refused_over_a_non_pass_record():
    """D9.4 already refuses a clean result over it; issuance refuses again."""
    passed = record(1, candidate=candidate())
    failed = record(2, candidate=candidate(), definition=DEF1, exit_code=1)
    bound = binding([passed, failed], set_value=set_digest([DEF0.digest, DEF1.digest]))
    with pytest.raises(CertificateIssuanceError, match=r"record #2 \(definition \[1\]\).*FAIL"):
        issue([passed, failed], bound)


def test_issuance_is_refused_when_a_named_record_is_missing():
    records = remote_records()
    with pytest.raises(CertificateIssuanceError, match="seq 2.*not retained"):
        remote_certificate(
            binding(records),
            views(records[:1]),
            repository=REPO,
            review_round=2,
            needs_fix_round=False,
            actionable_findings=0,
            reviewed=REVIEWED,
            comment_url=COMMENT,
            marker_key={},
            issued_at=ISSUED,
        )


def test_issuance_is_refused_when_the_records_do_not_encode_to_the_set_digest():
    """A binding naming only part of its set (one record of two definitions)."""
    records = remote_records()
    bound = binding(records[:1], set_value=set_digest([DEF0.digest, DEF1.digest]))
    with pytest.raises(CertificateIssuanceError, match="do not encode to its validation_set"):
        issue(records, bound)


def test_issuance_is_refused_for_a_record_of_another_candidate_or_policy():
    other = record(1, candidate=candidate(tree="e" * 40))
    with pytest.raises(CertificateIssuanceError, match="another candidate"):
        issue([other], binding([other]))
    timeout = record(1, candidate=candidate(), pol=policy(timeout_seconds=9))
    with pytest.raises(CertificateIssuanceError, match="another policy"):
        issue([timeout], binding([timeout]))


def test_issuance_is_refused_for_a_binding_of_a_local_candidate():
    """ADR 0009 D6: a ``local`` candidate is an unknown kind, never certified."""
    local_binding = {**binding([]), "candidate": local_candidate()}
    with pytest.raises(CertificateIssuanceError, match="must be 'remote', got 'local'"):
        issue([], local_binding)


# -- applicability, one field at a time (D12.3) ----------------------------------------


@pytest.fixture
def held():
    records = remote_records()
    cert = issue(records)
    return records, cert, remote_state(records, review_certificate=cert)


def test_an_issued_certificate_applies(held):
    records, cert, state = held
    assert certificate_problem(cert, state, configured(DEF0, DEF1)) == ""


def test_no_certificate_is_not_applicable(held):
    _, _, state = held
    assert certificate_problem({}, state, configured(DEF0, DEF1)) == "no review certificate is held"


def test_the_digest_must_verify(held):
    _, cert, state = held
    edited = {**cert, "issued_at": FINISHED}
    assert "digest does not verify" in certificate_problem(edited, state, configured(DEF0, DEF1))


def test_the_kind_must_be_remote(held):
    """ADR 0009 D6: a ``local`` certificate, or a REMOTE one relabelled
    ``local`` and sealed again, never applies, before anything else of it is read."""
    _, cert, state = held
    expected = "it is a 'local' certificate in a remote run"
    assert certificate_problem(resealed(cert, kind="local"), state, configured(DEF0, DEF1)) == (
        expected
    )
    assert certificate_problem({**cert, "kind": "local"}, state, configured(DEF0)) == expected


def test_every_named_record_must_exist(held):
    records, cert, state = held
    state.validation_records = records[:1]
    state.review_certificate = {}
    problem = certificate_problem(cert, state, configured(DEF0, DEF1))
    assert problem.startswith("it does not load") and "seq 2, which is not retained" in problem


def test_every_named_record_must_match_its_digest(held):
    records, cert, state = held
    state.review_certificate = {}
    state.validation_records = [
        records[0],
        record(2, candidate=candidate(), definition=DEF1, stdout="x"),
    ]
    assert "names record seq 2 under another digest" in certificate_problem(
        cert, state, configured(DEF0, DEF1)
    )


def test_every_named_record_must_be_pass():
    passed = record(1, candidate=candidate())
    failed = record(2, candidate=candidate(), definition=DEF1, exit_code=1)
    cert = resealed(
        issue([passed]),
        records=[{"seq": 1, "digest": passed["digest"]}, {"seq": 2, "digest": failed["digest"]}],
        validation_set_digest=set_digest([DEF0.digest, DEF1.digest]),
    )
    state = remote_state([passed, failed])
    assert "a certificate names PASS records only" in certificate_problem(
        cert, state, configured(DEF0, DEF1)
    )


def test_the_round_must_be_the_completed_round(held):
    _, cert, state = held
    state.review_round = 3
    assert "review round 2, not of the completed round 3" in certificate_problem(
        cert, state, configured(DEF0, DEF1)
    )


@pytest.mark.parametrize("result", ["stale", "needs_fix", "fixed", ""])
def test_the_completed_round_must_still_be_clean(held, result):
    """#175: a drift the controller observed marks the round ``stale`` and
    leaves its binding where it was; the certificate of that round no longer
    applies, whatever else still matches."""
    _, cert, state = held
    state.last_review_result = result
    assert certificate_problem(cert, state, configured(DEF0, DEF1)) == (
        f"the state's last review result is {result or 'unrecorded'!r}, not the clean round "
        "it certifies"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewed_pr_url", "https://github.com/owner/repo/pull/43"),
        ("reviewed_head_sha", "e" * 40),
        ("reviewed_base_ref", "develop"),
        ("reviewed_merge_base_sha", "0" * 40),
    ],
)
def test_every_review_binding_field_must_be_the_states(held, field, value):
    _, cert, state = held
    setattr(state, field, value)
    problem = certificate_problem(cert, state, configured(DEF0, DEF1))
    assert problem.startswith(f"its {field} ") and "is not the state's" in problem


def test_the_candidate_commit_must_be_the_reviewed_head():
    """The certificate's own HEAD equals its candidate's commit (load), so a
    certificate for another commit names another HEAD than the state's."""
    other = candidate(commit="e" * 40)
    records = remote_records(cand=other)
    moved = issue(
        records,
        binding(records, cand=other),
        reviewed=replace(REVIEWED, head_sha="e" * 40),
        marker_key={
            "review_round": 2,
            "reviewed_head_sha": "e" * 40,
            "reviewed_base_ref": "main",
            "reviewed_merge_base_sha": MERGE_BASE,
        },
    )
    state = remote_state(records)
    assert certificate_problem(moved, state, configured(DEF0, DEF1)).startswith(
        "its reviewed_head_sha 'eeee"
    )


def test_the_comment_must_be_the_last_review_comment(held):
    _, cert, state = held
    state.last_review_comment_url = f"{PR}#issuecomment-100"
    assert "is not the last review comment" in certificate_problem(
        cert, state, configured(DEF0, DEF1)
    )


def test_the_set_digest_must_be_the_one_in_force(held):
    _, cert, state = held
    problem = certificate_problem(cert, state, configured(DEF0))
    assert problem.startswith("the validation set changed")
    assert problem == configuration_drift(cert, configured(DEF0))
    assert certificate_problem(cert, state, configured(DEF1, DEF0)).startswith(
        "the validation set changed"
    ), "order is part of the set"


def test_the_policy_digest_must_be_the_one_in_force(held):
    _, cert, state = held
    changed = configured(DEF0, DEF1, pol=policy(timeout_seconds=601))
    problem = certificate_problem(cert, state, changed)
    assert problem.startswith("the validation policy changed")
    both = configured(DEF0, pol=policy(timeout_seconds=601))
    assert "set changed" in configuration_drift(cert, both)
    assert "policy changed" in configuration_drift(cert, both)


def test_every_named_record_must_be_eligible_under_the_policy_in_force():
    """D8.3 through #173's eligibility rule: a PASS made unenforced under a
    policy that requires the outer sandbox stands for nothing."""
    required = policy(mode="required")
    unenforced = record(1, candidate=candidate(), pol=required)
    cert = issue([unenforced], binding([unenforced], pol=required))
    state = remote_state([unenforced], review_certificate=cert)
    assert "record #1 it names is not evidence for definition [0]" in certificate_problem(
        cert, state, configured(DEF0, pol=required)
    )
    facts = SandboxFacts(enforced=True, backend="bwrap", network="host")
    enforced = record(1, candidate=candidate(), pol=required, res=result(0, sandbox=facts))
    cert = issue([enforced], binding([enforced], pol=required))
    state = remote_state([enforced], review_certificate=cert)
    assert certificate_problem(cert, state, configured(DEF0, pol=required)) == ""


# -- with the live revision (#175's use) -----------------------------------------------


def test_the_live_revision_equal_to_the_certificates_applies(held):
    _, cert, state = held
    live = Revision("https://github.com/OWNER/repo/pull/42", COMMIT.upper(), "main", MERGE_BASE)
    assert certificate_problem(cert, state, configured(DEF0, DEF1), live) == ""


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"pr_url": "https://github.com/owner/repo/pull/43"}, "not of the live PR"),
        ({"pr_url": "not a url"}, "not of the live PR"),
        ({"head_sha": "e" * 40}, "its reviewed_head_sha"),
        ({"base_ref": "develop"}, "its reviewed_base_ref"),
        ({"merge_base_sha": "0" * 40}, "its reviewed_merge_base_sha"),
    ],
)
def test_each_live_field_must_equal_the_certificates(held, over, needle):
    _, cert, state = held
    problem = certificate_problem(cert, state, configured(DEF0, DEF1), replace(REVIEWED, **over))
    assert needle in problem


def test_the_state_is_checked_before_the_live_revision(held):
    _, cert, state = held
    state.review_round = 3
    live = replace(REVIEWED, head_sha="e" * 40)
    assert "completed round 3" in certificate_problem(cert, state, configured(DEF0, DEF1), live)


def test_applicability_performs_no_io(held, monkeypatch):
    _, cert, state = held

    def forbidden(*args, **kwargs):
        raise AssertionError("certificate_problem performed I/O")

    for target, name in (
        (builtins, "open"),
        (subprocess, "Popen"),
        (subprocess, "run"),
        (socket, "socket"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    assert certificate_problem(cert, state, configured(DEF0, DEF1), REVIEWED) == ""
    assert certificate_problem(cert, state, configured(DEF0)) != ""


# -- the removed LOCAL kind (ADR 0009 D6, D8) -------------------------------------------


def test_a_local_certificate_is_never_applicable_in_a_run():
    """The certificate LOCAL issued, with its workspace record retained beside it:
    refused by its kind, with or without the live revision."""
    rec = workspace_record()
    cert = local_certificate(rec)
    state = remote_state([rec], review_certificate=cert)
    expected = "it is a 'local' certificate in a remote run"
    assert certificate_problem(cert, state, configured(DEF0)) == expected
    assert certificate_problem(cert, state, configured(DEF0), REVIEWED) == expected
    assert certificate_problem(cert, remote_state(), configured(DEF0)) == expected


def test_the_wording_says_it_is_no_merge_permission():
    assert "not a GitHub approval" in NOT_A_PERMISSION
    assert "permission to merge" in NOT_A_PERMISSION


# -- a narrower set never certifies the full one (ADR 0010, #342) ----------------------


def test_a_certificate_for_a_narrower_set_never_applies_under_the_full_set_and_back():
    """ADR 0010 D4. A review presented a FAST-sized set certifies that set and
    nothing wider: under the full set it is refused as a set change, not
    taken as partial evidence (no promotion). A certificate for the full set
    is refused the same way once the set in force is narrowed to a FAST-sized
    one (no downgrade), so narrowing the configuration can never let an
    earlier review stand for less than it was presented."""
    narrow_def = Definition(0, ("uv", "run", "pytest", "tests/test_evidence.py"))
    narrow = [record(1, candidate=candidate(), definition=narrow_def)]
    narrow_cert = issue(narrow)
    narrow_state = remote_state(narrow, review_certificate=narrow_cert)
    assert certificate_problem(narrow_cert, narrow_state, configured(narrow_def)) == ""
    for wider in (configured(narrow_def, DEF1), configured(DEF0, DEF1)):
        assert certificate_problem(narrow_cert, narrow_state, wider).startswith(
            "the validation set changed"
        )

    full = remote_records()
    full_cert = issue(full)
    full_state = remote_state(full, review_certificate=full_cert)
    assert certificate_problem(full_cert, full_state, configured(DEF0, DEF1)) == ""
    for narrower in (configured(DEF0), configured(narrow_def)):
        assert certificate_problem(full_cert, full_state, narrower).startswith(
            "the validation set changed"
        )
