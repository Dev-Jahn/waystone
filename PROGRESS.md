# PROGRESS

round 단위 작업 이력이 이 파일에 축적된다. 활성 task와 의존성은 `tasks.yaml`(CLI: `waystone task`)과 생성 파일 `ROADMAP.md` 참조.

## 2026-07-19-m0-contracts

- **Goal**: 0.12 M0-A 마감(baseline 동결)과 M0-B 산출물 전량 착지 — ruling 6건 확정 + ADR 7종 + 불변조건 확정본.
- **Shipped**:
  - **M0-A 완료** — `gate/trust-baseline-tag`: **`baseline/0.12-refactor` @ 7cfecd3**(annotated). exit 3조건 충족: ⑴ 미해결 trust major 2건이 하네스 사용 표면 밖임을 **실측**(review.mode=packet · freeze sidecar 0개 · `ingest_round_binding`이 packet 분기에서 freeze glob 이전 조기 반환 · 014 경로는 `--pr` 필수) ⑵ `docs/known-issues.md`에 영향 범위·게이트 무영향 근거 문서화 ⑶ feature freeze를 tag 메시지에 선언. 게이트 828 green + ruff clean 후 부착.
  - **ruling 6건 전부 확정** — ①배포브랜치(main=전체소스/dist=배포, M4 전환) ②**위협모델=우발적 손상만**(의도적 로컬 변조·adversarial filename·다중사용자 경계는 명시적 비보호; crafted filename 전제 finding은 REAL이어도 수용 잔여로 분류) ③round→run(내부 canonical, /round는 1.0까지 alias) ④SQLite 채택+DB는 project-local 기본·미지원 FS는 typed refusal ⑤**delivery 기본 commit**(사용자 결정 — 권고 manual에서 상향, automation level은 설정 조정·init이 묻지 않음) ⑥타 머신 resume 미지원 명시.
  - **ADR-0002~0008** (@ cf1073c, 2e06f71) — effect commit protocol(효과 7종 표+관측채널 불가 열+recovery 결정표) · run observability & cancellation(3분리·derived health·**취소는 의도만 기록, running/alive/unknown-effect는 삭제 금지**·supervisor identity) · executor 경계 · fact authority matrix · closeout manifest(add-only CAS·자기참조 금지·**deep audit는 로컬 artifact store 요구**=사용자 결정 b) · SQLite 운영 · terminology.
  - **`docs/invariants.md`** — I-01~12 + E-01~09 확정본(집·검증층 4열). E-불변조건 문구는 계획서에서 축자 이관, **기계 대조로 9건 전부 존재·표류 0 확인**.
