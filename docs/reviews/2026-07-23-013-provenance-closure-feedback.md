<!-- waystone feedback: the body below is the reviewer reply VERBATIM (byte-exact copy via `waystone review ingest`) — do not edit it; a triage skeleton is appended beneath it. -->
round: 2026-07-23-013-provenance-closure
reviewer: chatgpt:gpt-5.6-pro
reviewer-effort: high
review-target: e8b236a2bb600ad6c4cd1b36ee341b4a981b8a34
reply-metadata-json: {"metadata":{"effort":"high","model":"chatgpt:gpt-5.6-pro","request-digest":"sha256:dfb0bb926c290674971e411f7b84cee8501dfa65b827d0815c7afa555341aad3","review-target":"e8b236a2bb600ad6c4cd1b36ee341b4a981b8a34"},"narrative_digest":"sha256:32e8cedbffa26799e1d00f7360e9746d7277d50e26f462bc2b48319e8e541fd9","rendered_request_coverage_reason":null,"rendered_request_digest":"sha256:dfb0bb926c290674971e411f7b84cee8501dfa65b827d0815c7afa555341aad3","rendered_request_digest_matches":true}
ingested: 2026-07-23
source: /tmp/review.md
verbatim-bytes: 18642

---

```text
model: chatgpt:gpt-5.6-pro
effort: high
review-target: e8b236a2bb600ad6c4cd1b36ee341b4a981b8a34
request-digest: sha256:dfb0bb926c290674971e411f7b84cee8501dfa65b827d0815c7afa555341aad3
```

# Review verdict — CHANGES REQUESTED

**Critical 0 / Major 3**

이번 closure는 이전 지적을 이름만 바꾸어 닫은 작업은 아닙니다. 다음 항목은 실제 authority 경계로 이동했습니다.

* evaluate/promote 모델이 candidate OID에서 materialize된 별도 root를 보도록 변경됨
* verifier와 integration decision이 store/CAS에서 다시 로드됨
* unpublished in-memory tuple만으로 target apply를 통과시키는 경로가 폐쇄됨
* semantic verifier rejection이 동일 candidate에 대해 terminal이 됨
* canonical checkout 안에서는 superseded Project Brief fact가 disposition을 만들지 못함

특히 published verifier/decision reload와 private `_execute_stage()` composition은 이전 WS-GPT-024의 핵심 실패 메커니즘을 폐쇄한 것으로 봅니다. Apply 직전에도 verifier, reviewer, decision을 다시 읽고, 전달받은 객체와 reloaded tuple이 다르면 거부합니다.  Integration decision 자체도 실제 CAS와 effect intent를 다시 파싱하고, expected attempt/action lineage를 검증합니다.

다만 세 경계가 남았습니다.

1. candidate **filesystem**은 결속되지만 child **execution environment**는 결속되지 않습니다.
2. 승인된 candidate를 적용할 때 checked-out branch ref만 이동시키고 worktree/index는 갱신하지 않습니다.
3. current-objective 검증은 전달받은 root에는 엄격하지만, public `review` surface가 canonical project root를 사용하지 않습니다.

---

# Claims adjudication

| Claim                                       | 판정                                                                    |
| ------------------------------------------- | --------------------------------------------------------------------- |
| Candidate에서 evaluator/verifier가 실제 실행됨      | **부분 폐쇄** — filesystem/OID는 폐쇄, environment provenance는 미폐쇄           |
| Unpublished typed tuple로 promotion apply 불가 | **폐쇄**                                                                |
| Stale objective로 finding materialization 불가 | **부분 폐쇄** — canonical root에서는 폐쇄, linked-worktree public path에서 우회 가능 |
| Semantic reject는 동일 candidate에 terminal     | **폐쇄**                                                                |
| Failure detail은 safe diagnostic으로 노출        | **Major/Critical 없음**                                                 |
| Scaffold가 protocol field만 파생                | **Major/Critical 없음**                                                 |
| Release projection이 신규 runtime을 포함          | **정적 검토 기준 승인**, 전체 release readiness는 아래 findings로 보류                |

