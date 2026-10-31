"""The review certificate at the merge gate, across drift, recovery and MERGE (#175).

ADR 0006 D13 places the certificate check in ``_verify_pr_for_merge``, which
the READY_FOR_MERGE gate pass and MERGE share: after the PR identity, HEAD,
base and merge-base checks, before the #94 re-read of the review comment,
the readiness reads and ``merge.verification_commands``. It is one more
prerequisite, never "certificate => merge". D14.1 derives inapplicability
where evidence is used, with no staleness flag, and the MERGE crash windows
keep their existing recovery (D12.4).

End to end and offline, like ``tests/test_review_certificate.py``: real git
repositories and a local bare origin under ``tmp_path``, real Python
validators, the in-memory GitHub and scripted agents (the Pi fake for
parity). What is covered:

- the gate: a certificate and every live gate continue to the existing
  merge; each D13.2 requirement failing alone blocks, naming it, before the
  comment is re-read or any verification command runs, in one write; every
  other gate still refuses with a certificate held; ``unblock`` takes the
  way out the block names;
- each row of D14.1 at the consumers it reaches (a producer, the REVIEW
  entry, ``unblock``, the gate pass, MERGE and ``status``), the replan
  replacement and LOCAL included;
- the MERGE crash windows, a restart and the legacy states;
- ``resume`` holding READY_FOR_MERGE, and the dry-run plan;
- the same routing behind every provider (D17.2).

The pure applicability rules are in ``tests/test_certificate.py``.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace

import pytest

from autoforge import cli
from autoforge import engine as engine_module
from autoforge.certificate import Revision
from autoforge.evidence import (
    Definition,
    LocalCandidate,
    certificate_digest,
    find_evidence,
    observed_record,
    parse_candidate,
)
from autoforge.executor import ExecutionResult, execute
from autoforge.sandbox_policy import LaunchClass
from autoforge.transitions import Phase

from .conftest import (
    BASE_RUN_ID,
    BRANCH,
    GIT_IDENT,
    PR,
    FakeGitHub,
    analyze_payload,
    block,
    ci_check,
    comment_url,
    commit_in,
    fixed,
    fixer,
    git_out,
    is_shell_probe,
    make_engine,
    publish_pr_head,
    review_comment_body,
    review_result,
    scripted,
)
from .test_review_certificate import (
    ALSO_PASSES,
    PASSES,
    PY,
    REASON,
    _allow_list,
    _definition_set,
    _sandbox_mode,
    _timeout,
    certified,
    clean_reviewer,
    config,
    policy_digest,
    resumed,
    reviewing,
    saved,
)
from .test_validation_gates import _route

NO_CERTIFICATE = "no applicable ReviewCertificate for HEAD"
FAILS = [PY, "-c", "import sys; sys.exit(1)"]


def appends(marker) -> list[str]:
    """A ``merge.verification_commands`` entry recording each of its runs in ``marker``."""
    return [
        PY,
        "-c",
        "import pathlib, sys; pathlib.Path(sys.argv[1]).open('a').write('ran\\n')",
        str(marker),
    ]


def runs(marker) -> int:
    return marker.read_text().count("ran\n") if marker.exists() else 0


def comment_reads(gh) -> int:
    return sum(1 for call in gh.calls if call[0] == "get_comment")


def ready(tmp_state_dir, gh, *commands, premerge=()):
    """A certified run in READY_FOR_MERGE with the merge gate open in its configuration."""
    eng, head = certified(tmp_state_dir, gh, *commands)
    eng.config.safety.allow_merge = True
    eng.config.merge.verification_commands = [list(c) for c in premerge]
    return eng, head


def at(eng, phase: Phase):
    """``eng`` in ``phase``: READY_FOR_MERGE as certified, or MERGE after a real gate pass."""
    if phase == Phase.MERGE:
        out = eng.step(allow_merge=True)
        assert out.next_phase == "MERGE", out.message
    return eng


def merges_through(eng) -> list[str]:
    """Step through the gate pass and MERGE; the phases entered."""
    phases = []
    while eng.state.phase in (Phase.READY_FOR_MERGE, Phase.MERGE):
        phases.append(eng.step(allow_merge=True).next_phase)
    return phases


def reopened(eng, tmp_state_dir, gh, script=None, cfg=None):
    """A new controller process on the same run, the merge gate open in its configuration."""
    eng2 = resumed(eng, tmp_state_dir, gh, script, cfg=cfg)
    eng2.config.safety.allow_merge = True
    return eng2


def status_applicable(eng) -> str:
    """What ``status`` shows for the certificate: its ``Applicable:`` line."""
    s = saved(eng)
    lines = cli._render_validation_evidence(s, eng.config, cli._review_certificate_check(eng, s))
    (line,) = [line.strip() for line in lines if line.strip().startswith("Applicable:")]
    return line


def resealed(cert: dict, **fields) -> dict:
    """``cert`` with ``fields`` changed and its digest recomputed, as a forgery would be."""
    edited = json.loads(json.dumps(cert))
    edited.update(fields)
    edited["digest"] = certificate_digest(edited)
    return edited


def a_human_push(eng, parent: str, *, same_tree: bool = False, branch: str = BRANCH) -> str:
    """A commit on ``parent`` pushed to ``branch`` by someone else; its SHA.

    ``same_tree`` keeps the parent's tree (another commit, the same bytes);
    otherwise the commit adds a file. The checkout is not moved.
    """
    if same_tree:
        return publish_pr_head(eng, parent=parent, message="A human's push", branch=branch)
    workdir = str(eng.workdir)
    blob = subprocess.run(
        ["git", "-C", workdir, "hash-object", "-w", "--stdin"],
        input=f"pushed onto {parent}\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    entries = git_out("-C", workdir, "ls-tree", f"{parent}^{{tree}}").splitlines()
    entries.append(f"100644 blob {blob}\tpushed-{parent[:8]}.txt")
    tree = subprocess.run(
        ["git", "-C", workdir, "mktree"],
        input="\n".join(entries) + "\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    sha = git_out("-C", workdir, *GIT_IDENT, "commit-tree", tree, "-p", parent, "-m", "A push")
    eng.origin.publish(eng.workdir, sha, branch)
    return sha


def records_for(s, commit: str) -> list[dict]:
    return [r for r in s.validation_records if r["candidate"]["commit"] == commit]


# -- the gate: a prerequisite, never "certificate => merge" (D13) -----------------------


def test_a_certificate_and_every_live_gate_continue_to_the_existing_merge(tmp_state_dir, tmp_path):
    gh = FakeGitHub()
    marker = tmp_path / "premerge.txt"
    eng, head = ready(tmp_state_dir, gh, PASSES, premerge=[appends(marker)])
    cert = saved(eng).review_certificate
    out = eng.step(allow_merge=True)
    assert out.next_phase == "MERGE", out.message
    assert "where the review certificate of round 1 applies" in out.message
    assert runs(marker) == 1, "merge.verification_commands still ran with a certificate held"
    reads = comment_reads(gh)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and "merged by the controller" in out.message
    assert gh.merges == [(PR, "squash", head)]
    assert comment_reads(gh) == reads + 1, "MERGE re-read the review comment itself"
    assert runs(marker) == 1, "the premerge_verified_* cache rule is unchanged"
    s = saved(eng)
    assert s.counted_merged_prs == [PR]
    assert s.review_certificate == cert, "the certificate is neither re-issued nor rewritten"


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_no_certificate_blocks_before_anything_else_and_unblock_decides_review(
    tmp_state_dir, tmp_path, phase
):
    """A legacy state (none held, D15.4) with an OPEN PR: conclusive, never
    retried; the fresh round validates and certifies its own clean result."""
    gh = FakeGitHub()
    marker = tmp_path / "premerge.txt"
    eng, head = ready(tmp_state_dir, gh, PASSES, premerge=[appends(marker)])
    at(eng, phase)
    eng.state.review_certificate = {}
    eng._save()
    before, reads = runs(marker), comment_reads(gh)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    reason = saved(eng).block_reason
    assert reason.startswith(
        f"{NO_CERTIFICATE} {head}; unblock to run a new validated REVIEW round"
    )
    assert "nothing was merged or counted" in reason
    assert runs(marker) == before and comment_reads(gh) == reads
    assert gh.merges == [] and saved(eng).counted_merged_prs == []
    assert saved(eng).attempt == 0, "conclusive: no verification attempt was consumed"

    decision = eng.unblock(REASON)
    assert decision.unblocked and decision.phase == "REVIEW"
    assert "no review certificate is held" in decision.message
    eng.provider._handler = scripted(clean_reviewer(eng, 2))
    assert eng.step().next_phase == "READY_FOR_MERGE"
    cert = saved(eng).review_certificate
    assert cert["review_round"] == 2 and cert["reviewed_head_sha"] == head
    assert merges_through(eng) == ["MERGE", "UPDATE_EPIC"]
    assert gh.merges == [(PR, "squash", head)]


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
@pytest.mark.parametrize(
    ("change", "named"),
    [
        (_timeout, "the validation policy changed"),
        (_sandbox_mode, "the validation policy changed"),
        (_allow_list, "the validation policy changed"),
        (_definition_set, "the validation set changed"),
    ],
    ids=["timeout", "sandbox mode", "allow-list", "definition set"],
)
def test_a_set_or_policy_changed_since_issuance_blocks_naming_it(
    tmp_state_dir, tmp_path, phase, change, named
):
    """The operator restores the configuration and unblocks: the certificate
    applies again and the next gate pass merges, with no new round."""
    gh = FakeGitHub()
    marker = tmp_path / "premerge.txt"
    eng, head = ready(tmp_state_dir, gh, PASSES, premerge=[appends(marker)])
    at(eng, phase)
    restored = eng.config
    changed = config(PASSES)
    changed.merge.verification_commands = [appends(marker)]
    change(changed)
    eng2 = reopened(eng, tmp_state_dir, gh, cfg=changed)
    before, reads = runs(marker), comment_reads(gh)
    out = eng2.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    s = saved(eng2)
    assert f"the review certificate of round 1 for HEAD {head[:12]} does not apply" in (
        s.block_reason
    )
    assert named in s.block_reason and "since it was issued" in s.block_reason
    assert "Restore the configuration and 'unblock'" in s.block_reason
    assert runs(marker) == before and comment_reads(gh) == reads
    assert gh.merges == [] and s.counted_merged_prs == []
    assert s.review_certificate["policy_digest"] == policy_digest(eng), "never rewritten"
    assert status_applicable(eng2).startswith(f"Applicable: no: {named}")

    eng3 = reopened(eng2, tmp_state_dir, gh, cfg=restored)
    decision = eng3.unblock(REASON)
    assert decision.unblocked and decision.phase == "READY_FOR_MERGE"
    assert "review certificate of round 1 applies" in decision.message
    assert merges_through(eng3) == ["MERGE", "UPDATE_EPIC"]
    assert gh.merges == [(PR, "squash", head)]
    assert saved(eng3).review_round == 1, "no new round"


def test_unblock_under_the_changed_configuration_runs_a_new_validated_round(tmp_state_dir):
    """No automatic re-review: the operator's unblock decides REVIEW, whose
    entry validates under the configuration in force, and its certificate
    names that policy."""
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    changed = config(PASSES)
    _timeout(changed)
    eng2 = reopened(eng, tmp_state_dir, gh, cfg=changed)
    old = saved(eng2).review_certificate
    assert eng2.step(allow_merge=True).next_phase == "BLOCKED"
    assert eng2.provider.calls == [], "no reviewer relaunched by itself"
    decision = eng2.unblock(REASON)
    assert decision.unblocked and decision.phase == "REVIEW"
    assert "the validation policy changed" in decision.message
    eng2.provider._handler = scripted(clean_reviewer(eng2, 2))
    assert eng2.step().next_phase == "READY_FOR_MERGE"
    s = saved(eng2)
    cert = s.review_certificate
    assert cert["review_round"] == 2 and cert["policy_digest"] == policy_digest(eng2)
    assert cert["policy_digest"] != old["policy_digest"]
    (rerun,) = [
        v
        for v in s.validation_evidence()
        if v.candidate.commit == head and v.policy_digest == policy_digest(eng2)
    ]
    assert cert["records"] == [{"seq": rerun.seq, "digest": rerun.digest}]
    assert merges_through(eng2) == ["MERGE", "UPDATE_EPIC"]


def _edited_digest(eng, cert):
    return {**cert, "issued_at": "2020-01-01T00:00:00Z"}, "its digest does not verify"


def _another_pr(eng, cert):
    other = "https://github.com/owner/repo/pull/43"
    comment = dict(cert["comment"], url=comment_url(other, cert["comment"]["id"]))
    return resealed(cert, reviewed_pr_url=other, comment=comment), "its reviewed_pr_url"


def _another_base(eng, cert):
    key = dict(cert["comment"]["marker_key"], reviewed_base_ref="develop")
    comment = dict(cert["comment"], marker_key=key)
    return resealed(cert, reviewed_base_ref="develop", comment=comment), "its reviewed_base_ref"


def _another_merge_base(eng, cert):
    other = "0" * 40
    key = dict(cert["comment"]["marker_key"], reviewed_merge_base_sha=other)
    comment = dict(cert["comment"], marker_key=key)
    edited = resealed(cert, reviewed_merge_base_sha=other, comment=comment)
    return edited, "its reviewed_merge_base_sha"


def _another_comment(eng, cert):
    comment = dict(cert["comment"], url=comment_url(PR, 999_999), id=999_999)
    return resealed(cert, comment=comment), "its review comment"


def _another_round(eng, cert):
    key = dict(cert["comment"]["marker_key"], review_round=7)
    comment = dict(cert["comment"], marker_key=key)
    edited = resealed(cert, review_round=7, comment=comment)
    return edited, "it is of review round 7, not of the completed round 1"


def _a_record_missing(eng, cert):
    eng.state.validation_records = []
    return cert, "it does not load"


def _a_record_not_pass(eng, cert):
    """A FAIL for the same candidate and definition, named in the PASS record's place."""
    passed = eng.state.validation_records[0]
    failed = observed_record(
        seq=passed["seq"] + 1,
        candidate=parse_candidate(passed["candidate"]),
        definition=Definition(0, tuple(PASSES)),
        policy=eng._validation_policy(LaunchClass.EXPORT_COMMAND),
        result=ExecutionResult(
            command=list(PASSES),
            cwd="/x",
            exit_code=1,
            stdout="",
            stderr="",
            started_at=passed["started_at"],
            finished_at=passed["finished_at"],
        ),
        run_log=passed["run_log"],
    )
    eng.state.validation_records.append(failed)
    edited = resealed(cert, records=[{"seq": failed["seq"], "digest": failed["digest"]}])
    return edited, "a certificate names PASS records only"


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
@pytest.mark.parametrize(
    "edit",
    [
        _edited_digest,
        _another_pr,
        _another_base,
        _another_merge_base,
        _another_comment,
        _another_round,
        _a_record_missing,
        _a_record_not_pass,
    ],
)
def test_each_certificate_requirement_failing_alone_blocks_naming_it(
    tmp_state_dir, tmp_path, phase, edit
):
    """D13.2, one requirement at a time, with the PR at the reviewed revision
    and every other gate passing: never coerced, nothing run or written."""
    gh = FakeGitHub()
    marker = tmp_path / "premerge.txt"
    eng, head = ready(tmp_state_dir, gh, PASSES, premerge=[appends(marker)])
    at(eng, phase)
    edited, named = edit(eng, eng.state.review_certificate)
    eng.state.review_certificate = edited
    before, reads = runs(marker), comment_reads(gh)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED", out.message
    reason = eng.state.block_reason
    assert reason.startswith(f"{NO_CERTIFICATE} {head}: the review certificate held does not")
    assert named in reason and "it is never coerced" in reason
    assert "'unblock': with no applicable certificate it decides REVIEW" in reason
    assert runs(marker) == before and comment_reads(gh) == reads
    assert gh.merges == [] and eng.state.counted_merged_prs == [] and eng.state.attempt == 0


