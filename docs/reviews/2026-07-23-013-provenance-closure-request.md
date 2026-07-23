# Review Request — 2026-07-23-013-provenance-closure

The reviewer has the repository via git. This is a domain/code review, not a workflow audit —
keep the waystone harness out of scope unless asked.

- Project: waystone
- Branch: dev
- Reviewer: codex, gpt-5.5-pro
- Reviewing: e8b236a2bb600ad6c4cd1b36ee341b4a981b8a34   (diff against 3c411b2cba4ac6e757f768bb094aef737b4151b2)

<!-- Keep the Reviewing field on exactly one line with the literal spacing shown above. -->

## What changed and why

당신의 2026-07-23 회신(대상 3c411b2, 판정 CHANGES REQUESTED — Critical 0/Major 3)에 대한 폐쇄
round다. diff base가 정확히 그 SHA이므로 이 diff 전체가 그 판정에 대한 응답이다. 세 major 전부
REAL로 확정(main 독립 라인 추적 + finding당 적대 verifier 검증)한 뒤 같은 날 수리했다.

- WS-GPT-023: evaluate/promote의 외부 verifier/evaluator 모델 호출을 exact candidate
  materialization cwd로 이동 — evaluate는 engine-owned detached materialization
  (.waystone/candidate-contexts), promote는 execute_verifier의 기존 read-only materialization을
  실제 실행 root로 재사용해 외부 실행과 typed verifier를 한 executor 경계로 통합. post-hoc
  pass replay adapter(_publish_promotion_verifier)는 삭제. launch record에 candidate OID·
  materialized-root fingerprint·RunSpec digest 결속, supervisor가 launch 전·프로세스 종료 후
  fail-closed 재검증. content-blind E2E fixture를 cwd 파일을 실제로 읽는 content-aware로 교체,
  당신이 요구한 양방향 경계 회귀(integration↔candidate 상태 교차)를 evaluate·promote 각각 추가.
- WS-GPT-024: integration-decision 이후·target-ref-apply 직전에 verifier/decision/reviewer를
  store/CAS에서 reload한 객체로만 검증·적용(handler 반환값 폐기). execute_stage는
  _execute_stage로 private화해 authority handler 주입 표면 자체를 제거(테스트는 effect/backend
  수준만 주입하도록 재작성). 당신의 필수 negative(unpublished tuple → refusal + target ref 불변)
  포함. RunAssembly 통주입 대신 기존 reload primitive 재사용 — apply_integration_decision의
  private-ref 전용 불변조건(PC-17)은 약화하지 않고 promotion GitRefEffect를 유지했다.
- WS-GPT-025: validate_disposition_authority에 current-objective 검증 추가 — ref commit의
  HEAD ancestry + current committed brief의 동일 fact_id/digest/binding 대조, 불일치만
  `objective-superseded` typed refusal. 당신이 경고한 경계(무관 commit 추가만으로 무효화 금지)를
  회귀로 고정. generic AuthorityResolver는 무수정(historical provenance 성질 보존).
- open Q2: semantic verifier reject를 동일 candidate에 terminal로 — 발행된 semantic evidence의
  존재를 durable 경계로 삼아 process/backend·malformed 실패만 기존 lineage retry 허용.
- 부수: run start refusal envelope에 안전한 detail 채널 + preflight_failed 분류
  (authority/preflight/internal 3부류 기계 구별, unclassified는 예외 타입명만 노출, exit code
  불변) — 직전 round dogfood S1 차단 원인의 수리. README를 실 CLI 표면 대조 기반으로 재작성.
- open Q1은 기존 chore/decision-actor-principal-binding에 승계(OwnerDecisionRequired를 hard
  boundary로 문서화하지 않음), Q3은 ruling으로 selector 확장 안 함(candidate evidence 재평가와
  task materialization 권위는 효과가 다름 — 근거는 triage 표).

## Read these first

- PROGRESS.md의 `2026-07-23-013-provenance-closure` 절 (커밋 sha·검증 방법·incident 포함)
- docs/reviews/2026-07-23-013-review-closure-feedback.md 말미 triage 표 — 당신의 finding별
  검증 증거와 severity ruling(WS-GPT-024에서 내부 verifier의 minor 이견을 기각한 근거 포함)
- waystone/runs/engine.py — _stage_invocation의 candidate context 결속, _reload_promotion_authority,
  _execute_stage(이제 private), semantic reject terminal refusal
- waystone/runs/supervisor.py — RunnerCandidateContext launch 결속·전후 재검증
- waystone/reviews/findings.py — ObjectiveSuperseded와 current-objective 검증
- waystone/runs/transport.py + waystone/cli/run_group.py — failure envelope detail 계약
- scripts/tests/test_run_cli.py의 content-aware fixture와 e2e6, test_promote_evidence.py의
  unpublished-tuple·retry taxonomy 회귀

## Claims to attack

1. 외부 verifier/evaluator의 판단 provenance는 이제 기계적으로 candidate provenance와 일치한다 —
   launch cwd·fingerprint 결속·전후 재검증을 우회해 다른 tree를 보고 낸 판정을 candidate에
   결속시킬 방법이 없다.
