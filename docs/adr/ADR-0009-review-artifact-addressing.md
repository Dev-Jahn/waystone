# ADR-0009: review artifact를 UUID owner directory로 주소화한다

- Status: accepted
- Date: 2026-07-19
- Round: —
- SSOT sections affected: 없음 — 권위 원천은
  `docs/reviews/2026-07-19-m0-contracts-feedback.md`의 JW-GPT-018·020,
  ADR-0002~0008, `docs/invariants.md`, `docs/known-issues.md`이다
- Tasks: feat/review-artifact-addressing

## Context

review request, binding, feedback와 PR sidecar는 Git-tracked evidence이므로 ADR-0005의 Git fact
authority를 유지해야 한다. 그러나 기존 flat layout은 round 식별자와 artifact 종류를 한 filename에
delimiter로 이어 붙인다. 정상 식별자 자체가 `-freeze-` 같은 delimiter를 포함할 수 있어, filename을
분해해 owner를 추측하면 다른 owner의 malformed sidecar가 healthy owner의 판독을 차단할 수 있다.
`docs/known-issues.md`의 JW-GPT-015가 이 legacy PR-mode residual을 기록한다. runtime store의 key는
Git-tracked review artifact의 신원을 대신하지 않으므로 이 충돌을 구조적으로 없애지 않는다.

현행 E-09도 판정 근거로 filename을 포괄 허용하여 delimiter 기반 owner 추론을 정당화할 여지가
있다. 동시에 filesystem metadata만 금지하므로 hostname처럼 대상과 독립적으로 바뀌는 ambient
값은 잡지 못한다. 반대로 ambient 값을 전면 금지하면 ADR-0003이 한 boot/process lifetime 안에서
process 관측에 사용하는 boot ID, PID, process start token, monotonic time도 금지된다. 따라서 review
artifact의 주소 규칙과 durable identity·scoped ambient observation의 경계를 함께 확정해야 한다.

## Decision

### 신규 review artifact layout

신규 run의 모든 review artifact는 ADR-0005의 canonical `run_id`를 owner로 삼아 다음 Git-tracked
layout에만 발행한다.

```text
docs/reviews/runs/<run-uuid>/
  request.md
  request.binding.json
  feedback.md
  pr-freeze/<cycle>.json
  pr-demotion/<observation-id>.json
```

`<run-uuid>`는 RFC 9562 UUIDv7의 canonical lowercase hyphenated grammar를 만족해야 한다. 각
artifact의 schema 또는 body가 제공하는 payload `run_id`도 같은 grammar를 만족해야 하며,
directory segment의 값과 정확히 일치해야 한다. reader는 두 검사를 모두 통과한 뒤에만 artifact를
해당 run에 귀속한다. segment가 유효하지 않거나 payload `run_id`가 없거나 서로 다르면 identity
conflict로 거부하며, filename prefix나 인접 파일에서 owner를 보충 추측하지 않는다.

`request.md`, `request.binding.json`, `feedback.md` 같은 고정 leaf filename은 artifact kind를
식별한다. `<cycle>`과 `<observation-id>`는 이미 검증된 owner directory 안에서 sidecar를 찾는 local
locator다. 이 이름들을 왼쪽이나 오른쪽 delimiter로 분해하여 owner identity를 추론하는 규칙은
금지한다. Git-tracked path는 검증된 `run_id`의 주소일 수 있지만 filename 자체가 owner의 독립적인
신원 근거는 아니다.

writer는 신규 artifact를 이 layout에만 발행한다.

### Legacy strangler adapter

기존 `docs/reviews/` 직계 flat artifact는 bulk migration하거나 소급 rename하지 않는다. reader는
별도의 legacy adapter로 역사적 filename grammar와 payload를 판독하고 결과를 legacy evidence로
표시한다. canonical writer는 flat artifact를 새로 발행하지 않으며 canonical reader와 legacy
adapter는 같은 addressing 규칙을 공유하지 않는다.

