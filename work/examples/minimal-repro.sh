#!/bin/sh
# =============================================================================
# minimal-repro.sh —— 最小可复现实例（在部署宿主上执行，需要 docker）
# =============================================================================
# 它证明的是**端到端**的一件事：
#   同一个源文件与它被创建到库里的文件共享同一个 inode —— 这是**真正的硬链接**，
#   不是复制。这是本项目唯一的"不做假"验收点。
#
# 流程：
#   建临时目录（源/库/数据三个，源与库在**同一个父目录**下） → 放两个样本文件
#     → 构建镜像并起容器（**两条挂载**：临时父目录 $WORK 原样挂成 $WORK、数据卷挂 /data；
#        源与目标因此同属一个挂载 —— 这是硬链接能成立的前提）
#     → 断言：媒体挂载与数据卷是两条独立挂载（挂载 ID 不同）
#     → curl 登录 → **新建一个自定义处理器**（核心：重命名规则由处理器驱动）
#       → 建媒体库并引用它 → 只读预检（真实 link 实测）→ 触发 run → 轮询任务到终态
#     → 先看任务 result（linked > 0 且 link_failed == 0），再 stat -c '%d %i %h'
#       比对源与目标的 (设备号, inode) 相同且链接数 ≥ 2
#     → 清理（除非 KEEP=1）
#
# 用法：
#     sh examples/minimal-repro.sh                 # 仓库根取当前目录
#     KEEP=1 sh examples/minimal-repro.sh          # 保留临时目录以便排查
#     PORT=18099 sh examples/minimal-repro.sh      # 换端口
#     sh examples/minimal-repro.sh /path/to/repo   # 显式指定仓库根（用作 build context）
#
# 前置条件：
#   1. 宿主已装 docker + compose，且当前用户能执行 docker（以 root 跑最省事）。
#   2. 仓库根下存在 docker/Dockerfile、app/、web/（完整源码树）。
#   3. 基础镜像 python:3.11-slim 已存在或可拉取（本地已缓存则构建不需联网拉基座）。
#   4. `stat`/`curl` 可用。
#
# ⚠️ 本脚本存在的意义之一：源与目标**必须落在同一个挂载里**，否则内核的 `link()` 一律
#   返回 `EXDEV`（`fs/namei.c` 的 `do_linkat()` 比较两次路径解析得到的**挂载身份**，与
#   `st_dev`/`fsid`/是否同一子卷都无关）。所以本脚本不把源/库挂成 `/downloads`、
#   `/library` 两条独立 bind，而是把**它们共同的父目录** $WORK 原样挂进容器（容器路径
#   = 宿主路径）—— 这正是仓库里 compose 的形态（`/vol1/1000:/vol1/1000` + `./data:/data`）。
#   若拆成两条独立 bind 一定会失败：任务 result 会是 `linked=0, link_failed=1`，
#   错误文本 `[Errno 18] Invalid cross-device link`。
#
# 关于运行期能力：
#   **应用自带全部能力：不需要宿主提供任何额外文件或目录。** 硬链接与重命名都是应用
#   自己的原生实现，所以本脚本在**任何**宿主上都能跑，也不会去碰与本应用无关的宿主目录。
# =============================================================================
set -eu

REPO_ROOT="${1:-$(pwd)}"
PORT="${PORT:-18081}"
KEEP="${KEEP:-0}"
IMAGE="${IMAGE:-media-ingest:latest}"
CNAME="media-ingest-minimal-repro"
# 关于路径：现行处理器不依赖任何硬编码的库根/日志目录（路径来自媒体库配置，日志走
# /data/logs），所以本脚本**不需要**为它们伪造绝对路径 —— 而且"单一共同父挂载"也不允许
# 再往容器里塞额外 bind（额外的挂载点与媒体挂载跨挂载 ⇒ link() EXDEV）。
# 若某条链路确实需要某个前缀，正确做法是让库的 source_dir/target_dir 落在那个前缀下，
# 而不是再加一条挂载。

WORK="$(pwd)/.repro"
SRC="$WORK/dl"
DST="$WORK/lib"
DATA="$WORK/data"
LOG="$WORK/repro.log"

say()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    [OK]   %s\n' "$*"; }
bad()  { printf '    [FAIL] %s\n' "$*" >&2; }
die()  { bad "$*"; exit 1; }

