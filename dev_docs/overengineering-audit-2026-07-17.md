# 오버엔지니어링 전수조사 — 2026-07-17

## 판정 렌즈 (사용자 ruling, 2026-07-17)

> 오버엔지니어링은 "명확히 문제가 발생할 수 있지만 적당히 눈감고 넘어가라"가 **아니다**.
> **애초에 그렇게 구현을 안 했으면 발생할 일도 없는 문제를 누더기 기우듯 틀어막는 것**이다.
> 금 간 항아리를 테이프로 막지 말고, **새 항아리로 바꾸면 문제 클래스가 소멸하는지부터** 확인하라.

따라서 이 문서의 단위는 개별 방어 장치가 아니라 **부위(subsystem)별 반창고 밀도**다.
개별 장치의 "corner case 확률" 논쟁은 부차적이고, 핵심 질문은 부위마다 하나다:
**이 방어들이 지키는 문제가, 다른 구조를 택하면 존재 자체가 사라지는가?**

기본 원칙 (그대로 유효):
- 모델의 착각·환각·실수를 결정론적으로 차단하는 장치 = waystone의 존재 이유 → 기본 정당.
- 실제 발생 이력(field report, 리뷰 재현 probe)이 있는 방어 → 정당.
- 로컬 공격자(자기 repo를 스스로 조작) 방어 → 무의미 (그 사용자는 게이트 코드를 직접 고칠 수 있음).
- 참고용 기록에 붙는 무결성 기계 → 쓰기 규율 + 읽기 시점 재계산으로 대체.

## 방법

- Workflow 16 agents (scan 8클러스터 → 클러스터별 반박 검증), opus/high. 후보 42건.
- 검증자는 각 후보에 "실은 load-bearing"임을 입증 시도 (git 이력·task origin·리뷰 재현 여부 대조).
- 최종 판정·새 항아리 분석은 main session이 이번 세션의 기각 이력(반창고가 쌓인 과정을 직접 목격)과 결합해 수행.
- 한계: 검증자 판정은 "그 장치 단독" 기준이라, load-bearing 판정이라도 새 항아리로 클래스가 소멸하면 함께 사라질 수 있음(각 부위에 명시).

집계: over-engineering 12 / borderline 19 / load-bearing 11 (총 42).

---

## 부위 1 — 리뷰 발행 게이트 (review.py + fix/publication-gate-bypasses 편입 기준) 🔴 최고 밀도

**반창고 이력**: 원 목적은 steno 사건(발행 안 된 packet sha를 모델이 지어냄) 차단. 이후 R1 기각(5 우회)
→ R2 기각(blocker 4) → 3차 기준 총 17건까지 증식. 증식분 대부분이 "ancestry를 걸어서 발행 여부를
추론"하는 현 구조가 만드는 우회 표면을 하나씩 틀어막은 것.

| 발견 | 판정 | 요지 |
|---|---|---|
| accept: refs/replace/graft 무효화 + 시작 시 SHA 핀 | **over-eng** | 로컬 공격자 전용 시나리오. 재현 이력 없음. 자기 repo에 replace ref를 심는 사용자는 게이트 코드도 고칠 수 있음 |
| accept: negative-refspec stale ref 배제 + tag 섀도잉 | **over-eng** | 자기유발 config라야 성립. 실재했던 건 '삭제된 upstream stale ref'뿐이고 그건 이미 fetch+is_ancestor로 fail-closed |
| accept: merge 부모 트릭 방어(first-parent 체인 강제) | **over-eng** | 자연 linear 워크플로에서 발생 불가. **기준 #3(binding의 closeout 직접 결속)이 채택되면 조상 기반 우회 표면 자체가 소멸** |
| accept: reviews-tree 열거 bounded + oversized suffix 거부 | **over-eng** | binding-N은 라운드당 한 자리 수. binding-999999999.json이 생길 정상 경로 없음 |
| accept: skip-worktree/assume-unchanged 위장 차단 | borderline | 리뷰어가 실제 재현. 단, HEAD-tree를 증거 소스로 쓰면(이미 R2 방향) working-tree 위장 클래스가 통째로 무관해짐 |
| 사이드카 -2/-3 충돌 루프 + fail-closed 최신 선택 | borderline | 핵심(발행 시점 target_sha 불변 기록)은 정당, 충돌 번호 기계는 과잉 |
| codex_signals_at_head 신선도 + 동시각 fail-closed | load-bearing | 리뷰 blocker 재현 이력. 유지 |

**새 항아리**: 있음 — **"조상 추론"을 버리고 "직접 결속"으로.**
게이트가 증명해야 할 명제는 딱 하나다: *"이 packet이 주장하는 closeout SHA가 원격에 실제로 발행되어
있고, packet 파일(request/binding)이 그 커밋 트리에 있다"*. binding 사이드카가 closeout SHA를 직접
기록하고, 게이트는 (a) 그 SHA가 remote-tracking ref에 containment, (b) 그 커밋 트리에서 request/binding
바이트 대조 — 이 두 가지만 검증하면 merge topology·first-parent·replace-ref·working-tree 위장은
**검사할 필요가 있는 대상 자체가 아니게 된다.** R2가 이미 이 방향(HEAD-tree 증거 소스, binding 직접
결속)을 절반쯤 갔고, 기각 사유의 태반은 나머지 절반(ancestry 잔재)에서 나왔다.