---

# Confirmed findings

## WS-GPT-026 — candidate runner의 child environment가 frozen provenance에 포함되지 않는다

**Severity: major**

### 실패 메커니즘

현재 `RunnerInvocation` digest에 들어가는 값은 다음 세 가지뿐입니다.

```text
argv
cwd
candidate_context
```

`candidate_context`에는 candidate OID, root fingerprint, RunSpec digest가 들어가지만 child environment는 포함되지 않습니다. 따라서 동일 invocation digest가 서로 다른 `PATH`, `PYTHONPATH`, `GIT_*`, `UV_*`, virtualenv 및 config 환경에서 실행될 수 있습니다.

Evaluate의 detached path는 더 명확합니다. Supervisor environment는 allowlist가 아니라 현재 `os.environ` 전체를 복사한 뒤, 실행 중인 Waystone package root를 `PYTHONPATH`에 추가합니다.  Detached supervisor가 실제 evaluator를 시작할 때도 별도 `env`를 전달하지 않으므로 이 환경이 그대로 상속됩니다.

Promote verifier도 candidate materialized root에서 실행되기는 하지만, `subprocess.run()`에 environment를 전달하지 않기 때문에 현재 coordinator process의 environment를 그대로 상속합니다.

결과적으로 다음 상태가 가능합니다.

```text
cwd / candidate fingerprint  → candidate C
Git/Python/uv command source  → integration checkout B
verifier result               → B를 보고 PASS
published VerifierEvidence    → C에 결속
promotion                     → C 적용
```

Candidate root는 실행 전후 동일하므로 현재 fingerprint 검사는 이 차이를 발견하지 못합니다.

### 구체적 재현

별도 최소 Git 재현에서 candidate directory를 cwd로 두더라도 다음 환경을 설정하면:

```bash
GIT_DIR=/path/to/integration/.git
GIT_WORK_TREE=/path/to/integration
git show HEAD:verdict
```

candidate의 파일이 아니라 integration checkout의 `verdict`를 읽습니다. Candidate worktree bytes는 변하지 않으므로 pre/post fingerprint는 모두 통과합니다.

동일한 부류는 다음 변수에서도 발생할 수 있습니다.

```text
GIT_DIR
GIT_WORK_TREE
PYTHONPATH
PYTHONHOME
VIRTUAL_ENV
UV_PROJECT
UV_WORKING_DIRECTORY
UV_ENV_FILE
PIP_CONFIG_FILE
```

모든 변수가 항상 잘못된 결과를 만드는 것은 아니지만, invocation authority가 이 값들을 전혀 관측하거나 제한하지 않는다는 것이 문제입니다.

### 영향

* candidate와 다른 checkout의 test 결과가 candidate evidence로 승격될 수 있음
* Waystone dogfooding에서 verifier가 candidate package가 아니라 실행 중인 installed/dev Waystone package를 import할 수 있음
* 같은 invocation digest를 replay해도 다른 결과가 나올 수 있음
* verifier capability proof가 실제 child execution environment를 설명하지 못함

이는 단순 reproducibility 문제가 아니라 **judgment provenance mismatch**입니다.

### 필수 수정

`RunnerInvocation`에 frozen child-environment contract를 추가해야 합니다.

권장 형태:

```text
RunnerEnvironment
  inherited_allowlist
  explicit_values
  stripped_names
  value_digests
  environment_digest
```

최소 요구사항:

1. Evaluate detached supervisor와 Promote direct verifier가 같은 environment builder를 사용
2. `os.environ` wholesale inheritance 금지
3. Git·Python·uv의 checkout/project redirection 변수는 기본 제거
4. 필요한 `PATH`, `HOME`, `TMPDIR`, locale, proxy/network 변수는 명시적으로 선택
5. environment digest를 invocation digest에 포함
6. launch record와 verifier evidence가 environment digest를 보존
7. preflight proof가 실제 child environment와 일치하는지 확인

