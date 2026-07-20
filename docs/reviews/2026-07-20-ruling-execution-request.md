# Review Request — 2026-07-20-ruling-execution

The reviewer has the repository via git. This is a domain/code review, not a workflow audit —
keep the waystone harness out of scope unless asked.

- Project: waystone
- Branch: dev
- Reviewer: chatgpt:gpt-5.6-pro
- Reviewing: 1f7d942b418025296704ff5bac4a13ac54d00ca5   (diff against 197b2cfa643f16188111e68a2c2f2255efd481c7)

<!-- Keep the Reviewing field on exactly one line with the literal spacing shown above. -->

## What changed and why

사용자 ruling 집행 라운드다. 오전 라운드가 M0 exit 적대 리뷰를 FAIL(검증 후 blocker 2/major 4/minor 5)로 판정했고, 사용자가 노선 B(0.12는 재설계 — 합격 기준을 legacy 출력 동등성에서 invariants+accepted ADR+승격 계약으로 전환, legacy 828 suite 기본 폐기, git-tracked 기록 연속성은 승격 원칙으로 보존)를 확정했다. 이 라운드는 그 집행이다: ADR-0013(위협모델 완결)·ADR-0014+승격 계약 30건(기준 전환)으로 blocker 2건을 닫고, ADR-0006 Amendment·run id UUIDv7 단일화·doc-sync 6항목으로 major/minor를 닫고, settlement marker(역사 회신 3건 정착)와 migration sunset(-1661줄, 자동 이관→typed 거부)으로 나머지 ruling을 집행했다. 경량 재심이 병렬 문서 간 교차 blocker(WS-RX-1: ADR-0014 exit와 계획 M1-A 범위의 동시 성립 불가)를 적발해 ADR-0014 Amendment(단계별 gate 귀속)로 해소했다.

## Read these first

- `docs/meta/agent-reports-2026-07-20/w2-reexam.md` — 재심 판정표(원 10행 해소 9+전환 소멸 1)와 WS-RX-1..4
- `docs/adr/ADR-0014-m1a-acceptance-basis.md` — 기준 전환 결정문과 **Amendment(단계별 gate)** — 이 라운드의 최대 결정
- `docs/promoted-contracts.md` — 승격 계약 30건(confirmed v1) + 신규 의무 8건 + 비승격 전수 절
- `docs/adr/ADR-0013-operational-threat-model.md` — 신규 3축(env allowlist·lease principal·permission/symlink)
- `PROGRESS.md`의 `2026-07-20-ruling-execution` 절 — 커밋·게이트 포인터
- `scripts/common.py` diff (sunset) · `scripts/review.py` diff (settlement) — 코드 변경 2건

## Claims to attack

1. **ADR-0014 Amendment의 M1-A exit ③(현행 suite green)**: "출력 comparator 부활이 아니라 동작 무변경 단계의 자기 신호"라는 구분이 실제로 유지되는가 — M1-A 구현자가 suite를 green으로 만들기 위해 노선 B가 금지한 legacy 모사를 하게 되는 뒷문은 없는가.
2. **승격 계약 30건의 완전성**: 비승격 절이 85클래스를 전수 커버한다지만, ②(깨지면 조용한 데이터 훼손) 기준으로 승격됐어야 할 의미가 비승격 군에 섞여 있지 않은가.
3. **settlement marker의 fail-closed**: marker namespace의 비정규 JSON 처리(전체 무효화)와 중복 우회 봉쇄가 충분한가 — verbatim 원본 보존 원칙과 충돌하는 경로는 없는가.
4. **migration sunset의 detector 경계**: 보존된 0.11 host seed는 수용하고 project residue는 거부하는 경계가 실제 사용자 배치에서 오탐/누락 없이 갈리는가.
5. **ADR-0013 lease principal**: owner_token+fencing_epoch+entity_version CAS가 ADR-0002/0003과 정합하다는 재심 판정을 재검증하라 — 특히 OS lock과 DB lease의 이중 검증 순서.
6. **재심 자체의 맹점**: 재심은 문서 교차 모순에 집중했다 — 문서와 **현행 코드**의 모순(예: 오늘 삭제된 migration이 계획 어딘가에 전제로 남음)은 덜 봤다.

## Evidence already produced (mine — inspect, don't trust)

- 기별 보고서 8부(`docs/meta/agent-reports-2026-07-20/w2-*.md`) — VERIFIED 헤더에 명령 원문+rc.
- RED-first: settlement(marker 전 pending 3건 assert → 구현 전 red → green), sunset(자동 이관 green → 거부 red → green).
- digest 증거: settlement 원 cohort 9개 + 감사 9개 SHA-256 재계산, manifest Amendment의 기존 본문 byte-불변 SHA, 재심의 E-09 문자 단위 비교(equal=true, 760 chars).
- full gate: sunset 머지 후 817 rc=0, RX 수리 후 817 rc=0(로그 scratchpad, 명령 원문 PROGRESS).
- 재심 판정표: 원 exit 기준 10행 각각의 현재 anchor.

## Known weak spots

- **자기 검증 순환**: 재심 codex는 내(main)가 브리핑했고, WS-RX-1의 해소책(Amendment)도 내가 설계·자기 검수했다 — 3자 검증이 없다. 이 리뷰가 그 3자다.
- **M1-A exit ③의 테스트 수 드리프트**: "현행 suite"는 817로 움직이는 기준이다(이번 라운드에만 828→817). Amendment는 개별 테스트 폐기를 재구축 마일스톤 규칙에 맡겼지만, M1-A 도중의 suite 변동 규칙은 명시하지 않았다.
- **승격 목록은 클래스 단위 채굴**이다 — 행 단위 정밀 검증은 각 마일스톤 분해 시점으로 미뤄져 있다.
- **settlement 추가 동종 3건**은 감사만 됐고 처분 미결(decision/legacy-settlement-additional-cohort).
- sunset의 detector는 이 머신 배치로만 실검증됐다 — 타 머신 배치는 typed 거부 메시지가 안내할 뿐이다.

## Domain lens

개발 운영 하네스(Claude Code+Codex 플러그인). 이 라운드의 핵심 리스크는 **acceptance 체계 자체를 갈아끼운 것**이다 — 잘못되면 이후 모든 마일스톤이 잘못된 기준으로 통과한다. 특히 ADR-0014 본문과 Amendment의 관계(본문 :30-31을 Amendment :4가 한정)가 후속 독자에게 단일한 계약으로 읽히는지, 그리고 "재설계의 자유"와 "기록 연속성 보존"의 경계가 PC 행들에서 실제로 집행 가능한 문구인지를 의심해 달라.

## Response wanted

Start the reply with this block (replace values; key case/order/spacing and a Markdown fence are
optional; extra keys are preserved). Echo the `Reviewing` target, alone or as a 12–40 hex
`base-target` range, and copy the request digest exactly; missing/damaged values stay unknown, and
no model/target means ordinary prose:
```text
model: chatgpt:gpt-5.6-pro
effort: high
review-target: 1f7d942b418025296704ff5bac4a13ac54d00ca5
request-digest: sha256:c4d08a80fae8876813d79af615199860228c398b43958efeb7fb650de51859ff
```

Major / critical issues only. For each: a concrete failure mechanism and where you confirmed it.
Separate confirmed findings, open domain questions, and residual risks from unavailable
GPU / data / environment.
