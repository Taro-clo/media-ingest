# 处理器与规则契约

> 本文是处理器与重命名规则的**唯一真源**。`app/**`、`web/**`、`tests/**`、`docker/**`、`docs/**`
> 的一切实现与描述与本文冲突时，**以本文为准**；确需偏离时先在 CHANGELOG 记下结论并把结论写回本文。
>
> 当前形态：容器**自包含、不依赖任何宿主额外目录**；重命名规则**完全配置化**
> （参数与顺序、接受/拒绝目录正则、后缀白名单），配置文件与 WebUI **双向同步**；每个媒体库**各自记录日志**，
> 所有重命名日志**集中在同一目录**。

---

## 1. 三条不可动摇的底线

1. **绝不修改 qBittorrent 的原始下载文件**（`/vol1/1000/Download/qbittorrent`；任何代码路径都不得写它）。⚠️ `:ro` 挂载提供的**结构性保证**在单挂载形态下不可用：内核让「源单独 `:ro` 挂载」与「跨路径硬链接」**互斥**，改由**可验证的保证**接管：代码层永不写源目录 + 任务前后逐文件源完整性核对（`Task.result.source_integrity` + `warnings[]`）+ `tests/test_no_source_mutation.py`（见 `docs/guides/DEPLOYMENT.md` §3.5、README 不变量 3）。
2. **绝不把跨文件系统失败降级为复制**（`EXDEV` 必须报错并说明，不得静默复制）。
3. **重命名只发生在媒体库一侧**（`/vol1/1000/Library`），源目录一个字节都不动。

---

### 2.1 硬链接：行为稳定，实现自包含

硬链接的实现是**扫描与编排算法**，其中**不含任何"每个媒体库不同"的规则**
（源目录、目标目录都已经由媒体库配置提供），所以它没有可配置性可挖。它由 `app/processors/hardlink.py`
在应用进程内完成，容器**不依赖任何外部程序或宿主目录**：镜像搬到其他设备也能独立运行。

等价性不是靠人工比对保证的：`tests/test_hardlink_native.py` 的 A 组断言**冻结的黄金值**
（全新树 / 已记录 / 体积变化 / 源目录消失 / 日志损坏五种场景）。

> 这两条副作用**故意不做**：`chmod 777` 全库扫描与 `os.getlogin`（见 `docs/contracts/BASELINE.md` §1.6）。

> **跳过判据换源，日志照写。** `app/processors/hardlink.py` 的签名是
> `create_hard_links(source_dir, target_dir, *, skip: dict[str, int] | None = None)`。
> `skip=None`（默认）时**读全局日志** —— 自己 `read_logged_folders(source_dir)` 过滤全局
> `hard_links.log`，这条路径一字未变，`tests/test_hardlink_native.py` 的黄金值基线依赖它；
> 应用侧（`app/executor.py`）改为传 `skip`，判据来自**本库账本**（`app/link_records.py` 的
> `folder_sizes(library_id, source_dir=…, target_dir=…)`，键 = `(library_id, folder)`）：
> 「本库 + 同 `source_dir` + 同 `target_dir` + 同体积」四项全部一致才跳过。
> 两种情况下 `Logged: <folder> - <folder size>` 与 `文件夹已记录且体积一致，跳过: <folder>`
> 两种打印行、以及日志记账行都**原样照写**（日志仍是兼容与审计台账，只是不再决定跳不跳）。
> 修的是两个旧缺陷：**同源多库**（两个库指向同一 `source_dir` 时，A 库跑完后 B 库全被
> "体积一致，跳过"、一个新链接都不建）与**同库换目标**（旧判据里没有目标目录，换了目标照样跳过）。
> 契约见 `docs/contracts/BASELINE.md` §1.4 的说明与 §4 的 B5。

### 2.2 重命名为什么用规则引擎

重命名的**规则**（季目录正则、集数正则、后缀白名单、命名模板）如果内联在代码里，
就无法在不改代码的前提下调整。「库级目录规则」明确要求这些规则写到配置文件中、可在 WebUI 双向修改、
并且日后新增媒体库要有**不同的重命名逻辑**时不能写死。因此重命名走 §4 的通用规则引擎，
内置处理器各自带一份**默认规则**。等价性由项目自己的回归测试用**冻结的黄金值**守护
（`tests/test_processor_engine.py`、`tests/test_bangumi_episode_rules.py`、`tests/test_rename_extensions.py`，
`legacy` 预设同样必须通过）。

---

## 3. 处理器规格（ProcessorSpec）

一个处理器 = 一个 YAML 文档，`id` 全局唯一。内置三个（`builtin: true`）：

| id | name | kind | 规格 |
|---|---|---|---|
| `hardlink_only` | 只建硬链接 | `hardlink_only` | `app/processors/hardlink.py`（行为契约见 §2.1） |
| `bangumi` | 番剧 | `rename` | 规则引擎 `app/processors/engine.py`（默认规则见 §3.2） |
| `movie` | 电影 | `rename` | 规则引擎 `app/processors/engine.py`（默认规则见 §3.2） |