필수 회귀:

```text
integration tree: PASS
candidate tree: FAIL
ambient GIT_DIR/GIT_WORK_TREE 또는 UV_WORKING_DIRECTORY: integration 지정

expected:
  candidate verifier는 FAIL
  launch/environment digest는 ambient 변경을 관측
```

---

## WS-GPT-027 — promotion이 checked-out branch ref만 이동시키고 worktree/index는 이전 tree에 남긴다

**Severity: major**

### 실패 메커니즘

Promotion lineage의 integration target은 private staging ref가 아니라 evaluate 실행 시점의 symbolic `HEAD`입니다.

```python
git symbolic-ref --quiet HEAD
→ refs/heads/<current-branch>
```

따라서 선택된 target은 그 순간 실제 worktree에 checked out되어 있는 branch입니다.

Apply 단계는 reloaded authority를 확인한 뒤 `GitRefEffect`를 생성합니다.

```text
target_ref
expected old OID
candidate OID
```

그러나 `GitRefEffect`의 실제 driver는 다음 명령만 수행합니다.

```bash
git update-ref <checked-out-branch> <candidate-oid> <expected-old-oid>
```

`git update-ref`는 branch ref를 이동시키지만, 해당 branch를 checkout하고 있는 worktree의 파일과 index를 candidate tree로 materialize하지 않습니다.

### 최소 재현 결과

일반 Git 저장소에서 다음을 수행했습니다.

```text
before:
  HEAD = A
  worktree file = old

git update-ref refs/heads/master B A

after:
  HEAD = B
  worktree file = old
  git status = M  file
```

즉 successful promotion 이후 저장소는 다음 상태가 됩니다.

```text
branch/HEAD authority: candidate C
index/worktree bytes: previous B
```

현재 E2E test도 promotion 후 `git rev-parse HEAD == candidate_oid`만 확인합니다. 실제 worktree bytes와 clean status는 확인하지 않습니다.

### 영향

* 사용자는 candidate가 적용됐다고 보지만 실제 파일은 이전 버전임
* 후속 commit이 candidate 내용을 되돌리는 거대한 역방향 diff가 될 수 있음
* run snapshot이 branch tree와 worktree delta를 혼합한 상태에서 시작됨
* uncommitted user change가 있더라도 ref CAS만 맞으면 branch ref가 이동함
* 다른 worktree에서 target branch가 checked out된 경우 그 worktree가 같은 방식으로 불일치함
* “live tree delivery는 아직 M2”라는 현재 surface와 충돌함

Generic verification path에는 이미 checked-out integration ref를 거부하는 코드가 존재합니다.  그러나 staged public promotion은 그 안전 경로를 사용하지 않고 raw `GitRefEffect`를 사용합니다.

### 필수 수정

권장 기본값은 **private integration ref**입니다.

```text
refs/waystone/integration/<promotion-lineage-or-run-id>
```

Promotion은 이 private ref까지만 이동시켜야 합니다.

```text
promote
→ private integration ref
→ closeout
→ explicit delivery policy
→ checked-out user branch/worktree
```

현재 M2 delivery가 구현되지 않았다면 public branch는 움직이지 않는 편이 맞습니다.

즉시 branch delivery를 0.13에 유지하려면 최소한 다음이 별도 effect로 필요합니다.

* target ref가 checked out된 모든 worktree 관측
* clean worktree/index precondition
* user delivery consent
* ref와 index/worktree를 일관되게 전환하는 명시적 operation
* 실패 시 branch와 worktree가 서로 다른 tree를 가리키지 않는 recovery protocol

`git reset --hard` 같은 silent repair는 사용자 파일을 파괴할 수 있으므로 대안이 아닙니다.

필수 회귀:

```text
successful promote 이후:
  checked-out public branch OID unchanged
  canonical worktree bytes unchanged
  canonical index unchanged
  git status unchanged
  private integration ref만 candidate OID로 이동
```

