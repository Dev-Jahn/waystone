# Review Request — 2026-07-23-013-review-closure

The reviewer has the repository via git. This is a domain/code review, not a workflow audit —
keep the waystone harness out of scope unless asked.

- Project: waystone
- Branch: dev
- Reviewer: codex, gpt-5.5-pro
- Reviewing: 3c411b2cba4ac6e757f768bb094aef737b4151b2   (diff against 45b2fa991633ba03d207cd0b69aae69bca85aeda)

<!-- Keep the Reviewing field on exactly one line with the literal spacing shown above. -->

## What changed and why

직전 리뷰(당신의 2026-07-22 0.13 아키텍처 리뷰, 대상 45b2fa9)의 판정 — "Promote provenance:
CHANGES REQUESTED / Release readiness: 미승인" — 에 대한 폐쇄 round다. diff base가 정확히 당신이
검토한 SHA이므로, 이 diff 전체가 그 리뷰에 대한 응답이다.

- 필수 수정 1: promote의 independent-verify/integration-decision/adversarial-review가 하나의
  EvaluationEvidence digest로 alias되던 것을 제거 — 실제 read-only verifier runner 결과의 typed
  VerifierEvidence, exact-tuple 결속 coordinator IntegrationDecision, lineage-결속
  ReviewCycle+ReviewerEvidence 소비로 대체. accepted-risks → declared_risks 컴파일 배선으로
  risk-gated review가 public run start에서 실제 활성화. `waystone review attach` 최소 public 연결.
  요구한 회귀 3종(동일 actor/artifact 겸직 거부·risk에 reviewer 부재 거부·타 candidate 참조 거부) 포함.
- 필수 수정 2: review disposition/validation의 shape-only 검증을 exact authority 결속으로 —
  objective_ref는 exact commit의 PROJECT_BRIEF fact 재도출, evidence는 CAS/Git authority 실증,
  materialize 전 재검증, append 시그니처에 root 필수화(우회 불가).
- 정합성: 6-role fact를 4-role로 개정하고 owner evidence 기반 재adopt(ADR-0015).
- Release blocker: SHIP_PATHS 정리(legacy 6종 제거·waystone/ 포함)·projected smoke 4종 확장·
  v1 실행 범위("external Codex worker/evaluator 전용") 문서 한정.
- 당신이 요구한 dogfood: 3 시나리오 실측 — S2(confirmed major+accept-risk) PASS, S3(국소 bugfix)
  FAIL로 관료제 우려 실확인(파생 task 등록, 구현은 미착수), S1은 start refusal 불투명성에 차단.
- 부수 수리 2건: promotion reject의 정직한 terminal closeout + retry lineage, store의 0-byte
  WAL/SHM 선제 생성이 유발하던 SQLITE_IOERR_SHORT_READ 근절.

## Read these first

- PROGRESS.md의 `2026-07-23-013-review-closure` 절과 그 아래 w0723/w0723b bullet (커밋 sha 포함)
- docs/reviews/2026-07-22-013-intent-control-plane-feedback.md 말미 triage 표 (당신의 finding별 처분)
- waystone/runs/engine.py — validate_promotion_evidence/validate_promotion_rejection과 promote
  handler 재구성 (필수 수정 1의 본체)
- waystone/reviews/findings.py — validate_*_authority와 root 필수 append (필수 수정 2의 본체)
- docs/adr/ADR-0015-project-brief-role-model-realignment.md + dev_docs/w0723b-brief-adopt-runbook.md
- release-to-main.sh diff (SHIP_PATHS·projected smoke)

## Claims to attack

1. promote의 verifier/reviewer/decision은 이제 artifact·actor 수준에서 실제로 분리되어 있고,
   validate_promotion_evidence는 target-ref-apply 이전에 실행되므로 어떤 경로로도 alias된
   evidence로 promotion을 통과시킬 수 없다.
2. disposition의 fix-now materialize는 존재하지 않거나 stale한 objective로는 불가능하다 —
   "confirmed finding → 가짜 objective 귀속 → task" 경로가 닫혔다.
3. reject는 accept 검증의 약화가 아니라 동일 엄격성의 별도 종결 경로다(exact-tuple reject 검증,
   apply 미실행, seal 전 retry lineage).
4. run-owned candidate의 exact-object 검증(git cat-file 경유)은 위임-worktree 경로의 기존
   불변조건을 약화시키지 않는다(기본값 보존, promote만 명시 opt-out).
