FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    BRIDGE_HOME=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/app/

RUN mkdir -p /app/data /app/config

# 非 root 运行；挂载目录由 compose 指定，权限在宿主机 chown 到 1000:1000。
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin bridge \
    && chown -R bridge:bridge /app/data /app/config
USER bridge

CMD ["python", "/app/app/main.py"]
