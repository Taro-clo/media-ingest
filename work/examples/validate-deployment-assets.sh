#!/bin/sh
# =============================================================================
# validate-deployment-assets.sh —— 容器化资产自验（只读，不改任何仓库/生产文件）
# =============================================================================
# 与 qBittorrent 的配合见 examples/qbittorrent-autorun.md（那是运行时验收，本脚本
# 只做**静态**自验；两者的边界在文档里写清楚了）。
#
# 在**任何有 docker compose + python3 的 Linux 机器**上执行：
#
#     sh examples/validate-deployment-assets.sh            # 默认当前目录为仓库根
#     sh examples/validate-deployment-assets.sh /path/to/repo
#
# 它检查的是"文件层面可静态验证"的部分（全部只读、只写 mktemp 出来的临时目录）：
#   1. entrypoint.sh / healthcheck.sh / minimal-repro.sh 的 POSIX sh（dash）语法
#   2. 同样三个文件的 bash 语法
#   3. 是否残留 CR（Windows 编辑后常见的致命问题）
#   4. shellcheck（若装了）
#   5. healthcheck.sh 的**判据**：用 python 标准库起桩服务做行为测试，验证它只认 body 里的
#      `status=="ok"`（HTTP 200 不够 —— 该端点永远 200，degraded 也用 200 表达）、checks 里有
#      false 要逐条打到 stderr、**四项必需 checks 缺任何一项也判 unhealthy**、**后端新加的未知
#      checks 键不参与判定**（只留[提示]线索，避免升级后端即误报）、非 JSON 响应不抛栈、全健康时
#      exit 0、无服务时非 0，以及**无 python/wget 时不许把 HTTP 200 当健康**（降级回退的边界）；
#      外加对 healthcheck.sh 的静态断言（必须解析 JSON、不得依赖 curl 等）
#   6. `docker compose config` 能否成功插值（YAML 结构 + .env 契约）**以及**渲染后的对外事实：
#      镜像名/容器名/restart/ports/**两条 volumes**（媒体**单一共同父挂载**、容器路径 =
#      宿主路径，外加 /data）/environment 键与值
#   7. **端口贯穿**：PORT=18099 时宿主发布端口与容器内 PORT 必须同时移动
#   8. Dockerfile 断言：FROM/gosu/HEALTHCHECK/ENTRYPOINT/CMD 的 shell 展开/--workers 1/
#      COPY 布局/无 USER 指令/ENV 里 PORT 默认值只声明一次
#   9. `.dockerignore` 规则：Dockerfile 的 COPY 源一条都不许被排除（含"COPY 源在 context 里真实
#      存在"这一方向），机密/状态/噪音必须被排除
#  10. `docker build --check` 干跑（可选：MLM_BUILD_CHECK=1 才跑，不写镜像层）
#  11. 回调命令与契约一致：文档与 WebUI 设置页生成的命令都含 7 个 --data-urlencode、
#      token 在 URL query、且**不含** `kind=%K`（%K 是 Torrent ID，不是任务类型）
#  12. examples 的 API 调用口径：建库必须用 `processor_id`，可复制示例里不得出现
#      `permission_mode`（它是默认关闭的显式开关；示例里仍不传）
#  13. **真实硬链接探针**：在媒体共同父挂载下，从源根**有界递归**（深度 ≤3、
#      条目 ≤512、不跟随符号链接 —— 媒体库模型是 `源根/标题文件夹/文件`，只看一层永远找不到样本）
#      挑一个**已存在的普通文件**，在目标目录建临时硬链接再删；成功 PASS，EXDEV FAIL 并打印
#      可执行修法。**为什么不用 st_dev 判**：同一个 btrfs 子卷下的两个独立 bind，st_dev、
#      fsid、subvolid 三者可以完全相同，`link()` 照样 -EXDEV —— 内核比较的是**挂载身份**
#      （fs/namei.c 的 do_linkat()：old_path.mnt == new_path.mnt）。探针全程不写源目录。
#      用 SRC_ROOT/TGT_ROOT 指定源与目标根（**必须显式给出**，本节不自动取 .env 的值，
#      免得把临时 .mlm_probe_*.link 写进真实媒体库）；缺任一个即 SKIP（无法实证）。
# （脚本里每节的 note 标题就是准；本清单若与 note 不一致，以 note 为准。）
#
# 实现注意（改这个脚本时别踩）：
#   * `set -e` 下 python 退出 1 会直接终止脚本，所以每个 python 断言都用
#     `rc=0; run_py <<'PYEOF' 2>&1 || rc=$?` 形式接住退出码，再 if 判 rc。
#   * heredoc 作 `if` 的条件在 dash 下解析不可靠（会报 "fi unexpected
#     (expecting then)"），所以**不要**写 `if python3 - <<'PYEOF' ... PYEOF; then`。
#   * `$?` 紧跟在 heredoc 之后不可靠，必须像上面那样在同行用 `|| rc=$?` 接。
#   * 第 5 节的桩服务占 HC_PORT=18086。脚本正常结束时每个用例都会 stop_stub；但若脚本被
#     Ctrl-C / 被 timeout 杀掉，桩可能变成孤儿并占住 18086，下一次运行会连续
#     "桩服务启动失败"。届时 `pkill -f 'stub/serve.py'` 收尸即可。
#
# 不检查（需要真实运行，属于部署验收，见 docs/guides/DEPLOYMENT.md）：
#   * 镜像能否构建、uvicorn 是否真监听 PORT、容器是否 (healthy)
# （第 13 节会在**宿主**上做一次真实 link 探针，它验的是部署拓扑，不等价于跑完一次真任务
#   —— 真任务验收见 examples/minimal-repro.sh）
# =============================================================================
set -eu

REPO="${1:-$(pwd)}"
# 归一化为**绝对路径**：脚本后面会 cd 到临时目录，若 REPO 是相对的（常见用法是传 "."），
# 第 9 节的 COPY 源存在性检查会在 cd 之后全部指错地方。
REPO="$(cd "$REPO" 2>/dev/null && pwd || printf '%s' "$REPO")"
WORK="$(mktemp -d /tmp/mlm-validate.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
note() { printf '\n===== %s =====\n' "$*"; }
ok()   { PASS=$((PASS + 1)); printf '  [PASS] %s\n' "$*"; }
ko()   { FAIL=$((FAIL + 1)); printf '  [FAIL] %s\n' "$*"; }

[ -f "$REPO/docker-compose.yml" ] || { echo "在 $REPO 找不到 docker-compose.yml；请把仓库根作为参数传入" >&2; exit 2; }
[ -f "$REPO/docker/Dockerfile" ]  || { echo "在 $REPO 找不到 docker/Dockerfile" >&2; exit 2; }

cp "$REPO/docker-compose.yml" "$WORK/"
[ -f "$REPO/docker-compose.release.yml" ] && cp "$REPO/docker-compose.release.yml" "$WORK/"
[ -f "$REPO/.env.example" ] && cp "$REPO/.env.example" "$WORK/"
cp "$REPO/docker/entrypoint.sh" "$REPO/docker/healthcheck.sh" "$WORK/"
cp "$REPO/docker/Dockerfile" "$WORK/"
[ -f "$REPO/examples/minimal-repro.sh" ] && cp "$REPO/examples/minimal-repro.sh" "$WORK/"
[ -f "$REPO/.dockerignore" ] && cp "$REPO/.dockerignore" "$WORK/"
[ -f "$REPO/examples/qbittorrent-autorun.md" ] && cp "$REPO/examples/qbittorrent-autorun.md" "$WORK/"
[ -f "$REPO/examples/deploy-walkthrough.md" ] && cp "$REPO/examples/deploy-walkthrough.md" "$WORK/"
[ -f "$REPO/web/js/pages/settings.js" ] && mkdir -p "$WORK/settings" && cp "$REPO/web/js/pages/settings.js" "$WORK/settings/settings.js"

cd "$WORK"

note "1. POSIX sh 语法（dash）"
for f in entrypoint.sh healthcheck.sh minimal-repro.sh; do
    [ -f "$f" ] || continue
    if sh -n "$f" 2>"$WORK/.err"; then ok "$f"; else ko "$f: $(cat "$WORK/.err")"; fi
