# pre-1.0.0-refactor-proposal

**대상:** `Dev-Jahn/waystone`  
**분석 기준 브랜치:** `dev`  
**분석 스냅샷:** `d9070e2b8ca03db1782aecdd9dc208e28f7a6485` (2026-07-18)  
**문서 상태:** 설계 제안서 — 구현 계획과 1.0 진입 기준을 포함하되, 세부 API는 후속 ADR에서 확정  
**분석 범위:** 현재 버그 목록이 아니라 제품 경계, 핵심 불변조건, 상태·실행 모델, UX, 용어, README, 릴리스/CI-CD 구조

---

## 0. 결론

Waystone의 문제 선택과 신뢰성 철학은 유효하다. 특히 다음 네 가지는 1.0에서도 제품의 정체성으로 보존해야 한다.

1. **프로젝트 의도와 작업 전선이 세션보다 오래 살아남는다.**
2. **worker의 완료 주장과 harness가 계산한 사실을 구분한다.**
3. **구현, 독립 검증, 최종 수용 결정을 분리한다.**
4. **Git 상태와 artifact provenance를 기준으로 변경을 감사·복구할 수 있다.**

현재 문제는 기능이 많다는 사실만이 아니다. 더 근본적으로는 같은 불변조건이 다음 네 층에 반복 표현되어 있다는 점이다.

```text
skill의 자연어 프로토콜
    × Python CLI의 상태 검사
    × Git/worktree 상태
    × Markdown/YAML/JSON artifact 관계
```

이 중복 때문에 새로운 기능 하나를 추가할 때마다 모든 층의 예외 경로와 정합성을 다시 방어해야 한다. 따라서 pre-1.0 리팩터의 목적은 기능 삭제가 아니라 다음과 같아야 한다.

> **Waystone을 “많은 workflow 기능의 모음”에서 “하나의 durable run engine과 그 위의 얇은 UX”로 재구성한다.**

가장 중요한 권고는 다음과 같다.

| 결정 | 제안 |
|---|---|
| Waystone의 핵심 정의 | **세션과 worker를 넘어 지속되는, evidence-gated agentic development run engine** |
| `round`의 미래 | 자율화에 찬성. 단, 더 큰 skill prompt가 아니라 **resumable 상태 기계**로 구현 |
| 병렬 실행 | task별 live-tree apply가 아니라 **run 전용 integration worktree**에서 wave 단위로 통합 |
| 새로 unblock된 task | run 시작 시 승인된 **bounded closure** 안에서만 자동 편입 |
| 공개 surface | 기본적으로 `init`, `run`, `status`, 필요 시 `review`; 나머지는 advanced/내부 |
| 역할 모델 | `coordinator`, `worker`, `verifier`, `reviewer` 네 책임으로 축소 |
| 실행 모델 | host 세부 구현을 숨기고 `in-session`, `subagent`, `external` 세 범주로 축소 |
| runtime state | Git-tracked intent와 local transactional runtime state를 분리 |
| prompt 주입 | 전역 운영 계약과 8개 routing question을 제거하고 **작은 re-entry capsule**만 주입 |
| `improve`/overlay | 유지하되 core path에서 분리하고 “policy proposal”이라는 보조 기능으로 격리 |
| 릴리스 | source branch와 generated distribution branch를 명확히 분리하고 marketplace는 exact SHA로 pin |
| 1.0 기준 | 기능 수가 아니라 crash recovery, migration, source↔dist provenance, 낮은 인지 부하로 정의 |

---

## 1. 분석에서 확인한 현재 구조

### 1.1 첨부 평가에서 타당한 판단

첨부 평가는 다음 핵심을 정확히 짚었다.

- Waystone의 차별점은 task manager가 아니라 **claim과 evidence의 분리**에 있다.
- independent verifier, SHA-bound review, immutable record, main-session ownership은 실질적 강점이다.
- 결정적 작업을 script로 내린 방향은 올바르다.
- 가장 큰 장기 리스크는 단일 버그보다 **누적된 개념·운영 복잡도**다.
- prompt-level protocol을 더 늘리는 대신 상태 기계와 typed operation으로 옮겨야 한다.
- 릴리스, 보안 경계, onboarding, 공개 문서의 성숙도가 기능 추가보다 우선되어야 한다.

이 판단들은 `dev`의 실제 구현을 보더라도 대체로 유지된다.

### 1.2 첨부 평가보다 현재 `dev`가 앞서 있는 부분

평가가 참조한 `main`보다 `dev`는 상당히 진전되어 있다.

- README와 manifest는 v0.10의 role/execution/backend binding, four-layer policy, longitudinal metrics를 설명한다.
- `delegate plan`은 task packet digest와 profile fingerprint를 고정한 fan-out manifest를 만든다.
- worker 결과, verifier artifact, verdict, apply 사이에 digest chain과 provenance 검사가 있다.
- `round close`는 task/config/generated view/exposure를 하나의 rollback 가능한 close transaction으로 취급한다.
- current fan-out carrier는 자신의 결과가 권위가 없음을 명시하고, verdict/apply를 main-session의 직렬 책임으로 남긴다.
- init은 “단일 SSOT가 없는 프로젝트”를 명시적으로 지원한다.
- release projection은 allowlist 기반 positive manifest로 runtime tree를 생성하고 projected tree 자체를 smoke-test한다.

따라서 현 상태를 단순히 “문서가 낡고 protocol이 느슨한 0.10.0”으로 평가하면 부정확하다. 현재 구현은 신뢰성 문제를 진지하게 다루고 있다.

### 1.3 첨부 평가보다 더 근본적인 문제

반대로 `dev`를 보면 복잡성 문제는 평가에서 표현한 “기능이 너무 많다”보다 구조적이다.

#### A. 하나의 작업 완료가 여러 권위면을 통과한다

현재 한 delegated task는 대략 다음 데이터를 오간다.

```text
tasks.yaml
→ packet.yaml / claim.json / exposure.json / status.json / prompt.txt
→ result ref / patch / contract.yaml
→ verify-N.json
→ verdict-N.json
→ apply/discard state
→ round exposure
→ PROGRESS / ROADMAP / review request
→ improve evidence / metrics / overlay observation
```

각 artifact는 이유가 있다. 문제는 이들을 하나의 canonical operation log가 아니라 여러 파일과 호출 순서가 결속한다는 점이다. 정합성 규칙은 강하지만, 규칙 수가 계속 증가한다.

#### B. skill이 아직 orchestration의 일부를 기억해야 한다

`delegate`는 한 task를 상당히 자율적으로 처리하지만, skill은 여전히 다음을 자연어 절차로 수행한다.

- 8개 routing question 검토
- acceptance/scope 보강
- run 결과 분류와 제한된 retry
- verifier 호출
- criterion별 verdict 작성
- apply/discard
- exhaustive escalation condition 판별

`round`도 close CLI 이전·이후의 progress 작성, commit/push, review narrative 생성, re-entry pointer 갱신을 skill이 조율한다. 즉 deterministic core는 강하지만 **workflow 전체 상태는 아직 model turn 안에 부분적으로 존재한다.**

#### C. 역할과 실행 abstraction의 곱이 크다

현재 profile은 여섯 role과 다섯 execution type을 구분한다.

```text
roles:
  main, orchestrator, implementer, clerk, verifier, reviewer

executions:
  main-session, clean-subagent, forked-subagent,
  deterministic-workflow, external-runner
```

