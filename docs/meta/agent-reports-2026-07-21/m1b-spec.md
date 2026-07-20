# m1b-spec 구현 보고 — `feat/run-spec-planning`

## ① 구현 요약과 파일 목록

단일 task id에서 owner 입력과 live Git 상태를 읽어 immutable RunSpec을 발행하는 planning
경계를 구현했다. 진입점은 initialized-root와 task readiness를 store open보다 먼저 검사하고,
snapshot 전후 task digest가 같을 때만 store의 UUIDv7 run 한 건과 planned job 한 건을 만든다.
RunSpec과 base snapshot은 canonical JSON bytes의 SHA-256 content-addressed artifact로 기록하며,
기존 store의 artifact reference와 `PLANNED` transition으로 run에 결속한다. frozen input을 다시
쓰는 API는 두지 않았고, 현재 registry와의 차이는 typed drift 값/예외로만 노출한다.

Snapshot은 HEAD의 전체 path set, index tracked path, non-ignored untracked path의 합집합에서
최종 worktree bytes를 읽는다. regular file, executable bit, symlink, deletion, binary/non-UTF-8
bytes를 보존하고 ignored untracked는 제외한다. Git fact 조회는 `waystone.adapters.git`의 신규
byte-preserving read-only probe만 사용하며 `GIT_OPTIONAL_LOCKS=0`을 강제한다. HEAD, porcelain
status, raw index bytes, 두 번의 content capture가 모두 같아야 snapshot을 발행한다. unmerged
index, snapshot 중 변화, 읽을 수 없는/special path, absent skip-worktree path는 typed refusal한다.

변경 파일:

- `waystone/runs/spec.py` — frozen domain 값, planning, canonical artifact read/validation,
  snapshot capture, task drift detection.
- `waystone/adapters/git.py` — allowlist 기반 raw-byte read-only Git probe와 `GitReadError`.
- `scripts/tests/test_run_spec.py` — 11개 신규 계약 테스트.
- `scripts/tests/run_tests.py` — `RunSpecTests` import/aggregate 등록만 추가.

Store schema와 `waystone/jobs/`는 수정하지 않았고 신규 dependency도 추가하지 않았다.

커밋:

- `fab80cd` — `feat(m1b): add frozen one-task run planning`
- `f62045f` — `fix(m1b): reject incomplete review exemptions`

## ② 계약 매핑 표

| 계약 / fixture | 단언하는 테스트 함수 |
|---|---|
| ADR-0010 frozen canonical RunSpec, input/spec digest, store UUIDv7, one task = one run/job, artifact binding | `test_plan_freezes_owner_input_and_persists_one_run_one_job` |
| ADR-0010 acceptance 없는 dispatch 거부, store/root state 선생성 금지 | `test_missing_acceptance_refuses_before_run_or_project_state_creation` |
| ADR-0010 유한 retry/time/cost ceiling과 `stop` 고정 | `test_plan_freezes_owner_input_and_persists_one_run_one_job` |
| ADR-0010 risk-gated review 판정 입력 동결 | `test_plan_freezes_owner_input_and_persists_one_run_one_job` |
| ADR-0010 필수 authorization 필드 없는 explicit exemption fail-closed | `test_incomplete_explicit_review_exemption_is_rejected` |
| PC-15 owner title/acceptance/scope/deps freeze, registry drift typed 표시, stored bytes/state 불변, worker override surface 부재 | `test_registry_drift_is_typed_and_cannot_rewrite_frozen_input` |
| PC-15 planning 중 owner input 변경 시 run 미생성 | `test_task_change_during_planning_refuses_before_run_creation` |
| PC-17 HEAD + staged + unstaged tracked + non-ignored untracked, binary/deletion/executable 보존, ignored 제외, status/index/HEAD 불변 | `test_snapshot_includes_dirty_staged_and_untracked_without_mutating_index` |
| PC-17 snapshot 중 content/status/index 변화 시 typed no-write refusal | `test_snapshot_refuses_concurrent_tree_change_before_creating_state` |
| PC-17 assume-unchanged가 숨긴 tracked dirt도 실제 bytes로 포착 | `test_snapshot_does_not_trust_assume_unchanged_index_hint` |
| PC-17 absent skip-worktree path를 clean/default로 강등하지 않음 | `test_absent_skip_worktree_path_refuses_even_with_assume_unchanged` |
| PC-31 미초기화 root 조기 typed refusal, store gate 미진입, `.waystone/` 무생성 | `test_uninitialized_root_refuses_before_store_and_creates_nothing` |
| malformed registry가 traceback/default task로 강등되지 않고 typed refusal | `test_malformed_task_registry_is_a_typed_refusal` |

