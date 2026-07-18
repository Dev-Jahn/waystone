# PROGRESS

round 단위 작업 이력이 이 파일에 축적된다. 활성 task와 의존성은 `tasks.yaml`(CLI: `waystone task`)과 생성 파일 `ROADMAP.md` 참조.

## 2026-07-19-evidence-authority

- **Goal**: generation-binding 3차 리뷰(CHANGES REQUESTED — major 4: JW-GPT-007~010)를 전량 해소 — "완료" 판정을 지탱하는 증거의 권위를 가변 로컬 상태에서 불변·재파생 가능한 원천으로 이전.
- **Shipped** (전부 implementer=external-runner/codex:gpt-5.6-sol xhigh; 매 attempt 실측, 설계 회전 적대 리뷰(xhigh)·재량 0 델타는 main 직접 검증 — 근거는 verdict artifacts):
  - fix/receipt-read-time-rederive (007) — receipt 권위를 reply-metadata cache → verbatim body 읽기 시점 재파생으로 이전(cache는 진단 전용, legacy 경로 포함 cache 단독 신뢰 0). 손상 격리(한 receipt가 pending/improve/overlay를 중단 불가), 사유 taxonomy 구분(envelope/불일치/sidecar 부재), bounded 리더 복원(CRLF 정확 산술), overlay 패리티 (attempt-3 인수 @ 4af545d; 적대 2회전이 major 6·minor 7 적출)
  - fix/pr-cycle-generation-binding (008) — cycle marker·freeze sidecar를 digest 필수 v2 schema로 승격, 투영은 cycle 증거가 명명한 generation만 조회(latest 채택 금지), marker 단독 복구, v1/v2 혼재는 위양성 conflict가 아닌 정직한 skew, cycle 번호는 신뢰 operator만 계상 (attempt-2 인수 @ a2ff310)
  - fix/round-mint-anchor-immutable (009) — round 기존성 앵커에서 mutable PROGRESS heading 제거(검증된 immutable exposure 단독) — 문서 한 줄 편집으로 과거-dated round를 mint해 v1 legacy 창을 여는 경로 폐쇄, 익일 확장 무회귀 (attempt-1 인수 @ b3354c8)
  - fix/probe-proof-principal-binding (010) — fingerprint에 실행 principal(euid/gid/groups)·codex config root·Linux best-effort process context 축 추가(공유 checkout의 타 사용자 적중 차단), not-observed는 상태 동등 대조(macOS probe-once 유지, 비대칭 전이만 재프로브) (attempt-2 인수 @ 69036c1)
- **리뷰 finding 처분**: 007~010 전량 폐쇄. 컨테이너/namespace 심층 축·로컬 단일-파일 변조의 강한 변형(append-only/원격 store)은 decision/trust-threat-model-boundary로 위양(사용자 ruling 대기, 비차단).
- **Gates**: live 777→804 green(lane별 병합 후 재실측; P-lane은 codex 부재 이중 게이트) + ruff clean.
- **SSOT**: unchanged.
- **Decisions pending**: decision/trust-threat-model-boundary(위협모델 경계) · chore/pre-header-feedback-settlement(역사 정착).
- **Review**: requested (docs/reviews/2026-07-19-evidence-authority-request.md) — 4차 검토.
- **4차 회신 (2026-07-19, codex:gpt-5.6-sol 대체 리뷰 — 사용자 지시)**: 007·009 resolved, 008→JW-GPT-011 재개, 010→JW-GPT-013 still-broken, 신규 JW-GPT-012 — 3건 전부 main 검증으로 REAL major 확증(근거: feedback triage 표). task 등록은 사용자 지시로 보류 후 0.11.1 수정 라인으로 확정.
- **Release 0.11.0 (2026-07-19)**: 사용자 승인으로 **known-issues 명시 릴리스** — JW-GPT-011(혼합버전 PR cycle digest 오귀속)·012(freeze sidecar 손상 시 이전 cycle explicit 승격)·013(config 내용 변경에 probe proof 재사용)은 0.10 대비 퇴행이 아닌 미완 하드닝이며(각 표면은 0.11이 엄격히 강함), 수정은 0.11.1(round-5)로. 이 머신 dogfooding 개시(하네스 0.10.0→0.11.0).
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개).
- **Next**: 릴리스 0.11 → /plugin update → verifier binding 복원 → round-5(011~013 수정) → carrier 라이브 검증 → migration-sunset. 0.12 refactor plan은 사용자 추가 리뷰 대기(동결). minor 큐 별도.

