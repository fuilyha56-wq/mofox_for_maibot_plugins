"""API lifecycle、privacy、tombstone 与 recursion guard 测试。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from mofox_for_maibot_plugins.errors import (
    ApiDepthExceededError,
    ApiPermissionError,
    ApiRecursionError,
    MaiBotCompatError,
)
from mofox_for_maibot_plugins.runtime import (
    ApiTombstone,
    MaiBotApiRecord,
    MaiBotRealm,
    MaiBotRuntime,
    MaiBotUnitState,
)


def test_activating_state_exists() -> None:
    """MaiBotUnitState 包含 ACTIVATING。"""
    assert hasattr(MaiBotUnitState, "ACTIVATING")
    assert MaiBotUnitState.ACTIVATING.value == "activating"


def test_api_record_has_public_field() -> None:
    """MaiBotApiRecord 包含 public 字段，默认 False。"""
    record = MaiBotApiRecord(
        unit=None,  # type: ignore[arg-type]
        plugin_id="test",
        name="api1",
        version="1",
        handler=lambda: None,
        description="",
        dynamic=False,
    )
    assert record.public is False
    assert record.revision == 0


def test_new_error_types_exist() -> None:
    """新增的错误类型可导入且为 MaiBotCompatError 子类。"""
    assert issubclass(ApiRecursionError, MaiBotCompatError)
    assert issubclass(ApiDepthExceededError, MaiBotCompatError)
    assert issubclass(ApiPermissionError, MaiBotCompatError)


def _make_unit(
    plugin_id: str, state: MaiBotUnitState, realm: MaiBotRealm | None = None
) -> MaiBotRuntime:
    """构造带指定状态的最小单元。"""
    unit = MaiBotRuntime(realm or MaiBotRealm())
    unit.plugin_id = plugin_id
    unit.slug = plugin_id.replace(".", "_")
    unit.state = state
    return unit


def _register_api(
    realm: MaiBotRealm,
    unit: MaiBotRuntime,
    name: str,
    version: str,
    handler: Any,
    *,
    public: bool = False,
    dynamic: bool = False,
) -> None:
    """向 realm 注册一个 API record。"""
    record = MaiBotApiRecord(
        unit=unit,
        plugin_id=unit.plugin_id,
        name=name,
        version=version,
        handler=handler,
        description="",
        dynamic=dynamic,
        public=public,
    )
    realm._apis[(unit.plugin_id, name, version)] = record


class TestRequesterVisibility:
    """requester-aware visibility 测试。"""

    @pytest.mark.asyncio
    async def test_private_api_owner_can_call(self) -> None:
        """owner 可以调用自己的 private API。"""
        realm = MaiBotRealm()
        owner = _make_unit("owner", MaiBotUnitState.ACTIVE)
        _register_api(realm, owner, "secret", "1", lambda: "ok")

        result = await realm.call_api(owner, "owner.secret", "1", {})
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_private_api_other_unit_cannot_call(self) -> None:
        """其他 unit 不能调用 private API，错误与不存在相同。"""
        from mofox_for_maibot_plugins.errors import ApiUnavailableError

        realm = MaiBotRealm()
        owner = _make_unit("owner", MaiBotUnitState.ACTIVE)
        other = _make_unit("other", MaiBotUnitState.ACTIVE)
        _register_api(realm, owner, "secret", "1", lambda: "ok")

        with pytest.raises(ApiUnavailableError):
            await realm.call_api(other, "owner.secret", "1", {})

    @pytest.mark.asyncio
    async def test_public_api_other_unit_can_call(self) -> None:
        """其他 unit 可以调用 public API。"""
        realm = MaiBotRealm()
        owner = _make_unit("owner", MaiBotUnitState.ACTIVE)
        other = _make_unit("other", MaiBotUnitState.ACTIVE)
        _register_api(realm, owner, "pub", "1", lambda: "ok", public=True)

        result = await realm.call_api(other, "owner.pub", "1", {})
        assert result == "ok"

    def test_get_api_respects_visibility(self) -> None:
        """api.get 按 requester 过滤；private 对其他 unit 返回 None。"""
        realm = MaiBotRealm()
        owner = _make_unit("owner", MaiBotUnitState.ACTIVE)
        other = _make_unit("other", MaiBotUnitState.ACTIVE)
        _register_api(realm, owner, "secret", "1", lambda: "ok")

        assert realm.get_api(owner, "owner.secret", "1") is not None
        assert realm.get_api(other, "owner.secret", "1") is None

    def test_list_apis_filters_visibility(self) -> None:
        """api.list 只返回调用方可见的活动 API。"""
        realm = MaiBotRealm()
        owner = _make_unit("owner", MaiBotUnitState.ACTIVE)
        other = _make_unit("other", MaiBotUnitState.ACTIVE)
        _register_api(realm, owner, "secret", "1", lambda: "ok")
        _register_api(realm, owner, "pub", "1", lambda: "ok", public=True)

        assert len(realm.list_apis(owner)) == 2
        listed = realm.list_apis(other)
        assert len(listed) == 1
        assert listed[0]["name"] == "pub"


class TestTombstone:
    """逐 key tombstone 测试。"""

    @pytest.mark.asyncio
    async def test_removed_api_has_tombstone(self) -> None:
        """被移除的 API 保存 tombstone，call 抛出含 offline_reason 的错误。"""
        from mofox_for_maibot_plugins.errors import ApiUnavailableError

        realm = MaiBotRealm()
        owner = _make_unit("owner", MaiBotUnitState.ACTIVE)
        _register_api(realm, owner, "old", "1", lambda: "ok", dynamic=True)
        # 替换为空的动态集合
        result = await realm.replace_dynamic_apis(owner, [], "已下线")
        assert result["offline_reason"] == "已下线"

        assert ("owner", "old", "1") in realm._tombstones
        with pytest.raises(ApiUnavailableError) as exc_info:
            await realm.call_api(owner, "owner.old", "1", {})
        assert "已下线" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_republish_deletes_tombstone(self) -> None:
        """同 key 重发删除 tombstone。"""
        realm = MaiBotRealm()
        owner = _make_unit("owner", MaiBotUnitState.ACTIVE, realm)

        async def handler() -> str:
            return "ok"

        owner.instance = type(
            "FakeInstance",
            (),
            {"_dynamic_api_handlers": {"h": handler}},
        )()
        _register_api(realm, owner, "old", "1", handler, dynamic=True)
        await realm.replace_dynamic_apis(owner, [], "已下线")
        assert ("owner", "old", "1") in realm._tombstones

        await realm.replace_dynamic_apis(
            owner,
            [
                {
                    "type": "API",
                    "name": "old",
                    "metadata": {"version": "1", "handler_name": "h"},
                }
            ],
            "重新上线",
        )
        assert ("owner", "old", "1") not in realm._tombstones

    def test_unload_clears_tombstones(self) -> None:
        """unit 卸载清空 tombstone。"""
        realm = MaiBotRealm()
        owner = _make_unit("owner", MaiBotUnitState.ACTIVE)
        _register_api(realm, owner, "old", "1", lambda: "ok", dynamic=True)
        realm._tombstones[("owner", "old", "1")] = ApiTombstone(
            provider_plugin_id="owner",
            api_name="old",
            version="1",
            public=False,
            revision=1,
            offline_reason="已下线",
        )
        realm.remove_unit(owner)
        assert ("owner", "old", "1") not in realm._tombstones


class TestRecursionGuard:
    """递归检测测试。"""

    @pytest.mark.asyncio
    async def test_recursive_call_detected(self) -> None:
        """同一调用链内目标 key 已存在时抛 ApiRecursionError。"""
        realm = MaiBotRealm()
        unit_a = _make_unit("a", MaiBotUnitState.ACTIVE, realm)
        unit_b = _make_unit("b", MaiBotUnitState.ACTIVE, realm)

        async def handler_a() -> Any:
            return await realm.call_api(unit_a, "b.b", "1", {})

        async def handler_b() -> Any:
            return await realm.call_api(unit_b, "a.a", "1", {})

        # 跨 unit 递归需要 public API
        _register_api(realm, unit_a, "a", "1", handler_a, public=True)
        _register_api(realm, unit_b, "b", "1", handler_b, public=True)

        with pytest.raises(ApiRecursionError):
            await realm.call_api(unit_a, "a.a", "1", {})


class TestActivating:
    """ACTIVATING 阶段动态 API 同步测试。"""

    @pytest.mark.asyncio
    async def test_pending_apis_committed_on_activate(self) -> None:
        """ACTIVATING 阶段 replace_dynamic 写入 pending，activate 成功后提交。"""
        realm = MaiBotRealm()
        unit = _make_unit("owner", MaiBotUnitState.ACTIVATING, realm)
        unit._dynamic_api_handlers = {}
        # mock instance 提供 handler
        async def handler() -> str:
            return "ok"

        unit.instance = type(
            "FakeInstance",
            (),
            {"_dynamic_api_handlers": {"h": handler}},
        )()

        result = await realm.replace_dynamic_apis(
            unit,
            [{"type": "API", "name": "dyn", "metadata": {"version": "1", "handler_name": "h"}}],
            "上线中",
        )
        assert result["success"] is True
        assert len(unit._pending_dynamic_apis) == 1
        # ACTIVATING 阶段尚未进入活动集合
        assert ("owner", "dyn", "1") not in realm._apis

        unit.state = MaiBotUnitState.ACTIVE
        unit._commit_pending_dynamic_apis()
        assert ("owner", "dyn", "1") in realm._apis
        assert unit._dynamic_revision == 1

    @pytest.mark.asyncio
    async def test_pending_apis_discarded_on_failure(self) -> None:
        """on_load 失败时丢弃待提交动态 API。"""
        unit = _make_unit("owner", MaiBotUnitState.ACTIVATING)
        unit._pending_dynamic_apis = [object()]  # type: ignore[list-item]
        unit._discard_pending_dynamic_apis()
        assert unit._pending_dynamic_apis == []