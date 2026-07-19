# waystone 0.7.0 설계안  
## 적응형 멀티에이전트 개발 워크플로 하네스

**문서 상태:** Draft  
**대상 버전:** 0.7.0  
**문서 성격:** 제품 의도와 상위 설계 원칙을 정의하는 추상 설계안  
**비범위:** 세부 CLI 문법, 파일 스키마, 클래스 구조, 훅 구현, 모델별 프롬프트 전문

---

## 0. 요약

`waystone` 0.7.0의 목표는 잘 정의된 하나의 워크플로를 배포하는 데서 멈추지 않는다. 이 버전은 기존의 SSOT, task registry, round, review 중심 개발 절차를 바탕으로, 사용자의 실제 Claude Code 작업 이력과 프로젝트별 검증 결과를 관찰하여 **각 사용자와 프로젝트에 맞는 멀티에이전트 하네스로 점진적으로 진화하는 체계**를 지향한다.

0.7.0은 다음 전환을 의미한다.

> **고정된 predefined harness에서, 안정적인 기본 preset 위에 관측과 review evidence를 기반으로 개인·프로젝트별 정책을 축적하는 adaptive harness로의 전환**

이 하네스는 특정 모델에 종속되지 않는다. Fable, Opus, Sonnet, Codex 등은 현재 사용 가능한 모델 또는 도구의 구체적 binding일 뿐이며, 핵심 설계 단위는 `main`, `orchestrator`, `implementer`, `clerk`, `verifier`, `reviewer`, `external delegate`와 같은 역할과 책임이다.

또한 0.7.0은 토큰 또는 비용 절감만을 목표로 하지 않는다. 희소한 상위 모델의 reasoning budget, main session context, 개발자의 시간, 외부 도구 구독을 모두 포함한 자원 사용을 최적화하되, **최종 품질을 약화시키는 최적화는 성공으로 간주하지 않는다.** 최적화의 주된 근거는 단순한 agent 종료 상태나 API 오류가 아니라, round 이후 advisor·GPT·Codex·human review에서 검증된 blocker/major/minor finding, 재작업, 미해결 위험과 같은 품질 evidence여야 한다.

---

## 1. 배경과 문제 정의

현재 `waystone`의 핵심 강점은 개발 과정을 명시적 task와 round로 구조화하고, SSOT와 구현의 관계를 유지하며, 외부 review를 독립적인 품질 검증 단계로 둔다는 점이다. 이는 장시간 연구·개발 작업에서 상태 손실, 무계획한 변경, 검증 누락, 리뷰 피드백 유실을 줄이는 데 유효하다.

그러나 멀티에이전트 작업의 규모와 복잡성이 커질수록 다음 문제가 나타난다.

첫째, main session이 task 관리와 최종 판단뿐 아니라 탐색, 구현, 반복 디버깅까지 직접 떠안으면서 희소한 reasoning budget과 context를 소비할 수 있다. 반대로 단순한 task를 과도하게 위임하면 agent launch overhead, 전달 손실, 반복 실패 때문에 총비용이 더 커질 수 있다.

둘째, “이 작업은 어느 모델 또는 어느 role에 맡겨야 하는가”가 prose instruction과 매 순간의 모델 판단에 과도하게 의존한다. 이런 방식은 쉬운 요청을 외부 도구에 강제로 위임해야 하는 상황에서도 agent가 자의적으로 직접 처리하게 만들 수 있고, 역할 경계와 최종 책임을 흐린다.

셋째, 사용자와 프로젝트의 특성이 다르다. 동일한 preset이 어떤 프로젝트에서는 유용한 guard가 되지만, 다른 프로젝트에서는 불필요한 마찰이나 능력 억제로 작용할 수 있다. CUDA 연구 코드, 일반 웹 서비스, 수치 실험, 문서 중심 프로젝트는 요구되는 검증, 환경 준비, 위험 수준, task granularity가 서로 다르다.

