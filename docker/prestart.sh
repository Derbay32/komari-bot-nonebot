#! /usr/bin/env sh
set -e

# v2.0.0+：在 Gunicorn 启动应用进程前按固定顺序执行初始化：
#   1. Alembic 迁移（upgrade head）；
#   2. 版本化初始数据播种/校验（seed_bootstrap）。
# 本脚本由 docker/start.sh 在 set -e 环境下源入（source），
# 任一步失败即以非零退出码中止，容器拒绝启动（fail fast）。
echo "Running Alembic database migration: upgrade head"
python -m komari_bot.db.orm_bootstrap upgrade head
echo "Database migration completed"

echo "Running versioned initial data seed: seed_bootstrap"
python -m komari_bot.db.seed_bootstrap
echo "Initial data seed completed"