**권고**: 3차 위임을 현행 17개 기준으로 발진하지 말 것. 기준을 "직접 결속" 설계로 재작성
(예상 결과: 기준 수 17 → ~6, 코드도 순감). ※ 3차 발진은 이 문서 검토 뒤로 보류해 둠.

---

## 부위 2 — 리뷰 회신 메타데이터 (review.py 헤더 파싱 + improve/overlay projection) 🟠 해소 진행 중

**반창고 이력**: 저장 boolean 신뢰 → 스냅샷 → 체크섬 봉인(2회 기각, blocker 누적 9건) →
**사용자 ruling으로 새 항아리 교체 완료** (마커 섹션 + `review triage` script 교체 + 읽기 시점 재계산;
fix/review-feedback-triage-discipline, 검증 진행 중). 봉인 반창고 클래스는 소멸.

audit이 찾은 잔여 반창고 (교체 후에도 남는 것):

| 발견 | 판정 | 요지 |
|---|---|---|
| parse_review_reply_header의 corner 기계 (byte cap·줄별 UnicodeDecodeError 복구·중복 키 특례·surrogate 거부) | **over-eng** | 모든 분기의 종착지가 어차피 "None → not-configured". 통 UTF-8 decode 실패 = 헤더 없음 취급 한 줄이면 동일 결과 |
| read_feedback_reply_metadata의 자기 재검증 (방금 자기가 쓴 파일의 필드별 type 검사·재정규화) | **over-eng** | 도구가 자기 산출물을 적대적으로 재검증. 참고 기록 렌즈로 과잉 |
| improve._review_sha_binding 호환 wrapper | **over-eng** | **죽은 코드** — 호출자가 자기 테스트 2곳뿐. 삭제 |
| 32KiB cap 자체 | 유지 | 경계 처리는 이미 landed, 저비용 |

**권고**: triage-discipline 착지 후 후속 1건으로 위 3건 일괄 제거 (파서 단순화 + dead wrapper 삭제).

---

## 부위 3 — 위임 프리플라이트 프로브 + stderr 분류기 (delegate.py) 🟠

**반창고 이력**: spark1 field report(실재) → 사후 분류기 + 사전 프로브(같은 커밋, 같은 실패 모드에
벨트+멜빵) → 프로브 격리 기각 2회(분류 어휘·기록 내구성·stale 정리) → "실용 최소" ruling으로 어휘/정직성만
반영, 분류기는 heuristic hint로 격하 문서화(landed @ 5e0b290).

| 발견 | 판정 | 요지 |
|---|---|---|
| 사전 프로브 vs 사후 분류기 중복 | borderline | 실패 모드는 실재. 프로브의 유일 가치 = xhigh 장기 실행(수십 분) 전 fail-fast인데, **delegation마다 실제 codex 세션 1회를 소모**. 사후 conjunctive 검출은 무료 |
| stderr 분류기 완전성 추구 | 종결 | ruling으로 heuristic hint 격하. 더 벼리지 않음 |
| 프로브 증거 chmod-000 전용 테스트 요구 | borderline | record_dir은 방금 자기가 만든 자기 소유 디렉터리 — 제3자 chmod 경로 없음 |

**새 항아리 질문 (사용자 결정 필요)**: 프로브를 유지할 가치가 있는가?
비용 = 위임마다 codex 세션 1회(지연+과금). 편익 = 깨진 sandbox 환경에서 xhigh 본 실행(10~40분)
낭비 방지. **대안: 프로브를 기본 off로 하고, 사후 검출기가 failed-env를 기록한 머신에서만 다음
실행부터 켜는 것**(한 번 실패한 환경에서만 유료 보험) — 코드 순감 없이 비용만 줄이는 절충.

---

## 부위 4 — delegate 무결성 사슬 (packet/verdict/apply/cleanup) 🟡

| 발견 | 판정 | 요지 |
|---|---|---|
| discard 재개 시 원래 --reason 문자열 정확 재현 강제 | **over-eng** | 감사용 자유 노트의 정확 일치를 안전장치 취급. 크래시 복구에 마찰만 추가. **제거** |
| packet 3중 검사 (prepare 다이제스트 → claim 완전동등 → claim 다이제스트 재도출) | 축소 | claim쪽 다이제스트 재도출은 앞 두 검사에 포함됨. 1개 제거 |
| _cleanup 3중 사후조건 (lexists+list파싱+show-ref) | borderline | 세 residue가 독립이라는 반박도 성립. 저비용 — 유지 가능 |
| apply 시점 contract/patch 재해싱 | load-bearing | "적용되는 바이트 = 평결된 바이트"를 잇는 유일한 고리. 유지 |
| verify용 result-tree 재도출 대조 | borderline | git diff→apply 왕복이 완전 무손실은 아님(rename/mode 에지). 저비용 — 유지 |
| verifier worktree manifest (ignored까지 해싱) | **load-bearing** | **실전에서 2회 발화**(verify 자기오염 사건 — gitignored `.waystone/` 시딩은 git status로 안 보임). mtime_ns 필드만 제거 여지 |

