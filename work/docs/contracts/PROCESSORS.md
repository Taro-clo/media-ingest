# 处理器与规则契约

> **本文的读者**：要定制重命名规则的维护者、要接处理器语义的开发者、以及要判断「某次改名为什么这样命名」的运维。
> 只想知道怎么用请读 [`docs/guides/USER_GUIDE.md`](../guides/USER_GUIDE.md)，安装与升级见 [`docs/guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md)。
>
> **本文是处理器语义的权威定义**：处理器规格（`ProcessorSpec`）的字段、规则引擎的逐条语义、命名变量与段的含义、
> 内置处理器的默认规则、库级覆盖的合并规则，均以本文为准。HTTP 端点的路径、请求体与响应形状由
> [`docs/contracts/API.md`](API.md) 的 §5.7 / §5.8 定义，本文只写语义与约束。
>
> 当前形态：容器自包含、不依赖宿主额外目录；重命名规则完全配置化（参数与顺序、接受/拒绝目录正则、后缀白名单），
> 配置文件与 WebUI 双向同步；每个媒体库各自一条重命名日志，所有日志集中在同一目录。

---

## 1. 三条不可动摇的底线

1. **绝不修改 qBittorrent 的原始下载文件**。单挂载形态下「源目录单独 `:ro` 挂载」与「跨路径硬链接」在内核层互斥，
   结构性只读不可用，改由**可验证的保证**接管：代码层永不写源目录 + 每次任务前后逐文件比对源完整性
   （`Task.result.source_integrity` 与 `warnings[]`）+ 回归用例 `tests/test_no_source_mutation.py`。
   挂载前提见 [`docs/guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md) 的「源目录的保护」一节。
2. **绝不把跨文件系统失败降级为复制**：`EXDEV` 必须报错并说明原因（复制会占双倍空间且断掉做种关联）。
3. **重命名只发生在媒体库一侧**（`target_dir`），源目录一个字节都不动。

---

## 2. 硬链接与重命名的实现选择

### 2.1 硬链接：行为稳定，实现自包含

硬链接的实现是一套**扫描与编排算法**，其中不含任何「每个媒体库不同」的规则——源目录与目标目录都由媒体库配置提供，
所以它没有可配置面。它由 `work/app/processors/hardlink.py` 在应用进程内完成，容器不依赖任何外部程序或宿主目录；
行为等价由 `tests/test_hardlink_native.py` 的 A 组以**固定的黄金值**守护（全新树 / 已记录 / 体积变化 / 源目录消失 /
日志损坏五种场景）。有意不做的两条副作用：`chmod 777` 全库扫描与 `os.getlogin`（见 BASELINE.md 的「权限模式」一节）。

**跳过判据来自每库账本，日志照写。** 签名是 `create_hard_links(source_dir, target_dir, *, skip: dict[str, int] | None = None)`：
`skip=None`（默认）时读**全局** `hard_links.log`（`read_logged_folders(source_dir)`），这是黄金基线依赖的路径；应用侧
（`work/app/executor.py`）改为传 `skip`，判据来自**本库账本**——`work/app/link_records.py` 的
`folder_sizes(library_id, source_dir=…, target_dir=…)`，键是 `(library_id, folder)`，只有「本库 + 同 `source_dir` + 同
`target_dir` + 同体积」四项全一致才跳过。两种情况下 `Logged: <folder> - <folder size>` / `文件夹已记录且体积一致，跳过: <folder>`
的打印行与记账行都原样照写：日志仍是兼容与审计台账，只是不再决定跳不跳。这修掉了两个旧缺陷——**同源多库**（两个库指向
同一 `source_dir` 时，A 库跑完后 B 库全被判「体积一致」而一个新链接都不建）与**同库换目标**（旧判据里没有目标目录）。
细节见 [`docs/contracts/BASELINE.md`](BASELINE.md) 的「硬链接契约」。

### 2.2 重命名为什么用规则引擎

季目录正则、集数正则、后缀白名单、命名模板若内联在代码里，就无法在不改代码的前提下调整；而规则要求写进配置文件、
可在 WebUI 双向修改，并且新增媒体库要能用不同的命名逻辑。因此重命名走 §4 的通用规则引擎，内置处理器各带一份默认规则（§3.2）。
等价性由 `tests/test_processor_engine.py`、`tests/test_bangumi_episode_rules.py`、`tests/test_rename_extensions.py` 以
**固定的黄金值**守护（`legacy` 预设同样必须通过）。

---

## 3. 处理器规格（ProcessorSpec）

一个处理器 = 一个 YAML 文档，`id` 全局唯一（`^[a-z][a-z0-9_-]{1,31}$`）。内置三个（`builtin: true`，可重置、不可删除）：

| id | name | kind | 实现 |
|---|---|---|---|
| `hardlink_only` | 只建硬链接 | `hardlink_only` | `work/app/processors/hardlink.py`（行为见 §2.1） |
| `bangumi` | 番剧 | `rename` | 规则引擎 `work/app/processors/engine.py`（默认规则见 §3.2） |
| `movie` | 电影 | `rename` | 规则引擎（默认规则见 §3.2） |

`kind` 决定这个处理器走哪条流水线：`hardlink_only` 只建链不改名，`rename` 建链后再改名。

