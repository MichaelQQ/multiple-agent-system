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

import fnmatch
import json
import re
import shlex
import subprocess
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from .schemas import ImplementerHandoff, Result, SubtaskSpec, TesterHandoff

_VERIFY_RERUN_TIMEOUT_S = 300

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

    typed_handoff: TesterHandoff | ImplementerHandoff | None = None
    raw = result.handoff or {}

    if spec.role == "tester":
        try:
            typed_handoff = TesterHandoff.model_validate(raw)
        except ValidationError as e:
            fields = _invalid_fields(e)
            return _coerce(result, f"tester handoff missing or invalid: {', '.join(fields)}")
        if not typed_handoff.test_command:
            return _coerce(result, "tester handoff missing or invalid: test_command")
        if typed_handoff.initial_exit_code == 0:
            return _coerce(result, "tester handoff.initial_exit_code is 0 — tests are not failing")

    if spec.role == "implementer":
        try:
            typed_handoff = ImplementerHandoff.model_validate(raw)
        except ValidationError as e:
            fields = _invalid_fields(e)
            return _coerce(result, f"implementer handoff missing or invalid: {', '.join(fields)}")
        if typed_handoff.final_exit_code != 0:
            return _coerce(
                result,
                f"implementer handoff.final_exit_code is {typed_handoff.final_exit_code} — tests still fail",
            )

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
    test_cmd = typed_handoff.test_command if typed_handoff is not None else None
    if not test_cmd:
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


