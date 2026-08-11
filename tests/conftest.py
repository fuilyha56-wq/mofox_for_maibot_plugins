"""MaiBot 兼容桥测试公共配置。

将插件根目录以 ``mofox_for_maibot_plugins`` 包名注册到 ``sys.modules``，
使测试中的 ``from mofox_for_maibot_plugins.* import ...`` 可被解析。
同时将 Neo-MoFox 主仓库根加入 ``sys.path``，解析 ``from src.*`` 导入。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = Path(r"E:\Neo-mofox-instance\bot-3693525299\neo-mofox")

# Neo-MoFox 主仓库根（解析 from src.* 导入）
_repo_str = str(_REPO_ROOT)
if _repo_str not in sys.path:
    sys.path.insert(0, _repo_str)

# 插件根目录的目录名是 MofoxForMaibotPulgins，但包名是 mofox_for_maibot_plugins。
# 用 importlib 将物理目录注册为 mofox_for_maibot_plugins 包。
_pkg_name = "mofox_for_maibot_plugins"
if _pkg_name not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        _pkg_name,
        str(_PLUGIN_ROOT / "__init__.py"),
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    if spec is not None and spec.loader is not None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[_pkg_name] = module
        spec.loader.exec_module(module)