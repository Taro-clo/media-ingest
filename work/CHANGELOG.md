# 变更记录

## [1.0.0] — 2026-10-16

**首次公开发布。** 本小节即 1.0.0 的发布内容：出厂默认特典判据第 7 条 `CM集`、`permission_mode` 的
`### BREAKING` 修订，以及发版实测与发布时一并完成的整理。运行期版本串 `app.__version__ = "1.0.0"`，
镜像 `media-ingest:1.0.0`（同时打 `latest`），git tag `v1.0.0`。

**出厂默认特典判据（L3）新增第 7 条 `CM集`。** 纯默认值变更：代码结构、API 形状、数据库结构、
媒体处理流程（挂载、端口、环境变量、`/data` 布局）一律不变，也**不改动任何既有硬链接与重命名结果**。

### 变更

- **`app/processors/engine.py`**：`DEFAULT_EXTRAS_PATTERNS` 由 6 条增为 7 条 —— 追加 `CM集`
  （正则、大小写不敏感；`CM集` 三个字足够长，不会在标题里误伤，`PV` / `CM` / `OP` / `ED`
  四个短词仍不采用）。同时是 `read_global_extras_patterns()` 在数据库不可用时的兜底值。
- **`app/db.py`**：`DEFAULT_SETTINGS["extras_patterns"]` 同步为 7 条。播种仍是 `init_db()` 里的
  `INSERT OR IGNORE` ⇒ **只影响首次建库**：已在运行的实例、已有 `/data/mlm.db` 不会被改写
  （用户把判据清空成 `[]` 后也不会被重新写回）。
- **`web/js/pages/settings.js`**：设置页「特典文件排除」textarea 的默认行数 6 → 7。
- 文档同步（`docs/contracts/ARCHITECTURE.md`、`API.md`、`USER_GUIDE.md`、`FAQ.md`、`TROUBLESHOOTING.md`、
  `KNOWN_ISSUES.md`）与测试（`tests/test_extras_classification.py` 默认判据用例、
  `scripts/acceptance-test.py` 的期待值）。

### 影响（真实源实测，8534 文件）

- `kept_unresolved_episode` 54 → **49**、`extras_classified` 44 → **49**，两者之和不变（98）；
  `renamed` **1251**、`name_collisions` **34**、`unscoped_works` **7** 全部不变 ⇒ 只是把源里
  5 个 `… CM集 (BD …).mkv` 从「集数待解析」看板移到「特典已排除」（不改名、也不移动）。
- 想让**运行中的实例**立刻生效：设置页「特典文件排除」手动加一行 `CM集`；或删库重建
  （`init_db()` 会重新播种 7 条）。

### 验证

- `python scripts/syntax-check.py`：`PY_FILES 71 / PY_SYNTAX_FAILED 0`、`JS_FILES 18 / JS_FAIL 0`。
- 隔离实例（全新 DB）：播种结果 `["NCOP","NCED","Preview","Creditless","Info","予告編","CM集"]`（7 条）；
  `tests/test_extras_classification.py` 19 passed。
- 全量验收：`PASS=56 FAIL=0`（期待值：`kept_unresolved_episode 49` / `extras_classified 49` /
  看板 `total 90` = `49+34+7`）。

### 发版实测（2026-10-16，镜像 `media-ingest:1.0.0`）

- **镜像**：`media-ingest:1.0.0`（同时打 `latest`）= `0ec5d23658ce`，约 181 MB，由本次源码
  `docker build -f docker/Dockerfile` 构建；镜像内 `app.__version__ == "1.0.0"`。
- **容器内全量 pytest**（镜像内 `docker cp tests` 后 `python -m pytest -q tests`）：
  `525 passed, 7 skipped, 2 errors in 32.98s`（2 个 ERROR 是既有夹具误报，见上）。
- **全量验收 56 项**（`scripts/acceptance-test.py`，全新数据库 + 全新目标树）：
  `=== FULL100 RESULT PASS=56 FAIL=0 ===`。
- **两个全量测试库**（同一实例、两个互不相同的目标根，源为真实下载目录）：
  - `bangumi`：源 8534 文件 → 目标 54 作品 / 8534 文件**全部硬链接**（`linked 8534`、`skipped 0`、
    `renamed 1251`、`extras_classified 49`、`name_collisions 34`、`kept_unresolved_episode 49`、
    `unscoped_works 7`，6.1 s）；复跑 `linked 0 / skipped 54`（3.1 s ⇒ 幂等）。
  - `movie`：源 3986 文件 → 目标 45 作品 / 3986 文件**全部硬链接**（`linked 3986`、`skipped 0`、
    `renamed 46`、`extras_classified 1`，3.1 s）；复跑 `linked 0 / skipped 45`（3.1 s ⇒ 幂等）。
  - 两库的**源树清单前后逐文件比对**：仅前 0 / 仅后 0 / 大小变化 0（B9 红线的实测证据）。
