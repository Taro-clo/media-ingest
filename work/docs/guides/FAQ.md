# 常见问题 FAQ

> **这篇解决什么问题**：**没有具体报错、只有疑问**的那些问题 —— 会不会动我的文件、占不占双倍空间、能不能跨盘、为什么权限是这样、哪些事做了不可逆。
> 每条结构固定：**一句话答案 → 边界与前因后果 → 去哪查细节**。有具体报错请直接去 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)。
>
> 相关：[`USER_GUIDE.md`](USER_GUIDE.md)（网页怎么用）、[`DEPLOYMENT.md`](DEPLOYMENT.md)（安装/升级/备份/命令）、[`BASELINE.md`](../contracts/BASELINE.md) 与 [`COMPATIBILITY.md`](../contracts/COMPATIBILITY.md)（冻结行为与契约）。

**命令标注**：【宿主机】= SSH 登录宿主机执行；【容器内】= `docker exec -it media-ingest <命令>`。
后文的 `<HOST_IP>`、`/vol1/1000`、`Show A` 都是**示例值**。要调接口时先登录一次并存下 cookie，后面的 `-b cookies.txt` 都复用它：

```bash
curl -s -c cookies.txt -X POST http://127.0.0.1:18081/api/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"<管理员名>","password":"<密码>"}'
```

---

## 目录

