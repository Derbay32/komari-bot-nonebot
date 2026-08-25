"""TSK-222/TSK-223/TSK-246：group_admission 依赖方向与消费方硬依赖声明验收。

验收目标（主要 AST 静态扫描；TSK-246 追加 NoneBot 装载器 runtime seam）：

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
- TSK-246 消费方硬依赖声明：从生产代码识别 ``group_admission`` 顶层消费
  者，断言其所属插件入口（``__init__.py``）显式 ``require("group_admission")``；
  三插件（komari_memory / user_ban / character_binding）经 NoneBot 装载器
  runtime seam 逐入口装载验证声明，消费 import 只落在顶层 ``__all__`` 内。

生产包目录缺失时以断言失败（red）报告，而非跳过。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.group_admission.entry_gate_support import snapshot_event_registries

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    # TSK-232：config_schema.py 与其余 15 个插件同地位，经 TypedConfigModel
    # 基类定义强类型单行表（结构真源），非存储访问。
    "komari_bot.config.typed_config",
    # TSK-247：统一策略指纹契约的共享 canonical 真值——运行时 compile_policy
    # 与 CLI canonical 校验的单一验证真源（纯函数，无 I/O、无插件依赖）。
    # 只允许顶层模块本身，禁止其任何子模块前缀（admission_policy.*）。
    "komari_bot.admission_policy",
}

#: TSK-232：强类型配置表的 JSONB 列类型声明（sqlalchemy.dialects）。
#: 仅允许类型声明用途，连接/会话/DDL 仍被禁止。
ALLOWED_ABSOLUTE_MODULES = {
    "sqlalchemy.dialects.postgresql",
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
                if target not in ALLOWED_ABSOLUTE_MODULES:
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


def test_komari_absolute_import_allowlist_is_exact() -> None:
    """跨 komari_bot 绝对 import 白名单必须恰好是既有项加本票新增模块。

    防止后续票顺带放宽架构边界（业务插件、存储、遥测、DDL 或 deep import
    前缀）；只锁定集合内容，不引用任何实现行号。
    """
    assert {
        "komari_bot.plugins.config_manager",
        "komari_bot.config.typed_config",
        "komari_bot.admission_policy",
    } == ALLOWED_KOMARI_ABSOLUTE_MODULES


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
    """TSK-232 已正式解除阶段 A 限制：config_schema 与群准入迁移就位。

    阶段 A 曾要求 group_admission 不建 config_schema.py / 不引入群准入迁移；
    TSK-232 破坏性 cutover 后两者均为正式契约（强类型单行表 + 0010-0013
    admission 迁移锚点），本用例改为断言它们确实存在并注册。
    """
    assert (PACKAGE_DIR / "config_schema.py").exists(), (
        "TSK-232 后 group_admission 必须持有强类型 config_schema"
    )
    migrations_dir = PROJECT_ROOT / "migrations" / "versions"
    assert migrations_dir.is_dir(), "迁移版本目录缺失"
    anchors = [
        "0010_group_admission_expand.py",
        "0012_group_admission_backfill.py",
        "0013_group_admission_cutover.py",
    ]
    for anchor in anchors:
        assert (migrations_dir / anchor).exists(), f"缺少 admission 迁移锚点 {anchor}"


def test_phase_a_komari_management_plugin_does_not_mount_group_admission() -> None:
    """TSK-232 已正式解除阶段 A 禁挂载限制：komari_management 装配准入 Router。

    阶段 A 曾禁止 komari_management 引用 group_admission；TSK-232 把
    ``register_group_admission_api`` 纳入生产装配（ManagementApiComponents +
    register_management_api_for_driver），本用例改为断言装配确实存在。
    """
    api_runtime = PLUGINS_DIR / "komari_management" / "api_runtime.py"
    assert api_runtime.exists(), "api_runtime.py 缺失"
    text = api_runtime.read_text(encoding="utf-8")
    assert "register_group_admission_api" in text, (
        "komari_management 生产装配必须挂载 group_admission Router"
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


# ---------------------------------------------------------------------------
# TSK-223 阶段 B / TSK-248 收尾：可观测性保持封闭运维诊断预算（不引入指标
# 基础设施、不显式接 Sentry）；TSK-248 起允许 driver lifecycle / scheduler
# 装配（startup/shutdown 钩子与周期 job），但仍保持存储边界 / 无第二持久真源。
# ---------------------------------------------------------------------------

#: 阶段 B 额外禁止的遥测/诊断库 import 根（与阶段 A 互补）。
FORBIDDEN_PHASE_B_IMPORT_ROOTS = {
    "prometheus_client",
    "opentelemetry",
    "sentry_sdk",
    "statsd",
    "datadog",
}

#: 阶段 B 禁止的标识符（AST 级别扫描，docstring 提及不会误报）：
#: 显式 Sentry 异常捕获、全局指标注册表。
#: TSK-248 起 **允许** driver lifecycle 装配：``on_startup`` / ``on_shutdown``
#: 钩子与 ``add_job`` 周期任务由生产生命周期票落地（见
#: test_lifecycle_driver.py / test_lifecycle_scheduler.py），因此从本禁集合中
#: 移除；生命周期行为的真实验证由那些用例承担，不以本静态扫描代替。
FORBIDDEN_PHASE_B_IDENTIFIERS = {
    "capture_exception",
    "add_event_processor",
    "CollectorRegistry",
}


def _identifier_occurrences(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    occurrences: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_PHASE_B_IDENTIFIERS:
            occurrences.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_PHASE_B_IDENTIFIERS:
            occurrences.append((node.lineno, node.attr))
    return occurrences


def test_phase_b_observability_keeps_operational_diagnostic_boundaries() -> None:
    """阶段 B 不引入指标基础设施/显式 Sentry 接口；TSK-248 起允许生命周期装配。

    可观测性只由低基数内存计数 + 现有应用日志 + 内存通知构成：不引入
    Prometheus/OTel/全局指标注册表与 ``/metrics``（阶段 A 标记扫描互补），
    不显式调用 ``capture_exception`` / 不向日志挂原始异常对象。

    TSK-248 变更：阶段 B 曾禁止注册 ``on_startup`` / ``on_shutdown`` /
    scheduler 作业（当时 ``process_observability`` 由后续 scheduler 票驱
    动）；本票落地生产生命周期装配后解除该禁止，但**存储边界与无第二持久
    真源约束由本文件其余静态用例继续守护**（不因解除钩子禁止而放开
    redis / sqlalchemy / DDL 等禁用面）。内容泄漏由金丝雀用例在运行时验收。
    """
    offenders: list[str] = []
    for module_file in _package_modules():
        for lineno, target in _absolute_import_targets(module_file):
            root = target.split(".")[0]
            if root in FORBIDDEN_PHASE_B_IMPORT_ROOTS:
                offenders.append(
                    f"{module_file.name}:{lineno}: 禁止的诊断/指标库 import {target}"
                )
        for lineno, name in _identifier_occurrences(module_file):
            offenders.append(
                f"{module_file.name}:{lineno}: 禁止的标识符 {name}"
            )
    assert offenders == [], f"阶段 B 出现越界诊断/装配面: {offenders}"


# ---------------------------------------------------------------------------
# TSK-246：group_admission 消费方硬依赖声明（consumer dependency census）
#
# 验收目标：
#
# - AST census：从生产代码识别全部 ``group_admission`` 顶层消费者，断言其
#   所属插件入口（``__init__.py``）显式 ``require("group_admission")``，未
#   来新增消费者漏声明时精确失败；兼容既有 komari_help 约定——消费模块与
#   ``require`` 同文件声明且该模块在插件装载期被入口 import（NoneBot 加载
#   期即完成声明）。
# - NoneBot 装载器 runtime seam：recording require 逐插件装载三插件
#   （komari_memory / user_ban / character_binding）生产入口，断言装载期间
#   记录到 ``group_admission`` 声明；装载副作用（matcher / preprocessor /
#   driver 生命周期钩子）精确恢复。
# - 消费 import 只落在 ``group_admission`` 顶层 ``__all__`` 内（ADR-0006 只
#   消费顶层暴露面），不引入 deep import（既有用例覆盖）。
#
# 本票不新增 public bool helper、不改群号解析/准入资格/命令回复——由既有
# user_ban / character_binding / komari_memory 公共行为测试守住零行为变化
# （见测试命令中对应目录）。
# ---------------------------------------------------------------------------

GROUP_ADMISSION_PLUGIN = "group_admission"
GROUP_ADMISSION_MODULE = "komari_bot.plugins.group_admission"

#: TSK-246 明确要求补齐硬依赖声明的三个消费者。
CONSUMER_PLUGINS = ("user_ban", "character_binding", "komari_memory")


def _module_require_names(path: Path) -> set[str]:
    """AST 提取模块顶层 ``require("...")`` 调用的字符串参数集合。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "require"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def _group_admission_import_targets(
    path: Path,
) -> list[tuple[int, str, list[str]]]:
    """提取模块对 ``group_admission`` 的全部绝对 import 形态。

    返回 ``(lineno, target, imported_names)``：

    - ``import komari_bot.plugins.group_admission`` /
      ``import komari_bot.plugins.group_admission as g`` → target 为该模块名，
      imported_names 为空；
    - ``from komari_bot.plugins.group_admission import X, Y`` → target 为该模块
      名，imported_names 为 ``["X", "Y"]``；
    - ``from komari_bot.plugins import group_admission`` /
      ``from komari_bot.plugins import group_admission as g`` → target 归一为
      ``komari_bot.plugins.group_admission``。

    只匹配 ``level == 0`` 的绝对 import；包内 relative import 属组内实现不受限。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                (node.lineno, alias.name, [])
                for alias in node.names
                if alias.name == GROUP_ADMISSION_MODULE
                or alias.name.startswith(f"{GROUP_ADMISSION_MODULE}.")
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == GROUP_ADMISSION_MODULE:
                found.append(
                    (node.lineno, node.module, [a.name for a in node.names])
                )
            elif node.module == "komari_bot.plugins":
                found.extend(
                    (node.lineno, GROUP_ADMISSION_MODULE, [])
                    for alias in node.names
                    if alias.name == "group_admission"
                )
    return found


def _iter_group_admission_consumers() -> list[tuple[str, Path]]:
    """遍历生产插件包，返回消费 ``group_admission`` 顶层面的
    ``(plugin_name, module_path)`` 列表。"""
    consumers: list[tuple[str, Path]] = []
    for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
        if not plugin_dir.is_dir() or plugin_dir.name == GROUP_ADMISSION_PLUGIN:
            continue
        consumers.extend(
            (plugin_dir.name, module_file)
            for module_file in sorted(plugin_dir.rglob("*.py"))
            if _group_admission_import_targets(module_file)
        )
    return consumers


def _top_level_all(path: Path) -> set[str]:
    """AST 提取模块顶层 ``__all__ = [...]`` 字符串集合。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
            and isinstance(node.value, ast.List)
        ):
            return {
                elt.value
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }
    return set()


