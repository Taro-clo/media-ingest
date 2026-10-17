# 行为基线（当前行为契约 + B 编号索引）

> **定位**：本文登记**当前实现必须满足的可观察行为**，以及两套**编号索引** ——
> **B1–B13**（实现契约，§4）与 **L1–L9**（已知限制，§5）。这两套编号是代码注释、测试注释与
> 其它文档共用的引用锚点，因此**编号不重排、不复用**；新增条目一律往后排。
>
> **本文只登记"行为是什么"**，不重复其它文档的推导过程：
>
> | 要知道 | 去哪看 |
> |---|---|
> | 处理器规格、规则语义、预设与增量 | [`docs/contracts/PROCESSORS.md`](PROCESSORS.md)（唯一真源） |
> | 同一挂载 / `linkat` / `st_dev` 的论证、文件系统支持矩阵 | [`docs/contracts/COMPATIBILITY.md`](COMPATIBILITY.md) §2 |
> | 测试 ↔ 契约条目的逐条映射 | [`docs/meta/TEST-MAP.md`](../meta/TEST-MAP.md) |
> | 每条契约的验证证据（E 编号） | [`docs/meta/EVIDENCE.md`](../meta/EVIDENCE.md) |
> | 目录结构、部署、排障 | `docs/meta/STRUCTURE.md`、`docs/guides/DEPLOYMENT.md`、`docs/guides/TROUBLESHOOTING.md` |
>
> **红线**：应用自身的配置与测试样本独立存放；**任何代码路径都不得写源目录**（§4 的 B5）。
>
> **路径写法**：文中 `<MEDIA_ROOT>` 指 compose 的 `${MEDIA_ROOT:-/vol1/1000}`（容器路径 = 宿主路径）；
> `<LIBRARY_ROOT>` 指某个媒体库的 `target_dir`；`<NAS_IP>` 指部署机的地址。这些都是**可替换占位**，
> 不是本项目的固定事实。

---

## 0. 整体数据流

```
qBittorrent 完成下载
        │
        ▼
<MEDIA_ROOT>/Download/qbittorrent/{Bangumi,Movie}/<一级文件夹>/...      ← 源目录（只读语义）
        │   app/processors/hardlink.py   （os.link，不改动源文件）
        ▼
<LIBRARY_ROOT>/{Bangumi,Movie}/<同名一级文件夹>/...                   ← 与源同 inode
        │   app/processors/engine.py     （原地 os.rename）
        ▼
Emby 读取
```

硬链接与重命名是两条独立链路，靠**日志**解耦：硬链接把「已处理过的一级文件夹」记进日志与每库账本，
重命名读自己的库日志判断某个标题文件夹是否处理过。

---

## 1. 硬链接契约

### 1.1 入口与签名

```python
def create_hard_links(source_dir, target_dir, *, skip: dict[str, int] | None = None,
                      permission_mode: bool = False) -> None
```

- `skip=None`（默认）⇒ 跳过判据来自**全局硬链接日志** `read_logged_folders(source_dir)`；
- 传入 `skip` ⇒ 判据来自**每库账本**（见 §4 的 B10 与 §1.4 末尾的说明）；
- `permission_mode` ⇒ 见 §1.6（默认关闭）。

签名、副作用、打印顺序与日志格式都是冻结的契约，由 `tests/test_hardlink_native.py` 的
**冻结黄金值**断言。

### 1.2 日志格式（解析契约）

用 Python `logging` 写入，格式串为 `'%(asctime)s - %(message)s'`。写入记录：

```
2026-10-09 01:20:11,123 - Source: <source_dir> - Folder: <folder> - Folder size: <int>
```

解析规则**必须逐字保持**：

1. 只处理同时含 `' - Source: '`、`' - Folder: '`、`' - Folder size: '` 的行；
2. `line.strip().split(' - ', 1)` → `(timestamp_part, message)`；
3. `message.split(' - ')` **必须恰好得到 3 段**，否则 `except (ValueError, IndexError): continue` 静默忽略该行；
4. `int(size_part.split(': ')[1])`；
5. **只保留 `source_dir_from_log == source_dir` 的记录** —— 同一日志文件可服务多个源目录，按源目录隔离。

