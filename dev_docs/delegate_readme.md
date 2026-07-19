# How delegation works in waystone

정확히는 두 층을 구분해야 합니다.

- `$waystone:delegate` / `/waystone:delegate`: 전체 위임 절차를 조정하는 스킬
- `waystone delegate <subcommand>`: 실제 record·worktree·검증·판정을 다루는 저수준 CLI

아래 본문은 최신 개발트리 `dev@1065ea0` 기준입니다. 현재 설치된 릴리스 `0.10.0`과의 차이는 마지막에 따로 정리했습니다.

## 전체 흐름

```text
선택·routing
  ├─ host-guided 실행
  │    └─ main/subagent/workflow가 직접 수행
  │       delegate record 없음
  │
  └─ implementer/external-runner
       └─ run
           ├─ failed-env / failed-runner / failed-artifact
           │    └─ discard 후 새 run으로 retry
           │
           └─ needs-review
                ├─ verify          검증 증거 추가, 상태 그대로
                ├─ verdict         판정 기록 추가, 상태 그대로
                ├─ apply           live tree에 반영 → applied
                └─ discard         결과 폐기 → discarded
```

중요한 경계는 다음과 같습니다.

- `run` 성공은 “완료”가 아니라 `needs-review`입니다.
- `verify`는 증거만 만들고 판단하지 않습니다.
- `verdict`는 판단을 기록하지만 patch를 적용하지 않습니다.
- `apply`는 patch만 live tree에 반영합니다. 커밋하거나 task를 `done`으로 바꾸지 않습니다.
- `discard`는 verdict 없이도 실패·crash record를 정리할 수 있습니다.

실제 handler는 8개입니다: `plan`, `run`, `status`, `show`, `verify`, `verdict`, `apply`, `discard`. [delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:3433)

## 명령 요약

| 명령 | 실제 역할 | 주요 생성·변경 | 상태 변화 |
|---|---|---|---|
| `plan` | 여러 위임의 fan-out manifest 생성 | stdout JSON만 | 없음 |
| `run` | 격리 worktree에서 implementer 실행 | record, refs, worktree, patch, contract | `claimed → running → needs-review/failed-*` |
| `status` | record 상태 조회 | 없음 | 없음 |
| `show` | patch·contract·검증·실패 증거 조회 | 없음 | 없음 |
| `verify` | 독립 verifier 실행 | `verify-N.json` | `needs-review` 유지 |
| `verdict` | main-session 판정 기록 | `verdict-N.json` | `needs-review` 유지 |
| `apply` | 승인 patch를 live tree에 적용 | live working tree | `needs-review → applied` |
| `discard` | 위임 결과와 worktree 폐기 | worktree/ref 제거, record 보존 | `* → discarding → discarded` |

## 1. `delegate plan`

```bash
waystone delegate plan <task-id>... --json \
  [--routing-note "..."] [--root DIR]
```

여러 task를 deterministic workflow carrier에 넘기기 위한 계획만 생성합니다. 직접 runner를 시작하지 않습니다.

내부에서 하는 일:

1. `delegation.enabled`가 활성화되어 있는지 검사합니다.
2. profile에서 `orchestrator`, `clerk`, `implementer` binding을 읽습니다.
3. 다음 조건을 강제합니다.

   - orchestrator: `deterministic-workflow`
   - implementer: `external-runner`
   - 세 역할 모두 model/backend와 effort가 명시됨

4. task ID 중복을 제거합니다.
5. 각 task에 대해 검사합니다.

   - 상태가 `pending|active`
   - 모든 dependency가 `done`
   - acceptance criteria가 존재
   - 같은 task의 nonterminal delegation이 없음
   - 전체 delegation record 중 corrupt nonterminal record가 없음

6. 다음을 포함한 JSON manifest를 stdout에 출력합니다.

   - root와 correlation ID
   - profile fingerprint
   - registry fingerprint
   - task별 packet digest
   - declared scope와 dependency
   - scope overlap 쌍
   - scope 미선언 task
   - orchestrator/clerk/implementer carrier binding

`plan` 자체는 `.waystone/delegations` record나 worktree를 만들지 않습니다. carrier가 이 manifest를 받아 각 task에 대해 `run`을 호출합니다. [delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:3220)

