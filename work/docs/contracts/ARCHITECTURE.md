# 架构与接口契约

> 架构与跨模块契约：技术选型、运行前提、数据模型、模块地图、API 不变式、前端契约。版本 1.0.0；
> 镜像 `media-ingest:latest`，基础镜像 `python:3.11-slim`。
>
> **别处已有的真相，本文只放指针**：处理器规格 → [`PROCESSORS.md`](PROCESSORS.md)；端点/字段 →
> [`API.md`](API.md) §5；可观察行为与 B/L 编号 → [`BASELINE.md`](BASELINE.md)；同一挂载与文件系统矩阵 →
> [`COMPATIBILITY.md`](COMPATIBILITY.md) §2；镜像分层与目录 → [`STRUCTURE.md`](../meta/STRUCTURE.md)；
> 部署/端口/环境变量 → [`DEPLOYMENT.md`](../guides/DEPLOYMENT.md)；测试↔契约映射 →
> [`TEST-MAP.md`](../meta/TEST-MAP.md)。
>
> **改契约的流程**：① 先在 `CHANGELOG.md` 提出；② 改代码与测试；③ 更新本文小节与 `BASELINE.md` 的编号
> 条目；④ 同步 `API.md` / `PROCESSORS.md`。**章节号不重排** —— 代码按章节号反向引用本文
> （`app/config.py` 的 `Settings` docstring 直接写"属性即 ARCHITECTURE §6.1 列出的那批"）。
>
> **路径写法**：`<MEDIA_ROOT>` = compose 的 `${MEDIA_ROOT:-/vol1/1000}`（容器路径 = 宿主路径）；
> `<LIBRARY_ROOT>` = 媒体库 `target_dir`；`<NAS_IP>` = 部署机地址。均为可替换占位。

---

## 1. 技术选型

| 决策 | 选择 | 理由 |
|---|---|---|
| 后端 | Python 3.11（容器内固定） | stdlib 足够；硬链接与重命名都在应用进程内完成 |
| Web | FastAPI + Uvicorn | 自动校验；依赖少、镜像小 |
| 前端 | **免构建**原生 ES modules + CSS | 无 node/npm；无供应链 |
| 存储 | SQLite（WAL） | 单文件、随数据卷持久化、无额外容器 |
| 口令 / 会话 | `hashlib.scrypt`（`scrypt$N$r$p$salt$hash`，N=2^14,r=8,p=1）+ HMAC 签名 Cookie（`HttpOnly`、`SameSite=Lax`） | 全 stdlib，无原生依赖 |
| 并发 | 单进程 + 单工作线程串行队列 | [`BASELINE.md`](BASELINE.md) B11 |
| **运行依赖** | 只有 `fastapi==0.115.6`、`uvicorn[standard]==0.32.1`、**`PyYAML==6.0.3`**（`docker/Dockerfile`） | 其余全 stdlib。**PyYAML 是运行依赖**（配置是 YAML），不是测试依赖 |
| 架构 | `linux/amd64`、`linux/arm64` | 基座镜像 multi-arch |

> ⚠️ **镜像内没有 pytest，也不含 `tests/`**（只 COPY `app/` 与 `web/`）。容器内验收用
> `sh scripts/pytest-in-container.sh` —— 它把测试目录 `docker cp` 进一次性容器、临时装 pytest，跑完即删。

---

## 2. 容器路径布局（**关键设计决策**）

**只挂一条「单一共同父挂载（容器路径 = 宿主路径）」—— 源与目标都在这条挂载里**，因此 qBittorrent
回调传来的 `%F` 零转换直接可用。

| 宿主路径 | 容器路径 | 模式 | 说明 |
|---|---|---|---|
| `${MEDIA_ROOT:-/vol1/1000}` | **同宿主路径** | **rw** | 下载源与媒体库都在其下 ⇒ 源与目标**同挂载**，硬链接的**内核前提**（论证只在 [`COMPATIBILITY.md`](COMPATIBILITY.md) §2） |
| `<部署目录>/data` | `/data` | rw | SQLite `mlm.db`、`secret.key`、`config/`、`logs/`（每库一条重命名日志） |

> **只有这两条 bind**。拆成 `/downloads:ro` + `/library` 两条独立 bind 会形成两个挂载 ⇒ 硬链接必然
> `EXDEV`。qBittorrent 容器自己的路径映射与本应用无关，只要 `%F` 恰好等于宿主路径即可。
> **容器自包含**：硬链接由 `app/processors/hardlink.py`（冻结黄金值见 `tests/test_hardlink_native.py`）、
> 重命名由 `app/processors/engine.py` 执行，运行期不载入任何外部模块或外部程序。

