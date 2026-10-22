"""The byte-fidelity fixture of an operator comment (K10, #243 sections 12 and 14).

``tests/fixtures/operator_comment_fidelity/capture.json`` is what a
maintainer captured once, on a scratch issue, with
``tests/operator_comment_fidelity.py``: the raw GraphQL nodes GitHub
returned for one payload posted through the documented pipe (``status
--action X-... | gh issue comment <target> --body-file -``) and one pasted
into the web UI. These tests replay the nodes through
``GitHubClient.get_issue_comments`` and K10's own operation, and pin what
the controller decides on each.

The pipe's comment must be the payload byte for byte: exact evidence rests
on it. Should a capture show otherwise, the fixture is still committed as
captured, this test fails, and #243's M2 stops so that ADR 0004 revisits
exact evidence; the fixture is never written or edited by hand. It is
#243's M2 prerequisite (PR #254 R2-F4), and nothing here stands in for it.

The committed capture (scratch issue #264, 2026-10-21, gh 2.102.1) shows
the pipe's comment byte-equal to the payload, so it is exact evidence. The
web paste is not: it came back without the two trailing spaces of the
hard-break line and with ``%`` and two line feeds after the marker
(apparently the shell's end-of-line mark, copied with the text from a
terminal), so it is a conflict. The last test runs the capture tool itself
offline, so that a live run yields a fixture these tests read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoforge.claims import render_operator_comment_marker
from autoforge.effect_ops import OperatorCommentOp
from autoforge.effects import (
    Action,
    Decision,
    EffectKind,
    EffectOwner,
    EffectRecord,
    normalized_comment_text,
    sha256_text,
)
from autoforge.executor import ExecutionRequest, ExecutionResult
from autoforge.github import GitHubClient
from autoforge.transitions import Phase
from autoforge.validation import parse_comment_url, parse_issue_url
from tests import operator_comment_fidelity as fidelity

SCRATCH = "https://github.com/owner/repo/issues/9001"


def _capture(path: Path = fidelity.FIXTURE) -> dict:
    if not path.is_file():
        pytest.fail(
            f"{path.relative_to(Path(__file__).parent.parent)} is missing. It is #243's M2 "
            "prerequisite (PR #254 R2-F4): a maintainer captures it once on a scratch issue "
            "with `uv run python -m tests.operator_comment_fidelity` (render, post through "
            "the pipe and the web UI, capture; see that module). Never write it by hand."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _page(nodes: list[dict]) -> str:
    """One complete page of the scratch issue's listing, as `gh api graphql` answers."""
    return json.dumps(
        {
            "data": {
                "repository": {
                    "issue": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": nodes,
                        }
                    }
                }
            }
        }
    )


def _replaying(nodes: list[dict]) -> GitHubClient:
    def run(req: ExecutionRequest) -> ExecutionResult:
        assert req.command[1:3] == ["api", "graphql"], req.command
        return ExecutionResult(req.command, None, 0, _page(nodes), "", "t", "t")

    return GitHubClient(runner=run, retry_delay_seconds=0)


def _record(capture: dict, via: str) -> EffectRecord:
    """The K10 record of ``via``'s payload, loaded as recovery loads one (cross-checked)."""
    target = capture["scratch_issue"]
    post = capture["posts"][via]
    marker = render_operator_comment_marker(
        capture["run_issue"], capture["pr"], capture["round"], post["finding_id"], target
    )
    record = EffectRecord(
        position=0,
        kind=EffectKind.OPERATOR_COMMENT,
        owner=EffectOwner("capture", Phase.FIX, capture["run_issue"], capture["pr"], ""),
        identity={"issue_url": target, "marker": marker},
        target={"issue_url": target},
        precondition={"allowed_by": ["issue_body"], "issue_body_sha256": "0" * 64, "binding": None},
        payload={"body": post["payload"]},
        completion="operator_comment_exact_or_bound_in_complete_listing",
    )
    return EffectRecord.from_dict(record.to_dict())


def _decide(capture: dict, via: str) -> tuple[str, Decision]:
    """(the body ``get_issue_comments`` returns for ``via``'s comment, K10's decision on it)."""
    nodes = [p["node"] for p in capture["posts"].values()]
    client = _replaying(nodes)
    record = _record(capture, via)
    cid = parse_comment_url(capture["posts"][via]["node"]["url"]).comment_id
    [comment] = [c for c in client.get_issue_comments(capture["scratch_issue"]) if c.id == cid]
    return comment.body, OperatorCommentOp(client, plan=[record]).reconcile_listing(record)