5. scaffold는 protocol 필드를 실제 authority에서만 유도하며 의미 필드를 무단 보정하지 않는다.
6. 신규 release projection(SHIP_PATHS + smoke 4종)으로 만든 release는 신 runtime을 온전히
   포함하고 legacy 표면을 기대하지 않는다.

## Evidence already produced (mine — inspect, don't trust)

- full suite 234→258 green(dev 027ec38에서 조합 gate; 각 머지마다 재실행 — PROGRESS bullet의
  단계별 수치). 전 기 산출물에 main 독립 재검증 적용: base RED 재현(disposition 12건 error,
  promote 계약 import 불가), 호스트 suite 재실행, diff 검독.
- 독립 release 리허설: /tmp 클론에서 release-to-main.sh 전체 실행 — 258 gate·projection·smoke
  4종 PASS, main@20359b1 빌드, 실 repo main 불변.
- brief adoption: record sha256:5b516b0a…가 adoption commit fc4c20a의 HEAD:PROJECT_BRIEF.md
  bytes·owner evidence digest/size와 대조 검증됨(runbook의 Python 검사).
- dogfood 로그: /tmp/waystone-w0723b-dogfood.4WXlHV (S1/S2/S3, pre-registered criteria 포함) —
  단 /tmp라 repo 밖이며 세션 밖에서 소실 가능. 판정 요지는 tasks.yaml의 spike result와 PROGRESS.
- store 수리: 수리 전 3/3 재현·수리 후 60 detached runs/240 polls/240 direct reads 무오류
  (scripts/tests/test_run_store_ioerror_stress.py 동봉).

## Known weak spots

- disposition의 decided_by/reported_by binding_digest ↔ frozen profile 대조(당신의 "가능하면"
  항목)는 부분 NO-GO — frozen VerificationPlan이 worker/verifier binding만 보존해 대조할 frozen
  authority가 없다. 후속 설계 필요(chore/decision-actor-principal-binding 연계).
- promotion verifier의 결과 승격이 runner result_summary(pass/fail)를 criterion 결과로 매핑하는
  adapter를 경유한다 — verifier가 실제로 무엇을 검사했는지의 의미 깊이는 runner prompt/binding에
  위임되어 있고 engine은 결속만 강제한다.
- dogfood S1이 차단된 start refusal 불투명성(code만 반환)은 미수리 — fix/run-start-refusal-
  diagnosability로 등록만. S3의 관료제 확인도 수리 미착수(feat/lifecycle-maintenance-path).
- 이 round의 request 자체가 release(0.11.1) round CLI로 생성됨 — migration으로 config의
  review.reviewers가 사라져 기본값(codex, gpt-5.5-pro)이 binding에 동결됐다. 회신 identity가
  불일치해 receipt가 pending으로 남을 수 있으나, finding 채택은 main 독립 검증으로 한다(기존 규범).
- 하루에 8기 codex 발진·머지 — 조합 gate는 green이나 병행 변경 간 의미 상호작용(특히 scaffold ↔
  promote reject 경로)은 각자의 e2e로만 커버됐다.

## Domain lens

trust-kernel 관점으로 봐달라: 이 round의 본질은 "이름의 분리"를 "authority의 분리"로 바꾸는
작업이었다. 특히 ⑴ promote evidence tuple의 결속이 우회 가능한 seam이 남았는지(handler 교체,
resume 재진입, retry lineage 경유), ⑵ disposition authority 재검증이 append 경계 밖(직접 파일
쓰기, materialize 이후 brief 개정)에서 무력화되는지, ⑶ reject terminal이 실패 은닉의 새 통로가
되는지(reject 후 상태로 성공을 위장할 방법), ⑷ release projection이 dev-only 오염 없이 신
runtime을 완결 포함하는지를 공격해달라. 관료제화(당신의 전략 경고)는 S3로 실증됐고 수리 방향
3종이 등록되어 있다 — 그 방향 자체에 이견이 있으면 지금 말해달라.

## Response wanted

Start the reply with this block (replace values; key case/order/spacing and a Markdown fence are
optional; extra keys are preserved). Echo the `Reviewing` target, alone or as a 12–40 hex
`base-target` range, and copy the request digest exactly; missing/damaged values stay unknown, and
no model/target means ordinary prose:
```text
model: codex
effort: high
review-target: 3c411b2cba4ac6e757f768bb094aef737b4151b2
request-digest: sha256:d52a2b8c890bc466c941c0cdea83d6648e989ee4d7e363ee914ef8a3ce5526d0
```

Major / critical issues only. For each: a concrete failure mechanism and where you confirmed it.
Separate confirmed findings, open domain questions, and residual risks from unavailable
GPU / data / environment.
