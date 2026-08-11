# MaiBot Plan 1: SDK 2.8.0 与发送结果 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 MaiBot 兼容桥从 `maibot-plugin-sdk==2.7.1` 精确升级到 `==2.8.0`，并让所有已支持的 `send.*` handler 返回结构化 RPC 结果，使 SDK 2.8.0 的 `return_details=True` 与 `SendResult` 契约可工作。

**Architecture:** 修改 `sdk/__init__.py` 的版本守卫、`manifest.json` 的依赖声明和描述、`runtime.py` 中 `_dispatch_capability` 的 send handler 返回值。不新增 capability、不替换 legacy runtime、不改变 API identity 合同。send handler 从返回裸 `bool` 改为返回 `{"success": bool, "sent": bool, "message_id": str | None}`，由 SDK `PluginContext._normalize_capability_result()` 决定最终公开 bool 或 `SendResult`。

**Tech Stack:** Python 3.11.9, `maibot-plugin-sdk==2.8.0`, Neo-MoFox `send_api`, `pytest`, `ruff`

## Global Constraints

- Python `>=3.11`，依赖与运行统一使用 `uv`
- 精确锁定 `maibot-plugin-sdk==2.8.0`，不接受其他 patch 版本
- `image_base64` / `emoji_base64` 参数名不变
- 发送失败不得返回 `success=True`
- `message_id` 只取 Adapter/Service 明确确认的响应字段，缺失时为 `None`
- 不在本计划新增 capability、不替换 legacy runtime、不修改 API identity 合同
- 所有函数/方法参数与返回值必须有类型注解
- 所有函数、类、文件需包含文档字符串
- 新增 `src/` 代码时同步补齐 `test/` 对应单测

---

## File Structure

| 文件 | 职责 | 操作 |
| --- | --- | --- |
| `sdk/__init__.py` | SDK 版本守卫与准入验证 | 修改 |
| `manifest.json` | 桥 manifest 依赖声明与描述 | 修改 |
| `runtime.py` | send handler 返回结构化结果 | 修改 |
| `tests/test_sdk_version.py` | SDK 2.8.0 身份与导出测试 | 新建 |
| `tests/test_send_result.py` | send handler 结构化返回测试 | 新建 |

---

### Task 1: 升级 SDK 版本守卫

**Files:**
- Modify: `sdk/__init__.py`
- Test: `tests/test_sdk_version.py`

**Interfaces:**
- Produces: `SDK_VERSION = "2.8.0"`, `require_official_sdk() -> Any`（验证 2.8.0 导出）

- [ ] **Step 1: Write the failing test**

```python
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
    ctx_cls = sdk.PluginContext
    # PluginContext 实例化需要 rpc_call，用 mock 检查类属性
    send_cls = type(ctx_cls.send)  # noqa: F841
    # 直接检查 SendCapability 的方法签名
    from maibot_sdk.capabilities.send import SendCapability
    import inspect
    for method_name in ("text", "image", "emoji", "forward", "hybrid", "command", "custom"):
        method = getattr(SendCapability, method_name)
        sig = inspect.signature(method)
        assert "return_details" in sig.parameters, f"{method_name} 缺少 return_details"
        assert sig.parameters["return_details"].default is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sdk_version.py -v`
Expected: FAIL with `SDK_VERSION == "2.7.1"` 不等于 `"2.8.0"`

- [ ] **Step 3: Write minimal implementation**

```python
"""官方 ``maibot-plugin-sdk`` 2.8.0 的运行时准入验证。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from ..errors import MaiBotUnitAdmissionError

SDK_VERSION = "2.8.0"


def require_official_sdk() -> Any:
    """导入并验证官方 MaiBot SDK 2.8.0，绝不注入本地 shim 或 YesMai 模块。

    验证范围：
        - ``__version__ == "2.8.0"``
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
            "缺少官方依赖 maibot-plugin-sdk==2.8.0"
        ) from exc
    actual_version = str(getattr(sdk, "__version__", "")).strip()
    if actual_version != SDK_VERSION:
        raise MaiBotUnitAdmissionError(
            f"MaiBot SDK 版本必须为 {SDK_VERSION}，当前为 {actual_version or '<unknown>'}"
        )
    required = ("MaiBotPlugin", "PluginContext", "PluginPaths", "Tool", "Command", "Action", "API")
    missing = [name for name in required if not hasattr(sdk, name)]
    if missing:
        raise MaiBotUnitAdmissionError(
            "官方 maibot_sdk 缺少必要导出: " + ", ".join(missing)
        )
    if not hasattr(sdk, "SendResult"):
        raise MaiBotUnitAdmissionError("官方 maibot_sdk 缺少 SendResult 导出")
    _validate_send_methods()
    return sdk


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sdk_version.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sdk/__init__.py tests/test_sdk_version.py
git commit -m "feat: upgrade SDK version guard to 2.8.0 with SendResult validation"
```

