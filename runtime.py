"""官方 MaiBot SDK 2.8.0 外部插件的受限桥运行时。

本模块不会注入本地 shim、不会加载 YesMai，也不会为没有公开宿主映射的能力
伪造成功。每一个外部插件都是独立单元，在 Neo-MoFox 组件快照前完成导入、
官方配置与 Context 注入、准入以及 Tool 预生成。
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import importlib.metadata
import importlib.util
import inspect
import json
import re
import sys
import types
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

from src.app.plugin_system.api import (
    llm_api,
    message_api,
    permission_api,
    send_api,
    stream_api,
)
from src.core.models.message import Message, MessageType
from src.kernel.concurrency import get_task_manager
from src.kernel.llm import LLMPayload, ROLE, Text
from src.kernel.logger import get_logger

from .errors import (
    ApiDepthExceededError,
    ApiPermissionError,
    ApiRecursionError,
    ApiUnavailableError,
    ConfigTransactionError,
    InvalidEventHandlerResultError,
    MaiBotUnitAdmissionError,
    UnitUnavailableError,
    UnsupportedMaiBotCapabilityError,
)
from .sdk import SDK_VERSION, require_official_sdk

logger = get_logger("mofox_maibot_compat.runtime")

# API 调用链递归检测 ContextVar 与最大深度
_api_call_stack: contextvars.ContextVar[tuple[tuple[str, str, str], ...]] = (
    contextvars.ContextVar("_api_call_stack", default=())
)
_MAX_API_DEPTH = 16

_SUPPORTED_TYPES = frozenset({"COMMAND", "TOOL", "API", "EVENT_HANDLER"})
_REJECTED_TYPES = frozenset(
    {"HOOK_HANDLER", "HOME_CARD", "MESSAGE_GATEWAY", "WORKFLOW_STEP"}
)
_SUPPORTED_CAPABILITIES = frozenset(
    {
        "send.text",
        "send.image",
        "send.emoji",
        "config.get",
        "config.get_all",
        "config.get_plugin",
        "api.call",
        "api.get",
        "api.list",
        "api.replace_dynamic",
        "llm.generate",
        "message.get_recent",
        "message.get_by_time_in_chat",
        "person.get_id",
        "chat.open_session",
    }
)


class MaiBotUnitState(str, Enum):
    """外部 MaiBot 单元的状态机。"""

    DISCOVERED = "discovered"
    PREPARED = "prepared"
    REGISTERED = "registered"
    ACTIVATING = "activating"
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    DRAINING = "draining"
    UNLOADED = "unloaded"


@dataclass(frozen=True)
class MaiBotComponent:
    """经过官方 SDK 收集与桥准入后的组件声明。"""

    name: str
    component_type: str
    metadata: dict[str, Any]
    handler: Callable[..., Any]
    order: int


@dataclass(frozen=True)
class MaiBotApiRecord:
    """桥域内可调用 API 的不可变登记。"""

    unit: "MaiBotRuntime"
    plugin_id: str
    name: str
    version: str
    handler: Callable[..., Any]
    description: str
    dynamic: bool
    public: bool = False
    revision: int = 0


@dataclass(frozen=True)
class ApiTombstone:
    """已下线 API 的墓碑记录。"""

    provider_plugin_id: str
    api_name: str
    version: str
    public: bool
    revision: int
    offline_reason: str


class MaiBotRealm:
    """同一个桥加载的外部插件共享的原子 API 注册域。"""

    def __init__(self) -> None:
        """建立空的插件和 API 查找表。"""
        self._units: dict[str, MaiBotRuntime] = {}
        self._apis: dict[tuple[str, str, str], MaiBotApiRecord] = {}
        self._tombstones: dict[tuple[str, str, str], ApiTombstone] = {}

    def add_unit(self, unit: "MaiBotRuntime") -> None:
        """原子登记一个已完成准入的单元及其静态 API。"""
        if unit.plugin_id in self._units:
            raise MaiBotUnitAdmissionError(f"MaiBot plugin_id 冲突: {unit.plugin_id}")
        candidate = dict(self._apis)
        for record in unit.static_api_records:
            key = (record.plugin_id, record.name, record.version)
            if key in candidate:
                raise MaiBotUnitAdmissionError(
                    f"桥域 API 冲突: {record.plugin_id}.{record.name}@{record.version}"
                )
        self._check_qualified_name_collisions(
            unit, unit.static_api_records, existing=candidate, tombstones=self._tombstones
        )
        for record in unit.static_api_records:
            key = (record.plugin_id, record.name, record.version)
            candidate[key] = record
        self._units[unit.plugin_id] = unit
        self._apis = candidate

    def remove_unit(self, unit: "MaiBotRuntime") -> None:
        """原子移除单元的静态与动态 API 及其 tombstone。"""
        self._units.pop(unit.plugin_id, None)
        self._apis = {
            key: record for key, record in self._apis.items() if record.unit is not unit
        }
        self._tombstones = {
            key: ts
            for key, ts in self._tombstones.items()
            if ts.provider_plugin_id != unit.plugin_id
        }

    def _commit_pending(
        self, unit: "MaiBotRuntime", records: list[MaiBotApiRecord]
    ) -> None:
        """on_load 成功后原子发布待提交动态 API，并递增 revision。"""
        self._check_qualified_name_collisions(
            unit, records, existing=self._apis, tombstones=self._tombstones
        )
        candidate = {
            key: record
            for key, record in self._apis.items()
            if record.unit is not unit or not record.dynamic
        }
        for record in records:
            key = (record.plugin_id, record.name, record.version)
            owner = candidate.get(key)
            if owner is not None:
                raise ApiUnavailableError(
                    f"动态 API 与其他单元或静态 API 冲突: {record.plugin_id}.{record.name}@{record.version}"
                )
            candidate[key] = record
        next_revision = unit._dynamic_revision + 1
        for record in records:
            key = (record.plugin_id, record.name, record.version)
            candidate[key] = replace(record, revision=next_revision)
        self._apis = candidate
        unit._dynamic_revision = next_revision

    async def replace_dynamic_apis(
        self,
        unit: "MaiBotRuntime",
        declarations: list[dict[str, Any]],
        offline_reason: str,
    ) -> dict[str, Any]:
        """校验后一次性替换指定单元的全部动态 API，保存 tombstone。

        ``ACTIVATING`` 阶段写入待提交集合，``ACTIVE`` 阶段直接替换活动集合。
        """
        if unit.state not in (MaiBotUnitState.ACTIVATING, MaiBotUnitState.ACTIVE):
            raise UnitUnavailableError(f"MaiBot 单元不可同步动态 API: {unit.plugin_id}")
        normalized_reason = str(offline_reason or "").strip() or "动态 API 已下线"
        # 空集合表示全部下线，无需 handler 校验
        records = (
            []
            if not declarations
            else unit.build_dynamic_api_records(declarations)
        )
        self._check_qualified_name_collisions(
            unit, records, existing=self._apis, tombstones=self._tombstones
        )
        if unit.state is MaiBotUnitState.ACTIVATING:
            unit._pending_dynamic_apis = records
            return {
                "success": True,
                "count": len(records),
                "revision": unit._dynamic_revision + 1,
                "offline_reason": normalized_reason,
            }
        new_keys = {(r.plugin_id, r.name, r.version) for r in records}
        candidate = {
            key: record
            for key, record in self._apis.items()
            if record.unit is not unit or not record.dynamic
        }
        next_revision = unit._dynamic_revision + 1
        new_tombstones = dict(self._tombstones)
        for key, record in self._apis.items():
            if record.unit is unit and record.dynamic and key not in new_keys:
                new_tombstones[key] = ApiTombstone(
                    provider_plugin_id=record.plugin_id,
                    api_name=record.name,
                    version=record.version,
                    public=record.public,
                    revision=next_revision,
                    offline_reason=normalized_reason,
                )
        for key in new_keys:
            new_tombstones.pop(key, None)
        for record in records:
            key = (record.plugin_id, record.name, record.version)
            owner = candidate.get(key)
            if owner is not None:
                raise ApiUnavailableError(
                    f"动态 API 与其他单元或静态 API 冲突: {record.plugin_id}.{record.name}@{record.version}"
                )
            candidate[key] = replace(record, revision=next_revision)
        self._apis = candidate
        self._tombstones = new_tombstones
        unit._dynamic_revision = next_revision
        return {
            "success": True,
            "count": len(records),
            "revision": unit._dynamic_revision,
            "offline_reason": normalized_reason,
        }

    def _check_qualified_name_collisions(
        self,
        unit: "MaiBotRuntime",
        records: list[MaiBotApiRecord],
        *,
        existing: dict[tuple[str, str, str], MaiBotApiRecord],
        tombstones: dict[tuple[str, str, str], ApiTombstone],
    ) -> None:
        """检查新记录与活动记录、tombstone 之间是否存在 qualified-name 碰撞。

        qualified name 为 ``f"{plugin_id}.{api_name}"``。两个不同 ApiKey
        生成同一全名（如 ``("a.b","c")`` 与 ``("a","b.c")``）时必须整次失败。
        """
        qualified_to_keys: dict[str, list[tuple[str, str, str]]] = {}
        for key in list(existing) + list(tombstones):
            plugin_id, api_name, _version = key
            qualified_to_keys.setdefault(f"{plugin_id}.{api_name}", []).append(key)
        for record in records:
            qualified = f"{record.plugin_id}.{record.name}"
            colliding = qualified_to_keys.get(qualified, [])
            for key in colliding:
                if key != (record.plugin_id, record.name, record.version):
                    raise ApiUnavailableError(
                        f"API 全名碰撞: {qualified} 由多个 ApiKey 生成"
                    )
            qualified_to_keys.setdefault(qualified, []).append(
                (record.plugin_id, record.name, record.version)
            )
        # 新记录之间也不能碰撞
        seen: dict[str, tuple[str, str, str]] = {}
        for record in records:
            qualified = f"{record.plugin_id}.{record.name}"
            key = (record.plugin_id, record.name, record.version)
            if qualified in seen and seen[qualified] != key:
                raise ApiUnavailableError(
                    f"API 全名碰撞: {qualified} 由多个 ApiKey 生成"
                )
            seen[qualified] = key

    async def call_api(
        self,
        requester: "MaiBotRuntime",
        api_name: str,
        version: str,
        arguments: dict[str, Any],
    ) -> Any:
        """调用唯一解析到的活动桥域 API，执行 requester visibility 与递归检测。

        Args:
            requester: 发起调用的单元。
            api_name: API 全名或短名。
            version: API 版本。
            arguments: 传递给 handler 的参数。

        Returns:
            API handler 的返回值。

        Raises:
            ApiUnavailableError: API 不存在或调用方无权访问。
            ApiRecursionError: 检测到递归调用。
            ApiDepthExceededError: 超过最大调用深度。
        """
        record = self._resolve_api(requester, api_name, version)
        target_key = (record.plugin_id, record.name, record.version)
        current_stack = _api_call_stack.get()
        if target_key in current_stack:
            raise ApiRecursionError(
                f"API 递归调用检测: {record.plugin_id}.{record.name}@{record.version}"
            )
        if len(current_stack) >= _MAX_API_DEPTH:
            raise ApiDepthExceededError(
                f"API 调用深度超限: {len(current_stack)} >= {_MAX_API_DEPTH}"
            )
        # 审计：不记录 API 敏感参数
        logger.debug(
            f"API 调用审计 requester={requester.plugin_id} "
            f"target={record.plugin_id}.{record.name}@{record.version} "
            f"depth={len(current_stack) + 1}"
        )
        token = _api_call_stack.set((*current_stack, target_key))
        try:
            await record.unit.acquire_lease()
            try:
                result = _call_accepted(record.handler, **arguments)
                return await result if inspect.isawaitable(result) else result
            finally:
                await record.unit.release_lease()
        finally:
            _api_call_stack.reset(token)

    def get_api(
        self, requester: "MaiBotRuntime", api_name: str, version: str
    ) -> dict[str, Any] | None:
        """返回单个可见 API 的元数据；可见 tombstone 返回 offline 元数据。"""
        try:
            return _api_metadata(self._resolve_api(requester, api_name, version))
        except ApiUnavailableError:
            tombstone = self._find_tombstone(
                requester, str(api_name or "").strip(), str(version or "").strip()
            )
            if tombstone is not None:
                return {
                    "plugin_id": tombstone.provider_plugin_id,
                    "name": tombstone.api_name,
                    "version": tombstone.version,
                    "description": "",
                    "public": tombstone.public,
                    "dynamic": True,
                    "revision": tombstone.revision,
                    "online": False,
                    "offline_reason": tombstone.offline_reason,
                }
            return None

    def list_apis(
        self, requester: "MaiBotRuntime", plugin_id: str = ""
    ) -> list[dict[str, Any]]:
        """列出调用方可见的全部活动 API。"""
        return [
            _api_metadata(record)
            for record in self._apis.values()
            if (not plugin_id or record.plugin_id == plugin_id)
            and record.unit.state is MaiBotUnitState.ACTIVE
            and (record.public or record.unit is requester)
        ]

    def _resolve_api(
        self, requester: "MaiBotRuntime", api_name: str, version: str
    ) -> MaiBotApiRecord:
        """解析全限定或唯一短 API 名称，按 requester visibility 过滤。"""
        normalized_name = str(api_name or "").strip()
        normalized_version = str(version or "").strip()
        if not normalized_name:
            raise ApiUnavailableError("API 名称不能为空")
        # 精确索引，不用 rsplit 反解析；先按全名匹配，再按短名匹配
        candidates: list[MaiBotApiRecord] = []
        for key, record in self._apis.items():
            record_plugin_id, record_name, record_version = key
            qualified = f"{record_plugin_id}.{record_name}"
            if normalized_name == qualified or (
                "." not in normalized_name and record_name == normalized_name
            ):
                if normalized_version and record_version != normalized_version:
                    continue
                candidates.append(record)
        visible = [
            record
            for record in candidates
            if record.unit.state is MaiBotUnitState.ACTIVE
            and (record.public or record.unit is requester)
        ]
        if not visible:
            tombstone = self._find_tombstone(
                requester, normalized_name, normalized_version
            )
            if tombstone is not None:
                raise ApiUnavailableError(
                    f"API 已下线: {tombstone.offline_reason}"
                )
            raise ApiUnavailableError(f"桥域 API 不可用: {api_name}@{version or '*'}")
        if len(visible) != 1:
            raise ApiUnavailableError(
                f"桥域 API 名称不唯一: {api_name}@{version or '*'}"
            )
        return visible[0]

    def _find_tombstone(
        self, requester: "MaiBotRuntime", api_name: str, version: str
    ) -> ApiTombstone | None:
        """查找调用方可见的 tombstone。"""
        candidates: list[ApiTombstone] = []
        for key, tombstone in self._tombstones.items():
            ts_plugin_id, ts_name, ts_version = key
            qualified = f"{ts_plugin_id}.{ts_name}"
            if api_name == qualified or (
                "." not in api_name and ts_name == api_name
            ):
                if version and ts_version != version:
                    continue
                if tombstone.public or ts_plugin_id == requester.plugin_id:
                    candidates.append(tombstone)
        if len(candidates) == 1:
            return candidates[0]
        return None


class MaiBotRuntime:
    """单个官方 MaiBot SDK 2.8.0 插件的隔离运行时。"""

    def __init__(self, realm: MaiBotRealm | None = None) -> None:
        """创建未加载的单元；默认桥域仅方便独立测试。"""
        self.realm = realm or MaiBotRealm()
        self.sdk: Any = None
        self.plugin_dir = ""
        self.plugin_id = ""
        self.slug = ""
        self.module_prefix = ""
        self.module: types.ModuleType | None = None
        self.instance: Any = None
        self.context: Any = None
        self.state = MaiBotUnitState.DISCOVERED
        self.failure = ""
        self.components: list[MaiBotComponent] = []
        self.dynamic_tools: list[tuple[MaiBotComponent, str]] = []
        self.static_api_records: list[MaiBotApiRecord] = []
        self._pending_dynamic_apis: list[MaiBotApiRecord] = []
        self._dynamic_revision: int = 0
        self._messages: dict[str, Message] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._active_calls = 0
        self._lease_condition = asyncio.Condition()

    def prepare(self, plugin_path: str) -> None:
        """导入插件并在宿主快照前完成官方 SDK 语义注入和准入。"""
        if self.state is not MaiBotUnitState.DISCOVERED:
            raise MaiBotUnitAdmissionError("MaiBot 单元只能预备一次")
        entry = Path(plugin_path).expanduser().resolve() / "plugin.py"
        if not entry.is_file():
            raise MaiBotUnitAdmissionError(f"MaiBot 插件入口不存在: {entry}")
        try:
            self.sdk = require_official_sdk()
            manifest = _read_manifest(entry.parent)
            self.plugin_dir = str(entry.parent)
            self.plugin_id = _manifest_plugin_id(manifest, entry.parent)
            self.slug = _make_slug(self.plugin_id, entry.parent)
            self.module_prefix = f"_mofox_maibot_{self.slug}"
            _reject_yesmai_imports(entry.parent)
            self.module = self._import_module(entry)
            self.instance = self._create_instance(self.module)
            self._assert_lifecycle_overrides()
            self.instance.set_plugin_config(self.instance.build_default_config())
            paths = self.sdk.PluginPaths(
                data_dir=Path("data")
                / "plugins"
                / "mofox_for_maibot_plugins"
                / self.slug,
                runtime_dir=Path("temp")
                / "plugins"
                / "mofox_for_maibot_plugins"
                / self.slug,
            )
            paths.data_dir.mkdir(parents=True, exist_ok=True)
            paths.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.context = self.sdk.PluginContext(self.plugin_id, self._rpc_call, paths)
            self.instance._set_context(self.context)
            self._collect_components()
            self.realm.add_unit(self)
            self.state = MaiBotUnitState.PREPARED
        except Exception:
            self.realm.remove_unit(self)
            self._cleanup_modules()
            raise

    def mark_registered(self) -> None:
        """在宿主已取得组件快照时冻结单元公开 Tool 集。"""
        if self.state is MaiBotUnitState.PREPARED:
            self.state = MaiBotUnitState.REGISTERED

    async def activate(self) -> None:
        """调用官方 on_load；失败只隔离当前单元。

        在 on_load 前转入 ACTIVATING，允许动态 API 同步；
        成功后转入 ACTIVE，失败转入 QUARANTINED。
        """
        if self.state is not MaiBotUnitState.REGISTERED:
            raise UnitUnavailableError(f"MaiBot 单元未注册: {self.plugin_id}")
        self.state = MaiBotUnitState.ACTIVATING
        try:
            await self.instance.on_load()
            self._commit_pending_dynamic_apis()
            self.state = MaiBotUnitState.ACTIVE
        except Exception as exc:  # noqa: BLE001
            self._discard_pending_dynamic_apis()
            self.state = MaiBotUnitState.QUARANTINED
            self.failure = f"on_load 失败: {type(exc).__name__}: {exc}"
            logger.error(f"MaiBot 单元隔离 {self.plugin_id}: {self.failure}")

    def _commit_pending_dynamic_apis(self) -> None:
        """on_load 成功后原子发布待提交动态 API。"""
        if not self._pending_dynamic_apis:
            return
        self.realm._commit_pending(self, self._pending_dynamic_apis)
        self._pending_dynamic_apis.clear()

    def _discard_pending_dynamic_apis(self) -> None:
        """on_load 失败时丢弃待提交动态 API。"""
        self._pending_dynamic_apis.clear()

    async def drain_and_unload(self, timeout: float = 5.0) -> None:
        """停止新调用、取消受管任务、有界等待 lease 并运行 on_unload。"""
        if self.state is MaiBotUnitState.UNLOADED:
            return
        self.state = MaiBotUnitState.DRAINING
        self.realm.remove_unit(self)
        for task_id in list(self._tasks):
            get_task_manager().cancel_task(task_id)
        pending = [task for task in self._tasks.values() if not task.done()]

        async def wait_leases() -> None:
            """等待活动外部调用释放其租约。"""
            async with self._lease_condition:
                if self._active_calls:
                    await self._lease_condition.wait_for(
                        lambda: self._active_calls == 0
                    )

        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, wait_leases(), return_exceptions=True),
                timeout=max(0.01, timeout),
            )
        except TimeoutError:
            logger.warning(f"MaiBot 单元 {self.plugin_id} drain 超时，强制取消受管任务")
            for task in pending:
                task.cancel()
        if self.instance is not None:
            try:
                await asyncio.wait_for(
                    self.instance.on_unload(), timeout=max(0.01, timeout)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"MaiBot 单元 {self.plugin_id} on_unload 失败: {exc}")
        self.components.clear()
        self.dynamic_tools.clear()
        self.static_api_records.clear()
        self._messages.clear()
        self._tasks.clear()
        self.context = None
        self.instance = None
        self._cleanup_modules()
        self.state = MaiBotUnitState.UNLOADED

    async def acquire_lease(self) -> None:
        """在调用外部处理器、工具或 API 前获取运行租约。"""
        async with self._lease_condition:
            if self.state is not MaiBotUnitState.ACTIVE:
                raise UnitUnavailableError(
                    f"MaiBot 单元不可用: {self.plugin_id} ({self.state.value})"
                )
            self._active_calls += 1

    async def release_lease(self) -> None:
        """释放运行租约并唤醒处于卸载 drain 的调用方。"""
        async with self._lease_condition:
            self._active_calls = max(0, self._active_calls - 1)
            if not self._active_calls:
                self._lease_condition.notify_all()

    async def dispatch_message(self, message: Message) -> tuple[bool, bool, Message]:
        """派发 Command 和 ON_MESSAGE EventHandler，返回命中、阻断和替换消息。"""
        await self.acquire_lease()
        try:
            self._messages[message.stream_id] = message
            payload = _normalize_inbound_message(message)
            text = str(payload["plain_text"])
            matched = False
            blocked = False
            current = message
            for component in self.components:
                if component.component_type != "COMMAND" or not _command_matches(
                    component, text
                ):
                    continue
                matched = True
                result = _call_accepted(
                    component.handler,
                    message=payload,
                    stream_id=message.stream_id,
                    text=text,
                )
                if inspect.isawaitable(result):
                    result = await result
                blocked = _parse_command_result(component.name, result) or blocked
            event_handlers = sorted(
                (
                    item
                    for item in self.components
                    if item.component_type == "EVENT_HANDLER"
                ),
                key=lambda item: (-int(item.metadata.get("weight", 0)), item.order),
            )
            for component in event_handlers:
                matched = True
                result = _call_accepted(
                    component.handler,
                    message=_normalize_inbound_message(current),
                    stream_id=current.stream_id,
                    text=str(current.processed_plain_text or current.content or ""),
                )
                if inspect.isawaitable(result):
                    result = await result
                current, should_stop = _parse_event_result(component, result, current)
                blocked = should_stop or blocked
            return matched, blocked, current
        finally:
            await self.release_lease()

    async def execute_tool(
        self, external_name: str, stream_id: str, **kwargs: Any
    ) -> tuple[bool, Any]:
        """执行 namespaced Tool 对应的官方 handler。"""
        component = next(
            (item for item, _ in self.dynamic_tools if item.name == external_name), None
        )
        if component is None:
            return False, f"MaiBot 工具不存在: {external_name}"
        await self.acquire_lease()
        try:
            if stream_id not in self._messages:
                raise UnitUnavailableError(f"MaiBot 工具缺少来源消息: {stream_id}")
            result = _call_accepted(component.handler, stream_id=stream_id, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return _normalize_tool_result(result)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"MaiBot 工具失败 {self.plugin_id}.{external_name}: {exc}")
            return False, f"工具执行失败: {exc}"
        finally:
            await self.release_lease()

    def tool_schema(
        self, component: MaiBotComponent, namespaced_name: str
    ) -> dict[str, Any]:
        """从官方 Tool metadata 生成保留原始参数语义的宿主 schema。"""
        raw = component.metadata.get("parameters_raw")
        if isinstance(raw, dict) and raw:
            schema = dict(raw)
            if schema.get("type") != "object" and "properties" not in schema:
                schema = {"type": "object", "properties": schema}
        else:
            typed = component.metadata.get("parameters")
            schema = {"type": "object", "properties": {}}
            if isinstance(typed, list):
                properties: dict[str, Any] = {}
                required: list[str] = []
                for item in typed:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    if not name:
                        continue
                    properties[name] = {
                        "type": str(item.get("param_type") or "string"),
                        "description": str(item.get("description") or ""),
                    }
                    if bool(item.get("required", True)):
                        required.append(name)
                schema = {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }
        return {
            "type": "function",
            "function": {
                "name": namespaced_name,
                "description": str(
                    component.metadata.get("brief_description")
                    or component.metadata.get("description")
                    or component.name
                ),
                "parameters": schema,
            },
        }

    def build_dynamic_api_records(
        self, declarations: list[dict[str, Any]]
    ) -> list[MaiBotApiRecord]:
        """把官方动态 API 声明映射为可调用的桥域 API。"""
        if self.instance is None:
            raise UnitUnavailableError("MaiBot 实例不可用")
        records: list[MaiBotApiRecord] = []
        seen: set[tuple[str, str, str]] = set()
        for declaration in declarations:
            if (
                not isinstance(declaration, dict)
                or str(declaration.get("type") or "").upper() != "API"
            ):
                raise ApiUnavailableError("动态 API 声明必须是官方 API 组件")
            name = str(declaration.get("name") or "").strip()
            metadata = declaration.get("metadata")
            if not name or not isinstance(metadata, dict):
                raise ApiUnavailableError("动态 API 缺少 name 或 metadata")
            version = str(metadata.get("version") or "1").strip() or "1"
            handler_name = str(metadata.get("handler_name") or "").strip()
            handler = self.instance._dynamic_api_handlers.get(handler_name)
            if not callable(handler):
                handler = getattr(self.instance, handler_name, None)
            if not callable(handler):
                raise ApiUnavailableError(f"动态 API handler 不可调用: {handler_name}")
            key = (self.plugin_id, name, version)
            if key in seen:
                raise ApiUnavailableError(f"动态 API 重复: {name}@{version}")
            seen.add(key)
            records.append(
                MaiBotApiRecord(
                    unit=self,
                    plugin_id=self.plugin_id,
                    name=name,
                    version=version,
                    handler=handler,
                    description=str(metadata.get("description") or ""),
                    dynamic=True,
                    public=bool(metadata.get("public", False)),
                )
            )
        return records

    async def apply_config_transaction(
        self, data: dict[str, Any], version: str
    ) -> None:
        """在内存中原子更新官方配置，回调失败时恢复旧快照。"""
        if self.instance is None:
            raise UnitUnavailableError("MaiBot 实例不可用")
        previous = self.instance.get_plugin_config_data()
        try:
            self.instance.set_plugin_config(data)
            await self.instance.on_config_update(
                "self", self.instance.get_plugin_config_data(), version
            )
        except Exception as exc:  # noqa: BLE001
            try:
                self.instance.set_plugin_config(previous)
            except Exception as rollback_error:  # noqa: BLE001
                raise ConfigTransactionError(
                    f"配置更新失败且回滚失败: {rollback_error}"
                ) from exc
            raise ConfigTransactionError(f"配置更新已回滚: {exc}") from exc

    def register_task(self, coro: Coroutine[Any, Any, Any]) -> None:
        """为桥拥有的扩展协程创建 TaskManager 受管任务。"""
        if self.state not in (MaiBotUnitState.ACTIVATING, MaiBotUnitState.ACTIVE):
            raise UnitUnavailableError(
                f"MaiBot 单元未激活，不能创建任务: {self.plugin_id} ({self.state.value})"
            )
        task_info = get_task_manager().create_task(
            coro,
            name=f"compat:maibot:{self.slug}",
            group_name=f"compat:maibot:{self.slug}",
            metadata={"bridge": "maibot", "plugin_id": self.plugin_id},
        )
        if task_info.task is not None:
            task_id = task_info.task_id
            self._tasks[task_id] = task_info.task
            task_info.task.add_done_callback(lambda _: self._tasks.pop(task_id, None))

    def _import_module(self, entry: Path) -> types.ModuleType:
        """用私有包前缀导入并支持外部插件内相对导入。"""
        self._ensure_official_sdk_modules()
        package = types.ModuleType(self.module_prefix)
        package.__path__ = [str(entry.parent)]
        package.__package__ = self.module_prefix
        sys.modules[self.module_prefix] = package
        module_name = f"{self.module_prefix}.plugin"
        spec = importlib.util.spec_from_file_location(
            module_name,
            entry,
            submodule_search_locations=[str(entry.parent)],
        )
        if spec is None or spec.loader is None:
            raise MaiBotUnitAdmissionError(f"无法创建外部模块规格: {entry}")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = self.module_prefix
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _ensure_official_sdk_modules(self) -> None:
        """向外部单元导入器暴露已验证的官方 SDK 模块，不注入本地 shim。"""
        if self.sdk is None:
            raise MaiBotUnitAdmissionError("官方 MaiBot SDK 尚未加载")
        module_names = [
            "maibot_sdk",
            "maibot_sdk.components",
            "maibot_sdk.context",
            "maibot_sdk.types",
        ]
        for module_name in module_names:
            module = sys.modules.get(module_name)
            if module is None:
                __import__(module_name)
                module = sys.modules.get(module_name)
            if module is None:
                raise MaiBotUnitAdmissionError(
                    f"官方 MaiBot SDK 子模块不可用: {module_name}"
                )

    def _create_instance(self, module: types.ModuleType) -> Any:
        """使用 factory 或唯一官方 MaiBotPlugin 子类创建实例。"""
        factory = getattr(module, "create_plugin", None)
        if callable(factory):
            instance = factory()
        else:
            candidates = [
                item
                for item in vars(module).values()
                if inspect.isclass(item)
                and issubclass(item, self.sdk.MaiBotPlugin)
                and item is not self.sdk.MaiBotPlugin
            ]
            if len(candidates) != 1:
                raise MaiBotUnitAdmissionError(
                    "外部插件必须提供 create_plugin() 或唯一 MaiBotPlugin 子类"
                )
            instance = candidates[0]()
        if not isinstance(instance, self.sdk.MaiBotPlugin):
            raise MaiBotUnitAdmissionError("外部 factory 未返回官方 MaiBotPlugin")
        return instance

    def _assert_lifecycle_overrides(self) -> None:
        """拒绝保留 SDK 基类强制生命周期的插件。"""
        for name in ("on_load", "on_unload", "on_config_update"):
            if getattr(type(self.instance), name, None) is getattr(
                self.sdk.MaiBotPlugin, name, None
            ):
                raise MaiBotUnitAdmissionError(f"外部 MaiBot 插件必须覆盖 {name}()")

    def _collect_components(self) -> None:
        """依据官方 get_components/get_llm_providers 建立完整且可验证的映射。"""
        if self.instance.get_llm_providers():
            raise MaiBotUnitAdmissionError("不支持官方 LLMProvider 声明")
        declarations = self.instance.get_components()
        self._reject_all_declared_component_types()
        if not isinstance(declarations, list):
            raise MaiBotUnitAdmissionError("官方 get_components() 必须返回列表")
        components: list[MaiBotComponent] = []
        tools: list[tuple[MaiBotComponent, str]] = []
        apis: list[MaiBotApiRecord] = []
        seen_tools: set[str] = set()
        for order, declaration in enumerate(declarations):
            component = self._parse_component(declaration, order)
            if component.component_type in _REJECTED_TYPES:
                raise MaiBotUnitAdmissionError(
                    f"不支持 MaiBot 组件: {component.component_type}"
                )
            if component.component_type not in _SUPPORTED_TYPES:
                raise MaiBotUnitAdmissionError(
                    f"未知 MaiBot 组件: {component.component_type}"
                )
            if component.component_type == "COMMAND":
                pattern = str(component.metadata.get("command_pattern") or "")
                if pattern:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        raise MaiBotUnitAdmissionError(
                            f"命令正则无效 {component.name}: {exc}"
                        ) from exc
            elif component.component_type == "EVENT_HANDLER":
                if str(component.metadata.get("event_type") or "") != "on_message":
                    raise MaiBotUnitAdmissionError("仅支持 ON_MESSAGE EventHandler")
            elif component.component_type == "TOOL":
                namespaced = namespaced_tool_name(self.slug, component.name)
                if namespaced in seen_tools:
                    raise MaiBotUnitAdmissionError(f"重复 Tool 名称: {namespaced}")
                seen_tools.add(namespaced)
                tools.append((component, namespaced))
            elif component.component_type == "API":
                apis.append(
                    MaiBotApiRecord(
                        unit=self,
                        plugin_id=self.plugin_id,
                        name=component.name,
                        version=str(component.metadata.get("version") or "1"),
                        handler=component.handler,
                        description=str(component.metadata.get("description") or ""),
                        dynamic=False,
                        public=bool(component.metadata.get("public", False)),
                    )
                )
            components.append(component)
        self.components = components
        self.dynamic_tools = tools
        self.static_api_records = apis

    def _reject_all_declared_component_types(self) -> None:
        """检查官方收集范围外的声明，避免它们被静默遗漏。"""
        components_module = sys.modules.get("maibot_sdk.components")
        marker_name = "__maibot_component_info__"
        if components_module is not None:
            marker_name = str(
                getattr(components_module, "_COMPONENT_INFO_ATTR", marker_name)
            )
        for name in dir(type(self.instance)):
            try:
                method = getattr(type(self.instance), name)
            except Exception:  # noqa: BLE001
                continue
            info = getattr(method, marker_name, None)
            component_type = str(
                getattr(getattr(info, "type", None), "value", "") or ""
            ).upper()
            if component_type in _REJECTED_TYPES:
                raise MaiBotUnitAdmissionError(f"不支持 MaiBot 组件: {component_type}")

    def _parse_component(self, declaration: Any, order: int) -> MaiBotComponent:
        """验证官方组件字典并从其 handler_name 解析绑定实例方法。"""
        if not isinstance(declaration, dict):
            raise MaiBotUnitAdmissionError("官方组件声明必须是字典")
        name = str(declaration.get("name") or "").strip()
        component_type = str(declaration.get("type") or "").upper()
        metadata = declaration.get("metadata")
        if not name or not component_type or not isinstance(metadata, dict):
            raise MaiBotUnitAdmissionError("官方组件声明缺少 name/type/metadata")
        handler_name = str(metadata.get("handler_name") or "").strip()
        handler = getattr(self.instance, handler_name, None)
        if not callable(handler):
            raise MaiBotUnitAdmissionError(f"组件 handler 不可调用: {handler_name}")
        return MaiBotComponent(name, component_type, dict(metadata), handler, order)

    async def _rpc_call(
        self,
        method: str,
        plugin_id: str,
        payload: dict[str, Any] | None,
        *,
        timeout_ms: int | None = None,
    ) -> Any:
        """实现官方 PluginContext 白名单 RPC；不接受伪造宿主原始方法。"""
        del timeout_ms
        if plugin_id != self.plugin_id:
            raise PermissionError("MaiBot RPC plugin_id 与当前单元不匹配")
        if method not in {
            "cap.call",
            "host.route_message",
            "host.update_message_gateway_state",
        }:
            raise PermissionError(f"不允许 MaiBot Host RPC: {method}")
        if method != "cap.call":
            raise UnsupportedMaiBotCapabilityError(f"未映射 MaiBot Host RPC: {method}")
        if not isinstance(payload, dict):
            raise UnsupportedMaiBotCapabilityError("cap.call payload 必须是字典")
        capability = str(payload.get("capability") or "").strip()
        args = payload.get("args")
        if not isinstance(args, dict):
            raise UnsupportedMaiBotCapabilityError("cap.call args 必须是字典")
        return await self._dispatch_capability(capability, args)

    async def _dispatch_capability(
        self, capability: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """严格把支持的 capability 路由到 Neo-MoFox 公开 API。"""
        if capability not in _SUPPORTED_CAPABILITIES:
            raise UnsupportedMaiBotCapabilityError(
                f"未映射 MaiBot capability: {capability}"
            )
        if capability == "send.text":
            return await self._send_text(args)
        if capability == "send.image":
            return await self._send_image(args, image=True)
        if capability == "send.emoji":
            return await self._send_image(args, image=False)
        if capability == "config.get":
            value = _lookup(
                self.instance.get_plugin_config_data(),
                str(args.get("key") or ""),
                args.get("default"),
            )
            return {"success": True, "value": value}
        if capability == "config.get_all":
            return {"success": True, "value": self.instance.get_plugin_config_data()}
        if capability == "config.get_plugin":
            requested = str(args.get("plugin_name") or "").strip()
            if requested and requested != self.plugin_id:
                raise UnsupportedMaiBotCapabilityError(
                    "桥仅允许读取当前 MaiBot 单元配置"
                )
            return {"success": True, "value": self.instance.get_plugin_config_data()}
        if capability == "api.call":
            result = await self.realm.call_api(
                self,
                str(args.get("api_name") or ""),
                str(args.get("version") or ""),
                dict(args.get("args") or {}),
            )
            return {"success": True, "result": result}
        if capability == "api.get":
            return {
                "success": True,
                "api": self.realm.get_api(
                    self,
                    str(args.get("api_name") or ""),
                    str(args.get("version") or ""),
                ),
            }
        if capability == "api.list":
            return {
                "success": True,
                "apis": self.realm.list_apis(
                    self, str(args.get("plugin_id") or "")
                ),
            }
        if capability == "api.replace_dynamic":
            apis = args.get("apis")
            if not isinstance(apis, list):
                raise ApiUnavailableError("api.replace_dynamic 的 apis 必须为列表")
            return await self.realm.replace_dynamic_apis(
                self, apis, str(args.get("offline_reason") or "动态 API 已下线")
            )
        if capability == "llm.generate":
            return await self._generate_llm(args)
        if capability == "message.get_recent":
            messages = await message_api.get_recent_messages(
                str(args.get("chat_id") or args.get("stream_id") or ""),
                limit=int(args.get("limit") or 10),
            )
            return {"success": True, "messages": messages}
        if capability == "message.get_by_time_in_chat":
            messages = await message_api.get_messages_by_time_in_chat(
                str(args.get("chat_id") or args.get("stream_id") or ""),
                float(args.get("start_time") or 0.0),
                float(args.get("end_time") or 0.0),
                limit=int(args.get("limit") or 0),
            )
            return {"success": True, "messages": messages}
        if capability == "person.get_id":
            return {
                "success": True,
                "person_id": permission_api.generate_person_id(
                    str(args.get("platform") or ""), str(args.get("user_id") or "")
                ),
            }
        if capability == "chat.open_session":
            stream = await stream_api.get_or_create_stream(
                platform=str(args.get("platform") or ""),
                user_id=str(args.get("user_id") or ""),
                group_id=str(args.get("group_id") or ""),
                chat_type=str(args.get("chat_type") or "private"),
            )
            return {
                "success": True,
                "stream": {"stream_id": getattr(stream, "stream_id", "")},
            }
        raise UnsupportedMaiBotCapabilityError(
            f"未映射 MaiBot capability: {capability}"
        )

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

    async def _generate_llm(self, args: dict[str, Any]) -> dict[str, Any]:
        """通过公开 llm_api 执行仅文本的官方 LLM capability。"""
        task = str(args.get("task") or "").strip()
        model = str(args.get("model") or "").strip()
        if not task and not model:
            raise UnsupportedMaiBotCapabilityError(
                "llm.generate 必须显式指定 task 或 model"
            )
        if task and model:
            raise UnsupportedMaiBotCapabilityError(
                "llm.generate 不能同时指定 task 和 model"
            )
        selected_model = task or model
        model_set = (
            llm_api.get_model_set_by_task(task)
            if task
            else llm_api.get_model_set_by_name(model)
        )
        request_model_set = _with_llm_overrides(
            model_set,
            temperature=args.get("temperature"),
            max_tokens=args.get("max_tokens"),
        )
        request = llm_api.create_llm_request(
            request_model_set,
            request_name=f"compat.maibot.{self.slug}",
        )
        for payload in _llm_payloads(args.get("prompt")):
            request.add_payload(payload)
        response = await request.send(stream=False)
        return {
            "success": True,
            "response": response.message or "",
            "reasoning": response.reasoning_content or "",
            "reasoning_content": response.reasoning_content or "",
            "reasoning_parts": list(response.reasoning_parts or []),
            "tool_calls": [
                _tool_call_to_dict(call) for call in response.call_list or []
            ],
            "model": selected_model,
        }

    def _source_message(self, stream_id: str) -> Message:
        """确保 capability 被路由回一个真实来源消息。"""
        source = self._messages.get(stream_id)
        if source is None:
            raise UnitUnavailableError(f"MaiBot capability 无来源 stream: {stream_id}")
        return source

    @staticmethod
    def _outbound_message(
        source: Message,
        content: Any,
        message_type: MessageType,
        plain_text: str,
    ) -> Message:
        """构建保留来源流、平台、目标和 reply 的公开宿主出站消息。"""
        target_group_id = source.extra.get("group_id") or source.extra.get(
            "target_group_id"
        )
        target_user_id = source.extra.get("target_user_id") or source.sender_id
        extra: dict[str, Any] = {}
        if str(source.chat_type or "").lower() == "group" and target_group_id:
            extra["target_group_id"] = str(target_group_id)
        elif target_user_id:
            extra["target_user_id"] = str(target_user_id)
        return Message(
            content=content,
            processed_plain_text=plain_text,
            message_type=message_type,
            platform=source.platform,
            chat_type=source.chat_type,
            stream_id=source.stream_id,
            reply_to=source.message_id or None,
            **extra,
        )

    def _cleanup_modules(self) -> None:
        """清理外部插件私有模块前缀。"""
        for module_name in list(sys.modules):
            if module_name == self.module_prefix or module_name.startswith(
                f"{self.module_prefix}."
            ):
                sys.modules.pop(module_name, None)
        self.module = None


def namespaced_tool_name(slug: str, external_name: str) -> str:
    """返回严格隔离的宿主 Tool 名称。"""
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", str(external_name or "").strip()).strip(
        "_"
    )
    if not normalized:
        raise MaiBotUnitAdmissionError("MaiBot Tool 名称不能为空")
    return f"maibot__{slug}__{normalized}"


def prepare_runtime(realm: MaiBotRealm, plugin_dir: str | Path) -> Any:
    """按插件公开导入契约选择官方 SDK 或 MaiBot 0.11.x profile。"""
    directory = Path(plugin_dir).resolve()
    uses_legacy_api = False
    for source_file in directory.rglob("*.py"):
        try:
            source = source_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise MaiBotUnitAdmissionError(
                f"无法读取外部插件源码: {source_file}"
            ) from exc
        if re.search(r"(?m)^\s*from\s+src\.(?:plugin_system|config)\b", source):
            uses_legacy_api = True
            break
        if re.search(r"(?m)^\s*import\s+src\.(?:plugin_system|config)\b", source):
            uses_legacy_api = True
            break

    if uses_legacy_api:
        from .legacy_runtime import MaiBotLegacyRuntime

        runtime = MaiBotLegacyRuntime(realm)
    else:
        runtime = MaiBotRuntime(realm)
    runtime.prepare(directory)
    return runtime


def _read_manifest(directory: Path) -> dict[str, Any]:
    """读取外部 manifest，并验证其 SDK 范围和声明 capability。"""
    manifest_path = directory / "_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MaiBotUnitAdmissionError(f"解析 _manifest.json 失败: {exc}") from exc
    if not isinstance(manifest, dict):
        raise MaiBotUnitAdmissionError("_manifest.json 顶层必须是对象")
    sdk_range = manifest.get("sdk")
    if isinstance(sdk_range, dict):
        minimum = str(sdk_range.get("min_version") or "")
        maximum = str(sdk_range.get("max_version") or "")
        if minimum and _version_tuple(minimum) > _version_tuple(SDK_VERSION):
            raise MaiBotUnitAdmissionError("外部插件要求更高版本 MaiBot SDK")
        if maximum and _version_tuple(maximum) < _version_tuple(SDK_VERSION):
            raise MaiBotUnitAdmissionError("外部插件 SDK 上限不包含 2.8.0")
    declared = manifest.get("capabilities")
    if isinstance(declared, list):
        unsupported = sorted(
            {str(item) for item in declared if str(item) not in _SUPPORTED_CAPABILITIES}
        )
        if unsupported:
            raise MaiBotUnitAdmissionError(
                "外部 manifest 声明未映射 capability: " + ", ".join(unsupported)
            )
    _validate_manifest_dependencies(manifest)
    return manifest


def _validate_manifest_dependencies(manifest: dict[str, Any]) -> None:
    """确认 manifest 声明的 Python 依赖已由受控环境安装，桥不隐式安装。"""
    raw_dependencies = manifest.get(
        "python_dependencies", manifest.get("dependencies", [])
    )
    if raw_dependencies is None:
        return
    if not isinstance(raw_dependencies, list):
        raise MaiBotUnitAdmissionError("外部 manifest 的 dependencies 必须是列表")
    declarations: list[str] = []
    for item in raw_dependencies:
        if isinstance(item, str):
            declarations.append(item)
            continue
        if not isinstance(item, dict):
            raise MaiBotUnitAdmissionError(
                "外部 manifest 的 dependencies 项必须是字符串或对象"
            )
        dependency_type = str(item.get("type") or "python_package").strip().lower()
        if dependency_type not in {"python_package", "python", "pip"}:
            continue
        name = str(item.get("name") or "").strip()
        version_spec = str(
            item.get("version_spec") or item.get("version") or ""
        ).strip()
        if not name:
            raise MaiBotUnitAdmissionError(
                "外部 manifest 的 Python dependency 缺少 name"
            )
        declarations.append(f"{name}{version_spec}")
    for declaration in declarations:
        requirement_text = declaration.strip()
        if not requirement_text:
            continue
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exc:
            raise MaiBotUnitAdmissionError(
                f"外部 manifest 依赖声明无效: {declaration}"
            ) from exc
        package_name = requirement.name
        if package_name.lower().replace("_", "-") in {"yesmai", "maibot-yesmai"}:
            raise MaiBotUnitAdmissionError(
                "标准 MaiBot 2.8.0 bridge 不接受 YesMai 依赖"
            )
        try:
            installed_version = importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise MaiBotUnitAdmissionError(
                f"外部 manifest 依赖未安装: {declaration}"
            ) from exc
        if requirement.specifier and installed_version not in requirement.specifier:
            raise MaiBotUnitAdmissionError(
                f"外部 manifest 依赖版本不满足: {declaration}（已安装 {installed_version}）"
            )


def _reject_yesmai_imports(plugin_dir: Path) -> None:
    """在执行外部代码前递归拒绝插件包中任何 YesMai 导入。"""
    for source_file in plugin_dir.rglob("*.py"):
        try:
            source = source_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise MaiBotUnitAdmissionError(
                f"无法读取外部插件源码: {source_file}"
            ) from exc
        if re.search(
            r"(?m)^\s*(?:from\s+(?:com\.yesmai|yesmai)\b|import\s+(?:com\.yesmai|yesmai)\b)",
            source,
        ):
            raise MaiBotUnitAdmissionError(
                f"标准 MaiBot 2.8.0 bridge 不接受 YesMai 导入: {source_file.name}"
            )


def _manifest_plugin_id(manifest: dict[str, Any], directory: Path) -> str:
    """使用 manifest ID 或基于目录生成稳定本地身份。"""
    plugin_id = str(manifest.get("id") or "").strip()
    return plugin_id or f"local.{_make_slug(directory.name, directory)}"


def _make_slug(seed: str, directory: Path) -> str:
    """构建路径稳定、名称安全的单元 slug。"""
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(seed).lower()).strip("_") or "plugin"
    digest = hashlib.sha256(str(directory.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{normalized}_{digest}"


def _version_tuple(value: str) -> tuple[int, ...]:
    """将版本文本转为可比较数字元组。"""
    values = re.findall(r"\d+", str(value))
    return tuple(int(value) for value in values[:3]) or (0,)


def _command_matches(component: MaiBotComponent, text: str) -> bool:
    """按官方 command_pattern 或声明名称/aliases 匹配消息。

    遵循 MaiBot ``find_command_by_text`` 语义：有 pattern 时用 ``re.search``
    （非 ``re.match``），无 pattern 时遍历 aliases 做 ``str.startswith``。
    ``component.name`` 作为默认 alias 以兼容仅声明名称的 Command。
    """
    pattern = str(component.metadata.get("command_pattern") or "")
    if pattern:
        return re.search(pattern, text) is not None
    aliases = [
        component.name,
        *[str(alias) for alias in component.metadata.get("aliases", [])],
    ]
    return any(text.startswith(alias) for alias in aliases if alias)


def _parse_command_result(name: str, result: Any) -> bool:
    """解析 MaiBot Host 使用的命令三元返回和拦截等级。"""
    if result is None:
        return False
    if (
        not isinstance(result, tuple)
        or len(result) != 3
        or not isinstance(result[2], int)
        or result[2] < 0
    ):
        raise InvalidEventHandlerResultError(
            f"MaiBot Command {name} 必须返回 (success, value, intercept_level: int)"
        )
    return bool(result[2])


def _parse_event_result(
    component: MaiBotComponent, result: Any, previous: Message
) -> tuple[Message, bool]:
    """解析 ON_MESSAGE EventHandler 的显式替换与阻断结果。"""
    if result is None:
        return previous, False
    if not isinstance(result, dict):
        raise InvalidEventHandlerResultError(
            f"MaiBot EventHandler {component.name} 必须返回 dict 或 None"
        )
    modified_message = result.get("modified_message", result.get("message"))
    message = (
        previous
        if modified_message is None
        else _apply_modified_message(
            component.name,
            previous,
            modified_message,
        )
    )
    blocked = bool(result.get("blocked", False)) or not bool(
        result.get("continue_processing", True)
    )
    return message, blocked


def _apply_modified_message(name: str, previous: Message, modified: Any) -> Message:
    """应用 EventHandler 允许修改的消息内容字段，保留宿主路由身份。"""
    if isinstance(modified, Message):
        return modified
    if not isinstance(modified, Mapping):
        raise InvalidEventHandlerResultError(
            f"MaiBot EventHandler {name} 的 modified_message 必须是消息字典"
        )
    plain_text = str(modified.get("plain_text", previous.processed_plain_text or ""))
    raw_message = modified.get("raw_message")
    raw_data = copy.deepcopy(previous.raw_data)
    if isinstance(raw_message, list):
        if isinstance(raw_data, dict):
            raw_data["message"] = copy.deepcopy(raw_message)
        else:
            raw_data = {"message": copy.deepcopy(raw_message)}
    extra = copy.deepcopy(previous.extra)
    for key in ("media", "unknown_segments"):
        value = modified.get(key)
        if isinstance(value, list):
            extra[key] = copy.deepcopy(value)
    return Message(
        message_id=previous.message_id,
        time=previous.time,
        reply_to=previous.reply_to,
        content=plain_text,
        processed_plain_text=plain_text,
        message_type=previous.message_type,
        sender_id=previous.sender_id,
        sender_name=previous.sender_name,
        sender_cardname=previous.sender_cardname,
        sender_role=previous.sender_role,
        platform=previous.platform,
        chat_type=previous.chat_type,
        stream_id=previous.stream_id,
        raw_data=raw_data,
        **extra,
    )


def _normalize_tool_result(result: Any) -> tuple[bool, Any]:
    """将官方 Tool/Action 结果归一化为宿主二元 Tool 返回。"""
    if isinstance(result, tuple):
        return (
            bool(result[0]) if result else True,
            result[1] if len(result) > 1 else "",
        )
    if isinstance(result, dict):
        return bool(result.get("success", True)), result.get(
            "content", result.get("result", result)
        )
    return True, result


def _call_accepted(handler: Callable[..., Any], **kwargs: Any) -> Any:
    """只注入外部处理器签名明确接受的关键字参数。"""
    signature = inspect.signature(handler)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return handler(**kwargs)
    return handler(
        **{
            name: value
            for name, value in kwargs.items()
            if name in signature.parameters
        }
    )


def _normalize_inbound_message(message: Message) -> dict[str, Any]:
    """构建官方命令可消费的结构化消息，raw_message 为规范化段列表。"""
    group_id = str(
        message.extra.get("group_id") or message.extra.get("target_group_id") or ""
    )
    segments, protocol_unknown_segments = _normalize_segments(message)
    host_media = message.extra.get("media")
    host_unknown_segments = message.extra.get("unknown_segments")
    media = copy.deepcopy(host_media) if isinstance(host_media, list) else []
    unknown_segments = list(protocol_unknown_segments)
    if isinstance(host_unknown_segments, list):
        unknown_segments.extend(copy.deepcopy(host_unknown_segments))
    return {
        "plain_text": str(message.processed_plain_text or message.content or ""),
        "raw_message": segments,
        "message_id": message.message_id,
        "stream_id": message.stream_id,
        "platform": message.platform,
        "chat_type": message.chat_type,
        "user_id": message.sender_id,
        "group_id": group_id,
        "sender": {
            "id": message.sender_id,
            "name": message.sender_name,
            "cardname": message.sender_cardname,
            "role": message.sender_role,
        },
        "reply_to": message.reply_to,
        "is_admin": str(message.sender_role or "").lower()
        in {"owner", "operator", "admin"},
        "media": media,
        "unknown_segments": unknown_segments,
    }


def _normalize_segments(
    message: Message,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从公开原始数据和消息字段创建已知段及未映射协议段。"""
    raw = message.raw_data
    source = raw.get("message") if isinstance(raw, dict) else None
    segments: list[dict[str, Any]] = []
    unknown_segments: list[dict[str, Any]] = []
    if isinstance(source, list):
        for item in source:
            if not isinstance(item, dict):
                unknown_segments.append(
                    {"type": "invalid", "data": copy.deepcopy(item)}
                )
                continue
            kind = str(item.get("type") or "").lower()
            data = item.get("data")
            if not isinstance(data, dict):
                data = {}
            if kind == "text":
                segments.append(
                    {"type": "text", "data": {"text": str(data.get("text") or "")}}
                )
            elif kind == "at":
                target = str(data.get("target_user_id") or data.get("qq") or "").strip()
                if target:
                    segments.append({"type": "at", "data": {"target_user_id": target}})
            elif kind == "reply":
                segments.append(
                    {"type": "reply", "data": {"id": str(data.get("id") or "")}}
                )
            elif kind in {"image", "emoji", "voice", "record", "video", "file"}:
                segments.append({"type": kind, "data": copy.deepcopy(data)})
            else:
                unknown_segments.append(
                    {"type": kind or "unknown", "data": copy.deepcopy(data)}
                )
    text = str(message.processed_plain_text or message.content or "")
    has_text_segment = any(item["type"] == "text" for item in segments)
    if text and not has_text_segment:
        segments.insert(0, {"type": "text", "data": {"text": text}})
    if not any(item["type"] == "at" for item in segments):
        at_users = message.extra.get("at_users")
        if isinstance(at_users, list):
            for value in at_users:
                target = str(value or "").strip()
                if target:
                    segments.append({"type": "at", "data": {"target_user_id": target}})
    return segments, unknown_segments


