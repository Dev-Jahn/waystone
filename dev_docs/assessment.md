# 종합 평가

**Waystone은 문제 정의와 안전성 철학은 상당히 강하지만, 현재 공개 플러그인으로서는 기능 범위가 지나치게 넓고 운영 복잡도가 높은 편입니다.**

| 평가 항목          |         점수 |
| -------------- | ---------: |
| 문제 정의·제품 방향성   | **8.5/10** |
| 워크플로 설계        | **8.0/10** |
| 구현의 결정성·감사 가능성 | **8.5/10** |
| 안전성·사용자 통제     | **8.0/10** |
| 코드·테스트 엔지니어링   | **7.5/10** |
| UX·학습 비용       | **5.5/10** |
| 문서·릴리스 성숙도     | **6.0/10** |
| 범용성·도입 용이성     | **5.5/10** |

**현재 완성도: 7.2/10**
**아이디어 및 아키텍처 잠재력: 8.5/10**

---

## 1. 이 플러그인이 실제로 해결하려는 문제

Waystone은 단순 task manager나 프롬프트 모음이 아닙니다. 핵심은 장기 실행되는 agentic software development에서 다음 문제를 통제하려는 것입니다.

* 세션 간 intent drift
* 컨텍스트 압축·종료 후 프로젝트 방향 손실
* agent가 말하는 “완료”와 실제 검증 결과의 혼동
* 구현자와 검증자의 역할 혼재
* 여러 문서에 흩어진 task·decision·review 상태
* delegated coding 결과의 무비판적 수용

이를 위해 `SSOT.md`, `tasks.yaml`, `ROADMAP.md`, `PROGRESS.md`, ADR, review artifact를 결합하고, 작업을 round 단위로 닫으며, delegation 결과를 별도 verifier와 main-session verdict로 평가합니다. README가 설명하는 제품 포지셔닝과 실제 skill 구현은 대체로 일치합니다. ([GitHub][1])

이 문제 선택 자체는 타당합니다. 특히 장기 연구·복잡한 소프트웨어 프로젝트에서는 agent의 단기 context보다 **외부화된 상태와 검증 가능한 artifact**가 중요하기 때문입니다.

---

# 강점

## 2. “Agent의 주장”과 “시스템이 관측한 사실”을 분리한다

가장 좋은 설계입니다.

`delegate` skill은 다음을 명시적으로 구분합니다.

* base SHA, patch, changed files: harness가 계산한 evidence
* worker의 verification, risk, limitation: delegate가 주장한 내용
* verifier 결과: independent-verifier evidence
* 최종 accept/reject: main session의 별도 verdict

특히 worker가 생성한 보고서를 곧바로 사실로 승격하지 못하게 하고, verdict를 delegation contract와 분리합니다.

이는 일반적인 coding agent workflow의 중요한 결함을 정확히 겨냥합니다. 많은 시스템이 다음을 사실상 동일하게 취급합니다.

```text
agent가 테스트했다고 보고함
≈ 테스트가 실제로 성공함
≈ 변경사항이 올바름
≈ merge해도 됨
```

Waystone은 이 네 단계를 분리합니다. 단순하지만 실제 agent orchestration에서 중요한 구조입니다.

---

## 3. 최종 통제권을 agent worker에게 주지 않는다

delegated task는 별도 worktree에서 수행하고, 최종적으로 main session이 evidence에 기반해 apply 또는 discard하도록 설계되어 있습니다. README뿐 아니라 skill의 구체적인 verdict schema와 resolution 단계에도 이 원칙이 반영되어 있습니다. ([GitHub][1])

다음 정책도 적절합니다.

* acceptance criterion별 `met` 판정
* 실제 실행한 command와 exit code 기록
* verifier blocker를 무시하려면 이를 반박하는 구체적 agent check 필요
* drift 발생 시 자동 3-way apply나 silent stash 금지
* failed delegation record를 재작성하지 않고 새 attempt로 보존

이 구조는 재현성, 사후 분석, 책임 소재 측면에서 잘 설계되어 있습니다.

---

## 4. deterministic layer와 LLM judgment layer의 분리가 명확하다

