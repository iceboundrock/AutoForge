"""The review certificate in the engine (ADR 0006 D12, #174).

End to end and offline: real git repositories and a local bare origin under
``tmp_path``, real Python validators, the in-memory GitHub and scripted
agents (the Pi fake for parity). What is covered:

- issuance in the write that applies a clean round, ``REVIEW ->
  READY_FOR_MERGE`` and LOCAL ``REVIEW -> DONE``, from the round's binding,
  the review binding and the comment the controller posted (D12.2), and the
  crash windows around it, #162's journal included;
- configuration drift at issuance (D12.6) and inconsistent inputs, which
  enter ``BLOCKED`` instead;
- the OPEN-PR unblock, which needs an applicable certificate to decide
  ``READY_FOR_MERGE`` (D12.3), and the MERGED one, which does not (D12.4);
- ``status``, and that no prompt or published payload carries a certificate
  (D1.3);
- the same certificate behind every provider (D17.2).

The pure issuance and applicability rules are in ``tests/test_certificate.py``.
"""

from __future__ import annotations

import json
import re
import sys

import pytest

from autoforge import cli
from autoforge import engine as engine_module
from autoforge.certificate import (
    NOT_A_PERMISSION,
    CertificateIssuanceError,
    Configured,
    Revision,
    local_certificate,
)
from autoforge.config import default_config
from autoforge.errors import StateError
from autoforge.evidence import Definition, observed_record, parse_candidate, set_digest
from autoforge.executor import ExecutionResult
from autoforge.sandbox_policy import LaunchClass
from autoforge.state import load_state
from autoforge.transitions import Phase

from .conftest import (
    BRANCH,
    PR,
    FakeGitHub,
    analyze_payload,
    block,
    commit_in,
    connect_origin,
    controller_review_comment,
    git_out,
    make_engine,
    publish_pr_head,
    read_context_of,
    review_result,
    scripted,
)
from .local_helpers import finding as local_finding
from .local_helpers import fix_result as local_fix_result
from .local_helpers import impl_result, local_repo, touch_impl
from .local_helpers import review_result as local_review_result
from .local_helpers import scripted as local_scripted
from .test_validation_gates import _route, failing_finding

PY = sys.executable
PASSES = [PY, "-c", "pass"]
ALSO_PASSES = [PY, "-c", "import sys; sys.exit(0)"]
REASON = "the operator restored what the block names"


def config(*commands: list[str]):
    cfg = default_config()
    cfg.validation.commands = [list(c) for c in commands]
    return cfg


def saved(eng):
    return load_state(eng.paths.state_file)


def resumed(eng, tmp_state_dir, gh, script=None, cfg=None):
    """A new controller process on the same state, git remote and GitHub."""
    eng.close()
    eng2 = make_engine(tmp_state_dir, script, github=gh, cfg=cfg or eng.config)
    connect_origin(eng2, eng.origin)
    eng2.origin = eng.origin
    eng2.load()
    return eng2


def reviewing(tmp_state_dir, gh, script, *commands):
    """A REMOTE run in REVIEW (round 1 upcoming) of a PR at a real commit on the origin."""
    eng = make_engine(tmp_state_dir, script, github=gh, cfg=config(*commands), origin=True)
    head = publish_pr_head(eng)
    gh.add_pr(head_sha=head)
    eng.state.phase = Phase.REVIEW
    eng.state.current_pr_url = PR
    eng.state.current_branch = BRANCH
    eng.state.current_head_sha = head
    eng.state.current_base_ref = "main"
    return eng, head


def clean_reviewer(eng, round_: int = 1):
    return lambda req: block(review_result(round_, eng.state.current_head_sha))


def certified(tmp_state_dir, gh, *commands):
    """A REMOTE run that reviewed its PR clean and entered READY_FOR_MERGE."""
    eng, head = reviewing(tmp_state_dir, gh, None, *commands)
    eng.provider._handler = scripted(clean_reviewer(eng))
    assert eng.step().next_phase == "READY_FOR_MERGE"
    return eng, head


def comments_of(gh) -> list:
    return list(gh.comments.get(PR, []))


def publish_then_crash(gh) -> None:
    """The review comment lands on GitHub and the controller dies before saving it."""
    gh.write_failures.append(("create_pr_comment", KeyboardInterrupt(), True))


