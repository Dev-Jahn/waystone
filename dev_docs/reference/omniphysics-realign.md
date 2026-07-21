> 👤 **USER TURN**

```
분석을 마쳤습니다. 커밋 이력, 태스크 레지스트리, 설계 문서(SSOT/DESIGN 단계 계획), 코드/문서 볼륨, 최근 작업 초점까지 교차 확인한 객관적 판정입니다.

---
한 줄 결론

기반(foundation)은 매우 탄탄하지만, 지난 ~2주간 프로젝트는 "구현"이 아니라 "방법론·리뷰·거버넌스 루프"에 들어가 있고, 정작 최종 산출물인 "데이터 공장(P5)"은 착수조차 안 됐습니다. "실행 가능한 프로토타입"이 무엇을 뜻하느냐에 따라 예상이 극단적으로 갈립니다 — 최소형은 수 주, 비전 충실형은 불확정(성공 여부 자체가 열린 연구 문제).

---
1. 사실관계 (타임라인 · 개발량)

┌────────────────────┬───────────────────────────────────────────────────┐
│        항목        │                        값                         │
├────────────────────┼───────────────────────────────────────────────────┤
│ 기간               │ 2026-06-09 → 07-20 (41일, 실제 커밋일 ~30일)      │
├────────────────────┼───────────────────────────────────────────────────┤
│ 총 커밋            │ 545                                               │
├────────────────────┼───────────────────────────────────────────────────┤
│ omniphysics 소스   │ 253 파일 / 91.6k 줄 (+ 테스트 79 파일 / 23.5k 줄) │
├────────────────────┼───────────────────────────────────────────────────┤
│ 설계·거버넌스 문서 │ markdown 238개                                    │
├────────────────────┼───────────────────────────────────────────────────┤
│ 태스크             │ 67개 (done 51 · pending 13 · blocked/parked 4)    │
└────────────────────┴───────────────────────────────────────────────────┘

커밋 성격 분포가 이 프로젝트의 정체를 드러냅니다:

docs 238 · feat 69 · fix 61 · evidence 46 · chore 45 · test 18 · gate 12
→ docs+evidence+chore+gate = 약 62%가 문서·증거·거버넌스, 순수 구현(feat)은 13%. 태스크 타입도 fix 33 · decision 10 · spike 8 대비 feat은 7개뿐입니다. 이건 feature 배달 프로젝트가 아니라 research/verification 프로젝트의 프로파일입니다.

---
2. 계획 대비 진행률 (마일스톤 위치)

전체 계획: P-1 → P0(a–d) → P0.5 → P1 → P1.5 → P2 → P3 → P4 → P5(데이터 공장)

┌──────┬────────────────────────────────────┬───────────────────────────────────────────────────────────────────┐
│ 단계 │                내용                │                               상태                                │
├──────┼────────────────────────────────────┼───────────────────────────────────────────────────────────────────┤
│ P-1  │ executable abstraction probes      │ ✅ (cp1a/cp2a/cp2b/cp3a 존재)                                     │
├──────┼────────────────────────────────────┼───────────────────────────────────────────────────────────────────┤
│ P0   │ 계약 계층 + 런타임 substrate       │ ✅ (M2 substrate, contracts, grader scaffold)                     │
├──────┼────────────────────────────────────┼───────────────────────────────────────────────────────────────────┤
│ P1   │ single-modality baselines          │ 🟡 부분 — 액체 bulk(WCSPH/Tait) V1+V2만 완료                      │
├──────┼────────────────────────────────────┼───────────────────────────────────────────────────────────────────┤
│ —    │ contact 코어(우선순위 사다리 ①)    │ ⛔ 막힘 → "major-refactor research fork, 성공 UNCERTAIN"(ADR-038) │
├──────┼────────────────────────────────────┼───────────────────────────────────────────────────────────────────┤
│ P1.5 │ IsaacSim micro-bridge              │ 🟡 lifecycle smoke·bridge probe만 (실제 custom-field 경로 아님)   │
├──────┼────────────────────────────────────┼───────────────────────────────────────────────────────────────────┤
│ P2   │ pairwise coupling                  │ ❌ 미착수                                                         │
├──────┼────────────────────────────────────┼───────────────────────────────────────────────────────────────────┤
│ P3   │ gas/vapor + phase-change           │ ❌ 미착수                                                         │
├──────┼────────────────────────────────────┼───────────────────────────────────────────────────────────────────┤
│ P4   │ IsaacSim 통합 확장                 │ ❌ 미착수                                                         │
├──────┼────────────────────────────────────┼───────────────────────────────────────────────────────────────────┤
│ P5   │ dataset production (= 데이터 공장) │ ❌ 미착수 — dataset/writers·readers는 빈 __init__.py 스텁         │
└──────┴────────────────────────────────────┴───────────────────────────────────────────────────────────────────┘

로드맵(ROADMAP.md)에 active 마일스톤이 M2 하나뿐이라는 점이 핵심입니다. P2–P5는 아직 로드맵에 편입조차 안 됐습니다.

비전 대비 체감 진행률: 첫 end-to-end 프로토타입 기준 ~25–35%. 그런데 남은 65–75%에 가장 어렵고 이미 여러 번 실패한 부분들이 몰려 있습니다.

---
3. 속도 분석 — 그리고 "정체 신호"

주간 omniphysics 코드 증가량(churn):
06-09: +142k(스캐폴드)  06-16: +10.5k  06-23: +8.9k
06-30: +38.9k(probe/gate)  07-07: +14.4k  07-14: +6.6k ↓

결정적 지표 — 최근 14일(07-07 이후):
- 총 커밋 138개 중 docs/reviews 90개, solver/numerics/dataset 소스를 건드린 커밋은 단 6개.
- 이 기간 작업 로그(PROGRESS)는 전부 review4-fixes ~ review8-fixes, law freeze REV 1→2→3(전부 기각·docs-only), gate PARK 처분입니다.

즉 명목 속도(51 done / 41일 ≈ 1.2 태스크/일)는 착시입니다. 최근 소진된 태스크는 대부분 리뷰 사이클에서 나온 fix(문서 교정)이지, 마일스톤을 전진시키는 feat이 아닙니다. 새 물리/생산 코드의 속도는 사실상 0에 수렴했고, 에너지가 인증 방법론 다듬기로 흘러 들어갔습니다.

---
4. 남은 작업의 난이도 — "엔지니어링"이 아니라 "연구"

이게 예상의 핵심입니다. 남은 pending/blocked 태스크는 추정 가능한 구현이 아니라 이미 여러 차례 벽을 친 연구 문제입니다:

- contact 두-계수 마찰 법칙 (SSOT §6의 최우선 ①): freeze REV 1·2·3 전부 리뷰 기각 → 지금 spike/vbd-kamino-residual-eta-certification-arch + ...-fractional-transaction-arch라는 아키텍처 리팩터 연구 포크로 넘어감. ADR-038이 명시적으로 **"성공 UNCERTAIN"**이라 표기.
- 액체/계면 tension 7건 PARKED (H1 clumping이 instrument-invalid로 판명).
- cavitation interior-void gate PARKED ("open research problem — 3 design families refuted").
- g-jump static balance blocked.

CLAUDE.md의 연구 스탠스 자체가 "게이트를 통과시키려 상수를 맞추는 것 금지(anti-Goodhart)"를 규율로 못박고 있어서, 이 정체는 버그가 아니라 의도된 정직성의 비용입니다. 좋은 규율이지만, 그 대가로 "언제 끝날지 추정 불가능한 연구"에 락인된 상태입니다.

---
5. 프로토타입까지 예상 (두 시나리오로 분리)

"프로토타입"의 정의를 나눠야 정직한 답이 됩니다.

시나리오 A — 최소 실행 프로토타입 (공장의 thin vertical slice)

액체 bulk 한 modality를 Isaac Sim 통해 굴려 provenance 스탬프가 붙은 소규모 합성 데이터셋을 실제로 뽑아내는 것.
- 이미 있는 부품: SPH solver(검증됨), Isaac 하네스(렌더/캡처), 계약 계층, grader, episode_manifest/production_envelope scaffold.
- 빠진 것(순수 엔지니어링, 추정 가능): ① dataset/writers·readers 실구현, ② P1.5 실제 custom-field bridge, ③ "sim 실행 → 라벨 데이터 emit → 특성화 이력 부착" 배선, ④ 최소 replay/regression.
- 추정: 집중 투입 시 약 3–6주. 단, 지금의 궤도는 여기를 향하고 있지 않습니다 — contact 인증 연구에 붙잡혀 있음. 사용자가 "①을 잠시 park하고 공장 슬라이스부터 세운다"는 결정을 내려야 진입 가능.

시나리오 B — 비전 충실 프로토타입

우선순위 ① contact 코어가 PhysX/기본 Newton 대비 가시적 우위 입증 + 인증 두 기둥 통과 + 데이터 공장.
- contact 인증이 지금 열린 연구 포크(성공 불확실). residual→η certificate와 atomic fractional transaction 둘 다 "INFEASIBLE-MAJOR"로 판정됐고, 새 아키텍처로 넘으려는 투자 단계.
- 추정 불가. 낙관적으로 수 개월, 비관적으로는 "이 계열이 현 substrate에서 인증 가능한가" 자체가 미해결(PARK≠impossibility지만 보장도 없음).

---
6. 종합 판단

- 강점: 기반·계약·검증 규율·문서화는 상용 R&D 수준으로 견고. 방법론(anti-Goodhart)이 자기기만을 실제로 잡아내고 있음(리뷰 사이클에서 반복 적발).
- 리스크: 프로젝트가 **"제품 로드맵의 옷을 입은 연구 프로젝트"**로 운영되고 있음. 최우선 물리(contact)가 가장 깊은 구멍이고, 정작 north-star 산출물(데이터 공장 P5)은 스텁 상태. 속도 지표상 최근 2주는 전진이 아니라 제자리 다지기.
- 가장 중요한 조언: "실행 가능한 프로토타입"을 실제로 손에 쥐려면 — 현재처럼 contact 인증이 끝나길 기다리는 순차 경로가 아니라 — 액체 modality로 공장 슬라이스(시나리오 A)를 병렬로 먼저 세우는 전략 결정이 필요합니다. 그러면 "돌아가는 데모"는 ①의 연구 성패와 **분리(decouple)**되어 몇 주 안에 나올 수 있습니다. 현 궤도를 유지하면, 실행 가능한 프로토타입 시점은 contact 연구 포크의 성공 여부에 종속되어 사실상 미정입니다.
```
이건 현재 프로젝트의 진행상황에 대한 분석인데, 좀 뼈아픈 내용이야. 원래 내가 원했던게 다소 막연하게, '뭐가 됐든 지금의 clunky한 physx 기반 물리엔진(마찰같은 건 제대로 반영도 안되고, 물체 표면끼리 서로 관통하고, 천 재질 물체는 메시를 관통해 낑기고...온갖 문제가 많음) 보다 좀 더 나은, 제대로 된 물리엔진을 isaacsim에 얹어서 VLA 모델 학습이나 test를 위한 좀 더 reality가 높은 환경을 구축해보자' 정도의 목표였는데, 이걸 처음부터 너무 거창하고 정확하게 구현하려다 보니 프로젝트가 산으로 간 것 같음. 가장 큰 문제는 이 프로젝트에서 사용하는 harness의 축이 되는 SSOT.md와 CLAUDE.md가 잘못 설정되어있는 것 같아.
프로젝트를 skimming해서 분석해보고, 나한테 소크라테스식 산파술 처럼, 내가 정확히 뭘 원하는지, 어떤 접근을 취해야 프로젝트가 제대로 목표를 향해 갈 수 있을지를 도출하기 위한 질문을 하나씩 던져줘. 여러 turn 을 반복하며 내 생각을 구체화 해 나가야 하니까 지금 너무 오래 생각할 필요는 없어. 반복해서 상황에 맞게 나한테 질문을 던지면서 추상적이었던 목표를 구체화 시키다가, '이제 확실하게 선명해졌다' 라고 할 만한 수준이 되면 개정할 CLAUDE.md와 SSOT.md 를 작성하면 됨.

---

> 🤖 **GPT TURN**

현재 `main`의 최신 HEAD `27f46b27dc2333c7703446664cf77b2f41de34e8`을 기준으로 `SSOT.md`, `CLAUDE.md`, `DESIGN.md`, 최근 커밋·진행 로그, dataset/Isaac Sim bridge 구현을 훑었습니다. 로컬 클론은 실행 환경의 네트워크 차단으로 불가능했지만, GitHub Connector에서 해당 SHA의 파일을 직접 읽었습니다. fileciteturn4file0L1-L12

## 초기 판정

당신의 문제의식은 대체로 맞습니다. 다만 정확히 말하면 **`SSOT.md`의 비전 전체가 잘못됐다기보다, 비전보다 먼저 실행 순서와 검증 체계를 고정한 것이 문제**입니다.

### 1. `SSOT.md`의 상위 비전은 오히려 원래 의도와 가깝습니다

현재 SSOT는 프로젝트를 “인증된 공학 해석 도구”가 아니라, **VLA용 데이터를 만들 만큼 정직하게 특성화된 물리 환경**으로 정의합니다. 또한 검증은 유한해야 하고, research preview를 일찍 내야 한다고 명시합니다. 이 부분은 지금 말씀하신 현실적인 방향과 크게 충돌하지 않습니다. fileciteturn7file0L11-L21 fileciteturn7file0L23-L31

### 2. 실제로 프로젝트를 잘못 끌고 간 것은 `contact-first` 순서 고정입니다

SSOT는 자원 배분 순서를 다음처럼 선고정합니다.

> contact 코어 정밀화 → 액체·젖음·연성체·입자 → omni 지평

그런데 바로 아래의 열린 질문에는 아직도 다음 세 가지가 미정으로 남아 있습니다.

- 첫 contact 타깃이 무엇인지
- 데이터 제품이 dataset인지 env+generator인지
- 최소 research preview가 무엇인지

즉, **제품과 성공 장면을 정하기 전에 가장 어려운 연구 분야를 임계경로로 지정한 역전**이 일어났습니다. fileciteturn7file0L42-L49 fileciteturn7file0L51-L59

### 3. `CLAUDE.md`는 좋은 연구 규율을 프로젝트 운영체제로 과잉 승격했습니다

anti-Goodhart, derive-then-freeze, 다중 probe, 과대 인증 금지는 모두 유효한 연구 원칙입니다. 문제는 이 원칙이 **물리적 주장을 검증할 때 쓰는 제한적 안전장치**가 아니라, 거의 모든 개발 판단에 적용되는 전역 절차가 되었다는 점입니다. fileciteturn6file0L10-L24

거기에 모든 작업의 사전 등록, SSOT와의 차이마다 decision task와 ADR, 매 라운드의 종료·리뷰 패킷이 결합되어 있습니다. 그 결과, 작은 프로토타입 가설조차 연구 논문의 사전등록과 비슷한 비용을 치르게 됩니다. fileciteturn6file0L58-L66

### 4. 더 근본적인 범위 폭발은 demoted `DESIGN.md`에도 남아 있습니다

구체 설계는 장기적으로 PhysX backend의 대안이 되는 것을 목표로 하며, 첫 wave에 이미 다음을 함께 넣었습니다.

`rigid contact + liquid + heat + phase change + gas + coupling + wetting + dataset + Isaac Sim`

이것은 “한 가지 VLA 장면을 PhysX보다 낫게 만든다”가 아니라 사실상 **새로운 멀티피직스 플랫폼을 처음부터 설계한다**는 계획입니다. 문서상 demoted 되었어도 에이전트가 계속 이를 실행 설계로 참조하므로 실질적인 범위 압력은 남아 있습니다. fileciteturn12file0L5-L23 fileciteturn12file0L25-L46

### 5. 한 가지 사실관계는 수정할 필요가 있습니다

프로젝트가 아직 “데이터 공장”을 실제로 돌리지 못했다는 평가는 맞지만, dataset 계층이 빈 스텁뿐인 것은 아닙니다. 별도 observation/state/event/ledger writer, manifest, replay, camera–GT pairing이 구현되어 있고, Isaac Sim에는 host/GPU 경로를 가진 particle bridge도 존재합니다. fileciteturn14file0L3-L22 fileciteturn17file0L96-L146 fileciteturn18file0L3-L27

따라서 정확한 병목은 다음입니다.

> **부품이 없는 것이 아니라, 어떤 사용자 문제를 해결하는 제품인지 결정되지 않아 부품들이 하나의 설득력 있는 end-to-end 장면으로 수렴하지 못했다.**

---

## 첫 번째 질문

다음의 제약을 받아들였다고 가정하겠습니다.

> 당분간 “범용 물리엔진”이나 “PhysX의 장기 대체재”라는 목표는 내려놓고, 앞으로 6–8주 동안 **단 하나의 VLA 조작 장면에서 PhysX의 단 하나의 실패를 재현 가능하게 줄이는 Isaac Sim 환경**만 만든다.

이 결과만으로도 프로젝트의 첫 성공으로 인정할 수 있습니까?

- **A. 그렇다.** 좁고 task-specific해도 실제 VLA 환경에서 PhysX보다 나으면 된다.
- **B. 아니다.** 첫 결과부터 여러 장면·물리 현상에 재사용 가능한 새로운 solver core여야 한다.
- **C. 중간이다.** 장면은 하나여도 되지만, 반드시 재사용 가능해야 하는 특정 코어가 있다.

A/B/C 중 가장 가까운 것을 고르고, **포기할 수 없는 이유 하나**만 설명해 주세요.

---

> 👤 **USER TURN**

B. 목표를 너무 협소하게 잡는 경우, 이걸 확장/일반화시키는 게 사실상 그냥 새로 만드는 것 수준으로 어려운 경우가 될 가능성이 높음.

---

> 🤖 **GPT TURN**

그 우려는 타당합니다. **데모 전용 우회로가 핵심 아키텍처와 분리되면, 성공한 프로토타입이 기술 부채만 남길 수 있습니다.**

다만 여기서 `재사용 가능한 core`의 범위를 정하지 않으면 다시 “모든 물리를 포괄하는 공통 기반”으로 팽창합니다. 현재 `DESIGN.md`는 contact, liquid, heat, phase change, gas, wetting을 한 초기 구조에 함께 넣어 사실상 가장 넓은 의미의 일반화를 선택했습니다. fileciteturn12file0L25-L46

## 두 번째 질문

당신이 말하는 **“확장 가능한 코어”**는 아래 중 어디까지여야 합니까?

**A. 동일 현상 안의 일반화**
예: contact solver 하나가 다양한 형상, 질량비, 재질, grasp, insertion, stacking에 재사용된다. 액체나 천은 별도 solver여도 된다.

**B. 서로 다른 현상을 수용하는 공통 런타임**
contact, liquid, cloth solver는 각각 달라도 되지만, state representation, stepping, coupling, Isaac Sim bridge, dataset export는 같은 substrate를 사용한다.

**C. 서로 다른 현상까지 통일된 수치 formulation**
rigid contact, deformable, cloth, liquid 등을 가능한 한 하나의 입자·constraint·variational framework에서 푼다.

첫 프로토타입이 장기적으로 버려지지 않기 위해 필요한 **최소 수준**은 A, B, C 중 무엇입니까?

---

> 👤 **USER TURN**

솔직히 말해서 전공이 물리 쪽이 아니라서 잘 모름. A로는 내가 원하는 수준이 안될 것 같고, 선택지만 봤을 때는 C가 좋아보이긴 하는데 딱봐도 B보다 C가 order가 다른 수준으로 난이도가 높을 것 같은데. C가 구현이 가능한 수준이면 C로 가되, 너무 어려우면 B로도 괜찮을 듯. 해결되었으면 하는 간단한 예시 하나를 들어보자면, 로봇 arm의 gripper가 표면이 마른 유리컵을 집어들때와 표면이 물에 젖어있는 유리컵을 집어들 때 마찰력이 다른 것을 정확히 시뮬레이션하고, rigid body 표면에 액체가 묻은 경우에 여기 가해지는 gripper의 힘이 얼마나 강하냐에 따라 미끄러지는 정도가 다르거나, gripper 접촉면의 고무 재질의 정지, 운동 마찰계수가 얼마이고, 탄성계수가 얼마이고 힘을 얼마나 주는지에 따라 그 고무 표면이 눌리는 정도가 달라져서 그립력이 달라지는 등을 정확히 시뮬레이션 하는 것.
'정확히'의 기준이 다소 추상적이긴 하지만, 어느정도 오차가 있다 하더라도 엔진의 설계 자체가 최소 위에 언급한 정도의 물리는 나름의 기준 아래에서 중요한 물리적 상호작용을 빠뜨리지 않고 처리할 수 있어야 내가 원하는 엔진의 프로토타입이라고 할 수 있음.

