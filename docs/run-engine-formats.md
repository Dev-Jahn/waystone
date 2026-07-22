# Run engine v2 표현/형식 계약 registry

**Status:** v2 canonical surface — 0.13 C2 cutover. v1 runtime input is refused; migration is a
separate one-time operation and is not a compatibility path.
**소유 결정:** `decision/run-engine-format-pinning-batch` (각 기체의 v1 표현을 gate에서 명문화, 이후 변경은 migration 경유 강제)
**원천:** canonical v2 `ProjectContext`, `ProjectFrame`, `WorkBrief`, `AssurancePlan`, `RunSpec`,
`OutcomeDelta`, and the preserved M1-B kernel.

## 0.13 v2 surface

`RunSpec` freezes one `lifecycle_stage` (`explore|evaluate|promote`), the used fact/owner-source
references, semantic WorkBrief digest, CompletionContract, AssurancePlan, and revision. Objective
progress is published as one typed OutcomeDelta through `run close`; status reads the objective,
stage, waiting-context, and latest delta before audit counts. No v1 name or legacy CLI alias is
accepted as v2 success.

## 0. 변경 규칙 (binding)

이 문서에 고정된 어떤 이름·형식·enum 값도, 이후 변경 시 **반드시 store migration registry**(`store.py`의 `_MIGRATIONS` + `SCHEMA_VERSION` 승격)를 경유하고 **이 문서의 개정을 동반**해야 한다. 임의 in-place 변경, silent 확장, wire 표현의 조용한 재해석은 금지. §10의 "미고정" 항목은 아직 이 규칙의 대상이 아니다(고정 전까지는 자유롭게 결정 가능).

## 1. Identity

- **run_id**: RFC 9562 UUIDv7, **canonical lowercase**. 48-bit Unix-ms 필드 + 74 CSPRNG bits(`secrets.randbits`), version nibble `0b0111`, variant `0b10`. 정규식 `[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}`, `parsed.version == 7` 재검증. UNIQUE 충돌은 bounded retry (`generate_run_id`, `_validate_run_id`, `_RUN_ID_PATTERN` in store.py).
- **job_id**: `<run-uuid>:job` (spec.py `f"{run.run_id}:job"`). one task = one run = one job. store가 job UUID 생성기를 주지 않아 run UUID에서 파생.
- **entity_kind**: 닫힌 enum `run` | `job` | `attempt` | `action` (`EntityKind`).
- **attempt_id / action_id**: project-global primary key 문자열. store에 canonical generator가 없어 caller가 발급 (§10 미고정). cleanup action은 deterministic prefix `cancel-cleanup-...` (cancel.py).
- **lease_id**: `lease_id == action_id` (deterministic, lease.py; lease 보고 ④5).
- **entity version 규약**: 초기 **0** (`version=0`, 최초 transition reason 필수 `created`, `prev_state = None`). 성공 transition마다 **+1** — transition audit chain 길이 = `version + 1`, CAS는 `expected_version == current.version` exact match (`record_transition`, `_validate_transition_chain`).

## 2. Canonical 인코딩

- **JSON (store-side, 대다수)**: `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")` — sorted-key, compact, **raw UTF-8 (비-ASCII 미이스케이프)**. 적용: store, spec, preflight, effects, verify, supervisor, cancel (동일 4-인자 형태).
- **JSON (transport wire, 예외)**: `ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False` — **ASCII escaping (비-ASCII → `\uXXXX`)**. 즉 store-side와 **다르다**. non-UTF-8 Git pathname은 `surrogateescape`로 str화 후 ASCII-escape 왕복 (transport.py `_canonical_bytes`).
- **digest 표기**: `sha256:<64 lowercase hex>`. 정규식 `sha256:[0-9a-f]{64}` (`_DIGEST_PATTERN`, `validate_sha256_digest`). 산출: `content_hash = hashlib.sha256(payload).hexdigest()` → `f"sha256:{...}"` (core.content_hash).
- **canonical artifact 저장 경로**: `.waystone/artifacts/sha256-<hex>` (content-addressed, atomic publish + 재해시, artifacts.py).

## 3. Artifact reference id 문법 (전 네임스페이스)