2. target-ref-apply는 발행된 store/CAS artifact 없이는 어떤 handler composition으로도 도달
   불가능하다 — _execute_stage 접근·재조립·resume 재진입 어느 경로로도.
3. objective-superseded 검증은 realignment 후 stale ref를 확실히 거부하면서, 무관 commit
   추가만으로는 유효한 ref를 거부하지 않는다(양방향 모두).
4. semantic reject terminal은 실패 위장으로 우회 불가 — verifier가 semantic fail을 process
   failure처럼 보이게 만들어 retry를 얻는 경로가 없다.
5. failure envelope의 detail 채널은 진단 가능성을 주면서 unclassified 경로에서 비밀·경로·스택을
   노출하지 않는다.
6. README의 모든 표면 서술은 현재 코드와 일치한다(과장·미구현 표면 없음).

## Evidence already produced (mine — inspect, don't trust)

- full suite 258→271 green @ 057708a(wave 조합 gate) + README 랜딩 후 271 재확인. 각 머지마다
  표적 게이트 별도 실행.
- 전 task base-RED를 main이 base(078da42) 임시 worktree에서 독립 재현: disposition 신규 회귀
  5/5 FAIL(materialize가 task를 실제 생성하는 결함 출력 포함), transport KeyError 'detail'+cli
  5건, promote_evidence 4건+RunnerCandidateContext ImportError(구조 RED).
- 수리 전 적대 검증: finding당 codex verifier 1기(v023·v024·v025, 반증 시도 각 4-5방향 전부
  실패 기록) + main 라인 추적 — triage 표의 file:line 증거.
- 구현 4기(A/B/C/README)의 보고서에 property별 acceptance 판정·RED 기록(pre-registered 기준
  대비) 보존.

## Known weak spots

- evaluate의 engine-owned candidate materialization은 durable resume 경로라 자동 GC가 없다 —
  완료 run 수만큼 누적(chore/candidate-context-materialization-gc 등록, 미착수).
- WS-GPT-024 수리는 canonical promote 경로의 reload를 강제하지만, reload primitive 자체의
  대상은 여전히 store/CAS 무결성을 신뢰한다 — store 파일의 직접 변조는 위협모델 밖(solo local
  trust domain)이며 당신의 Q1 계보(actor principal binding)와 함께 M2 과제로 남는다.
- semantic reject terminal의 경계는 "발행된 semantic verifier evidence의 존재"다 — verifier
  모델이 결과 발행 전에 죽는 실패는 process failure로 분류되어 retry 가능하다. 발행 직전
  crash를 반복시키는 적대 시나리오는 stochastic이 아니라 가용성 문제로 판단했지만 이견 환영.
- 네트워크 단절로 wave 3기가 동시 사망 후 재발진한 이력이 있다(PROGRESS Incidents) — B는
  기완료분 인수, A는 전체 재실행, C는 dirty 이어받기. C의 RED 일부는 base가 아니라 baseline
  injection 방식으로 확인됐고, main의 base-RED 재현이 이를 보강한다.
- 이 round의 request도 release(0.11.1) round CLI로 생성 — frozen reviewer가 기본값(codex,
  gpt-5.5-pro)으로 동결되는 기지의 한계 지속. 회신 identity 불일치 시 receipt pending은
  정상이며 finding 채택은 main 독립 검증으로 한다.

## Domain lens

trust-kernel 관점을 유지해달라. 이번 round의 본질은 당신이 지적한 두 부등식 —
judgment provenance ≠ candidate provenance, in-memory object ≠ published authority — 을
등식으로 만드는 것이었다. 특히 ⑴ candidate materialization·fingerprint 검증의 TOCTOU 창
(launch 전 검증과 프로세스 실행 사이, 종료 후 검증과 evidence 발행 사이), ⑵ _execute_stage
private화가 Python 관례(name mangling 아님)에 의존하는 정도와 그것이 "봉쇄"로 충분한지,
⑶ reload된 authority와 spec의 exact-tuple 대조에 남은 alias 가능성, ⑷ objective-superseded의
ancestry 검사가 rebase·force-push 후 이력에서 오판정하는 경계, ⑸ envelope detail이 typed
refusal 문자열을 신뢰하는 것의 정보 노출 상한을 공격해달라. release 미승인의 세 사유가
폐쇄됐는지가 이 round의 판정 질문이다.

## Response wanted

Start the reply with this block (replace values; key case/order/spacing and a Markdown fence are
optional; extra keys are preserved). Echo the `Reviewing` target, alone or as a 12–40 hex
`base-target` range, and copy the request digest exactly; missing/damaged values stay unknown, and
no model/target means ordinary prose:
```text
model: codex
effort: high
review-target: e8b236a2bb600ad6c4cd1b36ee341b4a981b8a34
request-digest: sha256:dfb0bb926c290674971e411f7b84cee8501dfa65b827d0815c7afa555341aad3
```

Major / critical issues only. For each: a concrete failure mechanism and where you confirmed it.
Separate confirmed findings, open domain questions, and residual risks from unavailable
GPU / data / environment.
