"""预占日志归属前缀验收测试（KOMARIBOT-14）。

预占编排已切到 komari_chat 的 proactive_reservation module，
``_attempt_reply`` 中冷却 / 限流 / 重复拒绝三条 debug 日志必须归属
``[KomariChat]``，不得残留 ``[KomariMemory]``；komari_chat 插件内
不得存在指向预占语义的 ``[KomariMemory]`` 日志前缀。
"""

from __future__ import annotations

from pathlib import Path

KOMARI_CHAT_DIR = (
    Path(__file__).resolve().parents[2] / "komari_bot" / "plugins" / "komari_chat"
)
MESSAGE_HANDLER = KOMARI_CHAT_DIR / "handlers" / "message_handler.py"

#: _attempt_reply 预占拒绝路径的三条日志正文。
RESERVATION_REJECTION_MESSAGES = (
    "主动回复冷却或生成预占中",
    "主动回复频率超限",
    "主动回复消息已预占或已送达",
)

#: 预占语义关键词（用于插件级前缀扫描）。
RESERVATION_KEYWORDS = ("预占", "冷却", "频率超限")


def test_attempt_reply_rejection_logs_use_komari_chat_prefix() -> None:
    """_attempt_reply 三条预占拒绝日志前缀全部为 [KomariChat]。"""
    source = MESSAGE_HANDLER.read_text(encoding="utf-8")
    for message_text in RESERVATION_REJECTION_MESSAGES:
        assert f"[KomariChat] {message_text}" in source, (
            f"缺少 [KomariChat] 前缀日志: {message_text}"
        )
        assert f"[KomariMemory] {message_text}" not in source, (
            f"残留 [KomariMemory] 前缀日志: {message_text}"
        )


def test_no_komari_memory_prefix_with_reservation_semantics_in_chat_plugin() -> None:
    """komari_chat 插件内不存在指向预占语义的 [KomariMemory] 日志前缀。"""
    offenders: list[str] = []
    for path in sorted(KOMARI_CHAT_DIR.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "[KomariMemory]" in line and any(
                keyword in line for keyword in RESERVATION_KEYWORDS
            ):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], f"存在预占语义的 [KomariMemory] 日志残留: {offenders}"
