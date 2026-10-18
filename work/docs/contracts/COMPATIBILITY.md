# 兼容性说明（COMPATIBILITY）

> 这篇文档回答「**哪些环境/文件系统/命名方式是被支持的，哪些不是**」。
> **§2 是"同一挂载"这条前提在全仓的唯一权威** —— 其它文档只引用、不复述论证。
>
> 冻结的行为基线见 [`BASELINE.md`](BASELINE.md)（§4 是 B1–B13、§5 是 L1–L9）；处理器的可配置面见
> [`PROCESSORS.md`](PROCESSORS.md)；测试 ↔ 契约条目的映射见 [`docs/meta/TEST-MAP.md`](../meta/TEST-MAP.md)；
> 出具体报错时查 [`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md)。

**一句话前提**：本应用是「**在 Linux 容器里、对同一个挂载（mount）内的本地文件、按固定的规则语义处理
你的下载目录**」的封装 —— **硬链接**由 `app/processors/hardlink.py` 执行，**重命名**由配置驱动的规则
引擎 `app/processors/engine.py` 执行。凡是不满足这条前提的场景，都不在支持范围内。

> **路径写法**：`<MEDIA_ROOT>` = compose 的 `${MEDIA_ROOT:-/vol1/1000}`（容器路径 = 宿主路径）；
> `<LIBRARY_ROOT>` = 媒体库的 `target_dir`；`<HOST_IP>` = 部署机地址。都是**可替换占位**，不是固定事实。

---

## 1. 支持矩阵

| 维度 | 支持 | 不支持 / 未验证 | 依据 |
|---|---|---|---|
| 运行形态 | Docker 容器（`docker compose up -d`） | 直接在宿主机跑 Python、Windows 服务、群晖套件 | 源与目标必须在**同一个挂载**内（§2） |
| 宿主 OS | Linux（Debian 系，例如飞牛 fnOS） | Windows / macOS 作为**运行**宿主 | [`docs/guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md) |
| CPU 架构 | `linux/amd64`、`linux/arm64`（基座 `python:3.11-slim` 为 multi-arch） | 无 | `docker/Dockerfile` 的 `FROM` 与 multi-arch 注释 |
| Python | 3.11（容器内固定） | 其他版本**未验证** | `docker/Dockerfile` |
| 运行依赖 | 只有 `fastapi==0.115.6`、`uvicorn[standard]==0.32.1`、`PyYAML==6.0.3` | 任意额外依赖 | `docker/Dockerfile` |
| 应用端口 | 单实例、`uvicorn --workers 1`（**架构要求**） | 多 worker / 多副本 / 多实例共享同一数据卷 | `BASELINE.md` B11；日志"读-改-写整份"（L1） |
| 队列并发 | 全局单线程串行 | 任何形式的并发执行 | `BASELINE.md` B11 |
| 配置格式 | YAML 1.1（`/data/config/libraries.yaml`、`/data/config/processors/<id>.yaml`，首行 `schema_version: 2`） | 手写 JSON 配置（`config.json` 不是真源） | [`PROCESSORS.md`](PROCESSORS.md) §6.1 |
| 数据库 | SQLite（WAL，10 张表） | 外部数据库 | [`ARCHITECTURE.md`](ARCHITECTURE.md) §3 |
| 文件系统 | ext4 / btrfs / xfs 等**同一挂载**内的 POSIX 文件系统（设计上支持，见 §2） | 跨挂载、网络/联合文件系统 | §2 |
| 开发/测试环境 | 任意能跑 Python 的平台（测试套件不依赖真实挂载） | 在非 Linux 上**真跑任务**（容器路径不是真实挂载点） | §6 |

---

## 2. 文件系统兼容性（**最关键的一节**，全仓唯一权威）

硬链接要求**源与目标在同一个挂载（mount）内**，且该文件系统**支持硬链接**。