### 同一挂载校验（实证探针取代 `st_dev`）

`same_filesystem` 是**三态** `bool | None`，判据分三级（`app/paths.py`：`probe_cross_filesystem()` /
`check_same_mount()` / `mount_id_for()` / `check_same_filesystem()`）：

| 级别 | 手段 | 说明 |
|---|---|---|
| **主判据** | **实证 `link()` 探针** | 从源根有界递归取一普通文件 → `os.link` 到目标根临时名 → `unlink`；**源目录零写入**、不跟随符号链接 |
| **预筛** | `/proc/self/mountinfo` 的 mount ID | **必要不充分**：相同不代表能建 |
| **降级** | `st_dev` | 仅无 `/proc` 时使用，并标注为推断 |

`null` = 源树下找不到可试链的普通文件 ⇒ **无法实证、不猜**。跨挂载时**运行前只警告不硬拦**（进
`Task.result.warnings[]`，中文带可执行修法），最终结论由执行前后逐文件核对给出（失败即 `link_failed`，
**绝不复制兜底**，B8）。建库与跑任务都不被它硬拦；**只有** `POST /api/libraries/{id}/validate` 会在
`same_filesystem is false` 时进 `errors[]` 并 `ok=false`。

> 📌 源目录只读是**"可验证"而非"结构性"**保证（`:ro` 与跨路径硬链接在内核层面互斥）：
> ①代码层永不写源目录（`tests/test_no_source_mutation.py`）②任务前后**逐文件源完整性核对**
> （`Task.result.source_integrity = {ok, changed[]}` + `warnings[]`）。见 `BASELINE.md` B5。

### 后续约定

- 容器路径 = 宿主路径 ⇒ 日志里的路径与传给建链实现的路径两侧一致，"体积一致则跳过"与孤儿清理按原样
  工作。`HOST_PATH_MAP` **纯展示**（键必须是绝对容器路径，默认空表 = 恒等映射）。
- 硬链接账本 `hard_links.log` **继续写、不再当判据**（判据是 §3 的 `link_records`，B10）。
- `hardlink.py` 用 `logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format=LOG_FORMAT)`
  **未指定 encoding** ⇒ 跟随 locale；日志含中文，故容器**必须**有 `LANG=C.UTF-8` 与 `PYTHONUTF8=1`，否则
  `UnicodeDecodeError`（B9）。
- 日志正文按 ` - ` 切片后必须恰好 3 段 ⇒ 文件夹名含 `" - "` 的条目**整行被忽略**（L2，不修；后果见
  [`COMPATIBILITY.md`](COMPATIBILITY.md) §3.1）。

---

## 3. 数据模型（SQLite，**10 张表**）

全部由 `app/db.py` 的 `SCHEMA` 建出；`libraries` / `todos` / `link_records` 的列是常量
（`_LIBRARIES_COLUMNS` / `_TODOS_COLUMNS` / `_LINK_RECORDS_COLUMNS`），老库由 `migrate()` 补建
（`_ensure_todos_table()`、`_ensure_link_records_table()`，**与 `SCHEMA` 同一份 DDL**）。

| 表 | 用途 | 关键约束 |
|---|---|---|
| `users` | 管理员账号 | `username UNIQUE`；`password_hash` 为 scrypt 串 |
| `processors` | 处理器注册表（规格 + 元数据） | `builtin` 标记；`log_file` 服务端分配（可空） |
| `libraries` | 媒体库规则 | `name UNIQUE`；`processor` 指向注册表 id；`permission_mode` 默认 0 |
| `tasks` | 任务与结果 | `status ∈ {queued,running,success,failed,skipped,cancelled}`；`result` 为 JSON 文本 |
| `task_logs` | 结构化日志 | `ON DELETE CASCADE`；索引 `(task_id, id)` |
| `settings` | 键值设置 | `key PRIMARY KEY` |
| `config_files` | 配置镜像同步状态 | 与 `/data/config/**` 一一对应 |
| `config_events` | 双向同步事件 | 供 `GET /api/system/info` 的 `config_sync{}` |
| `todos` | 待办看板（全局一份） | `(kind, dedupe_key)` + `status`；`library_id`/`task_id` **故意不是外键** |
| `link_records` | **每库「已执行作品」账本** = 建链跳过判据 | 唯一索引 `(library_id, folder)` |