cleanup() {
    code=$?
    if [ "$code" -ne 0 ]; then
        printf '\n脚本中途失败（exit %s）。容器日志末尾：\n' "$code" >&2
        docker logs --tail 80 "$CNAME" >&2 2>/dev/null || true
        printf '\n完整构建/运行日志：%s\n' "$LOG" >&2
        printf '临时目录：%s\n' "$WORK" >&2
    fi
    docker rm -f "$CNAME" >/dev/null 2>&1 || true
    if [ "$KEEP" = "1" ]; then
        printf '\nKEEP=1：保留 %s 与 %s\n' "$WORK" "$LOG"
    else
        rm -rf "$WORK"
    fi
    exit "$code"
}
trap cleanup EXIT

# ---- 0. 前置检查 -----------------------------------------------------------
say "0. 前置检查"
[ -f "$REPO_ROOT/docker/Dockerfile" ] || die "在 $REPO_ROOT 找不到 docker/Dockerfile；请把仓库根作为第一个参数传入"
[ -d "$REPO_ROOT/app" ] || info "未找到 app/（镜像构建会因 COPY app/ 失败）"
command -v docker >/dev/null 2>&1 || die "找不到 docker 命令"
docker info >/dev/null 2>&1 || die "docker 不可用（守护进程未启动，或当前用户不在 docker 组）"
command -v curl >/dev/null 2>&1 || die "找不到 curl（本脚本用它调 API 并轮询）"
command -v stat >/dev/null 2>&1 || die "找不到 stat（本脚本用它比对 inode）"
docker rm -f "$CNAME" >/dev/null 2>&1 || true
# 注意：$LOG 在 $WORK/.repro 里，必须先建目录再落日志（目录不存在时 `: > "$LOG"`
# 会报 `cannot create …: Directory nonexistent`，脚本连第 0 步都过不去）。
mkdir -p "$WORK"
: > "$LOG"
ok "docker 可用；仓库根 = $REPO_ROOT；宿主端口 = $PORT"

# ---- 1. 建临时目录与样本 ---------------------------------------------------
say "1. 建临时源/库/数据目录并放入样本文件"
rm -rf "$WORK"
mkdir -p "$SRC/Bangumi/Show A/Season 1" "$DST/Bangumi" "$DATA"
: > "$LOG"
printf 'fake mkv video payload for repro\n'    > "$SRC/Bangumi/Show A/Season 1/[Lilith-Raws] Show A - 01.mkv"
printf 'fake ass subtitle payload for repro\n' > "$SRC/Bangumi/Show A/Season 1/Show.S01E01.chs.ass"
info "源目录结构（1:1 模拟 qBittorrent 产物）："
find "$SRC" -mindepth 1 | sed "s|$WORK|  .|"
ok "源 / 库 / 数据 三个目录就绪"

# ---- 2. 起容器（单一共同父挂载：源与目标同一挂载）-------------------------
say "2. 构建并启动容器（$CNAME）"
info "构建日志重定向到 $LOG"
docker build -f "$REPO_ROOT/docker/Dockerfile" -t "$IMAGE" "$REPO_ROOT" >>"$LOG" 2>&1 \
    || { bad "镜像构建失败"; tail -40 "$LOG" >&2; exit 1; }
# 预置管理员：非交互脚本必须能调 /api/libraries（它需要登录会话，不能匿名）。
# 该密码只用于**一次性临时容器**，容器用完即删（见 cleanup）。
ADMIN_USER="repro-admin"
ADMIN_PASS="repro-admin-pass"
# 容器挂载（与仓库里的 compose 完全一致）：
#   1) "$WORK:$WORK"  —— **单一共同父挂载**：源（$SRC）与目标（$DST）都在它下面，
#      且容器内路径 = 宿主路径（源/库的绝对路径在容器里逐字相同，无需任何换算）。
#   2) "$DATA:/data"  —— 应用数据卷（SQLite/日志/secret.key/config），必须与媒体挂载
#      分属两条独立挂载：否则 entrypoint 启动时那条 `chown -R /data` 会递归进媒体目录。
docker run -d --name "$CNAME" \
    -p "127.0.0.1:${PORT}:${PORT}" \
    -e PUID=1000 \
    -e PGID=1001 \
    -e TZ=Asia/Shanghai \
    -e DATA_DIR=/data \
    -e PORT="$PORT" \
    -e CONFIG_DIR=/data/config \
    -e MEDIA_ROOT="$WORK" \
    -e DEFAULT_SOURCE_ROOT="$SRC" \
    -e DEFAULT_TARGET_ROOT="$DST" \
    -e ADMIN_USERNAME="$ADMIN_USER" \
    -e ADMIN_PASSWORD="$ADMIN_PASS" \
    -v "$WORK:$WORK" \
    -v "$DATA:/data" \
    "$IMAGE" >>"$LOG" 2>&1 || { bad "docker run 失败"; exit 1; }
