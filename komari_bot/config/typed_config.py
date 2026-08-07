"""动态配置与 Prompt 强类型表的通用安全公共基类。

每个动态配置资源对应一张以 ``TypedConfigModel`` 为基类的 SQLModel table，
每个 Prompt 资源对应一张以 ``TypedPromptModel`` 为基类的单行表：同一个类
同时承担 Pydantic 校验与 SQLAlchemy 映射。本模块放在 common 层且不依赖
NoneBot / 业务插件，Alembic 迁移环境可以只加载本模块与各
``config_schema`` / ``prompt_schema`` 源文件，不必导入完整插件包。

存储专用字段约定：

- ``id``：单行表固定主键（恒为 1）；
- ``revision``：CAS 修订号，存储在每次写操作时递增；
- ``updated_at``：最后写入时间（带时区）。

三者均 ``exclude=True``，``model_dump()`` 永不暴露；Pydantic 校验仍然覆盖
全部字段。SQLModel table 模型默认构造函数不执行校验，这里严格覆写
``__init__``，使直接 ``Schema(invalid=...)`` 构造仍执行完整 Pydantic 校验。
注册与校验均只依赖 SQLModel/Pydantic 公开入口，不使用 ``sqlmodel._compat``
等私有 API。
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from sqlalchemy import DateTime, Table
from sqlmodel import Field as SQLModelField
from sqlmodel import SQLModel

_TypedConfigMetaBase = type(SQLModel)
"""SQLModel 公开元类（SQLModelMetaclass），通过 type(SQLModel) 取得。"""

_VALIDATION_STATE = threading.local()
"""线程级构造保护：model_validate 内部会无参构造实例，需跳过二次校验。"""


def _utcnow() -> datetime:
    """返回带时区的当前时间。"""
    return datetime.now(UTC)


def typed_model_config(**kwargs: Any) -> Any:
    """构造配置表子类的 ``model_config``。

    SQLModel 把 ``model_config`` 声明为私有的 ``SQLModelConfig`` TypedDict
    （位于 ``sqlmodel._compat``，且附带 ``table`` / ``registry`` 键），
    子类直接赋值 ``ConfigDict`` 会被 Pyright 判定为类型不兼容。本工厂在
    单点完成类型擦除：返回值运行时就是标准 ``ConfigDict`` 字典，SQLModel
    元类的全部读取 / 写入行为不受影响，配置子类无需逐文件写忽略注释。
    """
    from pydantic import ConfigDict

    return ConfigDict(**kwargs)


TYPED_CONFIG_MODEL_REGISTRY: dict[str, type[TypedConfigModel]] = {}
"""plugin_name → table 模型类 的进程内注册表。"""

TYPED_PROMPT_MODEL_REGISTRY: dict[str, type[TypedPromptModel]] = {}
"""prompt resource_id → prompt table 模型类 的进程内注册表。"""


def _register_typed_config_model(cls: type[TypedConfigModel]) -> None:
    """把已完成表结构构建的强类型模型注册进进程内注册表。

    Prompt 表（声明非空 ``prompt_resource_id``）以资源 ID 为键注册进独立的
    ``TYPED_PROMPT_MODEL_REGISTRY``，不与配置资源共用 ``plugin_name`` 槽位：
    像 ``group_history_summary`` 这类资源 ID 与同名插件配置资源重叠时，
    共用注册表必然产生注册冲突。配置表沿用 ``plugin_name`` 键。
    """
    prompt_resource_id = getattr(cls, "prompt_resource_id", "") or ""
    if prompt_resource_id:
        if not issubclass(cls, TypedPromptModel):
            msg = (
                f"{cls.__name__} 声明了 prompt_resource_id，"
                "Prompt 表模型必须继承 TypedPromptModel"
            )
            raise TypeError(msg)
        existing_prompt = TYPED_PROMPT_MODEL_REGISTRY.get(prompt_resource_id)
        if existing_prompt is not None and existing_prompt is not cls:
            msg = (
                f"Prompt 资源 {prompt_resource_id} 的表已注册为 "
                f"{existing_prompt.__name__}，不能重复注册 {cls.__name__}"
            )
            raise RuntimeError(msg)
        TYPED_PROMPT_MODEL_REGISTRY[prompt_resource_id] = cls
        return
    if not cls.plugin_name:
        msg = f"{cls.__name__} 是配置表模型，必须声明 plugin_name ClassVar"
        raise TypeError(msg)
    existing = TYPED_CONFIG_MODEL_REGISTRY.get(cls.plugin_name)
    if existing is not None and existing is not cls:
        msg = (
            f"插件 {cls.plugin_name} 的配置表已注册为 "
            f"{existing.__name__}，不能重复注册 {cls.__name__}"
        )
        raise RuntimeError(msg)
    TYPED_CONFIG_MODEL_REGISTRY[cls.plugin_name] = cls


class _TypedConfigMetaclass(_TypedConfigMetaBase):
    """在表结构构建完成后执行注册。

    元类 ``__init__`` 晚于 ``__init_subclass__``：SQLModel 在元类初始化阶段
    才构建 ``__table__`` 与 mapper，因此注册必须放在元类 ``__init__``，
    ``__init_subclass__`` 时机拿不到 ``__table__``。
    """

    def __init__(cls, classname: str, bases: tuple[type, ...], dict_: dict[str, Any], **kw: Any) -> None:  # noqa: N805
        super().__init__(classname, bases, dict_, **kw)
        # 注意：SQLModel 0.0.39 不支持表模型再子类化（继承字段的 sa_type
        # 元数据在重建列时丢失、字段类属性是映射列描述符），表模型的
        # 子类化视为未定义行为，测试替身请改用独立 Pydantic 模型。
        if getattr(cls, "__table__", None) is not None and issubclass(
            cls, TypedConfigModel
        ):
            _register_typed_config_model(cls)


class TypedConfigModel(SQLModel, metaclass=_TypedConfigMetaclass):
    """所有动态配置强类型表的公共基类。"""

    #: 配置资源所属插件名；table 子类必须覆写。
    plugin_name: ClassVar[str] = ""

    # SQLModel 把 ``__tablename__`` 声明为 declared_attr，导致子类直接赋值
    # 字符串时 Pyright 报类型冲突；``__table__`` 则完全没有类型声明。这里在
    # 公共基类重新声明一次（只写注解、不写值，不改变任何运行时行为），
    # 14 个配置子类即可保持自然写法，存储层也能类型安全地访问表对象。
    __tablename__: ClassVar[str]  # pyright: ignore[reportIncompatibleVariableOverride]
    __table__: ClassVar[Table]

    # 只使用 Field 关键字（primary_key/nullable 等）声明存储字段，
    # 不使用共享的 sa_column 对象：SQLAlchemy Column 实例只能绑定一张表，
    # 基类共享 Column 会导致第二个子类构造失败。SQLModel 会为每个
    # table 子类独立构造列。`updated_at` 的写入时间由存储层在每次
    # upsert/update 时显式赋值，不依赖列级 default/onupdate。
    id: int = SQLModelField(default=1, primary_key=True, exclude=True)
    revision: int = SQLModelField(default=1, nullable=False, exclude=True)
    updated_at: datetime = SQLModelField(
        default_factory=_utcnow,
        # SQLModel 类型签名只接受 sa_type 的类对象，运行时同时接受实例；
        # 带时区 DateTime 必须以实例形式传入。
        sa_type=DateTime(timezone=True),  # pyright: ignore[reportArgumentType]
        nullable=False,
        exclude=True,
    )

    def __init__(self, /, **data: Any) -> None:
        """构造时强制执行完整 Pydantic 校验。

        SQLModel 对 table 模型的默认构造等价于 ``model_construct``，不会
        运行字段约束与 validator。这里先经 ``model_validate`` 校验输入数据
        （非法时抛出 ``ValidationError``），再用校验通过的数据执行
        SQLAlchemy 兼容构造。``model_validate`` 内部会无参构造临时实例
        （线程级深度保护使其跳过重复校验），避免无限递归。
        """
        if getattr(_VALIDATION_STATE, "depth", 0) > 0:
            super().__init__(**data)
            return
        _VALIDATION_STATE.depth = 1
        try:
            validated = type(self).model_validate(data)
        finally:
            _VALIDATION_STATE.depth = 0
        model_fields = type(self).model_fields
        super().__init__(
            **{
                name: getattr(validated, name)
                for name in model_fields
                if hasattr(validated, name)
            }
        )


def get_typed_config_model(
    plugin_name: str,
) -> type[TypedConfigModel] | None:
    """按插件名返回已注册的配置表模型；未注册返回 None。"""
    return TYPED_CONFIG_MODEL_REGISTRY.get(plugin_name)


class TypedPromptModel(TypedConfigModel):
    """Prompt 资源强类型表的公共基类。

    每个 Prompt 资源一张单行表，存储专用字段（id/revision/updated_at）与
    严格构造语义完全继承配置表。注册键使用 ``prompt_resource_id`` 而非
    ``plugin_name``：资源 ID 可能与同名插件的配置资源重叠（如
    ``group_history_summary``），独立注册表避免槽位冲突。
    """

    #: Prompt 资源唯一标识，与运行时 loader/API 的 resource_id 一致；
    #: table 子类必须覆写。
    prompt_resource_id: ClassVar[str] = ""


def _plugins_root() -> Path:
    """返回 komari_bot/plugins 目录（基于本文件位置，避免包导入副作用）。"""
    return Path(__file__).resolve().parent.parent / "plugins"


def _load_schema_module(plugin_name: str, module_stem: str) -> bool:
    """以源文件方式加载指定插件的 Schema 模块（config_schema / prompt_schema）。

    直接按文件路径创建模块，不执行插件包 ``__init__``，因此不会触发
    NoneBot 插件入口副作用，也不会访问数据库。加载成功后模型经元类注册进
    ``TYPED_CONFIG_MODEL_REGISTRY`` 或 ``TYPED_PROMPT_MODEL_REGISTRY``。
    """
    module_name = f"komari_bot.plugins.{plugin_name}.{module_stem}"
    if module_name in sys.modules:
        return True
    schema_path = _plugins_root() / plugin_name / f"{module_stem}.py"
    if not schema_path.is_file():
        return False
    spec = importlib.util.spec_from_file_location(module_name, schema_path)
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return True


def _load_config_schema_module(plugin_name: str) -> bool:
    """以源文件方式加载指定插件的 config_schema 模块。"""
    return _load_schema_module(plugin_name, "config_schema")


def _load_prompt_schema_module(plugin_name: str) -> bool:
    """以源文件方式加载指定插件的 prompt_schema 模块（允许不存在）。"""
    return _load_schema_module(plugin_name, "prompt_schema")


def ensure_typed_config_model(
    plugin_name: str,
) -> type[TypedConfigModel] | None:
    """返回插件配置表模型；注册表缺失时安全加载其 config_schema 源文件。"""
    model = TYPED_CONFIG_MODEL_REGISTRY.get(plugin_name)
    if model is not None:
        return model
    if not _load_config_schema_module(plugin_name):
        return None
    return TYPED_CONFIG_MODEL_REGISTRY.get(plugin_name)


def ensure_typed_prompt_model(
    resource_id: str,
) -> type[TypedPromptModel] | None:
    """返回 Prompt 资源的强类型表模型；缺失时安全扫描全部 prompt_schema。

    Prompt 资源 ID 与插件目录名不必一一对应（如 ``komari_memory_summary``
    的 prompt_schema 位于 ``komari_memory`` 目录），因此不按名字定向加载，
    而是扫描全部插件目录的 ``prompt_schema.py`` 源文件。
    """
    model = TYPED_PROMPT_MODEL_REGISTRY.get(resource_id)
    if model is not None:
        return model
    plugins_root = _plugins_root()
    for candidate in sorted(plugins_root.iterdir()):
        if not candidate.is_dir():
            continue
        _load_prompt_schema_module(candidate.name)
        model = TYPED_PROMPT_MODEL_REGISTRY.get(resource_id)
        if model is not None:
            return model
    return None


def load_all_typed_config_models() -> int:
    """扫描并加载全部插件的 config_schema 与 prompt_schema，返回注册成功总数。

    面向 Alembic 迁移环境：只读取源文件、不触发业务插件 ``__init__``、
    不访问数据库。总数同时包含配置表与 Prompt 强类型表。
    """
    plugins_root = _plugins_root()
    for candidate in sorted(plugins_root.iterdir()):
        if not candidate.is_dir():
            continue
        _load_config_schema_module(candidate.name)
        _load_prompt_schema_module(candidate.name)
    return len(TYPED_CONFIG_MODEL_REGISTRY) + len(TYPED_PROMPT_MODEL_REGISTRY)


def load_all_plugin_orm_models() -> int:
    """扫描并加载全部插件目录的 orm_models 无副作用模型模块。

    面向 Alembic 迁移环境：与 ``load_all_typed_config_models`` 同一套安全
    加载器，按源文件路径加载 ``orm_models.py``，不执行插件包 ``__init__``、
    不访问数据库；模块内的 SQLModel table 模型经元类注册进
    ``SQLModel.metadata``，供 autogenerate / check 对比。返回加载成功的
    模块数（不存在的目录不计数）。
    """
    loaded = 0
    plugins_root = _plugins_root()
    for candidate in sorted(plugins_root.iterdir()):
        if not candidate.is_dir():
            continue
        if _load_schema_module(candidate.name, "orm_models"):
            loaded += 1
    return loaded


def public_config_field_names(config: TypedConfigModel) -> set[str]:
    """返回配置实例对外的公共字段名集合（不含存储专用字段）。"""
    return set(config.model_dump().keys())


def Field(*args: Any, **kwargs: Any) -> Any:  # noqa: N802
    """配置 Schema 统一的字段声明入口。

    在 SQLModel ``Field`` 之上补充 ``json_schema_extra`` 直接透传：
    ``pydantic.Field`` 不识别 ``sa_type`` / ``sa_column`` 等 SQLModel
    专用参数（会被当作废弃额外键丢弃），而 ``sqlmodel.Field`` 不接受
    ``json_schema_extra`` 命名参数。本封装把 ``json_schema_extra``
    折叠进 SQLModel 的 ``schema_extra`` 透传通道，最终
    ``model_fields[name].json_schema_extra`` 与 Pydantic 原生行为一致，
    管理 API 的 secret / apply_mode 元数据读取方式不变。
    """
    json_schema_extra = kwargs.pop("json_schema_extra", None)
    if json_schema_extra is not None:
        schema_extra = dict(kwargs.pop("schema_extra", None) or {})
        schema_extra["json_schema_extra"] = json_schema_extra
        kwargs["schema_extra"] = schema_extra
    return SQLModelField(*args, **kwargs)