---

> 🤖 **GPT TURN**

좋습니다. 이 예시는 중요한 경계를 드러냅니다.

당신이 원하는 것은 **모든 현상을 하나의 수치 formulation으로 푸는 C 자체**라기보다, 다음 인과사슬을 끊지 않고 계산하는 엔진입니다.

\[
\text{gripper force}
\rightarrow
\text{고무 변형·접촉면적·압력분포}
\rightarrow
\text{건식/습식 계면 상태}
\rightarrow
\text{정지마찰 한계·미끄럼 전이}
\rightarrow
\text{컵의 slip / lift 성공 여부}
\]

여기에 젖은 표면에서는 액막 두께, 배수, 점성 전단, 경우에 따라 모세관·접착 효과가 들어갑니다. 즉 핵심은 **통일된 solver 하나**보다 **rigid body + deformable contact + friction law + thin-fluid/interface 모델을 일관되게 결합하는 능력**입니다.

따라서 현재로서는 다음 방향이 더 맞아 보입니다.

> **B를 기반으로 하되, 결합 계면은 C에 가까울 정도로 물리적으로 엄격하게 설계한다.**

각 현상에 적합한 solver는 분리하되, 접촉면의 상태와 힘·변형·액체 정보를 서로 주고받는 공통 인터페이스를 핵심 제품으로 삼는 것입니다. 이 방식이라면 액체와 고무를 억지로 하나의 formulation에 넣지 않고도, 말씀하신 장면을 장면 전용 해킹 없이 일반화할 여지가 있습니다.

## 세 번째 질문

이제 가장 중요한 **“정확히”의 의미**를 정해야 합니다. 젖은 유리컵 grasp 실험을 실제 장비에서도 반복할 수 있다고 할 때, 프로토타입이 만족해야 할 최소 기준은 어느 쪽에 가깝습니까?

**A. 정성적 인과관계**
젖으면 더 잘 미끄러지고, grip force를 높이면 덜 미끄러우며, 고무가 부드러우면 접촉 양상이 달라지는 정도를 올바른 방향으로 재현한다.

**B. 장면별 보정 후 정량 예측**
해당 컵·고무·액체 조합의 실험값으로 파라미터를 보정하면, 미끄럼 시작 grip force나 slip distance를 예컨대 10–20% 오차로 맞춘다.

**C. 재료별 1회 보정 후 미관측 조건으로 전이**
고무, 유리, 물의 재료 파라미터를 각각 한 번 측정하면, 새로운 컵 형상·grip force·젖음량·접촉 위치에서도 별도의 장면별 튜닝 없이 결과를 예측한다.

**D. 실험 보정에 거의 의존하지 않는 절대 예측**
문헌·재료 물성만으로 실제 grasp 결과를 정량적으로 맞춘다.

당신이 **“이 정도가 안 되면 PhysX보다 나은 엔진을 만들었다고 부를 수 없다”**고 보는 최소선은 어디입니까?

---

> 👤 **USER TURN**

C

---

> 🤖 **GPT TURN**

그렇다면 목표는 상당히 선명해졌습니다.

당신이 원하는 프로토타입의 핵심은 단순히 “PhysX보다 그럴듯하다”가 아니라 다음입니다.

> **한 번 특성화한 재료·계면 모델이, 학습에 사용하지 않은 형상·하중·젖음 조건에서도 물리적 결과를 예측해야 한다.**

따라서 장면별 파라미터 조정은 실패입니다. 새 컵을 넣을 때마다 마찰계수나 damping을 다시 맞춰야 한다면 기존 엔진의 경험적 튜닝과 본질적으로 다르지 않습니다.

이 기준은 아키텍처 선택에도 영향을 줍니다. 하나의 만능 formulation인 C가 반드시 필요한 것은 아니지만, 적어도 다음 상태들은 solver 사이에서 명시적으로 보존되어야 합니다.

\[
\text{surface state}
+
\text{contact pressure}
+
\text{deformation}
+
\text{fluid film state}
+
\text{stick/slip history}
\]

즉 단순한 `rigid body solver ↔ liquid solver` 데이터 교환이 아니라, **계면 자체가 독립적인 물리 모델이자 상태를 가진 구성요소**여야 합니다.

## 네 번째 질문

여기서 현실적으로 매우 중요한 경계를 정해야 합니다.

마찰·젖음·접착은 보통 “고무의 물성”, “유리의 물성”, “물의 물성”만으로 완전히 분리되지 않고, **고무–유리 표면 쌍과 그 표면 상태**에 의존합니다. 같은 고무와 유리라도 표면 거칠기, 코팅, 오염, 세척 상태에 따라 결과가 크게 달라질 수 있습니다.

따라서 다음 중 어느 수준의 사전 특성화를 허용하겠습니까?

**A. 계면 조합별 1회 특성화 허용**
예: `고무 A × 유리 B × 물` 조합을 한 번 실험하여 계면 파라미터를 얻고, 이후 컵 형상·접촉 위치·grip force·물의 양·동작 속도가 달라져도 재튜닝하지 않는다.

**B. 개별 재료 특성화만 허용**
고무, 유리, 물을 각각 독립적으로 한 번 측정하면, 처음 만나는 모든 조합의 상호작용을 예측해야 한다.

**C. 혼합형**
탄성·점성·밀도 등 bulk 물성은 재료별로 측정하고, 마찰·젖음·접착처럼 본질적으로 계면에 속하는 값만 재료 쌍별로 한 번 측정한다.

프로토타입의 최소 기준으로 어느 것이 맞습니까?

---

> 👤 **USER TURN**

현실적으로 C가 맞는 것 같긴 한데, 단순히 재료/재료쌍의 조합을 측정한다고 해서 정확한 예측이 될 것 같지는 않음.
말한대로 '같은 고무와 유리라도 표면 거칠기, 코팅, 오염, 세척 상태'에 따라 결과가 달라질 수 있으니, 기본적으로는 재료와 재료쌍을 한번씩 측정해서 기준점으로 삼되 이후에 구체적으로 해당 재료로 구성된 어떤 instance냐에 따라서 optional하게 보정을 할 수는 있어야 함. 이 보정은 해당 object에만 적용되고 같은 재료인 다른 object(다르게 생긴)에는 적용되지 않는 일종의 overlay 식으로. 물론 단순히 재료만 가지고 판단하는게 아니라, 접촉면의 형상이 모델에 표현된 경우 이것도 반영해서 가능한 이런 보정이 없어도 그럭저럭 동작하는 수준은 나오도록 gap을 미리 좁혀두면 좋겠음. 예를들어 같은 고무더라도 매끈한 고무보다, 뾰족뾰족하게 요철 처리를 해놓은 고무 표면이 접촉면적은 적음에도 불구하고 접지력이 더 좋은 것처럼.

---

> 🤖 **GPT TURN**

지금 설명으로 보아, 필요한 모델은 **단일 재료 상수표**가 아니라 다음과 같은 계층 구조입니다.

\[
\text{bulk material}
\;\rightarrow\;
\text{material-pair interface}
\;\rightarrow\;
\text{resolved surface geometry/state}
\;\rightarrow\;
\text{object-instance overlay}
\]

구체적으로는:

- **Bulk material**: 탄성계수, Poisson 비, 점탄성, 밀도, 점도처럼 재료 자체에 귀속되는 물성
- **Material-pair interface**: 고무–유리의 건식 마찰, 접착, 젖음 상태에서의 계면 전이처럼 재료 쌍에 귀속되는 물성
- **Resolved geometry/state**: 실제 메시나 별도 surface representation에 표현된 요철, 접촉 곡률, 액막 두께, 국소 압력, 오염 상태
- **Instance overlay**: 특정 컵이나 특정 gripper pad만의 코팅, 미세 거칠기, 노화, 세척 상태, 제조 편차를 반영하는 국소 보정

이 구조라면 같은 재료의 다른 객체에 보정이 전파되는 문제를 피하면서도, 기본 모델은 보정 없이 어느 정도 일반화할 수 있습니다.

다만 “뾰족한 고무가 접촉면적은 작아도 접지력이 좋다”는 현상은 단순히 마찰계수가 높아진 것으로 처리하면 안 됩니다. 경우에 따라 다음 메커니즘이 각각 다르게 기여할 수 있습니다.

- 국소 압력 증가와 고무 변형
- 표면 거칠기에 대한 히스테리시스 손실
- 형상적 맞물림
- 젖은 계면에서의 배수 채널 형성
- 실제 접촉면적의 비선형 변화
- 접촉부 주변 액막 파괴

즉, 가능하면 메시 형상과 접촉 역학으로 설명하고, 해상도 아래의 미세구조만 effective interface model로 넘기는 것이 맞습니다.

## 다섯 번째 질문

**object-instance overlay가 어떤 종류의 보정을 허용해야 합니까?**

### A. 독립적으로 측정 가능한 물리량만 허용

예:

- 표면 거칠기 통계 또는 height map
- 코팅 두께
- 국소 탄성·경도
- 오염 또는 세척 상태
- 접촉각·액막 유지 특성

실제 grasp 결과를 보고 coefficient를 맞추는 것은 금지합니다.

### B. 소수의 instance 실험으로 저차원 파라미터 보정 허용

예를 들어 해당 컵에 대해 몇 번의 별도 마찰·압축 실험으로 overlay parameter를 추정한 뒤, 그 실험에 사용하지 않은 grip force, 자세, 접촉 위치, 젖음량을 예측합니다.

즉 **instance calibration은 허용하지만 scene-by-scene tuning은 금지**합니다.

### C. 결과가 현실적이면 직접적인 경험적 보정도 허용

특정 물체의 slip 결과에 맞춰 correction curve나 residual model을 학습하는 것도 허용합니다.

현재 설명으로는 **B**에 가장 가까워 보입니다. 다만 이 선택은 프로젝트의 “정확성”과 검증 규율을 크게 좌우합니다. A/B/C 중 어느 수준입니까?

---

> 👤 **USER TURN**

B가 맞는 것 같긴한데, 우리 연구실은 전문적인 역학, 기계 전공 연구실 처럼 정확히 뭘 측정하거나 수행을 할 환경이 안됨을 감안해야함. 가진 장비는 UR7e 로봇팔, vla test 용 so-101 몇개, realsense rgb-d 카메라 정도. 보정을 위해 할 수 있는 실험 case는 마찰 계수 측정은 경사면에서 미끄러지는 시간을 잰다던가 gripper의 힘을 얼마로 설정했을 때부터 미끄러지더라, 정도밖에 없을 것 같음. 따라서 보정의 편의성도 굉장히 중요한 요소 중 하나이고 경우에 따라 C를 채택해야 할 수도 있어.

---

> 🤖 **GPT TURN**

이 조건은 매우 중요합니다. 이제 목표는 **정밀 계측 장비로 물성을 완전히 식별하는 엔진**이 아니라,

> **제한된 장비와 몇 번의 간단한 상호작용만으로 보정할 수 있고, 보정에 쓰지 않은 동작 조건을 예측하는 물리 시뮬레이터**

가 되어야 합니다.

따라서 앞의 B와 C를 다음처럼 결합하는 것이 맞아 보입니다.

\[
\text{physics model}
+
\text{interpretable parameter calibration}
+
\text{bounded instance residual}
\]

1. 기본적으로는 탄성, 마찰, 점성, 젖음, 표면 형상 등 명시적인 물리 모델이 결과를 설명합니다.
2. 경사면 slip, gripper squeeze-and-lift, 일정 힘에서의 변형처럼 연구실에서 수행 가능한 실험으로 파라미터를 역추정합니다.
3. 그 파라미터만으로 설명되지 않는 instance-specific 편차는 제한된 empirical overlay로 보정할 수 있습니다.
4. 다만 overlay가 전체 결과를 대신하는 black-box가 되어서는 안 됩니다. 적용 대상, 보정 데이터 범위, 외삽 거리와 불확실성을 명시해야 합니다.
5. 보정에 사용한 바로 그 trajectory만 재현하는 것은 성공이 아닙니다. 다른 grip force, 접근 속도, 접촉 위치, 젖음량으로 hold-out 검증해야 합니다.

이것은 앞서 말한 C의 무제한 경험적 튜닝이 아니라, **물리 모델을 우선하고 부족한 부분만 instance-local residual로 메우는 제한적 C**입니다. 이 경우 “보정 편의성”은 부가 기능이 아니라 solver와 동등한 1급 설계 요구사항이 됩니다.

## 여섯 번째 질문

새로운 컵이나 gripper pad 하나를 등록할 때, 현실적으로 허용할 수 있는 **보정 비용의 상한**은 어느 정도입니까?

**A. 5분 이내, 3–5회 자동 실험**
대충 집고 들어 올리거나 경사면에서 미끄러뜨리는 정도만 허용합니다.

**B. 30분 이내, 10–30회 자동 실험**
여러 grip force, 속도, 자세를 로봇이 자동 sweep하고 카메라와 로봇 상태를 기록할 수 있습니다.

**C. 1–2시간, 수십 회 이상의 체계적 실험**
객체당 별도 calibration session을 운영해도 괜찮습니다.

**D. 객체 중요도에 따라 계층화**
기본 등록은 A 수준으로 하고, 정확도가 중요한 객체만 B나 C 수준으로 승격합니다.

프로토타입이 실용적이라고 느껴지는 최소 기준은 어느 것입니까?

---

> 👤 **USER TURN**

D이지만 정확도가 중요해도 C는 과함. 기본적으로 A이되 중요하면 B정도가 상한.

---

> 🤖 **GPT TURN**

좋습니다. 그러면 **보정 비용 자체가 핵심 제품 요구사항**으로 확정됩니다.

- **기본 등록:** 5분 이내, 3–5회 수준의 간단한 실험
- **정밀 등록:** 30분 이내, 10–30회 자동화된 실험
- **비허용 경로:** 전문 하중계, 촉각 어레이, 정밀 표면 계측, 수십 분 이상의 수작업 셋업을 필수로 요구하는 모델

따라서 solver가 이론적으로 정교하더라도, 필요한 파라미터를 `UR7e + gripper state + RGB-D` 정도로 식별할 수 없다면 주력 경로에는 부적합합니다. 식별되지 않는 물리량은 임의의 정확한 상수처럼 다루기보다, 제한된 effective parameter나 불확실성 범위로 흡수해야 합니다.

## 일곱 번째 질문

이 프로젝트의 **최종 성공 판정**에서 무엇이 더 상위에 있어야 합니까?

### A. 물리적 예측 정확도가 1차 목표

실제 실험의 hold-out 조건에서 다음을 정량적으로 예측하면 성공입니다.

- 미끄럼 시작 grip force
- 시간에 따른 slip distance
- lift 성공·실패
- 고무 변형량
- 젖음량 변화에 따른 결과 변화

VLA 성능 향상은 중요한 후속 검증이지만, 초기 프로토타입의 필수 조건은 아닙니다.

### B. VLA 성능 향상이 1차 목표

실제 물리량 예측에 오차가 있어도, 이 환경에서 훈련하거나 테스트한 VLA가 실제 로봇에서 더 잘 일반화하면 성공입니다.

예를 들어 젖은 컵 grasp 성공률, 실패 감지, grip-force 조절 능력이 PhysX 기반 환경보다 좋아지면 됩니다.

### C. 둘 다 필수지만 순서가 있음

먼저 제한된 장면군에서 물리적 hold-out 예측력을 입증하고, 그다음 그 차이가 실제 VLA 학습·평가 성능 개선으로 이어지는지 확인합니다. 물리 예측만 맞고 VLA에 효용이 없으면 연구 산출물은 될 수 있지만 프로젝트의 최종 성공은 아닙니다.

현재 의도에는 **C**가 가장 가까워 보이지만, A/B/C 중 어느 것이 맞습니까?

---

> 👤 **USER TURN**

A. VLA 학습까지 조건으로 걸면 너무 복잡해짐. 이를테면 정확한 물리 모델링 자체가 궁극적으로는 분명 올바른 방향인데 vla의 학습난이도를 높여서 더 큰 모델 capacity를 요구하거나 학습이 오래걸리고 단기적으로 성능이 낮아보일수 있는데, 이런걸 모두 고려해서 엔진을 개발한다는 건 말이 안됨.

---

> 🤖 **GPT TURN**

그러면 프로젝트의 1차 성공 조건은 다음으로 정리됩니다.

> **보정에 사용하지 않은 물체 형상·하중·속도·젖음 조건에서 실제 물리적 결과를 예측하는 것.**

VLA 학습 성능은 엔진의 평가 기준이 아니라 **후속 활용 사례**입니다. 따라서 `SSOT.md`의 “VLA용 데이터 공장”은 장기 응용으로 내려가고, 프로젝트의 중심 산출물은 **제한된 장비로 보정 가능한 예측 물리 엔진**에 가까워집니다.

여기서 다음 아키텍처 분기점이 생깁니다.

## 여덟 번째 질문

젖은 유리컵 grasp를 예측할 때, 엔진 내부에서 물리를 어느 정도까지 **공간적으로 직접 풀어야** 합니까?

### A. 명시적 고충실도 모델이 필수

- 고무 gripper pad를 deformable continuum/mesh로 계산
- 국소 접촉압력과 실제 접촉면적 계산
- 액막 두께와 배수 흐름을 공간장으로 계산
- stick/slip을 접촉점 또는 표면 요소 단위로 계산

결과만 맞는 축약 모델은 프로토타입으로 인정하지 않습니다.

### B. 해석 가능한 reduced-order model이면 충분

예를 들어 접촉부마다 다음 상태만 둡니다.

\[
A_{\text{contact}},\quad
p_{\text{mean}},\quad
h_{\text{film}},\quad
\mu_{\text{effective}},\quad
s_{\text{slip}}
\]

이 상태들을 물리적으로 해석 가능한 constitutive law로 갱신하고, hold-out 실험을 예측하면 됩니다. 고무 내부 응력장이나 액막 전체 유동장을 직접 계산할 필요는 없습니다.

### C. 해상도 적응형 hybrid

- 메시로 표현된 형상·큰 변형·큰 액체 덩어리는 직접 계산
- 미세 거칠기, 얇은 액막, 실제 접촉면적처럼 현재 해상도 아래의 현상은 reduced interface model로 처리
- 필요할 때 특정 부분만 더 높은 fidelity 모델로 교체 가능

현재 요구에는 C가 가장 자연스럽지만, 첫 프로토타입이 반드시 충족해야 할 최소선은 A/B/C 중 어디입니까?

---

> 👤 **USER TURN**

C가 현실적일 듯. 연산 성능도 고려해야함. 일반적인 시뮬레이션 엔진처럼 미리 정해진 고정 초기 조건에서 연산만 하면 되는게 아니라 vla모델 평가 또는 online RL을 하거나 하는 경우 어떤 action을 할지를 추론하느냐에 따라 로봇이 다르게 움직일 수 있으므로 pro6000 blackwell 정도 gpu에서 준-realtime 시뮬레이션이 가능해야함.

---

> 🤖 **GPT TURN**

이 답으로 성능 요구가 처음으로 명확해졌습니다.

당신이 원하는 것은 오프라인 해석기가 아니라,

> **행동에 따라 접촉·변형·젖음 상태가 즉시 달라지는 폐루프 로봇 시뮬레이터이며, 단일 워크스테이션급 GPU에서 준실시간으로 실행되어야 한다.**

따라서 첫 프로토타입부터 다음 구조가 필요합니다.

