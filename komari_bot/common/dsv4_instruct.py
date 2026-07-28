"""DeepSeek V4 角色扮演专用指令注入工具。"""

from __future__ import annotations

from typing import Any, Literal, cast

DSV4_INSTRUCT_DISABLED = "disabled"
DSV4_INSTRUCT_AUTO = "auto"
DSV4_INSTRUCT_INNER_OS = "inner_os"
DSV4_INSTRUCT_NO_INNER_OS = "no_inner_os"

Dsv4InstructMode = Literal["disabled", "auto", "inner_os", "no_inner_os"]

INNER_OS_MARKER = (
    "\n\n【角色沉浸要求】在你的思考过程（<think>标签内）中，请遵守以下规则：\n"
    '1. 请以角色第一人称进行内心独白，用括号包裹内心活动，例如"（心想：……）"或"(内心OS：……)"\n'
    '2. 用第一人称描写角色的内心感受，例如"我心想""我觉得""我暗自"等\n'
    "3. 思考内容应沉浸在角色中，通过内心独白分析剧情和规划回复"
)

NO_INNER_OS_MARKER = (
    "\n\n【思维模式要求】在你的思考过程（<think>标签内）中，请遵守以下规则：\n"
    '1. 禁止使用圆括号包裹内心独白，例如"（心想：……）"或"(内心OS：……)"，所有分析内容直接陈述即可\n'
    '2. 禁止以角色第一人称描写内心活动，例如"我心想""我觉得""我暗自"等，请用分析性语言替代\n'
    "3. 思考内容应聚焦于剧情走向分析和回复内容规划，不要在思考中进行角色扮演式的内心戏表演"
)

_VALID_MODES = {
    DSV4_INSTRUCT_DISABLED,
    DSV4_INSTRUCT_AUTO,
    DSV4_INSTRUCT_INNER_OS,
    DSV4_INSTRUCT_NO_INNER_OS,
}


def normalize_dsv4_instruct_mode(value: object) -> Dsv4InstructMode:
    """规范化 DSV4 指令注入模式。"""
    mode = str(value or DSV4_INSTRUCT_AUTO).strip().lower()
    if mode not in _VALID_MODES:
        return DSV4_INSTRUCT_AUTO
    return cast("Dsv4InstructMode", mode)


def should_apply_dsv4_instruct(*, model: str, mode: str) -> bool:
    """判断当前请求是否应注入 DSV4 专用指令。"""
    normalized_mode = normalize_dsv4_instruct_mode(mode)
    if normalized_mode == DSV4_INSTRUCT_DISABLED:
        return False
    if normalized_mode == DSV4_INSTRUCT_AUTO:
        return "deepseek-v4" in model.lower()
    return True


def resolve_dsv4_instruct_marker(*, mode: str) -> str | None:
    """解析要注入的 DSV4 指令内容。"""
    normalized_mode = normalize_dsv4_instruct_mode(mode)
    if normalized_mode in {DSV4_INSTRUCT_AUTO, DSV4_INSTRUCT_INNER_OS}:
        return INNER_OS_MARKER
    if normalized_mode == DSV4_INSTRUCT_NO_INNER_OS:
        return NO_INNER_OS_MARKER
    return None


def append_marker_to_message_content(content: Any, marker: str) -> Any:
    """将指令追加到一条 OpenAI message 的 content 末尾。"""
    if isinstance(content, str):
        return f"{content}{marker}"

    if not isinstance(content, list):
        return content

    updated_content = list(content)
    for index in range(len(updated_content) - 1, -1, -1):
        part = updated_content[index]
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        updated_part = dict(part)
        updated_part["text"] = f"{updated_part.get('text', '')}{marker}"
        updated_content[index] = updated_part
        return updated_content

    updated_content.append({"type": "text", "text": marker.lstrip()})
    return updated_content


def inject_dsv4_instruct_to_first_user_message(
    messages: list[dict[str, Any]],
    *,
    model: str,
    mode: str,
) -> list[dict[str, Any]]:
    """在第一条 user 消息末尾注入 DSV4 角色扮演指令。"""
    if not should_apply_dsv4_instruct(model=model, mode=mode):
        return messages

    marker = resolve_dsv4_instruct_marker(mode=mode)
    if marker is None:
        return messages

    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        updated_message = dict(message)
        updated_message["content"] = append_marker_to_message_content(
            updated_message.get("content"), marker
        )
        messages[index] = updated_message
        break
    return messages
