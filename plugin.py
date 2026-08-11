"""官方 MaiBot SDK 2.8.0 的 Neo-MoFox 受限兼容桥。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from src.core.components import BaseEventHandler, BasePlugin, BaseTool, register_plugin
from src.core.components.types import ChatType, EventType
from src.core.models.message import Message
from src.kernel.event import EventDecision
from src.kernel.logger import get_logger

from .config import MaiBotCompatConfig
from .config_merger import (
    ConfigChangePoller,
    apply_toml_data_to_units,
    build_merged_config_class,
    collect_plugin_sections,
    install_merged_config,
)
from .runtime import MaiBotRealm, MaiBotRuntime, MaiBotUnitState, prepare_runtime

logger = get_logger("mofox_maibot_compat")


class MaiBotPluginBridge(BaseEventHandler):
    """将 Neo-MoFox 入站消息分派给构造期预备的 MaiBot 单元。"""

    name = "maibot_plugin_bridge"
    description = "官方 MaiBot SDK 2.8.0 消息事件桥"
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]
    timeout = 0

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """执行共享 bridge 的消息派发，不改变 EventBus 参数键集合。"""
        runtime = getattr(self.plugin, "bridge", None)
        if not isinstance(runtime, MaiBotBridge):
            return EventDecision.PASS, params
        return await runtime.dispatch_event(event_name, params)


class MaiBotBridge:
    """管理官方 SDK 单元、组件快照与桥域 API 的共享运行时。"""

    def __init__(self, plugin: BasePlugin) -> None:
        """建立桥域和外部单元集合。"""
        self.plugin = plugin
        self.realm = MaiBotRealm()
        self.units: list[MaiBotRuntime] = []
        self._config_poller: ConfigChangePoller | None = None
        self._plugin_slug_field_map: dict[str, dict[str, str]] = {}

    def prepare_units(self) -> None:
        """在宿主读取 get_components 前预备所有候选外部插件。"""
        config = self.plugin.config
        if not isinstance(config, MaiBotCompatConfig) or not config.plugin.enabled:
            return
        for directory in _resolve_plugin_dirs(config):
            try:
                unit = prepare_runtime(self.realm, directory)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    f"MaiBot 单元准入拒绝 {directory}: {type(exc).__name__}: {exc}"
                )
                continue
            self.units.append(unit)
        self._install_merged_config()

    def _install_merged_config(self) -> None:
        """构建合并配置类并替换 ConfigManager 缓存，使 webui 能编辑被兼容插件配置。

        被兼容 MaiBot 插件的 ``PluginConfigBase`` 配置模型被转换为 SectionBase
        子类，合并到 ``MaiBotCompatConfig`` 的子类中。替换 ConfigManager 缓存后，
        webui 的 ConfigParser 会从合并配置类的 model_fields 提取全部 section schema。
        同时启动配置文件变更轮询，在 webui 保存后把新配置分发回插件实例。
        """
        if not self.units:
            return
        plugin_sections, slug_field_map = collect_plugin_sections(self.units)
        self._plugin_slug_field_map = slug_field_map
        if not plugin_sections:
            logger.debug("无被兼容 MaiBot 插件配置需要合并")
            return
        merged_cls = build_merged_config_class(
            MaiBotCompatConfig,
            plugin_sections,
            "MergedMaiBotCompatConfig",
        )
        config_path = (
            Path("config") / "plugins" / self.plugin.plugin_name / "config.toml"
        )
        merged_instance = install_merged_config(
            self.plugin.plugin_name, merged_cls, config_path
        )
        # 同步桥自身持有的配置引用，使后续读取到合并后的值
        self.plugin.config = merged_instance
        logger.info(
            f"MaiBot 桥合并配置已安装，含 {len(plugin_sections)} 个被兼容插件配置"
        )
        self._config_poller = ConfigChangePoller(
            config_path,
            on_change=self._on_config_file_changed,
        )
        self._config_poller.start()
        logger.info("MaiBot 桥配置轮询已启动")

    def _on_config_file_changed(self, data: dict[str, Any]) -> None:
        """webui 保存配置后，把被兼容插件部分分发回各单元实例。"""
        apply_toml_data_to_units(data, self.units, self._plugin_slug_field_map)
        logger.debug("MaiBot 桥配置变更已分发到被兼容插件实例")

    def mark_registered(self) -> None:
        """标记全部已预备单元已越过宿主组件快照边界。"""
        for unit in self.units:
            unit.mark_registered()

    async def activate(self) -> None:
        """按稳定发现顺序激活单元，并隔离单个生命周期失败。"""
        for unit in self.units:
            if unit.state is MaiBotUnitState.REGISTERED:
                await unit.activate()

    async def unload(self) -> None:
        """有界排空所有单元，取消任务、清理 API 与模块前缀。"""
        timeout = _configured_timeout(self.plugin.config)
        if self._config_poller is not None:
            await self._config_poller.stop()
            self._config_poller = None
        for unit in list(self.units):
            await unit.drain_and_unload(timeout)
        self.units.clear()
        self._plugin_slug_field_map.clear()

    async def dispatch_event(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """在活动单元间派发消息并回写允许替换的既有 message 字段。"""
        if event_name != EventType.ON_MESSAGE_RECEIVED.value:
            return EventDecision.PASS, params
        message = params.get("message")
        if not isinstance(message, Message):
            return EventDecision.PASS, params
        current = message
        blocked = False
        for unit in self.units:
            if unit.state is not MaiBotUnitState.ACTIVE:
                continue
            try:
                _matched, unit_blocked, current = await unit.dispatch_message(current)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"MaiBot 单元消息派发失败 {unit.plugin_id}: {exc}")
                continue
            blocked = blocked or unit_blocked
        if current is not message:
            params["message"] = current
        return (EventDecision.STOP if blocked else EventDecision.SUCCESS), params


def _make_tool_class(
    unit: MaiBotRuntime,
    external_name: str,
    namespaced_name: str,
    description: str,
) -> type[BaseTool]:
    """为一个官方 Tool 或兼容 Action 创建 namespaced 宿主 BaseTool。"""
    tool_name = namespaced_name
    original_name = external_name
    tool_description = description or f"MaiBot 工具 {original_name}"
    component = next(
        component for component, name in unit.dynamic_tools if name == namespaced_name
    )
    schema = unit.tool_schema(component, tool_name)

    class MaiBotTool(BaseTool):
        """桥预生成的官方 MaiBot Tool 包装器。"""

        name = tool_name
        description = tool_description
        display_name = f"[tool][maibot]{original_name}"
        chatter_allow: list[str] = []
        chat_type = ChatType.ALL

        async def execute(self, **kwargs: Any) -> tuple[bool, Any]:
            """按当前宿主工具调用 stream 反向分派到所属外部单元。"""
            return await unit.execute_tool(
                original_name,
                self.get_current_stream_id(),
                **kwargs,
            )

        @classmethod
        def to_schema(cls) -> dict[str, Any]:
            """返回带桥命名空间的工具 JSON schema。"""
            return schema

    MaiBotTool.__name__ = f"MaiBotTool_{unit.slug}_{original_name}"
    MaiBotTool.__qualname__ = MaiBotTool.__name__
    return MaiBotTool


@register_plugin
class MaiBotCompatPlugin(BasePlugin):
    """MaiBot 官方 SDK 2.8.0 受限桥插件根组件。"""

    plugin_name = "mofox_for_maibot_plugins"
    plugin_description = "在 Neo-MoFox 中运行受限的官方 MaiBot SDK 2.8.0 插件"
    plugin_version = "0.1.4"
    configs = [MaiBotCompatConfig]

    def __init__(self, config: MaiBotCompatConfig | None = None) -> None:
        """构造期预备单元，以满足宿主组件快照要求。"""
        super().__init__(config)
        _migrate_legacy_paths()
        self.bridge = MaiBotBridge(self)
        self._components_frozen = False
        self._component_snapshot: list[type] | None = None
        if isinstance(config, MaiBotCompatConfig) and config.plugin.enabled:
            self.bridge.prepare_units()

    def get_components(self) -> list[type]:
        """返回唯一桥 EventHandler 与构造期预生成的全部 Tool。"""
        if self._component_snapshot is None:
            self._components_frozen = True
            self.bridge.mark_registered()
            components: list[type] = [MaiBotPluginBridge]
            for unit in self.bridge.units:
                for component, namespaced_name in unit.dynamic_tools:
                    components.append(
                        _make_tool_class(
                            unit,
                            component.name,
                            namespaced_name,
                            str(
                                component.metadata.get("brief_description")
                                or component.metadata.get("description")
                                or ""
                            ),
                        )
                    )
            self._component_snapshot = components
        return list(self._component_snapshot)

    async def on_plugin_loaded(self) -> None:
        """在组件已由宿主注册后激活已准入单元。"""
        await self.bridge.activate()

    async def on_plugin_unloaded(self) -> None:
        """排空所有单元并清除桥域运行时状态。"""
        await self.bridge.unload()


def _resolve_plugin_dirs(config: MaiBotCompatConfig) -> list[str]:
    """解析单插件路径或桥自身 plugins 目录下的候选插件。"""
    single = str(config.plugin.plugin_path or "").strip()
    if single:
        return [single]
    root_value = str(config.plugin.plugins_root or "").strip()
    root = (
        Path(root_value).expanduser()
        if root_value
        else Path(__file__).resolve().parent / "plugins"
    )
    if not root.is_dir():
        logger.warning(f"MaiBot 插件仓库不存在: {root}")
        return []
    return [
        str(item)
        for item in sorted(root.iterdir())
        if item.is_dir() and (item / "plugin.py").is_file()
    ]


def _configured_timeout(config: Any) -> float:
    """从桥公开配置提取安全的卸载超时。"""
    section = getattr(config, "plugin", None)
    value = getattr(section, "unload_timeout_seconds", 5.0)
    return float(value) if isinstance(value, int | float) and value > 0 else 5.0


def _migrate_legacy_paths() -> None:
    """仅在最新路径缺失时复制旧插件名的配置和数据，绝不覆盖冲突。"""
    for base in (Path("config") / "plugins", Path("data") / "plugins"):
        # 旧错误名 -> 上一版名 -> 当前名，逐级迁移
        legacy = base / "EMofoxForMaibotPulgins"
        previous = base / "MofoxForMaibotPulgins"
        current = base / "mofox_for_maibot_plugins"
        # 先把旧错误名迁移到上一版名（保留原逻辑）
        if legacy.exists() and not previous.exists():
            try:
                previous.parent.mkdir(parents=True, exist_ok=True)
                if legacy.is_dir():
                    shutil.copytree(legacy, previous)
                else:
                    shutil.copy2(legacy, previous)
                logger.info(f"MaiBot 名称迁移完成: {legacy} -> {previous}")
            except OSError as exc:
                logger.error(f"MaiBot 名称迁移失败 {legacy} -> {previous}: {exc}")
        # 再把上一版名迁移到当前名
        source = previous if previous.exists() else (legacy if legacy.exists() else None)
        if not source or current.exists():
            if source and current.exists():
                logger.warning(
                    f"MaiBot 名称迁移检测到双路径，未覆盖: {source} -> {current}"
                )
            continue
        try:
            current.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, current)
            else:
                shutil.copy2(source, current)
            logger.info(f"MaiBot 名称迁移完成: {source} -> {current}")
        except OSError as exc:
            logger.error(f"MaiBot 名称迁移失败 {source} -> {current}: {exc}")


__all__ = ["MaiBotBridge", "MaiBotCompatPlugin", "MaiBotPluginBridge"]
