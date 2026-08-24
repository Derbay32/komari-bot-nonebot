"""生产 driver 生命周期与可观测性调度装配（TSK-248）。

本模块在 ``group_admission`` 包导入时按 Driver 就绪情况条件装配：

- Driver 已初始化（受支持生产路径：``docker/bot.py`` 严格先
  ``nonebot.init()`` 再 ``load_from_toml()`` 加载插件，插件加载期 Driver
  必已就绪）时，恰好注册：

  - 一个 ``driver.on_startup`` 钩子：经 ``komari_bot.plugins.config_manager``
    顶层唯一注册表 ``get_config_manager`` 恰一次获取 manager 并启动运行时；
    manager 获取 / 初始化异常安全收敛 ``failed``，不向 driver 冒泡；
  - 一个 ``driver.on_shutdown`` 钩子：恰一次注销周期 job（容忍
    ``JobLookupError``）并 ``close`` 运行时（幂等，listener 由
    ``runtime.close`` 清理）；
  - 一个 interval 周期 job：只调用当前 ``runtime._runtime`` 的
    ``process_observability``，覆盖 5 分钟归属窗口、30 分钟提醒、READY 60 秒
    稳定恢复与 pending SUPERUSER 通知重试，不新增第二状态 / 持久面。

- Driver 未初始化（``get_driver()`` 抛 ``ValueError``）时跳过全部 lifecycle
  装配：这是测试 / 工具在 ``nonebot.init()`` 前直接 import 本包的兼容场景，
  只跳过生命周期，业务调用面与管理 Router 装配不受影响。受支持的生产启动
  顺序保证插件加载期 Driver 已就绪，不会走该分支；本模块**不**承诺同进程
  pre-init import 后再自动补装（首次导入决定是否装配，模块被 ``sys.modules``
  缓存，不会自动重新执行，也不会在 Driver 初始化后补挂钩子 / job）。

装配契约：``group_admission`` 是 NoneBot 插件，包入口已在 import 任何依赖
子模块前 ``require`` 声明硬依赖 ``config_manager`` 与
``nonebot_plugin_apscheduler``；本模块只在 Driver 就绪时装配 lifecycle。

- manager 只经 config_manager **顶层** ``get_config_manager`` 唯一注册表取
  得（禁止直接构造 ``ConfigManager``，禁止 deep import ``.manager``）；
- ``runtime._runtime`` 与 config_manager 顶层 getter 均在调用期解析，测试可
  注入真实运行时 singleton 与 registry fake 而不触碰生产 seam；
- 日志只记录安全静态文本与异常类型，绝不携带异常正文 / 动态标识 / 策略内
  容；本模块不调用、不挂载 ``register_group_admission_api``（管理 Router 由
  ``komari_management`` 装配且只装配一次）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apscheduler.jobstores.base import JobLookupError
from nonebot import get_driver, logger
from nonebot_plugin_apscheduler import scheduler

from . import runtime as _runtime_module
from .config_schema import GroupAdmissionConfigSchema

if TYPE_CHECKING:
    from nonebot.internal.driver import Driver

    from komari_bot.plugins.config_manager import ConfigManager

#: 可观测性周期 job 的固定 id（shutdown 按此恰一次注销）。
OBSERVABILITY_JOB_ID = "group_admission_observability"

#: 周期（秒）：≤60 秒保证 READY 稳定 60 秒恢复窗能被及时观测（最迟
#: 60+60 秒检出）；``process_observability`` 自身幂等、无 sleep。
OBSERVABILITY_INTERVAL_SECONDS = 60


@dataclass(slots=True)
class _LifecycleState:
    """生产生命周期装配的单飞状态（模块私有，PluginState 模式）。

    ``acquired_manager`` 非 ``None`` 表示 startup 已经顶层唯一 getter 获取
    manager（重复 startup 不再获取 / 初始化）；持可变属性避免 ``global``。
    """

    acquired_manager: ConfigManager | None = None


_state = _LifecycleState()


async def _run_observability() -> None:
    """周期 job 唯一动作：驱动当前运行时的可观测性结算。

    ``runtime._runtime`` 在调用期解析，测试注入的 singleton 与生产单例
    同构；``close`` 后 ``process_observability`` 为 no-op。
    """
    await _runtime_module._runtime.process_observability()


def _unregister_observability_job() -> None:
    """注销周期 job；不存在时容忍 ``JobLookupError``（重复 shutdown 幂等）。"""
    try:
        scheduler.remove_job(OBSERVABILITY_JOB_ID)
    except JobLookupError:
        logger.debug("群聊准入可观测性周期任务不存在，无需注销")
    except Exception:
        logger.opt(exception=False).error(
            "群聊准入可观测性周期任务注销失败"
        )


async def _start_runtime() -> None:
    """恰一次获取 manager 并启动运行时；失败收敛 failed 不冒泡。

    - 经 config_manager 顶层唯一注册表 ``get_config_manager`` 获取，获取
      恰一次（重复 startup 不再获取 / 初始化）；
    - manager 获取 / 存储初始化异常只收敛 ``failed``，绝不向 driver 冒泡；
    - 日志只记安全静态文本与异常类型，不含异常正文。
    """
    if _state.acquired_manager is None:
        try:
            from komari_bot.plugins.config_manager import (
                get_config_manager,
            )

            _state.acquired_manager = get_config_manager(
                "group_admission", GroupAdmissionConfigSchema
            )
        except Exception as exc:
            logger.opt(exception=False).error(
                "群聊准入 manager 获取失败，运行时故障关闭（异常类型 {}）",
                type(exc).__name__,
            )
            return
    try:
        await _runtime_module._runtime.start(_state.acquired_manager)
    except Exception as exc:
        logger.opt(exception=False).error(
            "群聊准入运行时启动失败，运行时故障关闭（异常类型 {}）",
            type(exc).__name__,
        )


async def _startup() -> None:
    """生产 startup：启动运行时（单飞，重复调用不重复获取 / 初始化）。"""
    await _start_runtime()


async def _shutdown() -> None:
    """生产 shutdown：恰一次停周期任务并 close；重复调用幂等不抛。"""
    _unregister_observability_job()
    await _runtime_module._runtime.close()


def _install_lifecycle(driver: Driver) -> None:
    """Driver 就绪时恰好装配一次生产生命周期（私有，无公开 API）。

    - 注册一个 interval 周期 job（``replace_existing=True`` 幂等）与一个
      startup / shutdown driver lifespan 钩子；
    - 只由下方 ``driver is not None`` 分支调用，正常生产路径恰执行一次；
    - 不挂载管理 Router：``register_group_admission_api`` 归
      ``komari_management`` 装配且只装配一次。
    """
    scheduler.add_job(
        _run_observability,
        "interval",
        seconds=OBSERVABILITY_INTERVAL_SECONDS,
        id=OBSERVABILITY_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "群聊准入可观测性周期任务已注册（每 {} 秒）",
        OBSERVABILITY_INTERVAL_SECONDS,
    )
    driver.on_startup(_startup)
    driver.on_shutdown(_shutdown)


try:
    driver = get_driver()
except ValueError:
    # 测试 / 工具在 nonebot.init() 前直接 import 本包：Driver 尚未初始化，
    # 跳过全部 lifecycle 装配（只跳过生命周期，业务调用面与管理 Router 装
    # 配不受影响）。受支持生产启动顺序（docker/bot.py 先 init 再加载插件）
    # 保证插件加载期 Driver 已就绪，不会走该分支；同进程 pre-init import
    # 后不自动补装。
    driver = None

if driver is not None:
    # 受支持生产路径：Driver 已就绪，恰注册一个 startup / shutdown 钩子与
    # 一个 interval 可观测性 job（见 _install_lifecycle）。
    _install_lifecycle(driver)