---

## WS-GPT-028 — public review surface가 linked worktree HEAD를 “current project authority”로 사용할 수 있다

**Severity: major**

### 실패 메커니즘

`validate_disposition_authority()` 자체는 전달받은 root 안에서는 강해졌습니다.

* objective commit이 current HEAD의 ancestor인지 확인
* current HEAD의 동일 fact가 여전히 존재하는지 확인
* digest와 binding이 동일한지 확인

문제는 public `waystone review`가 canonical `ProjectContext`를 사용하지 않는다는 점입니다.

Review CLI의 root resolver는 다음과 같습니다.

```python
Path(value).resolve()
# 또는
find_project_root(Path.cwd())
```

Unified front door도 `review_group.main()`을 그대로 호출하며, 사전에 canonical project normalization을 수행하지 않습니다.

따라서 다음 경로가 성립합니다.

```text
canonical checkout:
  HEAD = B
  objective O가 수정 또는 폐기됨

linked worktree:
  HEAD = A
  old objective O가 여전히 binding

cwd = linked worktree
waystone review disposition ...
waystone review materialize ...
```

`validate_disposition_authority()`는 linked worktree의 `A`를 `current_head`로 읽습니다. 따라서 `ProjectFactRef(commit=A)`는 superseded가 아니라 current authority로 판정됩니다.

이후 materialize도 동일 linked root에서 실행됩니다. It loads linked `tasks.yaml` and directly calls `tasks_cli.cmd_add(root, fields)`.  `cmd_add()` 자체에는 canonical project나 linked-worktree mutation guard가 없고 전달된 `root/tasks.yaml`을 바로 수정합니다.

### 결과

원래 폐쇄하려던 경로가 checkout context만 바꾸면 다시 열립니다.

```text
confirmed finding
→ canonical project에서는 폐기된 objective
→ stale linked worktree에서는 current objective
→ fix-now disposition
→ linked tasks.yaml에 executable task 생성
```

추가로 validation/disposition/review artifact도 linked checkout의 `docs/reviews`에 기록되므로, canonical checkout의 finding chain과 별도의 authority branch가 생깁니다.

### 영향

* 폐기된 제품 방향이 다시 selected work로 materialize됨
* canonical checkout에서는 보이지 않는 review/task state 생성
* 같은 project에 두 개의 “current” finding head와 task registry가 존재
* linked worktree에서 이후 run을 시작할 경우 stale intent가 다시 execution input으로 유입될 수 있음
* 사용자 surface가 내부 checkout topology를 알아야만 안전해짐

### 필수 수정

`review`의 모든 public subcommand가 `ProjectContext`를 먼저 해석해야 합니다.

```text
ingest
validate
disposition
materialize
attach
```

권장 규칙:

* runtime/review/task authority root는 항상 `canonical_root`
* linked worktree cwd는 locator일 뿐 project identity가 아님
* Git-tracked review 및 selected-work mutation은 canonical checkout에서만 수행
* 명시적으로 noncanonical mutation을 지원하지 않는다면 typed refusal
* `tasks_cli.cmd_add()` 같은 내부 direct call도 canonical context proof 없이는 호출 금지
* `--root` 역시 arbitrary initialized checkout이 아니라 registered canonical project로 해석

필수 회귀:

```text
canonical root B: objective changed
linked worktree A: old objective remains
cwd = linked worktree
append/materialize using old ProjectFactRef(A)

expected:
  objective-superseded 또는 noncanonical-worktree refusal
  canonical tasks.yaml unchanged
  linked tasks.yaml unchanged
  양쪽 docs/reviews unchanged
```

---

# 폐쇄된 이전 findings

## Published authority reload

WS-GPT-024의 핵심은 폐쇄된 것으로 봅니다.

