"""Durable GitHub claims (`autoforge.claims`): the one identity contract.

Pure tests of the marker protocol every REMOTE entry and every post-agent
read-back consumes: exact-schema decoding per kind, the scan that
classifies every marker and skips none, the collection whose cardinality
questions are inconclusive while any object is defective, and the
renderers that round-trip through the decoders.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import pytest

from autoforge.claims import (
    FOLLOW_UP,
    IMPLEMENTATION,
    PROGRESS,
    REVIEW,
    Claimants,
    FollowUpClaim,
    ImplementationClaim,
    ProgressClaim,
    ReviewClaim,
    collect,
    render_follow_up_marker,
    render_implementation_marker,
    render_progress_marker,
    scan,
)
from autoforge.errors import ClaimConflictError, ConfigurationError
from autoforge.result_parser import MAX_FINDING_ID_CHARS, MAX_URL_CHARS
from autoforge.validation import parse_issue_url, parse_pr_url

ISSUE = "https://github.com/owner/repo/issues/2"
ISSUE3 = "https://github.com/owner/repo/issues/3"
PR = "https://github.com/owner/repo/pull/42"
PR41 = "https://github.com/owner/repo/pull/41"
SHA_A = "a" * 40
SHA_B = "b" * 40


@dataclass(frozen=True)
class Obj:
    """The least a marked GitHub object is: a URL and a body."""

    url: str
    body: str


def marker(name: str, payload: object) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return f"<!-- {name}: {text} -->"


VALID = {
    IMPLEMENTATION.name: {"issue": ISSUE},
    REVIEW.name: {"round": 1, "reviewed_head_sha": SHA_A, "needs_fix_round": False},
    PROGRESS.name: {"issue": ISSUE, "pr": PR},
    FOLLOW_UP.name: {"finding_id": "R1-F1", "pr": PR},
}
KINDS = [IMPLEMENTATION, REVIEW, PROGRESS, FOLLOW_UP]


def _valid(kind):
    return dict(VALID[kind.name])


# -- valid markers ------------------------------------------------------------------
def test_each_kind_decodes_its_documented_payload_to_a_typed_claim():
    assert scan(IMPLEMENTATION, marker("ai-implementation", _valid(IMPLEMENTATION))).claims == (
        ImplementationClaim(issue=parse_issue_url(ISSUE)),
    )
    assert scan(REVIEW, marker("ai-review-result", _valid(REVIEW))).claims == (
        ReviewClaim(round=1, reviewed_head_sha=SHA_A, needs_fix_round=False, finding_ids=None),
    )
    assert scan(PROGRESS, marker("ai-epic-progress", _valid(PROGRESS))).claims == (
        ProgressClaim(issue=parse_issue_url(ISSUE), pr=parse_pr_url(PR)),
    )
    assert scan(FOLLOW_UP, marker("ai-follow-up", _valid(FOLLOW_UP))).claims == (
        FollowUpClaim(pr=parse_pr_url(PR), finding_id="R1-F1"),
    )
    for kind in KINDS:
        assert scan(kind, marker(kind.name, _valid(kind))).defects == ()


def test_review_marker_lowercases_the_sha_and_keeps_finding_ids():
    payload = {
        "round": 2,
        "reviewed_head_sha": SHA_A.upper(),
        "needs_fix_round": True,
        "finding_ids": ["R2-F1", "R2-F2"],
    }
    (claim,) = scan(REVIEW, marker("ai-review-result", payload)).claims
    assert claim.reviewed_head_sha == SHA_A and claim.key == (2, SHA_A, None)
    assert claim.finding_ids == ("R2-F1", "R2-F2") and claim.needs_fix_round is True


def test_review_marker_binds_the_base_branch_into_the_key():
    """The round's identity is the diff it decided on: HEAD *and* base. A
    marker naming another base, or none (written before the key had one),
    is a different key and never the round's comment for a bound base."""
    payload = {**_valid(REVIEW), "reviewed_base_ref": "main"}
    (claim,) = scan(REVIEW, marker("ai-review-result", payload)).claims
    assert claim.reviewed_base_ref == "main" and claim.key == (1, SHA_A, "main")
    (other,) = scan(
        REVIEW, marker("ai-review-result", {**payload, "reviewed_base_ref": "release/1.x"})
    ).claims
    (legacy,) = scan(REVIEW, marker("ai-review-result", _valid(REVIEW))).claims
    assert legacy.reviewed_base_ref is None
    assert len({claim.key, other.key, legacy.key}) == 3
    objs = [Obj("c1", marker("ai-review-result", payload))]
    objs.append(Obj("c2", marker("ai-review-result", {**payload, "reviewed_base_ref": "dev"})))
    objs.append(Obj("c3", marker("ai-review-result", _valid(REVIEW))))
    collection = collect(REVIEW, objs, "comment")
    assert collection.defects == ()
    assert collection.claimants((1, SHA_A, "main"), "r1").exactly_one().obj.url == "c1"
    assert collection.claimants((1, SHA_A, "release/1.x"), "r1").at_most_one() is None


