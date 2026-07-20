# m1b-cancel 구현 보고서

- Task: `feat/run-cancel-quiescence`
- Branch: `m1b/cancel`
- Base: `943147c feat(m1b): detached runner supervisor + process identity`
- Commit: `aa217763287b814dde8047527ad6cac43194cd37`

## 1. 구현 요약과 파일 목록

### 구현

- `request_cancel()`은 action에 결속된 canonical cancellation-intent artifact와
  `cancel-requested` transition을 먼저 기록한다. 이 경로는 terminal cancellation이나 cleanup을
  수행하지 않는다.
- signal은 current lease principal, immutable runner plan, supervisor launch/runtime의 모든 상위
  identity 필드, embedded process/supervisor identity, invocation digest, direct OS liveness와
  `Supervisor.probe_action()`이 모두 일치할 때만 전송한다.
- signal 전 실패로 남은 `stopping`은 새 engine의 `resume_cancel()`이 동일 identity를 재검증하고
  재시도할 수 있다. signal capability 자체가 없으면 pending으로 강등하지 않고 typed error를 낸다.
- terminal cancellation은 positive `EXITED`와 `EffectEngine.inspect/reconcile` 완료를 AND로 요구한다.
  current source principal guard 안에서 run CAS와 append-only terminal evidence를 함께 commit한다.
- cleanup은 원 runner telemetry를 완료 marker로 쓰지 않는다. intent digest에서 결정되는 별도 action을
  만들고 immutable cleanup-plan evidence, 자체 lease/principal, `cleanup-ready → cleanup-executing →
  cleanup-completed` transition으로 실행한다.
- destructive executor는 SQLite transaction 밖에서 action별 advisory lock을 잡은 채 실행한다. 실패는
  `cleanup-executing`으로 정직하게 남고 같은 action을 idempotently 재개한다. 두 번째 완료 호출은 새
  engine에서도 durable no-op이다.
- M1-B의 single-task/single-source-action 범위를 명시적으로 강제한다. terminal WAI와 cleanup 완료 CAS
  양쪽에서 run membership, source action, current principal을 다시 검사한다.

### 변경 파일

| 파일 | 변경 |
|---|---|
| `waystone/runs/cancel.py` | 취소 intent, verified signal, quiescence/reconcile terminal gate, 별도 cleanup action 구현 |
| `scripts/tests/test_run_cancel.py` | 필수 fixture 5건과 identity/restart/partial-cleanup/sibling 안전 테스트 9건 |
| `scripts/tests/run_tests.py` | `RunCancelTests` import와 aggregate 등록만 추가 |

## 2. 계약 매핑

이 task에 직접 귀속된 numbered PC 행은 없다. `docs/promoted-contracts.md`의 신규 의무 E-08,
ADR-0003 취소 절, cleanup entry의 ADR-0013 principal CAS와 §6 fixture 4를 아래 테스트가 고정한다.

