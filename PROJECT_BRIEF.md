---
schema: waystone-project-brief-1
status: provisional
---
# waystone

## Purpose

에이전트와 함께하는 장기 연구·개발에서는 세션이 바뀔 때마다 방향, 결정, 검증 증거가
사라지기 쉽다. 대화는 압축되고 메모리 파일은 흩어지며, 완료 선언만 남고 그 선언을
뒷받침한 증거는 남지 않는다. 멀티에이전트 작업에서는 main이 탐색·구현·반복 디버깅까지
떠안거나 단순 작업을 과잉 위임해 총비용을 키우고, prose와 순간적 모델 판단에 의존한
역할 배분은 책임 경계를 흐린다. 프로젝트마다 필요한 검증·환경·위험 수준이 다른데도 같은
preset을 적용하면 마찰이 생기며, 오류 없이 끝난 session log만으로는 결과 품질을 알 수 없다.

waystone은 이 intent drift와 context loss를 더 강한 단일 prompt로 해결하지 않는다. 관측,
분석, delegation, evidence, review, policy adaptation을 분리해 연결하고, 여러 모델과 도구가
명확한 책임 아래 협업하도록 하는 개발 운영 하네스다. 프로젝트에는 세션보다 오래 사는 방향
문서와 검증된 task 목록이 있고, 작업은 경계 있는 run으로 진행되며, 위임 결과는 독립 검증과
기록된 판정 없이는 인수되지 않고, 과거 실행은 다음 workflow 개선의 근거가 된다. 이 방향
문서는 owner가 채택한 확정 판단에 대해서는 **binding but falsifiable**하다. 현실의 증거와
충돌해도 문서를 지키기 위해 현실을 왜곡하지 않고, 정식 판단 절차를 거쳐 방향을 개정한다.

일차 사용자는 저자 자신이다. 여러 머신과 Claude Code·Codex 두 host에 걸쳐 장기 연구와
소프트웨어 프로젝트를 병행하는 솔로 개발자가, 세션·에이전트·머신이 바뀌어도 프로젝트의
방향과 증거를 보존하려고 사용한다. 이 솔로 개발자 위상은 장식이 아니라 모든 트레이드오프의
지배 변수다. robustness 투자, readiness의 보수성, 통계적 엄밀성은 개인 도구에 맞추며,
모델의 추론 능력과 유연성을 억누르는 hard gate의 나열은 실패 형태로 본다.

## Commitments

- [commitment/quality-before-efficiency] 정확성·연구 품질, 검증 가능성, 작업 지속성, 자원 효율의 순서로 우선한다. 최종 품질을 약화하는 절감, 비용 때문에 고난도 판단을 하위 모델에 맡기는 것, 상위 모델이 기계적 잡무를 직접 하는 것은 모두 허용하지 않는다.
- [commitment/evidence-over-self-report] 완료 선언, exit code, API success가 아니라 변경 파일, 실행된 검증, review finding, remediation 이력을 근거로 삼으며, 기능은 실제 소비 지점까지 이어져야 구현된 것으로 인정한다.
- [commitment/roles-over-model-names] 설계 단위는 coordinator·worker·verifier·reviewer 4-role의 책임이고 모델은 profile이 binding한다. 구독이나 모델 세대가 바뀌면 binding만 바뀌어야 하며 workflow를 재설계하게 해서는 안 된다.
- [commitment/scripts-for-repeatable-work] 검증·렌더링·bookkeeping·로그 파싱처럼 반복 가능한 단계는 결정론적 script가 facts를 만들고, 모델은 판단과 해석을 담당한다. bookkeeping에 모델 token을 쓰는 설계는 허용하지 않는다.
- [commitment/progressive-enforcement] 정책은 observe, recommend, warn, enforce 순으로 승격하며, 승격에는 evidence와 사용자 동의가 필요하다. hard enforcement는 의미가 명확하고 false positive 가능성이 낮을 때만 허용한다.
- [commitment/local-first-nondestructive] trace·evidence·overlay 같은 행동 데이터는 로컬에 두고 공유 저장소에 commit하지 않으며, 공유는 동의가 필요한 명시적 물질화 경로로만 한다. 기존 이력은 보존하고 생성물은 검토할 수 있도록 uncommitted 상태로 남긴다.
- [commitment/no-silent-fallback] 이름과 다른 동작으로 성공을 가장하지 않는다. parsing 실패, evidence 부재, 계산할 수 없는 지표는 조용히 0이나 생략으로 바꾸지 않고 이유와 함께 드러낸다.
- [commitment/simple-except-evidence-integrity] 불안정한 외부 format에 방어적 versioning과 adapter layer를 쌓지 않고 깨지면 고치되, silent drop 방지, coverage 보고, fail-loud를 포함한 기록 무결성은 단순화를 위한 절약 대상에서 제외한다.

## Prototype scope

- [prototype/canonical-surfaces] 현재 canonical surface는 `brief`, `run`, `review`, `status`와 이를 보조하는 `ideate`, `init`이며, brief의 확인·채택, 단계가 지정된 run, finding 처리, objective-first 상태 투영을 제공한다.
- [prototype/dual-host-single-state] Claude Code와 Codex 두 host가 한 프로젝트의 canonical brief·task·run·review·outcome 상태를 공유한다.
- [prototype/solo-local-machine] 지원하는 실행 신뢰영역은 솔로 개발자가 운영하는 로컬 단일 머신이며, candidate와 evidence도 같은 canonical project에서 로컬로 도달할 수 있는 범위에 둔다.
- [prototype/evidence-based-finding-disposition] review finding은 claim에서 validation과 disposition으로 분리해 evidence로 확인하며, 명시적으로 선택된 work만 task로 materialize한다.
- [prototype/outcome-ledger] 수용된 objective progress는 `refs/waystone/outcomes`의 add-only first-parent ledger에 OutcomeDelta로 기록하고 status는 그 권위를 투영한다.