@pytest.mark.parametrize(
    "text",
    [
        "<!--ai-implementation:{}-->",
        "<!--   ai-implementation  :   {}   -->",
        "<!-- ai-implementation:\n{}\n-->",
        "prose before <!-- ai-implementation: {} --> prose after",
    ],
    ids=["tight", "padded", "newlines", "embedded"],
)
def test_whitespace_around_the_name_and_payload_is_not_part_of_the_marker(text):
    body = text.format(json.dumps(_valid(IMPLEMENTATION)))
    result = scan(IMPLEMENTATION, body)
    assert result.defects == () and result.claims[0].key == parse_issue_url(ISSUE).identity


def test_a_marker_of_another_kind_is_neither_a_claim_nor_a_defect():
    body = marker("ai-implementation", _valid(IMPLEMENTATION))
    for kind in (REVIEW, PROGRESS, FOLLOW_UP):
        assert scan(kind, body) == scan(kind, "")
    assert scan(FOLLOW_UP, "<!-- ai-follow-up-note: not a marker -->").claims == ()
    assert scan(FOLLOW_UP, "<!-- ai-follow-up-note: not a marker -->").defects == ()
    assert scan(IMPLEMENTATION, None) == scan(IMPLEMENTATION, "")  # type: ignore[arg-type]


