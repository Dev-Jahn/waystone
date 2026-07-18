#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Remote reconciliation: is the local HEAD actually pushed, and how far behind is it.

Subcommands (also reachable as `waystone remote <sub>`):
  verify [root] [--round ID]  exit 0 if HEAD is pushed; with --round, also verify the packet
                              request and latest binding are byte-identical in the remote tree and
                              the binding's closeout SHA is contained in that remote (direct binding)
  drift  [root]   print how many commits the local HEAD is behind upstream (informational)

Deterministic git plus repo-artifact checks. Used by the round skill to refuse announcing a
review packet that is not byte-present in the pushed remote tree, and by the dashboard to surface
remote drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import find_project_root, head_pushed  # noqa: E402


def _root(argv: list[str]) -> Path | None:
    positional = [arg for index, arg in enumerate(argv)
                  if not arg.startswith("--")
                  and (index == 0 or argv[index - 1] != "--round")]
    if positional:
        return Path(positional[-1]).resolve()
    return find_project_root(Path.cwd())


def verify(root: Path, round_id: str | None = None) -> int:
    if round_id is not None:
        # The direct-binding gate fetches and pins the exact live upstream branch. The local
        # HEAD's relationship to that branch is deliberately not consulted — a diverged local
        # HEAD must not reject a genuinely published packet.
        import review
        return 0 if review.verify_packet_publication(root, round_id) == 0 else 3
    pushed, info = head_pushed(root, fetch=True)
    if "reason" in info:
        print(f"remote: cannot verify — {info['reason']}", file=sys.stderr)
        return 3
    if pushed:
        behind = info.get("behind")
        tail = f" ({behind} behind {info['upstream']})" if behind else ""
        print(f"remote: HEAD {info['head'][:12]} is pushed to {info['upstream']}{tail}")
        return 0
    print(f"remote: HEAD {(info.get('head') or '?')[:12]} is NOT on {info['upstream']} — push before requesting review",
          file=sys.stderr)
    return 3


def drift(root: Path) -> int:
    pushed, info = head_pushed(root, fetch=True)
    if "reason" in info:
        print(f"remote: {info['reason']}")
        return 0
    behind = info.get("behind")
    state = "pushed" if pushed else "UNPUSHED"
    print(f"{state}; {behind if behind is not None else '?'} behind {info['upstream']}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("verify", "drift"):
        print(__doc__, file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]
    root = _root(rest)
    if root is None:
        print("remote: no initialized project (missing .waystone.yml)", file=sys.stderr)
        return 1
    round_id = None
    if "--round" in rest:
        index = rest.index("--round")
        if index + 1 >= len(rest):
            print("remote verify: --round requires an id", file=sys.stderr)
            return 1
        round_id = rest[index + 1]
    if sub == "drift" and round_id is not None:
        print("remote drift: --round is valid only with verify", file=sys.stderr)
        return 1
    return verify(root, round_id) if sub == "verify" else drift(root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