done

note "2. bash 语法"
for f in entrypoint.sh healthcheck.sh minimal-repro.sh; do
    [ -f "$f" ] || continue
    if bash -n "$f" 2>"$WORK/.err"; then ok "$f"; else ko "$f: $(cat "$WORK/.err")"; fi
done

note "3. 是否残留 CR（Windows 编辑器引入的行尾 \\r 会让 /bin/sh 解析失败）"
cr_files=""
for f in entrypoint.sh healthcheck.sh minimal-repro.sh docker-compose.yml docker-compose.release.yml .env.example; do
    [ -f "$f" ] || continue
    if LC_ALL=C grep -q "$(printf '\r')" "$f" 2>/dev/null; then cr_files="$cr_files $f"; fi
done
if [ -n "$cr_files" ]; then
    ko "以下文件含 CR：$cr_files"
else
    ok "三个 shell 脚本与 compose/.env.example 均无 CR"
fi

note "4. shellcheck（可选）"
if command -v shellcheck >/dev/null 2>&1; then
    sc_targets="entrypoint.sh healthcheck.sh"
    # shellcheck disable=SC2086  # 目标列表按空白拆开是有意的
    if shellcheck -s sh -S warning $sc_targets; then ok "shellcheck 无 warning 级问题"; else ko "shellcheck 报出问题（见上）"; fi
else
    printf '  [SKIP] 未安装 shellcheck\n'
fi

note "5. healthcheck.sh：无服务必须失败 + 必须按 body 的 status 判定（HTTP 200 不够）"
if PORT=18099 sh healthcheck.sh; then
    ko "无服务时竟然返回 0（探针失效）"
else
    ok "无可连服务时退出非 0（连接被拒不算健康）"
fi

# ---- 5b. 行为测试：起一个只回 HTTP 200 的桩服务，验证探针真的看 status ----
# 为什么必须做这一节：`GET /api/system/health` **永远**返回 HTTP 200，业务不健康用
# body 里的 "status":"degraded" 表达。只看状态码的探针永远不会失败，degraded 的容器
# 在 Docker 眼里仍是 healthy —— 只看状态码的探针永远不会失败，这里用行为测试钉住它。
# 桩服务只用 python3 标准库（http.server），不需要 flask/fastapi。
if command -v python3 >/dev/null 2>&1; then
    mkdir -p "$WORK/stub"
    STUB_PY="$WORK/stub/serve.py"
    cat > "$STUB_PY" <<'PYS'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
payload = open(sys.argv[2], encoding="utf-8").read().encode("utf-8")

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *a):
        pass

HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PYS
    HC_PORT=18086
    START_OK=1
    run_stub() {
        # $1 = json 文件路径；起桩服务，成功写 $WORK/stub.pid 并返回 0
        rm -f "$WORK/stub.pid"
        python3 "$STUB_PY" "$HC_PORT" "$1" >"$WORK/stub.log" 2>&1 &
        STUB=$!
        i=0
        while [ "$i" -lt 50 ]; do
            if python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.2)
try:
    s.connect(('127.0.0.1', $HC_PORT)); s.close(); sys.exit(0)
except Exception:
    sys.exit(1)" 2>/dev/null; then
                echo "$STUB" > "$WORK/stub.pid"
                return 0
            fi
            i=$((i + 1))
            sleep 0.1
        done
        kill "$STUB" 2>/dev/null || true
        return 1
    }
    stop_stub() {
        if [ -f "$WORK/stub.pid" ]; then
            kill "$(cat "$WORK/stub.pid")" 2>/dev/null || true
            rm -f "$WORK/stub.pid"
        fi
    }

    # /api/system/health 恒回 **4** 项 checks（PROCESSORS.md §9）。
    # 下面所有桩响应都用真实形态，避免"用旧形态测新判据"的假绿。
    HC_EXTRA='"processors":true,"config":true'

    # (1) status=degraded，且 checks.data_dir=false，但 HTTP 仍是 200
    printf '%s' "{\"status\":\"degraded\",\"checks\":{\"db\":true,\"data_dir\":false,$HC_EXTRA}}" > "$WORK/stub_degraded.json"
    if run_stub "$WORK/stub_degraded.json"; then
        rc=0
        PORT="$HC_PORT" sh healthcheck.sh >"$WORK/hc.out" 2>"$WORK/hc.err" || rc=$?
        stop_stub
        if [ "$rc" -eq 0 ]; then
            ko "桩服务返回 HTTP 200 + status=degraded，探针却 exit 0（只看状态码的老缺陷又回来了）"
        else
            ok "status=degraded（HTTP 200）被判 unhealthy（exit $rc）"
        fi
        if LC_ALL=C grep -q "data_dir" "$WORK/hc.err"; then
            ok "失败项逐条打印到 stderr（含 data_dir）"
        else
            ko "stderr 没有逐条列出失败的 checks（部署排查会没线索）：$(head -c 200 "$WORK/hc.err")"
        fi
    else
        ko "桩服务启动失败（端口 $HC_PORT 被占？），5b 行为测试跳过"
    fi

    # (2) status=ok 但某个 check 为 false → 也必须失败（不能只看 status 字段）
    printf '%s' "{\"status\":\"ok\",\"checks\":{\"db\":true,\"data_dir\":false,$HC_EXTRA}}" > "$WORK/stub_checkfalse.json"
    if run_stub "$WORK/stub_checkfalse.json"; then
        rc=0
        PORT="$HC_PORT" sh healthcheck.sh >"$WORK/hc.out" 2>"$WORK/hc.err" || rc=$?
        stop_stub
        if [ "$rc" -eq 0 ]; then
            ko "status=ok 但 checks.data_dir=false 时探针仍 exit 0（漏判）"
        else
            ok "checks 里有 false 时判 unhealthy（exit $rc），不只依赖 status 字段"
        fi
    else
        ko "桩服务启动失败（端口 $HC_PORT 被占？），第二个行为测试跳过"
    fi

    # (3) 完全健康 → 必须 exit 0（否则探针只会假阳性失败）
    printf '%s' "{\"status\":\"ok\",\"checks\":{\"db\":true,\"data_dir\":true,$HC_EXTRA}}" > "$WORK/stub_ok.json"
    if run_stub "$WORK/stub_ok.json"; then
        rc=0
        PORT="$HC_PORT" sh healthcheck.sh >"$WORK/hc.out" 2>"$WORK/hc.err" || rc=$?
        stop_stub
        if [ "$rc" -eq 0 ]; then
            ok "status=ok 且 checks 全 true 时 exit 0（不会假阳性失败）"
        else
            ko "健康响应被误判为 unhealthy（exit $rc）：$(head -c 200 "$WORK/hc.err")"
        fi
    else
        ko "桩服务启动失败（端口 $HC_PORT 被占？），第三个行为测试跳过"
    fi

    # (4) 返回非 JSON（例如反向代理插了一页 HTML）→ 必须失败且不抛栈
    printf '%s' '<html>502 Bad Gateway</html>' > "$WORK/stub_html.json"
    if run_stub "$WORK/stub_html.json"; then
        rc=0
        PORT="$HC_PORT" sh healthcheck.sh >"$WORK/hc.out" 2>"$WORK/hc.err" || rc=$?
        stop_stub
        if [ "$rc" -eq 0 ]; then
            ko "响应不是 JSON 却判健康"
        elif LC_ALL=C grep -q "Traceback" "$WORK/hc.err"; then
            ko "非 JSON 响应时抛了 Python 栈（应打印一行可读原因）"
        else
            ok "非 JSON 响应 → 非 0 且无栈回溯（exit $rc）"
        fi
    else
        ko "桩服务启动失败（端口 $HC_PORT 被占？），第四个行为测试跳过"
    fi

    # (5) 容器里既没有 python 也没有 wget 时，**不能**靠 HTTP 200 蒙混过关。
    # 这一条钉住 wget 那级降级回退的边界：降级路径只探"HTTP 可达"，而本端点永远 200，
    # 所以必须在探活**之前**判定，或干脆失败 —— 绝不能把 degraded 报成健康。
    # 手段：用 env -i + 只含必要工具的 PATH 跑探针（python3/python/wget 在 /usr/bin，
    # 不在这个 PATH 里），桩服务仍回 degraded。
    if run_stub "$WORK/stub_degraded.json"; then
        rc=0
        env -i PATH=/usr/sbin:/sbin:/bin \
            PORT="$HC_PORT" sh healthcheck.sh >"$WORK/hc.out" 2>"$WORK/hc.err" || rc=$?
        stop_stub
        if [ "$rc" -eq 0 ]; then
            ko "无 python 无 wget 时对 degraded 响应仍 exit 0（降级路径把 200 当健康）"
        else
            ok "无 python/wget 时不报假健康（exit $rc；降级路径不会把 HTTP 200 当成功）"
        fi
    else
        ko "桩服务启动失败（端口 $HC_PORT 被占？），第五个行为测试跳过"
    fi

    # (6) processors 是**必需**键：缺失（响应被裁剪）必须判 unhealthy。
    # 这一条钉住"缺键就是真异常、不许静默放过"的决定：后端已落地。
    printf '%s' '{"status":"ok","checks":{"db":true,"data_dir":true,"config":true}}' > "$WORK/stub_missing_optional.json"
    if run_stub "$WORK/stub_missing_optional.json"; then
        rc=0
        PORT="$HC_PORT" sh healthcheck.sh >"$WORK/hc.out" 2>"$WORK/hc.err" || rc=$?
        stop_stub
        if [ "$rc" -eq 0 ]; then
            ko "checks 少了必需的 processors 却判健康（缺键必须 unhealthy）"
        elif LC_ALL=C grep -q "\[缺失\]" "$WORK/hc.err"; then
            ok "必需键缺失判 unhealthy（exit $rc），并在 stderr 标出 [缺失]"
        else
            ko "缺键判了 unhealthy，但 stderr 没有 [缺失] 明细：$(head -c 200 "$WORK/hc.err")"
        fi
    else
        ko "桩服务启动失败（端口 $HC_PORT 被占？），第六个行为测试跳过"
    fi

    # (7) 新增两项任一为 false（status 仍报 ok）也必须失败 —— 它们各自对应真事故：
    # processors=false ⇒ 内置处理器没加载（重命名规则坏了）；config=false ⇒ /data/config 不可写。
    printf '%s' '{"status":"ok","checks":{"db":true,"data_dir":true,"processors":true,"config":false}}' > "$WORK/stub_config_false.json"
    if run_stub "$WORK/stub_config_false.json"; then
        rc=0
        PORT="$HC_PORT" sh healthcheck.sh >"$WORK/hc.out" 2>"$WORK/hc.err" || rc=$?
        stop_stub
        if [ "$rc" -eq 0 ]; then
            ko "checks.config=false（/data/config 不可写）却判健康"
        elif LC_ALL=C grep -q "config" "$WORK/hc.err"; then
            ok "checks.config=false 判 unhealthy（exit $rc）并指出具体项"
        else
            ko "config=false 判了 unhealthy，但 stderr 没指出是哪一项：$(head -c 200 "$WORK/hc.err")"
        fi
    else
        ko "桩服务启动失败（端口 $HC_PORT 被占？），第七个行为测试跳过"
    fi

    # (8) 后端日后**新增**的 checks 键（这里模拟一个为 false 的未知键）不得判死容器。
    # 理由：未知键的 false 未必是事故，拿它判死会让"升级后端"直接变成"容器凭空 unhealthy"，
    # 而这类事故排查成本极高。四项必需键齐全且为 true + status=ok 时，多出来的键只当[提示]。
    # 若新键真出事，后端会把它算进 status="degraded"，那时我们照样拦得住。
    printf '%s' '{"status":"ok","checks":{"db":true,"data_dir":true,"processors":true,"config":true,"future_unknown_check":false}}' > "$WORK/stub_extra_key.json"
    if run_stub "$WORK/stub_extra_key.json"; then
        rc=0
        PORT="$HC_PORT" sh healthcheck.sh >"$WORK/hc.out" 2>"$WORK/hc.err" || rc=$?
        stop_stub
        if [ "$rc" -ne 0 ]; then
            ko "多出一个未知且为 false 的 checks 键就把容器判成 unhealthy（升级后端即误报）：$(head -c 200 "$WORK/hc.err")"
        elif LC_ALL=C grep -q "future_unknown_check" "$WORK/hc.err"; then
            ok "未知 checks 键不参与判定（exit 0），但仍在 stderr 留下[提示]线索"
        else
            ko "未知 checks 键没判死是对的，但 stderr 没留下任何线索（排查会瞎）"
        fi
    else
        ko "桩服务启动失败（端口 $HC_PORT 被占？），第八个行为测试跳过"
    fi
