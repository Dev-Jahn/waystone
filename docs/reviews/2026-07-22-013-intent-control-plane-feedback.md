<!-- waystone feedback: the body below is the reviewer reply VERBATIM (byte-exact copy via `waystone review ingest`) — do not edit it; a triage skeleton is appended beneath it. -->
round: 2026-07-22-013-intent-control-plane
reviewer: (unknown)
reviewer-effort: (unknown)
review-target: (unknown)
reply-metadata-json: {"metadata":{},"rendered_request_coverage_reason":"request-digest-missing","rendered_request_digest_matches":null}
ingested: 2026-07-23
source: /tmp/review.md
verbatim-bytes: 22550

---

# 검토 기준

현재 `dev`의 최신 상태는 `45b2fa991633ba03d207cd0b69aae69bca85aeda`입니다. M1-B trust kernel 위에 `PROJECT_BRIEF`, WorkBrief, `explore/evaluate/promote`, review disposition, OutcomeDelta, objective-first status를 올린 0.13 intent-control-plane 재설계까지 진행된 상태로 보았습니다.

검토는 GitHub의 현재 소스·테스트·gate evidence를 정적으로 대조한 것입니다. 제가 suite와 실제 backend를 별도로 재실행하지는 않았습니다. 저장소의 최종 gate 기록에는 real Codex backend가 worker completion → candidate publication → OutcomeDelta ledger까지 완주했고, 전체 234-test suite가 통과한 것으로 기록되어 있습니다.

# 종합 판정

> **전체 설계 방향은 승인할 만합니다.**
> 제안했던 방법론을 그대로 복제하지 않고, Waystone의 기존 trust kernel에 맞는 별도 control plane으로 재해석한 것이 오히려 적절합니다.

다만 현재 상태를 나누어 판정하면 다음과 같습니다.

| 영역                                               | 판정                                  |
| ------------------------------------------------ | ----------------------------------- |
| Project intent와 불확실성 모델                          | **승인**                              |
| Review finding의 진위·우선순위 분리                       | **승인**                              |
| Worker semantic context와 context request         | **승인, 실행 routing은 부분적**             |
| Explore/Evaluate lifecycle                       | **승인**                              |
| Promote의 lineage·CAS·지원 범위 gate                  | **대체로 승인**                          |
| Promote의 verifier/reviewer/decision actor 분리     | **변경 필요**                           |
| Review disposition의 objective/evidence authority | **변경 필요**                           |
| 테스트 축소와 진행률 모델                                   | **승인**                              |
| 사용자 surface                                      | **방향 승인, coordinator용 scaffold 필요** |
| Release projection                               | **미승인 — 현재 알려진 blocker**            |

가장 중요한 결론은 다음입니다.

> **원래 문제 세 가지를 개념적으로만 문서화한 것이 아니라, 실제 authority와 runtime transition으로 옮기는 데 성공했습니다. 그러나 가장 비싼 `promote` 단계에서는 아직 “action 이름이 분리되어 있다”는 것과 “actor와 evidence가 실제로 분리되어 있다”는 것이 혼동되어 있습니다.**

---

# 원래 세 문제를 얼마나 해결했는가

## 1. 불확실한 사용자 의도를 잘못된 명세로 고정하는 문제

이 부분은 가장 잘 해결됐습니다.

현재 `PROJECT_BRIEF.md`는 commitment, prototype boundary, non-goal, hypothesis, open question, revision trigger를 구분합니다. 프로젝트가 `committed` 상태여도 commitment·prototype·non-goal만 binding이고, hypothesis와 question은 계속 nonbinding입니다.

각 fact는 단순 heading 문자열이 아니라 다음에 결속됩니다.

```text
commit
path
fact_id
fact_digest
binding
```

따라서 “대화에서 그렇게 말한 것 같다”거나 coordinator가 요약했다는 이유만으로 hypothesis가 requirement가 되지 않습니다.