> **编号是刻意保留原号的**（其它文档里的「FAQ 第 N 问」引用因此仍然指向同一主题）；号码不连续，是因为大部分问题已按主题迁走 —— 迁到哪一篇见文末[「迁走的问题去哪了」](#迁走的问题去哪了)。

| # | 问题 | 一句话 |
|---|---|---|
| [1](#1-为什么不复制文件非要建硬链接) | 为什么不复制文件，非要建硬链接？ | 复制会让空间翻倍，并破坏"两边永远是同一份数据" |
| [2](#2-硬链接会占双倍空间吗) | 硬链接会占双倍空间吗？ | 不会，盘上只存一份 |
| [3](#3-能不能跨硬盘跨文件系统建硬链接) | 能不能跨硬盘建硬链接？ | 不能，且要求"同一个挂载"，不只是同一个文件系统 |
| [4](#4-会不会动我的下载文件) | 会不会动我的下载文件？ | 不会；但保护从"物理只读"降级为"可验证" |
| [5](#5-能不能删掉原始下载) | 能不能删掉原始下载？ | 能，但孤儿清理开着会把媒体库那份一起删 |
| [6](#6-qbittorrent-不开着还能工作吗) | qBittorrent 不开着还能工作吗？ | 能，只有"自动触发"和 API 补偿需要它 |
| [13](#13-能不能同时处理多个库) | 能不能同时处理多个库？ | 库不限，但队列串行，一次一个任务 |
| [16](#16-能不能加自己的正则--命名规则) | 能不能加自己的正则/命名规则？ | 能，规则就是配置；可执行表达式是代价 |
| [21](#21-hook-token-泄露了怎么办) | hook token 泄露了怎么办？ | 设置页重新生成，旧 token 立刻失效 |
| [27](#27-为什么媒体库里的文件权限和下载目录里的一样) | 为什么媒体库文件权限和下载一样？ | 同一个 inode，权限天然只有一份 |
| [37](#37-为什么有的文件没被改名而是停在原地) | 为什么有的文件没被改名，停在原地？ | 命中了「特典文件排除」，保留原名不移动 |
| [39](#39-删除作品记录会删掉我的下载文件吗) | 删除作品记录会删掉下载文件吗？ | 不会，只删目标侧 |
| [40](#40-两个媒体库用同一个下载目录会互相影响吗) | 两个库用同一个下载目录会互相影响吗？ | 不会，判据带库 id |

---

## 1. 为什么不复制文件，非要建硬链接？

**一句话答案**：硬链接让同一份数据在盘上**只存一份**、同时出现在两个目录里；复制会让空间翻倍，还会让"这两份永远是同一份数据"这个前提悄悄失效。

| | 硬链接（本项目） | 复制 |
|---|---|---|
| 硬盘占用 | **一份**（多的是目录里的名字） | **两份** |
| 建链速度 | 元数据操作，毫秒级 | 真的要搬几十 GB |
| 删掉下载原文件后 | 媒体库那份照常播放（直到最后一个名字被删） | 独立副本，也能播 |
| 能否验证"是同一个文件" | 能，`ls -i` 的 inode 号相同 | 不能 |
| 改内容的相互影响 | 改一个等于改两个 | 互不影响 |

媒体库动辄几十 TB，复制会让存储需求**直接翻倍** —— 这是硬链接成为唯一合理实现的理由。

**反过来**：遇到建不了硬链接的情况，应用**宁可让任务失败并报错，也绝不偷偷改成复制**。因为静默复制会让用户以为没占空间、实际占了两份，而且"删了源还能播"的前提会悄悄失效。

> 契约依据：[`BASELINE.md`](../contracts/BASELINE.md) §1；症状与修法见 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 1。

## 2. 硬链接会占双倍空间吗？

**一句话答案**：不会。整个文件系统里那份数据只存**一份**。

硬链接是在目录里加一个**新名字**指向**同一个 inode**。**【宿主机】** 自己验：

```bash
ls -i /vol1/1000/Download/qbittorrent/Bangumi/"Show A"/Season\ 1/*.mkv
ls -i /vol1/1000/Library/Bangumi/"Show A"/Season\ 1/*.mkv
stat -c 'links=%h %n' /vol1/1000/Library/Bangumi/"Show A"/Season\ 1/*.mkv
df -h /vol1/1000
```

两侧 **inode 号相同**、`links >= 2`；而 `df` 的已用空间在建链前后**没有明显变化**（`du -sh` 两边看起来一样大，那是"文件夹内容有多大"，不是"盘上多占了多少"）。

> ⚠️ **反面必须知道**：既然是同一份数据，**原地改内容会同时影响两边**（在媒体库里"保存"某个文件，下载目录那份的内容也变了）。
> 结论：**只改名、只删除是安全的；不要原地编辑内容。**

## 3. 能不能跨硬盘（跨文件系统）建硬链接？

**一句话答案**：不能 —— 而且比"跨文件系统"更严：**必须同一个挂载**。这是内核限制，`os.link` 返回 `EXDEV`（`Invalid cross-device link`），本项目不提供绕过的办法（唯一绕法是复制，那就不叫硬链接了）。

**为什么**：inode 号只在单个文件系统内唯一；更关键的是内核在真正建链接前还要比较源与目标的**挂载身份** —— 不是同一个挂载就**一律 EXDEV**，跟 `st_dev`、btrfs 子卷、ZFS dataset 都没有关系。所以"同一块盘"不等于"同一个挂载"。

**你会看到**（建库 / 改库被拒时的报错；预检与任务告警里措辞可能不同，但同样点明"不是同一个挂载"）：

```
跨挂载错误：源 /vol1/1000/Download/qbittorrent/Bangumi（挂载点 /vol1/1000，mount ID 26）与目标根
/vol1/1000/Library/Bangumi（挂载点 /vol1/1000，mount ID 27）不是同一个挂载，os.link 会返回 EXDEV。
本应用不会回退为复制文件。请把两个目录放到同一个宿主挂载下。
```

（读不到挂载信息时会退到 `st_dev` 判据，文案开头是「跨文件系统错误：…」。）

**建库 / 改库时这条判定直接拦下**（`400`，库存不进去）；对**已存在的库**跑任务时只告警、不硬拦 —— 真跑起来由逐文件结果给出结论，失败就是 `link_failed`，不会悄悄复制一份冒充硬链接。

**怎么判断**（**【宿主机】**）：

```bash
findmnt -T /vol1/1000/Download/qbittorrent -o TARGET
findmnt -T /vol1/1000/Library              -o TARGET
```

两条 `TARGET`（挂载点）**相同**才行。**别用 `stat -c '%d'` 当依据**：`st_dev` 相同**不能**说明能建硬链接（两条独立 bind 就曾 `st_dev` 相同却分属两个挂载）。

**怎么办**：把下载根与媒体库根放进**同一条 bind**（`MEDIA_ROOT` 取两者的共同父目录），别拆成两条。挂载形态与各类宿主机的结论见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §2.1、§3.3 与 [`COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) §2；症状见 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 1。

> 真要跨盘只有"复制"一条路，本应用刻意不做。你可以自己 `rsync` 一份副本，但要清楚那已经不是硬链接方案（空间翻倍、删源后两份互不相干）。

## 4. 会不会动我的下载文件？

**一句话答案**：不会。但**请诚实理解**：把源目录单独挂 `:ro`、由内核保证写不进去 —— 这条**结构性保证**与本方案的单一共同父挂载**互斥**，它被换成了下面这套**可验证的保证**。**这是降级，不是等价替代。**

三层保证：

1. **代码层**：应用没有写源目录的代码路径，回归测试 `tests/test_no_source_mutation.py` 钉住这一点。
2. **执行体**：建链阶段对源目录只做 `listdir` / `walk` / 读大小 / `os.link(源 → 目标)` —— `os.link` 不修改源文件，只是在目标目录加一个名字。
3. **重命名只发生在目标**：引擎原地改名的是媒体库里那份的名字，不动下载目录里的名字。

**再加一步"可验证"**：任务**开始前**对源目录逐文件快照（相对路径、大小、inode、mtime），**结束后**再核对；有差异就写进 `result.source_integrity = {"ok": false, "changed": [...]}` 并在 `warnings` 里给出中文告警、列出被动过的路径。（硬链接本身会让源文件 `nlink`+1、`ctime` 变化，这两项**不算**差异，不会误报。）

> 结论：现在能**发现**源被动过并告警，但**不能阻止**它。这是单挂载形态的已知代价 —— 为什么互斥见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §3.1，怎么读这条核对结论见 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 3。

**唯一的理论例外**（硬链接的固有性质，不是应用的行为）：原地**编辑内容**（不是改名）会同时改掉下载目录那份。只改名、只删除是安全的。

## 5. 能不能删掉原始下载？

**一句话答案**：可以，删掉下载那份**不会**让媒体库那份失效（数据还在）。但**有个大坑**：`orphan_cleanup` 开着时，下一次跑任务会认定"源已经不在了" → **把媒体库那份也递归删掉**。

删掉一个名字，inode 的链接计数从 2 变 1，数据不消失：

```bash
rm -rf /vol1/1000/Download/qbittorrent/Bangumi/"Show A"    # 媒体库那份仍可正常播放
```

**要"删源保库"，先关孤儿清理**，它有**两级**、是"与"的关系（两个都为真才真的删）：

- 单库：编辑该媒体库 → 关掉「孤儿清理」（`orphan_cleanup`）；
- 全局：设置页把 `orphan_cleanup_enabled` 设为 `false`（总开关）。

**【宿主机】** 也可以用接口改全局开关：

```bash
curl -s -b cookies.txt -X PUT http://127.0.0.1:18081/api/settings \
  -H 'Content-Type: application/json' -d '{"orphan_cleanup_enabled": false}'
```

> ⚠️ 这是本项目里最容易造成"我的媒体库文件不见了"的设置组合。打算清理下载盘就**先关**孤儿清理。关掉后，源已被删的旧记录仍在账本里，每轮只是 `stat` 一次，没有别的副作用。

**还有一件事**：删掉原始下载**不会立刻释放空间** —— 媒体库那份还指着同一个 inode，要等**最后一个名字**也被删掉。

> 两个开关的页面位置与默认值见 [`USER_GUIDE.md`](USER_GUIDE.md) §8.5；删了什么、怎么恢复见 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 6。

## 6. qBittorrent 不开着还能工作吗？

**一句话答案**：能。网页操作、手动跑任务、扫描补偿都不需要 qBittorrent 活着；只有"下载完成自动触发"和"qBittorrent API 补偿"需要它在跑。

| 功能 | 需要 qBittorrent 在跑吗 |
|---|---|
| 网页新建/编辑媒体库 | 不需要 |
| 手动跑任务（全量 / 指定文件夹） | 不需要 |
| 扫描补偿（`scan_enabled=true` 时按间隔扫描源目录） | 不需要（它只读源目录里的文件夹） |
| 「下载完成自动触发」回调 | **需要**（是 qBittorrent 去执行那条 curl） |
| `qbittorrent_url` 的 API 补偿 | **需要** |

也就是说：qBittorrent 停了，你手动跑一次照样能把已下载的整理好。这也是扫描补偿存在的意义 —— 回调漏了（容器重启、curl 超时）还能补上。

> ⚠️ 扫描补偿**默认开启**（`scan_enabled=true`，每 900 秒扫一次）；可在设置页关掉或调整周期，见 [`USER_GUIDE.md`](USER_GUIDE.md) §8.4。已有部署升级上来时保留原来的开关值，不会被强制打开。

## 13. 能不能同时处理多个库？

**一句话答案**：能建任意多个库，但**同一时刻只跑一个任务** —— 队列是串行的。

- **库数量不限**：每个库有自己的源目录、目标目录、匹配条件、处理器。
- **一次回调可以命中多个库**：同时满足多个库条件的种子会**全部入队**（响应里 `tasks` 列出新建任务，`ignored` 列出没命中的库）。
- **队列串行**是刻意的：建链与重命名都会"读-改-写"账本/日志，并发调用会互相覆盖（[`BASELINE.md`](../contracts/BASELINE.md) §4 的 B9 要求单工作线程 + 锁）。
- **串行不会很慢**：硬链接是元数据操作，主要耗时在算文件夹体积。想更快请**缩小运行范围**（按单个作品/文件夹跑），不要试图加并发 —— 加 `--workers` 会破坏串行保证。

> 想只处理某一部剧、某个文件夹：见 [`USER_GUIDE.md`](USER_GUIDE.md) §6.2；任务为什么排在队里见 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 14。

## 16. 能不能加自己的正则 / 命名规则？

**一句话答案**：**能 —— 这个项目就是为这件事做的。** 重命名规则是**配置**（不是代码）：在网页「处理器」页改，或直接改 `/data/config/processors/<id>.yaml`。

| 想改什么 | 字段 |
|---|---|
| 新文件名怎么拼（段与顺序） | `naming.segments`（`{param}` / `{literal}` / `pad`） |
| 更复杂的拼接、条件、正则提取 | `naming.expressions`（Python 表达式，见下方 ⚠️） |
| 哪些子目录算"季目录"、季号从哪来 | `traverse.accept_dirs`（`{pattern, season_group}`） |
| 哪些目录整棵跳过（特典、CD、扫图…） | `traverse.reject_dirs` |
| 接受哪些后缀 | `extensions`（**大小写敏感**） |
| 集数怎么从文件名认出来 | `episode_patterns` + `episode_fallback` |
| 同后缀取哪个文件 | `selection.per`（`file` / `extension`） |
| 该库日志叫什么 | `log_file`（只能填文件名） |

**不能改的**：硬链接的**路径规则与目录层级**（`目标/<一级文件夹>/<相对源的完整子路径>/<同名文件>`，逐字节保留），它是硬链接阶段的契约，也没有开关 —— 层级一变 Emby 就认不出剧集结构（[`BASELINE.md`](../contracts/BASELINE.md) §1）。

**改完怎么生效**：网页保存（同一请求就把文件写好），或直接改文件后**等自动导入**（周期是 DB 设置项 `config_sync_interval_seconds`，默认 5 秒；**它不是环境变量**，写进 `.env` 不生效）。验证：编辑器里的**实时预览** → 「预检」→ 手动跑一个**小文件夹**，看任务日志里的 `Renamed:` 行。

> ⚠️ **`naming.expressions` 里可以写 Python 表达式**（可用 `re`、`os`、`len`、`str`、`int`、`sorted`、`min`、`max` 与 `zfill`/`splitext`/`basename`/`ass_suffix`）。这是"可扩展、不写死"的代价：**能改配置文件的人就能在容器里执行代码**。不需要就留空。
> ⚠️ 改了处理器，**所有用它的库都跟着变**。想只影响新库，就复制一份处理器再改。

> 字段全表与默认值：[`PROCESSORS.md`](../contracts/PROCESSORS.md)；页面操作见 [`USER_GUIDE.md`](USER_GUIDE.md) §5；文件与 DB 的双向同步规则见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §2.6。

## 21. hook token 泄露了怎么办？

**一句话答案**：去设置页**重新生成**一个，然后把 qBittorrent 里的回调命令换成新的。

**token 是什么**：`/api/hooks/*` 的钥匙。拿到它的人能**触发任务**，但读不到你的数据、删不了文件（那些接口要登录 cookie）。

1. **【浏览器】** 设置页 → 回调 / Hook → **重新生成 token**（旧的**立即失效**，且无法找回）；
2. 复制页面上那条**完整的回调命令**；
3. qBittorrent WebUI → 设置 → 下载 → **运行外部程序** → 粘贴替换；
4. 验证（**【宿主机】**，用新 token）：

   ```bash
   curl -s -X POST "http://<HOST_IP>:18081/api/hooks/qbittorrent?token=<新TOKEN>&name=Test&hash=abcdef1234567890"
   ```

   返回里含 `matched` / `tasks` = 新 token 生效；`401` + `回调 token 不正确…` = 没生效（多半是多余的空格/引号，或 URL 里的 `&` 被 shell 截断）；`405` = 漏了 `-X POST`（回调路由只接受 POST）。

> ⚠️ **回调参数只从 URL 查询串或 urlencoded 表单体读取**。把命令改成 `-H 'Content-Type: application/json' -d '{...}'` 会**静默失效**：HTTP 200、但 `matched` 恒为 0，不报任何错。
> token 存在数据库设置里（`/data/mlm.db`），**备份 `data/` 时备份里也含 token**。

> 回调怎么配、怎么验见 [`USER_GUIDE.md`](USER_GUIDE.md) §9.3 与 [`DEPLOYMENT.md`](DEPLOYMENT.md) §5；返回 401 的排查见 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 9。

## 27. 为什么媒体库里的文件权限和下载目录里的一样？

**一句话答案**：因为它们本来就是**同一个文件**（同一个 inode）—— 硬链接不复制内容，也不复制权限；**权限是 inode 的属性，天然只有一份**。

- 你在媒体库里看到的属主/权限位，和下载目录里的**是同一份**，改哪边都一样。
- 所以"媒体库权限不对"的根因一定在**源文件本身**，不是"应用没给媒体库设权限"。
- 应用**默认不** `chmod` 媒体库：那会连带改掉源文件的权限位。只有显式开启 `permission_mode` 才会（且会连带改源，见 [`USER_GUIDE.md`](USER_GUIDE.md) §8.9）。
- 想让 Emby 等其它账号读得到，正确做法是保证**源文件/源目录本身**对相应账号可读，而不是递归 `chmod` 媒体库整棵树（那会真的改到下载原文件）。

**【宿主机】** 自查是不是同一个 inode（同一文件在两侧各 `stat` 一次）：

```bash
stat -c 'inode=%i  links=%h  %n' "/vol1/1000/Download/qbittorrent/Bangumi/Show A/Season 1/Show A S01E01.mkv" \
                                 "/vol1/1000/Library/Bangumi/Show A/Season 1/Show A S01E01.mkv"
```

两行 `inode` 相同、`links >= 2` ⇒ 它们就是同一份数据。

> 属主/`PUID`/`PGID` 怎么设、权限报错怎么对号入座：[`DEPLOYMENT.md`](DEPLOYMENT.md) §10.4；报错排查见 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 7。

## 37. 为什么有的文件没被改名，而是停在原地？

**一句话答案**：它命中了设置页「**特典文件排除**」里的正则 —— 软件认为它是 `NCOP` / `Preview` 这类特典，于是**排除在改名之外**：保留原名、留在原位置（**不改名也不移动**）。

- 出厂默认 7 条：`NCOP`、`NCED`、`Preview`、`Creditless`、`Info`、`予告編`、`CM集`；对**含扩展名的文件名**做**大小写不敏感**的正则搜索，只作用于当前作用域目录的**直接文件**。
- **没有丢东西、也没有多出来东西**：命中文件停在原处（硬链接照旧），**不会**被搬进 `<作品目录>/其他/`，也**不会**创建 `其他/` 目录。
- **怎么关**：把设置页那张卡片**清空**（保存成空数组）⇒ 功能整体关闭，一切按正片处理。
- **怎么改**：一行一条正则；误判正片时写精确（`^NCOP$` 而不是裸 `NCOP`）；**非法正则会当场报错**（点明第几条、哪个模式编译失败），不会静默吞掉。
- 结果里看 `result.extras_classified` 与 `samples.extras_classified`（每条 `{dir, name, size, pattern}`，`pattern` 是命中的那条判据，便于反查误判）。
- ⚠️ **对所有库、所有处理器生效**，`legacy` 预设也不例外。

> 设置项位置与逐条说明见 [`USER_GUIDE.md`](USER_GUIDE.md) §8.7.1；"文件没被改名"的完整排查（含季目录、后缀、集数规则）见 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 25 与场景 15。

## 39. 删除作品记录会删掉我的下载文件吗？

**一句话答案**：**不会。** 删除只动**目标目录**（媒体库侧），源下载目录**一个字节都不动**。

- 硬链接只是指向同一份数据的另一条目录项；删掉目标那条，源目录那个名字还指着同一份数据。
- 删除会动**三处**：① 账本 `link_records` 里这些作品的行；② 本库重命名缓存（日志文件）里的同名条目 —— **不清它，下次运行会打印 `Skipping folder: … (already processed at …)`，看起来像"删了没生效"**；③ 目标目录里的**整个作品文件夹**。
- ⚠️ 三件事必须知道：① 目标里那个作品文件夹**整棵被删、不可撤销**；② 目标里"**不是本应用建立的**"文件（`st_nlink < 2`，通常是你自己放进去的）会被**一并删掉** —— 确认框会在数量大于 0 时显著警告；③ 删除是**先预演（`dry_run`，磁盘一个字节不动）再确认执行**，一步弹窗。

> 操作步骤与确认框文案见 [`USER_GUIDE.md`](USER_GUIDE.md) §6.4；删了之后目标里多出 ` (2)` 后缀是怎么回事见 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 29。

## 40. 两个媒体库用同一个下载目录，会互相影响吗？

**一句话答案**：**不会互相影响。** 判据是「**本库** + 同 `source_dir` + 同 `target_dir` + 同体积」，每个库各有一本账（`link_records` 的唯一键是 `(library_id, folder)`）。

- 两个库同源、各自不同目标 ⇒ **各建各的**，互不干扰；同一个库换了目标目录 ⇒ 旧记录对不上 ⇒ 该库**重跑一遍**。
- ⚠️ 如果账本是**空的**（换了数据卷、清了 `data/`）⇒ 第一次**全量**运行会把每个作品**重新建链一次**；目标里已改好名的文件会以**原名**再链一份（同一 inode 的第二个目录项），重命名阶段于是多出 `xxx (2).mkv`。**不丢数据**，只是多出重复名字。
- 账本是**作品级**的、**不保证文件级完整性**：你在目标目录里的手动增删改一律保留，不会被"顺手修复"；「作品记录」里两本账的差集（`rename_missing` / `rename_only`）**只做提示**。

> 作品记录怎么读、两本账对不上怎么办见 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 27；账本语义另见 [`USER_GUIDE.md`](USER_GUIDE.md) §6.5。

---

## 迁走的问题去哪了

原 FAQ 的 42 问里，凡属"具体报错怎么修"「某个按钮怎么点」「某个字段填什么」的，都已按主题并入权威文档。按原编号查：

| 原编号 | 现在看哪 |
|---|---|
| 7 为什么显示"跳过"、10 第二次跑什么都没做、23 第一次会不会全部重做 | [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 4、场景 5、场景 28 |
| 8 为什么文件没被重命名、9 支持哪些命名格式 | [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 15；规则全表 [`PROCESSORS.md`](../contracts/PROCESSORS.md) §3.2；段名速查 [`USER_GUIDE.md`](USER_GUIDE.md) §5.5 |
| 11 容器重启后任务会继续吗 | [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 13 |
| 12 如何只处理某一部剧、32 新加一个库要写代码吗 | [`USER_GUIDE.md`](USER_GUIDE.md) §6.2、§5.9 |
| 14 日志在哪、25 应用日志与账本日志怎么分开、33 每库日志与 `xxxcache.log` | [`DEPLOYMENT.md`](DEPLOYMENT.md) §2.6；[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 24 |
| 15 忘记密码 | [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 11 |
| 17 删除媒体库规则会删我的文件吗 | [`USER_GUIDE.md`](USER_GUIDE.md) §10 |
| 18 `orphan_cleanup` 默认安全吗、19 `permission_mode` 开了会怎样 | [`USER_GUIDE.md`](USER_GUIDE.md) §8.5、§8.9；[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 6、场景 7 |
| 20 强制重跑会有什么后果、38 想重做某个作品 | **整库 `force` 已停用**（返回 400）；重做走 [`USER_GUIDE.md`](USER_GUIDE.md) §6.4 与 [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 16、场景 28，副作用见 KI-9 |
| 22 应用读写宿主上哪些目录、28 需要装 Python 吗 | [`DEPLOYMENT.md`](DEPLOYMENT.md) §1、§2.1（镜像自带运行环境） |
| 24 换了下载/媒体库路径、29 改了 yaml 网页会变吗、30 网页改了文件会变吗 | [`DEPLOYMENT.md`](DEPLOYMENT.md) §6.5、§2.6；[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 22 |
| 26 回调 401 / token 不对 | [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 8、场景 9 |
| 31 只想要最朴素的旧口径 | [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 20；[`PROCESSORS.md`](../contracts/PROCESSORS.md) §3 |
| 34 为什么必须同一个挂载、35 拆两条 bind 会怎样、36 `same_filesystem` 是 `null` | [`COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) §2；[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 1、场景 21 |
| 41 作品记录里怎么快速找到某个作品、42 删了文件为什么没补回来 | 检索口径见 [`KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-8；[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 27、场景 28 |

---

## 下一步看哪篇

- 网页上每个按钮怎么用、三种库怎么建 → [`USER_GUIDE.md`](USER_GUIDE.md)
- 怎么装、卷怎么挂、怎么升级/备份/改端口 → [`DEPLOYMENT.md`](DEPLOYMENT.md)
- 接口怎么调、字段什么意思 → [`API.md`](../contracts/API.md)
- 有具体报错要查 → [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)
- 哪条行为"为什么是这样"、哪些不能改 → [`BASELINE.md`](../contracts/BASELINE.md)、[`COMPATIBILITY.md`](../contracts/COMPATIBILITY.md)