`reference_kind`는 `ArtifactReferenceKind` 닫힌 enum: `attempt` | `evidence` | `decision` (artifacts.py). 아래 reference id는 **integration-decision만 `decision`, 나머지 전부 `evidence`** 로 결속된다. (`attempt` kind는 store-kernel 소유이며 이 slice의 reference id 문법으로는 사용되지 않음.)

| reference id 문법 | kind | 소유 모듈 |
|---|---|---|
| `run-spec:<run_id>` | evidence | spec |
| `base-snapshot:<run_id>` | evidence | spec |
| `verification-plan:<run_id>` | evidence | preflight |
| `verification-preflight:<run_id>` | evidence | preflight |
| `preflight-receipt:<run_id>:<hex>` (hex = `sha256:` 접두 제거) | evidence | preflight |
| `effect-plan:<action_id>` | evidence | effects (store/transport/verify 공유) |
| `effect-intent:<action_id>` | evidence | effects |
| `effect-observation:<action_id>:<digest_suffix>` | evidence | effects, verify |
| `engine-check-evidence:<action_id>` | evidence | verify |
| `verifier-evidence:<action_id>` | evidence | verify |
| `integration-decision:<action_id>` | **decision** | verify |
| `runner-invocation:<lineage_key>:<action_id>` (lineage_key = `sha256:<hex>` — runner at-most-once/retry authority) | evidence | effects |
| `transport-action-plan:<action_id>` | evidence | transport |
| `transport-result:<action_component>` (action_component = sha256hex(action_id)) | evidence | transport |
| `transport-result:<action_component>:artifact:<name_component>` | evidence | transport |
| `cancellation-intent:<run_id>` | evidence | cancel |
| `cancellation-terminal:<run_id>` | evidence | cancel |
| `cancellation-cleanup-plan:<cleanup_action_id>` | evidence | cancel |

canonical JSON payload의 `"schema"` 태그(버전 동봉): `waystone-run-spec-1`, `waystone-run-base-snapshot-1`, `waystone-verification-plan-1`, `waystone-verification-preflight-1`, `waystone-runner-proof-1`, `waystone-effect-plan-1`, `waystone-effect-intent-1`, `waystone-effect-observation-1`, `waystone-runner-completion-1`, `waystone-verifier-evidence-1`, `waystone-engine-check-evidence-1`, `waystone-integration-decision-1`, `waystone-integration-decision-intent-1`, `waystone-verification-transcript-1`, `waystone-cancellation-intent-1`, `waystone-cancellation-terminal-1`, `waystone-cancellation-cleanup-plan-1`,
`waystone-transport-action-plan-1`, `waystone-transport-result-1`.

supervisor sidecar receipt 파일(CAS artifact 아님 — reference id 없음, canonical payload
schema tag만): `waystone-supervisor-launch-1`, `waystone-supervisor-runtime-1`,
`waystone-supervisor-heartbeat-1`, `waystone-supervisor-wait-1`.

정정 이력 (2026-07-21, WS-GPT-606): 초판이 누락한 runner-invocation·transport 3행과
supervisor schema 4종을 추가하고, artifact reference가 아닌 `fixture-verification:<action_id>`
행(completion marker의 process_identity 문자열 — fixture 하네스 전용)을 삭제했다.

## 4. TransitionReason v1 enum (전수)

`store.py` `TransitionReason(str, Enum)` — **정확히 7값, 닫힘**:

| 값 | 소유/발행 단계 |
|---|---|
| `created` | 최초 entity 생성 (version 0 필수 reason) |
| `planned` | RunSpec/VerificationPlan freeze, planned job/effect action |
| `claimed` | lease claim |
| `process-started` | effect 단계 진입 (generic effect·runner에 재사용), cleanup-executing |
| `effect-observed` | effect observed → completed 전이 |
| `completed` | 완료 (effect / submit / cleanup-completed) |
| `cancel-requested` | cancel intent 기록 + terminal cancellation |

명시적 부재(설계된 결정): `stopping`/`canceled`/`cleanup-*` 전용 reason 없음, `effect-unknown`/`effect-conflict`/`blocked` reason 없음. 이런 의미는 **run/action state 문자열**이 보존하고 reason은 위 7값을 재사용한다 (cancel 보고 ④3·4, effects 보고 ④2, transport 보고 ④2).

