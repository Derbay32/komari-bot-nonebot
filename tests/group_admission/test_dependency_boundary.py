"""TSK-222/TSK-223：group_admission 依赖方向与第二持久真源静态边界验收。

验收目标（AST 静态扫描，不 import 生产代码）：

- 生产 ``group_admission`` 包不得 import Redis / 文件存储 / SQLAlchemy /
  asyncpg / nonebot-plugin-orm，也不得出现 DDL 字面量——PostgreSQL 强类型
  配置表（经 config_manager）是唯一持久真源，LKG 只驻留进程内存；
- ``group_admission`` 不得 import 任何业务插件；跨 ``komari_bot`` 绝对
  import 仅允许 ``komari_bot.plugins.config_manager`` 顶层暴露面
  （ADR-0006）与 ``komari_bot.management`` 共享管理包（TSK-223 控制面
  鉴权/审计，其子模块导入形态与 user_ban 等既有消费方一致），其余一律走
  包内 relative import 或 NoneBot 装配；
- 其他生产代码不得 deep import ``group_admission`` 内部子模块（测试代码
  的 module-owned 深 import 属已约定 seam，不在限制范围）；
- TSK-223 阶段 A 静态断言：不引入 config_schema/migration、
  komari_management 挂载/require、Prometheus/OTel/全局指标、拒绝表/
  Redis 明细/独立 JSONL/AgentRun（共享 management audit 导入不算新增独
  立 JSONL）。

生产包目录缺失时以断言失败（red）报告，而非跳过。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.group_admission_acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = PROJECT_ROOT / "komari_bot" / "plugins"
PACKAGE_DIR = PLUGINS_DIR / "group_admission"

#: 第二持久真源 / 存储层禁用 import 根：Redis、文件存储桥、SQL 栈与直接
#: ORM 会话。策略持久化只允许经 config_manager 的版本化快照基础设施。
FORBIDDEN_IMPORT_ROOTS = {
    "redis",
    "aioredis",
    "asyncpg",
    "sqlalchemy",
    "sqlmodel",
    "nonebot_plugin_orm",
    "aiofiles",
}

#: 唯一允许的跨 komari_bot 绝对 import（必须恰好是顶层模块，禁止深 import）。
ALLOWED_KOMARI_ABSOLUTE_MODULES = {
    "komari_bot.plugins.config_manager",
}

#: TSK-223：共享管理包允许任意子模块（鉴权/审计工具面，与 user_ban 等既有
#: 消费方的导入形态一致）；komari_management **插件** 仍被禁止。
ALLOWED_KOMARI_PREFIXES = ("komari_bot.management.",)

DDL_MARKERS = ("CREATE TABLE", "CREATE INDEX", "ALTER TABLE", "DROP TABLE")

#: TSK-223 阶段 A 包内禁止出现的遥测/第二持久面文本标记（共享 management
#: audit 导入本身不在此列，它不算新增独立 JSONL）。
#: TSK-223 阶段 A 包内禁止出现的第二持久面文本标记（共享 management
#: audit 导入本身不在此列，它不算新增独立 JSONL）。库级禁用依赖走
#: import 目标扫描，避免 docstring 提及禁令本身时误报。
FORBIDDEN_PHASE_A_MARKERS = (
    ".jsonl",
    "/metrics",
)

#: 阶段 A 控制面也不得引入遥测库、AgentRun 或 Redis 明细面（与头部存储
#: 禁用根互补，单独列出以便归因）。
FORBIDDEN_PHASE_A_IMPORT_ROOTS = {
    "redis",
    "aioredis",
    "prometheus_client",
    "opentelemetry",
}

FORBIDDEN_PHASE_A_IMPORT_TARGET_PREFIXES = (
    "komari_bot.plugins.agent_run_logger",
)


def _package_modules() -> list[Path]:
    assert PACKAGE_DIR.is_dir(), (
        "group_admission 生产包尚未实现，无法执行依赖边界验收"
    )
    module_files = sorted(PACKAGE_DIR.rglob("*.py"))
    assert module_files, "group_admission 包内没有任何模块"
    return module_files


def _absolute_import_targets(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue  # 包内 relative import 允许
            if node.module:
                targets.append((node.lineno, node.module))
    return targets


def test_group_admission_package_exists_with_modules() -> None:
    module_files = _package_modules()
    assert (PACKAGE_DIR / "__init__.py").exists(), "包必须有顶层 __init__.py"
    assert len(module_files) >= 1


def test_group_admission_has_no_forbidden_storage_or_business_imports() -> None:
    violations: list[str] = []
    for module_file in _package_modules():
        for lineno, target in _absolute_import_targets(module_file):
            root = target.split(".")[0]
            if root in FORBIDDEN_IMPORT_ROOTS:
                violations.append(
                    f"{module_file.name}:{lineno}: 禁用存储依赖 {target}"
                )
                continue
            if target.startswith("komari_bot."):
                if target in ALLOWED_KOMARI_ABSOLUTE_MODULES:
                    continue
                if target.startswith(ALLOWED_KOMARI_PREFIXES):
                    continue
                violations.append(
                    f"{module_file.name}:{lineno}: 越界 komari_bot import {target}"
                )
    assert violations == [], (
        "group_admission 依赖方向违规（只允许 config_manager 顶层、共享 "
        "management 包、NoneBot 装配、标准库与第三方非存储依赖）: "
        f"{violations}"
    )


def test_group_admission_declares_no_sql_ddl_second_source() -> None:
    """包内不得出现建表/改表 DDL 字面量（第二持久真源信号）。"""
    offenders: list[str] = []
    for module_file in _package_modules():
        text = module_file.read_text(encoding="utf-8").upper()
        offenders.extend(
            f"{module_file.name}: {marker}"
            for marker in DDL_MARKERS
            if marker in text
        )
    assert offenders == [], f"group_admission 出现 DDL 字面量: {offenders}"


def _iter_other_plugin_sources() -> list[tuple[str, Path]]:
    sources: list[tuple[str, Path]] = []
    for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
        if not plugin_dir.is_dir() or plugin_dir.name == "group_admission":
            continue
        sources.extend(
            (plugin_dir.name, module_file)
            for module_file in sorted(plugin_dir.rglob("*.py"))
        )
    return sources


def test_other_plugins_do_not_deep_import_group_admission_internals() -> None:
    """ADR-0006：跨插件引用只允许 group_admission 顶层暴露面。"""
    offenders: list[str] = []
    for plugin_name, module_file in _iter_other_plugin_sources():
        for lineno, target in _absolute_import_targets(module_file):
            if target.startswith("komari_bot.plugins.group_admission."):
                offenders.append(f"{plugin_name}/{module_file.name}:{lineno}: {target}")
    assert offenders == [], (
        f"发现指向 group_admission 内部子模块的跨插件 import: {offenders}"
    )


def test_core_komari_bot_has_no_group_admission_deep_imports() -> None:
    """komari_bot 非插件代码（core/db/llm/config/management 等）同样不得
    deep import group_admission 内部。"""
    offenders: list[str] = []
    for module_file in sorted((PROJECT_ROOT / "komari_bot").rglob("*.py")):
        if PACKAGE_DIR in module_file.parents:  # group_admission 包内部允许
            continue
        for lineno, target in _absolute_import_targets(module_file):
            if target.startswith("komari_bot.plugins.group_admission."):
                relative = module_file.relative_to(PROJECT_ROOT)
                offenders.append(f"{relative}:{lineno}: {target}")
    assert offenders == [], (
        f"发现 komari_bot 内部对 group_admission 的 deep import: {offenders}"
    )


# ---------------------------------------------------------------------------
# TSK-223 阶段 A：控制面落地但不引入越界生产工件（静态白盒断言）
# ---------------------------------------------------------------------------


def test_phase_a_does_not_introduce_config_schema_or_migration() -> None:
    """本票不创建 config_schema.py，也不新增任何群准入迁移。"""
    assert not (PACKAGE_DIR / "config_schema.py").exists(), (
        "阶段 A 不得创建 group_admission config_schema.py"
    )
    migrations_dir = PROJECT_ROOT / "migrations" / "versions"
    assert migrations_dir.is_dir(), "迁移版本目录缺失"
    offenders: list[str] = []
    for migration_file in sorted(migrations_dir.glob("*.py")):
        text = migration_file.read_text(encoding="utf-8")
        if "group_admission" in text:
            offenders.append(migration_file.name)
    assert offenders == [], f"阶段 A 不得引入群准入迁移: {offenders}"


def test_phase_a_komari_management_plugin_does_not_mount_group_admission() -> None:
    """本票不修改 komari_management 最终挂载，也不在其引入 require/import。"""
    management_plugin_dir = PLUGINS_DIR / "komari_management"
    assert management_plugin_dir.is_dir(), "komari_management 插件目录缺失"
    offenders: list[str] = []
    for module_file in sorted(management_plugin_dir.rglob("*.py")):
        text = module_file.read_text(encoding="utf-8")
        if "group_admission" in text:
            offenders.append(module_file.name)
    assert offenders == [], (
        f"阶段 A 不得在 komari_management 挂载/引用 group_admission: {offenders}"
    )


def test_phase_a_has_no_telemetry_or_side_persistence_channels() -> None:
    """阶段 A 不引入 Prometheus/OTel/全局指标、拒绝表/Redis 明细/独立
    JSONL/AgentRun；共享 management audit 导入不算新增独立 JSONL。"""
    offenders: list[str] = []
    for module_file in _package_modules():
        lowered = module_file.read_text(encoding="utf-8").lower()
        offenders.extend(
            f"{module_file.name}: {marker}"
            for marker in FORBIDDEN_PHASE_A_MARKERS
            if marker in lowered
        )
        for lineno, target in _absolute_import_targets(module_file):
            root = target.split(".")[0]
            if root in FORBIDDEN_PHASE_A_IMPORT_ROOTS:
                offenders.append(
                    f"{module_file.name}:{lineno}: 禁止的遥测/存储 import {target}"
                )
            elif target.startswith(FORBIDDEN_PHASE_A_IMPORT_TARGET_PREFIXES):
                offenders.append(
                    f"{module_file.name}:{lineno}: 禁止的 AgentRun import {target}"
                )
    assert offenders == [], f"阶段 A 出现越界遥测/第二持久面: {offenders}"