# -- schema defects, one equivalence class per case -----------------------------------
def _cases():
    """(kind, payload, reason fragment) for every malformed shape of every kind."""
    yield IMPLEMENTATION, {}, "missing key(s) issue"
    yield IMPLEMENTATION, {"issue": ISSUE, "pr": PR}, "unknown key(s) pr"
    yield IMPLEMENTATION, {"issue": 2}, "issue must be a GitHub issue URL string"
    yield IMPLEMENTATION, {"issue": PR}, "issue is not a GitHub issue URL"
    yield IMPLEMENTATION, {"issue": ISSUE + "?x=1"}, "issue is not a GitHub issue URL"
    yield IMPLEMENTATION, {"issue": ISSUE + "#top"}, "issue is not a GitHub issue URL"
    yield IMPLEMENTATION, {"issue": "https://gitlab.com/owner/repo/issues/2"}, "not a GitHub"
    yield IMPLEMENTATION, {"issue": ""}, "not a GitHub issue URL"
    r = _valid(REVIEW)
    yield REVIEW, {**r, "round": True}, "round must be a JSON integer >= 1"
    yield REVIEW, {**r, "round": 1.0}, "round must be a JSON integer >= 1"
    yield REVIEW, {**r, "round": "1"}, "round must be a JSON integer >= 1"
    yield REVIEW, {**r, "round": 0}, "round must be a JSON integer >= 1"
    yield REVIEW, {**r, "round": -1}, "round must be a JSON integer >= 1"
    yield REVIEW, {k: v for k, v in r.items() if k != "needs_fix_round"}, "missing key(s)"
    yield REVIEW, {**r, "needs_fix_round": 0}, "needs_fix_round must be a JSON boolean"
    yield REVIEW, {**r, "needs_fix_round": "true"}, "needs_fix_round must be a JSON boolean"
    yield REVIEW, {**r, "reviewed_head_sha": SHA_A[:7]}, "40-character git SHA"
    yield REVIEW, {**r, "reviewed_head_sha": "g" * 40}, "40-character git SHA"
    yield REVIEW, {**r, "reviewed_head_sha": 1}, "40-character git SHA"
    yield REVIEW, {**r, "extra": 1}, "unknown key(s) extra"
    yield REVIEW, {**r, "finding_ids": "R1-F1"}, "finding_ids must be a JSON array"
    yield REVIEW, {**r, "finding_ids": [1]}, "finding_ids entry must be a string"
    yield REVIEW, {**r, "finding_ids": ["R2-F1"]}, "does not belong to round 1"
    yield REVIEW, {**r, "finding_ids": ["R1-F1", "R1-F1"]}, "finding_ids repeats an id"
    yield REVIEW, {**r, "finding_ids": ["R1-F1 "]}, "not a finding id"
    yield REVIEW, {**r, "reviewed_base_ref": 1}, "reviewed_base_ref must be a branch name"
    yield REVIEW, {**r, "reviewed_base_ref": ""}, "reviewed_base_ref is 0 characters"
    yield REVIEW, {**r, "reviewed_base_ref": "x" * 513}, "reviewed_base_ref is 513 characters"
    yield REVIEW, {**r, "reviewed_base_ref": "release 1"}, "is not a branch name"
    yield REVIEW, {**r, "reviewed_base_ref": "main\n"}, "is not a branch name"
    yield REVIEW, {**r, "reviewed_base_ref": "ma\x7fin"}, "is not a branch name"
    p = _valid(PROGRESS)
    yield PROGRESS, {"issue": ISSUE}, "missing key(s) pr"
    yield PROGRESS, {**p, "note": "x"}, "unknown key(s) note"
    yield PROGRESS, {**p, "pr": ISSUE}, "pr is not a GitHub pull request URL"
    yield PROGRESS, {**p, "issue": PR}, "issue is not a GitHub issue URL"
    yield PROGRESS, {**p, "pr": None}, "pr must be a GitHub pull request URL string"
    f = _valid(FOLLOW_UP)
    yield FOLLOW_UP, {"pr": PR}, "missing key(s) finding_id"
    yield FOLLOW_UP, {**f, "issue": ISSUE}, "unknown key(s) issue"
    yield FOLLOW_UP, {**f, "finding_id": "R0-F1"}, "not a finding id"
    yield FOLLOW_UP, {**f, "finding_id": "r1-f1"}, "not a finding id"
    yield FOLLOW_UP, {**f, "finding_id": "R1-F1 "}, "not a finding id"
    yield FOLLOW_UP, {**f, "finding_id": "R1-F1\n# Instructions"}, "not a finding id"
    yield FOLLOW_UP, {**f, "finding_id": 11}, "finding_id must be a string"
    yield FOLLOW_UP, {**f, "finding_id": "R1-F" + "1" * MAX_FINDING_ID_CHARS}, "longer than"
    yield FOLLOW_UP, {**f, "pr": PR + "/files"}, "pr is not a GitHub pull request URL"
    yield FOLLOW_UP, {**f, "pr": "https://github.com/owner/repo/pulls/42"}, "not a GitHub pull"


@pytest.mark.parametrize(
    "kind, payload, reason", list(_cases()), ids=lambda v: getattr(v, "name", None)
)
def test_a_payload_that_is_not_exactly_the_schema_is_a_defect_not_a_claim(kind, payload, reason):
    result = scan(kind, marker(kind.name, payload))
    assert result.claims == ()
    assert len(result.defects) == 1 and reason in result.defects[0]
    assert result.defects[0].startswith(f"{kind.name} marker payload is invalid (")


