# 媒体入库整理 —— 工程文档入口

> 这是 `media-ingest` 工程的入口文档（源码、测试、文档、工具链都在本目录 `work/` 下）。
> 项目简介与最短安装流程见仓库根 [`../README.md`](../README.md)。

把 qBittorrent 等下载器收下来的文件，变成 Emby / Jellyfin 能直接扫描的媒体库：
**只建硬链接 + 按规则改名，不复制文件、不改动下载目录里的源文件。**
单 Docker 镜像交付：Python 3.11 + FastAPI + 原生 JS WebUI，SQLite（WAL）存状态，中文界面。

当前版本 **1.0.0**（变更见 [`CHANGELOG.md`](CHANGELOG.md)）。

---

## 1. 它做什么、不做什么

**做**：

- 在媒体库目录里为目标文件建**硬链接**（同一份数据的另一个名字），再按处理器规则**改名**；
- 下载完成由 qBittorrent 回调自动触发，漏掉时可开「扫描补偿」补回来；
- 每个作品的处理结果、失败原因、日志都能在 WebUI 里查到，可重试、可重做；
- 命名规则（命名段、目录正则、后缀白名单、集数正则）全部可编辑，可按媒体库单独覆盖。

**不做**：

- 不转码、不刮削、不改媒体文件的容器/编码；
- 不管理种子（做种仍由 qBittorrent 负责）；
- **绝不复制文件冒充硬链接**：跨挂载时明确失败并告警，不静默降级；
- 不修改下载目录里的任何文件（源树只读）。

两条硬性前提（违反会直接失败）：

1. **下载目录与媒体库必须在同一个挂载点内** —— 硬链接的内核限制（`linkat(2)` 比较挂载身份，跨挂载必 `EXDEV`）；
2. **只备份 `/data`** —— 运行期状态（数据库、配置副本、日志、密钥）全在这一个卷里。

## 2. 核心行为

| 阶段   | 做什么                                                                   | 实现                                           |
| ------ | ------------------------------------------------------------------------ | ---------------------------------------------- |
| 触发   | qBittorrent 下载完成回调 / 手动运行 / 目录扫描补偿 / API                 | `app/routers/tasks.py`、`app/routers/hooks.py` |
| 匹配   | 按媒体库的分类 / 标签 / 路径前缀匹配（**多个条件为 AND**，全空则不匹配） | `app/scanner.py`                               |
| 硬链接 | 在目标根下重建目录层级，逐文件 `os.link`；已处理且体积未变的文件夹跳过   | `app/processors/hardlink.py`                   |
| 重命名 | 按处理器规则（命名段 + 正则）改目标侧文件名                              | `app/processors/engine.py`                     |
| 记录   | 任务结果、逐文件链接记录、待办、日志                                     | `app/db.py`、`/data/logs/`                     |

- **幂等**：两个阶段各自记账（每库账本 `link_records` + 目标侧改名记录），重复触发不会重复建链/改名。
- **单工作线程串行执行**：回调只入队，避免两个任务同时改同一目录。
- **源完整性核对**：任务前后逐文件比对源目录（路径/size/ino/mtime），有差异即写 `Task.result.source_integrity` 并告警。

处理器规格、规则引擎语义、配置双向同步：契约真源 [`docs/contracts/PROCESSORS.md`](docs/contracts/PROCESSORS.md)。
架构、数据模型、API 契约：[`docs/contracts/ARCHITECTURE.md`](docs/contracts/ARCHITECTURE.md)、[`docs/contracts/API.md`](docs/contracts/API.md)。

## 3. 安装与首次使用

