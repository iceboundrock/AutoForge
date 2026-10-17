"""The controller's review certificate: issuance and applicability (ADR 0006 D12, #174).

A ``ReviewCertificate`` means exactly (D1.2): for candidate C, in review
round N, bound to PR / HEAD / base / merge base (REMOTE) or to workspace
fingerprint F (LOCAL), the controller accepted a clean review result; it
presented exactly validation set V to that review, and every record in V is
``PASS`` for C under the definitions and policy recorded; for REMOTE, it
verified the published review comment identity K. It is not a GitHub
approval, a CI result, permission to merge, proof that the code is correct
or an authorization token, and it stands in for no merge-gate fact
(:data:`NOT_A_PERMISSION`).

Two things live here:

- **Issuance** (D12.2). :func:`remote_certificate` and
  :func:`local_certificate` build the certificate from persisted, verified
  inputs only: the ``review_evidence`` binding of the completed round (its
  candidate, record seqs and digests, set and policy digests are copied,
  never computed from the configuration in force), the review binding, the
  clean verdict and, for REMOTE, the comment identity #162 observed. An input
  that is missing or inconsistent raises :class:`CertificateIssuanceError`
  naming it: no certificate is built.
- **Applicability** (D12.3). :func:`certificate_problem` says why a
  certificate does not apply to the state, the configuration in force and,
  when given, the live PR revision, or ``""`` when it does.
  :func:`configuration_drift` is its set-and-policy part alone, which the
  issuing write routes on (D12.6).

The schema, its bounds and its load-time integrity check are #172's
(:func:`autoforge.evidence.check_certificate`). A certificate is never shown
to an agent, rendered into a prompt or published, and never accepted from
one (D1.3). This module does no I/O and reads no clock: the engine passes
``issued_at``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .errors import AutoForgeError, ConfigurationError, StateError
from .evidence import (
    KIND_LOCAL,
    KIND_REMOTE,
    SCHEMA,
    Candidate,
    Definition,
    LocalCandidate,
    Outcome,
    Policy,
    RecordView,
    RemoteCandidate,
    bound_records,
    certificate_digest,
    check_certificate,
    eligible,
    parse_candidate,
    set_digest,
    validation_set_digest,
)
from .state import AutoForgeState
from .transitions import WorkflowMode
from .validation import parse_comment_url, parse_pr_url

# The fixed wording every display of a certificate carries (D1.2).
NOT_A_PERMISSION = (
    "a review certificate records what the controller observed of one clean review; it is "
    "not a GitHub approval, a CI result or permission to merge, and the merge gate re-reads "
    "every fact it decides on from GitHub"
)

CLEAN = {"needs_fix_round": False, "actionable_findings": 0}


class CertificateIssuanceError(AutoForgeError):
    """A certificate cannot be issued from the persisted inputs (ADR 0006 D12.2).

    An invariant violation: the REVIEW entry never launches a reviewer under
    another round's or candidate's binding (D5.5), a clean result over a
    non-``PASS`` record is refused (D9.4) and load checks every record, so
    the issuing write enters ``BLOCKED`` naming the input instead of the
    transition, and writes no certificate.
    """


@dataclass(frozen=True)
class Configured:
    """The validation set and policy P the configuration in force yields (D3.5, D8.1)."""

    definitions: tuple[Definition, ...]
    policy: Policy

    @property
    def validation_set_digest(self) -> str:
        return validation_set_digest(self.definitions)

    @property
    def policy_digest(self) -> str:
        return self.policy.digest


@dataclass(frozen=True)
class Revision:
    """A PR revision: the REMOTE review binding, or the one GitHub returned live."""

    pr_url: str
    head_sha: str
    base_ref: str
    merge_base_sha: str


# -- issuance (D12.2) ------------------------------------------------------------------


def _issuance_inputs(
    binding: Mapping[str, object],
    records: Sequence[RecordView],
    *,
    review_round: int,
    needs_fix_round: bool,
    actionable_findings: int,
) -> tuple[Candidate, list[RecordView]]:
    """The binding's candidate and named records, once every issuance requirement holds."""
    if needs_fix_round is not False or actionable_findings != 0:
        raise CertificateIssuanceError(
            f"review round {review_round} is not clean (needs_fix_round {needs_fix_round}, "
            f"{actionable_findings} actionable finding(s)); only a clean result is certified"
        )
    if not binding:
        raise CertificateIssuanceError(
            f"no review_evidence binding is persisted for review round {review_round}: the "
            "controller cannot say which validation the review was presented"
        )
    bound_round = binding.get("review_round")
    if bound_round != review_round:
        raise CertificateIssuanceError(
            f"the review_evidence binding is of review round {bound_round}, not of the "
            f"completed round {review_round}"
        )
    try:
        candidate = parse_candidate(binding.get("candidate"), "the review_evidence candidate")
        named = bound_records(binding, records)
    except StateError as exc:
        raise CertificateIssuanceError(f"the review_evidence binding is unusable: {exc}") from None
    for view in named:
        if view.outcome is not Outcome.PASS:
            raise CertificateIssuanceError(
                f"the review_evidence binding names validation record #{view.seq} (definition "
                f"[{view.index}]), which is {view.outcome.value}, not PASS"
            )
        if view.candidate.identity != candidate.identity:
            raise CertificateIssuanceError(
                f"the review_evidence binding names validation record #{view.seq}, which is "
                "for another candidate"
            )
        if view.policy_digest != binding.get("policy_digest"):
            raise CertificateIssuanceError(
                f"the review_evidence binding names validation record #{view.seq}, which ran "
                "under another policy"
            )
    if set_digest([v.definition_digest for v in named]) != binding.get("validation_set_digest"):
        raise CertificateIssuanceError(
            f"the {len(named)} validation record(s) the review_evidence binding names do not "
            "encode to its validation_set_digest: the review was not presented a whole set"
        )
    return candidate, named


