"""Failing tests encoding the `mas webhooks test` CLI acceptance criteria.

Tests invoke mas.webhooks_cmd.webhooks_app via Typer's CliRunner.
HTTP transport is monkeypatched via urllib.request.urlopen (the same
transport used by the production fire path in mas.notify.fire_webhooks).
Config loading is monkeypatched via mas.webhooks_cmd.load_config so no
real .mas/ directory is required.

All tests that exercise command behaviour will fail against the stub
(which raises NotImplementedError) with AssertionError — not ImportError,
AttributeError, or fixture-not-found.
"""
from __future__ import annotations

import io
import json
import re
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from mas.schemas import MasConfig, ProviderConfig, RoleConfig, WebhookConfig
from mas.webhooks_cmd import webhooks_app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(webhooks: list[WebhookConfig]) -> MasConfig:
    return MasConfig(
        providers={"fake": ProviderConfig(cli="fake")},
        roles={"proposer": RoleConfig(provider="fake")},
        webhooks=webhooks,
    )


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"OK"):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def _url_from_req(req) -> str:
    return req.full_url if hasattr(req, "full_url") else str(req)


# ---------------------------------------------------------------------------
# (i) Two webhooks both return 200 -> table shows `2xx` twice, exit 0
# ---------------------------------------------------------------------------