실제 Waystone이 직접 실행하는 것은 `external-runner`뿐이고 나머지는 host-guided다. 이 모델은 표현력은 높지만, 사용자와 구현 모두에게 큰 상태 공간을 만든다. 특히 `main`과 `orchestrator`, `clerk`와 deterministic step의 경계는 제품 핵심을 위해 반드시 필요한 구분이 아니다.

#### D. global session prompt가 제품 철학과 충돌한다

SessionStart는 최대 8,000자 안에서 다음을 함께 주입할 수 있다.

- main operating contract
- 8개 routing question의 식별자
- 6개 role 설명
- overlay 상태
- needs-review delegation
- evidence 상태
- pending review
- START_HERE
- active/blocked/decision tasks
- next actionable
- SSOT digest

크기 제한이 있다는 점은 좋다. 그러나 사용자가 제시한 불변 지향점은 **harness-derived prompt를 최소화하고 모델 고유의 판단 공간을 보존하는 것**이다. 현재 구조는 제한된 크기 안에 많은 운영 규칙을 항상 주입한다는 점에서 이 목표와 긴장 관계에 있다.

#### E. fan-out은 병렬 dispatch이지 자율 run이 아니다

현재 carrier는 다음만 수행한다.

- manifest 검증
- scope 기반 lane 구성
- 여러 `delegate run` 시작
- 결과 pointer 집계

그리고 의도적으로 다음은 하지 않는다.

- verify
- verdict
- apply/discard
- retry
- downstream task unblock 후 재계획
- round close

이는 현재 safety boundary로서는 올바르다. 다만 “round 전체를 자율 실행”하려면 carrier를 확장하는 것이 아니라 **carrier 위에 durable scheduler와 integration state를 추가**해야 한다.

---

## 2. 1.0에서 고정할 제품 정의

### 2.1 제안하는 한 문장

> **Waystone은 장기 agentic development를 세션과 worker 사이에서 중단 없이 이어가고, 변경을 독립 검증한 뒤 하나의 감사 가능한 결과로 통합하는 workflow harness다.**

더 짧은 public copy는 다음이 적절하다.

> **Keep long-running agent development coherent across sessions and workers.**

현재의 “Agents forget. Projects shouldn’t.”는 유지할 가치가 있다. 다만 바로 아래 설명은 기능 열거가 아니라 다음 세 결과를 명시해야 한다.

1. **resume without reconstruction**
2. **parallel work without losing ownership**
3. **integration without trusting self-reported completion**

### 2.2 핵심 제품 약속

#### Promise 1 — Continuity

- project intent, current frontier, unresolved decisions가 session transcript에 종속되지 않는다.
- 새 session은 작은 handoff capsule만으로 작업을 재개한다.
- model memory가 아니라 project state가 권위다.

#### Promise 2 — Bounded execution

- task 또는 goal은 명시적 scope, acceptance, dependency 안에서 실행된다.
- worker는 격리된 환경에서 실행되며 live user tree를 임의로 변경하지 않는다.
- run의 범위와 종료 조건이 시작 시 고정된다.

#### Promise 3 — Independent evidence

- worker claim, harness fact, verifier finding, coordinator decision이 구분된다.
- “test를 했다고 말함”은 “test가 성공함”이나 “변경이 올바름”으로 승격되지 않는다.
- 수용 결정은 criterion별 evidence에 묶인다.

#### Promise 4 — Smooth handoff

- 여러 worker의 결과는 하나의 integration surface로 모인다.
- 세션이 끊겨도 run이 어느 단계인지, 무엇이 끝났고 무엇이 막혔는지 복구된다.
- 사용자는 내부 artifact graph를 알 필요 없이 결과, blocker, 필요한 결정만 본다.

### 2.3 1차 사용자 정의

현재 SSOT는 작성자 자신 또는 소수 사용자에 맞춘 개인 harness라고 명시한다. 1.0 전에는 이를 다음처럼 조금 넓히는 것이 좋다.

> **Primary:** 여러 session과 agent를 사용하는 개인 개발자 및 소규모 팀.  
> **Secondary:** provenance와 검증 비용이 중요한 연구·고신뢰 프로젝트.  
> **Not yet:** 조직 전체의 task management, multi-tenant governance, 범용 CI 대체.

“범용성”은 Jira, Linear, GitHub Projects, 모든 model host를 즉시 지원한다는 의미가 아니다. 핵심 kernel의 입력·출력 interface가 작고 host adapter가 분리되어 있다는 의미여야 한다.

---

## 3. 보존해야 할 불변조건

리팩터 중 아래 항목은 구현 형태가 바뀌어도 약화시키지 않는다.

| ID | 불변조건 |
|---|---|
| I-01 | owner-authored intent와 acceptance가 worker가 만든 주장보다 우선한다. |
| I-02 | worker는 자신의 변경을 최종 수용할 수 없다. |
| I-03 | changed files, patch bytes, base/result SHA, digest는 Git/harness가 계산한다. |
| I-04 | verifier evidence와 integration decision은 별도 artifact/actor로 남는다. |
| I-05 | live user work를 자동 stash, silent commit, silent 3-way apply하지 않는다. |
| I-06 | 실패·재시도·discard 이력을 덮어쓰지 않는다. |
| I-07 | 기존 프로젝트 도입과 migration은 non-destructive, previewable, idempotent다. |
| I-08 | 새로운 policy는 observe → warn → enforce로 점진 승격하며 consent와 waiver를 기록한다. |
| I-09 | state corruption이나 provenance 불일치는 success로 degrade하지 않는다. |
| I-10 | 모델에는 목표, 경계, 판단 요청만 전달하고 bookkeeping protocol은 전달하지 않는다. |
| I-11 | host capability가 부족하면 다른 실행 형태로 조용히 가장하지 않는다. |
| I-12 | public UX는 내부 safety machinery의 상세를 요구하지 않는다. |

특히 I-10과 I-12를 기존 안전성 원칙과 같은 급으로 올려야 한다. 지금까지는 correctness invariant가 주로 강화되었고, prompt minimality와 cognitive load는 상대적으로 후순위였다.

---

## 4. 제안 아키텍처: One Run Engine

### 4.1 목표 구조

```text
┌─────────────────────────────────────────────────────────┐
│ User / Claude Code / Codex                              │
│   init · run · status · review                          │
└───────────────────────┬─────────────────────────────────┘
                        │ thin command / typed request
┌───────────────────────▼─────────────────────────────────┐
│ Waystone Run Engine                                    │
│                                                         │
│  Run FSM ─ Scheduler ─ Job FSM ─ Evidence ─ Integration │
│     │          │          │          │          │        │
│     └──────────┴──────────┴──────────┴──────────┘        │
│                 transactional event journal              │
└─────────────┬───────────────────────┬───────────────────┘
              │                       │
      ┌───────▼────────┐      ┌───────▼────────────────┐
      │ Project store  │      │ Execution adapters     │
      │ config/tasks   │      │ host subagent/workflow │
      │ brief/decisions│      │ codex/claude external  │
      └───────┬────────┘      └────────┬───────────────┘
              │                        │
      ┌───────▼────────┐      ┌────────▼───────────────┐
      │ Projections    │      │ Run integration tree  │
      │ roadmap/status │      │ snapshots/patches/tests│
      │ handoff/review │      └────────────────────────┘
      └────────────────┘
```

핵심은 모든 기능을 하나의 거대 module에 넣는 것이 아니다. **모든 변경형 operation이 동일한 run/job state machine과 journal을 사용하게 하는 것**이다.

### 4.2 두 종류의 state만 둔다

#### A. Git-tracked intent state