현재 v1 carrier는 Claude Workflow뿐이며, scope가 겹치거나 선언되지 않은 task는 직렬, scope가 분리된 task만 병렬 lane에 배치합니다. carrier는 `verify`, `verdict`, `apply`, `discard`를 하지 않습니다. [delegate-fanout.workflow.js](/Users/jahn/workspace/waystone/templates/hosts/claude-code/delegate-fanout.workflow.js:79)

## 2. `delegate run`

```bash
waystone delegate run <task-id> \
  [--accept "..."]... \
  [--note "retry reason"] \
  [--routing-note "..."] \
  [--root DIR]
```

fan-out carrier에서는 추가로 다음을 사용합니다.

```bash
--expect-packet-sha SHA
--expect-profile FINGERPRINT
--carrier claude-workflow
--carrier-instance ID
--json-events
```

실제 순서는 다음과 같습니다.

### A. 실행 전 gate

- initialized project인지 확인
- delegation consent 확인
- unborn HEAD, submodule, unmerged index, 진행 중인 merge/rebase 등을 거부
- reserved `JW_REPORT.yaml`이 live tree에 이미 있으면 거부
- profile에서 `implementer/external-runner` binding 확인
- task가 `pending|active`인지 확인
- dependency가 전부 `done`인지 확인
- acceptance criteria가 비어 있지 않은지 확인
- 동일 task의 nonterminal delegation이 없는지 확인
- `--expect-profile`과 현재 profile fingerprint 비교
- `--expect-packet-sha`와 다시 만든 packet digest 비교

`--role` 옵션은 존재하지만 현재 `implementer` 외 역할은 거부합니다. host-guided binding을 external runner로 몰래 변환하지 않습니다. [delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:1557)

### B. delegation claim

고유 ID를 만듭니다.

```text
20260718T123456Z-fix-something
```

그리고 먼저 다음 record를 생성합니다.

```text
.waystone/delegations/<did>/claim.json
```

이 시점 이후 실패하면 최소한 delegation ID와 task 소유권은 복구할 수 있습니다. 아직 `status.json`이 없으면 `status`에서 `claimed`로 표시됩니다.

동일 task의 `failed-*`, `running`, `needs-review`, `discarding` record도 모두 task 소유권을 계속 잡습니다. `applied`, `discarded`만 terminal입니다. [delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:1422)

### C. 현재 tree snapshot

live branch나 index를 건드리지 않고 임시 Git index를 사용해 다음을 snapshot commit으로 고정합니다.

- HEAD의 모든 tracked 파일
- staged 변경
- unstaged 변경
- non-ignored untracked 파일

깨끗하면 HEAD 자체를 base로 사용하고, dirty하면 branch에 연결되지 않은 snapshot commit을 만듭니다.

```text
refs/waystone/delegations/<did>
```

그 base를 사용해 detached worktree를 만듭니다.

```text
~/.waystone/cache/worktrees/<project-slug>/<did>/
```

따라서 구현 agent는 사용자가 현재 보고 있던 dirty tree와 같은 출발점을 봅니다. [delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:261)

### D. immutable context 기록

record에 다음을 남깁니다.

```text
claim.json
packet.yaml
exposure.json
status.json
prompt.txt
```

- `packet.yaml`: task, acceptance, dependency, scope, routing/retry context
- `exposure.json`: base SHA, dirty 여부, binding, model, profile fingerprint, sandbox, overlay
- `status.json`: `running` 상태 및 이후 모든 전이
- `prompt.txt`: implementer에게 실제 전달된 prompt

### E. 환경 준비

`.waystone.yml`의 `delegation.env_prep`이 있으면 해당 명령을 순서대로 실행합니다.

없으면 첫 번째로 발견한 파일에 따라 하나를 선택합니다.

```text
uv.lock            → uv sync --frozen
pnpm-lock.yaml     → pnpm install --frozen-lockfile
package-lock.json  → npm ci
Cargo.toml         → cargo fetch
go.mod             → go mod download
```

shell 없이 worktree cwd에서 실행하며, 실패하면 `failed-env`가 되고 worktree를 보존합니다. [delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:665)

### F. implementer 실행

