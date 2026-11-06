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

What the controller can read and what counts as activity depend on the
profile's output format, `stream-json` by default or `text`. This prompt does
not tell you which, so rely on neither. Under `stream-json` output, a reply
written while a background agent runs cannot be read yet: the CLI holds it back
until no background agent is left, and any later turn's reply replaces it.
Under `text` output, the CLI prints nothing until it exits, so no reply can be
read before that and the CLI is never stopped on one.

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
your next turn opens when the task fires. What happens until then depends on
the task and on the output format:

- A background command, a `Monitor` and a scheduled wakeup are silent. The CLI
  writes nothing until one fires, and what a command prints goes to its own
  file, so none of it counts as activity, and one that outlasts an agent limit
  ends the run as a timeout.
- Under `stream-json` output, a background agent is not silent while it works.
  Its own messages reach the CLI's output and each counts as activity, so being
  between turns does not by itself run out the idle limit. A long tool call of
  the agent's writes nothing, so the idle limit can still end the run during
  one, and no output moves the limit on the run's total length.
- Under `text` output, a background agent is silent like the rest. The CLI
  writes nothing until it exits, even while the agent works, so none of its
  messages counts as activity. Such a run has no idle limit, only the limit on
  its total length, and an agent that outlasts that limit ends the run as a
  timeout.
