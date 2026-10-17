# =============================================================================
# media-ingest —— 应用镜像
# =============================================================================
# 构建（在仓库根目录执行）：
#     docker compose build
#   或  docker build -f docker/Dockerfile -t media-ingest:latest .
#
# 容器内目录（详见 docs/meta/STRUCTURE.md）：
#     /srv/mlm/app/                  Python 包（WORKDIR）
#     /srv/mlm/web/                  前端静态资源
#     /usr/local/bin/mlm-entrypoint  启动脚本（属主修复 → gosu 降权）
#     /usr/local/bin/mlm-healthcheck 健康检查脚本
#     /data/                         唯一持久化卷（DB、配置镜像、日志、secret.key）
# =============================================================================

FROM python:3.11-slim

# ---- 构建期元信息 -----------------------------------------------------------
LABEL org.opencontainers.image.title="NAS Media Library Manager" \
      org.opencontainers.image.description="为 qBittorrent 已完成的下载创建硬链接并按媒体库配置重命名（源树只读，绝不写回）" \
      org.opencontainers.image.source="media-ingest" \
      org.opencontainers.image.licenses="MIT"

# ---- 容器内语言环境 ---------------------------------------------------------
# 日志文件按进程 locale 写入，必须锁定 UTF-8，否则中文日志会乱码或报错。
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
# gosu: entrypoint 降权工具；tzdata: 让 TZ 生效；ca-certificates: 供将来 https 用。
# 不装 curl：健康检查用 python 标准库 urllib。
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        gosu \
        tzdata \
        ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# ---- Python 依赖 ------------------------------------------------------------
# 只装三个运行时依赖，其余全部是 Python 标准库。
# 三者都 pin 到有预编译 wheel 的版本，因此不需要 gcc / python3-dev 构建工具链。
RUN set -eux; \
    python -m pip install --no-cache-dir --upgrade pip; \
    python -m pip install --no-cache-dir \
        "fastapi==0.115.6" \
        "uvicorn[standard]==0.32.1" \
        "PyYAML==6.0.3"

# ---- 非 root 用户 -----------------------------------------------------------
# 镜像内先建占位 app 用户（999:999，避开宿主常见的 1000）；
# entrypoint 启动时按 PUID/PGID 用 usermod/groupmod 改成目标 uid/gid，再 gosu 降权。
RUN set -eux; \
    groupadd --gid 999 app; \
    useradd --uid 999 --gid 999 --home-dir /home/app --create-home --shell /usr/sbin/nologin app

# ---- 应用代码 ---------------------------------------------------------------
WORKDIR /srv/mlm
COPY app/ /srv/mlm/app/
COPY web/ /srv/mlm/web/

# 代码属主交给 app（运行时真实 uid 由 entrypoint 再纠正一次）
RUN chown -R app:app /srv/mlm

# ---- 数据卷 -----------------------------------------------------------------
# /data 持久化 SQLite、任务日志、硬链接日志、secret.key，以及配置镜像目录
# /data/config（libraries.yaml + processors/*.yaml）。
RUN set -eux; \
    mkdir -p /data /data/config; \
    chown -R app:app /data

# ---- 运行期环境变量（可被 compose 覆盖）------------------------------------
# TZ / PORT / DATA_DIR 已在上面的 ENV 块里设过默认值。
# ⚠️ 这里有意**没有** CONFIG_SYNC_INTERVAL_SECONDS：应用不读这个 env，
#    配置同步周期只由数据库设置键 `config_sync_interval_seconds` 决定。
# ⚠️ 这里有意**不**内置 HOST_PATH_MAP：默认必须是空表（恒等映射），
#    否则非恒等映射会把用户填写的宿主路径改错。非恒等部署才由 compose/.env 显式设置。
ENV DATA_DIR=/data \
    CONFIG_DIR=/data/config \
    PUID=auto \
    PGID=auto

# 文档性声明；实际端口由 ENV PORT 决定（compose 的 ports 需与之一致）。
EXPOSE 18081

# ---- 健康检查 ---------------------------------------------------------------
# 请求 /api/system/health 并解析 body 里的四项 checks（详见 docker/healthcheck.sh）。
HEALTHCHECK --start-period=20s --interval=30s --timeout=10s --retries=3 \
    CMD ["/bin/sh", "/usr/local/bin/mlm-healthcheck"]

# ---- 入口 -------------------------------------------------------------------
COPY docker/entrypoint.sh /usr/local/bin/mlm-entrypoint
COPY docker/healthcheck.sh /usr/local/bin/mlm-healthcheck
RUN chmod 0755 /usr/local/bin/mlm-entrypoint /usr/local/bin/mlm-healthcheck

# 以 root 启动仅为做 uid/gid 与属主修复；entrypoint 内部立刻 gosu 降权。
ENTRYPOINT ["/usr/local/bin/mlm-entrypoint"]

# ---- 启动命令 ---------------------------------------------------------------
# 必须用 sh -c 让 ${PORT} 在容器内展开：exec 形式不经过 shell，改 .env 的 PORT 后
# uvicorn 仍绑旧端口 → 容器永远 unhealthy。--workers 1 是架构要求（日志是整份读改写）。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-18081}\" --workers 1 --proxy-headers --forwarded-allow-ips '*' --no-server-header"]
