# End-to-end demonstration

A procedure for recording one real AutoForge run and publishing it so that
anyone can check it. The run goes from a GitHub issue to a pull request that
a separate reviewer has passed and that waits for a human to merge it. Every
claim in the write-up must be checkable against GitHub and the run's trace.
It is the evidence behind Gate 3 (end-to-end evidence) of the Public Alpha
([#255](https://github.com/iceboundrock/AutoForge/issues/255)), asked for by
[#261](https://github.com/iceboundrock/AutoForge/issues/261).

**No demonstration has been recorded yet.** [Recorded runs](#recorded-runs)
lists each one once it is published. Until then, nothing on this page is
evidence that AutoForge did anything.

The run is the maintainer's to make: it needs live agent logins, paid model
calls and a public repository. The [clean-room procedure](clean-room.md)
checks that the documents get a newcomer to a first run. This one publishes
a run as evidence. It installs AutoForge and redacts output the same way,
and adds what a public claim needs: a repository others can read, containment
while agents read public text, the run's trace, and a list of what humans
did.

## What the write-up may claim

The write-up claims only what a reader can check without the maintainer's
machine. Each claim needs a GitHub artifact and the trace
([Collect the trace](#collect-the-trace)):

| Claim | On GitHub | In the trace |
|---|---|---|
| AutoForge implemented the issue | the pull request from the branch `autoforge/<issue>`, and its first commit | the `ANALYZE_EXECUTE` line of the events excerpt; `pr` in the state excerpt |
| A separate agent reviewed it | each round's comment, headed `AI Code Review — Round <n>`, whose `Reviewed HEAD:` line names a commit of the pull request | a `REVIEW` line per round, with its profile, provider and model; a `reviews` entry per round with the same `reviewed_head_sha` and the comment's URL |
| A review finding was fixed automatically | a round's comment with findings and `Needs another fix round: YES`; a later commit on the pull request; a later round's comment that names that commit, with `None.` under Findings and `Needs another fix round: NO` | `REVIEW`, `FIX`, `REVIEW` in that order; a `needs_fix` round whose `finding_count` is above 0, then a `clean` round |
| The pull request waits for a human | the pull request open and unmerged when the run stopped; `safety.allow_merge: false` in the published configuration | `phase` is `READY_FOR_MERGE` |

**A fix iteration is claimed only when both columns show it.** A run whose
first review is clean is published as a clean run. Either the claim is
dropped, or a second, staged run that shows it is published too, with the
staging disclosed. A finding the run left to a follow-up issue rather than
a commit was not fixed, and the write-up says which findings went there.

**"Separate" is not "another vendor".** The reviewer is its own agent launch
under a review profile, and it did not write the change. Whether its model
comes from another vendor than the implementer's depends on the
configuration. The two-provider starter's review is cross-vendor and the
Claude-only starter's is not
([Getting started, step 4](getting-started.md#4-write-a-configuration)).
The write-up says which applies.

**GitHub shows one author.** The controller makes every write on GitHub with
the operator's token, and the implementer commits under the git identity of
the environment it runs in. So GitHub names the operator's account as the
author of the pull request, its review comments and, through that identity,
its commits. The trace ties them to the run: its timestamps and commits
match the pull request's timeline. Only the human-actions list says that no
human wrote anything there in between, and the write-up says so.

## Before the run

### The repository

The repository must be public and owned by the maintainer. It is made for
the demonstration rather than forked from someone's project, so no third
party receives its pull requests. It should be small enough to read in a
minute, with a test suite and a CI workflow that reports a check on each
pull request. It also needs an EPIC and the issue, as in
[Getting started, step 3](getting-started.md#3-choose-and-prepare-a-repository).
The maintainer's decision on #261 (2026-10-23) chose
[`iceboundrock/autoforge-demo`](https://github.com/iceboundrock/autoforge-demo).
Its `docs/RUNBOOK.md` is the operator's checklist for that repository, and
it was first written before Getting started existed. Where the two differ, follow
the documents of the [revision under test](#the-revision-under-test-and-the-configuration),
and say so in the write-up.

**Staging.** An issue may be written so that a first implementation is
likely to miss something, so that a fix iteration can happen. For example, it
can spell out edge cases the implementer must handle. The write-up says
what was staged, and links the issue as it was when the run started (GitHub
keeps its edit history).

### Containment

On a public repository, anyone who can comment on the issue or the pull
request can address the agents, and the agents run as the operator
([Prompt injection, and public repositories in particular](security.md#prompt-injection-and-public-repositories-in-particular)).
Before the run, put each of these in place and record it for the write-up:

- **Interaction limits.** Limit interactions to collaborators right before
  the run, for longer than the run can last. GitHub lifts the limit when it
  expires. Set it with your own login: it needs the repository's
  administration permission, which the run's token must not have.

  ```bash
  gh api --method PUT repos/<owner>/<repo>/interaction-limits \
    -f limit=collaborators_only -f expiry=one_week
  gh api repos/<owner>/<repo>/interaction-limits
  ```

  The second command reads the limit back. Keep its `limit` and
  `expires_at` for the write-up.
- **A dedicated environment.** Use a new OS user, VM or container that
  holds this run's credentials and no others (item 2 of
  [Recommended settings](security.md#recommended-settings-for-a-repository-you-care-about)).
  Run `doctor` there.
- **A GitHub token for this repository only.** Use a fine-grained personal
  access token restricted to the demo repository, with the permissions in
  the *Merge gate closed* column of
  [GitHub token permissions](security.md#github-token-permissions) and
  without Workflows. On a public repository it reads check runs without a
  Checks permission.
- **A git identity used for nothing else.** It makes the pull request's
  commits recognisably the run's, and its address is published, so use
  GitHub's `noreply` form. The write-up names it.
- **Model spend.** Before the first paid call:
  - for a provider that bills per call, set a spend limit on the provider's
    side wherever it offers one, and write down the planned budget;
  - for a subscription login, invent no dollar figure. The run's own bounds
    are its limit: `workflow.max_review_rounds`, `workflow.max_total_steps`,
    `execution.max_correction_attempts`, and each profile's
    `idle_timeout_seconds` and `max_runtime_seconds`
    ([Loop bounds and replanning](usage.md#loop-bounds-and-replanning)).
    Write them down.
- **`sandbox.mode`.** Choose `disabled` with the containment above, or
  `required` on a host that can enforce it
  ([The outer sandbox is opt-in and Linux-only](security.md#the-outer-sandbox-is-opt-in-and-linux-only)).
  The write-up says which, and never calls a `disabled` run sandboxed.

### The revision under test, and the configuration

Install AutoForge from one revision, as
[The revision under test](clean-room.md#the-revision-under-test) describes:
a commit's 40 hexadecimal digits, or a release candidate's exact version.
Keep the `uv tool list --show-version-specifiers --show-python` line that
shows which ([clean-room step 2](clean-room.md#the-steps)).

Publish the configuration: commit it to the demo repository, or attach it
to the write-up. It keeps the merge gate closed
(`safety.allow_merge: false`). It also writes out every profile with its
provider, model and effort, as the starters do, so a reader need not know
the built-in routing.

Then run `doctor` and record the versions as
[Getting started, step 5](getting-started.md#5-check-the-environment-doctor)
says: the output of `doctor`, saved as `trace/doctor.txt`, and
`trace/versions.txt`. Add the
OS and architecture (`cat /etc/os-release`, `uname -srm`). The `uv tool list`
line names the Python.

## The run

In the dedicated environment, from a clone of the demo repository, follow
[Getting started, step 6](getting-started.md#6-preview-run-and-read-where-it-stopped):

```bash
autoforge --config autoforge.yaml run --epic "$epic" --issue "$issue" --dry-run
autoforge --config autoforge.yaml run --epic "$epic" --issue "$issue"
autoforge --config autoforge.yaml status
```

Run every command of the run, and those of
[Collect the trace](#collect-the-trace), from the clone's top directory. A
command finds the run in its state directory, which is `.autoforge/` in the
directory it runs from unless the configuration's `state_dir` or a
`--state-dir` names another. A relative one is relative to that directory
too. If you gave `run` a `--state-dir`, give it to every later command.

Keep the run's terminal output, from the command to the `READY_FOR_MERGE`
banner or the `BLOCKED` line, as `terminal.txt` in the run's trace
directory: `trace/run-1/` for the first run
([Collect the trace](#collect-the-trace)). Then:

- **Leave GitHub and the checkout alone while the run is active.** Do not
  comment on the issue or the pull request, push to `autoforge/<issue>` or
  edit the issue. Anything you do there becomes a human action the
  write-up must explain.
- **Publish a stop as it happened.** Leave a `BLOCKED` run as
  [Troubleshooting](troubleshooting.md#blocked-the-controller-will-not-decide-alone)
  says, and list each `unblock`, `resume` and change you made as a human
  action.
- **Collect a run before you start over.** A new `run` replaces the one in
  the state directory: `run --force` does at any phase, and a plain `run`
  does without asking once the old one is `BLOCKED`, `FAILED` or `DONE`
  (one that holds operator actions needs `--force`). It replaces
  `state.json`, and with it the old run's review history and counts, so
  `status` shows only the new run from then on
  ([Starting over](troubleshooting.md#starting-over)). Before you start
  over:
  1. [Collect the trace](#collect-the-trace) of the run into
     `trace/run-1/`. It keeps the run's ID, its whole `status.json` and its
     two excerpts there, beside its `terminal.txt`.
  2. Keep its log directory, `<state dir>/logs/<run-id>/`. The new run
     leaves it in place, and it is the only record of that run's launches.
     Do not publish it.

  Then collect the new run into `trace/run-2/`. The new run does not adopt
  the old run's open pull request. Close that pull request first for a
  fresh implementation, or take it over
  ([Taking over a PR another run owns](usage.md#taking-over-a-pr-another-run-owns));
  otherwise the new run blocks (`pr-owner`). Starting over, and what became
  of the old pull request, are human actions. The write-up reports each run
  apart, with its own excerpts, timings and outcome.
- **Collect the trace before you merge.** The merge, if you make one, is a
  human action.

## Collect the trace

Collect each run into a trace directory of its own, from the directory the
run was started in ([The run](#the-run)). Set `state` to the run's state
directory: `.autoforge` as below, or what the configuration's `state_dir`
or the run's `--state-dir` named. Set `out` to the run's trace directory:
`trace/run-1`, then `trace/run-2` for a run that replaced it.

```bash
state=.autoforge
out=trace/run-1
mkdir -p "$out"
```

The commands below need `jq`. They pass `state` to `status` as
`--state-dir` and read the run log from the same directory, so both
excerpts come from the same run whatever the configuration says.

Never publish `state.json`, the output of `status --json` (`status.json`
below), `events.jsonl` or a launch's log directory whole. Together they
hold local paths, every prompt and every agent's output
([Find the run and read its state](troubleshooting.md#find-the-run-and-read-its-state)).
The two excerpts keep only the fields a claim needs. The rest of the trace
directory stays with you.

**The state at the end:**

```bash
autoforge --config autoforge.yaml --state-dir "$state" status --json > "$out/status.json"
jq '{run_id, repository, epic_url, issue: .current_issue_url, pr: .current_pr_url,
     phase, review_round, step_count, created_at, updated_at,
     reviews: [.review_history[] | {round, reviewed_head_sha, result,
               finding_count, review_comment_url, timestamp}],
     unblocks: .unblock_history}' "$out/status.json" > "$out/status-excerpt.json"
```

The span from `created_at` to `updated_at` is the run's wall-clock time,
from its creation to its last save. `reviews` has one entry per completed
review round of the pull request.

**The phase sequence and timings.** The run log has one line per agent
launch, per command the controller ran itself and per operator action:

```bash
run=$(jq -r .run_id "$out/status.json")
jq -c 'def t: if (. // "") == "" then null
              else sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601 end;
  {seq, phase, attempt, correction, review_round, profile, provider, model, effort,
   started_at,
   seconds: ([.started_at, .finished_at] | map(t)
             | if any(. == null) then null else .[1] - .[0] end),
   exit_code, timed_out, ended_by,
   cost_micro_usd: .provider_summary.cost_micro_usd}' \
  "$state/logs/$run/events.jsonl" > "$out/events-excerpt.jsonl"
```

How to read a line:

- **`phase`.** An agent's phase (`ANALYZE_EXECUTE`, `REVIEW`, `FIX`), a
  command the controller ran (a validation command is
  `<phase>-candidate-validation`), or a record of its own:
  `INITIALIZING-epic-binding` when the run starts, `BLOCKED-unblock` for an
  `unblock`, and `<phase>-config-change` for an accepted configuration
  change.
- **`review_round`.** The count of completed review rounds when the launch
  began, so a `REVIEW` line shows the round before its own. A review's own
  round is in its comment's heading and in `reviews`.
- **`attempt` and `correction`.** A retried launch has a higher `attempt`,
  and a correction prompt after a malformed result has `correction: true`.
- **`seconds`.** The launch's own duration. The time between launches is
  the controller's (its GitHub reads and writes, its pushes), or a human's
  while the run was stopped.
- **`cost_micro_usd`.** The cost Claude Code reported for its launch, in
  millionths of a US dollar. Under a subscription login it is not what you
  were billed. OpenCode's launches report no cost, so read theirs from the
  vendor's usage page
  ([Provider authentication and billing](compatibility.md#provider-authentication-and-billing)).
- **A `resume` leaves no line.** Note each one yourself.

The excerpts leave out, on purpose: `cwd` and `log_dir` (local paths),
`command`, `metadata`, `error`, and `parsed_result`, each agent's whole
result. What an agent's result published is on GitHub.

### Redact, then read every excerpt yourself

The excerpts hold no prompt and no agent output. They can still hold the
text of an `unblock --reason`, and the terminal and `doctor` output hold
more. Apply the clean-room list
([What to capture](clean-room.md#what-to-capture), "Redact before you post")
to everything you publish. A search helps, but it does not replace reading:

```bash
grep -nE "$HOME|ghp_|gho_|github_pat_|sk-" \
  "$out/status-excerpt.json" "$out/events-excerpt.jsonl" "$out/terminal.txt" \
  trace/doctor.txt trace/versions.txt
```

Search the same files for your user name and e-mail address too, and do
the same for each run you collected.

## The write-up

Publish it in the demo repository, as its README or a page the README
links, or as a section of this page. It holds:

1. **Links:** the repository and its start commit (the default branch's
   head when the run started), the EPIC, the issue, the pull request, each
   round's review comment, each fix commit, any follow-up issue the run
   opened, and the AutoForge revision (a commit or a tag).
2. **Configuration and versions:** the configuration file; the provider,
   model and effort of each phase; `sandbox.mode`; the closed merge gate.
   The versions of AutoForge (the `uv tool list` line), Python, `uv`, git,
   `gh`, Claude Code and OpenCode, as `doctor` and `versions.txt` report
   them, and the OS and architecture.
3. **Staging:** what of the issue was written to draw a finding, or "none".
4. **Containment:** the interaction limit as read back, the environment,
   the token's scope and permissions, the git identity, the spend limits or
   the subscription and the run's bounds, and `sandbox.mode`.
5. **Human actions,** listed apart from AutoForge's (below).
6. **Phases and timings:** a table built from the events excerpt (phase,
   round, profile and model, start, duration, outcome). Add the total
   wall-clock time, the number of review rounds, and every failure, retry,
   correction or recovery as it happened. A run you replaced gets its own
   table, from its own trace directory, with its outcome.
7. **The fix iteration:** claimed with its evidence from both columns of
   [the claims table](#what-the-write-up-may-claim), or "the review was
   clean in round 1".
8. **Cost and limits:** for a provider billed per call, the spend and tokens
   its console reports, against the planned budget. For a subscription, the
   number of agent launches, their durations and any usage limit hit,
   reported apart from API spend.
9. **The excerpts,** redacted, with a line saying they were reviewed for
   credentials and personal data before publication.
10. **How to reproduce it:** [the procedure below](#reproduce-it), with the
    demonstration's own values filled in.

**Human actions.** List them in one table, in order: what a human did, when
and why. The list includes at least:

- creating the repository and its settings, the EPIC and the issue;
- setting the interaction limit;
- installing and configuring AutoForge, running `doctor` and the dry run;
- starting the run, and each `resume` and `unblock` with its reason;
- starting over, and what became of the old run's pull request;
- any change made on GitHub while the run was active;
- collecting and redacting the trace;
- the merge, if there was one.

Everything else on the pull request (its commits, its description, the
review comments) the controller published. A human edit to any of it is
listed here, and is never presented as AutoForge's output.

## Link it from #261

The write-up is not the last step. #261 asks for one more deliverable: a
comment on [#261](https://github.com/iceboundrock/AutoForge/issues/261)
that links everything a reader needs to check the run. Once the write-up
and everything it links are public, post that comment with:

- the demo repository, its EPIC and the issue;
- the pull request;
- the published configuration;
- the write-up;
- the AutoForge commit the run installed, all 40 hexadecimal digits, and
  for a release candidate its version too.

If you published more than one run (a run you replaced, or a staged second
run), link each with its own pull request and outcome. Then add each run
to [Recorded runs](#recorded-runs) in a pull request to AutoForge.

## Reproduce it

Someone else can run the same demonstration on a fork, with their own
credentials. Expect different code, findings and timings. What should
reproduce is the sequence: `ANALYZE_EXECUTE`, then `REVIEW` rounds with a
`FIX` after each round that has findings, and a stop at `READY_FOR_MERGE`
with the pull request unmerged, or a `BLOCKED` stop that names its reason.
Whether a round has findings depends on the models.

1. Read [Security boundaries](security.md) and [Containment](#containment):
   the fork is public, and the same risks apply.
2. Fork the demo repository and clone the fork. Make the write-up's start
   commit the fork's default branch, so that the fork does not already hold
   the demonstrated change:

   ```bash
   gh repo fork <owner>/<demo-repo> --clone=false
   git clone https://github.com/<you>/<demo-repo>.git
   cd <demo-repo>
   git push origin <start-commit>:refs/heads/demo-start
   gh repo edit <you>/<demo-repo> --default-branch demo-start --enable-issues
   ```

   A fork has its issues turned off until the last command turns them on.
   Its Actions also stay off until you enable them on its **Actions** tab,
   which its CI needs to run on the pull request.
3. Copy the EPIC and the issue the write-up links into the fork. Take each
   body with
   `gh issue view <number> -R <owner>/<demo-repo> --json body -q .body`,
   and create each issue as
   [Getting started, step 3](getting-started.md#3-choose-and-prepare-a-repository)
   does. Make the issue a sub-issue of the EPIC, or pass `--epic`.
4. Install AutoForge at the write-up's revision and take its published
   configuration. Change only a model your account cannot use, and note the
   change. Then sign in, run `doctor`, the dry run and the run:
   [Getting started](getting-started.md), steps 2 and 4 to 6.
5. [Collect the trace](#collect-the-trace), and compare your phase sequence
   and `reviews` with the write-up's.

## At a release candidate

Gate 3 asks for the demonstration at the release candidate. Either run it
again with the candidate's exact version and add the run to
[Recorded runs](#recorded-runs), or record on #255 why no change since the
demonstrated revision affects it.
`git log --oneline <demonstrated commit>..v<version> -- src/` lists the
changes to weigh.

## Recorded runs

None yet.

Each published run is added here with:

- the date (UTC);
- the AutoForge revision;
- the pull request;
- the result: `READY_FOR_MERGE` after so many rounds, with or without a fix
  iteration, or `BLOCKED` and why;
- a link to its write-up.
