<!-- waystone feedback: the body below is the reviewer reply VERBATIM (byte-exact copy via `waystone review ingest`) — do not edit it; a triage skeleton is appended beneath it. -->
round: 2026-07-20-review-remediation
reviewer: codex:gpt-5.6-sol
reviewer-effort: ultra
review-target: ad26225341e993cecb3dfc705bdda6ce88e3ff18
reply-metadata-json: {"metadata":{"effort":"ultra","model":"codex:gpt-5.6-sol","request-digest":"sha256:2b0781dd5c1ca34200e75ec96d38b36dc9b77bfcce29d1afe1c9c6eebe9cd344","review-target":"ad26225341e993cecb3dfc705bdda6ce88e3ff18"},"narrative_digest":"sha256:19bdd8ecfa44b8b367ff69c5ca728339d1d5732518d38fa1e2ba412f88b6541d","rendered_request_coverage_reason":null,"rendered_request_digest":"sha256:2b0781dd5c1ca34200e75ec96d38b36dc9b77bfcce29d1afe1c9c6eebe9cd344","rendered_request_digest_matches":true}
ingested: 2026-07-20
source: /tmp/review.md
verbatim-bytes: 11549

---

model: codex:gpt-5.6-sol
effort: ultra
review-target: ad26225341e993cecb3dfc705bdda6ce88e3ff18
request-digest: sha256:2b0781dd5c1ca34200e75ec96d38b36dc9b77bfcce29d1afe1c9c6eebe9cd344

# External adversarial review — 2026-07-20-review-remediation

판정: **CHANGES REQUESTED — blocker 1건, major 5건.** 전체 830-test suite가 green인 상태에서도 아래 반례들이 재현된다.

## Confirmed findings

### WS-GPT-301 — E-08 known-debt 목록이 원 blocker의 핵심 역계약을 누락했다

- Severity: blocker

WS-GPT-201은 unreadable `status.json`·`exposure.json`에서 destructive cleanup을 성공시키는 `DelegateCorruptRecordTests.test_discard_accepts_corrupt_record`를 직접 반례로 들었다(`docs/reviews/2026-07-20-ruling-execution-feedback.md:26-30`). 그러나 triage는 근거를 ledger `#473/#510/#516`으로 축약했고(`:99-104`), Amendment 2도 그 세 항목만 E-08 known debt로 열거했다(`docs/adr/ADR-0014-m1a-acceptance-basis.md:128-138`).

authoritative ledger에는 목록 밖 E-08 reverse contract가 계속 남아 있다. `#517/#518`은 destructive discard를 reconcile·observed-quiescent 흐름으로 rewrite하도록 판정되어 있고(`docs/porting-ledger.md:823-824`), 특히 `#537`은 unreadable effect state에서 worktree 삭제를 허가하므로 rewrite 판정이다(`docs/porting-ledger.md:845-853`). matrix도 이들을 포함한 일곱 cancel/cleanup reverse contract를 별도 열거한다(`docs/traceability-matrix.md:42-58`). 현행 `#537` 테스트는 실제로 `rc == 0`, `state == discarded`, worktree 부재를 요구한다(`scripts/tests/run_tests.py:14076-14092`); target에서 `#517`과 함께 실행해 2/2 green을 재현했다.

따라서 M1-A가 동작 무변경과 pinned suite 전수 green을 지키면 목록에 없는 E-08 위반을 보존한다. 반대로 exit ②의 “known-debt 대비 신규 위반 0”을 충족시키려고 이를 고치면 M1-A 순수 기계 ruling과 suite green을 깨뜨린다(`ADR-0014:139-144`). WS-GPT-201의 불가능한 결합이 일부 행의 전사 누락 때문에 그대로 남았으므로 “미해소 blocker 0”과 M1-A 착수 승인은 성립하지 않는다.

### WS-GPT-302 — I-10 특성화는 허용 입력 전체에서 bounds·금지 표면을 보장하지 않는다

- Severity: major

