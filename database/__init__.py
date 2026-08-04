from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType


def _get_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        import streamlit as st

        return str(st.secrets.get(name, "")).strip()
    except Exception:
        return ""


def using_supabase() -> bool:
    return bool(
        _get_secret("SUPABASE_URL")
        and (
            _get_secret("SUPABASE_SECRET_KEY")
            or _get_secret("SUPABASE_SERVICE_ROLE_KEY")
        )
    )


def _load_legacy_module() -> ModuleType:
    legacy_path = Path(__file__).resolve().parent.parent / "database.py"
    spec = importlib.util.spec_from_file_location(
        "ruleflow_legacy_database",
        legacy_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载本地数据库模块：{legacy_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if using_supabase():
    from .supabase_backend import *  # noqa: F401,F403
else:
    _legacy = _load_legacy_module()
    for _name in dir(_legacy):
        if not _name.startswith("__"):
            globals()[_name] = getattr(_legacy, _name)