사람과 agent가 장기간 공유해야 하는 내용이다.

- `.waystone.yml`
- `tasks.yaml`
- optional project brief/spec path
- ADR/decision documents
- 선택적으로 공유할 review request/feedback
- consent를 거쳐 publish한 project policy

#### B. local transactional runtime state

실행 중인 orchestration과 기계적 evidence다.

- run/job state
- leases and attempts
- snapshots and digests
- worker/verifier outputs
- integration decisions
- local consent evidence
- metrics and policy observations

제안 저장 형태:

```text
.waystone/
  state.db                 # canonical runtime journal + projections index
  artifacts/
    sha256-...             # patch, logs, reports, schemas; content-addressed
  exports/
    run-<id>.json          # portable/audit export on demand
```

Python 표준 라이브러리 SQLite를 사용하면 다음을 한 번에 얻는다.

- transaction
- uniqueness constraint
- crash-safe commit
- cross-process coordination 보조
- schema migration
- queryable lineage
- 수십 개 sidecar 간 부분 write 감소

SQLite가 불투명하다는 단점은 `waystone inspect`, `waystone export`, human-readable generated summary로 완화한다. `tasks.yaml`과 project direction은 계속 Git에서 사람이 읽을 수 있어야 한다. 즉 task registry까지 DB로 옮기자는 제안이 아니다.

SQLite 채택이 과도하다고 판단되면 대안은 append-only `events.jsonl` + content-addressed artifact store다. 그러나 현재 sidecar 수와 cross-process locking 요구를 고려하면 SQLite가 더 작은 failure surface를 제공할 가능성이 높다.

### 4.3 Run state machine

```text
created
  → planning
  → ready
  → executing
  → reconciling
  → final-verification
  → publishing
  → completed

any nonterminal state
  → waiting-user
  → resumed into prior phase

any nonterminal state
  → failed | cancelled
```

`waiting-user`는 failure가 아니다. 다음과 같은 typed reason을 가진 durable state다.

- `criteria-ambiguous`
- `scope-ambiguous`
- `security-consent-required`
- `unrefuted-blocker`
- `integration-conflict`
- `retry-budget-exhausted`
- `host-capability-unavailable`
- `live-tree-drift`
- `external-approval-required`

Skill이 자연어로 exhaustive table을 기억하는 대신 engine이 enum으로 상태를 반환한다.

### 4.4 Job state machine

하나의 task attempt는 다음 상태를 가진다.

```text
queued
  → preparing
  → executing
  → produced
  → verifying
  → deciding
  → accepted
  → integrated
  → succeeded

terminal alternatives:
  failed-env
  failed-worker
  failed-artifact
  rejected
  cancelled
```

재시도는 같은 job을 되감지 않는다.

```text
job A attempt 1: failed-worker
job A attempt 2: succeeded
```

Run은 현재 attempt pointer만 갱신하고 모든 attempt를 journal에 보존한다. 기존 immutable attempt 원칙을 유지하면서 파일 naming과 상태 검사 로직을 단순화할 수 있다.

### 4.5 Canonical operation API

모든 skill과 host adapter는 다음 수준의 operation만 사용한다.

```text
run.create(scope, policy, base)
run.plan(run_id)
run.next_actions(run_id)
run.submit_result(action_id, result)
run.continue(run_id)
run.cancel(run_id, reason)
run.status(run_id)
run.export(run_id)
```

내부 action type 예:

```text
execute_job
verify_job
judge_job
integrate_job
reconcile_conflict
run_checks
publish_review
request_user_decision
```

각 action은 immutable input digest를 갖는다. host/skill은 action을 수행할 뿐, 다음 상태를 스스로 선택하지 않는다. action result를 engine에 제출하면 engine이 transition을 결정한다.

이 구조는 Claude Workflow carrier와도 잘 맞는다. carrier는 `delegate run` 여러 개를 직접 조합하는 대신 `run.next_actions`가 준 transport-safe action을 수행하고 결과를 돌려준다. 권위는 계속 engine에 있다.

---

## 5. 자율 `round`에 대한 판단

### 5.1 방향에는 찬성한다

현재 `round`는 이름상 “한 autonomous work cycle”이지만 실제 public skill은 주로 closeout과 review publication을 수행한다. 반면 사용자가 기대하는 round는 다음을 포함한다.

```text
scope 결정
→ ready task 선택
→ 병렬 dispatch
→ 각 결과 검증·수용
→ 통합
→ 새로 unblock된 task dispatch
→ run-level 검증
→ review/close/handoff
```

이것이 Waystone의 핵심 문제 정의에 더 잘 맞는다. `delegate`가 한 task를 자율 처리하고 `round`가 close만 하는 현재 분리는 사용자 관점에서 인위적이다.

### 5.2 다만 “모든 non-blocked task”를 기본값으로 삼지 않는다

현재 global backlog에서 ready인 task를 전부 자동 소비하면 다음 문제가 생긴다.

- 사용자가 이번 작업에서 의도하지 않은 영역까지 변경
- 무한히 새 task가 생성·unblock되는 run
- unrelated task 간 integration risk 증가
- 비용·시간·network 권한의 경계 불명확
- backlog 우선순위가 사실상 scheduler policy가 됨

따라서 run은 시작 시 **bounded task closure**를 고정해야 한다.

지원할 scope selector:

```text
one task
one milestone
one explicit goal + derived task set
selected task ids
--all-ready              # 명시적 opt-in만
```

자동으로 새로 unblock된 task를 실행하는 조건:

1. 시작 시 frozen closure에 포함되어 있다.
2. 모든 dependency가 run integration state에서 완료되었다.
3. acceptance와 scope가 이미 충분하다.
4. remaining budget와 parallelism policy 안에 있다.
5. 새 user decision이나 security consent가 필요하지 않다.

closure 밖에서 발견된 문제는 task로 기록할 수 있지만 자동 실행하지 않는다. 단, 선택된 acceptance를 달성하는 데 필수이며 동일 scope 안인 경우 engine이 `scope-expansion-request`를 만들고 main이 판단할 수 있다.

### 5.3 전용 integration worktree를 둔다

자율 round를 현재 `delegate apply`의 반복으로 구현하면 live tree drift와 task 간 충돌이 핵심 failure point가 된다. 대신 run 하나가 다음을 소유한다.

```text
run base snapshot
run integration worktree/branch
parallel job worktrees
verified job patches
integration history
final deliverable patch/commit
```

#### Wave 1

- ready task를 scope-overlap과 dependency로 partition한다.
- disjoint task는 동일 run base에서 병렬 실행한다.
- unknown/overlap scope는 직렬 또는 별도 wave로 보낸다.

#### Per-job acceptance

- worker 결과를 harness가 patch/result SHA로 계산한다.
- verifier가 acceptance를 검사한다.
- coordinator decision이 accepted/rejected를 기록한다.

#### Integration

- accepted patch를 deterministic order로 integration worktree에 적용한다.
- 각 apply 후 task-specific check를 실행한다.
- patch conflict가 나면 user live tree가 아니라 run integration tree 안에서 해결한다.
- 자동 3-way 적용을 기본으로 하지 않는다. typed reconciliation action을 새 worker 또는 main judgment에 보낸다.

#### Next wave

- integration head를 새 base로 삼아 downstream task를 실행한다.
- dependency가 같은 run에서 끝나면 즉시 ready로 전환한다.

#### Final gate

- 전체 run acceptance
- cross-task tests
- scope/invariant scan
- independent run-level review가 설정된 경우 그 결과
- final tree digest

