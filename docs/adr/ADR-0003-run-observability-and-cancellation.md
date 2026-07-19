# ADR-0003: run 관측과 취소를 긍정적 증거에 결속한다

- Status: accepted
- Date: 2026-07-19
- Round: —
- SSOT sections affected: 없음 — 0.12 계획서 §3-8·§3-9·§3-10과 E-08을 확정·정밀화
- Tasks: docs/adr-runtime-core-contracts

## Context

진행 중인 run에서 “살아 있는가”, “어디까지 갔는가”, “지금 무엇을 하는가”는 서로 다른
질문이며 증거원도 다르다. 2026-07-19 운영에서는 출력 침묵을 종료로 오판해 살아 있는
delegation을 정리하려 했고, 장시간 유지된 `record.lock`이 우연히 destructive cleanup을 막았다.
0.12는 이 lock을 DB claim, lease, fencing, 짧은 OS lock으로 분해하므로 우연한 보호도 사라진다.
계획서 §3-8~§3-10과 E-08은 세 관측 축, 양방향 종료 규칙, derived health, read-only 조회,
supervisor 소유권과 취소 안전 원칙을 확정했다. 계획서가 ADR에 남긴 cancel·quiescence·cleanup의
정확한 분기표와 불완전한 process identity의 처리 방법은 이 문서에서 결정한다.

## Decision

### Liveness, progress, current를 분리한다

`run status`와 `run watch`는 다음 세 필드를 독립적으로 계산한다. 한 필드의 부재나 unknown을
다른 필드의 값으로 메우지 않는다.

| 축 | 질문 | 권위 증거 | honest-unknown 계약 |
|---|---|---|---|
| `liveness` | 살아 있는가 | action별 관측 계약이 주는 긍정적 신호. runner에서는 supervisor가 같은 process identity를 대조한 실행 관측과 그 identity에 결속된 heartbeat freshness를 사용한다. | `unknown(reason=...)`. heartbeat 부재나 로그 침묵을 `exited`로 바꾸지 않는다. |
| `progress` | 어디까지 갔는가 | frozen closure를 분모로 한 task/job의 authoritative transition 집계 | closure 또는 상태를 읽을 수 없으면 `unknown-progress(reason=...)`와 마지막 확정 지점만 보인다. 미확정 작업을 완료로 추정하지 않는다. |
| `current` | 지금 무엇을 하는가 | 현재 claimed action의 `action_kind`와 `claimed_at`; multi-job run은 job별 current를 집계 | 현재 claim을 확정할 수 없으면 `unknown-current(reason=...)`. 최근 로그 문구로 대신하지 않는다. |

worker의 free-text, stdout, stderr는 세 축 어느 것의 권위 증거도 아니다. worker 보고는 claim이며,
supervisor가 보존한 process 관측과 엔진 transition만 harness fact다.

### E-08은 양방향 규칙이다

출력 부재, 로그 미기록, stderr 오류 누적, heartbeat 부재는 종료 증거가 아니다. 반대로 다음 중
하나가 action과 동일 process identity에 결속되어 긍정적으로 관측되면 종료 증거다.

- supervisor가 회수한 wait status
- action에 결속된 atomic completion marker
- 같은 process identity의 종료를 증명하는 OS/supervisor 관측

긍정적 실행 관측이 있으면 `alive`, 긍정적 종료 관측이 있으면 `exited`라고 답한다. 둘 다 없거나
identity를 대조할 수 없으면 사유를 동반한 `unknown`이다. host boot identity 불일치나 PID 재사용
가능성은 `exited`가 아니라 `unknown(identity-mismatch)`다. stale heartbeat라도 같은 child process가
살아 있음을 긍정적으로 관측했다면 종료로 판정하지 않으며 cleanup도 허용하지 않는다.

### `stalled`는 FSM state가 아닌 derived health다

`runs` current-state row의 `run_state`와 read-time `health`를 분리한다. heartbeat freshness처럼
시간의 경과만으로 바뀌는 값을 authoritative transition으로 저장하지 않는다.

```json
{"run_state":"running","health":"unknown","health_reason":"heartbeat-stale-process-observation-unavailable"}
```

`stalled`는 오직 **다음 progress를 만들 수 있는 action이 하나도 없고, 그 원인이 아직 해소되지
않은 `unknown-effect`인 경우**에만 계산되는 health다. `failed`, `waiting_user`, `canceled`,
`completed` 같은 FSM transition은 `run resume`이 external effect reconciliation을 마친 뒤에만
기록한다. 조회 시점에 health가 달라져도 transition이나 entity version은 바뀌지 않는다.

### Liveness와 progress 집계

