# 兼容性说明（COMPATIBILITY）

> 这篇文档回答「**哪些环境/文件系统/命名方式是被支持的，哪些不是**」。
> 冻结的行为基线见 [`docs/contracts/BASELINE.md`](BASELINE.md) §1–§3；**实现契约 B1–B10** 见同文 §4；
> 处理器的可配置面见
> [`docs/contracts/PROCESSORS.md`](PROCESSORS.md)；出具体报错时查 [`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md)。

**一句话前提**：本应用是「**在 Linux 容器里、对同一个挂载（mount）内的本地文件、按固定的规则语义处理你的下载目录**」的封装 ——
**硬链接**由 `app/processors/hardlink.py` 执行，**重命名**由配置驱动的规则引擎执行；
二者的行为都由项目自己的回归测试以**冻结的黄金值**守护。
凡是不满足这条前提的场景，都不在支持范围内。

---

## 1. 支持矩阵

| 维度 | 支持 | 不支持 / 未验证 | 依据 |
|---|---|---|---|
| 运行形态 | Docker 容器（`docker compose up -d`） | 直接在宿主机上跑 Python、Windows 服务、群晖套件 | 架构前提：源与目标放在**同一条共同父挂载**里（容器路径 = 宿主真实路径，`MEDIA_ROOT`），另有 `/data`、`/data/config` 两个挂载点 |
| 宿主 OS | Linux（Debian 系，例如飞牛 fnOS） | Windows / macOS 作为**运行**宿主 | 见 [`docs/guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md) |
| CPU 架构 | `linux/amd64`、`linux/arm64`（基座 `python:3.11-slim` 为 multi-arch） | 无 | `docker/Dockerfile:18` |
| Python | 3.11（容器内固定） | 其他版本未验证（测试套件在 3.14 上可通过，但**运行**以容器为准） | `docker/Dockerfile` |
| 应用端口 | 单实例、`uvicorn --workers 1`（**架构要求**） | 多 worker / 多副本 / 多实例共享同一数据卷 | B9；硬链接日志"读-改-写整份"（L1） |
| 队列并发 | 全局单线程串行 | 任何形式的并发执行 | B9 |
| 配置格式 | YAML 1.1（`/data/config/libraries.yaml`、`/data/config/processors/<id>.yaml`，首行 `schema_version: 2`） | 手写 JSON 配置（`config.json` 不是真源） | [`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §6.1（唯一真源） |
| 开发/测试环境（Windows） | ✅ 全量测试套件可跑（`tests/run_all.py`，纯 stdlib 兜底 runner；装了 fastapi 可直接跑 `pytest`） | ❌ 在 Windows 上**真跑任务**（容器路径不是真实挂载点） | 见 §6 |

---

## 2. 文件系统兼容性（最关键的一节）

硬链接要求**源与目标在同一个挂载（mount）内**，且该文件系统**支持硬链接**。

> **这是最关键的一处前提。** Linux 的 `linkat(2)` 由内核 `do_linkat()` 判定：它比较的是**挂载身份**（`old_path.mnt != new_path.mnt` ⇒ `EXDEV`），
> **不是** `st_dev`。所以"两个目录在同一块盘、`stat -c '%d'` 相同"**并不保证** `link()` 能成功 —— 只要它们是容器里的**两条独立 bind 挂载**（例如 `/downloads:ro` + `/library`），
> 硬链接就永远建不出来。正确形态是**单一共同父挂载**（见 [`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md) 场景 1、场景 21）。
> 判断"是否同一个挂载"用 `findmnt -T <目录> -o TARGET`（两条 `TARGET` 相同才算），**不要**用 `st_dev`。

