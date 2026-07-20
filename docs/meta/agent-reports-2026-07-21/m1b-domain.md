# m1b-domain 구현 보고서

## 1. 구현 요약과 파일 목록

커밋: `47ef054 feat(jobs): add canonical run domain roles`

- `waystone/jobs/domain.py`
  - `Role` 4값(`coordinator`, `worker`, `verifier`, `reviewer`), `ExecutorKind` 3값,
    `ExecutionCategory` 3값을 닫힌 enum으로 추가했다.
  - role, execution category, backend만 갖는 frozen `RoleBinding`을 추가했다.
  - role과 executor kind 사이의 변환·추론 API는 추가하지 않았다.
- `waystone/jobs/profile_v1.py`
  - profile v1 YAML을 읽기 전용으로 판독하는 `read_profile_v1()` adapter를 추가했다.
  - `implementer`는 `worker`로 판독하되 legacy role/execution을 provenance에 보존한다.
  - `main`/`orchestrator`는 non-role legacy surface로, `clerk`는 deterministic-step 표식으로
    보존한다.
  - leaf role의 `deterministic-workflow`, 미지원 entry·role·execution·binding shape는
    `ProfileV1Refusal` 값으로 fail closed한다. profile 일부만 성공으로 반환하지 않는다.
  - 중복 YAML key의 last-value-wins도 `invalid_profile_v1` refusal로 막았다.
  - well-formed backend의 실제 실행 capability 판정은 후속 preflight에 남겼다.
- `scripts/tests/test_run_domain.py`
  - 지정된 domain/profile adapter 계약 테스트 6개를 추가했다. 모든 fixture project/profile은
    `TemporaryDirectory` 아래에 격리했다.
- `scripts/tests/run_tests.py`
  - 기존 항목은 바꾸지 않고 `RunDomainTests` import와 aggregate 항목만 추가했다.

## 2. 계약 매핑

| 계약 / ADR / fixture 행 | 이를 단언하는 테스트 함수 |
|---|---|
| ADR-0008 — canonical role은 정확히 4개, `main`/`orchestrator`는 role 아님 | `RunDomainTests.test_domain_enums_are_closed`, `RunDomainTests.test_profile_schema_fixture_read_round_trip_preserves_all_dispositions` |
| ADR-0004 — executor kind는 정확히 `engine`/`carrier`/`user` | `RunDomainTests.test_domain_enums_are_closed` |
| ADR-0008 — execution category는 정확히 `in-session`/`subagent`/`external`, deterministic workflow는 네 번째 범주가 아님 | `RunDomainTests.test_domain_enums_are_closed`, `RunDomainTests.test_unsupported_binding_returns_typed_refusal` |
| ADR-0008 — `implementer` → `worker`, legacy provenance 보존, canonical profile 재발행 없음 | `RunDomainTests.test_implementer_maps_to_worker_with_legacy_provenance_without_reissue` |
| ADR-0008 — `clerk`는 deterministic step, `main`/`orchestrator`는 canonical role로 승격하지 않음 | `RunDomainTests.test_profile_schema_fixture_read_round_trip_preserves_all_dispositions` |
| ADR-0004/0008 — role과 executor kind는 독립 축이며 상호 추론 API가 없음 | `RunDomainTests.test_role_and_executor_kind_have_no_inference_api` |
| PC-27(binding 판독 절반) — 미지원 execution/entry는 stable code·reason의 typed refusal이며 valid worker 뒤의 invalid verifier도 부분 성공하지 않음 | `RunDomainTests.test_unsupported_binding_returns_typed_refusal` |
| silent fallback 금지 — duplicate YAML binding과 unreadable path를 성공/예외로 뭉개지 않음 | `RunDomainTests.test_parse_failures_return_typed_refusal_without_last_value_wins` |
| `templates/profile-schema.json` 형태의 실제 6-role fixture — read-only 판독, 역할/실행 mapping, arbitrary well-formed backend는 capability로 오판하지 않음 | `RunDomainTests.test_profile_schema_fixture_read_round_trip_preserves_all_dispositions` |

## 3. 검증 결과

