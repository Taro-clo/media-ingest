#!/bin/sh
# =============================================================================
# entrypoint —— 以 root 启动、做一次性的 uid/gid 与属主修复，然后 gosu 降权到 app
# =============================================================================
# 为什么以 root 启动：容器绑定的宿主目录（/data、/srv/mlm）属主可能是任意 uid/gid，
# 只有 root 能在**运行时**纠正它们；纠正完立刻降权，进程不以 root 运行。
#
# 明确的边界（下面每一步都做了，且只做这些）：
#   1. **绝不写媒体挂载**（MEDIA_ROOT 下的任何路径）：不 chown、不 chmod、不 mkdir、
#      不 touch。源目录与目标媒体库共用**一条共同父挂载**（Linux `link()` 不能
#      跨挂载，见 docker-compose.yml 文件头第 1 点），于是"/downloads 以 :ro
#      提供结构性只读保证"这层保护并不存在。容器侧改为两道替代保证：
#        a) 本脚本的结构性纪律（只对 $DATA_DIR 与 $APP_HOME 做 chown -R）；
#        b) 启动期结构检查：$DATA_DIR 必须与 $MEDIA_ROOT **不在同一个挂载**里
#           （否则 chown -R $DATA_DIR 会递归改到媒体文件的属主 —— 硬链接共享 inode，
#           那会连带改到 qBittorrent 侧看到的文件属性）。
#   2. 只有 $DATA_DIR（默认 /data）与 $APP_HOME（应用根，默认 /srv/mlm）会被 chown。
#   3. 最终 exec 的是 gosu app，uvicorn 进程的 uid 一定是 PUID。
#
# ==================== 单一共同父挂载（两条卷）======================
# compose 的卷是**两条**：`${MEDIA_ROOT}:${MEDIA_ROOT}`（源与目标的共同父挂载）
# 与 `./data:/data`。因此：
#   * 本脚本**不检查 /downloads、/library**（单挂载形态下容器里没有这两个路径），
#     改为检查 $MEDIA_ROOT 是否挂上、以及"$DATA_DIR 与 $MEDIA_ROOT 不在同一挂载"；
#   * **绝不**对 $MEDIA_ROOT 下任何路径做 chown/chmod/mkdir；只递归 chown $DATA_DIR；
#   * $DATA_DIR 不是 /data 时，CONFIG_DIR 默认值改为 `${DATA_DIR}/config` 并**导出**，
#     与应用侧 `app/config.py` 的 CONFIG_DIR 语义对齐（否则容器会在 /data/config 上
#     撞到 "对 uid … 不可写" 而拒绝启动）。
#
# 启动自检现在只确认 /data/config（CONFIG_DIR）可写 ——
# 它对应 healthcheck 的 checks.config 一项。
# =============================================================================
set -eu

