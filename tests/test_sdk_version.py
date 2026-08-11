"""SDK 2.8.0 版本守卫测试。"""

from __future__ import annotations

import pytest

from mofox_for_maibot_plugins.sdk import SDK_VERSION, require_official_sdk
from mofox_for_maibot_plugins.errors import MaiBotUnitAdmissionError


def test_sdk_version_is_280() -> None:
    """SDK_VERSION 必须精确为 2.8.0。"""
    assert SDK_VERSION == "2.8.0"


def test_require_official_sdk_returns_module() -> None:
    """require_official_sdk 返回的模块 __version__ 为 2.8.0。"""
    sdk = require_official_sdk()
    assert hasattr(sdk, "__version__")
    assert str(sdk.__version__) == "2.8.0"


def test_require_official_sdk_validates_send_result() -> None:
    """require_official_sdk 验证顶层 SendResult 导出。"""
    sdk = require_official_sdk()
    assert hasattr(sdk, "SendResult")


def test_require_official_sdk_validates_send_methods() -> None:
    """require_official_sdk 验证 SendCapability 七个发送方法和 return_details。"""
    sdk = require_official_sdk()
    # 直接检查 SendCapability 的方法签名
    from maibot_sdk.capabilities.send import SendCapability
    import inspect
    for method_name in ("text", "image", "emoji", "forward", "hybrid", "command", "custom"):
        method = getattr(SendCapability, method_name)
        sig = inspect.signature(method)
        assert "return_details" in sig.parameters, f"{method_name} 缺少 return_details"
        assert sig.parameters["return_details"].default is False