def _lookup(config: Mapping[str, Any], key: str, default: Any) -> Any:
    """按点分隔路径读取当前单元的内存配置快照。"""
    current: Any = config
    for part in str(key or "").split("."):
        if not part or not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _llm_payloads(prompt: Any) -> list[LLMPayload]:
    """将官方 capability 的纯文本 prompt 无损转为宿主 payload。"""
    payloads: list[LLMPayload] = []
    if isinstance(prompt, str):
        payloads.append(LLMPayload(ROLE.USER, Text(prompt)))
    elif isinstance(prompt, list):
        for item in prompt:
            if isinstance(item, dict):
                role_name = str(item.get("role") or "user").lower()
                role = (
                    ROLE.SYSTEM
                    if role_name == "system"
                    else ROLE.ASSISTANT
                    if role_name == "assistant"
                    else ROLE.USER
                )
                content = item.get("content", "")
                if not isinstance(content, str):
                    raise UnsupportedMaiBotCapabilityError(
                        "llm.generate 仅支持文本 content"
                    )
                payloads.append(LLMPayload(role, Text(content)))
            else:
                payloads.append(LLMPayload(ROLE.USER, Text(str(item))))
    else:
        payloads.append(LLMPayload(ROLE.USER, Text(str(prompt or ""))))
    if not payloads:
        raise UnsupportedMaiBotCapabilityError("llm.generate 需要非空 prompt")
    return payloads


