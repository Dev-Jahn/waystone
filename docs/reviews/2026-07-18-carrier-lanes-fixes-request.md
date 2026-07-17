# Review Request — 2026-07-18-carrier-lanes-fixes

The reviewer has the repository via git. This is a domain/code review — 이 repo의 도메인은 플러그인 하네스 자체이므로 아래 변경의 코드·설계 타당성을 본다. **직전 라운드(2026-07-18-carrier-lanes) 회신의 재검토 라운드다** — 당신이 CHANGES REQUESTED로 지적한 major 4건 + 승격된 open question 1건이 전부 이 diff에서 해소되었는지가 핵심 질문이다.

- Project / Branch: waystone / dev
- Reviewing: 4c042031af9fe1722676de8bbe41fccba5464b30   (diff against e9e5c140947375f3a55cc9b8c2c681ff6c458da4)

## What changed and why

직전 회신의 지적 5건(JW-GPT-001/002/003/004 + Q1 승격)을 3개 lane으로 해소했다. 002·003은 직전 라운드 마감 직후 착지(diff base 이후 첫 커밋들), 이번 diff의 본체는 나머지 3건이다:

1. **프로브 증명 머신 격리 (Q1 승격分)** — codex sandbox 프로브의 1회-검증 증명을 커밋 추적 `.waystone.yml`(`delegation.codex_runner_verified`)에서 **미커밋 per-checkout 마커** `.waystone/codex-runner-verified`로 이동. legacy 키는 조용히 수용하지 않고 무시+제거 안내(디스크 config를 실제 읽은 로드 경로에서만 발화). 마커는 **untracked이고 내용이 계약값일 때만** 증명으로 인정 — 커밋돼 전파된 마커는 무시+untrack 안내 후 재프로브. `.waystone` self-ignore는 복원적·원자적(빈/훼손/symlink `.gitignore` 복구, legacy migration 경로도 단일 헬퍼 경유). probe-once는 전용 flock으로, lock 하 재확인 자체를 barrier 게이팅 동시성 테스트가 고정.
2. **narrative digest 결속 (JW-GPT-004)** — round request binding에 canonical narrative digest를 포함. narrative만 바뀐 같은-target 재-prepare가 새 sidecar를 발행하고 pending을 재개(RED). freeze와 packet publication 게이트 모두 exposure+보존 narrative에서 request를 재렌더해 대조 — request.md의 narrative만 변조한 packet은 거부(RED). digest 없는 legacy binding은 **양측 digestless일 때만** `legacy-pre-digest` 라벨 폴백(binding에서 digest만 제거한 위장은 feedback의 도장이 남아 pending 유지, RED). publication 게이트는 digest 하드 요구. improve 투영도 digest와 전용 `narrative_coverage_reason`을 보존.
3. **publication 증명 라이브화 (JW-GPT-001)** — persisted `refs/remotes/*` 신뢰를 `remote verify`와 `head_pushed` 양쪽에서 제거. 증명은 정확한 upstream branch를 **명시 command-line refspec으로 pid+uuid 전용 임시 ref에 fetch**해 원자 고정(공유 FETCH_HEAD는 증거로 읽지 않음 — 동시 fetch 경합 무영향; cleanup 실패도 fail-closed). 원격 branch 삭제·fetch refspec 제외 양쪽 모두 거부(RED — 실 bare-remote, publisher/validator/mutator clone 분리). fetch 실패는 `ls-remote --exit-code`로 부재 vs 네트워크를 구분. upstream remote `'.'`(로컬)은 거부. freeze의 binding↔exposure 교차검증을 packet 게이트와 동등화(reviewers/mode 포함).

각 lane은 external-runner(codex:gpt-5.6-sol, xhigh)가 구현하고, 매 attempt마다 host 스위트+ruff 실측과 raw codex 적대 리뷰(xhigh, read-only)를 통과해야 인수됐다 — 1·2번은 attempt-3, 3번은 attempt-2에서 인수. 전 회전의 기각 사유와 판정 근거는 delegation record의 verdict artifact에 남아 있다.

## Read these first

1. `scripts/common.py` — `_ensure_project_self_ignore` / `fetch_upstream_head` / `ancestry_status` / `head_pushed` (신뢰 표면의 공통 기반)
2. `scripts/delegate.py` — `_codex_runner_marker_recorded` / `_record_codex_runner_verified` / `_run_codex`의 probe-once lock 경로
3. `scripts/review.py` — `write_round_request_binding` / `_assess_narrative_digest` / `_reproduce_bound_review_request`(이름은 다를 수 있음 — freeze·`verify_packet_publication`의 재렌더 대조부) / `pending_reviews` 완료 판정
4. `scripts/improve.py` — `_review_binding`의 digest 보존
5. `docs/reviews/2026-07-18-carrier-lanes-feedback.md` — 당신의 원 지적(대조용)

