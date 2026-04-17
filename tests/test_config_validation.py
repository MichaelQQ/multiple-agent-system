"""Tests for config validation."""

from __future__ import annotations

import copy
import pytest
import yaml
from pathlib import Path

from mas.config import load_config
from mas.errors import ConfigValidationError


def make_valid_config():
    return {
        "providers": {
            "claude-code": {
                "cli": "claude",
                "max_concurrent": 2,
                "extra_args": [],
            },
            "opencode": {
                "cli": "opencode",
                "max_concurrent": 1,
                "extra_args": [],
            },
        },
        "roles": {
            "proposer": {
                "provider": "claude-code",
                "model": "claude-haiku-4-5-20251001",
                "timeout_s": 600,
                "max_retries": 2,
            },
            "implementer": {
                "provider": "opencode",
                "timeout_s": 3600,
                "max_retries": 2,
            },
        },
        "max_proposed": 10,
    }


VALID_CONFIG = make_valid_config()


@pytest.fixture
def mas_dir(tmp_path):
    mas = tmp_path / ".mas"
    mas.mkdir()
    return mas


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


class TestValidConfig:
    def test_loads_without_error(self, mas_dir, monkeypatch):
        cfg = make_valid_config()
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        config = load_config(project=mas_dir)
        assert "claude-code" in config.providers
        assert "opencode" in config.providers
        assert "proposer" in config.roles
        assert "implementer" in config.roles


class TestUnknownTopLevelKey:
    def test_raises_config_validation_error(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        cfg["unknown_toplevel_key"] = "value"
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(project=mas_dir)
        assert "unknown_toplevel_key" in str(exc_info.value)


class TestMissingRequiredKey:
    @pytest.mark.parametrize("missing_key", ["providers"])
    def test_raises_config_validation_error(self, missing_key, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        del cfg[missing_key]
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(project=mas_dir)
        assert "Field" in str(exc_info.value)
        assert missing_key in str(exc_info.value)


class TestInvalidType:
    def test_max_proposed_string_raises_error(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        cfg["max_proposed"] = "abc"
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(project=mas_dir)
        assert "Field" in str(exc_info.value)
        assert "max_proposed" in str(exc_info.value)

    def test_max_concurrent_string_raises_error(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        cfg["providers"]["claude-code"]["max_concurrent"] = "not_an_int"
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(project=mas_dir)
        assert "Field" in str(exc_info.value)
        assert "max_concurrent" in str(exc_info.value)


class TestUnknownFieldInProviderOrRole:
    def test_unknown_field_in_provider(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        cfg["providers"]["claude-code"]["unknown_field"] = "value"
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(project=mas_dir)
        assert "Field" in str(exc_info.value)
        assert "unknown_field" in str(exc_info.value)

    def test_unknown_field_in_role(self, mas_dir, monkeypatch):
        roles = copy.deepcopy(VALID_CONFIG["roles"])
        roles["proposer"]["unknown_role_field"] = "value"
        write_yaml(mas_dir / "config.yaml", VALID_CONFIG)
        write_yaml(mas_dir / "roles.yaml", {"roles": roles})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(project=mas_dir)
        assert "Field" in str(exc_info.value)
        assert "unknown_role_field" in str(exc_info.value)


class TestRoleReferenceUnknownProvider:
    def test_role_referencing_nonexistent_provider(self, mas_dir, monkeypatch):
        roles = copy.deepcopy(VALID_CONFIG["roles"])
        roles["proposer"]["provider"] = "nonexistent-provider"
        write_yaml(mas_dir / "config.yaml", VALID_CONFIG)
        write_yaml(mas_dir / "roles.yaml", {"roles": roles})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(project=mas_dir)
        err = exc_info.value
        assert "nonexistent-provider" in str(err)
        assert "proposer" in str(err)


class TestErrorMessages:
    def test_error_contains_field_path(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        cfg["max_proposed"] = "abc"
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(project=mas_dir)
        err = exc_info.value
        assert err.errors
        assert any("max_proposed" in e["field"] for e in err.errors)

    def test_to_user_friendly_includes_hint(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        cfg["unknown_toplevel_key"] = "value"
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(project=mas_dir)
        friendly = exc_info.value.to_user_friendly()
        assert "Hint" in friendly
        assert "config.yaml" in friendly or "roles.yaml" in friendly
