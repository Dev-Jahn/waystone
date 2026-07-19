# ADR-0011: canonical project와 checkout context를 분리한다

- Status: accepted
- Date: 2026-07-19
- Round: —
- SSOT sections affected: 없음 — ADR-0005·ADR-0007의 project 해석 경계를 정밀화
- Tasks: feat/canonical-project-identity

## Context

ADR-0005는 active project의 runtime state authority를 SQLite `state.db`로, ADR-0007은 기본
database path를 project-local `.waystone/state.db`로 확정한다. 그러나 현재 checkout의 root와
등록된 project의 root를 구분하지 않으면 linked worktree 안의 `cwd`도 project root처럼 보인다.
그 결과 같은 Git repository의 checkout마다 별도 runtime authority가 생길 수 있다.

2026-07-19에 잔류 `cwd`에서 registry 변경 명령을 실행해 linked worktree의 `tasks.yaml`에 변경을
기록하고 pre-0.9 project state migration까지 유발한 사고가 두 번 있었다. 0.12에서 같은 해석을
유지하면 단순한 Git-tracked registry 오염을 넘어 run definition, claim uniqueness, cancellation,
artifact GC를 포함한 runtime 상태 전체가 서로 다른 `state.db`에 쌓인다. 한 checkout의 active run이
다른 checkout에서는 존재하지 않는 것처럼 보이는 authority split이다.

이 ADR의 근거 범위는 `docs/reviews/2026-07-19-m0-contracts-feedback.md`의 JW-GPT-017 중
`실패 메커니즘`과 `필수 수정`, ADR-0002~0008, `docs/invariants.md`로 한정한다.

## Decision

### `ProjectContext` domain object

모든 project-scoped command는 path 하나가 아니라 다음 immutable value object를 먼저 resolve한다.

| Field | 의미 |
|---|---|
| `project_id` | 등록된 canonical project record의 안정적인 opaque identity. checkout path, project name, branch, `HEAD`에서 파생하지 않는다. |
| `canonical_root` | 해당 `project_id`를 등록할 때 확정한 primary checkout root. linked worktree root를 등록값으로 허용하지 않는다. |
| `active_worktree_root` | 현재 명령이 호출됐거나 명시적 selector가 선택한 Git worktree root. canonical checkout이면 `canonical_root`와 같다. |
| `git_common_dir` | active checkout과 canonical checkout이 공유한다고 Git이 보고한 absolute common administrative directory. 같은 repository family인지 판정하는 현재-machine locator이며 `project_id`의 대체물이 아니다. |
| `checkout_identity` | canonical checkout과 각 linked worktree를 구분하는 opaque identity. branch, `HEAD`, filesystem metadata가 아니라 Git의 per-worktree administrative identity에 결속한다. |

`checkout_identity`는 checkout lifetime의 context identity다. worktree를 제거한 뒤 다시 만들면 새
identity가 될 수 있으며, 이를 durable project identity로 승격하지 않는다. canonical checkout은
reserved canonical identity를 사용하고 linked worktree는 `git_common_dir` 아래에서 Git이 부여한
per-worktree administrative identity를 사용한다.

### Resolve 순서와 fail-closed 규칙

engine은 intent, task, policy 또는 consent를 읽거나 쓰기 전, 특히 database path를 계산하거나
DB open·migration을 수행하기 전에 다음 순서로 `ProjectContext`를 확정한다.

1. 명시적 project selector가 있으면 그 입력을, 없으면 `cwd`를 starting location으로 삼아 Git이
   보고하는 `active_worktree_root`, worktree-private Git dir, `git_common_dir`를 읽는다. 이 단계의
   root는 active checkout 후보일 뿐 canonical project 판정이 아니다.
2. machine registry의 각 registered canonical root에서 Git 정보를 새로 resolve하고, active
   checkout과 같은 `git_common_dir`를 공유하는 canonical project record를 찾는다. registry의 path
   문자열이나 directory 모양만으로 같은 project라고 추측하지 않는다.
3. 정확히 한 record의 `project_id`와 `canonical_root`가 일치하고 active checkout이 Git의 worktree
   registration에 속함을 확인한다. canonical root 자체가 linked worktree이거나, 일치 항목이 없거나,
   둘 이상이거나, Git 관측이 불완전하면 stable code를 가진 typed refusal로 중단한다.
4. Git의 worktree-private administrative identity로 `checkout_identity`를 만들고,
   `active_worktree_root == canonical_root`인지에 따라 canonical 여부를 확정한다.

대표 refusal code는 `project_context_unregistered`, `project_context_ambiguous`,
`canonical_root_is_linked_worktree`다. mapping에 실패했을 때 main worktree나 가장 가까운 `.waystone`
directory를 추측하지 않는다. 이 규칙은 I-09와 I-11의 fail-toward-verification을 project resolution에
적용한 것이다.

