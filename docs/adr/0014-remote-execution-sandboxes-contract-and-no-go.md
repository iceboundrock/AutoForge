# ADR 0014. Remote execution sandboxes: the contract a provider must meet, and a no-go for now

- **Status:** proposed. The decision proposed here is a **no-go** for a
  remote production adapter (#277). It is not in effect until the
  maintainer signs it off on #276, which that issue's fifth acceptance
  criterion asks for. This line does not record a sign-off. Written at
  `main` `3311238` (2026-11-15), where every reference below was read.
  - *Implemented with it:* nothing in the package. No configuration key,
    state field, protocol number, launch class, prompt or default
    changes, and no provider is declared supported. The prototype of §6
    is test code: `tests/remote_sandbox_prototype.py`, exercised by
    `tests/test_remote_sandbox_prototype.py`. Four tests in that file
    keep this record honest. They check that every test it names exists
    (`test_adr_0014_names_only_tests_that_exist`), that it claims no
    state protocol number and its links resolve
    (`test_adr_0014_claims_no_state_protocol_number_and_its_links_resolve`),
    that ADR 0013's capability matrix still refuses a remote endpoint
    (`test_adr_0014s_no_go_and_adr_0013s_refusal_of_a_remote_endpoint_agree`),
    and that the package never imports the prototype
    (`test_the_package_never_imports_the_prototype`).
  - *Recorded here:* the provider comparison (§3), the contract any
    remote backend must meet (§4, R1 to R10), the decision and its
    reasons (§2), what reopens it (§5), and what the prototype shows and
    does not show (§6).
- **Decides:** #276 (ENV-C2), for EPIC #266.
- **Builds on:** [ADR 0013](0013-container-sandbox-backend-lifecycle-trust-boundaries-and-rollout.md).
  Its copy-in form (D12), artifact (D5), journal and reconciliation (D6)
  and failure taxonomy (D17) are what a remote backend would reuse. Its
  capability matrix (D16) refuses a runtime endpoint on another machine.
  Its §4 ends its column on remote execution sandboxes with "a new
  trusted party needs its own record". This is that record.
- **Amends:** nothing. A go would amend ADR 0002 §3 and ADR 0013 D16 at
  least (§7).
- **Related:** ADRs 0002, 0004, 0005, 0006 and 0008 as #267 amended them
  (§7), and ADR 0007 (helper processes). The issues: #23 (protocol
  numbering), #255 (the Public Alpha, which does not depend on this),
  #257 (the security audit's residuals R3 and R4), #271, #272, #273,
  #274 and #292 (the container backend's unbuilt children), #277 (the
  remote adapter) and #294 (egress mediation).

## 1. Context

### 1.1 The question

