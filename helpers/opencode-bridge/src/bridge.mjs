// @ts-check
/**
 * AutoForge's OpenCode SDK bridge (ADR 0007 D2.2, D3.1-D3.5, D4.2-D4.5, §5).
 *
 * One process per launch, started by the controller through its duplex
 * handle, inside the launch's allow-listed environment, containment and
 * outer sandbox. It speaks the `autoforge-opencode-bridge` envelope
 * (protocol 1) on stdin and stdout: one JSON object per LF-terminated line.
 * It starts the pinned OpenCode runtime as `<server_command> serve
 * --hostname=127.0.0.1 --port=0` as its own child, in its own process group,
 * with a fresh password, and talks to it through `createOpencodeClient` of
 * `@opencode-ai/sdk/v2` only. It never uses the SDK's server launcher.
 *
 * The routes it calls, and no others (D3.5): the version read-back
 * (`/global/health`), the event stream (`/event`), session get and create
 * (`/api/session`, the only route that takes a caller's session id), prompt
 * admission (`/session/{id}/prompt_async` with a caller's message id) and
 * its read-back (`/session/{id}/message/{messageID}`), the message history
 * (`/session/{id}/message`), the session status (`/session/status`), abort
 * (`/session/{id}/abort`), and question and permission reply and reject.
 * Runtime 1.18.34's `/api` session runner does not run a configured
 * provider's model and has no `wait` yet, so turns run through the classic
 * engine, and its asks and replies are the classic family throughout
 * (docs/adr/0007, D4.3's amendment).
 *
 * Nothing here is written to a log but the server's own output, which is
 * copied to stderr. The password never leaves this process and the server's
 * environment: not in an argv, a record or stderr.
 */
import { spawn } from "node:child_process"
import { createHash, randomBytes } from "node:crypto"
import { createOpencodeClient } from "@opencode-ai/sdk/v2/client"

const ENVELOPE = "autoforge-opencode-bridge"
const PROTOCOL = 1
// Set by build.mjs from the bundled SDK's package.json.
const SDK_VERSION = __SDK_VERSION__
const SERVER_USERNAME = "opencode"
const HOSTNAME = "127.0.0.1"
const READY = /^opencode server listening on (http:\/\/127\.0\.0\.1:([0-9]{1,5}))$/
const SESSION_ID = /^ses_[A-Za-z0-9_-]{1,120}$/
const MESSAGE_ID = /^msg_[A-Za-z0-9_-]{1,120}$/
const MODES = ["new", "continue", "recover"]
const START_KEYS = [
  "type",
  "server_command",
  "session_id",
  "mode",
  "input_id",
  "prompt",
  "cwd",
  "agent",
  "model",
  "expect",
  "policy",
  "limits",
]
// What each limit may be set to, in seconds or bytes, with its default.
const LIMITS = {
  startup_seconds: [1, 600, 60],
  request_seconds: [1, 600, 30],
  settle_seconds: [1, 600, 30],
  abort_seconds: [1, 300, 10],
  stop_seconds: [1, 60, 5],
  max_record_bytes: [4096, 256 * 1024 * 1024, 33 * 1024 * 1024],
  max_event_bytes: [4096, 256 * 1024 * 1024, 16 * 1024 * 1024],
  max_response_bytes: [4096, 1024 * 1024 * 1024, 64 * 1024 * 1024],
  max_messages: [1, 100000, 5000],
}
// Finish reasons of a final assistant message that end the turn with an answer.
const ANSWER_FINISHES = ["stop", "length"]
// The classic engine's observational events this bridge reads nothing of.
const OBSERVED = new Set([
  "server.connected",
  "server.heartbeat",
  "session.created",
  "session.updated",
  "session.diff",
  "session.compacted",
  "message.removed",
  "message.part.removed",
  "plugin.added",
  "catalog.updated",
  "reference.updated",
  "integration.updated",
  "integration.connection.updated",
  "models-dev.refreshed",
  "file.edited",
  "file.watcher.updated",
  "todo.updated",
  "lsp.updated",
  "lsp.client.diagnostics",
  "vcs.branch.updated",
  "project.updated",
  "project.directories.updated",
  "installation.updated",
  "installation.update-available",
  "mcp.tools.changed",
  "command.executed",
  "server.instance.disposed",
  "global.disposed",
  "workspace.ready",
  "workspace.status",
])
// The family this bridge does not answer: an ask of it fails closed.
const OTHER_FAMILY_ASKS = new Set(["question.v2.asked", "permission.v2.asked"])
// One allow-listed input shown per tool (never a command, a content or a diff).
const TOOL_DETAILS = {
  read: "filePath",
  edit: "filePath",
  write: "filePath",
  list: "path",
  glob: "pattern",
  grep: "pattern",
  webfetch: "url",
  websearch: "query",
}
const MAX_DETAIL_CHARS = 512
const MAX_REASON_CHARS = 400
// How often a streamed text or reasoning delta is reported as activity.
const DELTA_INTERVAL_MS = 2000

/** @typedef {{type: string, [key: string]: unknown}} EnvelopeRecord */

class BridgeFailure extends Error {
  /**
   * @param {string} code a fixed name of what failed
   * @param {string} reason bounded text naming fields, never a secret
   * @param {Record<string, unknown>} [details] flat, bounded facts
   */
  constructor(code, reason, details = {}) {
    super(reason)
    this.code = code
    this.details = details
  }
}

/** @param {unknown} value */
function text(value) {
  return typeof value === "string" ? value : ""
}

/** @param {string} value @param {number} limit */
function clip(value, limit) {
  const flat = value.replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]+/g, " ")
  return flat.length <= limit ? flat : flat.slice(0, limit - 3) + "..."
}

/** The SHA-256 of `value`'s canonical JSON (sorted keys, no spaces), in hex. @param {unknown} value */
function digest(value) {
  return createHash("sha256").update(canonical(value), "utf8").digest("hex")
}

/** @param {unknown} value @returns {string} */
function canonical(value) {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]"
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort()
    return "{" + keys.map((k) => JSON.stringify(k) + ":" + canonical(/** @type {any} */ (value)[k])).join(",") + "}"
  }
  return JSON.stringify(value === undefined ? null : value)
}

/** @param {unknown} value */
function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

// -- the envelope ---------------------------------------------------------------
class Envelope {
  constructor() {
    this.maxRecordBytes = LIMITS.max_record_bytes[2]
    this.closed = false
  }

  /** Write one record as one line; false when it does not fit the bound. @param {EnvelopeRecord} record */
  write(record) {
    if (this.closed) return false
    const line = JSON.stringify(record)
    if (Buffer.byteLength(line, "utf8") + 1 > this.maxRecordBytes) return false
    try {
      process.stdout.write(line + "\n")
    } catch {
      this.closed = true
      return false
    }
    return true
  }
}

