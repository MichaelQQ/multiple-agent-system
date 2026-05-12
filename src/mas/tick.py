from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import alert_notifier, audit, board, current_subtask, graph as _graph, state as _state, summary as _summary, transitions, worktree
from .adapters import AdapterUnavailableError, get_adapter
from .config import load_config, project_root, project_dir, validate_config, ConfigWatcher
from .ids import task_id as new_task_id
from .logging import get_task_logger
from .proposals import RejectedProposal, write_rejected_proposal
from .patterns import read_patterns
from .roles import _list_goals, _list_goals_with_meta, find_similar_goal, gather_proposer_signals, goal_similarity, parse_plan, render_prompt
from .schemas import BaseModel, ConfigDict, MasConfig, Plan, ProposalHandoff, Result, Role, StuckDetectionConfig, Task

log = logging.getLogger("mas.tick")


class LockBusy(RuntimeError):
    pass


def _is_task_stuck(task_dir: Path, config: StuckDetectionConfig) -> tuple[bool, str]:
    """Check if a task is stuck based on subtask marker age or idle time.

    Returns (True, reason) if stuck, (False, '') otherwise.
    """
    # Check current_subtask marker
    marker_path = task_dir / ".current_subtask"
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text())
            start_time_iso = marker.get("start_time_iso")
            if start_time_iso:
                elapsed_s = current_subtask._get_elapsed_s(start_time_iso)
                elapsed_h = elapsed_s / 3600
                threshold = config.current_subtask_timeout_hours
                if elapsed_h > threshold:
                    subtask_id = marker.get("subtask_id", "unknown")
                    return (True, f'current subtask {subtask_id} running for {elapsed_h:.1f}h (threshold: {threshold}h)')
                else:
                    # Marker exists and is not expired — task is actively working
                    return (False, '')
        except (json.JSONDecodeError, OSError):
            pass

    # No current subtask marker — check if any subtask has a result
    subtasks_dir = task_dir / "subtasks"
    if subtasks_dir.exists():
        for child_dir in subtasks_dir.iterdir():
            if (child_dir / "result.json").exists():
                return (False, '')

    # No subtask result — check idle time via transitions.log
    transitions_log = task_dir / ".transitions.log"
    if transitions_log.exists():
        try:
            lines = transitions_log.read_text().splitlines()
            if lines:
                first_line = lines[0]
                parts = first_line.split("|", 3)
                if len(parts) >= 1:
                    timestamp_str = parts[0]
                    try:
                        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        elapsed_s = (now - ts).total_seconds()
                        elapsed_h = elapsed_s / 3600
                        threshold = config.task_idle_timeout_hours
                        if elapsed_h > threshold:
                            return (True, f'task idle for {elapsed_h:.1f}h (threshold: {threshold}h)')
                    except ValueError:
                        pass
        except OSError:
            pass

    return (False, '')


