# ADR-0010: run spec은 readiness·유한 retry·risk-gated review를 확정한 뒤 dispatch한다

- Status: accepted
- Date: 2026-07-19
- Round: —
- SSOT sections affected: 없음 — coordinator control plane의 dispatch·completion 계약을 확정
- Tasks: feat/run-spec-readiness-contract
- Authority: `docs/reviews/2026-07-19-m0-contracts-feedback.md`의 JW-GPT-016
  `실패 메커니즘`·`필수 수정`, `docs/adr/ADR-0002`~`ADR-0008`, `docs/invariants.md`

## Context

ADR-0008은 `coordinator`가 scope, plan, acceptance를 고정한다고 정의하지만, 그 산출물이 달성
가능한지, 범위가 닫혔는지, 검증되지 않은 전제를 참조하는지는 정하지 않는다. I-01도
owner-authored intent와 acceptance의 우선순위를 보존할 뿐 criterion 자체의 품질 조건은 주지
않는다. 따라서 구조가 올바른 job이라도 관측 불가능한 결과, 검증되지 않은 subsystem 전제,
적용 범위가 없는 요구, 결과 성질로 위장한 구현법을 포함한 채 dispatch될 수 있다.

2026-07-19 round-6의 실측은 이 누락이 이론적 위험이 아님을 보였다. 기각 12건 중 3건의 근본
원인은 coordinator가 작성한 조항이었다. 각각 미검증 기준 채택, 달성 불가능한 요구, 범위 미기재로
인한 과잉 demotion이었다. 두 lane은 각각 네 번의 attempt 동안 finding 수와 patch 크기가
증가하며 발산했다. 기각된 attempt는 모두 기계적 gate가 green이었고, 이 결함은 적대 리뷰만
찾았다.

ADR-0002는 재시도를 새 attempt와 새 `action_id`로 안전하게 기록하는 방법을 정하지만 재시도를
언제 멈출지는 정하지 않는다. ADR-0008의 `reviewer`도 run 수준 architecture·domain quality를
평가하지만 어떤 변경이 reviewer evidence를 요구하는지 정해져 있지 않다. 이 세 빈칸은 서로
독립된 기능이 아니라 coordinator가 제출한 control-plane proposal을 dispatch와 completion의
권위로 승격하기 전 검증하는 하나의 run-spec readiness contract다.

## Decision

### Frozen run spec과 readiness 경계

`coordinator`가 만든 run spec은 제안이며 engine fact가 아니다. engine은 criterion, retry ceiling,
review requirement와 그 판정 근거를 하나의 canonical bytes로 직렬화하고 `run_spec_digest`에
결속한다. criterion의 `source_pointer`가 가리키는 owner-authored 입력과 project policy의 authority는
ADR-0005에 따라 frozen Git tree bytes에 있고, run definition·readiness transition의 authority는
active project의 SQLite에 있다. critic과 reviewer 원문은 digest로 식별되는 artifact bytes이며 DB
row나 free-text 보고가 그 내용을 대신하지 않는다.

dispatch 가능한 spec은 다음 순서를 모두 만족한 `frozen-ready` revision뿐이다.

```text
candidate
  -> deterministic-valid
  -> critic-clean | critic-not-required
  -> frozen-ready(run_spec_digest)
  -> dispatch
```

검사 실패나 critic concern이 있는 revision은 dispatch하지 않는다. `coordinator`는 기존 revision을
재작성하지 않고 concern을 해소한 새 revision을 제출한다. 새 revision이 다시 모든 gate를 통과한
뒤에만 새 frozen run spec으로 발행한다. 거부된 revision, 검사 결과, critic artifact는 audit에서
보존한다. worker prompt나 구현 사고 과정을 규율하는 것이 아니라 시스템 권위에 제출되는 입력만
검사한다.

### Acceptance contract readiness

자유 문장 자체는 허용한다. `claim`, `negative_case`, 정당한 `method_constraint`의 본문은 자연어일
수 있다. 다만 구조 없는 단일 문자열은 dispatch-ready criterion이 아니다. owner가 직접 작성한
문장은 byte-exact로 보존한 채 envelope만 붙이고, `coordinator`가 합성한 criterion은 최소한 다음
필드를 직접 채운다.

