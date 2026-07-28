-- 已废弃：此文件曾复制运行时表结构，现已停止维护且禁止执行。
-- 唯一 Schema 真源：komari_bot/common/vector_storage_schema.py
-- 如需手工预建或排障，请先运行：
-- poetry run python scripts/render_memory_schema.py \
--   --embedding-dimension <当前维度> --output /tmp/komari-memory-schema.sql
-- 审阅生成文件后再使用：
-- psql -v ON_ERROR_STOP=1 ... -f /tmp/komari-memory-schema.sql

\echo '错误：database/init_orm.sql 已废弃，未执行任何 DDL。请使用 render_memory_schema.py。'
\quit