```yaml
schema_version: 2                 # 由服务端写入
id: bangumi                       # ^[a-z][a-z0-9_-]{1,31}$
name: 番剧
description: 按「作品/Season N/文件」结构重命名为 SxxExx
kind: rename                      # rename | hardlink_only
enabled: true
builtin: true                     # 内置：可重置、不可删除
traverse:
  title_depth: 1                  # 标题文件夹在根下的层级；当前固定 1，不可覆盖
  file_scope: subdir              # subdir：取自匹配 accept_dirs 的子目录；title：直接取自标题文件夹（Movie 语义）
  accept_dirs:                    # 空数组 ⇒ 任何子目录都接受（只在 file_scope=subdir 时有意义）
    - {pattern: '^Season (\d+)$', season_group: 1}
  reject_dirs:                    # 命中即整棵子树跳过；大小写不敏感 + 整名匹配
    - {pattern: '(?i)^(sp|cm|op|ed)s?$'}
  skip_season_zero: true          # season_group 补零后为 "00" 时跳过
extensions: [.mkv, .mp4, .ass]    # 回退值；全局 rename_extensions 或库级覆盖非空时优先（§3.3）
naming:
  expressions: {}                 # 具名表达式，按书写顺序求值，可引用上下文与更早的表达式
  segments:                       # 最终文件名的组装顺序
    - {param: title}
    - {literal: ' S'}
    - {param: season, pad: 2}
    - {literal: E}
    - {param: episode, pad: 2}
    - {param: suffix}
episode_patterns:                 # 有序规则，序号即优先级；内置默认的 9 条见 §3.2
  - name: chapter
    pattern: '(?i)第\s*(?P<ep>\d{1,2})(?!\d)\s*[集话話回]'
episode_fallback: null            # 全不命中时用的字面量；null = 认不出集数就保留原名
selection:
  per: file                       # file = 每个匹配文件都处理；extension = 同后缀只取体积最大者
log_file: bangumicache.log        # 只允许文件名；落在 settings.log_dir（默认 /data/logs）
log_format: json_cache            # { "<标题文件夹>": {"size": N, "timestamp": "YYYY-MM-DD HH:MM:SS"} }
```

正则一律写成**单引号标量**（`pattern: '^Season (\d+)$'`），避免 YAML 转义规则介入。`accept_dirs` 的 `season_group` 缺省 `1`，
合法取值是 `0` 或 `≥1` 且不超过该正则的捕获组数量；`season_group: 0` 与空 `accept_dirs` 是同一类情形——只用这条正则判断
接不接受、不取季号（`{season_raw}` 为空串，`{season}` 仍补零为 `"00"`）。越界在保存期报错并提示改法。

### 3.1 `naming` 的可用名字

`segments[].param` 与 `expressions` 的右值都可以引用下列**上下文名字**（`TEMPLATE_VARS`，共 12 个）：

| 名字 | 取值 |
|---|---|
| `title` | 标题文件夹名 |
| `filename` | 作用域内当前文件名（含扩展名） |
| `stem` | 原文件名去掉最后一个扩展名 |
| `ext` | 扩展名，带点：`.mkv` |
| `extname` | 扩展名，不带点：`mkv` |
| `season_raw` | `accept_dirs` 的 `season_group` 捕获组（字符串）。`season_group: 0`、`accept_dirs` 为空、或 `file_scope=title` ⇒ `""` |
| `season` | `season_raw.zfill(2)`；无季号时 `"".zfill(2)` ⇒ `"00"`，唯一为 `""` 的情形是 `file_scope=title` |
| `episode_raw` | `episode_patterns` 按书写顺序首个命中规则里序号最小的非空捕获组；全不命中为 `episode_fallback`（`null` ⇒ 空串） |
| `episode` | `episode_raw.zfill(2)` |
| `suffix` | `.ass` 文件取多段后缀（`.chs.ass`）；其他文件等价于 `ext` |
| `works_dir` | 该媒体库的 `source_dir`（绝对容器路径） |
| `rel_dir` | 作用域目录相对标题文件夹的路径 |

- **集数没认出来时**：`episode_patterns` 全部未命中**且** `episode_fallback` 为空 ⇒ `episode_raw` 与 `episode` 都是空串
  （不是字符串 `None`）；此时若 `segments` 引用了它们，引擎**不给这个文件改名**（见 §4 第 9 步）。显式配置了非空
  `episode_fallback`（含 `legacy` 预设的字符串 `"None"`）算「已解析」，照旧渲染那个字面量。空串或纯空白**等价于 `null`**。
  **只读内部名字 `episode_resolved`**：`true` = 命中模式或配了兜底；它不在 WebUI 变量表里，但模板与段都能读它
  （`known_context_names()` = `TEMPLATE_VARS` + `ENGINE_ONLY_VARS` + 本 spec 的表达式名）。
- `expressions` 的右值是 Python 表达式，可用 `re`、`os`、`len`、`str`、`int`、`sorted`、`min`、`max`，以及辅助函数
  `zfill(v, n)`、`splitext(p)`、`basename(p)`、`ass_suffix(filename)`。它在应用进程内 `eval`，等价于允许在配置文件里执行
  Python 表达式——这是为「可扩展、不写死」接受的取舍；配置文件位于 `/data` 数据卷内，能改它的人本来就能改容器配置。
