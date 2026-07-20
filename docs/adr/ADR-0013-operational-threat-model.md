# ADR-0013: 우발 손상 경계의 operational threat model을 고정한다

- Status: accepted
- Date: 2026-07-20
- Round: —
- SSOT sections affected: 없음 — 0.12 계획서 M0-B와 §9 ruling 2를 운영 계약으로 완결
- Tasks: fix/m0-threat-model-completion

## Context

0.12 계획서 M0-B는 위협모델을 M4 사용자 문서가 아니라 store와 executor 구현 전의 설계 입력으로
요구한다. 계획서 §9 ruling 2는 보호 경계를 확정했지만, DB·artifact permission과 symlink의 실패
방향, 위임 child environment 전달, lease·lock principal은 구현자가 임의로 정할 수 있는 상태였다.
반면 DB filesystem 배치, same-machine linked-worktree context, runner probe의 config fingerprint 범위는
이미 accepted ADR와 invariant가 확정했다.

이 ADR은 기존 결정을 한 operational contract로 흡수하고 남은 축만 결정한다. 여기서 artifact는
ADR-0005의 digest-addressed local artifact store를 뜻한다. Git-tracked project record나 사용자가
관리하는 source tree 전체의 permission·symlink 정책을 새로 정하지 않는다.

## Decision

### 보호 경계

0.12 계획서 §9 ruling 2의 마스터 경계를 그대로 적용한다.

- 보호 대상은 crash, partial write, tool bug, 정상 운영 중의 이름·귀속 충돌을 포함한 **우발 손상**이다.
- 의도적 로컬 파일 조작·위조·adversarial filename 생성, 다중 사용자 권한 경계,
  container·namespace 경계는 **명시적 비보호 대상**이다.

아래의 mode, no-follow, allowlist, token 대조가 의도적 조작 일부도 막을 수는 있지만 이는 지원하는
adversarial security boundary가 아니다. 동일 OS account나 privileged local actor에 대한 방어,
signature·seal·별도 hardened namespace는 도입하지 않는다.

### 기존 결정 흡수

| 계획서 축 | 기존 권위 anchor | 흡수하는 계약 | 이 ADR의 처분 |
|---|---|---|---|
| 보호 대상·의도적 비보호 대상 | `dev_docs/0.12.0-refactor-plan.md` §9 ruling 2 | 우발 손상만 보호하고 의도적 로컬 조작·다중 사용자·namespace는 보호하지 않는다. | 그대로 흡수하며 재결정하지 않는다. |
| DB 배치와 filesystem | ADR-0007 `Filesystem과 database 위치` | 기본은 `canonical_root/.waystone/state.db`; 미지원 filesystem은 `unsupported_state_filesystem` typed refusal과 명시적 relocation 제안으로 중단하며 silent fallback·journal mode 강등은 금지한다. | permission·symlink preflight가 이 위치 결정을 앞지르거나 자동 relocation을 일으키지 않는다. 배치를 재결정하지 않는다. |
| same-machine shared checkout | ADR-0011 `ProjectContext`, `Resolve 순서와 fail-closed 규칙`, `Consequences` | linked worktree들은 한 `project_id`와 canonical DB를 공유하고 서로 다른 `checkout_identity`를 가진다. 모호한 mapping과 noncanonical intent mutation은 typed refusal한다. | checkout identity와 command policy를 재결정하지 않는다. 다중 사용자 shared checkout은 마스터 경계 밖이다. |
| config fingerprint 범위 | `docs/invariants.md` E-03 | checkout·machine·principal·runtime(config 내용 포함)이 exact-match일 때만 runner probe proof를 재사용하며 `not-observed`는 그 상태끼리 동등하다. | 축을 줄이거나 넓히지 않는다. mismatch면 proof를 재사용하지 않고 재probe하며, 재관측 불가는 `runner_probe_unavailable`로 드러내고 이전 proof의 truthy fallback으로 바꾸지 않는다. |

