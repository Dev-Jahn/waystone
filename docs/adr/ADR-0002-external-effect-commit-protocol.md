# ADR-0002: 외부 효과를 관측한 뒤 원자적으로 완료한다

- Status: accepted
- Date: 2026-07-19
- Round: —
- SSOT sections affected: 없음 — 0.12 계획서 §3-4의 runtime core 계약을 확정·정밀화
- Tasks: docs/adr-runtime-core-contracts

## Context

SQLite transaction은 Git ref, worktree, artifact, process, push, GitHub marker 같은 외부 효과와
원자적으로 묶일 수 없다. DB만 먼저 완료하면 실제 효과가 없을 수 있고, 효과를 먼저 실행하면
효과 직후 DB commit 전에 죽었을 때 재개가 같은 효과를 다시 실행할 수 있다. lease 만료 역시
소유자의 생존 가능성을 추정할 뿐, 이미 시작된 효과가 없었다는 증거가 아니다. 따라서 0.12
계획서 §3-4는 store 구현 전에 모든 외부 효과에 공통인 5단계 protocol, 효과 종류별 관측 계약,
fencing과 CAS 규칙을 ADR로 고정하도록 요구한다. 계획서는 recovery의 두 crash window를
구분하라는 원칙만 확정하고 정확한 분기표는 ADR에 남겼으며, 이 문서가 그 미결 세부를 결정한다.

## Decision

### 공통 5단계

모든 외부 효과 action은 다음 순서를 따른다. 단계 이름은 action lifecycle의 권위 있는 용어이며,
중간 단계를 건너뛰어 `completed`를 기록할 수 없다.

| 단계 | 필수 사실 | 허용되는 일 |
|---|---|---|
| `planned` | immutable `input_digest`와 idempotency key를 DB에 기록 | 아직 외부 효과를 실행하지 않는다. |
| `claimed` | `owner_token`과 새 `fencing_epoch`를 CAS로 획득 | 현재 owner만 효과 시작을 시도할 수 있다. |
| `effect` | 외부 산출물에 `run_id`·`job_id`·`action_id`를 직접 각인하거나 engine-owned 이름/metadata로 일의적으로 결속 | 효과별 idempotency precondition을 만족할 때만 실행한다. |
| `observed` | 엔진이 Git, filesystem, supervisor/process, remote API에서 결과를 재도출하고 `observed_digest`를 계산 | worker나 carrier의 free-text 보고를 사실로 승격하지 않는다. |
| `completed` | 현재 entity version과 fencing epoch를 확인하여 `observed_digest`와 함께 한 DB transaction으로 commit | action을 terminal success로 소비할 수 있다. |

`observed`는 “명령이 성공을 보고했다”는 뜻이 아니라 권위 채널에서 기대한 외부 상태를 다시
읽었다는 뜻이다. 외부 산출물 자체에 식별자를 넣을 수 없는 표면은 engine-owned ref/path/metadata가
그 산출물과 세 식별자의 일의적 대응을 보존해야 한다.

### Effect 종류별 계약

같은 `action_id` 아래의 retry는 아래 표의 idempotency 조건으로 이미 만들어진 결과를 재관측하거나,
효과가 일어나지 않았음이 긍정적으로 확인된 뒤 같은 의미 상태를 CAS로 성립시키려는
reconciliation이다. 같은 의미의 효과를 무조건 한 번 더 실행하는 retry가 아니다.

