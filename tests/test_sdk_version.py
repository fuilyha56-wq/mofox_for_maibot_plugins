"""SDK 2.17.0 版本守卫测试。"""

from __future__ import annotations

import pytest

from mofox_for_maibot_plugins.sdk import SDK_VERSION, require_official_sdk
from mofox_for_maibot_plugins.errors import MaiBotUnitAdmissionError


def test_sdk_version_is_current() -> None:
    """SDK_VERSION 必须精确为当前锁定的 2.17.0。"""
    assert SDK_VERSION == "2.17.0"


def test_require_official_sdk_returns_module() -> None:
    """已安装官方 SDK 时返回与锁定版本一致的模块。"""
    try:
        sdk = require_official_sdk()
    except MaiBotUnitAdmissionError as exc:
        pytest.skip(f"官方 SDK 未安装或未满足 2.17.0 契约: {exc}")
    assert hasattr(sdk, "__version__")
    assert str(sdk.__version__) == "2.17.0"


def test_require_official_sdk_validates_current_public_exports() -> None:
    """守卫验证官方 2.17.0 顶层新增导出。"""
    sdk = require_official_sdk()
    for name in (
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
    ):
        assert hasattr(sdk, name)


def test_require_official_sdk_validates_send_methods() -> None:
    """require_official_sdk 验证 SendCapability 七个发送方法和 return_details。"""
    try:
        require_official_sdk()
    except MaiBotUnitAdmissionError as exc:
        pytest.skip(f"官方 SDK 未安装或未满足 2.17.0 契约: {exc}")
    # 直接检查 SendCapability 的方法签名
    from maibot_sdk.capabilities.send import SendCapability
    import inspect
    for method_name in ("text", "image", "emoji", "forward", "hybrid", "command", "custom"):
        method = getattr(SendCapability, method_name)
        sig = inspect.signature(method)
        assert "return_details" in sig.parameters, f"{method_name} 缺少 return_details"
        assert sig.parameters["return_details"].default is False