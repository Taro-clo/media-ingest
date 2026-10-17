# 行为基线（不可变契约）

> 本文档是**现行实现必须满足的行为契约**：硬链接由原生实现 `app/processors/hardlink.py` 完成，
> 重命名由配置驱动的规则引擎 `app/processors/engine.py` 完成。这里记录的是两者**可观察行为**的
> 冻结判据 —— 日志行格式、跳过判据、边界条件与已知限制，由项目自己的回归测试用**冻结的黄金值**断言。
>
> §0、§1、§2 是硬链接与重命名的行为事实；§3 是规则引擎在当前实现下的等价约束（B1–B10）；
> §4 是已知限制（L1–L9）；§5 是行为回归的守护方式。
>
> **红线**：应用自身的配置与测试样本独立存放；任何代码路径都不得写源目录（见 §3 的 B5）。

---

## 0. 整体数据流

```
qBittorrent 完成下载
        │
        ▼
/vol1/1000/Download/qbittorrent/{Bangumi,Movie}/<一级文件夹>/...
        │   hardlink.py   （os.link，不改动源文件）
        ▼
/vol1/1000/Library/{Bangumi,Movie}/<同名一级文件夹>/...   ← 与源同 inode
        │   engine.py   （原地 os.rename）
        ▼
Emby 读取（容器内 /library/...）
```

硬链接与重命名是两条独立的链路，通过**日志文件**解耦：硬链接把「已处理过的一级文件夹」记进日志与每库账本，
重命名读自己的 `json_cache` 日志判断某个标题文件夹是否处理过。

---

## 1. 硬链接契约

### 1.1 入口与签名

```python
def create_hard_links(source_dir, target_dir, *, skip: dict[str, int] | None = None) -> None
```

- `skip=None`（默认）⇒ 跳过判据来自**全局硬链接日志** `read_logged_folders(source_dir)`；
- 传入 `skip` ⇒ 判据来自**每库账本**（见 §1.4）。

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

> ⚠️ 蕴含约束：**路径中若含 `" - "` 会破坏解析**；目录名中若含 `" - "` 会使该行被静默忽略。属于既有行为，不修正。（应用另走一条结构化通道拿失败项，于是这类文件夹的失败也能正确改判成 `already_linked`；解析本身仍逐字不动。）

### 1.3 孤儿清理（破坏性，必须保留）

对日志中记录的每个 `folder`，若 `source_dir/folder` **不存在**，则：

