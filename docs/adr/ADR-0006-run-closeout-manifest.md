# ADR-0006: run closeout manifest의 add-only 최소 계약을 확정한다

- Status: accepted
- Date: 2026-07-19
- Round: —
- SSOT sections affected: 없음 — 이 ADR의 권위 원천은 0.12 계획서 §5-4이다
- Tasks: docs/adr-state-authority-contracts

## Context

active run의 SQLite lifecycle과 local artifact는 다른 머신에 Git만으로 전달되지 않는다. 종료 결과를 전달하려면 Git에 남는 요약이 필요하지만, 이를 DB dump나 criterion별 verdict 복사본으로 만들면 Git과 DB 사이에 두 번째 runtime authority가 생긴다. 또한 manifest 내용에 그 manifest를 게시할 commit SHA를 넣는 방식은 commit이 만들어지기 전에는 값을 알 수 없는 자기참조 순환이다. 계획서의 미결 항목은 2026-07-19 사용자 결정 옵션 b로 확정한다. local verifier artifact가 삭제되면 manifest에는 digest만 남고 deep audit에는 local artifact store가 필요하다.

## Decision

각 `run_id`에는 deterministic한 Git-tracked path로 식별되는 closeout manifest가 **정확히 하나**만 존재한다. publication은 그 path가 없다는 조건의 add-only CAS이며, 이미 게시된 bytes의 수정·교체·삭제를 금지한다. 동일 bytes의 재시도는 no-op일 수 있지만 다른 bytes의 두 번째 publication은 typed conflict다.

manifest는 다음 최소 필드만 가진다. 구현은 schema version을 올리지 않고 runtime 편의 필드를 추가해서는 안 된다.

| Field | Type | 계약 |
|---|---|---|
| `schema` | string | literal `waystone-run-closeout-1` |
| `run_id` | UUID string | ADR-0005의 canonical run identity이며 path의 run identity와 일치 |
| `task_id` | string | run이 실행한 등록 task identity |
| `outcome` | enum | `succeeded`, `failed`, `cancelled`, `discarded` 중 하나인 terminal 결과의 typed summary; 중간 FSM transition을 담지 않음 |
| `base_sha` | full Git object id | frozen code input commit |
| `code_result_sha` | full Git object id | run이 만든 code 결과 commit; publication commit과 다른 개념 |
| `run_spec_digest` | digest object | frozen closure·실행 입력을 결속하는 algorithm + digest |
| `verifier_artifact_digests` | array of digest objects | local verifier artifact 각각의 algorithm + digest; artifact body나 criterion verdict는 포함하지 않음 |

`outcome`은 closeout 결과이지 action 단계, heartbeat, process liveness, retry, claim, lease, transition log의 복제본이 아니다. 이 runtime lifecycle은 SQLite authority에 남고 manifest에 이식하지 않는다. manifest는 DB dump가 아니며 DB row를 빠짐없이 보존하려는 확장을 금지한다.

`code_result_sha`는 code 결과를 가리킨다. manifest publication 전에 publication 대상 remote의 durable ref에서 해당 object가 도달 가능함을 새로 관측해야 하며, 도달 가능성을 증명할 수 없으면 manifest를 게시하지 않는다.

publication commit은 **이 manifest 파일을 포함해 게시한 Git commit**에서 파생한다. `publication_commit_sha` 또는 동일한 자기 SHA 필드를 manifest 내용에 넣지 않는다. commit SHA는 manifest bytes와 surrounding tree로부터 나중에 생기므로 content identity가 아니며, cherry-pick 등 다른 publication history에서는 달라질 수 있다. 요약하면 `code_result_sha`는 필수 content field이고 publication commit SHA는 Git history에서 얻는 외부 provenance다.

### Verifier artifact 보존 한계