`brief adopt`도 단순히 front matter를 바꾸는 것이 아니라 owner evidence를 CAS에 보존한 뒤 adoption record를 남기는 typed gate로 구현되어 있습니다.

이는 첨부한 Omniphysics realignment 대화에서 드러난 핵심 실패, 즉 **불확실한 그림을 너무 일찍 SSOT로 고정하고 이후 모든 작업이 그 잘못된 방향을 정밀하게 실행한 문제**를 직접 겨냥합니다. 

### 판단

초기 제안보다 현재 구현이 더 낫습니다. `ProjectFrame` 같은 새 public jargon을 추가하지 않고, 표준적인 `PROJECT_BRIEF.md` 안에서 fact별 binding을 구현했기 때문입니다.

---

## 2. 강한 review가 모든 실제 결함을 현재 개발 의무로 바꾸는 문제

이 부분도 핵심 구조는 제대로 바뀌었습니다.

현재 review는 다음 세 artifact를 분리합니다.

```text
Claim
→ Validation
→ Disposition
```

Claim은 reviewer의 주장, validation은 실제 failure mechanism의 확인, disposition은 current objective에 비추어 어떤 처분을 할지의 결정입니다. 각 revision은 append-only chain으로 연결되고, 최신 validation이 바뀌면 기존 disposition은 stale이 됩니다.

`tasks.yaml`로 materialize되는 것은 명시적으로 선택된 다음 두 disposition뿐입니다.

```text
fix-now
fix-before-promotion
```

`backlog`, `accept-risk`, `no-action`은 실제 결함이더라도 task가 되지 않습니다.

Skill 수준에서도 reviewer를 “evidence sensor”로 제한하고, REAL finding마다 ADR·permanent test·새 review cycle·remediation을 자동 요구하지 않도록 명시했습니다.

또한 M1-B의 1,099개 suite에서 현재 234개까지 줄였지만, store·effect reconciliation·verification 같은 trust kernel fault fixture는 별도로 유지하고 gate에서 확인했습니다. 따라서 단순한 coverage 포기가 아니라 **폐기된 workflow contract의 test를 함께 제거한 것**으로 판단됩니다.

### 판단

`confirmed major ≠ fix-now`가 실제 데이터 모델과 CLI로 성립합니다. 이 부분은 명확한 성공입니다.

---

## 3. Orchestrator–worker 사이의 context 병목

의미 전달은 크게 개선됐습니다.

WorkBrief에는 다음이 포함됩니다.

* objective와 desired delta
* why now
* current state와 known failures
* fixed decisions
* worker가 선택 가능한 것
* escalation boundary
* constraints와 non-goals
* open questions
* expected evidence
* 관련 source와 provenance

Provenance도 `owner-source`, `harness-observation`, `coordinator-summary`로 나뉩니다.

실제 prompt는 coordinator summary를 다음처럼 명시적으로 낮은 권위로 표시합니다.

> `Coordinator context — interpretation, not owner authority`

그리고 protocol·lease·artifact naming 같은 bookkeeping은 전달하지 않으면서, 목적·현재 상태·결정 공간·source는 전달합니다.

Worker가 context 부족을 감지하면 code change 없이 `context-requested`를 반환하고, coordinator response는 새 WorkBrief·RunSpec revision·attempt를 만듭니다. 기존 attempt나 budget을 덮어쓰지 않는 구조도 적절합니다.

### 남은 한계

Profile은 다음 세 execution category를 선언합니다.

```text
in-session
subagent
external
```

하지만 현재 staged execution은 non-external을 거부하고, external 중에서도 직접 실행 가능한 Codex binding만 허용합니다.

따라서 다음 원칙은 아직 완성되지 않았습니다.

> Context transfer loss가 큰 작업은 위임하지 않고 main session이 직접 수행한다.

현재는 **위임됐을 때의 context 품질**은 좋아졌지만, **위임 여부 자체를 context transfer cost에 따라 결정하는 canonical routing**은 아직 없습니다.