> **这是最关键的一处前提。** Linux 的 `linkat(2)` 由内核 `do_linkat()` 判定：它比较的是**挂载身份**
> （`old_path.mnt != new_path.mnt` ⇒ `EXDEV`），**不是** `st_dev`。所以"两个目录在同一块盘、
> `stat -c '%d'` 相同"**并不保证** `link()` 能成功 —— 只要它们是容器里的**两条独立 bind 挂载**
> （例如 `/downloads:ro` + `/library`），硬链接就永远建不出来。
> 正确形态是**单一共同父挂载**（见 [`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md)
> 场景 1、场景 21）。
> 判断"是否同一个挂载"用 `findmnt -T <目录> -o TARGET`（两条 `TARGET` 相同才算），**不要**用 `st_dev`。
> 反过来，`st_dev` 相同**也不能**预测 `link()` 成功 —— 这是"预检假绿"的根因（`BASELINE.md` B8）。

| 文件系统 / 形态 | 硬链接 | 结论 |
|---|---|---|
| ext4 / btrfs / xfs（主机本地存储池，同一挂载内） | ✅ 设计上支持 | **推荐**场景；前提是源与目标在**同一挂载**内，需在你的环境上按 §8 第 1、2 项确认 |
| ZFS（同一 dataset 且同一挂载内） | ⚠️ 设计上支持、未验证 | 跨 dataset 通常是**跨挂载** ⇒ EXDEV |
| mdadm/LVM 拼出来的同一卷、同一挂载内 | ✅ 设计上支持 | ⚠️ 不要用 `stat -c '%d'` 判断 —— `st_dev` 相同但分属两条 bind 时 `link()` 依然 `EXDEV` |
| NTFS（Windows 本地盘） | ⚠️ 单卷内可用 | **不是**目标环境；仅用于跑测试，`st_ino`/`st_dev` 语义与 Linux 不同 |
| exFAT / FAT32（U 盘、部分外接盘） | ❌ | 根本无法建硬链接 ⇒ 任务必然大面积失败 |
| SMB / CIFS 网络共享 | ❌ 按不支持处理 | 多数实现不支持硬链接；即使支持，`st_dev`/`st_ino` 语义也不可靠 |
| NFS | ❌ 按不支持处理 | 同上；且实证探针（只读源目录、临时文件写目标侧）可能被服务端拒绝 ⇒ 结论是 `null`（无法实证），**不是** `true` |
| FUSE / 云盘挂载 / mergerfs / unionfs | ❌ / 未验证 | 硬链接语义通常不成立；**不支持** |
| 源与目标被挂成**两条独立 bind**（最常见，哪怕同一块盘） | ❌ | 内核按挂载身份判定 ⇒ 必然 EXDEV |
| 源与目标分属两个存储池 / 两个卷（哪怕同一台宿主机） | ❌ | 同上（跨挂载）；典型 EXDEV 场景 |
| 大小写敏感 | — | 应用不做大小写折叠；扩展名白名单是**大小写敏感**的（L7，建链实现与规则引擎一致） |
| 文件名编码 | UTF-8 | 容器固定 `LANG=C.UTF-8`/`PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`（B9）；非 UTF-8 文件名**未验证** |

**为什么"跨卷/跨挂载复制"不是替代方案**：复制会让媒体库与下载**各占一份空间**，并且从此两边不再是
"同一个文件"（改一边不会改另一边），违背了本项目的核心前提（见 [`docs/guides/FAQ.md`](../guides/FAQ.md)）。
应用**故意不提供**复制回退（**B8**），并在调用建链实现后用 `(st_dev, st_ino)` 逐文件核对产物**确实是
硬链接**，否则记进 `link_failed` 并报「非硬链接」；源目录一侧另有**任务前后逐文件源完整性核对**
（`Task.result.source_integrity`，**B5**）。

---

## 3. 已固定下来的行为（黄金值）

执行体只有两处：**重命名**由规则引擎 `app/processors/engine.py`，**硬链接**由
`app/processors/hardlink.py`。应用只做自己的事、不依赖任何外部程序，因此镜像**自包含**。

这些行为由**项目自己的回归测试**以**冻结的黄金值**守护。逐条映射（哪个契约点由哪个测试函数断言、
结论是什么）**统一登记在 [`docs/meta/TEST-MAP.md`](../meta/TEST-MAP.md)**；实际运行记录与历史采集
摘要登记在 [`docs/meta/EVIDENCE.md`](../meta/EVIDENCE.md)。本节只保留**用户会直接撞到的预期结果**。

### 3.1 命名结果对照（这些是**预期结果**，不是 bug）

一级文件夹 `Show A`、季文件夹 `Season 1` 下（`preset=legacy` 是最朴素的旧口径，与冻结黄金值一致；
`default` 认不出集数时**保留原名** —— 下表是 **`legacy`** 的结果）：

| 原文件名 | 结果 |
|---|---|
| `[Lilith-Raws] Show - 01.mkv` | `Show A S01E01.mkv` |
| `Show.S01E01.chs.ass` | `Show A S01E01.chs.ass` |
| `Show 12.mp4` | `Show A S01E12.mp4` |
| `Show.EP03.mkv` / `Show.E05.mkv` / `Show.#07.mkv` / `Show.第05集.mkv` | `…S01E03/E05/E07/E05…` |
| `Show [09] x 第05集.mkv` | `…S01E09…`（**按最早出现位置**，`[09]` 在前） |
| `Show 第05集 x [09].mkv` | `…S01E05…` |
| `Show.01.ass` | `Show A S01ENone.01.ass`（`.01` 不是被识别的集数标记 ⇒ 集数为 `None`，后缀原样保留；`default` 保留原名） |
| `plain.ass` / `a.b.c.ass` / `x.01.sc.ass` / `no number here.mkv` | `… S01ENone…`（同上） |
| `UPPER.MKV` | 不动（大小写敏感白名单） |

季/目录层面的兼容规则：

| 情形 | 行为 |
|---|---|
| `Season 1 (2018)`、`Season 01`、`Season1` | **不处理**（`fullmatch` 不通过，L5） |
| `Season 0` | **不处理**（`"00"` 跳过） |
| 季文件夹里的**嵌套目录**（如 `Season 1/Subs/`） | 里面的文件**永不重命名**（L6） |
| 作品根目录下的散落文件（`Show A/loose.mkv`） | **不重命名**（只处理季文件夹的直接子文件） |
| 一级文件夹命中 `default` 的特典类拒绝规则（`Scans`/`CD`/`SP`…） | **整棵子树都不处理**，并留一条 warning（有意增量，`BASELINE.md` B4） |
| 文件名/目录名里含 `" - "` 的**一级文件夹** | 日志行被静默忽略 ⇒ 每次都当"未记录"重跑并刷 `File exists`（L2，数据不受损）。应用把"目标已是同一 inode 的硬链接"判成 `already_linked`，**不会因此判任务失败**；但**文件夹级**的体积跳过永远命中不了它，所以每轮仍会重新走一遍建链。这层改判**不依赖被截短的文本行**：建链实现把失败项结构化暴露（`hardlink.last_failures()`），文本切分按 `[Errno`/`[WinError` 锚点 ⇒ 含 `" - "` 的文件夹同样正确记进 `already_linked`（**stdout 与日志格式一个字都没改**） |
| 同后缀多文件（Movie） | 只改**体积最大**的那个，其余保持原名 |
| 体积相同的一级文件夹（追加了等体积文件） | 会被跳过（L8/L9）——**这是既有规则** |

> **迁移兼容性**：按 `preset=legacy` 配置，命名结果与目录层级就与"最朴素的旧口径"**完全一致**
> （判据是项目自己的冻结黄金值）。差别只在**日志位置与归属**（每个媒体库有自己的重命名日志，
> 路径由 `settings.library_log_path()` 决定，形如 `<data>/logs/<log_file>`）与多了任务/队列/网页/
> 处理器配置这层外壳。日志按库各自记账，**不需要任何"日志迁移"步骤**（`BASELINE.md` §4.1）。

### 3.2 契约点 → 测试的映射索引

完整的三向映射（契约点 / 测试函数 / 结论）**已迁至 [`docs/meta/TEST-MAP.md`](../meta/TEST-MAP.md)**，
按 `BASELINE.md` 的 §1 / §2–§3 / §4（B1–B13）/ §5（L1–L9）、`ARCHITECTURE.md` 的 §2 / §5 / §6、
`API.md` 的 §5 分组。本文件不再维护这张表，避免两处漂移。

举证方式不变：`app/processors/hardlink.py` 与 `app/processors/engine.py` 是唯一执行体，行为由
**冻结的黄金值**断言（`tests/test_hardlink_native.py` 的 A 组五条 + `tests/test_processor_engine.py` /
`tests/test_bangumi_episode_rules.py` / `tests/test_rename_extensions.py` /
`tests/test_processor_spec.py`）。

### 3.3 已登记的契约点（原"与文档不一致的偏差清单"）

这些条目当初是"文档说 X、实现是 Y"，现已**全部按实现收编为契约点**（测试一条都没改）。
逐条的测试依据见 `TEST-MAP.md`；这里只留**结论与权威处**，防止同一事实写两遍又漂移。

| 主题 | 现行契约（一句话） | 权威处 |
|---|---|---|
| `/validate` 响应形状 | **扁平对象、22 个字段，没有 `checks[]`**；`same_filesystem` 三态：`false` ⇒ `errors[]` + `ok=false`，`null` 或未实证的 `true` ⇒ 只进 `warnings[]`（**`warnings` 非空 ≠ 有问题**）；`permissions` 是权限预检（**不受 `?probe=` 控制**，`ok=false` 时并入外层 `errors[]`） | `API.md` §3.8；`ARCHITECTURE.md` §5.0 ⑧ |
| 静态前端缓存 | 非 `/api/*` 的 `GET`/`HEAD` 一律 `Cache-Control: no-store`：`StaticFiles` 的 `ETag`/`Last-Modified` 会诱发浏览器启发式缓存，部署新版本后普通刷新仍可能用旧 JS；`no-store` 才能"换镜像即生效" | `ARCHITECTURE.md` §5.0 ②′；`app/main.py` 的 HTTP 中间件 |
| 方法 | `/validate` 是 **POST**（GET → 404）；`/api/auth/password` 是 **POST**（PUT → 405） | `API.md` §5 |
| 删除媒体库的响应 | 多返回 `library_id` 与 `link_records_removed`（超集，只增不改） | `API.md` §5；`ARCHITECTURE.md` §5.0 ⑦ |
| 任务日志 item | 仍是 `{id, task_id, ts, level, message, stream}`（`task_id` 属超集） | `API.md` §5 |
| `Show.01.ass` 的 legacy 产物 | `Show A S01ENone.01.ass`（`.01` 是"最后一个点之后"的后缀，被保留） | §3.1；`BASELINE.md` §2.6 |
| 回调参数 | 只从**查询串或 urlencoded 表单**读取（JSON body 被静默忽略），查询串优先 | `API.md` §6；`ARCHITECTURE.md` §5.0 ⑨ |
| `permission_mode` | **默认关闭的显式开关**：chmod 只在显式开启时执行，且收窄到**目标侧**（库根 + 本次处理到的每个一级文件夹的目标子树，含被跳过的旧作品），符号链接不跟进 | `BASELINE.md` §1.6 与 **B13** |
| `GET /api/processors` 的顺序 | **内置优先**（`app/processors/defaults.py::BUILTIN_IDS`：`hardlink_only → bangumi → movie`），其余按 `id` 升序；最终顺序由 `app/processors/registry.py::list_processors()` 的显式 `sort_key` 决定 | `API.md` §5.7 |
| 处理器 spec 的 `log_file` | **非必填**：缺省 / `""` / `null` 都合法 = "请服务端生成"，由 `registry.next_log_file()` 填成 `<slug(处理器 id)>cache.log`；含 `/`、`\`、`..` 或非字符串才报错 | `PROCESSORS.md` §5 |
| 处理器响应体形状 | `GET /api/processors/{id}` 返回**规格本身**（`id/name/kind/builtin` 与 `traverse`/`naming` **同层**），**没有** `spec` 包一层；媒体库响应另有别名 `processor`（= `processor_id`，超集） | `API.md` §5.7 |
| 同一处理器下多个库的 `log_file` | **一个库一份日志**：建库不传 ⇒ 服务端分配唯一名（第一个 `bangumicache.log`、第二个 `bangumicache-2.log`）；显式名撞车 ⇒ **422** 字段级 `log_file` 并提示下一个可用名 | `PROCESSORS.md` §5 |
| `accept_dirs` 的 `season_group` | 允许显式 **`0`** = "只用这条正则判断接受、**不取季号**"（季号 `"00"`，与 `accept_dirs: []` 同一条分支）；缺省仍是 `1`；负数报错 | `PROCESSORS.md` §3.1 |
| 挂载身份 vs `st_dev` | 内核判据是**挂载身份**，`same_filesystem`/`probe` 是**三态 `bool | None`** + **只读安全**的实证探针；源目录保护是"代码层不写源 + 任务前后源完整性核对" | **§2**；`BASELINE.md` B5/B8 |

> ⚠️ **本测试套件不证明挂载行为**（那需要真实容器）：容器侧证据由部署侧提供
> （`docs/meta/EVIDENCE.md` 的 E 编号）。

### 3.4 测试运行方式

> **容器内验收用 `sh scripts/pytest-in-container.sh`** —— 镜像本身**不含 pytest、也不含 `tests/`**
> （`docker/Dockerfile` 只 COPY `app/` 与 `web/`）；该脚本把测试目录 `docker cp` 进一个一次性容器、
> 临时 `pip install pytest httpx`，跑完即删。**不要在容器里直接 `python -m pytest tests`**。
>
> 开发机上的运行方式、逐文件用例数、跳过项与历史采集记录见
> [`docs/meta/EVIDENCE.md`](../meta/EVIDENCE.md) 与 `docs/meta/TEST-MAP.md`。
> `tests/conftest.py` 同时兼容 pytest 与纯 stdlib 兜底 runner（`tests/run_all.py`）。

### 3.5 版本兼容性

**现行兼容性口径 —— 历史版本逐级升级的差异不再追溯，以下即今天的行为基线。**

**① `force=true` 只允许单作品。** `POST /api/libraries/{id}/run` 的 `force=true` 只允许配合
`scope.mode=="folder"`；**整库 force ⇒ 400**（报错原文：「整库强制重跑已停用：force 只允许配合
scope.mode='folder'（单个作品）使用。要重做作品，请到「作品记录」里删除对应作品的记录，下次运行时
这些作品会自动补做，其余作品不受影响。」）。⚠️ **唯一的破坏点**：整库 + `force=true` 的调用收到 400。
停用理由是整库 force 会让建链阶段把**已经改过名**的文件按**原名**再链一份（同一 inode 的第二个目录项，
见 [`KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-7）。

**② 作品清单接口的查询参数。** `GET /api/libraries/{id}/works` 的 `q` 是**可选**参数（作品名子串，
ASCII 大小写不敏感，`%`/`_`/`\` 按字面处理）；`limit` 夹在 `1..200`、默认 **50**；响应含回显 `q` 与
`ledger_total`（本库账本**总条数**），而 `total` = **命中 `q` 的条数**（只有不传 `q` 时两者才相等）。
⚠️ 把 `total` 当"本库作品总数"读的调用方请**改读 `ledger_total`**。`GET /api/todos` 的 `q` 同理
（按 `title`/`detail`/`dedupe_key` 朴素子串筛，**任何取值都不会 400**）。**WebUI 变化不算 API 变化**：
「作品记录」是独立页面（`#/works`、`#/works/<library_id>`，hash query `?q=&page=&limit=`，每页
25/50/100/200），它调用的仍是同一个 `GET /api/libraries/{id}/works`，**HTTP 契约一个字段都没变**。

**③ 账本与"重做某个作品"。** `link_records` 表由**启动时自动补建**（新装库走 `SCHEMA`、老库走
`migrate()` 的 `_ensure_link_records_table()`，**同一份 DDL 常量** `_LINK_RECORDS_COLUMNS`）——
**没有需要手工执行的迁移步骤、也不需要改任何配置**。账本是**作品级**的、**不保证文件级完整性**：
目标目录里被手动增删改的文件不会被发现、也**不会**被修复。**唯一的重做途径 = 在「作品记录」里删掉
该作品的记录**（连带删掉目标目录里整个作品文件夹，源目录不受影响），下次运行自动补做该作品。

**④ 不回填历史 `hard_links.log`。** 旧日志是**全局**的、不带库/目标维度（键 = 源目录 + 作品名 + 体积），
无法可靠还原成"某库某作品已处理" ⇒ 已有媒体库的账本**从空开始**，其**第一次全量运行**会把每个作品
重新建链；已在目标目录里改过名的文件会以**原名**再链一份，重命名阶段于是多出 `xxx (2).mkv` 后缀。
**不丢数据**（硬链接与源目录不受影响），规避办法见 [`KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-7。
建链与重命名的**结果本身不因账本判据改变**：账本只影响"哪些作品被跳过"，不影响"链出来的东西长什么样"；
`hard_links.log` **照写**（格式与解析契约未变），只是**不再用于跳过判断**（`BASELINE.md` B10）。

**⑤ 「坏名字」的处理与留痕。** 命名段序列里写了上下文没有的变量名 ⇒ 那一段**什么都不输出**、
文件名少一段、任务仍 `success`；留痕齐全（`result.unknown_params` + `samples.unknown_params` +
每个文件一条 WARNING + 一条 `warnings` 汇总 + 收尾摘要）。`episode_fallback` 为**空串或纯空白**与
`null` **等价**（= 没配兜底）⇒ 保留原文件名，不会渲染出 `… S01E00.mkv`（显式非空字符串含 `legacy` 的
`"None"` 则**行为不变**）。命名渲染出**空文件名** ⇒ 跳过该文件 + 中文告警。
「立即重命名」（`POST /api/libraries/{id}/works/rename`）而该库**目标目录不存在** ⇒ **提前 400**
并提示「请先运行一次本库（会自动创建目标目录并建立硬链接）」（该接口不建链、不创建目标根）。

**⑥ 保存期校验（`strict_names`，默认 `True`）。** API 保存处理器 / 保存库级覆盖 / 导入配置时，
段序列里的 `param` 与模板里的 `{名字}` 都必须在可用名单内，否则 **422**；**运行路径显式放宽**
（`engine.run_rename` 用 `strict_names=False`）—— 库里早先存下的老配置不会因为一次重跑而失败。
`episode_fallback` 的归一发生在**读规格时**（内存里），**不改写**库里已存的 JSON。

**⑦ 可观测项（不是失败）。** 待办看板含第六种待办 **`unscoped_works`**（扁平结构里无法归属到任何作品的
文件夹，见 `KNOWN_ISSUES.md` KI-1）；重跑整库后看板可能多出若干条。待办样本不受"只留前 100 条"的
上限截断。`RenameOutcome` 的若干字段只走内存与任务结果 JSON（`Task.result` 与日志表，**无列结构变化**）。

**⑧ 容器内路径。** entrypoint 与健康检查脚本安装到 `/usr/local/bin/mlm-entrypoint`、
`/usr/local/bin/mlm-healthcheck`；容器内应用根是 **`/srv/mlm`** —— **任何写死路径的地方都要与之一致**
（`docker exec` 排障命令、外部监控探针、自定义 cron 里的应用根路径与 `PYTHONPATH`）。
容器内路径与仓库目录的对照见 [`docs/meta/STRUCTURE.md`](../meta/STRUCTURE.md)。

---

## 4. 平台行为差异

应用**只在 Linux 容器里运行**；测试套件在 Windows 上也能跑，所以同一段逻辑在两个平台上的表现差异
必须写清楚 —— 这些差异**只影响测试的判定方式**，不影响容器里的真实行为。

| 行为 | Linux（容器，真实行为） | Windows（仅测试） | 影响 |
|---|---|---|---|
| `os.rename` 目标已存在 | **引擎不走这一步**：「防丢」在 `os.rename` **之前**就按体积定好终名（加 ` (2)`… 后缀 / 原有文件让位 / 保留原名），**两种平台都不覆盖** | 若绕过引擎直接用 `os.rename`：报 `[WinError 183]`、目标保留 | 测试断言"引擎按「防丢」定名、**不覆盖**"；**不要**把 Windows 的行为当"应用的行为" |
| 目录的 `st_size` | 通常 4096（与内容无关） | 也非 0/不确定 | 测试不依赖目录体积 |
| 目录的只读位（`chmod 0o555`） | 真的阻止在其中创建文件 | **不阻止**（只影响删除权限位） | 一条 POSIX-only 用例在 Windows 上**跳过** |
| 跨文件系统构造 | 可用挂载点/`/dev/shm` 等构造 EXDEV | 需要第二块盘/卷，通常没有 | 3 条跨设备用例在单文件系统机器上**跳过**（不是失败） |
| 符号链接 | 普通用户可创建 | 需管理员权限或开发者模式 | 1 条符号链接用例在 Windows 上**跳过** |
| `os.link` 的源 `st_nlink` | 建链后 1 → 2 | 同 | 测试断言"恰好 +1"，并校验 `(st_dev, st_ino)` 相同 |
| 路径分隔符 | `/` | 同时接受 `\` | 应用内部统一转 `/`（`paths.normalize()`） |
| 文件系统大小写 | 敏感（ext4） | 不敏感（NTFS 默认） | 白名单是**字符串判断**，与文件系统无关，因此 `.MKV` 用例在两边结论一致 |

---

## 5. 外部依赖兼容性

| 依赖 | 兼容范围 | 说明 |
|---|---|---|
| Docker / Compose | Docker 20.10+ 与 Compose v2（`docker compose`） | 使用 `env_file: .env`、`restart: unless-stopped`；未用 swarm 专有字段 |
| FastAPI / Uvicorn / Starlette | `fastapi==0.115.6`、`uvicorn[standard]==0.32.1`（星型依赖解析为 starlette 0.41.x） | 由 `docker/Dockerfile` pin；**不要在运行时升降级** |
| **PyYAML** | `PyYAML==6.0.3`（**必须 pin**） | 配置是 YAML（`PROCESSORS.md` §6.1）：`yaml.safe_load` / `yaml.safe_dump(allow_unicode=True, sort_keys=False, default_flow_style=False)`，UTF-8 无 BOM，首行 `schema_version: 2` |
| qBittorrent | 支持「运行外部程序」的 4.x（WebUI/Nox 均可） | 回调用 7 个查询参数 `token/name/hash/category/tags/content_path/save_path`（`kind=` 仍被接受但不再使用）；**匹配条件全空的库永不命中** |
| qBittorrent WebAPI | 用于「设置 → 测试连接」 | 连不上时 `POST /api/settings/test-qbittorrent` 返回 **HTTP 200** + `{"ok":false,...}`（连通性失败不是 HTTP 错误码） |
| 浏览器 | 需支持 `EventSource`（SSE）与 `fetch` | 实时日志走 `GET /api/tasks/{id}/stream`（`event: task` → `event: log` → `event: done`）；反代需关闭缓冲 |
| 时区 | `TZ`（默认 `Asia/Shanghai`） | 库里存 UTC ISO 串，**所有对外时间**都由 `app/serializers.py::format_time()` 渲染成本地 `YYYY-MM-DD HH:MM:SS`；重命名日志的 `timestamp` 同格式 |
| 认证 | 单管理员账号，scrypt 哈希，会话 Cookie `mlm_session`（`HttpOnly; SameSite=lax`，7 天） | **未设 `Secure`**；反代终止 TLS 的部署**未验证**（§7） |
| 资源规模 | 未做压力测试 | 全局串行 + 每次整份读写日志 ⇒ 首跑十万级文件会很慢（§7） |

---

## 6. 测试覆盖边界

测试套件**可以在任意能跑 Python 的平台完整运行**（有 pytest 就用 pytest，没有就用纯 stdlib 兜底 runner
`tests/run_all.py`）。它**不依赖真实挂载**：临时目录当源/目标，三态判据靠**注入** EXDEV 与意外 `errno`
来覆盖（`tests/test_paths.py`、`tests/test_hardlink_precheck.py`）。

**有三类与文件系统强相关的路径，在没有真实挂载的测试环境里没有被执行过**（在支持的环境上会
被 pytest 自动 skip，而不是"假装通过"）：

| 跳过类别 | 为什么 | 在哪才会真正验证 |
|---|---|---|
| 跨设备/跨挂载的逐文件失败路径（3 条） | 单文件系统环境无法构造跨设备 | POSIX 容器 |
| 只读源目录（1 条） | Windows 的目录只读位不阻止创建文件 | POSIX |
| 符号链接文件（1 条） | Windows 上创建符号链接需要额外权限 | POSIX |

也就是说：**跨挂载的真实行为、只读源目录、符号链接文件**这三类只在容器/POSIX 上才会被真正验证。
逐文件用例数、实际运行的环境与结论见 [`docs/meta/EVIDENCE.md`](../meta/EVIDENCE.md)；
用 `sh scripts/pytest-in-container.sh` 在容器里跑完再引用结论。

---

## 7. 未覆盖项与原因

| 未覆盖项 | 原因 |
|---|---|
| **Windows / macOS 上真跑任务** | 应用按容器内路径配置；`app/executor.py` 直接对这些路径 `os.path.isdir()` 并交给建链实现与规则引擎。在 Windows 测试环境里它们不是真实挂载点 ⇒ 跑不起来。**这是环境限制，不是缺陷。** |
| **跨文件系统/不支持硬链接的文件系统上的任何"降级方案"** | 设计上不提供复制回退（B8）。见 §2。 |
| **非 UTF-8 文件名 / 非 UTF-8 locale** | 容器固定 UTF-8（B9）；未在 GBK 等 locale 下验证，也未测试含无效 UTF-8 字节的文件名。 |
| **源目录里"找不到可用文件样本"时给出确定结论** | 实证探针只在**有界范围**内找样本（深度 ≤3、条目 ≤512、不跟随符号链接），找不到就返回 **`null`（无法实证）** 而**不是** `true`；应用**不会**为了让它变绿去源目录里造测试文件（那正是假绿的来源）。 |
| **SMB/NFS/FUSE/mergerfs（网络或联合文件系统）** | 硬链接语义不可靠，`st_dev`/`st_ino` 也可能失真；未验证，按"不支持"处理（§2）。 |
| **多实例 / 多 worker 并发** | SQLite + 单工作线程 + 日志"读-改-写整份"（L1/B11）都假定单实例。 |
| **已知边界行为（L1–L9）** | 它们是**冻结契约**，应用不修，只能识别与绕开（见 §3.1 与 [`TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md)）。 |
| **规模/性能上限** | 未做压力测试；首跑十万级文件会很慢（全局串行 + 每次整份读写日志）。 |
| **反向代理 / HTTPS / 域名访问** | 会话 Cookie 未设 `Secure`，SSE 经反代可能被缓冲；**未验证**。 |
| **`permission_mode`（777）的影响面** | 默认关闭 ⇒ **默认路径下不发生任何 `chmod`**。开启的代价（源文件权限位一起变）无法通过实现技巧消除，只能由开启者显式承担（界面二次确认 + 任务日志留痕 + 只动目标侧）。见 `BASELINE.md` §1.6 与 **B13**。 |
| **自动创建管理员账号（`.env` 的 `ADMIN_PASSWORD`）的覆盖语义** | 只在"一个用户都没有"时创建；已有用户时不覆盖。重置口令见 [`TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md)。 |
| **带着旧宿主路径的 `hard_links.log`** | 应用不改写日志里的 `Source:` 前缀；遇到这类日志只能**删掉它让建链阶段重建一次**（`BASELINE.md` §4.1）。账本同样**不回填**历史日志（§3.5 ④）。 |
| **从旧日志还原"某库某作品已处理"** | 旧日志是**全局**的、不含库与目标目录维度，无法可靠映射；应用**刻意不做**这种回填（§3.5 ④ 与 KI-7）。 |

---

## 8. 最小待确认项

请在你的真实环境上确认下面这几条（它们决定"你的场景是否落在支持范围内"）：

| # | 待确认项 | 怎么确认 |
|---|---|---|
| 1 | 下载目录与媒体库目录是否在**同一个挂载**里（同卷/同存储池**还不够** —— 见 §2） | **【宿主机】** `findmnt -T <源目录> -o TARGET,SOURCE` 与 `findmnt -T <媒体库目录> -o TARGET,SOURCE`，两条的 `TARGET` 必须**相同**（`stat -c '%d'` 相同**不能**作为依据） |
| 2 | 目标文件系统是否**真的支持硬链接** | **【宿主机】** 在目标目录里 `touch a && ln a b && ls -li a b`（两个 inode 号相同即支持），测完删掉 |
| 3 | 宿主机的 CPU 架构（决定是否需要 ARM64 构建） | **【宿主机】** `uname -m` |
| 4 | 你现有媒体库是怎么命名的（决定是否会有"新旧不一致"） | 抽样几个文件名，对照 §3.1 的命名表；有 `ENone` 说明历史文件里已有"无集数"的产物 |
| 5 | qBittorrent 版本与「运行外部程序」的占位符支持 | qBittorrent → 选项 → 下载 → 运行外部程序；参考 §5 与 [`TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md) |
| 6 | 是否需要经反向代理/HTTPS 访问（影响 SSE 与 Cookie） | 若经反代：确认关闭响应缓冲、并接受 Cookie 未设 `Secure`（见 §7） |
| 7 | 数据卷所在文件系统与剩余空间（日志/DB/配置/secret.key 都写这里） | **【宿主机】** `df -h <部署目录>/data` |
| 8 | 健康检查的判据 | `GET /api/system/health` **恒返回 200**，必须看 body：`status == "ok"` 且 `checks` **四项**（`db`/`data_dir`/`processors`/`config`）全为 `true`；只看 HTTP 码会漏判 |