\[
\text{resolved geometry / bulk dynamics}
+
\text{sub-grid interface model}
+
\text{adaptive fidelity}
+
\text{multi-rate stepping}
\]

예를 들어 rigid/articulation은 높은 빈도로 갱신하되, 고무 내부 변형이나 액막 상태는 필요한 접촉 영역에서만 계산하고, 느리게 변하는 상태는 더 낮은 빈도로 갱신해야 합니다. 반대로 모든 고무 pad와 액막을 매 timestep마다 고해상도 continuum로 풀면 준실시간 목표와 양립하기 어렵습니다.

또한 “빠른 모드”가 전혀 다른 경험적 물리여서는 안 됩니다. 동일한 재료·계면 모델을 공유하면서 공간 해상도, 반복 횟수, active region, update frequency만 조절하는 **fidelity ladder**가 적절합니다.

## 아홉 번째 질문

여기서 `준-realtime`과 `online RL`의 요구를 분리해야 합니다. 다음 중 프로젝트의 **필수 성능 기준**은 어디입니까?

### A. 단일 환경 폐루프 준실시간

RTX PRO 6000 Blackwell급 GPU 한 장에서:

- Isaac Sim 렌더링 포함
- 로봇 1대와 작업 장면 1개
- 제어 주기 약 10–30 Hz
- 물리 시뮬레이션 wall-clock 속도 최소 실시간의 0.5–1배

VLA 평가와 interactive test가 가능하면 충분하며, 병렬 RL throughput은 후속입니다.

### B. 소수 병렬 환경

같은 GPU에서 4–16개 환경을 동시에 굴리면서 각 환경이 최소 실시간의 0.5배 정도를 유지해야 합니다.

### C. 대규모 online RL

수십–수백 개 환경을 병렬 실행해야 하며, 개별 장면의 상세 접촉·젖음 모델보다 aggregate simulation throughput이 우선입니다.

### D. 두 단계

- **필수:** A
- **후속:** 동일한 물리 모델의 축약 fidelity를 사용해 B로 확장
- C는 당분간 명시적 비목표

현재 요구를 보면 **D**가 가장 일관적으로 보입니다. 어느 것이 맞습니까?

---

> 👤 **USER TURN**

D. C는 애초에 일반 RL이 아니라 VLA인 시점에서 절대 불가능함. C는 언급조차 할 필요없이 영원히 비목표임. B는 당장 필요하지는 않은데 가능하면 좋긴 할 듯.

---

> 🤖 **GPT TURN**

좋습니다. 성능 목표는 이렇게 고정할 수 있습니다.

> **필수:** RTX PRO 6000 Blackwell급 GPU 1장에서 Isaac Sim 렌더링을 포함한 단일 폐루프 환경을 준실시간으로 실행한다.
> **선택적 확장:** 동일 물리 모델의 낮은 fidelity profile로 4–16개 정도의 소수 병렬 환경을 지원한다.
> **영구 비목표:** 수십–수백 개 환경을 병렬화하는 일반 RL용 throughput 엔진.

여기서 중요한 점은 `online RL`도 대규모 병렬 RL을 뜻하지 않는다는 것입니다. 이 프로젝트에서 필요한 online interaction은 **하나 또는 소수의 고충실도 환경에서 VLA·policy가 행동을 바꾸며 상호작용하는 것**입니다.

이제 현재 프로젝트 범위를 크게 좌우할 질문이 하나 있습니다.

## 열 번째 질문

젖은 유리컵 grasp를 다룰 때, 첫 프로토타입이 **컵이 젖게 되는 과정 자체**까지 계산해야 합니까?

### A. 젖음 상태를 초기조건으로 제공해도 된다

컵 표면에 다음과 같은 필드를 외부에서 지정합니다.

\[
h_{\mathrm{film}}(x),\quad
\text{wetness}(x),\quad
\text{contamination}(x)
\]

엔진은 이 상태에서 접촉, 배수, 마찰 변화, 고무 변형, 미끄럼을 계산합니다. 물을 붓거나 튀겨서 그 상태가 형성되는 과정은 첫 프로토타입 범위 밖입니다.

### B. 제한된 젖음 진화는 계산해야 한다

초기 액막은 제공할 수 있지만, grasp 중에 발생하는 다음 현상은 계산해야 합니다.

- gripper 압력에 따른 액막 배수
- 접촉 영역 밖으로의 이동
- 시간에 따른 건조 또는 잔류
- 접촉과 해제에 따른 국소 wetness 변화

다만 컵에 물을 붓는 자유표면 유동까지는 필요하지 않습니다.

### C. bulk liquid부터 접촉까지 end-to-end여야 한다

물을 붓거나 컵을 적시는 자유표면 유동에서 시작해, 표면 액막 형성, 배수, 젖은 접촉과 미끄럼까지 하나의 결합계로 계산해야 합니다.

첫 프로토타입의 최소선은 A/B/C 중 어디입니까?

---

> 👤 **USER TURN**

C가 명시적 목표이긴 한데, 난이도 때문에 prototype이라고 A나 B로 타협하면 추후 확장시 오히려 dept가 될 것 같음. 처음부터 C로 가는게 좋을 듯.

---

> 🤖 **GPT TURN**

그렇다면 **C를 최종 목표가 아니라 첫 아키텍처의 경계조건으로 고정**해야 합니다.

다만 여기서 한 가지를 분리해야 합니다.

\[
\text{C-compatible prototype}
\neq
\text{모든 구성요소가 처음부터 고충실도인 prototype}
\]

첫 버전이 반드시 가져야 할 것은 다음 **인과 경로 전체**입니다.

\[
\text{bulk liquid}
\rightarrow
\text{표면 충돌·부착}
\rightarrow
\text{surface film 형성}
\rightarrow
\text{중력·표면장력에 따른 이동}
\rightarrow
\text{gripper 접촉 시 배수}
\rightarrow
\text{고무 변형·압력분포}
\rightarrow
\text{습식 마찰·stick/slip}
\rightarrow
\text{컵 운동}
\]

반면 각 화살표의 첫 구현은 제한된 모델일 수 있습니다. 예를 들어 bulk liquid는 비교적 거친 입자 해상도, 액막은 2D surface field, 고무는 reduced deformable patch model로 시작할 수 있습니다. 중요한 것은 나중에 고충실도 solver로 교체할 때 **상태 표현과 결합 계약을 폐기하지 않아도 되는 것**입니다.

오히려 “전체 체인의 일부를 생략한 정교한 solver”보다 “전체 체인을 잇는 교체 가능한 저충실도 구현”이 C로 가는 데 기술 부채가 적을 수 있습니다.

## 열한 번째 질문

첫 프로토타입에서 다음 중 어느 수준을 요구합니까?

### A. 전체 인과사슬 우선

bulk liquid부터 slip까지 모든 단계가 실제로 연결되어 동작해야 합니다. 다만 일부 단계는 reduced-order 또는 coarse model이어도 됩니다. 각 모델은 나중에 더 정교한 구현으로 교체 가능해야 합니다.

### B. 전체 인과사슬 + 모든 핵심 단계의 정량 검증

bulk liquid, 부착·액막 형성, 배수, 고무 변형, 습식 마찰을 각각 별도의 hold-out 실험으로 정량 검증해야 첫 프로토타입으로 인정합니다.

### C. 절충

전체 사슬은 A처럼 완성하되, 첫 정량 검증은 최종 관측량에 집중합니다.

- slip onset grip force
- slip distance 또는 slip velocity
- lift 성공·실패
- 젖음량 변화에 따른 추세

중간 상태인 액막 두께나 접촉압력은 우선 계측·기록하되, 독립적인 정량 검증은 후속으로 둡니다.

현재 장비와 목표를 함께 고려하면 **C**가 가장 현실적인데, 당신이 생각하는 최소선은 무엇입니까?

---

> 👤 **USER TURN**

ok. C.

---

> 🤖 **GPT TURN**

확정하겠습니다.

첫 프로토타입은 **bulk liquid → 표면 액막 → gripper 접촉·배수 → 고무 변형 → 습식 마찰 → cup slip**의 전체 인과사슬을 실제로 연결합니다. 다만 초기 검증은 중간장 각각을 완전히 인증하는 것이 아니라, 보정에 쓰지 않은 조건에서 다음 최종 관측량을 예측하는 데 집중합니다.

\[
F_{\text{slip onset}},\quad
v_{\text{slip}}(t),\quad
d_{\text{slip}}(t),\quad
\text{lift success/failure}
\]

액막 두께, 접촉압력, 실제 접촉면적 등의 중간 상태는 반드시 기록·관찰 가능해야 하지만, 첫 프로토타입의 독립 정량 검증 대상에서는 제외합니다.

## 열두 번째 질문 — PhysX를 어디까지 남길 것인가

이 결정이 현재의 contact 연구 포크를 계속할지, 훨씬 단순한 통합 경로로 전환할지를 좌우합니다.

### A. PhysX 위에 보정 계층을 얹는다

PhysX가 rigid-body와 기본 contact를 계속 계산하고, omniphysics가 습식 마찰·액막·고무 변형에 따른 correction force를 추가합니다.

장점은 구현이 빠르다는 것이지만, PhysX의 penetration, contact manifold, friction 결과에 종속되어 원래 문제를 근본적으로 해결하지 못할 수 있습니다.

### B. 물리적 권한을 명시적으로 분리한다

- PhysX 또는 Newton: 로봇 articulation, joint drive, 관련 없는 배경 물체
- omniphysics: 대상 계면의 충돌·접촉·변형·마찰·액체 및 컵 운동
- 동일 물체에 대해 두 엔진이 힘을 중복 계산하지 않도록 **한 시점에 한 solver만 authoritative**
- Isaac Sim: 렌더링, 센서, 장면 구성, 제어 인터페이스

예를 들어 `gripper pad ↔ wet glass cup` 접촉에서는 PhysX contact를 끄고 omniphysics가 접촉력을 계산하지만, UR 로봇의 관절 구동까지 처음부터 새로 구현하지는 않습니다.

### C. PhysX를 물리 계산에서 완전히 제거한다

로봇 articulation, rigid dynamics, collision, contact, deformable, liquid를 모두 omniphysics/Newton 경로가 담당하고 Isaac Sim은 시각화·센서 shell로만 사용합니다.

장기적으로 가장 순수하지만, 첫 프로토타입 전에 articulation과 범용 rigid-body engine까지 다시 만들어야 할 위험이 있습니다.

당신이 원하는 엔진으로 인정할 수 있는 **최소선**은 A/B/C 중 어디입니까? 현재 요구에는 **B**가 가장 잘 맞아 보입니다.

---

> 👤 **USER TURN**

C. 괜히 기존 엔진의 요소를 남겨두고 그 사이의 상호작용들의 관계와 제어들을 통제하고 관리하느라 골머리썩는 것 보다는 내가 완전히 통제할 수 있는 substrate 위에서 문제를 해결하는게 나아보임. articulation이나 rigid-body engine은 newton이나 여타 open-source에서 참고할 수 있는 핵심 logic들을 최대한 참고해서 빠르게 재구현하면 됨(라이브러리를 그대로 가져다 쓰거나 코드를 그대로 porting하는 건 피했으면 좋겠음).

---

> 🤖 **GPT TURN**

이 선택으로 프로젝트의 정체는 분명해집니다.

> **Isaac Sim은 렌더링·센서·장면 입출력 shell이고, `omniphysics`가 모든 authoritative physics state와 dynamics를 소유한다.**

즉 다음을 모두 자체 경로에서 처리해야 합니다.

\[
\text{articulation}
+\text{rigid dynamics}
+\text{collision/contact}
+\text{deformable}
+\text{liquid/film}
+\text{coupling}
\]

PhysX나 Newton solver가 동일 물체의 힘이나 상태를 일부라도 소유하는 혼합 권한 모델은 피합니다. Warp 같은 GPU 실행 기반을 사용하는 것은 가능하지만, Newton의 solver를 장기적인 물리 권한자로 두지는 않는 방향입니다. 현재 저장소가 표방하는 “Newton/Warp 기반 solver + Isaac Sim integration”보다 더 강한 독립 엔진 노선입니다. fileciteturn6file0L3-L8

다만 **“완전히 통제한다”와 “모든 수치·기하 primitive를 직접 작성한다”는 별개**입니다. 후자까지 요구하면 wet grasp 이전에 범용 엔진 기반을 만드는 데 대부분의 시간이 들어갈 수 있습니다.

## 열세 번째 질문 — 자체 구현의 경계

다음 중 어디까지를 직접 소유해야 합니까?

### A. 물리 의미론과 상태 소유권만 자체 구현

`omniphysics`가 constitutive law, 상태, coupling, time integration의 선택을 소유합니다. 다만 collision detection, sparse solver, BVH 등의 검증된 오픈소스 구성요소는 fork 가능한 dependency로 사용할 수 있습니다.

### B. 물리엔진 핵심은 전부 자체 구현

직접 구현:

- rigid/articulation dynamics
- broad phase와 narrow phase
- contact manifold
- constraint/contact solver
- deformable 및 fluid solver
- coupling과 stepping

허용되는 외부 기반은 Warp/CUDA, BLAS·sparse primitive, 기본 mesh I/O처럼 **물리 의미를 결정하지 않는 저수준 도구**뿐입니다. 오픈소스 엔진은 알고리즘과 설계 참고 자료로만 사용합니다.

### C. CUDA 위의 대부분을 자체 구현

BVH, 충돌 기하, 선형해법까지 직접 구현하며 외부 수치 라이브러리도 최소화합니다.

현재 말씀하신 의도는 **B**에 가장 가까워 보입니다. 맞습니까?

---

> 👤 **USER TURN**

응. B에 가까운 듯. warp를 이용해 cpu가 아니라 gpu에서 구동함으로써 성능 이득을 얻을 수 있는 모든 요소는 warp나 cuda로 구현해서 아까 언급한 실시간성을 챙기는 것이 목표야. 핵심 요소가 warp로 구현된 기존 source가 있는 경우 채택해도 되는데, 우리 엔진과 잘 조화되야함.

---

> 🤖 **GPT TURN**

좋습니다. 그러면 외부 코드 재사용 원칙은 **“직접 작성 여부”가 아니라 “물리적 권한과 통제권을 누가 갖는가”**로 정리하는 것이 맞습니다.

## 현재까지 확정된 구현 경계

`omniphysics`가 직접 소유해야 하는 것은 다음입니다.

- authoritative physical state
- rigid-body 및 articulation dynamics
- collision/contact 의미론
- deformable·liquid·surface-film 상태
- constitutive law와 material/interface model
- coupling 순서와 time integration
- solver convergence·failure semantics
- fidelity profile과 calibration parameter의 의미

반면 다음은 외부 구현을 채택하거나 참고할 수 있습니다.

- Warp/CUDA kernel과 병렬 primitive
- BVH 구축, spatial hashing, scan/reduction 같은 범용 GPU 알고리즘
- sparse linear algebra와 iterative solver primitive
- 기존 Warp 기반 collision 또는 constraint 알고리즘
- 검증된 수치기법의 구현 패턴

단, 외부 source를 채택하더라도 그것이 **숨겨진 두 번째 물리엔진**이 되어서는 안 됩니다. 자체 world state, 자체 stepping lifecycle, 불투명한 contact law를 그대로 끌고 오는 방식은 피하고, `omniphysics`의 상태·계약·스케줄에 편입되는 교체 가능한 구성요소여야 합니다.

즉 원칙은 다음에 가깝습니다.

> **물리 의미론은 자체 소유하고, 범용 계산 primitive와 조화 가능한 GPU 구현은 적극 재사용한다.**

이는 “모든 코드를 처음부터 작성한다”보다 현실적이고, 기존 엔진에 물리 권한을 위임하는 것과도 다릅니다.

## 열네 번째 질문 — 일반성을 어떻게 증명할 것인가

당신이 처음에 A 수준의 협소한 프로토타입을 거부한 핵심 이유는, wet-cup demo에만 맞춘 구조가 나중에 확장되지 않을 수 있다는 점이었습니다. 그렇다면 첫 프로토타입에서 **재사용 가능성을 어느 정도까지 실제 장면으로 입증해야 하는지** 정해야 합니다.

### A. 하나의 통합 장면군에서 넓게 일반화

`wet deformable grasp`라는 한 장면군 안에서 다음을 hold-out으로 바꿉니다.

- 컵 형상과 크기
- gripper pad 형상
- 고무 재료
- 건식·습식 상태와 물의 양
- grip force
- 접근 위치·속도·자세
- 표면 패턴과 instance overlay

이 범위에서 재튜닝 없이 예측되면 첫 프로토타입의 일반성으로 충분합니다.

### B. 통합 장면군 + 독립적인 dry-contact 장면

위 wet grasp 외에, 같은 rigid/contact/articulation core를 사용하는 별도 장면을 하나 더 요구합니다.

예:

- peg insertion
- tight-fit assembly
- stacking 안정성
- dry grasp 후 외력에 의한 slip
- edge/contact-rich manipulation

wet grasp에만 필요한 liquid·film 계층은 사용하지 않더라도, **동일한 rigid/contact core가 별도 문제에서도 PhysX의 penetration·마찰·접촉 불안정 문제를 개선**해야 합니다.

### C. 세 가지 이상의 물리군

wet grasp, dry insertion/contact, cloth 또는 다른 deformable manipulation까지 첫 프로토타입에 포함합니다.

현재까지의 일반성 요구와 범위 통제를 함께 고려하면 **B**가 가장 균형적입니다. 첫 프로토타입의 최소 증명 범위는 A/B/C 중 어느 쪽입니까?

---

> 👤 **USER TURN**

C. wet grasp은 그냥 내 생각을 가장 전달하기 쉽게 든 예시였고, 가능하면 요즘 vla benchmark에서 표준적으로 사용되어온 libero, libero pro 등에서 등장하는 object/task, lehome 과 같은 최신 환경에서의 deformable/cloth 상호작용 등을 개선할 수 있었으면 좋겠음.

---

> 🤖 **GPT TURN**

이 답은 중요한 수정입니다. **wet grasp가 제품 정의가 아니라, 엔진이 보존해야 할 물리적 인과관계를 설명하는 예시**였던 것입니다.

또한 벤치마크들의 역할은 구분해야 합니다.

- **LIBERO/LIBERO-PRO**는 주로 rigid-object 조작 장면과 VLA 일반화 평가에 유용합니다. 특히 LIBERO-PRO의 확장은 object appearance·scale, 위치, 언어, task logic, environment 변화에 초점을 둡니다. 따라서 여기서 task와 asset을 가져올 수는 있지만, 그 자체를 물리 충실도의 ground truth로 삼기는 어렵습니다. citeturn578301view1
- **LeHome**은 명시적으로 가정환경의 deformable-object manipulation을 목표로 하며, 의류·식품 등 다양한 변형체와 저비용 로봇에서의 end-to-end 평가를 다룹니다. 이쪽은 cloth/deformable 범위의 직접적인 coverage target에 가깝습니다. citeturn578301view0

따라서 현재 목표는 대략 다음 세 축으로 보입니다.

\[
\begin{aligned}
&\textbf{Rigid/contact:} &&
\text{grasp, insertion, stacking, articulation, penetration 방지} \\
&\textbf{Deformable/cloth:} &&
\text{folding, dragging, grasping, snagging, self/contact interaction} \\
&\textbf{Liquid/interface:} &&
\text{pouring, wetting, wiping, wet contact, phase/interface state}
\end{aligned}
\]

이것은 다시 상당히 큰 범위입니다. 하지만 이전 계획과의 차이는 **각 현상을 독립적인 논문급 인증 대상으로 만드는 것이 아니라, 동일한 엔진 구조가 이 세 물리군을 실제로 수용할 수 있음을 증명하는 것**이어야 한다는 데 있습니다.

## 열다섯 번째 질문 — 세 물리군의 검증 깊이

첫 프로토타입에서 세 축 모두를 어느 수준까지 요구합니까?

### A. 세 축 모두 동등한 정량 검증

각 축마다 실제 hold-out 실험과 비교합니다.