log() { printf '%s [entrypoint] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
warn() { printf '%s [entrypoint][WARN] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }
die() { printf '%s [entrypoint][FATAL] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; exit 1; }

# PUID/PGID 默认 auto：按媒体共同父挂载的属主自动识别（见下面第 0 节），
# 用户不必再去宿主查 id。显式传数字时按数字来（老部署/特殊权限场景）。
PUID="${PUID:-auto}"
PGID="${PGID:-auto}"
DATA_DIR="${DATA_DIR:-/data}"
# DATA_DIR 不是 /data 时，CONFIG_DIR 的默认值跟随 DATA_DIR（与应用侧 config.py 的
# CONFIG_DIR 语义一致），并**导出**给应用进程 —— 否则应用会去用 /data/config，
# 而这个路径在"数据卷挂在别处"的部署里不存在/不可写，容器会在下面的可写性检查里 FATAL。
# 显式设置了 CONFIG_DIR 就尊重它。
CONFIG_DIR="${CONFIG_DIR:-${DATA_DIR%/}/config}"
export CONFIG_DIR
PORT="${PORT:-18081}"
# 源与目标的**共同父挂载**（容器内路径 = 宿主路径，见 docker-compose.yml 与 .env.example §3）。
# 本脚本只**只读**地用它做结构性检查，绝不写入其下任何路径（见文件头边界 1）。
MEDIA_ROOT="${MEDIA_ROOT:-}"
APP_USER="app"
# 应用根（Dockerfile 的 WORKDIR）：只含 app/ 包与 web/ 静态资源。
APP_HOME="/srv/mlm"
# ⚠️ 依赖 Dockerfile 里 `groupadd --gid 999 app` 建立的**同名组** app：
#    groupmod -g 改的是"组 app"的 gid，所以 ① 组名必须与 APP_USER 相同；
#    ② 该组必须已存在。若将来把组名改成别的（如 appgroup），这里会静默失去作用，
#    必须同时改下面所有 groupmod/附加组逻辑与 Dockerfile 的 groupadd。
APP_GROUP="app"

# ---------------------------------------------------------------------------
# 0. 运行身份自动识别（PUID/PGID = auto 时）
#    "能读下载源、能写媒体库"的那个用户，就是媒体共同父挂载的属主，直接用它的
#    uid/gid，省掉用户去宿主执行 `id` 再手填。识别顺序：
#      $MEDIA_ROOT 属主 -> $DATA_DIR 属主 -> 1000:1001（镜像内的通用默认）
#    只读 stat，不写任何东西；显式传入数字时完全不介入。
# ---------------------------------------------------------------------------
pair_of_owner() {
    [ -n "${1:-}" ] || return 0
    [ -d "$1" ] || return 0
    stat -c '%u %g' "$1" 2>/dev/null || true
}
puid_auto=0
pgid_auto=0
case "$PUID" in auto|"") puid_auto=1 ;; esac
case "$PGID" in auto|"") pgid_auto=1 ;; esac
if [ "$puid_auto" = 1 ] || [ "$pgid_auto" = 1 ]; then
    det=""
    det_src=""
    for cand in "${MEDIA_ROOT:-}" "$DATA_DIR"; do
        [ -n "$cand" ] || continue
        pair="$(pair_of_owner "$cand")"
        [ -n "$pair" ] || continue
        det_uid="${pair%% *}"
        det_gid="${pair##* }"
        if [ "$det_uid" != "0" ]; then
            det="$pair"
            det_src="$cand"
            break
        fi
    done
    if [ -n "$det" ]; then
        det_uid="${det%% *}"
        det_gid="${det##* }"
        if [ "$puid_auto" = 1 ]; then PUID="$det_uid"; fi
        if [ "$pgid_auto" = 1 ]; then PGID="$det_gid"; fi
        log "自动识别运行身份：PUID=${PUID} PGID=${PGID}（取自 ${det_src} 的属主）"
    else
        warn "无法从媒体挂载/数据目录识别属主，回退默认 1000:1001；若读写报 Permission denied，请显式设置 PUID/PGID"
    fi
fi
PUID="${PUID:-1000}"
PGID="${PGID:-1001}"

# ---------------------------------------------------------------------------
# 0.1 基本校验
# ---------------------------------------------------------------------------
[ -n "$PUID" ] || die "PUID 不能为空"
[ -n "$PGID" ] || die "PGID 不能为空"
case "$PUID" in *[!0-9]*) die "PUID 必须是数字，收到 \"$PUID\"" ;; esac
case "$PGID" in *[!0-9]*) die "PGID 必须是数字，收到 \"$PGID\"" ;; esac
case "$PORT" in *[!0-9]*) die "PORT 必须是数字，收到 \"$PORT\"" ;; esac
id "$APP_USER" >/dev/null 2>&1 || die "镜像内缺少用户 $APP_USER，镜像可能损坏"
# 主组必须与用户同名（见上面 APP_GROUP 的说明）；不同名时明确报错，避免静默降级。
getent group "$APP_GROUP" >/dev/null 2>&1 || die "镜像内缺少组 $APP_GROUP（Dockerfile 里应有 groupadd --gid 999 $APP_GROUP）"
[ -n "$CONFIG_DIR" ] || die "CONFIG_DIR 不能为空"
# MEDIA_ROOT（源与目标的共同父挂载）是可选的：只在 compose 里传了才检查。
# 这里全部是**只读**检查，绝不写入其下任何路径（见文件头边界 1）。
if [ -n "$MEDIA_ROOT" ]; then
    case "$MEDIA_ROOT" in
        /) die "MEDIA_ROOT 不能是 /（把宿主根目录整个挂进容器，风险不可接受）" ;;
        /*) ;;
        *) die "MEDIA_ROOT 必须是以 / 开头的绝对路径，收到 \"$MEDIA_ROOT\"" ;;
    esac
    [ "$MEDIA_ROOT" != "$DATA_DIR" ] || die "MEDIA_ROOT 不能等于 DATA_DIR（$DATA_DIR）：数据卷必须与媒体挂载分开，否则下面的 chown -R 会递归改到媒体文件属主（硬链接共享 inode，会连带影响 qBittorrent 侧）"
fi

# 读取某路径**所在挂载**的 mount ID（Linux /proc/self/mountinfo 的第 1 个字段）。
# 用途：证明 $DATA_DIR 与 $MEDIA_ROOT 不在同一挂载（=== chown -R $DATA_DIR 碰不到媒体文件）。
# 非 Linux（没有 /proc）时输出空串，调用方据此跳过检查（优雅降级，不误判）。
mount_id_of() {
    [ -r /proc/self/mountinfo ] || return 0
    awk -v target="$1" '
        {
            mp = $5
            gsub(/\\040/, " ", mp)            # mountinfo 用 \040 转义空格
            if (mp == "/" || target == mp || index(target, mp "/") == 1) {
                if (length(mp) >= best) { best = length(mp); id = $1 }   # 最长前缀优先
            }
        }
        END { if (best > 0) print id }
    ' /proc/self/mountinfo
}

# ---------------------------------------------------------------------------
# 1. 把 app 用户对齐到 PGID / PUID
#    默认 1000:1001 是常见 NAS 的用户/组 ID；宿主上请用 id 命令确认你自己的。目标 gid/uid 在镜像里可能已被占用
#    （例如该 gid 已对应某个系统组），此时**不覆盖、不失败**，改为复用同名组/用户
#    并检查 app 的最终身份是否就是 PUID/PGID；不一致就给出去权提示而不是硬崩。
# ---------------------------------------------------------------------------
cur_uid="$(id -u "$APP_USER")"
cur_gid="$(id -g "$APP_USER")"

if [ "$cur_gid" != "$PGID" ]; then
    existing_grp="$(getent group "$PGID" 2>/dev/null | cut -d: -f1 || true)"
    if [ -z "$existing_grp" ]; then
        # 注意这里改的是**组** $APP_GROUP（与用户同名），不是用户名。
        # -o 允许非唯一 gid；依赖 Dockerfile 的 groupadd 已建好同名组（上面已校验）。
        groupmod -o -g "$PGID" "$APP_GROUP" 2>/dev/null \
            || warn "groupmod -g $PGID $APP_GROUP 失败；继续使用 gid=$cur_gid"
    elif [ "$existing_grp" != "$APP_GROUP" ]; then
        warn "gid $PGID 已被组 \"$existing_grp\" 占用；复用该组，app 的附加组将包含 $existing_grp"
        usermod -a -G "$existing_grp" "$APP_USER" 2>/dev/null \
            || warn "把 app 加入附加组 $existing_grp 失败"
    fi
fi

if [ "$cur_uid" != "$PUID" ]; then
    existing_usr="$(getent passwd "$PUID" 2>/dev/null | cut -d: -f1 || true)"
    if [ -z "$existing_usr" ]; then
        usermod -o -u "$PUID" "$APP_USER" 2>/dev/null \
            || warn "usermod -u $PUID $APP_USER 失败；继续使用 uid=$cur_uid"
    elif [ "$existing_usr" != "$APP_USER" ]; then
        warn "uid $PUID 已被用户 \"$existing_usr\" 占用；无法把 app 改成该 uid"
    fi
fi

final_uid="$(id -u "$APP_USER")"
final_gid="$(id -g "$APP_USER")"
if [ "$final_uid" != "$PUID" ] || [ "$final_gid" != "$PGID" ]; then
    warn "app 最终身份为 ${final_uid}:${final_gid}，请求的是 ${PUID}:${PGID}。"
    warn "若读取源目录报 Permission denied，请把 PUID/PGID 调整为宿主下载目录的属主。"
fi

# ---------------------------------------------------------------------------
# 2. 数据目录与配置目录就绪（**只碰 $DATA_DIR、$APP_HOME，绝不碰只读挂载点**）
# ---------------------------------------------------------------------------
[ -d "$DATA_DIR" ] || mkdir -p "$DATA_DIR" || die "无法创建数据目录 $DATA_DIR"
[ -w "$DATA_DIR" ] || die "数据目录 $DATA_DIR 不可写；请检查宿主目录属主/权限"

# CONFIG_DIR（默认 /data/config）是配置文件的镜像目录（PROCESSORS.md §6.1）。
# 它是 DB 的可移植镜像：WebUI 每次保存都会原子写出这里，后台线程也会监视它做导入。
# 不可写 ⇒ 配置无法落盘、WebUI 会一直提示不同步，所以这里直接**失败**（对应
# healthcheck 的 checks.config）。
mkdir -p "$CONFIG_DIR" || die "无法创建配置目录 $CONFIG_DIR（CONFIG_DIR=$CONFIG_DIR）"

# 只对 $DATA_DIR 做递归 chown：它是持久化根（SQLite、日志、config/、secret.key），必须归 app。
# ⚠️ **绝不**对媒体挂载（$MEDIA_ROOT 下的任何路径）做 chown/chmod/mkdir/touch：
#    源目录与媒体库共用一条共同父挂载，"/downloads 以 :ro 保证只读"那层保护并不存在。
#    媒体库可能极大，递归改属主既慢又危险（硬链接共享 inode，改属主会连带改到
#    qBittorrent 侧那些源文件的属主）。这条约束在任何部署形态下都适用，别改。
#    下面紧接着用 mount ID 做一次**结构检查**，确保 $DATA_DIR 不在媒体挂载里。
if [ -n "$MEDIA_ROOT" ] && [ -d "$MEDIA_ROOT" ]; then
    data_mnt="$(mount_id_of "$DATA_DIR")"
    media_mnt="$(mount_id_of "$MEDIA_ROOT")"
    if [ -n "$data_mnt" ] && [ -n "$media_mnt" ] && [ "$data_mnt" = "$media_mnt" ]; then
        die "结构检查失败：DATA_DIR（$DATA_DIR，mount $data_mnt）与 MEDIA_ROOT（$MEDIA_ROOT，mount $media_mnt）在**同一个挂载**里。
     下面的 chown -R $DATA_DIR 会递归进入媒体挂载、改到媒体文件的属主（硬链接共享 inode，
     会连带影响 qBittorrent 侧看到的属性），因此拒绝启动。
     修法：在 compose 里把数据卷单独挂上（\"./data:/data\"），或把 DATA_DIR 指到一个独立挂载。"
    fi
    log "结构检查通过：$DATA_DIR（mount ${data_mnt:-?}）与 $MEDIA_ROOT（mount ${media_mnt:-?}）不在同一挂载"
elif [ -n "$MEDIA_ROOT" ]; then
    warn "$MEDIA_ROOT 不存在：媒体共同父挂载没挂上，硬链接任务必然失败（compose 第一条 volumes）"
fi
chown -R "$APP_USER:$APP_USER" "$DATA_DIR" 2>/dev/null \
    || warn "chown -R $APP_USER $DATA_DIR 未完全成功（可能是 NFS/SMB 或不支持 chown 的卷）"

# $APP_HOME 只需顶层 + app/ 包目录；COPY 已在构建期 chown，这里兜底。
chown -R "$APP_USER:$APP_USER" "$APP_HOME" 2>/dev/null || warn "chown $APP_HOME 失败（非致命）"

# 以 app 身份实测 CONFIG_DIR 可写（root 的 -w 测试会掩盖属主问题）
if gosu "$APP_USER" test -w "$CONFIG_DIR" 2>/dev/null; then
    log "配置目录可写：$CONFIG_DIR（以 uid $final_uid 身份验证通过）"
else
    die "配置目录 $CONFIG_DIR 对 uid $final_uid 不可写。
     它默认位于数据卷 $DATA_DIR 内（compose 里 \"./data:/data\" 那条卷；这里是 $DATA_DIR/config）。
     请在宿主把该卷的属主交给 ${PUID}:${PGID}，例如：
       sudo chown -R ${PUID}:${PGID} '<宿主上对应 $DATA_DIR 的目录>'
     （NAS 默认部署即 sudo chown -R ${PUID}:${PGID} /vol1/1000/Docker/media-ingest/data）
     若你把 DATA_DIR 指到了别处，请同时显式设置 CONFIG_DIR。"
fi

# ---------------------------------------------------------------------------
# 3. 媒体挂载就绪性提示（**只读**；绝不在这里写任何文件）
#    源与目标共用一条共同父挂载 $MEDIA_ROOT，容器内路径 = 宿主路径。
#    这里只做提示，不做判定：真正的健康判据在 /api/system/health 的
#    checks.db / data_dir / processors / config（v2 起源/目标根目录两项检查已删除）。
# ---------------------------------------------------------------------------
if [ -n "$MEDIA_ROOT" ] && [ -d "$MEDIA_ROOT" ]; then
    # 只读断言：以 app 身份确认能进入媒体挂载（读源树是硬链接任务的前提）。
    if gosu "$APP_USER" test -r "$MEDIA_ROOT" 2>/dev/null; then
        log "媒体挂载可读：$MEDIA_ROOT（以 uid $final_uid 身份验证通过；本脚本不写入其下任何路径）"
    else
        warn "$MEDIA_ROOT 对 uid $final_uid 不可读；硬链接任务会失败（检查 PUID/PGID 与宿主权限）。"
    fi
elif [ -z "$MEDIA_ROOT" ]; then
    warn "未传入 MEDIA_ROOT；跳过媒体挂载检查（compose 应在 environment 里传 ${MEDIA_ROOT}）。"
fi
# ⚠️ 注意：启动期**不知道**每个媒体库的源/目标目录（它们是 DB 里的库属性，不是设置项），
#    所以本脚本无法预判某条子路径是"源"还是"库"；**任何**写入都是被禁止的。
#    同挂载校验在 WebUI 建库/改库时进行（跨挂载直接拒绝，不再依赖健康检查）。

# ---------------------------------------------------------------------------
# 5. 降权并 exec
# ---------------------------------------------------------------------------
log "启动命令：uid=$final_uid gid=$final_gid data=$DATA_DIR config=$CONFIG_DIR port=$PORT media=${MEDIA_ROOT:-<未传入>}"
exec gosu "$APP_USER" "$@"