Waystone은 가능한 작업을 Python/Bash로 내리고, 모델은 판단이 필요한 부분에만 사용하려는 방향을 취합니다.

예를 들면:

* task registry validation
* roadmap rendering
* git diff 및 changed-file 계산
* SHA binding
* log parsing
* evidence projection
* overlay replay
* environment preparation
* merge gating

README에서도 “scripts for repeatable steps; models for judgment”를 명시하고 있습니다. ([GitHub][1])

이는 비용 절감보다도 **semantic nondeterminism의 노출 범위를 줄인다**는 점에서 적절합니다. agentic workflow에서 LLM이 task database, 상태 전이, SHA 검증까지 직접 담당하면 신뢰성이 급격히 나빠집니다.

---

## 5. brownfield 도입을 고려한 초기화 설계가 좋다

`init`은 기존 프로젝트를 Waystone 구조에 맞춰 강제로 재구성하기보다, 기존 ADR·progress·review path를 탐지하여 config를 기존 구조에 맞추도록 지시합니다. 기존 파일 이동도 명시적 동의 없이는 하지 않으며, 생성 결과도 commit하지 않고 사용자가 검토하게 합니다.

또한 managed block 밖의 `CLAUDE.md`나 `AGENTS.md` 내용을 건드리지 않는 규칙, 기존 memory에서 repo-derived state와 실제 사용자·환경 정보를 구분하는 정책도 합리적입니다.

“non-destructive”가 단순 마케팅 문구가 아니라 skill instruction에 어느 정도 구체화되어 있습니다.

---

## 6. consent와 gradual enforcement를 핵심 abstraction으로 취급한다

정책 자동화를 바로 blocking rule로 적용하지 않고 다음 단계를 거칩니다.

```text
관측 → recommendation → replay → warning → enforcement
```

현재 공개 설명상 v0.8 계열에서는 warning도 non-blocking이며, managed agent와 project hook 설치도 preview와 consent를 요구합니다. ([GitHub][1])

Adaptive agent tooling에서 매우 중요한 속성입니다. 사용자의 실제 workflow를 학습한다는 명목으로 자동 생성된 policy가 곧바로 개발을 차단하는 시스템은 운영상 위험합니다.

---

## 7. hook 설계가 project-scoped이고 비교적 보수적이다

등록된 hook은 다음 범위로 제한됩니다.

* session context injection
* compaction/session end 시 resume snapshot
* task file 읽기 유도
* Write/Edit 이후 task validation 및 roadmap regeneration

그리고 `.waystone.yml`이 없는 프로젝트에서는 fast no-op을 의도합니다.

hook이 모든 shell command나 git operation을 가로채는 구조가 아니라는 점은 긍정적입니다. 전역 플러그인이 다른 프로젝트의 동작을 오염시킬 위험을 줄입니다.

---

## 8. CI가 최소한의 계약 검증 이상을 수행한다

CI는 다음을 검사합니다.

* Claude/Codex plugin manifest 버전 일치
* hook 종류와 launcher 존재 여부
* test suite
* 실제 Codex CLI에 checkout을 marketplace plugin으로 설치하는 smoke test

특히 install smoke test는 manifest의 정적 validation보다 실질적인 가치가 있습니다.

테스트 코드도 임시 Git repository와 worktree를 사용하여 release, remote push, SHA binding, review cycle 등의 통합 동작을 검사하는 형태입니다. 단순 unit mocking만 하는 프로젝트보다는 신뢰도가 높습니다.

---

# 약점과 리스크

## 9. 가장 큰 약점은 과도한 개념적·운영적 복잡도다

Waystone이 사용하는 핵심 개념은 상당히 많습니다.

* SSOT
* task registry
* milestone
* round
* review packet
* PR review cycle
* delegation record
* immutable snapshot
* implementer binding
* verifier binding
* execution backend
* verdict
* criterion evidence
* overlay
* replay
* consent log
* lane
* warning level
* generated digest
* re-entry snapshot

각 개념은 개별적으로 합리적이지만, 전체를 동시에 이해해야 정상 운영할 수 있습니다.