- rigid/contact: slip onset, insertion force, penetration
- cloth: 펼침·접힘 형상, 처짐, grasp 후 변형
- liquid/interface: 유량, 잔류량, 젖음·미끄럼

세 축 모두 정량 예측력을 보여야 프로토타입으로 인정합니다.

### B. 세 축 모두 실제로 연결하되, 하나만 정량 주력으로 검증

세 축은 모두 동일한 runtime, material system, collision/contact core, coupling 계약 위에서 실제로 동작해야 합니다. 다만 첫 번째 정량 anchor는 하나만 선정하고, 나머지 둘은 다음 조건까지만 요구합니다.

- 장면별 scripted force나 결과 조작 금지
- 공통 material/contact/state 모델 사용
- 명백한 비물리 현상과 수치 불안정 없음
- 후속 정량 검증을 위해 중간 상태와 측정량 기록 가능
- solver 교체 없이 fidelity를 높일 수 있는 구조

### C. 두 축을 정량 검증하고 세 번째는 구조적 시연

예를 들어:

- rigid/contact: 정량 검증
- cloth/deformable: 정량 검증
- liquid/wet coupling: 전체 인과사슬은 연결하되 초기에는 characterization 수준

현재 인력·장비·준실시간 요구를 고려하면 **C가 범용성 주장을 지키면서도 다시 검증 연구에 매몰되지 않을 가능성이 가장 높습니다.**

A/B/C 중 어느 수준이 첫 프로토타입의 최소선입니까?

---

> 👤 **USER TURN**

C 다만 rigit/contact+liquid/wet coupling 을 main으로.

---

> 🤖 **GPT TURN**

좋습니다. 그러면 첫 프로토타입의 검증 우선순위는 다음처럼 잡는 것이 맞습니다.

1. **주력 정량 축 1 — rigid/contact**
   - 관통 억제
   - 정지마찰–미끄럼 전이
   - 접촉 순응성·재질 변형이 grip/insertion 결과에 미치는 영향
   - 새로운 형상·하중·속도 조건으로의 hold-out 전이

2. **주력 정량 축 2 — liquid/wet coupling**
   - bulk liquid 이동
   - 고체 표면 부착과 film 형성
   - 접촉 압력에 따른 배수
   - wetness가 마찰·grip·slip에 미치는 영향
   - bulk liquid부터 최종 물체 운동까지 인과사슬 보존

3. **구조적 증명 축 — cloth/deformable**
   - 동일 runtime, collision/contact, material/state 체계에서 실제 동작
   - 메시 관통·끼임·자기충돌 실패가 명백히 개선
   - 첫 프로토타입에서는 정밀 실험 일치까지 요구하지 않음
   - 이후 정량 검증으로 확장 가능한 상태·계측 인터페이스는 갖춤

이는 현재 SSOT의 단순한 `contact → 나머지` 순차 사다리와 다릅니다. **rigid/contact와 liquid/wet coupling을 하나의 공동 주력 경로로 묶고**, cloth는 아키텍처 일반성을 검증하는 세 번째 소비자로 두는 형태입니다.

## 열여섯 번째 질문 — 첫 프로토타입의 대표 장면군

이제 “프로토타입 완료”를 판정할 구체적인 장면 세트를 정해야 합니다. 다음 중 어느 구성이 의도에 가장 가깝습니까?

### A. 물리 메커니즘 중심의 소형 시험대

- dry/wet cup grasp
- peg insertion 또는 tight-fit assembly
- liquid pouring과 표면 wetting
- cloth drape/pull-through

검증과 디버깅은 쉽지만, VLA benchmark와의 연결성은 약합니다.

### B. 실제 benchmark task를 그대로 중심에 둔다

LIBERO/LIBERO-PRO/LeHome의 대표 task를 선택하고, 그 장면 안에서 물리 정확도를 개선합니다.

응용 관련성은 높지만, benchmark asset과 task가 물리 검증에 적합하지 않거나 ground truth를 얻기 어려울 수 있습니다.

### C. **쌍을 이루는 두 계층**

각 주력 현상마다:

- **계층 1:** 단순하고 계측 가능한 canonical physics scene
  예: 경사면 slip, squeeze-and-lift, insertion, pour-on-plate, wet grasp
- **계층 2:** 동일 solver와 material parameter를 사용하는 benchmark-like scene
  예: LIBERO 계열 조작, kitchen pouring/wiping, LeHome cloth task

계층 1에서 보정·정량 검증하고, **파라미터를 다시 맞추지 않은 채** 계층 2로 전이합니다.

첫 프로토타입은 어느 구성을 따라야 합니까? 현재까지의 “정량 예측 + 실제 VLA 환경에 쓰일 일반성” 요구에는 **C**가 가장 잘 맞습니다.

---

> 👤 **USER TURN**

Ok. C가 좋아보이네.

---

> 🤖 **GPT TURN**

좋습니다. 그러면 첫 프로토타입의 장면 구조는 다음으로 고정할 수 있습니다.

\[
\text{canonical physics scene}
\;\xrightarrow[\text{동일 파라미터 유지}]{\text{calibration + hold-out validation}}\;
\text{benchmark-like scene}
\]

예를 들어 rigid/contact 축이라면 경사면·squeeze-and-lift·insertion 시험에서 보정하고, 그 파라미터를 다시 맞추지 않은 채 LIBERO 계열 조작 장면으로 옮깁니다. Liquid/wet 축도 단순 pouring·wet-grasp 시험대에서 검증한 뒤, kitchen-like manipulation으로 전이합니다.

## 열일곱 번째 질문 — “PhysX보다 낫다”의 판정 방식

프로토타입의 성공 조건을 어떤 형태로 두는 것이 맞습니까?

### A. 상대 비교 중심

동일한 보정 데이터와 비슷한 연산 예산을 제공했을 때, `omniphysics`가 PhysX 또는 기본 Newton보다 주요 관측량을 더 정확하게 예측하면 성공입니다.

단점은 기존 엔진이 매우 부정확한 경우, 절대적으로는 여전히 좋지 않은 모델도 통과할 수 있다는 점입니다.

### B. 절대 정확도 중심

PhysX와 관계없이, 사전에 정한 hold-out 오차 기준을 충족해야 합니다.

예:

- slip onset force 오차
- insertion force–displacement curve 오차
- cup trajectory 또는 slip distance 오차
- pouring 후 잔류량 오차
- 비물리적 penetration 상한

단점은 현재 장비로 정밀 ground truth를 얻기 어려운 항목이 있다는 것입니다.

### C. 이중 기준

다음을 모두 요구합니다.

1. **최소 절대 품질선**
   - 폭발, 심각한 관통, 비정상 에너지 증가 같은 치명적 실패가 없어야 함
   - 측정 가능한 주요 결과가 넓게 설정된 허용 오차 안에 들어와야 함

2. **기존 엔진 대비 유의미한 개선**
   - 동일 calibration budget
   - 동일 장면과 hold-out 조건
   - 유사한 GPU 및 wall-clock 예산
   - 핵심 지표 일부에서 PhysX/Newton보다 명확히 우수
   - 다른 핵심 지표를 심각하게 악화시키지 않음

3. **성능 하한**
   - 단일 Isaac Sim 폐루프 환경에서 준실시간 조건 유지

정확한 오차율과 개선 폭은 나중에 장면별로 정하되, **판정 구조 자체**는 A/B/C 중 어느 것이 맞습니까?

---

> 👤 **USER TURN**

C

---

> 🤖 **GPT TURN**

좋습니다. 성공 판정 구조는 다음으로 고정됩니다.

\[
\boxed{
\text{절대 품질 하한}
\;\land\;
\text{PhysX/기본 Newton 대비 개선}
\;\land\;
\text{준실시간 성능 하한}
}
\]

즉 기존 엔진보다 조금만 나은 것으로는 부족하고, 절대적으로도 쓸 수 있는 수준이어야 합니다. 반대로 물리 오차만 낮고 폐루프 실행이 지나치게 느린 모델도 프로토타입으로 인정하지 않습니다.

이제 `CLAUDE.md`를 가장 크게 바꿀 운영 원칙을 정해야 합니다. 현재 프로젝트가 정체된 직접 원인은, **후보를 구현하고 측정하기 전에 법칙·게이트·증거 구조를 지나치게 완결하려 한 것**에 가깝습니다.

## 열여덟 번째 질문 — 연구·검증 루프를 어떻게 운영할 것인가

### A. 현재의 동결 우선 방식을 유지

물리 법칙, 파라미터 출처, acceptance criterion을 구현 전에 엄격히 확정하고, one-shot 실행으로 판정합니다. 느리더라도 사후 튜닝 가능성을 최대한 차단합니다.

### B. 일반적인 반복 개발 방식

결과를 보면서 formulation과 파라미터를 자유롭게 반복 수정합니다. 최종 결과가 좋으면 충분하며, 별도의 calibration/hold-out 분리는 최소화합니다.

### C. **탐색과 판정을 명시적으로 분리**

**Exploration lane**
- 구현 → 측정 → 수정의 빠른 반복 허용
- calibration scene 결과를 보며 formulation과 수치 파라미터를 조정 가능
- 실패한 아이디어도 가볍게 폐기
- 사전 ADR, 독립 리뷰, one-shot gate를 요구하지 않음
- 일정 시간 안에 진전이 없으면 단순 모델이나 다른 formulation으로 전환

**Evaluation lane**
- 후보가 충분히 성숙하면 코드·파라미터·평가 절차를 동결
- 보정에 쓰지 않은 hold-out 조건에서 실행
- PhysX/Newton 비교와 성능 측정
- 이 단계 결과를 본 뒤 같은 평가 세트를 다시 맞추면 해당 평가는 무효
- 정식 엔진 경로로 승격할 때만 ADR·회귀 테스트·재현 artifact를 요구

이 구조에서는 anti-Goodhart 원칙을 버리지 않지만, **개발 전 과정이 아니라 공식 판정 경계에서만 강하게 적용**합니다.

앞으로의 기본 운영 모델은 A/B/C 중 어느 것이어야 합니까?

---

> 👤 **USER TURN**

뭐가 좋을 것 같아? 모든 개발/검증/실행 등은 claude fable 5가 orchestration 하는 다중 codex(gpt-5.6-sol, xhigh-ultra) worker로 거의 완전 자율 진행 될거고, 나는 중간중간 진행상황이 drift 되거나 하지 않는지 검토하며 피드백을 주는 정도만 수행할 거야.

---

> 🤖 **GPT TURN**

## 추천: **C**, 정확히는 `Exploration → Evaluation → Promotion`의 3단계 모델

완전 자율 다중 에이전트 체계에서는 A와 B 모두 위험합니다.

- **A를 유지하면**, 에이전트들은 구현보다 문서·게이트·리뷰처럼 “안전하게 완료 판정을 받을 수 있는 작업”을 계속 생성합니다. 현재 `CLAUDE.md`의 derive→freeze→one-shot 규율과 모든 작업의 사전 등록·라운드별 리뷰 패킷 요구가 결합되면서, 실제로 이런 거버넌스 attractor가 생겼습니다. fileciteturn6file0L10-L24 fileciteturn6file0L58-L66
- **B로 풀어버리면**, 여러 worker가 calibration scene과 테스트에 맞춰 formulation, threshold, residual correction을 반복 조정하면서 자신도 모르게 benchmark에 과적합할 가능성이 높습니다.
- 따라서 **빠른 반복은 허용하되, 공식 평가와 승격 경계만 엄격하게 격리하는 C**가 맞습니다.

현재처럼 “한 번 보고 수정하면 Goodhart”라고 보는 것은 지나치게 경직되어 있습니다. 연구개발은 결과를 보고 수정해야 합니다. 금지해야 하는 것은 **결과를 본 뒤에도 같은 hold-out을 독립 증거인 것처럼 계속 사용하는 것**입니다.

---

## 권장 운영 구조

### 1. Exploration lane — 기본 작업 모드

여기서는 자유롭게 반복합니다.

- calibration scene 결과를 보면서 formulation 수정
- 수치 안정화, time step, solver iteration 조정
- 새로운 constitutive law나 contact formulation 비교
- 빠른 prototype, 실패, 폐기
- 동일 calibration set 반복 사용
- worker 간 설계·구현·검증 fan-out

이 단계에서는 원칙적으로 다음을 요구하지 않습니다.

- 사전 ADR
- 외부 리뷰 패킷
- one-shot 실행
- 독립 reviewer 승인
- 완결된 이론적 인증 문서
- 모든 micro-task의 레지스트리 등록

대신 자동으로 남겨야 하는 것은 간단합니다.

```text
candidate_id
code_commit
configuration
calibration scenes
metrics before/after
runtime
known failures
next decision
```

즉 **실험 이력은 남기되, 실험 전에 법전을 완성하지 않습니다.**

Exploration의 완료 조건도 문서가 아니라 다음 중 하나여야 합니다.

1. 실행 가능한 candidate가 생겼다.
2. 측정 가능한 개선이 있었다.
3. 정해진 계산·시간 예산을 소진해 후보를 폐기했다.

---

### 2. Evaluation lane — 격리된 공식 판정

후보가 어느 정도 성숙했을 때만 진입합니다.

진입 시 다음을 동결합니다.

- commit SHA
- solver configuration
- material/calibration parameters
- 평가 장면
- 평가 지표
- 반복 횟수와 random seed 정책
- PhysX/Newton baseline 조건
- GPU·wall-clock budget

그 후 별도의 evaluator worker가 **read-only candidate**를 평가합니다.

중요한 규칙은 다음입니다.

- implementer worker는 평가 중 candidate를 수정하지 못함
- evaluator는 acceptance threshold를 바꾸지 못함
- stochastic scene이면 한 번이 아니라 사전에 정한 \(N\)회 반복
- 평가가 실패해도 후보를 수정할 수 있음
- 다만 수정 후에는 새로운 `candidate_id`가 됨
- 이미 노출된 hold-out은 calibration 자료로 전환
- 다음 공식 승격 증거에는 새로운 hold-out split 또는 hidden seed set을 사용

즉 기존의 문자 그대로의 `one-shot` 대신:

> **한 candidate version에 대해 평가 프로토콜을 사후 변경하지 않는다.**

이 정도면 anti-Goodhart의 목적을 보존하면서도 정상적인 연구 반복을 막지 않습니다.

---

### 3. Promotion lane — 실제 엔진 경로로 승격

Evaluation을 통과한 후보만 정식 runtime에 들어갑니다.

이때 처음으로 다음을 요구합니다.

- concise ADR
- regression test
- absolute-quality 결과
- PhysX/Newton 상대 비교
- 준실시간 성능 결과
- 지원·비지원 regime
- calibration 요구량
- failure semantics
- benchmark-like scene 전이 결과

ADR은 후보를 만들기 위한 허가서가 아니라, **이미 증거가 확보된 결정을 고정하는 기록**이어야 합니다.

현재 프로젝트에서는 ADR과 gate가 구현의 선행조건으로 작동하는 경우가 많았습니다. 이를 뒤집어야 합니다.

\[
\text{implement and learn}
\rightarrow
\text{freeze and evaluate}
\rightarrow
\text{document and promote}
\]

---

## 자율 에이전트 환경에 추가할 핵심 제약

### 구현 산출물 우선

다음은 milestone 진척으로 계산하지 않는 것이 좋습니다.

- 문서만 추가한 research fork
- gate를 위한 gate
- 리뷰 패킷의 연속 수정
- 아직 실행 candidate가 없는 acceptance 설계
- 같은 주장을 표현만 바꿔 재비준하는 작업

문서-only 작업은 명시적인 사용자 승인이나 실제 실패에서 파생된 경우에만 허용해야 합니다.

### Gate는 candidate 뒤에 온다

다음 순서를 금지해야 합니다.

```text
완벽한 gate 설계
→ gate 리뷰
→ gate 수정
→ 법칙 freeze
→ freeze 재리뷰
→ 구현 착수 여부 검토
```

대신:

```text
최소 candidate
→ calibration scene에서 failure mechanism 확인
→ 어떤 지표가 실제로 구별력을 갖는지 확인
→ evaluation protocol 동결
→ hold-out 평가
```

### 역할 분리

- **Implementer:** candidate 작성과 calibration
- **Evaluator:** 동결된 candidate 판정
- **Adversarial reviewer:** 승격 직전 failure mechanism 공격
- **Orchestrator:** 예산, WIP, 의존성, 승격 상태 관리

Adversarial review를 모든 탐색 iteration에 붙이면 현재와 같은 review loop가 재발합니다. 가장 비싼 리뷰는 **Evaluation 또는 Promotion 경계에서만** 사용해야 합니다.

### 자동 중단 규칙

한 formulation이 실패했을 때 에이전트가 스스로 더 큰 연구 프로그램을 생성하지 못하게 해야 합니다.

예를 들면:

- 사전 지정 exploration budget 소진
- 두 candidate 세대 연속으로 핵심 지표 개선 없음
- 준실시간 budget을 구조적으로 초과
- 요구 파라미터가 보유 장비로 식별 불가능
- 새 substrate나 대규모 아키텍처가 필요해짐

이 경우 자동으로 `PARK`만 하지 말고, 사용자에게 다음 형태로 올려야 합니다.

```text
1. 단순 모델로 축소
2. 다른 formulation으로 전환
3. 요구사항 완화
4. 별도 장기 연구 track 승인
```

에이전트가 4번을 자동 선택해서는 안 됩니다.

---

## 결론

당신의 운영 환경에는 **C가 명확히 적합**합니다. 다만 현재 `CLAUDE.md`의 anti-Goodhart 원칙을 조금 완화하는 정도로는 부족합니다.

핵심 전환은 다음입니다.

> **탐색 과정은 빠르고 반복적으로, 평가 과정은 격리되고 엄격하게, 문서화는 승격 이후에.**

현재는 탐색 단계부터 평가·인증 규율이 적용되어 있습니다. 이것이 자율 에이전트의 생산성을 물리 구현이 아니라 방법론 강화 쪽으로 흘려보낸 주된 구조적 원인입니다.

## 열아홉 번째 질문 — 인간 승인 경계

에이전트가 자율적으로 진행하되, 다음 중 어느 수준의 변경에서만 당신의 승인을 받게 하는 것이 좋습니까?

**A. SSOT 목표나 scope를 바꿀 때만 승인**

**B. 다음 네 가지에만 승인**
- SSOT 목표·비목표 변경
- 공식 evaluation metric/hold-out 변경
- 새로운 대규모 architecture 또는 research fork 착수
- 핵심 물리 메커니즘을 포기하거나 reduced model로 대체

**C. 모든 candidate의 Promotion 전에 승인**

제 추천은 **B**입니다. 구현과 승격은 자동화하되, 에이전트가 스스로 문제 정의나 판정 기준을 바꾸지는 못하게 하는 방식입니다.

---

> 👤 **USER TURN**

ok. b.

---

> 🤖 **GPT TURN**

확정합니다. 인간 승인은 다음 네 경계에서만 필요합니다.

- `SSOT.md`의 목표·비목표 변경
- 공식 evaluation metric 또는 hold-out 구성 변경
- 새로운 대규모 architecture/research fork 착수
- 핵심 물리 메커니즘을 포기하거나 reduced model로 대체

그 외의 후보 탐색, 구현, 실패·폐기, evaluation, 통과한 후보의 promotion은 고정된 규칙 안에서 에이전트가 자율적으로 수행합니다.

## 스무 번째 질문 — 구현 순서

일반성을 지키면서도 다시 substrate 설계에 매몰되지 않으려면, 다음 중 어떤 순서가 적절합니까?

### A. 공통 엔진 기반을 먼저 완성

rigid, articulation, collision, contact, deformable, fluid, coupling을 포괄하는 architecture와 contract를 먼저 설계·구현한 뒤 응용 장면을 붙입니다.

장기 구조는 깔끔해 보이지만, 현재 프로젝트가 빠진 문제를 반복할 가능성이 큽니다.

### B. **실행 가능한 수직 경로를 만들되, 초기에 세 물리군이 구조를 공격하게 한다**

1. 최소 authoritative runtime만 구축
   `world state`, stepping, GPU scheduling, material/interface registry, collision API, Isaac Sim state publication
