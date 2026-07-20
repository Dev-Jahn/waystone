# M1-A 기계 분할 분해 계획 (docs/m1a-mechanical-split-plan)

기준: 계획 §3-2·§6 M1-A + ADR-0014(Amendment 1 단계별 gate·Amendment 2 known-debt/suite pin/순수
기계 ruling·Addendum I-10 부채). 동작·저장 형식 변경 0. 이동과 수정은 커밋 분리.
suite identity: `docs/m1a-suite-manifest.txt`(830, @ b027d52) — 변경은 ID 보존 이동 또는 main
승인 후 approved-diffs 기재만.

## 현행 인벤토리 (dev f513a4e)

| 모듈 | 줄 | 내부 의존 | M1-A 처분 |
|---|---|---|---|
| common.py | 1114 | (없음) | **분해 이동** — kernel 기저 |
| tasks.py | 678 | common, round, validate | **이동** — registry kernel |
| delegate.py | 3888 | common | **이동** — run kernel 경계 |
| review.py | 3047 | common | 잔류 (M2+, features) |
| improve.py | 4746 | common, cclog, codexlog | 잔류 (M2+, features) |
| overlay.py | 2676 | common | 잔류 (M2+, features) |
| round.py | 527 | common | 잔류 (tasks가 쓰는 text-surgery helper만 노출 유지) |
| waystone.py | 719 | common (dispatcher, runpy) | **adapter 전환** — composition root 호출로 |
| 기타 (cclog·codexlog·merge·ssot·validate·…) | ~2.3k | 얕음 | 잔류 |
| tests/run_tests.py | 21806 (830 tests) | 전 모듈 | **기계 분할** |

## 목표 배치 (§3-2 layout, M1-A 범위만)

```text
waystone/
  __init__.py
  core/        # common.py에서: WorkflowError·Pre09StateError·typed refusal, fs/lock/atomic-write,
               #   json(dup-key 거부)/yaml 헬퍼 — 상방 import 금지 (검증 대상)
  project/     # common.py에서: root 발견·profile·state dir·pre-0.9 sunset detector
               # tasks.py 전체: registry read/mutate/validate 표면
  adapters/
    git.py     # common.py+delegate.py의 git probe·worktree·checkout-context 헬퍼
  runs/        # delegate.py 전체(packet·prompt 조립·worktree lifecycle·runner transport·verify)
               #   — 내부 재설계 없음, 파일 이동+import 경로만
  cli/         # composition root: 기존 scripts/waystone.py dispatch의 이식 대상
```

improve/overlay/review/round는 M1-A에서 **이동하지 않는다**(계획 §3-2 명시). 이들은 기존
`scripts/` 경로에서 `waystone.core/project`를 import하도록 **호출부만** 바꾼다(adapter 커밋).

## 커밋 규율 (순수 기계 증명)

1. **move 커밋**: `git mv` + import 경로 수정만. 함수 본문 diff 0 (`git diff --find-renames`로
   본문 무변경 확인 가능해야 함). 커밋 메시지에 `[m1a-move]` 표기.
2. **adapter 커밋**: 잔류 모듈·bin 진입점의 import 전환. `[m1a-adapter]` 표기.
3. 금지: 이동 중 rename·시그니처 변경·dead code 제거·스타일 정리(발견 시 finding으로 기록만).
   known-debt(E-08 3건·E-09 1건·I-10 표면 5종) **수리 금지** — Amendment 2 §2·§3.
4. I-10 특성화(template SHA-256 oracle 포함)가 red가 되면 = prompt 조립 표면이 이동 중 변한 것
   — 즉시 중단·역추적 (테스트 개정으로 우회 금지).

## 테스트 분할 (test-ID 보존)

- run_tests.py의 클래스들을 클러스터 단위로 `scripts/tests/test_<area>.py`로 `git mv` 수준
  분할(공유 fixture/helper는 `scripts/tests/support.py`로 — import만, 복제 금지).
- test-ID는 `Class.test_method`로 pin돼 있어 파일 이동은 manifest 불변. 클래스명·메서드명
  변경 금지.
- `run_tests.py`는 **집계 진입점으로 잔존**(전 모듈 로드 + 기존 CLI 인자 계약 유지 — 표적
  게이트 호출 방식 `run_tests.py <Class>[.<method>]` 불변).
- 분할 후 게이트: manifest 830 전수 green + AST 전수 대조 = 830.

## wave 분해 (task 등록, 병렬성·hot-file 단독 소유)

| task | 내용 | 소유 파일 |
|---|---|---|
| chore/m1a-package-skeleton | waystone/ 골격+composition root+`waystone.cli` dispatch 이식, bin 전환 | waystone/* 신설, bin/, scripts/waystone.py |
| chore/m1a-core-project-split | common.py 분해 이동(core/project/adapters.git) + 잔류 모듈 adapter 커밋 | scripts/common.py→waystone/, 잔류 모듈 import 행 |
| chore/m1a-registry-move | tasks.py→waystone/project (round text-surgery 의존 경계 유지) | scripts/tasks.py→waystone/ |
| chore/m1a-runs-move | delegate.py→waystone/runs + git 헬퍼 adapters 정렬 | scripts/delegate.py→waystone/ |
| chore/m1a-test-suite-split | run_tests.py 클러스터 분할 (단독 소유, **마지막 순차**) | scripts/tests/* |

의존: skeleton → {core-project-split} → {registry-move, runs-move 병렬} → test-suite-split.
(core-project-split이 공유 기저라 선행; registry·runs는 파일 소유 분리로 병렬 가능.)

## Exit (ADR-0014 Amendment 1 ③ + Amendment 2 ②)

1. 분할 완료(위 5 task) + import 방향 검증(core 상방 import 0 — 기계 검사 스크립트).
2. manifest 830 전수 green (suite identity 불변 또는 approved-diffs 기재).
3. known-debt 대비 신규 invariant 위반 0 (I-10 특성화·E-08/E-09 고정 테스트가 감시).
4. front door(`bin/waystone`) 관측 동작 불변 — 표적 스모크(task list/show·delegate 경로 1회).

## 실행 시점

이 wave는 **2026-07-20-review-remediation 리뷰 회신 triage 후** 발사한다 — 회신 finding이 방금
머지된 코드의 수리를 요구하면 대형 파일 이동과 충돌하므로(hot-file 전면 교차), 순서 고정.