ok "容器已启动：$CNAME（共同父挂载 $WORK ⇒ 源与目标同一挂载）"

info "等待 /api/system/health 报 status=ok（最多 60s；注意该端点无论如何都返回 HTTP 200）"
i=0
READY=0
while [ "$i" -lt 60 ]; do
    HEALTH_RAW="$(curl -sS "http://127.0.0.1:${PORT}/api/system/health" 2>/dev/null || true)"
    case "$HEALTH_RAW" in
        *'"status":"ok"'*) READY=1; break ;;
    esac
    i=$((i + 1))
    sleep 1
done
if [ "$READY" -ne 1 ]; then
    bad "60 秒内健康检查未报 status=ok（最后响应：$HEALTH_RAW）"
    printf '    容器日志：\n' >&2
    docker logs --tail 40 "$CNAME" >&2 || true
    printf '\n    提示：健康检查的 checks 明细可直接看容器内探针输出：\n' >&2
    printf '      docker inspect --format "{{json .State.Health}}" %s\n' "$CNAME" >&2
    exit 1
fi
ok "健康检查通过（第 ${i} 秒，status=ok）"

# ---- 2b. 登录拿会话 Cookie（后续 /api/libraries 与 /api/tasks 都需要它）----
JAR="$WORK/cookies.txt"
LOGIN_RAW="$(curl -sS -X POST "http://127.0.0.1:${PORT}/api/auth/login" \
    -H 'Content-Type: application/json' \
    -c "$JAR" \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")" \
    || die "POST /api/auth/login 请求失败"
case "$LOGIN_RAW" in
    *'"username"'*) ok "登录成功（会话 Cookie 存于 $JAR）" ;;
    *) die "登录失败，响应：$LOGIN_RAW" ;;
esac

# 注意：`docker exec` 默认以 root 进容器，所以 `docker exec … id` 永远显示 uid=0，
# 拿它当"非 root"判据必然假 FAIL。要看**主进程**的身份，读 PID 1 的 /proc/1/status。
IDENT="$(docker exec "$CNAME" cat /proc/1/status 2>/dev/null | awk '/^Name:|^Uid:|^Gid:/' || true)"
info "容器 PID 1 身份（/proc/1/status）：$(printf '%s' "$IDENT" | tr '\n' ' ')"
case "$IDENT" in
    *"Name:	uvicorn"*|*"Name: uvicorn"*)
        case "$IDENT" in
            *"Uid:	1000"*|*"Uid: 1000"*)
                case "$IDENT" in
                    *"Gid:	1001"*|*"Gid: 1001"*) ok "以非 root（PID 1 = uvicorn，uid=1000 gid=1001）运行" ;;
                    *) die "PID 1 的 gid 不是 1001：$IDENT" ;;
                esac ;;
            *) die "PID 1 的 uid 不是 1000：$IDENT" ;;
        esac ;;
    *) die "PID 1 不是 uvicorn（可能已退出或换了启动方式）：$IDENT" ;;
esac

