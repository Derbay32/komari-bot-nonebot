# 第一阶段：生成 bot.py (保持你原来的逻辑)
FROM python:3.9-slim as requirements_stage

WORKDIR /wheel

RUN python -m pip install --no-cache-dir pipx

COPY ./pyproject.toml /wheel/

# 使用 pipx 运行 nb-cli 生成 bot.py
RUN python -m pipx run --no-cache nb-cli generate -f /tmp/bot.py


# 第二阶段：运行环境
FROM python:3.9-slim

WORKDIR /app

# 规范化 ENV 格式 (key=value)
ENV TZ=Asia/Shanghai
ENV PYTHONPATH=/app
ENV APP_MODULE=_main:app
ENV MAX_WORKERS=1

# 复制脚本和配置文件
COPY ./docker/gunicorn_conf.py ./docker/start.sh /
RUN chmod +x /start.sh

# 从第一阶段复制生成的 bot.py
COPY --from=requirements_stage /tmp/bot.py /app/
COPY ./docker/_main.py /app/

# --- 关键修改部分 ---
# 先复制依赖文件进行安装，利用 Docker 缓存
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# 最后复制项目所有代码（这样代码变动时不需要重新安装依赖）
COPY . /app/

CMD ["/start.sh"]