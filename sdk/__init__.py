"""官方 ``maibot-plugin-sdk`` 2.7.1 的运行时准入验证。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from ..errors import MaiBotUnitAdmissionError

SDK_VERSION = "2.7.1"


def require_official_sdk() -> Any:
    """导入并验证官方 MaiBot SDK，绝不注入本地 shim 或 YesMai 模块。"""
    try:
        sdk = import_module("maibot_sdk")
    except ImportError as exc:
        raise MaiBotUnitAdmissionError(
            "缺少官方依赖 maibot-plugin-sdk==2.7.1"
        ) from exc
    actual_version = str(getattr(sdk, "__version__", "")).strip()
    if actual_version != SDK_VERSION:
        raise MaiBotUnitAdmissionError(
            f"MaiBot SDK 版本必须为 {SDK_VERSION}，当前为 {actual_version or '<unknown>'}"
        )
    required = ("MaiBotPlugin", "PluginContext", "PluginPaths", "Tool", "Command", "Action")
    missing = [name for name in required if not hasattr(sdk, name)]
    if missing:
        raise MaiBotUnitAdmissionError(
            "官方 maibot_sdk 缺少必要导出: " + ", ".join(missing)
        )
    return sdk


__all__ = ["SDK_VERSION", "require_official_sdk"]