`delegate` skill 하나만 약 290줄이며, 8개의 routing question, 6단계 workflow, 10개의 exhaustive escalation condition, 여러 JSON schema와 CLI subcommand를 포함합니다.

이는 다음 위험을 만듭니다.

1. 모델이 instruction의 일부를 누락할 가능성
2. 사용자가 상태를 직관적으로 파악하기 어려움
3. failure가 코드 버그인지 workflow invariant 위반인지 구분하기 어려움
4. Waystone을 운영하는 비용이 실제 개발 비용보다 커지는 소규모 프로젝트 발생
5. 구현자 본인 외 contributor의 진입 장벽 증가

현 상태는 **“workflow harness”라기보다 작은 agentic development operating system**에 가깝습니다.

---

## 10. 제품이 여러 상이한 문제를 한 플러그인 안에서 동시에 해결한다

현재 범위에는 적어도 다음 제품이 겹쳐 있습니다.

* 프로젝트 ideation assistant
* project memory system
* task registry
* roadmap generator
* project dashboard
* work-cycle manager
* external review pipeline
* PR merge gate
* coding delegation runner
* independent verifier
* session-log analytics
* adaptive policy engine
* multi-project status manager

각 기능 간 철학은 일관되지만, product boundary는 불명확합니다. 이 때문에 사용자는 Waystone의 핵심 가치가 무엇인지 즉시 판단하기 어렵습니다.

객관적으로 가장 차별화된 부분은 다음 두 가지입니다.

1. **delegation → independent verification → main-session verdict**
2. **review feedback를 사실이 아닌 검증할 claim으로 취급**

반면 ideation, roadmap, status dashboard, session memory는 이미 유사한 도구가 많은 영역입니다. 핵심 차별점이 부가 기능에 묻힐 수 있습니다.

---

## 11. 문서와 manifest 간 버전 정합성이 깨져 있다

현재 `plugin.json`의 버전은 **0.10.0**입니다.

하지만 기본 branch의 README는 다음과 같이 설명합니다.

* “v0.8.2 is implemented”
* “v0.8 — current release”
* “v0.9 planned”

([GitHub][1])

이는 외부 사용자 관점에서 명백한 릴리스 메타데이터 결함입니다. 다음 중 무엇인지 판단할 수 없습니다.

* manifest만 먼저 0.10.0으로 올라감
* README가 오래됨
* 0.9와 0.10 기능이 구현됐지만 문서가 누락됨
* main이 development snapshot임

기능적 버그는 아니지만, 프로젝트가 auditability를 강조한다는 점을 고려하면 신뢰를 훼손하는 부분입니다.

---

## 12. release/distribution 체계가 아직 오픈소스 제품 수준으로 정리되지 않았다

현재 저장소 페이지에는 별도 GitHub Release가 없고, 기본 branch README는 marketplace repository를 통해 설치하도록 안내합니다. ([GitHub][1])

이 방식은 Claude Code plugin 생태계에서는 가능하지만, 다음이 부족합니다.

* version별 changelog
* immutable release artifact
* upgrade/migration guide
* compatibility matrix
* rollback 절차
* supported Claude Code/Codex version
* breaking-change policy
* signed or checksummed artifact
* release provenance

특히 stateful tool은 schema migration과 backward compatibility가 중요합니다. `.waystone.yml`, task schema, delegation artifacts, consent logs가 누적되므로 단순 plugin update보다 훨씬 엄격한 migration 정책이 필요합니다.

---

## 13. 테스트는 진지하지만, 배포 branch에서 테스트를 제거하는 방식은 검증 가능성을 낮춘다

README는 테스트와 개발 도구가 `dev`에 있고 `main`은 distributable runtime이라고 명시합니다. ([GitHub][1])

CI에서도 `main`에 직접 push된 경우 test suite를 생략하고 contract 및 smoke test만 실행합니다. test job은 PR 또는 `main`이 아닌 branch에서만 실행됩니다.

release projection으로 runtime과 test source를 분리하는 의도는 이해되지만 단점이 있습니다.