### `link_records`（`app/link_records.py`）—— 建链阶段的跳过判据

```sql
CREATE TABLE link_records (
  id INTEGER PRIMARY KEY,
  library_id INTEGER NOT NULL,          -- 故意不是外键：库删除时显式 drop_by_library()
  folder TEXT NOT NULL,                 -- 作品名 = 建链阶段的一级文件夹名
  source_dir TEXT NOT NULL DEFAULT '',  -- 参与判据：换源或换目标，旧记录一律不算数
  target_dir TEXT NOT NULL DEFAULT '',
  target_path TEXT NOT NULL DEFAULT '', -- 当时落点，供 WebUI 展示与删除时核对
  size INTEGER NOT NULL DEFAULT 0,      -- 与日志里 Folder size 同源
  linked INTEGER NOT NULL DEFAULT 0,    -- 本次实际新建的链接数（0 = 写了记录但没建链）
  first_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX idx_link_records_library_folder ON link_records(library_id, folder);
```

- **判据维度**：`(library_id, folder)` + `source_dir` + `target_dir` + `size` **四项全等**才跳过。
  `hard_links.log` 里的键没有库也没有目标 ⇒ ①两库指向同一 `source_dir` 时 A 库跑完 B 库全被"体积一致，
  跳过"；②同库**换目标目录**照样跳过 —— 这就是判据搬进 DB 的原因（B10）。
- **为何无外键**：库删除时由 `app/routers/libraries.py` 显式调 `link_records.drop_by_library()`，删除动作
  要看得见（同 `todos`）；`DELETE /api/libraries/{id}` 响应里 `link_records_removed` 报告行数。
- **语义**：账本是**作品级**的，**不保证文件级完整性** —— 目标目录里的手动增删改一律保留、不会被自动
  修复。唯一重做途径 = 用 §5 的 `/works*` 接口删掉该作品记录（连带删目标产物），下次运行自动补做。
- **不回填历史**：老库升级后账本为空 ⇒ 第一次运行是全量（后果见 [`COMPATIBILITY.md`](COMPATIBILITY.md)
  §3.5 与 `KNOWN_ISSUES.md` KI-7）。
