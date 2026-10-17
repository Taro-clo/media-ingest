# 变更记录

## [1.0.0] — 2026-10-16

**首次公开发布。** 运行期版本串 `app.__version__ = "1.0.0"`；镜像 `media-ingest:1.0.0`
（同时打 `latest`）；git tag `v1.0.0`。

**分发**：源码在 GitHub Release；预构建镜像在 GHCR —— `ghcr.io/taro-clo/media-ingest:1.0.0`
（`linux/amd64` + `linux/arm64`，由 GitHub Actions 从同一源码构建，因此镜像 ID 与下文本机构建的
`0ec5d23658ce` 不同属正常）。镜像部署方式见 `README.md`「快速开始（镜像部署）」与
`docs/guides/DEPLOYMENT.md` §4.6。

本版包含两处行为改动（出厂默认特典判据新增 `CM集`、`permission_mode` 契约修订），
以及完整发版实测结果，见下文。

### 部署：预构建镜像手动安装、默认零配置（`linux/amd64` / `linux/arm64` 均可）

- **镜像部署改成手动三步**：① 创建容器数据目录（`mkdir -p`，路径自选）→ ② 下载
  `work/docker-compose.release.yml`（或从 README 复制内容自行创建）→ ③ 在 compose 同目录的 `.env` 里
  按需设置挂载目录 / 数据目录 / 端口（`MEDIA_ROOT` 必改；`MLM_DATA_DIR`、`PORT` 按需；`MLM_VERSION` 可选），然后
  `docker compose -f docker-compose.release.yml up -d`。compose 与文档用 ✏️ 标出要改的位置；
  **不再提供一键安装脚本**。
- **镜像 tag 不写死版本号**：`docker-compose.release.yml` 的 `image:` 默认 `${MLM_VERSION:-latest}`
  （跟随最新发布），安装文档与 compose 里的下载地址改指 `main` 分支（README / `DEPLOYMENT.md` 不再钉死
  `v1.0.0`）；要固定某次发布就在 `.env` 里设 `MLM_VERSION=1.0.0`。
- **运行身份默认 `auto`**：`docker/entrypoint.sh` 按 `MEDIA_ROOT`（其次数据目录）的属主自动识别 `PUID`/`PGID`，
  不必先去宿主 `id`；显式传数字时行为不变，识别失败回退 `1000:1001` 并打 WARN。`docker/Dockerfile` 的默认 ENV
  同步改为 `auto`（`docker run` 直接跑也能自适应）。
- **默认值随 `MEDIA_ROOT` 派生**：`DEFAULT_SOURCE_ROOT`/`DEFAULT_TARGET_ROOT` 默认取
  `<MEDIA_ROOT>/Download`、`<MEDIA_ROOT>/Library`（仍只用于首次建库播种）；`HOST_PATH_MAP` 默认 `{}`（恒等）。
- **数据卷默认 `./data`**（compose 文件同目录），不再写死 `/vol1/1000/Docker/media-ingest/data`，换机器/换根目录不用改配置。
- 以上只影响部署资产（`docker-compose.yml`、`docker-compose.release.yml`、`.env.example`、`docker/entrypoint.sh`、
  `docker/Dockerfile` 与文档/校验脚本），**运行时代码、API、数据库结构与媒体处理结果均不变**。
  `examples/validate-deployment-assets.sh` 相应增加 release compose 的静态断言，并反向断言不再引用一键脚本。

### 变更：出厂默认特典判据（L3）新增第 7 条 `CM集`

纯默认值变更：代码结构、API 形状、数据库结构、媒体处理流程（挂载、端口、环境变量、`/data` 布局）
一律不变，也**不改动任何既有硬链接与重命名结果**。

- **`app/processors/engine.py`**：`DEFAULT_EXTRAS_PATTERNS` 由 6 条增为 7 条 —— 追加 `CM集`
  （正则、大小写不敏感；`CM集` 三个字足够长，不会在标题里误伤，`PV` / `CM` / `OP` / `ED`
  四个短词仍不采用）。同时是 `read_global_extras_patterns()` 在数据库不可用时的兜底值。
- **`app/db.py`**：`DEFAULT_SETTINGS["extras_patterns"]` 同步为 7 条。播种仍是 `init_db()` 里的
  `INSERT OR IGNORE` ⇒ **只影响首次建库**：已在运行的实例、已有 `/data/mlm.db` 不会被改写
  （用户把判据清空成 `[]` 后也不会被重新写回）。
- **`web/js/pages/settings.js`**：设置页「特典文件排除」textarea 的默认行数 6 → 7。
- 文档与测试同步：`docs/` 下相关文档、`tests/test_extras_classification.py` 的默认判据用例、
  `scripts/acceptance-test.py` 的期待值。

**实测影响**（在一个 8,534 文件的真实媒体库上）：

- `kept_unresolved_episode` 54 → **49**、`extras_classified` 44 → **49**，两者之和不变（98）；
  `renamed` **1251**、`name_collisions` **34**、`unscoped_works` **7** 全部不变 ⇒ 只是把源里
  5 个 `… CM集 (BD …).mkv` 从「集数待解析」看板移到「特典已排除」（不改名、也不移动）。
