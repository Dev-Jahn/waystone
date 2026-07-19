# waystone 0.7.0–0.9.0 설계안
## 적응형 멀티에이전트 개발 워크플로 하네스 — 3버전 로드맵

**문서 상태:** Approved as implementation baseline (2026-07-13) — 최종 리뷰 승인. 승인 조건이었던 두 불변식(semantic inference provenance §4.2/§17-11, delegation source snapshot binding §4.5/§17-12)은 본 판에 반영됨
**진행:** §5.1 v0.7.0 Observe & Advise 완료 (2026-07-13, 0.7.2까지 — 기록: `0.7.0-m1-implementation-notes.md`; bw2 첫 실사용 검증 통과). §5.2 v0.8.0 Delegate & Verify **구현·릴리스 완료** (2026-07-14, v0.8.0 @ main `7a9c67a` — 기록: `0.8.0-m1/m2-implementation-notes.md`; dirty-state=snapshot commit ADR 확정, §5.2 전 능력 탑재, 실 codex/companion smoke 전부 PASS). **v0.9.0 "Unify & Automate" 릴리스 완료** (2026-07-15 — 본 문서 밖의 owner 승인 재범위: cross-host 저장 통합 + locking + delegate 자율화, 기록: `0.9-pre-adr-storage-lock-autonomy.md` §8-§9).
**개정 (2026-07-15):** 전수 감사(`design-fidelity-audit-2026-07-15.md`, 213요소) 후 owner 지시("설계의 모든 요소를 포괄하도록")로 설계 완전성 arc(L1–L3)가 갭을 해소했다. 규범 개정 R1–R7은 **부록 C**(원문에 우선). 능력별 실상태는 감사 문서 + 부록 C R1. 이월 확정분(enforce 승격·waiver 운영·scale-up topology·hook형 실시간 guard)은 README Roadmap "Next — Adapt & Enforce"가 기록.
**대상 버전:** 0.7.0 – 0.9.0
**문서 성격:** 제품 의도, 상위 설계 원칙, 버전별 개발 지침을 정의하는 추상 설계안
**비범위:** 세부 CLI 문법, 파일 스키마, 클래스 구조, 훅 구현, 모델별 프롬프트 전문
**대체 관계:** 본 문서는 `waystone-0.7.0-design.md`(이전 draft)를 대체한다. 이전 문서의 모든 실질 내용을 계승하되, 다자간 설계 리뷰에서 수렴된 결정 사항과 저자의 결정을 반영해 단일 버전 계획을 3버전 arc로 재구성했다.

---

## 0. 요약

이 설계안의 목표는 잘 정의된 하나의 워크플로를 배포하는 데서 멈추지 않는다. 기존의 SSOT, task registry, round, review 중심 개발 절차를 바탕으로, 사용자의 실제 Claude Code 작업 이력과 프로젝트별 검증 결과를 관찰하여 **각 사용자와 프로젝트에 맞는 멀티에이전트 하네스로 점진적으로 진화하는 체계**를 지향한다.

이 전환은 다음과 같이 요약된다.

> **고정된 predefined harness에서, 안정적인 기본 preset 위에 관측과 review evidence를 기반으로 개인·프로젝트별 정책을 축적하는 adaptive harness로의 전환**

이전 draft가 이 전체 비전을 0.7.0 한 버전에 담았던 것과 달리, 본 문서는 이를 **세 버전의 arc**로 나눈다. 구상된 능력은 하나도 버리지 않되, 각 버전이 단독으로 완결된 사용자 가치를 제공하도록 cut line을 긋는다.

- **0.7.0 — Observe & Advise:** 기존 작업 이력을 관측(trace)하고 해석(audit)하여, 근거가 붙은 개선 제안 리포트를 제공한다. 이 버전의 본체는 "관측 기반 개선 제안"이다.
- **0.8.0 — Delegate & Verify:** delegation을 하네스가 책임지는 실행 primitive로 만든다. 전용 worktree, 재현 가능한 환경 준비, artifact contract, role–model·backend 정적 binding, project-local adaptive overlay와 recommend/warn 수준의 정책 적용이 여기에 담긴다.
- **0.9.0 — Adapt & Enforce:** 정책 계층을 완성하고, evidence가 정당화하는 guard를 enforce로 승격하며, 대규모 작업을 위한 scale-up topology를 연다. **0.9.0까지 원래 구상한 15개 능력 전부가 담긴다.**

이 하네스는 특정 모델에 종속되지 않는다. Fable, Opus, Sonnet, Codex 등은 현재 사용 가능한 모델 또는 도구의 구체적 binding일 뿐이며, 핵심 설계 단위는 `main`, `orchestrator`, `implementer`, `clerk`, `verifier`, `reviewer`와 같은 역할과 책임이다. 외부 도구를 통한 delegation은 별도의 역할이 아니라 역할을 실행하는 backend다(§8.2).

또한 이 설계는 토큰 또는 비용 절감만을 목표로 하지 않는다. 희소한 상위 모델의 reasoning budget, main session context, 개발자의 시간, 외부 도구 구독을 모두 포함한 자원 사용을 최적화하되, **최종 품질을 약화시키는 최적화는 성공으로 간주하지 않는다.** 최적화의 주된 근거는 단순한 agent 종료 상태나 API 오류가 아니라, round 이후 advisor·GPT·Codex·human review에서 검증된 blocker/major/minor finding, 재작업, 미해결 위험과 같은 품질 evidence여야 한다.

마지막으로, 이것은 사업의 명운을 건 제품이 아니라 **저자 자신의 workflow를 더 효율적이고 좋게 만드는 개인 도구**이며, 커뮤니티에서 바이럴이 된다면 더 훌륭한 결과다. 이 위상 규정은 이후 모든 트레이드오프 — robustness 투자 수준, readiness 기준의 보수성, 통계적 엄밀성의 요구 수준 — 에 일관되게 적용된다.

---

## 1. 배경과 문제 정의

현재 `waystone`의 핵심 강점은 개발 과정을 명시적 task와 round로 구조화하고, SSOT와 구현의 관계를 유지하며, 외부 review를 독립적인 품질 검증 단계로 둔다는 점이다. 이는 장시간 연구·개발 작업에서 상태 손실, 무계획한 변경, 검증 누락, 리뷰 피드백 유실을 줄이는 데 유효하다.

그러나 멀티에이전트 작업의 규모와 복잡성이 커질수록 다음 문제가 나타난다.

첫째, main session이 task 관리와 최종 판단뿐 아니라 탐색, 구현, 반복 디버깅까지 직접 떠안으면서 희소한 reasoning budget과 context를 소비할 수 있다. 반대로 단순한 task를 과도하게 위임하면 agent launch overhead, 전달 손실, 반복 실패 때문에 총비용이 더 커질 수 있다.

둘째, "이 작업은 어느 모델 또는 어느 role에 맡겨야 하는가"가 prose instruction과 매 순간의 모델 판단에 과도하게 의존한다. 이런 방식은 쉬운 요청을 외부 도구에 강제로 위임해야 하는 상황에서도 agent가 자의적으로 직접 처리하게 만들 수 있고, 역할 경계와 최종 책임을 흐린다.

셋째, 사용자와 프로젝트의 특성이 다르다. 동일한 preset이 어떤 프로젝트에서는 유용한 guard가 되지만, 다른 프로젝트에서는 불필요한 마찰이나 능력 억제로 작용할 수 있다. CUDA 연구 코드, 일반 웹 서비스, 수치 실험, 문서 중심 프로젝트는 요구되는 검증, 환경 준비, 위험 수준, task granularity가 서로 다르다.

넷째, session log에 기록된 "성공" 또는 "실패"만으로는 작업 품질을 충분히 판단할 수 없다. API 오류 없이 종료된 작업도 심각한 설계 결함을 포함할 수 있고, 반대로 도구 오류를 겪은 세션도 최종적으로 높은 품질의 결과를 낼 수 있다. 따라서 하네스 개선은 세션 운영 신호와 실제 review evidence를 결합해야 한다.

이 문제들을 단일한 더 강한 prompt로 해결하려 하지 않는다. 대신 **관측, 분석, delegation, evidence, review, policy adaptation을 분리하고 연결하는 운영체계**를 세 버전에 걸쳐 제공한다.

---

## 2. 제품 비전

`waystone`는 모델을 세세하게 통제하는 규칙 모음이 아니라, 여러 모델과 도구가 명확한 책임 아래 협업하도록 만드는 개발 운영 하네스여야 한다.

이 하네스가 제공해야 할 것은 다음 세 가지다.

1. **안정적인 기본 작업 질서**
   task, dependency, round, SSOT, verification, review, acceptance의 기본 절차는 사용자의 로그가 아직 없더라도 즉시 유효해야 한다.

2. **실제 행동을 관측하는 피드백 루프**
   사용자가 `waystone`를 사용하기 전의 Claude Code 로그까지 포함해, 어떤 역할이 어떤 작업을 수행했고 어디서 context·budget·quality가 손실되었는지 관찰할 수 있어야 한다.

3. **근거 기반의 점진적 개인화**
   관찰된 습관을 그대로 모방하지 않고, review finding과 검증 evidence를 통해 실제로 문제가 된 패턴만 policy delta로 제안해야 한다. 모든 개인화는 설명 가능하고, 되돌릴 수 있어야 한다. 정책의 발동률과 예상 마찰은 적용 전에 과거 로그에 대한 shadow replay로 사전 점검하고, 정책의 실제 품질 이득은 적용 후 live evidence로 검증한다(§4.4의 의미론 참조).