def test_group_admission_consumer_plugin_entry_declares_require() -> None:
    """consumer dependency census：消费者所属插件入口必须显式声明硬依赖。

    从生产代码识别全部 ``group_admission`` 顶层消费者，断言其所属插件入口
    （``__init__.py``）包含 ``require("group_admission")``；兼容既有
    komari_help 约定（消费模块与 ``require`` 同文件声明且该模块在插件装载期
    被入口 import）。未来新增消费者漏声明时本用例精确失败。
    """
    missing: list[str] = []
    for plugin_name, module_file in _iter_group_admission_consumers():
        entry = PLUGINS_DIR / plugin_name / "__init__.py"
        declared = GROUP_ADMISSION_PLUGIN in _module_require_names(entry)
        if not declared:
            declared = GROUP_ADMISSION_PLUGIN in _module_require_names(module_file)
        if not declared:
            missing.append(
                f"{plugin_name} ({module_file.relative_to(PLUGINS_DIR)})"
            )
    assert missing == [], (
        "以下插件消费 group_admission 顶层面但未在插件入口显式声明硬依赖 "
        f'require("{GROUP_ADMISSION_PLUGIN}"): {missing}'
    )


def test_group_admission_consumer_imports_stay_within_top_level_all() -> None:
    """消费方 import 的符号必须全部落在 ``group_admission`` 顶层 ``__all__`` 内。

    ADR-0006 只消费顶层暴露面：``from komari_bot.plugins.group_admission
    import X`` 的 ``X`` 必须是顶层 ``__all__`` 成员；``import ... as pkg``
    整模块形态不在名单校验范围（运行时属性访问）。
    """
    all_symbols = _top_level_all(PACKAGE_DIR / "__init__.py")
    assert all_symbols, "group_admission 顶层 __all__ 缺失，无法校验消费面"
    violations: list[str] = []
    for plugin_name, module_file in _iter_group_admission_consumers():
        for lineno, _target, names in _group_admission_import_targets(module_file):
            violations.extend(
                f"{plugin_name}/{module_file.name}:{lineno}: {name}"
                for name in names
                if name == "*" or name not in all_symbols
            )
    assert violations == [], (
        f"消费方 import 了 group_admission 顶层未导出的符号: {violations}"
    )


