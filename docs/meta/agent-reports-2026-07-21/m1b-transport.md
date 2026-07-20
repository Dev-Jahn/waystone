# m1b-transport 구현 보고 — `feat/run-actions-transport`

- base: `2f1dde2 feat(m1b): external effect commit protocol 엔진`
- 구현 commit: `8cf5386 feat(m1b): add run actions transport`
- push: 하지 않음

## ① 구현 요약과 파일 목록

`ActionTransport`를 추가해 immutable transport action plan으로 `carrier`/`user` action을
effect action과 구분했다. `actions_next(run_id)`는 ready outward action을 최우선으로 반환하고,
engine-owned effect는 bounded work budget 안에서 `EffectEngine`으로 reconcile한다. 실행 중 runner는
양수 polling hint가 있는 `busy`를 즉시 반환하고, terminal/wait/blocked 상태는 닫힌 `IdleReason`의
`idle`을 반환한다. executor plan이 없는 generic action은 carrier로 추측하지 않고 typed refusal한다.

`submit(action_id, result_payload)`은 current claimed outward action, immutable input digest, current
fencing epoch, action별 result schema, artifact bytes digest를 순서대로 검증한다. Git `result_sha`와
`changed_files`는 `adapters.git`으로 다시 읽고 carrier 주장과 exact 비교하며, test 결과는 bound
runner effect의 immutable invocation digest, fresh normalized completion marker, effect-owned observation
receipt, stdout/stderr CAS 재해시에서만 만든다. carrier가 보낸 `test_results`는 authority로 받지
않는다. raw non-UTF-8 Git pathname도 `surrogateescape` + ASCII-escaped canonical JSON으로 왕복한다.

submit은 첫 lease guard로 stale principal을 I/O 전에 거부하고, authority 관찰과 immutable CAS
publication 뒤 두 번째 guard의 짧은 DB callback에서만 result reference와 `completed` 전이를
승격한다. host가 상태 전이, retry, effect 재실행, evidence 승격을 선택하는 API는 노출하지 않았다.

실패에는 closed `TransportFailureCode`, `TransportExitCode`, recoverability 분류와 canonical
`encode_envelope`/`decode_envelope`를 구현했다. connection/timeout/HTTP 5xx 상당은 recoverable,
계약·검증 거부는 terminal, 분류 불능은 별도 `unclassified` code다.

변경 파일:

- `waystone/runs/transport.py` — action transport, result schema, typed refusals/envelope codec.
- `scripts/tests/test_run_transport.py` — 26개 transport 계약/fault fixture.
- `scripts/tests/run_tests.py` — `RunTransportTests` import와 aggregate 등록만 추가.

기존 `waystone/` 모듈과 legacy `scripts/*` 구현은 수정하지 않았고 신규 dependency도 추가하지
않았다.

## ② 계약 매핑 표

slice plan은 이 task에 numbered crash fixture나 promoted PC 전체를 직접 귀속하지 않았다.
아래 PC-16 행은 §3-3/I-03 중 **transport submit 경계만** 담당한다. PC-16의 patch bytes,
binary apply와 post-verdict 교체 거부 전체는 `feat/run-verify-decision` 소유다.

| 계약 / 할당 행 | 직접 단언하는 테스트 함수 |
|---|---|
| ADR-0004 executor 경계, M1-B exit 3: engine-owned action 반환 0·내부 소진 | `test_actions_next_exhausts_engine_action_and_returns_only_new_outward_action`, `test_plain_action_is_not_silently_inferred_as_carrier` |
| ADR-0004 3분기 우선순위: outward가 engine busy보다 우선 | `test_ready_outward_action_wins_over_in_flight_engine_action` |
| ADR-0004/§3-6 비차단 runner busy | `test_positive_in_flight_runner_returns_busy_without_blocking`, `test_planned_runner_never_calls_synchronous_executor` |
| §3-6 idle terminal/wait/blocked와 typed reason | `test_idle_reasons_are_closed_for_terminal_and_wait_states`, `test_unknown_effect_is_idle_only_when_run_is_actually_blocked`, `test_conflict_effect_is_idle_only_when_run_is_actually_blocked` |
| outward plan/action/reference 원자 결속과 recoverable claim | `test_actions_next_recovers_atomic_planned_outward_action` |
| §3-3 submit 검증 ① current claimed carrier/user action | `test_submit_refuses_noncurrent_claim_without_state_change`, `test_guard_refusal_writes_no_git_observation_artifacts_or_durable_rows`, `test_final_guard_race_preserves_authoritative_state`, `test_submit_preserves_plan_refusal_and_maps_unknown_principal` |
| §3-3 submit 검증 ② input digest | `test_submit_refuses_input_digest_mismatch_without_state_change` |
| §3-3 submit 검증 ③ fencing epoch | `test_submit_refuses_stale_fencing_epoch_without_state_change` |
| §3-3 submit 검증 ④ action result schema | `test_submit_refuses_action_result_schema_mismatch_without_state_change`, `test_carrier_test_results_and_non_json_objects_are_not_accepted_as_authority` |
| §3-3 submit 검증 ⑤ artifact digest = actual bytes | `test_submit_refuses_artifact_digest_mismatch_without_state_change` |
| §3-3 I-03 / PC-16 submit projection: Git facts engine 재도출 | `test_submit_rederives_git_facts_and_refuses_carrier_forgery`, `test_submit_persists_exact_engine_derived_git_facts`, `test_submit_preserves_engine_derived_non_utf8_git_paths` |
| §3-3 I-03: test 결과 caller 주장을 authority로 사용하지 않음 | `test_submit_persists_only_engine_observed_runner_test_results`, `test_submit_refuses_tampered_runner_authority_without_state_change`, `test_carrier_test_results_and_non_json_objects_are_not_accepted_as_authority` |
| 상태 전이는 engine만 결정 | `test_valid_submit_commits_engine_decided_completion` |
| §6 8-S3 typed envelope: transient/terminal/unclassified + exit enum | `test_failure_envelopes_classify_transient_contract_and_unknown`, `test_envelope_codec_accepts_registered_shapes_and_rejects_unknown_code` |