## ③ 검증 결과

- Focused: `uv run scripts/tests/test_run_spec.py` → **11 tests, rc=0**.
- Compile: `uv run python -m compileall -q waystone/runs/spec.py waystone/adapters/git.py scripts/tests/test_run_spec.py` → **rc=0**.
- Diff hygiene: `git diff --check` → **rc=0**.
- Full suite 명령:

  ```sh
  env -u FORCE_COLOR -u CLICOLOR_FORCE uv run scripts/tests/run_tests.py > /tmp/suite-m1b-spec.log 2>&1; echo "suite rc=$?"
  ```

  결과: **suite rc=0**, `Ran 881 tests in 93.686s`, `OK`.
  로그: `/tmp/suite-m1b-spec.log`.

## ④ 계약 해석 및 needs-ruling 후보

1. **ADR-0010 acceptance envelope와 legacy registry가 충돌한다.** ADR은 구조 없는 단일 문자열을
   dispatch-ready로 인정하지 않고 `origin`, `claim`, immutable `source_pointer`, closed
   `subject_scope`, registered evidence adapter, `negative_case`, `method_constraint`와 deterministic
   validator/독립 critic을 요구한다. 반면 이 task briefing은 legacy `load_tasks` 재사용과
   `accept: [string]` freeze를 요구하며, 현행 task schema에는 위 envelope/adapter registry가 없다.
   이 구현은 slice의 구체 요구를 우선해 legacy 문자열을 owner-authored 원문으로 보존하고
   empty/blank/duplicate만 거부하며 `critic-not-required`로 기록한다. 따라서 ADR의 전체 semantic
   readiness validator/critic을 구현했다고 주장할 수 없다. 대안은 모든 현행 free-text task를
   `criterion-*` refusal하는 것이지만 task-id-only planning을 사실상 전부 막는다. envelope의
   authority/schema와 legacy string 승격 규칙을 main에서 판정해야 한다.

2. **Review decision의 mandatory 여부가 충돌한다.** ADR은 모든 frozen spec이
   `none|required` 결정을 반드시 가져야 한다고 쓰지만 briefing은 "판정 입력이 있으면" spec에
   기록하라고 한정한다. project policy/compiler 입력 schema도 이 slice에 없다. 구현은 명시적
   `ReviewDecision`이 들어오면 requirement/reason/rule id/policy digest를 동결하고, 없으면 `null`로
   보존한다. builtin required reason과 `no-review-trigger`만 허용하고 project-defined reason은
   fail-closed한다. `explicit-review-exemption`은 authorizing source pointer, bounded scope,
   rationale를 표현할 schema가 없으므로 불완전 입력을 거부한다. 후속 policy compiler가 decision을
   항상 공급할지, spec planner가 입력 부재 자체를 거부할지 판정이 필요하다.

3. **ADR-0010에는 숫자 retry 기본값이 없다.** 문서는 profile/project policy가 값을 제공할 수
   있다고만 하고 concrete default를 정하지 않는다. briefing의 "ADR-0010 문언의 기본값"을 그대로
   찾을 수 없었다. task-id-only API를 유지하기 위해 가장 작은 no-retry 정책
   (`max_attempts_per_job=1`, `max_total_attempts=1`, retryable class 없음, stop)과 positive bounded
   meter (`1 day`, `1 attempt`, `attempt-start`)를 고정했다. 이 숫자/closed unit/meter는 ADR에서
   확정된 값이 아니므로 main이 policy source와 기본값을 판정해야 한다. 대안은 resolved policy가
   없을 때 planning 자체를 typed refusal하는 것이다.

