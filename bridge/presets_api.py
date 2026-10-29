"""预设的接口层：列、保存、应用、删除、回滚。

界面在「计算参数」页和「费率」页都挂了这套，
因为一份预设可以同时装着参数口径和各国费率。
"""

from __future__ import annotations

from src import presets


def list_presets(scope: str = "") -> dict:
    """列出预设。scope 传店铺 id 时，该店预设排前面。"""
    items = presets.list_all(scope)
    return {"ok": True, "items": items, "scope": scope or "global"}


def save_preset(payload: dict) -> dict:
    """把当前配置存成预设。"""
    return presets.capture(
        str(payload.get("name") or ""),
        scope=str(payload.get("scope") or "global"),
        include_rates=bool(payload.get("include_rates")),
        desc=str(payload.get("desc") or ""),
    )


def update_preset(payload: dict) -> dict:
    return presets.update(
        str(payload.get("preset_id") or ""),
        name=str(payload.get("name") or ""),
        desc=str(payload.get("desc") or ""),
        include_rates=payload.get("include_rates"),
    )


def apply_preset(payload: dict) -> dict:
    return presets.apply(str(payload.get("preset_id") or ""),
                         country=str(payload.get("country") or ""))


def delete_preset(payload: dict) -> dict:
    return presets.delete(str(payload.get("preset_id") or ""))


def list_backups() -> dict:
    return {"ok": True, "items": presets.backups()}


def rollback(payload: dict) -> dict:
    return presets.rollback(str(payload.get("name") or ""))