```yaml
schema_version: 2
id: bangumi                      # ^[a-z][a-z0-9_-]{1,31}$
name: 番剧
description: 按「作品/Season N/文件」结构重命名为 SxxExx
kind: rename                     # rename | hardlink_only
enabled: true
builtin: true                    # 内置：可重置、不可删除

# ── 目录遍历与接受 / 拒绝（§3.2）──────────────────────────────
traverse:
  title_depth: 1                 # 标题文件夹在媒体库根下的层级，1 = 直接子目录（当前固定 1）
  file_scope: subdir             # subdir：文件取自标题文件夹下匹配 accept_dirs 的子目录
                                 # title ：文件直接取自标题文件夹（Movie 语义）
  accept_dirs:                   # 空数组 ⇒ 任何子目录都接受（仅 file_scope=subdir 时有意义）
    - {pattern: '^Season (\d+)$', season_group: 1}
                                 # season_group：缺省 1；合法取值为 0 或 ≥1，且必须 ≤ 该正则
                                 #   的捕获组数量。0 = 只用这条正则判断接受、**不取季号**
                                 #   （{season_raw} 为空串，{season} 按现有规则补零为 "00"，
                                 #   与 accept_dirs 为空时逐字相同）；越界会校验失败并提示怎么写。
  reject_dirs:                   # 命中即**整棵子树跳过**（标题层级与所有子目录层级都生效）
    # 这份名单覆盖常见的特典 / 扫图 / 分卷目录（有意的行为增量）。
    # 全部大小写不敏感 + 整名匹配，避免误伤正常标题。
    - {pattern: '(?i)^(sp|cm|op|ed)s?$'}            # SP CM OP ED SPs CMs OPs EDs
    - {pattern: '(?i)^scans?$'}                      # Scan Scans
    - {pattern: '(?i)^cds?$'}                        # CD CDs
    - {pattern: '(?i)^(映像特典|特典cd)$'}            # 映像特典 特典CD
    - {pattern: '(?i)^previews?$'}                   # Preview Previews
    - {pattern: '(?i)^scan \(vol\.\d+\)$'}           # Scan (Vol.任意数字)
    - {pattern: '(?i)^vol\.\d+$'}                    # Vol.任意数字
    - {pattern: '(?i)^(extra|bonus|menus|music)$'}   # EXTRA Bonus Menus Music
    - {pattern: '(?i)^subtitles?$'}                  # Subtitle Subtitles
    - {pattern: '(?i)^screenshots?$'}                # ScreenShot ScreenShots
    - {pattern: '(?i)^(ncop|nced)$'}                 # NCOP NCED ncop nced
  skip_season_zero: true         # season_group 补零后为 "00" 时跳过

# ── 后缀白名单（对该媒体库通用；大小写敏感）────
# 这里是**回退值**：全局设置 rename_extensions 或库级 rule_override.extensions 非空时，
# 它们优先（三层优先级见 §3.3）。
extensions: [.mkv, .mp4, .ass]

# ── 命名（§3.1）─────────────────────────────────────────────
naming:
  # ① 具名表达式：按**书写顺序**求值，可引用上下文与前面已算出的表达式。
  expressions: {}
  # ② 段序列：最终文件名的组装顺序，可自由调整。
  segments:
    - {param: title}
    - {literal: ' S'}
    - {param: season, pad: 2}
    - {literal: E}
    - {param: episode, pad: 2}
    - {param: suffix}

# ── 剧集号提取（命名里用到 episode 时必需）─────────────────────
# 下面是当前内置 bangumi 的推荐默认：9 条**有序**规则，序号即优先级（小者先试）。
# 三条硬性要求：① 每条都必须把集号捕获成**命名组** `ep`（引擎只取命名组；`legacy` 的两条
# 用的是 `g1..g8`，靠"组序号"取第一非空组）；
# ② 全部用 `(?i)` 忽略大小写；③ 数字只允许 1~2 位（`(?!\d)` 把 3 位以上排除掉）。
episode_patterns:
  - name: chapter
    pattern: '(?i)第\s*(?P<ep>\d{1,2})(?!\d)\s*[集话話回]'
  - name: episode_word
    pattern: '(?i)(?<![A-Za-z])Episode\s*(?P<ep>\d{1,2})(?!\d)'
  - name: season_episode
    pattern: '(?i)(?<![A-Za-z])S\s*\d{1,2}(?!\d)\s*E\s*(?P<ep>\d{1,2})(?!\d)'
  - name: bracketed
    pattern: '(?i)\[\s*(?P<ep>\d{1,2})\s*\]'
  - name: ep_marker
    pattern: '(?i)(?<![A-Za-z])EP\s*(?P<ep>\d{1,2})(?!\d)'
  - name: hash_marker
    pattern: '(?i)#\s*(?P<ep>\d{1,2})(?!\d)'
  - name: dash
    pattern: '(?i)-\s*(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)'
  - name: bare_e
    pattern: '(?i)(?<=\s)[Ee]\s*(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)(?=\s|\.)'
  - name: bare_number
    pattern: '(?i)(?<=\s)(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)(?=\s|\.)'
episode_fallback: None           # 全不命中时用的字面量；留空（null）=「认不出集数就保留原名、不改名」
                                 # （内置 bangumi 的推荐默认就是 null；
                                 #   `legacy` 预设写的是**字符串** "None"，会把 None 作为集号渲染进文件名）

# ── 同一后缀取哪个文件 ───────────────────────────────────────
selection:
  per: file                      # file      = 每个匹配文件都处理（Bangumi 语义）
                                 # extension = 同后缀只取体积最大者（Movie 语义）

# ── 日志 ──────────────────────────────────────────────────────
log_file: bangumicache.log       # 必须只是文件名；落在 settings.log_dir（=/data/logs）
log_format: json_cache           # { "<标题文件夹>": {"size": N, "timestamp": "YYYY-MM-DD HH:MM:SS"} }
```

### 3.1 `naming` 的可用名字

`segments[].param` 与 `expressions` 的右值都可以引用下列**上下文名字**：

| 名字 | 取值 |
|---|---|
| `title` | 标题文件夹名 |
| `filename` | 作用域内当前文件名（含扩展名） |
| `stem` | 原文件名去掉最后一个扩展名 |
| `ext` | 扩展名，带点：`.mkv` |
| `extname` | 扩展名，不带点：`mkv` |
| `season_raw` | `accept_dirs` 的 `season_group` 捕获组（字符串）。`season_group: 0`、`accept_dirs` 为空、或 `file_scope=title` ⇒ `""` |
| `season` | `season_raw.zfill(2)` —— **无季号时 `"".zfill(2)` ⇒ `"00"`**，所以它**永远是补零串**；**唯一为 `""` 的情形是 `file_scope=title`**（文件直接取自标题文件夹，不存在季概念） |
| `episode_raw` | `episode_patterns` **按书写顺序**（序号小者优先）首个命中规则里**序号最小**的非空捕获组；全不命中为 `episode_fallback`（`null` ⇒ 空串，见下） |
| `episode` | `episode_raw.zfill(2)` |
| `suffix` | `.ass` 文件：多段后缀（`.chs.ass`）；其他文件：等价于 `ext` |
| `works_dir` | 该媒体库的 `source_dir`（绝对容器路径） |
| `rel_dir` | 作用域目录相对标题文件夹的路径 |

> **集数没认出来时**：`episode_patterns` 全部未命中 **且** `episode_fallback` 留空（`null`）
> ⇒ `episode_raw` 与 `episode` 都是**空串**（**不是**字符串 `None`）。此时若 `naming.segments` 引用了它们，
> 引擎**不给这个文件改名**（保留原文件名；见 §4 第 9 步、§3.2 增量 ③）；
> **显式配置**了 `episode_fallback`（含 `legacy` 预设的字符串 `"None"`）则属"已解析"，照旧渲染该字面量。
> 另有一个只读名字 **`episode_resolved`**（引擎内部键）：`true` = 命中模式**或**配了
> `episode_fallback`。它**不在** WebUI 变量表（`TEMPLATE_VARS`）里，但**模板写法与段写法都能用它**
> （`{episode_resolved}` 不会被判成未知变量，见下面那条校验说明）；`naming.segments[].param`
> 与 `expressions` 右值当然也可以读它。
>
> **`episode_fallback` 的"空"**：空串 `""` 与纯空白**等价于 `null`**（= 没配兜底）。
> WebUI 里把「集数兜底」输入框清空、或配置文件里写 `episode_fallback:`（YAML 空值）都是这个情形；
> 库里**早先存下**的空串在读取时也会被归一成 `null`（不改写你库里的 JSON，下次保存该处理器时才落成 `null`）。
> 只有**非空字符串**才算"已配置的兜底字面量"（`legacy` 预设的 `"None"` 属于此类，行为不变）。

`expressions` 的右值是 Python 表达式，可用：`re`、`os`、`len`、`str`、`int`、`sorted`、`min`、`max`，
以及辅助函数 `zfill(v, n)`、`splitext(p)`、`basename(p)`、`ass_suffix(filename)`。

`segments[]` 只接受 `param` / `literal` / `pad` 三个键；`param` 先查 `expressions`、再查上表。
`pad` 对返回值做 `str(v).zfill(pad)`。

