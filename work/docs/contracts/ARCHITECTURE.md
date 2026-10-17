# 架构与接口契约（冻结）

> 版本：v1（冻结）。任何修改必须同步更新本文件与 `CHANGELOG.md`。
> 配套文档：`docs/contracts/BASELINE.md`（不可变行为契约）。

---

## 概览（阅读顺序）

本架构的四项要点：**容器自包含、不依赖任何外部挂载**；**重命名规则完全配置化**
（参数与顺序、接受/拒绝目录正则、后缀白名单）；**配置文件与 WebUI 双向同步**；
**每个媒体库各自记录日志且集中在同一目录**。

**处理器契约的唯一真源是 [`docs/contracts/PROCESSORS.md`](PROCESSORS.md)**（规则的字段、预设与
处理器注册表都在那里）。本文 §1 技术选型、§2 容器路径布局、§3 数据模型、§4 处理器选择规则、
§5 通用约定与端点清单、§6 模块契约、§7 前端契约、§8 回调命令、§9/§9.1 部署事实、
§10 写入范围都是**现行契约**。

---

## 1. 技术选型与理由

| 决策 | 选择 | 理由 |
|---|---|---|
| 后端语言 | Python 3.11 | 语义清楚、stdlib 足够，硬链接与重命名都在应用进程内完成（`app/processors/hardlink.py`、`app/processors/engine.py`） |
| Web 框架 | FastAPI + Uvicorn | 自动校验、OpenAPI；依赖少、镜像小 |
| 前端 | **免构建**原生 ES modules + CSS | 构建环境无 node/npm；NAS 构建更快更稳；无需 npm 供应链 |
| 存储 | SQLite（WAL） | 单文件、随数据卷持久化、无额外容器 |
| 口令哈希 | `hashlib.scrypt`（stdlib） | 无需 bcrypt 原生依赖；参数 N=2^14,r=8,p=1，格式 `scrypt$N$r$p$salt_b64$hash_b64` |
| 会话 | 签名 Cookie（stdlib `hmac`+`secrets`） | 无额外依赖；`SameSite=Lax`、`HttpOnly` |
| 并发 | 单进程 + 单工作线程串行队列 | 规避 `create_hard_links` 日志"读-改-写"竞争 |
| 依赖 | 仅 `fastapi`, `uvicorn[standard]` | 其余全部 stdlib |

---

## 2. 容器路径布局（**关键设计决策**）

**决策：只挂一条「单一共同父挂载（容器路径 = 宿主路径）」—— 源与目标都在这条挂载里。**
因此 qBittorrent 回调传来的 `%F`（content path）可以**零转换**直接使用。

| 宿主（NAS）路径 | 容器路径 | 模式 | 说明 |
|---|---|---|---|
| `${MEDIA_ROOT:-/vol1/1000}` | **同宿主路径**（`/vol1/1000`） | **rw** | **单一共同父挂载**：下载源 `/vol1/1000/Download/qbittorrent` 与媒体库 `/vol1/1000/Library` 都在它下面 ⇒ 源与目标**同挂载**，这是硬链接成立的**内核前提**（见下） |
| `/vol1/1000/Docker/media-ingest/data` | `/data` | rw | SQLite、任务队列、配置与日志的父目录 |
| — | `/data/logs` | — | 应用日志 + **每个媒体库一条**的重命名日志（`docs/contracts/PROCESSORS.md` §5） |
| — | `/data/config` | — | 配置文件镜像 `libraries.yaml` 与 `processors/*.yaml`（§6 双向同步） |

> **只有上面这一条 bind**：拆成 `/downloads` + `/library` 两条独立 bind 会形成两个挂载 ⇒ 硬链接必然 `EXDEV`。
> qBittorrent 容器自己的路径映射与本应用无关，只要回调带来的 `%F` 恰好等于宿主路径即可。

### 容器自包含：不需要任何宿主额外目录

容器**不依赖宿主上的任何额外目录**：
硬链接由 `app/processors/hardlink.py`（原生实现，证据 `tests/test_hardlink_native.py` 的冻结黄金值）执行，
重命名走配置驱动的规则引擎 `app/processors/engine.py`，运行期不载入任何外部模块。

### 同一挂载校验（实证探针取代 `st_dev`）

源与目标不仅要"同一个文件系统"，还必须**同一个挂载** —— 内核 `linkat(2)` 在建链前先比较**挂载身份**
（`do_linkat()` 里的 `old_path.mnt != new_path.mnt`），不同挂载**一律 `EXDEV`**，与 `st_dev`、`fsid`、btrfs 子卷都无关。

三级判据（`app/paths.py`）：

| 级别 | 手段 | 说明 |
|---|---|---|
| **主判据** | **实证 `link()` 探针** | 从源根有界递归（深度 ≤3、≤512 条、不跟符号链接）取一个已存在普通文件 → `os.link` 到目标根临时名（`.mlm_probe_*.link`）→ `unlink`；**源目录零写入** |
| **预筛** | `/proc/self/mountinfo` 的 **mount ID** | **必要不充分**：相同不代表能建（btrfs 同一挂载跨子卷仍可能 `EXDEV`） |
| **降级** | `st_dev` | **仅在无 `/proc` 时**使用并标注为推断 —— `st_dev` 相同**不能**预测 `link()` 成功（两条独立 bind"假绿"的根因） |

`same_filesystem` 的类型是 **`bool | None`** 三态：`true` / `false` / `null`（源树下找不到可试链的普通文件 ⇒ **无法实证**，不猜）。
判定为跨挂载时：**运行前只警告、不硬拦**（进 `Task.result.warnings[]`，中文、带可执行修法），
最终结论由执行前后的逐文件核对给出（失败即 `link_failed`，**绝不复制兜底**）。文案：

```
跨挂载：源 /vol1/1000/Download/qbittorrent/Bangumi 与目标根 /vol1/1000/Library/Bangumi 不在同一个挂载，
os.link 会返回 EXDEV。本应用不会回退为复制文件。
请把下载目录与媒体库目录放进同一个 bind 挂载（见 docker-compose.yml 的 MEDIA_ROOT）。
```

> 📌 源目录的只读保证是**"可验证"、不是"结构性"的**（`:ro` 与跨路径硬链接在内核层面互斥）：
> ①代码层永不写源目录（`tests/test_no_source_mutation.py`）②任务前后**逐文件源完整性核对**（`Task.result.source_integrity = {ok, changed[]}` + `warnings[]` 中文告警）。**这是降级、不是等价替代。**

### 日志路径与容器路径一致

容器路径 = 宿主路径 ⇒ 日志里存的路径与传给建链实现的路径**两侧一致**，"体积一致则跳过"的幂等判定
与孤儿清理都按日志原样工作。`HOST_PATH_MAP` 是**纯展示**用途。

**每个媒体库都有自己独立的重命名日志**（重命名阶段），而硬链接账本 `hard_links.log`
**继续写入、只是不再充当跳过判据**（账本判据见 §3 的 `link_records`）。

### 其它路径相关的既有行为（不要踩）

- `app/processors/hardlink.py` 用 `open(LOG_FILE, 'r')`，**未指定 encoding** ⇒ 跟随 locale。
  日志含中文，因此容器**必须**有 `LANG=C.UTF-8` 与 `PYTHONUTF8=1`，否则 `UnicodeDecodeError`
  （它是 `ValueError` 的子类，但 `for line in f` 在 try 之外，会直接抛出）。
- `app/processors/hardlink.py` 要求切片后恰好 3 段 ⇒ **文件夹名里含 ` - `（空格连字符空格）的条目会被整行忽略**
  （`ValueError` ⇒ 跳过该行）。属已知限制 **L2**，不修。

---

## 3. 数据模型（SQLite）

```sql
-- 用户（首个通过 /api/auth/setup 创建）
CREATE TABLE users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, created_at TEXT NOT NULL);

-- 媒体库规则
CREATE TABLE libraries (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL CHECK(type IN ('Bangumi','Movie','Custom')),
  processor TEXT NOT NULL CHECK(processor IN ('bangumi','movie','hardlink_only')),
  enabled INTEGER NOT NULL DEFAULT 1,
  source_dir TEXT NOT NULL,           -- 容器路径
  target_dir TEXT NOT NULL,           -- 容器路径
  match_categories TEXT NOT NULL DEFAULT '[]',   -- JSON array[str]
  match_tags TEXT NOT NULL DEFAULT '[]',
  match_path_prefixes TEXT NOT NULL DEFAULT '[]',
  orphan_cleanup INTEGER NOT NULL DEFAULT 1,
  permission_mode INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

-- 任务
CREATE TABLE tasks (
  id INTEGER PRIMARY KEY,
  library_id INTEGER REFERENCES libraries(id) ON DELETE SET NULL,
  library_name TEXT,                  -- 冗余，便于库删除后仍可读
  kind TEXT NOT NULL,                 -- hardlink | rename | rename_hardlink
                                      --（legacy 兼容：link -> hardlink，link+rename -> rename_hardlink）
  trigger TEXT NOT NULL,              -- qbittorrent | scan | manual | retry
  status TEXT NOT NULL,               -- queued | running | success | failed | skipped | cancelled
  scope TEXT NOT NULL,                -- JSON {"mode":"full"|"folder","folder":str} 或 {"mode":"works","folders":[str]}
  payload TEXT NOT NULL DEFAULT '{}',
  result TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);

CREATE INDEX idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX idx_tasks_status ON tasks(status);

-- 任务日志（结构化 + 原始 stdout）
CREATE TABLE task_logs (
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  ts TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL, stream TEXT);
CREATE INDEX idx_task_logs_task ON task_logs(task_id, id);

-- 待办看板：全局一份，不按媒体库分表
CREATE TABLE todos (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                 -- link_failed | rename_suffix
                                      -- | unresolved_episode | source_integrity | task_failed
  dedupe_key TEXT NOT NULL,           -- 身份键：同一 (kind, dedupe_key) 的重复发生不新增条目
  title TEXT NOT NULL,                -- 标识符（路径 / 目标名 / 库名）
  detail TEXT,                        -- 原始上下文（多条换行分隔，应用层截到 4000 字符）
  severity TEXT NOT NULL DEFAULT 'warning',   -- info | warning | error（无 CHECK，见下）
  status TEXT NOT NULL DEFAULT 'open',        -- open | done（无 CHECK，见下）
  library_id INTEGER,                 -- **故意不是外键**：库/任务被删或被 max_history 裁掉后，
  library_name TEXT,                  -- 待办（用户自己看过的账）必须仍在，故只存"当时的快照"
  task_id INTEGER,                    -- **故意不是外键**，同上（trim_history 会 DELETE tasks）
  task_kind TEXT,                     -- hardlink | rename | rename_hardlink（当时那个任务的 kind）
  occurrences INTEGER NOT NULL DEFAULT 1,     -- 被观察到的次数（首次为 1，重复发生时 +1）
  first_seen_at TEXT NOT NULL,        -- **永不变**
  last_seen_at TEXT NOT NULL,
  resolved_at TEXT,                   -- 打钩时间；reopen 时清回 NULL
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

-- 三件事各有索引：按状态+种类筛、按 (kind,dedupe_key) 查已有 open、按库筛
CREATE INDEX idx_todos_status_kind ON todos(status, kind);
CREATE INDEX idx_todos_dedupe ON todos(kind, dedupe_key, status);
CREATE INDEX idx_todos_library ON todos(library_id);
CREATE INDEX idx_todos_seen ON todos(last_seen_at DESC);

-- 每库「已执行作品」账本：建链阶段的跳过判据，键 = (库, 作品名)
CREATE TABLE link_records (
  id INTEGER PRIMARY KEY,
  library_id INTEGER NOT NULL,          -- **故意不是外键**：库删除时由 app/routers/libraries.py
  folder TEXT NOT NULL,                 -- 显式调 link_records.drop_by_library() 清掉
  source_dir TEXT NOT NULL DEFAULT '',  -- 参与判据：换了源或换了目标，旧记录一律不算数
  target_dir TEXT NOT NULL DEFAULT '',
  target_path TEXT NOT NULL DEFAULT '', -- 记录当时的落点，供 WebUI 展示与删除时核对
  size INTEGER NOT NULL DEFAULT 0,      -- 建链时那个文件夹的体积（与日志里 Folder size 同源）
  linked INTEGER NOT NULL DEFAULT 0,    -- 本次实际新建的链接数（0 = 写了记录但一个都没新建）
  first_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);

-- 唯一索引就是账本的身份：(library_id, folder) 一行，重复写入只刷新不新增
CREATE UNIQUE INDEX idx_link_records_library_folder ON link_records(library_id, folder);
CREATE INDEX idx_link_records_library ON link_records(library_id, updated_at DESC);

-- 键值设置
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
```

