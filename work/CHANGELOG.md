# 变更记录

## [Unreleased]

### 新增：建库/改库时的权限预检（不依赖「预检」按钮）

**场景**：qBittorrent 写出的文件属主与容器运行身份（`PUID/PGID`）对不上时，硬链接会 `Permission denied`
（`fs.protected_hardlinks=1` 要求对源文件可读**且**可写）。此前只有用户主动点「预检」或等第一次任务失败
才会发现，而多数人不会点按钮。

- **行为**：`POST /api/libraries` 与 `PUT /api/libraries/{id}` 在写库前，以**应用自己的运行身份**实测：
  源目录可列出可进入（R+X）；目标目录已存在则**真写一个临时文件再删**（`os.open(O_CREAT|O_EXCL)`，
  能识破只读挂载与 ACL），目标目录不存在则检查最近已存在的祖先可写；源树抽样文件不可读只记 `warnings`。
  **源目录还不存在不算错**（「先建库、后有第一次下载」是正常流程，旧行为不变，这种情况交给「预检」提示）；
  其余权限类 `errors` ⇒ `400`，文案带当前 uid/gid/附加组并指向 FAQ 第 43 问；**不写数据库**。
- **预检接口**：`POST /api/libraries/{id}/validate` 响应新增 `permissions` 字段
  （`{ok, identity, errors, warnings, source_exists, target_exists, probe}`），其 `errors` 同时并入外层
  `errors[]`、`ok`；「预检」按钮额外做的仍是 `os.link` 实证与完整诊断。
- **安全**：探针只写**目标目录**（或目标最近已存在祖先），文件名带 `.mlm_probe_` 前缀且写完即删，
  **绝不写源目录**；`tests/test_no_source_mutation.py` 的破坏性写入点白名单同步登记 `paths._write_probe`。
- **测试**：`tests/test_paths.py` 7 个单元用例（源缺失/源不可读/目标不可写/目标尚不存在查祖先/
  目标路径是文件/抽样文件不可读/正常库不留探针残留）；`tests/test_api_smoke.py` 3 个集成用例
  （建库被权限拦 400、改库被拦且不落库、validate 报告 `permissions`）。

### 新增：qBittorrent 回调路径映射（设置项 `downloader_path_map`）

**场景**：qBittorrent 容器把下载目录挂成 `/downloads`，而本应用按单挂载契约使用与宿主同构的容器路径
（如 `/vol1/1000/Download/qbittorrent`）⇒ 回调带来的 `%F` 在应用里不存在。此前只能按分类命中、切不出一级
文件夹，每次退化为**整库全量**；要修复只能改 qBittorrent 的挂载或自己写 wrapper 脚本。

- **WebUI**：设置页 → qBittorrent 卡片新增「回调路径映射」，可添加**多条**。左侧输入框标注「容器内路径
  （qBittorrent 容器里的路径）」（示例 `/downloads`），右侧标注「绝对路径（本应用容器里的绝对路径）」
  （示例 `/vol1/1000/Download/qbittorrent`），各自带示例提示；留空 = 不换算（旧行为）。
- **行为**：回调入口先按**最长前缀**把 `content_path` 换算成本应用容器路径，再做库匹配与一级文件夹切分；
  换算后的路径**必须真实存在**，否则本次不换算（通常退化为全量）并在响应 `warnings` 里说明。响应新增
  `translation{applied,rule,raw,app,warning}` 块（仅在配置了映射时出现）。
  库的 `source_dir` / `target_dir` / `match_path_prefixes` 仍一律填**本应用容器**内的路径。
- **设置项**：`downloader_path_map`（默认 `""`，多行文本，一行一条 `qB 容器路径 => 本应用容器路径`，
  `#` 注释与空行忽略）。`GET/PUT /api/settings` 读写；格式错误（缺 `=>`、某一侧为空、非绝对路径、
  左侧重复、左侧为 `/`）在 `PUT` 时整体 `400`，`detail` 点名第几行。无需数据库迁移（`DEFAULT_SETTINGS` 新增默认空串）。
- **与 `HOST_PATH_MAP` 的区别**：`HOST_PATH_MAP` 是环境变量、容器路径 → **宿主**路径、**只用于显示**；
  `downloader_path_map` 是设置项、**qBittorrent 容器**路径 → **本应用容器**路径、**参与回调的功能判定**。
- **测试**：`tests/test_paths.py` 15 个单元用例（解析/最长前缀/边界/目标不存在回退）+ `tests/test_api_smoke.py`
  3 个集成用例（换算后命中并切出一级文件夹、目标不存在退化为全量、保存校验与回显）。
- **文档**：`docs/contracts/API.md` §5.2/§5.3/§6.1、`docs/contracts/ARCHITECTURE.md` §2/§5/§8、
  `docs/guides/USER_GUIDE.md` §4.4/§8.8/§9/§8.10、`docs/guides/DEPLOYMENT.md` §2.1/§4.2/§5.2/§5.5、
  `docs/meta/TEST-MAP.md`、`examples/qbittorrent-autorun.md` §3/§4。

### 修复：WebUI「修改密码」没有任何反馈（旧密码对错、成功与否都看不出来）

**场景**：设置页改密码时，`POST /api/auth/password` 若返回 `401`（当前密码不正确），页面既不弹提示
也不标红任何输入框；成功时也只有一句轻提示、不离开页面 —— 用户无法判断旧密码输对了没有、
密码到底改没改成功。

