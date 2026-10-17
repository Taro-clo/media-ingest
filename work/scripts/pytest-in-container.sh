#!/bin/bash
# 在一次性容器里用**当前源码树的测试**跑 pytest。
# 镜像里不含 tests/（Dockerfile 只 COPY app/ web/ docker/），所以用 docker cp 送进去。
#
# 用法：
#   bash scripts/pytest-in-container.sh [镜像 tag] [测试目录] [容器名]
# 默认：
#   镜像 tag media-ingest:dev
#   测试目录 <脚本所在目录>/../tests
#   容器名   media-ingest-pytest
# 环境变量：
#   PYTEST_PORT  测试夹具期望的端口（默认 18081）
set -u

IMG="${1:-media-ingest:dev}"
TESTS="${2:-$(cd "$(dirname "$0")/.." && pwd)/tests}"
C="${3:-media-ingest-pytest}"
PORT="${PYTEST_PORT:-18081}"

[ -d "$TESTS" ] || { echo "找不到测试目录: $TESTS" >&2; exit 1; }
echo "TESTS=$TESTS  FILES=$(find "$TESTS" -name '*.py' | wc -l)"

docker rm -f "$C" >/dev/null 2>&1 || true
docker run -d --name "$C" --entrypoint python "$IMG" -c "import time; time.sleep(1e9)" >/dev/null
docker exec "$C" rm -rf /srv/mlm/tests
docker cp "$TESTS" "$C:/srv/mlm/tests"
docker exec "$C" python -m pip install -q pytest httpx 2>&1 | tail -1

echo "== pytest =="
docker exec -e PORT="$PORT" -w /srv/mlm "$C" \
  python -m pytest -q tests --tb=line -p no:cacheprovider 2>&1 | tail -40
RC=$?
docker rm -f "$C" >/dev/null 2>&1 || true
echo "PYTEST_DONE"
exit $RC