- `segments[]` 只接受 `param` / `literal` / `pad` 三个键；`param` 先查 `expressions`、再查上表；`pad` 做 `str(v).zfill(pad)`。
  **段里的 `param` 必须是可用变量名**（KI-11）：保存期（`POST`/`PUT /api/processors`、库级覆盖、配置文件导入）直接 `422`，
  提示「段里用了未知变量 {xxx}。可用变量：…；自定义名字要先在 `naming.expressions` 里定义。」；库级覆盖的 `{…}` 模板
  按**合并后**的名单校验。运行期不拒绝整份规格（旧配置与手改 YAML 必须能跑完），而是记 `result.unknown_params` 计数 +
  每个文件一条 WARNING + `warnings` 汇总——写错名字的后果是那一段留空、文件名少一段却照样算改名成功。

### 3.2 内置处理器的默认规则

**bangumi**：段序列 `title` + ` S` + `season` + `E` + `episode` + `suffix`；`accept_dirs` 认 `^Season (\d+)$`；
`skip_season_zero: true`；`selection.per: file`；`extensions: [.mkv, .mp4, .ass]`；`episode_fallback: null`。
`episode_patterns` 是 9 条**有序**规则（序号即优先级；引擎按书写顺序 `search`，第一条命中的规则胜出，**不比较位置先后**）。
三条硬性要求：每条都把集号捕获成命名组 `ep`（引擎只取命名组；`legacy` 的两条用 `g1..g8`，靠组序号取第一个非空组）、
全部用 `(?i)` 忽略大小写、数字只允许 1~2 位（`(?!\d)` 排除 3 位以上）。

| # | `name` | 正则（省略公共前缀 `(?i)`） | 命中例 ⇒ 集号 | 反例 |
|---|---|---|---|---|
| 1 | `chapter` | `第\s*(?P<ep>\d{1,2})(?!\d)\s*[集话話回]` | `第 12 話.mkv` | `第123話.mkv` |
| 2 | `episode_word` | `(?<![A-Za-z])Episode\s*(?P<ep>\d{1,2})(?!\d)` | `Episode 3.mkv` | `Episode123.mkv` |
| 3 | `season_episode` | `(?<![A-Za-z])S\s*\d{1,2}(?!\d)\s*E\s*(?P<ep>\d{1,2})(?!\d)` | `S02E07.mkv` | `S123E07.mkv` |
| 4 | `bracketed` | `\[\s*(?P<ep>\d{1,2})\s*\]` | `[05].mkv` | `[1920x1080].mkv`、`[1AE35F3A].mkv` |
| 5 | `ep_marker` | `(?<![A-Za-z])EP\s*(?P<ep>\d{1,2})(?!\d)` | `EP09.mkv` | `PREP09` |
| 6 | `hash_marker` | `#\s*(?P<ep>\d{1,2})(?!\d)` | `#04.mkv` | — |
| 7 | `dash` | `-\s*(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)` | `- 11.mkv` | `- 10 bit.mkv` |
| 8 | `bare_e` | `(?<=\s)[Ee]\s*(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)(?=\s\|\.)` | `E12.mkv` | `Show.E07.mkv`（`E` 前不是空白） |
| 9 | `bare_number` | `(?<=\s)(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)(?=\s\|\.)` | `Show 12.mkv` | `Show 12v2.mkv`、`Show 1920x1080.mkv` |

1~7 是「有明确标记」的写法，8~9 是兜底（8 只认「空白 + `E`/`e` + 数字」，紧贴点号的 `Show.E07.mkv` 不命中；9 要求数字前是
空白、后是空白或句点，分辨率 / 码率 / `12v2` 不会被当成集号）。`\s` 含全角空白（`\u3000`）。全不命中 ⇒ 保留原名。
`reject_dirs` 覆盖常见特典 / 扫图 / 分卷目录：SP/CM/OP/ED（含复数）、Scan(s)、CD(s)、映像特典、特典CD、Preview(s)、
`Scan (Vol.N)`、`Vol.N`、EXTRA/Bonus/Menus/Music、Subtitle(s)、ScreenShot(s)、NCOP/NCED；全部大小写不敏感 + 整名匹配。

**movie**：段序列 `title` + `.` + `extname`；`file_scope: title`；`selection.per: extension`；`extensions` 同 bangumi；
`reject_dirs` 由用户指定，必选 `特典`/`CD`/`扫图`/`scans`，另加同义与常见特典目录名（SP、Menu、Preview、EXTRA、Bonus、
NCOP/NCED、Sample），每条由 `description` 说明来源（`safe_dump` 不保留注释）。完整取值见 `work/app/processors/defaults.py`。