| Effect | Idempotency 기준 | 관측 방법 | **관측 채널 불가 시** | Retry 방식 |
|---|---|---|---|---|
| Git ref 생성·갱신 | expected old OID를 precondition으로 한 ref CAS | 실제 ref OID를 다시 읽어 expected/desired OID와 대조 | 로컬 관측은 원칙적으로 항상 가능해야 한다. repository/ref를 읽을 수 없으면 `unknown-effect`이며 ref 부재로 간주하지 않는다. | desired OID면 관측 결과를 채택한다. expected old OID면 동일 action을 재조정하고, 다른 OID면 conflict로 차단한다. |
| Worktree 생성 | action별 고정 경로와 전용 ref | `git worktree list`의 등록 정보와 해당 worktree `HEAD`를 함께 대조 | 어느 한쪽이라도 읽을 수 없으면 `unknown-effect`이며 미생성으로 간주하지 않는다. | 식별자·ref·HEAD가 모두 맞으면 재사용한다. 긍정적으로 미생성임을 확인한 경우에만 동일 action으로 생성하고, 불일치 잔여물은 conflict로 reconcile한다. |
| Artifact write | content digest 경로와 같은 filesystem 내 atomic rename | 최종 bytes를 다시 읽어 digest를 재계산 | unreadable은 `unknown-effect`이며 absent와 구분한다. | digest가 맞으면 채택한다. 긍정적 absent일 때만 동일 action으로 다시 쓰며, 다른 bytes가 있으면 덮어쓰지 않고 conflict로 차단한다. |
| Runner 실행 | action당 at-most-once인 process execution | supervisor 소유 atomic completion marker와 process identity를 대조 | `unknown-effect`로 대기하며 같은 action을 재실행하지 않는다. | 실행이 시작되지 않았음이 긍정적으로 증명된 경우에만 같은 action의 최초 실행을 허용한다. 실행된 runner의 재시도는 현재 action을 확정적으로 terminal 처리한 뒤 새 attempt와 새 `action_id`로만 만든다. |
| Patch integration | expected parent/tree를 precondition으로 한 integration CAS | integration commit의 parent와 tree를 재도출 | repository, commit, tree 중 필요한 관측을 할 수 없으면 `unknown-effect`이며 미적용으로 간주하지 않는다. | desired commit/tree면 채택하고, expected parent가 유지되면 동일 action으로 reconcile한다. parent/tree가 다르면 재관측 후 conflict로 차단한다. |
| Push | expected remote OID를 precondition으로 한 remote ref CAS | live remote ref의 OID를 조회 | network 또는 remote 조회 불가는 `unknown-effect`로 대기하며 미푸시로 가정하지 않는다. | desired OID면 채택한다. expected remote OID면 동일 action의 CAS를 재시도하고, 다른 OID면 conflict로 차단한다. |
| GitHub marker | action에 결속된 remote dedupe key | 신뢰 대상 remote event를 다시 조회하여 key와 payload digest를 대조 | API/network 관측 불가는 `unknown-effect`로 대기하며 미게시로 가정하지 않는다. | 같은 key와 payload면 채택한다. 긍정적 absent일 때만 동일 key로 재조정하며, 같은 key의 다른 payload는 conflict로 차단한다. |

관측 실패는 `false`, absent, exited 중 어느 값으로도 축약하지 않는다. 관측 채널이 복구될 때까지
`unknown-effect`로 fail-toward-verification하며, 그 상태 자체로 재실행이나 destructive cleanup을
허용하지 않는다.

### Fencing과 CAS correctness

1. action claim은 DB transaction과 unique constraint로 직렬화하고, 인수·재인수 때마다
   `fencing_epoch`를 단조 증가시킨다. claim은 기대한 entity version에 대한 CAS여야 한다.
2. lease와 heartbeat는 liveness 판단 자료일 뿐이다. lease 만료나 heartbeat 부재는 effect 부재,
   ownership 회수 완료, retry 또는 cleanup 허가를 뜻하지 않는다.
3. effect를 시작하거나 submit/apply/push 결과를 채택하는 경로는 현재 `action_id`,
   `fencing_epoch`, entity version, 효과별 expected state를 대조해야 한다. CAS 실패 후 blind retry는
   금지하고 다시 관측하여 desired state, unchanged expected state, conflict 중 하나로 분류한다.
4. stale worker의 늦은 submit은 현재 fencing epoch와 entity version이 다르므로 거부한다. 외부
   시스템에 이미 도달한 in-flight 결과는 없던 것으로 만들지 않고 현재 owner가 식별자와 expected
   state를 재관측해 채택 또는 conflict 처리한다.
5. `completed` transaction은 현재 entity version과 fencing epoch를 CAS하고, action에 결속된
   `observed_digest` 및 관측 근거를 함께 기록한다. 외부 효과 성공 보고만으로는 commit할 수 없다.

따라서 lease expiry가 회복을 시작하게 할 수는 있어도 correctness를 증명하지는 않는다.
correctness의 근거는 끝까지 `fencing_epoch + expected-state CAS + observed_digest`다.

### “중복 실행 0”의 정의

M2 exit gate에서 “중복 실행 0”은 **같은 `action_id`가 나타내는 의미적 effect가 두 번 성립하지
않는다**는 뜻이다. idempotency key는 최소한 `action_id`, `action_kind`, `input_digest`, target,
expected state에 결속한다. 동일 action의 reconciliation은 이미 성립한 desired state를 채택하거나
긍정적으로 absent인 상태에서 최초 효과를 성립시키는 것이므로 중복 실행으로 세지 않는다.

