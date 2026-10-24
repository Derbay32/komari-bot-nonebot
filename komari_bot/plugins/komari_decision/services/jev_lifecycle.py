"""生产 driver 生命周期与判定调度装配（TSK-182 standby）。

本模块在 ``komari_decision`` 包导入时按 Driver 就绪情况条件装配；它注册在既有
legacy 生命周期钩子**之后**，因此 NoneBot 的反向 shutdown 会先停 Jev，再轮到
共享配置/Prompt 存储关闭：

- 一个 ``driver.on_startup`` 钩子：执行**一次**判定运行时初始化尝试，然后**无论
  初始化是否成功**都安装两个周期 job，使配置/Prompt 修正在不重启进程的情况下被
  既有框架刷新机制发现；
- 一个 ``driver.on_shutdown`` 钩子：先注销周期 job（阻止新工作），再等待自有
  刷新/清理任务、关闭引擎并释放自有资源；
- 两个固定 id 的 APScheduler interval job（``max_instances=1``、
  ``coalesce=True``）：

  - 每 1 秒：只刷新**本地**配置/Prompt/存储前置条件（不探测模型）；
  - 每 300 秒：一次有界日志清理（``limit=1000``），判定关闭时也照常运行。

装配契约：

- 重复 startup 幂等（运行时自身的 ``startup`` 单飞，job 用固定 id +
  ``replace_existing``）；不会产生重复 job/引擎/租约；
- 调度器注册失败只做安全上报（固定文本 + 异常类型），绝不伪装成 ready，也绝不
  让角色服务启动多出一个全局硬依赖；
- 日志只含安全静态文本与异常类型，绝不含异常正文、凭据或平台标识；
- 本模块不调用、不挂载 ``register_decision_api``（管理 Router 由
  ``komari_management`` 装配且只装配一次）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apscheduler.jobstores.base import JobLookupError
from nonebot import get_driver, logger
from nonebot_plugin_apscheduler import scheduler

from .jev_runtime import (
    PRUNE_INTERVAL_SECONDS,
    PRUNE_JOB_ID,
    REFRESH_INTERVAL_SECONDS,
    REFRESH_JOB_ID,
    get_decision_runtime,
)

if TYPE_CHECKING:
    from nonebot.internal.driver import Driver

    from .jev_runtime import DecisionRuntime


def _runtime() -> DecisionRuntime:
    """调用期解析运行时单例（测试可注入同构实例而不触碰生产 seam）。"""
    return get_decision_runtime()


async def _refresh_job() -> None:
    """每 1 秒：只刷新本地配置/Prompt/存储前置条件（不发模型请求）。"""
    await _runtime().refresh()


async def _prune_job() -> None:
    """每 300 秒：一次有界日志清理（与判定开关无关）。"""
    await _runtime().prune()


def _install_jobs() -> None:
    """安装两个固定 id 的周期 job（重复调用幂等）。"""
    scheduler.add_job(
        _refresh_job,
        "interval",
        seconds=REFRESH_INTERVAL_SECONDS,
        id=REFRESH_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _prune_job,
        "interval",
        seconds=PRUNE_INTERVAL_SECONDS,
        id=PRUNE_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "[KomariDecision] Jev 判定前置条件刷新周期任务已注册（每 {} 秒）",
        REFRESH_INTERVAL_SECONDS,
    )
    logger.info(
        "[KomariDecision] Jev 判定日志清理周期任务已注册（每 {} 秒）",
        PRUNE_INTERVAL_SECONDS,
    )


def _remove_jobs() -> None:
    """注销两个周期 job；不存在时容忍 ``JobLookupError``（重复关闭幂等）。"""
    for job_id in (REFRESH_JOB_ID, PRUNE_JOB_ID):
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            logger.debug("[KomariDecision] Jev 判定周期任务不存在，无需注销")
        except Exception as exc:
            logger.opt(exception=False).error(
                "[KomariDecision] Jev 判定周期任务注销失败: error={}",
                type(exc).__name__,
            )


async def _startup() -> None:
    """生产 startup：一次初始化尝试，随后安装 job（失败也安装）。"""
    runtime = _runtime()
    try:
        await runtime.startup()
    except Exception as exc:
        # 运行时内部已把可预期失败投影为 failed；这里只兜底未预期异常，
        # 绝不中断后续角色服务 startup 钩子。
        logger.opt(exception=False).error(
            "[KomariDecision] Jev 判定运行时启动失败，判定进入 failed 状态: error={}",
            type(exc).__name__,
        )
    try:
        _install_jobs()
    except Exception as exc:
        # 注册失败：清理可能已部分安装的 job，并把插件级生命周期失败投影到健康
        # （普通刷新不会清除它，只有真正注册成功才清除）。
        _remove_jobs()
        runtime.report_lifecycle_failure("internal_error")
        logger.opt(exception=False).error(
            "[KomariDecision] Jev 判定周期任务注册失败，判定保持被动失败状态: error={}",
            type(exc).__name__,
        )
    else:
        runtime.clear_lifecycle_failure()


async def _shutdown() -> None:
    """生产 shutdown：先停新工作，再等自有任务收尾并关闭自有资源。"""
    _remove_jobs()
    try:
        await _runtime().close()
    except Exception as exc:
        logger.opt(exception=False).error(
            "[KomariDecision] Jev 判定运行时关闭失败: error={}", type(exc).__name__
        )


def _install_lifecycle(driver: Driver) -> None:
    """Driver 就绪时恰好装配一次生产生命周期（私有，无公开 API）。"""
    driver.on_startup(_startup)
    driver.on_shutdown(_shutdown)
    logger.info("[KomariDecision] Jev 判定生产生命周期已装配（standby，不接收流量）")


try:
    driver = get_driver()
except ValueError:
    # 测试 / 工具在 nonebot.init() 前直接 import 本插件：Driver 尚未初始化，
    # 跳过生命周期装配（业务调用面与管理 Router 装配不受影响）。受支持的生产
    # 启动顺序（docker/bot.py 先 init 再加载插件）保证插件加载期 Driver 已就绪。
    driver = None

if driver is not None:
    _install_lifecycle(driver)