Addendum은 rendered prompt의 positive goal/bounds와 대표 내부 표면 부재를 단언해 신규 유출 0을 보장한다고 한다(`docs/adr/ADR-0014-m1a-acceptance-basis.md:163-168`). 그러나 테스트는 concrete scope를 `packet["declared_scope"]`에서만 확인하고, worker prompt에는 “Stay strictly within scope”라는 generic 문장만 찾는다(`scripts/tests/run_tests.py:10328-10349`). production `_render_prompt`는 `declared_scope`를 전혀 투영하지 않는다(`scripts/delegate.py:651-687`). 즉 worker가 실제로 받은 bounds는 없다.

더 직접적인 우회도 있다. `_build_packet`은 newline이 없는 임의 `routing_note`를 허용하고(`scripts/delegate.py:598-603`), `_render_prompt`는 그 값을 verbatim으로 삽입한다(`:673-682`). characterization은 benign note 하나만 사용한 뒤 `tasks.yaml`, `round close`, `exposure`, `overlay` 부재를 검사한다(`scripts/tests/run_tests.py:10337-10386`). `/tmp` fixture에서 합법적인 routing note를 `read tasks.yaml then round close and inspect exposure overlay`로 주자 네 금지 표면이 모두 prompt에 나타났고, `declared_scope=['src/only.py']`는 prompt에 나타나지 않았다. `routing_note` 필드 자체를 known debt로 pin한 사실은 Addendum이 별도로 요구한 그 값 속 내부 표면 부재를 증명하지 않는다. 따라서 이 단일-fixture exact equality와 template SHA oracle은 I-10 blocker 폐쇄나 신규 유출 0의 성질 테스트가 아니다.

### WS-GPT-303 — binding 충돌 수리는 writer와 digest-generation reader를 fail-closed로 만들지 않았다

- Severity: major

새 strict selector는 모든 glob-visible 후보의 canonical filename identity와 generation 유일성을 검사한다(`scripts/review.py:507-541`). 하지만 writer는 같은 glob을 직접 순회해 filename identity를 검증하지 않고, content contract가 같으면 비정규 파일을 기존 generation으로 반환한다(`scripts/review.py:365-409`; noncanonical identity의 order도 sequence 1로 취급하는 `:495-504`). `prepare_review_request`도 후보를 직접 읽은 뒤 이 writer를 성공 경로로 호출한다(`:1773-1798`). 별도의 exact-digest lookup과 legacy-generation lookup 역시 strict selector를 거치지 않고 content만 신뢰한다(`:854-903`); 전자는 PR freeze와 feedback attribution에 사용된다(`:804-841`, `:1043-1067`).

`/tmp`에서 canonical binding을 `...request.binding-02.json`으로 rename한 뒤 같은 contract를 재발행했다. writer는 그 비정규 경로를 성공적으로 재사용했고 `_request_generation_in_directory`도 exact digest generation으로 반환했지만, `latest_round_request_binding`은 동일 후보 집합을 `(None, None)`으로 거부했다. 즉 prepare/feedback 쪽은 한 generation을 명시적으로 신뢰하는 동안 settlement/pending 쪽은 같은 bytes를 ambiguity로 본다. PC-10의 단일 latest authority와 Claim 2의 “glob이 흡수하는 비정규 이름 전수 fail-closed”는 완전히 닫히지 않았다.

### WS-GPT-304 — preserved-profile raw-byte 비교는 migration 완료의 필요조건도 충분조건도 아니다

- Severity: major

현재 detector는 preserved profile을 모은 뒤 live profile이 존재할 때만 비교 집합에 추가하고, 서로 다른 body가 두 개 이상일 때만 offender를 만든다(`scripts/common.py:412-455`). 이 때문에 preserved profile 하나만 있고 live `.waystone/profile.yml`이 없는 실제 미완료 seed 상태는 `migrate_project_state == False`로 통과한다. `/tmp` fixture에서 그대로 `False`, `live_exists == False`를 재현했다. 설계상 Phase 2는 live profile이 없을 때 preserved seed를 복사해야 한다(`dev_docs/0.9-pre-adr-storage-lock-autonomy.md:333-340`).

