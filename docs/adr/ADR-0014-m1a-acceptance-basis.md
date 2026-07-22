# ADR-0014: M1-A acceptance basis를 invariants·accepted ADR·승격 계약으로 전환한다

- Status: historical — superseded by 0.13 C2 surface cutover
- Date: 2026-07-20
- Round: —
- SSOT sections affected: 없음 — 0.12 재설계의 milestone acceptance authority를 확정한다
- Tasks: docs/adr-m1a-acceptance-basis

## Context

0.12는 legacy 구조를 보존하는 리팩터가 아니라 재설계다. 종전 계획서 §2-5와 M1-A exit는
828개 legacy test를 출력 등급에 배정하고 old/new 결과를 비교하는 방식으로 silent contract drop을
막으려 했다. 그러나 M0 exit 리뷰의 WS-CDX-3는 등급 배정과 normalization이 실제 comparator
계약으로 실행될 수 없음을 확인했다. 등급 gate를 수리하면 828개 legacy 출력이 새 설계의 목적함수가
되어 재설계가 옛 구현을 test-by-test로 모사하는 작업으로 바뀐다.

2026-07-20 사용자 ruling은 이 전제를 교체했다. 새 시스템이 보존해야 할 것은 옛 코드와 출력의
동형성이 아니라 확정된 안전·권위 계약과 Git-tracked 프로젝트 기록의 연속성이다.

## Decision

### Acceptance authority

M1-A와 이후 0.12 재구축의 합격 기준은 다음 세 계약 집합의 합집합이다.

1. `docs/invariants.md`의 I-01~I-12·E-01~E-09
2. 판정 시점에 `accepted`인 ADR의 적용 가능한 계약
3. main이 확정한 승격 계약 목록

legacy 출력 동등성, legacy 828 suite의 green, porting ledger의 등급 합계나 행별 처분은 이
합격 기준의 필요조건도 충분조건도 아니다.

### Legacy suite는 retire-by-default다

legacy 828 test는 기본 폐기한다. 전수 이식이나 import/fixture path만 바꾸는 기계적 port를 하지
않는다. main이 명시적으로 승격한 의미 계약만 새 시스템 경계의 새 계약 테스트로 다시 작성한다.
새 테스트는 옛 코드 구조, 내부 파일 배치, CLI 내부 동작이나 출력 문구를 복제하지 않고 승격된
계약의 성공·거부·fault 방향을 검증한다. legacy test의 물리 삭제 시점과 작업은 이 ADR 범위 밖이다.

### 승격 경계

Git-tracked 프로젝트 기록의 연속성은 기본 승격 대상이다. `.waystone.yml`, `tasks.yaml`과
archive, 기존 `docs/reviews/` request·binding·feedback 아카이브, `PROGRESS.md`, `ROADMAP.md` 등은
새 시스템이 계속 읽고 유효한 다음 기록을 이어쓸 수 있어야 한다. 역사 기록을 일괄 rename하거나
새 runtime store의 값으로 대체하지 않는다. 새 writer의 canonical layout과 schema는 accepted
ADR을 따르며, 이 ADR은 run ID나 manifest 계약을 다시 결정하지 않는다.

machine-local 상태와 그 저장 형식, 코드 내부 구조, CLI 내부 동작은 continuity 대상이 아니므로
자유롭게 폐기할 수 있다. 다만 해당 동작이 invariant, accepted ADR 또는 승격 계약을 구현하는
유일한 수단이었다는 이유로 그 상위 계약까지 폐기할 수는 없다.

`docs/promoted-contracts.md`는 main이 인수·확정할 후보 초안이다. 초안 행은 스스로 gate를
확장하지 않으며, main이 확정한 행만 위 세 번째 계약 집합에 들어간다.

### Porting ledger는 채굴 체크리스트다

`docs/porting-ledger.md`는 legacy 828 test에서 역사적 observable을 찾기 위한 characterization
기록과 채굴 체크리스트로만 사용한다. 출력 등급표와 `port`/`rewrite` 처분은 참고 정보이며,
comparator gate, coverage denominator 또는 M1-A exit 판정으로 사용하지 않는다. ledger 파일
자체는 이 결정에서 변경하지 않는다.

### 종전 M1-A exit를 supersede한다

이 ADR은 계획서 M1-A의 r3 출력 등급별 동일성 exit 전체, 즉 결정 당시 `:632-643`의 등급표와
ledger 배정 문단을 명시적으로 supersede한다. 계획서 원문은 역사적 맥락으로 보존하고, M1-A의
구현 범위 설명은 이 ADR에서 다시 결정하지 않는다.

새 M1-A exit는 다음을 모두 만족할 때다.

