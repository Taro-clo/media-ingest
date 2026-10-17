# 部署手册（Docker / NAS）

> **本文解决什么**：把本应用从零装到「浏览器能打开、qBittorrent 能触发」—— 装前确认、共同父挂载、硬链接为什么只能在同一个挂载里建、源目录靠什么保护、升级、备份恢复、ARM64、非 root 运行。
> 面向**不懂代码的 NAS 用户**：命令可直接复制，每条前面标了**在哪执行**；前提是 NAS 已装好 Docker（含 `docker compose`）。
> **本文不负责**：网页每个按钮怎么点 → [`USER_GUIDE.md`](USER_GUIDE.md)；接口 → [`../contracts/API.md`](../contracts/API.md)；报错排查 → [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) / [`FAQ.md`](FAQ.md)。

---

## 0. 占位符（**照抄前换成你自己的**）

| 占位符 | 文中示例值 | 换成什么 / 在哪改 |
|---|---|---|
| `<NAS_IP>` | `192.168.1.100` | 你 NAS 的局域网地址 |
| `<MEDIA_ROOT>` | `/vol1/1000` | 下载源与媒体库的**共同父目录**（宿主路径）；compose 的 `x-media-root` 锚点（或环境变量 `MEDIA_ROOT`） |
| `<下载根>` | `<MEDIA_ROOT>/Download/qbittorrent` | qBittorrent 下载根（源） |
| `<媒体库根>` | `<MEDIA_ROOT>/Library` | 媒体库根（目标） |
| `<数据卷>` | `<项目目录>/data` | 数据目录的**宿主**路径；默认 = compose 同目录的 `./data`，要换位置改 compose 的 `x-data-dir` 锚点（或设环境变量 `MLM_DATA_DIR`）（§2.3） |
| `<项目目录>` | `/vol1/1000/Docker/media-ingest` | compose 项目目录（放 `docker-compose.yml` 处） |
| `<PUID>:<PGID>` | `auto`（识别为 `1000:1001` 之类） | 容器运行身份；默认 `auto` = 按 `<MEDIA_ROOT>` 属主自动识别（§1.3、§10） |
| `<HOOK_TOKEN>` | 设置页那串 | 回调 token，只在设置页拿（§5.1） |
| `<备份目录>` | `/vol1/1000/backup` | 你放备份的地方 |

> 下文出现的 `/vol1/1000/...` 一律读作「你的 `<MEDIA_ROOT>` 下的同名子目录」。它同时是 `docker-compose.yml` 与 `app/config.py` 里**出厂默认值**的形态，按默认路径部署时不用改。

**命令执行位置**：`【NAS 宿主】` = SSH 登录 NAS 或 NAS 的终端（不是容器里）；`【容器内】` = `docker exec -it media-ingest <命令>`；`【任意电脑】` = 你自己电脑。开 SSH 在 NAS 的「控制面板 → 终端机」。

---

## 1. 前置条件检查清单

**1.2 与 1.3 是最常导致「装了但一直失败」的两条。**

### 1.1 Docker、compose、目录

**【NAS 宿主】**

```bash
docker --version && docker compose version
docker info --format '{{.ServerVersion}} arch={{.Architecture}}'   # amd64 → 直接装；arm64 → §9
ls -ld <下载根> <媒体库根> <数据卷>      # 三个都必须在（数据卷没有就 mkdir -p）
```

`<下载根>` = 源，`<媒体库根>` = 目标，`<数据卷>` = 数据库/配置/日志等所有会变的状态（§2.3）。`docker compose`（v2）报错就试 `docker-compose version`，后文命令相应替换。

### 1.2 源与目标必须在**同一个挂载**上（关键）

```bash
findmnt -T <下载根> -o TARGET
findmnt -T <媒体库根> -o TARGET        # 两条 TARGET 必须相同，否则硬链接必然 EXDEV（§3）
```

> ⚠️ **不要拿 `stat -c '%d'`（`st_dev`）相同当依据**：内核 `linkat(2)` 比的是**挂载身份**，
> 同一 `st_dev` 的两个独立 bind 之间 `link()` 照样 `EXDEV`。容器内的最终判据是媒体库页的「验证」
> （`same_filesystem` 三态，§4.4）。

### 1.3 端口与运行身份（PUID / PGID）

```bash
ss -lntp | grep -q ':18081 ' && echo '18081 被占用，请换端口' || echo '18081 空闲'
id                                                    # 只是想确认属主；PUID/PGID 已默认 auto，可不填
sudo -u '#1000' test -r <下载根> && echo '源目录可读 OK'
sudo -u '#1000' test -w <媒体库根> && echo '目标目录可写 OK'
```

**运行身份默认 `auto`**：容器启动时按 `MEDIA_ROOT`（媒体共同父挂载）的属主自动取 uid/gid，
也就是"能读下载、能写媒体库"的那个用户 —— 通常不用你查 `id`、也不用填 `PUID`/`PGID`。
只有两种情况需要显式写数字：想固定成与目录属主不同的用户；或日志里出现
`无法从媒体挂载/数据目录识别属主` 的 WARN（说明挂载还没就绪/属主是 root）。
显式写了数字就完全按数字来，不再自动识别（见 §10）。

换端口只改 compose 文件头的 `x-port` 锚点（或设环境变量 `PORT`）；宿主与容器端口引用同一个锚点，改一处两边同步（§8.1）。没有 `sudo` 就用 `stat -c '%A %U %G %n' <路径>` 看权限位，再按 §10 调整。

### 1.4 qBittorrent 容器（建议）

```bash
docker inspect qbittorrent --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
```

必须能看到 `<下载根> -> <下载根>` 这一行（容器路径 = 宿主路径）：本应用把同一宿主目录挂到**同一个容器内路径**，回调传来的 `%F` 才能零转换直接用（§2.1、§5）。磁盘空间只需看**数据卷**是否有几百 MB 余量（硬链接几乎不占额外空间；`/data` 不必与下载/媒体库同挂载）。

---

## 2. 卷挂载完整表（**最重要的一节**）

本应用**只有 2 条 bind mount**（宿主目录 = 容器目录，不是 Docker 命名卷）：

| # | 宿主路径 | 容器路径 | 模式 | 用途 |
|---|---|---|---|---|
| 1 | compose 锚点 `x-media-root`（默认 `/vol1/1000`，可用 `MEDIA_ROOT` 覆盖） | 与宿主**完全一致** | `rw` | **单一共同父挂载**：源与目标都在它下面 ⇒ 天然同挂载，硬链接成立的前提（§2.1） |
| 2 | compose 锚点 `x-data-dir`（默认 `./data`，可用 `MLM_DATA_DIR` 覆盖） | `/data` | `rw` | 数据库、配置、日志、密钥（§2.3） |

镜像部署（`docker-compose.release.yml`）用 YAML 锚点写在文件头，environment / ports / volumes 全部引用同一处：

```yaml
x-media-root: &media-root "${MEDIA_ROOT:-/vol1/1000}"
x-data-dir: &data-dir "${MLM_DATA_DIR:-./data}"
# …
    volumes:
      - type: bind
        source: *media-root
        target: *media-root
      - type: bind
        source: *data-dir
        target: /data
```

源码构建（`work/docker-compose.yml`）用等价的短语法，改的是同名变量：

```yaml
    volumes:
      - "${MEDIA_ROOT:-/vol1/1000}:${MEDIA_ROOT:-/vol1/1000}"
      - "${MLM_DATA_DIR:-./data}:/data"
```

> ⚠️ **源与目标同属一条可写挂载，"写不进去"这层保护不存在。** 单挂载形态下「源单独 `:ro`」与「跨路径建硬链接」**内核层面互斥**，只能二选一；替代它的是**可验证的保证**（代码层不写源 + 任务前后逐文件核对，§3.1）—— **这是降级、不是等价替代**，本文不粉饰。

### 2.1 为什么只挂一个共同父目录（以及 `:ro` 为什么退场）

