# Review Request — 2026-07-20-review-remediation

The reviewer has the repository via git. This is a domain/code review, not a workflow audit —
keep the waystone harness out of scope unless asked.

- Project: waystone
- Branch: dev
- Reviewer: codex:gpt-5.6-sol
- Reviewing: ad26225341e993cecb3dfc705bdda6ce88e3ff18   (diff against 1f7d942b418025296704ff5bac4a13ac54d00ca5)

<!-- Keep the Reviewing field on exactly one line with the literal spacing shown above. -->

## What changed and why

이전 두 round packet(fleet-fix-wave·ruling-execution)에 대한 적대 리뷰 회신 13 finding을 전량
처리한 수리 라운드다. 핵심 변경: ① ADR-0014 **Amendment 2**(known-debt 분리·suite identity
pin·M1-A 순수 기계 ruling·I-10 경계) + **Addendum**(w4 작업이 발견한 현행 I-10 위반 — rendered
worker prompt가 registry 표면 5종을 전달 — 을 known-debt로 편입, 수리는 M1-B 소유). ② 코드
수리 5건: 역사 아카이브 reader dual-prefix 복원(21건 소거 회귀), review binding generation
충돌 fail-closed 3종, migration sunset detector fail-open 2건(preserved profile 분기·symlink
container), linked read의 lock-이전 canonical 정규화(잔재 생성 0). ③ I-10 특성화 테스트
(pinned-debt 형태: TASK_BLOCK exact equality + template SHA-256 전문 oracle). ④ 문서 3건:
M1-C comparator exit supersession, 승격 계약 85클래스 양방향 폐쇄 + PC-31, traceability
matrix 재생성 + ADR-0013 fault 의무 3건 등록. 라운드 말미에 미해소 blocker 0을 확인하고
M1-A 착수를 승인했다(decision/m1a-start-approval — 사용자 위임 조건 집행).

## Read these first

1. `docs/adr/ADR-0014-m1a-acceptance-basis.md` — Amendment 2와 Addendum(신규 절 전부)
2. `docs/reviews/2026-07-20-fleet-fix-wave-feedback.md`·`docs/reviews/2026-07-20-ruling-execution-feedback.md` — 원 finding과 triage(verdict·verifier 요지·task 결속)
3. `PROGRESS.md` 최신 절(2026-07-20-review-remediation)
4. `docs/meta/agent-reports-2026-07-20/w4-*.md` — 기별 보고서(RED/GREEN 재현 명령 원문 포함, w4-i10.md는 NO-GO 경위)
5. `docs/promoted-contracts.md` — PC-31과 ledger reverse closure 표
6. `scripts/review.py`(binding selector)·`scripts/common.py`(sunset detector)·`scripts/tasks.py`(canonical read 정규화) 해당 구획

## Claims to attack

1. Amendment 2 Addendum의 처분이 옳다 — 현행 I-10 위반을 M1-A 전에 고치지 않고 known-debt로
   pin하는 것이 "순수 기계 단계" ruling과 정합하며, pinned-debt 특성화(exact pin + 부재 단언 +
   template 전문 oracle)가 기계 이동 중 신규 유출 0을 실제로 보장한다.
2. binding generation 충돌 수리가 PC-10을 완전히 닫았다 — glob이 흡수하는 비정규 이름 전수가
   fail-closed이며, 정상 재발행(-2 유일 최신)과 settled 3건의 기존 판정이 불변이다.
3. sunset detector 수리 2건이 원본 무수정(no-follow·읽기 전용)을 유지하면서 fail-open을 닫았다
   — 특히 preserved profile raw-bytes 비교가 "정상 완료 이관은 분기 profile을 만들 수 없다"는
   전제에 정당하게 기댄다.
4. linked read 정규화의 canonical 증명(재탐침 + decoy 방어)이 우회 불가다 — 특히
   common_dir.parent가 canonical checkout이 아닌 배치에서 typed 거부로 떨어진다.
5. M1-A 착수 판정의 전제(미해소 blocker 0)가 실제로 성립한다 — 등록된 이월 task들(M1-B 수리·
   M1 편입 2건)이 blocker로 남았어야 할 것을 강등한 것이 아니다.

## Evidence already produced (mine — inspect, don't trust)

- 기별 보고서(`docs/meta/agent-reports-2026-07-20/w4-*.md`)의 VERIFIED 절: RED→GREEN 재현
  명령 원문과 rc. i10-v2는 RED 증명 3종(금지 문자열 주입·pinned 표면 제거·중복 투영).
- w4-i10 NO-GO의 main 독립 재현(direct render로 5 표면 PRESENT 확인) — Addendum 본문에 경로
  인용, 재현 명령은 w4-i10.md.
- 머지별 표적 게이트 rc=0 ×6, 최종 full gate 830 tests rc=0 (817→830 증분 내역은 PROGRESS).
- docs 검증 스크립트: M1-C supersession byte 복원 대조 rc=0, 85클래스 exact-set 폐쇄 rc=0,
  matrix AST 대조 stale=0 (명령 원문은 w4-docs.md).

## Known weak spots

- i10 특성화의 template SHA-256 hard-pin은 의도적 brittleness다(모든 템플릿 변경이 명시적
  테스트 개정을 강제) — M1-A 동결 중에는 원하는 성질이지만, M1-B에서 debt 수리 시 테스트
  개정 절차가 Addendum에 명문화돼 있지는 않다.
- linked read 정규화는 canonical checkout이 `common_dir.parent`인 표준 배치만 증명 가능 —
  분리 git-dir 배치는 전부 typed 거부로 떨어진다(fail-closed이나 UX 제약).
- 1차 발사 5기가 네트워크 장애로 격추돼 잔존 작업을 감사·승계하는 비정형 경로를 탔다 —
  각 보고서의 승계 판단 절이 그 감사 기록이며, 잔존물 무단 신뢰는 없었다고 주장한다(검증 대상).
- 역사 pending 리뷰 7건은 이 라운드 범위 밖(3건은 사용자 ruling 대기, decision/legacy-settlement-additional-cohort).

## Domain lens

리뷰 판정 기계(binding·settlement)는 fail-closed 설계 관례를 따른다 — 모호성은 pending/
unknown으로, 임의 tiebreak 금지. migration sunset은 읽기 전용 typed 거부만 허용(자동 수리
금지, ADR-0013 symlink 계약). I-10/Amendment 2의 known-debt 기계는 "M1-A는 동작 무변경,
수리는 소유 마일스톤"이라는 단계별 gate 원칙의 적용례다. 위임 worker prompt는 목표·경계·
판정 요청만 담아야 하며(I-10), 내부 bookkeeping 표면 전달은 부채로 관리된다.

## Response wanted

Start the reply with this block (replace values; key case/order/spacing and a Markdown fence are
optional; extra keys are preserved). Echo the `Reviewing` target, alone or as a 12–40 hex
`base-target` range, and copy the request digest exactly; missing/damaged values stay unknown, and
no model/target means ordinary prose:
```text
model: codex:gpt-5.6-sol
effort: high
review-target: ad26225341e993cecb3dfc705bdda6ce88e3ff18
request-digest: sha256:2b0781dd5c1ca34200e75ec96d38b36dc9b77bfcce29d1afe1c9c6eebe9cd344
```

Major / critical issues only. For each: a concrete failure mechanism and where you confirmed it.
Separate confirmed findings, open domain questions, and residual risks from unavailable
GPU / data / environment.
