# Runtime state disposition audit

작성 시각: 2026-07-19T12:57:04Z

대상 Git base: `b90c06392ae4a4aa4c5412b73fd8647614752d72`

## 결론

### 감사 시점 원결과 (2026-07-19)

계획서 §5-1의 원칙, 즉 **현재 권위가 있는 항목은 Git에 있거나 다른 권위 원천에서 재파생
가능해야 한다**는 기준을 실물에 적용한 결과 위반은 **6건**이었다. 모두 아래 `M0 finding`에
명시했다. 원감사는 finding만 기록했으며 task를 등록하거나 runtime state를 변경하지 않았다.

`.waystone/`의 untracked 상태 자체는 finding이 아니다. 현재 의사결정·감사 사실의 권위가 그
경로에 있고, 해당 bytes 또는 같은 의미의 값을 Git이나 다른 권위 채널에서 결정적으로 복구할 수
없는 경우만 finding이다. 이 기준 때문에 projection, cache, OS lock은 exact bytes를 재생성할 수
없더라도 authority finding으로 분류하지 않았다.

### 현 처분 (2026-07-20 main 판정)

후속 main 판정에서 감사 시점 위반 6건은 **task 2건(F-01·F-06) + 명시 수용
4건(F-02~F-05)**으로 처분됐다. 세부 근거와 수용 조건은 아래
[Finding 처분 (main 판정 2026-07-20)](#finding-처분-main-판정-2026-07-20)을 따른다.

## 범위와 판정 규칙

권위 원천은 `dev_docs/0.12.0-refactor-plan.md`의 §2-5, §4, §5-1, §6 M0-C/M1-A,
`docs/invariants.md`, `docs/adr/ADR-0002`~`ADR-0012`, `docs/known-issues.md`로 한정했다.
실물 판정에는 파일 존재, 형식, Git tracking 여부와 현행 reader/writer가 관측한 역할을 사용했다.

- linked implementer worktree 자체에는 `.waystone/`이 없다. `git_common_dir`와
  `~/.waystone/projects.json`을 대조해 이 repository family의 canonical root가
  `/Users/jahn/workspace/waystone`임을 확인했고, ADR-0011에 따라 그 root의 `.waystone/`을
  project runtime state로 감사했다.
- `git-tracked`는 canonical repository에서 `git ls-files`로 확인했다. `.waystone/` 아래 실물은
  전부 self-ignore되고, 표의 Git 유지 항목은 전부 추적된다.
- `재파생 가능`은 파일을 다시 비슷하게 쓸 수 있다는 뜻이 아니다. named authority channel에서
  필요한 의미를 결정적으로 다시 얻을 수 있어야 한다. M1-A 등급에 따라 canonical artifact는
  bytes, machine JSON은 schema/value, human handoff는 의미를 기준으로 판정했다.
- ADR-0005~0007의 SQLite와 content-addressed artifact store는 0.12의 **새 local authority**다.
  현재 legacy bytes가 장래 DB에 들어갈 수 있다는 사실만으로 현재 재파생 가능하다고 보지 않았다.
  idempotent import가 명시된 항목만 `DB 이관`으로 적었다.
- ADR-0002~0004는 lock/liveness/executor를 authority와 분리하는 데, ADR-0008·0010·0012는
  worker claim, harness fact, verifier evidence, decision을 분리하는 데 적용했다. ADR-0009는 review
  evidence의 Git authority를, ADR-0011은 canonical project 경계를 확정하는 근거로 적용했다.

## 실물 inventory

### Canonical project `.waystone/`

snapshot에는 top-level entry 12개(regular file 8, directory 4)가 있었다. symlink, socket, FIFO는
없었다.

| 경로 | 실물 |
|---|---|
| `.waystone/.gitignore` | 2 bytes, 내용 `*` |
| `.waystone/profile.yml` | `waystone-profile-1`, 6 role binding |
| `.waystone/start-here.md` | persistent human handoff projection 1개 |
| `.waystone/resume.md` | ephemeral structured re-entry projection 1개 |
| `.waystone/lock` | 현재 project advisory lock 파일 1개 |
| `.waystone/codex-runner-verification.lock` | 0-byte probe serialization lock |
| `.waystone/codex-runner-verified` | checkout/machine/principal/runtime 축 probe proof 1개 |
| `.waystone/consents.jsonl` | consent event 4행 |
| `.waystone/delegations/` | record 101개: applied 27, discarded 64, failed-env 2, needs-review 6, running 2 |
| `.waystone/exposure/` | immutable round exposure JSON 12개 |
| `.waystone/overlay/` | `review-ingests.jsonl` 1개, review-feedback event 8행; delta/warning 실물 없음 |
| `.waystone/review-requests/` | published packet round의 local narrative source 2개 |

101개 delegation record에서 발견한 직계 file class는 `claim.json` 101,
`exposure.json` 101, `packet.yaml` 101, `prompt.txt` 101, `status.json` 101,
`record.lock` 101, `runner.jsonl`/`runner.stderr` 각 99, `last_message.md` 93,
`sandbox-probe-{result.json,jsonl,stderr,last-message.md}` 각 4, `verify.stderr` 1개다.
모든 record에는 `artifact/`가 있었고 그 아래 `changes.patch` 93, `contract.yaml` 93,
`verdict-1.json` 45개가 있었다. local `refs/waystone/delegations/*`는 70개뿐이므로 Git ref도
101개 record 전체의 재파생 원천이 아니며, ref 자체도 cross-machine Git-tracked evidence가 아니다.

§5-1 표에 없지만 실물로 발견한 항목은 `.gitignore`, `resume.md`,
`codex-runner-verification.lock`, `review-requests/*`다. 이 네 항목도 아래 처분표에 추가했다.
0.12 target인 `.waystone/state.db`와 `.waystone/artifacts/`는 아직 없었다. 현행 문서에 등장하는
optional `maturity.json`, `improve/`, `boundary-hooks-enabled`도 이 snapshot에는 없었다.

### Machine tier `~/.waystone/`

| 경로 | 실물 |
|---|---|
| `~/.waystone/projects.json` | canonical project registration 1개 (`waystone`) |
| `~/.waystone/registry.lock` | machine registry advisory lock 1개 |
| `~/.waystone/cache/worktrees/` | repository slug directory 1개, linked worktree 8개 |

8개 cache worktree는 `docs-adr-runtime-core-contracts`, `docs-adr-state-authority-contracts`,
`docs-invariants-and-terminology`, `feat-run-spec-readiness-contract`,
`feat-canonical-project-identity`, `feat-review-artifact-addressing`, 이 감사 작업,
`chore-porting-ledger-bootstrap`이다. `~/.waystone/overlay/`와 `~/.waystone/improve/`는 없었다.

## 처분 대조표

`현재 권위`의 `부분`은 subtree 안에 claim/cache와 authority-bearing artifact가 함께 있다는 뜻이다.
그 경우 authority-bearing subset 하나라도 복구 불가능하면 원칙을 위반한다.

| 현행 항목 | 실물 | 현재 권위 | Git tracked | 재파생 가능 | 0.12 처분 | 근거 |
|---|---:|---|---|---|---|---|
| `.waystone.yml` | 있음 | 예: project config/policy | 예 | 예, frozen Git bytes | Git 유지 | 계획 §3-1 A층, §5-1; ADR-0005 |
| `tasks.yaml` | 있음 | 예: intent/deps/acceptance | 예 | 예, frozen Git bytes | Git 유지 | I-01; 계획 §3-1·§5-2; ADR-0005 |
| `PROGRESS.md` | 있음 | 예: Git work log | 예 | 예, Git bytes | Git 유지 | 계획 §3-1·§5-1 |
| `ROADMAP.md` | 있음 | 아니오: `tasks.yaml`의 projection | 예 | 예, `tasks.yaml`에서 의미 재생성 | Git 유지 | 계획 §3-1·§5-1 |
| `docs/reviews/*` | 25 files | 예: request/reply/binding evidence | 예 | 예, Git bytes가 authority | Git 유지; 신규는 UUID owner layout | 계획 §2-1·§5-2; E-01·E-02; ADR-0005·0009 |
| `.waystone/.gitignore` | 있음 | 아니오: self-ignore support | 아니오 | 예, 내용 `*` 재생성 | 역할 분리: state를 authority로 만들지 않는 guard 유지 | I-07; 실제 self-ignore |
| `.waystone/profile.yml` | 있음 | **예: 현재 role routing/config intent** | 아니오 | **아니오: human-authored binding을 다른 원천에서 복구 불가** | 로컬 유지; v1 adapter(M1), v2 공개(M3) | 계획 §5-1; ADR-0004·0008·0012; **F-01** |
| `.waystone/start-here.md` | 있음 | 아니오: handoff projection | 아니오 | 예, human 의미 등급으로 Git/runtime frontier에서 재생성 | 역할 분리: 유지하되 engine이 생성 | 계획 §5-1·§6 M1-A; ADR-0008 |
| `.waystone/resume.md` | 있음(§5-1 누락) | 아니오: ephemeral re-entry projection | 아니오 | 예, HEAD/tasks/round 상태에서 normalized 재생성 | 역할 분리: handoff projection | E-08; ADR-0003·0008 |
| `.waystone/delegations/*` | 101 records | **부분: packet/status/contract/decision과 probe receipt bytes는 authority-bearing; worker report/log는 claim** | 아니오 | **아니오: rejected/in-flight patch, decision, probe receipt와 retained attempt history 전체를 Git/ref에서 복구 불가** | read-only archive + `inspect`; bulk migration 없음 | I-02~I-06; E-05·E-07; 계획 §2-2·§5-1; ADR-0005·0006·0010·0012; **F-02** |
| `.waystone/exposure/*` | 12 JSON | **예: event-time profile/policy/config exposure** | 아니오 | **아니오: 당시 local inputs와 관측시점을 재관측 불가** | 신규 발행 중지, closeout manifest로 대체; legacy read-only | 계획 §5-1·§5-4; ADR-0005·0006; **F-03** |
| `.waystone/lock` | 있음 | 아니오: OS lock/diagnostic carrier | 아니오 | 예, file 재생성; lock ownership은 OS 현재 관측 | 역할 분리: DB claim, lease/fence, short OS lock, CAS | 계획 §3-5·§5-1; ADR-0002·0003 |
| `.waystone/consents.jsonl` | 4 events | **예: user consent audit** | 아니오 | **아니오: 과거 user choice/context를 재관측 불가** | idempotent DB import, 원본 read-only 보존 | I-07·I-08; 계획 §2-2·§5-1·§5-2; **F-04** |
| `.waystone/overlay/review-ingests.jsonl` | 8 events | **예: observed review-feedback event/provenance** | 아니오 | **아니오: ingest event/time과 일부 local provenance를 Git feedback에서 복구 불가** | features/policy로 역할 분리, 0.12 형식·내용 동결 | I-08; 계획 §5-1; ADR-0005; **F-05** |
| `.waystone/overlay/{deltas,warnings,...}` | 없음 | 해당 없음 | 아니오 | 해당 없음 | features/policy로 역할 분리, 0.12 형식·내용 동결 | 계획 §5-1; 실물 부재 명시 |
| `.waystone/codex-runner-verified` | 있음 | 예: reusable probe proof | 아니오 | 예, exact axes로 probe 재실행 | probe table로 DB 이관, E-03 보존 | E-03·E-09; 계획 §2-2·§5-1 |
| `.waystone/codex-runner-verification.lock` | 있음(§5-1 누락) | 아니오: probe serialization | 아니오 | 예, file 재생성; lock state는 OS 관측 | 역할 분리: short OS lock + probe DB/CAS | 계획 §3-5; ADR-0002·0003 |
| `.waystone/review-requests/*` | 2 narratives(§5-1 누락) | 아니오: published packet request의 local staging/source copy; 권위는 tracked request/binding | 아니오 | authority 기준 해당 없음; published rendered request는 Git에 존재 | local 신규 발행 중지, Git-tracked canonical review layout으로 역할 이전 | 계획 §2-1; E-01·E-02; ADR-0005·0009 |
| `~/.waystone/projects.json` | 1 registration(§5-1 누락) | **예: canonical project mapping의 machine registry** | 아니오 | **아니오: 등록 intent와 장래 opaque `project_id`를 checkout path에서 추론할 수 없음** | machine registry로 역할 분리·유지; recoverability 처분은 미확정 | I-09·I-11; ADR-0011; **F-06** |
| `~/.waystone/registry.lock` | 있음(§5-1 누락) | 아니오: registry serialization | 아니오 | 예, file 재생성; lock state는 OS 관측 | 역할 분리: short OS lock | 계획 §3-5; ADR-0002·0011 |
| `~/.waystone/cache/worktrees/*` | 8 worktrees(§5-1 누락) | 아니오: execution workspace/cache | 아니오 | 조건부: terminal result는 base+patch로 normalize 가능; in-flight bytes는 불가하지만 아직 authority가 아님 | cache/workspace 유지; running/unknown effect에서 destructive cleanup 금지 | E-08; ADR-0002·0003·0005 |
| `.waystone/state.db` + `artifacts/` | **없음** | 현행 해당 없음 | 아니오 | 해당 없음 | 0.12 local transactional authority 신설 | 계획 §3-1·§5-5; ADR-0005~0007·0011 |

## M0 findings

### F-01 — major — project profile의 유일한 권위가 untracked local file이다

`.waystone/profile.yml`의 binding은 현재 실행 route/backend를 결정하고 binding 부재 시 fail-loud한다.
그러나 동일 내용을 가진 Git source나 결정적 projection은 없다. 파일 유실은 잘못된 default로
degrade하지는 않지만 human-authored routing intent를 복구 불가능하게 만든다. §5-1의 `로컬 유지`
처분은 compatibility를 설명할 뿐 이 recoverability gap을 닫지 않는다.

### F-02 — major — delegation archive에 재파생 불가능한 harness fact와 decision이 있다

101개 record는 worker claim만 모은 cache가 아니다. harness-computed base/patch/changed-file fact,
immutable packet, current status, probe receipt, main decision과 record 간 retained attempt history 중
일부의 유일한 bytes가
여기에 있다. 70개 snapshot ref와 현재 Git history로는 discarded, failed, in-flight record와
artifact body를 모두 복원할 수 없다. `read-only archive`는 추가 변형을 막지만 backup 또는 다른
authority를 제공하지 않는다. ADR-0006이 local artifact 삭제 시 deep audit 불가를 명시한 한계와
같은 방향이다.

### F-03 — major — round exposure의 event-time authority가 local-only이다

12개 exposure는 round close 시점에 실제로 유효했던 profile, overlay, config fingerprint를
보존한다. 현재 파일이나 Git HEAD를 다시 읽는 것으로 과거 조합과 관측시점을 복원할 수 없다.
신규 발행 중지와 closeout manifest 대체는 미래 writer를 닫지만 기존 historical authority의
recoverability는 해결하지 않는다.

### F-04 — major — consent audit가 local JSONL 한 벌뿐이다

4개 consent event의 선택, 시각, 대상 hash/context는 과거 user action의 권위 기록이다. 현재 managed
file이나 policy 상태는 과거 consent의 충분한 재파생 원천이 아니다. DB importer는 0.12 이후의
authority 전환 수단이지만 import 전 원본 유실을 복구하지 못하므로 M0 finding이다.

### F-05 — minor — review-ingest event provenance가 local-only이다

`review-ingests.jsonl` 8행은 tracked feedback body와 별도로 “언제 어떤 source/event id로 ingest가
관측되었는가”를 기록하고 adaptive evidence가 소비한다. feedback body에서 event time과 local
ingest provenance를 재파생할 수 없다. completion/merge authority는 Git review evidence에 남아
있으므로 F-01~F-04보다 영향이 좁아 minor로 분류했다.

### F-06 — major — machine project registry가 canonical mapping의 단일 권위다

`~/.waystone/projects.json` 한 파일이 linked checkout을 canonical project로 정규화하는 등록
정보를 제공한다. ADR-0011은 path나 `git_common_dir`에서 durable `project_id`를 추론하지 말라고
명시하므로 repository scan은 재파생이 아니다. registry 유실 시 재등록은 새로운 owner action이지
기존 등록 intent의 복구가 아니다. §5-1에는 이 machine-tier authority의 처분도 없다.

## 비-finding 경계와 known issues

- `start-here.md`와 `resume.md`는 스스로도 pointer/projection이라고 밝히며 authoritative state를
  `tasks.yaml`, `PROGRESS.md`, `ROADMAP.md`로 돌린다. narrative bytes가 exact-reproducible하지 않아도
  authority violation은 아니다.
- lock 파일의 bytes는 ownership/liveness authority가 아니다. ADR-0002·0003에 따라 lock/heartbeat
  부재를 effect 부재나 종료로 해석하지 않고 OS/supervisor의 현재 관측과 DB CAS로 역할을 나눈다.
- `codex-runner-verified`는 local proof이지만 exact axes를 다시 probe해 재생성할 수 있으므로
  §5-1 원칙을 만족한다. marker에 기록된 incidental stat은 E-09에 따라 진단용일 뿐 reuse authority
  축으로 승격하면 안 된다.
- `docs/known-issues.md`의 JW-GPT-014·015는 PR-mode legacy review projection 결함이다. 이
  repository는 packet mode이고 freeze/demotion sidecar 실물이 없었다. 두 기존 issue는 위 6개
  storage finding과 별개이며, 이 감사에서 새 task를 등록하지 않았다.
- §2-5의 porting ledger/traceability matrix는 이 처분표와 별개 산출물이다. 다만 M1에서
  delegation patch/review request 같은 canonical artifact writer를 옮길 때 byte-identical,
  machine JSON은 schema/value 동일, handoff는 의미 호환이라는 §6 M1-A 등급을 적용해야 한다.


---

## Finding 처분 (main 판정 2026-07-20)

감사가 낸 6건을 성격별로 3건으로 통합해 처분했다.

| Finding | 처분 | 근거 |
|---|---|---|
| **F-01** profile.yml의 권위가 untracked local | `fix/profile-intent-vs-capability-split` (major) | 실물 확인 결과 profile.yml은 **프로젝트 정책**이다 — "어려운 설계는 fable, 구현은 codex xhigh"는 이 프로젝트를 어떻게 일할지의 결정이며 다른 머신에서도 동일해야 한다. project 라우팅 의도(git-tracked)와 machine 능력·해석(local)을 분리한다. |
| **F-06** machine-tier state의 처분 부재 | `docs/machine-tier-state-disposition` (major) | 계획서 §5-1은 project-local `.waystone/`만 다루는데, ADR-0011이 2026-07-19에 `~/.waystone/projects.json`을 canonical mapping 권위로 신설했다. 계획이 자기가 만든 권위를 아직 분류하지 않은 상태다. |
| **F-02~F-05** 역사적 local-only 감사 기록 | **수용된 잔여** — `decision/local-audit-recoverability` (ruled) | 아래 참조. |

### 수용된 잔여: 역사적 local-only 감사 기록

**대상**: delegation record 101건 · round exposure 12건 · consent event 4건 · review-ingest provenance.

**계약**: 이 기록들은 machine-local이며 **git에서 재파생 불가하고 유실 시 복구되지 않는다.** 이를
결함이 아니라 **명시적으로 수용된 한계**로 확정한다(사용자 결정 2026-07-20).

**근거**: ⑴ 백업·요약 기계는 그 자체가 두 번째 권위 표면이 되어 "원본과 요약 중 무엇이 맞나"를
낳는다 — ADR-0006이 closeout manifest에 verifier verdict를 이식하지 않기로 한 것과 같은 논리다.
⑵ 67MB 대부분이 러너 로그이며, git 편입 시 저장소가 매 라운드 부풀고 프롬프트 원문·모델 출력이
공개 히스토리에 박힌다. ⑶ **정확성이 걸린 판정 — 무엇이 인수됐고 왜 — 은 이미 전부 git에 있다**
(`tasks.yaml` result·`PROGRESS.md`·`docs/reviews/`·커밋 메시지). 사라지는 것은 과정의 세부다.

**동반 규율 (수용의 조건)**:

1. 완료 판정·인수 근거를 이 기록에만 의존해 기술하지 않는다 — 결론과 그 근거는 PROGRESS와
   task result에 남기는 것이 규율이다.
2. 머신 교체·초기화 시 이 기록이 함께 사라진다는 것을 전제로 운영한다.
3. 다른 머신에서 수행한 위임의 기록은 그 머신에만 남는다 — 이는 정상 동작이며
   cross-machine handoff는 git 경유(task·리뷰·PROGRESS)로 이루어진다(§5-3 ruling #6과 정합).
