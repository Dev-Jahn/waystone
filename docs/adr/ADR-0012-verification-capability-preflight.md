# ADR-0012: verification plan과 capability preflight를 dispatch 전에 고정한다

- Status: accepted
- Date: 2026-07-19
- Round: —
- SSOT sections affected: 없음 — E-07과 ADR-0004의 dispatch 전제조건을 정밀화
- Tasks: feat/canonical-project-identity

## Context

E-07은 verifier evidence를 `(base, patch bytes) → result digest`에 결속하지만, required check와
independent verifier가 실제 환경에서 실행 가능한지는 보장하지 않는다. ADR-0004도 실행 불가능한
action의 silent executor fallback을 금지하지만, 검증 환경의 준비를 worker dispatch 전에 증명하는
계약은 두지 않았다.

2026-07-19의 delegation 8회 모두에서 runner의 worktree-local `uv` cache에 `pyyaml`과 `ruff`가
없어 전체 suite와 lint를 실행하지 못했다. 그중 한 건은 RED 단계조차 시작하지 못했다. 당시에는
coordinator가 main checkout에서 ad hoc command를 실행해 보완했지만 autonomous run에서는 worker의
limitation이 coordinator 단독 확인이나 check 생략으로 조용히 바뀔 수 있다. 이는 I-02·I-04와 E-07이
요구하는 독립 검증을 actor 하나의 주장으로 축소한다.

이 ADR의 근거 범위는 `docs/reviews/2026-07-19-m0-contracts-feedback.md`의 JW-GPT-019 중
`실패 메커니즘`과 `필수 수정`, ADR-0002~0008, `docs/invariants.md`로 한정한다.

## Decision

### Frozen `VerificationPlan`

coordinator는 acceptance와 함께 `VerificationPlan`을 run/job spec에 넣고 worker dispatch 전에
freeze한다. plan과 각 check에는 최소한 다음 정보가 들어간다.

| Field | 계약 |
|---|---|
| `required_checks` | stable `check_id`, phase, exact command/arguments, working-directory rule, expected outcome |
| `command_input_digest` | command, relevant environment, base/input와 fixture를 결속하는 digest |
| `required_toolchain` | executable, runtime, dependency와 version constraint |
| `environment_preparation` | 동일 환경을 재현하는 선언적·기록 가능한 준비 절차 |
| `network_cache_requirements` | network 필요 여부, 허용된 source, cache namespace와 offline 가능 여부 |
| `sandbox_level` | check와 verifier에 필요한 filesystem/process/network 권한 |
| `authoritative_executor` | required deterministic check는 `engine`으로 고정 |
| `worker_execution_required` | capability 확인과 별개로 worker가 RED 또는 self-check로 같은 command를 실제 반복해야 하는지 여부 |
| `verifier_backend_capability` | required independent verifier의 backend, sandbox, result-digest 입력 능력 |

plan 전체는 digest로 run/job spec에 결속한다. dispatch 뒤 command, dependency, sandbox 또는
executor를 바꾸는 것은 환경 적응이 아니라 새 plan이다. coordinator decision에 변경 사유를 남기고
다시 freeze·preflight하지 않으면 사용할 수 없다.

### Capability preflight는 dispatch precondition이다

engine은 frozen spec을 받은 뒤 worker process를 시작하기 전에 plan이 선언한 environment를
materialize하고 다음을 모두 확인한다.

1. engine-owned isolated job/integration worktree에서 모든 required deterministic check의 exact
   entrypoint와 dependency를 resolve하고 실행할 수 있다.
2. 모든 required check는 실제 worker와 같은 sandbox, worktree layout, toolchain,
   dependency/cache namespace에서도 command가 test/lint runner까지 실행될 수 있다.
   `worker_execution_required=true`이면 worker job 안에서의 실제 반복도 frozen contract에 남는다.
3. `environment_preparation`은 선언된 입력만으로 반복 가능하고 준비 과정과 결과 digest를 artifact로
   기록할 수 있다. undeclared network나 ambient host cache에 의존하지 않는다.
4. required independent verifier backend가 사용 가능하며 frozen base, patch bytes, result digest와
   artifact output을 받을 capability가 있다.
5. RED-first가 acceptance의 일부면 base snapshot에서 지정한 RED command를 실제 실행해 expected
   failure를 관측하고 artifact로 기록할 수 있다.

단순히 executable 이름이 `PATH`에 있다는 사실은 충분하지 않다. preflight는 command invocation,
필수 import/plugin, sandbox access와 declared cache availability를 확인한다. RED-first가 아닌 check의
base 결과가 test failure여도 command가 정상 실행되어 structured result를 냈다면 capability failure로
오분류하지 않는다. 환경 준비와 check semantics를 별도 artifact로 기록한다.

