# =============================================================================
# NAS 媒体库硬链接管理器 —— 应用镜像
# =============================================================================
# 构建（在仓库根目录执行，即本文件所在目录的上一级）：
#     docker compose build
#   或  docker build -f docker/Dockerfile -t media-ingest:latest .
#
# 设计要点（**自包含**）：
#   * 基座用 python:3.11-slim：若本地已缓存该镜像，构建期无需联网拉取基座。
#     官方 python 镜像同时提供 linux/amd64 与 linux/arm64。
#   * 只装三个运行时依赖（fastapi / uvicorn[standard] / PyYAML），其余全部是 Python 标准库。
#   * 运行期**非 root**：entrypoint 以 root 做一次属主修复，然后 gosu 降权到 app 用户。
#   * 硬链接与重命名全部由应用自己的代码承担（app/processors/hardlink.py + engine.py），
#     行为由项目自己的回归测试守卫；容器自包含，不需要挂载任何外部目录。
#
# 容器内目录（详见 docs/meta/STRUCTURE.md）：
#     /srv/mlm/app/                 Python 包（唯一应用根，WORKDIR）
#     /srv/mlm/web/                 前端静态资源
#     /usr/local/bin/mlm-entrypoint 启动脚本（uid/gid 修复 → gosu 降权）
#     /usr/local/bin/mlm-healthcheck 健康检查脚本
#     /data/                        唯一持久化卷（DB、配置镜像、日志、secret.key）
# =============================================================================

FROM python:3.11-slim

# ---- 构建期元信息 -----------------------------------------------------------
LABEL org.opencontainers.image.title="NAS Media Library Manager" \
      org.opencontainers.image.description="为 qBittorrent 已完成的下载创建硬链接并按媒体库配置重命名（源树只读，绝不写回）" \
      org.opencontainers.image.source="media-ingest" \
      org.opencontainers.image.licenses="MIT"

# ---- 容器内语言环境 ---------------------------------------------------------
# 日志文件用 open(LOG_FILE) 写入、不指定编码，依赖 locale；
# 必须锁定 UTF-8，否则容器内 POSIX locale 会造成 UnicodeEncodeError / 乱码。
ENV LANG=C.UTF-8 \
    LANGUAGE=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/srv/mlm \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    PORT=18081 \
    DATA_DIR=/data

# ---- 系统依赖 ---------------------------------------------------------------
# gosu  : entrypoint 降权工具（apt 包名即 gosu，Debian bookworm 有）
# tzdata: 让 TZ 环境变量真正生效（容器内时间戳/日志时间）
# ca-certificates: 若将来开启 qBittorrent API 走 https 时需要
# 不装 curl：HEALTHCHECK 用 python 标准库 urllib（见 docker/healthcheck.sh）
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        gosu \
        tzdata \
        ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# ---- Python 依赖 ------------------------------------------------------------
# 版本说明（撰写时点的当前稳定版，2025-11 核对 PyPI）：
#   fastapi        0.115.6 —— 0.115.x 是 2024-12 的稳定线，0.115.6 为最后一版补丁
#   uvicorn[standard] 0.32.1 —— 0.32.x 稳定线；[standard] 带 uvloop/httptools/websockets
#     （ARCHITECTURE.md §5 的 /api/tasks/stream SSE 需要 h11 之外的长连接支持；
#      标准库 http 也能工作，standard extra 只是更稳更快）
#   PyYAML         6.0.3（PROCESSORS.md §6.1）：处理器的规则写在
#     /data/config 下的 YAML 里，正是为了让用户能手改（注释、正则不必双重转义）。
#     只多一点几 MB，却省掉"把规则内联/转义在 JSON 里"的一整类可用性问题。
#     pin 6.0.3 而非 6.0.2：6.0.2 在较新的 CPython 上没有预编译 wheel，pip 会回落
#     源码构建并失败；6.0.3 有 manylinux wheel。容器是 3.11（两者都有 wheel），
#     但统一 6.0.3 能让"开发机验证"与"镜像里跑的"完全一致。
#     ⚠️ 因为用 wheel，**不需要** gcc / python3-dev 等构建工具链（本镜像刻意不装）。
# 升级方法：改下面的 pin → 重新 build → 看 /api/system/health 是否 200。
# 注意：不加版本上下界以外的约束，依赖由 pip 自行解析（镜像体积可控）。
RUN set -eux; \
    python -m pip install --no-cache-dir --upgrade pip; \
    python -m pip install --no-cache-dir \
        "fastapi==0.115.6" \
        "uvicorn[standard]==0.32.1" \
        "PyYAML==6.0.3"

# ---- 非 root 用户 -----------------------------------------------------------
# 镜像内先创建一个占位 app 用户（999:999，刻意避开宿主常见的 1000）。
# entrypoint 会在启动时按 PUID/PGID（默认 1000:1001 = NAS youruser:Users）
# 用 usermod/groupmod 把它改成目标 uid/gid，然后 gosu app 降权。
RUN set -eux; \
    groupadd --gid 999 app; \
    useradd --uid 999 --gid 999 --home-dir /home/app --create-home --shell /usr/sbin/nologin app

# ---- 应用代码 ---------------------------------------------------------------
# 硬链接与重命名全部由应用自己的代码承担（app/processors/hardlink.py + engine.py），
# 行为由项目自己的回归测试守卫。
WORKDIR /srv/mlm
COPY app/ /srv/mlm/app/
COPY web/ /srv/mlm/web/

