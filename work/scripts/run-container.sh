#!/bin/sh
# 起一个**隔离的测试实例**（绝不触碰生产容器与生产数据卷）。
#
# 用法：
#   sh scripts/run-container.sh [镜像 tag] [容器名] [端口] [宿主数据目录]
# 默认：
#   镜像 tag   media-ingest:dev
#   容器名     media-ingest-test
#   端口       18087
#   数据目录   /vol1/1000/Docker/media-ingest/test-data
# 环境变量：
#   MEDIA_ROOT_HOST  媒体共同父挂载（默认 /vol1/1000）
#   PUID / PGID      运行身份（默认 1000 / 1001）
#   RESET=1          先清空宿主数据目录（默认保留，方便重启验证持久化）
#
# 说明：数据目录默认**复用**，只有显式 RESET=1 才删除，避免误删。
set -e

TAG="${1:-media-ingest:dev}"
C="${2:-media-ingest-test}"
PORT="${3:-18087}"
DATA="${4:-/vol1/1000/Docker/media-ingest/test-data}"
MEDIA="${MEDIA_ROOT_HOST:-/vol1/1000}"

case "$DATA" in
  ""|"/"|"/data") echo "拒绝使用数据目录: $DATA" >&2; exit 1 ;;
esac

docker rm -f "$C" >/dev/null 2>&1 || true
if [ "${RESET:-0}" = "1" ]; then
  echo "RESET=1 → 清空 $DATA"
  rm -rf "$DATA"
fi
mkdir -p "$DATA"

docker run -d --name "$C" -p "$PORT:$PORT" \
  -v "$DATA:/data" \
  -v "$MEDIA:$MEDIA" \
  -e PUID="${PUID:-1000}" -e PGID="${PGID:-1001}" \
  -e DATA_DIR=/data -e CONFIG_DIR=/data/config -e PORT="$PORT" \
  -e TZ="${TZ:-Asia/Shanghai}" -e LANG=C.UTF-8 -e PYTHONUTF8=1 \
  -e MEDIA_ROOT="$MEDIA" \
  -e HOST_PATH_MAP="{\"$MEDIA\":\"$MEDIA\"}" \
  "$TAG" >/dev/null

sleep 12
docker ps --filter "name=^${C}$" --format 'CONTAINER {{.Names}} | {{.Status}} | {{.Ports}}'
echo "--- 健康检查（应输出 status=ok 且六项全 true）---"
docker exec "$C" /usr/local/bin/mlm-healthcheck && echo HEALTHCHECK_RC=0 || echo HEALTHCHECK_RC=$?