### 축별 operational contract

| 축 | 보호 대상 | 의도적 비보호 대상 | fail-closed 방향 |
|---|---|---|---|
| 전체 threat boundary | crash·부분 쓰기·도구 버그·정상 운영 중 이름/귀속 충돌 | 의도적 로컬 조작·위조·adversarial filename, 다중 사용자 권한, container/namespace | 아래 각 축의 typed refusal 또는 `unknown`을 success로 축약하지 않는다. |
| DB permission | 잘못된 owner·mode·umask로 생긴 우발적 공유 접근과 DB/WAL/SHM mutation | 동일 account·privileged actor의 의도적 변조, ACL을 security boundary로 삼는 운영 | DB open·migration 전에 검증한다. 불일치나 관측 불가는 `unsafe_state_permissions`; 자동 `chmod`·`chown`·relocation 없이 stateful command를 거부한다. |
| Artifact permission | staging bytes의 오귀속과 finalized content-addressed bytes의 우발적 재쓰기 | 의도적 local artifact 변조·삭제와 다중 사용자 confidentiality 경계 | root 결함은 `unsafe_artifact_permissions`로 store operation을 거부한다. 개별 leaf 결함은 그 artifact를 신뢰하지 않고 해당 run에 격리하며 다른 run 투영은 계속한다(E-06). 자동 수리·덮어쓰기는 금지한다. |
| Shared checkout | 잔류 `cwd`·linked worktree 혼동으로 생기는 project authority split | 다중 사용자 shared checkout의 상호 격리 | ADR-0011의 `project_context_*`·`noncanonical_intent_mutation` refusal을 그대로 따른다. 미지원 shared filesystem은 ADR-0007에 따라 거부한다. |
| Symlink | engine-owned state/artifact path의 우발적 redirect, root escape, 잘못된 file kind | 의도적 symlink swap·TOCTOU 공격에 대한 보안 보장, user source tree의 일반 symlink | engine-owned subtree에서 symlink를 follow하지 않는다. symlink·escape·type mismatch·검증 불가는 각각 typed refusal하며 target을 unlink·replace하거나 다른 path로 fallback하지 않는다. |
| Config fingerprint | 다른 checkout·machine·principal·runtime proof의 우발적 재사용 | 의도적 proof bytes 위조 | E-03 exact-match가 아니면 재사용을 거부한다. 새 probe를 관측할 수 없으면 `runner_probe_unavailable`이며 관측 불가를 일치로 추측하지 않는다. |
| Delegated child env | ambient parent env에 의한 credential 노출, tool/runtime drift, engine control 오염 | 악성 child에 대한 sandbox·secret confinement, 동일 account가 다른 채널로 읽는 값 | 빈 map에서 closed allowlist를 구성한다. 필수 env 부재·해석 불가는 `child_env_required_missing`, 금지 env 요청은 `child_env_not_allowed`로 spawn 전에 거부하며 parent env 전체 상속으로 retry하지 않는다. |
| Lease·lock principal | stale·다른 owner가 heartbeat, effect, submit, apply, cleanup을 수행하는 우발적 ownership 혼동 | owner token 탈취·DB 위조·동일 account의 의도적 lock 방해 | 현재 token·epoch·entity version 또는 실제 OS lock handle을 증명하지 못하면 `lease_principal_mismatch`, `lease_principal_unknown`, `lock_busy`, `lock_principal_unknown`으로 mutation을 거부한다. expiry·PID·lockfile bytes만으로 takeover하지 않는다. |

### DB·artifact permission과 symlink

POSIX mode를 제공하는 filesystem에서 engine이 새로 만드는 runtime object의 기본은 다음과 같다.