Codex backend:

```text
codex exec
  -C <worktree>
  -m <bound-model>
  -s workspace-write
  model_reasoning_effort=<bound-effort>
```

Claude backend는 OS-level confinement가 없기 때문에 기본적으로 거부됩니다. 사용자가 명시적으로 다음을 승인해야 합니다.

```bash
--allow-unsandboxed-runner --reason "..."
```

이 경우에도 cwd와 tool permission은 보안 경계로 간주하지 않습니다.

runner는 worktree root에 `JW_REPORT.yaml`을 남기도록 지시받습니다. 이 보고서는 patch에 포함되기 전에 제거되고 다음 항목만 `delegate-claimed`로 contract에 들어갑니다.

- 수행했다고 주장한 verification
- limitations
- risks
- escalations

runner의 주장은 harness 사실이나 acceptance 판정으로 승격되지 않습니다.

### G. 결과 산출

runner 종료 후 Git에서 직접 계산합니다.

- result snapshot SHA
- changed-file 목록
- binary-safe patch
- patch SHA-256
- empty patch 여부

생성되는 결과:

```text
refs/waystone/delegations/<did>-result

.waystone/delegations/<did>/artifact/
├── contract.yaml
└── changes.patch       # non-empty일 때만
```

`contract.yaml`에는 harness가 계산한 base/result/patch 사실과 delegate가 주장한 report가 분리되어 들어갑니다.

성공 상태는 `needs-review`입니다. 실패는 다음 중 하나이며 worktree는 그대로 남습니다.

- `failed-env`
- `failed-runner`
- `failed-artifact`

`run`은 자동 retry하지 않습니다. 스킬이 transient failure라고 판단하면 기존 record를 `discard`한 뒤 `--note`를 붙여 새 record로 다시 `run`합니다.

## 3. `delegate status`

```bash
waystone delegate status [<delegation-id>] [--json] [--root DIR]
```

하나 또는 모든 delegation의 다음 정보를 읽습니다.

- delegation ID
- task ID
- state
- base SHA
- 최초 timestamp
- corrupt 여부

특징:

- claim만 있고 exposure/status가 아직 없으면 `claimed`
- 전체 목록에서는 손상 record를 `[corrupt]`로 표시하고 나머지 목록은 계속 출력
- `--json`은 한 건이어도 배열을 출력
- 상태나 파일을 변경하지 않음

상태 계산은 [delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:2990)에 있습니다.

## 4. `delegate show`

```bash
waystone delegate show <delegation-id> [surface] [--root DIR]
```

surface별 의미:

| 옵션 | 출력 |
|---|---|
| 없음 | 상태, changed-file 수, patch 유무, report 유무, verify 개수 |
| `--patch` | `changes.patch` raw bytes |
| `--report` | delegate 주장만이 아니라 전체 `contract.yaml` |
| `--exposure` | immutable `exposure.json` |
| `--verify` | 가장 최신 `verify-N.json` |
| `--failure` | 기록된 error, probe/runner stderr 마지막 50줄, 진단 hint |

surface 옵션은 한 번에 하나만 허용합니다. 단일 record 조회는 strict하므로 corrupt 파일이 있으면 정확한 파일명을 대며 실패합니다. [delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:3055)

## 5. `delegate verify`

```bash
waystone delegate verify <delegation-id> [--root DIR]
```

`needs-review`에서만 실행됩니다.

내부 순서:

1. contract, exposure, patch digest를 검증합니다.
2. 보존된 worktree를 강제로 다음 상태로 정규화합니다.

   ```text
   checkout --force --detach <base>
   clean -fd
   apply <exact changes.patch>
   ```

3. 만들어진 tree가 contract의 `result_sha`와 동일한지 검증합니다.
4. 현재 profile의 verifier binding을 읽습니다.
5. acceptance와 changed files를 넣은 adversarial-review prompt를 실행합니다.
6. 실행 전후 tracked/untracked/ignored 파일과 HEAD를 비교합니다.
7. verifier가 worktree를 수정했다면 결과를 기록하지 않고 실패합니다.
8. 성공하면 다음 번호로 append합니다.

   ```text
   artifact/verify-1.json
   artifact/verify-2.json
   ...
   ```

