#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Review-bundle builder + ChatGPT reviewer-kit renderer.

A review bundle is a SHA-pinned `jahns-review-bundle/v1` zip the user attaches to the web reviewer
(ChatGPT). Unlike a naive working-tree zip it (a) ships `repo/` built DIRECTLY from git objects of
the reviewed head — tracked files only, no .git/caches/secrets, and (crucially) a tracked symlink is
stored as a REGULAR file holding its target STRING (mode 0o100644, never a S_IFLNK entry that `unzip`
would rebuild as a live link) and recorded in manifest.symlinks, so it can't resolve out-of-tree at
the reviewer; (b) carries a base..head diff so the reviewer sees the changed surface; and
(c) binds review identity (project/round/mode/cycle/base/head) so the reply can be cross-checked at
ingest. Generation + schema validation are script-deterministic (content is a pure function of the
inputs), never a model hand-assembling a zip.

Usage:
  jw_bundle.py bundle <root> --round <id> [--out <dir>]   # packet mode: base from the round sidecar, head = HEAD
  jw_bundle.py bundle <root> --pr <N>    [--out <dir>]     # pr mode: identity from the frozen review cycle
  jw_bundle.py kit [--out <dir>]                           # render the reviewer kit (static templates)

Exit codes: 0 ok, 1 usage/precondition failure, 2 schema-invalid bundle.
"""
from __future__ import annotations

import datetime
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yaml  # noqa: E402

from jw_common import (  # noqa: E402
    CONFIG_NAME, WorkflowError, find_project_root, git_full_sha, git_rc, is_ancestor,
    load_config, normalize_config, upstream_ref,
)

# git's canonical empty tree — the base for a first-round (base=None) review, so DIFF/CHANGED_FILES
# describe the same (root)..head surface COMMITS and the manifest comparison advertise.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
BUNDLE_SCHEMA = "jahns-review-bundle/v1"
RECORD_SCHEMA = "jahns-review-bundle-record/v1"
KIT_SCHEMA = "jw-reviewer-kit/v1"
REVIEWER_PROTOCOL = "jw-chatgpt-reviewer/v1"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)  # fixed entry timestamp → byte-stable archive (modulo the manifest's generated_at)
KIT_DIR = Path(__file__).resolve().parent.parent / "templates" / "chatgpt-reviewer"
KIT_SOURCES = (
    "PROJECT_INSTRUCTIONS.txt", "JW_INSTRUCTION.md", "JW_REPOSITORY_CONTRACT.md",
    "JW_REVIEW_PLAYBOOK.md", "JW_OUTPUT_CONTRACT.md", "JW_EXAMPLES.md",
)
# loose kit: a short, unversioned domain-reviewer setup for the raw-repo-zip flow (the default).
# Not a provenance protocol — no hash manifest — just enough to keep an external reviewer on
# domain review (and off the harness) while the per-round brief carries the specifics.
KIT_LOOSE_DIR = Path(__file__).resolve().parent.parent / "templates" / "chatgpt-reviewer-loose"
KIT_LOOSE_SOURCES = ("REVIEWER_INSTRUCTIONS.md", "REVIEWER_CONTEXT.md")


def _is_sha(v: object) -> bool:
    import re
    return isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}", v) is not None


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── round sidecar (base watermark) ───────────────────────────────────────────
# round close records the BASE (previous watermark); the HEAD is the commit actually bundled, which
# `jw review bundle` stamps in at build time (the reviewed tree is the committed HEAD, so repo/ and
# the manifest scope are computed from the SAME tree — no pre-/post-closeout provenance split).

def record_path(root: Path, cfg: dict, round_id: str) -> Path:
    return root / cfg["reviews_dir"] / f"{round_id}-bundle.yaml"


def write_record(root: Path, cfg: dict, identity: dict) -> Path:
    prev = read_record(root, cfg, identity["round_id"]) or {}
    rec = {
        "schema": RECORD_SCHEMA,
        "project": identity["project"],
        "round_id": identity["round_id"],
        "review_mode": "packet",
        "review_cycle": None,
        "branch": identity.get("branch"),
        "base_sha": identity.get("base_sha"),
        "round_commit": identity.get("round_commit"),  # the round's tip (= last_round_commit at close)
        # a prior `jw review bundle` stamp is preserved across a re-close, so re-closing a
        # bundled round can't silently null the head and disable the ingest SHA cross-check.
        "head_sha": prev.get("head_sha"),
        "bundled_at_utc": prev.get("bundled_at_utc"),
    }
    p = record_path(root, cfg, identity["round_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return p


def read_record(root: Path, cfg: dict, round_id: str) -> dict | None:
    p = record_path(root, cfg, round_id)
    if not p.is_file():
        return None
    try:
        rec = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    return rec if isinstance(rec, dict) else None


def record_stamp_head(root: Path, cfg: dict, round_id: str, head: str) -> None:
    """Persist the exact reviewed head so `jw review ingest` can bind the reply to it."""
    rec = read_record(root, cfg, round_id) or {}
    rec["head_sha"] = head
    rec["bundled_at_utc"] = _now_utc()
    record_path(root, cfg, round_id).write_text(
        yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _resolve_packet(root: Path, cfg: dict, round_id: str) -> dict:
    rec = read_record(root, cfg, round_id)
    if rec is None:
        raise WorkflowError(
            f"no bundle record at {record_path(root, cfg, round_id)} — close the round first "
            f"(`jw round close . --round {round_id}`) so the base watermark is captured.")
    if rec.get("schema") != RECORD_SCHEMA:
        raise WorkflowError(f"bundle record schema mismatch: {rec.get('schema')!r}")
    head = git_full_sha(root, "HEAD")  # the committed HEAD IS the reviewed tree (repo/ == scope source)
    if head is None:
        raise WorkflowError("cannot resolve HEAD to a commit.")
    return {
        "project": rec.get("project") or cfg.get("project"),
        "round_id": round_id,
        "review_mode": "packet",
        "review_cycle": None,
        "branch": rec.get("branch"),
        "base_sha": rec.get("base_sha") if _is_sha(rec.get("base_sha")) else None,
        "round_commit": rec.get("round_commit") if _is_sha(rec.get("round_commit")) else None,
        "head_sha": head,
        "repo": None,
    }


def _resolve_pr(root: Path, pr: int, round_id: str | None) -> dict:
    """PR-mode identity comes from the frozen review cycle (the SHA-bound target), not a sidecar —
    the macro reviewer reviews the same head the cycle pinned, even though it can no longer browse
    the repo through the (broken) chat connector."""
    import jw_review
    ctx = jw_review.pr_context(root, pr)
    if ctx is None:
        raise WorkflowError(f"could not load PR #{pr} context (gh/auth/repo).")
    cfg = ctx["policy"] or load_config(root)
    # provenance: the repo owner is always a trusted operator (added at use-sites, not in the config
    # default) — mirror facts_from_bundle/merge so an untrusted PR commenter can't inject a higher
    # freeze marker to redirect the bundled SHA.
    owner = ctx["repo"].split("/", 1)[0] if ctx.get("repo") else ""
    operators = tuple({owner, *((cfg.get("review", {}) or {}).get("operators", []) or [])} - {""})
    markers = jw_review.parse_bodies(ctx["bundle"]["bodies"])
    lc = jw_review.latest_cycle(markers, operators)
    if lc is None:
        raise WorkflowError(
            f"PR #{pr} has no frozen review cycle — run `jw review freeze --pr {pr}` first.")
    head = lc.get("target_sha")
    if not _is_sha(head):
        raise WorkflowError(f"frozen cycle target_sha is not a 40-hex sha: {head!r}")
    return {
        "project": cfg.get("project"),
        "round_id": round_id or lc.get("round_id") or f"pr-{pr}-cycle-{lc.get('cycle')}",
        "review_mode": "pr",
        "review_cycle": lc.get("cycle"),
        "branch": ctx["bundle"].get("head_ref"),
        "base_sha": lc.get("base_sha") if _is_sha(lc.get("base_sha")) else None,
        "head_sha": head,
        "repo": ctx["repo"],
    }


# ── git evidence ─────────────────────────────────────────────────────────────

def _git_bytes(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        p = subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorkflowError(f"git {args[0]} failed: {e}") from e
    if p.returncode != 0:
        raise WorkflowError(f"git {args[0]} {' '.join(args[1:])[:40]} failed: "
                            f"{p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout


def _sha_pushed(root: Path, sha: str) -> tuple[bool, str]:
    up = upstream_ref(root)
    if not up:
        return (False, "no upstream tracking branch")
    rc, _, err = git_rc(root, "fetch", "--quiet", up.split("/", 1)[0])
    if rc != 0:
        return (False, f"fetch failed — remote unverifiable: {err or 'error'}")
    return (is_ancestor(root, sha, up), up)


def _repo_entries(root: Path, head: str, omissions: list[dict]) -> list[tuple[str, str, str, str]]:
    """(path, mode, type, sha) for every tracked object in the head tree, straight from git — the
    EXACT tracked tree, NOT `git archive` (which honors .gitattributes export-ignore/export-subst
    and would silently drop or substitute files). `-r` lists blobs + gitlinks (type 'commit'). A
    non-utf8 path can't be a faithful zip arcname; it's dropped AND recorded as an omission (so
    bundle_complete flips false) rather than silently shrinking the tree."""
    raw = _git_bytes(root, "ls-tree", "-r", "-z", head)
    out: list[tuple[str, str, str, str]] = []
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, _, pth = rec.partition(b"\t")
        parts = meta.split(b" ")
        if len(parts) != 3:
            continue
        try:
            path = pth.decode("utf-8")
        except UnicodeDecodeError:
            omissions.append({"kind": "non-utf8-path", "detail": repr(pth)})
            continue
        out.append((path, parts[0].decode(), parts[1].decode(), parts[2].decode()))
    return out


def _read_blobs(root: Path, shas: list[str]) -> dict[str, bytes]:
    """Blob bytes for the given object shas via a single `git cat-file --batch` (no filesystem).
    Fail-closed: every requested object MUST come back, else (a missing/pruned object — gc race,
    corruption, blobless clone) the bundle would silently ship empty files, so we raise."""
    uniq = sorted(set(shas))
    if not uniq:
        return {}
    out = _git_bytes(root, "cat-file", "--batch", stdin=("\n".join(uniq) + "\n").encode())
    res: dict[str, bytes] = {}
    i, n = 0, len(out)
    while i < n:
        nl = out.find(b"\n", i)
        if nl < 0:
            break
        header = out[i:nl].decode("utf-8", "replace").split(" ")
        i = nl + 1
        if len(header) < 3 or not header[2].isdigit():
            continue  # a "<sha> missing" line carries NO content — skip it, the assert below catches it
        res[header[0]] = out[i:i + int(header[2])]
        i += int(header[2]) + 1  # content + trailing newline
    missing = set(uniq) - set(res)
    if missing:
        raise WorkflowError(
            f"{len(missing)} tracked object(s) unreadable from git (e.g. {sorted(missing)[0][:12]}) — "
            f"object DB incomplete (gc race / corruption / blobless clone); refusing to ship empty files.")
    return res


# bookkeeping paths a round-close commit may touch — used to tell a legitimate closeout commit (head
# == round tip + bookkeeping edits) apart from forward/next-round CODE commits.
def _config_at(root: Path, ref: str) -> dict:
    """Parse + normalize .jahns-workflow.yml AS OF `ref` (the reviewed round tip). Fail CLOSED: a
    config that can't be read/parsed at the round tip means we can't define that round's bookkeeping
    surface, so we refuse rather than fall back to the (mutable) working-tree config — a later commit
    must not be able to widen the bookkeeping dirs and reclassify forward CODE as a closeout edit."""
    rc, out, err = git_rc(root, "show", f"{ref}:{CONFIG_NAME}")
    if rc != 0:
        raise WorkflowError(
            f"cannot read {CONFIG_NAME} at round tip {ref[:12]} ({err.strip() or 'not found'}) — "
            f"cannot verify the round's bookkeeping surface; re-close the round before bundling.")
    try:
        return normalize_config(yaml.safe_load(out))
    except (yaml.YAMLError, ValueError) as e:
        raise WorkflowError(
            f"{CONFIG_NAME} at round tip {ref[:12]} is unparseable ({e}) — cannot verify the round's "
            f"bookkeeping surface; re-close the round before bundling.")


def _closeout_only(root: Path, frm: str, head: str) -> bool:
    rc, out, err = git_rc(root, "diff", "--name-only", f"{frm}..{head}")
    if rc != 0:  # fail CLOSED: an empty stdout on a *failed* diff must never read as "closeout-only"
        raise WorkflowError(
            f"cannot verify round binding: `git diff {frm[:12]}..{head[:12]}` failed "
            f"({err.strip() or 'error'}) — refusing to bundle on an unverifiable diff.")
    # bookkeeping surface is the config AS OF the round tip (`frm`), never the working tree — else a
    # post-close `generated_dir`/etc. change could wave forward CODE through under this round's label.
    cfg = _config_at(root, frm)
    ok_files = {"tasks.yaml", CONFIG_NAME, "ROADMAP.md", cfg.get("progress", "PROGRESS.md")}
    ok_dirs = tuple(str(d).rstrip("/") + "/" for d in
                    (cfg.get("generated_dir"), cfg.get("reviews_dir"),
                     cfg.get("progress_archive_dir"), cfg.get("adr_dir")) if d)
    for f in (ln for ln in out.splitlines() if ln.strip()):
        if f not in ok_files and not any(f.startswith(d) for d in ok_dirs):
            return False
    return True


def _round_binding_error(root: Path, cfg: dict, identity: dict, head: str) -> str | None:
    """Bind the bundled head to the round being bundled, so `jw review bundle --round X` can't review
    whatever HEAD happens to point at. head must be at/after the round tip, this round must be the
    latest closed one, and any commits past the round tip must be closeout (bookkeeping) only.

    Fail CLOSED on every axis: a record without a round_commit (a pre-binding/legacy sidecar) or a
    watermark/diff git command that can't resolve means we cannot PROVE the head belongs to this
    round, so we refuse and demand a re-close — silently skipping the guard would ship whatever tree
    HEAD points at under this round's label (the v0.3.0 fail-open GPT found)."""
    rcommit = identity.get("round_commit")
    if not _is_sha(rcommit):
        return (f"this round's bundle record has no round_commit binding (pre-binding/legacy record) — "
                f"re-close the round (`jw round close . --round {identity['round_id']}`) so the reviewed "
                f"head is bound to the round before bundling; refusing to bundle an unbound head.")
    if not is_ancestor(root, rcommit, head):
        return f"HEAD {head[:12]} is behind this round's commit {rcommit[:12]} — check out the round head."
    wm = (cfg.get("state") or {}).get("last_round_commit")
    if wm:
        wm_full = git_full_sha(root, str(wm))
        if wm_full is None:
            return (f"workflow watermark {str(wm)[:12]} is unresolvable (history rewrite/gc?) — cannot "
                    f"confirm this is the latest closed round; re-close the round before bundling.")
        if wm_full != rcommit:
            return (f"a later round was closed (watermark {wm_full[:12]} ≠ this round's {rcommit[:12]}) — "
                    f"bundle the current round, or re-close this one.")
    if head != rcommit:
        try:
            if not _closeout_only(root, rcommit, head):
                return (f"HEAD has commit(s) past the round tip {rcommit[:12]} that change non-bookkeeping "
                        f"files — commit only the round closeout before bundling, or re-close the round.")
        except WorkflowError as e:  # a failed binding diff fails closed (not silently closeout-only)
            return str(e)
    return None


