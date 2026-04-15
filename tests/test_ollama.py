import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mas.adapters.ollama import OllamaAdapter
from mas.schemas import Result


class _MockOllama:
    def __init__(self):
        self.response_payload = {"response": "{}", "done_reason": "stop"}
        self.response_status = 200
        self.last_body = None
        self.last_path = None

    def make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                outer.last_path = self.path
                outer.last_body = json.loads(raw.decode()) if raw else None
                self.send_response(outer.response_status)
                self.send_header("Content-Type", "application/json")
                body = json.dumps(outer.response_payload).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


@pytest.fixture
def mock_ollama():
    mock = _MockOllama()
    server = ThreadingHTTPServer(("127.0.0.1", 0), mock.make_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    mock.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield mock
    finally:
        server.shutdown()
        server.server_close()


def _run_wrapper(tmp_path, mock_url, env_overrides=None):
    task_dir = tmp_path / "taskdir-abc"
    task_dir.mkdir()
    (task_dir / "_ollama_config.json").write_text(
        json.dumps(
            {
                "cli": "ollama",
                "model": "test-model",
                "extra_args": [],
                "prompt": "hello",
                "task_dir": str(task_dir),
                "role": "proposer",
            }
        )
    )
    wrapper_path = task_dir / "_ollama_wrapper.py"
    wrapper_path.write_text(OllamaAdapter._wrapper_source())

    env = {
        "PATH": "/usr/bin:/bin",
        "OLLAMA_HOST": mock_url,
    }
    if env_overrides:
        env.update(env_overrides)

    proc = subprocess.run(
        [sys.executable, str(wrapper_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return task_dir, proc


def _read_result(task_dir):
    return json.loads((task_dir / "result.json").read_text())


def test_wrapper_source_compiles():
    src = OllamaAdapter._wrapper_source()
    compile(src, "<wrapper>", "exec")


def test_wrapper_success_writes_result(tmp_path, mock_ollama):
    mock_ollama.response_payload = {
        "response": json.dumps(
            {
                "status": "success",
                "summary": "a task summary",
                "handoff": {"goal": "x"},
            }
        ),
        "done_reason": "stop",
        "prompt_eval_count": 123,
        "eval_count": 45,
    }
    task_dir, proc = _run_wrapper(tmp_path, mock_ollama.url)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    result = _read_result(task_dir)
    assert result["status"] == "success"
    assert result["summary"] == "a task summary"
    assert result["handoff"] == {"goal": "x"}
    assert result["task_id"] == "taskdir-abc"
    assert result["tokens_in"] == 123
    assert result["tokens_out"] == 45
    assert result["artifacts"] == []
    assert result["verdict"] is None


def test_wrapper_sends_expected_request_body(tmp_path, mock_ollama):
    mock_ollama.response_payload = {
        "response": json.dumps({"status": "success", "summary": "ok"}),
        "done_reason": "stop",
    }
    _run_wrapper(
        tmp_path,
        mock_ollama.url,
        env_overrides={
            "MAS_OLLAMA_NUM_PREDICT": "2048",
            "MAS_OLLAMA_NUM_CTX": "16384",
            "MAS_OLLAMA_TEMPERATURE": "0.7",
        },
    )
    assert mock_ollama.last_path == "/api/generate"
    body = mock_ollama.last_body
    assert body["model"] == "test-model"
    assert body["prompt"] == "hello"
    assert body["format"] == "json"
    assert body["stream"] is False
    assert body["options"]["num_predict"] == 2048
    assert body["options"]["num_ctx"] == 16384
    assert body["options"]["temperature"] == 0.7


def test_wrapper_truncation_writes_failure(tmp_path, mock_ollama):
    mock_ollama.response_payload = {
        "response": '{"status": "success", "summary": "partial',
        "done_reason": "length",
    }
    task_dir, proc = _run_wrapper(tmp_path, mock_ollama.url)
    assert proc.returncode == 1

    result = _read_result(task_dir)
    assert result["status"] == "failure"
    assert "truncated" in result["summary"].lower()
    assert "done_reason=length" in result["summary"]


def test_wrapper_http_error_writes_failure(tmp_path, mock_ollama):
    mock_ollama.response_status = 500
    mock_ollama.response_payload = {"error": "boom"}
    task_dir, proc = _run_wrapper(tmp_path, mock_ollama.url)
    assert proc.returncode == 1

    result = _read_result(task_dir)
    assert result["status"] == "failure"
    assert "ollama HTTP call failed" in result["summary"]


def test_wrapper_recovers_json_from_wrapped_response(tmp_path, mock_ollama):
    # format=json normally gives pure JSON, but if the model emits prose
    # around it (e.g. when format is ignored), the bare-JSON fallback should
    # still recover the object.
    mock_ollama.response_payload = {
        "response": 'prose before {"status": "success", "summary": "hi"} trailing',
        "done_reason": "stop",
    }
    task_dir, proc = _run_wrapper(tmp_path, mock_ollama.url)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    result = _read_result(task_dir)
    assert result["status"] == "success"
    assert result["summary"] == "hi"


def test_wrapper_maps_message_alias_to_summary(tmp_path, mock_ollama):
    # Models sometimes emit "message" instead of "summary"; wrapper should
    # rename it so the Result schema (extra=forbid, summary required) validates.
    mock_ollama.response_payload = {
        "response": json.dumps(
            {"status": "success", "message": "did the thing"}
        ),
        "done_reason": "stop",
    }
    task_dir, proc = _run_wrapper(tmp_path, mock_ollama.url)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    result = _read_result(task_dir)
    assert result["summary"] == "did the thing"
    assert "message" not in result
    # Must validate against the real Result schema.
    Result.model_validate(result)


def test_wrapper_drops_unknown_keys(tmp_path, mock_ollama):
    mock_ollama.response_payload = {
        "response": json.dumps(
            {
                "status": "success",
                "summary": "ok",
                "notes": "extra chatter",
                "random_field": 42,
            }
        ),
        "done_reason": "stop",
    }
    task_dir, proc = _run_wrapper(tmp_path, mock_ollama.url)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    result = _read_result(task_dir)
    assert "notes" not in result
    assert "random_field" not in result
    Result.model_validate(result)


def test_wrapper_missing_status_field_writes_failure(tmp_path, mock_ollama):
    mock_ollama.response_payload = {
        "response": json.dumps({"summary": "no status here"}),
        "done_reason": "stop",
    }
    task_dir, proc = _run_wrapper(tmp_path, mock_ollama.url)
    assert proc.returncode == 1

    result = _read_result(task_dir)
    assert result["status"] == "failure"
    assert "no valid result JSON" in result["summary"]