else
    printf '  [SKIP] 无 python3，跳过 5b 行为测试\n'
fi

# ---- 5c. 静态断言：healthcheck.sh 不能被改回"只看 HTTP 200" ----
if [ -f healthcheck.sh ]; then
    if grep -q '"ok"' healthcheck.sh; then
        ok "healthcheck.sh 里存在 status == \"ok\" 判据"
    else
        ko "healthcheck.sh 里找不到 status == \"ok\" 判据（可能被改回只看 HTTP 200）"
    fi
    if grep -q "checks" healthcheck.sh; then
        ok "healthcheck.sh 会检查 checks 明细"
    else
        ko "healthcheck.sh 不再检查 checks 明细"
    fi
    if grep -qE '^[[:space:]]*exec +(python3|python) +-c ' healthcheck.sh; then
        ok "healthcheck.sh 用镜像自带 python 解析（python:3.11-slim 没有 curl/wget）"
    else
        ko "healthcheck.sh 没有 exec python3/python -c 解析响应（可能被改回只看 HTTP 200）"
    fi
    # 只看**代码行**：注释里提到 curl（"本镜像没有 curl/wget"）不算依赖
    if grep -v '^[[:space:]]*#' healthcheck.sh | grep -q 'curl'; then
        ko "healthcheck.sh 依赖 curl（python:3.11-slim 里没有 curl）"
    else
        ok "未依赖 curl（解析用 python 标准库；wget 仅是无 python 时的降级回退）"
    fi
    # 四项 checks **全部必需**，且不得再有"宽容降级"分支（见 PROCESSORS §9）
    HC_CODE="$(grep -v '^[[:space:]]*#' healthcheck.sh)"
    if printf '%s' "$HC_CODE" | grep -q '"processors", "config"'; then
        ok "healthcheck.sh 把 processors/config 列为必需键（四项全必需）"
    else
        ko "healthcheck.sh 没有把 processors/config 列入必需键（缺键应判 unhealthy）"
    fi
    if printf '%s' "$HC_CODE" | grep -q 'optional'; then
        ko "healthcheck.sh 仍有 optional 宽容分支（后端已落地，缺键必须算异常）"
    else
        ok "healthcheck.sh 已无 optional 宽容分支（缺键 = unhealthy）"
    fi
fi

note "6. docker compose config 插值 + 渲染事实"
if ! command -v docker >/dev/null 2>&1; then
    printf '  [SKIP] 无 docker，跳过 6~7\n'
else
    grep -vE '^[[:space:]]*(#|$)' .env.example > .env 2>/dev/null || : > .env

    if HOST_PATH_MAP='__LITERAL_JSON__' docker compose -f docker-compose.yml config --format json > rendered.json 2>err.txt; then
        ok "compose config 成功插值、YAML 合法"
    else
        ko "compose config 失败：$(cat err.txt)"
    fi

    if python3 - "$WORK" <<'PYEOF'
import json, os, sys
work = sys.argv[1]
d = json.load(open(os.path.join(work, "rendered.json")))
s = d["services"]["media-ingest"]
PASS = FAIL = 0
def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1; print("  [PASS] " + label)
    else:
        FAIL += 1; print("  [FAIL] " + label)

check(s.get("image") == "media-ingest:latest", "image = media-ingest:latest")
check(s.get("container_name") == "media-ingest", "container_name = media-ingest")
check(s.get("restart") == "unless-stopped", "restart = unless-stopped")

