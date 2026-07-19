모든 쟁점 라인 실측 완료. 교정 20건과 교차 모순 4건을 조정 규칙으로 정리해 최종 대장을 작성한다.

# waystone 설계 전수 감사 — 최종 대장 (교정 반영판)

기준: dev_docs/waystone-0.7-0.9-design.md(승인 baseline) vs dev @ bc71d1b. 교정 20건 반영: ◐/✗→● 상향 6건(갭 대장 제외), ●→◐ 하향 7건, superseded→✗ 재판정 5건(무기록 드롭 확정), ✗→superseded 3건(§8.3), ✗→◐ 1건(§12 L542).

**교정 간 충돌 조정 규칙(실측 근거로 확정)**:
- (조정1) exposure의 `guards: None`·`waivers: []`(overlay.py:583, delegate.py:584)는 미출하 서브시스템의 참값으로 **갭이 아님**(교정 5·6 채택). 단 round exposure가 실패해도 close가 성공하는 best-effort 구조(round.py:271-276 — 주석에 S11/S5 의도 명기, owner 승인 supersede 아님)는 **잔존 갭**(교정 14·§17-4 채택). 즉 불변식#4는 delegation 측 ●, round 측 ◐(유일 갭 = 항상-기록 보장 부재).
- (조정2) SUPERSEDED(이월) 인정 범위 = README.md:232 Next 행 실문구가 명시하는 3요소만: "Promote proven checks to **enforceable guards** with **recorded waivers**, and support **larger parallel task groups**"(실측 재확인). 이 기준으로 maturity 감사의 §6.4 Enforce ✗ 판정은 §5.3 L328과 동일 요소이므로 **이월로 통일**(교정 8과 동일 논리), 반대로 §5.3 L327/L330/L332·§16#14·부록A ADR1은 무기록 드롭 → ✗ 확정(교정 16-20).
- (조정3) §5.3/§7 L329(exposure 완성)는 enforce·waiver 축은 이월(위 문구에 포함), 4층 노출 축은 L327과 함께 무기록 — 이월로 분류하되 caveat 부착.
- (조정4) §3.4 precedence의 supersede 근거는 pre-ADR §7.1(L333-342, profile 축 한정)뿐 — overlay 축의 user/project 분리·narrow-over-broad는 무기록 갭(교정 19·20과 정합). ※ §8-1은 머신 루트 결정으로 무관(실측).
- (부속) 교정 3에 따라 nuisance rate unlabeled-null 관련 서브갭(§16#12, §10 L485-493)은 전부 철회 — 설계 §4.4 L228-231의 의미론 그 자체.

---

## 1. 갭 대장 (◐/✗ 전수, 12군집)

### G1. Role/Routing/Profile 3축 시스템 — 크기 L
설계 §8.2의 6역할×5실행×N백엔드가 2역할×1실행×1백엔드로 축소, §9 라우팅 하네스 전무. 감사 전체에서 가장 넓은 단일 갭.
- §8.2 L435 role 축 ◐ — profile 소비 role은 implementer/verifier 2종뿐 (delegate.py:603-604 `role != "implementer" → WorkflowError "not consumed in M1"`, :249 verifier)
- §8.2 L429 clerk ✗ — grep 0건, binding 소비자·하네스 role 부재
- §8.2 L431 reviewer ◐ — profile 밖 경로: .waystone.yml `review.reviewers` 기본값이 모델 id 문자열 `["codex","gpt-5.5-pro"]` (common.py:1234, review.py:437-449 직접 소비)
- §3.2 L89-93 ◐ [교정15] — 위와 동일 원인: roles-over-model-names 원칙을 reviewer 책임이 위반 (하네스가 행사하는 역할 3종 중 1종이 원칙 밖)
- §8.2 L436 execution 축 ◐ — external-runner 1/5종만 실행 가능 (delegate.py:235-236 fail-loud); clean/forked subagent·deterministic workflow·main-session 미구현
- §8.2 L437 backend 축 ◐ — 스키마는 `<runner>:<model>` 허용하나 실행은 codex 전용 (delegate.py:281-287 "schema-valid but not executable in M1")
- §9 L457-466 8질문 라우팅 프레임 ✗ — 주입 계약·stanza·conventions 모든 표면에서 부재; dev CLAUDE.md는 오히려 모델명 직접 라우팅
- §9 L468 ◐ — 3축 binding이 위 축소에 갇힘; 'capability' 개념 미표현
- §9 L470 ◐ — codex-as-reviewer가 role 축 밖 별도 경로
- §8.1 L420 / §5.2 L306 / §16#9 L657 ◐ [교정11] — 운영계약 4구성 중 routing policy가 정적 binding 나열 1줄 (session_context.py:34-55 `_routing_line`, `_operating_contract`:122-143)
- §13 L564 ◐ — routing heuristic이 machine-readable 정책이 아닌 prose
- §5.2 L305 ◐ — 정적 binding 자체는 존재하나 3축 축소
- §16#6 L654 ◐ — 표의 0.8● 대비 실상 ◐: role 부분·routing 하네스 부재·**budget 분석 전무**

의존: 내부 순서 = 스키마(L1) → 소비자(L2). G4(라우팅 개선 질문)·G12(fan-out형 execution은 scale-up 이월과 경계 명시 필요)와 접점.

### G2. 관측(audit) 렌즈 완성 — 크기 M
설계 §4.3의 9현상 중 2렌즈 부재, 3렌즈 근사. 대체로 기존 데이터 위에 얹는 작업.
- lens#3 worker scope drift ✗ (L193) — exposure의 declared scope vs harness-computed changed_files 대조 렌즈 부재 (improve.py:92-101 lens 목록에 없음). 데이터는 양쪽 다 존재 → S
- lens#9 guard/waiver 마찰 ✗ (L199-200) — warnings.jsonl(overlay.py:408-425)이 쌓이는데 소비 렌즈 없음 → S, G4와 공유
- lens#2 delegation 이득 후보 ◐ (L192) — 발생한 delegation 서술만, 반사실적 후보 신호(task 크기·반복·context 비용 조인) 없음 → M
- lens#4 환경 미준비 반복 실패 ◐ (L194) — error_landscape가 일반 오류 집계에 그침, env_prep 실패·의존성 오류 시그니처 미분리 → S
- lens#8 finding 집중 ◐ (L198) — project·source별만, role/project-area 분해 없음; round↔session 바인딩 unknown(improve.py:1248)이 상류 원인 → M
- §4.2 L182 alias/canonical join primitive ◐ — 서로 다른 slug로 갈라진 논리 프로젝트를 결합하는 명시 alias 부재 → S-M

의존: lens#8 ← round↔session join primitive(L1); lens#9 → G4 환류의 입력.

### G3. Evidence/Review 연결 완성 — 크기 M
§4.6·§5.2 L307의 '정식화'가 verification 축만 배선됨.
- finding 유형 taxonomy ✗ (§4.6 L262) — _parse_triage가 {id, severity, status, task_id}만 반환(improve.py:712-713); 6유형 분류 부재. **하류 전체(재발·§15.1)의 병목**
- 동일유형 재발 ✗ (§4.6 L264) — taxonomy 부재로 차단
- remediation round 부담 ◐ (§4.6 L263) — origin 링크만, 추가 round 계수·reopen 추적 없음
- reviewed SHA 투영 단절 ◐ (§4.6 L265) — review.py:21-22,98-103이 target_sha/base_sha를 마커에 보유하나 improve 투영이 파일 경로만 노출(improve.py:799-807) → S(데이터 존재)
- acceptance-전 해소 ◐ (§4.6 L266) — 현재 task_status 근사, acceptance 시점 바인딩 없음
- route·guard 연결 ◐ (§4.6 L256/268, §5.2 L307 [교정9], §16#5 L653 [교정10], §7 L378-402) — improve의 exposure 소비는 task_id/state/verification뿐(improve.py:902-927), warnings/route 참조 0건

의존: taxonomy(L1 스키마) → 재발 → G9 §15.1; guard 연결 ← G4의 warnings 소비와 동일 데이터.

### G4. 개선 루프 환류(live evidence) 회로 — 크기 M
§4.4 루프의 마지막 두 단계(live evidence 축적 검증 → 다음 cycle 입력)가 미배선. enforce 불요 — warn 수준에서 지금 닫을 수 있음.
- §4.4 L208-224 ◐ [교정12] — warnings.jsonl은 쓰기만 되고(overlay.py:408-425) improve.py·skills/improve/SKILL.md 어디에도 참조 0건(grep 실측 재확인)
- §2.3 L65-66 ◐ — 적용 후 정책의 실질 이득 검증 회로 부재(§7 L401 인과 규율 내에서 발화율·재발 추이로 표현해야 함)
- §2 L68-75 질문4 ◐ — guard 효과 판정은 lens#9+환류로 해소
- §10 L483 same-scope 해소 ◐ 중 friction 환류 부분 — conflict 기록은 되나 다음 improve cycle 입력 경로 미확인
- §10 L505-507 ◐ 중 stale-evidence/re-review 트리거(binding·stack 변경 감지) — 지금 가능 부분(반복 waiver 트리거는 G12)
- §16#3 L651 ◐ — 0.9 '루프 완성'의 warn 수준 잔여분(enforce 종점은 G12)

의존: G2 lens#9, G3 guard join과 데이터 공유. decisions.jsonl(improve.py:1379) 소비 포함.

### G5. 성숙도 모델 기계화 — 크기 S-M (owner 결정 D3 종속)
Bootstrap/Calibrate/Tune이 하네스 계산 상태가 아니라 skill 산문 임계로만 존재.
- §6.1 ◐, §6.2 ◐, §6.3 ◐ — 단계 판정·전이 기록 코드 부재(grep bootstrap/calibrate/tune → improve.py 0건); Tune 임계만 SKILL.md:78-83에 명시, Bootstrap↔Calibrate 경계는 모델 재량
- §6.5 ◐ — readiness gate가 skill 문구+replay 게이트(overlay.py:296-299) 조합; recommendation 생성 gate 없음
- §5.1 L286 ◐, §5.2 L311 ◐, §16#4 L652 ◐(0.7/0.8 부분) — 동일 원인

의존: 없음(독립). 단 Enforce 종점·enforce threshold는 G12.

### G6. Policy layering·precedence — 크기 L (owner 결정 D1a·D1d 선행)
4층 중 실재하는 층은 project-local overlay 1층. 무기록 미결 군집.
- §10 layer3 user/project overlay 분리 ✗, §5.3 L327 ✗ [교정19], §16#11 L659 ◐ — candidate_scope(overlay.py:39)는 라벨만, 승격 미트리거
- §10 layer4 task/round override 층 ✗ — project-single 철학과 무충돌이므로 결정 후 독립 구현 가능(M)
- §10 L483 층간 narrow-over-broad precedence ✗ — 합성 엔진 자체 부재(단일 층)
- §10 layer1 ◐ — base preset이 machine-composable 정책 층이 아닌 정적 문서·절차
- §10 L495-503 ◐ 중 'accepted' 상태 결여 — add=acceptance 접힘(overlay.py:37 DELTA_STATUSES, :238-239) → 설계 개정 R6 후보
- §3.4 L101-108 ◐ [조정4] — overlay 축 precedence 무기록(profile 축 기각만 §7.1 supersede)
- 부록A ADR1 ✗ [교정20] — committed-vs-overlay precedence 미결 ADR

의존: G7 materialization(committed 층 존재)과 상호 의존.

### G7. Materialization·Consent 확대 — 크기 M-L (owner 결정 D1b 선행)
- §14 L586-596 materialization 파이프라인 ✗ — local recommendation→committed project policy 승격 전무
- §5.3 L332 ✗ [교정18], §16#13 L661 ◐(0.9 부분) — managed project agents·project-level hooks의 consent 설치 전무(agents/ 부재, hooks.json은 플러그인 번들 훅)
- §11 L515 ✗ / L516 ✗ / L517 ✗ / L518 ✗ — init에 agent·hook·시작수준·delegation 활성화 질문 없음(skills/init/SKILL.md 실측)
- §11 L519 ◐, L523 ◐, L527 ◐ — 구현된 경로(rec/overlay)에 한해 충족; 공통 consent 프레임 미컴포넌트화
- §5.1 L287 ◐ — 재사용 가능한 consent 프레임 부재(스킬별 산문)
- §14 L595 ◐, L598 ◐ — materialization 부재로 해당 동의·공유 지점 moot

의존: consent 공통 프레임(S-M)은 선행 구현 가능; committed 층은 G6과 연동.

### G8. Guard 규칙 커버리지(warn 수준) — 크기 M
§12가 명명한 탐지 대상 중 2규칙만 출하(overlay.py:48-62 RULES). **enforce 없이 현행 warn machinery로 배선 가능** — 승격만 G12.
- L537 scope 밖 mutation ✗ — G2 lens#3와 동일 데이터(declared scope vs changed_files)로 boundary warn 규칙화 가능
- L539 blind retry ✗ — 사후 audit lens만 존재, live 신호 없음(hook형 필요 소지 → 설계 개정 R5)
- L538 미검증 완료 보고 ◐ — delegation 경로 한정(rule1 + verdict 게이트), 일반 main 주장 미커버(R5)
- L540 delegation artifact 부재 ◐ — 시작된 delegation은 구조적 커버, 'delegation이 요구됐다' 판정 부재
- L542 환경 contract 우회 ◐ [교정7] — worker 측은 구조적 봉쇄(delegate.py:383 workspace-write sandbox+network 차단, :390 UV_CACHE_DIR 격리, :689-696 사전 env prep; dev_docs/0.8.0-m1-implementation-notes.md:23); main-session 측 탐지만 미구현
- L543 고위험 close ◐ — open severe warn만, 독립 review 부재 close 미탐지
- §8.2 L441 ◐ [교정13] / §17-8 ◐ — worker의 scope/SSOT 비확장이 산문뿐 → lens#3+L537 규칙으로 부분 기계화(자기수락 차단은 이미 기계화 ●)

### G9. 지표 집계 계층(§15) — 크기 M
4묶음 모두 원시 피드는 존재하나 명명된 지표(rate·재발률·전후 추이)로 집계하는 코드 전무.
- §15.1 ◐ — taxonomy(G3) 부재 + longitudinal 재발·reopen·post-acceptance 결함 미계산
- §15.2 ◐ — 전후 비교·delegation 완료율·opportunity-adjusted 비율 없음(원시 카운트만)
- §15.3 ◐ — env 실패·ad-hoc mutation·acceptance 재현성 미계산
- §15.4 ◐ — warnings.jsonl/decisions.jsonl 집계기 부재; hard-block/waiver 서브지표는 소스 자체가 이월 arc 종속(G12)
- §2 L68-75 질문6 ◐ — round 축적에 따른 실제 개선 판정 = 이 계층의 산출물

의존: G3 taxonomy → 15.1; G4 소비 → 15.4; longitudinal 저장(스냅샷 간 비교) 신설 필요. 지표별 최초 측정 버전 표기(§15 L637)는 L3 문서 작업.

### G10. CLAUDE.md constitution 분리 '완성' — 크기 S-M (owner 결정 D1c 선행)
- §13 L566 ✗, §5.3 L330 ✗ [교정16], §16#14 L662 ✗ [교정17] — 분리 기반(references/main-contract.md 8줄, 0.8 산출물)은 존재하나 전역 CLAUDE.md 단순화·이관은 빌드도 이월 기록도 없음(README.md:232 실문구·pre-ADR §8 실측으로 확인)
- 부록A ADR2 ◐ — 경계의 0.9 측(기존 CLAUDE.md 정리) 미이행

### G11. 불변식·소형 잔여 — 크기 S
- §17-4 ◐ / §5.2 L309 ◐ [교정14·조정1] — round exposure 항상-기록 보장 부재: round.py:271-276이 예외를 삼키고 "close still succeeded". 수정은 fail-loud화 1건(S) — 불변식#4에서 도출 가능, owner 결정 불요
- §17-5 ◐ — 인과 주장 금지가 산문 규율(provenance 라벨이 뒷받침) — 완전 기계화 불가 → 설계 개정 R3
- §14 L576-577 ◐ — raw content opt-in 메커니즘 부재(현행은 전면 비수집으로 더 보수적) → 설계 개정 R4
- §16 능력 지도 표 자체의 실측 불일치(#6 0.8● 표기 등) — L1 문서 정정

### G12. 이월 arc 종속 갭(작업 없음 — Next 슬롯에서 일괄 해소, §2 대장 참조)
◐ 상태이나 결손분이 전부 owner 기록으로 이월된 항목: §3.5(enforce 단계), §6.5(enforce threshold), §10 L485-493(stage 후보 중 enforce), §10 L495-503('enforced' 상태), §10 L505-507(반복 waiver 트리거), §12 L547(guard waiver — delegate 경로 override provenance는 ● 기존재), §16#12 L660·#15 L663·#3/#4의 0.9 enforce 부분, §15.4의 hard-block/waiver 서브지표, exposure guards/waivers 필드 채움(조정1 — arc 착륙 시 배선 필수).

---

## 2. SUPERSEDED 대장

### A. 이월(carry-over) — Adapt & Enforce arc
근거: README.md:229-232 (v0.9.0 릴리스 커밋 bc71d1b, Next 행 실문구 "Promote proven checks to enforceable guards with recorded waivers, and support larger parallel task groups | Planned") + pre-ADR dev_docs/0.9-pre-adr-storage-lock-autonomy.md §8 L370-381(owner 7건 결정으로 0.9.0 범위를 storage/lock/autonomy로 확정).
- enforce 승격 + waiver/provenance 운영: §5.3 L328, §6.4 L364-366(조정2 — maturity 감사의 ✗를 이월로 통일), §16#12·#15의 0.9 ● 부분, §16#3/#4의 enforce 종점
- 성숙도 arc 종점(Enforce): §5.3 L331
- exposure 완성(enforce·waiver 축): §5.3/§7 L329 — **caveat**: 4층 노출 축은 Next 문구 밖(무기록, G6/D1a 소관)
- scale-up topology 전체: §8.3 L445-447(경로1 orchestrator carrier)·L449(경로2 orchestrator subagent)·L451(선택 조건) [교정8], §5.3 L336, §16#10 L658 — "larger parallel task groups" 문구로 커버; lanes.py는 lane containment 검사만 실재
- §5 intro L274 — '15능력 전부 0.9까지, 아무것도 안 밀림' 명제 자체
- (소형) `waystone context` verb — pre-ADR §8-7 L380 "이월 후보" 명기

### B. 대체(replacement) — pre-ADR 저장·프로필 모델
근거: dev_docs/0.9-pre-adr-storage-lock-autonomy.md §7(L329-366)·§8, owner 승인 "1a 2a 3a 4b 5a 6a 7b"(L372).
- profile 2-레이어(user+project) → **project 단일 기각·확정**: §7.1 L333-342 ("쓸데없이 복잡", "머신 default 레이어는 만들지 않는다", L342 "레이어 필드 불필요") — §3.4·§10·부록A의 **profile 축** 다층에만 적용(overlay 축은 미커버, 조정4)
- improve 분석·산출물 거주지·스코프 → single-project 기본: §7.2 L344-349
- 파생 데이터 거주지 global(`~/.claude/waystone/`) → project-local `.waystone/`: §7.4 L366 "2026-07-13 거주지 결정은 이 절로써 전면 supersede" — 단 local-only 규범 자체는 보존·구현(common.py:118-124)
- Codex 재진입 주입 descope: §8-7 L380

### C. SUPERSEDED 판정이 기각된 것(무기록 드롭 — 갭 대장 편입)
Next 행 실문구·pre-ADR §7/§8/§9 어디에도 근거 위치가 없어 감사 어휘 요건 불충족 [교정16-20]:
- 전역 CLAUDE.md 단순화·이관(§5.3 L330, §16#14) → G10
- managed agents/project hooks consent 설치(§5.3 L332) → G7
- user/project overlay 분리+4층 layering(§5.3 L327) → G6
- committed-vs-overlay precedence ADR(부록A ADR1) → G6/D1d

---

## 3. Top-down 완성 아키텍처 제안

수직절단 대신 "계약을 먼저 완성하고 소비자를 채우는" 3레이어. 기존 태스크 #2/#3/#4에 대응.

### L1 — 스키마·계약 레이어 (태스크 #2)
모든 하류 작업의 형태를 고정하는 층. 소비자가 없어도 스키마·검증·fail-loud를 먼저 완성.
1. **profile 3축 전체 스키마** [G1]: role 6종·execution 5종·backend 문법을 스키마로 선언 + 유효 조합표 + 미구현 조합 fail-loud 유지(현행 delegate.py:281-287 패턴 확장). reviewer를 profile role로 편입하는 스키마(review.reviewers → role 참조; 기존 모델명 목록은 마이그레이션 경로 명시) — §3.2 위반 해소의 계약 측.
2. **routing policy 아티팩트** [G1]: §9 8질문을 machine-readable 정책(질문→role/execution 선호)으로 표현, session_context._operating_contract(:122-143)의 4번째 구성요소로 주입.
3. **finding taxonomy 스키마** [G3]: triage 테이블 계약에 6유형 열 추가(improve._parse_triage 확장 대상 정의). G3 재발·G9 §15.1의 전제.
4. **exposure 계약 확정** [G11·G12]: guards/waivers 필드의 현행 의미(미출하 arc 전까지 null/[]가 참값) 문서화 + **round exposure fail-loud화**(round.py:271-276 — 불변식#4에서 직접 도출).
5. **join primitive** [G2]: 프로젝트 alias/canonical identity(§4.2 L182) + round↔session 바인딩(lens#8·§4.6 L266의 상류).
6. **능력지도·로드맵 정정 문서**: §16 표를 실측으로 갱신(#6 0.8●→◐ 등), §5 intro 명제 폐기, Next arc 범위 명문화 — owner 결정(D1·D2) 결과를 ADR로 기록.

### L2 — 소비자·기능 레이어 (태스크 #3)
1. **backend/execution 소비자** [G1]: `claude:<model>` 러너(우선), 이후 gemini:; verifier/reviewer의 다중 backend; execution 축의 clean/forked subagent·deterministic workflow(단, fan-out 오케스트레이션은 Next arc와 경계 — 단일 delegation의 실행 방식까지만).
2. **role 소비자** [G1]: review.py가 profile reviewer binding을 소비; clerk role 정의+저모호성 잡무 경로.
3. **관측 렌즈 5건** [G2]: scope-drift, warn-friction(lens#9), delegation-opportunity, env-unpreparedness 분리, role/area 분해.
4. **evidence 연결 4건** [G3]: reviewed SHA 투영(S), taxonomy 소비+재발 계산, route/guard join, acceptance 시점 바인딩.
5. **환류 회로** [G4]: warnings.jsonl/decisions.jsonl을 improve 입력으로 소비 → 질문4 답변·after-apply 추이 리포트(§7 L401 규율: 발화율·재발 추이만, 인과 주장 금지).
6. **guard warn 규칙 확장** [G8]: scope-out-mutation, env-bypass(main-session 측), 고위험 close 일반화 — 현행 overlay RULES(overlay.py:48-62)에 warn stage로 추가(enforce 불요).
7. **성숙도 machine-state** [G5, D3 결정 시] / **consent 공통 프레임+materialization** [G7, D1b 결정 시] / **layer4 override 층·precedence** [G6, D1a·D1d 결정 시].

### L3 — 문서·지표 레이어 (태스크 #4)
1. **지표 집계기** [G9]: §15 4묶음을 명명된 지표로 산출(longitudinal 저장 신설, 지표별 최초 측정 버전 표기 — §15 L637). waiver/hard-block 서브지표는 Next arc 착륙 후.
2. **CLAUDE.md 이관** [G10, D1c 결정 시]: repo 배포물 측 정리 + 사용자 전역 CLAUDE.md 단순화 가이드.
3. README/conventions/능력지도 정합 유지, G12 이월 목록을 Next 슬롯 스펙 초안으로 봉인.

### 설계 개정이 필요한 지점 (설계 원문과의 충돌 — 구현으로 닫을 수 없는 것)
- **R1** §5 intro L274·§16 표 0.9 열: 실측과 불일치 — pre-ADR·README가 사실상 개정했으므로 baseline 문서 자체를 정정(L1-6).
- **R2** §10 4층 layering: pre-ADR §7.1의 project-single 철학과 충돌 — D1a 결정에 따라 §10을 개정(overlay user 승격 유지/축소/폐기).
- **R3** §17-5(인과 주장 금지)·§17-8(scope/SSOT 비확장): 모델 발화·행위의 완전 기계 강제는 불가능 — 강제 수단을 '기록·감사가능성 + 렌즈/guard 근사'로 명문화.
- **R4** §14 L576-577 opt-in: 현행(전면 비수집)이 규범보다 보수적 — '기본 비수집, 필요 시 opt-in 추가 가능'으로 개정하면 기능 추가 없이 정합.
- **R5** §12 guard '탐지'의 수단: boundary warn으로 충족되는 항목 vs hook형 실시간 개입이 필요한 항목(L538 일반화, L539 blind-retry)을 설계에 구분 명시 — 후자는 마찰 비용이 커서 D2 범위 논의에 포함.
- **R6** §10 lifecycle 'accepted': add=acceptance 접힘(overlay.py:238-239)을 다이어그램에 반영하거나 상태 추가 — 현행이 더 단순하므로 개정 권고.
- **R7** §4.4 nuisance unlabeled-null이 설계 정합임을 §16 표 각주로 명시(교정 3 — 라벨 소스는 waiver arc 착륙 후 생김).

의존 요약: D1/D2/D3(아래) → L1 → L2(G1 소비자·G3 재발·G4 환류는 L1 스키마 종속; G2 lens#3·G8 L537은 상호 데이터 공유로 동시 진행 가능) → L3(G9는 G3·G4 산출물 종속).

## 4. Owner 결정 필요 항목 (설계·헌장·기존 결정으로 도출 불가한 것만)

**D1. 무기록 드롭 4건의 처분** — 0.9 재범위 때 이월 기록 없이 남은 것들. 각각 유지(Next 편입)/개정/폐기 중 택일:
- (a) overlay user/project 분리 + 4층 layering + 층간 precedence (§5.3 L327, §10 L478-483, §16#11) — pre-ADR §7.1의 project-single 철학과의 정합 방향 포함 (R2 연동)
- (b) committed project policy materialization + managed agents/project hooks의 consent 설치 (§14 L586-596, §11 L515-518, §5.3 L332, §16#13)
- (c) 전역 CLAUDE.md 단순화·이관 (§13 L566, §5.3 L330, §16#14, 부록A ADR2)
- (d) committed-vs-overlay precedence ADR (부록A ADR1) — (a)(b) 결정에 종속, 둘 다 폐기면 자동 소멸

**D2. Next(Adapt & Enforce) 슬롯 확정** — README.md:232에 '무엇'(enforce·waiver·scale-up)은 기록됐으나 버전 번호·정확한 범위가 미정: D1 편입분 포함 여부, §12 실시간 guard의 수단(boundary warn 한정 vs hook 확장, R5), `waystone context` verb(pre-ADR §8-7 이월 후보) 포함 여부.

**D3. 성숙도 단계의 구현 형태** — §6이 단계 모델을 규범으로 두나, machine-state 구현(G5)과 skill-prose 유지+설계 개정(§3.6 분업 해석) 둘 다 헌장과 양립 — 어느 쪽인지.

(그 외 후보였던 round exposure fail-loud화·reviewer profile 편입·alias primitive는 각각 불변식#4·§8.2+§3.2·§4.2에서 직접 도출 가능하므로 owner 결정 불요 — L1 배정.)
---

## 5. 지휘 결정 기록 (main session, 2026-07-15 — owner 지시 "설계의 모든 요소를 포괄하도록 완벽하게, top-down으로")

owner의 포괄 지시가 D1을 실질 해소한다고 해석한다: **무기록 드롭은 드롭이 아니라 전부 구현 대상이다.**

- **D1a 채택**: overlay user/project 분리 메커니즘 + task/round override 층 + narrow-over-broad precedence를 구현한다. pre-ADR §7.1(profile project-단일)과 충돌하지 않음 — 그 supersede는 profile 축 한정으로 기록돼 있고, overlay의 user 승격은 설계 §10 원문이 요구하는 evidence-gated 메커니즘이다(조기 분리 ceremony 금지 원칙 유지 — 승격 트리거만 구현, 강제 분리 없음).
- **D1b 채택**: materialization 파이프라인 + 공통 consent 프레임 + managed agents/project hooks의 consent 설치를 구현한다.
- **D1c 채택**: 전역 CLAUDE.md 단순화·이관 경로를 구현한다(§13 L566 — waystone이 짧은 constitution + machine-readable 정책 분리를 생성·안내).
- **D1d(ADR1) 초안 채택**: precedence = 층간 narrow-over-broad(task/round > project > user > base), 동일-scope 충돌은 설계 원문의 least-restrictive resolution + 기록. committed project policy와 local overlay가 같은 scope에서 충돌하면 **committed가 이기고 overlay는 shadowed로 가시 표기**(조용한 무시 금지). owner 거부권 유보 — 릴리스 전 확인 항목.
- **D2**: Next(Adapt & Enforce) 슬롯은 README 기록대로 유지 — enforce 승격·waiver 운영·scale-up topology·`waystone context`는 이번 범위 밖. 그 외 전부(이 문서 G1-G11)는 이번에 닫는다. §12 실시간 guard 중 hook형이 필요한 항목(L538 일반화, L539)은 R5에 따라 설계 개정으로 boundary-warn 수단을 명시하고 hook형은 Next로.
- **D3**: 성숙도 단계는 machine-state로 구현(결정론적 카운트 판정은 §3.6 분업상 script 몫; 전이 기록은 exposure 정신과 일치).

구현 레이어는 §3 제안 그대로: L1(스키마·계약) → L2(소비자·기능) → L3(문서·지표). 설계 개정 R1-R7은 L1과 함께 baseline 문서에 반영한다.

### §5 추가 지휘 결정 (L2-A 리뷰 F7 판정, 2026-07-15)

**execution 축의 host-실행 모드(clean/forked-subagent, deterministic-workflow, main-session) 소비 방식 확정:** waystone은 호스트의 subagent 실행을 소유할 수 없다(플러그인은 CLI+스킬 — Task-tool 스폰은 호스트 몫). 또한 headless CLI로 clean-subagent를 흉내 내는 것은 정의상 external-runner와 동일 기계라 별도 소비자가 아니다. 따라서 host-실행 모드의 소비자는 3면으로 구성한다:
1. **routing 계약 주입** (L1 완료) — main session이 세션 시작 시 binding을 지도로 받는다.
2. **스킬 레벨 소비** (→ **L3 범위에 명시 편입**) — delegate/round/improve 스킬이 "이 role 작업은 profile binding의 execution/backend를 따라 라우팅하라"를 규범 절차로 지시(예: clerk 작업은 binding이 정한 모델의 clean subagent로; implementer가 clean-subagent로 바인딩되면 delegate run 대신 host 위임 후 round 기록).
3. **관측 귀속** (L2-B lens#8) — trace의 agent 분류가 role 어휘로 집계되어 binding 준수가 사후 관찰 가능.
`delegate run`은 host-실행 binding에 대해 fail-loud하되 메시지가 위 경로를 안내한다. clerk 러너는 만들지 않는다. 이 판정은 설계 개정 R3의 일부로 baseline에 반영한다(§8.2에 '실행 주체' 열 추가).
