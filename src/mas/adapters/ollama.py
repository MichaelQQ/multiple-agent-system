from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from .base import Adapter, DispatchHandle


class OllamaAdapter(Adapter):
    """Non-agentic text provider.

    Launches a detached Python wrapper that:
      1. POSTs the prompt to the Ollama HTTP API with ``format=json`` so
         the model is constrained to produce valid JSON.
      2. Writes the response as ``result.json`` in ``task_dir``.

    Host is read from ``OLLAMA_HOST`` (default ``http://localhost:11434``).
    Token budget is controlled by ``MAS_OLLAMA_NUM_PREDICT`` and
    ``MAS_OLLAMA_NUM_CTX`` env vars.

    If the response cannot be parsed as JSON the wrapper writes a failure
    result so the orchestrator can retry rather than waiting for orphan
    detection.
    """

    name = "ollama"
    agentic = False

    def health_check(self) -> bool:
        cli = self.provider_cfg.cli or "ollama"
        return self._check_cli_responsive(cli, ["--version"])

    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
        cli = self.provider_cfg.cli or "ollama"
        model = self.role_cfg.model or "gemma4:e4b"
        args: list[str] = [cli, "run", model]
        args += list(self.provider_cfg.extra_args)
        return args

    # ------------------------------------------------------------------
    # Override dispatch to add post-processing: run ollama synchronously
    # inside a detached wrapper, then write result.json from its output.
    # ------------------------------------------------------------------

    def dispatch(
        self,
        prompt: str,
        task_dir: Path,
        cwd: Path,
        log_path: Path,
        role: str,
        stdin_text: str | None = None,
    ) -> DispatchHandle:
        if not self.health_check():
            message = self._last_health_error or f"{self.provider_cfg.cli} is unavailable"
            from .base import AdapterUnavailableError
            raise AdapterUnavailableError(message)
        cli = self.provider_cfg.cli or "ollama"
        model = self.role_cfg.model or "gemma4:e4b"
        extra = list(self.provider_cfg.extra_args)

        base_prompt = stdin_text if stdin_text is not None else prompt
        # Non-agentic: model cannot write files. The HTTP call uses
        # format=json so the model is constrained to emit a JSON object;
        # we still tell it what that object should represent.
        effective_prompt = (
            base_prompt
            + "\n\nYou cannot write files. Your entire response must be a "
            "single JSON object representing result.json for this task."
        )

        wrapper_path = task_dir / "_ollama_wrapper.py"
        wrapper_path.parent.mkdir(parents=True, exist_ok=True)

        config_path = task_dir / "_ollama_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "cli": cli,
                    "model": model,
                    "extra_args": extra,
                    "prompt": effective_prompt,
                    "task_dir": str(task_dir),
                    "role": role,
                },
                ensure_ascii=False,
            )
        )

        wrapper_src = self._wrapper_source()
        wrapper_path.write_text(wrapper_src)

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("ab")
        env = self._env()
        env["MAS_ROLE"] = role
        env["MAS_TASK_DIR"] = str(task_dir)

        proc = subprocess.Popen(
            [sys.executable, str(wrapper_path)],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            env=env,
        )
        log_fh.close()

        return DispatchHandle(
            pid=proc.pid,
            provider=self.name,
            role=role,
            task_dir=task_dir,
            log_path=log_path,
        )

    @staticmethod
    def _wrapper_source() -> str:
        return textwrap.dedent("""
            import json, os, re, socket, sys, time, urllib.request, urllib.error

            _BARE_JSON_RE = re.compile(r"(\\{[\\s\\S]*\\})")

            def _write_failure(task_dir, summary, feedback):
                result = {
                    "task_id": os.path.basename(task_dir),
                    "status": "failure",
                    "summary": summary,
                    "artifacts": [],
                    "handoff": None,
                    "verdict": None,
                    "feedback": feedback,
                    "tokens_in": None,
                    "tokens_out": None,
                    "duration_s": 0.0,
                    "cost_usd": None,
                }
                out_path = os.path.join(task_dir, "result.json")
                with open(out_path, "w") as fh:
                    json.dump(result, fh, indent=2)
                return out_path

            _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ollama_config.json")
            with open(_cfg_path) as _f:
                cfg = json.load(_f)
            model = cfg["model"]
            prompt, task_dir, role = cfg["prompt"], cfg["task_dir"], cfg["role"]

            host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
            if not host.startswith("http"):
                host = "http://" + host
            num_predict = int(os.environ.get("MAS_OLLAMA_NUM_PREDICT", "4096"))
            num_ctx = int(os.environ.get("MAS_OLLAMA_NUM_CTX", "8192"))
            temperature = float(os.environ.get("MAS_OLLAMA_TEMPERATURE", "0.2"))
            timeout = int(os.environ.get("MAS_OLLAMA_TIMEOUT", "3600"))

            body = {
                "model": model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {
                    "num_predict": num_predict,
                    "num_ctx": num_ctx,
                    "temperature": temperature,
                },
            }
            url = host + "/api/generate"
            print(
                f"[ollama-wrapper] POST {url} model={model} "
                f"num_predict={num_predict} num_ctx={num_ctx}",
                flush=True,
            )

            started = time.time()
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                _write_failure(
                    task_dir,
                    f"ollama HTTP call failed: HTTP {exc.code}: {exc.reason}",
                    str(exc),
                )
                print(f"[ollama-wrapper] wrote failure result.json (http error: {exc})", flush=True)
                sys.exit(1)
            except (urllib.error.URLError, socket.timeout) as exc:
                exc_str = str(exc)
                if isinstance(exc, socket.timeout) or "timed out" in exc_str.lower():
                    _write_failure(
                        task_dir,
                        f"ollama HTTP call failed: timeout after {timeout}s",
                        exc_str,
                    )
                else:
                    _write_failure(
                        task_dir,
                        f"ollama HTTP call failed: connection error: {exc_str}",
                        exc_str,
                    )
                print(f"[ollama-wrapper] wrote failure result.json (url error: {exc})", flush=True)
                sys.exit(1)
            except json.JSONDecodeError as exc:
                _write_failure(
                    task_dir,
                    "ollama HTTP call failed: invalid JSON response",
                    str(exc),
                )
                print(f"[ollama-wrapper] wrote failure result.json (json decode error: {exc})", flush=True)
                sys.exit(1)
            except Exception as exc:
                _write_failure(
                    task_dir,
                    f"ollama HTTP call failed: {exc}",
                    str(exc),
                )
                print(f"[ollama-wrapper] wrote failure result.json (error: {exc})", flush=True)
                sys.exit(1)

            duration = time.time() - started
            raw = payload.get("response", "") or ""
            done_reason = payload.get("done_reason")
            prompt_eval_count = payload.get("prompt_eval_count")
            eval_count = payload.get("eval_count")
            print(
                f"[ollama-wrapper] done reason={done_reason} "
                f"prompt_eval={prompt_eval_count} eval={eval_count} "
                f"response_chars={len(raw)} duration_s={duration:.1f}",
                flush=True,
            )
            print(raw[:4000], flush=True)

            data = None
            if raw:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    m = _BARE_JSON_RE.search(raw)
                    if m:
                        candidate = m.group(1)
                        for end in range(len(candidate), 0, -1):
                            try:
                                data = json.loads(candidate[:end])
                                break
                            except json.JSONDecodeError:
                                pass

            if not isinstance(data, dict) or "status" not in data:
                truncated = done_reason and done_reason != "stop"
                reason = "truncated output" if truncated else "no valid result JSON"
                _write_failure(
                    task_dir,
                    f"ollama response had {reason} (done_reason={done_reason})",
                    raw[:3000],
                )
                print(f"[ollama-wrapper] wrote failure result.json ({reason})", flush=True)
                sys.exit(1)

            # Normalize to Result schema (extra=forbid). Map common aliases
            # and drop unknown keys so pydantic validation succeeds downstream.
            if "summary" not in data:
                for alias in ("message", "title", "description"):
                    if isinstance(data.get(alias), str):
                        data["summary"] = data.pop(alias)
                        break
            allowed = {
                "task_id", "status", "summary", "artifacts", "handoff",
                "verdict", "feedback", "tokens_in", "tokens_out",
                "duration_s", "cost_usd",
            }
            data = {k: v for k, v in data.items() if k in allowed}
            data.setdefault("task_id", os.path.basename(task_dir))
            data.setdefault("summary", f"{role} returned no summary")
            data.setdefault("artifacts", [])
            data.setdefault("handoff", None)
            data.setdefault("verdict", None)
            data.setdefault("feedback", None)
            data["tokens_in"] = prompt_eval_count if data.get("tokens_in") is None else data["tokens_in"]
            data["tokens_out"] = eval_count if data.get("tokens_out") is None else data["tokens_out"]
            data["duration_s"] = duration if not data.get("duration_s") else data["duration_s"]
            data.setdefault("cost_usd", None)

            out_path = os.path.join(task_dir, "result.json")
            with open(out_path, "w") as fh:
                json.dump(data, fh, indent=2)
            print(f"[ollama-wrapper] wrote result.json with status={data.get('status')}", flush=True)
        """)