# base=None (first round) diffs against the empty tree so DIFF/CHANGED_FILES cover the full
# (root)..head surface — consistent with COMMITS (`git log <head>`) and the manifest comparison,
# and (unlike `git show`) rendering a merge-commit head correctly.
def _diff(root: Path, base: str | None, head: str) -> str:
    _, out, _ = git_rc(root, "diff", "--find-renames", f"{base or EMPTY_TREE}..{head}")
    return out + "\n" if out else ""


def _changed_files(root: Path, base: str | None, head: str) -> tuple[str, int]:
    _, out, _ = git_rc(root, "diff", "--name-status", "--find-renames", f"{base or EMPTY_TREE}..{head}")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return ("\n".join(lines) + "\n" if lines else ""), len(lines)


def _commits(root: Path, base: str | None, head: str) -> str:
    rng = f"{base}..{head}" if base else head  # base=None → full history reachable from head
    _, out, _ = git_rc(root, "log", "--no-merges", "--pretty=format:%H %s (%an, %aI)", rng)
    return out + "\n" if out else ""


def _scope(root: Path, round_id: str, head: str) -> dict:
    """Round tasks/anchors read from tasks.yaml AT THE REVIEWED HEAD (not the working tree), so the
    manifest scope can never disagree with the registry the reviewer reads in repo/tasks.yaml."""
    _, out, _ = git_rc(root, "show", f"{head}:tasks.yaml")
    try:
        data = yaml.safe_load(out) if out else {}
    except yaml.YAMLError:
        data = {}
    tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict) and t.get("round") == round_id]
    ids = sorted(t["id"] for t in tasks if isinstance(t.get("id"), str))
    anchors = sorted({t["anchor"] for t in tasks if isinstance(t.get("anchor"), str) and t.get("anchor")})
    return {"tasks": ids, "ssot_anchors": anchors}