/**
 * The controller's records, framed on LF with a bound, as they arrive.
 * Pushes each parsed record to `onRecord`, end of file to `onEnd`, and a
 * framing problem to `onError`.
 */
class Input {
  /**
   * @param {(record: EnvelopeRecord) => void} onRecord
   * @param {() => void} onEnd
   * @param {(failure: BridgeFailure) => void} onError
   */
  constructor(onRecord, onEnd, onError) {
    this.limit = LIMITS.max_record_bytes[2]
    /** @type {Buffer[]} */
    this.chunks = []
    this.size = 0
    this.onRecord = onRecord
    this.onEnd = onEnd
    this.onError = onError
    this.failed = false
    process.stdin.on("data", (chunk) => this.feed(/** @type {Buffer} */ (chunk)))
    process.stdin.on("end", () => {
      if (this.failed) return
      if (this.size) this.fail("stdin ended inside an unterminated record")
      else this.onEnd()
    })
    process.stdin.on("error", () => {
      if (!this.failed) this.onEnd()
    })
  }

  /** @param {Buffer} chunk */
  feed(chunk) {
    if (this.failed) return
    let start = 0
    for (;;) {
      const lf = chunk.indexOf(10, start)
      if (lf < 0) break
      const piece = chunk.subarray(start, lf)
      if (this.size + piece.length > this.limit) return this.fail("a controller record is larger than its bound")
      this.chunks.push(piece)
      const line = Buffer.concat(this.chunks).toString("utf8").replace(/\r$/, "")
      this.chunks = []
      this.size = 0
      start = lf + 1
      let record
      try {
        record = JSON.parse(line)
      } catch {
        return this.fail("a controller record is not JSON")
      }
      if (!isObject(record) || typeof record.type !== "string") return this.fail("a controller record has no type")
      this.onRecord(record)
      if (this.failed) return
    }
    const rest = chunk.subarray(start)
    if (this.size + rest.length > this.limit) return this.fail("a controller record is larger than its bound")
    if (rest.length) {
      this.chunks.push(rest)
      this.size += rest.length
    }
  }

  /** @param {string} reason */
  fail(reason) {
    this.failed = true
    this.onError(new BridgeFailure("protocol", reason))
  }
}

// -- the start record -------------------------------------------------------------
/**
 * The validated `start` record. Every field is required; nothing else is
 * accepted, so the controller cannot pass anything this bridge would not
 * check.
 * @param {EnvelopeRecord} record
 */
