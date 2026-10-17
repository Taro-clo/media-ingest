#!/bin/sh
# =============================================================================
# media-ingest 一键部署脚本（linux/amd64 与 linux/arm64 通用）
# =============================================================================
# 目标：**尽量不需要你配置**。自动做的事：
#   * 自动探测媒体共同父目录（也可以在第一个参数里显式给）；
#   * 自动派生"下载源 / 媒体库"根目录（<MEDIA_ROOT>/Download、<MEDIA_ROOT>/Library，
#     也认 downloads / 下载 / library / 媒体库 等大小写与中文写法）；
#   * PUID/PGID 交给容器自动识别（取媒体目录属主），不用查 id；
#   * 数据卷默认放在本文件同目录的 ./data，不污染根目录；
#   * 自动拉取对应架构的镜像（amd64 / arm64 由 Docker 自己挑）；
#   * 起容器后等健康检查通过，再打印访问地址。
#
# 用法：
#   sh deploy.sh                     # 自动探测媒体目录
#   sh deploy.sh /mnt/media          # 显式指定媒体共同父目录
#   PORT=18090 sh deploy.sh /mnt/media
#
# 可用环境变量：
#   MEDIA_ROOT        媒体共同父目录（等价于第一个参数）
#   PORT              宿主端口（默认 18081）
#   MLM_DATA_DIR      数据卷宿主路径（默认 ./data，相对本文件所在目录）
#   DEFAULT_SOURCE_ROOT / DEFAULT_TARGET_ROOT  覆盖自动派生的源/目标根
#   MLM_VERSION       镜像版本（默认 1.0.0；latest 跟随最新发布）
#   MLM_REF           下载 compose 用的 git ref（默认 v1.0.0）
#   MLM_COMPOSE       本地已有的 compose 文件路径（给了就不再下载）
#   MLM_NO_WAIT=1     不等待健康检查
# =============================================================================
set -eu

log()  { printf '[deploy] %s\n' "$*"; }
warn() { printf '[deploy][WARN] %s\n' "$*" >&2; }
die()  { printf '[deploy][FATAL] %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

case "${1:-}" in
    -h|--help) usage ;;
esac

# ---------------------------------------------------------------- 0. 依赖检查
command -v docker >/dev/null 2>&1 || die "找不到 docker。请先安装 Docker（含 Compose v2）。"
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
    warn "检测到旧版 docker-compose v1；建议升级到 Docker Compose v2（v1 对部分写法支持较差）。"
else
    die "Docker Compose 不可用：需要 'docker compose'（v2）或 'docker-compose'。"
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ARCH=$(uname -m 2>/dev/null || echo unknown)
case "$ARCH" in
    x86_64|amd64)  ARCH_NAME="linux/amd64" ;;
    aarch64|arm64) ARCH_NAME="linux/arm64" ;;
    *)             ARCH_NAME="$ARCH（不受支持：镜像只有 amd64/arm64）" ;;
esac
log "主机架构：$ARCH（镜像：$ARCH_NAME）"

# ------------------------------------------------------------ 1. 媒体根目录
MEDIA_ROOT_ARG="${1:-}"
MEDIA_ROOT="${MEDIA_ROOT_ARG:-${MEDIA_ROOT:-}}"

have_media_layout() {  # $1 目录里是否有常见的下载/媒体库子目录
    [ -d "$1" ] || return 1
    for sub in Download downloads 下载 qbittorrent Library library 媒体库; do
        [ -e "$1/$sub" ] && return 0
    done
    return 1
}

if [ -z "$MEDIA_ROOT" ]; then
    log "未指定 MEDIA_ROOT，开始自动探测媒体目录…"
    for cand in /vol1/1000 /volume1 /volume1/media /mnt/user /mnt/media /srv/media /data/media "$HOME/media" "$HOME"; do
        [ -n "$cand" ] || continue
        if have_media_layout "$cand"; then
            MEDIA_ROOT="$cand"
            log "探测到媒体目录：$MEDIA_ROOT"
            break
        fi
    done
fi

if [ -z "$MEDIA_ROOT" ]; then
    die "没能自动探测到媒体目录。请显式指定，例如：
    sh deploy.sh /你的/下载与媒体库的共同父目录
  （它应同时包含下载目录和媒体库目录，如 /mnt/media/Download 与 /mnt/media/Library）"
