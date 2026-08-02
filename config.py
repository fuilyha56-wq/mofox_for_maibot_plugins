"""MaiBot 兼容桥的配置模型。"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class MaiBotCompatConfig(BaseConfig):
    """定义待加载 MaiBot 插件的位置与启用状态。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "MaiBot 插件兼容桥配置"

    @config_section("plugin", title="插件设置", tag="plugin")
    class PluginSection(SectionBase):
        """兼容桥基础配置。"""

        enabled: bool = Field(default=True, description="是否启用兼容桥", label="启用兼容桥")
        plugins_root: str = Field(
            default="",
            description="MaiBot 插件仓库目录：每个子目录作为一个插件加载（须含 plugin.py）。留空时默认为桥同级目录下的 plugins/ 子目录",
            label="插件仓库目录",
            input_type="file",
            tag="file",
        )
        plugin_path: str = Field(
            default="",
            description="（可选）单个 MaiBot 插件目录；留空时使用插件仓库目录批量加载",
            label="单个插件目录",
            input_type="file",
            tag="file",
        )
        unload_timeout_seconds: float = Field(
            default=5.0,
            description="外部 MaiBot 单元卸载 drain 与 on_unload 的最大等待秒数",
            label="卸载等待秒数",
        )
        config_version: str = Field(default="0.2.0", description="配置版本", label="配置版本", disabled=True)

    plugin: PluginSection = Field(default_factory=PluginSection)