**五处相对旧口径的有意行为增量**（`CHANGELOG` 与 [`docs/contracts/COMPATIBILITY.md`](COMPATIBILITY.md) 需显式标注）：
① bangumi 的 `reject_dirs`（旧口径不拒绝任何目录）；② movie 的 `reject_dirs`；③ bangumi 的 `episode_fallback: null`
（旧口径渲染字符串 `None` ⇒ `… S01ENone.mkv`，同一作品的不同压制版本会撞成同一目标名互相覆盖）；④ bangumi 的
`episode_patterns`（9 条有序规则，`legacy` 预设换回两条旧口径规则 `BANGUMI_LEGACY_EPISODE_PATTERNS`）；⑤ **L3 特典排除**。
①–④ 是**字段级差异**（写在处理器规格里、`preset` 可切换）；⑤ 是**全局设置项** `extras_patterns`，对所有处理器与库生效、
`legacy` 预设也不解除它，因此**不属于预设差异**、不进 `LEGACY_OVERRIDES`。它存在的理由：`reject_dirs` 只匹配目录名，
与正片平级的散装 `NCOP.mkv` / `Preview.mkv` 不在其管辖内，没有 L3 就会被当正片套上集号。

**两个内置处理器都有 `legacy` 预设**（`POST /api/processors/{id}/reset {"preset":"legacy"}`），相对 `default` 有**三处**差异，
逐项来自 `work/app/processors/defaults.py` 的 `LEGACY_OVERRIDES`：`traverse.reject_dirs: []` + `episode_fallback: "None"` +
`episode_patterns: [ep, trailing]`（两条旧口径正则）。`{"preset":"default"}` 恢复上述默认值。新建处理器用
`{"template":"bangumi"}` 拿到的同样是 `null` 与 9 条默认规则（默认值来自 `builtin_spec()` 本身，不是靠播种后的库）。

**想把既有实例改回「留空」**：编辑页的「集数兜底」是普通文本框，清空后提交的是空串 `""`（引擎当成「已解析」），所以没有
GUI 路径，只能用 ① 把 `/data/config/processors/<id>.yaml` 的 `episode_fallback` 写成 `null`（双向同步默认 5 秒一轮）；
② `PUT /api/processors/{id}` 传 `{"episode_fallback": null}`；③ `reset` 回 `default`（整套回默认值，会覆盖其它自定义）。

### 3.3 库级规则覆盖（`libraries.rule_override`）

**处理器是「这一类媒体库的默认规则」，库级覆盖是「这一个库的例外」。** 库只通过 `processor_id` 引用处理器；`rule_override`
是一段可空 JSON，**只覆盖用户真正填了的子键**，其余继承处理器（子键合并、数组整体替换）。它存在 DB 列里，由 WebUI 的
「重命名规则（本库）」卡片编辑，并随 `GET /api/libraries/{id}` 原样返回。允许的顶层键恰好三个：

| 段 | 允许的子键 | 语义 |
|---|---|---|
| `naming` | `template`、`segments`、`expressions` | `template` 是 calibre 风格的一行模板，保存时编译成 `segments`（模板优先于处理器原有的段序列）；`segments` 若直接提供则整体替换；`expressions` 按名增量合并 |
| `traverse` | `accept_dirs`、`reject_dirs`、`file_scope`、`skip_season_zero` | 数组整体替换；`title_depth` **不可覆盖**（只支持 `1`，写它会得到一条中文报错） |
| `extensions` | 字符串数组 | 每项以点开头；优先级见下 |

```json
{"naming": {"template": "[{season}-{episode}] {title}{suffix}"},
 "traverse": {"accept_dirs": [{"pattern": "^Season (\\d+)$", "season_group": 1}], "reject_dirs": [{"pattern": "^SP$"}]},
 "extensions": [".mkv", ".ts"]}
```

**命名模板**用 `{变量}` 拼一行，变量名就是 §3.1 的 12 个上下文名。`{episode:02}` 补零到 2 位；字面花括号写 `{{` 与 `}}`；
认不出的宽度只丢掉补零、不报错；未知变量名报中文错误。编译后仍是段序列，可以逐段微调。模板只存在于库级覆盖里，不进
`processors/*.yaml`——处理器编辑器继续用段序列 UI（「按模板生成段序列」可从一行模板生成段），这样就不必定义「模板与段序列
同时给出时谁赢」。

**后缀白名单的三层优先级**（`work/app/processors/overrides.py::effective_extensions`）：

```
库级 rule_override.extensions  →  全局设置 settings.rename_extensions  →  处理器 spec.extensions  →  内置默认 [.mkv, .mp4, .ass]
```

全局白名单对所有媒体库生效（设置页「允许重命名的文件后缀」）：留空 `[]` = 未设置，退回每个库自己的处理器清单；填了就要每项
以点开头，保存时统一转小写并去重。处理器 YAML 里的 `extensions` 因此是「没设全局值时的回退值」。

**校验**：`PUT/POST /api/libraries` 先用 `validate_override` 做字段级校验，再把覆盖合并进处理器规格跑一次 `validate_spec`
查**跨层冲突**（例如处理器是 `file_scope: title` 而库级覆盖给了 `accept_dirs`）。任何一条不过都返回 `422`，
`detail` 是 `[{loc, msg, type}]` 列表，逐条中文说明。

---

## 4. 规则引擎语义

输入：`spec`（§3）+ `root`（该媒体库的 `target_dir`）。执行体：`work/app/processors/engine.py`。

```python
run_rename(spec, root, *, log_file=None, folder=None, folders=None,
           force=False, now=None, works_dir="", override=None) -> RenameOutcome
```