| Object | 생성 mode | 사용 전 의미 검사 |
|---|---|---|
| engine-owned runtime·artifact directory | `0700` | effective OS account가 소유하고 owner가 탐색·쓰기 가능하며 group/other write가 없어야 한다. |
| mutable DB, `-wal`, `-shm`, lock metadata, artifact staging file | `0600` | effective OS account가 소유하고 owner read/write가 가능하며 non-owner write가 없어야 한다. |
| digest 검증과 atomic publish를 마친 immutable artifact | `0400` | regular file이고 owner가 읽을 수 있으며 어떤 class에도 write bit가 없어야 한다. |

mode는 우발적 노출·수정의 표면을 줄이는 운영 기본값이지 다중 사용자 격리 보장이 아니다. ACL,
privileged access, 동일 account의 의도적 `chmod`는 보호 범위 밖이다. 기존 object의 permission을 자동으로
넓히거나 좁히지 않는다. 필요한 owner access, ownership, non-owner write 여부 또는 finalized artifact의
불변성을 증명할 수 없으면 각각 `unsafe_state_permissions`나 `unsafe_artifact_permissions`로 거부한다.
permission을 판정할 수 없는 filesystem에서 성공으로 추측하지 않는다.

path 검사는 ADR-0011이 `ProjectContext`로 canonical root를, 또는 ADR-0007에 따라 사용자가 명시한
relocation root를 확정한 **뒤** 그 engine-owned runtime root와 하위 component에만 적용한다. OS 전체
ancestor나 user source tree의 symlink를 금지하지 않는다. DB leaf와 sidecar, artifact root, staging,
final artifact, lock path는 no-follow 방식으로 열고 expected regular-file/directory kind와 확정 root
containment를 확인한다.

- DB endpoint·sidecar·managed state directory의 symlink, escape, kind mismatch는 DB를 열거나
  migration하기 전에 각각 `state_path_symlink`, `state_path_escape`, `state_path_type_mismatch`로
  command 전체를 거부한다.
- artifact root의 같은 결함은 각각 `artifact_root_symlink`, `artifact_root_escape`,
  `artifact_root_type_mismatch`로 artifact store operation 전체를 거부한다.
- 개별 artifact leaf의 같은 결함은 target을 follow·hash·delete하지 않고
  각각 `artifact_path_symlink`, `artifact_path_escape`, `artifact_path_type_mismatch` integrity
  finding으로 해당 artifact와 run을 격리한다.
- no-follow와 containment를 증명할 수 없으면 `engine_owned_path_unverifiable`이다. 자동 follow,
  unlink, replace, relocation은 허용하지 않는다.

이는 우발적인 symlink 생성과 path confusion을 감지하는 계약이다. 의도적 swap race를 막는
tamper-proof filesystem을 주장하지 않는다.

### Delegated child environment

child environment는 `os.environ` 복사본에서 빼는 방식이 아니라 빈 map에 다음 세 종류만 더해 만든다.

1. engine이 생성·정규화한 action/run identity, workspace, isolated temp/cache, locale·color 같은
   non-authority runtime 값
2. frozen action spec 또는 executor adapter schema가 이름·source·normalization까지 선언한
   toolchain/config environment
3. action capability와 사용자 consent가 명시한 credential binding

실제 credential value는 ambient parent에 같은 이름이 있다는 이유만으로 가져오지 않으며 log나
artifact에 평문으로 기록하지 않는다. 전달되는 effective environment의 이름과 behavior-affecting
값 또는 credential binding identity는 invocation/input digest에 결속해 ADR-0012의 frozen
`VerificationPlan`과 같은 environment를 preflight와 실행에서 사용한다.

나머지 parent environment는 전달하지 않는다. undeclared interpreter·loader·virtualenv·VCS·shell
startup·proxy·SSH socket·token 변수와 host orchestration control은 ambient inheritance 대상이 아니다.
특히 lease `owner_token`, fencing authority, DB mutation authority, live lock handle은 child에게 전달할
수 없다. 필요한 allowlisted 값이 없거나 확정할 수 없으면 `child_env_required_missing`, forbidden
authority나 allowlist 밖 값을 contract가 요구하면 `child_env_not_allowed`로 dispatch 전에 중단한다.
다른 executor나 full parent environment로 silent retry하지 않는다.

