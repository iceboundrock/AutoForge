"""The byte-fidelity capture of an operator comment's payload (K10, #243 sections 12 and 14).

Exact evidence (ADR 0004 D5.9) rests on one premise no offline test can
establish: a payload the operator posts through the documented pipe,

    autoforge status --action X-... | gh issue comment <target> --body-file -

comes back from :meth:`autoforge.github.GitHubClient.get_issue_comments`
byte for byte. A maintainer establishes it once, by hand, on a scratch issue
they own, with this module (``uv run python -m tests.operator_comment_fidelity
...``, from the repository root). It is never part of the suite and never
writes to GitHub itself: the maintainer posts, as an operator would.

1. ``render --via pipe --issue <scratch issue URL>`` writes the payload of
   :func:`payload` exactly as ``status --action`` writes one (``sys.stdout``,
   nothing before or after it). Pipe it into ``gh issue comment <scratch
   issue URL> --body-file -``.
2. ``render --via web --issue <scratch issue URL> > web.md``: open the file,
   copy all of it, paste it into the scratch issue's comment box on
   github.com, and submit.
3. ``capture --issue <scratch issue URL>`` reads the scratch issue's
   complete comment listing with the controller's own client (the
   maintainer's ``gh`` credential), keeps the raw GraphQL node of each of the
   two comments, and writes :data:`FIXTURE`, with what ran (``gh --version``,
   the commit) and when.

``tests/test_operator_comment_fidelity.py`` replays the recorded nodes
through ``get_issue_comments`` and pins the controller's decision on them.
If the pipe's comment is not byte-equal to the payload, ``capture`` says so
and the fixture is committed exactly as captured anyway: the test then
fails, and #243's M2 stops so that ADR 0004 revisits exact evidence. Never
write or edit :data:`FIXTURE` by hand.

The payloads are those of a real operator action of the scratch issue's
repository: the run's issue #243 and PR #254 (both of that repository; pass
``--run-issue`` and ``--pr`` for another), review round 2, findings
:data:`FINDINGS` (one per route, so that each comment carries a marker key
of its own). No run reads them: a K10 target is an issue its run's issue
body references or its EPIC, never a scratch issue.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from autoforge.claims import OPERATOR_COMMENT, render_operator_comment_marker, scan
from autoforge.effects import normalized_comment_text, operator_comment_body, sha256_text
from autoforge.executor import ExecutionRequest, ExecutionResult, execute
from autoforge.github import GitHubClient
from autoforge.result_parser import validate_operator_comment_body
from autoforge.validation import parse_comment_url, parse_issue_url

FIXTURE = Path(__file__).parent / "fixtures" / "operator_comment_fidelity" / "capture.json"
RUN_ISSUE_NUMBER = 243
PR_NUMBER = 254
ROUND = 2
FINDINGS = {"pipe": "R2-F4", "web": "R2-F5"}
ROUTES = {
    "pipe": "the documented pipe: status --action's bytes into gh issue comment --body-file -",
    "web": "a paste of the same bytes into github.com's comment box",
}


def text(repository: str) -> str:
    """The agent's part of both payloads: what an editor or a renderer could change.

    Paragraphs, a list, a code span, an indented code line, a link of the
    run's repository, non-ASCII characters and a line ending in two spaces
    (a Markdown hard break), within the parser's rules for the text.
    """
    return (
        "Coordination note for the byte-fidelity capture of #243 (sections 12 and 14).\n"
        "\n"
        "- A list item with a `code span` and a link to "
        f"https://github.com/{repository}/blob/main/docs/usage.md.\n"
        "- Non-ASCII text: café, naïve, 中文, — and ✓.\n"
        "\n"
        "A line ending in two spaces, a Markdown hard break:  \n"
        "and the line after it.\n"
        "\n"
        "    an indented code line\n"
        "\n"
        "The last line."
    )


def _urls(scratch: str, run_issue: int, pr: int) -> tuple[str, str, str]:
    """(scratch issue, run issue, PR), canonical, all of the scratch issue's repository."""
    ref = parse_issue_url(scratch)
    base = f"https://github.com/{ref.repository}"
    return ref.canonical, f"{base}/issues/{run_issue}", f"{base}/pull/{pr}"


def payload(via: str, scratch: str, run_issue: int = RUN_ISSUE_NUMBER, pr: int = PR_NUMBER) -> str:
    """The exact bytes of ``via``'s operator comment on ``scratch``, as FIX renders one."""
    target, issue_url, pr_url = _urls(scratch, run_issue, pr)
    body = validate_operator_comment_body(
        text(parse_issue_url(target).repository),
        repository=parse_issue_url(target).repository,
    )
    return operator_comment_body(body, issue_url, pr_url, ROUND, FINDINGS[via], target)