이후에만 사용자의 branch로 하나의 patch/commit을 전달한다. 이렇게 하면 “task N개가 live tree를 N번 mutate”하는 구조를 “run이 한 번 전달”하는 구조로 바꿀 수 있다.

### 5.4 사용자 개입 정책

자율 run은 사용자에게 자주 묻지 않아야 하지만, 무엇이든 결정해도 되는 것은 아니다.

| 상황 | 기본 처리 |
|---|---|
| worker 실행·환경 준비·routine retry | 자동 |
| task-specific test 선택·실행 | 자동 |
| verifier minor/major finding | acceptance와 evidence로 engine/main이 판정 |
| 명확히 refute된 finding | 근거를 기록하고 자동 계속 |
| blocker가 남음 | `waiting-user` 또는 task rejection |
| criteria/scope를 owner 자료에서 exact derivation 불가 | `waiting-user` |
| closure 밖 작업이 필요 | scope expansion 요청 |
| sandbox/credential/network 권한 상승 | 명시적 consent |
| integration conflict | bounded reconciliation attempt 후 unresolved면 사용자 |
| live branch가 run base에서 drift | final delivery 직전에 사용자에게 선택 제시 |
| 외부 human approval이 policy상 필수 | publishing 단계에서 대기 |

### 5.5 skill은 얇아져야 한다

새 `/waystone:run` 또는 호환 alias `/waystone:round` skill의 책임은 다음 정도여야 한다.

```text
1. user goal/task selector를 engine에 전달
2. engine이 반환한 model-judgment action만 수행
3. typed result를 engine에 다시 제출
4. waiting-user일 때 한 번의 명확한 질문
5. 완료 결과를 간단히 보고
```

상태 전이, retry count, artifact naming, apply 순서, escalation enum, resume logic은 skill에 쓰지 않는다. 결과적으로 skill은 수백 줄의 운영 매뉴얼이 아니라 수십 줄의 host adapter가 된다.

### 5.6 기존 `round` 이름 처리

표준 용어 관점에서는 `run` 또는 `workflow run`이 더 일반적이다. 권고안:

- 내부 canonical entity: **run**
- 문서 설명: **bounded work cycle**
- public command: pre-1.0에서 `/waystone:run` 도입
- `/waystone:round`는 migration alias로 유지
- 1.0에서도 브랜드상 `round`를 살리고 싶다면 UI alias로만 남기고 schema/API에는 쓰지 않는다.

---

## 6. 용어와 개념 축소

### 6.1 제안 vocabulary

| 현재 용어 | 제안 public 용어 | 제안 internal 용어 | 판단 |
|---|---|---|---|
| SSOT | project brief / project spec | intent source | “SSOT가 반드시 하나”라는 인상을 제거. 기존 파일명은 허용 |
| task registry | tasks | task store | registry라는 구현어를 public에서 제거 |
| round | run / work cycle | run | 표준 orchestration 용어 사용 |
| delegation | worker job | job / attempt | 한 task 실행 단위로 축소 |
| packet | task contract | job manifest | 입력 계약이라는 의미를 분명히 함 |
| claim | worker report | worker_claim | provenance 구분은 유지 |
| exposure | snapshot metadata | execution snapshot | 자의적 용어 제거 |
| contract.yaml | job result | job_result | “contract”의 입력/출력 혼동 제거 |
| verdict | integration decision | decision | 판정 대상과 actor를 명시 |
| apply/discard | accept/reject result | integration action | public에서는 단순화 |
| lane | parallel slot / wave | wave | DAG scheduler 표준 용어 사용 |
| overlay delta | policy proposal | policy proposal | adaptive 기능을 보조 기능으로 위치시킴 |
| compose | resolve policy | policy resolution | 표준 config 용어 |
| materialize | publish policy | policy publication | 무엇이 생기는지 명확 |
| START_HERE / resume snapshot | handoff | handoff projection | 한 개념으로 통일 |
| review packet | review request | review request | 이미 표준에 가까움 |
| clerk | 노출하지 않음 | deterministic step/operator | role에서 제거 |

### 6.2 역할 모델 단순화

권고 role:

```text
coordinator  — scope, plan, acceptance, final decision
worker       — implement one bounded job
verifier     — independently test/check a job result
reviewer     — assess run-level architecture/domain quality
```

변경 이유:

- `main`은 실행 위치이지 책임 이름이 아니다.
- `orchestrator`는 LLM role보다 engine의 기능이어야 한다.
- `clerk`가 하는 반복 작업은 model role로 분리하기보다 deterministic step으로 내려야 한다.
- `implementer`는 업계에서도 통하지만 `worker`가 host/subagent/external을 포괄하기 쉽다.

고급 사용자를 위해 backend binding은 유지한다.

```yaml
profiles:
  default:
    worker: codex:gpt-...
    verifier: codex:gpt-...
    reviewer: claude:...
```

그러나 public setup에서 role × execution × effort의 모든 조합을 보여주지 않는다.

### 6.3 실행 모델 단순화

public abstraction:

```text
in-session
subagent
external
```

host-specific detail:

```text
Claude: clean subagent / forked subagent / workflow
Codex: subagent / plugin workflow / external exec
```

이 세부사항은 adapter capability로 내려간다. profile은 “책임에 어떤 backend를 쓸지”를 말하고, engine은 host capability와 policy에 따라 transport를 선택한다. 사용자가 fork/clean/workflow의 차이를 알고 싶을 때만 expert config를 연다.

---

## 7. 공개 UX 재설계

### 7.1 기본 surface

#### 필수

```text
/waystone:init
/waystone:run [task|goal|milestone]
```

#### 보조

```text
/waystone:status
/waystone:review
```

#### advanced

```text
/waystone:ideate
/waystone:improve
/waystone:doctor
/waystone:inspect
```

`delegate`는 기능을 삭제하지 않되 다음 중 하나로 이동한다.

- `/waystone:run <one-task>`의 내부 primitive
- expert alias `/waystone:delegate`
- 진단·수동 제어용 CLI

사용자가 일반적으로 `verify → verdict → apply` subcommand를 알 필요는 없다. `inspect`에서 evidence를 볼 수 있으면 된다.

### 7.2 init을 한 번의 setup으로 줄인다

현재 init은 Git, SSOT, policy level, delegation, review mode, host instructions, host memory, project registration, agent/hooks/statusline 설치를 한 번에 다룬다. 각 결정은 합리적이지만 onboarding에는 과하다.

권고 default flow:

```text
1. repo 구조 자동 탐지
2. 사용할 project brief 후보가 하나면 제안; 없으면 optional로 진행
3. 생성·수정할 파일과 자동 실행 권한을 한 화면에 preview
4. 사용자 한 번 승인
5. minimal config + task store + local runtime 초기화
6. 완료 요약
```

Advanced choice는 필요할 때 lazy-configure한다.

- first external run 때 backend/profile 설정
- first external review 때 review mode 설정
- first policy proposal 때 observation consent
- statusline은 별도 install command

이는 consent를 줄이는 것이 아니라 **consent를 실제 capability 사용 시점으로 옮기는 것**이다.

### 7.3 config를 preset 중심으로 만든다

예시:

```yaml
version: 2
project: waystone
brief: SSOT.md          # optional
mode: balanced          # conservative | balanced | autonomous
review: external        # off | external | pr
```

고급 설정은 `.waystone/profile.yml` 또는 `waystone config --expert`에서 관리한다.

Preset 의미:

| preset | 자동 실행 | 독립 검증 | 병렬성 | 사용자 질문 |
|---|---:|---:|---:|---|
| conservative | 제한적 | 항상 | 1 | 주요 단계 |
| balanced | bounded run | risk 기반/기본 on | 안전한 wave | ambiguity/blocker만 |
| autonomous | bounded closure 전체 | 항상 | configured max | hard escalation만 |

### 7.4 SessionStart context를 re-entry capsule로 축소한다

Default target: **약 500–1,200자**, hard cap 1,500자.

```text
[waystone] project=waystone branch=dev dirty=0
active run: run-2026... executing (3/5 jobs complete)
waiting: none
next: resume run; task fix/foo is verifying
handoff: .waystone/handoff.md
```

필요 시 task 1–3개와 blocker만 추가한다.

제거할 전역 주입:

- 8개 routing question
- 모든 role 설명
- overlay ID 목록
- evidence lake 요약
- 상세 operating contract
- 긴 project digest

대신 skill이 실행될 때 다음처럼 목적별 context를 요청한다.

```text
waystone context --for run
waystone context --for review
waystone context --for improve
```

이렇게 하면 모델은 일반 작업에서는 고유한 문제 해결 방식을 유지하고, workflow operation에서만 필요한 경계를 받는다.

### 7.5 hook 축소

권고 기본 hook:

1. **SessionStart:** read-only re-entry capsule
2. **PreCompact/SessionEnd:** optional UI handoff refresh. Run state는 이미 durable하므로 correctness 의존 금지

Optional, explicit consent:

3. **Stop boundary:** non-blocking policy warning

제거 또는 비활성 기본값:

- 모든 Read 앞 task nudge
- 모든 Write/Edit 뒤 광범위 guard

Waystone-owned file이 실제로 수정된 경우만 targeted validation을 실행하거나, CLI가 자신의 write 뒤 projection을 갱신하게 한다. 핵심 state가 사용자의 arbitrary editor action에 의존하지 않으면 PostToolUse hook의 역할이 크게 줄어든다.

---

## 8. 코드 구조 리팩터

### 8.1 제안 package layout

```text
waystone/
  cli/
    main.py
    commands/
  core/
    models.py
    enums.py
    errors.py
    events.py
    store.py
    migrations.py
  project/
    config.py
    tasks.py
    intent.py
    projections.py
    handoff.py
  runs/
    engine.py
    planner.py
    scheduler.py
    state_machine.py
    recovery.py
  jobs/
    executor.py
    verifier.py
    decision.py
    integration.py
    artifacts.py
  adapters/
    git.py
    claude_code.py
    codex.py
    external_codex.py
    external_claude.py
  features/
    external_review.py
    policy.py
    improve.py
    dashboard.py
```

현재 대형 module의 기능을 단순히 파일로 나누는 것이 목적은 아니다. 의존 방향을 다음처럼 고정한다.

```text
features → run API → core
adapters → core protocol
project → core
core → no feature imports
```

Session hook가 `delegate._private_function`을 직접 import하는 식의 결합을 없애고, 모든 consumer가 public read API만 사용하게 한다.

### 8.2 하나의 schema source

현재 JSON Schema, Python validator, skill text, README table이 같은 규칙을 반복한다. 제안:

- Python dataclass/enum 또는 하나의 declarative schema를 canonical source로 둔다.
- JSON Schema와 CLI help는 생성한다.
- transition table도 code에서 생성 가능한 문서로 만든다.
- README는 상세 field를 복사하지 않고 generated reference로 링크한다.

새 dependency를 줄이려면 stdlib `dataclasses`, `enum`, `argparse`, `sqlite3`를 우선한다. Pydantic/Typer 도입은 이득이 명확할 때만 한다.

### 8.3 CLI parser 통일

현재 unified front door 뒤에 new-style, legacy module, hand-rolled parser가 혼재한다. pre-1.0에서 다음을 완료한다.

- 하나의 `argparse` command tree
- 모든 command가 `main(argv) -> ExitCode`
- stdout은 human 또는 `--json`; stderr는 diagnostic
- exit code를 enum으로 중앙화
- private script direct invocation은 compatibility wrapper로만 유지
- migration은 명시적 transaction boundary에서 한 번만 실행

### 8.4 typed error와 recovery contract

예:

```text
UsageError             → 2
PolicyRefusal          → 3
RecoverableWait        → 4
EnvironmentFailure     → 5
ArtifactCorruption     → 6
InternalFailure        → 70
```

정확한 숫자는 후속 ADR에서 정하면 된다. 중요한 것은 free-text stderr를 skill이 분류하지 않는 것이다. JSON output에는 다음이 항상 있어야 한다.

```json
{
  "ok": false,
  "code": "integration-conflict",
  "recoverable": true,
  "run_id": "...",
  "next_actions": []
}
```

### 8.5 fault injection을 1급 테스트로 둔다

각 transition의 write 직전·직후 process kill을 주입한다.

검증할 property:

- resume 후 duplicate worker가 생기지 않는다.
- accepted patch가 두 번 적용되지 않는다.
- artifact는 partial file로 권위화되지 않는다.
- lease가 만료되면 안전하게 reclaim된다.
- DB commit과 content-addressed artifact 사이 orphan은 doctor가 정리한다.
- live tree는 final delivery 전까지 변하지 않는다.

---

## 9. 모델 사용 원칙 재설계

### 9.1 model이 해야 하는 일

- ambiguous goal을 bounded task set으로 분해
- owner material에서 acceptance를 정리
- trade-off가 있는 계획 선택
- verifier finding의 의미 판단
- conflict resolution patch 제안
- run-level architecture/domain review
- 사용자에게 결과를 설명

### 9.2 model이 하지 않아야 하는 일

- state transition 선택
- artifact sequence number 계산
- retry budget 기억
- SHA/digest 비교
- dependency ready 판정
- lock/lease 관리
- generated view sync
- policy stage transition
- release version propagation

### 9.3 worker prompt 최소화

Worker prompt는 다음만 포함한다.

```text
goal
acceptance criteria
allowed scope
base snapshot identity
constraints/security policy
required structured result schema
```

“먼저 A를 읽고 B를 실행하고 C를 보고한 다음…” 같은 절차는 가능한 한 제거한다. worker의 창의성은 해결 방법에 쓰고, harness는 결과와 경계만 검증한다.

### 9.4 routing question을 compiler input으로 이동

현재 8개 routing question은 좋은 설계 검토 항목이지만 매 session prompt에 들어갈 필요는 없다. 이를 deterministic/risk-based routing feature로 바꾼다.

입력:

- task scope known/unknown
- dependency count
- risk/severity
- independent verification requirement
- estimated tool intensity
- host capabilities
- budget preset

출력:

```json
{
  "role": "worker",
  "transport": "external",
  "backend": "codex:...",
  "verification": "required",
  "reason_codes": ["independent-context", "bounded-scope"]
}
```

모호할 때만 coordinator model이 한 번 판단한다. routing policy는 계속 audit 가능하지만 prompt boilerplate가 아니다.

---

## 10. 보조 기능의 위치

### 10.1 ideate

유지한다. 단, 제품 핵심이 아니라 **project brief bootstrapper**로 설명한다.

- Waystone 사용에 필수 아님
- 단일 SSOT가 없는 repo도 정상 지원
- 기존 design docs를 import/map할 수 있음
- 생성된 brief는 사용자가 검토해야 함

### 10.2 roadmap/status

유지한다. 둘 다 canonical task/run state의 projection이어야 한다.

