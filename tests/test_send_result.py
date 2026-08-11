"""send handler 结构化返回测试。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

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
    async def test_send_text_success_returns_structured(
        self, mock_runtime: MaiBotRuntime
    ) -> None:
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
    async def test_send_text_failure_returns_sent_false(
        self, mock_runtime: MaiBotRuntime
    ) -> None:
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


class TestSendImageResult:
    """send.image handler 结构化返回。"""

    @pytest.mark.asyncio
    async def test_send_image_success_returns_structured(
        self, mock_runtime: MaiBotRuntime
    ) -> None:
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
    async def test_send_image_failure_returns_sent_false(
        self, mock_runtime: MaiBotRuntime
    ) -> None:
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
    async def test_send_image_empty_base64_raises(
        self, mock_runtime: MaiBotRuntime
    ) -> None:
        """send.image 空 base64 抛 UnsupportedMaiBotCapabilityError。"""
        from mofox_for_maibot_plugins.errors import UnsupportedMaiBotCapabilityError

        with pytest.raises(UnsupportedMaiBotCapabilityError):
            await mock_runtime._dispatch_capability(
                "send.image",
                {"image_base64": "", "stream_id": "qq_group_123"},
            )


class TestSendEmojiResult:
    """send.emoji handler 结构化返回。"""

    @pytest.mark.asyncio
    async def test_send_emoji_success_returns_structured(
        self, mock_runtime: MaiBotRuntime
    ) -> None:
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
    async def test_send_emoji_failure_returns_sent_false(
        self, mock_runtime: MaiBotRuntime
    ) -> None:
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