def _head_moved(eng, gh, head):
    a_human_push(eng, head)
    return "REVIEW", "PR HEAD moved after the clean review"


def _ci_fails(eng, gh, head):
    gh.prs[PR].checks = [ci_check(conclusion="FAILURE")]
    return "BLOCKED", "failing or inconclusive checks: ci"


def _verification_fails(eng, gh, head):
    eng.config.merge.verification_commands = [FAILS]
    return "BLOCKED", "failed with exit 1"


def _comment_gone(eng, gh, head):
    gh.comments[PR] = []
    return "BLOCKED", "comment not found"


def _behind(eng, gh, head):
    gh.prs[PR].merge_state_status = "BEHIND"
    return "BLOCKED", "mergeStateStatus=BEHIND"


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
@pytest.mark.parametrize(
    "refuse", [_head_moved, _ci_fails, _verification_fails, _comment_gone, _behind]
)
def test_every_other_gate_still_refuses_with_an_applicable_certificate(
    tmp_state_dir, phase, refuse
):
    """D13.3: the certificate removes no gate. HEAD drift still routes to
    REVIEW before the certificate is consulted; the rest refuse on their own."""
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    at(eng, phase)
    assert eng.review_certificate_problem() == ""
    expected, named = refuse(eng, gh, head)
    out = eng.step(allow_merge=True)
    assert out.next_phase == expected, out.message
    assert named in (out.message if expected == "REVIEW" else eng.state.block_reason)
    assert NO_CERTIFICATE not in out.message
    assert gh.merges == [] and saved(eng).counted_merged_prs == []