# ── manifest ─────────────────────────────────────────────────────────────────

def build_manifest(cfg: dict, identity: dict, has_checks: bool,
                   worktree_dirty: bool, omissions: list[dict],
                   symlinks: list[dict] | None = None) -> dict:
    base = identity.get("base_sha")
    paths = {
        "repo_root": "repo",
        "request": "__review__/REQUEST.md",
        "diff": "__review__/DIFF.patch",
        "changed_files": "__review__/CHANGED_FILES.txt",
        "commits": "__review__/COMMITS.txt",
    }
    if has_checks:
        paths["checks"] = "__review__/CHECKS.yaml"
    return {
        "schema": BUNDLE_SCHEMA,
        "reviewer_protocol": REVIEWER_PROTOCOL,
        "bundle_complete": not omissions,
        "project": identity.get("project"),
        "repo": identity.get("repo"),
        "round_id": identity["round_id"],
        "review_mode": identity["review_mode"],
        "review_cycle": identity.get("review_cycle"),
        "branch": identity.get("branch"),
        "base_sha": base,
        "head_sha": identity["head_sha"],
        "comparison": f"{base}..{identity['head_sha']}" if base else f"(root)..{identity['head_sha']}",
        "generated_at_utc": _now_utc(),
        "tree_source": "git-ls-tree",  # exact tracked tree from git objects (no export-ignore/subst, no symlink follow)
        "worktree_dirty": worktree_dirty,
        # tracked symlinks: shipped under repo/ as REGULAR files holding the link target (never a live
        # link); this list tells the reviewer which repo/ files are symlink targets, not file contents.
        "symlinks": symlinks or [],
        "paths": paths,
        "workflow": {
            "config": CONFIG_NAME,
            "ssot": cfg.get("ssot"),
            "tasks": "tasks.yaml",
            "progress": cfg.get("progress"),
            "roadmap": "ROADMAP.md",
            "adr_dir": cfg.get("adr_dir"),
            "reviews_dir": cfg.get("reviews_dir"),
            "generated_dir": cfg.get("generated_dir"),
        },
        "scope": None,  # filled by the caller (tasks/anchors/changed_file_count)
        "omissions": omissions,
    }