* 사용자가 배포된 정확한 source tree와 대응되는 테스트를 같은 commit에서 보기 어려움
* supply-chain audit가 복잡해짐
* main에 직접 발생한 문제를 full suite가 잡지 못할 수 있음
* GitHub source archive가 완전한 개발 source가 아님
* 외부 contributor가 기본 branch만 보고 재현하기 어려움

더 일반적인 방식은 test를 repository에 남겨두고 package artifact에서만 제외하는 것입니다.

---

## 14. 테스트 파일의 설명과 현재 버전 사이에도 상당한 drift가 있다

`run_tests.py`의 docstring은 여전히 “waystone v0.2.0 correctness kernel”이라고 설명하지만, 내부 import는 delegate, overlay, lanes, dashboard, resume 등 훨씬 이후의 기능을 포함합니다.

기능상 치명적이지는 않지만 다음 가능성을 시사합니다.

* 오래된 문서 문자열이 정리되지 않음
* 기능 추가 속도에 비해 documentation hygiene가 따라가지 못함
* release/version naming이 코드베이스 전반에서 체계적으로 관리되지 않음

앞서 언급한 README 0.8.2와 manifest 0.10.0 불일치와 같은 종류의 문제입니다.

---

## 15. hook의 비용과 간섭 범위를 실제 측정으로 증명해야 한다

hook description은 non-Waystone project에서 `<10ms` no-op이라고 주장합니다.

그러나 공개적으로 확인한 범위에서는 다음에 대한 benchmark artifact가 보이지 않습니다.

* 평균·p95 hook latency
* 대형 monorepo에서 PostToolUse 비용
* `tasks.yaml`이 큰 경우 validation 시간
* session context injection token overhead
* compaction 직전 snapshot failure rate
* hook script failure 시 Claude Code 본체에 주는 영향

특히 `PostToolUse`가 모든 `Write|Edit` 후 실행되고 timeout이 30초인 점은 주의가 필요합니다. 실제 script가 파일 path를 빠르게 filtering한다면 문제없지만, 성능 claim은 테스트나 benchmark로 공개하는 것이 적절합니다.

---

## 16. UX가 “모델이 instruction을 완벽히 이행한다”는 가정에 다소 의존한다

CLI에 많은 invariant가 들어간 것은 장점입니다. 하지만 일부 핵심 동작은 여전히 skill instruction 준수에 의존합니다.

예:

* 정확히 8개의 routing question을 policy order로 검토
* acceptance criterion을 owner-authored material에서만 합성
* bounded material만 읽기
* escalation condition 외에는 사용자에게 묻지 않기
* delegate claim과 harness fact의 언어적 구분
* 보고서에서 특정 내부 용어 제거
* record pointer를 정확히 한 번만 표시

이런 규칙은 formal enforcement가 아니라 prompt-level protocol인 부분이 많습니다.

즉, 시스템은 deterministic core를 갖고 있지만 **전체 workflow의 correctness는 여전히 LLM policy adherence에 상당 부분 의존**합니다.

개선 방향은 instruction을 더 늘리는 것이 아니라, 가능한 규칙을 state machine과 typed CLI operation으로 옮기는 것입니다.

---

## 17. profile과 role routing abstraction이 현재 지원 기능보다 앞서 있다

설계는 role과 model name을 분리하고 다음 execution type을 다룹니다.

* main-session
* clean-subagent
* forked-subagent
* deterministic-workflow
* external-runner

그러나 실제 `delegate run`은 implementer + external-runner 조합만 지원하며, 다른 조합은 host-native mechanism으로 넘깁니다.

이 abstraction은 향후 확장성 측면에서는 좋지만, 현재 사용자에게는 **일반화된 설정 모델에 비해 실제 실행 backend가 제한적인 상태**로 보일 수 있습니다.

즉, architecture는 v1 이후를 준비하지만 현재 product surface는 아직 그 추상화를 충분히 정당화하지 못합니다.

---

## 18. 보안 모델을 별도 문서로 명확히 해야 한다

좋은 안전장치는 존재합니다.

* worktree isolation
* unsandboxed Claude runner 기본 거부
* explicit consent 요구
* path scope 기록
* patch 기반 apply
* 사용자 작업 자동 stash 금지
* worker와 verifier 역할 분리