def _with_llm_overrides(
    model_set: Any,
    *,
    temperature: Any,
    max_tokens: Any,
) -> list[dict[str, Any]]:
    """复制显式模型集合，并仅接受 SDK 可表达的请求覆盖参数。"""
    if not isinstance(model_set, list) or not model_set:
        raise UnsupportedMaiBotCapabilityError("llm.generate 未解析到可用模型集合")
    result: list[dict[str, Any]] = []
    normalized_temperature: float | None = None
    normalized_max_tokens: int | None = None
    if temperature is not None:
        if isinstance(temperature, bool) or not isinstance(temperature, int | float):
            raise UnsupportedMaiBotCapabilityError(
                "llm.generate 的 temperature 必须是数值"
            )
        normalized_temperature = float(temperature)
    if max_tokens is not None:
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens <= 0
        ):
            raise UnsupportedMaiBotCapabilityError(
                "llm.generate 的 max_tokens 必须是正整数"
            )
        normalized_max_tokens = max_tokens
    for entry in model_set:
        if not isinstance(entry, Mapping):
            raise UnsupportedMaiBotCapabilityError(
                "llm.generate 模型集合条目必须是字典"
            )
        copied = dict(entry)
        if normalized_temperature is not None:
            copied["temperature"] = normalized_temperature
        if normalized_max_tokens is not None:
            copied["max_tokens"] = normalized_max_tokens
        result.append(copied)
    return result


def _tool_call_to_dict(call: Any) -> dict[str, Any]:
    """把公开 LLM ToolCall 映射为不泄漏内部对象的 SDK 响应字段。"""
    if isinstance(call, Mapping):
        return dict(call)
    return {
        "id": getattr(call, "id", None),
        "name": str(getattr(call, "name", "") or ""),
        "args": getattr(call, "args", {}),
    }


def _api_metadata(record: MaiBotApiRecord) -> dict[str, Any]:
    """返回不会泄露实例或 handler 的安全 API 元数据。"""
    return {
        "plugin_id": record.plugin_id,
        "name": record.name,
        "version": record.version,
        "description": record.description,
        "public": record.public,
        "dynamic": record.dynamic,
        "revision": record.revision,
        "online": True,
    }


__all__ = [
    "MaiBotApiRecord",
    "MaiBotComponent",
    "MaiBotRealm",
    "MaiBotRuntime",
    "MaiBotUnitState",
    "namespaced_tool_name",
    "_normalize_inbound_message",
]