**（a）容器路径必须等于宿主路径**：qBittorrent 的「运行外部程序」把 `%F`（**qBittorrent 容器视角**的路径，如 `/vol1/1000/Download/qbittorrent/Bangumi/Show A`）传进来。本应用容器路径与宿主一致 ⇒ 拿来就能用，不需要 `HOST_PATH_MAP` 翻译；改成挂 `/dl` 会让所有回调指向不存在的路径 —— 这是**契约**而非偏好（[`../contracts/ARCHITECTURE.md`](../contracts/ARCHITECTURE.md) §2）。

**（b）`:ro` 为什么退场**：内核 `do_linkat()` 比较挂载身份（`old_path.mnt != new_path.mnt` ⇒ `EXDEV`），与 `st_dev` / `fsid` / btrfs 子卷无关。拆成两条独立 bind ⇒ 两个挂载 ⇒ 硬链接永远建不出来（"任务全部 `link_failed` + `[Errno 18] Invalid cross-device link`"的根因），而只看 `st_dev` 的预检还会报"可以创建硬链接" —— **假绿**。源目录只读与硬链接可建只能二选一；本项目选后者 + 可验证的完整性核对。

### 2.2 媒体库挂载：`rw`，且与下载根同挂载

`rw` 必需（要建目录、建硬链接、**原地改名**）；媒体库根是**媒体库设置**里的参数（如 `<媒体库根>/Bangumi`），容器内不必非叫 `Library`。两者都由镜像内的应用代码执行（`app/processors/hardlink.py`、`app/processors/engine.py`），硬链接阶段的可观察输出是冻结契约（[`../contracts/BASELINE.md`](../contracts/BASELINE.md) §1，锁定位 `tests/test_hardlink_native.py`）。

### 2.3 数据卷：默认部署目录下的 `./data`，想放别处就改 `x-data-dir`（或设 `MLM_DATA_DIR`）

| 容器内 | 内容 |
|---|---|
| `/data/mlm.db`（+ `-wal` / `-shm`） | SQLite：账号、处理器与媒体库规则、任务历史、任务日志、设置 |
| `/data/secret.key` | 会话签名密钥（删了 = 所有人被登出） |
| `/data/config/libraries.yaml`、`/data/config/processors/<id>.yaml` | 规则文件镜像（与 DB 双向同步，§2.5） |
| `/data/logs/hard_links.log` | 硬链接台账（逐字格式见 §2.6 与 [`../contracts/BASELINE.md`](../contracts/BASELINE.md) B6） |
| `/data/logs/<log_file>` | **每个媒体库一条**重命名日志（`json_cache`，默认 `<slug>cache.log`） |

**默认数据卷 = compose 文件同目录的 `./data`**（镜像部署由文件头 `x-data-dir: &data-dir "${MLM_DATA_DIR:-./data}"` 定义，源码构建是 `"${MLM_DATA_DIR:-./data}:/data"`），
即部署目录下的 `data/`。不用配置、也不会在你没指定的地方（比如盘根）创建目录。

> 💡 **想放别处**：改 compose 文件头的 `x-data-dir` 一行（推荐，改一处即生效），或在 `.env` 里加 `MLM_DATA_DIR=/你的路径/data`，或命令行前缀 `MLM_DATA_DIR=…` 覆盖。
> ⚠️ **`MLM_DATA_DIR` 是可选变量**：不写就用上面的默认值。
> 📌 **备份 / 迁移 / 换机只需要搬这一个目录**（§7）。

**为什么必须是宿主目录**：容器是一次性的（`down`、重装、升级都会丢掉容器），状态放容器里等于没保存。
`/data` 不必与下载/媒体库同挂载，放 SSD 反而更好。

### 2.4 全新装的默认配置（内置处理器）

| 处理器 id | 名称 | `kind` | 执行体 | 登记的默认日志文件 | 文件作用域 |
|---|---|---|---|---|---|
| `hardlink_only` | 只建硬链接 | `hardlink_only` | `hardlink.py` | `hardlink_only.log`（不改名，通常不写入内容） | —（只建链接） |
| `bangumi` | 番剧 | `rename` | `engine.py` | `bangumicache.log` | `subdir`（`Season N` 子目录） |
| `movie` | 电影 | `rename` | `engine.py` | `moviecache.log` | `title`（标题文件夹直接取文件） |

完整字段（接受后缀、命名段、`episode_patterns`…）见 [`../contracts/PROCESSORS.md`](../contracts/PROCESSORS.md) §3。

**两个预设**（处理器页「恢复默认」时可选）：

| 预设 | 含义 |
|---|---|
| `default` | 推荐值：相对 `legacy` 有**四处增量** —— 番剧/电影各多一套**拒绝目录**规则、番剧 `episode_fallback` 留空（`null`，认不出集数就**保留原文件名**）、番剧 `episode_patterns` 是 9 条带边界约束的规则 |
| `legacy` | 最朴素的旧口径：`reject_dirs: []` + `episode_fallback: "None"` + 两条最宽松的集数规则 |

> ⚠️ **`episode_fallback` 不随升级自动改变**（播种只在 `processors` 表为空时发生）。想让已存在的番剧处理器"认不出就保留原名"：① 改 `/data/config/processors/bangumi.yaml` 的 `episode_fallback:` 为 `null`（双向同步，5 秒生效；等价 `PUT /api/processors/bangumi` 给 `null`）；② `POST /api/processors/bangumi/reset` + `{"preset":"default"}`（**整套**覆盖自定义）。
> ⚠️ **别用编辑页「集数兜底」输入框清空来表达"留空"**：清空提交空串 `""`，引擎当成"已解析"；反方向（`null` → 字符串）用输入框填 `None` 可以。

首次启动自动完成（无需人工）：`processors` 表为空 ⇒ 写入上表三个处理器的 `default` 预设 ⇒ 把 `libraries.processor` 映射为 `processor_id` ⇒ 立刻写出 `libraries.yaml` 与 `processors/*.yaml`。

**核对【NAS 宿主】**：`curl -s http://127.0.0.1:18081/api/processors`、`curl -s http://127.0.0.1:18081/api/config`（看每个文件的 sha256 / `in_sync` / `errors`）、`cat <数据卷>/config/processors/bangumi.yaml`。

### 2.5 配置目录（`/data/config`）：布局、双向同步、冲突处理

**单一真源 + 单向仲裁**：**运行时真源永远是 SQLite**，配置文件只是它的**可移植镜像**。

**布局**：`/data/config/libraries.yaml`（媒体库清单：processor_id、路径、匹配条件、log_file）+ `/data/config/processors/<id>.yaml`（一个处理器一个文件；内置三个 + 自定义）。

格式 **YAML 1.1**（`safe_load`/`safe_dump`，`allow_unicode=True`、`sort_keys=False`），UTF-8 无 BOM，首行 `schema_version: 2`，依赖 `PyYAML==6.0.3`；**正则一律单引号标量**（`pattern: '^Season (\d+)$'`）。目录由 env `CONFIG_DIR` 指定（默认 `/data/config`，在 `/data` 卷内，无需额外挂载）。应用**不读** `.env`。

| 方向 | 触发 | 行为 |
|---|---|---|
| **DB → 文件** | 任何通过 API 的成功写入（WebUI 保存） | 同一请求内**原子写出**（`*.yaml.tmp` → `os.replace`），新 sha256 记进 `config_files` |
| **文件 → DB** | 每 `config_sync_interval_seconds` 秒（**DB 设置项**，默认 **5**，区间 0–86400；**不是 env**） | 比对 mtime + sha256；哈希 ≠ 上次记录 ⇒ 判为外部编辑，执行导入 |
| 导入成功 | — | 一事务内替换 `libraries` / `processors`，写 `config_events`，页面提示「配置文件已从磁盘重新导入」，随后回写一次（规范化）对齐哈希 |
| 导入失败 | YAML 语法错、`schema_version` 不对、规格校验不过 | **不改数据库**（继续按旧规则跑），`GET /api/config` 的 `errors` 显示红色横幅，**你的文件不会被覆盖** |

删除语义：文件里消失的**自定义**处理器/媒体库随导入删除；**内置处理器永不删除**（只能 reset）。API 写入与后台导入持同一把锁，不会交错。想关掉自动导入：把 `config_sync_interval_seconds` 设 `0`，之后只在 `POST /api/config/reload` 时导入。