| 文件系统 | 硬链接 | 结论 |
|---|---|---|
| ext4 / btrfs / xfs（NAS 本地存储池） | ✅ | **推荐**，本项目假设的场景 —— 前提是源与目标在**同一挂载**内 |
| ZFS（数据集内） | ✅ | 同一 dataset 且同一挂载内可以；跨 dataset 通常是**跨挂载** ⇒ EXDEV |
| mdadm/LVM 拼出来的同一卷 | ✅ | **同一挂载内**即可。⚠️ 不要用 `stat -c '%d'` 判断 —— `st_dev` 相同但分属两条 bind 时 `link()` 依然 `EXDEV`（内核比的是挂载身份） |
| NTFS（Windows 本地盘） | ✅（单卷内） | 可用，但**不是**本项目的目标环境；仅用于跑测试 |
| exFAT / FAT32（U 盘、部分外接盘） | ❌ | 根本无法建硬链接 ⇒ 任务必然大面积失败 |
| SMB/CIFS 网络共享 | ⚠️ 视服务端 | 多数实现不支持硬链接；即使支持，`st_dev`/`st_ino` 语义也可能不可靠 ⇒ **不支持** |
| NFS | ⚠️ 视服务端 | 同上；且 `probe_cross_filesystem` 的实证探测（**只读**源目录、临时文件写在目标侧）可能被服务端拒绝 ⇒ 结论是 `null`（无法实证），**不是** `true` |
| FUSE / 云盘挂载 / mergerfs / unionfs | ❌ / 未验证 | 硬链接语义通常不成立；**不支持** |
| 源与目标被挂成**两条独立 bind**（最常见，哪怕同一块盘） | ❌ | 内核按挂载身份判定 ⇒ 必然 EXDEV，见 [`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md) 场景 1 |
| 源与目标分属两个存储池 / 两个卷（哪怕同一台 NAS） | ❌ | 同上（跨挂载）；典型 EXDEV 场景 |
| 大小写敏感 | — | 应用不做大小写折叠；扩展名白名单是**大小写敏感**的（L7，建链实现与规则引擎一致） |
| 文件名编码 | UTF-8 | 容器固定 `LANG=C.UTF-8`/`PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`（B10） |

**为什么"跨卷/跨挂载复制"不是替代方案**：复制会让媒体库与下载**各占一份空间**，并且从此两边不再是"同一个文件"（改一边不会改另一边），违背了本项目的核心前提（见 [`docs/guides/FAQ.md`](../guides/FAQ.md) 第 1、2、3 问）。应用**故意不提供**复制回退（B6），并在调用建链实现前后用 `(st_dev, st_ino)` 逐文件核对产物**确实是硬链接**，否则记进 `link_failed` 并报「非硬链接」；源目录一侧另有**任务前后逐文件源完整性核对**（`Task.result.source_integrity`，见 B7）。

---

## 3. 已固定下来的行为（黄金值）

**重命名**由规则引擎（`app/processors/engine.py`）执行；**硬链接**由 `app/processors/hardlink.py` 执行。
唯一执行体就是这两处，应用只做自己的事、不依赖任何外部程序。

这些行为由**项目自己的回归测试**以**冻结的黄金值**守护：`tests/test_hardlink_native.py` A 组五条
（`test_the_native_implementation_on_a_fresh_tree` / `..._when_already_logged` / `..._when_a_folder_size_changes` /
`..._when_a_source_folder_disappears` / `..._on_a_damaged_log`）断言冻结的**黄金值**；规则引擎侧由
`tests/test_processor_engine.py` / `test_bangumi_episode_rules.py` / `test_rename_extensions.py` 守护。
因此镜像**自包含**，搬到别的设备不再有隐含依赖。

下表是"冻结点 → 证据测试"的对应关系（测试都在 `tests/`，运行方式见 §3.4）：

| 冻结点 | 内容 | 证据测试 |
|---|---|---|
| 集数正则与优先级 | `search`（按文件中**最早出现**位置，而非分支优先级）→ 依次 `episode1..8` 取第一个非空 → `zfill(2)`；否则 `ALEPISODE_PATTERN`；都没有则为字面量 `"None"` | `tests/test_processor_engine.py::test_expressions_are_evaluated_in_written_order`、`tests/test_bangumi_episode_rules.py` |
| 季文件夹识别 | `SEASON_PATTERN.fullmatch`（`Season (\d+)`）；`Season 0`（`00`）跳过 | `tests/test_processor_engine.py::test_accept_dirs_use_fullmatch_not_search`、`::test_skip_season_zero_is_honoured_when_a_season_group_is_present` |
| 重命名结果 | `legacy` 预设下重命名后的**整棵树**（文件名+目录结构）、`Renamed:` 行集合、每库日志的键与体积、跳过集合、异常容忍边界的判据被冻结成**黄金值**，由项目自己的回归测试直接断言 | `tests/test_processor_engine.py` / `tests/test_bangumi_episode_rules.py` / `tests/test_rename_extensions.py`（⚠️ 同名冲突**有意分叉**：见本行下方的 L4 行与 `tests/test_processor_engine.py::test_a_within_run_collision_keeps_both_files_with_a_suffix`） |
| `default` 与 `legacy` 的差异 | **只允许** [`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §3 列明的**四处有意增量**（⚠️ **L2「防丢」不算预设差异**：同名不再覆盖对两个预设**同样生效**，是引擎的**全局行为**，见本文件「`os.rename` 目标已存在」那行与 [`docs/contracts/BASELINE.md`](BASELINE.md) §5 的 L4 行）：① **bangumi** 的 `reject_dirs`（30 个名字：`SP/CM/OP/ED/SPs/CMs/OPs/EDs/Scan/Scans/CD/CDs/映像特典/特典CD/Preview/Previews/Scan (Vol.任意数字)/Vol.任意数字/EXTRA/Bonus/Menus/Music/Subtitle/Subtitles/ScreenShot/ScreenShots/NCOP/NCED/ncop/nced`，语境是**子文件夹层**；**不含任何发行组前缀**，`^\[ANi\]` 之类的说法是错的）；② **movie** 的 `reject_dirs` 由**用户指定**（`特典/CD/扫图/scans` + `SP/Menu/Preview/EXTRA/Bonus/NCOP/NCED/Sample`，全部 `(?i)^…$`）；③ **bangumi** 的 `episode_fallback`：**推荐默认是 `null`**—— 即"命名引用了集数、但 `episode_patterns` 一个都没命中 ⇒ **保留原文件名、不改名**"，而 `legacy` 预设把该字段写成**字符串** `"None"`（于是把字面量 `None` 命名进文件名，如 `… S01ENone.mkv`，同一作品的多个压制版本会撞成同一个目标名互相覆盖）；④ **bangumi** 的 `episode_patterns`（L1）：默认是 `docs/contracts/PROCESSORS.md` §3.2 增量 ④ 里那 **9 条有序规则**（`chapter`/`episode_word`/`season_episode`/`bracketed`/`ep_marker`/`hash_marker`/`dash`/`bare_e`/`bare_number`，序号小者优先、均忽略大小写、数字限 1~2 位），都带"这里不可能是集号"的边界约束 —— 实测把 `Show - 1920x1080.mkv` 认成第 19 集、把末尾 CRC 方括号 `[1AE35F3A]` 里的 `E35` 认成第 35 集这类误判（示例语料 8534 个文件名里"数字落在 6 位以上十六进制串内部"的命中：旧口径 21 个 ⇒ 新规则 0 个）不再发生；`legacy` 预设写回旧口径那两条（`BANGUMI_LEGACY_EPISODE_PATTERNS`，捕获组仍是 `g1..g8`/`g1`） | `tests/test_processor_spec.py::test_the_builtin_bangumi_preset_pair_differs_only_in_the_registered_increments`、`tests/test_processor_engine.py::test_the_builtin_bangumi_default_preset_keeps_the_original_name_when_unresolved`、`::test_the_builtin_bangumi_legacy_preset_still_renders_the_string_none`、`tests/test_bangumi_episode_rules.py`（9 条规则正/反例表 + legacy 两条字面量冻结 + `preset_spec` 深拷贝隔离） |
| **L3「特典排除」** | 命中全局设置项 `extras_patterns` 的散装特典文件（`NCOP` / `Preview` …）**退出改名流程** —— **不改名、也不移动**（保留原名留在原位置）。⚠️ 它**不是** `default` / `legacy` 的**预设差异**（由全局设置控制、对两个预设同样生效、`legacy` 也不解除，`extras_patterns: []` 可整体关闭），因此**不进 B4 的四处增量清单** | `tests/test_extras_classification.py`（19 条，含"L3 关闭时不认领特典"与"默认判据各自命中"） |
| 硬链接路径规则 | `目标/<一级文件夹>/<相对源的完整子路径>/<同名文件>`，逐字节保留 | `tests/test_adapter_link.py`、`tests/test_no_source_mutation.py` |
| 幂等标记 | **建链实现这一侧**：**一级文件夹体积**（体积一致即跳过）；体积变化则删旧记录行再写新行。⚠️ **应用层的判据不是这条**：应用层的"已处理"来自每库作品账本（库 + 源 + 目标 + 体积，见下方契约点 **B5**） | `tests/test_idempotency.py`、`tests/test_processor_engine.py::test_second_run_reports_the_skip_record` |
| 孤儿清理 | 日志里记录过、源已不存在的 folder ⇒ `shutil.rmtree` 目标目录；**日志行仍留在磁盘上**（建链实现只删内存字典，日志行保留） | `tests/test_adapter_orphan.py::test_default_cleanup_removes_an_orphaned_target_folder`、`::test_orphan_cleanup_false_leaves_the_library_and_the_log_untouched`、`tests/test_hardlink_native.py::test_the_native_implementation_when_a_source_folder_disappears` |
| 日志行格式 | 硬链接日志 = `%(asctime)s - %(message)s`，正文 `Source: <源> - Folder: <名> - Folder size: <整数>`（解析时**正好 3 段**，否则静默跳过）；重命名日志 = JSON，每条多一个 `works_dir` 键、**读日志时忽略未知键** | `tests/test_library_logs.py::test_written_bytes_are_byte_identical_to_the_original_dump_parameters`、`tests/test_processor_engine.py::test_log_entry_has_size_timestamp_and_works_dir`、`::test_pre_existing_log_entries_with_unknown_keys_are_tolerated`、`tests/test_hardlink_native.py::test_the_native_implementation_keeps_the_frozen_print_prefixes` |
| Movie 的分组 | 按 `filename.split('.')[-1]` 分组，每组**只保留体积最大**者；`title = folder`；`.srt`/`.MKV` 不重命名但**会建硬链接** | `tests/test_processor_engine.py::test_selection_per_extension_keeps_only_the_largest_file_per_suffix`、`tests/test_adapter_link.py::test_full_run_links_every_nested_file` |
| title 级作用域的函数名 | title 级作用域用的仍是 `rename_bangumi_episodes()` | `tests/test_processor_engine.py::test_title_scope_reads_files_from_the_title_folder_itself`（同一套 title 级作用域语义） |
| 处理器规格与注册表 | 三个内置处理器 `hardlink_only`/`bangumi`/`movie`、两个预设 `default`/`legacy`；规格校验、YAML 往返、日志文件命名 | `tests/test_processor_spec.py`（61 条）、`tests/test_registry.py`（19 条）、`tests/test_processor_engine.py`（34 条） |

> ⚠️ **上表的举证方式**：`app/processors/hardlink.py` 与规则引擎 `app/processors/engine.py` 是唯一执行体，行为由项目自己的回归测试（**冻结的黄金值**）守护：`tests/test_hardlink_native.py`、`tests/test_processor_engine.py`、`tests/test_bangumi_episode_rules.py`、`tests/test_rename_extensions.py`。

> **扩展名白名单核查结论**：实际会用到的扩展名**只有** `.mkv`/`.mp4`/`.ass`，**不含任何纯音频后缀** ⇒ 白名单里没有需要删去的项，也不存在"音乐文件被误改名或误建链"的情形；`.srt`/`.MKV` 属**不重命名但会建硬链接**（后者是大小写敏感所致），行为由 `tests/test_processor_engine.py::test_extensions_are_matched_case_sensitively`、`tests/test_rename_extensions.py`、`tests/test_adapter_link.py::test_full_run_links_every_nested_file` 钉住。

### 3.1 命名结果对照（这些是**预期结果**，不是 bug）

一级文件夹 `Show A`、季文件夹 `Season 1` 下（`preset=legacy` 是最朴素的旧口径，与冻结黄金值一致；`default` 多一条特典类拒绝规则，且认不出集数的文件**保留原名** —— 下表是 **`legacy`** 的结果，`default` 里那几个 `…ENone…` 全部**不改名**，`Show.E05.mkv`（`E` 前是点号而不是空白）同理**不改名**；`Show.EP03.mkv`/`Show.#07.mkv`/`Show.第05集.mkv` 照旧命中）：

| 原文件名 | 结果 |
|---|---|
| `[Lilith-Raws] Show - 01.mkv` | `Show A S01E01.mkv` |
| `Show.S01E01.chs.ass` | `Show A S01E01.chs.ass` |
| `Show 12.mp4` | `Show A S01E12.mp4` |
| `Show.EP03.mkv` / `Show.E05.mkv` / `Show.#07.mkv` / `Show.第05集.mkv` | `…S01E03/E05/E07/E05…` |
| `Show [09] x 第05集.mkv` | `…S01E09…`（**按最早出现位置**，`[09]` 在前） |
| `Show 第05集 x [09].mkv` | `…S01E05…` |
| `Show.01.ass` | `Show A S01ENone.01.ass`（`.01` 不是被识别的集数标记 ⇒ 集数为 `None`，后缀原样保留） |
| `plain.ass` | `Show A S01ENone.ass` |
| `a.b.c.ass` | `Show A S01ENone.c.ass` |
| `x.01.sc.ass` | `Show A S01ENone.sc.ass` |
| `no number here.mkv` | `Show A S01ENone.mkv` |
| `UPPER.MKV` | 不动（大小写敏感白名单） |

季/目录层面的兼容规则：

| 情形 | 行为 |
|---|---|
| `Season 1 (2018)`、`Season 01`（带括号/非纯数字后缀） | **不处理**（`fullmatch` 不通过，L5） |
| `Season 0` | **不处理**（`"00"` 跳过） |
| 季文件夹里的**嵌套目录**（如 `Season 1/Subs/`） | 里面的文件**永不重命名**（L6） |
| 作品根目录下的散落文件（`Show A/loose.mkv`） | **不重命名**（只处理季文件夹的直接子文件） |
| 一级文件夹命中 `default` 的特典类拒绝规则（`Scans`/`CD`/`SP`…） | **整棵子树都不处理**，并留一条 warning（有意增量，见下方契约点 B4） |
| 文件名/目录名里含 `" - "` 的**一级文件夹** | 日志行会被静默忽略 ⇒ 每次都当"未记录"重跑并刷 `File exists`（L2，数据不受损）。应用能识别"目标已是同一 inode 的硬链接"并算成幂等跳过，**不会因此判任务失败**；但**文件夹级**的体积跳过永远命中不了它，所以每轮仍会重新走一遍建链（日志行格式所限，见 L2）。这一层改判**不依赖被截短的文本行** —— 建链实现把失败项结构化暴露（`hardlink.last_failures()`），适配层优先用它，文本切分按 `[Errno`/`[WinError` 锚点，所以含 `" - "` 的文件夹同样能正确记进 `already_linked`（**stdout 与日志格式一个字都没改**） |
| 同后缀多文件（Movie） | 只改**体积最大**的那个，其余保持原名 |
| 体积相同的一级文件夹（追加了等体积文件） | 会被跳过（L8/L9）——**这是既有规则** |

> 迁移兼容性：只要按 `preset=legacy` 配置，命名结果与目录层级就与"最朴素的旧口径"**完全一致**（现行判据是项目自己的冻结黄金值，见本节前言与 §3.1）；
> 差别只在于**日志位置与归属**（每个媒体库有自己的重命名日志文件，路径形如 `<data>/logs/<log_file>`，
> 由 `settings.library_log_path()` 决定）与**多了任务/队列/网页/处理器配置**这层外壳。
> 日志按库各自记账，不需要任何"日志迁移"步骤，详见 [`docs/contracts/BASELINE.md`](BASELINE.md) §4.1。

### 3.2 契约点 → 测试函数 → 结论（逐条）

下表把 [`docs/contracts/BASELINE.md`](BASELINE.md)（§1–§5）与 [`docs/contracts/ARCHITECTURE.md`](ARCHITECTURE.md)（§2/§5/§6）的契约点逐条映射到**具体测试函数**，并给出结论。所有测试均在 `tests/` 下，运行方式见 §3.4。

> ⚠️ **举证方式**：`app/processors/hardlink.py` 与规则引擎 `app/processors/engine.py` 是唯一执行体，行为由项目自己的回归测试（**冻结的黄金值**）承担：`tests/test_hardlink_native.py`、`tests/test_processor_engine.py`、`tests/test_bangumi_episode_rules.py`、`tests/test_rename_extensions.py`。

#### BASELINE §1 硬链接流水线（执行 `app/processors/hardlink.py`，B3）

| 契约点 | 测试函数 | 结论 |
|---|---|---|
| 只处理一级文件夹；体积 = 递归求和（空文件夹为 0） | `test_adapter_link.py::test_full_run_links_every_nested_file`、`test_adapter_link.py::test_empty_folder_is_logged_with_size_zero`、`test_hardlink_native.py::test_the_native_implementation_on_a_fresh_tree` | 通过 |
| 日志正文格式（3 段，恰好 3 段否则静默跳过）；一行一 folder | `test_adapter_link.py::test_documented_log_line_per_first_level_folder`、`test_hardlink_native.py::test_the_native_implementation_keeps_the_frozen_print_prefixes` | 通过 |
| 体积一致 ⇒ 跳过（**建链实现**里幂等的唯一来源；应用层改用每库账本，见下方契约点 B5） | `test_adapter_link.py::test_second_run_with_same_sizes_is_skipped`、`test_idempotency.py::test_second_run_is_a_pure_noop`、`test_hardlink_native.py::test_the_native_implementation_when_already_logged` | 通过 |
| 体积不一致 ⇒ 删旧记录行、重写后仍只有一行 | `test_idempotency.py::test_size_change_removes_the_old_record_and_keeps_one_line`、`test_hardlink_native.py::test_the_native_implementation_when_a_folder_size_changes` | 通过 |
| `makedirs(exist_ok=True)` 建目标层级 | `test_adapter_link.py::test_full_run_links_every_nested_file` | 通过 |
| `os.link` 成功/失败都打印且**不中断** | `test_adapter_link.py::test_existing_target_file_fails_without_overwriting`、`test_rule_validation.py::test_a9_copied_files_are_detected_and_reported_as_failures` | 通过 |
| 目标已有同名文件 ⇒ 不覆盖（FileExistsError 被捕获） | `test_adapter_link.py::test_existing_target_file_fails_without_overwriting` | 通过 |
| 孤儿清理：源不存在 + 目标存在 ⇒ `rmtree`；**日志行仍留在磁盘**；`orphan_cleanup=False` 时逐字节复原 | `test_adapter_orphan.py::test_default_cleanup_removes_an_orphaned_target_folder`、`test_adapter_orphan.py::test_orphan_cleanup_false_leaves_the_library_and_the_log_untouched`、`test_hardlink_native.py::test_the_native_implementation_when_a_source_folder_disappears` | 通过 |
| 源目录不存在 ⇒ `FileNotFoundError` 向上抛 | `test_adapter_link.py::test_missing_source_dir_returns_an_exception_field` | 通过 |
| 跨文件系统只失败、**绝不复制** | `test_adapter_link.py::test_cross_filesystem_warns_but_still_runs_the_script_without_copying`、`test_paths.py::test_probe_reports_exdev_as_a_reliable_false`、`test_rule_validation.py::test_a9_no_copy_fallback_anywhere_in_the_backend` | **部分未覆盖**：两条跨设备用例在只有单文件系统的机器上 skip（见 §6）；EXDEV 分支本身由 `test_probe_reports_exdev_as_a_reliable_false` 注入实证（`ok=False` 是可靠判据，但**不再硬拦**，见 B6/B7） |
| 产物**确实是硬链接**（同 `st_dev`/`st_ino`、`st_nlink==2`、非 symlink、源仍在） | `test_adapter_link.py::test_every_target_file_is_a_real_hardlink_never_a_copy` | 通过 |
| 源树零改动（size/mtime/ino/mode/内容） | `test_no_source_mutation.py::test_hardlink_pipeline_never_touches_the_source_tree`、`test_no_source_mutation.py::test_second_and_forced_runs_keep_the_source_pristine` | 通过 |
| `chmod`/`os.getlogin()` 只在 `__main__` 分支 | `tests/test_hardlink_native.py`（`create_hard_links()` 无 `__main__` 副作用）、`test_permission_mode.py` 全套 9 条 | 通过 |

#### BASELINE §2/§3 重命名（现行判据为冻结黄金值，B5）

| 契约点 | 测试函数 | 结论 |
|---|---|---|
| `SEASON_PATTERN`/`EPISODE_PATTERN`/`ALEPISODE_PATTERN` 逐字符冻结 | `tests/test_processor_spec.py::test_module_constants_match_the_frozen_contract`、`tests/test_bangumi_episode_rules.py::test_legacy_preset_keeps_the_old_loose_behaviour` | 通过 |
| 集数解析：`search` 按**最早出现位置**，依次 `episode1..8` 取第一个非空 → `zfill(2)`；否则 ALEPISODE；否则**字面量 `"None"`（str，不是 null）** | `tests/test_bangumi_episode_rules.py::test_legacy_preset_keeps_the_old_loose_behaviour`、`test_processor_engine.py::test_the_builtin_bangumi_legacy_preset_still_renders_the_string_none`、`test_processor_engine.py::test_expressions_are_evaluated_in_written_order` | 通过 |
| 命名 `{title} S{season}E{episode}{suffix}`；`.ass` 多段后缀保留（`.chs.ass`） | `tests/test_bangumi_episode_rules.py::test_default_rules_rename_with_the_expected_episode` | 通过 |
| `find_season_folders` 用 `SEASON_PATTERN.fullmatch`（`Season 1 (2018)` 被忽略）；`Season 0` 跳过 | `test_processor_engine.py::test_accept_dirs_use_fullmatch_not_search`、`test_processor_engine.py::test_skip_season_zero_is_honoured_when_a_season_group_is_present` | 通过 |
| 只处理季文件夹的**直接子文件**（L6） | `test_processor_engine.py::test_subdir_scope_requires_a_direct_child_and_a_whitelisted_lowercase_extension` | 通过 |
| 扩展名白名单 `.mkv/.mp4/.ass` 且**大小写敏感** | `test_processor_engine.py::test_extensions_are_matched_case_sensitively`、`tests/test_rename_extensions.py` | 通过 |
| `os.rename` 就地改名（不产生硬链接） | `test_processor_engine.py::test_folder_scope_touches_only_that_title_and_unknown_folder_is_a_noop`、`test_no_source_mutation.py::test_rename_pipelines_only_run_inside_the_target_root` | 通过 |
| L4：`os.rename` 目标已存在时的**平台差异** | **引擎不走这一步**：引擎加后缀 / 让位 / 保留原名（**有意分叉**），在任何平台上都不覆盖；引擎侧用例见 `test_processor_engine.py::test_a_within_run_collision_keeps_both_files_with_a_suffix`、`::test_an_existing_target_collision_makes_the_smaller_newcomer_take_a_suffix`、`tests/test_rename_collisions.py` | 通过（引擎侧无覆盖） |
| 缓存 JSON 形状/位置/损坏容错/体积跳过 | `test_library_logs.py::test_json_indent_matches_the_original_script`、`test_library_logs.py::test_written_bytes_are_byte_identical_to_the_original_dump_parameters`、`test_library_logs.py::test_read_json_cache_degrades_to_empty_mapping`、`test_processor_engine.py::test_log_entry_has_size_timestamp_and_works_dir`、`test_processor_engine.py::test_pre_existing_log_entries_with_unknown_keys_are_tolerated`、`test_processor_engine.py::test_second_run_reports_the_skip_record`、`test_rule_validation.py::test_b6_engine_logs_live_in_the_data_volume_and_never_beside_the_scripts` | 通过 |
| 按 `split('.')[-1]` 分组、每组**只保留体积最大**者；`{title}.{ext}`；`title = folder`（原样，含点/感叹号） | `test_processor_engine.py::test_selection_per_extension_keeps_only_the_largest_file_per_suffix`、`test_processor_engine.py::test_title_scope_reads_files_from_the_title_folder_itself` | 通过 |
| `.srt`/`.MKV` 不重命名（但会被建硬链接） | `test_processor_engine.py::test_extensions_are_matched_case_sensitively`、`test_adapter_link.py::test_full_run_links_every_nested_file`（`Subs/extra.srt` 也被建链） | 通过 |
| title 级作用域的函数名 `rename_bangumi_episodes()` | `tests/test_processor_engine.py::test_title_scope_reads_files_from_the_title_folder_itself`（同一套 title 级作用域语义） | 通过 |
| title 级作用域**不检查** `isfile` ⇒ 形如 `Sub.mp4` 的**目录**也会参与改名 | `test_processor_engine.py::test_title_scope_renames_a_directory_that_looks_like_a_video_like_the_original_script`（title 级作用域刻意不过滤 `isfile`，同一用例还对照证明 `subdir` 级要求 `isfile`） | 通过（既有行为，应用不修） |
| 缓存/跳过/force/定向/根目录缺失 | `test_processor_engine.py::test_force_only_prunes_the_named_folder`、`test_processor_engine.py::test_an_invalid_spec_is_refused_without_touching_any_file`、`test_processor_engine.py::test_hardlink_only_kind_refuses_to_rename` | 通过 |
| **规格非法 ⇒ 一律不动文件** | `test_processor_engine.py::test_an_invalid_spec_is_refused_without_touching_any_file`（`exception` + warning `处理器规格不合法，已放弃本次重命名。`） | 通过 |
| 命名层沙箱与容错 | `test_processor_engine.py::test_expressions_are_evaluated_in_a_restricted_sandbox`、`::test_expression_forward_reference_reports_a_chinese_warning`、`::test_segments_rendering_an_empty_name_skip_the_file_with_a_chinese_reason`、`::test_unknown_segment_param_is_still_dropped_but_now_recorded` | 通过（最后一条**记录**一个已知缺口：未知 `param` 的留痕仅在任务结果里，**没有** warning） |

#### BASELINE §4 实现契约 B1–B10
| 契约点 | 测试函数 | 结论 |
|---|---|---|
| **B1** 判据的产物已冻结成**黄金值**，由 `test_hardlink_native.py` A 组五条（`test_the_native_implementation_on_a_fresh_tree` / `..._when_already_logged` / `..._when_a_folder_size_changes` / `..._when_a_source_folder_disappears` / `..._on_a_damaged_log`）与 `test_processor_engine.py` / `test_bangumi_episode_rules.py` / `test_rename_extensions.py` 断言 | `test_hardlink_native.py::test_the_native_implementation_on_a_fresh_tree`、`::test_the_native_implementation_when_already_logged` 等 A 组五条、`test_processor_engine.py`、`test_bangumi_episode_rules.py`、`test_rename_extensions.py` | 通过（现行 5 个判据：整棵树、`Renamed:` 集合、日志键与体积、跳过集合、源树零改动，均为**冻结黄金值**） |
| **B4** `default` 与 `legacy` 的差异**只允许** `docs/contracts/PROCESSORS.md` §3 列明的有意增量（共**四处**：① bangumi `reject_dirs`、② movie `reject_dirs`、③ bangumi `episode_fallback`、④ bangumi `episode_patterns` —— 9 条有序规则 vs 旧口径的两条）。⚠️ **L2「防丢」与 L3「特典排除」都不在这四处里**：两者都**不是预设差异** —— L2 是引擎的**全局行为**，L3 是**由全局设置项控制的可选能力**（`extras_patterns`，对两个预设同样生效；只排除改名、不移动文件） | `test_processor_spec.py::test_the_builtin_bangumi_preset_pair_differs_only_in_the_registered_increments`、`test_bangumi_episode_rules.py`、`tests/test_processor_engine.py::test_reject_dirs_block_the_whole_subtree_before_any_parsing` | 通过 |
| **B3** §2.3 的三个正则逐字符冻结；`default` 的改动必须由 YAML 显式表达 | `test_processor_spec.py::test_module_constants_match_the_frozen_contract`、`test_processor_engine.py::test_accept_dirs_use_fullmatch_not_search`、`tests/test_bangumi_episode_rules.py::test_legacy_preset_keeps_the_old_loose_behaviour` | 通过 |
| **B5** 日志格式冻结（硬链接纯文本 / 重命名 JSON），每条记录另有 `works_dir`，**读日志忽略未知键**；引擎日志必须落数据卷 | `test_rule_validation.py::test_b6_engine_logs_live_in_the_data_volume_and_never_beside_the_scripts`、`test_library_logs.py::test_json_indent_matches_the_original_script`（见注）、`test_processor_engine.py::test_log_entry_has_size_timestamp_and_works_dir`、`::test_pre_existing_log_entries_with_unknown_keys_are_tolerated`、`test_file_log_isolation.py` 全套 6 条（日志隔离：`hard_links.log` 只收两类记账行） | 通过 |
| **B7** "已处理"判据是**每库作品账本**：跳过当且仅当 `(library_id, 作品名)` 在库账本里且 `source_dir`、`target_dir`、体积**三者全等**（`app/link_records.py`，表 `link_records`）。同源多库、同库换目标都不再互相"跳过"；`hard_links.log` 仍**逐字照写**但不再参与判断，同一套打印与记账行为由 `app/processors/hardlink.py` 执行 | `test_works_ledger.py::test_ledger_is_scoped_by_library_source_and_target`、`::test_ledger_rows_are_refreshed_instead_of_duplicated`、`::test_skip_dict_becomes_the_only_judgement`、`::test_default_path_still_reads_the_global_log`、`::test_forced_folder_scope_really_relinks`、`test_idempotency.py::test_second_run_is_a_pure_noop`、`test_processor_engine.py::test_second_run_reports_the_skip_record`、`test_adapter_link.py::test_second_run_with_same_sizes_is_skipped` | 通过（后三条是建链实现输出等价性的证据，黄金值基线见 `tests/test_hardlink_native.py`） |
| **B6** 绝不实现复制回退；调用前**挂载身份**拦截（`same_filesystem` 三态）、调用后逐文件核对 `(st_dev, st_ino)` | `test_rule_validation.py::test_a9_no_copy_fallback_anywhere_in_the_backend`、`test_rule_validation.py::test_a9_copied_files_are_detected_and_reported_as_failures`、`test_adapter_link.py::test_every_target_file_is_a_real_hardlink_never_a_copy`、`test_paths.py::test_paths_module_never_copies_files` | 通过；跨设备 3 条在只有单文件系统的机器上 skip（见 §6） |
| **B7** 源目录**不靠** `:ro` 挂载保护 —— 单挂载形态下内核让「源单独只读挂载」与「跨路径硬链接」**互斥**（见 §2 与 TROUBLESHOOTING 场景 1/3），改为「代码层不写源目录 + **任务前后逐文件源完整性核对**（`Task.result.source_integrity`）」：**可验证的保证** | `test_hardlink_precheck.py` 全套（执行器只警告不硬拦 + `source_integrity` 落进 `Task.result` + `/validate?probe=true` 三态 + `/api/system/info` 只读展示）、`test_paths.py` 的三态/mount ID/只读安全/有界递归/快照用例、`test_no_source_mutation.py` 全套（audit hook + 探针不写源）、`test_rule_validation.py::test_a10_source_tree_is_never_opened_for_writing` | 通过 |
| **B8** `LANG=C.UTF-8`/`PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8` | `test_rule_validation.py::test_a11_locale_and_encoding_are_locked_to_utf8` | 通过 |
| **B9** 全局串行、单工作线程 + 锁；孤儿清理保留为可选开关 `orphan_cleanup`（默认开，关闭时不得执行该分支，每次删除记 WARN） | `test_rule_validation.py::test_a12_single_process_single_worker`、`test_queue_serialization.py` 全套 8 条（含 `max_inflight==1`、严格 FIFO、执行器崩溃后继续消费）、`test_adapter_orphan.py::test_default_cleanup_removes_an_orphaned_target_folder`、`::test_orphan_cleanup_false_leaves_the_library_and_the_log_untouched`、`::test_a_folder_that_still_exists_is_never_removed`、`::test_drop_and_restore_lines_preserve_the_file_byte_for_byte` | 通过 |
| **B10** `permission_mode` 是**默认关闭的显式开关**：默认 `False` 时一个 `chmod` 都不做；开启后只动**目标侧**（库根 + 本次处理到的每个一级文件夹的目标子树，含被跳过的旧作品），符号链接不跟进 | `test_permission_mode.py` 全套 9 条（建链实现 / 适配层 / 执行器三层）、`test_hardlink_native.py` 的入口签名指纹、`test_rule_validation.py` 的三处替身；依据见 §3.3 第 8 条 | 通过（契约确认的行为，非"故意偏离"） |

> **B6 注**：上表第二行的函数名以 §3 表格为准（`tests/test_library_logs.py::test_json_indent_matches_the_original_script`）。

> **源侧保护注**：源侧保护**不是**挂载权限，而是 B9 的「代码层零源写入 + 任务前后逐文件源完整性核对」：
> 单挂载形态下内核的 ``linkat(2)`` 比较**挂载身份**，「源单独只读挂载」与「跨路径硬链接」**互斥**，
> 所以 ``/downloads`` 不挂 ``:ro``；见 `docs/contracts/BASELINE.md` B9、
> `docs/guides/TROUBLESHOOTING.md` 场景 1/3 与本文 §3.3 第 14 条。

#### BASELINE §5 限制 L1–L9（**都是既有行为，应用不得修** —— 测试只"确认它没被修"）

| 限制 | 测试函数（确认行为保持不变） |
|---|---|
| L1 日志逐行重写、并发会互相覆盖 | `test_rule_validation.py::test_a12_single_process_single_worker`（用 B9 规避，测试确认单 worker） |
| L2 目录名含 `" - "` ⇒ 该行永远解析失败、每次重复尝试 | `test_idempotency.py::test_folder_name_containing_dash_no_longer_reports_spurious_failures`、`test_idempotency.py::test_size_change_in_a_dashed_folder_never_dedupes_the_record`、`test_hardlink_native.py::test_last_failures_reports_the_full_target_path_for_dashed_folders` |
| L3 无集数 ⇒ 字面量 `None`（`ENone`） | `test_bangumi_episode_rules.py::test_legacy_preset_keeps_the_old_loose_behaviour`、`test_processor_engine.py::test_the_builtin_bangumi_legacy_preset_still_renders_the_string_none`（`Show.01.ass` ⇒ `Show A S01ENone.01.ass`） |
| L4 `os.rename` 可静默覆盖（平台相关）—— **引擎有意分叉、不再覆盖** | 引擎侧：`test_processor_engine.py::test_a_within_run_collision_keeps_both_files_with_a_suffix`、`::test_an_existing_target_collision_makes_the_smaller_newcomer_take_a_suffix`、`::test_renaming_a_file_to_its_own_name_is_not_a_collision`、`::test_a_collision_free_run_produces_no_collision_warning`、`::test_a_rerun_keeps_the_suffixed_names_without_stacking_or_recounting`、`tests/test_library_overrides.py::test_executor_makes_collisions_visible_and_keeps_every_file` |
| L5 `Season 1 (2018)` 因 `fullmatch` 被忽略 | `test_processor_engine.py::test_accept_dirs_use_fullmatch_not_search` |
| L6 季文件夹内嵌套子目录文件永不重命名 | `test_processor_engine.py::test_subdir_scope_requires_a_direct_child_and_a_whitelisted_lowercase_extension`、`test_hardlink_native.py::test_the_native_implementation_when_a_source_folder_disappears` |
| L7 只处理 `.mkv/.mp4/.ass`（大小写敏感），`.srt` 不重命名但**会被建硬链接** | `test_processor_engine.py::test_extensions_are_matched_case_sensitively`、`test_adapter_link.py::test_full_run_links_every_nested_file` |
| L8 体积变化检测是事后的（原地改写同尺寸发现不了） | `test_idempotency.py::test_size_change_removes_the_old_record_and_keeps_one_line`（体积变才重做）、`test_idempotency.py::test_second_run_is_a_pure_noop` |
| L9 一级文件夹体积是唯一标记、**体积相同即跳过** | `test_idempotency.py::test_second_run_is_a_pure_noop`、`test_idempotency.py::test_empty_folder_stays_skipped_forever`、`test_adapter_link.py::test_second_run_with_same_sizes_is_skipped` |

#### ARCHITECTURE §2/§5/§6 模块与 API 契约

| 契约点 | 测试函数 | 结论 |
|---|---|---|
| §2 路径映射/同挂载校验/白名单 | `test_paths.py` 全套 **56 条 + 2 skip**（`normalize`/`is_inside`/`existing_ancestor`/`to_host`/`to_container`/`check_same_filesystem`/`check_cross_mounts`/`assert_allowed`/`check_hardlink_support`/`probe_cross_filesystem`/`find_sample_file`/`cleanup_probe_leftovers`/`snapshot_source_tree`/`verify_source_tree`） | 通过；`probe_cross_filesystem` / `same_filesystem` 是**三态 `bool \| None`**（`true` 已实证 / `false` 跨挂载 / `null` 无法实证）—— 该契约的用例（三态、只读安全、有界递归、`nlink`/`ctime` 不误报）在 `test_paths.py` 与 `test_hardlink_precheck.py` |
| §6.3 security（scrypt 哈希、会话、`require_user`、hook token） | `test_api_smoke.py::test_setup_login_logout_lifecycle`、`::test_password_change_invalidates_the_session`、`::test_anonymous_callers_are_rejected_with_401`、`::test_require_user_routes_answer_200_after_login`、`::test_qbittorrent_hook_requires_a_token_and_matches_configured_libraries` | 通过 |
| §6.5 script_adapter（`ScopedOs`/`LinkOutcome`/`run_hardlinks`/`remove_log_entries`/`_drop_missing_source_lines`/`ensure_log_file`） | `test_adapter_link.py`、`test_adapter_scoped.py`、`test_adapter_orphan.py`、`test_hardlink_native.py` | 通过；重命名已移出本模块（见 engine/registry） |
| §6.5b 处理器与配置（`app/processors/{spec,defaults,engine,logs,registry}.py`、`app/configstore.py`） | `test_processor_spec.py`（61 条）、`test_processor_engine.py`（34 条）、`test_library_logs.py`（17 条）、`test_registry.py`（19 条） | 通过 |
| §6.6 queue（`start/stop/submit/cancel/running_task_id/queued_count`） | `test_queue_serialization.py` 全套 8 条 | 通过 |
| §6.7 executor（预检、`result` 计数、status 规则） | `test_api_smoke.py::test_run_creates_real_hardlinks_and_renames_end_to_end`（`result` 全字段 + 第二次 `linked==0/skipped==2`）、`::test_retry_and_cancel_follow_the_status_rules` | 通过 |
| §6.8 scanner（`scan_once`、有活动任务则跳过） | `test_api_smoke.py::test_qbittorrent_hook_requires_a_token_and_matches_configured_libraries`（scan 分支） | 通过 |
| §6.9 `QBClient`/`test_connection` | `test_api_smoke.py::test_settings_password_semantics_are_exact`（含 `POST /api/settings/test-qbittorrent` 返回 **200** + `{"ok":false,…}`） | 通过 |
| §5 API（认证、库 CRUD/校验、任务、日志/SSE、统计、设置、系统、hooks、**处理器与配置**） | `test_api_smoke.py` 全套 31 条 | 通过；**12 处文档偏差/契约点**见 §3.3（第 1/2/3/4/5/7/9/10/11/12/13/14 条，均按实现断言，文档已改） |
| §5 处理器端点形状（10 条：`GET/POST /api/processors`、`GET/PUT/DELETE /api/processors/{id}`、`POST /api/processors/{id}/reset`、`POST /api/processors/validate`、`GET /api/config`、`POST /api/config/reload`、`POST /api/config/export`） | `test_api_smoke.py::test_processor_endpoints_round_trip`（七条处理器端点的 HTTP 往返：信封/排序、规格同层、201/409/422/400、`validate` 永远 200、`reset` 的 `reject_dirs` + `episode_fallback` 两处差异（第三处 `episode_patterns` 由 `test_bangumi_episode_rules.py` 覆盖）、`DELETE` 三态）、`::test_config_endpoints_round_trip`（`GET /api/config` 键集合、`export` 写出四份 YAML、`reload?force=true` **查询参数**、坏文件的 `result.errors` 与 `in_sync:false`、修好后自动清空）、`test_registry.py`、`test_processor_spec.py` | 通过；`GET /api/processors` 的**排序**按 §3.3 第 9 条 |

### 3.3 与文档不一致的偏差清单（测试一律按**实现**断言）

**结论**：第 1–9 条与第 11–14 条**全部按「实现为真、改文档」处理**，测试一条都不用改；其中第 6 条与第 8 条是文档/规格侧的实质错误。文档侧：`docs/contracts/ARCHITECTURE.md` §5 的 `{ok, checks:[…]}` **作废**、改为实现的扁平结构，并新增 §5.0 第 7 条**超集规则**（文档列出的字段是**下界**，实现可多返回、只增不改）、第 9 条（`probe` 子对象与只读源目录降级不算失败）、第 10 条（回调参数必须走查询串）；`docs/contracts/BASELINE.md:203` 与 §2.6 的命名算法按冻结代码改正；`permission_mode` 见第 8 条 —— 现行的口径是「默认关闭的显式开关」（不是"永不实现"）。
**第 9–14 条**是契约里**必须写下来的顺序/必填性/形状/隔离性/取值域/前提语义**，登记原因都是"最容易在实现与文档之间悄悄漂移"；其中**第 14 条是前提级的修正**（挂载身份 vs `st_dev`），它同时改写了 B7 的口径。

| # | 文档说的 | 实现是 | 依据测试 | 结论 |
|---|---|---|---|---|
| 1 | `POST /api/libraries/{id}/validate` 返回 `{ok, checks:[{name,ok,message}]}` | **扁平结构，21 个字段**：`{ok, source_dir, target_dir, source_exists, target_exists, same_filesystem, probe:{ok,note}, errors[], warnings[], library_id, library_name, type, processor_id, processor_ok, processor_name, processor_kind, log_file, log_path, source_folders, target_dir_exists, same_filesystem_paths}`（`app/routers/libraries.py:486-509`），**没有 `checks`**；`combination_ok` **从未实现**，处理器是否存在由 **`processor_ok`** 表达（见 [`docs/contracts/API.md`](API.md) §3.8）。`same_filesystem` 的**落点已冻结**：`false`（已证实建不出硬链接）⇒ 同一文案进 `errors[]` 且 `ok=false`；`null`（判不了）⇒ **只**进 `warnings[]` 且 `ok=true`；`true` 且判据文案非空 ⇒ **也**进 `warnings[]` ⇒ **`warnings` 非空 ≠ 有问题**（"只警告不硬拦"只适用于**跑任务**，不适用于 `validate` 的 `false`） | `test_api_smoke.py::test_library_crud_and_validation_surface`、`test_hardlink_precheck.py::test_validate_response_shape_is_flat_and_never_waits_for_combination_ok`、`test_hardlink_precheck.py::test_validate_error_and_warning_landing_is_frozen`、`test_hardlink_precheck.py::test_validate_reports_exdev_as_a_conclusive_error_but_never_copies`、`test_hardlink_precheck.py::test_validate_treats_unprovable_as_a_warning_not_an_error` | 实现为真；ARCHITECTURE §5 已改，新增 §5.0 第 7 条 |
| 2 | （散文未写清方法） | `/validate` 是 **POST**（GET → 404）；`/api/auth/password` 是 **POST**（PUT → 405） | 同上、`test_api_smoke.py::test_password_change_invalidates_the_session` | 无需改文档，断言正确 |
| 3 | `DELETE /api/libraries/{id}?confirm=true` → `{"deleted":true,"hardlinks_kept":true}` | 多返回一个 `library_id` | `test_api_smoke.py::test_library_delete_requires_confirm_and_keeps_hardlinks` | 属超集，已补进 §5 表格 + §5.0 第 7 条 |
| 4 | 曾拟去掉 `/api/tasks/{id}/logs` 的 item 里 `task_id` | item **仍是** `{id, task_id, ts, level, message, stream}` | `test_api_smoke.py::test_logs_and_sse_stream_share_the_same_frame_shape` | **未实施**，以实现为准；已补进 §5 表格 |
| 6 | `docs/contracts/BASELINE.md:203`：`Show.01.ass` → `…S01ENone.ass` | `Show A S01ENone.01.ass`（`.01` 是"最后一个点之后"的后缀，被保留） | `test_processor_engine.py::test_the_builtin_bangumi_legacy_preset_still_renders_the_string_none` | **文档笔误**，已改；按冻结实现（`before_ass = filename[:rfind('.ass')]`、`last_dot = before_ass.rfind('.')`、`suffix = filename[last_dot:]`）回读确认 |
| 7 | （示例命令曾用 `kind=%K`） | 回调用 **7 个查询参数**；JSON body 被忽略 | `test_api_smoke.py::test_qbittorrent_hook_requires_a_token_and_matches_configured_libraries` | 静默失败最难查，已写进 §5.0 第 8 条与排障文档 |
| 8 | `permission_mode`（777） | **可选开启（默认关闭）**：chmod 只在显式开启时执行，所以**默认路径不 chmod**；开启时应用自己做等价动作，但**收窄到目标侧**（库根 + 本次处理到的每个一级文件夹的目标子树，含账本里体积一致而被跳过的旧作品），符号链接不跟进 | `test_permission_mode.py` 全套 9 条、`test_hardlink_native.py` 的入口签名指纹、`test_rule_validation.py` 的三处替身 | 现行口径：**默认关闭 + 界面二次确认 + 只动目标侧**（见下方修订理由） |
| 9 | （未写）`GET /api/processors` 的**顺序** | **内置优先**，内置之间按 `app/processors/defaults.py::BUILTIN_IDS`（`hardlink_only → bangumi → movie`），其余按 `id` 升序。SQL 只保证 `builtin DESC, id ASC`，最终顺序由 `app/processors/registry.py::list_processors()` 的显式 `sort_key` 决定 | `test_registry.py::test_list_processors_filters_and_orders`、`test_api_smoke.py::test_system_info_exposes_paths_and_processors`（`builtin_ids` 与 items 顺序一致） | **登记为契约点**：文档措辞必须是"内置优先（`BUILTIN_IDS` 顺序），其余按 id 升序"，不能写"按 id 排序" |
| 10 | （未写）处理器规格里 `log_file` 是否必填 | **非必填**：缺省 / `""` / `null` 都合法，语义是"请服务端生成"，由 `registry.next_log_file()` 填成 `<slug(处理器 id)>cache.log`（从内置模板建的自定义处理器**绝不**借用 `moviecache.log`/`bangumicache.log`）；只有含 `/`、`\`、`..` 或非字符串才报错 | `test_processor_spec.py::test_log_file_is_not_a_required_top_level_field`、`::test_log_file_is_optional_and_empty_means_let_the_server_generate`、`test_registry.py::test_create_processor_generates_its_own_log_file_and_never_borrows_a_builtin_one` | **登记为契约点**（媒体库侧同规则：`<log_dir>/<basename(log_file)>`） |
| 11 | （未写）处理器响应体形状 | `GET /api/processors/{id}` 返回**规格本身**（`id/name/kind/builtin` 与 `traverse`/`naming` **同层**），**没有** `spec` 包一层；媒体库响应里另有别名 `processor`（值 = `processor_id`） | `test_registry.py::test_get_processor_returns_none_and_require_raises_404`、`test_api_smoke.py::test_library_crud_and_validation_surface`、`test_processor_spec.py::test_reference_script_is_a_retired_field_and_is_silently_dropped` | 实现为真；`processor` 别名属**超集**，保留以兼容既有前端 |
| 12 | （未写）同一个处理器下**多个媒体库**的 `log_file` | **一个库一份日志**：建库不传 ⇒ 服务端分配唯一名（基名 = 处理器 spec 的 `log_file`，第一个 `bangumicache.log`、第二个 `bangumicache-2.log`；去重集合排除本处理器自己的登记名，所以第一个库不会变成 `-2`）；显式给的名字与别的库/处理器撞车 ⇒ **422** 字段级 `log_file`（并提示下一个可用名）；旧库回填按库 id 升序逐个去重 | `test_library_log_isolation.py` 5 条：`::test_two_libraries_on_the_same_processor_get_distinct_log_files`、`::test_the_second_library_is_not_fooled_by_the_first_librarys_processed_mark`、`::test_an_explicit_log_file_colliding_with_another_library_is_rejected`、`::test_a_path_like_log_file_is_still_rejected`、`::test_the_v1_to_v2_backfill_gives_every_library_its_own_log_file` | **登记为契约点**（这份日志就是「已处理」账本，见 `app/processors/engine.py:344`/`:408`，共用会让第二个库对同名同体积文件夹**静默跳过**；`docs/meta/EVIDENCE.md` 的表述正确） |
| 13 | 曾写「`accept_dirs` 的正则没有捕获组时 `season_raw` 为 `""`」（隐含"必须给捕获组"） | **`season_group` 允许显式 `0`** = 「只用这条正则判断接受、**不取季号**」；缺省仍是 `1`（所以"无捕获组 + 不写 `season_group`"依旧 **422**，只是消息给出两条出路）；`0` 的季号语义是 `season_raw == ""` 而 `season == "".zfill(2) == "00"`，与 `accept_dirs: []`（不配接受规则）**逐字同一条分支**；负数报 `season_group 必须是 0 或 >=1 的整数（0 表示只判断接受、不取季号）。` | `test_processor_spec.py::test_accept_dirs_without_a_capture_group_must_say_season_group_zero`、`::test_season_group_zero_and_in_range_values_validate`、`::test_season_group_out_of_range_is_still_rejected`、`::test_negative_season_group_is_rejected`、`test_processor_engine.py::test_season_group_zero_accepts_every_matching_subdir_and_leaves_season_raw_empty`（含"与 `accept_dirs: []` 产物逐字节相同"的对照） | **登记为契约点**（契约允许显式 `0`，`app/processors/spec.py:471-485` 已落盘；`docs/contracts/PROCESSORS.md` 按此改） |
| 14 | §2 曾写「硬链接要求源与目标在**同一个文件系统实例**上」、`mdadm/LVM` 行写「只要 `st_dev` 相同即可（**用 `stat -c '%d'` 验证**）」、B7 写「源目录 `:ro` 挂载；源树零改动」 | **内核判据是挂载身份**：`linkat(2)` 由 `do_linkat()` 比较 `old_path.mnt != new_path.mnt` ⇒ `EXDEV`，与 `st_dev`／`fsid`／子卷无关。因此「同一块盘被挂成两条独立 bind（例如 `/downloads:ro` + `/library`）」时 `st_dev` 相同、`link()` 依然失败（**硬链接永远建不出来**）；而只读源目录里的旧预检 `mkstemp` → `EROFS` → 会自我降级成 `ok:true, "已改用 st_dev 判断"`（**假绿**）。现行契约：`same_filesystem`/`probe` 是**三态 `bool \| None`**（`true` 已实证 / `false` 跨挂载 / `null` 无法实证）+ **只读安全**的实证探针（只读源目录、临时文件写目标侧、有界递归找样本）；源目录保护是「代码层不写源目录 + 任务前后**逐文件源完整性核对**（`Task.result.source_integrity`）」 | 现有：`test_no_source_mutation.py` 全套（audit hook + 快照）、`test_rule_validation.py::test_a10_source_tree_is_never_opened_for_writing`；三态/有界递归/只读安全/`source_integrity` 的用例在 `test_paths.py`（增三态、mount ID、只读安全、有界递归与快照用例）与 `test_hardlink_precheck.py`（14 条执行器/接口语义） | **文档侧已改**（§1、§2、B6、B7、§3.2 §2 行）：`/downloads:ro` 退场，**结构性保证 → 可验证的保证**。⚠️ **本测试套件不证明挂载行为**（那需要真实容器）：容器侧证据由部署侧提供 |

**第 8 条的结论依据**：硬链接与源文件**共享 inode**，对媒体库里的文件 `chmod` 会**同时改掉** `/vol1/1000/Download/qbittorrent` 下源文件的权限位（＝修改原始下载，红线见 B4/B5）—— 这个事实无法通过实现技巧消除。因此 `permission_mode` 的代价**只可能由用户显式承担**：① 默认关闭（`libraries.permission_mode` 默认 0，关闭时一个 `chmod` 都不做）；② WebUI **从关到开必须二次确认**，确认框写明会改到源文件权限位；③ 作用范围收窄到**目标侧**（库根 + 本次处理到的每个一级文件夹的目标子树，含被跳过的旧作品），绝不递归源目录、不跟进符号链接；④ 每次运行把"改了 N 个目录 / M 个文件"以 WARNING 写进任务日志，因为源完整性核对（B9，比 size/ino/mtime）**看不见权限位变化**。该契约见 `docs/contracts/BASELINE.md` §1.6 与 B10、`docs/contracts/ARCHITECTURE.md` §5.0 第 9 条。

### 3.4 实测输出摘要

**运行方式**（仓库根目录）：

```powershell
$env:PYTHONPATH = (Resolve-Path .tmp\pylibs).Path   # 可选：提供 fastapi/uvicorn/httpx
$env:PYTHONUTF8 = "1"
python tests\run_all.py                             # 全量
$env:MLM_ONLY = "api_smoke"; python tests\run_all.py # 只跑匹配的测试文件（开发期过滤）
```

**环境**：Windows + Python 3.14.7；`.tmp/pylibs` 提供 `fastapi 0.115.6 / uvicorn 0.32.1 / starlette 0.41.3 / httpx 0.28.1 / PyYAML 6.0.3`（与 `docker/Dockerfile` 的 pin **完全一致**）。该目录**没有 pytest**，因此走 `tests/_minipytest.py` 兜底 runner（纯 stdlib 子集）。

> 注意：跑 API 测试必须带上 `PYTHONPATH=.tmp\pylibs`，否则 `test_api_smoke.py` 会整文件跳过（`No module named 'fastapi'`）。

**全量实测**：

```
==== 427 passed, 5 skipped, 0 failed, 0 collect-error ====
$LASTEXITCODE = 0
```

**5 个跳过（跳过原因，全部是环境不可构造、有意跳过）**：

```
SKIP test_adapter_link::test_cross_filesystem_warns_but_still_runs_the_script_without_copying: 单文件系统环境，无法构造跨设备场景
SKIP test_no_source_mutation::test_source_directory_may_be_mounted_read_only: Windows 的目录只读位不阻止创建文件，只能在 POSIX 上验证
SKIP test_paths::test_same_filesystem_rejects_a_cross_device_pair: 单文件系统环境，无法构造跨设备用例
SKIP test_paths::test_probe_really_fails_across_devices: 单文件系统环境，无法构造跨设备用例
SKIP test_processor_engine::test_a_symlinked_file_is_renamed_from_its_own_name: Windows 上创建符号链接需要额外权限，本用例只在 POSIX 上有意义
```

**API 冒烟单独跑**（真实 `fastapi.testclient.TestClient`）：

```
==== 31 passed, 0 skipped, 0 failed, 0 collect-error ====
```

逐文件用例计数（同一次全量运行，含参数化展开；**PASS+SKIP 合计 432 = 427 + 5**）：

| 测试文件 | 用例数 | 其中 skip |
|---|---|---|
| `tests/test_processor_spec.py` | 61 | 0 |
| `tests/test_paths.py` | 56 | 2（跨设备） |
| `tests/test_processor_engine.py` | 34 | 1（符号链接，POSIX-only） |
| `tests/test_api_smoke.py` | 31 | 0 |
| `tests/test_registry.py` | 19 | 0 |
| `tests/test_rule_validation.py` | 18 | 0 |
| `tests/test_library_logs.py` | 17 | 0 |
| `tests/test_hardlink_precheck.py` | 14 | 0 |
| `tests/test_adapter_orphan.py` | 10 | 0 |
| `tests/test_adapter_scoped.py` | 10 | 0 |
| `tests/test_config_sync.py` | 10 | 0 |
| `tests/test_idempotency.py` | 9 | 0 |
| `tests/test_adapter_link.py` | 8 | 1（跨设备） |
| `tests/test_queue_serialization.py` | 8 | 0 |
| `tests/test_no_source_mutation.py` | 8 | 1（POSIX 只读目录） |
| `tests/test_library_log_isolation.py` | 5 | 0 |

> 上表只列现存的测试文件。判定产物（整棵树、`Renamed:` 行集合、日志键与体积、跳过集合、源树零改动）由**冻结的黄金值**守护：
> `tests/test_hardlink_native.py`（A 组五条 + `test_the_native_implementation_keeps_the_frozen_print_prefixes`）、
> `tests/test_processor_engine.py`、`tests/test_bangumi_episode_rules.py`、`tests/test_rename_extensions.py`、
> `tests/test_processor_spec.py`。

> 容器内验收请用 `python -m pytest tests -q`（`tests/conftest.py` 同时兼容 pytest 与兜底 runner）。

### 3.5 版本兼容性

**现行兼容性口径 —— 历史版本逐级升级的差异不再追溯，以下即今天的行为基线。**

**① API：`force=true` 只允许单作品。**

| 接口 | 现行行为 | 判定 |
|---|---|---|
| `POST /api/libraries/{id}/run` | `force=true` 只允许 `scope.mode=="folder"`（单个作品）；**整库 force ⇒ 400**，报错原文：「整库强制重跑已停用：force 只允许配合 scope.mode='folder'（单个作品）使用。要重做作品，请到「作品记录」里删除对应作品的记录，下次运行时这些作品会自动补做，其余作品不受影响。」 | ⚠️ **唯一的破坏点**：整库 + `force=true` 的调用收到 400。停用理由是整库 force 会让建链阶段把**已经改过名**的文件按**原名**再链一份（同一 inode 的第二个目录项，见 [KI-7](../meta/KNOWN_ISSUES.md#ki-7-升级后首次全量运行会重链并可能多出-2后缀)） |
| `GET /api/libraries/{id}/works` | 作品清单 + 两本账差集提示；可选查询参数 `q`（作品名子串匹配，ASCII 大小写不敏感，`%` / `_` / `\` 按**字面**处理）；`limit` 由 `clamp_limit` 夹在 `1..200`，默认 **50**（非法 / `0` / 负数回落 `50`）；响应含 `q`（回显生效的搜索词）与 `ledger_total`（本库账本**总条数**），`total` = **命中 `q` 的条数**；搜索与分页都在**服务端**做 | ✅ 不破坏既有调用。⚠️ 把 `total` 当"本库作品总数"读的调用方请**改读 `ledger_total`**（只有**不传 `q`** 时 `total` 才与本库总数相等）；不传 `limit` 时每页上限为 50，需要更多请显式传 `limit`（上限仍是 `200`）。`q` 是**可选**参数，不传即不过滤 ⇒ **向后兼容** |
| `POST /api/libraries/{id}/works/delete` | `dry_run` 预演 / `confirm` 二次确认 | ✅ 不破坏既有调用 |
| `POST /api/libraries/{id}/works/rename` | 202 + Task，只对勾选作品立即重命名、不建链 | ✅ 不破坏既有调用 |
| `DELETE /api/libraries/{id}` | 响应含 `link_records_removed`（同时清掉本库账本；重命名缓存**不动**） | ✅ 新增字段，既有字段未变 |
| `GET /api/todos` | 可选查询参数 `q`（按作品**朴素子串**筛 `title` / `detail` / `dedupe_key` 三列；前后空白 `strip`，空 / 纯空白 = 不过滤；ASCII 大小写不敏感；`%` / `_` / `\` 按**字面**处理）；`total` = **过滤后**条数 | ✅ **向后兼容**：不传 / 传空 ⇒ 不过滤；**任何取值都不会 400** |
| `GET /api/todos/summary`、`/detect`、`/detect-resolve`、`/resolve`、`/reopen` | `q` **不影响**任何计数端点 | ✅ |

**② WebUI 变化不算 API 变化。**

- 「作品记录」是**独立页面**：hash 路由 `#/works`、`#/works/<library_id>`，hash 查询参数 `?q=&page=&limit=`，每页 **25 / 50 / 100 / 200**（默认 **50**）；每个媒体库旁边的「作品记录」按钮**跳转**到该页。页面调用的仍是同一个 `GET /api/libraries/{id}/works`，**HTTP 契约一个字段都没变** —— 只跑接口的调用方不受影响。
- 待办看板的筛选栏是**四个**筛选（含「作品」输入框 + 「清空」，输入停 0.4 秒自动筛选）。它调用的仍是同一个 `GET /api/todos`，**只是多带一个可选参数** —— 只跑接口的调用方不受影响。
- WebUI 启动时默认页只渲染一次：页面清理器不会被后一次渲染覆盖，非仪表盘页面**不会**在几秒后被 8 秒轮询弹回仪表盘。

**③ 数据与操作语义。**

- `link_records` 表（每库「已执行作品」账本，唯一键 `(library_id, folder)`）由**启动时自动补建**：新装库走 `SCHEMA`、老库走 `migrate()` 的 `_ensure_link_records_table()`（`app/db.py:410`），两处**同一份 DDL**（`_LINK_RECORDS_COLUMNS`，`app/db.py:86`）。**没有需要手工执行的迁移步骤、也不需要改任何配置**。
- 账本是**作品级**的，**不保证文件级完整性**：目标目录里被手动增删改的文件不会被发现、也**不会**被"修复"，你在目标目录里的修改**一律保留**；
- **唯一的重做途径 = 在「作品记录」（`GET /api/libraries/{id}/works`）里删掉该作品的记录** —— 删除会连带删掉目标目录里整个作品文件夹（源目录不受影响），下次运行自动补做该作品，**其余作品不受影响**；
- 因此"单独重做某一个作品"的正确做法是删记录，而不是整库 `force`（后者不提供，见 ①）。
- **不回填**历史 `hard_links.log`：旧日志是**全局**的、不带库/目标维度（键 = 源目录 + 作品名 + 体积），无法可靠地还原成"某库某作品已处理"。因此已有媒体库的账本**从空开始**，其**第一次全量运行**会把每个作品重新建链；已在目标目录里改过名的文件会以**原名**再链一份（同一 inode 的第二个目录项），重命名阶段于是多出 `xxx (2).mkv` 后缀。**不丢数据**（硬链接、源目录不受影响），规避办法见 [KI-7](../meta/KNOWN_ISSUES.md#ki-7-升级后首次全量运行会重链并可能多出-2后缀)。
- 响应字段语义（`total` = 命中数）**只影响调用方怎么读** —— 磁盘上的账本、`hard_links.log`、重命名缓存、待办台账与目标目录**一个字节都不动**。

**④ 硬链接与重命名结果本身不因账本判据改变。** 建链与重命名的实现只有一份：`app/processors/hardlink.py` 与引擎规则；`tests/test_hardlink_native.py` 以冻结黄金值守护两种打印行（`已记录文件夹体积: <folder> - <size>`、`文件夹已记录且体积一致，跳过: <folder>`），**逐字未变**。账本判据 = **本库 + 同 `source_dir` + 同 `target_dir` + 同体积**，只影响"哪些作品被跳过"，不影响"链出来的东西长什么样"。`hard_links.log` **照写**（兼容既有工具与审计：体积登记行、孤儿目录清理行，格式与解析契约未变），只是**不再用于跳过判断**（[`docs/contracts/BASELINE.md`](BASELINE.md) B5）。

**⑤ 「坏名字」的处理与留痕。**

| 场景 | 现行行为 | 判定 |
|---|---|---|
| 命名段序列里写了上下文没有的变量名（例如 `{"param": "no_such_name"}`） | 那一段**什么都不输出**、文件名少一段、任务仍 `success`；留痕齐全：`result.unknown_params` + `samples.unknown_params` + 每个文件一条 WARNING 日志 + 一条 `warnings` 汇总告警 + 收尾摘要「命名未知变量 N，」 | ✅ **不报错**（只是可见） |
| `episode_fallback` 为空串（`""` / 纯空白） | 与 `null` **等价**（= 没配兜底）⇒ 命名引用集数且全部未命中时**保留原文件名**，不会渲染出 `… S01E00.mkv` | ⚠️ **有意修复**（字段口径见 [`docs/contracts/API.md`](API.md) §5.7.1）："清空输入框"不算兜底值；显式配了**非空**字符串（含 `legacy` 预设的 `"None"`）的库**行为不变** |
| 命名渲染出**空文件名** | 跳过该文件 + 中文告警「段序列渲染出空文件名」 | ✅ |
| `POST /api/libraries/{id}/works/rename`（「立即重命名」）而该库**目标目录不存在/被移走** | **提前 400** 并提示「请先运行一次本库（会自动创建目标目录并建立硬链接）」 | ⚠️ **有意加固**：本接口不建链、也不创建目标根，继续跑只会产出一堆误导记录；建链路线（`POST /{id}/run`）会自动创建目标根，**不受影响** |

- **保存期校验（`strict_names`，默认 `True`）**：API 保存处理器、保存库级覆盖、配置文件导入时，段序列里的 `param` 与模板里的 `{名字}` 都必须在可用名单内（`TEMPLATE_VARS` 12 个 + `episode_resolved` + 该 spec `naming.expressions` 定义的名字），否则 **422**。**运行路径显式放宽**（`engine.run_rename` 用 `strict_names=False`）—— 库里早先存下的老配置、人工改过的 YAML 不会因为一次重跑而失败，它们由上面那套留痕负责。⚠️ 老配置**重新保存**时会被拦住（要么改对名字，要么在 `naming.expressions` 里定义它）—— 这是刻意的口径。✅ 两种写法的校验口径一致：库级覆盖的模板同样接受 `{episode_resolved}` 与"处理器里定义的表达式名"（不会误判成未知变量）。
- `episode_fallback` 的归一发生在**读规格时**（内存里），**不改写**库里已存的 JSON（下次保存该处理器时才会写成 `null`）；`unknown_params` 只进任务 `result` JSON 与日志表，**不新增列**（`tasks.result` 本来就是 JSON 文本）。
- `hard_links.log` 的 `创建硬链接失败: <目标> - <异常>` **允许同一目标出现多行**（失败项逐次留痕）。

**⑥ 建链记账与孤儿清理。**

- **含 `" - "` 的一级文件夹：失败的账不再算错。** 第二遍起不再记 `link_failed>0`（任务因此不再显示失败）—— 这些"目标已存在且就是同一 inode 硬链接"的条目判成 `already_linked`（计入 `skipped`）。**日志行数与"每轮重复走一遍建链"的现象不变**（欠账见 `docs/contracts/BASELINE.md` L2）。
- **`orphan_cleanup = false` 的本意。** 配了账本的库在"源作品文件夹已被删掉"之后，第二次运行**不会**删除目标侧对应文件夹。如果你其实希望清掉这些目录：打开该库的孤儿清理再跑一次，或手动删除目标文件夹并到「作品记录」里删掉账本记录（两者都会让下次运行重新建链）。

**⑦ 可观测项（不是失败）。**

- 待办看板含第六种待办 **`unscoped_works`**（扁平结构里无法归属到任何作品的文件夹，见 [`docs/contracts/ARCHITECTURE.md`](ARCHITECTURE.md) §待办与 [`docs/meta/KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-1）：重跑整库后看板可能多出若干条；
- 待办样本不受「只留前 100 条」的上限截断（`samples` 完整返回）；
- `RenameOutcome` 的部分字段只走内存与任务结果 JSON（`Task.result` JSON 与日志表，无列结构变化）。

**⑧ 容器内路径与仓库目录。** entrypoint 与健康检查脚本安装到 `/usr/local/bin/mlm-entrypoint`、`/usr/local/bin/mlm-healthcheck`；容器内应用根是 **`/srv/mlm`** —— ⚠️ **任何写死路径的地方都要与之一致**：`docker exec` 排障命令、外部监控探针、自定义 cron 里的应用根路径与 `PYTHONPATH`。仓库根分区明确：工具链在 `scripts/`、报告在 `reports/`，容器内路径与仓库目录的对照见 [`docs/meta/STRUCTURE.md`](../meta/STRUCTURE.md)。`/data` 数据卷布局（`mlm.db`、`config/`、`logs/`、`secret.key`）、端口、环境变量、HTTP API、数据库表结构不受影响。

---

## 4. 平台行为差异（Windows 测试环境 vs Linux 运行容器）

应用**只在 Linux 容器里运行**；但测试套件在 Windows 上也能跑，所以**同一段逻辑在两个平台上的表现差异**必须写清楚 —— 这些差异**只影响测试的判定方式**，不影响容器里的真实行为。

| 行为 | Linux（容器，真实行为） | Windows（仅测试） | 影响 |
|---|---|---|---|
| `os.rename` 目标已存在 | **引擎不走这一步**：L2「防丢」在 `os.rename` **之前**就按体积定好终名（加 ` (2)`… 后缀 / 原有文件让位 / 保留原名），**两种平台都不覆盖** | 若绕过引擎直接用 `os.rename`：报 `[WinError 183]`、目标保留（该平台差异已被 L2 提前化解） | 测试断言"引擎按 L2 定名、**不覆盖**"——现行守卫见 `test_processor_engine.py` 的碰撞用例，L2 的引擎侧用例见 `test_processor_engine.py::test_a_within_run_collision_keeps_both_files_with_a_suffix` 等；**不要**把 Windows 的行为当成"应用的行为" |
| 目录的 `st_size` | 通常 4096（与内容无关） | 也非 0/不确定 | 测试不依赖目录体积 |
| 目录的只读位（`chmod 0o555`） | 真的阻止在其中创建文件 | **不阻止**（只影响删除权限位） | 有一条 POSIX-only 用例在 Windows 上**跳过**（`test_no_source_mutation.py`） |
| 跨文件系统构造 | 可用挂载点/`/dev/shm` 等构造 EXDEV | 需要第二块盘/卷，通常没有 | 3 条跨设备用例在只有单文件系统的机器上**跳过**（不是失败） |
| 符号链接 | 普通用户可以创建 | 需要管理员权限或开发者模式 | 1 条符号链接用例在 Windows 上**跳过**（`test_processor_engine.py`） |
| `os.link` 的源 `st_nlink` | 建链后 1 → 2 | 同 | 测试断言"恰好 +1"，并校验 `(st_dev, st_ino)` 相同 |
| 路径分隔符 | `/` | 同时接受 `\` | 应用内部统一转 `/`（`paths.normalize`）；日志里写的是规范化后的路径 |
| 文件系统大小写 | 敏感（ext4） | 不敏感（NTFS 默认） | 测试用 `.MKV` 验证"大小写敏感白名单"时，在 Windows 上也能得到正确结论（因为建链实现与规则引擎都做**字符串判断**，与文件系统无关） |

---

## 5. 外部依赖兼容性

| 依赖 | 兼容范围 | 说明 |
|---|---|---|
| Docker / Compose | Docker 20.10+ 与 Compose v2（`docker compose`） | 使用 `env_file: .env`、`restart: unless-stopped`；`docker-compose.yml` 未用 swarm 专有字段 |
| FastAPI / Uvicorn / Starlette / httpx | `fastapi==0.115.6`、`uvicorn[standard]==0.32.1`、`starlette 0.41.3`、`httpx 0.28.1`（`docker/Dockerfile` 与测试用 `.tmp/pylibs` **完全一致**） | API 测试在两类环境里跑同一套断言 |
| PyYAML | `PyYAML==6.0.3`（**必须 pin**：6.0.2 在 Python 3.14 上没有 wheel，pip 会回落源码构建） | 配置是 YAML（[`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §6.1）：`yaml.safe_load` / `yaml.safe_dump(allow_unicode=True, sort_keys=False, default_flow_style=False)`，UTF-8 无 BOM，首行 `schema_version: 2` |
| qBittorrent | 支持「运行外部程序」的 4.x（WebUI/Nox 均可） | 回调用 7 个查询参数 `token/name/hash/category/tags/content_path/save_path`（`kind=` 仍被接受但不再使用）；**匹配条件全空的库永不命中** |
| qBittorrent WebAPI | 用于「设置 → 测试连接」 | 连不上时 `POST /api/settings/test-qbittorrent` 返回 **HTTP 200** + `{"ok":false,...}`（连通性失败不是 HTTP 错误码） |
| 浏览器 | 需要支持 `EventSource`（SSE）与 `fetch` 的现代浏览器 | 任务实时日志走 `GET /api/tasks/{id}/stream`（`event: task` → `event: log` → `event: done`）；反代需关闭缓冲 |
| 时区 | `TZ`（默认 `Asia/Shanghai`） | 库里存 UTC ISO 串，**所有对外时间**（任务/日志/库/处理器的 `created_at`/`updated_at`）都由 `app/serializers.py::format_time()` 渲染成本地 `YYYY-MM-DD HH:MM:SS`；重命名日志里的 `timestamp` 也是本地 `YYYY-MM-DD HH:MM:SS`（与冻结基线一致） |
| 认证 | 单管理员账号，scrypt 哈希，会话 Cookie `mlm_session`（`HttpOnly; SameSite=lax`，7 天） | 未设 `Secure`；反代终止 TLS 的部署未验证（见「未覆盖项」） |

---

## 6. Windows 上的测试覆盖边界

本仓库的测试套件可以在 Windows 上完整运行（有 pytest 就用 pytest，没有就用纯标准库兜底 runner）：

```powershell
$env:PYTHONPATH = (Resolve-Path .tmp\pylibs).Path   # 可选：装了 fastapi/httpx 才能跑 API 测试
$env:PYTHONUTF8 = "1"
python tests\run_all.py
```

**当前基线：`427 passed, 5 skipped, 0 failed`。** 5 个跳过全部是**环境不可构造**，且都是**有意跳过而非"假装通过"**：

| 跳过的用例 | 原因 |
|---|---|
| `test_paths::test_same_filesystem_rejects_a_cross_device_pair` | 单文件系统环境，无法构造跨设备用例 |
| `test_paths::test_probe_really_fails_across_devices` | 同上 |
| `test_adapter_link::test_cross_filesystem_warns_but_still_runs_the_script_without_copying` | 同上（跨设备只**警告**并照跑，逐文件报 `EXDEV`） |
| `test_no_source_mutation::test_source_directory_may_be_mounted_read_only` | Windows 的目录只读位不阻止创建文件，只能在 POSIX 上验证 |
| `test_processor_engine::test_a_symlinked_file_is_renamed_from_its_own_name` | Windows 上创建符号链接需要额外权限 |

也就是说：**跨设备/跨挂载的逐文件失败路径、只读源目录、符号链接文件**这三类与文件系统强相关的路径，在没有真实挂载的测试环境里**没有被执行过** —— 它们在容器/POSIX 上才会真正被验证（验收时请以容器内的 `python -m pytest tests -q` 为准）。三态判据的**逻辑**不依赖真实挂载：`false`（注入 `EXDEV`）与 `null`（注入无法解释的 `errno`、空源树、不可写目标）都有可执行的注入用例，见 `tests/test_paths.py` 与 `tests/test_hardlink_precheck.py`。

---

## 7. 未覆盖项与原因

| 未覆盖项 | 原因 |
|---|---|
| **Windows / macOS 上真跑任务** | 应用按容器内路径（`MEDIA_ROOT` 下的源/目标路径，单挂载形态下 = 宿主真实路径）配置；`app/executor.py` 直接对这些路径 `os.path.isdir()` 并交给应用自己的建链实现（`app/processors/hardlink.py`）与规则引擎（`app/processors/engine.py`）。在 Windows 测试环境里它们不是真实挂载点 ⇒ 跑不起来。**这是环境限制，不是缺陷。** 测试用临时目录当源/目标。 |
| **跨文件系统/不支持硬链接的文件系统上的任何"降级方案"** | 设计上不提供复制回退（B8）。见 §2。 |
| **非 UTF-8 文件名 / 非 UTF-8 locale** | 容器固定 UTF-8（B10）；未在 GBK 等 locale 下验证，也未测试含无效 UTF-8 字节的文件名。 |
| **在源目录里"找不到可用文件样本"时给出确定结论** | 预检的实证探针只在**有界范围**内找样本（深度 ≤3、条目 ≤512、不跟随符号链接），找不到就返回 **`null`（无法实证）** 而**不是** `true`；应用**不会**为了让它变绿去源目录里造测试文件（那正是假绿的来源，见 §2 与 [`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md) 场景 21）。 |
| **SMB/NFS/FUSE/mergerfs（网络或联合文件系统）** | 硬链接语义不可靠，`st_dev`/`st_ino` 也可能失真；未验证，按"不支持"处理。 |
| **多实例 / 多 worker 并发** | SQLite + 单工作线程 + 日志"读-改-写整份"（L1/B9）都假定单实例。 |
| **已知边界行为**（L1–L9） | 它们是**冻结契约**，应用不修，只能识别与绕开（见 §3.1 与 [`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md) 场景 5/15/16）。 |
| **规模/性能上限** | 未做压力测试；首跑十万级文件会很慢（全局串行 + 每次整份读写日志）。 |
| **反向代理 / HTTPS / 域名访问** | 会话 Cookie 未设 `Secure`，SSE 经反代可能被缓冲；未验证。 |
| **`permission_mode`（777）—— 默认关闭的显式开关** | 应用调用的入口函数（`app/processors/hardlink.py` 的 `create_hard_links()`）不带任何 `chmod` 副作用 ⇒ **默认路径下不发生任何 `chmod`**。把它做成默认关闭的可选开关 —— 硬链接与源共享 inode 的代价虽无法消除，但它确实解决了非 root 服务读不到库内文件的真问题；代价由开启者显式承担（界面二次确认 + 任务日志留痕 + 只动目标侧）。见 §3.3 第 8 条、`docs/contracts/BASELINE.md` §1.6 与 B10、`docs/contracts/ARCHITECTURE.md` §5.0 第 9 条。 |
| **自动创建管理员账号（`.env` 里的 `ADMIN_PASSWORD`）的覆盖语义** | 只在"一个用户都没有"时创建；数据库已有用户时不覆盖。重置口令请走 [`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md) 场景 11。 |
| **带着旧宿主路径的 `hard_links.log`** | 应用不改写日志里的 `Source:` 前缀；每个媒体库有自己的重命名日志。遇到这类日志只能**删掉它让建链阶段重建一次**来修复（见 [`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md) 场景 5 与 `docs/contracts/BASELINE.md` §4.1）。建链的跳过判断由每库账本 `link_records` 承担，这份日志**只写不读**；账本同样**不回填**历史日志（原因与影响见 §3.5 ③）。 |
| **从旧日志还原"某库某作品已处理"** | 旧 `hard_links.log` 是**全局**的、不含库与目标目录维度（键 = 源目录 + 作品名 + 体积），无法可靠映射到"哪个库、链到哪里"；应用**刻意不做**这种回填。后果与规避见 §3.5 ③ 与 [`docs/meta/KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-7。 |

## 8. 最小待确认项

请在你的真实环境上确认下面这几条（它们决定"你的场景是否落在支持范围内"）：

| # | 待确认项 | 怎么确认 |
|---|---|---|
| 1 | 下载目录与媒体库目录是否在**同一个挂载**里（同卷/同存储池**还不够** —— 见 §2） | **【NAS 宿主】** `findmnt -T /vol1/1000/Download/qbittorrent -o TARGET,SOURCE` 与 `findmnt -T /vol1/1000/Library -o TARGET,SOURCE`，两条的 `TARGET` 必须**相同**（`stat -c '%d'` 相同**不能**作为依据） |
| 2 | 目标文件系统是否**真的支持硬链接** | **【NAS 宿主】** 在目标目录里 `touch a && ln a b && ls -li a b`（两个 inode 号相同即支持），测完删掉 |
| 3 | NAS 的 CPU 架构（决定是否需要 ARM64 构建） | **【NAS 宿主】** `uname -m` |
| 4 | 你现有媒体库是怎么命名的（决定是否会有"新旧不一致"） | 抽样几个文件名，对照 §3.1 的命名表；有 `ENone` 说明历史文件里已有"无集数"的产物 |
| 5 | qBittorrent 版本与「运行外部程序」的占位符支持 | qBittorrent → 选项 → 下载 → 运行外部程序；参考 §5 与 [`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md) 场景 8 |
| 6 | 是否需要经反向代理/HTTPS 访问（影响 SSE 与 Cookie） | 若经反代：确认关闭响应缓冲、并接受 Cookie 未设 `Secure`（见「未覆盖项」） |
| 7 | 数据卷所在文件系统与剩余空间（日志/DB/配置/secret.key 都写这里） | **【NAS 宿主】** `df -h /vol1/1000/Docker/media-ingest/data` |
| 8 | （无待确认项） | 健康检查**六项**（`db` / `data_dir` / `downloads_readable` / `library_writable` / `processors` / `config`）；判定产物由**冻结黄金值**守护（见 §3 开头与 [`docs/contracts/BASELINE.md`](BASELINE.md) §4） |
