# m1b-verify 구현 보고

- 브랜치: `m1b/verify`
- base: `943147c feat(m1b): detached runner supervisor + process identity (feat/run-supervisor-identity)`
- 구현 커밋: `39504a4 feat(m1b): verify and apply run decisions`

## 1. 구현 요약과 파일

- `waystone/runs/verify.py`
  - `adapters.git`에서 direct-child base/result commit, tree OID, changed-file raw bytes, binary patch bytes를 다시 도출하고 canonical result digest를 만든다.
  - frozen `VerificationPlan`/preflight의 engine action을 exact set으로 실행한 뒤, worker와 분리된 verifier actor를 read-only materialized result root에서 실행한다.
  - runner effect plan, completion observation receipt, stdout/stderr artifact와 canonical verifier transcript를 semantic engine/verifier evidence에 결속한다. invalid/empty/nonzero/timeout/mutation에서는 `verifier-evidence:*`를 발행하지 않는다.
  - owner criterion exact set, result/verifier/engine evidence digest, coordinator actor, blocker override 근거를 typed refusal로 검증하고 append-only integration decision을 기록한다.
  - apply 때 RunSpec/plan/preflight/runner receipt/verifier/decision/Git triple을 다시 읽고 rehash한 뒤 `PatchIntegrationEffect` CAS로 private integration ref를 이동한다. live worktree/index/user dirt는 읽기·fingerprint만 하며 stash, commit, 3-way apply를 하지 않는다.
  - retry는 새 attempt와 새 action만 허용하며, 성공 verifier/decision lineage는 실패 조상을 다시 지목해도 terminal refusal한다. verifier lineage는 project lock으로 semantic evidence 발행까지 직렬화한다.
- `scripts/tests/test_run_verify.py`
  - PC-16/17/20/21/22, ADR-0008/0012, retry lineage를 실제 임시 Git repo/worktree와 RunStore fixture로 검증하는 25개 테스트를 추가했다.
- `scripts/tests/run_tests.py`
  - 기존 aggregate 항목을 변경하지 않고 `RunVerifyTests` import와 등록만 추가했다.

## 2. 계약 매핑

