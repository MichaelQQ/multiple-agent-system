# Reliability & Coordination Gap Plan

Plan to close 7 architectural gaps in `mas` that separate it from Claude Code-grade systems. Grounded in current source: Pydantic strict I/O + `verify.py` already exist; `_append_revision_cycle` in `src/mas/tick.py:545` already implements plan→act→verify→correct→repeat; `transitions.jsonl` + `events.py` provide audit. Fixes below extend rather than greenfield.

## 1. Execution reliability (LLM output as probabilistic signal)

**Now:** `src/mas/verify.py` does post-hoc checks (handoff fields, log-mention of test runner, sandbox markers, evaluator acceptance). Single-shot — no cross-validation.

**Fix:**
- Add **typed handoff schemas per role** (`ImplementerHandoff`, `TesterHandoff`, `EvaluatorHandoff` Pydantic models in `src/mas/schemas.py`) instead of `dict[str, Any]`. Forces shape at parse, not in `verify.py`.
- Add **structural diff verifier**: implementer claims `final_exit_code=0` → tick re-runs `test_command` against the worktree headlessly before accepting. Cheap, catches fabricated success.
- Add **N-of-M evaluator quorum** as opt-in `roles.evaluator.quorum: 2` — dispatch 2 evaluators with different providers, accept only on agreement; disagreement → `needs_revision` with both feedbacks merged.

Effort: M.

## 2. State management (global understanding decay)

**Now:** `prior_results` carries previous role outputs; `revision_feedback` carries cycle feedback. No file-system snapshot, no graph.

**Fix:**
- Persist `parent_dir/state.json`: `{worktree_files_touched, test_command, last_known_green_sha, accepted_artifacts[], rejected_attempts[]}`. Updated by tick after each child completion; injected into next role prompt as `inputs.state`.
- Replace flat `prior_results` with **task graph** (`graph.json`: nodes = subtasks, edges = causality/revision links). Lets evaluators see "this is rev-2 because rev-1 failed grep check X."
- Snapshot worktree on green: `git tag mas/{task_id}/green-{cycle}` so revisions can diff against last-known-good.

Effort: M.

## 3. Coordination / arbitration

**Now:** Sequential implementer→tester→evaluator. No conflict resolution; evaluator is sole arbiter.

**Fix:**
- **Structured disagreement protocol**: when evaluator returns `needs_revision`, implementer's next prompt must include `disputes: list[{evaluator_claim, implementer_response}]`. After 1 cycle, if implementer disputes a claim, an `arbiter` role (new, optional) is dispatched with both sides + repo state and emits binding verdict.
- **Provider diversity rule**: enforce in config validation that evaluator's provider ≠ implementer's provider for a given parent. Prevents same-model collusion on hallucinated "looks correct."
- **Plan-time consensus** (cheap): orchestrator emits 2 plan variants; second pass picks one with rationale. Optional — only worth it for tasks with `cost_budget_usd > threshold`.

Effort: M (arbiter role) → L (full quorum).

## 4. Robust agent loop (plan→act→verify→correct→repeat)

**Now:** Loop exists but two failure modes:
- `_append_revision_cycle` silently no-ops when cycles exhausted (`.mas/ideas.md` bug — parent stuck in `doing/` forever).
- No dynamic replan: if 2 cycles fail the same way, we keep retrying the same plan.

**Fix:**
- Fix the `ideas.md` bug: cycles exhausted → parent → `failed/` with reason `revision_cycles_exhausted` (mirror `tick.py:540-542` path).
- Add **replan trigger**: after `max_revision_cycles - 1` cycles with the same failing artifact, re-dispatch orchestrator with `inputs.replan_reason = <feedback summary>` to emit a new subtask sequence. Bounded by a separate `max_replans: 1`.
- Add **convergence detector**: if cycle N's evaluator feedback is >85% similar (token Jaccard) to cycle N-1's, escalate to arbiter (gap 3) or fail — signals the loop isn't making progress.

Effort: S (bug fix) + M (replan) + S (convergence).

## 5. Production-grade tool execution

**Now:** Worktree gives reversibility (good). `dispatch()` is fire-and-forget. No sandbox beyond what the underlying CLI provides.

