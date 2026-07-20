# REPORT — m1b-effects / feat/run-effect-protocol

## 1. 구현 요약과 파일 목록

- 기준 커밋: `8c1cf88 feat(m1b): lease·fencing 원시 계층`
- 외부 effect action의 `planned → claimed → effect → observed → completed` 수명주기와 crash reconcile 엔진을 구현했다.
- 구현 registry는 계약 범위인 `git-ref`, `worktree`, `artifact-write`, `runner-execution`, `patch-integration` 다섯 종류만 등록한다. `push`, `github-marker` 및 기타 미등록 kind는 mutation 전에 typed refusal한다.
- Git 사실은 `waystone.adapters.git`만을 통해 관측한다. ref CAS, worktree registration/HEAD, artifact 최종 bytes digest, runner completion marker/process identity/output artifacts, integration commit parent/tree를 각각 독립 재도출한다.
- 관측 불가·malformed authority·불확실한 runner는 absent로 축약하지 않고 `unknown-effect`로 대기시킨다. conflict/partial 결과는 보존하며 blind retry나 destructive cleanup을 하지 않는다.
- runner는 실제 process 생성 대신 pluggable executor 경계를 제공한다. write-ahead intent를 먼저 고정하고 completion marker와 process identity를 검증하며, 같은 의미 입력의 동시/반복 action은 store transaction 안의 append-only lineage reservation으로 차단한다.
- store schema는 v1을 유지했다. 기존 v1 API를 재작성하지 않고 package-internal atomic plan/runner reservation 및 guarded effect transition surface만 추가했다.
- 신규 의존성은 없다.

변경 파일:

- `waystone/runs/effects.py` — 신규 effect protocol, 5종 driver/observer, reconcile API, typed result/error 및 fixture callback 경계
- `waystone/runs/store.py` — additive package-internal planned-action transaction, runner lineage reservation, guarded lifecycle transition, generic transition 우회 차단
- `scripts/tests/test_run_effects.py` — 신규 계약/fault-injection 테스트 23개
- `scripts/tests/run_tests.py` — `RunEffectTests` import 및 aggregate 등록만 추가

## 2. 계약 매핑

`docs/promoted-contracts.md`의 PC 행 가운데 이 task에 직접 귀속된 행은 없다. `dev_docs/m1b-slice-plan.md` §3/§5의 이 task 행은 `ADR-0002 전체`, fixture 3, effect-kind별 crash fixture를 직접 귀속한다.

| 귀속 계약 | 이를 단언하는 테스트 함수 |
|---|---|
| ADR-0002 공통 5단계, immutable digest/idempotency key, exact lease principal, completed의 observed digest transaction commit | `test_plan_is_atomic_immutable_and_unknown_kinds_refuse_before_mutation`, `test_five_stage_lifecycle_commits_observed_digest_under_exact_lease`, `test_effect_action_cannot_bypass_guarded_five_stage_lifecycle` |
| ADR-0002 registry — 정확히 5종 구현, 미등록 kind typed refusal, 실제 authority 재관측 | `test_plan_is_atomic_immutable_and_unknown_kinds_refuse_before_mutation`, `test_registered_effect_kinds_use_real_authority_observation` |
| Git ref — expected-old OID CAS, 실제 ref OID 관측, full-ref/깨진 ref 판독 경계 | `test_git_ref_expected_old_oid_cas_updates_only_the_expected_state`, `test_full_ref_boundary_and_broken_ref_authority_fail_before_effect`, `test_git_adapter_exceptions_and_malformed_facts_are_unknown` |
| Worktree — action별 고정 path/ref, `git worktree list`와 HEAD 공동 관측, partial/conflicting registration 보존 | `test_registered_effect_kinds_use_real_authority_observation`, `test_malformed_worktree_authority_is_unknown_not_absent`, `test_worktree_ref_registered_elsewhere_or_partial_after_intent_conflicts` |
| Artifact write — digest path, atomic publication, 최종 bytes 재해시, 충돌 시 비덮어쓰기 | `test_registered_effect_kinds_use_real_authority_observation`, `test_conflicting_git_and_artifact_state_is_preserved_without_blind_retry` |
| Runner — action당 at-most-once WAI, marker/process identity/output digest 관측, 불확실 시 재발사 금지 | `test_runner_write_ahead_intent_missing_or_mismatched_marker_never_relaunches`, `test_runner_marker_identity_and_output_artifacts_require_independent_proof` |
| Runner retry — terminal 확인 뒤 새 attempt + 새 action만 허용, 같은 입력의 plain/concurrent planning 차단 | `test_retry_requires_completed_old_action_new_attempt_and_new_action`, `test_runner_retry_lineage_cannot_be_bypassed_with_plain_plan`, `test_concurrent_runner_planning_atomically_reserves_one_invocation` |
| Patch integration — expected parent/tree precondition, integration commit parent/tree 재도출, ref CAS | `test_patch_adoption_rederives_expected_parent_tree_precondition`, `test_registered_effect_kinds_use_real_authority_observation`, `test_conflicting_git_and_artifact_state_is_preserved_without_blind_retry` |
| ADR-0002 crash 결정표 — 5종 각각 효과 전 사망은 긍정적 absent/quiescence 뒤 최초 실행 | `test_all_effect_kinds_crash_before_effect_reconcile_one_first_execution` |
| ADR-0002 crash 결정표 — 5종 각각 효과 후 completion commit 전 사망은 재실행 없이 관측 채택 | `test_all_effect_kinds_crash_after_effect_reconcile_without_reexecution` |
| 필수 fixture 3 — effect 완료 후 DB completion 전 kill, `exited-unreconciled`, resume 1회 수렴 및 2차 no-op | `test_fixture_3_exited_unreconciled_runner_completes_exactly_once` |
| 관측 채널 불가/authority 판독 실패 — `unknown-effect`, destructive 진행 0 | `test_all_kinds_unavailable_observation_wait_unknown_without_destruction`, `test_real_git_and_filesystem_read_failures_are_unknown_not_absent`, `test_git_adapter_exceptions_and_malformed_facts_are_unknown` |
| 관측 receipt 결손·변조 시 completed 오승격 금지 | `test_observed_recovery_requires_bound_rehashable_observation_receipt` |
| stale principal의 intent/effect 시작, submit, completion 거부 | `test_stale_principal_effect_start_submit_and_completion_are_guarded` |
| D5 — 신규 Git 사실은 `waystone.adapters.git` 단일 원천 | `test_registered_effect_kinds_use_real_authority_observation`, `test_git_adapter_exceptions_and_malformed_facts_are_unknown` |
| D10 — 같은 action의 5종 semantic idempotency, completed effect의 2차 수행 0 | `test_all_effect_kinds_crash_after_effect_reconcile_without_reexecution`, `test_conflicting_git_and_artifact_state_is_preserved_without_blind_retry`, `test_git_ref_expected_old_oid_cas_updates_only_the_expected_state`, `test_runner_write_ahead_intent_missing_or_mismatched_marker_never_relaunches` |