> ⚠️ 蕴含约束：**路径或目录名里含 `" - "` 会破坏解析**，该行被静默忽略。属既有行为，不修正
> （后果与修法见 [`docs/contracts/COMPATIBILITY.md`](COMPATIBILITY.md) §3.1 的对应行）。
> 应用另走一条结构化通道（`hardlink.last_failures()`）拿失败项，于是这类文件夹的失败也能正确改判成
> `already_linked`；**解析本身仍逐字不动**。

### 1.3 孤儿清理（破坏性，必须保留）

对日志中记录的每个 `folder`，若 `source_dir/folder` **不存在**，则：

```python
shutil.rmtree(target_folder)                                    # 递归删除目标硬链接文件夹
logging.info(f"Deleted orphaned hard link folder: {target_folder}")
print(f"Deleted orphaned hard link folder: {target_folder}")
del logged_folders[folder]
```

**这是既定行为，本项目不改变其判定逻辑**，但：

- 应用使用**自己的日志副本**，因此只会清理"本应用自己建立过链接"的文件夹；
- 提供显式开关 `orphan_cleanup`（默认 `true` = 保持原行为），关闭时应用不得执行该分支；
- 每次触发在任务日志中以 **WARN** 级显著记录被删除的路径。

### 1.4 主流程

| 行为 |
|---|
| `first_level_folders = [f for f in os.listdir(source_dir) if os.path.isdir(os.path.join(source_dir, f))]` —— **只处理第一层文件夹** |
| `folder_size` = 递归求和（`os.walk` + `os.path.getsize`），**不排除符号链接**，空文件夹为 `0` |
| 若 `folder in logged_folders and logged_folders[folder] == folder_size` → `print("文件夹已记录且体积一致，跳过: {folder}")` 并 `continue`（**幂等性的唯一来源**，见 B7） |
| 体积不一致 → 重写日志文件删掉该 folder 的**所有**匹配行（子串匹配 `f"Source: {source_dir} - Folder: {folder} - Folder size:"`，读-改-写整份） |
| `os.makedirs(target_folder, exist_ok=True)`；递归 `os.walk(source_folder)` 并用 `os.path.relpath` 复原层级 |
| `os.link(source_file, target_file)` —— **硬链接，绝不复制** |
| 成功 → `print(f"已创建硬链接: {target_file}")` |
| 任何异常 → `print(f"创建硬链接失败: {target_file} - {e}")`，**继续处理下一个文件**（不中断） |
| 完成后 `logging.info(f"Source: ... - Folder: ... - Folder size: ...")` 并 `print(f"已记录文件夹体积: {folder} - {folder_size}")` |

**目录层级契约**：`target/<folder>/<相对源文件夹的完整子路径>/<同名文件>` —— 逐字节保留源侧的相对层级
与文件名，不做任何清洗/重命名。

> ⚠️ **跳过判据的来源（应用侧）** —— 上表描述的是原生实现的行为，逐字不变。应用侧把"已处理过"的
> 判据从**全局日志**搬到了**每库账本**（`app/link_records.py`，表 `link_records`）：
>
> - **不变**：两种打印行（`已记录文件夹体积: <folder> - <size>`、`文件夹已记录且体积一致，跳过: <folder>`）
>   与 `Source: … - Folder: … - Folder size: …` 记账行**逐字不变**，日志**照写**；只遍历一级目录、
>   目录层级契约、`os.link`、`os.makedirs` 与命名/后缀规则全部不变。
>   `create_hard_links(..., skip=None)` 的默认路径（读全局日志）逐字一致。
> - **变**：应用传入 `skip` 时判据来自账本，键为 `(library_id, folder)`，且必须
>   「本库 + 同 `source_dir` + 同 `target_dir` + 同体积」四项**全部匹配**（B10）。日志因此从"判据"
>   降为"**兼容与审计**"。

### 1.5 关键边界条件

