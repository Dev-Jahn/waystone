# Waystone 보존 불변조건

이 문서는 0.12 리팩터가 구현 형태와 저장 위치를 바꾸더라도 약화할 수 없는 불변조건의 확정본이다.
I-01~I-12의 문구는 `dev_docs/pre-1.0.0-refactor-proposal.md` §3, E-01~E-09의 문구는
`dev_docs/0.12.0-refactor-plan.md` §4를 권위 원천으로 한다. 집은 계획서 §3·§5와
ADR-0002~0007이 확정한 새 구조의 소유 경계다.

검증층은 해당 불변조건을 반드시 결속할 테스트 층을 뜻한다.

- `characterization`: 기존 public observable contract와 legacy fixture를 고정한다.
- `kernel test`: 새 run engine의 domain·store·adapter 경계에서 성질을 직접 검증한다.
- `fault-injection`: crash, partial write, stale lease, 손상 또는 관측 불가를 주입해 안전 방향을 검증한다.

| 불변조건 ID | 확정 문구 | 새 구조에서의 집 | 검증층 |
|---|---|---|---|
| I-01 | owner-authored intent와 acceptance가 worker가 만든 주장보다 우선한다. | `project/intent·tasks` + `runs/planner` | characterization + kernel test |
| I-02 | worker는 자신의 변경을 최종 수용할 수 없다. | `jobs/verifier·decision` + `runs/integration` | characterization + kernel test |
| I-03 | changed files, patch bytes, base/result SHA, digest는 Git/harness가 계산한다. | `adapters/git` + `runs`의 action submit 검증 | characterization + kernel test |
| I-04 | verifier evidence와 integration decision은 별도 artifact/actor로 남는다. | `jobs/verifier·decision` + content-addressed artifact store | characterization + kernel test |
| I-05 | live user work를 자동 stash, silent commit, silent 3-way apply하지 않는다. | `runs/integration·delivery` + `adapters/git` | characterization + kernel test + fault-injection |
| I-06 | 실패·재시도·discard 이력을 덮어쓰지 않는다. | `core/store`의 transitions audit + `runs/recovery` | characterization + kernel test + fault-injection |
| I-07 | 기존 프로젝트 도입과 migration은 non-destructive, previewable, idempotent다. | `core/migrations` + `project`의 legacy adapter | characterization + kernel test + fault-injection |
| I-08 | 새로운 policy는 observe → warn → enforce로 점진 승격하며 consent와 waiver를 기록한다. | `features/policy` + runtime store의 consent·waiver audit | characterization + kernel test |
| I-09 | state corruption이나 provenance 불일치는 success로 degrade하지 않는다. | `core/store·errors` + `doctor` | characterization + kernel test + fault-injection |
| I-10 | 모델에는 목표, 경계, 판단 요청만 전달하고 bookkeeping protocol은 전달하지 않는다. | `runs/planner` + job manifest·prompt adapter | characterization + kernel test |
| I-11 | host capability가 부족하면 다른 실행 형태로 조용히 가장하지 않는다. | `core` executor contract + `adapters` capability | characterization + kernel test |
| I-12 | public UX는 내부 safety machinery의 상세를 요구하지 않는다. | `cli` + `project/handoff` + public read API | characterization + kernel test |
| E-01 | 리뷰 요청 generation은 rendered digest로 결속되고 회신은 그 digest를 에코한다 | features/external_review — round-5 폐쇄 후 의미 동결 이관 | characterization + kernel test |
| E-02 | 완료 판정은 mutable cache가 아니라 verbatim body의 읽기 시점 재파생에서 온다 | DB는 cache만; 재파생 경로가 권위 | characterization + kernel test + fault-injection |
| E-03 | runner probe proof는 checkout·machine·principal·runtime(config 내용 포함, round-5) 축 exact-match에서만 재사용, not-observed는 상태 동등 | probe 테이블 + adapters/codex | characterization + kernel test + fault-injection |
| E-04 | run 기존성·closeout의 **cross-machine 판정 권위는 git-tracked closeout manifest 단독**이다. DB는 그 digest와 관측 시점만 기록하며 충돌 시 git이 우선한다 (§5-4) | runs + project/projections | kernel test + fault-injection |
| E-05 | attempt **record**는 append-only(재작성·삭제 금지). attempt의 현재 상태는 current-state row의 CAS 갱신으로 바뀌고 모든 전이는 transitions audit에 남는다. decision은 criterion별 evidence에 결속 | store 계약 (§3-1) | characterization + kernel test + fault-injection |
| E-06 | **logical 손상**(개별 artifact·record row)은 해당 run으로 격리되어 타 run 투영을 중단시키지 않는다. **SQLite file corruption 자체는 git evidence나 artifact bytes를 수정하지 않지만 artifact reference 판독을 막을 수 있으므로**, 복구 후 content digest 재검증을 요구한다. filesystem·disk corruption은 별도 위협으로 분리 기술한다 | store + doctor (§5-5) | kernel test + fault-injection |
| E-07 | **어떤 verifier artifact도 자신이 검토하지 않은 result digest를 승인하지 않는다.** 판정은 항상 `(base, patch bytes) → result digest` 쌍에 결속되며, base가 바뀌면 그 판정은 자동으로 이월되지 않는다 (§6 M2-3) | jobs/verification + integration | characterization + kernel test |
| E-08 | **실행 상태 정직성.** 출력 침묵·로그 부재·stderr 오류·heartbeat 부재만으로 실행 종료를 주장하지 않는다. `alive`는 해당 action의 관측 계약이 주는 **긍정적 liveness evidence**로만, `exited`는 **supervisor wait status 또는 action에 결속된 atomic completion evidence**로만 주장한다. 어느 쪽도 성립하지 않으면 **사유를 동반한 `unknown`**이다. **`unknown` 또는 running effect는 destructive resolution의 근거가 될 수 없다**(§3-9). 진행률은 frozen closure와 authoritative transition 사실에서만 파생하며, worker free-text는 liveness·progress·acceptance 어느 것의 증거도 아니다 | runs/observability + core lease + supervisor | kernel test + fault-injection |
| E-09 | **Durable identity·ownership·attribution·evidence ordering은 대상이 변하지 않아도 독립적으로 변할 수 있는 incidental ambient 값에 결속하지 않는다.** Durable identity는 대상이 변하지 않는 동안 유지되는 intrinsic identifier(예: canonical UUID, content digest, Git object ID)에서만 온다. boot ID·PID·process start token·monotonic time 같은 ambient 값은 계약에 observation scope와 lifetime이 명시되고 그 안에서 재관측될 때 locator 또는 freshness evidence로만 사용할 수 있다. scope 밖·불일치·재관측 불가에서는 `unknown`으로 강등하며 durable identity로 승격하지 않는다. hostname·cwd·mtime/ctime/inode·directory stat/열거 순서는 진단·표시·탐색을 넘는 identity·ownership·attribution 근거가 될 수 없다. filename delimiter 분해도 legacy adapter 밖에서는 owner identity의 추론 근거가 될 수 없다. Git-tracked filename/path는 검증된 intrinsic identity에 대한 주소와 artifact kind 식별에는 사용할 수 있으나, 이름을 분해해 owner identity를 추론할 수 없다. | core/attribution + adapters | characterization + kernel test + fault-injection |

모든 신뢰 표면은 I-09의 구체화인 fail-toward-verification을 따른다. 이 표의 검증층은
M0-C traceability matrix에서 각 ID의 실제 테스트에 연결하며, 연결되지 않은 행은 이관 완료로
간주하지 않는다.