이는 당장 blocker는 아닙니다. 다만 0.13이 모든 execution category를 지원한다고 주장해서는 안 됩니다. 현재 v1 지원 범위를 “external Codex worker/evaluator”로 좁혀 문서화하거나, 이후 실제 carrier/in-session path를 구현해야 합니다.

---

# 잘된 핵심 설계

## `Explore → Evaluate → Promote`가 단순 label이 아님

Stage별 assurance가 실제로 다릅니다.

```text
Explore:
  worker → result adapter → candidate publish → completion

Evaluate:
  candidate freeze → read-only evaluator → evaluation evidence → completion

Promote:
  evaluated candidate freeze → verify → optional review
  → integration decision → target apply → completion
```

Explore에서는 probe가 optional이고 regression contract·independent verification·review가 필요하지 않습니다. Evaluate부터 frozen evaluation spec과 read-only execution이 필요하고, Promote에서만 regression contract, supported scope, accepted risks, committed frame, integration apply가 요구됩니다.

이것은 기존의 “모든 작업에 promotion-grade assurance 적용” 문제를 구조적으로 해소합니다.

## Completion contract가 stage에 따라 authority를 제한함

Explore의 learning criterion은 nonbinding hypothesis/question에만 결속되고, evaluate criterion은 frozen evaluation spec, promotion criterion은 binding commitment/prototype/owner request 또는 accepted ADR·passed evaluation evidence에만 결속됩니다.

따라서 금지한 자동 승격:

```text
hypothesis → requirement
```

은 실제 compiler에서 차단됩니다.

## Progress가 task count에서 분리됨

완료된 run은 `OutcomeDelta`를 발행하며, status는 objective, stage, waiting context, last delta를 먼저 보여주고 task 수는 audit 정보로만 다룹니다. `no-objective-delta`도 숨기지 않지만 progress로 계산하지 않습니다.

이것은 프로젝트가 “fix 20개 처리했으니 진척됐다”고 착각하는 문제를 잘 막습니다.

## Gate 수행 방식이 비교적 정직함

0.13 gate는 real backend smoke가 schema 문제로 여러 번 실패했을 때 suite green을 성공 대리 지표로 쓰지 않았고, 각 실패를 별도 finding으로 남겼습니다. 최종적으로 실제 Codex worker completion, candidate ref, outcome ledger와 세 digest가 일치한 실행을 확보한 뒤 PASS를 선언했습니다.

이 과정은 새 방법론이 자기 자신에게도 적용됐다는 긍정적인 신호입니다.

---

# 필수 수정 1 — Promote의 actor/evidence 분리가 명목상으로만 존재함

현재 가장 중요한 문제입니다.

Promotion의 public stage handler는 다음처럼 구성됩니다.

```python
"independent-verify": lambda: evidence_digest,
"integration-decision": lambda: evidence_digest,
"adversarial-review": lambda: evidence_digest,
```

여기서 `evidence_digest`는 이전 evaluate 단계의 `EvaluationEvidence` digest입니다. 즉 같은 artifact가 다음을 모두 대행합니다.

* independent verification
* adversarial review
* coordinator integration decision

`execute_assurance_dag()`는 frozen action 이름이 모두 실행됐는지와 mutation boundary만 검사합니다. 반환값이 실제 verifier artifact인지, reviewer artifact인지, coordinator decision artifact인지 확인하지 않습니다.

이는 현재 보존 invariant와 충돌합니다.

> verifier evidence와 integration decision은 별도 artifact/actor로 남는다.

Evaluation evidence를 subsequent verification의 **입력**으로 사용하는 것은 타당합니다. 그러나 동일 digest를 verification, review, decision의 **결과 artifact**로 사용하는 것은 다른 문제입니다.

## Risk-gated review도 public path에서 사실상 비활성

`compile_assurance_plan()`은 `declared_risks`가 있을 때만 `adversarial-review` action을 넣습니다.

그런데 public `run start`의 assurance compilation은 `declared_risks`를 넘기지 않습니다. accepted-risks record를 promotion completion에 요구하기는 하지만, 그 내용이 review trigger로 컴파일되지는 않습니다.

