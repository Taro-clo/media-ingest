# 媒体入库整理（Media Ingest）

把 qBittorrent 等下载器收下来的文件，变成 Emby / Jellyfin 能直接扫描的媒体库：
**只建硬链接 + 按规则改名，不复制文件、不改动下载目录里的源文件。**

单 Docker 镜像交付（后端 FastAPI + 原生 JS WebUI，中文界面），当前版本 **1.0.0**。
提供预构建多架构镜像（x86_64 / arm64），**三步即可部署**，不克隆源码、不需要本地构建；
镜像默认跟随最新发布（`latest`），需要固定版本时在 `.env` 里钉住（见步骤 3）。

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

| 项           | 要求                                                                                                               |
| ------------ | ------------------------------------------------------------------------------------------------------------------ |
| 系统         | 任意 Linux（服务器、PC、迷你主机等），x86_64 或 arm64                                                              |
| 运行时       | Docker 20.10+ 与 Docker Compose v2                                                                                 |
| 镜像         | 预构建多架构镜像 `ghcr.io/taro-clo/media-ingest`（linux/amd64、linux/arm64，按宿主自动选择）；镜像部署无需本地构建 |
| **关键前提** | **下载目录与媒体库必须在同一个挂载点内** —— 硬链接的内核限制；拆成两个挂载会报 `Invalid cross-device link`         |
| 端口         | 默认 `18081`（可改）                                                                                               |
| 权限         | 容器内以非 root 用户运行；`PUID`/`PGID` 默认 `auto`，按媒体目录属主自动识别，一般不用配                            |
| 可选         | qBittorrent（自动触发）；Emby / Jellyfin（消费媒体库）                                                             |

## 快速开始（镜像部署）

不需要克隆源码、不需要本机构建。三步走（下面的示例路径都换成你自己的）：

**1. 创建容器数据存储目录**（`mlm.db`、日志、`secret.key`、`config` 默认写在该目录下的 `data/`；位置随意，示例仅供参考）：

```bash
mkdir -p /你的/路径/media-ingest
```

**2. 创建 docker-compose**（放在上面建好的目录里）

方法一：执行命令从仓库拉取

```bash
cd /你的/路径/media-ingest
curl -fsSLO https://raw.githubusercontent.com/Taro-clo/media-ingest/main/work/docker-compose.release.yml
```

方法二：自行创建名为 `docker-compose.release.yml` 的文件（同样放在这个目录里），把下面整段内容复制进去

```yaml
name: media-ingest

# =============================================================================
# 挂载媒体父目录（下载目录与媒体库的共同父目录；容器内路径 = 宿主路径）：
#   * 必须填宿主上已存在的绝对路径；
#   * 下载目录与媒体库都在它下面（例如 下载文件在/vol1/1000/Download ，则把双引号内的内容改为 /vol1/1000，媒体库将创建在其之下）。
x-media-root: &media-root "${MEDIA_ROOT:-/vol1/1000}"

# 容器数据目录：
#   如果本文件在第 1 步建好的目录内则可以不改。
x-data-dir: &data-dir "${MLM_DATA_DIR:-./data}"

# WebUI 端口（未被占用时不必修改）。
x-port: &port "${PORT:-18081}"

# =============================================================================
# 如果不熟悉compose写法，以下内容都不必修改。

services:
  media-ingest:
    image: "ghcr.io/taro-clo/media-ingest:${MLM_VERSION:-latest}"
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
      # 源/目标目录不在这里配置：新建媒体库时在 WebUI 里自己填（容器内路径 = 宿主路径），
      # 保存时会校验两者在同一挂载；应用不再有「允许的源/目标根目录」这类全局设置，
      # 也不会从 MEDIA_ROOT 派生目录名 —— 你的目录叫什么、挂在哪都行，只要容器看得到。
      # 容器内监听端口（与下面 ports 用同一个锚点）。
      PORT: *port
      # ✏️ 挂载目录（媒体共同父目录；容器内路径 = 宿主路径）。改法见文件头，只改一处即可。
      MEDIA_ROOT: *media-root
      # 可选：会话签名密钥（留空 → 首次启动生成到 /data/secret.key）。
      SECRET_KEY: "${SECRET_KEY:-}"
      # 可选：首次启动自动创建管理员（都留空 → 首次打开 WebUI 时引导创建）。
      ADMIN_USERNAME: "${ADMIN_USERNAME:-}"
      ADMIN_PASSWORD: "${ADMIN_PASSWORD:-}"
      # HOST_PATH_MAP 故意不设：单挂载形态下容器路径 = 宿主路径，
      # 空映射（应用默认）就是恒等换算。只有旧式 /downloads + /library 双 bind
      # 部署才需要显式给 JSON 映射。

    ports:
      # 宿主端口 : 容器端口，两侧都引用同一个锚点，改端口只需改文件头一处。
      - target: *port
        published: *port

    volumes:
      # ✏️ 1. 媒体：**单一共同父挂载**（硬链接可用的前提）。容器内路径 = 宿主路径。
      - type: bind
        source: *media-root
        target: *media-root
      # ✏️ 2. 应用数据：可写持久化（SQLite、日志、secret.key、config）。
      - type: bind
        source: *data-dir
        target: /data

    # 健康检查沿用镜像 Dockerfile 里那一条（四项 checks 的解析也在镜像里）。

    # 日志轮转：避免长期运行把磁盘写满。
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

**3. 按需设置挂载目录、数据目录与端口**：

以记事本格式打开 compose 文件，修改开头的 `x-media-root`；

`x-data-dir` 和 `x-port` 可以按需修改，一般不改。

```yaml
# ===== 文件头三个锚点：通常只需要改这一段 =====
# 媒体共同父目录（下载目录与媒体库的共同父目录；必改）
x-media-root: &media-root "${MEDIA_ROOT:-/vol1/1000}"