def _key(via: str, scratch: str, run_issue: int, pr: int) -> object:
    target, issue_url, pr_url = _urls(scratch, run_issue, pr)
    marker = render_operator_comment_marker(issue_url, pr_url, ROUND, FINDINGS[via], target)
    return scan(OPERATOR_COMMENT, marker).claims[0].key


def _recording(runner: Callable[[ExecutionRequest], ExecutionResult], pages: list[str]):
    """``runner``, keeping each answer's stdout (the raw GraphQL pages)."""

    def run(req: ExecutionRequest) -> ExecutionResult:
        result = runner(req)
        pages.append(result.stdout)
        return result

    return run


def _nodes(pages: list[str]) -> list[dict]:
    """Every comment node of the recorded listing pages, as GitHub returned it."""
    nodes: list[dict] = []
    for page in pages:
        try:
            data = json.loads(page)
        except ValueError:
            continue
        issue = ((data.get("data") or {}).get("repository") or {}).get("issue") or {}
        nodes += (issue.get("comments") or {}).get("nodes") or []
    return nodes


def _output(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return ""
    return done.stdout.strip()


def capture(
    scratch: str,
    run_issue: int,
    pr: int,
    output: Path,
    *,
    runner: Callable[[ExecutionRequest], ExecutionResult] = execute,
    describe: Callable[[list[str]], str] = _output,
) -> int:
    """Read the scratch issue's listing and write the fixture; 1 when a route is missing.

    ``runner`` runs ``gh`` (the real one, with the maintainer's credential)
    and ``describe`` the local version reads; a test passes stand-ins.
    """
    target, issue_url, pr_url = _urls(scratch, run_issue, pr)
    pages: list[str] = []
    comments = GitHubClient(runner=_recording(runner, pages)).get_issue_comments(target)
    raw = {parse_comment_url(str(n.get("url", ""))).comment_id: n for n in _nodes(pages)}
    posts: dict[str, dict] = {}
    verdict = 0
    for via in FINDINGS:
        key = _key(via, target, run_issue, pr)
        found = [
            c for c in comments if any(k.key == key for k in scan(OPERATOR_COMMENT, c.body).claims)
        ]
        if len(found) != 1:
            print(
                f"{via}: {len(found)} comments on {target} carry the {FINDINGS[via]} marker; "
                "post exactly one, as the module docstring says, and capture again",
                file=sys.stderr,
            )
            verdict = 1
            continue
        comment = found[0]
        expected = payload(via, target, run_issue, pr)
        posts[via] = {
            "finding_id": FINDINGS[via],
            "route": ROUTES[via],
            "payload": expected,
            "payload_sha256": sha256_text(expected),
            "node": raw[comment.id],
        }
        same = comment.body == expected
        close = normalized_comment_text(comment.body) == normalized_comment_text(expected)
        print(
            f"{via}: {comment.url}: "
            + (
                "byte-equal to the payload"
                if same
                else "differs only in line endings or trailing whitespace"
                if close
                else "differs from the payload"
            )
            + f" (sha256 {sha256_text(comment.body)})"
        )
        if via == "pipe" and not same:
            print(
                "The documented pipe does not round-trip the payload: commit the fixture as "
                "captured; #243's M2 stops and ADR 0004 revisits exact evidence",
                file=sys.stderr,
            )
    if verdict:
        return verdict
    document = {
        "captured_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gh_version": (describe(["gh", "--version"]).splitlines() or [""])[0],
        "autoforge_commit": describe(["git", "rev-parse", "HEAD"]),
        "scratch_issue": target,
        "run_issue": issue_url,
        "pr": pr_url,
        "round": ROUND,
        "posts": posts,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"wrote {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.operator_comment_fidelity")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("render", "capture"):
        command = sub.add_parser(name)
        command.add_argument("--issue", required=True, help="the scratch issue's URL")
        command.add_argument("--run-issue", type=int, default=RUN_ISSUE_NUMBER)
        command.add_argument("--pr", type=int, default=PR_NUMBER)
        if name == "render":
            command.add_argument("--via", choices=sorted(FINDINGS), required=True)
        else:
            command.add_argument("--output", type=Path, default=FIXTURE)
    args = parser.parse_args(argv)
    if args.command == "render":
        # As ``status --action`` writes a payload (cli._print_action_payload).
        sys.stdout.write(payload(args.via, args.issue, args.run_issue, args.pr))
        sys.stdout.flush()
        return 0
    return capture(args.issue, args.run_issue, args.pr, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
