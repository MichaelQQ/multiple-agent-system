import json
import yaml
import pytest
from pathlib import Path
from typer.testing import CliRunner
from mas.cli import app

runner = CliRunner()


@pytest.fixture
def temp_mas(tmp_path, monkeypatch):
    mas = tmp_path / ".mas"
    mas.mkdir()

    # Patch project_dir/project_root in cli.py's namespace (used by config_show)
    monkeypatch.setattr("mas.cli.project_dir", lambda *args: mas)
    monkeypatch.setattr("mas.cli.project_root", lambda *args: tmp_path)
    # Patch in config.py's namespace (used by load_config internals)
    monkeypatch.setattr("mas.config.project_dir", lambda *args: mas)
    monkeypatch.setattr("mas.config.project_root", lambda *args: tmp_path)
    # Redirect user-level config to an empty temp dir so ~/.config/mas doesn't interfere
    user_config_dir = tmp_path / "user_config"
    user_config_dir.mkdir()
    monkeypatch.setattr("mas.config.USER_CONFIG_DIR", user_config_dir)

    return mas


def write_config(mas_dir, config_data=None, roles_data=None):
    if config_data is not None:
        (mas_dir / "config.yaml").write_text(yaml.dump(config_data))
    if roles_data is not None:
        (mas_dir / "roles.yaml").write_text(yaml.dump(roles_data))


def test_config_show_default_yaml(temp_mas):
    """(1) Default YAML output has top-level keys config: and roles:, includes pydantic defaults."""
    config_data = {
        "providers": {
            "test-provider": {"cli": "test-cli"}
        },
        "roles": {
            "proposer": {"provider": "test-provider", "model": "test-model"}
        }
    }
    write_config(temp_mas, config_data)

    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.output

    output = yaml.safe_load(result.output)
    assert "config" in output
    assert "roles" in output

    # Pydantic-applied defaults must appear even when not in user's config.yaml
    assert output["config"]["daemon"]["log_max_bytes"] == 10485760
    assert output["config"]["daemon"]["log_backup_count"] == 5
    assert output["roles"]["proposer"]["timeout_s"] == 1800


def test_config_show_json(temp_mas):
    """(2) --json emits valid JSON that round-trips into the same dict shape as YAML output."""
    config_data = {
        "providers": {"p1": {"cli": "c1"}},
        "roles": {"proposer": {"provider": "p1"}}
    }
    write_config(temp_mas, config_data)

    result_yaml = runner.invoke(app, ["config", "show"])
    result_json = runner.invoke(app, ["config", "show", "--json"])

    assert result_yaml.exit_code == 0, result_yaml.output
    assert result_json.exit_code == 0, result_json.output

    from_yaml = yaml.safe_load(result_yaml.output)
    from_json = json.loads(result_json.output)

    assert "config" in from_json
    assert "roles" in from_json
    assert from_json["config"]["daemon"]["log_max_bytes"] == 10485760
    # JSON and YAML representations should decode to the same dict
    assert from_json == from_yaml


def test_config_show_mutually_exclusive(temp_mas):
    """(3) --yaml and --json are mutually exclusive: exits non-zero with a clear error message."""
    result = runner.invoke(app, ["config", "show", "--yaml", "--json"])
    assert result.exit_code != 0
    # The error output must mention the conflicting flags or "mutually exclusive"
    combined = (result.output or "") + (result.stderr or "")
    assert any(kw in combined.lower() for kw in ("mutually exclusive", "cannot", "--yaml", "--json"))