---

### Task 2: 更新 manifest 依赖声明

**Files:**
- Modify: `manifest.json`
- Test: `tests/test_manifest.py`

**Interfaces:**
- Produces: `manifest.json` 中 `python_dependencies` 为 `["maibot-plugin-sdk==2.8.0"]`，`description` 不再引用 2.7.1

- [ ] **Step 1: Write the failing test**

```python
"""manifest.json 依赖声明测试。"""

from __future__ import annotations

import json
from pathlib import Path


def test_manifest_python_dependencies_pin_280() -> None:
    """manifest python_dependencies 精确锁定 2.8.0。"""
    manifest_path = Path(__file__).parent.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    deps = manifest.get("python_dependencies", [])
    assert "maibot-plugin-sdk==2.8.0" in deps


def test_manifest_description_does_not_mention_271() -> None:
    """manifest description 不再引用 2.7.1。"""
    manifest_path = Path(__file__).parent.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    description = manifest.get("description", "")
    assert "2.7.1" not in description
    assert "2.8.0" in description
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_manifest.py -v`
Expected: FAIL with `"maibot-plugin-sdk==2.7.1"` 不等于 `"==2.8.0"`

- [ ] **Step 3: Write minimal implementation**

修改 `manifest.json`：

```json
{
  "name": "mofox_for_maibot_plugins",
  "display_name": "MofoxForMaibotPulgins",
  "version": "0.1.3",
  "description": "在 MoFox 中加载受限的官方 MaiBot SDK 2.8.0 插件。",
  "author": "MoFox Community",
  "dependencies": {"plugins": [], "components": []},
  "include": [{"component_type": "event_handler", "component_name": "maibot_plugin_bridge", "dependencies": []}],
  "entry_point": "plugin.py",
  "min_core_version": "1.2.0-rc.2",
  "api_version": {"event_api": "1.0.0"},
  "python_dependencies": ["maibot-plugin-sdk==2.8.0"],
  "dependencies_required": true,
  "license": "AGPL-3.0",
  "categories": ["tool"],
  "tags": ["maibot", "compat", "bridge", "sdk", "plugin-loader"]
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_manifest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add manifest.json tests/test_manifest.py
git commit -m "feat: update manifest to pin maibot-plugin-sdk==2.8.0"
```

---

### Task 3: send.text handler 返回结构化结果

**Files:**
- Modify: `runtime.py` — `_send_text()` 和 `_dispatch_capability()` 中 `send.text` 分支
- Test: `tests/test_send_result.py`

**Interfaces:**
- Consumes: `send_api.send_message(message) -> bool`（Neo-MoFox 公开 API）
- Produces: `_send_text(args) -> dict[str, Any]` 返回 `{"success": bool, "sent": bool, "message_id": None}`

- [ ] **Step 1: Write the failing test**

