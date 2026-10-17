# 故障排查（TROUBLESHOOTING）

> 这篇文档回答的是「**页面上出现了某个具体报错，我该怎么办**」。
> 想知道「为什么是这个行为、哪些不能改」→ [`docs/contracts/BASELINE.md`](../contracts/BASELINE.md)、[`docs/contracts/COMPATIBILITY.md`](../contracts/COMPATIBILITY.md)；
> 想知道「网页怎么用」→ [`docs/guides/USER_GUIDE.md`](USER_GUIDE.md)；想知道「怎么装」→ [`docs/guides/DEPLOYMENT.md`](DEPLOYMENT.md)；
> 「概念性问题」（硬链接会不会占双倍空间、能不能删原始下载…）→ [`docs/guides/FAQ.md`](FAQ.md)。

**标注约定**：每条场景的诊断命令都标了执行位置。

| 标注 | 含义 |
|---|---|
| **【NAS 宿主】** | 在 NAS 的 SSH 里、项目目录（默认 `/vol1/1000/Docker/media-ingest`）下执行 |
| **【容器内】** | 在容器里执行；用 `docker exec -it media-ingest <命令>` 从宿主进容器 |
| **【浏览器】** | 打开 `http://<NAS_IP>:18081`，在页面上操作 |

后文默认：容器名/服务名 = `media-ingest`，端口 = `18081`，数据卷 = `/vol1/1000/Docker/media-ingest/data`（容器内 `/data`）。

---

## 0. 五分钟通用诊断（任何问题都先做这三步）

**第一步：看系统信息页。**

**【浏览器】** 打开「系统信息」页（或 `GET /api/system/info`）。重点看四块：

| 看什么 | 正常应该是 | 不对时应看 |
|---|---|---|
| `mounts[]` | `MEDIA_ROOT`（**容器路径 = 宿主真实路径**，一条 bind，可写）、`/data`、`/data/config`；**没有**单独的 `/downloads` / `/library` 挂载。**条目数随设置变化**：固定项 `/data`、`/data/config` 加 `allowed_source_roots` / `allowed_target_roots` 两个白名单设置项各自的**最近已存在祖先**（所以别按"恰好几条"核对）。`/data/config` 是 `/data` 的子目录，通常 `device`/`fs_type` 为空字符串 | 场景 1、3、22 |
| `same_filesystem`（三态） | `true` = **已实证**能在容器内建硬链接；`false` = 跨挂载，必然失败；`null` = **无法实证，不是通过** | 场景 1、21 |
| `processors` / `config` | `processors.total` 为 3、`builtin_ids` 为 `["hardlink_only","bangumi","movie"]`；`config.files[]` 的 `in_sync` 为 `true` | 场景 2 |

**第二步：看健康检查。**

**【NAS 宿主】**

```bash
curl -s http://127.0.0.1:18081/api/system/health
```

返回 `{"status":"ok","checks":{"db":true,"data_dir":true,"downloads_readable":true,"library_writable":true,"processors":true,"config":true},...}`
—— **六项**里是 `false` 的那一项，`details` 里有对应的中文原因，直接对号入座（`processors:false` → 场景 2；`config:false` → 数据卷不可写，见场景 7；与源/目标可访问性相关的两项（`downloads_readable` / `library_writable`）为 `false` → 场景 1、2、3、22；`db:false` / `data_dir:false` → 场景 7）。
`status` 为 `"degraded"` 表示**能启动、但有检查项不通过**，此时任务多半会失败。

**第三步：看任务的原始输出。**

**【浏览器】** 「任务」→ 点开失败的任务 → 「日志」。应用日志与建链阶段的 stdout 都在这里（`stream` 为 `stdout` 的是原生实现 `app/processors/hardlink.py` 自己打印的，最接近真相）。

**【NAS 宿主】** 直接看文件（建链阶段的输出同时落在应用日志里）：

```bash
tail -n 80 /vol1/1000/Docker/media-ingest/data/logs/hard_links.log
docker compose logs --tail=100 media-ingest
```

> ⚠️ **任务失败时，先看 `result` 里的计数再看 `error`。** 建链阶段「单个文件失败不中断」，所以一个任务可以**部分成功**：
> `linked > 0` 且 `link_failed > 0` 是**正常的部分成功**（状态会判为 `failed`，但已经建好的硬链接不会回滚，也不会损坏数据）。
> 计数含义：`linked`（新建硬链接）/`skipped`（体积一致而跳过的一级文件夹数 **+ `already_linked`**）/`link_failed`/`already_linked`（目标已存在且**就是源文件的硬链接**，按幂等跳过处理，不算失败）/`renamed`/`rename_failed`/`orphans_removed`/`folders_scanned`/`bytes_linked`。

---

## 快速索引

