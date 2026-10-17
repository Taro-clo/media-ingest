# 媒体入库整理（Media Ingest）

把 qBittorrent 等下载器收下来的文件，变成 Emby / Jellyfin 能直接扫描的媒体库：
**只建硬链接 + 按规则改名，不复制文件、不改动下载目录里的源文件。**

单 Docker 镜像交付（后端 FastAPI + 原生 JS WebUI，中文界面），当前版本 **1.0.0**。
提供预构建多架构镜像（x86_64 / arm64），**两条命令即可部署**，不克隆源码、不需要本地构建。

## 它解决什么问题

- 下载目录里的文件名五花八门（`[某字幕组] 番剧 - 01 [1080p].mkv`），Emby / Jellyfin 认不出来；
- 直接在下载目录里整理又会破坏做种（qBittorrent 靠原路径与原文件名做种）；
- 手动改名、建链接，一次几百个文件，容易漏、也容易改错。

本项目的做法：在媒体库目录里为目标文件建立**硬链接**（同一份数据的另一个名字），再按规则改名。
源文件一个字节都不动，做种不受影响；媒体库与下载目录共享同一份磁盘空间，不会占双份。

## 工作原理

```
/vol1/1000/Download/qbittorrent/番剧/Season 01/xxx.mkv   ← 源文件（保持原样）
                                     │ 硬链接 + 改名
                                     ▼
/vol1/1000/Library/番剧/番剧 S01E01.mkv                  ← 同一 inode 的另一个名字
```

## 功能

- **硬链接入库**：媒体库里的文件与源文件是同一份数据；源树全程只读。
- **可配置的命名规则**：内置番剧 / 电影处理器，命名段、季集解析、特典识别都可在 WebUI 里改，配置与数据库双向同步。
- **自动触发**：qBittorrent 下载完成回调触发入库；漏掉回调时可用「扫描补偿」补回来。
- **任务可追踪**：每个作品一条记录，能看到处理了哪些文件、失败原因，可重试、可重做单个作品。
- **幂等**：重复运行不会重复建链或重复改名，可安全重跑。
- **中文 WebUI**：媒体库、任务、处理器、设置、待办看板。

## 运行要求

| 项 | 要求 |
|---|---|
| 系统 | 任意 Linux（NAS、服务器、PC），x86_64 或 arm64 |
| 运行时 | Docker 20.10+ 与 Docker Compose v2 |
| 镜像 | 预构建多架构镜像 `ghcr.io/taro-clo/media-ingest`（linux/amd64、linux/arm64，按宿主自动选择）；镜像部署无需本地构建 |
| **关键前提** | **下载目录与媒体库必须在同一个挂载点内** —— 硬链接的内核限制；拆成两个挂载会报 `Invalid cross-device link` |
| 端口 | 默认 `18081`（可改） |
| 权限 | 容器内以非 root 用户运行；`PUID`/`PGID` 默认 `auto`，按媒体目录属主自动识别，一般不用配 |
| 可选 | qBittorrent（自动触发）；Emby / Jellyfin（消费媒体库） |

## 快速开始（镜像部署）

不需要克隆源码、不需要本机构建。三步走（下面的示例路径都换成你自己的）：

**1. 创建容器数据存储目录**（`mlm.db`、日志、`secret.key`、`config` 都写在这里；位置随意，示例仅供参考）：

```bash
mkdir -p /你的/路径/media-ingest-data
```

**2. 下载 docker-compose**（或者把下面折叠的内容复制成 `docker-compose.release.yml`）：

```bash
mkdir -p /你的/路径/media-ingest && cd /你的/路径/media-ingest
curl -fsSLO https://raw.githubusercontent.com/Taro-clo/media-ingest/v1.0.0/work/docker-compose.release.yml
```

<details>
<summary>或者从 README 复制完整内容自行创建（点开）</summary>

