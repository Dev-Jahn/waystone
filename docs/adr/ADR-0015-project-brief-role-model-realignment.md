# ADR-0015: project brief 역할 commitment를 canonical 4-role로 realign한다

- Status: accepted
- Date: 2026-07-23
- Round: —
- PROJECT_BRIEF facts affected: `commitment/roles-over-model-names`
- Tasks: fix/brief-role-model-realignment

## Context

`PROJECT_BRIEF.md`의 `commitment/roles-over-model-names`는 설계 단위를
`main·orchestrator·implementer·clerk·verifier·reviewer` 6-role의 책임으로 서술한다. 이 이름들은
pre-0.13 release 하네스 세대에서 책임, 실행 위치, engine 기능, deterministic step을 한 role 목록에
섞어 표현한 용어다.

0.13 redesign mandate와 ADR-0008은 canonical role을 `coordinator`, `worker`, `verifier`,
`reviewer` 네 개로 재설계했다. `waystone/jobs/domain.py`의 domain enum과 profile schema도 이
4-role만 허용한다. 2026-07-22 intent control-plane review의 F3는 owner authority인 committed
brief가 이 canonical domain과 충돌함을 지적했다. 같은 날 main은 자율권 정책에 따라 4-role을
canonical로 확정했고, 코드를 구 brief에 맞춰 6-role로 되돌리는 선택을 금지했다. 2026-07-23
owner의 "나머지 전부 착수" 지시는 이 ruling에 따라 F3 수정을 진행할 권위를 제공했다.

## Decision

`commitment/roles-over-model-names`의 fact ID와 핵심 주장은 유지한다. 문언의 역할 목록만
`coordinator·worker·verifier·reviewer` 4-role로 realign한다. 설계는 모델 이름이 아니라 네 역할의
책임을 단위로 삼고, 각 역할의 모델은 profile이 binding한다. 구독이나 모델 세대가 바뀌면 profile
binding만 교체하며 workflow를 재설계하지 않는다.

`main`은 실행 위치, `orchestrator`는 engine 기능, `clerk`는 deterministic step으로 남고 canonical
role이 아니다. legacy `implementer` binding은 adapter가 `worker`로 판독할 수 있지만 새 profile에
canonical role로 발행하지 않는다. 코드와 profile schema의 4-role 모델은 변경하지 않는다.

개정된 brief는 이 branch에서 `provisional`로 둔다. owner evidence를 보존하는 adoption은 squash
merge로 commit identity가 확정된 뒤 main dev checkout에서 실행하고, 그 결과인 `committed` frame을
별도 commit한다. 이 worktree에는 adoption record를 생성하지 않는다.

## Consequences

- binding commitment, domain enum, profile schema가 같은 4-role 책임 모델을 가리킨다.
- 역할과 모델 binding을 분리한다는 기존 commitment는 그대로 유지된다.
- pre-0.13 artifact와 audit의 6-role 표기는 역사 기록으로 보존하며 canonical 입력으로 재발행하지
  않는다.
- squash merge 직후 brief는 adoption 전까지 provisional이므로 run의 binding source로 사용할 수
  없다. main은 runbook의 adopt와 후속 commit을 완료해야 한다.

## Alternatives considered

- **코드를 6-role로 되돌린다.** 0.13 mandate, ADR-0008, domain enum, profile schema와 ruling을 모두
  뒤집고 책임과 실행 메커니즘을 다시 결합하므로 기각한다.
- **fact ID를 새로 만든다.** 역할보다 모델명을 우선하지 않는 기존 판단을 폐기한 것이 아니라 그
  역할 명칭을 canonical domain에 맞춘 것이므로 불필요한 identity 단절을 만든다.
- **squash merge 전에 adopt한다.** adoption 뒤 commit identity가 다시 바뀌어 owner가 채택한 exact
  frame과 최종 Git frame의 결속을 잃으므로 기각한다.