최종적으로 `waystone`는 다음 질문에 답할 수 있어야 한다.

- 이 사용자의 main session은 어떤 종류의 작업에서 불필요하게 직접 개입하는가?
- 어떤 task는 delegation이 이득이고, 어떤 task는 직접 처리하는 편이 더 단순한가?
- 특정 프로젝트에서 반복적으로 발생하는 major finding은 어떤 workflow 상태와 연결되는가?
- 어떤 guard는 실제 결함을 예방할 가능성이 높고, 어떤 guard는 단지 마찰만 늘리는가?
- 이 프로젝트의 환경과 검증 절차를 worker에게 어떻게 재현 가능하게 제공해야 하는가?
- round가 축적될수록 routing, verification, review policy가 실제로 개선되고 있는가?

---

## 3. 핵심 설계 철학

### 3.1 품질 우선, 효율 최적화

비용·토큰·context 절감은 중요하지만, 그 자체가 목적이 아니다. 다음 우선순위를 따른다.

> **정확성 및 연구·개발 품질 → 검증 가능성 → 안정적인 작업 지속성 → 자원 효율**

상위 모델이 직접 수행해야 할 고난도 판단을 단순히 비용 때문에 하위 모델로 내리는 것은 최적화가 아니다. 반대로 상위 모델이 기계적인 탐색, 반복적인 patch 작성, 대량 로그 정리까지 직접 수행하는 것도 바람직하지 않다. 하네스의 역할은 가장 싼 실행자를 찾는 것이 아니라, **각 역할이 책임져야 할 최소 충분한 역량을 배치하고 최종 품질을 독립적으로 검증하는 것**이다.

### 3.2 모델 이름보다 역할과 capability

하네스의 schema와 분석 vocabulary는 특정 모델 이름을 중심으로 설계하지 않는다. `main_direct_debugging`, `worker_retry_loop`, `verification_debt`, `review_finding_density`처럼 역할과 행동을 표현해야 한다.

구체적인 모델은 사용자 profile이 역할에 binding한다. 오늘의 main model이나 implementer model이 바뀌더라도, 하네스의 의미론은 유지되어야 한다.

### 3.3 자기보고보다 evidence

Agent의 "완료했습니다", process exit code, API success는 품질의 충분한 증거가 아니다. 변경된 파일, 실행한 검증 명령, 실제 결과, review finding, remediation history가 더 중요한 근거다.

하네스는 agent가 잘 행동할 것이라고 가정하기보다, **작업 결과가 어떤 evidence로 뒷받침되는지 추적**해야 한다.

### 3.4 강한 기본값과 얇은 개인화 overlay

기본 preset은 보수적이고 이해 가능하며 장기간 안정적이어야 한다. 개인화는 preset 자체를 계속 변형하는 것이 아니라 다음과 같은 overlay로 축적한다.

- 사용자의 공통 습관, 자원 선호, 프로젝트별 특성을 반영하는 adaptive overlay(project-local; 0.8에서 도입, 0.9에서 user/project로 분리 — §10 참조. 0.7.0의 improve는 advisory 권고만 생성하며 overlay를 저장하지 않는다)
- 현재 task 또는 round에만 적용되는 명시적 override

정책 우선순위는 좁은 범위가 넓은 범위를 덮는 형태여야 하며, 각 delta에는 근거, 적용 범위, 생성 시점, 상태가 남아야 한다.

### 3.5 최소 제약, 선택적 강제

하네스는 agent의 잘못된 행동을 줄여야 하지만, 고난도 작업에서 모델의 추론 능력과 유연성을 억누르면 안 된다. 모든 관측 패턴을 곧바로 hard gate로 바꾸지 않는다.

정책은 기본적으로 다음 단계를 거친다.

> **observe → recommend → warn → enforce**

Hard enforcement는 scope 위반, evidence 없는 통과 주장, delegation artifact 누락처럼 의미가 명확하고 false positive 가능성이 낮은 경우에 한정한다. 나머지는 경고와 review feedback으로 남긴다.

### 3.6 추론은 모델에, 반복 가능한 절차는 하네스에

작업 분해, 설계 판단, 모순 해소, 최종 acceptance처럼 문맥과 고난도 reasoning이 필요한 결정은 모델이 담당한다. 반면 worktree 생성, 실행 환경 준비, 외부 delegate 호출, artifact 수집, evidence 기록, 정책 replay처럼 반복 가능하고 결정적인 절차는 script와 workflow가 담당한다.

즉, 이 설계는 prompt를 더 길게 만드는 것이 아니라 **추론해야 할 것과 자동화해야 할 것을 분리**한다.

### 3.7 정확한 구현과 단순한 운영의 양립

기존 전역 지침에서 강조된 사전 사고, 단순성, 요청 의도에 맞는 완전한 구현, 국소적 변경, 검증 가능한 목표는 constitution에 반영할 가치가 있다. 다만 세부 routing과 도구 사용 규칙을 모두 CLAUDE.md에 누적하면 context 비용과 instruction drift가 커진다.

따라서 핵심 원칙은 짧은 constitution에 남기고, 세부 정책은 기계가 읽고 검증할 수 있는 별도 policy와 evidence 체계로 이동해야 한다.

### 3.8 Robustness보다 impact

하네스의 핵심 입력인 Claude Code session log 포맷은 플랫폼이 안정성을 보장하지 않는 내부 인터페이스다. 그러나 이에 대비한 포맷 versioning, adapter 계층, 방어적 스키마 감지 같은 **불확실한 미래 포맷을 위한 범용 compatibility architecture는 만들지 않는다.** 포맷이 실제로 바뀌면 그때 필요한 최소한의 hotfix로 대응한다. 형식에 관대한 파싱은 쉽게 얻을 수 있다면 좋지만, robustness나 reliability에 얽매인 overengineering은 이 도구의 위상(개인 workflow 도구)에 맞지 않는 낭비다.

단, **speculative compatibility infrastructure를 만들지 않는 것과 분석 유효성을 확인하지 않는 것은 다르다.** trace parser가 포맷 변화로 일부 record를 조용히 버리거나 잘못 분류하면 이후 audit과 policy recommendation 전체가 오염된다 — 이는 robustness 문제가 아니라 evidence integrity 문제다. 따라서 trace의 분석 유효성은 타협하지 않는다: 알 수 없는 record를 조용히 분류하지 않고, parse coverage와 unparsed record 비율을 보고하며, 필수 필드가 사라지면 trace를 fail-loud 또는 degraded 상태로 표시하고, 입력 source와 parser version을 산출물에 기록한다. "깨졌는데 정상적으로 분석된 것처럼 보이는 상태"는 허용하지 않는다.

이것은 개별 이슈에 대한 대응 방침이 아니라 설계 전반의 원칙이다: **실패가 가시적이고 복구 가능하며 evidence를 오염시키지 않는 범위에서는, 기능적으로·정성적으로 사용자에게 더 impactful한 결과를 주는 작업이 broad robustness를 높이는 작업보다 우선한다.**

### 3.9 게이트를 조이기보다 signal을 키운다

개인화의 병목은 evidence의 양이다. solo 사용자의 프로젝트당 closed round와 review는 천천히 쌓이므로, 통계적 엄밀성을 기준으로 readiness를 정의하면 대부분의 프로젝트는 영원히 개인화에 도달하지 못한다.

이에 대한 답은 readiness 기준을 더 보수적으로 잡는 것이 아니다. **개인화 signal이 부족하다면, 같은 로그에서 더 강력하고 효율적으로 signal을 추출하는 방법 — `fabulous-fable`이 개척한 방식이든, 새로운 방향이든 — 을 찾는 것이 더 근본적이고 나은 해법이다.** readiness gate는 "데이터가 없는데 개인화를 가장하지 않는다"는 정직성 장치로만 기능하며, 그 문턱은 관대하게 잡는다(§6 참조). 이 도구의 목적은 제품 출시 기준 통과가 아니라 저자의 workflow가 실제로 더 좋아지는 것이고, 잘못된 recommendation은 되돌리면 된다.

### 3.10 Script-first 경제성

improve 루프의 실행·비용 모델은 세 단계로 요약된다.

1. 간단하고 결정론적인 분석 — 세션 파싱, 이벤트 집계, 패턴 카운팅, 발동률 계산 — 은 **최대한 스크립트화**한다. 결정적이고, 무료이며, 재현 가능하다.
2. 하네스의 강화 — audit 해석, policy delta 제안, 리포트 종합 — 는 **가장 강력한 모델(현재 binding 기준 최상위 tier)로 한 번, 또는 주기적으로 가끔** 수행한다.
3. 강화된 하네스는 이후의 일상 작업에서 **장기적으로 예산을 절약**한다. 비싼 분석은 드물게, 그 결실은 매 round에.

---

## 4. 개념적 구성

이 설계는 다음의 상호 연결된 계층으로 이해할 수 있다. 각 계층이 어느 버전에서 구현되는지는 §5의 로드맵이 정의한다.

### 4.1 Stable Base Harness

사용 이력이 없는 프로젝트에도 적용되는 기본 질서다.

- task와 dependency의 명시
- SSOT 및 decision provenance
- round 단위의 실행과 close
- 검증 evidence의 기록
- 독립 review와 finding ingest
- final acceptance의 책임 분리
- context compact 이후 재진입 가능성