## Long-term direction

- [long-term/adaptive-harness] 고정된 predefined harness에서 출발해 관측과 review evidence를 근거로 사용자별·프로젝트별 정책을 축적하는 adaptive harness로 진화한다.
- [long-term/community-adoption-is-bonus] 다른 사용자의 채택은 환영할 보너스지만 장기 설계를 왜곡할 목표로 삼지 않고, 솔로 개발자 도구라는 위상을 유지한다.
- [long-term/multimachine-evidence-extension] 멀티머신 evidence 공유는 필요를 입증하는 증거가 생긴 뒤 다룰 장기 확장 영역이며 현재 prototype의 구현 의무가 아니다.

## Non-goals

- [non-goal/cost-minimization] 가장 싼 실행자를 찾는 비용 최소화 도구가 아니다. 최소 충분 역량을 배치하고 독립 검증하는 것이 목적이다.
- [non-goal/general-project-management-platform] 이슈 트래커, merge queue, 이벤트 inbox, 자체 hosting runner를 재발명하는 범용 프로젝트 관리·CI 플랫폼이 아니다.
- [non-goal/model-or-host-dependence] 특정 모델 이름이나 단일 host를 전제하는 도구가 아니다.
- [non-goal/causal-claims-from-observation] session log의 상관 패턴을 인과적 품질 이득으로 주장하지 않는다. 정책의 품질 이득은 적용 후 live evidence로만 성립하며 shadow replay는 발동률과 마찰을 추정할 뿐이다.
- [non-goal/imitation-of-habits] 관찰된 행동을 그대로 정책화하는 습관 모방 학습이 아니다. review finding과 검증 evidence로 실제 문제임이 확인된 pattern만 delta 후보로 삼는다.
- [non-goal/commercial-grade-defense] 상용 수준 방어를 목표하지 않으며 위협 모델과 robustness를 솔로 개발자의 신뢰 경계에 맞춘다.

## Working hypotheses

- [hypothesis/project-brief-anchor-bet] 프로젝트마다 하나의 살아 있는 방향 문서와 생성된 view를 두는 것이 흩어진 memory와 chat summary보다 장기 방향 유지에 우월할 것이다.
- [hypothesis/three-axis-binding-bet] 역할, 실행 방식, backend를 분리하면 모델 이름 중심 설정보다 모델 교체와 host 추가에 견고할 것이다.
- [hypothesis/adaptive-overlay-bet] 강한 기본값 위에 좁은 범위가 넓은 범위를 덮고 모든 delta에 근거·범위·상태를 남기는 얇은 정책 overlay를 두는 편이 preset 자체를 계속 변형하는 것보다 설명 가능하고 되돌리기 쉬울 것이다.
- [hypothesis/evidence-gated-autonomy-bet] 기준별 판정, 독립 검증, 변조 봉쇄를 갖춘 harness 인수 gate 위에서는 위임 loop가 자율적으로 동작할 수 있고, 인간 개입을 열거된 escalation 사유에 한정하는 편이 매 단계 승인보다 안전하고 빠를 것이다.
- [hypothesis/dual-host-single-state-bet] Claude Code와 Codex가 같은 프로젝트 상태를 공유하는 것이 host별 독립 상태보다 workflow의 수명을 길게 만들 것이다.
- [hypothesis/dogfooding-bet] waystone 자신의 개발을 waystone으로 운영하는 것이 가장 정직한 검증 경로일 것이다.

## Open questions

- [question/enforcement-justification-boundary] 어떤 evidence 수준이면 관측된 check가 실제 작업을 차단해도 되며, waiver의 기록·만료·남용 방지는 어떤 형태여야 마찰이 이득을 넘지 않는가?
- [question/scale-up-ownership-boundary] 병렬 task group을 열 때 main session이 cross-task 결정의 단일 소유자로 남는 구조는 어디까지 유지되는가?
- [question/adaptive-self-proof] longitudinal metrics가 run이 쌓일수록 routing·verification·review 정책이 개선됨을 보여줄 것인가? 보여주지 못하면 무엇을 폐기할 것인가?
- [question/generalization-investment] 개인 도구 위상을 유지하면서 문서·onboarding·안전 기본값 등 다른 사용자를 위한 형태에 어디까지 투자할 것인가?
- [question/multimachine-learning] 머신별 독립 학습을 유지할 것인가, 아니면 언젠가 evidence의 선택적 공유 경로를 열 것인가?

## Revision triggers

- [trigger/counterevidence-to-directional-bets] 방향성 베팅에 반대되는 evidence가 축적되면 decision을 열고, 채택된 변경은 ADR로 기록해 방향을 개정한다.
- [trigger/dogfooding-fails-self-proof] dogfooding 데이터에서 workflow 개선이 측정되지 않아 자기증명에 실패하면 adaptive harness 비전 자체를 재검토한다.
- [trigger/implementation-contradicts-brief] 구현 evidence가 brief와 모순되면 조용히 이탈하거나 현실을 문서에 맞추지 않고 main 또는 owner의 명시적 ruling을 거쳐 brief나 구현을 개정한다.
- [trigger/owner-explicit-direction] owner가 방향의 변경이나 재검토를 명시적으로 지시하면 realignment를 열고 그 판단을 반영한다.
