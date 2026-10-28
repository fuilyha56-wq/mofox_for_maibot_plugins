"""MaiBot 2.17 component capability host mappings."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mofox_for_maibot_plugins.runtime import MaiBotRealm, MaiBotRuntime


@pytest.fixture
def runtime() -> MaiBotRuntime:
    unit = MaiBotRuntime(MaiBotRealm())
    unit.plugin_id = "external"
    unit.slug = "external"
    return unit


@pytest.mark.asyncio
async def test_component_listing_uses_public_plugin_api(runtime: MaiBotRuntime) -> None:
    plugin = type("Plugin", (), {"plugin_version": "1.2.3", "plugin_description": "demo"})()
    with patch(
        "mofox_for_maibot_plugins.runtime.plugin_api.get_all_plugins",
        return_value={"demo": plugin},
    ):
        result = await runtime._dispatch_capability("component.get_all_plugins", {})
    assert result == {
        "success": True,
        "plugins": [{"name": "demo", "version": "1.2.3", "description": "demo"}],
    }


@pytest.mark.asyncio
async def test_component_load_resolves_path_before_public_loader(runtime: MaiBotRuntime) -> None:
    with (
        patch(
            "mofox_for_maibot_plugins.runtime.plugin_api.get_plugin_path",
            return_value=r"E:\plugins\demo",
        ),
        patch(
            "mofox_for_maibot_plugins.runtime.plugin_api.load_plugin",
            new_callable=AsyncMock,
            return_value=True,
        ) as load_plugin,
    ):
        result = await runtime._dispatch_capability(
            "component.load_plugin", {"plugin_name": "demo"}
        )
    assert result == {"success": True, "result": True}
    load_plugin.assert_awaited_once_with(r"E:\plugins\demo")
