# 目录结构（权威说明）

> 本文回答三个问题：**仓库里什么放哪**、**容器里什么在哪**、**运行期数据落在哪**。
> 任何"这个文件该放哪"的疑问都以本文为准；新增目录前先看 §6 的决策表。
>
> 术语：**工作树** = git 仓库里的 `work/`（现行源码根）；**镜像** = `work/docker/Dockerfile`
> 构建出的容器镜像；**数据卷** = 宿主上挂给容器 `/data` 的目录。
> 文中 `/vol1/1000/...` 均为示例路径，按你的实际环境替换。

---

## 1. 三个目录域，别混淆

| 域 | 位置 | 归谁管 | 进 git？ | 进镜像？ |
|---|---|---|---|---|
| **A 工作树** | `work/` | 开发者 | 是 | 只有 `app/`、`web/`、`docker/` 被 `COPY` 进镜像 |
| **B 镜像** | 容器根文件系统（`/srv/mlm`、`/usr/local/bin`、`/data` 骨架） | Dockerfile | — | — |
| **C 数据卷** | 宿主 `<data>` ↔ 容器 `/data` | 应用运行期 | **否**（`.gitignore` 排除） | 否（镜像里只有空骨架） |

一句话：**A 是源码，B 是构建产物，C 是运行期状态**。C 里的东西（`mlm.db`、`config/`、`logs/`、
`secret.key`）永远不要放进 A；A 里的东西也不要在运行期改写。

---

## 2. A —— 工作树 `work/`

```
work/
├── app/                    后端 Python 包（镜像内 /srv/mlm/app）
│   ├── __init__.py         __version__（版本号唯一真源）
│   ├── main.py             FastAPI 装配：生命周期、路由挂载、静态前端
│   ├── config.py           Settings（环境变量解析）+ 派生路径（logs/、secret.key）
│   ├── db.py               SQLite 建表/迁移
│   ├── queue.py            任务队列（DB 即队列，单工作线程串行）
│   ├── scanner.py          定时扫描 + 周期刷新 qBittorrent 状态
│   ├── executor.py         任务执行（建链 → 重命名 → 落库 → 待办）
│   ├── script_adapter.py   硬链接运行期适配（调用 app/processors/hardlink.py）
│   ├── processors/         处理器：registry / spec / defaults / overrides /
│   │                       hardlink（原生建链）/ engine（规则引擎）/ logs / __init__
│   ├── routers/            HTTP 路由模块（共 11 个文件：auth / libraries / tasks / system 等 10 个挂路由，
│   │                       common 为公共依赖、__init__ 为导出）
│   └── …                   paths / link_records / logbuf / security / serializers /
│                           qbittorrent / todos / api_models / configstore
├── web/                    静态前端（镜像内 /srv/mlm/web，免构建）
│   ├── index.html
│   ├── css/app.css
│   └── js/  api / app / config_sync / i18n / processors_util / router / store / ui .js
│        └── pages/  dashboard / libraries / library_edit / login / processors /
│                    processor_edit / settings / tasks / task_detail / works .js
├── docker/                 容器构建与启动（三件套，唯一被 COPY 的 docker 内容）
│   ├── Dockerfile
│   ├── entrypoint.sh       → 镜像内 /usr/local/bin/mlm-entrypoint
│   └── healthcheck.sh      → 镜像内 /usr/local/bin/mlm-healthcheck
├── tests/                  单元/集成测试（**不进镜像**；跑法见 README 的开发章节）
├── docs/                   文档（**不进镜像**，按读者分三桶）
│   ├── contracts/          契约定义（实现与测试的依据）：ARCHITECTURE / PROCESSORS / API /
│   │                       BASELINE / COMPATIBILITY
│   ├── guides/             用户手册：USER_GUIDE / DEPLOYMENT / TROUBLESHOOTING / FAQ
│   └── meta/               结构与证据：STRUCTURE（本文）/ TEST-MAP / EVIDENCE / KNOWN_ISSUES
├── examples/               可直接抄的部署与自检示例
│   ├── docker-compose.example.yml      带注释的部署编排示例
│   ├── deploy-walkthrough.md               从零到硬链接成立的完整演练
│   ├── qbittorrent-autorun.md      qBittorrent 回调配置（命令的权威副本）
│   ├── minimal-repro.sh            任意 Docker 机器上的最小复现
│   └── validate-deployment-assets.sh  部署资产静态自验（只读）
├── scripts/                工具链（见 scripts/README.md）
│   ├── build-image.sh           源码包 → 构建树 → docker build + 镜像结构断言
│   ├── run-container.sh         起一个带测试数据的容器实例
│   ├── pytest-in-container.sh   把 tests/ 拷进容器跑 pytest
│   ├── acceptance-test.py       全量验收 harness（HTTP + docker exec 断言）
│   ├── syntax-check.py          Python AST + node --check 语法体检
│   ├── backup-bundle.sh         bundle + 源码 tar 归档并自检
│   └── README.md                工具链用法与变量速查
├── reports/                验收报告与产物
│   ├── 1.0.0/              1.0.0 首发验收报告
│   └── README.md           报告索引与命名口径
├── docker-compose.yml      现行编排（两条 bind，含注释）
├── .env.example            环境变量模板（分必需/可选，含注释）
├── .dockerignore           限定 build context：只放行 app/ web/ docker/
├── .gitignore              运行期数据、缓存、环境文件
├── README.md               工程文档入口（先看这个）
└── CHANGELOG.md            版本变更记录
```