def validate_manifest(m: dict) -> list[str]:
    """Schema check before publishing — required identity + path/workflow/scope shape. Fail-closed."""
    errs: list[str] = []
    if not isinstance(m, dict):
        return ["manifest must be a mapping"]
    if m.get("schema") != BUNDLE_SCHEMA:
        errs.append(f"schema must be {BUNDLE_SCHEMA!r}")
    if m.get("reviewer_protocol") != REVIEWER_PROTOCOL:
        errs.append(f"reviewer_protocol must be {REVIEWER_PROTOCOL!r}")
    if not (isinstance(m.get("project"), str) and m["project"].strip()):
        errs.append("project: required non-empty string")
    if not (isinstance(m.get("round_id"), str) and m["round_id"].strip()):
        errs.append("round_id: required non-empty string")
    if m.get("review_mode") not in ("packet", "pr"):
        errs.append("review_mode: must be 'packet' or 'pr'")
    cyc = m.get("review_cycle")
    if not (cyc is None or (type(cyc) is int and cyc >= 1)):
        errs.append("review_cycle: must be a positive int or null")
    if m.get("review_mode") == "pr" and cyc is None:
        errs.append("review_cycle: required in pr mode")
    if not _is_sha(m.get("head_sha")):
        errs.append("head_sha: must be a 40-hex commit sha")
    if not (m.get("base_sha") is None or _is_sha(m.get("base_sha"))):
        errs.append("base_sha: must be a 40-hex commit sha or null")
    if _is_sha(m.get("base_sha")) and m.get("base_sha") == m.get("head_sha"):
        errs.append("base_sha: equals head_sha (empty review range)")  # gate, don't trust the writer
    if not isinstance(m.get("bundle_complete"), bool):
        errs.append("bundle_complete: must be a boolean")
    paths = m.get("paths")
    if not isinstance(paths, dict) or "repo_root" not in paths or "request" not in paths:
        errs.append("paths: must include at least repo_root and request")
    if not isinstance(m.get("workflow"), dict):
        errs.append("workflow: must be a mapping")
    scope = m.get("scope")
    if not (isinstance(scope, dict) and isinstance(scope.get("changed_file_count"), int)):
        errs.append("scope.changed_file_count: required int")
    if not isinstance(m.get("omissions"), list):
        errs.append("omissions: must be a list")
    if not isinstance(m.get("symlinks"), list):
        errs.append("symlinks: must be a list")
    return errs