**`link_records`（`app/link_records.py`）—— 建链阶段的跳过判据**：账本是**作品级**的，
一行 = 「本库曾经把某个一级文件夹链接到 `target_path`、体积多少」，作品名就是建链阶段的**一级文件夹名**。
设计要点：

- **与 `hard_links.log` 的分工**：日志**照写**（两种打印行、两类记账行、行格式逐字不变），
  角色是**兼容与审计** —— 既有工具照旧能读，但它**不再决定跳不跳**；账本才是判据。
  日志判据是**全局**的、键里没有库也没有目标 ⇒ ①两个库指向同一 `source_dir` 时，A 库跑完后
  B 库全被"体积一致，跳过"（新库一个链接都不建）；②同库**换目标目录**照样跳过。
- **判据的维度**：`(library_id, folder)` + `source_dir` + `target_dir` + `size` **四项全等**才跳过；
  `app/processors/hardlink.py` 的 `create_hard_links(source_dir, target_dir, *, skip=None)` 在
  `skip=None` 时仍走全局日志的原路径，**输出逐字不变**（`tests/test_hardlink_native.py` 的冻结黄金值）。
- **为何无外键**：库被删除时由 `app/routers/libraries.py` 显式调 `link_records.drop_by_library()`
  清掉，删除动作要看得见（同 `todos` 的理由）；`DELETE /api/libraries/{id}` 的响应里新增
  `link_records_removed` 报告这个行数。
- **建表两处同源**：新装库由 `SCHEMA` 带出，老库由 `migrate()` 调 `_ensure_link_records_table()`
  补建（**同一份 DDL 常量**，在 `_ensure_todos_table` 之后调用）。
- **语义**：账本是**作品级**的，**不保证文件级完整性** —— 用户在目标目录里的
  手动增删改**一律保留**、不会被自动修复；唯一的重做途径 = 用下面 §5 的三个 `/works*` 接口删掉
  该作品的记录（连带删目标产物），下次运行自动补做，其余作品不受影响。
- **不回填历史**：老库升级后账本是**空的** ⇒ 第一次运行是"全量"。
- **访问器**：`search_records(library_id: int, *, query: str | None = None,
  limit: int | None = None, offset: int = 0) -> tuple[list[dict], int]` 返回 `(本页行, 匹配总数)`。
  `query` 对**作品文件夹名**做**子串**匹配：ASCII **大小写不敏感**，`%` / `_` / `\` 按**字面量**
  处理（`LIKE … ESCAPE '\'`）；**空串或纯空白 = 不过滤**；排序 `folder COLLATE NOCASE`，因此
  **翻页稳定**；`limit=None` = **不分页**（内部用，例如对整库算「账本 vs 重命名缓存」的差集）。
  旧的 `list_records(library_id)` 保留为**不分页、不过滤**的访问器，测试仍在用。

**默认 settings**：

| key | 默认值 | 说明 |
|---|---|---|
| `allowed_source_roots` | `[DEFAULT_SOURCE_ROOT]`（默认 `/vol1/1000/Download`，即整个下载树） | 媒体库 `source_dir` 必须在其内。**只影响首次播种**，对已存在的 DB 不生效；旧的默认单值 `["/downloads"]` 会在升级时被**有界改写**（见 `docs/guides/DEPLOYMENT.md` §6.5） |
| `allowed_target_roots` | `[DEFAULT_TARGET_ROOT]`（默认 `/vol1/1000/Library`） | 媒体库 `target_dir` 必须在其内。同上 |
| `hook_token` | 随机生成 | `/api/hooks/*` 的 Bearer token |
| `scan_enabled` | `false` | 目录扫描补偿 |
| `scan_interval_seconds` | `900` | 扫描周期 |
| `qbittorrent_url` | `""` | 如 `http://192.168.1.100:18080` |
| `qbittorrent_username` / `qbittorrent_password` | `""` | 可选，用于 API 补偿 |
| `log_level` | `INFO` | 应用日志等级 |
| `orphan_cleanup_enabled` | `true` | 全局孤儿清理总开关 |
| `max_history` | `2000` | 任务历史保留条数 |
| `config_sync_interval_seconds` | `5` | 配置文件镜像与数据库的双向同步周期（秒，区间 **0–86400**，`0` = 只写不导入）。**它是数据库设置项、不是环境变量** —— 不存在 `CONFIG_SYNC_INTERVAL_SECONDS` 这个 env |
| `max_task_log_lines` | `5000` | 单个任务写入 `task_logs` 的行数上限，超出写一条 `日志已截断` 的 WARNING |
| `rename_extensions` | `[]` | 全局重命名后缀白名单（三层里居中：库覆盖 → 本设置 → 处理器清单） |
| `extras_patterns` | `["NCOP", "NCED", "Preview", "Creditless", "Info", "予告編", "CM集"]` | **散装特典判据（L3）**（**正则字符串数组**，一条一项）；`[]` = **关闭**该功能。每条在 `PUT` 时先 `re.compile`，有一条编不过就整体 `400`。引擎侧由 `engine.read_global_extras_patterns()` / `normalize_extras_patterns()` 读取（与 `rename_extensions` 同一条设置通路）。出厂默认 **7 条** |
| `todo_kinds` | `""`（= 五种全启用） | **待办看板的种类开关**：逗号 / 顿号 / 分号 / 空白分隔的 kind 列表；**未知或非法的值忽略**，整串全是非法值 ⇒ 回落"全部启用"。关掉的种类**连记都不记**（不是"记了但隐藏"）。⚠️ 它**不出现在 `GET /api/settings` 的响应里**（见 [`docs/contracts/API.md`](API.md) §5.2），当前值看 `GET /api/todos/summary` 的 `disabled_kinds`。见 §6.10 |

---

## 4. 处理器选择规则（自定义库不得绕过既有规则）