특히 `claude:<model>` external backend를 structurally unsandboxed로 간주하고 명시적 override를 요구하는 것은 적절합니다.

그러나 다음 공격면에 대한 명시적 threat model이 필요합니다.

* 악의적인 repository instruction
* malicious lockfile/setup script
* symlink 및 path traversal
* worktree 밖 파일 접근
* environment variable 및 credential leakage
* verifier와 implementer가 같은 provider/context를 공유할 때의 correlated failure
* generated patch의 binary/submodule 처리
* hooks가 조작된 local config를 신뢰하는 문제
* session log 분석 시 민감정보 노출
* Git hook, npm lifecycle, Python build backend 등의 implicit code execution

현재 README의 철학 설명만으로는 security-sensitive delegation runner의 경계를 충분히 평가하기 어렵습니다.

---

# 아키텍처에 대한 판단

## 19. 구조적으로 가장 성공적인 부분

Waystone의 핵심 pipeline은 다음처럼 요약할 수 있습니다.

```text
Owner intent
   ↓
Durable task + acceptance criteria
   ↓
Immutable base snapshot
   ↓
Isolated implementation
   ↓
Harness-computed patch
   ↓
Independent verification
   ↓
Criterion-by-criterion verdict
   ↓
Explicit apply / discard
   ↓
Review and workflow evidence
```

이 pipeline은 논리적으로 건전합니다.

특히 다음 세 property가 좋습니다.

### Provenance

각 정보가 누가 또는 무엇에 의해 생성됐는지 구분합니다.

### Commit binding

검토한 code와 merge되는 code가 같은 SHA인지 확인하려 합니다.

### Monotonic audit trail

실패 record나 이전 attempt를 재작성하기보다 새 artifact로 누적합니다.

이 세 가지는 장기적으로 agent reliability 연구에도 의미 있는 설계입니다.

---

## 20. 구조적으로 가장 위험한 부분

Waystone은 다음 네 층을 동시에 유지합니다.

```text
LLM skill protocol
CLI state machine
Git/worktree state
Markdown/YAML/JSON artifact graph
```

각 층 사이의 consistency invariant가 많습니다.

예를 들어 task 상태, delegation 상태, Git base SHA, patch, verifier artifact, verdict, progress log, review packet이 서로 맞아야 합니다. 기능이 늘어날수록 state-space가 빠르게 커집니다.

따라서 장기적으로는 feature 추가보다 아래가 더 중요합니다.

* explicit finite-state machine
* schema version migration
* transaction boundary
* crash recovery
* idempotency proof
* invariant checking
* garbage collection
* artifact lineage query

현재의 방향은 좋지만, 이 복잡도를 계속 skill text와 subcommand 추가로 관리하면 유지보수가 어려워질 가능성이 높습니다.

---

# 구체적인 개선 우선순위

## P0 — 신뢰성 및 릴리스 정합성

1. **README, manifest, roadmap, test docstring의 버전을 즉시 통일**
2. GitHub Release와 `CHANGELOG.md` 제공
3. 지원 Claude Code/Codex 버전 명시
4. artifact/schema compatibility 및 migration policy 문서화
5. `main`에 대응하는 test source를 같은 tag 또는 별도 source archive로 제공
6. 기본 branch 직접 push에도 full test suite 실행

이 항목들은 기능 추가보다 우선입니다.

---

## P1 — 제품 범위 축소 및 onboarding

핵심 제품을 다음 세 단계로 재구성하는 것이 좋습니다.

### Core

* task registry
* round
* review evidence

### Delegation

* isolated runner
* verifier
* verdict
* apply/discard

### Adaptive layer

* improve
* overlay
* replay
* policy promotion

사용자는 Core만 설치·사용할 수 있어야 하며, Delegation과 Adaptive 기능은 명시적으로 활성화하는 편이 좋습니다.

현재 init 과정에서도 선택지가 있지만, 개념과 문서상으로는 모든 기능이 하나의 큰 시스템처럼 노출됩니다.

---

## P1 — protocol을 코드로 더 이동

다음을 skill instruction이 아니라 CLI가 강제하도록 개선할 가치가 있습니다.