def test_config_show_field_scalar(temp_mas):
    """(4a) --field config.daemon.log_max_bytes prints the scalar value and exits 0."""
    config_data = {
        "providers": {"p1": {"cli": "c1"}},
        "roles": {"proposer": {"provider": "p1"}},
    }
    write_config(temp_mas, config_data)

    result = runner.invoke(app, ["config", "show", "--field", "config.daemon.log_max_bytes"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "10485760"


def test_config_show_field_roles(temp_mas):
    """(4b) --field roles.<role>.<attr> reads from the roles section."""
    config_data = {
        "providers": {"p1": {"cli": "c1"}},
        "roles": {
            "proposer": {"provider": "p1"},
            "implementer": {"provider": "p1", "timeout_s": 3600},
        }
    }
    write_config(temp_mas, config_data)

    result = runner.invoke(app, ["config", "show", "--field", "roles.implementer.timeout_s"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "3600"

    result2 = runner.invoke(app, ["config", "show", "--field", "roles.proposer.provider"])
    assert result2.exit_code == 0, result2.output
    assert result2.output.strip() == "p1"


def test_config_show_field_list_index(temp_mas):
    """(4c) --field config.webhooks.0.url does list indexing."""
    config_data = {
        "providers": {"p1": {"cli": "c1"}},
        "roles": {"proposer": {"provider": "p1"}},
        "webhooks": [{"url": "http://example.com/hook"}],
    }
    write_config(temp_mas, config_data)

    result = runner.invoke(app, ["config", "show", "--field", "config.webhooks.0.url"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "http://example.com/hook"


def test_config_show_field_missing(temp_mas):
    """(4d) Unknown dotted path exits 2 with a clear error on stderr."""
    config_data = {
        "providers": {"p1": {"cli": "c1"}},
        "roles": {"proposer": {"provider": "p1"}}
    }
    write_config(temp_mas, config_data)

    result = runner.invoke(app, ["config", "show", "--field", "config.does.not.exist"])
    assert result.exit_code == 2
    assert "not found" in result.stderr.lower()


def test_config_show_key_order_stable(temp_mas):
    """(1b) Key order is stable across two consecutive runs."""
    config_data = {
        "providers": {"p1": {"cli": "c1"}},
        "roles": {"proposer": {"provider": "p1"}}
    }
    write_config(temp_mas, config_data)

    result1 = runner.invoke(app, ["config", "show"])
    result2 = runner.invoke(app, ["config", "show"])

    assert result1.exit_code == 0, result1.output
    assert result2.exit_code == 0, result2.output
    assert result1.output == result2.output


def test_config_show_secret_masking(temp_mas):
    """(5) Sensitive URL query params masked to *** by default."""
    config_data = {
        "providers": {"p1": {"cli": "c1"}},
        "roles": {"proposer": {"provider": "p1"}},
        "webhooks": [
            {"url": "http://example.com/hook?token=abc123&other=visible"},
            {"url": "http://example.com/hook2?password=s3cr3t&x=1"},
        ],
    }
    write_config(temp_mas, config_data)

    result = runner.invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)

    url0 = output["config"]["webhooks"][0]["url"]
    assert "token=***" in url0
    assert "abc123" not in url0
    assert "other=visible" in url0

    url1 = output["config"]["webhooks"][1]["url"]
    assert "password=***" in url1
    assert "s3cr3t" not in url1


def test_config_show_mask_helpers():
    """(5) _mask_secrets and _mask_url_query helpers mask correctly on synthetic data."""
    from mas.cli import _mask_secrets, _mask_url_query

    # _mask_url_query: sensitive query params masked
    url = _mask_url_query("http://host/path?token=abc&x=1&secret=xyz&normal=ok")
    assert "token=***" in url
    assert "secret=***" in url
    assert "normal=ok" in url
    assert "abc" not in url
    assert "xyz" not in url

    # _mask_secrets: dict with sensitive keys masked, others untouched
    data = {"api_key": "topsecret", "name": "alice", "token": "xyz", "count": 5}
    masked = _mask_secrets(data)
    assert masked["api_key"] == "***"
    assert masked["token"] == "***"
    assert masked["name"] == "alice"
    assert masked["count"] == 5

    # nested dict
    nested = {"outer": {"password": "pass123", "label": "ok"}}
    masked2 = _mask_secrets(nested)
    assert masked2["outer"]["password"] == "***"
    assert masked2["outer"]["label"] == "ok"


def test_config_show_unsafe_secrets(temp_mas):
    """(5) --unsafe-show-secrets reveals original sensitive values in webhook URLs."""
    config_data = {
        "providers": {"p1": {"cli": "c1"}},
        "roles": {"proposer": {"provider": "p1"}},
        "webhooks": [{"url": "http://example.com/hook?token=abc123"}],
    }
    write_config(temp_mas, config_data)

    result = runner.invoke(app, ["config", "show", "--json", "--unsafe-show-secrets"])
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)

    assert "token=abc123" in output["config"]["webhooks"][0]["url"]


def test_config_show_invalid_yaml(temp_mas):
    """(6) Malformed YAML: error on stderr, exit non-zero, no partial stdout."""
    (temp_mas / "config.yaml").write_text("invalid: [yaml: structure")

    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code != 0
    assert "error" in result.stderr.lower()
    assert result.stdout == ""


def test_config_show_schema_violation(temp_mas):
    """(6) Schema/cross-field validation error: validation issue on stderr, exit non-zero, no stdout."""
    config_data = {
        "providers": {"p1": {"cli": "c1"}},
        "roles": {"proposer": {"provider": "unknown-provider"}}
    }
    write_config(temp_mas, config_data)

    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code != 0
    assert "validation" in result.stderr.lower()
    assert result.stdout == ""