fi
[ -d "$MEDIA_ROOT" ] || die "MEDIA_ROOT=$MEDIA_ROOT 不存在。请先创建（Docker 会以 root 建空目录，权限容易出问题），或换一个已存在的目录。"
case "$MEDIA_ROOT" in
    /) die "MEDIA_ROOT 不能是 /（把宿主根目录整个挂进容器，风险不可接受）" ;;
    /*) ;;
    *) MEDIA_ROOT=$(CDPATH= cd -- "$MEDIA_ROOT" && pwd) ;;
esac

# ------------------------------------------------------- 2. 源根 / 目标根派生
pick_first_dir() {  # 打印第一个存在的候选目录
    for d in "$@"; do
        if [ -n "$d" ] && [ -d "$d" ]; then printf '%s\n' "$d"; return 0; fi
    done
    return 1
}

DEFAULT_SOURCE_ROOT="${DEFAULT_SOURCE_ROOT:-}"
DEFAULT_TARGET_ROOT="${DEFAULT_TARGET_ROOT:-}"
if [ -z "$DEFAULT_SOURCE_ROOT" ]; then
    DEFAULT_SOURCE_ROOT=$(pick_first_dir \
        "$MEDIA_ROOT/Download" "$MEDIA_ROOT/downloads" "$MEDIA_ROOT/下载" \
        "$MEDIA_ROOT/qbittorrent" || printf '%s' "$MEDIA_ROOT/Download")
fi
if [ -z "$DEFAULT_TARGET_ROOT" ]; then
    DEFAULT_TARGET_ROOT=$(pick_first_dir \
        "$MEDIA_ROOT/Library" "$MEDIA_ROOT/library" "$MEDIA_ROOT/媒体库" \
        || printf '%s' "$MEDIA_ROOT/Library")
fi
log "下载源根：$DEFAULT_SOURCE_ROOT"
log "媒体库根：$DEFAULT_TARGET_ROOT"
[ -d "$DEFAULT_SOURCE_ROOT" ] || warn "下载源根不存在（首次建库前需要真实存在，否则任务会失败）"
[ -d "$DEFAULT_TARGET_ROOT" ] || warn "媒体库根不存在（首次建库前需要真实存在，否则任务会失败）"

# 硬链接前提：源与目标必须在同一个挂载（同一 st_dev）。都真实存在时才检查。
if [ -d "$DEFAULT_SOURCE_ROOT" ] && [ -d "$DEFAULT_TARGET_ROOT" ]; then
    src_dev=$(stat -c '%d' "$DEFAULT_SOURCE_ROOT" 2>/dev/null || echo "")
    tgt_dev=$(stat -c '%d' "$DEFAULT_TARGET_ROOT" 2>/dev/null || echo "")
    if [ -n "$src_dev" ] && [ -n "$tgt_dev" ] && [ "$src_dev" != "$tgt_dev" ]; then
        warn "下载源与媒体库不在同一个文件系统（st_dev $src_dev vs $tgt_dev）：硬链接会失败（EXDEV）。
  请把两者放到同一个挂载下，或把 MEDIA_ROOT 指向它们的共同父目录（同一个文件系统）。"
    fi
fi

# ---------------------------------------------------------------- 3. compose
if [ -n "${MLM_COMPOSE:-}" ]; then
    COMPOSE_FILE="$MLM_COMPOSE"
else
    COMPOSE_FILE="$SCRIPT_DIR/docker-compose.release.yml"
    if [ ! -f "$COMPOSE_FILE" ]; then
        REF="${MLM_REF:-v1.0.0}"
        URL="https://raw.githubusercontent.com/Taro-clo/media-ingest/$REF/work/docker-compose.release.yml"
        log "下载 compose 文件（$REF）…"
        command -v curl >/dev/null 2>&1 || die "需要 curl 或 wget 来下载 compose 文件（也可用 MLM_COMPOSE 指定本地文件）"
        curl -fsSL "$URL" -o "$COMPOSE_FILE" || die "下载失败：$URL"
    fi
fi
[ -f "$COMPOSE_FILE" ] || die "compose 文件不存在：$COMPOSE_FILE"

# 数据卷默认落在 compose 文件同目录的 ./data
COMPOSE_DIR=$(CDPATH= cd -- "$(dirname -- "$COMPOSE_FILE")" && pwd)
MLM_DATA_DIR="${MLM_DATA_DIR:-$COMPOSE_DIR/data}"
mkdir -p "$MLM_DATA_DIR" || die "无法创建数据目录 $MLM_DATA_DIR"

# 数据卷与媒体目录若在同一个挂载，容器启动时的结构检查会拒绝（保护媒体文件属主）。
if [ -d "$MEDIA_ROOT" ]; then
    data_dev=$(stat -c '%d' "$MLM_DATA_DIR" 2>/dev/null || echo "")
    media_dev=$(stat -c '%d' "$MEDIA_ROOT" 2>/dev/null || echo "")
    if [ -n "$data_dev" ] && [ "$data_dev" = "$media_dev" ]; then
        log "提示：数据目录与媒体目录在同一文件系统（$MEDIA_ROOT）；容器用独立 bind 挂载，不受影响。"
    fi
fi

# 生成 .env（compose 会自动读取同目录 .env）
ENV_FILE="$COMPOSE_DIR/.env"
{
    echo "# 由 deploy.sh 生成（$(date '+%Y-%m-%d %H:%M:%S')）；改完可重跑脚本覆盖。"
    echo "MEDIA_ROOT=$MEDIA_ROOT"
    echo "MLM_DATA_DIR=$MLM_DATA_DIR"
    echo "PORT=${PORT:-18081}"
    echo "DEFAULT_SOURCE_ROOT=$DEFAULT_SOURCE_ROOT"
    echo "DEFAULT_TARGET_ROOT=$DEFAULT_TARGET_ROOT"
    echo "MLM_VERSION=${MLM_VERSION:-1.0.0}"
} > "$ENV_FILE"

log "数据目录：$MLM_DATA_DIR"
log "端口：${PORT:-18081}"

# ------------------------------------------------------------------ 4. 启动
log "拉取镜像并启动（首次约 60 MB）…"
$COMPOSE -f "$COMPOSE_FILE" up -d

if [ "${MLM_NO_WAIT:-0}" = "1" ]; then
    log "已启动（未等待健康检查）。"
else
    log "等待健康检查通过（最多 180 秒）…"
    i=0
    status=""
    while [ "$i" -lt 60 ]; do
        status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' media-ingest 2>/dev/null || echo "")
        case "$status" in
            healthy) break ;;
            unhealthy) break ;;
        esac
        i=$((i + 1))
        sleep 3
    done
    case "$status" in
        healthy)
            log "容器已 healthy。"
            ;;
        unhealthy)
            warn "容器已启动但健康检查为 unhealthy。查看日志："
            warn "    $COMPOSE -f '$COMPOSE_FILE' logs --tail=100"
            ;;
        *)
            warn "等待健康检查超时（当前状态：${status:-未知}）。查看状态与日志："
            warn "    $COMPOSE -f '$COMPOSE_FILE' ps"
            warn "    $COMPOSE -f '$COMPOSE_FILE' logs --tail=100"
            ;;
    esac
fi

# ---------------------------------------------------------------- 5. 收尾提示
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "${HOST_IP:-}" ] || HOST_IP="<本机 IP>"
PORT_VAL="${PORT:-18081}"
printf '\n'
log "完成。浏览器打开： http://${HOST_IP}:${PORT_VAL}"
log "健康检查：           http://${HOST_IP}:${PORT_VAL}/api/system/health"
printf '\n'
cat <<EOF
常用命令（compose 文件：$COMPOSE_FILE）：
  状态    $COMPOSE -f "$COMPOSE_FILE" ps
  日志    $COMPOSE -f "$COMPOSE_FILE" logs -f --tail=100
  停止    $COMPOSE -f "$COMPOSE_FILE" down
  升级    MLM_VERSION=<新版本> $COMPOSE -f "$COMPOSE_FILE" pull && \\
          MLM_VERSION=<新版本> $COMPOSE -f "$COMPOSE_FILE" up -d
  备份    cp -a "$MLM_DATA_DIR" "<备份位置>"   # 含 mlm.db、config/、secret.key、logs/

数据目录：$MLM_DATA_DIR
媒体父目录：$MEDIA_ROOT（容器内路径 = 宿主路径，不需要改任何映射）
EOF