* delegation state transition
* allowed retry count
* verifier requirement
* criterion completeness
* blocker override rules
* report data model
* escalation reason enum
* routing decision record
* exact artifact lineage

최종 사용자 보고서 역시 LLM이 자유 생성하기보다 CLI가 machine-readable summary를 만들고 모델이 이를 간단히 표현하게 하는 편이 안정적입니다.

---

## P1 — 명시적인 threat model

최소 다음을 포함하는 `SECURITY.md`가 필요합니다.

| 영역               | 필요한 명시                                             |
| ---------------- | -------------------------------------------------- |
| Repository trust | 프로젝트 내 instruction과 executable file을 어느 수준으로 신뢰하는가 |
| Environment prep | 어떤 lockfile command가 실행될 수 있는가                     |
| Credentials      | worker 환경에 어떤 token/env가 전달되는가                     |
| Sandbox          | Codex와 Claude backend의 실제 격리 차이                    |
| Filesystem       | project root 밖 읽기·쓰기 통제                            |
| Git              | submodule, LFS, binary, symlink 처리                 |
| Logs             | prompt/session log 저장·비식별화 정책                      |
| Verification     | implementer-verifier independence의 한계              |

---

## P2 — 성능 및 효과를 정량화

Waystone의 가치를 주장하려면 다음 지표가 유용합니다.

* session re-entry 후 task reconstruction 시간
* context injection token 수
* hook p50/p95 latency
* delegation apply/discard 비율
* verifier가 발견한 blocker 비율
* worker self-report와 independent verification 불일치율
* review issue 재발률
* Waystone 사용 전후 intent drift incidence
* workflow overhead/time-to-merge 변화

특히 “evidence-centered workflow가 실제 오류를 얼마나 줄였는가”를 보여주면 단순 productivity plugin보다 연구·엔지니어링 도구로서 차별성이 커집니다.

---

# 누구에게 적합한가

## 적합

* 여러 세션에 걸친 연구·개발 프로젝트
* agent delegation을 자주 사용하는 개인 개발자
* 결과보다 provenance와 verification을 중시하는 프로젝트
* Git/worktree와 structured artifact에 익숙한 사용자
* 모델 실행 비용보다 잘못된 변경의 비용이 더 큰 환경
* AI coding workflow 자체를 연구하거나 실험하는 사용자

## 과도할 가능성이 큼

* 작은 단일 세션 작업
* 간단한 CRUD 프로젝트
* Git과 structured task registry를 선호하지 않는 팀
* 즉각적인 자동화와 최소 설정을 원하는 사용자
* 이미 Linear/Jira/GitHub Projects와 강하게 통합된 조직
* Claude Code를 단순 코드 생성 도구로만 사용하는 사용자

---

# 최종 판단

Waystone은 **평범한 Claude Code plugin보다 설계 수준이 높습니다.** 특히 다음은 실질적인 강점입니다.

* completion claim과 evidence의 분리
* independent verifier
* SHA-bound review
* immutable delegation record
* main-session ownership
* non-destructive adoption
* gradual policy enforcement

반면 현재는 **좋은 아이디어가 너무 많은 기능으로 확장된 상태**입니다. 가장 큰 리스크는 개별 알고리즘의 오류보다 시스템 전체의 복잡성입니다.

객관적으로 표현하면:

> **연구 지향적이고 안전성에 민감한 agentic development harness로서는 설득력이 높다. 하지만 일반 사용자를 위한 안정된 플러그인 제품으로 보기에는 릴리스 정합성, 문서 구조, 보안 모델, UX 단순화가 아직 부족하다.**

향후 성공 여부는 v0.11이나 v0.12에 기능을 더 넣는 것보다, 기존 기능을 다음 세 축으로 얼마나 압축하느냐에 달려 있습니다.

```text
작은 core state machine
명확한 trust boundary
낮은 사용자 인지 부하
```

현재 코드와 문서만 기준으로 보면 **“매우 유망한 advanced prototype이자 실제 사용 가능한 early product”**에 가장 가깝습니다.

[1]: https://github.com/Dev-Jahn/waystone "GitHub - Dev-Jahn/waystone · GitHub"