class TestTwoWebhooks200:
    def test_exit_0_and_table_shows_2xx_twice(self, monkeypatch):
        cfg = _cfg([
            WebhookConfig(url="http://hook1.example/a", events=["test"]),
            WebhookConfig(url="http://hook2.example/b", events=["test"]),
        ])
        monkeypatch.setattr("mas.webhooks_cmd.load_config", lambda *a, **k: cfg)
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResponse(200))

        result = runner.invoke(webhooks_app, ["test"])

        assert result.exit_code == 0, (
            f"expected exit 0 when both webhooks return 200, "
            f"got {result.exit_code}. output: {result.stdout!r}"
        )
        count_2xx = result.stdout.count("2xx")
        assert count_2xx >= 2, (
            f"expected '2xx' to appear at least twice (one per webhook), "
            f"got {count_2xx}. output: {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# (ii) One webhook returns 500 -> Result col shows "500", Detail <=80 chars, exit 1
# ---------------------------------------------------------------------------

class TestWebhook500:
    def test_exit_1_shows_500_and_body_slice(self, monkeypatch):
        cfg = _cfg([WebhookConfig(url="http://hook.example/x", events=["test"])])
        monkeypatch.setattr("mas.webhooks_cmd.load_config", lambda *a, **k: cfg)

        body_bytes = b"Internal server fault: database connection pool exhausted"

        def fake_urlopen(*a, **k):
            raise urllib.error.HTTPError(
                "http://hook.example/x", 500, "Internal Server Error",
                {}, io.BytesIO(body_bytes),
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = runner.invoke(webhooks_app, ["test"])

        assert result.exit_code == 1, (
            f"expected exit 1 for 500 response, got {result.exit_code}. "
            f"output: {result.stdout!r}"
        )
        assert "500" in result.stdout, (
            f"expected '500' in table Result column, got: {result.stdout!r}"
        )
        # Detail column must include a slice of the response body (<=80 chars)
        body_str = body_bytes.decode()
        assert body_str[:20] in result.stdout, (
            f"expected beginning of response body in Detail column. "
            f"Looking for: {body_str[:20]!r}. Got: {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# (iii) One webhook raises Timeout -> row shows "timeout", exit 1
# ---------------------------------------------------------------------------

class TestWebhookTimeout:
    @pytest.mark.parametrize("exc", [
        socket.timeout("timed out"),
        urllib.error.URLError(socket.timeout("timed out")),
    ])
    def test_exit_1_shows_timeout(self, exc, monkeypatch):
        cfg = _cfg([WebhookConfig(url="http://hook.example/y", events=["test"])])
        monkeypatch.setattr("mas.webhooks_cmd.load_config", lambda *a, **k: cfg)

        def raise_it(*a, **k):
            raise exc

        monkeypatch.setattr(urllib.request, "urlopen", raise_it)

        result = runner.invoke(webhooks_app, ["test"])

        assert result.exit_code == 1, (
            f"expected exit 1 for timeout, got {result.exit_code}. "
            f"output: {result.stdout!r}"
        )
        assert "timeout" in result.stdout.lower(), (
            f"expected 'timeout' in table output, got: {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# (iv) events filter excludes --event -> row shows "skipped", NOT POSTed
#      other webhook returns 200 -> exit 0
# ---------------------------------------------------------------------------

class TestEventFilterSkip:
    def test_skipped_not_posted_exit_0_when_other_200(self, monkeypatch):
        skipped_url = "http://hook.example/skipped"
        posted_url = "http://hook.example/posted"
        cfg = _cfg([
            WebhookConfig(url=skipped_url, events=["done"]),   # won't match "test"
            WebhookConfig(url=posted_url, events=["test"]),    # will match
        ])
        monkeypatch.setattr("mas.webhooks_cmd.load_config", lambda *a, **k: cfg)

        posted_urls: list[str] = []

        def fake_urlopen(req, *a, **k):
            posted_urls.append(_url_from_req(req))
            return _FakeResponse(200)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = runner.invoke(webhooks_app, ["test", "--event", "test"])

        assert result.exit_code == 0, (
            f"expected exit 0 when only one webhook fires and returns 200, "
            f"got {result.exit_code}. output: {result.stdout!r}"
        )
        assert "skipped" in result.stdout.lower(), (
            f"expected 'skipped' in table for non-matching webhook, "
            f"got: {result.stdout!r}"
        )
        assert not any(skipped_url in u for u in posted_urls), (
            f"skipped webhook ({skipped_url}) must NOT be POSTed. "
            f"Actually posted: {posted_urls}"
        )


# ---------------------------------------------------------------------------
# (v) --url <u> filters to matching webhook; unknown --url exits 2 with stderr message
# ---------------------------------------------------------------------------

class TestUrlFilter:
    def test_url_filter_posts_only_to_matching(self, monkeypatch):
        url_a = "http://hook.example/alpha"
        url_b = "http://hook.example/beta"
        cfg = _cfg([
            WebhookConfig(url=url_a, events=["test"]),
            WebhookConfig(url=url_b, events=["test"]),
        ])
        monkeypatch.setattr("mas.webhooks_cmd.load_config", lambda *a, **k: cfg)

        posted_urls: list[str] = []

        def fake_urlopen(req, *a, **k):
            posted_urls.append(_url_from_req(req))
            return _FakeResponse(200)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = runner.invoke(webhooks_app, ["test", "--url", url_a])

        assert result.exit_code == 0, (
            f"expected exit 0 when target URL returns 200, "
            f"got {result.exit_code}. output: {result.stdout!r}"
        )
        assert len(posted_urls) == 1, (
            f"expected exactly 1 POST (to {url_a}), "
            f"got {len(posted_urls)}: {posted_urls}"
        )
        assert any(url_a in u for u in posted_urls), (
            f"expected POST to {url_a}, got: {posted_urls}"
        )
        assert not any(url_b in u for u in posted_urls), (
            f"expected {url_b} NOT to be POSTed, got: {posted_urls}"
        )

    def test_unknown_url_exits_2_with_error_on_stderr(self, monkeypatch):
        cfg = _cfg([WebhookConfig(url="http://hook.example/real", events=["test"])])
        monkeypatch.setattr("mas.webhooks_cmd.load_config", lambda *a, **k: cfg)

        unknown_url = "http://hook.example/does-not-exist"
        result = runner.invoke(webhooks_app, ["test", "--url", unknown_url])

        _stderr = getattr(result, "stderr", None)
        assert result.exit_code == 2, (
            f"expected exit 2 for unknown --url, got {result.exit_code}. "
            f"stdout: {result.stdout!r}  stderr: {_stderr!r}"
        )
        combined = (result.stdout + (_stderr or "")).lower()
        assert "no configured webhook" in combined or (
            "error" in combined and unknown_url in (result.stdout + (_stderr or ""))
        ), (
            f"expected error message about unknown webhook URL. "
            f"stdout: {result.stdout!r}  stderr: {_stderr!r}"
        )
        assert unknown_url in (result.stdout + (_stderr or "")), (
            f"expected the unknown URL in error output. "
            f"stdout: {result.stdout!r}  stderr: {_stderr!r}"
        )


# ---------------------------------------------------------------------------
# (vi) empty cfg.webhooks -> "No webhooks configured.", exit 0
# ---------------------------------------------------------------------------

class TestEmptyWebhooks:
    def test_no_webhooks_exit_0_with_message(self, monkeypatch):
        cfg = _cfg([])
        monkeypatch.setattr("mas.webhooks_cmd.load_config", lambda *a, **k: cfg)

        result = runner.invoke(webhooks_app, ["test"])

        assert result.exit_code == 0, (
            f"expected exit 0 when no webhooks configured, "
            f"got {result.exit_code}. output: {result.stdout!r}"
        )
        assert "no webhooks configured" in result.stdout.lower(), (
            f"expected 'No webhooks configured.' in output, "
            f"got: {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# (vii) --event defaults to "test"; filters=["test"] fire, filters=["done"] skip
# ---------------------------------------------------------------------------

class TestEventDefaultsToTest:
    def test_default_event_is_test(self, monkeypatch):
        posted_url = "http://hook.example/will-post"
        skipped_url = "http://hook.example/will-skip"
        cfg = _cfg([
            WebhookConfig(url=posted_url, events=["test"]),
            WebhookConfig(url=skipped_url, events=["done"]),
        ])
        monkeypatch.setattr("mas.webhooks_cmd.load_config", lambda *a, **k: cfg)

        posted_urls: list[str] = []

        def fake_urlopen(req, *a, **k):
            posted_urls.append(_url_from_req(req))
            return _FakeResponse(200)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = runner.invoke(webhooks_app, ["test"])  # no --event => defaults to "test"

        assert result.exit_code == 0, (
            f"expected exit 0 with default event 'test', "
            f"got {result.exit_code}. output: {result.stdout!r}"
        )
        assert any(posted_url in u for u in posted_urls), (
            f"expected {posted_url} (events=['test']) to be POSTed with default event. "
            f"Posted: {posted_urls}"
        )
        assert not any(skipped_url in u for u in posted_urls), (
            f"expected {skipped_url} (events=['done']) NOT to be POSTed with default event. "
            f"Posted: {posted_urls}"
        )


# ---------------------------------------------------------------------------
# (viii) Synthetic payload: exact keys, field values, task_id pattern, ISO8601 UTC timestamp
# ---------------------------------------------------------------------------

class TestSyntheticPayload:
    REQUIRED_KEYS = frozenset({
        "task_id", "role", "goal", "from", "to",
        "summary", "status", "timestamp", "task_dir", "_synthetic",
    })

    def test_payload_keys_and_values(self, monkeypatch):
        cfg = _cfg([WebhookConfig(url="http://hook.example/z", events=["test"])])
        monkeypatch.setattr("mas.webhooks_cmd.load_config", lambda *a, **k: cfg)

        captured: list[dict] = []

        def fake_urlopen(req, *a, **k):
            captured.append(json.loads(req.data))
            return _FakeResponse(200)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        runner.invoke(webhooks_app, ["test"])

        assert len(captured) == 1, (
            f"expected exactly 1 POST for 1 matching webhook, got {len(captured)}"
        )
        body = captured[0]

        missing = self.REQUIRED_KEYS - set(body.keys())
        assert not missing, f"payload missing required keys: {missing}. Got: {sorted(body.keys())}"

        extra = set(body.keys()) - self.REQUIRED_KEYS
        assert not extra, f"payload has unexpected extra keys: {extra}"

        assert re.match(r"^webhook-test-[0-9a-f]{8}$", body["task_id"]), (
            f"task_id must match ^webhook-test-[0-9a-f]{{8}}$, got: {body['task_id']!r}"
        )
        assert body["role"] == "proposer", f"expected role='proposer', got {body['role']!r}"
        assert body["from"] == "proposed", f"expected from='proposed', got {body['from']!r}"
        assert body["to"] == "doing", f"expected to='doing', got {body['to']!r}"
        assert body["status"] == "success", f"expected status='success', got {body['status']!r}"
        assert body["_synthetic"] is True, f"expected _synthetic=True, got {body['_synthetic']!r}"

        ts = body["timestamp"]
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            assert dt.tzinfo is not None, "timestamp must be timezone-aware (UTC)"
        except ValueError as exc:
            pytest.fail(f"timestamp is not valid ISO8601: {ts!r} -> {exc}")


# ---------------------------------------------------------------------------
# (ix) Regression: fire_webhooks (production fire path) must swallow exceptions
# ---------------------------------------------------------------------------

class TestProductionFirePathSwallows:
    """Regression: refactoring must not change the silent-by-design webhook error handling."""

    def test_fire_webhooks_swallows_http_error(self, monkeypatch):
        from mas.notify import fire_webhooks

        wh = WebhookConfig(url="http://hook.example/prod", events=["doing->done"])
        payload = {
            "from": "doing",
            "to": "done",
            "task_id": "test-task",
            "role": "implementer",
            "goal": "test goal",
            "summary": "ok",
            "status": "success",
            "task_dir": "/tmp/test-task",
        }

        def boom(*a, **k):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", boom)

        try:
            fire_webhooks([wh], payload)
        except Exception as exc:
            pytest.fail(
                f"fire_webhooks must swallow exceptions (production silent-by-design), "
                f"but raised {type(exc).__name__}: {exc}"
            )


# ---------------------------------------------------------------------------
# (x) mas webhooks --help lists `test`; mas webhooks test --help documents options
# ---------------------------------------------------------------------------

class TestHelpOutput:
    def test_webhooks_help_lists_test_command(self):
        result = runner.invoke(webhooks_app, ["--help"])
        assert result.exit_code == 0, (
            f"expected --help to exit 0, got {result.exit_code}"
        )
        # With @webhooks_app.callback() present, Typer renders a group help page that
        # includes a "Commands" section listing subcommands.  Without the callback,
        # Typer collapses the app so --help shows the single `test` command's own help
        # directly — no "Commands" section.  This distinguishes the two cases.
        assert "Commands" in result.stdout, (
            f"expected a 'Commands' section in `webhooks --help` (requires "
            f"@webhooks_app.callback() so Typer does not collapse the single-command "
            f"app). got: {result.stdout!r}"
        )
        assert "test" in result.stdout, (
            f"expected 'test' subcommand in webhooks --help, got: {result.stdout!r}"
        )

    def test_webhooks_test_help_documents_options(self):
        result = runner.invoke(webhooks_app, ["test", "--help"])
        assert result.exit_code == 0, (
            f"expected test --help to exit 0, got {result.exit_code}"
        )
        for opt in ("--url", "--event", "--timeout-s"):
            assert opt in result.stdout, (
                f"expected '{opt}' in `webhooks test --help`, got: {result.stdout!r}"
            )


# ---------------------------------------------------------------------------
# (xi) Shared HTTP helper: raw urllib.request.urlopen must live in one place
#      mas.notify._post_webhook is the shared helper; both fire_webhooks and
#      webhooks_cmd must delegate HTTP dispatch to it.
# ---------------------------------------------------------------------------

class TestSharedPostHelper:
    """Enforce that urllib.request.Request/urlopen lives in exactly one place."""

    def test_notify_exports_post_webhook_helper(self):
        import mas.notify
        helper = getattr(mas.notify, "_post_webhook", None)
        assert helper is not None, (
            "mas.notify must export a _post_webhook(url, data, timeout_s) helper "
            "so that both fire_webhooks and webhooks_cmd can reuse it instead of "
            "duplicating urllib.request.Request/urlopen"
        )
        assert callable(helper), "mas.notify._post_webhook must be callable"

    def test_webhooks_cmd_does_not_call_urlopen_directly(self):
        """webhooks_cmd must not duplicate urllib.request.urlopen — delegate to mas.notify._post_webhook."""
        import inspect
        import mas.webhooks_cmd
        source = inspect.getsource(mas.webhooks_cmd)
        assert "urllib.request.urlopen" not in source, (
            "webhooks_cmd must not call urllib.request.urlopen directly. "
            "HTTP dispatch must be delegated to mas.notify._post_webhook to avoid "
            "duplicating the Request/urlopen boilerplate from mas/notify.py"
        )

    def test_fire_webhooks_does_not_call_urlopen_directly(self):
        """fire_webhooks must not duplicate urllib.request.urlopen — delegate to mas.notify._post_webhook."""
        import inspect
        import mas.notify
        source = inspect.getsource(mas.notify.fire_webhooks)
        assert "urllib.request.urlopen" not in source, (
            "fire_webhooks must not call urllib.request.urlopen directly. "
            "HTTP dispatch must be delegated to mas.notify._post_webhook so the "
            "invocation lives in exactly one place"
        )


# ---------------------------------------------------------------------------
# (xii) Typer callback: webhooks_app must not be collapsed by Typer
#
# Typer silently collapses a Typer app that has exactly one command and no
# registered callback.  The collapsed app promotes the single command to the
# root entry point, so `runner.invoke(webhooks_app, ['test'])` treats the
# literal string 'test' as an unrecognised positional argument and Click exits
# with code 2 before the command body is entered.
#
# Fix: add `@webhooks_app.callback()` (with any body) to webhooks_cmd.py.
# With the callback registered, Typer keeps the subcommand layer and
# `runner.invoke(webhooks_app, ['test'])` dispatches into the `test` body.
# ---------------------------------------------------------------------------

class TestTyperCallbackPresent:
    """webhooks_app must have a registered callback to prevent Typer collapse."""

    def test_webhooks_help_usage_line_names_group_not_test(self):
        """--help must render a group usage line (e.g. 'Usage: webhooks ...'), not the collapsed single-command line ('Usage: test ...').

        Without @webhooks_app.callback(), Typer collapses the app and --help
        shows 'Usage: test [OPTIONS]'.  With the callback, it shows the group
        usage that includes 'COMMAND [ARGS]...' and a Commands section.
        """
        result = runner.invoke(webhooks_app, ["--help"])
        assert result.exit_code == 0, (
            f"expected exit 0 for --help, got {result.exit_code}"
        )
        usage_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        assert "COMMAND" in usage_line, (
            f"usage line must include 'COMMAND' (group-style help), not the collapsed "
            f"single-command line.  Got: {usage_line!r}. "
            f"Fix: add @webhooks_app.callback() to webhooks_cmd.py."
        )

    def test_invoking_test_subcommand_does_not_exit_2(self, monkeypatch):
        """runner.invoke(webhooks_app, ['test']) must not exit 2 (unknown positional).

        When Typer collapses the app, 'test' is treated as an unrecognised
        positional and Click exits 2 before the command body runs.  Adding
        @webhooks_app.callback() prevents collapse.
        """
        cfg = _cfg([])
        monkeypatch.setattr("mas.webhooks_cmd.load_config", lambda *a, **k: cfg)

        result = runner.invoke(webhooks_app, ["test"])
        assert result.exit_code != 2, (
            f"exit_code 2 means Typer collapsed webhooks_app and treated 'test' as an "
            f"unrecognised positional argument. "
            f"Fix: add @webhooks_app.callback() to webhooks_cmd.py. "
            f"stdout: {result.stdout!r}"
        )