이 allowlist는 우발적 inheritance와 실행 drift를 막는다. 악성 child가 동일 account의 filesystem,
process, network에서 secret을 찾지 못하게 하는 sandbox 계약은 아니다.

### Lease·lock principal

E-03의 probe `principal` 축과 lease principal은 서로 다른 계약이다. 이 ADR은 E-03의 principal 의미를
재결정하지 않는다. lease principal은 그 probe 축, username, UID, PID, hostname, `cwd`가 아니라 engine이
특정 action claim에 발급한 opaque `owner_token`으로 식별되는 **engine-owned claim incarnation**이며,
최소한 다음 현재 DB 사실에 결속한다.

```text
project_id · run_id · action_id · executor_kind · owner_token · fencing_epoch · entity_version
```

모든 leased action의 principal은 claim을 획득한 engine-owned supervisor instance다. `executor_kind`나
carrier dispatch는 action 결속값일 뿐 child worker·carrier·model·user approval을 lease owner로 승격하지
않는다. supervisor만 owner token을 보유하고 heartbeat와 authoritative process telemetry를 쓴다.
token은 우발적인 owner 혼동을 막는 opaque identity이지 signature·secret seal이 아니다.

heartbeat renew, effect 시작, submit, completion, apply와 cleanup은 매번 current action의
`owner_token + fencing_epoch + entity_version`을 exact-match/CAS한다. mismatch는
`lease_principal_mismatch`; row나 current principal을 확정할 수 없으면 `lease_principal_unknown`이다.
둘 다 renew, effect, submit, takeover, destructive cleanup을 허용하지 않는다. lease expiry나 heartbeat
부재는 ADR-0002·ADR-0003대로 correctness나 quiescence 증거가 아니며 이 거부를 해제하지 않는다.

짧은 single-machine critical section의 OS lock principal은 lockfile에 적힌 PID·hostname·token이 아니라
kernel advisory lock handle을 실제로 보유한 engine process다. action-bound section은 handle을 획득한
뒤에도 DB의 current token·epoch·version을 다시 확인한다. contention은 `lock_busy`, handle 보유를
검증할 수 없으면 `lock_principal_unknown`으로 critical section에 진입하지 않는다. stale lockfile을
지우거나 process metadata를 추측해 우회하지 않으며 OS lock은 DB lease·fencing을 대체하지 않는다.

## Consequences

- M1 store와 executor는 permission, path, child env, principal을 성공 전에 preflight해야 한다.
- 우발적 drift는 typed refusal 또는 E-06 범위의 artifact 격리로 드러나며 자동 수리·fallback하지 않는다.
- owner-only mode와 no-follow 검사가 있어도 의도적 local tampering이나 multi-user isolation을
  지원한다고 주장할 수 없다.
- E-03의 probe execution principal, lease principal, OS lock holder는 서로 다른 개념으로 유지된다.

## Alternatives considered

- **parent environment를 복사한 뒤 일부 위험 변수만 제거** — 새 변수와 host별 control surface가
  암묵적으로 전달되고 frozen execution input을 닫을 수 없으므로 기각.
- **UID·PID·hostname 또는 lockfile bytes를 lease/lock owner로 사용** — scoped locator와 ambient
  metadata를 durable ownership으로 오인하고 stale process의 mutation을 막지 못하므로 기각.
- **permission mismatch를 자동 `chmod`하거나 symlink target을 따라 계속 실행** — 사용자가 승인하지
  않은 state mutation과 authority path 변경을 일으키므로 기각.
- **signature·sealed artifact·hardened namespace로 local tampering까지 방어** — §9 ruling 2의 명시적
  비보호 범위를 확장하므로 기각.
