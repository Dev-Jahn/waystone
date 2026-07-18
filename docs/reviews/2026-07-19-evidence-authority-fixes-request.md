# Review Request — 2026-07-19-evidence-authority-fixes

The reviewer has the repository via git. This is a domain/code review, not a workflow audit —
keep the waystone harness out of scope unless asked.

- Project: waystone
- Branch: dev
- Reviewer: chatgpt:gpt-5.6-pro
- Reviewing: b64b2f86639b1de3b88e178b859c03baa4c312aa   (diff against 5bf3f9038b47e817091606a39afb72e7c3d4ad90)

<!-- Keep the Reviewing field on exactly one line with the literal spacing shown above. -->

# Review Request — 2026-07-19-evidence-authority-fixes

This is a domain/code review of the waystone harness itself. **4차 리뷰(codex 대체 리뷰, REAL major 3: JW-GPT-011~013)의 5차 라운드다** — 그 3건이 전부 해소되었는지가 핵심 질문이다.

> **전체 라운드 diff 창: `2e0f1fb..b64b2f8`** (직전 라운드 2026-07-19-evidence-authority의 tip 기준). 헤더의 diff base가 `5bf3f90`으로 표기되어 있다면 그것은 같은 round의 중간 closeout 커밋이다 — 재close 시 base 표기 drift(minor로 등록: fix/reclose-diff-base-drift). 리뷰는 위 전체 창을 봐달라.

## What changed and why

4차 회신의 major 3건을 3개 lane으로 해소했다(0.11.1 hotfix 라인). 운영 특기: 0.11.0 하네스의 probe가 자기 실행이 유발하는 `~/.codex` 디렉터리 stat 변화를 스스로 거부해 `delegate run`이 전면 불가였고(013과 동일 근원의 실사고), 전 lane을 raw codex exec으로 우회하되 검증 규율(게이트 주관측 재실측·RED 독립 재현·적대 리뷰 회전)은 동등하게 유지했다.

1. **probe config 내용 결속 (JW-GPT-013)** — fingerprint가 config root **디렉터리 stat**(내용 변경에 둔감, 자기 churn에 과민) 대신 `CODEX_HOME/config.toml`의 **내용 digest**를 신뢰 축으로 결속한다. 디렉터리 stat은 진단 기록으로 강등되어 신뢰 대조와 probe-중-변화 거부에서 제외된다(안정 축의 거부는 유지). absent는 정직 기록 + 양쪽 absent 상태 동등(macOS probe-once 보존), 존재-불가독은 fail-toward-probe, marker에는 digest만 저장(원문 금지). 실사고(probe 영구 실패)도 이 수정으로 해소.
2. **v1 supersession 정직화 (JW-GPT-011)** — 공유 selector가 same-cycle의 진짜 최신 marker를 고르고, 최신이 v1이면 v2 digest 권위를 강등한다(3면 동일 규칙: classify·ingest_round_binding·improve._review_binding). completion event와 merge gate가 skew 사유를 차단 조건으로 소비한다. mixed-host 갭은 **demotion sidecar**(strict filename↔content identity, 로컬 v2 contract에 결속, 원자 쓰기)로 폐쇄 — GitHub에서 관측된 supersession이 로컬에 영속화되어 offline 투영이 explicit digest를 재주장하지 못한다. **freeze CLI는 `--round` 필수가 되었고 v1 marker를 신규 발행하지 않는다**(digest-capability 탐지는 host-local 증거 의존이라 durable할 수 없음이 리뷰 회전에서 입증되어 탐지 자체를 제거). post-cutoff round_id의 v1 marker는 stream에 v2가 없어도 digest-era 차단. tie·미파싱 timestamp 혼재는 fail-closed.
3. **freeze sidecar 손상 격리 (JW-GPT-012)** — corrupt freeze sidecar는 (request sidecar·demotion과 동형으로) filename identity 기반 sentinel로 보존되고, 최신 cycle이 corrupt면 그 round는 honest-unknown이다 — 이전 cycle이 explicit로 승격되는 fallback 폐쇄. content↔filename round/cycle 불일치는 corrupt. cross-round prefix 충돌(round id에 '-freeze-' 포함 가능)은 foreign-skip으로 격리되어 improve/ingest 투영이 동일 디렉터리에서 같은 판정을 낸다.

각 lane은 매 attempt 실측(스위트+ruff 주관측 재실측, rc 직접 캡처)과 RED 독립 재현, 설계 회전의 codex 적대 리뷰(xhigh)를 거쳤다: 013 1회전 인수(major 0) · 011 3회전(REJECT 2 → REJECT 1 → ACCEPT-WITH-NOTES 0) · 012 2회전(REJECT 1 → 12줄 기계 델타는 비례성 규칙으로 main 직접 검증, 기록됨).

## Read these first