def verify_implementer_test_rerun(
    spec: SubtaskSpec,
    result: Result,
    worktree: Path,
    test_command: str | None,
    timeout_s: int = _VERIFY_RERUN_TIMEOUT_S,
) -> Result:
    """Headlessly re-run the declared test_command in the worktree to validate
    an implementer's claim of final_exit_code=0. Catches fabricated success.

    Skipped when: result is not a successful implementer claim, the spec is
    docs_only, no test_command is available, or the worktree path is missing.
    A non-zero re-run exit code coerces the result to failure; failure to
    invoke the runner becomes environment_error so retries don't burn the
    role's retry budget on bad environment."""
    if result.status != "success":
        return result
    if spec.role != "implementer":
        return result
    if _is_docs_only(spec):
        return result
    if not test_command:
        return result
    if not worktree.is_dir():
        return result

    raw = result.handoff or {}
    if raw.get("final_exit_code") != 0:
        return result

    try:
        proc = subprocess.run(
            test_command,
            shell=True,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return _coerce(
            result,
            f"implementer test re-run timed out after {timeout_s}s "
            f"(test_command={test_command!r}) — claim of final_exit_code=0 unverified",
        )
    except OSError as e:
        return _coerce(
            result,
            f"implementer test re-run failed to start: {e} "
            f"(test_command={test_command!r})",
            status="environment_error",
        )

    if proc.returncode != 0:
        combined = (proc.stdout or "") + (proc.stderr or "")
        tail = "\n".join(combined.splitlines()[-20:])
        return _coerce(
            result,
            f"implementer claimed final_exit_code=0 but headless re-run of "
            f"{test_command!r} returned exit_code={proc.returncode}. "
            f"output tail:\n{tail}",
        )

    return result


def _invalid_fields(e: ValidationError) -> list[str]:
    """Extract the dotted field paths reported by a ValidationError."""
    fields: list[str] = []
    seen: set[str] = set()
    for err in e.errors():
        loc = err.get("loc", ())
        field = ".".join(str(p) for p in loc) if loc else "<root>"
        if field not in seen:
            seen.add(field)
            fields.append(field)
    return fields


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


_BASELINE_FILE = ".baseline.json"


def _git_head_sha(worktree: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    sha = r.stdout.strip()
    return sha or None


def _dirty_files(worktree: Path) -> set[str]:
    """Return the set of paths reported by `git status --porcelain -uall` —
    modified, added, deleted, renamed, or untracked (excluding gitignored).
    `-uall` expands untracked directories so we get individual file paths
    rather than a collapsed `dir/` entry."""
    try:
        r = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain", "-uall"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if r.returncode != 0:
        return set()
    files: set[str] = set()
    for line in r.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.add(path.strip().strip('"'))
    return {f for f in files if f}


def _committed_changes_since(worktree: Path, baseline_sha: str) -> set[str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", baseline_sha, "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if r.returncode != 0:
        return set()
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


def capture_worktree_baseline(worktree: Path, child_dir: Path) -> None:
    """Snapshot `HEAD` SHA + dirty files into `child_dir/.baseline.json` so a
    later `verify_allowed_paths` call can compute the delta this subtask
    actually introduced. Idempotent — overwrites on each dispatch (intended
    behavior on retry: measure against the new starting point)."""
    if not worktree.is_dir():
        return
    payload = {
        "head_sha": _git_head_sha(worktree),
        "dirty": sorted(_dirty_files(worktree)),
    }
    try:
        (child_dir / _BASELINE_FILE).write_text(json.dumps(payload))
    except OSError:
        pass


def _read_baseline(child_dir: Path) -> dict | None:
    p = child_dir / _BASELINE_FILE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _changed_files_since(worktree: Path, baseline: dict | None) -> set[str]:
    current_dirty = _dirty_files(worktree)
    if baseline is None:
        return current_dirty
    baseline_dirty = set(baseline.get("dirty") or [])
    new_dirty = current_dirty - baseline_dirty
    baseline_sha = baseline.get("head_sha")
    committed: set[str] = set()
    if isinstance(baseline_sha, str) and baseline_sha:
        committed = _committed_changes_since(worktree, baseline_sha)
    return new_dirty | committed


def _path_allowed(path: str, patterns: list) -> bool:
    """Match a path against an allowlist. Supported pattern forms:
      - exact: `src/mas/tick.py`
      - shell glob (path-aware via `PurePosixPath.match`, where `*` does
        not cross `/`): `src/mas/*.py`, `*.md`
      - directory prefix (with or without trailing slash): `src/mas/` or
        `src/mas` allows any descendant `src/mas/foo/bar.py`.
    Falls back to `fnmatch` when `PurePosixPath.match` rejects the pattern."""
    p = PurePosixPath(path)
    for pat in patterns:
        if not isinstance(pat, str) or not pat:
            continue
        if path == pat:
            return True
        prefix = pat.rstrip("/")
        if prefix and path.startswith(prefix + "/"):
            return True
        if any(ch in pat for ch in "*?["):
            try:
                if p.match(pat):
                    return True
            except (ValueError, NotImplementedError):
                if fnmatch.fnmatch(path, pat):
                    return True
    return False


def verify_allowed_paths(
    spec: SubtaskSpec,
    result: Result,
    worktree: Path,
    child_dir: Path,
) -> Result:
    """If `spec.constraints.allowed_paths` is declared, compute the worktree
    files this subtask changed (relative to the dispatch-time baseline) and
    coerce to failure if any change falls outside the allowlist.

    Skipped on non-success results, docs_only specs, missing worktree, or when
    no allowed_paths constraint is set. Without a baseline file, falls back to
    the worktree's full dirty set — strict but conservative."""
    if result.status != "success":
        return result
    if _is_docs_only(spec):
        return result
    constraints = spec.constraints or {}
    allowed = constraints.get("allowed_paths")
    if not allowed or not isinstance(allowed, list):
        return result
    if not worktree.is_dir():
        return result

    baseline = _read_baseline(child_dir)
    changed = _changed_files_since(worktree, baseline)
    if not changed:
        return result

    violations = sorted(f for f in changed if not _path_allowed(f, allowed))
    if not violations:
        return result

    shown = violations[:10]
    suffix = f" (+{len(violations) - 10} more)" if len(violations) > 10 else ""
    return _coerce(
        result,
        f"{spec.role} touched files outside allowed_paths={list(allowed)}: "
        f"{shown}{suffix}",
    )


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