function parseStart(record) {
  const unknown = Object.keys(record).filter((k) => !START_KEYS.includes(k))
  if (unknown.length) throw new BridgeFailure("protocol", `start carries unknown field(s) ${unknown.sort().join(", ")}`)
  const need = (/** @type {boolean} */ ok, /** @type {string} */ what) => {
    if (!ok) throw new BridgeFailure("protocol", `start: ${what}`)
  }
  const command = record.server_command
  need(typeof command === "string" && command.startsWith("/") && !/[\u0000-\u001f]/.test(command), "server_command is not an absolute path")
  need(typeof record.mode === "string" && MODES.includes(record.mode), "mode is not new, continue or recover")
  need(typeof record.session_id === "string" && SESSION_ID.test(record.session_id), "session_id is not a session id")
  need(typeof record.input_id === "string" && MESSAGE_ID.test(record.input_id), "input_id is not a message id")
  need(typeof record.prompt === "string" && record.prompt.length > 0, "prompt is not a non-empty string")
  need(typeof record.cwd === "string" && record.cwd.startsWith("/"), "cwd is not an absolute path")
  need(typeof record.agent === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(record.agent), "agent is not an agent name")
  const model = record.model
  need(isObject(model), "model is not an object")
  const m = /** @type {Record<string, unknown>} */ (model)
  need(
    Object.keys(m).every((k) => ["provider_id", "model_id", "variant"].includes(k)),
    "model carries an unknown field",
  )
  need(typeof m.provider_id === "string" && /^[a-z0-9][a-z0-9._-]{0,63}$/.test(m.provider_id), "model.provider_id is not valid")
  need(typeof m.model_id === "string" && /^[^\s#]{1,256}$/.test(m.model_id), "model.model_id is not valid")
  need(m.variant === null || (typeof m.variant === "string" && /^[a-z][a-z0-9_-]{0,31}$/.test(m.variant)), "model.variant is not valid")
  const expect = record.expect
  need(isObject(expect) && Object.keys(/** @type {object} */ (expect)).join() === "runtime_version", "expect must hold runtime_version only")
  const runtimeVersion = /** @type {Record<string, unknown>} */ (expect).runtime_version
  need(typeof runtimeVersion === "string" && /^[0-9]+\.[0-9]+\.[0-9]+$/.test(runtimeVersion), "expect.runtime_version is not a version")
  const policy = record.policy
  need(isObject(policy), "policy is not an object")
  const p = /** @type {Record<string, unknown>} */ (policy)
  need(p.schema === 1 && p.provider === "opencode-sdk", "policy is not schema 1 of opencode-sdk")
  need(p.agent === record.agent, "policy.agent is not the start's agent")
  const replies = p.replies
  need(
    Array.isArray(replies) && replies.every((r) => r === "once" || r === "reject") && replies.includes("reject"),
    "policy.replies must be once and reject only",
  )
  const limitsIn = record.limits
  need(isObject(limitsIn), "limits is not an object")
  /** @type {Record<string, number>} */
  const limits = {}
  for (const [name, [low, high, fallback]] of Object.entries(LIMITS)) {
    const value = /** @type {Record<string, unknown>} */ (limitsIn)[name]
    if (value === undefined) {
      limits[name] = fallback
      continue
    }
    need(Number.isInteger(value) && /** @type {number} */ (value) >= low && /** @type {number} */ (value) <= high, `limits.${name} is out of range`)
    limits[name] = /** @type {number} */ (value)
  }
  const extra = Object.keys(/** @type {object} */ (limitsIn)).filter((k) => !(k in LIMITS))
  need(!extra.length, `limits carries unknown field(s) ${extra.sort().join(", ")}`)
  return {
    serverCommand: /** @type {string} */ (command),
    mode: /** @type {string} */ (record.mode),
    sessionID: /** @type {string} */ (record.session_id),
    inputID: /** @type {string} */ (record.input_id),
    prompt: /** @type {string} */ (record.prompt),
    cwd: /** @type {string} */ (record.cwd),
    agent: /** @type {string} */ (record.agent),
    providerID: /** @type {string} */ (m.provider_id),
    modelID: /** @type {string} */ (m.model_id),
    variant: /** @type {string | null} */ (m.variant),
    runtimeVersion: /** @type {string} */ (runtimeVersion),
    replies: /** @type {string[]} */ (replies),
    limits,
  }
}

// -- bounded fetch ----------------------------------------------------------------
/**
 * A response whose body errors once it has carried more than `limit` bytes,
 * or, with `perEvent`, more than `limit` bytes since its last blank line (one
 * server-sent event). Nothing is ever held past its bound.
 * @param {Response} response @param {number} limit @param {boolean} perEvent @param {(why: string) => void} onExceeded
 */
function bounded(response, limit, perEvent, onExceeded) {
  if (!response.body) return response
  let since = 0
  let previous = 0
  const guard = new TransformStream({
    transform(chunk, controller) {
      if (perEvent) {
        for (const byte of chunk) {
          since = byte === 10 && previous === 10 ? 0 : since + 1
          previous = byte === 13 ? previous : byte
          if (since > limit) {
            onExceeded("an event of the server's stream is larger than its bound")
            controller.error(new Error("event larger than its bound"))
            return
          }
        }
      } else {
        since += chunk.byteLength
        if (since > limit) {
          onExceeded("a response of the server is larger than its bound")
          controller.error(new Error("response larger than its bound"))
          return
        }
      }
      controller.enqueue(chunk)
    },
  })
  return new Response(response.body.pipeThrough(guard), {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  })
}

// -- the server -------------------------------------------------------------------
/**
 * The runtime as this bridge's own child: never `detached` (it stays in the
 * bridge's process group, where the executor's group signals reach it), never
 * through a shell, its stdin ignored, both its pipes drained to stderr. Its
 * environment is the bridge's, which is the launch's, plus the password.
 */
class Server {
  /** @param {string} command @param {string} cwd @param {string} password */
  constructor(command, cwd, password) {
    this.exitCode = /** @type {number | null} */ (null)
    this.signal = /** @type {string | null} */ (null)
    this.exited = false
    this.stopped = "running"
    /** @type {((value: void) => void)[]} */
    this.waiters = []
    /** @type {Record<string, string | undefined>} */
    const env = { ...process.env, OPENCODE_SERVER_PASSWORD: password }
    delete env.OPENCODE_SERVER_USERNAME
    this.proc = spawn(command, ["serve", `--hostname=${HOSTNAME}`, "--port=0"], {
      cwd,
      env,
      stdio: ["ignore", "pipe", "pipe"],
      detached: false,
      shell: false,
      windowsHide: true,
    })
    this.proc.on("exit", (code, signal) => {
      this.exited = true
      this.exitCode = code
      this.signal = signal
      for (const resolve of this.waiters.splice(0)) resolve()
    })
    this.proc.stderr?.on("data", (chunk) => copyToStderr(chunk))
  }

  /**
   * The URL the readiness line names, read from stdout within `deadlineMs`.
   * Every other line is copied to stderr, then and afterwards.
   * @param {number} deadlineMs
   * @returns {Promise<string>}
   */
  ready(deadlineMs) {
    return new Promise((resolve, reject) => {
      let buffer = ""
      let found = false
      const timer = setTimeout(() => {
        if (!found) reject(new BridgeFailure("startup", `the server printed no readiness line within ${deadlineMs / 1000}s`))
      }, deadlineMs)
      this.proc.on("error", (error) => {
        clearTimeout(timer)
        reject(new BridgeFailure("startup", `the server could not be started (${/** @type {NodeJS.ErrnoException} */ (error).code ?? "error"})`))
      })
      this.proc.on("exit", (code, signal) => {
        clearTimeout(timer)
        if (!found) reject(new BridgeFailure("startup", `the server exited before it was ready (exit ${code ?? signal})`))
      })
      this.proc.stdout?.on("data", (chunk) => {
        if (found) return copyToStderr(chunk)
        buffer += chunk.toString("utf8")
        let lf
        while (!found && (lf = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, lf).replace(/\r$/, "")
          buffer = buffer.slice(lf + 1)
          const match = READY.exec(line)
          if (match && Number(match[2]) >= 1 && Number(match[2]) <= 65535) {
            found = true
            clearTimeout(timer)
            resolve(match[1])
          } else {
            copyToStderr(Buffer.from(line + "\n"))
          }
        }
        if (found && buffer) copyToStderr(Buffer.from(buffer))
        if (!found && buffer.length > 65536) {
          copyToStderr(Buffer.from(buffer))
          buffer = ""
        }
      })
    })
  }

  /** @param {number} ms */
  waitExit(ms) {
    if (this.exited) return Promise.resolve(true)
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve(this.exited), ms)
      this.waiters.push(() => {
        clearTimeout(timer)
        resolve(true)
      })
    })
  }

  /**
   * SIGTERM, a bounded wait, then SIGKILL and another; what happened, as
   * evidence for the controller, never a claim the group is empty (the
   * executor checks that).
   * @param {number} graceMs
   */
  async stop(graceMs) {
    if (!this.exited) {
      this.stopped = "sigterm"
      this.proc.kill("SIGTERM")
      if (!(await this.waitExit(graceMs))) {
        this.stopped = "sigkill"
        this.proc.kill("SIGKILL")
        if (!(await this.waitExit(graceMs))) this.stopped = "survived"
      }
    } else if (this.stopped === "running") {
      this.stopped = "exited"
    }
    return { stopped: this.stopped, exit_code: this.exitCode, signal: this.signal }
  }
}

/** @param {Buffer} chunk */
function copyToStderr(chunk) {
  try {
    process.stderr.write(chunk)
  } catch {
    // The controller drains stderr; a closed stderr loses only diagnostics.
  }
}

// -- the bridge -------------------------------------------------------------------
class Bridge {
  constructor() {
    this.out = new Envelope()
    this.asks = 0
    /** @type {Map<string, {kind: string, providerID: string, sessionID: string, state: string}>} */
    this.open = new Map()
    this.skippedUnknown = 0
    this.foreignEvents = 0
    this.siblingSettled = 0
    this.rejectedPermission = false
    this.streamError = ""
    /** @type {Promise<void>} */
    this.decisions = Promise.resolve()
    this.events = 0
    /** @type {null | ReturnType<typeof parseStart>} */
    this.start = null
    /** @type {null | Server} */
    this.server = null
    /** @type {null | ReturnType<typeof createOpencodeClient>} */
    this.client = null
    this.stream = new AbortController()
    this.state = "hello"
    this.turnRunning = false
    this.aborting = false
    this.settling = false
    this.settleAgain = false
    this.idleAt = 0
    /** @type {NodeJS.Timeout | null} */
    this.settleTimer = null
    /** @type {null | {name: string, status: number | null, retryable: boolean | null}} */
    this.lastError = null
    this.toolsStarted = new Set()
    this.lastDelta = 0
    this.finishing = false
    this.stopRequested = false
    /** @type {(value: {outcome: string, failure?: BridgeFailure, result?: EnvelopeRecord}) => void} */
    this.settle = () => {}
    /** @type {Promise<{outcome: string, failure?: BridgeFailure, result?: EnvelopeRecord}>} */
    this.done = new Promise((resolve) => {
      this.settle = resolve
    })
  }