ports = s.get("ports") or []
p = ports[0] if ports else {}
check(p.get("target") == 18081 and str(p.get("published")) == "18081", "ports 默认 18081:18081 -> %r" % (ports,))

vols = {v.get("target"): v for v in (s.get("volumes") or [])}
# **只有两条**卷，且媒体卷必须是**单一共同父挂载**：
#   1) ${MEDIA_ROOT}:${MEDIA_ROOT} —— 源与目标都落在它下面。内核 fs/namei.c 的
#      do_linkat() 硬性要求 old_path.mnt == new_path.mnt，否则 -EXDEV；
#      **st_dev/fsid/是否同一子卷都不构成判据**（同 st_dev、同 fsid、同 subvol，
#      两个独立 bind 之间 link() 照样 EXDEV）。容器路径 = 宿主路径 ⇒ HOST_PATH_MAP 恒等。
#   2) ./data:/data ——（SQLite / 日志 / secret.key / config）。
check(len(s.get("volumes") or []) == 2,
      "volumes 恰好两条（单一媒体共同父挂载 + /data，实际 %d 条）"
      % len(s.get("volumes") or []))
media = [v for v in (s.get("volumes") or []) if v.get("target") != "/data"]
check(len(media) == 1, "媒体共同父挂载恰好一条（实际 %d 条）" % len(media))
if len(media) == 1:
    mv = media[0]
    check(str(mv.get("source")) == str(mv.get("target")),
          "媒体挂载容器路径 = 宿主路径（source=%s target=%s）"
          % (mv.get("source"), mv.get("target")))
    check(not mv.get("read_only"),
          "媒体挂载可写（只读挂载无法创建硬链接）")
    check(str(mv.get("target", "")).startswith("/"),
          "媒体挂载目标是绝对路径 -> %r" % (mv.get("target"),))
# 反向断言：两条独立 bind 不得再现（它们正是 EXDEV 的根因）
for gone in ("/downloads", "/library"):
    check(gone not in vols,
          "volumes 不再有独立挂载点 %s（跨挂载 link() 必 EXDEV）" % gone)
data = vols.get("/data")
check(data is not None, "volumes 含 /data（数据卷）")
if data is not None:
    check(not data.get("read_only"), "volumes /data 可写（SQLite/日志需要写入）")
    check(str(data.get("source", "")).endswith("/data"),
          "数据卷默认落在 compose 文件同目录的 ./data（source=%r，免配置、不污染根目录）"
          % (data.get("source"),))

env = s.get("environment") or {}
# 运行身份默认 auto：容器按媒体挂载属主自动识别，用户不必再去宿主查 id。
for k, want in (("PUID", "auto"), ("PGID", "auto"), ("TZ", "Asia/Shanghai"),
                ("DATA_DIR", "/data"), ("PORT", "18081"),
                ("CONFIG_DIR", "/data/config"),
                ("LANG", "C.UTF-8"), ("PYTHONUTF8", "1"), ("PYTHONIOENCODING", "utf-8")):
    check(env.get(k) == want, "environment %s = %r" % (k, want))
# MEDIA_ROOT 必须与媒体卷挂载点一致（容器路径 = 宿主路径；它也用于 HOST_PATH_MAP 恒等）
check(str(env.get("MEDIA_ROOT", "")).startswith("/"),
      "environment MEDIA_ROOT 是绝对路径 -> %r" % (env.get("MEDIA_ROOT"),))
if len(media) == 1:
    check(str(env.get("MEDIA_ROOT")) == str(media[0].get("target")),
          "environment MEDIA_ROOT == 媒体卷挂载点（%r vs %r）"
          % (env.get("MEDIA_ROOT"), media[0].get("target")))
# v2 删除了「源/目标根」设置项，也删掉了由 MEDIA_ROOT 自动派生：这两项连 env 都不该出现。
check("DEFAULT_SOURCE_ROOT" not in env and "DEFAULT_TARGET_ROOT" not in env,
      "源码 compose 不再传 DEFAULT_SOURCE_ROOT/DEFAULT_TARGET_ROOT（v2 已删除这两个设置项）")
# 反向断言：CONFIG_SYNC_INTERVAL_SECONDS **不得**作为 env 出现。
# 应用只读 DB 设置键 `config_sync_interval_seconds`（app/configstore.py:623、
# app/processors/registry.py:402），留一个同名 env 会让人以为改了周期而实际不生效。
check("CONFIG_SYNC_INTERVAL_SECONDS" not in env,
      "environment 不得有 CONFIG_SYNC_INTERVAL_SECONDS（同步周期唯一真源是 DB 设置键，env 不生效）")

check("HEALTHCHECK" not in (d.get("services", {}).get("media-ingest", {}) or {}),
      "compose 未覆盖镜像内 HEALTHCHECK（该段已刻意移除，让镜像里那份生效）")