def test_a_transient_comment_read_with_a_certificate_held_is_rechecked(tmp_state_dir):
    from autoforge.errors import GitHubUnavailableError, VerificationError

    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    gh.get_comment_error = GitHubUnavailableError("HTTP 502")
    with pytest.raises(VerificationError, match="review-comment read"):
        eng.step(allow_merge=True)
    assert saved(eng).phase == Phase.READY_FOR_MERGE and saved(eng).attempt == 1
    gh.get_comment_error = None
    assert merges_through(eng) == ["MERGE", "UPDATE_EPIC"]


def test_the_gate_stays_closed_with_a_certificate_held(tmp_state_dir):
    from autoforge.errors import VerificationError

    gh = FakeGitHub()
    eng, head = certified(tmp_state_dir, gh, PASSES)
    calls = list(gh.calls)
    with pytest.raises(VerificationError, match="merge is disabled"):
        eng.step(allow_merge=True)
    assert gh.calls == calls and gh.merges == []


def test_a_refused_certificate_is_one_blocked_write(tmp_state_dir, monkeypatch):
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    eng.state.review_certificate = {}
    eng._save()
    real = engine_module.save_state
    writes = []

    def counted(state, *args, **kwargs):
        writes.append(state.phase)
        return real(state, *args, **kwargs)

    monkeypatch.setattr(engine_module, "save_state", counted)
    assert eng.step(allow_merge=True).next_phase == "BLOCKED"
    assert writes == [Phase.BLOCKED]
    s = saved(eng)
    assert s.counted_merged_prs == [] and s.merged_since_epic_update == 0 and gh.merges == []


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
def test_the_dry_run_plan_names_the_certificate_check_from_state(tmp_state_dir, phase):
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    at(eng, phase)
    calls = list(gh.calls)

    def note():
        (line,) = [n for n in eng.step(dry_run=True).plan.notes if "review certificate" in n]
        return line

    assert "before the review comment is re-read or any merge.verification_commands" in note()
    assert note().endswith("the certificate of round 1 applies")
    eng.config.execution.command_timeout_seconds += 1
    assert "it does not apply (the validation policy changed" in note()
    eng.state.review_certificate = {}
    assert note().endswith("none is held, so the gate would enter BLOCKED (unblock decides REVIEW)")
    assert gh.calls == calls, "a dry run reads nothing"