def _common(
    kind: str,
    candidate: Candidate,
    review_round: int,
    binding: Mapping[str, object],
    issued_at: str,
) -> dict:
    pairs = binding["records"]
    assert isinstance(pairs, list)  # bound_records read it
    return {
        "schema": SCHEMA,
        "kind": kind,
        "candidate": candidate.to_dict(),
        "review_round": review_round,
        "records": [{"seq": pair["seq"], "digest": pair["digest"]} for pair in pairs],
        "validation_set_digest": binding["validation_set_digest"],
        "policy_digest": binding["policy_digest"],
        "clean": dict(CLEAN),
        "issued_at": issued_at,
    }


def _sealed(
    certificate: dict,
    records: Sequence[RecordView],
    *,
    local: bool,
    repository: str,
    run_id: str,
) -> dict:
    """Digest ``certificate`` and check it as a load will: a writer never builds one it refuses."""
    certificate["digest"] = certificate_digest(certificate)
    try:
        check_certificate(certificate, records, local=local, repository=repository, run_id=run_id)
    except StateError as exc:
        raise CertificateIssuanceError(f"the certificate would not load: {exc}") from None
    return certificate


def remote_certificate(
    binding: Mapping[str, object],
    records: Sequence[RecordView],
    *,
    repository: str,
    run_id: str,
    review_round: int,
    needs_fix_round: bool,
    actionable_findings: int,
    reviewed: Revision,
    comment_url: str,
    marker_key: Mapping[str, object],
    issued_at: str,
) -> dict:
    """The REMOTE certificate of a clean round (D12.1, D12.2).

    ``reviewed`` is the review binding the round's comment marker names
    (PR, HEAD, base, merge base), ``comment_url`` the comment #162 read back
    (or its journal observed) and ``marker_key`` that marker's key. The
    binding's candidate must be the reviewed HEAD's commit: its tree was
    derived from that commit at the REVIEW entry (#173).
    """
    candidate, _ = _issuance_inputs(
        binding,
        records,
        review_round=review_round,
        needs_fix_round=needs_fix_round,
        actionable_findings=actionable_findings,
    )
    if not isinstance(candidate, RemoteCandidate):
        raise CertificateIssuanceError("the review_evidence binding names a LOCAL candidate")
    if candidate.commit != reviewed.head_sha.lower():
        raise CertificateIssuanceError(
            f"the review_evidence binding names commit {candidate.commit[:12]}, not the "
            f"reviewed HEAD {reviewed.head_sha[:12]}"
        )
    try:
        comment = parse_comment_url(comment_url)
        pr_url = parse_pr_url(reviewed.pr_url).canonical
    except ConfigurationError as exc:
        raise CertificateIssuanceError(f"the review comment identity is unusable: {exc}") from None
    certificate = _common(KIND_REMOTE, candidate, review_round, binding, issued_at)
    certificate.update(
        {
            "reviewed_pr_url": pr_url,
            "reviewed_head_sha": reviewed.head_sha.lower(),
            "reviewed_base_ref": reviewed.base_ref,
            "reviewed_merge_base_sha": reviewed.merge_base_sha,
            "comment": {
                "url": comment.canonical,
                "id": comment.comment_id,
                "marker_key": {
                    "review_round": marker_key.get("review_round"),
                    "reviewed_head_sha": marker_key.get("reviewed_head_sha"),
                    "reviewed_base_ref": marker_key.get("reviewed_base_ref"),
                    "reviewed_merge_base_sha": marker_key.get("reviewed_merge_base_sha"),
                },
            },
        }
    )
    return _sealed(certificate, records, local=False, repository=repository, run_id=run_id)