def _snapshot_lifespan() -> dict[str, list[object]]:
    """快照 driver 生命周期钩子，避免装载插件入口残留 startup/shutdown 回调。"""
    from nonebot import get_driver

    lifespan = get_driver()._lifespan
    return {
        "startup": list(lifespan._startup_funcs),
        "ready": list(lifespan._ready_funcs),
        "shutdown": list(lifespan._shutdown_funcs),
    }


def _restore_lifespan(snapshot: dict[str, list[object]]) -> None:
    from nonebot import get_driver

    lifespan = get_driver()._lifespan
    lifespan._startup_funcs = list(snapshot["startup"])
    lifespan._ready_funcs = list(snapshot["ready"])
    lifespan._shutdown_funcs = list(snapshot["shutdown"])


def _restore_event_registries(snapshot: dict[str, Any]) -> None:
    """恢复 matcher 与四组 message 注册表（与 entry_gate_support 语义一致）。"""
    import nonebot.matcher as _matcher_mod
    import nonebot.message as _msg_mod

    _matcher_mod.matchers.clear()
    _matcher_mod.matchers.update(snapshot["matchers"])
    _msg_mod._run_preprocessors.clear()
    _msg_mod._run_preprocessors.update(snapshot["run_pre"])
    _msg_mod._run_postprocessors.clear()
    _msg_mod._run_postprocessors.update(snapshot["run_post"])
    _msg_mod._event_postprocessors.clear()
    _msg_mod._event_postprocessors.update(snapshot["event_post"])
    _msg_mod._event_preprocessors.clear()
    _msg_mod._event_preprocessors.update(snapshot["event_pre"])


