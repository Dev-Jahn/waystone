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