# -- the invalidation matrix (D14.1) ----------------------------------------------------


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
@pytest.mark.parametrize(
    "push",
    [
        lambda eng, head: a_human_push(eng, head),
        lambda eng, head: a_human_push(eng, head, same_tree=True),
        lambda eng, head: a_human_push(eng, f"{head}^"),
    ],
    ids=["new commit", "same tree, another commit", "force push"],
)
def test_a_push_after_the_clean_review_certifies_the_new_head_alone(tmp_state_dir, phase, push):
    """Existing drift -> REVIEW, at the gate pass or (pushed between the gate
    pass and the write) at MERGE. The old certificate stays as history and
    does not apply; the REVIEW entry validates the new commit (the commit is
    part of the key, the same tree included) and its round certifies it."""
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    at(eng, phase)
    old = saved(eng).review_certificate
    new = push(eng, head)
    out = eng.step(allow_merge=True)
    assert out.next_phase == "REVIEW" and "PR HEAD moved" in out.message
    s = saved(eng)
    assert s.review_certificate == old, "kept as history, never rewritten for the new head"
    assert status_applicable(eng) == (
        "Applicable: no: the state's last review result is 'stale', not the clean round it "
        "certifies"
    )
    assert records_for(s, new) == []

    eng.provider._handler = scripted(clean_reviewer(eng, 2))
    assert eng.step().next_phase == "READY_FOR_MERGE"
    s = saved(eng)
    (validated,) = records_for(s, new)
    assert len(records_for(s, head)) == 1, "the old record is retained"
    cert = s.review_certificate
    assert cert["review_round"] == 2 and cert["reviewed_head_sha"] == new
    assert cert["records"] == [{"seq": validated["seq"], "digest": validated["digest"]}]
    assert merges_through(eng) == ["MERGE", "UPDATE_EPIC"]
    assert gh.merges == [(PR, "squash", new)]


def test_unblock_after_a_push_decides_review_and_the_old_certificate_never_applies(
    tmp_state_dir,
):
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    gh.prs[PR].checks = [ci_check(conclusion="FAILURE")]
    assert eng.step(allow_merge=True).next_phase == "BLOCKED"
    gh.prs[PR].checks = [ci_check()]
    new = a_human_push(eng, head)
    decision = eng.unblock(REASON)
    assert decision.unblocked and decision.phase == "REVIEW"
    s = saved(eng)
    assert s.last_review_result == "stale" and s.current_head_sha == new
    assert eng.review_certificate_problem().startswith("the state's last review result is")
    eng.provider._handler = scripted(clean_reviewer(eng, 2))
    assert eng.step().next_phase == "READY_FOR_MERGE"
    assert saved(eng).review_certificate["reviewed_head_sha"] == new


def test_a_base_that_advanced_with_the_merge_base_unchanged_keeps_the_certificate(
    tmp_state_dir,
):
    """The certificate binds the merge base, not the base tip: it still
    applies, the gate merges, and a PR GitHub reports BEHIND is the readiness
    check's business."""
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    merge_base = saved(eng).reviewed_merge_base_sha
    tip = a_human_push(eng, merge_base, branch="main")
    gh.workflow_runs[BASE_RUN_ID] = replace(gh.workflow_runs[BASE_RUN_ID], head_sha=tip)
    assert eng.origin.merge_base("main", head) == merge_base
    assert status_applicable(eng) == "Applicable: yes"
    gh.prs[PR].merge_state_status = "BEHIND"
    assert eng.step(allow_merge=True).next_phase == "BLOCKED"
    assert "BEHIND" in eng.state.block_reason and NO_CERTIFICATE not in eng.state.block_reason
    gh.prs[PR].merge_state_status = "CLEAN"
    eng.unblock(REASON)
    assert merges_through(eng) == ["MERGE", "UPDATE_EPIC"]
    assert gh.merges == [(PR, "squash", head)]
    assert saved(eng).review_round == 1, "no new round"


def _rewritten_base(eng, gh, head):
    """The base rewritten under its name: ``main`` force-pushed to a sibling of its tip."""
    old = eng.origin.head("main")
    sibling = a_human_push(eng, f"{old}^", branch="main")
    gh.workflow_runs[BASE_RUN_ID] = replace(gh.workflow_runs[BASE_RUN_ID], head_sha=sibling)
    return "main", eng.origin.merge_base("main", head), "PR merge base moved"


def _retarget(eng, gh, head):
    tip = eng.origin.head("main")
    eng.origin.publish(eng.workdir, tip, "release")
    gh.prs[PR].base_ref = "release"
    gh.workflow_runs[BASE_RUN_ID] = replace(
        gh.workflow_runs[BASE_RUN_ID], head_branch="release", head_sha=tip
    )
    return "release", tip, "PR base changed to 'release'"