1. main이 확정한 승격 목록의 각 계약에 새 시스템 계약 테스트가 존재하고 모두 green이다.
2. I-01~I-12·E-01~E-09 위반이 0이다.
3. 적용 가능한 accepted ADR 계약과 알려진 모순 또는 실패한 계약 검사가 0이다.

따라서 WS-CDX-3는 comparator를 수리해서 닫는 blocker가 아니다. 실행 불가능한 comparator gate를
폐기하고 위 acceptance authority로 대체함으로써 소멸한다.

## Consequences

- legacy test count와 출력 등급 일치는 0.12 진척률이나 합격 증거가 아니다.
- 승격된 의미는 새 architecture 경계에서 다시 검증하므로 legacy test 자체는 새 시스템의
  verification evidence가 아니다.
- Git-tracked 역사와 사용자가 읽는 프로젝트 기록은 이어지지만 machine-local/internal
  compatibility 부담은 제거된다.
- 의도적 비승격은 승격 목록의 비승격 절에 클래스 군 단위로 남겨 누락과 구분한다.
- legacy coverage가 없는 invariant와 accepted ADR 계약은 legacy에서 승격할 항목이 아니라 새
  계약 테스트로 직접 구현해야 한다.

## Alternatives considered

- **(A) 출력 등급 gate를 수리한다.** 828개 test를 실제 관측 표면에 다시 배정하고, 동적 field의
  normal form과 executable old/new comparator를 만든다. WS-CDX-3가 현 gate의 실행 불가성과
  내적 불일치를 확인했고, 이 수리는 828개 legacy output을 새 설계의 목적함수로 만들어 전수 이식
  수렁을 낳는다. “새 계약을 지키는가” 대신 “옛 구현을 흉내 내는가”를 묻게 되어 재설계와
  싸우므로 기각한다.
- **Git-tracked 기록까지 전부 폐기한다.** 기존 프로젝트가 task·review·progress 역사를 읽거나
  이어쓸 수 없어 사용자 데이터 연속성을 파괴하므로 기각한다.

## Amendment (2026-07-20) — 단계별 gate 귀속과 M1-A exit 정정

재심 finding WS-RX-1(blocker)·WS-RX-2(major)가 확인한 정합 결함을 고정한다.

1. **승격 목록 확정.** `docs/promoted-contracts.md`는 2026-07-20 main 인수로 PC-01~PC-30 전량이
   확정됐다(confirmed v1). 이 시점부터 그 문서의 행이 본문의 세 번째 계약 집합이다.
2. **단계별 적용 원칙.** 세 계약 집합의 합집합은 0.12 재구축 **전체**의 합격 권위다. 각 승격
   계약의 새 계약 테스트 의무는 그 계약을 소유한 서브시스템이 **재구축되는 마일스톤**의 exit에
   귀속된다. 마일스톤별 귀속표는 계획서의 마일스톤 분해가 소유하며, 각 마일스톤의 task 분해
   시점에 확정한다. PC-01~30 전량 green은 0.12 재구축 완료의 exit이지 개별 마일스톤의 exit가
   아니다.
3. **M1-A exit 정정.** M1-A는 계획서가 고정한 대로 동작·저장 형식 변경 0의 기계적 구조 분할이며
   이 Amendment는 그 범위를 넓히지 않는다. 본문 새 M1-A exit 3항 중 승격 계약 테스트 항(1)은
   M1-A에 **적용하지 않고** 다음으로 대체한다: ① 계획 M1-A 범위의 구조 분할 완료 ② I-01~12·
   E-01~09 위반 0 ③ **동작 무변경의 자기 신호로서 현행 legacy suite green** — 이는 출력 등급
   comparator의 부활이 아니다. 의도적으로 동작을 바꾸지 않는 단계는 기존 suite를 그대로 실행하는
   것이 가장 싼 drift 감지이며, suite의 개별 테스트 폐기는 해당 서브시스템의 재구축 마일스톤에서
   본문 retire-by-default 규칙대로 진행한다.
4. **본문 :30-31의 한정.** "legacy 828 suite의 green은 필요조건도 충분조건도 아니다"는 재구축
   마일스톤들의 acceptance에 대한 서술로 한정한다. 행동 보존을 선언한 기계 단계(M1-A)에서는
   위 ③이 필요조건이다.

## Amendment 2 (2026-07-20) — 알려진 부채의 분리와 suite identity 고정

리뷰 finding WS-GPT-201(blocker)·WS-GPT-202(major)가 확인한 결함을 고정한다: Amendment 1의
M1-A exit ②(invariant 위반 0)는 절대치로는 달성 불가다 — 현행 suite 자체가 invariant에 반하는
동작을 성공 조건으로 고정하고 있고(ledger가 E-08 반-계약으로 분류·settled), 그것을 M1-A에서
고치면 동작 무변경 원칙이 깨진다. 또한 ③(현행 suite green)은 suite identity를 고정하지 않아
같은 patch가 suite를 축소해도 통과하는 자기참조였다.