반대 방향도 잘못됐다. `.waystone/profile.yml`은 현재의 human-authored routing authority이며 다른 원천에서 재생성할 수 없다(`docs/runtime-state-audit.md:109`). 완료 후 사용자가 project-local profile을 정상 변경하면 preserved seed와 달라지는 것이 당연하지만, detector는 이를 pre-0.9 conflict로 거부한다. preserved `reviewer: old`, live `reviewer: new-project-choice` fixture에서 `Pre09StateError`와 양쪽 byte 무수정을 재현했다. 이 검사는 task/review/delegate의 공용 진입점에서 실행되므로(`scripts/tasks.py:529-549`, `scripts/delegate.py:3745`, `scripts/review.py:3007`) 정상 프로젝트 명령 전반을 차단한다. 현재 테스트는 divergent preserved, preserved-vs-live mismatch, 전부 동일한 경우만 다루며 missing-live와 completed-then-edit를 누락한다(`scripts/tests/run_tests.py:18210-18271`). Claim 3의 “정상 완료 이관은 분기 profile을 만들 수 없다”는 전제가 성립하지 않는다.

### WS-GPT-305 — marker 수리는 `<slug>` leaf만 검사해 engine-owned 상위 symlink를 따라간다

- Severity: major

worktree cache 경로는 `machine_dir()/cache/worktrees`로 구성되지만(`scripts/common.py:179-180`), sunset detector는 그 아래 최종 `<slug>` path만 `_checked_entries`/`lstat`한다(`:481-489`). 따라서 `cache/worktrees` 자체가 symlink이면 ancestor를 따라간 뒤 외부 target의 leaf를 ordinary directory 또는 absence로 관측한다. leaf 자체 symlink 테스트는 이 경우를 다루지 않는다(`scripts/tests/run_tests.py:18175-18193`).

`/tmp`에서 `~/.waystone/cache/worktrees -> external`을 만들고 slug leaf를 비워 둔 결과 detector는 `False`를 반환했다. 이어 production과 동일한 `delegate._mkdir_or_refuse` 경로(`scripts/delegate.py:305-325`)가 external target 아래 directory를 생성했다: `ancestor_is_symlink=True`, `external_write=True`. 즉 no-follow·원본 무수정 detector가 상위 container redirect를 놓치고 이후 writer가 그 redirect를 실제로 따른다. 이는 engine-owned subtree symlink를 follow하지 말고 검증 불가 시 typed refusal하라는 ADR-0013 계약(`docs/adr/ADR-0013-operational-threat-model.md:52,73-88`)과 Claim 3을 위반한다.

### WS-GPT-306 — linked read는 미초기화 active selector를 canonical same-relative decoy로 승격한다

- Severity: major

explicit task root는 단순 `resolve()`만 하고 초기화 여부를 확인하지 않는다(`scripts/tasks.py:417-421`). `_canonical_read_root`는 linked path의 상대경로를 `common_dir.parent` 아래 canonical candidate로 옮긴 뒤, 그 candidate가 initialized project인지 만 확인한다(`:466-500`). active selector 자체의 `find_project_root(root) == root` 또는 `.waystone.yml` 존재는 증명하지 않는다.

`/tmp`의 실제 Git linked worktree에서 active `linked/nested`에는 `.waystone.yml`이 없고, canonical checkout의 같은 상대경로에만 initialized decoy project와 task를 만들었다. `task list <linked/nested>`는 `rc=0`으로 canonical-only task를 출력했고 canonical decoy에 `.waystone/lock`을 생성했다: `linked_initialized=False`, `canonical_task_exposed=True`, `canonical_lock_created=True`. 따라서 미초기화 selector가 typed refusal되지 않고 다른 project authority로 치환된다. 이는 PC-31의 미초기화 root no-state/typed-refusal 계약(`docs/promoted-contracts.md:42`)과 Claim 4의 재탐침·decoy 방어를 직접 깨뜨린다.

## Open domain questions

1. `routing_note`가 owner가 신뢰하는 무제한 worker text라서 I-10 금지 문자열 규칙의 예외라면, Addendum §2의 대표 내부 표면 부재 주장과 characterization 기대를 철회하고 그 예외 경계를 명시해야 한다. 예외가 아니라면 field 값에 대한 enforcement/property test가 필요하다.
2. explicit linked `--root`가 “active path 자체가 initialized project”를 뜻하는지, “같은 repository의 canonical relative locator”만 뜻하는지 ruling이 필요하다. 현재 PC-31은 전자를 요구하지만 구현은 후자로 동작한다.