liveness는 action 및 job 단위로 먼저 계산한 뒤 run에 집계한다. engine supervisor, job별 runner,
carrier, 아직 대기 중인 job의 상태를 각각 보존한다. 일부 job이 `unknown`이어도 독립 job이 진행할
수 있으면 run은 `running`이며 `degraded` 또는 `attention_required` health를 함께 보일 수 있다.
한 lane의 unknown을 run 전체의 exited 또는 stalled로 축약하지 않는다.

progress의 고정 분모는 run planning 때 확정된 **frozen closure tasks**다. 분자는 그 closure에
대응하여 authoritative transition으로 확인된 `accepted|terminal` job/task 수이며, 상태별 count를
우선 표시한다. retry, verification, reconciliation 때문에 동적으로 늘어나는 action 수는 분모로
쓰지 않고 현재 phase 설명에만 쓴다. frozen closure를 읽을 수 없으면 백분율이나 임의 분모를
만들지 않고 마지막으로 확정된 count와 `unknown-progress`를 표시한다.

```text
Tasks: 3/5 completed          Jobs: 1 running · 1 waiting
Wave: 2/3                     Current: verifying task-x · 4m 12s
Last confirmed transition: 38s ago
```

### `status`와 `watch`는 엄격한 read-only projection이다

`run status`와 `run watch`는 같은 projection을 각각 1회 또는 반복 렌더링할 뿐이다. 두 명령은:

- lease나 heartbeat를 갱신하지 않는다.
- action을 claim하거나 owner/fencing epoch를 바꾸지 않는다.
- reconcile, retry, signal, cleanup을 실행하지 않는다.
- state transition, entity version, audit row를 쓰지 않는다.
- cache migration이나 artifact repair를 실행하지 않는다.

읽기 실패는 전체 `status-unavailable` 또는 영향을 받은 field의 typed `unknown`으로 드러낸다.
조회 호출 자체가 liveness evidence가 되거나 run을 살아 있는 것처럼 보이게 해서는 안 된다.

heartbeat는 audit transition이 아니라 최신 값을 덮어쓰는 local mutable telemetry다.

```text
leases/action_runtime  = latest heartbeat mutable telemetry
transitions            = planned · claimed · process-started · effect-observed ·
                         completed · cancel-requested 같은 의미 있는 변화만 append
```

### 취소, quiescence, cleanup 안전 계약

`run cancel`은 실행 종료가 아니라 **취소 의도**만 기록한다. `running` action, 긍정적으로 `alive`인
process, `unknown-effect`, `cancel-pending` 중 하나라도 해당하면 관련 worktree, ref, artifact를
삭제하지 않는다. lease 만료와 heartbeat 부재만으로 signal, terminal cancellation, cleanup을
허용하지 않는다.

```text
running → cancel-requested → stopping → canceled
running → cancel-requested → cancel-pending(reason=unknown-effect)
```

이 ADR에서 `observed-quiescent`는 다음 사실이 모두 긍정적으로 확인된 상태로 결정한다.

1. 해당 action에 결속된 모든 process가 시작되지 않았거나 종료됐음을 supervisor/OS가 verified
   process identity로 관측했다.
2. 진행 중일 수 있는 external effect가 없고, 이미 발생한 effect는 관측·reconcile되어 결과가
   current-state row에 결속됐다.
3. 현재 fencing epoch와 entity version이 확정되어 stale worker의 늦은 submit이 CAS로 거부된다.

정확한 취소 분기는 다음과 같다. 이 표가 계획서에서 ADR에 남긴 truth table을 확정한다.

| 현재 관측 | `run cancel`의 기록 | Signal | Terminal cancellation | Cleanup |
|---|---|---|---|---|
| 같은 identity의 process가 `alive` | `cancel-requested`, 이후 `stopping` | engine-owned supervisor가 verified identity에만 전송 | wait status를 긍정적으로 관측하고 effect를 reconcile한 뒤에만 `canceled` CAS | `observed-quiescent` 전 금지 |
| `running`이지만 process identity를 검증할 수 없음, PID/start/boot identity 불일치 | `cancel-requested` 후 `cancel-pending(reason=identity-unknown)` | 금지 | 금지 | 금지 |
| heartbeat stale 또는 lease expired이고 process 관측 채널도 불가 | `cancel-requested` 후 `cancel-pending(reason=liveness-unknown)` | 금지 | 금지 | 금지 |
| effect 유무 또는 종료 결과가 불명확한 `unknown-effect` | `cancel-requested` 후 `cancel-pending(reason=unknown-effect)` | verified process identity가 없으면 금지 | reconcile 전 금지 | 금지 |
| process의 종료는 긍정적으로 관측했으나 effect/DB commit이 미reconcile | 취소 의도 유지 | 불필요 | `run resume`이 effect를 reconcile하기 전 금지 | reconcile 및 `observed-quiescent` 전 금지 |
| 모든 process/effect가 reconcile되어 `observed-quiescent` | 취소 의도를 현재 entity version에 CAS | 불필요 | `canceled`로 CAS 가능 | terminal cancellation 뒤 별도 idempotent cleanup action으로만 허용 |

