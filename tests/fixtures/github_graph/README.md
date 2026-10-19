# Native EPIC graph fixtures (#179)

REST answers for the reads behind `autoforge.epic_graph` (ADR 0008 D3),
used by `tests/test_github.py` and `tests/test_epic_graph.py`:

| File | Endpoint |
|---|---|
| `repository.json` | `GET /repos/octo-org/octo-repo` |
| `epic_issue.json` | `GET /repos/octo-org/octo-repo/issues/158` (the EPIC) |
| `epic_sub_issues.json` | one page of `GET .../issues/158/sub_issues?per_page=100` |
| `issue_180.json` | `GET .../issues/180` (a child) |
| `issue_180_blocked_by.json` | one page of `GET .../issues/180/dependencies/blocked_by?per_page=100` |

**Provenance.** These files were not captured from a live call. The phase
that wrote them had no GitHub access. They follow GitHub's REST reference
for the `Issue` object the sub-issue and issue-dependency endpoints return
("Sub-issues" and "Issue dependencies" in the REST docs): `id`, `number`,
`html_url`, `repository_url`, `state`, `state_reason`, `parent_issue_url`,
`sub_issues_summary` (`total`, `completed`, `percent_completed`) and
`issue_dependencies_summary` (`blocked_by`, `total_blocked_by`, `blocking`,
`total_blocking`), plus the usual fields the graph never reads (title,
body, labels, user, timestamps). Replace them with captured answers when
they are re-recorded. Keep the same shapes and the two conditions below.

They pin the two behaviours #179 depends on:

- **The sub-issue listing includes closed children.** The EPIC reports
  `sub_issues_summary.total: 3` with `completed: 1`, and the listing holds
  all three, the closed `#178` among them. `completed` and
  `percent_completed` only make sense if `total` counts closed children.
- **`total_blocked_by` counts closed blockers.** `#180` reports
  `blocked_by: 1` (open blockers) and `total_blocked_by: 2`. Its listing
  holds the open `#179` and the closed `#178`.

If GitHub ever behaves differently, the snapshot still fails closed. A
listing without closed children, or a `total_blocked_by` without closed
blockers, makes the listing's length differ from the count it is checked
against, and the snapshot is refused. It is never accepted incomplete.
The tests that serve these files with one condition altered prove it.