> **段里的 `param` 必须是"可用变量名"（保存期校验，KI-11）**：可用名单 = 上表 12 个
> `TEMPLATE_VARS` + 引擎内部键 `episode_resolved` + **本 spec `naming.expressions` 里定义的名字**
> （由 `app/processors/spec.py` 的 `known_context_names()` 给出）。写错名字的后果是那一段
> 运行期查不到东西就**留空**，文件名因此少一段、改名却照样算成功 —— 登录日志里一行都没有。
> 因此分两层处理：
> * **保存期**（`POST`/`PUT /api/processors`、库级覆盖、配置文件导入）⇒ **422 / 导入报错**，
>   提示「段里用了未知变量 {xxx}。可用变量：…；自定义名字要先在 naming.expressions 里定义。」
>   （`param` 是空串时另报「param 不能是空字符串」）；库级覆盖的 `{…}` 模板同样按**合并后**的名单校验
>   （处理器里定义的表达式名 + 覆盖自己的表达式名都算认识）。
> * **运行期**⇒ **不拒绝整份规格**（库里早先存下的老配置、手改过的 YAML 必须能照旧跑完），
>   而是把这件事报出来：任务结果 `unknown_params` / `samples.unknown_params` + 每个文件一条 WARNING
>   日志 + `warnings` 汇总告警。**改法**：把那个名字改对，或在 `naming.expressions` 里定义它。

> **`accept_dirs` 的 `season_group`**：**缺省 `1`**；合法取值为 **`0` 或 `≥1`**，并且必须
> **≤ 该正则的捕获组数量**。`season_group: 0` 与「`accept_dirs` 为空」是**同一类情形** ——
> 只用这条正则判断该子目录接不接受，**不从它取季号**：`{season_raw}` 为空串，而 `{season}`
> 仍按现有规则走 `"".zfill(2)` ⇒ **`"00"`**（**不是**空串），与 `accept_dirs` 为空时**逐字相同**。
> 越界时的报错是「`season_group={group} 超出正则的捕获组数量（该正则只有 {n} 个组）；
> 若不打算从这个正则取季号，请显式写 "season_group": 0，或给正则加一个捕获组。`」

`expressions` 的右值是 Python 表达式，可用：`re`、`os`、`len`、`str`、`int`、`sorted`、`min`、`max`，
以及辅助函数 `zfill(v, n)`、`splitext(p)`、`basename(p)`、`ass_suffix(filename)`。

`segments[]` 只接受 `param` / `literal` / `pad` 三个键；`param` 先查 `expressions`、再查上表。
`pad` 对返回值做 `str(v).zfill(pad)`。

> **安全面（已对用户明示）**：`expressions` 在应用进程内用 `eval` 求值，等价于"允许在配置文件里执行
> Python 表达式"。这是为满足"可拓展、不写死"而接受的取舍；配置文件位于 `/data` 数据卷内，
> 能改它的人本来就能改容器配置。

### 3.2 内置处理器的默认规则

- **bangumi**：`naming` 的段序列是 `title` + ` S` + `season` + `E` + `episode` + `suffix`；
  `accept_dirs` 认 `^Season (\d+)$`；`skip_season_zero: true`；
  `selection.per: file`；`extensions: [.mkv, .mp4, .ass]`；
  `episode_patterns` 是下面增量 ④ 的 9 条有序规则；
  `BANGUMI_LEGACY_EPISODE_PATTERNS` 是 `legacy` 预设专用的两条兜底规则
  （§3 的 YAML 示例就是这 9 条当前默认）；
  `reject_dirs` 覆盖这份名单：
  SP/CM/OP/ED/SPs/CMs/OPs/EDs、Scan/Scans、CD/CDs、映像特典、特典CD、Preview/Previews、
  `Scan (Vol.N)`、`Vol.N`、EXTRA/Bonus/Menus/Music、Subtitle/Subtitles、ScreenShot/ScreenShots、
  NCOP/NCED（`ncop`/`nced` 由大小写不敏感覆盖）。这段逻辑是**有意的行为增量**。
- **movie**：`naming` 的段是 `title` + `.` + `extname`（即 `f"{title}.{extension}"`）；
  `file_scope: title`；`selection.per: extension`；`extensions: [.mkv, .mp4, .ass]`；
  `reject_dirs` 由**用户指定**：必选四条
  `特典`/`CD`/`扫图`/`scans`，另加同义与常见特典目录名（SP、Menu、Preview、EXTRA、Bonus、
  NCOP/NCED、Sample）。全部**大小写不敏感 + 整名匹配**，不误伤正常片名；每条在 YAML 里
  由 `description` 字段说明来源（`safe_dump` 不保留注释）。

- **bangumi（`episode_fallback`）**：**推荐默认是 `null`** —— 命名引用了集数、但
  `episode_patterns` 一个都没命中时**保留原文件名、不改名**（若渲染**字符串** `None` ⇒
  `… S01ENone.mkv`：同一作品的不同压制版本会撞成同一个目标名互相覆盖，**真实事故**）。
  `legacy` 预设把该字段写成**字符串** `"None"`（于是不再改名、改成渲染字面量 None）；
  想要同样效果也可以把这个字段**显式**写成字符串 `"None"`（注意 YAML 里要加引号，否则 `None` 会被解析成 `null`）。

- **bangumi（`episode_patterns`）**：旧口径的两条正则（一条 `(?:…)` 里塞 8 个候选分支 +
  一条"空白后的尾号"）**全部没有"这里不可能是集号"的边界约束**，在真实媒体库里会误判：
  `Show - 1920x1080.mkv` 被 `-\s*(\d{1,2})` 捞成第 **19** 集、末尾 CRC 方括号 `[1AE35F3A]` 里的
  `E35` 被 `E(\d{1,2})` 当成第 **35** 集（量化：真实源 **8534** 个文件名里"数字落在 6 位以上十六进制串内部"的命中，那两条正则 **21** 个 ⇒
  下面 9 条 **0** 个）。默认因此是 **9 条有序规则**，序号即优先级（小者先试；引擎按 `episode_patterns`
  的书写顺序 `search`，第一条命中的规则胜出，**不比较位置先后**）：

  下表省略公共前缀 `(?i)`；`(?P<ep>…)` 就是集号；单元格里的 `\|` 是 Markdown 表格转义（实际是 `|`），
  逐字可直接复制的版本见上面 §3 的 YAML 示例。

  | # | `name` | 正则 | 命中例 ⇒ 集号 | 反例（不命中） |
  |---|---|---|---|---|
  | 1 | `chapter` | `第\s*(?P<ep>\d{1,2})(?!\d)\s*[集话話回]` | `第 12 話.mkv`、`第7集.mkv` | `第123話.mkv`（3 位数） |
  | 2 | `episode_word` | `(?<![A-Za-z])Episode\s*(?P<ep>\d{1,2})(?!\d)` | `Episode 3.mkv`、`EPISODE 5.mkv` | `Episode123.mkv` |
  | 3 | `season_episode` | `(?<![A-Za-z])S\s*\d{1,2}(?!\d)\s*E\s*(?P<ep>\d{1,2})(?!\d)` | `S02E07.mkv`、`s2e9.mkv`、`S 02 E 08.mkv` | `S123E07.mkv` |
  | 4 | `bracketed` | `\[\s*(?P<ep>\d{1,2})\s*\]` | `[05].mkv`、`[ 5 ] x.mkv` | `[1920x1080].mkv`、`[1AE35F3A].mkv` |
  | 5 | `ep_marker` | `(?<![A-Za-z])EP\s*(?P<ep>\d{1,2})(?!\d)` | `EP09.mkv`、`ep 10.mkv` | `PREP09`（前面紧贴字母） |
  | 6 | `hash_marker` | `#\s*(?P<ep>\d{1,2})(?!\d)` | `#04.mkv`、`# 4.mkv` | 无（`#` 后面必须紧跟数字） |
  | 7 | `dash` | `-\s*(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)` | `- 11.mkv`、`-6.mkv` | `- 10 bit.mkv`、`x264-10bit.mkv` |
  | 8 | `bare_e` | `(?<=\s)[Ee]\s*(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)(?=\s\|\.)` | `E12.mkv`、`e 3.mkv` | `Show.E07.mkv`（`E` 前不是空白） |
  | 9 | `bare_number` | `(?<=\s)(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)(?=\s\|\.)` | `Show 12.mkv`、`Show 12 1080p.mkv` | `Show 12v2.mkv`、`Show 1920x1080.mkv` |

  - 1~7 是"有明确标记"的写法，8~9 是**兜底**（8 只认"空白 + `E`/`e` + 数字"，所以 `Show.E07.mkv`
    这种**紧贴点号**的写法不命中；9 要求数字前是空白、后是空白或句点，所以分辨率 / 码率 /
    `12v2` 这类不会被当成集号）。`\s` 含全角空白（`\u3000`），所以 `Show　12　1080p.mkv` 也认得出。
  - 9 条**全部不命中** ⇒ 走增量 ③（`episode_fallback: null` ⇒ **保留原文件名**）。在同一份真实源上，
    `Season N` 下直接的 `.mkv`/`.mp4`/`.ass` 共 **1349** 个，其中未命中 **98** 个（特典、多版本、
    与正片平级的 `PV`/`NCOP` 之类）—— 它们保留原名，不会被套上错误的集号。
  - 自定义规则时**必须**把集号捕获成命名组 `ep`（引擎只认命名组；`legacy` 的 `g1..g8`
    靠**组序号**取第一个非空组）。

