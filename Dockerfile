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


# 第二阶段：按 package-lock.json 构建长期存活的 DSH 运行时
FROM node:24.9.0-bookworm-slim AS dsh_build

WORKDIR /dsh

# 先复制 lock 与本地包清单，最大化依赖层缓存
COPY ./dsh/package.json ./dsh/package-lock.json /dsh/
COPY ./dsh/runtime/package.json /dsh/runtime/
COPY ./dsh/bundle/package.json /dsh/bundle/

RUN npm ci --no-audit --no-fund

# 复制可编译源码；node_modules/dist 由 .dockerignore 排除，构建阶段重新生成
COPY ./dsh /dsh

RUN npm run build \
    && npm prune --omit=dev


# 第三阶段：运行环境
FROM python:3.13-slim

ARG SENTRY_RELEASE

WORKDIR /app

# ENV
ENV TZ=Asia/Shanghai
ENV PYTHONPATH=/app
ENV APP_MODULE=_main:app
ENV MAX_WORKERS=1
ENV SENTRY_RELEASE="${SENTRY_RELEASE}"
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

# Node 官方二进制所需的运行时库（不安装 npm/编译器）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 libgcc-s1 libatomic1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 从依赖阶段复制由 lock 文件精确安装的运行时环境
COPY --from=dependencies_stage /opt/venv /opt/venv

# 复制脚本和配置文件
COPY ./docker/gunicorn_conf.py ./docker/start.sh /
# prestart 在应用启动前执行 Alembic 迁移（由 start.sh 源入执行）
COPY ./docker/prestart.sh /app/prestart.sh
RUN chmod +x /start.sh /app/prestart.sh

COPY ./docker/bot.py /app/
COPY ./docker/_main.py /app/

# 复制项目所有代码
COPY . /app/

# 已构建的 DSH 资产最后覆盖，避免宿主上下文里的源码/缓存遮蔽构建产物
COPY --from=dsh_build /usr/local/bin/node /usr/local/bin/node
COPY --from=dsh_build /dsh/package.json /app/dsh/package.json
COPY --from=dsh_build /dsh/package-lock.json /app/dsh/package-lock.json
COPY --from=dsh_build /dsh/node_modules /app/dsh/node_modules
COPY --from=dsh_build /dsh/runtime/package.json /app/dsh/runtime/package.json
COPY --from=dsh_build /dsh/runtime/dist /app/dsh/runtime/dist
COPY --from=dsh_build /dsh/bundle /app/dsh/bundle
COPY --from=dsh_build /dsh/compose /app/dsh/compose

CMD ["/start.sh"]
