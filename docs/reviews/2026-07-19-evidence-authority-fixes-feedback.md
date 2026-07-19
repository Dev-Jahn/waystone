<!-- waystone feedback: the body below is the reviewer reply VERBATIM (byte-exact copy via `waystone review ingest`) — do not edit it; a triage skeleton is appended beneath it. -->
round: 2026-07-19-evidence-authority-fixes
reviewer: gpt-5.6-sol
reviewer-effort: xhigh
review-target: b64b2f857e26e6eef23e50a9ffbca8a2c8cb3ee5
reply-metadata-json: {"metadata":{"effort":"xhigh","model":"gpt-5.6-sol","review-target":"b64b2f857e26e6eef23e50a9ffbca8a2c8cb3ee5"},"rendered_request_coverage_reason":"request-digest-missing","rendered_request_digest_matches":null}
ingested: 2026-07-19
source: /tmp/review.md
verbatim-bytes: 4989

---

```text
model: gpt-5.6-sol
effort: xhigh
review-target: b64b2f857e26e6eef23e50a9ffbca8a2c8cb3ee5
```

## 직전 3건 상태

| 항목 | 판정 | 근거 |
|---|---|---|
| JW-GPT-011 | still-broken | `classify()`와 merge gate는 late-v1을 차단하지만, `merge`가 관측한 supersession은 sidecar로 영속화되지 않아 이후 offline projection이 기존 v2 digest를 다시 explicit으로 주장한다. |
| JW-GPT-012 | new-concern | corrupt latest-cycle의 stale fallback은 차단됐지만, `-freeze-`를 포함한 타 round의 malformed filename이 healthy round의 ingest를 중단시키는 새 cross-round 경로가 남았다. |
| JW-GPT-013 | resolved | `config.toml` content digest가 comparison 축에 포함되고 directory stat만 진단에서 제외되며, unreadable 상태는 reuse blocker가 되고 probe 후 재측정도 유지된다(`scripts/delegate.py:1169-1182,1316-1321,1362-1372,1521-1528`). |

## Confirmed findings

### JW-GPT-014 — merge가 관측한 v1 supersession이 offline projection에 영속되지 않음

#### 실패 메커니즘

1. 로컬에는 cycle N의 v2 freeze sidecar와 request generation A가 있고, GitHub에는 같은 cycle의 더 늦은 trusted v1 marker가 존재한다.
2. `waystone merge --pr N`은 `facts_from_bundle()`을 통해 remote marker를 읽고, `merge_gate()`가 `cycle_version_skew_reason`을 소비하여 merge를 차단한다(`scripts/merge.py:130-177,180-196`; `scripts/merge.py:60-63`).
3. 그러나 이 online 관측은 demotion sidecar를 기록하지 않는다. Production에서 `write_pr_freeze_demotion()`을 호출하는 곳은 `review status` 경로뿐이다(`scripts/review.py:2448-2488`).
4. 따라서 `merge` 직후 offline `ingest_round_binding()`은 여전히 로컬 v2 sidecar만 보며 `persisted_demotion=False`로 generation A를 복구한다(`scripts/review.py:646-655,682-710`). `improve._review_binding()`도 같은 상태에서 A를 `request_provenance: explicit`으로 반환한다(`scripts/improve.py:1017-1023,1039-1078`).

즉 supersession을 실제로 관측한 지원 CLI 경로를 거쳤는데도 online은 unknown, offline은 explicit exact-generation으로 갈라진다.

#### 필수 수정

trusted remote cycle demotion의 검출·영속화를 공용 함수로 만들고, `status`뿐 아니라 `merge`처럼 remote classification을 수행하는 모든 관련 online 경로가 이를 호출해야 한다. 관측한 demotion을 기록하지 못한 경우에도 이후 offline projection이 기존 v2 digest를 authoritative로 재주장하지 못하도록 해야 한다.

### JW-GPT-015 — foreign round의 malformed freeze filename이 healthy round ingest를 차단함

#### 실패 메커니즘