1. **Known-debt 목록 (이 Amendment가 고정).** 다음은 M1-A 시점에 존재가 알려진 invariant
   부채이며, M1-A exit 판정에서 제외된다:
   - `docs/porting-ledger.md` #473(claim-only discard)·#510(running discard)·#516(orphan
     discard) — E-08 반-계약 suite 고정, rewrite disposition·ruling settled, M1-B 이후
     재구축 마일스톤이 해소를 소유한다.
   - #486(shim identity의 size/mtime 판정) — E-09 반-계약, content digest rewrite 예정.
   - `docs/promoted-contracts.md` "Legacy reference가 없는 신규 계약 의무" 절의 특성화 공백
     전체 — 각 항목의 신규 테스트가 소유 마일스톤에서 신설된다.
2. **M1-A exit ② 정정.** "I-01~12·E-01~09 위반 0"은 **위 known-debt 목록 대비 신규 위반 0**
   으로 읽는다. 목록에 없는 위반의 신규 도입은 불허하며, 목록의 부채를 M1-A에서 수리하는 것도
   불허한다(동작 무변경 — 수리는 소유 마일스톤의 일이다).
3. **M1-A 성격 ruling (리뷰 open question 1).** M1-A는 **순수 기계 단계**다. 알려진
   E-08/E-09 부채의 수리를 M1-A에 포함하는 노선은 기각한다.
4. **Suite identity 고정 (exit ③ 정정).** M1-A 착수 시점에 시작 commit의 test-ID 전수와
   계수를 manifest로 pin한다. M1-A 도중 suite 변경은 ⑴ 기계 분할에 따른 이동(test-ID 보존)
   ⑵ main이 명시 승인해 manifest 차이 목록에 기록한 항목 외에는 불허한다. exit ③의 "green"은
   이 manifest 전수의 실행 green이다.
5. **I-10 bookkeeping 경계 (WS-GPT-101 폐쇄 지원).** worker prompt에 허용되는 bookkeeping은
   **WAYSTONE_REPORT 보고 계약 stanza뿐**이다. registry(tasks.yaml)·round·exposure·overlay 등
   내부 상태 표면의 경로·지시·프로토콜은 worker prompt에 전달하지 않는다. 이 경계는
   특성화 테스트가 단언한다(fix/i10-prompt-minimality-characterization).

## Amendment 2 — Addendum (2026-07-20, 같은 날) — I-10 현행 위반의 부채 편입

특성화 착수 작업(w4-i10)이 §5 경계의 **현행 위반**을 확정했다(main 독립 재현 완료): 실제
dispatch 경로(`scripts/delegate.py` — packet 조립 :544-592, TASK_BLOCK 렌더 :651-665·:673-687,
`prompt.txt` 저장 :2156)가 registry 유래 표면 `status`·`milestone`·`round`·`anchor`·
`routing_note`(dependency 존재 시 `deps`+상태 포함)를 rendered worker prompt에 전달한다.
따라서 "현행 템플릿에서 green"인 정직한 경계-준수 테스트는 작성 불가다.

1. **Known-debt 목록 확장 (§1에 추가).** 위 표면 집합의 worker prompt 전달을 I-10 부채로
   고정한다. 근거: `docs/meta/agent-reports-2026-07-20/w4-i10.md`(재현 명령 포함). 수리는
   M1-B(delegate 재구축)가 소유한다 — `fix/delegate-prompt-i10-surface-strip`. 수리 시점에
   `anchor`는 goal/bounds 자료로 재편성될 수 있다(규범 목표는 §5 그대로; 목표 표면 집합의
   확정은 수리 task의 일).
2. **특성화 테스트의 형태 재규정.** `fix/i10-prompt-minimality-characterization`은
   pinned-debt 형태로 작성한다: ⑴ 양성 단언(goal·bounds·acceptance·WAYSTONE_REPORT stanza)
   ⑵ 현행 부채 표면 5종의 **정확 고정**(exact pin — 목록 외 registry/내부 표면이 하나라도
   추가되면 red) ⑶ 부채 목록 외 대표 내부 표면(tasks.yaml 경로·ROADMAP·PROGRESS·.waystone/·
   round close·exposure·overlay 지시)의 부재 단언. 이로써 M1-A가 요구하는 보호(기계 이동 중
   **신규** 유출 0)는 성립하며, WS-GPT-101 blocker는 이 테스트의 착지로 폐쇄된다.
3. **정합성.** 이 처분은 §2(known-debt 대비 신규 위반 0)·§3(M1-A 순수 기계 — 부채 수리 불허)
   의 기존 기계를 그대로 적용한 것이다. M1-A에서 이 부채를 수리하는 것 역시 불허된다.