def certified_on_history(tmp_state_dir, gh, *commands):
    """``certified``, on a ``main`` with history, so that a rewrite can move the merge base."""
    eng = make_engine(tmp_state_dir, None, github=gh, cfg=config(*commands), origin=True)
    second = a_human_push(eng, eng.origin.head("main"), branch="main")
    gh.workflow_runs[BASE_RUN_ID] = replace(gh.workflow_runs[BASE_RUN_ID], head_sha=second)
    head = publish_pr_head(eng, parent=second)
    gh.add_pr(head_sha=head)
    eng.state.phase = Phase.REVIEW
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = head
    eng.state.current_base_ref = "main"
    eng.provider._handler = scripted(clean_reviewer(eng))
    assert eng.step().next_phase == "READY_FOR_MERGE"
    eng.config.safety.allow_merge = True
    assert saved(eng).reviewed_merge_base_sha == second
    return eng, head


@pytest.mark.parametrize("move", [_rewritten_base, _retarget], ids=["rewritten base", "retarget"])
def test_a_moved_base_routes_to_review_reuses_the_records_and_certifies_the_new_base(
    tmp_state_dir, move
):
    """Candidate-tree records stay applicable (the commit did not move); the
    certificate does not, and the new round issues one for the new base."""
    gh = FakeGitHub()
    eng, head = certified_on_history(tmp_state_dir, gh, PASSES)
    s = saved(eng)
    old, records = s.review_certificate, s.validation_records
    base, merge_base, what = move(eng, gh, head)
    assert (base, merge_base) != (old["reviewed_base_ref"], old["reviewed_merge_base_sha"])
    out = eng.step(allow_merge=True)
    assert out.next_phase == "REVIEW" and what in out.message
    assert saved(eng).review_certificate == old
    assert status_applicable(eng).startswith("Applicable: no: the state's last review result")

    eng.provider._handler = scripted(clean_reviewer(eng, 2))
    assert eng.step().next_phase == "READY_FOR_MERGE"
    s = saved(eng)
    assert s.validation_records == records, "the records were reused: no validator ran"
    cert = s.review_certificate
    assert cert["records"] == old["records"] and cert["review_round"] == 2
    assert cert["candidate"] == old["candidate"]
    assert (
        cert["reviewed_head_sha"],
        cert["reviewed_base_ref"],
        cert["reviewed_merge_base_sha"],
    ) == (head, base, merge_base)
    assert merges_through(eng) == ["MERGE", "UPDATE_EPIC"]
    assert gh.merges == [(PR, "squash", head)]


def _edited_to_needs_fix(gh, head, merge_base):
    return review_comment_body(1, head, True, base_ref="main", merge_base_sha=merge_base)


def _doubled(gh, head, merge_base):
    (comment,) = gh.comments[PR]
    return comment.body + "\n\n" + comment.body


@pytest.mark.parametrize("phase", [Phase.READY_FOR_MERGE, Phase.MERGE])
@pytest.mark.parametrize(
    ("edit", "named"),
    [
        (_edited_to_needs_fix, "says needs_fix_round: true"),
        (_doubled, "is not the clean review in state"),
    ],
    ids=["edited", "doubled"],
)
def test_an_edited_or_doubled_review_comment_is_refused_by_the_re_read(
    tmp_state_dir, tmp_path, phase, edit, named
):
    """The certificate applies (it names the comment the state names); the
    #94 re-read of what that comment now says refuses, before any command."""
    gh = FakeGitHub()
    marker = tmp_path / "premerge.txt"
    eng, head = ready(tmp_state_dir, gh, PASSES, premerge=[appends(marker)])
    at(eng, phase)
    merge_base = saved(eng).reviewed_merge_base_sha
    (comment,) = gh.comments[PR]
    gh.comments[PR] = [replace(comment, body=edit(gh, head, merge_base))]
    before = runs(marker)
    assert eng.review_certificate_problem() == ""
    out = eng.step(allow_merge=True)
    assert out.next_phase == "BLOCKED"
    reason = saved(eng).block_reason
    assert named in reason and "not backed by GitHub" in reason
    assert NO_CERTIFICATE not in reason
    assert runs(marker) == before and gh.merges == []


def _finding(round_: int) -> dict:
    return {
        "id": f"R{round_}-F1",
        "classification": "non-blocked",
        "title": "A regression test is missing",
        "location": "src/feature.py",
        "required_resolution": "add a regression test",
    }


def test_an_old_certificate_stays_as_history_and_the_next_producer_validates_a_new_set(
    tmp_state_dir,
):
    """Round 1 certifies HEAD A. A push to B and a needs-fix round 2 leave
    round 1's certificate in state, untouched and inapplicable. The definition
    set then changes: the FIX producer validates its commit under the new set,
    the REVIEW entry reuses those records, and round 3 certifies the fix
    commit under the new set alone."""
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    first = saved(eng).review_certificate
    pushed = a_human_push(eng, head)
    assert eng.step(allow_merge=True).next_phase == "REVIEW"
    eng.provider._handler = scripted(lambda req: block(review_result(2, pushed, [_finding(2)])))
    assert eng.step().next_phase == "FIX"
    s = saved(eng)
    assert s.review_certificate == first, "kept, never rewritten"
    assert (
        eng.review_certificate_problem() == "it is of review round 1, not of the completed round 2"
    )
    assert status_applicable(eng) == (
        "Applicable: no: it is of review round 1, not of the completed round 2"
    )

    changed = config(PASSES, ALSO_PASSES)
    changed.safety.allow_merge = True
    eng2 = reopened(eng, tmp_state_dir, gh, scripted(fixer(fixed("R2-F1"))), cfg=changed)
    assert eng2.step().next_phase == "REVIEW"
    s = saved(eng2)
    fix = s.current_head_sha
    assert fix != pushed and len(records_for(s, fix)) == 2, "the producer ran the new set"
    eng2.provider._handler = scripted(clean_reviewer(eng2, 3))
    validated = list(s.validation_records)
    assert eng2.step().next_phase == "READY_FOR_MERGE"
    s = saved(eng2)
    assert s.validation_records == validated, "the REVIEW entry reused the producer's records"
    cert = s.review_certificate
    assert cert["review_round"] == 3 and cert["reviewed_head_sha"] == fix
    assert cert["records"] == [
        {"seq": r["seq"], "digest": r["digest"]} for r in records_for(s, fix)
    ]
    assert cert["validation_set_digest"] != first["validation_set_digest"]
    assert merges_through(eng2) == ["MERGE", "UPDATE_EPIC"]
    assert gh.merges == [(PR, "squash", fix)]