- `scripts/syntax-check.py`：PY 71 / JS 18 全过；全仓 Markdown 相对链接检查 0 断链；
  宿主 `examples/validate-deployment-assets.sh` ⇒ `VALIDATOR_RC=0`、`34 PASS / 0 FAIL`（脚本自身的汇总行）。
- **发布动作**：git tag `v1.0.0`；归档 `media-ingest-v1.0.0.bundle` 与 `src-v1.0.0.tar.gz`（各带
  `.sha256`）落 `/vol1/1000/Docker/media-ingest/backups/`，bundle 克隆自检通过。

### 发布时一并完成的整理（同日）

- **实例命名**：一次性容器统一用 `media-ingest-*` 前缀（验收实例 `media-ingest-test`、pytest
  实例 `media-ingest-pytest`、最小复现 `media-ingest-minimal-repro`、构建实例 `media-ingest-builder`）。
  镜像内部标识（`/srv/mlm`、`mlm.db`、`/usr/local/bin/mlm-entrypoint`、`MLM_*` 环境变量、
  `mlm-*` 线程名）保持不变，`tests/test_queue_serialization.py` 有断言。
- **口令不进仓库**：`scripts/acceptance-test.py` 的 `MLM_ADMIN_PW` / `MLM_QBT_PASS` 无默认值，
  缺任一个即打印中文提示并 `exit 2`；真实口令由调用方经环境变量传入
  （`tests/conftest.py` 的夹具口令同理）。
- **宿主目录**：部署目录与数据卷统一在 `/vol1/1000/Docker/media-ingest/`（`data/`、
  `test-data-100`、`backups/`、`build-src/`）；全仓路径默认值同步（`docker-compose.yml` 的
  `${MLM_DATA_DIR:-…}`、4 个 shell 脚本、`docs/**` 与两份 README）。
- **验收报告口径**：公开发布版只保留 1.0.0 的报告
  [`reports/1.0.0/RELEASE-1.0.0.md`](reports/1.0.0/RELEASE-1.0.0.md)；
  `reports/README.md` 与 `docs/meta/STRUCTURE.md` 的命名口径同步为
  `reports/<版本>/RELEASE-<版本>.md`（原始 JSON 产物不进发布版）。
- **复验**：改名后对最终源码树重跑 `python3 scripts/syntax-check.py`（PY 71 / JS 18）、
  `examples/validate-deployment-assets.sh`（`VALIDATOR_RC=0`、`34 PASS / 0 FAIL`）、容器内
  pytest（`525 passed / 7 skipped / 2 errors`）、56 项验收（`PASS=56 FAIL=0`）、两个全量测试库
  （`bangumi` 54 作品 / 8534 文件、`movie` 45 作品 / 3986 文件，均全硬链接且复跑幂等），
  实例健康检查六项全通过。

### BREAKING：`permission_mode` —— 默认关闭的显式开关

**一句话**：这个开关**真的能用** —— 但**默认关闭**，且界面**从关到开必须二次确认**。
属于契约修订：`docs/contracts/BASELINE.md` §1.6 与 §4 的 **B13** 原文已重写，故以 `### BREAKING` 标注。

#### 变更

- **`app/processors/hardlink.py`**：新增 `PERMISSION_MODE_DIR` / `PERMISSION_MODE_FILE = 0o777` 与
  `last_permission_stats()`；`create_hard_links(source_dir, target_dir, *, skip=None, permission_mode=False)`
  新增**仅关键字**参数（默认 `False` ⇒ 默认路径行为不变，字节级黄金值不受影响）。开启时新增
  `_chmod_if_needed()` / `_apply_permission_mode()`：库根目录本身 + 本次处理到的**每个一级文件夹的目标子树**
  （含「体积一致 ⇒ 跳过」的旧作品）改成 `0777`；逐条目比对权限位，已经是目标值就跳过（幂等，重跑第二次统计为 0）；
  单个条目失败只记账不中断；**符号链接一律不跟进**（`os.chmod` 默认会跟进，那会改到库外面的文件）。
  **不打印任何新行** ⇒ stdout 契约与日志格式不变。
- **`app/script_adapter.py`**：`LinkOutcome` 新增 `permission_dirs` / `permission_files` / `permission_errors`；
  `run_hardlinks(..., permission_mode=False)` 把开关原样传给原生实现，并在开启时经 `_permission_stats()` 回读统计
  （统计通道坏了不让整次运行失败，与 `_structured_failures` 同一取向）。