- 想让**运行中的实例**立刻生效：设置页「特典文件排除」手动加一行 `CM集`；或删库重建
  （`init_db()` 会重新播种 7 条）。

验证：`python scripts/syntax-check.py` 全过；隔离实例（全新 DB）播种结果为
`["NCOP","NCED","Preview","Creditless","Info","予告編","CM集"]`（7 条），
`tests/test_extras_classification.py` 19 passed；全量验收 `PASS=56 FAIL=0`
（期待值 `kept_unresolved_episode 49` / `extras_classified 49` / 看板 `total 90` = `49+34+7`）。

### BREAKING：`permission_mode` —— 默认关闭的显式开关

**一句话**：这个开关**真的能用**，但**默认关闭**，且界面**从关到开必须二次确认**。
属于契约修订，`docs/contracts/BASELINE.md` §1.6 与 §4 的 B13 原文已重写，故以 BREAKING 标注。

- **语义**：开启后把**目标库**里本功能产物的目录/文件改成 `0777`（含「体积一致 ⇒ 跳过」的旧作品），
  好让非 root 的播放器服务读得到；**不是**对整棵目标库递归 `chmod 777`。
- **默认关闭**：`libraries.permission_mode = 0` 时一个 `chmod` 都不做，默认路径行为与之前完全一致。
- **连带副作用（无法消除）**：硬链接与源文件共享同一 inode ⇒ 源文件的**权限位**会一起变
  （内容 / 大小 / inode 号 / mtime 不变）。源完整性核对（B9）不检查权限位，所以改了多少会写进任务日志
  （WARNING，含中文说明与统计字段 `permission_mode_fixed_dirs` / `permission_mode_fixed_files`）。
- **只动目标侧**：库根目录 + 本次处理到的每个一级文件夹的目标子树；不递归源目录、不跟进符号链接。
- **失败不致命**：某路径 chmod 失败只记 WARNING + `warnings[]`，不判任务失败。
- **实现**：`app/processors/hardlink.py`（`PERMISSION_MODE_DIR`/`PERMISSION_MODE_FILE = 0o777`、
  仅关键字参数 `permission_mode=False`、`last_permission_stats()`）、`app/script_adapter.py`
  （`LinkOutcome` 新增统计字段）、`app/executor.py`（读库开关、写任务日志）、
  `web/js/pages/library_edit.js`（保存前 confirmDialog）。DB 字段与 API 形状早就贯通，未改动。
- **测试**：新增 `tests/test_permission_mode.py`（9 条，POSIX-only）；既有黄金值测试因默认关闭而全部不变。

### 发版实测（2026-10-16，镜像 `media-ingest:1.0.0`）

- **镜像**：`media-ingest:1.0.0`（同时打 `latest`）= `0ec5d23658ce`，约 181 MB，
  由本次源码 `docker build -f docker/Dockerfile` 构建；镜像内 `app.__version__ == "1.0.0"`。
- **容器内全量 pytest**（`docker cp tests` 后 `python -m pytest -q tests`）：
  `525 passed, 7 skipped, 2 errors in 32.98s`。
  2 个 ERROR 是既有夹具误报（`tests/test_no_source_mutation.py` 的 teardown），与 1.0.0 改动无关。
- **全量验收 56 项**（`scripts/acceptance-test.py`，全新数据库 + 全新目标树）：
  `=== FULL100 RESULT PASS=56 FAIL=0 ===`。
- **两个全量测试库**（同一实例、两个互不相同的目标根，源为真实下载目录）：
  - `bangumi`：源 8,534 文件 → 目标 54 作品 / 8,534 文件**全部硬链接**（`linked 8534`、`skipped 0`、
    `renamed 1251`、`extras_classified 49`、`name_collisions 34`、`kept_unresolved_episode 49`、
    `unscoped_works 7`，6.1 s）；复跑 `linked 0 / skipped 54`（3.1 s ⇒ 幂等）。
  - `movie`：源 3,986 文件 → 目标 45 作品 / 3,986 文件**全部硬链接**（`linked 3986`、`skipped 0`、
    `renamed 46`、`extras_classified 1`，3.1 s）；复跑 `linked 0 / skipped 45`（3.1 s ⇒ 幂等）。
  - 两库的**源树清单前后逐文件比对**：仅前 0 / 仅后 0 / 大小变化 0（B9 红线的实测证据）。
- `scripts/syntax-check.py`：PY 71 / JS 18 全过；全仓 Markdown 相对链接检查 0 断链；
  `examples/validate-deployment-assets.sh` ⇒ `VALIDATOR_RC=0`、`34 PASS / 0 FAIL`。
- **发布产物**：git tag `v1.0.0`；归档 `media-ingest-v1.0.0.bundle` 与 `src-v1.0.0.tar.gz`
  （各带 `.sha256`）保存在部署主机的 `backups/` 目录，bundle 克隆自检通过。

> 验收数据可复现：`scripts/build-image.sh` → `run-container.sh` → `acceptance-test.py` →
> `pytest-in-container.sh`（见 [`scripts/README.md`](scripts/README.md)）。`acceptance-test.py`
> 必须在**全新数据库**上跑，`MLM_ADMIN_PW` / `MLM_QBT_PASS` 由调用方经环境变量提供（仓库内不含口令）。
