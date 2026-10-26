"""debug 安全结果报告格式化与私密投递。

TSK-200：Agent Run 采集器退役后，报告只是请求 ID、结果类型、成功/失败、
安全固定错误码与最终结果的纯渲染：

- 不渲染逐轮 LLM 调用、工具参数/结果或 token 用量聚合
- 不持有或读取采集历史；调用方只传显式 ``request_id``
- 最终结果只渲染固定的安全字段白名单
- 单节点按行切分并限制体积，每批不超过 50 个节点
- 合并转发失败时按同章节普通文本逐条发送
- 完整报告只投递到发起 SUPERUSER 的私聊
- 显式公开时仅发送二次脱敏的结构化摘要
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageSegment

from komari_bot.onebot.onebot_messages import plain_text_message

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_NODE_TEXT_LENGTH = 3500
MAX_NODES_PER_BATCH = 50
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: 最终结果允许渲染的固定安全字段（键 → 报告标签）。
#: 只包含业务结果与聚合计数/标签；不含用户标识、输入正文、异常正文或
#: 工具参数。未列出的键一律丢弃。
_SAFE_FINAL_RESULT_LABELS: dict[str, str] = {
    "reply_text": "回复正文",
    "favorability_delta": "拟议好感度",
    "reply_to_message_id": "引用消息 ID",
    "filtered_message_count": "筛选消息数",
    "filter_label": "筛选方式",
    "time_range": "时间范围",
    "image_page_count": "图片页数",
    "image_truncated": "图片是否截断",
    "history_status": "历史状态",
    "history_coverage_ratio": "历史覆盖率",
    "history_completed_batches": "历史完成批次",
    "history_failed_batch": "历史失败批次",
    "image_failure_mode": "图片失败模式",
    "image_failure_stages": "图片失败阶段",
    "image_failed_count": "图片失败数量",
}


def _group_business_admitted(group_id: int | None) -> bool:
    """对群公开诊断结果执行一次 BUSINESS 裁决（TSK-229 / 摘要.debug_public）。

    同步、无 I/O；只认 ``business`` 资格。缺值视为不可靠归属，故障关闭拒绝。
    调用面读取顶层 ``adjudicate``（包级运行时属性解析，可被测试替身替换拦截），
    只在被调用时惰性 import，避免测试 / 插件装载期触发 ``require``。
    """
    if group_id is None:
        return False
    import komari_bot.plugins.group_admission as admission_pkg

    result = admission_pkg.adjudicate((int(group_id),))
    return result.qualification.value == "business"


@dataclass(frozen=True, slots=True)
class DiagnosticDeliveryResult:
    """完整私聊报告与可选公开脱敏报告的投递结果。"""

    private_delivered: bool
    public_delivered: bool | None


def _truncate_text(content: str, max_chars: int = 500) -> str:
    """截断安全字段文本；回复正文由调用方决定是否原样渲染。"""
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "..."


def _safe_error_code(value: str | None) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if _ERROR_CODE_PATTERN.fullmatch(candidate) else "internal_error"


def _render_final_result_fields(
    final_result_info: Mapping[str, Any] | None,
) -> list[str]:
    """按固定安全白名单渲染最终结果字段。"""
    if not final_result_info:
        return []
    lines: list[str] = []
    for key, value in final_result_info.items():
        label = _SAFE_FINAL_RESULT_LABELS.get(key)
        if label is None:
            continue
        if key == "reply_text":
            lines.append(f"{label}:\n{value}")
        elif key == "favorability_delta":
            lines.append(f"{label}: {value}")
        else:
            lines.append(f"{label}: {_truncate_text(str(value), max_chars=300)}")
    return lines


def _split_into_nodes(text: str) -> list[str]:
    """将长文本拆分为合并转发节点的内容列表（按行切分，限制单节点体积）。"""
    nodes: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        line_len = len(line)

        # 如果当前行本身超过限制，先与已有缓存合并，再单独切分超长行
        if line_len > MAX_NODE_TEXT_LENGTH:
            if current_lines:
                nodes.append("".join(current_lines))
                current_lines = []
                current_len = 0
            # 将超长行切分为多个块
            nodes.extend(
                line[offset : offset + MAX_NODE_TEXT_LENGTH]
                for offset in range(0, line_len, MAX_NODE_TEXT_LENGTH)
            )
            continue

        if current_len + line_len > MAX_NODE_TEXT_LENGTH and current_lines:
            nodes.append("".join(current_lines))
            current_lines = []
            current_len = 0
        current_lines.append(line)
        current_len += line_len

    if current_lines:
        content = "".join(current_lines)
        if current_len > MAX_NODE_TEXT_LENGTH:
            content = content[:MAX_NODE_TEXT_LENGTH]
        nodes.append(content)

    if not nodes:
        nodes.append("(无诊断信息)")

    return nodes


def _build_chapters(
    request_id: str,
    result_type: str,
    *,
    succeeded: bool,
    error: str | None,
    final_result_info: Mapping[str, Any] | None = None,
    public_redacted: bool = False,
) -> list[tuple[str, str]]:
    """构建报告章节列表，每项为 (章节标题, 章节正文)。

    TSK-200：只有「请求总览」与「最终结果」两章。

    request_id: 命令显式持有并传入的任务请求 ID
    result_type: "reply" 或 "summary"
    error: 安全固定错误码；非安全码形状归一化为 internal_error
    final_result_info: 只允许安全字段白名单中的键；回复正文不截断
    public_redacted: 群公开二次脱敏（隐藏输入、输出、用户标识与异常正文）
    """
    overview_lines = [
        f"请求 ID: {request_id}",
        f"类型: {result_type}",
        f"状态: {'成功' if succeeded else '失败'}",
    ]

    if public_redacted:
        final_lines = [
            f"最终结果: {'已成功完成' if succeeded else '执行失败'}",
            "输入、输出、用户标识与异常正文已隐藏",
        ]
        if not succeeded:
            final_lines.append(f"错误码: {_safe_error_code(error)}")
    elif succeeded:
        final_lines = ["最终结果: 已成功完成"]
        final_lines.extend(_render_final_result_fields(final_result_info))
    else:
        final_lines = [
            "最终结果: 执行失败",
            f"错误码: {_safe_error_code(error)}",
        ]

    return [
        ("请求总览", "\n".join(overview_lines)),
        ("最终结果", "\n".join(final_lines)),
    ]


async def build_and_send_diagnostic_report(
    *,
    bot: Bot,
    user_id: int,
    request_id: str,
    result_type: str,
    succeeded: bool,
    error: str | None = None,
    final_result_info: Mapping[str, Any] | None = None,
    public_group_id: int | None = None,
) -> DiagnosticDeliveryResult:
    """私聊完整报告，并可向群内额外投递二次脱敏报告。

    Args:
        bot: OneBot Bot 实例
        user_id: 已通过鉴权的 SUPERUSER ID
        request_id: 命令显式持有的任务请求 ID
        result_type: "reply" 或 "summary"
        succeeded: 是否成功
        error: 安全固定错误码（失败时）
        final_result_info: 最终结果安全字段（回复正文、好感度变化等）
        public_group_id: 显式请求公开脱敏报告时的目标群 ID
    """
    private_chapters = _build_chapters(
        request_id,
        result_type,
        succeeded=succeeded,
        error=error,
        final_result_info=final_result_info,
    )
    private_delivered = await _send_report_nodes(
        bot,
        _chapters_to_nodes(private_chapters),
        forward_api="send_private_forward_msg",
        text_api="send_private_msg",
        target_key="user_id",
        target_id=user_id,
        channel="private",
    )

    public_delivered: bool | None = None
    if public_group_id is not None:
        # TSK-229：群公开结果为不可分瞬时效果，投递前按目标群 BUSINESS 裁决；
        # 受限则正常丢弃该群公开结果，不发送、不通知。完整私聊诊断
        # （SUPERUSER 运维类别）不受群限制，已在上面投递完毕。
        if not _group_business_admitted(public_group_id):
            return DiagnosticDeliveryResult(
                private_delivered=private_delivered,
                public_delivered=None,
            )
        public_chapters = _build_chapters(
            request_id,
            result_type,
            succeeded=succeeded,
            error=error,
            final_result_info=final_result_info,
            public_redacted=True,
        )
        public_delivered = await _send_report_nodes(
            bot,
            _chapters_to_nodes(public_chapters),
            forward_api="send_group_forward_msg",
            text_api="send_group_msg",
            target_key="group_id",
            target_id=public_group_id,
            channel="public_redacted",
        )

    return DiagnosticDeliveryResult(
        private_delivered=private_delivered,
        public_delivered=public_delivered,
    )


def _chapters_to_nodes(chapters: list[tuple[str, str]]) -> list[str]:
    """把报告章节转换为受大小限制的合并转发节点。"""
    nodes: list[str] = []
    for title, body in chapters:
        chapter_text = f"【{title}】\n{body}"
        nodes.extend(_split_into_nodes(chapter_text))
    return nodes


async def _send_report_nodes(
    bot: Bot,
    nodes: list[str],
    *,
    forward_api: str,
    text_api: str,
    target_key: str,
    target_id: int,
    channel: str,
) -> bool:
    """发送报告节点；合并转发失败时仅向同一目标降级为普通消息。"""
    delivered = True
    for batch_idx in range(0, len(nodes), MAX_NODES_PER_BATCH):
        batch = nodes[batch_idx : batch_idx + MAX_NODES_PER_BATCH]
        forward_nodes = [
            MessageSegment.node_custom(
                user_id=int(bot.self_id),
                nickname="Komari Debug",
                content=plain_text_message(node_text),
            )
            for node_text in batch
        ]

        try:
            await bot.call_api(
                forward_api,
                **{target_key: target_id},
                messages=forward_nodes,
            )
            logger.info(
                "[KomariDebug] 诊断报告发送成功: channel={} batch={}/{} nodes={}",
                channel,
                batch_idx // MAX_NODES_PER_BATCH + 1,
                (len(nodes) - 1) // MAX_NODES_PER_BATCH + 1,
                len(batch),
            )
        except Exception as exc:
            logger.warning(
                "[KomariDebug] 合并转发失败，降级为同目标普通消息: "
                "channel={} batch={} error_type={}",
                channel,
                batch_idx // MAX_NODES_PER_BATCH + 1,
                type(exc).__name__,
            )
            batch_delivered = await _send_text_fallback(
                bot,
                batch,
                text_api=text_api,
                target_key=target_key,
                target_id=target_id,
                channel=channel,
            )
            delivered = delivered and batch_delivered
    return delivered


async def _send_text_fallback(
    bot: Bot,
    batch_nodes: list[str],
    *,
    text_api: str,
    target_key: str,
    target_id: int,
    channel: str,
) -> bool:
    """合并转发失败时，将节点逐条发回同一个私聊或群聊目标。"""
    delivered = True
    for node_text in batch_nodes:
        node_delivered = await _send_message(
            bot,
            api=text_api,
            target_key=target_key,
            target_id=target_id,
            message=node_text,
            channel=channel,
        )
        delivered = delivered and node_delivered
    return delivered


async def _send_message(
    bot: Bot,
    *,
    api: str,
    target_key: str,
    target_id: int,
    message: Any,
    channel: str,
) -> bool:
    """向指定目标发送消息，日志不记录目标 ID 或消息正文。"""
    safe_message = plain_text_message(message) if isinstance(message, str) else message
    try:
        await bot.call_api(api, **{target_key: target_id}, message=safe_message)
    except Exception as exc:
        logger.warning(
            "[KomariDebug] 消息投递失败: channel={} error_type={}",
            channel,
            type(exc).__name__,
        )
        return False
    return True


async def send_private_message(bot: Bot, user_id: int, message: Any) -> bool:
    """向已鉴权的 SUPERUSER 私聊发送文本或消息段。"""
    return await _send_message(
        bot,
        api="send_private_msg",
        target_key="user_id",
        target_id=user_id,
        message=message,
        channel="private",
    )


async def send_group_text(bot: Bot, group_id: int, text: str) -> bool:
    """发送不含诊断正文的群消息文本。"""
    return await _send_message(
        bot,
        api="send_group_msg",
        target_key="group_id",
        target_id=group_id,
        message=plain_text_message(text),
        channel="group_receipt",
    )
