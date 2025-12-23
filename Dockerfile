FROM python:3.9 as requirements_stage

WORKDIR /wheel

RUN python -m pip install --user pipx

COPY ./pyproject.toml \
  /wheel/



RUN python -m pipx run --no-cache nb-cli generate -f /tmp/bot.py


FROM python:3.9-slim

WORKDIR /app

ENV TZ Asia/Shanghai
ENV PYTHONPATH=/app

COPY ./docker/gunicorn_conf.py ./docker/start.sh /
RUN chmod +x /start.sh

ENV APP_MODULE _main:app
ENV MAX_WORKERS 1

COPY --from=requirements_stage /tmp/bot.py /app
COPY ./docker/_main.py /app

RUN pip install --no-cache-dir gunicorn uvicorn[standard] nonebot2 nonebot2[fastapi] nonebot-adapter-onebot aiohttp aiosqlite nonebot2[aiohttp] nonebot-plugin-localstore nonebot2[websockets]
COPY . /app/

CMD ["/start.sh"]