또한 canonical `review` CLI에는 claim/validation/disposition/materialize만 있고, external reviewer artifact를 promotion lineage의 `ReviewCycle`로 append하는 public 경로는 없습니다.

## 권장 수정

새 subsystem을 만들 필요는 없습니다. 이미 존재하는 타입과 경계를 실제로 연결하면 됩니다.

1. `independent-verify`는 기존 `VerifierEvidence`를 실제 verifier binding으로 생성하거나 로드해야 합니다.
2. `integration-decision`은 별도 coordinator artifact로 생성하고 다음을 참조해야 합니다.

   * exact candidate
   * evaluation evidence
   * verifier evidence
   * 필요한 경우 reviewer evidence
3. `adversarial-review`는 exact promotion lineage와 target result에 결속된 `ReviewCycle` 또는 reviewer artifact를 소비해야 합니다.
4. accepted-risks/supported-scope record를 파싱해 `declared_risks`를 assurance compiler에 전달해야 합니다.
5. review surface에는 새 public 개념을 추가하지 말고, ingest된 reviewer result를 현재 promotion lineage에 attach하는 최소 연결만 둡니다.

## 최소 회귀 테스트

광범위한 matrix는 필요 없습니다.

* 동일 artifact 또는 동일 actor가 verifier·reviewer·decision 세 역할을 겸하면 거부
* declared trust risk가 있는데 reviewer artifact 없이 promotion하면 거부
* reviewer artifact가 다른 candidate/result digest를 가리키면 거부

이 세 개면 핵심 경계를 충분히 잡습니다.

---

# 필수 수정 2 — Review disposition의 objective/evidence가 shape만 검증됨

Review finding chain의 구조는 좋지만, semantic authority verification이 빠져 있습니다.

Validation의 `evidence_refs`는 현재 `{kind, digest}` 형태와 digest 문자열 형식만 검사합니다. 해당 digest의 bytes가 실제 CAS나 Git authority에 존재하는지는 확인하지 않습니다.

Disposition의 `objective_ref`도 `commit`, `path`, `fact_id`, `fact_digest`, `binding` 필드가 문자열·digest 형태인지만 검사합니다. 실제 `PROJECT_BRIEF.md`의 해당 commit/fact와 일치하는지 재도출하지 않습니다.

CLI는 이 payload를 parse한 뒤 곧바로 append합니다.

테스트 fixture에서도 실제로 존재하지 않는 `"a" * 40` commit과 합성 fact digest가 유효한 disposition으로 사용됩니다.

## 영향

현재 구조는 다음은 잘 막습니다.

```text
confirmed major
→ 자동 task
```

하지만 다음은 아직 가능합니다.

```text
confirmed finding
→ 존재하지 않거나 stale한 objective에 current-objective라고 귀속
→ fix-now disposition
→ task materialize
```

즉 **finding truth와 priority를 분리했지만, priority 판단이 실제 objective authority에 결속되지 않을 수 있습니다.**

## 권장 수정

별도 review 전용 resolver를 만들지 말고, completion contract가 이미 사용하는 authority machinery를 재사용하는 것이 맞습니다.

* disposition `objective_ref`를 `ProjectFactRef`/`AuthorityRef`로 parse
* exact commit의 `PROJECT_BRIEF.md`에서 fact digest와 binding을 재검증
* validation evidence는 최소한 CAS 존재를 확인
* Git evidence가 필요하면 기존 typed Git authority ref를 사용
* `decided_by.binding_digest`와 `reported_by.binding_digest`도 가능하면 target run의 frozen profile binding에 대조

이 수정은 schema를 복잡하게 늘리는 것이 아니라, **이미 정의된 authority type을 review에도 일관되게 적용하는 작업**입니다.

---

# 전략적으로 가장 주의할 점 — 새 lifecycle이 또 다른 관료제가 될 가능성