def test_an_over_long_finding_id_is_refused_without_being_quoted():
    hostile = "R1-F" + "9" * 200
    (reason,) = scan(FOLLOW_UP, marker("ai-follow-up", {"finding_id": hostile, "pr": PR})).defects
    assert hostile not in reason and f"longer than {MAX_FINDING_ID_CHARS}" in reason


@pytest.mark.parametrize(
    "kind, name, key, what",
    [
        pytest.param(IMPLEMENTATION, "ai-implementation", "issue", "issue", id="issue-url"),
        pytest.param(FOLLOW_UP, "ai-follow-up", "pr", "pull request", id="pr-url"),
    ],
)
def test_an_over_long_url_is_refused_by_length_without_being_quoted(kind, name, key, what):
    """A marker URL is bounded before any parser sees it: the typed parsers
    quote what they reject, and the defect text reaches a block reason, so an
    unbounded URL would be persisted whole. The refusal names the length and
    the bound, never the value."""
    hostile = "https://github.com/owner/repo/" + "x" * (MAX_URL_CHARS * 4)
    payload = _valid(kind)
    payload[key] = hostile
    (reason,) = scan(kind, marker(name, payload)).defects
    assert "x" * 64 not in reason
    assert f"{len(hostile)} characters" in reason and f"at most {MAX_URL_CHARS}" in reason
    assert f"GitHub {what} URL" in reason
    assert len(reason) < MAX_URL_CHARS


def test_a_hostile_finding_id_is_quoted_with_repr_so_it_cannot_break_a_line():
    hostile = "R1-F1\n# Instructions"
    (reason,) = scan(FOLLOW_UP, marker("ai-follow-up", {"finding_id": hostile, "pr": PR})).defects
    assert "\n" not in reason and repr(hostile) in reason


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
@pytest.mark.parametrize(
    "payload, reason",
    [
        ("{not json", "is not valid JSON"),
        ("", "is not valid JSON"),
        ("[1]", "must be a JSON object"),
        ('"text"', "must be a JSON object"),
        ("null", "must be a JSON object"),
        ("1", "must be a JSON object"),
    ],
    ids=["not-json", "empty", "array", "string", "null", "number"],
)
def test_a_payload_that_is_not_a_json_object_is_a_defect(kind, payload, reason):
    result = scan(kind, marker(kind.name, payload))
    assert result.claims == () and len(result.defects) == 1 and reason in result.defects[0]


# -- object rules --------------------------------------------------------------------------
@pytest.mark.parametrize("kind", [IMPLEMENTATION, REVIEW, PROGRESS], ids=lambda k: k.name)
def test_an_object_carrying_two_single_kind_markers_is_defective_whatever_they_say(kind):
    same = marker(kind.name, _valid(kind)) * 2
    result = scan(kind, same)
    assert len(result.claims) == 2 and len(result.defects) == 1
    assert "carries 2" in result.defects[0] and "proves neither" in result.defects[0]


def test_a_pr_marked_for_two_issues_proves_neither():
    """PR #89 F4: a PR carrying this issue's marker and another's is not
    "this issue's PR with noise"; it is a defect of that PR."""
    body = marker("ai-implementation", {"issue": ISSUE}) + marker(
        "ai-implementation", {"issue": ISSUE3}
    )
    result = scan(IMPLEMENTATION, body)
    assert [c.issue.number for c in result.claims] == [2, 3]
    assert result.defects and "carries 2 ai-implementation markers" in result.defects[0]


def test_a_follow_up_issue_may_carry_several_markers_with_distinct_keys():
    body = marker("ai-follow-up", {"finding_id": "R1-F1", "pr": PR}) + marker(
        "ai-follow-up", {"finding_id": "R2-F1", "pr": PR}
    )
    result = scan(FOLLOW_UP, body)
    assert [c.finding_id for c in result.claims] == ["R1-F1", "R2-F1"] and result.defects == ()