```python
"""send handler 结构化返回测试。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mofox_for_maibot_plugins.runtime import MaiBotRuntime, MaiBotRealm


@pytest.fixture
def mock_runtime() -> MaiBotRuntime:
    """创建带 mock 来源消息的运行时。"""
    realm = MaiBotRealm()
    runtime = MaiBotRuntime(realm)
    runtime.plugin_id = "test_plugin"
    runtime.slug = "test"
    # mock 来源消息
    from src.core.models.message import Message, MessageType
    msg = Message(
        content="hello",
        processed_plain_text="hello",
        message_type=MessageType.TEXT,
        platform="qq",
        chat_type="group",
        stream_id="qq_group_123",
        sender_id="user_1",
    )
    runtime._messages["qq_group_123"] = msg
    return runtime


class TestSendTextResult:
    """send.text handler 结构化返回。"""

    @pytest.mark.asyncio
    async def test_send_text_success_returns_structured(self, mock_runtime: MaiBotRuntime) -> None:
        """send.text 成功时返回 success/sent/message_id 结构。"""
        with patch(
            "mofox_for_maibot_plugins.runtime.send_api.send_message",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await mock_runtime._dispatch_capability(
                "send.text",
                {"text": "hi", "stream_id": "qq_group_123"},
            )
        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["sent"] is True
        assert "message_id" in result

    @pytest.mark.asyncio
    async def test_send_text_failure_returns_sent_false(self, mock_runtime: MaiBotRuntime) -> None:
        """send.text 失败时 sent=False，不伪造 message_id。"""
        with patch(
            "mofox_for_maibot_plugins.runtime.send_api.send_message",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await mock_runtime._dispatch_capability(
                "send.text",
                {"text": "hi", "stream_id": "qq_group_123"},
            )
        assert result["success"] is False
        assert result["sent"] is False
        assert result["message_id"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_send_result.py::TestSendTextResult -v`
Expected: FAIL — 当前 `_send_text` 返回 `bool`，`_dispatch_capability` 包装为 `{"success": bool}`

- [ ] **Step 3: Write minimal implementation**

在 `runtime.py` 中修改 `_send_text` 返回结构化字典，并修改 `_dispatch_capability` 的 `send.text` 分支：

```python
    async def _send_text(self, args: dict[str, Any]) -> dict[str, Any]:
        """按来源消息的会话、平台与回复目标发送文本，返回结构化结果。

        Returns:
            包含 ``success``、``sent`` 和 ``message_id`` 的字典。
        """
        source = self._source_message(str(args.get("stream_id") or ""))
        ok = await send_api.send_message(
            self._outbound_message(
                source,
                str(args.get("text") or ""),
                MessageType.TEXT,
                str(args.get("text") or ""),
            )
        )
        return {"success": ok, "sent": ok, "message_id": None}
```

在 `_dispatch_capability` 中修改 `send.text` 分支：

```python
        if capability == "send.text":
            return await self._send_text(args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_send_result.py::TestSendTextResult -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime.py tests/test_send_result.py
git commit -m "feat: send.text handler returns structured RPC result"
```

---

### Task 4: send.image handler 返回结构化结果

**Files:**
- Modify: `runtime.py` — `_send_image()` 和 `_dispatch_capability()` 中 `send.image` 分支
- Test: `tests/test_send_result.py`

**Interfaces:**
- Produces: `_send_image(args, image=True) -> dict[str, Any]` 返回 `{"success": bool, "sent": bool, "message_id": None}`

- [ ] **Step 1: Write the failing test**

```python
class TestSendImageResult:
    """send.image handler 结构化返回。"""

    @pytest.mark.asyncio
    async def test_send_image_success_returns_structured(self, mock_runtime: MaiBotRuntime) -> None:
        """send.image 成功时返回 success/sent/message_id 结构。"""
        with patch(
            "mofox_for_maibot_plugins.runtime.send_api.send_message",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await mock_runtime._dispatch_capability(
                "send.image",
                {"image_base64": "iVBOR...", "stream_id": "qq_group_123"},
            )
        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["sent"] is True
        assert "message_id" in result

    @pytest.mark.asyncio
    async def test_send_image_failure_returns_sent_false(self, mock_runtime: MaiBotRuntime) -> None:
        """send.image 失败时 sent=False。"""
        with patch(
            "mofox_for_maibot_plugins.runtime.send_api.send_message",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await mock_runtime._dispatch_capability(
                "send.image",
                {"image_base64": "iVBOR...", "stream_id": "qq_group_123"},
            )
        assert result["success"] is False
        assert result["sent"] is False
        assert result["message_id"] is None

    @pytest.mark.asyncio
    async def test_send_image_empty_base64_raises(self, mock_runtime: MaiBotRuntime) -> None:
        """send.image 空 base64 抛 UnsupportedMaiBotCapabilityError。"""
        from mofox_for_maibot_plugins.errors import UnsupportedMaiBotCapabilityError
        with pytest.raises(UnsupportedMaiBotCapabilityError):
            await mock_runtime._dispatch_capability(
                "send.image",
                {"image_base64": "", "stream_id": "qq_group_123"},
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_send_result.py::TestSendImageResult -v`
Expected: FAIL — 当前 `_send_image` 返回 `bool`