사용자 결정 옵션 b에 따라 manifest는 `verifier_artifact_digests`만 보존한다. 최소 criterion verdict, criterion별 pass/fail, verifier 출력 본문을 manifest에 복제하지 않는다. 이는 manifest를 두 번째 verdict store나 DB dump로 팽창시키지 않기 위한 의도적 경계다.

local artifact store에 digest와 일치하는 bytes가 있으면 deep audit가 가능하다. 그 artifact가 삭제되면 manifest는 어떤 digest가 참조됐는지만 증명하며 내용이나 criterion verdict를 복원하지 못한다. 이 경우 deep audit는 불가능하고 **해당 local artifact store가 필요하다**고 정직하게 보고해야 한다. digest 자체를 artifact 존재나 verifier 판정의 대체 증거로 해석해서는 안 된다.

## Consequences

- Git을 받은 머신은 run의 최소 결과와 code result를 식별할 수 있지만 active runtime history를 resume할 수는 없다.
- 자기참조 없이 manifest와 publication commit의 provenance를 모두 표현할 수 있다.
- manifest는 작고 immutable하지만 local verifier artifact가 보존되지 않으면 criterion 수준의 deep audit도 보존되지 않는다.
- published manifest가 가리키는 local artifact의 자동 GC 정책은 ADR-0007을 따르며, 외부 삭제가 일어나면 `doctor`가 dangling reference로 드러낸다.

## Alternatives considered

- **manifest에 자기 `publication_commit_sha` 포함** — commit 생성 전 값을 요구하는 자기참조 순환이므로 기각.
- **SQLite runtime lifecycle 전체를 manifest에 복제** — Git과 DB에 중복 authority를 만들고 manifest를 DB dump로 바꾸므로 기각.
- **최소 criterion verdict를 manifest에 이식** — verifier/verdict store의 두 번째 복사본이 되어 최소 계약을 팽창시키므로 사용자 결정 옵션 b에 따라 기각.

## Amendment (2026-07-20)

이 절은 위 Decision을 in-place amend한다. 서로 충돌하는 경우 이 절이 우선하고, 충돌하지 않는
원문은 그대로 유지된다. 이는 runtime 편의 필드 추가가 아니라 최초 구현 계약의 내부 모순을
교정하는 것이므로 schema literal은 `waystone-run-closeout-1`을 유지한다. 이 절 이전의
`task_id` 단수 및 모든 outcome에서 non-null `code_result_sha`를 요구하는 문구는 유효 계약이
아니다.

### 계획 §5-4 대비 deviation

계획 §5-4의 예시 schema와 다른 점을 다음처럼 확정한다. `의도적`으로 표시한 차이는 이 ADR의
최소 manifest 경계가 계획의 예시 필드 목록을 해당 범위에서 supersede한 것이며, 나머지 §5-4
제약은 유지된다.

| 계획 §5-4 항목 | 판정 | 유효 계약과 근거 |
|---|---|---|
| `base_snapshot_sha` → `base_sha` | 의도적 | 같은 frozen code input commit의 명칭만 정규화한다. |
| `task_ids` → `task_id` | **누락** | 단수 축소를 폐기하고 아래 `task_ids` 계약으로 교정한다. `task_id`는 허용하지 않는다. |
| `closure_digest` 제외 | 의도적 | `run_spec_digest`가 frozen closure뿐 아니라 실행 입력과 readiness 결정을 함께 결속하므로 별도 closure digest를 중복하지 않는다. |
| `decision_digest` 제외 | 의도적 | portable closeout에는 typed `outcome`만 두고 integration decision의 상세 provenance와 evidence는 I-04의 별도 actor/artifact authority에 남긴다. manifest를 두 번째 decision index로 만들지 않는다. |
| `verification` 제외 | 의도적 | durable code result는 `code_result_sha`로 식별하고 evidence 주소만 `verifier_artifact_digests`로 보존한다. exact result binding은 artifact body의 계약이며, `summary`·criterion verdict를 복제하지 않는 것은 사용자 결정 옵션 b이다. |
| `delivery` 제외 | 의도적 | `delivery.mode` policy는 `run_spec_digest`에 결속하고 delivery action은 runtime lifecycle에 남긴다. `result_ref`는 재관측해야 하는 Git fact이므로 manifest에는 fresh remote reachability를 통과한 code result만 남긴다. |
| `review` 제외 | 의도적 | external review는 계획 §5-4가 지정한 기존 `docs/reviews/*` protocol의 별도 Git authority에 남기며 nullable pointer를 중복하지 않는다. |
| `outcome` 추가 | 의도적 | runtime transition 전체를 복제하거나 필드 부재에서 추론하지 않고 run의 terminal 결과만 typed summary로 전달한다. |

