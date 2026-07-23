# Review Request — 2026-07-24-013-authority-closure

The reviewer has the repository via git. This is a domain/code review, not a workflow audit —
keep the waystone harness out of scope unless asked.

- Project: waystone
- Branch: dev
- Reviewer: codex, gpt-5.5-pro
- Reviewing: b31d01b437a0ec44eae0f03aa00fbe96dd334317   (diff against e8b236a2bb600ad6c4cd1b36ee341b4a981b8a34)

<!-- Keep the Reviewing field on exactly one line with the literal spacing shown above. -->

## What changed and why

당신의 2026-07-23 회신(대상 e8b236a, 판정 CHANGES REQUESTED — Critical 0/Major 3)에 대한 폐쇄
round다. diff base가 정확히 그 SHA이므로 이 diff 전체가 그 판정에 대한 응답이다. 세 major 전부
REAL 확정(main 독립 라인 추적 + finding당 적대 verifier) 후 다음 날 수리했다.

- WS-GPT-026: 자식 실행 환경을 invocation authority에 결속했다. 신설
  waystone/runs/environment.py의 build_runner_environment()가 명시 allowlist(PATH·HOME·
  locale·TERM·TMPDIR·USER·SHELL·proxy·UV_CACHE_DIR)로만 환경을 구축하고 — os.environ
  wholesale 상속 전면 제거 — 결정적 digest(정렬 key=value)를 invocation digest·supervisor
  launch record(schema v2)·promotion verifier launch evidence에 포함시켰다. supervisor
  bootstrap·실제 runner·direct promote verifier의 모든 subprocess가 명시 env=를 받고, 실행
  파일 탐색도 frozen PATH로 한다. detached 자식은 자기 환경을 재구축해 frozen digest와
  대조하고 불일치면 fail-loud. 당신이 지적한 PYTHONPATH 하네스 주입은 폐지했다 — runner는
  codex exec라 waystone import가 필요 없었고, 유일하게 import가 필요한 detached supervisor
  bootstrap은 실행 중 package root를 cwd로 명시하는 방식으로 대체했다.
- WS-GPT-027: 당신의 open question(promotion vs delivery)은 권고안을 ruling으로 채택했다 —
  promotion은 private integration ref(refs/waystone/integration/<promotion-lineage-id>,
  lineage id = evaluate WorkBrief의 canonical brief_id) 승격까지만, live-tree delivery는 M2의
  별도 명시 operation, 0.13에서 public branch는 이동하지 않는다. evaluate 시작 시 zero-OID
  CAS로 최초 생성하고 기존 ref는 보존해 expected-old CAS 의미론을 lineage 위에서 유지한다.
  generic apply의 PC-17 checked-out-ref 거부를 staged promotion apply 직전에도 강제했다.
  git reset --hard류 silent repair는 도입하지 않았다. 당신이 지적한 E2E blindspot은 역단언으로
  교정 — 이제 public branch OID·worktree bytes·index·status 불변과 private ref 이동을
  단언하며, 구 동작(HEAD 이동)이면 FAIL한다.
- WS-GPT-028: review 전 subcommand가 _review_context front door로 canonical
  ProjectContext(resolve_project_context)를 선검증한다. linked worktree cwd의 mutation과 명시
  --root <linked-worktree> 모두 기존 canonical_root_is_linked_worktree typed refusal로 거부
  (조용한 정규화 대신 refusal을 택한 근거: run start의 기존 관례와 정합). materialize는
  canonical ProjectContext proof 없이는 tasks_cli.cmd_add 경로에 진입할 수 없다. context.py의
  기존 resolver·오류 코드만 재사용했고 새 의미론은 만들지 않았다.
- 부수: 범위 밖 기존 결함 1건 발견·등록(fix/runtime-publication-race minor — supervisor
  runtime.json이 내용 기록 전 O_CREAT|O_EXCL로 노출되는 관측 창).

## Read these first

- PROGRESS.md의 `2026-07-24-013-authority-closure` 절 (커밋 sha·검증 방법·ruling 포함)
- docs/reviews/2026-07-23-013-provenance-closure-feedback.md 말미 triage 표 — 당신의 세
  finding별 검증 증거와 open Q1/Q2/Q3 처분
- waystone/runs/environment.py — allowlist builder와 digest 계약 전체
- waystone/runs/supervisor.py — launch schema v2, frozen env 전달·재검증, frozen PATH 탐색,
  구 _supervisor_environment 삭제
- waystone/cli/run_group.py의 _integration_target — private ref 산출·zero-OID CAS 생성
- waystone/cli/review_group.py의 _review_context와 materialize의 ProjectContext proof
- 신규 회귀 3모듈: scripts/tests/test_runner_env_provenance.py·
  test_promotion_integration_ref.py·test_review_canonical_root.py

## Claims to attack

1. 자식 실행 환경은 이제 invocation authority의 일부다 — ambient GIT_*/PYTHONPATH/UV_*
   redirect로 verifier/evaluator의 판단 소스를 candidate 밖으로 돌리는 경로가 없고, 통과되는
   모든 환경 값은 digest로 관측된다.