### Command policy

| Command 성질 | canonical checkout | noncanonical linked worktree |
|---|---|---|
| read-only `status`·`inspect` | canonical DB를 읽는다. | 같은 `project_id`의 canonical DB로 정규화해 읽는다. |
| task·config·consent 등 project intent 변경 | 허용한다. | `noncanonical_intent_mutation` typed refusal로 기본 거부한다. |
| canonical checkout을 input으로 하는 run 시작 | 기존 UX대로 별도 selector 없이 허용한다. | current checkout을 암묵적으로 input으로 사용하지 않는다. |
| linked worktree를 의도한 run input으로 사용 | 명시적 `--from-worktree` selector가 가리킨 checkout을 검증한 뒤 허용한다. | 명시적 `--from-worktree` selector가 현재 checkout을 가리킬 때만 허용한다. |

`--root`는 canonical project를 찾기 위한 project selector이지 linked checkout을 run input으로
승격하는 consent가 아니다. linked worktree path를 `--root`로 넘겨도 intent mutation refusal이나
`--from-worktree` 요구를 우회하지 못한다. `--from-worktree`를 사용해도 `project_id`, runtime DB,
policy authority는 canonical project에 남고 선택한 checkout은 frozen run input에만 기록된다.

Waystone-owned worker worktree와 integration worktree에서는 project-intent mutation을 항상
거부한다. 해당 checkout을 `--from-worktree`로 다시 지정해도 이 금지는 해제되지 않는다. engine이
그 checkout에서 만드는 code result와 engine-owned integration action은 project intent mutation과
별개의 frozen action contract를 따른다.

ADR-0003의 계약에 따라 linked worktree에서 실행한 `status`·`inspect` 정규화도 엄격한 read-only다.
canonical DB open에 필요한 schema migration, cache repair 또는 registration 변경이 감지되면 조회가
그 작업을 대신 수행하지 않고 typed unavailable/refusal을 반환한다.

### ADR-0005·ADR-0007과의 관계

ADR-0005 표의 “active project의 SQLite `state.db`”는 이 ADR이 resolve한 `project_id`의 canonical
runtime authority를 뜻한다. Git-tracked intent bytes는 계속 Git이 authority이며 DB가 그 payload의
두 번째 authority가 되지 않는다는 ADR-0005의 규칙은 바뀌지 않는다.

ADR-0007의 기본 `project-local .waystone/state.db`는
`canonical_root/.waystone/state.db`로 해석한다. 순서는 다음과 같이 고정한다.

```text
resolve ProjectContext
→ prove canonical project mapping
→ derive canonical DB path (or its explicitly configured relocation)
→ run ADR-0007 filesystem and journal-mode preflight
→ open or migrate DB
```

따라서 linked worktree의 `.waystone/state.db`를 먼저 열어 본 뒤 canonical DB로 fallback하지 않는다.
ADR-0007이 허용한 명시적 machine-local relocation도 `project_id`에 결속되며 active linked worktree별
path가 되지 않는다. 이 ADR은 ADR-0007의 filesystem, WAL, backup, GC 결정을 뒤집지 않고 그보다
앞선 project identity 판정 지점을 추가한다.

## Consequences

- 같은 Git repository의 linked worktree들은 하나의 `project_id`와 runtime DB를 공유하되 서로 다른
  `checkout_identity`를 가진다.
- read-only 관측은 어느 checkout에서 시작해도 같은 runtime authority를 보며, 조회 자체가 migration이나
  state mutation을 만들지 않는다.
- project intent 변경과 noncanonical checkout input 선택은 우발적인 `cwd`에 의해 발생하지 않는다.
- 등록 정보나 Git worktree 관측이 손상·모호하면 정상 checkout을 추측하는 대신 command가 중단된다.

## Alternatives considered

- **현재 `cwd`에서 발견한 root를 project root로 사용** — linked worktree마다 Git intent와 runtime
  DB가 갈라져 2026-07-19 사고를 재현하므로 기각.
- **`git_common_dir` 자체를 `project_id`로 사용** — clone·relocation에 따라 달라지는 machine locator를
  durable domain identity로 오인하므로 기각.
- **linked worktree에서도 모든 mutation을 허용하고 경고만 표시** — 잔류 `cwd`가 project intent를
  바꾸는 실패를 막지 못하므로 기각.
- **모든 command에 `--root`를 의무화** — 정상 canonical checkout의 public UX까지 복잡하게 만들며
  `--root`만으로 checkout input consent를 표현하지도 못하므로 기각.