### 유효 schema와 multi-task mapping

`waystone-run-closeout-1`의 유효 필드는 아래 아홉 개뿐이며 모두 존재해야 한다. 이 표와 충돌하는
위 Decision의 필드 type·cardinality는 이 표로 대체한다.

| Field | Type | 계약 |
|---|---|---|
| `schema` | string | literal `waystone-run-closeout-1` |
| `run_id` | string | ADR-0005의 canonical run identity |
| `task_ids` | array of strings | frozen closure의 등록 task identity 전체를 각각 정확히 한 번 포함 |
| `outcome` | enum | `succeeded`, `failed`, `cancelled`, `discarded` 중 하나인 run terminal summary |
| `base_sha` | full Git object id | frozen code input commit |
| `code_result_sha` | full Git object id or null | 관측·reconcile된 run-level code result commit 또는 아래 계약에 따른 null |
| `code_result_absence_reason` | enum or null | `execution-not-started`, `result-commit-not-produced`, null 중 하나 |
| `run_spec_digest` | digest object | frozen closure·실행 입력을 결속하는 algorithm + digest |
| `verifier_artifact_digests` | array of digest objects | 관측된 local verifier artifact 전체의 algorithm + digest; 관측된 artifact가 없으면 빈 배열 |

빈 `verifier_artifact_digests`는 artifact가 관측되지 않았다는 사실만 나타내며 required verification을
면제하거나 `succeeded`를 허용하지 않는다. outcome은 frozen completion contract를 먼저 충족해야 한다.

run당 manifest는 계속 정확히 하나다. `task_ids`는 실행 완료분이나 현재 wave가 아니라 ADR-0003의
**frozen closure 전체**와 set-equal이어야 하며, 중복·누락·closure 밖 task를 금지한다. 원본 task
identity bytes를 Unicode normalization이나 case folding 없이 unsigned UTF-8 byte order로 오름차순
정렬한다. 이는 membership 직렬화 순서이지 dependency, job 실행 또는 wave 순서를 뜻하지 않는다.
closure를 읽거나 `run_spec_digest`와의 결속을 검증할 수 없으면 manifest를 게시하지 않는다.

M1-B의 one task = one run/job은 길이 1인 `task_ids`로 표현한다. M2의 multi-job·multi-wave run은
모든 wave의 frozen closure task를 같은 run manifest 하나에 기록한다. task, job, wave별 manifest를
추가하지 않으며 retry·attempt·action identity도 `task_ids`에 넣지 않는다. 따라서 ADR-0003의
`Tasks: 3/5`, `Wave: 2/3`은 실행 중 SQLite projection으로 남고 closeout manifest cardinality와
충돌하지 않는다.

### No-result terminal 규칙

`code_result_sha`와 `code_result_absence_reason`은 다음 tagged pair만 허용한다.

| 조건 | `code_result_sha` | `code_result_absence_reason` |
|---|---|---|
| `succeeded` | non-null full Git object id | null |
| `failed`·`cancelled`·`discarded`, code result commit이 있음 | non-null full Git object id | null |
| `failed`·`cancelled`·`discarded`, result-producing execution/effect가 시작되지 않았음이 확인됨 | null | `execution-not-started` |
| `failed`·`cancelled`·`discarded`, result-producing execution/effect가 하나 이상 시작됐고 이를 terminal까지 관측·reconcile했으나 result commit이 만들어지지 않음 | null | `result-commit-not-produced` |