`docs/reviews/runs/` 아래에서 신규 grammar나 payload 일치 검사를 실패한 artifact를 flat legacy
artifact로 fallback하지 않는다. legacy adapter의 filename 판독은 기존 evidence를 보존하기 위한
경계 내부의 compatibility 동작일 뿐, 신규 artifact의 owner identity나 durable attribution 근거로
승격할 수 없다. 따라서 JW-GPT-014·015는 기존 flat 파일에 대해서는 legacy PR-mode residual로
남고, 신규 layout에서만 delimiter collision 부류가 구조적으로 제거된다.

### Durable identity와 scoped ambient observation

E-09의 경계를 다음 세 범주로 고정하고 `docs/invariants.md`의 해당 행을 함께 개정한다.

| 범주 | 정의와 예 | 허용되는 사용 |
|---|---|---|
| durable intrinsic identity | 대상이 변하지 않는 동안 변하지 않는 값. canonical UUID, content digest, Git object ID, 계약으로 안정성이 보장된 host identity | 장기 identity, ownership, attribution, binding에 사용 |
| scoped ambient observation | 관측 시점의 환경값이지만 유효 scope와 lifetime이 계약에 명시된 값. boot ID + PID + process start token, 같은 boot의 monotonic time | 명시된 scope 안에서 locator 또는 freshness evidence로만 사용 |
| incidental ambient value | 대상과 독립적으로 바뀔 수 있는 값. hostname, cwd, mtime/ctime, inode, directory stat, enumeration order | 진단·표시·탐색에만 사용하며 durable authority에는 사용하지 않음 |

값의 사용이 정당하려면 다음을 모두 만족해야 한다.

1. observation scope와 lifetime이 계약에 명시되어 있다.
2. 값의 변화가 실제 대상 identity 또는 observation epoch의 변화와 대응한다.
3. 판정 시점에 권위 채널에서 재관측할 수 있다.
4. scope 밖, 불일치 또는 재관측 불가에서는 durable identity로 승격하지 않고 `unknown`으로 강등한다.
5. 더 안정적인 intrinsic identifier가 있는데 중복 신원축으로 사용하지 않는다.

따라서 ADR-0003의 boot ID·PID·process start token 조합과 같은 boot 안의 monotonic time은
process locator와 freshness evidence로 정당하다. 일반 hostname과 cwd는 각각 machine/project의
durable identity가 아니며, filesystem metadata와 enumeration order도 evidence ordering이나
attribution의 근거가 아니다. review path에서는 검증된 UUID directory가 주소이고 payload
`run_id`와의 일치가 귀속 근거이며, filename delimiter 분해는 이 기준을 충족하지 않는다.

## Consequences

- 신규 review artifact의 owner 경계가 directory 하나로 닫혀 정상 ID 사이의 delimiter collision이
  다른 run의 판독에 영향을 주지 않는다.
- Git-tracked review evidence authority는 유지되며 runtime store key를 Git evidence의 신원으로
  오인하지 않는다.
- reader는 신규 layout과 legacy flat layout을 모두 지원하지만 writer는 신규 layout만 발행하는
  strangler migration이 된다.
- 기존 flat evidence의 ambiguity는 소급 제거됐다고 주장하지 않으며 JW-GPT-014·015 residual을
  legacy adapter 경계에서 계속 드러낸다.
- E-09는 hostname을 포함한 incidental ambient 값을 포괄하면서 ADR-0003의 scoped process 관측은
  그대로 허용한다.

## Alternatives considered

- **flat filename delimiter 규칙을 신규 artifact에도 유지** — 정상 ID 사이의 prefix collision과
  owner ambiguity가 남으므로 기각.
- **runtime store key를 review artifact의 authority로 사용** — Git-tracked evidence와 store 사이에
  중복 authority를 만들고 ADR-0005의 fact 경계를 위반하므로 기각.
- **기존 flat artifact를 일괄 migration 또는 rename** — 역사적 evidence bytes와 path provenance를
  바꾸므로 기각.
- **ambient 값을 전면 금지** — ADR-0003의 boot/process scope 안에서 필요한 liveness·freshness
  관측까지 막으므로 기각.
