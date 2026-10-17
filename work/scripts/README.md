# scripts/ —— 构建 · 部署 · 验证工具链

本目录放构建 · 部署 · 验证工具链脚本，每个都能独立跑、参数化、有中文注释。

约定：脚本默认值面向 NAS（`/vol1/1000/Docker/media-ingest/…`），
所有默认值都可以用参数或环境变量覆盖，因此同一份脚本可安全地在测试/生产之间切换。

| 脚本 | 用途 | 典型用法 |
|---|---|---|
| `build-image.sh` | 在 NAS 上把源码 tar 解到全新构建目录并 `docker build`，随后断言镜像内结构（`/srv/mlm/{app,web}`、`/usr/local/bin/mlm-*`） | `sh scripts/build-image.sh /tmp/mlm-src.tgz media-ingest:1.0.0` |
| `run-container.sh` | 起一个隔离的测试实例（不动生产容器/数据卷；默认复用数据目录，`RESET=1` 才清空） | `sh scripts/run-container.sh media-ingest:1.0.0 media-ingest-test 18087` |
| `pytest-in-container.sh` | 用当前源码树的 `tests/` 在一次性容器里跑 pytest（`docker cp` 送入，镜像里没有 tests） | `bash scripts/pytest-in-container.sh media-ingest:1.0.0` |
| `acceptance-test.py` | 56 项全量功能验收：HTTP + `docker exec` 检查正在运行的实例（建库/建链/改名/重跑/孤儿清理/回调/看板/日志等） | `MLM_CONTAINER=media-ingest-test MLM_EXPECT_VERSION=1.0.0 python3 scripts/acceptance-test.py` |
| `syntax-check.py` | 提交前语法自检：AST 解析 `app/`+`tests/`，`node --check` 前端 js；仓库移动后依然可用 | `python scripts/syntax-check.py` |
| `backup-to-nas.sh` | 归档 bundle + 源码 tar + sha256，并用 bundle 克隆自检（HEAD/tag/版本号） | `sh scripts/backup-to-nas.sh /tmp/media-ingest-v<版本>.bundle <版本>` |

## 变量速查（acceptance-test.py）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MLM_BASE` | `http://127.0.0.1:18087` | 被测实例基址 |
| `MLM_CONTAINER` | `media-ingest-test` | 容器名（`docker exec` 断言用） |
| `MLM_EXPECT_VERSION` | `1.0.0` | 期望的 `app.__version__` |
| `MLM_SRC` / `MLM_TGT` | `…/Download/qbittorrent/Bangumi` / `…/Library/Test2214` | 验收源目录 / 目标库（会被真实建链与改名） |
| `MLM_LIB_NAME` / `MLM_LOG_FILE` | `全量验收库` / `bangumi2214.log` | 验收库名 / 库日志文件名 |
| `MLM_ADMIN_PW` | **无**（必须提供） | 首次 `POST /api/auth/setup` 用的口令；真实口令不进仓库，调用方用环境变量传入 |
| `MLM_QBT_URL/USER/PASS` | `http://192.168.1.100:18080` `admin` / **无**（`PASS` 必须提供） | qBittorrent WebAPI（H 组检查用） |
| `MLM_OUT` | `/tmp/mlm-acceptance` | 结果目录（`FULL-<版本>.json` / `.md`） |

> ⚠️ **harness 与版本的口径**：`acceptance-test.py` 的 C7/C8/G1 期望值（`kept_unresolved_episode=49`、
> `extras_classified=49`、看板 `total=90`）对应 1.0.0 的出厂判据（7 条，含 `CM集`，见
> [`../CHANGELOG.md`](../CHANGELOG.md) 的 `[1.0.0]` 段）。

## 前置条件

- 在 NAS 宿主上执行（需要 `docker`/`git`/`python3`；`syntax-check.py` 另需 `node`）。
- 宿主已存在目标镜像 tag；`build-image.sh` 需要先把源码 tar 放到 `/tmp/`。
- `acceptance-test.py` 会**真实**在 `MLM_TGT` 里建硬链接并改名，别指向生产库。
- `acceptance-test.py` 必须在**全新数据库**上跑：它只清空媒体库与目标树，**不清** DB 里的逐文件
  决策缓存 —— 在同一份 DB 上复跑会得到 `linked 8534 / renamed 0`（链接重建、改名不再做），
  看上去像失败但不是代码问题。干净基线请用 `RESET=1` 起新数据卷，或先重播种数据库。
