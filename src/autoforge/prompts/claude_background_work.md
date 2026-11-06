## Background work under Claude Code

Ending your turn does not end your invocation. While something you started
in the background is pending (a `Bash` command run in the background, a
`Monitor`, a background `Agent`, a scheduled wakeup), the CLI stays alive to
give you another turn when it fires, and the clean-up under "Working rules"
starts only after the CLI has exited. A final reply written with something
still pending ends in one of two ways. The controller stops the CLI shortly
after a reply it can read, and what was pending is lost, with no turn for you
to act on it. Or the run hangs until an agent limit ends it as a timeout, and
your reply is not used.

So before you write the reply that carries the `CONTROL_RESULT` block:

- Stop, with `TaskStop`, every background task, `Monitor` and background agent
  you started and have not seen finish. A loop or watcher that waited for
  something that has since ended still counts. Cancel any wakeup or scheduled
  job you set.
- Give each wait of yours a bound when you start it: a timeout on a command
  run in the background and on a `Monitor`, a deadline in any loop that waits
  for a condition. A condition that never comes true must not keep the run
  waiting.

Ending a turn without the block, to wait for a task, is not a final reply:
your next turn opens when the task fires. The CLI is silent until then and
nothing the task prints counts as activity, so a task that outlasts an agent
limit ends the run as a timeout.