def test_a_follow_up_issue_repeating_a_key_is_defective():
    body = marker("ai-follow-up", {"finding_id": "R1-F1", "pr": PR}) + marker(
        "ai-follow-up", {"finding_id": "R1-F1", "pr": PR.replace("owner", "OWNER")}
    )
    result = scan(FOLLOW_UP, body)
    assert len(result.claims) == 2 and result.defects == (
        "carries the same ai-follow-up marker more than once",
    )


def test_a_defective_marker_beside_a_valid_one_still_makes_the_object_defective():
    body = marker("ai-implementation", "{broken") + marker("ai-implementation", {"issue": ISSUE})
    result = scan(IMPLEMENTATION, body)
    assert len(result.claims) == 1 and len(result.defects) == 1


def test_an_unterminated_marker_does_not_swallow_a_later_valid_marker():
    """`<!-- ai-implementation: {"issue": ...` with no `-->` must not match up
    to the next `-->` and hide the valid marker inside the match."""
    valid = marker("ai-implementation", {"issue": ISSUE})
    body = '<!-- ai-implementation: {"issue": "' + ISSUE + '"\n\n' + valid + "\n"
    result = scan(IMPLEMENTATION, body)
    assert len(result.claims) == 1 and result.claims[0].issue.number == 2
    assert result.defects == ()
    result = scan(IMPLEMENTATION, '<!-- ai-implementation: {"issue": "' + ISSUE + '"')
    assert result == scan(IMPLEMENTATION, "")


def test_a_marker_whose_payload_opens_another_comment_is_not_one_marker():
    body = (
        "<!-- ai-implementation: <!-- ai-implementation: " + json.dumps({"issue": ISSUE}) + " -->"
    )
    result = scan(IMPLEMENTATION, body)
    assert len(result.claims) == 1 and result.defects == ()


# -- collections and cardinality ------------------------------------------------------------
def _issue_key(url: str):
    return parse_issue_url(url).identity


def test_collect_scans_every_object_once_and_answers_each_key():
    prs = [
        Obj(PR, marker("ai-implementation", {"issue": ISSUE})),
        Obj(PR41, marker("ai-implementation", {"issue": ISSUE3})),
        Obj("https://github.com/owner/repo/pull/40", "no marker at all"),
    ]
    c = collect(IMPLEMENTATION, prs, "open PR")
    assert c.defects == () and [h.obj.url for h in c.holders] == [PR, PR41]
    assert c.claimants(_issue_key(ISSUE), "issue #2").at_most_one().obj.url == PR
    assert c.claimants(_issue_key(ISSUE3), "issue #3").exactly_one().obj.url == PR41
    assert (
        c.claimants(_issue_key("https://github.com/owner/repo/issues/9"), "#9").at_most_one()
        is None
    )


def test_identity_is_compared_case_insensitively_and_by_parsed_url_not_string():
    variant = "https://github.com/OWNER/Repo/issues/2"
    c = collect(
        IMPLEMENTATION, [Obj(PR, marker("ai-implementation", {"issue": variant}))], "open PR"
    )
    assert c.claimants(_issue_key(ISSUE), "issue #2").exactly_one().obj.url == PR
    c = collect(
        FOLLOW_UP,
        [
            Obj(
                ISSUE3,
                marker(
                    "ai-follow-up",
                    {"finding_id": "R1-F1", "pr": "https://github.com/Owner/REPO/pull/42"},
                ),
            )
        ],
        "open issue",
    )
    assert (
        c.claimants((parse_pr_url(PR).identity, "R1-F1"), "R1-F1").exactly_one().obj.url == ISSUE3
    )
    assert c.claimants((parse_pr_url(PR41).identity, "R1-F1"), "R1-F1").at_most_one() is None