# ── bundle assembly (direct from git objects — never the filesystem) ─────────

def _zi(arcname: str, unix_mode: int) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(arcname, date_time=ZIP_EPOCH)
    zi.external_attr = unix_mode << 16
    zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def _write_bundle_zip(out_zip: Path, review_files: dict[str, bytes],
                      repo_entries: list[tuple[str, str, str, str]], blobs: dict[str, bytes]) -> None:
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(review_files):
            zf.writestr(_zi(f"__review__/{rel}", 0o100644), review_files[rel])
        for path, mode, typ, sha in sorted(repo_entries, key=lambda e: e[0]):
            if typ != "blob":
                continue  # gitlink/submodule recorded as an omission, never materialized
            content = blobs.get(sha, b"")
            if mode == "120000":
                # SYMLINK: store the target as a REGULAR text file (mode 0o100644), NOT a S_IFLNK
                # entry. A S_IFLNK zip entry is rebuilt as a LIVE link by `unzip` (it honors the mode
                # bit), which would resolve out-of-tree at the reviewer; a regular file holding the
                # target string can never do that. The original symlink-ness is recorded in
                # manifest.symlinks, never in the zip mode.
                zf.writestr(_zi(f"repo/{path}", 0o100644), content)
            else:
                zf.writestr(_zi(f"repo/{path}", 0o100755 if mode == "100755" else 0o100644), content)