`spec` 可传 dict 或 `ProcessorSpec`；`log_file` 是**完整路径**，缺省时按 spec 的 `log_file` + `settings.log_dir` 解析；
`folder` 只处理一个标题文件夹；`folders` 只处理一批（`scope.mode=works`：WebUI 勾选若干作品后「立即重命名」，与 `folder`
同时给出时以它为准）；`force` 忽略「已处理」判据并清掉旧日志条目后重跑；`now` 供测试注入时间；`works_dir` 写进日志条目；
`override` 是库级规则覆盖，在校验前合并进 `spec`。

1. 遍历 `root` 的**直接子目录**，每个是一个**标题文件夹** `T`。
2. `T` 的名字命中任一 `reject_dirs` ⇒ **跳过整棵子树**，记 `warnings`。
3. 计算 `size(T)` = `os.walk` 累加所有文件 `st_size`。
4. 读该处理器的日志：已有 `T` 且 `size` 相同 ⇒ 打印 `Skipping folder: {T} (already processed at {ts})` 并跳过
   （这是唯一的「已处理」判据，除非 `force`）。
5. 取文件作用域：`file_scope: title` ⇒ 作用域 = `[T]`、`season_raw = ""`；`file_scope: subdir` ⇒ 候选是 `T` 的直接子目录，
   依次 ① 命中 `reject_dirs` 跳过 ② `accept_dirs` 非空且不匹配跳过 ③ `skip_season_zero` 且季号为 `"00"` 跳过 ④ 其余为作用域目录。
6. 作用域内文件 = **仅该目录的直接子项**且 `name.endswith(tuple(extensions))`（**大小写敏感**）。`file_scope: subdir` 还要过
   `os.path.isfile`；`file_scope: title` **不过滤**（目录名恰好命中后缀白名单也会进候选）。
7. 选择，**先做 L3 特典剔除**：命中全局设置项 `extras_patterns` 的文件先被剔出改名候选（不参与争名、不顶掉正片的干净名、
   不计入 `episode_unresolved`），留到第 12 步记账。剔除**必须发生在**「每个扩展名只留最大者」之前，否则特典会先抢到名额又被
   剔掉、正片继续等下一轮，而下一轮整目录会因「体积一致」被跳过。判据是对**含扩展名的文件名**做**大小写不敏感**的
   `re.search`，**只作用于作用域目录的直接文件**（嵌套子目录一律不动）；`extras_patterns: []` = 关闭 L3。然后 `per: file` ⇒
   全部；`per: extension` ⇒ 按 `name.split('.')[-1]` 分组，每组只留 `st_size` 最大者。
8. 按书写顺序求值 `naming.expressions`，再按 `naming.segments` 拼出新文件名。
9. **没解析出集数就不改名**：`segments` 引用了集数而该文件未解析出（全不命中且无兜底）⇒ 保留原文件名、不执行 `os.rename`
   （硬链接阶段已用原名链进目标），打印 `Kept (no episode): {相对路径}`，计入 `kept_unresolved_episode` 并在 `warnings`
   汇总一条中文告警；任务仍算成功。
10. **L2 定名 + `os.rename`**：改名之前先用 `_resolve_target_names()` 对整个作用域的候选定最终名——按渲染出的干净名分组，
    组内按体积从大到小（相同则按目录列出先后），体积最大的拿干净名，其余依次加 ` (2)`、` (3)`…（上限 999）。目标路径本来
    就存在时：原有文件比新来的小 ⇒ 原有文件改名**让位**（不删除，计入 `displaced_targets`）而新来的拿干净名，否则新来的
    自己加后缀。**幂等**（候选后缀路径已是本文件自己的 inode ⇒ 照用）；找不到空位 ⇒ 保留原文件名 + 记一条 `error`
    （**绝不覆盖**）。成功打印 `Renamed: {old} -> {new}` 或 `Renamed (suffix): …`，失败计入 `errors`。不经 L2 直接
    `os.rename` 会在 POSIX 上静默覆盖、在 Windows 上报 `WinError 183`，所以 L2 不可关闭。
11. 处理完 `T` 后写日志 `{T: {"size": size(T), "timestamp": now, "works_dir": …}}` 并打印 `Folder processed: {T},
    Size: {n} bytes, Timestamp: {ts}`。**读日志时忽略未知键**（老条目没有 `works_dir` 也要能读）。第 9 步「保留原名」的文件
    也会走到这里，所以下一轮同 size 判据照样生效。
12. **L3 特典排除（不移动）**：第 7 步剔出来的文件原地不动——不改名、不挪位置（目标侧那条硬链接保持原样），也不创建任何目录
    （不存在「作品名/其他/」）。按作用域逐个打印 `Extra (kept): {作用域标签}` 并计入 `extras_classified`
    （`{dir, name, size, pattern}`，`pattern` 是命中的判据，便于把误判的判据写精确）。误判了正片不需要手工搬回：改好判据后
    下一轮它会重新走正常改名流程，因为它从没被移动过。

**输出结构**（`RenameOutcome`，供执行器与 WebUI 使用）：