각 5중 검증 위조 fixture의 helper는 action row만이 아니라 SQLite 전 테이블과 artifact 파일의
full snapshot이 동일함을 단언한다.

## ③ 검증 결과

- Focused: `uv run scripts/tests/run_tests.py RunTransportTests` → **26 tests, rc=0**, `1.748s`.
- `git diff --check` → **rc=0**.
- `uv run python -m compileall -q waystone/runs/transport.py scripts/tests/test_run_transport.py`
  → **rc=0**.
- 필수 aggregate 명령:

  ```sh
  env -u FORCE_COLOR -u CLICOLOR_FORCE uv run scripts/tests/run_tests.py > /tmp/suite-m1b-transport.log 2>&1; echo "suite rc=$?"
  ```

  결과: **suite rc=0**, `Ran 1003 tests in 100.198s`, `OK`.
  로그: `/tmp/suite-m1b-transport.log`.

aggregate legacy diagnostic은 ignored `.waystone/lock`을 갱신했다. 관측한 순서는
size 91 / SHA-256 `bb0dfb93469e10e8c2136d37b09ca771241b0e4f38233e93b6419519ee7d86f2`,
첫 full run 뒤 size 90 / `796a4c2535cf3f72a745cfc5d0bef28af51c8eb99444c3370ceba9924bf745cb`,
최종 full run 뒤 size 89 / `1de9fa61eac850b3825d42c8936ddce3cd8da2c4dbc6a0c3bc89fc5e6b97d11d`다.
계약의 알려진 예외이므로 복원·삭제하지 않았다. 측정 구간에 `.waystone/.gitignore`와
`.waystone/profile.yml` digest는 변하지 않았다. host `PreCompact` hook이 만든 ignored
`.waystone/resume.md`(size 3935, SHA-256
`698a6fc1258c60111decc25069e7f518cc837a88299b65b79fe9298e26427017`)는 기록 후 최종 상태에서
제거했다.

## ④ 계약 해석 및 needs-ruling 후보

1. **Planned runner를 시작할 detached supervisor surface가 이 base에 없다.** 동기
   `EffectEngine` callback을 호출하면 수 분간 block할 수 있고, background thread를 새 supervisor로
   발명하면 D6/at-most-once 경계를 위반한다. 따라서 planned runner는 즉시
   `engine_executor_unavailable` typed refusal하고 executor를 호출하지 않는다. 실제 supervisor가
   runner를 `effect` 상태로 시작한 뒤에는 `actions_next`가 즉시 busy/reconcile한다. ADR-0004가
   unavailable executor의 typed refusal을 허용하지만, briefing의 “모든 engine-owned action 내부
   소진”을 planned runner까지 독립적으로 요구한다면 supervisor task와의 조합 surface가 필요하다.

2. **Active run을 `blocked`로 바꿀 정직한 transition reason이 없다.** 현재 store의 closed
   `TransitionReason`에는 `effect-unknown`/`effect-conflict`/`blocked` reason이 없고 transport가
   `planned` 같은 다른 reason을 재사용하면 함수명과 의미가 어긋난다. authoritative run이 이미
   `blocked`이면 typed `effect_unknown`/`effect_conflict` idle을 반환하고, active run이면
   `run_not_actionable` refusal한다. `busy`나 가짜 `run_state=blocked`로 강등하지 않았다. briefing이
   active UNKNOWN/CONFLICT 호출 하나로 blocked idle까지 요구한다면 run FSM owner가 state/reason
   vocabulary와 engine transition API를 추가해야 한다.