> ⚠️ **本节前半部分的枚举耦合规则不再成立**，保留仅作对照。现行处理器规则见
> [`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §3、§7。

「`type` 决定 `processor` 的允许集合」（`Bangumi` 只能配 `bangumi` 等）这条**不成立**：

- `type` 变成**自由文本标签**（只用于展示与统计，不参与任何校验、也不再约束处理器）；
- 媒体库改用 **`processor_id`**，它必须指向处理器注册表里一个**存在**的处理器
  （内置三个：`hardlink_only` / `bangumi` / `movie`；其余由用户自建）；
- 用户自建处理器**只能**由既有的三种构件组装出来——`traverse`（遍历与接受/拒绝目录）、
  `extensions`（后缀白名单）、`naming`（命名表达式与段序）、`selection`（同后缀多文件的取舍）。
  **没有任何字段能改变"只在库侧重命名"、"绝不移动/删除源文件"、"跨文件系统不降级为复制"这三条底线。**

仍然有效的通用校验：

- 校验失败 → HTTP 422，`detail` 是**字段级中文**列表，元素形状与 FastAPI / Pydantic 一致：
  `{"loc": ["body", "<字段>", …], "msg": "<中文>", "type": "value_error.mlm"}`（`app/routers/common.py:32-47`
  `unprocessable()` / `:50-60` `unprocessable_many()`）。**字段名在 `loc` 里（`loc[0]` 固定 `"body"`，
  点号路径逐段展开，最后一个元素即字段名），没有 `field` 键** —— 这样前端一套解析就能同时吃下
  Pydantic 自身的校验错误与本应用手工抛出的错误。
  `{"field": "<点号路径，可为空串>", "message": "<中文>"}` 只出现在
  **`POST /api/processors/validate` 的 200 响应体**里（`app/processors/spec.py` 的 `iter_errors()`；
  该端点**不落库**，规格合法时返回 `{ok: true, errors: []}`）。两者别混：**422 看 `loc`、validate 看 `field`**。
  实测（库日志名与其它库撞车，`app/routers/libraries.py:127-132`）：
  ```json
  {"detail": [{"loc": ["body", "log_file"],
               "msg": "日志文件名 bangumicache.log 已被其它媒体库或处理器占用；请换一个名字，或留空由系统自动分配（下一个可用名：bangumicache-2.log）。",
               "type": "value_error.mlm"}]}
  ```
- `source_dir` 必须在 `allowed_source_roots` 内；`target_dir` 必须在 `allowed_target_roots` 内。
- `source_dir` 与 `target_dir` 不得互相包含，也不得相同。

---

## 5. HTTP API 契约

所有 `/api/*`（除 `auth/*`、`system/health`、`hooks/*`）需要会话 Cookie；未认证返回 401。
错误统一 `{"detail": "<中文可读信息>"}`。

### 5.0 通用约定（后端与前端都按此实现）

**分页列表统一信封**（适用于 `GET /api/tasks`、`GET /api/libraries` 与 `GET /api/todos`）：

```json
{"items": [ ... ], "total": 137, "limit": 50, "offset": 0}
```

- `items` 恒为数组（**不是**裸数组，也**不用** `{tasks:...}` / `{libraries:...}` 这种按资源命名的键）；
- `total` 是**过滤后**的匹配总数，前端据此算页数；
- 非分页的集合接口（`/api/stats/overview.recent`）仍返回裸数组。

**路由挂载**：后端用 `StaticFiles(directory="web", html=True)` 把 `web/` **整棵**挂在 `/`，
`/api/*` 路由先注册。因此前端 `index.html` 里用**相对路径**引用 `css/app.css`、`js/app.js` 是正确的，
且 `#/...` 的 hash 路由不需要服务端 rewrite。`GET /` 返回 `web/index.html`。

**响应体字段冻结（后端与前端都不得再猜）**

1. **媒体库列表项的 `stats`**（`GET /api/libraries` 与 `GET /api/libraries/{id}` 都带）：

   ```json
   "stats": {
     "last_task_id": 12, "last_kind": "hardlink", "last_status": "success",
     "last_trigger": "hook", "last_finished_at": "2026-01-15T10:00:00+08:00",
     "last_error": null
   }
   ```
   无任务时整体为 `null`（不是全 `null` 的对象）。`last_task_id` 用于列表页点进详情。

2. **`GET /api/tasks` 的 `q`**：对 `library_name` 与 `error` 两列做**大小写不敏感的子串**匹配（OR）。

3. **`Task.result`：计数 + 限量样本**（不是全量数组，避免大库把 DB 撑爆）：

   ```json
   "result": {
     "linked": 0, "skipped": 101, "link_failed": 0, "already_linked": 0,
     "renamed": 12, "rename_failed": 0,
     "orphans_removed": 0, "folders_scanned": 101, "bytes_linked": 0, "duration_ms": 1234,
     "samples": {
       "link_failed": [{"src": "...", "dst": "...", "error": "..."}],
       "orphans_removed": ["/vol1/1000/Library/Bangumi/Old Show"],
       "renamed": [{"from": "x 01.mkv", "to": "Show S01E01.mkv"}]
     },
     "samples_truncated": false,
     "source_integrity": {"ok": true, "changed": []}
   }
   ```
   计数恒为整数，`duration_ms` 恒为整数；**每个 sample 数组不设上限**（待办条数 = 样本条数，
   一次扫描里有多少个文件出问题就留多少条，
   8 534 个建链失败就是 8 534 条）。`samples_truncated` 因此成为**保留字段**：
   仍然照旧出现在 `result` 里、恒为 `false`；若日后把 `app/executor.py` 的 `SAMPLE_LIMIT`
   改回数字，它会恢复"样本被截断"的原义。全量逐条运行明细另有去处：`GET /api/tasks/{id}/logs`；
   任务详情页的逐条**渲染**另有一层显示上限（只影响一屏渲染多少行，不影响落账条数）。
   `source_integrity` 见 §5.0 与 `docs/contracts/API.md` §4.1：任务前后对源树逐文件快照/复核（`相对路径`/`size`/`ino`/`mtime_ns`）；
   `nlink`/`ctime` 的固有变化不算差异。`Task` **顶层**还有 `warnings[]` —— 两类：「跨挂载」（运行前判定，带中文修法）与「源目录被动过」（与 `source_integrity` 同时出现）。

   ```json
   {"scan_enabled": true, "scan_interval_seconds": 900, "log_level": "INFO",
    "max_history": 2000, "orphan_cleanup_enabled": true, "config_sync_interval_seconds": 5,
    "allowed_source_roots": ["/vol1/1000/Download/qbittorrent"], "allowed_target_roots": ["/vol1/1000/Library"],
    "hook_token": "<明文>", "qbittorrent_url": "", "qbittorrent_username": "",
    "qbittorrent_password": ""}
   ```
   `bool` / `int` / `array` 必须是真类型，**不得**返回 JSON 字符串。
   `PUT /api/settings` 接受同样的**已解析类型**的**部分**对象，类型不符 → 400；返回更新后的全量对象。
   设置项含 `config_sync_interval_seconds`。
   两个根目录键的初值取自 `DEFAULT_SOURCE_ROOT` / `DEFAULT_TARGET_ROOT` 两个 env（**只影响首次播种**，
   对已存在的 DB 不生效）；默认单值 `["/downloads"]` / `["/library"]` 会被**有界改写**（见 §3 与 `docs/guides/DEPLOYMENT.md` §6.5）。

5. **`GET /api/system/info`**：`path_map` 是对象 `{"container_to_host": {...}, "host_to_container": {...}}`；
   另有 `processors{}`、`config_sync{}` 与 `rename_logs[]`（见
   `docs/contracts/PROCESSORS.md` §5/§7）。

6. **所有面向用户的时间字段统一走 `format_time()`**（冻结）：即本地时区（`settings.timezone`，默认
   `Asia/Shanghai`）的 `YYYY-MM-DD HH:MM:SS`，与任务对象的 `created_at` / `finished_at` **同一种格式**。
   **`GET /api/tasks/{id}/logs` 的 `ts` 与 `/api/tasks/{id}/stream` 的 SSE 载荷都包含在内。**
   原实现让日志 `ts` 直吐数据库里的 UTC 原值（`...Z`），后果是**同一个任务详情页上，
   任务时间是本地时间、日志时间是 UTC，差 8 小时**——用户一定会当成 bug 报上来。
   **时间格式没有任何例外**。需要原始 UTC 值的场景另加 `*_utc` 字段，
   **不允许同一个字段承担两种语义**。
   前端相应地**直接显示**该字符串，不要自行按 UTC 解析或再加偏移。

7. **超集规则：文档列出的字段是实现必须提供的下界，实现可以多返回**（冻结）。多出来的字段
   必须**只增不改** —— 已列字段的名字、类型、语义一个都不许变。已知的超集（这一条就是它们的存在依据）：
   - `DELETE /api/libraries/{id}` 多返回 `library_id`；
   - `GET /api/tasks/{id}/logs` 的每个 item 多返回 `task_id`；
   - `GET /api/auth/me` 多返回 `authenticated`、`created_at`；
   - `GET /api/system/info` 多返回 `details`、`encoding`、`disk`、`mounts[].read_only`；
   - `GET /api/stats/overview` 多返回 `qbittorrent{status,version}`、`recent_limit`。

   前端可以安全读取这些字段，但**不得**把它们当成契约去改（改了就要同步本表）。

8. **`POST /api/libraries/{id}/validate` 的响应是扁平对象**，**不是** `{ok, checks:[{name,ok,message}]}`
   （实现不返回 `checks[]`）。权威形状见 §5 媒体库表那一行，共 **21 个字段**；
   `errors[]` 非空即预检失败，`warnings[]` 只提示不阻塞。`?probe=`（默认 `true`）控制是否做
   **实证 `link()` 探针**（在源树有界递归取一个已存在的普通文件 → link 到目标根临时名 → 删掉并还原目标目录时间戳；**不写源目录**）；
   因此 `same_filesystem` 的类型是 **`bool | None`** 三态（`null` = 判不了，例如源树里找不到可链的普通文件）。
   落点要分清：`same_filesystem is false`（**已证实**建不出硬链接）⇒ `ok=false` 且原因**进 `errors[]`**；
   `null` ⇒ 只进 `warnings[]`；`true` 但仅据挂载 ID / `st_dev` 预筛（未实证）⇒ 也进 `warnings[]`。
   而 `POST /api/libraries` / `PUT /api/libraries/{id}` **不做**这个判据（跨挂载**不阻断建库**）；
   **跑任务**（`/run`）时判据即使得到 `false` 也只写 `Task.result.warnings[]` 并继续尝试 ——
   最终结论由执行前后的逐文件核对给出。详见 §2「同一挂载校验」与 `docs/contracts/API.md` §3.8。

9. **`/api/hooks/*` 的参数只从 URL 查询串或 urlencoded 表单体读取，JSON 请求体被静默忽略。**
   qBittorrent 的"运行外部程序"用 `curl --data-urlencode`（即 urlencoded 表单），完全匹配。
   但若有人改成 `-H 'Content-Type: application/json' -d '{...}'`，会得到 `torrent.name` 为空、
   命中 0 个库 —— 这是静默失败里最难查的一种，文档与排障必须点名。**查询串的优先级高于表单体**。

10. **`permission_mode` 是"默认关闭的真实开关"，不是一个"看起来能开"的装饰**：
   `LibraryInput.permission_mode`（默认 `False`）→ `libraries.permission_mode`（`0/1`）→ 执行器读出后传给
   `script_adapter.run_hardlinks(..., permission_mode=...)` → `app/processors/hardlink.py` →
   `create_hard_links(source_dir, target_dir, *, skip=..., permission_mode=...)`。开启时把**目标侧**
   （库根目录 + 本次处理到的每个一级文件夹的目标子树，**含账本里体积一致而被跳过的旧作品**）改成 `0777`，
   符号链接绝不跟进；关闭时**一个 `chmod` 都不做**。副作用的真源是 **inode 共享**：源文件权限位一起变
   （内容/大小/inode 号/mtime 不变 ⇒ 不触发 §7 的源完整性核对），所以 WebUI 在**从关到开**时必须二次确认
   （`confirmDialog`，见 `web/js/pages/library_edit.js`），任务日志每次以 WARNING 记录改了多少目录/文件。
   契约真源见 `docs/contracts/BASELINE.md` §1.6 与 §4 的 B5。


### 认证

| 方法 路径 | 请求 | 响应 |
|---|---|---|
| `GET /api/auth/me` | — | `{"needs_setup":bool,"username":str\|null}` |
| `POST /api/auth/setup` | `{username,password}` | 201 `{"username":...}`；已有用户 → 409 |
| `POST /api/auth/login` | `{username,password}` | 200 + `Set-Cookie: mlm_session=...`；失败 401 |
| `POST /api/auth/logout` | — | 204 |
| `POST /api/auth/password` | `{old_password,new_password}` | 204 |

密码策略：长度 ≥ 8。`ADMIN_USERNAME` / `ADMIN_PASSWORD` 环境变量存在时，首次启动自动创建管理员（`settings.seed`）。

### 媒体库

| 方法 路径 | 说明 |
|---|---|
| `GET /api/libraries` | 分页列表（见 §5.0 信封）；每项附带 `stats`：最近一次任务状态 |
| `POST /api/libraries` | 新建；body 见 §5.1 |
| `GET /api/libraries/{id}` | 详情 |
| `PUT /api/libraries/{id}` | 更新（整体替换） |
| `DELETE /api/libraries/{id}?confirm=true` | 无 `confirm` → 400（文案含 `?confirm=true`）；**只删规则，绝不删除已创建的硬链接**；响应 `{"deleted":true,"hardlinks_kept":true,"library_id":N,"link_records_removed":K}`（`link_records_removed`：本库被清掉的账本行数；`library_id` 是实现返回的超集，见 §5.0 第 7 条） |
| `POST /api/libraries/{id}/validate?probe=true` | 预检。**响应是扁平对象，不是 `{ok, checks[]}`**（见 §5.0 第 7 条），**21 个字段**：`{ok, source_dir, target_dir, source_exists, target_exists, same_filesystem, probe, errors, warnings, library_id, library_name, type, processor_id, processor_ok, processor_name, processor_kind, log_file, log_path, source_folders, target_dir_exists, same_filesystem_paths}`。`same_filesystem` 为 **`bool \| null`**；`false`（已证实建不出硬链接）⇒ `ok=false` 且原因进 `errors[]`，`null` / 未实证的 `true` ⇒ 进 `warnings[]`；而**建库与跑任务都不被它硬拦** |
| `POST /api/libraries/{id}/run` | `{scope:{"mode":"full"}\|{"mode":"folder","folder":"X"}, force:bool}` → **202 + Task 对象**（含 `id`、`status:"queued"`）。**`force=true` 只允许配合 `scope.mode=="folder"`**，否则 **400**，文案：「整库强制重跑已停用：force 只允许配合 scope.mode='folder'（单个作品）使用。要重做作品，请到「作品记录」里删除对应作品的记录，下次运行时这些作品会自动补做，其余作品不受影响。」整库重跑的替代路径 = 下面的三个 `/works*` 端点（按作品删记录后下次运行自动补做） |
| `GET /api/libraries/{id}/works` | **作品记录（支持服务端搜索与分页）**：查询参数 `q`（可选，对**作品文件夹名**做子串匹配，ASCII 大小写不敏感，`%`/`_`/`\` 按字面量，空/纯空白 = 不过滤）、`limit`（**默认 50**，收敛到 **1..200**）、`offset`（**>= 0**）→ `{items:[{folder,size,linked,source_dir,target_dir,target_path,first_at,updated_at,target_exists,rename_recorded,target_files,target_bytes,target_loose}], q, total, ledger_total, limit, offset, library_id, library_name, source_dir, target_dir, rename_log_path, rename_entries, rename_missing:[…], rename_only:[…]}`。`total` = `q` 过滤后的匹配数；`ledger_total` = 本库账本的**全部**记录数，与 `q` 无关。`target_exists` = 目标目录里这个作品文件夹还在不在；`rename_recorded` = 本库重命名 JSON 缓存里还有没有它；`target_files` / `target_bytes` / `target_loose` 由**遍历目标目录**算出，因此**只对当前页返回的作品算**（目标目录不存在时三项为 `0`）；`rename_entries` / `rename_missing` / `rename_only` 仍按**整库账本**计算，不受 `q` / 翻页影响。两个差集只做提示、**不自动修复**，也不改动任何文件 |
| `POST /api/libraries/{id}/works/delete` | **删除作品记录（破坏性）**：体 `{folders:[…], dry_run:false, confirm:false}`。`dry_run=true` 只回执 `{dry_run:true, items:[{folder,target_path,in_ledger,rename_recorded,exists,files,bytes,loose_files}]}`；非 dry-run 且 `confirm=false` ⇒ **400**；执行时删**三处** —— 账本行、重命名缓存条目、目标目录里整个作品文件夹（`shutil.rmtree`，符号链接**只删链接本身**），返回 `{deleted:true, ledger_removed:[…], rename_removed:[…], items:[{…,target_removed,error}], next_run_note}`。**源目录一个字节都不动**。dry-run 的清单由**不分页**的 `search_records(..., limit=None)` 现算（整库范围内） |
| `POST /api/libraries/{id}/works/rename` | **立即重命名选中作品**：体 `{folders:[…]}` → **202 + Task**（`kind="rename"`、`trigger="manual"`、`scope={"mode":"works","folders":[…]}`、`payload.force=true`）；`hardlink_only` 处理器 ⇒ **400**；库停用 ⇒ **409** |

#### 5.1 LibraryInput

> **字段语义**：处理器由 **`processor_id`** 指定（字符串外键，必须存在于处理器注册表，
> 见 `docs/contracts/PROCESSORS.md` §3）；`type` 是**自由文本标签**（仅供展示与统计，不约束 `processor_id`）；
> `processor_ok` 表达"处理器是否还存在"；另有 `log_file`。

```json
{
  "name": "番剧",
  "type": "Bangumi",
  "processor_id": "bangumi",
  "enabled": true,
  "source_dir": "/vol1/1000/Download/qbittorrent/Bangumi",
  "target_dir": "/vol1/1000/Library/Bangumi",
  "match_categories": ["ban"],
  "match_tags": [],
  "match_path_prefixes": ["/vol1/1000/Download/qbittorrent/Bangumi"],
  "orphan_cleanup": true,
  "permission_mode": false,
  "log_file": "bangumicache.log"
}
```
响应额外带 `"warnings": []` 和 `"stats": {...}`（形状见 §5.0 第 1 条）。

> ⚠️ **`permission_mode` 是显式开关（默认关闭），落点见 §5.0 第 9 条**：
> 硬链接与源文件共享 inode，`chmod` 目标侧的硬链接会**同时改掉 qBittorrent 源文件的权限位**
> —— 这正是它**默认关闭**、且界面从关到开**必须二次确认**的原因（代价由开启者显式承担）。
> API 接受并**保存**该字段（响应回读是 `true`/`false`，不是 `null`）；开启时的动作范围、失败处理与
> 日志口径见 `docs/contracts/BASELINE.md` §1.6 与 B5。

理由：库里必须存**容器路径**，这样 WebUI 展示、任务参数与日志归属才有统一的坐标；
硬链接账本也是拿容器路径比对日志 —— 当前账本不再被改写
（见 `docs/contracts/PROCESSORS.md` §5.3），但"只存容器路径"这条不变。

### 任务

| 方法 路径 | 说明 |
|---|---|
| `GET /api/tasks` | 查询参数 `status`,`library_id`,`q`,`limit`(默认50,最大200),`offset`；按 §5.0 信封返回 |
| `GET /api/tasks/{id}` | 详情（含 `result`） |
| `GET /api/tasks/{id}/logs?after_id=0&limit=500` | 增量日志（轮询用）：`{"items":[{"id","task_id","ts","level","message","stream"}],"last_id":N}`；`after_id` 传上次的 `last_id`（**是日志主键、不是时间戳**）。`task_id` 是实现返回的超集，见 §5.0 第 7 条 |
| `POST /api/tasks/{id}/retry` | 以同一 payload 新建任务 → **202 + Task 对象**；仅 `failed`/`cancelled` 可重试 → 否则 409 |
| `POST /api/tasks/{id}/cancel` | 仅 `queued` 可取消（204）；`running` → 409（串行队列不可中断） |
| `DELETE /api/tasks/{id}` | 删除历史记录（不影响磁盘） |
| `GET /api/tasks/{id}/stream` | **按任务**的 SSE：首帧 `event: task`（任务快照）→ `event: log`（新日志，载荷与 `/logs` 的 `items` 逐字段同形）→ `event: done`；前端可降级为轮询 `/logs`。**注意是命名事件流**，只读第一条 `data:` 会拿到任务快照而不是日志行 |

### 待办

| 方法 路径 | 认证 | 说明 |
|---|---|---|
| `GET /api/todos` | 需要 | 查询参数 `status`(`open` 默认 / `done` / `all`)、`kind`、`library_id`、`q`（按作品**朴素子串**筛 `title`/`detail`/`dedupe_key` 三列，空 = 不过滤）、`limit`(默认50,最大200)、`offset`；按 §5.0 信封返回。**非法 `status`/`kind` ⇒ 400** |
| `GET /api/todos/summary` | 需要 | 计数：`{open,done,total,by_status,by_kind,libraries,enabled_kinds,disabled_kinds,detectable_kinds,undetectable_kinds}`，裸对象。`by_kind` **五种全列**（无条目的为 0），前端筛选栏直接吃它；两个 `*detectable*` 键给「一键检测」用 |
| `POST /api/todos/{id}/resolve` | 需要 | 打钩：`open` → `done`。**无请求体**，200 + 改后的待办对象；不存在 ⇒ 404；**幂等**（已是 `done` 不重写 `resolved_at`）；**不删任何数据** |
| `POST /api/todos/{id}/reopen` | 需要 | 恢复：`done` → `open`（清空 `resolved_at`）。同上，200 + 对象 / 404 |
| `GET /api/todos/detect` | 需要 | **一键检测**：`kind` / `library_id` / `limit`（默认 200、最大 500）/ `offset`；**纯只读** —— 把"记录里的文件路径"与磁盘实况对一遍，返回 `{checked,found,candidates[],detectable_kinds,undetectable_kinds}`。每个候选带人话证据 `detect_evidence`；**绝不改任何待办的 `status`** |
| `POST /api/todos/detect-resolve` | 需要 | **确认后批量打钩**：体 `{"ids":[...]}` ⇒ `{requested,resolved,skipped[],missing[]}`；**打钩前再核对一遍**实况（变了就进 `skipped[]` 并说明原因），空 `ids` 合法。语义与 `/resolve` 完全一致：只改状态、**不删数据** |

> ⚠️ **上表是 6 条端点**；列表的 `limit`/`offset` 就是分页（前端看板 20 条一页），
> 明细**没有**"每类 50 条"的上限。两条检测端点只列候选、由用户在弹窗里勾选后才落笔，见 §6.10 的
> `detect_resolved()` / `resolve_detected()`。

> **没有 `DELETE`（刻意）**：删掉一条就再也说不清"这事发生过几次、上次什么时候"。去重与"重复发生"
> 的完整语义（同一 `(kind,dedupe_key)` 有 `open` ⇒ 只累加；曾 `done` 后再发生 ⇒ 新开一条并保留历史）
> 见 §6.10；字段级的 JSON 契约见 [`docs/contracts/API.md`](API.md) §5.9。

### 统计 / 设置 / 系统

| 方法 路径 | 说明 |
|---|---|
| `GET /api/stats/overview` | `{tasks:{queued,running,success_24h,failed_24h,total},libraries:{total,enabled},recent:[Task...],queue:{running_task_id,queued_count}}` |
| `GET /api/settings` | 全量键值。**`hook_token` 返回明文**（设置页要一键复制回调命令）；`qbittorrent_password` 已设置时返回 `"***"`，未设置返回 `""` |
| `PUT /api/settings` | 部分更新；值 `"***"` 表示保持原值 |
| `POST /api/settings/test-qbittorrent` | 测试 qBittorrent 连通性。body 可选 `{url,username,password}`；**缺省则用已保存的设置**（`password` 为 `"***"` 时用库里存的真值）。响应 `{ok:bool,version:str\|null,message:str,latency_ms:int}`；连不上返回 **200 + `ok:false`**（那是业务结果，不是 API 错误），认证失败同理 |
| `GET /api/system/health` | **无需认证**；`{status:"ok"\|"degraded", checks:{db,data_dir,downloads_readable,library_writable,processors,config}}`（**六项**）；容器 HEALTHCHECK 用 |
| `GET /api/system/info` | 路径映射表、处理器/配置统计、挂载判定（`same_filesystem` 三态）、磁盘与日志、版本 |

### 回调

| 方法 路径 | 认证 | 说明 |
|---|---|---|
| `POST /api/hooks/qbittorrent` | `?token=` 或 `Authorization: Bearer` | 查询/表单参数：`name,hash,category,tags,content_path,save_path,kind,root_path`；返回 `{"matched":int,"tasks":[id...],"ignored":[...]}` |
| `POST /api/hooks/scan` | 同上 | 立即触发一次目录扫描 |

匹配逻辑（**AND 语义，已冻结**）：按 `enabled` 的库依次判断 `match_categories ∩ [category]`、`match_tags ∩ tags`、`content_path` 是否以某个 `match_path_prefixes` 开头。
- **每个"已填写"的条件都必须满足**才算命中（逐条 `return False`）——填了 `category` 又填了 `tag`，只满足其中一个 ⇒ **不匹配**。
- 三个列表**全空 ⇒ 视为不匹配**（避免误抓）。
- 未命中的库要在 `ignored_details[]` 里给出中文原因，便于 WebUI/日志解释"为什么没进这个库"。
- 命中多个库 → 全部入队。

> ⚠️ 本句原文只写了"依次判断"、没有写"或"，属歧义；实现（`app/routers/hooks.py` 的 `_match_library()`）把它定为 **AND**，此处按实现收紧。这是**用户可见行为**，文档与 WebUI 帮助文案必须一致。

---

## 6. Python 模块契约

### 6.1 `app/config.py`

```python
class Settings:            # pydantic BaseSettings 风格，但用 stdlib 读取 os.environ
    data_dir: str          # env DATA_DIR, default "/data"
    db_path: str           # <data_dir>/mlm.db
    config_dir: str        # env CONFIG_DIR, default "/data/config"
    default_source_root: str  # env DEFAULT_SOURCE_ROOT, default "/vol1/1000/Download"（**只用于首次播种**）
    default_target_root: str  # env DEFAULT_TARGET_ROOT, default "/vol1/1000/Library"（**只用于首次播种**）
    host_path_map: dict[str,str]  # env HOST_PATH_MAP (JSON) 容器路径 -> 宿主路径，**仅用于展示**
    port: int              # env PORT, default 18081
    secret_key: str        # env SECRET_KEY 或 <data_dir>/secret.key 自动生成
    admin_username: str|None
    admin_password: str|None
    timezone: str          # env TZ, default "Asia/Shanghai"
settings: Settings        # 单例
```
`Settings` 里的 `log_dir` 与 `config_dir` 是两个派生目录。

#### ⚠️ `host_path_map` 的不变量（容易踩的坑）

**`settings.host_path_map` 的每一个键都必须是「以 `/` 开头的绝对容器路径」。** 键是容器路径，值是宿主路径。

- **默认值是空表（恒等映射）**：容器路径 = 宿主路径，`DEFAULT_HOST_PATH_MAP = {}`，
  `paths.to_host()` 原样返回容器路径；只有「容器路径 ≠ 宿主路径」的非恒等部署才需要自己填。
  若填了键，键必须是**绝对容器路径**（如 `"/vol1/1000/Download/qbittorrent"`），
  **不能**写成 `"downloads"` 这种缺前导斜杠的形式。
- `_load_host_path_map()` 必须对**所有分支**（含 `raw` 为空、JSON 非法、非 dict、
  以及最终退回默认值的那几处）统一过一遍「键补前导斜杠」归一化，
  **不得**只归一化 env 传来的那一支。
- `host_to_container_map()` 必须**再做一次防御性归一化**，保证返回值也是绝对路径；
  否则 `paths.to_host()` 的"显示宿主路径"会静默失效（键少了斜杠就匹配不上）。
  `HOST_PATH_MAP` **只用于界面展示**，不参与任何功能判定。

### 6.2 `app/db.py`

```python
def connect() -> sqlite3.Connection      # check_same_thread=False, WAL, row_factory=sqlite3.Row
def get_conn() -> sqlite3.Connection     # thread-local
def init_db() -> None                    # 建表 + 默认 settings + 播种 admin
@contextmanager
def tx() -> Iterator[sqlite3.Connection] # 事务
def get_setting(key: str, default=None) -> str|None
def set_setting(key: str, value: str) -> None
def all_settings() -> dict[str,str]
```

### 6.3 `app/security.py`

```python
def hash_password(pw: str) -> str                 # scrypt$N$r$p$salt$hash
def verify_password(pw: str, stored: str) -> bool # 恒时比较
def make_session(username: str) -> str            # "username.expiry.hmac"
def read_session(token: str) -> str|None
def require_user(request) -> str                  # FastAPI 依赖，未认证抛 401
def check_hook_token(token: str|None) -> bool
```

### 6.4 `app/paths.py`

```python
def to_host(container_path: str) -> str           # 用 host_path_map，无匹配则原样返回
def to_container(host_path: str) -> str
def normalize(path: str) -> str                   # 绝对化 + 去尾斜杠（保留根）
def is_inside(child: str, parent: str) -> bool
def existing_ancestor(path: str) -> str           # 取最深的已存在祖先（用于 mount ID / st_dev 降级 / 可写探测）
def check_same_filesystem(source: str, target: str) -> tuple[bool|None, str|None]
    # 返回 (ok, error_message)；ok 为三态：True/False/None（无法实证）；error_message 为上面 §2 的中文全文
def check_cross_mounts(source_dir: str, target_dir: str) -> tuple[bool,str|None]
    # mount ID 预筛：相同不代表能建（必要不充分）
def assert_allowed(path: str, roots: list[str], kind: str) -> None  # 抛 ValueError
def probe_cross_filesystem(source_dir: str, target_dir: str) -> tuple[bool,str|None]
    # 实证探针：有界递归取一个已存在普通文件 → os.link 到目标根临时名 → unlink（**不写源目录**）
```

### 6.5 `app/script_adapter.py`

> ⚠️ 运行期执行的是原生实现 `app/processors/hardlink.py`（可观察输出由
> `tests/test_hardlink_native.py` 的冻结黄金值锁定）。
> `ScopedOs` / `LinkOutcome` / `RenameOutcome` 仍然有效；契约真源见
> [`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §2.2 / §3.3 / §8。

```python
class ScopedOs:
    """os 代理：仅在 scoped_path 上过滤 listdir/ scandir 结果，其余属性透传真实 os。"""
    def __init__(self, real_os, scoped_path: str, allowed: list[str])
    def __getattr__(self, name)

@dataclass
class LinkOutcome:
    folder: str                      # 一级文件夹名；"*" 表示全量
    created: list[str]
    failed: list[tuple[str,str]]     # (target_path, error)
    skipped: list[str]               # 因体积一致而跳过
    already_linked: list[str]        # 目标已存在且**就是源文件的硬链接**（同 inode）
    orphans_removed: list[str]
    logged: list[tuple[str,int]]     # (folder, size) 本次写入日志的记录
    stdout: str
    exception: str|None              # 未捕获异常（如源目录不存在）

@dataclass
class RenameOutcome:
    renamed: list[tuple[str,str]]    # (old_basename, new_basename)
    failed: list[tuple[str,str]]
    skipped_folders: list[str]
    stdout: str
    exception: str|None
    # —— 实际实现在 app/processors/engine.py：renamed/skipped/errors/
    #    kept_unresolved_episode/folders_scanned/folders_processed/warnings/log_file/
    #    stdout/exception + 下面五个）——
    kept_unresolved_episode: list[str]   # 命名引用了集数却没解析出集数 ⇒ 保留原名
    name_collisions: list[dict]          # L4「可见性」：改名时目标名已被占用（{dir,target,previous,current,kind}）
    renamed_with_suffix: list[dict]      # L2「防丢」：争名加后缀（{dir,old,clean,target,size,reason,displaced}）
    displaced_targets: list[dict]        # L2「防丢」：原有文件让位（{dir,old,target,clean,size}）
    episode_unresolved: list[str]        # L4「可见性」：**所有**没解析出集数的文件
    extras_classified: list[dict]        # L3「特典排除」：命中判据、被排除在改名之外（{dir,name,size,pattern}）
    unknown_params: list[dict]           # KI-11：命名里用了不存在的变量名 ⇒ 该段留空（{label,file,params}）
    rendered_targets: dict[str,str]      # 仅运行期记账：目标绝对路径 → 作用域标签

def run_hardlinks(*, source_dir: str, target_dir: str, log_file: str,
                  folder: str|None = None, folders: list[str]|None = None,
                  force: bool = False, orphan_cleanup: bool = True,
                  skip: dict[str,int]|None = None) -> LinkOutcome
# 重命名**不经过本模块**：执行器直接调用配置驱动的引擎
# app/processors/engine.py::run_rename(spec, root, *, log_file=None, folder=None,
#     folders=None, force=False, now=None, works_dir="", override=None) -> RenameOutcome
def remove_log_entries(log_file: str, source_dir: str, folder: str) -> int
    # 从日志副本中删除该 folder 的记录；返回删除行数

# 硬链接账本 hard_links.log 继续写入；每个媒体库各有独立的
# 重命名日志，见 docs/contracts/PROCESSORS.md §5 与 app/processors/logs.py。
```

> `run_hardlinks()` 有 `folders`（`scope.mode="works"` 的作品清单，作品名 = 目标目录
> 一级文件夹名）与 `skip`（每库账本给出的 `{folder: size}`；`None` = 读全局 `hard_links.log` 的老路径，
> 黄金值用例只走这条）。`force` 只清**当前作用域**里的名字（旧日志里这些行 / 重命名 JSON 缓存里
> 这些条目），**不再有"整库"语义的入口**；`run_rename()` 同步新增 `folders`。
> 契约见 [`BASELINE.md`](BASELINE.md) §4 与 [`PROCESSORS.md`](PROCESSORS.md) §2.1。

`run_hardlinks` 实现要点（必须逐条满足）：

1. `mod = hardlink_native`（模块级单例）。
2. 覆盖 `mod.LOG_FILE = log_file`。
3. **接管 root logger**：确保 `logging.getLogger()`（root）已存在一个指向 `log_file` 的 `FileHandler`，`format='%(asctime)s - %(message)s'`，`level=INFO`，`encoding='utf-8'`；若已存在同路径 handler 则复用。**不得**移除其他 handler。
   > 原生实现里的 `logging.basicConfig` 在 root 已有 handler 时是 no-op，因此格式与编码由应用保证。
   > **「日志隔离」**：这个 handler 上还挂了 `_BookkeepingFilter`（`app/script_adapter.py` 的
   > `_LOG_BOOKKEEPING_PREFIXES`），让该文件**只**接收两类记账行 —— `Source: <源目录> - Folder: <文件夹> -
   > Folder size: <字节>`（`Source: ` 行还必须**同时**含 ` - Folder: ` 与 ` - Folder size: `）与
   > `Deleted orphaned hard link folder: <目录>`；其余记录（uvicorn 访问日志、执行器任务日志、异常回溯…）
   > 在这个 handler 上被丢弃。**只过滤、不清理、不轮转**（历史行一个字节都不删），也**不影响其它 handler**
   > （控制台 / 任务日志照旧收到全部记录）。原因：root 是进程级单例，不挡的话整个应用的日志都会灌进这个
   > "数据文件"、无限膨胀。回归用例：`tests/test_file_log_isolation.py`。
   > 注意：`create_hard_links` 内 `print()` 的输出与 `logging` 输出**格式不同**：日志行形如
   > `2026-10-09 01:20:11,123 - Source: /downloads/Bangumi - Folder: X - Folder size: 123`。
4. `folder` 非空时：`saved = mod.os; mod.os = ScopedOs(saved, source_dir, [folder])`，`try/finally` 恢复。
5. `orphan_cleanup=False` 时：**必须**在不改源码的前提下抑制孤儿清理。实现方式（择一，须在实现中注释说明）：
   - 方案：把 `mod.shutil` 换成代理，拦截 `rmtree` 并记录调用、抛出一个内部哨兵异常？**不可**——会中断流程。
   - **采用方案**：`orphan_cleanup=False` 时，先把应用日志副本中"源已不存在"的条目临时剔除到内存、跑完再回写。
     即：读日志 → 过滤出 `source_dir/<folder>` 仍存在的行 → 写回 → 执行 → 结果不变。
     > 更简单且等价：若 `orphan_cleanup=False`，先把日志里所有"源不存在"的行**备份并从文件移除**，
     > 执行后再合并回写。这样 `create_hard_links` 的孤儿分支不会命中任何条目。
   - 该实现必须写单元测试：源被删 + `orphan_cleanup=False` ⇒ 目标硬链接目录**仍存在**。
6. `force=True` 时先 `remove_log_entries(log_file, source_dir, folder)`。
7. `contextlib.redirect_stdout(io.StringIO())` 包裹调用；同时把捕获内容逐行写入应用日志（`stream="stdout"`）。
8. 解析 stdout：
   - `已创建硬链接: <path>` → `created`
   - `创建硬链接失败: <path> - <err>` → `failed`
   - `文件夹已记录且体积一致，跳过: <folder>` → `skipped`
   - `已删除旧记录: <folder>` → 记录 INFO
   - `Deleted orphaned hard link folder: <path>` → `orphans_removed`
   - `已记录文件夹体积: <folder> - <size>` → `logged`
   - `已删除文件体积记录`→ INFO
   - 未匹配行 → 原样以 DEBUG 记录
9. **跨挂载前置警告（不硬拦）**：调用前跑三级判据（实证探针 / mount ID 预筛 / 无 `/proc` 时才用 `st_dev`）；
   判定为跨挂载 ⇒ **只写 `warnings[]`（中文、带可执行修法）然后照常调用建链实现**（`app/processors/hardlink.py`）—— 真正的失败由 `link_failed` 与执行后核对给出结论。
10. 调用后逐文件核对：对所有 `created` 的 target，断言 `os.stat(target).st_ino == os.stat(source).st_ino` 且 `st_dev` 相同；不符者移入 `failed` 并报"非硬链接"。
11. **源完整性核对**：任务**前**对源目录逐文件快照（相对路径 / `size` / `st_ino` / `mtime_ns`），任务**后**复核 ⇒
    `Task.result.source_integrity = {ok, changed[]}`；硬链接本身造成的 `nlink` +1 与 `ctime` 变化**不算**差异（避免误报）；
    有差异时同时写 `warnings[]` 中文告警并列出路径。（源目录的 `:ro` 挂载与跨路径硬链接在内核层面互斥，这是替代它的**可验证保证**。）

重命名要点（现行实现 `app/processors/engine.py`，执行器直接调用 `run_rename()`）：

1. 执行器直接调用引擎：`root` = 媒体库的 `target_dir`；`log_file` 是日志文件**完整路径**（缺省时引擎按 spec 的 `log_file` + `settings.log_dir` 解析）；另有 `folder` / `folders` / `force` / `override` 参数（见上面的签名）。
2. `folder` 非空 → `ScopedOs(mod.os, root_dir, [folder])`。
3. `force=True` → 从 JSON 日志删除该 folder 键。
4. 函数名：Bangumi 与 Movie **都是 `rename_bangumi_episodes()`**（见 BASELINE §3）。
5. 解析 stdout：`Renamed: <old> -> <new>`（INFO）/ `Renamed (suffix): <old> -> <new>`（L2 加后缀，INFO）/ `Displaced: <clean> -> <new>`（L2 原有文件让位，INFO）/ `Kept (no episode): <label>`（WARNING）/ `Extra (kept): <作用域标签>`（L3 特典排除：命中判据、保留原名；引擎输出英文前缀、执行器映射成中文 INFO）/ `Error renaming <old>: <err>` / `Skipping folder: <folder> (...)`。**未匹配行一律按 DEBUG 记**（`app/executor.py` 的 `_log_parsed_line()`）—— 上面这些前缀都已在表里，不会再掉进 DEBUG。
6. **L3 特典排除（本应用新增的能力）**：命中全局设置项 `extras_patterns` 的散装特典**退出改名流程** —— 不参与正片争名、不计入 `episode_unresolved`，**不改名、也不移动**（保留原名留在原位置；硬链接阶段已经建好的目标侧文件保持原样），**不会创建任何目录**。引擎对每个命中文件打一行 `Extra (kept): <作用域标签>`，并把 `{dir, name, size, pattern}` 记进 `extras_classified`（`pattern` = 命中的那条判据，便于"发现误判就把它写精确"）。命中判定在**候选过滤阶段**就完成、且**先于**「每个扩展名只留最大者」的削减，否则特典会先抢到"同扩展名最大"的名额、随后被剔掉，导致正片继续等待而下一轮因"体积一致"整目录跳过（已有回归用例）。

### 6.6 `app/queue.py`

```python
@dataclass
class TaskSpec:
    library_id: int|None; kind: str; trigger: str; scope: dict; payload: dict

class TaskQueue:
    def start(self) -> None
    def stop(self, timeout: float = 10) -> None
    def submit(self, spec: TaskSpec) -> int      # 返回 task_id，立即 INSERT status='queued'
    def cancel(self, task_id: int) -> bool       # 仅 queued
    @property
    def running_task_id(self) -> int|None
    def queued_count(self) -> int
queue: TaskQueue
```
单工作线程 `threading.Thread(daemon=True)`，循环取最早 `queued` 任务并执行；异常必须捕获并写入 task + 日志。

### 6.7 `app/executor.py`

```python
def execute_task(task_id: int) -> None
```
流程：
1. 读 task/library；`status='running'`, `started_at`。
2. 预检：源可读、目标父目录存在（不存在则 `os.makedirs(target_dir)`——与建链实现一致）、同文件系统。
   - **注意**：`target_dir` 的创建由建链阶段自己做；预检只验证其**最近已存在祖先**与源**在同一个挂载**上（三级判据见 §2）。
3. `kind` 含 link → `run_hardlinks(...)`；解析 `LinkOutcome`，写日志与 `result`。`scope.mode="works"`（`scope.folders` = 作品名清单）时，建链前用 `link_records.folder_sizes(library_id, source_dir=…, target_dir=…)` 造出 `skip` 字典，连同 `folders=work_folders` 一起传下去；建链后把**本次真正写进日志**的作品 `upsert` 进账本（含 `linked` 计数），并 `drop` 被孤儿清理删掉的目标文件夹；`force` 时被强制的作品会先从 `skip` 里摘掉，否则旧账本行会让"强制重跑"变成静默空操作。
4. `kind` 含 rename → `engine.run_rename(spec, target_dir, …)`（同样支持 `folders=work_folders`）。
5. 汇总 `result = {linked, skipped, link_failed, already_linked, renamed, rename_failed, orphans_removed, duration_ms}`
   （`skipped` = 文件夹级跳过 + `already_linked`；"目标已存在且是源文件的硬链接"由
   `_split_already_linked()` 从 `failed` 里改判，见 §6.5 要点 10）。
   另有两个与重命名相关的键：`kept_unresolved_episode`（命名引用了集数却没解析出集数、
   因而**保留原文件名**的文件数）与 `samples.kept_unresolved_episode`（明细，路径相对媒体库根目录）；
   它们**不计入** `rename_failed`，`status` 仍是 `success`，但 `warnings` 里有一条中文汇总告警
   （语义与判定条件见 [`docs/contracts/API.md`](API.md) §4.1、[`docs/contracts/BASELINE.md`](BASELINE.md) §5）。
   **L4「可见性」+ L2「防丢」**再加四个键：`name_collisions` + `samples.name_collisions`（改名时目标文件名已被占用**的事实清单**：`within_run` = 本次运行内更早的文件占用了同一个目标名、`existing_target` = 目标上本来就有的同名文件）、`renamed_with_suffix` + `samples.renamed_with_suffix`（**实际加了 `" ({index})"` 后缀保留**的文件数，`reason` 同上两种）、`displaced_targets` + `samples.displaced_targets`（**原有文件让位**给体积更大的新文件的条数，**不删除**），以及 `episode_unresolved` + `samples.episode_unresolved`（**所有**没解析出集数的文件，是 `kept_unresolved_episode` 的超集）。**没有覆盖**：争名由 ` (2)`/` (3)`… 化解，找不到空位则保留原名 + 记 `error`。`name_collisions` 与 `renamed_with_suffix` 非空时各有一条**可计数**的 `warnings` 汇总告警（`displaced_targets` 不写告警，只进 INFO 日志与摘要）；`name_collisions` 与 `renamed_with_suffix` 明细在日志里是 **WARNING**、`displaced_targets` 与 `episode_unresolved` 是 **INFO**；收尾摘要新增「同名冲突 X，未解析出集数 Y」与「冲突加后缀 N，原有文件让位 M」两段。
   **L3「特典排除」**再加一个键：`extras_classified` + `samples.extras_classified`（命中 `extras_patterns`、**被排除在改名之外**的散装特典数量；明细每项 `{dir, name, size, pattern}`）。特典**没有改名、也没有移动**，因此不计入 `renamed` / `name_collisions` / `episode_unresolved`；`> 0` 时 `warnings` 里有一条汇总告警（列出前 10 条 `文件名（判据）`），明细日志是 **INFO**（引擎 stdout 的 `Extra (kept): …` 被执行器映射成中文「特典保留原名（判据命中，未改名、未移动）：…」），收尾摘要新增「特典保留原名 N，」。
    **再加一个键（KI-11）**：`unknown_params` + `samples.unknown_params`（命名段序列里用了"上下文没有"的变量名、因而该段被**留空**的文件数；明细每项 `{label, file, params}`，`label` 是作用域标签、`file` 是源文件名、`params` 是被留空的变量名）。它是**唯一不影响文件名正确性**的一类：文件名少了一段、`status` 仍是 `success`。明细日志是 **WARNING**（「命名里的未知变量被留空（文件名少了一段）：…」），收尾摘要新增「命名未知变量 N，」，`> 0` 时另有一条 `warnings` 汇总告警。**保存期**（`validate_spec(strict_names=True)`，默认）会先拦住**新写的**错名字（422），这个键针对的是库里早先存下的老配置 / 手改过的 YAML。
6. `status`：`exception` 或 `link_failed>0 and linked==0` → `failed`；否则 `success`（有部分失败时 `success` + `warnings`）。
7. 写 `finished_at`；`task_logs` 落库；`trim_history()`。

### 6.8 `app/scanner.py`

```python
def scan_once() -> list[int]      # 遍历 enabled 库，检查 source_dir 下是否有"未记录或体积变化"的一级文件夹，命中则 submit
class Scanner: start()/stop()      # 后台线程，按 scan_interval_seconds 轮询
```
扫描**只创建任务**，不做任何文件操作。

### 6.9 `app/qbittorrent.py`

```python
class QBClient:
    def __init__(self, base_url, username, password, timeout=10)
    def login(self) -> None
    def torrents(self, **filters) -> list[dict]
    def app_version(self) -> str
def test_connection(base_url, username, password) -> tuple[bool,str]
def test_saved() -> tuple[bool,str]    # 用已保存的设置测连通性（scanner 的周期刷新用它）
def latency_of(base_url, username, password) -> tuple[bool,str,int]
```

**只用于状态展示与连通性测试**：触发任务一律走 qB 的完成回调（§8），本模块**不**参与。
`Scanner._refresh_qbittorrent()` 每 5 个 tick 调一次 `test_saved()`，把结果写进
`qbittorrent_last_status` / `qbittorrent_version`（仪表盘的状态灯读这两个键）。
⚠️ 刷新**必须**用模块级函数：`QBClient` 上没有 `test_connection` 方法，写成
`client.test_connection()` 会被宽 `except` 吞掉 ⇒ 状态灯永远不亮
（回归测试 `tests/test_scanner_qbittorrent.py`）。

### 6.10 `app/todos.py`

**全局唯一一份**的「待办看板」台账：任务跑完时把 `result` 里"值得你知道、但不该打断任务"的情况
记成待办，首页仪表盘展示，用户打钩即可。**不弹窗、不阻断、不改任何既有行为**（它只读 `result` 与
`tasks` 行，写只写 `todos` 表）。

```python
class TodoKind(TypedDict):      # title / severity / description / source_keys
    ...
KNOWN_KINDS: dict[str, TodoKind]   # 五种，见下表
TODO_STATUSES = ("open", "done")
TODO_SEVERITIES = ("info", "warning", "error")
# 没有 MAX_EVENTS_PER_KIND 这个上限 —— 一次扫描产生的明细**全部**落库，
#          前端看板按 20 条一页翻，列表接口用 limit / offset。
MAX_DETAIL_CHARS = 4000         # 单条 detail 的字符上限（超出截断并注明）
DETECT_MODES: dict[str, str]    # 「一键检测」：kind → 判定模式（"present" = 目标文件已存在；
                                #   "missing" = 记录里的最终文件已不在那个路径上）

def normalize_kinds(raw: str | None) -> list[str]          # 分隔符 [,，、;；\s]+，未知值丢弃
def enabled_kinds(raw: str | None = None) -> list[str]     # 读设置 todo_kinds；空 / 全非法 ⇒ 全部
def record_task_events(*, result: dict, library_id: int | None, library_name: str | None,
                       task_id: int | None, task_kind: str = "run",
                       status: str | None = None, error: str | None = None) -> list[int]
def get_todo(todo_id: int) -> dict | None
def list_todos(*, status="open", kind=None, library_id=None, query=None,
               limit=50, offset=0) -> dict   # query = "按作品朴素子串"筛选（三列 OR）
def todo_summary() -> dict
def resolve_todo(todo_id: int) -> dict | None
def reopen_todo(todo_id: int) -> dict | None
def detect_resolved(*, kind=None, library_id=None, limit=200, offset=0,
                    only_ids=None) -> dict   # 「一键检测」：**纯只读**，只列候选、绝不改状态
def resolve_detected(todo_ids) -> dict       # 确认后批量打钩；打钩前**再核对一遍**实况
def todo_dict(row) -> dict
```

**六种 kind 与去重键**（没有 `name_collisions` 这一种：它和 `rename_suffix` 记的是同一次重命名；`unscoped_works` 是第六种）（`dedupe_key` 是"同一条待办"的身份；同一 `(kind, dedupe_key)` 的重复发生
**不新增条目**）：

| kind | severity | 触发条件（从 `result` 读） | `dedupe_key` |
|---|---|---|---|
| `link_failed` | error | `result["link_failed"] > 0` | 明细的 `dst`（退化为 `src`） |
| `rename_suffix` | warning | `result["renamed_with_suffix"] > 0` **或** `result["displaced_targets"] > 0`（**两条产生路径合并成一条待办**：新来的文件自己加后缀 / 目标盘上原有的同名文件让位。`result` 里**总是**有这个键，读取仍逐个容错） | 明细的 `target` |
| `unresolved_episode` | warning | `result["kept_unresolved_episode"] > 0` | 明细路径（字符串列表） |
| `unscoped_works` | warning | `result["unscoped_works"] > 0`（**KI-1**：子目录模式下作品目录里没有可接受的季目录、文件直接躺在作品目录里 ⇒ 重命名阶段整目录跳过。明细每项 `{dir, folder, files, samples}`；`source_keys` 里同时列了 `samples.unscoped_works`） | 明细的 `dir`（退化为 `folder`） |
| `source_integrity` | error | `result["source_integrity"]["ok"] is False`（**只在明确为 False 时**） | 库（`library:{id}`） |
| `task_failed` | error | `status == "failed"` 或 `error` 非空（**必须由调用方显式传 `status=`/`error=`**，因为 `result` 字典里没有这两个键） | 错误文本首行（无错误文本时按库聚合） |

> 📌 **一键检测**：`DETECT_MODES` 只登记**记录里带得出单个文件路径**的种类 ——
> `link_failed`（`present`：记录里的目标文件**已存在** ⇒ 那条链接补上了）、
> `rename_suffix` / `unresolved_episode`（`missing`：记录里的最终文件**已不在那个路径上**）。
> `task_failed` 与 `source_integrity` 描述的是「一次任务 / 整棵树」，**没有单文件路径**可核对 ⇒ 永远只能人工判断，
> **`unscoped_works` 同理**（要判断的是"这一部作品的目录结构对不对"，不是某个文件在不在）——
> 它也**不在** `DETECT_MODES` 里，由 `todo_summary()["undetectable_kinds"]` 如实告知前端（别让人以为"没检测出来 = 没问题"）。
> 路径匹配按记录里那条**相对路径的尾巴**做（样本里的 `dir` 有时只写 `Season 1`、不含作品名）；
> `detect_resolved()` 只返回候选（含人话证据 `detect_evidence`），`resolve_detected()` 才落笔，且**逐条复核**。

> 📌 **分页**：`list_todos()` 的 `limit`/`offset` 就是翻页手段；`limit` 由路由层收敛
> （`clamp_limit`，上限 200），前端看板 20 条一页。**明细没有每类 50 条的上限**。

**去重语义（冻结，前端/文档/测试都按此）**

1. 同一 `(kind, dedupe_key)` 已有 `open` ⇒ **只累加**：`occurrences += 1`、刷新 `last_seen_at` 与
   "最近一次"的上下文（`task_id` / `task_kind` / `detail` / `severity` / `title`），`first_seen_at` **不动**；
2. 该 `(kind, dedupe_key)` 曾 `done` 而**再次发生** ⇒ **新插一条 `open`**，历史那条 `done` 一个字节都不动
   （于是"出现过几次、每次什么时候"天然留在表里）；
3. `resolve` 只改 `status` 并记 `resolved_at`，**绝不删数据**；`reopen` 改回 `open` 并清空 `resolved_at`；
   两者都**幂等**（已是目标状态就不重复写时间戳）；
4. 计数 > 0 但 `samples` 缺失 / 形状不认识 ⇒ **仍记一条**（`dedupe_key` 退化为 `library:{id}` 或
   `no-sample`，detail 里写明计数），绝不静默丢事件；`result` 缺键、类型错、`None`、字符串
   `"3"`、bool 等一律按 0 / 空处理（读取器逐个包 try，单条明细坏掉不影响其它明细）；
5. 列表排序 `未完成在前 → last_seen_at DESC → id DESC`（分页稳定）。

**接线点（`app/executor.py`）**：任务收尾写 `tasks` 表拿到最终 `status` / `error` 之后，调用一次
`record_task_events(result=result, library_id=..., library_name=..., task_id=..., task_kind=...,
status=status, error=error)`。`record_task_events` **自己开事务、整批一个事务**，内部异常只记日志、
**绝不向外抛**（看板记不上不能影响任务结果）；**未接线时看板恒空，任务行为完全不变**。

---

## 7. 前端契约（`web/`，免构建）

- 入口 `web/index.html` 引用 `web/js/app.js`（`type="module"`）。
- 路由：hash 路由 `#/dashboard` `#/libraries` `#/tasks` `#/tasks/{id}` `#/works` `#/works/{id}`（可带 hash query `?q=&page=&limit=`）`#/settings` `#/login`。
- 状态：`apiFetch(path, opts)` 统一处理 401 → 跳登录、错误 toast。
- 必须支持：深色模式（`prefers-color-scheme` + 手动切换存 localStorage）、中文文案 + `web/js/i18n.js` 可扩展、响应式（≥1280 / 768–1279 / <768）、空状态、骨架/加载态、错误提示、表单校验、删除二次确认弹窗。
- 媒体库卡片的运行弹窗**不渲染**"整库强制执行"开关（`i18n` 键 `lib.run.force` 保留但不再使用，新增 `lib.run.force_note` 说明替代路径）—— 后端对整库 `force` 直接 400；卡片新增「作品记录」入口（多选，逐行显示体积 / 目标是否还在 / 是否已记录重命名 / 记录时间 / 目标路径，底部「立即重命名」「删除记录」；**不再开弹窗，改为跳转独立页面 `#/works/<id>`**，见下条）；删除 = `/works/delete` 的 `dry_run` 预演 → 二次确认（逐作品目标路径 / 文件数 / 总字节 / **非硬链接文件数 `loose_files`**，`loose_files>0` 显著警告）→ **只需一次弹窗确认**（点「确定删除」即执行；勾选确认框不存在）→ 执行 → 结果。
- 「作品记录」是**独立页面** —— `web/js/pages/works.js`，hash 路由 `#/works`（全部库）与 `#/works/<library_id>`，hash query `?q=&page=&limit=`（**可前进/后退**），每页大小 **25 / 50 / 100 / 200**（**默认 50**）；搜索走**服务端**（`web/js/api.js` 的 `listLibraryWorks(id, {q, limit, offset})`）并**防抖 400 ms**，**勾选跨页保留**；删除流程不变（`/works/delete` 的 `dry_run` 预演 → **显式确认** → 确认）外加「立即重命名」（`/works/rename`）。`web/js/app.js` 侧边栏新增 `nav.works`「作品记录」并注册两条路由（`withLayout('app', 'works', 'nav.works', pageWorks.render)`）；`web/js/pages/libraries.js` 的每库「作品记录」按钮不再开对话框，改为 `router.navigate('#/works/<id>')`；`web/js/i18n.js` 补 `works.*` / `nav.works`（**zh 与 en**，**英文模式下侧边栏不会半截中文**）。分页/搜索参数与 `total` / `ledger_total` 语义见 §5「媒体库」。
- **不得引入任何 CDN 或需构建的工具链**。
- 任务日志用 `GET /api/tasks/{id}/logs?after_id=` 轮询（1.5s），SSE 可选增强。

---

## 8. qBittorrent 回调命令（唯一给定形式）

qBittorrent → 设置 → 下载 → **运行外部程序**：

```
curl -s -X POST "http://192.168.1.100:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" --data-urlencode "name=%N" --data-urlencode "hash=%I" --data-urlencode "category=%L" --data-urlencode "tags=%G" --data-urlencode "content_path=%F" --data-urlencode "save_path=%D" --data-urlencode "root_path=%R"
```

（qbittorrent 容器内已确认存在 `curl`。）

**命令里刻意不传 `kind`**。qBittorrent 的 `%K` 官方语义是 **Torrent ID**（内部数字 ID），
不是"文件类型"；把它当 `kind` 传会被 `EXPLICIT_KINDS` 白名单安全忽略，任务类型由库的
`processor` 推导（`kind_for_processor()`：bangumi/movie → `rename_hardlink`，否则 `hardlink`）。
`kind` 保留为**可选的高级覆盖项**（手工传 `hardlink` / `rename` / `rename_hardlink`），
标准命令里一律不传。多传的其它占位符（`%Z` 大小、`%S` 状态等）会被静默忽略：
`hooks.py` 只从表单里取 `PARAM_NAMES` 里列出的键。**本节是回调命令的唯一真源**，
`README.md` §5.3、`docs/guides/DEPLOYMENT.md`、`docs/guides/USER_GUIDE.md`、`examples/qbittorrent-autorun.md`
与 WebUI 设置页生成的那条命令都必须与本节逐字一致。

---

## 9. 命名与部署约定（全项目统一）

| 项 | 固定值 |
|---|---|
| 镜像名 | `media-ingest:latest`（本地构建，不推送 registry） |
| 容器名 | `media-ingest` |
| Compose 项目目录（示例） | `/vol1/1000/Docker/media-ingest/` |
| 数据卷宿主目录 | `/vol1/1000/Docker/media-ingest/data` |
| 数据卷容器路径 | `/data` |
| 应用根（容器内） | `/srv/mlm`（`WORKDIR` + `PYTHONPATH`） |
| 容器脚本（容器内） | `/usr/local/bin/mlm-entrypoint`、`/usr/local/bin/mlm-healthcheck` |
| 对外端口 | `18081`（部署前请确认宿主上该端口未被占用） |
| 访问地址 | `http://192.168.1.100:18081` |
| 容器内用户 | `app`，默认 `PUID=1000`、`PGID=1001`（宿主上运行容器的用户/组 ID，按你的环境修改） |
| 时区 | `TZ=Asia/Shanghai` |

### 9.1 `docker compose config` 渲染结果（作为文档与验收的对齐基准）

- `restart: unless-stopped`；`logging: json-file, max-size=10m, max-file=3`。
- **端口两侧同源**：`environment.PORT: "${PORT:-18081}"` + `ports: ["${PORT:-18081}:${PORT:-18081}"]`。
  改 `PORT` 会同时移动宿主与容器两侧，保证 entrypoint 打印的端口 / uvicorn 绑定端口 /
  healthcheck 探测端口 / 宿主发布端口**四者一致**。默认渲染为 `18081:18081`。
  ⚠️ 若写成 `ports: "${PORT:-18081}:18081"` 就是缺陷：`.env` 里设 `PORT=18099` 时
  uvicorn 绑 18099、宿主把 18099 映射到容器 18081、healthcheck 探 18099 ⇒ 容器永远 unhealthy。
- **healthcheck 来自镜像 `Dockerfile`，compose 刻意不覆盖**（`docker compose config` 里 `healthcheck: null`）。
  脚本 `/usr/local/bin/mlm-healthcheck` 自己读容器内 `$PORT`。
- volumes（**2 条 bind**，顺序即 compose 中的顺序）：
  1. `${MEDIA_ROOT:-/vol1/1000}` → **同宿主路径**（rw）—— **单一共同父挂载**，下载源与媒体库都在其下 ⇒ 源与目标**同挂载**（硬链接的内核前提）
  2. `/vol1/1000/Docker/media-ingest/data` → `/data`（rw；其下自动派生 `/data/logs` 与 `/data/config`）
  ⚠️ **不要**拆成 `/downloads:ro` + `/library` 两条独立 bind —— 那样会形成两个挂载，`os.link` 一律 `EXDEV`。
  **容器里 `/downloads`、`/library` 都不是应用挂入的路径**：
  老库 DB 里若还留着指向它们的 `allowed_source_roots` / `allowed_target_roots`，会在升级时被**有界一次性改写**（见 `docs/guides/DEPLOYMENT.md` §6.5），
  否则 `GET /api/system/health` 的 `downloads_readable` / `library_writable` 会是 `false`。
- `environment`（全部无前缀）：`PUID=1000`、`PGID=1001`、`TZ=Asia/Shanghai`、`DATA_DIR=/data`、
  `PORT=18081`、`CONFIG_DIR=/data/config`、`MEDIA_ROOT=/vol1/1000`（compose 用它做卷的宿主侧）、
  `DEFAULT_SOURCE_ROOT` / `DEFAULT_TARGET_ROOT`（**只影响首次播种**，对已存在 DB 不生效）、
  `LANG=C.UTF-8`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`。
  ⚠️ **`CONFIG_SYNC_INTERVAL_SECONDS` 不是环境变量**：同步周期的唯一真源是 DB 设置项
  `config_sync_interval_seconds`（区间 0–86400，`0` = 不导入外部改动），在设置页 / `PUT /api/settings` 改。
- `env_file: .env` **只做 compose 插值、不会自动注入容器**；真正进容器的是 `environment:` 段，
  应用代码也**不读** `.env` 文件。
- 运维自验脚本：`sh examples/validate-deployment-assets.sh [仓库根]`（只读 + 临时目录；PASS 数以
  脚本当次输出为准）。

## 10. 写入范围划分

各范围的写入互不重叠，改动只落在自己那一侧：

- **应用后端**：`app/**`
- **前端**：`web/**`
- **部署与运维**：`docker/**`、`docker-compose.yml`、`.env.example`、`examples/**`
- **测试**：`tests/**`
- **文档**：`docs/**`、`README.md`
- **版本记录**：`CHANGELOG.md`
- **仓库忽略规则**：`.gitignore`

**约定**：本文件与 `docs/contracts/BASELINE.md` 是行为契约，**不得擅自修改**；需要变更契约时先提出，改完后同步更新本文件与 `CHANGELOG.md`。