1. `scripts/delegate.py` — `_codex_config_root_identity`(config_toml digest) / `_codex_runner_comparison_view`(디렉터리 stat 제외) / `_codex_runner_reuse_blockers`
2. `scripts/review.py` — `select_latest_review_cycle_row` / `classify()`의 supersession·digest-era 사유 / `freeze()`(--round 필수) / demotion sidecar 3종 함수 / `read_pr_freeze_binding`(filename identity) / `ingest_round_binding`(foreign-skip·corrupt-latest unknown)
3. `scripts/improve.py` — `_round_review_sidecars`(corrupt sentinel) / `_review_binding`(demotion 소비·corrupt latest unknown)
4. `scripts/merge.py` — skew 사유 차단
5. `docs/reviews/2026-07-19-evidence-authority-feedback.md` — 4차 회신 원문+triage(대조용)

## Claims to attack

1. 신형 CLI가 v1 review-cycle marker를 발행하는 경로는 존재하지 않으며, 구식 host의 v1 재freeze는 same-cycle·후속-cycle·타-host 어느 변형에서도 v2 digest를 오귀속시키거나 merge gate를 통과시키지 못한다.
2. 완료·증거 투영의 어떤 표면도 supersession 이후 explicit exact-generation을 주장하지 않는다 — online(classify)과 offline(sidecar 투영)이 일치한다.
3. 한 파일의 손상·이름 충돌이 (a) stale 증거를 승격시키지 못하고 (b) 타 round의 투영을 중단시키지 못한다.
4. probe proof는 config 내용이 바뀌면 재사용되지 않고, probe 자신의 부작용으로는 실패하지 않는다 — probe-once(불변 환경 재사용)와 fail-toward-probe(불확실 시 재프로브)가 동시에 성립한다.

## Evidence already produced (mine — inspect, don't trust)

| Claim | Command / artifact | My reading | Where it lives |
|---|---|---|---|
| 전체 무회귀 | `env -u FORCE_COLOR uv run scripts/tests/run_tests.py` | 807→828 green + ruff clean (매 attempt·매 병합 재실측) | PROGRESS `2026-07-19-evidence-authority-fixes` Gates |
| RED 진위 | base/직전-attempt 코드 + 신규 테스트 재실행 | 전 attempt에서 신규 계약 테스트 사전 실패 재현 | PROGRESS 동 entry |
| 회전별 기각·반박 | 적대 리뷰 원문 | major 단조 수렴(013: 0 / 011: 2→1→0 / 012: 1→0) | scratchpad verdict 파일들, 요지는 커밋 메시지 |

## Known weak spots

1. **수용 residual 4건(비차단, 문서화됨)**: digest-era-v1-freeze 사유의 demotion 미영속(감사 parity — gate 무영향) · demotion 쓰기의 동시 writer TOCTOU(단일 사용자 모델) · crafted demotion의 fail-closed DoS · unreadable 파일 rename 위조(filename=identity 계약) — 뒤 둘은 decision/trust-threat-model-boundary ruling 범위.
2. **raw 우회로 인한 delegation record 부재** — 이 라운드의 attempt 이력은 waystone delegation record가 아니라 커밋 메시지·PROGRESS·registry result에 있다(하네스 버그가 대상 그 자체였던 자기참조 상황의 의도된 우회).
3. **config 결속 범위는 config.toml 최소 계약** — config.d/profiles/auth 입력은 미결속(계약에 명시).

## Domain lens

직전과 동일 — 신뢰 표면의 fail-direction. 특히 이번엔 **"양방향 정직성"**: 013은 과민(자기 churn 거부)과 둔감(내용 미감지)을 동시에 고치는 수정이고, 011·012는 fail-closed가 위양성(가짜 corrupt·과잉 차단·cross-round 오염)으로 넘어지지 않으면서 위음성(stale 승격·오귀속)을 막는지를 본다.

회신에는 직전 회신의 3건(JW-GPT-011~013) 각각에 대해 resolved / still-broken / new-concern 판정을 명시해달라. 범위 밖: `dev_docs/`, 직전 tip(2e0f1fb) 이전 이력, 워크플로 절차, 문서화된 수용 residual의 재발견(새 진입 경로가 아니면), decision/trust-threat-model-boundary에 위양된 변형들, 0.12 refactor plan.

## Response wanted

Start the reply with this block (replace values; key case/order/spacing and a Markdown fence are
optional; extra keys are preserved). Echo the `Reviewing` target, alone or as a 12–40 hex
`base-target` range, and copy the request digest exactly; missing/damaged values stay unknown, and
no model/target means ordinary prose:
```text
model: chatgpt:gpt-5.6-pro
effort: high
review-target: b64b2f86639b1de3b88e178b859c03baa4c312aa
request-digest: sha256:db4e6c1dcf6f0d1d284c58ac0500627d37b6f75c9f7ca8b2b2d6f0cccc01e9b9
```

Major / critical issues only. For each: a concrete failure mechanism and where you confirmed it.
Separate confirmed findings, open domain questions, and residual risks from unavailable
GPU / data / environment.
