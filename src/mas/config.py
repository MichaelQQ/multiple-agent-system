from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schemas import MasConfig


USER_CONFIG_DIR = Path.home() / ".config" / "mas"
PROJECT_DIR_NAME = ".mas"


def project_root(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
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
    user_cfg = _load_yaml(USER_CONFIG_DIR / "config.yaml")
    user_roles = _load_yaml(USER_CONFIG_DIR / "roles.yaml")
    proj_cfg = _load_yaml(proj / "config.yaml")
    proj_roles = _load_yaml(proj / "roles.yaml")

    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, user_cfg)
    merged = _deep_merge(merged, proj_cfg)
    roles = _deep_merge(user_roles, proj_roles)
    if roles:
        merged["roles"] = roles.get("roles", roles)
    return MasConfig.model_validate(merged)
