"""MaiBot 0.11.x legacy plugin runtime.

This profile exposes the public ``src.plugin_system`` ABI used by MaiBot
0.11.x plugins while keeping imported modules isolated from Neo-MoFox.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator

import inspect
import json
import logging
import re
import sys
import tomllib

from maibot_sdk import Field, PluginConfigBase
from pydantic import create_model
from src.app.plugin_system.api import send_api
from src.core.models.message import Message

from .runtime import (
    MaiBotComponent,
    MaiBotRealm,
    MaiBotUnitState,
)

logger = logging.getLogger("maibot_compat.legacy_runtime")


@dataclass
class ConfigField:
    """MaiBot 0.11.x configuration field declaration."""

    type: type
    default: Any
    description: str
    example: str | None = None
    required: bool = False
    choices: list[Any] | None = field(default_factory=list)


class LegacyComponentType(Enum):
    """MaiBot 0.11.x component type values."""

    ACTION = "action"
    COMMAND = "command"
    TOOL = "tool"
    SCHEDULER = "scheduler"
    EVENT_HANDLER = "event_handler"

    def __str__(self) -> str:
        return self.value


@dataclass
class ComponentInfo:
    """MaiBot 0.11.x component metadata."""

    name: str
    component_type: LegacyComponentType
    description: str = ""
    enabled: bool = True
    plugin_name: str = ""
    is_built_in: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandInfo(ComponentInfo):
    """MaiBot 0.11.x command metadata."""

    command_pattern: str = ""

    def __post_init__(self) -> None:
        self.component_type = LegacyComponentType.COMMAND


class BaseCommand(ABC):
    """MaiBot 0.11.x command base class."""

    command_name: str = ""
    command_description: str = ""
    command_pattern: str = ""

    def __init__(
        self, message: Any, plugin_config: dict[str, Any] | None = None
    ) -> None:
        self.message = message
        self.matched_groups: dict[str, str] = {}
        self.plugin_config = plugin_config or {}
        self.log_prefix = "[Command]"

    def set_matched_groups(self, groups: dict[str, str]) -> None:
        """Store named regex groups for the current command invocation."""

        self.matched_groups = groups

    def get_config(self, key: str, default: Any = None) -> Any:
        """Read a nested plugin configuration value by dotted path."""

        current: Any = self.plugin_config
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def _source_route(self) -> tuple[str, str, str]:
        """Return stream, platform and current message ID for host send APIs."""

        chat_stream = getattr(self.message, "chat_stream", None)
        stream_id = str(getattr(chat_stream, "stream_id", "") or "")
        if not stream_id:
            raise RuntimeError("Legacy MaiBot command has no source stream_id")
        platform = str(getattr(chat_stream, "platform", "") or "")
        message_id = str(getattr(self.message, "message_id", "") or "")
        return stream_id, platform, message_id

    async def send_text(
        self,
        content: str,
        set_reply: bool = False,
        reply_message: Any = None,
        storage_message: bool = True,
    ) -> bool:
        """Send text through the source Neo-MoFox stream."""

        del reply_message, storage_message
        stream_id, platform, message_id = self._source_route()
        return await send_api.send_text(
            content=content,
            stream_id=stream_id,
            platform=platform or None,
            reply_to=message_id if set_reply and message_id else None,
        )

    async def send_image(
        self,
        image_base64: str,
        set_reply: bool = False,
        reply_message: Any = None,
        storage_message: bool = True,
    ) -> bool:
        """Send an image through the source Neo-MoFox stream."""

        del reply_message, storage_message
        stream_id, platform, message_id = self._source_route()
        return await send_api.send_image(
            image_data=image_base64,
            stream_id=stream_id,
            platform=platform or None,
            reply_to=message_id if set_reply and message_id else None,
        )

    async def send_emoji(
        self,
        emoji_base64: str,
        set_reply: bool = False,
        reply_message: Any = None,
        storage_message: bool = True,
    ) -> bool:
        """Send an emoji through the source Neo-MoFox stream."""

        del reply_message, storage_message
        stream_id, platform, _message_id = self._source_route()
        return await send_api.send_emoji(
            emoji_data=emoji_base64,
            stream_id=stream_id,
            platform=platform or None,
        )

    async def send_voice(self, voice_base64: str) -> bool:
        """Send voice data through the source Neo-MoFox stream."""

        stream_id, platform, _message_id = self._source_route()
        return await send_api.send_voice(
            voice_data=voice_base64,
            stream_id=stream_id,
            platform=platform or None,
        )

    @classmethod
    def get_command_info(cls) -> CommandInfo:
        """Build the legacy command descriptor from class attributes."""

        if "." in cls.command_name:
            raise ValueError(f"Command name contains '.': {cls.command_name}")
        return CommandInfo(
            name=cls.command_name,
            component_type=LegacyComponentType.COMMAND,
            description=cls.command_description,
            command_pattern=cls.command_pattern,
        )

    @abstractmethod
    async def execute(self) -> tuple[bool, str | None, bool]:
        """Execute the command and return success, response and block flag."""


class BasePlugin(ABC):
    """MaiBot 0.11.x plugin base class with isolated configuration loading."""

    plugin_name: str = ""
    enable_plugin: bool = True
    dependencies: list[str] = []
    python_dependencies: list[str] = []
    config_file_name: str = "config.toml"
    config_schema: dict[str, Any] = {}
    config_section_descriptions: dict[str, str] = {}

    def __init__(self, plugin_dir: str) -> None:
        self.plugin_dir = plugin_dir
        self.config = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        defaults = _legacy_defaults(self.config_schema)
        config_path = Path(self.plugin_dir) / self.config_file_name
        if not config_path.is_file():
            return defaults
        with config_path.open("rb") as config_file:
            loaded = tomllib.load(config_file)
        return _merge_config(defaults, loaded)

    @abstractmethod
    def get_plugin_components(self) -> list[tuple[ComponentInfo, type[Any]]]:
        """Return the plugin's legacy component declarations."""