## 5. Lease principal + fencing epoch + typed error

- **principal triple** (`LeasePrincipal.cas_tuple`): `(owner_token, fencing_epoch, entity_version)` — 6개 guard entry(heartbeat renew·effect start·submit·completion·apply·cleanup) 전부에서 exact match 검사. 전체 필드: `run_id, action_id, owner_token, fencing_epoch, entity_version, monotonic_deadline`.
- **owner_token**: CSPRNG(`secrets`), release 후 재사용 안 함.
- **fencing_epoch**: 단조 증가, release 이후에도 보존, 재사용 금지. 상한 `_MAX_FENCING_EPOCH = (1 << 63) - 1`; 도달 시 wrap 없이 `fencing_epoch_exhausted` refusal.
- **expiry**: DB `expires_at`은 telemetry/liveness hint 전용(wall clock) — claim/takeover/mutation 권한 아님. in-process 판정은 `time.monotonic()` 기준 `monotonic_deadline`만 사용.
- **typed error code 목록** (lease.py): `lease_error`(base), `lease_principal_mismatch`, `lease_principal_unknown`, `lease_already_claimed`, `lease_reclaim_refused`, `lease_state_error`, `fencing_epoch_exhausted`, `lock_busy`, `lock_principal_unknown`.

## 6. Runner proof 7축 · process identity · completion marker

- **Runner proof bounded 관측 7축** (preflight.py, fixed-source): `cache-boundary`, `platform-kernel`, `process-security`, `runner-binary`, `runner-config-content`, `runner-version`, `sandbox-contract`. state-equival 재사용용 sentinel `not-observed`. checkout/machine/principal identity와 config content는 별도 digest 축(7축에 미포함). schema `waystone-runner-proof-1`.
- **ProcessIdentity 필드 집합** (supervisor.py, `from_payload` exact 8-key): `host_boot_identity`, `pid`, `process_start_token`, `action_id`, `supervisor_owner_token`, `fencing_epoch`, `resolved_executable`, `invocation_digest` — 마지막 둘 중 **최소 하나는 non-null 필수**. (Linux: boot UUID + `/proc/<pid>/stat` start ticks; macOS: `kern.boottime` + `libproc` start; PID-only fallback 없음.)
- **RunnerCompletionMarker 필수 필드** (effects.py, schema `waystone-runner-completion-1`): `run_id`, `job_id`, `action_id`, `fencing_epoch`(≥1), `launch_token`, `process_identity`, `started_at`, `finished_at`, `returncode`, `signal`(returncode/signal 중 **정확히 하나만** non-null), `stdout_artifact_digest`, `stderr_artifact_digest`(둘 다 `sha256:` digest). stdout/stderr bytes 자체는 CAS artifact, marker에는 digest만.

## 7. Transport wire

- 인코딩: §2의 **ASCII-escaped** canonical JSON. `encode_envelope` / `decode_envelope`.
- **Failure envelope**: exact key set `{"ok": false, "code": <TransportFailureCode>, "recoverable": <bool>, "next_actions": ...}`.
- **Submit success envelope**: `{"action_id", "ok": true, "result_digest", "state"}`.
- **`actions_next` 3분기 반환 형태**:
  - outward action: `{"action": {"action_id","action_kind","entity_version","executor_kind","fencing_epoch","input","input_digest","ownership","result_schema"}}` — `executor_kind ∈ {carrier, user}`, `ownership = {"expires_at", "kind":"engine-claim"}`.
  - busy: `{"action": null, "engine": "busy", "poll_after_s": <양수 int>, "run_state": <str>}`.
  - idle: `{"action": null, "engine": "idle", "reason": <IdleReason>, "run_state": <매핑값>}`.
- **TransportFailureCode** (str enum): `transport_error`, `action_not_current`, `input_digest_mismatch`, `fencing_epoch_mismatch`, `result_schema_mismatch`, `artifact_digest_mismatch`, `git_facts_mismatch`, `action_plan_invalid`, `run_not_actionable`, `engine_executor_unavailable`, `engine_test_evidence_invalid`, `transient_transport_failure`, `unclassified`.
- **IdleReason** (str enum): `run_completed`→run_state `completed`, `run_waiting_user`→`waiting_user`, `run_blocked`/`effect_unknown`/`effect_conflict`→`blocked`.
- **TransportExitCode** (IntEnum): `OK=0`, `UNCLASSIFIED=1`, `REFUSED=2`, `TEMPORARY_FAILURE=75`. 매핑: transient/recoverable→75, terminal 계약 거부→2, 분류 불능→1, 성공→0.