- 집중 회귀: `RunDomainTests` + legacy `DelegateProfileTests`, 25 tests, rc=0.
- 전체 suite 명령:
  `env -u FORCE_COLOR -u CLICOLOR_FORCE uv run scripts/tests/run_tests.py > /tmp/suite-m1b-domain.log 2>&1; echo "suite rc=$?"`
- 최종 결과: **suite rc=0**, `Ran 844 tests in 85.670s`, `OK`.
- 로그: `/tmp/suite-m1b-domain.log`
- `git diff --cached --check`: 통과.
- 최종 worktree의 `.waystone/`: 부재 확인.

## 4. 계약 해석 / needs-ruling 후보

1. Adapter entrypoint·성공 container·refusal code 어휘는 문서가 이름까지 고정하지 않았다.
   가장 작은 명시 API로 `read_profile_v1(path) -> ProfileV1 | ProfileV1Refusal`을 택했고,
   stable code는 `profile_v1_unreadable`, `invalid_profile_v1`,
   `unsupported_profile_binding`으로 정했다.
2. 필수 항목의 “fixture profile 파싱 왕복”은 writer 금지 및 canonical 재발행 금지와 양립하도록
   `temp profile bytes → read adapter → source bytes 동일`의 read-only round trip으로 해석했다.
   serializer/dumper는 의도적으로 제공하지 않았다.
3. `implementer`/`verifier`/`reviewer`의 legacy `deterministic-workflow`는 `subagent`로 축약하지
   않고 typed refusal한다. 대안은 host workflow를 subagent처럼 보는 것이지만, ADR-0008이 이를
   leaf execution category와 별도인 내부 orchestration procedure로 명시하므로 refusal이 가장
   보수적이다.
4. `main`/`orchestrator`를 조용히 버리거나 coordinator로 추론하지 않고 각각
   execution-location / engine-orchestration non-role 표식으로 보존했다. `clerk`도 실행 가능한
   role로 만들지 않고 deterministic-step 표식만 남겼다. 이 세 표식의 구체 타입명은 계약이
   고정하지 않았다.
5. profile 하나에 valid worker와 unsupported verifier가 함께 있으면 전체 profile을 refusal한다.
   부분 성공을 반환하는 대안은 worker가 invalid verifier 판독 전에 시작될 여지를 남겨 PC-27과
   맞지 않는다고 판단했다.
6. schema/runtime 불일치에서는 fail-closed 조합을 택했다. top-level unknown field는 schema대로
   거부하고, whitespace-only `use_for`는 runtime처럼 거부하며, `entry`는 verifier의
   `adversarial-review`만 허용한다. 특히 generic schema 구조상 non-verifier에도 그 entry가
   허용되는 것처럼 보이는 대안이 있으나 semantic binding으로 지원할 근거가 없어 refusal한다.
7. `effort`, `use_for`, legacy verifier `entry`는 shape를 검증하지만 canonical `RoleBinding`에는
   싣지 않았다. 할당 계약이 RoleBinding을 role/category/backend 세 필드로 고정한 것으로
   해석했다. 후속 engine input이 이 optional metadata를 요구한다면 별도 계약 결정이 필요하다.
8. arbitrary well-formed backend prefix는 adapter에서 capability refusal하지 않는다. binding
   identity 판독과 실제 runner/sandbox capability preflight를 분리하라는 task 귀속에 따른 선택이다.

## 5. 스코프 밖에서 발견한 문제

1. 현행 `templates/profile-schema.json`과 legacy `waystone/runs/delegate.py` validator 사이에
   이미 다음 차이가 있다: unknown top-level field, whitespace-only `use_for`, generic `entry`
   허용 위치/값, arbitrary backend schema 허용과 실제 runner token 지원 범위. legacy 파일은
   지시대로 수정하지 않았다.
2. 지정된 전체 aggregate suite가 worktree 루트에 ignored `.waystone/lock`과
   `.waystone/.gitignore`를 생성했다. 신규 `test_run_domain.py`는 tempdir만 사용하므로 legacy
   aggregate side effect다. 최종 suite 결과를 보존한 뒤 두 일반 파일과 빈 directory를 명시
   경로로 제거했고, 최종 `.waystone/` 부재를 확인했다. 생성물은 테스트용 lock state였으며
   복구할 필요가 없다. 이 task 범위에서는 legacy test/lock 경로를 수정하지 않았다.