def test_exactly_one_refuses_absence_and_at_most_one_accepts_it():
    c = collect(IMPLEMENTATION, [], "open PR")
    q = c.claimants(_issue_key(ISSUE), "issue #2")
    assert q.at_most_one() is None
    with pytest.raises(ClaimConflictError) as exc:
        q.exactly_one()
    assert str(exc.value) == "no open PR carries the ai-implementation marker for issue #2"


def test_two_claimants_are_refused_by_both_questions_naming_both_objects():
    c = collect(
        IMPLEMENTATION,
        [
            Obj(PR41, marker("ai-implementation", {"issue": ISSUE})),
            Obj(PR, marker("ai-implementation", {"issue": ISSUE})),
        ],
        "open PR",
    )
    q = c.claimants(_issue_key(ISSUE), "issue #2")
    for ask in (q.at_most_one, q.exactly_one):
        with pytest.raises(ClaimConflictError) as exc:
            ask()
        assert str(exc.value) == (
            f"2 open PRs carry the ai-implementation marker for issue #2 ({PR41}, {PR})"
        )


def test_a_defect_on_an_unrelated_object_makes_every_question_inconclusive():
    """ "No PR claims issue #2" is not provable while some PR carries a marker
    that could not be read: it may be #2's PR, botched. Both questions refuse,
    and the reason names the defective object, not the key's holders."""
    c = collect(
        IMPLEMENTATION,
        [
            Obj(PR, marker("ai-implementation", {"issue": ISSUE})),
            Obj(PR41, marker("ai-implementation", "{oops")),
        ],
        "open PR",
    )
    q = c.claimants(_issue_key(ISSUE), "issue #2")
    for ask in (q.at_most_one, q.exactly_one):
        with pytest.raises(ClaimConflictError) as exc:
            ask()
        assert str(exc.value).startswith(
            "cannot establish which open PR carries the ai-implementation marker for issue #2: "
            f"open PR {PR41}: ai-implementation marker payload is not valid JSON"
        )
    with pytest.raises(ClaimConflictError, match=f"open PR {PR41}: ai-implementation"):
        c.grouped(lambda claim: True, "issue #2")


def test_foreign_valid_claims_are_neither_claimants_nor_defects():
    c = collect(
        FOLLOW_UP,
        [
            Obj(ISSUE3, marker("ai-follow-up", {"finding_id": "R1-F1", "pr": PR41})),
            Obj(
                "https://github.com/owner/repo/issues/4",
                marker("ai-follow-up", {"finding_id": "R1-F2", "pr": PR}),
            ),
        ],
        "open issue",
    )
    assert c.defects == ()
    assert c.claimants((parse_pr_url(PR).identity, "R1-F1"), "R1-F1").at_most_one() is None
    mine = c.grouped(lambda claim: claim.pr.identity == parse_pr_url(PR).identity, "PR #42")
    assert {k[1] for k in mine} == {"R1-F2"}


def test_grouped_keeps_every_holder_of_a_key_and_applies_the_selection():
    a = Obj(ISSUE3, marker("ai-follow-up", {"finding_id": "R1-F1", "pr": PR}))
    b = Obj(
        "https://github.com/owner/repo/issues/4",
        marker("ai-follow-up", {"finding_id": "R1-F1", "pr": PR}),
    )
    c = collect(FOLLOW_UP, [a, b], "open issue")
    key = (parse_pr_url(PR).identity, "R1-F1")
    assert [h.obj for h in c.grouped(lambda claim: True, "PR")[key]] == [a, b]
    assert c.grouped(lambda claim: claim.finding_id != "R1-F1", "PR") == {}
    with pytest.raises(ClaimConflictError, match="2 open issues carry"):
        c.claimants(key, "finding R1-F1").at_most_one()


