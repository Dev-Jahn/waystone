# w0723b project brief 재-adoption runbook

이 절차는 `task/w0723b-brief-realign`을 main에 squash merge한 **뒤**, main dev checkout에서 한 번만
실행한다. squash 전 branch나 worktree에서 adoption하면 최종 commit identity가 달라지므로 유효한
인수 순서가 아니다. 시작 시 `PROJECT_BRIEF.md`는 개정된 fact와 `status: provisional`을 포함해야
하며 main worktree는 clean이어야 한다.

## Owner evidence exact bytes

evidence 파일은 아래 UTF-8 text와 마지막 newline을 exact bytes로 사용한다.

```text
Owner direction (2026-07-23): "나머지 전부 착수"
Ruling: commitment/roles-over-model-names keeps its identity and roles-over-model-names meaning, but its canonical responsibility model is coordinator, worker, verifier, reviewer.
Ruling: main, orchestrator, implementer, clerk, verifier, reviewer is pre-0.13 release-harness terminology; code and profile must not be reverted from the canonical 4-role model.
Ruling source: 2026-07-22 intent-control-plane review F3 triage and the recorded autonomous-policy decision.
```

## Main 실행

아래 block 전체를 main dev checkout에서 실행한다. `WAYSTONE_HOME`을 설정하지 않아 실제 dev
machine registry lock을 사용하고, adoption artifact는 main project의 ignored local state에 쓴다.

```bash
set -eu
MAIN_ROOT=/Users/jahn/workspace/waystone
EVIDENCE_FILE=/tmp/waystone-w0723b-brief-adoption-evidence.txt

test "$(git -C "$MAIN_ROOT" branch --show-current)" = main
test -z "$(git -C "$MAIN_ROOT" status --porcelain)"

printf '%s\n' \
  'Owner direction (2026-07-23): "나머지 전부 착수"' \
  'Ruling: commitment/roles-over-model-names keeps its identity and roles-over-model-names meaning, but its canonical responsibility model is coordinator, worker, verifier, reviewer.' \
  'Ruling: main, orchestrator, implementer, clerk, verifier, reviewer is pre-0.13 release-harness terminology; code and profile must not be reverted from the canonical 4-role model.' \
  'Ruling source: 2026-07-22 intent-control-plane review F3 triage and the recorded autonomous-policy decision.' \
  > "$EVIDENCE_FILE"

cd "$MAIN_ROOT"
UV_CACHE_DIR=/tmp/waystone-uv-cache uv run scripts/waystone.py brief check "$MAIN_ROOT"
ADOPT_OUTPUT=$(UV_CACHE_DIR=/tmp/waystone-uv-cache uv run scripts/waystone.py brief adopt "$MAIN_ROOT" --evidence "$EVIDENCE_FILE")
printf '%s\n' "$ADOPT_OUTPUT"
UV_CACHE_DIR=/tmp/waystone-uv-cache uv run scripts/waystone.py brief check "$MAIN_ROOT"
UV_CACHE_DIR=/tmp/waystone-uv-cache uv run scripts/waystone.py brief show "$MAIN_ROOT" --fact commitment/roles-over-model-names

git diff --check -- PROJECT_BRIEF.md
git diff --exit-code -- . ':(exclude)PROJECT_BRIEF.md'
git add PROJECT_BRIEF.md
git commit -m 'docs(brief): adopt canonical 4-role commitment'

ADOPTION_RECORD_DIGEST=${ADOPT_OUTPUT##*adoption_record=}
ADOPTION_RECORD_PATH="$MAIN_ROOT/.waystone/artifacts/${ADOPTION_RECORD_DIGEST/:/-}"
UV_CACHE_DIR=/tmp/waystone-uv-cache uv run - "$MAIN_ROOT" "$ADOPTION_RECORD_PATH" "$EVIDENCE_FILE" <<'PY'
import hashlib
import json
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
record_path = Path(sys.argv[2])
evidence_path = Path(sys.argv[3])
record = json.loads(record_path.read_bytes())
committed = subprocess.run(
    ["git", "show", "HEAD:PROJECT_BRIEF.md"],
    cwd=root,
    check=True,
    stdout=subprocess.PIPE,
).stdout
evidence = evidence_path.read_bytes()
assert record["schema"] == "waystone-brief-adoption-1"
assert record["after_digest"] == "sha256:" + hashlib.sha256(committed).hexdigest()
assert record["owner_evidence"]["digest"] == "sha256:" + hashlib.sha256(evidence).hexdigest()
assert record["owner_evidence"]["size"] == len(evidence)
print(f"verified adoption record {record_path.name} against commit {subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()}")
PY
```

첫 `brief check`의 기대 결과는 `status provisional`, adopt 출력은 owner evidence와 adoption record의
SHA-256 두 개, 두 번째 check의 기대 결과는 `status committed`다. `brief show` 결과의 fact text는
`coordinator·worker·verifier·reviewer 4-role` 문언이어야 한다.

## 저장 위치와 독립 검증

adopt는 evidence bytes와 `waystone-brief-adoption-1` JSON record를 각각 content-addressed artifact로
main project의 `.waystone/artifacts/sha256-<hex>`에 저장한다. `WAYSTONE_HOME`은 CLI dispatch의
machine registry lock 위치를 바꾸지만 이 project-local artifact 위치를 바꾸지 않는다. CLI 출력의
`owner_evidence=sha256:<hex>`와 `adoption_record=sha256:<hex>`가 두 artifact의 주소다.

마지막 Python 검사는 adoption record의 `after_digest`를 후속 adoption commit의
`HEAD:PROJECT_BRIEF.md` bytes와, record의 evidence digest·size를 위 exact evidence file과 대조한다.
따라서 단순히 `status committed` 문자열만 보는 것이 아니라 최종 Git frame과 보존된 owner evidence의
결속을 확인한다.