- 수정 권위가 아님
- stale 여부를 hash/version으로 표시
- `status`는 하나의 현재 run과 blockers 중심
- cross-project dashboard는 advanced

### 10.3 external review

핵심 원칙은 유지한다.

- reviewer reply를 claim으로 취급
- exact preservation
- finding verification 후 task 등록
- PR mode SHA binding

다만 기본 run 완료와 외부 macro-review를 분리한다.

```text
job verification         # run completion의 기본 gate
external macro review    # project policy가 요구할 때 또는 사용자가 요청할 때
```

모든 작은 run이 반드시 외부 review packet을 만들 필요는 없다. review policy를 risk/size/preset으로 선택할 수 있게 한다.

### 10.4 improve와 adaptive policy

삭제하지 않는다. 다만 1.0 core의 critical path에서는 분리한다.

```text
Core run engine
    ↓ evidence export
Improve analyzer
    ↓ recommendation
Policy proposal
    ↓ observe → warn → enforce
```

용어를 단순화한다.

- overlay delta → policy proposal
- replay → evaluate on history
- promote → enable warning/enforcement
- materialize → publish to repository
- compose → resolve effective policy

현재 enforceable guard, waiver, 더 많은 lens를 추가하기 전에 run kernel 리팩터를 먼저 끝내는 것이 좋다. 그렇지 않으면 기존 여러 권위면 위에 또 하나의 blocking state machine이 얹힌다.

---

## 11. README 재설계

### 11.1 README의 역할

README는 전체 protocol reference가 아니라 다음 질문에 3분 안에 답해야 한다.

1. Waystone이 무슨 문제를 해결하는가?
2. 기존 coding agent 사용과 무엇이 다른가?
3. 설치 후 무엇을 입력하면 되는가?
4. 무엇을 자동으로 하고, 언제 나에게 묻는가?
5. 내 repo와 자격 증명에 무엇을 할 수 있는가?
6. 어느 프로젝트에 적합하지 않은가?

현재 상세 delegation lifecycle, command map, metrics lens, policy layer 설명은 가치가 있지만 README의 중심을 압도한다. 이 내용은 `docs/architecture/`, `docs/reference/`로 이동한다.

### 11.2 제안 목차

```text
1. Hero: 한 문장 + 3개 결과
2. 60-second demo
3. How it works: Brief → Run → Verify → Integrate → Handoff
4. Install
5. Quick start
6. What Waystone asks you / what it never does silently
7. When to use / when not to use
8. Optional capabilities: external review, improve, status
9. Configuration presets
10. Security and trust boundaries
11. Compatibility and releases
12. Contributing
13. Links to architecture/reference
```

### 11.3 제안 opening copy

```markdown
# Waystone

**Keep long-running agent development coherent across sessions and workers.**

Waystone turns a project goal into a resumable, bounded run. It dispatches work in
isolated environments, verifies results independently, integrates accepted changes in a
dedicated worktree, and hands back one auditable result.

- Resume after a new session without reconstructing the project.
- Run independent tasks in parallel without giving workers final authority.
- Accept changes from Git-derived evidence, not an agent's “done” message.
```

### 11.4 60-second quick start

```text
/plugin marketplace add ...
/plugin install waystone
/waystone:init
/waystone:run "implement the next milestone"
```

예상 output 예시를 보여준다.

```text
Run created: 2026-07-18-auth
Scope: 4 tasks, 2 parallel waves
Status: 3 completed, 1 verifying
User decisions needed: none
Result: ready for final review
```

### 11.5 release-ready 문서 세트

필수:

- `ARCHITECTURE.md` 또는 `docs/architecture.md`
- `SECURITY.md`
- `CONTRIBUTING.md`
- `CHANGELOG.md`
- `docs/concepts.md`
- `docs/recovery.md`
- `docs/configuration.md`
- `docs/compatibility.md`
- `docs/migrations.md`
- `docs/release-process.md`

권장:

- `CODE_OF_CONDUCT.md`
- issue/PR templates
- support policy
- deprecation policy
- architecture decision index

---

## 12. Release 및 CI/CD 제안

### 12.1 현재 방식에서 유지할 것

현재 release projection의 다음 성질은 좋다.

- runtime ship path를 positive manifest로 열거
- dev tree에서 exact commit을 test
- 임시 worktree에서 projected tree 생성
- projected runtime을 다시 smoke-test
- deploy tree가 dev-only file을 포함하지 않는지 검사
- Claude/Codex manifest version 정합성 검사
- marketplace install smoke

이것을 버리지 말고 **CI build artifact**로 승격한다.

### 12.2 현재 branch 의미를 명확히 바꾼다

권고 topology:

```text
main        = full source of truth (code + tests + docs)
dist        = CI-generated distributable tree; human edits 금지
vX.Y.Z tag  = source release identity
marketplace = exact dist SHA를 pin한 catalog
```

현재처럼 default `main`이 runtime projection이고 `dev`가 실제 source인 구조는 외부 contributor와 audit에 혼란을 준다. 테스트를 배포 tree에서 제외하는 것은 문제없지만, GitHub의 default branch에는 full source와 tests가 있어야 한다.

branch 이름을 즉시 바꾸기 어렵다면 최소한 다음을 보장한다.

- source default branch를 명확히 표시
- generated branch는 `dist` 또는 `release`로 명명
- generated branch README 상단에 source tag/SHA 링크
- branch protection상 release bot 외 write 금지

### 12.3 version의 단일 권위

Claude Code marketplace는 plugin source의 exact commit SHA를 pin할 수 있고, version 해석에서는 plugin 자체 `plugin.json`의 version이 marketplace entry보다 우선한다. 따라서:

- `plugin.json` version을 canonical release version으로 둔다.
- marketplace entry에는 중복 version을 두지 않는다.
- marketplace는 `repo + ref + exact sha`로 dist commit을 pin한다.
- Claude와 Codex manifest는 release command가 함께 생성/검증한다.
- version bump 없이 runtime commit만 바뀌는 stable release를 허용하지 않는다.

### 12.4 제안 release workflow

```text
Release PR
  ├─ version/changelog/migration metadata
  ├─ full source CI
  └─ compatibility review
        ↓ merge + signed tag
Build dist
  ├─ positive-manifest projection
  ├─ deterministic tree digest
  ├─ secret/license scan
  ├─ Claude validator + install smoke
  ├─ Codex validator + install smoke
  ├─ upgrade fixture tests
  └─ provenance manifest
        ↓
Push generated dist commit
        ↓
Create GitHub Release
  source tag/SHA ↔ dist SHA ↔ tree digest/checksums
        ↓
Open marketplace PRs
  exact dist SHA pin + generated metadata
        ↓
marketplace CI / install smoke / approval
        ↓
merge stable or edge channel
```

### 12.5 marketplace update 방식

현재 cross-repo deploy key로 marketplace default branch에 바로 push하는 대신 다음 중 하나를 권장한다.

1. marketplace repo가 `repository_dispatch`를 받고 자신의 `GITHUB_TOKEN`으로 PR 생성
2. fine-grained GitHub App이 양 repo에 설치되어 release PR 생성

중요한 것은 **직접 push가 아니라 reviewable PR + required checks**다.

### 12.6 release channel

```text
waystone-edge    → release candidate dist SHA
waystone         → stable dist SHA
```

Claude Code는 서로 다른 ref/SHA를 가리키는 stable/latest marketplace channel을 지원한다. pre-1.0에서 edge channel로 migration과 autonomous run을 dogfood하고 stable에는 검증된 version만 승격한다.