- **行为**：旧密码错误 ⇒「当前密码」输入框下红字「当前密码不正确，请重新输入」并聚焦，其余输入保留、
  不跳页；其它错误 ⇒ 顶部错误提示并显示后端原因；成功 ⇒ 清空输入框、提示「密码已修改，请用新密码
  重新登录」并**直接回到登录页**（后端在该接口里会显式删 `mlm_session` Cookie，页面必须承认这一点，
  否则看起来还登录着、下一次请求却 401）。旧提示「修改后当前会话仍然有效」是错的，已改。
- **旧密码错误**：除「当前密码」字段下方的红字外，右下角再补一条错误提示 —— 浏览器（Chrome/Edge）
  在密码框上弹出的密码泄露/自动填充气泡可能正好盖住字段下方的红字，只留一行小字容易被漏看。
- **登录态**：`api.js` 的 401 分支原本对所有接口都会 `clearUser()`；改密码的 401 会被误当成会话失效
  而清掉前端登录态（输错旧密码后一切导航都会弹回登录页）。现在该接口用 `resetAuthOn401: false`
  走独立分支：不清登录态、不弹「登录已失效」、由页面自己展示错误。
- **顺带**：全局配置同步轮询（15s 一次 `GET /api/config`）在登出/改密后仍会继续跑，导致登录页每
  15 秒弹一次「登录已失效，请重新登录」。现在 `isLoggedIn()` 为假时轮询直接跳过。
- **缓存**：静态前端原先没有任何 `Cache-Control`，浏览器会按 `Last-Modified` 推算"启发式新鲜期"，
  期内普通刷新也不会重新校验 —— 部署新版前端后旧 `js` 仍被继续使用（现场表现就是"修了但没生效"）。
  现在 `app/main.py` 给非 `/api/*` 的 `GET`/`HEAD` 响应统一加 `Cache-Control: no-cache`（带 ETag
  校验，文件没变仍走 `304`，开销可忽略）。
- **验证**：在真实 Chromium 里对临时容器 `:18082` 全流程走通 —— 旧密码填错（401）→ 字段红字、仍保持
  登录（可正常切回仪表盘）；旧密码正确 → 204 + 成功提示 + 自动回登录页，等待 20s 无多余弹窗
  （抓包里 204 之后没有 401 `GET /api/config`，修复前有）；新密码重新登录成功。
- **文档**：`docs/contracts/API.md` §3.8（`?probe=false` 只跳过 link 实证、不跳过权限预检）、
  `docs/guides/USER_GUIDE.md` §1/§8.1（改密后会话立即失效、旧密码错误就地提示）。

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
  `work/docker-compose.release.yml`（或从 README 复制内容自行创建）→ ③ 按需改 compose 文件头的三个锚点
  （媒体挂载 `x-media-root` 必改；数据目录 `x-data-dir`、端口 `x-port` 按需），然后
  `docker compose -f docker-compose.release.yml up -d`。**不再提供一键安装脚本**。
- **免 `.env` 的单点配置**：release compose 用 YAML 锚点（`x-media-root` / `x-data-dir` / `x-port`）
  把 `environment` / `ports` / `volumes` 收成同一处定义，改一行即全部生效；`.env` 与命令行环境变量
  变成**可选覆盖**，不再需要「同一个变量在多处一起改」。
- **镜像 tag 不写死版本号**：`docker-compose.release.yml` 的 `image:` 默认 `${MLM_VERSION:-latest}`
  （跟随最新发布），安装文档与 compose 里的下载地址改指 `main` 分支（README / `DEPLOYMENT.md` 不再钉死
  `v1.0.0`）；要固定某次发布就设 `MLM_VERSION=1.0.0`。
- **运行身份默认 `auto`**：`docker/entrypoint.sh` 按 `MEDIA_ROOT`（其次数据目录）的属主自动识别 `PUID`/`PGID`，
  不必先去宿主 `id`；显式传数字时行为不变，识别失败回退 `1000:1001` 并打 WARN。`docker/Dockerfile` 的默认 ENV
  同步改为 `auto`（`docker run` 直接跑也能自适应）。
- **删除「允许的源/目标根目录」设置项**：`allowed_source_roots` / `allowed_target_roots` 从 DB、API、WebUI、
  健康检查与迁移代码中**整体删除**；不再有首次播种的自动派生，也不再读取 `DEFAULT_SOURCE_ROOT` /
  `DEFAULT_TARGET_ROOT`（两份 compose、`.env.example`、examples 里都已移除）。建库/改库时改为**直接校验
  源与目标在同一挂载**（`paths.check_cross_mounts()` 查互相包含，`paths.check_same_mount()` 判挂载，不同
  挂载 ⇒ `400`）。
- **健康检查从六项精简为四项**：`db`、`data_dir`、`processors`、`config`；`downloads_readable` /
  `library_writable` 两项删除（它们只探白名单第一个根，随白名单一起消失）。`docker/healthcheck.sh`、
  `scripts/acceptance-test.py` 与前端标签同步。
- **数据卷默认 `./data`**（compose 文件同目录），不再写死 `/vol1/1000/Docker/media-ingest/data`，换机器/换根目录不用改配置。
- 影响面：部署资产（两份 compose、`.env.example`、`docker/entrypoint.sh`、`docker/Dockerfile`、健康检查脚本）与
  **API/DB 契约**（settings 少两个键、`/api/system/health` 少两项、`/api/system/info` 的挂载表改由媒体库推导）；
  老库升级时自动删除这两个遗留设置项；**媒体处理结果不变**，已有 `/data/mlm.db` 的实例其他数据不受影响。
  `examples/validate-deployment-assets.sh` 相应改为四项健康检查断言与「不再传 `DEFAULT_*`」反向断言。

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