> **五处有意的行为增量**（必须在 `CHANGELOG` 与 `docs/contracts/COMPATIBILITY.md` 显式标注）：
> ① bangumi 的 `reject_dirs`；② movie 的 `reject_dirs`（用户指定）；
> ③ bangumi 的 `episode_fallback = null`（若改成字符串 `"None"` ⇒ 会把认不出集数的文件命名成
> `… S01ENone.mkv` 并互相覆盖）；
> ④ bangumi 的 `episode_patterns`：**是**上面那 9 条有序规则，`legacy`
> 预设换回两条旧口径规则（`BANGUMI_LEGACY_EPISODE_PATTERNS`）；
> ⑤ **L3 特典排除**：命中**全局设置项** `extras_patterns`
> 的散装特典文件**退出改名流程**：**不改名、也不移动** —— 保留原名留在原位置（"归入
> `其他/`"的行为不存在）。⚠️ 它与 ①–④ **不是一类东西**：
> ①–④ 是**字段级差异**（写在处理器规格里、`preset` 可切换），而 ⑤ 是**全局设置**、
> 对**所有**处理器与库生效，`legacy` 预设**也不解除**它 —— 别把它列进 `LEGACY_OVERRIDES` 或"预设差异"。
> 顺带说明为什么需要它：`reject_dirs` 只匹配**目录名**，与正片平级的散装 `NCOP.mkv` / `Preview.mkv`
> 这类**文件**不在它的管辖范围内（没有 L3 就会被当正片套上集号）；L3 补的正是这个洞。
>
> **如何恢复旧口径**：用 `POST /api/processors/bangumi/reset {"preset":"legacy"}`（一次把三处差异
> ——`reject_dirs` + `episode_fallback` + `episode_patterns`—— 全部回退成最朴素的旧口径）；
> 或者只改其中一项：在 WebUI「处理器」编辑页把**集数兜底**（`episode_fallback`）从留空改成字符串
> `None`，把**集数规则**（`episode_patterns`）换回上面增量 ④ 里说的那两条（`name: ep` / `name: trailing`，
> 捕获组 `g1..g8`）。注意 `legacy` 是**整套**回退，只想要其中一处就手工改。
>
> ⚠️ **反方向（既有实例的 `"None"` → `null` / `legacy` 两条规则 → 9 条默认规则）没有 GUI 路径**：处理器规格是
> **播种后独立存在**的（DB / `processors/*.yaml`），升级镜像**不会**自动改它；而「处理器」编辑页的
> **集数兜底**是普通文本输入框，**清空后提交的是空串 `""` 而不是 `null`**，引擎会把空串当成"已解析"
> （结果命名成 `S01E00…`，照样撞名）。要改回"留空"请用：① 把 `/data/config/processors/<id>.yaml` 的
> `episode_fallback` 写成 `null`（WebUI 与配置文件双向同步，默认 5 秒一轮）；或
> ② `PUT /api/processors/{id}` 传 `{"episode_fallback": null}`；或
> ③ `POST /api/processors/{id}/reset {"preset":"default"}`（整套回推荐默认，会覆盖其它自定义，
> 同时也会把 `episode_patterns` 换回 9 条默认规则）。

**内置 `bangumi` 的 `episode_fallback` 默认值来自 `builtin_spec()` 本身**，所以 `POST /api/processors`
**新建**处理器（`{"template":"bangumi"}`）和 `builtin_specs()` 首次播种的那份**也都是 `null`**
（新建即安全）；`episode_patterns` 同理 —— `builtin_specs()` / `template_spec("bangumi")` 拿到的
都是那 9 条默认规则。

两个内置处理器都提供 **`legacy` 预设**（`POST /api/processors/{id}/reset {"preset":"legacy"}`）：
它相对 `default` 有**三处**差异 —— `traverse.reject_dirs: []` + `episode_fallback: "None"` +
`episode_patterns: [ep, trailing]`（两条旧口径正则），即**最朴素的旧口径**。
`{"preset":"default"}` 恢复上表的推荐默认值。
差异清单的真源是 `app/processors/defaults.py` 的 `LEGACY_OVERRIDES`（`preset_spec` 逐项应用）。

### 3.3 库级规则覆盖（`libraries.rule_override`）

**处理器是"这一类媒体库的默认规则"，库级覆盖是"这一个库的例外"。** 库只通过 `processor_id`
引用处理器；`libraries.rule_override` 是一段可空的 JSON，**只覆盖用户真正填了的子键**，其余一律
继承处理器（子键合并、数组整体替换）。它存在 DB 列里、由 WebUI 的「重命名规则（本库）」卡片编辑，
并随 `GET /api/libraries/{id}` 原样返回。

```json
{
  "naming":   { "template": "[{season}-{episode}] {title}{suffix}" },
  "traverse": {
    "accept_dirs": [{"pattern": "^Season (\\d+)$", "season_group": 1}],
    "reject_dirs": [{"pattern": "^SP$"}],
    "file_scope": "subdir",
    "skip_season_zero": true
  },
  "extensions": [".mkv", ".ts"]
}
```

允许的顶层键恰好三个：`naming` / `traverse` / `extensions`。

| 段 | 允许的子键 | 语义 |
|---|---|---|
| `naming` | `template`、`segments`、`expressions` | `template` 是 calibre 风格的**一行模板**，保存时编译成 `segments`（模板优先于处理器原有的段序列）；`segments` 若被直接提供则整体替换；`expressions` **按名增量合并**（不整体替换） |
| `traverse` | `accept_dirs`、`reject_dirs`、`file_scope`、`skip_season_zero` | 数组**整体替换**。`title_depth` **不可覆盖**（只支持 `1`，写它会得到一条中文报错） |
| `extensions` | 字符串数组 | 每项以点开头；**三层优先级**见下 |

