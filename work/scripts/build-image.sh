#!/bin/sh
# 在宿主机上把源码包解到全新构建目录并构建镜像，随后断言镜像内结构。
#
# 用法：
#   sh scripts/build-image.sh [源码包] [镜像 tag] [构建目录]
# 默认：
#   源码包   /tmp/mlm-src.tgz
#   镜像 tag media-ingest:dev
#   构建目录 /vol1/1000/Docker/media-ingest/build-src
# 可用环境变量：
#   BUILD_LOG    docker build 的日志路径（默认 /tmp/mlm-build.log）
#
# 说明：每次都会 rm -rf 构建目录再解包，保证「构建树 == 源码包内容」。
set -e

TARBALL="${1:-/tmp/mlm-src.tgz}"
TAG="${2:-media-ingest:dev}"
SRC="${3:-/vol1/1000/Docker/media-ingest/build-src}"
LOG="${BUILD_LOG:-/tmp/mlm-build.log}"

[ -f "$TARBALL" ] || { echo "找不到源码包: $TARBALL" >&2; exit 1; }

rm -rf "$SRC"
mkdir -p "$SRC"
tar xzf "$TARBALL" -C "$SRC"
echo "EXTRACTED_FILES=$(find "$SRC" -type f | wc -l)"
echo -n "VERSION="; sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/app/__init__.py" | head -1

echo "--- docker build ---"
cd "$SRC"
if docker build -t "$TAG" -f docker/Dockerfile . > "$LOG" 2>&1; then
  echo BUILD_RC=0
else
  echo BUILD_RC=$?
  tail -20 "$LOG"
  exit 1
fi
tail -5 "$LOG"

echo "--- 镜像内结构断言 ---"
docker run --rm --entrypoint sh "$TAG" -c '
  test -d /srv/mlm/app && echo OK_SRV_APP
  test -d /srv/mlm/web && echo OK_SRV_WEB
  test -x /usr/local/bin/mlm-entrypoint && echo OK_ENTRYPOINT
  test -x /usr/local/bin/mlm-healthcheck && echo OK_HEALTHCHECK
'
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' | grep -F "$(echo "$TAG" | cut -d: -f1)" || true