# 结构断言：媒体挂载（$WORK）与数据卷（/data）必须是**两条独立挂载**。
# 理由：entrypoint 启动时对 $DATA_DIR 做 `chown -R`；如果两者是同一个挂载，
# 那条 chown 会递归进媒体目录、改到真实媒体文件的属主。entrypoint 自己也会挡这一条
# （命中即拒绝启动），这里再验一次，是为了让"容器确实起来了"的同时**形态正确**。
MOUNTINFO="$(docker exec "$CNAME" sh -c "grep -E \" $WORK | /data \" /proc/self/mountinfo | cut -d' ' -f1,5" 2>/dev/null || true)"
MEDIA_MNT="$(printf '%s\n' "$MOUNTINFO" | awk -v p="$WORK" '$2 == p { print $1 }')"
DATA_MNT="$(printf '%s\n' "$MOUNTINFO" | awk '$2 == "/data" { print $1 }')"
[ -n "$MEDIA_MNT" ] || die "容器 mountinfo 里找不到媒体挂载 $WORK（挂载形态不对？）"
[ -n "$DATA_MNT" ] || die "容器 mountinfo 里找不到 /data 挂载（挂载形态不对？）"
[ "$MEDIA_MNT" != "$DATA_MNT" ] || die "媒体挂载与数据卷是**同一个挂载**（mount ID=$MEDIA_MNT）—— 启动时的 chown -R 会改到媒体文件属主，形态非法"
ok "两条挂载互相独立：媒体 $WORK（mount $MEDIA_MNT）/ 数据 /data（mount $DATA_MNT）"

# 实证：容器内真的能在源与目标之间建硬链接（这才是主判据 —— 挂载 ID 不同**不保证**
# link() 成功，例如 btrfs 同一挂载内跨子卷仍可能 EXDEV）。
# 探针只**读**源文件，只在库侧建一个临时名并立刻删除 ⇒ 源目录零写入。
LINKPROBE="$(docker exec "$CNAME" sh -c "
    f=\$(find '$SRC' -maxdepth 3 -type f -print -quit 2>/dev/null)
    [ -n \"\$f\" ] || { echo SKIP; exit 0; }
    w=\$(mktemp -d '$DST/.mlm-probe.XXXXXX') || { echo NODIR; exit 1; }
    if ln \"\$f\" \"\$w/p.link\" 2>/dev/null; then echo OK; else echo EXDEV; fi
    rm -rf \"\$w\"" 2>/dev/null || true)"
case "$LINKPROBE" in
    *OK*)    ok "实测 link() 成功：容器内源与目标落在同一挂载" ;;
    *SKIP*)  info "源目录里没有可实证的样本文件（跳过实证探测）" ;;
    *NODIR*) bad "无法在库侧创建临时目录 —— 库挂载不可写？"; exit 1 ;;
    *)       die "实测 link() 失败（跨挂载 EXDEV）—— 源与目标必须在同一个挂载里；见本脚本头部说明" ;;
esac

info "健康检查详情：$(printf '%s' "$HEALTH_RAW" | head -c 1000)"

# ---- 3. 新建一个**自定义处理器**（核心：重命名规则由处理器驱动，不是写死在代码里）----
# 从内置 `bangumi` 模板派生：规则沿用该模板的既有行为，但它是**独立的**处理器
# （builtin=false、自己的 log_file），因此它有自己的日志、可以随便改规则而不影响内置处理器。
say "3. 从 bangumi 模板新建自定义处理器（POST /api/processors → 201）"
PROC_ID="repro_bangumi"
PROC_JSON="{\"id\":\"${PROC_ID}\",\"name\":\"复现实例（自定义番剧处理器）\",\"template\":\"bangumi\"}"
PROC_RAW="$(curl -sS -X POST "http://127.0.0.1:${PORT}/api/processors" \
    -H 'Content-Type: application/json' \
    -b "$JAR" \
    -d "$PROC_JSON")" || die "POST /api/processors 请求失败"
printf '    %s\n' "$PROC_RAW"
case "$PROC_RAW" in
    *"\"id\":\"$PROC_ID\""*|*"\"id\": \"$PROC_ID\""*) ok "自定义处理器已创建：$PROC_ID" ;;
    *) die "自定义处理器未创建成功（见上方原始响应；409 = id 已存在，删掉后重跑或改 PROC_ID）" ;;
esac
case "$PROC_RAW" in
    *'"builtin":false'*|*'"builtin": false'*) ok "它是非内置处理器（builtin=false），不会被内置重置影响" ;;
    *) bad "响应里 builtin 不是 false —— 自定义处理器应当是 builtin=false（见上方原始响应）"; exit 1 ;;
