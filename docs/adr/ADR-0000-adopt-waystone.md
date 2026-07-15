# ADR-0000: waystone 자신의 개발을 waystone으로 운영한다

- Status: accepted
- Date: 2026-07-16
- Round: —
- SSOT sections affected: §방향성 베팅 (dogfooding bet)
- Tasks: —

## Context

v0.10.0 "Bind & Compose" 릴리스로 0.7–0.9 설계의 완전성 arc가 마감됐다. 사용자의 global constitution은 이미 task 상태를 `waystone task`에, nontrivial 구현을 `waystone delegate`에 두도록 의무화하는데, 정작 waystone 개발 자체는 미채택 상태로 raw codex exec + scratchpad 산출물로 진행돼 왔다. 다음 arc(Adapt & Enforce)는 enforce 승격 임계값·waiver·guard 튜닝의 근거로 실사용 evidence 스트림을 요구하며, 이 머신에서 세션 로그가 가장 많이 쌓인 프로젝트가 바로 이 repo다.

## Decision

이 repo의 개발을 waystone으로 운영한다. 단, 하네스로 쓰는 waystone은 항상 릴리스판(marketplace 설치본)이고 개발 대상은 dev 워킹트리다 — dev 트리의 스크립트를 살아있는 하네스로 쓰지 않는다. waystone이 생성하는 프로젝트 부산물(SSOT.md, tasks.yaml, PROGRESS.md, docs/* 등)은 dev에 커밋하되 main 릴리스에서 제외한다(`release-to-main.sh` EXCLUDES).

## Consequences

dogfooding evidence가 improve 분석·longitudinal metrics·enforce 튜닝의 정직한 데이터 소스가 된다. 하네스 버그로 작업이 막히면 해당 항목만 raw 레시피(codex exec 직접 호출)로 우회하고 그 버그를 finding으로 기록한다 — dev 트리 스크립트로 갈아타지 않는다. 자기 데이터에서 워크플로 개선이 측정되지 않으면(SSOT §열린 질문 "자기증명") 이 결정과 adaptive 비전 자체를 재검토한다.

## Alternatives considered

- dev 워킹트리를 하네스로 직접 사용 — delegate 버그가 그 버그를 고치는 작업을 오염시키는 자기참조 순환이라 기각.
- 미채택 유지(raw codex exec 지속) — global constitution 위반 상태의 지속이며 다음 arc의 evidence 요구와 충돌해 기각.
