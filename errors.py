"""MaiBot SDK 兼容桥的稳定失败类型。"""

from __future__ import annotations


class MaiBotCompatError(RuntimeError):
    """MaiBot 兼容桥的错误基类。"""


class MaiBotUnitAdmissionError(MaiBotCompatError):
    """外部 MaiBot 单元无法完整映射时抛出。"""


class UnitUnavailableError(MaiBotCompatError):
    """外部单元未激活、隔离、排空或卸载时抛出。"""


class UnsupportedMaiBotCapabilityError(MaiBotCompatError):
    """官方 PluginContext capability 未由宿主无损映射时抛出。"""


class ApiUnavailableError(MaiBotCompatError):
    """桥域 API 不存在、冲突、离线或所属单元不可用时抛出。"""


class LateComponentRegistrationError(MaiBotCompatError):
    """外部单元越过组件快照后试图新增宿主工具时抛出。"""


class InvalidEventHandlerResultError(MaiBotCompatError):
    """EventHandler 返回值不符合桥声明的可回写契约时抛出。"""


class ConfigTransactionError(MaiBotCompatError):
    """外部配置事务校验、回调或回滚失败时抛出。"""


class ApiRecursionError(MaiBotCompatError):
    """API 调用链检测到递归时抛出。"""


class ApiDepthExceededError(MaiBotCompatError):
    """API 调用链超过配置的最大深度时抛出。"""


class ApiPermissionError(MaiBotCompatError):
    """API 调用方无权访问目标 API 时抛出。

    仅在 owner 内部诊断使用；跨 unit 对外折叠为 unavailable。
    """


__all__ = [
    "ApiDepthExceededError",
    "ApiPermissionError",
    "ApiRecursionError",
    "ApiUnavailableError",
    "ConfigTransactionError",
    "InvalidEventHandlerResultError",
    "LateComponentRegistrationError",
    "MaiBotCompatError",
    "MaiBotUnitAdmissionError",
    "UnitUnavailableError",
    "UnsupportedMaiBotCapabilityError",
]