non-null SHA에는 기존 remote reachability 계약을 그대로 적용한다. publication 직전에 대상 remote의
durable ref에서 그 object의 도달 가능성을 새로 관측하지 못하면 게시를 거부한다. null에는 도달성
요구를 적용할 object가 없다. 그러나 result commit이 실제로 존재하는데 도달성을 증명하지 못한
경우, remote 관측 실패만 있는 경우, 또는 result 존재 여부가 `unknown`인 경우를 null로 강등해서는
안 된다. 이 경우 manifest publication 자체를 거부한다. `base_sha`나 다른 placeholder를 no-result
sentinel로 복사하는 것도 금지한다.

`execution-not-started`와 `result-commit-not-produced`는 supervisor/action journal과 external effect
reconciliation이 뒷받침하는 긍정적 사실이어야 한다. 로그·heartbeat 침묵, stderr, ref 조회 실패는
absence 증거가 아니다. manifest의 `cancelled`는 일반 cancel intent가 ADR-0003 FSM의 terminal
`canceled`에 도달한 경우만 요약하며 `cancel-requested`, `stopping`, `cancel-pending`에는 사용할 수
없다. `discarded`는 compatibility `delegate discard`를 포함한 typed discard intent가 같은
cancel/reconcile 경로로 terminalized된 경우에만 사용한다. `unknown-effect`가 남거나
`observed-quiescent`가 성립하지 않으면 어느 terminal outcome도 게시하지 않는다.

### Canonical publication path

canonical repository-relative path는 다음 하나다.

```text
docs/runs/<run-id>/closeout.yaml
```

`<run-id>`는 ADR-0005가 정의한 canonical run identity를 그대로 사용하며 이 ADR은 그 값 문법을
재정의하지 않는다. directory segment와 manifest payload의 `run_id`는 byte-for-byte 일치해야 한다.
writer, reader, absent-CAS는 이 exact path만 사용한다. case normalization, 다른 extension·leaf,
flat filename, shard 또는 다른 directory를 alias나 fallback으로 인정하지 않는다. add-only CAS와
동일-bytes retry·different-bytes typed conflict는 이 exact path에 적용한다.

### 계약 정합 anchor

| 개정 항목 | 정합 anchor | 성립 이유 |
|---|---|---|
| §5-4 deviation | ADR-0003 `status와 watch는 엄격한 read-only projection이다`; ADR-0010 `Frozen run spec과 readiness 경계` | manifest는 lifecycle을 복제하지 않고 `run_spec_digest`와 terminal summary만 전달한다. |
| multi-task mapping | ADR-0003 `Liveness와 progress 집계`; 계획 M1-B·M2 | frozen closure 전체를 run-level array 하나로 옮겨 `Tasks N/M`·wave별 runtime projection을 보존한다. |
| no-result terminal | ADR-0003 `취소, quiescence, cleanup 안전 계약`; E-08 | 긍정적 quiescence와 effect reconciliation 뒤에만 terminal/null을 기록하고 unknown에서는 publication을 거부한다. |
| canonical path | ADR-0005 `run_id 생성과 uniqueness 책임`; E-04 | canonical identity 하나를 exact Git path 하나에 결속해 서로 다른 absent-CAS 위치가 생기지 않는다. |

## Amendment (2026-07-22, 0.13 C2)

The closeout authority is now the completed run's `OutcomeDelta` pair published by `run close` to
the outcome ledger. Canceled runs are not ledger entries (R3). Explore closeout may remain local;
remote reachability is required only for live apply/promotion code results (E-04 is narrowed
accordingly). Legacy round closeout and compatibility aliases are not accepted as canonical input.