# 代码属主交给 app（真正运行时的 uid 由 entrypoint 再纠正一次）
RUN chown -R app:app /srv/mlm

# ---- 数据卷 -----------------------------------------------------------------
# /data 持久化 SQLite、任务日志、硬链接日志、secret.key。
# compose 里用宿主绝对路径:/data 绑定，保证状态不落在容器层。
# /data/config 是**配置文件镜像目录**（libraries.yaml + processors/*.yaml，
# 见 PROCESSORS.md §6.1）：运行期真源仍是 SQLite，这里是可移植镜像，宿主可直接编辑。
# 这里先建好并交给 app，entrypoint 启动时还会再确认一次可写（healthcheck 的 checks.config）。
RUN set -eux; \
    mkdir -p /data /data/config; \
    chown -R app:app /data

# ---- 运行期环境变量（可被 compose 覆盖）------------------------------------
# TZ 与 PORT 已在上面的 ENV 块里设过默认值（TZ=Asia/Shanghai、PORT=18081），此处不重复。
# ⚠️ 这里**没有** CONFIG_SYNC_INTERVAL_SECONDS，是刻意的：应用不读这个 env
#    （配置同步周期唯一真源是 DB 设置键 `config_sync_interval_seconds`，默认 5 秒、
#    区间 0–86400、0 = 关闭外部导入，WebUI 设置页 / PUT /api/settings 可改）。
#    留一个同名 env 会让人以为改了周期、实际不生效 —— 那是最难排查的一类问题。
# ⚠️ 这里**不**内置 `HOST_PATH_MAP`：它的默认值必须是**空表（恒等映射）**
#    （`app/config.py` 的 `DEFAULT_HOST_PATH_MAP = {}`，见 ARCHITECTURE §6.1 不变量）。
#    理由：这个 ENV 若写死
#    `{"/downloads":"/vol1/1000/Download/qbittorrent","/library":"/vol1/1000/Library",…}`，
#    而**容器路径 = 宿主路径**（共同父目录单挂载）时，那份映射立刻变成毒药：
#    `app/paths.py` 的 `to_container()` 规则 2（宿主前缀反查）会把用户在 WebUI 里填的
#    `/vol1/1000/Download/qbittorrent/Bangumi` 改写成容器里并不存在的 `/downloads/Bangumi`，
#    随即撞上 `assert_allowed()`：`源目录 /downloads/Bangumi 不在允许的根路径内`。
#    更隐蔽的是：**只有落在映射值前缀下的路径才会中招**，fixture 放在别处时全套验收照样全绿。
#    非恒等部署（容器路径 ≠ 宿主路径）才由 compose / `.env` 显式设置它。
ENV DATA_DIR=/data \
    CONFIG_DIR=/data/config \
    PUID=1000 \
    PGID=1001

# 文档性声明；真实端口由上面 ENV 的 PORT 决定（改 .env 里的 PORT 时，
# compose 的 ports 映射也必须一起改，否则宿主端口与容器端口不一致）。
EXPOSE 18081

# ---- 健康检查 ---------------------------------------------------------------
# 走 /api/system/health（无需认证），用 python 标准库 urllib，不额外装 curl。
# 脚本优先使用容器自带的 python，缺失时回退到 wget/busybox。
HEALTHCHECK --start-period=20s --interval=30s --timeout=10s --retries=3 \
    CMD ["/bin/sh", "/usr/local/bin/mlm-healthcheck"]

# ---- 入口 -------------------------------------------------------------------
# 启动脚本安装到 /usr/local/bin（不在应用根里，避免与业务代码混放）。
COPY docker/entrypoint.sh /usr/local/bin/mlm-entrypoint
COPY docker/healthcheck.sh /usr/local/bin/mlm-healthcheck
RUN chmod 0755 /usr/local/bin/mlm-entrypoint /usr/local/bin/mlm-healthcheck

# 以 root 启动，只为做 uid/gid 与属主修复；entrypoint 内部立刻 gosu 降权。
# 不写 USER app：那会跳过 entrypoint 的全部修复逻辑。
ENTRYPOINT ["/usr/local/bin/mlm-entrypoint"]

# ---- 启动命令 ---------------------------------------------------------------
# ⚠️ PORT 必须是"真生效"的，所以这里**不能**用 exec 形式的 ["uvicorn", …, "--port", "18081"]：
#    exec 形式不经过 shell，${PORT} 不会被展开，改 .env 里的 PORT 后 uvicorn 仍绑 18081，
#    而 healthcheck.sh 会去请求新端口 → 容器永远 unhealthy，且日志打印的端口与真实端口不符。
#    因此用 sh -c 让 shell 展开 ${PORT:-18081}。
# ENTRYPOINT 的 `exec gosu "$APP_USER" "$@"` 会把 ["sh","-c","…"] 原样传给 gosu，
#    最终 uvicorn 仍是 app 用户、仍是 PID 1（exec 链上没有多余 shell 残留）。
# --workers 1 是**架构要求**：日志是"整份读—改—写"的，
#    多 worker 会互相覆盖。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-18081}\" --workers 1 --proxy-headers --forwarded-allow-ips '*' --no-server-header"]