현재 설계는 연구·제품 개발에는 매우 잘 맞습니다. 하지만 평범한 bugfix나 maintenance에서도 다음 사슬을 강제할 가능성이 있습니다.

```text
project hypothesis/question
→ explore candidate
→ frozen evaluation spec
→ evaluate
→ regression/scope/risk records
→ promote
```

현재 explore learning criterion은 project-level hypothesis/question만 source로 허용합니다. owner의 단순 bugfix 요청을 nonbinding exploration authority로 바로 사용할 수 없습니다.

이는 아직 확정 defect라고 보지 않습니다. 실제 사용에서 다음 두 종류의 작업을 한 번씩 dogfood해야 합니다.

1. 연구 성격의 불확실한 architecture task
2. 매우 평범한 국소 bugfix 또는 문서·config 정정

두 번째 작업에서 다음 현상이 나타나면 과도한 것입니다.

* 가짜 hypothesis를 `PROJECT_BRIEF.md`에 추가해야 함
* 단순 회귀 확인을 위해 별도 evaluation spec 문서를 만들어야 함
* product direction과 무관한 변경도 full promotion ceremony를 거쳐야 함

그 경우 곧바로 네 번째 lifecycle stage를 추가해서는 안 됩니다. 더 작은 수정이 적합합니다.

* owner request를 **nonbinding explore objective**로 허용
* 기존 `explore/evaluate/promote` 안에 low-risk maintenance assurance profile을 둠
* deterministic focused check를 최소 evaluation evidence로 사용할 수 있게 함

실제 불편이 반복되기 전에는 구현하지 않는 편이 맞습니다.

---

# UX와 coordinator burden

사용자 surface는 상당히 단순해졌습니다.

```text
brief
run
review
status
```

`/run` skill도 35줄이며 state-machine protocol을 모델에게 설명하지 않습니다.

하지만 내부 coordinator는 현재 다음 파일을 직접 만들어야 합니다.

```text
canonical WorkBrief JSON
OutcomeDelta YAML
필요한 AuthorityRef와 digest
```

`run start`는 `--work-brief <file>`을 필수로 요구하고, close는 `--outcome <file>`을 요구합니다.

WorkBrief는 canonical sorted compact JSON을 요구하므로, 모델이 이를 손으로 조립하는 것은 의미 작업이라기보다 protocol 작업이 될 수 있습니다.

## 권장 수정

새 사용자 명령을 늘리지 말고, skill이 호출하는 deterministic scaffold를 내부적으로 제공하는 편이 좋습니다.

```text
harness가 자동 생성:
  brief_id/revision
  current ProjectFactRef
  binding digest
  candidate/evaluation lineage
  boilerplate schema
  CAS reference

coordinator가 작성:
  why now
  desired delta
  current semantic context
  fixed/free/escalation decisions
  expected evidence
```

즉 **프로토콜은 script가 만들고, 의미만 모델이 작성**해야 합니다.

이는 현재 설계 원칙과 정확히 일치합니다.

---

# 즉시 고쳐야 할 작은 정합성 문제

현재 committed `PROJECT_BRIEF.md`에는 역할이 과거의 6-role 용어로 남아 있는 binding commitment가 있습니다. 반면 실제 canonical domain과 profile은 다음 4개 역할만 허용합니다.

```text
coordinator
worker
verifier
reviewer
```

이것은 단순 문서 typo보다 중요합니다. `PROJECT_BRIEF.md`의 commitment는 owner authority이기 때문입니다.

해당 fact를 현재 4-role 모델로 수정하고 새 owner adoption record로 다시 결속해야 합니다. 코드가 문서에 맞춰 6-role로 돌아가면 안 됩니다.

---

# Release 상태

현재 release는 준비되지 않았습니다. 이는 최신 commit에서도 이미 인지한 상태입니다.

`release-to-main.sh`의 positive manifest에는 삭제된 legacy script들이 아직 들어 있습니다.

```text
scripts/dashboard.py
scripts/delegate.py
scripts/lanes.py
scripts/review.py
scripts/round.py
scripts/ssot.py
```