def crashed_after_publication(tmp_state_dir, gh, *commands):
    """REVIEW whose clean comment is published, its state not yet saved (#162's window)."""
    eng, head = reviewing(tmp_state_dir, gh, None, *commands)
    eng.provider._handler = scripted(clean_reviewer(eng))
    publish_then_crash(gh)
    with pytest.raises(KeyboardInterrupt):
        eng.step()
    s = saved(eng)
    assert s.phase == Phase.REVIEW and s.completion_context, "the plan was saved"
    assert s.review_certificate == {} and s.review_round == 0
    assert len(comments_of(gh)) == 1
    return eng, head


def policy_digest(eng) -> str:
    return eng._validation_policy(LaunchClass.EXPORT_COMMAND).digest


# -- issuance: REVIEW -> READY_FOR_MERGE (D12.2) ---------------------------------------


@pytest.mark.parametrize("commands", [[PASSES, ALSO_PASSES], []], ids=["set", "empty set"])
def test_a_clean_round_writes_a_certificate_of_its_exact_inputs(tmp_state_dir, commands):
    gh = FakeGitHub()
    eng, head = certified(tmp_state_dir, gh, *commands)
    s = saved(eng)
    cert, bound = s.review_certificate, s.review_evidence
    assert s.phase == Phase.READY_FOR_MERGE
    # The exact candidate: the reviewed HEAD's commit and the tree it names.
    assert cert["kind"] == "remote"
    assert cert["candidate"] == bound["candidate"]
    assert cert["candidate"]["commit"] == head == s.reviewed_head_sha
    assert cert["candidate"]["tree"] == git_out(
        "-C", str(eng.workdir), "rev-parse", f"{head}^{{tree}}"
    )
    # The exact validation set: the binding's records, in order, and its digests.
    assert cert["records"] == bound["records"]
    assert len(cert["records"]) == len(commands)
    assert cert["validation_set_digest"] == bound["validation_set_digest"]
    assert cert["validation_set_digest"] == set_digest(
        [Definition(i, tuple(c)).digest for i, c in enumerate(commands)]
    )
    assert cert["policy_digest"] == bound["policy_digest"] == policy_digest(eng)
    # The exact review binding and the comment the controller posted and read back.
    comment = controller_review_comment(eng, 1)
    assert (
        cert["review_round"],
        cert["reviewed_pr_url"],
        cert["reviewed_head_sha"],
        cert["reviewed_base_ref"],
        cert["reviewed_merge_base_sha"],
    ) == (
        1,
        s.reviewed_pr_url,
        s.reviewed_head_sha,
        s.reviewed_base_ref,
        s.reviewed_merge_base_sha,
    )
    assert cert["comment"]["url"] == comment.url == s.last_review_comment_url
    assert cert["comment"]["id"] == comment.id
    assert cert["comment"]["marker_key"] == {
        "review_round": 1,
        "reviewed_head_sha": head,
        "reviewed_base_ref": "main",
        "reviewed_merge_base_sha": s.reviewed_merge_base_sha,
    }
    assert cert["clean"] == {"needs_fix_round": False, "actionable_findings": 0}
    assert eng.review_certificate_problem() == ""
    pr = gh.get_pr(PR)
    live = Revision(pr.url, pr.head_sha, pr.base_ref, s.reviewed_merge_base_sha)
    assert eng.review_certificate_problem(live) == ""


def test_a_needs_fix_round_writes_no_certificate(tmp_state_dir):
    gh = FakeGitHub()
    eng, head = reviewing(tmp_state_dir, gh, None, PASSES)
    eng.provider._handler = scripted(lambda req: block(review_result(1, head, [failing_finding()])))
    assert eng.step().next_phase == "FIX"
    assert saved(eng).review_certificate == {}


