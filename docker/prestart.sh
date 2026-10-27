#! /usr/bin/env sh
set -e

# v2.0.0+：在 Gunicorn 启动应用进程前执行数据库迁移：
#   1. Alembic 迁移（upgrade head）是冷启动硬门槛，失败立刻中止；
#   2. 可选 Jev 判定 Prompt 初始化失败只输出固定安全诊断，不阻止独立服务启动。
# 本脚本由 docker/start.sh 在 set -e 环境下源入（source），
# Alembic 任一步失败即以非零退出码中止，容器拒绝启动（fail fast）。
echo "Running Alembic database migration: upgrade head"
python -m komari_bot.db.orm_bootstrap upgrade head
echo "Database migration completed"

echo "Running optional decision prompt seed: seed_bootstrap"
python -m komari_bot.db.seed_bootstrap
echo "Optional decision prompt seed finished"