@pytest.fixture
def _recorded_requires(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """TSK-246 runtime seam：以 recording require 包裹 conftest 桩。

    ``nonebot_plugin_apscheduler`` 返回 ``sys.modules`` 中的 dummy 模块
    （三个插件入口都在装载期 require 它）；其余委托 conftest
    ``_fake_require``（registry 内插件返回桩、未声明抛错）。
    """
    import nonebot.plugin as np

    original = np.require
    recorded: list[str] = []

    def _recording_require(name: str) -> object:
        recorded.append(name)
        if name == "nonebot_plugin_apscheduler":
            return sys.modules["nonebot_plugin_apscheduler"]
        return original(name)

    monkeypatch.setattr(np, "require", _recording_require)
    yield recorded


def _load_plugin_entry_isolated(plugin_name: str) -> str:
    """以唯一模块名装载插件生产入口（不触碰 conftest 包 shim），返回模块名。

    ``spec_from_file_location`` 对点号模块名会把 ``__package__`` 设成完整
    模块名，导致入口内的相对 import 把已加载的强类型子模块（如
    ``config_schema``）当作新模块重新执行，触发 SQLModel 表重复定义；显式
    把 spec 标记为普通模块（``submodule_search_locations=None``），使
    ``__package__`` 与 ``__spec__.parent`` 都落在插件包名上，相对 import 复用
    ``sys.modules`` 中的真实子模块。
    """
    module_name = f"komari_bot.plugins.{plugin_name}._tsk246_entry"
    sys.modules.pop(module_name, None)
    module_path = PLUGINS_DIR / plugin_name / "__init__.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        msg = f"无法装载 {plugin_name} 插件入口"
        raise RuntimeError(msg)
    spec.submodule_search_locations = None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module_name


@pytest.mark.parametrize("plugin_name", CONSUMER_PLUGINS)
def test_plugin_entry_declares_group_admission_require_on_load(
    plugin_name: str,
    _recorded_requires: list[str],
) -> None:
    """NoneBot 装载器 seam：三插件生产入口装载期必须声明
    ``require("group_admission")``。

    recording require 逐插件装载真实生产入口（spec_from_file_location 唯一
    模块名，避开 conftest 包 shim），断言装载期间记录到 group_admission
    声明；装载副作用（matcher / preprocessor / driver 生命周期钩子）在
    ``finally`` 中精确恢复。
    """
    module_name = f"komari_bot.plugins.{plugin_name}._tsk246_entry"
    snapshot = snapshot_event_registries()
    lifespan_snapshot = _snapshot_lifespan()
    try:
        _load_plugin_entry_isolated(plugin_name)
        recorded = list(_recorded_requires)
        assert GROUP_ADMISSION_PLUGIN in recorded, (
            f"{plugin_name} 插件入口装载期未调用 "
            f'require("{GROUP_ADMISSION_PLUGIN}")，记录到: {recorded}'
        )
    finally:
        sys.modules.pop(module_name, None)
        _restore_lifespan(lifespan_snapshot)
        _restore_event_registries(snapshot)


# ---------------------------------------------------------------------------
# TSK-253：registry/lifespan 恢复唯一真源边界（AST census）
#
# 验收目标：
#
# - 中立 ``registry_isolation_support.py`` 是 tests/group_admission/ 内
#   registry/lifespan 恢复的唯一真源；其他文件不得重新实现恢复写操作
#   （lifespan list 原地切片 / 属性重绑、registry ``clear`` / ``update``）。
#   census 只基于受管容器的**真实恢复写操作**判定，不用 ``_restore_*`` /
#   ``_clear_*`` 名称前缀、也不用任何旧函数名白名单豁免——存量三处恢复
#   路径（``test_dependency_boundary`` / ``entry_gate_support`` /
#   ``lifecycle_support``）正是本票要求迁移/删除的第二恢复真源，census 绝
#   不保护它们；名称与 registry/lifespan 无关的恢复/清理 helper 不含这些
#   写操作，自然放行。
# - 只读访问（``list`` / ``set`` / ``dict`` / ``items`` / ``in``）与新增
#   hooks 的差分比较（``func not in snapshot``）仍允许，不在此禁集合内。
# - 扫描基于 AST，docstring / 注释不会误报。
# - 中立 helper 不接管 ``sys.modules``、不 reload、不注入 scheduler /
#   runtime fake：不得 import ``importlib`` / ``nonebot_plugin_apscheduler`` /
#   ``pytest``，不得访问 ``sys.modules``，不得使用 ``monkeypatch``。
#
# 红基线：helper 尚未创建时，行为用例以 ``ModuleNotFoundError``、census 用
# 例以「唯一真源缺失」失败（red）；临时放入最小/正确 helper 后，census 仍
# 因上述三处既有旧恢复写操作而红（red）。只有实现代理迁移三个调用方并删除
# 这些写操作才允许转绿。
# ---------------------------------------------------------------------------

GROUP_ADMISSION_TESTS_DIR = PROJECT_ROOT / "tests" / "group_admission"

#: 被管理的 lifespan list 槽位（原地切片恢复，禁止属性重绑）。
_LIFESPAN_LIST_NAMES = frozenset(
    {"_startup_funcs", "_ready_funcs", "_shutdown_funcs"}
)

#: 被管理的 registry 容器槽位（set/dict-like，clear + update 恢复）。
_REGISTRY_CONTAINER_NAMES = frozenset(
    {
        "matchers",
        "_run_preprocessors",
        "_run_postprocessors",
        "_event_preprocessors",
        "_event_postprocessors",
    }
)

#: 恢复写操作只识别**受管容器的真实写操作**，不按 ``_restore_*`` /
#: ``_clear_*`` 名称前缀、也不按任何旧函数名判断：名称与 registry/lifespan
#: 无关的恢复/清理 helper（如 ``_restore_unrelated_state`` /
#: ``_clear_other_cache``）因不含这些写操作而自然放行。本票要求迁移并删除
#: 存量三处恢复路径（``test_dependency_boundary`` 的 ``_restore_lifespan`` /
#: ``_restore_event_registries``、``entry_gate_support`` 的
#: ``_clear_registries`` / ``_restore_registries`` / ``event_gate_context``、
#: ``lifecycle_support`` 的 ``lifecycle_context``），census 绝不豁免它们：
#: 收敛完成前它们体内的 lifespan 赋值/切片与 registry ``clear`` /
#: ``update`` 一律计为第二恢复真源（red）。
def _registry_isolation_helper_path() -> Path:
    """返回中立恢复唯一真源的模块路径。"""
    return GROUP_ADMISSION_TESTS_DIR / "registry_isolation_support.py"


def _last_attr(node: ast.expr) -> str | None:
    """属性链最后一环属性名：``a.b.c`` → ``"c"``（顶层 Attribute 的 attr）。"""
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _scan_registry_restore_writes(paths: list[Path]) -> list[str]:
    """AST 扫描给定文件中的 registry/lifespan 恢复写操作。

    只识别**写操作**（docstring / 注释不进入 AST，不会误报；只读访问与
    hook 差分比较不在扫描范围）：

    - lifespan list 恢复：``driver._lifespan._startup_funcs[:] = X`` 的原地
      切片赋值，与 ``lifespan._startup_funcs = X`` 的属性重绑；
    - registry 清空 / 整表回填：对 ``matchers`` / ``_run_preprocessors`` /
      ``_run_postprocessors`` / ``_event_preprocessors`` /
      ``_event_postprocessors`` 调用 ``.clear()`` / ``.update(...)``。

    只按上述真实写操作判定，**不做任何函数名豁免**：不按 ``_restore_*`` /
    ``_clear_*`` 名称前缀、也不按旧函数名白名单放行。存量三处恢复路径
    （``test_dependency_boundary`` / ``entry_gate_support`` /
    ``lifecycle_support``）体内的写操作就是本票要求删除的第二恢复真源，
    收敛完成前一律计为违规；名称与受管容器无关的恢复/清理 helper（如
    ``_restore_unrelated_state`` / ``_clear_other_cache``）因不含这些写操作
    而自然放行。
    """
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.Call)):
                continue
            where = f"{path.name}:{node.lineno}"
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and isinstance(
                        target.slice, ast.Slice
                    ):
                        last = _last_attr(target.value)
                        if last in _LIFESPAN_LIST_NAMES:
                            violations.append(
                                f"{where}: lifespan 列表原地切片恢复 {last}"
                            )
                    elif isinstance(target, ast.Attribute):
                        last = _last_attr(target)
                        if last in _LIFESPAN_LIST_NAMES:
                            violations.append(
                                f"{where}: lifespan 列表属性重绑 {last}"
                            )
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in {
                    "clear",
                    "update",
                }:
                    last = _last_attr(func.value)
                    if last in _REGISTRY_CONTAINER_NAMES:
                        violations.append(
                            f"{where}: registry {last}.{func.attr}()"
                        )
    return violations