| Field | 계약 |
|---|---|
| `origin` | `owner-authored` 또는 `coordinator-synthesized`. critic 의무를 결정하는 provenance이며 추론으로 바꾸지 않는다. |
| `claim` | 결과에서 참이어야 할 비어 있지 않은 property. 구현 활동이나 희망을 완료 성질로 쓰지 않는다. |
| `source_pointer` | frozen Git tree의 owner-authored source를 가리키는 immutable `commit + path + anchor` 포인터. |
| `subject_scope` | 적용 대상 task/job, path/resource, lifecycle phase·cycle 중 criterion의 진릿값을 바꿀 수 있는 축을 닫힌 identifier로 지정한다. |
| `observable_evidence` | `kind`와 등록된 `adapter`를 함께 지정한다. evidence를 실제로 관측·판정할 계약이 없는 kind는 허용하지 않는다. |
| `negative_case` | claim이 충족되지 않았다고 판정할 관측 가능한 failure 또는 counterexample을 비어 있지 않게 적는다. |
| `method_constraint` | `{present: false}` 또는 `{present: true, constraint, source_pointer}`다. `true`는 owner source가 특정 방법을 요구할 때만 허용한다. |

예를 들어 구조의 shape은 다음과 같다. 이는 criterion 내용이나 adapter vocabulary를 고정하는
구현 schema가 아니라 필수 의미 필드의 예시다.

```yaml
origin: coordinator-synthesized
claim: "integration decision은 worker가 아닌 별도 actor가 기록한다"
source_pointer:
  commit: "<frozen-input-sha>"
  path: docs/invariants.md
  anchor: I-02
subject_scope:
  task_ids: ["<task-id>"]
  lifecycle: integration-decision
observable_evidence:
  kind: artifact-observation
  adapter: decision-artifact
negative_case: "worker identity가 자신의 result에 대한 최종 decision actor로 기록된다"
method_constraint:
  present: false
```

모든 criterion은 origin과 무관하게 dispatch 전에 같은 deterministic validator를 통과한다. 같은
frozen input과 adapter registry snapshot에는 같은 결과가 나와야 하며, validator는 최소한 다음
typed error를 반환한다.

| Error | 조건 |
|---|---|
| `criterion-empty` | criterion 또는 필수 의미 field가 비었다. |
| `criterion-duplicate` | canonicalized 의미 필드가 같은 criterion이 둘 이상이다. |
| `source-not-found` | `source_pointer`의 commit, path 또는 anchor가 frozen input에서 존재하지 않는다. |
| `scope-unspecified` | `subject_scope`가 없거나 진릿값을 제한하는 identifier를 하나도 지정하지 않는다. |
| `evidence-adapter-not-found` | 선언한 evidence kind를 판정할 adapter가 frozen registry에 없다. |
| `reference-target-invalid` | task/job/path/resource/phase 등 pointer가 가리키는 대상이 없거나 해당 revision에 유효하지 않다. |

형식 검사 뒤 독립 `contract critic`은 다음 닫힌 concern type만 반환한다.

| Concern | 의미 |
|---|---|
| `unachievable` | 선언된 scope와 관측 계약 안에서 claim을 만족했다고 증명할 가능한 상태가 없다. |
| `unbounded` | 대상·수량·기간·종료 조건 중 필요한 경계가 없어 유한한 완료 판정이 불가능하다. |
| `unverified-reference` | claim이 지정 evidence로 확인하지 않는 외부 성질이나 subsystem 안전성을 참이라고 전제한다. |
| `scope-ambiguous` | 구조상 scope가 채워졌어도 둘 이상의 합리적인 적용 대상이나 시점으로 해석된다. |
| `implementation-prescriptive` | owner가 명시한 method constraint가 아닌 구현 선택을 결과 property처럼 요구한다. |

critic artifact는 `run_spec_digest`, concern type, 대상 criterion digest, 근거를 담는다. critic은
`coordinator`와 분리된 actor/context에서 수행하며, criterion replacement, 자동 rewrite,
`suggested_method`를 만들거나 worker에게 구현법을 지시하지 않는다. concern을 해소할 책임은
`coordinator`에게 있다. contract critic evidence는 dispatch readiness 증거일 뿐 verifier evidence나
완료 단계의 adversarial reviewer evidence를 대신하지 않는다.

