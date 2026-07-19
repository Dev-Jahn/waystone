# ADR-0004: executor 경계와 비차단 next-action 계약을 확정한다

- Status: accepted
- Date: 2026-07-19
- Round: —
- SSOT sections affected: 없음 — 이 ADR의 권위 원천은 0.12 계획서 §3-6이다
- Tasks: docs/adr-state-authority-contracts

## Context

run engine이 다음 action을 계산하는 일과 그 action을 실제로 실행하는 일의 경계가 없으면 host가 engine 내부 작업을 다시 실행하거나, 반대로 engine이 user 승인을 대신하는 문제가 생긴다. 특히 `run resume`와 `actions next`가 engine-owned action까지 외부에 노출하면 호출자는 내부 action의 idempotency·fencing 계약을 알 수 없고 중복 실행 위험이 생긴다. 반대로 장시간 engine 작업이 끝날 때까지 호출을 붙잡으면 CLI와 host orchestration 모두 비차단 계약을 잃는다. 따라서 `executor_kind`의 소유권과 두 명령의 반환 상태를 하나의 계약으로 고정한다.

## Decision

`executor_kind`는 action을 **누가 실행할 권한과 책임을 갖는지**를 나타내며 role, provenance, trust 수준을 뜻하지 않는다. 값은 다음 세 가지뿐이다.

| `executor_kind` | 소유 범위 | 실행·완료 책임 | `run resume` / `actions next` 노출 |
|---|---|---|---|
| `engine` | engine 프로세스가 구현하고 effect 관측까지 할 수 있는 상태 전이·내부 작업 | engine이 claim, 실행, 관측, recovery를 수행한다 | 외부 action으로 반환하지 않고 호출 안에서 내부 소진한다 |
| `carrier` | 선언된 host carrier나 runner만 수행할 수 있는 host-native 작업 | carrier가 동일한 `action_id`로 실행 결과를 engine에 보고한다 | 실행 가능한 outward action으로 반환한다 |
| `user` | 사용자 결정, 승인, 입력 또는 수동 외부 작업이 필요한 경계 | user의 명시적 응답 전에는 누구도 완료로 대리 기록하지 않는다 | 실행 가능한 outward action으로 반환한다 |

action 생성 후 `executor_kind`를 다른 종류로 암묵 재분류해서는 안 된다. 선언된 executor를 사용할 수 없으면 typed refusal 또는 typed idle reason으로 드러내며, engine이나 carrier가 user action을 대신하는 fallback은 금지한다.

`run resume`와 `actions next`는 동일한 progress-driving 의미론을 갖는다. 두 호출은 ready 상태인 `engine` action을 claim하고 내부 work budget 안에서 실행·관측·commit하며, 다음 경계 중 하나에 도달할 때까지 계속 소진한다. 이름과 달리 `actions next`는 read-only 조회가 아니며, read-only 관측은 `status`와 `watch`의 계약이다.

두 호출은 오래 기다리지 않고 아래의 상호 배타적인 세 분기 중 정확히 하나를 반환한다. 분기 우선순위는 표의 위에서 아래 순서다.

| 반환 분기 | 조건 | 필수 payload |
|---|---|---|
| action 있음 | ready인 outward action이 있다 | 안정적인 `action_id`, `executor_kind`가 `carrier` 또는 `user`인 action |
| engine busy | outward action은 없고 engine-owned action이 실행·관측·recovery 중이거나 이번 호출의 내부 work budget을 소진했다 | 양수 `poll_after_s` |
| engine idle | outward action도 없고 현재 진행 중인 engine-owned action도 없다 | 자유 문자열이 아닌 안정된 code를 가진 typed `reason` |

`poll_after_s`는 재조회 힌트이지 완료 시각이나 lease가 아니다. busy 분기는 sleep하거나 완료까지 block하지 않는다. idle의 `reason`은 terminal, dependency wait, refusal처럼 호출자가 다음 동작을 결정할 수 있는 닫힌 type이어야 하며, `null`, 빈 action 목록, 자유 형식 message만으로 idle과 busy를 구분해서는 안 된다. ready outward action과 engine busy가 동시에 존재하면 action 분기를 반환한다.

## Consequences

- host와 user는 자신이 소유한 action만 받아 실행하므로 engine 내부 action의 중복 실행 표면이 사라진다.
- engine 내부 진행이 길어도 호출자는 `poll_after_s`를 따라 polling할 수 있고, host event loop를 점유하지 않는다.
- `actions next` 호출이 상태를 전진시킬 수 있으므로 순수 관측 도구로 사용하면 안 된다.
- 새 executor 종류가 필요해지면 enum과 모든 반환·recovery 계약을 함께 개정해야 하며 기존 값을 fallback으로 재사용할 수 없다.

## Alternatives considered

- **모든 ready action을 caller에 반환** — engine 내부 idempotency와 fencing 책임을 host에 누출하므로 기각.
- **engine action 종료까지 blocking** — CLI와 carrier가 장시간 점유되고 crash recovery polling 경계가 사라지므로 기각.
- **실행 불가 action을 다른 executor가 대행** — user 승인과 carrier provenance를 위조하는 silent fallback이므로 기각.
