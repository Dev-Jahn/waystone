# ADR-0005: fact별 authority·cache·머신 간 전달 경계를 확정한다

- Status: accepted
- Date: 2026-07-19
- Round: —
- SSOT sections affected: 없음 — 이 ADR의 권위 원천은 0.12 계획서 §5-2이다
- Tasks: docs/adr-state-authority-contracts

## Context

0.12 run engine은 Git, SQLite, local artifact store, OS supervisor, remote provider에 걸친 사실을 다룬다. 같은 사실을 둘 이상의 저장소가 authoritative하게 보유하면 crash recovery나 다른 머신의 resume가 어느 복사본을 믿어야 하는지 결정할 수 없다. 특히 Git fact의 내용을 DB에 다시 저장하고 그 row를 원본처럼 사용하면 checkout·fetch 이후 DB가 조용히 stale해진다. 각 fact의 단일 authority, 허용되는 cache, 머신 간 전달 방법을 명시해 충돌 시 재관측할 원천을 고정한다.

## Decision

**DB는 Git fact를 authoritative하게 복제하지 않는다.** DB에는 join에 필요한 식별자와 Git fact의 digest/object id, `observed_at`만 비권위 cache로 둘 수 있다. Git payload를 편의를 위해 projection하더라도 그 값으로 Git을 대체하거나 충돌을 판정해서는 안 되며, 사용 전 현재 Git authority에서 다시 읽어 digest를 대조한다.

| Fact | Authority | 허용 Cache | 머신 간 전달 |
|---|---|---|---|
| Git-tracked run 입력·project policy·task/plan bytes | 해당 Git commit의 tree bytes | DB의 content digest + `observed_at`; payload 복제는 비권위 projection | fetch/clone으로 commit과 tree를 전달한 뒤 digest 재검증 |
| `base_sha`, `code_result_sha`, Git tree/ref 상태 | Git object database와 현재 ref 관측 | object id + `observed_at`; DB row는 ref의 현재값이 아님 | Git protocol로 object/ref를 전달하고 수신 머신에서 다시 resolve |
| `code_result_sha`의 remote reachability | 대상 remote의 ref/object graph | remote name, 관측한 object id, `observed_at` | cache를 복사하지 않고 대상 remote에서 재관측 |
| 게시된 run closeout manifest의 bytes와 run별 유일성 | manifest를 포함하는 Git history | manifest digest + `observed_at` | Git으로 manifest commit을 전달하고 bytes/digest 재검증 |
| run definition, frozen closure, FSM transition, claim·lease·fence | active project의 SQLite `state.db` | 별도 authoritative cache 없음; read model은 같은 DB에서 파생 | Git으로 전달하지 않는다. 필요한 state 이전은 ADR-0007의 일관된 SQLite backup/restore로만 수행 |
| action 실행 상태 (`planned`부터 terminal까지의 현재 단계와 관측 receipt) | SQLite action journal과 그 transaction/CAS 규칙 | query용 derived view만 허용 | active run 중 다른 머신으로 복제하지 않는다. closeout에는 runtime lifecycle을 이식하지 않는다 |
| immutable runner·verifier·receipt artifact bytes | digest로 식별되는 local artifact store의 실제 bytes | DB에는 path, digest, size 같은 index만 저장 | 명시적 content-addressed artifact transfer 후 digest 검증; Git manifest의 digest만으로 bytes가 전달되지는 않음 |
| heartbeat | owning supervisor가 갱신하는 SQLite mutable telemetry row | 같은 row의 `boot_id`, monotonic 관측값, `observed_at`; append-only transition으로 승격 금지 | 전달하지 않는다. 다른 머신에서는 stale 값을 liveness로 해석하지 않고 `unknown` |
| process liveness | 해당 머신의 OS/supervisor가 process identity를 긍정적으로 관측한 결과 | DB에 process identity, `boot_id`, monotonic 관측값, `observed_at` | 전달할 수 없는 machine-local fact다. owner 머신에서 재관측할 수 없으면 `unknown` |
| run health (`running`, `stalled` 등 derived health) | action/job 단위 liveness·progress·current fact로 query 시 계산한 파생값 | 입력 digest와 `observed_at`을 붙인 일시 cache만 허용 | 값을 복제하지 않고 authority facts로 재계산; 입력을 관측할 수 없으면 `unknown` |
| carrier/user action 결과 | engine이 수락해 SQLite에 commit한 typed receipt; 원본 증거 bytes는 artifact store | DB의 receipt digest와 artifact index | runtime DB 이전 또는 artifact 명시 전송으로만 전달; host의 구두/화면 상태는 authority가 아님 |

Authority가 서로 충돌하면 최신 timestamp를 고르는 것이 아니라 표의 authority를 재관측한다. `observed_at`은 freshness 설명용이며 authority 순위를 만들지 않는다. 특히 heartbeat의 침묵, 다른 boot의 PID, remote reachability cache는 종료·생존·도달 가능성의 현재 증거가 아니다.

### `run_id` 생성과 uniqueness 책임

- engine은 최초 run row를 쓰기 전에 RFC 9562 UUIDv7을 생성하고 canonical lowercase hyphenated string을 `run_id`로 고정한다. UUID의 시간 성분은 정렬 편의일 뿐 ordering이나 authority 근거로 사용하지 않는다.
- generator는 표준이 요구하는 random bits를 CSPRNG로 생성해 머신 간 충돌 저항성을 제공할 책임이 있다. host timestamp, PID, task id의 단순 연결은 금지한다.
- 각 `state.db`는 `run_id`에 `UNIQUE` constraint를 두어 project-local 중복을 기계적으로 거부한다. insert collision은 기존 run을 덮어쓰거나 resume하지 않고 새 UUID 생성으로 재시도한다.
- Git publication 계층은 같은 `run_id`에 manifest가 이미 있는지 add-only CAS로 검사한다. 같은 `run_id`와 다른 manifest digest가 관측되면 cross-machine identity conflict로 typed refusal하며 timestamp 우선이나 merge로 해소하지 않는다.
- 따라서 generator는 전역 collision resistance, SQLite는 로컬 uniqueness, Git publication은 머신 간 충돌 검출을 맡는다. 어느 한 계층도 다른 계층의 책임을 대신했다고 주장하지 않는다.

## Consequences

- checkout이나 fetch 뒤 DB cache가 오래됐더라도 Git fact는 Git에서 재파생할 수 있다.
- active runtime state는 Git sync만으로 다른 머신에서 resume되지 않는다. state 이동이 필요하면 일관된 DB와 필요한 artifact를 함께 명시적으로 이전해야 한다.
- process liveness와 run health는 다른 머신에서 과장되지 않고 `unknown`을 보존한다.
- closeout manifest는 이식 가능한 최소 결과 요약이고 local DB의 runtime history 대체물이 아니다.

## Alternatives considered

- **Git payload를 DB에 authoritative mirror로 저장** — 두 authority가 checkout/fetch 후 갈라지므로 기각.
- **모든 runtime row를 Git으로 전달** — mutable lifecycle과 machine-local liveness를 portable fact로 오인하게 하므로 기각.
- **timestamp+PID 기반 `run_id`** — clock skew·reboot·다중 머신에서 uniqueness 책임을 충족하지 못하므로 기각.