def test_the_certificate_and_the_transition_are_one_write(tmp_state_dir, monkeypatch):
    """A save failure injected into the transition's write leaves neither on
    disk; the next process completes the round from #162's journal and issues
    the certificate with no reviewer, no second comment and no extra round."""
    gh = FakeGitHub()
    eng, head = reviewing(tmp_state_dir, gh, None, PASSES)
    eng.provider._handler = scripted(clean_reviewer(eng))
    real = engine_module.save_state

    def failing(state, *args, **kwargs):
        if state.phase == Phase.READY_FOR_MERGE:
            raise OSError("disk full")
        return real(state, *args, **kwargs)

    monkeypatch.setattr(engine_module, "save_state", failing)
    with pytest.raises(OSError, match="disk full"):
        eng.step()
    monkeypatch.setattr(engine_module, "save_state", real)
    s = saved(eng)
    assert s.phase == Phase.REVIEW and s.review_certificate == {}
    assert s.review_round == 0 and s.completion_context
    records = s.validation_records

    eng2 = resumed(eng, tmp_state_dir, gh, ["the reviewer must not run again"])
    assert eng2.step().next_phase == "READY_FOR_MERGE"
    s = saved(eng2)
    assert eng2.provider.calls == []
    assert len(comments_of(gh)) == 1
    assert s.review_round == 1 and len(s.review_history) == 1
    assert s.validation_records == records, "no validator ran"
    assert s.review_certificate["comment"]["url"] == controller_review_comment(eng2, 1).url
    assert eng2.review_certificate_problem() == ""


def test_a_round_completed_from_the_journal_issues_the_certificate_from_the_observed_comment(
    tmp_state_dir,
):
    gh = FakeGitHub()
    eng, head = crashed_after_publication(tmp_state_dir, gh, PASSES)
    (posted,) = comments_of(gh)
    records = saved(eng).validation_records
    eng2 = resumed(eng, tmp_state_dir, gh, ["the reviewer must not run again"])
    out = eng2.step()
    assert out.next_phase == "READY_FOR_MERGE"
    assert "no reviewer launched" in out.message
    s = saved(eng2)
    assert eng2.provider.calls == [] and comments_of(gh) == [posted]
    assert s.validation_records == records
    cert = s.review_certificate
    assert cert["comment"]["url"] == posted.url and cert["comment"]["id"] == posted.id
    assert cert["review_round"] == 1 and s.review_round == 1
    assert eng2.review_certificate_problem() == ""


# -- drift at issuance (D12.6) ---------------------------------------------------------


def _timeout(cfg):
    cfg.execution.command_timeout_seconds += 1


def _sandbox_mode(cfg):
    cfg.sandbox.mode = "preferred"


def _allow_list(cfg):
    cfg.execution.env_allowlist_extra.append("AUTOFORGE_CERTIFICATE_TEST")


def _definition_set(cfg):
    cfg.validation.commands.append(list(ALSO_PASSES))


@pytest.mark.parametrize("commands", [[PASSES], []], ids=["set", "empty set"])
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
def test_a_journal_completion_under_another_configuration_certifies_the_binding_and_blocks(
    tmp_state_dir, commands, change, named
):
    """The comment is out, so the round completes with no reviewer, no second
    comment and no validator; the certificate carries the binding's digests,
    never the ones in force, and the same write enters BLOCKED naming what
    changed. Restoring the configuration lets the unblock decide
    READY_FOR_MERGE; the new configuration decides REVIEW."""
    gh = FakeGitHub()
    eng, head = crashed_after_publication(tmp_state_dir, gh, *commands)
    before = saved(eng)
    bound = before.review_evidence
    restored = config(*commands)
    changed = config(*commands)
    change(changed)

    eng2 = resumed(eng, tmp_state_dir, gh, ["the reviewer must not run again"], cfg=changed)
    out = eng2.step()
    assert out.next_phase == "BLOCKED"
    s = saved(eng2)
    assert eng2.provider.calls == [] and len(comments_of(gh)) == 1
    assert s.validation_records == before.validation_records, "no validator ran"
    assert s.review_round == 1 and s.last_review_result == "clean"
    assert named in s.block_reason and "READY_FOR_MERGE was not entered" in s.block_reason
    assert "Restore the configuration and 'unblock'" in s.block_reason
    cert = s.review_certificate
    assert cert["policy_digest"] == bound["policy_digest"]
    assert cert["validation_set_digest"] == bound["validation_set_digest"]
    assert cert["records"] == bound["records"]
    assert eng2.review_certificate_problem().startswith(named)

    under_new = eng2.unblock(REASON, dry_run=True)
    assert under_new.message.startswith("[dry-run] would re-enter REVIEW")
    assert "no applicable review certificate" in under_new.message and named in under_new.message

    eng3 = resumed(eng2, tmp_state_dir, gh, cfg=restored)
    result = eng3.unblock(REASON)
    assert result.unblocked and result.phase == "READY_FOR_MERGE"
    assert "review certificate of round 1 applies" in result.message
    assert saved(eng3).review_certificate == cert


