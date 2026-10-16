## The controller's validation failed

The controller ran the configured validation commands (`validation.commands`)
against the commit an earlier launch of this phase reported, and one did not
pass. So nothing was pushed or published and no review started, and this
launch is the same phase again.

- Candidate: {{FAILED_CANDIDATE}}
- Command that did not pass: {{FAILED_RECORD}}
- Validation failures of this phase so far: {{FAILURE_COUNT}} of
  {{MAX_FAILURE_COUNT}}. At {{MAX_FAILURE_COUNT}} the run stops (`BLOCKED`)
  and a human decides.

{{FAILED_OUTPUT}}

The failure is final for that commit: the controller does not run the
command again on the same commit, so reporting it unchanged counts as one
more failure without running anything. Find the cause, from the output above
and by running the command yourself in this worktree, and commit a fix on top
of your earlier work. The controller runs each command on your commit's tree
alone, in a fresh export: uncommitted, untracked and ignored files, caches
and build outputs of this worktree do not exist there.
