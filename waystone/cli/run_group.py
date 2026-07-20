"""`waystone run` user and carrier transport surface."""
from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Callable, Mapping

from waystone.core import WorkflowError
from waystone.runs.engine import CancelReason, ResumeResult, RunEngine
from waystone.runs.spec import UninitializedRunSpecError
from waystone.runs.transport import (
    ActionPlanRefusal,
    TransportError,
    encode_envelope,
    failure_envelope,
)


EngineFactory = Callable[[Path], RunEngine]


def _default_engine_factory(root: Path) -> RunEngine:
    return RunEngine(root)


_engine_factory: EngineFactory = _default_engine_factory


def _project_root(start: Path) -> Path:
    try:
        current = start.resolve(strict=True)
    except OSError as error:
        raise UninitializedRunSpecError(start) from error
    for candidate in (current, *current.parents):
        marker = candidate / ".waystone.yml"
        try:
            info = marker.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise UninitializedRunSpecError(start) from error
        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            return candidate
        raise UninitializedRunSpecError(start)
    raise UninitializedRunSpecError(start)


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _failure(error: BaseException) -> int:
    if isinstance(error, TransportError):
        failure = error
    elif isinstance(error, WorkflowError):
        code = getattr(error, "code", type(error).__name__)
        failure = ActionPlanRefusal(f"{code}: {error}")
    else:
        failure = error
    exit_code, envelope = failure_envelope(failure)
    print(encode_envelope(envelope).decode("utf-8"))
    return int(exit_code)


def _run_id(engine: RunEngine, value: str | None) -> str:
    return engine.latest_run_id() if value is None else value


def _resume_text(result: ResumeResult) -> str:
    if result.completion is not None:
        return (
            f"Run {result.run_id} completed on private integration ref "
            f"{result.completion.applied.target_ref}. Live-tree delivery is not performed."
        )
    if result.cancellation is not None:
        return f"Run {result.run_id} cancellation state: {result.cancellation.state}"
    if result.dispatch is not None:
        branch = result.dispatch
        if branch.get("engine") == "busy":
            return f"Run {result.run_id} is progressing; poll after {branch['poll_after_s']}s."
        if branch.get("engine") == "idle":
            return f"Run {result.run_id}: {branch['reason']}"
        action = branch.get("action")
        if isinstance(action, dict):
            return f"Run {result.run_id} is waiting for {action.get('executor_kind')} action."
    return f"Run {result.run_id} has no renderable result."


def _parse_optional_json(args: list[str]) -> tuple[str | None, bool]:
    run_id = None
    as_json = False
    for arg in args:
        if arg == "--json":
            if as_json:
                raise ActionPlanRefusal("--json may be passed only once")
            as_json = True
        elif run_id is None:
            run_id = arg
        else:
            raise ActionPlanRefusal(f"unexpected argument {arg!r}")
    return run_id, as_json


def main(argv: list[str]) -> int:
    """Parse one run subcommand and render only public projections/envelopes."""
    try:
        if not argv:
            raise ActionPlanRefusal(
                "expected start, resume, status, watch, cancel, or actions")
        # PC-31 is intentionally before command-specific parsing and file reads.
        root = _project_root(Path.cwd())
        engine = _engine_factory(root)
        command, args = argv[0], argv[1:]

        if command == "start":
            if len(args) != 1:
                raise ActionPlanRefusal("start requires exactly one task id")
            result = engine.start(args[0])
            print(
                f"Run {result.run_id} started via the new engine; "
                "legacy delegate remains unchanged."
            )
            return 0

        if command == "resume":
            if len(args) > 1:
                raise ActionPlanRefusal("resume accepts at most one run id")
            result = engine.resume(_run_id(engine, args[0] if args else None))
            print(_resume_text(result))
            return 0

        if command == "status":
            run_id, as_json = _parse_optional_json(args)
            identity = _run_id(engine, run_id)
            if as_json:
                print(_json(engine.status_json(identity)))
            else:
                print(engine.status_human(identity))
            return 0

        if command == "watch":
            if len(args) > 1:
                raise ActionPlanRefusal("watch accepts at most one run id")
            identity = _run_id(engine, args[0] if args else None)
            try:
                for frame in engine.watch(identity):
                    print(frame, flush=True)
            except KeyboardInterrupt:
                return 130
            return 0

        if command == "cancel":
            if len(args) != 3 or args[1] != "--reason":
                raise ActionPlanRefusal(
                    "cancel requires <run-id> --reason user-requested")
            try:
                reason = CancelReason(args[2])
            except ValueError as error:
                raise ActionPlanRefusal(
                    "cancel reason must be user-requested") from error
            result = engine.cancel(args[0], reason)
            print(f"Run {result.run_id} cancellation state: {result.state}")
            return 0

        if command == "actions":
            if not args:
                raise ActionPlanRefusal("actions requires next or submit")
            action_command, action_args = args[0], args[1:]
            if action_command == "next":
                if len(action_args) != 2 or action_args[1] != "--json":
                    raise ActionPlanRefusal(
                        "actions next requires <run-id> --json")
                print(_json(engine.actions_next(action_args[0])))
                return 0
            if action_command == "submit":
                if len(action_args) != 3 or action_args[1] != "--file":
                    raise ActionPlanRefusal(
                        "actions submit requires <action-id> --file <result>")
                path = Path(action_args[2])
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise ActionPlanRefusal(
                        f"cannot read canonical result file: {error}") from error
                if not isinstance(payload, dict):
                    raise ActionPlanRefusal("result file must contain one JSON object")
                print(_json(engine.actions_submit(action_args[0], payload)))
                return 0
            raise ActionPlanRefusal(f"unknown actions command {action_command!r}")

        if command == "deliver":
            raise ActionPlanRefusal(
                "run deliver is not implemented in M1-B; delivery policy belongs to M2")
        raise ActionPlanRefusal(f"unknown run command {command!r}")
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as error:  # CLI boundary turns every failure into one typed envelope
        return _failure(error)


__all__ = ["main"]