이 계층은 개인화와 무관하게 예측 가능해야 한다. 관측 계층이 어떤 이유로든 작동하지 않아도 base harness는 온전히 동작한다.

### 4.2 Observation Layer

Claude Code의 기존 작업 로그를 읽어 구조화한다. 입력의 기본 위치는 사용자의 `CLAUDE_CONFIG_DIR` 아래 `projects`이며, 여러 로그 디렉터리나 별도 archive를 추가로 지정할 수 있어야 한다. `waystone` 사용 여부와 관계없이 과거 작업을 분석할 수 있어야 한다.

Observation은 원칙적으로 판단하지 않는다. 세션, 모델, 역할, tool use, delegation, workflow, retry, context exposure, verification, 종료 상태와 같은 사실을 재구성한다.

**Semantic inference provenance.** raw log에서 복원되는 의미 정보 — 이 agent가 실제로 implementer였는가, 특정 tool sequence가 verification이었는가, 어느 user instruction이 하나의 task에 대응하는가, 이것이 delegation인지 단순 subagent 호출인지, 미구조화 과거 로그가 어느 round·task에 대응하는가 — 는 확정 사실이 아니라 추론일 수 있다. 따라서 trace에서 파생된 역할·task·delegation·verification 등의 의미 label은 그 provenance를 보존한다: **명시적으로 기록된 사실(explicit), 규칙으로 추론한 사실(inferred), 불명확한 사실(unknown)을 구분하며, 불명확한 값은 추측으로 채우지 않는다.** 필요하면 evidence source, inference rule, confidence를 이후 붙일 수 있다. 이 구분이 있어야 audit이 "이 세션의 main이 구현을 직접 수행했다"와 "tool pattern상 main 직접 구현으로 추정된다"를 구분해 recommendation의 근거 강도를 정직하게 표기할 수 있다. 이 원칙은 첫 trace schema를 고정하기 전에(0.7.0) 적용된다.

Project identity는 raw cwd 문자열과 동일시하지 않는다. 여러 머신, path encoding, cwd 변경으로 나뉜 source가 같은 logical project일 수 있으며, 명시적 alias 또는 canonical identity를 통해 결합할 수 있어야 한다. 반대로 근거 없이 자동 병합해서도 안 된다. 구체적 식별 알고리즘은 비범위다.

입력 포맷의 불안정성에 대한 태도는 §3.8을 따른다: 범용 compatibility 계층을 선제 구축하지 않고, 깨지면 hotfix한다. 다만 parse coverage와 알 수 없는 record는 조용히 삼켜지지 않고 명시적으로 드러난다(§3.8의 evidence integrity 원칙).

### 4.3 Audit Layer

Observation 결과와 review artifact를 결합해 반복되는 비효율과 품질 위험을 해석한다.

중심 lens는 특정 모델이 아니라 다음과 같은 일반적 현상이다.

- main session의 불필요한 직접 구현 또는 반복 디버깅
- delegation이 이득이었을 가능성이 높은 작업
- worker의 scope drift
- 환경 미준비로 인한 반복 실패
- 검증 부채와 evidence 부족
- large output에 의한 main context 오염
- blind retry와 가설 없는 실패 반복
- review finding이 집중되는 task, role, project area
- 과도한 guard와 반복되는 waiver가 만드는 사용자 마찰 (하네스 자체의 guard·waiver를 대상으로 하는 이 lens는 해당 machinery가 도입되는 0.8.0/0.9.0부터 활성화된다; 0.7.0에서는 기존 로그에서 관측 가능한 일반적 마찰만 다룬다)

### 4.4 Adaptive Improvement Loop

사용자-facing entrypoint는 하나의 단순한 개선 인터페이스여야 한다. 내부적으로 observation과 audit을 수행하지만, 사용자가 여러 분석 명령을 직접 조립하도록 요구하지 않는다.

개선 루프는 다음 흐름을 따른다.

```text
기존 작업 로그 + round/review evidence
        ↓
      trace
        ↓
      audit
        ↓
adaptive policy delta 제안
        ↓
과거 로그에 대한 shadow replay (발동률·마찰 사전 점검)
        ↓
사용자 검토 및 선택적 적용
        ↓
새 round의 live evidence 축적 (품질 이득의 실제 검증)
        ↓
다음 improve cycle
```

이 루프의 목적은 자동으로 policy를 계속 늘리는 것이 아니라, **실제 defect와 자원 낭비를 줄이는 소수의 고가치 delta만 유지하는 것**이다.

**Shadow replay의 의미론.** shadow replay가 말할 수 있는 것과 없는 것을 명확히 구분한다. 과거 로그는 해당 정책 *없이* 실행된 세션의 기록이므로, replay로 추정할 수 있는 것은 **그 정책이 있었다면 얼마나 자주 발동했을지(발동률)와 얼마나 마찰을 일으켰을지(non-actionable trigger·noise)** 뿐이다. "그 정책이 있었다면 결과 품질이 좋아졌을지"라는 반사실적 이득은 원리적으로 replay로 검증할 수 없다 — 정책이 있었다면 작업의 궤적 자체가 달라졌을 것이기 때문이다. 따라서 shadow replay는 적용 전 마찰 사전 점검으로만 사용하고, **정책의 실제 품질 이득은 적용 후 live observe/warn 단계에서 축적되는 prospective evidence로만 검증한다.** 이 구분을 흐리는 구현 — replay 결과를 정책 이득의 증명으로 취급하는 구현 — 은 이 설계에 대한 위반이다.

한 가지 용어 주의: 과거 로그만으로는 발동이 실제로 불필요했는지(true false positive)를 자동 판정할 수 없다 — 그 판정에는 retrospective labeling, 후속 waiver/override, 명확한 contract exception, 또는 수동 표본 검토가 필요하다. 따라서 replay의 로그 기반 산출물은 "false-positive rate"가 아니라 **estimated nuisance rate**(retrospective friction proxy)로 표기한다.

improve 엔진의 실행·비용 구조는 §3.10을 따른다: 결정론적 분석은 스크립트가, 해석과 제안은 최상위 tier 모델이 드물게, 그 결실은 일상 round가 누린다.

### 4.5 Delegation and Execution Layer

Delegation은 단순한 자연어 권고가 아니라 하네스가 책임지는 실행 primitive다. 외부 코드 도구를 사용해야 한다면 worker agent에게 "필요하면 호출하라"고 요청하는 대신, 하네스가 script 수준에서 직접 호출하고 결과 artifact를 수집한다. 이 방식의 실현가능성은 이미 실증되어 있다 — 저자의 환경에서 `codex exec` 기반의 script 수준 외부 위임이 실사용 중이다.

Delegation은 다음 불변식을 가져야 한다.

- 작업 범위와 acceptance criteria가 명시된다.
- 하나의 task/worktree에는 동시에 하나의 mutation owner만 존재한다.
- 모든 delegation은 **immutable source snapshot에 결합**된다: 최소한 logical project identity, base revision, dirty-state 처리 방식, task packet, effective policy exposure를 식별할 수 있어야 하며, 결과 artifact와 그 검증은 같은 base를 참조한다.
- Claude Code 내장 worktree와 혼동되지 않는 `waystone` 전용 worktree 공간을 사용한다.
- delegate 시작 전에 프로젝트 stack에 맞는 실행 환경이 결정적으로 준비된다.
- agent가 환경 부재를 자의적으로 해결하거나 ad-hoc dependency 설치를 하지 않는다.
- 결과는 patch, 변경 파일, 검증 evidence, 제한 사항, 미해결 위험으로 구조화된다.
- delegate는 결과를 제안하지만 최종 acceptance를 소유하지 않는다.
- main 또는 독립 verifier가 결과를 평가한다.

Python, JavaScript, Rust, Go 등 일반적인 stack은 표준 환경 준비 의미론을 가질 수 있어야 하며, 프로젝트별 custom preparation도 명시적으로 제공할 수 있어야 한다. 핵심은 특정 package manager를 강제하는 것이 아니라, **동일 task가 다른 worktree에서도 재현 가능한 환경에서 시작되도록 하는 것**이다.

Dirty working tree의 처리 방식 — clean committed HEAD만 허용 / staged snapshot 생성 / explicit patch를 base에 선적용 — 은 0.8.0 delegation 설계에서 확정할 결정 사항이다. 다만 어느 방식을 택하든, **delegate가 암묵적으로 "현재 파일 상태"를 가져왔다고 가정해서는 안 된다**는 점은 설계에 고정한다. base가 불명확한 delegation의 결과는 stale하거나 잘못된 대상에 적용될 수 있고, verifier가 delegate와 다른 base를 검토하면 검증 자체가 무효가 되기 때문이다.

### 4.6 Evidence and Review Layer

각 round는 단순한 작업 묶음이 아니라 학습 가능한 evidence unit이다. 작업 로그만으로는 품질을 알 수 없으므로, review 결과를 중심 supervision으로 사용한다.

특히 다음 정보가 중요하다.

- finding의 severity
- finding이 실제 결함으로 검증되었는지 여부
- finding의 유형: correctness, scope, architecture, verification, reproducibility, reporting 등
- remediation에 필요한 추가 round
- 같은 유형의 finding 재발 여부
- review가 대상으로 삼은 정확한 commit 또는 artifact
- final acceptance 전에 해소되었는지 여부