`owner-authored` criterion의 critic은 선택이며 `critic-not-required`를 명시적으로 기록할 수 있다.
autonomous mode에서 하나라도 `coordinator-synthesized` criterion이 있으면 critic은 필수다. project
policy는 critic 의무를 강화할 수 있지만 이 필수 조건을 약화할 수 없다.

### Retry와 수렴의 hard ceiling

frozen run spec은 다음 값을 유한한 수와 닫힌 type으로 확정한다.

```yaml
retry:
  max_attempts_per_job: <positive-integer>
  max_total_attempts: <positive-integer>
  time_budget: {limit: <positive-number>, unit: <closed-unit>}
  cost_budget: {limit: <positive-number>, unit: <closed-unit>, meter: <registered-meter>}
  retryable_failure_classes: [<typed-failure-class>]
  budget_exhaustion_policy: stop
```

profile이나 project policy가 기본값을 제공할 수 있지만 freeze 전에 구체적인 값과 meter로 resolve해야
한다. `unlimited`, 생략, 자동 증가 값은 허용하지 않는다. attempt는 worker 실행을 다시 시작할 때
새로 세며 ADR-0002와 E-05에 따라 새 `action_id`와 append-only record를 갖는다. 동일 action의
재관측·reconciliation은 새 attempt가 아니다. `unknown-effect`를 retryable failure로 바꾸거나
ceiling을 피하기 위해 counter를 초기화해서는 안 된다.

engine은 새 attempt나 비용을 만드는 action을 authorize하기 전에 job별·run 전체 attempt와 남은
time/cost reservation을 원자적으로 대조한다. 남은 budget보다 큰 작업은 시작하지 않는다. 실행
중 ceiling에 도달하면 새 작업을 발행하지 않고 진행 중 effect는 ADR-0003의 cancel·quiescence
계약으로 정지시킨다. hard ceiling은 추가 실행 권한을 닫는 경계이며, 이미 in-flight인 effect가
없었다고 가장하거나 unsafe cleanup을 허가하지 않는다.

한계 도달은 attempt나 run의 일반 `failed`로 축약하지 않고 다음 typed stop으로 드러낸다.

```text
waiting_user(reason=lane-not-converging,
             ceiling=max_attempts_per_job, consumed=<n>, limit=<n>)
waiting_user(reason=retry-budget-exhausted,
             ceiling=max_total_attempts|time_budget|cost_budget,
             consumed=<value>, limit=<value>)
```

이 상태에서는 engine과 `coordinator`가 budget을 자동 연장하거나 다른 failure class로 바꾸어 retry할
수 없다. 새 owner decision이 새 frozen run spec과 새 digest로 명시적으로 ceiling을 바꾸기 전에는
추가 attempt가 불가능하다. 따라서 autonomous mode도 사람 개입 없이 무한 재시도할 수 없다.

finding 추세, patch growth, 같은 criterion의 반복 실패를 결합하는 trajectory scoring과 adaptive
convergence heuristic은 0.13+로 이월한다. 현재 계약은 위의 결정론적 counter와 budget만 correctness
gate로 사용하며, 모델이 “곧 수렴할 것”이라고 주장해 ceiling을 넘기지 못한다.

### Risk-gated adversarial reviewer requirement

모든 run에 adversarial review를 강제하지 않는다. frozen run spec은 다음 결정을 반드시 가진다.

```text
review_requirement: none | required
review_reason: <typed-code>
```

판정은 frozen project policy의 named rule, owner decision, 선언한 `subject_scope`에 결속한다. Waystone
자체에서는 다음 trust surface를 건드리는 scope가 최소 `required`다.

