"""MaiBot 插件配置到 MoFox 配置的转换器。

把被兼容 MaiBot 插件的 ``PluginConfigBase`` 配置模型转换成 MoFox 的
``SectionBase`` 子类，用于动态生成桥的合并配置类，使 webui 能展示
并编辑被兼容插件的配置。

MaiBot 配置结构（Pydantic BaseModel）：
- 顶层配置类（如 ``AvatarFetcherPluginConfig``）包含若干字段
- 嵌套 ``PluginConfigBase`` 子类字段 -> 一个配置节（如 ``plugin``）
- 普通字段（bool/int/float/str/list/Literal）-> 节内字段
- UI 元数据通过 ``__ui_label__``/``__ui_icon__`` 和 ``Field(json_schema_extra=...)`` 声明

转换策略：
- 每个被兼容插件生成一个 SectionBase 子类（section 名 = maibot_{slug}）
- 顶层配置类中每个嵌套 PluginConfigBase 字段扁平化到该 SectionBase，
  字段名用 ``{section}__{field}`` 连接（避免 webui 多页）
- 普通标量字段直接映射
- 字段 UI 属性（label/description/hint/input_type 等）从 json_schema_extra 提取
"""

from __future__ import annotations

from typing import Any, get_args, get_origin

from src.kernel.config import Field, SectionBase, config_section

# MaiBot PluginConfigBase 的延迟类型检查标记，避免在 import 期依赖 SDK
_PLUGIN_CONFIG_BASE_CLS: type | None = None


def _is_plugin_config_class(annotation: Any) -> bool:
    """判断注解是否为 MaiBot PluginConfigBase 子类。"""
    global _PLUGIN_CONFIG_BASE_CLS
    if _PLUGIN_CONFIG_BASE_CLS is None:
        try:
            from maibot_sdk.config import PluginConfigBase  # type: ignore[import-not-found]

            _PLUGIN_CONFIG_BASE_CLS = PluginConfigBase
        except ImportError:
            return False
    return isinstance(annotation, type) and issubclass(
        annotation, _PLUGIN_CONFIG_BASE_CLS
    )


def _extract_json_extra(field_info: Any) -> dict[str, Any]:
    """把 Pydantic FieldInfo 的 json_schema_extra 归一化为 dict。"""
    extra = getattr(field_info, "json_schema_extra", None)
    if isinstance(extra, dict):
        return extra
    return {}


def _extract_numeric_constraints(field_info: Any) -> dict[str, Any]:
    """从 FieldInfo 提取 ge/le/step 约束。"""
    result: dict[str, Any] = {}
    json_extra = _extract_json_extra(field_info)
    if "min" in json_extra:
        result["ge"] = json_extra["min"]
    if "max" in json_extra:
        result["le"] = json_extra["max"]
    if "step" in json_extra and json_extra["step"] is not None:
        result["step"] = json_extra["step"]
    return result


def _extract_literal_choices(annotation: Any) -> list[Any] | None:
    """提取 Literal 注解的可选值列表。"""
    origin = get_origin(annotation)
    if str(origin) != "typing.Literal":
        return None
    choices = list(get_args(annotation))
    return choices or None


def _extract_field_ui_attrs(field_info: Any) -> dict[str, Any]:
    """从 FieldInfo 提取 webui 显示属性。"""
    json_extra = _extract_json_extra(field_info)
    attrs: dict[str, Any] = {}
    label = json_extra.get("label")
    if label:
        attrs["label"] = str(label)
    hint = json_extra.get("hint")
    if hint:
        attrs["hint"] = str(hint)
    placeholder = json_extra.get("placeholder")
    if placeholder:
        attrs["placeholder"] = str(placeholder)
    input_type = json_extra.get("input_type")
    if input_type:
        attrs["input_type"] = str(input_type)
    icon = json_extra.get("x-icon")
    if icon:
        attrs["icon"] = str(icon)
    if json_extra.get("hidden"):
        attrs["hidden"] = True
    if json_extra.get("disabled"):
        attrs["disabled"] = True
    return attrs


def _make_mutable_default_factory(value: Any) -> Any:
    """为可变默认值（list/dict）生成 default_factory。"""

    def _factory() -> Any:
        if isinstance(value, list):
            return list(value)
        if isinstance(value, dict):
            return dict(value)
        return value

    return _factory