이 계층은 어떤 모델이 "성공했다"는 식의 단순한 승패표를 만드는 데 쓰이지 않는다. 대신 **어떤 workflow 상태와 정책이 높은 품질 또는 반복 결함과 연결되는지**를 판단하는 근거를 제공한다.

---

## 5. 버전 로드맵

세 버전은 각각 단독으로 완결된 가치를 제공하며, 뒤 버전은 앞 버전의 산출물 위에 선다. 원래 구상된 15개 능력(§16의 매핑표 참조)은 0.9.0까지 전부 담긴다 — 어떤 능력도 "0.9 이후"나 "미정"으로 밀리지 않는다.

### 5.1 v0.7.0 — Observe & Advise

**목표:** 사용자의 기존 Claude Code 작업 이력 전체를 관측·해석하여, 근거가 붙은 workflow 개선 제안 리포트를 하나의 guided 인터페이스로 제공한다.

**담기는 능력:**

- **Trace** (§4.2): `CLAUDE_CONFIG_DIR/projects` 및 추가 지정 source에서 세션·tool use·delegation·retry·verification 사실 재구성. `fabulous-fable`의 파싱·정규화 자산 중 필요한 최소만 이식한다(전체 플랫폼 복제 금지 — §18).
- **Audit** (§4.3): 9개 lens 기반의 비효율·품질 위험 해석. 결정론적 집계는 스크립트, 해석은 모델(§3.10).
- **`/waystone:improve` guided 인터페이스** (§4.4): trace→audit→advisory 리포트를 하나의 흐름으로. 이 버전에서 리포트는 **advisory** — 권고와 근거를 보여주되 자동으로 아무것도 바꾸지 않는다. 다만 각 recommendation에 대한 사용자의 승인·거부 결정은 plugin-local에 기록해(§14), 거부율 지표(§15.4)와 다음 improve cycle의 입력으로 삼는다.
- **Review evidence projection (최소 수준)** (§4.6): 기존 review·triage artifact — 이미 플러그인에 있는 review ingest 기능의 산출물 — 를 audit이 소비할 수 있는 구조화된 evidence view로 투영한다. review reply의 ingest 자체를 재구현하는 것이 아니다. evidence layer로의 정식화는 0.8.0에서.
- **성숙도 모델의 Bootstrap·Calibrate 단계** (§6): observe-only telemetry와 soft recommendation.
- **Interactive consent 프레임워크** (§11): init/improve의 TUI 기반 명시적 선택. 이후 버전의 모든 설치·적용 동의가 이 프레임을 재사용한다.
- **파생 데이터 거주지 규칙** (§14): 행동 데이터는 plugin-local, 공유 repo에 절대 불포함.
- **Model-agnostic 분석 vocabulary** (§3.2): audit 산출물이 모델 이름이 아닌 역할·행동 단위로 기술됨.

**의존 관계:** base harness(기존 0.6.0)만을 전제한다. 0.8/0.9의 어떤 기능도 필요로 하지 않는다.

**성공 기준(§15에서 분배):** main session의 불필요한 직접 구현·반복 디버깅이 리포트에서 실제로 식별되는가; 사용자가 받아들일 만한 recommendation 비율; 리포트의 재현성 — 단 두 층위로 나눈다: 동일 입력에 대해 trace·audit의 구조적 사실과 수치(event table, 집계 metric, finding join, evidence pointer, parse coverage)는 결정적으로 재현되어야 하고, 모델이 생성하는 설명과 recommendation은 byte-level 동일성을 요구하지 않되 핵심 evidence와 우선순위가 불합리하게 변동하지 않아야 한다(§3.10의 script/model 분업과 대응); 사용자에 의해 거부된 recommendation 비율이 압도적이지 않은가.

**Cut line (이 버전에서 하지 않는 것):** 정책의 자동 적용, shadow replay, delegation runner, worktree/환경 준비, guard의 warn/enforce, overlay 저장. 리포트가 권고하는 것을 실행하는 주체는 아직 사용자다.

### 5.2 v0.8.0 — Delegate & Verify

**목표:** delegation을 하네스가 책임지는 결정적 실행 primitive로 만들고, 0.7.0의 권고를 project-local adaptive overlay로 저장·적용(recommend/warn까지)할 수 있게 한다.

**담기는 능력:**

- **Deterministic external delegation + artifact contract** (§4.5): script 수준 runner(예: `codex exec` 계열), 결과의 구조화(patch, 변경 파일, 검증 evidence, 제한 사항, 미해결 위험).
- **`waystone` 전용 worktree + 재현 가능한 환경 준비** (§4.5): stack별 표준 준비 의미론 + 프로젝트별 custom preparation.
- **Role 정적 binding** (§8.2, §9): 사용자 profile이 역할에 모델과 실행 backend를 binding하는 정적 config. 런타임 introspection을 요구하지 않는다.
- **Main-as-orchestrator 운영 contract 정식화** (§8.1): 0.7.0까지는 원칙이던 것을 명시적 contract(짧은 constitution + task/round state + routing policy + live evidence summary)로.
- **Evidence and Review Layer 정식화** (§4.6): review evidence projection을 route·guard state·verification evidence와 연결.
- **Adaptive overlay (project-local) + recommend/warn 적용** (§10): 0.7.0 리포트의 delta를 저장하고 observe/recommend/warn 수준으로 적용. 적용 범위는 현재 프로젝트로 제한하며, 각 delta에 candidate_scope(user_candidate/project_candidate/unresolved)와 observed_in provenance를 기록한다. enforce는 아직 없다.
- **Effective policy exposure record** (§7): 각 round와 delegation이 실행 당시의 effective policy(fingerprint·version), 적용된 overlay 집합, 활성 guard와 stage, route와 execution backend, 명시적 override/waiver를 식별할 수 있는 immutable record를 남긴다. overlay 적용과 함께 도입되며, adaptive loop의 성립 조건이다.
- **Shadow replay** (§4.4): 발동률·마찰(estimated nuisance rate) 사전 점검 의미론으로 도입. overlay 적용 전 점검 단계.
- **성숙도 모델의 Tune 단계** (§6).

이 버전의 warn은 하네스가 소유한 실행 경계 — delegation 시작·종료, round close, review ingest, 명시적 guard/check command — 에서 제공되는 **boundary warning**이며, delegation runner와 round close 같은 script/workflow 수준 검사로 동작한다. 임의의 Claude Code tool call을 가로채는 live warning과 hook 기반의 실시간 개입(enforce 포함)은 0.9.0의 project-level agent/hook 설치와 함께 온다.

**의존 관계:** 0.7.0의 trace/audit(replay와 overlay 제안의 원료), consent 프레임워크(delegation·worktree 활성화 동의), 거주지 규칙(overlay 저장 위치).

**성공 기준:** delegation 환경 준비 실패 감소; ad-hoc dependency mutation 감소; worktree별 acceptance 재현성; 적절한 delegation 완료율 증가; external delegation이 적합하다고 판단된 task 중 실제로 유용한 artifact를 생산한 비율(§15.2); 변경과 검증 evidence의 연결 비율 향상.

**Cut line:** enforce 승격 없음; user/project overlay 분리 없음(adaptive 단일층); orchestrator subagent 없음; CLAUDE.md constitution 이관 없음.

### 5.3 v0.9.0 — Adapt & Enforce

**목표:** 정책 계층과 성숙도 arc를 완성하고, evidence가 정당화하는 guard를 enforce로 승격하며, 대규모 작업의 scale-up 경로를 연다.

**담기는 능력 — 0.9 core:**

- **User/Project overlay 분리** (§10): cross-project evidence가 실제로 쌓인 항목만 adaptive overlay에서 user overlay로 승격하고, 나머지는 project overlay로 정착시킨다. base/user/project/task 4층 layering 완성.
- **Enforce 승격 + waiver/provenance 운영** (§12): observe→warn 이력과 shadow replay 마찰 점검을 통과한 guard의 enforce 승격, 모든 override·waiver의 provenance 기록.
- **Policy exposure 완성** (§7): 0.8.0에서 도입된 exposure record를 enforce·waiver·4층 layering까지 포괄하도록 완성.
- **짧은 CLAUDE.md constitution과 machine-readable policy의 분리 완성** (§13): 기존 전역 CLAUDE.md의 단순화를 포함.
- **성숙도 모델 전체 arc 완성** (§6): Bootstrap→Calibrate→Tune→Enforce.
- **Interactive 설치 확대** (§11): managed project agents, project-level hooks의 consent 기반 설치.

**담기는 능력 — 0.9 scale-up profile:**

- **Scale-up topology** (§8.3): deterministic workflow carrier에 의한 fan-out과, 대규모 campaign을 위한 orchestrator subagent(확장 모드).

scale-up profile은 core와 같은 0.9.0 범위지만 실험적 topology이며 core와의 의존성이 약하므로, **scale-up의 완성도가 0.9 core의 acceptance를 막지 않는다.**

**의존 관계:** 0.8.0의 overlay와 warn 운영 이력(enforce 승격의 근거), policy exposure record(효과 판단의 전제), delegation/worktree(scale-up의 실행 기반), evidence layer(waiver·provenance의 저장처).

**성공 기준:** review에서 검증된 blocker/major finding의 감소; 동일 유형 finding의 재발률 감소; remediation round와 reopen 감소; guard 경고·hard block의 빈도와 waiver 비율이 마찰 신호로 관리되는가; improve cycle이 실제로 유지한 policy delta 수(소수 정예).

