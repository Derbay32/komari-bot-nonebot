#! /usr/bin/env sh
set -e

# v2.0.0：在 Gunicorn 启动应用进程前执行 Alembic 迁移（upgrade head）。
# 本脚本由 docker/start.sh 在 set -e 环境下源入（source），
# 迁移命令失败即以非零退出码中止，容器拒绝启动（fail fast）。
echo "Running Alembic database migration: upgrade head"
python -m komari_bot.db.orm_bootstrap upgrade head
echo "Database migration completed"
