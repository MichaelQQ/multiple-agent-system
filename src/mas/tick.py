from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from . import board, transitions, worktree
from .adapters import AdapterUnavailableError, get_adapter
from .config import load_config, project_root, project_dir, validate_config
from .ids import task_id as new_task_id
from .logging import get_task_logger
from .roles import _list_goals, find_similar_goal, gather_proposer_signals, parse_plan, render_prompt
from .schemas import BaseModel, ConfigDict, MasConfig, Plan, ProposalHandoff, Result, Role, Task

log = logging.getLogger("mas.tick")


class LockBusy(RuntimeError):
    pass


class TickEnv(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    repo: Path
    mas: Path
    cfg: MasConfig


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


def run_tick(*, start: Path | None = None) -> None:
    project_root_path = project_root(start)
    mas = project_dir(start)
    if start is not None and (start / ".git").is_dir():
        repo = start
    else:
        repo = project_root_path
    cfg = load_config(mas)

    issues = validate_config(cfg, mas)
    if issues:
        issue_msgs = "; ".join(f"{i.field}: {i.message}" for i in issues)
        log.error("validation failed: %s", issue_msgs)
        raise ValueError(f"Validation failed: {issue_msgs}")

    env = TickEnv(repo=repo, mas=mas, cfg=cfg)
    board.ensure_layout(mas)

    try:
        lock = _acquire_lock(mas)
    except LockBusy:
        log.info("tick skipped: another tick holds the lock")
        return

    try:
        _reap_workers(env)
        _advance_doing(env)
        _maybe_dispatch_proposer(env)
    finally:
        lock.close()


def _reap_workers(env: TickEnv) -> None:
    """No-op beyond pid bookkeeping: board.count_active_pids already clears dead
    PID files. Detached workers write result.json themselves before exit."""
    board.count_active_pids(env.mas)


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
        if not log_path.exists():
            _dispatch_role(env, parent_task, parent_dir, parent_dir, role="proposer")
        return

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
        _dispatch_role(env, parent_task, parent_dir, wt, role="orchestrator")
        return

    plan = parse_plan(plan_path, parent_task.id)
    subtasks_root = parent_dir / "subtasks"
    subtasks_root.mkdir(exist_ok=True)

    # Sequential: find first subtask without successful result.
    next_child = _next_ready_child(plan, subtasks_root)
    if next_child is None:
        if _all_children_passed(plan, subtasks_root):
            _finalize_parent(env, parent_dir, parent_task)
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
        _handle_child_result(env, parent_dir, parent_task, plan, next_child, result)
        return

    child_task = Task(
        id=new_task_id(next_child.goal, salt=next_child.id),
        parent_id=parent_task.id,
        role=next_child.role,
        goal=next_child.goal,
        inputs=next_child.inputs,
        constraints=next_child.constraints,
        prior_results=_collect_prior_results(plan, next_child.id, subtasks_root),
        cycle=parent_task.cycle,
        attempt=child_attempt,
    )
    _dispatch_role(env, child_task, child_dir, wt, role=next_child.role)


def _collect_prior_results(plan: Plan, current_id: str, subtasks_root: Path) -> list[Result]:
    """Return results of subtasks that precede current_id in plan order."""
    priors: list[Result] = []
    for spec in plan.subtasks:
        if spec.id == current_id:
            break
        r = board.read_result(subtasks_root / spec.id)
        if r is not None:
            priors.append(r)
    return priors


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


def _synthesize_orphan_result(task_dir: Path, task_id: str, role: str, attempt: int) -> Result:
    tail = _read_log_tail(task_dir, role, attempt)
    r = Result(
        task_id=task_id,
        status="failure",
        summary=f"{role} exited without writing result.json",
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
        try:
            pid = int(p.read_text().strip())
        except (ValueError, OSError):
            p.unlink(missing_ok=True)
            continue
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
    for spec in plan.subtasks:
        child_dir = subtasks_root / spec.id
        result = board.read_result(child_dir) if child_dir.exists() else None
        if result is None:
            return spec
        if result.status == "success" and (spec.role != "evaluator" or result.verdict == "pass"):
            continue
        # Found one that needs attention (failure / needs_revision / evaluator fail)
        return spec
    return None


def _all_children_passed(plan: Plan, subtasks_root: Path) -> bool:
    for spec in plan.subtasks:
        result = board.read_result(subtasks_root / spec.id)
        if result is None or result.status != "success":
            return False
        if spec.role == "evaluator" and result.verdict != "pass":
            return False
    return True


def _handle_child_result(env, parent_dir, parent_task, plan, spec, result):
    child_dir = parent_dir / "subtasks" / spec.id
    txns = transitions.read_transitions(child_dir, limit=3)
    if txns:
        txn_str = " | ".join(f"{txn.from_state}→{txn.to_state}({txn.reason})" for txn in txns)
        result.feedback = (result.feedback or "") + (f"\n[transition history: {txn_str}]" if result.feedback else f"[transition history: {txn_str}]")

    # Success path: mark and move on (by next tick).
    if result.status == "success" and (spec.role != "evaluator" or result.verdict == "pass"):
        return

    # Evaluator verdict handling
    if spec.role == "evaluator" and result.verdict == "needs_revision":
        _append_revision_cycle(parent_dir, plan, parent_task, feedback=result.feedback or "")
        return

    # Failure: retry up to max_retries
    attempts_path = (parent_dir / "subtasks" / spec.id / ".attempt").resolve()
    attempt = int(attempts_path.read_text()) if attempts_path.exists() else 1
    role_cfg = env.cfg.roles[spec.role]
    if attempt < (role_cfg.max_retries + 1):
        # Bump attempt and clear result so next tick redispatches with previous_failure.
        attempts_path.write_text(str(attempt + 1))
        child_dir = parent_dir / "subtasks" / spec.id
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


def _append_revision_cycle(parent_dir: Path, plan: Plan, parent_task, feedback: str) -> None:
    from .schemas import SubtaskSpec

    # Bound by plan.max_revision_cycles
    existing_cycles = sum(1 for s in plan.subtasks if s.id.startswith("rev-"))
    if existing_cycles >= plan.max_revision_cycles:
        return

    cycle_n = existing_cycles + 1
    base_inputs = {"feedback": feedback, "parent_goal": parent_task.goal}
    new_children = [
        SubtaskSpec(id=f"rev-{cycle_n}-tester", role="tester",
                    goal=f"Augment tests to cover evaluator feedback (cycle {cycle_n})", inputs=base_inputs),
        SubtaskSpec(id=f"rev-{cycle_n}-implementer", role="implementer",
                    goal=f"Address evaluator feedback and make tests pass (cycle {cycle_n})", inputs=base_inputs),
        SubtaskSpec(id=f"rev-{cycle_n}-evaluator", role="evaluator",
                    goal=f"Evaluate revision {cycle_n}", inputs=base_inputs),
    ]
    plan.subtasks.extend(new_children)
    (parent_dir / "plan.json").write_text(plan.model_dump_json(indent=2))


def _finalize_parent(env: TickEnv, parent_dir: Path, parent_task) -> None:
    wt = parent_dir / "worktree"
    if wt.exists():
        worktree.commit_changes(wt, parent_task.goal)
    worktree.prune(env.repo, wt, keep_branch=True)
    dst = env.mas / "tasks" / "done" / parent_task.id
    board.move(parent_dir, dst, reason="role_success")


# --- 3. Proposer ------------------------------------------------------------


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

    existing_goals = (
        _list_goals(env.mas, "proposed")
        + _list_goals(env.mas, "doing")
        + _list_goals(env.mas, "done", limit=50)
        + _list_goals(env.mas, "failed", limit=20)
    )
    hit = find_similar_goal(goal, existing_goals, threshold=env.cfg.proposal_similarity_threshold)
    if hit is not None:
        tlog.info("dropping duplicate proposal (jaccard=%.2f vs %r): %r", hit[1], hit[0], goal)
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
    tlog.info("materialized proposal %s", tid)


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


def _dispatch_role(
    env: TickEnv,
    task: Task,
    task_dir: Path,
    cwd: Path,
    *,
    role: Role,
) -> None:
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
        )

    attempt = task.attempt
    log_path = task_dir / "logs" / f"{role}-{attempt}.log"

    stdin_text = prompt if not adapter.agentic else None
    try:
        handle = adapter.dispatch(
            prompt=prompt,
            task_dir=task_dir,
            cwd=cwd,
            log_path=log_path,
            role=role,
            stdin_text=stdin_text,
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
        board.move(task_dir, env.mas / "tasks" / "failed" / task.id, reason="adapter_unavailable")
        return
    board.write_pid(task_dir / "pids", role, role_cfg.provider, handle.pid)