`_reload_promotion_authority()`는 current attempt의 verifier와 decision을 실제 store/CAS에서 다시 읽고, review chain도 다시 로드합니다.  `_apply_candidate()`는 apply 직전에 같은 tuple을 한 번 더 reload하고 전달값과 불일치하면 거부합니다.

Unpublished synthetic tuple이 target apply에 도달하지 못하는 negative regression도 추가됐습니다.

## Semantic reject retry

Published semantic reject는 동일 candidate에 대해 terminal이고, 새 candidate 또는 owner ruling 없이 새 attempt를 만들지 못합니다.  Process failure와 malformed output만 별도 retry 대상으로 남긴 테스트 구성도 적절합니다.

## Current-objective comparison 내부 로직

Canonical root가 전달된다는 전제에서는 다음이 모두 구현됐습니다.

* unchanged fact + unrelated commit: 허용
* fact text 변경: 거부
* fact 삭제: 거부
* binding 변경: 거부
* non-ancestor objective commit: 거부
* materialize 시 재검증

문제는 이 로직 자체가 아니라 public caller가 canonical root를 보장하지 않는다는 점입니다.

---

# Open domain questions

## 1. Promotion과 delivery의 제품 경계

다음 중 하나를 명시적으로 선택해야 합니다.

### 권고

```text
Promotion:
  private integration ref에 durable capability를 승격

Delivery:
  사용자의 checked-out branch/worktree에 반영
```

이 분리가 현재의 nondestructive 원칙과 M2 계획에 가장 잘 맞습니다.

### 대안

Promotion 자체가 delivery라면 0.13에서 delivery consent, dirty-tree drift 및 worktree/index materialization까지 책임져야 합니다. 단순 branch ref update는 어느 정의에도 맞지 않습니다.

## 2. Child environment에서 무엇을 허용할 것인가

Environment allowlist에는 최소한 다음 ruling이 필요합니다.

* network proxy와 인증 변수를 verifier에 전달할지
* user-level Git config와 global tool config를 허용할지
* project virtualenv를 사용할지 engine-owned toolchain만 사용할지
* `HOME`을 격리할지
* Codex configuration bytes를 어느 digest에 결속할지

핵심은 특정 선택보다 **동일 environment가 invocation authority에 포함되는 것**입니다.

## 3. Owner principal binding

`decided_by.role: owner`가 실제 owner evidence 또는 principal과 결속되는 문제는 이번 target에서도 별도 장기 경계로 남아 있습니다. 이번 request가 해당 범위를 이미 별도 설계 대상으로 분리했고 solo-local trust domain을 전제로 하므로 신규 major로 중복 계상하지 않았습니다.

다만 현 상태에서 owner-only disposition을 암호학적 또는 독립적인 human-authorization boundary라고 설명해서는 안 됩니다.

---

# Residual risks and unavailable environment

* 이번 검토는 지정 diff `3c411b2… → e8b236a…`의 production code, targeted regressions, request 및 관련 authority path를 정적으로 대조했습니다.
* 지정 target SHA에 연결된 GitHub Actions workflow run이나 combined status는 connector에서 확인되지 않았습니다.
* Request에 기록된 271-test suite와 real Codex smoke는 제가 재실행하지 않았습니다.
* Candidate environment 문제는 코드 추적 외에 별도 최소 재현으로 `GIT_DIR/GIT_WORK_TREE`가 cwd와 다른 checkout의 Git authority를 선택하는 것을 확인했습니다.
* Checked-out branch apply 문제도 별도 최소 Git 재현으로 `update-ref` 후 HEAD만 이동하고 worktree file은 이전 bytes, index는 modified 상태가 되는 것을 확인했습니다.
* GPU와 별도 dataset은 이번 trust-kernel 검토에 필요하지 않았습니다.
* 첨부된 Omniphysics 대화 기록은 Waystone의 intent-control 설계가 도입된 배경으로만 참고했으며, 이번 세 code finding의 직접적인 증거로 사용하지 않았습니다.

# 최종 판정

이번 target으로 다음은 실질적으로 폐쇄됐습니다.