## Residual risks from unavailable GPU / data / environment

- GPU, 외부 dataset, network, 별도 service가 필요한 검증은 없었다. 이용 불가 자원 때문에 보류한 finding도 없다.
- macOS의 현재 filesystem/Git에서만 동적 재현했다. 다른 filesystem의 symlink·permission 의미는 실행하지 않았지만 WS-GPT-305는 현재 지원 환경에서 이미 재현되는 fail-open이다.
- 전체 suite를 target에서 직접 실행해 `Ran 830 tests`, `OK`, rc=0을 확인했다. 위 finding들은 모두 그 green 상태와 공존한다.

## Independent verification

- review 범위: `git diff 1f7d942b418025296704ff5bac4a13ac54d00ca5..ad26225341e993cecb3dfc705bdda6ce88e3ff18`.
- E-08 역계약 표적 테스트 2건: 2/2 green.
- binding alias, preserved-profile 두 방향, ancestor-symlink, I-10 legal-input, linked-root decoy fixture: 모두 `/tmp`에서 production helper/CLI로 재현.
- repository HEAD는 target과 일치하고 최종 `git status --ignored --short`는 clean이다. full-suite가 current worktree에 일시 생성한 ignored `.waystone/`와 `scripts/__pycache__/`는 audit 즉시 생성물 전수만 제거했으며, tracked bytes에는 변화가 없었다.


---

<!-- waystone triage: BEGIN -->
## Finding triage (main 판정, 2026-07-20 — finding당 독립 opus verifier 반증 검증, 전 건 동적 재현 수반)

릴리스 하네스(0.11.1)의 skeleton parser는 구식 JW prefix 전용이라 표를 생성하지 못했다(알려진
하네스/dev skew — dev는 WS 전용 계약). 아래는 free-form 직접 triage다. 종합: **blocker 0 생존**
(리뷰어 1b/5M → 검증 후 **2M/4m**). M1-A 착수 승인 유지(WS-GPT-301 blocker 귀결 기각).

### WS-GPT-301 — E-08 known-debt 전사 누락 (리뷰어 blocker)
- verdict: **PARTIAL** (누락 사실 REAL · blocker 귀결 REJECTED) → **minor** / taxonomy: reporting
- verifier 요지: 인용 전부 확인 — Amendment 2 §1은 E-08 역계약 7행 중 3행만 열거했고 3번째
  항목의 교차참조는 헤더 범위상 나머지를 배제(오참조). 단 promoted-contracts "unsafe discard·
  cleanup" 명시적 비승격군이 7행 전수를 이미 클래스 처분(침묵 누락 아님)했고, exit ②는 "신규
  도입 불허+M1-A 수리 금지"라 강제 수리 딜레마 불성립 — 게이트 성립, 승인 유지.
- 조치: `docs/adr-0014-e08-debt-closure` **done** (Addendum 2 §1, dev 214a6fc — 카테고리 폐쇄).

### WS-GPT-302 — I-10 특성화의 허용 입력 보장 한계 (리뷰어 major)
- verdict: **PARTIAL** (major 기각 · 잔여 minor) / taxonomy: verification
- verifier 요지: 기계적 사실 전부 재현되나 위협 혼동 — exact-pin이 코드 유래 신규 투영을 실제
  방어함을 역실험으로 입증(부재 단언이 놓치는 케이스를 exact-pin이 잡음). routing_note **값**
  채널은 별개 위협으로 이미 pin된 부채(M1-B 소유). declared_scope 미투영은 테스트 주석·보고서에
  명시 공개된 정직한 특성화(은폐 주장 허위). WS-GPT-101 원문은 값 내용 enforcement를 요구한 바
  없음.
- 조치: `docs/adr-0014-addendum-i10-value-clarify` **done** (Addendum 2 §2, dev 214a6fc) +
  M1-B strip task에 값 채널 범위 명시.