def _convert_field(
    field_name: str,
    field_info: Any,
    namespace: dict[str, Any],
    new_annotations: dict[str, Any],
    *,
    prefix: str = "",
) -> bool:
    """转换单个字段到 namespace/new_annotations，返回是否成功。

    嵌套 PluginConfigBase 字段会被扁平化：子字段以 ``{section}__{field}``
    命名合并到同一 SectionBase，避免 webui 按嵌套 section 分页显示。

    Args:
        field_name: 原始字段名
        field_info: Pydantic FieldInfo
        namespace: 目标类 namespace
        new_annotations: 目标类注解字典
        prefix: 扁平化字段名前缀（嵌套 section 递归时使用）
    """
    annotation = getattr(field_info, "annotation", str)
    description = getattr(field_info, "description", "") or ""
    default = getattr(field_info, "default", None)
    has_default = getattr(field_info, "is_required", lambda: False)() is False

    # 嵌套 PluginConfigBase：扁平化子字段
    if _is_plugin_config_class(annotation):
        child_prefix = f"{prefix}{field_name}__" if prefix else f"{field_name}__"
        for child_name, child_info in annotation.model_fields.items():
            if not _convert_field(
                child_name, child_info, namespace, new_annotations, prefix=child_prefix
            ):
                new_annotations.pop(f"{child_prefix}{child_name}", None)
                namespace.pop(f"{child_prefix}{child_name}", None)
        return True

    # 扁平化后的最终字段名
    flat_name = f"{prefix}{field_name}" if prefix else field_name
    label = description if prefix else None
    ui_attrs = _extract_field_ui_attrs(field_info)
    if label is not None and "label" not in ui_attrs:
        ui_attrs["label"] = label

    origin = get_origin(annotation)
    choices = _extract_literal_choices(annotation)

    # 枚举（Literal）字段
    if choices is not None:
        from typing import Literal

        try:
            py_type = Literal[tuple(choices)]  # type: ignore[valid-type]
        except TypeError:
            py_type = str
        fallback_default = choices[0] if choices else ""
        namespace[flat_name] = Field(
            default=default
            if has_default and default is not None
            else fallback_default,
            description=description,
            choices=choices,
            **ui_attrs,
        )
        new_annotations[flat_name] = py_type
        return True

    # list 字段
    if annotation is list or origin is list:
        args = get_args(annotation)
        item_type = args[0] if args else Any
        try:
            py_type = list[item_type]  # type: ignore[valid-type]
        except TypeError:
            py_type = list  # type: ignore[valid-type]
        if isinstance(default, list):
            namespace[flat_name] = Field(
                default_factory=_make_mutable_default_factory(default),
                description=description,
                input_type="list",
                **ui_attrs,
            )
        else:
            namespace[flat_name] = Field(
                default=[],
                description=description,
                input_type="list",
                **ui_attrs,
            )
        new_annotations[flat_name] = py_type
        return True

    # 基础类型
    if annotation is bool:
        py_type = bool
        fallback = False
    elif annotation is int:
        py_type = int
        fallback = 0
    elif annotation is float:
        py_type = float
        fallback = 0.0
    elif annotation is str:
        py_type = str
        fallback = ""
    else:
        # 未知类型兜底为 str
        py_type = str
        fallback = ""

    constraints = _extract_numeric_constraints(field_info)

    # 带 slider 约束的数值
    if constraints and annotation in (int, float):
        final_default = default if has_default and default is not None else fallback
        namespace[flat_name] = Field(
            default=final_default,
            description=description,
            input_type="slider",
            **constraints,
            **ui_attrs,
        )
    elif isinstance(default, (list, dict)):
        namespace[flat_name] = Field(
            default_factory=_make_mutable_default_factory(default),
            description=description,
            **ui_attrs,
        )
    else:
        final_default = default if has_default and default is not None else fallback
        namespace[flat_name] = Field(
            default=final_default,
            description=description,
            **ui_attrs,
        )
    new_annotations[flat_name] = py_type
    return True


def _convert_section(
    config_class: Any,
    *,
    section_name: str,
    title: str | None = None,
) -> type[SectionBase]:
    """把 MaiBot 顶层 PluginConfigBase 转换为扁平化的 SectionBase 子类。"""
    namespace: dict[str, Any] = {
        "__doc__": (config_class.__doc__ or "").strip(),
        "__qualname__": f"MaiBotSection_{section_name}",
    }
    new_annotations: dict[str, Any] = {}

    for field_name, field_info in config_class.model_fields.items():
        _convert_field(field_name, field_info, namespace, new_annotations)

    namespace["__annotations__"] = new_annotations
    base = type(
        f"MaiBotSection_{section_name}",
        (SectionBase,),
        namespace,
    )
    resolved_title = title or section_name
    return config_section(section_name, title=resolved_title)(base)


def convert_maibot_config(
    config_class: Any,
    plugin_slug: str,
) -> dict[str, type[SectionBase]]:
    """把 MaiBot 插件配置模型转换为 SectionBase 子类。

    Args:
        config_class: MaiBot 插件的顶层 PluginConfigBase 子类
        plugin_slug: 插件 slug，用于生成唯一 section 名

    Returns:
        dict: {"maibot_config": SectionBase 子类}（固定单 key）
    """
    if not _is_plugin_config_class(config_class):
        return {}

    section_name = f"maibot_{plugin_slug}"
    section_cls = _convert_section(
        config_class,
        section_name=section_name,
        title=f"MaiBot: {plugin_slug}",
    )
    return {"maibot_config": section_cls}


def extract_maibot_config_data(config_class: Any) -> dict[str, Any]:
    """提取 MaiBot 配置模型的默认数据字典（扁平化）。"""
    if not _is_plugin_config_class(config_class):
        return {}
    try:
        instance = config_class()
    except Exception:  # noqa: BLE001
        return {}
    return _extract_flat_data(instance.model_dump(mode="python"), config_class)


def _extract_flat_data(
    data: dict[str, Any], config_class: Any, *, prefix: str = ""
) -> dict[str, Any]:
    """递归提取默认值，嵌套 section 扁平化。"""
    result: dict[str, Any] = {}
    for field_name, field_info in config_class.model_fields.items():
        annotation = getattr(field_info, "annotation", None)
        flat_name = f"{prefix}{field_name}" if prefix else field_name
        value = data.get(field_name)
        if _is_plugin_config_class(annotation) and isinstance(value, dict):
            child_prefix = f"{flat_name}__"
            result.update(_extract_flat_data(value, annotation, prefix=child_prefix))
        else:
            result[flat_name] = value
    return result