```text
in-memory verifier/decision object → apply
semantic reject → stochastic retry-to-pass
historical objective ref → canonical current objective
```

그러나 다음 경계가 남았습니다.

```text
candidate filesystem provenance
≠
candidate execution-environment provenance

approved candidate ref
≠
consistent checked-out worktree

canonical current objective
≠
linked-worktree-local current objective
```

따라서 `e8b236a2bb600ad6c4cd1b36ee341b4a981b8a34`는 **CHANGES REQUESTED**입니다.


---

<!-- waystone triage: BEGIN -->
## Findings (triage — 자유 형식 리뷰(WS-GPT- prefix라 skeleton 미파싱), verbatim 본문에서 직접 추출; 전 항목 main 독립 코드 대조 + finding당 codex verifier 1기 적대 검증 완료)

리뷰 정식 판정: "**CHANGES REQUESTED** — Critical 0 / Major 3". **직전 round의 major 3건(WS-GPT-023 core·024·025 로직)은 폐쇄 판정** — published authority reload·private composition·semantic reject terminal·current-objective 로직(canonical root 전제) 전부 인정. Claims adjudication: 폐쇄 3(unpublished tuple·semantic terminal·release projection 정적), 부분 폐쇄 2(candidate 실행 — filesystem은 폐쇄/environment 미폐쇄, stale objective — canonical root에서는 폐쇄/linked worktree 우회), 무-major 2(failure detail·scaffold). **Release readiness는 신규 major 3건 폐쇄까지 계속 보류.**

| # | finding (리뷰 절) | verdict | type | severity | evidence (검증 근거) | task id |
|---|---|---|---|---|---|---|
| WS-GPT-026 | "candidate runner의 child environment가 frozen provenance에 포함되지 않는다" — cwd/fingerprint는 candidate에 결속되나 GIT_DIR/GIT_WORK_TREE/PYTHONPATH/UV_* 등 ambient env가 판단 소스를 다른 checkout으로 redirect 가능 | REAL | verification | major | main 독립 확인: invocation digest=argv/cwd/candidate_context뿐(`engine.py:634-643`), supervisor env=`dict(os.environ)` 통상속+**실행 중 waystone 패키지를 PYTHONPATH 선두 주입**(`supervisor.py:857-864` — dogfooding에서 verifier가 candidate가 아닌 하네스 패키지를 import하는 자기참조 오염 벡터), promote verifier subprocess env 미전달(verify.py에 env= 부재). verifier v026 CONFIRMED — pre/post fingerprint는 candidate bytes 불변만 검사해 타 checkout 읽기를 검출 불가, 리뷰어의 GIT_DIR 최소 재현은 표준 git 의미론으로 유효. 비악의 도달(셸 잔존 env·dev/installed 혼입) 실재 | fix/runner-environment-provenance |
| WS-GPT-027 | "promotion이 checked-out branch ref만 이동시키고 worktree/index는 이전 tree에 남긴다" — update-ref 후 HEAD=candidate/파일=이전/status dirty | REAL | correctness | major | main 독립 확인: target=`git symbolic-ref HEAD`의 checked-out branch(`run_group.py:287-295`), apply driver=`git update-ref`만(`effects.py:1542`), E2E는 `rev-parse HEAD`만 단언(`test_run_cli.py:806` — worktree bytes/status 미검사). verifier v027 CONFIRMED — 우회 경로가 아니라 E2E가 통과시키는 기본 경로. generic 경로(apply_integration_decision)의 checked-out 거부(PC-17)를 staged promotion이 미사용(w0723c에서 그 불변조건을 보존하기 위해 GitRefEffect를 유지한 결정의 귀결 — 당시 머지 노트에 기록된 사항) | fix/promotion-private-integration-ref |
| WS-GPT-028 | "public review surface가 linked worktree HEAD를 current project authority로 사용할 수 있다" — WS-GPT-025 수정이 checkout topology로 재개방 | REAL | architecture | major | main 독립 확인: review만 raw root 해석(`review_group.py:256` — `Path(value).resolve()`/`find_project_root(cwd)`, attach만 예외적으로 context 사용 `:336`), run/status/brief는 `resolve_project_context` 사용. canonical 기계에 `canonical_root_is_linked_worktree` 오류 코드 기존재(context.py:36) — 배선 누락이 본질. verifier v028 CONFIRMED — 이 프로젝트의 상시 `.claude/worktrees` 운영 topology에서 도달 실증, materialize가 linked tasks.yaml을 직접 변경(cmd_add에 canonical guard 없음) | fix/review-canonical-root-authority |