2. rigid/contact canonical scene을 실제로 실행
3. 같은 runtime에 bulk liquid → surface film → wet contact 경로를 연결
4. 첫 prototype이 굳기 전에 cloth/deformable을 세 번째 소비자로 붙여, rigid/liquid에만 맞춘 abstraction을 조기에 깨뜨림
5. 두 개 이상의 실제 소비자가 필요로 하지 않는 추상화는 만들지 않음
6. 각 단계는 항상 Isaac Sim에서 실행 가능한 상태를 유지

즉 좁은 데모를 만드는 것은 아니지만, 범용 schema를 먼저 완성하지도 않습니다.

### C. 세 solver track을 병렬로 독립 개발한 뒤 통합

rigid/contact, liquid/wet, cloth를 각각 빠르게 만들고 나중에 공통 runtime으로 합칩니다. 병렬성은 높지만 상태·시간 적분·접촉 권한이 서로 달라져 통합 비용이 커질 수 있습니다.

제 판단은 **B**입니다. 특히 “공통화는 예측으로 하지 않고, 실제 두 번째·세 번째 물리 소비자가 요구할 때 한다”는 규칙이 중요합니다.

이 구현 순서를 채택해도 됩니까?

---

> 👤 **USER TURN**

ok. b.

---

> 🤖 **GPT TURN**

확정하겠습니다.

구현 원칙은 **“범용 기반을 먼저 완성하지 않고, 항상 실행 가능한 수직 경로를 유지하면서 실제 소비자 셋이 추상화를 형성하게 한다”**입니다.

\[
\text{minimal runtime}
\rightarrow
\text{rigid/contact}
\rightarrow
\text{liquid/wet coupling}
\rightarrow
\text{cloth/deformable로 구조 공격}
\]

각 단계는 Isaac Sim에서 실제로 실행 가능해야 하며, 두 개 이상의 물리군이 요구하지 않는 공통 추상화는 미리 만들지 않습니다. 세 track을 독립 개발한 뒤 나중에 통합하는 것도 피합니다.

## 스물한 번째 질문 — 진행을 시간으로 어떻게 강제할 것인가

자율 에이전트가 다시 장기간 substrate·방법론 작업으로 빠지지 않게 하려면, **실행 가능한 통합 산출물의 주기**를 정해야 합니다.

### A. 1주 단위 executable increment

매주 말 반드시 Isaac Sim에서 눈으로 확인할 수 있는 통합 실행 결과가 있어야 합니다. 물리 정확도가 낮더라도 이전 주보다 인과사슬이 하나 더 연결되어야 합니다.

### B. 2주 단위 executable increment

2주마다 다음 중 하나를 실제 실행 결과로 제출합니다.

- 새로운 물리 메커니즘 연결
- 기존 메커니즘의 측정 가능한 정확도 개선
- 준실시간 성능 개선
- 새로운 canonical 또는 benchmark-like scene 전이

문서·게이트·리팩터만으로는 한 주기를 종료할 수 없습니다.

### C. milestone 단위

기간은 고정하지 않고, 각 solver나 subsystem이 충분히 완성되면 통합합니다.

현재의 drift를 막으려면 **B**가 가장 현실적이라고 봅니다. 1주는 복잡한 GPU 물리 작업에 너무 짧아 피상적인 데모를 유도할 수 있고, C는 다시 무기한 연구를 허용할 가능성이 큽니다.

**2주마다 반드시 실행 가능한 통합 increment를 내는 규칙**을 채택해도 됩니까?

---

> 👤 **USER TURN**

음. 시간을 개발 주기에 넣는 건 내 지금 개발 방식과는 맞지 않는 것 같아. 이 프로젝트만 진행하는 게 아니라, 다른 연구들도 병행하고 있고, 가끔 그냥 개발을 멈추고 다른 일들을 우선 처리하다가 재개하는 경우도 있고, 그 주에 남은 토큰 한도에 따라 강제로 추가 진행을 못하는 일도 있고, 운좋게 사용량 초기화 이벤트가 있어서 하루만에 거의 무한정에 가까운 토큰을 쏟아부을 수 있는 날도 있어. 오히려 기존처럼 C가 제일 적합한 것 같은데, 만약 무기한 연구허용이 우려된다면 이런 상황에서 보완할 수 있는 방법을 생각해줘. 이 부분은 굳이 다시 묻지않고 자율적으로 보완해도 돼.

---

> 🤖 **GPT TURN**

맞습니다. 이 프로젝트에는 **달력 기반 cadence보다 작업량·의사결정 기반의 경계**가 적합합니다. 개발이 며칠 또는 몇 주 중단되더라도 그 자체를 실패로 보지 않고, 토큰·GPU 자원이 집중되는 날에는 제한 없이 전진할 수 있어야 합니다.

따라서 구현 주기는 **C — milestone 단위**로 두되, 무기한 연구를 막는 장치는 시간 대신 아래처럼 설계하는 것이 좋습니다.

## 비시간 기반 진행 통제

### 1. Milestone은 반드시 executable capability로 끝난다

milestone의 완료 조건은 문서나 설계 승인일 수 없습니다. 다음 중 하나가 실제로 실행되어야 합니다.

- 새로운 물리적 인과 경로가 연결됨
- 기존 경로의 hold-out 예측 정확도가 개선됨
- 동일 fidelity에서 성능이 개선됨
- canonical scene의 파라미터가 benchmark-like scene으로 전이됨
- 새 물리군이 기존 runtime을 실제로 소비함

다음은 milestone 완료로 인정하지 않습니다.

- ADR 또는 설계 문서만 완성
- gate/review 체계만 강화
- 실행 candidate 없는 architecture synthesis
- 같은 문서의 반복 재비준
- 테스트가 늘었지만 새로운 실행 동작이나 오류 탐지 능력이 없는 경우

문서 작업은 필요할 수 있지만, **실행 산출물을 지원하는 작업**이어야 합니다.

---

### 2. 연구 예산은 날짜가 아니라 `candidate generation`으로 제한한다

각 연구 문제는 착수할 때 다음을 선언합니다.

```text
research_question
mainline_capability_blocked
candidate_families_to_try
max_candidate_generations
max_major_architecture_pivots
required_measurements
fallback_options
human-approval boundary
```

기본값은 대략 다음 정도가 적절합니다.

- 후보 formulation: 최대 3개 family
- family당 큰 수정: 최대 2세대
- 신규 대규모 architecture pivot: 자동 허용 0회
- 동일 평가 기준에 대한 review/fix cycle: 최대 2회
- 그 이상은 현재 lane을 중단하고 결과를 요약

예를 들어 마찰 모델 후보가 3개이고 각 후보를 두 번 크게 수정했다면, 여섯 번째 이후의 반복은 에이전트가 자동으로 계속할 수 없습니다. 다음 중 하나를 선택해야 합니다.

1. 가장 나은 후보를 제한된 regime용으로 채택
2. simpler reduced model로 전환
3. mainline에서 분리해 장기 research backlog로 이동
4. 새로운 architecture fork를 인간에게 제안

이렇게 하면 개발을 몇 달 쉬어도 예산은 소모되지 않고, 하루에 많은 토큰을 투입해도 같은 가설을 무한 반복할 수 없습니다.

---

### 3. 연구 실패가 mainline 전체를 막지 못하게 한다

모든 research task는 다음 둘 중 하나로 분류합니다.

- **Mainline-critical:** 없으면 현재 executable vertical slice가 물리적으로 성립하지 않음
- **Fidelity-upgrade:** 있으면 더 정확하지만 현재 slice 실행에는 필수 아님

`Fidelity-upgrade` 연구가 실패하거나 PARK되어도 다음 milestone은 계속 진행합니다.

`Mainline-critical` 연구가 예산을 소진하면 에이전트가 자동으로 더 큰 연구 포크를 만들지 않고, 앞서 정한 인간 승인 경계로 올립니다.

현재 contact 연구에서는 특정 certification 방식의 실패가 사실상 전체 프로젝트의 임계경로가 되었습니다. 개정된 운영에서는 **해당 formulation의 승격 실패**와 **엔진 전체 진행 불가**를 분리해야 합니다.

---

### 4. WIP를 제한한다

자율 orchestrator가 동시에 활성화할 수 있는 것은 기본적으로 다음 정도가 적절합니다.

- primary executable milestone: 1개
- 이를 직접 지원하는 exploration lane: 최대 2개
- 독립적인 장기 연구 lane: 기본 0개, 인간 승인 시에만 추가

많은 task를 병렬 등록하는 것보다, 하나의 물리 경로가 실제 Isaac Sim 실행으로 수렴하게 만드는 것이 우선입니다.

---

### 5. 모든 작업 중단 지점은 재개 가능한 checkpoint를 남긴다

불규칙하게 개발을 멈추는 방식을 고려하면, 각 세션이나 유의미한 작업 묶음이 끝날 때 다음만 자동 기록하면 됩니다.

```text
last_executable_commit
exact_run_command
last_known_good_scene
current_candidate_id
calibration/evaluation split
latest metrics
known failure mechanism
next falsifiable hypothesis
uncommitted or transient artifacts
```

이것은 장문의 `PROGRESS.md` 서술보다 중요합니다. 다음 에이전트 세션이 문서를 다시 해석하지 않고 곧바로 마지막 실행 상태를 재현할 수 있어야 합니다.

---

### 6. 진행률은 task 수가 아니라 capability graph로 측정한다

예를 들어 첫 프로토타입의 capability graph를 다음처럼 둡니다.

```text
authoritative GPU runtime
  ├─ rigid/articulation
  ├─ collision/contact
  ├─ compliant surface
  ├─ bulk liquid
  ├─ solid-liquid transfer
  ├─ surface film
  ├─ wet contact/friction
  ├─ cloth/deformable consumer
  ├─ Isaac Sim sensor publication
  └─ calibration + hold-out evaluation
```

각 노드는 다음 상태만 가집니다.

- `absent`
- `executable`
- `characterized`
- `evaluated`
- `promoted`

커밋 수나 완료 task 수는 진행률 지표에서 제외합니다. 이는 현재 `CLAUDE.md`의 task/round 중심 운영을 executable capability 중심으로 바꾸는 핵심입니다. 현재 workflow는 모든 작업의 사전 등록과 라운드별 리뷰를 강하게 요구하므로, 에이전트가 구현보다 관리 가능한 문서 작업으로 끌릴 유인이 있습니다. fileciteturn6file0L58-L66

---

## 스물두 번째 질문 — 식별 불가능성과 불확실성

보유 장비로 3–5회의 간단한 실험만 수행하면, 여러 물리 파라미터 조합이 비슷한 결과를 낼 수 있습니다. 예를 들어 낮은 마찰계수와 높은 액막 유지 효과가 동일한 slip trajectory를 만들 수도 있습니다.

이때 엔진은 어떤 방식으로 결과를 제공해야 합니까?

### A. 항상 하나의 최적 파라미터만 사용

보정 결과에서 가장 잘 맞는 파라미터 세트 하나를 선택하고, 모든 실행을 결정론적으로 수행합니다. 빠르고 단순하지만 실제로 식별되지 않은 파라미터를 확정값처럼 취급할 위험이 있습니다.

### B. 항상 불확실성 분포를 유지

복수의 가능한 파라미터와 결과 분포를 유지하여 다음과 같이 출력합니다.

\[
P(\text{slip}),\qquad
F_{\text{onset}}\in[a,b],\qquad
d_{\text{slip}}(t)\pm\sigma(t)
\]

더 정직하지만 매 실행에서 ensemble을 굴리면 준실시간 목표와 충돌할 수 있습니다.

### C. 두 가지 실행 profile

- **Interactive profile:** 가장 가능성 높은 단일 파라미터 세트로 준실시간 실행
- **Uncertainty profile:** calibration, validation 또는 중요한 평가 시에만 소수 ensemble이나 parameter bounds를 실행
- 결과의 신뢰도가 낮은 조건에서는 engine이 이를 claim metadata로 명시
- 간단한 추가 실험으로 불확실성을 가장 많이 줄일 수 있는 조건도 제안

현재의 장비·보정 비용·준실시간 요구를 함께 보면 **C**가 가장 적합해 보입니다. 어느 쪽이 맞습니까?

---

> 👤 **USER TURN**

ok. c.

---

> 🤖 **GPT TURN**

확정합니다.

- **Interactive profile:** 단일 대표 파라미터 세트로 준실시간 실행
- **Uncertainty profile:** calibration·validation 때만 소수 ensemble 또는 parameter bounds 실행
- **Claim metadata:** 식별 불확실성, 적용 범위, 외삽 여부를 명시
- **Active calibration:** 추가로 어떤 간단한 실험을 하면 불확실성이 가장 크게 줄어드는지 제안

이 구조에서는 “정확한 상수 하나를 알아냈다”는 과장을 피하면서도, 실제 폐루프 실행 성능은 유지할 수 있습니다.

## 스물세 번째 질문 — Isaac Sim 호환성의 경계

PhysX를 물리 권한에서 완전히 제거하더라도, 기존 Isaac Sim·LIBERO 계열 장면을 얼마나 그대로 받아들여야 하는지가 남아 있습니다.

### A. Drop-in backend에 가깝게

기존 USD/Isaac Sim 장면을 거의 수정 없이 열면, 표준 rigid body·joint·collision·material 설정을 `omniphysics`가 모두 해석해야 합니다.

장점은 기존 환경 이식이 쉽다는 것이지만, 초기부터 PhysX schema의 넓은 호환 계층을 구현해야 합니다.

### B. 명시적인 지원 subset + adapter

다음과 같은 공통 요소만 직접 읽습니다.

- USD geometry와 transform
- mass/inertia
- 지원되는 joint·articulation
- collision geometry
- `omniphysics` material/interface schema
- sensor와 renderer 설정

기존 benchmark 장면은 얇은 adapter 또는 변환기를 통해 이 subset으로 변환합니다. 지원하지 않는 schema는 조용히 근사하지 않고 명시적으로 거부합니다.

### C. 완전 독자 scene format

Isaac Sim은 결과 시각화와 센서 출력에만 사용하고, 물리 장면은 별도의 자체 포맷으로 전부 다시 작성합니다.

제 판단은 **B**가 적합합니다. 기존 자산과 로봇 정의를 활용하면서도, PhysX의 의미론과 호환성 부채를 새 엔진 안으로 그대로 끌고 오지 않는 방식입니다.

첫 프로토타입의 최소 호환성 경계는 A/B/C 중 무엇입니까?

---

> 👤 **USER TURN**

ok. B.

---

> 🤖 **GPT TURN**

확정합니다.

- 기존 USD geometry, transform, mass/inertia, 지원 joint·articulation, collision geometry는 재사용
- 물리 의미는 `omniphysics` schema와 adapter가 명시적으로 변환
- 지원하지 않는 PhysX/Isaac schema는 추측해서 근사하지 않고 fail-closed
- benchmark 장면은 원본을 무리하게 drop-in 실행하기보다, 변환·검증된 지원 subset으로 가져옴
- Isaac Sim은 renderer·sensor·asset/scene shell이며 authoritative simulation state는 `omniphysics`가 소유

## 스물네 번째 질문 — 최종 산출물의 중심은 무엇인가

현재 `SSOT.md`는 프로젝트를 **“VLA용 데이터 공장”**으로 정의하지만, 지금까지 구체화된 목표는 데이터셋보다 **예측 가능한 자체 물리엔진**에 더 가깝습니다. 이 위계를 분명히 해야 다시 P5 dataset production이 엔진 자체보다 상위 목표가 되는 일을 막을 수 있습니다.

### A. 독립 물리엔진이 본체

- `omniphysics`는 Isaac Sim 없이도 headless로 실행·검증 가능
- 자체 world/state/step/material/calibration API 제공
- Isaac Sim은 첫 번째이자 가장 중요한 adapter
- 데이터 생성, VLA 평가 환경, replay는 엔진 위의 응용 계층

### B. Isaac Sim용 물리 backend가 본체

- 주 실행 환경은 항상 Isaac Sim/Kit
- standalone 실행은 unit test와 소형 probe 정도만 지원
- 엔진 API도 Isaac Sim lifecycle과 USD scene을 중심으로 설계

### C. 둘을 동등한 필수 산출물로 취급

- standalone authoritative engine
- Isaac Sim 통합 backend
- 두 경로 모두 첫 프로토타입 완료 조건

제 판단은 **A**가 가장 일관적입니다. 물리 상태와 실행을 완전히 통제하려는 목표를 지키면서도, Isaac Sim 종속성 때문에 core 개발·검증이 막히는 것을 방지합니다. 다만 첫 프로토타입 완료 시점에는 Isaac adapter가 실제로 동작해야 하므로, “Isaac 통합을 후순위로 미룬다”는 뜻은 아닙니다.

A/B/C 중 어느 쪽입니까?

---

> 👤 **USER TURN**

ok. A.

---

> 🤖 **GPT TURN**

확정합니다.

> **`omniphysics`의 본체는 standalone authoritative physics engine이다.**
> Isaac Sim integration, VLA evaluation environment, dataset/replay는 그 위에 올라가는 응용·adapter 계층이다.

따라서 첫 프로토타입도 두 경로를 갖습니다.

\[
\text{Standalone headless engine}
\rightarrow
\text{Isaac Sim adapter}
\rightarrow
\text{renderer / sensors / VLA environment}
\]

Core의 정확성·재현성·성능 검증은 Isaac Sim 없이 수행할 수 있어야 하지만, 실제 프로토타입 완료 조건에는 Isaac Sim 장면에서의 폐루프 실행도 포함됩니다.

## 스물다섯 번째 질문 — 하나의 범용 formulation인가, 결합 가능한 전문 solver들인가

초기에 잠시 유보했던 가장 큰 아키텍처 선택을 이제 명확히 해야 합니다.

### A. 통일 formulation을 우선한다

Rigid body, articulation, deformable, cloth, liquid를 가능한 한 하나의 particle·constraint·variational framework로 표현합니다.

장점은 coupling이 자연스러울 가능성이 있다는 점이지만, 서로 다른 물리군에 동일한 이산화와 solver 구조를 강제하면서 정확도·성능·개발 난도가 모두 악화될 수 있습니다.

### B. 전문 solver를 사용하되 공통 엔진 의미론으로 결합한다

각 물리에 적합한 formulation을 허용합니다.

- rigid/articulation: constraint 또는 variational dynamics
- contact/friction: 자체 contact state와 constitutive law
- cloth/deformable: FEM, projective/variational 또는 적합한 reduced model
- bulk liquid: particle/grid/hybrid 계열
- surface film: surface field 또는 reduced interface model

대신 다음은 완전히 공통으로 소유합니다.

- authoritative world state와 timebase
- geometry·material·interface identity
- contact와 coupling event
- conservation/energy transfer accounting
- GPU scheduling과 multi-rate stepping
- calibration/uncertainty parameter 의미
- Isaac Sim publication API

특히 `surface/interface state`가 solver 간 임시 메시지가 아니라 1급 상태가 되어, wet contact처럼 여러 solver가 만나는 현상을 담당합니다.

### C. B로 시작하지만 장기적으로 A로 수렴해야 한다

초기에는 전문 solver를 쓰되, 장기적으로 하나의 통일 formulation으로 교체하는 것을 명시적 목표로 둡니다.

제 판단은 **B**입니다. 당신이 원하는 일반성은 “모든 것을 같은 방정식으로 푼다”가 아니라, **서로 다른 물리가 중요한 인과관계를 빠뜨리지 않고 일관되게 결합되는 것**입니다. 또한 준실시간 조건에서는 각 물리군에 맞는 수치법과 update rate를 선택할 자유가 필요합니다.

**단일 범용 formulation 자체는 비목표로 두고, 공통 authoritative runtime과 interface/coupling semantics를 엔진의 통일 지점으로 삼는 B를 채택해도 됩니까?**

---

> 👤 **USER TURN**

ok. B.

---

> 🤖 **GPT TURN**

확정합니다.

> **`omniphysics`는 하나의 만능 formulation이 아니라, 물리군별 전문 solver를 공통 authoritative runtime 아래 결합하는 엔진이다.**