print("  ---- compose 事实小计：%d PASS / %d FAIL ----" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
PYEOF
    then
        ok "compose 渲染后的事实清单（镜像名/容器名/端口/volumes+ro/environment）"
    else
        ko "compose 渲染后的事实清单与预期不符（见上）"
    fi

    # 反向断言：compose 的 HOST_PATH_MAP **默认值**不得含 /downloads（镜像侧由第 8 节钉）。
    # 见第 8 节那条断言：那样的映射会把真实宿主路径改写成不存在的容器路径。
    if grep -E '^[[:space:]]*HOST_PATH_MAP:' docker-compose.yml | LC_ALL=C grep -q '/downloads'; then
        ko "docker-compose.yml 的 HOST_PATH_MAP 默认值又指向 /downloads（真实宿主路径会被改写成不存在的前缀）"
    else
        ok "docker-compose.yml 的 HOST_PATH_MAP 默认值不含 /downloads"
    fi

    note "6b. 镜像部署 compose（docker-compose.release.yml：预构建镜像路径）"
    if [ ! -f docker-compose.release.yml ]; then
        printf '  [SKIP] 无 docker-compose.release.yml\n'
    elif docker compose -f docker-compose.release.yml config --format json > rendered-rel.json 2>err-rel.txt; then
        if python3 - "$WORK" <<'PYEOF'
import json, os, sys
work = sys.argv[1]
d = json.load(open(os.path.join(work, "rendered-rel.json")))
s = d["services"]["media-ingest"]
PASS = FAIL = 0
def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1; print("  [PASS] " + label)
    else:
        FAIL += 1; print("  [FAIL] " + label)

check(str(s.get("image")) == "ghcr.io/taro-clo/media-ingest:latest",
      "release 镜像默认跟随最新发布 ghcr.io/taro-clo/media-ingest:latest（实际 %r）" % (s.get("image"),))
check("build" not in s, "release compose 不含 build（纯拉取，无需源码/本地构建）")
check(s.get("container_name") == "media-ingest", "container_name = media-ingest")
check("env_file" not in s, "release compose 不依赖 .env（环境变量全部在 environment 里自带默认）")
ports = s.get("ports") or []
p = ports[0] if ports else {}
check(p.get("target") == 18081 and str(p.get("published")) == "18081",
      "release ports 默认 18081:18081 -> %r" % (ports,))
vols = {v.get("target"): v for v in (s.get("volumes") or [])}
check(len(vols) == 2, "release volumes 恰好两条（媒体共同父挂载 + /data，实际 %d）" % len(vols))
media = [v for v in (s.get("volumes") or []) if v.get("target") != "/data"]
check(len(media) == 1, "release 媒体共同父挂载恰好一条")
if len(media) == 1:
    check(str(media[0].get("source")) == str(media[0].get("target")),
          "release 媒体挂载容器路径 = 宿主路径（source=%s target=%s）"
          % (media[0].get("source"), media[0].get("target")))
data = vols.get("/data")
check(data is not None, "release volumes 含 /data")
if data is not None:
    check(str(data.get("source", "")).endswith("/data"),
          "release 数据卷默认 ./data（source=%r）" % (data.get("source"),))
env = s.get("environment") or {}
for k, want in (("PUID", "auto"), ("PGID", "auto"), ("DATA_DIR", "/data"),
                ("CONFIG_DIR", "/data/config"), ("PORT", "18081")):
    check(env.get(k) == want, "release environment %s = %r" % (k, want))
check("HOST_PATH_MAP" not in env,
      "release 不声明 HOST_PATH_MAP（单挂载下空映射=恒等，由应用默认值提供）")
media_root = str(env.get("MEDIA_ROOT") or "")
check(media_root.startswith("/"), "release MEDIA_ROOT 是绝对路径 -> %r" % (media_root,))
check("DEFAULT_SOURCE_ROOT" not in env and "DEFAULT_TARGET_ROOT" not in env,
      "release 不显式传 DEFAULT_SOURCE_ROOT/DEFAULT_TARGET_ROOT（v2 已彻底删除这两个设置项）")
print("  ---- release compose 事实小计：%d PASS / %d FAIL ----" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
PYEOF
        then
            ok "release compose 渲染后的事实清单（纯拉取/端口/双卷/auto 身份/派生根）"
        else
            ko "release compose 渲染后的事实清单与预期不符（见上）"
        fi
    else
        ko "docker-compose.release.yml 渲染失败：$(cat err-rel.txt)"
    fi

    # 单点配置护栏：release compose 必须用 YAML 锚点定义挂载/端口，environment/ports/volumes 全部引用锚点。
    anchor_fail=""
    for pat in '^x-media-root: &media-root' '^x-data-dir: &data-dir' '^x-port: &port'; do
        LC_ALL=C grep -qE -- "$pat" docker-compose.release.yml || anchor_fail="$anchor_fail 缺少锚点定义($pat)"
    done
    for pat in '\*media-root' '\*data-dir' '\*port'; do
        case "$pat" in
            '\*data-dir') need=1 ;;   # 只被 volumes.source 一处引用（target 固定 /data）
            *) need=2 ;;
        esac
        n=$(LC_ALL=C grep -c -- "$pat" docker-compose.release.yml 2>/dev/null || true)
        [ "${n:-0}" -ge "$need" ] || anchor_fail="$anchor_fail 锚点 $pat 引用不足(${n:-0}<${need})"
    done
    if [ -n "$anchor_fail" ]; then
        ko "release compose 未做到锚点单点配置：$anchor_fail"
    else
        ok "release compose 用 YAML 锚点单点定义媒体挂载/数据目录/端口（改一行即全部生效）"
    fi
    if LC_ALL=C grep -q 'DEFAULT_SOURCE_ROOT\|DEFAULT_TARGET_ROOT' docker-compose.release.yml; then
        ko "release compose 又出现 DEFAULT_SOURCE_ROOT/DEFAULT_TARGET_ROOT（v2 已彻底删除这两个设置项）"
    else
        ok "release compose 不显式传 DEFAULT_*（运行期从 MEDIA_ROOT 派生）"
    fi

    note "6c. 一键安装脚本已取消（回归护栏：安装方式 = 手动三步）"
    onecmd_hits=""
    for f in docker-compose.release.yml "$REPO/../README.md" "$REPO/docs/guides/DEPLOYMENT.md"; do
        [ -f "$f" ] || continue
        if LC_ALL=C grep -q 'deploy\.sh' "$f"; then onecmd_hits="$onecmd_hits $f"; fi
    done
    if [ -n "$onecmd_hits" ]; then
        ko "以下文件又引用了已取消的一键脚本 deploy.sh：$onecmd_hits"
    else
        ok "发布 compose 与安装文档不再引用一键脚本（用户侧是手动三步）"
    fi

    # 回归护栏：安装资产里的 compose 下载地址不得再钉死版本 tag（统一从 main 分支取最新）。
    pin_hits=""
    for f in docker-compose.release.yml "$REPO/../README.md" "$REPO/docs/guides/DEPLOYMENT.md"; do
        [ -f "$f" ] || continue
        if LC_ALL=C grep -q 'raw.githubusercontent.com/Taro-clo/media-ingest/v[0-9]' "$f"; then
            pin_hits="$pin_hits $f"
        fi
    done
    if [ -n "$pin_hits" ]; then
        ko "以下文件的 compose 下载地址又钉死了版本 tag（应改用 main 分支取最新）：$pin_hits"
    else
        ok "发布 compose 与安装文档的下载地址指向 main 分支（不钉死版本）"
    fi


    PORT=18099 docker compose -f docker-compose.yml config --format json 2>/dev/null > rendered99.json
    if python3 - "$WORK" <<'PYEOF'
import json, os, sys
d = json.load(open(os.path.join(sys.argv[1], "rendered99.json")))
s = d["services"]["media-ingest"]
p = (s.get("ports") or [{}])[0]
env = s.get("environment") or {}
okp = p.get("target") == 18099 and str(p.get("published")) == "18099"
oke = env.get("PORT") == "18099"
print("  [%s] 宿主发布端口随之变为 18099（实际 %r）" % ("PASS" if okp else "FAIL", p))
print("  [%s] 容器内 PORT 随之变为 18099（实际 %r）" % ("PASS" if oke else "FAIL", env.get("PORT")))
sys.exit(0 if (okp and oke) else 1)
PYEOF
    then
        ok "改 PORT 时宿主端口与容器端口同步（healthcheck 才不会打错端口）"
    else
        ko "端口未贯穿：改 PORT 后宿主/容器端口不一致"
    fi

    if [ -s err.txt ]; then ko "compose config stderr 非空：$(cat err.txt)"; else ok "compose config 无 stderr 警告"; fi
fi

note "8. Dockerfile 断言"
if python3 - "$WORK" <<'PYEOF'
import os, re, sys
src = open(os.path.join(sys.argv[1], "Dockerfile"), encoding="utf-8").read()
# 注释剥离副本：凡是"某关键字不得出现"这类**否定式**断言，一律查这个副本。
# 理由：Dockerfile 里有意写了不少解释性注释，例如"PyYAML 有 wheel，
# 所以不需要 gcc"。若拿全文匹配，这些注释会把正确的文件判成 FAIL —— 是断言写错了，不是文件错了。
code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
PASS = FAIL = 0
def check(cond, label):
    global PASS, FAIL
    if cond: PASS += 1; print("  [PASS] " + label)
    else: FAIL += 1; print("  [FAIL] " + label)

check(bool(re.search(r'^FROM python:3\.11-slim$', src, re.M)), "FROM python:3.11-slim")
check('gosu' in src, "gosu 已安装（entrypoint 降权依赖它）")
check(bool(re.search(r'HEALTHCHECK[^\n]*\\\s*\n\s*CMD \["/bin/sh", "/usr/local/bin/mlm-healthcheck"\]', src)),
      "HEALTHCHECK 指向 mlm-healthcheck（/usr/local/bin）")
check(bool(re.search(r'^ENTRYPOINT \["/usr/local/bin/mlm-entrypoint"\]$', src, re.M)),
      "ENTRYPOINT 指向 mlm-entrypoint（/usr/local/bin）")
check(bool(re.search(r'^CMD \["sh", "-c", "exec uvicorn', src, re.M)),
      "CMD 用 sh -c 以展开 ${PORT}（exec 形式不会展开环境变量）")
check('--workers 1' in src, "CMD 固定 --workers 1（单 worker 才能保证任务串行）")
check(bool(re.search(r'COPY app/ /srv/mlm/app/', src)), "COPY app/ → /srv/mlm/app")
check(bool(re.search(r'COPY web/ /srv/mlm/web/', src)), "COPY web/ → /srv/mlm/web")
check(bool(re.search(r'COPY docker/entrypoint\.sh', src)), "COPY entrypoint.sh")
check(bool(re.search(r'COPY docker/healthcheck\.sh', src)), "COPY healthcheck.sh")
check(not re.search(r'^CMD \["uvicorn"[^\n]*"--port",\s*"18081"', src, re.M), "CMD 未把端口写死为 exec 形式的 18081")
check(not re.search(r'^USER ', src, re.M), "无 USER 指令（保留 root 启动、由 entrypoint 降权）")
env_blocks = re.findall(r'^ENV (.+?)(?=\n[A-Z#]|\Z)', src, re.M | re.S)
port_decls = sum(len(re.findall(r'(?<![:\-\w])PORT=18081', b)) for b in env_blocks)
check(port_decls == 1, "ENV 中 PORT=18081 默认值只声明 %d 次（应为 1）" % port_decls)
check('--port \\"${PORT:-18081}\\"' in src, "CMD 的 --port 取自 ${PORT:-18081}")