# -- inconsistent inputs (D12.2) -------------------------------------------------------


def _no_binding(eng, state):
    state["review_evidence"] = {}
    return "no review_evidence binding is persisted for review round 1"


def _another_round(eng, state):
    state["review_evidence"]["review_round"] = 7
    return "binding is of review round 7, not of the completed round 1"


def _a_failure(eng, state):
    """A FAIL for the same candidate and definition, named by the binding (it loads:
    a binding may end at its set's first non-PASS)."""
    passed = state["validation_records"][0]
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
    state["validation_records"].append(failed)
    state["review_evidence"]["records"] = [{"seq": failed["seq"], "digest": failed["digest"]}]
    return f"names validation record #{failed['seq']} (definition [0]), which is FAIL"


@pytest.mark.parametrize("edit", [_no_binding, _another_round, _a_failure])
def test_an_inconsistent_input_blocks_with_no_certificate(tmp_state_dir, edit):
    """Never READY_FOR_MERGE: the round is recorded (its comment is out), no
    certificate is written, and the unblock decides a fresh REVIEW."""
    gh = FakeGitHub()
    eng, head = crashed_after_publication(tmp_state_dir, gh, PASSES)
    eng.close()
    data = json.loads(eng.paths.state_file.read_text())
    named = edit(eng, data)
    eng.paths.state_file.write_text(json.dumps(data))
    eng2 = resumed(eng, tmp_state_dir, gh, ["the reviewer must not run again"])
    assert eng2.step().next_phase == "BLOCKED"
    s = saved(eng2)
    assert s.review_certificate == {}
    assert named in s.block_reason
    assert "no review certificate was issued" in s.block_reason
    assert "READY_FOR_MERGE was not entered" in s.block_reason
    assert eng2.provider.calls == [] and len(comments_of(gh)) == 1
    assert s.review_round == 1 and s.last_review_result == "clean"
    decision = eng2.unblock(REASON, dry_run=True)
    assert decision.message.startswith("[dry-run] would re-enter REVIEW")
    assert "no review certificate is held" in decision.message