signal은 identity가 완전히 검증된 process에만 보낸다. cleanup은 cancellation transition에 섞지 않고
별도 idempotent action으로 실행한다. compatibility `delegate discard`와 crash 잔여물 정리도 이
cancel/reconcile 경로를 우회할 수 없다. `unknown` 또는 running effect는 destructive resolution의
근거가 될 수 없다는 규칙은 E-08의 안전 귀결이며, traceability matrix에는 독립 행으로 둔다.

### Process supervision과 identity

PID 단독 대조는 PID 재사용을 구분하지 못하므로 process identity의 최소 집합을 다음으로 고정한다.

| 필드 | 목적 |
|---|---|
| `host_boot_identity` | 재부팅 전후의 PID·monotonic 시간 영역을 구분 |
| `pid` | 현재 host의 process locator |
| `process_start_token` 또는 검증 가능한 process start time | 같은 boot 안의 PID 재사용 구분 |
| `action_id` | process를 immutable action input과 결속 |
| `supervisor_owner_token`과 `fencing_epoch` | 관측·submit을 현재 engine owner와 결속 |
| `resolved_executable` 또는 `invocation_digest` | 의도한 실행과 실제 child invocation을 결속 |

최소 집합을 완성하거나 현재 관측과 일치시킬 수 없으면 process identity는 검증되지 않은 것이며
`unknown(identity-incomplete|identity-mismatch)`로 답한다. signal이나 cleanup을 위한 PID fallback은
두지 않는다.

authoritative process telemetry의 writer는 worker, model, carrier가 아니라 **engine-owned
supervisor**다. supervisor만 heartbeat를 갱신하고, child identity와 wait status를 보존하고,
stdout/stderr artifact의 bytes와 digest를 확정하고, atomic completion marker를 쓴다. worker가 쓴
heartbeat, completion 문구, exit 주장은 claim일 뿐 이 필드를 대체하지 못한다.

completion marker의 최소 필드는 다음과 같다.

```text
action_id · fencing_epoch · process_identity · started_at · finished_at ·
returncode|signal · stdout_artifact_digest · stderr_artifact_digest
```

heartbeat freshness의 duration 계산은 wall clock이 아니라 같은 `host_boot_identity` 안의 monotonic
clock으로 한다. boot identity가 다르면 이전 monotonic 값은 비교하지 않고 identity mismatch로
처리한다. wall time은 사용자 표시용 `started_at`·`finished_at`과 진단에만 사용하며 freshness나
cleanup 권한의 근거가 아니다.

## Consequences

- 사용자는 로그 파일, PID, worktree 경로를 뒤지지 않고도 확정 가능한 값, unknown 사유, 마지막
  확정 transition을 구분해 볼 수 있다.
- `status`와 `watch`를 반복 호출해도 run의 version, lease, heartbeat, transition은 변하지 않는다.
- 관측 채널 장애나 identity mismatch 시 취소가 즉시 끝나지 않고 `cancel-pending`에 머물 수 있다.
  이는 살아 있는 실행과 산출물을 지우지 않기 위한 의도된 fail-direction이다.
- supervisor가 engine 소유 구성요소가 되며 adapter는 process identity, wait status, atomic marker,
  monotonic heartbeat를 제공해야 한다.
- fault injection은 stale heartbeat+live child, PID/boot mismatch, exit 후 DB commit 전 kill,
  `unknown-effect` cancel, 반복 status/watch의 무변경을 각각 검증해야 한다.

## Alternatives considered

- **로그 또는 heartbeat 침묵을 exited로 해석** — 관측 불가와 종료를 혼동해 살아 있는 실행을
  정리할 수 있으므로 기각.
- **`stalled`를 authoritative FSM state로 저장** — 시간 경과나 조회가 state transition을 만들고
  audit를 오염시키므로 기각.
- **action 수를 progress 분모로 사용** — retry와 reconciliation이 action을 늘려 진행률 의미가
  변하므로 기각.
- **cancel이 즉시 process kill과 cleanup을 수행** — identity 불명 또는 effect in-flight에서 다른
  process와 복구 증거를 파괴할 수 있으므로 기각.
- **worker가 자기 heartbeat와 completion을 authoritative하게 기록** — 관측 대상이 자기 생존과
  완료를 증명하는 순환이 생기므로 기각.