**Cut line:** 이 arc의 종점이다. cross-user learning, shared telemetry, `fabulous-fable` 전체 파이프라인 내장은 이 버전에서도 하지 않는다(§18).

---

## 6. Improve의 성숙도 모델

프로젝트별 tuning은 evidence가 쌓이기 전에는 신뢰할 수 없다. 따라서 `/waystone:improve`는 항상 같은 수준의 결론을 내리지 않는다.

### 6.1 Bootstrap

Closed round와 review가 부족한 상태다. 이 단계에서는 안정적인 base preset과 observe-only telemetry를 제공한다. 개인화된 규칙을 강하게 주장하지 않는다.

### 6.2 Calibrate

몇 개의 round와 기본적인 delegation·verification 기록이 쌓인 상태다. 반복 행동과 friction을 탐지하고 soft recommendation을 생성할 수 있다.

### 6.3 Tune

여러 review cycle에서 실제 finding과 remediation evidence가 축적된 상태다. overlay를 제안하고, routing과 guard를 더 정교하게 조정할 수 있다.

### 6.4 Enforce

추천 정책이 shadow replay의 마찰 점검을 통과하고, 실제 observe/warn 단계에서 낮은 false positive와 유의미한 품질 개선 가능성을 보인 상태다. 이때 일부 규칙을 enforce로 승격할 수 있다.

### 6.5 게이트의 보수성에 대하여

단계 진입의 최소 round 수는 고정된 보편 상수가 아니라 프로젝트 규모와 evidence density에 따라 달라질 수 있다. 중요한 것은 **데이터가 부족할 때 개인화를 가장하지 않는 readiness gate**다.

단, 이 gate의 문턱은 **관대하게** 잡는다. 이것은 제품 출시 게이트가 아니라 개인 도구의 정직성 장치다. 반복 패턴이 몇 번 보이면 recommendation을 내고, 사용자가 승인하면 적용하고, 틀렸으면 되돌린다 — 이 사이클 자체가 안전장치이며, 통계적 유의성이 안전장치일 필요는 없다. evidence가 정말로 부족해 보인다면 취할 방향은 게이트를 조이는 것이 아니라 signal 추출을 강화하는 것이다(§3.9).

다만 이 관대함은 단계별로 균일하지 않다. **readiness gate는 recommendation 생성에 관대하게 적용한다. 그러나 warn과 enforce는 서로 다른 evidence threshold를 갖는다.** recommendation은 반복 관측, 합리적인 mechanism, 사용자가 판단 가능한 설명, 쉬운 되돌림이면 충분하다. warn은 더 잦은 반복과 낮은 warning frequency, 실제로 actionable하다는 판단을 요구한다. 특히 enforce는 단순 반복 빈도가 아니라 계약의 명확성, 낮은 해석 모호성, retrospective friction 점검(shadow replay), prospective observe/warn evidence를 요구한다. 각 단계의 비용 — 제안을 읽는 비용, 경고에 응답하는 비용, action이 차단되는 비용 — 이 다르기 때문이다. 관대한 게이트 원칙(§3.9)은 recommendation 층위의 원칙이지, 차단의 문턱을 낮추는 원칙이 아니다.

---

## 7. Review 중심 supervision

적응 루프에서 가장 강한 supervision signal은 review다.

Session log의 agent state는 주로 다음을 말해준다.

- API 또는 tool 실행이 중단되었는가
- command가 실패했는가
- agent가 정상 종료했는가
- 어떤 도구를 얼마나 사용했는가

반면 review는 다음을 말해준다.

- 결과가 실제 요구와 일치하는가
- 설계 또는 구현에 중대한 결함이 있는가
- 검증이 충분한가
- scope와 SSOT가 보존되었는가
- 보고된 주장에 evidence가 있는가

따라서 audit은 단순 오류율보다, verified review finding을 task와 route, guard state, verification evidence, project area에 연결해야 한다. 예를 들어 "mutation 후 검증 evidence가 부족한 round에서 verification-related major finding이 반복되었다"는 패턴은 guard 승격의 강한 근거가 된다.

이 연결이 성립하려면 각 round가 **어떤 정책 아래 실행되었는지**를 알아야 한다. 정책은 round 이후 바뀔 수 있으므로, 현재 policy file을 읽는 것으로는 부족하다. 따라서 다음을 설계 불변식으로 둔다: **각 round와 delegation은 실행 당시의 effective policy, route decision, model/backend binding, guard stage를 식별할 수 있는 immutable policy exposure record를 가진다.** 전체 policy의 복사가 필수는 아니지만, 최소한 policy fingerprint·version, 적용된 overlay 집합, 활성 guard와 stage, route와 execution backend, 명시적 override/waiver의 의미는 보존되어야 한다. 이것은 단순 provenance가 아니라 adaptive loop의 성립 조건이다 — 이것이 없으면 나중에 finding이 줄었더라도 어느 정책 변화와 연결되는지 알 수 없다.

또 하나, review finding과 route의 관찰적 상관관계를 바로 인과로 해석해서는 안 된다. 어려운 task가 더 강한 모델이나 복잡한 topology로 배정되는 선택 편향이 있기 때문이다. **과거 로그와 review finding의 연관은 policy candidate를 생성하는 데 사용한다. 특정 route 또는 guard가 품질을 개선했다고 주장하는 근거는 prospective 적용 이후의 evidence, 직접적인 contract violation 예방, 또는 명시적으로 통제된 비교에서만 얻는다.** `fabulous-fable`의 무거운 통계 체계를 가져올 필요는 없지만, 이 관찰적 후보 생성과 prospective 검증의 구분은 유지한다.

다만 review도 절대적 gold label은 아니다. Reviewer source, task 난이도, diff 규모, review depth에 따라 finding 수가 달라질 수 있다. 그러므로 raw count를 기계적으로 최적화하지 않고, finding provenance와 task context를 함께 보존해야 한다.

---

## 8. 역할과 권한 모델

### 8.1 Main Session: 기본 orchestrator

기본 topology에서는 main session이 orchestrator다. Main은 사용자 의도와 전체 프로젝트 상태에 가장 가까우며 다음을 소유한다.

- task graph와 round 범위
- routing 및 delegation 결정
- 사용자 clarification과 decision escalation
- 여러 worker 결과의 종합
- 최종 acceptance
- round close와 review 요청

Main은 subagent definition을 억지로 상속받은 것으로 취급하지 않는다. 대신 main session 전용 운영 contract를 명시적으로 제공해야 한다. 이는 짧은 constitution, 현재 task/round state, routing policy, live evidence summary로 구성된다.

Main이 모든 구현을 직접 수행하는 것은 기본값이 아니지만, 작은 수정까지 기계적으로 위임하는 것도 목표가 아니다. Direct work와 delegation의 경계는 task 크기, 정보 전달 비용, 위험, 예상 반복 횟수, main context 비용을 함께 고려한다.

### 8.2 Worker Roles와 실행 backend

Worker는 좁은 책임을 가진다.

- **Implementer:** 명시된 범위 안에서 구현하고 검증 evidence를 생산한다.
- **Clerk:** 저모호성 탐색, 추출, 변환, 반복 가능한 잡무를 처리한다.
- **Verifier:** 구현자와 독립적으로 acceptance criteria와 위험을 검사한다.
- **Reviewer:** diff 또는 artifact의 결함과 단순화 기회를 적대적으로 탐색한다.

**External delegation은 별도의 역할이 아니라, 역할을 수행하는 실행 backend다.** Codex는 상황에 따라 Codex-backed implementer, Codex-backed reviewer, Codex-backed verifier가 될 수 있고, Claude subagent도 implementer 또는 reviewer가 될 수 있다. 축이 섞이지 않도록 세 축을 분리한다.

- **책임 축(role):** main, orchestrator, implementer, clerk, verifier, reviewer
- **실행 방식 축(execution):** main session, clean subagent, forked subagent, deterministic workflow, external runner
- **모델·도구 binding 축(backend):** `claude:<model>`, `codex:<model>`, `gemini:<model>` 등

Worker role은 Claude subagent, fork, workflow 또는 external runner를 통해 실행될 수 있으며, 어떤 role을 어떤 실행 방식과 backend에 binding할지는 사용자 profile의 정적 config가 정한다(§9). 이 분리가 있어야 role 정적 binding과 model-agnostic routing에서 축이 섞이지 않는다.

Worker는 실행 backend와 무관하게 task scope를 재정의하거나, SSOT의 의미를 독자적으로 바꾸거나, 자기 결과에 대한 최종 acceptance를 수행하지 않는다. 범위를 넘어서는 문제는 escalation한다.

### 8.3 Scale-up Topologies

기본은 main-as-orchestrator지만, 대규모 작업에서는 두 가지 확장 경로를 허용한다(0.9.0).

첫째, 반복 가능하고 명시적인 fan-out은 deterministic workflow가 orchestrator carrier가 된다. 이때 **작업 분해와 task packet의 경계는 main이 결정하고, workflow는 결정된 packet의 인스턴스화, 병렬 delegate 실행, 결과 집계라는 결정적 절차만 수행한다**(§3.6의 원칙과 정렬 — 분해는 reasoning이므로 모델의 몫, 실행은 절차이므로 하네스의 몫). 최종 판단은 main에 반환된다.

둘째, 매우 큰 campaign에서 독립적인 task bundle을 장기간 관리해야 한다면 orchestrator subagent를 사용할 수 있다. 이 경우에도 orchestrator subagent는 bundle 수준의 계획과 worker 조율만 담당하며, 사용자 의도 변경, cross-bundle decision, 최종 acceptance는 main이 소유한다.