preflight evidence에는 `verification_plan_digest`, environment preparation digest, worker와 engine
각 probe의 exact command·exit·artifact digest, verifier capability 결과를 넣는다. 이 evidence는
실제 result verification을 대신하지 않으며 해당 frozen environment가 dispatch 가능한지만 증명한다.

필수 capability가 없으면 worker를 dispatch하지 않는다.

```text
waiting_user(reason=verification-environment-unavailable)
refused(reason=required-check-unexecutable)
```

사용자 승인, credential, 허용된 dependency/cache 제공처럼 외부 입력으로 준비가 가능하면
`waiting_user`를 사용한다. frozen command 또는 sandbox에서 실행 자체가 불가능하면 `refused`를
사용한다. 이 상태를 worker self-report, coordinator manual check, 다른 command, 약한 sandbox 또는
다른 executor로 조용히 강등하지 않는다. 이는 ADR-0004와 I-11의 typed refusal 규칙이다.

명시적 waiver가 required verification을 제거할 수는 있지만 생략한 `check_id`, 결정 actor, 이유를
decision artifact에 기록하고 `VerificationPlan`을 다시 freeze한 뒤 남은 plan 전체를 preflight해야
한다. waiver는 생략한 check를 실행됐다고 표시하거나 E-07 evidence로 대체하지 않는다.

### 채택안: authoritative deterministic check를 engine action으로 실행한다

preflight를 통과한 뒤 worker는 bounded implementation과 요구된 RED/self-check를 수행할 수 있다.
그러나 그 command 결과는 environment가 완전해도 계속 `worker_claim`이다. worker가 자기 결과를
수용할 수 없다는 I-02는 toolchain 품질로 해제되지 않는다.

권위 있는 deterministic check는 engine이 frozen `VerificationPlan`에 따라 isolated
job/integration worktree에서 실행한다. engine은 base와 worker patch bytes로 result를 구성하고,
result digest를 재도출한 뒤 exact command, exit status, stdout/stderr와 생성 artifact digest를
ADR-0002의 engine-owned action으로 관측·기록한다. command 변경이나 main checkout의 ad hoc 결과는
이 action의 evidence가 아니다.

required independent verifier는 ADR-0008의 별도 `verifier` role로 같은 frozen result digest를
검토하고 별도 artifact를 남긴다. engine은 deterministic command의 executor와 evidence recorder일
뿐 verifier 판단이나 coordinator의 integration decision을 대행하지 않는다. coordinator는 worker
claim, engine check evidence, verifier artifact를 소비해 decision을 기록한다. 따라서 실행 위치를
engine으로 옮겨도 다음 분리가 유지된다.

```text
worker implementation and self-check claim
→ engine-owned deterministic check evidence
→ independent verifier evidence bound to result digest
→ coordinator integration decision
```

검증의 독립성은 “worker와 다른 directory에서 command를 실행했다”가 아니라 구현 actor가 자기
결과를 최종 승인하지 못하고 verifier evidence와 integration decision이 별도 actor/artifact로 남는
데서 온다. 채택안은 worker 자기보고를 권위 사실에서 제거하고 required verifier availability를
dispatch gate로 만들므로 E-07의 독립성을 약화하지 않고 강화한다.

## Consequences

- worker와 authoritative check 환경이 dispatch 전에 준비되므로 missing dependency를 limitation으로
  남긴 채 구현부터 시작하지 않는다.
- required verifier를 사용할 수 없는 run은 조정자 단독 검증으로 완료되지 않는다.
- engine은 deterministic check 환경과 artifact를 소유해야 하지만 worker·verifier·decision의
  provenance 경계는 유지한다.
- environment 또는 command 변경은 새 plan digest와 새 preflight evidence를 요구한다.

## Alternatives considered

- **worker 환경만 보장하고 worker self-check를 authoritative verification으로 사용** — 명령은
  실행 가능해져도 구현자가 자기 결과를 승인하는 I-02 위반과 self-report provenance 문제를
  해결하지 못하므로 기각.
- **채택: deterministic verification을 engine-owned action으로 이동** — engine이 exact result를
  재구성·관측하고 별도 verifier가 판단하므로 worker claim과 검증 사실을 분리한다. worker 환경의
  실행 가능성은 RED/self-check readiness로 계속 preflight하지만 authority 근거로 쓰지 않는다.
- **dispatch 후 missing tool을 worker limitation으로 허용** — 검증 불가능한 job을 이미 시작하고
  coordinator 대행이나 check 생략을 유도하므로 기각.
- **main checkout에서 coordinator가 ad hoc command 실행** — frozen result/environment 결속과 별도
  verifier provenance가 없어 E-07 evidence가 될 수 없으므로 기각.
