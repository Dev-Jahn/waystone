# ADR-0007: SQLite filesystem·backup·artifact GC 운영 계약을 확정한다

- Status: accepted
- Date: 2026-07-19
- Round: —
- SSOT sections affected: 없음 — 이 ADR의 권위 원천은 0.12 계획서 §5-5이다
- Tasks: docs/adr-state-authority-contracts

## Context

SQLite의 transaction 보장은 database path가 놓인 filesystem의 locking·durability 의미론에 의존한다. project-local 경로가 network, sync, virtual filesystem에 놓였는데도 engine이 임의로 다른 path나 약한 journal mode를 고르면 실행은 이어져 보이지만 authority와 crash safety가 달라진다. backup도 `state.db`만 복사하면 WAL의 committed data를 빠뜨리거나 서로 다른 시점의 sidecar를 조합할 수 있다. artifact cleanup은 live run이나 published audit evidence를 지우지 않도록 DB state, manifest reference, retention을 함께 판정해야 한다.

## Decision

### Filesystem과 database 위치

기본 database path는 project-local `.waystone/state.db`다. engine은 database를 열거나 생성하기 전에 resolved parent filesystem이 SQLite의 process locking, atomic replace, sync/durability, configured journal mode를 지원한다고 판정해야 한다. 알려진 network/sync filesystem이거나 이 성질을 증명할 수 없는 filesystem은 지원 대상으로 추정하지 않는다.

지원되지 않으면 `unsupported_state_filesystem` 같은 안정된 code, 판정한 path/filesystem, 필요한 성질을 담은 typed refusal로 중단한다. 오류는 명시적으로 설정할 수 있는 machine-local state path를 제안하되 자동으로 그곳을 선택하거나 DB를 이동하지 않는다. 사용자가 relocation을 승인·설정한 뒤에만 새 path를 사용한다. project-local open 실패를 `$WAYSTONE_HOME`, 임시 디렉터리, memory DB로 silent fallback해서는 안 된다.

기본 journal mode는 `WAL`이며 open 후 `PRAGMA journal_mode`의 실제 결과를 확인한다. filesystem이 이를 제공하지 못하거나 configured mode와 결과가 다르면 typed refusal한다. engine은 성공처럼 보이게 하려고 `DELETE`, `TRUNCATE`, memory journal 등으로 무단 강등하지 않는다. journal mode 변경이 필요하면 별도의 명시적 migration·운영 결정이어야 한다.

### 일관된 backup

`state.db`, `state.db-wal`, `state.db-shm`의 raw file copy는 backup으로 인정하지 않는다. checkpoint 요청만 성공했다고 raw copy가 일관된 backup으로 바뀌지도 않는다. 지원하는 backup 방식은 다음 중 하나다.

1. SQLite `sqlite3_backup` API로 일관된 source snapshot을 destination DB에 복제한다.
2. writer를 quiesce하고 checkpoint 성공을 확인한 뒤, 그 잠금을 유지한 채 일관성을 보장하는 atomic filesystem snapshot을 만든다.
3. SQLite가 transactionally 생성하는 `VACUUM INTO`를 사용한다.

backup 완료 후 destination에 SQLite integrity check를 실행하고, 실패한 산출물은 복구 가능한 backup으로 게시하지 않는다. relocation이나 restore 역시 engine이 database를 사용 중인 상태에서 raw overwrite하지 않는다.

### Artifact GC

GC는 reference를 먼저 mark하고 후보를 나중에 sweep하는 두 단계로만 수행한다.

**Mark roots:**

- 모든 nonterminal run과 그 action/job이 참조하는 artifact
- published closeout manifest가 digest로 참조하는 artifact
- retention 기간이 지나지 않은 `failed` 또는 `discarded` run의 artifact
- 현재 실행·검증·publication이 lease나 fence로 사용 중인 artifact

**Sweep 조건:**

- nonterminal run의 artifact는 어떤 age에서도 삭제하지 않는다.
- published closeout reference는 retention 만료나 run outcome보다 우선하며 자동 GC에서 보존한다.
- `failed`와 `discarded` artifact는 terminal 확인과 configured retention 만료를 모두 만족하고 다른 mark root가 없을 때만 후보가 된다.
- sweep 직전에 DB state와 manifest references를 다시 읽어 mark 이후 생긴 reference가 없는지 확인한다.
- 삭제 전에 artifact bytes의 digest를 다시 계산해 content-addressed identity 또는 기록된 expected digest와 일치하는지 확인한다. 불일치하거나 expected digest가 없으면 삭제하지 않고 typed integrity finding으로 격리한다.

`doctor`는 reference가 있는데 expected artifact bytes가 없는 **dangling reference**와, artifact bytes는 있지만 DB·published manifest·active lease 어디에서도 참조되지 않는 **orphan artifact**를 별도 유형으로 보고한다. dangling reference를 orphan으로 취급해 metadata를 지우거나, orphan이라는 이유만으로 retention·digest 검증 없이 즉시 삭제해서는 안 된다. ADR-0006의 verifier artifact가 외부에서 삭제된 경우 manifest digest는 남고 `doctor`는 이를 dangling reference로 보고하며, deep audit 가능성을 복구됐다고 주장하지 않는다.

## Consequences

- 지원되지 않는 checkout 위치에서는 run이 시작되지 않지만, 사용자가 명시한 machine-local relocation으로 안전하게 운영할 수 있다.
- backup은 WAL timing과 무관하게 하나의 일관된 SQLite snapshot을 갖는다.
- GC는 live work와 published closeout evidence를 보존하며, 삭제 전 digest mismatch를 data corruption 신호로 남긴다.
- `doctor` 출력만으로 missing evidence와 단순 미참조 파일을 구분할 수 있다.

## Alternatives considered

- **지원되지 않는 filesystem에서 자동 path fallback 또는 journal mode 강등** — 사용자가 모르는 authority·durability 변경이므로 기각.
- **DB와 WAL sidecar raw copy** — 서로 다른 transaction 시점이 섞이거나 committed WAL을 누락할 수 있어 기각.
- **age만으로 artifact 삭제** — nonterminal 작업과 published audit reference를 손상할 수 있어 기각.