```
{renamed: [{dir, old, new}], skipped: [{folder, reason, at}], kept_unresolved_episode: [str],
 episode_unresolved: [str], name_collisions: [{dir,target,previous,current,kind}],
 renamed_with_suffix: [{dir,old,clean,target,size,reason,displaced}], displaced_targets: [{dir,old,target,clean,size}],
 extras_classified: [{dir,name,size,pattern}], unknown_params: [{label, file, params}],
 unscoped_works: [{dir, folder, files, samples}], errors: [{dir, file, error}],
 folders_scanned, folders_processed, warnings: [str], log_file}
```

`kept_unresolved_episode` 的元素是相对媒体库根目录的路径字符串；`name_collisions` / `renamed_with_suffix` /
`displaced_targets` 来自 L2/L4，`extras_classified` 来自 L3；`unknown_params` 对应 KI-11（`label` 是作用域标签、`file` 是
源文件名、`params` 是这次渲染里「上下文没有、被留空」的变量名，排序去重），执行器把它们转成 `Task.result` 上的同名计数与
`samples.*` 明细（见 API.md §4.1）。

### 4.1 行为等价性（黄金值）

规则引擎 `work/app/processors/engine.py` 是**唯一执行体**，没有「跑两套实现逐字节比对」的差分测试；行为由
`tests/test_processor_engine.py`、`tests/test_bangumi_episode_rules.py`、`tests/test_rename_extensions.py` 以**固定的黄金值**
守护（文件树、`Renamed:` 行集合、日志 JSON 的键集合与 size）。`default` 相对 `legacy` 的差异只允许出现在 §3.2 标注的四处
**字段级有意增量**上，且逐条单独断言。**L3 特典排除不属于这份差异清单**（它是全局设置、对两个预设同等生效、
`extras_patterns: []` 可整体关闭，也不写在 `LEGACY_OVERRIDES` 里），由 `tests/test_extras_classification.py` 单独覆盖。

---

## 5. 日志

### 5.1 三类日志

| 类别 | 位置 | 形态 |
|---|---|---|
| **硬链接日志** | `/data/logs/hard_links.log` | 纯文本 `%(asctime)s - %(message)s`（`work/app/processors/hardlink.py` 用 `logging` 写，UTF-8）。兼容与审计台账：跳过判据由每库账本提供（§2.1），行格式与写法不变 |
| **每个媒体库一条重命名日志** | `/data/logs/<log_file>` | `json_cache`：`{ "<标题文件夹>": {"size": N, "timestamp": "YYYY-MM-DD HH:MM:SS", "works_dir": "/vol1/1000/Download/qbittorrent/…"} }`，`ensure_ascii=False, indent=4` |
| **任务日志（WebUI 用）** | SQLite `task_logs` + 内存环形缓冲 | 全部处理器共享，任务详情页轮询它（增量拉取见 API.md §4.4） |

### 5.2 硬性要求

- **所有重命名日志必须在同一目录**：`settings.log_dir`（默认 `/data/logs`），**不允许**按媒体库分子目录。
- **每个媒体库各自一条日志**。缺省命名顺序：① 处理器登记的 `log_file`（内置三个分别是 `bangumicache.log` /
  `moviecache.log` / `hardlink_only.log`，自定义处理器是 `<id>cache.log`）；② 按库唯一化：该名已被其它库或其它处理器占用时，
  在扩展名前追加 `-2`、`-3`…（第二个用 `bangumi` 的库拿到 `bangumicache-2.log`）。第一个用某处理器的库保持登记名；磁盘上已有
  同名文件时照样用它并继续追加。处理器连登记名都没有时兜底为 `"{slug}cache.log"`（`slug` 由库名生成：小写、非 `[a-z0-9]`
  折叠为 `-`；纯中文库名退化成通用名时改用处理器 id 当种子）。为什么必须按库唯一：日志记的是「这个标题文件夹已处理过、
  体积多少」，两个库共用一份时，A 库处理过的同名同体积文件夹会让 B 库静默跳过，用户看到的是「B 库跑了但什么都没做」。
- `log_file` 只允许文件名（不得含 `/`、`..`）且必须以 `.log` 结尾；显式传入且与其它库已占用的名字撞车 ⇒ `422`。新建库时日志
  文件不存在则创建空 `{}`，不从任何外部目录播种；已用过的日志文件原样保留、继续追加。

### 5.3 硬链接日志：只写不读，不改写

`hard_links.log` 是兼容与审计台账：应用**从不改写它**，也不把任何外部日志「播种」进数据卷。

- **只接收记账行**（「日志隔离」过滤器）：`ensure_file_logger()` 在这个 FileHandler 上挂了白名单过滤器 `_BookkeepingFilter`
  （前缀常量 `_LOG_BOOKKEEPING_PREFIXES`），只有 `Deleted orphaned hard link folder: …` 与 `Source: …`（后者必须同时含
  ` - Folder: ` 与 ` - Folder size: `）能通过；应用自身的日志与近似不匹配的行一律丢弃。根因是 FileHandler 挂在**根 logger**
  上，不设白名单就会把整个应用的日志灌进这个数据文件。**只过滤、不清理、不轮转**：历史行不删、`_parse_log_line` 的解析契约
  不变；`max_task_log_lines` / `max_history` 只约束任务日志与任务历史。回归用例 `tests/test_file_log_isolation.py`。
