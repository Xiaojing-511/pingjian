#!/bin/sh
# 容器里同时跑 FastAPI 和 DSH。插件在镜像构建时已经装进 profile，这里只在目录丢失时补一次。
set -eu
export DSH_HOME="${DSH_HOME:-/opt/pingjian/dsh}"
mkdir -p "$DSH_HOME" /tmp/pingjian-workspace
if [ ! -d "$DSH_HOME/profiles/sdk-minimal" ]; then
  dsh --profile sdk-minimal --dump-default-config
  dsh plugin --profile sdk-minimal add file:/app/dsh-plugin/pingjian-tools
fi
exec uvicorn server.main:app --host 0.0.0.0 --port 8787
