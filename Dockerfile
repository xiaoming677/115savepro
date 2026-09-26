FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PORT=5000 \
    HOST=0.0.0.0

WORKDIR /app

# 时区与编译依赖（p115client 的部分依赖需要编译）
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY templates/ ./templates/
COPY config/config.template.json ./config/config.template.json

# 首次启动若没有 config.json 则从模板生成
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -e' \
    'mkdir -p /app/config /app/log' \
    'if [ ! -s /app/config/config.json ]; then' \
    '  echo "[init] 生成 config.json"' \
    '  cp /app/config/config.template.json /app/config/config.json' \
    'fi' \
    'exec python web_app.py' > /app/start.sh && chmod +x /app/start.sh

EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/api/auth/check',timeout=5).status==200 else 1)"

CMD ["/app/start.sh"]