3. **Preflight action과 runner effect 사이 durable identity bridge가 없다.** transport plan은
   caller가 낸 digest를 받지 않고 immutable runner effect plan의 `invocation_digest`를 결속한다.
   그러나 현재 `EngineCheckAction`에는 materialized `action_id`가 없고 late submit 시
   `load_dispatch_ready()`도 사용할 수 없어, 이 digest가 그 action의 `prepared_input_digest`에서
   생성됐음을 transport만으로 재증명할 수 없다. supervisor/action materializer가 preflight evidence를
   검증한 뒤 그 binding을 effect plan에 결속하는 API가 필요하다.

4. **CAS bytes와 SQLite reference는 교차-authority 원자 transaction이 아니다.** 첫 guard 뒤
   content-addressed bytes를 publish하고 두 번째 guard에서만 DB authority로 승격한다. 두 번째 guard
   race에서는 action/lease/transition/reference가 전부 그대로지만 unreferenced immutable CAS bytes가
   남을 수 있다. CAS fsync를 `BEGIN IMMEDIATE` 안으로 옮기면 unbounded carrier payload가 모든 DB
   writer를 막고, callback 후반/commit fault의 orphan도 제거하지 못하므로 기존 effect/preflight와
   같은 짧은-guard 방식을 선택했다. staging/GC 또는 DB+CAS commit protocol은 store/artifact owner의
   별도 ruling이 필요하다.

5. **Generic outward action planning store API가 없다.** action row에는 executor kind, input/schema
   digest나 plan reference를 atomic하게 만들 generic surface가 없어 private
   `_plan_outward_action`이 immutable plan을 쓰고 package-internal
   `_create_planned_effect_action`을 조합했다. 이 helper 이름은 effect 전용이지만 reference id가
   `effect-plan:*`이 아니면 generic guarded transition으로 동작한다. 후속 planner가 의존하기 전
   generic `create_planned_action` surface와 store-level executor binding을 고정해야 한다.

6. **Git base binding의 상위 연결이 없다.** private planner가 engine-observed current `HEAD`를
   immutable base로 저장하고 submit에서 base→result facts를 다시 계산한다. carrier는 repository나
   base를 선택하지 못한다. 그러나 frozen RunSpec base를 outward plan에 넘기는 materializer surface는
   아직 없으므로 최종 engine 조합에서는 그 binding을 추가해야 한다.

7. **Transport wire format은 byte-level registry에 아직 고정되지 않았다.** sorted-key compact
   JSON, ASCII escaping, schema names, code strings와 exit 값 `OK=0`, `UNCLASSIFIED=1`, `REFUSED=2`,
   `TEMPORARY_FAILURE=75`를 선택했다. CLI bridge가 외부 호환 표면으로 노출하기 전에 format registry
   또는 ADR ruling이 필요하다.

8. **`unclassified`의 bool 해석:** envelope가 bool을 요구하므로 `recoverable=false`로 두되 이는
   terminal 판정이 아니라 “자동 retry를 허가하지 않음”을 뜻하고 별도 code/exit 1로 보존했다.
   tri-state를 원하면 envelope 계약 개정이 필요하다.

9. **Lease principal mismatch/unknown의 transport taxonomy:** submit check ① 관점에서 둘 다
   `action_not_current`로 wrapping했다. ADR-0013의 내부 `lease_principal_mismatch`와
   `lease_principal_unknown` code를 transport wire에서도 구분해야 한다면 closed transport enum에 두
   code를 추가해야 한다.

## ⑤ 스코프 밖에서 발견한 문제

- `feat/run-supervisor-identity`가 합쳐져도 planned runner를 transport가 nonblocking dispatch하는
  명시적 호출 surface가 필요하다. 현재 effect API는 runner callback을 동기로 실행한다.
- run/store v1에는 effect UNKNOWN/CONFLICT를 run-level blocked reason으로 승격할 vocabulary가 없다.
- preflight `EngineCheckAction` → materialized runner `action_id` 결속과 expected exit/evidence 판정은
  현재 모듈 사이에 비어 있다.
- full PC-16의 patch bytes, binary diff/apply, base/tree result 결속과 post-verdict 교체 거부는 이
  task가 아니라 `feat/run-verify-decision`에 남아 있다. 이 task는 submit-side SHA/path 재도출과 raw
  non-UTF-8 pathname 보존만 검증했다.
- unreferenced immutable artifact CAS bytes의 GC/staging surface가 없다. DB authority 누출은 없지만
  fault/race 반복 시 디스크 사용량이 증가할 수 있다.
- host hook의 ignored `.waystone/resume.md` 생성은 구현과 무관한 harness side effect였고 위에 기록한
  뒤 제거했다. final/session-end hook이 다시 만들 수 있다.