| 계약 / ADR / fixture 행 | 단언 테스트 함수 |
|---|---|
| PC-16 — Git 도출 triple, binary/non-UTF-8 patch byte 보존 | `test_pc16_git_triple_and_verifier_artifact_preserve_exact_binary_bytes` |
| PC-16 — verdict 이후 result ref/patch 교체 거부 | `test_pc16_post_decision_result_ref_tamper_is_refused` |
| PC-17 — dirty/staged/untracked/ignored user bytes와 index 불변 | `test_pc17_apply_preserves_dirty_staged_untracked_and_ignored_user_bytes` |
| PC-17 — pre-existing integration drift의 atomic no-write | `test_pc17_preexisting_integration_drift_is_atomic_no_write` |
| PC-17 — public/registered-worktree target refusal | `test_pc17_apply_refuses_public_or_linked_worktree_target_without_write` |
| PC-17 — private symref가 checked-out user branch로 탈출하지 못함 | `test_pc17_apply_refuses_symbolic_private_ref_escape` |
| PC-17/PC-22 — injected linked-worktree race 재검사 | `test_pc17_pc22_linked_worktree_race_is_rechecked_before_cas` |
| PC-17/PC-22 — CAS 이후 unrelated user edit 보존 및 성공 오분류 방지 | `test_pc17_pc22_post_cas_user_edit_does_not_reclassify_success` |
| PC-20 / ADR-0008 — worker·verifier·coordinator actor 분리와 review worktree 불변 | `test_pc20_verifier_is_read_only_and_separate_from_worker_and_integrator` |
| PC-20 — empty/malformed/nested-invalid/nonzero/timeout 무발행 | `test_pc20_empty_invalid_and_failed_output_publish_no_semantic_evidence` |
| PC-20 — read-only review root mutation typed refusal 및 무발행 | `test_pc20_mutating_verifier_is_refused_without_evidence` |
| PC-20 — unordered valid output의 receipt 전 canonicalization과 decision 소비 가능성 | `test_pc20_valid_output_is_canonical_before_runner_receipt` |
| PC-20/PC-22 — runner plan, observation receipt, stdout/stderr transcript, decision intent producer 결속 | `test_pc20_pc22_producer_plans_bind_exact_verification_and_decision_inputs` |
| ADR-0012 authoritative deterministic check — frozen engine action exact 실행과 result evidence 결속 | `test_adr0012_pc20_engine_actions_run_exactly_and_bind_result_evidence` |
| ADR-0012 / PC-20 — frozen verifier backend/sandbox substitution refusal | `test_adr0012_pc20_frozen_verifier_adapter_substitution_is_refused` |
| PC-21 — missing/extra criterion, wrong digest, worker self-acceptance, unsupported override의 구분된 typed refusal | `test_pc21_decision_refusals_distinguish_every_binding_violation` |
| PC-21 — blocker override가 passing engine check와 stored evidence에 exact mapping | `test_pc21_blocker_override_requires_grounded_engine_check_mapping` |
| PC-21 — unrelated binary ArtifactWrite가 decision lineage를 오염하지 않음 | `test_pc21_decision_lineage_skips_unrelated_binary_artifact_write` |
| PC-21 — internally inconsistent intent의 lineage forgery 거부 | `test_pc21_inconsistent_decision_intent_cannot_forge_retry_lineage` |
| PC-21 — concurrent coordinator decision 한 개만 발행 | `test_pc21_concurrent_decisions_serialize_one_lineage` |
| PC-22 — execution-time 모든 authority artifact 재로드/rehash 및 tamper no-write | `test_pc22_apply_reloads_contract_decision_and_verifier_artifact_digests` |
| PC-22 — apply attempt를 accepted decision attempt에 결속 | `test_pc22_apply_refuses_attempt_outside_decision_lineage` |
| PC-22 — target CAS race가 concurrent winner를 덮지 않음 | `test_pc22_cas_race_is_refused_without_overwriting_concurrent_result` |
| Retry lineage — concurrent verifier retry의 terminal evidence 단일 발행 | `test_pc20_concurrent_retries_publish_one_terminal_evidence` |
| Retry lineage — 실패 후 새 attempt/action만 허용, 같은 ID 및 성공/실패-조상 재사용 거부 | `test_pc20_pc21_retry_requires_new_attempt_and_new_action_identity` |
| Binary Git fixture | `test_pc16_git_triple_and_verifier_artifact_preserve_exact_binary_bytes` |
| Dirty worktree fixture | `test_pc17_apply_preserves_dirty_staged_untracked_and_ignored_user_bytes` |
| Malformed/timeout/mutation verifier fixture | `test_pc20_empty_invalid_and_failed_output_publish_no_semantic_evidence`, `test_pc20_mutating_verifier_is_refused_without_evidence` |
| Integration drift/CAS/symref fixture | `test_pc17_preexisting_integration_drift_is_atomic_no_write`, `test_pc22_cas_race_is_refused_without_overwriting_concurrent_result`, `test_pc17_apply_refuses_symbolic_private_ref_escape` |

## 3. 검증 결과

- 직접 계약 모듈: `Ran 25 tests in 28.355s`, `OK` (`/tmp/test-run-verify.log`)
- 결합 집중 스위트 (`RunVerifyTests`, effects, preflight, artifacts/store, spec): `Ran 117 tests in 40.283s`, `OK` (`/tmp/focused-m1b-verify.log`)
- 필수 aggregate 명령:

  `env -u FORCE_COLOR -u CLICOLOR_FORCE uv run scripts/tests/run_tests.py > /tmp/suite-m1b-verify.log 2>&1; echo "suite rc=$?"`

- 결과: `suite rc=0`; `Ran 1013 tests in 128.691s`; `OK`
- 로그: `/tmp/suite-m1b-verify.log`

Aggregate 실행은 허용된 legacy diagnostic으로 ignored `.waystone/lock`을 `2026-07-21 07:45:40`에 갱신했다. ignored `.waystone/.gitignore`도 존재하며, host harness가 만든 ignored `.waystone/resume.md`가 존재한다. 이를 복원·삭제하지 않았다. `.waystone/profile.yml`은 이 작업 전부터 있던 fixture/host 상태이며 수정하지 않았다.

## 4. 계약 해석 및 needs-ruling 후보

