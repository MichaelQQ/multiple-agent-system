from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from .base import Adapter, DispatchHandle


class OllamaAdapter(Adapter):
    """Non-agentic text provider.

    Launches a detached Python wrapper that:
      1. Pipes the prompt to ``ollama run <model>``
      2. Extracts the first JSON object from the response
      3. Writes it as ``result.json`` in ``task_dir``

    If JSON extraction fails the wrapper writes a failure result so the
    orchestrator can retry rather than waiting for orphan detection.
    """

    name = "ollama"
    agentic = False

    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
        cli = self.provider_cfg.cli or "ollama"
        model = self.role_cfg.model or "llama3.2"
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
        cli = self.provider_cfg.cli or "ollama"
        model = self.role_cfg.model or "llama3.2"
        extra = list(self.provider_cfg.extra_args)

        base_prompt = stdin_text if stdin_text is not None else prompt
        # Non-agentic: model cannot write files — instruct it to reply with
        # the result JSON directly so the wrapper can extract it.
        effective_prompt = (
            base_prompt
            + "\n\nIMPORTANT: You cannot write files. Respond with ONLY a "
            "valid JSON object (no markdown prose, no extra text) that "
            "represents result.json for this task."
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
        return textwrap.dedent("""\
            import json, os, re, subprocess, sys

            _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ollama_config.json")
            with open(_cfg_path) as _f:
                cfg = json.load(_f)
            cli, model, extra_args = cfg["cli"], cfg["model"], cfg["extra_args"]
            prompt, task_dir, role = cfg["prompt"], cfg["task_dir"], cfg["role"]

            cmd = [cli, "run", model] + extra_args
            print(f"[ollama-wrapper] running {{' '.join(cmd)}}", flush=True)

            try:
                r = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=3600,
                )
            except Exception as exc:
                result = {{
                    "task_id": os.path.basename(task_dir),
                    "status": "failure",
                    "summary": f"ollama invocation failed: {{exc}}",
                    "artifacts": [],
                    "handoff": None,
                    "verdict": None,
                    "feedback": str(exc),
                    "tokens_in": None,
                    "tokens_out": None,
                    "duration_s": 0.0,
                    "cost_usd": None,
                }}
                out_path = os.path.join(task_dir, "result.json")
                with open(out_path, "w") as fh:
                    json.dump(result, fh, indent=2)
                print(f"[ollama-wrapper] wrote failure result.json (invocation error)", flush=True)
                sys.exit(1)

            raw = r.stdout
            if r.stderr:
                print("[ollama-wrapper] stderr:", r.stderr[:2000], flush=True)
            print(f"[ollama-wrapper] raw output ({{len(raw)}} chars):", flush=True)
            print(raw[:4000], flush=True)

            # Extract JSON: prefer fenced block, fall back to first bare object.
            data = None
            m = re.search(r"```(?:json)?\\s*(\\{{[\\s\\S]*?\\}})\\s*```", raw)
            if m:
                candidate = m.group(1)
            else:
                m = re.search(r"(\\{{[\\s\\S]*\\}})", raw)
                candidate = m.group(1) if m else None

            if candidate:
                try:
                    data = json.loads(candidate)
                except json.JSONDecodeError:
                    # Try trimming trailing garbage
                    for end in range(len(candidate), 0, -1):
                        try:
                            data = json.loads(candidate[:end])
                            break
                        except json.JSONDecodeError:
                            pass

            if not isinstance(data, dict) or "status" not in data:
                result = {{
                    "task_id": os.path.basename(task_dir),
                    "status": "failure",
                    "summary": "ollama response contained no valid result JSON",
                    "artifacts": [],
                    "handoff": None,
                    "verdict": None,
                    "feedback": raw[:3000],
                    "tokens_in": None,
                    "tokens_out": None,
                    "duration_s": 0.0,
                    "cost_usd": None,
                }}
                out_path = os.path.join(task_dir, "result.json")
                with open(out_path, "w") as fh:
                    json.dump(result, fh, indent=2)
                print("[ollama-wrapper] wrote failure result.json (no JSON found)", flush=True)
                sys.exit(1)

            data.setdefault("task_id", os.path.basename(task_dir))
            data.setdefault("artifacts", [])
            data.setdefault("handoff", None)
            data.setdefault("verdict", None)
            data.setdefault("feedback", None)
            data.setdefault("tokens_in", None)
            data.setdefault("tokens_out", None)
            data.setdefault("duration_s", 0.0)
            data.setdefault("cost_usd", None)

            out_path = os.path.join(task_dir, "result.json")
            with open(out_path, "w") as fh:
                json.dump(data, fh, indent=2)
            print(f"[ollama-wrapper] wrote result.json with status={{data.get('status')}}", flush=True)
        """)