def _default_out(root: Path, cfg: dict) -> Path:
    """Bundles are build artifacts, not source — default to an ignored subdir so a `*.review.zip`
    never lands in a tracked path. The request/feedback markdown stay tracked in reviews_dir."""
    d = root / cfg["reviews_dir"] / "bundles"
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("# review bundles are build artifacts, not source\n*.review.zip\n", encoding="utf-8")
    return d


def bundle(root: Path, round_id: str | None, pr: int | None, out_dir: Path | None,
           allow_unpushed: bool = False) -> int:
    cfg = load_config(root)
    try:
        identity = _resolve_pr(root, pr, round_id) if pr is not None else _resolve_packet(root, cfg, round_id)
    except WorkflowError as e:
        print(f"jw_bundle: {e}", file=sys.stderr)
        return 1
    head = identity["head_sha"]
    base = identity.get("base_sha")

    # packet: bind the resolved HEAD to the round being bundled, so a stale --round (HEAD moved on to
    # later/next-round commits) can't ship the wrong tree under this round's label.
    if pr is None:
        binding_err = _round_binding_error(root, cfg, identity, head)
        if binding_err:
            print(f"jw_bundle: {binding_err}", file=sys.stderr)
            return 1

    # base must be a reachable commit AND an ancestor of head, else the diff would be empty or
    # cross-branch. Fail closed rather than ship a misleading diff (history rewrite/gc / wrong base).
    if base is not None:
        if git_full_sha(root, base) is None:
            print(f"jw_bundle: recorded base {base[:12]} is not a reachable commit (history rewrite/gc?) — "
                  f"re-bundle from a reachable base or re-close the round.", file=sys.stderr)
            return 1
        if not is_ancestor(root, base, head):
            print(f"jw_bundle: base {base[:12]} is not an ancestor of head {head[:12]} — the recorded "
                  f"base is on a different line of history; re-close the round.", file=sys.stderr)
            return 1

    pushed, info = _sha_pushed(root, head)
    if not pushed and not allow_unpushed:
        print(f"jw_bundle: head {head[:12]} is not pushed ({info}) — push the review commit first, "
              f"or pass --allow-unpushed. A review must point at a durable commit.", file=sys.stderr)
        return 1

    dirty = bool([ln for ln in git_rc(root, "status", "--porcelain")[1].splitlines()
                  if ln and not ln.startswith("??")])

    # request body: the model-authored round request (claims). Required — it carries the
    # falsifiable acceptance claims the reviewer attacks; the tree alone is not a request.
    # A symlinked request/checks is refused: read_bytes() would FOLLOW it and ship out-of-tree
    # content into __review__/, defeating the bundle's "nothing outside the tracked tree leaks"
    # promise (the control plane reads from the filesystem, unlike repo/ which is git objects).
    req_path = root / cfg["reviews_dir"] / f"{identity['round_id']}-request.md"
    if req_path.is_symlink():
        print(f"jw_bundle: review request {req_path} is a symlink — refusing (a symlinked request "
              f"would ship out-of-tree content); replace it with a regular file.", file=sys.stderr)
        return 1
    if not req_path.is_file():
        print(f"jw_bundle: no review request at {req_path} — write it (templates/review-request.md) "
              f"before bundling.", file=sys.stderr)
        return 1
    request = req_path.read_bytes()

    checks_path = root / cfg["reviews_dir"] / f"{identity['round_id']}-checks.yaml"
    if checks_path.is_symlink():
        print(f"jw_bundle: review checks {checks_path} is a symlink — refusing (would ship out-of-tree "
              f"content); replace it with a regular file or remove it.", file=sys.stderr)
        return 1
    checks = checks_path.read_bytes() if checks_path.is_file() else None

    omissions: list[dict] = []
    try:
        entries = _repo_entries(root, head, omissions)  # appends any non-utf8-path omission
        blobs = _read_blobs(root, [sha for _p, _m, typ, sha in entries if typ == "blob"])
    except WorkflowError as e:
        print(f"jw_bundle: {e}", file=sys.stderr)
        return 1
    omissions += [{"kind": "submodule", "detail": f"{p}: gitlink {sha[:12]} not materialized"}
                  for p, _m, typ, sha in entries if typ == "commit"]
    # tracked symlinks: the blob content IS the link target; ship it as a regular file (see
    # _write_bundle_zip) and record path→target here so the reviewer knows it's a link, not content.
    symlinks = [{"path": p, "target": blobs.get(sha, b"").decode("utf-8", "replace")}
                for p, m, typ, sha in entries if typ == "blob" and m == "120000"]

    diff = _diff(root, base, head)
    changed, changed_count = _changed_files(root, base, head)
    commits = _commits(root, base, head)
    scope = {**_scope(root, identity["round_id"], head), "changed_file_count": changed_count}

    manifest = build_manifest(cfg, identity, checks is not None, dirty, omissions, symlinks)
    manifest["scope"] = scope
    errs = validate_manifest(manifest)
    if errs:
        for e in errs:
            print(f"jw_bundle: manifest invalid — {e}", file=sys.stderr)
        return 2

    review_files: dict[str, bytes] = {
        "MANIFEST.yaml": yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True).encode("utf-8"),
        "REQUEST.md": request,
        "DIFF.patch": diff.encode("utf-8"),
        "CHANGED_FILES.txt": changed.encode("utf-8"),
        "COMMITS.txt": commits.encode("utf-8"),
    }
    if checks is not None:
        review_files["CHECKS.yaml"] = checks

    out_dir = out_dir.resolve() if out_dir else _default_out(root, cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_zip = out_dir / f"{identity['project']}@{head[:12]}__{identity['round_id']}.review.zip"
    _write_bundle_zip(out_zip, review_files, entries, blobs)
    if pr is None:
        record_stamp_head(root, cfg, identity["round_id"], head)  # so ingest can bind the reply to this head

    print(f"bundle: {out_zip}")
    print(f"  schema {BUNDLE_SCHEMA}  mode {identity['review_mode']}"
          + (f" cycle {identity['review_cycle']}" if identity['review_cycle'] else ""))
    print(f"  base {base[:12] if base else '(root)'}..{head[:12]}  "
          f"{changed_count} changed file(s){'  [worktree dirty]' if dirty else ''}")
    if omissions:
        print(f"  omissions: {len(omissions)} (see manifest)")
    print("Attach the zip to the reviewer and paste:")
    print("  첨부한 review bundle을 Project Sources의 JW_INSTRUCTION.md 절차로 검토하고, "
          "JW_OUTPUT_CONTRACT.md 형식으로만 답해.")
    return 0


# ── reviewer kit ─────────────────────────────────────────────────────────────

def kit(out_dir: Path | None, mode: str = "loose") -> int:
    """Render the ChatGPT reviewer kit. Default `mode="loose"` writes the short domain-reviewer
    setup (REVIEWER_INSTRUCTIONS + optional REVIEWER_CONTEXT) for the raw-repo-zip flow.
    `mode="strict"` writes the SHA-pinned JW_* protocol kit + a tamper-evident KIT_MANIFEST for
    provenance-gated (PR) review. Carries no target-repository state — one-time per-protocol setup."""
    if mode == "loose":
        out = (out_dir or Path.cwd() / "jahns-chatgpt-reviewer-kit").resolve()
        out.mkdir(parents=True, exist_ok=True)
        for name in KIT_LOOSE_SOURCES:
            src = KIT_LOOSE_DIR / name
            if not src.is_file():
                print(f"jw_bundle kit: missing template {src}", file=sys.stderr)
                return 1
            (out / name).write_bytes(src.read_bytes())
        print(f"reviewer kit (loose) → {out}")
        print("  ChatGPT setup: paste REVIEWER_INSTRUCTIONS.md into Project instructions; "
              "optionally upload REVIEWER_CONTEXT.md as a Project Source.")
        print("  Per round: attach the repo zip (incl. .git) + the round brief — no fixed protocol.")
        return 0
    out = (out_dir or Path.cwd() / "jahns-chatgpt-reviewer-kit").resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": KIT_SCHEMA,
        "protocol": REVIEWER_PROTOCOL,
        "bundle_schema": BUNDLE_SCHEMA,
        "generated_at_utc": _now_utc(),
        "templates": {},
    }
    for name in KIT_SOURCES:
        src = KIT_DIR / name
        if not src.is_file():
            print(f"jw_bundle kit: missing template {src}", file=sys.stderr)
            return 1
        data = src.read_bytes()
        (out / name).write_bytes(data)
        manifest["templates"][name] = hashlib.sha256(data).hexdigest()
    (out / "KIT_MANIFEST.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"reviewer kit → {out}")
    print(f"  {len(KIT_SOURCES)} sources + KIT_MANIFEST.yaml (protocol {REVIEWER_PROTOCOL})")
    print("  ChatGPT setup: paste PROJECT_INSTRUCTIONS.txt into Project instructions; "
          "upload the 5 JW_*.md as Project Sources.")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def _opt(argv: list[str], name: str) -> str | None:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _root(argv: list[str]) -> Path | None:
    for a in argv:
        if not a.startswith("-") and Path(a).is_dir():
            return Path(a).resolve()
    return find_project_root(Path.cwd())


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("bundle", "kit"):
        print(__doc__, file=sys.stderr)
        return 1
    sub, rest = argv[0], argv[1:]
    out = _opt(rest, "--out")
    if sub == "kit":
        mode = "strict" if "--strict" in rest else "loose"
        return kit(Path(out).resolve() if out else None, mode)
    root = _root(rest)
    if root is None:
        print("jw_bundle: no initialized project (missing .jahns-workflow.yml)", file=sys.stderr)
        return 1
    pr_s = _opt(rest, "--pr")
    round_id = _opt(rest, "--round")
    if pr_s is None and round_id is None:
        print("jw_bundle bundle: one of --round <id> (packet) or --pr <N> (pr) is required", file=sys.stderr)
        return 1
    try:
        return bundle(root, round_id, int(pr_s) if pr_s else None,
                      Path(out).resolve() if out else None, "--allow-unpushed" in rest)
    except WorkflowError as e:
        print(f"jw_bundle: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