```yaml
# =============================================================================
# media-ingest —— docker-compose.release.yml（镜像部署）
# =============================================================================
# 用预构建镜像直接跑：不需要源码、不需要本地构建。安装分三步（详见 docs/guides/DEPLOYMENT.md §4.6）：
#
#   1. 创建容器数据目录（SQLite、日志、secret.key、config 都写这里；路径随意，按自己的环境建）：
#        mkdir -p /你的/路径/media-ingest-data
#
#   2. 保存本文件（也可以从仓库 README 复制内容自行创建）：
#        https://raw.githubusercontent.com/Taro-clo/media-ingest/v1.0.0/work/docker-compose.release.yml
#
#   3. 按需改下面的挂载目录（见 ✏️ 标记），然后启动：
#        docker compose -f docker-compose.release.yml up -d
#      `docker compose -f docker-compose.release.yml ps` 出现 (healthy) 即成功；
#      浏览器打开 http://<本机 IP>:18081 建管理员、建媒体库。
#
# 挂载目录怎么改：本文件的路径全部引用同一个变量，**只改一处即可** ——
# 在**本文件同目录**建一个 `.env`（compose 自动读取，仅用于 ${VAR} 插值）：
#     MEDIA_ROOT=/你的/下载目录与媒体库的共同父目录    # 必改
#     MLM_DATA_DIR=/你的/路径/media-ingest-data       # 可选；默认是本文件同目录的 ./data
#     PORT=18081                                      # 可选
# 也可以直接改下面 environment/volumes 里的默认值（那样三处要一起改，否则路径不一致）。
#
# 其它都有自适应默认值：PUID/PGID=auto（取媒体挂载属主）、数据卷=./data、
# 首次建库的源/目标根=<MEDIA_ROOT>/Download 与 <MEDIA_ROOT>/Library。
#
# 部署前必读：**下载目录与媒体库必须落在同一个挂载里**（硬链接不能跨挂载）。
# 所以这里只挂一条"共同父目录"，且容器内路径 = 宿主路径：
#     - ${MEDIA_ROOT:-/vol1/1000}:${MEDIA_ROOT:-/vol1/1000}
# ⚠️ 不要拆成 /downloads、/library 两条 bind：那样硬链接必然失败（EXDEV）。
# ⚠️ MEDIA_ROOT 必须是宿主上**真实存在**的目录，否则 Docker 会以 root 新建空目录。
# ⚠️ 数据卷（MLM_DATA_DIR）只要求可写，不要求和媒体同一文件系统。
#
# 版本：默认钉住 1.0.0；要跟随最新发布设 MLM_VERSION=latest。
# 镜像在 GHCR（GitHub Container Registry），多架构（linux/amd64、linux/arm64）：
#     ghcr.io/taro-clo/media-ingest:1.0.0
# =============================================================================

name: media-ingest

services:
  media-ingest:
    image: "ghcr.io/taro-clo/media-ingest:${MLM_VERSION:-1.0.0}"
    container_name: media-ingest

    # 单进程运行：硬链接日志是"读-改-写整份"，多 worker 会互相覆盖。
    restart: unless-stopped

    environment:
      # 运行身份：默认 auto = 按媒体共同父挂载的属主自动识别（能读下载、能写媒体库的
      # 那个用户），不需要去宿主执行 id。要固定就显式写数字，例如 PUID=1000 PGID=1000。
      PUID: "${PUID:-auto}"
      PGID: "${PGID:-auto}"
      # 时区与字符集（写中文日志依赖 UTF-8 locale，不要改成 C/POSIX）。
      TZ: "${TZ:-Asia/Shanghai}"
      LANG: "C.UTF-8"
      PYTHONUTF8: "1"
      PYTHONIOENCODING: "utf-8"
      # 数据目录（容器内）：SQLite、任务日志、库日志、secret.key、config。
      DATA_DIR: "/data"
      CONFIG_DIR: "/data/config"
      # 首次建库时"允许的源/目标根目录"的初值：默认跟随 MEDIA_ROOT 派生
      # （<MEDIA_ROOT>/Download 与 <MEDIA_ROOT>/Library），换根目录不用另填。
      # 只用于 DB 首次播种，已部署实例请在 WebUI「设置」页修改（改环境变量不生效）。
      DEFAULT_SOURCE_ROOT: "${DEFAULT_SOURCE_ROOT:-${MEDIA_ROOT:-/vol1/1000}/Download}"
      DEFAULT_TARGET_ROOT: "${DEFAULT_TARGET_ROOT:-${MEDIA_ROOT:-/vol1/1000}/Library}"
      # 容器内监听端口；必须与下面 ports 的容器侧一致，故两者用同一个表达式。
      PORT: "${PORT:-18081}"
      # ✏️ 挂载目录（媒体共同父目录；容器内路径 = 宿主路径）。改法见文件头，只改一处即可。
      MEDIA_ROOT: "${MEDIA_ROOT:-/vol1/1000}"
      # 可选：会话签名密钥（留空 → 首次启动生成到 /data/secret.key）。
      SECRET_KEY: "${SECRET_KEY:-}"
      # 可选：首次启动自动创建管理员（都留空 → 首次打开 WebUI 时引导创建）。
      ADMIN_USERNAME: "${ADMIN_USERNAME:-}"
      ADMIN_PASSWORD: "${ADMIN_PASSWORD:-}"
      # HOST_PATH_MAP 故意不设：单挂载形态下容器路径 = 宿主路径，
      # 空映射（应用默认）就是恒等换算。只有旧式 /downloads + /library 双 bind
      # 部署才需要显式给 JSON 映射。

    ports:
      # 宿主端口 : 容器端口，两侧都取 ${PORT:-18081}，改端口只需改一处。
      - "${PORT:-18081}:${PORT:-18081}"

    volumes:
      # ✏️ 1. 媒体：**单一共同父挂载**（硬链接可用的前提）。容器内路径 = 宿主路径。
      #    改法见文件头（在 .env 里设 MEDIA_ROOT，只改一处）。
      - "${MEDIA_ROOT:-/vol1/1000}:${MEDIA_ROOT:-/vol1/1000}"
      # ✏️ 2. 应用数据：可写持久化（SQLite、日志、secret.key、config）。
      #    默认放 compose 文件同目录的 ./data（任意环境都可写，不污染根目录）；
      #    想用第 1 步建好的目录就设 MLM_DATA_DIR=/绝对路径。
      - "${MLM_DATA_DIR:-./data}:/data"

    # 健康检查沿用镜像 Dockerfile 里那一条（六项 checks 的解析也在镜像里）。

    # 日志轮转：避免长期运行把磁盘写满。
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

</details>

> 折叠里的内容与仓库文件 `work/docker-compose.release.yml` 一致，以仓库文件为准。

**3. 按需设置挂载目录**（只改一处）：在 compose 同目录建 `.env`（compose 会自动读取，只用于插值）：

```bash
cat > .env <<'EOF'
# 必改：下载目录与媒体库的共同父目录（宿主上必须已存在，下载/媒体库都在它下面）
MEDIA_ROOT=/你的/下载目录与媒体库的共同父目录
# 可选：用第 1 步创建的目录（不写 = 本目录下的 ./data）
MLM_DATA_DIR=/你的/路径/media-ingest-data
# 可选：18081 被占用时改（宿主与容器端口同步跟随）
# PORT=18099
EOF
```

也可以直接改 compose 里对应的默认值（`${MEDIA_ROOT:-…}`、`${MLM_DATA_DIR:-…}`），改的话同一变量涉及的几处要一起改。然后启动：

```bash
docker compose -f docker-compose.release.yml up -d
docker compose -f docker-compose.release.yml ps      # STATUS 出现 (healthy) 即成功
```

默认值（不建 `.env` 也能跑，但媒体目录会按 `/vol1/1000` 找）：

| 项 | 默认 | 覆盖方式（`.env` 或 compose 默认值） |
|---|---|---|
| 镜像版本 | `1.0.0` | `MLM_VERSION=latest` |
| 媒体共同父目录 | `/vol1/1000` | `MEDIA_ROOT=/你的/路径` |
| 数据目录（`/data`） | compose 同目录的 `./data` | `MLM_DATA_DIR=/你的/路径/data` |
| 运行身份 | `auto`（按媒体目录属主识别） | `PUID=1000 PGID=1000` |
| 源 / 目标根 | `<MEDIA_ROOT>/Download`、`<MEDIA_ROOT>/Library`（仅首次建库播种） | `DEFAULT_SOURCE_ROOT=…` |
| 端口 | `18081` | `PORT=18099` |
| 路径映射 | 空 = 恒等 | 容器内路径 ≠ 宿主路径时才需要 `HOST_PATH_MAP` |

- `MEDIA_ROOT` 必须是宿主上**已存在**的目录，且下载目录与媒体库都在它下面（硬链接的硬前提）。
- 启动后浏览器打开 `http://<你的 IP>:18081`，按向导创建管理员，再新建第一个媒体库。
- 想从源码构建（改代码 / 访问不了 GHCR）：见下方「快速开始（从源码构建）」。