## Claims to attack

1. 커밋 추적 파일 어디에도 프로브 증명이 남지 않으며, 어떤 경로로 전파된 증명도(커밋된 legacy 키·커밋된 마커) 다른 머신의 프로브를 생략시키지 못한다.
2. 같은 target에 narrative만 바꿔 재-prepare하면 round는 반드시 pending으로 재개되고, 구 feedback·구 sidecar 재사용으로 완료 처리될 수 있는 경로가 없다 (단, ingest 시점 도장의 재-prepare race 1건은 알려진 잔여 — Known weak spots 참조).
3. packet이든 PR이든, 게시되는 request 내용은 prepare가 검증한 입력(exposure+보존 narrative)의 결정적 재렌더와 일치할 때만 나간다 — request.md·exposure 단독 변조는 게이트가 거부한다.
4. 원격에 실제로 존재하지 않는 branch/commit을 published로 판정할 수 있는 경로가 없다 — stale tracking ref, fetch refspec 제외, FETCH_HEAD 경합, 로컬 `'.'` remote 전부 차단.
5. legacy(digest 없는 binding) 라운드의 완료는 항상 `legacy-pre-digest`로 식별 가능하고, digest-strip으로 신규 라운드를 legacy로 위장할 수 없다.

## Evidence already produced (mine — inspect, don't trust)

| Claim | Command / artifact | My reading | Where it lives |
|---|---|---|---|
| 전체 무회귀 | `uv run scripts/tests/run_tests.py` | 730→748 green (874c4c3에서 740, 4673b58에서 748) + ruff F401/F841 clean | PROGRESS `2026-07-18-carrier-lanes-fixes` Gates |
| RED-first 계약 | 각 delegation record의 `delegate_report.verification` | 신규 계약 전건 사전 rc=1 재현 후 green | `.waystone/delegations/20260717T20*/artifact/contract.yaml` (미커밋 로컬 티어 — 요청 시 발췌 제공) |
| attempt별 기각·인수 근거 | verdict artifact | attempt 기각 사유가 회전마다 신규·수렴(격리 결함→위생→진단 폴리시) | 동 record `artifact/verdict-1.json` |

## Known weak spots

1. **ingest 시점 도장 race (알려진 잔여, 별도 task)** — 회신은 model/effort/review-target만 자기증언하므로, ingest는 그 시점의 binding digest를 도장한다. prepare(A)→리뷰 중 재-prepare(B)→A 기반 회신 도착 순서에서는 B 완료로 오판 가능. 해소는 reply 헤더 계약 변경(request-digest 에코)이 필요해 `fix/reply-narrative-echo`(major)로 분리, 사용자 ruling 대기.
2. **shallow 경계의 ancestry rc=1** — `merge-base --is-ancestor`의 fatal 실패는 판정 불가로 정직 보고하지만, shallow 경계에서 rc=1로 조용히 끝나는 위상은 여전히 '미포함'으로 보고(fail-closed 방향 — false PASS는 불가). verdict에 override-unmet으로 명시 기록, `fix/shallow-ancestry-honesty`(minor) 등록.
3. **역사 라운드 3건의 영구 pending** — 0.10 구조화 헤더 이전 형식의 feedback은 완료 판정에 도달하지 못한다. 이 diff의 회귀가 아니라 기존 부채임을 패치 전 코드로 재현 확인(`chore/pre-header-feedback-settlement` 등록).

## Domain lens

신뢰 표면의 fail-direction 일관성: 이 라운드의 모든 변경은 "의심스러우면 증명을 다시 만들거나(fail-toward-probe) 게시를 거부(fail-closed)"여야 한다. 어느 경로든 fail-open(증명 생략·거짓 published·조용한 완료)을 찾으면 그것이 major다.

## Out of scope

`dev_docs/`(로컬 설계 노트), 직전 라운드 tip(e9e5c140) 이전 이력, 이 문서를 만든 워크플로 절차, `fix/reply-narrative-echo`가 다룰 reply 계약 확장의 설계.

## Response wanted

Start the reply with this block (replace values; key case/order/spacing and a Markdown fence are
optional; extra keys are preserved). Echo the `Reviewing` target, alone or as a 12–40 hex
`base-target` range; missing/damaged values stay unknown, and no model/target means ordinary prose:
```text
model: <model-id>
effort: <effort>
review-target: 4c042031af9fe1722676de8bbe41fccba5464b30
```

Major / critical issues only. For each: a concrete failure mechanism and where you confirmed it. Separate confirmed findings, open domain questions, and residual risks from unavailable environment. 직전 회신의 5건 각각에 대해 resolved / still-broken / new-concern 판정을 명시해달라.