def test_registry_isolation_helper_is_the_single_restore_source() -> None:
    """TSK-253：中立 ``registry_isolation_support`` 是恢复的唯一真源。

    其他 ``tests/group_admission/`` 文件不得重新实现 registry/lifespan 恢复
    写操作（AST 只扫描真实写操作，只读访问与 hook 差分比较仍允许；不按函数
    名或前缀豁免，存量三处恢复路径收敛前一律计为第二真源）。helper 缺失时
    以「唯一真源缺失」失败；helper 就位后若 ``test_dependency_boundary`` /
    ``entry_gate_support`` / ``lifecycle_support`` 仍保留旧恢复写操作，则继续
    以「发现第二恢复真源」失败（red）。
    """
    helper = _registry_isolation_helper_path()
    assert helper.is_file(), (
        "registry_isolation_support.py 尚不存在（中立恢复唯一真源缺失，"
        "TSK-253 红基线）"
    )
    scan_paths = [
        path
        for path in sorted(GROUP_ADMISSION_TESTS_DIR.glob("*.py"))
        if path.name != helper.name
    ]
    violations = _scan_registry_restore_writes(scan_paths)
    assert violations == [], (
        "发现第二 registry/lifespan 恢复真源（必须统一走 "
        f"registry_isolation_support 的 context manager）: {violations}"
    )