**命名模板（calibre 风格）**：用 `{变量}` 拼一行，变量名就是 §3.1 那 12 个上下文名
（`title`/`filename`/`stem`/`ext`/`extname`/`season_raw`/`season`/`episode_raw`/`episode`/
`suffix`/`works_dir`/`rel_dir`）。`{episode:02}` 表示补零到 2 位；字面花括号写 `{{` 与 `}}`；
认不出的宽度（如 `{episode:0x}`）只丢掉补零、不报错；**未知变量名**报中文错误（前端边写边报）。
编译后仍是段序列，所以拆完还能逐段微调 —— 段序列始终是唯一真源。

> 模板**只存在于库级覆盖里**，不进 `processors/*.yaml`：处理器编辑器继续用段序列 UI
> （按钮「按模板生成段序列」可以从一行模板生成段），库编辑页给的是模板输入框 + 变量按钮 + 实时预览。
> 这样就不必发明"模板与段序列同时给出时谁赢"的语义。

**后缀白名单的三层优先级**（全局重命名后缀白名单，`app/processors/overrides.py::effective_extensions`）：

```
库级 rule_override.extensions  →  全局设置 settings.rename_extensions  →  处理器 spec.extensions  →  内置默认 [.mkv, .mp4, .ass]
```

**全局白名单对所有媒体库生效**（设置页「允许重命名的文件后缀」，`PUT /api/settings` 的
`rename_extensions`）：留空 `[]` = **未设置**，退回每个库自己的处理器清单；
填了就要每项以点开头，保存时统一转小写并去重。处理器 YAML 里的 `extensions` 因此降级为
"没设全局值时的回退值"，处理器页仍有该字段（存量配置继续可用）。

**校验**：`PUT/POST /api/libraries` 会先用 `validate_override` 做字段级校验，再把覆盖合并进处理器
规格跑一次 `validate_spec` 查**跨层冲突**（例如处理器是 `file_scope: title` 而库级覆盖给了
`accept_dirs`）。任何一条不过都返回 422，`detail` 是 `[{loc, msg, type}]` 列表，逐条中文说明。

---

## 4. 规则引擎语义（必须逐条实现）

输入：`spec`（§3）+ `root`（该媒体库的 `target_dir`）。执行体：`app/processors/engine.py`。

签名：`run_rename(spec, root, *, log_file=None, folder=None, force=False, now=None, works_dir="") -> RenameOutcome`
（`spec` 可传 dict 或 `ProcessorSpec`；`log_file` 是**完整路径**，缺省时按 spec 的 `log_file` + `settings.log_dir` 解析；
`folder` 只处理一个标题文件夹；`now` 供测试注入时间；`works_dir` 写日志条目的 `works_dir` 用，
默认空串以便纯函数测试不需要 DB。）

1. 遍历 `root` 的**直接子目录** → 每个是一个**标题文件夹** `T`。
2. 若 `T` 的名字命中任一 `reject_dirs` ⇒ **跳过 T 整棵子树**，记 `warnings`。
3. 计算 `size(T)` = `os.walk` 累加所有文件 `st_size`。
4. 读该处理器的日志文件；若已有 `T` 且 `size` 相同 ⇒ 打印
   `Skipping folder: {T} (already processed at {ts})` 并跳过（**这是唯一的"已处理"判据**）。
5. 取文件作用域：
   - `file_scope: title` ⇒ 作用域 = `[T]`，`season_raw = ""`
   - `file_scope: subdir` ⇒ 候选 = `T` 的直接子目录，按顺序：
     ① 命中 `reject_dirs` ⇒ 跳过该子目录；② `accept_dirs` 非空且不匹配 ⇒ 跳过；
     ③ `skip_season_zero` 且 `season_raw.zfill(2) == "00"` ⇒ 跳过；④ 其余为作用域目录。
6. 作用域内文件 = **仅该目录的直接子项**，且 `name.endswith(tuple(extensions))`（**大小写敏感**）。
   进一步的 `isfile` 过滤**按作用域类型分化**：
   - `file_scope: subdir` ⇒ **要**过滤非文件项（要求 `os.path.isfile`）；
   - `file_scope: title` ⇒ **不过滤**（直接 `name.endswith(...)`
     就拼新名，目录名若恰好命中后缀白名单也会进候选）。

   > 两种作用域的处理本来就不一致，**以本节为准**。
7. 选择：
   - **先做 L3 特典剔除**：候选里命中**全局设置项** `extras_patterns` 的文件**先被剔出**改名候选
     （它们不参与争名、也不会顶掉正片的干净名，且**不计入** `episode_unresolved`），留到第 12 步统一记账。
     ⚠️ **必须在这一步（"每个扩展名只留最大者"）之前剔除** —— 否则特典会先抢到"同扩展名最大"的名额、
     随后被剔掉，于是正片继续等下一轮，而下一轮整目录会因"体积一致"被 `Skipping folder` 跳过（真缺陷）。
     判据是对**含扩展名的文件名**做**大小写不敏感**的 `re.search`；**只作用于作用域目录的直接文件**，
     嵌套子目录（如 `Season 1/特典/`）里的文件一律不动；`extras_patterns: []` = 关闭 L3。
   - `per: file` ⇒ 全部
   - `per: extension` ⇒ 按 `name.split('.')[-1]` 分组，每组只留 `st_size` 最大者
8. 求值 `naming.expressions`（按书写顺序），再按 `naming.segments` 拼出新文件名。
9. **没解析出集数就不改名**（见 §3.2 有意增量 ③）：若 `naming.segments` **引用了集数**
   （`episode` / `episode_raw`）而该文件"没解析出集数"——`episode_patterns` 一个都没命中 **且**
   `episode_fallback` 留空（`null`）——⇒ 该文件**保留原文件名、不执行 `os.rename`**
   （硬链接阶段已经用原名把它链进目标）：日志里打一行 `Kept (no episode): {相对路径}`，
   计入 `RenameOutcome.kept_unresolved_episode`，`warnings` 里再汇总一条中文告警；
   任务仍算成功。**显式配置**了非 `null` 的 `episode_fallback`（例如 `legacy` 预设的字符串 `"None"`）
   属于"已解析"，照旧把那**一个字面量**渲染进文件名。动机：若把认不出集数的文件都命名成
   `… S01ENone.mkv`，同一作品的不同压制版本会撞成同一个目标名、`os.rename` 互相覆盖（真实事故）。
10. **L2 定名 + `os.rename`**：改名**之前**先对整个作用域的候选调用 `_resolve_target_names()`
    定下最终名 —— 按渲染出的干净名分组、组内按**体积从大到小**（相同则按目录列出的先后），**体积最大的拿干净名**，
    其余依次加 ` (2)`、` (3)`…（模板 `" ({index})"`，上限 **999**）。目标路径**本来就存在**时：原有文件比新来的**小**
    ⇒ 原有文件改名**让位**（**不删除**，计入 `displaced_targets`）、新来的拿干净名；否则新来的自己加后缀。
    **幂等**（候选后缀路径已经是本文件自己的 inode ⇒ 照用，不越叠越多）；找不到空位 ⇒ **保留原文件名** + 记一条
    `error`（**绝不覆盖**）。然后 `os.rename(old, new)`（**只在库侧**）；成功打印 `Renamed: {old} -> {new}`
    （加过后缀时是 `Renamed (suffix): {old} -> {new}`），失败打印 `Error renaming {old}: {err}` 并计入 `errors`。
    ⚠️ 这是**与 `os.rename` 直接覆盖的有意分叉**：不经 L2 定名就 `os.rename` 会在 POSIX 上**静默覆盖**（真实事故：全库跑丢 114 个文件），
    在 Windows 上则是 `[WinError 183]`；`LEGACY_OVERRIDES` 只声明预设字段差异、**不解除** L2。