def test_an_edited_certificate_is_refused_at_load_never_reissued(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = certified(tmp_state_dir, gh, PASSES)
    eng.close()
    data = json.loads(eng.paths.state_file.read_text())
    data["review_certificate"]["reviewed_base_ref"] = "develop"
    eng.paths.state_file.write_text(json.dumps(data))
    with pytest.raises(StateError, match="review_certificate"):
        load_state(eng.paths.state_file)


def test_a_restart_in_ready_for_merge_holds_an_applicable_certificate(tmp_state_dir):
    gh = FakeGitHub()
    eng, _ = certified(tmp_state_dir, gh, PASSES)
    cert = saved(eng).review_certificate
    eng2 = resumed(eng, tmp_state_dir, gh)
    s = eng2.state
    assert s.phase == Phase.READY_FOR_MERGE and s.review_certificate == cert
    assert eng2.review_certificate_problem() == ""
    assert cli._review_certificate_check(eng2, s) == (True, "")


# -- the unblock (D12.3, D12.4) --------------------------------------------------------


def blocked_ready(tmp_state_dir, gh, *commands, max_rounds=None):
    eng, head = certified(tmp_state_dir, gh, *commands)
    eng._block(Phase.READY_FOR_MERGE, None, "a required check failed; it was rerun")
    if max_rounds is not None:
        eng.config.workflow.max_review_rounds = max_rounds
    return eng, head


def test_unblock_of_an_open_pr_with_an_applicable_certificate_decides_ready_for_merge(
    tmp_state_dir,
):
    gh = FakeGitHub()
    eng, head = blocked_ready(tmp_state_dir, gh, PASSES)
    result = eng.unblock(REASON)
    assert result.unblocked and result.phase == "READY_FOR_MERGE"
    assert "clean-reviewed HEAD" in result.message
    assert "review certificate of round 1 applies" in result.message


@pytest.mark.parametrize("legacy", [True, False], ids=["no certificate", "inapplicable"])
def test_unblock_of_an_open_pr_without_an_applicable_certificate_decides_review(
    tmp_state_dir, legacy
):
    gh = FakeGitHub()
    eng, head = blocked_ready(tmp_state_dir, gh, PASSES)
    if legacy:
        # As a state from before Wave 2B migrates: no certificate (D15.4).
        eng.state.review_certificate = {}
        eng._save()
        reason = "no review certificate is held"
    else:
        eng.config.execution.command_timeout_seconds += 1
        reason = "the validation policy changed"
    result = eng.unblock(REASON)
    assert result.unblocked and result.phase == "REVIEW"
    assert "has no applicable review certificate" in result.message and reason in result.message
    s = saved(eng)
    assert s.last_review_result == "clean", "the revision did not move: nothing is stale"
    # The fresh round validates at its entry and certifies its own clean result.
    eng.provider._handler = scripted(clean_reviewer(eng, 2))
    assert eng.step().next_phase == "READY_FOR_MERGE"
    s = saved(eng)
    assert s.review_certificate["review_round"] == 2 == s.review_round
    assert eng.review_certificate_problem() == ""


def test_unblock_of_an_uncertified_open_pr_at_the_round_cap_is_refused(tmp_state_dir):
    gh = FakeGitHub()
    eng, head = blocked_ready(tmp_state_dir, gh, PASSES, max_rounds=1)
    eng.state.review_certificate = {}
    eng._save()
    raw = eng.paths.state_file.read_text()
    result = eng.unblock(REASON)
    assert not result.unblocked
    assert "Raise 'workflow.max_review_rounds' in the config" in result.message
    assert "its clean review of round 1 has no applicable review certificate" in result.message
    assert "no review certificate is held" in result.message
    assert eng.paths.state_file.read_text() == raw


def test_unblock_of_a_merged_uncounted_pr_needs_no_certificate(tmp_state_dir):
    """D12.4: counting a merge already made is not a merge decision."""
    gh = FakeGitHub()
    eng, head = blocked_ready(tmp_state_dir, gh, PASSES)
    eng.state.review_certificate = {}
    eng._save()
    gh.prs[PR].state = "MERGED"
    result = eng.unblock(REASON)
    assert result.unblocked and result.phase == "READY_FOR_MERGE"
    assert "not yet counted" in result.message


# -- status and the trust boundary (D1.3) ----------------------------------------------


def test_status_shows_the_certificate_and_whether_it_applies(tmp_state_dir, monkeypatch, capsys):
    gh = FakeGitHub()
    eng, head = certified(tmp_state_dir, gh, PASSES)
    s = saved(eng)
    cert = s.review_certificate
    check = cli._review_certificate_check(eng, s)
    text = "\n".join(cli._render_validation_evidence(s, eng.config, check))
    assert f"Review certificate: remote, commit {head[:12]}" in text
    assert "review round 1, 1 record(s)" in text
    assert cert["validation_set_digest"][:16] in text and cert["policy_digest"][:16] in text
    assert f"issued {cert['issued_at']}" in text
    assert "Applicable: yes" in text
    assert f"Note: {NOT_A_PERMISSION}" in text

    monkeypatch.setattr(cli, "_engine_for", lambda args, cfg=None: eng)
    assert cli.main(["--state-dir", str(tmp_state_dir), "status", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["review_certificate"] == cert, "the raw field, as persisted"
    assert printed["review_certificate_status"] == {
        "kind": "remote",
        "candidate": cert["candidate"],
        "review_round": 1,
        "validation_set_digest": cert["validation_set_digest"],
        "policy_digest": cert["policy_digest"],
        "issued_at": cert["issued_at"],
        "applicable": True,
        "problem": "",
        "note": NOT_A_PERMISSION,
    }

    eng.config.execution.command_timeout_seconds += 1
    assert cli.main(["--state-dir", str(tmp_state_dir), "status"]) == 0
    assert "Applicable: no: the validation policy changed" in capsys.readouterr().out
    assert cli.main(["--state-dir", str(tmp_state_dir), "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)["review_certificate_status"]
    assert status["applicable"] is False
    assert status["problem"].startswith("the validation policy changed")


def test_status_without_a_certificate_derives_none(tmp_state_dir, monkeypatch, capsys):
    gh = FakeGitHub()
    eng, _ = reviewing(tmp_state_dir, gh, None, PASSES)
    eng._save()
    monkeypatch.setattr(cli, "_engine_for", lambda args, cfg=None: eng)
    assert cli.main(["--state-dir", str(tmp_state_dir), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["review_certificate_status"] is None
    assert cli.main(["--state-dir", str(tmp_state_dir), "status"]) == 0
    assert "Review certificate" not in capsys.readouterr().out


def test_no_prompt_and_no_published_payload_carries_the_certificate(tmp_state_dir):
    gh = FakeGitHub()
    eng, head = certified(tmp_state_dir, gh, PASSES)
    cert = saved(eng).review_certificate
    # The next round's reviewer, with the certificate held.
    eng.state.phase = Phase.REVIEW
    eng._save()
    eng.provider._handler = scripted(clean_reviewer(eng, 2))
    assert eng.step().next_phase == "READY_FOR_MERGE"
    prompt = eng.provider.calls[-1].prompt
    context = "".join(p.read_text() for p in read_context_of(prompt).iterdir())
    published = "".join(body for _, _, body in gh.effect_writes)
    for text in (prompt, context, published):
        assert "review_certificate" not in text
        assert cert["digest"] not in text
        assert cert["issued_at"] not in text


# -- LOCAL (D12.5) ---------------------------------------------------------------------


def local_run(tmp_path, steps, *commands):
    from .conftest import make_local_engine

    root = local_repo(tmp_path / "repo")
    cfg = default_config()
    cfg.local.validation_commands = [list(c) for c in commands]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = local_scripted(eng, root, steps)
    return eng, root


def implemented(text: str = "v1\n"):
    return (lambda r: touch_impl(r, text), lambda e: impl_result())


def reviewed(findings=None, round_: int = 1):
    return (None, lambda e: local_review_result(e.state.workspace_fingerprint, round_, findings))


@pytest.mark.parametrize("commands", [[PASSES], []], ids=["set", "empty set"])
def test_local_done_carries_a_certificate_of_the_reviewed_workspace(tmp_path, commands):
    """Behind the exploding GitHub: issuance makes no GitHub call."""
    eng, root = local_run(tmp_path, [implemented(), reviewed()], *commands)
    assert [o.next_phase for o in eng.run(max_steps=5)] == ["ANALYZE_EXECUTE", "REVIEW", "DONE"]
    s = saved(eng)
    cert, bound = s.review_certificate, s.review_evidence
    assert cert["kind"] == "local"
    assert cert["reviewed_workspace_fingerprint"] == s.reviewed_workspace_fingerprint
    assert cert["candidate"]["fingerprint"] == s.workspace_fingerprint
    assert cert["candidate"] == bound["candidate"]
    assert cert["records"] == bound["records"] and len(cert["records"]) == len(commands)
    assert cert["validation_set_digest"] == bound["validation_set_digest"]
    assert cert["policy_digest"] == bound["policy_digest"]
    assert cert["review_round"] == 1
    assert "comment" not in cert
    assert eng.review_certificate_problem() == ""
    text = "\n".join(
        cli._render_validation_evidence(s, eng.config, cli._review_certificate_check(eng, s))
    )
    assert "Review certificate: local, workspace" in text and "Applicable: yes" in text


def test_local_a_fix_round_certifies_the_new_workspace_only(tmp_path):
    steps = [
        implemented(),
        reviewed([local_finding()]),
        (lambda r: touch_impl(r, "v2\n"), lambda e: local_fix_result(["R1-F1"])),
        reviewed(round_=2),
    ]
    eng, root = local_run(tmp_path, steps, PASSES)
    eng.step()
    assert eng.step().next_phase == "REVIEW"
    assert eng.step().next_phase == "FIX"
    s = saved(eng)
    first, old_fingerprint = s.review_evidence, s.workspace_fingerprint
    assert s.review_certificate == {}, "a round with findings is not certified"
    assert eng.step().next_phase == "REVIEW"
    assert eng.step().next_phase == "DONE"
    s = saved(eng)
    cert = s.review_certificate
    assert cert["review_round"] == 2
    assert cert["reviewed_workspace_fingerprint"] == s.workspace_fingerprint != old_fingerprint
    assert cert["records"] == s.review_evidence["records"] != first["records"]
    # The round-1 binding names F0: it issues nothing for the review of F1.
    with pytest.raises(CertificateIssuanceError, match="not the reviewed workspace"):
        local_certificate(
            dict(first, review_round=2),
            s.validation_evidence(),
            run_id=s.run_id,
            review_round=2,
            needs_fix_round=False,
            actionable_findings=0,
            reviewed_workspace_fingerprint=s.workspace_fingerprint,
            issued_at=cert["issued_at"],
        )


def test_local_done_is_never_entered_without_an_applicable_certificate(tmp_path, monkeypatch):
    """A LOCAL difference at issuance is an invariant violation (the contract
    holds the configuration for the run), and blocks all the same."""
    eng, root = local_run(tmp_path, [implemented(), reviewed()], PASSES)
    eng.step()
    assert eng.step().next_phase == "REVIEW"
    real = eng._configured_validation

    def drifted() -> Configured:
        configured = real()
        return Configured(configured.definitions[:0], configured.policy)

    monkeypatch.setattr(eng, "_configured_validation", drifted)
    assert eng.step().next_phase == "BLOCKED"
    s = saved(eng)
    assert s.phase == Phase.BLOCKED
    assert "the validation set changed" in s.block_reason
    assert "DONE was not entered" in s.block_reason and "start a new local run" in s.block_reason


def test_local_a_crash_before_the_done_write_leaves_no_certificate(tmp_path, monkeypatch):
    """LOCAL has no journal: the existing REVIEW recovery relaunches the
    reviewer, and the DONE write issues the certificate."""
    steps = [implemented(), reviewed(), reviewed()]
    eng, root = local_run(tmp_path, steps, PASSES)
    eng.step()
    assert eng.step().next_phase == "REVIEW"
    real = engine_module.save_state

    def failing(state, *args, **kwargs):
        if state.phase == Phase.DONE:
            raise OSError("disk full")
        return real(state, *args, **kwargs)

    monkeypatch.setattr(engine_module, "save_state", failing)
    with pytest.raises(OSError, match="disk full"):
        eng.step()
    monkeypatch.setattr(engine_module, "save_state", real)
    s = saved(eng)
    assert s.phase == Phase.REVIEW and s.review_certificate == {}
    eng.load()
    assert eng.step().next_phase == "DONE"
    s = saved(eng)
    assert s.review_certificate["review_round"] == 1
    assert eng.review_certificate_problem() == ""


# -- provider parity (D17.2) -----------------------------------------------------------


def _normalized(cert: dict, s) -> str:
    """The certificate with what differs between two runs of the same script
    replaced: the time, the commits' SHAs and the record digests over them."""
    text = json.dumps({k: v for k, v in cert.items() if k != "digest"}, sort_keys=True)
    for value, token in (
        (cert["issued_at"], "<ISSUED>"),
        (s.reviewed_head_sha, "<HEAD>"),
        (cert["candidate"]["tree"], "<TREE>"),
        (s.reviewed_merge_base_sha, "<MERGE_BASE>"),
        *((pair["digest"], f"<RECORD {pair['seq']}>") for pair in cert["records"]),
    ):
        text = text.replace(value, token)
    return text


def test_the_same_certificate_behind_every_provider(tmp_path_factory):
    """The same scripted implementer and reviewer behind Claude, OpenCode and
    Pi: certificates equal but for the commits' own SHAs and the time."""
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
        phases = [eng.step().next_phase for _ in range(3)]
        assert phases == ["ANALYZE_EXECUTE", "REVIEW", "READY_FOR_MERGE"]
        s = saved(eng)
        assert eng.review_certificate_problem() == ""
        seen.append(_normalized(s.review_certificate, s))
        if provider == "pi":
            assert eng.provider.calls == [], "the Pi fake answered, not the scripted provider"
    assert seen[0] == seen[1] == seen[2]
    assert re.search(r'"commit": "<HEAD>"', seen[0]) and '"tree": "<TREE>"' in seen[0]