- **Gates**: 828 green + ruff clean — lane별 1회 + 병합 후 1회, 전부 main 주관측(rc 직접 캡처). **코드 변경 0건**(문서·레지스트리만).
- **계획 r4→r5**: 외부 리뷰 3차(설계 승인, 필수 2+정밀화 8) 반영 — 수용 9/조정 1/기각 0. 신설: **§3-9 취소·정지·정리 안전 계약**(리뷰 최대 기여 — record.lock이 *우연히* 제공하던 "실행 중 cleanup 차단"이 lock 분해와 함께 사라진다는 지적), E-08 양방향 정밀화, `stalled`를 FSM state→derived health, liveness를 job 단위 계산 후 집계, progress 분모를 frozen closure로, status/watch 엄격 read-only, §3-10 process supervision·identity. **조정 1건**: M0-A 순서 — 리뷰는 `014·015 폐쇄→0.11.2→tag`를 권고했으나 리뷰어가 알 수 없던 두 사실(round-6 발산 결과, 두 결함이 PR-mode 한정이고 dogfooding은 packet mode)로 **0.11.1을 baseline으로 tag**하고 해소는 M1-B 수용 기준에 편입.
- **신규 finding**: `fix/probe-machine-axis-hostname-drift`(minor) — probe fingerprint의 `machine` 축이 hostname(`Mac.local`)이라 네트워크/DHCP 변경 시 증명 무효화·매번 재프로브. 같은 marker에 안정적 `host_identity`(IOPlatformUUID)가 이미 존재. E-09 계열(ambient 값을 신원 축으로 사용)이나 **E-09 문구는 파일시스템 메타데이터에 한정돼 이 사례를 못 잡는다**(리뷰 패킷 R-8).
- **조정자 실수 1건**: `git merge --squash`가 커밋 없는 lane 브랜치에 대해 **오류 없이 "no changes"로 성공** — `ls`로 확인하지 않았다면 산출물 0개인 채 "병합 완료"로 보고했을 것. 조용한 실패의 전형이며 리뷰 패킷 R-2의 실사례.
- **SSOT**: unchanged.
- **Review**: `docs/reviews/2026-07-19-residual-after-0.12-request.md` — **설계 공백 리뷰**(코드 리뷰 아님). 이번 세션의 실패·실수 중 0.12가 설계대로 전부 구현되어도 남는 것 8건(R-1~R-8)과 메타 질문(계획이 엔진은 촘촘히 설계했으나 조정자는 거의 설계하지 않았다 — 이것이 불변 지향점 ②와 양립하는가). 리뷰어 = chatgpt:gpt-5.6-pro(사용자 지정).
- **Next**: M0-C characterization + porting ledger(등급 배정) + runtime-state 처분 감사 → M0 exit review → M1-A 승인. 리뷰 회신은 M0-C 착수 전 반영이 바람직(무엇을 "보존할 동작"으로 고정하느냐가 R-5·R-6과 직결).

## 2026-07-19-supersession-attribution-attempts

- **Goal**: 5차 리뷰의 REAL major 2건(JW-GPT-014 merge 관측-기록 불일치 / JW-GPT-015 foreign malformed sidecar가 healthy round ingest 차단) 해소.
- **결과: 미착지 — 두 lane 모두 4회전 후 중단, 병합 0건. dev는 03ba429 그대로.** 라운드 산출물은 수리가 아니라 (a) ruling 1건 확정, (b) 하네스 결함 2건 발굴, (c) 0.12 구조 교체 필요성의 실측 증거다.
- **회전 궤적** (전부 implementer=external-runner/codex:gpt-5.6-sol xhigh, 매 attempt main이 게이트 재실측 + RED 독립 재현 + codex 적대 리뷰):
  - **014**: a1 major2(cycle_conflict 조기반환·비권위 로컬정책으로 영구 증거 기록) → a2 major2(approve 우회·순차 fan-out 부분상태) → a3 major3(freeze 우회·과잉 demotion·보상삭제 비원자) → **a4 major4** — a4는 구조적으로 옳은 방향(단일 chokepoint `classify_remote_review` + 원자적 단일 관측 레코드)이었으나 신규 결함 3건 유입. **결정적**: `marker_valid`는 v2의 base_sha/reviewers를 선택 필드로 허용(review.py:2007-2010)하는데 `_validated_demotion_target`은 필수 요구(1350-1352) → 정상 축약형 v2가 supersede되면 **모든 online 명령이 차단되는 위양성**. 병합 시 원 결함보다 악화. 변경 규모 149→908줄, 폐쇄율 < 유입율.
  - **015**: a1 major2 → a2 major3(phantom owner 세탁·fan-out 오염·**단조성 위반**: 새 라운드의 평범한 artifact 추가만으로 기존 corruption이 사라짐) → a3 **main 직접 기각**(단조성을 `st_mtime_ns` 순서에 결속 — git clone/checkout이 mtime을 재작성하므로 다중 머신에서 보장 소멸; round-5 JW-GPT-013 교훈 재발) → a4 major3. a4 진전: `unattributable` 1급 상태 도입, 공용 resolver로 improve 99줄 순감, E-09 준수 + mtime 불변 테스트. 미해결: unattributable 행을 최단 prefix bucket에 배정해 무관 round 오염·실제 owner의 stale 승격, 이중 glob 매칭 파일이 두 round 귀속, 외부 review-requests 상충 generation에서 ingest/improve 판정 분기.