11. 处理完 `T` 后写日志 `{T: {"size": size(T), "timestamp": now, "works_dir": <绝对容器路径>}}`，
    打印 `Folder processed: {T}, Size: {n} bytes, Timestamp: {ts}`。
    **读日志时忽略未知键**（老日志里的条目没有 `works_dir` 也要能读）。
    > 第 9 步"保留原名"的文件**也**会走到这一步：标题文件夹仍有条目，
    > 所以下一轮同 size 判据照样生效。
12. **L3 特典排除（不移动）**：第 7 步剔出来的命中文件**原地不动** ——
    名字不改、位置不挪（目标侧那条硬链接保持原样），也**不会**创建任何目录（不归入
    `作品名/其他/`）。引擎按作用域逐个打印一行
    `Extra (kept): {作用域标签}`，并计入 `RenameOutcome.extras_classified`
    （`{dir, name, size, pattern}`；`dir` 是作用域标签、`name` 是原文件名、`pattern` 是命中的那条判据
    —— 便于"发现误判就去把判据写精确"）。**这是 L3 带来的行为**：误判了正片不需要手工搬回，
    改好判据后下一轮它会重新走正常改名流程（因为它**从没被移动过**）。

**输出结构**（`RenameOutcome`，供执行器与 WebUI 使用）：
`{renamed: [{dir, old, new}], skipped: [{folder, reason, at}], kept_unresolved_episode: [str],
episode_unresolved: [str], name_collisions: [{dir,target,previous,current,kind}],
renamed_with_suffix: [{dir,old,clean,target,size,reason,displaced}],
displaced_targets: [{dir,old,target,clean,size}], extras_classified: [{dir,name,size,pattern}],
unknown_params: [{label, file, params}], unscoped_works: [{dir, folder, files, samples}],
errors: [{dir, file, error}], folders_scanned, folders_processed, warnings: [str], log_file}`
（`kept_unresolved_episode` 的元素是"相对媒体库根目录"的路径字符串；
`name_collisions` / `renamed_with_suffix` / `displaced_targets` 来自 L2/L4，
`extras_classified` 来自 L3；`unknown_params` 对应 **KI-11**：
元素 `{label, file, params}` —— `label` 是作用域标签（`作品名/子目录/文件名`）、`file` 是源文件名、
`params` 是这次渲染里"上下文没有、被留空"的变量名（排序去重）；执行器把它们转成 `Task.result`
上的同名计数 + `samples.*` 明细。）

### 4.1 行为等价性（冻结黄金值）

> 规则引擎 `app/processors/engine.py` 是**唯一执行体**，没有任何"跑两套实现再逐字节
> 比对"的差分测试。行为由项目自己的回归测试用
> **冻结的黄金值**守护 —— `tests/test_processor_engine.py`、`tests/test_bangumi_episode_rules.py`、
> `tests/test_rename_extensions.py`（文件树、`Renamed:` 行集合、日志 JSON 的键集合与 size）。
> `legacy` 预设代表**最朴素的旧口径**（差异清单见下）。
>
> `default` 相对 `legacy` 的差异**只允许**出现在 §3.2 标注的**有意增量**上
> （① bangumi 的 `reject_dirs`、② movie 的 `reject_dirs`、③ bangumi 的 `episode_fallback`、
> ④ bangumi 的 `episode_patterns`），且**逐条单独断言**。
> ⚠️ ⑤ **L3 特典排除不是"预设差异"**：它是**全局设置**，对 `default` 与 `legacy` **同等生效**
> （`extras_patterns: []` 可整体关闭），所以**不进**这份差异清单、也不写在 `LEGACY_OVERRIDES` 里；
> 它由 `tests/test_extras_classification.py` 单独覆盖。

---

## 5. 日志

### 5.1 三类日志

| 类别 | 位置 | 形态 |
|---|---|---|
| **硬链接日志** | `/data/logs/hard_links.log` | 纯文本 `%(asctime)s - %(message)s`（由 `app/processors/hardlink.py` 的 `logging` 写，FileHandler 用同格式、UTF-8）。这个 handler 上还挂着「日志隔离」白名单过滤器，**只放行两类记账行**（见 §5.2 末条）。这个文件是**兼容与审计**台账：跳过判据由每库账本 `link_records` 提供（见 §2.1），**行格式与写法规矩不变** |
| **每个媒体库一条重命名日志** | `/data/logs/<log_file>` | `json_cache`：`{ "<标题文件夹>": {"size": N, "timestamp": "YYYY-MM-DD HH:MM:SS", "works_dir": "/vol1/1000/Download/qbittorrent/..."} }`，`ensure_ascii=False, indent=4` |
| **任务日志（WebUI 用）** | SQLite `task_logs` + 内存环形缓冲 | 全部处理器共享；任务详情页轮询它 |

### 5.2 硬性要求

- **所有重命名日志必须在同一目录**：`settings.log_dir`（默认 `/data/logs`），**不允许**按媒体库分子目录。
- **每个媒体库各自一条日志**（这一条依赖下面的「按库唯一化」）。缺省命名顺序：
  1. **该处理器登记的 `log_file`**：内置三个分别是 `bangumicache.log` / `moviecache.log` /
     `hardlink_only.log`，自定义处理器是它的 `<id>cache.log`；
  2. 拿到名字后**按库唯一化**：若该名已被**其它库**或**其它处理器**占用，就在扩展名前追加
     `-2`、`-3`…（例如第二个用 `bangumi` 的库会拿到 `bangumicache-2.log`）。
  **第一个**使用某处理器的库保持该处理器的登记名；**磁盘上已有同名文件时照样用它并继续追加**
  > 为什么必须按库唯一：日志记的是"这个标题文件夹**已经处理过**、体积多少"。两个库共用一份日志时，
  > A 库处理过的同名同体积文件夹会让 B 库**静默跳过**（`engine.py` 的"体积一致 ⇒ `Skipping folder`"
  > 分支），用户看到的是"B 库跑了但什么都没做" —— 静默的数据正确性问题，所以按库隔离。
  兜底（处理器连登记名都没有）：按库名转写 `"{slug}cache.log"`；`slug` 由库名生成
  （小写、非 `[a-z0-9]` 折叠为 `-`；纯中文库名会退化成通用名，此时改用处理器 id 当种子）。
- `log_file` 必须**只允许文件名**（不得含 `/`、`..`）且**必须以 `.log` 结尾**；**显式传入**的名字
  与**其它库已占用**的名字撞车 ⇒ **422**（字段 `log_file`，中文 detail）。以上都在
  `POST/PUT /api/libraries` 中强校验。
- 新建库时若日志文件不存在则创建空 `{}`；**不从任何外部目录播种**。
  已经用过的 `/data/logs/bangumicache.log`、`/data/logs/moviecache.log` **原样保留、继续追加**。