# 容器数据目录：
#   如果本文件在第 1 步建好的目录内则可以不改。
x-data-dir: &data-dir "${MLM_DATA_DIR:-./data}"

# WebUI 端口（未被占用时不必修改）。
x-port: &port "${PORT:-18081}"
```

**① 媒体挂载 `x-media-root` —— 唯一必改项**

- 填「下载目录」和「媒体库」的**共同父目录**。例：下载在 `/vol1/1000/Download`、媒体库在 `/vol1/1000/Library`，就填 `/vol1/1000`。
- 必须是宿主上**已存在**的**绝对路径**。
- 容器内路径 = 宿主路径（compose 里 `source` 与 `target` 引用同一个锚点），所以填宿主真实路径，**不要**填 `/data`、`/media` 这类容器内的路径。
- 下载目录与媒体库必须在**同一个挂载点**内（硬链接不能跨挂载，否则报 `Invalid cross-device link`）。
- 源目录 / 目标目录在**新建媒体库时**自己填（容器内路径 = 宿主路径），保存时会校验两者在**同一个挂载**；应用不再有全局的「允许的源/目标根目录」设置，目录名也不限定叫 `Download` / `Library`。

**② 容器数据目录 `x-data-dir` —— 按需改**

- 指向第 1 步创建的目录（或它下面的 `data/`，默认是后者）。里面是 `mlm.db`（全部配置、媒体库、处理器、任务记录）、`secret.key`、`config/`、`logs/`。
- 只要求**可写且持久化**，不要求和媒体同一文件系统；**这是唯一必须备份的东西**，不要放 `/tmp` 这类临时目录。
- 建议放在媒体目录**之外**：避免被 Emby / Jellyfin 扫描，备份时也不会和媒体混在一起。

**③ WebUI 端口 `x-port` —— 按需改**

- 默认 `18081`，启动后访问 `http://<宿主 IP>:18081`。
- 端口被占用（`docker compose up` 报 `port is already allocated`，或宿主 `ss -ltn | grep 18081` 有输出）时改一个空闲端口，例如 `x-port: &port "18099"`。
- `ports` 映射与容器内 `PORT` 用同一个锚点，改一处两边同步；访问地址随之变成 `http://<宿主 IP>:18099`。
- 如果是带防火墙 / 安全组的服务器，记得放行该端口。

> 不想改本文件也可以：在 compose 同目录建 `.env`（可选，compose 自动读取）写 `MEDIA_ROOT=/你的路径`、`MLM_DATA_DIR=/你的/路径/media-ingest/data`、`PORT=18099`，或启动命令前加同名环境变量（`MEDIA_ROOT=/mnt/media docker compose -f docker-compose.release.yml up -d`）——两者都会覆盖锚点里的默认值，且所有引用一起生效。

然后启动并确认：

```bash
docker compose -f docker-compose.release.yml pull
docker compose -f docker-compose.release.yml up -d
docker compose -f docker-compose.release.yml ps      # STATUS 出现 (healthy) 即成功
```

**4. 浏览器打开 http://\<部署宿主机 的 IP>:18081，出现首次设置用户名和密码的界面即成功**

默认值（不改文件、不建 `.env` 也能跑，但媒体目录会按 `/vol1/1000` 挂载）：

| 项                  | 默认                         | 改法（改 compose 锚点，或设同名环境变量）                      |
| ------------------- | ---------------------------- | -------------------------------------------------------------- |
| 镜像版本            | `latest`（跟随最新发布）     | `MLM_VERSION=1.0.0`（钉住固定版本）                            |
| 媒体共同父目录      | `/vol1/1000`                 | `x-media-root`（或 `MEDIA_ROOT=/你的/路径`）                   |
| 数据目录（`/data`） | compose 同目录的 `./data`    | `x-data-dir`（或 `MLM_DATA_DIR=/你的/路径/media-ingest/data`） |
| 运行身份            | `auto`（按媒体目录属主识别） | `PUID=1000 PGID=1000`                                          |
| 端口                | `18081`                      | `x-port`（或 `PORT=18099`）                                    |
| 路径映射            | 空 = 恒等                    | 容器内路径 ≠ 宿主路径时才需要 `HOST_PATH_MAP`                  |