def test_registry_isolation_helper_keeps_scope_narrow() -> None:
    """TSK-253：中立 helper 不接管 sys.modules / reload / scheduler / fake。

    只允许保存与恢复 registry/lifespan 容器；模块弹出、插件 reload、
    scheduler 替换与 runtime 单例注入等编排职责必须留在既有调用方
    （``entry_gate_support`` / ``lifecycle_support``）。AST 扫描避免
    docstring 误报。
    """
    helper = _registry_isolation_helper_path()
    assert helper.is_file(), (
        "registry_isolation_support.py 尚不存在（中立恢复唯一真源缺失，"
        "TSK-253 红基线）"
    )
    tree = ast.parse(helper.read_text(encoding="utf-8"), filename=str(helper))

    forbidden_import_roots = {"importlib", "nonebot_plugin_apscheduler", "pytest"}
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    offenders = sorted(imported_roots & forbidden_import_roots)
    assert offenders == [], (
        f"helper 不得导入 reload/fake 注入载体: {offenders}"
    )

    sys_modules_touched = [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "modules"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    ]
    assert sys_modules_touched == [], (
        f"helper 不得访问 sys.modules（模块接管）: {sys_modules_touched}"
    )

    monkeypatch_refs = [
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "monkeypatch"
    ]
    assert monkeypatch_refs == [], (
        f"helper 不得使用 monkeypatch（fake 注入）: {monkeypatch_refs}"
    )