def local_certificate(
    binding: Mapping[str, object],
    records: Sequence[RecordView],
    *,
    run_id: str,
    review_round: int,
    needs_fix_round: bool,
    actionable_findings: int,
    reviewed_workspace_fingerprint: str,
    issued_at: str,
) -> dict:
    """The LOCAL certificate of a clean round (D12.1, D12.2, D12.5): GitHub is never consulted.

    ``reviewed_workspace_fingerprint`` is the fingerprint the round was
    bound to and re-checked against after the reviewer exited; the
    binding's candidate must be that workspace.
    """
    candidate, _ = _issuance_inputs(
        binding,
        records,
        review_round=review_round,
        needs_fix_round=needs_fix_round,
        actionable_findings=actionable_findings,
    )
    if not isinstance(candidate, LocalCandidate):
        raise CertificateIssuanceError("the review_evidence binding names a REMOTE candidate")
    if candidate.fingerprint != reviewed_workspace_fingerprint:
        raise CertificateIssuanceError(
            f"the review_evidence binding names workspace {candidate.fingerprint[:16]}..., not "
            f"the reviewed workspace {reviewed_workspace_fingerprint[:16]}..."
        )
    certificate = _common(KIND_LOCAL, candidate, review_round, binding, issued_at)
    certificate["reviewed_workspace_fingerprint"] = reviewed_workspace_fingerprint
    return _sealed(certificate, records, local=True, repository="", run_id=run_id)


# -- applicability (D12.3, D12.6, D13.2) -------------------------------------------------


def configuration_drift(certificate: Mapping[str, object], configured: Configured) -> str:
    """Which of the certificate's set and policy digests the configuration no longer yields, or "".

    D12.6: the issuing write routes on this, and so does
    :func:`certificate_problem`, so that a drift is named as the drift.
    """
    drifted = []
    if certificate.get("validation_set_digest") != configured.validation_set_digest:
        drifted.append(
            f"the validation set changed (the review was presented set "
            f"{str(certificate.get('validation_set_digest'))[:16]}..., the configuration now "
            f"yields {configured.validation_set_digest[:16]}...)"
        )
    if certificate.get("policy_digest") != configured.policy_digest:
        drifted.append(
            f"the validation policy changed (the review was presented policy "
            f"{str(certificate.get('policy_digest'))[:16]}..., the configuration now yields "
            f"{configured.policy_digest[:16]}...)"
        )
    return "; ".join(drifted)


def _binding_problem(certificate: Mapping[str, object], state: AutoForgeState) -> str:
    """REMOTE: the certificate's review binding and comment against the state's."""
    for name in (
        "reviewed_pr_url",
        "reviewed_head_sha",
        "reviewed_base_ref",
        "reviewed_merge_base_sha",
    ):
        held = getattr(state, name)
        if name == "reviewed_head_sha":
            held = held.lower()
        if certificate.get(name) != held:
            return (
                f"its {name} {str(certificate.get(name))[:80]!r} is not the state's {held[:80]!r}"
            )
    candidate = certificate.get("candidate")
    commit = candidate.get("commit") if isinstance(candidate, dict) else None
    if commit != state.reviewed_head_sha.lower():
        return f"its candidate commit {str(commit)[:12]} is not the reviewed HEAD"
    comment = certificate.get("comment")
    url = comment.get("url") if isinstance(comment, dict) else None
    if url != state.last_review_comment_url:
        return (
            f"its review comment {str(url)[:200]} is not the last review comment "
            f"{state.last_review_comment_url[:200] or '(none)'}"
        )
    return ""


def _local_binding_problem(certificate: Mapping[str, object], state: AutoForgeState) -> str:
    """LOCAL: the certificate's workspace against the one the state's last review bound."""
    bound = state.reviewed_workspace_fingerprint
    held = certificate.get("reviewed_workspace_fingerprint")
    if held != bound:
        return (
            f"its reviewed workspace {str(held)[:16]}... is not the state's reviewed workspace "
            f"{bound[:16] or '(none)'}..."
        )
    try:
        policy_digest = state.local_contract().workspace_policy.digest()
    except (StateError, ConfigurationError) as exc:
        return f"the run contract is unreadable: {exc}"
    expected = LocalCandidate(bound, policy_digest, state.run_id).identity
    candidate = parse_candidate(certificate.get("candidate"), "the certificate candidate")
    if candidate.identity != expected:
        return "its candidate is not the reviewed workspace under this run's contract"
    return ""