Orchestrator subagent는 기본 topology가 아니라, **bundle 간 독립성, 명확한 acceptance, 전달 손실보다 context 절감 효과가 큰 경우에만 선택되는 확장 모드**다.

---

## 9. Model-Agnostic Routing

Routing 정책은 "Fable은 무엇을 하고 Opus는 무엇을 한다"가 아니라 다음 질문으로 표현되어야 한다.

- 이 task에는 어느 수준의 reasoning이 필요한가?
- 현재 맥락을 상속해야 하는가?
- 독립적인 시각이 필요한가?
- scope가 명확하고 bounded한가?
- 반복적인 tool execution이 중심인가?
- 실패 시 재시도 비용이 얼마나 큰가?
- 최종 품질을 누가 독립적으로 검증하는가?
- 사용자에게 budget sensitivity가 있는가?

사용자 profile은 이러한 role과 capability에 현재 이용 가능한 모델과 실행 backend를 binding한다(§8.2의 세 축). 이 binding은 **사용자가 갱신하는 정적 config**이며, 플랫폼의 런타임 모델 introspection을 요구하지 않는다. 모델 세대나 구독이 바뀌면 사용자가 binding만 변경하고, task 의미론과 review loop는 유지된다.

Codex는 별도의 role이 아니라 implementer·reviewer·verifier 같은 role을 수행할 수 있는 실행 backend(external runner)로 취급한다. 하네스는 사용 가능 여부, 실행 mode, 결과 형식, 검증 필요성을 관리하지만, Codex 사용 자체를 전체 설계의 중심으로 두지 않는다.

---

## 10. Policy Layering과 진화

Effective policy는 다음 층의 합성으로 이해한다.

1. **Base preset:** 모든 프로젝트에 적용되는 안정적 원칙
2. **Adaptive overlay (project-local, 0.8에서 도입):** 사용자의 습관, 선호, budget·delegation 성향과 프로젝트별 특성을 하나의 층에 담는다. 0.7.0의 improve는 advisory 권고만 생성하고, 그 권고가 0.8.0에서 adaptive overlay로 저장·적용된다. **적용 범위는 현재 프로젝트로 제한한다** — 사용자 공통 선호와 project-specific rule이 섞인 상태에서 다른 프로젝트가 이를 상속하면 project-specific 항목이 잘못 전역 적용될 수 있기 때문이다. 다만 각 delta에는 candidate_scope(user_candidate / project_candidate / unresolved)와 observed_in(관측된 프로젝트 목록) provenance를 기록해, 이후 분리의 근거를 보존한다.
3. **User overlay / Project overlay (0.9에서 분리):** cross-project evidence가 실제로 쌓여 "내 습관"과 "이 프로젝트의 요구"를 구별할 수 있게 되면, 여러 프로젝트에서 반복된 evidence가 있는 항목만 user overlay(공통 습관·선호·budget 성향)로 승격하고, 나머지는 project overlay(stack, risk, 검증 절차, 프로젝트별 review pattern)로 정착시킨다. 구별할 evidence가 없는 동안의 조기 분리는 ceremony일 뿐이므로 하지 않는다.
4. **Task/Round override:** 현재 작업의 명시적 예외와 일시적 정책

정책 우선순위는 좁은 범위가 넓은 범위를 덮는다. 여기에 더해 **동일 scope 안에서 방향이 상충하는 delta의 해소 규칙**을 확정한다: 동일 scope 충돌 시 기본적으로 **least-restrictive resolution** — 덜 강제적인 단계(observe < warn < enforce)를 선택 — 을 사용하고, 명시적 task/round override만이 이를 상회할 수 있으며, 충돌은 자동으로 숨기지 않고 friction evidence로 기록해 다음 improve cycle의 입력으로 삼는다.

각 recommendation은 다음 정보를 가져야 한다.

- 어떤 evidence에서 도출되었는가
- 어느 범위에 적용되는가
- 기대 효과가 무엇인가
- 어떤 품질 또는 friction 위험이 있는가
- shadow replay에서 어떤 발동률·estimated nuisance rate가 나왔는가
- observe, warn, enforce 중 어느 단계인가
- 사용자가 언제 승인·거부했는가

각 policy delta는 다음의 lifecycle 상태를 가진다.

```text
proposed → accepted → observing → warning → enforced
                          ↓           ↓         ↓
                      suspended ←──────────────┘
                          ↓
                       retired
```

**Policy delta는 영구 진리가 아니다.** main/worker binding 변경, 프로젝트 stack 변경, test command·디렉터리 구조 변경, Claude Code 기능 변화, 이전 finding의 근본 원인 제거, 새 base preset의 동일 문제 해결 등으로 생성 근거와 적용 환경이 바뀌면 재검토되며, stale evidence 또는 반복 waiver는 suspension·retirement의 근거가 된다.

정책은 단방향으로 누적되지 않는다. 반복되는 waiver, 높은 false positive, review 개선 효과 부재가 관찰되면 완화하거나 제거할 수 있어야 한다. Adaptive harness는 자기 규칙을 계속 늘리는 시스템이 아니라, **불필요한 규칙을 제거하면서 소수의 유효한 규칙을 정제하는 시스템**이어야 한다.

---

## 11. Interactive Setup과 명시적 동의

Agent definition과 project-level hook은 강력한 기능이며, 설치 범위와 강제 수준을 사용자가 이해하고 선택해야 한다.

`init`과 `improve`는 Claude Code의 interactive TUI를 통해 다음을 선택하게 해야 한다.

- managed project agents를 설치할지
- project-level hooks를 설치할지
- observe, warn, safe-enforce 중 어느 수준으로 시작할지
- delegation worktree 및 환경 준비를 활성화할지
- user-level 또는 project-level 어느 범위에 적용할지

Plugin 내장 agent의 기능 제한 때문에 project-local agent 설치가 유용할 수 있지만, 이를 자동으로 수행하지 않는다. 적용 전에는 변경 대상, 효과, 되돌리는 방법을 보여준다.

Non-interactive CLI는 자동화와 reproducibility를 위해 유지하되, 사용자-facing slash command는 복잡한 내부 단계를 하나의 guided flow로 감싼다.

이 consent 프레임워크는 0.7.0에서 도입되고, 0.8.0(delegation·worktree 활성화)과 0.9.0(agent/hook 설치, enforce 승격)의 모든 동의 지점이 이를 재사용한다.

---

## 12. Live Evidence와 Guard의 역할

Live guard는 모델을 통제하는 일반적 감시자가 아니라, 명확한 계약 위반과 evidence gap을 탐지하는 도구다.

가치가 높은 guard 범주는 다음과 같다.

- 선언된 scope 밖 mutation
- 검증되지 않은 변경을 완료 또는 통과로 보고
- 동일 실패를 새로운 가설 없이 반복
- delegation이 요구된 task에서 결과 artifact가 없음
- worker가 final acceptance 또는 권한 밖 decision을 수행
- project의 재현 가능한 환경 contract를 우회
- review가 요구되는 고위험 변경을 독립 검토 없이 close

반면 main session의 모든 Bash나 Edit를 금지하는 식의 model-specific, tool-count 기반 강제는 기본 정책이 되어서는 안 된다. 이런 행동은 audit과 warning의 대상일 수 있지만, task 특성과 실제 결과를 함께 봐야 한다.

모든 override와 waiver는 허용될 수 있으나, silent bypass가 아니라 provenance를 남겨야 한다.

---

## 13. CLAUDE.md와 Machine-Readable Policy의 분리

CLAUDE.md는 모든 세부 정책의 저장소가 아니다. 다음과 같은 짧은 constitution을 담는 것이 적절하다.

- 가정을 숨기지 않고 trade-off를 명시한다.
- 가장 단순한 올바른 방법을 우선한다.
- 이름과 요청 의도를 왜곡하는 silent fallback을 사용하지 않는다.
- 변경은 task 목적에 국소화한다.
- 성공 기준을 evidence로 검증한다.
- main은 routing과 final acceptance를 소유한다.
- 비사소한 실행은 적절한 worker 또는 deterministic workflow로 위임한다.
- review finding은 검증 후 task와 policy 개선에 반영한다.

모델 binding, routing heuristic, guard 단계, 환경 준비, 프로젝트별 acceptance command는 별도 machine-readable policy와 live evidence에 둔다.

기존 전역 CLAUDE.md는 중요한 설계 입력이지만 불변의 규범은 아니다. 실제 audit과 review에서 과잉 제약, instruction conflict, context waste가 확인되면 단순화할 수 있어야 한다. 이 이관과 단순화는 0.9.0에서 수행한다.

---

## 14. 데이터, 프라이버시, 거주지

Trace의 기본 원칙은 local-first와 최소 수집이다.

- 기본 source는 사용자의 Claude Code project logs다.
- 여러 source directory를 명시적으로 결합할 수 있다.
- 구조적 metadata와 tool event를 우선 사용한다.
- raw prompt, source content, command body, assistant prose는 필요한 분석에만 opt-in한다.
- review artifact와 code path는 프로젝트 민감 정보로 취급한다.
- user profile은 다른 사용자에게 전이되지 않는다.
- cross-user learning이나 shared telemetry는 명시적 동의 없이는 수행하지 않는다.