Codex verifier는 `read-only` sandbox에서 실행됩니다. Claude verifier는 `Read/Glob/Grep`만 허용하고, 실행 전후 filesystem postcondition으로 쓰기를 탐지합니다.

상태는 계속 `needs-review`입니다. 여러 번 verify한 경우 최신 artifact가 이후 verdict의 기준입니다. [delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:2781)

## 6. `delegate verdict`

```bash
waystone delegate verdict <delegation-id> \
  --file verdict.json \
  [--override-blocker --reason "..."] \
  [--override-unmet --reason "..."] \
  [--root DIR]
```

이 명령은 테스트나 verifier를 실행하지 않습니다. main session이 만든 판단 JSON을 검증하고 append합니다.

검사하는 것:

- delegation이 `needs-review`
- verdict criteria가 packet acceptance 원문 집합과 정확히 일치
- run에서 verification이 required로 기록됐으면 `verify-N.json` 존재
- verifier가 불필요한 경로라면 `agent_checks`가 최소 하나 존재
- `met: true`인 apply criterion에 해석 가능한 evidence reference 존재
- 최신 verifier blocker가 있으면 각 blocker를 직접 반박하는 `agent_checks`와 override 필요
- unmet criterion을 두고 apply하려면 명시적 `--override-unmet --reason` 필요

그 뒤 harness가 다음을 추가합니다.

- 판단 시각
- main-session provenance
- 최신 verify 번호
- 현재 profile fingerprint
- contract/patch/verify digest

그리고 append-only artifact를 씁니다.

```text
artifact/verdict-1.json
artifact/verdict-2.json
...
```

상태는 여전히 `needs-review`입니다. 새로운 verify를 실행하면 기존 verdict는 stale해져 새 verdict가 필요합니다. [delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:2693)

## 7. `delegate apply`

```bash
waystone delegate apply <delegation-id> [--root DIR]
```

조건:

- 상태가 `needs-review`
- canonical 최신 verdict가 존재
- 최신 verdict decision이 `apply`
- verdict 이후 contract/patch/verify가 변경되지 않음
- blocker/unmet override gate가 모두 유효

그 뒤 live tree에 plain `git apply`를 실행합니다.

- 3-way apply 없음
- stash fallback 없음
- 자동 충돌 해결 없음
- live tree drift로 patch가 맞지 않으면 전체 실패
- empty patch는 no-op 성공

성공하면:

- cached worktree 제거
- 상태를 `applied`로 전환
- record directory 보존
- base/result refs 보존
- live tree에 변경은 생기지만 커밋은 하지 않음
- `tasks.yaml`의 task 상태도 바꾸지 않음

[delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:2915)

## 8. `delegate discard`

```bash
waystone delegate discard <delegation-id> \
  --reason "..." [--root DIR]
```

모든 nonterminal 상태를 정리할 수 있습니다.

- `claimed`
- `running`
- `failed-*`
- `needs-review`
- `discarding`
- corrupt/crash remnant

순서:

1. reason과 함께 `discarding` 상태를 먼저 기록
2. cached worktree 제거
3. base/result refs 제거
4. Git worktree prune 및 제거 postcondition 확인
5. `discarded` 기록

중간 cleanup이 실패하면 `discarding`과 reason이 남으므로 같은 명령을 재실행해 이어갈 수 있습니다.

record directory 자체는 감사 이력으로 영구 보존됩니다. verdict는 필수가 아닙니다.

`--orphan`은 record directory는 사라졌지만 worktree/ref만 남은 경우의 복구 경로입니다.

```bash
waystone delegate discard <did> --orphan --reason "..."
```

[delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:2951)

## 저장 위치와 terminal 결과

```text
프로젝트 record:
  <project>/.waystone/delegations/<did>/

격리 worktree:
  ~/.waystone/cache/worktrees/<project-slug>/<did>/

Git refs:
  refs/waystone/delegations/<did>
  refs/waystone/delegations/<did>-result
```

| 최종 명령 | live tree | worktree | refs | record |
|---|---|---|---|---|
| `apply` | patch 반영 | 제거 | 보존 | 보존 |
| `discard` | 변경 없음 | 제거 | 제거 | 보존 |