1. **PC-22 durable reconcile 경계**: synchronous `apply_integration_decision`은 patch effect를 plan하기 직전까지 contract, decision, verifier/runner evidence와 Git triple을 두 번 재검증한다. 그러나 기존 `PatchIntegrationEffect` durable spec은 repository/ref/base/result OID·tree만 저장하고 approval artifact digest는 저장하지 않는다. 따라서 plan 저장 뒤 crash가 나고 generic `EffectEngine.reconcile`가 직접 호출되면 `verify.py`의 authority reload를 다시 거치지 않는다. 이 기체는 기존 `effects.py` 수정 금지라 해당 schema/reconcile hook을 확장할 수 없었다. PC-22가 crash-reconcile에도 approval bundle 재검증을 요구한다면 후속 ruling과 effects 계약 변경이 필요하다.
2. **마지막 checkout 검사와 CAS 사이 TOCTOU**: target을 canonical private direct ref로 제한하고 모든 registered worktree를 effect plan 직전에 재검사하며, symref escape도 거부한다. 그래도 외부 Git 프로세스가 마지막 검사 직후 `PatchIntegrationEffect`의 `update-ref` 직전에 target을 worktree HEAD symref로 붙이는 미세 창은 두 Git authority를 하나의 CAS로 묶을 기존 primitive가 없어 남는다. 이 창까지 PC-17/22 concurrent drift로 보는지 ruling이 필요하다.
3. **private target 정책**: unrelated user branch/worktree를 움직이지 않는 보수적 선택으로 apply target을 `refs/waystone/integration/*` direct ref로 제한했다. 계약 문언이 arbitrary target ref apply를 의도했다면 별도 안전 모델이 필요하다.
4. **Git adapter 명령 표면**: 모든 Git 사실은 `waystone.adapters.git`을 경유한다. 다만 `git_read_bytes` allow-list에는 `worktree`와 `symbolic-ref`가 없어 registered-worktree/direct-ref 사실은 같은 adapter의 fail-closed `git_rc`를 사용했다. strict하게 모든 사실에 `git_read_bytes`를 요구한다면 adapter 변경이 필요하다.
5. **verifier fixture 실행 경계**: supervisor 결합은 후속이라는 브리핑에 따라 executor는 in-process fixture다. 검토 tree는 별도 materialization, read-only permission, before/after fingerprint로 보호하지만 OS-level process sandbox는 아니다. 또한 exact result ref가 한 registered worker worktree에서 해당 commit으로 checkout되어 있어야 materialize한다.
6. **invalid verifier output 시 engine evidence**: deterministic engine checks가 완료된 경우 engine-check evidence는 남을 수 있지만 semantic `verifier-evidence:*`는 invalid/empty/failed/timeout에서 발행하지 않는다. PC-20의 “verifier artifact 무발행”을 semantic verifier artifact로 해석했다.
7. **result shape**: patch-integration effect의 base+result 모델에 맞춰 result commit은 frozen base의 direct child여야 한다. merge/multi-commit result를 조용히 축약하지 않고 typed refusal한다.
8. **apply attempt**: 별도 integration-attempt 모델이 없으므로 apply action은 accepted decision을 기록한 attempt에 귀속해야 한다고 보수적으로 해석했다.
9. **criterion evidence digest**: verifier criterion의 `evidence_digests`는 canonical digest로 검증되고 verifier transcript/artifact에 결속되지만, 각 digest가 ArtifactStore의 별도 blob을 반드시 가리켜야 한다는 문언은 PC-20/21에 명시되지 않아 availability를 강제하지 않았다. 별도 evidence-attachment 계약을 뜻했다면 ruling이 필요하다.
10. **verifier project lock**: 성공 발행 직전의 retry race를 닫기 위해 fixture verifier 실행부터 semantic evidence 발행까지 project lock을 유지한다. 후속 supervisor가 장기 프로세스를 붙일 때는 terminal reservation/publication을 store transaction으로 승격할지 결정해야 한다.

## 5. 스코프 밖에서 발견한 문제

- `PatchIntegrationEffect`에 approval bundle digest와 reconcile-time authority callback/schema가 없다. 이번 기체의 기존 모듈 수정 금지 때문에 수정하지 않았다.
- Git ref CAS와 registered-worktree HEAD attachment를 원자적으로 묶는 effect primitive가 없다. 이번 기체에서 `effects.py`는 수정하지 않았다.
- `git_read_bytes`가 `worktree list --porcelain -z`와 `symbolic-ref --quiet` read probe를 지원하지 않는다. adapter 확장은 스코프 밖이라 수정하지 않았다.
- production verifier supervisor/process sandbox 결합과 result object를 checkout 없이 materialize하는 Git object reader는 후속 범위다.
- legacy `waystone/runs/delegate.py`의 기존 Git helper/legacy path는 D1에 따라 수정하지 않았다.
- criterion별 별도 evidence artifact availability가 계약으로 승격되면 verifier output/attachment schema가 추가로 필요하다.