- 硬链接日志**不做播种、也不做 `Source` 前缀本地化**——见 §5.3。

### 5.3 硬链接日志：只写不读，不改写

`hard_links.log` 是**兼容与审计**台账：应用**从不改写它**，也不把任何外部日志"播种"进数据卷。

理由（详见 `docs/contracts/BASELINE.md` §4.1）：每个媒体库有自己的重命名日志，不再由多个库共用一份
`hard_links.log`；跳过判据由每库账本 `link_records` 承担，而这份日志里的 `source_dir`
直接就是媒体库配置的**容器路径**，不存在宿主/容器两套写法，因此也没有"前缀本地化"这回事。

- **`hard_links.log` 只接收记账行**（「日志隔离」过滤器）：`ensure_file_logger()` 在这个 FileHandler 上挂了
  白名单过滤器 `_BookkeepingFilter`（`app/processors/hardlink.py`，前缀常量 `_LOG_BOOKKEEPING_PREFIXES`），
  只有两类能通过：① `Deleted orphaned hard link folder: …`；② `Source: …`
  （且必须**同时**含 ` - Folder: ` 与 ` - Folder size: `）。应用自身的日志（uvicorn 访问日志、任务日志、
  异常回溯）以及与这两类"近似但不匹配"的行一律丢弃 —— 根因是 `ensure_file_logger()` 把 FileHandler 挂在
  **根 logger** 上，不设白名单就会把整个应用的日志灌进这个数据文件。
  **只过滤、不清理、不轮转**：历史行一个字节都不删，行格式与 `_parse_log_line` 的解析契约都没有变；
  `max_task_log_lines` / `max_history` 只约束任务日志与任务历史，与这些数据文件无关。
  排查与自查见 `docs/guides/TROUBLESHOOTING.md` 场景 24，回归用例 `tests/test_file_log_isolation.py`。
- **WebUI 的「重命名日志」表由 `GET /api/system/info` 的 `rename_logs` 提供**（前端**不直接读文件**）：
  逐库一条、顺序与 `libraries` 一致，字段 `{library_id, library_name, log_file, exists, entries, last_write}`；
  `exists=false` ⇒ `entries=0` 且 `last_write=null`；`entries` 是日志的顶层键计数（缓存未命中时读盘统计）；
  `last_write` 是本地时间 `YYYY-MM-DD HH:MM:SS`（与 `GET /api/config` 的 `files[].updated_at` 同格式，不是 ISO/UTC）。
- ⚠️ 磁盘上可能已经有 `/data/logs/hard_links.log`（历史运行留下的）。应用直接继续使用它，
  **不做任何改写**。

---

## 6. 配置文件与双向同步（库级规则覆盖）

### 6.1 文件布局（`settings.config_dir`，默认 `/data/config`）

```
/data/config/
├── libraries.yaml            # 媒体库清单（含 processor_id、路径、匹配条件、log_file）
└── processors/
    ├── bangumi.yaml          # 一个处理器一个文件，文件名 = <id>.yaml
    ├── movie.yaml
    ├── hardlink_only.yaml
    └── <custom-id>.yaml
```

- 格式：**YAML 1.1**（`yaml.safe_load` / `yaml.safe_dump`，`allow_unicode=True`、`sort_keys=False`、
  `default_flow_style=False`），UTF-8、无 BOM、**以 `schema_version: 2` 开头**。
- 需要 `PyYAML`（`docker/Dockerfile` 的 pip 依赖新增 `PyYAML==6.0.3`）。
  > ⚠️ 必须是 **6.0.3**，不是 6.0.2：`PyYAML==6.0.2` 没有 Python 3.14 的预编译 wheel，
  > pip 会回落源码构建并失败（`error: failed-wheel-build-for-install`）；
  > 6.0.3 提供 wheel，测试环境与容器里的行为一致。
  选 YAML 而非 JSON 的唯一理由是**用户要手工编辑**：YAML 支持注释、正则不必双重转义、可读性显著更好。
- 正则一律写成**单引号标量**（`pattern: '^Season (\d+)$'`），避免 YAML 转义规则介入。
- 应用**不读** `.env`。

### 6.2 同步语义（单一真源，避免"双向"变成互相打架）

- **运行时真源 = SQLite**。DB 是唯一的写入仲裁者；配置文件是它的**可移植镜像**。
- **写出方向（DB → 文件）**：任何一次通过 API 的成功写入，都在同一请求内把受影响文件
  **原子写出**（写 `*.yaml.tmp` → `os.replace`），并把新 sha256 记进 `config_files`。
- **读入方向（文件 → DB）**：后台线程每 `settings.config_sync_interval_seconds`（默认 **5**）秒
  比对一次目录内文件的 mtime+sha256。若某文件哈希 ≠ 上次由应用写出/导入时记录的哈希，
  判定为**外部编辑**，执行导入：
  1. 解析 + `schema_version` 校验 + §3 全量校验；失败 ⇒ **不改 DB**（`imported` 返回 `null`，只写一条错误事件），
     写一条 `config_events` 错误记录，并通过 `GET /api/config` 的 `errors` 暴露给 WebUI 顶部横幅。
     `errors` 只反映**最近一次成功导入之后**发生的导入错误（按 `config_events.id` 判定、上限 20 条，**不是
     只追加的历史日志**）：导入失败不改 DB，修好文件重新导入成功后该数组自动清空；
  2. 成功 ⇒ 在一个事务里替换 `libraries` / `processors`，写 `config_events(direction='import')`，
     并让 WebUI 显示「配置文件已从磁盘重新导入」；
  3. 导入完成后**回写**一次文件（规范化格式），使哈希重新对齐，避免抖动。
- **删除语义**：文件里消失的**非内置**处理器 / 媒体库 ⇒ 随导入一并从 DB 删除（并写事件）。
  内置处理器**永不删除**，只允许重置。
- **冲突窗口**：API 写入持有 `_config_lock`；后台 watcher 导入持有同一把锁 ⇒ 不会交错。
- 同步线程的启停并入应用 lifespan；把设置项 `config_sync_interval_seconds` 设为 `0` ⇒ 关闭自动导入（只手工 reload）。**它不是环境变量**：周期只从 DB 设置表读，写进 `.env` / compose 不会生效。

### 6.3 `libraries.yaml` 结构

```yaml
schema_version: 2
libraries:
  - id: 1
    name: 番剧
    type: Bangumi                # 自由标签，仅用于展示与统计
    processor_id: bangumi        # 必须存在于 processors/
    enabled: true
    source_dir: /vol1/1000/Download/qbittorrent/Bangumi
    target_dir: /vol1/1000/Library/Bangumi
    match_categories: []
    match_tags: []
    match_path_prefixes: []
    orphan_cleanup: true
    permission_mode: false       # 默认关闭的显式开关
    log_file: bangumicache.log
```

---

