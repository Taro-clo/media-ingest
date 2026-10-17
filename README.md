# 媒体入库整理（Media Ingest）

把 qBittorrent 等下载器收下来的文件，变成 Emby / Jellyfin 能直接扫描的媒体库：
**只建硬链接 + 按规则改名，不复制文件、不改动下载目录里的源文件。**

单 Docker 镜像交付（后端 FastAPI + 原生 JS WebUI，中文界面），当前版本 **1.0.0**。

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
| **关键前提** | **下载目录与媒体库必须在同一个挂载点内** —— 硬链接的内核限制；拆成两个挂载会报 `Invalid cross-device link` |
| 端口 | 默认 `18081`（可改） |
| 权限 | 容器内以非 root 用户（`PUID` / `PGID`）运行；需对下载目录可读、对媒体库可写 |
| 可选 | qBittorrent（自动触发）；Emby / Jellyfin（消费媒体库） |

## 快速开始

以 NAS 为例（下面的宿主路径按你的实际环境修改）：

1. 把 `work/` 整个目录（或发布源码包解出的内容）放到 NAS，例如 `/vol1/1000/Docker/media-ingest`；
   确认 `.env` 里的 `MEDIA_ROOT`（下载目录与媒体库的共同父目录，默认 `/vol1/1000`）在宿主上**已存在**。
2. 复制配置并修改三项：
   ```bash
   cd /vol1/1000/Docker/media-ingest
   cp .env.example .env
   vi .env          # 至少改 PUID / PGID / MEDIA_ROOT
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