def certificate_problem(
    certificate: Mapping[str, object],
    state: AutoForgeState,
    configured: Configured,
    live: Revision | None = None,
) -> str:
    """Why ``certificate`` does not apply, or ``""`` when it does (D12.3; #175 adds ``live``).

    Checked, in this order, each failing with one bounded reason: it is
    held, its kind is the run's mode, its digest verifies and it loads as
    a certificate against the state's records (D11.1); its round is the
    state's completed round; its review binding equals the state's (REMOTE:
    PR, HEAD, base ref and merge base, its candidate's commit is the
    reviewed HEAD and its comment is ``last_review_comment_url``; LOCAL: the
    reviewed workspace fingerprint, under this run's contract); the
    configuration in force yields its set and policy digests
    (:func:`configuration_drift`); and every record it names is eligible
    evidence for its candidate under that set and policy
    (:func:`autoforge.evidence.eligible`). With ``live`` (REMOTE only: the
    PR URL, HEAD, base ref and merge base as GitHub returned them) those
    must equal the certificate's too. Pure: no I/O.
    """
    if not certificate:
        return "no review certificate is held"
    local = state.mode == WorkflowMode.LOCAL
    if live is not None and local:
        raise ValueError("a LOCAL certificate has no live GitHub revision")
    kind = certificate.get("kind")
    mode_kind = KIND_LOCAL if local else KIND_REMOTE
    if kind != mode_kind:
        return f"it is a {str(kind)[:16]!r} certificate in a {mode_kind} run"
    if certificate_digest(certificate) != certificate.get("digest"):
        return "its digest does not verify: it was edited or corrupted"
    try:
        records = state.validation_evidence()
        check_certificate(
            certificate,
            records,
            local=local,
            repository=state.repository,
            run_id=state.run_id,
        )
    except StateError as exc:
        return f"it does not load: {str(exc)[:300]}"
    if certificate["review_round"] != state.review_round:
        return (
            f"it is of review round {certificate['review_round']}, not of the completed round "
            f"{state.review_round}"
        )
    problem = (
        _local_binding_problem(certificate, state)
        if local
        else _binding_problem(certificate, state)
    )
    if problem:
        return problem
    drift = configuration_drift(certificate, configured)
    if drift:
        return drift
    candidate = parse_candidate(certificate["candidate"], "the certificate candidate")
    named = bound_records(certificate, records)
    for definition, view in zip(configured.definitions, named, strict=True):
        if not eligible(
            view, candidate, definition, configured.policy, state.validation_override_seq
        ):
            return (
                f"validation record #{view.seq} it names is not evidence for definition "
                f"[{definition.index}] of its candidate under the policy in force"
            )
    if live is not None:
        return _live_problem(certificate, live)
    return ""


def _live_problem(certificate: Mapping[str, object], live: Revision) -> str:
    try:
        same_pr = parse_pr_url(live.pr_url).same_target(
            parse_pr_url(str(certificate["reviewed_pr_url"]))
        )
    except ConfigurationError:
        same_pr = False
    if not same_pr:
        return (
            f"it is of PR {certificate['reviewed_pr_url']}, not of the live PR {live.pr_url[:200]}"
        )
    for name, value in (
        ("reviewed_head_sha", live.head_sha.lower()),
        ("reviewed_base_ref", live.base_ref),
        ("reviewed_merge_base_sha", live.merge_base_sha),
    ):
        if certificate[name] != value:
            return f"its {name} {str(certificate[name])[:80]!r} is not the live {value[:80]!r}"
    return ""


def describe(certificate: Mapping[str, object]) -> str:
    """One display line: kind, candidate, round, set and policy digests, ``issued_at``."""
    candidate = parse_candidate(certificate["candidate"], "the certificate candidate")
    pairs = certificate["records"]
    count = len(pairs) if isinstance(pairs, list) else 0
    return (
        f"{certificate['kind']}, {candidate.describe()}, review round "
        f"{certificate['review_round']}, {count} record(s) "
        f"of set {str(certificate['validation_set_digest'])[:16]}..., policy "
        f"{str(certificate['policy_digest'])[:16]}..., issued {certificate['issued_at']}"
    )