통일 지점은 방정식이 아니라 다음입니다.

\[
\text{world state}
+\text{timebase}
+\text{geometry/material/interface identity}
+\text{contact/coupling semantics}
+\text{energy·momentum transfer}
+\text{GPU scheduling}
+\text{calibration/uncertainty}
\]

따라서 rigid/contact, cloth/deformable, bulk liquid, surface film은 서로 다른 수치법과 update rate를 사용할 수 있습니다. 다만 wet grasp처럼 solver 경계를 가로지르는 현상은 `surface/interface state`를 통해 명시적으로 연결되어야 합니다. 장기적으로 모든 solver를 하나의 formulation으로 합치는 것도 목표로 두지 않습니다.

## 스물여섯 번째 질문 — 엔진이 책임질 물리적 스케일

“범용성”을 잘못 해석하면 다시 산업용 해석기, 차량 충돌, 건물 규모 유동까지 범위가 팽창할 수 있습니다. 첫 프로토타입과 장기 north-star의 기본 적용 영역을 어디에 둘까요?

### A. 일상 로봇 조작 스케일

주요 범위:

- tabletop·kitchen·household manipulation
- 한 대의 로봇과 주변 작업 물체
- 손가락·gripper부터 가구 일부 정도의 크기
- grasp, insertion, stacking, pouring, wiping, folding
- 저속 또는 중저속 접촉
- 소량의 액체와 의류·수건·포장재 수준의 deformable

고속 충돌, 차량, 구조역학, 대규모 CFD 등은 비목표입니다.

### B. 가정·산업 조작 전반

A에 더해 대형 articulated object, room-scale mobile manipulation, 무거운 산업 부품, 다수 로봇과 복잡한 환경까지 기본 범위에 둡니다.

### C. 일반 목적 고전역학 엔진

크기·속도·물리 regime를 특정하지 않고 광범위한 classical physics를 장기 목표로 유지합니다.

현재 설명한 VLA·LIBERO·LeHome 사용처와 준실시간 요구에는 **A**가 가장 일관적입니다. 첫 north-star를 A로 제한해도 됩니까?

---

> 👤 **USER TURN**

질문의 첫 north-star라는 단어 선택이 좀 모순이 있네. 프로토타입의 목표는 A인데, 장기(프로토타입 완성 이후) north-start는 B라고 생각하면 될 것 같아.

---

> 🤖 **GPT TURN**

맞습니다. 제가 `north-star`와 `prototype scope`를 섞어 썼습니다. 구분은 다음이 정확합니다.

## 두 개의 시간 지평

### 프로토타입 목표

**일상 로봇 조작 스케일**

- tabletop, kitchen, household manipulation
- 단일 로봇과 제한된 작업 공간
- grasp, insertion, stacking, pouring, wiping, folding
- 저속·중저속 접촉
- 소량 액체
- 의류, 수건, 포장재 수준의 cloth/deformable
- 단일 RTX PRO 6000 Blackwell급 GPU에서 폐루프 준실시간 실행

### 장기 north-star

**가정·산업 조작 전반**

- room-scale mobile manipulation
- 대형 articulated object
- 무거운 산업 부품
- 다수 물체와 더 복잡한 접촉망
- 필요하다면 소수 로봇의 상호작용
- 더 넓은 액체량·deformable 규모
- 동일한 material/interface/runtime 의미론을 유지한 확장

다만 차량 충돌, 항공·우주, 건축 구조해석, 대규모 CFD를 포괄하는 일반-purpose classical physics engine까지는 장기 목표가 아닙니다.

따라서 설계 원칙은 다음이어야 합니다.

> **프로토타입은 A의 요구로 평가하되, A에만 맞춘 특별 경로나 데이터 구조를 만들지 않고 B로 확장 가능한 ownership·state·coupling 경계를 유지한다.**

반대로 B에서 언젠가 필요할 수 있다는 이유만으로, 프로토타입에서 쓰이지 않는 대형 장면 분산처리·다중 로봇·room-scale broad phase 등을 미리 구현해서도 안 됩니다.

## 스물일곱 번째 질문 — 정확도와 준실시간성이 충돌할 때

동일한 장면에서 계산량이 순간적으로 증가해 목표 wall-clock을 지키기 어려운 경우, 엔진의 기본 행동은 무엇이어야 합니까?

### A. 물리 충실도를 우선하고 느려진다

접촉 수나 액체 복잡도가 증가하면 실시간보다 느려져도 동일 fidelity를 유지합니다. 정확도 저하는 허용하지 않습니다.

### B. 준실시간성을 우선하고 자동으로 fidelity를 낮춘다

iteration, active region, 공간 해상도, update frequency 등을 줄여 wall-clock을 유지합니다. 다만 결과 정확도가 조건에 따라 달라질 수 있습니다.

### C. 명시적인 실행 profile과 fail-visible degradation

- **Validation profile:** 정확도 우선. 필요하면 실시간보다 느려질 수 있음
- **Interactive profile:** 준실시간 목표. 사전에 허용된 범위 안에서만 adaptive fidelity 사용
- fidelity가 허용 하한 아래로 내려가야 하는 상황이면 조용히 근사하지 않고, `budget exceeded` 또는 `unsupported regime`를 명시
- 어떤 축이 축소되었는지 기록
  예: contact iterations, film resolution, deformable update rate
- benchmark 결과에는 실제 사용된 fidelity와 wall-clock 정보를 함께 남김

현재 목표에는 **C**가 가장 일관적으로 보입니다. 어느 쪽이 맞습니까?

---

> 👤 **USER TURN**

ok. C. 그리고 앞으로 질문 몇개정도 해야 설계가 충분히 명확해질지도 알려줄래? 굳이 재촉해서 결과의 품질을 떨어뜨리고 싶지는 않으니 빨리 끝내달라는 걸로 오해하지는 말고.

---

> 🤖 **GPT TURN**

**C로 확정**하겠습니다.

- `Validation profile`: 정확도 우선, 실시간보다 느려질 수 있음
- `Interactive profile`: 준실시간 우선, 사전 허용된 fidelity 축만 조정
- 허용 하한을 넘는 축소가 필요하면 조용히 근사하지 않고 `budget exceeded` 또는 `unsupported regime`
- 실제 사용된 해상도, 반복 횟수, update rate, wall-clock을 결과에 기록

## 남은 질문 수

현재 기준으로는 **약 7–10개의 핵심 질문**이면 `SSOT.md`와 `CLAUDE.md`를 책임 있게 개정할 정도로 선명해질 것으로 봅니다. 가장 가능성 높은 수는 **8개 전후**입니다.

지금까지는 프로젝트의 **정체성, 물리 범위, 아키텍처 철학, 검증 방식, 자율 운영 방식**을 정했습니다. 개념적 방향은 약 **75–80% 정도** 정리된 상태입니다. 남은 질문은 새로운 비전을 찾는 것보다, 그 비전을 실행 가능한 경계로 닫는 작업에 가깝습니다.

대략 다음 쟁점들이 남아 있습니다.

1. **프로토타입에서 실제로 구현할 대표 장면 세트**
   rigid/contact, liquid/wet, cloth 각각 무엇을 canonical scene과 benchmark-like scene으로 삼을지.

2. **프로토타입에서 명시적으로 제외할 물리**
   thermal, gas, phase change, fracture, granular 등을 어디까지 장기 backlog로 내릴지.

3. **첫 구현의 수치적 출발점**
   rigid/contact, liquid, deformable에 어떤 solver family를 우선 후보로 둘지. 이것은 상세 알고리즘을 확정하려는 질문이 아니라 탐색 범위를 제한하기 위한 것입니다.

4. **재현성·결정론의 요구 수준**
   bitwise determinism이 필요한지, 통계적·허용오차 기반 replay면 충분한지.

5. **외부 구현 재사용 경계**
   Warp 기반 기존 kernel이나 알고리즘을 어느 수준까지 채택할 수 있는지, 그리고 독립적인 물리 의미론 소유를 어떻게 판정할지.

6. **실패와 지원 범위의 표현 방식**
   엔진이 불확실하거나 지원하지 않는 regime에서 중단할지, 결과와 warning을 함께 낼지.

7. **현재 저장소에서 무엇을 보존하고 무엇을 폐기할지**
   contracts, dataset, bridge, validation, contact 연구 산출물, task registry 중 어떤 것은 자산이고 어떤 것은 새 방향을 방해하는 부채인지.

8. **개정 이후 첫 capability sequence**
   새 `SSOT.md`와 `CLAUDE.md`가 적용된 뒤 orchestrator가 처음 실행할 구체적 순서.

질문 과정에서 새로운 모순이 발견되면 2–3개가 추가될 수 있습니다. 반대로 몇 가지가 한 답에서 함께 닫히면 더 적게 끝날 수도 있습니다. **질문 수를 목표로 삼지 않고, 문서를 작성했을 때 에이전트가 중대한 제품 결정을 임의로 추론할 필요가 없어지는 시점**을 종료 기준으로 삼겠습니다.

## 스물여덟 번째 질문 — 프로토타입에서 제외할 물리

현재 구 설계에는 heat, gas/vapor, phase change, wetting, liquid, contact 등이 초기 wave에 함께 들어가 있습니다. 새 프로토타입에서 범위를 닫으려면 다음 중 어느 쪽이 맞습니까?

### A. 명시적으로 세 축만 포함

프로토타입의 물리 범위:

- rigid body / articulation / collision / contact / friction
- compliant contact와 제한된 deformable/cloth
- bulk liquid / surface film / wet interaction

다음은 프로토타입 비목표로 내립니다.

- heat transfer
- evaporation·condensation
- boiling·freezing 등 phase change
- gas dynamics와 vapor transport
- fracture
- granular·porous
- acoustics, electromagnetics

단, 장기 north-star에서 필요에 따라 추가할 수 있도록 runtime의 ownership/coupling 경계만 막지 않습니다.

### B. 열·증발까지 포함

젖음과 건조를 제대로 다루기 위해 최소한 heat transfer와 evaporation은 첫 프로토타입 범위에 둡니다.

### C. 현상별로 최소 stub을 모두 유지

초기 구상에 있던 heat, gas, phase change 등의 실행 경로를 저충실도로라도 프로토타입에 남깁니다.

제 판단은 **A**입니다. 젖은 접촉에서 첫 프로토타입이 다룰 `drying`은 필요하다면 경험적 surface-film loss law로 제한하고, 열·증발의 완전한 연성 문제로 확장하지 않는 편이 맞습니다. 어느 쪽입니까?

---

> 👤 **USER TURN**

ok. A. 다만 구조 설계 시 추후 확장은 염두에 뒀으면 좋겠음. C처럼 stub을 남기란 말은 아니고, 엔진 자체가 확장성이 좋은 구조가 되도록.

---

> 🤖 **GPT TURN**

확정합니다.

## 프로토타입 물리 범위

포함:

- rigid body / articulation / collision / contact / friction
- compliant contact
- 제한된 deformable / cloth
- bulk liquid / surface film / wet interaction

프로토타입 비목표:

- heat transfer
- gas / vapor transport
- evaporation·condensation
- boiling·freezing 등의 phase change
- fracture
- granular / porous
- acoustics
- electromagnetics

다만 확장성은 **미래 물리의 빈 스텁을 미리 만드는 방식**이 아니라 다음 경계를 안정적으로 설계하는 방식으로 확보합니다.

\[
\text{solver registration}
+\text{owned state}
+\text{field/event exchange}
+\text{coupling schedule}
+\text{material/interface identity}
+\text{conservation accounting}
\]

새 물리군은 이 경계를 통해 추가하되, 현재 실제 소비자가 요구하지 않는 공통 schema나 speculative abstraction은 만들지 않습니다. 즉 **확장 가능하되 미리 구현하지 않는다**가 원칙입니다.

## 스물아홉 번째 질문 — 결정론과 replay 수준

GPU contact·fluid·cloth 계산에서는 병렬 reduction, contact ordering, atomic 연산 때문에 완전한 bitwise determinism이 성능과 크게 충돌할 수 있습니다. 동시에 calibration, hold-out 비교, 자율 디버깅을 위해 재현성은 필요합니다.

어느 수준을 요구해야 합니까?

### A. Bitwise determinism

동일 scene, seed, build, GPU에서 모든 state가 bit-identical해야 합니다. 가능하면 다른 GPU에서도 동일해야 합니다.

장점은 디버깅이 쉽지만, GPU 알고리즘 선택과 성능을 과도하게 제한할 수 있습니다.

### B. 물리적·통계적 재현성

실행마다 부동소수점 수준의 차이는 허용합니다. 대신 trajectory, slip onset, force curve, 잔류량 같은 관측량이 정해진 tolerance 안에서 재현되면 됩니다.

### C. 계층형 결정론

- **Debug profile:** 동일 build·GPU에서는 가능한 한 deterministic ordering과 reproducible replay를 사용
- **Interactive profile:** 성능을 위해 일부 비결정적 GPU primitive 허용
- 모든 실행은 seed, build, solver config, hardware, initial state, action stream을 기록
- 승격·회귀 판정은 raw state bit equality가 아니라 물리 관측량과 invariant tolerance를 사용
- 동일 build·GPU에서도 허용오차를 벗어나는 분산은 버그
- 서로 다른 GPU·driver 간 bitwise equality는 비목표

제 판단은 **C**가 정확성과 준실시간성을 함께 만족시키는 수준입니다. 어느 쪽이 맞습니까?

---

> 👤 **USER TURN**

ok. C.

---

> 🤖 **GPT TURN**

확정합니다.

- **Debug profile:** 동일 build·GPU에서 가능한 한 재현 가능한 실행 순서 유지
- **Interactive profile:** 준실시간성을 위해 일부 비결정적 GPU primitive 허용
- **모든 실행:** seed, commit/build, solver config, hardware, initial state, action stream 기록
- **평가·회귀:** raw state의 bitwise equality가 아니라 물리 관측량·불변량의 tolerance로 판정
- **동일 build·GPU에서 허용범위를 넘는 실행 간 분산:** 버그
- **GPU·driver 간 bitwise 동일성:** 비목표

## 서른 번째 질문 — 프로토타입 장면 세트

프로토타입 범위가 다시 확산되지 않도록, 완료 판정에 사용할 장면을 제한된 세트로 고정할 필요가 있습니다. 다음 구성이 적절해 보입니다.

### Canonical physics scenes

1. **Dry/wet slip calibration**
   - 경사면 또는 수평 인장 방식
   - 건식·습식 조건
   - slip onset, slip time/거리 측정
   - material-pair 및 instance overlay의 기본 보정에 사용

2. **Compliant squeeze-and-lift**
   - 고무 gripper pad로 rigid object를 집어 들어 올림
   - grip force, 접근 속도, 접촉 위치, 표면 형상, wetness 변화
   - contact pressure, pad deformation, stick/slip, lift 성공 여부 기록

3. **Contact-rich insertion**
   - peg insertion 또는 tight-fit assembly
   - 관통, 접촉 안정성, 정지·운동 마찰 전이, force–displacement 관계 검증
   - liquid는 사용하지 않아 rigid/contact core 자체를 독립 검증

4. **Pour → film → wet interaction**
   - bulk liquid를 고체 표면에 붓고 surface film이 형성·이동
   - 이후 grasp 또는 wiping 접촉으로 배수·마찰 변화를 유발
   - `bulk liquid → film → contact → rigid motion` 전체 인과사슬 검증

5. **Cloth structural consumer**
   - 천을 집어 들고 장애물 위로 끌거나 좁은 형상 사이로 통과
   - cloth–rigid, cloth–gripper, self-contact
   - 첫 버전은 정량 정확도보다 관통·끼임·폭발 없이 공통 runtime을 소비하는지 확인

### Benchmark-like transfer scenes

- **Rigid/contact:** LIBERO 계열의 grasp, placement, insertion 또는 articulated-object task 1–2개
- **Liquid/wet:** kitchen-like pouring·wet-object manipulation 또는 wiping task 1개
- **Cloth:** LeHome 계열의 grasp·drag·fold task 1개

Canonical scene에서 얻은 material/interface parameter는 benchmark-like scene으로 옮길 때 다시 맞추지 않습니다. Asset 변환에 필요한 질량·관성·형상·instance overlay 등록은 허용하지만, task 결과에 맞춘 parameter tuning은 금지합니다.

이 **5개 canonical + 물리군별 benchmark-like transfer** 구성을 프로토타입의 고정 평가 세트로 삼는 것이 적절합니까? 너무 많다고 판단되면 어떤 항목을 제거해야 하는지도 함께 말해 주세요.

---

> 👤 **USER TURN**

ok.

---

> 🤖 **GPT TURN**

좋습니다. 프로토타입의 고정 평가 범위는 다음으로 확정합니다.

- **Canonical 5종:** dry/wet slip, compliant squeeze-and-lift, contact-rich insertion, pour→film→wet interaction, cloth structural consumer
- **Transfer scenes:** rigid/contact, liquid/wet, cloth 각각 benchmark-like 장면
- Canonical에서 식별한 material/interface parameter는 transfer scene에서 재튜닝하지 않음
- 질량·관성·실제 형상·instance overlay 등록은 허용하되 task 결과에 맞춘 tuning은 금지

현재 설계 명확도는 약 **85–90%**입니다. 남은 핵심 질문은 대략 **5–7개**로 봅니다. 주로 수치법 선택 권한, 실패 의미론, 기존 저장소 자산 정리, 첫 실행 순서를 닫는 단계입니다.

## 서른한 번째 질문 — 수치 formulation을 누가, 어떻게 선택할 것인가

`SSOT.md`에 특정 알고리즘을 미리 박으면 다시 잘못된 연구 경로에 장기간 종속될 수 있습니다. 반대로 완전히 열어두면 자율 에이전트가 끝없는 solver 탐색을 벌일 수 있습니다.

### A. 지금 특정 solver family를 고정

예를 들어 rigid/contact는 특정 variational·constraint formulation, liquid는 특정 SPH/MPM 계열, cloth는 특정 FEM/VBD 계열로 미리 결정합니다.

장점은 구현 방향이 명확하지만, 실제 canonical scene에서 부적합해도 문서 권위 때문에 계속 끌고 갈 위험이 있습니다.

### B. **제한된 후보 경쟁으로 선택**

각 핵심 subsystem에 대해:

- 가장 단순한 executable baseline 1개
- 구조적으로 다른 challenger 최대 2개
- 후보 family당 큰 수정 최대 2세대
- 동일 canonical scene, 정확도, 안정성, calibration 식별성, GPU 성능으로 비교
- 승자는 Evaluation lane을 거쳐 mainline으로 promotion
- 모든 후보가 실패하면 자동으로 더 큰 architecture fork를 만들지 않고 인간 승인 경계로 올림

`SSOT.md`는 특정 solver 이름보다 **요구되는 물리적 의미와 평가 기준**을 고정하고, `CLAUDE.md`가 이 제한된 탐색 절차를 강제합니다.

### C. 에이전트가 자유롭게 최선의 formulation을 찾음

후보 수와 연구 세대를 제한하지 않고, 충분한 성능이 나올 때까지 계속 탐색합니다.

제 추천은 **B**입니다. 구체적인 첫 후보군은 구현 착수 시 짧은 기술 정찰로 정하되, 정찰 자체도 후보 수와 산출물을 제한해야 합니다.

이 방식을 채택해도 됩니까?

---

> 👤 **USER TURN**

ok.

---

> 🤖 **GPT TURN**

확정합니다.

각 핵심 subsystem은 **제한된 후보 경쟁**으로 선택합니다.

- executable baseline 1개
- 구조적으로 다른 challenger 최대 2개
- 후보 family당 major revision 최대 2세대
- 동일 장면·동일 calibration budget·유사 연산 예산으로 비교
- 정확도, 안정성, 식별 가능성, GPU 성능을 함께 평가
- 승격 실패 시 자동으로 더 큰 research fork를 만들지 않음
- 전 후보 실패 시 인간 승인 경계로 올림

`SSOT.md`는 solver 이름이 아니라 **보존해야 할 물리 의미와 성공 기준**을 고정하고, 구체 formulation은 교체 가능한 결정으로 둡니다.