def register_plugin(plugin_class: type[BasePlugin]) -> type[BasePlugin]:
    """Record a legacy plugin class without touching a host-global registry."""

    if not issubclass(plugin_class, BasePlugin):
        return plugin_class
    if "." in plugin_class.plugin_name:
        raise ValueError(f"Plugin name contains '.': {plugin_class.plugin_name}")
    _REGISTERED_PLUGIN_CLASSES.append(plugin_class)
    return plugin_class


class _ChatApi:
    """Mapped subset of the MaiBot 0.11.x chat API."""

    @staticmethod
    def get_stream_type(chat_stream: Any) -> str:
        if chat_stream is None:
            raise ValueError("chat_stream cannot be None")
        return "group" if getattr(chat_stream, "group_info", None) else "private"


class LegacyPluginAdapter:
    """Expose a legacy plugin through the configuration API used by the bridge."""

    config_model: type[PluginConfigBase] | None = None

    def __init__(self, legacy: BasePlugin) -> None:
        self._legacy = legacy
        self._plugin_config_data = dict(legacy.config)

    @classmethod
    def get_config_model(cls) -> type[PluginConfigBase] | None:
        """Return the dynamic model bound to this runtime-specific adapter class."""

        return cls.config_model

    def set_plugin_config(self, config: dict[str, Any]) -> None:
        """Replace the legacy plugin configuration."""

        self._plugin_config_data = dict(config)
        self._legacy.config = dict(config)

    def get_plugin_config_data(self) -> dict[str, Any]:
        """Return a copy of the active legacy plugin configuration."""

        return dict(self._plugin_config_data)

    async def on_config_update(self, *_args: Any) -> None:
        """Invoke an optional legacy configuration callback."""

        callback = getattr(self._legacy, "on_config_update", None)
        if callable(callback):
            result = callback()
            if inspect.isawaitable(result):
                await result