반대로 실제 신규 runtime인 `waystone/` package는 shipping manifest에 없습니다. `DEV_ONLY_PATHS`도 아직 `SSOT.md`를 이름으로 사용합니다.

따라서 현재 projection으로 release하면 다음 둘 중 하나가 발생할 수 있습니다.

* 필요한 신규 package가 배포에서 빠짐
* 삭제된 legacy surface를 여전히 release contract가 기대함

이것은 설계 재검토 사항이 아니라 국소적인 release blocker입니다.

수정 후 projected smoke는 단순 `status` import뿐 아니라 최소한 다음을 확인해야 합니다.

```text
waystone brief check
waystone status
waystone run --help 또는 typed start refusal
waystone review --help
```

실 backend 전체를 release smoke마다 돌릴 필요는 없습니다.

---

# 권고하는 마감 순서

## 1. 새 기능 동결

현재 control plane에 개념을 더 추가하지 않는 것이 좋습니다. 이미 필요한 domain vocabulary는 충분합니다.

## 2. 두 correctness gap 폐쇄

* promotion actor/evidence separation
* review objective/evidence authority resolution

## 3. Committed brief 정합화

* 6-role fact → 4-role fact
* 재-adopt

## 4. 세 가지 dogfood

| 시나리오                          | 확인할 것                                            |
| ----------------------------- | ------------------------------------------------ |
| 불확실한 architecture exploration | semantic brief와 context request가 실제 판단 품질을 높이는가  |
| confirmed major + accept-risk | task가 생기지 않고 objective-first status가 정상인가        |
| 평범한 국소 bugfix                 | 가짜 hypothesis나 과도한 evaluation ceremony를 요구하지 않는가 |

## 5. Release projection 갱신

* `waystone/` 포함
* legacy script 제거
* `PROJECT_BRIEF.md` semantics 반영
* projected runtime smoke

---

# 최종 판단

이번 구현은 제안한 명칭이나 방법론을 표면적으로 옮긴 것이 아닙니다. 다음 네 가지를 Waystone의 기존 trust model에 맞게 독립적으로 재구성했습니다.

```text
불확실성의 typed authority
stage별 assurance allocation
review truth와 priority의 분리
objective-relative progress
```

특히 다음은 실질적인 개선입니다.

* `SSOT`를 `PROJECT_BRIEF`로 교체
* provisional → explicit adoption
* WorkBrief semantic provenance
* context-requested resume
* REAL finding 자동 task화 제거
* explore의 저비용 assurance
* OutcomeDelta 기반 진행률
* 기존 governance test 대량 제거

따라서 **방향을 되돌리거나 다시 설계할 이유는 없습니다.**

다만 현재 `promote`는 외관상으로만 verifier/reviewer/coordinator action이 나뉘어 있고, 실제 evidence는 하나로 alias됩니다. 이 상태에서 “risk-proportional high-assurance promotion이 완성됐다”고 선언하는 것은 이릅니다. Review disposition도 objective authority를 실제로 재검증해야 합니다.

정식 판정은 다음과 같습니다.

> **0.13 intent-control-plane 아키텍처: 승인**
> **Explore/Evaluate 및 review disposition 방향: 승인**
> **Promote provenance: CHANGES REQUESTED**
> **Release readiness: 미승인**

위 두 correctness gap과 release manifest를 폐쇄하면, Waystone은 기존의 “엄격하지만 개발을 검증 루프로 끌어들이는 harness”에서 **불확실성을 보존하면서 필요한 순간에만 강한 assurance를 사용하는 개발 control plane**으로 실제 전환됐다고 평가할 수 있습니다.


---

<!-- waystone triage: BEGIN -->
## Findings (triage — 자유 형식 리뷰, verbatim 본문에서 직접 추출; 전 항목 코드 대조 검증 완료)

리뷰 정식 판정: "0.13 intent-control-plane 아키텍처: 승인 / Explore·Evaluate 및 review disposition 방향: 승인 / **Promote provenance: CHANGES REQUESTED** / Release readiness: 미승인".