4. **Store v1에 RunSpec/snapshot/drift 전용 kind가 없다.** 기존 store/domain 수정 금지와 migration
   최소화를 택해 RunSpec과 snapshot을 `EVIDENCE` artifact reference 두 건으로 결속했고 schema
   migration은 추가하지 않았다. Drift는 `RunInputDrift`/`RunInputDriftError`로 run id에 귀속되지만
   store transition reason/state로 persist하지 않는다. store enum을 확장할지, read-time typed
   projection이 PC-15의 "표시"를 충족하는지 판정이 필요하다.

5. **Canonical representation이 문서에서 byte-level로 고정되지 않았다.** 구현은 sorted-key,
   compact UTF-8 JSON과 `sha256:<hex>`를 선택했다. job id도 store가 job UUID 생성기를 제공하지 않아
   project-global 충돌을 피하도록 `<run-UUIDv7>:job`으로 결정했다. 두 형식 모두 후속 consumer가
   의존하기 전에 main에서 고정해야 한다.

6. **Snapshot artifact 표현이 고정되지 않았다.** live index/worktree와 Git object DB를 쓰지 않기
   위해 temporary Git tree/commit이나 patch가 아니라 HEAD-rooted full final-tree bytes를 저장했다.
   path/content는 base64, deletion은 tombstone, mode는 Git mode로 canonicalize한다. 큰 repository의
   artifact 크기, submodule/special file 처리, sparse checkout의 absent skip-worktree materialization은
   아직 정책이 없다. 현재는 불확실한 항목을 typed refusal한다.

7. **거부 revision audit의 owner가 불명확하다.** root/task/snapshot readiness 거부는 store open
   전에 no-write하므로 ADR의 "거부 revision 보존" DB audit이 없다. 반대로 run 생성 뒤 artifact/
   transition 실패는 candidate run이 남는다. PC-31/no-state와 rejected-revision audit의 우선순위 및
   audit 저장소를 판정해야 한다.

8. **4-role binding은 이 task의 명시 산출 필드가 아니다.** 필독 domain/profile adapter를 판독했지만
   briefing의 frozen input 목록과 RunSpec 필드에는 binding을 넣으라는 계약 및 profile 경로가 없다.
   추측성 선반영을 피하려고 role binding은 포함하지 않았다. 후속 preflight/transport가 binding을
   소비하기 전에 RunSpec 소유인지 별도 VerificationPlan 소유인지 판정이 필요하다.

9. **필수 full suite가 root `.waystone/lock`을 실제로 덮어썼다.** suite 전 root lock은 size 219,
   mtime `1784569736`, SHA-256 prefix `f8a6063a...`였으나, 최초 suite가 persistent lock diagnostic을
   rewrite했다. 원본 bytes/full digest를 별도 백업하지 않아 정확 복구할 수 없었고, 임의 추측으로
   다시 쓰지 않았다. 최종 suite 뒤 현재 값은 size 90, mtime `1784572008`, SHA-256
   `ab1604994e5e2fe74106f977c7d7687c205d4df07e1935ed7c2dd4f248275654`, 내용은
   `{"pid": 68773, "host": "unknown", "verb": "run_tests", "at": "2026-07-21T03:26:48+09:00"}`다.
   `.waystone/.gitignore`와 `.waystone/profile.yml`은 사전/사후 size·mtime·digest가 같았다. 이는
   구현 코드나 신규 테스트 fixture가 아니라 mandatory legacy aggregate가 persistent project lock을
   획득하면서 발생했지만, "이 worktree의 `.waystone/` 무수정" 제약 위반이라는 사실은 동일하다.

## ⑤ 스코프 밖에서 발견한 문제

- Aggregate suite가 현재 project root의 persistent `.waystone/lock` diagnostics를 rewrite한다.
  검증 명령 자체와 root-state 무수정 계약이 양립하지 않는다. legacy runner/lock 수정은 이 task의
  D1 범위 밖이므로 수정하지 않았다.
- 실제 `tasks.yaml`의 `feat/run-spec-planning` 행에는 `accept`와 `scope`가 없다. 새 planner에 그
  task id를 직접 넣으면 명시 계약대로 `criterion-empty` refusal한다. registry를 보완하는 일은
  owner-authored intent mutation이므로 이 worker가 수정하지 않았다.
- `ruff` executable은 현재 uv 환경에 없어 lint 실행을 할 수 없었다. D8에 따라 dependency를
  추가 설치하지 않았고 compileall, diff check, focused/full unittest로 검증했다.

