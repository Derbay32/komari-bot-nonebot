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

RUN pip install --no-cache-dir \
gunicorn uvicorn[standard] nonebot2 \
nonebot-adapter-onebot>=2.4.6 \
aiohttp>=3.13.2,<4.0.0 \
aiosqlite>=0.22.0,<0.23.0 \
nonebot-plugin-localstore>=0.7.4 \
nonebot-plugin-apscheduler>=0.5.0 \
nonebot2[fastapi]>=2.4.4
COPY . /app/

CMD ["/start.sh"]