  // -- the run --------------------------------------------------------------------
  async run() {
    process.stdout.on("error", () => {
      this.out.closed = true
    })
    new Input(
      (record) => this.onRecord(record),
      () => this.onStdinEnd(),
      (failure) => this.fail(failure),
    )
    this.out.write({
      type: "hello",
      envelope: ENVELOPE,
      protocol: PROTOCOL,
      sdk_version: SDK_VERSION,
      node_version: process.versions.node,
    })
    const ended = await this.done
    await this.finish(ended)
  }

  /** @param {EnvelopeRecord} record */
  onRecord(record) {
    if (this.finishing) return
    if (this.state === "hello") {
      if (record.type !== "start") return this.fail(new BridgeFailure("protocol", `expected start, got ${clip(record.type, 40)}`))
      try {
        this.start = parseStart(record)
      } catch (error) {
        return this.fail(error instanceof BridgeFailure ? error : new BridgeFailure("protocol", "start is not valid"))
      }
      this.out.maxRecordBytes = this.start.limits.max_record_bytes
      this.state = "starting"
      this.begin().catch((error) => this.fail(error))
      return
    }
    if (record.type === "decision") {
      // One decision at a time, in the order they came: a reject settles the
      // session's other asks, which a later decision must see.
      this.decisions = this.decisions.then(() => this.decide(record)).catch((error) => this.fail(error))
    } else if (record.type === "interrupt") {
      if (Object.keys(record).length !== 1) return this.fail(new BridgeFailure("protocol", "interrupt carries fields"))
      this.interrupt().catch((error) => this.fail(error))
    } else {
      this.fail(new BridgeFailure("protocol", `unknown controller record ${clip(record.type, 40)}`))
    }
  }

  /** End of file on stdin: the orderly-shutdown request (D3.3). */
  onStdinEnd() {
    // An abort already under way settles the run itself, within its window:
    // stopping the server first would cut the runtime's own abort short.
    if (this.finishing || this.aborting) return
    if (this.turnRunning && this.state !== "starting") {
      this.interrupt().catch((error) => this.fail(error))
      return
    }
    this.settle({ outcome: "interrupted" })
  }

  /** @param {unknown} error */
  fail(error) {
    const failure =
      error instanceof BridgeFailure
        ? error
        : new BridgeFailure("internal", `the bridge failed (${error instanceof Error ? clip(error.name, 60) : "error"})`)
    this.settle({ outcome: "failed", failure })
  }

  // -- startup, binding and admission (D3.1, D3.4, D4.2, D9.5) ---------------------
  async begin() {
    const start = /** @type {NonNullable<typeof this.start>} */ (this.start)
    const password = randomBytes(32).toString("base64url")
    const server = new Server(start.serverCommand, start.cwd, password)
    this.server = server
    const url = await server.ready(start.limits.startup_seconds * 1000)
    this.checkpoint()
    const authorization = "Basic " + Buffer.from(`${SERVER_USERNAME}:${password}`).toString("base64")
    const requestMs = start.limits.request_seconds * 1000
    /** @param {RequestInfo | URL} input @param {RequestInit} [init] */
    const boundedFetch = async (input, init) => {
      const request = input instanceof Request && init === undefined ? input : new Request(input, init)
      const path = new URL(request.url).pathname
      if (path === "/event") {
        const response = await fetch(request)
        return bounded(response, start.limits.max_event_bytes, true, (why) => this.fail(new BridgeFailure("bounds", why)))
      }
      const signal = AbortSignal.any([request.signal, AbortSignal.timeout(requestMs)])
      const response = await fetch(new Request(request, { signal }))
      return bounded(response, start.limits.max_response_bytes, false, (why) => this.fail(new BridgeFailure("bounds", why)))
    }
    this.client = createOpencodeClient({
      baseUrl: url,
      headers: { authorization },
      directory: start.cwd,
      fetch: boundedFetch,
    })
    const health = await this.call("the version read-back", () => this.api().global.health())
    const version = isObject(health) ? text(/** @type {any} */ (health).version) : ""
    this.out.write({ type: "server", runtime_version: clip(version, 64) })
    if (version !== start.runtimeVersion) {
      throw new BridgeFailure("pair", `the server reports runtime ${clip(version, 64) || "(none)"}, not ${start.runtimeVersion}`)
    }
    this.checkpoint()
    await this.subscribe(start.limits.startup_seconds * 1000)
    this.checkpoint()
    await this.bind(start)
    this.checkpoint()
    if (start.mode === "recover") {
      // Nothing is admitted: the final answer is read from the history (D9.9).
      if ((await this.readInput(start, true)) !== "identical") {
        throw new BridgeFailure("admission", "the caller's input is not complete in the session")
      }
      this.state = "settling"
      this.requestSettle()
      return
    }
    this.turnRunning = true
    const sent = await this.admit(start)
    this.checkpoint()
    this.state = "running"
    // An identical input already in the session was not sent again: its turn
    // ran under an earlier server, so the history alone can say how it ended.
    if (!sent) this.requestSettle()
  }

  /** A stop requested while startup awaited a request ends startup there. */
  checkpoint() {
    if (this.finishing || this.stopRequested) throw new BridgeFailure("stopped", "the bridge was stopped during startup")
  }

  api() {
    return /** @type {NonNullable<typeof this.client>} */ (this.client)
  }

  /**
   * One request: its data on success, a failure naming what was asked
   * otherwise. The generated client returns errors rather than throwing by
   * default, and a network failure arrives as an error without a response:
   * both are handled.
   * @param {string} what
   * @param {() => Promise<any>} request
   * @param {{allow?: number[]}} [options] statuses returned as `{status}` instead of failing
   */
  async call(what, request, options = {}) {
    let result
    try {
      result = await request()
    } catch (error) {
      throw new BridgeFailure("request", `${what} failed (${error instanceof Error ? clip(error.name, 60) : "error"})`)
    }
    const status = result?.response?.status
    if (status === undefined) throw new BridgeFailure("request", `${what} got no response`)
    if (options.allow?.includes(status)) return { status }
    if (result.error !== undefined || status < 200 || status > 299) {
      const tag = isObject(result.error) ? text(result.error._tag) || text(result.error.name) : ""
      throw new BridgeFailure("request", `${what} was refused (HTTP ${status}${tag ? ", " + clip(tag, 60) : ""})`, { status })
    }
    return result.data
  }