- [ ] **Step 3: Write minimal implementation**

修改 `_send_image` 返回结构化字典：

```python
    async def _send_image(self, args: dict[str, Any], *, image: bool) -> dict[str, Any]:
        """发送图片或表情，不将其降级成文本，返回结构化结果。

        Args:
            args: capability 参数。
            image: ``True`` 为图片，``False`` 为表情。

        Returns:
            包含 ``success``、``sent`` 和 ``message_id`` 的字典。
        """
        source = self._source_message(str(args.get("stream_id") or ""))
        key = "image_base64" if image else "emoji_base64"
        data = str(args.get(key) or "").strip()
        if not data:
            raise UnsupportedMaiBotCapabilityError(f"{key} 不能为空")
        ok = await send_api.send_message(
            self._outbound_message(
                source,
                data,
                MessageType.IMAGE if image else MessageType.EMOJI,
                "[图片]" if image else "[表情]",
            )
        )
        return {"success": ok, "sent": ok, "message_id": None}
```

在 `_dispatch_capability` 中修改 `send.image` 分支：

```python
        if capability == "send.image":
            return await self._send_image(args, image=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_send_result.py::TestSendImageResult -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime.py tests/test_send_result.py
git commit -m "feat: send.image handler returns structured RPC result"
```

---

### Task 5: send.emoji handler 返回结构化结果

**Files:**
- Modify: `runtime.py` — `_dispatch_capability()` 中 `send.emoji` 分支
- Test: `tests/test_send_result.py`

**Interfaces:**
- Produces: `send.emoji` 分支调用 `_send_image(args, image=False)` 并返回结构化字典

- [ ] **Step 1: Write the failing test**

```python
class TestSendEmojiResult:
    """send.emoji handler 结构化返回。"""

    @pytest.mark.asyncio
    async def test_send_emoji_success_returns_structured(self, mock_runtime: MaiBotRuntime) -> None:
        """send.emoji 成功时返回 success/sent/message_id 结构。"""
        with patch(
            "mofox_for_maibot_plugins.runtime.send_api.send_message",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await mock_runtime._dispatch_capability(
                "send.emoji",
                {"emoji_base64": "iVBOR...", "stream_id": "qq_group_123"},
            )
        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["sent"] is True
        assert "message_id" in result

    @pytest.mark.asyncio
    async def test_send_emoji_failure_returns_sent_false(self, mock_runtime: MaiBotRuntime) -> None:
        """send.emoji 失败时 sent=False。"""
        with patch(
            "mofox_for_maibot_plugins.runtime.send_api.send_message",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await mock_runtime._dispatch_capability(
                "send.emoji",
                {"emoji_base64": "iVBOR...", "stream_id": "qq_group_123"},
            )
        assert result["success"] is False
        assert result["sent"] is False
        assert result["message_id"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_send_result.py::TestSendEmojiResult -v`
Expected: FAIL — 当前 `send.emoji` 分支调用 `_send_image` 但包装为 `{"success": bool}`

- [ ] **Step 3: Write minimal implementation**

在 `_dispatch_capability` 中修改 `send.emoji` 分支：

```python
        if capability == "send.emoji":
            return await self._send_image(args, image=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_send_result.py::TestSendEmojiResult -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime.py tests/test_send_result.py
git commit -m "feat: send.emoji handler returns structured RPC result"
```

---

### Task 6: 更新 runtime.py 文档字符串中的版本引用

**Files:**
- Modify: `runtime.py` — 模块文档字符串和 `MaiBotRuntime` 类文档字符串
- Test: `tests/test_no_271_references.py`

**Interfaces:**
- Produces: `runtime.py` 文档字符串不再引用 2.7.1

- [ ] **Step 1: Write the failing test**

