export const meta = {
  name: 'waystone-delegate-fanout',
  description: 'Carry a pre-decided waystone fan-out plan: lane-scheduled delegate-run legs, deterministic aggregation; verdict/apply stay in main',
  phases: [
    { title: 'Validate', detail: 'plan manifest integrity gate (no dispatch)' },
    { title: 'Dispatch', detail: 'lane-scheduled waystone delegate run legs' },
    { title: 'Aggregate', detail: 'deterministic collation of carrier reports' },
  ],
}

// ── contract (dev_docs/waystone-0.7-0.9-design.md §8.3 / ADR-0001) ───────────
// Main decided decomposition and packet boundaries BEFORE this run, via
// `waystone delegate plan ... --json`. This script only carries that manifest:
// it instantiates decided packets, bounds parallelism, and aggregates.
// It NEVER applies, discards, verdicts, verifies, or retries.
// Resume is unsupported: re-plan from disk and invoke fresh; --expect-packet-sha
// mechanically refuses stale dispatch. The return value is a non-authoritative
// carrier report — main re-derives facts from .waystone/delegations/<did>/.
//
// args: { plan: <waystone-fanout-plan-1 object>, width?: number (1..8, default 3),
//         notes?: {taskId: string}, waystoneBin?: string, validateOnly?: boolean }
// The host transport may deliver args JSON-stringified (measured live 2026-07-17);
// normalize before validating — a parse failure throws, never degrades.

let A = args
if (typeof A === 'string') {
  try { A = JSON.parse(A) } catch (e) { throw new Error(`args arrived as a non-JSON string: ${e}`) }
}
if (!A || typeof A !== 'object') throw new Error('args must be an object: {plan, width?, notes?, waystoneBin?, validateOnly?}')
let P = A.plan
if (typeof P === 'string') {
  try { P = JSON.parse(P) } catch (e) { throw new Error(`args.plan arrived as a non-JSON string: ${e}`) }
}
if (!P || P.schema !== 'waystone-fanout-plan-1')
  throw new Error('args.plan must be the object emitted by `waystone delegate plan ... --json`')

const SAFE_PATH = /^\/[A-Za-z0-9._/-]+$/            // no spaces/metacharacters — fail loud on exotic roots
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._/-]*$/
const SAFE_SHA = /^sha256:[0-9a-f]{64}$/
const SAFE_FPR = /^sha256:[0-9a-f]{12,64}$/
const SAFE_CID = /^[A-Za-z0-9][A-Za-z0-9.:_-]*$/
const SAFE_NOTE = /^[A-Za-z0-9 .,:;()/_-]+$/         // allowlist, single plain line

const root = String(P.root || '')
if (!SAFE_PATH.test(root)) throw new Error('plan.root must be an absolute path without spaces or shell metacharacters')
const cid = String(P.correlation_id || '')
if (!SAFE_CID.test(cid)) throw new Error('plan.correlation_id missing or unsafe')
if (!SAFE_FPR.test(String(P.profile_fingerprint || ''))) throw new Error('plan.profile_fingerprint missing or unsafe')
const routingNote = P.routing_note ? String(P.routing_note) : ''
if (routingNote && !SAFE_NOTE.test(routingNote)) throw new Error('plan.routing_note must be one plain allowlisted line')

const tasks = P.tasks || []
if (!tasks.length) throw new Error('plan.tasks is empty')
for (const t of tasks) {
  if (!SAFE_ID.test(String(t.task_id))) throw new Error(`unsafe task id: ${t.task_id}`)
  if (!SAFE_SHA.test(String(t.packet_sha256))) throw new Error(`missing/unsafe packet_sha256 for ${t.task_id}`)
  if (t.deps_ok !== true) throw new Error(`dependencies unsatisfied for ${t.task_id} — plan must gate deps before carry`)
}
if (new Set(tasks.map(t => t.task_id)).size !== tasks.length) throw new Error('duplicate task ids in plan')

const clerk = P.carrier && P.carrier.clerk
if (!clerk || !clerk.backend || !clerk.effort)
  throw new Error('plan.carrier.clerk backend/effort required — derived from profile, never guessed')
const fam = /^claude:(fable|opus|sonnet|haiku)/.exec(String(clerk.backend))
if (!fam) throw new Error(`clerk backend must be a claude model family for this carrier, got ${clerk.backend}`)
const EFFORT_MAP = { none: 'low', minimal: 'low', low: 'low', medium: 'medium', high: 'high', xhigh: 'xhigh' }
const leafModel = fam[1]
const leafEffort = EFFORT_MAP[String(clerk.effort)]
if (!leafEffort) throw new Error(`effort '${clerk.effort}' unsupported on the Claude carrier — refuse, never substitute`)