常见情形（一行一事）：**WebUI 保存后又改同一文件** ⇒ 5 秒内文件版本被导入、后写的赢（以 DB 为准就 `POST /api/config/export`）；**文件被写坏** ⇒ DB 不受影响 + 红色横幅、文件保持原样（按 `errors` 改，对照表见 [`USER_GUIDE.md`](USER_GUIDE.md) §5.8；或删掉该文件再 `export`）；**删了 `processors/<id>.yaml`** ⇒ 下次导入删除该处理器，仍有库引用则整体失败（先改库的 `processor_id` 或放回文件）；**手改"没反应"** ⇒ 看 `GET /api/config` 的 `in_sync`，急就 `POST /api/config/reload?force=true`；**想恢复出厂** ⇒ `POST /api/processors/{id}/reset` + `{"preset":"default"}` / `{"preset":"legacy"}`。

> ⚠️ `/data/config` 报 `Permission denied` ⇒ 查 `PUID`/`PGID` 与 `ls -ld <数据卷>/config`（§10.3）。

处理器 YAML 的字段表、命名表达式可用名字、内置默认值 → [`../contracts/PROCESSORS.md`](../contracts/PROCESSORS.md) §3。
> ⚠️ **安全面**：`naming.expressions` 的右值在应用进程内 `eval`；不需要就把 `expressions: {}`。

### 2.6 每库日志文件（都在 `/data/logs/`）

三类，**全部**在 `/data/logs/`，**不允许**分子目录：

| 类别 | 文件 | 作用 |
|---|---|---|
| **硬链接台账** | `hard_links.log` | 纯文本 `时间 - Source: … - Folder: … - Folder size: …`。只写两类记账行（体积登记 / 孤儿目录清理），历史行不清理。**它是审计台账，不再是"跳过"的判据**（判据是每库账本） |
| **每库一条重命名日志** | `<log_file>` | `json_cache`：`{ "<标题文件夹>": {"size": N, "timestamp": "…", "works_dir": "…"} }` —— "已重命名过"的幂等依据 |
| **任务日志** | SQLite `task_logs` + 内存环形缓冲 | 网页任务详情页看到的那份 |

**新建库不填 `log_file` 时的生成规则**：① 取该处理器登记的日志名（`bangumicache.log` / `moviecache.log` / `hardlink_only.log` / 自定义 `<id>cache.log`）；② **按库唯一化** —— 被占用就在扩展名前加 `-2`、`-3`；③ 第一个用某处理器的库保持登记名，磁盘上已有同名文件则**照用并继续追加**（避免全量重跑）；④ 兜底按库名转写 `{slug}cache.log`（`slug` = 小写、非 `[a-z0-9]` 折成 `-`；中文库名会塌成一串 `-`，建议手填 ASCII）；⑤ **显式填写**且与已占用名撞车 ⇒ **422**。

**硬规则**：`log_file` **只能是文件名**（含 `/` 或 `..` 直接 422）；新建库时不存在会创建空 `{}`；磁盘上已存在的同名日志原样保留、继续追加。

**自查【NAS 宿主】**：`ls -l <数据卷>/logs/`；`python3 -m json.tool bangumicache.log | head -20`。

> ⚠️ **"已处理"的判据是每库账本 `link_records`**，不是日志文件：作品记录还在就不会重做。要重做某作品，删掉作品记录或对应账本记录（[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) 场景 16、场景 28）。

---

## 3. 「源和目标必须在同一个挂载」的原理与自查