- `MEDIA_ROOT` 必须是宿主上**已存在**的目录，且下载目录与媒体库都在它下面（硬链接的硬前提）。
- 镜像默认 `latest`：每次发布都会更新，重装 / 重建容器时可能拿到新版本；想钉住某次发布就设 `MLM_VERSION=1.0.0`（改锚点、写 `.env` 或命令前加变量都行；版本号见 [Releases](https://github.com/Taro-clo/media-ingest/releases) 与 [`work/CHANGELOG.md`](work/CHANGELOG.md)）。
- 升级（compose 从 `main` 下载时）：重新执行步骤 2 方法一的下载命令，再 `docker compose -f docker-compose.release.yml pull && up -d`，compose 与镜像一起更新；数据目录原样保留。
- 启动后浏览器打开 `http://<你的 IP>:18081`（改过 `PORT` 就用新端口），按向导创建管理员，再新建第一个媒体库。
- 想从源码构建（改代码 / 访问不了 GHCR）：见下方「快速开始（从源码构建）」。

> 失败排查、升级回滚与备份见 [`work/docs/guides/DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md)；
> 从零开始的完整演练见 [部署实战演练](work/examples/deploy-walkthrough.md)。

## 快速开始（从源码构建）

下面的宿主路径按你的实际环境修改：

1. 把 `work/` 整个目录（或发布源码包解出的内容）放到宿主机，例如 `/vol1/1000/Docker/media-ingest`；
   确认 `.env` 里的 `MEDIA_ROOT`（下载目录与媒体库的共同父目录，默认 `/vol1/1000`）在宿主上**已存在**。

2. 复制配置，只按需求改 `MEDIA_ROOT`（其余默认值都能用：`PUID/PGID=auto` 自动识别、数据目录 `./data`；源/目标目录在新建媒体库时自己填）：

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

5. 浏览器打开 `http://<宿主机 IP>:18081`，按向导创建管理员，然后新建第一个媒体库。

> 逐条命令、预期输出的完整演练见 [部署实战演练](work/examples/deploy-walkthrough.md)；
> 每个选项的详细说明见 [`work/docs/guides/DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md)。

## 第一次使用

- 建库前 5 分钟自检（源 / 目标同挂载、目录可读写）→ [`work/docs/guides/USER_GUIDE.md`](work/docs/guides/USER_GUIDE.md) §3
- 新建番剧库 / 电影库 / 自定义库的完整例子 → [`USER_GUIDE.md`](work/docs/guides/USER_GUIDE.md) §4
- 所有配置项（含 `.env` 可选覆盖）的含义与默认值 → [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §4.2
- 接上 qBittorrent，下载完成自动入库 → [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §5

## 数据、备份与升级

- 运行期数据都在 `/data` 卷里：`mlm.db`、`secret.key`、`config/`、`logs/`。**这是唯一必须备份的东西**，换机器时迁走它即可。
- 备份与恢复 → [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §7
- 升级与回滚 → [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §6

## 出问题时

| 症状                                      | 先看                                                                                                                  |
| ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| 容器起不来 / 显示 `unhealthy`             | [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §4.4.1、[`TROUBLESHOOTING.md`](work/docs/guides/TROUBLESHOOTING.md) |
| 硬链接失败（`Invalid cross-device link`） | [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §2.1、§3                                                            |
| `Permission denied` / 属主问题            | [`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §10                                                                 |
| 下载完成但没触发入库                      | [`USER_GUIDE.md`](work/docs/guides/USER_GUIDE.md) §9、[`DEPLOYMENT.md`](work/docs/guides/DEPLOYMENT.md) §5            |
| 已确认的限制与不修项                      | [`work/docs/meta/KNOWN_ISSUES.md`](work/docs/meta/KNOWN_ISSUES.md)                                                    |

## 文档导航

| 读者        | 文档                                                                                                                                                                                                                                                                                       |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 使用        | [`work/docs/guides/`](work/docs/guides/)：`USER_GUIDE.md`（WebUI 使用）、`DEPLOYMENT.md`（部署 / 升级 / 备份）、`TROUBLESHOOTING.md`、`FAQ.md`                                                                                                                                             |
| 二开 / 维护 | [`work/docs/contracts/`](work/docs/contracts/)：`ARCHITECTURE.md`、`API.md`、`PROCESSORS.md`、`BASELINE.md`、`COMPATIBILITY.md`；[`work/docs/meta/`](work/docs/meta/)：`STRUCTURE.md`、`TEST-MAP.md`、`KNOWN_ISSUES.md`、`EVIDENCE.md`；[`work/scripts/README.md`](work/scripts/README.md) |
| 版本与验收  | [`work/CHANGELOG.md`](work/CHANGELOG.md)、[`work/reports/`](work/reports/)                                                                                                                                                                                                                 |

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