const notes = A.notes || {}
for (const k of Object.keys(notes)) {
  if (!SAFE_NOTE.test(String(notes[k]))) throw new Error(`notes[${k}] must be one plain allowlisted line`)
}
const bin = String(A.waystoneBin || 'waystone')
if (!/^[A-Za-z0-9._/-]+$/.test(bin)) throw new Error('unsafe waystoneBin')
const width = Math.max(1, Math.min(8, Math.floor(A.width ?? 3)))

// ── Validate: lane scheduling (deterministic, in-script) ─────────────────────
// Pairwise-disjoint declared scopes may share a parallel batch; overlapping or
// undeclared scope runs strictly sequentially. There is NO parallel override.
phase('Validate')
function normPrefix(p) { return String(p).replace(/\/+$/, '') + '/' }
function overlap(a, b) {
  const x = normPrefix(a), y = normPrefix(b)
  return x.startsWith(y) || y.startsWith(x)
}
const known = tasks.filter(t => (t.scope || []).length)
const unknown = tasks.filter(t => !(t.scope || []).length)
const batches = []
for (const t of known) {
  let placed = false
  for (const b of batches) {
    if (b.length < width && b.every(o => !o.scope.some(x => t.scope.some(y => overlap(x, y))))) {
      b.push(t); placed = true; break
    }
  }
  if (!placed) batches.push([t])
}
for (const t of unknown) batches.push([t])           // one per batch → sequential
if (unknown.length) log(`scope undeclared → sequential lanes: ${unknown.map(t => t.task_id).join(', ')}`)
log(`lane plan: ${batches.map(b => `[${b.map(t => t.task_id).join(', ')}]`).join(' → ')}`)

if (A.validateOnly) {
  return { schema: 'waystone-fanout-validate-1', ok: true,
           correlation_id: cid, batches: batches.map(b => b.map(t => t.task_id)) }
}

const LEAF_SCHEMA = {
  type: 'object', required: ['task_id', 'did', 'state', 'run_exit', 'summary'],
  properties: {
    task_id: { type: 'string' }, did: { type: ['string', 'null'] },
    state: { enum: ['needs-review', 'failed-env', 'failed-runner', 'failed-artifact', 'running',
                    'discarding', 'corrupt', 'claimed', 'refused', 'no-record', 'unknown'] },
    run_exit: { type: ['integer', 'null'] },
    changed_file_count: { type: ['integer', 'null'] },
    changed_paths: { type: 'array', items: { type: 'string' }, maxItems: 200 },
    changed_paths_truncated: { type: ['boolean', 'null'] },
    patch_empty: { type: ['boolean', 'null'] }, patch_sha256: { type: ['string', 'null'] },
    delegate_report: { enum: ['present', 'absent', 'invalid', null] },
    base_sha: { type: ['string', 'null'] },
    warnings: { type: 'array', items: { type: 'string' }, maxItems: 20 },
    failure_tail: { type: ['string', 'null'], maxLength: 2000 },
    summary: { type: 'string', maxLength: 400 },
  },
}

function leafPrompt(t) {
  const noteFlag = notes[t.task_id] ? ` --note "${notes[t.task_id]}"` : ''
  const routeFlag = routingNote ? ` --routing-note "${routingNote}"` : ''
  return `You are a mechanical executor for one waystone delegation. Task: "${t.task_id}". Root: "${root}".
Follow these steps EXACTLY. Do not improvise, do not fix problems, do not clean anything up.
HARD PROHIBITIONS: never run delegate apply / verdict / verify / discard; never edit files;
never start the run command more than once.

STEP 1 — start the run in the background, exactly once:
Use the Bash tool with run_in_background=true (the run can exceed an hour; a foreground call
times out) on this single command:
  ${bin} delegate run "${t.task_id}" --root "${root}" --expect-packet-sha ${t.packet_sha256} \
    --expect-profile ${P.profile_fingerprint} --carrier claude-workflow \
    --carrier-instance ${cid} --json-events${noteFlag}${routeFlag} </dev/null
If you are re-invoked and a run was already started, do NOT start another; continue with STEP 2.

STEP 2 — wait. The background command re-invokes you when it exits. Do not poll, do not sleep.
Record its exit code as run_exit.

STEP 3 — parse stdout as NDJSON events:
- the "claimed" event gives the authoritative delegation id (did).
- the "finished" event gives state, artifact path, base_sha, changed_file_count, patch_sha256,
  patch_empty, delegate_report_present.
- If there is a "claimed" event but no "finished" event, run:
  ${bin} delegate status "<did>" --json --root "${root}"   and take state from it.
- If there is no "claimed" event at all: did = null, state = "no-record" (a pre-claim refusal);
  put the last 15 stderr lines into failure_tail.

STEP 4 — transcription (only when state is "needs-review"):
Run: ${bin} delegate show "<did>" --report --root "${root}" and transcribe from the contract
YAML: up to 200 changed_files "path" values into changed_paths; set changed_paths_truncated =
true when changed_file_count exceeds what you listed. If state starts with "failed", run
${bin} delegate show "<did>" --failure --root "${root}" and put the last 30 lines in failure_tail.

STEP 5 — return JSON per the schema. warnings = every stderr line starting with "waystone warn"
(max 20). summary = one factual sentence (state, file count). Report facts only; never judge
whether the work is acceptable.`
}

