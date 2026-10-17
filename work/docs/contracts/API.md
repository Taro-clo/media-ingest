# HTTP API

> **本文的读者**：要写脚本 / 命令行自动化的用户、要接监控面板的运维、以及维护者。
> 普通用户请读 [`docs/guides/USER_GUIDE.md`](../guides/USER_GUIDE.md)，不需要本文。
> **本文是 HTTP 接口的权威定义**：路径、方法、是否要登录、请求体、响应形状、错误码都以本文为准，并与 `app/routers/*.py` 的实现逐条对应。实现可以多返回字段，但**不会改掉本文已列字段的名字、类型与语义**。
> 处理器规格、规则引擎语义、命名段、预设差异的权威定义在 [`docs/contracts/PROCESSORS.md`](PROCESSORS.md)；架构与数据模型在 [`docs/contracts/ARCHITECTURE.md`](ARCHITECTURE.md)。

---

## 目录

- [1. 通用约定](#1-通用约定)
- [2. 认证接口](#2-认证接口)
- [3. 媒体库接口](#3-媒体库接口)
- [4. 任务接口](#4-任务接口)
- [5. 统计 / 设置 / 系统 / 处理器 / 配置同步](#5-统计--设置--系统--处理器--配置同步)
- [6. 回调接口（qBittorrent 用）](#6-回调接口qbittorrent-用)
- [7. SSE 实时流事件格式](#7-sse-实时流事件格式)
- [8. curl 快速上手](#8-curl-快速上手)

---

## 1. 通用约定

### 1.1 地址与端口

| 项目 | 值 |
|---|---|
| 默认端口 | `18081`（容器内 `PORT` 环境变量可改，见 `app/config.py`） |
| 基础地址 | `http://$NAS_IP:18081` |
| 全部接口前缀 | `/api` |
| 数据格式 | 请求与响应均为 `application/json`（`/api/hooks/*` 例外，见 §6） |

下文示例把 `$NAS_IP` 当作你自己 NAS 的地址（例如 `http://$NAS_IP:18081`），复制命令时请先替换。

### 1.2 认证方式

绝大多数接口都要先登录。登录成功后服务端下发一个**签名 Cookie**（名称 `mlm_session`，`HttpOnly` + `SameSite=Lax`），后续请求带上它即可。没有 API Key / Basic Auth。

| 路径前缀 | 是否需要登录 |
|---|---|
| `/api/auth/me`、`/api/auth/setup`、`/api/auth/login` | ❌ 不需要 |
| `/api/system/health` | ❌ 不需要（给容器健康检查用） |
| `/api/hooks/*` | ❌ 不需要 Cookie，但要 hook token（见 §6） |
| 其他全部 `/api/*` | ✅ 需要登录 Cookie |

未登录或 Cookie 过期 → **HTTP 401**：`{"detail": "未登录或登录已过期，请重新登录。"}`

> 💡 命令行工具（curl / wget）不会自动保存 Cookie，请用 `-c cookies.txt` 保存、`-b cookies.txt` 发送，见 §8。

### 1.3 统一错误格式

所有错误响应体都是一个带 `detail` 的 JSON 对象，`detail` 是**中文可读信息**，可直接展示给用户：

```json
{"detail": "目标目录 /vol1/1000/Library/Bangumi 不在允许的根路径内，当前允许：/vol1/1000/Library。"}
```

| 状态码 | 含义 |
|---|---|
| `200` | 成功 |
| `201` | 创建成功（`POST /api/auth/setup`、`POST /api/libraries`、`POST /api/processors`） |
| `202` | 已受理、异步执行（`POST /api/libraries/{id}/run`、`.../works/rename`、`POST /api/tasks/{id}/retry`） |
| `204` | 成功且无响应体（logout、改密码、取消任务、删除任务历史） |
| `400` | 请求参数不合法（传入值校验失败、缺 `confirm`、路径不在白名单、状态不允许该操作等） |
| `401` | 未登录 / 密码错误 / hook token 错误 |
| `404` | 对象不存在（如库 id、任务 id 不存在） |
| `409` | 状态冲突（实例已初始化、媒体库重名、媒体库已停用、内置处理器不可删、任务当前状态不允许该操作） |
| `422` | **请求体字段级校验失败**：`detail` 是**数组**，元素 `{"loc": ["body", "processor_id"], "msg": "中文说明", "type": "value_error.mlm"}`。出现在 `POST`/`PUT /api/libraries`、`POST`/`PUT /api/processors`（处理器 id 不存在、`log_file` 撞名、规格不合法等）。**没有 `field` 键** |

> 📌 `422` 还有第二种形状：只有 `POST /api/processors/validate` 返回 `200` + `errors` 数组，元素是 `{"field": "naming.segments[0].param", "message": "…"}`（`field` 是点号路径字符串，可为空串）。两种形状的区别见 §3.4 与 §5.7.8。

### 1.4 时间格式

所有时间字段都是**字符串**，格式 `YYYY-MM-DD HH:MM:SS`（容器时区由 `TZ` 决定，默认 `Asia/Shanghai`）；未发生的时刻为 `null`。

`created_at` / `started_at` / `finished_at` / `last_finished_at`、任务日志行的 `ts`、SSE 载荷里的 `ts` **全部**是这种本地格式。

> ⚠️ **唯一的例外**是「文件系统事实 / 调度事实」类诊断字段：`GET /api/system/info` 的 `scan.last_run_at` / `scan.next_run_at` 是 UTC ISO8601（形如 `2025-11-03T03:22:41Z`），刻意不做时区转换 —— 不要拿它们与任务时间直接比大小。

---

## 2. 认证接口

### 2.1 `GET /api/auth/me` — 问服务端"我现在是什么状态"

- **认证**：不需要
- **请求体**：无

**响应 `200`**（还没建管理员，页面应跳「创建管理员」）：

```json
{"authenticated": false, "needs_setup": true, "username": null, "created_at": null}
```

**响应 `200`**（已有管理员且已登录）：

```json
{"authenticated": true, "needs_setup": false, "username": "admin", "created_at": "2026-10-09 01:20:11"}
```

**响应 `200`**（已有管理员但当前未登录）：

```json
{"authenticated": false, "needs_setup": false, "username": null, "created_at": null}
```

| 字段 | 说明 |
|---|---|
| `authenticated` | 当前请求是否带着有效会话 |
| `needs_setup` | 是否**一个用户都还没有**（决定前端跳「创建管理员」还是「登录页」） |
| `username` / `created_at` | 已登录时是当前用户；否则 `null` |

> 这个接口**未登录也返回 `200`**（不是 `401`）：前端首屏必须能问出"该初始化还是该登录"。

### 2.2 `POST /api/auth/setup` — 创建第一个管理员

- **认证**：不需要（**只有数据库里一个用户都没有时才允许调用**）
- **请求体**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `username` | string | ✅ | 用户名，全局唯一，长度 ≥ 2 |
| `password` | string | ✅ | 口令，**长度 ≥ 8** |

```json
{"username": "admin", "password": "MyPassw0rd!"}
```

**响应 `201`**：`{"ok": true, "username": "admin", "message": "初始化完成。"}`，并**同时下发会话 Cookie** —— 初始化完即已登录，不用再调 `login`。

**错误示例**

| 状态码 | 响应体 | 触发条件 |
|---|---|---|
| `409` | `{"detail": "实例已初始化，无法重复执行初始化。如需重置密码请使用修改密码接口。"}` | 已建过管理员 |
| `400` | `{"detail": "用户名至少需要 2 个字符。"}` | 用户名太短 |
| `400` | `{"detail": "密码至少需要 8 个字符。"}` | 口令太短 |

> 也可以跳过这一步：在 `.env` 里设置 `ADMIN_USERNAME` / `ADMIN_PASSWORD`，容器首次启动会自动创建管理员（见 [`docs/guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md)）。**首次登录后请立刻在「设置」页改密码**，并把 `.env` 里的明文口令删掉。

### 2.3 `POST /api/auth/login` — 登录

- **认证**：不需要
- **请求体**：同 `setup`（`username` + `password`）

**响应 `200`**，并带响应头（`Max-Age` 由服务端的会话有效期决定）：

```
Set-Cookie: mlm_session=<签名值>; HttpOnly; SameSite=Lax; Path=/
```

```json
{"ok": true, "username": "admin", "message": "登录成功。"}
```

**错误示例** `401`：`{"detail": "用户名或密码错误。"}`

> 用户名不存在与密码错误返回**同一句文案**（防止靠试探枚举用户名）；登录成功会更新该用户的 `last_login_at`。

### 2.4 `POST /api/auth/logout` — 退出登录

- **认证**：需要
- **请求体**：无
- **响应 `204`**（无响应体），同时清除 `mlm_session` Cookie。

### 2.5 `POST /api/auth/password` — 修改自己的密码

- **认证**：需要
- **请求体**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `old_password` | string | ✅ | 当前口令 |
| `new_password` | string | ✅ | 新口令，**长度 ≥ 8** |

**响应 `204`**（无响应体）。

**错误示例**

| 状态码 | 响应体 |
|---|---|
| `401` | `{"detail": "当前密码不正确。"}` |
| `400` | `{"detail": "新密码至少需要 8 个字符。"}` |
| `400` | `{"detail": "新密码不能与当前密码相同。"}` |

> **改完密码当前会话会立刻失效**：服务端返回 `204` 时会显式清掉 `mlm_session` Cookie，请用新密码重新登录。更换 `SECRET_KEY` 或删除 `data/secret.key` 会让**所有**已有会话立即失效（见 [`docs/guides/USER_GUIDE.md`](../guides/USER_GUIDE.md)）。

---

## 3. 媒体库接口

媒体库 = 一条"把哪个下载目录、按哪套重命名规则、链到哪个媒体库根"的规则。

### 3.1 媒体库对象（响应体形状）

```json
{
  "id": 1,
  "name": "番剧",
  "type": "番剧",
  "processor_id": "bangumi",
  "enabled": true,
  "source_dir": "/vol1/1000/Download/qbittorrent/Bangumi",
  "target_dir": "/vol1/1000/Library/Bangumi",
  "match_categories": ["ban"],
  "match_tags": [],
  "match_path_prefixes": ["/vol1/1000/Download/qbittorrent/Bangumi"],
  "orphan_cleanup": true,
  "permission_mode": false,
  "log_file": "bangumicache.log",
  "rule_override": {},
  "created_at": "2026-10-09 01:20:11",
  "updated_at": "2026-10-09 01:20:11",
  "warnings": [],
  "stats": {
    "last_task_id": 42,
    "last_kind": "hardlink",
    "last_status": "success",
    "last_trigger": "hook",
    "last_finished_at": "2026-10-09 01:22:03",
    "last_error": null
  }
}
```

`rule_override` 是「本库规则覆盖」：没有覆盖时是空对象 `{}`；有覆盖时形如 `{"naming": {"template": "...", "segments": [...]}, "traverse": {...}, "extensions": [...]}`，语义见 §3.2.1 与 [PROCESSORS.md](PROCESSORS.md) 的「库级规则覆盖」一节。

`stats` 是**只读**的最近一次任务摘要，**该库从未跑过任务时整体为 `null`**（不是"每个字段都为 null 的对象"）。列表页可直接用 `stats.last_task_id` 跳到任务详情。

> ⚠️ `source_dir` / `target_dir` 的**存储与返回一律是容器路径**。请求里填宿主路径会被 `app/paths.py` 反查成容器路径后再落库，所以 `POST` / `PUT` 之后回读到的值可能与你提交的不同 —— 这是归一化，不是被改坏。
> 库对象对外**只有 `processor_id`**；`libraries` 表里另有一列 `processor` 作为内部镜像，**不出现在任何 API 响应里**。前端与脚本请一律只用 `processor_id`。
> 旧版本配置里的字段更名（`processor` → `processor_id`、`type` 由枚举变自由标签等）见 [`docs/contracts/COMPATIBILITY.md`](COMPATIBILITY.md) 的「版本兼容性」一节。

### 3.2 字段含义与约束

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | 库名，**全局唯一**；重复 → `409` |
| `type` | string | **自由标签**（不是枚举）。`番剧` / `电影` / `纪录片` / `4K`… 随便填，只用于展示与统计；**不约束处理器** |
| `processor_id` | string | **必须**是 `GET /api/processors` 里存在的处理器 id（内置三个见 §5.7） |
| `enabled` | bool | 关掉后 qBittorrent 回调与扫描都不会再命中它 |
| `source_dir` | string | **容器路径**，必须与 `target_dir` 在**同一个挂载**上（否则 `422`，建不了硬链接） |
| `target_dir` | string | **容器路径**，必须与 `source_dir` 在**同一个挂载**上 |
| `match_categories` | string[] | qBittorrent 分类白名单，空数组 = 不按分类匹配 |
| `match_tags` | string[] | qBittorrent 标签白名单，空数组 = 不按标签匹配 |
| `match_path_prefixes` | string[] | `content_path` 前缀白名单，空数组 = 不按路径匹配 |
| `orphan_cleanup` | bool | 是否允许**删除**「源已不存在的目标硬链接文件夹」。默认 `true` |
| `permission_mode` | bool | **真实开关，默认 `false`**。传 `true` 会把**目标侧**（库根 + 本次处理到的每个一级文件夹的目标子树）改成 `0777`；响应恒为 `true`/`false`。⚠️ 硬链接与源文件共享 inode ⇒ **源文件的权限位会一起变**，所以从关到开需要二次确认。见下面的说明框 |
| `log_file` | string | 该库的**重命名日志文件名**。**只允许文件名**（不含 `/`、`..`）且必须以 `.log` 结尾，落在 `/data/logs`；**省略时按库唯一分配**（先取处理器登记的日志名，被占用时在扩展名前加 `-2`/`-3`…，见 §5.7.9）；**显式传入**却与其它库撞名 ⇒ **`422`** |
| `rule_override` | object \| null | **本库的规则覆盖**，只写想改的项，其余**逐项继承** `processor_id` 指向的处理器。`null` / 省略 / `{}` 都表示"没有覆盖"（见 §3.2.1） |
| `warnings` | string[] | **只读**，服务端返回；提示结构前提不满足（如选了 `bangumi` 处理器但没有 `Season NN` 子目录、目标目录不存在、关闭了孤儿清理等） |
| `stats` | object \| null | **只读**，最近一次任务摘要；从未跑过任务时为 `null` |

> ⚠️ **`permission_mode` 是「默认关闭的显式开关」**：`chmod 777` 只在开启时执行，默认路径一个 `chmod` 都不做。它解决"非 root 服务（如 Emby）读不到库内文件"的实际问题，代价是硬链接与源文件**共享 inode** ⇒ `chmod` 目标侧会同时改掉下载目录里源文件的权限位（内容 / 大小 / inode 号 / mtime 不变，因此不触发源完整性核对）。这正是它默认关闭、并把"改了 N 个目录 / M 个文件"写进任务日志的原因。范围与边界见 [`docs/contracts/BASELINE.md`](BASELINE.md) 的「权限模式」一节。

**`type` 与 `processor_id` 不互相约束**

- 任何媒体库都能选**任何存在的处理器**（内置或自定义）；
- 三个内置处理器语义不变 —— 只是"选择"从"按类型限制"变成"按 id 自由选"；
- 想要新的命名逻辑不需要写代码：新建一个处理器即可（§5.7），媒体库指向它。

**其他硬性校验**（违反一律 `422` 中文错误）:

1. `source_dir` 与 `target_dir` 必须**在同一个挂载**上（`paths.check_cross_mounts()`，跨挂载 ⇒ `422`）；
2. `source_dir` 与 `target_dir` **不得相同**，也**不得互相包含**；
3. `processor_id` 必须存在（不存在 ⇒ `422`）；
4. `log_file` 若提供，必须只是文件名、以 `.log` 结尾，且**不得与其它库已占用的名字撞车**（字段 `log_file`）；
5. `rule_override` 若提供，必须能**独立读懂**（形状 / 正则能否编译 / 模板变量认不认识），而且**合并到处理器规则之后必须仍然合法**（跨层冲突 ⇒ `422`，例如处理器是 `file_scope: title` 而库级写了 `accept_dirs`）。错误字段名带 `rule_override.` 前缀。

> `name` 重复仍返回 `409`、删除要 `?confirm=true` 仍返回 `400` —— 这类"语义冲突"不在 `422` 的范围，见各自接口小节。

#### 3.2.1 `rule_override` —— 本库规则覆盖

只覆盖想改的那几项，其余逐项继承处理器。只允许三个顶层键，**未出现的键一律继承**：

| 键 | 类型 | 覆盖什么 |
|---|---|---|
| `naming.template` | string | **命名模板**。写 `{变量}`，落库时编译成段序列 |
| `naming.segments` | array | 段序列（与 `template` 二选一；同时给出时**模板优先**） |
| `naming.expressions` | object | 自定义表达式，**按名增量合并**（同名覆盖、其余保留） |
| `traverse.accept_dirs` | array | 接受目录正则，元素 `{"pattern": "...", "season_group": N}`；**整体替换** |
| `traverse.reject_dirs` | array | 拒绝目录正则，元素 `{"pattern": "..."}`；**整体替换**（命中即整棵子树跳过） |
| `traverse.file_scope` | string | `subdir`（番剧：进 `Season NN` 子目录找）或 `title`（电影：只看标题文件夹的直接文件） |
| `traverse.skip_season_zero` | bool | 是否跳过 `Season 00` |
| `extensions` | string[] | 本库的后缀白名单（**优先级最高**，压过全局设置与处理器清单） |

- **`traverse.title_depth` 不可覆盖**（当前只支持 `1`：标题文件夹必须是库根下的直接子目录）—— 写了会得到明确的中文错误，而不是被静默忽略。
- 可用变量见 [PROCESSORS.md](PROCESSORS.md) 的「`naming` 的可用名字」一节（12 个命名上下文 + 自定义表达式）。
- 后缀白名单的**三层优先级**：`rule_override.extensions` → 全局设置 `rename_extensions` → 处理器 `extensions`（见 [PROCESSORS.md](PROCESSORS.md) 的「库级规则覆盖」一节）。
- 请求示例：

```json
{
  "name": "番剧（方括号命名）",
  "processor_id": "bangumi",
  "source_dir": "/vol1/1000/Download/qbittorrent/Bangumi",
  "target_dir": "/vol1/1000/Library/Bangumi",
  "rule_override": {
    "naming": {"template": "[{season}-{episode}] {title}{suffix}"},
    "traverse": {"reject_dirs": [{"pattern": "^SP$"}]}
  }
}
```

- `PUT` 的语义：**省略 `rule_override` ⇒ 保留现值**；传 `{}` ⇒ **清空覆盖**（回到完全跟随处理器）；传对象 ⇒ 整体替换（不与旧覆盖合并）。

### 3.3 `GET /api/libraries` — 库列表

- **认证**：需要
- **请求体**：无
- **查询参数**：`limit`（默认 `50`，**最大 `200`**，超过按 `200` 处理）、`offset`（默认 `0`）。按 `id` **升序**返回。

**响应 `200`**：**分页信封**（不是裸数组），`items` 里每项是 §3.1 的完整对象：

```json
{
  "items": [
    {
      "id": 1, "name": "番剧", "type": "番剧", "processor_id": "bangumi", "log_file": "bangumicache.log",
      "enabled": true,
      "source_dir": "/vol1/1000/Download/qbittorrent/Bangumi", "target_dir": "/vol1/1000/Library/Bangumi",
      "match_categories": ["ban"], "match_tags": [],
      "match_path_prefixes": ["/vol1/1000/Download/qbittorrent/Bangumi"],
      "orphan_cleanup": true, "permission_mode": false,
      "created_at": "2026-10-09 01:20:11", "updated_at": "2026-10-09 01:20:11",
      "warnings": [],
      "stats": {"last_task_id": 42, "last_kind": "hardlink", "last_status": "success",
                "last_trigger": "hook", "last_finished_at": "2026-10-09 01:22:03", "last_error": null}
    }
  ],
  "total": 3,
  "limit": 50,
  "offset": 0
}
```

| 字段 | 说明 |
|---|---|
| `items` | 本页数据，**恒为数组**（无数据时 `[]`，不是 `null`） |
| `total` | **过滤后**的总条数（不是本页条数），用来算页数 |
| `limit` / `offset` | 服务端实际生效的分页参数（`limit` 会被上限截断） |

> 分页信封是统一约定：`GET /api/libraries` 与 `GET /api/tasks` 都是这个形状，而 `/api/stats/overview.recent` 这类**非分页集合**仍是裸数组。
> 跨挂载（源与目标不在**同一个挂载**，`link()` 必返 `EXDEV`）**不在建库时校验**，而是由「预检」（§3.8）给出结论；跑任务时也不硬拦（只写 `Task.result.warnings[]`）。原理见 [`docs/contracts/COMPATIBILITY.md`](COMPATIBILITY.md) 的「文件系统兼容性」一节。

### 3.4 `POST /api/libraries` — 新建媒体库

- **认证**：需要
- **请求体**：`LibraryInput`

```json
{
  "name": "番剧",
  "type": "番剧",
  "processor_id": "bangumi",
  "enabled": true,
  "source_dir": "/vol1/1000/Download/qbittorrent/Bangumi",
  "target_dir": "/vol1/1000/Library/Bangumi",
  "match_categories": ["ban"],
  "match_tags": [],
  "match_path_prefixes": ["/vol1/1000/Download/qbittorrent/Bangumi"],
  "orphan_cleanup": true,
  "log_file": "bangumicache.log",
  "permission_mode": false,
  "rule_override": {}
}
```

| 字段 | 必填 | 默认值 |
|---|---|---|
| `name` | ✅ | — |
| `type` | ❌ | `""`（自由标签） |
| `processor_id` | ✅ | —（必须是已存在的处理器 id） |
| `enabled` | ❌ | `true` |
| `source_dir` | ✅ | — |
| `target_dir` | ✅ | — |
| `match_categories` | ❌ | `[]` |
| `match_tags` | ❌ | `[]` |
| `match_path_prefixes` | ❌ | `[]` |
| `orphan_cleanup` | ❌ | `true` |
| `log_file` | ❌ | **按库唯一分配**：该处理器登记的日志名（内置 `bangumicache.log` / `moviecache.log` / `hardlink_only.log`），与其它库冲突时加 `-2`/`-3`… |
| `permission_mode` | ❌ | `false`（**默认关闭**；传 `true` 才开启，界面会要求二次确认） |
| `rule_override` | ❌ | `{}`（见 §3.2.1） |

**响应 `201`**：§3.1 的完整对象（**含 `warnings`**，例如选了 `bangumi` 处理器但目标目录里将来没有 `Season NN` 子文件夹时会提示结构前提）。

**错误示例**

`422`（处理器不存在）—— `detail` 是**数组**，元素含 `loc` / `msg` / `type`，**没有 `field` 键**：

```json
{"detail": [{"loc": ["body", "processor_id"], "msg": "处理器「bangumi-x」不存在。可用处理器：bangumi、hardlink_only、movie。", "type": "value_error.mlm"}]}
```

`400`（路径越界）：

```json
{"detail": "源目录 /mnt/other/x 不在允许的根路径内，当前允许：/vol1/1000/Download/qbittorrent。"}
```

`400`（互相包含）：

```json
{"detail": "目标目录 /vol1/1000/Download/qbittorrent/Bangumi/lib 位于源目录 /vol1/1000/Download/qbittorrent/Bangumi 之内，不允许互相包含。"}
```

`400`（名称为空）：`{"detail": "媒体库名称不能为空。"}`
`400`（源与目标相同）：`{"detail": "源目录与目标目录不能相同。"}`
`409`（重名）：`{"detail": "已存在同名媒体库「番剧」，请换一个名称。"}`

> ⚠️ `orphan_cleanup: true` 是**破坏性**能力：源目录里某个一级文件夹被删掉后，下一次跑任务会**递归删除**目标库里对应的硬链接文件夹。它删的是**硬链接**（不占额外空间、不影响下载原文件），但媒体库里那份会消失。不确定就先设 `false`。

### 3.5 `GET /api/libraries/{id}` — 库详情

- **认证**：需要
- **响应 `200`**：§3.1 的完整对象
- **响应 `404`**：`{"detail": "媒体库不存在。"}`

### 3.6 `PUT /api/libraries/{id}` — 更新媒体库（**整体替换**）

- **认证**：需要
- **请求体**：与 `POST` 相同的完整 `LibraryInput`
- **响应 `200`**：更新后的完整对象

> ⚠️ 这是**整体替换**而不是局部合并：没传的字段会回到默认值。请先 `GET` 拿到当前值，改完再整体 `PUT` 回去。

### 3.7 `DELETE /api/libraries/{id}?confirm=true` — 删除媒体库规则

- **认证**：需要
- **查询参数**：`confirm=true`（**必填**）
- **响应 `200`**：

```json
{"deleted": true, "hardlinks_kept": true, "library_id": 1, "link_records_removed": 12}
```

`hardlinks_kept: true` 是固定值，明示这个接口绝不会删除磁盘上任何已创建的硬链接。历史任务记录里这条库的 `library_id` 会被置空（`null`），但 `library_name` 保留下来，所以任务列表不会出现无名任务。

`link_records_removed` = 随本库一起清掉的**作品账本行数**（`link_records` 表里 `library_id` 等于本库的行，见 §3.10）。账本必须跟着库一起删 —— 留着会让「重建同名库」继承旧记录而被静默跳过。**注意**：本库的**重命名 JSON 缓存不动**（缓存是历史记录），所以重命名阶段的账要手工清理。

**错误示例** `400`（没带确认参数）：`{"detail": "删除媒体库需要二次确认，请携带 confirm=true。"}`

> ✅ **只删规则，绝不删除磁盘上已创建的硬链接文件。** 已链到媒体库的文件原样留在那里，Emby 照常能播；规则删掉后这个库不再出现在回调匹配里。

### 3.8 `POST /api/libraries/{id}/validate` — 预检（不跑任务）

- **认证**：需要
- **请求体**：无
- **查询参数**：`probe`（bool，默认 `true`）

`probe=true` 时做一次**实证 link 探针**：在源树里**有界递归**（深度 ≤3、条目 ≤512、不跟随符号链接）找一个**已存在的普通文件**，把它 `os.link` 到**目标根**下的临时名（`.mlm_probe_<pid>_<rand>.link`）再立刻 `unlink`，并还原目标目录的 atime/mtime。**它绝不写源目录**（若在源目录里 `mkstemp`，源为 `:ro` 时必然 `EROFS`、于是把 `ok` 置 `true` 自我降级 —— 那正是"假绿"的根因）。探针给不出结论时（源树里没有可链的普通文件 / 目标不可写 / `os.link` 返回其它 errno）**不猜**：`probe.ok` 记 `null`，`same_filesystem` 落到「挂载 ID →（读不到 `/proc` 时）`st_dev`」的级联判据。传 `?probe=false` 则完全跳过实证（响应里的 `probe` 为 `null`），直接给级联结论。

> 📌 **判据顺序**：
> ① **实证 `link()` 探针是主判据** —— 能建就是能建（`true`），不能建就是不能建（`false`）；
> ② 探针给不出结论时用 `/proc/self/mountinfo` 的 **mount ID 预筛**：不同挂载 ⇒ `false`（可靠负判据）；相同 ⇒ `true` 但**只是预筛通过、未实证**（`note` 会注明「仅据挂载 ID；该判据必要不充分」—— btrfs 同一挂载内跨子卷仍可能 `EXDEV`）；
> ③ 只有在**读不到 `/proc`**（非 Linux）时才降级回 `st_dev` 比较，`note` 标注那是推断、**可能假绿**；
> ④ 全都判不了 ⇒ `null`。**`st_dev` 相等不能预测 `link()` 成功**，不要把它当结论。

> 📌 **`false` 与 `null` 的落点不同（本接口）**：`false` = **已证实**建不出硬链接 ⇒ `ok` 置 `false` 且原因进 `errors[]`；`null` = 判不了 ⇒ 只进 `warnings[]`；`true` 但「未实证」（仅预筛通过）也会在 `warnings[]` 里留一条提醒。
> **跑任务时则不硬拦**：`POST /api/libraries/{id}/run` 的前置判据即使得到 `false` 也只写 `Task.result.warnings[]` 并继续尝试，真正的成败由执行后的逐文件核对给出。

> 📌 **`errors[]` / `warnings[]` 的出现条件**：
> - **`errors[]`（同时 `ok=false`）**：① 源与目标相同或互相包含；② **源目录不存在或不是目录**；③ `same_filesystem is false`（此时**同一条判据文案进 `errors[]`**）；④ `processor_ok is false`（处理器已不存在）。
> - **只 `warnings[]`（`ok` 仍为 `true`）**：① 目标目录当前不存在（跑任务时由建链阶段 `os.makedirs` 建）；② `same_filesystem is null` ⇒ `无法判定源/目标之间能否创建硬链接：` + 判据文案；③ `same_filesystem is true` 且判据文案非空（仅据挂载 ID / `st_dev` 的**未实证**结论）。
> - **`probe`**：只有 `?probe=true`（默认）时才非 `null`，形状 `{ok, note}`，`ok` 与 `same_filesystem` 同为三态 `bool | null`。
> - **建库永不因跨挂载被拦**：`POST /api/libraries` 与 `PUT /api/libraries/{id}` **不做**这个判据。跨挂载只影响预检结论与运行期 `warnings[]`，不阻断建库。

> 📌 **判据文案**（`probe.note` / `errors[]` / `warnings[]` 里的同一族文案）—— 下面 `{}` 是插值，其余照抄；测试与前端可按这些前缀匹配：

| # | 触发条件 | 文案开头 |
|---|---|---|
| 1 | 探针成功 | `已实证：把源树中的 {sample} 硬链接到 {dst_ancestor} 成功（临时链接已删除、目标目录时间戳已还原，源目录未被写入）。` |
| 2 | 探针 `EXDEV` | `实证失败：把 {sample} 硬链接到 {tmp_link} 时 os.link 返回 EXDEV（{strerror}）：源与目标不在同一个文件系统/子卷。本应用不会回退为复制文件；请把两个目录放到同一个宿主文件系统与子卷下。` |
| 3 | 探针返回其它 errno | `无法实证：把 {sample} 硬链接到 {tmp_link} 时 os.link 返回 {strerror}（errno={errno}）；该错误无法与「跨挂载」区分，改用挂载 ID 预筛。` |
| 4 | 源树取不到样本 | `无法实证：源树 {src_ancestor} 在深度 3、条目 512 之内找不到任何普通文件（不跟随符号链接），无法取样建硬链接。` |
| 5 | 目标不可写 | `无法实证：目标目录 {dst_ancestor} 不可写，无法在其下建临时链接。` |
| 6 | 读不到 `/proc`（仅 Windows 等环境） | `无法读取 /proc/self/mountinfo，挂载 ID 判据不可用。` |
| 7 | mount ID 相同 | `源 {src_ancestor} 与目标 {dst_ancestor} 同属挂载 {mount_point}（mount ID {id}）。注意：挂载 ID 相等是**必要不充分**判据（btrfs 同一挂载内跨子卷仍可能 EXDEV）。` |
| 8 | mount ID 不同 | `跨挂载错误：源 {src_ancestor}（挂载点 {src_mount}，mount ID {src_id}）与目标根 {dst_ancestor}（挂载点 {dst_mount}，mount ID {dst_id}）不是同一个挂载，os.link 会返回 EXDEV。本应用不会回退为复制文件。请把两个目录放到同一个宿主挂载下。` |
| 9 | 仅 `st_dev` 判等 | `未实证，仅据 st_dev（源 … 与目标 … 同为 st_dev={dev}，可能假绿：同一设备号但不同挂载子树时 os.link 仍可能 EXDEV）。{挂载 ID 判据文案}` |
| 10 | `st_dev` 不同 | `跨文件系统错误：源 {src_ancestor} (st_dev={src_dev}) 与目标根 {dst_ancestor} (st_dev={dst_dev}) 不在同一文件系统，os.link 会返回 EXDEV。…` |
| 11 | 全都判不了 | `无法判定：{path} 不存在或不可访问，且无法实证、也读不到挂载 ID。` |
| 12 | 路径级硬错 | `目标路径 {dst_ancestor} 不是目录，无法创建硬链接。` / `源路径 {src_ancestor} 不是目录，无法读取。` |

- **响应 `200`**：结构是**扁平**的（不是 `checks` 数组），共 **21 个字段**。**真 Linux 单挂载（推荐形态，实证通过）**：

```json
{
  "ok": true,
  "source_dir": "/vol1/1000/Download/qbittorrent/Bangumi",
  "target_dir": "/vol1/1000/Library/Bangumi",
  "source_exists": true,
  "target_exists": true,
  "same_filesystem": true,
  "probe": {"ok": true, "note": "已实证：把源树中的 /vol1/1000/Download/qbittorrent/Bangumi/Show A/Show A S01E01.mkv 硬链接到 /vol1/1000/Library/Bangumi 成功（临时链接已删除、目标目录时间戳已还原，源目录未被写入）。"},
  "errors": [],
  "warnings": [
    "已实证：把源树中的 /vol1/1000/Download/qbittorrent/Bangumi/Show A/Show A S01E01.mkv 硬链接到 /vol1/1000/Library/Bangumi 成功（临时链接已删除、目标目录时间戳已还原，源目录未被写入）。"
  ],
  "library_id": 1,
  "library_name": "番剧",
  "type": "番剧",
  "processor_id": "bangumi",
  "processor_ok": true,
  "processor_name": "番剧",
  "processor_kind": "rename",
  "log_file": "bangumicache.log",
  "log_path": "/data/logs/bangumicache.log",
  "source_folders": 12,
  "target_dir_exists": true,
  "same_filesystem_paths": {"source_path": "/vol1/1000/Download/qbittorrent/Bangumi", "target_path": "/vol1/1000/Library/Bangumi"}
}
```

**跨挂载（两条独立 bind，实证 `EXDEV`）** —— 只列变化/关键字段，其余同上例：

```json
{
  "ok": false,
  "same_filesystem": false,
  "probe": {"ok": false, "note": "实证失败：把 /vol1/1000/Download/qbittorrent/Bangumi/Show A/Show A S01E01.mkv 硬链接到 /vol1/1000/Library/.mlm_probe_4321_9f3a1c02.link 时 os.link 返回 EXDEV（Invalid cross-device link）：源与目标不在同一个文件系统/子卷。本应用不会回退为复制文件；请把两个目录放到同一个宿主文件系统与子卷下。"},
  "errors": [
    "实证失败：把 /vol1/1000/Download/qbittorrent/Bangumi/Show A/Show A S01E01.mkv 硬链接到 /vol1/1000/Library/.mlm_probe_4321_9f3a1c02.link 时 os.link 返回 EXDEV（Invalid cross-device link）：源与目标不在同一个文件系统/子卷。本应用不会回退为复制文件；请把两个目录放到同一个宿主文件系统与子卷下。"
  ],
  "warnings": ["目标目录 /vol1/1000/Library/Bangumi 目前不存在：运行任务时由建链阶段自动创建。"]
}
```

> 📌 `same_filesystem is false` 时后端把同一条判据文案写进 `errors[]`（**`errors[0]` 就是 `probe.note` 的原文**）并把 `ok` 置 `false`；判不出来（`null`）时 `errors` 为空、只有 `warnings[]`。
> ⚠️ **`warnings[]` 也会装成功的判据文案**：`same_filesystem is true` 且判据文案非空时它同样进 `warnings[]`，所以**「`warnings` 非空」不代表有问题**。仅据挂载 ID / `st_dev` 预筛得到的 `true` 会在文案末尾多出「（未实证，仅据挂载 ID；该判据必要不充分。）」或「未实证，仅据 st_dev（…可能假绿…）」。
> 📌 真 Linux 上探针能读到 `/proc/self/mountinfo`，所以**不会**出现第 6 条文案；它只在 Windows 等没有 `/proc` 的环境里出现。

| 字段 | 说明 |
|---|---|
| `ok` | `errors` 为空时为 `true`；**只看 `errors`，不看 `warnings`** |
| `source_exists` / `target_exists` | 源目录 / 目标目录**本身**是否存在 |
| `same_filesystem` | 源与目标能否建硬链接，**三态**：`true` = 可建（已实证，或仅 mount ID / `st_dev` 预筛通过 —— 后者会在 `warnings[]` 注明「未实证」）；`false` = **明确不行**；**`null` = 判不了**（不等于失败，也不是 `false`） |
| `probe` | 实证探针结果 `{ok, note}`，或 `null`（`?probe=false` 时）。`ok` 是**探针自己的**结论，可能与 `same_filesystem` 不同（探针 `null` 而挂载 ID 判成 `false` 时，`same_filesystem` 是 `false` 而 `probe.ok` 仍是 `null`）。`note` 是逐字中文文案，不是靠 `stat` 推断的话术 |
| `errors` / `warnings` | 中文可读字符串数组；`errors` 非空 = 预检不通过。落点规则见上面的 📌 |
| `source_folders` | 源目录下的一级文件夹数量；源目录不存在时为 `null` |
| `processor_ok` / `processor_name` / `processor_kind` | 处理器是否还存在（`spec is not None`）/ 处理器显示名 / **处理器的 `kind` 取值**（`rename` 或 `hardlink_only`，就是规格里的 `spec["kind"]`，**不是** `builtin`/`custom` —— 是否内置看 `GET /api/processors` 的 `builtin` 字段）。**`processor_ok: false` ⇒ `ok=false`** 且 `errors[]` 里写「处理器 X 不存在（可能已被删除）。请先到「处理器」页重建，或把本库改到其它处理器。」 |
| `log_file` / `log_path` | 该库重命名日志的**文件名**与它落盘的**绝对路径** |
| `same_filesystem_paths` | 实际参与比较的两个已存在路径（排查"为什么说我跨挂载"时看它） |

> **建议新建库后先点一次「预检」**：它能在真正跑任务之前把跨文件系统、路径越界这类问题暴露出来。判定规则很简单：**`errors[]` 非空 = 预检失败**；`warnings[]` 只是提示，不阻塞。

### 3.9 `POST /api/libraries/{id}/run` — 手动跑一次

- **认证**：需要
- **请求体**（**可以整体省略**，省略等价于 `{"scope": {"mode": "full"}, "force": false}`）：

```json
{"scope": {"mode": "full"}, "force": false}
```

或只处理某一个一级文件夹：

```json
{"scope": {"mode": "folder", "folder": "Show A"}, "force": false}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `scope.mode` | `"full"` \| `"folder"` | ✅ | 全量 或 单个一级文件夹 |
| `scope.folder` | string | `mode=folder` 时必填 | 一级文件夹名（**不带路径**） |
| `force` | bool | ❌（默认 `false`） | ⚠️ 强制重跑：先从应用日志副本里删掉该文件夹的「已处理」记录，让建链阶段**不再跳过**它。**只允许配合 `scope.mode="folder"`**；整库 force ⇒ `400` |

- **响应 `202`**：新建的 Task 对象（见 §4.1），其 `status` 为 `queued`
- **响应 `400`**：`scope` 非法 / `folder` 为空 / `folder` 里带路径分隔符（`{"detail": "scope.folder 必须是源目录下的一级文件夹名，不能包含路径分隔符：a/b"}`）/ **`force=true` 却没配 `scope.mode="folder"`**
- **响应 `404`**：库不存在
- **响应 `409`**：该库已被停用（`{"detail": "媒体库「番剧」已停用，请先启用后再运行。"}`）

**错误示例** `400`（整库 force）：

```json
{"detail": "整库强制重跑已停用：force 只允许配合 scope.mode='folder'（单个作品）使用。要重做作品，请到「作品记录」里删除对应作品的记录，下次运行时这些作品会自动补做，其余作品不受影响。"}
```

> 📌 **为什么撤下整库 force**：整库 force 会让**建链阶段**把已经改过名的文件按「原名」再链一份（目标里同一 inode 出现两个目录项），**重命名阶段**接着多出一个 `xxx (2).mkv` 后缀 —— 越点越乱。要重做作品请走两条正途：①「作品记录」里对选中作品点「立即重命名」（只重命名、不建链，见 §3.12）；②「作品记录」里删掉记录（连带删除目标产物，见 §3.11）⇒ 下次运行自动补做，其余作品不受影响。WebUI 的运行弹窗因此不再渲染整库强制开关，固定以 `force=false` 调用本接口。

> `kind` 由库的 `processor_id` 决定，**不能由请求指定**：`bangumi` / `movie` 同时建链与重命名（`rename_hardlink`），`hardlink_only` 只建链（`hardlink`）。所以**本接口**没有「只重命名」这种用法 —— 一次运行按处理器完整执行，处理过的部分幂等跳过。「只重命名」另有专门接口：§3.12。

> ⚠️ 单作品 `force: true` 会让建链阶段对已存在的目标文件再次 `os.link`，结果是**一批无害的 `FileExistsError`**（建链阶段不覆盖、不删除现有文件，见 [`docs/contracts/BASELINE.md`](BASELINE.md)）。它**不是**「补建缺失链接」的工具：它对**源相对路径**建链，而目标里的文件早已被改名，于是同一 inode 在目标里留下**两个目录项**（多出 ` (2)` 后缀），任务照样 `success`（静默）。详见 [`docs/meta/KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) **KI-9**；WebUI 与所有自动化路径都传 `force=false`，只有直接调用本接口才可能踩到。

### 3.10 `GET /api/libraries/{id}/works` — 作品记录（本库的「已执行作品」清单）

- **认证**：需要
- **请求体**：无
- **查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `q` | string | ❌ | 对作品名（目标目录下的**一级文件夹名**）做**子串**匹配。ASCII 大小写不敏感；`%` / `_` / `\` 按**字面**处理（`LIKE … ESCAPE '\'`）；纯空白 = 不过滤 |
| `limit` | int | ❌（默认 `50`） | 每页条数，夹到 `1..200`；**非法 / `0` / 负数 ⇒ 回落 `50`** |
| `offset` | int | ❌（默认 `0`） | 跳过条数，夹到 `>= 0` |

**示例请求**：`GET /api/libraries/1/works?q=Show&limit=50&offset=0`

**响应 `200`**：分页信封 + 本库的两本账摘要：

```json
{
  "items": [
    {
      "folder": "Show A",
      "size": 123456789,
      "linked": 12,
      "source_dir": "/vol1/1000/Download/qbittorrent/Bangumi",
      "target_dir": "/vol1/1000/Library/Bangumi",
      "target_path": "/vol1/1000/Library/Bangumi/Show A",
      "first_at": "2026-10-09 01:22:03",
      "updated_at": "2026-10-09 01:22:03",
      "target_exists": true,
      "rename_recorded": true,
      "target_files": 12,
      "target_bytes": 123456789,
      "target_loose": 0
    }
  ],
  "total": 3,
  "ledger_total": 12,
  "q": "Show",
  "limit": 50,
  "offset": 0,
  "library_id": 1,
  "library_name": "番剧",
  "source_dir": "/vol1/1000/Download/qbittorrent/Bangumi",
  "target_dir": "/vol1/1000/Library/Bangumi",
  "rename_log_path": "/data/logs/bangumicache.log",
  "rename_entries": 12,
  "rename_missing": [],
  "rename_only": []
}
```

| 字段 | 说明 |
|---|---|
| `items` | 本页作品记录，**恒为数组**；按作品名排序（大小写不敏感） |
| `folder` | 作品名 = 目标目录下的**一级文件夹名**（本库账本的唯一键） |
| `size` | 记录时该作品的**总体积**（建链阶段的跳过判据之一） |
| `linked` | 建链阶段**实际写入体积记录的文件个数** |
| `source_dir` / `target_dir` | 写这条记录时的源/目标目录（**换过目录的旧记录不算数**） |
| `target_path` | 作品在目标里的绝对路径 |
| `first_at` / `updated_at` | 首次记录时间 / 最后一次刷新时间 |
| `target_exists` | 目标目录里这个作品文件夹**还在不在**（被手动删过 ⇒ `false`） |
| `rename_recorded` | 本库**重命名阶段**的 JSON 缓存里还有没有这个作品 |
| `target_files` / `target_bytes` / `target_loose` | **现算**的目标文件夹 文件数 / 总字节数 / **非硬链接文件数**（`st_nlink < 2`，与 §3.11 的 `loose_files` 同义）。⚠️ **只对当前页的行现算**（逐行 `os.walk`），是**页内统计、不是全库统计** |
| `total` | **匹配 `q` 的记录条数**（**不是**本库作品总数）。不传 `q` / 传空白时等于本库总数 |
| `ledger_total` | 本库账本的**总条数**（**忽略 `q`**）。要拿库总数就读这个字段 |
| `q` | 回显生效的搜索词（`strip()` 之后；没有过滤时是空串） |
| `limit` / `offset` | 分页信封（回显**夹取后**的值） |
| `rename_log_path` / `rename_entries` | 本库重命名缓存的绝对路径 / 条目数（排查"删了记录没重建"时看它） |
| `rename_missing` / `rename_only` | 两本账的**差集**，只做提示 |

`rename_missing` = 在作品账本里、但重命名缓存里**没有**的作品名；`rename_only` = 只在重命名缓存里、账本里**没有**的名字。**这两个数组只做提示，应用永不据此自动修复** —— 账本刻意是**作品级**的，**不保证文件级完整性**。

> 📌 **服务端搜索与分页**：排序为作品名 `COLLATE NOCASE`，翻页稳定；`q` 只决定哪些行入选，不改变顺序。`total` = 命中 `q` 的条数，`ledger_total` = 本库账本总条数（只把 `total` 当"命中数"用、要总数读 `ledger_total` 即可；迁移路径见 [`docs/contracts/COMPATIBILITY.md`](COMPATIBILITY.md) 的「版本兼容性」）。`target_files` / `target_bytes` / `target_loose` 只对**本页**的行现算；`rename_entries` / `rename_missing` / `rename_only`（以及每行的 `source_dir` / `target_dir` / `target_exists` / `rename_recorded`）按**整本账**计算，**不受 `q` 与翻页影响**。

**错误**：`404`（`{"detail": "媒体库 999 不存在。"}`）。

> 📌 **账本语义（运维必读）**：
> - 跳过判据 = **本库 + 同 `source_dir` + 同 `target_dir` + 同体积**（记录在 `link_records` 表，唯一键 `(library_id, folder)`）。
>   这**不是**全局判据：**不要**用全局 `hard_links.log`（键 = 源目录 + 作品名 + 体积）去判断跳过，否则①两个库指向同一源目录时，A 库跑完后 B 库全被「体积一致，跳过」⇒ 新库一个链接都不建；②同库换目标目录照样跳过。
> - `hard_links.log` **照写**（兼容与审计），但**不再参与跳过判断**。
> - 账本是**作品级**的，**不保证文件级完整性**；用户在目标目录里的**手动增删改一律保留**、不会被自动"修复"。
> - **重做单个作品有两条途径**：① 在本清单里对选中作品点「立即重命名」（只重命名、不建链，§3.12）；② 删掉该作品记录（连带删目标产物，§3.11），下次运行自动补做，其余作品不受影响。
> - ⚠️ **空账本注意**：账本为空时**第一次全量**运行会把每个作品**重新建链一次**；已在目标里改过名的文件会以**原名**再链一份（同一 inode 的第二个目录项），重命名阶段于是多出 `xxx (2).mkv` 后缀。**不丢数据**（同 inode），但会多出重复文件名。建议用新库、或先清空目标目录、或逐作品处理。详见 [`docs/meta/KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) **KI-7**。
> 用户视角的操作说明（页面上怎么点、每一列怎么读）见 [`docs/guides/USER_GUIDE.md`](../guides/USER_GUIDE.md) 的「查看作品记录」一节。

### 3.11 `POST /api/libraries/{id}/works/delete` — 删除作品记录（连带删除目标产物）

- **认证**：需要
- **请求体**：

```json
{"folders": ["Show A", "Show B"], "dry_run": false, "confirm": false}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `folders` | string[] | ✅ | 要删除的作品名（**一级文件夹名**）；空列表 ⇒ `400` |
| `dry_run` | bool | ❌（默认 `false`） | `true` = **只回执会删什么，磁盘一个字节不动**（WebUI 的确认框用它填内容） |
| `confirm` | bool | ❌（默认 `false`） | 非 dry-run 且 `confirm=false` ⇒ **`400`**（防误调用；WebUI 侧只做**一级**弹窗确认） |

**预演 `200`**（`dry_run=true`）：

```json
{
  "dry_run": true,
  "library_id": 1,
  "library_name": "番剧",
  "target_dir": "/vol1/1000/Library/Bangumi",
  "rename_log_path": "/data/logs/bangumicache.log",
  "items": [
    {"folder": "Show A", "target_path": "/vol1/1000/Library/Bangumi/Show A",
     "in_ledger": true, "rename_recorded": true, "exists": true,
     "files": 12, "bytes": 123456789, "loose_files": 0}
  ]
}
```

**执行 `200`**（`dry_run=false, confirm=true`）：

```json
{
  "deleted": true,
  "library_id": 1,
  "library_name": "番剧",
  "ledger_removed": ["Show A"],
  "rename_removed": ["Show A"],
  "items": [
    {"folder": "Show A", "target_path": "/vol1/1000/Library/Bangumi/Show A",
     "in_ledger": true, "rename_recorded": true, "exists": false,
     "files": 0, "bytes": 0, "loose_files": 0,
     "target_removed": true, "error": null}
  ],
  "next_run_note": "这些作品已从账本移除：下次运行本库时会重新建链并按规则重命名，其余作品不受影响。"
}
```

| 字段 | 说明 |
|---|---|
| `dry_run` | `true` 时是预演回执；执行回执里没有这个键（改由 `deleted: true` 表示） |
| `items[].in_ledger` / `rename_recorded` | 该作品在**作品账本** / **重命名缓存**里有没有 |
| `items[].exists` / `files` / `bytes` / `loose_files` | 目标文件夹在不在 / 文件数 / 总字节数 / **非硬链接文件数** |
| `loose_files` | `st_nlink < 2` 的文件数 —— **不是本应用建立的硬链接**（通常是用户自己放进来的东西，符号链接也算）。`> 0` 时 WebUI 会显著警告：**删除会一并删掉它们** |
| `ledger_removed` / `rename_removed` | 实际被清掉的两本账的作品名数组 |
| `items[].target_removed` / `error` | 目标文件夹是否真的删掉 / 失败原因（`null` = 成功） |
| `next_run_note` | 固定提示：下次运行会重新建链并重命名，其余作品不受影响 |

**执行时删三处，缺一不可**：

1. `link_records` 里这些作品的行（**建链阶段的判据**）；
2. 本库**重命名 JSON 缓存**里的同名条目 —— 只删第 1 处的话，下次运行重命名阶段会打印 `Skipping folder: … (already processed at …)`，用户会以为"删了记录也没重建"；
3. 目标目录里的**整个作品文件夹**（`shutil.rmtree`；**符号链接只删链接本身**）。

> ⚠️ **源目录一个字节都不动**：硬链接是**另一条目录项**，删它**不会减少源文件的数据**（同一 inode 还有源目录那个名字指着）。这就是"删作品记录不会删掉你的下载文件"的原因。

**错误示例** `400`（非 dry-run 且没确认）：

```json
{"detail": "删除作品记录会连带删除目标目录里的整个作品文件夹（源目录不受影响），必须显式确认：请先带 dry_run=true 查看将删除的内容，再用 confirm=true 提交。"}
```

`400`（作品名不是一级文件夹名 / 列表为空）：

```json
{"detail": "作品名必须是目标目录下的一级文件夹名，不能包含路径分隔符：a/b"}
```

```json
{"detail": "必须至少指定一个作品（folders 不能为空）。"}
```

> 📌 **作品名校验**：`folder` 里出现 `/`、`\`、`.`、`..` ⇒ `400`；空列表（或全是空白项）⇒ `400`。算出的绝对路径若**等于目标目录本身**或**越出目标目录**也会被拒（`safe_target_path`，只允许目标目录的**一级子目录**）。`404`：库不存在。

### 3.12 `POST /api/libraries/{id}/works/rename` — 对选中作品立即重命名（不建链）

- **认证**：需要
- **请求体**：

```json
{"folders": ["Show A"]}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `folders` | string[] | ✅ | 要立即重命名的作品名（**一级文件夹名**）；校验同 §3.11 |

- **响应 `202`**：新建的 Task 对象（见 §4.1），其 `status` 为 `queued`。任务特征：`kind="rename"`、`trigger="manual"`、`scope={"mode":"works","folders":["Show A"]}`、`payload.force=true`。
- **响应 `400`**：`folders` 校验失败（同 §3.11）；或处理器是 `hardlink_only`；或**目标目录不存在**（见下）
- **响应 `404`**：库不存在
- **响应 `409`**：该库已被停用（`{"detail": "媒体库「番剧」已停用，请先启用后再运行。"}`）

**错误示例** `400`（`hardlink_only` 处理器）：

```json
{"detail": "处理器「纯硬链接」是 hardlink_only，没有任何重命名规则，不能单独执行重命名。"}
```

**错误示例** `400`（目标目录被移走/删掉）：

```json
{"detail": "目标目录 /vol1/1000/Library/Test/t1 不存在：纯重命名任务只整理目标目录里已经存在的文件。请先运行一次本库（会自动创建目标目录并建立硬链接），或检查该库的目标目录是否被移动或删除。"}
```

> 📌 **只重命名、不建链**：本接口**永远**只作用于勾选的作品（`scope.mode="works"`），因此可以安全地强制重跑 —— 它是整库 force 不存在的情况下重做单个作品的**两条途径之一**（另一条是 §3.11 删记录后等下次运行自动补做，见 §3.9）。**建链阶段不参与**。
> ⚠️ **目标目录必须先存在**：本接口不建链、因此也不会像 §3.9 那样自动创建目标根；目标根缺失时它只会把「文件夹不存在」一条条写进结果，看起来像"重命名失败"。所以这种情况直接 `400` 拦掉，并指路"先运行一次本库"。
> ⚠️ `kind="rename"` 的任务**不是** `hardlink` / `rename_hardlink` —— `kind` 由本接口固定为 `rename`。

---

## 4. 任务接口

### 4.1 任务对象（Task）

```json
{
  "id": 42,
  "library_id": 1,
  "library_name": "番剧",
  "kind": "rename_hardlink",
  "trigger": "hook",
  "status": "success",
  "scope": {"mode": "folder", "folder": "Show A"},
  "payload": {"library_name": "番剧", "force": false, "processor_id": "bangumi"},
  "result": {
    "linked": 12, "skipped": 0, "link_failed": 0, "already_linked": 0,
    "renamed": 12, "rename_failed": 0, "orphans_removed": 0,
    "kept_unresolved_episode": 0,
    "name_collisions": 0, "episode_unresolved": 0,
    "renamed_with_suffix": 0, "displaced_targets": 0,
    "extras_classified": 0,
    "unknown_params": 0,
    "unscoped_works": 0,
    "folders_scanned": 1, "bytes_linked": 331020584,
    "duration_ms": 1843,
    "samples": {
      "link_failed": [],
      "orphans_removed": ["/vol1/1000/Library/Bangumi/Old Show"],
      "renamed": [{"from": "x 01.mkv", "to": "Show A S01E01.mkv"}],
      "kept_unresolved_episode": [],
      "name_collisions": [],
      "episode_unresolved": [],
      "renamed_with_suffix": [],
      "displaced_targets": [],
      "extras_classified": [],
      "unknown_params": [],
      "unscoped_works": []
    },
    "samples_truncated": false,
    "source_integrity": {"ok": true, "changed": []},
    "warnings": []
  },
  "error": null,
  "created_at": "2026-10-09 01:20:11",
  "started_at": "2026-10-09 01:20:11",
  "finished_at": "2026-10-09 01:20:13"
}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `kind` | `hardlink` / `rename` / `rename_hardlink` | 建链、重命名、或两者都做。由库的 `processor_id` 决定：`bangumi` / `movie` → `rename_hardlink`，`hardlink_only` → `hardlink`。§3.12 的「立即重命名」产生 `rename`（只跑重命名、不建链） |
| `trigger` | `hook` / `scan` / `manual` / `retry` | qBittorrent 回调 / 扫描补偿 / 网页「立即运行」 / 重试 |
| `status` | `queued` / `running` / `success` / `failed` / `skipped` / `cancelled` | 见本节末尾 |
| `scope` | `{"mode":"full","folder":null}` / `{"mode":"folder","folder":"Show A"}` / `{"mode":"works","folders":["Show A"]}` | 处理范围（见 §3.9 / §3.12） |
| `payload` | object | 本次运行的原始参数（`library_name` / `force` / `processor`）；重试时原样复用并多一个 `retry_of`。§3.12 产生的任务里 `force=true` |
| `result` | object \| null | **计数 + 限量样本**，见下；未跑完时为 `null` |
| `error` | string \| null | 失败原因（中文），成功时为 `null` |

**`result` 的字段**（只放**计数**和**少量样本**；全量逐条明细请查 `GET /api/tasks/{id}/logs`）

| 字段 | 说明 |
|---|---|
| `linked` | 新建的硬链接数量。**`linked: 0` 也可能是一次成功** —— 目标都已存在，全部幂等跳过 |
| `skipped` | 跳过的数量 = **体积一致而整 folder 跳过的一级文件夹数** + `already_linked` |
| `already_linked` | 目标侧**已经存在、且就是源文件自己的硬链接**（同一 `(st_dev, st_ino)`）的文件数。建链实现照样 `os.link` 失败并打印 `创建硬链接失败: … File exists`，但应用层算**幂等跳过**而不是失败 —— 所以"给库重跑"不再把任务判成 `failed`。同名但**不是**硬链接（inode 不同）的文件仍算 `link_failed`，**绝不覆盖** |
| `link_failed` | 建链失败数量（跨文件系统、权限不足、路径不存在，以及"目标同名但不是硬链接"等） |
| `renamed` / `rename_failed` | 重命名成功 / 失败数量。**加了后缀的改名也计入 `renamed`**（另有 `renamed_with_suffix` 单列）；L2 的「原有文件让位」**不计入** `renamed`，只进 `displaced_targets` 与 INFO 日志 |
| `kept_unresolved_episode` | 命名引用了集数（`episode` / `episode_raw`）但**没解析出集数**（`episode_patterns` 全部未命中**且**没配 `episode_fallback`）的文件数。这些文件**保留原文件名、不执行 `os.rename`**（硬链接阶段已用原名链进目标），并在日志里各打一行 `Kept (no episode): <标题文件夹>/<子目录>/<原文件名>`（**WARNING**）。它们**不算失败**（`status` 仍是 `success`），但 `warnings[]` 里有一条汇总告警。此前的行为是把**字符串** `None` 渲染进文件名（例如 `… S01ENone.mkv`），同一作品的多个压制版本会撞成同一个目标名互相覆盖。注意：**显式配置**了 `episode_fallback`（例如 `preset=legacy` 的字符串 `"None"`）属于"已解析"，照旧把那个字面量渲染进文件名。**这个计数可能比老实例升级前更大**：默认 `episode_patterns` 现在是 9 条带边界约束的有序规则，`Show.E07.mkv`、`Show - 1920x1080.mkv`（以前被错认成第 19 集）这类现在都算"没解析出集数" |
| `name_collisions` | 改名时**目标文件名已被占用**的**事实清单** —— **不再**表示"即将被覆盖"。判定在 `os.rename` **之前**用 `os.path.lexists` 做一次**只读**探测；`renamed` 计数不变。`old_path == new_path`（改到自己，`force` 重跑时大量出现）**不算**冲突。每条样本的 `kind` 有两种：`within_run`（本次运行中**更早**的某个文件已占用同一目标名 —— 从前这会**静默顶掉**先前那次改名，那正是真实丢文件的那一类；现在由 L2 加后缀化解）与 `existing_target`（目标路径**本来就存在**）。非空时 `warnings[]` 里有一条可计数汇总告警，说明 L2 怎么化解（加后缀 / 原有文件让位） |
| `renamed_with_suffix` | 改名时**目标名被占用、因而加了后缀保留**的文件数。L2 在 `os.rename` **之前**先为一个作用域里所有候选文件定下最终名：按渲染出的干净名分组、组内按**体积从大到小**（体积相同按目录列出的先后），**体积最大的拿干净名**，其余依次加 ` (2)`、` (3)`…（模板 `" ({index})"`，上限 **999**）；目标路径**本来就存在**时，若原有文件**比新来的小** ⇒ 原有文件改名让位（不删除，计入 `displaced_targets`）、新来的拿干净名，否则新来的自己加后缀。找不到空位 ⇒ 记一条 `error` 并**保留原文件名**（**绝不覆盖**）。**幂等**：候选后缀路径已经是**本文件自己**（同一 inode）时照用，不会越叠越多。日志每条 **WARNING** `冲突加后缀保留：…`（引擎 stdout 是 `Renamed (suffix): <源文件名> -> <最终文件名>`，执行器映射成 **INFO**；没加后缀时是 `Renamed: …`，同样 **INFO**），收尾摘要新增 `冲突加后缀 N`。任务 `status` 仍是 `success` |
| `displaced_targets` | 因为**体积更大的**新文件要来拿干净名、而被改名到 `" (N)"` 的**原有文件**数量（**让位、不删除、不复制数据**，只改目录项 —— 源侧下载文件不受影响）。日志每条 **INFO** `原有文件让位：…`，收尾摘要 `原有文件让位 M`。明细见 `samples.displaced_targets` |
| `extras_classified` | 命中**全局**设置项 `extras_patterns` 的**散装特典文件**数量。作用域 = **当前作用域目录的直接文件**（子目录模式是 `作品名/Season N/` 的直接文件；`file_scope: title` 是作品目录的直接文件），判据 = 对**含扩展名的文件名**做**大小写不敏感**的 `re.search`；**嵌套子目录里的文件一律不动**。命中者**退出改名流程** —— 不参与正片争名、**不计入** `episode_unresolved`、**不改名也不移动**（保留原名留在原位置，目标侧那条硬链接保持原样），**不会创建任何新目录**。日志：每个命中文件一行 **INFO**「特典保留原名（判据命中，未改名、未移动）：…」，另有一条 **INFO**「特典保留原名：…」；收尾摘要 `特典保留原名 N，`。`> 0` 时 `warnings[]` 里另有一条汇总告警。任务 `status` 仍是 `success` |
| `episode_unresolved` | **L4「可见性」**：**所有**没解析出集数的文件数（`episode_patterns` 全部未命中**且**没配 `episode_fallback`），**不论它是否因此保留了原名**。它是 `kept_unresolved_episode` 的**超集**：命名模板引用了集数时两者重合；命名模板**不**引用集数（例如 movie 的「标题.后缀」）时只有它非空 —— 这种文件照旧按模板改名了，但"解析失败"不再静默。当它大于 `kept_unresolved_episode` 时，`warnings[]` 里另有一条汇总告警说明差了多少个、为什么 |
| `unknown_params` | **KI-11**：命名段序列里用了**上下文没有的变量名**（例如 `naming.segments` 写了 `{"param": "no_such_name"}`，或库级覆盖的段引用了不存在的名字）的文件数。那些段**什么都不输出** ⇒ 该文件**文件名少了一段**，但改名**照样成功**。日志：引擎 stdout 一行 `Unknown param: <作品>/<子目录>/<文件名> (<变量名>)`（执行器映射成 **WARNING**「命名里的未知变量被留空（文件名少了一段）：…」），收尾摘要 `命名未知变量 N，`，`warnings[]` 里另有一条汇总告警（前 10 个文件 + 改法）。**保存期就会拦住新写的错名字**（见 §5.7.8 的 422），这条计数针对的是"库里早先存下的老配置 / 手改过的 YAML / 直接改数据库" |
| `unscoped_works` | **KI-1**：子目录模式下「作品目录里没有可接受的季目录、文件直接躺在作品目录里」而被**整部跳过改名**的作品数。这一路既计数也进待办看板，明细形状见 `samples.unscoped_works`。**纯建链 / `hardlink_only` 任务不进这段代码**，所以它们的该键恒为 `0` |
| `orphans_removed` | 删除的孤儿硬链接**文件夹**数量 |
| `folders_scanned` | 本次扫描过的一级文件夹数量 |
| `bytes_linked` | 本次新建硬链接的字节总量（硬链接不额外占用空间） |
| `duration_ms` | 本次执行耗时（毫秒，整数） |
| `samples` | **十一个**数组（形状见下表）。**每个数组不设上限**：待办条数 = 样本条数，一次扫描里有多少个文件出问题就列多少条 |
| `samples_truncated` | **保留字段**。样本不再截断，恒为 `false`；若日后把 `SAMPLE_LIMIT` 改回数字，它恢复原义（`true` = 样本被截断、真实数量看计数） |
| `warnings` | **非致命**的中文告警字符串数组（例如「3 个文件未能建立硬链接（详见日志）。」）。**还会出现五类**：① 运行前判定**跨挂载**（指名修法）；② **源目录在任务前后被动过**（同时写 `source_integrity`）；③ **L2 争名**（`name_collisions > 0` 时的汇总告警）；④ **实际加了后缀**（`renamed_with_suffix > 0` 时的汇总告警，列出前 10 条 `源名 → 目标名`）；⑤ **L3 特典排除**（`extras_classified > 0` 时的汇总告警，列出前 10 条 `文件名（命中的判据）`）。每条都会同时以 `警告：…` 打进任务日志（**WARNING**）。有它不代表任务失败 —— 失败看 `status` / `error` |
| `source_integrity` | `{ok: bool, changed: string[]}`，**每个任务结果都带这个键**。任务**开始前**对源根做一次快照（`相对路径` / `size` / `ino` / `mtime_ns`），**结束后复核**：正常是 `{"ok": true, "changed": []}`；只有真的发现源目录被动过才是 `{"ok": false, "changed": ["相对路径", …]}`，并同时写一条 `warnings[]`。**任务的 `status` / `error` 语义不因它改变** —— 它只是一条独立的事实记录（硬链接不触发误报：`nlink` / `ctime` 的变化不算，因为建硬链接会让源文件 `nlink+1`、`ctime` 变化，而内容 / `mtime` / `ino` 不变）。这是对"不写源目录"的**可验证保证** |

**`samples` 十一个数组的元素形状**

| 数组 | 元素 |
|---|---|
| `link_failed` | `{src, dst, error}` |
| `orphans_removed` | 路径字符串 |
| `renamed` | `{from, to}` |
| `kept_unresolved_episode` | **相对媒体库根目录**的路径字符串 `标题文件夹/子目录/原文件名` |
| `name_collisions` | `{dir, target, previous, current, kind}`：`dir` = 作用域目录标签 `作品名/Season 1`（title 模式为空串）；`target` = 新的**文件名**；`previous` = 先前占用该目标的**源文件名**（`existing_target` 时是目标上原本的文件名）；`current` = 本次被改名的**源文件名**；`kind` = `within_run` 或 `existing_target` |
| `episode_unresolved` | 与 `kept_unresolved_episode` 同形的路径字符串 |
| `renamed_with_suffix` | `{dir, old, clean, target, size, reason, displaced}`：`old` = **源文件名**；`clean` = 它想要但被占用的干净名；`target` = **最终**带后缀的名字；`size` = 源文件体积；`reason` = `within_run`（本次运行里更大的文件拿了干净名）或 `existing_target`（目标盘上原有的同名文件更大）；`displaced` 字段**目前恒为空串**，让位事实看 `displaced_targets` |
| `displaced_targets` | `{dir, old, target, clean, size}`：`old` = 占位者**原来的新名字**（等于 `clean`）；`target` = 它让位后的名字；`size` = **赢家**（新来的那个文件）的体积，**不是**占位者自己的 |
| `extras_classified` | `{dir, name, size, pattern}`：`dir` = **作用域标签**（子目录模式是 `作品名/Season 1`；title 模式是作品名）；`name` = **源文件名**（它没被改名）；`size` = 源文件体积；`pattern` = **命中的那条判据**（便于"发现误判就把判据写精确"） |
| `unknown_params` | `{label, file, params}`：`label` = 作用域标签 `作品名/子目录/源文件名`；`file` = **源文件名**；`params` = 该文件这次渲染里"上下文没有、被留空"的变量名（排序去重） |
| `unscoped_works` | `{dir, folder, files, samples}`：`dir` = **作品目录**的绝对路径；`folder` = 作品目录名；`files` = 直接躺在这个目录里、且没被特典判据排除的文件数；`samples` = 前几个文件名用「、」连接 |

**`status` 含义**

| 状态 | 含义 |
|---|---|
| `queued` | 已入队，还没轮到（队列**串行**，同一时刻只有一个任务在跑） |
| `running` | 正在执行 |
| `success` | 执行完成（**可能带部分失败警告**，看 `result.link_failed` / `rename_failed`）；全部幂等跳过也算 `success` |
| `failed` | 执行失败（抛异常，或所有文件都建链失败） |
| `skipped` | 无需处理（源目录里没有符合条件的一级文件夹，或体积一致全部跳过） |
| `cancelled` | 在排队时被取消 |

### 4.2 `GET /api/tasks` — 任务列表

- **认证**：需要
- **查询参数**：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `status` | string | — | 按状态过滤，取值同 §4.1。**非法值返回 `400`**：`{"detail": "未知的任务状态：xxx。可选值：queued、running、success、failed、skipped、cancelled。"}` |
| `library_id` | int | — | 只看某个库的任务 |
| `q` | string | — | 关键词搜索：对**库名**与**错误信息**做大小写不敏感的子串匹配（命中任一个即保留） |
| `limit` | int | `50` | **最大 `200`**，超了按 `200`；`0` 或非法值按 `50` |
| `offset` | int | `0` | 分页偏移，负数按 `0` |

- **响应 `200`**：**分页信封**（按 `id` 倒序 = 最新在前），结构与 `GET /api/libraries` 一致：

```json
{"items": [ /* Task 对象，见 §4.1 */ ], "total": 137, "limit": 50, "offset": 0}
```

`items` 恒为数组（不是 `{"tasks": [...]}`）；`total` 是**过滤后**匹配的任务总数（不受 `limit` / `offset` 影响）。

`ts` 与 `created_at` / `finished_at` 一样是本地 `YYYY-MM-DD HH:MM:SS`。仍然是 UTC ISO 的只有诊断字段 `scan.last_run_at` / `scan.next_run_at`（见 §1.4）。

### 4.3 `GET /api/tasks/{id}` — 任务详情（含 `result`）

- **认证**：需要
- **响应 `200`**：§4.1 的完整对象
- **响应 `404`**：`{"detail": "任务 42 不存在。"}`

### 4.4 `GET /api/tasks/{id}/logs` — 增量拉取任务日志

前端**轮询**日志用的接口（每 1.5 秒拉一次）。

- **认证**：需要
- **查询参数**：

| 参数 | 默认 | 说明 |
|---|---|---|
| `after_id` | `0` | 只返回 `id > after_id` 的日志行；用上一次响应的最后一条 `id`（或 `last_id`）作为下一次的 `after_id`。按**主键**而不是时间戳增量，不会漏掉同一毫秒内的行 |
| `limit` | `500` | 单次最多返回条数，**最大 `2000`** |

- **响应 `200`**（**对象，不是裸数组**）：

```json
{
  "items": [
    {"id": 101, "task_id": 42, "ts": "2026-10-09 09:20:11", "level": "INFO",  "message": "开始处理：Show A", "stream": null},
    {"id": 102, "task_id": 42, "ts": "2026-10-09 09:20:11", "level": "INFO",  "message": "已创建硬链接: /vol1/1000/Library/Bangumi/Show A/Season 1/Show A S01E01.mkv", "stream": "stdout"},
    {"id": 103, "task_id": 42, "ts": "2026-10-09 09:20:12", "level": "WARNING",  "message": "Deleted orphaned hard link folder: /vol1/1000/Library/Bangumi/Old Show", "stream": "stdout"},
    {"id": 104, "task_id": 42, "ts": "2026-10-09 09:20:13", "level": "INFO",  "message": "Renamed: x 01.mkv -> Show A S01E01.mkv", "stream": "stdout"},
    {"id": 105, "task_id": 42, "ts": "2026-10-09 09:20:13", "level": "INFO",  "message": "Renamed (suffix): x 02.mkv -> Show A S01E01 (2).mkv", "stream": "stdout"},
    {"id": 106, "task_id": 42, "ts": "2026-10-09 09:20:13", "level": "WARNING",  "message": "同名冲突：Show A/Season 1 · 目标名 Show A S01E01.mkv ← 本次源名 x 02.mkv（先前占用者 x 01.mkv；kind=within_run）——L2 防丢策略已生效：不覆盖，冲突文件加「 (2)」「 (3)」… 后缀，一个都不丢；明细见任务样本 samples.renamed_with_suffix。", "stream": null},
    {"id": 107, "task_id": 42, "ts": "2026-10-09 09:20:13", "level": "WARNING",  "message": "冲突加后缀保留：Show A/Season 1 · x 02.mkv → Show A S01E01 (2).mkv（想要的干净名 Show A S01E01.mkv 已归体积更大的文件，体积 331020584 字节；reason=within_run）", "stream": null},
    {"id": 108, "task_id": 42, "ts": "2026-10-09 09:20:14", "level": "INFO",  "message": "特典保留原名（判据命中，未改名、未移动）：Show A/Season 1/NCOP.mkv", "stream": "stdout"},
    {"id": 109, "task_id": 42, "ts": "2026-10-09 09:20:14", "level": "INFO",  "message": "特典保留原名：Show A/Season 1 · NCOP.mkv（104857600 字节；命中判据 NCOP，未改名、未移动）", "stream": null}
  ],
  "last_id": 109,
  "task_id": 42
}
```

| 字段 | 说明 |
|---|---|
| `items` | 日志行数组（**永远是数组**） |
| `items[].task_id` | 所属任务 id（列表项里也有） |
| `items[].level` | `DEBUG` / `INFO` / `WARNING` / `ERROR`（服务端**原样**存库里写的值 —— 是 `WARNING` 而不是 `WARN`）；**另外会出现小写 `warning`**（日志截断提示那条，`app/logbuf.py` 写的就是小写） |
| `items[].stream` | `"stdout"` 表示这一行来自建链阶段（`app/processors/hardlink.py`）的 `print`；`null` 表示应用自己的日志 |
| `items[].ts` | 本地时间 `YYYY-MM-DD HH:MM:SS`（按容器 `TZ`） |
| `last_id` | 本批最后一条的 `id`；下次请求把它当 `after_id`。本批为空时等于请求里的 `after_id` |

> ⚠️ **破坏性操作固定是 `WARNING` 级**：建链阶段删除孤儿硬链接文件夹的行会以 `Deleted orphaned hard link folder: <路径>` 出现。看到它就说明媒体库里**真的**有一个文件夹被递归删除了 —— 删的是硬链接，源下载文件不受影响。

> 📌 **引擎 stdout 前缀 → 日志级别**（`app/executor.py::_log_parsed_line()` 的完整对照；**未匹配的行原样记为 `DEBUG`**，不丢信息）：

| 引擎 stdout 前缀 | 级别 | 备注 |
|---|---|---|
| `创建硬链接失败: ` | `ERROR` | |
| `Error renaming ` | `ERROR` | 改名失败 |
| `Deleted orphaned hard link folder: ` | `WARNING` | 真的删掉了目标库里的一个文件夹（硬链接，源文件不受影响） |
| `Kept (no episode): ` | `WARNING` | 没解析出集数 ⇒ 保留原文件名 |
| `Renamed: ` / `Renamed (suffix): ` / `Displaced: ` | `INFO` | 普通改名 / **L2 防丢**加后缀 / **L2 防丢**原有文件让位 |
| `Extra (kept): ` | `INFO` | **L3 特典排除**：会被改写成中文「特典保留原名（判据命中，未改名、未移动）：{作用域标签}」，每个命中文件一行 |
| `Skipping folder: ` / `已创建硬链接: ` / `文件夹已记录且体积一致，跳过: ` / `已删除旧记录: ` / `已记录文件夹体积: ` / `已删除文件体积记录` | `INFO` | 记账与跳过类 |
| 其它任何未匹配的行 | `DEBUG` | 原样记录 |

### 4.5 `POST /api/tasks/{id}/retry` — 重试

- **认证**：需要
- **请求体**：无
- **行为**：用**同一份 payload**（同样的库、范围、force）**新建**一个任务；原任务记录保留不变。
- **响应 `202`**：新 Task 对象（`trigger` 为 `"retry"`，`payload` 里多一个 `retry_of` 指向原任务）
- **响应 `409`**（当前状态不允许重试）：`{"detail": "只有失败或已取消的任务可以重试，当前状态为 success。如需重新执行该媒体库，请使用「立即运行」。"}`
- **响应 `409`**（原任务的媒体库已被删除）：`{"detail": "原任务的媒体库已被删除，无法重试。请重新创建媒体库后手动运行。"}`

> 成功过的任务**不能**重试：那只会产生一条重复的成功记录、掩盖真正的失败。想再跑一次，请用媒体库页的「立即运行」（可选 `force`）。队列是**串行**的，重试只是入队，不会打断正在跑的任务。

### 4.6 `POST /api/tasks/{id}/cancel` — 取消

- **认证**：需要
- **请求体**：无
- **响应 `204`**：**无响应体**
- **响应 `409`**（任务正在跑）：`{"detail": "任务正在执行中，无法取消。硬链接创建到一半中断会留下不完整的目录，请等待本次执行结束（队列是串行的，取消它也不会让下一个任务更早开始）。"}`
- **响应 `409`**（任务已经不在排队状态）：`{"detail": "只有排队中的任务可以取消，当前状态为 success。"}`

> 只有 `queued` 的任务能取消，成功时返回 `204`（无响应体），不是返回更新后的 Task。**`running` 的任务不能中断** —— 这是刻意的：脚本正在「读-改-写整份日志」、并且可能已经建了一半硬链接，硬杀会留下不完整目录。

### 4.7 `DELETE /api/tasks/{id}` — 删除任务历史

- **认证**：需要
- **响应 `204`**：无响应体
- **响应 `409`**（任务正在跑，不允许删历史）：`{"detail": "任务执行中，无法删除其历史记录。"}`
- **行为**：删除这条任务记录及其日志（`queued` 的会先被取消）。**不影响磁盘上的任何文件。**

---

## 5. 统计 / 设置 / 系统 / 处理器 / 配置同步

### 5.1 `GET /api/stats/overview` — 仪表盘数据

- **认证**：需要
- **查询参数**：`recent_limit`（默认 `8`，最大 `200`）—— `recent` 里返回几条任务
- **响应 `200`**：

```json
{
  "tasks": {"queued": 1, "running": 0, "success_24h": 37, "failed_24h": 2, "total": 412},
  "libraries": {"total": 3, "enabled": 2},
  "recent": [],
  "queue": {"running_task_id": null, "queued_count": 1},
  "qbittorrent": {"status": "ok", "version": "5.0.4"}
}
```

| 字段 | 说明 |
|---|---|
| `tasks.queued` / `tasks.running` | 当前排队 / 正在执行的任务数 |
| `tasks.success_24h` / `tasks.failed_24h` | 最近 24 小时**跑完**（按 `finished_at` 算，不是创建时间）的成功 / 失败数 |
| `tasks.total` | 历史任务总数 |
| `libraries.total` / `libraries.enabled` | 媒体库总数 / 已启用数 |
| `recent` | 最近若干条 Task 对象（**裸数组**，不是分页信封） |
| `queue.running_task_id` / `queue.queued_count` | 正在跑的任务 id（空闲为 `null`）/ 排队中的任务数 |
| `qbittorrent.status` / `version` | 后台扫描线程最近一次探测 qBittorrent 的结果：`status` 取 `"ok"` / `"error"` / `null`（从未探测过）；`version` **只在探测成功时写入**。两者来自设置键 `qbittorrent_last_status` / `qbittorrent_version`（§5.2），刷新节奏是每 5 个 tick（tick 默认 60 秒）一次 |

### 5.2 `GET /api/settings` — 读取全部设置

- **认证**：需要
- **响应 `200`**：**裸对象**（不分页、不套信封），值**已经是真类型** —— `true` 就是布尔、`900` 就是数字、`["/vol1/1000/Download/qbittorrent"]` 就是数组，**不是** JSON 字符串：

```json
{
  "scan_enabled": false,
  "scan_interval_seconds": 900,
  "log_level": "INFO",
  "max_history": 2000,
  "max_task_log_lines": 5000,
  "orphan_cleanup_enabled": true,
  "rename_extensions": [".mkv", ".mp4", ".ass"],
  "extras_patterns": ["NCOP", "NCED", "Preview", "Creditless", "Info", "予告編", "CM集"],
  "config_sync_interval_seconds": 5,
  "qbittorrent_url": "",
  "qbittorrent_username": "",
  "qbittorrent_password": "",
  "hook_token": "3f9c1d2e..."
}
```

键的顺序是**固定**的（就是上面这个顺序，也是设置页表单的顺序），前端不用自己排。

> 📌 这份响应就是**设置表里的可编辑键**（`app/routers/settings.py` 的 `settings_view()` 显式列出的那 **13 个**）。`config_dir` **不是设置项** —— 它只出现在 `GET /api/system/info`（§5.6），不要在 `PUT /api/settings` 里写它。
> 📌 反过来也有一个"表里有、这里没有"的键：`todo_kinds`（见下）**不会**出现在这份响应里（它虽登记在 `STR_KEYS`、可以用 `PUT` 写，但 `settings_view()` 的显式列表里没有它，而"表外键回传"回路又只放行**非** `KNOWN_KEYS` 的键）。当前值请查 `GET /api/todos/summary` 的 `disabled_kinds`（§5.9）。

| 键 | 返回什么 | 为什么 |
|---|---|---|
| `hook_token` | **明文** | 设置页要一键复制带 token 的 curl 回调命令，不给明文这个功能没法用。请勿贴到公开页面或截图里 |
| `qbittorrent_password` | 已设置 → `"***"`；未设置 → `""` | 不回传明文；`"***"` 在 `PUT` 里表示「保持原值」。两者语义不同，别当成同一个 |

**`rename_extensions`**：**允许重命名的文件后缀白名单**，对**所有**媒体库生效。优先级**低于**库级覆盖、**高于**处理器里的 `extensions`（三层：`rule_override.extensions` → 本设置 → 处理器清单）。默认 `[]` = 未设置，此时按各处理器的清单走。详见 [PROCESSORS.md](PROCESSORS.md) 的「库级规则覆盖」一节。

**`extras_patterns`（L3 特典排除）**：**散装特典文件的判据**，一个**正则字符串数组**（一条一项），对**所有**媒体库生效、**没有**库级覆盖。出厂默认 **7 条**：`["NCOP", "NCED", "Preview", "Creditless", "Info", "予告編", "CM集"]`；**空数组 `[]` = 关闭这个功能**。判据是对**含扩展名的文件名**做**大小写不敏感**的 `re.search`（子串命中即可，所以 `NCOP` 也能命中 `Show A NCOP.mkv`），且只作用于**当前作用域目录的直接文件**（嵌套子目录里的文件一律不动）。命中者**退出改名流程**：**不改名、也不移动**，不参与正片争名（不会顶掉正片的干净名）、也不计入 `episode_unresolved`。

> 📌 **注意区分两件事**：命中文件**不会被归入**与 `Season N/` 同级的 `其他/` 子目录（不会创建 `其他/`），它们原地不动。任务结果里的 `extras_classified` 只表示「**被排除在改名之外**」的数量，明细形状见 §4.1。

每条在 `PUT` 时都会先试着 `re.compile`：**只要有一条编不过就整体 `400`**（中文点名第几条 + 模式原文），不会写进库里。命中者如何被剔出改名流程、剔除发生在哪一步，见 [PROCESSORS.md](PROCESSORS.md) 的「规则引擎语义」一节第 7、12 步，以及本文 §4.1 里 `extras_classified` 的说明。

**`todo_kinds`**：**待办看板的种类开关**（逗号 / 顿号 / 分号 / 空白分隔的 kind 列表，取值见 §5.9）。默认空串 = **全部启用**；**未知或非法的值一律忽略**；整串全是非法值时同样回落到"全部启用"（不会把看板清空）。关掉的种类**连记都不记**（不是"记了但隐藏"），`GET /api/todos/summary` 的 `disabled_kinds` 会列出当前关掉的种类。这个键**已在 `settings` 表里**（`app/db.py` 的 `DEFAULT_SETTINGS`，初值 `""`）**并已登记进 `STR_KEYS`**，所以 `PUT /api/settings` 可以直接写它；但它**不会出现在 `GET /api/settings` 的响应里** —— 当前生效值请看 `GET /api/todos/summary` 的 `disabled_kinds`（§5.9）。**没登记的表外键才会撞下面那条「表外的键名 ⇒ `400`」**。

**不出现在这份响应里的「服务端记账键」**（后端用 `INTERNAL_KEYS` 过滤掉，**不是"已删除"**）：

| 被隐藏的键 | 为什么不给看 | 想观测该去哪看 |
|---|---|---|
| `config_last_export_at`、`config_last_import_at` | 服务端自己记账（上次把 DB 写出成配置文件、上次从配置文件导入的时间），**不是可编辑设置**。一旦暴露，前端会把它们渲染成表单，用户一保存就撞「未知的设置项」`400` | `GET /api/config` 的 `last_export_at` / `last_import_at`（§5.8.1） |
| `qbittorrent_last_status`、`qbittorrent_version` | 上次测试连接的**结果缓存**，同理不可编辑。⚠️ 注意设置键是 `qbittorrent_version`（**没有** `last_`），与 `qbittorrent_last_status` **不对称** | 设置页「测试连接」的结果、`GET /api/stats/overview` 的 `qbittorrent{status,version}`（§5.1）。这两个键还会被后台扫描线程顺带刷新：每 5 个 tick 用已保存的地址测一次连通性并写回 |
| `scan_last_run_at` | 扫描器的**运行事实**（只读时刻），同理 | `GET /api/system/info` 的 `scan.last_run_at` / `scan.next_run_at`（§5.6） |

> 📌 后端 `INTERNAL_KEYS`（`app/routers/settings.py`）是**上表 5 个 + 2 个遗留键**，共 **7 个**：`config_last_import_at`、`config_last_export_at`、`qbittorrent_last_status`、`qbittorrent_version`、`scan_last_run_at`、`seed_logs_on_first_run`、`log_seeded_at`。后两个是**遗留键**：不再使用，但老数据卷里可能还在（读取不报错，只是不展示）。

> 📌 **表外的键**（新版本新增、或你手工塞进 `settings` 表的）也会**原样带上**，不会"莫名其妙消失"。
> ⚠️ 值已按类型解析 —— 是**真类型**，不是 JSON 字符串（不要按 `"false"` / `"900"` / `"[\"/vol1/1000/Download/qbittorrent\"]"` 这种形态去解析）。

### 5.3 `PUT /api/settings` — 部分更新设置

- **认证**：需要
- **行为**：只更新你传入的键，其他键保持不变；**返回更新后的全量对象**（形状与 §5.2 完全一致，值也是已解析类型）。
- **请求体**：与 `GET` **同样的真类型**（不是字符串！），可以只传一部分：

```json
{"scan_enabled": true, "scan_interval_seconds": 600, "rename_extensions": [".mkv", ".mp4"]}
```

**校验规则（违反一律 `400`，`detail` 是中文说明）**

| 键类型 | 要求 |
|---|---|
| 布尔（`scan_enabled` / `orphan_cleanup_enabled`） | 必须是 `true` / `false`；传字符串 `"true"` → 400 |
| 整数（`scan_interval_seconds` / `max_history` / `max_task_log_lines` / `config_sync_interval_seconds`） | 必须是整数（`true` 会被拒 —— 布尔不能当 1 用），且落在区间内 |
| 后缀数组（`rename_extensions`） | 字符串数组，每项**以点开头**（如 `".mkv"`）；大小写不敏感（`".MKV"` 归一化成 `".mkv"`）且**保序去重**；**空数组合法**（= 未设置，行为回落到处理器清单） |
| 正则数组（`extras_patterns`） | 字符串数组，且**每一条都必须能被 `re.compile`**；有一条编不过就整体 `400`，中文点名第几条 + 模式原文（如 `第 2 条正则 'NCOP(' 无法编译：missing ), unterminated subpattern at position 4。…`）。**空数组合法 = 关闭 L3**；数组里出现空行 / 空字符串 ⇒ `400`（要关就写 `[]`，别写 `[""]`）；条目**保序去重** |
| 字符串（`log_level` / `qbittorrent_*` / `hook_token` / `todo_kinds`） | 必须是字符串；`log_level` 只能是 `DEBUG` / `INFO` / `WARNING` / `ERROR`；`todo_kinds` 的**非法值不会 400**（服务端读的时候忽略非法项、必要时回落"全部启用"，见 §5.2）。⚠️ `config_dir` **不是设置项**，传它只会撞「表外的键名」 |
| 表外的键名 | 400（防止把 `scan_enabeld` 这种拼错的键写进去，然后永远读不出效果） |

整数区间：`scan_interval_seconds` **30–86400**（小于 30 秒没意义，一次全量扫描本身就可能更久）、`max_history` **10–100000**、`max_task_log_lines` **100–1000000**（太小会把正常的逐文件日志截掉）、`config_sync_interval_seconds` **0–86400**（**`0` = 关闭自动导入**，只保留手工 `POST /api/config/reload`）。

**四个特殊传值**

| 传值 | 效果 |
|---|---|
| `qbittorrent_password: "***"` | **保持库里原值**（前端把整个设置对象原样回传时不会冲掉密码） |
| `qbittorrent_password: null` | **清空已保存的密码**（显式 JSON `null`；这是"把填错的密码去掉"的通道。三种写法语义互不重叠：`null` = 清空、`"***"` = 保持原值、缺省 = 不变） |
| `hook_token: "***"`（或不传该键） | **保持不变** |
| `hook_token: ""` | **重新随机生成一个新 token**（`secrets.token_urlsafe(32)`） |
| `hook_token: "具体值"` | **改成这个值**（自定义 token） |

> 📌 `hook_token` 的**三种写入语义**（空串 = 重新生成 / 具体值 = 改成该值 / `"***"` 或缺省 = 不变）合起来构成**唯一的轮换入口**：这个 token **不是环境变量**，`.env` 里没有它也不会被读取；它由首次启动随机生成并存在 `/data` 的数据库里，`GET /api/settings` **明文**返回。
> ⚠️ 换掉 token 后，qBittorrent 里那条已配置的回调命令会**立刻开始 401**，必须把新命令重新粘进「运行外部程序」（见 [`docs/guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md) 的「在 qBittorrent 里填写」一节）。

**错误示例**

`400`（类型不符）：`{"detail": "设置项 scan_enabled 需要布尔值（true/false），收到 str。"}`

`400`（整数越界）：`{"detail": "设置项 scan_interval_seconds 必须在 30 到 86400 之间，收到 5。"}`

`400`（未知键）：

```json
{"detail": "未知的设置项：scan_enabeld。可用设置项：config_sync_interval_seconds、extras_patterns、hook_token、log_level、max_history、max_task_log_lines、orphan_cleanup_enabled、qbittorrent_password、qbittorrent_url、qbittorrent_username、rename_extensions、scan_enabled、scan_interval_seconds、todo_kinds。"}
```

> `log_level` 改完**立刻生效**（当场调整根 logger 与 `app` logger 的等级），不需要重启容器。

**默认值与含义**

| key | 默认值 | 说明 |
|---|---|---|
| `hook_token` | 随机生成 | `/api/hooks/*` 的 token（三种传法见 §6） |
| `scan_enabled` | `false` | 目录扫描补偿开关（回调漏了也能补上） |
| `scan_interval_seconds` | `900` | 扫描周期（秒），区间 30–86400 |
| `qbittorrent_url` | `""` | 例如 `http://$NAS_IP:18080` |
| `qbittorrent_username` / `qbittorrent_password` | `""` | 可选，用于 API 补偿与「测试连接」 |
| `log_level` | `INFO` | 应用日志等级 |
| `orphan_cleanup_enabled` | `true` | 全局孤儿清理总开关（**全局为 false 时，所有库都不会清理孤儿**） |
| `max_history` | `2000` | 任务历史保留条数，超出自动裁剪 |
| `max_task_log_lines` | `5000` | **单个任务**最多保留多少行日志，超出后只写一条"已达上限"的提示 |
| `config_sync_interval_seconds` | `5` | 后台"从磁盘导入配置"的周期（秒）；**`0` = 关闭自动导入** |
| `rename_extensions` | `[]` | 全局重命名后缀白名单（三层里居中，见 §5.2）；`[]` = 未设置 |
| `extras_patterns` | `["NCOP", "NCED", "Preview", "Creditless", "Info", "予告編", "CM集"]` | 散装特典判据；`[]` = **关闭**该功能。详见 §5.2 |
| `todo_kinds` | `""` | 待办看板种类开关；空串 = **全部启用**。⚠️ 它**不出现在** `GET /api/settings` 的响应里（见 §5.2） |

> ⚠️ `config_dir` **不在上表里** —— 它不是设置项（改不了），是服务端从环境/数据卷推导出的目录。要看它请用 `GET /api/system/info` 的 `config_dir`（§5.6）。
> ⚠️ `orphan_cleanup_enabled` 是**全局总开关**，与每个库自己的 `orphan_cleanup` 是「与」的关系：只有两者都为真，才会真的删除孤儿硬链接文件夹。改它之前请先读 [`docs/guides/USER_GUIDE.md`](../guides/USER_GUIDE.md) 的「孤儿清理」一节。

### 5.4 `POST /api/settings/test-qbittorrent` — 测试 qBittorrent 连接

- **认证**：需要
- **请求体**（**可省略**，省略 = 用已保存的设置）：`{"url": "...", "username": "...", "password": "..."}`，三个字段都可选：
  - 不传某个字段 → 用库里已保存的值；
  - `password: "***"` → 用库里保存的真密码（前端把设置对象原样回传时不会把密码弄丢）。
- **行为**：向 qBittorrent 发一次真实请求并计时。

**响应 `200`（连得上）**：

```json
{"ok": true, "version": "5.0.4", "message": "连接成功，qBittorrent 版本 5.0.4", "latency_ms": 12}
```

**响应 `200`（连不上 —— 这是业务结果，不是 HTTP 错误）**：

```json
{"ok": false, "version": null, "message": "连接超时。", "latency_ms": 3001}
```

| 字段 | 说明 |
|---|---|
| `ok` | 是否连上 |
| `version` | 成功时是 qBittorrent 版本号；失败为 `null` |
| `message` | 中文可读信息，可直接显示给用户 |
| `latency_ms` | 本次探测耗时（毫秒，整数） |

**错误示例** `400`（参数不全属于"调用错了"，所以给 400 而不是 `ok:false`）：

```json
{"detail": "尚未配置 qBittorrent 地址，请先填写地址再测试连接。"}
```

```json
{"detail": "qBittorrent 地址必须以 http:// 或 https:// 开头。"}
```

> 📌 业务失败也是 `200`：连不上、认证失败都只体现在 `ok` 上（`ok:false` + 中文 `message`），只有参数问题才 `400`。

### 5.5 `GET /api/system/health` — 健康检查（无需认证）

- **认证**：**不需要**（容器 `HEALTHCHECK` 用它）
- **响应 `200`**（**永远是 `200`**，健康与否只看 `status` 字段）：

```json
{
  "status": "ok",
  "checks": {"db": true, "data_dir": true, "processors": true, "config": true},
  "details": {
    "db": "数据库可读：/data/mlm.db",
    "data_dir": "/data 可写。",
    "processors": "内置处理器已加载：3/3。",
    "config": "/data/config 可写。"
  },
  "warnings": [],
  "version": "1.0.0",
  "time": "2026-10-09 01:20:11"
}
```

| 字段 | 说明 |
|---|---|
| `status` | 只有**所有 `checks` 为真且 `warnings` 为空**时才是 `"ok"`，否则 `"degraded"` |
| `checks` | **四个**布尔项，见下表 |
| `details` | 与 `checks` **同样的键**，每项一句中文说明；失败时这里写明具体原因 |
| `warnings` | 非致命但值得注意的事：目前唯一来源是**文件系统编码不是 UTF-8**（读中文日志时可能抛 `UnicodeDecodeError`，请确认 `LANG=C.UTF-8` / `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`） |
| `version` / `time` | 应用版本号与本次探测时间（本地时间字符串） |

| check | 含义 | 失败怎么办 |
|---|---|---|
| `db` | SQLite 可读写（执行一次真实查询） | 检查 `data/` 卷是否可写、磁盘是否满 |
| `processors` | 三个内置处理器都已成功加载（`processors/` 目录可解析） | 看 `GET /api/config` 的 `errors`；`/data/config/processors/*.yaml` 被手改坏时会出现提示 |
| `config` | `/data/config` **可写**（真的建一个临时文件再删掉） | 检查 `data/` 卷属主（PUID/PGID）与磁盘空间 |
| `data_dir` | `<DATA_DIR>` 可写（**真的建一个临时文件再删掉**，不是看权限位） | 检查数据卷挂载与属主（PUID/PGID） |

> 📌 v2 起**不再有** `downloads_readable` / `library_writable` 两项：它们只探「允许的源/目标根目录」白名单的
> 第一个根，而白名单机制已整体删除。源/目标目录是否**同挂载**改由**建库/改库时**校验（§3.2，跨挂载直接 `422`），
> 运行期的问题体现在任务的 `warnings[]` / `link_failed` 里；`/api/system/info` 的 `same_filesystem` 仍是只读参考。

> 这个接口**刻意永不抛异常**：数据库挂掉时它也正常返回 `200 + degraded`，而不是 `500` —— 探测脚本只认 HTTP 状态码与 `status` 字段。
> ⚠️ **健康检查的判据是响应体而不是 HTTP 状态码**：`docker/healthcheck.sh` 会解析响应 JSON，**`status == "ok"` 且 `checks` 四个键全为 `true`** 才退出码 0，否则把 `[失败] checks.xxx = false` / `[缺失] checks.xxx 未出现在响应里` 逐条写到 stderr（`degraded` 的容器因此会真的显示 `(unhealthy)`）。容器显示 `unhealthy` 时的定位步骤见 [`docs/guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md) 的「容器显示 `unhealthy` 时怎么定位」一节。

> 📌 源目录的不可变性仍由「代码层不写源目录 + 任务前后逐文件源完整性核对」保证（见 §4.1 的 `Task.result.source_integrity`）。**这是「可验证的保证」，不是结构性保证。**

**验证服务是否真的健康**（镜像里没有 `curl`，用宿主机的）：

```bash
# 1) Docker 自己的判定（healthcheck.sh：status=="ok" 且 checks 四项全 true 才 healthy）
docker inspect --format '{{.State.Health.Status}}' media-ingest

# 2) 看接口原始返回
curl -s http://$NAS_IP:18081/api/system/health | python3 -m json.tool

# 3) 在容器内查（用镜像自带的 python）
docker exec media-ingest python3 -c "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:18081/api/system/health')),ensure_ascii=False,indent=2))"
```

### 5.6 `GET /api/system/info` — 系统与路径信息

- **认证**：需要
- **响应 `200`**：给「设置 → 系统信息」页用的**诊断全量数据**。

```json
{
  "version": "1.0.0",
  "service": "media-ingest",
  "container_name": "media-ingest",
  "timezone": "Asia/Shanghai",
  "data_dir": "/data",
  "log_dir": "/data/logs",
  "config_dir": "/data/config",
  "path_map": {"container_to_host": {}, "host_to_container": {}},
  "ports": {"container": 18081, "host": 18081, "protocol": "tcp"},
  "mounts": [
    {"container_path": "/data", "host_path": "/vol1/1000/Docker/media-ingest/data",
     "device": "/dev/sdb1", "fs_type": "ext4", "options": "rw,relatime",
     "st_dev": 55, "exists": true, "read_only": false}
  ],
  "same_filesystem": {
    "source_path": "/vol1/1000/Download/qbittorrent", "target_path": "/vol1/1000/Library",
    "source_dev": 55, "target_dev": 55, "downloads_vs_library": true,
    "message": "源 /vol1/1000/Download/qbittorrent 与目标 /vol1/1000/Library 同属挂载 /vol1/1000（mount ID 26）。注意：挂载 ID 相等是**必要不充分**判据（btrfs 同一挂载内跨子卷仍可能 EXDEV）。（未实证，仅据挂载 ID；该判据必要不充分。）"
  },
  "processors": {
    "total": 3,
    "builtin_ids": ["hardlink_only", "bangumi", "movie"],
    "items": [{"id": "bangumi", "name": "番剧", "kind": "rename", "builtin": true}]
  },
  "libraries": [
    {"id": 1, "name": "番剧", "source_dir": "/vol1/1000/Download/qbittorrent/Bangumi",
     "target_dir": "/vol1/1000/Library/Bangumi", "processor_id": "bangumi", "enabled": true}
  ],
  "rename_logs": [
    {"library_id": 1, "library_name": "番剧库", "log_file": "/data/logs/bangumicache.log",
     "exists": true, "entries": 128, "last_write": "2026-01-15 09:30:00"}
  ],
  "log_level": "INFO",
  "queue": {"running_task_id": null, "queued_count": 0},
  "scan": {"enabled": false, "interval_seconds": 900, "last_run_at": null, "next_run_at": null, "running": false},
  "config": {
    "config_dir": "/data/config",
    "files": [{"path": "/data/config/libraries.yaml", "relative": "libraries.yaml", "exists": true,
               "sha256": "9f2c…", "in_sync": true, "size": 812, "updated_at": "2026-10-20 10:15:00"}],
    "last_import_at": null, "last_export_at": "2026-10-20 10:15:00", "errors": [],
    "watcher_running": true, "interval_seconds": 5.0
  },
  "encoding": {"filesystem": "utf-8", "stdout": "utf-8", "utf8_mode": true, "warning": null},
  "disk": {
    "data": {"path": "/data", "total": 999653638144, "used": 412316860416, "free": 587336777728},
    "library": {"path": "/vol1/1000/Library", "total": 999653638144, "used": 412316860416, "free": 587336777728}
  }
}
```

> ⚠️ 本接口返回的就是下表列出的这些键。

| 字段 | 说明 |
|---|---|
| `version` / `service` / `container_name` | 应用版本号 / 服务名 / 容器名 |
| `timezone` | 容器时区（`TZ`），所有本地时间字段都按它换算 |
| `data_dir` / `log_dir` / `config_dir` | 三个关键目录：数据卷、**日志目录**（`/data/logs`，所有重命名日志都在这一层）、**配置目录**（默认 `/data/config`；`libraries.yaml` 与 `processors/*.yaml` 在这里） |
| `path_map.container_to_host` | `HOST_PATH_MAP` 原样（容器前缀 → 宿主前缀）。**默认是空表 = 恒等映射**（容器路径 = 宿主路径）；**它只是展示用的换算表**，填错不影响任何功能 |
| `path_map.host_to_container` | 上面的**反向**表，前端不用自己反转 |
| `ports` | 容器内监听端口与宿主发布端口（改 `.env` 的 `PORT` 时两者一起动） |
| `mounts[]` | **受关注的挂载点**，动态算出：固定 `/data`、`/data/config`，再加上**每个媒体库**的 `source_dir` / `target_dir` 各自**最近已存在的祖先**（固定项在前，去重）。元素 = `container_path` / `host_path`（按 `path_map` 换算，默认恒等）/ `device` / `fs_type` / `options` / `st_dev` / `exists` / `read_only`。⚠️ `device` / `fs_type` / `options` **只在该路径本身就是挂载点时**才有值（只认精确挂载点、不做前缀回溯），否则是空串 —— 例如 `/data/config` 通常只是 `/data` 卷里的子目录。`read_only` 取决于**实际挂载选项**里有没有独立的 `ro` 项（`errors=remount-ro` 不算），不是配置声明；源目录**不得**只读挂载。⚠️ `st_dev` 是 Linux `dev_t` 的**原始数值**，仅供展示与排查；在**非 Linux 环境**上可能超出 JavaScript 安全整数范围（`JSON.parse` 会丢精度），客户端**不应**据此做逻辑判断 |
| `same_filesystem` | **概览对象**（与 §3.8 的同名字段不同层）：取自**第一个媒体库**（`id` 升序）的 `source_dir` / `target_dir`，形状为 `source_path` / `target_path`、`source_dev` / `target_dev`、`downloads_vs_library`（**三态 `bool \| null`**）、`message`（**逐字**的中文判据文案）。**尚无媒体库时**为 `source_path` / `target_path` 空串、两个 dev 为 `null`、`downloads_vs_library: null`，`message` 为「尚无媒体库：源/目标目录的挂载与硬链接可行性要等建好第一个媒体库后才能判断。」⚠️ 本接口**刻意不做写入型实证探针**（走 `probe=False`：`/info` 会被 UI 反复轮询，不适合每次往目标目录写一个临时链接），所以它只有「挂载 ID 预筛 → 读不到 `/proc` 时降级 `st_dev`」两级：`downloads_vs_library: true` 只代表**预筛通过、未实证**，`false` 才是可靠负判据；权威实证结论看 §3.8 `validate?probe=true` 的 `probe` |
| `processors` | `{total, builtin_ids, items[]}`：`total` = 处理器总数；`builtin_ids` = 内置处理器的 id 列表（固定顺序 `hardlink_only` → `bangumi` → `movie`）；`items[]` = 每个处理器的 `{id, name, kind, builtin}`（**`kind` 是 `rename` / `hardlink_only`**；是否内置看 `builtin`） |
| `libraries[]` | **每个媒体库一条**摘要：`{id, name, source_dir, target_dir, processor_id, enabled}`，按 `id` 升序。⚠️ `rename_logs` 与它**同一个顺序**，前端可以按下标对齐两张表 |
| `rename_logs[]` | **每个媒体库一条**的重命名日志状态：`{library_id, library_name, log_file, exists, entries, last_write}`。`log_file` 是**容器内绝对路径**（`/data/logs/<log_file>`）；`exists=false` ⇒ `entries=0` 且 `last_write=null`；`entries` 是该日志的**顶层键计数**（进程缓存未命中时读盘统计）；`last_write` 是**本地时间** `YYYY-MM-DD HH:MM:SS`（**不是** ISO/UTC） |
| `log_level` | 当前日志等级 |
| `queue` | 正在跑的任务 id 与排队数 |
| `scan` | 扫描补偿调度器的状态：`{enabled, interval_seconds, last_run_at, next_run_at, running}`。`last_run_at` / `next_run_at` 是 **UTC ISO8601**（诊断字段，见 §1.4），从未扫过时为 `null` |
| `config` | **配置同步状态**，形状与 `GET /api/config`（§5.8.1）**完全相同**：`{config_dir, files[], last_import_at, last_export_at, errors[], watcher_running, interval_seconds}`。字段语义只看 §5.8.1，本文不重复 |
| `encoding` | 文件系统编码 / stdout 编码 / 是否 UTF-8 模式 + `warning`（非 UTF-8 时的中文说明）。**这一组分三个字段而不是一个**，就是为了让"日志里的中文为什么乱码"能一眼定位 |
| `disk.data` / `disk.library` | 数据卷 / 媒体库所在文件系统的 `total` / `used` / `free`（字节）；取不到时为 `null` |

> 📌 **`rename_logs` 就是设置页「重命名日志」表的数据来源** —— 前端**不要直接读日志文件**（那是容器内路径，浏览器访问不到，而且并发读会与任务写入打架）。「每个媒体库一份日志、所有重命名日志在同一目录」的可观测性验收就看这个字段。

> 📌 **`HOST_PATH_MAP` 现在是纯展示的，而且默认留空（`HOST_PATH_MAP={}`）**：容器路径 = 宿主路径（恒等），不需要换算；只有「容器路径 ≠ 宿主路径」的非恒等部署才需要自己填。所以它**只影响网页上显示的路径好不好看**，填错、漏填都**不影响任务行为**。键建议写成带前导 `/` 的绝对容器路径，否则显示会不伦不类。

> 日志相关的三个概念分工：**硬链接日志**（`/data/logs/hard_links.log`，纯文本）、**每个媒体库一条重命名日志**（`/data/logs/<log_file>`，JSON 缓存形态）、**任务日志**（数据库 + 内存缓冲，任务详情页轮询）。前两类都是幂等判定的依据，**别手工删**。`hard_links.log` **只写"记账行"**（体积登记与孤儿目录清理两类），uvicorn 访问日志 / 任务执行日志 / 异常回溯等**不会混入**这个文件（该 handler 挂了白名单过滤器，见 [`docs/contracts/ARCHITECTURE.md`](ARCHITECTURE.md) 的「`app/script_adapter.py`」一节）；**只过滤、不清理**，历史行一个字节都不删。权威的日志格式与写入纪律见 [`docs/contracts/BASELINE.md`](BASELINE.md) 的「硬链接契约」与「硬链接日志的写入纪律」两节。

### 5.7 处理器接口（7 条）

**什么是"处理器"**：一个处理器 = 一整套"**怎么重命名**"的规则 —— 命名方式（段序列）+ 命名段表达式 + 接受目录正则 + 拒绝目录正则 + 接受的后缀名 + 用哪一条日志。这些规则**全部是 YAML 配置**，与 WebUI 双向同步，不写死在代码里。

内置三个（`builtin: true`，**可重置、不可删除**）：

| id | 名称 | kind | 执行体 | 说明 |
|---|---|---|---|---|
| `hardlink_only` | 只建硬链接 | `hardlink_only` | 原生实现 `app/processors/hardlink.py` | 不重命名，只做硬链接 |
| `bangumi` | 番剧 | `rename` | 规则引擎 `app/processors/engine.py` | 按「作品 / Season N / 文件」重命名为 `SxxExx` |
| `movie` | 电影 | `rename` | 规则引擎 `app/processors/engine.py` | 「每个后缀只留体积最大者」并命名为 `<标题>.<ext>` |

> 📌 本文档只描述**接口形状**。处理器对象的完整字段语义、默认规则来源、规则引擎的 10 步语义见 [PROCESSORS.md](PROCESSORS.md) 的「§3 数据模型」与「§4 规则引擎」；硬链接行为契约见 [BASELINE.md](BASELINE.md) 的「硬链接契约」一节。

#### 5.7.1 处理器对象（ProcessorSpec）

```yaml
schema_version: 2
id: bangumi
name: 番剧
description: 按「作品/Season N/文件」结构重命名为 SxxExx
kind: rename                  # rename | hardlink_only
enabled: true
builtin: true                 # 内置：可重置、不可删除
traverse:
  title_depth: 1              # 标题文件夹在媒体库根下的层级（当前固定 1）
  file_scope: subdir          # subdir：文件取自 accept_dirs 匹配到的子目录；title：文件直接在标题文件夹里
  accept_dirs:
    - {pattern: '^Season (\d+)$', season_group: 1}
  reject_dirs:                # 命中即整棵子树跳过（大小写不敏感、整名锚定 ^…$）
                              # 以下是 bangumi 的推荐默认：11 条规则；要最朴素的旧口径请 reset 成 legacy
    - {pattern: '(?i)^(sp|cm|op|ed)s?$'}
    - {pattern: '(?i)^scans?$'}
    - {pattern: '(?i)^cds?$'}
    - {pattern: '(?i)^(映像特典|特典cd)$'}
    - {pattern: '(?i)^previews?$'}
    - {pattern: '(?i)^scan \(vol\.\d+\)$'}
    - {pattern: '(?i)^vol\.\d+$'}
    - {pattern: '(?i)^(extra|bonus|menus|music)$'}
    - {pattern: '(?i)^subtitles?$'}
    - {pattern: '(?i)^screenshots?$'}
    - {pattern: '(?i)^(ncop|nced)$'}
  skip_season_zero: true
extensions: [.mkv, .mp4, .ass]   # 大小写敏感
naming:
  expressions: {}             # 具名表达式：按书写顺序求值，可引用上下文与前面算出的
  segments:                   # 最终文件名的组装顺序
    - {param: title}
    - {literal: ' S'}
    - {param: season, pad: 2}
    - {literal: E}
    - {param: episode, pad: 2}
    - {param: suffix}
episode_patterns:             # 9 条有序规则（序号小者优先；全部忽略大小写、数字限 1~2 位；
                              #   每条都带"这里不可能是集号"的边界约束）
  - {name: chapter,        pattern: '(?i)第\s*(?P<ep>\d{1,2})(?!\d)\s*[集话話回]'}
  - {name: episode_word,   pattern: '(?i)(?<![A-Za-z])Episode\s*(?P<ep>\d{1,2})(?!\d)'}
  - {name: season_episode, pattern: '(?i)(?<![A-Za-z])S\s*\d{1,2}(?!\d)\s*E\s*(?P<ep>\d{1,2})(?!\d)'}
  - {name: bracketed,      pattern: '(?i)\[\s*(?P<ep>\d{1,2})\s*\]'}
  - {name: ep_marker,      pattern: '(?i)(?<![A-Za-z])EP\s*(?P<ep>\d{1,2})(?!\d)'}
  - {name: hash_marker,    pattern: '(?i)#\s*(?P<ep>\d{1,2})(?!\d)'}
  - {name: dash,           pattern: '(?i)-\s*(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)'}
  - {name: bare_e,         pattern: '(?i)(?<=\s)[Ee]\s*(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)(?=\s|\.)'}
  - {name: bare_number,    pattern: '(?i)(?<=\s)(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)(?=\s|\.)'}
episode_fallback: None        # 推荐默认：YAML 的 None ⇒ null ⇒ 认不出集数就**保留原文件名**
                              # （`legacy` 是字符串 "None" ⇒ 命名成 `… S01ENone.mkv`）
                              # 空串 / 纯空白与 null 等价（读取时归一，不改写库里的 JSON）
selection:
  per: file                   # file = 每个匹配文件都处理；extension = 同后缀只取体积最大者
log_file: bangumicache.log    # 只允许文件名；落在 settings.log_dir（/data/logs）
log_format: json_cache
```

`legacy` 预设把 `episode_patterns` 换回两条：`{name: ep, pattern: '(?i)(?:第\s*(?P<g1>\d{1,2})\s*[集话話回]|\[\s*(?P<g2>\d{1,2})\s*\]|Episode\s*(?P<g3>\d{1,2})|EP\s*(?P<g4>\d{1,2})|e(?P<g5>\d{1,2})|E(?P<g6>\d{1,2})|\#\s*(?P<g7>\d{1,2})|-\s*(?P<g8>\d{1,2}))'}` 与 `{name: trailing, pattern: '(?:\s+(?P<g1>\d{1,2}))'}`。

> 📌 **`movie` 的默认 `reject_dirs` 是另一套**（共 10 条，同样大小写不敏感、整名锚定）：`^(特典|特典cd)$`、`^(cd|cds)$`、`^扫图$`、`^scans?$`、`^(sp|sps)$`、`^(menu|menus)$`、`^(previews?)$`、`^(extra|bonus)$`、`^(ncop|nced)$`、`^samples?$`（均带 `(?i)`）。
> **两套都不得把发行组/压制组前缀当作拒绝对象** —— `[ANi]`、`[VCB-Studio]` 这类目录**照常处理**。
> `hardlink_only` 的 `reject_dirs` 默认是空数组（它只建链，不重命名）。

**`naming` 里可以用的名字**（`segments[].param` 与 `expressions` 右值都能引用）：

| 名字 | 取值 |
|---|---|
| `title` | 标题文件夹名 |
| `filename` | 作用域内当前文件名（含扩展名） |
| `stem` | 原文件名去掉最后一个扩展名 |
| `ext` / `extname` | 扩展名（带点 `.mkv` / 不带点 `mkv`） |
| `season_raw` / `season` | `accept_dirs` 的 `season_group` 捕获组（**缺省 `1`**）。`season_raw` 在「`season_group: 0` / `accept_dirs` 为空 / `file_scope=title`」三种情形下都是 `""`；`season` = `season_raw.zfill(2)`，因此**无季号时是 `"00"` 而不是空串**，只有 `file_scope=title` 时 `season` 才是 `""` |
| `episode_raw` / `episode` | `episode_patterns` **按书写顺序**首个命中的那条里**序号最小的非空命名组**（自定义规则**必须**把集号命名为 `ep`；取不到命名组时退回"序号最小的数字组"，`legacy` 的 `g1..g8` 就是靠这条兜底）；后者是 `.zfill(2)`；全不命中**且**没配 `episode_fallback` 时两者都是**空串**，此时若命名引用了它们，这个文件**保留原文件名、不改名**（见 `result.kept_unresolved_episode`）；显式配置了 `episode_fallback` 时用该字面量 |
| `episode_resolved` | **引擎内部键**：`true` = 解析出了集数（命中模式或配了 `episode_fallback`）。它**不在** WebUI 变量按钮的 12 个变量表（`TEMPLATE_VARS`）里，但**模板写法与段写法都认**（可用名单 = `TEMPLATE_VARS` + `episode_resolved` + 该 spec `naming.expressions` 里定义的名字，由 `app/processors/spec.py` 的 `known_context_names()` 给出） |
| `suffix` | `.ass` 文件取多段后缀（`.chs.ass`）；其他文件等价于 `ext` |
| `works_dir` / `rel_dir` | 该库的 `source_dir` / 作用域目录相对标题文件夹的路径 |

`segments[]` 只接受 **`param` / `literal` / `pad`** 三个键；`param` 先查 `expressions`、再查上表；`pad` 做 `str(v).zfill(pad)`。
`expressions` 的右值是 **Python 表达式**，可用 `re` / `os` / `len` / `str` / `int` / `sorted` / `min` / `max`，以及 `zfill(v,n)` / `splitext(p)` / `basename(p)` / `ass_suffix(filename)`。

> ⚠️ **安全面（已在 README 与设置页明示）**：`expressions` 在应用进程内用 `eval` 求值 —— 等价于"允许在配置文件里执行 Python 表达式"。这是为满足"可拓展、不写死"而接受的取舍；配置文件位于 `/data` 数据卷内，**能改它的人本来就能改容器配置**。别把写权限交给不可信的人。

#### 5.7.2 `GET /api/processors` — 处理器列表

- **认证**：需要
- **查询参数**：`builtin`（`true` / `false`）、`kind`（`rename` / `hardlink_only`）、`limit`（默认 50，最大 200）、`offset`
- **排序**：**内置优先**，固定按 `hardlink_only` → `bangumi` → `movie`（即 `app/processors/defaults.py` 的 `BUILTIN_IDS` 顺序，**不是字母序**）；其余自定义处理器排在后面、按 `id` 升序。分页稳定。
- **响应 `200`**：**分页信封**（结构见 §1.2）

```json
{"items": [ {"schema_version": 2, "id": "bangumi", "name": "番剧", "kind": "rename", "enabled": true, "builtin": true, "traverse": {}, "extensions": [".mkv", ".mp4", ".ass"], "naming": {}, "episode_patterns": [], "episode_fallback": null, "selection": {"per": "file"}, "log_file": "bangumicache.log", "log_format": "json_cache"} ],
 "total": 3, "limit": 50, "offset": 0}
```

#### 5.7.3 `GET /api/processors/{id}` — 单条

- 不存在 ⇒ `404`（中文 `detail`）

#### 5.7.4 `POST /api/processors` — 新建自定义处理器

- **请求体**：`ProcessorCreate`（`app/api_models.py`）—— 只有五个键：

| 键 | 类型 | 说明 |
|---|---|---|
| `id` | string | **必填**，`^[a-z][a-z0-9_-]{1,31}$` |
| `name` | string | 可省略，默认 `""` |
| `kind` | string \| null | 可省略；`rename` / `hardlink_only`，省略时按默认推导 |
| `template` | string | 可省略，默认 `"blank"`；新建时的初始规则模板 |
| `spec` | object \| null | 可省略；直接给完整 spec 时以它为准 |

  `schema_version` 由**服务端**写，`builtin` 一律为 `false`。
- **响应 `201`**：新建后的完整对象
- `id` 已存在 ⇒ `409`；`id` 不符合 `^[a-z][a-z0-9_-]{1,31}$` ⇒ `422`
- 新建后媒体库表单的处理器下拉里就会多出这一项（前端拉 `GET /api/processors`）

#### 5.7.5 `PUT /api/processors/{id}` — 全量替换规则

- **请求体**：完整 spec；`id` **不可改**（改了就 `422`）
- **内置允许改规则**（改完会写一条配置事件，便于追溯）；**响应 `200`**：替换后的完整对象
- **典型用法**：改 `naming.segments` 调整命名顺序、加减 `reject_dirs`、增删 `extensions`

#### 5.7.6 `DELETE /api/processors/{id}` — 删除自定义处理器

- 内置 ⇒ `409`（内置**永不删除**，只能重置）
- 被任何一个媒体库引用 ⇒ `409`，**并在 `detail` 里给出引用清单**：

```json
{"detail": "处理器「纪录片」仍被 2 个媒体库引用，请先改掉这些媒体库的处理器：纪录片库、4K 纪录片"}
```

#### 5.7.7 `POST /api/processors/{id}/reset` — 恢复默认 / 最朴素的旧口径

- **请求体**：`{"preset": "default"}` 或 `{"preset": "legacy"}`
- **响应 `200`**：重置后的完整对象

| preset | 效果 |
|---|---|
| `default` | 内置推荐值：**相对 `legacy` 的四处增量** —— ① bangumi `reject_dirs`、② movie `reject_dirs`、③ bangumi `episode_fallback: null`（**认不出集数就保留原文件名**，不会命名成 `… S01ENone.mkv`）、④ bangumi `episode_patterns` 九条有序规则 |
| `legacy` | **最朴素的旧口径**：`reject_dirs: []` + `episode_fallback: "None"`（大小写敏感的后缀白名单；认不出集数时命名的集号位置会写成字面量 `None`） |

> 💡 "我想让结果和以前完全一样" ⇒ 对每个内置处理器执行一次 `reset {"preset":"legacy"}`（见 [`docs/guides/FAQ.md`](../guides/FAQ.md) 的对应条目）。
> ⚠️ **升级提示**：`reset` **只在你主动调用时**才改变处理器；升级镜像不会自动改**已经存在**的处理器规格（播种只在 `processors` 表为空时发生）。既有实例想吃到「认不出集数就保留原名」，用 `POST /api/processors/bangumi/reset {"preset":"default"}`（整套回推荐默认，会覆盖对其的其它自定义），或 `PUT /api/processors/bangumi` 只传 `{"episode_fallback": null}`（等价于直接改 `/data/config/processors/bangumi.yaml`）。
> ⚠️ **不要**用 WebUI 编辑页的「集数兜底」输入框**清空**来表达"留空"：它提交的是空串 `""` 而不是 `null`，引擎会把空串当成"已解析"（结果会命名成 `S01E00…`）。

#### 5.7.8 `POST /api/processors/validate` — 只校验，不落库

- **请求体**：一个 spec；**响应 `200`**：`{"ok": bool, "errors": [{"field": "naming.segments[1]", "message": "中文说明"}]}`
- **注意**：这个接口**不写数据库、不写文件**，专门给规则编辑器做"保存前先试一下"
- **全项目只有这个接口用 `field` / `message` 这种错误元素**；其它接口的 `422` 都是 `detail[{loc,msg,type}]`（见 §1.3）

**配置校验错误对照表**（处理器接口的 `422` 与 `/api/processors/validate` 的 `errors[]` 用的是**同一套判据**，只有**返回形状不同**：前者 `detail[{loc,msg,type}]`，后者 `errors[{field,message}]` —— 下表列的是 `field`。判据相同的另一个含义：这里能被 `field` 指出的问题，写进文件后也会让那次导入失败、并出现在 `GET /api/config` 的 `errors[]` 里）

| `field` | 常见 `message` | 怎么修 |
|---|---|---|
| `schema_version` | 必须是 2 | 手改过 YAML 的：把开头改回 `schema_version: 2` |
| `id` | 只能是 `^[a-z][a-z0-9_-]{1,31}$` | 用小写字母开头、只用小写字母/数字/`_`/`-`，2–32 位 |
| `kind` | 只能是 `rename` 或 `hardlink_only` | 拼写检查 |
| `extensions[i]` | 后缀必须以 `.` 开头 | 写 `.mkv` 而不是 `mkv` |
| `naming.segments[i]` | 段只接受 `param` / `literal` / `pad` 三个键 | 删掉多余键；`param` 与 `literal` 至少有一个 |
| `naming.segments[i].param` | `段里用了未知变量 {xxx}。可用变量：…；自定义名字要先在 naming.expressions 里定义。`（`param` 是空串时是 `param 不能是空字符串（这个段会什么都不输出）。`） | 用 WebUI 编辑页的**下拉框**重选参数（它只列出可用名单）；或先在「命名表达式」里定义那个名字。**运行期不拦**（老配置照旧跑，只在 `unknown_params` 里留痕，见 §4.1 与 KI-11） |
| `naming.template` | `模板里用了未知变量 {xxx}。可用变量：…。`（名单含 `episode_resolved` 与表达式名） | 同上一行 |
| `naming.expressions.<name>` | 表达式无法求值（`eval` 失败） | 检查括号/引号；只使用上面列出的名字与函数 |
| `traverse.accept_dirs[i].pattern` | 正则无法编译 | 正则语法错误；YAML 里建议用单引号标量 |
| `traverse.accept_dirs[i].season_group` | 捕获组序号超出正则里的捕获组数量（缺省 1；合法 0 或 ≥1） | 改成实际存在的组号，或写 `0` 表示"只判断接受、不取季号"（`season_raw` 为空串、`season` 补零为 `"00"`，与 `accept_dirs` 为空逐字相同） |
| `traverse.reject_dirs[i].pattern` | 正则无法编译 | 同上 |
| `episode_patterns[i].pattern` | 正则无法编译 | 同上 |
| `selection.per` | 只能是 `file` 或 `extension` | 拼写检查 |
| `traverse.title_depth` | 当前只支持 `1` | 别改（标题文件夹 = 媒体库根的直接子目录） |
| `log_file` | 只允许文件名，不能包含 `/` 或 `..` | 日志**必须**都放在 `/data/logs` 同一层（见 §5.7.9） |

#### 5.7.9 处理器与日志的约定

| 类别 | 位置 | 形态 |
|---|---|---|
| **硬链接日志** | `/data/logs/hard_links.log` | 纯文本 `%(asctime)s - %(message)s`（由 `app/processors/hardlink.py` 写）。**只写两类记账行**（体积登记 / 孤儿目录清理），其余应用日志不混入；历史行不清理 |
| **每个媒体库一条重命名日志** | `/data/logs/<log_file>` | `json_cache`：`{"<标题文件夹>": {"size": N, "timestamp": "YYYY-MM-DD HH:MM:SS", "works_dir": "…"}}` |
| **任务日志** | SQLite + 内存缓冲 | 全部处理器共享，任务详情页轮询（§4.4） |

- **所有重命名日志必须在同一目录** `/data/logs`，**不允许**按媒体库分子目录；
- **设置页的「重命名日志」表（库名 / 日志文件 / 条目数 / 最后写入时间）数据来源就是 §5.6 的 `rename_logs`** —— 前端**不直接读日志文件**（容器内路径，浏览器访问不到；并发读还会与任务写入打架）。这也是「每个媒体库一份日志」的可观测性验收点；
- **每个媒体库各自一条**（依赖"按库唯一化"，规则见 [PROCESSORS.md](PROCESSORS.md) 的「日志文件唯一化」一节）：缺省命名 = 该处理器登记的 `log_file`（内置 `bangumicache.log` / `moviecache.log` / `hardlink_only.log`；自定义处理器 `<id>cache.log`），然后**按库唯一化** —— 该名已被**其它库**或**其它处理器**占用时，在扩展名前加 `-2`、`-3`…（第二个用 `bangumi` 的库拿到 `bangumicache-2.log`）；**第一个**使用某处理器的库保持登记名，**磁盘上已有同名文件时照样用它并继续追加**；显式传入的名字与其它库撞车 ⇒ **422**（字段 `log_file`）；
- 新建库时若日志文件不存在则创建空 `{}`；
- **日志就是"已处理"的判据**（对重命名而言是"标题文件夹 + 体积"）：手工删掉它会让那些文件夹被重新处理一遍（无害，但会刷出一批日志）。账本与重做的完整语义见 §3.10。
- **库日志文件只记"记账行"（日志隔离）**：`hard_links.log` 只接收 `Source: … - Folder: … - Folder size: …`（必须同时含 ` - Folder: ` 与 ` - Folder size: `）与 `Deleted orphaned hard link folder: …` 两类行，其余应用日志（uvicorn 访问日志 / 任务日志 / 异常回溯）在这个 handler 上被丢弃。这些文件都是**数据型**的：**只过滤、不清理、不轮转**（历史行一个字节都不删）。任务日志与任务历史的上限（`max_task_log_lines` 默认 5000 / `max_history` 默认 2000）与它们无关。写入纪律的权威定义见 [BASELINE.md](BASELINE.md) 的「硬链接日志的写入纪律」一节。

### 5.8 配置同步接口（3 条）

**配置文件在哪**（`settings.config_dir`，默认 `/data/config`）：

```
/data/config/
├── libraries.yaml            # 媒体库清单
└── processors/
    ├── bangumi.yaml          # 一个处理器一个文件，文件名 = <id>.yaml
    ├── movie.yaml
    ├── hardlink_only.yaml
    └── <custom-id>.yaml
```

格式是 **YAML**（`PyYAML==6.0.3`；UTF-8、无 BOM、以 `schema_version: 2` 开头）。选 YAML 不选 JSON 的理由是**给人手改**：能写注释、正则不必双重转义。

> 📌 **单一写入仲裁者 + 双向同步**：运行时真源**永远是数据库**，配置文件是它的**可移植镜像**。
> **写出去**：任何一次 API 成功写入，都在**同一个请求内**把受影响文件原子写出（`*.yaml.tmp` → `os.replace`），并把新 sha256 记进 `config_files`。
> **读进来**：后台线程每 `config_sync_interval_seconds`（默认 **5** 秒）比对文件的 mtime + sha256；哈希 ≠ 上次记录的哈希 ⇒ 判定为**外部编辑** ⇒ 导入。
> **导入失败时**：**数据库原样不动**、错误记进 `config_events` 并通过下面的 `GET /api/config` 的 `errors` 暴露成 WebUI 顶部横幅 —— **手改坏的文件不会被应用覆盖掉**，可以从容修好再导入。
> **API 写入与后台导入共用同一把锁** ⇒ 两边不会交错。

#### 5.8.1 `GET /api/config` — 配置同步状态

- **认证**：需要
- **响应 `200`**：

```json
{
  "config_dir": "/data/config",
  "files": [
    {"path": "/data/config/libraries.yaml", "relative": "libraries.yaml", "exists": true,
     "sha256": "9f2c…", "in_sync": true, "size": 812, "updated_at": "2026-10-20 10:15:00"},
    {"path": "/data/config/processors/bangumi.yaml", "relative": "processors/bangumi.yaml", "exists": true,
     "sha256": "1ab3…", "in_sync": true, "size": 1402, "updated_at": "2026-10-20 10:15:00"}
  ],
  "last_import_at": "2026-10-20 10:15:12",
  "last_export_at": "2026-10-20 10:15:00",
  "errors": [],
  "watcher_running": true,
  "interval_seconds": 5.0
}
```

| 字段 | 说明 |
|---|---|
| `files[].in_sync` | `true` = 磁盘上的内容和数据库记录的哈希一致（即"文件就是数据库的镜像"）。`false` ⇒ 文件被外部改过，还没导入（或导入失败） |
| `last_import_at` / `last_export_at` | 最近一次"从文件导入"与"写出到文件"的时间（本地时间字符串），从未发生过时为 `null` |
| `errors[]` | 导入失败的原因（`path` / `message` / `at`）。**非空时 WebUI 顶部会显示红色横幅** |
| `watcher_running` | 后台"配置文件监视"线程是否活着（`true` 表示外部编辑会被自动导入） |
| `interval_seconds` | 该线程的轮询间隔（秒，来自设置项 `config_sync_interval_seconds`，默认 `5.0`）；设为 `0` ⇒ 自动导入已关闭、不再轮询 |

> 📌 **`errors` 的准确语义**：它只列出**最近一次成功导入之后**发生的导入错误（锚在 `config_events.id` 上判定，不是比时间字符串；上限 20 条）。**它不是一个只追加的历史日志** —— 导入失败不会改动数据库，把文件修好、让它重新导入成功后，这个数组会**自动清空**。从来没有成功导入过时，任何一条 `import-error` 都算"当前错误"。
> 📌 **导入的返回值**（`app/configstore.py` 的导入函数）：`{"ok": bool, "path": str, "imported": ...|null, "errors": [...]}`；**失败时 `imported` 为 `null`**，并且只写一条错误事件（不会写半份数据）。

#### 5.8.2 `POST /api/config/reload` — 强制从文件导入

- **查询参数**：`force=true` ⇒ 忽略哈希直接导入（默认只导入"哈希对不上"的文件）
- **响应 `200`**：先是与 `GET /api/config` **完全相同**的那组快照字段，再额外多一个 `result`：

```json
{
  "config_dir": "/data/config",
  "files": [ {"path": "/data/config/processors/bangumi.yaml", "relative": "processors/bangumi.yaml",
              "exists": true, "sha256": "1ab3…", "in_sync": true, "size": 1402,
              "updated_at": "2026-10-13 02:31:07"} ],
  "last_import_at": "2026-10-13 02:31:07",
  "last_export_at": "2026-10-13 02:30:55",
  "errors": [],
  "watcher_running": true,
  "interval_seconds": 5.0,
  "result": {
    "ok": true,
    "imported": [
      {"path": "/data/config/processors/bangumi.yaml", "kind": "processor", "id": "bangumi", "action": "updated", "builtin": true},
      {"path": "/data/config/libraries.yaml", "kind": "libraries", "created": 1, "updated": 2, "removed": 0}
    ],
    "errors": []
  }
}
```

| `result` 里的字段 | 说明 |
|---|---|
| `result.ok` | **布尔值** = "这一轮没有任何导入错误"（等价于 `result.errors` 为空）。它**不是** `imported`，也不是数组 |
| `result.imported[]` | **只有真正被导入的文件**才有元素（哈希没变、走了"未改动"快速路径的文件**不出现**）。每个元素都带 `path` 与 `kind`：`kind="processor"` ⇒ 另带 `id` / `action`（`"created"` 或 `"updated"`）/ `builtin`；`kind="libraries"` ⇒ 另带 `created` / `updated` / `removed` |
| `result.imported[]` 里的删除记录 | 文件里**消失**的**非内置**处理器会以 `{"kind": "processor-purge", "removed": <个数>}` 出现（这一项**没有** `path`） |
| `result.errors[]` | **字符串数组**（不是对象数组），失败原因与 `GET /api/config` 的 `errors[].message` 同源 |
| 顶层的 `errors[]` | 与 `GET /api/config` 同一字段、同一语义：**最近一次成功导入之后**的错误，导入成功后自动清空 |

- 典型场景：你在宿主机上用编辑器改完 `libraries.yaml`，不想等 5 秒

#### 5.8.3 `POST /api/config/export` — 强制把数据库写出去

- **响应 `200`**：同样是"与 `GET /api/config` 相同的快照 **+ 一个额外键**"，额外键叫 **`written`**，值是**写出的绝对路径列表**（**没有 `exported`**）：

```json
{"config_dir": "/data/config", "files": [ … ],
 "last_import_at": "2026-10-13 02:31:07", "last_export_at": "2026-10-13 02:33:10",
 "errors": [], "watcher_running": true, "interval_seconds": 5.0,
 "written": ["/data/config/libraries.yaml", "/data/config/processors/bangumi.yaml",
             "/data/config/processors/movie.yaml", "/data/config/processors/hardlink_only.yaml"]}
```

- **响应 `400`**：一份都没写出来时**不是返回空数组**，而是 `400`，`detail` 为「没有可导出的配置（处理器与媒体库都是空的），或目录不可写：`/data/config`」
- 典型场景：刚从备份恢复了数据库、或者想把当前规则**覆盖回**磁盘（撤销一次误改的文件）

> ⚠️ **冲突处理（谁赢）**：**导入方向是"文件胜出"** —— 一个被外部改过的文件一旦被导入，它会**覆盖数据库里的对应规则**。所以：
> - 在网页上改完规则，**别再用旧文件覆盖它**（要么不动文件，要么先 export）；
> - 手工改文件时，改完等 5 秒（或手动 reload），让 DB 认账；
> - 看不出谁赢了，就看 `GET /api/config` 的 `in_sync` 与 `last_import_at` / `last_export_at`；
> - 删除语义：文件里**消失**的非内置处理器/媒体库会随导入从 DB 删除；**内置处理器永不删除**，只允许重置。

---

### 5.9 待办接口（6 条）

> 对应 [`docs/contracts/ARCHITECTURE.md`](ARCHITECTURE.md) 的「§6.10 `app/todos.py`」。看板是**全局一份**的台账：任务跑完时把"值得你知道、但不该打断任务"的情况记一条，首页仪表盘上展示，看完打钩即可。**不弹窗、不阻断、不自动重试任何东西。**

**四种状态语义**

| # | 情形 | 结果 |
|---|---|---|
| 1 | 同一 `(kind, dedupe_key)` 已有 `open`，又发生一次 | **只累加**：`occurrences += 1`、`last_seen_at` 与"最近一次"的上下文（任务号 / 任务类型 / `detail`）刷新成本次的；`first_seen_at` **永不变**，**不新增条目** |
| 2 | 该 `(kind, dedupe_key)` 曾被打钩（`done`）后**再次发生** | **新插一条 `open`**；历史那条 `done` 一个字节都不动。于是"出现过几次、每次什么时候"天然留在表里 |
| 3 | 打钩（`resolve`） | 只把 `status` 改成 `done` 并记 `resolved_at`，**绝不删数据**；`reopen` 可随时改回 `open`（清空 `resolved_at`） |
| 4 | 列表排序 | 未完成在前，其次 `last_seen_at` 倒序，最后 `id` 倒序（分页稳定） |

#### 5.9.1 待办对象（Todo）

`GET /api/todos` 的 `items[]`、以及两个 `POST` 的响应体，都是下面这个形状（**裸对象，不套信封**）：

```json
{
  "id": 12,
  "kind": "link_failed",
  "kind_title": "硬链接创建失败",
  "kind_description": "源文件没能在媒体库里建出硬链接（跨文件系统、权限、目标已是别的 inode 等）。",
  "dedupe_key": "/vol1/1000/Library/动画/Show/Show S01E01.mkv",
  "status": "open",
  "is_open": true,
  "severity": "error",
  "title": "/vol1/1000/Library/动画/Show/Show S01E01.mkv",
  "detail": "源路径：/vol1/1000/Download/qbittorrent/Show/Show S01E01.mkv\n目标路径：…\n错误：EPERM",
  "library_id": 2,
  "library_name": "动画",
  "task_id": 87,
  "task_kind": "rename_hardlink",
  "occurrences": 3,
  "first_seen_at": "2026-10-14 09:12:03",
  "last_seen_at": "2026-10-14 21:40:11",
  "resolved_at": null,
  "created_at": "2026-10-14 09:12:03",
  "updated_at": "2026-10-14 21:40:11"
}
```

| 字段 | 含义 |
|---|---|
| `kind` | 六种之一：`link_failed` / `rename_suffix` / `unresolved_episode` / `unscoped_works`（KI-1：作品目录里没有可接受的「季目录」） / `source_integrity` / `task_failed`（手工塞进库里的未知 kind 也照样列出来，不会"看不见"） |
| `kind_title` / `kind_description` | 由服务端的 `KNOWN_KINDS` 就地补上（**响应里就有**，前端不需要自己维护一份文案表） |
| `severity` | `info` / `warning` / `error`（前端挑徽标颜色用；顺序也是"越靠后越严重"） |
| `title` | **标识符**（路径 / 目标名 / 库名），解释性文案在 `kind_title` 里 |
| `detail` | **原始上下文**（路径、错误文本；多条用换行分隔），最长 4000 字符，超出会截断并注明 |
| `dedupe_key` | 身份键；同一 `(kind, dedupe_key)` 的重复发生不会新增条目（见上表语义 1） |
| `library_id` / `library_name` / `task_id` / `task_kind` | 来源（**库名是"记这条时的快照"**：库后来改名或删除，历史待办照样显示当时的名字）。`task_kind` 取自产生它的任务的 `kind`（`hardlink` / `rename` / `rename_hardlink`） |
| `occurrences` | 这条条目被**观察到**的次数（从 1 开始，语义 1 时递增） |
| `first_seen_at` / `last_seen_at` / `resolved_at` / `created_at` / `updated_at` | 时间戳一律走 §1.4 的**本地时间**格式（库里存 UTC，响应已转换） |

> 📌 `library_id` / `task_id` **不是外键**：任务历史会被 `max_history` 裁剪、媒体库可以被删除，但待办是**用户自己看过的账**，不该被别人的清理动作带走。所以它们只是"当时的编号"。
> 📌 打钩**没有** `DELETE` 端点（**刻意**）：删掉一条就再也说不清"这事发生过几次、上次什么时候"。

#### 5.9.2 `GET /api/todos` — 待办列表

- **认证**：需要
- **查询参数**

| 参数 | 默认 | 说明 |
|---|---|---|
| `status` | `open` | `open` / `done` / `all`（`all` = 不过滤）。**非法值 ⇒ `400`** |
| `kind` | 不过滤 | 六种 kind 之一；**非法值 ⇒ `400`**（`detail` 里列出全部可选值） |
| `library_id` | 不过滤 | 整数 |
| `q` | 不过滤 | 按作品筛选。前后空白 `strip`，空/纯空白 = 不过滤；否则按**子串**（ASCII 大小写不敏感，`%` / `_` / `\` 按**字面**处理，与 §3.10 的 `q` 同一套转义）匹配 `title` / `detail` / `dedupe_key` **三列**（OR）。它是**朴素子串**、不是"按作品精确归属"：`todos` 是全局一份台账、表里没有 `folder` 列，作品名只出现在摘要、详情里的目录/路径和去重键里，所以同一段文字出现在别的条目里也会命中（见 [`KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-10） |
| `limit` / `offset` | `50` / `0` | 与 `/api/tasks` 同一套收敛规则（§1.2）：`limit` 收敛到 **1–200**，非法值按 `50`；`offset` 负数按 `0` |

- **响应 `200`**：信封格式（`total` 是**过滤后**总数，不是全库条数）

```json
{"items": [ /* Todo 对象，见 §5.9.1 */ ], "total": 3, "limit": 50, "offset": 0}
```

- **响应 `400`**：`{"detail": "未知的待办状态：OPEN。可选值：open、done、all。"}`

#### 5.9.3 `GET /api/todos/summary` — 计数（徽标 + 筛选下拉框的数据源）

- **认证**：需要
- **响应 `200`**：裸对象

```json
{
  "open": 4,
  "done": 9,
  "total": 13,
  "by_status": {"open": 4, "done": 9},
  "by_kind": [
    {"kind": "link_failed", "title": "硬链接创建失败", "severity": "error", "open": 3, "done": 1, "total": 4}
  ],
  "libraries": [
    {"library_id": 2, "library_name": "动画", "open": 4, "done": 9, "total": 13}
  ],
  "enabled_kinds": ["link_failed", "rename_suffix", "unresolved_episode", "source_integrity", "task_failed"],
  "disabled_kinds": [],
  "detectable_kinds": ["link_failed", "rename_suffix", "unresolved_episode"],
  "undetectable_kinds": ["unscoped_works", "source_integrity", "task_failed"]
}
```

> 📌 `by_kind` **把五种 kind 全列出来**（没有条目的计数为 0；`name_collisions` 已撤下——它和 `rename_suffix` 记的是同一次重命名），所以筛选下拉框不需要前端自己拼；库里存在但注册表没登记的 kind 也会追加在末尾（`title` 就是 kind 本身）。
> 📌 `disabled_kinds` 是被设置项 `todo_kinds` 关掉的种类（见 §5.2）。
> 📌 `detectable_kinds` / `undetectable_kinds` 是「一键检测」（§5.9.6）能不能自动核对的分组 —— 前端应该把 `undetectable_kinds` 如实告诉用户（"这几种只能人工看"），别让人以为"没检测出来 = 没问题"。
> 📌 **没有"每类最多 50 条明细"的上限**：一次全量扫描产生的明细会**全部**落库（§5.9.1）；前端看板默认 20 条一页。

#### 5.9.4 `POST /api/todos/{id}/resolve` — 打钩（`open` → `done`）

- **认证**：需要；**无请求体**
- **响应 `200`**：改后的 Todo 对象（§5.9.1）
- **响应 `404`**：`{"detail": "待办 12 不存在。"}`
- **幂等**：已经是 `done` 的条目**不重复写 `resolved_at`**（反复点不会把"什么时候真正打完钩"越改越新）
- **不删任何数据**；随时可以 `reopen`

#### 5.9.5 `POST /api/todos/{id}/reopen` — 恢复未完成（`done` → `open`）

- **认证**：需要；**无请求体**
- **响应 `200`**：改后的 Todo 对象（`status` = `open`、`resolved_at` = `null`）
- **响应 `404`**：`{"detail": "待办 12 不存在。"}`
- **语义**：只是把这一条恢复；**不会**因此删掉它、也不会影响其它条目。恢复后如果同一个 `(kind, dedupe_key)` 再次发生，会按语义 1 在这条上累加（而不是再开一条）

#### 5.9.6 `GET /api/todos/detect` — 一键检测：哪些待办其实已经不用管了

- **认证**：需要
- **查询参数**：`kind`（可选）、`library_id`（可选）、`limit`（默认 200、上限 500）、`offset`（默认 0）
- **能自动核对的种类**：`link_failed`（**目标文件已存在** ⇒ 那条链接其实补上了）、`rename_suffix` / `unresolved_episode`（**记录里的最终文件已不在那个路径上** ⇒ 文件被改名/挪走/清掉了，这条不用再处理）。
- **不能自动核对的种类**：`task_failed`、`source_integrity` —— 它们描述的是「一次任务」或「整棵树」的状态，**没有单个文件路径**可核对，只能人工判断。
- **⚠️ 这个接口是纯只读的**：它**绝不**改动任何待办的 `status`。返回的 `candidates` 只是"建议"，要用户在弹窗里勾选、再由 §5.9.7 落笔。
- **响应 `200`**：

```json
{
  "checked": 12,
  "found": 2,
  "candidates": [
    {"id": 31, "kind": "rename_suffix", "title": "Season 1 | Show S01E05 (2).mkv",
     "severity": "warning", "status": "open", "library_id": 2, "library_name": "动画",
     "detect_evidence": "记录里的最终文件名已不存在：Show A/Season 1/Show S01E05 (2).mkv",
     "detail": "目录：Season 1\n…"}
  ],
  "detectable_kinds": ["link_failed", "rename_suffix", "unresolved_episode"],
  "undetectable_kinds": ["unscoped_works", "source_integrity", "task_failed"]
}
```

> 📌 `checked` = **这一轮实际核对过的条数**（受 `limit`/`offset` 与 `kind`/`library_id` 筛选影响），不是总数。
> `detect_evidence` 是**人话证据**（「目标文件已存在」/「记录里的最终文件名已不存在：<相对路径>」）—— 前端必须把它显示在弹窗里，用户才敢按下「标记完成」。
> 路径匹配按**记录里那条相对路径的尾巴**做（样本里的 `dir` 有时只写 `Season 1`、不含作品名），所以前缀缺作品名也不会被误判成"文件没了"。

#### 5.9.7 `POST /api/todos/detect-resolve` — 确认后批量打钩

- **认证**：需要
- **请求体**：`{"ids": [31, 32]}`
- **响应 `200`**：

```json
{"requested": 2, "resolved": 2, "skipped": [], "missing": []}
```

- **打钩前会再核对一遍**（弹窗可能已经开了很久）：实况变了 ⇒ **不误标**，进 `skipped[]`，`reason` 里说明原因（如「文件已回到记录路径，不再支持自动打钩」）。
- `ids` 里已经不存在的条目 ⇒ 进 `missing[]`，不算失败。
- **空 `ids` 合法**（`{"ids": []}` ⇒ `resolved: 0`）。
- **语义与 §5.9.4 完全一致**：只改 `status` / `resolved_at`，**一行都不删**，随时可以 `reopen`。

---

## 6. 回调接口（qBittorrent 用）

回调接口给 qBittorrent 的「运行外部程序」调用，或者给你的自动脚本用。

> ⚠️ **参数只从 URL 查询串或 urlencoded 表单体（`application/x-www-form-urlencoded`）读取，JSON 请求体被静默忽略**（见 [ARCHITECTURE.md](ARCHITECTURE.md) 的「§5.0 跨接口约定」第 9 条）。qBittorrent 的「运行外部程序」用 `curl --data-urlencode`，正好就是 urlencoded 表单，天然匹配。
>
> 但如果你把命令改成 `-H 'Content-Type: application/json' -d '{"name":"...","category":"..."}'`，你会得到 `torrent.name` 为空、**命中 0 个库**，而且**不报错**（token 对、HTTP 200，只是 `matched:0`）—— 这是最难查的一种静默失败。**查询串的优先级高于表单体**（同名参数以 URL 里的为准）。

**认证方式**：不用登录 Cookie，但要带 **hook token**。token 有**三种传法**，等价：

| # | 传法 | 示例 |
|---|---|---|
| 1 | URL 查询参数 | `?token=<HOOK_TOKEN>` |
| 2 | 请求头 Bearer | `-H "Authorization: Bearer <HOOK_TOKEN>"` |
| 3 | 表单字段 | `--data-urlencode "token=<HOOK_TOKEN>"` |

> token 从哪来：登录 WebUI → **设置 → 回调 / Hook**，页面上有 token 与**完整回调命令**，直接复制粘贴即可（见 [`docs/guides/USER_GUIDE.md`](../guides/USER_GUIDE.md)）。也可以 `GET /api/settings` 读 `hook_token` 字段（§5.2）。
> token 的取用顺序是 **① URL 查询参数 → ② 表单字段 → ③ `Authorization: Bearer`**（前一个找不到才看下一个）。
> token 错误或缺失 → **401**：

```json
{"detail": "回调 token 不正确。请在「设置」页复制完整的回调命令（含 token=...），或重新生成 token 后更新 qBittorrent 的「运行外部程序」。"}
```

### 6.1 `POST /api/hooks/qbittorrent` — qBittorrent 下载完成回调

- **认证**：hook token（三种传法任选）
- **参数**（查询参数或表单参数均可）：

| 参数 | qBittorrent 占位符 | 说明 |
|---|---|---|
| `name` | `%N` | 种子名称 |
| `hash` | `%I` | 种子哈希 |
| `category` | `%L` | 分类 |
| `tags` | `%G` | 标签（逗号分隔） |
| `content_path` | `%F` | 内容路径（**与容器内 `/vol1/1000/Download/qbittorrent/...` 同路径，零转换**） |
| `save_path` | `%D` | 保存路径 |
| `root_path` | `%R` | 根路径 |
| `kind` | **无**（`%K` 不是它） | **可选的高级覆盖项**，手工传才生效（取值见下）；标准命令不传。注意 qBittorrent 的 `%K` 是 **Torrent ID**，不要拿它当任务类型 |

- **响应 `200`**（命中 1 个库）：

```json
{
  "matched": 1,
  "tasks": [42],
  "ignored": ["电影", "国漫"],
  "ignored_details": [
    {"library_id": 2, "name": "电影", "reason": "分类 Movie 不在该库的分类列表里。"},
    {"library_id": 3, "name": "国漫", "reason": "回调带的标签与该库的标签条件没有交集。"}
  ],
  "warnings": [],
  "torrent": {"name": "Show A S01E01", "hash": "8f2c0a…"}
}
```

| 字段 | 说明 |
|---|---|
| `matched` | 命中的媒体库数量（= `tasks` 的长度） |
| `tasks` | 因此新建（入队）的任务 id 列表 |
| `ignored` | 未被匹配的库名 |
| `ignored_details` | **每个未命中的库 + 具体中文原因**（排查「为什么没触发」看这里，比 `ignored` 详细得多） |
| `warnings` | 非致命提示。例如 `content_path` 切不出一级文件夹（单文件种子）时会说明「本次按全量执行」；当前没有任何已启用库时也会提示 |
| `torrent` | 回显 `name` / `hash`，便于和 qBittorrent 侧对账 |

- **匹配逻辑**：遍历**已启用**的库（按 id 升序）逐个判断。**所有「已填写」的条件都必须成立（AND）**：
  1. 填了 `match_categories` → 回调的 `category` 必须**等于**列表里的一项；
  2. 填了 `match_tags` → 回调的 `tags` 必须与它**有交集**（比较统一转小写；`%G` 的分隔符不稳定，逗号、中文逗号、顿号、分号、空白都当分隔符）；
  3. 填了 `match_path_prefixes` → `content_path` 必须落在其中某个前缀之下（前缀写宿主路径也会先换算成容器路径再比）。

  **三个列表全空 ⇒ 视为不匹配**（避免误抓整个下载目录）。命中多个库 → 全部入队。

- **`kind` 可被显式覆盖（标准命令里一律不传）**：回调带 `kind=hardlink` / `link` / `rename` / `rename_hardlink`（兼容 `link+rename`）时按它执行；不带或值不认识时，按库的处理器推导（`bangumi` / `movie` → `rename_hardlink`，`hardlink_only` → `hardlink`）。标准回调命令的权威定义见 [ARCHITECTURE.md](ARCHITECTURE.md) 的「§8 qBittorrent 回调」一节。

- **响应 `200`（一个都没匹配）**：

```json
{
  "matched": 0,
  "tasks": [],
  "ignored": ["番剧", "电影"],
  "ignored_details": [
    {"library_id": 1, "name": "番剧", "reason": "content_path /vol1/1000/Download/qbittorrent/Movie/x 不在该库的任何路径前缀之下。"}
  ],
  "warnings": [],
  "torrent": {"name": "Some Movie 2024", "hash": "1a2b3c…"}
}
```

> 💡 `matched: 0` 不是错误 —— 说明这个种子没命中任何库的匹配条件。先看 `ignored_details[].reason` 的中文原因，再回去调整库的 `match_categories` / `match_tags` / `match_path_prefixes`。

### 6.2 `POST /api/hooks/scan` — 立即触发一次目录扫描

- **认证**：hook token（同上）
- **请求体**：无
- **行为**：立即扫描一次所有已启用库的 `source_dir`，对「未记录或体积变化」的一级文件夹建任务。**扫描只创建任务，不做任何文件操作。**
- **响应 `200`**（**对象，不是裸数组**；字段与 §6.1 一致）：

```json
{"matched": 2, "tasks": [43, 44], "ignored": [], "ignored_details": [], "warnings": []}
```

> 这个接口不管 `scan_enabled` 是否为 `true` —— 手动调用总是执行一次。手动扫描没有"匹配条件"这一步，所以 `ignored` 恒为空数组、也没有 `torrent` 回显。

---

## 7. SSE 实时流事件格式

`GET /api/tasks/{id}/stream` **需要登录**（浏览器 / `curl` 会带上 Cookie）。这是**按任务**的流（URL 里的 `{id}` 是任务 id），不是全局任务流。

连接建立后服务端的行为：

1. 立刻补发一条 `event: task`（该任务当前状态）+ 该任务**已有日志的尾部**（最多 500 条 `event: log`）；
2. 之后实时推送新事件；空闲时每 **≥15 秒**发一行注释当心跳：`: keepalive`（`:` 开头是 SSE 注释，客户端忽略即可，用来防止中间代理掐连接）；
3. 任务进入终态（`success` / `failed` / `skipped` / `cancelled`）时，把剩余日志补完，再发一条 `event: done`，然后**服务端主动结束**本次连接。

**事件 1：任务状态变更**

```
event: task
data: {"id":42,"library_id":1,"library_name":"番剧","kind":"rename_hardlink","trigger":"hook","status":"running","created_at":"2026-10-09 01:20:11","started_at":"2026-10-09 01:20:11","finished_at":null}

```

**事件 2：新日志行**

```
event: log
data: {"task_id":42,"id":103,"ts":"2026-10-09 09:20:12","level":"INFO","message":"已创建硬链接: /vol1/1000/Library/Bangumi/Show A/Season 1/Show A S01E01.mkv","stream":"stdout"}

```

**事件 3：任务结束（服务端随后关闭连接）**

```
event: done
data: {"task_id":42,"status":"success"}

```

要点：

- 每个事件以**空行**结尾；`event:` 行给出类型（`task` / `log` / `done`），`data:` 行是**单行** JSON；
- 响应头包含 `Cache-Control: no-cache`、`Connection: keep-alive`、`X-Accel-Buffering: no`（最后一条是给 Nginx 看的：别缓冲这个响应）；
- **前端默认用轮询**（`GET /api/tasks/{id}/logs?after_id=`，每 1.5 秒），SSE 是可选的增强通道。所以 SSE 掉线**不影响功能**；
- 观察用（注意 URL 里的任务 id，`-N` 关闭 curl 的输出缓冲）：

```bash
curl -N -b cookies.txt http://$NAS_IP:18081/api/tasks/42/stream
```

---

## 8. curl 快速上手

完整可跑的流程脚本见仓库的 `work/scripts/`（`README.md`、`acceptance-test.py`、`run-container.sh` 等）。这里只留**调用形状**，逐步说明与「每一步期望看到什么」见 [`docs/guides/USER_GUIDE.md`](../guides/USER_GUIDE.md) 与 `work/scripts/README.md`。

约定：`$NAS_IP` 是你的 NAS 地址，`18081` 是 `PORT` 默认值；除步骤 0 外都要带 `-b cookies.txt`（上一步登录存下的 `mlm_session`）。

| # | 目的 | 请求 |
|---|---|---|
| 0 | 确认服务活着（**免登录**） | `GET /api/system/health` |
| 1 | 登录并保存 Cookie | `POST /api/auth/login`（`{"username","password"}`）→ 期望 Cookie 里有 `mlm_session`；再用 `GET /api/auth/me` 验证 |
| 2 | 看现有媒体库 / 处理器 / 配置同步状态 | `GET /api/libraries`、`GET /api/processors`、`GET /api/config` |
| 3 | 新建媒体库 | `POST /api/libraries`（`name` / `type` / `processor_id` / `enabled` / `source_dir` / `target_dir` / `match_*` / `orphan_cleanup`），记下响应里的 `id` |
| 4 | 预检（强烈建议） | `POST /api/libraries/{id}/validate`；`ok: false` 时先解决（最常见是跨文件系统） |
| 5 | 跑一次任务 | `POST /api/libraries/{id}/run`，body 形如 `{"scope":{"mode":"full"},"force":false}`；只处理一个一级文件夹用 `{"scope":{"mode":"folder","folder":"Show A"},"force":false}` |
| 6 | 查作品账本、重做单个作品 | `GET /api/libraries/{id}/works`；重做走 `POST …/works/rename`（只重命名不建链）或 `POST …/works/delete`（先 `dry_run:true` 预演，再 `dry_run:false,"confirm":true` 执行）。⚠️ 删除**只动目标目录**，源目录一个字节都不动 |
| 7 | 查任务与日志 | `GET /api/tasks?limit=10`、`GET /api/tasks/{id}`、`GET /api/tasks/{id}/logs?after_id=0&limit=500` |
| 8 | 模拟一次回调（验证回调链路） | `POST /api/hooks/qbittorrent?token=<HOOK_TOKEN>`，用 `--data-urlencode` 传 `name` / `hash` / `category` / `tags` / `content_path` / `save_path` / `root_path`（**不传 `kind`**）。`matched: 0` 就看 `ignored_details[].reason` |
| 9 | 改设置 | `PUT /api/settings`，值要写成**真类型**（`true` 不是 `"true"`，`600` 不是 `"600"`），否则 `400` |
| 10 | 改规则 / 配置同步 | `GET /api/processors/{id}` → `POST /api/processors/validate`（只校验）→ `PUT /api/processors/{id}`（全量替换）→ `POST /api/processors/{id}/reset`（`{"preset":"legacy"}` 或 `default`）；配置侧 `GET /api/config`、`POST /api/config/reload?force=true`、`POST /api/config/export` |

## 下一步看哪篇

- 我要**装起来**（含卷挂载为什么不这么配就不工作）→ [`docs/guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md)
- 我要**在网页上操作**（不理解某个字段填什么）→ [`docs/guides/USER_GUIDE.md`](../guides/USER_GUIDE.md)
- 我**遇到问题**了（任务失败、没触发、跨盘…）→ [`docs/guides/FAQ.md`](../guides/FAQ.md)、[`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md)
- 我想知道**为什么必须是这个行为** → [`docs/contracts/BASELINE.md`](BASELINE.md)、[`docs/contracts/ARCHITECTURE.md`](ARCHITECTURE.md)