넷째, session log에 기록된 “성공” 또는 “실패”만으로는 작업 품질을 충분히 판단할 수 없다. API 오류 없이 종료된 작업도 심각한 설계 결함을 포함할 수 있고, 반대로 도구 오류를 겪은 세션도 최종적으로 높은 품질의 결과를 낼 수 있다. 따라서 하네스 개선은 세션 운영 신호와 실제 review evidence를 결합해야 한다.

0.7.0은 이 문제를 단일한 더 강한 prompt로 해결하려 하지 않는다. 대신 **관측, 분석, delegation, evidence, review, policy adaptation을 분리하고 연결하는 운영체계**를 제공해야 한다.

---

## 2. 제품 비전

`waystone`는 모델을 세세하게 통제하는 규칙 모음이 아니라, 여러 모델과 도구가 명확한 책임 아래 협업하도록 만드는 개발 운영 하네스여야 한다.

이 하네스가 제공해야 할 것은 다음 세 가지다.

1. **안정적인 기본 작업 질서**  
   task, dependency, round, SSOT, verification, review, acceptance의 기본 절차는 사용자의 로그가 아직 없더라도 즉시 유효해야 한다.

2. **실제 행동을 관측하는 피드백 루프**  
   사용자가 `waystone`를 사용하기 전의 Claude Code 로그까지 포함해, 어떤 역할이 어떤 작업을 수행했고 어디서 context·budget·quality가 손실되었는지 관찰할 수 있어야 한다.

3. **근거 기반의 점진적 개인화**  
   관찰된 습관을 그대로 모방하지 않고, review finding과 검증 evidence를 통해 실제로 문제가 된 패턴만 policy delta로 제안해야 한다. 모든 개인화는 설명 가능하고, 되돌릴 수 있고, 적용 전에 과거 로그에 대한 shadow replay를 거쳐야 한다.

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

비용·토큰·context 절감은 중요하지만, 그 자체가 목적이 아니다. 0.7.0은 다음 우선순위를 따른다.

> **정확성 및 연구·개발 품질 → 검증 가능성 → 안정적인 작업 지속성 → 자원 효율**

상위 모델이 직접 수행해야 할 고난도 판단을 단순히 비용 때문에 하위 모델로 내리는 것은 최적화가 아니다. 반대로 상위 모델이 기계적인 탐색, 반복적인 patch 작성, 대량 로그 정리까지 직접 수행하는 것도 바람직하지 않다. 하네스의 역할은 가장 싼 실행자를 찾는 것이 아니라, **각 역할이 책임져야 할 최소 충분한 역량을 배치하고 최종 품질을 독립적으로 검증하는 것**이다.

### 3.2 모델 이름보다 역할과 capability

하네스의 schema와 분석 vocabulary는 특정 모델 이름을 중심으로 설계하지 않는다. `main_direct_debugging`, `worker_retry_loop`, `verification_debt`, `review_finding_density`처럼 역할과 행동을 표현해야 한다.

구체적인 모델은 사용자 profile이 역할에 binding한다. 오늘의 main model이나 implementer model이 바뀌더라도, 하네스의 의미론은 유지되어야 한다.

### 3.3 자기보고보다 evidence

Agent의 “완료했습니다”, process exit code, API success는 품질의 충분한 증거가 아니다. 변경된 파일, 실행한 검증 명령, 실제 결과, review finding, remediation history가 더 중요한 근거다.

하네스는 agent가 잘 행동할 것이라고 가정하기보다, **작업 결과가 어떤 evidence로 뒷받침되는지 추적**해야 한다.

### 3.4 강한 기본값과 얇은 개인화 overlay

기본 preset은 보수적이고 이해 가능하며 장기간 안정적이어야 한다. 개인화는 preset 자체를 계속 변형하는 것이 아니라 다음과 같은 overlay로 축적한다.

- 사용자 공통 습관과 자원 선호를 반영하는 user overlay
- 프로젝트의 stack, 위험, 검증, task 특성을 반영하는 project overlay
- 현재 task 또는 round에만 적용되는 명시적 override