| 계약 / ADR / fixture | 단언 테스트 함수 |
|---|---|
| E-08 positive liveness/exit, unknown에서 destructive resolution 금지 | `test_fixture_4_case_2_unverified_identity_never_signals`, `test_fixture_4_case_3_exited_unreconciled_refuses_terminal_and_cleanup`, `test_fixture_4_case_5_expiry_and_no_heartbeat_never_authorize_cleanup` |
| ADR-0003 cancel은 intent만 기록; `running`/`alive`/`unknown-effect`/`cancel-pending` 자원 보존 | `test_fixture_4_case_1_unknown_effect_records_intent_and_preserves_resources`, `test_fixture_4_case_2_unverified_identity_never_signals`, `test_fixture_4_case_4_verified_reconcile_cleanup_is_restart_idempotent` |
| ADR-0003 terminal/cleanup = positive EXITED **AND** effect reconciliation | `test_fixture_4_case_3_exited_unreconciled_refuses_terminal_and_cleanup`, `test_fixture_4_case_4_verified_reconcile_cleanup_is_restart_idempotent` |
| ADR-0003 cleanup은 terminal 뒤 별도 idempotent action | `test_fixture_4_case_4_verified_reconcile_cleanup_is_restart_idempotent`, `test_partial_cleanup_failure_is_typed_and_resumes_same_action` |
| ADR-0013 cleanup current owner token + fencing epoch + entity version guard | `test_fixture_4_case_4_verified_reconcile_cleanup_is_restart_idempotent`, `test_partial_cleanup_failure_is_typed_and_resumes_same_action`, `test_fixture_4_case_5_expiry_and_no_heartbeat_never_authorize_cleanup` |
| §6 fixture 4 / case 1: unknown-effect cancel은 intent만 기록, bytes 불변 | `test_fixture_4_case_1_unknown_effect_records_intent_and_preserves_resources` |
| §6 fixture 4 / case 2: stale/unknown identity signal 0회 | `test_fixture_4_case_2_unverified_identity_never_signals` |
| §6 fixture 4 / case 3: EXITED + unreconciled는 terminal/cleanup 거부 | `test_fixture_4_case_3_exited_unreconciled_refuses_terminal_and_cleanup` |
| §6 fixture 4 / case 4: verified signal → exit → real reconcile → canceled → cleanup 2회 | `test_fixture_4_case_4_verified_reconcile_cleanup_is_restart_idempotent` |
| §6 fixture 4 / case 5: expired lease + heartbeat 부재는 cleanup 권한 아님 | `test_fixture_4_case_5_expiry_and_no_heartbeat_never_authorize_cleanup` |
| signal 전 실패가 `stopping`을 영구 고착시키지 않음 | `test_signal_failure_in_stopping_is_retryable_after_restart` |
| single-source 범위 밖 live sibling은 cleanup 0회 | `test_live_sibling_added_after_terminal_blocks_source_cleanup` |

## 3. 검증

- Focused: `uv run scripts/tests/test_run_cancel.py` → 9 tests, rc 0.
- 인접 기질: supervisor 11 + effects 23 + lease 12 + cancel 9 = 55 tests, 모두 green.
- 최종 aggregate 명령:

  ```text
  env -u FORCE_COLOR -u CLICOLOR_FORCE uv run scripts/tests/run_tests.py > /tmp/suite-m1b-cancel.log 2>&1; echo "suite rc=$?"
  ```

- 최종 결과: **suite rc=0**, `Ran 997 tests in 105.501s`, `OK`.
- 로그: `/tmp/suite-m1b-cancel.log`
- `git diff --cached --check`: green before commit.

필수 aggregate가 ignored `.waystone/lock`과 `.waystone/.gitignore`를 생성/갱신했다. 최종 확인에서
ignored `.waystone/profile.yml`과 host-harness `.waystone/resume.md`도 관측했다. 계약 지시에 따라
lock/profile 상태를 복원·삭제하지 않았다.

## 4. 계약 해석 및 needs-ruling 후보

1. **구체 resource manifest/late sibling 경계.** Pinned store/effects에는 source action이 소유한
   worktree/ref/artifact의 immutable manifest나 target reservation API가 없다. 현재 구현은 M1-B
   single-source 범위, deterministic cleanup action, fixed `executor_id`, source/cleanup principal guard와
   membership 재검사를 사용하며 executor를 engine-owned exact-action adapter로 신뢰한다. 그러나
   `cleanup-executing` WAI 직후 외부 executor가 끝나기 전에 별도 코드가 canceled run에 새 action을
   직접 추가하는 경합을 `cancel.py`만으로 금지할 수는 없다. 엄밀한 multi-action 지원에는 intent에
   결속된 exact target manifest, worktree/ref expected-state CAS, creation과 공유하는 reservation/lock,
   artifact reachability-aware GC 또는 store의 canceled-run child 금지가 필요하다. 기존 모듈 수정 금지
   때문에 이를 선반영하지 않았다.
2. **Content-addressed artifact 삭제.** 테스트 artifact는 fixture 전용 unique bytes라 cleanup callback이
   삭제한다. 실제 CAS bytes는 다른 reference가 공유할 수 있으므로 production adapter가 action 단위로
   raw unlink해서는 안 되고 mark-root/reachability GC로 넘겨야 한다. GC는 계획상 M1-C 이후 범위다.