## 2026-07-18-generation-binding

- **Goal**: carrier-lanes-fixes 재리뷰(gpt-5.6-pro/xhigh, CHANGES REQUESTED — major 4)를 전량 해소: request generation 정체성 결속과 프로브 증명의 runtime 결속.
- **Shipped** (전부 implementer=external-runner/codex:gpt-5.6-sol xhigh; 매 attempt 호스트 스위트+ruff 실측, 설계 회전엔 raw codex 적대 리뷰(xhigh)·재량 0 기계 델타엔 main 직접 검증 — 근거는 각 delegation record verdict artifact):
  - fix/binding-schema-v2-digests — binding-2 schema: narrative+rendered request digest 필수(부재=corrupt), digest-capability를 round 날짜 cutoff(> 2026-07-18)로 sidecar 밖 앵커(다운그레이드 위장 차단, same-day 정품 v1 보호), 비실재 날짜 corrupt(packet·PR 패리티), 게이트·freeze의 stored rendered digest 대조+byte-동일 게시, round close 신규 mint 현재일 검증(기존 round 익일 확장 허용) (attempt-5 인수 @ a8f208a; attempt-4가 verdict 인수 후 병렬 병합 base drift로 hash-동일 재적용 — 같은 파일 lane 순차 병합 교훈 기록)
  - fix/probe-proof-runtime-fingerprint — 프로브 증명 마커를 versioned JSON fingerprint 계약으로: host identity(machine-id 정규형/IOPlatformUUID)·resolved codex 경로+stat·stdout version(stderr 기록 전용)·sandbox 관측 정직화·mount 정체성, exact-match만 생략+축별 안내, 전 실행 표면 resolved 경로 통일, codex 부재 CI-동등 스위트 green (attempt-4 인수 @ 84c1293)
  - fix/reprepare-generation-atomicity — reprepare를 binding 선발행 fail-closed 순서로(어느 지점 중단도 구 완료 은닉 불가), pending이 request·narrative의 최신 binding 재현성 확인 (attempt-1 인수 @ 79f241e)
  - fix/reply-narrative-echo — request가 자기 rendered digest 노출(sentinel 순환 해소), 회신 request-digest 에코가 도장 근거(echo-era 라운드는 에코 필수, 폴백은 legacy 한정), receipt는 명명된 generation 일관+읽기 시점 재대조, stale/미상/no-echo 구분 (attempt-3 인수 @ cc5ae84) — JW-GPT-004 메커니즘 A 폐쇄
- **리뷰 finding 처분**: JW-GPT-004(A+B)·005·006·Q1 전량 폐쇄. 반박·이관 판정은 각 verdict artifact에 기록 — 유한 잔여 2건(전환일 다운그레이드 창·정적 stderr-only shim 아래 child 교체)은 코드 주석 문서화 + chore/pre-header-feedback-settlement 확장 예정.
- **Gates**: live 전체 스위트 748→777 green(각 lane 병합 후 재실측, F-lane은 codex 부재 PATH 이중 게이트) + ruff F401/F841 clean. 0.10.0 ingest가 남긴 중복 v1 sidecar 1건 제거(계약 동일, canonical 보존).
- **SSOT**: unchanged.
- **Decisions pending**: chore/pre-header-feedback-settlement(역사 라운드 정착 방식 — 전환일 잔여 흡수 확장 포함).
- **Review**: requested (docs/reviews/2026-07-18-generation-binding-request.md) — 3차 검토.
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개).
- **Next**: 3차 리뷰 통과 시 릴리스 0.11 → /plugin update → hooks 마커 → verifier binding 복원 → carrier 라이브 검증 → migration-sunset. minor 큐: marker-diagnostics-polish · shallow-ancestry-honesty · hook-matrix-color-env · legacy-name-residue(사용자 지시).

## 2026-07-18-carrier-lanes-fixes

