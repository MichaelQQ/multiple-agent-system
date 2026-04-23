"""Post-hoc verification of tester/implementer/evaluator results.

The tester and implementer prompts ask for `handoff.initial_exit_code` and
`handoff.final_exit_code` respectively, reflecting a real `test_command` run.
Without enforcement, agents that can't actually execute their test command
(sandbox blocks, missing tooling) return fabricated values with status=success
based on "static analysis". This module rejects such results before they
pollute the control flow.

Language-agnostic: the test runner is whatever the tester declares in
`handoff.test_command` (pytest, go test, cargo test, npm test, …). The
verification extracts the first shell token from that command and requires
it to appear in the worker's log.

For testers/implementers:
  1. Required handoff fields are present and well-typed.
  2. The agent's log contains the declared test_command's executable. If the
     log instead shows sandbox/permission language, downgrade to
     environment_error so retries don't get burned on bad environment.

For evaluators, the orchestrator can declare acceptance checks via
`spec.constraints.required_artifacts` and `spec.constraints.required_grep`
which are verified independently of the LLM's verdict. A pass verdict is
downgraded to needs_revision when either check fails, so an evaluator cannot
mark work complete that does not satisfy its own acceptance criteria.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

from .schemas import Result, SubtaskSpec

_SANDBOX_MARKERS = (
    "blocked by the sandbox",
    "sandbox restrictions",
    "permission denied",
    "requires explicit user approval",
    "blocked as sensitive",
)


def _test_command_signature(test_command: str) -> str | None:
    """Extract the first executable token from a shell command for log matching.
    E.g. 'pytest -q' → 'pytest'; '.venv/bin/pytest tests/' → 'pytest';
    'npm test' → 'npm'; 'cargo test --release' → 'cargo'."""
    try:
        tokens = shlex.split(test_command)
    except ValueError:
        tokens = test_command.split()
    for tok in tokens:
        if "=" in tok and not tok.startswith("./") and not tok.startswith("/"):
            # env var assignment like FOO=bar; skip
            continue
        # strip leading path to the basename so "/foo/bin/pytest" → "pytest"
        basename = tok.rsplit("/", 1)[-1]
        if basename:
            return basename
    return None


def _log_mentions(log_path: Path, needle: str) -> bool:
    if not log_path.exists() or not needle:
        return False
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return False
    # whole-word match to avoid "go" matching "going", "npm" matching "npms", etc.
    return bool(re.search(r"\b" + re.escape(needle) + r"\b", text))


def _log_mentions_sandbox_block(log_path: Path) -> bool:
    if not log_path.exists():
        return False
    try:
        text = log_path.read_text(errors="replace").lower()
    except OSError:
        return False
    return any(m in text for m in _SANDBOX_MARKERS)


def _is_docs_only(spec: SubtaskSpec) -> bool:
    return bool(spec.constraints.get("docs_only") or spec.inputs.get("docs_only"))


def verify_child_result(
    spec: SubtaskSpec,
    result: Result,
    child_dir: Path,
    attempt: int,
) -> Result:
    """Return the result unchanged if it passes verification, or a coerced
    Result (status=failure or environment_error) if it does not.

    Only applies to tester/implementer success claims; evaluator, orchestrator,
    and docs-only subtasks are passed through."""
    if result.status != "success":
        return result
    if spec.role not in ("tester", "implementer"):
        return result
    if _is_docs_only(spec):
        return result

    handoff = result.handoff or {}

    if spec.role == "tester":
        test_cmd = handoff.get("test_command")
        init_code = handoff.get("initial_exit_code")
        missing: list[str] = []
        if not test_cmd or not isinstance(test_cmd, str):
            missing.append("test_command")
        if not isinstance(init_code, int):
            missing.append("initial_exit_code")
        if missing:
            return _coerce(result, f"tester handoff missing or invalid: {', '.join(missing)}")
        if init_code == 0:
            return _coerce(result, "tester handoff.initial_exit_code is 0 — tests are not failing")

    if spec.role == "implementer":
        final_code = handoff.get("final_exit_code")
        if not isinstance(final_code, int):
            return _coerce(result, "implementer handoff missing or invalid: final_exit_code")
        if final_code != 0:
            return _coerce(result, f"implementer handoff.final_exit_code is {final_code} — tests still fail")

    log_path = child_dir / "logs" / f"{spec.role}-{attempt}.log"
    # Sandbox markers first: if the runner was blocked, any stray mention of
    # its name in the log isn't evidence of a real run.
    if _log_mentions_sandbox_block(log_path):
        return _coerce(
            result,
            f"{spec.role} log shows sandbox/permission block — self-reported exit code is unverified",
            status="environment_error",
        )

    # Derive the test runner's executable name from whichever handoff supplies
    # test_command (tester supplies it directly; implementer may omit it). If
    # we can't derive a signature, we skip the log-mention check rather than
    # false-flag a real run — the handoff field checks above still apply.
    test_cmd = handoff.get("test_command")
    if not isinstance(test_cmd, str) or not test_cmd:
        return result
    signature = _test_command_signature(test_cmd)
    if not signature:
        return result
    if not _log_mentions(log_path, signature):
        return _coerce(
            result,
            f"{spec.role} log does not mention the declared test runner {signature!r} "
            f"(from test_command={test_cmd!r}) — self-reported exit code is unverified",
        )

    return result


def _coerce(result: Result, reason: str, *, status: str = "failure") -> Result:
    """Return a new Result with coerced status and reason prepended to summary."""
    return result.model_copy(update={
        "status": status,
        "summary": f"[verify:{status}] {reason}. original: {result.summary}",
        "feedback": (result.feedback or "") + (f"\n[verify:{status}] {reason}"),
    })


def verify_evaluator_result(
    spec: SubtaskSpec,
    result: Result,
    worktree: Path,
) -> Result:
    """For evaluator subtasks: if the LLM emitted verdict=pass but the
    orchestrator's acceptance checks (required_artifacts / required_grep in
    spec.constraints) fail, downgrade to verdict=needs_revision with a
    machine-generated feedback message describing what's missing."""
    if spec.role != "evaluator":
        return result
    if result.verdict != "pass":
        return result

    constraints = spec.constraints or {}
    req_artifacts = constraints.get("required_artifacts") or []
    req_grep = constraints.get("required_grep") or []

    missing: list[str] = []

    for rel in req_artifacts:
        if not isinstance(rel, str):
            continue
        p = worktree / rel
        if not p.exists():
            missing.append(f"missing file: {rel}")

    for rule in req_grep:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern")
        glob = rule.get("file_glob") or "**/*"
        count_min = int(rule.get("count_min") or 1)
        if not pattern or not isinstance(pattern, str):
            continue
        found = _grep_count(worktree, pattern, glob)
        if found < count_min:
            missing.append(
                f"grep {pattern!r} in {glob!r} found {found}, need ≥{count_min}"
            )

    if not missing:
        return result

    reason = "evaluator verdict downgraded: " + "; ".join(missing)
    return result.model_copy(update={
        "status": "needs_revision",
        "verdict": "needs_revision",
        "summary": f"[verify:needs_revision] {reason}. original: {result.summary}",
        "feedback": (result.feedback or "") + f"\n[verify:needs_revision] {reason}",
    })


def _grep_count(worktree: Path, pattern: str, glob: str) -> int:
    try:
        rx = re.compile(pattern)
    except re.error:
        return 0
    total = 0
    for p in worktree.glob(glob):
        if not p.is_file():
            continue
        try:
            for line in p.read_text(errors="replace").splitlines():
                if rx.search(line):
                    total += 1
        except OSError:
            continue
    return total