```python
shutil.rmtree(target_folder)                                           # 递归删除目标硬链接文件夹
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
| `folder_size = sum(os.path.getsize(os.path.join(root, file)) for root, dirs, files in os.walk(source_folder) for file in files)` —— 递归求和，**不排除符号链接**，空文件夹体积为 `0` |
| 若 `folder in logged_folders and logged_folders[folder] == folder_size` → `print("文件夹已记录且体积一致，跳过: {folder}")` 并 `continue`（**幂等性的唯一来源**） |
| 体积不一致 → 重写日志文件删掉该 folder 的**所有**匹配行（子串匹配 `f"Source: {source_dir} - Folder: {folder} - Folder size:"`，读-改-写整份），`print("已删除旧记录: {folder}")` |
| `os.makedirs(target_folder, exist_ok=True)` |
| 递归 `os.walk(source_folder)`；`relative_path = os.path.relpath(root, source_folder)`；`os.makedirs(os.path.join(target_folder, relative_path), exist_ok=True)` |
| `os.link(source_file, target_file)` —— **硬链接，绝不复制** |
| 成功 → `print(f"已创建硬链接: {target_file}")` |
| 任何异常 → `print(f"创建硬链接失败: {target_file} - {e}")`，**继续处理下一个文件**（不中断） |
| 完成后记录新体积（`logging.info(f"Source: ... - Folder: ... - Folder size: ...")`） |
| `print(f"已记录文件夹体积: {folder} - {folder_size}")` |

**目录层级契约**：`target/<folder>/<相对源文件夹的完整子路径>/<同名文件>` —— 逐字节保留源侧的相对层级与文件名，不做任何清洗/重命名。

> ⚠️ **跳过判据的来源（应用侧）** —— 上表描述的是原生实现的行为，逐字不变；
> 应用侧把"已处理过"的判据从**全局日志**搬到了**每库账本**（`app/link_records.py`，表 `link_records`）：
>
> - **不变**：两种打印行（`Logged: <folder> - <folder size>`、
>   `文件夹已记录且体积一致，跳过: <folder>`）与日志记账行**逐字不变**，日志**照写**；
>   只遍历**一级目录**不变；上面那条**目录层级契约**、`os.link`、`os.makedirs`
>   以及命名与后缀规则全部不变。`create_hard_links(..., skip=None)` 在
>   `skip=None` 时走**原路径**（`read_logged_folders(source_dir)` 过滤全局 `hard_links.log`），
>   逐字一致（`tests/test_hardlink_native.py` 的 A 组黄金值基线）。
> - **变**：应用传入 `skip` 时判据来自账本，键为 `(library_id, folder)`，且必须
>   「本库 + 同 `source_dir` + 同 `target_dir` + 同体积」四项**全部匹配**。全局日志判据的
>   键里**没有库也没有目标** ⇒ ①两个库指向同一 `source_dir` 时，A 库跑完后 B 库全被
>   "体积一致，跳过"（新库一个链接都不建）；②同库**换目标目录**照样跳过。日志因此从"判据"
>   降为"**兼容与审计**"。契约条目见 §4 的 **B7** 与 **B10**。

### 1.5 关键边界条件

| 场景 | 行为 |
|---|---|
| 目标文件已存在（`os.link` → `FileExistsError`） | 被 `except` 捕获，打印"创建硬链接失败"，**不覆盖、不删除**。因此"未变更体积时重跑"靠 §1.4 的跳过分支避免；强制重跑会产生一批无害的 FileExistsError |
| 源与目标跨文件系统 | `os.link` → `OSError(EXDEV)`，同样只打印失败，**绝不回退为复制** |
| 源目录不存在 | `os.listdir` 抛 `FileNotFoundError`，**未捕获，直接向上抛出** |
| 空文件夹 | `folder_size == 0`；首次会 `makedirs(target_folder)` 后记录 `0` |
| 符号链接 | `os.walk` 默认不跟随目录符号链接；文件符号链接会被 `os.link` 直接链接（可能产生指向符号链接的硬链接） |
| 权限 | `os.link` 继承源 inode 的权限与属主 |

### 1.6 权限模式（`chmod 777` 是默认关闭的显式开关）

> ⚠️ **`chmod 777` 这一条是「默认关闭的显式开关」**（**默认关闭、开启前必须二次确认**）。
> 硬链接与源文件**共享同一个 inode**，`chmod` 目标侧的硬链接会**同时改掉**
> `/vol1/1000/Download/qbittorrent` 下对应源文件的权限位；它的确解决了
> 「非 root 服务（如 Emby）读不到库内文件」的真问题，所以改为**由用户显式承担**这个代价。
> 开关的边界（实现 `app/processors/hardlink.py`，测试 `tests/test_permission_mode.py`）：
> - 目标权限位 `0777`（`PERMISSION_MODE_DIR` / `PERMISSION_MODE_FILE`），只作用于**目标侧**：
>   库根目录本身、本次处理到的每个一级文件夹的目标侧整棵子树（**含"体积一致 ⇒ 跳过"的旧作品**）；
> - **绝不**递归或改动源目录、**绝不**跟进符号链接（`os.chmod` 默认会跟进，那会改到库外面的文件）；
> - 关闭时（默认）**一个 `chmod` 都不做**（不 chmod、不 stat 权限位）；
> - 源侧权限位的变化**不**触发 B5 的源完整性核对 —— 它比的是
>   `(相对路径, st_size, st_ino, st_mtime_ns)`，不比权限位；但任务日志会以 WARNING 记录
>   「已把 N 个目录 / M 个文件修正为 0777，源文件权限位随之改变（大小、inode 号、mtime 不变）」；
> - 硬链接天然继承源文件权限（`os.link` 语义）；若只想放宽权限而**不愿**影响源文件，
>   请在**宿主上**对目录显式操作（见 `docs/guides/DEPLOYMENT.md` §10.4），并清楚它同样共享 inode；
> - 重命名（`os.rename`）只改目录项、不碰 inode，因此不受此约束影响。
>
> 反过来说：`permission_mode` **开着的时候**就是一个"能改掉 qBittorrent 源文件权限位"的开关 ——
> 这正是它默认关闭、且界面从关到开必须二次确认的原因（`web/js/pages/library_edit.js`）。

### 1.7 默认路径对

```
/vol1/1000/Download/qbittorrent/Bangumi  →  /vol1/1000/Library/Bangumi
/vol1/1000/Download/qbittorrent/Movie    →  /vol1/1000/Library/Movie
```

---

## 2. 重命名契约

### 2.1 配置常量

| 常量 | 值 |
|---|---|
| `ROOT_DIR` | 媒体库的 `target_dir`（番剧默认 `/vol1/1000/Library/Bangumi`，电影默认 `/vol1/1000/Library/Movie`） |
| `LOG_DIR` | `settings.log_dir`（默认 `/data/logs`）—— **不按媒体库分子目录** |
| `LOG_FILE` | `<LOG_DIR>/<该媒体库的 log_file>`，番剧默认 `bangumicache.log`，电影默认 `moviecache.log` |

### 2.2 缓存日志格式

JSON 字典：

```json
{ "<一级文件夹名>": { "size": <int>, "timestamp": "YYYY-mm-dd HH:MM:SS", "works_dir": "<绝对容器路径>" } }
```

- 读失败（`FileNotFoundError` / `JSONDecodeError`）→ 返回 `{}`；
- 写：读-改-写整份（`open('w')`），`json.dump(..., ensure_ascii=False, indent=4)`；**读日志时必须忽略未知键**；
- 跳过条件：`folder in log_data and log_data[folder]["size"] == get_folder_size(folder_path)` → `print("Skipping folder: {folder} (already processed at {timestamp})")`，并跳过该一级文件夹（**唯一的"已处理"判据**）。

### 2.3 季文件夹识别

`SEASON_PATTERN.fullmatch(entry)`：**fullmatch ⇒ 只接受恰好 `Season <数字>`**；`Season 1 (2018)`、`Season1`、`SP` 均不匹配。

### 2.4 剧集号提取

现行默认（`default` 预设）是 **9 条带边界约束的有序规则**（见 [`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §3.2 增量 ④，没有"抓到数字就算集号"的分支），
`episode_fallback` 是 `null`（增量 ③）⇒ 提取不到集数就**保留原文件名**，不会产生 `ENone` 这类字面量。