- WebUI 的「重命名日志」表由 `GET /api/system/info` 的 `rename_logs` 提供（前端不直接读文件）：逐库一条、顺序与 `libraries`
  一致，字段 `{library_id, library_name, log_file, exists, entries, last_write}`；`exists=false` ⇒ `entries=0` 且
  `last_write=null`；`last_write` 是本地时间 `YYYY-MM-DD HH:MM:SS`。磁盘上已有的 `/data/logs/hard_links.log` 直接继续使用，
  不做任何改写。

> 完整理由与「为什么不再需要 `Source` 前缀本地化」见 [`docs/contracts/BASELINE.md`](BASELINE.md) 的「硬链接日志的写入纪律」；
> 排查步骤见 [`docs/guides/TROUBLESHOOTING.md`](../guides/TROUBLESHOOTING.md)。

---

## 6. 配置文件与双向同步

### 6.1 文件布局（env `CONFIG_DIR`，默认 `/data/config`）

```
/data/config/
├── libraries.yaml            # 媒体库清单（含 processor_id、路径、匹配条件、log_file）
└── processors/
    ├── bangumi.yaml          # 一个处理器一个文件，文件名 = <id>.yaml
    ├── movie.yaml
    ├── hardlink_only.yaml
    └── <custom-id>.yaml
```

- 格式：**YAML 1.1**（`yaml.safe_load` / `yaml.safe_dump`，`allow_unicode=True`、`sort_keys=False`、`default_flow_style=False`），
  UTF-8、无 BOM、以 `schema_version: 2` 开头。需要 `PyYAML==6.0.3`：6.0.2 没有新 Python 版本的预编译 wheel，pip 会回落源码
  构建并失败。选 YAML 而非 JSON 的理由是**用户要手工编辑**（支持注释、正则不必双重转义）。应用**不读** `.env`。

### 6.2 同步语义

- **运行期权威 = SQLite**：DB 是唯一的写入仲裁者，配置文件是它的可移植镜像。
- **写出方向（DB → 文件）**：任何一次通过 API 的成功写入，都在同一请求内把受影响文件**原子写出**（写 `*.yaml.tmp` →
  `os.replace`），并把新 sha256 记进 `config_files`。
- **读入方向（文件 → DB）**：后台线程每 `settings.config_sync_interval_seconds`（默认 **5**）秒比对目录内文件的 mtime +
  sha256；哈希与上次写出/导入时记录的不一致即判定为**外部编辑**，执行导入：
  1. 解析 + `schema_version` 校验 + §3 全量校验；失败 ⇒ **不改 DB**（`imported` 返回 `null`），写一条 `config_events` 错误记录，
     并通过 `GET /api/config` 的 `errors` 暴露给 WebUI 顶部横幅。`errors` 只反映**最近一次成功导入之后**发生的导入错误
     （按 `config_events.id` 判定、上限 20 条，不是只追加的历史日志），重新导入成功后自动清空；
  2. 成功 ⇒ 一个事务里替换 `libraries` / `processors`，写 `config_events(direction='import')`；
  3. 导入完成后**回写**一次文件（规范化格式），使哈希重新对齐，避免抖动。
- **删除语义**：文件里消失的**非内置**处理器 / 媒体库随导入一并从 DB 删除（并写事件）；内置处理器永不删除，只允许重置。
- **冲突窗口**：API 写入与后台导入持同一把 `_config_lock`，不会交错。同步线程并入应用 lifespan；
  `config_sync_interval_seconds` 设为 `0` ⇒ 关闭自动导入（只能手工 reload）。它**不是环境变量**：周期只从 DB 设置表读，
  写进 `.env` / compose 不会生效（区间 0–86400）。

### 6.3 `libraries.yaml` 结构

```yaml
schema_version: 2
libraries:
  - {id: 1, name: 番剧, processor_id: bangumi, enabled: true,
     source_dir: /vol1/1000/Download/qbittorrent/Bangumi,
     target_dir: /vol1/1000/Library/Bangumi, log_file: bangumicache.log}
```

其余字段（`type`、`match_categories`、`match_tags`、`match_path_prefixes`、`orphan_cleanup`、`permission_mode`）与
`GET /api/libraries/{id}` 的响应同名同义，见 API.md §3.1 与 §3.2。`processor_id` 必须存在于 `processors/`。

---

## 7. HTTP API：处理器与配置同步端点

共 **10 条**：`/api/processors` 的 `GET`（列表）/ `GET {id}` / `POST` / `PUT {id}` / `DELETE {id}` / `POST {id}/reset` /
`POST validate`（不落库），以及 `/api/config` 的 `GET` / `POST reload` / `POST export`。**路径、请求体与响应形状一律以
[`docs/contracts/API.md`](API.md) 的 §5.7「处理器接口」与 §5.8「配置同步接口」为准**，此处只记语义约束：

- `POST /api/processors` 新建自定义处理器（`builtin=false`，`schema_version` 由服务端写）；`PUT` 全量替换 spec 且 `id` 不可改，
  内置处理器允许改规则（写事件）；`DELETE` 内置 ⇒ 409，被任何媒体库引用 ⇒ 409 并给出引用清单。
- `POST /api/processors/{id}/reset` 的 `preset` 取 `default`（推荐默认）或 `legacy`（最朴素的旧口径，见 §3.2）；
  `validate` 只做校验，返回 `{ok, errors:[{field,message}]}`。