**파생 데이터의 거주지.** 여기서 서로 성격이 다른 두 종류의 데이터를 구분한다.

**관측·추론으로 생성된 행동 데이터와 미승인 overlay는 항상 local-only다.** 사용자 습관 profile, main의 과잉 개입 위치, area별 finding density, raw trace, recommendation history, waiver 패턴, 개인별 model/budget 선호는 모두 plugin-local 공간(`~/.claude/waystone/` 계열, 비커밋)에 거주하며, **공유 repo에 절대 들어가지 않는다.** 커밋되는 생성물(SSOT, review 문서 등)과 plugin-local 생성물의 경계는 gitignore 규칙으로 명시적으로 유지한다 — 이런 행동 데이터가 PR이나 팀 공유 표면으로 유출되는 일은 설계 위반이다.

반면 **사용자가 검토하고 명시적으로 승인한 비민감 project policy** — 이 프로젝트의 acceptance command, delegation 환경 준비 규칙, 특정 경로의 scope guard, 고위험 변경의 review requirement, project-local agent 정의, project-level hook 설정 — 는 별도의 materialization 단계를 통해 project configuration으로 승격할 수 있다.

```text
local recommendation
    ↓ 사용자 승인
sanitized project policy candidate
    ↓ 적용 범위 선택
local project policy 또는 committed project policy
```

여기서도 자동 커밋은 하지 않으며 interactive consent(§11)를 요구한다. 이 구분이 있어야 같은 프로젝트를 다른 머신에서 쓰거나 repo를 재클론했을 때 project contract가 재현되면서도, 행동 기반 개인화 정보는 유출되지 않는다 — 즉 0.9의 project overlay·project-level hooks·project-local agents와 local-only 규칙 사이의 긴장이 해소된다.

Multi-machine 환경은 0.7–0.9 범위에서는 머신별 독립 학습(동기화 없음)으로 취급한다. 단, 위의 committed project policy는 repo를 통해 자연히 공유된다.

이 설계는 `fabulous-fable`과 같은 연구 플랫폼 전체를 내장하지 않는다. 필요한 최소한의 trace와 audit만 제공하고, 고급 통계·행동 연구는 별도 분석 시스템과 연계할 수 있다.

---

## 15. 성공 평가

성공은 단일 비용 지표로 평가하지 않는다. 아래 지표군은 §5의 버전 로드맵에 분배되어 각 버전의 성공 기준이 된다.

### 15.1 품질 (주로 0.9.0에서 평가 가능, 0.8.0부터 축적)

- verified severe finding(blocker/major)의 재발 감소 — 단순 finding 수 감소는 reviewer depth 저하로도 얻어지므로, 재발률·acceptance 이후 결함·remediation burden과 함께 해석한다
- 동일 taxonomy finding의 재발률 감소
- remediation round와 reopen 감소
- verification/report grounding 관련 finding 감소
- 최종 acceptance 이후에 발견되는 결함·회귀 감소

### 15.2 실행 효율 (0.7.0에서 식별, 0.8.0에서 개선 측정)

- main session의 불필요한 직접 구현·반복 디버깅 감소
- main context 증가량과 raw output 유입 감소
- 적절한 delegation 완료율 증가
- external delegation이 적합하다고 판단된 task 중 실제로 유용한 artifact를 생산한 비율 (opportunity-adjusted — 사용량 자체를 늘리는 것은 목표가 아니다)
- worker 간 중복 탐색과 blind retry 감소

### 15.3 재현성과 안정성 (0.8.0)

- delegation 환경 준비 실패 감소
- ad-hoc dependency mutation 감소
- worktree별 acceptance 재현성 향상
- 변경과 검증 evidence의 연결 비율 향상

### 15.4 사용자 마찰 (지표별 최초 측정 가능 버전 표기)

- guard 경고와 hard block의 빈도 (warn은 0.8.0부터, hard block은 0.9.0부터)
- waiver 및 override 비율 (0.9.0부터)
- 사용자에 의해 거부된 recommendation 비율 (0.7.0부터)
- 같은 경고의 반복 노출 (0.8.0부터)
- improve cycle이 실제로 유지한 policy delta 수 (0.8.0부터)

품질이 유지되지 않는 efficiency improvement, 또는 사용자 마찰만 증가시키는 defect prevention은 성공으로 간주하지 않는다.

---

## 16. 능력 범위와 버전 매핑

원래 구상된 15개 능력과 버전 매핑이다. ●는 해당 버전에서 완성, ◐는 부분 도입을 뜻한다.

| # | 능력 | 0.7.0 | 0.8.0 | 0.9.0 |
|---|------|:-----:|:-----:|:-----:|
| 1 | 기존 Claude Code 작업 이력을 구조화하는 trace | ● | | |
| 2 | workflow 비효율과 품질 위험을 해석하는 audit | ● | | |
| 3 | 하나의 guided `/waystone:improve` 인터페이스 | ◐ advisory | ◐ overlay 제안 | ● improve 루프 완성 |
| 4 | evidence readiness를 고려한 bootstrap/calibrate/tune/enforce 단계 | ◐ Bootstrap·Calibrate | ◐ Tune | ● Enforce |
| 5 | review finding 중심의 quality supervision | ◐ evidence projection | ● 정식화 | |
| 6 | model-agnostic role, routing, budget 분석 | ◐ 분석 vocabulary | ● 정적 binding | |
| 7 | deterministic external delegation과 artifact contract | | ● | |
| 8 | 분리된 worktree와 재현 가능한 환경 준비 | | ● | |
| 9 | main-as-orchestrator 기본 topology | ◐ 원칙 | ● 운영 contract | |
| 10 | workflow/orchestrator subagent를 이용한 선택적 scale-up | | | ● |
| 11 | base/user/project/task policy layering | | ◐ adaptive overlay(project-local) | ● 4층 완성 |
| 12 | shadow replay와 observe→warn→enforce 승격 | | ◐ replay + observe/warn | ● enforce |
| 13 | interactive agent/hook 설치 및 명시적 사용자 승인 | ◐ consent 프레임 | ◐ delegation 동의 | ● agent/hook 설치 |
| 14 | 짧은 CLAUDE.md constitution과 machine-readable policy 분리 | | | ● |
| 15 | live evidence, waiver, provenance 기반의 guard 운영 | | ◐ observe/warn guard | ● waiver/provenance |

15개 능력 전부가 0.9.0까지 완성된다.

---

## 17. 운영 불변식

철학이 여러 절에 분산되어 있으므로, 구현이 반드시 보존해야 하는 최소 불변식을 여기에 모은다. 이후 구현 PR을 검토할 때 "이 구현이 설계에 맞는가"의 1차 기준이 된다.

1. **Final acceptance에는 항상 단일 소유자가 있다.** 기본 소유자는 main이다. (§8.1)
2. **하나의 task/worktree에는 동시에 하나의 mutation owner만 존재한다.** (§4.5)
3. **모든 완료·통과 주장은 실제 evidence에 연결된다.** (§3.3)
4. **모든 round와 delegation은 실행 당시의 effective policy exposure를 기록한다.** (§7)
5. **관찰적 상관관계만으로 policy 효과나 모델 우열을 주장하지 않는다.** 품질 개선 주장은 prospective evidence, contract violation의 직접 예방, 또는 통제된 비교에서만 나온다. (§7)
6. **Recommendation은 자동으로 enforce로 승격되지 않는다.** warn과 enforce는 각자 더 높은 evidence threshold를 요구한다. (§6.5)
7. **알 수 없는 trace format은 조용히 무시되지 않는다.** parse coverage와 degraded 상태는 명시적으로 드러난다. (§3.8)
8. **Worker는 scope·SSOT·acceptance authority를 스스로 확장하지 않는다.** 실행 backend와 무관하게 적용된다. (§8.2)
9. **Adaptive policy는 생성·승격될 수 있는 만큼 완화·폐기될 수도 있다.** stale evidence와 반복 waiver는 suspension·retirement의 근거다. (§10)
10. **행동 evidence와 미승인 개인화 산출물은 local-only다.** 사용자가 명시적으로 승인한 비민감 project policy만 materialization을 거쳐 공유 표면으로 승격될 수 있다. (§14)
11. **Trace에서 파생된 의미 label(역할·task·delegation·verification)은 provenance를 보존한다.** explicit / inferred / unknown을 구분하며, 불명확한 값은 추측으로 채우지 않는다. (§4.2)
12. **모든 delegation은 immutable source snapshot에 결합된다.** logical project, base revision, dirty-state 처리 방식, task packet, policy exposure가 식별되고, 결과 artifact와 검증은 같은 base를 참조한다. (§4.5)

---

## 18. 비목표

이 설계(0.7.0–0.9.0 전체)는 다음을 목표로 하지 않는다.

- 특정 모델을 보편적으로 최상위 또는 하위 역할에 고정
- 모든 작업을 최대한 많이 delegation
- main session의 직접 tool use를 일괄 금지
- 세션 로그만으로 모델 품질을 판정
- 자동으로 생성된 policy를 사용자 검토 없이 적용
- advisor, GPT, Codex 또는 human review를 대체
- 하나의 orchestration topology를 모든 규모에 강제
- `fabulous-fable`의 전체 typed-DAG/ML/연구 파이프라인을 plugin에 복제
- 비용 절감을 위해 연구 신호, 검증 깊이, 출력 품질을 희생
- 모델의 고난도 문제 해결 능력을 좁은 규칙으로 억제
- 불안정한 입력 포맷에 대비한 범용 compatibility architecture(versioning/adapter 계층) 선제 구축 — 실패가 가시적이고 evidence를 오염시키지 않는 한 impact가 broad robustness에 우선한다. 단, trace의 분석 유효성(§3.8의 evidence integrity)은 이 비목표의 예외다

