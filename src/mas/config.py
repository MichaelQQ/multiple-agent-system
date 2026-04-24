from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError
from pydantic_core import ValidationError as PydanticCoreValidationError

from .errors import ConfigValidationError
from .schemas import MasConfig, ValidationIssue

log = logging.getLogger("mas.config")


USER_CONFIG_DIR = Path.home() / ".config" / "mas"
PROJECT_DIR_NAME = ".mas"


def project_root(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    if (start / ".git").is_dir():
        for p in [start, start.parent, *start.parents]:
            if (p / PROJECT_DIR_NAME).is_dir():
                return p
    for p in [start, *start.parents]:
        if (p / PROJECT_DIR_NAME).is_dir():
            return p
    raise FileNotFoundError(f"no .mas/ found from {start}")


def project_dir(start: Path | None = None) -> Path:
    return project_root(start) / PROJECT_DIR_NAME


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def load_config(project: Path | None = None) -> MasConfig:
    proj = project or project_dir()
    yaml_failed = False

    def _safe(path: Path) -> dict[str, Any]:
        nonlocal yaml_failed
        try:
            return _load_yaml(path)
        except yaml.YAMLError as e:
            log.warning("invalid YAML in %s: %s", path, e)
            yaml_failed = True
            return {}

    user_cfg = _safe(USER_CONFIG_DIR / "config.yaml")
    user_roles = _safe(USER_CONFIG_DIR / "roles.yaml")
    proj_cfg = _safe(proj / "config.yaml")
    proj_roles = _safe(proj / "roles.yaml")

    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, user_cfg)
    proj_cfg_roles = proj_cfg.get("roles", {})
    proj_cfg_providers = proj_cfg.get("providers", {})
    if proj_cfg_providers and proj_cfg_roles:
        merged["providers"] = proj_cfg_providers
    else:
        merged = _deep_merge(merged, proj_cfg)
    for k, v in proj_cfg.items():
        if k != "providers":
            merged[k] = v
    roles = _deep_merge(user_roles, proj_roles)
    if roles:
        merged["roles"] = roles.get("roles", roles)
    log.debug("config loaded", extra={"path": str(proj / "config.yaml")})

    try:
        config = MasConfig.model_validate(merged)
    except (PydanticValidationError, PydanticCoreValidationError) as e:
        if yaml_failed:
            config = MasConfig(providers={}, roles={}, proposer_signals={}, max_proposed=10)
        else:
            raise ConfigValidationError.from_pydantic(e) from e

    _validate_cross_field_constraints(config)

    return config


def _validate_cross_field_constraints(config: MasConfig) -> None:
    errors: list[dict] = []
    for role_name, role_cfg in config.roles.items():
        if role_cfg.provider not in config.providers:
            errors.append({
                "field": f"roles.{role_name}.provider",
                "message": f"Provider '{role_cfg.provider}' is not defined in the providers section",
                "input": role_cfg.provider,
            })
    if errors:
        lines = ["Configuration validation failed:"]
        for e in errors:
            lines.append(f"  - Field '{e['field']}': {e['message']}")
            lines.append(f"    Received: {e['input']}")
            lines.append(f"    Available providers: {list(config.providers.keys())}")
        raise ConfigValidationError(message="\n".join(lines), errors=errors)


def _config_has_content(cfg: MasConfig) -> bool:
    return bool(cfg.providers and cfg.roles)


