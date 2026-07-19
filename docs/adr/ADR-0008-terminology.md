# ADR-0008: public 용어·역할·실행 범주를 축소한다

- Status: accepted
- Date: 2026-07-19
- Round: —
- SSOT sections affected: 없음 — 권위 원천은 0.12 계획서 §1·§2-7·§2-8·§9 ruling #3과 proposal §6이다
- Tasks: docs/invariants-and-terminology

## Context

현재 표면은 하나의 work cycle과 그 job·입력·결과·결정을 `round`, `delegation`, `packet`,
`contract`, `verdict`, `lane`처럼 서로 다른 관점의 이름으로 노출한다. 역할 이름도 책임, 실행 위치, transport를 섞어 사용하고,
profile의 role × execution × effort 곱을 public setup에 드러낸다. 이 상태에서는 사용자가 내부
safety machinery를 이해해야 하며 I-10과 I-12를 약화한다. proposal §6의 축소안을 채택하되,
기존 artifact를 소급 개명하지 않는 계획서 §2-7, compatibility alias를 유지하는 §2-8,
내부 canonical 이름을 `run`으로 확정한 ruling #3을 함께 적용한다.

## Decision

### 용어 축소표

채택 용어에서 `public`과 `internal`을 함께 적은 행은 두 표면의 이름이 의도적으로 다르다는
뜻이다. 기존 이름은 legacy artifact 판독과 compatibility 입력에서만 허용하고 새 schema, API,
artifact에는 internal 용어를 사용한다.

| 기존 용어 | 채택 용어 | 폐기 사유 |
|---|---|---|
| SSOT | public: project brief / project spec; internal: intent source | “SSOT가 반드시 하나”라는 인상을 제거한다. 기존 파일명은 허용한다. |
| task registry | public: tasks; internal: task store | `registry`라는 구현어를 public에서 제거한다. |
| round | public: run / work cycle; internal: run | 표준 orchestration 용어로 통일한다. |
| delegation | public: worker job; internal: job / attempt | 한 task의 bounded 실행 단위로 축소한다. |
| packet | public: task contract; internal: job manifest | 입력 계약이라는 의미를 분명히 한다. |
| claim | public: worker report; internal: `worker_claim` | worker 주장이라는 provenance를 이름에 보존한다. |
| exposure | public: snapshot metadata; internal: execution snapshot | 제품 의미가 드러나지 않는 자의적 이름을 제거한다. |
| `contract.yaml` | public: job result; internal: `job_result` | 입력 contract와 결과 artifact의 혼동을 제거한다. |
| verdict | public: integration decision; internal: decision | 판정 대상과 결정 actor를 드러낸다. |
| apply / discard | public: accept / reject result; internal: integration action | 사용자가 보는 결과 처분을 직접 표현한다. |
| lane | public: parallel slot / wave; internal: wave | DAG scheduler의 표준 단위로 통일한다. |
| overlay delta | policy proposal | adaptive policy를 core execution과 구분되는 보조 기능으로 둔다. |
| compose | public: resolve policy; internal: policy resolution | 표준 configuration 용어로 바꾼다. |
| materialize | public: publish policy; internal: policy publication | 생성되는 결과를 직접 표현한다. |
| START_HERE / resume snapshot | public: handoff; internal: handoff projection | 재진입 정보를 하나의 개념으로 통일한다. |
| review packet | review request | 이미 통용되는 review 용어로 통일한다. |
| clerk | public에 노출하지 않음; internal: deterministic step / operator | 반복 bookkeeping을 model role이 아니라 deterministic step으로 내린다. |

`run`은 schema와 API의 내부 canonical entity이며 `/waystone:run`이 canonical skill command다.
`/waystone:round`는 1.0까지 동일한 canonical run API를 호출하는 compatibility alias로 유지하며 별도 `round` entity나 상태를 만들지 않는다.
기존 파일명과 historical artifact는 소급 개명하지 않는다. 이름 변경은 public 표면과 신규
artifact에만 적용한다.

### 역할 4개

canonical role은 다음 네 개뿐이다.

| 역할 | 책임 |
|---|---|
| `coordinator` | scope, plan, acceptance를 고정하고 integration의 최종 결정을 내린다. |
| `worker` | 하나의 bounded job을 구현하며 자신의 결과를 최종 수용하지 않는다. |
| `verifier` | job result를 독립적으로 test/check하고 result digest에 결속된 evidence를 남긴다. |
| `reviewer` | run 수준의 architecture·domain quality를 평가한다. |

`main`은 실행 위치이고 role이 아니다. `orchestrator`는 engine의 기능이며 role이 아니다.
`clerk` 작업은 deterministic step으로 실행한다. 기존 `implementer` profile binding은 M1-B의
profile v1 adapter가 `worker`로 판독하지만 새 profile의 canonical role로 발행하지 않는다.

role은 책임과 provenance를, ADR-0004의 `executor_kind` (`engine`, `carrier`, `user`)는 action을
실행할 권한과 책임을 나타낸다. 두 enum은 서로 대체하거나 추론하지 않는다.

### 실행 3범주

public execution abstraction은 다음 세 범주뿐이다.

| 실행 범주 | 의미 |
|---|---|
| `in-session` | 현재 host session이 bounded work를 직접 수행한다. |
| `subagent` | host가 격리된 agent context를 만들고 수행시킨다. |
| `external` | host 밖의 runner transport가 수행한다. |

clean/forked subagent, workflow, plugin workflow, external exec 같은 host-specific detail은
adapter capability로 내린다. profile은 책임에 사용할 backend를 말하고 engine은 host capability와
policy에 따라 transport를 선택한다. capability가 없으면 typed refusal하며 다른 범주로 조용히
가장하지 않는다.

ADR-0001의 `deterministic-workflow`는 네 번째 public 실행 범주가 아니다. 그것은 고정 plan
manifest를 수행하는 내부 orchestration procedure이고, host carrier와 leaf runner 실행 범주는
별도 축으로 유지한다. 이 ADR은 그 의미나 기존 evidence/verdict gate를 변경하지 않는다.

## Consequences

- public 문서와 setup은 run, 네 역할, 세 실행 범주만 먼저 설명할 수 있다.
- role, execution, `executor_kind`, carrier의 의미가 분리되어 provenance나 실행 권한을 이름에서
  잘못 추론하지 않는다.
- legacy command, profile, artifact 판독에는 adapter가 필요하지만 canonical store에는 legacy
  이름을 복제하지 않는다.
- 1.0까지 `/waystone:round`와 `/waystone:run`의 동작을 함께 검증해야 한다. alias 제거 또는 추가
  유지가 필요하면 1.0 경계에서 별도 결정을 기록한다.

## Alternatives considered

- 현재 vocabulary를 그대로 유지 — 한 run의 입력·실행·결정을 여러 제품 개념처럼 노출해 I-12를
  계속 위반하므로 기각.
- 기존 파일과 artifact를 일괄 rename — historical evidence와 compatibility를 파괴하고 계획서
  §2-7을 뒤집으므로 기각.
- role × execution × effort의 모든 조합을 public model로 유지 — 책임과 transport를 다시 결합하고
  setup 복잡도를 보존하므로 기각.
- `round`를 schema/API canonical 이름으로 유지 — ruling #3의 내부 canonical `run` 결정을
  뒤집으므로 기각.