- **访问器** `search_records(library_id, *, query=None, limit=None, offset=0) -> tuple[list[dict], int]`：
  `query` 对作品名做子串匹配（ASCII 大小写不敏感，`%`/`_`/`\` 按字面量，空/纯空白 = 不过滤），排序
  `folder COLLATE NOCASE` ⇒ 翻页稳定，`limit=None` = 不分页。

### 默认 settings

| key | 默认 | 说明 |
|---|---|---|
| `hook_token` | 随机 | `/api/hooks/*` 的 Bearer token |
| `scan_enabled` / `scan_interval_seconds` | `false` / `900` | 目录扫描补偿 |
| `qbittorrent_url` / `_username` / `_password` | `""` | 仅状态展示与连通性测试（触发一律走回调） |
| `log_level` | `INFO` | 应用日志等级 |
| `orphan_cleanup_enabled` | `true` | 全局孤儿清理总开关 |
| `max_history` | `2000` | 任务历史保留条数 |
| `config_sync_interval_seconds` | `5` | 配置镜像双向同步周期（0–86400，`0` = 只写不导入）。**是 DB 设置项、不是环境变量** |
| `max_task_log_lines` | `5000` | 单任务写入上限，超出记一条 `日志已截断` WARNING |
| `rename_extensions` | `[]` | 全局后缀白名单（库覆盖 → 本设置 → 处理器清单） |
| `extras_patterns` | 出厂 **7 条** | 散装特典判据（正则数组，`[]` = 关闭）；`PUT` 时逐条 `re.compile`，一条编不过整体 `400` |
| `todo_kinds` | `""`（= 全部启用） | 待办种类开关；非法值忽略；**不出现在 `GET /api/settings`**，现值看 `GET /api/todos/summary` 的 `disabled_kinds` |

---

## 4. 处理器选择与校验（自定义库不得绕过既有规则）

处理器规则真源是 [`PROCESSORS.md`](PROCESSORS.md) §3、§7；这里只登记校验面的共同约定。

- `type` 是**自由文本标签**（仅供展示与统计，**不约束**处理器）；库用 **`processor_id`** 指向注册表里
  **存在**的处理器，`processor_ok` 表达"处理器是否还存在"。
- 用户自建处理器**只能**由既有构件组装：`traverse`（接受/拒绝目录）、`extensions`（后缀白名单）、
  `naming`（命名表达式与段序）、`selection`（同后缀多文件取舍）。**没有任何字段能改变**"只在库侧重命名"、
  "绝不移动/删除源文件"、"跨挂载不降级为复制"这三条底线。
- **两种错误形状别混**：422 的 `detail` 是字段级中文列表，元素为
  `{"loc": ["body","<字段>",…], "msg": "<中文>", "type": "value_error.mlm"}`（`app/routers/common.py` 的
  `unprocessable()` / `unprocessable_many()`）—— **字段名在 `loc` 里，没有 `field` 键**；
  `{"field":…, "message":…}` 只出现在 `POST /api/processors/validate` 的 **200** 响应（
  `app/processors/spec.py::iter_errors()`，该端点**不落库**）。
- `source_dir` 与 `target_dir` 都是**容器路径**，不做前缀白名单校验；建库/改库时由
  `paths.check_cross_mounts()` 校验两者**不得相同、不得互相包含**，并要求能建硬链接（同一挂载）。

---

## 5. HTTP API 契约

`/api/*`（除 `auth/*`、`system/health`、`hooks/*`）需会话 Cookie，未认证 401；错误统一
`{"detail": "<中文可读信息>"}`。**端点清单（方法/请求体/响应字段/状态码）的唯一真源是
[`API.md`](API.md) §5**；下面登记**跨端点的形状与不变式**。

### 5.0 通用约定与不变式（后端与前端都按此实现）

**① 分页信封**：`{"items": [...], "total": 137, "limit": 50, "offset": 0}`。`items` 恒为数组；`total`
是**过滤后**匹配总数；非分页集合接口（如 `stats/overview.recent`）仍返回裸数组。

**② 路由挂载**：`StaticFiles(directory="web", html=True)` 把 `web/` 挂在 `/`，`/api/*` 先注册 ⇒ 前端用
**相对路径**引用 `css/app.css`、`js/app.js`，`#/...` hash 路由无需服务端 rewrite。

**③ 响应体字段冻结**：

1. 库列表项 `stats`（列表与详情都带）= `{"last_task_id","last_kind","last_status","last_trigger",
   "last_finished_at","last_error"}`；无任务时**整体为 `null`**（不是全 `null` 的对象）。
2. `GET /api/tasks` 的 `q` 对 `library_name` 与 `error` 两列做**大小写不敏感子串**匹配（OR）。
3. **`Task.result` = 计数 + 不限量样本**：`linked`/`skipped`/`link_failed`/`already_linked`/`renamed`/
   `rename_failed`/`orphans_removed`/`folders_scanned`/`bytes_linked`/`duration_ms` 恒为整数，另有
   `samples{...}`、`samples_truncated`（**恒 `false`**）与 `source_integrity:{ok,changed[]}`。**每个 sample
   数组不设上限**（待办条数 = 样本条数）；逐条明细另走 `GET /api/tasks/{id}/logs`（详情页的渲染上限不
   影响落账条数）。`Task` **顶层**另有 `warnings[]`：「跨挂载」与「源目录被动过」两类。
4. `GET /api/settings` 的 `bool`/`int`/`array` 必须是**真类型**；`hook_token` 明文；
   `qbittorrent_password` 已设置时返回 `"***"`。
5. `GET /api/system/info` 的 `path_map` 是对象 `{"container_to_host":{…},"host_to_container":{…}}`，另有
   `processors{}`、`config_sync{}`、`rename_logs[]`。
6. **所有面向用户的时间字段统一走 `format_time()`**（`app/serializers.py`）：本地时区（`settings.timezone`，
   默认 `Asia/Shanghai`）的 `YYYY-MM-DD HH:MM:SS`，与任务对象同格式 —— **`GET /api/tasks/{id}/logs` 的
   `ts` 与 SSE 载荷都在内，无例外**。需要原始 UTC 时另加 `*_utc` 字段；**不允许同一字段承担两种语义**。
   前端直接显示，不自行加偏移。
7. **超集规则（冻结）**：文档列出的字段是实现的**下界**，可多返回，但多出的必须"只增不改" —— 已列字段的
   名字/类型/语义一个都不许变。已知超集：`DELETE /api/libraries/{id}` 多 `library_id`；
   `/api/tasks/{id}/logs` 的 item 多 `task_id`；`GET /api/auth/me` 多 `authenticated`/`created_at`；
   `GET /api/system/info` 多 `details`/`encoding`/`disk`/`mounts[].read_only`；
   `GET /api/stats/overview` 多 `qbittorrent{status,version}`/`recent_limit`。前端可安全读取，但**不得**当
   契约改动（改了要同步本表）。
8. **`POST /api/libraries/{id}/validate` 是扁平对象、没有 `checks[]`**（21 个字段，形状见 `API.md` §3.8）。
   `?probe=`（默认 `true`）控制是否做实证探针；`same_filesystem` 是 `bool | None`。落点：`false`
   （**已证实**建不出）⇒ `ok=false` + `errors[]`；`null` ⇒ **只**进 `warnings[]`；`true` 但仅据 mount ID /
   `st_dev` 预筛 ⇒ 也进 `warnings[]`。故 **`warnings` 非空 ≠ 有问题**。`POST /api/libraries` /
   `PUT /api/libraries/{id}` **不做**此判据（跨挂载**不阻断建库**）；`/run` 时判据为 `false` 也只写
   `warnings[]` 并继续尝试。
9. **`/api/hooks/*` 的参数只从 URL 查询串或 urlencoded 表单体读取，JSON 请求体被静默忽略**；
   **查询串优先级高于表单体**。用 `-H 'Content-Type: application/json' -d '{…}'` 会得到 `torrent.name`
   为空、命中 0 个库 —— 最难查的静默失败，文档与排障必须点名。
10. **`permission_mode` 是"默认关闭的真实开关"**：`LibraryInput.permission_mode`（默认 `False`）→
    `libraries.permission_mode`（`0/1`）→ 执行器 → `script_adapter.run_hardlinks(...,
    permission_mode=…)` → `create_hard_links(..., permission_mode=…)`。边界与代价见 `BASELINE.md` §1.6
    与 **B13**；WebUI **从关到开**必须二次确认。

### 5.1 LibraryInput

> 处理器由 **`processor_id`** 指定（必须存在于注册表）；`type` 是自由文本标签；`log_file` 缺省/空串/
> `null` = 请服务端生成唯一名（`PROCESSORS.md` §5）。

```json
{
  "name": "番剧", "type": "Bangumi", "processor_id": "bangumi", "enabled": true,
  "source_dir": "<MEDIA_ROOT>/Download/qbittorrent/Bangumi",
  "target_dir": "<LIBRARY_ROOT>/Bangumi",
  "match_categories": ["ban"], "match_tags": [],
  "match_path_prefixes": ["<MEDIA_ROOT>/Download/qbittorrent/Bangumi"],
  "orphan_cleanup": true, "permission_mode": false,
  "log_file": "bangumicache.log"
}
```

响应额外带 `"warnings": []` 与 `"stats": {…}`（§5.0 ③-1）。`permission_mode` **接受并保存**（回读是
`true`/`false`，不是 `null`）。库里必须存**容器路径**，这样 WebUI 展示、任务参数与日志归属才有统一坐标。

---

## 6. 模块契约（`app/`）

模块编号**保持不变**（代码与测试按 `§6.N` 引用）。逐条行为见 `BASELINE.md`，规则语义见 `PROCESSORS.md`。

### 6.1 `app/config.py`

```python
class Settings:                    # pydantic 风格，用 stdlib 读 os.environ
    data_dir: str                  # env DATA_DIR, default "/data"
    db_path: str                   # <data_dir>/mlm.db
    config_dir: str                # env CONFIG_DIR, default "/data/config"
    host_path_map: dict[str,str]   # env HOST_PATH_MAP (JSON) 容器->宿主，**仅用于展示**
    port: int                      # env PORT, default 18081
    secret_key: str                # env SECRET_KEY 或 <data_dir>/secret.key 自动生成
    admin_username: str|None
    admin_password: str|None
    timezone: str                  # env TZ, default "Asia/Shanghai"
    # 派生：log_dir=<data_dir>/logs、hardlink_log_path、library_log_path(log_file)
settings: Settings                 # 单例
```

**`host_path_map` 的不变量（易踩）**：每个键都必须是**以 `/` 开头的绝对容器路径**，默认空表 = 恒等映射。
`_load_host_path_map()` 必须对**所有分支**（raw 为空、JSON 非法、非 dict、退回默认值）统一过一遍"键补
前导斜杠"归一化，`host_to_container_map()` 再做一次防御性归一化；否则 `paths.to_host()` 的展示会静默
失效。`HOST_PATH_MAP` **只用于界面展示**，不参与任何功能判定。

### 6.2 `app/db.py`

- `connect()`（`check_same_thread=False`、WAL、`row_factory`）、`get_conn()`（thread-local）、`init_db()`、`tx()`、`get_setting`/`set_setting`/`all_settings`。表见 §3

### 6.3 `app/security.py`

- `hash_password`/`verify_password`（恒时比较）、`make_session`（`"username.expiry.hmac"`）、`read_session`、`require_user`（未认证 401）、`check_hook_token`

### 6.4 `app/paths.py`

- `to_host`/`to_container`/`normalize`/`is_inside`/`existing_ancestor`/`check_cross_mounts`/`check_same_filesystem`/`probe_cross_filesystem`/`find_sample_file`/`cleanup_probe_leftovers`/`mount_id_for`/`check_same_mount`/`check_hardlink_support`/`snapshot_source_tree`/`verify_source_tree`。语义见 §2

### 6.5 `app/script_adapter.py`

> ⚠️ 运行期执行的是原生实现 `app/processors/hardlink.py`（可观察输出由 `tests/test_hardlink_native.py` 的
> 冻结黄金值锁定）。`ScopedOs`/`LinkOutcome`/`RenameOutcome` 仍有效；细节真源见 `PROCESSORS.md`
> §2.2/§3.3/§8。

```python
class ScopedOs:                     # os 代理：仅在 scoped_path 上过滤 listdir/scandir
    def __init__(self, real_os, scoped_path: str, allowed: list[str])

@dataclass
class LinkOutcome:
    folder: str; created: list[str]; failed: list[tuple[str,str]]; skipped: list[str]
    already_linked: list[str]; orphans_removed: list[str]; logged: list[tuple[str,int]]
    stdout: str; exception: str|None

@dataclass
class RenameOutcome:                # 实际主体在 app/processors/engine.py
    renamed: list[tuple[str,str]]; failed: list[tuple[str,str]]; skipped_folders: list[str]
    kept_unresolved_episode: list[str]; name_collisions: list[dict]
    renamed_with_suffix: list[dict]; displaced_targets: list[dict]
    episode_unresolved: list[str]; extras_classified: list[dict]
    unknown_params: list[dict]; rendered_targets: dict[str,str]

def run_hardlinks(*, source_dir, target_dir, log_file, folder=None, folders=None,
                  force=False, orphan_cleanup=True, skip=None) -> LinkOutcome
def remove_log_entries(log_file, source_dir, folder) -> int
# 重命名**不经过本模块**：执行器直接调 app/processors/engine.py::run_rename(...)
```

必须逐条满足的实现要点：

1. 模块级单例 `mod = hardlink_native`；覆盖 `mod.LOG_FILE = log_file`。
2. **接管 root logger**：确保 root 上有一个指向 `log_file` 的 `FileHandler`
   （`format='%(asctime)s - %(message)s'`、`level=INFO`、`encoding='utf-8'`），同路径则复用，
   **不得**移除其它 handler（原生 `basicConfig` 在 root 已有 handler 时是 no-op，故格式与编码由应用保证）。
3. **「日志隔离」**：该 handler 挂 `_BookkeepingFilter`（`_LOG_BOOKKEEPING_PREFIXES`），让文件**只**收两类
   记账行（`Source: … - Folder: … - Folder size: …` 与 `Deleted orphaned hard link folder: …`），其余记录
   在此 handler 丢弃、**不影响其它 handler**。**只过滤、不清理、不轮转**。回归
   `tests/test_file_log_isolation.py`。
4. `folder` 非空 ⇒ `ScopedOs` 包一层 `mod.os`，`try/finally` 恢复。
5. `orphan_cleanup=False` 必须**不改源码**就抑制孤儿清理：把日志里"源已不存在"的行临时剔除到内存、执行后
   合并回写（用例：源被删 + 开关关闭 ⇒ 目标硬链接目录**仍存在**）。
6. `force=True` 先 `remove_log_entries(...)`（只清**当前作用域**的名字，无"整库"语义入口）。
7. `contextlib.redirect_stdout` 捕获并逐行写进应用日志（`stream="stdout"`）。
8. 解析 stdout 前缀 → `created`/`failed`/`skipped`/`orphans_removed`/`logged`；未匹配行按 DEBUG。
9. **跨挂载前置警告（不硬拦）**：调用前跑三级判据，跨挂载只写 `warnings[]` 后照常调用。
10. 调用后逐文件核对 `(st_dev, st_ino)`；不符者移入 `failed` 并报"非硬链接"。"目标已存在且就是源文件的
    硬链接"由 `_split_already_linked()` 从 `failed` 改判为 `already_linked`。
11. **源完整性核对**：任务前快照、任务后复核 ⇒ `Task.result.source_integrity`；硬链接造成的 `nlink`+1
    与 `ctime` 变化**不算**差异（避免误报），有差异时同时写 `warnings[]`（B5）。
### 6.6 `app/queue.py`

- `TaskSpec{library_id,kind,trigger,scope,payload}`；`TaskQueue.start()/stop(timeout=10)/submit(spec)->int/cancel(task_id)->bool(仅 queued)/running_task_id/queued_count`；单例 `queue`。单工作线程（`daemon=True`）取最早 `queued` 执行，异常必须捕获并写入 task+日志（B11）；`submit` 立即 `INSERT status='queued'`

### 6.7 `app/executor.py`

- `execute_task(task_id)`：读 task/library → `running` → 预检（源可读、目标最近已存在祖先与源同挂载、目标父目录按建链实现的方式创建）→ link 走 `run_hardlinks`、rename 走 `engine.run_rename` → 汇总 `result` → 定 `status` → 写 `finished_at`/`task_logs`/`trim_history()`/`todos.record_task_events()`。`scope.mode="works"` 时用 `link_records.folder_sizes(...)` 造 `skip` 并传 `folders=`，建链后 `upsert` 真正写进日志的作品、`drop` 被孤儿清理的目标；`force` 的作品先从 `skip` 摘掉（否则"强制重跑"是静默空操作）。`status`：有 `exception` 或 `link_failed>0 and linked==0` ⇒ `failed`；否则 `success`（部分失败 = `success`+`warnings`）；`skipped` = 文件夹级跳过 + `already_linked`

### 6.8 `app/scanner.py`

- `scan_once()->list[int]`：遍历 enabled 库，有"未记录或体积变化"的一级文件夹则 `submit`；`Scanner.start()/stop()` 按 `scan_interval_seconds` 轮询。**只创建任务，不做任何文件操作**

### 6.9 `app/qbittorrent.py`

- `QBClient(base_url,username,password,timeout=10)` 的 `login()`/`torrents(**filters)`/`app_version()`；模块级 `test_connection(base_url,username,password)`、`test_saved()`、`latency_of(...)`。**只用于状态展示与连通性测试**，触发一律走回调。⚠️ 刷新**必须**用模块级函数 —— `QBClient` 上没有 `test_connection` 方法，写成 `client.test_connection()` 会被宽 `except` 吞掉 ⇒ 状态灯永远不亮（`tests/test_scanner_qbittorrent.py`）

### 6.10 `app/todos.py`

- 全局唯一一份看板：只读 `result` 与 `tasks`，只写 `todos`，**不弹窗、不阻断、不改既有行为**（B12）。常量 `KNOWN_KINDS`（**六种**）/`TODO_STATUSES`/`TODO_SEVERITIES`/`MAX_DETAIL_CHARS=4000`/`DETECT_MODES`；函数 `normalize_kinds`/`enabled_kinds`/`record_task_events`/`get_todo`/`list_todos`/`todo_summary`/`resolve_todo`/`reopen_todo`/`detect_resolved`/`resolve_detected`/`todo_dict`。六种 kind：`link_failed`(error)、`rename_suffix`(warning，合并"加后缀"与"原有文件让位")、`unresolved_episode`(warning)、`unscoped_works`(warning)、`source_integrity`(error，**只在明确为 `False` 时**)、`task_failed`(error，调用方必须显式传 `status=`/`error=`)。**去重语义（冻结）**：同 `(kind,dedupe_key)` 已有 `open` ⇒ 只累加 `occurrences`、刷新 `last_seen_at` 与最近上下文，`first_seen_at` 不动；曾 `done` 而再次发生 ⇒ **新插一条 `open`**，历史那条一个字节不动；`resolve`/`reopen` 只改 `status`/`resolved_at`，**绝不删数据**且幂等；计数 >0 但样本缺失 ⇒ **仍记一条**（`dedupe_key` 退化），绝不静默丢事件；`result` 缺键/类型错按 0 处理。排序：未完成在前 → `last_seen_at DESC` → `id DESC`。`DETECT_MODES` 只登记带得出单个文件路径的种类（`link_failed`=`present`；`rename_suffix`/`unresolved_episode`=`missing`）；`task_failed`/`source_integrity`/`unscoped_works` **不可检测**，由 `todo_summary()["undetectable_kinds"]` 如实告知前端；`detect_resolved()` 只列候选，`resolve_detected()` 才落笔且逐条复核。接线点：`execute_task` 收尾拿到最终 `status`/`error` 后调一次 `record_task_events(...)`，它**自己开事务、整批一个事务**，内部异常只记日志、**绝不外抛**

---

## 7. 前端契约（`web/`，免构建）

- 入口 `web/index.html` 引用 `web/js/app.js`（`type="module"`）；**不得引入任何 CDN 或需构建的工具链**。
- hash 路由：`#/dashboard` `#/libraries` `#/tasks` `#/tasks/{id}` `#/works` `#/works/{id}`
  （hash query `?q=&page=&limit=`）`#/settings` `#/login`。
- `apiFetch(path, opts)` 统一处理 401 → 跳登录、错误 toast。
- 必须支持：深色模式（`prefers-color-scheme` + 手动切换）、中文文案 + `web/js/i18n.js`（zh/en 都要）、
  响应式（≥1280 / 768–1279 / <768）、空状态、加载态、错误提示、表单校验、删除二次确认。
- 任务日志用 `GET /api/tasks/{id}/logs?after_id=` 轮询（1.5s），SSE 为可选增强。
- 交互细节（作品记录页、碰撞警告、二次确认文案）见 [`USER_GUIDE.md`](../guides/USER_GUIDE.md)。

---

## 8. qBittorrent 回调命令

qBittorrent → 设置 → 下载 → **运行外部程序**：

```
curl -s -X POST "http://<NAS_IP>:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" --data-urlencode "name=%N" --data-urlencode "hash=%I" --data-urlencode "category=%L" --data-urlencode "tags=%G" --data-urlencode "content_path=%F" --data-urlencode "save_path=%D" --data-urlencode "root_path=%R"
```

- **刻意不传 `kind`**：qBittorrent 的 `%K` 语义是 **Torrent ID**（内部数字），不是"文件类型"；当 `kind`
  传会被 `EXPLICIT_KINDS` 白名单忽略。任务类型由库的处理器推导（`kind_for_processor()`）；`kind` 保留为
  **可选高级覆盖项**（`hardlink`/`rename`/`rename_hardlink`）。
- 多传的其它占位符被静默忽略：`hooks.py` 只取 `PARAM_NAMES` 列出的键。
- **本节是回调命令的唯一真源**：`README.md`、`DEPLOYMENT.md`、`USER_GUIDE.md`、
  `examples/qbittorrent-autorun.md` 与 WebUI 设置页生成的命令都必须与本节逐字一致。
- 匹配是 **AND 语义**（每个"已填写"条件都必须满足；三个列表全空 ⇒ 视为不匹配；命中多个库 → 全部入队；
  未命中的库在 `ignored_details[]` 里给中文原因），见 `API.md` §6。

---

## 9. 部署事实（指针）

镜像名、容器名、端口、`PUID`/`PGID`、`TZ`、`restart` 策略、两条 bind 的渲染结果、`env_file: .env` 只做
插值不注入、healthcheck 来自镜像、运维自验脚本 —— **全部以 [`DEPLOYMENT.md`](../guides/DEPLOYMENT.md)
为权威**；镜像分层与数据卷布局见 [`STRUCTURE.md`](../meta/STRUCTURE.md) §3/§4。

本文只留两条**架构级**约束：①端口在 compose 里必须**两侧同源**（`environment.PORT` 与 `ports` 都用
`${PORT:-18081}`），否则 healthcheck 与宿主发布端口会错位；②不要拆成两条独立 bind（见 §2）。

## 10. 写入范围划分（指针）

各范围写入互不重叠（`app/**`、`web/**`、`docker/**`+`examples/**`、`tests/**`、`docs/**`、`CHANGELOG.md`），
**完整划分以 [`STRUCTURE.md`](../meta/STRUCTURE.md) 为权威**。本文与 `BASELINE.md` 是契约文档，改契约走
开头的流程。