| 场景 | 行为 |
|---|---|
| 目标文件已存在（`os.link` → `FileExistsError`） | 被 `except` 捕获并打印失败，**不覆盖、不删除**。重跑靠 §1.4 的跳过分支；强制重跑会产生一批无害的 `FileExistsError` |
| 源与目标跨挂载 / 跨文件系统 | `os.link` → `OSError(EXDEV)`，只打印失败，**绝不回退为复制**（B8） |
| 源目录不存在 | `os.listdir` 抛 `FileNotFoundError`，**未捕获，直接向上抛出** |
| 空文件夹 | `folder_size == 0`；首次会 `makedirs(target_folder)` 后记录 `0` |
| 符号链接 | `os.walk` 默认不跟随目录符号链接；文件符号链接会被 `os.link` 直接链接 |
| 权限 | `os.link` 继承源 inode 的权限与属主 |

### 1.6 权限模式（`chmod 777` 是默认关闭的显式开关）

> ⚠️ **B13**：`permission_mode` **默认关闭、开启前必须二次确认**。
> 硬链接与源文件**共享同一个 inode**，`chmod` 目标侧的硬链接会**同时改掉**源目录下对应文件的权限位；
> 它确实解决了「非 root 服务（如 Emby）读不到库内文件」的真问题，所以改为**由用户显式承担**这个代价。
> 实现 `app/processors/hardlink.py`，测试 `tests/test_permission_mode.py`。边界：
>
> - 目标权限位 `0777`（`PERMISSION_MODE_DIR` / `PERMISSION_MODE_FILE`），只作用于**目标侧**：
>   库根目录本身、本次处理到的每个一级文件夹的目标侧整棵子树（**含"体积一致 ⇒ 跳过"的旧作品**）；
> - **绝不**递归或改动源目录、**绝不**跟进符号链接（`os.chmod` 默认会跟进，那会改到库外面的文件）；
> - 关闭时（默认）**一个 `chmod` 都不做**（不 chmod、不 stat 权限位）；
> - 源侧权限位的变化**不**触发 B5 的源完整性核对 —— 它比的是
>   `(相对路径, st_size, st_ino, st_mtime_ns)`，不比权限位；但任务日志会以 WARNING 记录
>   「已把 N 个目录 / M 个文件修正为 0777，源文件权限位随之改变（大小、inode 号、mtime 不变）」；
> - 硬链接天然继承源文件权限（`os.link` 语义）；若只想放宽权限而**不愿**影响源文件，
>   请在**宿主上**对目录显式操作（见 [`docs/guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md)），
>   并清楚它同样共享 inode；
> - 重命名（`os.rename`）只改目录项、不碰 inode，因此不受此约束影响。

### 1.7 默认路径对（示例值，按你的部署替换）

```
<MEDIA_ROOT>/Download/qbittorrent/Bangumi  →  <LIBRARY_ROOT>/Bangumi
<MEDIA_ROOT>/Download/qbittorrent/Movie    →  <LIBRARY_ROOT>/Movie
```

---

## 2. 重命名契约

### 2.1 配置常量

| 常量 | 值 |
|---|---|
| `ROOT_DIR` | 媒体库的 `target_dir` |
| `LOG_DIR` | `settings.log_dir`（默认 `/data/logs`）—— **不按媒体库分子目录** |
| `LOG_FILE` | `<LOG_DIR>/<该媒体库的 log_file>`（每库一份，见 `PROCESSORS.md` §5） |

### 2.2 缓存日志格式

JSON 字典：

```json
{ "<一级文件夹名>": { "size": <int>, "timestamp": "YYYY-mm-dd HH:MM:SS", "works_dir": "<绝对容器路径>" } }
```

- 读失败（`FileNotFoundError` / `JSONDecodeError`）→ 返回 `{}`；
- 写：读-改-写整份（`open('w')`），`json.dump(..., ensure_ascii=False, indent=4)`；**读日志时必须忽略未知键**；
- 跳过条件：`folder in log_data and log_data[folder]["size"] == get_folder_size(folder_path)` →
  `print("Skipping folder: {folder} (already processed at {timestamp})")`，跳过该一级文件夹
  （**唯一的"已处理"判据**）。

### 2.3 季文件夹识别

`SEASON_PATTERN.fullmatch(entry)`：**fullmatch ⇒ 只接受恰好 `Season <数字>`**；
`Season 1 (2018)`、`Season1`、`SP` 均不匹配。

### 2.4 剧集号提取

现行默认（`default` 预设）是 **9 条带边界约束的有序规则**（见
[`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §3.2 增量 ④，没有"抓到数字就算集号"的分支），
`episode_fallback` 是 `null`（增量 ③）⇒ 提取不到集数就**保留原文件名**，不会产生 `ENone` 这类字面量。

`legacy` 预设换回"最朴素的旧口径"：两条正则 + `episode_fallback: "None"`，行为如下：

1. 第一条正则 `search(filename)`（**search，非 fullmatch；按文件名中最早出现的匹配**）；
2. 命中 → 依次取第 1→8 个分组中**第一个非空**的，`zfill(2)` 返回；
3. 未命中 → 第二条正则 `search` → 取其分组 `.zfill(2)`；
4. 都未命中 → 返回字符串 `"None"`（**注意是字符串，会进入文件名**）。

> 交替顺序即优先级：`第…集` > `[NN]` > `EpisodeNN` > `EPNN` > `eNN` > `ENNN` > `#NN` > `-NN`。
> `(?i)` 使 `ENN` 与 `eNN` 等价，但 `eNN` 在交替中先于 `ENN`，故 `E01` 由前者捕获——结果值相同。
>
> ⚠️ 上述链只在 `legacy` 预设下成立（B4），由冻结的黄金值断言。

### 2.5 主流程

| 行为 |
|---|
| 遍历媒体库根目录 `os.listdir(ROOT_DIR)`；非目录 → `continue`；体积一致 → 跳过该一级文件夹（§2.2） |
| `find_season_folders(folder_path)`：`os.listdir` 的**直接子目录**中满足 `SEASON_PATTERN.fullmatch(entry)` 的 |
| `video_files` = 季文件夹**直接子文件**中扩展名在 `.mkv`/`.mp4`/`.ass` 内的（**大小写敏感**，`.MKV` 不处理） |
| `season = SEASON_PATTERN.search(season_folder).group(1).zfill(2)`；`season == "00"` → `continue`（`search` 作用于**完整路径**） |
| `title = os.path.basename(os.path.dirname(season_folder))` = 一级文件夹名；`episode = get_episode_numbers(filename)` |
| `.ass`（**lower() 判断**）按 §2.6 的 suffix 算法拼名；其他扩展名用 `os.path.splitext(filename)[1]` |
| `os.rename(old_path, new_path)`；成功 `print("Renamed: {old} -> {new}")`，异常 `print("Error renaming {old}: {e}")` |
| `write_log(folder, get_folder_size(folder_path))` + `print("Folder processed: {folder}, Size: {size} bytes, Timestamp: {ts}")` |

### 2.6 输出命名规则（冻结）

| 输入（季文件夹内） | 输出 |
|---|---|
| `[Lilith-Raws] Show - 01.mkv` | `<一级文件夹名> S01E01.mkv` |
| `Show.S01E01.chs.ass` | `<一级文件夹名> S01E01.chs.ass` |
| `Show 12.mp4` | `<一级文件夹名> S01E12.mp4`（经第二条正则，尾号兜底） |
| `Show.01.ass` | `legacy` 预设：`<一级文件夹名> S01ENone.01.ass`（**`.01` 被当作后缀保留**）；`default` 预设：**保留原文件名**（增量 ③） |

- `suffix` 的准确算法：`before_ass = filename[:filename.lower().rfind('.ass')]`、
  `last_dot = before_ass.rfind('.')`、`suffix = filename[last_dot:] if last_dot != -1 else '.ass'`。
  即**从 `.ass` 之前最后一个点开始，整段原样保留**（不是只保留最后一段扩展名）：
  `Show.S01E01.chs.ass` → `.chs.ass`；`Show.01.ass` → `.01.ass`；
  `before_ass` 里一个点都没有时（如 `Show.ass`）才回落成 `.ass`。
- `os.rename` 在 POSIX 上对已存在的目标**静默覆盖**——这是平台语义。⚠️ 但**应用层不复刻**：
  引擎按体积定终名、原有同名文件让位、找不到空位则保留原名，**绝不覆盖**（「防丢」，
  见 §5 的 L4 行与 [`docs/contracts/COMPATIBILITY.md`](COMPATIBILITY.md) §3.1）。
- 幂等性：重命名后的文件名再次输入仍得到同一名字（`os.rename` 同路径为 no-op）。

### 2.7 不处理的目录

- **字幕区分繁简**不做 —— 未实现，也不在当前范围内。
- 「SP/CM/OP/ED/… 名单子文件夹不重命名」在规则引擎里由 `reject_dirs` 显式配置
  （见 [`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §3.2 增量 ①）；由于文件作用域只取季文件夹的
  **直接**子文件，嵌套子目录本来就不被处理。

---

## 3. 电影重命名契约

| 行为 |
|---|
| `ROOT_DIR` = 该媒体库的 `target_dir`；`LOG_FILE` = `<LOG_DIR>/<该库的 log_file>` |
| 遍历 `ROOT_DIR` 第一层目录；体积一致则跳过 |
| `title = folder`（一级文件夹名即标题） |
| `video_files` = 文件夹**第一层直接文件**中后缀在 `.mkv`/`.mp4`/`.ass` 内的（大小写敏感） |
| 按 `filename.split('.')[-1]` 分组，每组保留**体积最大**的一个文件 |
| `new_name = f"{title}.{extension}"`，`extension` 为分组键（最后一段后缀）；`os.rename(old_path, new_path)` |
| 记录体积并 `print("Folder processed: ...")` |

**关键后果**：

- 同一文件夹内**每个后缀只重命名体积最大的那个文件**，其余文件保留原名；
- 已有 `<title>.<ext>` 与另一同后缀文件并存时，平台 `os.rename` 会**静默覆盖**目标——**应用层不再覆盖**
  （让位 / 加后缀 / 保留原名，见 §5 的 L4）；
- 未在 `.mkv/.mp4/.ass` 白名单内的文件（如 `.srt`、`.MKV`）完全不处理（但**会被建硬链接**）；
- 缓存日志格式、跳过规则与番剧侧完全一致。

---

## 4. 实现契约 B1–B13

> 本节登记**建链实现与规则引擎必须满足的行为约束**。**重命名**整体由配置驱动的规则引擎
> `app/processors/engine.py` 执行，**硬链接**由 `app/processors/hardlink.py` 执行（可观察行为逐字不变）。
>
> 同一挂载（`linkat` 按挂载身份判 `EXDEV`、`st_dev` 不能预测 `link()` 成功）的完整论证**只有一处**：
> [`docs/contracts/COMPATIBILITY.md`](COMPATIBILITY.md) §2。本节只在 B8 里引用结论，不复述推导。

| # | 规则 | 理由 |
|---|---|---|
| B1 | 容器**自包含**：`COPY app/` 只带入应用自己的代码，除数据卷与媒体卷外不挂任何宿主目录 | 镜像搬到其他设备也能独立运行 |
| B2 | **重命名与硬链接都由应用自己的代码执行**：重命名由 `app/processors/engine.py`、硬链接由 `app/processors/hardlink.py`；运行期不载入任何外部模块 | 硬链接行为一个字都不许变，因此由 `tests/test_hardlink_native.py` A 组对**冻结的黄金值**断言 |
| B3 | **判据的产物被冻结成黄金值**：`tests/test_hardlink_native.py` A 组五条场景 + `tests/test_processor_engine.py` / `tests/test_bangumi_episode_rules.py` / `tests/test_rename_extensions.py` | 这是"行为没有漂移"的核心证据；举证方式为**冻结黄金值断言** |
| B4 | `default` 预设相对 `legacy` 的差异**只允许** [`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §3 列明的**四处增量**（① bangumi `reject_dirs`、② movie `reject_dirs`、③ bangumi `episode_fallback = null`、④ bangumi `episode_patterns` 九条有序规则）。⚠️ **「防丢」（同名不覆盖）不算预设差异**：它对两个预设**同样生效**，是引擎的**全局行为**，`LEGACY_OVERRIDES` 只声明预设字段差异、**不解除**防丢（见 §5 的 L4 行）。⚠️ **「特典排除」也不算预设差异**：它是**新增能力**，由**全局设置项** `extras_patterns` 控制、对两个预设同样生效（`extras_patterns: []` 可整体关闭）；口径是**只排除改名**（命中者保留原名、留在原位置，不归入 `其他/`） | 差异必须可枚举、可审计，不能悄悄漂移 |
| B5 | 源目录**不靠挂载只读保护**，而是**可验证的保证** —— ① 代码层任何路径都不写源目录（**唯一例外：B13 的 `chmod`**，它只改权限位，不改内容/大小/inode 号/mtime，见 §1.6）；② 任务前后做**源完整性核对**（快照 `(相对路径, st_size, st_ino, st_mtime_ns)`，任何差异 ⇒ `Task.result.source_integrity.ok = false` 且 `warnings[]` 列出变动路径）；③ `tests/test_no_source_mutation.py`。核对**不得**比较 `nlink`/`ctime`（硬链接本身会让它们变化） | 内核让"源单独 `:ro` 挂载"与"跨路径硬链接"互斥（源与目标必须在**同一个 bind 挂载**内，见 COMPATIBILITY §2），所以源侧保护是**可验证的保证**而不是结构性保证 |
| B6 | 日志格式冻结：硬链接日志 = `'%(asctime)s - %(message)s'` 纯文本；重命名日志 = `{"<作品名>": {"size": <int>, "timestamp": "YYYY-mm-dd HH:MM:SS"}}` JSON。每条记录上**另有 `works_dir`**（绝对容器路径）以便迁移；**读日志时必须忽略未知键** | 格式兼容 + 每个媒体库可独立配置日志文件 |
| B7 | 幂等性仍只由"一级文件夹体积一致 ⇒ 跳过"提供（§1.4 / §2.2），**不得引入别的去重机制**。判据的**来源**是每库账本（B10），**判据本身与粒度不变** | 既有幂等规则 |
| B8 | 绝不实现"复制回退"；任务开始前判明源与目标是否可建硬链接（**实证 `link()` 探针为主判据**，mount ID 为预筛、`st_dev` 为无 `/proc` 时的降级回退，三态 `bool \| None`）——不可建时写**中文告警到 `Task.result.warnings[]` 但允许任务继续跑（警告，不硬拦）**；任务结束后仍逐文件核对 `(st_dev, st_ino)` | 硬性约束（不得静默复制冒充硬链接）。`linkat` 按**挂载身份**判 EXDEV，`st_dev` 相等**不能**预测 `link()` 成功 |
| B9 | 容器内设置 `LANG=C.UTF-8` / `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`；孤儿清理（§1.3）保留为可选开关 `orphan_cleanup`，默认开启，关闭时不得执行该分支，且每次删除都要在任务日志里以 WARN 记录 | 写日志文件时不依赖宿主 locale（日志含中文，编码不固定会 `UnicodeDecodeError`）；破坏性动作必须可控且留痕 |
| B10 | **建链跳过判据的来源与维度**：应用侧判据 = 账本 `link_records` 的 `(library_id, folder)` 且 `source_dir` / `target_dir` / `size` 三项**全部一致**；`hard_links.log` **照写**（两种打印行与两类记账行逐字不变），但**不再是判据**。`create_hard_links(..., skip=None)` 在 `skip=None` 时仍走**全局日志过滤**这条原路径 | 全局日志判据的键里没有库也没有目标 ⇒ ①两个库指向同一 `source_dir` 时，A 库跑完 B 库全被"体积一致，跳过"；②同库**换目标目录**照样跳过。判据搬进 DB 才能按库、按源、按目标各自成立 |
| B11 | **全局串行执行**：单工作线程 + 锁，同一时刻只有一个任务可能触碰文件；不得引入多 worker / 多实例并行 | 日志"读-改-写整份"（§5 的 L1）与 SQLite 单写者都假定串行；多进程会互相覆盖 |
| B12 | **待办看板只记录、不阻断**：任务收尾由 `app/todos.py::record_task_events()` 记账，它**自己开事务、内部异常只记日志绝不外抛**；未接线时看板为空、任务行为完全不变 | "值得你知道但不该打断任务"的事不能反过来影响任务结果 |
| B13 | **`permission_mode` 是默认关闭的显式开关**：关闭时一个 `chmod` 都不做；开启只动**目标侧**（库根 + 本次处理到的每个一级文件夹的目标子树，含被跳过的旧作品），符号链接不跟进；界面从关到开二次确认，任务日志以 WARNING 记录改动条数 | 硬链接与源共享 inode ⇒ 对库内文件 `chmod` 会**同时改掉源文件权限位**；这个代价只能由开启者显式承担（边界见 §1.6） |

### 4.1 每库独立日志：不需要播种与 `Source:` 前缀本地化

每个媒体库有自己的日志文件（[`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §5），不再由多个库共用一份
`hard_links.log`，因此不存在"宿主前缀与容器前缀对不上"的场景；`HOST_PATH_MAP` **只用于展示**，
应用**不改写** ` - Source: ` 字段里的任何前缀。记录归属靠**显式登记的 `works_dir`**（YAML 里可改）表达。

### 4.2 硬链接日志的写入纪律

`hard_links.log` 只接收两类记账行（「日志隔离」过滤器 `_BookkeepingFilter`）：孤儿清理行与
`Source: … - Folder: … - Folder size: …` 行；**只过滤、不清理、不轮转**，历史行一个字节都不删。
完整纪律与实现要点见 [`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §5.3。

---

## 5. 已知限制（**记录，不修正**）

| # | 限制 | 影响 |
|---|---|---|
| L1 | 重写日志时**逐行判定**，并发调用会互相覆盖 | 已由全局串行执行（B11）规避 |
| L2 | 目录/路径名含 `" - "` 时该日志行被静默忽略，每次都被当作"未记录"→ 重复尝试链接 | 产生重复 `FileExistsError`，不损坏数据。这些失败在**应用侧**改判成 `already_linked`（幂等跳过）；日志行解析与"每轮重复走一遍"照旧（冻结行为） |
| L3 | 认不出集数时，`legacy` 口径会把字面量 `None` 写进文件名（`… S01ENone.mkv`） | 既有命名结果。**`default` 预设已规避**：`episode_fallback: null` ⇒ 认不出集数就**保留原文件名**（有意增量 ③） |
| L4 | `os.rename` 对已存在的目标在 POSIX 上静默覆盖 | 极端情况下丢失一个硬链接（源文件不受影响）。**应用层不再覆盖**（「防丢」）：引擎先给作用域内所有候选文件定终名（同名组按**体积从大到小**，最大的拿干净名，其余加 ` (2)`、` (3)`…，上限 999），目标盘上原有的同名文件比新来的小时改名**让位**（**不删除**），找不到空位则**保留原名** + 记 `error`；含 `legacy` 预设、两种平台 |
| L5 | `SEASON_PATTERN.fullmatch` 使 `Season 1 (2018)` 之类的季文件夹被忽略 | 既有识别规则 |
| L6 | 季文件夹内嵌套子目录中的文件永不重命名 | 既有行为（见 §2.7） |
| L7 | 只处理 `.mkv/.mp4/.ass`，大小写敏感；`.srt` 等不重命名但**会被建立硬链接** | 硬链接与重命名两套白名单不同，属既有设计 |
| L8 | 体积变化检测是"事后"的：若下载工具在链接后**原地改写**已存在文件（同尺寸），不会被发现 | 保持既定行为 |
| L9 | 把"一级文件夹体积"作为唯一处理标记，**体积相同即跳过**；若同一作品被追加了等体积文件，不会被发现 | 既有幂等规则 |

---

## 6. 行为回归

行为回归由项目自己的回归测试承担，判据是**冻结的黄金值**；执行体只有
`app/processors/hardlink.py` 与 `app/processors/engine.py` 两处。

- **测试 ↔ 契约条目（B/L）的逐条映射**：[`docs/meta/TEST-MAP.md`](../meta/TEST-MAP.md)
- **验证证据（E 编号）与历史采集记录**：[`docs/meta/EVIDENCE.md`](../meta/EVIDENCE.md)

硬链接日志与重命名日志的**行格式与写法规矩一律不变**，因此既有工具与审计脚本无需改动。
