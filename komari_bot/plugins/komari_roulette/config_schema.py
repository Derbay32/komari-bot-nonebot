"""komari_roulette 强类型配置资源（TSK-279）。

存储真源：单行表 ``komari_roulette_config``（``id=1``、CAS ``revision``、
``updated_at``）；结构真源是本模块的 SQLModel 元数据，由 Alembic 版本链建表。

运行时语义（TSK-269 §1）：

- ``plugin_enable`` 默认 ``false``，动态生效；
- 四项道具权重默认各 1、非负、总和必须大于 0；权重只在 ``waiting → active``
  读取一次并随对局冻结，配置更新只影响之后新建的对局；
- ``action_copy_pool`` / ``final_copy_pool`` 是闭集键 → 非空模板列表的 JSONB
  配置，模板规则与校验全部委托 :mod:`komari_bot.plugins.komari_roulette.copy_pool`。

本模块按源文件被 Alembic 迁移环境加载（不执行插件包 ``__init__``），因此模块顶层
只能依赖共享层；``copy_pool`` / ``domain`` 的引用一律延迟到校验与取值时导入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import model_validator
from sqlalchemy.dialects.postgresql import JSONB

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config

if TYPE_CHECKING:
    from komari_bot.plugins.komari_roulette.domain import ItemType

__all__ = ["DynamicConfigSchema"]


def _default_action_copy_pool() -> dict[str, list[str]]:
    """代码内默认成功动作文案池（拷贝为可变列表以匹配 JSONB 列）。"""

    from .copy_pool import DEFAULT_ACTION_COPY_POOL

    return {
        key: list(templates) for key, templates in DEFAULT_ACTION_COPY_POOL.items()
    }


def _default_final_copy_pool() -> dict[str, list[str]]:
    """代码内默认终局文案池（按终局原因分键）。"""

    from .copy_pool import DEFAULT_FINAL_COPY_POOL

    return {key: list(templates) for key, templates in DEFAULT_FINAL_COPY_POOL.items()}


class DynamicConfigSchema(TypedConfigModel, table=True):
    """俄罗斯轮盘插件动态配置。"""

    plugin_name: ClassVar[str] = "komari_roulette"
    __tablename__ = "komari_roulette_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    # 插件控制
    plugin_enable: bool = Field(
        default=False,
        description="插件启用状态",
        json_schema_extra={"apply_mode": "immediate"},
    )

    # 道具权重（开局冻结）
    item_weight_magnifier: int = Field(
        default=1,
        ge=0,
        description="放大镜权重",
        json_schema_extra={"apply_mode": "immediate"},
    )
    item_weight_beer: int = Field(
        default=1,
        ge=0,
        description="啤酒权重",
        json_schema_extra={"apply_mode": "immediate"},
    )
    item_weight_burst: int = Field(
        default=1,
        ge=0,
        description="连发器权重",
        json_schema_extra={"apply_mode": "immediate"},
    )
    item_weight_lock: int = Field(
        default=1,
        ge=0,
        description="锁权重",
        json_schema_extra={"apply_mode": "immediate"},
    )

    # 结果文案（闭集键 → 非空模板列表）
    action_copy_pool: dict[str, list[str]] = Field(
        default_factory=_default_action_copy_pool,
        sa_type=JSONB,
        description="成功动作结果句文案池",
        json_schema_extra={"apply_mode": "immediate"},
    )
    final_copy_pool: dict[str, list[str]] = Field(
        default_factory=_default_final_copy_pool,
        sa_type=JSONB,
        description="completed 终局文案池",
        json_schema_extra={"apply_mode": "immediate"},
    )

    @model_validator(mode="after")
    def _validate_roulette_config(self) -> "DynamicConfigSchema":
        """跨字段校验：权重总和为正，文案池满足闭集与模板契约。"""

        total = (
            self.item_weight_magnifier
            + self.item_weight_beer
            + self.item_weight_burst
            + self.item_weight_lock
        )
        if total <= 0:
            message = "道具权重总和必须大于 0"
            raise ValueError(message)

        from .copy_pool import compile_copy_pool

        compile_copy_pool(self.action_copy_pool, self.final_copy_pool)
        return self

    def item_weights(self) -> dict[ItemType, int]:
        """当前配置的道具权重映射（领域启动动作的唯一读取入口）。"""

        from .domain import ItemType

        return {
            ItemType.MAGNIFIER: self.item_weight_magnifier,
            ItemType.BEER: self.item_weight_beer,
            ItemType.BURST: self.item_weight_burst,
            ItemType.LOCK: self.item_weight_lock,
        }