> 完整论述（含 *arr 生态的同类要求）见 [`../contracts/COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) §2；本节只留结论与命令。

**硬链接不是复制**：在目录里新增一个文件名，指向**已经存在的同一份数据**（同一个 inode）。只有一份数据、不占双倍空间、`ls -i` 两边 inode 相同、删掉下载侧那个名字媒体库那份照样能播。

**两条硬要求**：① 同一个**文件系统**（POSIX 语义前提：inode 号只在单文件系统内唯一）；② 同一个**挂载**（内核 `linkat(2)` 先比挂载身份，不同挂载一律 `EXDEV`，与 `st_dev`/`fsid`/btrfs 子卷无关）。跨过去就是：

```
OSError: [Errno 18] Invalid cross-device link: '<下载根>/Bangumi/x.mkv' -> '<媒体库根>/Bangumi/x.mkv'
```

**判据的层级**：主判据是**实证 `link()` 探针**（从源根有界递归取一个已存在的普通文件，link 到目标根临时名再删，**不写源目录**）；预筛是 `/proc/self/mountinfo` 的 **mount ID** 相同（必要不充分，btrfs 同挂载跨子卷仍可能 `EXDEV`）；无 `/proc` 时降级看 `st_dev`（**不能**预测 `link()` 成功，只看它会"假绿"）；源树下找不到可链的普通文件 ⇒ `same_filesystem: null`（明确说"无法实证"）。

> 🚫 **绝不回退为复制文件。** 判定跨挂载时运行前**只警告、不硬拦**（进 `Task.result.warnings[]`）；真跑起来由执行前后的逐文件核对给结论（失败就是 `link_failed`，不会悄悄复制一份冒充硬链接）。

**自查命令【NAS 宿主】**

```bash
findmnt -T <下载根> -o TARGET; findmnt -T <媒体库根> -o TARGET   # ① 主判据：TARGET 相同
docker exec media-ingest stat -c '%d %n' <下载根> <媒体库根>      # ② 仅参考：不同=必然不行，相同≠可行
df <下载根> <媒体库根>                                           # ③ 看 Mounted on 列
ls -i <下载根>/<作品> <媒体库根>/<作品>; stat -c '%h %i %n' …     # ④ 验证结果：inode 必须相同，%h ≥ 2
du -sh <下载根>/<作品> <媒体库根>/<作品>                          # ⑤ 反证没占双倍空间
```

**跨挂载时你会看到什么**：任务详情/预检给出「跨挂载：源 … 与目标根 … 不在同一个挂载，os.link 会返回 EXDEV。本应用不会回退为复制文件。请把下载目录与媒体库目录放进同一个 bind 挂载（`MEDIA_ROOT`）。」，任务结论是 `link_failed`（不是静默成功）。

**怎么修**：把下载根与媒体库根放进**同一条 bind**（`MEDIA_ROOT`），**不要**拆成两条；实在想跨盘只有"复制"一条路，本应用刻意不做 —— 正解是把媒体库根换到与下载根同一个挂载下。

### 3.1 源目录的保护：从"结构性只读"到"可验证"

| 手段 | 说明 |
|---|---|
| 代码层不写源目录 | 任何代码路径都不写源根；`tests/test_no_source_mutation.py` 守住 |
| 任务前后**逐文件核对** | 快照 `(相对路径, size, ino, mtime_ns)`，结束后复核 ⇒ `Task.result.source_integrity = {ok:false, changed:[...]}` + `warnings[]` 中文告警（列出路径） |
| 不误报 | 硬链接会让源文件 `nlink+1`、`ctime` 变 —— 这两项**不在**比较范围内 |

> 📌 **照抄口径**：源目录**不是**"只读"的。保护方式是**可验证的保证**（代码不写 + 跑完逐文件核对 + 测试），**能发现**源被动过并告警，但**不能阻止**它。

---

## 4. 安装步骤（从零到页面可访问）

> **两种装法，先选一种：**
> 1. **预构建镜像**（推荐，无需源码、无需本机构建） → §4.6：建数据目录 → 下载 compose → 按需设置挂载目录 / 数据目录 / 端口 → `up -d`。
> 2. **从源码构建**（要改代码、或访问不了镜像仓库时） → §4.1–4.5。

### 4.0 约定值

| 项 | 值 |
|---|---|
| Compose 项目目录 | `<项目目录>`（示例 `/vol1/1000/Docker/media-ingest`） |
| 镜像 / 容器名 | 源码构建 `media-ingest:latest`；镜像部署 `ghcr.io/taro-clo/media-ingest:<tag>`（默认 `latest`，跟随最新发布）；容器名都是 `media-ingest` |
| 数据卷宿主目录 | `<数据卷>`（默认 `<项目目录>/data`，§2.3） |
| 端口 / 访问地址 | `18081` / `http://<NAS_IP>:18081` |
| 容器内运行用户 | `app`（`<PUID>:<PGID>`，出厂示例 `1000:1001`） |

### 4.1 把项目文件放到 NAS 上

【任意电脑】`scp -r <项目目录> youruser@<NAS_IP>:<项目目录>`；或【NAS 宿主】先 `mkdir -p <项目目录>`，再用 File Station / SMB 把**全部文件**拖进去。目录里至少要看到 `docker-compose.yml`、`.env.example`、`docker/`、`app/`、`web/`、`docs/`。

### 4.2 建数据目录、写 `.env`

**【NAS 宿主】**

```bash
mkdir -p <数据卷>
cd <项目目录> && cp .env.example .env && vi .env
```

**必须核对的几项**（其余照模板；模板里 `PUID/PGID` 已是 `auto`，`DEFAULT_*` 已注释掉由 `MEDIA_ROOT` 派生）：

```bash
MEDIA_ROOT=/vol1/1000             # ★ 唯一必须按你的环境改的一项：下载目录与媒体库的共同父目录
TZ=Asia/Shanghai
PORT=18081
DATA_DIR=/data
CONFIG_DIR=/data/config           # 在 /data 卷内，无需额外挂载
PUID=auto                         # 按 MEDIA_ROOT 属主自动识别；想固定就写数字（§1.3）
PGID=auto
# MLM_DATA_DIR=<数据卷>           # 可选：数据卷放别处（默认 compose 同目录的 ./data，§2.3）

# 两个白名单的**首次播种初值**：不写则自动取 <MEDIA_ROOT>/Download、<MEDIA_ROOT>/Library
# （只对空库生效；已装实例请到设置页改）
# DEFAULT_SOURCE_ROOT=/vol1/1000/Download
# DEFAULT_TARGET_ROOT=/vol1/1000/Library
HOST_PATH_MAP={}                  # 路径映射，**只用于界面展示**；恒等部署留空对象即可（见下方 💡）
```

> ⚠️ **`.env` 不被应用代码读取**，只被 `docker compose` 用来做 `${VAR}` 插值，再经 `env_file: - .env` 进入容器。
> **不要删 compose 里的 `env_file: - .env`**，删了它只是个躺在磁盘上的文本文件。

> 💡 `HOST_PATH_MAP` 只是"给人看的翻译表"：`{}` 与显式恒等映射 `{"/vol1/1000":"/vol1/1000"}` 等价；只有迁移到**非恒等部署**（容器内仍是 `/downloads`、`/library`）时才需要覆盖。键必须是带前导 `/` 的绝对容器路径。`PUID`/`PGID` 怎么查、改完要 `chown` 什么见 §10。

### 4.2.1 部署资产自检（可选，**不需要 docker**）

```bash
sh examples/validate-deployment-assets.sh [仓库根]
```

**共 13 节**（编号 1–13；第 7 项「端口贯穿」并在第 6 节内）：三个 shell 脚本的 `dash`/`bash` 语法与 Windows CR 残留；`healthcheck.sh` 判据（桩服务行为测试：无服务、或**六项必需 checks 缺任一项**都必须判 unhealthy）；`docker compose config` 插值 + 渲染后事实（镜像/容器名、`restart`、端口、**恰好两条 volumes**、environment 键值）；改 `PORT` 时宿主与容器端口同步；Dockerfile 断言；`.dockerignore`；回调命令与 examples 的 API 口径；以及**第 13 节的真实硬链接探针**（需显式给 `SRC_ROOT`/`TGT_ROOT`，缺任一个即 SKIP）。脚本只在 `mktemp` 临时目录里写东西。

> 判据是 **[FAIL] 计数为 0**，不是固定 PASS 数。⚠️ **文档不代替验收** —— "构建成功 + 容器 `healthy`"属部署验收（§4.3 / §4.4）。

### 4.3 启动

```bash
cd <项目目录>
docker compose up -d --build        # 首次要构建镜像，几分钟
docker compose logs -f --tail=100   # Ctrl+C 只退出看日志，不停容器
docker compose ps
docker inspect media-ingest --format '{{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
```

`health=healthy` 是期望值（刚启动 30 秒内可能是 `starting`）。日志里关注：有没有 `Permission denied` / `No such file or directory`；有没有打印**配置已初始化/已同步**；uvicorn 是否在 `0.0.0.0:18081` 监听。

> ℹ️ **`healthy` 是真的"业务健康"**：`docker/healthcheck.sh` 解析 `/api/system/health`，**`status == "ok"` 且 `checks` 六个键全为 `true`** 才判 healthy（`db`、`data_dir`、`downloads_readable`、`library_writable`、`processors`、`config`）。该接口**永远返回 HTTP 200**，所以**不要**只看状态码。

### 4.4 验证服务可访问（不用登录）

**【NAS 宿主】** `curl -s http://127.0.0.1:18081/api/system/health` —— 期望 `"status":"ok"`，哪项 `false` 就查 §4.4.1。

**【容器内】** 直接跑健康检查脚本（`status=="ok"` 且六项全 `true` 才退出 0；退出码 `0`/`1`/`2` 分别 = 健康 / `degraded` 或响应非法 / 连不上或 HTTP 非 200）：

```bash
docker exec media-ingest /usr/local/bin/mlm-healthcheck
```

> 镜像里**没有 `curl`**（基础镜像 `python:3.11-slim`）。要看完整响应用自带 python：
> `docker exec media-ingest python3 -c "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:18081/api/system/health')),ensure_ascii=False,indent=2))"`

**首次启动、未登录时的预期**：`GET /api/system/health` → `200` + `status:"ok"`（**唯一免认证**接口）；`GET /api/auth/me` → `200` + `{"authenticated":false,"needs_setup":true,…}`（未登录**也不**返回 401，前端靠它决定显示「创建管理员」还是「去登录」）；`GET /api/libraries`、`GET /api/system/info` → `401`；`GET /` → `200`（`web/index.html`）。

### 4.4.1 容器显示 `unhealthy` 时怎么定位

**不要重装、不要删数据** —— 一定是某个 `checks` 为 `false`，或出现了 `warnings`。

**① 看 Docker 记录的失败输出**：`docker inspect --format '{{json .State.Health}}' media-ingest` → 读 `Status` / `FailingStreak` / `Log[-1].ExitCode` 与 `Log[-1].Output`（脚本自己打印的原因）。`--start-period` 内是 `starting`，属正常。
**② 看接口怎么说**：`curl -s http://127.0.0.1:18081/api/system/health | python3 -m json.tool` → 关注 `status`、`checks`、`details`（每项一句中文）、`warnings`。
**③ 对照修根因**：

| `checks` 键 | `false` 的含义 | 怎么办 |
|---|---|---|
| `db` | 打不开 `/data` 下的 SQLite | 数据卷没挂上 / 属主不对 → §10.2、§7.4 |
| `processors` | 三个内置处理器没有全部加载 | 看 `GET /api/processors` 与 `GET /api/config` 的 `errors`；必要时删掉 `/data/config/processors/` 下对应文件并重启 |
| `config` / `data_dir` | `/data/config` 或 `/data` 不可写 | `chown -R <PUID>:<PGID> <数据卷>`（§10.2） |
| `downloads_readable` | `allowed_source_roots` 第一个目录读不到 | 核对 `PUID`/`PGID` 与宿主属主（§10.2） |
| `library_writable` | `allowed_target_roots` 第一个目录不可写 | 同上；**别对媒体库整棵树 `chown -R`**（会连带改到硬链接共享的源文件） |
| `warnings` 非空 | 通常是文件系统编码不是 UTF-8 | 按 `details` 设 `LANG=C.UTF-8` / `PYTHONUTF8=1`（§4.2） |

> **两条判据的区别**：`/api/system/health` **永远 200**，健康与否看响应体的 `status` 与 `checks`；`healthcheck.sh` 解析响应体，`degraded` 会让容器真的显示 `(unhealthy)` 并在 stderr 打印 `[失败]` / `[缺失]` 行。
> **任何时候都以接口的 `status` 与 `checks` 为准。**

健康的容器与响应长这样：

```
media-ingest  media-ingest:latest  "/usr/local/bin/…"  Up 45 seconds (healthy)  0.0.0.0:18081->18081/tcp
{"status":"ok","checks":{"db":true,"data_dir":true,"downloads_readable":true,"library_writable":true,"processors":true,"config":true},"warnings":[],"version":"1.0.0"}
```

### 4.5 浏览器打开并创建管理员

打开 `http://<NAS_IP>:18081`：`.env` 填了 `ADMIN_USERNAME` / `ADMIN_PASSWORD` → 直接用该账号登录；没填 → 页面引导你**创建管理员**（用户名 + 密码，**至少 8 位**）。登录后立刻：① 设置页改密码（若用 `.env` 初始化），并把 `.env` 里的 `ADMIN_PASSWORD` 删掉；② 设置页「回调 / Hook」复制 `hook_token` 与完整回调命令（§5 要用）；③ 新建媒体库前先看 [`USER_GUIDE.md`](USER_GUIDE.md)。

### 4.6 用预构建镜像安装（不构建，推荐）

镜像由 GitHub Actions 构建，多架构（`linux/amd64`、`linux/arm64`，按宿主架构自动选择），推送到 GHCR：
`ghcr.io/taro-clo/media-ingest:latest`（默认跟随最新发布；要钉住版本设
`MLM_VERSION=1.0.0`）。**不需要源码、不需要本机 Docker 构建。**

全程三步，需要你自己决定的只有三处：**数据目录放哪**、**下载目录与媒体库的共同父目录是什么**、
**端口要不要换**（默认 `18081` 可用则不用动）。

**① 创建容器数据存储目录**（SQLite、日志、`secret.key`、`config` 都写在这里；路径随意，下面示例换成你自己的）：

```bash
mkdir -p <数据卷>
```

> 这个目录只要求**可写且持久化**，不要求与媒体同一文件系统；下文用它填 `x-data-dir`。
> 它也是**唯一必须备份**的东西（§7），不要放 `/tmp` 这类临时目录，建议放在媒体目录之外。

**② 下载 docker-compose（或从 README 复制内容自行创建）**：

```bash
mkdir -p <项目目录> && cd <项目目录>
curl -fsSLO https://raw.githubusercontent.com/Taro-clo/media-ingest/main/work/docker-compose.release.yml
```

> 根 [`README.md`](../../../README.md) 的「快速开始（镜像部署）」里有一份完整内容，与仓库文件逐字一致。
> `main` 分支 = 仓库最新版：升级时重新下载本文件即可让 compose 与镜像一起更新。
> 文件名保持 `docker-compose.release.yml` 即可；也可以改名成默认的 `docker-compose.yml`，之后命令里的 `-f …` 相应去掉。

**③ 按需设置挂载目录、数据目录与端口**：只改 compose 文件头「部署配置」里的锚点，改一处即全部生效：

```yaml
# ===== 部署配置：通常只需要改这一段 =====
# 媒体共同父目录（下载目录与媒体库的共同父目录；必改）
x-media-root: &media-root "/vol1/1000"
# 容器数据目录（默认本文件同目录的 ./data；想用第 ① 步建的目录就改成绝对路径）
x-data-dir: &data-dir "./data"
# WebUI 端口（默认 18081）
x-port: &port "18081"
```

改完启动：

```bash
docker compose -f docker-compose.release.yml pull
docker compose -f docker-compose.release.yml up -d
docker compose -f docker-compose.release.yml ps        # STATUS 出现 (healthy) 即成功
```

- **① `x-media-root`（唯一必改）**：下载目录与媒体库的共同父目录，例：下载 `/vol1/1000/Download`、
  媒体库 `/vol1/1000/Library` ⇒ 填 `/vol1/1000`。必须宿主上**已存在**的绝对路径（否则 Docker 会以
  root 建空目录，程序报源目录不可读写）；容器内路径 = 宿主路径，不要填 `/data`、`/media` 这类容器内路径。
  下载与媒体库还必须在**同一个挂载点**内（否则硬链接报 `Invalid cross-device link`）——不确定时用
  `df -T <下载目录> <媒体库目录>` 或 `findmnt -T <目录>` 确认。首次建库会派生源 / 目标根
  `<MEDIA_ROOT>/Download`、`<MEDIA_ROOT>/Library`；目录名不同就首次启动后在 WebUI「设置」页改。
- **② `x-data-dir`（建议改）**：指向第 ① 步目录的宿主绝对路径，放 `mlm.db`、`secret.key`、`config/`、`logs/`；
  不写默认 `./data`。只要求可写且持久化，是唯一必须备份的东西（§7.1）。
- **③ `x-port`（按需）**：默认 `18081`；`docker compose up` 报 `port is already allocated` 或宿主
  `ss -ltn | grep 18081` 有输出时换一个端口（如 `x-port: &port "18099"`）。`ports` 与容器内 `PORT`
  引用同一个锚点，改一处两边同步，访问地址随之变化；有防火墙 / 安全组的话记得放行。

> 不用改本文件也行：在 compose 同目录建 `.env`（可选，compose 自动读取）写 `MEDIA_ROOT=<媒体共同父目录>`、
> `MLM_DATA_DIR=<数据卷>`、`PORT=18099`，或启动命令前加同名环境变量
> （`MEDIA_ROOT=/mnt/media docker compose -f docker-compose.release.yml up -d`）——
> 两者都覆盖锚点里的默认值，且所有引用一起生效。
> ✅ 本文件里同一个值靠锚点单点定义（`environment` / `ports` / `volumes` 都引用锚点），
> 所以只改锚点一处即可，不需要像手写多份表达式那样多处同步。

**默认值（都不用手配）**：

| 项 | 默认 | 想改时 |
|---|---|---|
| 镜像版本 | `latest`（跟随最新发布） | `MLM_VERSION=1.0.0` 钉住固定版本（改锚点、`.env` 或命令前变量均可） |
| 媒体共同父目录 | `/vol1/1000` | 改 `x-media-root` 锚点（或设 `MEDIA_ROOT=/你的/路径`） |
| 数据卷（宿主） | compose 文件同目录的 `./data` | 改 `x-data-dir` 锚点（或设 `MLM_DATA_DIR=/你的/路径/data`） |
| 运行身份 | `PUID=auto` / `PGID=auto`：按 `MEDIA_ROOT` 属主自动识别（识别失败回退 `1000:1001` 并在日志 WARN） | 想固定就写数字 `PUID=1000 PGID=1000` |
| 源 / 目标根 | 由 `MEDIA_ROOT` 自动派生 `<MEDIA_ROOT>/Download`、`<MEDIA_ROOT>/Library`（仅首次建库时播种） | 一般不用设；特殊值用 `DEFAULT_SOURCE_ROOT=… DEFAULT_TARGET_ROOT=…` |
| 端口 | `18081` | 改 `x-port` 锚点（或设 `PORT=18099`，宿主与容器同步移动） |
| `HOST_PATH_MAP` | 空 = 恒等（容器路径就是宿主路径） | 只在容器内路径 ≠ 宿主路径时才需要 |

- `MEDIA_ROOT` 必须是宿主上真实存在的目录；**下载目录与媒体库在同一个挂载里**这条不变量不变（§2、§3）。
- 数据卷默认建在 compose 同目录的 `./data`；想用 ① 创建的目录就把 `x-data-dir` 改成它的宿主绝对路径。
- 后续验证、创建管理员、接 qBittorrent 与源码构建完全相同：§4.4 → §4.5 → §5。

> GHCR 在国内部分网络下可能较慢或不可达；这种情况用源码构建（§4.3），或先在有网络的地方
> `docker pull` 后 `docker save -o media-ingest.tar ghcr.io/taro-clo/media-ingest:latest`，拷到 NAS 再
> `docker load -i media-ingest.tar` —— 加载后的镜像名就是 compose 默认的 `latest`，通常不用改 `image:`。
> 非 x86_64 的 NAS（arm64）不用改任何东西：镜像里有 arm64 版，`docker pull` 会按架构自动挑选。

---

## 5. 接入 qBittorrent（下载完成自动触发）

### 5.1 先拿到 token

设置页 →「回调 / Hook」→ 点「复制」拿到**完整回调命令**（已含 token 与地址）。

> 📌 **`hook_token` 不是环境变量**：`.env` 里没有这个键，填了也不被读取；它由应用**首次启动时随机生成**并存在 `/data` 的数据库里。**唯一的轮换入口是设置页的「重新生成 Token」**（等价 `PUT /api/settings` 带 `{"hook_token": ""}` 重新生成、带具体值改成该值、带 `"***"` 或不带保持不变）。
> ⚠️ 它等于这个应用的"后门钥匙"，别贴到公开论坛/截图里。**重新生成后必须立刻回到 qBittorrent**（旧 token 立即失效，那边会开始 401）：重新生成 → 复制新命令 → 粘回 qBittorrent → 手动模拟一次回调确认 200。

### 5.2 在 qBittorrent 里填写

qBittorrent WebUI（`http://<NAS_IP>:18080`）→ **设置 → 下载 → 运行外部程序（Run external program）**，把设置页拿到的那**一整行**粘进去，确认**已启用**，保存。

**完整命令的唯一权威副本在 [`examples/qbittorrent-autorun.md`](../../examples/qbittorrent-autorun.md)**（含全部 7 个占位符与逐项解释）—— 本文只留必须知道的四条：

| 要点 | 说明 |
|---|---|
| 必须是**一行** | 不要换行、不要多余空格 |
| **7 个占位符** | `%N` `%I` `%L` `%G` `%F` `%D` `%R`（名称/哈希/分类/标签/内容路径/保存路径/根路径），**照抄，不要改** |
| **不要加 `kind=%K`** | `%K` 是 **Torrent ID**，不是任务类型。类型由媒体库的处理器推导；`kind` 只是手工传 `hardlink`/`rename`/`rename_hardlink` 时的可选覆盖项（[`../contracts/ARCHITECTURE.md`](../contracts/ARCHITECTURE.md) §8 是唯一真源） |
| **`--data-urlencode`** | 种子名/路径可能含空格与中文，不能写 `-d "name=%N"` |

> `%F` 为什么能直接用：qBittorrent 容器与本应用把同一宿主目录挂在**同一个容器内路径**（§2.1）。`curl` 由 qBittorrent 镜像自带。

### 5.3 验证回调真的生效

**① 手动模拟一次回调（最可靠）【NAS 宿主】**

```bash
curl -s -X POST "http://127.0.0.1:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" \
  --data-urlencode "category=ban" --data-urlencode "tags=" \
  --data-urlencode "content_path=<下载根>/Bangumi/Show A" --data-urlencode "save_path=<下载根>/Bangumi" \
  --data-urlencode "root_path=<下载根>" --data-urlencode "kind=completed"
```

| 返回 | 含义 | 下一步 |
|---|---|---|
| `{"matched":1,"tasks":[43],"ignored":[]}` | ✅ 命中并入队 | 去任务页看任务 |
| `{"matched":0,"tasks":[],"ignored":["番剧"]}` | 回调通了，但**没命中任何库** | 看 `ignored_details[].reason`（[`USER_GUIDE.md`](USER_GUIDE.md) §9 与 §4.4） |
| `401`「回调 token 不正确…」 | token 错或没带上 | 重新从设置页复制完整命令（别带多余空格/引号） |
| 连接被拒绝 | 应用没跑或端口不对 | `docker compose ps` |

> ⚠️ 手动模拟只验证"**回调链路**通不通"，不代表下载完成时 qBittorrent 真会调用（取决于 §5.2 的配置与开关）。

**② 从 qBittorrent 容器内部测（验证网络可达）**：`docker exec qbittorrent curl -s -X POST "http://<NAS_IP>:18081/api/system/health"` —— 有 JSON 输出 = 网络通。

**③ 真跑一个种子**：完成后 `docker compose logs --tail=100` 应看到 `INFO qbittorrent 回调：…` 与 `INFO 命中媒体库「番剧」，任务 #43 已入队`，任务页出现新任务。

### 5.4 回调漏了怎么办（扫描补偿）

**（a）目录扫描（推荐）**：设置页打开 `scan_enabled = true`，`scan_interval_seconds = 900`（15 分钟）。会定期扫描所有已启用库的 `source_dir`，发现「没记录过、或体积变了」的一级文件夹就建任务。

```bash
curl -s -b cookies.txt -X PUT http://127.0.0.1:18081/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"scan_enabled":"true","scan_interval_seconds":"900"}'
```

**（b）手动触发一次扫描**：`curl -s -X POST "http://127.0.0.1:18081/api/hooks/scan?token=<HOOK_TOKEN>"` —— 这个接口**不看** `scan_enabled` 开关，手动调用总是执行一次扫描。

> 扫描**只创建任务**，不做任何文件操作。

### 5.5 （可选）qBittorrent API 连接信息

设置页的 `qbittorrent_url` / `qbittorrent_username` / `qbittorrent_password` 用于 **API 补偿**（向 qBittorrent 查种子列表）。应用**不靠它工作**：回调与扫描都不依赖它，不填不影响主流程。

---

## 6. 升级与回滚

### 6.1 核心概念：数据在宿主上，容器可丢弃

`/data` 是宿主目录的 bind mount（§2.3），所以**升级不会碰你的数据**：账号、规则、任务历史、`config/`、`logs/` 全在 `<数据卷>` 里原地不动。容器删了重建，数据还在。

### 6.2 升级步骤

**① 先备份**（永远先备份）：见 §7。

**② 替换代码并重建【NAS 宿主】**

```bash
cd <项目目录>
docker compose down                 # 停并删容器；数据卷不受影响
# 用新版本文件覆盖项目目录 —— 保留 .env，不碰数据卷
docker compose up -d --build
docker compose logs -f --tail=100
```

> ⚠️ **覆盖代码时千万不要删 `.env` 和数据卷目录**。有 git 的话 `git pull` 最安全：它只更新跟踪的文件。

**③ 验证**：`curl -s http://127.0.0.1:18081/api/system/health` + `docker inspect media-ingest --format '{{.State.Health.Status}}'`。

> **升级时多看一眼**：首次启动会自动补建每库账本表 `link_records`（新库由 `SCHEMA` 带出，老库由 `migrate()` 补建）—— **不需要手工建表**。但账本**不从旧的 `hard_links.log` 回填**，老媒体库**第一次全量运行会把每个作品重新建链**，已在目标里改过名的文件会以原名再链一份、可能多出 ` (2)` 后缀（**不丢数据**）。原因与三种规避见 [`../contracts/COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) §3.5 与 [`../meta/KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-7。

### 6.3 回滚

```bash
cd <项目目录> && docker compose down
# 换回旧版本项目文件（git：git checkout <旧版本标签>），或用旧镜像
docker compose up -d --build
```

**关于数据**：数据卷原地不动，回滚本身不丢数据。但若新版本做过数据库结构变更，旧版本可能不认识新结构 —— 所以**升级前先备份**（§7），必要时连数据卷一起恢复。升级前看 `CHANGELOG.md` 有没有数据结构变更。

### 6.4 只升级不重建（用预构建镜像）

镜像部署（§4.6）换版本只需要重新拉镜像，不构建、不碰数据卷。默认跟随 `latest` 时：

```bash
cd <项目目录>
# 先备份（§7）；compose 若也从 main 重新下载过，镜像与配置就一起更新
docker compose -f docker-compose.release.yml pull
docker compose -f docker-compose.release.yml up -d
```

要钉住某个固定发布（而不是跟随 `latest`）：

```bash
MLM_VERSION=1.0.1 docker compose -f docker-compose.release.yml pull
MLM_VERSION=1.0.1 docker compose -f docker-compose.release.yml up -d
```

源码构建的部署没有 `pull` 这一步，走 §6.2 的 `docker compose up -d --build`。

### 6.5 升级后必做：确认两个路径白名单

**为什么**：单挂载形态之后，容器里只有 `<MEDIA_ROOT>` 与 `/data` 两条挂载；更早版本的默认值 `/downloads`、`/library` 在容器里**根本不存在**。若两个 roots 停在那两个值上，`/api/system/health` 的 `downloads_readable` / `library_writable` 会变 **`false`** ⇒ **开箱 unhealthy**（其余项都正常，很容易误判成权限问题）。

**自动迁移规则**（只做一次）：

- 设置值**恰好逐字等于**更早版本的默认单值（源 `["/downloads"]`、目标 `["/library"]`）⇒ **自动改写**为 `[DEFAULT_SOURCE_ROOT]` / `[DEFAULT_TARGET_ROOT]`（即宿主真实路径）；
- 你自己改过（任何别的值，包括多元素列表）⇒ **一律不动** —— 迁移刻意不猜你的意图。

**30 秒确认【NAS 宿主】**

`curl -s http://127.0.0.1:18081/api/system/health`（`downloads_readable` / `library_writable` 应为 `true`）+ `curl -s http://127.0.0.1:18081/api/settings`（看两个 roots 的实际值）。

- 两个 roots 是**宿主真实路径**（如 `/vol1/1000/Download`、`/vol1/1000/Library`）⇒ 迁移成功；
- 仍是 `/downloads` / `/library`（你曾自定义过，迁移按约定跳过）⇒ 到 **WebUI 设置页**改成宿主真实路径；
- ⚠️ `DEFAULT_SOURCE_ROOT` / `DEFAULT_TARGET_ROOT` **只影响首次播种**，对已存在的 DB 不生效 —— 改它们解决不了这个死角。

**升级后的行为变化**（看一眼再决定要不要调）：**重命名改由规则引擎执行**，默认（`default` 预设）比 `legacy` 多拒绝目录规则与 9 条集数规则 —— 想要旧口径就到处理器页「恢复默认」选 `legacy`；**硬链接改由原生实现执行**（`app/processors/hardlink.py`），可观察输出由 `tests/test_hardlink_native.py` 的冻结黄金值守护。

**回滚注意**：回滚到旧版本要把整个数据卷一起恢复（§7.3），并把 `docker-compose.yml` / `.env` 换成旧版本那一套。新版本写出的 `/data/config/` 对旧版本是无害的额外文件，可以留着。

---

## 7. 备份与恢复

### 7.1 必须备份什么

| 必须备份 | 路径（相对 `<数据卷>`） | 丢了会怎样 |
|---|---|---|
| ✅ **数据库** | `mlm.db` | 账号、**所有处理器与媒体库规则**、任务历史、设置全丢，要重配 |
| ✅ **WAL 伴生文件** | `mlm.db-wal`、`mlm.db-shm` | 只备份 `mlm.db` 可能丢最近的写入 |
| ✅ **配置文件** | `config/`（`libraries.yaml` + `processors/*.yaml`） | 规则在数据库里也有一份，所以不一定致命；但**手改过的 YAML** 只有这里有（§7.5） |
| ✅ **会话密钥** | `secret.key` | 所有已登录会话失效（要重新登录）；**不影响**文件与规则 |
| ✅ **日志目录** | `logs/` | 幂等判据（硬链接台账 + 每库一条重命名日志）丢失 → 已处理过的文件夹会被重跑一次（一批无害的 `File exists`，**不损坏文件**） |

**不需要备份**：项目代码、`.env`（建议手抄 PUID/PGID/TZ/ADMIN，但它含明文口令，别和备份放一起）、Docker 镜像。

> 📌 **只需搬一个目录**：上面全部内容都在**数据卷**（默认 `<项目目录>/data`，或你设的 `MLM_DATA_DIR`）里；媒体库与下载目录**不在备份范围内**（它们是真身数据，硬链接不需要备份）。

### 7.2 备份命令（先停容器，最稳）

```bash
cd <项目目录> && docker compose stop
tar -czf <备份目录>/mlm-data-$(date +%Y%m%d-%H%M%S).tar.gz -C <数据卷的父目录> "$(basename <数据卷>)"
docker compose start
```

不停容器也能备份（**要带上 WAL**）：`docker exec media-ingest sh -c 'cd /data && tar -czf - mlm.db mlm.db-wal mlm.db-shm secret.key config logs' > <备份目录>/mlm-data-live-$(date +%Y%m%d-%H%M%S).tar.gz` —— 通过容器内 tar 打包可避开宿主权限不足，但打包瞬间数据库可能正在被写。**建议**加进 NAS 定时任务，每天一次，留最近 7~14 份。

### 7.3 恢复命令

```bash
cd <项目目录> && docker compose stop
mv "$(basename <数据卷>)" "$(basename <数据卷>).before-restore"    # 先保留现状，别直接删
mkdir -p "$(basename <数据卷>)"
tar -xzf <备份目录>/mlm-data-<YYYYmmdd-HHMMSS>.tar.gz -C <数据卷的父目录>
docker compose start
```

### 7.4 恢复后需要做什么

1. **修属主**（解包成 root 属主时容器里的 `app` 读不了，§10.2）：`chown -R <PUID>:<PGID> <数据卷>`；
2. **检查健康**：`docker compose ps` + `curl -s http://127.0.0.1:18081/api/system/health`；
3. **重新登录网页**：`secret.key` 没恢复或换了 `SECRET_KEY` ⇒ 旧会话失效，**账号还在数据库里**；
4. **核对设置页的 `hook_token`** 是否与 qBittorrent 里填的一致（恢复旧备份且之后重生成过 → 要更新回调命令）；
5. **看一次任务历史**，确认规则与历史都在；
6. **确认后再删** `*.before-restore`（**不可逆**）：`rm -rf <数据卷>.before-restore`（先 `ls` 确认路径！）。

### 7.5 只备份 / 只恢复配置目录（`/data/config`）

`config/` 是你手改过的重命名规则的**可读快照**（数据库里那份是二进制形态）。

```bash
cd <数据卷> && tar -czf <备份目录>/mlm-config-$(date +%Y%m%d-%H%M%S).tar.gz -C . config   # 只备份
mkdir -p /tmp/mlm-config && tar -xzf <备份目录>/mlm-config-<YYYYmmdd-HHMMSS>.tar.gz -C /tmp/mlm-config
cp -a /tmp/mlm-config/config/. <数据卷>/config/ && chown -R <PUID>:<PGID> <数据卷>/config     # 只恢复（容器可不停）
curl -s -X POST -b /tmp/cookies.txt http://127.0.0.1:18081/api/config/reload
```

> ⚠️ **恢复配置会覆盖数据库里的规则**（导入方向是"文件胜出"）。只想撤销一次误改，先 `POST /api/config/export`
> 留一份再覆盖。只想把某个处理器恢复出厂：处理器页「恢复默认」或 `POST /api/processors/{id}/reset`。
> `reload` / `export` 都**需要登录**（[`../contracts/API.md`](../contracts/API.md) §8）。

---

## 8. 端口与反向代理

### 8.1 换端口

compose 的宿主侧与容器侧共用 `${PORT}`，所以**只改 `.env` 一处**（`PORT=18082`），然后 `docker compose up -d`。

> ⚠️ 若你手工写死过 `ports:`（如 `"18081:18081"`），两边数字都要改且必须一致：右边 = 容器内 uvicorn 监听端口（由 `PORT` 决定），左边 = 浏览器访问端口。改了端口，qBittorrent 回调命令里的 URL 端口也要同步改。

### 8.2 反向代理（可选）

**SSE 必须关缓冲** —— 少了 `proxy_buffering off`，实时日志会被 Nginx 缓冲住，页面看着像"卡住了"。

```nginx
location / {
    proxy_pass http://<NAS_IP>:18081;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;            # SSE 关键
    proxy_cache off;                # SSE 关键
    proxy_read_timeout 3600s;
}
```

> ⚠️ **不要把本应用直接暴露到公网**。它是局域网管理工具，只有一层口令认证。外网访问走 VPN（WireGuard/Tailscale），或至少加 HTTPS + 访问控制。

---

## 9. x86_64 与 ARM64

### 9.1 成本评估：几乎零成本

| 因素 | 情况 |
|---|---|
| 基础镜像 | `python:3.11-slim`（官方镜像，**同时提供 `linux/amd64` 与 `linux/arm64`**） |
| Python 依赖 | 只有三个**纯 Python** 包：`fastapi==0.115.6`、`uvicorn[standard]==0.32.1`、`PyYAML==6.0.3` —— 没有需要编译的 C 扩展 |
| 前端 | **免构建**的原生 ES modules + CSS（不需要 node/npm） |
| 镜像体积（参考） | 约 **180MB**（单平台构建；以 `docker image ls` 为准） |

> 📌 本文示例与验收针对 **x86_64**（`arch=amd64`）。单平台构建出的镜像只能在那个架构上跑。

### 9.2 方式 A：在 ARM NAS 上本机构建（最简单）

`cd <项目目录> && docker compose up -d --build` —— 官方基础镜像会自动拉 arm64 版本，`pip` 会自动装 arm64 wheel。

### 9.3 方式 B：buildx 交叉构建并推送

```bash
docker buildx create --use --name media-ingest-builder && docker buildx inspect --bootstrap   # 首次
# 多平台必须 --push（不能 --load）；只构建一种架构时才可以用 --load
docker buildx build --platform linux/amd64,linux/arm64 -f docker/Dockerfile -t <你的仓库地址>/media-ingest:latest --push .
docker buildx build --platform linux/arm64 -f docker/Dockerfile -t media-ingest:latest --load .
```

在 NAS 上改用镜像：把 `docker-compose.yml` 的 `build:` 段注释掉、只留 `image:`，然后 `docker compose pull && docker compose up -d`。别把私有仓库的账号密码写进 `docker-compose.yml`，用 `docker login`。

---

## 10. 非 root 运行与 PUID / PGID

### 10.1 为什么要关心 / 是什么

容器以 root 运行，建出来的文件和目录就是 root 属主：你的 NAS 账号可能删不掉/改不了，Emby 可能读不了；反过来 uid 不对时**它自己**也会 `Permission denied`。所以本项目**刻意不以 root 执行业务逻辑**：entrypoint 以 root 启动只为**修权限**，修完立刻降权到 `app` 用户。

| 变量 | 含义 | 默认 / 参考值 |
|---|---|---|
| `PUID` | 容器内应用用户的 **User ID** | `auto` = 自动取 `MEDIA_ROOT` 的属主 uid；显式写数字（如 `1000`）则固定 |
| `PGID` | 容器内应用用户的 **Group ID** | `auto` = 自动取 `MEDIA_ROOT` 的属主 gid；显式写数字（如 `1000`、`1001`）则固定 |

启动流程：entrypoint 以 root 起 → 若 `PUID/PGID` 是 `auto`，`stat` 媒体挂载属主并据此取值（不写任何文件）→ 按 `PUID`/`PGID` 改写 `app` 用户 → `chown` 数据与代码目录 → `gosu` 降权 → 以 `app` 起 `uvicorn`（实现是自带的 `docker/entrypoint.sh`，含义与 linuxserver 系列一致）。

> ⚠️ **`docker exec` 进去默认是 root shell**（镜像 `Config.User` 为空，降权在 entrypoint 内部完成）。要以**应用身份**操作：`docker exec -u <PUID>:<PGID> -it media-ingest sh`。

### 10.2 怎么查、怎么改

**默认不用查也不用填**：`PUID/PGID` 是 `auto` 时，容器启动按 `MEDIA_ROOT` 的属主自动识别，
日志里会打印 `自动识别运行身份：PUID=… PGID=…（取自 … 的属主）`。识别不出来（例如媒体目录属主是 root）
会 WARN 并回退 `1000:1001`，这时才需要显式配置：

**① 查正确的 uid/gid**：`id youruser` → 形如 `uid=1000(…) gid=1001(Users)`，把数字填进 `PUID` / `PGID`。
**② 改 `.env`** 的 `PUID=` / `PGID=`（或直接删掉这两行/写成 `auto`）。
**③ 重建容器**让 entrypoint 重新修权限：`cd <项目目录> && docker compose down && docker compose up -d`（源码构建加 `--build`）。
**④ 核对**：`docker exec media-ingest sh -c 'grep -E "^(Uid|Gid):" /proc/1/status'` 应等于你配的/自动识别的值。

**改完需要 `chown` 什么**：

- `/data` 与 `/srv/mlm`：**entrypoint 自动**按 PUID/PGID 修属主（手动兜底 `chown -R <PUID>:<PGID> <数据卷>`）；
- `<媒体库根>`：**你自己**。要让新 `PUID` 可写就 `chown <PUID>:<PGID> <媒体库根>` 或放宽目录权限（§10.3）—— entrypoint 不会也不该 `chown` 整棵树；
- `<下载根>`：只需**可读**（代码层从不写这里、也不做 `:ro`，保护靠代码层 + 逐文件核对，§3.1）。

**验证【NAS 宿主】**：`docker exec media-ingest id`；`ls -ld <数据卷>`；`sudo -u '#<PUID>' test -w <媒体库根> && echo OK || echo '不可写，需要 chown/chmod'`。

### 10.3 权限调不过去怎么办

属主是别人又不想改，可以放宽**目录本身**的权限位：`chmod 775 <媒体库根>`。

> ⚠️ **不要**用 `chown -R` / `chmod -R` 递归改整棵媒体库树 —— 硬链接共享 inode，改媒体库里的文件等于改**下载源文件**的属性！这正是应用**默认不执行 `chmod`** 的原因。
> 唯一的显式开关是 `permission_mode`（**默认关**）：界面上开启并二次确认后，应用才把**目标侧**（库根 + 本次处理到的作品子树，含被跳过的旧作品）改成 `0777`，代价是源文件权限位一起变（[`../contracts/BASELINE.md`](../contracts/BASELINE.md) B13、[`../contracts/API.md`](../contracts/API.md) §3.2）。**优先**在宿主上针对**目录**调整。

### 10.4 常见权限报错对照

| 报错 | 原因 | 处理 |
|---|---|---|
| `Permission denied: '/data/config'` | 配置目录属主不对 | `chown -R <PUID>:<PGID> <数据卷>` 后重启（§10.2） |
| `Permission denied: '<下载根>'` | `PUID` 读不了下载目录 | §10.2 ④ |
| `Permission denied: '<媒体库根>/…'` | `PUID` 写不了媒体库 | §10.3 |
| `Permission denied: '/data/mlm.db'` | 数据卷属主不对 | `chown -R <PUID>:<PGID> <数据卷>` 后重建 |
| 日志写"跨文件系统错误" | 不是权限，是文件系统/挂载 | §3 |

---

## 11. 常用运维命令速查

全部在**【NAS 宿主】**执行（`cd <项目目录>` 之后）：

```bash
docker compose up -d / stop / start / restart      # 启停
docker compose ps; docker compose logs -f --tail=200
docker inspect media-ingest --format '{{.State.Status}} {{.State.Health.Status}}'
docker exec -u <PUID>:<PGID> -it media-ingest sh   # 进容器（⚠️ 不加 -u 会是 root shell）
docker exec media-ingest sh -c 'grep -E "^(Uid|Gid):" /proc/1/status'   # PID 1 真实身份
curl -s http://127.0.0.1:18081/api/system/health   # 健康接口
docker inspect media-ingest --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{if .RW}}rw{{else}}ro{{end}}){{println}}{{end}}'   # 应恰好两条、均 rw
docker system df; du -sh <数据卷>
```

**应用自己的日志文件（`/data/logs/`）**：`ls -lh <数据卷>/logs/`、`tail -n 100 <数据卷>/logs/hard_links.log`。

---

## 下一步看哪篇

- 网页上每个按钮/字段什么意思 → [`USER_GUIDE.md`](USER_GUIDE.md)
- 用 curl / 脚本调接口 → [`../contracts/API.md`](../contracts/API.md)
- 「为什么没触发」「为什么任务失败」→ [`FAQ.md`](FAQ.md)
- 具体报错怎么修 → [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)
- 兼容规则为什么是这样 → [`../contracts/BASELINE.md`](../contracts/BASELINE.md)、[`../contracts/COMPATIBILITY.md`](../contracts/COMPATIBILITY.md)
