"""Tests for config module - untested paths from test_config_validation.py."""

from __future__ import annotations

import copy
import pytest
import yaml
from pathlib import Path

from mas.config import _deep_merge, _load_yaml, load_config, project_dir, project_root
from mas.errors import ConfigValidationError


PROVIDER_USER = {"cli": "user", "max_concurrent": 1, "extra_args": []}
PROVIDER_PROJ = {"cli": "proj", "max_concurrent": 1, "extra_args": []}
PROVIDER_CLAUDE = {"cli": "claude", "max_concurrent": 2, "extra_args": []}

ROLE_PROPOSER = {
    "provider": "claude-code",
    "model": "claude-haiku-4-5-20251001",
    "timeout_s": 600,
    "max_retries": 2,
}

VALID_CONFIG = {
    "providers": {"claude-code": PROVIDER_CLAUDE},
    "roles": {"proposer": {"provider": "claude-code"}},
    "max_proposed": 10,
}


@pytest.fixture
def mas_dir(tmp_path):
    mas = tmp_path / ".mas"
    mas.mkdir()
    return mas


def write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


class TestLoadYaml:
    def test_valid_yaml_parses_correctly(self, tmp_path):
        data = {"key": "value", "nested": {"a": 1, "b": 2}}
        path = tmp_path / "config.yaml"
        write_yaml(path, data)
        result = _load_yaml(path)
        assert result == data
        assert result["key"] == "value"
        assert result["nested"]["a"] == 1

    def test_missing_file_returns_empty_dict(self, tmp_path):
        path = tmp_path / "nonexistent.yaml"
        result = _load_yaml(path)
        assert result == {}

    def test_null_content_returns_empty_dict(self, tmp_path):
        path = tmp_path / "null.yaml"
        path.write_text("null\n")
        result = _load_yaml(path)
        assert result == {}

    def test_invalid_yaml_raises_yaml_error(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text(": invalid yaml syntax\n  - this:\n    is: broken")
        with pytest.raises(yaml.YAMLError):
            _load_yaml(path)


class TestDeepMerge:
    def test_overlay_overrides_base_scalar(self):
        base = {"a": 1, "b": 2}
        overlay = {"b": 3}
        result = _deep_merge(base, overlay)
        assert result == {"a": 1, "b": 3}

    def test_nested_dicts_merge_recursively(self):
        base = {"outer": {"inner": 1, "extra": 2}}
        overlay = {"outer": {"inner": 10}}
        result = _deep_merge(base, overlay)
        assert result == {"outer": {"inner": 10, "extra": 2}}

    def test_overlay_adds_new_keys(self):
        base = {"existing": 1}
        overlay = {"new": 2}
        result = _deep_merge(base, overlay)
        assert result == {"existing": 1, "new": 2}

    def test_overlay_replaces_dict_with_scalar(self):
        base = {"key": {"nested": "dict"}}
        overlay = {"key": "scalar"}
        result = _deep_merge(base, overlay)
        assert result == {"key": "scalar"}

    def test_empty_overlay_returns_base_unchanged(self):
        base = {"a": 1, "b": 2}
        overlay = {}
        result = _deep_merge(base, overlay)
        assert result == {"a": 1, "b": 2}


class TestProjectRoot:
    def test_finds_mas_in_current_dir(self, tmp_path, monkeypatch):
        mas = tmp_path / ".mas"
        mas.mkdir()
        subdir = mas / "subdir"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        result = project_root(subdir)
        assert result == tmp_path

    def test_finds_mas_in_ancestor(self, tmp_path, monkeypatch):
        mas = tmp_path / ".mas"
        mas.mkdir()
        subdir = tmp_path / "level1" / "level2"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        result = project_root(subdir)
        assert result == tmp_path

    def test_raises_error_when_no_mas_exists(self, tmp_path, monkeypatch):
        subdir = tmp_path / "nodir"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        with pytest.raises(FileNotFoundError):
            project_root(subdir)


class TestLoadConfigMerging:
    def test_project_config_overrides_user(self, tmp_path, monkeypatch):
        user_cfg = tmp_path / ".config" / "mas"
        user_cfg.mkdir(parents=True)
        user_cfg_data = {"providers": {"user-provider": PROVIDER_USER}}
        write_yaml(user_cfg / "config.yaml", user_cfg_data)

        mas = tmp_path / ".mas"
        mas.mkdir()
        proj_cfg_data = {
            "providers": {"project-provider": PROVIDER_PROJ},
            "roles": {"proposer": {"provider": "project-provider"}},
        }
        write_yaml(mas / "config.yaml", proj_cfg_data)

        monkeypatch.setattr("mas.config.USER_CONFIG_DIR", user_cfg)
        config = load_config(mas)
        assert "project-provider" in config.providers
        assert "user-provider" not in config.providers

    def test_user_config_preserved_when_project_lacks_it(self, tmp_path, monkeypatch):
        user_cfg = tmp_path / ".config" / "mas"
        user_cfg.mkdir(parents=True)
        user_cfg_data = {"providers": {"user-provider": PROVIDER_USER}}
        write_yaml(user_cfg / "config.yaml", user_cfg_data)

        mas = tmp_path / ".mas"
        mas.mkdir()
        proj_cfg_data = {"providers": {"proj-provider": PROVIDER_PROJ}, "roles": {}}
        write_yaml(mas / "config.yaml", proj_cfg_data)

        monkeypatch.setattr("mas.config.USER_CONFIG_DIR", user_cfg)
        config = load_config(mas)
        assert "user-provider" in config.providers

    def test_roles_extracted_from_roles_key(self, mas_dir, monkeypatch):
        roles_dict = {"roles": {"proposer": {"provider": "claude-code"}}}
        write_yaml(mas_dir / "config.yaml", VALID_CONFIG)
        write_yaml(mas_dir / "roles.yaml", roles_dict)
        monkeypatch.chdir(mas_dir.parent)
        config = load_config(mas_dir)
        assert "proposer" in config.roles

    def test_roles_without_roles_wrapper_is_handled(self, mas_dir, monkeypatch):
        roles_dict = {"proposer": {"provider": "claude-code"}}
        write_yaml(mas_dir / "config.yaml", VALID_CONFIG)
        write_yaml(mas_dir / "roles.yaml", roles_dict)
        monkeypatch.chdir(mas_dir.parent)
        config = load_config(mas_dir)
        assert "proposer" in config.roles


class TestLoadConfigMissingFiles:
    def test_missing_max_proposed_uses_default(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        del cfg["max_proposed"]
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        config = load_config(mas_dir)
        assert config.max_proposed == 10

    def test_user_config_dir_not_exists(self, mas_dir, monkeypatch, tmp_path):
        nonexistent = tmp_path / "nonexistent-user-config"
        monkeypatch.setattr("mas.config.USER_CONFIG_DIR", nonexistent)
        write_yaml(mas_dir / "config.yaml", VALID_CONFIG)
        write_yaml(mas_dir / "roles.yaml", {"roles": VALID_CONFIG["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        config = load_config(mas_dir)
        assert config.providers

    def test_roles_yaml_absent(self, mas_dir, monkeypatch):
        write_yaml(mas_dir / "config.yaml", VALID_CONFIG)
        monkeypatch.chdir(mas_dir.parent)
        config = load_config(mas_dir)
        assert "proposer" in config.roles


class TestSchemaEnforcement:
    def test_extra_fields_at_top_level_rejected(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        cfg["extra_toplevel_field"] = "not_allowed"
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(mas_dir)
        assert "extra_toplevel_field" in str(exc_info.value)

    def test_invalid_role_name_rejected(self, mas_dir, monkeypatch):
        roles = {"custom_role": {"provider": "claude-code"}}
        write_yaml(mas_dir / "config.yaml", VALID_CONFIG)
        write_yaml(mas_dir / "roles.yaml", {"roles": roles})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError):
            load_config(mas_dir)

    def test_provider_invalid_extra_args_type(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        cfg["providers"]["claude-code"]["extra_args"] = "not_a_list"
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(mas_dir)
        assert "extra_args" in str(exc_info.value)


class TestErrorQuality:
    def test_errors_contains_field_message_input(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        cfg["unknown_key"] = "value"
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(mas_dir)
        err = exc_info.value
        assert err.errors
        assert all("field" in e for e in err.errors)
        assert all("message" in e for e in err.errors)
        assert all("input" in e for e in err.errors)

    def test_to_user_friendly_has_numbered_items(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        cfg["unknown_key"] = "value"
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(mas_dir)
        friendly = exc_info.value.to_user_friendly()
        assert "1." in friendly or "\n1 " in friendly

    def test_to_user_friendly_has_hint(self, mas_dir, monkeypatch):
        cfg = copy.deepcopy(VALID_CONFIG)
        cfg["unknown_key"] = "value"
        write_yaml(mas_dir / "config.yaml", cfg)
        write_yaml(mas_dir / "roles.yaml", {"roles": cfg["roles"]})
        monkeypatch.chdir(mas_dir.parent)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(mas_dir)
        friendly = exc_info.value.to_user_friendly()
        assert "Hint" in friendly