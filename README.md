# media-ingest —— 媒体入库整理（Media Ingest，原生实现）

把 qBittorrent 之类的下载目录变成 Emby / Jellyfin 能直接用的媒体库：
**只建硬链接 + 按规则改名，不复制、不改动源文件**。后端 FastAPI + 原生 JS WebUI，
以单个 Docker 镜像交付，在局域网 NAS（如飞牛 fnOS，任意 Linux NAS 均可）上运行。

## 命名

| 名称 | 值 | 说明 |
|---|---|---|
| 产品名（中文 / 英文） | **媒体入库整理** / **Media Ingest** | WebUI 标题、侧栏、日志、API 标题 |
| 仓库目录 | `media-ingest` | 仓库根目录名 |
| 镜像名 / compose 项目名 / 服务名 / 容器名 | `media-ingest` | `docker-compose.yml`、`examples/docker-compose.nas.yml`、构建与运维脚本 |
| 代码标识 | `service` / `container_name` = `media-ingest` | `GET /api/system/health` |
| 工程目录 | `work/` | 仓库内的现行工程源码根 |
| NAS 宿主目录（示例） | `/vol1/1000/Docker/media-ingest/…` | 示例部署目录：数据卷、`backups/` 都在它下面；可换成你的路径（文档与脚本的默认值取此路径） |
| 发布镜像 | `media-ingest:1.0.0`（同时打 `latest`） | 2026-10-16 首发 |

**不随命名改变**：`/data` 布局、`mlm.db` 与 `settings` 表、`MLM_*` 环境变量、`/srv/mlm` 镜像内路径、
`app` Python 包名、Git 仓库目录 `media-ingest`。

> 归档靠 git tag + 宿主备份目录里的 bundle / 源码包（示例路径
> `/vol1/1000/Docker/media-ingest/backups/`）—— `media-ingest-v1.0.0.bundle` + `.sha256`、
> `src-v1.0.0.tar.gz` + `.sha256`。
> 归档不入库；需要时从该备份目录取回。

## 现行工程：`work/`

仓库根只保留**现行工程** `work/`：源码、测试、文档、工具链与验收报告都在它下面。

| 入口 | 内容 |
|---|---|
| [`work/README.md`](work/README.md) | 工程总入口：功能、边界、目录、部署、运维、故障处理 |
| [`work/CHANGELOG.md`](work/CHANGELOG.md) | 版本变更记录（`[1.0.0]` 首发） |
| [`work/docs/contracts/ARCHITECTURE.md`](work/docs/contracts/ARCHITECTURE.md) | 架构与接口契约（改动需同步本文件与 CHANGELOG） |
| [`work/docs/contracts/BASELINE.md`](work/docs/contracts/BASELINE.md) | 行为基线（历史分析，不构成现行约束） |
| [`work/docs/meta/STRUCTURE.md`](work/docs/meta/STRUCTURE.md) | 目录结构与命名约定 |
| [`work/docs/`](work/docs/) | **文档三桶**：[`contracts/`](work/docs/contracts/)（AI 契约）、[`guides/`](work/docs/guides/)（用户手册）、[`meta/`](work/docs/meta/)（结构与证据）—— 导航见 [`work/docs/README.md`](work/docs/README.md) |
| [`work/scripts/README.md`](work/scripts/README.md) | 构建 / 起容器 / pytest / 全量验收 / 归档工具链 |
| [`work/reports/README.md`](work/reports/README.md) | 验收报告索引与「验收证据在哪」 |
| [`work/examples/`](work/examples/) | compose 样例、NAS 部署说明、部署资产自检脚本 |

## 当前状态

- **已发布**：**1.0.0**（首发）—— 镜像 `media-ingest:1.0.0`（同时打 `latest`），约 181 MB。
  容器内 pytest `525 passed / 7 skipped / 2 errors`（2 个 ERROR 是测试夹具误报），
  全量验收 `PASS=56 FAIL=0`；两个全量测试库（`bangumi` 8534 文件 → 54 作品、`movie` 3986 文件 → 45 作品）
  全部硬链接、复跑幂等、源树零改动。详见 [`work/CHANGELOG.md`](work/CHANGELOG.md) 的 `[1.0.0]` 小节。
- **tag**：**`v1.0.0`**。
- **归档**：`media-ingest-v1.0.0.bundle` 与 `src-v1.0.0.tar.gz`（各带 `.sha256`），放在宿主备份目录里
  （示例 `/vol1/1000/Docker/media-ingest/backups/`）。
- **一次性容器命名**：统一用 `media-ingest-*` 前缀
  （`media-ingest-test` / `media-ingest-pytest` / `media-ingest-minimal-repro` / `media-ingest-builder`）。
- **验收脚本需要环境变量**：`MLM_ADMIN_PW` / `MLM_QBT_PASS` 必须由环境变量传入，缺失即 `exit 2`
  （仓库内不含任何真实口令）。

## 最短上手路径

```bash
cd work
cp .env.example .env            # 改 MEDIA_ROOT 等
docker compose up -d            # 首次启动后在 WebUI 里 POST /api/auth/setup 建管理员
```

发版/验收流程（在部署宿主上跑，工具链说明见 `work/scripts/README.md`）：
`build-image.sh` → `run-container.sh` → `acceptance-test.py` → `pytest-in-container.sh` → `backup-to-nas.sh`。

## 已知的坑（源码打包与验收）

- **源码打包必须先用 LF**：Windows 上若开了 `core.autocrlf=true`，`git archive` 默认产出 CRLF，
  会让镜像内容与 Linux 端不一致。用
  `git -c core.autocrlf=false archive --format=tar.gz -o <out>.tgz HEAD:work`。
- **`acceptance-test.py` 必须在全新数据库上跑**：它只清媒体库与目标树，不清 DB 里的逐文件决策缓存；
  在同一份 DB 上复跑会得到 `linked 8534 / renamed 0`，看着像失败其实不是。
- **不要用 `sed -i "s/\r$//"` 处理这些脚本**：中文 Python 源码会被切掉字节
  （`import http.cookiejar` → `import http.cookieja`）。仓库文件本来就是 LF，直接跑即可。
- **`/data` 是唯一必须备份的东西**：`mlm.db`、`secret.key`、`config/{libraries.yaml,processors/*.yaml}`、`logs/`。
  全局设置（含 L3 特典判据 `extras_patterns`）存在 `mlm.db` 的 `settings` 表里，换设备要迁移 `/data`。

## 许可证

[MIT](LICENSE)。想用就用：复制、修改、改名、商用、再分发都不需要事先征求同意，唯一要求是保留
`LICENSE` 里的版权声明（`Copyright (c) 2026 The Media Ingest Authors`）。软件按「原样」提供、
不附带任何担保；它会在你的硬盘上建硬链接并改名，请先在自己的数据上试。