### WS-GPT-303 — binding writer·digest-reader 비대칭 (리뷰어 major)
- verdict: **PARTIAL** (비대칭 REAL · fail-open 기각) → **minor** / taxonomy: correctness(위생)
- verifier 요지: 재현 확정. 단 모든 판정(settlement·pending·ingest·freeze·완료 대조)은 fail-closed
  경로가 gate — path-B 값이 판정을 위조할 수 없음을 경로별 입증. WS-GPT-206 수리는 완결(회귀
  테스트 확인), 이 건은 신규 인접 관찰. 실질 결함은 writer의 조용한 비정규 재사용+성공 출력
  (wedged round, constitution의 침묵 성공 금지 위반). 수리 회귀 위험 낮음(정직 이력은 비정규명
  0).
- 조치: `fix/review-binding-writer-identity` 등록 (minor).

### WS-GPT-304 — preserved-profile 비교의 양방향 결함 (리뷰어 major)
- verdict: **② REAL major (수리 회귀) · ① REJECTED** / taxonomy: correctness
- verifier 요지: ② live profile을 비교 집합에 넣은 것은 207 처방의 과확장(원 증거는 preserved
  끼리의 분기만) — live는 복원 불가한 현재 권위이며, .pre-0.9 잔재 머신에서 구조적 전면 차단
  재현. 기존 테스트는 잘못된 oracle을 고정 중. ① missing-live 미완료 주장은 sunset이 폐지한
  구 Phase 2 의무 의존 — 기각(preserved 단독 보존=합법 해소 상태). ①·②는 상호 모순 요구.
- 조치: `fix/sunset-live-profile-overreach` 등록 (**major**) — live 제거·oracle 반전·missing-live
  수용 테스트.

### WS-GPT-305 — engine-owned 조상 symlink follow (리뷰어 major)
- verdict: **REAL major** (208 미완 아닌 신규 인접) / taxonomy: correctness
- verifier 요지: cache/worktrees 조상 symlink 시 detector 통과 + `_mkdir_or_refuse`가 외부 경로에
  실제 디렉터리 생성 재현(mkdir 경로에 자체 방어 부재 확인 — "refuse"는 OSError 변환뿐).
  ADR-0013:52·:73-77 위반 확정(machine root 하위 component는 no-follow 범위). 208 브리프·수리는
  명시 범위(slug leaf) 완수 — 한 단계 위의 동종 결함.
- 조치: `fix/worktrees-cache-ancestor-symlink` 등록 (**major**) — detector 조상 검사 + mkdir
  no-follow·containment 2겹.

### WS-GPT-306 — linked read의 미초기화 selector 치환 (리뷰어 major)
- verdict: **PARTIAL** (실재·재현 · PC-31 위반 과대) → **minor** / taxonomy: correctness
- verifier 요지: 재현 확정(orphan branch 시나리오 — .waystone.yml은 tracked라 동일 커밋에선 발생
  불가, 브랜치 분기·미추적 중첩에서만). PC-31 no-state 조항은 미위반(미초기화 쪽 생성물 0) —
  "조용한 권위 치환"으로 재명명. 같은 소유자 저장소라 착취성 낮음. cwd 암묵 경로·mutation은
  정상 거부 확인.
- open question 2 ruling (main): **명시 root = 그 경로 자체가 초기화된 프로젝트** — canonical
  정규화는 litter 회피 redirect일 뿐, 현행 checkout에 부재한 프로젝트를 되살리는 locator가
  아니다. 근거: PC-31 문언·최소 놀람·기존 정규화 테스트 전부 이 해석 충족.
- 조치: `fix/linked-read-selector-init-gate` 등록 (minor) — 정규화 전 selector 초기화 검증,
  실패 시 typed 거부 + RED 테스트.

### Open questions 처분
1. routing_note 예외 경계 → Addendum 2 §2가 경계 명문화(코드 유래 vs 값 내용), enforcement는
   M1-B strip task 소유. 철회 아닌 범위 확정.
2. 명시 linked root semantics → 위 306 ruling(전자 해석 채택).

### 종합 처분
- 문서 2건 즉시 집행(done, dev 214a6fc). 코드 4건(2M/2m) 등록 — M1-A 분할 wave **이전에**
  수리 wave로 처리(hot-file: common.py=304+305 동일 구획 인접, review.py=303, tasks.py=306).
- M1-A 착수 승인 유지. 분할 wave는 이 수리 wave 마감 후 발사.
<!-- waystone triage: END -->