| 现象关键词 | 场景 |
|---|---|
| `跨文件系统错误` / `EXDEV` / `Invalid cross-device link` | [1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link) |
| 启动即 `degraded` / 健康检查里有 `false` 项 | [2](#2-启动即-degraded健康检查六项里有-false) |
| 源目录可写 / 想确认应用没动源文件 / `source_integrity` | [3](#3-源目录是可写的怎么确认应用没有动你的源文件) |
| 系统信息里没有任何已处理记录 / 第一次就全量重扫 | [4](#4-第一次使用就全量重扫账本从空开始) |
| 每次都满屏 `创建硬链接失败: … File exists`（日志里 `Source:` 是宿主路径） | [5](#5-每次运行都满屏创建硬链接失败--file-exists并且重复处理同一批文件夹) |
| 目标目录里的文件夹被删了 / `Deleted orphaned hard link folder` | [6](#6-目标目录被自动删除了孤儿清理) |
| `Permission denied` / `Read-only file system` / 建不了目标目录 | [7](#7-权限类报错permission-denied--read-only-file-system) |
| qBittorrent 下载完了但没起任务 | [8](#8-qbittorrent-下载完成后回调没有触发任务) |
| 回调返回 `401` | [9](#9-回调返回-401hook-token-不匹配) |
| 页面打不开 / 端口被占用 | [10](#10-端口-18081-被占用或页面打不开) |
| 忘记密码 / 所有人被登出 | [11](#11-忘记管理员密码--所有人都被登出) |
| ARM64 / aarch64 NAS 上构建或运行异常 | [12](#12-arm64aarch64nas-上的构建与运行) |
| 容器重启后任务停住了 | [13](#13-容器重启后的队列状态) |
| 任务一直 `queued` 不动 | [14](#14-任务长时间停在-queued) |
| 文件没改名 / 名字里有 `ENone` / 集数认错或认不出（`1920x1080`、`Show.E07.mkv`）/ 日志里 `Kept (no episode):` | [15](#15-文件没有被改名--名字里出现-enone) |
| 源文件被改了 / 改了名字但任务说"跳过" | [16](#16-改了源文件但任务仍然跳过) |
| 配置文件被外部改坏 / 红色横幅 / `errors` 非空 | [17](#17-配置文件被外部改坏后如何恢复) |
| 库列表红字「规则已删除」/ 处理器不见了 | [18](#18-处理器被删除了但媒体库还在引用) |
| 保存媒体库报 422 / 日志文件名不合法 | [19](#19-日志文件名不合法422) |
| 特典/扫图目录整棵被跳过 / 想换成最朴素的旧口径 | [20](#20-特典目录被跳过时如何用-legacy-换回最朴素的旧口径) |
| 校验说 `same_filesystem: true` 但真跑全失败 / 探针假绿 / `null` | [21](#21-same_filesystem-说可以真跑却全部失败预检假绿) |
| 换成单挂载后容器 `unhealthy` / 设置页根目录还是 `/downloads`、`/library` | [22](#22-升级到单挂载形态后容器-unhealthy旧的允许根目录还在) |
| 保存媒体库报 422、字段以 `rule_override.` 开头 / 模板说「未知变量」 | [23](#23-库级覆盖--命名模板保存报-422) |
| 改了后缀白名单没生效 / 某个库的后缀和别的库不一样 | [23.1](#231-改了后缀白名单却没生效) |
| 库日志里混着访问日志/任务日志 / 日志会不会无限增长 | [24](#24-库日志文件里混进了应用日志会不会无限增长) |
| 文件没改名、停在原地 / `extras_classified` 大于 0 / 正片被当特典 | [25](#25-文件没被改名停在原地l3-特典排除) |
| 待办打钩后那一行不见了 / 一键检测说没问题 / 看板只显示 20 条 | [26](#26-待办看板分页行首圆圈一键检测) |
| 「作品记录」里显示「已不在」/「未记录」/ 两本账对不上（`rename_missing`、`rename_only`） | [27](#27-作品记录里显示已不在未记录怎么办) |
| 删了作品记录，下次运行还是没重做 / 日志里 `Skipping folder: … (already processed at …)`、`文件夹已记录且体积一致，跳过: …` | [28](#28-删了作品记录下次运行还是没重做) |
| 删除后目标里多出 ` (2)` 后缀文件 / 首次全量运行后多出重复文件名 | [29](#29-删除后目标里出现--2-后缀文件) |
| 「作品记录」页搜不到某个作品 / 搜到的条数和印象不符 / 输入 `%`、`_` 却当通配符用 | [30](#30-作品记录页搜不到某个作品) |
| 文件名**少了一段** / 任务成功但名字缺一截 / `unknown_params` / 「未知变量被留空」/ 保存命名报 422 说未知变量 | [31](#31-文件名少了一段命名里用了不存在的变量名) |

---

## 1. 任务全部 `link_failed`、错误里有「跨文件系统错误」/ `[Errno 18] Invalid cross-device link`

> 本场景是**最可能踩到的一个**：Linux 的 `linkat(2)` **只允许同一个挂载内的硬链接**。
> 如果你的 compose 用的是「`/downloads:ro` + `/library` 两条独立 bind」，硬链接**永远建不出来**。

**【现象】**

- 任务 `status: failed`，每个文件都是：`创建硬链接失败: /library/… - [Errno 18] Invalid cross-device link`（Windows 上是 `[WinError 17]`，语义相同）。
- `result` 里 `linked: 0`、`link_failed` 等于文件数；**库目录只落了一半结构**（有些文件夹建出来了、有些完全没有）。
- 系统信息页 `same_filesystem` 为 `false`；也可能报 `true`（**假绿**，见[场景 21](#21-same_filesystem-说可以真跑却全部失败预检假绿)）。

**【原因】** 硬链接的前提不是"同一块盘"，而是**同一个挂载（mount）**。内核 `do_linkat()` 在创建硬链接前直接比较源与目标的挂载身份（`old_path.mnt != new_path.mnt`）⇒ **不同挂载一律 `EXDEV`**，与 `st_dev`、`fsid`、btrfs 子卷、ZFS dataset 是否相同**无关**。这就是"`stat -c %d` 两个数字一样、`link()` 照样失败"的原因：它们是同一块盘，但被 compose 挂成了**两条独立的 bind 挂载**。

宿主上的 `/vol1/1000/Download/qbittorrent` 与 `/vol1/1000/Library` 同在 `/vol1` 这一个挂载里 —— 所以**在宿主上直接建硬链接能成功**，而在容器里（两条独立 bind）必然 `EXDEV`。

**正确形态（单挂载形态：单一共同父挂载、容器路径 = 宿主路径）**：

```yaml
services:
  media-ingest:
    volumes:
      - "${MEDIA_ROOT:-/vol1/1000}:${MEDIA_ROOT:-/vol1/1000}"   # 源与目标的共同父目录，一条 bind
      - "./data:/data"
```

`MEDIA_ROOT` 取**源与目标的共同父目录**（通常 `/vol1/1000`）。这样两个目录在容器内仍位于同一个挂载内、`link()` 可用；同时容器路径与宿主一致，`HOST_PATH_MAP` 变成恒等，日志与 WebUI 里直接显示宿主真实路径。

**不要把源目录再单独拆一条 `:ro` bind** —— 那会立刻退回本场景（`EXDEV`）。源目录的保护改由代码层与运行期核对提供，见[场景 3](#3-源目录是可写的怎么确认应用没有动你的源文件)。

**不会静默复制冒充硬链接**（B8/A9）：现象是"库目录只落一半结构"，**不是**"多出一份副本"、也不是数据被改坏。已经建好的硬链接不回滚，重跑时体积一致的文件夹会被跳过。

**【诊断命令】**

**【NAS 宿主】** 这一条就能定论 —— 源与目标是不是**同一个挂载根**：

```bash
findmnt -T /vol1/1000/Download/qbittorrent -o TARGET,SOURCE,FSTYPE,OPTIONS
findmnt -T /vol1/1000/Library              -o TARGET,SOURCE,FSTYPE,OPTIONS
# 两条的 TARGET 必须是同一个挂载根（例如都是 /vol1 或 /）。不同 ⇒ 必然 EXDEV。
```

看容器实际挂了几条：

```bash
docker inspect media-ingest --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} rw={{.RW}}{{"\n"}}{{end}}'
# 正确形态：一条 MEDIA_ROOT（容器路径 = 宿主路径）+ /data。若还能看到 /downloads 与 /library 两条 ⇒ 就是本场景。
```

**【容器内】** 只读安全的实证（拿一个**已存在**的源文件直接链到目标根，不写源目录、探测完立刻删）：

```bash
docker exec -it media-ingest sh -c \
  'f=$(find /vol1/1000/Download/qbittorrent -maxdepth 3 -type f | head -n1); \
   ln "$f" /vol1/1000/Library/.mlm_probe_test && echo LINK-OK && rm -f /vol1/1000/Library/.mlm_probe_test || echo LINK-FAILED'
```

**【浏览器】** 对出问题的媒体库点「校验」（`POST /api/libraries/{id}/validate?probe=true`），看 `same_filesystem` 与 `probe.note`。

**【解决步骤】**

1. 把 compose 的挂载改成上面的**单一共同父挂载 + `/data`**（删掉 `/downloads:ro` 与 `/library` 两条），**【NAS 宿主】** `docker compose up -d`（**不要**用 `docker compose restart`，它不重读 compose 文件）。
2. 宿主上确认两个目录同一个挂载（`findmnt -T` 的 `TARGET` 相同）；容器内确认上面那条 `ln` 输出 `LINK-OK`。
3. **【浏览器】** 点「校验」→ 应看到 `same_filesystem: true` 且 `probe.ok: true`；再点「立即运行」。
4. **已"部分成功"的任务不用清理**：已建好的硬链接是有效的，重跑时体积一致的文件夹会被跳过，只补做失败的部分。
5. 如果源与目标确实无法放进同一个挂载（例如下载盘与媒体库盘是两块物理盘），唯一合规的做法是**把其中一方搬到另一方的挂载里**；本项目**不提供**跨挂载复制模式（见 [`docs/guides/FAQ.md`](FAQ.md) 第 1、3 问）。

> 💡 **预检的 `probe.note` 是给人看的中文可执行文案，不要忽略它。** 当无法实证时它会明确说出原因与下一步（例如源目录下找不到任何可链的普通文件），此时 `same_filesystem` 是 `null` 而**不是** `true` —— 别把"无法实证"当成"通过"。

---

## 2. 启动即 `degraded`：健康检查六项里有 `false`

**【现象】** 容器起来了，但 `/api/system/health` 的 `status` 是 `"degraded"`（**能启动、但有检查项不通过**），页面顶部出现红字，或任务一开跑就失败。

**【先看是哪一项】** `checks` 固定**六项**，逐项对号入座：

| `false` 的项 | 含义 | 去哪 |
|---|---|---|
| `db` / `data_dir` / `config` | 数据卷不可写、数据库打不开或损坏、配置文件读不了 | [场景 7](#7-权限类报错permission-denied--read-only-file-system) |
| `downloads_readable` | 某个媒体库的 `source_dir` 不存在或不可读 | [场景 1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link)、场景 22 |
| `library_writable` | 某个媒体库的 `target_dir` 不存在或不可写 | [场景 7](#7-权限类报错permission-denied--read-only-file-system)、场景 22 |
| `processors` | 内置处理器缺失或损坏、处理器配置不合法 | 看 `GET /api/processors`、`GET /api/config` 的 `errors`（[场景 17](#17-配置文件被外部改坏后如何恢复)、[场景 18](#18-处理器被删除了但媒体库还在引用)） |

每一项在 `details` 里都带中文原因。

**【诊断命令】**

**【NAS 宿主】**

```bash
curl -s http://127.0.0.1:18081/api/system/health | python3 -m json.tool
curl -s http://127.0.0.1:18081/api/processors | python3 -m json.tool | head -n 40
```

**【镜像结构自检】** 六项检查里**没有**"外部依赖"这一项：镜像自带运行环境（宿主不需要装 Python 或别的工具），硬链接与重命名分别由 `app/processors/hardlink.py`、`app/processors/engine.py` 完成。所以 `degraded` **只可能**来自上表那六项的运行环境问题 —— 不需要去核对镜像里有没有别的东西。

**【解决步骤】**

1. `degraded` 本身**不代表数据坏了**：先按上表定位那一项；
2. 改过 `/data/config/` 下的文件后应用会重新读取；若 `processors:false` 一直不消失，检查内置处理器是不是被删了（[场景 18](#18-处理器被删除了但媒体库还在引用)）；
3. 源/目标目录的权限与挂载形态，见 [场景 1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link) 与 [场景 3](#3-源目录是可写的怎么确认应用没有动你的源文件)；
4. 修好后 **【NAS 宿主】** `docker compose up -d`（**不要**用 `docker compose restart`，它不重读 compose 文件），`status` 应回到 `ok`。

---

## 3. 源目录是可写的：怎么确认应用没有动你的源文件

> 源目录与目标同属**一条可写挂载**（B9）：单挂载形态下它不可能同时只读，原因见[场景 1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link)。

**【现象】**

- 系统信息页挂载表里**看不到单独的 `/downloads` 条目**了，只有一条 `MEDIA_ROOT`（容器路径 = 宿主真实路径，`read_only: false`）与 `/data`。
- 危险信号（**这才需要处理**）：源目录树里出现**非下载工具产生**的改动（源文件 `mtime` 变了、多了临时文件/半成品），或任务结果里 `source_integrity.ok` 为 `false`。

**【原因】** 内核让两件事**互斥**：硬链接要求源与目标在**同一个挂载**内（[场景 1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link)），而"源目录单独挂载且只读"意味着源是**另一条 bind** ⇒ 硬链接必然 `EXDEV`。于是"给源目录一条 `:ro` 挂载"这条**物理保证**与单挂载形态**无法同时成立**。

这不是"少了一道防线就算了"，而是防线形态换了：

| | 当前形态 | 判据 |
|---|---|---|
| 源目录保护 | 代码层不写源目录 + **任务前后逐文件源完整性核对** | **可验证的保证**：越界会被检出并告警 |
| 谁保证 | 本应用 | `Task.result.source_integrity` + 测试覆盖（`tests/test_no_source_mutation.py`） |
| 能否被绕过 | 理论上能（进程有写权限） | 但**每一次实际改动都会在任务结果里留下 `changed` 路径与告警** |

**【诊断命令】**
不要用 `touch /downloads/...` 这种方式取证（源目录本来就可写，探不出"应用有没有乱写"）。要判"应用有没有动源文件"，看**任务结果里的核对结论**：

**【浏览器】** 打开出问题的任务详情，看 `result`：

- `result.source_integrity.ok` 应为 `true`。
- 若为 `false`：`result.source_integrity.changed[]` 列出了**被动过**的源文件路径；`result.warnings[]` 里有中文告警。**这属于缺陷，请附任务 id 与这两段原文报 issue。**
- 正常任务**不应**出现该告警。硬链接本身会让 `nlink`/`ctime` 变化，**这不算**源被改动（核对口径刻意排除这两个字段）。

**【NAS 宿主】** 需要人工比对时，用文件系统时间戳看源树最近是否被写过：

```bash
find /vol1/1000/Download/qbittorrent -type f -newermt '-1 hour' -printf '%TY-%Tm-%Td %TH:%TM %p\n' | head -n 50
```

**【容器内】** 确认挂载形态（应当**没有** `/downloads`/`/library` 这两条独立挂载）：

```bash
docker inspect media-ingest --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} rw={{.RW}}{{"\n"}}{{end}}'
```

**【解决步骤】**

1. **正常情况什么都不要做**：看到 `MEDIA_ROOT` 是 `rw=true` 是**预期**的（目标目录要写）。只要 `source_integrity.ok` 一直是 `true`，源就在受保护状态。
2. **如果 `source_integrity.ok == false`**：先把任务报告里的 `changed` 路径与告警原文抓下来，**在重跑之前**检查这些文件是不是下载工具自己写的（例如 qBittorrent 正在做种、校验时改 `mtime`）。确认不是下载工具所为 ⇒ 报 issue（附任务 id、`changed`、可用的话加 `sha256sum`）。
3. **不要**为了让源只读而给它再拆一条 `:ro` bind —— 那会直接把硬链接打回[场景 1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link)。若你确实想要"源绝对不可写"，唯一合规做法是让源与目标**不共享父挂载**，那就**用不了硬链接**（本项目不提供复制模式），两者只能二选一。
4. 想自查代码侧纪律：**在装有 Python 的电脑上**跑 `python tests\run_all.py`，`test_no_source_mutation.py` 全套绿即表示"目标库之外没有任何写入点"这条纪律仍被测试钉住。

---

## 4. 第一次使用就全量重扫（账本从空开始）

**【现象】**

- 新接入的库（账本还是空的）**第一次运行就全量重扫**：`folders_scanned` 等于库里的文件夹数、`skipped` 为 `0`。
- **【NAS 宿主】** `<data>/logs/hard_links.log` 不存在，或存在但里面**没有本库源目录的记录**。
**【原因】** 建链阶段判定"这个一级文件夹我处理过"的**首选依据是每库自己的账本**（数据库表 `link_records`：本库 + 同 `source_dir` + 同 `target_dir` + 同体积）—— 执行器把这份账本当 `skip` 传给建链实现；账本从**空**开始，所以第一次接入必然全量扫描。于是：
- 跑过一次之后，账本里就有了这些作品的记录，第二次开始才会正常 `skipped`；
- `/data/logs/hard_links.log` 是**审计流水**（只有在没有库账本时才被当兜底判据读，见场景 5）：宿主上别处留下的同名文件**不会被继承** —— 应用只读写 `/data` 这条数据卷，也不会把别处的日志拷进来。

**重做本身是安全的**（不覆盖、不丢数据），只是会刷出一批 `File exists` 失败；这些"目标就是同一 inode 硬链接"的条目会被改判为**幂等跳过**（`already_linked`、计入 `skipped`），任务不会因为"全部已存在且 `linked == 0`"被判 `failed`。

**【诊断命令】**

**【NAS 宿主】**

```bash
docker compose exec media-ingest ls -l /data/logs/
docker compose exec media-ingest wc -l /data/logs/hard_links.log
docker compose exec media-ingest grep -o 'Source: [^ ]*' /data/logs/hard_links.log | sort -u
```

`grep` 那行的输出应当与媒体库的 `source_dir` **逐字相同**（单挂载形态下就是宿主真实路径，如 `/vol1/1000/Download/qbittorrent/Bangumi`）。若出现 `/downloads/...` 这种**容器内已不存在的旧路径** ⇒ 见场景 5。

**【浏览器】** 「系统信息」→ `mounts[]` 确认源目录所在的那条 `MEDIA_ROOT` 挂载 `exists: true`（单挂载形态下源与目标在**同一条可写挂载**里，见场景 3）；「任务」详情看 `result.folders_scanned` / `result.skipped`。

**【解决步骤】**

1. **第一次全量重扫是预期的**：跑完就写下了本库的记录，之后才有 `skipped`。
2. **别指望把宿主上别处的旧日志接进来**：应用只读写 `/data` 这条数据卷。要么让它全量跑一遍，要么先按场景 5 的办法处理旧日志；**不要**手工改日志里的体积数值（见场景 16）。
3. 数据卷里的 `hard_links.log` 被删掉/损坏 ⇒ 等价于"回到第一次"，全量重扫一次即恢复。
4. 日志位置固定为 `<data>/logs/hard_links.log`（即 `DATA_DIR` 下的 `logs/` 子目录，见 `app/config.py:153`）；**这份日志由原生硬链接实现自己读写**（`app/processors/hardlink.py`），应用只负责保证文件与目录存在。这个文件**只写两类记账行**（体积登记 + 孤儿目录清理），其余应用日志不混入、历史行也**不清理** —— 见场景 24。

> 💡 `HOST_PATH_MAP` 只用于**展示与路径换算**（系统信息页把容器路径显示成宿主路径、反向换算请求里的路径），**不参与**日志匹配 —— 改了它不会影响"跳过"判定。

---

## 5. 每次运行都满屏「创建硬链接失败: … File exists」，并且重复处理同一批文件夹

**【现象】**

- 每个任务都在刷：`创建硬链接失败: <目标目录>/… - [Errno 17] File exists`（Windows 上是 `[WinError 183]`）。
- `skipped` 恒为 `0`，`folders_scanned` 与库里的文件夹数一样多 —— **每次都是全量重扫**。
- 数据卷日志里那些记录的 `Source:` 与媒体库当前的 `source_dir` **不一致**（典型：容器内已不存在的 `/downloads/...`，或你换过源目录之后的旧路径）—— 这只是**这份日志的来历**，**不是**每次全量重扫的原因（原因见下）。

**【原因】** 现行建链阶段（原生实现 `app/processors/hardlink.py`）判定"这个文件夹我处理过"的**首选依据是每库作品账本**（数据库表 `link_records`，判据 = 本库 + 同 `source_dir` + 同 `target_dir` + 同体积，见[场景 27](#27-作品记录里显示已不在未记录怎么办)）—— 执行器把这份账本当 `skip` 传进建链实现（`app/script_adapter.py:599`）。**只有在没有库账本时**（`skip is None`）才退回另一判据：读 `<data>/logs/hard_links.log`、按**日志里的 `Source:` 字段与传进来的源目录字符串完全相等**来认（`app/processors/hardlink.py:329`）。所以**日志里的 `Source:` 与当前 `source_dir` 对不上，并不会导致重链**；那份日志只当审计记录，应用**不会**改写它，也没有"把 `Source:` 前缀本地化"这类机制。
**什么情况下才会这样**：这一批作品**不在账本里**（账本从空开始，或这些作品的记录被删过，见[场景 27](#27-作品记录里显示已不在未记录怎么办)）⇒ 每轮都当新文件夹处理。硬链接建第二次必然 `File exists`（数据不受损，但等于白跑）。

> ⚠️ **"跳过"判定不看这份日志**：建链阶段的"已处理"判据在**每库自己的账本**（数据库表 `link_records`，判据 = 本库 + 同 `source_dir` + 同 `target_dir` + 同体积，见[场景 27](#27-作品记录里显示已不在未记录怎么办)）。`hard_links.log` **照写**，但**只当审计日志**。所以本文"删掉旧日志让它重建"的做法**不影响跳过判定**；这条场景只对**日志原文的阅读、孤儿清理（场景 6）与建链 stdout** 还有意义。真正要"重做某个作品"，走[场景 27](#27-作品记录里显示已不在未记录怎么办)。

**【诊断命令】**

**【NAS 宿主】**

```bash
docker compose exec media-ingest head -n 5 /data/logs/hard_links.log
docker compose exec media-ingest grep -o 'Source: [^ ]*' /data/logs/hard_links.log | sort -u
docker compose exec media-ingest grep -c 'Folder:' /data/logs/hard_links.log
```

日志里的 `Source:` 应当与媒体库的 `source_dir` **逐字相同**（这是**审计记录**的判据，**不是**"跳过"判据）。若出现 `/downloads/...` 之类**容器内已不存在**的路径 ⇒ 日志是旧部署留下的（本场景的"旧日志"部分）。
想看应用实际使用的源目录：媒体库详情里的 `source_dir`（或 `GET /api/libraries`）。

**【解决步骤】**

1. **删掉（或先改名留底）这份旧日志，让它重建一次**：
   **【NAS 宿主】** `mv /vol1/1000/Docker/media-ingest/data/logs/hard_links.log /vol1/1000/Docker/media-ingest/data/logs/hard_links.log.host-paths.bak`
   然后不需要重启：下一次任务开始时应用会自己建出空日志。
2. 再跑一次任务：**这些作品还不在账本里时**，它们都会走"新文件夹"分支，已存在的硬链接会报 `File exists`（**数据不受损**、不覆盖）。跑完日志里记的就是**当前的 `source_dir`** 了。
3. **"跳过"只看每库账本，与这份日志无关**：账本里已有（本库 + 同源 + 同目标 + 同体积）的作品会打印「文件夹已记录且体积一致，跳过」，**已经建好的硬链接不会被重建**；重建这份日志只是让审计记录与当前 `source_dir` 对齐。
4. `HOST_PATH_MAP` **不参与**日志匹配 —— 它只用于展示与路径换算（`app/paths.py::to_host/to_container`）。单挂载形态下它是恒等映射，改它不会修好本场景，也不会弄坏"跳过"判定。
5. 若你**不想**丢掉旧的体积记录：只能手工把日志里 `Source:` 的值改成当前 `source_dir`（用 sed 等），风险自负；**但绝对不要顺手改 `Folder size:` 的数值** —— 见下面第一条 ⚠️。

> ⚠️ **不要为了"让它跳过"而手改日志里的体积** —— 体积是**日志里**的幂等标记（L9；现行"跳过"主判据是每库账本，见上）。手改会造成回退路径下"该文件夹永远不处理"。
> ⚠️ **文件夹名里含 `" - "` 的记录永远解析不了**（L2）：这类文件夹在回退路径下每次都会被当成"未记录"重复尝试。这是冻结的既有行为，**应用不会去"修"它**；表现为日志里同一个 folder 反复出现 `File exists` 错误，不影响其他文件夹。这些 `File exists` 会被正确算成 `already_linked`（幂等跳过、计入 `skipped`），任务**不会因此显示失败**；日志仍会每轮多一行（那是冻结行为）。

---

## 6. 目标目录被自动删除了（孤儿清理）

**【现象】**

- 任务日志/stdout 里出现 `Deleted orphaned hard link folder: /vol1/1000/Library/…/Show A`，`result.orphans_removed > 0`。
- 媒体库里某个作品文件夹**整目录消失**了。

**【原因】** 这是**有意的**功能（现行执行体：原生实现 `app/processors/hardlink.py`）：日志里记录了 folder X 已处理，但现在**源目录里已经没有 X 了**（你删了原始下载）⇒ 目标里的 `X` 目录被视为"孤儿"，整个 `rmtree` 掉。
**关键：它删的是目标目录下由本应用生成的硬链接目录，绝不碰源目录里的文件。** 单挂载形态下源目录与目标同挂载、本身可写（[场景 3](#3-源目录是可写的怎么确认应用没有动你的源文件)），这条边界由代码层保证并由测试钉住（`tests/test_no_source_mutation.py`）。

**【诊断命令】**

**【浏览器】** 任务详情 → `result.samples.orphans_removed`，看被删的是哪些目标路径（应当是**目标目录**下的路径，如 `/vol1/1000/Library/...`）。

**【NAS 宿主】**

```bash
# 这次到底删了什么（建链阶段的 stdout 也落在这份日志里）
grep 'Deleted orphaned' /vol1/1000/Docker/media-ingest/data/logs/hard_links.log | tail -n 20
# 源文件还在不在（一定还在）
ls -l /vol1/1000/Download/qbittorrent/Bangumi/
```

**【解决步骤】**

1. **先判断这是不是你想要的**：如果你确实删了原始下载、也不想在媒体库里留空壳，那就是**正常行为**，不用管。
2. 如果你希望**保留**这些目录（例如原始下载只是临时移走、还会移回来）：
   **【浏览器】** 编辑该媒体库 → 关掉「孤儿清理」（`orphan_cleanup`）→ 保存。之后该库的任务**不会**删任何目标目录。
   > `orphan_cleanup=false` 的实现方式：**先把日志里"源已不存在"的行摘出来**，跑完再把它们**原样合并回写**（位置不变、逐字节复原），因此既不会删目录、也不会让日志出现抖动。
3. 想恢复被删的目录：**重新跑一次**该库即可（源还在的话，硬链接会重新建出来，几乎不占额外空间）。
4. **误删的边界**：只有当"日志里有记录 + 源目录里确实没有该文件夹 + 目标里存在同名目录"三条同时成立才会删。要确认历史状态，**直接看 `/data/logs/hard_links.log` 原文**：`docker compose exec media-ingest grep -c 'Folder:' /data/logs/hard_links.log`（记录条数）与 `grep -A2 'Source: <库的源目录>'`；`GET /api/system/info` 里**没有**字段替你回答这个问题。

---

## 7. 权限类报错（`Permission denied` / `Read-only file system`）

**【现象】**

- `创建硬链接失败: <目标目录>/… - [Errno 13] Permission denied`
- `[Errno 30] Read-only file system`（往只读的挂载或镜像内的文件写）
- 健康检查里与源/目标可访问性相关的检查项为 `false`（`downloads_readable`/`library_writable`）、`checks.data_dir: false`
- 容器起不来，日志里有 `无法创建 /data/...`。

**【原因】** 容器内以非 root 的用户（`app`）运行；`/data` 与**目标目录**的宿主目录属主/权限必须让该用户可写。源目录在单挂载形态下与目标同挂载、本身可写，但**应用不写它**（[场景 3](#3-源目录是可写的怎么确认应用没有动你的源文件)）。

**【诊断命令】**

**【容器内】**

```bash
docker exec -it media-ingest id
docker exec -it media-ingest ls -ld /data /data/logs /data/config /vol1/1000/Library /vol1/1000/Download/qbittorrent
```

**【NAS 宿主】** 看宿主侧属主（飞牛/Debian 上常见 `uid=1000`）：

```bash
ls -ld /vol1/1000/Docker/media-ingest/data /vol1/1000/Library
stat -c '%u:%g %a %n' /vol1/1000/Docker/media-ingest/data /vol1/1000/Library
```

**【浏览器】** 系统信息页 → 挂载表里两列 `exists` / `read_only`；健康检查里 details 的中文原因。

**【解决步骤】**

1. **数据卷必须可写**（这是最常见的坑）：**【NAS 宿主】**
   ```bash
   mkdir -p /vol1/1000/Docker/media-ingest/data/logs
   chown -R 1000:1000 /vol1/1000/Docker/media-ingest/data
   chmod -R u+rwX /vol1/1000/Docker/media-ingest/data
   ```
2. **媒体库根必须可写**（应用要在里面建硬链接）：把 `/vol1/1000/Library` 的属主改成与容器内用户一致，或给它组写权限：
   ```bash
   chown -R 1000:1000 /vol1/1000/Library   # 先确认这个 uid 与容器内 id 一致
   ```
   如果你不希望改整个库的属主，至少保证**目标子目录**可写（例如只把 `/vol1/1000/Library/Bangumi` 交给它）。
3. **启动失败**几乎都是 `/data` 不可写（第 1 步）—— 启动自检看的是那六项 `checks`（见[场景 2](#2-启动即-degraded健康检查六项里有-false)）。
4. 「权限模式」（`permission_mode`，**默认关**）**是真实开关**：默认关闭时一个 `chmod` 都不做；显式开启（界面从关到开需二次确认）后，应用会把**目标侧**（库根 + 本次处理到的每个作品文件夹的目标子树，含被跳过的旧作品）改成 `0777`。⚠️ 硬链接与源文件**共享 inode**，所以开启会**同时改掉 `/vol1/1000/Download/qbittorrent` 下源文件的权限位**（内容/大小/inode/mtime 不变）—— 这是「为了让非 root 服务读得到」而付出的代价，请优先按上面第 1–3 步在宿主上做精确的属主/权限调整。每次开启运行都会在任务日志里以 WARNING 记下改了多少目录/文件。依据见 [`docs/contracts/COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) §3.3 第 8 条、[`docs/contracts/BASELINE.md`](../contracts/BASELINE.md) §1.6/B13。
5. 群晖/威联通上若出现奇怪的 `EACCES`：确认没有开启"共享文件夹只读快照/回收站"之类的宿主级限制。

---

## 8. qBittorrent 下载完成后回调没有触发任务

**【现象】** 下载完成，但「任务」列表里没有新任务；qBittorrent 里那条"运行外部程序"看起来配了却没反应。

**【原因】** 可能的原因按概率排序：① qBittorrent 的「运行外部程序」**没启用**或路径被引号/空格弄坏；② 回调 URL 里的 token 不对（会 **401**，见场景 9）；③ 库的匹配条件**全空**（分类/标签/路径前缀都为空的库**永远不会被回调命中**）；④ 该库被「停用」（`enabled=false`）或已有排队/执行中的任务（扫描会跳过它）；⑤ 容器与 qBittorrent 的网络不通（端口/防火墙/Docker 网络）。

**【诊断命令】**

**【NAS 宿主】** 手动打一次回调（用真实 token，从设置页复制的那个）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  'http://127.0.0.1:18081/api/hooks/qbittorrent?token=<你的 token>&name=Test&hash=abcdef1234567890&category=&tags=&content_path=/vol1/1000/Download/qbittorrent/Bangumi/Show%20A&save_path=/vol1/1000/Download/qbittorrent'
```

把 `-o /dev/null` 去掉就能看到响应体：`{"matched":N,"tasks":[...],"ignored":[...],"ignored_details":[...],"warnings":[...],"torrent":{...}}`。

**【NAS 宿主】** 看容器有没有收到请求：

```bash
docker compose logs --tail=100 media-ingest | grep -i hook
```

**【浏览器】** 「设置」→ 检查 `hook_token`；「媒体库」→ 看每个库的**匹配条件**是否非空、是否启用。

**【解决步骤】**

1. **回调用 7 个参数，全部走查询字符串**（JSON body 会被忽略）：
   `?token=…&name=…&hash=…&category=…&tags=…&content_path=…&save_path=…`。`kind=` 参数仍被接受但不使用。
2. 在 qBittorrent 里确认「工具 → 选项 → 下载 → 运行外部程序」已勾选，命令形如：
   ```bash
   /usr/bin/curl -s "http://<NAS_IP>:18081/api/hooks/qbittorrent?token=<token>&name=%N&hash=%I&category=%L&tags=%G&content_path=%F&save_path=%D"
   ```
   （Windows 客户端上用 `curl.exe` 并注意引号。）
3. **给库配上匹配条件**（至少一个）：分类（`match_categories`）、标签（`match_tags`）、路径前缀（`match_path_prefixes`）。全空的库**永远不会命中**，响应里会出现在 `ignored_details` 并给出原因文案。
4. 如果响应 `matched: 0` 且 `warnings` 里有「当前没有任何**已启用**的媒体库，回调无处可去。」⇒ 去媒体库页把库启用。
5. 想"什么都收"用扫描接口：`POST /api/hooks/scan?token=…`（它**不看匹配条件**，按已启用库逐个建任务；源目录不存在也会建，任务会以失败告终并给出原因）。
6. 网络排查：从 qBittorrent 所在容器/主机 `curl` 一下上文的健康检查；确认 Docker 网络能互通，或改用 NAS 的真实 IP 而不是 `127.0.0.1`。

---

## 9. 回调返回 `401`（hook token 不匹配）

**【现象】** qBittorrent 侧报错（外部程序退出码非 0 / 日志里是 401），响应体：

```json
{"detail":"回调 token 不正确…请在「设置」页重新生成 token 后，更新 qBittorrent 的「运行外部程序」。"}
```

**【原因】** token 缺失、拼错、带上了空格/引号，或你在设置页**重新生成**过 token（旧 URL 立刻失效）。

**【诊断命令】**

**【浏览器】** 「设置」→ 复制当前 `hook_token`（明文显示）。

**【NAS 宿主】** 两种传法都应成功（先找一把干净的 token）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:18081/api/hooks/qbittorrent?token=<token>&name=T&hash=h&content_path=/x&save_path=/y'
curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer <token>' 'http://127.0.0.1:18081/api/hooks/qbittorrent?name=T&hash=h&content_path=/x&save_path=/y'
```

**【解决步骤】**

1. 在设置页**重新生成** token（一次性动作，生成后无法"找回"旧值），并**立刻**把 qBittorrent 里的外部程序命令替换掉。
2. 注意 shell 引用：URL 里有 `&`，在 Windows 的 `cmd`/PowerShell 里**必须加引号**，否则参数被截断 ⇒ token 变成空 ⇒ 401。
3. 若 token 泄露过（贴进聊天记录/截图）：**必须重新生成** —— 持有 token 的人可以触发任务（但他改不了你的文件，也删不了硬链接）。
4. 确认没有在 token 前后多打空格或换行符（从网页复制时常见）。

---

## 10. 端口 18081 被占用，或页面打不开

**【现象】** 容器起不来，日志里 `bind: address already in use`；或容器 running 但页面连不上。

**【原因】** 宿主上已有别的进程/容器占用 18081；或者你改了 `PORT` 但只改了一侧（容器内监听端口与 `ports:` 的容器侧不一致）；或者浏览器走的是 NAS 防火墙拦掉的路径。

**【诊断命令】**

**【NAS 宿主】**

```bash
ss -lntp | grep 18081 || netstat -lntp | grep 18081
docker ps --format '{{.Names}}\t{{.Ports}}' | grep 18081
docker compose logs --tail=50 media-ingest
```

**【浏览器 / 宿主】**

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18081/api/system/health
```

`000` = 连不上；`200` = 应用正常（那就是浏览器/网络侧问题）。

**【解决步骤】**

1. 找出占用者：若是别的容器，改本项目的端口最省事 —— 在 `.env` 里设 `PORT=18082`（compose 里 `ports: "${PORT:-18081}:${PORT:-18081}"` 与容器内 `PORT` 会**同步**跟随），然后 **【NAS 宿主】** `docker compose up -d`。
2. ⚠️ **容器内监听端口必须与 `ports:` 的容器侧一致**，否则宿主端口会指向一个没人监听的端口（页面表现为连接被拒）。所以**只改 `.env` 的 `PORT`**，不要手写只改一侧的 `ports:`。
3. 确认防火墙/安全组放通了宿主的 18081（或你改的端口）。
4. 健康检查用的是容器内 `$PORT`，改端口后它自动跟随；`curl -s http://127.0.0.1:<PORT>/api/system/health` 用来验收。
5. 若是"容器 running 但 healthcheck 一直 unhealthy"，看 `docker inspect media-ingest --format '{{json .State.Health}}'`，多半是端口不一致（第 2 条）。

---

## 11. 忘记管理员密码 / 所有人都被登出

**【现象】** 登录页输什么都 401（`{"detail":"用户名或密码错误。"}`）；或者本来好好的，突然所有人都要重新登录。

**【原因】** 口令用 scrypt 哈希保存，**无法找回，只能重置**。另一种情况是数据卷里的 `secret.key`（会话签名密钥，`<data>/secret.key`）丢了/被换了 ⇒ 所有会话失效，**但密码没变**，重新登录即可。

**【诊断命令】**

**【容器内】**

```bash
docker exec -it media-ingest ls -l /data/secret.key /data/mlm.db
docker exec -it media-ingest python -c "import sqlite3;print(sqlite3.connect('/data/mlm.db').execute('SELECT id,username,created_at FROM users').fetchall())"
```

**【NAS 宿主】** `ls -l /vol1/1000/Docker/media-ingest/data/` 确认 `secret.key` 存在（它只在首次启动时生成）。

**【解决步骤】**

1. **先备份**：**【NAS 宿主】** `cp -a /vol1/1000/Docker/media-ingest/data /vol1/1000/Docker/media-ingest/data.bak-$(date +%F)`
2. **清空账号**（只影响登录，**不动媒体库规则、任务历史和你的文件**）：**【容器内】**
   ```bash
   docker exec -it media-ingest python -c "import sqlite3;c=sqlite3.connect('/data/mlm.db');c.execute('DELETE FROM users');c.commit();c.close();print('账号已清空')"
   ```
3. **【NAS 宿主】** `docker compose restart`，再打开页面 —— 会回到「创建管理员」。
4. 若 `.env` 里有 `ADMIN_USERNAME`/`ADMIN_PASSWORD`，在"一个用户都没有"时它们会自动创建账号；想自己设就用页面的创建流程，之后把 `.env` 里的明文密码删掉。
5. 如果**只有你自己**被登出而密码是对的：那是 `secret.key` 的问题，不要删账号 —— 直接重新登录；确认 `data/secret.key` 没有被备份工具/挂载变更弄丢。

---

## 12. ARM64（aarch64）NAS 上的构建与运行

**【现象】** 在 ARM64 NAS（部分群晖/威联通、树莓派、Mac 上的 `--platform=linux/arm64`）上构建报 `no matching manifest`、`exec format error`，或跑起来极慢。

**【原因】** 基座是 `python:3.11-slim`（官方 multi-arch，ARM64 有）；依赖只有 `fastapi==0.115.6` 与 `uvicorn[standard]==0.32.1`（`docker/Dockerfile:18/59/68`）。`uvicorn[standard]` 会带 `uvloop`/`httptools`：**优先用预编译 wheel**，装不上时会尝试从源码编译（需要编译器 ⇒ 构建失败或极慢）。另外本应用**必须单进程**：`--workers 1`（`docker/Dockerfile:125`）是**架构要求** —— 建链阶段对日志做"读-改-写整份"并依赖全局串行（B11）。

**【诊断命令】**

**【NAS 宿主】**

```bash
uname -m                      # aarch64 = ARM64
docker version --format '{{.Server.Arch}}'
docker image inspect media-ingest:latest --format '{{.Architecture}}/{{.Os}}'
docker compose logs --tail=80 media-ingest
```

**【解决步骤】**

1. **原机构建**：直接在 ARM64 机器上 `docker compose build`（不要从 x86 机器导出镜像后拷过去）。
2. 构建慢/失败时，先确认基础镜像已存在，避免构建期拉取超时：
   ```bash
   docker pull python:3.11-slim
   docker compose build --no-cache
   ```
3. `exec format error` = 架构不匹配，重建即可（第 1 条）。
4. **绝对不要**为了提高吞吐而加 `--workers`/`replicas`：多进程会破坏"同一 (源,目标) 同时只有一个任务、全局串行"的前提（B11），日志"读-改-写整份"还会互相覆盖（L1）。要更快请缩小运行范围（按文件夹运行）而不是加并发。
5. 若 `uvloop` 在 ARM64 上装不上：`uvicorn[standard]==0.32.1` 的 wheel 通常覆盖 `manylinux aarch64`；确实失败时需要在构建期装编译工具链（`gcc`/`python3-dev`），**但这属于对环境动手**，请先备份并在容器外改 Dockerfile（该文件的写入范围属于 devops）。

---

## 13. 容器重启后的队列状态

**【现象】** `docker compose restart` 之后，任务列表里有任务永远停在 `queued`/`running`；或你想知道"重启会不会把没跑完的任务接着跑"。

**【原因】** 队列是**持久化在 SQLite** 里的（`tasks` 表），单工作线程在启动时开始消费 `status='queued'` 的任务（B11）。因此：

- ✅ **重启后仍在 `queued` 的任务会继续执行**（FIFO，按 id 升序）。
- ⚠️ **重启瞬间正在 `running` 的任务不会自动续跑**：进程被杀，它留在 `running`（历史记录），**不会**自动重试；需要你手动「重试」（`POST /api/tasks/{id}/retry`，只对 `failed`/`cancelled` 有效）或「立即运行」媒体库。
- 取消只在**还没开始执行**时有效（`queued` → 204）；已经 `running` 的取消会返回 409。

**【诊断命令】**

**【NAS 宿主】**

```bash
docker compose exec media-ingest python -c "import sqlite3;c=sqlite3.connect('/data/mlm.db');[print(r) for r in c.execute('SELECT id,library_name,status,created_at,started_at,finished_at FROM tasks ORDER BY id DESC LIMIT 20')]"
```

**【浏览器】** 「任务」页按状态筛选（`GET /api/tasks?status=queued|running|failed|success`），「统计」页看 `queue.running_task_id` / `queue.queued_count`。

**【解决步骤】**

1. 重启后先看「统计」：`queued_count` 若在下降，说明队列正在消费，**等它跑完**即可（大库可能很久）。
2. `running` 却已经没人跑的任务：直接对它「重试」（若状态不是 `failed`/`cancelled` 会返回 409 并说明规则），或对该库点「立即运行」。
3. **重跑是安全的**：体积一致的文件夹会被跳过（`skipped`），已有硬链接不会重建，源文件永不改动。
4. 想让重启后的行为更可控：把 `restart: unless-stopped`（`docker-compose.yml:77`）保留 —— 它会自动拉起容器，队列随之续跑。
5. 若重启后任务**立即大批失败**且错误含 `File exists`：见场景 5（旧日志里的 `Source:` 是宿主路径），那不是重启引起的。

---

## 14. 任务长时间停在 `queued`

**【现象】** 任务状态一直 `queued`，日志只有「任务已入队（类型=…，触发=…）」。

**【原因】** 队列是**全局单线程**（一次一个任务，B11）。前面有长任务在跑时，后来的任务只能排队。另外：**同一媒体库已有排队/执行中任务时，扫描/回调会跳过它**（`app/scanner.py:125`），所以"我点了扫描但它没建任务"也可能是这个。

**【诊断命令】**

**【浏览器】** 「统计」页 → `queue.running_task_id`、`queue.queued_count`；「任务」页筛 `status=running` 看是谁在占用。

**【NAS 宿主】**

```bash
docker compose exec media-ingest python -c "import sqlite3;c=sqlite3.connect('/data/mlm.db');[print(r) for r in c.execute(\"SELECT id,status,library_name,started_at FROM tasks ORDER BY id\")]"
docker compose logs --tail=50 media-ingest
```

**【解决步骤】**

1. 正常排队 ⇒ 等前一个跑完（大库的全量首跑可能几十分钟，取决于文件数）。
2. 确认工作线程活着：`docker compose logs` 里应有 worker 相关输出；**不要**用 `--workers 2` 或起两个容器来"加速"（会破坏串行保证，见场景 12 第 4 条）。
3. 想立刻做某个库：**取消**排在它前面的 `queued` 任务（任务页「取消」，仅 `queued` 可用），或先停掉正在跑的（只能等它结束 —— 应用不提供强杀）。
4. 若 `running_task_id` 指向一个其实已经没在跑的任务（容器被强杀留下的），重启容器后用「立即运行」重新发起（重启会把队列唤醒）。
5. 首跑太慢的根治办法是**分库/分文件夹**跑（`kind` 支持按 `scope.folder` 只处理某一个一级文件夹）。

---

## 15. 文件没有被改名 / 名字里出现 `ENone`

**【现象】** 任务成功，但某个文件名字变成 `Show A S01ENone.mkv`；或者某些文件完全没动（日志里每行 `Kept (no episode): …`）；或者 `Season 1 (2018)` 里的文件没被处理。

**【原因】** 这些**大多是冻结的既有行为（L3/L5/L6/L7），应用不会"修正"**（见 [`docs/contracts/BASELINE.md`](../contracts/BASELINE.md) §5）：

- 文件名里**没有任何集数标记**：这时看规格里有没有配 `episode_fallback`——
  - **配了非 `null` 的值**（`legacy` 预设的字符串 `"None"`，或你自己写的字面量）⇒ 集数段就是那个字面量 ⇒ `…S01ENone.mkv`（L3）。
  - **没配 / 留空**（`episode_fallback: null`，**内置 `bangumi` 的推荐默认就是它**）⇒ 引擎**不给它造名字**：命名既然引用了 `episode` / `episode_raw`，这个文件就**保留原文件名、完全不改**，日志里一行 `Kept (no episode): <文件夹>/<原文件名>`（WARNING），并计入 `result.kept_unresolved_episode`（明细在 `result.samples.kept_unresolved_episode`，同时 `warnings[]` 里有一条汇总告警）。
  - **`episode_fallback` 是空串**（在编辑页把「集数兜底」输入框清空、或配置文件里写 `episode_fallback:`）⇒ **空串与 `null` 等价** ⇒ 回到上面那条"保留原文件名、不改名"。该字段的边界与核对方法见 [`docs/contracts/API.md`](../contracts/API.md) §5.7.1 里 `episode_fallback` 那一行。
- 只处理 `.mkv`/`.mp4`/`.ass`，且**大小写敏感**：`UPPER.MKV` **不会**被改名（L7）；`.srt` 也不改名，但**会被建硬链接**。
- **集数认错**（`legacy` 预设那两条正则的既有毛病）：那两条分支 `-\s*(\d{1,2})` / `E(\d{1,2})`
  没有任何"这里不可能是集号"的约束，于是 `Show - 1920x1080.mkv` 被认成第 **19** 集、`[1AE35F3A]` 里的
  `E35` 被认成第 **35** 集。内置 `bangumi` 的 `episode_patterns` **默认是 9 条带边界约束的有序规则**
  （[`docs/contracts/PROCESSORS.md`](../contracts/PROCESSORS.md) §3.2 增量 ④），这两类都不命中 ⇒ 改成"保留原名"。
  如果你仍看到这种误判，说明该处理器的规格是旧的（处理器规格**不会**随镜像升级）——
  用「恢复默认」选 `default`，或在编辑页把「集数规则」换成 §3 里那 9 条。
- **集数认不出**（新规则比旧规则**严格**，这是有意的）：`Show.E07.mkv` / `Show.e06.mkv` 这种 **`E` 紧贴点号**
  的旧写法不命中（规则 8 要求 `E` 前是**空白**），`Show 12v2.mkv`、`Show 1920x1080.mkv` 也不命中（规则 9
  要求数字后是空白或句点）。这些文件**保留原名**并出现在 `kept_unresolved_episode` 里。想恢复旧行为：
  整体用 `legacy` 预设，或把集数规则里换回 `legacy` 那两条（见 [`docs/contracts/PROCESSORS.md`](../contracts/PROCESSORS.md) §3.2 增量 ④）。
- 季文件夹必须**完整匹配** `Season (\d+)`：`Season 1 (2018)` 被忽略（L5）。
- 只处理季文件夹的**直接子文件**：季文件夹里再套一层目录，里面的文件**永不重命名**（L6）。
- 同一后缀名有多个文件时，**只保留体积最大的那个**（按后缀分组），其余保持原名。

**【诊断命令】**

**【浏览器】** 任务详情 → `result.renamed` / `result.rename_failed` / `result.kept_unresolved_episode` / `result.samples.renamed` / `result.samples.kept_unresolved_episode`；任务的日志里直接有引擎输出的 `Renamed: <旧> -> <新>`、`Kept (no episode): <文件夹>/<文件名>`（保留原名，没解析出集数）与 `Error renaming <旧>: <错误>`（以及跳过原因 `跳过 …：命中拒绝目录规则 …`）。

**【NAS 宿主】** 想看全量输出与日志：

```bash
docker compose exec media-ingest ls -l /data/logs/
# 每个媒体库自己的重命名日志是 JSON：<作品名> -> {size, timestamp, works_dir}
docker compose exec media-ingest head -c 400 /data/logs/bangumicache.log
docker compose logs --tail=200 media-ingest | grep -E 'Renamed:|Error renaming|Kept \(no episode\):'
```

（**重命名由配置驱动的规则引擎执行**（`app/processors/engine.py`）；`hard_links.log` 只记硬链接流水线。）

**【解决步骤】**

1. 先按上表核对命名规则（**这些不是 bug**）。要看完整规则与冻结证据：`docs/contracts/BASELINE.md` §2/§3 与 `docs/contracts/COMPATIBILITY.md`。
2. **改名失败**（`rename_failed > 0`）常见于**目标名已存在**：
   - 文件名无集数 ⇒ 全部算出 `Show A S01ENone.mkv` ⇒ 互相撞名。修法：让文件名带上集数（`第01集`/`[01]`/`EP01`/`E01`/`- 01`/`#01` 之一 —— 注意 **默认规则要求 `E01` 前面是空白**，写 `Show.E01.mkv` 这种紧贴点号的形式**不算**；`Show E01.mkv` / `Show E01 1080p.mkv` 才算），再重跑；**或者**把该处理器的 `episode_fallback` **清空成 `null`** ⇒ 这类文件**保留原文件名**、根本不会再撞名（日志里每行 `Kept (no episode): …`，计入 `result.kept_unresolved_episode`）。
   - **L2「防丢」：POSIX 的静默覆盖已不存在**：目标名被占用时按**体积**判定 —— 体积最大的拿干净名，其余加 ` (2)`、` (3)`… 后缀保留（`result.renamed_with_suffix`），目标盘上原有的同名文件比新来的小时改名**让位**（`result.displaced_targets`，**不删除**），找不到空位则**保留原名**并记 `error`。所以"少一个硬链接"不会再发生；`result.name_collisions > 0` 只说明**发生过争名**，不等于丢文件。⚠️ 这是刻意与 `os.rename` 的默认行为分叉：`os.rename` 在目标同名时仍是 POSIX 静默覆盖、Windows 报 `[WinError 183]`（见 [`docs/contracts/BASELINE.md`](../contracts/BASELINE.md) §5 的 L4 行）。
3. `Season 1 (2018)` 这类：改目录名为 `Season 1`（**这会改变你的库结构**，请自行决定）—— 应用不能替你放宽匹配（那是冻结契约）。
4. 想让"没有集数的文件"不动：把文件名补上集数，或把它们移出季文件夹；还可以把处理器的 `episode_fallback` **清空成 `null`** —— 引擎会**保留原文件名**并在 result / 日志里明确记一笔。
5. 第二遍跑时**已经正确命名的文件会自己"改到自己"**（`renamed: [("X","X")]`），这是正常无害的（不会丢文件），不要当成故障。

---

## 16. 改了源文件但任务仍然跳过

**【现象】** 你在下载目录里**原地替换/改写了**某个已处理文件夹里的文件（体积**没变**），重跑任务时该文件夹被「跳过」。

**【原因】** 现行建链阶段（原生实现 `app/processors/hardlink.py`）的幂等标记是**一级文件夹的总体积**（L9）。体积一样就跳过（`文件夹已记录且体积一致，跳过: <folder>`）。而且体积检查是**事后**的：链接完成后源被原地改写（同尺寸）不会被发现（L8）。**这不是本应用的取舍，而是冻结的既有规则。**

**【诊断命令】**

**【浏览器】** 任务日志里搜 `文件夹已记录且体积一致，跳过:`，能看到跳过了哪些文件夹。

**【NAS 宿主】** 对比"日志里记的体积"与"当前实际体积"：

```bash
grep 'Source:' /vol1/1000/Docker/media-ingest/data/logs/hard_links.log | tail -n 20
du -sb /vol1/1000/Download/qbittorrent/Bangumi/*/    # 与上面的 Folder size 对照
```

**【解决步骤】**

1. **只想让某个文件夹重做**：运行弹窗里**没有"强制重跑"开关**，也不接受整库 `force`（返回 400）。正确做法二选一：① 在「作品记录」（左侧栏里的独立页面）里**删掉该作品的记录** ⇒ 下次运行自动重新建链 + 重命名（⚠️ 会连带删掉目标里那个作品文件夹，源目录不动）；② 只想重新排一次名字 ⇒ 勾选它点「**立即重命名**」（只重命名、不建链）。要彻底重建硬链接见第 2 条。
   ⚠️ **不要**用单作品 `force=true`（`scope.mode="folder"`，只有接口能发）来"补文件"：建链用的是源目录里的**原名**，目标里已改好名的同一 inode 会被**再链一份**（同一 inode 两个目录项 + ` (2)` 后缀），详见 [`docs/meta/KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-9。它也照样会打出一批 `File exists`："目标就是同一 inode 硬链接"的那些会被改判为**幂等跳过**（`already_linked`，任务不算 `failed`），只有同名而 inode 不同才算 `link_failed`。
2. **想彻底重建硬链接**：先删掉**目标目录**下对应的文件夹（或改库的目标目录），**再到「作品记录」里删掉该作品的账本记录**，然后跑一次普通执行。**绝不要删源目录下的文件**。
3. 只改了**体积**（增删文件）的话**不用管**：体积不一致会被自动识别为"需要重做"，旧记录行会被清掉再写新行。
4. 想避免"等体积改动被忽略"：改完之后顺手加/删任一文件（改变体积）；或删掉该作品的账本记录后重跑（见第 1 条）。⚠️ 用 `force` 只是为了绕开"体积没变"这个盲区，**不是**让目标变干净的手段（见 KI-9）。

---

## 17. 配置文件被外部改坏后如何恢复

**【现象】**

- 设置页或处理器页顶部出现**红色横幅**，提示配置文件有错误。
- 你手改了 `/data/config/processors/*.yaml` 或 `/data/config/libraries.yaml` 之后，WebUI 里的处理器/媒体库**没有变成你手写的样子**，看起来「停在最后一次好配置」。
- 容器日志里能找到 `配置导入失败` 之类的 WARNING（不影响服务运行）。

**【原因】**

配置是**双向同步**的：WebUI 的每次保存都会原子写出 YAML；反过来，后台 watcher 每 `CONFIG_SYNC_INTERVAL_SECONDS` 秒（默认 5）比对文件的 mtime + sha256，发现外部编辑就尝试导入。**导入失败时数据库一个字节都不改**（事务回滚），错误进 `config_events` 表，并通过 `GET /api/config` 的 `errors` 暴露给 WebUI —— 所以你看到的永远是「最后一次成功导入」的状态。

**【诊断命令】**

**【浏览器】** 打开设置页顶部横幅，或直接看接口：

```bash
# 【容器内】看一眼当前快照：errors 里就是坏文件与原因
curl -s -b "mlm_session=<你的会话 cookie>" http://127.0.0.1:18081/api/config | python3 -m json.tool
```

关注四个字段：

| 字段 | 含义 |
|---|---|
| `errors[] = {path, message, at}` | **最近一次成功导入之后**发生的导入错误（最多 20 条）。**改对文件后会自动清空** —— 它不是"历史错误清单"。 |
| `last_import_at` | 最后一次成功导入的时间 |
| `files[] = {path, relative, exists, sha256, in_sync, size, updated_at}` | 每个配置文件与数据库是否一致；`in_sync: false` = 磁盘与库不一致 |
| `config_dir` | 配置目录（默认 `/data/config`） |

**【解决步骤】**

1. **把 YAML 改回合法内容**（对照 `schema_version: 2` 与处理器字段表；正则、`kind`、`log_file`、`naming.segments` 是最容易写错的地方），然后强制重新导入：

   ```bash
   # 【容器内】注意 force 是**查询参数**，不是 JSON body
   curl -s -X POST -b "mlm_session=<你的会话 cookie>" \
     "http://127.0.0.1:18081/api/config/reload?force=true" | python3 -m json.tool
   ```

   `force=true` 会忽略哈希缓存、**全量重导**一遍；不带参数则只导入"哈希变了"的文件。
2. **不想修、只想恢复成 WebUI 里的样子**：删掉（或改名成 `.bak`）那个坏文件，再执行一次上面的 `reload`。数据库里的处理器/媒体库仍然完好；随后用下面这条**用数据库覆盖磁盘**，把文件重新写回来：

   ```bash
   # 【容器内】把 DB 里的处理器与媒体库全部写出（会覆盖手工编辑，慎用）
   curl -s -X POST -b "mlm_session=<你的会话 cookie>" \
     http://127.0.0.1:18081/api/config/export | python3 -m json.tool
   ```

   `POST /api/config/export` 只有一条硬规则：**它会覆盖磁盘上的同名文件**，手工编辑的内容会丢。想要"文件为准"就用 `reload`，想要"界面为准"就用 `export`。
3. **配置文件里的媒体库引用了不存在的处理器**（导入顺序是先处理器、后媒体库，所以处理器文件坏了会连带库导入失败）：先修好处理器 YAML（或先建好处理器），再 `reload`。
4. 修好之后确认横幅消失：`errors` 变回 `[]`、`last_import_at` 前进、`files[]` 全部 `in_sync: true`。

**【注意】** `errors` 的语义是「**最近一次成功导入之后**的错误」，所以：① 改对文件后它会**自动清空**，不需要手工清理；② 如果一次都没成功导入过（全新部署），它也会是空的 —— 空 `errors` 不等于"配置一定同步"，请同时看 `files[].in_sync`。

---

## 18. 处理器被删除了但媒体库还在引用

**【现象】**

- 媒体库列表里该库后面带一个**红色徽章「规则已删除」**（英文界面 `Processor deleted`）。
- 该库的任务在**重命名阶段**失败/跳过，错误里出现：`处理器 <id> 不存在（可能已被删除）。`
- 库编辑页的处理器下拉把原值显示成 `<id>（当前已不存在）`。

**【原因】**

处理器（重命名规则）是媒体库的**外键**。数据库不禁止你删掉一个仍被引用的处理器（也不会连带删库），所以会出现「库还在、规则没了」的状态：硬链接阶段不依赖重命名规则、照常能跑；**重命名阶段**拿不到规则，只能报错跳过。

**【诊断命令】**

**【浏览器】** 看库列表的红色徽章，或：

```bash
# 【容器内】processor_missing: true 就是"规则已删除"；processor_id 是它引用谁
curl -s -b "mlm_session=<你的会话 cookie>" "http://127.0.0.1:18081/api/libraries?limit=200" \
  | python3 -m json.tool
```

库对象里的相关字段：`processor_id`（引用）/ `processor_name` / `processor_kind` / `processor_missing`。库的 `warnings[]` 里会有一条原文：`该库引用的处理器「<id>」不存在：任务会在重命名阶段报错并跳过。`

**【解决步骤】**

1. **让规则回来**：在 WebUI「处理器」页新建一个 **id 完全相同**的处理器（或从模板复制一个再改 id），保存后该库立刻恢复正常（库不用改）。
2. **或者改引用**：在库编辑页把「处理器」下拉改成一个**存在**的处理器并保存；保存时若处理器不存在，接口会直接返回 **422**（`loc: ["body","processor_id"]`，中文说明可用处理器有哪些），不会让你存下一个坏引用。
3. **删除被引用的处理器会被拦住**：`DELETE /api/processors/{id}` 在仍被媒体库引用时返回 **409**，消息里列出引用它的库（`仍有媒体库在使用该处理器，无法删除：<库名列表>。请先改掉这些媒体库的处理器。`）。所以"正常路径"下不会出现这种状态 —— 出现它通常是因为**手工删了 `/data/config/processors/<id>.yaml`**，被 watcher 导入成了"删除处理器"。
4. **内置处理器永远删不掉**：`DELETE /api/processors/bangumi` 返回 **409** `bangumi 是内置处理器，不能删除，只能「恢复默认」。` —— 想改回出厂行为请用 `POST /api/processors/bangumi/reset`（见场景 20）。

**【注意】** 手工删除 `/data/config/processors/<id>.yaml` **等于在界面上删掉该处理器**（文件是配置的真源之一）。只想临时停用某个库，请用库的「启用」开关，不要删配置文件。

---

## 19. 日志文件名不合法（422）

**【现象】**

保存媒体库时 `POST /api/libraries` 或 `PUT /api/libraries/{id}` 返回 **422**，响应体里能读到字段级错误（真实形状是 `detail: [{loc, msg, type}]`，`loc[0]` 固定为 `"body"`）：

| 你填的值 | 错误消息（原文） |
|---|---|
| `logs/my.log`、`../evil.log`、`sub\a.log` | `日志文件名只能是文件名，不能包含路径分隔符：…。所有日志都固定在数据卷的《/data/logs》目录下。` |
| `mycache`、`a.txt` | `日志文件名必须以 .log 结尾：…（例如 bangumicache.log）。` |
| 与别的库/处理器同名 | `日志文件名 … 已被其它媒体库或处理器占用；请换一个名字，或留空由系统自动分配（下一个可用名：bangumicache-2.log）。` |

**【原因】**

所有重命名日志**固定在数据卷的 `/data/logs/` 下**（不允许路径穿越）。更关键的是：**一个媒体库一份日志** —— 这份日志记的是「这个标题文件夹已经处理过、体积是多少」，如果两个库共用一份，A 库处理完的同名同体积文件夹会让 B 库直接走「体积一致 ⇒ 已处理」分支被**静默跳过**（你会看到"B 库跑了但什么都没做"）。所以服务端会为每个库分配唯一名。

**【对策】**

1. **什么都不填**（推荐）：服务端按库自动分配 —— 基名是处理器登记的日志名（内置 `bangumicache.log` / `moviecache.log` / `hardlink_only.log`，自定义 `<id>cache.log`），已被占用就在**扩展名前**追加序号：第一个 bangumi 库是 `bangumicache.log`，第二个是 `bangumicache-2.log`。
2. **想自己命名**：只给文件名（不含 `/`、`\`、`..`）、必须以 `.log` 结尾、不能与任何媒体库或**任何处理器登记的**日志名重复。错误消息里会直接告诉你下一个可用名。
3. **已有名字的库不用担心重名**：分配按库 id 升序逐个去重（`bangumi→bangumicache.log`、`movie→moviecache.log`、`hardlink_only→hardlink_only.log`、其它→`{slug}cache.log`），**已有非空名字原样保留**；万一撞名，只有后来者会被追加 `-2`。这也意味着**磁盘上已存在同名文件不算冲突** —— 第一个库照样用 `bangumicache.log` 并**继续追加**。

**【诊断命令】**

```bash
# 【容器内】看所有库各自用哪个日志文件（按库隔离）
curl -s -b "mlm_session=<你的会话 cookie>" "http://127.0.0.1:18081/api/libraries?limit=200" \
  | python3 -c "import json,sys; [print(l['id'], l['name'], '->', l['log_file']) for l in json.load(sys.stdin)['items']]"
```

---

## 20. 特典目录被跳过时如何用 `legacy` 换回最朴素的旧口径

**【现象】**

- 某些标题文件夹的结果和你**期望的不一样**。典型：作品名像 `SP` / `CMs` / `Scans` / `CD` / `映像特典` / `Preview` / `NCOP` / `Subtitle` / `ScreenShots` 这种**特典/扫图/字幕目录**，整个子树被**跳过**（里面的文件不改名）。
- 反过来也成立：你把 `traverse.reject_dirs` 清空之后，特典目录又被改名了。

**【原因】**

这是**有意加的拒绝规则增量**（①②；有意增量共**四处**，另两处是 ③ `episode_fallback`
与 ④ `episode_patterns`，见 §15 与 [`docs/contracts/PROCESSORS.md`](../contracts/PROCESSORS.md) §3.2、`docs/contracts/BASELINE.md` §4 B4）：

- `bangumi` 内置处理器的 `traverse.reject_dirs` 是一份特典/扫图类目录名单（SP/CM/OP/ED/Scans/CD/映像特典/Preview/…）；
- `movie` 内置处理器有一份特典名单（`特典`/`CD`/`扫图`/`scans` 等）。

`preset=legacy` 就是**把这些拒绝规则清空**，从而回到「最朴素的旧口径」。`legacy` 与 `default`
在拒绝目录上的差异就是把 `traverse.reject_dirs` 置空；**两者在集数上也不同** ——
`episode_fallback`（字符串 `"None"` vs `null`）与 `episode_patterns`（两条宽松老规则 vs 9 条新规则），
所以点「恢复默认/legacy」会**同时**改这三处，见 [`docs/contracts/PROCESSORS.md`](../contracts/PROCESSORS.md) §3.2。

**【诊断命令】**

**【浏览器】** 打开「处理器」页 → 选中该处理器 → 看「拒绝目录正则」列表是否非空。

```bash
# 【容器内】看某个处理器当前是否带拒绝规则
curl -s -b "mlm_session=<你的会话 cookie>" http://127.0.0.1:18081/api/processors/bangumi \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('reject_dirs =', d['traverse']['reject_dirs'])"
```

**【解决步骤】**

1. **整类换成最朴素的旧口径**：在 WebUI 处理器编辑页点「恢复默认」，在二次确认弹窗里选 **legacy**（= 最朴素的旧口径）。等价的接口调用：

   ```bash
   # 【容器内】body 可省（= default）；legacy 会把 traverse.reject_dirs 置空
   curl -s -X POST -b "mlm_session=<你的会话 cookie>" \
     -H 'Content-Type: application/json' -d '{"preset":"legacy"}' \
     http://127.0.0.1:18081/api/processors/bangumi/reset | python3 -m json.tool
   ```

   之后再用上面的诊断命令确认 `reject_dirs = []`。
2. **只想改掉个别规则**：别用 reset（会整份覆盖），直接在处理器编辑页增删「拒绝目录正则」行，保存即可（`PUT /api/processors/{id}` 是全量替换）。
3. **想两套并存**（例如"番剧用 `default`、特典合集用 `legacy`"）：从 `bangumi` 模板**新建一个自定义处理器**，把它的拒绝规则清空，再给对应的媒体库改选它。这样不用动内置处理器。
4. **`legacy` 只对内置处理器有效**：对自定义处理器调用 reset 会说「自定义处理器没有可恢复的默认值」；自定义处理器请自己编辑规则。

**【注意】** 回退成 `legacy` 之后，**已经按 default 处理过的库不会自动重做**：重命名日志里已经记了「该文件夹已处理 + 体积」，体积没变就会被跳过。**整库「强制重跑」已停用**（返回 400），想让作品按新规则重跑请逐作品来：在「作品记录」里勾选 →「**立即重命名**」（只按新规则改名，不建链），或**删掉该作品的记录**后跑一次普通执行（目标文件夹整体重建，见场景 16 / 27）。

---

## 21. `same_filesystem` 说可以，真跑却全部失败（预检假绿）

**【现象】**

- 媒体库「校验」显示 `same_filesystem: true`（甚至 `probe.ok: true`），但一点「立即运行」，每个文件都是 `[Errno 18] Invalid cross-device link`（= 场景 1）。
- 或者：`same_filesystem` 是 **`null`**，页面上没有红字，于是被当成"通过"。
- 也可能直接给出 **`已改用 st_dev 判断`** 之类的 note，然后一切"绿"。

**【原因】** **`st_dev` 相等 ≠ 能建硬链接。** 内核对硬链接的判据是**挂载身份**（`do_linkat()` 比较 `old_path.mnt`），不是设备号。同一块盘被 compose 挂成两条 bind ⇒ `st_dev` 相同、`link()` 依然 `EXDEV`。判定级联在实证探针不可用时也会退回 `st_dev` 比较 ⇒ 那种 `true` 不是实证结论，note 会提示可能假绿。

**【口径：三态】** 探针的结论只能是三种，**没有第四种**：

| `same_filesystem` | 含义 | 可信度 |
|---|---|---|
| `true` | 已经**实际**在容器内用 `os.link` 成功链过一个真实文件（探针只读源、写在目标侧、用完即删） | 实证结论，可信 |
| `false` | 挂载身份不同 ⇒ `link()` 必然 `EXDEV` | 可靠（内核语义） |
| `null` | **无法实证**：源目录里找不到可用的普通文件样本，或探针无法运行 | **不是"通过"**，需要按提示处理 |

判定级联（从强到弱）：① **实证探针**（主判据）→ ② **挂载身份**（不同 ⇒ `false`；相同 ⇒ `true` 但 note 会注明"未实证、必要不充分"）→ ③ 回退 **`st_dev`**（相同 ⇒ `true` + note 提示可能假绿）→ ④ 都不可用 ⇒ `null`。

**【诊断命令】**

**【浏览器】** 对媒体库点「校验」，或：

```bash
# 【NAS 宿主】
curl -s -b "mlm_session=<你的会话 cookie>" \
  'http://127.0.0.1:18081/api/libraries/1/validate?probe=true' | python3 -m json.tool
```

看三个地方：`same_filesystem`（三态）、`probe.ok`、`probe.note`（**中文，直接告诉你下一步**）。

**【NAS 宿主】** 想知道"到底是不是同一个挂载"（`st_dev` 在这里帮不上忙）：

```bash
findmnt -T <源目录> -o TARGET,SOURCE,FSTYPE,OPTIONS
findmnt -T <目标目录> -o TARGET,SOURCE,FSTYPE,OPTIONS   # 两条 TARGET 必须相同
```

**【解决步骤】**

1. `same_filesystem: false` ⇒ 按[场景 1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link)改成**单一共同父挂载**。
2. `same_filesystem: null` ⇒ 读 `probe.note`。最常见的原因是源目录里**没有可用的普通文件样本**：探针只在**有界范围**内找（递归深度 ≤3、最多看 512 个条目、**不跟随符号链接**）。解决办法是等下载目录里真的有一个文件后重新校验 —— **不要**为了让它变绿而手工往源目录塞测试文件。
3. **永远不要把 `null` 当成 `true`。** 它的意思是"没能证明可以"，不是"证明可以"。
4. 探针是**只读安全**的：它只**读**源目录，临时文件建在**目标**侧，退出时删掉（残留的 `.mlm_probe_*` 会在下次启动时被清理）。它**不会**修改源文件；如果你怀疑它动过源，看[场景 3](#3-源目录是可写的怎么确认应用没有动你的源文件)的 `source_integrity`。

**【注意】** 三态的"落点"要分清，别把好消息当故障、也别把错误当提醒：

- **校验接口**（`POST /api/libraries/{id}/validate`）：`false` 是**结论性错误** ⇒ `ok: false` 且同一句文案进 `errors[]`；只有 `null` 才**只**进 `warnings[]` 并放行；`true` 且带判据文案时**也会**写进 `warnings[]` —— 连"已实证成功"那句也进 ⇒ **`warnings` 非空 ≠ 有问题**，看到黄字别慌，逐条读文案。
- **跑任务**：运行前的预筛只**警告**、不硬拦（跨挂载时任务仍会跑，然后在每个文件上失败）。这是刻意的：预检万一误报 `false`，不该把本来能跑的任务挡在门外。所以**别指望它会拦住你** —— 看到 `false` 就按场景 1 处理。

---

## 22. 升级到单挂载形态后容器 unhealthy（旧的"允许根目录"还在）

**【现象】**

- 按[场景 1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link)把 compose 改成单一共同父挂载后，容器状态是 **`unhealthy`**，或任务立刻失败并提示路径**不在允许的根目录下**。
- 打开「设置」页，两个根目录还是老值：源 `/downloads`、目标 `/library` —— 而这两个容器路径**已经不存在了**。
- 健康检查里与源/目标可访问性相关的那几项是 `false`（键名以 `/api/system/health` 实际返回为准）。

**【原因】** `allowed_source_roots` / `allowed_target_roots` 是**存在数据库里**的（不是每次从 compose 现算），**旧部署**里存的还是 `/downloads`、`/library`。你把挂载换成 `MEDIA_ROOT` 之后：

- 数据库里那两个旧路径在容器内**已不存在** ⇒ 可访问性检查失败 ⇒ `unhealthy`；
- 即便你能建库，路径白名单（`paths.assert_allowed`）也会拿旧根目录去比，新路径一律"越界"。

**新装**的部署不会遇到这个坑：默认根目录由环境变量驱动（`DEFAULT_SOURCE_ROOT` / `DEFAULT_TARGET_ROOT`，默认跟随 `MEDIA_ROOT`）；**沿用旧数据卷**的部署才会带着旧值。

**【诊断命令】**

```bash
# 【NAS 宿主】健康检查（六项）
curl -s -b "mlm_session=<你的会话 cookie>" http://127.0.0.1:18081/api/system/health | python3 -m json.tool
# 看 checks/details 里哪一项 false；再看当前生效的根目录设置
curl -s -b "mlm_session=<你的会话 cookie>" http://127.0.0.1:18081/api/settings | python3 -m json.tool
```

```bash
# 【容器内】确认旧路径确实不存在（这就是 unhealthy 的直接原因）
docker exec -it media-ingest sh -c 'ls -d /downloads /library 2>&1'
```

**【解决步骤】**

1. **【浏览器】** 「设置」页把两个根目录改成**宿主真实路径**（单挂载形态下容器路径 = 宿主路径，例如源 `/vol1/1000/Download/qbittorrent`、目标 `/vol1/1000/Library`），保存。
2. 这两个路径必须**在同一个挂载里**，否则硬链接依旧不可能（[场景 1](#1-任务全部-link_failed错误里有跨文件系统错误-errno-18-invalid-cross-device-link)）。
3. 对每个媒体库点一次「校验」：`same_filesystem` 应为 `true`、`probe.ok: true`。
4. 如果设置页里改不动或保存后仍然报旧路径：说明还有别处引用旧路径 —— 检查媒体库自身的「源目录/目标目录」（库级路径优先于全局默认），把它们一并改掉。
5. 改完健康检查应恢复 `healthy`；`docker compose up -d` 之后**不需要**清库重建。

---

## 23. 库级覆盖 / 命名模板保存报 422

**【现象】**

- 保存媒体库时红色报错，错误项的字段路径以 **`rule_override.`** 开头（例如
  `rule_override.naming.template`、`rule_override.traverse.accept_dirs[0].season_group`）。
- 或者在设置页保存后缀白名单时报错。

**【原因】** 库级覆盖是**先校验自己、再合并到处理器上整体校验一遍**（两遍都过才落库），
所以报错可能来自"覆盖本身写错了"，也可能来自"覆盖与处理器冲突"。

**【错误文案对照表】**（照抄自 `app/processors/overrides.py` 与 `app/processors/spec.py`）

| 报错文案（片段） | 意思与改法 |
|---|---|
| `未知字段：xxx（只允许 extensions、traverse、naming）。` | 覆盖只认这三个键；`title_depth` 也不可覆盖 |
| `title_depth 不可按库覆盖（当前只支持 1…）` | 标题文件夹必须是库根的直接子目录，这条不支持按库改 |
| `file_scope 必须是 subdir 或 title，当前是 'xxx'` | 拼写错误 |
| `模板里用了未知变量 {xxx}。可用变量：…` | 变量名不在 12 个上下文里、也不是你在「命名表达式」里定义的名字 |
| `模板里出现了空的 {}，请写变量名或对花括号转义为 {{ }}。` | 想输出一个字面花括号要写 `{{` / `}}` |
| `模板变量 {episode:00} 的补零写法不合法：只支持 {名称:0位数}，例如 {episode:02}。` | 位数必须是 ≥1 的整数 |
| `season_group={n} 超出正则的捕获组数量（该正则只有 {m} 个组）；若不打算从这个正则取季号，请显式写 "season_group": 0，或给正则加一个捕获组。` | 最常见：正则里没有括号。要季号就加一组（`^Season (\d+)$`），不要就写 `0` |
| `file_scope=title 时文件直接取自标题文件夹，accept_dirs 无意义，请留空。` | **跨层冲突**：处理器是 `title` 作用域，但你给这个库写了接受目录规则 |
| `后缀必须以点开头，当前是 'mkv'。` | 写 `.mkv` |
| `设置项 rename_extensions 的每一项都必须以点开头（如 ".mkv"），收到 'mkv'。` | 设置页那一栏同理；**留空是合法的**（= 未设置） |

> 💡 前置信号：**保存媒体库表单时，覆盖卡片里的错误会直接标在卡片上**（并自动把开关打开），
> 不用去猜是哪一层的错。

### 23.1 改了后缀白名单却没生效

有效白名单按 **库级覆盖 → 全局设置 → 处理器清单** 三层取**第一个非空值**。所以常见原因是：

1. 某个库的覆盖里写了 `extensions` —— 它会**盖过**你在设置页写的那份。去库编辑页看那张卡片的开关；
2. 设置页那栏**留空** = 未设置（不是"一个都不认"）。**一个后缀都不想要**是做不到的；
3. 后缀是**大小写敏感**的：磁盘上是 `.MKV` 而清单里写 `.mkv` 就**不匹配**；
4. 硬链接阶段不看后缀（它整棵树链接过去）—— 没改名不代表没被处理。

---

## 24. 库日志文件里混进了应用日志，会不会无限增长

**【现象】**

- `<data>/logs/hard_links.log`（或旧部署留下的副本）里除了
  `Source: … - Folder: … - Folder size: …` 与 `Deleted orphaned hard link folder: …`，还夹着
  `127.0.0.1 - "GET /api/tasks …" 200`、`任务 7：…`、Python 异常回溯等**与应用任务无关**的行。
- 文件体积只涨不缩，担心它把数据卷撑满。

**【原因】** 写这个文件的处理器必须把 `FileHandler` 挂在 **root logger** 上（**A5 契约**：
`logging.basicConfig` 在 `root.handlers` 非空时是 no-op，格式与编码只能由
应用保证）。root 是**进程级单例** ⇒ **整个应用**的日志记录（uvicorn 访问日志、执行器任务日志、异常回溯）
都会顺着这个 handler 进文件。

**【解决步骤】**

1. **该 handler 上挂了白名单过滤器**（`app/script_adapter.py` 的
   `_BookkeepingFilter` / `_LOG_BOOKKEEPING_PREFIXES`），只放行两类记账行：
   - `Source: <源目录> - Folder: <文件夹> - Folder size: <字节>`（必须**同时**含 ` - Folder: ` 与
     ` - Folder size: `，"形似而实非"的行会被丢弃）；
   - `Deleted orphaned hard link folder: <目录>`。

   其余记录在这个文件上被丢弃 —— `docker compose logs` 与任务详情页**不受影响**（过滤是 handler 级的）。
2. **历史行不会被清理**：过滤**只挡新写入的行**，已经写进文件的旧行**一个字节都不删** —— 它们是
   "跳过 / 改名"的判断依据（`_parse_log_line` 的解析契约未变）。所以**文件不会自己变小**，只是不再被灌。
3. 老文件里的噪音行**无害**，不必手工删；真要瘦身，唯一安全做法是**整份删掉让该库全量重扫一次**
   （见场景 4 / 5），代价是一次全量运行与一批无害的 `File exists`。
4. 自查（容器内）：

   ```sh
   # 记账行的条数（正常是"处理过的文件夹数"量级）
   docker compose exec media-ingest grep -c 'Folder size:' /data/logs/hard_links.log
   # 有没有混进访问日志（旧文件里会有；新写入的行不该再有）
   docker compose exec media-ingest grep -c '"GET /' /data/logs/hard_links.log
   ```

> 💡 **"上限"是另外两个设置项**：`max_task_log_lines`（默认 **5000**，单个任务）与 `max_history`
> （默认 **2000**，任务条数）管的是**任务日志与任务历史**，与这个库日志文件无关。库日志
> （`hard_links.log`）与两个数据文件（`bangumicache.log` / `moviecache.log`）都是**数据型**的，
> 按设计**完全不清理**。

---

## 25. 文件没被改名，停在原地（L3 特典排除）

**【现象】**

- 媒体库里某部作品的目录下，`NCOP.mkv`、`Preview 01.mkv` 这类文件**名字和源目录里一样**（没有套上集号），
  也**没有**被移进任何子目录 —— 它们就停在原来的 `Season N/` 里。
- 任务详情页的 `extras_classified` 大于 0，日志里有一条中文 INFO
  「特典保留原名（判据命中，未改名、未移动）：Show A/Season 1/NCOP.mkv」。

**【原因】** 这是 **L3 特典排除**：文件名命中设置页「**特典文件排除**」
（`extras_patterns`，出厂默认 `NCOP` / `NCED` / `Preview` / `Creditless` / `Info` / `予告編` / `CM集` 七条正则）⇒
该文件**退出改名流程** —— **不改名、也不移动**（保留原名留在原位置）。
判据对**含扩展名的文件名**做大小写不敏感的正则搜索，**只作用于当前作用域目录的直接文件**。

> ⚠️ 命中特典判据的文件**不改名、也不移动**（保留原名留在原位置）—— 不会创建 `<作品目录>/其他/`。

**【解决步骤】**

1. **确认这是不是误判**：看那些没改名的文件名。真特典（`NCOP` / `Preview` / 菜单 / 预告）⇒ 一切正常，
   不需要做任何事；**正片被误判** ⇒ 走第 2 步。
2. **修正判据**（设置页「特典文件排除」卡片，一行一条正则）：把太宽的裸词收紧成锚定式，例如把 `NCOP`
   改成 `^NCOP$`，或删掉误伤的那一条；保存时非法正则会**当场报错**（点明第几条、哪个模式编译失败）。
   想彻底关掉这个功能 ⇒ 清空成**空数组**（保存后 `extras_classified` 恒为 0，所有文件照旧走正常改名）。
3. **改完判据后不用做别的**：这些文件本来就**没有被改名、也没有被移动**。
4. **旧部署留下的 `其他/` 想还原**：手工把里面的文件移回目标位置即可（它们是硬链接，同一文件系统内移动
   **不占额外空间**）；移回后下一个会改名的文件照旧按新判据处理。
5. **别把它当丢文件**：命中文件**没有被删除、没有改名、也没有被搬走**，只是保留原名停在原地。
   想核对，用任务结果的 `samples.extras_classified`（每条 `{dir, name, size, pattern}`，
   `pattern` 就是命中的那条判据）。

> 💡 常见混淆：`reject_dirs`（处理器自己的字段）只匹配**目录名**，所以与正片平级的散装 `NCOP.mkv`
> 从来不在它管辖范围内 —— L3 补的就是这个洞。相关：场景 15（文件没被改名）、
> [`docs/guides/USER_GUIDE.md`](USER_GUIDE.md) §8.7.1、[`docs/guides/FAQ.md`](FAQ.md) 第 37 问。

---

## 26. 待办看板：分页、行首圆圈、一键检测

**【现象与原因】**（以下全部是**设计行为**）

- **只看到 20 条**：看板一页 20 条，底部有「上一页 / 下一页」与「第 X / Y 页（共 N 条）」。
  同类明细**全部落库**（没有"每类最多 50 条"的截断）；
  要在接口层拿全量用 `GET /api/todos?limit=…&offset=…`（`limit` 上限 200）。
- **打钩后那一行没消失**：刻意如此 —— 刚勾掉的行会**留在原地变灰**（内部用 `board.pinned` 钉住），
  直到你**翻页 / 换筛选 / 手动刷新**。这样"我刚刚勾掉的是哪几条"一眼可见。
- **找不到「打钩」按钮**：打钩是**行首的圆圈**（`circle` 图标）—— 点一下圆圈里出现**绿勾**、整行变灰；
  再点一下恢复未完成。**筛选默认就是「未完成」**。
- **一键检测说"没有可标记的"**：只能检测**记录里带得出单个文件路径**的种类 ——
  `link_failed`（目标文件已存在 ⇒ 链接补上了）、`rename_suffix` / `unresolved_episode`
  （记录里的最终文件已不在那个路径上）。**`task_failed`、`source_integrity` 与 `unscoped_works` 永远检测不了**（它们描述的是
  一次任务 / 整棵树 / 目录结构，没有单个文件可核对）—— 弹窗里会如实列出这些种类，**别把"没检测出来"当成"没问题"**。

**【解决步骤】**

1. **翻页**：用看板底部的「上一页 / 下一页」；条目比预期少就先去掉种类筛选再看一遍。
2. **一键检测**：点筛选栏右边的「一键检测」→ 弹窗里逐条**勾选**要标记的（默认全选，可取消）→
   点「标记完成（N）」。**打钩前会再核对一遍**实况，实况变了的那条会被跳过并在提示里说明原因；
   检测接口本身**纯只读** —— 弹窗打开期间不会改动任何待办。
3. **误勾了**：在行首圆圈上再点一下就恢复未完成（`reopen`）；**打钩从不删数据**。
4. **看不到已完成的条目**：把状态筛选切到「已完成」或「全部」。
5. **接口层面**：`GET /api/todos/detect`（只读）与 `POST /api/todos/detect-resolve`（体 `{"ids":[...]}`，
   返回 `{requested,resolved,skipped[],missing[]}`），契约见 [`docs/contracts/API.md`](../contracts/API.md) §5.9.6 / §5.9.7。

> 💡 `detect_evidence` 是**人话证据**（例如「记录里的最终文件名已不存在：Show A/Season 1/…」），
> 弹窗里会显示它 —— 证据对不上就别勾。相关：[`docs/guides/USER_GUIDE.md`](USER_GUIDE.md) §2.5。

---

## 27. 作品记录里显示「已不在」/「未记录」怎么办

**【现象】**（「作品记录」**页** —— 左侧栏里的独立页面，或媒体库卡片上的「作品记录」按钮，见 [`docs/guides/USER_GUIDE.md`](USER_GUIDE.md) §6.5）

- 某一行的「目标里是否还在」列显示 **已不在**（接口字段 `target_exists: false`）—— 目标目录里这个作品文件夹**没了**；
- 或「是否已记录重命名」列显示 **未记录**（接口字段 `rename_recorded: false`）—— 本库的**重命名缓存**里没有这个作品；
- 页面的**信息行**里还可能标出两本账的差集（**按整个库算**，不受当前页影响）：`rename_missing`（在作品账本里、但重命名缓存里没有）/ `rename_only`（只在重命名缓存里、账本里没有）。

**【原因】** 这两列是**两条独立的事实**，各自有各自的来源，**应用不会自动"修复"它们**：

| 显示 | 含义 | 常见成因 |
|---|---|---|
| 目标里**已不在** | 目标目录里这个作品文件夹不在了 | 你手工删了它、被孤儿清理删过（[场景 6](#6-目标目录被自动删除了孤儿清理)）、磁盘/挂载出过问题 |
| **未记录**（重命名） | 本库的重命名 JSON 缓存里没有这个作品 | 这个作品只跑过建链、没跑过重命名；处理器是 `hardlink_only`；或缓存被手工删过 |
| `rename_missing` / `rename_only` | 两本账对不上的名字 | 上面两种情形的组合 —— 账本刻意是**作品级**的，**不保证文件级完整性** |

> 📌 **差集只是提示，永不自动修复。** 应用不会因为你看到 `rename_missing` 就去补一次重命名，也不会去删 `rename_only` 里的东西。你在目标目录里的**手动增删改一律保留**。

**【诊断命令】**

**【浏览器】** 左侧栏 →「作品记录」（或媒体库卡片 →「作品记录」按钮，会自动选中那个库）；信息行的「命中 N 条 / 本库共 M 条」与表格里那两列状态就是答案。
**【NAS 宿主】**

```bash
# 本库的账本（每行：folder, size, linked, target_exists, rename_recorded, target_path…）
curl -s -b cookies.txt "http://192.168.1.100:18081/api/libraries/1/works?limit=200&offset=0"

# 目标里的作品文件夹到底在不在
ls -d /vol1/1000/Library/Bangumi/'Show A'

# 本库的重命名缓存（响应里的 rename_log_path 就是它）
docker compose exec media-ingest cat /data/logs/bangumicache.log
```

**【解决步骤】**

1. **只想把名字重新捋一遍**（不重建链）：在「作品记录」里勾选该作品 →「**立即重命名**」（接口 `POST /api/libraries/{id}/works/rename`，返回 **202 + Task**，`kind="rename"`、`scope={"mode":"works","folders":[…]}`）。⚠️ 处理器是 `hardlink_only` 的库会返回 **400**（它没有重命名规则）；库已停用返回 **409**。
2. **目标里已不在、想让它重新出现**：勾选该作品 →「**删除记录**」（弹窗点「确定删除」即执行，⚠️ **会连带删除目标目录里的整个作品文件夹**，见[场景 29](#29-删除后目标里出现--2-后缀文件)与 [`docs/contracts/API.md`](../contracts/API.md) §3.11 的警告）⇒ 下次**运行**本库时它会自动补做（重新建链 + 重命名），**其余作品不受影响**。
3. ⚠️ **别想着"重新建链但保留产物"**：对单个作品跑 `scope.mode="folder"`（可带 `force=true`，只有接口能发）**做不到"只补缺的那几个文件"** —— 建链用的是源目录里的**原名**，目标里已改好名的同一 inode 会被**再链一份**，目标文件夹里于是出现同一 inode 的**两个目录项**、并多出 ` (2)` 后缀（实测：2 个文件变 4 个，`nlink=3`，不占额外空间）。要干净重建就走第 2 条（删记录 ⇒ 下次运行重做），详见 [`docs/meta/KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-9。
4. **两本账差集长期不收敛且你不在意**：**什么都不用做** —— 差集是提示，不是错误，也不会让任务失败。
5. 接口与字段的完整契约见 [`docs/contracts/API.md`](../contracts/API.md) §3.10；账本语义见 [`docs/guides/USER_GUIDE.md`](USER_GUIDE.md) §6.5。

---

## 28. 删了作品记录，下次运行还是没重做

**【现象】** 在「作品记录」里删掉某个作品（接口返回 `deleted: true`），下次运行本库**看起来什么都没发生** —— 目标目录里还是那些文件，或者只重建了硬链接、**名字一个都没改**。

**【原因】** 一个作品要"重做"，得**两本账同时清掉**；只清一半就会出现"漏做一半"：

| 只清了 | 结果 | 日志关键字 |
|---|---|---|
| 只清**作品账本**（`link_records`） | 建链阶段会重跑（可能报一批无害的 `File exists`），但**重命名阶段仍会跳过** | `Skipping folder: Show A (already processed at 2026-10-09 01:22:03)` |
| 只清**重命名缓存**（JSON） | 重命名会重跑，但建链阶段仍然被跳过 | `文件夹已记录且体积一致，跳过: Show A` |

**正常的删除接口是"两本一起清"的**（响应里的 `ledger_removed` 与 `rename_removed` 会各自列出被清掉的名字）。所以你看到"删了没重做"，通常是下面这几种：

1. 你不是用「删除记录」删的 —— 而是手工删了数据库行 / 手工改过 `hard_links.log`，**重命名缓存一个字节没动**；
2. `data/` 目录被**备份恢复 / 配置同步**覆盖过，把旧的 JSON 缓存又带回来了；
3. 你只删了作品记录，但**没有再跑一次任务**（删除本身**不会**触发重跑，只是"下次运行时补做"）。

**【诊断命令】**

**【NAS 宿主】**

```bash
# 这次删除到底清了哪两本账（对照 ledger_removed / rename_removed 是否都列出了该作品）
curl -s -b cookies.txt -X POST http://192.168.1.100:18081/api/libraries/1/works/delete \
  -H 'Content-Type: application/json' -d '{"folders":["Show A"],"dry_run":true}'

# 重命名缓存里还有没有它（in_ledger / rename_recorded 两个布尔值直接给答案）
curl -s -b cookies.txt "http://192.168.1.100:18081/api/libraries/1/works" | grep -A2 'Show A'

# 任务日志里搜跳过关键字
docker compose exec media-ingest grep -n 'Skipping folder' /data/logs/bangumicache.log
docker compose exec media-ingest grep -n '文件夹已记录且体积一致，跳过' /data/logs/hard_links.log
```

**【解决步骤】**

1. **重新走一遍「删除记录」**（推荐）：它会**同时**清 `link_records` 的行与本库重命名缓存里的同名条目（[`docs/contracts/API.md`](../contracts/API.md) §3.11 的"删三处"）。注意 **dry_run 预演不会清任何东西**，必须用 `dry_run=false, confirm=true` 提交。
2. **确认缓存真的清了**：再跑一次 `GET /api/libraries/{id}/works`，看 `rename_entries` 是否下降、该作品的 `rename_recorded` 是否变 `false`。
3. **然后主动跑一次任务**：删除**不会**自动触发运行。
4. **如果只是想重命名**：别绕这么大圈，直接「立即重命名」（[场景 27](#27-作品记录里显示已不在未记录怎么办) 第 1 步）。
5. ⚠️ **删库也要知道**：`DELETE /api/libraries/{id}` 会清掉本库的作品账本（响应里的 `link_records_removed`），但**不动重命名 JSON 缓存** —— 缓存是历史记录，删库 ≠ 重命名没发生过（见 [`docs/contracts/API.md`](../contracts/API.md) §3.7）。

---

## 29. 删除后目标里出现 ` (2)` 后缀文件

**【现象】** 删掉作品记录（或账本为空的库第一次全量运行）之后，目标目录里出现 `Show S01E01 (2).mkv`、`Show S01E02 (2).mkv` 这类带 ` (2)` 后缀的文件；待办看板里也多出 `rename_suffix` 条目。

**【原因】** 这是**同一份数据的第二个目录项**，不是复制、不占额外空间、更不丢数据：

- 你（或上一次重命名）已经把目标里的文件改成了**新名字**，但**源目录里还是原名**；
- 重做时**建链阶段**按"**原名**"再链一份 ⇒ 目标里同一个 inode 出现两个目录项；
- **重命名阶段**想把原名那份改成新名字时，发现**干净名已被占用** ⇒ 按 L2「防丢」加 ` (2)`、` (3)`… 后缀保留（**永不覆盖**）；
- **账本为空的库**（沿用旧数据卷的部署）第一次全量运行会把每个作品重新建链一次，所以**这类库更容易看到它**（见 [`docs/guides/USER_GUIDE.md`](USER_GUIDE.md) §6.4）。

**【诊断命令】**

**【浏览器】** 任务详情 → `result.renamed_with_suffix` 与 `samples.renamed_with_suffix`（每条 `{dir, old, clean, target, size, reason, displaced}`，`reason` 是 `within_run` 或 `existing_target`）。
**【NAS 宿主】**

```bash
# 确认重复名字指向同一份数据（同一 inode、nlink >= 2）
ls -li /vol1/1000/Library/Bangumi/'Show A'/Season\ 1/ | head

# 哪些文件被加了后缀（建链阶段的输出也在这份日志里）
grep -n '同名冲突' /data/logs/hard_links.log | tail -n 20
```

**【解决步骤】**

1. **判断是不是重复**：`ls -li` 里两个名字的 **inode 相同**（第一列数字一样）、`nlink >= 2` ⇒ 就是同一份数据的两条目录项，**删掉多余的那条不减少任何数据**。确认无误后删掉带 ` (2)` 的那一条即可。
2. **想避免它再出现**：别用"整库强制重跑"（已停用，返回 400）。**账本为空的库**（换了数据卷、清了 `data/`）建议用新库、或先清空目标目录、或逐作品处理（见 [`docs/guides/FAQ.md`](FAQ.md) 第 40 问）。
3. **同一作品反复出现后缀**：说明你手工改过的名字与源文件名长期不一致。要么在「作品记录」里删掉该作品的记录让它在**空目标**上重做，要么接受"目标里保留两条目录项"（数据不受影响）。
4. **绝不手工去改源文件名**来"对齐" —— 那会让下载工具的下一次校验认不出文件。改名字的正确位置是**重命名规则**（[`docs/guides/USER_GUIDE.md`](USER_GUIDE.md) §5）。
5. 相关：L2「防丢」的完整口径见 [`docs/contracts/BASELINE.md`](../contracts/BASELINE.md) L4。

---

## 30. 作品记录页搜不到某个作品

**【现象】** 在「作品记录」页的搜索框里敲了名字，却是 **0 条**（信息行显示「命中 0 条 / 本库共 M 条」），或者搜出来的条数和你的印象对不上。

**【原因】** 搜索是**朴素子串**匹配，规则比直觉窄：

| 你输入的是 | 实际行为 |
|---|---|
| 目标里**改名后**的名字 | **搜不到** —— 匹配的是**作品名**，也就是**源目录里的一级文件夹名**；改名不会让它变 |
| `%作品%`、`S01%` 这类"通配符"写法 | **搜不到**（或搜出奇怪结果）—— `%`、`_`、`\` 都是**字面字符**，不是通配符 |
| 别的库里的作品名 | **搜不到** —— 只搜**当前选中的那个库**的记录 |
| 作品**内部**的文件名、目标路径片段、日期 | **搜不到** —— 只匹配作品名，不看这些 |
| 名字里的普通字符（ASCII 大小写随意） | **能搜到** —— 子串匹配，ASCII 大小写不敏感 |

另外：**M = 0** 时不是"搜不到"，而是这个库**根本没有记录**（还没跑过建链、或没任何作品建链成功）。

**【诊断命令】**

**【浏览器】** 先把搜索框**清空**，看信息行的「本库共 M 条」：**M > 0** 说明记录在，是你**搜的词**不对；**M = 0** 说明**库选错了**或这个库确实没记录。同时确认「作品」列显示的是**源目录里的原名**，而不是目标里改名后的名字。
**【NAS 宿主】**

```bash
# 本库的账本（不传 q 就是全部；folder 正是搜索匹配的字段）
curl -s -b cookies.txt "http://192.168.1.100:18081/api/libraries/1/works?limit=50&offset=0"

# 源目录里的原始一级文件夹名（"作品名"就是它）
ls -1 /vol1/1000/Download/Bangumi/
```

**【解决步骤】**

1. **换词**：用**源目录里的原名**中的几个字重搜 —— 不要用目标里改名后的名字，也不要加 `%` / `_`。
2. **换库**：确认页面上方的**库选择器**选的是跑过这个作品的库（从媒体库卡片进来时它已预选成那个库；在这里直接切换即可，切换会**清空已勾选的作品**）。
3. **确认记录存在**：清空搜索框，看「本库共 M 条」以及列表里有没有这个作品。确实没有就跑一次该库的「手动执行」（**已建链的作品**才会记账）。
4. **搜到了但状态不对**（显示「已不在」/「未记录」）⇒ 转[场景 27](#27-作品记录里显示已不在未记录怎么办)。
5. 页面各部位与两个按钮的动作见 [`docs/guides/USER_GUIDE.md`](USER_GUIDE.md) §6.5；接口与字段契约见 [`docs/contracts/API.md`](../contracts/API.md) §3.10。

---

## 31. 文件名少了一段（命名里用了不存在的变量名）

**【现象】** 任务**成功**，同一批文件的命名规律却"缺一截"：例如模板本该渲染成 `Show A S01E01 (1080p).mkv`，实际是 `Show A S01E01 .mkv`（多一个空格）或直接 `Show A S01E01.mkv`；任务告警里有一条「有 N 个文件的命名里用了不存在的变量…」，任务详情里能看到 `unknown_params` 与 WARNING 日志「命名里的未知变量被留空（文件名少了一段）：…」。**保存期**改写命名时则相反：直接被 **422** 拦住，提示「段里用了未知变量 {xxx}。可用变量：…；自定义名字要先在 naming.expressions 里定义。」

**【原因】** 命名段序列里的 `param` 是一个**上下文里没有的名字**（**KI-11**）。段序列是"按名字取变量再拼字符串"，取不到 ⇒ 那一段**留空**、其余照拼 —— 文件名因此少一段，但改名本身是成功的（引擎**不会**为它造一个假的变量值，也不会让任务失败）。

用**合法名单**判断：`naming.segments[].param` 与模板 `{名字}` 只能是

- 变量表 12 个：`title` / `filename` / `stem` / `ext` / `extname` / `season_raw` / `season` / `episode_raw` / `episode` / `suffix` / `works_dir` / `rel_dir`；
- 引擎内部键 `episode_resolved`；
- **本处理器 `naming.expressions` 里定义的名字**（自定义派生变量）。

**它为什么会出现**（界面里正常操作很难造出来：段参数是**下拉框**、模板框当场校验）：

| 入口 | 说明 |
|---|---|
| 手写 / 外部生成的 YAML | `/data/config/processors/<id>.yaml` 里 `naming.segments` 直接写 `{"param": "titlee"}`；配置文件同步**只做形状校验、不校验名字**；这类文件**导入时报错**（老规格仍在库里、仍在跑） |
| 直接调 API | `POST`/`PUT /api/processors` 或库级覆盖提交 `naming.segments`（**422**；**老规格**只有在运行期才可见） |
| 改名/删除了表达式 | 段引用了 `naming.expressions` 里的自定义名，**后来把那个表达式删了**（覆盖是**按名增量合并**，删掉就真的没了）⇒ 段名变成未知 |
| 模板里引用了变量表外的名字 | 模板与段写法对 `{episode_resolved}` 与自定义表达式名的口径**一致**，两条写法都能读到 |
| 沿用旧数据卷 | 老规格里本来就有的错名字，现在**第一次可见** —— 行为没变，只是不再静默 |

**【诊断命令】**

**【浏览器】** 仪表盘/任务页 → 该任务详情：告警里的汇总行 + 逐条 WARNING；`result.unknown_params`（计数）与 `result.samples.unknown_params`（每项 `{label, file, params}`）；处理器编辑页看命名段与「命名表达式」区域是否对得上（不在名单里的名字在下拉框里带 **（未知）** 标记）。

**【NAS 宿主】** 直接看库里存的规格与最近一次任务结果：

```bash
# 1) 处理器规格里真实的段序列 / 表达式名（JSON 字段名是 spec_json）
docker compose exec media-ingest python - <<'PY'
import json, sqlite3
c = sqlite3.connect('/data/mlm.db')
for pid, name, spec in c.execute('SELECT id, name, spec_json FROM processors'):
    n = json.loads(spec).get('naming', {})
    print(pid, '|', name, '| segments=', n.get('segments'), '| expressions=', list((n.get('expressions') or {}).keys()))
PY

# 2) 最近一次任务里的未知变量计数与明细（列名是 result，不是 result_json）
docker compose exec media-ingest python - <<'PY'
import json, sqlite3
c = sqlite3.connect('/data/mlm.db')
row = c.execute('SELECT id, result FROM tasks ORDER BY id DESC LIMIT 1').fetchone()
r = json.loads(row[1] or '{}')
print('task', row[0], 'unknown_params =', r.get('unknown_params'), r.get('samples', {}).get('unknown_params'))
PY

# 3) 日志里的逐条告警
docker compose logs --tail=500 media-ingest | grep -F '命名里的未知变量被留空'
```

**【解决步骤】**

1. **找到那个名字**：看 `samples.unknown_params` 里的 `params`（或日志括号里的变量名），再在上面第 1 条命令的输出里找到是哪一段（`segments` 第几项）—— 通常是拼错、或是已被删掉的表达式名。
2. **改对**：在处理器编辑页把该段的参数从下拉框重选（下拉框只列出可用名单，选完保存即 422 消失）；若是模板写法，把 `{错名字}` 改成合法名字。
3. **要保留自定义名字**：在「命名表达式」里补上同名定义（例如 `custom = ...`），它就会重新变成可用名字（**不用**改段）。
4. **重跑**：该文件的名字**不会自动修正** —— 段渲染出的是"少一段的名字"，重跑时幂等判据看的是体积/账本，不比对名字；想让已改错名的文件回到正确名字，按 [`docs/meta/KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-11 的说明处理（改好命名后**删掉该作品的账本记录再跑**，或手工改回目标里的文件名）。
5. 想确认修复生效：跑一次该库，看 `unknown_params == 0`、任务告警里不会有那条汇总行。

> **为什么任务不算失败？** 因为"文件名少一段"不影响任务的核心保证（不写源目录、不覆盖、不丢文件），而**让它失败**会让整个库因为一个历史错名字而跑不动 —— 这是刻意的取舍（取舍理由见 KI-11）。**新写的错名字在保存期就被拦住**，运行期这一层是给"库里早先存下的老配置"兜底的。

---

## 未覆盖项与原因

下面是**本故障排查文档没有覆盖**的内容，以及为什么没覆盖 —— 目的是让你知道"这里没有答案不是疏忽"。

| 未覆盖项 | 原因 |
|---|---|
| **在任何非 Linux 环境（Windows/macOS）上真跑任务** | 应用按容器内路径（`MEDIA_ROOT` 下的源/目标路径，单挂载形态下=宿主真实路径）配置与执行；在 Windows/macOS 测试环境里这些路径不是真实挂载点，`app/executor.py` 直接 `os.path.isdir()` 它们 ⇒ 必然失败。这是**环境限制**，不是缺陷。见 [`docs/contracts/COMPATIBILITY.md`](../contracts/COMPATIBILITY.md)。 |
| **跨挂载建硬链接的"绕过办法"** | 内核层面就不允许（`do_linkat()` 比较挂载身份 ⇒ `EXDEV`，见场景 1），且设计上**永不提供**复制回退（B8）。任何"复制模式"都会让媒体库与下载各占一份空间，违背前提。唯一合规做法是把源与目标放进**同一个挂载**（单一共同父挂载）。 |
| **在"源目录只读"的形态下实证探测** | 这个形态**不存在**（场景 3）：单挂载形态下源与目标同挂载、必然可写，探针因此可以**真的试一次** `os.link`（只读源目录、临时文件建在目标侧、用完即删）。仍无法实证的情形只剩"源目录里找不到可用的普通文件样本" ⇒ 预检返回 `null` 而**不是** `true`（场景 21）。 |
| **非 UTF-8 的 locale / 文件名** | 容器固定 `LANG=C.UTF-8`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`（B10）。**没有**在 GBK/非 UTF-8 locale 下验证过；含无效 UTF-8 字节的文件名、或宿主与容器编码不一致的场景未覆盖。 |
| **命名与建链的既定边界行为**（名称含 `" - "` 的文件夹、`ENone`、`Season 1 (2018)` 被忽略、等体积跳过…） | 它们是**冻结的既定行为**（L1–L9），本应用**不修**。本篇只说明"如何识别与绕开"，不给"修改行为"的方案。**例外**：① 处理器**没配** `episode_fallback`（`null`）时，引擎不会把**字符串** `None` 造进文件名，而是对该文件**保留原文件名** —— 这是"可见地跳过"路径；`legacy` 预设显式配了 `"None"`，行为与 L3 完全一致 —— 而**内置 `bangumi` 的推荐默认也是 `null`**（新装 / 新建处理器 / 「恢复默认」选 `default` 都拿到它），所以默认就落在新路径上。② **L2「防丢」不是 `os.rename` 的默认行为**：`os.rename` 在目标同名时会 POSIX 静默覆盖（真实事故里全库丢过 114 个文件）、Windows 报 `[WinError 183]`；引擎改为按体积让位 / 加 ` (2)`… 后缀保留 / 保留原名，**无条件生效（含 `legacy`）** —— 详见 L4。 |
| **超大规模（十万级文件）与性能** | 没有做过压力测试与容量上限验证。队列全局串行（B11）、日志"读-改-写整份"（L1）都是天然瓶颈。首跑大库耗时长属预期。 |
| **qBittorrent 各版本/各客户端（如 qBittorrent-nox、第三方 WebUI）的外部程序参数差异** | 只按 `%N/%I/%L/%G/%F/%D` 这套占位符说明；不同版本/Qt 环境下的引号与转义差异需以 qBittorrent 官方文档为准。 |
| **反代 / HTTPS / 域名访问下的 Cookie 与 SSE** | 会话 Cookie 为 `HttpOnly; SameSite=lax`（未设 `Secure`），反向代理终止 TLS 的部署**未验证**；SSE（`/api/tasks/{id}/stream`）经 Nginx 等反代时可能需要关闭缓冲，本篇未覆盖。 |
| **备份/迁移的完整流程** | 只在关键处给了"先 `cp -a data`"的提醒；完整备份与迁移步骤见 [`docs/guides/DEPLOYMENT.md`](DEPLOYMENT.md)。 |
| **多实例并发访问同一数据卷** | 明确**不支持**：SQLite + 单工作线程 + 日志"读-改-写整份"（L1/B11）都假定单实例。 |

## 最小待确认项

这些是本篇里**无法在没有真实 NAS 环境的情况下确认**的点。请在你的环境上逐条核对（每条都给了验证方法）：

| # | 待确认项 | 为什么需要确认 | 怎么确认 |
|---|---|---|---|
| 1 | 你 NAS 上 `/vol1/1000/Download/qbittorrent` 与 `/vol1/1000/Library` 是否在**同一个挂载**里（注意：同一个挂载 ≠ 同一文件系统） | 决定场景 1 是否会发生 —— 不同挂载 ⇒ 硬链接必然 `EXDEV`，与 `st_dev` 是否相同无关 | **【NAS 宿主】** `findmnt -T /vol1/1000/Download/qbittorrent -o TARGET` 与 `findmnt -T /vol1/1000/Library -o TARGET`，两条的 `TARGET` 是否**相同**（`stat -c '%d'` 相同**不**能作为依据） |
| 2 | 你的数据卷里**有没有**一份更早的部署写下的 `<data>/logs/hard_links.log`（`Source:` 与当前 `source_dir` 不一致，典型是历史遗留的 `/downloads/...`） | 有的话该库会**每次都全量重扫**并刷 `File exists`（场景 5）；没有就是干净的首次运行（场景 4） | **【NAS 宿主】** `grep -o 'Source: [^ ]*' /vol1/1000/Docker/media-ingest/data/logs/hard_links.log \| sort -u` —— 输出应当与库的 `source_dir` 逐字相同；出现 `/downloads/...` 就是旧记录 |
| 3 | 你的 `HOST_PATH_MAP` 是否与实际挂载一一对应（方向：容器→宿主） | 单挂载形态下容器路径 = 宿主路径，它是**恒等映射、只影响界面展示**（**不参与**日志匹配，A13）；写错会让系统信息页显示的宿主路径不对 | **【浏览器】** 系统信息页 `path_map` 与 `mounts[].host_path` 是否与你 NAS 上的真实路径一致 |
| 4 | 每个库的**重命名日志**（`<data>/logs/<log_file>`）是否与库一一对应、没有两个库共用一份 | 两个库共用一份日志会出现「体积一致 ⇒ 静默跳过」的假象；应用已保证自定义处理器不会借用内置日志（`builtin_log_files()`/`avoid_builtin_log_file()`） | **【浏览器】** 各媒体库详情的 `log_file` / `log_path`；**【NAS 宿主】** `ls -l /vol1/1000/Docker/media-ingest/data/logs/` |
| 5 | 你的 NAS 是否 ARM64、`python:3.11-slim` 是否已在本地 | 决定构建期是否需要联网拉取/是否会 `exec format error`（场景 12） | **【NAS 宿主】** `uname -m; docker images \| grep python` |
| 6 | 端口 18081 是否被别的东西占用 | 决定是否需要改 `.env` 的 `PORT`（场景 10） | **【NAS 宿主】** `ss -lntp \| grep 18081` |
| 7 | qBittorrent 的「运行外部程序」在你这版上的实际占位符与转义 | 决定场景 8/9 的命令能否照抄 | 在 qBittorrent 里配好后，看**任务**是否出现；或 **【NAS 宿主】** `docker compose logs \| grep -i hook` 是否看到请求（看到 401 就说明网络通、token 错） |
| 8 | 目标库目录的属主与容器内 `id` 是否一致 | 决定场景 7 的权限修复怎么做 | **【容器内】** `id`；**【NAS 宿主】** `stat -c '%u:%g %a %n' /vol1/1000/Library` |