  /**
   * Subscribe to the event stream before any session call, so that no ask
   * can be missed (point 6), and wait for its first event. The stream is not
   * retried: a disconnection fails the invocation, since the live stream has
   * no replay that would prove nothing was missed.
   * @param {number} deadlineMs
   */
  async subscribe(deadlineMs) {
    /** @type {(value: void) => void} */
    let connected = () => {}
    const first = new Promise((resolve) => {
      connected = resolve
    })
    let sub
    try {
      sub = await this.api().event.subscribe(
        {},
        {
          signal: this.stream.signal,
          sseMaxRetryAttempts: 1,
          onSseError: (/** @type {unknown} */ error) => {
            // What failed, as the client words it (a status line): never a header.
            this.streamError = clip(error instanceof Error ? error.message : "error", 120)
          },
        },
      )
    } catch {
      throw new BridgeFailure("stream", "the event stream could not be opened")
    }
    ;(async () => {
      let first_ = true
      for await (const event of sub.stream) {
        if (first_) {
          first_ = false
          connected()
        }
        this.onEvent(event)
      }
    })()
      .catch(() => {})
      .finally(() => {
        if (!this.finishing) {
          const why = this.streamError ? ` (${this.streamError})` : ""
          this.fail(new BridgeFailure("stream", `the event stream ended before the turn did${why}`))
        }
      })
    const timer = new Promise((_, reject) =>
      setTimeout(() => reject(new BridgeFailure("stream", "the event stream sent nothing in time")), deadlineMs).unref(),
    )
    await Promise.race([first, timer])
  }

  /**
   * The session, bound by its caller id and checked against what was asked
   * (D9.5): `new` refuses an id that already exists (the create route would
   * hand the existing session back), `continue` and `recover` require it.
   * @param {NonNullable<typeof this.start>} start
   */
  async bind(start) {
    const found = await this.call(
      "the session read",
      () => this.api().v2.session.get({ sessionID: start.sessionID }),
      { allow: [404] },
    )
    const exists = !(isObject(found) && found.status === 404)
    let info
    if (start.mode === "new") {
      if (exists) throw new BridgeFailure("session", "a session with the caller's id already exists")
      /** @type {{providerID: string, id: string, variant?: string}} */
      const model = { providerID: start.providerID, id: start.modelID }
      if (start.variant) model.variant = start.variant
      const created = await this.call("the session create", () =>
        this.api().v2.session.create({
          id: start.sessionID,
          agent: start.agent,
          model,
          location: { directory: start.cwd },
        }),
      )
      info = isObject(created) ? /** @type {any} */ (created).data : undefined
    } else {
      if (!exists) throw new BridgeFailure("session", `no session with the caller's id exists to ${start.mode}`)
      info = /** @type {any} */ (found).data
    }
    const problems = []
    if (!isObject(info)) problems.push("no session")
    else {
      if (info.id !== start.sessionID) problems.push("id")
      if (info.parentID !== undefined) problems.push("parent")
      if (!isObject(info.location) || info.location.directory !== start.cwd) problems.push("location")
      if (info.agent !== start.agent) problems.push("agent")
      const model = info.model
      if (!isObject(model) || model.providerID !== start.providerID || model.id !== start.modelID) problems.push("model")
      else if (start.variant && model.variant !== start.variant) problems.push("variant")
    }
    if (problems.length) {
      throw new BridgeFailure("session", `the session read back does not match the start record (${problems.join(", ")})`)
    }
    this.out.write({
      type: "session",
      session_id: start.sessionID,
      mode: start.mode,
      directory: start.cwd,
      agent: start.agent,
      provider_id: start.providerID,
      model_id: start.modelID,
      variant: start.variant,
    })
  }

  /**
   * The input under the caller's message id, read back. The classic route
   * neither refuses nor de-duplicates a message id it already holds (it adds
   * the parts again), so the id is read first: an identical input is not
   * sent again, a different one is a conflict, and an absent one is sent and
   * then read back until it holds exactly the prompt.
   * @param {NonNullable<typeof this.start>} start
   */
  async admit(start) {
    const existing = await this.readInput(start, false)
    if (existing === "identical") {
      this.out.write({ type: "admitted", input_id: start.inputID, sent: false })
      return false
    }
    /** @type {{sessionID: string, messageID: string, agent: string, model: {providerID: string, modelID: string}, parts: {type: "text", text: string}[], variant?: string}} */
    const body = {
      sessionID: start.sessionID,
      messageID: start.inputID,
      agent: start.agent,
      model: { providerID: start.providerID, modelID: start.modelID },
      parts: [{ type: "text", text: start.prompt }],
    }
    if (start.variant) body.variant = start.variant
    await this.call("the prompt admission", () => this.api().session.promptAsync(body))
    const until = Date.now() + start.limits.request_seconds * 1000
    for (;;) {
      if ((await this.readInput(start, false)) === "identical") break
      if (Date.now() > until) throw new BridgeFailure("admission", "the admitted input was not read back in time")
      await sleep(100)
    }
    this.out.write({ type: "admitted", input_id: start.inputID, sent: true })
    return true
  }

  /**
   * `absent`, `identical`, or a failure: the message under the input id is
   * not the caller's prompt (a conflict), or, with `required`, is absent.
   * A message whose parts are still being written reads as absent.
   * @param {NonNullable<typeof this.start>} start @param {boolean} required
   */
  async readInput(start, required) {
    const read = await this.call(
      "the input read-back",
      () => this.api().session.message({ sessionID: start.sessionID, messageID: start.inputID }),
      { allow: [404] },
    )
    if (isObject(read) && read.status === 404) {
      if (required) throw new BridgeFailure("admission", "the caller's input does not exist in the session")
      return "absent"
    }
    const info = isObject(read) ? read.info : undefined
    const parts = isObject(read) && Array.isArray(read.parts) ? read.parts : []
    if (!isObject(info) || info.role !== "user" || info.sessionID !== start.sessionID || info.id !== start.inputID) {
      throw new BridgeFailure("admission", "the message under the caller's input id is not the caller's input")
    }
    if (!parts.length) return "absent"
    const texts = parts.filter((p) => isObject(p) && p.type === "text")
    if (texts.length !== parts.length || texts.length !== 1 || texts[0].text !== start.prompt) {
      throw new BridgeFailure("admission", "the caller's input id holds a different input (a conflict)")
    }
    return "identical"
  }