또한 모든 서브커맨드는 handler 실행 전 root를 resolve하고, 필요한 경우 machine/project lazy migration을 수행할 수 있습니다. [delegate.py](/Users/jahn/workspace/waystone/scripts/delegate.py:3306)

## `$waystone:delegate` 스킬이 추가로 하는 일

스킬은 위 CLI를 단순 호출하는 wrapper가 아닙니다.

1. routing 질문 8개를 평가합니다.
2. profile binding에 따라 실행 경로를 결정합니다.
3. owner material에서 정확히 도출되는 경우에만 task scope/acceptance를 먼저 registry에 기록합니다.
4. external runner라면 `run`을 호출합니다.
5. worker report를 사실로 취급하지 않고 `show --report`로 contract만 읽습니다.
6. verifier binding이 있으면 `verify`; 없으면 main-session의 bounded direct checks를 수행합니다.
7. criterion별 verdict를 기록합니다.
8. 결정에 따라 `apply` 또는 `discard`합니다.
9. transient retry는 기존 record를 폐기하고 최대 새 run으로 처리합니다.

반대로 binding이 `main-session`, `clean-subagent`, `forked-subagent`, `deterministic-workflow`라면 일반적으로 `delegate run`을 호출하지 않습니다. host가 해당 실행을 담당하고 route attribution은 round close에 기록합니다.

## 설치판 `0.10.0`과 현재 dev의 차이

현재 세션의 실제 설치판은 `0.10.0`이고, 이 버전에는 7개 command만 있습니다. [설치판 delegate.py](/Users/jahn/.codex/plugins/cache/jahns-codex-marketplace/waystone/0.10.0/scripts/delegate.py:2436)

| 항목 | 설치판 0.10.0 | 현재 dev |
|---|---|---|
| `plan` | 없음 | 있음 |
| dependency gate | dependency를 packet에만 기록하고 `run`에서 차단하지 않음 | 모든 dependency가 `done`이어야 함 |
| `run --json-events` | 없음 | 있음 |
| stale plan pin | 없음 | `--expect-packet-sha`, `--expect-profile` |
| carrier attribution | 없음 | `--carrier`, `--carrier-instance` |
| `status --json` | 없음 | 있음 |
| Codex verifier | host에 따라 CLI/companion transport 파생 | host-independent `codex exec` read-only |

즉 지금 설치판으로 직접 실행한다면 `plan`이나 carrier 옵션은 사용할 수 없습니다. 개발트리의 `bin/waystone-codex`를 실행할 때만 8개 표면이 보입니다.

## 현재 dev에서 확인된 불일치·미해결점

- 서브커맨드별 `--help`가 구현되어 있지 않습니다. `delegate --help`도 오류로 처리됩니다.
- 통합 front-door 설명은 아직 `plan`을 누락합니다. [waystone.py](/Users/jahn/workspace/waystone/scripts/waystone.py:19)
- skill 문서는 `apply --override-no-verdict --reason`을 언급하지만 실제 parser는 이를 지원하지 않으며, apply는 반드시 verdict를 요구합니다. [SKILL.md](/Users/jahn/workspace/waystone/skills/delegate/SKILL.md:262), [run_tests.py](/Users/jahn/workspace/waystone/scripts/tests/run_tests.py:9325)
- dev의 plan digest가 prompt에 들어가는 `milestone`, `round`, `anchor`, `notes`를 아직 포함하지 않습니다.
- manifest의 `registry_fingerprint`는 생성되지만 현재 `run`에서 소비되지 않습니다.
- dev의 Codex sandbox probe 성공 증명이 machine-local이 아니라 tracked `.waystone.yml`에 기록되는 문제가 확인되어 수정 task로 등록돼 있습니다. [carrier-lanes-feedback.md](/Users/jahn/workspace/waystone/docs/reviews/2026-07-18-carrier-lanes-feedback.md:155)

분석은 실제 parser·구현·문서·테스트를 독립 대조했으며, workspace 파일은 변경하지 않았습니다. 검증 중 잘못된 test selector로 두 번 loader error가 있었지만 정확한 이름으로 재실행한 대상 테스트는 통과했고, 제품 동작 오류는 아니었습니다.