- **구조 진단**: 014 = 관측 지점을 열거해도 다음 게 나옴(status→merge→approve→freeze→base-policy 분기) — 필요한 건 분류 경로가 하나뿐인 구조. 015 = round id가 `-freeze-`를 포함 가능한 한 파일명 귀속은 원리적으로 다의적. 둘 다 **fact당 authority가 하나여야 한다**는 0.12 M1 명제의 실증 사례.
- **dev 잔존 위험(정직한 기술)**: merge gate는 여전히 skew를 차단하므로 **게이트 우회 경로 없음**. 실제 피해는 offline 증거 투영(`improve` 리포트·offline ingest)이 online 판정과 어긋나 무효화된 리뷰 세대를 explicit으로 표시할 수 있는 것 — 오해 유발, 게이트 무영향. 두 task는 status=blocked, notes에 시도 이력·진단 기록.
- **Gates**: 각 attempt lane에서 전체 스위트 green 재실측(014 a4: 840 OK / 015 a4: 832 OK, 양쪽 ruff clean) — 게이트는 통과했으나 적대 리뷰가 잡은 결함은 테스트 없는 동작이었다. 병합하지 않았으므로 dev 스위트는 828 불변.
- **Ruling 확정**: decision/trust-threat-model-boundary — **우발적 손상만 방어**(크래시·부분 쓰기·도구 버그·정상 운영 중 이름 충돌). 의도적 로컬 파일 조작·adversarial filename·다중 사용자 권한 경계는 **명시적 비보호**. 근거: 로컬 쓰기 권한자는 scripts/*.py 직접 수정이라는 더 쉬운 경로가 있음. 적용: crafted filename 전제 finding은 REAL이어도 수용된 잔여로 분류하고 재론하지 않음. M4 SECURITY.md에 사용자용 명시.
- **신규 하네스 결함(리뷰 아닌 dogfooding 산출)**: fix/delegate-env-prep-uv-cache(major — 위임 러너 worktree 캐시에 pyyaml·ruff 부재로 러너가 자기 게이트·RED를 못 돌림; 이번 8회전 전부에서 재현) · fix/registry-worktree-misroute-guard(major — 잔류 cwd로 registry 변경이 linked worktree에 기록되고 pre-0.9 migration 유발, 2회 발생).
- **0.12 plan r3→r4**: 사용자 지시로 **실행 관측 설계 1급 편입**(§3-8 liveness/progress/current 3분리 + honest-unknown, **E-08** 침묵은 종료의 증거가 아님, `run watch`, budget 3행) — 근거는 이 라운드의 실사고(네트워크 전환 시 main session이 러너 생사를 판정 못해 살아있는 delegation을 discard 시도, record lock이 저지). 추가로 **E-09**(신뢰·귀속 판정을 파일시스템 메타데이터에 결속 금지) 신설 — 같은 실수 2회 재발(round-5 디렉터리 stat, round-6 mtime)로 개인 규율에서 불변조건으로 승격.
- **조정자(main) 자기 결함 3건**: acceptance 조항이 기각 사유를 직접 유발 — ① "improve와 동일 규칙으로 일원화"(그 규칙의 안전성 미검증) ② "explicit 되살아나는 경로 없음"(디렉터리 전체 unwritable에서 원리적 불가) ③ "**모든** 식별 가능한 v2 contract를 demote"(cycle 범위 누락 → 과잉 demotion 결함 유발). 조항을 구현 지시에서 **성질(property) 기술**로 바꾼 회전에서 양 lane 모두 구조적 해법으로 전환 — 다음 라운드부터 성질 기술을 기본으로.
- **5차 리뷰 receipt**: **UNBOUND(pending)** — raw 리뷰 프롬프트가 회신 헤더를 손으로 작성하며 full SHA 오기 + request-digest 누락. 하네스가 E-01대로 거부(설계대로 동작). finding은 리뷰어 attestation이 아닌 main의 독립 코드 검증으로 채택. 레시피 교정은 메모리 기록.
- **SSOT**: unchanged.
- **Next**: 0.12 M0로 이동 — M0-A 잔여(baseline tag·feature freeze) → M0-B(ruling 6건 중 ②확정, 나머지 5건 + ADR 세트) → M0-C. 014/015는 M1에서 구조적으로 재검토. 0.11.2는 내지 않으며 0.11.1이 현행 릴리스.

## 2026-07-19-evidence-authority-fixes

- **Goal**: 4차 리뷰(codex 대체, REAL major 3: JW-GPT-011~013) 전량 해소 — 0.11.1 hotfix 라인. 부수 목표: 0.11.0 probe 자기-churn 실사고(delegate run 전면 불가) 해소.
- **운영 특기**: 0.11.0 하네스의 probe 결함(013과 동일 근원)으로 `delegate run` 발사가 불가 → 전 lane을 **raw codex exec 우회**(사용자 규칙: 하네스 버그는 raw 우회 + finding 기록). 검증은 lane별 게이트 주관측 재실측 + RED 독립 재현 + codex 적대 리뷰(xhigh)로 delegation 파이프라인과 동등 규율 유지.
- **Shipped** (전부 raw codex gpt-5.6-sol xhigh; 순차 병합 013→011→012):
  - fix/probe-config-content-binding (013) — fingerprint를 config.toml 내용 digest에 결속(디렉터리 stat은 진단 강등), absent 상태 동등·읽기 실패 fail-toward-probe·digest-only 저장. probe 자기-churn 실사고 해소 포함 (attempt-1 인수 @ 1cbff2a; 적대 리뷰 ACCEPT-WITH-NOTES major 0)
  - fix/pr-cycle-v1-supersession-honesty (011) — same-cycle later-v1이 v2 digest 권위를 강등(3면 동일 규칙·tie/미파싱 fail-closed), completion·merge gate가 skew 사유 소비, mixed-host demotion sidecar 영속화, freeze `--round` 필수화(신형 CLI v1 발행 금지·capability 탐지 제거), post-cutoff v1은 digest-era 차단 (attempt-3 인수 @ ae2e8e3; 적대 회전 REJECT2→REJECT1→ACCEPT-WITH-NOTES0)
  - fix/freeze-sidecar-corruption-isolation (012) — corrupt freeze sidecar를 sentinel 보존(filename↔content identity 대조), 최신 cycle 손상 시 round honest-unknown(이전 cycle explicit 승격 금지), cross-round prefix 충돌 foreign-skip(improve/ingest 판정 일치) (attempt-2 인수 @ fc70ce1; REJECT1→기계 델타 main 직접 검증 — 비례성 규칙 기록)
- **Gates**: live 807→828 green(매 attempt·매 병합 주관측 재실측, rc 직접 캡처) + ruff clean. RED 전 attempt 독립 재현.
- **수용 residual(비차단)**: digest-era-v1-freeze 사유의 demotion 미영속(감사 parity, gate 무영향) · demotion 쓰기 동시 writer TOCTOU(단일 사용자 모델) · crafted demotion fail-closed DoS · unreadable rename 위조(filename=identity 계약) — 뒤 둘은 decision/trust-threat-model-boundary 범위.
- **부수 사고 기록**: ① registry 조작 1회가 잔류 cwd로 worktree에서 실행돼 worktree tasks.yaml 오염+pre-0.9 profile seed — 즉시 원복, "registry 조작은 main repo에서만" 성문화. ② 0.10.0 dedup 버그의 중복 binding sidecar 재발행 2회 수동 제거(0.11.1 후 재발 여부가 dogfooding 검증 포인트).
- **SSOT**: unchanged.
- **Next**: 5차 재검토 요청 게시 → 0.11.1 릴리스(`release-to-main.sh`) → `/plugin update` → delegate run 복구 확인 → 구식 pending 표기(historic 5건+evidence-authority binding-unavailable) 0.11.1 판독으로 재확인. 0.12 refactor plan r2는 사용자 리뷰 대기.

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