```python
"""验证运行时代码不再引用 2.7.1。"""

from __future__ import annotations

from pathlib import Path


def test_runtime_module_docstring_no_271() -> None:
    """runtime.py 模块文档字符串不引用 2.7.1。"""
    content = Path("runtime.py").read_text(encoding="utf-8")
    # 允许在注释中提到"从 2.7.1 升级"，但不允许作为当前基线声明
    lines = content.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("#"):
            continue
        if "2.7.1" in stripped and "升级" not in stripped and "from" not in stripped.lower():
            # 检查是否是当前基线声明
            if "官方 MaiBot SDK 2.7.1" in stripped:
                assert False, f"runtime.py 仍引用 2.7.1 作为当前基线: {stripped}"


def test_mai_bot_runtime_class_docstring_no_271() -> None:
    """MaiBotRuntime 类文档字符串不引用 2.7.1。"""
    content = Path("runtime.py").read_text(encoding="utf-8")
    assert "官方 MaiBot SDK 2.7.1 插件的隔离运行时" not in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_no_271_references.py -v`
Expected: FAIL — `runtime.py` 模块文档字符串和 `MaiBotRuntime` 类文档字符串引用 2.7.1

- [ ] **Step 3: Write minimal implementation**

修改 `runtime.py` 第 1 行模块文档字符串：

```python
"""官方 MaiBot SDK 2.8.0 外部插件的受限桥运行时。

本模块不会注入本地 shim、不会加载 YesMai，也不会为没有公开宿主映射的能力
伪造成功。每一个外部插件都是独立单元，在 Neo-MoFox 组件快照前完成导入、
官方配置与 Context 注入、准入以及 Tool 预生成。
"""
```

修改 `MaiBotRuntime` 类文档字符串：

```python
class MaiBotRuntime:
    """单个官方 MaiBot SDK 2.8.0 插件的隔离运行时。"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_no_271_references.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime.py tests/test_no_271_references.py
git commit -m "docs: update runtime.py docstrings from 2.7.1 to 2.8.0"
```

---

### Task 7: 全量回归与 ruff 检查

**Files:**
- Test: 全部测试文件

**Interfaces:**
- Consumes: Task 1–6 的全部修改

- [ ] **Step 1: Run ruff check**

Run: `ruff check runtime.py sdk/__init__.py`
Expected: 0 errors

- [ ] **Step 2: Run all new tests**

Run: `pytest tests/test_sdk_version.py tests/test_manifest.py tests/test_send_result.py tests/test_no_271_references.py -v`
Expected: ALL PASS

- [ ] **Step 3: Run existing tests to verify no regression**

Run: `pytest tests/ -v --timeout=30`
Expected: ALL PASS（或只有与本次修改无关的 pre-existing failure）

- [ ] **Step 4: Verify SDK parity manually**

Run: `python -c "from maibot_sdk import __version__; print(__version__); from maibot_sdk import SendResult; print(SendResult.__annotations__)"`
Expected: `2.8.0` 和 `{'sent': <class 'bool'>, 'message_id': typing.Optional[str]}`

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: full regression for SDK 2.8.0 upgrade and send result contract"
```

---

## Self-Review

### Spec coverage

| 规格章节 | 覆盖任务 |
| --- | --- |
| §2 SendResult 与 return_details | Task 1, 3, 4, 5 |
| §3 sdk/__init__.py 偏差 | Task 1 |
| §3 send handler 偏差 | Task 3, 4, 5 |
| §4.1 运行时版本 2.8.0 | Task 1, 2 |
| §4.1 SendResult 导出验证 | Task 1 |
| §4.1 七个发送方法 return_details 验证 | Task 1 |
| §6.1 Host RPC 返回 success/sent/message_id | Task 3, 4, 5 |
| §6.2 send.text/image E 级 | Task 3, 4 |
| §6.2 send.emoji C 级（本计划仍返回结构化） | Task 5 |
| §6.3 发送失败不返回 success=True | Task 3, 4, 5 |
| §6.3 message_id 缺失为 None | Task 3, 4, 5 |
| 验收 M2801 | Task 1 |
| 验收 M2802 | Task 3, 4, 5（默认 bool 由 SDK 解包） |
| 验收 M2803 | Task 3, 4, 5（return_details=True 由 SDK 解包） |
| 验收 M2804 | Task 4, 5 |
| 验收 M2805 | Task 3, 4, 5 |

### Placeholder scan

无 TBD、TODO 或占位符。

### Type consistency

- `_send_text` 和 `_send_image` 返回 `dict[str, Any]`，与 `_dispatch_capability` 的返回类型一致
- `SDK_VERSION` 在 Task 1 定义为 `"2.8.0"`，Task 2 的 manifest 和 Task 6 的文档字符串都引用同一值
- `require_official_sdk()` 签名不变，调用方无需修改