> 失败排查、升级回滚与备份见 [`work/docs/guides/DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md)；
> 从零开始的完整演练见 [`work/examples/nas-deploy.md`](work/examples/nas-deploy.md)。

## 快速开始（从源码构建）

适合需要改代码、或无法访问 GHCR 的环境。以 NAS 为例（下面的宿主路径按你的实际环境修改）：

1. 把 `work/` 整个目录（或发布源码包解出的内容）放到 NAS，例如 `/vol1/1000/Docker/media-ingest`；
   确认 `.env` 里的 `MEDIA_ROOT`（下载目录与媒体库的共同父目录，默认 `/vol1/1000`）在宿主上**已存在**。
2. 复制配置，只按需求改 `MEDIA_ROOT`（其余默认值都能用：`PUID/PGID=auto` 自动识别、数据目录 `./data`、源/目标根从 `MEDIA_ROOT` 派生）：
   ```bash
   cd /vol1/1000/Docker/media-ingest
   cp .env.example .env
   vi .env          # 通常只需改 MEDIA_ROOT
   ```
3. 构建并启动（首次构建需要几分钟；数据目录默认建在部署目录下的 `./data`，想放别处就在 `.env` 里设 `MLM_DATA_DIR`）：
   ```bash
   docker compose up -d --build
   ```
4. 等容器变健康：
   ```bash
   docker compose ps          # STATUS 列里要出现 (healthy)
   ```
5. 浏览器打开 `http://<NAS 的 IP>:18081`，按向导创建管理员，然后新建第一个媒体库。

> 逐条命令、预期输出的完整演练见 [`work/examples/nas-deploy.md`](work/examples/nas-deploy.md)；
> 每个选项的详细说明见 [`work/docs/guides/DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md)。

## 第一次使用

- 建库前 5 分钟自检（源 / 目标同挂载、目录可读写）→ [`work/docs/guides/USER_GUIDE.md`](work/docs/guides/USER_GUIDE.md) §3
- 新建番剧库 / 电影库 / 自定义库的完整例子 → [`USER_GUIDE.md`](work/docs/guides/USER_GUIDE.md) §4
- `.env` 每一项的含义与默认值 → [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §4.2
- 接上 qBittorrent，下载完成自动入库 → [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §5

## 数据、备份与升级

- 运行期数据都在 `/data` 卷里：`mlm.db`、`secret.key`、`config/`、`logs/`。**这是唯一必须备份的东西**，换机器时迁走它即可。
- 备份与恢复 → [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §7
- 升级与回滚 → [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §6

## 出问题时

| 症状 | 先看 |
|---|---|
| 容器起不来 / 显示 `unhealthy` | [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §4.4.1、[`TROUBLESHOOTING.md`](work/docs/guides/TROUBLESHOOTING.md) |
| 硬链接失败（`Invalid cross-device link`） | [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §2.1、§3 |
| `Permission denied` / 属主问题 | [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §10 |
| 下载完成但没触发入库 | [`USER_GUIDE.md`](work/docs/guides/USER_GUIDE.md) §9、[`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §5 |
| 已确认的限制与不修项 | [`work/docs/meta/KNOWN_ISSUES.md`](work/docs/meta/KNOWN_ISSUES.md) |

## 文档导航

| 读者 | 文档 |
|---|---|
| 使用 | [`work/docs/guides/`](work/docs/guides/)：`USER_GUIDE.md`（WebUI 使用）、`DEPLOYMENT.md`（部署 / 升级 / 备份）、`TROUBLESHOOTING.md`、`FAQ.md` |
| 二开 / 维护 | [`work/docs/contracts/`](work/docs/contracts/)：`ARCHITECTURE.md`、`API.md`、`PROCESSORS.md`、`BASELINE.md`、`COMPATIBILITY.md`；[`work/docs/meta/`](work/docs/meta/)：`STRUCTURE.md`、`TEST-MAP.md`、`KNOWN_ISSUES.md`、`EVIDENCE.md`；[`work/scripts/README.md`](work/scripts/README.md) |
| 版本与验收 | [`work/CHANGELOG.md`](work/CHANGELOG.md)、[`work/reports/`](work/reports/) |

全部文档索引与阅读顺序：[`work/docs/README.md`](work/docs/README.md)。

## 仓库结构

```
media-ingest/
├── work/                现行工程：源码、测试、文档、脚本、报告
│   ├── app/  web/       后端与前端
│   ├── docker/          Dockerfile、entrypoint、healthcheck
│   ├── docs/            文档（guides / contracts / meta）
│   ├── examples/        可直接抄的部署示例与自检脚本
│   ├── scripts/         构建 / 测试 / 验收 / 归档工具链
│   └── reports/         验收报告
└── README.md            本文件
```

镜像名 / 容器名 / 数据目录布局等命名约定见 [`work/docs/meta/STRUCTURE.md`](work/docs/meta/STRUCTURE.md)。

## 版本与许可

- 当前版本 **1.0.0**；变更记录见 [`work/CHANGELOG.md`](work/CHANGELOG.md)。
- [MIT](LICENSE) 许可：可自由复制、修改、商用、再分发，保留版权声明即可。
  软件按「原样」提供、不附带担保；它会在你的硬盘上建硬链接并改名，请先在测试数据上试。