def test_a_replan_replacement_inherits_no_certificate(tmp_state_dir):
    """The source PR's certificate stays in state as history after the
    activation write (which is unchanged), and never applies to the
    replacement: its PR, candidate and round all differ. The replacement's
    producer validates its own commit, and its own clean round 1 replaces
    the certificate; the gate then merges the replacement."""
    from autoforge.validation import parse_pr_url

    from .conftest import certify_review
    from .test_replan import (
        REPLACEMENT_PR,
        _park_at_hard_threshold,
        _reimplement,
        _replan_result,
        _reviewed_head,
        _trigger_review_payload,
    )

    gh = FakeGitHub()
    script = scripted(
        lambda req: block(_trigger_review_payload(_reviewed_head(req))),
        lambda req: _replan_result(req, _reimplement(req)),
        lambda req: block(review_result(1, _reviewed_head(req))),
    )
    eng = _park_at_hard_threshold(tmp_state_dir, gh, script)
    # The source PR as an earlier clean round of it left the state: certified.
    s = eng.state
    s.reviewed_pr_url = PR
    s.reviewed_head_sha = eng.source
    s.reviewed_base_ref = "main"
    s.reviewed_merge_base_sha = eng.merge_base
    s.last_review_comment_url = comment_url(PR, 300)
    planted = certify_review(
        eng, git_out("-C", str(eng.workdir), "rev-parse", f"{eng.source}^{{tree}}")
    )
    eng.config.validation.commands = [list(PASSES)]
    eng.config.safety.allow_merge = True

    assert eng.step().next_phase == "REPLAN_REEXECUTE"
    assert eng.step().next_phase == "REVIEW"
    s = saved(eng)
    replacement = s.current_head_sha
    assert s.current_pr_url == REPLACEMENT_PR and s.review_round == 0
    assert s.review_certificate == planted, "the old PR's certificate stays as history"
    assert (
        eng.review_certificate_problem() == "it is of review round 19, not of the completed round 0"
    )
    assert planted["reviewed_pr_url"] != parse_pr_url(REPLACEMENT_PR).canonical
    assert planted["candidate"]["commit"] == eng.source != replacement
    (record,) = records_for(s, replacement)

    assert eng.step().next_phase == "READY_FOR_MERGE"
    s = saved(eng)
    cert = s.review_certificate
    assert cert["reviewed_pr_url"] == parse_pr_url(REPLACEMENT_PR).canonical
    assert (cert["review_round"], cert["candidate"]["commit"]) == (1, replacement)
    assert cert["records"] == [{"seq": record["seq"], "digest": record["digest"]}]
    assert records_for(s, eng.source), "the source's records stay as history too"
    assert merges_through(eng) == ["MERGE", "UPDATE_EPIC"]
    assert gh.merges == [(REPLACEMENT_PR, "squash", replacement)]
    assert gh.prs[PR].state == "CLOSED"


# -- LOCAL: zero GitHub calls (D12.5) ---------------------------------------------------


def _local_evidence_for(eng, fingerprint: str):
    s = eng.state
    configured = eng._configured_validation()
    candidate = LocalCandidate(fingerprint, s.local_contract().workspace_policy.digest(), s.run_id)
    return find_evidence(
        s.validation_evidence(), candidate, configured.definitions, configured.policy, None
    )


def test_local_fingerprint_drift_before_review_certifies_nothing(tmp_path):
    """Behind the exploding GitHub. The tree changes after the write phase
    validated it: the existing drift handling blocks REVIEW, no certificate
    is issued, and the records of the verified workspace are no evidence for
    the changed one."""
    from .local_helpers import touch_impl
    from .test_review_certificate import implemented, local_run, reviewed

    eng, root = local_run(tmp_path, [implemented(), reviewed()], PASSES)
    eng.step()
    assert eng.step().next_phase == "REVIEW"
    verified = eng.state.workspace_fingerprint
    assert _local_evidence_for(eng, verified).complete
    touch_impl(root, "an edit nothing validated\n")
    assert eng.step().next_phase == "BLOCKED"
    s = saved(eng)
    assert s.review_certificate == {} and s.review_round == 0
    now = eng.local_workspace().snapshot().fingerprint
    assert now != verified and _local_evidence_for(eng, now).records == ()


def test_local_a_certificate_names_only_the_workspace_its_round_reviewed(tmp_path):
    """DONE holds the certificate of the reviewed workspace F. An edit after
    it is F': no record is evidence for F', and against a state bound to F'
    the certificate does not apply."""
    from .local_helpers import touch_impl
    from .test_review_certificate import implemented, local_run, reviewed

    eng, root = local_run(tmp_path, [implemented(), reviewed()], PASSES)
    assert [o.next_phase for o in eng.run(max_steps=5)] == ["ANALYZE_EXECUTE", "REVIEW", "DONE"]
    s = saved(eng)
    reviewed_fingerprint = s.reviewed_workspace_fingerprint
    assert s.review_certificate["reviewed_workspace_fingerprint"] == reviewed_fingerprint
    assert eng.review_certificate_problem() == ""
    touch_impl(root, "an edit after DONE\n")
    now = eng.local_workspace().snapshot().fingerprint
    assert now != reviewed_fingerprint and _local_evidence_for(eng, now).records == ()
    eng.state.reviewed_workspace_fingerprint = now
    eng.state.workspace_fingerprint = now
    assert eng.review_certificate_problem().startswith("its reviewed workspace")


# -- the MERGE crash windows and recovery (§2.16) ---------------------------------------


