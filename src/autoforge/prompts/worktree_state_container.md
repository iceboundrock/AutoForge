## Your workspace starts from a bound revision

This launch runs in a private workspace of its own, not in a worktree an
earlier launch used. The controller seeded it from the revision it bound
for this phase entry and, when an earlier launch of the same entry
committed work the controller imported, from that commit as well: your
`HEAD` starts there.

Uncommitted work from an earlier launch is never carried into this one:
the controller discards each launch's workspace when the launch ends, and
only a commit it imported is kept. So there are no leftover changes here
for you to inspect, continue from or discard, and whatever you leave
uncommitted when you finish is lost. Commit your work before you finish,
as the steps below require.
