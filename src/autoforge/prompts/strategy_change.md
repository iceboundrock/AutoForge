## The controller asks for another approach

The candidate you handed back last, {{CANDIDATE}}, has the same tree as the
failing candidate before it and failed the same validation definition the
same way. So the last launch made no objective progress: no definition that
failed passes now, the failure did not change, and the code did not change.
Another launch on the same approach would spend a failure and change
nothing.

Take a different approach this time. Re-read the failure above and the code
it exercises, question what the last attempt assumed, and commit a change
that differs from it. If you conclude that the task cannot be done as
specified, report `blocked` with the reason rather than hand back the same
candidate again.

This is the one change of approach the controller grants this task, and it
adds no launch: at failure {{MAX_FAILURE_COUNT}} the run still stops
(`BLOCKED`), and the controller keeps every candidate it judged for a human.