현재 남은 핵심 질문은 약 **4–6개**입니다.

## 서른두 번째 질문 — 지원 범위를 벗어났을 때의 동작

엔진이 calibration 범위 밖이나 아직 검증되지 않은 조건에 진입했을 때 어떻게 행동해야 합니까?

예를 들면 다음과 같습니다.

- 보정한 것보다 훨씬 큰 grip force
- surface film이 검증 범위보다 지나치게 두꺼움
- 예상보다 많은 동시 contact
- cloth 변형률이 지원 범위를 초과
- adaptive fidelity가 허용 하한까지 내려감
- solver residual이나 에너지 오차가 급증

### A. 항상 fail-fast

지원 범위를 벗어나면 즉시 simulation을 중단합니다.

정직하지만 interactive VLA evaluation에서 사소한 외삽에도 episode가 계속 종료될 수 있습니다.

### B. 경고만 남기고 계속 실행

가능한 근사값으로 계속 진행하고 warning을 기록합니다.

사용하기는 편하지만, 물리적으로 무효인 결과가 정상 데이터처럼 소비될 위험이 큽니다.

### C. **등급화된 fail-visible semantics**

상태를 명시적으로 구분합니다.

- `SUPPORTED`: 검증된 envelope 내부
- `EXTRAPOLATING`: 검증 범위 밖이지만 수치적으로 안정적이며 제한적으로 계속 실행
- `DEGRADED`: 성능 예산 때문에 fidelity가 낮아졌거나 일부 모델이 fallback으로 전환됨
- `INVALID`: 물리적 claim을 할 수 없는 상태
- `FAILED`: non-finite, solver divergence, 심각한 penetration, state ownership 충돌처럼 실행을 계속할 수 없음

실행 정책은 다음처럼 나눕니다.

- **Interactive profile:** `EXTRAPOLATING`과 허용된 `DEGRADED`에서는 계속 실행하되 frame/episode metadata에 전파
- **Validation profile:** `SUPPORTED`가 아니면 공식 평가 결과를 무효 처리
- `INVALID`는 센서 출력까지 계속 생성할 수는 있어도 학습·평가용 유효 episode로 출하하지 않음
- `FAILED`는 즉시 중단하고 재현 가능한 failure artifact 생성
- 상태가 회복되더라도 해당 구간의 validity flag는 지우지 않음

제 추천은 **C**입니다. 이 의미론을 채택해도 됩니까?

---

> 👤 **USER TURN**

ok. C.

---

> 🤖 **GPT TURN**

확정합니다.

지원 범위 밖의 실행은 `SUPPORTED / EXTRAPOLATING / DEGRADED / INVALID / FAILED`로 명시하고, profile별 처리 정책을 분리합니다. 특히 `INVALID` 구간을 정상 학습·평가 데이터처럼 출하하지 않고, `FAILED`에서는 재현 artifact를 남기도록 합니다.

현재 남은 핵심 질문은 **약 3–5개**입니다.

## 서른세 번째 질문 — 현재 저장소를 얼마나 과감하게 재편할 것인가

새 방향은 현재 프로젝트의 우선순위와 운영 방식만 조금 수정하는 수준이 아닙니다. 물리엔진의 정체성, 검증 단계, Isaac Sim의 역할, dataset의 위계까지 바뀌므로 기존 구조를 어떻게 다룰지 정해야 합니다.

### A. 현재 구조를 점진적으로 개조

기존 contracts, task graph, ADR, milestone, solver 구조를 최대한 유지하면서 새 목표에 맞게 하나씩 수정합니다.

장점은 기존 작업을 보존한다는 것이지만, 오래된 의존성과 gate가 새 mainline을 계속 구속할 가능성이 큽니다.

### B. 사실상 clean slate

현재 구현은 참고 자료로만 남기고, 새로운 package·task registry·문서 체계로 처음부터 다시 시작합니다.

구조는 깨끗해지지만, 이미 유효한 GPU kernel, bridge, dataset/replay, 계측 코드까지 다시 만들 위험이 있습니다.

### C. **통제된 구조 리셋**

- 현재 상태를 `legacy/research-v1` 성격의 기준점으로 tag 또는 archive
- 새 `SSOT.md`와 `CLAUDE.md`를 즉시 최상위 규칙으로 적용
- 새 authoritative runtime과 capability graph를 중심으로 mainline을 재구성
- 기존 API, contracts, task dependency, ADR 결론에 대한 하위 호환 의무는 두지 않음
- 기존 코드는 자동 승계하지 않고 다음 세 분류로 판정
  - `reuse`: 새 구조에 그대로 조화되는 저수준 자산
  - `adapt/rewrite`: 아이디어나 kernel은 유효하지만 ownership·API를 바꿔야 하는 자산
  - `archive`: 과거 연구 증거로만 보존하고 새 mainline을 막지 않는 자산
- 기존 gate와 governance test는 새 capability의 실제 실패를 잡는 경우에만 유지
- 기존 dataset, bridge, Warp kernel도 “이미 만들었으니 유지”가 아니라 새 executable consumer가 필요로 할 때만 편입
- 구 ADR은 역사적 provenance이지 새 엔진의 자동 선행조건이 아님

제 판단은 **C**입니다. 기존의 유효한 기술 자산은 채굴하되, 기존 방법론과 의존성 그래프가 새 프로젝트의 운영체제로 남지 않게 하는 방식입니다.

현재 저장소에 대해 **C의 통제된 구조 리셋**을 채택해도 됩니까?

---

> 👤 **USER TURN**

괜히 migration 과정에서 복잡성만 추가될 것 같음. B로 가되 재활용 가치가 있는 자산은 clean slate 위에 참고해서 이식하는 방식으로. 기존 task, adr 등은 archiving은 해두되 혼선을 막기위해 전부 무시하고, research 자료나 유효한 kernel 등 실질 가치가 있는 자산들만 참고자료로 활용.

---

> 🤖 **GPT TURN**

좋습니다. 그러면 **점진적 개조가 아니라 실제 clean slate**로 확정하는 편이 맞습니다.

## 확정된 리셋 원칙

새 mainline은 기존 구조에 대한 호환 의무를 지지 않습니다.

- 기존 `tasks.yaml`, `ROADMAP.md`, `PROGRESS.md`
- 기존 ADR의 결정·dependency
- 기존 gate와 review packet
- 기존 contract/API
- 기존 phase·milestone 체계
- 기존 SSOT/DESIGN의 실행 계획

위 항목들은 **새 프로젝트의 입력이나 선행조건으로 사용하지 않습니다.**

기존 저장소에서 가져올 수 있는 것은 실질 기술 자산뿐입니다.

- 검증된 Warp/CUDA kernel
- collision·solver·bridge 구현에서 확인된 유효한 기법
- 재현 가능한 probe와 측정 도구
- dataset/replay 코드 중 새 구조에 직접 필요한 부분
- 실패 메커니즘을 입증한 research evidence
- 외부 source 분석과 수치 formulation 조사

다만 이식할 때는 기존 모듈을 통째로 끌고 오기보다 다음 절차가 적절합니다.

\[
\text{legacy asset inspection}
\rightarrow
\text{새 ownership/API에 맞춘 재작성 또는 최소 이식}
\rightarrow
\text{새 테스트와 benchmark로 독립 검증}
\]

“기존에 존재한다”는 사실은 채택 근거가 아니며, 새 capability가 실제로 필요로 하고 새 구조와 조화될 때만 이식합니다.

또한 혼선을 막으려면 기존 자료를 새 default branch의 `archive/` 디렉터리에 대량으로 남기는 방식은 피하는 것이 좋습니다. 에이전트 검색과 코드 탐색에 계속 노출되기 때문입니다. 기존 상태는 immutable tag 또는 별도 frozen branch로 보존하고, 새 mainline에는 참조 방법만 짧게 기록하는 것이 더 안전합니다.

현재 남은 핵심 질문은 **약 2–4개**입니다.

## 서른네 번째 질문 — 새 프로젝트의 작업·문서 운영은 얼마나 가볍게 할 것인가

Clean slate 이후에도 자율 에이전트가 작업을 추적할 최소 운영 체계는 필요합니다. 어느 수준이 적절합니까?

### A. 파일 기반 최소 운영

새 mainline에는 다음만 둡니다.

- `SSOT.md`: 목표, 범위, 성공 기준
- `CLAUDE.md`: 에이전트 운영 규칙
- `CAPABILITIES.md`: capability graph와 각 노드의 상태
- `CHECKPOINT.md`: 마지막 실행 가능 상태와 다음 가설
- 소수의 subsystem별 기술 문서

모든 micro-task의 사전 등록은 하지 않습니다. ADR도 인간 승인 경계나 promotion된 중대한 결정에만 작성합니다.

### B. 간소화된 task registry 유지

가벼운 `tasks.yaml` 또는 issue tracker를 유지하되:

- executable milestone과 blocker만 등록
- exploration micro-task는 미등록
- 자동 dependency graph와 round/review packet은 제거
- task 완료 수를 진행률로 사용하지 않음

### C. 기존과 유사한 정형 registry를 새로 설계

모든 작업을 다시 사전 등록하지만, schema와 gate 규칙만 단순화합니다.

제 추천은 **A를 기본으로 하고, 실제 병렬 작업 충돌이 생길 때만 B 수준의 얇은 registry를 추가하는 것**입니다. 자율 orchestration에 필요한 것은 작업 목록의 완전성보다 `현재 executable capability`, `활성 후보`, `남은 연구 예산`, `정확한 재실행 명령`입니다.

새 clean slate의 초기 운영 체계는 A/B/C 중 어디가 맞습니까?

---

> 👤 **USER TURN**

ok. 권고대로. 다만 에이전트 운영규칙은 orchestrator용 CLAUDE.md 와 worker용 AGENTS.md 로 분리.

---

> 🤖 **GPT TURN**

확정합니다. 새 mainline의 운영 문서는 다음처럼 분리합니다.

## 문서 역할

### `SSOT.md` — 제품·물리 목표의 권위

담을 내용:

- 프로젝트 정체성
- prototype scope와 장기 north-star
- 물리적 성공 기준
- 포함·비포함 범위
- standalone engine과 Isaac Sim의 관계
- 정확도·성능·calibration 요구
- canonical/transfer scene 구조
- 인간만 변경할 수 있는 제품 수준 결정

에이전트 작업 절차나 세부 구현법은 넣지 않습니다.

### `CLAUDE.md` — orchestrator 운영 규칙

Claude Fable 5가 담당할 내용:

- capability graph 관리
- 활성 executable milestone 선정
- exploration/evaluation/promotion lane 전환
- worker fan-out과 scope 분배
- candidate/research budget 집행
- WIP 제한
- hold-out 격리
- 인간 승인 경계 판별
- legacy 자산 채굴 여부 결정
- promotion과 checkpoint 작성
- drift 감지 및 중단
- `SSOT.md`와 공식 평가 기준을 worker가 임의 변경하지 못하도록 통제

### `AGENTS.md` — Codex worker 실행 규칙

각 worker가 따라야 할 내용:

- 할당받은 candidate와 파일 scope만 작업
- 구현·실험·측정 결과를 우선
- exploration 중 빠른 반복 허용
- SSOT, 공식 metric, hold-out, 대규모 architecture를 임의 변경하지 않음
- 새로운 research fork를 자율 생성하지 않음
- 실패를 숨기는 fallback이나 threshold 조정 금지
- candidate record와 정확한 재현 명령 제출
- Warp/CUDA 우선 구현 및 성능 측정
- unsupported/degraded 상태를 명시적으로 전파
- promotion 권한은 갖지 않고 증거만 orchestrator에 반환

초기 운영 파일은 다음 정도면 충분합니다.

```text
SSOT.md
CLAUDE.md
AGENTS.md
CAPABILITIES.md
CHECKPOINT.md
```

별도 task registry는 실제 병렬 충돌이나 추적 문제가 관찰되기 전에는 만들지 않습니다.

## 서른다섯 번째 질문 — 다중 worker의 코드 통합 방식

거의 완전 자율로 여러 Codex worker가 동시에 구현한다면, write ownership과 integration 방식을 정해야 합니다.

### A. 하나의 working tree를 공유

각 worker가 지정된 파일 범위만 수정합니다. 빠르지만 충돌, 오염, 부분 구현 노출, 다른 candidate 간 상태 혼합 위험이 큽니다.

### B. 모든 worker가 독립 branch/worktree 사용

orchestrator가 결과를 검토해 cherry-pick 또는 merge합니다. 격리는 강하지만, 작은 조사·측정 작업까지 모두 branch lifecycle을 요구해 다소 무거울 수 있습니다.

### C. 계층형 격리

- 코드 candidate와 architecture 변경은 반드시 독립 worktree/branch
- read-only 조사, benchmark 분석, 기존 자산 검토는 branch 없이 수행 가능
- 동일 subsystem의 경쟁 candidate는 서로의 구현을 보지 않는 독립 worktree에서 작성
- orchestrator만 mainline에 통합
- evaluation candidate는 immutable commit으로 고정
- worker는 main에 직접 push하거나 promotion하지 못함
- 사소한 통합 보정도 orchestrator가 수행하거나 별도 짧은 integration worker에 위임

제 권고는 **C**입니다. 이 통합 방식을 채택해도 됩니까?

---

> 👤 **USER TURN**

ok. C.

---

> 🤖 **GPT TURN**

확정합니다.

- 코드 candidate·architecture 변경: 독립 branch/worktree
- 조사·문헌·legacy asset inspection: read-only로 수행 가능
- 경쟁 candidate: 상호 격리
- evaluation 대상: immutable commit으로 고정
- mainline 통합·promotion 권한: orchestrator만 보유
- worker의 main 직접 push·승격 금지

현재 남은 핵심 결정은 **약 2개**입니다.

## 서른여섯 번째 질문 — 수치 acceptance threshold를 언제 고정할 것인가

현재까지 성공 판정 구조는 정했습니다.

\[
\text{절대 품질 하한}
\land
\text{기존 엔진 대비 개선}
\land
\text{준실시간 성능 하한}
\]

그러나 `slip onset force 오차 ≤ 몇 %`, `penetration ≤ 몇 mm`, `실시간 배율 ≥ 얼마` 같은 구체 숫자를 지금 SSOT에 박으면, 장비 한계와 실제 noise floor를 모르는 상태에서 또 다른 잘못된 법전이 될 수 있습니다. 반대로 숫자를 전혀 고정하지 않으면 에이전트가 결과를 본 뒤 기준을 유리하게 정할 수 있습니다.

### A. 지금 모든 수치 기준을 고정

문서를 작성할 때 각 canonical scene의 절대 오차·상대 개선·성능 기준까지 결정합니다.

### B. 후보 구현 후 결과를 보고 기준 결정

실제 candidate와 baseline 결과를 확인한 뒤 합리적인 threshold를 정합니다.

개발은 편하지만 결과에 맞춘 기준 설정이 될 위험이 큽니다.

### C. **측정 체계와 기준 설정 절차를 먼저 고정하고, 수치는 baseline 측정 뒤 evaluation 전에 동결**

권장 절차:

1. canonical scene, 관측량, 측정 방법, 반복 횟수, calibration/hold-out split을 먼저 확정
2. 실제 장비의 반복 실험으로 measurement noise와 run-to-run variance 측정
3. PhysX·기본 Newton baseline을 동일 조건에서 측정
4. candidate 결과를 보지 않은 별도 evaluator가 다음을 근거로 수치 threshold 제안
   - 장비의 식별 가능 범위
   - baseline 오차
   - task에 의미 있는 최소 개선폭
   - 준실시간 사용성 하한
5. 인간이 **첫 공식 threshold set만 승인**
6. 이후 해당 evaluation generation 동안 고정
7. threshold를 바꾸면 기존 hold-out은 폐기하거나 새 evaluation generation으로 명시

`SSOT.md`에는 metric의 종류와 판정 구조를 두고, 구체 수치는 versioned evaluation specification에 둡니다. 이렇게 하면 SSOT가 다시 장면별 숫자 저장소가 되는 것도 피할 수 있습니다.

제 판단은 **C**가 맞습니다. 이 방식을 채택합니까?

---

> 👤 **USER TURN**

음. C가 맞는 것 같긴 한데, 아직 robot과 연동해서 통제된 조건으로 자동화된 실험을 한다던가 할만한 프레임워크를 구축해 둔게 없어서 실제 물리적인 수치를 개발 착수 전에 제공해 주긴 어려움. web이나 open source로 fidelity가 굉장히 높은 수준으로 검증된 데이터나 시뮬레이션 case를 가져오던가, 일단 physX, newton baseline을 기준으로 개발 시작하고 나중에 기준을 좀 더 보강할 수는 없을까?

---

> 🤖 **GPT TURN**

가능합니다. 오히려 현재 상황에서는 **실물 계측 프레임워크가 완성될 때까지 엔진 개발을 보류하는 것이 잘못**입니다. 다만 `PhysX/Newton보다 낫다`와 `현실을 정량 예측한다`를 같은 증거 수준으로 취급해서는 안 됩니다.

기존의 C를 다음처럼 **버전이 있는 증거 세대(evidence generation)** 방식으로 수정하는 것이 적절합니다.

## 권장 구조: 개발 착수와 현실 검증을 분리

### `G0 — Numerical bootstrap`

지금 즉시 시작할 수 있는 단계입니다.

사용하는 증거:

- 해석해 또는 manufactured solution
- 운동량·에너지·질량 등 invariant
- resolution/convergence test
- 고충실도 reference solver와의 제한적 비교
- 동일 장면의 PhysX·기본 Newton baseline
- 공개된 실험 benchmark

이 단계에서 고정하는 것:

- canonical scene
- 관측량
- calibration/hold-out 분리
- 실행 예산
- baseline 조건
- 치명적 실패 기준
- 평가 코드

아직 고정하지 않아도 되는 것:

- 연구실 물체에 대한 최종 오차율
- 실제 gripper–cup 계면의 절대 물성
- 최종 `SUPPORTED` envelope

`G0`를 통과한 capability의 상태는 다음처럼 제한합니다.

```text
EXPERIMENTAL
numerically_verified = true
real_world_validated = false
```

즉 mainline 개발에는 사용할 수 있지만, 현실 정량 예측이 검증됐다고 주장하지 않습니다.

---

### `G1 — External benchmark characterization`

공개된 실험 데이터와 재현 가능한 reference implementation을 적극 활용합니다.

예를 들어 유체에는 SPHERIC이 wet-bottom dam-break를 포함한 공개 validation case를 제공하며, 해당 case에는 실험 형상, 수심 조건, 디지털화된 자유표면 profile과 측정 자료가 포함됩니다. 이런 사례는 bulk liquid와 wet-surface 전이의 수치적 기본기를 검증하는 데 유용합니다. citeturn675226search1turn755172search16

접촉·변형에는 IPC reference implementation과 관련 연구의 재현 scene을 **고충실도 수치 reference**로 사용할 수 있습니다. 이는 관통 방지, contact geometry, 마찰 solver의 수치적 거동을 비교하는 데 유용하지만, 현실 재료의 마찰계수 자체를 알려주는 실험 ground truth는 아닙니다. citeturn322312search8turn755172academia48turn755172academia50

Cloth에는 실제 depth·point-cloud를 포함하고 동적·준정적 cloth manipulation을 비교하는 공개 sim-to-real benchmark가 있습니다. 세 종류의 cloth와 rigid table contact를 사용하며, 기존 simulator들의 정확도·안정성·계산비용을 비교할 수 있도록 데이터와 metric이 공개되어 있습니다. 이 자료는 자체 실험 장치를 만들기 전 cloth structural consumer의 외부 anchor로 적합합니다. citeturn675226search0turn675226search2turn675226search3

이 단계까지 통과한 상태는 다음 정도가 적절합니다.

```text
CHARACTERIZED
external_benchmarked = true
lab_specific_validated = false
```

---

### `G2 — Lab-light validation`

이 단계는 나중에 구축하되, 전문적인 tribometer나 촉각 센서를 전제로 하지 않습니다.