def test_claimants_message_uses_the_noun_and_what_it_was_given():
    q = Claimants(REVIEW, "round 3 at HEAD abc", (), ("comment x: broken",), "comment")
    with pytest.raises(ClaimConflictError) as exc:
        q.at_most_one()
    assert str(exc.value) == (
        "cannot establish which comment carries the ai-review-result marker for round 3 "
        "at HEAD abc: comment x: broken"
    )


# -- renderers ------------------------------------------------------------------------------
def test_renderers_emit_canonical_urls_and_round_trip_through_the_decoders():
    impl = render_implementation_marker("https://github.com/OWNER/Repo/issues/2")
    assert impl == '<!-- ai-implementation: {"issue": "https://github.com/OWNER/Repo/issues/2"} -->'
    assert scan(IMPLEMENTATION, impl).claims[0].key == _issue_key(ISSUE)
    prog = render_progress_marker(ISSUE, PR + "/")
    assert prog == f'<!-- ai-epic-progress: {{"issue": "{ISSUE}", "pr": "{PR}"}} -->'
    assert scan(PROGRESS, prog).defects == ()
    fu = render_follow_up_marker(PR, "R2-F3")
    assert fu == f'<!-- ai-follow-up: {{"finding_id": "R2-F3", "pr": "{PR}"}} -->'
    assert scan(FOLLOW_UP, fu).claims[0].key == (parse_pr_url(PR).identity, "R2-F3")


def test_a_renderer_refuses_to_emit_a_marker_the_decoder_would_reject():
    with pytest.raises(ValueError, match="not a finding id"):
        render_follow_up_marker(PR, "F1")
    with pytest.raises(ValueError, match="not a finding id"):
        render_follow_up_marker(PR, "R1-F1\n")
    with pytest.raises(ConfigurationError):
        render_implementation_marker(PR)  # a PR is not an issue
    with pytest.raises(ConfigurationError):
        render_progress_marker(ISSUE, ISSUE)


# -- scanning cost is part of the contract ------------------------------------------

# GitHub caps a body at 65536 characters; every shape stays under that.
_HOSTILE_BODIES = {
    "unterminated marker then blanks": "<!-- ai-implementation: " + " " * 65000,
    "unterminated payload then blanks": "<!-- ai-implementation: a" + " " * 65000,
    "many unterminated markers with blank runs": ("<!-- ai-implementation: a" + " " * 3000) * 20,
    "unterminated marker then text": "<!-- ai-implementation: " + "a" * 65000,
    "openers only": "<!-- " * 13000,
    "named openers only": "<!-- ai-implementation:" * 2800,
    "blanks inside a valid marker": "<!-- ai-implementation:" + " " * 65000 + '{"issue": 1} -->',
}


@pytest.mark.parametrize("shape", sorted(_HOSTILE_BODIES))
def test_scanning_a_hostile_body_takes_linear_time(shape):
    """The pattern runs over every open issue and PR body the controller lists,
    text anyone can edit. An unterminated marker followed by a run of blanks
    used to cost seconds per kilobyte (the lazy payload and a trailing `\\s*`
    competed for the blanks); the scan must stay a single linear pass."""
    body = _HOSTILE_BODIES[shape]
    assert len(body) <= 65536
    started = time.perf_counter()
    result = scan(IMPLEMENTATION, body)
    assert time.perf_counter() - started < 1.0, shape
    assert result.claims == ()


def test_payload_whitespace_is_stripped_before_decoding():
    """Blanks and newlines around the payload are layout, not data; a payload
    that is only blanks is an empty payload, reported as one."""
    padded = '<!--   ai-implementation  :  \n {"issue": "' + ISSUE + '"} \n\t -->'
    result = scan(IMPLEMENTATION, padded)
    assert len(result.claims) == 1 and result.claims[0].issue.number == 2
    assert result.defects == ()
    blank = scan(IMPLEMENTATION, "<!-- ai-implementation:    \n   -->")
    assert blank.claims == () and len(blank.defects) == 1
    assert "not valid JSON" in blank.defects[0]
