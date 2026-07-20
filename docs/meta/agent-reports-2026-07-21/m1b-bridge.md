# m1b-bridge 구현 보고 — `feat/run-cli-bridge`

- base: `7cfe763 feat(m1b): verifier·decision·apply 수직 완성 (feat/run-verify-decision)`
- 구현 commit: `ebfb4d1 feat(m1b): add run CLI engine bridge`
- push: 하지 않음

## ① 구현 요약과 파일 목록

`RunEngine` 조립 facade를 추가했다. 단일 task의 frozen `RunSpec`과
`VerificationPlan`을 만든 뒤 기존 preflight가 낸 `EngineCheckAction`의
`prepared_input_digest`와 exact `RunnerInvocation`을 결속하고, 기존
`EffectEngine`/`Supervisor`를 통해 detached runner를 비차단 기동한다. resume은 runner
effect reconcile과 cancel resume을 조합하고, 정상 runner 종료 뒤 기존 verifier,
coordinator decision, private integration ref apply API를 차례로 호출해 run을 완료한다.
조립 계층이 retry나 상태 전이 판단을 새로 만들지 않도록 각 소유 모듈의 결과를 그대로
배선했다.

status/watch는 `RunStore.open`을 호출하지 않는다. 기존 DB를 SQLite `mode=ro`로 열어
일관된 temporary backup snapshot을 만든 뒤 observe renderer를 실행하므로 project DB,
WAL/SHM, `.gitignore`를 쓰지 않는다.

`waystone run` group에 `start`, `resume`, `status`, `watch`, `cancel`, carrier용
`actions next/submit`을 추가했다. `deliver`는 M2 소유라는 typed refusal만 반환한다.
미초기화 root gate는 argument별 파일 읽기나 engine 생성보다 먼저 실행한다. human apply
문구는 private `refs/waystone/integration/*` 완료만 말하며 live-tree delivery를 주장하지
않는다. 기존 `delegate` dispatcher와 구현은 수정하지 않았다.

변경 파일:

- `waystone/runs/engine.py` — one-task engine facade, exact dispatch binding,
  detached supervisor 조립, resume/cancel/verify/decision/private-apply, read-only store opener.
- `waystone/cli/run_group.py` — run 사용자/transport CLI와 typed envelope boundary.
- `waystone/cli/main.py` — `run` dispatcher 등록과 legacy project-state precheck 우회 등록.
- `scripts/tests/test_run_cli.py` — 8개 CLI/engine 계약 테스트.
- `scripts/tests/run_tests.py` — `RunCliTests` aggregate 등록만 추가.

## ② 계약 매핑 표

| 할당 계약 / ADR / fixture 행 | 이를 직접 단언하는 테스트 함수 |
|---|---|
| M1-B §6 exit 1, D2 opt-in: start→Supervisor→marker→verify→decision→private apply→completed | `test_one_task_cli_run_completes_through_supervisor_verify_decision_and_private_apply` |
| M1-B §6 exit 3, ADR-0004: `actions next`가 engine-owned action을 노출하지 않음 | `test_one_task_cli_run_completes_through_supervisor_verify_decision_and_private_apply` |
| transport 인계 ④1, ADR-0003 D6: planned runner detached 비차단 dispatch 후 busy | `test_planned_runner_dispatch_returns_busy_without_waiting_for_completion` |
| transport 인계 ④3·④6, ADR-0012: preflight digest와 exact invocation 결속, mismatch는 시작 전 refusal | `test_runner_invocation_must_match_frozen_preflight_digest`, `test_one_task_cli_run_completes_through_supervisor_verify_decision_and_private_apply` |
| observe 인계 ④2, §3-8: status/watch CLI가 e2e 중 authoritative DB row를 바꾸지 않음 | `test_status_and_watch_cli_use_read_only_open_during_e2e` |
| verify 인계 ④3, PC-16·17·21·22: apply는 private integration ref이며 live tree를 delivery하지 않음 | `test_one_task_cli_run_completes_through_supervisor_verify_decision_and_private_apply` |
| PC-31 run 표면: 미초기화 root의 모든 하위 명령 typed refusal + `.waystone/` 무생성 | `test_uninitialized_root_refuses_every_run_subcommand_without_creating_state` |
| PC-27 / ADR-0012: 미지원 binding/backend preflight refusal의 CLI envelope 전파 | `test_unsupported_backend_preflight_refusal_reaches_typed_cli_envelope` |
| ADR-0003 fixture 4 CLI 판: cancel intent 기록, unknown-effect pending, resource 보존 | `test_cancel_cli_records_intent_and_exposes_unknown_effect_pending` |
| §3-3 front door: `run` group 등록, legacy state check 경로 미사용 | `test_main_dispatcher_registers_run_without_legacy_project_state_check` |

## ③ 검증 결과

- Focused: `uv run scripts/tests/run_tests.py RunCliTests` → **8 tests, rc=0**.
- Adjacent regression: `RunCliTests RunObserveTests RunCancelTests` → **39 tests, rc=0**.
- 조립 전체 인접 묶음: spec/preflight/effects/supervisor/transport/observe/cancel/verify/cli
  테스트 전부 green.