2. promotion 성공은 public checkout의 어떤 상태(branch OID·worktree bytes·index·status)도
   바꾸지 않는다 — 어떤 코드 경로로도 checkout/reset에 도달할 수 없다.
3. private integration ref의 expected-old CAS는 동시 lineage 전진을 fail-closed로 거부하며,
   최초 생성 CAS(zero-OID)도 경합에 안전하다.
4. review surface는 linked worktree HEAD를 current authority로 쓸 수 없다 — cwd 유도·명시
   --root·직접 함수 호출(materialize) 어느 경로로도.
5. PYTHONPATH 주입 폐지는 dogfooding 자기참조 오염 벡터를 제거하면서 detached supervisor
   부트스트랩과 installed 실행을 모두 보존한다.
6. 교정된 E2E는 회귀를 실제로 잡는다 — 구 promotion 동작을 재도입하면 FAIL한다.

## Evidence already produced (mine — inspect, don't trust)

- full suite 271→284 green: wave 조합 gate @ 7465ae7 + registry 커밋 전 재확인. 각 worktree
  전체 게이트(274·275·277)는 구현 기체 보고와 별개로 main이 재실행.
- 전 task base-RED를 main이 base(1ded7e7) 임시 worktree에서 독립 재현: 026 구조
  RED(waystone.runs.environment 부재), 027 신규 3건 구조 RED + 교정 e2e6 FAIL on base, 028
  4/4 FAIL — base에서 stale disposition 기록과 linked materialize가 실제로 성공하는 출력
  포함.
- 구현 3기(codex sol xhigh/high/high)의 보고서에 property별 acceptance 판정·RED 기록
  (pre-registered 기준 대비) 보존. main이 세 diff 전부 정독(특히 cwd-bootstrap이 ambient
  재도입이 아님을 별도 확인).

## Known weak spots

- private integration ref는 lineage authority 보존을 위해 durable하다 — GC/정리 정책 미설계
  (기존 chore/candidate-context-materialization-gc와 같은 계열로 등록·순연).
- supervisor launch v1 read 호환 창: 기존 증거 관측용으로 v1 스키마 read를 허용한다(신규
  detached 실행은 digest 부재 시 fail-loud). v1 read 표면이 남는 동안은 구 증거에 env
  결속이 없다.
- allowlist로 통과되는 HOME·proxy의 값은 digest에 결속되지만, HOME 아래 외부 도구 설정
  파일(git config·codex config)의 내용 자체는 비결속 — known boundary로 남긴다(solo local
  trust domain).
- detached supervisor bootstrap의 package-root cwd는 python -m의 sys.path 선두 의미론에
  의존한다 — 실행 중 하네스 자신을 가리키는 결정적 선택이지만, 이 경로의 견고성 이견 환영.
- runtime.json 0-byte publication race(기존 결함, minor 등록) — 관측 표면의 정직성 창.
- 이 round의 request도 release(0.11.1) round CLI로 생성 — frozen reviewer 기본값(codex,
  gpt-5.5-pro) 동결 한계 지속. 회신 identity 불일치 시 receipt pending은 정상이며 finding
  채택은 main 독립 검증으로 한다.

## Domain lens

trust-kernel 관점을 유지해달라. 이 round의 본질은 당신이 남긴 세 부등식 — 실행 환경
provenance ≠ candidate provenance, 승격된 ref ≠ 일관된 checkout, canonical objective ≠
linked-worktree objective — 를 등식으로 만드는 것이었다. 특히 ⑴ env digest 재검증의 TOCTOU
창(launch 기록과 detached 재검증 사이), ⑵ allowlist에서 빠진 변수 중 판단 소스를 바꿀 수
있는 잔여 벡터(동적 linker LD_*/DYLD_* 계열은 제거되지만 PATH 자체는 통과된다 — frozen
PATH 탐색이 충분한 방어인지), ⑶ refs/waystone/* private namespace에 대한 쓰기 권위가 다른
표면에서 보호되는지, ⑷ review front door를 우회하는 내부 호출 표면 잔존 여부, ⑸ promotion
결과를 소비하는 하위 표면(status·outcome ledger)이 private ref 의미론과 정합한지를
공격해달라. release 미승인의 세 사유가 폐쇄됐는지가 이 round의 판정 질문이다.

## Response wanted

Start the reply with this block (replace values; key case/order/spacing and a Markdown fence are
optional; extra keys are preserved). Echo the `Reviewing` target, alone or as a 12–40 hex
`base-target` range, and copy the request digest exactly; missing/damaged values stay unknown, and
no model/target means ordinary prose:
```text
model: codex
effort: high
review-target: b31d01b437a0ec44eae0f03aa00fbe96dd334317
request-digest: sha256:5e21870b8569720e7fec449a5d8b6308cdc1f633f0d71586cecb8c60cd17690a
```

Major / critical issues only. For each: a concrete failure mechanism and where you confirmed it.
Separate confirmed findings, open domain questions, and residual risks from unavailable
GPU / data / environment.