`legacy` 预设换回"最朴素的旧口径"：两条正则 + `episode_fallback: "None"`，行为如下：

1. 第一条正则 `search(filename)`（**search，非 fullmatch；按文件名中最早出现的匹配**）；
2. 命中 → 依次取第 1→8 个分组中**第一个非空**的，`zfill(2)` 返回；
3. 未命中 → 第二条正则 `search` → 取其分组 `.zfill(2)`；
4. 都未命中 → 返回字符串 `"None"`（**注意是字符串，会进入文件名**）。

> 交替顺序即优先级：`第…集` > `[NN]` > `EpisodeNN` > `EPNN` > `eNN` > `ENNN` > `#NN` > `-NN`。
> `(?i)` 使 `ENN` 与 `eNN` 等价，但 `eNN` 在交替中先于 `ENN`，故 `E01` 由前者捕获——结果值相同。
>
> ⚠️ 上述链只在 `legacy` 预设下成立（**B4/B5**），由冻结的黄金值断言。

### 2.5 主流程

| 行为 |
|---|
| 遍历媒体库根目录 `os.listdir(ROOT_DIR)` |
| 非目录 → `continue` |
| 体积一致 → 跳过该一级文件夹（见 §2.2） |
| `find_season_folders(folder_path)`：`os.listdir` 的**直接子目录**中满足 `SEASON_PATTERN.fullmatch(entry)` 的 |
| 遍历每个季文件夹 |
| `video_files = [f for f in os.listdir(season_folder) if os.path.isfile(...) and f.endswith(('.mkv', '.mp4', '.ass'))]` —— **仅季文件夹直接子文件**，**扩展名大小写敏感**（`.MKV` 不处理） |
| `season = SEASON_PATTERN.search(season_folder).group(1).zfill(2)`；`season == "00"` → `continue`（注意 `search` 作用于**完整路径**） |
| `title = os.path.basename(os.path.dirname(season_folder))` = 一级文件夹名 |
| 逐个取 `episode = get_episode_numbers(filename)` |
| `.ass`（**lower() 判断**）：`before_ass = filename[:filename.lower().rfind('.ass')]`；`last_dot = before_ass.rfind('.')`；`suffix = filename[last_dot:]` 若 `last_dot != -1`，否则 `suffix = '.ass'`；`new_name = f"{title} S{season}E{episode}{suffix}"` |
| 其他：`ext = os.path.splitext(filename)[1]`；`new_name = f"{title} S{season}E{episode}{ext}"` |
| `os.rename(old_path, new_path)` |
| 成功 `print("Renamed: {old} -> {new}")`；异常 `print("Error renaming {old}: {e}")` |
| `write_log(folder, get_folder_size(folder_path))` + `print("Folder processed: {folder}, Size: {size} bytes, Timestamp: {ts}")` |