## 7. HTTP API：处理器与配置同步端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/processors` | `{items:[ProcessorSpec], total, limit, offset}`；`?builtin=&kind=` |
| GET | `/api/processors/{id}` | 单条 |
| POST | `/api/processors` | 新建自定义处理器；`builtin=false`；`schema_version` 由服务端写 |
| PUT | `/api/processors/{id}` | 全量替换 spec；`id` 不可改；内置处理器**允许**改规则（写事件） |
| DELETE | `/api/processors/{id}` | 内置 ⇒ 409；被任何媒体库引用 ⇒ 409 并给出引用清单 |
| POST | `/api/processors/{id}/reset` | body `{"preset":"default"｜"legacy"}` ⇒ 重置为内置默认 / 最朴素的旧口径 |
| POST | `/api/processors/validate` | body 为 spec，**不落库**，返回 `{ok, errors:[{field,message}]}` |
| GET | `/api/config` | `{config_dir, files:[{path,relative,exists,sha256,in_sync,size,updated_at}], last_import_at, last_export_at, errors:[{path,message,at}]}` |
| POST | `/api/config/reload` | 强制从文件导入（`?force=true` 时忽略哈希） |
| POST | `/api/config/export` | 强制把 DB 写出到文件 |

共 **10 条**新增端点（其中 `validate` 不落库）。

- `POST /api/libraries` / `PUT /api/libraries/{id}`：`type` 与 `processor` 的**枚举约束取消**，
  改为「`processor_id` 必须存在」。`log_file` 缺省时按 §5.2 自动生成。
- `POST /api/libraries` / `PUT /api/libraries/{id}` 另接受 **`rule_override`**（对象或 `null`，见 §3.3）：
  - `POST` 省略或传 `null` ⇒ 不设覆盖；传 `{}` ⇒ 清空覆盖。
  - `PUT` **省略该键 = 保留现值**（`null` 语义与"删除覆盖"不同：要删就传 `{}`）。
  - `GET /api/libraries[/{id}]` 的每条库都带 `rule_override`（已归一化的对象，没有覆盖时为 `{}`）。
  - 校验失败一律 `422` + 中文字段级错误，逐条列出（含**跨层冲突**，如处理器 `file_scope: title`
    与库级 `accept_dirs` 同时存在）。
- `PUT /api/settings` 新增 `rename_extensions`（字符串数组，允许空数组 = 未设置；每项必须以点开头）。
  **另新增 `extras_patterns`**（**正则**字符串数组，一条一项，允许空数组 = **关闭 L3 特典排除**；
  保存时逐条 `re.compile`，有一条编不过就整体 `400` 并点名第几条与模式原文）。
- 校验失败一律 `422` + 中文字段级错误（`{"detail":[{"loc":[...],"msg":"中文"}]}`）。

---

## 8. 前端契约增量

- 新增 `#/processors`（处理器列表）与 `#/processors/{id}`（规则编辑器），并加入侧边导航。
- 规则编辑器四大块，与 §3.1–3.3 一一对应：
  1. **命名方式**：可视化"段"列表 —— 上下移调整顺序、增删段、选参数、`pad` 输入、`literal` 文本输入；
     **实时预览**（示例上下文渲染）；另给一个独立「命名表达式」区域（键值对增删）。
     该卡片顶部另有 **「命名模板」** 输入框（calibre 风格 `{变量}`，由当前段序列预填）
     + 12 个变量按钮 + 「按模板生成段序列」按钮；模板一边写一边校验未知变量。
  2. **接受目录正则**：可增删的 `{pattern, season_group}` 行；显示能否编译。
  3. **拒绝目录正则**：可增删的 `{pattern}` 行；附一行说明"命中即整棵子树跳过"。
  4. **接受的后缀名**：可增删的 chips；校验以 `.` 开头。
- 媒体库表单新增 **「重命名规则（本库）」** 卡片（`rule_override`，见 §3.3）：一个开关
  （关 = 完全跟随处理器，保存时提交 `{}` 清空覆盖）+ 命名模板输入框 + 变量按钮 + 实时预览
  + 接受/拒绝目录正则行（带季号捕获组输入）+ 本库后缀白名单 textarea；
  预览与错误提示都在卡片内，不必等保存才知道写错了。
- 设置页新增 **「允许重命名的文件后缀」** 卡片（`rename_extensions`）：每行一个后缀，
  留空 = 未设置，说明文字写明它的优先级高于处理器清单、且被库级覆盖压过。
- 设置页另新增 **「特典文件排除」** 卡片（`extras_patterns`，L3）：textarea，**一行一条正则**
  （**不按空格切**），非法正则保存时当场报错；留空/空数组 = **关闭特典排除**（命中文件回到普通改名流程）。
  命中者**不改名也不移动**：保留原名留在原位置（`其他/` 这个目录不存在）。
  它写的是**判据**而不是后缀名，别和上面那张卡混。
- 设置页的「特典文件排除」卡片里的判据**只做排除**：命中文件不会进 `其他/`，也不会被改名，
  任务结果里计数是 `extras_classified`（口径：`{dir,name,size,pattern}`）。
- 顶部固定两个按钮：「保存」（`PUT`）与「恢复默认」（`POST .../reset`，二次确认，弹窗内可选
  `default` / `legacy` 两个预设，并说明两者差异：legacy = 最朴素的旧口径、不含拒绝目录规则）。
- 媒体库表单的"处理器"下拉改为拉 `GET /api/processors`（不再写死三个选项）；新增 `log_file` 输入。
- 全局顶部增「配置文件同步」状态条：`in_sync === false` 的列出文件名；
  `errors` 非空时显示红色横幅 + 错误详情 + 「重新从磁盘导入」按钮；另给「立即写出配置」。
- 不得引入 CDN / 构建工具链；仍为原生 ES modules + 手写 CSS。

---

## 9. 容器化与配置增量

- **新增** env 只有 `CONFIG_DIR`（默认 `/data/config`）。同步周期**不是** env —— 它是 DB 设置项
  `config_sync_interval_seconds`（初值见 `app/db.py` 的 `DEFAULT_SETTINGS`，区间 **0–86400**，
  `0` = 关闭自动导入），在设置页 / `PUT /api/settings` 改。
- `/data/config` 位于 `/data` 卷内，**无需额外挂载**。
- Dockerfile：`COPY app/` 只带入应用自己的代码；pip 依赖含 `PyYAML==6.0.3`。
- `docker/entrypoint.sh`：启动自检检查 `/data/config` 可写等条件。
- `HEALTHCHECK` 共 **六项**：`db`、`data_dir`、`downloads_readable`、`library_writable`、`processors`、`config`
  （`processors` = 三个内置处理器已加载）。

---

## 10. 数据模型迁移

- `init_db()` 幂等新增表：
  - `processors(id TEXT PK, name TEXT, kind TEXT, builtin INT, spec_json TEXT, created_at TEXT, updated_at TEXT)`
  - `config_files(path TEXT PK, sha256 TEXT, at TEXT)`
  - `config_events(id INTEGER PK, path TEXT, direction TEXT, sha256 TEXT, at TEXT, note TEXT)`
  - `libraries` 新增列 `processor_id TEXT`、`log_file TEXT`（用 `PRAGMA table_info` 检查缺列再 `ALTER TABLE`）；
    `libraries` 再新增 `rule_override TEXT`（可空 JSON，见 §3.3），同样是缺列才 `ALTER TABLE`，老行保持 `NULL`。
- 首次启动：`processors` 为空 ⇒ 写入三个内置默认；旧库里 `libraries.processor` 的取值
  （`bangumi`/`movie`/`hardlink_only`）直接映射到同 `id`，无需人工干预。
- 首次启动就把 `libraries.yaml` 与 `processors/*.yaml` 写出来（等价于一次 export）。
- **既有数据卷无需任何手工操作**；`/data/logs/*.log` 原样继续使用。