  // -- observation (D10.4) ---------------------------------------------------------
  /** @param {any} event */
  onEvent(event) {
    if (this.finishing) return
    this.events += 1
    const type = isObject(event) ? text(event.type) : ""
    const props = isObject(event) && isObject(event.properties) ? event.properties : {}
    const start = this.start
    if (!start || !type) {
      this.skippedUnknown += 1
      return
    }
    const sessionID = text(props.sessionID) || text(props.info?.sessionID) || text(props.part?.sessionID)
    if (type === "question.asked" || type === "permission.asked") {
      if (sessionID !== start.sessionID) {
        this.refuseForeignAsk(type, props).catch((error) => this.fail(error))
        return
      }
      this.onAsk(type, props).catch((error) => this.fail(error))
      return
    }
    if (OTHER_FAMILY_ASKS.has(type)) {
      this.fail(new BridgeFailure("interaction", `an ask of a family this bridge does not answer (${type})`))
      return
    }
    if (OBSERVED.has(type) || type.startsWith("session.next.")) return
    if (sessionID && sessionID !== start.sessionID) {
      this.foreignEvents += 1
      return
    }
    switch (type) {
      case "session.status":
        return this.onStatus(props.status)
      case "session.idle":
        return this.requestSettle()
      case "session.error":
        return this.onSessionError(props.error)
      case "message.updated":
        return this.onMessage(props.info)
      case "message.part.updated":
        return this.onPart(props.part)
      case "message.part.delta":
        return this.onDelta()
      case "question.replied":
      case "question.rejected":
      case "permission.replied":
        return this.onReplied(text(props.requestID))
      default:
        this.skippedUnknown += 1
    }
  }

  /** @param {any} status */
  onStatus(status) {
    const kind = isObject(status) ? status.type : undefined
    if (kind === "busy") {
      this.out.write({ type: "event", kind: "status", status: "busy" })
    } else if (kind === "retry") {
      const attempt = Number.isInteger(status.attempt) ? status.attempt : null
      this.out.write({ type: "event", kind: "status", status: "retry", attempt })
    } else if (kind === "idle") {
      this.out.write({ type: "event", kind: "status", status: "idle" })
      this.requestSettle()
    } else {
      this.fail(new BridgeFailure("protocol", `the session reported an unknown status (${clip(text(kind), 40) || "none"})`))
    }
  }

  /** @param {any} error */
  onSessionError(error) {
    const name = isObject(error) ? text(error.name) : ""
    const data = isObject(error) && isObject(error.data) ? error.data : {}
    this.lastError = {
      name: clip(name, 80),
      status: Number.isInteger(data.statusCode) ? data.statusCode : null,
      retryable: typeof data.isRetryable === "boolean" ? data.isRetryable : null,
    }
    this.out.write({ type: "event", kind: "error", name: clip(name, 80) })
  }

  /** @param {any} info */
  onMessage(info) {
    if (!isObject(info) || info.role !== "assistant") return
    const completed = isObject(info.time) && Number.isFinite(info.time.completed)
    if (completed && typeof info.finish === "string") {
      this.out.write({ type: "event", kind: "message", finish: clip(info.finish, 40) })
    }
  }

  /** @param {any} part */
  onPart(part) {
    if (!isObject(part)) return
    if (part.type === "text") this.out.write({ type: "event", kind: "text" })
    else if (part.type === "reasoning") this.out.write({ type: "event", kind: "reasoning" })
    else if (part.type === "step-finish") this.out.write({ type: "event", kind: "step", reason: clip(text(part.reason), 40) })
    else if (part.type === "tool") this.onTool(part)
  }

  /** @param {any} part */
  onTool(part) {
    const state = isObject(part.state) ? part.state : {}
    const status = text(state.status)
    const tool = clip(text(part.tool), 64)
    const callID = clip(text(part.callID), 128)
    if (status === "running" && !this.toolsStarted.has(callID)) {
      this.toolsStarted.add(callID)
      const key = /** @type {Record<string, string>} */ (TOOL_DETAILS)[tool]
      const value = key && isObject(state.input) ? state.input[key] : undefined
      this.out.write({
        type: "event",
        kind: "tool",
        status: "running",
        tool,
        call_id: callID,
        detail: typeof value === "string" ? clip(value, MAX_DETAIL_CHARS) : "",
      })
    } else if (status === "completed" || status === "error") {
      this.toolsStarted.delete(callID)
      this.out.write({
        type: "event",
        kind: "tool",
        status,
        tool,
        call_id: callID,
        input_sha256: digest(state.input ?? null),
        result_sha256: digest(status === "completed" ? (state.output ?? null) : (state.error ?? null)),
      })
    }
  }

  onDelta() {
    const now = Date.now()
    if (now - this.lastDelta < DELTA_INTERVAL_MS) return
    this.lastDelta = now
    this.out.write({ type: "event", kind: "text" })
  }

  /** @param {string} providerID */
  onReplied(providerID) {
    for (const ask of this.open.values()) {
      if (ask.providerID === providerID && ask.state === "open") ask.state = "settled"
    }
  }

  // -- asks and decisions (D9.6) ------------------------------------------------------
  /** @param {string} type @param {any} props */
  async onAsk(type, props) {
    const kind = type === "question.asked" ? "question" : "permission"
    const providerID = text(props.id)
    const requestID = String(++this.asks)
    const tool = isObject(props.tool)
      ? { message_id: clip(text(props.tool.messageID), 128), call_id: clip(text(props.tool.callID), 128) }
      : null
    /** @type {EnvelopeRecord} */
    let record
    if (kind === "question") {
      const questions = Array.isArray(props.questions) ? props.questions : null
      const valid =
        questions &&
        questions.length > 0 &&
        questions.every(
          (/** @type {any} */ q) =>
            isObject(q) &&
            typeof q.question === "string" &&
            typeof q.header === "string" &&
            Array.isArray(q.options) &&
            q.options.every((/** @type {any} */ o) => isObject(o) && typeof o.label === "string" && typeof o.description === "string") &&
            (q.multiple === undefined || typeof q.multiple === "boolean") &&
            (q.custom === undefined || typeof q.custom === "boolean"),
        )
      if (!providerID || !valid) return this.refuseMalformed(kind, providerID)
      record = {
        type: "ask",
        request_id: requestID,
        kind,
        provider_request_id: providerID,
        session_id: text(props.sessionID),
        questions: questions.map((/** @type {any} */ q) => ({
          question: q.question,
          header: q.header,
          options: q.options.map((/** @type {any} */ o) => ({ label: o.label, description: o.description })),
          multiple: q.multiple ?? null,
          custom: q.custom ?? null,
        })),
        tool,
      }
    } else {
      const patterns = Array.isArray(props.patterns) ? props.patterns : null
      const valid =
        typeof props.permission === "string" && patterns !== null && patterns.every((/** @type {any} */ p) => typeof p === "string")
      if (!providerID || !valid) return this.refuseMalformed(kind, providerID)
      record = {
        type: "ask",
        request_id: requestID,
        kind,
        provider_request_id: providerID,
        session_id: text(props.sessionID),
        permission: props.permission,
        patterns,
        metadata_sha256: digest(props.metadata ?? null),
        tool,
      }
    }
    this.open.set(requestID, { kind, providerID, sessionID: text(props.sessionID), state: "open" })
    if (!this.out.write(record)) {
      // Too large for the envelope: refused, never shown in part.
      await this.reject(this.open.get(requestID), "")
      throw new BridgeFailure("interaction", `an ask (${kind}) is larger than the record bound`)
    }
  }