### 2.6 输出命名规则（冻结）

| 输入（季文件夹内） | 输出 |
|---|---|
| `[Lilith-Raws] Show - 01.mkv` | `<一级文件夹名> S01E01.mkv` |
| `Show.S01E01.chs.ass` | `<一级文件夹名> S01E01.chs.ass` |
| `Show 12.mp4` | `<一级文件夹名> S01E12.mp4`（经第二条正则，尾号兜底） |
| `Show.01.ass` | `legacy` 预设：`<一级文件夹名> S01ENone.01.ass`（**`.01` 被当作后缀保留**；无 `e`/`EP` 等前缀时集数落到 `"None"`）；`default` 预设：**保留原文件名**（增量 ③） |

- `suffix` 的准确算法：`before_ass = filename[:filename.lower().rfind('.ass')]`、
  `last_dot = before_ass.rfind('.')`、`suffix = filename[last_dot:] if last_dot != -1 else '.ass'`。
  即**从 `.ass` 之前最后一个点开始，整段原样保留**（不是只保留最后一段扩展名）：
  `Show.S01E01.chs.ass` → `.chs.ass`；`Show.01.ass` → `.01.ass`；
  `before_ass` 里一个点都没有时（如 `Show.ass`）才回落成 `.ass`。
- `os.rename` 在 POSIX 上对已存在的目标**静默覆盖**——这是平台语义。⚠️ 但**应用层不复刻**：
  L2「防丢」起，引擎按体积让位 / 加 ` (2)`… 后缀保留 / 保留原名，**绝不覆盖**（见 §4 的 L4 行）。
- 幂等性：重命名后的文件名再次输入仍得到同一名字（`os.rename` 同路径为 no-op）。

### 2.7 不处理的目录

