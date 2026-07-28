"""从运行时唯一 DDL 真源生成 Komari Memory 运维 SQL。"""

from __future__ import annotations

import argparse
from pathlib import Path

from komari_bot.common.vector_storage_schema import (
    build_memory_schema_statements,
    render_schema_statements,
)

_GENERATED_HEADER = """-- 本文件由 scripts/render_memory_schema.py 生成，请勿手工维护。
-- 唯一 Schema 真源：komari_bot/common/vector_storage_schema.py
-- 执行前请备份数据库，并使用 psql -v ON_ERROR_STOP=1。

"""


def build_memory_schema_sql(embedding_dimension: int) -> str:
    """生成指定向量维度的完整 Memory SQL。"""
    statements = build_memory_schema_statements(embedding_dimension)
    return _GENERATED_HEADER + render_schema_statements(statements)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--embedding-dimension",
        type=int,
        required=True,
        help="当前 embedding_provider 使用的向量维度",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="生成 SQL 的目标文件；建议使用临时路径并先审阅",
    )
    return parser


def main() -> None:
    """命令行入口。"""
    args = _build_parser().parse_args()
    output: Path = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        build_memory_schema_sql(args.embedding_dimension),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
