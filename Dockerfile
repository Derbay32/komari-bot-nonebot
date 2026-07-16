# 第一阶段：按 poetry.lock 构建运行时虚拟环境
FROM python:3.13-slim AS dependencies_stage

ARG POETRY_VERSION=2.2.1
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
ENV POETRY_NO_INTERACTION=1

WORKDIR /app

RUN python -m pip install --no-cache-dir "poetry==${POETRY_VERSION}" \
    && python -m venv "${VIRTUAL_ENV}"

COPY ./pyproject.toml ./poetry.lock /app/

RUN poetry check --lock \
    && poetry sync --only main --no-root --no-ansi


# 第二阶段：运行环境
FROM python:3.13-slim

WORKDIR /app

# ENV
ENV TZ=Asia/Shanghai
ENV PYTHONPATH=/app
ENV APP_MODULE=_main:app
ENV MAX_WORKERS=1
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

# 从依赖阶段复制由 lock 文件精确安装的运行时环境
COPY --from=dependencies_stage /opt/venv /opt/venv

# 复制脚本和配置文件
COPY ./docker/gunicorn_conf.py ./docker/start.sh /
RUN chmod +x /start.sh

COPY ./docker/bot.py /app/
COPY ./docker/_main.py /app/

# 复制项目所有代码
COPY . /app/

CMD ["/start.sh"]
