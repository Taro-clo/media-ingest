# 故障排查（TROUBLESHOOTING）

> 这篇回答「**页面上出现了某个具体报错，我该怎么办**」。
> 想知道「为什么是这个行为、哪些不能改」→ [`BASELINE.md`](../contracts/BASELINE.md)、[`COMPATIBILITY.md`](../contracts/COMPATIBILITY.md)；
> 网页怎么用 → [`USER_GUIDE.md`](USER_GUIDE.md)；怎么装 / 升级 / 备份 / 改端口 → [`DEPLOYMENT.md`](DEPLOYMENT.md)；
> 「概念性疑问」（会不会占双倍空间、能不能删掉原始下载…）→ [`FAQ.md`](FAQ.md)。

**标注约定**：每条诊断命令都标了执行位置。

| 标注 | 含义 |
|---|---|
| **【NAS 宿主】** | 在 NAS 的 SSH 里、项目目录（示例 `/vol1/1000/Docker/media-ingest`）下执行 |
| **【容器内】** | 在容器里执行：`docker exec -it media-ingest <命令>` |
| **【浏览器】** | 打开 `http://<NAS_IP>:18081`，在页面上操作 |

后文默认：容器名/服务名 = `media-ingest`，端口 = `18081`，数据卷 = 部署目录下的 `./data`（容器内 `/data`，下称 `<data>`；可用 `MLM_DATA_DIR` 改）。`<NAS_IP>`、`/vol1/1000`、`Show A` 都是**示例值**，换成你自己的。

**要调接口时**先登录一次、把会话 cookie 存成文件，后面所有 `-b cookies.txt` 都复用它（**【NAS 宿主】**，用户名/密码换成自己的）：

```bash
curl -s -c cookies.txt -X POST http://127.0.0.1:18081/api/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"<管理员名>","password":"<密码>"}'
```

---

## 0. 五分钟通用诊断（任何问题都先做这三步）

**第一步：看系统信息页。**

**【浏览器】** 「系统信息」页（或 `GET /api/system/info`）。重点看三块：

| 看什么 | 正常应该是 | 不对时看 |
|---|---|---|
| `mounts[]` | 一条 `MEDIA_ROOT`（**容器路径 = 宿主路径**、可写）+ `/data` + `/data/config`；**没有**单独的 `/downloads` / `/library`。条目数**随媒体库变化**：固定项之外还有每个库的 `source_dir` / `target_dir` 的**最近已存在祖先**，别按"恰好几条"核对 | 场景 1、21、22 |
| `same_filesystem`（三态） | `true` = **已实证**能在容器内建硬链接；`false` = 跨挂载，必然失败；`null` = **无法实证，不等于通过** | 场景 1、21 |
| `processors` / `config` | `processors.total` 为 3、`builtin_ids` 为 `["hardlink_only","bangumi","movie"]`；`config.files[].in_sync` 全为 `true` | 场景 17、18 |

**第二步：看健康检查。**

**【NAS 宿主】**

```bash
curl -s http://127.0.0.1:18081/api/system/health | python3 -m json.tool
```