## 8. Permission mode 계약

`os.open`에 `O_CREAT|O_EXCL|O_NOFOLLOW`로 신규 생성, 명시 mode 부여. 기존 object는 자동 `chmod`/`chown` 없이 semantic 검사 후 typed refusal(`unsafe_state_permissions` 등).

| 대상 | mode | 상수 |
|---|---|---|
| state directory | `0700` | `_STATE_DIRECTORY_MODE` (store) |
| DB `state.db` / `-wal` / `-shm` / advisory lock leaf | `0600` | `_MUTABLE_STATE_FILE_MODE` (store/lease) |
| `.waystone/` 및 `artifacts/` directory | `0700` | `_ARTIFACT_DIRECTORY_MODE` (artifacts) |
| artifact staging leaf | `0600` | `_STAGING_FILE_MODE` (artifacts) |
| final artifact | `0400` (publish 전 `fchmod`) | `_FINAL_ARTIFACT_MODE` (artifacts) |

주: final `0400`과 기존 corruption fixture의 직접 overwrite 충돌은 main ruling으로 해소 완료 — 주입 방식을 명시적 chmod 변조 시뮬레이션으로 갱신(계약 단언 불변, dev `bc36f2d`·`7cfe763`).

## 9. 명시적 비고정 (이 registry가 아직 고정하지 않는 것)

각 항목은 `decision/run-engine-format-pinning-batch` 미확정분. 고정 전까지 §0 변경 규칙 미적용.

1. **attempt_id / action_id 문법** 자체 — store canonical generator 부재, project-global vs run-scoped child ID scope 미결(store 보고 ④2, lease 보고 ④5).
2. **blocked reason vocabulary** — `effect-unknown`/`effect-conflict`/`stopping`/`canceled`/`cleanup-*` 전용 TransitionReason 신설 여부(현재 state 문자열로만 보존).
3. **evidence digest cardinality/equality** — `transitions.evidence_digest` ↔ `evidence` reference의 exactly-one/at-least-one/독립 receipt 판정(store 보고 ④1, verify 보고 ④9): 현재 digest canonical form과 immutability만 강제.
4. **lease principal의 `project_id` / `executor_kind` 축** — store v1 schema에 column 없음(lease 보고 ④1).
5. **pending cancellation reason 3분** — `identity-unknown` / `liveness-unknown` / `unknown-effect` 어휘의 계획 vs ADR-0003 충돌(cancel 보고 ④3).
6. **RED artifact stdout/stderr 상세 형식**, verifier transcript 상세 shape(preflight ④7, verify ④6).
7. **transport `invocation_digest` ↔ preflight `prepared_input_digest` bridge**(transport ④3), generic outward action planning surface·store executor binding(transport ④5).
8. **CAS orphan bytes GC / mark-root reachability** — DB reference 없는 immutable bytes 정리(공통 ④, M1-C+ 범위).

---
## 코드에서 확인 못 해 제외한 항목

- **`profile_v1` refusal code**(`profile_v1_unreadable` 등)와 non-role 표식(`main`/`orchestrator`/`clerk`) 구체 타입명 — domain 보고 ④에만 있고 이 문서 범위(run engine 형식)와 별개 계층이라 제외.
- **base-snapshot 내부 인코딩 상세**(path/content base64, deletion tombstone, Git mode canonicalize) — spec 보고 ④6 서술은 있으나 §형식 registry 수준의 필드 스키마는 코드에서 전수 대조하지 않아 미기재.
- **`ActionResultSchema` / `ResultField` / `ResultValueKind`의 필드별 result 스키마 상세**(transport.py) — enum 존재만 확인, 각 action-kind별 result field 목록은 미전수.
- **cancel signal preflight/틱 관측 필드**·supervisor sidecar/wait receipt 스키마 필드 전수 — 이름(`cancel-signal-preflight` 등) 확인, 필드 집합은 미전수.