def test_merge_after_a_saved_gate_pass_reruns_the_whole_gate(tmp_state_dir, monkeypatch):
    """The READY_FOR_MERGE -> MERGE write is made and the process dies: MERGE
    checks the certificate against the live PR again, and refuses one the
    configuration in force no longer matches."""
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    at(eng, Phase.MERGE)
    restored = eng.config
    changed = config(PASSES)
    _allow_list(changed)
    eng2 = reopened(eng, tmp_state_dir, gh, cfg=changed)
    assert eng2.step(allow_merge=True).next_phase == "BLOCKED"
    assert "the validation policy changed" in saved(eng2).block_reason
    assert gh.merges == []

    real = engine_module.certificate_problem
    lives = []

    def spied(certificate, state, configured, live=None):
        lives.append(live)
        return real(certificate, state, configured, live)

    monkeypatch.setattr(engine_module, "certificate_problem", spied)
    eng3 = reopened(eng2, tmp_state_dir, gh, cfg=restored)
    assert eng3.unblock(REASON).phase == "READY_FOR_MERGE"
    lives.clear()
    assert merges_through(eng3) == ["MERGE", "UPDATE_EPIC"]
    merge_base = saved(eng3).reviewed_merge_base_sha
    live = [revision for revision in lives if revision is not None]  # None: the plan's note
    assert live == [Revision(PR, head, "main", merge_base)] * 2, "each pass checked it live"