  /** @param {string} kind @param {string} providerID */
  async refuseMalformed(kind, providerID) {
    if (providerID) await this.reject({ kind, providerID, sessionID: "", state: "open" }, "")
    throw new BridgeFailure("interaction", `an ask (${kind}) carries a field this bridge cannot render`)
  }

  /** @param {string} type @param {any} props */
  async refuseForeignAsk(type, props) {
    const kind = type === "question.asked" ? "question" : "permission"
    const providerID = text(props.id)
    if (providerID) await this.reject({ kind, providerID, sessionID: text(props.sessionID), state: "open" }, "")
    throw new BridgeFailure("interaction", `an ask (${kind}) came from a session outside the bound session`)
  }

  /** @param {EnvelopeRecord} record */
  async decide(record) {
    const allowed = ["type", "request_id", "directive", "answers", "message"]
    if (Object.keys(record).some((k) => !allowed.includes(k))) throw new BridgeFailure("protocol", "decision carries an unknown field")
    const ask = this.open.get(text(record.request_id))
    if (!ask) throw new BridgeFailure("protocol", "a decision for a request this bridge never sent")
    if (ask.state === "sibling") return
    if (ask.state !== "open") throw new BridgeFailure("protocol", "a second decision for one request")
    const directive = text(record.directive)
    const message = record.message === undefined || record.message === null ? "" : record.message
    if (typeof message !== "string") throw new BridgeFailure("protocol", "decision.message is not a string")
    if (directive === "pause") {
      // Left unanswered: the controller closes stdin, and the stop fails it (D9.6).
      ask.state = "paused"
      return
    }
    if (ask.kind === "question") {
      if (directive === "answer") {
        const answers = record.answers
        if (!Array.isArray(answers) || !answers.every((a) => Array.isArray(a) && a.every((s) => typeof s === "string"))) {
          throw new BridgeFailure("protocol", "decision.answers is not a list of lists of strings")
        }
        ask.state = "answered"
        await this.call("the question reply", () =>
          this.api().question.reply({ requestID: ask.providerID, answers: /** @type {string[][]} */ (answers) }),
        ).catch((error) => {
          throw staleOr(error, "question")
        })
        return
      }
      if (directive === "reject" || directive === "fail") {
        await this.reject(ask, "")
        return
      }
    } else {
      if (directive === "once" && this.start?.replies.includes("once")) {
        ask.state = "answered"
        await this.call("the permission reply", () =>
          this.api().permission.reply({ requestID: ask.providerID, reply: "once" }),
        ).catch((error) => {
          throw staleOr(error, "permission")
        })
        return
      }
      if (directive === "reject" || directive === "fail") {
        await this.reject(ask, message)
        return
      }
      if (directive === "always") {
        await this.reject(ask, "")
        throw new BridgeFailure("interaction", "an always reply is never sent (it would outlive the call)")
      }
    }
    throw new BridgeFailure("protocol", `decision.directive ${clip(directive, 20)} does not fit a ${ask.kind}`)
  }

  /**
   * Reject `ask`. A permission reject settles the session's other pending
   * asks too (D9.6): those are read back from the pending lists, marked, never
   * answered afterwards, and reported as a count.
   * @param {any} ask @param {string} message
   */
  async reject(ask, message) {
    ask.state = "rejected"
    if (ask.kind === "question") {
      await this.call("the question reject", () => this.api().question.reject({ requestID: ask.providerID })).catch((error) => {
        throw staleOr(error, "question")
      })
      return
    }
    /** @type {{requestID: string, reply: "reject", message?: string}} */
    const body = { requestID: ask.providerID, reply: "reject" }
    if (message) body.message = message
    const earlier = this.rejectedPermission
    this.rejectedPermission = true
    try {
      await this.call("the permission reject", () => this.api().permission.reply(body))
    } catch (error) {
      // An ask already gone after an earlier reject of this session's was
      // settled by it (D9.6): what this reject asked for already happened.
      if (earlier && error instanceof BridgeFailure && error.details.status === 404) {
        ask.state = "sibling"
        this.siblingSettled += 1
        this.out.write({ type: "event", kind: "asks_settled", count: 1 })
        return
      }
      throw staleOr(error, "permission")
    }
    const others = [...this.open.values()].filter((other) => other.state === "open" && other !== ask)
    if (!others.length) return
    const questions = await this.call("the pending questions", () => this.api().question.list({}))
    const permissions = await this.call("the pending permissions", () => this.api().permission.list({}))
    const pending = new Set(
      [...(Array.isArray(questions) ? questions : []), ...(Array.isArray(permissions) ? permissions : [])]
        .filter((p) => isObject(p))
        .map((p) => text(p.id)),
    )
    let count = 0
    for (const other of others) {
      if (!pending.has(other.providerID)) {
        other.state = "sibling"
        count += 1
      }
    }
    if (count) {
      this.siblingSettled += count
      this.out.write({ type: "event", kind: "asks_settled", count })
    }
  }

  // -- interrupt and completion (D10.3, point 9, point 11) --------------------------
  async interrupt() {
    const start = this.start
    if (!start || !this.client || this.aborting || this.state === "starting") {
      // Before the input is admitted there is no turn to abort: startup stops
      // at its next step, and nothing is admitted after it.
      this.stopRequested = true
      if (!this.aborting) this.settle({ outcome: "interrupted" })
      return
    }
    this.aborting = true
    if (this.turnRunning) {
      await this.call("the abort", () => this.api().session.abort({ sessionID: start.sessionID }))
      this.requestSettle()
      setTimeout(() => this.settle({ outcome: "interrupted" }), start.limits.abort_seconds * 1000).unref()
    } else {
      this.settle({ outcome: "interrupted" })
    }
  }

  requestSettle() {
    if (this.finishing || !this.start || (this.state !== "running" && this.state !== "settling")) return
    if (!this.idleAt) this.idleAt = Date.now()
    if (this.settling) {
      this.settleAgain = true
      return
    }
    this.settling = true
    this.checkSettled()
      .catch((error) => this.fail(error))
      .finally(() => {
        this.settling = false
        if (this.settleAgain && !this.finishing) {
          this.settleAgain = false
          this.requestSettle()
        }
      })
  }