class MaiBotLegacyRuntime:
    """Prepare and execute one MaiBot 0.11.x plugin in an isolated profile."""

    profile = "legacy-0.11"

    def __init__(self, realm: MaiBotRealm) -> None:
        self.realm = realm
        self.state = MaiBotUnitState.DISCOVERED
        self.plugin_dir: Path | None = None
        self.plugin_id = ""
        self.slug = ""
        self.instance: LegacyPluginAdapter | None = None
        self.components: list[MaiBotComponent] = []
        self.dynamic_tools: list[tuple[MaiBotComponent, str]] = []
        self.module_names: list[str] = []
        self._component_classes: dict[str, type[Any]] = {}
        self._legacy_modules = _build_legacy_modules()

    def prepare(self, plugin_dir: str | Path) -> None:
        """Load plugin metadata, install shims and collect legacy components."""

        self.plugin_dir = Path(plugin_dir).resolve()
        self.slug = re.sub(r"[^a-zA-Z0-9_]", "_", self.plugin_dir.name)
        manifest = self._read_manifest()
        author = manifest.get("author", "")
        if isinstance(author, dict):
            author = author.get("name", "")
        del author
        self.plugin_id = str(manifest.get("name") or self.slug)

        entry_path = self.plugin_dir / "plugin.py"
        if not entry_path.is_file():
            raise FileNotFoundError(f"Legacy MaiBot entry not found: {entry_path}")

        try:
            _REGISTERED_PLUGIN_CLASSES.clear()
            with self._installed_shims():
                module = self._load_entry(entry_path)
                plugin_class = self._find_plugin_class(module)
                legacy_instance = plugin_class(str(self.plugin_dir))

            config_model = _build_config_model(legacy_instance.config_schema, self.slug)
            adapter_class = type(
                f"{self.slug.title()}LegacyPluginAdapter",
                (LegacyPluginAdapter,),
                {"config_model": config_model},
            )
            self.instance = adapter_class(legacy_instance)
            self.components = []
            self._component_classes.clear()
            for order, (component_info, component_class) in enumerate(
                legacy_instance.get_plugin_components()
            ):
                if not component_info.enabled:
                    continue
                component_type = str(component_info.component_type).upper()
                if component_type != "COMMAND":
                    raise RuntimeError(
                        "MaiBot 0.11 legacy profile currently maps only COMMAND components; "
                        f"unsupported component: {component_info.name} ({component_type})"
                    )
                metadata = dict(component_info.metadata)
                command_pattern = getattr(component_info, "command_pattern", "")
                if command_pattern:
                    metadata["command_pattern"] = command_pattern
                descriptor = MaiBotComponent(
                    name=component_info.name,
                    component_type=component_type,
                    metadata=metadata,
                    handler=component_class,
                    order=order,
                )
                self.components.append(descriptor)
                self._component_classes[component_info.name] = component_class
            self.state = MaiBotUnitState.PREPARED
        except Exception:
            self._cleanup_modules()
            self.instance = None
            self.components.clear()
            self.dynamic_tools.clear()
            self._component_classes.clear()
            raise

    def mark_registered(self) -> None:
        """Mark the legacy unit as visible in the host component snapshot."""

        if self.state is MaiBotUnitState.PREPARED:
            self.state = MaiBotUnitState.REGISTERED

    async def activate(self) -> None:
        """Invoke the optional legacy load callback."""

        if self.state is not MaiBotUnitState.REGISTERED or self.instance is None:
            return
        callback = getattr(self.instance._legacy, "on_load", None)
        if callable(callback):
            with self._installed_shims():
                result = callback()
                if inspect.isawaitable(result):
                    await result
        self.state = MaiBotUnitState.ACTIVE

    async def drain_and_unload(self, _timeout: float = 5.0) -> None:
        """Invoke the optional unload callback and remove owned modules."""

        if self.instance is not None:
            callback = getattr(self.instance._legacy, "on_unload", None)
            if callable(callback):
                with self._installed_shims():
                    result = callback()
                    if inspect.isawaitable(result):
                        await result
        await self.unload()

    async def dispatch_message(self, message: Message) -> tuple[bool, bool, Message]:
        """Match and execute legacy command components for one inbound message."""

        if self.instance is None:
            return False, False, message
        text = str(message.processed_plain_text or message.content or "")
        matched = False
        blocked = False
        legacy_message = _build_legacy_message(message)
        for component in self.components:
            if component.component_type != "COMMAND":
                continue
            pattern = str(component.metadata.get("command_pattern") or "")
            match = re.search(pattern, text) if pattern else None
            if pattern and match is None:
                continue
            if not pattern and not text.startswith(component.name):
                continue
            matched = True
            command_class = self._component_classes[component.name]
            command_module = sys.modules.get(command_class.__module__)
            global_config = getattr(command_module, "global_config", None)
            bot_config = getattr(global_config, "bot", None)
            if bot_config is not None:
                bot_config.qq_account = str(message.extra.get("self_id") or "")
            command = command_class(
                legacy_message, self.instance.get_plugin_config_data()
            )
            if match is not None:
                command.set_matched_groups(match.groupdict())
            with self._installed_shims():
                result = await command.execute()
            if not isinstance(result, tuple) or len(result) != 3:
                raise TypeError(
                    f"Legacy MaiBot command {component.name} must return a three-item tuple"
                )
            blocked = bool(result[2]) or blocked
        return matched, blocked, message

    def _read_manifest(self) -> dict[str, Any]:
        for name in ("_manifest.json", "manifest.json"):
            path = self.plugin_dir / name  # type: ignore[operator]
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        return {}

    @contextmanager
    def _installed_shims(self) -> Iterator[None]:
        modules = self._legacy_modules
        alias_root = f"plugins.{self.plugin_dir.name}"  # type: ignore[union-attr]
        alias_package = sys.modules.get(alias_root)
        if alias_package is None:
            alias_package = ModuleType(alias_root)
            alias_package.__path__ = [str(self.plugin_dir)]  # type: ignore[attr-defined]
            alias_package.__package__ = alias_root
            sys.modules[alias_root] = alias_package
            self.module_names.append(alias_root)
        elif str(self.plugin_dir) not in getattr(alias_package, "__path__", []):
            raise ImportError(
                f"Legacy plugin alias already owned by another path: {alias_root}"
            )
        previous: dict[str, ModuleType | None] = {}
        try:
            for name, module in modules.items():
                previous[name] = sys.modules.get(name)
                sys.modules[name] = module
            yield
        finally:
            for name, old_module in previous.items():
                if old_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old_module
            for name in list(sys.modules):
                if name == alias_root or name.startswith(f"{alias_root}."):
                    self.module_names.append(name)

    def _load_entry(self, entry_path: Path) -> ModuleType:
        module_name = f"_mofox_maibot_legacy_{self.slug}.plugin"
        package_name = module_name.rpartition(".")[0]
        package = ModuleType(package_name)
        package.__path__ = [str(self.plugin_dir)]  # type: ignore[attr-defined]
        package.__package__ = package_name
        sys.modules[package_name] = package
        self.module_names.append(package_name)
        spec = spec_from_file_location(module_name, entry_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create legacy module spec for {entry_path}")
        module = module_from_spec(spec)
        sys.modules[module_name] = module
        self.module_names.append(module_name)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _find_plugin_class(module: ModuleType) -> type[BasePlugin]:
        candidates = list(_REGISTERED_PLUGIN_CLASSES)
        candidates.extend(
            value
            for value in vars(module).values()
            if inspect.isclass(value)
            and issubclass(value, BasePlugin)
            and value is not BasePlugin
        )
        if not candidates:
            raise RuntimeError(
                "Legacy MaiBot plugin did not export a BasePlugin subclass"
            )
        return candidates[-1]

    async def apply_config_transaction(
        self, config_data: dict[str, Any], version: str
    ) -> None:
        """Apply WebUI configuration to the legacy plugin adapter."""

        if self.instance is None:
            raise RuntimeError("Legacy MaiBot plugin is not prepared")
        self.instance.set_plugin_config(config_data)
        await self.instance.on_config_update("self", config_data, version)

    async def unload(self) -> None:
        """Remove modules owned by this legacy runtime."""

        self._cleanup_modules()
        self.instance = None
        self.components.clear()
        self.dynamic_tools.clear()
        self._component_classes.clear()
        self.state = MaiBotUnitState.UNLOADED

    def _cleanup_modules(self) -> None:
        """Remove private and legacy alias modules owned by this unit."""

        for module_name in reversed(self.module_names):
            sys.modules.pop(module_name, None)
        self.module_names.clear()


_REGISTERED_PLUGIN_CLASSES: list[type[BasePlugin]] = []


def _legacy_defaults(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        section_name: {
            field_name: config_field.default
            for field_name, config_field in fields.items()
            if isinstance(config_field, ConfigField)
        }
        for section_name, fields in schema.items()
        if isinstance(fields, dict)
    }


def _merge_config(defaults: dict[str, Any], loaded: dict[str, Any]) -> dict[str, Any]:
    merged = {name: dict(values) for name, values in defaults.items()}
    for section_name, section_values in loaded.items():
        if isinstance(section_values, dict) and isinstance(
            merged.get(section_name), dict
        ):
            merged[section_name].update(section_values)
        else:
            merged[section_name] = section_values
    return merged


def _build_config_model(schema: dict[str, Any], slug: str) -> type[PluginConfigBase]:
    section_fields: dict[str, Any] = {}
    for section_name, fields in schema.items():
        if not isinstance(fields, dict):
            continue
        model_fields: dict[str, Any] = {}
        for field_name, config_field in fields.items():
            if not isinstance(config_field, ConfigField):
                continue
            field_options: dict[str, Any] = {"description": config_field.description}
            if config_field.choices:
                field_options["json_schema_extra"] = {"choices": config_field.choices}
            model_fields[field_name] = (
                config_field.type,
                Field(default=config_field.default, **field_options),
            )
        section_model = create_model(
            f"{slug.title()}{section_name.title()}Config",
            __base__=PluginConfigBase,
            **model_fields,
        )
        section_fields[section_name] = (
            section_model,
            Field(default_factory=section_model, description=section_name),
        )
    return create_model(
        f"{slug.title()}LegacyConfig",
        __base__=PluginConfigBase,
        **section_fields,
    )


def _build_legacy_modules() -> dict[str, ModuleType]:
    plugin_system = ModuleType("src.plugin_system")
    plugin_system.__path__ = []  # type: ignore[attr-defined]
    exports = {
        "BaseCommand": BaseCommand,
        "BasePlugin": BasePlugin,
        "ConfigField": ConfigField,
        "ComponentInfo": ComponentInfo,
        "CommandInfo": CommandInfo,
        "ComponentType": LegacyComponentType,
        "register_plugin": register_plugin,
        "chat_api": _ChatApi,
        "get_logger": logging.getLogger,
    }
    for name, value in exports.items():
        setattr(plugin_system, name, value)

    base_package = ModuleType("src.plugin_system.base")
    base_package.__path__ = []  # type: ignore[attr-defined]
    base_command_module = ModuleType("src.plugin_system.base.base_command")
    base_command_module.BaseCommand = BaseCommand
    base_plugin_module = ModuleType("src.plugin_system.base.base_plugin")
    base_plugin_module.BasePlugin = BasePlugin
    config_types_module = ModuleType("src.plugin_system.base.config_types")
    config_types_module.ConfigField = ConfigField
    component_types_module = ModuleType("src.plugin_system.base.component_types")
    for name, value in {
        "ComponentInfo": ComponentInfo,
        "CommandInfo": CommandInfo,
        "ComponentType": LegacyComponentType,
    }.items():
        setattr(component_types_module, name, value)
    apis_package = ModuleType("src.plugin_system.apis")
    apis_package.__path__ = []  # type: ignore[attr-defined]
    chat_api_module = ModuleType("src.plugin_system.apis.chat_api")
    chat_api_module.ChatManager = _ChatApi
    for name in ("get_stream_type",):
        setattr(chat_api_module, name, getattr(_ChatApi, name))
    apis_package.chat_api = chat_api_module
    register_module = ModuleType("src.plugin_system.apis.plugin_register_api")
    register_module.register_plugin = register_plugin

    config_package = ModuleType("src.config")
    config_package.__path__ = []  # type: ignore[attr-defined]
    config_module = ModuleType("src.config.config")
    config_module.global_config = SimpleNamespace(bot=SimpleNamespace(qq_account=""))
    config_package.config = config_module
    return {
        "src.plugin_system": plugin_system,
        "src.plugin_system.base": base_package,
        "src.plugin_system.base.base_command": base_command_module,
        "src.plugin_system.base.base_plugin": base_plugin_module,
        "src.plugin_system.base.config_types": config_types_module,
        "src.plugin_system.base.component_types": component_types_module,
        "src.plugin_system.apis": apis_package,
        "src.plugin_system.apis.chat_api": chat_api_module,
        "src.plugin_system.apis.plugin_register_api": register_module,
        "src.config": config_package,
        "src.config.config": config_module,
    }


def _build_legacy_message(message: Message) -> SimpleNamespace:
    chat_type = str(message.chat_type or "").lower()
    group_id = message.extra.get("group_id") or message.extra.get("target_group_id")
    group_info = (
        SimpleNamespace(
            group_id=str(group_id),
            group_name=str(message.extra.get("group_name") or ""),
        )
        if chat_type == "group" and group_id
        else None
    )
    user_info = SimpleNamespace(
        user_id=str(message.sender_id or ""),
        user_nickname=str(message.extra.get("sender_name") or ""),
    )
    chat_stream = SimpleNamespace(
        stream_id=message.stream_id,
        platform=message.platform,
        group_info=group_info,
        user_info=user_info,
    )
    return SimpleNamespace(
        chat_stream=chat_stream,
        message_id=message.message_id,
        plain_text=str(message.processed_plain_text or message.content or ""),
        raw_message=message.content,
        session_id=message.stream_id,
    )