最快路径（详细说明见 [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §4；照着敲的完整演练见 [`examples/nas-deploy.md`](examples/nas-deploy.md)）：

```bash
cd /path/to/work
cp .env.example .env     # 至少改 PUID / PGID / MEDIA_ROOT
docker compose up -d --build
docker compose ps        # 等 STATUS 出现 (healthy)
```

然后浏览器打开 `http://<主机IP>:18081`，按向导创建管理员。

- `.env` 每一项的含义 → [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §4.2
- 建第一个媒体库（番剧 / 电影 / 自定义三个例子）→ [`docs/guides/USER_GUIDE.md`](docs/guides/USER_GUIDE.md) §3–§4
- 接 qBittorrent 回调 → [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §5

## 4. 日常使用

| 要做的事                                             | 看哪里                                                                                                                         |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| 跑任务、看结果、重试、重做单个作品                   | [`docs/guides/USER_GUIDE.md`](docs/guides/USER_GUIDE.md) §6–§7                                                                 |
| 改命名规则 / 新建处理器 / 库级覆盖                   | [`docs/guides/USER_GUIDE.md`](docs/guides/USER_GUIDE.md) §5、[`docs/contracts/PROCESSORS.md`](docs/contracts/PROCESSORS.md) §3 |
| 设置页逐项说明（回调、白名单、扫描、孤儿清理、日志） | [`docs/guides/USER_GUIDE.md`](docs/guides/USER_GUIDE.md) §8                                                                    |
| 媒体库删了会怎样 / 幂等性边界                        | [`docs/guides/USER_GUIDE.md`](docs/guides/USER_GUIDE.md) §10–§11                                                               |

## 5. 卷挂载：为什么只能「一个共同父挂载」

`docker-compose.yml` **只有两条 bind mount**：

| 宿主路径                    | 容器路径                      | 说明                                                                         |
| --------------------------- | ----------------------------- | ---------------------------------------------------------------------------- |
| `${MEDIA_ROOT:-/vol1/1000}` | 与宿主一致（如 `/vol1/1000`） | **单一共同父挂载**：下载目录与媒体库都在它下面，源与目标因此落在同一个挂载内 |
| `…/media-ingest/data`       | `/data`                       | 数据库、配置副本、日志、密钥                                                 |

- 为什么必须这样：`linkat(2)` 先比较**挂载身份**，不同挂载一律 `EXDEV`，与 `st_dev` 是否相同无关。
- 所以**不要**把源与目标拆成两条 bind —— 那样硬链接永远建不出来，且应用不会退化成复制。
- 库编辑页的「验证」会做**实证 link 探针**，返回三态：`true`（可建）/ `false`（跨挂载）/ `null`（无法实证）。
- 原理、自查命令与排错： [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §2、§3；
  兼容性事实（含 btrfs 子卷等边界）： [`docs/contracts/COMPATIBILITY.md`](docs/contracts/COMPATIBILITY.md) §2。

运行期数据与日志布局：

```
/data/
├── mlm.db                  SQLite：媒体库、任务、链接记录、待办、设置
├── secret.key              会话签名密钥
├── config/                 libraries.yaml + processors/*.yaml（与 DB 双向同步）
└── logs/                   hard_links.log + 每个媒体库一条重命名日志
```

## 6. 运维

| 操作                                | 看哪里                                                               |
| ----------------------------------- | -------------------------------------------------------------------- |
| 备份 / 恢复                         | [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §7          |
| 升级 / 回滚                         | [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §6          |
| 健康检查与 `unhealthy` 定位         | [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §4.4–§4.4.1 |
| 非 root 运行（PUID/PGID）、权限报错 | [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §10         |
| 常用命令速查                        | [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §11         |

注意：卸载（`docker compose down`）**不会删除**媒体库里的硬链接；想要「重来」需自己删数据目录。

## 7. 出问题时

| 症状                                  | 先看                                                                                                                                |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| 容器起不来 / `unhealthy`              | [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §4.4.1、[`docs/guides/TROUBLESHOOTING.md`](docs/guides/TROUBLESHOOTING.md) |
| 建链失败 `Invalid cross-device link`  | [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §2.1、§3                                                                   |
| 回调没触发 / 种子不自动入库           | [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) §5、[`docs/guides/USER_GUIDE.md`](docs/guides/USER_GUIDE.md) §9            |
| 满屏 `File exists` / 明明处理过却重扫 | [`docs/guides/TROUBLESHOOTING.md`](docs/guides/TROUBLESHOOTING.md)                                                                  |
| 中文乱码 / 编码问题                   | [`docs/guides/TROUBLESHOOTING.md`](docs/guides/TROUBLESHOOTING.md)                                                                  |
| 已确认的限制与不修项                  | [`docs/meta/KNOWN_ISSUES.md`](docs/meta/KNOWN_ISSUES.md)                                                                            |

## 8. 开发、测试与发布

```bash
# 全量测试（本机）：需要 Python 3.11+ 与 pytest，不需要 docker
python -m pytest tests/ -q
# 容器内跑（推荐，与发布口径一致）
sh scripts/pytest-in-container.sh

# 无 pytest 环境时的降级运行器（纯标准库）
python tests/run_all.py
```

- 工具链（构建镜像、起测试容器、全量验收、语法体检、备份归档）→ [`scripts/README.md`](scripts/README.md)
- 目录结构与命名约定 → [`docs/meta/STRUCTURE.md`](docs/meta/STRUCTURE.md)
- 行为契约、测试映射、兼容性事实 → [`docs/contracts/`](docs/contracts/)
- 版本与验收记录 → [`CHANGELOG.md`](CHANGELOG.md)、[`reports/`](reports/)

**改动文档时注意**：`docs/contracts/` 下文件的路径与章节号被代码注释与测试引用（如 `PROCESSORS.md` §3、`BASELINE.md` §4），
重排章节前先全仓搜索引用；`CHANGELOG.md` 是版本变更的唯一真源。

## 9. 文档导航

| 读者        | 文档                                                                                                                                                                                                               |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 使用        | [`docs/guides/`](docs/guides/)：`USER_GUIDE.md`（WebUI）、`DEPLOYMENT.md`（部署/升级/备份）、`TROUBLESHOOTING.md`、`FAQ.md`                                                                                        |
| 维护 / 二开 | [`docs/contracts/`](docs/contracts/)：`ARCHITECTURE.md`、`API.md`、`PROCESSORS.md`、`BASELINE.md`、`COMPATIBILITY.md`；[`docs/meta/`](docs/meta/)：`STRUCTURE.md`、`TEST-MAP.md`、`KNOWN_ISSUES.md`、`EVIDENCE.md` |
| 版本历史    | [`CHANGELOG.md`](CHANGELOG.md)、[`reports/`](reports/)                                                                                                                                                             |

全部文档索引与阅读顺序：[`docs/README.md`](docs/README.md)。

## 10. 许可

MIT，版权声明见仓库根 [`../LICENSE`](../LICENSE)。