  /**
   * Whether the turn is over, decided from the server's own records, never
   * from an idle signal alone: the session is not busy, no ask of this
   * invocation is open, and the last message of the session is a completed
   * assistant message answering the admitted input. Until the settle window
   * after the first idle signal has passed, anything short of that is looked
   * at again.
   */
  async checkSettled() {
    const start = /** @type {NonNullable<typeof this.start>} */ (this.start)
    const statuses = await this.call("the session status", () => this.api().session.status({}))
    const status = isObject(statuses) ? statuses[start.sessionID] : undefined
    const busy = isObject(status) && status.type !== "idle"
    const openAsk = [...this.open.values()].some((ask) => ask.state === "open")
    const window = start.limits.settle_seconds * 1000
    const later = (/** @type {string} */ why) => {
      if (Date.now() - this.idleAt > window) {
        throw new BridgeFailure("completion", why)
      }
      if (this.settleTimer) clearTimeout(this.settleTimer)
      this.settleTimer = setTimeout(() => this.requestSettle(), 250)
      this.settleTimer.unref()
    }
    if (busy || openAsk) {
      // Looked at again until it is not: an idle signal that raced the status
      // must not leave the turn unobserved.
      this.idleAt = 0
      if (this.settleTimer) clearTimeout(this.settleTimer)
      this.settleTimer = setTimeout(() => this.requestSettle(), 1000)
      this.settleTimer.unref()
      return
    }
    const messages = await this.history(start)
    const index = messages.findIndex((m) => m.info?.id === start.inputID)
    if (index < 0) throw new BridgeFailure("completion", "the admitted input is not in the session's history")
    const after = messages.slice(index + 1)
    if (after.some((m) => m.info?.role !== "assistant")) {
      throw new BridgeFailure("completion", "another input was admitted to the session after this one")
    }
    const last = after.at(-1)
    if (!last) return later("the session settled without answering the admitted input")
    const info = last.info
    if (info.parentID !== start.inputID) throw new BridgeFailure("completion", "the last answer is not for the admitted input")
    if (!(isObject(info.time) && Number.isFinite(info.time.completed))) {
      return later("the last answer did not complete")
    }
    if (isObject(info.error)) {
      const name = clip(text(info.error.name), 80)
      if (this.aborting && name === "MessageAbortedError") {
        this.settle({ outcome: "interrupted" })
        return
      }
      const data = isObject(info.error.data) ? info.error.data : {}
      throw new BridgeFailure("model", `the answer ended with an error (${name || "unnamed"})`, {
        error: name,
        status: Number.isInteger(data.statusCode) ? data.statusCode : null,
        retryable: typeof data.isRetryable === "boolean" ? data.isRetryable : null,
      })
    }
    const finish = text(info.finish)
    if (finish === "tool-calls") return later("the session settled after a tool call, with no final answer")
    if (!ANSWER_FINISHES.includes(finish)) {
      throw new BridgeFailure("completion", `the answer ended for an unknown reason (${clip(finish, 40) || "none"})`)
    }
    if (this.aborting) {
      this.settle({ outcome: "interrupted" })
      return
    }
    const parts = Array.isArray(last.parts) ? last.parts : []
    const answer = parts
      .filter((p) => isObject(p) && p.type === "text" && p.synthetic !== true && p.ignored !== true)
      .map((p) => text(p.text))
      .join("\n")
    if (!answer.trim()) throw new BridgeFailure("completion", "the final answer has no text")
    this.settle({
      outcome: "result",
      result: {
        type: "result",
        session_id: start.sessionID,
        input_id: start.inputID,
        message_id: clip(text(info.id), 128),
        finish,
        text: answer,
      },
    })
  }

  /**
   * The session's messages from the admitted input on, read page by page
   * from the newest, never past `max_messages`: a history that does not reach
   * the input within the bound fails closed.
   * @param {NonNullable<typeof this.start>} start
   * @returns {Promise<any[]>}
   */
  async history(start) {
    /** @type {any[]} */
    let collected = []
    /** @type {string | undefined} */
    let before
    const page = 100
    for (;;) {
      /** @type {{sessionID: string, limit: number, before?: string}} */
      const query = { sessionID: start.sessionID, limit: page }
      if (before) query.before = before
      const read = await this.call("the message history", () => this.api().session.messages(query))
      const batch = Array.isArray(read) ? read.filter((m) => isObject(m) && isObject(m.info)) : []
      collected = [...batch, ...collected]
      if (batch.some((m) => m.info.id === start.inputID)) return collected
      if (batch.length < page || collected.length >= start.limits.max_messages) return collected
      const oldest = text(batch[0].info.id)
      if (!oldest || oldest === before) return collected
      before = oldest
    }
  }

  // -- the end ----------------------------------------------------------------------
  /** @param {{outcome: string, failure?: BridgeFailure, result?: EnvelopeRecord}} ended */
  async finish(ended) {
    this.finishing = true
    if (this.settleTimer) clearTimeout(this.settleTimer)
    let { outcome, failure, result } = ended
    if (result && !this.out.write(result)) {
      outcome = "failed"
      failure = new BridgeFailure("bounds", "the final answer is larger than the record bound")
    }
    const start = this.start
    if (outcome === "failed" && this.turnRunning && !this.aborting && this.client && start) {
      // A turn still running is stopped before the server is (D3.3, step 2).
      this.aborting = true
      await this.call("the abort", () => this.api().session.abort({ sessionID: start.sessionID })).catch(() => {})
    }
    this.stream.abort()
    const stopSeconds = start ? start.limits.stop_seconds : LIMITS.stop_seconds[2]
    const server = this.server ? await this.server.stop(stopSeconds * 1000) : { stopped: "never_started", exit_code: null, signal: null }
    const counts = {
      events: this.events,
      skipped_unknown: this.skippedUnknown,
      foreign_events: this.foreignEvents,
      asks: this.asks,
      sibling_settled: this.siblingSettled,
    }
    if (outcome === "failed" && failure) {
      this.out.write({
        type: "failure",
        code: failure.code,
        reason: clip(failure.message, MAX_REASON_CHARS),
        details: failure.details,
        server,
        ...counts,
      })
      process.exitCode = 1
    } else {
      this.out.write({ type: "end", outcome, server, ...counts })
      process.exitCode = 0
    }
    // Nothing of this bridge may keep it alive past its last record; stdout
    // is a pipe, written synchronously, so nothing is lost by exiting now.
    process.exit()
  }
}

/** @param {unknown} error @param {string} kind */
function staleOr(error, kind) {
  if (error instanceof BridgeFailure && error.details.status === 404) {
    return new BridgeFailure("interaction", `the ${kind} ask was settled before this bridge's reply reached it`)
  }
  return error
}

/** @param {number} ms */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

const bridge = new Bridge()
process.on("uncaughtException", (error) => {
  // Stop the runtime first, then one bounded failure record, then exit non-zero (D3.3, step 5).
  try {
    bridge.server?.proc.kill("SIGKILL")
  } catch {}
  bridge.out.write({ type: "failure", code: "internal", reason: `the bridge crashed (${clip(error?.name ?? "error", 60)})`, details: {} })
  process.exit(2)
})
bridge.run().catch((error) => {
  try {
    bridge.server?.proc.kill("SIGKILL")
  } catch {}
  bridge.out.write({ type: "failure", code: "internal", reason: `the bridge crashed (${clip(error?.name ?? "error", 60)})`, details: {} })
  process.exit(2)
})