---

## 19. 최종 설계 명제

이 설계의 중심 명제는 다음과 같다.

> **좋은 멀티에이전트 개발 하네스는 정답 역할 분담을 미리 완벽하게 알고 있는 시스템이 아니다. 안정적인 기본 작업 질서를 제공하고, 실제 실행과 독립 review에서 evidence를 모으며, 사용자와 프로젝트의 반복 패턴에 맞추어 소수의 정책을 점진적으로 정제하는 시스템이다.**

따라서 이것은 "더 많은 agent를 사용하는 버전"도, "상위 모델의 토큰을 아끼는 버전"도 아니다. 이는 main과 worker role들이 — Claude subagent든, fork든, deterministic workflow든, external runner든 어떤 backend 위에서 실행되든 — 명확한 책임과 evidence contract 아래 협업하고, 각 round의 결과가 다음 round의 하네스를 개선하는 **적응형 개발 운영체계**다.

세 버전의 arc는 이 명제를 감당 가능한 걸음으로 나눈 것이다: 0.7.0은 보는 눈을(observe & advise), 0.8.0은 움직이는 손을(delegate & verify), 0.9.0은 배우는 습관을(adapt & enforce) 만든다. 각 버전은 그 자체로 사용자의 workflow를 실질적으로 개선하며, 앞 버전의 evidence가 뒤 버전의 근거가 된다.

이 arc가 완성되면 `waystone`는 단순한 workflow convention plugin을 넘어, 사용자의 연구·개발 방식과 함께 진화하면서도 품질, 재현성, 책임 경계를 보존하는 개인화된 멀티에이전트 하네스가 된다.

---

## 부록: 구현 단계 메모 (비규범)

본 부록은 설계 규범이 아니다. 최종 리뷰에서 문서 수준의 결정이 아니라 구현 단계의 ADR 또는 task로 내리기로 한 사항과, 권장 착수 순서를 유실 방지를 위해 기록한다.

### A. 구현 중 확정할 ADR 후보

- **Committed project policy와 local adaptive overlay의 우선순위** (0.9 layering 구현 전 ADR): 합리적인 기본 방향은 `task/round override > local project adaptive overlay > committed project contract > user overlay > base preset`. 단, committed project contract를 팀·프로젝트의 권위 있는 규칙으로 볼 것이라면 local overlay가 이를 완화하지 못하게 할 수도 있다.
- **0.8 main contract와 0.9 CLAUDE.md 이관의 경계**: 0.8은 runtime-injected main operating contract의 도입이고, 0.9는 기존 전역 지침의 실제 정리·이관·중복 제거다. 모순은 아니며 구현 시 이 구분을 유지한다.
- **0.7 interactive consent의 범위**: 0.7에는 아직 agent/hook/delegation 적용이 거의 없으므로 범용 wizard framework를 만들지 않는다. recommendation 승인·거부 기록과 이후 버전이 재사용할 최소한의 질문 패턴이면 충분하다.
- **Delegation의 dirty-state 처리 방식** (0.8 delegation 설계 시): clean committed HEAD만 허용 / staged snapshot 생성 / explicit patch 선적용 중 택일(§4.5).

### B. 0.7.0 첫 milestone의 권장 수직 절단

전체 adaptive loop를 흉내 내기보다, 다음 수직 절단이 실제 프로젝트 로그에서 end-to-end로 동작하는 것을 먼저 확인한 뒤 lens를 늘린다. 처음부터 `fabulous-fable`의 풍부한 분류 체계를 넓게 이식하면, 0.7의 핵심 가치인 "실제로 유용한 개선 제안"보다 분석 인프라가 먼저 커질 위험이 있다.

1. **Source discovery** — `CLAUDE_CONFIG_DIR/projects` + 반복 가능한 추가 source
2. **Minimal trace** — session/actor/model/tool/usage/error/verification 후보 + parse coverage + semantic provenance(§4.2)
3. **Review evidence projection** — 기존 review·triage artifact를 round·task·severity 단위로 구조화
4. **Deterministic audit facts** — main direct work, retry loop, verification debt, delegation pattern, context-heavy output, review finding association
5. **`/waystone:improve`** — facts + evidence pointer를 최상위 모델이 해석, recommendation 승인·거부 기록, 자동 적용 없음

---

## 부록 C — 2026-07-15 개정 (R1–R7, 설계 완전성 arc)

전수 감사(`design-fidelity-audit-2026-07-15.md`)와 owner 지시("설계의 모든 요소를 포괄하도록 완벽하게")에 따른 규범 개정. 본 부록은 해당 절 원문에 우선한다. 근거 결정은 감사 문서 §5(D1–D3, F7 판정).

### R1 — §5 서문·§16 능력 지도 정정
"15개 능력 전부가 0.9.0까지 완성된다"(§5 서문)는 폐기한다. 실측 기준(감사 문서가 SSOT): 0.8.0의 #6 표기는 ●가 아닌 ◐였다. 설계 완전성 arc(L1–L3)가 #3(improve 루프, warn 수준), #5, #6, #9, #11(4층), #13(consent 설치)을 완성했고, **#4의 Enforce 종점·#10(scale-up)·#12의 enforce 승격·#14(전역 CLAUDE.md 이관은 L3에서 안내 경로 제공)·#15(waiver 운영)는 "Next — Adapt & Enforce" 슬롯으로 이월**되었다(README Roadmap이 기록). 이월분을 제외한 전 능력은 arc 완료 시점에 ●이다.

### R2 — §10 policy layering의 구현 확정형
4층은 다음으로 실현된다: base = 코드 내장 기본 정책(layer 0로 합성 참여), user overlay = `~/.waystone/overlay/`(승격은 registry-canonical 프로젝트 ≥2곳의 불변 evidence 게이트를 가진 명시 verb — 조기 분리 ceremony 금지 유지), project = 로컬 overlay + **committed policy(`docs/waystone-policy.yaml`, materialization 산출물)**, round override = `--reason` 필수·close 시 만료·크래시 복구 포함. 합성은 narrow-over-broad(round>project>user>base), 동일-scope 충돌은 least-restrictive+conflict 기록, committed vs local은 committed 승리+shadowed 가시 표기(ADR: 감사 문서 §5 D1d). 층간 식별은 composite identity `{layer, id}`. **profile은 project 단일**(pre-ADR §7.1)로 §10과 별개 축.

### R3 — §8.2/§9/§17-5/§17-8 실행 주체와 강제 수단의 명문화
§8.2의 실행 축에 '실행 주체' 구분을 추가한다: **waystone-executable = external-runner뿐**(codex·claude backend). clean/forked-subagent·deterministic-workflow·main-session은 호스트가 실행 주체이며, waystone은 (1) routing 계약 주입, (2) 스킬의 라우팅 규범, (3) 관측 귀속(role 렌즈)으로 소비한다 — 별도 러너를 발명하지 않는다. §17-5(인과 주장 금지)·§17-8(worker scope/SSOT 비확장)의 강제 수단은 '기록·감사가능성 + 렌즈/guard 근사'다: 완전 기계 강제는 모델 발화·행위 특성상 불가능하며, scope-drift 규칙·provenance 라벨·verdict 게이트가 근사 기계다. claude external-runner는 codex와 달리 구조적 network sandbox가 없어 **기본 거부 + 명시 override(--reason 기록)**로 운영한다.

### R4 — §14 raw content opt-in
현행(전면 비수집)이 규범보다 보수적이므로 규범을 "기본 비수집; 필요 시 opt-in 추가 가능(미구현이어도 정합)"으로 개정한다. materialization 산출물(committed)은 sanitized rule/stage/params와 한 줄 설명만 담고 행동 evidence·로컬 경로를 포함하지 않는다(§14 준수, L2-D F7).

### R5 — §12 guard 탐지 수단의 구분
§12의 탐지 대상을 두 수단으로 나눈다: (a) **boundary warn으로 충족**(이번 arc 출하): scope 밖 mutation(delegation-scope-drift-v1), env manifest mutation(env-manifest-mutation-v1), 독립 review 부재 close(review-skipped-closes-v1), 증거 없는 done(done-without-evidence-v1) — 전부 tri-state 평가·replay 지원·warn 한정. (b) **hook형 실시간 개입 필요**(Next arc): blind retry 실시간 감지, main 임의 주장의 일반 검증. enforce 승격·waiver는 (b)와 함께 이월.

### R6 — §10 delta lifecycle의 'accepted' 상태
별도 'accepted' 상태를 추가하지 않는다 — `overlay add`가 곧 acceptance(명시 사용자 명령 + AskUserQuestion 후 호출)이며, accept→delta 연결은 `--from-rec` 1:1 강제와 decisions.jsonl 조인으로 감사 가능하다. 현행이 더 단순하고 정보 손실이 없다.

### R7 — §4.4 nuisance rate의 unlabeled-null 정합
replay의 `estimated_nuisance_rate: null + nuisance_provenance: unlabeled`는 결함이 아니라 §4.4 규범 그 자체다(라벨 소스인 waiver/override는 Next arc에서 생김). §16 표를 읽을 때 #12의 0.8 ◐는 이 의미로 해석한다.