| `review_reason` | Review를 요구하는 변경 |
|---|---|
| `trust-surface-store` | run/action state, transaction, CAS, fence, artifact identity 또는 retention authority |
| `trust-surface-review-binding` | reviewer identity, digest binding, finding·approval provenance |
| `trust-surface-completion-gate` | integration, merge, closeout, completion 조건 또는 waiver |
| `trust-surface-migration` | persisted state나 user work를 변환·이동·삭제하는 migration |
| `trust-surface-sandbox` | executor boundary, sandbox, capability, 권한 강등·승격 |
| `trust-surface-evidence-authority` | fact authority, evidence source, 관측·재사용·귀속 규칙 |
| `owner-required` | owner가 특정 run에 review를 명시적으로 요구 |

project는 git-tracked policy에서 추가 path/rule과 typed code를 선언할 수 있다. 범용 trust-surface
자동 분류기는 이 계약의 일부가 아니다. pre-dispatch 판정은 선언 scope와 policy rule을
결정론적으로 대조하고 적용한 rule id와 policy digest를 함께 기록한다.

`review_requirement: none`은 (a) 어떤 review trigger도 선언 scope와 integrated result에 적용되지
않아 `review_reason: no-review-trigger`인 경우, 또는 (b) frozen policy나 owner-authored source가
특정 rule과 bounded scope를 명시적으로 면제해 `review_reason: explicit-review-exemption`인 경우에만
허용한다. 면제는 authorizing `source_pointer`, rule id, 대상 scope, rationale을 기록해야 한다.
`coordinator`가 “low risk”라고 자유 판단하거나 docs/test-only라는 이름만 붙여 면제를 만들 수 없고,
blanket exemption도 허용하지 않는다.

completion 직전 engine은 harness-computed changed files와 effect 종류를 같은 frozen policy에 다시
대조한다. integrated result가 새 review trigger를 건드리면 frozen 값이 `none`이었더라도 effective
requirement는 `required`로 강화하고 해당 typed reason을 기록한다. 결과 scope가 면제 범위를
벗어나면 면제는 무효다. requirement를 자동으로 `required`에서 `none`으로 약화하는 방향은 없다.

effective requirement가 `required`이면 exact `integrated_result_digest`와 `run_spec_digest`에 결속된
독립 `reviewer` artifact가 있고, 그 artifact가 completion을 허용하며, 같은 digest에 대한 미해소
blocking concern이 없어야 run을 `completed`로 전이할 수 있다. 결과 bytes가 바뀌면 review는 stale이며
새 digest를 다시 검토한다. green test, verifier evidence, contract critic 통과는 이 reviewer evidence를
대체하지 않는다.

### 기존 불변조건과의 결속

- I-01은 owner source를 `coordinator` 합성보다 우선시하고, I-02는 worker가 readiness나 completion을
  자기 승인하지 못하게 한다.
- ADR-0002의 effect reconciliation과 E-05의 append-only attempt history는 그대로 유지된다. 이 ADR은
  안전한 retry가 새로 계획될 수 있는 유한 범위만 추가한다.
- ADR-0003의 `waiting_user`와 cancel·quiescence 계약이 ceiling stop을 운반한다. status 조회나 health
  추정은 budget을 연장하거나 retry를 만들지 않는다.
- I-04와 E-07에 따라 verifier, contract critic, reviewer, integration decision은 서로 다른
  provenance와 대상 digest를 갖는다.
- I-09에 따라 readiness 근거나 required review가 없으면 success로 degrade하지 않는다. I-10에 따라
  이 검사는 control-plane artifact 경계에만 적용하고 worker의 domain reasoning을 prompt protocol로
  대체하지 않는다.
- ADR-0006의 closeout `run_spec_digest`는 이 ADR의 criterion·ceiling·review decision까지 결속한다.
  closeout manifest가 runtime evidence body의 두 번째 authority가 되지는 않는다.

## Consequences

- 형식상 실행 가능한 job과 의미상 검증 가능한 acceptance를 dispatch 전에 구분할 수 있다.
- autonomous run은 coordinator가 만든 잘못된 criterion이나 유한 budget 소진을 새 attempt로 덮지
  못하고 typed stop으로 드러낸다.
- trust surface 변경은 green verifier만으로 완료할 수 없으며 exact integrated result에 대한 독립
  architecture·domain review를 갖는다. 위험 trigger가 없는 run은 review 비용을 지불하지 않는다.