## Amendment 2 — Addendum 2 (2026-07-20) — 목록 폐쇄 정정과 보장 범위 명확화

리뷰 round 2026-07-20-review-remediation의 finding 2건(WS-GPT-301·302, 검증 후 minor)을
정정한다. 두 건 모두 문서 결함이며 M1-A의 성격·게이트·착수 승인을 바꾸지 않는다.

1. **E-08 known-debt의 카테고리 폐쇄 (WS-GPT-301 정정).** §1 첫 항목의 행 나열
   (#473/#510/#516)은 불완전 전사였다. M1-A 시점에 존재가 알려진 E-08 반-계약 suite 고정의
   **완전한 집합**은 `docs/traceability-matrix.md`의 "취소·cleanup 역-계약 legacy test" 절과
   `docs/promoted-contracts.md`의 "unsafe discard·cleanup characterization" 명시적 비승격군이
   동일하게 열거하는 **7행**(#473·#510·#516·#517·#518·#533·#537)이다. §1의 E-08 항목은 이 두
   열거를 참조하는 카테고리 폐쇄로 읽는다. §1 세 번째 항목("Legacy reference가 없는 신규 계약
   의무" 절 참조)은 그 헤더 범위상 legacy reference를 **가진** 이들 행을 포괄하지 않았음을
   인정한다 — 이 Addendum이 그 간극을 닫는다. 해소 소유(재구축 마일스톤)는 불변이다.
2. **I-10 "신규 유출 0" 보장 범위 (WS-GPT-302 정정).** Addendum §2의 보장은 **코드 유래**
   신규 투영 표면에 대한 것이다 — 방어 주체는 exact-pin(TASK_BLOCK 전행 일치 + rendered
   prompt 전문 oracle + template SHA-256)이며, 부재 단언은 fixture 범위의 보조 검사다.
   운영자가 공급하는 `routing_note` **값 내용**을 통한 문자열 유입은 별개 채널로, §1이 pin한
   routing_note 부채의 일부이며 그 제약(값 검증 또는 투영 자체의 제거)은 M1-B
   `fix/delegate-prompt-i10-surface-strip`이 소유한다. M1-A에서의 값 검증 추가는 동작 변경
   이므로 §3에 따라 불허된다.

## Amendment 2 — Addendum 3 (2026-07-20) — M1-A 호환 shim의 계약 경계 ruling 2건

closeout 리뷰 finding WS-GPT-501·502(검증 후 minor)가 요구한 경계 결정을 accepted ruling으로
명문화한다. 두 건 모두 M1-A exit를 재열지 않는다(검증: invariant/suite 게이트 무손상, 대상
표면의 소비자 0 실증).

1. **`waystone` package root는 의도적으로 비어 있다 (WS-GPT-501 수용 ruling).** 지원 계약은
   **submodule import의 import-order 무관성**(`from waystone.X import …`·`import waystone.X`
   — 실소비 전부)이며, adapter의 legacy `import waystone` 표면(`main`/`os` 노출)은
   **scripts-first 경로 전용**이다. repo-first 순서에서 빈 root가 선택되는 것은 skeleton
   설계의 수용된 성질이다 — 그 조합(repo-first + legacy shim 선행)의 소비자는 0이고, repo
   내 유일한 `PYTHONPATH=scripts` 용례는 release smoke가 **오염으로 규정해 제거하는** 경로다
   (test_release 격리 계약). root에 cli 표면을 재수출하는 수리는 **기각한다** — cli.main→
   common→waystone.core의 실제 순환 import를 도입한다(검증자 실증). w6 rebind와 그 scripts-
   first 증명 범위는 올바르다.
2. **compat shim의 지원 monkeypatch 표면 = `setattr`/`delattr`뿐 (WS-GPT-502 ruling).**
   direct module-dict mutation(`mock.patch.dict(module.__dict__, …)`·`vars(module)[…]=…` 등)은
   M1-A "동작 무변경" 계약 **외**로 확정한다. 근거 결속: ⑴ 전달이 기술적으로 불가하다 —
   이동된 함수의 globals는 owner 모듈 dict이고 module `__dict__`는 가로채기 매핑으로 교체
   불가 ⑵ 소비자 0 실증 — suite의 module-attr patch 전수가 setattr 표면, dict-patch 전수가
   os.environ 대상 ⑶ wrapper 우회는 re-export identity 파괴로 더 큰 관측 변경. 이 제한은
   shim 3종의 **모듈 docstring**에 표기해 `help()`/`__doc__`에서 가시화한다(종전 metaclass
   docstring 단독 표기는 비가시 — 함께 정정).