// ── Dispatch: sequential batches, parallel within a batch, keyed by index ────
phase('Dispatch')
const results = []
for (let i = 0; i < batches.length; i++) {
  const batch = batches[i]
  log(`dispatching lane ${i + 1}/${batches.length}: ${batch.map(t => t.task_id).join(', ')}`)
  const settled = await parallel(batch.map(t => () =>
    agent(leafPrompt(t), { label: `run:${t.task_id}`, phase: 'Dispatch',
                           model: leafModel, effort: leafEffort, schema: LEAF_SCHEMA })))
  settled.forEach((r, k) => {
    const t = batch[k]                                // index-keyed: reported ids never re-key results
    if (!r) {
      results.push({ task_id: t.task_id, did: null, state: 'unknown', run_exit: null,
                     changed_paths: [], warnings: [], failure_tail: null, suspect: true,
                     summary: 'leaf agent failed or was skipped' })
      return
    }
    const suspect = r.task_id !== t.task_id
    results.push({ ...r, task_id: t.task_id, suspect,
                   warnings: suspect
                     ? [...(r.warnings || []), `leaf reported task_id ${r.task_id} — re-keyed by dispatch index`]
                     : (r.warnings || []) })
  })
}

// ── Aggregate: deterministic collation ───────────────────────────────────────
phase('Aggregate')
const scopeOf = new Map(tasks.map(t => [t.task_id, t.scope || []]))
const conflicts = []
for (let i = 0; i < results.length; i++) for (let j = i + 1; j < results.length; j++) {
  const a = results[i], b = results[j]
  if (a.changed_paths_truncated || b.changed_paths_truncated) {
    conflicts.push({ tasks: [a.task_id, b.task_id], status: 'unknown',
                     reason: 'changed_paths truncated — main must re-derive from contract.yaml' })
    continue
  }
  const shared = (a.changed_paths || []).filter(p => (b.changed_paths || []).includes(p))
  if (shared.length) conflicts.push({ tasks: [a.task_id, b.task_id], status: 'conflict', paths: shared.slice(0, 20) })
}
const scopeViolations = results
  .filter(r => (r.changed_paths || []).some(p => {
    const s = scopeOf.get(r.task_id) || []
    return s.length && !s.some(pref => (p + '/').startsWith(normPrefix(pref)) || p === pref)
  }))
  .map(r => r.task_id)
const needsAttention = results
  .filter(r => r.state !== 'needs-review' || r.suspect || scopeViolations.includes(r.task_id))
  .map(r => ({ task_id: r.task_id, did: r.did, state: r.state,
               suspect: !!r.suspect, scope_violation: scopeViolations.includes(r.task_id) }))
log(`fan-out complete: ${results.length - needsAttention.length}/${results.length} clean needs-review; `
  + `${conflicts.length} conflict entries; ${needsAttention.length} need main triage`)
return {
  schema: 'waystone-fanout-result-1',
  correlation_id: cid, root,
  results,                          // non-authoritative carrier report — pointers, not evidence
  conflicts,                        // 'unknown' entries mean main must re-derive from contract.yaml
  needs_attention: needsAttention,  // failed-*/refused/suspect/scope-violation → main triage
  protocol: 'verdict/apply are main-session actions, strictly serial; re-check '
    + '`waystone delegate status <did> --json` and the on-disk record before acting '
    + 'on any state reported here; never resume this run — re-plan and invoke fresh',
}
