# Native EPIC graph fixtures (#179)

GitHub's REST answers for the reads behind `autoforge.epic_graph` (ADR 0008
D3), used by `tests/test_github.py` and `tests/test_epic_graph.py`. They are
the graph of EPIC #176 of `iceboundrock/AutoForge`:

| File | Endpoint |
|---|---|
| `repository.json` | `GET /repos/iceboundrock/AutoForge` |
| `issue_176.json` | `GET .../issues/176` (the EPIC) |
| `issue_176_sub_issues.json` | `GET .../issues/176/sub_issues?per_page=100` (one page, all 7 rows) |
| `issue_179_blocked_by.json` | `GET .../issues/179/dependencies/blocked_by?per_page=100` |
| `issue_182_blocked_by.json` | `GET .../issues/182/dependencies/blocked_by?per_page=100` |
| `issue_183_blocked_by.json` | `GET .../issues/183/dependencies/blocked_by?per_page=100` |
| `issue_182.json` | `GET .../issues/182` (a child, for the re-read) |

**Provenance.** Captured live on 2026-10-19 between 19:05:19 and 19:05:23
UTC, with unauthenticated, read-only `GET`s against the public repository
(`Accept: application/vnd.github+json`, API version `2022-11-28`). Every
listing fit on one page: no answer carried a `Link` header. The only
change to the captured JSON is each issue's `body`. It is replaced by
`"[body omitted from the recorded fixture: the graph never reads it]"`.
The JSON is re-indented, with key order and every other key and value
exactly as GitHub returned them. That includes titles, users, reactions
and the full `repository` object in listing rows.

## The two behaviours #179 depends on

GitHub's REST reference does not settle either behaviour. "List sub-issues"
and "List dependencies an issue is blocked by" take no state filter.
Neither page says whether closed issues are listed. The `Issue` schema
lists `sub_issues_summary` (`total`, `completed`, `percent_completed`) and
`issue_dependencies_summary` (`blocked_by`, `total_blocked_by`,
`blocking`, `total_blocking`) without saying what they count. The live
answers here settle both behaviours:

- **The sub-issue listing includes closed children.** #176 reports
  `sub_issues_summary.total: 7`, `completed: 4`. Its listing holds all
  seven rows: #177, #178, #180 and #181 closed as `completed`, and #179,
  #182 and #183 open.
- **`total_blocked_by` counts closed blockers, and `blocked_by` counts
  only open ones.** #179 reports `blocked_by: 0`, `total_blocked_by: 2`.
  Its listing holds #177 and #158, both closed as `completed`. #182
  reports 1 and 3 and lists the open #179 with the closed #158 and #181.
  #183 reports 2 and 4 and lists the open #179 and #182 with the closed
  #158 and #181. The EPIC #176 reports 0 and 1, for its closed blocker
  #158.

Both match what the snapshot assumed, so its completeness check is
unchanged: each child listing must equal `sub_issues_summary.total`, and
each blocker listing must equal `total_blocked_by`. If GitHub ever counted
otherwise, a listing that holds a closed child or a closed blocker would
be longer than its count, and the snapshot would be refused. The tests
that serve these files with one condition altered prove it. The one case
that check could not see is a child whose blockers are all closed. It
would report zero and its listing would not be read. #179 above shows
that GitHub counts that case too: none open, two in all.

## A third observation: `parent_issue_url` is omitted, not null

GitHub leaves `parent_issue_url` out of an issue that has no parent. The
REST schema calls the field "string or null", but the EPIC's own answer
(`issue_176.json`) has no such key. Neither does #158's row in each
`blocked_by` listing. Every child row and `issue_182.json` carry the field.
The client first refused a row without the field, which would have refused
every snapshot of an EPIC without a parent. It now reads an absent field as
no parent. The sub-issue summaries are still required.
