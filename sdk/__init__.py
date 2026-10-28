"""官方 ``maibot-plugin-sdk`` 2.17.0 的运行时准入验证。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from ..errors import MaiBotUnitAdmissionError

SDK_VERSION = "2.17.0"


def require_official_sdk() -> Any:
    """导入并验证官方 MaiBot SDK 2.17.0，绝不注入本地 shim 或 YesMai 模块。

    验证范围：
        - ``__version__ == "2.17.0"``
        - ``MaiBotPlugin``、``PluginContext``、``PluginPaths``
        - ``Tool``、``Command``、``Action``、``API``
        - 顶层 ``SendResult``
        - ``SendCapability`` 七个发送方法均带 ``return_details``

    Returns:
        官方 ``maibot_sdk`` 模块对象。

    Raises:
        MaiBotUnitAdmissionError: 版本不匹配或缺少必要导出时抛出。
    """
    try:
        sdk = import_module("maibot_sdk")
    except ImportError as exc:
        raise MaiBotUnitAdmissionError(
            "缺少官方依赖 maibot-plugin-sdk==2.17.0"
        ) from exc
    actual_version = str(getattr(sdk, "__version__", "")).strip()
    if actual_version != SDK_VERSION:
        raise MaiBotUnitAdmissionError(
            f"MaiBot SDK 版本必须为 {SDK_VERSION}，当前为 {actual_version or '<unknown>'}"
        )
    required = (
        "MaiBotPlugin",
        "PluginContext",
        "PluginPaths",
        "Tool",
        "Command",
        "Action",
        "API",
        "ReplyExtension",
        "EventHandler",
        "HookHandler",
        "HomeCard",
        "LLMProvider",
        "LLMProviderBase",
        "MessageGateway",
        "WorkflowStep",
        "PluginConfigBase",
        "Field",
        "SendResult",
        "CONFIG_RELOAD_SCOPE_SELF",
        "ON_BOT_CONFIG_RELOAD",
        "ON_MODEL_CONFIG_RELOAD",
    )
    missing = [name for name in required if not hasattr(sdk, name)]
    if missing:
        raise MaiBotUnitAdmissionError(
            "官方 maibot_sdk 缺少必要导出: " + ", ".join(missing)
        )
    _validate_send_methods()
    _validate_public_modules()
    return sdk


def _validate_public_modules() -> None:
    """验证 2.17.0 的公开 capability 与 legacy compat 模块面。"""
    from importlib.util import find_spec

    required_modules = (
        "maibot_sdk.components",
        "maibot_sdk.config",
        "maibot_sdk.context",
        "maibot_sdk.llm_provider",
        "maibot_sdk.messages",
        "maibot_sdk.types",
        "maibot_sdk.capabilities.api",
        "maibot_sdk.capabilities.chat",
        "maibot_sdk.capabilities.component",
        "maibot_sdk.capabilities.config",
        "maibot_sdk.capabilities.database",
        "maibot_sdk.capabilities.emoji",
        "maibot_sdk.capabilities.frequency",
        "maibot_sdk.capabilities.gateway",
        "maibot_sdk.capabilities.knowledge",
        "maibot_sdk.capabilities.llm",
        "maibot_sdk.capabilities.logging",
        "maibot_sdk.capabilities.maisaka",
        "maibot_sdk.capabilities.message",
        "maibot_sdk.capabilities.person",
        "maibot_sdk.capabilities.render",
        "maibot_sdk.capabilities.send",
        "maibot_sdk.capabilities.statistics",
        "maibot_sdk.capabilities.tool",
        "maibot_sdk.compat",
        "maibot_sdk.compat.legacy_adapter",
    )
    missing = [name for name in required_modules if find_spec(name) is None]
    if missing:
        raise MaiBotUnitAdmissionError(
            "MaiBot SDK 2.17.0 模块面不完整: " + ", ".join(missing)
        )


def _validate_send_methods() -> None:
    """验证 SendCapability 七个发送方法均带 return_details 参数。"""
    import inspect

    from maibot_sdk.capabilities.send import SendCapability

    for method_name in ("text", "image", "emoji", "forward", "hybrid", "command", "custom"):
        method = getattr(SendCapability, method_name, None)
        if method is None:
            raise MaiBotUnitAdmissionError(
                f"SendCapability 缺少发送方法: {method_name}"
            )
        sig = inspect.signature(method)
        if "return_details" not in sig.parameters:
            raise MaiBotUnitAdmissionError(
                f"SendCapability.{method_name} 缺少 return_details 参数"
            )


__all__ = ["SDK_VERSION", "require_official_sdk"]


