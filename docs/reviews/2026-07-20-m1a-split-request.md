# Review Request — 2026-07-20-m1a-split

The reviewer has the repository via git. This is a domain/code review, not a workflow audit —
keep the waystone harness out of scope unless asked.

- Project: waystone
- Branch: dev
- Reviewer: codex:gpt-5.6-sol
- Reviewing: 73789f9fd2a8bb829a36169d46137f173df4f52a   (diff against ad26225341e993cecb3dfc705bdda6ce88e3ff18)

<!-- Keep the Reviewing field on exactly one line with the literal spacing shown above. -->

## What changed and why

두 갈래 라운드다. ① 직전 remediation packet의 codex 리뷰 6 finding을 검증·처분(수리 wave w5:
sunset live-profile 과잉 제거·worktree cache 조상 symlink 2겹 방어·binding writer typed
fail-closed·linked selector 초기화 게이트 + ADR-0014 Amendment 2 Addendum 2). ② **M1-A 기계
분할 전체 집행**: scripts/ 모놀리스의 kernel 경계 4개(dispatcher·common·delegate·tasks)를
`waystone/` package(cli/core/project/runs/adapters)로 이동하고 21.8k줄 테스트 스위트를 13개
주제 모듈로 분할했다. 모든 이동은 [m1a-move]/[m1a-adapter] 커밋 분리, source-identical AST
기계 증명, 호환 shim(monkeypatch bridge·import-shadow 보존), front-door byte-identity, pinned
manifest 838 green을 통과했고, main이 M1-A exit 충족을 판정했다(ADR-0014 기준).

## Read these first

1. `PROGRESS.md` 최신 절(2026-07-20-m1a-split) — 커밋별 지도
2. `dev_docs/m1a-split-plan.md` — 분해 계획(규범)과 실제 집행 대조
3. `docs/adr/ADR-0014-m1a-acceptance-basis.md` — Amendment 2·Addendum·Addendum 2 전체
4. `docs/meta/agent-reports-2026-07-20/m1a-*.md` 5건 — 기별 기계 증명(§1 AST 대조 원문 포함)
5. `docs/meta/agent-reports-2026-07-20/w5-*.md` 3건 + `docs/reviews/2026-07-20-review-remediation-feedback.md` triage
6. `scripts/common.py`·`scripts/tasks.py`·`scripts/delegate.py`(호환 shim 3종)·`scripts/tests/run_tests.py`(집계 adapter)·`docs/m1a-suite-manifest.txt`

## Claims to attack

1. 이동이 정말 순수 기계였다 — source-identical 증명(AST 선언 대조·전소스 정규화)이 빈틈없고,
   허용된 변경(bootstrap·자원 경로)이 실제로 그것뿐이다.
2. 호환 shim 3종(_CommonShim류 bridge)이 기존 관측면을 완전 보존한다 — 특히 테스트의
   monkeypatch(setattr/delattr)·import-shadow(`import waystone`이 scripts adapter를 선택)·
   runpy 표면. shim 자체가 새 동작(예: sys.modules 조작)으로 도입한 회귀가 없다.
3. 테스트 분할이 test-ID 838을 실제로 보존했다 — 집계 adapter의 `__module__` 재바인딩이
   선택자·출력 identity를 유지하고, 클래스·helper가 각 1회씩만 존재한다.
4. M1-A exit 판정이 정당하다 — known-debt 대비 신규 위반 0(Amendment 2 §2)·suite identity
   (§4)·front door 불변이 실증거로 뒷받침되며, 판정에 쓰인 검증 스크립트들에 허점이 없다.
5. w5 수리 4건이 검증자 권고 형태와 일치하고 새 fail-open/오탐을 도입하지 않았다.

## Evidence already produced (mine — inspect, don't trust)

- 기별 보고서의 기계 증명 원문: AST 선언 대조(84/102·160/234·32/49·84클래스/43 helper),
  전소스 정규화 byte-identity(runs), bridge 전수 프로그램 검증(234/234 setattr 전달),
  front-door 전후 SHA-256 동일(각 단계), full suite rc=0(기체별 고유 로그).
- main 최종 검증: 병합 HEAD에서 core 상방 import 0 + manifest 838 delta 0 + full gate 838
  rc=0 재실행.
- w5: RED→GREEN 재현 원문(HOME 격리), 검증자 6기의 반증 판정은 feedback triage에 결속.

## Known weak spots

- 호환 shim의 module-class bridge와 sys.modules/sys.path bootstrap 조작은 영리한 임시 비계다
  — 의도는 M1-C cut-over에서 제거이나, 그때까지 import 순서에 민감한 새 코드가 들어오면
  가장자리가 있을 수 있다(현 suite는 green).
- delegate의 `_git`은 adapters.git과 의미가 달라 정렬을 거절했다(관찰 기록) — 두 git 헬퍼
  집합이 병존하는 상태가 M1-B까지 지속된다.
- run_tests.py 집계 adapter의 `cls.__module__ = __name__` 재바인딩은 identity 출력 보존용
  트릭이다 — unittest 내부 동작 변화에 취약할 수 있다.
- I-10 위반(worker prompt의 registry 표면 5종)은 여전히 known-debt로 존재한다(수리는 M1-B
  fix/delegate-prompt-i10-surface-strip 소유 — Addendum·Addendum 2가 경계 명문화).
- 역사 pending 리뷰 7건은 범위 밖(3건 사용자 ruling 대기).

## Domain lens

M1-A의 유일 계약은 "동작·저장 형식 변경 0"이다(ADR-0014: 순수 기계 단계, known-debt 수리
금지). 따라서 이 라운드의 리뷰는 기능 설계가 아니라 **이동의 무결성**을 공격하는 것이 옳다:
diff 0 증명의 빈틈, shim의 의미 누수, suite identity의 우회 가능성. 수리 4건(w5)만 예외적으로
동작 변경이며 각각 fail-closed 방향이다.

## Response wanted

Start the reply with this block (replace values; key case/order/spacing and a Markdown fence are
optional; extra keys are preserved). Echo the `Reviewing` target, alone or as a 12–40 hex
`base-target` range, and copy the request digest exactly; missing/damaged values stay unknown, and
no model/target means ordinary prose:
```text
model: codex:gpt-5.6-sol
effort: high
review-target: 73789f9fd2a8bb829a36169d46137f173df4f52a
request-digest: sha256:8dede37fa718e9d49c4e613b462c8a95a8115038fbc93cc46e71822ac372e3ea
```

Major / critical issues only. For each: a concrete failure mechanism and where you confirmed it.
Separate confirmed findings, open domain questions, and residual risks from unavailable
GPU / data / environment.
