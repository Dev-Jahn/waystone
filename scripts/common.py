#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Compatibility adapter for shared Waystone helpers.

Legacy monkeypatch forwarding supports the setattr/delattr surface only; direct module
``__dict__`` mutation (e.g. ``mock.patch.dict``) is not forwarded to the moved owners
(ADR-0014 Amendment 2 Addendum 3 §2 — the module dict cannot be replaced with an
intercepting mapping; no current consumers use that surface).
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_waystone_preloaded = "waystone" in sys.modules
import waystone.adapters.git as _git_owner  # noqa: E402
import waystone.core as _core_owner  # noqa: E402
import waystone.project as _project_owner  # noqa: E402

from waystone.core import (  # noqa: E402, F401
    WorkflowError,
    Pre09StateError,
    _real_directory,
    _regular_file,
    _ensure_project_self_ignore,
    canonical_payload_hash,
    content_hash,
    _lock_verb,
    _lock_timeout,
    _lock_holder_message,
    hold_lock,
    _record_scope_path,
    normalize_scope_prefix,
    canonical_scope_prefixes,
    parse_iso_timestamp,
    _packet_declared_scope,
    _path_in_declared_scope,
    delegation_scope_drift,
    write_text_atomic,
    write_bytes_atomic,
    load_yaml,
)

from waystone.project import (  # noqa: E402, F401
    CONFIG_NAME,
    TASKS_NAME,
    machine_dir,
    project_state_path,
    ensure_project_state_dir,
    consent_path,
    record_consent,
    has_accepted_consent,
    worktrees_cache_dir,
    registry_path,
    registry_lock_path,
    overlay_lock_path,
    project_lock_path,
    require_initialized_root,
    hold_project_lock,
    _pre_0_9_host_roots,
    _preserved_pre_0_9_root,
    _checked_lstat,
    _checked_entries,
    _unresolved_pre_0_9_machine_paths,
    _append_existing,
    _append_children,
    _append_preserved_profile_conflicts,
    _unresolved_pre_0_9_project_paths,
    require_supported_machine_state,
    require_supported_project_state,
    migrate_home_data,
    migrate_project_state,
    _read_registry,
    _normalized_registry_path,
    registry_entry_paths,
    validate_registry_path_uniqueness,
    resolve_project_paths,
    TASK_TYPES,
    TASK_STATUSES,
    MILESTONE_STATUSES,
    SEVERITIES,
    TASK_ID_RE,
    MILESTONE_ID_RE,
    ROUND_RE,
    find_project_root,
    has_project_config,
    normalize_config,
    load_config,
    load_tasks,
    next_actionable,
    _project_slug,
    resume_path,
    start_here_path,
)

from waystone.adapters.git import (  # noqa: E402, F401
    git_rc,
    git_full_sha,
    upstream_ref,
    _upstream_tracking,
    _VERIFY_FETCH_REF_PREFIX,
    _VERIFY_FETCH_REF_RE,
    _sweep_stale_verify_fetch_refs,
    fetch_upstream_head,
    ancestry_status,
    is_ancestor,
    head_pushed,
    git,
    git_branch_info,
)

class _CommonShim(type(sys)):
    """Keep legacy module-level monkeypatches bound to moved helpers' owner globals.

    Legacy monkeypatch forwarding supports only setattr/delattr. Direct module
    ``__dict__`` mutation (for example, ``mock.patch.dict``) is not forwarded
    because the module dict cannot be replaced with an intercepting mapping.
    This non-conventional surface has no current consumers.
    """

    _routes = {
        name: tuple(
            owner for owner in (_core_owner, _project_owner, _git_owner)
            if name in vars(owner) and vars(owner)[name] is value
        )
        for name, value in vars(sys.modules[__name__]).items()
    }

    def __setattr__(self, name, value):
        for owner in self.__class__._routes.get(name, ()):
            setattr(owner, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        for owner in self.__class__._routes.get(name, ()):
            delattr(owner, name)
        super().__delattr__(name)


sys.path.pop(0)
if not _waystone_preloaded:
    del sys.modules["waystone"]
sys.modules[__name__].__class__ = _CommonShim
del _CommonShim, _core_owner, _project_owner, _git_owner, _waystone_preloaded