esac
case "$PROC_RAW" in
    *'"log_file":"bangumicache.log"'*|*'"log_file": "bangumicache.log"'*)
        bad "自定义处理器的 log_file 与内置处理器相同 —— 两个处理器会互相判定“已处理”，这是必须避免的"
        exit 1 ;;
    *'"log_file":'*) ok "它有自己的日志文件（与内置 bangumi 不共享，避免互相跳过）" ;;
    *) info "响应里没有 log_file 字段（响应结构可能已变）" ;;
esac

# ---- 4. 建媒体库（引用刚建的自定义处理器）----------------------------------
say "4. 通过 API 创建媒体库（processor_id=$PROC_ID，type 只是自由文本标签）"
LIB_JSON="{\"name\":\"repro-bangumi\",\"type\":\"番剧\",\"processor_id\":\"$PROC_ID\",\"enabled\":true,\"source_dir\":\"$SRC/Bangumi\",\"target_dir\":\"$DST/Bangumi\",\"match_categories\":[],\"match_tags\":[],\"match_path_prefixes\":[\"$SRC/Bangumi\"],\"orphan_cleanup\":true}"
LIB_RAW="$(curl -sS -X POST "http://127.0.0.1:${PORT}/api/libraries" \
    -H 'Content-Type: application/json' \
    -b "$JAR" \
    -d "$LIB_JSON")" || die "POST /api/libraries 请求失败"
printf '    %s\n' "$LIB_RAW"
LIB_ID="$(printf '%s' "$LIB_RAW" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')"
[ -n "$LIB_ID" ] || die "响应里没有 id 字段；媒体库未创建成功（见上方原始响应）"
case "$LIB_RAW" in
    *"\"processor_id\":\"$PROC_ID\""*|*"\"processor_id\": \"$PROC_ID\""*)
        ok "媒体库已创建：id=$LIB_ID，处理器=$PROC_ID（$SRC/Bangumi → $DST/Bangumi）" ;;
    *) bad "媒体库的 processor_id 不是 $PROC_ID（见上方原始响应）"; exit 1 ;;
esac

# ---- 4b. 只读预检（硬链接可行性；不建目录、不建链接）-----------------------
# 这一步的价值：它把"源与目标是否真能 link()"在**执行前**就报出来，而不是等任务跑到
# 一半才以 EXDEV 失败。这里的预检是**实证**的：在库侧建一个临时硬链接再删掉
# （源目录零写入），返回 same_filesystem: true / false / null
# （null = 源目录里找不到可实证的文件，例如空目录 —— 那是"无法实证"，不是失败）。
say "4b. 只读预检 /validate（实测源↔目标能否 link）"
# 只读：不建目录、不建硬链接。`?probe=true` 显式要求**实证**探针（默认即 true）；
# 传 false 时只剩挂载 ID / st_dev 预筛，那两样都是"必要不充分"，可能假绿。
# 探针的"无痕"证据：目标目录 mtime 取前值，调用后再比对（app 用 os.utime 还原 mtime）。
TGT_MT_0="$(stat -c '%Y' "$DST/Bangumi" 2>/dev/null || echo NA)"
VALID_RAW="$(curl -sS -X POST "http://127.0.0.1:${PORT}/api/libraries/${LIB_ID}/validate?probe=true" \
    -H 'Content-Type: application/json' \
    -b "$JAR" \
    -d '{}')" || die "POST …/validate 请求失败"
printf '    %s\n' "$VALID_RAW"
case "$VALID_RAW" in
    *'"errors":[]'*|*'"errors": []'*) ok "预检无错误" ;;
    *) bad "预检报出错误（见上方原始响应）—— 先解决它再往下跑"; exit 1 ;;
esac
case "$VALID_RAW" in
    *'"same_filesystem":true'*|*'"same_filesystem": true'*)
        ok "预检实证：源与目标在同一挂载（same_filesystem=true）" ;;
    *'"same_filesystem":false'*|*'"same_filesystem": false'*)
        die "预检实证为 false：源与目标跨挂载，link() 会 EXDEV。检查 $WORK 是否被挂成单一共同父挂载" ;;
    *) info "预检没给 same_filesystem（响应结构可能已变），以第 5 步的任务 result 为准" ;;