- engine에는 deterministic validator, critic artifact binding, budget reservation/counter, project
  review policy와 completion gate 구현이 필요하다. 이 구현은 M1-B·M2에서 계약별 kernel test와
  fault-injection으로 결속한다.

## Alternatives considered

- **criterion을 자유 문자열로만 유지** — source, scope, evidence, negative case를 결정론적으로 검사할
  수 없어 같은 실패를 dispatch 뒤에 발견하므로 기각.
- **deterministic schema 검사만 사용** — 구조가 채워져도 달성 불가능하거나 미검증 전제를 가진
  criterion을 찾지 못하므로 기각.
- **critic이 criterion을 자동 수정하거나 구현법을 제안** — owner intent와 coordinator 책임을
  critic이 대신하고 method prescription을 재생산하므로 기각.
- **retry를 coordinator 판단으로 계속 연장** — green gate를 반복하면서도 lane이 무한 발산할 수
  있어 기각.
- **모든 run에 adversarial review 강제** — trust surface를 건드리지 않는 bounded change에도 같은
  비용을 부과하므로 기각.
- **green test와 verifier evidence를 adversarial review로 간주** — round-6에서 모든 기계적 gate가
  green인 기각 attempt의 설계 결함을 잡지 못했으므로 기각.

## Amendment (2026-07-21) — M1-B v1 adapter ruling: legacy acceptance·review decision·retry 기본값

M1-B 구현(`feat/run-spec-planning`)이 확인한 3자 충돌을 고정한다: 본문의 구조화 acceptance
envelope(+deterministic validator·독립 critic)는 현행 task registry의 `accept: [string]`
및 계획 §2-6(tasks.yaml 형식 0.12 불변)과 동시에 문자 그대로 성립할 수 없다
(`decision/run-spec-v1-interpretation-batch`, m1b-spec 보고 ④1·②·③ — opus 독립 검증이
충돌 실재를 확인).

1. **v1 acceptance adapter (M1-B~envelope 이행 전까지의 계약 준수 형태).** planner는 legacy
   `accept` 문자열을 owner-authored criterion **원문**으로 동결하고 empty/blank/duplicate만
   deterministic 거부하며, readiness 판정을 `frozen-ready`·critic 처분을 `critic-not-required`로
   **명시 기록**한다. 이는 본문 envelope의 대체가 아니라 이행기 adapter다 — 전체 envelope·
   validator·critic 의무는 면제되지 않고 소유가 이월된다.
2. **envelope의 착지 경로.** 구조화 envelope는 tasks.yaml 형식을 바꾸지 않고 **dispatch-time
   acceptance contract 조립**(owner가 dispatch 시점에 envelope를 작성·동결, registry 문자열은
   그 원문 재료)으로 구현한다. 소유: M2의 goal freeze/acceptance 경계(계획 M2-5와 결속).
   그 시점까지 v1 adapter 출력의 `critic-not-required`는 "critic 미실행"의 정직한 기록이며
   준수 주장이 아니다.
3. **review decision 부재 = null 보존.** 정책 컴파일러가 없는 동안 planner는 decision을
   합성하지 않는다(silent 강등 회피 — 명시 입력만 동결, project-defined reason fail-closed).
   본문의 "모든 frozen spec은 none|required 결정을 가져야 한다"는 정책 컴파일러 도입 후의
   목표 상태로 한정하며, 컴파일러 도입 시 mandatory로 전환한다.
4. **retry 기본값.** 정책 원천 부재 시 v1 기본은 no-retry(`max_attempts_per_job=1`,
   `max_total_attempts=1`, retryable class 없음, stop)와 positive bounded meter(1 day,
   attempt-start)다. 이 수치는 본문이 아니라 이 Amendment가 소유하며, profile/project policy
   원천이 생기면 그쪽이 우선한다.

이 Amendment는 본문 계약의 목표 상태를 바꾸지 않는다 — 이행기 형태와 그 소유를 고정할 뿐이다.

## Amendment (2026-07-22, 0.13 C2)

Readiness is stage-scoped and compiled into the frozen `AssurancePlan`; there is no blanket
requirement that every run contain an independent check or review. WorkBrief semantic context and
its item-level provenance are part of the frozen input. Review claims remain separate from
validation/disposition, and only selected disposition may materialize work.