**Fix:**
- Add `mas tick --dry-run-child` mode: dispatch child with `MAS_DRY_RUN=1`; child writes `proposed_diff.patch` instead of mutating worktree. Tick applies patch only if it parses cleanly and doesn't touch files outside `inputs.allowed_paths`.
- **Path allowlist enforcement**: `subtask.constraints.allowed_paths: ["src/mas/tick.py"]` — tick verifies post-hoc via `git diff --name-only` and rejects (status=failure) if scope violated.
- Add `mas verify <task_id>` command that re-runs the recorded `test_command` and compares against `handoff.final_exit_code` — cheap auditor, not in critical path.

Effort: M.

## 6. Context compression

**Now:** No compression. `prior_results` accumulates raw `Result.summary` + `feedback`. Prompts grow linearly per cycle.

**Fix:**
- Add `roles.py` helper `compress_prior_results(results, target_tokens)`: keep latest result verbatim; older results → 1-line `{role}: {status} — {first 200 chars of summary}`. Apply when `len(serialized) > 8KB`.
- Add **retrieval slice**: instead of full `prior_results`, include only entries where `spec.role` matches current role OR `feedback` mentions current role's artifacts (regex on filenames). Cuts implementer prompt from "all prior" to "things relevant to me."
- Long-task hierarchical summary: when a parent has >5 subtasks done, run a one-shot `claude_code` call to produce `parent_dir/summary.md`; inject into subsequent subtask prompts in place of full history.

Effort: S (truncation) → M (retrieval) → M (hierarchical).

## 7. Scaling / persistent reasoning

**Now:** `transitions.jsonl` per task, `events.jsonl` global, `audit.py` events. No cross-task graph; `mas stats` aggregates only.

**Fix:**
- Most of this **falls out of #2** (graph) + **#6** (compression). The audit trail already exists.
- Add `mas trace <task_id>`: render the `graph.json` + `transitions.jsonl` + cost rollup as a single document (existing `trace.py` is a stub at 162 lines — extend it).
- Add **failure pattern index**: `state.json` carries `rejected_attempts`; a periodic job (or tick suffix) writes `.mas/patterns.jsonl` of recurring failure signatures. Proposer reads it to avoid re-proposing tasks that previously failed for the same reason.

Effort: S (trace) + M (patterns).

## TODO (suggested execution order — high impact, low risk first)

- [x] **1.** Fix `_append_revision_cycle` exhausted-cycles bug → move parent to `failed/` with `revision_cycles_exhausted` reason (gap 4, ~30min, scoped in `ideas.md`)
- [x] **2.** Typed handoff schemas: replace `Result.handoff: dict[str, Any]` with role-specific Pydantic models (gap 1)
- [x] **3.** Headless test re-run verifier in tick before accepting implementer success (gap 1)
- [x] **4.** `compress_prior_results` truncation helper, applied when serialized size > 8KB (gap 6)
- [x] **5.** Retrieval slice for `prior_results` — filter by current role + filename regex (gap 6)
- [x] **6.** `parent_dir/state.json` foundation: write/read on every child completion (gap 2)
- [x] **7.** Git tag snapshots `mas/{task_id}/green-{cycle}` on green (gap 2)
- [x] **8.** Replan trigger: orchestrator re-dispatch with `replan_reason` after near-exhaustion (gap 4)
- [x] **9.** Convergence detector: Jaccard similarity on cycle-N vs cycle-(N-1) feedback (gap 4)
- [x] **10.** `subtask.constraints.allowed_paths` post-hoc enforcement via `git diff --name-only` (gap 5)
- [x] **11.** `mas verify <task_id>` CLI: re-run recorded `test_command` and audit (gap 5)
- [x] **12.** Provider diversity rule: validate `evaluator.provider != implementer.provider` (gap 3)
- [x] **13.** Optional `arbiter` role for structured disagreement resolution (gap 3)
- [x] **14.** Optional N-of-M evaluator quorum (`roles.evaluator.quorum: 2`) (gap 1)
- [x] **15.** `graph.json` task graph replacing flat `prior_results` (gap 2)
- [x] **16.** Hierarchical summary for parents with >5 done subtasks (gap 6)
- [x] **17.** Extend `trace.py` → `mas trace <task_id>` rendering graph + transitions + cost (gap 7)
- [ ] **18.** `.mas/patterns.jsonl` failure-pattern index, consumed by proposer (gap 7)
- [ ] **19.** `mas tick --dry-run-child` mode with `proposed_diff.patch` apply gate (gap 5)
- [ ] **20.** Plan-time consensus (2 plan variants, second-pass picker) — gated on `cost_budget_usd` (gap 3)