---

## 부위 5 — registry-core / legacy migration (common.py) 🟡 sunset 후보

symlink 거부·resumable 마커·byte-exact 스냅샷 전부 리뷰 blocker(F1-F8) 대응 이력이 있는
load-bearing 판정. **그러나 새 항아리 질문이 상위에 있다**: 이 서브시스템 전체가 "pre-0.9 레이아웃
→ 현행 레이아웃" 1회성 이관용이고, 사용자는 1명이다. **모든 머신이 이관을 마친 시점에 마커·재개·
자동 repair 상태 기계 전체를 삭제**하는 sunset task를 등록해 두는 것이 정답 (개별 장치 다듬기가 아니라).

기타: `_unique_path` .2~.9999 루프(축소 — 상한 100이면 충분), lock holder 신원 재구성 진단(유지 —
CC/Codex 2호스트 경합은 설계된 상태), round close의 SSOT copytree 백업/복원(부분 축소 여지).

---

## 부위 6 — release-to-main.sh + hooks 🟢 대체로 건강

| 발견 | 판정 | 요지 |
|---|---|---|
| session_context._routing_block 전수 스키마/순서/개수 재검증 | **over-eng** | 자기 repo의 정적 문서를 매 세션 재검증. 발생 이력 없음. 축소 |
| checked-out-main 3중 확인 + same-value CAS | load-bearing | 리뷰어가 실제 재현한 사건(HEAD 이동) 기반. 유지 |
| TMPDIR common-dir/전체 worktree 가드 | borderline | 자기 작성 추정적 경화(이번 세션 A lane). 이미 landed, 저비용 — 유지하되 전례로 삼지 않기 |
| resume atomic rename-claim, cleanup 결과 플래그 | load-bearing | 유지 |

---

## 부위 7 — 계약 문서 / 템플릿 🟡

| 발견 | 판정 | 요지 |
|---|---|---|
| delegate SKILL "매 위임마다 8개 정책 질문을 순서대로 walk" | **over-eng** | 의식(ritual)이 된 체크리스트 — 라우팅 실수를 잡은 이력 없음. 요약 1줄 + 애매할 때만 참조로 완화 |
| fanout 템플릿 SAFE_* 정규식 (하네스가 계산한 값 재검증) | borderline | trusted-by-construction 입력의 재검증. carrier가 신생이라 당분간 유지 후 재평가 |
| PR-mode 규약 (2축 provenance, updated_at 시각) | load-bearing | 재현된 우회 이력 기반. 유지 |

---

## 부위 8 — 테스트 기계 🟢 소폭

- 스크립트 소스를 텍스트로 읽어 **주석 문구 리터럴**을 단언하는 테스트 2건 → 부정 단언(금지 구문)만
  남기고 산문 단언은 구조 마커로 완화.
- TOCTOU 주입 테스트들(git wrapper·rename race)은 각각 실제 anti-simplification을 고정 — 유지.
- periphery: codexlog 재귀 walker(undocumented 스키마 — 유지), improve._finding_evidence의
  `..` 경로 거부(**over-eng** — 그 문자열은 어디서도 dereference되지 않는 참고 문자열. 제거).

---

## 우선순위 요약

**A. 즉시 (다음 라운드 안):**
1. **발행 게이트 3차 기준 재작성** — "직접 결속" 새 항아리로. 조상/replace/refspec/bounded 기준 4건 폐기. ← 가장 크고, 3차 발진이 이 결정에 걸려 있음
2. discard reason-match 제거, packet 3중 검사 1개 축약, _review_sha_binding dead wrapper 삭제, _finding_evidence `..` 거부 제거 — 전부 순삭제, 한 task로 묶기 가능
3. triage-discipline 착지 후: 헤더 파서 corner 기계 + 자기 재검증 제거

**B. 사용자 결정 필요:**
4. 프리플라이트 프로브 존폐/절충 (기본 off + 실패 이력 머신만 on)
5. legacy migration sunset 시점 (모든 머신 이관 완료 확인 → 서브시스템 삭제)

**C. 축소 여지 (급하지 않음):**
6. session_context routing 재검증, SKILL 8질문 의식, _unique_path 상한, 산문 단언 테스트, verifier manifest의 mtime_ns

**D. 명시적으로 건드리지 않는 것 (load-bearing 확인):**
verifier worktree manifest(2회 실전 발화), apply digest 재해싱, checked-out-main 가드, resume atomic claim, codex_signals 신선도, migration symlink/스냅샷 규율(sunset 전까지), PR-mode 규약.

---

*방법 상세: Workflow run wf_e04e7ea8-7f6 (16 agents, 1.09M tokens). 원자료: scratchpad audit-flat.json.
이 문서는 판정 리스트이며, 개별 항목의 실행은 task 등록 후 진행.*