## 3. 검증 결과

- 계약 지정 aggregate 명령:
  `env -u FORCE_COLOR -u CLICOLOR_FORCE uv run scripts/tests/run_tests.py > /tmp/suite-m1b-effects.log 2>&1; echo "suite rc=$?"`
- 결과: `suite rc=0`
- 로그: `/tmp/suite-m1b-effects.log`
- 로그 종결: `Ran 929 tests in 90.218s` / `OK`
- 추가 확인: `scripts/tests/test_run_effects.py` 23/23, `test_run_store.py` 26/26, `test_run_lease.py` 12/12 green; `git diff --check` green
- 두 독립 구현 검토의 최종 판정: blocker 없음

## 4. 계약 해석 및 needs-ruling 후보

1. `planned`는 lease가 생기기 전 단계이므로 immutable plan 생성 자체는 단일 store transaction으로 원자화했고, `claimed` 이후 모든 상태 전이는 lease guard와 같은 store transaction 안에서만 허용했다. “모든 상태 전이 = lease guard”를 최초 `created → planned`에도 문자 그대로 적용하려면 lease lifecycle 선행 변경이 필요하다.
2. store v1의 `TransitionReason`은 폐쇄 enum이므로 generic `effect` 단계에는 기존 `PROCESS_STARTED`를 사용했다. `unknown-effect`와 `exited-unreconciled`는 외부 사실에서 도출되는 `EffectResult` 분류이며 새 persisted v1 state/reason으로 만들지 않았다.
3. `observed_digest`는 observed/completed transition에 결속되는 외부 authority 결과 digest다. observation receipt artifact의 digest는 그 사실·principal·action binding을 담은 evidence envelope 자체의 content digest로 별도 유지했다.
4. 이미 완료된 action에 대한 직접 `execute_effect` 재호출은 semantic duplicate typed refusal이고, 동일 action의 반복 `reconcile_actions`는 completed no-op이다.
5. runner WAI 이후 spawn 여부가 불명확하고 marker도 없으면 quiescence만으로 “실행되지 않음”을 증명할 수 없으므로 영구적으로 `unknown-effect`에 머문다. supervisor가 이후 더 강한 process identity/absence 증거를 제공해야 한다.
6. 실제 process spawn은 후속 supervisor 소유다. engine은 callback 직전까지 lease를 재검증하고 owner/fence/action/launch token을 넘기지만, 마지막 DB guard와 callback 내부 spawn 사이의 짧은 race는 callback/supervisor가 전달받은 principal을 다시 검증해야 완전히 닫힌다.
7. SQLite transaction과 filesystem artifact publication은 원자적으로 묶을 수 없다. intent/receipt publication 직전에 exact principal을 재검증하고, publication 뒤 guarded transition에서 다시 CAS한다. 그 사이 reclaim이 이기면 content-addressed orphan bytes만 남고 DB state/effect로 채택되지 않는다. “mutation 0”이 DB/authority mutation뿐 아니라 unreferenced evidence bytes 0까지 뜻한다면 store/lease protocol의 추가 ruling이 필요하다.
8. runner `invocation_digest`를 같은 run/job 안의 “같은 의미 입력”으로 해석했다. 중복 plain plan은 explicit retry lineage 없이는 거부하고, prior action이 불확실/nonterminal이면 새 action도 거부하며, positive terminal 뒤에만 새 attempt/action을 허용한다. action ID만 바꾸면 즉시 허용하는 해석은 ADR의 uncertain same-input 금지와 충돌한다.
9. worktree는 path와 전용 ref가 모두 absent면 `git worktree add -b`로 한 effect에서 만든다. WAI 전부터 정확한 expected ref만 존재하는 경우는 prerequisite로 허용하지만, WAI 뒤 ref-only/path-only partial은 conflict다. “전용 ref도 반드시 action이 최초 생성”이어야 한다면 이 prerequisite 허용 여부를 별도 확정해야 한다.
10. Git ref/patch target은 D5의 exact `for-each-ref` 관측을 유지하기 위해 full `refs/*`만 받는다. pseudo-ref나 shorthand를 지원하려면 adapter authority 계약 확장이 선행되어야 한다.
11. patch integration은 이미 만들어진 integration commit의 parent/tree를 재검증한 뒤 expected-parent ref CAS로 게시하는 모델로 해석했다. desired commit이 이미 ref에 있어도 expected parent의 tree precondition을 다시 도출한다.
12. ref처럼 payload에 임의 metadata를 삽입할 수 없는 effect는 immutable plan/WAI/observation receipt의 engine-owned metadata로 run/job/action을 일의 결속한다. content-addressed artifact bytes 자체는 다른 action과 공유될 수 있고 reference/receipt가 귀속을 고정한다.
13. runner의 nonzero return code도 “runner process effect가 완료됨”을 뜻한다. 그 결과를 job 성공/실패로 판정하는 일은 후속 verification task 소유다.
14. resume은 DB의 current lease tuple을 재구성해 current owner로 reconcile한다. lease API에 desired-effect 전용 takeover primitive가 없으므로, 효과 전 reclaim은 positive quiescence/absence를 확인한 뒤 새 epoch를 얻는 기존 lease 조합을 사용한다.
15. package-internal guarded store method는 active transaction과 exact owner/fence/entity version을 검증한다. `lease.py`를 수정하지 않는 제약 안에서는 그 transaction이 오직 특정 LeaseManager call stack에서 시작됐다는 opaque provenance token까지 증명하지는 못한다.
16. public actions transport와 external submit은 후속 task다. 이번 통합 stale-principal 테스트의 submit은 effect engine 내부 callback 경계를 의미한다.
17. positive quiescence는 callback으로 주입했다. 실제 supervisor/cancel subsystem의 process identity 기반 quiescence 구현은 후속 task 소유다.
18. 기존 store version 규약을 따라 transaction 내부에만 보이는 `created` v0 뒤 `planned` v1을 기록한다. 외부에 half-planned action이 노출되지는 않는다.
19. runner stdout/stderr와 artifact-write final bytes는 observation receipt가 digest로 참조한다. 향후 GC가 receipt graph를 재귀 mark하지 않는 설계라면 이 final artifact들에 직접 DB reference를 추가하는 ruling이 필요하다.
20. runner lineage는 engine의 사전 integrity 검사와 store transaction 내부 atomic correctness 검사 양쪽에서 검증한다. 현재는 후자가 race 안전성의 authority이고 전자는 typed diagnostics를 조기에 제공하지만, 장기 drift 방지를 위해 역할을 API 문서로 더 강하게 고정할 수 있다.
21. worktree porcelain parser는 모든 record에 HEAD를 요구한다. 현재 계약의 non-bare temporary repository에는 맞지만, source가 bare repository인 실행까지 지원해야 한다면 `bare` record의 별도 authority 규칙이 필요하다.
22. schema v2를 추가하지 않고 기존 v1 append-only artifact reference에 runner lineage reservation을 저장했다. 별도 query/index나 GC 정책이 필요해지면 migration registry를 통한 v-next schema가 후속으로 필요할 수 있다.

## 5. 스코프 밖에서 발견·유보한 항목

- `push`, `github-marker` kind는 ADR 표에는 있으나 이번 task에서 명시적으로 제외되어 구현하지 않았다.
- detached supervisor/process 생성, real backend, cancellation/cleanup, public actions transport, job verification/decision은 후속 task 소유라 구현하지 않았다.
- store permission hardening 및 ADR-0013 project/executor full-principal binding은 별도 task 소유라 수정하지 않았다.
- bare Git repository의 worktree authority 규칙은 현재 문서에 없어서 지원을 추측하지 않았다.
- aggregate suite가 ignored `.waystone/.gitignore`와 `.waystone/lock`을 생성/갱신했다. 지시대로 복원·삭제하지 않았다.
- 같은 aggregate 실행 중 ignored `.waystone/resume.md`의 timestamp도 갱신된 사실을 확인했다. 이는 명시된 lock/gitignore diagnostic 예외보다 넓은 legacy side effect이며 수정·복원하지 않았다. `.waystone/profile.yml`은 실행 전부터 존재한 ignored 파일이다.
