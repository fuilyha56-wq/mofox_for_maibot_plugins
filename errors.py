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


__all__ = [
    "ApiUnavailableError",
    "ConfigTransactionError",
    "InvalidEventHandlerResultError",
    "LateComponentRegistrationError",
    "MaiBotCompatError",
    "MaiBotUnitAdmissionError",
    "UnitUnavailableError",
    "UnsupportedMaiBotCapabilityError",
]