정책 우선순위는 좁은 범위가 넓은 범위를 덮는 형태여야 하며, 각 delta에는 근거, 적용 범위, 생성 시점, 상태가 남아야 한다.

### 3.5 최소 제약, 선택적 강제

하네스는 agent의 잘못된 행동을 줄여야 하지만, 고난도 작업에서 모델의 추론 능력과 유연성을 억누르면 안 된다. 모든 관측 패턴을 곧바로 hard gate로 바꾸지 않는다.

정책은 기본적으로 다음 단계를 거친다.

> **observe → recommend → warn → enforce**

Hard enforcement는 scope 위반, evidence 없는 통과 주장, delegation artifact 누락처럼 의미가 명확하고 false positive 가능성이 낮은 경우에 한정한다. 나머지는 경고와 review feedback으로 남긴다.

### 3.6 추론은 모델에, 반복 가능한 절차는 하네스에

작업 분해, 설계 판단, 모순 해소, 최종 acceptance처럼 문맥과 고난도 reasoning이 필요한 결정은 모델이 담당한다. 반면 worktree 생성, 실행 환경 준비, 외부 delegate 호출, artifact 수집, evidence 기록, 정책 replay처럼 반복 가능하고 결정적인 절차는 script와 workflow가 담당한다.

즉, 0.7.0은 prompt를 더 길게 만드는 것이 아니라 **추론해야 할 것과 자동화해야 할 것을 분리**한다.

### 3.7 정확한 구현과 단순한 운영의 양립

기존 전역 지침에서 강조된 사전 사고, 단순성, 요청 의도에 맞는 완전한 구현, 국소적 변경, 검증 가능한 목표는 0.7.0의 constitution에 반영할 가치가 있다. 다만 세부 routing과 도구 사용 규칙을 모두 CLAUDE.md에 누적하면 context 비용과 instruction drift가 커진다.

따라서 핵심 원칙은 짧은 constitution에 남기고, 세부 정책은 기계가 읽고 검증할 수 있는 별도 policy와 evidence 체계로 이동해야 한다.

---

## 4. 0.7.0의 개념적 구성

0.7.0은 다음의 상호 연결된 계층으로 이해할 수 있다.

### 4.1 Stable Base Harness

사용 이력이 없는 프로젝트에도 적용되는 기본 질서다.

- task와 dependency의 명시
- SSOT 및 decision provenance
- round 단위의 실행과 close
- 검증 evidence의 기록
- 독립 review와 finding ingest
- final acceptance의 책임 분리
- context compact 이후 재진입 가능성

이 계층은 개인화와 무관하게 예측 가능해야 한다.

### 4.2 Observation Layer

Claude Code의 기존 작업 로그를 읽어 구조화한다. 입력의 기본 위치는 사용자의 `CLAUDE_CONFIG_DIR` 아래 `projects`이며, 여러 로그 디렉터리나 별도 archive를 추가로 지정할 수 있어야 한다. `waystone` 사용 여부와 관계없이 과거 작업을 분석할 수 있어야 한다.

Observation은 원칙적으로 판단하지 않는다. 세션, 모델, 역할, tool use, delegation, workflow, retry, context exposure, verification, 종료 상태와 같은 사실을 재구성한다.

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
- 과도한 guard와 반복되는 waiver가 만드는 사용자 마찰

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
user/project별 policy delta 제안
        ↓
과거 로그에 대한 shadow replay
        ↓
사용자 검토 및 선택적 적용
        ↓
새 round의 live evidence 축적
        ↓