check('PyYAML==6.0.3' in src,
      "pip 依赖含 PyYAML==6.0.3（配置文件是 YAML，PROCESSORS.md §6.1）")
check('PyYAML==6.0.2' not in src,
      "不得再 pin PyYAML==6.0.2（新 CPython 上无 wheel，pip 会回落源码构建而失败）")
check(not re.search(r'\b(gcc|python3-dev|build-essential)\b', code),
      "未引入 gcc/python3-dev 等构建工具链（PyYAML 有 wheel，不需要从源码编译）")
check('CONFIG_DIR=/data/config' in code, "ENV 声明 CONFIG_DIR=/data/config")
check('CONFIG_SYNC_INTERVAL_SECONDS' not in code,
      "Dockerfile 不得声明 CONFIG_SYNC_INTERVAL_SECONDS（应用不读该 env，周期真源是 DB 设置键）")
# 镜像层不得内置非空 HOST_PATH_MAP。
# 反例：Dockerfile 里若写死 HOST_PATH_MAP={"/downloads":"/vol1/1000/Download/qbittorrent",…}，
# 而容器路径 = 宿主路径、容器里没有 /downloads；app/paths.py::to_container() 的
# 「宿主前缀反查」会把用户填的 /vol1/1000/Download/qbittorrent/Bangumi 改写成 /downloads/Bangumi，
# 随即变成容器里不存在的路径（v2 已删掉当年的 assert_allowed() 白名单；现在会在建库/校验时报「源目录不存在」）。
# 只有落在映射值前缀下的路径才中招 —— fixture 放别处时整套验收照样全绿，所以必须在结构上钉死。
map_values = re.findall(r'HOST_PATH_MAP\s*=\s*(\S+)', code)
check(all(v.strip("\"'") in ("{}", "") for v in map_values),
      "Dockerfile 未内置非空 HOST_PATH_MAP（默认必须是空表 = 恒等映射；实际声明 %r）"
      % (map_values,))