def test_the_capture_is_of_the_payload_fix_renders_today():
    """The recorded payloads are today's renderings of the recorded inputs:
    a change to the header, the marker or the text rules needs a new capture."""
    capture = _capture()
    target = capture["scratch_issue"]
    run_issue = parse_issue_url(capture["run_issue"]).number
    pr = int(capture["pr"].rsplit("/", 1)[1])
    assert set(capture["posts"]) == set(fidelity.FINDINGS)
    for via, post in capture["posts"].items():
        assert post["finding_id"] == fidelity.FINDINGS[via]
        assert post["payload"] == fidelity.payload(via, target, run_issue, pr)
        assert post["payload_sha256"] == sha256_text(post["payload"])
    assert capture["captured_at"] and capture["gh_version"]


def test_the_documented_pipe_round_trips_the_payload_byte_for_byte():
    """The premise of exact evidence. A failure here is a finding about
    GitHub or `gh`, never about the fixture: M2 stops and ADR 0004 revisits
    exact evidence (#243 section 14)."""
    capture = _capture()
    body, decision = _decide(capture, "pipe")
    post = capture["posts"]["pipe"]
    payload = post["payload"]
    assert body == post["node"]["body"]  # the client hands the listed body on unchanged
    assert body == payload, (
        f"the pipe's comment is not the payload (sha256 {sha256_text(body)}, payload "
        f"{sha256_text(payload)})"
    )
    assert decision.action is Action.OBSERVE and decision.observed is not None
    assert decision.observed["evidence"] == "exact"
    assert decision.observed["url"] == post["node"]["url"]
    assert decision.observed["body_sha256"] == sha256_text(payload) == post["payload_sha256"]


def test_the_captured_web_ui_paste_is_not_the_payload_and_is_a_conflict():
    """The web route of the capture: the paste lost the two trailing spaces
    of the payload's hard break and gained ``%`` and two line feeds after
    the marker. Its own key with other bytes is a conflict, never exact
    evidence; as the difference is more than whitespace, the reason does
    not call it whitespace-only."""
    capture = _capture()
    body, decision = _decide(capture, "web")
    post = capture["posts"]["web"]
    payload = post["payload"]
    hard_break = "A line ending in two spaces, a Markdown hard break:"
    assert f"{hard_break}  \n" in payload
    assert body == post["node"]["body"]  # the client hands the listed body on unchanged
    assert body == payload.replace(f"{hard_break}  \n", f"{hard_break}\n") + "%\n\n"
    assert normalized_comment_text(body) != normalized_comment_text(payload)
    assert decision.action is Action.CONFLICT and decision.observed is None
    assert f"{post['node']['url']} carries its marker but its body is not the prepared payload" in (
        decision.reason
    )
    assert "only in line endings or trailing whitespace" not in decision.reason


def test_the_capture_tool_writes_a_fixture_these_tests_read(tmp_path, monkeypatch):
    """Offline: ``capture`` over a listing holding unrelated chatter, the
    pipe's exact payload and a web paste with CRLF line endings keeps the
    two raw nodes and writes a fixture the replay decides on; with a route
    missing it writes nothing."""
    nodes = [
        {
            "url": f"{SCRATCH}#issuecomment-{cid}",
            "body": body,
            "author": {"login": "maintainer"},
            "createdAt": "2026-10-21T00:00:00Z",
        }
        for cid, body in [
            (1, "Scratch issue for the K10 capture."),
            (2, fidelity.payload("pipe", SCRATCH)),
            (3, fidelity.payload("web", SCRATCH).replace("\n", "\r\n")),
        ]
    ]
    seen: list[list[str]] = []

    def run(req: ExecutionRequest) -> ExecutionResult:
        seen.append(req.command)
        return ExecutionResult(req.command, None, 0, _page(nodes), "", "t", "t")

    def describe(argv: list[str]) -> str:
        return {"gh": "gh version 2.81.0 (2026-10-01)", "git": "0" * 40}[argv[0]]

    output = tmp_path / "capture.json"
    assert fidelity.capture(SCRATCH, 243, 254, output, runner=run, describe=describe) == 0
    assert [c[:3] for c in seen] == [["gh", "api", "graphql"]]
    capture = _capture(output)
    assert capture["gh_version"] == "gh version 2.81.0 (2026-10-01)"
    assert capture["posts"]["pipe"]["node"] == nodes[1]
    assert capture["posts"]["web"]["node"] == nodes[2]
    assert "\\r\\n" in output.read_text(encoding="utf-8")  # escaped, so no editor changes it
    body, decision = _decide(capture, "pipe")
    assert body == capture["posts"]["pipe"]["payload"]
    assert decision.action is Action.OBSERVE and decision.observed is not None
    body, decision = _decide(capture, "web")
    assert decision.action is Action.CONFLICT
    assert "only in line endings or trailing whitespace" in decision.reason
    missing = tmp_path / "missing.json"
    nodes.pop(2)
    assert fidelity.capture(SCRATCH, 243, 254, missing, runner=run, describe=describe) == 1
    assert not missing.exists()