| # | finding (리뷰 절) | verdict | type | severity | evidence (검증 근거) | task id |
|---|---|---|---|---|---|---|
| F1 | 필수 수정 1 — "Promote의 actor/evidence 분리가 명목상으로만 존재함": independent-verify·integration-decision·adversarial-review가 전부 동일 `evidence_digest`로 alias, `execute_assurance_dag()`는 action 이름·mutation boundary만 검사 | REAL | correctness | blocker | `waystone/runs/engine.py:894-902`에 인용 코드 그대로 존재(3개 handler 모두 `lambda: evidence_digest`). `waystone/runs/assurance.py:880-907` execute_assurance_dag는 handler 집합 일치 + mutation 경계만 검사, 반환 artifact 종류 미검증. `execute_stage`(engine.py:957~)도 record 존재·frame committed·lineage frozen만 확인. typed VerifierEvidence·ReviewCycle·integration-decision 스키마(verify.py:60-61)는 존재하나 이 public 경로에 미배선 — "이미 존재하는 타입과 경계를 실제로 연결하면 됩니다"는 리뷰 진단과 일치 | fix/promote-actor-evidence-separation |
| F1b | 필수 수정 1 하위 — "Risk-gated review도 public path에서 사실상 비활성": `compile_assurance_plan()`은 `declared_risks` 있을 때만 adversarial-review 포함, public `run start`는 미전달; review CLI에 reviewer artifact를 promotion lineage ReviewCycle로 붙이는 public 경로 없음 | REAL | correctness | blocker | `declared_risks`는 `waystone/runs/assurance.py:258·270`(시그니처, 기본 `()`)에만 존재 — 비테스트 호출자 전무. `waystone/cli/run_group.py:368` compile 호출부에 미전달. review CLI 하위명령은 ingest/validate/disposition/materialize 4개뿐(`waystone/cli/review_group.py:259-275`); ReviewCycle은 run_group.py:346에서 load만, append 경로 없음 | (F1 task에 포함) |
| F2 | 필수 수정 2 — "Review disposition의 objective/evidence가 shape만 검증됨": evidence_refs는 `{kind, digest}` 형식만, objective_ref는 필드 문자열·digest 형태만 검사, CAS 존재·PROJECT_BRIEF fact 재도출 없음, CLI는 parse 후 곧바로 append | REAL | correctness | blocker | `waystone/reviews/findings.py:197-202`(evidence_refs: kind 문자열 + digest 형식만), `:224-232`(objective_ref: 필드 shape만). CLI `waystone/cli/review_group.py:176`(append_validation)·`:184`(append_disposition) — parse 직후 append, authority resolution 부재. "confirmed finding → 존재하지 않거나 stale한 objective에 귀속 → fix-now → materialize" 경로가 실제로 열려 있음 | fix/review-disposition-authority-binding |
| F3 | 즉시 고쳐야 할 작은 정합성 문제 — committed PROJECT_BRIEF에 "역할이 과거의 6-role 용어로 남아 있는 binding commitment" 존재, canonical domain·profile은 4-role만 허용 | REAL | scope | major | `PROJECT_BRIEF.md:34` `commitment/roles-over-model-names`가 main·orchestrator·implementer·clerk·verifier·reviewer 6-role 명명. 코드는 `waystone/jobs/domain.py:9-13` Role enum 4종(coordinator/worker/verifier/reviewer)만 허용. **ruling(자율권 정책 확정)**: 0.13 mandate의 4-role이 canonical — fact 개정 + 재-adopt, 코드를 brief에 맞춰 되돌리지 않음(리뷰 권고와 동일) | fix/brief-role-model-realignment |
| F4 | Release 상태 — "release-to-main.sh의 positive manifest에는 삭제된 legacy script들이 아직 들어 있습니다"; `waystone/` package는 manifest에 없음; DEV_ONLY_PATHS가 SSOT.md 사용 | REAL | correctness | blocker (release 한정) | `release-to-main.sh:13-43` SHIP_PATHS에 scripts/{dashboard,delegate,lanes,review,round,ssot}.py 잔존·`waystone/` 부재, `:56` DEV_ONLY_PATHS에 삭제된 SSOT.md. **기존 task가 이미 커버** — 리뷰어 추가 세부(legacy script 6종 제거, projected smoke를 `brief check`/`status`/`run --help`/`review --help`까지 확장) 이 행으로 승계 | chore/013-release-prep (기존) |
| F5 | 전략적 주의 — "새 lifecycle이 또 다른 관료제가 될 가능성": explore learning criterion이 project hypothesis/question만 source 허용, owner 단순 bugfix 요청 사용 불가; "아직 확정 defect라고 보지 않습니다" — dogfood 선행 요구 | REAL (관찰 사실; defect 아님 — 리뷰어 스스로 한정) | architecture | minor | `waystone/jobs/completion.py:594-600` — "explore learning criterion must source a project hypothesis/question", prefix가 hypothesis/question 외이거나 binding≠nonbinding이면 거부. owner-request는 promote objective에서만 허용(`:572`). 대응은 구현이 아니라 dogfood 선행(리뷰 지시: "실제 불편이 반복되기 전에는 구현하지 않는 편이 맞습니다") | spike/013-lifecycle-dogfood |
| F6 | UX와 coordinator burden — `run start`가 `--work-brief <file>` 필수, close가 `--outcome <file>` 필수, WorkBrief canonical sorted compact JSON 수제작은 "의미 작업이라기보다 protocol 작업" — "프로토콜은 script가 만들고, 의미만 모델이 작성" | REAL | architecture | major | `waystone/cli/run_group.py:53·78-79`(start: --work-brief 필수)·`:84-85`(close: --outcome 필수). 권고 방향이 현 설계 원칙(deterministic scaffold + 모델은 semantics만)과 일치 | feat/workbrief-scaffold |
| F7 | §3 남은 한계 — staged execution이 non-external 거부, "0.13이 모든 execution category를 지원한다고 주장해서는 안 됩니다" — v1 범위를 external Codex worker/evaluator로 문서 한정 필요; context-transfer-cost 기반 canonical routing 미구현 | REAL | reporting | minor | `waystone/runs/engine.py:429` — "stage execution requires an external binding". ExecutionCategory 3종은 선언되나(`jobs/domain.py:21-24`) 실행은 external만. blocker 아님(리뷰어 명시) — 문서 범위 한정으로 처분 | docs/013-v1-execution-scope |