def _check_cost_anomalies(env: TickEnv, parent_dir: Path) -> None:
    """Check for cost anomalies and fire alerts."""
    if not env.cfg.alert_webhooks:
        return
    from .cost_helpers import detect_anomalies
    anomalies = detect_anomalies(env.mas)
    for anomaly in anomalies:
        alert_notifier.send_alert(env.cfg.alert_webhooks, {
            "task_id": anomaly["task_id"],
            "event_type": "cost_anomaly",
            "reason": f"cost anomaly: actual={anomaly['actual_cost']}, baseline={anomaly['baseline']}, multiplier={anomaly['multiplier_exceeded']:.1f}x",
            "role": anomaly["role"],
            "cost": anomaly["actual_cost"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


class TickEnv(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    repo: Path
    mas: Path
    cfg: MasConfig
    paused: bool = False
    dry_run_child: bool = False


def _write_heartbeat(mas: Path) -> None:
    """Write current UTC time as ISO8601 to .mas/tick_heartbeat."""
    p = mas / "tick_heartbeat"
    p.write_text(datetime.now(timezone.utc).isoformat())


def _acquire_lock(mas_dir: Path):
    mas_dir.mkdir(parents=True, exist_ok=True)
    lock_path = mas_dir / "tick.lock"
    fh = lock_path.open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise LockBusy("another tick is running")
        raise
    return fh


def run_tick(
    *,
    start: Path | None = None,
    cfg: "MasConfig" | None = None,
    dry_run_child: bool = False,
) -> None:
    project_root_path = project_root(start)
    mas = project_dir(start)
    if start is not None and (start / ".git").is_dir():
        repo = start
    else:
        repo = project_root_path
    if cfg is None:
        cfg = load_config(mas)

    issues = validate_config(cfg, mas)
    if issues:
        issue_msgs = "; ".join(f"{i.field}: {i.message}" for i in issues)
        log.error("validation failed: %s", issue_msgs)
        raise ValueError(f"Validation failed: {issue_msgs}")

    paused = (mas / "PAUSED").exists()
    env = TickEnv(repo=repo, mas=mas, cfg=cfg, paused=paused, dry_run_child=dry_run_child)
    board.ensure_layout(mas)

    try:
        lock = _acquire_lock(mas)
    except LockBusy:
        log.info("tick skipped: another tick holds the lock")
        return

    try:
        _reap_workers(env)
        _advance_doing(env)
        if paused:
            log.info("paused (.mas/PAUSED present), skipping dispatch")
        else:
            _maybe_dispatch_proposer(env)
        from . import patterns as _patterns
        _patterns.refresh(env.mas)
        _patterns.success_refresh(env.mas)
    finally:
        lock.close()
    # Write heartbeat after lock release — keep tick fast.
    _write_heartbeat(env.mas)


_GRACE_AFTER_SIGTERM_S = 5.0


def _reap_workers(env: TickEnv) -> None:
    """Clear dead PIDs and enforce per-role wall-clock timeouts.

    For each live worker PID whose elapsed dispatch time exceeds
    `roles[<role>].timeout_s`, send SIGTERM, wait a short grace, then SIGKILL
    if still alive, and synthesize a `failure` result so the normal retry /
    fail-parent path in `_handle_child_result` takes over.

    Legacy single-line pidfiles (no dispatch timestamp) are skipped for
    timeout purposes — their age is unknown and we can't safely guess.
    """
    import signal
    import time as _time

    board.count_active_pids(env.mas)

    for pid_file in (env.mas / "tasks" / "doing").glob("**/pids/*.pid"):
        entry = board.read_pid_entry(pid_file)
        if entry is None:
            continue
        pid, dispatch_time = entry
        if dispatch_time is None:
            # Legacy pidfile: no timestamp → unknown age, skip.
            continue
        if not _pid_alive(pid):
            continue

        role = pid_file.name.split(".", 1)[0]
        role_cfg = env.cfg.roles.get(role)
        if role_cfg is None:
            continue

        elapsed = _time.time() - dispatch_time
        if elapsed <= role_cfg.timeout_s:
            continue

        task_dir = pid_file.parent.parent
        get_task_logger(log, task_id=task_dir.name, component="reaper").warning(
            "worker %s exceeded timeout_s=%s (elapsed=%.0fs), sending SIGTERM",
            role, role_cfg.timeout_s, elapsed,
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

        deadline = _time.time() + _GRACE_AFTER_SIGTERM_S
        while _time.time() < deadline:
            if not _pid_alive(pid):
                break
            _time.sleep(0.1)
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        pid_file.unlink(missing_ok=True)
        _synthesize_timeout_result(task_dir, role, int(role_cfg.timeout_s))


def _synthesize_timeout_result(task_dir: Path, role: str, timeout_s: int) -> None:
    if (task_dir / "result.json").exists():
        return
    attempt = _read_attempt(task_dir / ".attempt")
    tail = _read_log_tail(task_dir, role, attempt)
    try:
        task_id = board.read_task(task_dir).id
    except Exception:
        task_id = task_dir.name
    result = Result(
        task_id=task_id,
        status="failure",
        summary=f"timeout exceeded after {timeout_s}s",
        feedback=(f"log tail:\n{tail}" if tail else None),
        duration_s=float(timeout_s),
    )
    (task_dir / "result.json").write_text(result.model_dump_json(indent=2))
    transitions.log_transition(task_dir, "dispatched", "timeout", "worker exceeded role.timeout_s")


# --- 2. Advance -------------------------------------------------------------


def _advance_doing(env: TickEnv) -> None:
    for task_dir_ in board.list_column(env.mas, "doing"):
        try:
            _advance_one(env, task_dir_)
        except Exception:
            task_id = task_dir_.name
            get_task_logger(log, task_id=task_id, component="tick").exception("advance failed")


def _advance_one(env: TickEnv, parent_dir: Path) -> None:
    parent_task = board.read_task(parent_dir)

    # Proposer runs transiently in doing/. Archive on terminal state; also
    # handle the case where the worker exited without writing result.json
    # (crash, usage-limit, etc.) so it doesn't sit in doing/ forever.
    if parent_task.role == "proposer":
        result = board.read_result(parent_dir)
        if result is None and _worker_orphaned(parent_dir, "proposer", parent_task.attempt):
            failed_dir = env.mas / "tasks" / "failed" / parent_task.id
            board.move(parent_dir, failed_dir, reason="orphan_detected")
            return
        if result is not None:
            col = "done" if result.status == "success" else "failed"
            if result.status == "success":
                _materialize_proposal(env, result)
            board.move(parent_dir, env.mas / "tasks" / col / parent_task.id,
                       reason="role_success" if result.status == "success" else "role_failed")
            return
        # Never dispatched (no log file, no result) — dispatch now.
        log_path = parent_dir / "logs" / f"proposer-{parent_task.attempt}.log"
        if not log_path.exists() and not env.paused:
            _dispatch_role(env, parent_task, parent_dir, parent_dir, role="proposer")
        return

    # Stuck-task detection: check before proceeding with normal advancement.
    stuck, reason = _is_task_stuck(parent_dir, env.cfg.stuck_detection)
    if stuck:
        get_task_logger(log, task_id=parent_task.id, component="tick").warning(
            "task stuck: %s", reason
        )
        parent_task.stuck = True
        board.write_task(parent_dir, parent_task)
        if env.cfg.alert_webhooks:
            alert_notifier.send_alert(env.cfg.alert_webhooks, {
                "task_id": parent_task.id,
                "event_type": "hung_subtask",
                "reason": reason,
                "role": parent_task.role,
                "cost": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    plan_path = parent_dir / "plan.json"
    wt = parent_dir / "worktree"

    if not wt.exists():
        worktree.create(env.repo, parent_task.id, wt)

    if not plan_path.exists():
        pid_dir = parent_dir / "pids"
        if _role_running(pid_dir, "orchestrator"):
            return
        # Non-agentic orchestrator: materialize plan.json from result.handoff.
        result = board.read_result(parent_dir)
        if result is not None and result.status == "success" and result.handoff:
            if _materialize_plan(parent_dir, result):
                return  # plan.json written; next tick will parse it
        orch_attempt = _read_attempt(parent_dir / ".orchestrator_attempt")
        if _worker_orphaned(parent_dir, "orchestrator", orch_attempt):
            _retry_or_fail_orchestrator(env, parent_dir, parent_task, orch_attempt)
            return
        parent_task.attempt = orch_attempt
        if not env.paused:
            _dispatch_role(env, parent_task, parent_dir, wt, role="orchestrator")
        return

    plan = parse_plan(plan_path, parent_task.id)
    if _expand_evaluator_quorum(plan, env.cfg):
        plan_path.write_text(plan.model_dump_json(indent=2))
    subtasks_root = parent_dir / "subtasks"
    subtasks_root.mkdir(exist_ok=True)

    graph = _graph.read_graph(parent_dir)
    graph_changed = _graph.sync_from_plan(graph, plan)
    graph_changed |= _backfill_graph_from_disk(graph, plan, subtasks_root)
    if graph_changed:
        _graph.write_graph(parent_dir, graph)

    # Sequential: find first subtask without successful result.
    next_child = _next_ready_child(plan, subtasks_root)
    if next_child is None:
        if _all_children_passed(plan, subtasks_root):
            _finalize_parent(env, parent_dir, parent_task)
        return

    budget_exceeded, spent_usd, budget_usd, last_done_id = _check_cost_budget(
        env, parent_dir, parent_task, plan, subtasks_root
    )
    if budget_exceeded:
        parent_result = Result(
            task_id=parent_task.id,
            status="failure",
            summary="cost budget exceeded",
            handoff={
                "spent_usd": spent_usd,
                "budget_usd": budget_usd,
                "last_completed_subtask_id": last_done_id,
            },
            duration_s=0.0,
        )
        (parent_dir / "result.json").write_text(parent_result.model_dump_json(indent=2))
        failed_dir = env.mas / "tasks" / "failed" / parent_task.id
        board.move(parent_dir, failed_dir, reason="cost_budget_exceeded")
        return

    child_dir = subtasks_root / next_child.id
    child_dir.mkdir(parents=True, exist_ok=True)

    if _role_running(child_dir / "pids", next_child.role):
        return

    child_attempt = _read_attempt(child_dir / ".attempt")

    result = board.read_result(child_dir)
    # Orphan: worker previously dispatched (log for current attempt exists) but
    # left no result.json and no live pid. Synthesize a failure so the retry /
    # fail-parent path in _handle_child_result takes over.
    if result is None and _worker_orphaned(child_dir, next_child.role, child_attempt):
        result = _synthesize_orphan_result(child_dir, next_child.id, next_child.role, child_attempt)

    if result is not None:
        current_subtask._delete_current_subtask_marker(parent_dir)
        from . import verify as _verify
        dry_run_active = env.dry_run_child and next_child.role in ("implementer", "tester")
        result = _verify.verify_child_result(
            next_child, result, child_dir, child_attempt, dry_run=dry_run_active
        )
        if dry_run_active:
            result = _verify.apply_proposed_diff(next_child, result, wt, child_dir)
        result = _verify.verify_allowed_paths(next_child, result, wt, child_dir)
        if next_child.role == "evaluator":
            result = _verify.verify_evaluator_result(next_child, result, wt)
        if next_child.role == "implementer":
            test_cmd = _resolve_test_command(plan, next_child.id, subtasks_root, result)
            result = _verify.verify_implementer_test_rerun(next_child, result, wt, test_cmd)
        _state.update_state_from_result(
            parent_dir, next_child, result, worktree=wt, attempt=child_attempt
        )
        _graph.update_node_from_result(graph, next_child, result)
        _graph.write_graph(parent_dir, graph)
        _summary.maybe_write_summary(parent_dir, parent_task.goal)
        _handle_child_result(env, parent_dir, parent_task, plan, next_child, result)
        return

    if env.paused:
        return

    resolved_inputs = _resolve_feedback_ref(next_child.inputs, plan)
    state_obj = _state.read_state(parent_dir)
    state_dump = state_obj.model_dump(exclude_defaults=True, exclude_none=True)
    if state_dump:
        resolved_inputs = {**resolved_inputs, "state": state_dump}

    child_task = Task(
        id=new_task_id(next_child.goal, salt=next_child.id),
        parent_id=parent_task.id,
        role=next_child.role,
        goal=next_child.goal,
        inputs=resolved_inputs,
        constraints=next_child.constraints,
        prior_results=_collect_prior_results(
            plan, next_child.id, subtasks_root, parent_dir=parent_dir
        ),
        cycle=parent_task.cycle,
        attempt=child_attempt,
    )
    if next_child.constraints.get("allowed_paths"):
        from . import verify as _verify
        _verify.capture_worktree_baseline(wt, child_dir)
    pid = _dispatch_role(env, child_task, child_dir, wt, role=next_child.role)
    current_subtask._write_current_subtask_marker(
        parent_dir,
        role=next_child.role,
        provider=env.cfg.roles[next_child.role].provider,
        pid=pid or 0,
        subtask_id=next_child.id,
    )
    audit.append_event(
        parent_dir,
        event="dispatch",
        task_id=parent_task.id,
        role=next_child.role,
        provider=env.cfg.roles[next_child.role].provider,
        subtask_id=next_child.id,
        summary=f"dispatched {next_child.role}",
    )
    _check_cost_anomalies(env, parent_dir)


def _resolve_test_command(
    plan: Plan, current_id: str, subtasks_root: Path, result: Result
) -> str | None:
    """Find the test_command for an implementer re-run: prefer the implementer's
    own handoff, else walk back through prior subtasks for the most recent
    tester's declared test_command."""
    h = result.handoff or {}
    if isinstance(h.get("test_command"), str) and h["test_command"]:
        return h["test_command"]
    priors: list = []
    for spec in plan.subtasks:
        if spec.id == current_id:
            break
        priors.append(spec)
    for spec in reversed(priors):
        if spec.role != "tester":
            continue
        r = board.read_result(subtasks_root / spec.id)
        if r is None or r.handoff is None:
            continue
        cmd = r.handoff.get("test_command")
        if isinstance(cmd, str) and cmd:
            return cmd
    return None


def _backfill_graph_from_disk(graph, plan: Plan, subtasks_root: Path) -> bool:
    """For any graph node with status=None whose subtask has a result.json on
    disk, fold that result into the graph. Covers two cases: parents that
    pre-date graph.json and races where a child completed in a tick before
    update_node_from_result fires (e.g. when sync runs first on a fresh
    plan)."""
    changed = False
    by_id = {n.subtask_id: n for n in graph.nodes}
    for spec in plan.subtasks:
        node = by_id.get(spec.id)
        if node is None or node.status is not None:
            continue
        r = board.read_result(subtasks_root / spec.id)
        if r is None:
            continue
        if _graph.update_node_from_result(graph, spec, r):
            changed = True
    return changed


def _collect_prior_results(
    plan: Plan,
    current_id: str,
    subtasks_root: Path,
    *,
    parent_dir: Path | None = None,
) -> list[Result]:
    """Return prior results sliced to entries relevant to the current subtask.

    Sources nodes from `parent_dir/graph.json` (causality-annotated). Falls
    back to walking plan.subtasks + reading each result.json when the graph
    isn't available — this keeps tests that bypass the graph build path
    (constructing a Plan directly without a tick) working.
    """
    from .roles import extract_filename_refs, retrieval_slice

    current_spec = next((s for s in plan.subtasks if s.id == current_id), None)

    if parent_dir is None:
        parent_dir = subtasks_root.parent

    graph = _graph.read_graph(parent_dir)
    if graph.nodes:
        candidates = _graph.derive_prior_results(graph, plan, current_id)
    else:
        candidates = []
        for spec in plan.subtasks:
            if spec.id == current_id:
                break
            r = board.read_result(subtasks_root / spec.id)
            if r is not None:
                candidates.append((spec.role, r))

    if current_spec is None:
        return [r for _, r in candidates]

    fnames = extract_filename_refs(current_spec.inputs)
    return retrieval_slice(
        candidates, current_role=current_spec.role, current_filenames=fnames
    )


def _read_attempt(path: Path) -> int:
    if not path.exists():
        return 1
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return 1


def _worker_orphaned(task_dir: Path, role: str, attempt: int) -> bool:
    """A worker for (role, attempt) was dispatched but is no longer running
    and left no result. Identified by the presence of its per-attempt log
    without a live pid. Caller must ensure no result.json exists."""
    log_path = task_dir / "logs" / f"{role}-{attempt}.log"
    if not log_path.exists():
        return False
    return not _role_running(task_dir / "pids", role)


def _read_log_tail(task_dir: Path, role: str, attempt: int, lines: int = 20) -> str:
    p = task_dir / "logs" / f"{role}-{attempt}.log"
    try:
        text = p.read_text()
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


_ENV_ORPHAN_MARKERS = (
    "blocked by the sandbox",
    "sandbox restrictions",
    "requires explicit user approval",
    "blocked as sensitive",
    "permission denied",
)


def _synthesize_orphan_result(task_dir: Path, task_id: str, role: str, attempt: int) -> Result:
    tail = _read_log_tail(task_dir, role, attempt)
    lower = tail.lower()
    is_env = any(m in lower for m in _ENV_ORPHAN_MARKERS)
    r = Result(
        task_id=task_id,
        status="environment_error" if is_env else "failure",
        summary=f"{role} exited without writing result.json" + (" (environment error)" if is_env else ""),
        feedback=(f"log tail:\n{tail}" if tail else None),
        duration_s=0.0,
    )
    (task_dir / "result.json").write_text(r.model_dump_json(indent=2))
    return r


def _retry_or_fail_orchestrator(env: TickEnv, parent_dir: Path, parent_task: Task, attempt: int) -> None:
    role_cfg = env.cfg.roles["orchestrator"]
    if attempt < (role_cfg.max_retries + 1):
        (parent_dir / ".orchestrator_attempt").write_text(str(attempt + 1))
        tail = _read_log_tail(parent_dir, "orchestrator", attempt)
        (parent_dir / ".previous_failure").write_text(
            "orchestrator exited without plan.json" + (f"\n{tail}" if tail else "")
        )
        return
    board.move(parent_dir, env.mas / "tasks" / "failed" / parent_task.id, reason="max_retries_exceeded")


def _role_running(pid_dir: Path, role: str) -> bool:
    if not pid_dir.exists():
        return False
    for p in pid_dir.glob(f"{role}.*.pid"):
        entry = board.read_pid_entry(p)
        if entry is None:
            p.unlink(missing_ok=True)
            continue
        pid, _ = entry
        if _pid_alive(pid):
            return True
        p.unlink(missing_ok=True)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    # os.kill(pid, 0) succeeds for zombie processes too; check actual state.
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            capture_output=True, text=True, timeout=2,
        )
        state = r.stdout.strip()
        return bool(state) and state != "Z"
    except Exception:
        return True


def _next_ready_child(plan: Plan, subtasks_root: Path):
    for i, spec in enumerate(plan.subtasks):
        child_dir = subtasks_root / spec.id
        result = board.read_result(child_dir) if child_dir.exists() else None
        if result is None:
            return spec
        if result.status == "success" and (
            spec.role not in ("evaluator", "arbiter") or result.verdict == "pass"
        ):
            continue
        # Evaluator that returned needs_revision is "handled" once a later
        # revision cycle (or arbiter) has been appended; otherwise tick loops
        # on the same evaluator forever.
        if (spec.role == "evaluator"
                and (result.verdict == "needs_revision" or result.status == "needs_revision")
                and i + 1 < len(plan.subtasks)):
            continue
        # Found one that needs attention (failure / unresolved needs_revision)
        return spec
    return None


def _all_children_passed(plan: Plan, subtasks_root: Path) -> bool:
    for i, spec in enumerate(plan.subtasks):
        result = board.read_result(subtasks_root / spec.id)
        if result is None:
            return False
        # A needs_revision evaluator is superseded once its revision cycle
        # has been spawned (there is a later subtask); the next evaluator is
        # what counts. This applies to both status=needs_revision on the
        # Result and verdict=needs_revision.
        superseded_eval = (
            spec.role == "evaluator"
            and (result.status == "needs_revision" or result.verdict == "needs_revision")
            and i + 1 < len(plan.subtasks)
        )
        if superseded_eval:
            continue
        if result.status != "success":
            return False
        if spec.role == "evaluator" and result.verdict != "pass":
            return False
        if spec.role == "arbiter" and result.verdict != "pass":
            return False
    return True


def _handle_child_result(env, parent_dir, parent_task, plan, spec, result):
    audit.append_event(
        parent_dir,
        event="completion",
        task_id=parent_task.id,
        role=spec.role,
        provider=env.cfg.roles[spec.role].provider,
        subtask_id=spec.id,
        status=result.status,
        duration_s=result.duration_s,
        summary=result.summary,
    )

    # Evaluator quorum: defer until all sibling members complete, then
    # replace `result` with the merged consensus so the existing pass /
    # needs_revision branches see the quorum's collective verdict.
    if spec.role == "evaluator" and _quorum_base_id(spec.id) is not None:
        merged = _aggregate_quorum_result(plan, parent_dir, spec)
        if merged is None:
            return
        result = merged

    child_dir = parent_dir / "subtasks" / spec.id
    txns = transitions.read_transitions(child_dir, limit=3)
    if txns:
        txn_str = " | ".join(f"{txn.from_state}→{txn.to_state}({txn.reason})" for txn in txns)
        result.feedback = (result.feedback or "") + (f"\n[transition history: {txn_str}]" if result.feedback else f"[transition history: {txn_str}]")

    # Success path: mark and move on (by next tick). Evaluator/arbiter must
    # also return verdict=pass — verdict=fail/needs_revision routes below.
    if result.status == "success" and (
        spec.role not in ("evaluator", "arbiter") or result.verdict == "pass"
    ):
        return

    # Evaluator verdict handling
    if spec.role == "evaluator" and result.verdict == "needs_revision":
        feedback = result.feedback or ""
        if _should_dispatch_arbiter(env, plan, parent_dir):
            disputes = _latest_implementer_disputes(plan, parent_dir)
            _append_arbiter_subtask(parent_dir, plan, parent_task, feedback=feedback, disputes=disputes)
            return
        converged, sim = _detect_convergence(plan, feedback)
        if converged:
            reason = f"convergence_detected jaccard={sim:.2f}"
            if _read_replan_count(parent_dir) < env.cfg.max_replans:
                _trigger_replan(env, parent_dir, parent_task, reason=f"{reason}: {feedback}")
                return
            failed_dir = env.mas / "tasks" / "failed" / parent_task.id
            board.move(parent_dir, failed_dir, reason=reason)
            return
        if _should_trigger_replan(plan, parent_dir, env.cfg.max_replans):
            _trigger_replan(env, parent_dir, parent_task, reason=feedback)
            return
        appended = _append_revision_cycle(
            parent_dir, plan, parent_task, feedback=feedback, cfg=env.cfg
        )
        if not appended:
            failed_dir = env.mas / "tasks" / "failed" / parent_task.id
            board.move(parent_dir, failed_dir, reason="revision_cycles_exhausted")
        return

    # Arbiter verdict handling: binding pass/fail. Pass treated as parent
    # acceptance — falls through so _all_children_passed will finalize.
    # Fail moves parent straight to failed/ regardless of remaining cycles.
    if spec.role == "arbiter" and result.status == "success":
        if result.verdict == "pass":
            return
        if result.verdict == "fail":
            failed_dir = env.mas / "tasks" / "failed" / parent_task.id
            board.move(parent_dir, failed_dir, reason="arbiter_verdict_fail")
            return
        # verdict == needs_revision (or missing) → treat as inconclusive failure
        failed_dir = env.mas / "tasks" / "failed" / parent_task.id
        board.move(parent_dir, failed_dir, reason="arbiter_verdict_inconclusive")
        return

    child_dir = parent_dir / "subtasks" / spec.id
    attempts_path = (child_dir / ".attempt").resolve()
    attempt = int(attempts_path.read_text()) if attempts_path.exists() else 1

    # environment_error: retry without consuming the role's retry budget, but
    # cap total env retries to prevent infinite loops.
    if result.status == "environment_error":
        env_path = child_dir / ".env_retries"
        env_n = int(env_path.read_text()) if env_path.exists() else 0
        ENV_RETRY_CAP = 3
        if env_n < ENV_RETRY_CAP:
            env_path.write_text(str(env_n + 1))
            # Rename result so the same attempt slot is redispatched with a
            # fresh log file; keep the same attempt counter.
            (child_dir / "result.json").rename(child_dir / f"result.env-{env_n + 1}.json")
            # Rotate the log so the next dispatch lands in role-{attempt}.log cleanly.
            stale_log = child_dir / "logs" / f"{spec.role}-{attempt}.log"
            if stale_log.exists():
                stale_log.rename(child_dir / "logs" / f"{spec.role}-{attempt}.env-{env_n + 1}.log")
            (child_dir / ".previous_failure").write_text(
                f"[environment_error] {result.summary}\n" + (result.feedback or "")
            )
            return
        # Env retries exhausted → fall through to failure handling below.

    # Failure: retry up to max_retries
    role_cfg = env.cfg.roles[spec.role]
    if attempt < (role_cfg.max_retries + 1):
        # Bump attempt and clear result so next tick redispatches with previous_failure.
        attempts_path.write_text(str(attempt + 1))
        (child_dir / "result.json").rename(child_dir / f"result.failed-{attempt}.json")
        # Remove any stale result.json the worker may have written inside the worktree.
        stale = parent_dir / "worktree" / "result.json"
        if stale.exists():
            stale.unlink()
        # Write a marker so the next dispatch picks it up as previous_failure.
        (child_dir / ".previous_failure").write_text(result.summary + ("\n" + (result.feedback or "")))
        return

    # Retries exhausted → move parent to failed/
    failed_dir = env.mas / "tasks" / "failed" / parent_task.id
    board.move(parent_dir, failed_dir, reason="max_retries_exceeded")


CONVERGENCE_THRESHOLD = 0.85


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _jaccard_similarity(a: str, b: str) -> float:
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _detect_convergence(plan: Plan, current_feedback: str) -> tuple[bool, float]:
    """Compare current evaluator feedback to the most recent prior cycle's.

    Returns (converged, similarity). Converged when similarity >= threshold,
    signalling the loop is repeating itself rather than making progress.
    """
    if not plan.revision_feedback:
        return False, 0.0
    last_key = max(plan.revision_feedback.keys(), key=lambda k: int(k.split("-")[1]))
    sim = _jaccard_similarity(current_feedback, plan.revision_feedback[last_key])
    return sim >= CONVERGENCE_THRESHOLD, sim


def _read_replan_count(parent_dir: Path) -> int:
    p = parent_dir / ".replan_count"
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return 0


def _should_trigger_replan(plan: Plan, parent_dir: Path, max_replans: int) -> bool:
    """True when next revision should be replaced by re-dispatching the orchestrator.

    Fires when at least one revision cycle has run AND we'd be about to add the
    final allowed cycle (existing >= max_revision_cycles - 1), bounded by
    `max_replans`. The first failing eval still gets a normal revision cycle.
    """
    if max_replans <= 0:
        return False
    if _read_replan_count(parent_dir) >= max_replans:
        return False
    existing = len({s.id.split("-", 2)[1] for s in plan.subtasks if s.id.startswith("rev-")})
    if existing < 1:
        return False
    return existing >= plan.max_revision_cycles - 1


def _trigger_replan(env: TickEnv, parent_dir: Path, parent_task: Task, reason: str) -> None:
    """Re-dispatch orchestrator with `inputs.replan_reason` set.

    Archives the current plan and subtasks under `*.replan-{N}/`, clears the
    parent's stale orchestrator result, bumps `.orchestrator_attempt` so the
    orphan detector sees a fresh slot, and writes the updated parent task.json
    so the next tick re-enters the orchestrator code path.
    """
    n = _read_replan_count(parent_dir) + 1
    (parent_dir / ".replan_count").write_text(str(n))

    plan_path = parent_dir / "plan.json"
    if plan_path.exists():
        plan_path.rename(parent_dir / f"plan.replan-{n}.json")
    subtasks_dir = parent_dir / "subtasks"
    if subtasks_dir.exists():
        subtasks_dir.rename(parent_dir / f"subtasks.replan-{n}")
    graph_p = _graph.graph_path(parent_dir)
    if graph_p.exists():
        graph_p.rename(parent_dir / f"graph.replan-{n}.json")
    parent_result = parent_dir / "result.json"
    if parent_result.exists():
        parent_result.rename(parent_dir / f"result.replan-{n}.json")
    current_subtask._delete_current_subtask_marker(parent_dir)

    orch_attempt = _read_attempt(parent_dir / ".orchestrator_attempt")
    (parent_dir / ".orchestrator_attempt").write_text(str(orch_attempt + 1))

    parent_task.inputs = {**parent_task.inputs, "replan_reason": reason}
    board.write_task(parent_dir, parent_task)

    transitions.log_transition(
        parent_dir, "needs_revision", "replanning",
        f"replan_{n}_triggered: max cycles approached"
    )
    audit.append_event(
        parent_dir,
        event="replan",
        task_id=parent_task.id,
        role="orchestrator",
        provider=env.cfg.roles["orchestrator"].provider,
        summary=f"replan {n}: re-dispatching orchestrator",
    )


def _latest_implementer_disputes(plan: Plan, parent_dir: Path) -> list[dict]:
    """Read the most-recent implementer subtask's handoff and return its
    `disputes` list (each entry a `{evaluator_claim, implementer_response}`
    dict). Returns [] when no implementer has run yet, the handoff is
    malformed, or no disputes were recorded."""
    subtasks_root = parent_dir / "subtasks"
    for spec in reversed(plan.subtasks):
        if spec.role != "implementer":
            continue
        r = board.read_result(subtasks_root / spec.id)
        if r is None or r.handoff is None:
            return []
        raw = r.handoff.get("disputes")
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            claim = entry.get("evaluator_claim")
            response = entry.get("implementer_response")
            if isinstance(claim, str) and isinstance(response, str) and claim and response:
                out.append({"evaluator_claim": claim, "implementer_response": response})
        return out
    return []


def _should_dispatch_arbiter(env: TickEnv, plan: Plan, parent_dir: Path) -> bool:
    """True when (1) `arbiter` is configured as a role, (2) at least one
    revision cycle already ran, (3) no arbiter has been dispatched yet for
    this plan, and (4) the latest implementer raised non-empty disputes."""
    if "arbiter" not in env.cfg.roles:
        return False
    existing_cycles = len({s.id.split("-", 2)[1] for s in plan.subtasks if s.id.startswith("rev-")})
    if existing_cycles < 1:
        return False
    if any(s.role == "arbiter" for s in plan.subtasks):
        return False
    return bool(_latest_implementer_disputes(plan, parent_dir))


def _append_arbiter_subtask(
    parent_dir: Path,
    plan: Plan,
    parent_task,
    *,
    feedback: str,
    disputes: list[dict],
) -> None:
    """Append a single arbiter subtask to the plan. The arbiter receives the
    last evaluator's feedback and the implementer's disputes; its verdict is
    binding (pass → parent finalizes; fail → parent moves to failed/)."""
    from .schemas import SubtaskSpec

    spec = SubtaskSpec(
        id="arbiter-1",
        role="arbiter",
        goal="Resolve evaluator/implementer disagreement and emit binding verdict",
        inputs={
            "evaluator_feedback": feedback,
            "disputes": disputes,
            "parent_goal": parent_task.goal,
        },
    )
    failing_eval_id = next(
        (s.id for s in reversed(plan.subtasks) if s.role == "evaluator"),
        None,
    )
    plan.subtasks.append(spec)
    (parent_dir / "plan.json").write_text(plan.model_dump_json(indent=2))

    g = _graph.read_graph(parent_dir)
    _graph.sync_from_plan(g, plan)
    if failing_eval_id is not None:
        _graph.add_arbiter_link(
            g, from_evaluator_id=failing_eval_id, arbiter_id=spec.id, feedback=feedback
        )
    _graph.write_graph(parent_dir, g)


def _append_revision_cycle(
    parent_dir: Path, plan: Plan, parent_task, feedback: str, *, cfg: MasConfig | None = None
) -> bool:
    """Returns True if a new cycle was appended, False if max_revision_cycles was reached."""
    from .schemas import SubtaskSpec

    # Bound by plan.max_revision_cycles (count distinct cycles, not subtasks)
    existing_cycles = len({s.id.split("-", 2)[1] for s in plan.subtasks if s.id.startswith("rev-")})
    if existing_cycles >= plan.max_revision_cycles:
        return False

    cycle_n = existing_cycles + 1
    cycle_key = f"rev-{cycle_n}"
    plan.revision_feedback[cycle_key] = feedback
    ref_inputs = {"feedback_cycle": cycle_key, "parent_goal": parent_task.goal}
    new_children = [
        SubtaskSpec(id=f"{cycle_key}-tester", role="tester",
                    goal=f"Augment tests to cover evaluator feedback (cycle {cycle_n})", inputs=ref_inputs),
        SubtaskSpec(id=f"{cycle_key}-implementer", role="implementer",
                    goal=f"Address evaluator feedback and make tests pass (cycle {cycle_n})", inputs=ref_inputs),
        SubtaskSpec(id=f"{cycle_key}-evaluator", role="evaluator",
                    goal=f"Evaluate revision {cycle_n}", inputs=ref_inputs),
    ]
    # The evaluator that triggered this revision is the most recent
    # evaluator-or-quorum subtask preceding the new cycle.
    failing_eval_id = next(
        (s.id for s in reversed(plan.subtasks) if s.role == "evaluator"),
        None,
    )

    plan.subtasks.extend(new_children)
    if cfg is not None:
        _expand_evaluator_quorum(plan, cfg)
    (parent_dir / "plan.json").write_text(plan.model_dump_json(indent=2))

    g = _graph.read_graph(parent_dir)
    _graph.sync_from_plan(g, plan)
    if failing_eval_id is not None:
        _graph.add_revision_link(
            g,
            from_evaluator_id=failing_eval_id,
            new_subtask_ids=[c.id for c in new_children],
            feedback=feedback,
        )
    _graph.write_graph(parent_dir, g)
    return True


_QUORUM_ID_RE = re.compile(r"^(.+)-q(\d+)$")


def _quorum_base_id(spec_id: str) -> tuple[str, int] | None:
    """If `spec_id` matches the quorum pattern `<base>-q<N>`, return
    (base_id, N). Otherwise None."""
    m = _QUORUM_ID_RE.match(spec_id)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _quorum_siblings(plan: Plan, base_id: str):
    """Return all SubtaskSpec entries that are quorum members of base_id,
    in plan order."""
    out = []
    for s in plan.subtasks:
        match = _quorum_base_id(s.id)
        if match is not None and match[0] == base_id:
            out.append(s)
    return out


def _expand_evaluator_quorum(plan: Plan, cfg: MasConfig) -> bool:
    """Expand single evaluator subtasks into N quorum siblings when
    `roles.evaluator.quorum > 1`. Idempotent — already-expanded specs
    (id matches `<base>-q<N>`) are left alone.

    Returns True if any expansion took place."""
    from .schemas import SubtaskSpec  # noqa: F401  (model_copy uses it via plan)

    eval_role = cfg.roles.get("evaluator")
    if eval_role is None:
        return False
    quorum = getattr(eval_role, "quorum", 1)
    if quorum < 2:
        return False

    new_subtasks = []
    changed = False
    for spec in plan.subtasks:
        if spec.role != "evaluator" or _quorum_base_id(spec.id) is not None:
            new_subtasks.append(spec)
            continue
        for i in range(1, quorum + 1):
            new_subtasks.append(spec.model_copy(update={"id": f"{spec.id}-q{i}"}))
        changed = True

    if changed:
        plan.subtasks[:] = new_subtasks
    return changed


def _aggregate_quorum_result(plan: Plan, parent_dir: Path, spec) -> Result | None:
    """If `spec` is an evaluator quorum member, look up its siblings and
    return a merged consensus Result, or None if any sibling result is
    still missing (defer until all members complete).

    Consensus rule: every sibling must be status=success and verdict=pass
    for the merged result to be a pass; otherwise the merged result is
    needs_revision with each sibling's feedback concatenated."""
    if spec.role != "evaluator":
        return None
    match = _quorum_base_id(spec.id)
    if match is None:
        return None
    base_id, _idx = match
    siblings = _quorum_siblings(plan, base_id)
    if len(siblings) < 2:
        return None
    subtasks_root = parent_dir / "subtasks"
    sibling_results = [board.read_result(subtasks_root / s.id) for s in siblings]
    if any(r is None for r in sibling_results):
        return None

    all_pass = all(
        r.status == "success" and r.verdict == "pass" for r in sibling_results
    )
    last = sibling_results[-1]
    if all_pass:
        return last.model_copy(update={
            "summary": f"[quorum:pass {len(siblings)}/{len(siblings)}] {last.summary}",
        })
    fb_parts: list[str] = []
    for s, r in zip(siblings, sibling_results):
        verdict = r.verdict or r.status
        body = (r.feedback or "").strip()
        fb_parts.append(f"[{s.id} verdict={verdict}] {body}".rstrip())
    merged_feedback = "\n\n".join(fb_parts)
    return last.model_copy(update={
        "status": "needs_revision",
        "verdict": "needs_revision",
        "summary": f"[quorum:dissent {len(siblings)} members] no unanimous pass",
        "feedback": merged_feedback,
    })


def _resolve_feedback_ref(spec_inputs: dict, plan: Plan) -> dict:
    """Resolve `feedback_cycle` → `feedback` using plan.revision_feedback.
    Returns a new dict (does not mutate spec_inputs)."""
    if "feedback_cycle" not in spec_inputs:
        return spec_inputs
    cycle_key = spec_inputs["feedback_cycle"]
    feedback = plan.revision_feedback.get(cycle_key, "")
    resolved = {k: v for k, v in spec_inputs.items() if k != "feedback_cycle"}
    resolved["feedback"] = feedback
    return resolved


def _aggregate_child_costs(parent_dir: Path, plan) -> tuple[int, int, float]:
    total_in = 0
    total_out = 0
    total_cost = 0.0
    if plan is None:
        return total_in, total_out, total_cost
    subtasks_dir = parent_dir / "subtasks"
    for spec in plan.subtasks:
        r = board.read_result(subtasks_dir / spec.id) if subtasks_dir.exists() else None
        if r is None:
            continue
        total_in += r.tokens_in or 0
        total_out += r.tokens_out or 0
        total_cost += r.cost_usd or 0.0
    return total_in, total_out, total_cost


def _check_cost_budget(
    env: TickEnv,
    parent_dir: Path,
    parent_task: Task,
    plan: "Plan",
    subtasks_root: Path,
) -> tuple[bool, float, "float | None", "str | None"]:
    budget = getattr(parent_task, "cost_budget_usd", None)
    if budget is None:
        budget = getattr(env.cfg, "default_cost_budget_usd", None)
    if budget is None:
        return False, 0.0, None, None
    spent = 0.0
    last_done_id: str | None = None
    for spec in plan.subtasks:
        r = board.read_result(subtasks_root / spec.id) if subtasks_root.exists() else None
        if r is not None:
            spent += r.cost_usd or 0.0
            last_done_id = spec.id
    return spent >= budget, spent, budget, last_done_id


def _finalize_parent(env: TickEnv, parent_dir: Path, parent_task) -> None:
    current_subtask._delete_current_subtask_marker(parent_dir)
    wt = parent_dir / "worktree"
    if wt.exists():
        worktree.commit_changes(wt, parent_task.goal)

    plan_path = parent_dir / "plan.json"
    plan = None
    if plan_path.exists():
        try:
            plan = parse_plan(plan_path, parent_task.id)
        except Exception:
            pass

    total_in, total_out, total_cost = _aggregate_child_costs(parent_dir, plan)
    parent_result = Result(
        task_id=parent_task.id,
        status="success",
        summary=parent_task.goal,
        tokens_in=total_in,
        tokens_out=total_out,
        cost_usd=total_cost,
        duration_s=0.0,
    )
    (parent_dir / "result.json").write_text(parent_result.model_dump_json(indent=2))

    worktree.prune(env.repo, wt, keep_branch=True)
    dst = env.mas / "tasks" / "done" / parent_task.id
    board.move(parent_dir, dst, reason="role_success")


# --- 3. Proposer ------------------------------------------------------------

def _blocked_by_failure_pattern(env: TickEnv, goal: str) -> dict | None:
    """Return the first failure pattern that should block this goal, or None."""
    patterns = read_patterns(env.mas)
    if not patterns:
        return None
    threshold = env.cfg.proposal_similarity_threshold
    terminal_reasons = {'revision_cycles_exhausted', 'max_retries_exceeded', 'convergence_detected'}
    for pattern in patterns:
        sim = goal_similarity(goal, pattern.get('goal_sample', ''))
        if sim >= threshold:
            if pattern.get('count', 0) >= 2 or pattern.get('terminal_reason') in terminal_reasons:
                return pattern
    return None


def _materialize_proposal(env: TickEnv, result: Result) -> None:
    """Turn a successful proposer result.handoff into a proposed/ task card.

    Agentic providers can write the task.json themselves; non-agentic ones
    (Ollama) only emit result.json, so the tick loop materializes the card
    from handoff."""
    handoff_raw = result.handoff or {}
    try:
        handoff = ProposalHandoff.model_validate(handoff_raw)
    except Exception:
        handoff = None

    if handoff is None:
        goal = handoff_raw.get("goal") or result.summary
    else:
        goal = handoff.goal or result.summary

    tlog = get_task_logger(log, task_id=result.task_id, component="proposer")

    if not goal:
        tlog.warning("no goal in handoff, skipping materialization")
        return
    if len(board.list_column(env.mas, "proposed")) >= env.cfg.max_proposed:
        return

    blocking = _blocked_by_failure_pattern(env, goal)
    if blocking is not None:
        tlog.info(
            "skipping proposal blocked by failure pattern (signature=%s, reason=%s, count=%d): %r",
            blocking.get("signature", ""),
            blocking.get("terminal_reason", ""),
            blocking.get("count", 0),
            goal,
        )
        return

    goals_with_meta = (
        _list_goals_with_meta(env.mas, "proposed")
        + _list_goals_with_meta(env.mas, "doing")
        + _list_goals_with_meta(env.mas, "done", limit=50)
        + _list_goals_with_meta(env.mas, "failed", limit=20)
    )
    existing_goals = [g for _, _, g in goals_with_meta]
    hit = find_similar_goal(goal, existing_goals, threshold=env.cfg.proposal_similarity_threshold)
    if hit is not None:
        matched_goal, score = hit
        matched_column = "proposed"
        matched_task_id = ""
        for col, tid, g in goals_with_meta:
            if g == matched_goal:
                matched_column = col
                matched_task_id = tid
                break
        tlog.info("dropping duplicate proposal (jaccard=%.2f vs %r): %r", score, matched_goal, goal)
        from datetime import datetime, timezone as _tz
        truncated_goal = goal if len(goal) <= 500 else goal[:497] + "..."
        record = RejectedProposal(
            timestamp=datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            summary=result.summary or "",
            goal=truncated_goal,
            similarity_score=score,
            matched_task_id=matched_task_id,
            matched_column=matched_column,
            threshold=env.cfg.proposal_similarity_threshold,
        )
        write_rejected_proposal(env.mas, record)
        return

    inputs: dict = {}
    if handoff is not None and handoff.rationale:
        inputs["rationale"] = handoff.rationale
    elif handoff is None and handoff_raw.get("rationale"):
        inputs["rationale"] = handoff_raw["rationale"]

    if handoff is not None:
        if handoff.acceptance:
            inputs["acceptance"] = handoff.acceptance
        if handoff.suggested_changes:
            inputs["suggested_changes"] = handoff.suggested_changes
    else:
        for key in ("acceptance", "suggested_changes"):
            if handoff_raw.get(key):
                inputs[key] = handoff_raw[key]

    tid = new_task_id(goal)
    target = env.mas / "tasks" / "proposed" / tid
    if target.exists():
        tid = new_task_id(goal, salt=result.task_id)
        target = env.mas / "tasks" / "proposed" / tid
        if target.exists():
            tlog.warning("proposed/%s already exists, skipping", tid)
            return

    task = Task(id=tid, role="orchestrator", goal=goal, inputs=inputs)
    board.write_task(target, task)
    _write_proposal_doc(target, task)
    tlog.info("materialized proposal %s", tid)


def _write_proposal_doc(target: Path, task: Task) -> None:
    """Write a human-readable `task.md` alongside `task.json` for proposed tasks."""
    lines: list[str] = [f"# {task.goal}", ""]
    lines.append(f"- **Task ID:** `{task.id}`")
    lines.append(f"- **Role:** {task.role}")
    lines.append(f"- **Created:** {task.created_at.isoformat(timespec='seconds')}")
    lines.append("")

    rationale = task.inputs.get("rationale")
    if rationale:
        lines += ["## Rationale", "", str(rationale).strip(), ""]

    acceptance = task.inputs.get("acceptance")
    if acceptance:
        lines.append("## Acceptance criteria")
        lines.append("")
        if isinstance(acceptance, list):
            for item in acceptance:
                lines.append(f"- {item}")
        else:
            for line in str(acceptance).splitlines():
                line = line.strip()
                if not line:
                    continue
                lines.append(line if line.startswith(("-", "*")) else f"- {line}")
        lines.append("")

    suggested = task.inputs.get("suggested_changes")
    if suggested:
        lines += ["## Suggested changes", ""]
        for item in suggested:
            lines.append(f"- {item}")
        lines.append("")

    (target / "task.md").write_text("\n".join(lines).rstrip() + "\n")


def _materialize_plan(parent_dir: Path, result: Result) -> bool:
    """Write plan.json from result.handoff for non-agentic orchestrators.

    Returns True if plan.json was written successfully.
    """
    handoff = dict(result.handoff or {})
    handoff.setdefault("parent_id", result.task_id)
    tlog = get_task_logger(log, task_id=result.task_id, component="orchestrator")
    try:
        plan = Plan.model_validate(handoff)
    except Exception as e:
        tlog.warning("handoff is not a valid Plan (%s)", e)
        return False
    (parent_dir / "plan.json").write_text(plan.model_dump_json(indent=2))
    tlog.info("materialized plan.json from handoff")
    return True


def _maybe_dispatch_proposer(env: TickEnv) -> None:
    proposed = board.list_column(env.mas, "proposed")
    if len(proposed) >= env.cfg.max_proposed:
        return

    # Already a proposer task in doing/ (running or waiting to be dispatched)?
    for tdir in board.list_column(env.mas, "doing"):
        try:
            t = board.read_task(tdir)
        except Exception:
            continue
        if t.role == "proposer":
            return

    # Check provider concurrency cap
    role_cfg = env.cfg.roles["proposer"]
    prov_cfg = env.cfg.providers[role_cfg.provider]
    if board.count_active_pids(env.mas, role_cfg.provider) >= prov_cfg.max_concurrent:
        return

    ideas = env.mas / "ideas.md"
    ci_cmd = env.cfg.proposer_signals.get("ci_command")
    signals = gather_proposer_signals(
        env.repo,
        ideas_path=ideas,
        ci_command=ci_cmd,
        mas_root=env.mas,
    )
    goal = "Propose a new task for the board"
    tid = new_task_id(goal)
    task = Task(
        id=tid,
        role="proposer",
        goal=goal,
        inputs={"signals": signals.model_dump()},
    )
    # Proposer runs in a transient workspace inside doing/; proposals it emits
    # go to .mas/tasks/proposed/.
    tdir = env.mas / "tasks" / "doing" / tid
    board.write_task(tdir, task)
    _dispatch_role(env, task, tdir, tdir, role="proposer")


# --- Dispatch helper --------------------------------------------------------


def _consensus_enabled(cfg: MasConfig, task: Task) -> bool:
    """True when this task's cost budget meets the configured plan-consensus
    threshold. Falls back to `default_cost_budget_usd` when the task itself
    carries no per-task budget. Disabled when threshold is unset."""
    threshold = cfg.plan_consensus_threshold_usd
    if threshold is None:
        return False
    budget = task.cost_budget_usd
    if budget is None:
        budget = cfg.default_cost_budget_usd
    if budget is None:
        return False
    return budget >= threshold


def _consensus_prompt_block(task_dir: Path) -> str:
    """Prompt addendum injected via $consensus_block when plan-time consensus
    is gated on. Asks the orchestrator to draft two distinct plan variants,
    compare them, and write the chosen variant as plan.json plus a sidecar
    `plan_pick.json` capturing the rationale and discarded variant id."""
    return (
        "## Plan-time consensus mode\n"
        "\n"
        "This task's cost budget is high enough to justify a two-variant plan.\n"
        "Before writing `plan.json`, do the following:\n"
        "\n"
        f"1. Independently draft two complete Plan variants and save them as\n"
        f"   `{task_dir}/plan_variant_a.json` and `{task_dir}/plan_variant_b.json`.\n"
        "   The two variants MUST differ meaningfully (decomposition, test\n"
        "   strategy, or implementation approach) — not near-duplicates.\n"
        "2. Compare them in a short rationale (≤200 words): which variant is\n"
        "   more robust, and why.\n"
        "3. Write the chosen variant verbatim to `plan.json`. Write the\n"
        "   rationale and discarded variant id to `plan_pick.json`:\n"
        "\n"
        '   {"chosen": "a"|"b", "rationale": "..."}\n'
        "\n"
        "Treat each variant as a candidate you'd ship — no half-baked drafts.\n"
    )


def _dry_run_prompt_block(role: str, task_dir: Path) -> str:
    """Prompt addendum injected via $dry_run_block when --dry-run-child is on.

    Tells the agent the worktree is read-only this run and that the unified
    diff it would have applied must be written to `proposed_diff.patch` for
    tick to gate (parse + allowed_paths) and apply on its behalf."""
    return (
        "## Dry-run mode (MAS_DRY_RUN=1 is set)\n"
        "\n"
        f"Do **not** modify any file in the worktree. Instead, generate a unified\n"
        f"diff describing the change you would make (relative to the worktree HEAD,\n"
        f"using `git diff` format with `a/`/`b/` prefixes) and write it to:\n"
        f"\n"
        f"  {task_dir}/proposed_diff.patch\n"
        f"\n"
        "The orchestrator will validate the patch with `git apply --check`,\n"
        "enforce `constraints.allowed_paths` on every touched file, and apply it\n"
        "in the worktree. If the patch fails to parse or escapes the allowlist,\n"
        "your result will be coerced to failure regardless of `status`.\n"
        "\n"
        "Still write `result.json` as usual; populate `handoff` with the same\n"
        "fields you'd produce in normal mode (e.g. `final_exit_code` is the\n"
        "exit code you expect after the patch is applied — tick will re-run\n"
        "the test command to verify).\n"
    )


def _failure_pattern_block(mas_dir: Path, goal: str, top_n: int = 5) -> str:
    """Render a markdown block of recurring failure patterns relevant to *goal*.

    Reads ``mas_dir/patterns.jsonl``, filters patterns whose ``goal_sample``
    is semantically similar to *goal*, and formats the top-N into a prompt
    addendum injected via ``$pattern_block`` for implementer/tester roles.
    """
    patterns = read_patterns(mas_dir)
    if not patterns:
        return ""

    # Filter by relevance using goal_similarity with a reasonable threshold.
    threshold = 0.15
    relevant = [
        p for p in patterns
        if goal_similarity(goal, p.get("goal_sample", "")) >= threshold
    ]

    if not relevant:
        return ""

    # Sort by count descending, take top N.
    relevant.sort(key=lambda p: p.get("count", 0), reverse=True)
    top = relevant[:top_n]

    # Format as markdown block.
    lines = ["## Recurring failure patterns", ""]
    for p in top:
        lines.append(f"### {p.get('signature', '')}")
        lines.append(f"- Count: {p.get('count', 0)}")
        lines.append(f"- Terminal reason: {p.get('terminal_reason', '')}")
        lines.append(f"- Goal sample: {p.get('goal_sample', '')}")
        lines.append("")

    return "\n".join(lines)


def _dispatch_role(
    env: TickEnv,
    task: Task,
    task_dir: Path,
    cwd: Path,
    *,
    role: Role,
) -> int | None:
    role_cfg = env.cfg.roles[role]
    prov_cfg = env.cfg.providers[role_cfg.provider]

    # Respect provider concurrency cap.
    if board.count_active_pids(env.mas, role_cfg.provider) >= prov_cfg.max_concurrent:
        return

    adapter_cls = get_adapter(role_cfg.provider)
    adapter = adapter_cls(prov_cfg, role_cfg)

    # Inject previous_failure marker if present.
    pf = task_dir / ".previous_failure"
    if pf.exists():
        task.previous_failure = pf.read_text()
        pf.unlink()

    board.write_task(task_dir, task)

    # Hierarchical summary: when this is a child subtask and the parent has
    # already accumulated enough done subtasks for summary.md to exist, swap
    # the full prior_results history out for the digest.
    parent_summary = ""
    if task.parent_id:
        parent_summary = _summary.read_summary(task_dir.parent.parent) or ""

    dry_run_active = env.dry_run_child and role in ("implementer", "tester")
    dry_run_block = _dry_run_prompt_block(role, task_dir) if dry_run_active else ""

    consensus_block = ""
    if role == "orchestrator" and _consensus_enabled(env.cfg, task):
        consensus_block = _consensus_prompt_block(task_dir)

    pattern_block = ""
    if role in ("implementer", "tester"):
        pattern_block = _failure_pattern_block(env.mas, task.goal)

    # Render prompt
    prompt_path = env.mas / "prompts" / f"{role}.md"
    if not prompt_path.exists():
        log.warning("missing prompt template %s; using goal", prompt_path)
        prompt = task.goal
    else:
        prompt = render_prompt(
            prompt_path,
            task,
            task_dir=str(task_dir),
            worktree=str(cwd),
            mas_dir=str(env.mas),
            parent_summary=parent_summary,
            dry_run_block=dry_run_block,
            consensus_block=consensus_block,
            pattern_block=pattern_block,
        )

    attempt = task.attempt
    log_path = task_dir / "logs" / f"{role}-{attempt}.log"

    stdin_text = prompt if not adapter.agentic else None
    extra_env = {"MAS_DRY_RUN": "1"} if dry_run_active else None
    try:
        handle = adapter.dispatch(
            prompt=prompt,
            task_dir=task_dir,
            cwd=cwd,
            log_path=log_path,
            role=role,
            stdin_text=stdin_text,
            extra_env=extra_env,
        )
    except AdapterUnavailableError as e:
        result = Result(
            task_id=task.id,
            status="failure",
            summary=str(e),
            artifacts=[],
            handoff=None,
            verdict=None,
            feedback=None,
            tokens_in=None,
            tokens_out=None,
            duration_s=0.0,
            cost_usd=None,
        )
        (task_dir / "result.json").write_text(result.model_dump_json(indent=2))
        return None
    board.write_pid(task_dir / "pids", role, role_cfg.provider, handle.pid)
    return handle.pid