사용 가능한 실험은 앞서 정한 보정 예산 안에서 구성합니다.

- 경사면 dry/wet slip
- 일정 grip command에서 squeeze-and-lift
- grip command 또는 접근 속도 sweep
- RealSense 기반 pose·slip trajectory 추정
- lift success/failure
- 알려진 양의 물을 적용한 뒤 slip onset 변화
- 간단한 insertion trajectory와 관통·정렬 오차 측정

중요한 것은 힘을 매우 정밀하게 측정하는 것이 아니라, **관측 가능한 값과 그 불확실성을 정직하게 모델링하는 것**입니다. 절대 힘을 신뢰할 수 없으면 `commanded grip level`, `estimated effort`, `binary slip onset`, `pose trajectory`를 관측량으로 사용하고, 그 증거가 허용하는 claim만 냅니다.

자동화된 실험 프레임워크는 `G2`의 편의성과 반복성을 높이지만, 첫 `G2` 실험의 선행조건일 필요는 없습니다. 초기에는 수동 셋업과 반자동 기록으로 시작하고, 실험 protocol이 안정된 뒤 자동화하면 됩니다.

---

### `G3 — Automated calibration`

이것은 프로토타입 착수 조건도, 초기 물리 solver 개발의 blocker도 아닙니다.

목적은 다음입니다.

- 객체 등록 3–5회 자동 실험
- 중요한 객체의 10–30회 sweep
- active calibration
- 반복실험의 일관성 관리
- instance overlay 자동 추정

즉 `G3`는 **엔진 정확도의 근거라기보다 calibration UX와 확장성을 위한 계층**입니다.

## 공개 데이터의 정확한 역할

공개 자료를 사용할 때는 세 가지를 구분해야 합니다.

### 1. 실험 benchmark

실제 물리와의 오차를 평가할 수 있습니다. 다만 형상·재료·경계조건이 충분히 정의되어 있어야 합니다.

### 2. 고충실도 reference simulation

자체 구현의 수치 오류나 contact/coupling 차이를 찾는 데 유용합니다. 그러나 reference solver와 일치한다고 해서 현실과 일치하는 것은 아닙니다.

### 3. 문헌의 물성·현상 데이터

parameter scale, 속도·하중에 대한 추세, plausible regime를 정하는 데 유용합니다. 특정 연구실 물체의 직접 calibration 값으로 가져오면 안 됩니다.

특히 wet rubber–glass friction 실험에서는 표면의 친수성·소수성, 거칠기, 액막 배출과 dewetting 거동에 따라 접착과 마찰 양상이 크게 달라지는 것이 관찰됩니다. 따라서 공개된 wet-friction 곡선은 모델이 재현해야 할 **메커니즘과 규모의 sanity check**로는 유효하지만, 당신의 gripper pad와 유리컵에 그대로 적용할 oracle은 아닙니다. citeturn322312search2turn322312academia48

## Threshold를 늦게 정하면서도 Goodhart를 막는 방법

기준 숫자를 지금 전부 정할 필요는 없습니다. 대신 다음 순서를 강제합니다.

1. **Candidate를 보기 전에** scene, metric, 측정법, split을 고정합니다.
2. 공개 실험 자료와 PhysX/Newton baseline을 먼저 측정합니다.
3. Candidate 결과를 보지 않은 evaluator가 provisional threshold를 제안합니다.
4. 이를 `EVAL_SPEC_G0_v1`처럼 버전 관리합니다.
5. 해당 generation 동안 threshold는 변경하지 않습니다.
6. Candidate를 수정하면 새로운 candidate version으로 다시 평가합니다.
7. 이미 본 hold-out은 이후 calibration 자료로 강등하고, 다음 공식 평가는 새 split을 사용합니다.
8. 자체 실험이 생기면 `G2` 평가 사양을 새로 만듭니다.
9. `G0` 통과를 소급해 `G2` 통과로 바꾸지 않습니다.
10. 나중의 실물 증거가 기존 모델을 반박하면 capability를 실제로 demote하거나 교체합니다.

따라서 기준 강화는 허용하되, 다음과 같이 claim을 버전별로 분리합니다.

```text
G0: PhysX/Newton 대비 개선 + 수치적 건전성
G1: 공개 실험/reference benchmark와의 정합
G2: 연구실 물체·장비에서 hold-out 예측
```

## 결론

현재는 다음 작업부터 착수할 수 있습니다.

1. standalone authoritative runtime
2. rigid/contact executable baseline
3. PhysX/Newton matched-budget baseline harness
4. 공개 contact·fluid·cloth benchmark importer
5. evaluation specification과 candidate 격리
6. liquid → film → wet contact 수직 경로
7. 이후 간단한 lab-light protocol
8. 마지막으로 robot calibration 자동화

즉 **실물 자동화 프레임워크 부재는 개발 blocker가 아닙니다.** 다만 실물 검증 전에는 capability의 claim을 `EXPERIMENTAL` 또는 `CHARACTERIZED`로 제한해야 합니다.

## 서른일곱 번째 질문 — 프로토타입 완료의 최종 경계

제 권고는 다음입니다.

> 엔진 개발과 내부 capability promotion은 `G0/G1`만으로 진행할 수 있다.
> 그러나 **첫 프로토타입이 완성되었다는 최종 선언에는**, 자동화되지 않아도 좋으므로 rigid/contact와 liquid/wet 두 주력 축에 대해 최소한의 `G2` 실험이 필요하다.

즉 전문 계측이나 자동 robot sweep은 필요 없지만, 실제 컵·고무·물체를 사용한 소규모 hold-out에서 최소한 slip onset, trajectory, lift outcome 같은 결과를 한 번은 비교해야 합니다.

이 경계를 채택합니까?

---

> 👤 **USER TURN**

ok. 권고대로. 다음 turn에는 남은 질문 있으면 전부 한번에 물어봐줘.

---

> 🤖 **GPT TURN**

이제 남은 것은 비전보다 **구현·운영의 경계조건 6개**입니다. 아래 답이 정해지면 목표가 충분히 선명해졌다고 보고, 다음 단계에서 새 `SSOT.md`, orchestrator용 `CLAUDE.md`, worker용 `AGENTS.md`, 그리고 최소 운영 문서 초안을 작성할 수 있습니다.

각 항목에는 권고안을 표시했습니다. `권고안 전부 채택, 4번만 수정`처럼 답해도 됩니다.

---

## 1. 외부 소스 코드 재사용과 라이선스 경계

기존 Warp/CUDA 구현을 활용할 때 어느 정책을 적용할까요?

### A. 알고리즘만 참고하고 코드는 전부 새로 작성

가장 독립적이지만, 검증된 BVH·collision·linear algebra kernel까지 다시 구현하게 될 수 있습니다.

### B. 라이선스가 허용하는 코드는 폭넓게 직접 편입

개발은 빠르지만 외부 엔진의 lifecycle·상태 구조·물리 의미론이 core에 스며들 위험이 있습니다.

### C. **권고안 — 저수준 구현은 제한적으로 재사용, 물리 의미론은 자체 소유**

- MIT/BSD/Apache 등 명확한 permissive license의 구현만 자동 채택 가능
- 라이선스·원본 commit·변경 내역을 기록
- Warp/CUDA primitive, BVH, spatial query, reduction, sparse solver 등은 재사용 가능
- 외부 코드가 자체 world state, stepping lifecycle, contact law를 소유하면 통째로 도입하지 않음
- 필요한 kernel·알고리즘만 `omniphysics`의 state와 API에 맞춰 격리·적응
- GPL/AGPL 등 강한 copyleft 의존성을 core에 넣는 것은 인간 승인 필요
- 출처가 불분명하거나 라이선스가 불명확한 코드는 사용 금지
- 기존 Newton·IPC·다른 엔진은 중요한 참고·비교 대상이지만 자동 권위는 아님

**C를 채택할까요?**

---

## 2. 학습된 residual 또는 경험적 보정의 허용 범위

단순한 물리 파라미터만으로 실제 instance 편차를 설명하기 어려울 때, neural residual이나 black-box correction을 어디까지 허용할까요?

### A. 완전 금지

모든 힘과 상태 전이는 명시적인 constitutive law로만 계산합니다.

### B. 제한 없이 허용

hold-out 성능이 좋아지면 neural force correction이나 trajectory residual도 core에 넣을 수 있습니다.

### C. **권고안 — 제한적이고 분리된 residual만 허용**

- authoritative core의 기본 경로는 해석 가능한 물리 모델
- 저차원 instance overlay, monotone correction curve, bounded lookup model 등은 허용
- 보정에 사용된 관측량과 적용 범위를 명시
- residual은 기본 물리 상태를 대체하지 않고 제한된 항만 보정
- 비보존력·에너지 주입 등은 별도 ledger에 노출
- 외삽 시 자동으로 `EXTRAPOLATING` 또는 `INVALID`
- neural residual은 별도 experimental module로만 허용
- neural residual 없이는 통과하지 못하는 capability를 `SUPPORTED`로 승격하려면 인간 승인 필요
- benchmark task의 성공·실패 label을 직접 학습한 correction은 물리 검증 증거로 인정하지 않음

**C를 채택할까요?**

---

## 3. Sensor·rendering realism의 범위

Isaac Sim을 renderer·sensor shell로 사용한다고 했지만, sensor 자체의 물리적 정확도도 프로젝트가 책임질지 정해야 합니다.

### A. 물리엔진과 센서 모델을 함께 고도화

RGB-D noise, material appearance, motion blur, rolling shutter, calibration drift까지 프로토타입 범위에 포함합니다.

### B. **권고안 — 표준 센서를 사용하되 sensor realism은 프로토타입 비목표**

- Isaac Sim/RTX의 RGB, depth, segmentation, camera pose를 사용
- sensor stream과 authoritative physics state의 시간 정렬은 보장
- 표준적인 configurable noise·latency 적용은 가능
- 별도의 photorealism, depth artifact, tactile sensor realism 연구는 프로토타입 비목표
- sensor 모델의 한계가 물리엔진 평가 결과와 혼동되지 않도록 분리 기록
- 장기적으로 별도 plugin/subsystem으로 확장 가능

### C. RGB-D만 고도화

RealSense 특성만 별도 모델링하고 나머지는 표준 Isaac Sim 기능을 사용합니다.

**B를 채택할까요?**

---

## 4. Differentiability의 지위

향후 policy optimization이나 system identification을 위해 differentiable simulation을 요구할까요?

### A. 프로젝트 핵심 요구사항

모든 주요 solver와 coupling이 gradient를 제공해야 합니다.

### B. **권고안 — 비요구사항, 저비용이면 보존**

- prototype과 장기 architecture를 differentiability가 지배하지 않음
- Warp autodiff 등을 낮은 비용으로 유지할 수 있는 subsystem은 보존 가능
- gradient 지원을 위해 정확도·안정성·준실시간성·solver 선택을 희생하지 않음
- calibration은 gradient-free 또는 surrogate 기반이어도 됨
- differentiable path는 나중의 독립 capability로 추가 가능

### C. 영구 비목표

우연히 가능한 경우에도 gradient API를 노출하지 않습니다.

**B를 채택할까요?**

---

## 5. 플랫폼과 CPU fallback의 범위

### A. CPU와 GPU를 동등하게 지원

모든 solver가 CPU에서도 실용적인 성능으로 돌아가야 합니다.

### B. **권고안 — NVIDIA GPU가 production target, CPU는 제한된 참조 경로**

- Linux + NVIDIA CUDA + Warp가 첫 번째 지원 플랫폼
- RTX PRO 6000 Blackwell급 단일 GPU가 prototype 성능 기준점
- CPU는 소형 unit test, scalar reference, 디버깅, analytic comparison에만 사용 가능
- CPU production performance는 비목표
- AMD/Apple GPU, Windows native execution, multi-node 분산은 prototype 비목표
- GPU 전용 최적화 때문에 CPU 구현이 지나치게 비싸면 CPU fallback 없이도 허용
- 다만 GPU kernel 검증을 위한 작은 reference implementation은 가능한 경우 유지

### C. GPU 전용

CPU reference path도 만들지 않고 모든 실행을 CUDA에서만 수행합니다.

**B를 채택할까요?**

---

## 6. Clean slate를 실제 저장소에 적용하는 방식

기존 구현과 문서를 어떻게 보존하고 새 mainline을 어디에 둘지 결정해야 합니다.

### A. 새 저장소를 별도로 생성

기존 `newton-omniphysics`는 동결하고 별도 repository에서 시작합니다.

### B. 장기 개발 branch에서 시작하고 기존 `main` 유지

새 엔진이 어느 정도 완성된 후 기본 branch를 교체합니다.

### C. **권고안 — 기존 상태를 immutable하게 보존한 뒤 같은 저장소의 `main`을 clean slate로 전환**

- 전환 직전 commit에 명확한 immutable tag 생성
  예: `legacy-research-v1-final`
- 필요하면 frozen archival branch도 생성
- 기존 `tasks.yaml`, ADR, review packet, old design은 tag에서만 조회
- 새 `main`에는 과거 문서와 task graph를 복제하지 않음
- 새 root는 최소 문서와 새 package skeleton으로 시작
- legacy 자산을 이식할 때 원본 tag/path/SHA를 provenance로 기록
- Git history는 남아 있으므로 실질 자산을 참조할 수 있지만, 새 에이전트의 기본 검색 공간에서는 제거
- 새 core의 package name은 계속 `omniphysics`를 사용하되 기존 내부 API 호환은 보장하지 않음
- 첫 prototype promotion 전까지 public API stability와 backward compatibility는 비목표
- scene/material/evaluation artifact 중 외부에 저장되는 형식만 명시적으로 versioning

**C를 채택할까요?**

---

## 답변 이후의 종료 기준

위 6개가 닫히면 핵심 제품 결정을 더 묻지 않겠습니다. 다음 문서 세트에는 지금까지의 합의를 다음처럼 반영할 예정입니다.

| 문서 | 역할 |
|---|---|
| `SSOT.md` | standalone 예측 물리엔진의 목표, prototype 범위, 장기 north-star, 성공 기준 |
| `CLAUDE.md` | Fable orchestrator의 capability 중심 자율 운영, 후보 예산, lane, 인간 승인 경계 |
| `AGENTS.md` | Codex worker의 구현·실험·보고·격리 규칙 |
| `CAPABILITIES.md` | executable capability graph와 상태 체계 |
| `CHECKPOINT.md` | 중단·재개 가능한 현재 상태 형식 |

또한 첫 실행 순서와 capability milestones는 별도 질문 없이 지금까지의 답을 바탕으로 작성하겠습니다.

---

> 👤 **USER TURN**

ok. 전부 권고대로.

---

> 🤖 **GPT TURN**

## 질문 단계 종료

이제 목표는 문서화 가능한 수준으로 충분히 선명해졌습니다. 핵심 합의는 다음과 같습니다.

- 본체는 **VLA용 데이터 공장**이 아니라 **standalone predictive physics engine**이다.
- Isaac Sim은 asset·renderer·sensor·environment shell이며, authoritative physics는 전부 `omniphysics`가 소유한다.
- PhysX/Newton은 baseline·reference일 뿐 production dynamics에 참여하지 않는다.
- 하나의 만능 formulation이 아니라, rigid/contact·liquid/film·cloth 전문 solver를 공통 runtime과 interface semantics 아래 결합한다.
- Prototype의 주력 정량 축은 `rigid/contact`와 `liquid/wet coupling`, 구조 검증 축은 `cloth/deformable`이다.
- 개발은 `Exploration → Evaluation → Promotion`으로 분리하고, candidate generation 수로 연구를 제한한다.
- 기존 task/ADR/gate/roadmap은 clean-slate 이후 authority가 아니다.
- Claude/Fable orchestrator 규칙은 `CLAUDE.md`, Codex worker 규칙은 `AGENTS.md`로 분리한다.
- 진행률은 task 수가 아니라 executable capability maturity로 측정한다.

현재 GitHub `main`의 최신 commit은 `27f46b27dc2333c7703446664cf77b2f41de34e8`이며, 문서에는 이를 legacy boundary로 기록했습니다. fileciteturn27file0L1-L2

## 작성한 문서

### 전체 번들

[Clean-slate 문서 전체 ZIP](sandbox:/mnt/data/newton-omniphysics-clean-slate-docs.zip)

### 개별 파일

- [SSOT.md](sandbox:/mnt/data/newton-omniphysics-clean-slate-docs/SSOT.md) — 제품 정체성, prototype/장기 범위, 물리 인과관계, 성공 기준, 인간 승인 경계
- [CLAUDE.md](sandbox:/mnt/data/newton-omniphysics-clean-slate-docs/CLAUDE.md) — capability 중심 orchestration, lane 분리, candidate budget, WIP, drift 방지, 초기 실행 순서
- [AGENTS.md](sandbox:/mnt/data/newton-omniphysics-clean-slate-docs/AGENTS.md) — Codex worker의 scope, worktree 격리, 구현·평가 역할, GPU·검증·보고 규칙
- [CAPABILITIES.md](sandbox:/mnt/data/newton-omniphysics-clean-slate-docs/CAPABILITIES.md) — `C00–C15` capability register, canonical scene, promotion·prototype exit 조건
- [CHECKPOINT.md](sandbox:/mnt/data/newton-omniphysics-clean-slate-docs/CHECKPOINT.md) — 최초 clean-slate bootstrap 상태, 첫 falsifiable hypothesis, candidate budget, 재개 template
- [BUNDLE_README.md](sandbox:/mnt/data/newton-omniphysics-clean-slate-docs/BUNDLE_README.md) — 적용 순서 요약

## 문서에서 의도적으로 바꾼 것

### `SSOT.md`

North-star를 다음처럼 재정의했습니다.

> 제한된 장비로 보정하고, 미관측 조건을 예측하며, 단일 워크스테이션 GPU에서 준실시간으로 실행되는 로봇 조작용 독립 물리엔진.

`bulk liquid → surface film → contact squeeze-out → compliant pressure → wet friction → rigid motion`을 명시적인 필수 인과사슬로 넣었습니다. 동시에 heat, vapor, phase change, fracture, granular 등은 prototype 비범위로 내렸지만 향후 solver registration과 coupling boundary를 통한 확장은 막지 않았습니다.

### `CLAUDE.md`

기존의 `design/freeze/review → 구현` 순서를 다음으로 뒤집었습니다.

```text
implement and learn
→ freeze and evaluate
→ document and promote
```

시간 기반 milestone은 넣지 않았습니다. 대신 기본 연구 예산을 다음처럼 제한했습니다.

```text
baseline family: 1
challenger family: 최대 2
family당 major revision: 최대 2
동일 evaluation generation review/fix: 최대 2
자율 major architecture fork: 0
```

문서·게이트·리뷰만으로는 capability maturity가 올라가지 않습니다.

### `CAPABILITIES.md`

초기 clean-slate 상태는 모두 `ABSENT`로 두었습니다. 과거 구현이 있다는 이유만으로 새 mainline capability로 인정하지 않습니다.

첫 실행 경로는 다음입니다.

```text
C00 Authoritative GPU runtime
  → C02 Rigid-body dynamics
  → C04 Collision
  → C05 Persistent contact/friction
  → S01 Dry slip
```

그 뒤 articulation, compliant contact, liquid→film→wet-contact, cloth consumer 순으로 확장합니다.

## 아직 수행하지 않은 저장소 변경

문서 파일은 draft artifact로 작성했으며, GitHub 저장소에는 아직 다음 작업을 수행하지 않았습니다.

- `legacy-research-v1-final` tag 생성
- 기존 `main`의 clean-slate 교체
- package tree 삭제·재구성
- 문서 commit/push
- 기존 구현의 archive 또는 asset 선별

`CHECKPOINT.md`의 `NOT_CREATED`, `NOT_ESTABLISHED`, `NOT_DEFINED` 표시는 이 상태를 의도적으로 나타냅니다. 다음 실제 적용 단계는 legacy tag를 먼저 고정한 뒤, 같은 저장소의 `main`을 이 다섯 root 문서와 최소 package skeleton으로 교체하는 것입니다.

---