esac
# 光看 same_filesystem 不够：它为 true 有两条来源 —— ① probe.ok=true（真·实证）；
# ② 探针跑不了时由「挂载 ID 相等」或「st_dev 相等」推出的预筛结论（probe.ok=null，
#    必要不充分，同一设备号但不同挂载子树仍可能 EXDEV）。所以这里再断言 probe.ok。
VALID_FLAT="$(printf '%s' "$VALID_RAW" | tr -d ' \t\n\r')"
case "$VALID_FLAT" in
    *'"probe":{"ok":true'*)
        ok "预检为真·实证：probe.ok=true（已从源树取样文件真 link 到目标目录并删除）" ;;
    *'"probe":{"ok":false'*)
        die "预检实证失败：probe.ok=false（源与目标确实无法 link，很可能 EXDEV）；probe.note 见上" ;;
    *'"probe":null'*)
        info "预检无法实证（probe=null，源树在深度 3/条目 512 内没有普通文件等）——以第 5 步任务 result 为准" ;;
    *) info "预检响应里没有 probe 字段（版本可能较旧），以第 5 步任务 result 为准" ;;
esac
# 把 probe.note 原文打出来，便于与 app 侧日志/errno 对账（EXDEV=18 / EACCES=13 / ENOSPC=28）。
PROBE_NOTE="$(printf '%s' "$VALID_RAW" | sed -n 's/.*"note"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
[ -n "$PROBE_NOTE" ] && info "probe.note：$PROBE_NOTE"
# 实证成功时，note 里应当明写"源目录未被写入"——这是"只读安全探针"最直接的证据。
case "$VALID_FLAT" in
    *'"probe":{"ok":true'*)
        case "$PROBE_NOTE" in
            *"源目录未被写入"*) ok "探针安全性：note 明写「源目录未被写入」" ;;
            *) bad "实证成功但 note 未写明「源目录未被写入」：$PROBE_NOTE" ;;
        esac ;;
esac
# 无痕性：目标目录 mtime 必须与调用前一致 + 不得残留 .mlm_probe_* 临时链接。
if [ "$TGT_MT_0" != "NA" ]; then
    TGT_MT_1="$(stat -c '%Y' "$DST/Bangumi" 2>/dev/null || echo NA)"
    if [ "$TGT_MT_0" = "$TGT_MT_1" ]; then
        ok "探针无痕：目标目录 mtime 调用前后一致（$TGT_MT_0）"
    else
        bad "探针改了目标目录 mtime（$TGT_MT_0 → $TGT_MT_1）；源目录仍零写入，但目标未还原"
    fi
fi
RESIDUE="$(find "$WORK" -name '.mlm_probe_*' 2>/dev/null | wc -l | tr -d ' ')"
case "$RESIDUE" in
    0) ok "无探针残留（find $WORK -name '.mlm_probe_*' 计数 0）" ;;
    *) die "发现探针残留 $RESIDUE 个（.mlm_probe_* 未被清理）" ;;
esac

# ---- 4c. 降级分支：?probe=false 只许"无法实证"，不许冒充实证成功 ------------
# 级联顺序是「挂载 ID → st_dev」，具体走哪条取决于宿主（能读 /proc/self/mountinfo
# 就挂载 ID，否则 st_dev）。所以这里**不钉死**其中一条，只要求：
#   probe=null、same_filesystem 仍为 true（预筛说"同挂载"，不是 false）、errors 空、
#   且 warnings 里恰有一条含「未实证」并写明依据。
say "4c. 降级分支 /validate?probe=false（预筛不得被当成实证）"
VALID_FALSE="$(curl -sS -X POST "http://127.0.0.1:${PORT}/api/libraries/${LIB_ID}/validate?probe=false" \
    -H 'Content-Type: application/json' \
    -b "$JAR" \
    -d '{}')" || die "POST …/validate?probe=false 请求失败"
FALSE_FLAT="$(printf '%s' "$VALID_FALSE" | tr -d ' \t\n\r')"
case "$FALSE_FLAT" in
    *'"probe":null'*) ok "probe=false 时 probe 为 null（没有被冒充成实证）" ;;
    *) bad "probe=false 时 probe 不是 null（响应：$VALID_FALSE）" ;;
esac
case "$FALSE_FLAT" in
    *'"same_filesystem":true'*) ok "预筛结论为同挂载（same_filesystem=true，未被降级成 false）" ;;
    *'"same_filesystem":false'*) bad "预筛把同一挂载误判为 false（响应：$VALID_FALSE）" ;;
    *) info "预筛没给 same_filesystem（见上方原文）" ;;
