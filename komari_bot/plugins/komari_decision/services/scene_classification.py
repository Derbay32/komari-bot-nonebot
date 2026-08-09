"""群总结请求场景归类实现（KOMARIBOT-23 / KOMARIBOT-24）。

深场景归类 module：统一拥有场景 runtime 刷新、embedding 召回、评分与用途策略，
对外只暴露「命中 / 未命中 / 不可用（带稳定原因码）」的窄 operation。

调用方不能传入 runtime、候选 flags、场景键、阈值或指令；目标场景键
``_SUMMARY_SCENE_KEY`` 只存在于本 implementation 内部。

rerank 供应方可降级失败（网络/超时/HTTP 408/429/5xx/响应格式错误）进入
固定窗口失败预算（KOMARIBOT-24）：达到阈值返回
RERANK_FAILURE_BUDGET_EXHAUSTED；未达阈值且配置开启 fallback 时用已有
query embedding 与真实余弦归类；401/403、其他 4xx（含 425）、缺少 URL 等
本地配置非法立即返回 RERANK_UNAVAILABLE，不消耗预算。

本模块不创建 request trace / Agent Run，也不迁移既有聊天 DecisionEngine
与 UnifiedCandidateRerankService（分别由 KOMARIBOT-26 / KOMARIBOT-27 处理）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from nonebot import logger

from komari_bot.decision import (
    DecisionRuntimeState,
    DecisionRuntimeStatus,
    SummaryRequestClassificationResult,
    SummaryRequestUnavailableReason,
)
from komari_bot.plugins.embedding_provider import (
    EmbeddingResponseValidationError,
    RemoteResponseDecodeError,
    RemoteResponseTooLargeError,
    RemoteServiceFailureKind,
    RemoteServiceRequestError,
    RerankConfigurationError,
    RerankResponseValidationError,
)

from .summary_rerank_failure_budget import (
    RerankFailureBudgetUnavailableError,
    SummaryRerankFailureBudget,
)

if TYPE_CHECKING:
    from ..config_schema import KomariDecisionConfigSchema
    from .scene_runtime_service import (
        SceneRuntimeGeneralCandidate,
        SceneRuntimeService,
        SceneRuntimeSnapshot,
    )

# 群总结专用目标场景键：只允许存在于本 implementation，不对外暴露。
_SUMMARY_SCENE_KEY = "scene_group_history_summary"

# 已声明的 embedding 预期故障：远程请求/响应异常、传输超时（不含未初始化，
# 后者通过独立的 RuntimeError 消息契约识别）
_EMBEDDING_EXPECTED_ERRORS = (
    RemoteServiceRequestError,
    RemoteResponseTooLargeError,
    RemoteResponseDecodeError,
    EmbeddingResponseValidationError,
    TimeoutError,
)

# 已声明的 rerank 预期故障：远程请求/响应异常、传输超时、本地配置非法
_RERANK_EXPECTED_ERRORS = (
    RemoteServiceRequestError,
    RemoteResponseTooLargeError,
    RemoteResponseDecodeError,
    RerankResponseValidationError,
    RerankConfigurationError,
    TimeoutError,
)


def _is_numeric_summary_request(text: str) -> bool:
    """数字快速识别：标准化文本同时包含「总结」与任意数字。

    命中即本地判定为群总结请求，不触发 runtime 刷新与 embedding/rerank。
    """
    return "总结" in text and re.search(r"\d", text) is not None


def _get_embedding_provider() -> Any:
    """惰性获取 embedding_provider，避免模块导入阶段强依赖。"""
    from komari_bot.plugins import embedding_provider

    return embedding_provider


def _get_rerank_failure_budget() -> Any:
    """惰性获取群总结 rerank 失败预算存储。

    复用项目既有顶层 seam：经 ``komari_memory.get_plugin_manager()`` 获取
    manager，取 ``manager.redis``（RedisManager）后再取其 ``.redis`` 客户端；
    manager/RedisManager 不存在时构造不可用 store，``.redis`` 未初始化的
    明确 RuntimeError 可降级。来自 get_plugin_manager() 等的未声明程序错误
    （TypeError/AssertionError）继续传播，不宽捕获。
    """
    from komari_bot.plugins import komari_memory

    manager = komari_memory.get_plugin_manager()
    if manager is None:
        return SummaryRerankFailureBudget(None)
    memory_redis = manager.redis
    if memory_redis is None:
        return SummaryRerankFailureBudget(None)
    try:
        redis_client = memory_redis.redis
    except RuntimeError:
        # RedisManager.redis 未初始化时的明确 RuntimeError：可降级为不可用 store
        redis_client = None
    return SummaryRerankFailureBudget(redis_client)


def _is_embedding_expected_error(exc: BaseException) -> bool:
    """判断是否为已声明的 embedding 预期故障（未初始化/传输超时/远程服务/响应校验）。"""
    if isinstance(exc, _EMBEDDING_EXPECTED_ERRORS):
        return True
    return isinstance(exc, RuntimeError) and "尚未初始化" in str(exc)


def _is_rerank_expected_error(exc: BaseException) -> bool:
    """判断是否为已声明的 rerank 预期故障（未初始化/传输超时/远程服务/响应校验/配置）。"""
    if isinstance(exc, _RERANK_EXPECTED_ERRORS):
        return True
    return isinstance(exc, RuntimeError) and "尚未初始化" in str(exc)


def _is_rerank_failure_budgetable(exc: BaseException) -> bool:
    """判断供应方失败是否可计入失败预算。

    只有网络中断、超时、HTTP 408/429/5xx 与供应方响应格式错误可降级；
    401/403、其他 4xx（含 425）、缺少 URL/本地配置非法与未分类异常
    一律不消耗预算。
    """
    if isinstance(
        exc,
        (
            RerankResponseValidationError,
            RemoteResponseDecodeError,
            RemoteResponseTooLargeError,
            TimeoutError,
        ),
    ):
        return True
    if not isinstance(exc, RemoteServiceRequestError):
        return False
    if exc.failure_kind is RemoteServiceFailureKind.HTTP_STATUS:
        status = exc.status
        return status in (408, 429) or (
            status is not None and 500 <= status <= 599
        )
    return exc.failure_kind in (
        RemoteServiceFailureKind.NETWORK,
        RemoteServiceFailureKind.TIMEOUT,
        RemoteServiceFailureKind.RESPONSE_INVALID,
    )


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """计算两个向量的余弦相似度。"""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = 0.0
    norm1 = 0.0
    norm2 = 0.0
    for a, b in zip(v1, v2, strict=True):
        dot += a * b
        norm1 += a * a
        norm2 += b * b
    if norm1 <= 0.0 or norm2 <= 0.0:
        return 0.0
    return dot / ((norm1**0.5) * (norm2**0.5))


def _unavailable(reason: SummaryRequestUnavailableReason) -> SummaryRequestClassificationResult:
    """构造不可用结果。"""
    return SummaryRequestClassificationResult.unavailable(reason)


def _gate_checks(
    config: KomariDecisionConfigSchema,
    runtime_state: DecisionRuntimeState,
) -> SummaryRequestClassificationResult | None:
    """执行 plugin_enable / scene_persist / runtime 三态门控。

    通过门控返回 None，否则返回对应的不可用结果。
    """
    if not config.plugin_enable:
        return _unavailable(SummaryRequestUnavailableReason.DECISION_DISABLED)
    if not config.scene_persist_enabled:
        return _unavailable(SummaryRequestUnavailableReason.DECISION_DISABLED)
    if runtime_state.status is DecisionRuntimeStatus.DISABLED:
        return _unavailable(SummaryRequestUnavailableReason.DECISION_DISABLED)
    if runtime_state.status is DecisionRuntimeStatus.FAILED:
        return _unavailable(SummaryRequestUnavailableReason.RUNTIME_UNAVAILABLE)
    return None


async def _resolve_summary_snapshot(
    scene_runtime: SceneRuntimeService | None,
) -> tuple[SummaryRequestClassificationResult | None, SceneRuntimeSnapshot | None]:
    """刷新 runtime 并解析含群总结目标场景的快照。

    返回 (错误结果, 快照)：错误结果非 None 时快照恒为 None；
    错误结果为 None 时快照即为有效快照（恒非 None）。
    """
    if scene_runtime is None:
        return _unavailable(SummaryRequestUnavailableReason.RUNTIME_UNAVAILABLE), None
    try:
        await scene_runtime.refresh_if_runtime_updated()
    except (RuntimeError, OSError):
        # runtime 刷新为运维性数据库/传输调用边界，其失败视为 runtime 不可用；
        # 其他程序错误（如 AssertionError/TypeError）与取消继续传播
        return _unavailable(SummaryRequestUnavailableReason.RUNTIME_UNAVAILABLE), None
    snapshot = scene_runtime.get_scene_candidates()
    if snapshot is None:
        return _unavailable(SummaryRequestUnavailableReason.RUNTIME_UNAVAILABLE), None
    if not any(
        candidate.scene_id == _SUMMARY_SCENE_KEY
        for candidate in snapshot.general_candidates
    ):
        return _unavailable(SummaryRequestUnavailableReason.SCENE_DATA_UNAVAILABLE), None
    return None, snapshot


async def _classify_with_rerank(
    *,
    message_text: str,
    query_vector: list[float],
    config: KomariDecisionConfigSchema,
    provider: Any,
    top_scenes: list[SceneRuntimeGeneralCandidate],
) -> SummaryRequestClassificationResult:
    """使用总结专用 rerank 指令对 top-k 场景精排归类。

    调用方已确认配置与提供者均允许 rerank；此处处理提供者失败：
    可降级失败进入固定窗口失败预算（达阈值升级，未达阈值按配置 fallback），
    本地配置非法/鉴权类 4xx 立即返回 RERANK_UNAVAILABLE 且不消耗预算。
    """
    try:
        rerank_results = await provider.rerank(
            query=message_text,
            documents=[scene.text for scene in top_scenes],
            top_n=len(top_scenes),
            instruction=config.summary_rerank_instruction,
        )
    except Exception as exc:
        # 仅白名单稳定类型（远程服务/响应校验/传输超时/未初始化/配置非法）继续处理，
        # 未声明的程序错误（如 RuntimeError/TypeError/AssertionError）与取消继续传播
        if not _is_rerank_expected_error(exc):
            raise
        if not _is_rerank_failure_budgetable(exc):
            # 401/403、其他 4xx（含 425）、缺少 URL/本地配置非法：
            # 供应方未进入降级窗口，不消耗预算
            logger.warning(
                "[KomariDecision] 群总结 rerank 非降级失败: error_type={}",
                type(exc).__name__,
            )
            return _unavailable(SummaryRequestUnavailableReason.RERANK_UNAVAILABLE)
        return await _resolve_budgeted_rerank_failure(
            config=config,
            provider=provider,
            query_vector=query_vector,
            top_scenes=top_scenes,
        )

    # rerank 成功：best-effort 清零失败预算，清零失败不影响成功结果
    await _best_effort_clear_rerank_budget(provider)

    score_by_index: dict[int, float] = {}
    for result in rerank_results:
        if 0 <= result.index < len(top_scenes):
            score_by_index[result.index] = result.relevance_score

    best_index = max(
        range(len(top_scenes)),
        key=lambda index: score_by_index.get(index, 0.0),
    )
    best_scene = top_scenes[best_index]
    best_score = score_by_index.get(best_index, 0.0)
    if (
        best_scene.scene_id == _SUMMARY_SCENE_KEY
        and best_score >= config.summary_rerank_threshold
    ):
        return SummaryRequestClassificationResult.matched()
    return SummaryRequestClassificationResult.not_matched()


async def _resolve_budgeted_rerank_failure(
    *,
    config: KomariDecisionConfigSchema,
    provider: Any,
    query_vector: list[float],
    top_scenes: list[SceneRuntimeGeneralCandidate],
) -> SummaryRequestClassificationResult:
    """已声明的可降级 rerank 失败：记录预算，达阈值升级，未达阈值才考虑 fallback。

    预算 key 按提供方安全指纹全局隔离；Redis/预算存储不可用时返回
    FAILURE_BUDGET_UNAVAILABLE 并禁止 fallback。
    """
    budget = _get_rerank_failure_budget()
    try:
        count = await budget.record_failure(
            provider.get_rerank_provider_fingerprint(),
            int(config.summary_rerank_failure_window_seconds),
        )
    except RerankFailureBudgetUnavailableError:
        logger.warning(
            "[KomariDecision] 群总结 rerank 失败预算不可用，禁止 fallback"
        )
        return _unavailable(SummaryRequestUnavailableReason.FAILURE_BUDGET_UNAVAILABLE)

    logger.warning(
        "[KomariDecision] 群总结 rerank 供应方降级失败: count={} threshold={}",
        count,
        config.summary_rerank_failure_threshold,
    )
    if count >= config.summary_rerank_failure_threshold:
        # 达到阈值后不删除计数，后续失败仍升级；error 级日志只含计数，
        # 不含 endpoint/query/正文/凭据，原因码仍是唯一的升级标记
        logger.error(
            "[KomariDecision] 群总结 rerank 供应方持续降级失败，已耗尽失败预算: "
            "count={} threshold={}",
            count,
            config.summary_rerank_failure_threshold,
        )
        return _unavailable(
            SummaryRequestUnavailableReason.RERANK_FAILURE_BUDGET_EXHAUSTED
        )
    if not config.summary_rerank_fallback_enabled:
        return _unavailable(SummaryRequestUnavailableReason.RERANK_UNAVAILABLE)
    if config.summary_similarity_threshold is None:
        return _unavailable(SummaryRequestUnavailableReason.CONFIGURATION_INCOMPLETE)
    # fallback：使用本次已有 query embedding 与真实余弦继续归类
    return _classify_with_cosine(
        query_vector=query_vector,
        similarity_threshold=config.summary_similarity_threshold,
        top_scenes=top_scenes,
    )


async def _best_effort_clear_rerank_budget(provider: Any) -> None:
    """rerank 成功后清零失败预算；清零失败不得改变成功结果。

    只忽略明确的预算存储不可用（RerankFailureBudgetUnavailableError）；
    预算实现或 provider 指纹获取中的未声明程序错误继续传播。
    """
    try:
        budget = _get_rerank_failure_budget()
        await budget.clear(provider.get_rerank_provider_fingerprint())
    except RerankFailureBudgetUnavailableError:
        logger.warning(
            "[KomariDecision] 群总结 rerank 失败预算清零失败，忽略"
        )


def _classify_with_cosine(
    *,
    query_vector: list[float],
    similarity_threshold: float | None,
    top_scenes: list[SceneRuntimeGeneralCandidate],
) -> SummaryRequestClassificationResult:
    """使用真实余弦相似度与总结相似度阈值归类。"""
    if similarity_threshold is None:
        # 防御分支：入口处的配置完整性检查已拦截，正常流程不可达
        return _unavailable(SummaryRequestUnavailableReason.CONFIGURATION_INCOMPLETE)

    best_scene: SceneRuntimeGeneralCandidate | None = None
    best_score = 0.0
    for scene in top_scenes:
        score = _cosine_similarity(query_vector, scene.embedding)
        if best_scene is None or score > best_score:
            best_scene = scene
            best_score = score
    if best_scene is None:
        return SummaryRequestClassificationResult.not_matched()
    if best_scene.scene_id == _SUMMARY_SCENE_KEY and best_score >= similarity_threshold:
        return SummaryRequestClassificationResult.matched()
    return SummaryRequestClassificationResult.not_matched()


async def _classify_embedded(
    *,
    message_text: str,
    config: KomariDecisionConfigSchema,
    provider: Any,
    snapshot: SceneRuntimeSnapshot,
    query_vector: list[float],
    rerank_mode: bool,
    similarity_threshold: float | None,
) -> SummaryRequestClassificationResult:
    """对已嵌入的 query 执行 top-k 召回与 rerank/余弦模式化评分。

    rerank_mode 为「配置允许且提供者实际开启」的有效模式；
    提供者关闭时退化为真实余弦模式，绝不接收伪造的位置分数。
    """
    # 只从 general_candidates 召回，绝不把 NOISE/MEANINGFUL/CALL_* 加入候选
    top_k = max(1, config.summary_scene_top_k)
    scored = sorted(
        (
            (_cosine_similarity(query_vector, candidate.embedding), candidate)
            for candidate in snapshot.general_candidates
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    top_scenes = [candidate for _, candidate in scored[:top_k]]

    if rerank_mode:
        return await _classify_with_rerank(
            message_text=message_text,
            query_vector=query_vector,
            config=config,
            provider=provider,
            top_scenes=top_scenes,
        )
    return _classify_with_cosine(
        query_vector=query_vector,
        similarity_threshold=similarity_threshold,
        top_scenes=top_scenes,
    )


def _resolve_classification_mode(
    provider: Any,
    config: KomariDecisionConfigSchema,
) -> tuple[SummaryRequestClassificationResult | None, bool, float | None]:
    """解析有效 rerank 模式与余弦阈值。

    返回 (错误结果, 有效 rerank 模式, 相似度阈值)：embedding 未就绪或
    余弦模式缺阈值时错误结果非 None；通过时错误结果为 None。
    """
    if not provider.is_embedding_ready():
        # embedding service 未就绪：在余弦配置完整性判断前拦截，且不调用 embed
        return _unavailable(SummaryRequestUnavailableReason.EMBEDDING_UNAVAILABLE), False, None
    # 有效 rerank 模式 = 配置允许且提供者实际开启；提供者关闭时走真实余弦模式
    effective_rerank_mode = config.summary_rerank_enabled and bool(
        provider.is_rerank_enabled()
    )
    similarity_threshold = config.summary_similarity_threshold
    if not effective_rerank_mode and similarity_threshold is None:
        # 余弦模式需要配置相似度阈值；缺失时不进入 embedding 流程
        return _unavailable(SummaryRequestUnavailableReason.CONFIGURATION_INCOMPLETE), False, None
    return None, effective_rerank_mode, similarity_threshold


async def classify_summary_request(
    *,
    message_text: str,
    config: KomariDecisionConfigSchema,
    runtime_state: DecisionRuntimeState,
    scene_runtime: SceneRuntimeService | None,
) -> SummaryRequestClassificationResult:
    """执行群总结请求场景归类（配置与运行时已由调用方冻结解析）。

    流程：三态门控 → 数字快速识别 → 模式/配置解析 → runtime 刷新/快照/目标场景
    → query embedding → top-k 召回 → rerank 或显式余弦模式评分。
    """
    gated = _gate_checks(config, runtime_state)
    if gated is not None:
        return gated

    if _is_numeric_summary_request(message_text):
        return SummaryRequestClassificationResult.matched()

    provider = _get_embedding_provider()
    mode_error, effective_rerank_mode, similarity_threshold = (
        _resolve_classification_mode(provider, config)
    )
    if mode_error is not None:
        return mode_error

    resolve_error, snapshot = await _resolve_summary_snapshot(scene_runtime)
    if resolve_error is not None:
        return resolve_error
    assert snapshot is not None  # _resolve_summary_snapshot 构造不变量

    try:
        query_vector = await provider.embed(
            message_text,
            instruction=config.summary_embedding_instruction_query,
        )
    except Exception as exc:
        # 仅白名单稳定类型（远程服务/响应校验/传输超时/未初始化）映射为不可用，
        # 未声明 RuntimeError、TypeError、AssertionError 与取消继续传播
        if _is_embedding_expected_error(exc):
            return _unavailable(SummaryRequestUnavailableReason.EMBEDDING_UNAVAILABLE)
        raise

    return await _classify_embedded(
        message_text=message_text,
        config=config,
        provider=provider,
        snapshot=snapshot,
        query_vector=query_vector,
        rerank_mode=effective_rerank_mode,
        similarity_threshold=similarity_threshold,
    )


__all__ = ["classify_summary_request"]