### 12.7 rollback

단순히 marketplace SHA를 이전 commit으로 되돌리는 것만을 정상 rollback 전략으로 삼지 않는다. plugin version cache semantics 때문에 다음을 기본으로 한다.

- 마지막 정상 dist tree를 복원한 **새 patch version**을 발행
- version은 단조 증가
- marketplace는 새 dist SHA로 이동
- emergency catalog revert는 별도 smoke-tested runbook으로 보유

### 12.8 CI matrix

#### Source tests

- Python 3.10–현재 지원 범위
- Ubuntu 필수, macOS 권장, Windows/WSL 지원 범위 명시
- unit + integration + fault injection
- concurrent run/lock tests
- migration fixtures: 0.8, 0.9, 0.10 → current
- security path/symlink/env tests

#### Distribution tests

- generated tree에 allowlist 밖 파일 없음
- plugin manifest/skills/hooks schema validation
- Claude Code minimum supported + latest
- Codex minimum supported + latest
- local marketplace add/install/update
- one real initialized fixture에서 `init → run dry-smoke → status`
- previous stable install에서 upgrade smoke

#### Performance budgets

- non-Waystone hook no-op p95
- SessionStart p95 latency
- injected context character/token count
- task registry 10/100/1,000 entries
- run recovery after crash

#### Release gates

- source SHA와 dist provenance 일치
- changelog/version/migration note 존재
- no direct push to stable marketplace
- artifact checksums/provenance attached
- branch protection required checks green

---

## 13. 보안·신뢰 경계

`SECURITY.md`와 별도 threat model에서 최소 다음을 다룬다.

### Repository trust

- project instructions는 data인가, executable policy인가
- setup command와 lockfile detection이 어떤 code를 실행할 수 있는가
- package manager lifecycle script 처리
- malicious Git hooks, submodules, LFS, build backend

### Filesystem

- symlink/path traversal
- worktree 밖 read/write
- cache ownership과 shared checkout
- temp file permissions
- binary patch와 file mode

### Credentials and network

- worker env allowlist
- secrets redaction
- network default policy
- host credential inheritance
- verifier가 credential을 필요로 하는 경우

### Sandbox

- Codex workspace-write/read-only의 실제 보장
- Claude external runner의 non-confinement와 explicit override
- host-native subagent는 OS sandbox가 아닐 수 있음을 명시
- “tool deny”와 “process confinement”를 구분

### Independence

- worker와 verifier가 같은 provider/model일 때 correlated failure
- 동일 context inheritance의 영향
- run-level reviewer의 책임

### Logs and retention

- session log 분석 범위
- raw prompt 저장 여부
- local retention/cleanup
- export에 민감정보가 들어가지 않는지

현재의 fail-closed sandbox probe와 unsandboxed Claude refusal는 보존해야 한다. 다만 보안 강화가 계속 `delegate.py` 내부 분기 증가로만 나타나지 않도록 execution adapter별 capability와 policy로 분리한다.

---

## 14. 단계별 전환 계획

### Phase 0 — Refactor charter와 characterization

목표: 구현을 바꾸기 전에 현재 핵심 계약을 고정한다.

Deliverables:

- 이 문서에서 I-01~I-12를 `docs/invariants.md`로 확정
- current delegate/round/review flow의 black-box characterization tests
- source→dist provenance 정의
- terminology ADR
- feature freeze: 새로운 enforcement/lens/backend 확장 제한

Exit gate:

- 핵심 workflow를 “어떤 파일을 쓴다”가 아니라 observable contract로 설명 가능
- migration fixture와 golden artifacts 확보

### Phase 1 — Core state와 module boundaries

목표: 기존 UX를 유지하면서 내부 kernel을 추출한다.

Deliverables:

- `core` models/errors/store/events
- canonical run/job enums
- unified CLI parser
- public read/write service API
- legacy command adapter
- SQLite/event journal prototype
- artifact content-addressing

Exit gate:

- 기존 `/delegate` 동작이 새 job engine을 사용
- skill text에 상태 전이 규칙 중복 없음
- kill/restart recovery tests green

### Phase 2 — Run integration engine

목표: 한 task가 아니라 bounded task closure를 실행한다.

Deliverables:

- run create/plan/continue/status
- dedicated integration worktree
- DAG/wave scheduler
- dynamic unblocking within frozen closure
- task-specific + run-level verification
- conflict reconciliation action
- final single delivery artifact

Exit gate:

- 3개 independent task 병렬 실행, 2단계 dependency chain 자동 진행
- process kill 후 resume
- live user tree는 final delivery 전까지 unchanged
- accepted job가 중복 적용되지 않음

### Phase 3 — Thin skill와 simple UX

목표: 사용자가 내부 protocol을 모르게 한다.

Deliverables:

- `/run` skill
- `/round` compatibility alias
- default presets
- lazy capability setup
- re-entry capsule
- hook 축소
- advanced inspect/doctor

Exit gate:

- fresh user flow가 install → init → run으로 끝남
- default SessionStart context ≤ 1,500자
- routine run에서 사용자 질문 0회 또는 hard escalation만
- public README에 internal artifact 이름이 필수 지식으로 등장하지 않음

### Phase 4 — Optional feature isolation

목표: review/improve/policy/status가 core engine을 오염시키지 않는다.

Deliverables:

- external review adapter
- improve evidence export interface
- policy proposal terminology/API
- generated projection boundary
- cross-project dashboard를 optional feature로 격리

Exit gate:

- core run package가 improve/overlay/dashboard를 import하지 않음
- optional feature failure가 run completion artifact를 손상시키지 않음

### Phase 5 — 1.0 release hardening

Deliverables:

- full README rewrite
- SECURITY/ARCHITECTURE/CONTRIBUTING/CHANGELOG/migrations
- source-main + generated-dist topology
- edge/stable channels
- exact SHA marketplace PR flow
- compatibility matrix
- upgrade/rollback runbook
- signed release provenance/checksums

Exit gate:

- latest two pre-1.0 releases에서 upgrade smoke
- fresh install과 marketplace update smoke
- documented recovery for interrupted run/release
- no known destructive migration path

### 제안 version mapping

| Version | 목표 |
|---|---|
| 0.11 | Kernel extraction — 기존 UX, 새 state/job engine |
| 0.12 | Autonomous run opt-in — integration worktree + DAG waves |
| 0.13 | Simple surface default — `/run`, presets, minimal context |
| 0.14 / 1.0-rc | migrations, docs, security, release channels |
| 1.0 | stable run engine + documented compatibility contract |

Version 번호는 예시다. 중요한 것은 기능 묶음과 exit gate다.

---

## 15. 1.0 acceptance criteria

### Product

- Waystone의 한 문장 설명이 task manager, policy engine, log analyzer를 나열하지 않는다.
- 사용자는 기본 workflow에서 `init`과 `run`만 알면 된다.
- SSOT/project brief가 없는 repo도 first-class로 동작한다.
- external review와 improve는 명시적 optional capability다.

### Autonomy

- run scope가 시작 시 명시적으로 고정된다.
- ready wave를 자동 dispatch한다.
- same-run dependency 완료 후 downstream task가 자동 unblock된다.
- closure 밖 task는 자동 실행하지 않는다.
- routine retry와 verifier pass/fail을 사용자 개입 없이 처리한다.
- hard escalation은 typed reason으로 durable하게 대기한다.

### Reliability

