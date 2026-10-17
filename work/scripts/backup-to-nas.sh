#!/bin/sh
# 归档交付物：bundle + 源码 tar + sha256，并用 bundle 克隆自检完整性。
#
# 前置：先在本地生成 bundle 并推到 NAS，例如
#   git -C media-ingest bundle create /tmp/media-ingest-v1.0.0.bundle --all
#   (或 scp 推到 /tmp/)
#
# 用法：
#   sh scripts/backup-to-nas.sh <bundle 路径> <版本号> [源码树]
# 默认：
#   源码树 /vol1/1000/Docker/media-ingest/build-src
# 环境变量：
#   BACKUP_DIR  备份目录（默认 /vol1/1000/Docker/media-ingest/backups）
#   SKIP_CLONE  1 = 跳过 bundle 克隆验证（默认做验证）
#
# 说明：新的 bundle / tar 用版本号命名，**不覆盖**历史备份。
set -e

BUNDLE="${1:?用法: backup-to-nas.sh <bundle 路径> <版本号> [源码树]}"
VER="${2:?缺少版本号，例如 1.0.0}"
SRC="${3:-/vol1/1000/Docker/media-ingest/build-src}"
BK="${BACKUP_DIR:-/vol1/1000/Docker/media-ingest/backups}"

[ -f "$BUNDLE" ] || { echo "找不到 bundle: $BUNDLE" >&2; exit 1; }
mkdir -p "$BK"

cp "$BUNDLE" "$BK/media-ingest-v$VER.bundle"
sha256sum "$BK/media-ingest-v$VER.bundle" | tee "$BK/media-ingest-v$VER.bundle.sha256"

if [ -d "$SRC" ]; then
  tar -czf "$BK/src-v$VER.tar.gz" -C "$(dirname "$SRC")" "$(basename "$SRC")"
  sha256sum "$BK/src-v$VER.tar.gz" | tee "$BK/src-v$VER.tar.gz.sha256"
else
  echo "跳过源码打包（找不到 $SRC）"
fi

echo "--- 备份目录 ---"
ls -l "$BK"

if [ "${SKIP_CLONE:-0}" = "1" ]; then
  echo "SKIP_CLONE=1 → 跳过克隆验证"
  exit 0
fi

echo "--- 用 bundle 克隆验证 ---"
CLONE="/tmp/mlm-clone-v$VER"
rm -rf "$CLONE"
git clone -q "$BK/media-ingest-v$VER.bundle" "$CLONE"
cd "$CLONE"
echo "CLONE_HEAD=$(git rev-parse --short HEAD)"
echo "CLONE_TAGS=$(git tag | tr '\n' ' ')"
echo "CLONE_DIRTY=$(git status --short | wc -l)"
echo -n "CLONE_VERSION="; sed -n 's/^__version__ = "\(.*\)"/\1/p' work/app/__init__.py | head -1