- **`app/executor.py`**：从库记录读 `permission_mode` 并传下去；开启时把 `permission_mode_fixed_dirs` /
  `permission_mode_fixed_files` 写进 `Task.result`，并以 **WARNING** 往任务日志写一行中文说明（含「硬链接与源文件共享
  同一 inode，源文件权限位随之改变；大小/inode 号/mtime 不变」），chmod 失败项最多列 20 条并加一条 `warnings[]`。
- **`web/js/pages/library_edit.js` + `web/js/i18n.js`**：保存前**从关到开**弹 `confirmDialog`（标题/正文/细节三句中文
  文案写明会改到源文件权限位），取消则中止保存并提示「已取消保存：未开启权限模式」；风险文案写明真实语义：
  不是对整个目标库目录执行 `chmod 777`，而是收窄到本功能的产物。
- **`app/api_models.py` / `app/serializers.py` / `app/routers/libraries.py` / `app/configstore.py`**：**不需要改** ——
  字段本来就全程贯通（输入 → DB → 序列化 → 配置导出），过去只是执行器不读它。

#### 边界与已知副作用（已写进契约与确认框）

- **默认关闭**：`libraries.permission_mode = 0` 时**一个 `chmod` 都不做**。
- **只动目标侧**：库根目录 + 本次处理到的每个一级文件夹的目标子树；**不递归源目录**、**不跟进符号链接**。
- **副作用无法消除**：硬链接与源文件共享 inode ⇒ 源文件**权限位**一起变（内容/大小/inode 号/mtime 不变）。
  源完整性核对（B9）比的是 `(相对路径, st_size, st_ino, st_mtime_ns)`，**看不见**权限变化 —— 所以改了多少必须写进任务日志。
- **只收窄到本功能的产物**：不是对 `/vol1/1000/Library` 整棵树 `chmod 777`（那会连非本应用创建的内容一起改），
  避免误伤库里其它东西。
- **失败不致命**：某个路径 chmod 失败（只读挂载/权限不足）不会让任务失败，只记 WARNING + `warnings[]`。

#### 测试与验证

- 新增 `tests/test_permission_mode.py`（9 条，POSIX-only）：默认不 chmod、开启改整棵目标树、**跳过分支也修**、
  幂等第二次为 0、符号链接不跟进、适配层字段与统计、执行器读库开关并留日志、默认库零改动。
- 既有测试同步：`tests/test_hardlink_native.py` 的入口签名指纹加 `permission_mode: bool = False`；
  `tests/test_rule_validation.py` 三处 `create_hard_links` 替身加同名关键字参数（否则 `TypeError`）——
  **默认关闭**使既有黄金值/字节级等价性断言全部不动。
- 文档同步：`docs/contracts/{BASELINE,ARCHITECTURE,API,COMPATIBILITY,PROCESSORS}.md`、
  `docs/guides/{USER_GUIDE,FAQ,TROUBLESHOOTING,DEPLOYMENT}.md`、`README.md`，以及
  `examples/validate-deployment-assets.sh`（§12 断言口径：示例仍不传该字段，文案从「不得当功能用」改为「默认关闭」，
  并新增一条「原生实现入口必须默认关闭」的断言）。
- **实测**（本次改动后的树）：
  - 容器内全量 pytest（由本次源码 `docker build -f docker/Dockerfile` 构建）：
    **`525 passed, 7 skipped, 2 errors`** —— 新增的 9 条即 `tests/test_permission_mode.py`；
    2 个 ERROR 是既有夹具误报（`tests/test_no_source_mutation.py` 的 teardown，与 `docs/meta/EVIDENCE.md` 记录一致）。
  - `scripts/syntax-check.py`（PATH 里挂上随附的 node v24.21.0）：`PY_FILES 71 / PY_SYNTAX_FAILED 0`、
    `JS_FILES 18 / JS_SKIPPED 0 / JS_FAIL 0`。
  - 全仓 Markdown 链接检查：23 个文件 / 446 条链接 / **0 断链**。
  - NAS 上跑 `sh examples/validate-deployment-assets.sh <本树>`：**`VALIDATOR_RC=0`、`34 PASS / 0 FAIL`**（含 §12 的新断言）。
  - 无 pytest 的环境可用纯标准库降级 runner `tests/run_all.py`（覆盖面略小于 pytest，个别依赖 pytest 夹具的用例会显示为失败）
    —— 5 条为改动前基线就存在的 Windows 环境性失败，逐条相同。
