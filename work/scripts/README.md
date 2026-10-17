# scripts/ —— 构建 · 部署 · 验收工具链

本目录的脚本都可在部署宿主（有 `docker` / `git` / `python3` 的 Linux 机器）上独立运行，
参数可用命令行参数或环境变量覆盖；默认值面向常见部署目录 `/vol1/1000/Docker/media-ingest/…`，
换成自己的路径时改参数即可。

| 脚本 | 用途 | 典型用法 |
|---|---|---|
| `build-image.sh` | 把源码 tar 解到全新构建目录并 `docker build`，随后断言镜像内结构（`/srv/mlm/{app,web}`、`/usr/local/bin/mlm-*`） | `sh scripts/build-image.sh /tmp/media-ingest-src.tar.gz media-ingest:1.0.0` |
| `run-container.sh` | 起一个隔离的测试实例（不动生产容器/数据卷；数据目录默认复用，`RESET=1` 才清空） | `sh scripts/run-container.sh media-ingest:1.0.0 media-ingest-test 18087` |
| `pytest-in-container.sh` | 把当前源码树的 `tests/` 送进一次性容器跑 pytest（镜像里不带 tests） | `bash scripts/pytest-in-container.sh media-ingest:1.0.0` |
| `acceptance-test.py` | 56 项全量功能验收：HTTP + `docker exec` 检查正在运行的实例（建库/建链/改名/重跑/孤儿清理/回调/看板/日志等） | `MLM_CONTAINER=media-ingest-test MLM_EXPECT_VERSION=1.0.0 python3 scripts/acceptance-test.py` |
| `syntax-check.py` | 提交前语法自检：AST 解析 `app/`+`tests/`，`node --check` 检查前端 js | `python3 scripts/syntax-check.py` |
| `backup-to-nas.sh` | 归档 bundle + 源码 tar + sha256，并用 bundle 克隆自检（HEAD / tag / 版本号） | `sh scripts/backup-to-nas.sh /tmp/media-ingest-v<版本>.bundle <版本>` |

## acceptance-test.py 环境变量

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MLM_BASE` | `http://127.0.0.1:18087` | 被测实例基址 |
| `MLM_CONTAINER` | `media-ingest-test` | 容器名（`docker exec` 断言用） |
| `MLM_EXPECT_VERSION` | `1.0.0` | 期望的 `app.__version__` |
| `MLM_SRC` / `MLM_TGT` | `…/Download/qbittorrent/Bangumi` / `…/Library/TestLibrary` | 验收源目录 / 目标库（**会被真实建链与改名**） |
| `MLM_LIB_NAME` / `MLM_LOG_FILE` | `全量验收库` / `bangumi.log` | 验收库名 / 库日志文件名 |
| `MLM_ADMIN_PW` | 无（**必须提供**） | 首次 `POST /api/auth/setup` 用的口令 |
| `MLM_QBT_URL` / `MLM_QBT_USER` / `MLM_QBT_PASS` | `http://192.168.1.100:18080` / `admin` / 无（`PASS` **必须提供**） | qBittorrent WebAPI（回调检查用） |
| `MLM_OUT` | `/tmp/mlm-acceptance` | 结果目录（`FULL-<版本>.json` / `.md`） |

真实口令一律通过环境变量传入，仓库里不含任何口令；缺失时脚本直接 `exit 2`。

## 前置条件与注意事项

- Python 3.11+；跑 `tests/run_all.py` 时在中文 Windows 控制台需设 `PYTHONUTF8=1`
  （否则输出里的 `⇒` 等字符会触发 `UnicodeEncodeError`）。

- 在部署宿主上执行；宿主已有目标镜像 tag。`build-image.sh` 需要先把源码 tar 放到 `/tmp/`。
- `acceptance-test.py` 会**真实**在 `MLM_TGT` 里建硬链接并改名，**不要指向生产媒体库**。
- `acceptance-test.py` 必须在**全新数据库**上跑：它只清空媒体库与目标树，不清 DB 里的逐文件
  决策缓存 —— 在同一份 DB 上复跑会得到 `linked 多 / renamed 0`（链接重建、改名不再做），
  看上去像失败但不是代码问题。干净基线请用 `RESET=1` 起新数据卷，或先重播种数据库。
- C7/C8/G1 等固定断言（`kept_unresolved_episode`、`extras_classified`、看板条目数）对应
  本项目 1.0.0 的验收夹具与出厂判据；换一套媒体库素材时这些数字会变，属正常。
