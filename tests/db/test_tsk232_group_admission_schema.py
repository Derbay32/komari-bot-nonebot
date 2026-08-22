"""TSK-232 —— 强类型准入表 + 生产装配的结构面红基线（无数据库）。

覆盖工单 C 区（新表与装配）：

- ``komari_group_admission_config`` 强类型单行表：以 `TypedConfigModel`
  形式注册在 ``group_admission/config_schema.py``，列结构符合 ADR-0012
  （id=1 / revision CAS / updated_at / policy JSONB NOT NULL），并进入
  ``TYPED_CONFIG_MODEL_REGISTRY``（Alembic ``env.py`` 可加载，fresh 门控
  零漂移的基础）；
- ``register_group_admission_api`` 被统一管理 API 生产装配：
  ``ManagementApiComponents`` 数据类有对应 getter/注册字段、
  ``register_management_api_for_driver`` 调用之；
  ``komari_management/__init__.py`` 的 ``_load_management_components``
  经 ``require("group_admission")`` 传入该注册入口；
- 禁止 alias/双读入口：不允许为新策略另建读旧 plugin_enable /
  user_whitelist 的路由。

本文件只解析生产源文件与安全加载 ``typed_config`` 源文件，不执行业务插件
包 ``__init__``、不访问数据库、不触 NoneBot。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_ROOT = PROJECT_ROOT / "komari_bot" / "plugins"

ADMISSION_DIR = PLUGINS_ROOT / "group_admission"
MGMT_DIR = PLUGINS_ROOT / "komari_management"
MANAGEMENT_RUNTIME = MGMT_DIR / "api_runtime.py"


def test_group_admission_config_schema_model_registered() -> None:
    """group_admission/config_schema.py 存在且注册进 typed_config 注册表。"""
    schema = ADMISSION_DIR / "config_schema.py"
    assert schema.is_file(), "group_admission 缺少 config_schema.py（强类型准入表模型）"

    from komari_bot.config.typed_config import ensure_typed_config_model

    model = ensure_typed_config_model("group_admission")
    assert model is not None, "group_admission 未注册进 TYPED_CONFIG_MODEL_REGISTRY"
    assert model.__tablename__ == "komari_group_admission_config"
    for field_name in ("id", "revision", "updated_at", "policy"):
        assert field_name in model.model_fields, (
            f"group_admission 强类型表缺少字段 {field_name}"
        )
    # ADR-0012: policy 为 JSONB；强类型单行表约定 id/revision/updated_at 由
    # TypedConfigModel 基类提供。列级 NOT NULL/缺省由真实迁移门控
    # （test_tsk232_group_admission_gate）验证，这里只守模型字段面。
    source = schema.read_text(encoding="utf-8")
    assert "policy" in source


def test_group_admission_config_table_in_orm_metadata() -> None:
    """加载全部 typed config/orm 模型后，准入表必须在 SQLModel.metadata。"""
    from sqlmodel import SQLModel

    from komari_bot.config.typed_config import (
        load_all_plugin_orm_models,
        load_all_typed_config_models,
    )

    load_all_typed_config_models()
    load_all_plugin_orm_models()
    names = sorted(SQLModel.metadata.tables)
    assert "komari_group_admission_config" in names, (
        "komari_group_admission_config 必须被迁移环境（env.py）加载到 metadata"
    )


def test_management_components_has_group_admission_register_field() -> None:
    """ManagementApiComponents 数据类必须声明 register_group_admission_api 字段。"""
    source = MANAGEMENT_RUNTIME.read_text(encoding="utf-8")
    dataclass_block = re.search(
        r"@dataclass[^\n]*\nclass ManagementApiComponents:.*?\n?(?=\n\S|\Z)",
        source,
        re.DOTALL,
    )
    assert dataclass_block is not None, "未找到 ManagementApiComponents dataclass"
    assert re.search(
        r"register_group_admission_api[:\s]*[^\n]*",
        dataclass_block.group(0),
    ), "ManagementApiComponents 必须声明 register_group_admission_api 字段"


def test_management_runtime_registers_group_admission() -> None:
    """register_management_api_for_driver 必须调用 group admission 注册入口。"""
    source = MANAGEMENT_RUNTIME.read_text(encoding="utf-8")
    assert re.search(r"register_group_admission_api\s*\(", source), (
        "register_management_api_for_driver 必须挂载 group-admission 路由"
    )
    # 路由前缀（ADR 专属三路由）
    assert '/api/v2/group-admission/policy' in source or 'group-admission' in source


def test_management_loader_requires_group_admission() -> None:
    """_load_management_components 需 require("group_admission") 并接注册入口。"""
    loader_source = (MGMT_DIR / "__init__.py").read_text(encoding="utf-8")
    assert re.search(
        r'require\(["\']group_admission["\']\)', loader_source
    ), "komari_management/_load_management_components 必须 require group_admission"
    assert re.search(
        r"register_group_admission_api\s*=", loader_source,
    ), "组件装配必须把 entry 传入 ManagementApiComponents"


def test_no_admission_legacy_compat_seam() -> None:
    """准入插件不得读取/回退旧名单或提供 user_whitelist 兼容参数。"""
    init = (ADMISSION_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "user_whitelist" not in init
    assert "group_whitelist" not in init
    assert "plugin_enable" not in init, (
        "ADR-0012「不提供 plugin_enable」：准入插件不得提供关闭总闸"
    )
    # runtime/contracts/management_api 同为活代码，整体扫旧名单
    for sub in ("policy.py", "runtime.py", "management_api.py", "contracts.py"):
        text = (ADMISSION_DIR / sub).read_text(encoding="utf-8")
        assert "user_whitelist" not in text, f"{sub} 不得出现 user_whitelist"
        assert "group_whitelist" not in text, f"{sub} 不得出现 group_whitelist"