`checks` 固定**四项**：`db` / `data_dir` / `config` / `processors`。哪一项是 `false`，`details` 里就有对应的中文原因，直接对号入座（见[场景 2](#2-启动即-degraded健康检查四项里有-false)）。

> ⚠️ 源/目标目录是否**同一挂载**不再由健康检查判（旧版的 `downloads_readable` / `library_writable` 随「允许的根目录」白名单一起删除）：
> 建库/改库时校验，跨挂载直接 `400`；运行期问题看任务 `errors[]` / `link_failed` 与 `/api/system/info` 的 `same_filesystem`。
> 升级后媒体库路径仍是旧容器路径的排查见[场景 22](#22-升级到单挂载形态后容器-unhealthy旧的允许根目录还在)。

> ⚠️ **`status` 为 `degraded` 有两种来源**：① 四项里有 `false`；② 四项全 `true`，但 `warnings` 非空 —— 目前唯一的 warning 来源是**容器内文件系统编码不是 UTF-8**（消息里会点名 `LANG` / `PYTHONUTF8` / `PYTHONIOENCODING`），此时中文文件夹名会在读取日志时抛 `UnicodeDecodeError`。健康判据只有一条：`status == "ok"`。

**第三步：看任务的原始输出。**

**【浏览器】** 「任务」→ 点开失败的任务 →「日志」。应用日志与建链阶段的 stdout 都在这里（`stream` 为 `stdout` 的是原生实现 `app/processors/hardlink.py` 自己打印的，最接近真相）。

**【NAS 宿主】** 直接看文件：

```bash
tail -n 80 <data>/logs/hard_links.log
docker compose logs --tail=100 media-ingest
```

> ⚠️ **先看 `result` 里的计数，再看 `error`。** 建链阶段「单个文件失败不中断」，所以任务可以**部分成功**：`linked > 0` 且 `link_failed > 0` 是正常的部分成功（状态判为 `failed`，但已建好的硬链接不回滚、也不损坏数据）。
> 计数含义：`linked`（本次新建）/`skipped`（体积一致而跳过的一级文件夹数 **+ `already_linked`**）/`link_failed`/`already_linked`（目标已存在且**就是源文件的硬链接**，按幂等跳过、不算失败）/`renamed`/`rename_failed`/`orphans_removed`/`folders_scanned`/`bytes_linked`。

---

## 快速索引

| 现象关键词 | 场景 |
|---|---|
| `跨文件系统错误` / `EXDEV` / `Invalid cross-device link` | [1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link) |
| 启动即 `degraded` / 健康检查里有 `false` | [2](#2-启动即-degraded健康检查四项里有-false) |
| 想确认应用没动源文件 / `source_integrity` | [3](#3-源目录是可写的怎么确认应用没有动你的源文件) |
| 第一次就全量重扫 / 账本从空开始 | [4](#4-第一次使用就全量重扫账本从空开始) |
| 满屏 `创建硬链接失败: … File exists` / 反复处理同一批文件夹 | [5](#5-每次运行都满屏创建硬链接失败--file-exists并且重复处理同一批文件夹) |
| 目标目录被删 / `Deleted orphaned hard link folder` | [6](#6-目标目录被自动删除了孤儿清理) |
| `Permission denied` / `Read-only file system` | [7](#7-权限类报错permission-denied--read-only-file-system) |
| 下载完了但没起任务 | [8](#8-qbittorrent-下载完成后回调没有触发任务) |
| 回调 `401` | [9](#9-回调返回-401hook-token-不匹配) |
| 页面打不开 / 端口被占用 | [10](#10-端口-18081-被占用或页面打不开) |
| 忘记密码 / 所有人被登出 | [11](#11-忘记管理员密码--所有人都被登出) |
| ARM64 NAS 上构建或运行异常 | [12](#12-arm64aarch64nas-上的构建与运行) |
| 容器重启后任务停住 / 卡在 `running` | [13](#13-容器重启后的队列状态) |
| 任务一直 `queued` 不动 | [14](#14-任务长时间停在-queued) |
| 没改名 / `ENone` / 集数认错认不出 / `Kept (no episode):` | [15](#15-文件没有被改名--名字里出现-enone) |
| 改了源文件但任务仍跳过 | [16](#16-改了源文件但任务仍然跳过) |
| 配置被改坏 / 红色横幅 / `errors` 非空 | [17](#17-配置文件被外部改坏后如何恢复) |
| 红字「规则已删除」/ 处理器不见了 | [18](#18-处理器被删除了但媒体库还在引用) |
| 保存媒体库报 422 / 日志文件名不合法 | [19](#19-日志文件名不合法422) |
| 特典/扫图目录整棵跳过 / 想换回最朴素的旧口径 | [20](#20-特典目录被跳过时如何用-legacy-换回最朴素的旧口径) |
| `same_filesystem: true` 但真跑全失败 / 探针 `null` | [21](#21-same_filesystem-说可以真跑却全部失败预检假绿) |
| 换单挂载后库路径还是 `/downloads`、`/library`（旧版白名单已删除） | [22](#22-升级到单挂载形态后容器-unhealthy旧的允许根目录还在) |
| 保存报 422、字段以 `rule_override.` 开头 / 「未知变量」 | [23](#23-库级覆盖--命名模板保存报-422) |
| 改了后缀白名单没生效 | [23.1](#231-改了后缀白名单却没生效) |
| 库日志混进访问日志 / 会不会无限增长 | [24](#24-库日志里混进了应用日志会不会无限增长) |
| 文件没改名、停在原地 / `extras_classified` > 0 | [25](#25-文件没被改名停在原地l3-特典排除) |
| 作品记录「已不在」/「未记录」/ 两本账对不上 | [27](#27-作品记录里显示已不在未记录怎么办) |
| 删了作品记录还是没重做 | [28](#28-删了作品记录下次运行还是没重做) |
| 目标里多出 ` (2)` 后缀文件 | [29](#29-删除后目标里出现--2-后缀文件) |
| 文件名少了一段 / `unknown_params` | [31](#31-文件名少了一段命名里用了不存在的变量名) |

> 本文件里的「场景 N」与 `§N` 是同一个编号，两种写法都在用（其它文档与测试里有 `TROUBLESHOOTING.md §17`、`§21`、`场景 1`/`场景 3`、`场景 24` 这样的引用）。**编号与主题一一对应且不再重排**；新增场景只能追加编号。
>
> **编号 26、30 已迁走、不复用，所以编号看起来不连续**：待办看板的分页/圆圈/一键检测 → [`USER_GUIDE.md`](USER_GUIDE.md) §2.5 与 [`KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-10；「作品记录」搜不到某个作品 → KI-8。

---

## 1. 任务全部 `link_failed`、错误里有「跨文件系统错误」/ `[Errno 18] Invalid cross-device link`

**【现象】**

- 任务 `status: failed`，每个文件都是：`创建硬链接失败: /library/… - [Errno 18] Invalid cross-device link`（Windows 上是 `[WinError 17]`，语义相同）。
- `result` 里 `linked: 0`、`link_failed` 等于文件数；库目录只落了一半结构。
- 系统信息页 `same_filesystem` 为 `false`；也可能报 `true`（**假绿**，见[场景 21](#21-same_filesystem-说可以真跑却全部失败预检假绿)）。

**【原因】** 判据不是"同一块盘"，而是**同一个挂载**：内核在 `do_linkat()` 里直接比较源与目标的挂载身份，不同挂载一律 `EXDEV`，与 `st_dev`、`fsid`、btrfs 子卷、ZFS dataset 是否相同**无关**。原理与各家 NAS 的实测结论见 [`COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) §2。

**正确形态**：一条 `${MEDIA_ROOT:-/vol1/1000}:${MEDIA_ROOT:-/vol1/1000}` bind（`MEDIA_ROOT` 取源与目标的**共同父目录**，通常 `/vol1/1000`）+ 一条 `./data:/data`；完整 compose 见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §2。**不要把源目录再单独拆一条 `:ro` bind** —— 那会立刻退回本场景；源目录的保护改由代码层与运行期核对提供，见[场景 3](#3-源目录是可写的怎么确认应用没有动你的源文件)。

**不会静默复制冒充硬链接**：现象是"库目录只落一半结构"，不是"多出一份副本"。已建好的硬链接不回滚，重跑时体积一致的文件夹会被跳过。

**【诊断命令】**

**【NAS 宿主】** 一条就能定论 —— 源与目标是不是**同一个挂载根**：

```bash
findmnt -T /vol1/1000/Download/qbittorrent -o TARGET,SOURCE,FSTYPE,OPTIONS
findmnt -T /vol1/1000/Library              -o TARGET,SOURCE,FSTYPE,OPTIONS
# 两条 TARGET 必须是同一个挂载根。不同 ⇒ 必然 EXDEV。
```

容器实际挂了几条：

```bash
docker inspect media-ingest --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} rw={{.RW}}{{"\n"}}{{end}}'
# 正确形态：一条 MEDIA_ROOT（容器路径 = 宿主路径）+ /data。还能看到 /downloads 与 /library ⇒ 就是本场景。
```

**【容器内】** 只读安全的实证（拿一个**已存在**的源文件链到目标根，探测完立刻删，不写源目录）：

```bash
docker exec -it media-ingest sh -c \
  'f=$(find /vol1/1000/Download/qbittorrent -maxdepth 3 -type f | head -n1); \
   ln "$f" /vol1/1000/Library/.mlm_probe_test && echo LINK-OK && rm -f /vol1/1000/Library/.mlm_probe_test || echo LINK-FAILED'
```

**【浏览器】** 对出问题的库点「校验」，看 `same_filesystem` 与 `probe.note`。

**【解决步骤】**

1. 把 compose 改成上面的**单一共同父挂载 + `/data`**（删掉 `/downloads:ro` 与 `/library` 两条），**【NAS 宿主】** `docker compose up -d`（**不要**用 `restart`，它不重读 compose 文件）。
2. 宿主上确认两个目录同一个挂载；容器内确认上面那条 `ln` 输出 `LINK-OK`。
3. **【浏览器】** 点「校验」→ 应看到 `same_filesystem: true` 且 `probe.ok: true`；再点「立即运行」。**已"部分成功"的任务不用清理**：已建好的硬链接有效，重跑只补做失败的部分。
5. 源与目标确实放不进同一个挂载（两块物理盘）时，唯一合规做法是**把其中一方搬到另一方的挂载里**；本项目**不提供**跨挂载复制模式（见 [`FAQ.md`](FAQ.md) 第 1、3 问）。

> 💡 `probe.note` 是给人看的中文可执行文案，别忽略。无法实证时 `same_filesystem` 是 `null` 而**不是** `true` —— 别把"无法实证"当成"通过"。

---

## 2. 启动即 `degraded`：健康检查四项里有 `false`

**【现象】** 容器起来了，但 `/api/system/health` 的 `status` 是 `"degraded"`，页面顶部出现红字，或任务一开跑就失败。

**【先看是哪一项】**

| `false` 的项 | 含义 | 去哪 |
|---|---|---|
| `db` / `data_dir` / `config` | 数据卷不可写、数据库打不开或损坏、配置目录读不了 | [场景 7](#7-权限类报错permission-denied--read-only-file-system) |
| `processors` | 三个内置处理器没齐备或注册表不可用 | [场景 17](#17-配置文件被外部改坏后如何恢复)、[场景 18](#18-处理器被删除了但媒体库还在引用) |

每一项都在 `details` 里带中文原因。**四项全绿却仍 `degraded`** ⇒ 看 `warnings`：目前唯一来源是**容器内文件系统编码不是 UTF-8**，按消息里点名的三个环境变量修（`LANG=C.UTF-8`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`，它们在镜像里已预设）。

**【诊断命令】**

**【NAS 宿主】**

```bash
curl -s http://127.0.0.1:18081/api/system/health | python3 -m json.tool
curl -s http://127.0.0.1:18081/api/processors | python3 -m json.tool | head -n 40
```

> 四项检查里**没有**"外部依赖"这一项：镜像自带运行环境，宿主不需要装 Python 或别的工具。所以 `degraded` 只可能来自上表四项或 `warnings`。

**【解决步骤】**

1. `degraded` **不代表数据坏了**：先按上表定位那一项；改过 `/data/config/` 下的文件后应用会重新读取，`processors:false` 一直不消失就检查内置处理器（[场景 18](#18-处理器被删除了但媒体库还在引用)）。
2. 修好后 **【NAS 宿主】** `docker compose up -d`，`status` 应回到 `ok`。

---

## 3. 源目录是可写的：怎么确认应用没有动你的源文件

> 源目录与目标同属**一条可写挂载**：单挂载形态下它不可能同时只读（原因见[场景 1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link)，展开论证见 [`COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) §2 与 [`DEPLOYMENT.md`](DEPLOYMENT.md) §3.5）。

**【现象】**

- 挂载表里**看不到单独的 `/downloads` 条目**，只有一条 `MEDIA_ROOT`（容器路径 = 宿主真实路径、`read_only: false`）与 `/data` —— 这是**预期**。
- 真正需要处理的：源目录树里出现**非下载工具产生**的改动（源文件 `mtime` 变了、多了临时文件/半成品），或任务结果里 `source_integrity.ok` 为 `false`。

**【原因】** 「源目录单独挂载且只读」与硬链接**互斥**（源在另一条 bind 上 ⇒ 必然 `EXDEV`），所以这道物理保证退场，换成**可验证的保证**：代码层不写源目录 + 每次任务前后逐文件源完整性核对（`tests/test_no_source_mutation.py` 钉着）。能否被绕过：理论上能（进程有写权限），但每次实际改动都会留下 `changed` 路径与告警。

**【诊断命令】**

别用 `touch /downloads/...` 取证（源目录本来就可写，探不出"应用有没有乱写"）。判"应用有没有动源文件"看**任务结果里的核对结论**：

**【浏览器】** 打开出问题的任务详情，看 `result.source_integrity`：

- `ok` 应为 `true`。
- 若为 `false`：`changed[]` 列出**被动过**的源文件路径，`warnings[]` 里有中文告警。**这属于缺陷，请附任务 id 与这两段原文报 issue。**
- 硬链接本身会让 `nlink`/`ctime` 变化，**这不算**源被改动（核对口径刻意排除这两个字段）。

**【NAS 宿主】** 需要人工比对时，看源树最近是否被写过，并顺手确认挂载形态（应当**没有** `/downloads`/`/library` 两条独立挂载）：

```bash
find /vol1/1000/Download/qbittorrent -type f -newermt '-1 hour' -printf '%TY-%Tm-%Td %TH:%TM %p\n' | head -n 50
docker inspect media-ingest --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} rw={{.RW}}{{"\n"}}{{end}}'
```

**【解决步骤】**

1. **正常情况什么都不要做**：`MEDIA_ROOT` 是 `rw=true` 是**预期**的（目标要写）。只要 `source_integrity.ok` 一直为 `true`，源就在受保护状态。
2. **若 `ok == false`**：先把 `changed` 路径与告警原文抓下来，**在重跑之前**确认这些文件是不是下载工具自己写的（qBittorrent 做种/校验会改 `mtime`）。确认不是 ⇒ 报 issue（附任务 id、`changed`、有的话加 `sha256sum`）。
3. **不要**为了"让源只读"再拆一条 `:ro` bind —— 那会直接打回[场景 1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link)。想要"源绝对不可写"就只能放弃硬链接（本项目不提供复制模式），二选一。

---

## 4. 第一次使用就全量重扫（账本从空开始）

**【现象】**

- 新接入的库（账本还是空的）第一次运行就全量重扫：`folders_scanned` 等于库里的文件夹数、`skipped` 为 `0`。
- **【NAS 宿主】** `<data>/logs/hard_links.log` 不存在，或存在但没有本库源目录的记录。

**【原因】** "这个一级文件夹处理过"的**首选依据是每库自己的账本**（数据库表 `link_records`：本库 + 同 `source_dir` + 同 `target_dir` + 同体积），执行器把这份账本当 `skip` 传给建链实现。账本从**空**开始，所以第一次接入必然全量扫描；跑过一次后第二次开始才有 `skipped`。`/data/logs/hard_links.log` 只是**审计流水**（只在没有库账本时才被当兜底判据读，见[场景 5](#5-每次运行都满屏创建硬链接失败--file-exists并且重复处理同一批文件夹)）：宿主上别处留下的同名文件**不会被继承** —— 应用只读写 `/data` 这条数据卷。

**重做本身是安全的**（不覆盖、不丢数据），只是会刷出一批 `File exists`；这些"目标就是同一 inode 硬链接"的条目会被改判为**幂等跳过**（`already_linked`、计入 `skipped`），任务不会因为"全部已存在且 `linked == 0`"被判 `failed`。

**【诊断命令】**

**【NAS 宿主】**

```bash
docker compose exec media-ingest ls -l /data/logs/
docker compose exec media-ingest wc -l /data/logs/hard_links.log
docker compose exec media-ingest grep -o 'Source: [^ ]*' /data/logs/hard_links.log | sort -u
```

`grep` 的输出应当与媒体库的 `source_dir` **逐字相同**（单挂载形态下就是宿主真实路径）。若出现 `/downloads/...` 这种容器内已不存在的旧路径 ⇒ 见[场景 5](#5-每次运行都满屏创建硬链接失败--file-exists并且重复处理同一批文件夹)。

**【浏览器】** 「系统信息」→ `mounts[]` 里源目录所在的那条 `MEDIA_ROOT` 应为 `exists: true`；「任务」详情看 `result.folders_scanned` / `result.skipped`。

**【解决步骤】**

1. **第一次全量重扫是预期的**：跑完就写下了本库记录，之后才有 `skipped`。
2. **别指望把宿主上别处的旧日志接进来**：应用只读写 `/data`。要么让它全量跑一遍，要么先按[场景 5](#5-每次运行都满屏创建硬链接失败--file-exists并且重复处理同一批文件夹) 处理旧日志；**不要**手工改日志里的体积（[场景 16](#16-改了源文件但任务仍然跳过)）。
3. 数据卷里的 `hard_links.log` 被删/损坏 ⇒ 等价于"回到第一次"，重跑一次即恢复。这个文件只写两类记账行（体积登记 + 孤儿目录清理），其余应用日志不混入、历史行也不清理（[场景 24](#24-库日志里混进了应用日志会不会无限增长)）。
4. 日志位置固定为 `<data>/logs/hard_links.log`（由原生实现 `app/processors/hardlink.py` 自己读写），格式与应用日志的分工见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §2.6。`HOST_PATH_MAP` 只用于**展示与路径换算**，**不参与**日志匹配 —— 改了它不会影响"跳过"判定。

---

## 5. 每次运行都满屏「创建硬链接失败: … File exists」，并且重复处理同一批文件夹

**【现象】**

- 每个任务都在刷：`创建硬链接失败: <目标目录>/… - [Errno 17] File exists`（Windows 上是 `[WinError 183]`）。
- `skipped` 恒为 `0`，`folders_scanned` 与库里的文件夹数一样多 —— **每次都是全量重扫**。
- 日志里那些记录的 `Source:` 与媒体库当前的 `source_dir` 不一致（典型：容器内已不存在的 `/downloads/...`，或你换过源目录之后的旧路径）。

**【原因】** 现行建链阶段（原生实现 `app/processors/hardlink.py`）判定"这个文件夹我处理过"的**首选依据是每库作品账本**（[场景 27](#27-作品记录里显示已不在未记录怎么办)）。**只有在没有库账本时**才退回另一判据：读 `<data>/logs/hard_links.log`、按日志里的 `Source:` 字段与传进来的源目录字符串**完全相等**来认。所以日志里的 `Source:` 与当前 `source_dir` 对不上，**并不会**导致重链（那份日志只当审计记录，应用不会改写它）；**真正的原因是这一批作品不在账本里**（账本从空开始，或记录被删过，见[场景 4](#4-第一次使用就全量重扫账本从空开始)、[场景 28](#28-删了作品记录下次运行还是没重做)）⇒ 每轮都当新文件夹处理。硬链接建第二次必然 `File exists`（数据不受损，但等于白跑）。

> ⚠️ **"跳过"判定不看这份日志**。`hard_links.log` 照写，但只当审计日志；所以"删掉旧日志让它重建"只影响**审计记录与孤儿清理**，不会让任何作品重做。要真正重做某个作品，走[场景 28](#28-删了作品记录下次运行还是没重做)。

**【诊断命令】**

**【NAS 宿主】**

```bash
docker compose exec media-ingest head -n 5 /data/logs/hard_links.log
docker compose exec media-ingest grep -o 'Source: [^ ]*' /data/logs/hard_links.log | sort -u
docker compose exec media-ingest grep -c 'Folder:' /data/logs/hard_links.log
```

**【解决步骤】**

1. **先确认真的是"账本里没有"**：**【浏览器】**「作品记录」→ 选中该库 → 搜这个作品名，看有没有记录；没有 ⇒ 下次运行会重做一次（搜索是朴素子串、且只搜当前库，搜不到不等于没记录，见 [`KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-8）。
2. **想让审计记录与当前 `source_dir` 对齐**（可选、不影响跳过判定）：**【NAS 宿主】**
   `mv <data>/logs/hard_links.log <data>/logs/hard_links.log.bak` —— 不需要重启，下一次任务开始时会建出空日志。
3. **别手工改日志里的 `Source:` 值**，更**绝对不要**改 `Folder size:` 的数值 —— 体积是回退路径下的幂等标记，手改会造成"该文件夹永远不处理"。
4. `HOST_PATH_MAP` **不参与**日志匹配（它只用于展示与路径换算），改它修不好本场景。

> ⚠️ **文件夹名里含 `" - "` 的记录永远解析不了**（L2）：这类文件夹在回退路径下每次都被当成"未记录"，表现为同一个 folder 反复出现 `File exists`。这是冻结的既有行为，应用不会"修"它；其 `already_linked` 会计入 `skipped`，任务**不会**因此失败，只是日志每轮多一行。

---

## 6. 目标目录被自动删除了（孤儿清理）

**【现象】**

- 任务日志/stdout 里出现 `Deleted orphaned hard link folder: /vol1/1000/Library/…/Show A`，`result.orphans_removed > 0`。
- 媒体库里某个作品文件夹**整目录消失**了。

**【原因】** 这是**有意的**功能（原生实现 `app/processors/hardlink.py`）：账本记录了 folder X 已处理，但**源目录里已经没有 X**（你删了原始下载）⇒ 目标里的 `X` 目录被视为"孤儿"，整个 `rmtree` 掉。

**关键：它删的是目标里由本应用生成的硬链接目录，绝不碰源目录里的文件。** 单挂载形态下源目录与目标同挂载、本身可写（[场景 3](#3-源目录是可写的怎么确认应用没有动你的源文件)），这条边界由代码层保证并被 `tests/test_no_source_mutation.py` 钉住。

**【诊断命令】**

**【浏览器】** 任务详情 → `result.samples.orphans_removed`，看被删的是哪些**目标**路径。

**【NAS 宿主】**

```bash
grep 'Deleted orphaned' <data>/logs/hard_links.log | tail -n 20
ls -l /vol1/1000/Download/qbittorrent/Bangumi/          # 源文件一定还在
```

**【解决步骤】**

1. **先判断这是不是你想要的**：你确实删了原始下载、也不想在媒体库留空壳 ⇒ **正常行为**，不用管。
2. 想**保留**这些目录（原始下载只是临时移走）：**【浏览器】** 编辑该媒体库 → 关掉「孤儿清理」（`orphan_cleanup`）→ 保存。之后该库的任务不会删任何目标目录。
   > `orphan_cleanup=false` 的实现方式：**先把日志里"源已不存在"的行摘出来**，跑完再**原样合并回写**（位置不变、逐字节复原），所以既不删目录也不让日志抖动。
3. 想恢复被删的目录：**重跑一次**该库即可（源还在的话硬链接会重新建出来，几乎不占额外空间）。
4. **误删的边界**：只有"账本里有记录 + 源目录里确实没有该文件夹 + 目标里存在同名目录"三条同时成立才会删。要确认历史状态，直接看 `<data>/logs/hard_links.log` 原文；`GET /api/system/info` 里**没有**字段替你回答这个问题。

---

## 7. 权限类报错（`Permission denied` / `Read-only file system`）

**【现象】**

- `创建硬链接失败: <目标目录>/… - [Errno 13] Permission denied`
- `[Errno 30] Read-only file system`（往只读挂载或镜像内文件写）
- 健康检查里 `data_dir` / `config` 为 `false`（v2 已删掉源/目标根两项检查，见[场景 2](#2-启动即-degraded健康检查四项里有-false)）
- 容器起不来，日志里有 `无法创建 /data/...`

**【原因】** 容器以非 root 用户（`app`，`PUID`/`PGID`）运行；`/data` 与**目标目录**的宿主属主/权限必须让该用户可写。源目录在单挂载形态下本身可写，但**应用不写它**（[场景 3](#3-源目录是可写的怎么确认应用没有动你的源文件)）。属主、`PUID`/`PGID` 与常见报错对照的完整说明见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §10.5。

**【诊断命令】**

**【容器内】**

```bash
docker exec -it media-ingest id
docker exec -it media-ingest ls -ld /data /data/logs /data/config /vol1/1000/Library /vol1/1000/Download/qbittorrent
```

**【NAS 宿主】** 看宿主侧属主（飞牛/Debian 上常见 `uid=1000`）：

```bash
stat -c '%u:%g %a %n' /vol1/1000/Docker/media-ingest/data /vol1/1000/Library
```

**【浏览器】** 系统信息页挂载表里的 `exists` / `read_only`；健康检查 `details` 里的中文原因。

**【解决步骤】**

1. **数据卷必须可写**（最常见的坑）：**【NAS 宿主】**（`1000:1000` 换成你自己的 `<PUID>:<PGID>`，即容器内 `id` 看到的那个）
   ```bash
   mkdir -p /vol1/1000/Docker/media-ingest/data/logs
   chown -R 1000:1000 /vol1/1000/Docker/media-ingest/data
   chmod -R u+rwX /vol1/1000/Docker/media-ingest/data
   ```
2. **媒体库根必须可写**（应用要在里面建硬链接）：把 `/vol1/1000/Library` 的属主改成与容器内 `id` 一致，或给它组写权限。不想改整库属主就至少保证**目标子目录**可写（例如只把 `/vol1/1000/Library/Bangumi` 交给它）。启动失败几乎都是 `/data` 不可写（第 1 步）。
3. 「权限模式」（`permission_mode`，**默认关**）是**真实开关**：默认关闭时一个 `chmod` 都不做；显式开启（界面从关到开需二次确认）后，应用会把**目标侧**（库根 + 本次处理到的每个作品文件夹的目标子树，含被跳过的旧作品）改成 `0777`。⚠️ 硬链接与源文件**共享 inode**，所以开启会**同时改掉源文件的权限位**（内容/大小/inode/mtime 不变）—— 请优先按上面 1–2 步在宿主上做精确调整。每次开启运行都会在任务日志里以 WARNING 记下改了多少目录/文件。开关说明见 [`USER_GUIDE.md`](USER_GUIDE.md) §8.9，契约依据见 [`BASELINE.md`](../contracts/BASELINE.md) §1.6。
4. 群晖/威联通上若出现奇怪的 `EACCES`：确认没有开启"共享文件夹只读快照/回收站"之类的宿主级限制。

---
## 8. qBittorrent 下载完成后回调没有触发任务

**【现象】** 下载完成，但「任务」列表里没有新任务；qBittorrent 里那条「运行外部程序」看起来配了却没反应。

**【原因】** 按概率：① 「运行外部程序」没启用、或命令被引号/空格弄坏；② token 不对（会 **401**，见场景 9）；③ 库的**匹配条件全空**（分类/标签/路径前缀都为空的库**永远不会被回调命中**）；④ 该库 `enabled=false`，或已有排队/执行中的任务（扫描与回调都会跳过它，见场景 14）；⑤ 容器与 qBittorrent 网络不通。

**【诊断命令】**

**【NAS 宿主】** 手工打一次回调（`<TOKEN>` 换成设置页复制的那个）。**两个回调路由都只接受 `POST`**，忘了 `-X POST` 会拿到 `405`：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  'http://127.0.0.1:18081/api/hooks/qbittorrent?token=<TOKEN>&name=Test&hash=abcdef1234567890&category=&tags=&content_path=/vol1/1000/Download/qbittorrent/Bangumi/Show%20A&save_path=/vol1/1000/Download/qbittorrent'
```

去掉 `-o /dev/null` 看响应体：`{"matched":N,"tasks":[…],"ignored":[…],"ignored_details":[…],"warnings":[…],"torrent":{…}}`。再看容器有没有收到请求：`docker compose logs --tail=100 media-ingest | grep -i hook`。

**【解决步骤】**

1. **参数放查询串或 `application/x-www-form-urlencoded` 表单体都行，JSON body 一律被静默忽略**（于是 `matched` 恒为 0、还不报错）。业务参数 7 个：`name`/`hash`/`category`/`tags`/`content_path`/`save_path`/`root_path`（qB 占位符 `%N`/`%I`/`%L`/`%G`/`%F`/`%D`/`%R`），`token` 一般放查询串。`kind=` 是**可选的显式覆盖**（`hardlink`/`rename`/`rename_hardlink` 等，见 `app/routers/hooks.py` 的 `EXPLICIT_KINDS`），不传就按库的处理器推断。
2. 命令形如 `/usr/bin/curl -s -X POST "http://<NAS_IP>:18081/api/hooks/qbittorrent?token=<TOKEN>&name=%N&hash=%I&category=%L&tags=%G&content_path=%F&save_path=%D&root_path=%R"`；或者把 `%N` 那些用 `--data-urlencode "name=%N"` 逐个放进表单体。Windows 客户端用 `curl.exe` 并给整串加引号。填法见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §5.2。
3. **给库配至少一个匹配条件**，否则永远命中不了；响应 `ignored_details` 里有中文原因。
4. `matched: 0` 且 `warnings` 说「当前没有任何**已启用**的媒体库」⇒ 去媒体库页启用。
5. 想「什么都收」用 `POST /api/hooks/scan?token=…`：它**不看匹配条件**，按已启用库逐个建任务（源目录不存在也会建，任务会以失败告终并给出原因）。
6. 网络：从 qBittorrent 所在容器/主机 `curl` 一下健康检查；跨容器时用 NAS 真实 IP 而不是 `127.0.0.1`。

---

## 9. 回调返回 `401`（hook token 不匹配）

**【现象】** qBittorrent 侧外部程序退出码非 0 / 日志里是 401：

```json
{"detail":"回调 token 不正确…请在「设置」页重新生成 token 后，更新 qBittorrent 的「运行外部程序」。"}
```

**【原因】** token 缺失、拼错、带了空格/引号；或你在设置页**重新生成**过 token（旧值随即失效）。

**【诊断命令】** **【浏览器】** 设置页复制当前 `hook_token`（明文显示）。**【NAS 宿主】** 两种传法都应 200（记得 `-X POST`）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST 'http://127.0.0.1:18081/api/hooks/qbittorrent?token=<TOKEN>&name=T&hash=h&content_path=/x&save_path=/y'
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Authorization: Bearer <TOKEN>' 'http://127.0.0.1:18081/api/hooks/qbittorrent?name=T&hash=h&content_path=/x&save_path=/y'
```

**【解决步骤】**

1. 设置页**重新生成** token（生成后无法找回旧值），立刻替换 qBittorrent 里的命令并做上面第 1 条验证。
2. **shell 引用**：URL 里有 `&`，Windows 的 `cmd`/PowerShell 里不加引号会把参数截断 ⇒ token 变成空 ⇒ 401。
3. token 泄露过（贴进聊天记录/截图）必须重新生成：持有者能触发任务（但改不了你的文件、删不了硬链接）。
4. 从网页复制时注意首尾空格/换行。

---

## 10. 端口 18081 被占用，或页面打不开

**【现象】** 容器起不来，日志里 `bind: address already in use`；或容器 running 但页面连不上。

**【原因】** 宿主上已有进程/容器占用 18081；或改了 `PORT` 却只改一侧（容器内监听端口与 `ports:` 的容器侧不一致）；或防火墙拦了。

**【诊断命令】**

```bash
ss -lntp | grep 18081 || netstat -lntp | grep 18081
docker ps --format '{{.Names}}\t{{.Ports}}' | grep 18081
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18081/api/system/health   # 000=连不上，200=应用正常
```

**【解决步骤】**

1. 换端口：**只改 `.env` 的 `PORT`**（compose 的 `ports: "${PORT:-18081}:${PORT:-18081}"` 与容器内 `PORT` 会同步跟随），然后 `docker compose up -d`。**不要**手写只改一侧的 `ports:`——宿主端口会指向没人监听的端口。详见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §8.1。
2. 放通防火墙/安全组上的该端口。
3. 健康检查用的是容器内 `$PORT`，改端口后自动跟随。
4. 容器 running 但一直 `unhealthy`：`docker inspect media-ingest --format '{{json .State.Health}}'`，多半是端口不一致（第 1 条），见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §4.4.1。

---

## 11. 忘记管理员密码 / 所有人都被登出

**【现象】** 登录页输什么都 401（`{"detail":"用户名或密码错误。"}`）；或本来好好的，突然所有人都要重新登录。

**【原因】** 口令用 scrypt 哈希保存，**无法找回、只能重置**。另一种情况是数据卷里的 `secret.key`（会话签名密钥）丢了/被换了 ⇒ 所有会话失效，**但密码没变**，重新登录即可。

**【诊断命令】**

```bash
docker exec -it media-ingest ls -l /data/secret.key /data/mlm.db
docker exec -it media-ingest python -c "import sqlite3;print(sqlite3.connect('/data/mlm.db').execute('SELECT id,username,created_at FROM users').fetchall())"
```

**【解决步骤】**

1. **先备份**：`cp -a <data> <data>.bak-$(date +%F)`。
2. **清空账号**（只影响登录，**不动媒体库规则、任务历史和你的文件**）：
   ```bash
   docker exec -it media-ingest python -c "import sqlite3;c=sqlite3.connect('/data/mlm.db');c.execute('DELETE FROM users');c.commit();c.close();print('账号已清空')"
   ```
3. `docker compose restart`，再打开页面回到「创建管理员」。
4. `.env` 里的 `ADMIN_USERNAME`/`ADMIN_PASSWORD` 只在「一个用户都没有」时自动建账号；建好后把明文密码删掉。
5. **只有你自己被登出**而密码是对的 ⇒ 那是 `secret.key` 的问题，**不要删账号**，直接重新登录；确认 `data/secret.key` 没被备份工具/挂载变更弄丢。

---

## 12. ARM64（aarch64）NAS 上的构建与运行

**【现象】** 构建报 `no matching manifest`、运行报 `exec format error`，或跑起来极慢。

**【原因】** 基座 `python:3.11-slim` 是官方 multi-arch（ARM64 有）；依赖只有 `fastapi` + `uvicorn[standard]`，后者带 `uvloop`/`httptools` —— 优先用预编译 wheel，装不上时会尝试源码编译（需要编译器 ⇒ 慢或失败）。另外本应用**必须单进程**：`--workers 1` 是**架构要求**（建链阶段对日志做「读-改-写整份」并依赖全局串行，见 [`BASELINE.md`](../contracts/BASELINE.md) §4 的 B9）。

**【诊断命令】** `uname -m`（`aarch64` = ARM64）、`docker version --format '{{.Server.Arch}}'`、`docker image inspect media-ingest:latest --format '{{.Architecture}}/{{.Os}}'`。

**【解决步骤】**

1. **原机构建**：直接在 ARM64 机器上 `docker compose build`，不要从 x86 导出镜像再拷过去。步骤见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §9.2。
2. 构建慢/失败：先 `docker pull python:3.11-slim`，再 `docker compose build --no-cache`。
3. `exec format error` = 架构不匹配，重建即可。
4. **绝对不要**为提速加 `--workers`/`replicas`：多进程会破坏「同一时刻只有一个任务」与日志写入纪律（B9 / L1）。要快请缩小范围（按文件夹运行）；为什么不能并发的完整说明见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §11 与 [`BASELINE.md`](../contracts/BASELINE.md) §4 的 B9。
5. `uvloop` 确实装不上时才需要在构建期加编译工具链（`gcc`/`python3-dev`）—— 那属于改 Dockerfile，先备份。

---

## 13. 容器重启后的队列状态

**【现象】** 重启后任务列表里有的任务停在 `queued`/`running`；或想知道「重启会不会接着跑」。

**【原因】** 队列**持久化在 SQLite**（`tasks` 表），工作线程在**启动时立刻**按 id 升序 FIFO 消费 `status='queued'`（B9）。因此：

- ✅ **重启后仍在 `queued` 的任务会自动继续执行**（不需要你点任何东西）。
- ⚠️ **重启瞬间正在 `running` 的任务不会续跑**：进程被杀，那条记录停在 `running`（历史记录），既不会自动重试、也不能「重试」（`POST /api/tasks/{id}/retry` 只接受 `failed`/`cancelled`，否则 **409**）、不能取消（`running` 取消 **409**）、不能删除（**409**）。
- 唯一善后方式：对**那个媒体库**点「立即运行」重新发起一次（已建好的硬链接会被跳过，见场景 16）。
- 取消只在**还没开始执行**时有效（`queued` ⇒ 204）。

**【诊断命令】**

```bash
docker compose exec media-ingest python -c "import sqlite3;c=sqlite3.connect('/data/mlm.db');[print(r) for r in c.execute('SELECT id,library_name,status,created_at,started_at FROM tasks ORDER BY id DESC LIMIT 20')]"
```

**【浏览器】** 「统计」页看 `queue.running_task_id` / `queue.queued_count`；「任务」页按状态筛选。

**【解决步骤】**

1. `queued_count` 在下降 ⇒ 队列正在消费，**等它跑完**（大库可能很久）。别重复点「立即运行」，那只会多排一个任务。
2. 看到「卡在 `running`」的那条：**不要试「重试」「取消」「删除」**（都会 409），直接对该库「立即运行」。
3. `restart: unless-stopped` 建议保留 —— 它会自动拉起容器，队列随之续跑。
4. 重启后任务立即大批失败且含 `File exists`：与重启无关，见场景 5。

---

## 14. 任务长时间停在 `queued`

**【现象】** 状态一直 `queued`，日志只有「任务已入队（类型=…，触发=…）」。

**【原因】** 队列**全局单线程**（一次一个任务，B9）。前面有长任务时后面的只能排队。另外：**同一媒体库已有排队/执行中任务时，扫描与回调会直接跳过它** —— 「我点了扫描但它没建任务」多半是这个。

**【诊断命令】** 「统计」页 `queue.running_task_id` / `queued_count`；「任务」页筛 `status=running` 看是谁在占用；`docker compose logs --tail=50 media-ingest` 确认工作线程活着。

**【解决步骤】**

1. 正常排队 ⇒ 等前一个跑完（全量首跑几十分钟很正常）。
2. **不要**加 `--workers 2` 或起第二个容器（会破坏串行保证，见场景 12 第 4 条）。
3. 想立刻做某个库：取消排在它前面的 `queued` 任务；正在跑的那个**只能等**（应用不提供强杀）。
4. `running_task_id` 指向一个其实没在跑的任务 ⇒ 重启容器后用「立即运行」重新发起（重启会自动唤醒队列消费）。
5. 首跑太慢的根治办法是**分库/分文件夹**跑（`kind` 支持 `scope.folder` 只处理一个一级文件夹）。

---

## 15. 文件没有被改名 / 名字里出现 `ENone`

**【现象】** 某个文件变成 `Show A S01ENone.mkv`；或某些文件完全没动（日志里 `Kept (no episode): …`）；或 `Season 1 (2018)` 里的文件没被处理。

**【原因】** 这些**大多是冻结的既有行为（L3/L5/L6/L7）**，应用不会「修正」（见 [`BASELINE.md`](../contracts/BASELINE.md) §5）：

- **文件名里没有任何集数标记**：看规格里的 `episode_fallback` ——
  - 配了**非 `null`** 的字面量（`legacy` 预设的字符串 `"None"`，或你自己写的值）⇒ 集数段就是那个字面量 ⇒ `…S01ENone.mkv`（L3）。
  - **没配 / 留空 / 纯空白**（`episode_fallback: null`，**内置 `bangumi` 的推荐默认**）⇒ 引擎**不给它造名字**：命名引用了 `episode`/`episode_raw` 的这个文件**保留原文件名**，日志一行 `Kept (no episode):`（WARNING），计入 `result.kept_unresolved_episode` + `warnings[]`。
- **只处理 `.mkv`/`.mp4`/`.ass` 且大小写敏感**（L7）：`UPPER.MKV` 不改名；`.srt` 不改名但**会被建硬链接**。
- **集数认错**（`legacy` 那两条正则的既有毛病）：`-\s*(\d{1,2})` / `E(\d{1,2})` 没有任何「这里不可能是集号」的约束，于是 `Show - 1920x1080.mkv` 被认成第 **19** 集、`[1AE35F3A]` 里的 `E35` 被认成第 **35** 集。内置 `bangumi` 默认 9 条带边界约束的规则规避这两类（[`PROCESSORS.md`](../contracts/PROCESSORS.md) §3.2 增量 ④）。
- **集数认不出**（新规则比旧的**严格**，这是有意的）：`Show.E07.mkv`（规则 8 要求 `E` 前是**空白**）、`Show 12v2.mkv`、`Show 1920x1080.mkv`（规则 9 要求数字后是空白或句点）都不命中 ⇒ 保留原名并出现在 `kept_unresolved_episode` 里。想恢复旧行为：整体用 `legacy` 预设，或把集数规则换回 `legacy` 那两条。
- **季文件夹必须完整匹配 `Season (\d+)`**：`Season 1 (2018)` 被忽略（L5）；只处理季文件夹的**直接子文件**，再套一层目录里的文件永不重命名（L6）。
- 同一后缀有多个文件时**只保留体积最大的那个**，其余保持原名。

**【诊断命令】** **【浏览器】** 任务详情 → `result.renamed` / `rename_failed` / `kept_unresolved_episode` / `samples.*`；任务日志里搜 `Renamed:`、`Kept (no episode):`、`Error renaming`。

**【NAS 宿主】** 每个库的重命名日志是 JSON（`<作品名> -> {size, timestamp, works_dir}`），位置与格式见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §2.6：

```bash
docker compose exec media-ingest head -c 400 /data/logs/bangumicache.log
```

**【解决步骤】**

1. 先按上面逐条核对（**这些不是 bug**）。完整规则与冻结证据：`BASELINE.md` §2/§3、`PROCESSORS.md` §3.2。
2. **`rename_failed > 0`** 常见于**目标名已存在**：无集数的文件全算成同一个 `Show A S01ENone.mkv` 互相撞名。修法：让文件名带上集数（`第01集`/`[01]`/`EP01`/`E01`/` - 01`/`#01`；注意 `E01` 前必须是**空白**，`Show.E01.mkv` **不算**），或把 `episode_fallback` 留空 ⇒ 这类文件保留原名、根本不再撞名。
3. **不会静默覆盖（L4）**：目标名被占用时按**体积**判定 —— 最大者拿干净名，其余加 ` (2)`/` (3)`（`renamed_with_suffix`），原有同名且更小的改名**让位**（`displaced_targets`，**不删**），找不到空位则保留原名并记 `error`。所以 `name_collisions > 0` 只说明**发生过争名**，不等于丢文件。
4. `Season 1 (2018)` 这类：改目录名为 `Season 1`（**这会改变你的库结构**，自行决定）—— 应用不能替你放宽匹配（冻结契约）。
5. 第二遍跑时已正确命名的文件会「改到自己」（`renamed: [("X","X")]`），正常无害。

---

## 16. 改了源文件但任务仍然跳过

**【现象】** 你在下载目录里**原地替换/改写**了某个已处理文件夹里的文件（**体积没变**），重跑时该文件夹被「跳过」。

**【原因】** 幂等标记是**一级文件夹的总体积**（L9）：体积一样就跳过（日志 `文件夹已记录且体积一致，跳过: <folder>`）。而且体积核对是**事后**的：链接完成后源被同尺寸改写不会被发现（L8）。**这是冻结的既有规则，不是本应用的取舍。**

**【诊断命令】** **【浏览器】** 任务日志搜 `文件夹已记录且体积一致，跳过:`。**【NAS 宿主】** 对比账本体积与实际体积：

```bash
docker compose exec media-ingest python -c "import sqlite3;c=sqlite3.connect('/data/mlm.db');[print(r) for r in c.execute('SELECT folder,size FROM link_records ORDER BY id DESC LIMIT 20')]"
du -sb /vol1/1000/Download/qbittorrent/Bangumi/*/
```

**【解决步骤】**

1. **只想重做某个作品**：运行弹窗里**没有「强制重跑」开关**（前端根本不渲染它，应用固定以 `force=false` 调用），接口传整库 `force` 会 **400**：「整库强制重跑已停用：force 只允许配合 scope.mode='folder'（单个作品）使用。…」。正路二选一：① 到「作品记录」**删掉该作品的记录** ⇒ 下次运行自动重做（⚠️ 会连带删掉目标里那个作品文件夹，源目录不动）；② 只想重排名字 ⇒ 勾选它点「**立即重命名**」（只改名、不建链）。
2. **想彻底重建硬链接**：先删掉**目标目录**下对应文件夹，**再到「作品记录」里删掉该作品的账本记录**，然后跑一次普通执行。**绝不要删源目录下的文件**。
3. 只改了**体积**（增删文件）⇒ 不用管：体积不一致会自动识别为「需要重做」。
4. ⚠️ **不要**用单作品 `force=true`（只有接口能发）来「补文件」：建链用源目录里的**原名**，目标里已改好名的同一 inode 会被**再链一份**（同一 inode 两个目录项 + ` (2)` 后缀），详见 [`KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-9。它照样会打出一批 `File exists`：「目标就是同一 inode 硬链接」的那些会被改判为**幂等跳过**（`already_linked`，任务不算 `failed`），只有同名而 inode 不同才算 `link_failed`。

---
## 17. 配置文件被外部改坏后如何恢复

**【现象】** 页面顶部红字/横幅；`GET /api/config` 的 `errors` 非空；某个库的规则「自己变了」；同步线程在日志里报解析失败。

**【原因】** `/data/config/`（`libraries.yaml` + `processors/*.yaml`）与数据库是**双向同步**的（默认 5 秒一轮）。用外部编辑器改坏 YAML ⇒ **导入被拒、数据库保持原值**，错误累积在 `errors` 里；反过来，通过 WebUI/API 保存会在**同一请求内原子覆盖**文件（`*.yaml.tmp` → `os.replace`），所以「正在编辑器里改，文件被覆盖了」也是正常现象。完整规则见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §2.6。

**【诊断命令】**

**【浏览器】** 系统信息页 / `GET /api/config` 看 `errors[]`（会指出哪个文件、哪个字段或行）。**【NAS 宿主】**

```bash
ls -l /data/config/ /data/config/processors/
docker compose exec media-ingest head -n 30 /data/config/processors/bangumi.yaml
```

字段级报错的对照表见 [`USER_GUIDE.md`](USER_GUIDE.md) §5.8。

**【解决步骤】**

1. 按 `errors[]` 指的位置改回**合法 YAML**（缩进、`: ` 后要有空格、字符串里的 `{`/`}` 要引号）。改好后 5 秒内自动重新导入，`errors` 清空。
2. 不知道原来是什么样：从 `/data` 的备份里取同一文件（[`DEPLOYMENT.md`](DEPLOYMENT.md) §7）。
3. **不要**在「文件有错」的状态下反复保存同一份规则 —— 数据库侧的值是好的，继续手改文件只会让两边对不上。
4. 改完确认：`errors` 为空 + 处理器编辑页能打开 + 随便点一次「校验」。

---

## 18. 处理器被删除了但媒体库还在引用

**【现象】** 媒体库列表里该库红字「规则已删除」；任务一开跑就失败，`error` 里是「处理器 xxx 不存在（可能已被删除）。」

**【原因】** `libraries.processor_id` 指向的处理器已经不在 `processors` 表里（被手工删、被导入的配置覆盖、或 `processors/*.yaml` 被删掉后同步删除）。

**【诊断命令】** **【浏览器】** 「处理器」页看现有列表；`GET /api/libraries` 里该库的 `processor_id` 与 `processor` 字段。**【NAS 宿主】** `ls -l /data/config/processors/`。

**【解决步骤】**

1. **重建同 id 的处理器**：处理器页新建一个，`id` 填回原来的值（`GET /api/libraries` 里能查到）。媒体库不需要改。
2. 或者**把库改到别的处理器**：库编辑页选一个存在的处理器并保存。
3. 内置的三个（`hardlink_only` / `bangumi` / `movie`）删了可以用「恢复默认」重建（`POST /api/processors/{id}/reset`，会覆盖对它的自定义）。
4. **删处理器不会动已经建好的硬链接，也不会把已改的名字改回去** —— 它只影响之后的任务。

---

## 19. 日志文件名不合法（422）

**【现象】** 保存媒体库返回 422，`detail` 是数组，`loc` 里是 `["body","log_file"]`。

**【原因】** `log_file` 有三条硬规则（下面引的是错误原文，`{raw}` 处是你填的原值）：

- 「日志文件名只能是文件名，不能包含路径分隔符：`{raw}`。所有日志都固定在数据卷的《`<data>/logs`》目录下。」
- 「日志文件名必须以 `.log` 结尾：`{raw}`（例如 `bangumicache.log`）。」
- 「日志文件名 `xxx.log` 已被其它媒体库或处理器占用；请换一个名字，或留空由系统自动分配（下一个可用名：`bangumicache-2.log`）。」

留空时按处理器基名自动分配：第一个 bangumi 库拿 `bangumicache.log`，第二个拿 `bangumicache-2.log`（按库 id 升序；已有非空名的库原样保留）。

**【解决步骤】**

1. 填 `xxxcache.log` 这种纯文件名即可（不带 `/data/logs/` 前缀）。
2. 或者**留空**：服务端会按处理器基名分配一个不撞名的名字。
3. 改完保存前确认没有前后空格。

---

## 20. 特典目录被跳过时如何用 `legacy` 换回最朴素的旧口径

**【现象】** `NCOP`/`NCED`/`Preview`/扫图之类目录整棵没被处理，或反过来：你**想**处理它们却一直被跳过。

**【原因】** 两道闸门，都默认开着：① 全局设置项 `extras_patterns`（特典文件排除，见场景 25）；② 处理器 `traverse.reject_dirs`（`default` 预设里番剧/电影各有一套）。

**【解决步骤】**（**会改变处理范围，先备份库结构**）

1. 设置页把「特典文件排除」`extras_patterns` **清空**（空数组 = 关闭）。
2. 若仍被跳过，把该处理器换成 **`legacy` 预设**（`POST /api/processors/{id}/reset {"preset":"legacy"}`，或在编辑页选「恢复默认 → legacy」）：它把 `reject_dirs` 也清空。
3. 代价（[`PROCESSORS.md`](../contracts/PROCESSORS.md) §3.2）：`legacy` 的两条集数规则很宽松，会把 `1920x1080` 这类认成集号，且 `episode_fallback: "None"` 会把认不出集数的文件命名成 `…S01ENone.mkv`。
4. 只想放行**某几个**目录 ⇒ 别关全局，改 `reject_dirs`（去掉对应条目）更精确。

---

## 21. `same_filesystem` 说可以，真跑却全部失败（预检假绿）

**【现象】** 「校验」显示 `same_filesystem: true`、`probe.ok: true`，一点「立即运行」却整批 `EXDEV`（场景 1）。

**【原因】** 预检只是**探针**，不是全量证明：

- **【浏览器】「校验」= `POST /api/libraries/{id}/validate?probe=true`，只对"这一对目录"采样**：从该库 `source_dir` 有界递归（深度 ≤3、最多 512 个条目、不跟随符号链接）取一个已存在的普通文件，`os.link` 到 `target_dir` 下的临时名 `.mlm_probe_*` 再立刻删掉 —— 源目录一个字节都不写。
- 所以它能证明的是「这个样本、这两个目录能链」，**不能**证明「每个库的每个源/目标组合都同挂载」；多库时尤其明显。
- 健康检查**没有**同挂载项（v2 起只保留 `db`/`data_dir`/`processors`/`config`）；`/api/system/info` 的 `same_filesystem` 是另一件事：它取**第一个媒体库**的 `source_dir` / `target_dir` 做只读概览（`probe=False`），跟其它库无关。
- 早期版本把「判不了」当成 `true` —— 这正是"假绿"的成因（`work/tests/test_hardlink_precheck.py` 钉的就是这条）。

三态含义：`true` = **已实证**；`false` = 必然失败；`null` = **无法实证，不是通过**。

**【诊断命令】** 别信预检，直接实证（与场景 1 同一条）：

```bash
findmnt -T <库的 source_dir> -o TARGET,SOURCE,FSTYPE
findmnt -T <库的 target_dir> -o TARGET,SOURCE,FSTYPE   # 两条 TARGET 必须是同一个挂载根
docker exec -it media-ingest sh -c 'f=$(find <source_dir> -maxdepth 3 -type f | head -n1); ln "$f" <target_dir>/.mlm_probe_test && echo LINK-OK && rm -f <target_dir>/.mlm_probe_test || echo LINK-FAILED'
```

**【解决步骤】**

1. 出现 `LINK-FAILED` 或两条 `TARGET` 不同 ⇒ 就是真跨挂载，按场景 1 改 compose（不是"重试就好"）。
2. 预检 `null` 时不要当成通过：补一个真实存在的源目录再校验，或用上面的 `ln` 自证。
3. 修好挂载后 `docker compose up -d`（不是 `restart`），重新校验应得 `true` + 真跑成功。

---

## 22. 升级到单挂载形态后容器 `unhealthy`（旧的允许根目录还在）

> 📌 **v2 已彻底删除这套机制**：`allowed_source_roots` / `allowed_target_roots` 两个设置项连同健康检查里的
> `downloads_readable` / `library_writable` 一起从 DB、API、WebUI 中删掉，启动迁移会把老库里的这两个键清掉。
> 所以**升级到 v2 后不会再出现本条描述的现象**；保留此条只为解释旧版本的事故与迁移影响。

**【现象（仅旧版本）】** 从「`/downloads:ro` + `/library`」改成单挂载后容器一直 `unhealthy`；系统信息 `mounts[]` 里仍出现 `/downloads`、`/library`；或旧版健康检查 `downloads_readable: false` 而目录明明存在。

**【原因（仅旧版本）】** 这两个设置项持久化在数据库里，不跟随 compose 改动 —— 老实例里存的是旧容器路径，健康检查只探白名单第一个根。

**升级到 v2 后要做的检查**

1. 升级后 `/api/system/health` 的 `checks` 只有四项（`db`/`data_dir`/`processors`/`config`），不再有那两个根目录检查项。
2. 若 `mounts[]` 里仍出现 `/downloads`、`/library`，那是**媒体库记录**里的 `source_dir` / `target_dir`：到「媒体库」编辑该库，改成宿主真实路径（容器路径 = 宿主路径），保存时会自动校验同挂载。
3. 改完跑一次该库的「校验」确认 `same_filesystem: true`（或实证 `probe.ok: true`）；完整清单见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §6.5。

---

## 23. 库级覆盖 / 命名模板保存报 422

**【现象】** 保存媒体库返回 422，`loc` 以 `rule_override.` 开头；或提示「未知变量」。

**【原因】** 两处校验：

1. **覆盖只允许白名单字段**（`naming` / `traverse` / `extensions` / `episode_patterns` / `episode_fallback` 等，见 `app/processors/overrides.py` 的 `OVERRIDE_KEYS`）。文案：「本库规则覆盖必须是一个对象。」「未知字段：`x`（只允许 …）。」
2. **模板/段里的变量名必须在"合并覆盖之后"的可用名单里** —— 名单 = 12 个模板变量 + 引擎内键 `episode_resolved` + **该处理器 `naming.expressions` 里定义的名字**。文案：「模板里用了未知变量 `{x}`。可用变量：…。」「段里用了未知变量 `{x}`。可用变量：…。」

**【解决步骤】**

1. 变量从编辑页的按钮插入，别手打；`episode_resolved` 不在按钮里但可以直接写（模板与段都认）。
2. 想用自定义变量：先在 `naming.expressions` 里**定义**它，再在段里引用（执行期是 Python 表达式，代价见 [`PROCESSORS.md`](../contracts/PROCESSORS.md) §4）。
3. 覆盖只写你要改的键；删掉某个键表示"沿用处理器原值"，不是"清空"。

---

### 23.1 改了后缀白名单却没生效

**先分清改的是哪一个**：

| 名字 | 在哪 | 管什么 |
|---|---|---|
| `rename_extensions` | 设置页「允许重命名的文件后缀」 | **改名**阶段允许的后缀（全局） |
| `extensions` | 处理器 spec（`processors/*.yaml`） | **遍历/建链**阶段认哪些后缀（每处理器一套） |

**为什么改了没反应**：① 只改了一个，另一个仍挡着；② 该库用的是**另一个处理器**（库级 `rule_override.extensions` 也可能在覆盖）；③ 后缀匹配**大小写敏感**（`.MKV` ≠ `.mkv`）；④ 已经改好名的文件不会因为后缀变化被"改回去"。

**自查**：`GET /api/processors` 看该库处理器的 `extensions`；`GET /api/libraries` 看该库有没有 `rule_override.extensions`；设置页确认 `rename_extensions` 已保存。

---

## 24. 库日志里混进了应用日志、会不会无限增长

**结论两句话**：**不会混**（用日志 filter 白名单实现，见 `app/script_adapter.py` 的 `_BookkeepingFilter`）；**会一直长、且应用不会自己清理**（只过滤、不清理、不轮转）。

- `<data>/logs/hard_links.log` 只接收**两类**行：`Source: … - Folder: … - Folder size: …`（必须同时含 ` - Folder: ` 与 ` - Folder size: `）与 `Deleted orphaned hard link folder: …`。uvicorn 访问日志、任务日志、异常回溯在这条 handler 上被丢弃。
- 每个媒体库另有自己的重命名日志（JSON，一行一个作品）——同样只写自己的条目，见 [`DEPLOYMENT.md`](DEPLOYMENT.md) §2.6。
- 任务日志与任务历史的**上限**（`max_task_log_lines` 默认 5000、`max_history` 默认 2000）与这些文件**无关**：它们管的是任务页展示的内容。`max_history` 裁剪旧任务时会**连带删掉那些任务的任务日志**（库里/磁盘上的日志文件照样保留）。

**如果你确实看到"混进来了"**：先确认没把三个东西搞混 —— `hard_links.log`（建链台账）、`<log_file>`（该库的重命名缓存）、`docker compose logs`（应用 stdout）。三个都在看板上，长得不一样。

**想瘦身**：停容器 → 备份 → `mv` 旧文件 → 重启（应用会重建空文件）。这会丢掉审计历史行（不影响"跳过"判定，判据是 `link_records` 账本），风险自负。

---

## 25. 文件没被改名、停在原地（L3 特典排除）

**【现象】** 某个文件既没改名也没移动；`result.extras_classified > 0`；甚至正片被当成特典跳过了。

**【原因】** 全局设置项 `extras_patterns`（**特典文件排除**，默认 7 条）对**含扩展名的文件名**做**大小写不敏感的正则搜索**（是"搜索"，不是"全匹配"），只作用当前作用域里的**直接文件**。命中 ⇒ **不改名、不移动、不建 `其他/`**，只计数。它**对所有库、所有处理器生效**（`legacy` 也不例外），属于独立于预设的全局能力。

**【诊断命令】** **【浏览器】** 任务详情 → `result.samples.extras_classified[]`，每条是 `{dir, name, size, pattern}` —— `pattern` 就是命中的那条规则。

**【解决步骤】**

1. 确认是**误伤**还是**本来就想排除**：看 `pattern` 命中的是哪条。
2. 精度不够 ⇒ 把该条改成正则锚定的形式（例如 `^NCOP`），或直接删掉那一条。
3. 想整体关闭 ⇒ 设置页把 `extras_patterns` 清空成 `[]`（非法正则会当场报校验错误）。
4. 这是**冻结行为**（[`BASELINE.md`](../contracts/BASELINE.md) §5 的 L3 相关行）：应用不会因为「看起来像正片」就绕过它。

---

## 27. 「作品记录」里显示「已不在」/「未记录」怎么办

两本账对不上时，这一列就是在告诉你**哪一边缺**：

| 显示 | 含义 | 常见原因 |
|---|---|---|
| **已不在** | 账本（`link_records`）里有这个作品，但目标目录里找不到它的文件夹 | 手工删过目标目录、孤儿清理删过、改过目标路径 |
| **未记录** | 目标里有文件夹，但账本里没有对应记录 | 手工拷进去的、老的日志时代留下的、换过后端路径 |

**【解决步骤】**

1. 先判断"你希望它算不算数"：想保留 ⇒ 重跑该库让它重新登记；不想要 ⇒ 在「作品记录」里删掉记录（会一并删目标里的作品文件夹，**源目录不动**）。
2. **不要手工改数据库**。
3. 整库报错、且错误提到「目标目录不存在」时：那是目标被手工删了而账本还在（[`KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-12），先删该作品的记录或恢复目录。
4. 这一页的字段说明见 [`USER_GUIDE.md`](USER_GUIDE.md) §6.5。

---

## 28. 删了作品记录，下次运行还是没重做

**【原因】** 按可能性排序：

1. **删错了地方** —— 「作品记录」页删的是账本行；重命名缓存（每库的 JSON 日志）里还有同名条目，但那只影响重命名，不影响建链。
2. **目标里那份还在**：重建时目标已存在同名硬链接 ⇒ 判为 `already_linked`（幂等跳过、不算失败），所以你看到"没重做"。
3. **源目录里已经没有那个文件夹** ⇒ 不是"没重做"，而是没得做（此时 `orphan_cleanup` 开着还会把目标里那份清掉，见场景 6）。
4. **账本行没删干净**：同一作品在不同 `source_dir`/`target_dir` 下各有一行，你删的是另一行的。

**【解决步骤】** 按场景 16 第 2 条的两步来：**先删目标目录下对应文件夹** → **再删「作品记录」里的账本行** → 跑一次普通执行。只想重排名字就用「立即重命名」，不用建链。

---

## 29. 删除后目标里出现 ` (2)` 后缀文件

**【现象】** 目标目录里多出 `Show A S01E01 (2).mkv` 这类文件；或升级后第一次全量运行冒出一批。

**【原因】** 这是**防丢**（L4）而不是 bug：目标名被占用时按体积判定，最大者拿干净名、其余加 ` (2)`/` (3)`（`renamed_with_suffix`），原有同名且更小的改名**让位**（`displaced_targets`，不删）。触发场景：① 同一作品有多个压制版本/无集数文件，算出了同一个目标名；② 升级后首跑把"已改名"的作品按源原名**再链一份**（[`KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-7）。

**【诊断命令】**

```bash
ls -li <target_dir>/<作品>/            # 看 inode 与链接数
```

- **同一个 inode**（`ls -li` 第一列相同）⇒ 同一份数据和两个名字，删掉多余的那个名字**不丢数据**、不释放空间。
- **不同 inode** ⇒ 两份独立文件，删之前确认哪份要留（大的一般是"干净名"那份）。

**【解决步骤】** 确认是哪种之后按上面处理；预防见场景 15（让文件名带集数）；升级首跑的行为见 KI-7。

---

## 31. 文件名少了一段（命名里用了不存在的变量名）

**【现象】** 任务 `success`，但名字缺一截（例如本来该有 `[字幕组]` 的那段没了）；`result.unknown_params > 0`；任务日志里是「命名里的未知变量被留空（文件名少了一段）：…」。

**【原因】** 模板或段里引用了**不存在的变量名**。**运行期不因此拒绝整份规格**（为了让老 YAML 与手改过的配置照旧跑完）：那一整段被留空、计数并汇总进 `warnings[]`，其余段照常渲染。**保存路径是严格的** —— WebUI/API 保存同一种写法会 422（文案见场景 23）。

**【诊断命令】** `result.unknown_params` 与 `result.samples.unknown_params[]`（每个文件 + 用错的变量名）；日志里搜「未知变量」。

**【解决步骤】**

1. 从编辑页按钮插入变量；对照可用名单：12 个模板变量 + 引擎内置键（`ENGINE_ONLY_VARS`，当前只有 `episode_resolved`）+ 该处理器 `naming.expressions` 里**定义过**的名字。`episode_resolved` 不在按钮里，但模板与段都能直接写。
2. 自定义表达式名必须**先定义再用**（[`PROCESSORS.md`](../contracts/PROCESSORS.md) §4）。
3. 想彻底不出现"少一段"，就把它删掉而不是留一个拼错的名字。

---

## 还是没解决？

1. 先看 [`FAQ.md`](FAQ.md) —— 「没有具体报错、只是想确认某个行为」的问题在那里。
2. 「这是已知问题吗」→ [`KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md)（KI-1、KI-7 ~ KI-12）。
3. 「这个行为能不能改」→ [`BASELINE.md`](../contracts/BASELINE.md) §5（L1–L9 冻结限制）与 [`COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) §3。这些是**记录、不修正**的边界。
4. 报 issue 时请附：`GET /api/system/health`、`GET /api/system/info` 的 `mounts[]` 与 `same_filesystem`、出问题的**任务 id**、以及 `result` 里对应的 `samples` 片段（去掉路径里的敏感部分）。