- **字幕区分繁简**不做 —— 未实现，也不在当前范围内。
- 「SP/CM/OP/ED/… 名单子文件夹不重命名」在规则引擎里由 `reject_dirs` 显式配置（见 [`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §3.2 增量 ①）；
  由于文件作用域只取季文件夹的**直接**子文件，嵌套子目录本来就不被处理。

---

## 3. 电影重命名契约

| 行为 |
|---|
| 媒体库根目录 = `/vol1/1000/Library/Movie` |
| `LOG_DIR` = `settings.log_dir`；`LOG_FILE` = `.../moviecache.log` |
| 遍历 `ROOT_DIR` 第一层目录；体积一致则跳过 |
| `title = folder`（一级文件夹名即标题） |
| `video_files = [f for f in os.listdir(folder_path) if f.endswith(('.mkv', '.mp4', '.ass'))]` —— **只处理第一层直接文件**，大小写敏感 |
| 按 `filename.split('.')[-1]` 分组，每组保留**体积最大**的一个文件 |
| `new_name = f"{title}.{extension}"`，`extension` 为分组键（最后一段后缀） |
| `os.rename(old_path, new_path)` |
| 记录体积并 `print("Folder processed: ...")` |

**关键后果**：

- 同一文件夹内**每个后缀只重命名体积最大的那个文件**，其余文件保留原名；
- 已有 `<title>.<ext>` 与另一同后缀文件并存时，`os.rename` 会**静默覆盖**目标——平台语义（**应用层不再覆盖**：改为让位 / 加后缀 / 保留原名，见 §4 L4）；
- 未在 `.mkv/.mp4/.ass` 白名单内的文件（如 `.srt`、`.MKV`）完全不处理；
- 缓存日志格式、跳过规则与番剧侧完全一致。

---

## 4. 实现契约 —— 规则引擎与建链实现必须满足的约束

> 本节描述的是**建链实现与规则引擎必须满足的行为约束**。**重命名**整体由配置驱动的规则引擎执行，
> **硬链接**由 `app/processors/hardlink.py` 执行（可观察行为逐字不变）。下表是这些行为在当前实现下的等价约束。
>
> **挂载形态（B9）**：容器挂载源的**共同父目录**且容器路径 = 宿主路径
> （`${MEDIA_ROOT:-/vol1/1000}:/vol1/1000`）。原因：Linux `linkat` 按**挂载身份**判定，源与目标不在同一个
> bind 挂载内时 `link()` 一律返回 `EXDEV` —— 两条独立 bind（`/downloads:ro` + `/library`）会让硬链接
> **永远失败**（且只看 `st_dev` 的预检会给出假绿的"可以创建硬链接"）。代价是源子树不再由挂载只读保护，
> 由 B5/B9 的代码层保证与源完整性核对接管。

| # | 规则 | 理由 |
|---|---|---|
| B1 | 容器**自包含**：`COPY app/` 只带入应用自己的代码，除数据卷与媒体卷外不挂任何宿主目录 | 镜像搬到其他设备也能独立运行 |
| B2 | **重命名与硬链接都由应用自己的代码执行**：重命名由配置驱动的规则引擎 `app/processors/engine.py`，硬链接由 `app/processors/hardlink.py`；运行期不载入任何外部模块 | 硬链接的行为一个字都不许变，因此由 `tests/test_hardlink_native.py` A 组对**冻结的黄金值**断言（stdout + 日志内容） |
| B3 | **判据的产物被冻结成黄金值**：`tests/test_hardlink_native.py` A 组五条场景 + `tests/test_processor_engine.py` / `tests/test_bangumi_episode_rules.py` / `tests/test_rename_extensions.py` | 这是"行为没有漂移"的核心证据；举证方式为**冻结黄金值断言** |
| B4 | `default` 预设相对 `legacy` 的差异**只允许** [`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §3 列明的**四处增量**（① bangumi `reject_dirs`、② movie `reject_dirs`、③ bangumi `episode_fallback = null`、④ bangumi `episode_patterns` 九条有序规则）。⚠️ **L2「防丢」（同名不覆盖）不算预设差异**：它对 `default` 与 `legacy` **同样生效**，是引擎的**全局行为**；`LEGACY_OVERRIDES` 只声明预设字段差异，**不解除** L2（见 §4 的 L4 行）。⚠️ **L3「特典排除」也不算预设差异**：它是**新增能力**，由**全局设置项** `extras_patterns` 控制、对 `default` 与 `legacy` 同样生效（`extras_patterns: []` 可整体关闭）；口径是**只排除改名**（命中者保留原名、留在原位置，不归入 `其他/`），不写进 `LEGACY_OVERRIDES`、不进这份四处增量清单 | 差异必须可枚举、可审计，不能悄悄漂移 |
| B5 | 源目录**不靠挂载只读保护**，而是**可验证的保证** —— ① 代码层任何路径都不写源目录（**唯一例外：`permission_mode` 开启时的 `chmod`** —— 它只改权限位，不改内容/大小/inode 号/mtime，见 §1.6）；② 任务前后做**源完整性核对**（快照 `(相对路径, st_size, st_ino, st_mtime_ns)`，任何差异 ⇒ `Task.result.source_integrity.ok = false` 且 `warnings[]` 列出变动路径）；③ `tests/test_no_source_mutation.py`。核对**不得**比较 `nlink`/`ctime`（硬链接本身会让它们变化） | 内核让"源单独 `:ro` 挂载"与"跨路径硬链接"互斥（源与目标必须在**同一个 bind 挂载**内），所以源侧保护是**可验证的保证**而不是结构性保证 |
| B6 | 日志格式冻结：硬链接日志 = `'%(asctime)s - %(message)s'` 纯文本；重命名日志 = `{"<作品名>": {"size": <int>, "timestamp": "YYYY-mm-dd HH:MM:SS"}}` JSON。每条记录上**另有 `works_dir`**（绝对容器路径）以便迁移；**读日志时必须忽略未知键** | 格式兼容 + 每个媒体库可独立配置日志文件 |
| B7 | 幂等性仍只由"一级文件夹体积一致 ⇒ 跳过"提供（§1.4 / §2.2），**不得引入别的去重机制**（判据的**来源**是每库账本，**判据本身与粒度不变** —— 见 B10 与 §1.4 的跳过判据说明） | 既有幂等规则 |
| B8 | 绝不实现"复制回退"；任务开始前判明源与目标是否可建硬链接（**实证 `link()` 探针为主判据**，mount ID 为预筛、`st_dev` 为无 `/proc` 时的降级回退）——不可建时写**中文告警到 `Task.result.warnings[]` 但允许任务继续跑（警告，不硬拦）**；任务结束后仍逐文件核对 `(st_dev, st_ino)` | 硬性约束（不得静默复制冒充硬链接）。Linux `linkat` 按**挂载身份**判 EXDEV，`st_dev` 相等**不能**预测 `link()` 成功（只看 `st_dev` 会产生"预检假绿"） |
| B9 | 容器内设置 `LANG=C.UTF-8` / `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`；全局串行执行（单工作线程 + 锁），同一时刻只有一个任务可能触碰文件；孤儿清理（§1.3）保留为可选开关 `orphan_cleanup`，默认开启，关闭时不得执行该分支，且每次删除都要在任务日志里以 WARN 记录 | 写日志文件时不依赖宿主 locale；规避日志文件"读-改-写整份"的竞争 |
| B10 | **建链跳过判据的来源与维度**：应用侧判据 = 账本 `link_records` 的 `(library_id, folder)` 且 `source_dir` / `target_dir` / `size` 三项**全部一致**；`hard_links.log` **照写**（两种打印行与两类记账行逐字不变），但**不再是判据**。`create_hard_links(..., skip=None)` 在 `skip=None` 时走**全局日志过滤**这条原路径（由 `tests/test_hardlink_native.py` A 组黄金值断言） | 全局日志判据的键里没有库也没有目标 ⇒ ①两个库指向同一 `source_dir` 时，A 库跑完 B 库全被"体积一致，跳过"（新库一个链接都不建）；②同库**换目标目录**照样跳过。日志保留是为了既有工具兼容与字节级基线；判据搬进 DB 才能按库、按源、按目标各自成立 |

### 4.1 每库独立日志：不需要播种与 `Source:` 前缀本地化

每个媒体库有自己的日志文件（[`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §5），不再由多个库共用一份
`hard_links.log`，因此不存在"宿主前缀与容器前缀对不上"的场景；`HOST_PATH_MAP` **只用于展示**，
应用**不改写** ` - Source: ` 字段里的任何前缀。

记录归属靠**显式登记的 `works_dir`**（绝对容器路径，YAML 里可改）表达，语义比字符串前缀替换更明确。

于是"首次运行把已处理文件夹全部重扫一遍、满屏 `File exists`"不会发生——但**旧日志迁进来时要对齐
`works_dir`**：让每一行的 `works_dir` 与媒体库的 `source_dir` 一致。

### 4.2 硬链接日志的写入纪律

- `hard_links.log` **只接收两类记账行**（「日志隔离」过滤器，`_BookkeepingFilter`）：
  ① `Deleted orphaned hard link folder: …`；② `Source: …`（且必须**同时**含 ` - Folder: ` 与 ` - Folder size: `）；
- **只过滤、不清理、不轮转**：历史行一个字节都不删，行格式与 §1.2 的解析契约都没有变；
- 历史日志继续沿用、**不做任何改写**（见 [`docs/contracts/PROCESSORS.md`](PROCESSORS.md) §5.3）。

---

## 5. 已知限制（**记录，不修正**）

| # | 限制 | 影响 |
|---|---|---|
| L1 | 重写日志时**逐行判定**，并发调用会互相覆盖 | 已由全局串行执行（B9）规避 |
| L2 | 目录/路径名含 `" - "` 时该日志行被静默忽略，每次都被当作"未记录"→ 重复尝试链接 | 产生重复 FileExistsError，不损坏数据。这些失败在**应用侧**改判成 `already_linked`（幂等跳过，任务不再因此显示失败）；日志行解析与"每轮重复走一遍"照旧（冻结行为） |
| L3 | 认不出集数时，`legacy` 口径会把字面量 `None` 写进文件名（`… S01ENone.mkv`） | 既有命名结果。**`default` 预设已规避**：`episode_fallback: null` ⇒ 认不出集数就**保留原文件名**（有意增量 ③）；`legacy` 保留 `ENone` |
| L4 | `os.rename` 对已存在的目标在 POSIX 上静默覆盖 | 极端情况下丢失一个硬链接（源文件不受影响）。**应用层不再覆盖**：L2「防丢」先给作用域内所有候选文件定终名（同名组按**体积从大到小**，最大的拿干净名，其余加 ` (2)`、` (3)`…，上限 999），目标盘上原有的同名文件比新来的小时改名**让位**（**不删除**），找不到空位则**保留原名** + 记 `error`；含 `legacy` 预设、两种平台。（注：此处的 **L2「防丢」是工作项名**，与本表的 `L2` 那条「目录名含 `" - "`」**无关** —— 两者只是编号撞名） |
| L5 | `SEASON_PATTERN.fullmatch` 使 `Season 1 (2018)` 之类的季文件夹被忽略 | 既有识别规则 |
| L6 | 季文件夹内嵌套子目录中的文件永不重命名 | 既有行为（见 §2.7） |
| L7 | 只处理 `.mkv/.mp4/.ass`，大小写敏感；`.srt` 等不重命名但**会被建立硬链接** | 硬链接与重命名两套白名单不同，属既有设计 |
| L8 | 体积变化检测是"事后"的：若下载工具在链接后**原地改写**已存在文件（同尺寸），不会被发现 | 保持既定行为 |
| L9 | 把"一级文件夹体积"作为唯一处理标记，**体积相同即跳过**；若同一作品被追加了等体积文件，不会被发现 | 既有幂等规则 |

---

## 6. 行为回归

**行为回归由项目自己的回归测试承担**：判据是**冻结的黄金值** ——
`tests/test_hardlink_native.py`（A 组五条场景：全新树 / 已记录 / 体积变化 / 源目录消失 / 日志损坏）、
`tests/test_processor_engine.py`、`tests/test_bangumi_episode_rules.py`、`tests/test_rename_extensions.py`
（文件树、`Renamed:` 行集合、日志 JSON 的键集合与 size）。

硬链接日志与重命名日志的**行格式与写法规矩一律不变**，因此既有工具与审计脚本无需改动。