esac
case "$FALSE_FLAT" in
    *'"errors":[]'*) ok "预筛无 errors" ;;
    *) bad "预筛报出 errors（响应：$VALID_FALSE）" ;;
esac
UNVERIFIED="$(printf '%s' "$VALID_FALSE" | grep -o '未实证' | wc -l | tr -d ' ')"
case "$UNVERIFIED" in
    1) ok "warnings 里恰有一条含「未实证」（不多不少）" ;;
    *) bad "「未实证」在响应里出现 $UNVERIFIED 次（应为 1 次）" ;;
esac
case "$FALSE_FLAT" in
    *'仅据挂载 ID'*|*'仅据挂载ID'*|*'仅据 st_dev'*|*'仅据st_dev'*)
        ok "降级依据已写明（挂载 ID 或 st_dev 之一，不钉死实现细节）" ;;
    *) bad "预筛响应未写明降级依据（既没有「仅据挂载 ID」也没有「仅据 st_dev」）" ;;
esac

# ---- 5. 触发执行并轮询 -----------------------------------------------------
say "5. 触发 run 并轮询任务状态"
TASK_RAW="$(curl -sS -X POST "http://127.0.0.1:${PORT}/api/libraries/${LIB_ID}/run" \
    -H 'Content-Type: application/json' \
    -b "$JAR" \
    -d '{"scope":{"mode":"full"},"force":false}')" || die "POST …/run 请求失败"
printf '    %s\n' "$TASK_RAW"
TASK_ID="$(printf '%s' "$TASK_RAW" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')"
[ -n "$TASK_ID" ] || die "响应里没有任务 id（见上方原始响应）"
ok "任务已入队：task_id=$TASK_ID"

STATUS="unknown"
TASK_JSON=""
n=0
while [ "$n" -lt 90 ]; do
    TASK_JSON="$(curl -sS -b "$JAR" "http://127.0.0.1:${PORT}/api/tasks/${TASK_ID}" 2>/dev/null || true)"
    STATUS="$(printf '%s' "$TASK_JSON" | sed -n 's/.*"status"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p')"
    case "$STATUS" in
        success|failed|skipped|cancelled) break ;;
    esac
    n=$((n + 1))
    sleep 1
done
info "任务终态：status=$STATUS（轮询 ${n} 次）"
printf '    %s\n' "$TASK_JSON"
if [ "$STATUS" != "success" ]; then
    bad "任务未成功（status=$STATUS）；以下为任务日志："
    curl -sS -b "$JAR" "http://127.0.0.1:${PORT}/api/tasks/${TASK_ID}/logs?after_id=0&limit=200" 2>/dev/null | head -c 4000 >&2 || true
    printf '\n' >&2
    exit 1
fi
ok "任务执行成功"

# 任务 result 是硬链接成败的**主判据**（st_dev 相等**不能**预测 link() 成功，
# 跨挂载时 st_dev 也可能相同 —— 只看设备号会"假绿"）。所以先看计数器：
#   linked > 0 且 link_failed == 0
LINKED="$(printf '%s' "$TASK_JSON" | sed -n 's/.*"linked"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')"
LINKFAIL="$(printf '%s' "$TASK_JSON" | sed -n 's/.*"link_failed"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')"
[ -n "$LINKED" ] || die "任务 result 里没有 linked 字段（响应结构可能已变）：$TASK_JSON"
[ "$LINKED" -gt 0 ] || die "任务 status=success 但 linked=0 —— 没有建立任何硬链接（result：$TASK_JSON）"
[ "$LINKFAIL" = "0" ] || die "link_failed=$LINKFAIL > 0（result：$TASK_JSON）—— 典型原因是源与目标跨挂载（EXDEV）"
ok "任务 result：linked=$LINKED / link_failed=$LINKFAIL"