1. healthy round를 `R = 2026-07-19-a`, prefix가 겹치는 foreign round를 `F = 2026-07-19-a-freeze-b`라고 한다.
2. R에는 정상 freeze sidecar가 있고, F에는 filename이 손상된 `F-freeze-latest.binding.json`이 존재한다.
3. 이 파일은 R의 glob `R-freeze-*.binding*.json`에도 포함된다(`scripts/review.py:590`).
4. 숫자 cycle이 아니므로 `pr_freeze_binding_identity()`는 `None`을 반환한다. `ingest_round_binding(R)`은 foreign 여부를 판별하는 다음 분기에 도달하기 전에 R 자체를 corrupt로 즉시 반환한다(`scripts/review.py:595-599`).
5. 반면 `improve._round_review_sidecars()`는 동일 filename을 마지막 `-freeze-` 기준으로 F에 격리한다(`scripts/improve.py:1164-1182`). 따라서 healthy R에 대해 improve는 정상 projection을 유지하지만 ingest는 중단된다.

이는 한 foreign round의 damaged/adversarial filename이 다른 round의 projection을 중단시키는 구체적인 cross-round bleed다.

#### 필수 수정

`ingest_round_binding()`도 malformed filename의 fallback round identity를 `improve`와 동일한 규칙으로 먼저 파생해야 한다. 파생된 owner가 요청 round와 다르면 skip하고, 같은 round일 때만 corrupt로 처리해야 한다. Freeze와 demotion glob을 공통 라우팅 함수로 열거해 두 projection의 owner 판정을 일원화하는 것이 필요하다.

## Open domain questions

- 요청된 full SHA `b64b2f857e26e6eef23e50a9ffbca8a2c8cb3ee5`는 이 저장소에 존재하지 않는다. 실제 `b64b2f8`은 `b64b2f86639b1de3b88e178b859c03baa4c312aa`로 해석되므로, 코드 판정은 해당 round-closeout object를 대상으로 수행했다.

## Residual risks from unavailable environment

- `unverified-static`: 지시에 따라 테스트 스위트를 실행하지 않았다.
- `unverified-static`: JW-GPT-014의 실제 mixed-host GitHub marker → merge → offline projection 순서는 실행하지 않고 호출 경로를 정적으로 추적했다.
- `unverified-static`: JW-GPT-015의 malformed cross-round filename fixture는 실행하지 않고 glob·regex·분기 순서를 정적으로 추적했다.
- `unverified-static`: 실제 Codex 실행을 통한 config mutation 및 probe self-churn은 실행하지 않았다.



---

<!-- waystone triage: BEGIN -->
## Findings (triage skeleton — verify each before registering)

| finding | severity | type | verdict (REAL/REJECTED/NEEDS-RULING) | evidence | task id |
|---|---|---|---|---|---|
| JW-GPT-014 — merge가 관측한 v1 supersession이 offline projection에 영속되지 않음 | major | correctness | REAL | 직접 확인: `write_pr_freeze_demotion` 호출처는 production 코드에서 `review.py:2476`(=`status()`, def @2428) 단 한 곳. `merge.py:60-63`은 `cycle_version_skew_reason`을 차단 사유로 소비만 하고 영속화하지 않음 → merge가 supersession을 관측한 뒤에도 offline `ingest_round_binding`/`improve._review_binding`은 로컬 v2 sidecar만 보고 explicit 재주장. 요청서 claim 2(online↔offline 일치) 반증됨. merge gate 자체는 여전히 fail-closed(차단은 성립) — 결함은 증거 투영의 online/offline 분기 | fix/merge-observed-demotion-persistence |
| JW-GPT-015 — foreign round의 malformed freeze filename이 healthy round ingest를 차단함 | major | correctness | REAL | 직접 확인: `review.py:595-599`에서 `pr_freeze_binding_identity()`가 None이면 `identity[0] != round_id` foreign-skip 분기에 도달하기 전에 즉시 `corrupt-round-binding` 반환. glob `{round}-freeze-*.binding*.json`(review.py:590)은 prefix가 겹치는 foreign round의 파일까지 포함하므로 타 round의 손상 파일 1개가 healthy round의 ingest를 중단시킴. 바로 아래 demotion 루프는 반대로 `identity is not None and identity[0] != round_id: continue`로 관대 — 같은 파일군에 두 규칙 공존. `improve._round_review_sidecars`(improve.py:1164-1182)는 마지막 `-freeze-` 기준으로 격리하므로 두 투영이 갈림 = 012가 닫으려던 부류의 잔존 진입로 | fix/ingest-malformed-foreign-freeze-skip |
<!-- waystone triage: END -->
