## The controller's validation of what you review

Before launching you, the controller itself ran the configured validation
commands (`{{VALIDATION_KEY}}`) against {{VALIDATION_CANDIDATE}}, and
recorded what it observed. The list below is the controller's own record,
one line per command in the order they ran: its index and command line, how
it ended, how long it ran, and whether the outer sandbox enforced it. It is
not a claim by an agent, nothing you report changes it, and your result has
no field for it.

{{VALIDATION_RECORDS}}

{{VALIDATION_OUTPUT}}

How this bears on your review:

- `PASS` means the command exited 0 on exactly this candidate's tree, in a
  launch the controller ran. It is not a review: judge the change on its
  merits, as the steps below say.
- `FAIL` (a non-zero exit) or `TIMEOUT` (killed at its time limit) means the
  candidate did not pass the controller's validation, and the commands
  after it did not run. Judge what the failure means for the change. The
  controller refuses a clean result (`"needs_fix_round": false`) while any
  command above did not pass: report an actionable finding that names each
  failing command by its index (`[0]`, `[1]`, ...) and says what would
  resolve it.