- `POST /api/libraries` / `PUT /api/libraries/{id}`：`type` 与 `processor` 的枚举约束已取消，改为「`processor_id` 必须存在」；
  `log_file` 缺省时按 §5.2 自动生成。两者另接受 `rule_override`（见 §3.3）：`POST` 省略或传 `null` ⇒ 不设覆盖、`{}` ⇒ 清空覆盖；
  `PUT` **省略该键 = 保留现值**（要删覆盖就传 `{}`）。`GET /api/libraries[/{id}]` 的每条库都带已归一化的 `rule_override`
  （无覆盖时为 `{}`）。
- `PUT /api/settings` 的 `rename_extensions`（字符串数组，允许 `[]` = 未设置，每项以点开头）与 `extras_patterns`（正则数组，
  `[]` = 关闭 L3；保存时逐条 `re.compile`，有一条编不过就整体 `400` 并点名第几条与模式原文）。

---

## 8. 前端契约增量

逐页行为与组件细节由 [`docs/contracts/ARCHITECTURE.md`](ARCHITECTURE.md) 的「前端契约」定义；本节只记与处理器/规则直接相关、
改实现时不能丢的约束：

- 新增 `#/processors`（列表）与 `#/processors/{id}`（规则编辑器）并加入侧边导航；媒体库表单的「处理器」下拉改为拉
  `GET /api/processors`（不再写死三个选项），并新增 `log_file` 输入。规则编辑器四大块与 §3.1–§3.3 对应：**命名方式**（可视化
  段列表 + 实时预览，另有命名表达式区与 calibre 风格模板框 + 12 个变量按钮 + 「按模板生成段序列」）、**接受目录正则**
  （`{pattern, season_group}` 行，显示能否编译）、**拒绝目录正则**、**接受的后缀名**（chips，校验以 `.` 开头）。
- 媒体库表单新增「重命名规则（本库）」卡片（`rule_override`，见 §3.3）：开关（关 = 完全跟随处理器，保存时提交 `{}`）+ 模板
  输入框 + 变量按钮 + 实时预览 + 接受/拒绝目录行 + 本库后缀白名单；预览与错误提示都在这张卡片内。
- 设置页新增「允许重命名的文件后缀」（`rename_extensions`）与「特典文件排除」（`extras_patterns`，一行一条正则、不按空格切）
  两张卡片；后者写的是**判据**不是后缀名，命中者不改名也不移动，任务结果里计数是 `extras_classified`。
- 顶部固定「保存」与「恢复默认」（`POST …/reset`，二次确认，弹窗内可选 `default` / `legacy` 并说明差异）；全局顶部另有
  「配置文件同步」状态条：`in_sync === false` 时列出文件名，`errors` 非空时显示红色横幅、错误详情与「重新从磁盘导入」按钮，
  另给「立即写出配置」。不引入 CDN / 构建工具链：原生 ES modules + 手写 CSS。

---

## 9. 容器化与配置增量

- 新增 env 只有 `CONFIG_DIR`（默认 `/data/config`）。同步周期**不是** env——它是 DB 设置项 `config_sync_interval_seconds`
  （初值见 `work/app/db.py` 的 `DEFAULT_SETTINGS`，区间 0–86400，`0` = 关闭自动导入）。
- `/data/config` 位于 `/data` 卷内，**无需额外挂载**；Dockerfile 的 `COPY app/` 只带入应用自己的代码，pip 依赖含
  `PyYAML==6.0.3`；`docker/entrypoint.sh` 启动自检会检查 `/data/config` 可写等条件。
- `HEALTHCHECK` 共**六项**：`db`、`data_dir`、`downloads_readable`、`library_writable`、`processors`（三个内置处理器已加载）、
  `config`。调用方式与判读见 [`docs/guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md) 的「容器显示 unhealthy 时怎么定位」一节。

---

## 10. 数据模型迁移

存量数据卷升级时由 `init_db()` **幂等**补齐，无需手工操作：

- 新表：`processors(id TEXT PK, name TEXT, kind TEXT, builtin INT, spec_json TEXT, created_at, updated_at)`、
  `config_files(path TEXT PK, sha256 TEXT, at TEXT)`、`config_events(id INTEGER PK, path TEXT, direction TEXT, sha256 TEXT,
  at TEXT, note TEXT)`。`libraries` 新列：`processor_id TEXT`、`log_file TEXT`、`rule_override TEXT`（可空 JSON，见 §3.3）——
  用 `PRAGMA table_info` 检查缺列再 `ALTER TABLE`，老行保持 `NULL`。
- 首次启动：`processors` 为空 ⇒ 写入三个内置默认；旧库里 `libraries.processor` 的取值（`bangumi`/`movie`/`hardlink_only`）
  直接映射到同 `id`；同时把 `libraries.yaml` 与 `processors/*.yaml` 写出来（等价于一次 export）。`/data/logs/*.log` 原样继续使用。
- 版本兼容与升级步骤见 [`docs/contracts/COMPATIBILITY.md`](COMPATIBILITY.md) 的「版本兼容性」与
  [`docs/guides/DEPLOYMENT.md`](../guides/DEPLOYMENT.md) 的「升级与回滚」。
