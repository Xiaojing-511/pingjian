FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DSH_HOME=/opt/pingjian/dsh \
    PINGJIAN_INTERNAL_BASE=http://127.0.0.1:8787

WORKDIR /app

# dsh plugin 在安装本地包时可能调用 pnpm。运行 SDK 本身不需要系统 Node。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs npm \
    && npm install -g pnpm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dsh.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dsh.txt

COPY server ./server
COPY dsh ./dsh
COPY dsh-plugin ./dsh-plugin
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh \
    && mkdir -p /opt/pingjian/dsh /tmp/pingjian-workspace \
    && dsh --profile sdk-minimal --dump-default-config \
    && dsh plugin --profile sdk-minimal add file:/app/dsh-plugin/pingjian-tools

EXPOSE 8787
ENTRYPOINT ["/entrypoint.sh"]