- `uv run --with pyyaml python -m py_compile waystone/runs/engine.py waystone/cli/run_group.py scripts/tests/test_run_cli.py` → **rc=0**.
- `git diff --check` / staged `git diff --cached --check` → **rc=0**.
- 필수 aggregate:

  ```sh
  env -u FORCE_COLOR -u CLICOLOR_FORCE uv run scripts/tests/run_tests.py > /tmp/suite-m1b-bridge.log 2>&1; echo "suite rc=$?"
  ```

  최종 결과: **suite rc=0**, `Ran 1088 tests in 142.750s`, `OK`.
  로그: `/tmp/suite-m1b-bridge.log`.

aggregate의 알려진 legacy diagnostic이 ignored `.waystone/lock`을 size 91,
SHA-256 `ec9d76710c4063a203842f061fd773002ebeaadbd8fc3516ad630d88f3f505bb`에서
최종 size 89, SHA-256
`d6893e063385c50f53f72e47f7fb41e2b57468580674ced9590f314b150087b1`로
갱신했다. 계약대로 복원·삭제하지 않았다. `.waystone/profile.yml`과
`.waystone/.gitignore` digest는 변하지 않았다. host hook이 ignored
`.waystone/resume.md`를 생성한 사실을 기록했고 최종 상태에서 해당 파일만 제거했다.

## ④ 계약 해석 및 needs-ruling 후보

1. **production assembly owner가 이 base에 없다.** 공개 run 모듈은 frozen definition,
   capability/probe evidence, backend invocation, verifier adapter를 제공하지만 이를 실제 profile과
   backend에서 만드는 production compiler/factory는 없다. legacy delegate를 fallback으로 호출하면
   D2와 silent fallback 금지를 어긴다. 따라서 `RunEngine`은 explicit `RunAssembly`를 요구하고,
   없는 `run start`는 `run_engine_configuration_unavailable`을 transport의 typed refusal envelope로
   드러낸다. fixture backend는 이 seam으로 완주한다. 실제 backend adapter와 smoke는 D7의 gate
   소유로 남으며, default front door가 어느 factory를 받아야 하는지 ruling이 필요하다.
2. **run FSM과 preflight authority loader가 충돌한다.** `load_dispatch_ready()`는 run state가
   정확히 `dispatch-ready`일 때만 frozen dispatch authority를 돌려주지만, 실행 중으로 옮기면
   resume/actions next가 그 authority를 다시 읽을 수 없다. 보수적으로 runner 진행 중에도 run은
   `dispatch-ready`를 유지하고 action lifecycle이 실제 실행 상태를 소유하게 했다. cancel engine만
   `running`을 입력으로 요구하므로 cancel intent 직전에 `running`으로 전이한다. 별도
   dispatch-authority loader 또는 FSM ruling이 생기면 이 조합을 교체해야 한다.
3. **public read-only store opener가 없다.** `RunStore.open`은 migration, WAL 협상,
   `.gitignore` 생성 가능성이 있어 status/watch에 사용할 수 없다. `store.py` 수정 금지 조건 아래
   engine이 package-private schema validation과 construction token을 사용해 read-only SQLite backup
   store를 조립했다. 이 private 의존을 없애려면 store owner가 public read-only opener를 제공해야
   한다.
4. **`RunnerInvocation`은 argv/cwd만 표현한다.** preflight action의 non-empty
   `child_environment`를 exact하게 supervisor에 전달할 API가 없으므로 이를 무시하지 않고
   `run_engine_binding_refused`로 거부한다. environment-aware invocation 확장은 supervisor owner의
   ruling이 필요하다.
5. **cancel reason enum의 canonical vocabulary가 없다.** ADR/계획은 `<enum>`만 고정하고 값은
   고정하지 않았다. 임의 확장을 피하려고 owner-authored `user-requested` 하나만 닫힌 enum으로
   노출했다.
6. **detached launch 예약과 runtime identity 게시 사이에는 짧은 honest-unknown 구간이 있다.**
   `start` 자체는 launch 예약 직후 busy를 반환한다. 그 직후 별도 `actions next`가 runtime 파일
   게시 전에 들어오면 liveness를 alive로 추측하지 않고 typed `run_not_actionable`을 반환한다.
   계약 테스트는 runtime identity 게시 뒤 busy를 확인한다. 예약만으로 busy를 얼마 동안 허가할지
   정하려면 supervisor lease/telemetry 정책 ruling이 필요하다.
7. **late-stage crash용 public reload/reconcile API가 완결돼 있지 않다.** runner effects와 cancel은
   기존 reconcile/resume API로 복구한다. verifier/decision/apply는 같은 action id의 terminal
   evidence를 public하게 reload하는 API가 없고 plain 재호출은 append-only retry 규칙과 충돌할 수
   있다. 이 구현은 요구된 정상 수직 경로와 runner/cancel resume만 조립했으며, verify 이후 crash
   matrix를 silent replay하지 않았다.

## ⑤ 스코프 밖에서 발견한 문제

- 실제 Codex/Claude backend용 `VerificationPlanDefinition`·capability probe·exact
  `RunnerInvocation`·verifier adapter를 한 묶음으로 제공하는 production assembly가 아직 없다.
- store의 public read-only opener와 verification/decision/apply terminal evidence reload API가 없다.
- private integration ref에서 live tree로 전달하는 정책은 briefing대로 M2 소유이며 구현하지 않았다.
- `bin/waystone`은 기존대로 `scripts/waystone.py` front door에 위임하고 있어 별도 수정이 필요하지
  않았다.
- 그 밖의 unrelated source defect는 발견하지 못했고 수정하지 않았다.