### Open domain questions 처분 (ruling 자율권 정책)

- **Q1 (promotion vs delivery 제품 경계)**: **ruling 확정 — 권고안 채택.** promotion은 private integration ref(`refs/waystone/integration/*`) 승격까지, delivery는 별도 명시 operation(M2). 0.13에서 public branch는 이동하지 않는다. 근거: nondestructive 원칙·PC-17 정합·"live tree delivery는 M2" 기존 surface와 일치, 단순 branch ref update는 어느 delivery 정의에도 못 미침(리뷰어 논거 수용). fix/promotion-private-integration-ref에 반영.
- **Q2 (child environment allowlist ruling 항목들)**: fix/runner-environment-provenance 브리프에서 pre-register 후 구현 중 확정 — 핵심 수용 기준은 "특정 선택이 아니라 동일 environment가 invocation authority에 포함되는 것"(리뷰어 문언 그대로).
- **Q3 (owner principal binding)**: 기존 `chore/decision-actor-principal-binding` 유지(직전 round에서 이미 승계·기록) — owner-only disposition을 암호학적/독립 human-authorization 경계로 문서화하지 않는다는 제약 계속 유효. 신규 조치 없음.

### 등록 요약

- REAL 3건 / REJECTED 0건 / NEEDS-RULING 0건 (Q1은 자율 ruling 확정, Q2·Q3는 위 처분). 신규 등록 3건(전부 major).
- **직전 round 성과 확인**: 리뷰어가 major 3건(judgment provenance 부등식의 filesystem 축·published authority·objective currency 로직) 폐쇄를 명시 인정 — 이번 3건은 같은 부등식의 남은 축(environment·delivery 일관성·root canonicality)이다.
- release readiness는 계속 보류(리뷰어 명시) — 실 release는 이 3건 폐쇄 후로 재순연.
- 파일 충돌: 026·027이 engine.py 공유(026은 supervisor/verify 중심, 027은 effects/run_group 중심) — 착수 시 순차 또는 hot-file 구획 분할 필요. 028은 review_group.py 단독으로 독립 병렬 가능.
- 검증 방법: finding당 codex(gpt-5.6-sol, high) 적대 verifier 1기(read-only·정적, 보고서 scratchpad/reports/v026·v027·v028.md) + main 독립 라인 추적. 3기 모두 major 적정 판정(도달성 이견 없음 — 026·028은 비악의 도달 실증 포함).

### ingest 메타 상태

- reply 헤더의 request-digest는 이 round의 immutable sidecar와 일치, review-target `e8b236a…`도 round exposure 대상과 일치. declared model(chatgpt:gpt-5.6-pro) ≠ frozen reviewer(codex, gpt-5.5-pro — release 0.11.1 기본값 동결) → ingest 경고 "reply cannot count as configured feedback", **receipt pending(예고된 비치명 상태; finding 채택은 상기 main 독립 검증 경로로 — attestation 재작성 없음)**.
- `review-skipped-closes-v1`: **unevaluable(identity 불일치로 configured feedback 미집계)** — non-fire로 집계하지 않음. 그 외 adaptive-rule 출력 없음(경고는 rule fire 아님). verbatim 사본 18,642 bytes = drop-file 크기와 일치(byte-exact, drop-file 소비됨).
<!-- waystone triage: END -->