- every state transition is transactional/idempotent.
- crash injection 후 resume이 duplicate execution/apply를 만들지 않는다.
- worker claim과 harness evidence가 schema상 섞일 수 없다.
- final integration은 exact accepted artifact digest에 묶인다.
- live tree drift가 silent merge로 처리되지 않는다.
- state corruption은 explicit diagnostic과 recovery path를 제공한다.

### UX/prompt

- SessionStart default injection ≤ 1,500 chars.
- routing checklist를 매 session에 주입하지 않는다.
- worker prompt에는 goal, acceptance, scope, constraints, output schema만 들어간다.
- routine completion report는 outcome, checks, blockers, next action만 보여준다.
- 내부 packet/exposure/verdict 용어는 expert inspect 외에는 노출하지 않는다.

### Architecture

- one canonical run/job state machine.
- skill이 retry/state/artifact protocol을 정의하지 않는다.
- core package가 optional features를 import하지 않는다.
- host adapter가 capability를 선언하고 silent fallback하지 않는다.
- source schema에서 JSON schema/help/reference가 생성되거나 자동 정합성 검사를 통과한다.

### Release

- default source branch에 tests와 docs가 있다.
- generated dist는 source tag/SHA와 cryptographically mapped된다.
- marketplace entry가 exact dist SHA를 pin한다.
- source CI, dist CI, install/update smoke가 모두 release gate다.
- migration과 rollback-as-new-version이 문서화되어 있다.

---

## 16. 하지 말아야 할 리팩터

1. **모든 artifact를 제거하고 model memory에 의존하지 않는다.** 복잡성 축소는 provenance 삭제가 아니다.
2. **worker와 verifier를 합치지 않는다.** 이것은 핵심 차별점이다.
3. **편의를 위해 silent 3-way apply/stash를 도입하지 않는다.** integration worktree로 문제 위치를 옮긴다.
4. **자율성을 global backlog 소비로 정의하지 않는다.** bounded closure가 필요하다.
5. **모든 host를 동일한 capability가 있는 것처럼 추상화하지 않는다.** capability negotiation을 둔다.
6. **새 용어를 더 만들지 않는다.** run, job, task, check, review, decision, artifact로 제한한다.
7. **README에 내부 schema 전체를 복사하지 않는다.** reference 문서를 생성한다.
8. **1.0 전에 enforcement 기능을 계속 확장하지 않는다.** 먼저 kernel을 수렴한다.
9. **generated dist branch를 source of truth로 두지 않는다.** source와 mapping을 명확히 한다.
10. **SQLite 도입을 이유로 Git-tracked intent를 숨기지 않는다.** 사람의 방향·task·decision은 계속 diff 가능해야 한다.

---

## 17. 최종 우선순위

### P0 — 지금 시작할 것

1. core invariant/terminology ADR
2. run/job state model
3. `delegate` lifecycle의 engine 추출
4. source/default branch와 dist branch release 설계 확정
5. SessionStart injection 축소 실험

### P1 — kernel 이후

1. integration worktree
2. bounded DAG scheduler와 dynamic unblock
3. `/run` thin skill
4. config preset/lazy setup
5. README와 security docs

### P2 — 1.0 직전 또는 이후

1. enforceable policy/waiver 확대
2. 더 많은 improve lens
3. task source adapter 확장
4. 조직 단위 governance
5. 더 큰 fan-out 최적화

---

## 18. 최종 판단

Waystone은 핵심을 버리고 단순한 plugin으로 축소할 필요가 없다. 오히려 지금까지 만든 강한 evidence model을 **더 작은 kernel에 압축**해야 한다.

현재의 가장 좋은 부분은 다음이다.

```text
intent survives
worker does not self-approve
facts are Git-derived
verification is independent
acceptance is explicit
history is not silently rewritten
```

현재의 가장 위험한 부분은 다음이다.

```text
workflow correctness가 여러 skill turn과 artifact 관계에 분산됨
role/execution/policy abstraction이 실제 public need보다 큼
global prompt가 harness 내부 개념을 너무 많이 운반함
parallel dispatch와 end-to-end autonomous run 사이에 durable scheduler가 없음
source/release branch 의미가 외부 contributor에게 직관적이지 않음
```

따라서 pre-1.0의 핵심 전략은 다음 한 문장으로 정리된다.

> **기능을 더하는 대신, 한 번의 bounded run이 계획·병렬 실행·독립 검증·통합·handoff를 끝까지 소유하도록 만들고, 사용자와 모델에는 그 run의 목적과 현재 필요한 결정만 보여준다.**

이 구조가 완성되면 `ideate`, `roadmap`, `status`, `review`, `improve`, policy adaptation은 삭제하지 않고도 훨씬 명확한 위치를 갖는다. 모두 같은 durable run/evidence kernel의 입력, projection, 또는 optional consumer가 되기 때문이다.

---

## Appendix A — 제안 public flow

```text
User
  └─ /waystone:init
       └─ detect project, preview setup, initialize minimal state

User
  └─ /waystone:run "finish milestone M2"
       ├─ freeze bounded task closure
       ├─ snapshot project into integration worktree
       ├─ dispatch ready jobs in safe waves
       ├─ verify and decide each result
       ├─ integrate accepted patches
       ├─ dispatch newly unblocked jobs in the same closure
       ├─ run final checks/review policy
       └─ deliver one result + handoff

New session
  └─ small SessionStart capsule
       └─ /waystone:run
            └─ resume exact durable state
```

## Appendix B — 제안 routine completion report

```text
Run completed: auth-hardening

Result
- 4 tasks completed in 2 waves
- 3 worker patches accepted; 1 attempt discarded and retried
- final integration: 8 files changed

Verification
- unit tests: passed
- integration tests: passed
- independent verifier: no unresolved blockers
- final tree: <sha>

Handoff
- changes are ready as one commit/patch
- 1 follow-up task was recorded outside this run scope
```

## Appendix C — 분석한 repository surfaces

- `README.md`
- `SSOT.md`
- `.waystone.yml`
- `tasks.yaml`, `ROADMAP.md`
- `skills/init/SKILL.md`
- `skills/round/SKILL.md`
- `skills/delegate/SKILL.md`
- `skills/review/SKILL.md`
- `skills/improve/SKILL.md`
- `scripts/waystone.py`
- `scripts/round.py`
- `scripts/delegate.py`
- `scripts/review.py`
- `scripts/improve.py`
- `scripts/overlay.py`
- `scripts/common.py`
- `templates/profile-schema.json`
- `templates/routing-policy.yaml`
- `templates/hosts/claude-code/delegate-fanout.workflow.js`
- `hooks/hooks.json`
- `hooks/scripts/session_context.py`
- `.github/workflows/ci.yml`
- `.github/workflows/sync-marketplace.yml`
- `.github/workflows/sync-codex-marketplace.yml`
- `release-to-main.sh`

## Appendix D — 외부 배포 제약에 사용한 기준

Claude Code 공식 marketplace 문서에서 확인한 사항:

- plugin source는 GitHub repository의 branch/tag 또는 exact commit SHA로 pin할 수 있다.
- plugin version은 `plugin.json` → marketplace entry → git commit SHA 순으로 해석된다.
- `plugin.json`의 stale version은 marketplace entry version을 조용히 가릴 수 있다.
- stable/latest처럼 서로 다른 ref/SHA를 가리키는 release channel을 구성할 수 있다.
- `claude plugin validate`, local marketplace add, install smoke를 CI에서 사용할 수 있다.

이 제약 때문에 본 제안은 package registry 배포를 전제로 하지 않고, **source tag → generated dist commit → exact marketplace SHA pin** 흐름을 권고한다.