def validate_config(cfg: MasConfig, mas_dir: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if not _config_has_content(cfg):
        issues.append(ValidationIssue(
            field="config",
            message="Configuration is empty or missing required fields (providers, roles)",
        ))
        return issues

    for name, prov_cfg in cfg.providers.items():
        if prov_cfg.cli == "unknown" or shutil.which(prov_cfg.cli) is None:
            issues.append(ValidationIssue(
                field=f"providers.{name}.cli",
                message=f"CLI '{prov_cfg.cli}' not found in PATH",
            ))

    for role in cfg.roles:
        prompt_path = mas_dir / "prompts" / f"{role}.md"
        if not prompt_path.exists():
            issues.append(ValidationIssue(
                field=f"prompts/{role}.md",
                message=f"Prompt template not found",
            ))

    for i, wh in enumerate(cfg.webhooks):
        if not (wh.url.startswith("http://") or wh.url.startswith("https://")):
            issues.append(ValidationIssue(
                field=f"webhooks[{i}].url",
                message=f"Webhook URL must use http or https scheme, got: {wh.url!r}",
            ))

    return issues


def validate_environment(mas_dir: Path) -> list[ValidationIssue]:
    try:
        cfg = load_config(mas_dir)
    except ConfigValidationError as e:
        if e.errors:
            return [ValidationIssue(field=e.errors[0]["field"], message=e.errors[0]["message"])]
        return [ValidationIssue(field="config", message=str(e))]
    except yaml.YAMLError as e:
        return [ValidationIssue(field="config.yaml", message=str(e))]
    except Exception as e:
        return [ValidationIssue(field="config", message=str(e))]
    return validate_config(cfg, mas_dir)


def _get_config_value(cfg: MasConfig, path: str) -> Any:
    parts = path.split(".")
    value = cfg.model_dump()
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def config_diff(old_cfg: MasConfig, new_cfg: MasConfig) -> list[tuple[str, str, str]]:
    """Compare two MasConfig instances and return changes as (field_path, old_value, new_value)."""
    changes: list[tuple[str, str, str]] = []

    old_providers = old_cfg.model_dump().get("providers", {})
    new_providers = new_cfg.model_dump().get("providers", {})
    all_provider_names = set(old_providers.keys()) | set(new_providers.keys())
    for name in all_provider_names:
        if name not in old_providers:
            changes.append((f"providers.{name}", "<missing>", "<added>"))
        elif name not in new_providers:
            changes.append((f"providers.{name}", "<exists>", "<missing>"))
        else:
            old_p = old_providers[name]
            new_p = new_providers[name]
            for field in {"cli", "max_concurrent", "extra_args"}:
                old_val = old_p.get(field) if isinstance(old_p, dict) else getattr(old_p, field, None)
                new_val = new_p.get(field) if isinstance(new_p, dict) else getattr(new_p, field, None)
                if old_val != new_val:
                    changes.append((f"providers.{name}.{field}", str(old_val), str(new_val)))

    old_roles = old_cfg.model_dump().get("roles", {})
    new_roles = new_cfg.model_dump().get("roles", {})
    all_role_names = set(old_roles.keys()) | set(new_roles.keys())
    for name in all_role_names:
        if name not in old_roles:
            changes.append((f"roles.{name}", "<missing>", "<added>"))
        elif name not in new_roles:
            changes.append((f"roles.{name}", "<exists>", "<missing>"))
        else:
            old_r = old_roles[name]
            new_r = new_roles[name]
            for field in {"provider", "model", "timeout_s", "max_retries", "allowed_tools", "permission_mode", "extra_args"}:
                old_val = old_r.get(field) if isinstance(old_r, dict) else getattr(old_r, field, None)
                new_val = new_r.get(field) if isinstance(new_r, dict) else getattr(new_r, field, None)
                if old_val != new_val:
                    changes.append((f"roles.{name}.{field}", str(old_val), str(new_val)))

    for field in {"max_proposed", "proposal_similarity_threshold"}:
        old_val = getattr(old_cfg, field, None)
        new_val = getattr(new_cfg, field, None)
        if old_val != new_val:
            changes.append((field, str(old_val), str(new_val)))

    return changes


class ConfigWatcher:
    """Tracks config.yaml mtime and reports whether the file changed since last check."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self._last_mtime: float | None = None
        if self.config_path.exists():
            self._last_mtime = self.config_path.stat().st_mtime

    def has_changed(self) -> bool:
        if not self.config_path.exists():
            return False
        current_mtime = self.config_path.stat().st_mtime
        if self._last_mtime is None:
            return False
        return current_mtime != self._last_mtime

    def mark_checked(self) -> None:
        if self.config_path.exists():
            self._last_mtime = self.config_path.stat().st_mtime