@pytest.mark.parametrize("held", [True, False], ids=["certificate", "legacy"])
def test_a_crash_after_the_merge_call_is_recovered_and_counted_once(tmp_state_dir, held):
    """D12.4: counting a merge already made is not a merge decision. No second
    merge, one count, and no certificate is issued or rewritten; a legacy
    state (none held: saved in MERGE by a controller from before
    certificates) is reconciled exactly the same way."""
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    at(eng, Phase.MERGE)
    real = gh.merge_pr

    def merge_then_die(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    gh.merge_pr = merge_then_die
    with pytest.raises(KeyboardInterrupt):
        eng.step(allow_merge=True)
    gh.merge_pr = real
    s = saved(eng)
    assert s.phase == Phase.MERGE and gh.prs[PR].state == "MERGED"
    if not held:
        s.review_certificate = {}
        eng.state = s
        eng._save()
    cert = saved(eng).review_certificate

    eng2 = reopened(eng, tmp_state_dir, gh)
    out = eng2.step(allow_merge=True)
    assert out.next_phase == "UPDATE_EPIC" and "already MERGED on GitHub (recovered)" in out.message
    s = saved(eng2)
    assert len(gh.merges) == 1 and s.counted_merged_prs == [PR]
    assert s.merged_since_epic_update == 1
    assert s.review_certificate == cert


def test_a_legacy_merged_pr_found_from_ready_for_merge_is_counted_as_today(tmp_state_dir):
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    eng.state.review_certificate = {}
    eng._save()
    gh.prs[PR].state = "MERGED"  # a human merged it at the reviewed revision
    assert merges_through(eng) == ["MERGE", "UPDATE_EPIC"]
    s = saved(eng)
    assert gh.merges == [] and s.counted_merged_prs == [PR] and s.review_certificate == {}


def test_a_crash_during_merge_verification_reruns_it_and_the_certificate_check(
    tmp_state_dir, tmp_path, monkeypatch
):
    """The command ran and the controller died before its result was saved:
    the existing rerun rule (nothing cached), and the next pass checks the
    certificate again before running it."""
    gh = FakeGitHub()
    marker = tmp_path / "premerge.txt"
    eng, head = ready(tmp_state_dir, gh, PASSES, premerge=[appends(marker)])

    def ran_then_died(req):
        result = execute(req)
        if not is_shell_probe(req) and req.command == appends(marker):
            raise KeyboardInterrupt
        return result

    eng._runner = ran_then_died
    with pytest.raises(KeyboardInterrupt):
        eng.step(allow_merge=True)
    s = saved(eng)
    assert s.phase == Phase.READY_FOR_MERGE and runs(marker) == 1
    assert s.premerge_verified_head_sha == "", "the cache records only an observed pass"

    real = engine_module.certificate_problem
    checked = []

    def spied(certificate, state, configured, live=None):
        if live is not None:
            checked.append(runs(marker))
        return real(certificate, state, configured, live)

    monkeypatch.setattr(engine_module, "certificate_problem", spied)
    eng2 = reopened(eng, tmp_state_dir, gh)
    assert merges_through(eng2) == ["MERGE", "UPDATE_EPIC"]
    assert checked == [1, 2], "checked before the rerun at the gate pass, and again at MERGE"
    assert runs(marker) == 2 and gh.merges == [(PR, "squash", head)]


def test_a_push_while_the_review_entry_validates_certifies_only_the_new_head(tmp_state_dir):
    """§2.16, the candidate moved during validation: the records stay true
    for the commit they ran on, the reviewer reviews that commit, the
    re-read after the review finds the push (a stale round, no
    certificate), and the next round validates and certifies the new head."""
    gh = FakeGitHub()
    eng, head = reviewing(tmp_state_dir, gh, None, PASSES)
    eng.config.safety.allow_merge = True
    pushed = []

    def pushed_while_validating(req):
        result = execute(req)
        if not is_shell_probe(req) and req.command == list(PASSES) and not pushed:
            pushed.append(a_human_push(eng, head))
        return result

    eng._runner = pushed_while_validating
    eng.provider._handler = scripted(clean_reviewer(eng))
    out = eng.step()
    assert out.next_phase == "REVIEW", out.message
    (new,) = pushed
    s = saved(eng)
    assert s.last_review_result == "stale" and s.current_head_sha == new
    assert s.review_certificate == {}, "a stale round certifies nothing"
    (record,) = records_for(s, head)
    assert record["outcome"] == "PASS", "true for the commit it ran on"
    assert records_for(s, new) == []

    eng.provider._handler = scripted(clean_reviewer(eng, 2))
    assert eng.step().next_phase == "READY_FOR_MERGE"
    s = saved(eng)
    (validated,) = records_for(s, new)
    cert = s.review_certificate
    assert (cert["review_round"], cert["candidate"]["commit"]) == (2, new)
    assert cert["records"] == [{"seq": validated["seq"], "digest": validated["digest"]}]
    assert merges_through(eng) == ["MERGE", "UPDATE_EPIC"]
    assert gh.merges == [(PR, "squash", new)]


def test_a_restart_with_a_valid_certificate_merges(tmp_state_dir):
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    eng2 = reopened(eng, tmp_state_dir, gh, ["no agent runs at the gate"])
    assert merges_through(eng2) == ["MERGE", "UPDATE_EPIC"]
    assert eng2.provider.calls == [] and gh.merges == [(PR, "squash", head)]


@pytest.mark.parametrize("phase", [Phase.REVIEW, Phase.READY_FOR_MERGE, Phase.MERGE, Phase.BLOCKED])
def test_a_legacy_state_in_each_phase_reaches_a_validated_certified_merge(tmp_state_dir, phase):
    """A state from before the evidence existed loads with every evidence
    field empty (D15.4). REVIEW validates and certifies at its own write;
    the gate blocks one in READY_FOR_MERGE or MERGE, and an unblock (as
    from BLOCKED) decides a fresh validated REVIEW round."""
    gh = FakeGitHub()
    if phase == Phase.REVIEW:
        eng, head = reviewing(tmp_state_dir, gh, None, PASSES)
        eng.config.safety.allow_merge = True
        eng._save()
        rounds = 1
    else:
        eng, head = ready(tmp_state_dir, gh, PASSES)
        at(eng, Phase.MERGE if phase == Phase.MERGE else Phase.READY_FOR_MERGE)
        if phase == Phase.BLOCKED:
            eng._block(eng.state.phase, None, "a required check failed; it was rerun")
        eng.state.validation_records = []
        eng.state.review_evidence = {}
        eng.state.review_certificate = {}
        eng._save()
        if phase != Phase.BLOCKED:
            assert eng.step(allow_merge=True).next_phase == "BLOCKED"
            assert saved(eng).block_reason.startswith(f"{NO_CERTIFICATE} {head}")
        assert eng.unblock(REASON).phase == "REVIEW"
        rounds = 2
    eng.provider._handler = scripted(clean_reviewer(eng, rounds))
    assert eng.step().next_phase == "READY_FOR_MERGE"
    s = saved(eng)
    (record,) = records_for(s, head)
    cert = s.review_certificate
    assert cert["review_round"] == rounds and cert["reviewed_head_sha"] == head
    assert cert["records"] == [{"seq": record["seq"], "digest": record["digest"]}]
    assert merges_through(eng) == ["MERGE", "UPDATE_EPIC"]


# -- resume holding READY_FOR_MERGE (contract item 3) -----------------------------------


def test_resume_with_the_gate_closed_says_whether_the_certificate_applies(
    tmp_state_dir, monkeypatch, capsys
):
    """From state and the configuration in force: GitHub is not read."""
    gh = FakeGitHub()
    eng, head = certified(tmp_state_dir, gh, PASSES)
    monkeypatch.setattr(cli, "_engine_for", lambda args, cfg=None: eng)
    calls = list(gh.calls)

    def banner() -> str:
        assert cli.main(["--state-dir", str(tmp_state_dir), "resume"]) == 0
        out = capsys.readouterr().out
        assert "AutoForge workflow reached READY_FOR_MERGE." in out
        assert "The merge gate is closed: a human must review and merge the PR." in out
        (line,) = [line for line in out.splitlines() if "Review certificate:" in line]
        return line.strip()

    assert banner() == (
        "Review certificate: applies (round 1, against state and the configuration in force; "
        "the merge gate checks it against GitHub too)"
    )
    eng.config.execution.command_timeout_seconds += 1
    assert banner().startswith("Review certificate: does not apply (the validation policy changed")
    assert banner().endswith("the merge gate would refuse to merge")
    eng.config.execution.command_timeout_seconds -= 1
    s = saved(eng)
    s.review_certificate = {}
    eng.state = s
    eng._save()
    assert banner() == "Review certificate: none held; the merge gate would refuse to merge"
    assert gh.calls == calls and gh.merges == []


def test_resume_with_the_gate_open_runs_the_full_gate_certificate_included(tmp_state_dir):
    gh = FakeGitHub()
    eng, head = ready(tmp_state_dir, gh, PASSES)
    assert cli._resume_holding_state(eng, eng.state, allow_merge=True) is None
    eng.state.review_certificate = {}
    eng._save()
    outcomes = eng.run(allow_merge=True)
    assert [o.next_phase for o in outcomes] == ["BLOCKED"]
    assert saved(eng).block_reason.startswith(f"{NO_CERTIFICATE} {head}")
    assert gh.merges == []


# -- provider parity (D17.2) ------------------------------------------------------------


def _normalized(text: str, s) -> str:
    for value, token in (
        (s.reviewed_head_sha, "<HEAD>"),
        (s.reviewed_merge_base_sha, "<MERGE_BASE>"),
        (s.run_id, "<RUN>"),
    ):
        text = text.replace(value, token).replace(value[:12], token)
    return re.sub(r"(policy|set) [0-9a-f]{16}\.\.\.", r"\1 <DIGEST>...", text)


def test_the_gate_routes_the_same_behind_every_provider(tmp_path_factory):
    """The same implementer and reviewer behind Claude, OpenCode and Pi: the
    same refusal of a changed policy, the same unblock and the same merge."""
    seen = []
    for provider in ("claude", "opencode", "pi"):
        gh = FakeGitHub()
        state_dir = tmp_path_factory.mktemp(provider) / ".autoforge"
        heads: list[str] = []

        def implementer(req, heads=heads):
            heads.append(commit_in(req.cwd))
            return block(analyze_payload(heads[-1]))

        def reviewer(req, heads=heads):
            return block(review_result(1, heads[-1]))

        script = scripted(implementer, reviewer)
        eng = make_engine(state_dir, script, github=gh, cfg=config(PASSES), origin=True)
        _route(eng, provider, tmp_path_factory, script)
        eng.config.safety.allow_merge = True
        messages = [eng.step().message for _ in range(3)]
        eng.config.execution.command_timeout_seconds += 1
        messages.append(eng.step(allow_merge=True).message)
        eng.config.execution.command_timeout_seconds -= 1
        messages.append(eng.unblock(REASON).message)
        messages += [eng.step(allow_merge=True).message for _ in range(2)]
        s = saved(eng)
        assert s.phase == Phase.UPDATE_EPIC and gh.merges == [(PR, "squash", heads[-1])]
        seen.append([_normalized(m, s) for m in messages[2:]])
        if provider == "pi":
            assert eng.provider.calls == [], "the Pi fake answered, not the scripted provider"
    assert seen[0] == seen[1] == seen[2]
    assert "does not apply: the validation policy changed" in seen[0][1]