3. **pending reason 문언 충돌.** 계획 §3-9 요약 bullet은 identity 불명도
   `cancel-pending(unknown-effect)`로 축약하지만, accepted ADR-0003 truth table은
   `identity-unknown`, `liveness-unknown`, `unknown-effect`를 구분한다. 더 정밀한 accepted ADR 표를 따라
   세 reason을 보존했다.
4. **schema v1 reason 부재.** Store의 `TransitionReason`에는 stopping/canceled/cleanup 전용 reason이
   없다. run 취소 transition은 모두 `CANCEL_REQUESTED`, cleanup action은 existing
   `CANCEL_REQUESTED`/`PROCESS_STARTED`/`COMPLETED` reason을 사용한다. state 문자열이 의미를 보존한다.
   전용 reason을 원하면 store schema/API 변경 ruling이 필요하다.
5. **single-source 범위.** M1-B exit가 단일 task run을 대상으로 하므로 terminal과 cleanup은 source
   action 하나(plus deterministic cleanup action)만 허용한다. retry/multi-job cancellation은 target
   action set을 intent에 freeze하고 전체 set의 process/effect/principal을 aggregate해야 하며 이번 task
   범위에서 추측 구현하지 않았다.
6. **public composition seam 부재.** Exact signal에는 supervisor runtime/launch identity, terminal에는
   source action과 run을 한 transaction에서 묶는 CAS, cleanup action에는 guarded transition이 필요하다.
   공개 API가 없어 package-private `_read_runtime`, `_read_launch`, `_load_plan`, `_current_principal`,
   `_guard_operation`, `_record_guarded_action_transition`과 store transaction primitives를 조합했다.
   후속 통합 전 public signal/current-principal/cancel-terminal API 승격 여부가 필요하다.
7. **signal WAI의 중복 가능성.** `stopping` commit 뒤 signal syscall/commit 결과가 불명확하면 resume은
   같은 verified identity에 signal을 재전송한다. 종료 signal은 이 재시도를 감당하는 engine-owned
   adapter라는 선택이다. 정확히 한 번 signal receipt가 필요하면 supervisor-owned signal WAI/receipt
   API가 추가되어야 한다.
8. **signal capability 부재 처리.** Identity unknown과 engine misconfiguration을 섞지 않았다. identity가
   alive인데 signal adapter가 없으면 intent는 보존하되 `signal_capability_unavailable` typed error를 낸다.
   이를 pending 성공 결과로 바꾸는 대안은 silent fallback이라 기각했다.
9. **terminal 이후 source reclaim.** Terminal evidence는 당시 current source owner/fence/version에
   결속된다. 그 tuple이 이후 바뀌면 cleanup은 fail-closed한다. Terminal source lease를 합법적으로
   reclaim해야 하는 미래 흐름에는 새 quiescence evidence를 terminal/cleanup authority에 재결속하는
   별도 protocol이 필요하다.
10. **CAS artifact orphan.** Intent/terminal/cleanup-plan artifact bytes는 DB transition 전에 atomic CAS
    store에 기록된다. 뒤의 DB CAS가 실패하면 unreferenced bytes가 남을 수 있으며 성공으로 강등하지
    않는다. 이를 즉시 삭제하면 concurrent/shared digest를 훼손할 수 있어 향후 GC 대상으로 남긴다.

## 5. 스코프 밖에서 발견한 문제

- `Supervisor`에는 public verified-signal API와 exact runtime identity getter가 없다. 현재 task는 private
  evidence reader로 fail-closed 조합했지만 public boundary가 있으면 cancel module이 훨씬 단순해진다.
- `RunStore.create_action()`은 run state가 `canceled`여도 새 child action 생성을 자체 거부하지 않는다.
  현재 cancel module은 entry/WAI/completion에서 이를 감지하지만, 전역 FSM 불변조건으로 만들려면 store
  또는 planner task가 소유해야 한다.
- Source-owned worktree/ref/artifact manifest 및 shared artifact GC substrate가 아직 없다. 이 task의
  cleanup executor는 그 미래 adapter 경계를 대신 정의할 뿐 target planner/GC를 구현하지 않는다.
- 위 항목들은 scope 밖 existing module을 수정하지 않고 그대로 기록했다.