다음 improve cycle
```

이 루프의 목적은 자동으로 policy를 계속 늘리는 것이 아니라, **실제 defect와 자원 낭비를 줄이는 소수의 고가치 delta만 유지하는 것**이다.

### 4.5 Delegation and Execution Layer

Delegation은 단순한 자연어 권고가 아니라 하네스가 책임지는 실행 primitive다. 외부 코드 도구를 사용해야 한다면 worker agent에게 “필요하면 호출하라”고 요청하는 대신, 하네스가 script 수준에서 직접 호출하고 결과 artifact를 수집한다.

Delegation은 다음 불변식을 가져야 한다.

- 작업 범위와 acceptance criteria가 명시된다.
- Claude Code 내장 worktree와 혼동되지 않는 `waystone` 전용 worktree 공간을 사용한다.
- delegate 시작 전에 프로젝트 stack에 맞는 실행 환경이 결정적으로 준비된다.
- agent가 환경 부재를 자의적으로 해결하거나 ad-hoc dependency 설치를 하지 않는다.
- 결과는 patch, 변경 파일, 검증 evidence, 제한 사항, 미해결 위험으로 구조화된다.
- delegate는 결과를 제안하지만 최종 acceptance를 소유하지 않는다.
- main 또는 독립 verifier가 결과를 평가한다.

Python, JavaScript, Rust, Go 등 일반적인 stack은 표준 환경 준비 의미론을 가질 수 있어야 하며, 프로젝트별 custom preparation도 명시적으로 제공할 수 있어야 한다. 핵심은 특정 package manager를 강제하는 것이 아니라, **동일 task가 다른 worktree에서도 재현 가능한 환경에서 시작되도록 하는 것**이다.

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

이 계층은 어떤 모델이 “성공했다”는 식의 단순한 승패표를 만드는 데 쓰이지 않는다. 대신 **어떤 workflow 상태와 정책이 높은 품질 또는 반복 결함과 연결되는지**를 판단하는 근거를 제공한다.

---

## 5. Improve의 성숙도 모델

프로젝트별 tuning은 충분한 evidence가 쌓이기 전에는 신뢰할 수 없다. 따라서 `/waystone:improve`는 항상 같은 수준의 결론을 내리지 않는다.

### 5.1 Bootstrap

Closed round와 review가 부족한 상태다. 이 단계에서는 안정적인 base preset과 observe-only telemetry를 제공한다. 개인화된 규칙을 강하게 주장하지 않는다.

### 5.2 Calibrate

몇 개의 round와 기본적인 delegation·verification 기록이 쌓인 상태다. 반복 행동과 friction을 탐지하고 soft recommendation을 생성할 수 있다.

### 5.3 Tune

여러 review cycle에서 실제 finding과 remediation evidence가 축적된 상태다. user/project overlay를 제안하고, routing과 guard를 더 정교하게 조정할 수 있다.

### 5.4 Enforce

추천 정책이 shadow replay와 실제 observe/warn 단계에서 낮은 false positive와 유의미한 품질 개선 가능성을 보인 상태다. 이때 일부 규칙을 enforce로 승격할 수 있다.

정확한 최소 round 수는 고정된 보편 상수가 아니라 프로젝트 규모와 evidence density에 따라 달라질 수 있다. 중요한 것은 **데이터가 부족할 때 개인화를 가장하지 않는 readiness gate**다.

---

## 6. Review 중심 supervision

0.7.0의 적응 루프에서 가장 강한 supervision signal은 review다.

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

따라서 audit은 단순 오류율보다, verified review finding을 task와 route, guard state, verification evidence, project area에 연결해야 한다. 예를 들어 “mutation 후 검증 evidence가 부족한 round에서 verification-related major finding이 반복되었다”는 패턴은 guard 승격의 강한 근거가 된다.

다만 review도 절대적 gold label은 아니다. Reviewer source, task 난이도, diff 규모, review depth에 따라 finding 수가 달라질 수 있다. 그러므로 0.7.0은 raw count를 기계적으로 최적화하지 않고, finding provenance와 task context를 함께 보존해야 한다.

---

## 7. 역할과 권한 모델

### 7.1 Main Session: 기본 orchestrator

0.7.0의 기본 topology에서는 main session이 orchestrator다. Main은 사용자 의도와 전체 프로젝트 상태에 가장 가까우며 다음을 소유한다.

- task graph와 round 범위
- routing 및 delegation 결정
- 사용자 clarification과 decision escalation
- 여러 worker 결과의 종합
- 최종 acceptance
- round close와 review 요청

Main은 subagent definition을 억지로 상속받은 것으로 취급하지 않는다. 대신 main session 전용 운영 contract를 명시적으로 제공해야 한다. 이는 짧은 constitution, 현재 task/round state, routing policy, live evidence summary로 구성된다.

Main이 모든 구현을 직접 수행하는 것은 기본값이 아니지만, 작은 수정까지 기계적으로 위임하는 것도 목표가 아니다. Direct work와 delegation의 경계는 task 크기, 정보 전달 비용, 위험, 예상 반복 횟수, main context 비용을 함께 고려한다.

### 7.2 Worker Roles

Worker는 좁은 책임을 가진다.

- **Implementer:** 명시된 범위 안에서 구현하고 검증 evidence를 생산한다.
- **Clerk:** 저모호성 탐색, 추출, 변환, 반복 가능한 잡무를 처리한다.
- **Verifier:** 구현자와 독립적으로 acceptance criteria와 위험을 검사한다.
- **Reviewer:** diff 또는 artifact의 결함과 단순화 기회를 적대적으로 탐색한다.
- **External Delegate:** Codex와 같은 외부 도구를 deterministic runner를 통해 사용한다.

Worker는 task scope를 재정의하거나, SSOT의 의미를 독자적으로 바꾸거나, 자기 결과에 대한 최종 acceptance를 수행하지 않는다. 범위를 넘어서는 문제는 escalation한다.

### 7.3 Scale-up Topologies

기본은 main-as-orchestrator지만, 대규모 작업에서는 두 가지 확장 경로를 허용한다.

첫째, 반복 가능하고 명시적인 fan-out은 deterministic workflow가 orchestrator carrier가 된다. 여러 task packet을 생성하고, 병렬 delegate를 실행하고, 결과를 집계하되, 최종 판단은 main에 반환한다.

둘째, 매우 큰 campaign에서 독립적인 task bundle을 장기간 관리해야 한다면 orchestrator subagent를 사용할 수 있다. 이 경우에도 orchestrator subagent는 bundle 수준의 계획과 worker 조율만 담당하며, 사용자 의도 변경, cross-bundle decision, 최종 acceptance는 main이 소유한다.

Orchestrator subagent는 기본 topology가 아니라, **bundle 간 독립성, 명확한 acceptance, 전달 손실보다 context 절감 효과가 큰 경우에만 선택되는 확장 모드**다.

---

## 8. Model-Agnostic Routing

Routing 정책은 “Fable은 무엇을 하고 Opus는 무엇을 한다”가 아니라 다음 질문으로 표현되어야 한다.

- 이 task에는 어느 수준의 reasoning이 필요한가?
- 현재 맥락을 상속해야 하는가?
- 독립적인 시각이 필요한가?
- scope가 명확하고 bounded한가?
- 반복적인 tool execution이 중심인가?
- 실패 시 재시도 비용이 얼마나 큰가?
- 최종 품질을 누가 독립적으로 검증하는가?
- 사용자에게 budget sensitivity가 있는가?

사용자 profile은 이러한 role과 capability에 현재 이용 가능한 모델을 binding한다. 모델 세대나 구독이 바뀌면 binding만 변경되고, task 의미론과 review loop는 유지된다.

Codex는 특정 role을 수행할 수 있는 외부 실행 도구로 취급한다. 하네스는 사용 가능 여부, 실행 mode, 결과 형식, 검증 필요성을 관리하지만, Codex 사용 자체를 전체 설계의 중심으로 두지 않는다.

---

## 9. Policy Layering과 진화

Effective policy는 다음 네 층의 합성으로 이해한다.

1. **Base preset:** 모든 프로젝트에 적용되는 안정적 원칙
2. **User overlay:** 사용자의 공통 습관, 선호, budget 및 delegation 성향
3. **Project overlay:** stack, risk, 검증 절차, 프로젝트별 review pattern
4. **Task/Round override:** 현재 작업의 명시적 예외와 일시적 정책

각 recommendation은 다음 정보를 가져야 한다.

- 어떤 evidence에서 도출되었는가
- 어느 범위에 적용되는가
- 기대 효과가 무엇인가
- 어떤 품질 또는 friction 위험이 있는가
- shadow replay에서 어떤 결과가 나왔는가
- observe, warn, enforce 중 어느 단계인가
- 사용자가 언제 승인·거부했는가

정책은 단방향으로 누적되지 않는다. 반복되는 waiver, 높은 false positive, review 개선 효과 부재가 관찰되면 완화하거나 제거할 수 있어야 한다. Adaptive harness는 자기 규칙을 계속 늘리는 시스템이 아니라, **불필요한 규칙을 제거하면서 소수의 유효한 규칙을 정제하는 시스템**이어야 한다.

---

## 10. Interactive Setup과 명시적 동의

Agent definition과 project-level hook은 강력한 기능이며, 설치 범위와 강제 수준을 사용자가 이해하고 선택해야 한다.

`init`과 `improve`는 Claude Code의 interactive TUI를 통해 다음을 선택하게 해야 한다.

- managed project agents를 설치할지
- project-level hooks를 설치할지
- observe, warn, safe-enforce 중 어느 수준으로 시작할지
- delegation worktree 및 환경 준비를 활성화할지
- user-level 또는 project-level 어느 범위에 적용할지

Plugin 내장 agent의 기능 제한 때문에 project-local agent 설치가 유용할 수 있지만, 이를 자동으로 수행하지 않는다. 적용 전에는 변경 대상, 효과, 되돌리는 방법을 보여준다.

Non-interactive CLI는 자동화와 reproducibility를 위해 유지하되, 사용자-facing slash command는 복잡한 내부 단계를 하나의 guided flow로 감싼다.

---

## 11. Live Evidence와 Guard의 역할

Live guard는 모델을 통제하는 일반적 감시자가 아니라, 명확한 계약 위반과 evidence gap을 탐지하는 도구다.

0.7.0에서 가치가 높은 guard 범주는 다음과 같다.

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

## 12. CLAUDE.md와 Machine-Readable Policy의 분리

0.7.0에서 CLAUDE.md는 모든 세부 정책의 저장소가 아니다. 다음과 같은 짧은 constitution을 담는 것이 적절하다.

- 가정을 숨기지 않고 trade-off를 명시한다.
- 가장 단순한 올바른 방법을 우선한다.
- 이름과 요청 의도를 왜곡하는 silent fallback을 사용하지 않는다.
- 변경은 task 목적에 국소화한다.
- 성공 기준을 evidence로 검증한다.
- main은 routing과 final acceptance를 소유한다.
- 비사소한 실행은 적절한 worker 또는 deterministic workflow로 위임한다.
- review finding은 검증 후 task와 policy 개선에 반영한다.

모델 binding, routing heuristic, guard 단계, 환경 준비, 프로젝트별 acceptance command는 별도 machine-readable policy와 live evidence에 둔다.

기존 전역 CLAUDE.md는 중요한 설계 입력이지만 불변의 규범은 아니다. 실제 audit과 review에서 과잉 제약, instruction conflict, context waste가 확인되면 단순화할 수 있어야 한다.

---

## 13. 데이터와 프라이버시

Trace의 기본 원칙은 local-first와 최소 수집이다.

- 기본 source는 사용자의 Claude Code project logs다.
- 여러 source directory를 명시적으로 결합할 수 있다.
- 구조적 metadata와 tool event를 우선 사용한다.
- raw prompt, source content, command body, assistant prose는 필요한 분석에만 opt-in한다.
- review artifact와 code path는 프로젝트 민감 정보로 취급한다.
- user profile은 다른 사용자에게 전이되지 않는다.
- cross-user learning이나 shared telemetry는 명시적 동의 없이는 수행하지 않는다.

0.7.0은 `fabulous-fable`과 같은 연구 플랫폼 전체를 내장하지 않는다. 필요한 최소한의 trace와 audit만 제공하고, 고급 통계·행동 연구는 별도 분석 시스템과 연계할 수 있다.

---

## 14. 성공 평가

0.7.0의 성공은 단일 비용 지표로 평가하지 않는다.

### 14.1 품질

- review에서 검증된 blocker/major finding의 감소
- 동일 유형 finding의 재발률 감소
- remediation round와 reopen 감소
- verification/report grounding 관련 finding 감소
- 최종 acceptance 이후의 회귀 감소

### 14.2 실행 효율

- main session의 불필요한 직접 구현·반복 디버깅 감소
- main context 증가량과 raw output 유입 감소
- 적절한 delegation 완료율 증가
- 외부 도구 구독의 실질적 활용
- worker 간 중복 탐색과 blind retry 감소

### 14.3 재현성과 안정성

- delegation 환경 준비 실패 감소
- ad-hoc dependency mutation 감소
- worktree별 acceptance 재현성 향상
- 변경과 검증 evidence의 연결 비율 향상

### 14.4 사용자 마찰

- guard 경고와 hard block의 빈도
- waiver 및 override 비율
- 사용자에 의해 거부된 recommendation 비율
- 같은 경고의 반복 노출
- improve cycle이 실제로 유지한 policy delta 수

품질이 유지되지 않는 efficiency improvement, 또는 사용자 마찰만 증가시키는 defect prevention은 성공으로 간주하지 않는다.

---

## 15. 0.7.0의 핵심 기능 범위

0.7.0은 개념적으로 다음 능력을 제공해야 한다.

1. 기존 Claude Code 작업 이력을 구조화하는 trace 기능
2. workflow 비효율과 품질 위험을 해석하는 audit 기능
3. 하나의 guided `/waystone:improve` 개선 인터페이스
4. 최소 evidence readiness를 고려한 bootstrap/calibrate/tune/enforce 단계
5. review finding을 중심으로 한 quality supervision
6. model-agnostic role, routing, budget 분석
7. deterministic external delegation과 artifact contract
8. 분리된 worktree와 재현 가능한 환경 준비
9. main-as-orchestrator 기본 topology
10. workflow 또는 orchestrator subagent를 이용한 선택적 scale-up
11. base/user/project/task policy layering
12. shadow replay와 observe→warn→enforce 승격
13. interactive agent/hook 설치 및 명시적 사용자 승인
14. 짧은 CLAUDE.md constitution과 machine-readable policy 분리
15. live evidence, waiver, provenance 기반의 guard 운영

---

## 16. 비목표

0.7.0은 다음을 목표로 하지 않는다.

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

---

## 17. 최종 설계 명제

`waystone` 0.7.0의 중심 명제는 다음과 같다.

> **좋은 멀티에이전트 개발 하네스는 정답 역할 분담을 미리 완벽하게 알고 있는 시스템이 아니다. 안정적인 기본 작업 질서를 제공하고, 실제 실행과 독립 review에서 evidence를 모으며, 사용자와 프로젝트의 반복 패턴에 맞추어 소수의 정책을 점진적으로 정제하는 시스템이다.**

따라서 0.7.0은 “더 많은 agent를 사용하는 버전”도, “상위 모델의 토큰을 아끼는 버전”도 아니다. 이는 main, worker, external delegate, verifier, reviewer가 명확한 책임과 evidence contract 아래 협업하고, 각 round의 결과가 다음 round의 하네스를 개선하는 **적응형 개발 운영체계**다.

이 버전이 성공한다면 `waystone`는 단순한 workflow convention plugin을 넘어, 사용자의 연구·개발 방식과 함께 진화하면서도 품질, 재현성, 책임 경계를 보존하는 개인화된 멀티에이전트 하네스가 된다.