A *remote execution sandbox* runs an agent launch on another machine that
a service provides. In ADR 0013's terms (§1.5) it is a container runtime
on another machine. It has nothing to do with AutoForge's REMOTE mode.
#276 asks two things. Can such a sandbox be an execution-only backend
under AutoForge's authority model? If it can, should a production adapter
(#277) be built? It names two kinds of provider: Vercel Sandbox, a hosted
microVM service, and generic VM services. It asks for a reasoned
comparison, a minimal prototype of the transfer and the shutdown, and a
signed-off go or no-go.

Three constraints come from the owner, in #276's planning reconciliation
of 2026-10-23:

- **Pack transfer only.** A remote sandbox never gets a bind mount. The
  workspace seed pack is copied in, and a bounded pack with a manifest
  comes back, strictly imported by the controller. rsync, or a
  provider's file-sync API, may only be a transport optimisation beneath
  the git-object integrity check (#267's decision 12, ADR 0013 D12).
- **"Streaming/heartbeat" means the liveness of a remote resource, as one
  local controller process observes it.** It is never a protocol between
  AutoForge processes, a lease service or any distributed machinery
  (AGENTS.md, "Runtime model").
- **On a no-go, #277 is marked not planned** in the same action that completes
  #276, because #277 is natively blocked by #276 and #274.

### 1.2 What is in place at `3311238`

- **Built:** the container backend's lifecycle seam (#268), the launch's
  git input and the strict import of an exported artifact (#269), the
  container security policy (#270), and each provider's credential
  mapping (#290). The import is `candidate_artifact.import_artifact`. It
  already treats a pack and a manifest written by someone else as
  untrusted data. It checks them against the launch's git input and
  publishes only under L1.
- **Not built:** the image and runtime detection (#271), the lifecycle
  journal and reconciliation (#272), backend selection, the pin and the
  probe (#273), the seed pack builder (#292), and the MVP's end-to-end
  proof (#274). The container backend cannot be selected. ADR 0013 D16
  refuses a `DOCKER_HOST`, `CONTAINER_HOST` or Docker context that names
  a TCP or SSH endpoint, "(#276, #277)".

### 1.3 How the facts were gathered

No provider was measured. This environment has no Vercel account and no
cloud account, and holds no credential for either. Every provider fact
below comes from the provider's own documentation, read on 2026-11-15:

- Vercel Sandbox: the overview, pricing and quotas, the security model,
  the firewall, tags, persistence, authentication, the duration and
  persistence guide, and the Python SDK reference
  (`https://vercel.com/docs/sandbox` and the pages below it);
- Google Compute Engine: "Limit the run time of a VM";
- Amazon EC2: the `RunInstances` API reference.

Statements about SSH and OpenSSH are design reasoning, not provider facts.
Where a fact matters to a guarantee, §5 lists it as something a probe must
measure before any go.

## 2. The decision

**D1. No-go for a remote production adapter now.** #277 is not built.
Neither Vercel Sandbox nor any VM service is declared supported, and ADR
0013 D16 keeps refusing a runtime endpoint on another machine. Four
reasons, each enough on its own:

- **B1. The foundations do not exist.** A remote backend would reuse ADR
  0013's journal and reconciliation (#272), its image and helpers (#271),
  its probe and pin (#273), and the MVP's proof that they work (#274).
  None is built. A remote adapter now would either build them first, for
  its own case, or fork them.
- **B2. Three decisions are the owner's, and none has been taken.**
  - *Data leaving the machine.* A remote launch sends the repository's
    tree at the bound commit, the PR's history above the merge base, the
    issue and PR text of the read context, and the prompt to a third
    party. The agent's output is produced there. Under Vercel's
    credential brokering, the model traffic is also decrypted there (§3).
  - *A second controller credential.* The controller would hold a
    provider credential beside `gh`. At Vercel that is an access token
    "with access to the team", or a project's OIDC token, which expires
    after 12 hours and is renewed through the operator's Vercel CLI
    login.
  - *Review verdicts produced on a third party's machine.* The provider
    can alter what runs inside, so a reviewer's verdict would become a
    claim the provider could forge (§3.3).
- **B3. What a guarantee rests on is unmeasured.** That covers the
  deadline's enforcement and its delay, whether a lookup is passive,
  destroy and its read-back, a partial transfer's behaviour, credential
  brokering with each agent CLI, and the absence of any credential seen
  from inside. §5 lists each.
- **B4. Validation evidence has no design.** Export commands must stay on
  a local backend (R10), so a run would need two backends. The sandbox pin
  (ADR 0013 D8) records one.

**What is not a reason:**

- **The integrity of the transfer.** The prototype carries a seed pack out
  and a candidate pack back, through a fake provider, into the package's
  unchanged strict import. Every interruption, expiry and substituted
  file tried fails closed (§6). Pack transfer works the same whatever the
  provider is.
- **Cost.** At Vercel's documented Pro rates, an hour-long launch at the
  default size costs about $0.11 in compute (§3.2).

**D2. The contract in §4 binds any later proposal.** A record that
reopens D1 meets R1 to R10, or amends them with a stated reason. It does
not redo this analysis.

**D3. The transfer is pack-only, both ways (R4).** This is the owner's
decision 12, applied to remote providers. Each provider's own
conveniences are rejected as the content boundary: its git source, its
file sync or directory copy, its snapshots, drives and persistent
sandboxes.

**D4. Remote launches would be agent launches only (R10).** Export
commands (`validation.commands`, `merge.verification_commands`) stay on
the host's enforced backend.

**Consequences of D1 once signed off.**

- #277 is marked not planned, in the same action that completes #276,
  as #276's planning text says. That action is the maintainer's. This
  change performs no GitHub action.
- `docs/compatibility.md` says that remote execution sandboxes are not
  supported, with a pointer here.
- Nothing is persisted and no protocol number is needed, so #23 has
  nothing to coordinate. A go would add a remote entry kind to #272's
  lifecycle journal (a file of its own, not `state.json`), through #23.

## 3. The providers compared

### 3.1 Capabilities

"Docs" marks a fact read in the provider's documentation (§1.3), and
"design" a consequence drawn here. Nothing in the table was measured.

| | Vercel Sandbox | A generic VM service (GCE, EC2 as examples) |
|---|---|---|
| Isolation | a Firecracker microVM with its own kernel; commands may use `sudo` (docs) | a VM; the operator's image decides who is root (design) |
| Who owns the resource | Vercel; the controller holds a handle to it | the cloud provider; the same |
| Identity at create | a caller-chosen `name`, unique per project and immutable; `get_sandbox(name)` is a passive lookup (docs) | EC2: `ClientToken` makes `RunInstances` idempotent, at most 64 ASCII characters (docs); GCE: an instance name, unique where it is created (design) |
| Labels | at most five tags; keys up to 128 characters, values up to 256; a listing filters by one tag (docs) | EC2 `TagSpecifications` (docs); GCE labels (not read here) |
| Deadline the provider enforces | the session timeout: 5 minutes by default, at most 45 minutes on Hobby and 24 hours on Pro and Enterprise; Vercel stops the session when it is reached (docs) | GCE: `maxRunDuration` or `terminationTime`, with `instanceTerminationAction` `STOP` or `DELETE`, from 30 seconds to 120 days, possibly up to 30 seconds late (docs). EC2's `RunInstances` has no run-duration parameter. `InstanceInitiatedShutdownBehavior=terminate` acts only on a shutdown started inside (docs), which root inside can cancel (design) |
| Extending it | `extendTimeout` / `extend_execution_time_limit`, adding to the current limit up to the plan's maximum (docs) | GCE: not read here; EC2: nothing to extend |
| What stop and delete leave | persistence is on by default: a stop snapshots the filesystem, kept 30 days after last use. `persistent: false` discards it. `destroy()` deletes the sandbox and its sessions, and its snapshots only with `delete_orphan_snapshots`. Non-persistent sandboxes are removed after 14 days of inactivity (docs) | the disk follows the instance's delete settings (design) |
| Automatic resume | most SDK calls, file and command methods and `extend_execution_time_limit` among them, resume a stopped persistent sandbox. `stop()`, `destroy()` and `update()` do not (docs) | none (design) |
| File transfer | streaming reads and writes (`fs.open` in `rb` and `wb`). An upload's declared `size` raises a mismatch error. The pages read give no file size limit; the disk is 64 GB (docs) | SSH, or the provider's own copy tool (design) |
| A liveness channel | a command's stdin pipe, written by request: `send()` returns once the command accepts the bytes (docs). A controller that dies therefore sends no end of file (design: not measured) | an SSH session's stdin. It ends when the controller's host closes the connection, or after the server's keepalive limit when that host vanishes (design) |
| Egress control | `allow-all` (the default), `deny-all`, or a user-defined allow-list of domains, matched by SNI, or of CIDR ranges. Updates apply live and are not atomic. The documented caveat is domain fronting (docs) | the provider's firewall rules, by IP address (design) |
| Delivering a model credential | credential brokering: the firewall injects headers into requests to a domain, and "the secrets never enter the sandbox". It terminates TLS for that domain with a per-sandbox CA (docs) | ADR 0013 D14's environment variable (design) |
| The controller's credential | a project's OIDC token (a development token expires after 12 hours; running `vercel env pull` again fetches a new one), or an access token with access to the team, with team and project ids (docs) | the cloud account's credentials, which IAM can scope (design) |
| Credentials inside | Vercel-managed credentials only when the create asks for them (`credentials=`); a per-sandbox proxy CA, which is no credential, only where a rule terminates TLS. The pages read say nothing of anything else (docs) | the metadata service, which hands out an instance role's credentials. EC2 can disable the endpoint (`HttpEndpoint=disabled`), and the instance profile is optional (docs) |
| Regions | 19, `iad1` by default (docs) | the provider's |

### 3.2 Cost

Vercel's documented rates for Pro in `iad1` are $0.128 per Active CPU
hour, $0.0212 per GB-hour of provisioned memory, $0.60 per million
creations, and $0.08 per GB-month of snapshot storage. Data the sandbox
sends out is billed; data it downloads is free. Hobby includes 5 CPU hours,
420 GB-hours and 5,000 creations a month, allows 10 concurrent sandboxes,
and pauses creation past its allowance. An agent launch mostly waits on its model, so Active CPU
stays low. At the default 2 vCPUs (4 GB) and 10% utilisation, an
hour-long launch costs 0.2 × $0.128 + 4 × $0.0212 ≈ $0.11. That figure
leaves out the model's usage, which is the operator's in either case.
Whether downloading an artifact through the file API counts as data the
sandbox sends was not stated on the pages read.

For VM services no rate was read. Their cost has the same shape:
instance time and disk, with a minimum billing granularity.

What bounds cost is the deadline. A resource that the controller stops
renewing ends at its last deadline (R3). After a controller death, then, a
survivor costs at most one renewal window of compute, plus the model
tokens it spends in that window.

### 3.3 Trust

A remote provider is a new trusted party. Whatever runs inside, it can
read and alter: the seed, the read context, the prompt, the agent's work,
and the model traffic wherever brokering decrypts it. It is trusted, as the
agent is, for the confidentiality of what it is given and for the integrity
of the agent's execution. It is never trusted for the controller's state,
for GitHub authority, or for the candidate. The artifact comes back as a
claim, and the host imports it strictly (ADR 0013 D5).

- **Vercel** sees the repository content and, through brokering, the
  model API traffic in clear. In return the model key never enters the
  sandbox. On the documentation's word, that removes the audit's residual
  R3 (#257) there: a tool inside can still use the key through the
  brokered domain, but cannot read it. The firewall can restrict egress by
  domain, which narrows R4 to the allowed domains, minus SNI's
  domain-fronting caveat. Snapshots are encrypted at
  rest, and the infrastructure is SOC 2 Type II (docs). Data residency is
  "consult your plan".
- **A VM service** sees the disk and memory of a machine the operator
  configures. Model traffic stays end-to-end TLS from the agent. The
  model key sits in the VM's environment, so R3 is as under ADR 0013.
  Egress control is by IP, so R4 is as under ADR 0013 D9. The metadata
  service is one more credential source to close.
- **Both:** a provider that can falsify execution can falsify a reviewer's
  verdict and a validator's output. The first is B2's third decision. The
  second is why R10 keeps export commands local: ADR 0006's evidence says
  "the validator saw C", and ADR 0013 D2 lets that hold in a container
  only because the image and the local runtime are already trusted.

### 3.4 Portability

Vercel Sandbox is one vendor's API, offered through JavaScript and Python
SDKs and a `sandbox` CLI. AutoForge imports no SDK into the package
(ADR 0007 D2.1, D2.2). An adapter would therefore drive the CLI under the
executor, or a helper process as ADR 0007's helpers are driven. Its
strongest features, brokering and the domain firewall, do not carry over
to another provider.

A VM reached over SSH keeps the remote side portable. Only the create,
delete and deadline calls differ between providers. In return the
operator configures much more: images, networking, IAM, the metadata
service and host keys. The deadline is not uniform either: GCE enforces
one, and EC2's `RunInstances` does not.

On paper, Vercel meets more of §4 with documented API calls (R1, R2's
brokering, R3's renewable deadline, R6, R7). A VM over SSH gives the
liveness signal closest to ADR 0013's death-watch (R3), and portability.
This ranks nothing for support. It records where a reopening would start.

## 4. The remote contract

Any remote backend meets these, in addition to ADR 0013's decisions as
they apply to a copy-in runtime. The prototype (§6) implements the parts
marked *prototyped* against a fake provider.

**R1. Recoverable identity** (*prototyped*).

- **The entry comes first.** Before the controller asks a provider for
  anything, it journals the resource. The entry holds the launch's
  identity (run key, issue, phase, launch id), a nonce, the provider, and
  the *scope* the controller's credential reaches (account, project,
  region).
- **The name and tags follow from the entry alone.** The resource's name
  is a function of the entry: DNS-label safe and at most 63 characters.
  Its tags name the run, the issue, the launch, the kind and the nonce.
  That is five, Vercel's limit.
- **An ambiguous create is resolved by name before any re-issue.** A
  create whose answer was lost is followed by a lookup by name (ADR 0004
  D4.3's rule, for a remote object). The name is unique in its scope, so a
  re-issue can never make a second resource.
- **Ownership is proven, never guessed.** A resource is the run's only
  when its name, every tag and the nonce match an entry. A resource that
  carries the run's tag and matches no entry blocks the run, named, and
  is never removed.
- **The scope is read fresh.** It is read before every pass and again
  after it, as ADR 0013 D6 item 10 reads a store. A lookup in another
  scope finds nothing, and that is never read as "gone".
- **Provider requirement:** a caller-chosen unique name, or an idempotency
  token, plus labels a listing can filter on. Vercel meets it with `name`
  and tags, EC2 with `ClientToken` and tags.

**R2. Credentials** (*prototyped* for what a create may carry).

- **The provider credential stays with the controller.** It never enters
  the remote, by environment, file or brokering.
- **The remote gets no GitHub or git credential route.** The names
  `child_env.forbidden_env_entry` refuses for every agent are refused
  here too.
- **The model credential leaves the machine.** It goes either into the
  provider's brokering (Vercel) or into the remote's environment as ADR
  0013 D14's one variable (a VM). Which of the two is acceptable is B2's
  first decision.
- **No rotating or stored login is projected,** as D14 already says.
- **A VM gets no instance role or service account,** and its metadata
  endpoint is disabled where the provider allows it.
- **Nothing credential-shaped is found inside.** A from-inside
  measurement, as `container_measure.py` makes for containers, proves no
  credential in the environment, the files or the metadata service. A host
  where one is found is refused.

**R3. Liveness and the deadline** (*prototyped*).

- **The provider enforces a deadline.** Every resource is created with one
  at most a *renewal window* ahead, and never past the launch's ceiling:
  its `max_runtime_seconds` plus ADR 0002's graces, as ADR 0013 D6 item
  4's in-container deadline is set, plus the staging and export bounds.
- **The controller renews it.** From the create to the removal, the
  backend asks the provider for one more window whenever less than half
  a window remains. It reads the deadline back, and every transfer
  respects the deadline read. The controller holds the renewal as it
  holds the container backend's liveness stream. No process outlives the
  controller to do it.
- **The bound is read back, never assumed.** A deadline read back later
  than the one asked for, after the create or after a renewal, ends the
  launch: a dead controller's resource would outlive its window. The
  value read is never clipped locally, because that would not shorten
  the resource's life. At the create this is a setup failure, before a
  byte is sent. At a renewal it ends the run step, and the launch's
  result is not used. Either way the entry stays open until a removal is
  verified (R6). An earlier deadline is the provider's to choose, and the
  transfers respect it.
- **Every time is finite.** A deadline read back as NaN or an infinity
  ends the launch the same way. Such a value compares false with every
  bound, so a check that only compares would pass it, and a resource held
  until it would never end. The ceiling and the renewal window, which the
  entry records, are refused when they are not finite, as the entry is
  made and as it is read. So is a deadline a renewal or a transfer is
  given.
- **A controller that dies stops renewing,** so the provider ends the
  resource within one window. That bound replaces ADR 0013 D6's
  death-watch for a provider that offers no channel whose end the remote
  sees.
- **Where such a channel exists, it can end the resource sooner.** An SSH
  session is one. It ends the resource sooner and does not replace the
  deadline.
- **A deadline only inside the guest is not enough.** EC2's
  shutdown-initiated termination is an example: an agent with root can
  cancel it.
- **Not a heartbeat protocol.** This is the controller's request to the
  provider. Nothing on the remote side decides anything, and no AutoForge
  process talks to another.

**R4. Transfer: pack-only, both ways** (*prototyped*).

- **In: the seed pack and its manifest** (#292), and a copy of the read
  context, as ADR 0013 D12's copy-in form sends them.
  - **The pack is bound through the manifest.** The input binds the
    manifest's bytes (D4), and the manifest names the pack by its sha256.
    ADR 0013's manifest names commits, boundaries and tags, not the pack's
    bytes: there the host checks the pack itself, and the local runtime
    carries it. Here the pack crosses a provider's API and storage, so the
    remote side must be able to tell that the pack it holds is the one the
    host checked. A digest in the manifest is the simplest way, and the
    prototype's stand-in has one. Rerunning D4's pack checks on the
    remote side is the alternative a reopening may choose instead.
  - Before a byte leaves, the manifest read is the one the launch's git
    input binds (D4's hand-off), it names a pack, and each file is within
    its bound. The bytes checked are the bytes sent.
  - Each file goes with its size declared, read once from its descriptor
    and hashed as it is sent. The pack sent must be the one the manifest
    names, and each file must match the remote's digest of what it
    stored.
  - The stager checks the same binding on the remote side before it
    indexes anything: the manifest against the input's digest, which
    comes with its command and never from a file of the remote, and the
    pack against the manifest.
  - An upload that fails any of these is a setup failure, and no stager
    runs on it.
  - The prover's proof of the tree inside stays the only tree proof (D2).
- **Out: the artifact,** a manifest and a pack.
  - It is streamed into the launch's empty export directory: the
    manifest first, within its bound, then the pack, within the export
    bound and the size the manifest claims, hashed as it is read.
  - A download that fails is a candidate refusal, and everything it wrote
    is deleted.
  - The strict import then checks it all again. The transfer is never
    the content boundary.
- **Never the content boundary:**
  - the provider's git source (Vercel's `GitSource`), which would need a
    credential for a private repository and would let the remote choose
    what it fetched;
  - its file sync or directory copy;
  - rsync;
  - snapshots, drives, persistent sandboxes or mounted remote storage as a
    channel between launches.

**R5. Byte and size checks** (*prototyped*).

- **The bounds.** A seed file is bounded by #292's seed bound. The
  artifact's manifest is bounded by `MAX_MANIFEST_BYTES` (64 KiB), and
  its pack by `MAX_ARTIFACT_PACK_BYTES` (512 MiB) and by its own claim.
  These are the export bound of ADR 0013 D6 item 4.
- **Enforced while reading.** A download reads at most one chunk past its
  bound, so a stream that runs long is never read whole. A claim over the
  bound is refused before the pack is read.
- **What fails a transfer.** A stream that errors, ends short, runs long,
  differs from its claim, or is still running at the deadline fails it.
  So does a local write that stops: every byte a download counts is a
  byte written, and a short write is resumed where it stopped. A
  transfer that would start within a fixed margin of the deadline never
  starts.

**R6. Termination** (*prototyped*).

- **The resource is deleted, not stopped.** Persistence is off at create:
  Vercel's `persistent: false`, and `delete_orphan_snapshots` at
  `destroy`. A stopped persistent sandbox would keep the agent's
  filesystem for 30 days, and most SDK calls would resume it.
- **A destroy's answer proves nothing.** An entry closes only when a
  passive lookup, read in the entry's scope, finds nothing.
- **Bounded retries, then the entry stays.** A destroy that returns while
  the resource is still listed is retried within a bound. The entry then
  stays open for reconciliation, reported as ADR 0013 D17's clean-up
  failure.
- **Only passive calls.** Reconciliation and termination use passive
  lookups, listings and destroy alone. A call that can resume a sandbox
  is never one of them.

**R7. Deadline and cost.**

- **The ceiling fits the provider's longest session.** A profile whose
  ceiling exceeds it (Hobby's 45 minutes, for example) is refused before
  anything is journaled.
- **An exhausted quota is a capability refusal.** Hobby pauses creation
  past its allowance, and that is never retried as a transient failure.
- **The survival bound is stated.** It is one renewal window of compute
  plus that window's model usage, and the facts record it per launch, as
  ADR 0013 D16 records the watch fact.

**R8. Remote loss and artifact recovery** (*prototyped* for
reconciliation).

- **Loss during the run step.** The provider ends the resource (an
  outage, the deadline, a deletion). The run step ends abnormally, ADR
  0013 D17's "ended by something the executor did not send".
- **Loss during the export.** That is a candidate refusal.
- **The work is lost.** As ADR 0013 D4 says of a crash, the next launch
  starts from the bound seed and its carried commit.
- **Reconciliation is remove-only.**
  - It runs at every command's entry. It lists by the run's tag, looks
    each entry up by name, destroys what it owns and verifies it gone.
  - It never reads a file of a remote resource, so it never exports and
    never imports.
  - An artifact found remotely after a crash is not recovered. An artifact
    found in the export directory is deleted, as D6 item 6 already says.
  - A listing that keeps failing blocks the run, and is never read as
    "nothing left".

**R9. No GitHub write by an agent** (*prototyped* for what a create may
carry).

- **No GitHub credential, in any form.** The remote holds no GitHub
  credential and no git credential, and the controller pushes. No
  credential is brokered for a GitHub host: a brokered `Authorization`
  for `*.github.com`, `*` or `githubusercontent.com` is GitHub write
  authority handed to the agent.
- **Provider features that would hand one in, or reach the operator's
  private network, stay off:** Vercel-managed credentials, a `GitSource`
  with credentials, and a Secure Compute network into the operator's
  VPC.
- **Egress to GitHub is not itself a write.** Without a credential it
  writes nothing.

**R10. Agent launches only.**

- **Export commands stay on the host's enforced backend.** These are
  `validation.commands` and `merge.verification_commands`. ADR 0006's
  records and the certificate that reads them would otherwise rest on
  the provider's honesty (§3.3).
- **A run would then use two backends,** and the pin records one (ADR
  0013 D8). A reopening designs that pin, or decides on its own record
  that remote validation is acceptable.

## 5. What reopens the decision

A new issue, opened after #274's MVP gate is recorded on a local runtime,
that brings:

1. **The owner's decisions on B2:** which data may leave the machine (the
   source, the issue and PR text, the prompts, and the model traffic
   wherever brokering decrypts it); the provider credential's class and
   scope, and where the controller reads it; and whether review may run
   remotely.
2. **A probe on a real account,** opt-in and skipped by default like the
   repository's other live probes, run with synthetic credentials and no
   model call. It measures:
   - the time from the deadline to the resource gone, and after a
     `SIGKILL`ed controller that had been renewing;
   - that the lookup used is passive, that destroy and its read-back
     behave as R6 needs, and what a non-persistent sandbox leaves;
   - upload and download limits, and what an interrupted transfer
     leaves on each side;
   - from inside: no credential in the environment, the files or a
     metadata service, and the egress policy that applies;
   - brokering with each agent CLI: whether it starts with a placeholder
     key, trusts the per-sandbox CA, and reaches its API.
3. **A design for R10's two-backend pin,** or a decision that remote
   validation is acceptable.
4. **The amendments §7 names,** dated like #267's.

## 6. The prototype

`tests/remote_sandbox_prototype.py` holds the prototype and a fake
provider, `FakeRemote`. It is test code because a no-go ships nothing. A
reopening that goes moves what it needs into `src/autoforge` under its own
review. The fake enforces its own deadline: an expired resource stops and
loses its files, as a non-persistent sandbox does. It never resumes
anything, and it injects the faults the tests need.

**What it shows**, on real git in `tmp_path`:

- **Seed pack in, candidate pack and manifest out.** The bound seed is
  uploaded. A repository is built on the remote side from the uploaded
  pack alone. Two commits made there come back as an artifact, and the
  package's unchanged `import_artifact` makes the candidate. The only
  channel between the two repositories is the fake's upload and download
  (`test_a_seed_pack_goes_in_and_a_candidate_comes_back_through_the_strict_import`).
  An artifact the transfer accepts is still refused by the import when
  its manifest names another launch
  (`test_an_artifact_the_transfer_accepts_is_still_refused_by_the_strict_import`).
- **The seed fails closed.** No upload happens when the git input does
  not bind the seed
  (`test_a_seed_the_git_input_does_not_bind_is_never_uploaded`), within
  the deadline's margin
  (`test_a_seed_upload_too_close_to_the_deadline_never_starts`), or for a
  file over its bound
  (`test_a_seed_file_over_its_bound_is_refused_before_it_is_sent`). A cut
  upload (`test_a_seed_upload_cut_mid_stream_refuses_the_launch`), a file
  stored otherwise
  (`test_a_seed_the_remote_stored_otherwise_refuses_the_launch`) and an
  expiry during the upload
  (`test_a_seed_upload_the_deadline_cuts_refuses_the_launch`) each refuse
  the launch.
- **The pack is the one the bound manifest names.** A pack changed beside
  an unchanged manifest is refused once it is sent, and the stager would
  not index it. That holds for another pack `index-pack --strict` would
  accept, arbitrary bytes, and one flipped byte
  (`test_a_seed_pack_its_bound_manifest_does_not_name_is_refused_and_never_staged`).
  The stager also refuses a remote copy changed after the upload, the
  pack alone or the pack with a manifest that names it
  (`test_a_seed_the_remote_holds_otherwise_is_never_staged`). A bound
  manifest that names no pack sends nothing
  (`test_a_bound_seed_manifest_that_names_no_pack_sends_nothing`).
- **The artifact fails closed.** A pack stream that errors, ends short or
  runs long leaves an empty export directory
  (`test_an_artifact_download_that_breaks_leaves_nothing_to_import`). So
  do a claim over the bound, refused before the pack is read
  (`test_a_manifest_claiming_a_pack_over_the_bound_is_refused_before_the_pack_is_read`),
  a manifest over its own bound
  (`test_a_manifest_over_its_own_bound_is_refused`), a download the
  deadline cuts (`test_an_artifact_download_the_deadline_cuts_is_refused`),
  and an expired resource
  (`test_an_expired_resource_has_nothing_to_download`). Nothing is ever
  merged with leftovers
  (`test_an_artifact_is_never_merged_with_leftovers_in_the_export_directory`).
  Short local writes still leave the whole artifact, which the import
  accepts (`test_short_local_writes_still_leave_the_whole_artifact`), and
  a local write that stops, in the manifest or the pack, leaves an empty
  export directory (`test_a_local_write_that_stops_leaves_nothing_to_import`).
- **R1, identity.** The entry round-trips and is read strictly
  (`test_the_journal_entry_round_trips_and_is_read_strictly`). A ceiling
  or window that is not finite is refused when the entry is made and
  when it is read
  (`test_a_journal_entry_whose_ceiling_or_window_is_not_finite_is_refused`).
  The name and tags derive from the entry alone
  (`test_the_name_and_tags_derive_from_the_entry_alone`).
  - A lost create answer is resolved by name and makes no second resource
    (`test_an_ambiguous_create_is_resolved_by_name_and_never_makes_a_second_resource`).
  - A create lost before it happened is re-issued under the same name
    (`test_a_create_lost_before_it_happened_is_issued_once_more_under_the_same_name`).
  - A name holding another resource refuses the launch and removes nothing
    (`test_a_name_holding_another_resource_refuses_the_launch_and_removes_nothing`).
  - Another scope
    (`test_a_scope_other_than_the_entry_refuses_the_create_before_the_journal`)
    and a ceiling past the provider's longest session
    (`test_a_ceiling_past_the_providers_longest_session_is_refused_before_anything`)
    are refused before the journal is written.
- **R3, the deadline.** It is renewed within its window and never past
  the ceiling
  (`test_the_deadline_is_renewed_within_its_window_and_never_past_the_ceiling`).
  A controller that stops renewing leaves a resource the provider ends
  within one window
  (`test_a_dead_controller_leaves_a_resource_the_provider_ends_within_one_window`).
  The bound does not rest on the fake keeping the deadline it was asked
  for. When it keeps the resource longer, the create is refused, its
  entry kept and reconciled away
  (`test_a_create_whose_deadline_reads_back_past_the_one_asked_is_refused_and_kept`),
  and a renewal past the window or past the ceiling ends the launch, the
  resource left for its verified removal
  (`test_a_renewal_whose_deadline_reads_back_past_its_bound_ends_the_launch`).
  A deadline read back as NaN or infinity, which the fake never ends, is
  refused the same way at the create
  (`test_a_create_whose_deadline_reads_back_not_finite_is_refused_and_kept`)
  and at a renewal
  (`test_a_renewal_whose_deadline_reads_back_not_finite_ends_the_launch`).
  A renewal or a transfer given such a deadline asks the provider nothing
  (`test_a_deadline_that_is_not_finite_starts_no_renewal_and_no_transfer`).
- **R6 and R8, termination and reconciliation.**
  - An entry closes only on a lookup that finds nothing
    (`test_termination_closes_only_on_a_lookup_that_finds_nothing`).
  - A destroy that leaves the resource keeps the entry open
    (`test_a_destroy_that_returns_and_leaves_the_resource_keeps_the_entry_open`),
    and so does a scope change during a removal
    (`test_a_scope_that_changes_during_a_removal_closes_nothing`).
  - After a crash, reconciliation removes the run's resource without
    reading a file of it
    (`test_reconciliation_after_a_crash_removes_the_runs_resource_and_never_reads_its_files`).
    It closes an entry whose resource is gone
    (`test_reconciliation_closes_an_entry_whose_resource_never_existed_or_is_gone`).
  - It blocks on a resource with the run's tag and no entry, and removes
    nothing (`test_a_resource_with_the_runs_tag_and_no_entry_blocks_and_is_never_removed`).
  - It lists and removes nothing in another scope
    (`test_reconciliation_in_a_scope_other_than_the_entries_lists_and_removes_nothing`).
  - It blocks, never reads "gone", when the provider keeps failing to
    answer
    (`test_a_provider_that_keeps_failing_to_answer_blocks_rather_than_reads_gone`).
  - It closes nothing when the scope changes during the pass
    (`test_a_scope_that_changes_during_reconciliation_closes_nothing`).
- **R2 and R9, credentials.** A create is refused, before its entry is
  written, when it would carry a GitHub or git credential route, the
  provider's own credential, a credential brokered for a GitHub host,
  `*` included, or a provider-managed credential
  (`test_a_create_that_would_hand_the_remote_a_github_or_provider_credential_is_refused`).
  A model key brokered for its own API is admitted
  (`test_a_model_credential_brokered_for_its_own_api_is_admitted`).

**What it does not show.**

- **No provider behaves like the fake until a probe says so.** The fake
  enforces its deadline at once and its lookups are passive. Its destroy
  and its stream ends are exactly what the tests inject. That is §5's
  list.
- **No network transport is exercised.** The fake's streams are Python
  iterators.
- **#292 has not built a seed.** The seed pack is `pack-objects` of the
  seed commit. Its manifest is a stand-in that the git input binds by
  digest, as #292's will be, and that names the pack's sha256, as R4 asks
  of a remote backend's.
- **Nothing runs inside a remote:** no stager, no prover and no agent.
  Test code plays their part on the remote side, the stager's check of
  the seed included.

## 7. The accepted records

A no-go amends nothing. This table says what a go would have to keep, and
what it would have to amend, in the records #276 names, as #267 amended
them.

| Record | What a remote backend keeps | What a go would amend |
|---|---|---|
| ADR 0002 | §4a to §4e as §4f maps them for containers: the run step's client is the executor's child, and `close()` ends the remote resource | [§3](0002-executor-nothing-outlives-the-invocation.md#3-decision) and [§4f](0002-executor-nothing-outlives-the-invocation.md#4f-amendment-container-backends-267-2026-11-08): after the controller's death, the survival bound is one renewal window (R3), not D6's death-watch delay |
| ADR 0004 | every GitHub write is a controller effect. The remote holds no GitHub credential, and nothing brokered reaches GitHub (R9). An ambiguous create follows D4.3's reconcile-before-re-issue rule (R1) | nothing |
| ADR 0005 | G1 is stronger: no host path reaches the remote. G2 holds for host credentials, but the model credential leaves the host (R2). G4 and G5 hold, since the candidate returns only as a strictly imported pack | G3 and G6 as ADR 0013 amended them, with R3's bound in place of D6's |
| ADR 0006 | exact-candidate verification. The candidate is the strictly imported artifact's commit ([D6.3 as amended](0006-candidate-validation-evidence-and-review-certificates.md#26-remote-materialization-a-byte-exact-export-with-a-hash-back-proof-item-6)), and validation evidence is produced locally (R10) | nothing while R10 holds; P's backend facts ([D8.1](0006-candidate-validation-evidence-and-review-certificates.md#28-execution-policy-p-and-sandbox-facts-item-8)) if remote validation is ever admitted |
| ADR 0008 | worker locks and the order L0, L2, then L1. Remote objects carry the `RunKey` in their tags. Reconciliation is the per-issue engine's, under L2, at its entry. No lock domain is added, and no provider call runs under L1 | nothing |
| ADR 0013 | D1's authority, D4's seeding, D5's import, D6's journal and reconciliation, and D12's copy-in, all as they apply to a copy-in runtime | [D16](0013-container-sandbox-backend-lifecycle-trust-boundaries-and-rollout.md#d16-the-capability-matrix-271-272-273-275): its refusal of a remote endpoint, the new host rows, and the probe items of §5; [D8](0013-container-sandbox-backend-lifecycle-trust-boundaries-and-rollout.md#d8-backend-selection-runtime-detection-and-the-pin-decision-8-273-with-271s-detection): the pin, for R10 |

**What stays as it is,** whatever is decided: the bwrap backend and its
default, the container backend's design, dry runs, and the merge gate.
No orchestrator is added. Nothing like Sandcastle is embedded, and no
provider-side lifecycle is used (no persistent sandboxes, no
`getOrCreate` with hooks, no restart). No mount of any kind reaches a
remote, and no credential is exposed beyond R2's model credential.

*Rejected.*

- **A go for Vercel Sandbox now, conditional on #274.** It would leave
  #277 open on decisions (B2) and measurements (B3) that nobody has
  scheduled. It would also suggest that a provider is on its way to
  support when nothing about it was measured.
- **A provider-neutral remote adapter in the package, unreachable until a
  go.** That would be code with no user, kept current against
  foundations that do not exist yet (B1). The prototype shows what a go
  needs without shipping it.
- **The provider's git source as the input.** See R4.
- **The provider's persistent sandboxes or snapshots** to carry state
  between launches. That is ADR 0013 D4's rejected volume reuse, on a
  third party's disk.