실제로 실행을 한 번 더 해야 하는 retry는 새 attempt와 새 `action_id`를 발급한다. 특히 runner가
시작됐는지가 불확실하면 기존 action은 `unknown-effect`에 머물며, 새 action을 만들어 같은 입력을
실행하는 것도 금지한다. 기존 action의 effect와 terminal 결과가 긍정적으로 확정된 뒤에만 엔진의
retry policy가 새 attempt를 계획할 수 있다.

### Crash recovery 결정표

계획서가 ADR에 남긴 미결 항목을 다음과 같이 확정한다. recovery는 action record, effect별 권위
채널, ref/worktree, artifact digest, process identity와 completion marker를 함께 reconcile한다.
lease 만료 하나만으로 아래의 “긍정적” 조건을 만족시킬 수 없다.

| crash 뒤 관측 | 분류 | 상태 결정 | 허용되는 recovery | 금지되는 일 |
|---|---|---|---|---|
| `planned`만 있고 claim/effect 시작 기록이 없음 | 효과 전 | `planned` 유지 | 새 owner가 CAS claim한 뒤 같은 action의 최초 effect 실행 | claim 없이 실행 |
| `claimed`였으나 owner의 quiescence와 effect별 권위 채널의 absent가 모두 긍정적으로 확인됨 | **효과 전 사망** | 새 epoch로 reclaim | 같은 action이 effect를 최초로 실행 | lease 만료만 보고 absent로 간주 |
| 동일 process identity가 살아 있거나 effect가 진행 중임을 긍정적으로 관측 | 사망 아님 / in-flight | 기존 action nonterminal 유지 | 기존 owner/supervisor가 계속 수행하도록 두고 관측 | takeover, 재실행, cleanup |
| action에 결속된 desired effect와 digest는 관측되지만 `completed` transaction이 없음 | **효과 후 commit 전 사망** | `observed` 복원 | 현재 owner가 effect를 재실행하지 않고 재도출한 digest로 `completed` CAS commit | effect 재실행 |
| runner completion marker 또는 supervisor wait status가 effect 종료를 증명하지만 DB commit이 없음 | **효과 후 commit 전 사망** | marker/process identity를 검증해 `observed` 복원 | return code·signal·artifact digest를 reconcile하고 action을 한 번만 terminal commit | 같은 `action_id`의 runner 재실행 |
| partial result, expected state 불일치, 같은 idempotency key의 다른 payload가 관측됨 | conflict/partial-effect | typed conflict 또는 `unknown-effect` | 기존 산출물을 보존하고 별도 reconciliation 결정을 요구 | 덮어쓰기, blind retry, completed 승격 |
| 관측 채널 불가, process identity 불일치, runner 시작 뒤 completion 증거 부재 등 effect 유무를 확정할 수 없음 | 불확실 | `unknown-effect` | 채널 복구 또는 expert recovery를 기다리고 마지막 확정 근거를 노출 | 재실행, 새 retry action 생성, destructive cleanup |

“효과 전 사망”은 effect 부재가 긍정적으로 증명된 경우이고, “효과 후 commit 전 사망”은 action에
결속된 effect가 긍정적으로 관측된 경우다. 둘 다 증명되지 않으면 세 번째 값인 `unknown-effect`다.

## Consequences

- crash 후 resume은 상태 문자열을 보고 명령을 반복하는 기능이 아니라 외부 authority와 action
  record를 reconcile하는 기능이 된다.
- 각 adapter는 idempotency precondition, 관측기, 관측 불가 사유, conflict 분류를 구현해야 하므로
  단순 command wrapper보다 코드가 늘지만, 효과별 임의 fallback은 허용되지 않는다.
- runner는 at-least-once가 아니라 action당 at-most-once다. 불확실한 실행은 자동 재시도보다
  `unknown-effect` 차단을 택하므로 사람이 확인해야 하는 경우가 생긴다.
- M1·M2의 fault injection은 각 효과에서 effect 전 kill, effect 후 DB commit 전 kill, 관측 채널
  불가를 독립적으로 검증해야 한다.

## Alternatives considered

- **DB를 먼저 commit하고 외부 효과를 실행** — DB와 실제 세계가 불일치하며 완료가 효과를
  증명하지 못하므로 기각.
- **lease 만료 후 같은 명령을 다시 실행** — lease는 effect 부재 증거가 아니어서 중복 runner,
  push, marker를 만들 수 있으므로 기각.
- **모든 effect를 하나의 generic retry 정책으로 처리** — 관측 authority와 idempotency 조건이
  효과마다 달라 안전한 retry를 정의할 수 없으므로 기각.
- **관측 실패를 absent로 취급** — unreadable/unreachable을 미실행으로 오판하므로 기각.
