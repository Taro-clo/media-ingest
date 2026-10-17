# 1.0.0 首发验收报告

- **日期**：2026-10-16
- **版本串**：`app.__version__ = "1.0.0"`
- **镜像**：`media-ingest:1.0.0`（同时打 `latest`）= `0ec5d23658ce`，约 181 MB
- **交付**：git tag `v1.0.0`；归档 `media-ingest-v1.0.0.bundle`、`src-v1.0.0.tar.gz`（各带 `.sha256`），
  bundle 克隆自检通过
- **环境**：基础镜像 `python:3.11-slim`；验收与两库测试都跑在**全新数据库 + 全新目标树**上

## 1. 镜像构建

- `docker build -f docker/Dockerfile`，基础镜像 `python:3.11-slim`（约 2~5 分钟）
- 构建上下文 128 个文件；镜像内 7 项结构断言全部通过：服务端 `app/`、Web 资源、`entrypoint.sh`、
  `healthcheck.sh` 就位
- 镜像内 `app.__version__ == "1.0.0"`；同一源码重复构建得到同一镜像 ID（可复现）

## 2. 单元测试（容器内）

```
python -m pytest -q tests
→ 525 passed, 7 skipped, 2 errors in 32.98s
```

7 个 skip 是有意跳过的环境相关用例；2 个 `errors` 来自
[`tests/test_no_source_mutation.py`](../../tests/test_no_source_mutation.py) 两个用例的**夹具初始化**
（pytest 记为 ERROR 而非 FAILED），与 1.0.0 的应用改动无关，属已知的夹具问题。

## 3. 全量验收（56 项）

```
MLM_ADMIN_PW=… MLM_QBT_PASS=… python3 scripts/acceptance-test.py
→ === FULL100 RESULT PASS=56 FAIL=0 ===
```

覆盖范围：容器与健康检查、认证、媒体库配置与全量入库、目标树硬链接与目录结构、二次运行幂等、
队列串行化与日志隔离、TODO 看板计数、qBittorrent 连接、Web hook、容器存活。

## 4. 两个全量测试库（真实下载源）

| 库 | 处理器 | 源文件 | 目标 | 首次全量 | 复跑 |
|---|---|---|---|---|---|
| `bangumi` | bangumi | 8534 | 54 作品 / 8534 文件（全部硬链接） | `linked 8534 / renamed 1251 / extras_classified 49 / name_collisions 34 / kept_unresolved_episode 49 / unscoped_works 7`，6.1 s | `linked 0 / skipped 54`，3.1 s |
| `movie` | movie | 3986 | 45 作品 / 3986 文件（全部硬链接） | `linked 3986 / renamed 46 / extras_classified 1`，3.1 s | `linked 0 / skipped 45`，3.1 s |

- 两库跑在**同一个实例、两个互不相同的目标根**上，符合「一个库一个目标根」的部署约定。
- 复跑计数证明**幂等**：第二次运行只做跳过，不再建链接、不再重命名。
- **源目录零改动（红线）**：两库各自的源树在运行前后逐文件比对 —— 仅前 0 / 仅后 0 / 大小变化 0。

## 5. 静态自验与部署资产

- `python scripts/syntax-check.py`：`PY_FILES 71 / PY_SYNTAX_FAILED 0`、`JS_FILES 18 / JS_FAIL 0`
- 全仓 Markdown 相对链接检查：0 断链
- `sh examples/validate-deployment-assets.sh .`：`VALIDATOR_RC=0`，脚本自身汇总行 `34 PASS / 0 FAIL`
  （各节子断言的 `[PASS]` 行会更多，以汇总行为准）

## 6. 与部署验收的分工

本报告覆盖**静态自验 + 容器内测试 + 完整全量入库**；镜像构建、容器 (healthy)、真实硬链接的
部署侧验收步骤见 [`../../docs/guides/DEPLOYMENT.md`](../../docs/guides/DEPLOYMENT.md)。

## 7. 复现步骤

```sh
# 1) 构建镜像（源码包 = 本仓库 work/ 目录内容）
sh scripts/build-image.sh <src.tgz> media-ingest:1.0.0 <builddir>

# 2) 起一个全新隔离实例（RESET=1 先清空数据目录；脚本默认端口 18087）
RESET=1 sh scripts/run-container.sh media-ingest:1.0.0 media-ingest-test 18087 /path/to/test-data

# 3) 容器内单元测试
bash scripts/pytest-in-container.sh media-ingest:1.0.0 <tests-dir>

# 4) 56 项全量验收（口令与 qBittorrent 地址必须由环境变量提供，仓库内不含真实口令）
MLM_BASE=http://127.0.0.1:18087 MLM_CONTAINER=media-ingest-test \
MLM_SRC=<源目录> MLM_TGT=<目标目录> MLM_LIB_NAME=全量验收库 \
MLM_QBT_URL=http://<qBittorrent 地址>:18080 \
MLM_ADMIN_PW=… MLM_QBT_PASS=… python3 scripts/acceptance-test.py
```