### 등록 요약

- REAL 8건(F1·F1b 통합 등록) / REJECTED 0건 / NEEDS-RULING 0건 — F3의 brief 해석은 ruling 자율권 정책(2026-07-22)으로 main이 즉석 확정·기록.
- blocker 3건: fix/promote-actor-evidence-separation, fix/review-disposition-authority-binding, (release 한정) chore/013-release-prep(기존).
- 신규 등록 6건 + 기존 승계 1건. 리뷰의 승인 항목들(brief typed authority·claim→validation→disposition 분리·stage별 assurance·OutcomeDelta 진행률·gate 정직성)은 처분 불요 — 방향 유지 근거로만 기록.
- 리뷰어 권고 마감 순서: 신기능 동결 → correctness gap 2건(F1·F2) → brief 정합화(F3) → dogfood 3종(F5) → release projection(F4). blocker 2건(F1·F2)은 다음 round가 downstream 작업을 소비하기 전에 해소.

### ingest 메타 상태

- 이 round는 request 발행 없는 수동 round — round binding·request-digest 부재로 reply는 "configured feedback"으로 미집계(pending 수신 상태), `review-skipped-closes-v1` 평가에는 **unevaluable(binding 부재)**. adaptive-rule fire 출력 없음. verbatim 사본은 backup 대조로 byte-exact 확인(22,550 bytes, offset 506).
<!-- waystone triage: END -->