### 命名约定

- 工具链脚本：不带前导下划线，动词开头的 `kebab-case`（如 `build-image.sh`）。
  一次性排查脚本一律 `_名字.sh` / `_名字.py`，用完即弃、**不进工作树**。
- 报告文件名：`reports/<版本>/RELEASE-<版本>.md`（发版验收）或 `TEST-<版本>-<范围>-<日期>.md`
  （专项轮次）；本仓库只保留 1.0.0 一版（见 [`reports/README.md`](../../reports/README.md)），原始 JSON 产物不入库。
- 文档一律 `大写下划线.md`（如 `USER_GUIDE.md`），按读者分三桶：
  **契约** `docs/contracts/`（行为与接口定义，实现和测试以此为准）、
  **手册** `docs/guides/`（用户与运维）、
  **结构与证据** `docs/meta/`（结构说明、测试映射、验证证据、已知问题）。

---

## 3. B —— 镜像内（容器根文件系统）

```
/usr/local/bin/mlm-entrypoint      root 启动 → 校验 → 对齐 PUID/PGID → chown → gosu 降权（ENTRYPOINT）
/usr/local/bin/mlm-healthcheck     读容器内 $PORT，请求 /api/system/health，四项 checks 全 true 才退出 0
/srv/mlm/                          WORKDIR + PYTHONPATH（应用根）
├── app/                           ← work/app/
└── web/                           ← work/web/
/data/                             ← 宿主数据卷（见 §4）
└── config/                        初始骨架（运行时由 entrypoint/应用补齐属主）
```

要点：

- **没有 `USER app` 指令**：容器以 root 启动，由 entrypoint 做运行期修复后 `exec gosu app …` 降权。
- **应用根叫 `/srv/mlm`，不叫 `/app`**：避免工作目录与 Python 包同名嵌套；容器脚本安装在
  `/usr/local/bin/`（标准做法，也不混进应用代码目录）。
- 镜像里**没有** `tests/`、`docs/`、`examples/`、`scripts/`、`reports/`，也**没有** `.env`、`mlm.db`、
  `secret.key`（`.dockerignore` 结构性排除；`scripts/build-image.sh` 每次构建后复验）。
- `EXPOSE 18081` 只是声明，真实端口由环境变量 `PORT` 决定。

---

## 4. C —— 运行期数据卷