# ---- 6. 核心验收：硬链接（同 inode）---------------------------------------
# 源文件在库侧的**新名字**由媒体库选中的处理器决定。这里用的是第 3 步从内置
# `bangumi` 模板派生的自定义处理器 `$PROC_ID`（规则沿用该模板的既有行为）：
#   一级文件夹名 = "Show A"（title）
#   季文件夹 "Season 1" 命中 accept_dirs `^Season (\d+)$` ⇒ season=01
#   文件名 "[Lilith-Raws] Show A - 01" 命中 `-\d{1,2}` 交替式 ⇒ episode=01
#   ⇒ "Show A S01E01.mkv"
# 万一将来命名规则改了，下面的报错会打印库内实际内容，直接照着改 TARGET 即可。
TARGET="$DST/Bangumi/Show A/Season 1/Show A S01E01.mkv"
SOURCE="$SRC/Bangumi/Show A/Season 1/[Lilith-Raws] Show A - 01.mkv"
say "6. 验证硬链接：源与目标必须是同一 inode（stat -c '%d %i %h'）"
[ -f "$SOURCE" ] || die "源文件不存在（源目录被改动了？）：$SOURCE"
[ -f "$TARGET" ] || { info "库内实际内容："; find "$DST/Bangumi" -mindepth 1 2>/dev/null || true; die "目标文件不存在：$TARGET"; }

S_SRC="$(stat -c '%d %i %h' "$SOURCE")"
S_DST="$(stat -c '%d %i %h' "$TARGET")"
info "宿主 stat -c '%d %i %h'："
info "  源   : $S_SRC   $SOURCE"
info "  目标 : $S_DST   $TARGET"
DEV_SRC="$(printf '%s' "$S_SRC" | awk '{print $1}')"
INO_SRC="$(printf '%s' "$S_SRC" | awk '{print $2}')"
DEV_DST="$(printf '%s' "$S_DST" | awk '{print $1}')"
INO_DST="$(printf '%s' "$S_DST" | awk '{print $2}')"
LNK_DST="$(printf '%s' "$S_DST" | awk '{print $3}')"
[ "$DEV_SRC" = "$DEV_DST" ] || die "st_dev 不同（$DEV_SRC vs $DEV_DST）——说明发生了**复制**而非硬链接"
[ "$INO_SRC" = "$INO_DST" ] || die "st_ino 不同（$INO_SRC vs $INO_DST）——说明发生了**复制**而非硬链接"
[ "$LNK_DST" -ge 2 ] || die "目标链接数 $LNK_DST < 2 —— 不是硬链接"
ok "硬链接验证通过：st_dev=$DEV_SRC st_ino=$INO_SRC 链接数=$LNK_DST"

# 源目录内容未被改动（重命名只发生在库侧）
S_SRC2="$(stat -c '%d %i %h' "$SOURCE")"
[ "$S_SRC" = "$S_SRC2" ] || die "源文件 stat 在任务后发生变化"
if docker exec "$CNAME" sh -c "ls '$SRC/Bangumi/Show A/Season 1/' | grep -q 'Lilith-Raws'" >/dev/null 2>&1; then
    ok "源侧文件名未被改动（重命名只作用于库侧）"
fi

info "库内实际内容："
find "$DST" -mindepth 1 | sed "s|$WORK|  .|"

C_STAT="$(docker exec "$CNAME" sh -c "stat -c '%d %i %h' '$TARGET'" 2>/dev/null || true)"
if [ -n "$C_STAT" ]; then
    info "容器内 stat：$C_STAT"
fi

say "验收通过"
printf '    源  : %s\n' "$SRC/Bangumi/Show A/Season 1/"
printf '    库  : %s\n' "$DST/Bangumi/Show A/Season 1/"
printf '    数据: %s（SQLite / 日志）\n' "$DATA"
printf '    日志: %s\n' "$LOG"
printf '\n    本次验证覆盖：镜像能构建、主进程以 uid 1000/gid 1001 运行、**两条挂载**（媒体共同\n'
printf '    父挂载与 /data 必须是不同挂载；entrypoint 的只读结构检查通过）、容器内**实测 link() 成功**、\n'
printf '    健康检查 status=ok、**新建自定义处理器**（processor_id=%s）并让媒体库引用它、\n' "$PROC_ID"
printf '    API 全链路（登录→建处理器→建库→预检→执行）、任务 result linked>0 且 link_failed=0、\n'
printf '    以及**最关键的**：库内文件与源文件共享同一 inode（真硬链接，不是复制）。\n'
printf '\n    加 KEEP=1 重跑可保留上述目录；本脚本可重复执行（每次从零重建）。\n'
