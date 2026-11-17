## Your repository is a seeded copy

The repository in your workspace is a private copy the controller seeded
for this launch. It has no remote, so nothing more can be fetched into it.
{{HISTORY_DEPTH}} {{HISTORY_TAGS}} The operator sets how deep it goes
with `sandbox.container.history_depth` (a number of commits, or `full`)
and whether tags come with it with `sandbox.container.history_tags`. If
your task needs history this copy does not hold, say so in your result
rather than guessing what it would show.

What staging supports of the tree:

- a submodule (a gitlink) is an empty directory: its commit is never
  fetched;
- a Git LFS file is its pointer file, byte for byte, never its content;
- a sparse checkout is not applied: the whole tree is there.

Whatever you leave uncommitted when the launch ends is discarded with the
workspace.
