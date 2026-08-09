"""群总结 rerank 失败预算：Redis 固定窗口原子计数（KOMARIBOT-24）。

预算只服务于群总结归类的「持续降级 → 升级诊断」决策，不改变聊天侧
DecisionEngine / UnifiedCandidateRerankService。

窗口语义是固定窗口：每次业务级最终 rerank 失败原子 INCR，首次失败设置
EXPIRE，后续失败绝不刷新 TTL；窗口过期后重新从 1 计数。达到阈值后保留
计数（不删除），后续失败仍返回升级结果，直到成功 rerank 清零。

预算 key 只使用提供方安全指纹（rerank endpoint + model 的 SHA-256 短摘要），
key 与日志均不包含原始 URL、API Key、query、documents 或响应正文。
"""

from __future__ import annotations

from typing import Protocol, cast

from nonebot import logger

# Lua 脚本首行保留固定版本注释；固定窗口 = 仅首次失败设置 EXPIRE，绝不滑动续期。
_RECORD_FAILURE_SCRIPT = """-- summary_rerank_failure_budget_v1
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""

# 预算 key 前缀：后接提供方安全指纹，不含任何敏感信息。
_KEY_PREFIX = "komari_decision:summary_rerank_failure_budget"


class RerankFailureBudgetUnavailableError(RuntimeError):
    """失败预算存储（Redis）不可用。

    仅在 rerank 已失败、必须核验预算时抛出；调用方应返回
    FAILURE_BUDGET_UNAVAILABLE 并禁止 fallback。
    """


class _RedisClientProtocol(Protocol):
    """最小 Redis 客户端接口，便于测试注入。"""

    async def execute_command(self, *args: object) -> object: ...


class SummaryRerankFailureBudget:
    """Redis 固定窗口 rerank 失败预算。

    客户端经最小 ``execute_command`` Protocol 注入；client 为 None 或
    Redis 异常统一包装为 :class:`RerankFailureBudgetUnavailableError`，
    不向调用方泄漏原始异常。
    """

    def __init__(self, client: _RedisClientProtocol | None) -> None:
        self._client = client

    @staticmethod
    def _key(provider_fingerprint: str) -> str:
        """构造隔离的预算 key：仅含安全指纹，绝不含原始 URL/凭据。"""
        return f"{_KEY_PREFIX}:{provider_fingerprint}"

    async def record_failure(
        self,
        provider_fingerprint: str,
        window_seconds: int,
    ) -> int:
        """原子记录一次失败并返回窗口内累计次数。

        首次失败在同一 Lua 内设置 EXPIRE，后续失败不刷新 TTL。
        """
        if self._client is None:
            msg = "失败预算 Redis 客户端未就绪"
            raise RerankFailureBudgetUnavailableError(msg)
        try:
            raw = await self._client.execute_command(
                "EVAL",
                _RECORD_FAILURE_SCRIPT,
                1,
                self._key(provider_fingerprint),
                int(window_seconds),
            )
        except Exception:
            logger.warning(
                "[KomariDecision] rerank 失败预算记录失败，按存储不可用处理"
            )
            msg = "失败预算存储不可用"
            raise RerankFailureBudgetUnavailableError(msg) from None
        try:
            return int(cast("str | int", raw))
        except (TypeError, ValueError):
            msg = "失败预算存储返回异常结果"
            raise RerankFailureBudgetUnavailableError(msg) from None

    async def clear(self, provider_fingerprint: str) -> None:
        """清零指定提供方的失败计数（成功 rerank 后调用）。"""
        if self._client is None:
            msg = "失败预算 Redis 客户端未就绪"
            raise RerankFailureBudgetUnavailableError(msg)
        try:
            await self._client.execute_command(
                "DEL",
                self._key(provider_fingerprint),
            )
        except Exception:
            logger.warning(
                "[KomariDecision] rerank 失败预算清零失败，按存储不可用处理"
            )
            msg = "失败预算存储不可用"
            raise RerankFailureBudgetUnavailableError(msg) from None


__all__ = [
    "RerankFailureBudgetUnavailableError",
    "SummaryRerankFailureBudget",
]