```
<data>/                      容器内 = /data（唯一必须备份的卷）
├── mlm.db                   SQLite：媒体库、任务、链接记录、待办、设置（+ -wal/-shm）
├── secret.key               会话签名密钥（0600；首次启动生成，或由 SECRET_KEY 提供）
├── config/                  配置真源的落盘副本（与 DB 设置键双向同步）
│   ├── libraries.yaml
│   └── processors/<id>.yaml
└── logs/
    ├── hard_links.log       硬链接阶段日志（记录「哪个一级文件夹处理过、当时多大」）
    └── <log_file>           每个媒体库一条重命名日志（默认 <slug>cache.log）
```

- **宿主位置**：由 compose 的 `MLM_DATA_DIR` 决定，默认 `<部署目录>/data`
  （如 `/vol1/1000/Docker/media-ingest/data`）；想放别处就在 `.env` 里设 `MLM_DATA_DIR`
  （见 [`../guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md) §4/§7）。
- 由谁创建：`entrypoint.sh` 只建 `/data` 与 `/data/config` 骨架并 `chown`；
  `/data/logs`、`mlm.db`、`secret.key` 由应用在首次启动时创建。
- **绝不**在媒体挂载（`MEDIA_ROOT`）下做 `chown`/`chmod`/`mkdir`/`touch`：entrypoint 有启动期
  结构性检查，healthcheck 全程只发 HTTP。
- 备份/迁移：备份整个 `data/` 目录即可。`mlm.db` + `config/` + `secret.key` 缺一不可；
  `logs/` 建议一并备份 —— 它们是幂等账本，丢了不会丢数据，但会让已处理内容被重扫一遍。

---

## 5. 宿主上的其他目录（不进 git，也不进镜像）

| 宿主路径（示例） | 用途 | 说明 |
|---|---|---|
| `/vol1/1000/Docker/media-ingest/` | 部署目录（compose 文件所在处） | 生产实例：`docker compose up -d` |
| `…/media-ingest/data` | 数据卷（见 §4） | **唯一必须备份的运行期数据**；位置可由 `MLM_DATA_DIR` 改 |
| `…/media-ingest/build-src` | 构建工具的工作目录 | 每次构建重建，可丢 |
| `…/media-ingest/test-data` | 测试容器用的数据卷 | 默认**复用**；`RESET=1` 才清空 |
| `/vol1/1000` | 媒体共同父挂载（`MEDIA_ROOT`） | 源与目标必须同挂载，否则 `os.link` 一律 `EXDEV` |

---

## 6. 新增文件该放哪（决策表）

| 你要加的东西 | 放这里 |
|---|---|
| 后端模块 / 路由 | `app/`（路由放 `app/routers/`，处理器放 `app/processors/`） |
| 前端页面 / 文案 | `web/js/pages/`、`web/js/i18n.js` |
| 测试 | `tests/test_<主题>.py` |
| 契约定义（实现与测试依据，改动需同步 CHANGELOG） | `docs/contracts/`（并更新 `docs/README.md` 导航与本文件） |
| 用户手册（给用户/运维） | `docs/guides/` |
| 结构说明 / 测试映射 / 验证证据 / 已知问题 | `docs/meta/` |
| 部署示例 / 自检脚本（给别人抄） | `examples/` |
| 需要反复使用的运维脚本 | `scripts/`（参数化 + `MLM_*` 环境变量；更新 `scripts/README.md`） |
| 一次性排查脚本（用完即弃） | **不进仓库**：放数据卷或临时目录；值得保留的请参数化后进 `scripts/` |
| 验收报告与原始产物 | `reports/<版本>/`（文件名带版本与日期） |
| 运行期数据 / 密钥 / 日志 | **都不放仓库**，它们在数据卷里 |

---

## 7. 相关文档

- 构建与运行命令：[`../../scripts/README.md`](../../scripts/README.md)、[`../guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md)
- 版本与归档：[`../../reports/README.md`](../../reports/README.md)、[`../../CHANGELOG.md`](../../CHANGELOG.md)
- 架构与不变量：[`../../README.md`](../../README.md)、[`../contracts/ARCHITECTURE.md`](../contracts/ARCHITECTURE.md)、
  [`../contracts/BASELINE.md`](../contracts/BASELINE.md)
