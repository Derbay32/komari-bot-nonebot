# 第一阶段：生成 bot.py
FROM python:3.13-slim as requirements_stage

WORKDIR /wheel

# 安装 pipx 并生成 bot.py
RUN pip install --no-cache-dir pipx && \
    pipx run --no-cache nb-cli generate -f /tmp/bot.py && \
    rm -rf /root/.local/pipx


# 第二阶段：运行环境
FROM python:3.13-slim

# 设置工作目录和环境变量
WORKDIR /app
ENV TZ=Asia/Shanghai \
    PYTHONPATH=/app \
    APP_MODULE=_main:app \
    MAX_WORKERS=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 安装系统依赖并清理缓存（合并为一层）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 复制脚本并设置权限
COPY ./docker/gunicorn_conf.py ./docker/start.sh /
RUN chmod +x /start.sh

# 从第一阶段复制生成的 bot.py
COPY --from=requirements_stage /tmp/bot.py /app/
COPY ./docker/_main.py /app/

# 复制并安装依赖（利用 Docker 缓存）
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt && \
    rm -rf /root/.cache/pip

# 复制项目代码（最后复制，避免代码变更导致重新安装依赖）
COPY . /app/

CMD ["/start.sh"]