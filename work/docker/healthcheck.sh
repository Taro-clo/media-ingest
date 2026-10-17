#!/bin/sh
# =============================================================================
# healthcheck —— 请求 /api/system/health（无需认证），要求 body 里 status == "ok"
# =============================================================================
# ⚠️ 为什么**不能**只看 HTTP 状态码（务必别再改回去）：
#   `GET /api/system/health` 无论健康与否都返回 **HTTP 200**（端点契约见
#   docs/contracts/ARCHITECTURE.md §5 / §1）：业务不健康是用 body 里的 `"status":"degraded"`
#   表达的，HTTP 层永远 200。所以"只看 200"的健康检查**永远不会失败** —— 数据卷不可写、
#   下载卷没挂的容器，在 Docker 眼里依旧是 healthy，部署事故会被静默掩盖。
#   正确判据：解析 JSON，**只有 `status == "ok"` 才 exit 0**。
#
# body 形态（四项）：
#   {"status":"ok"|"degraded","checks":{"db":true,"data_dir":true,
#                                       "processors":true,"config":true}}
#   checks 各键对应的真实部署事故：
#     db                 数据卷 /data 不可写，或 SQLite 打不开
#     data_dir           /data 不存在或不可写
#     processors         三个内置处理器没能加载（PROCESSORS.md §9；处理规则坏了 = 重命名会错）
#     config             CONFIG_DIR（/data/config）不可写 ⇒ 配置无法落盘、WebUI 永远不同步
#   任一 false（或必需键缺失）都判 unhealthy，并把失败项逐条打印到 stderr 便于 `docker inspect`。
#   四项**全部必需**：后端已落地（`GET /api/system/health` 恒回 4 项），缺键即真异常。
#   后端日后新增的键**不参与判定**（只作为 [提示] 打到 stderr）：未知键的 false 未必是事故，
#   拿它判死会让容器在升级后端后凭空 unhealthy；若新键真出事，后端会算进 status="degraded"。
#
# 设计与理由：
#   * 解析只用镜像自带的 python3 标准库（urllib + json）。`python:3.11-slim` **没有
#     curl/wget**，所以不引入任何新依赖、也不假设有 curl。
#   * 端口读容器内 $PORT（与 uvicorn --port "${PORT:-18081}" 同一来源），默认 18081。
#   * 目标端口未监听 / 连接被拒 / 超时 / 返回非 JSON / HTTP 非 200 → 一律非 0 退出且
#     不抛栈（catch 全部异常，打印一行可读原因）。
#   * 严格 POSIX sh：不用 [[ ]]、不用数组、不用 bash 特有内建。
#
# ⚠️ **本脚本是纯只读的：只向 127.0.0.1 发一个 HTTP 请求 + 解析 JSON，
#    绝不在文件系统上创建/修改/删除任何文件 —— 更不碰媒体挂载（MEDIA_ROOT）。**
#    源与目标共用一条共同父挂载（硬链接不能跨挂载），"下载卷以 :ro 提供结构性只读保证"
#    这层保护并不存在，所以"探针绝不写文件"这条纪律必须显式守住：任何在这里
#    `touch`/`mkstemp` 的探针都会把写入落到真实媒体目录里。若将来要加探针，请保持
#    只读（优先用 HTTP 端点，例如 /api/system/health 与 /api/system/info）。
# =============================================================================
set -eu

PORT="${PORT:-18081}"
URL="http://127.0.0.1:${PORT}/api/system/health"

# 解析 + 判定（放在一处，python3/python 共用）。退出码：0=ok，1=degraded/解析失败，
# 2=连不上/超时/HTTP 非 200。所有诊断信息走 stderr。
__MLM_HEALTH_PY='
import json, sys, urllib.error, urllib.request

url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=5) as resp:
        code = resp.status
        body = resp.read().decode("utf-8", "replace")
except Exception as exc:                      # 端口未监听 / 连接被拒 / 超时 / DNS
    sys.stderr.write("healthcheck: 无法访问 %s：%s\n" % (url, exc))
    sys.exit(2)

if code != 200:
    sys.stderr.write("healthcheck: %s 返回 HTTP %s（应 200）\n" % (url, code))
    sys.exit(2)

try:
    data = json.loads(body)
except Exception as exc:
    sys.stderr.write("healthcheck: 响应不是合法 JSON：%s\n原始内容：%s\n" % (exc, body[:500]))
    sys.exit(1)

status = data.get("status")
checks = data.get("checks") or {}

# 必需键：缺一不可（异常响应里少了任何一个都算不健康）
#
# processors / config 也在必需项里（PROCESSORS.md §9）。后端 checks 恒为 4 项，
# 所以「键不存在」是真异常，不再有宽容分支。
required = ("db", "data_dir", "processors", "config")
missing = [k for k in required if k not in checks]
failed = [k for k in required if k in checks and not checks[k]]
# 后端日后**新增**的 check 键：一律**不**参与判定。理由：新键的 false 未必等于事故
# （例如某种"当前空闲/无警告"的语义），拿未知键判死会让容器在升级后端后凭空 unhealthy，
# 而这类事故排查成本极高。若新键真表示事故，后端会把它算进 status="degraded"，
# 我们仍然拦得住；这里只把为 false 的未知键当**提示**打到 stderr，保留线索。
extra_false = [k for k, v in checks.items() if k not in required and not v]

if status == "ok" and not failed and not missing:
    print("healthcheck: status=ok，checks 全部通过（%s）" % ", ".join(required))
    for k in extra_false:
        sys.stderr.write("  [提示] checks.%s = %r（后端新增键，不参与判定）\n" % (k, checks.get(k)))
    sys.exit(0)

sys.stderr.write("healthcheck: 不健康 status=%r\n" % (status,))
for k in missing:
    sys.stderr.write("  [缺失] checks.%s 未出现在响应里\n" % k)
for k in failed:
    sys.stderr.write("  [失败] checks.%s = %r\n" % (k, checks.get(k)))
for k in extra_false:
    sys.stderr.write("  [提示] checks.%s = %r（后端新增键，不参与判定）\n" % (k, checks.get(k)))
if not failed and not missing:
    sys.stderr.write("  原因：status 不是 \"ok\"\n")
sys.exit(1)
'

# 1) 首选容器自带的 python3
if command -v python3 >/dev/null 2>&1; then
    exec python3 -c "$__MLM_HEALTH_PY" "$URL"
fi

# 2) 回退：python（某些精简镜像没有 python3 这个软链）
if command -v python >/dev/null 2>&1; then
    exec python -c "$__MLM_HEALTH_PY" "$URL"
fi

# 3) 最后回退：wget（本镜像不会走到这里；没有 python 时只能退化为"能连上就算活"）
if command -v wget >/dev/null 2>&1; then
    echo "healthcheck: 容器内没有 python，退化为仅探测 HTTP 可达（无法判断 status）" >&2
    exec wget -q -T 5 -O /dev/null "$URL"
fi

echo "healthcheck: 容器内既没有 python3/python 也没有 wget，无法执行健康检查" >&2
exit 1