- **Goal**: carrier-lanes 리뷰(gpt-5.6-pro/xhigh, CHANGES REQUESTED)의 잔여 major 3건을 병렬 lane으로 해소하고 재검토 요청 — 신뢰 표면(프로브 증명·narrative 결속·publication 증명) 경화.
- **Shipped** (lane 3건은 implementer=external-runner/codex:gpt-5.6-sol xhigh, 각 attempt마다 host 스위트+ruff 실측 + raw codex 적대 리뷰(xhigh) + main verdict — 전 회전 근거는 delegation record의 verdict artifact):
  - fix/probe-proof-machine-scope — codex 프로브 증명을 커밋 추적 `.waystone.yml`에서 미커밋 per-checkout 마커(`.waystone/codex-runner-verified`)로 이동 (attempt-3 인수 @ 874c4c3, 위임 20260717T200543Z). 3회전 경화: 복원적·원자적 self-ignore(migration 경로 통일, symlink 불수용) · 마커는 untracked+계약값일 때만 증명(추적된 마커는 무시+untrack 안내+재프로브) · legacy 키 무시+제거 안내(source-aware) · barrier 게이팅 동시성 계약(lock 하 재확인 자체를 고정)
  - fix/binding-narrative-digest — binding contract에 canonical narrative digest 결속 (attempt-3 인수 @ 874c4c3, 위임 20260717T200543Z). narrative만 바뀐 재-prepare가 새 sidecar 발행+pending 재개(RED) · freeze/packet 게이트 모두 exposure+stored narrative 재렌더 대조(request.md 변조 거부 RED) · legacy digestless는 양측-digestless일 때만 legacy-pre-digest 라벨 폴백(digest-strip 위장 차단 RED), publication 게이트는 digest 하드 요구 · improve 투영에 digest·전용 narrative_coverage_reason 보존
  - fix/remote-verify-live-ref — publication 증명을 stale remote-tracking ref 신뢰에서 라이브 원격 증명으로 재작성 (attempt-2 인수 @ 4673b58, 위임 20260717T205101Z). 정확한 upstream branch를 명시 refspec으로 pid+uuid 전용 임시 ref에 fetch해 원자 고정(공유 FETCH_HEAD 미사용, cleanup 실패도 fail-closed) · persisted refs/remotes/* 신뢰를 remote verify·head_pushed 양쪽에서 제거(RED: 삭제된 upstream·refspec 제외 — 실 bare-remote, clone 분리) · 부재 vs 네트워크 구분(ls-remote --exit-code) · remote '.' 거부 · freeze binding↔exposure 교차검증을 packet 게이트와 동등화. criterion 9 잔여(shallow 경계 rc=1 오진단, fail-closed 방향)는 override-unmet 기록 + fix/shallow-ancestry-honesty 등록
  - docs/readme-delegate-fold — README delegate 섹션을 리드+`<details>` fold(전체 lifecycle·명령 지도·run 7단계·verify/verdict/apply/discard 계약)로 재작성 @ 07bab60 (main-session; dev_docs/delegate_readme.md 스타일 참조, 최신 표면 재검증)
- **신규 등록(이번 라운드 발견, 미착수)**: fix/reply-narrative-echo (major — ingest가 현재 binding digest를 도장하는 재-prepare race, reply 헤더 계약 변경 필요·사용자 ruling 대기) · fix/marker-diagnostics-polish · fix/shallow-ancestry-honesty · fix/hook-matrix-color-env (FORCE_COLOR env에서 스위트 1건 오염 — main tree 재현으로 lane 회귀 아님 판별) · chore/legacy-name-residue (JW/jahns-workflow 구명 잔재, 사용자 지시) · chore/pre-header-feedback-settlement (0.10 헤더 이전 역사 feedback 3라운드 영구 pending — 기존 부채)
- **Gates**: 병합 전체 스위트 730→748 green (2회: 874c4c3에서 740, 4673b58에서 748; FORCE_COLOR 중화 실측) + ruff F401/F841 clean. 각 lane worktree 개별 실측 green.
- **SSOT**: unchanged.
- **Decisions pending**: fix/reply-narrative-echo (reply 계약 변경 ruling) · chore/pre-header-feedback-settlement (역사 라운드 정착 방식).
- **Review**: requested (docs/reviews/2026-07-18-carrier-lanes-fixes-request.md) — carrier-lanes 재검토.
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개).
- **Next**: 재리뷰 통과 시 릴리스 0.11 → /plugin update → hooks 마커 → verifier binding 복원(TEMP UNBOUND 해제) → carrier 라이브 fan-out 검증. 이후 chore/migration-sunset(전 머신 이관 확인 후)·minor 큐(diagnostics polish·color env·name residue).

## 2026-07-18-carrier-lanes

- **Goal**: fix-wave 리뷰 큐를 3-lane 병렬 체인(R: review.py / D: delegate.py / A: release)으로 전량 해소하고, deterministic-workflow carrier(CC Workflow 통합)를 편입.
- **Shipped** (lane 항목은 전부 implementer=external-runner/codex:gpt-5.6-sol, 각 건 raw codex 적대 리뷰 + main-session agent_checks로 인수):
  - **R lane**: fix/publication-gate-direct-binding — ancestry 추론 전삭제, 단일 명제(원격 ref가 closeout SHA + byte-identical sidecar 보유) 직접 증명 · fix/reply-header-parser-simplification — 헤더 블록 한정 단일 decode 규칙 · feat/deterministic-review-packet — script 렌더링 + freeze 재렌더(3차) · feat/review-pending-ledger — pending 파생 전용(저장 없음) · feat/waystone-statusline — 파생 1줄, consent 설치
  - **D lane**: fix/drop-codex-companion — companion transport 전면 삭제, verify=codex exec 단일화 (ruling 2026-07-17) · fix/execution-surface-dep-gating — done 의존 게이트 · fix/preflight-probe-isolation + fix/probe-once-config-gate — 프로브 격리·1회 실행 · fix/verifier-transport-hardening — timeout/signal/빈 출력 정직 보고 · chore/hook-matrix-normal-mode-coverage — 양 모드 mutation-kill
  - **A lane**: fix/release-checked-out-main · chore/release-script-hardening — env-allowlist smoke, TMPDIR guard, fail-loud manifest
  - **Carrier**: decision/deterministic-workflow-carrier-semantics — ADR-0001 사용자 비준(2026-07-18) · feat/delegate-fanout-cli-contract + feat/deterministic-workflow-carrier-contract + feat/delegate-fanout-workflow-template — 14b0cff로 구현·merge, 비준으로 의존 충족 후 인수(e9e5c14)
  - **기타**: chore/overengineering-prune-batch1 (감사 batch 1) · fix/cli-uninitialized-root-gate (Fable subagent, lock chokepoint) · fix/review-feedback-triage-discipline (마커 섹션 + 읽기 시점 재계산) · decision/lanes-verify-round-scope · docs/readme-staleness-sweep — README 표면·배지(543→719)·ADR-0001 SSOT 포인터 정정 + docs gate 3표면 확장 (main-session, Workflow carrier 분석)
  - **Dropped 7건**: publication-gate-bypasses·reply-header-residuals·codex-exec-verifier-hardening(각 direct-binding·parser-simplification·transport-hardening으로 재설계 대체), companion 계열 2건(제거로 obsolete), verifier-guard-residuals-2, task-cli-arg-validation(strict options로 흡수)
- **Gates**: 전체 테스트 600→719 green (112s, 2026-07-18 2회 실측) + ruff F401/F841 clean. docs gate에 신규 CLI 표면 3종 편입.
- **SSOT**: unchanged; ADR-0001 ratified (2026-07-18).
- **Decisions pending**: none.
- **Review**: requested (docs/reviews/2026-07-18-carrier-lanes-request.md).
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개).
- **Next**: 릴리스 0.11 사용자 지시 대기 — 릴리스 후 /plugin update → hooks 마커 설치 → verifier binding 복원 → 라이브 검증. chore/migration-sunset은 전 머신 0.11+ 이관 확인 후 착수.

## 2026-07-16-fix-wave

- **Goal**: 첫 라운드 리뷰 지적 2건 + 현장 dogfooding 보고 3건(사용자·bw2·spark1) + 회신 프로토콜 재설계를 최대 병렬 위임으로 해소.
- **Shipped** (전부 implementer=external-runner/codex:gpt-5.6-sol, 각 건 raw codex 적대 리뷰 + main-session agent_checks로 인수):
  - fix/release-staging-isolation — 릴리스를 temp-index 투영 + positive SHIP manifest로 재작성 (ignored 로컬 파일 보존, 실패 원상; 리뷰어 지적)
  - fix/verifier-hook-hermeticity — verifier 세션에서 전 hook hermetic (ruling: 전 hook no-op)
  - fix/round-packet-remote-visibility — packet publication 게이트(push된 HEAD에 request+binding 실존), 엄격 Reviewing 파서 (2차, waiver 1건)
  - feat/effort-pro-ultra → fix/effort-drop-pro — ultra 추가 후 pro는 실측(자기선언 응답)으로 제거
  - feat/task-status-parked — 6번째 상태 parked (newton 요구)
  - fix/runner-env-failure-detection — 빈-성공 오분류 fail-loud + 프리플라이트 프로브 (spark1 AppArmor 사고)
  - feat/review-reply-structured-header — 회신 머리 key:value 블록(model/effort/review-target), robust 파싱, 회신-내장 결속 증거 (2차 + prefix 12+ 상향 84ad6a7)
  - decision/verifier-hook-isolation-contract (ruling: hermetic), decision/release-ship-manifest (ruling: positive manifest)
- **Gates**: 전체 테스트 558→600, 모든 apply 후 dev 게이트 green + ruff clean. 위임 2회 기각 후 재계약 재시도 2건(packet, header), 사용자 escalation 2건 기록.
- **SSOT**: unchanged.
- **Decisions pending**: none.
- **Review**: requested (docs/reviews/2026-07-16-fix-wave-request.md).
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개).
- **Next**: 대기 큐 10건(major 5: publication 우회, companion effort, 의존 게이팅, 프로브 격리, 헤더 잔여 + feat 3: 결정론 packet, pending ledger, statusline + chore 2). 릴리스(0.11)는 사용자 지시 대기 — 릴리스 스크립트는 이제 로컬 파일 안전.

## 2026-07-16-adopt-dogfooding

- **Goal**: waystone이 자기 자신의 개발 하네스가 되는 첫 사이클 — 채택 bootstrap + 첫 dogfooding finding 2건의 위임 수정.
- **Shipped**:
  - docs/adopt-waystone-harness — 자기채택 bootstrap: SSOT.md(ideate 합성), init(packet 리뷰·warn-allowed·delegation on), ADR-0000, 부산물 release EXCLUDES, 3축 profile 6-role binding (done; main-session)
  - fix/boundary-hook-cli-resolution — boundary hook을 plugin hooks.json으로 이동(마커 게이트·비차단·레거시 감지) (done; implementer=external-runner/codex:gpt-5.6-sol, 1차 discard 후 2차 apply)
  - fix/verify-worktree-self-contamination — verifier 세션에서 waystone session hook이 worktree에 상태를 시딩하던 결함을 WAYSTONE_VERIFIER_SESSION guard로 차단 (done; implementer=external-runner/codex:gpt-5.6-sol, 네트워크 실패 1회 discard 후 apply)
- **Gates**: 전체 테스트 558→562 OK + ruff F401,F841 clean (인수 verdict의 agent_checks; 커밋 bb8484c·9076ec8). main 누출 0 — release EXCLUDES tree-hash 시뮬레이션으로 증명 (4f8ddbc).
- **SSOT**: 신규 작성 (§1-§7) + ADR-0000 ratified.
- **Decisions pending**: none.
- **Review**: requested (docs/reviews/2026-07-16-adopt-dogfooding-request.md).
- **Adaptive rules**: unevaluable (활성 overlay 규칙 0개 — bootstrap 단계).
- **Next**: 릴리스로 고친 hook·guard를 설치본에 반영 → verifier binding 복원 + boundary hook enable로 라이브 검증. chore/verifier-session-guard-hardening 처리. 이후 Adapt & Enforce arc.

## waystone 채택 이전 이력 (요약, 2026-06-11 ~ 2026-07-16)

waystone 채택(ADR-0000) 이전의 개발은 round 구조 없이 진행됐다. 상세는 `dev` 브랜치 git log와 `dev_docs/` 설계·구현 노트(gitignored) 참조.

- **v0.1–v0.2 (2026-06):** 플러그인 생성(당시 이름 jahns-workflow). SHA-bound 리뷰 사이클, 결정론적 merge gate 등 correctness kernel을 7차례 외부 GPT 리뷰로 경화.
- **v0.3–v0.6 (2026-06 하순~07초):** 리뷰 번들 도입 후 단일 markdown 요청으로 단순화(v0.4.1), task registry CLI + archive(v0.5), ideate 스킬 + SSOT.md 표준화(v0.6). dev/main 분리, `release-to-main.sh`, marketplace CI 자동 sync 확립.
- **v0.7 Observe & Advise (2026-07-13):** 세션 로그 trace + audit 렌즈 + evidence 기반 improve 권고.
- **v0.8 Delegate & Verify (2026-07-14):** 격리 worktree delegation primitive, artifact contract, overlay/replay/warn, 독립 verify. v0.8.1에서 waystone으로 개명, v0.8.3 Codex 호스트 지원.
- **v0.9 Unify & Automate (2026-07-15):** cross-host 저장 통합(`{root}/.waystone` + `~/.waystone`), flock locking, delegate 자율화(verdict 게이트).
- **v0.10 Bind & Compose (2026-07-16):** 설계 완전성 arc — 213요소 전수 감사 후 role 3축 소비 완성, policy 4층 합성, guard 4규칙, longitudinal metrics, verdict digest 체인. 558 tests. 최종 인수 합격 후 릴리스.