print("  ---- Dockerfile 小计：%d PASS / %d FAIL ----" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
PYEOF
then ok "Dockerfile 关键断言全部通过"; else ko "Dockerfile 关键断言有失败项（见上）"; fi

note "9. .dockerignore：build context 隔离（防 .env / data / secret.key 进镜像层）"
# 语义已用**真实 docker build**确认（把一个只有 COPY 的 inspection 镜像跑起来、
# 列出 /ctx 下的文件）：目录被排除时其内容一并被排除，`.git`（不带尾斜杠）排除的是整个
# .git/。所以下面的 match() 会逐级判祖先目录，避免"结构其实是好的、自验却报 FAIL"。
cat > "$WORK/match.py" <<'PYEOF'
import os, re, sys
work = sys.argv[1]
lines = [l.strip() for l in open(os.path.join(work, ".dockerignore"), encoding="utf-8").read().splitlines()]
pats = [l for l in lines if l and not l.startswith("#")]

def _rx(pat):
    """把单条 .dockerignore 模式编译成锚定整串的正则。

    关键：判断"这条规则是否锚定到 context 根"要看**去掉尾斜杠之前**的形态。
    `data/` 是根锚定的（只排除顶层 data/）；`**/__pycache__/` 里的 `**`
    已经表达了"任意层级"，它同样**不能**被当成浮动模式 —— 否则会退化成
    `(?:.*/)?__pycache__` 并命中 `app/processors` 这类完全无关的目录，
    让自验误报"COPY 源被排除"（会踩到这个假阳性）。
    """
    rooted = "/" in pat.strip("/") or pat.rstrip() != pat.strip("/")
    if pat.rstrip().endswith("/**"):
        rooted = "/" in pat.strip("/")
    pat = pat.strip("/")
    # 先把 **/ 与 ** 换掉，再换 *（否则 * 会先吃掉 ** 的一半）
    src = re.escape(pat)
    src = src.replace(r"\*\*/", "\x00")   # **/   → 任意层级（含零层）
    src = src.replace(r"\*\*", "\x01")    # **    → 任意字符
    src = src.replace(r"\*", "[^/]*")     # *     → 单层内任意字符
    src = src.replace("\x00", "(?:.*/)?")
    src = src.replace("\x01", ".*")
    if not rooted:
        # 不含 / 的模式匹配任意层级下的同名项
        return re.compile("^(?:.*/)?" + src + "$")
    return re.compile("^" + src + "$")

_RX = [(raw.startswith("!"), _rx(raw[1:] if raw.startswith("!") else raw)) for raw in pats]


def match(path):
    """按 .dockerignore 的**实际**语义判定 path 是否被排除。

    目录规则（`foo`、`foo/`、`foo/**`）作用于"该目录及其下全部内容"，而路径一旦被
    排除，**任何**模式都匹配不到它下面的文件（Docker 的 exclude 是不可穿透的）。
    实现上等价于：先取 path 的每一级祖先，任何一个祖先命中规则 ⇒ 整条被排除。
    因此这里**不需要**再给规则加 `(?:/.*)?` 后缀 —— 加了就会让
    `**/__pycache__/` 反过来命中 `app/processors` 这种不含 __pycache__
    的目录，误报"COPY 源被排除"（会踩到）。
    """
    parts = path.strip("/").split("/")
    cands = ["/".join(parts[:i + 1]) for i in range(len(parts))]
    ignored = False
    for neg, rx in _RX:
        if any(rx.match(c) for c in cands):
            ignored = not neg
    return ignored
PYEOF
if [ ! -f "$WORK/.dockerignore" ]; then
    ko "仓库根缺少 .dockerignore（build context 会把 .git/、data/、.env 一并上传）"
else
    ok ".dockerignore 存在"
    # argv[1] = $WORK（.dockerignore 与 Dockerfile 的副本，供规则判定）
    # argv[2] = $REPO（真实仓库根，供"COPY 源是否真的存在"判定）
    # 两者必须分开：$WORK 只是临时目录，里面**没有** app/ 与 web/ 子树，
    # 拿它验存在性会把每条 COPY 都报成"不存在"（会造成假 FAIL）。
    if python3 - "$WORK" "$REPO" <<'PYEOF'
import os, sys
exec(open(os.path.join(sys.argv[1], "match.py"), encoding="utf-8").read())
FAIL = 0
# Dockerfile 实际 COPY 的源路径：一条都不许被忽略（共 4 条 COPY，逐个源）
must_keep = ["app", "app/main.py", "app/config.py", "app/routers/system.py",
             # 硬链接与重命名的原生实现在这里
             "app/processors/hardlink.py", "app/processors/engine.py",
             "web", "web/index.html", "web/js/app.js", "web/css/app.css",
             "docker", "docker/Dockerfile", "docker/entrypoint.sh", "docker/healthcheck.sh"]
for path in must_keep:
    if match(path):
        print("  [FAIL] COPY 源被 .dockerignore 排除了：%s" % path); FAIL += 1
if FAIL == 0:
    print("  [PASS] Dockerfile 全部 COPY 源（app/ web/ docker/）均未被忽略")

# 必须被忽略的机密 / 状态 / 噪音（防止未来 COPY . /app 把密钥烙进镜像层）
# 语义已用真实 docker build 确认（见日志里 "transferring context" 与镜像内文件清单）。
must_ignore = [".git/config", ".git/objects/ab/cdef", ".env", ".env.local", ".env.production",
               "data/mlm.db", "data/secret.key", "data/logs/hard_links.log",
               "secret.key", "docs/contracts/ARCHITECTURE.md", "docs/contracts/PROCESSORS.md",
               "tests", "tests/test_api.py",
               "examples/x.sh", "examples/validate-deployment-assets.sh",
               "README.md", "CHANGELOG.md",
               "docker-compose.yml", "probe_equivalence.py",
               "app/__pycache__/x.pyc", "web/__pycache__/y.pyc",
               "app/processors/__pycache__/x.pyc",
               "app/_selftest.py", "web/_check.py", "web/_classcheck.py", "web/_smoke.html",
               ".probe/x", ".tmp/x"]
miss = [p for p in must_ignore if not match(p)]
if miss:
    print("  [FAIL] 以下路径本应被忽略却仍会进 build context：%s" % ", ".join(miss)); FAIL += 1
else:
    print("  [PASS] %d 个机密/状态/噪音路径全部被排除（.env、data/、.git/、docs/、tests/ 等）" % len(must_ignore))

# .env.example 必须保留（compose 文档与自验要用；它不含真机密）
if match(".env.example"):
    print("  [FAIL] .env.example 被忽略了（它是模板，应保留）"); FAIL += 1
else:
    print("  [PASS] .env.example 未被忽略（模板文件保留，! 反向规则生效）")

# ---- Dockerfile 的每一条 COPY 源都必须在仓库里真实存在 ----------------------
# 这一条防的是"规则修好了、但 COPY 指向一个根本不存在的路径"这种假绿：
# 上面 must_keep 只证明"该路径没被 ignore"，这条证明"该路径真的在 context 里"。
# ⚠️ 解析陷阱：COPY 的源路径是相对**构建上下文根**（= compose 里 build.context、
#    也就是仓库根）解析的，本仓库的 Dockerfile 位于 docker/ 子目录，所以既不能
#    相对 docker/ 解析，也不能拿只放了几个副本文件的 $WORK 去解析 —— 两种写法都会
#    把每一条 COPY 报成"不存在"（两个假 FAIL 都会踩到）。
repo_root = os.path.abspath(sys.argv[2])
src = open(os.path.join(sys.argv[1], "Dockerfile"), encoding="utf-8").read()
src = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
copies = re.findall(r'^\s*COPY\s+(\S+)\s+\S+\s*$', src, re.M)
bad = []
for c in copies:
    if c.startswith("--"):          # 形如 COPY --chown=… 的选项，跳过
        continue
    if c.rstrip("/") in ("app", "web"):   # 目录 COPY：目录存在即可
        continue
    if not os.path.exists(os.path.join(repo_root, c)):
        bad.append(c)
if not copies:
    print("  [FAIL] 未从 Dockerfile 里解析出任何 COPY 指令（解析式可能已过时）"); FAIL += 1
elif bad:
    print("  [FAIL] COPY 源在仓库里不存在（构建必失败）：%s" % ", ".join(bad)); FAIL += 1
else:
    print("  [PASS] Dockerfile 的 %d 条 COPY 源在 context 中都真实存在：%s"
          % (len(copies), ", ".join(copies)))
sys.exit(0 if FAIL == 0 else 1)
PYEOF
    then ok ".dockerignore 规则核对通过（COPY 源未被误伤、机密与噪音已排除）"
    else ko ".dockerignore 规则有问题（见上）"; fi
fi

note "10. docker build --check 干跑（可选；不写镜像层、可跳过的额外保险）"
if [ "${MLM_BUILD_CHECK:-0}" = "1" ] && command -v docker >/dev/null 2>&1; then
    if docker build --check -f docker/Dockerfile "$WORK" 2>&1 | tail -20; then
        ok "docker build --check 未报 COPY/上下文问题"
    else
        ko "docker build --check 报出问题（见上）"
    fi
else
    printf '  [SKIP] 需显式 MLM_BUILD_CHECK=1 才跑（会读 build context，但 --check 不产出镜像层）\n'
fi

note "11. 回调命令与契约一致（kind 不参与标准命令；%K 不得被当任务类型）"
PARAMS="name hash category tags content_path save_path root_path"
if [ -f qbittorrent-autorun.md ]; then
    rc=0
    python3 - "$WORK" "$PARAMS" <<'PYEOF' 2>&1 || rc=$?
import os, re, sys
work, params = sys.argv[1], sys.argv[2].split()
md = open(os.path.join(work, "qbittorrent-autorun.md"), encoding="utf-8").read()
FAIL = 0
# 文档里必须真的列出 7 个 --data-urlencode（复制粘贴用的那条命令）
for p in params:
    if ('--data-urlencode "%s=%%' % p) not in md:
        print("  [FAIL] 文档命令缺参数 --data-urlencode \"%s=%%\"（占位符形式）" % p); FAIL += 1
if FAIL == 0:
    print("  [PASS] 文档里的回调命令含全部 7 个 --data-urlencode 参数（%s）" % ", ".join(params))
# token 必须在 URL query 上
if "?token=" in md:
    print("  [PASS] token 放在 URL query（?token=...），不需要 Cookie/登录")
else:
    print("  [FAIL] 文档里找不到 ?token= 形式"); FAIL += 1
# %K 是 Torrent ID，绝不能被当 kind 传（只查**传参形式**，正文里解释"不要写 kind=%K"是允许的）
if not re.search(r'--data-urlencode\s+"kind=%K"', md):
    print("  [PASS] 命令里没有 --data-urlencode \"kind=%K\"（%K 是 Torrent ID，不是任务类型）")
else:
    print("  [FAIL] 命令里出现了 --data-urlencode \"kind=%K\" —— %K 是 Torrent ID，会被白名单忽略且误导用户"); FAIL += 1
sys.exit(0 if FAIL == 0 else 1)
PYEOF
    if [ "$rc" -eq 0 ]; then
        ok "autorun 文档的回调命令与 ARCHITECTURE §8 契约一致"
    else
        ko "autorun 文档的回调命令与契约不一致（见上）"
    fi

    # WebUI 生成的那条命令（设置页「复制回调命令」）不得与文档漂移
    if [ -f settings/settings.js ]; then
        rc=0
        python3 - "$WORK" "$PARAMS" <<'PYEOF' 2>&1 || rc=$?
import os, re, sys
work, params = sys.argv[1], sys.argv[2].split()
js = open(os.path.join(work, "settings", "settings.js"), encoding="utf-8").read()
FAIL = 0
for p in params:
    if ('--data-urlencode "%s=%%' % p) not in js:
        print("  [FAIL] 设置页生成的命令缺参数 %s" % p); FAIL += 1
if '--data-urlencode "kind=%K"' in js:
    print("  [FAIL] 设置页生成的命令仍带 kind=%K（应与文档/§8 对齐后删掉）"); FAIL += 1
if FAIL == 0:
    print("  [PASS] 设置页生成的命令同样含 7 个参数、且不含 kind=%K（无漂移）")
sys.exit(0 if FAIL == 0 else 1)
PYEOF
        if [ "$rc" -eq 0 ]; then
            ok "WebUI 设置页生成的命令与文档/契约一致"
        else
            ko "WebUI 设置页生成的命令与文档/契约不一致（见上）"
        fi
    else
        printf '  [SKIP] 未找到 web/js/pages/settings.js（前端不在本仓库？）\n'
    fi
else
    printf '  [SKIP] 未找到 examples/qbittorrent-autorun.md\n'
fi

note "12. examples 的 API 调用口径（字段名：processor_id 规范；permission_mode 是默认关闭的开关，示例不传）"
rc12=0
python3 - "$WORK" <<'PYEOF' 2>&1 || rc12=$?
import os, re, sys
work = sys.argv[1]
FAIL = 0

def load(name):
    p = os.path.join(work, name)
    if not os.path.isfile(p):
        return None
    return open(p, encoding="utf-8").read()

# minimal-repro.sh 是真正发请求的脚本：建库 JSON 必须是现行形状
mr = load("minimal-repro.sh")
if mr is None:
    print("  [SKIP] 未找到 examples/minimal-repro.sh")
else:
    if "processor_id" in mr:
        print("  [PASS] minimal-repro.sh 用 processor_id 建库（规范字段）")
    else:
        print("  [FAIL] minimal-repro.sh 没有 processor_id —— 建库请求必须指定处理器"); FAIL += 1
    if "permission_mode" not in mr:
        print("  [PASS] minimal-repro.sh 不传 permission_mode（它是默认关闭的开关，示例保持默认）")
    else:
        print("  [FAIL] minimal-repro.sh 传了 permission_mode（示例应保持默认关闭，别让读者以为必须开）"); FAIL += 1
    if re.search(r'"processor"\s*:', mr):
        print("  [FAIL] minimal-repro.sh 用了 \"processor\": 别名（能用但不该出现在示例里，请用 processor_id）"); FAIL += 1
    else:
        print("  [PASS] minimal-repro.sh 没有用 \"processor\": 别名")

# deploy-walkthrough.md 是给人抄的文档：只查**传参形式**；正文里提醒"该开关默认关闭、开启会改到源文件权限位"是允许的
nd = load("deploy-walkthrough.md")
if nd is None:
    print("  [SKIP] 未找到 examples/deploy-walkthrough.md")
else:
    if "processor_id" in nd:
        print("  [PASS] deploy-walkthrough.md 的建库示例用 processor_id")
    else:
        print("  [FAIL] deploy-walkthrough.md 的建库示例没有 processor_id"); FAIL += 1
    if re.search(r'"permission_mode"\s*:', nd):
        print("  [FAIL] deploy-walkthrough.md 的请求体里仍带 \"permission_mode\":（示例保持默认关闭，不要在可复制示例里出现）"); FAIL += 1
    else:
        print("  [PASS] deploy-walkthrough.md 的请求体里没有 permission_mode 字段（保持默认关闭）")

# 原生实现的入口必须**默认关闭**，否则「默认关」就只是文档口号
hl = load("app/processors/hardlink.py")
if hl is None:
    print("  [SKIP] 未找到 app/processors/hardlink.py")
else:
    if "permission_mode: bool = False" in hl:
        print("  [PASS] 原生实现的 permission_mode 默认关闭（permission_mode: bool = False）")
    else:
        print("  [FAIL] 原生实现的 permission_mode 不是默认关闭"); FAIL += 1
sys.exit(0 if FAIL == 0 else 1)
PYEOF
if [ "$rc12" -eq 0 ]; then
    ok "examples 里的 API 调用口径与现行字段一致"
else
    ko "examples 里的 API 调用口径与现行字段不一致（见上）"
fi

# ---- 13. 真实硬链接探针（部署拓扑是否允许 link()）--------------------------
note "13. 真实硬链接探针（源与目标必须在同一个挂载内）"
# 为什么要有这一节：只看 st_dev 相等判断"能否硬链接"，结果是**假绿** —— 同一个 btrfs
# 子卷下的两个独立 bind，st_dev、fsid、subvolid 三者可以完全相同，link() 照样 -EXDEV。
# 内核看的是**挂载身份**：fs/namei.c 的 do_linkat() 要求 old_path.mnt == new_path.mnt。
# 所以这里做**实证**：从源根有界递归挑一个已存在的普通文件，在目标根建临时硬链接再删。
# 全程**不写源目录**（只读源也能跑）；ctime 可能变，但 ino/size/mtime 必须回到原值。
if [ -z "${SRC_ROOT:-}" ] || [ -z "${TGT_ROOT:-}" ]; then
    printf '  [SKIP] 未指定源/目标根 —— 本节**必须显式**给出两个根才运行（故意不自动取 .env 的值，\n'
    printf '         免得在别人的机器上把 .mlm_probe_*.link 临时文件写进真实媒体库）：\n'
    printf '           SRC_ROOT=/vol1/1000/Download/qbittorrent TGT_ROOT=/vol1/1000/Library \\\n'
    printf '             sh examples/validate-deployment-assets.sh .\n'
else
    printf '  源根=%s\n  目标根=%s\n' "$SRC_ROOT" "$TGT_ROOT"
    rc13=0
    python3 - "$SRC_ROOT" "$TGT_ROOT" <<'PYEOF' 2>&1 || rc13=$?
import os, sys

src_root, tgt_root = sys.argv[1], sys.argv[2]
MAX_DEPTH, MAX_ENTRIES = 3, 512   # 媒体模型：源根/标题文件夹/季/文件 ⇒ 深度 3

def find_file(root):
    """有界递归找第一个可链的普通文件。只看一层会永远找不到（顶层全是文件夹）。"""
    seen = 0
    queue = [(root, 0)]
    while queue:
        d, depth = queue.pop(0)
        if depth > MAX_DEPTH:
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            seen += 1
            if seen > MAX_ENTRIES:
                return None, seen
            p = os.path.join(d, name)
            try:
                if os.path.islink(p):        # 不跟随符号链接
                    continue
                if os.path.isfile(p):
                    return p, seen
                if os.path.isdir(p):
                    queue.append((p, depth + 1))
            except OSError:
                continue
    return None, seen

if not os.path.isdir(src_root):
    print("  [SKIP] 源根不存在或不是目录：%s" % src_root); sys.exit(3)
if not os.path.isdir(tgt_root):
    print("  [SKIP] 目标根不存在或不是目录：%s" % tgt_root); sys.exit(3)

sample, seen = find_file(src_root)
if sample is None:
    print("  [SKIP] 源根下（深度 ≤%d、条目 ≤%d，已看 %d 条）找不到可链的普通文件 —— 无法实证"
          % (MAX_DEPTH, MAX_ENTRIES, seen))
    sys.exit(3)

st = os.stat(sample)
probe = os.path.join(tgt_root, ".mlm_probe_%d.link" % os.getpid())
try:
    os.link(sample, probe)
except OSError as e:
    print("  [FAIL] link() 失败：errno=%s %s" % (e.errno, e.strerror))
    print("         源  ：%s" % sample)
    print("         目标：%s" % probe)
    print("         **修法**：源与目标必须落在**同一个挂载**内 —— 在 docker-compose.yml 里")
    print("         用单一共同父挂载（容器路径 = 宿主路径）：")
    print('           - ${MEDIA_ROOT:-/vol1/1000}:${MEDIA_ROOT:-/vol1/1000}')
    print("         不要再分别挂 /downloads 与 /library：两个 bind 就是两个挂载，")
    print("         即便 st_dev/fsid/子卷完全相同，link() 也一律 EXDEV。")
    print("         改完执行：docker compose up -d --force-recreate")
    sys.exit(1)

st_link = os.stat(probe)
os.unlink(probe)
st_after = os.stat(sample)
same_object = (st_link.st_dev, st_link.st_ino) == (st.st_dev, st.st_ino)
source_intact = ((st_after.st_ino, st_after.st_size, st_after.st_mtime_ns)
                 == (st.st_ino, st.st_size, st.st_mtime_ns))
if not same_object:
    print("  [FAIL] 目标处建出的东西与源**不是同一个 inode**（复制而非硬链接？）"); sys.exit(1)
if not source_intact:
    print("  [FAIL] 探针改变了源文件（ino/size/mtime 有变化）"); sys.exit(1)
print("  [PASS] 实测 link() 成功：%s" % sample)
print("         → %s   st_dev=%s st_ino=%s" % (probe, st.st_dev, st.st_ino))
print("         源文件 ino/size/mtime 探测前后逐项不变；临时链接已删除")
sys.exit(0)
PYEOF
    if [ "$rc13" -eq 0 ]; then
        ok "真实硬链接探针通过：源与目标在同一挂载内，link() 实测成功"
    elif [ "$rc13" -eq 3 ]; then
        printf '  [SKIP] 无法实证（源/目标根不存在，或源里没有可链文件）—— 不计 PASS/FAIL\n'
    else
        ko "真实硬链接探针失败：源与目标跨挂载（link() 报 EXDEV），见上方的修法"
    fi
fi

printf '\n===== 汇总：%d PASS / %d FAIL =====\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
printf '静态自验全部通过。\n'
printf '仍需部署验收（见 docs/guides/DEPLOYMENT.md）：镜像构建、容器 (healthy)、真实硬链接建立。\n'
printf '参考值：python:3.11-slim 预先存在时 build 约 2~5 分钟，\n'
printf '  镜像 media-ingest:latest 约 180 MB（本脚本只做静态自验，不构建）。\n'
