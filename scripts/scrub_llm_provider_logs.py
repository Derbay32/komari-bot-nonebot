"""显式净化历史 LLM JSONL 日志中的正文与 reasoning。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from komari_bot.common.llm_log_safety import (
    sanitize_log_text,
    scrub_log_directory,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("logs") / "llm_provider",
        help="LLM JSONL 日志目录",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际原地改写日志；省略时仅预览受影响文件数",
    )
    return parser.parse_args()


def _count_legacy_files(log_dir: Path) -> int:
    if not log_dir.exists():
        return 0

    affected_files = 0
    for log_file in log_dir.glob("*.jsonl"):
        original_text = log_file.read_text(encoding="utf-8")
        if sanitize_log_text(original_text) != original_text:
            affected_files += 1
    return affected_files


def main() -> None:
    args = _parse_args()
    log_dir: Path = args.log_dir
    affected_files = _count_legacy_files(log_dir)
    if not args.apply:
        print(  # noqa: T201
            f"预览完成：{affected_files} 个日志文件需要净化；未修改任何文件。"
        )
        print(  # noqa: T201
            "确认运维窗口和备份策略后，追加 --apply 执行原地净化。"
        )
        return

    scrubbed_files = scrub_log_directory(log_dir)
    print(f"净化完成：已原地改写 {scrubbed_files} 个日志文件。")  # noqa: T201


if __name__ == "__main__":
    main()
