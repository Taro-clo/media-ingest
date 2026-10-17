# 部署手册（Docker / NAS）

> **本文解决什么问题**：把本应用从零装到「浏览器能打开、qBittorrent 能触发」。
> 包括：装之前要确认什么、**共同父挂载**为什么必须这样挂、为什么"硬链接只能在同一个挂载里建"、源目录不做 `:ro` 时靠什么保护、升级、备份恢复、ARM64、非 root 运行。
> 面向**不懂代码的 NAS 用户**：命令都可以直接复制；每条命令前面都标了**在哪里执行**。
>
> 阅读前提：你已经在 NAS 上装好了 Docker（含 `docker compose`）。

**本文不是**：网页界面上每个按钮怎么点 → 看 [`docs/guides/USER_GUIDE.md`](USER_GUIDE.md)；接口怎么调 → 看 [`docs/contracts/API.md`](../contracts/API.md)；出错了 → 看 [`docs/guides/FAQ.md`](FAQ.md)。

---

## 命令执行位置的标注约定

本文每条命令前都有一个标记，意思是**这条命令要在哪台机器上敲**：

| 标记 | 含义 |
|---|---|
| 【NAS 宿主】 | 用 SSH 登录 NAS，或者在 NAS 的「终端 / SSH」里执行。**不是**容器里 |
| 【容器内】 | 进入应用容器内部执行，命令形如 `docker exec -it media-ingest <命令>` |
| 【任意电脑】 | 在你自己电脑（或者 NAS）上都能跑，例如浏览器、curl |

> 怎么 SSH 登录 NAS：群晖/威联通/飞牛 fnOS 都在「控制面板 → 终端机 / SSH」里开启 SSH 服务，然后用你自己的电脑 `ssh 用户名@NAS的IP`。

---

## 1. 前置条件检查清单

先一次性确认下面这些条件。**第 3、4 条是最容易导致「装了但一直失败」的原因。**

### 1.1 检查 Docker 与 compose

**【NAS 宿主】**

```bash
docker --version
docker compose version
docker info --format '{{.ServerVersion}} arch={{.Architecture}} os={{.OperatingSystem}}'
```

- 两条命令都要有输出。
- `arch=amd64`（即 x86_64）→ 直接用官方多架构基础镜像，**不需要任何额外操作**（见 §9）。
- `docker compose`（带空格，v2）失败的话，试试老写法 `docker-compose version`，那一节的命令把 `docker compose` 换成 `docker-compose` 即可。

### 1.2 检查三个宿主目录是否存在

**【NAS 宿主】**

```bash
ls -ld /vol1/1000/Download/qbittorrent \
       /vol1/1000/Library \
       /vol1/1000/Docker/media-ingest/data
```

三个路径**全都必须存在**（数据目录可以现建）。逐个说明：

| 路径 | 必须存在的原因 |
|---|---|
| `/vol1/1000/Download/qbittorrent` | qBittorrent 的下载根目录，本应用的**源** |
| `/vol1/1000/Library` | 媒体库根目录，本应用的**目标** |
| `/vol1/1000/Docker/media-ingest/data` | 本应用的**数据目录**（数据库、配置、日志），没有就 `mkdir -p` |

### 1.3 检查源与目标在**同一个挂载**上（关键！）

**【NAS 宿主】**

```bash
findmnt -T /vol1/1000/Download/qbittorrent -o TARGET
findmnt -T /vol1/1000/Library -o TARGET
```

两条输出的 `TARGET`（挂载点）**必须相同**（例如都是 `/vol1`，或都是 `/vol1/1000`）。相同 ⇒ 硬链接可行。

> ⚠️ **不要再用 `stat -c '%d'`（`st_dev`）相同作为依据。** Linux 的 `linkat(2)` 比较的是**挂载身份**，
> 不是 `st_dev`：两个目录完全可能 `st_dev` 相同却分属两个挂载（把同一个宿主目录分两条 bind 挂进容器就是这样），
> 此时 `link()` 一律 `EXDEV`（原理见 §3）。
>
> 容器里最终以**实证探针**为准：WebUI 媒体库页的「验证（含实证探针）」，`same_filesystem` 为三态
> `true` / `false` / `null`。

### 1.4 检查端口没被占用

**【NAS 宿主】**

```bash
ss -lntp | grep -q ':18081 ' && echo '18081 已被占用，请改用别的端口' || echo '18081 空闲，可用'
```

如果被占用：编辑 `.env` 里的 `PORT`，并把 `docker-compose.yml` 里的端口映射 `"18081:18081"` 左边那个数字也换掉（见 §8.1）。

### 1.5 检查运行身份（PUID / PGID）

**【NAS 宿主】**

```bash
id
id youruser
```

记下你自己的 `uid=` 和 `gid=`（例如 `uid=1000(youruser) gid=1001(Users)`）。这两个数字要填进 `.env` 的 `PUID` / `PGID`（见 §10）。

**还要确认容器身份的权限够用：**

```bash
# 源目录必须可读
sudo -u '#1000' test -r /vol1/1000/Download/qbittorrent && echo '源目录可读 OK' || echo '源目录对 uid 1000 不可读，需要处理权限'

# 目标目录必须可写
sudo -u '#1000' test -w /vol1/1000/Library && echo '目标目录可写 OK' || echo '目标目录对 uid 1000 不可写，需要处理权限'
```

> 如果你的 NAS 没有 `sudo`：用 `stat -c '%A %U %G %n' <路径>` 看权限位，再用 §10 的办法调整。

### 1.6 检查磁盘空间

**【NAS 宿主】**

```bash
df -h /vol1/1000/Docker /vol1/1000/Download /vol1/1000/Library
```

说明：**硬链接本身几乎不占额外空间**（它只是给同一个 inode 多起一个名字，详见 §3）。真正占空间的是 SQLite 数据库和日志。所以重点看 `/vol1/1000/Docker`（放数据卷）有没有几百 MB 以上余量。注意 `/data` **不需要**和下载/媒体库在同一个文件系统上。

### 1.7 检查 qBittorrent 容器（可选但强烈建议）

**【NAS 宿主】**

```bash
docker ps --filter name=qbittorrent --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
```

确认 qBittorrent 在跑，并记下它挂载下载目录的方式：

```bash
docker inspect qbittorrent --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
```

**必须能看到这一行**（或等价的同一宿主目录）：

```
/vol1/1000/Download/qbittorrent -> /vol1/1000/Download/qbittorrent
```

这条是整件事能成立的基础：本应用也把同一个宿主目录挂到**同一个容器内路径** `/vol1/1000/Download/qbittorrent`，qBittorrent 回调传来的 `%F`（content path）才能**零转换**直接用（见 §5）。

---

## 2. 卷挂载完整表（**最重要的一节**）

本应用**只有 2 个 bind mount**（宿主目录 = 容器目录，不是 Docker 命名卷）：

| # | 宿主（NAS）路径 | 容器路径 | 模式 | 用途 |
|---|---|---|---|---|
| 1 | `${MEDIA_ROOT:-/vol1/1000}` | `/vol1/1000`（**与宿主完全一致**） | `rw` 读写 | **单一共同父挂载**：下载源 `/vol1/1000/Download/qbittorrent` 与媒体库 `/vol1/1000/Library` 都在它下面，所以源与目标落在**同一个挂载**内 —— 这是硬链接能成立的**前提**（§2.1） |
| 2 | `./data`（= `/vol1/1000/Docker/media-ingest/data`） | `/data` | `rw` 读写 | 数据库、**配置文件**、任务日志、各库日志、密钥 |

> 硬链接与重命名都由镜像内的原生代码执行，配置在 `/data/config/`（就在 `/data` 卷里，**不需要额外挂载**）。
>
> 源与目标**必须共用这一条挂载**：把它们拆成两条独立 bind 会形成两个挂载，硬链接**永远失败**。详见 §2.1。
>
> 💡 **`MEDIA_ROOT` 是 compose / entrypoint 侧的变量**：只有 `docker/entrypoint.sh` 读它（做两条**只读**结构检查 ——
> 必须是绝对路径、不能是 `/`、不能等于 `DATA_DIR` 的挂载，且与 `DATA_DIR` **不在同一个挂载**里，否则拒绝启动），
> **应用代码不读它**。应用判断路径是否合法只看数据库设置里的「允许的源/目标根目录」；
> `DEFAULT_SOURCE_ROOT` / `DEFAULT_TARGET_ROOT` 只是这两个设置的**首次播种**初值（§4.2）。

**核对**：两条挂载都应可写，用 `docker inspect` 读出来应是

```
/vol1/1000 -> /vol1/1000                                rw=true   mode=rw
/vol1/1000/Docker/media-ingest/data -> /data   rw=true   mode=rw
```

容器内自查**"源与目标是否同一个挂载"**（这才是硬链接的判据）：

```bash
findmnt -T /vol1/1000/Download/qbittorrent
findmnt -T /vol1/1000/Library      # TARGET 应为同一个挂载点
```

> ⚠️ **源目录与目标同属一条可写挂载，不要依赖"写不进去"这层保护。** 单挂载形态下，内核让
> 「源单独 `:ro` 挂载」与「跨路径建硬链接」**互斥** —— 只能二选一，硬链接是本项目的核心功能，
> 所以 `:ro` 退场。替代它的是**可验证的保证**（代码层不写源目录 + 任务前后逐文件源完整性核对，
> 见 §2.1（b）与 §3.5）。**这是降级，不是等价替代，文档不粉饰这一点。**

对应 `docker-compose.yml` 片段（供你核对；实际以仓库里的 `docker-compose.yml` 为准）：

```yaml
services:
  media-ingest:
    image: media-ingest:latest
    build:
      context: .
      dockerfile: docker/Dockerfile
    container_name: media-ingest
    restart: unless-stopped
    env_file:
      - .env
    environment:
      CONFIG_DIR: "/data/config"
      MEDIA_ROOT: "${MEDIA_ROOT:-/vol1/1000}"
      # ⚠️ 这里**故意没有** CONFIG_SYNC_INTERVAL_SECONDS：同步周期是 DB 设置项
      # config_sync_interval_seconds（见下文「双向同步」），不是 env；写进来不会生效。
      # 其余键（PUID/PGID/HOST_PATH_MAP/DEFAULT_SOURCE_ROOT/DEFAULT_TARGET_ROOT/SECRET_KEY…）
      # 见仓库里的 docker-compose.yml 与 .env.example。
    ports:
      - "${PORT:-18081}:${PORT:-18081}"
    volumes:
      # 单一共同父挂载：源与目标必须同在一条 bind 里，否则 link() 一律 EXDEV（见 §2.1）
      - ${MEDIA_ROOT:-/vol1/1000}:${MEDIA_ROOT:-/vol1/1000}
      # 数据卷（持久化 SQLite / 任务日志 / 每库日志 / 配置文件）；宿主路径放哪儿都行
      - /vol1/1000/Docker/media-ingest/data:/data
```

下面逐条解释**为什么必须这么挂**。

### 2.1 为什么只挂一个共同父目录（以及源目录为什么不能单独挂 `:ro`）

**（a）为什么容器路径必须与宿主完全一致（`/vol1/1000` → `/vol1/1000`）**

qBittorrent 容器自己也把同一个宿主目录挂在 `/vol1/1000/Download/qbittorrent`。qBittorrent 的「运行外部程序」会把内容路径以 `%F` 传给本应用，而这个 `%F` 是**qBittorrent 容器视角**的路径，长这样：

```
/vol1/1000/Download/qbittorrent/Bangumi/Show A
```

本应用的容器路径与宿主（以及 qBittorrent 容器）**完全一致**，所以这个路径**拿来就能用，一个字都不用改**，也不需要 `HOST_PATH_MAP` 翻译（它因此**通常恒等**）。
如果本应用改成挂到比如 `/dl`，那所有回调都会指向一个不存在的路径 —— 这也是为什么"容器路径 = 宿主路径"是**契约**而不是个人偏好（见 [`docs/contracts/ARCHITECTURE.md`](../contracts/ARCHITECTURE.md) §2）。

**（b）为什么源与目标必须放进同一个挂载 —— 以及为什么 `:ro` 因此退场**

Linux 的 `linkat(2)` 要求源与目标**在同一个挂载内**（内核 `do_linkat()` 比较挂载身份：`old_path.mnt != new_path.mnt` ⇒ `EXDEV`），
**与 `st_dev`、`fsid`、btrfs 子卷都无关**。一旦把源和目标挂成两条独立 bind ⇒ 它们成了两个挂载 ⇒
**硬链接永远建不出来**（这正是"任务全部 `link_failed`、日志 `[Errno 18] Invalid cross-device link`"的根因），
而只看 `st_dev` 的预检还会报"可以创建硬链接"—— **假绿**。所以只挂 `${MEDIA_ROOT:-/vol1/1000}` 一条，让源与目标天然同挂载。

> 业界同生态的做法一致：*arr（Sonarr / Radarr）官方文档明确要求下载目录与媒体库位于**同一个挂载点**下，否则"硬链接"只能退化成复制或直接失败。

代价是：**源目录不能再单独以 `:ro` 挂载**（内核让这两件事互斥）。项目硬约束「任何时候不得移动、删除或修改 qBittorrent 原始下载文件」因此改由三层**可验证**的方式保证：

1. **代码层从不写源目录**（任何代码路径都不写源根，见 `tests/test_no_source_mutation.py`）；
2. **任务前后逐文件源完整性核对**：开始前对源根快照 `(相对路径, size, ino, mtime_ns)`，结束后复核，
   有差异就写 `Task.result.source_integrity = {ok: false, changed: [...]}` 并在 `Task.result.warnings[]` 里给中文告警（列出路径）；
3. **硬链接本身不会被误报**：`nlink` / `ctime` 的变化**不算差异**（建硬链接会让源文件 `nlink+1`、`ctime` 变，但内容 / `mtime` / `ino` 不变）。

> 📌 **请诚实理解这条约束**：包一层 bind 之后，源目录**不可能**在部署层面被内核挡住写入，
> 因此保护来自**可验证的保证**（代码不写 + 跑完逐文件核对 + 测试）。**这是降级、不是等价替代。**
> 换句话说：现在能**发现**源被动过并告警，但不能**阻止**它。不要把源目录描述成"只读"。

### 2.2 为什么媒体库要挂到 `/vol1/1000/Library` 并且是 `rw`

- **为什么 `rw`**：应用要在里面创建目录、创建硬链接、并**原地改名**（重命名由规则引擎完成，见 §2.3）。只读就没法工作。
- **为什么是 `/vol1/1000/Library`**：应用把媒体库根参数化成你配置的容器路径（例如 `/vol1/1000/Library/Bangumi`），所以容器内不必非叫 `/vol1/1000/Library`；但为了与宿主（以及 Emby 的挂载习惯）一致，本方案统一用 `/vol1/1000/Library`。
- **必须与 `/vol1/1000/Download/qbittorrent` 在同一个挂载上**：见 §3。

### 2.3 执行体：硬链接与重命名由谁执行

硬链接由原生实现执行（`app/processors/hardlink.py`），重命名由配置驱动的规则引擎执行
（`app/processors/engine.py`），两者都是镜像内的应用代码。

**硬链接阶段的可观察输出是冻结契约**：stdout 与日志文件的逐字格式记录在
`docs/contracts/BASELINE.md` §1，由现行原生实现遵循，锁定位是 `tests/test_hardlink_native.py`
里的**冻结黄金值**（同一棵 fixture 树、stdout 与日志文件逐字比对）。因此把镜像搬到别的设备部署，
硬链接功能不受影响。重命名的规则、字段与预设见 §2.5–§2.8。

### 2.4 为什么数据要挂到 `/data`

`/data` 里放的是**所有会变的状态**：

| 路径（容器内） | 内容 |
|---|---|
| `/data/mlm.db` | SQLite 数据库：账号、处理器与媒体库规则、任务历史、日志、设置 |
| `/data/mlm.db-wal` / `/data/mlm.db-shm` | SQLite WAL 模式的伴生文件（备份时要一起带上，见 §7） |
| `/data/secret.key` | 会话签名密钥（删了 = 所有人被登出） |
| `/data/config/libraries.yaml` | **媒体库清单**（与数据库双向同步，见 §2.6） |
| `/data/config/processors/<id>.yaml` | **每个处理器一个文件**（重命名规则，与数据库双向同步） |
| `/data/logs/hard_links.log` | 硬链接日志（逐字格式见 §2.8 与 [`docs/contracts/BASELINE.md`](../contracts/BASELINE.md) B14；记录的是容器路径） |
| `/data/logs/<log_file>` | **每个媒体库一条**重命名日志（`json_cache`，默认 `<slug>cache.log`） |
| `/data/logs/`（其余） | **没有别的文件**：应用运行日志在 SQLite 的 `task_logs` 表 + 内存环形缓冲，另只走控制台（`docker logs`），不写 `/data/logs/` |

**为什么必须是宿主 bind mount**：容器层是"一次性的" —— `docker compose down`、重装镜像、升级，容器都会被丢掉重建。状态只放在容器里就等于没保存。把 `./data` 映射到宿主目录后，容器随便删，数据都在。

**`/data` 不需要**和下载/媒体库在**同一个挂载**上（那是硬链接的前提，§2.1）—— 硬链接只在 `/vol1/1000/Download/qbittorrent` 与 `/vol1/1000/Library` 之间发生，`/data` 只是普通文件读写。放在系统盘或 SSD 上反而更好。

### 2.5 全新装的默认配置对照表（内置处理器）

本应用的容器**自带三个内置处理器**（`builtin: true`：不可删除、只能重置）。全新装、还没有任何人工
改动时，它们就是下面这样 —— 你可以拿这张表对照自己部署后的
`GET /api/processors` 或 `/data/config/processors/*.yaml`：

| 处理器 id | 名称 | 命名方式 `kind` | 执行体 | 默认日志文件 | 文件作用域 |
|---|---|---|---|---|---|
| `hardlink_only` | 只建硬链接 | `hardlink_only` | 原生实现 `app/processors/hardlink.py` | 不写重命名日志 | —（只建链接，不改名） |
| `bangumi` | 番剧 | `rename` | 规则引擎 `app/processors/engine.py` | `bangumicache.log` | `subdir`（`Season N` 子目录） |
| `movie` | 电影 | `rename` | 规则引擎 `app/processors/engine.py` | `moviecache.log` | `title`（标题文件夹直接取文件） |

启用状态、接受后缀（`.mkv`/`.mp4`/`.ass`）、命名段、`episode_patterns` 等完整字段见
[`docs/contracts/PROCESSORS.md`](../contracts/PROCESSORS.md) §3（那份文件是处理器契约的唯一真源）。

**两个预设的差别**（处理器页「恢复默认」时可选）：

| 预设 | 含义 |
|---|---|
| `default` | 内置推荐值：在 `legacy` 的基础上多出**拒绝目录**规则（番剧、电影各一套）、把番剧的 `episode_fallback` 留空（`null`）⇒ **认不出集数就保留原文件名**（不会命名成 `… S01ENone.mkv`） |
| `legacy` | **最朴素的旧口径**：`reject_dirs: []` + `episode_fallback: "None"` + 那两条最宽松的集数规则（含大小写敏感的后缀白名单、没有集数号也照样重命名成 `ENone`） |

> `default` 相对 `legacy` 的**四处增量**（① 番剧的 `reject_dirs`；② 电影的 `reject_dirs`；
> ③ 番剧的 `episode_fallback = null`；④ 番剧的 `episode_patterns` 换成 9 条带边界约束的有序规则），
> 都必须在
> [`CHANGELOG.md`](../../CHANGELOG.md) 与 [`docs/contracts/COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) 里显式标注。
> **想要最朴素的口径就用 `legacy`。**
>
> ⚠️ **`episode_fallback` 不随镜像升级自动改变**：第 1 步的播种**只在 `processors` 表为空时**发生，
> 既有实例的处理器规格是**播种后独立存在**的（DB 与 `processors/*.yaml` 里已经存了旧的字符串
> `"None"`）。想让**已经存在的**番剧处理器也"认不出集数就保留原名"，二选一：
> ① 只改这一项：把 `/data/config/processors/<id>.yaml`（内置番剧处理器是 `bangumi.yaml`）里的
> `episode_fallback: "None"` 改成 `episode_fallback: null` —— WebUI 与配置文件**双向同步**，默认
> 5 秒一轮自动生效；等价的接口调用是 `PUT /api/processors/bangumi`，字段名就是 `episode_fallback`，
> 值给 `null`；
> ② `POST /api/processors/bangumi/reset`，body `{"preset":"default"}` —— 一**整套**回到推荐默认
> （会把你对这个处理器的其它自定义改动一起覆盖掉）。**新建**的处理器不受影响（模板取自
> `builtin_spec()`，本来就是 `null`）。
>
> ⚠️ **别用「处理器」编辑页的「集数兜底」输入框清空来表达"留空"**：它是普通文本输入框，清空后
> 提交的是**空串** `""` 而不是 `null`，引擎会把空串当成"已解析"（结果会命名成 `S01E00…`，仍然撞名）。
> 这个方向请按上面 ①/② 做（反方向——把 `null` 改成字符串 `"None"`——用输入框填 `None` 是可以的）。

首次启动时会自动完成（**不需要任何人工操作**）：

1. `processors` 表为空 ⇒ 写入上表三个内置处理器的 `default` 预设；
2. 把 `libraries.processor` 取值（`bangumi`/`movie`/`hardlink_only`）映射为同名 `processor_id`；
3. 立刻把 `libraries.yaml` 与 `processors/*.yaml` 写出来（等价于一次 export）。

**核对命令（【任意电脑】/【NAS 宿主】均可）：**

```bash
# 当前处理器列表（含 builtin / kind / 执行体）
curl -s http://192.168.1.100:18081/api/processors | python3 -m json.tool

# 配置文件视角（每个文件的 sha256、是否与 DB 同步、最近导入/导出时间、导入错误）
curl -s http://192.168.1.100:18081/api/config | python3 -m json.tool

# 直接看某个处理器的完整规则（宿主机上）
cat /vol1/1000/Docker/media-ingest/data/config/processors/bangumi.yaml
```

### 2.6 配置文件目录（`/data/config`）：布局、双向同步与冲突处理

**为什么要有这一节**：配置文件与 WebUI 需要双向同步。双向同步最怕的是"两边同时改、互相打架"，
所以规则是**单一真源 + 单向仲裁**：**运行时真源永远是 SQLite**，配置文件只是它的**可移植镜像**。

#### 2.6.1 布局

```
/data/config/
├── libraries.yaml            # 媒体库清单（processor_id、路径、匹配条件、log_file）
└── processors/
    ├── bangumi.yaml          # 一个处理器一个文件，文件名 = <id>.yaml
    ├── movie.yaml
    ├── hardlink_only.yaml
    └── <custom-id>.yaml      # 你在 WebUI 里新建的处理器
```

- 格式是 **YAML 1.1**（`yaml.safe_load` / `yaml.safe_dump`，`allow_unicode=True`、`sort_keys=False`、
  `default_flow_style=False`），UTF-8、无 BOM、以 `schema_version: 2` 开头。新增依赖只有 `PyYAML==6.0.2`。
- **正则一律写成单引号标量**（`pattern: '^Season (\d+)$'`），避免 YAML 的转义规则介入。
- `/data/config` 位于 `/data` 卷内，**不需要额外挂载**；目录由 `CONFIG_DIR` 指定（默认 `/data/config`）。
- 应用**不读** `.env`。

#### 2.6.2 同步语义

| 方向 | 触发 | 行为 |
|---|---|---|
| **DB → 文件** | 任何一次通过 API 的成功写入（WebUI 保存） | 在**同一个请求内**把受影响文件**原子写出**（先写 `*.yaml.tmp`，再 `os.replace`），并把新 sha256 记进 `config_files` 表 |
| **文件 → DB** | 后台线程每 `config_sync_interval_seconds` 秒轮询（**DB 设置项**，默认 **5**、区间 0–86400；**不是 env**） | 比对每个文件的 mtime + sha256；若哈希 ≠ 上次由应用写出/导入时记录的哈希 ⇒ 判定为**外部编辑**，执行导入 |
| 导入成功 | — | 在一个事务里替换 `libraries` / `processors`，写一条 `config_events(direction='import')`，WebUI 显示「配置文件已从磁盘重新导入」；随后**回写一次**文件（规范化格式）使哈希对齐，避免反复抖动 |
| 导入失败 | YAML 语法错、`schema_version` 不对、规格校验不过 | **不改数据库**（继续按旧规则跑），写一条 `config_events` 错误记录，通过 `GET /api/config` 的 `errors` 暴露到 WebUI 顶部红色横幅；**你的文件不会被覆盖** |

- **删除语义**：文件里消失的**非内置**处理器 / 媒体库，会随导入从数据库删除（并写事件）；
  **内置处理器永不删除**，只允许重置（`POST /api/processors/{id}/reset`）。
- **冲突窗口**：API 写入持 `_config_lock`，后台导入持同一把锁 ⇒ 不会交错。
- **关掉自动导入**：把设置项 `config_sync_interval_seconds`（**DB 设置项，不是 env**）设成 `0`，之后只在你手动
  `POST /api/config/reload` 时导入（适合"配置文件由别的工具生成、我不想它被悄悄读进来"的场景）。

#### 2.6.3 冲突与故障：一张表说清怎么办

| 你做了什么 | 会发生什么 | 怎么办 |
|---|---|---|
| 在 WebUI 保存后，又在宿主机上改了同一个文件 | 5 秒内文件版本被导入，**后写的赢** | 想以后者为准：什么都不用做；想以 DB 为准：`POST /api/config/export` 把 DB 重新写出去 |
| 文件被写坏了（YAML 缩进错、正则不能编译、`extensions` 没以 `.` 开头……） | **数据库不受影响**，页面顶部红色横幅 + 错误详情；文件保持原样 | 按错误提示改文件（对照表见 [`docs/guides/USER_GUIDE.md`](USER_GUIDE.md) 的「配置校验错误对照表」），或直接删掉该文件再 `POST /api/config/export` 重新生成 |
| 手工删掉了某个 `processors/<id>.yaml` | 下次导入时该自定义处理器从 DB 删除；若还有媒体库引用它，导入会**整体失败**并提示引用关系 | 先改媒体库的 `processor_id`，或把文件放回去 |
| 手改文件后"没反应" | 可能是 mtime+sha256 与记录一致（例如你改回了原内容）、或轮询还没到 | `curl -s .../api/config` 看 `files[].in_sync`；急的话 `POST /api/config/reload?force=true` |
| 想把某个处理器恢复成出厂设置 | — | `POST /api/processors/{id}/reset`，body `{"preset":"default"}` 或 `{"preset":"legacy"}` |
| 写入失败（`Permission denied`） | `/data/config` 对容器 uid 不可写 | 检查 `PUID`/`PGID`（§10），宿主上 `ls -ld .../data/config` |

> 备份与恢复配置目录的步骤见 **§7.1 与 §7.5**。

### 2.7 `processors/*.yaml` 字段全表

一个处理器就是**一份 YAML**（文件名 = `<id>.yaml`），下面这些字段构成它的全部内容。
完整示例、每个内置处理器的默认值、以及命名表达式的可用名字，见
[`docs/contracts/PROCESSORS.md`](../contracts/PROCESSORS.md) §3；这一节是"改文件时对着看"的速查表。

| 字段 | 类型 | 含义 / 取值 | 备注 |
|---|---|---|---|
| `schema_version` | int | 恒为 `2` | 必须写在第一行；不符会被导入拒绝 |
| `id` | str | 处理器 id，`^[a-z][a-z0-9_-]{1,31}$` | 全局唯一；**改了 id 等于新建一个**，文件名必须与它一致 |
| `name` | str | 显示名（中文可） | 媒体库表单里看到的那个名字 |
| `description` | str | 一句话说明 | 纯展示 |
| `kind` | str | `rename` 或 `hardlink_only` | `hardlink_only` 只建硬链接、不重命名（由原生实现 `app/processors/hardlink.py` 完成） |
| `enabled` | bool | 该处理器是否可选 | `false` 时不能用来新建媒体库 |
| `builtin` | bool | 是否内置 | 内置**不可删除**，只能 `reset` |
| `traverse.title_depth` | int | 标题文件夹在媒体库根下的层级，当前固定 `1` | 即库根的直接子目录 |
| `traverse.file_scope` | str | `subdir`（从季目录取文件）或 `title`（直接从标题文件夹取） | 电影用 `title` |
| `traverse.accept_dirs[]` | list | `{pattern, season_group}`，季目录正则 | 空数组 ⇒ 任何子目录都接受；`season_group` 指明哪个捕获组是季号（**缺省 1**；`0` = 只判断接受、不取季号，与数组为空**逐字相同**：`season_raw` 空、`season` 补零为 `"00"`） |
| `traverse.reject_dirs[]` | list | `{pattern}`，命中即**整棵子树跳过** | `legacy` 预设里是空数组 |
| `traverse.skip_season_zero` | bool | 季号补零后为 `"00"` 时跳过 | 与 `legacy` 预设一致 |
| `extensions[]` | list[str] | 接受的后缀白名单，如 `[.mkv, .mp4, .ass]` | **大小写敏感**；每项必须以 `.` 开头。这是"处理器默认值"：设置项 `rename_extensions`（全局）与 `libraries.rule_override.extensions`（单库）会**盖过**它，见 [`docs/contracts/PROCESSORS.md`](../contracts/PROCESSORS.md) §3.1 |
| `naming.expressions` | map | 具名表达式，按**书写顺序**求值，可引用上下文与前面算出的名字 | 右值是 Python 表达式（见下方 ⚠️） |
| `naming.segments[]` | list | 新文件名的组装顺序，每项 `{param}` / `{literal}` / `pad` | 顺序可自由调整 |
| `episode_patterns[]` | list | `{name, pattern}`，剧集号正则，按顺序取**首个命中** | 命名里用到 `episode` 时必需 |
| `episode_fallback` | str \| null | 全不命中时的字面量；**留空（`null`）= 认不出集数就保留原文件名、不改名**（内置 `bangumi` 的推荐默认） | 写成字符串 `"None"` 会让认不出集数的文件显示成 `S01ENone`（`legacy` 预设正是这么配的） |
| `selection.per` | str | `file`（每个匹配文件都处理）或 `extension`（同后缀只留体积最大者） | 番剧 `file`、电影 `extension` |
| `log_file` | str | 该处理器默认的日志文件名 | **只能是文件名**（不含 `/`、`..`），落在 `/data/logs/` |
| `log_format` | str | 当前恒为 `json_cache` | `{ "<标题文件夹>": {"size": N, "timestamp": "…", "works_dir": "…"} }` |

`naming.segments[].param` 可用的名字：`title`、`filename`、`stem`、`ext`、`extname`、`season_raw`、
`season`、`episode_raw`、`episode`、`suffix`、`works_dir`、`rel_dir`、`episode_resolved`（只读键，逐个含义见
[`docs/contracts/PROCESSORS.md`](../contracts/PROCESSORS.md) §3.1）。

> ⚠️ **安全面**：`naming.expressions` 的右值是在应用进程内用 `eval` 求值的 Python 表达式。
> 这是为了满足"可拓展、不写死"而接受的取舍 —— 能改 `/data/config/` 的人本来就能改容器配置。
> 你自己的库如果不需要它，就让 `expressions: {}`。

### 2.8 每库日志文件（都在 `/data/logs/`）

的日志分三类，**全部**落在 `/data/logs/`，**不允许**按媒体库分子目录：

| 类别 | 文件 | 谁写 | 形态 | 作用 |
|---|---|---|---|---|
| **硬链接日志** | `/data/logs/hard_links.log` | 建链阶段的原生实现 `app/processors/hardlink.py` | 纯文本 `时间 - Source: … - Folder: … - Folder size: …`。**只写两类记账行**（体积登记 / 孤儿目录清理），应用日志不混入；历史行不清理 | **兼容与审计台账**；"这个文件夹已经链过了"的幂等依据是每库作品账本（见 [`docs/contracts/BASELINE.md`](../contracts/BASELINE.md) B14） |
| **每库一条重命名日志** | `/data/logs/<log_file>` | 规则引擎 | `json_cache`：`{ "<标题文件夹>": {"size": N, "timestamp": "…", "works_dir": "…"} }` | "这个文件夹已经重命名过了"的幂等依据 |
| **任务日志** | SQLite `task_logs` + 内存环形缓冲 | 应用 | 结构化（级别/时间/流） | 网页任务详情页看到的那份 |

**文件名的生成规则**（新建媒体库时不填 `log_file` 就用它）：

1. 先取**该处理器登记的日志名**：内置三个是 `bangumicache.log` / `moviecache.log` /
   `hardlink_only.log`，自定义处理器是它的 `<id>cache.log`；
2. 再做**按库唯一化**：若这个名字已被**其它库**或**其它处理器**占用，就在扩展名前追加
   `-2`、`-3`…（例如第二个用 `bangumi` 的库会拿到 `bangumicache-2.log`）；
3. **第一个**使用某处理器的库保持登记名；**磁盘上已有同名文件时照样用它并继续追加**
   （这是延续既有日志、避免对真实媒体库全量重跑的关键）；
4. 兜底（处理器连登记名都没有）：按库名转写 `{slug}cache.log` —— `slug` = 库名转小写、
   **非 `[a-z0-9]` 的字符折叠成 `-`**（中文库名会折成一串 `-`，建议手工填一个 ASCII 文件名）；
5. **显式填写**的 `log_file` 与其它库已占用名撞车 ⇒ **422**（字段 `log_file`）。

**三条硬规则**（在媒体库表单里强校验）：

- `log_file` **只能是文件名** —— 含 `/` 或 `..` 会直接 422 报错；
- 新建库时如果该文件不存在，应用会创建一个**空 `{}`**；
- 磁盘上已经存在的 `/data/logs/bangumicache.log`、`moviecache.log` 会被**原样保留、继续追加**（不会重新播种或清空）。

**自查**（【NAS 宿主】）：

```bash
cd /vol1/1000/Docker/media-ingest/data/logs
ls -l                                   # 每个库一个文件 + hard_links.log
python3 -m json.tool bangumicache.log | head -20   # 看条目与 size
```

> ⚠️ 同一条"日志即已处理"的副作用依然成立：**日志里记着某标题文件夹，即使你后来把库里的文件删了，
> 下次运行也不会重新建/重新命名它** —— 删掉日志里对应那一项（或整份日志）才会重做（见
> [`docs/guides/FAQ.md`](FAQ.md) 第 23 问）。
>
> `/data/logs/` 整个目录在 `/data` 卷里，备份跟着 §7 走。

---

## 3. 「源和目标必须在同一个挂载」的原理与自查

### 3.1 什么是硬链接（一句话版）

硬链接**不是复制文件**。它是在目录里新增一个文件名，指向**已经存在的同一份数据**（同一个 inode）。

结果就是：

- 同一个文件在硬盘上**只有一份数据**，占用空间**不会翻倍**；
- 下载目录里的文件和媒体库里的文件，是**同一份数据的两个名字**；
- 你删掉下载目录里那个名字（删掉原始下载），媒体库那个名字**依然能正常播放**；
- 它们的 inode 号（`ls -i` 看到的那个数字）**完全一样**。

### 3.2 为什么必须"同一个文件系统"**而且**"同一个挂载"

硬链接的本质是"在目录 B 里写一个指向 inode N 的条目"。inode 号**只在单个文件系统内部唯一** —— 换个文件系统，`inode 12345` 完全可能是另一个不相干的文件。
所以在 Linux 上其实有**两条**硬要求：

1. **同一个文件系统**（POSIX 的语义前提）；
2. **同一个挂载** —— 内核 `linkat(2)` 会先比较挂载身份（`do_linkat()` 里的 `old_path.mnt != new_path.mnt`），
   不同挂载**一律 `EXDEV`**，**与 `st_dev`、`fsid`、btrfs 子卷都无关**。

跨过去就会得到：

```
OSError: [Errno 18] Invalid cross-device link: '/vol1/1000/Download/qbittorrent/Bangumi/x.mkv' -> '/vol1/1000/Library/Bangumi/x.mkv'
```

即 **`EXDEV: Invalid cross-device link`**。这是内核级限制，**没有任何办法绕过**（除非复制文件 —— 而复制恰恰是本项目禁止的）。

**判据的层级（本项目现在怎么判）**：

| 层级 | 判据 | 可信度 |
|---|---|---|
| 主判据 | **实证 `link()` 探针**：从源根有界递归取一个已存在普通文件，link 到目标根的临时名再删（**不写源目录**） | 能建就是能建 |
| 预筛 | `/proc/self/mountinfo` 的 **mount ID** 是否相同 | 「**必要不充分**」—— btrfs 同一挂载跨子卷仍可能 `EXDEV` |
| 降级（仅无 `/proc` 时） | `st_dev` 是否相同 | **不能**预测 `link()` 成功 —— 只凭它判断会产生"假绿"的根因 |
| 无法判断 | 源树下找不到可链的普通文件 ⇒ `same_filesystem: null` | 明确说"无法实证"，不猜 |

> 🚫 **本应用绝不回退为复制文件。**
> 判定为跨挂载时，运行前**只警告、不硬拦**（结论进 `Task.result.warnings[]`，中文、带可执行修法），
> 任务照跑；真跑起来后由执行前后的逐文件核对给出最终结论（失败就是 `link_failed`，**不会**悄悄复制一份冒充硬链接）。
> 这同样是刻意的设计：静默复制会让你的硬盘空间翻倍、并且"硬链接"这个前提彻底失效（删掉下载文件后媒体库那份就不再共享数据了）。

### 3.3 自查命令与判读方法

**（1）`findmnt -T`：看这个路径究竟属于哪个挂载点（**最直接的判据**）**

**【NAS 宿主】／【容器内】**

```bash
findmnt -T /vol1/1000/Download/qbittorrent -o TARGET
findmnt -T /vol1/1000/Library -o TARGET
```

**判读**：两条的 `TARGET`（挂载点）**相同**才是同一个挂载。不同 ⇒ 硬链接必然 `EXDEV`（与 `st_dev` 是否相同无关）。

**（2）`stat -c '%d'`：看设备号（**仅供参考，不能作为结论**）**

**【NAS 宿主】**

```bash
stat -c '%d %n' /vol1/1000/Download/qbittorrent
stat -c '%d %n' /vol1/1000/Library
```

**判读**：数字**不同** ⇒ 必然不行（可以据此排除）；数字**相同** ⇒ **不能说行** —— 请回到（1）看挂载点，
或用 WebUI 的「验证（含实证探针）」核对。

在**容器内**核对（更贴近实际执行环境）：

**【容器内】**

```bash
docker exec media-ingest stat -c '%d %n' /vol1/1000/Download/qbittorrent /vol1/1000/Library
```

**（3）`df`：看挂载点**

**【NAS 宿主】**

```bash
df /vol1/1000/Download/qbittorrent /vol1/1000/Library
```

**判读**：看 `Mounted on` 那一列。两个路径落在**同一个挂载点**才有效（例如都是 `/vol1/1000` 或都是 `/vol1`）。

**（4）`/proc/self/mountinfo`：看 mount ID（预筛）**

```bash
# 容器内：两条路径所属挂载的 mount ID 是否相同
awk '{print $1, $5}' /proc/self/mountinfo | head
```

相同 ⇒ 通过预筛；**不相同** ⇒ 一定跨挂载。但预筛通过**不等于**能建 —— btrfs 同一挂载跨子卷仍可能 `EXDEV`（**必要不充分**）。

**（5）`ls -i`：直接看 inode（用来验证结果，不只是判断）**

建好链接之后，两个名字的 inode 号必须相同：

**【NAS 宿主】**

```bash
ls -i /vol1/1000/Download/qbittorrent/Bangumi/"Show A"/Season\ 1/*.mkv
ls -i /vol1/1000/Library/Bangumi/"Show A"/Season\ 1/*.mkv
```

（路径含空格所以要加引号和 `\ `。也可以先 `cd` 进目录再 `ls -i`。）

再看「链接计数」：硬链接数会 ≥ 2。

**【NAS 宿主】**

```bash
stat -c '%h %i %n' /vol1/1000/Library/Bangumi/"Show A"/Season\ 1/x.mkv
```

- `%h` = 硬链接数（≥ 2 说明至少还有另一个名字，比如原始下载那份）；
- `%i` = inode 号，两边必须一致。

**（6）`du` 反证"没占双倍空间"**

**【NAS 宿主】**

```bash
du -sh /vol1/1000/Download/qbittorrent/Bangumi/"Show A"
du -sh /vol1/1000/Library/Bangumi/"Show A"
```

两个数字会**几乎一样大**，但整块盘的已用空间并没有增加 —— 因为 `du` 统计的是"这套数据有多大"，而盘上只存了一份。用 `df -h /vol1/1000` 看总用量前后对比更能说明问题。

### 3.4 跨挂载时你会看到什么

**在应用里**（任务详情 / 日志 / 预检结果）：

```
跨挂载：源 /vol1/1000/Download/qbittorrent/Bangumi 与目标根 /vol1/1000/Library/Bangumi 不在同一个挂载，
os.link 会返回 EXDEV。本应用不会回退为复制文件。
请把下载目录与媒体库目录放进同一个 bind 挂载（见 docker-compose.yml 的 MEDIA_ROOT）。
```

任务本身会是 `link_failed`（**不是**静默成功，也**不是**复制出来的假硬链接）；
运行前的这条判定只进 `warnings[]`，**不会**阻止任务启动。

**如果你手动跑 `ln`**：

**【NAS 宿主】**

```bash
ln /vol1/1000/Download/qbittorrent/test.mkv /vol1/1000/Library/test.mkv
# ln: failed to create hard link '/vol1/1000/Library/test.mkv' => '/vol1/1000/Download/qbittorrent/test.mkv': Invalid cross-device link
```

**怎么修**：改成**单一共同父挂载** —— 把下载根与媒体库根放进**同一条 bind**（`docker-compose.yml` 的 `MEDIA_ROOT`）：

```yaml
volumes:
  - ${MEDIA_ROOT:-/vol1/1000}:${MEDIA_ROOT:-/vol1/1000}
  - ./data:/data
```

**不要**拆成 `…/qbittorrent:…` 与 `…/Library:…` 两条（那样会形成两个挂载，必然失败）。

> 实在想跨盘？只有"复制"这一条路，而本应用刻意不做（见 [`docs/guides/FAQ.md`](FAQ.md)「能不能跨硬盘」）。此时的正解是：把媒体库根换到与下载根同一个挂载下的目录。

### 3.5 源目录的只读保证：从「结构性」到「可验证」（重要）

源目录**不能**再单独以 `:ro` 挂载（见 §2.1（b）），改由下面三项保证：

| 手段 | 说明 |
|---|---|
| 代码层不写源目录 | 任何代码路径都不写源根；回归测试 `tests/test_no_source_mutation.py` 守住 |
| 任务前后**逐文件源完整性核对** | 开始前快照 `(相对路径, size, ino, mtime_ns)`，结束后复核；有差异 ⇒ `Task.result.source_integrity = {ok: false, changed: [...]}`，并在 `Task.result.warnings[]` 给中文告警（列出路径） |
| 不误报 | 硬链接本身会让源文件 `nlink+1`、`ctime` 变 —— 这两项**不在**比较范围内，所以不会误报 |

> 📌 **结论（请照抄口径）**：源目录**不是**"只读"的。内核让
> 「源单独 `:ro` 挂载」与「跨路径硬链接」**互斥**，所以保护方式是**可验证的保证** ——
> ① 代码层永不写源目录；② 任务前后逐文件源完整性核对（`Task.result.source_integrity`）+ `warnings[]` 中文告警；
> ③ `tests/test_no_source_mutation.py`。**这是降级、不是等价替代**：现在能**发现**源被动过并告警，但不能**阻止**它。

---

## 4. 安装步骤（从零到页面可访问）

### 4.0 约定值

| 项 | 值 |
|---|---|
| Compose 项目目录（示例） | `/vol1/1000/Docker/media-ingest/` |
| 镜像名 / 容器名 | `media-ingest:latest` / `media-ingest` |
| 数据卷宿主目录 | `/vol1/1000/Docker/media-ingest/data` |
| 对外端口 | `18081` |
| 访问地址 | `http://192.168.1.100:18081`（换成你的 NAS IP） |
| 容器内运行用户 | `app`（`PUID=1000` / `PGID=1001`） |

### 4.1 把项目文件放到 NAS 上

**方式 A：在你自己电脑上把整个项目目录拷到 NAS 的共享文件夹**

**【任意电脑】**（把 `项目目录` 换成你本地路径）

```bash
scp -r ./媒体入库整理 youruser@192.168.1.100:/vol1/1000/Docker/media-ingest
```

**方式 B：先在 NAS 上建目录，再用 File Station / SMB 拖进去**

**【NAS 宿主】**

```bash
mkdir -p /vol1/1000/Docker/media-ingest
```

然后把项目里的**全部文件**（包括 `docker-compose.yml`、`docker/`、`app/`、`web/`）放进去。

**确认放对了（这些文件必须都在）：**

**【NAS 宿主】**

```bash
cd /vol1/1000/Docker/media-ingest
ls -1
# 至少要看到：docker-compose.yml  .env.example  docker/  app/  web/  docs/
```

### 4.2 建数据目录、写 `.env`

**（1）建数据目录**

**【NAS 宿主】**

```bash
mkdir -p /vol1/1000/Docker/media-ingest/data
```

**（2）复制配置模板并编辑**

**【NAS 宿主】**

```bash
cd /vol1/1000/Docker/media-ingest
cp .env.example .env
```

用你顺手的编辑器打开 `.env`（NAS 上可用 `vi .env`，或通过共享文件夹在你的电脑上用记事本编辑）：

```bash
vi .env
```

**必须核对的几项**：

```bash
# 容器内运行身份。用 §1.5 里 `id youruser` 得到的 uid/gid 填
PUID=1000
PGID=1001

# 时区
TZ=Asia/Shanghai

# 数据目录 / 端口 / 配置目录（一般不用改）
DATA_DIR=/data
PORT=18081
CONFIG_DIR=/data/config                       # 处理器与媒体库配置文件所在目录（在 /data 卷内，无需额外挂载）
# ⚠️ 同步周期**不是环境变量**：真源是数据库设置项 `config_sync_interval_seconds`
# （默认 5 秒，区间 0–86400，0 = 关闭自动导入，在设置页或 PUT /api/settings 里改）。
# `CONFIG_SYNC_INTERVAL_SECONDS` 不是环境变量：写进 `.env` 不会生效（唯一真源是 DB 设置项）。

# ★ 单挂载形态的核心：**共同父挂载的宿主根**。下载目录与媒体库目录都必须在这一个目录下面。
# compose 用它做卷的宿主侧（容器路径 = 宿主路径），所以**改了它必须同时改 compose**：
#   - ${MEDIA_ROOT:-/vol1/1000}:/vol1/1000
# 填错、或换成另一块盘 ⇒ 硬链接会全部失败（跨挂载 EXDEV），见 §2.1、§3.5。
MEDIA_ROOT=/vol1/1000

# 两个白名单的**首次播种默认值**（新装第一次启动、数据库里还没有这两项设置时写入）。
# ⚠️ 只影响**首次播种**：对已经存在的数据库**不生效**。改这两个 env 解决不了已装实例的路径问题，
# 那种情况要在设置页（或 PUT /api/settings）直接改 allowed_source_roots / allowed_target_roots。
DEFAULT_SOURCE_ROOT=/vol1/1000/Download
DEFAULT_TARGET_ROOT=/vol1/1000/Library

# 路径映射表：容器路径 -> 宿主路径。格式是 JSON；键 = 容器路径，**必须以 / 开头**；值 = 宿主路径。
# 它**只用于界面展示**：把网页上的容器路径（/vol1/1000/Download/qbittorrent/Bangumi）翻译成宿主路径给人看。
# 填错或漏填**不影响功能** —— 它只是一张给人看的翻译表。
# ★ 推荐部署形态下**容器路径 = 宿主路径**，映射恒等、没有需要翻译的项 ⇒ **保持空对象即可**
#   （等价于随附 .env.example 里那份**显式恒等映射** `{"/vol1/1000":"/vol1/1000"}`）
HOST_PATH_MAP={}

# 首次启动可自动创建管理员（可选）。密码至少 8 位。
# 首次登录后请立刻在网页里改密码，并把下面两行删掉/注释掉。
ADMIN_USERNAME=admin
ADMIN_PASSWORD=改成你自己的强密码

# 会话签名密钥（可选）。留空时应用会自动生成并保存到 /data/secret.key。
# 如果你要固定它，填一个足够长的随机串。
SECRET_KEY=
```

> ⚠️ **关于 `.env` 的一个重要事实**：`.env` **不会被应用代码读取**。它只被 `docker compose` 用来做变量插值（把 `${PUID}` 这类占位替换掉）。`docker-compose.yml` 顶部显式写了 `env_file: - .env`，这才让 `.env` 里的 `PUID` / `PGID` / `ADMIN_USERNAME` / `ADMIN_PASSWORD` 真正进入容器。
> **所以：不要删掉 compose 里的 `env_file: - .env` 这一行**，删了你的 `.env` 就只是躺在磁盘上的一个普通文本文件。

> 💡 **`HOST_PATH_MAP` 只是"给人看的翻译表"，而且在推荐形态下通常恒等。**
> 网页上显示路径时会用到它；**填错、漏填都不会影响任务行为**。
>
> - ★ **默认留空即可**（`HOST_PATH_MAP={}`）：单挂载形态让容器路径 = 宿主路径（§2.1），源与目标都**不需要翻译**；
> - 随附的 `docker-compose.yml` / `.env.example` 给的是**显式恒等映射** `{"/vol1/1000":"/vol1/1000"}`；留空 `{}` 与应用默认（恒等）等价。只有迁移到**非恒等部署**（容器内仍是 `/downloads`、`/library`）时才需要覆盖它；
> - 键必须是**带前导 `/` 的绝对容器路径**（`"/vol1/1000/Download/qbittorrent"`）。默认值都带斜杠，照抄即可；
> - 只有**非恒等**部署（把宿主目录挂到别的容器路径上）才需要写映射；想加自己的映射就按同样格式追加，多写几条无害。

> 💡 关于 `PUID`/`PGID` 填多少、改完要 `chown` 什么，见 §10。

### 4.2.1 部署资产自检（可选，**不需要 docker**）

仓库自带一个**只读**自检脚本，用来在真正启动前先确认"容器化资产没被改坏"：

**【NAS 宿主】或任意有 `sh` 的机器（含容器化资产的仓库副本上）**

```bash
sh examples/validate-deployment-assets.sh [仓库根]
```

它会检查 **14 节**：三个 shell 脚本（`entrypoint.sh` / `healthcheck.sh` / `minimal-repro.sh`）的 `dash`/`bash` 语法与 Windows CR 残留；`healthcheck.sh` 的判据（用桩服务做行为测试：**没有服务在跑、或六项必需 checks 缺任何一项，都必须判 unhealthy**，防止假绿灯）；`docker compose config` 能否插值，以及渲染后的对外事实（镜像名/容器名/`restart`/端口、**恰好两条 volumes** —— 媒体单一共同父挂载 `/vol1/1000` + `./data:/data`、environment 键值）；改 `PORT` 时宿主与容器端口同步；`docker/Dockerfile` 的关键断言；`.dockerignore` 规则（Dockerfile 的 COPY 源一条都不许被排除）；回调命令与 examples 的 API 口径；以及**第 14 节的真实硬链接探针**（需显式给出 `SRC_ROOT`/`TGT_ROOT`，缺任一个即 SKIP）。脚本只在 `mktemp` 临时目录里写东西，不碰你的数据。

> 脚本运行后会打印 PASS / FAIL / SKIP 计数，常见 2～3 项 SKIP（可选工具未安装；`docker build --check` 要 `MLM_BUILD_CHECK=1` 才跑；第 14 节未显式给 `SRC_ROOT`/`TGT_ROOT`）。
> 判据是 **[FAIL] 计数为 0**，不是某个固定的 PASS 数 —— PASS 条数会随版本增加。
> ⚠️ **验收声明**：文档不代替验收 —— "容器真正构建成功、并且 `docker inspect` 显示 `healthy`"属于**部署验收步骤**（§4.3 / §4.4），不是"文档说已验证通过"。请按 §4.3 拉到 `healthy` 之后再算部署完成。

### 4.3 启动

**【NAS 宿主】**

```bash
cd /vol1/1000/Docker/media-ingest
docker compose up -d --build
```

第一次会构建镜像，可能需要几分钟（取决于 NAS 性能和网速）。

**看启动日志：**

**【NAS 宿主】**

```bash
docker compose logs -f --tail=100
```

关注这几件事：

- 有没有报错（尤其 `Permission denied`、`No such file or directory`）；
- 有没有打印**配置文件已初始化 / 已同步**（首次启动会写出 `/data/config/libraries.yaml` 与 `processors/*.yaml`）；
- 最后应看到 uvicorn 在监听 `0.0.0.0:18081`。

按 `Ctrl+C` 只是退出日志查看，**不会**停掉容器。

**确认容器在跑、且健康检查通过：**

**【NAS 宿主】**

```bash
docker compose ps
docker inspect media-ingest --format '{{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
```

`health=healthy` 是期望值。刚启动的 30 秒内可能是 `starting`，等一下再看。

> ℹ️ **`healthy` 现在是真的"业务健康"**：`docker/healthcheck.sh` 会解析 `/api/system/health` 的响应 JSON，**`status == "ok"` 且 `checks` **六个**键全为 `true`** 才判 `healthy`。所以出现 `(unhealthy)` 通常意味着某个 `checks` 为 `false`（数据卷不可写、内置处理器未加载、配置目录不可写、下载/库卷不可读不可写）—— 排查步骤见 §4.4.1。注意这个接口本身**永远返回 HTTP 200**（不健康是用 `status: "degraded"` 表达的），所以**不要**用 `curl -o /dev/null -w '%{http_code}'` 之类只看状态码的方式判断健康。

### 4.4 验证服务可访问（不用登录）

**【NAS 宿主】**

```bash
curl -s http://127.0.0.1:18081/api/system/health
```

期望看到 `"status":"ok"`。如果某项是 `false`，对照 [`docs/contracts/API.md`](../contracts/API.md) §5.5 的表格排查。

也可以【容器内】直接跑健康检查脚本（**它解析响应 JSON，只有 `status == "ok"` 且 `checks` **六个**键全为 `true` 才退出码 0**；否则把 `[失败]` / `[缺失]` 的项逐条写到 stderr。退出码：`0` = 健康、`1` = `degraded` 或响应不是合法 JSON、`2` = 连不上 / 超时 / HTTP 非 200。判据与排查方法见 §4.4.1）：

```bash
docker exec media-ingest /usr/local/bin/mlm-healthcheck
```

> 镜像里**没有 `curl`**（基础镜像是 `python:3.11-slim`）。要在容器里看完整响应，用镜像自带的 python：
>
> ```bash
> docker exec media-ingest python3 -c "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:18081/api/system/health')),ensure_ascii=False,indent=2))"
> ```

**首次启动、未登录时**：`/api/system/health` 是**唯一免认证**的接口，其余接口未登录一律 `401`：

| 请求 | 预期结果 |
|---|---|
| `GET /api/system/health` | `200`，`{"status":"ok",...}`（没有 `curl` 也能测，见下） |
| `GET /api/auth/me` | `200`，`{"authenticated":false,"needs_setup":true,"username":null,"created_at":null}` —— **未登录也**不返回 401，前端靠它决定显示「创建管理员」还是「去登录」 |
| `GET /api/libraries`、`GET /api/system/info` | `401`（未登录） |
| `GET /` | `200`，返回 `web/index.html`（`<!DOCTYPE html><html lang="zh-CN" data-theme="system">`） |

### 4.4.1 容器显示 `unhealthy` 时怎么定位

`docker compose ps` 的 STATUS 列出现 `(unhealthy)` 时，**不要重装、也不要删数据** —— 它一定是 `/api/system/health` 里某个 `checks` 为 `false`，或者出现了 `warnings`。三步定位：

**第 1 步：看 Docker 记录的失败输出**（【NAS 宿主】）

```bash
docker inspect --format '{{json .State.Health}}' media-ingest | python3 -m json.tool
# NAS 上没有 python3 就用：
docker inspect --format '{{json .State.Health}}' media-ingest
```

读三个字段：`Status`（`healthy` / `unhealthy` / `starting`）、`FailingStreak`（连续失败次数）、`Log[-1].ExitCode` 与 `Log[-1].Output`（脚本自己打印的失败原因）。启动后 `--start-period=20s` 内是 `starting`，属正常。

**第 2 步：看接口怎么说**（【NAS 宿主】）

```bash
curl -s http://127.0.0.1:18081/api/system/health | python3 -m json.tool
```

关注 `status`、`checks`、`details`（每项一句中文）、`warnings`。

**第 3 步：对照下表修根因**

| `checks` 键 | `false` 的含义 | 怎么办 |
|---|---|---|
| `db` | 打不开 `/data` 下的 SQLite | 数据卷没挂上 / 目录属主不对 → §10.3、§7.3 |
| `processors` | 三个内置处理器没有全部加载 | 处理器数据或配置文件损坏 → 看 `GET /api/processors` 与 `GET /api/config` 的 `errors`；必要时删掉 `/data/config/processors/` 下对应文件并重启（会按内置默认值重建） |
| `config` | `/data/config` 不可写 | `chown -R 1000:1001 ./data`（§10.3） |
| `data_dir` | `/data` 不可写 | `chown -R 1000:1001 ./data`（§10.3） |
| `downloads_readable` | `allowed_source_roots` 里第一个目录读不到 | `PUID`/`PGID` 是否与宿主属主一致 → §10.3 |
| `library_writable` | `allowed_target_roots` 里第一个目录不可写 | 同上；注意别对媒体库整棵树 `chown -R`（会连带改到硬链接共享的下载源文件） |
| （`warnings` 非空） | 通常是文件系统编码不是 UTF-8 | 按 `details` 的提示设 `LANG=C.UTF-8` / `PYTHONUTF8=1`，见 §4.2 |

> **`checks` 共六个键**：`db`、`data_dir`、`downloads_readable`、`library_writable`、`processors`、`config`。
> 不会有 `scripts` 这一项。

> **两条判据的区别要记清楚**（这也是为什么不能只看 `docker compose ps`）：
> - `GET /api/system/health` **永远返回 HTTP 200**，业务健康与否看响应体里的 `status`（`ok` / `degraded`）与 `checks`；
> - `healthcheck.sh` 解析响应体：**`status == "ok"` 且 `checks` 六个键全为 `true`** 才退出码 0 ⇒ `degraded` 的容器会真的显示 `(unhealthy)`，并在 stderr 打印 `[失败] checks.xxx = false` / `[缺失] checks.xxx 未出现在响应里`；
> - 所以任何时候都以 `/api/system/health` 的 `status` 与 `checks` 为准；`docker compose ps` 只是"快速一瞥"。
>
> （`healthcheck.sh` 只用镜像自带的 `python3` 标准库；`python:3.11-slim` 里**没有** `curl`/`wget`，所以脚本不依赖它们。）

**`healthy` 的容器长这样**（拿它对照你自己的输出；`checks` 共 6 项）。

`docker compose ps`：

```
media-ingest  media-ingest:latest  "/usr/local/bin/…"  Up 45 seconds (healthy)  0.0.0.0:18081->18081/tcp, [::]:18081->18081/tcp
```

`docker inspect --format '{{json .State.Health}}' media-ingest`（`Log[]` 展开后）：

```json
{
  "Status": "healthy",
  "FailingStreak": 0,
  "Log": [
    {
      "Start": "2026-10-13T00:37:50.06054199+08:00",
      "End": "2026-10-13T00:37:50.466367482+08:00",
      "ExitCode": 0,
      "Output": "healthcheck: status=ok，checks 全部通过（db, data_dir, downloads_readable, library_writable, processors, config）\n"
    }
  ]
}
```

`curl -s http://127.0.0.1:18081/api/system/health`：

```json
{"status":"ok","checks":{"db":true,"data_dir":true,"downloads_readable":true,"library_writable":true,"processors":true,"config":true},"details":{"db":"数据库可读：/data/mlm.db","processors":"内置处理器已加载：3/3。","config":"/data/config 可写。","data_dir":"/data 可写。","downloads_readable":"/vol1/1000/Download/qbittorrent 可读。","library_writable":"/vol1/1000/Library 可写。"},"warnings":[],"version":"1.0.0","time":"2026-10-13 00:38:30"}
```

> ✅ **容器里没有 `curl`/`wget` 也不影响健康检查**：`python:3.11-slim` 本来就不带它们，`healthcheck.sh` 用的是镜像自带的 `python3` 标准库。
> ✅ **`degraded` 会真的显示 `(unhealthy)`**：本接口**永远返回 200**，所以脚本不看状态码，而是解析响应体 ——
> `ExitCode` 变 `1`，`Output` 里是 `[失败] checks.xxx = false` / `[缺失] checks.xxx 未出现在响应里` 这类逐条原因。

### 4.5 浏览器打开并创建管理员

**在你自己电脑的浏览器里**打开：

```
http://192.168.1.100:18081
```

（把 IP 换成你的 NAS IP。）

- 如果 `.env` 里填了 `ADMIN_USERNAME` / `ADMIN_PASSWORD` → 直接用这个账号登录。
- 如果没有填 → 页面会引导你**创建管理员**：输入用户名 + 密码（**至少 8 位**）。

登录后建议立刻做的事：

1. **「设置」页 → 修改密码**（如果用了 `.env` 初始化），然后把 `.env` 里的 `ADMIN_PASSWORD` 删掉；
2. **「设置」页**把 `hook_token` 和完整的 qBittorrent 回调命令复制出来（下一节要用）；
3. 新建媒体库前，先看一眼 [`docs/guides/USER_GUIDE.md`](USER_GUIDE.md)。

---

## 5. 接入 qBittorrent（下载完成自动触发）

### 5.1 先拿到回调命令与 token

1. 浏览器打开 `http://<NAS_IP>:18081` 并登录；
2. 进 **设置**页，找到 **回调 / Hook** 一节；
3. 页面上有 **hook token** 和一个**完整可复制的回调命令**，点「复制」按钮；
4. 这条命令就是下面要填进 qBittorrent 的东西。

token 也可以从接口拿：

**【NAS 宿主】**

```bash
curl -s -b cookies.txt http://192.168.1.100:18081/api/settings
# 响应里的 "hook_token" 字段就是它
```

> ⚠️ `hook_token` 相当于这个应用的"后门钥匙"（拿到它就能触发任务）。**不要**贴到公开论坛、截图或聊天群里。怀疑泄露时，在设置页重新生成即可。

> 📌 **`hook_token` 不是环境变量**：`.env` 里**没有**这个键，也不需要填 —— 填了不会被任何代码读取。它由应用**首次启动时随机生成**，存在 `/data` 里的数据库（`settings` 表），设置页/`GET /api/settings` 读到的就是当前值。**唯一的轮换入口是设置页的「重新生成 Token」**（等价的接口写法：`PUT /api/settings` 带 `{"hook_token": ""}` → 重新随机生成；带具体值 → 改成该值；带 `"***"` 或不带 → 保持不变）。

> ⚠️ **重新生成 token 之后必须立刻回到 qBittorrent**：旧 token 立即失效，qBittorrent 里那条已配置的命令会开始返回 `401 回调 token 不正确`（而且你会以为"应用坏了"）。正确顺序是：**设置页重新生成 → 复制新命令 → 粘回 qBittorrent「运行外部程序」覆盖旧的 → 手动模拟一次回调确认 200**。

### 5.2 在 qBittorrent 里填写

打开 qBittorrent WebUI（`http://192.168.1.100:18080`）→ **设置（齿轮图标）→ 下载 → 运行外部程序**，把下面这条**完整**粘进去（`<HOOK_TOKEN>` 换成你的 token）：

```
curl -s -X POST "http://192.168.1.100:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" --data-urlencode "name=%N" --data-urlencode "hash=%I" --data-urlencode "category=%L" --data-urlencode "tags=%G" --data-urlencode "content_path=%F" --data-urlencode "save_path=%D" --data-urlencode "root_path=%R"
```

**关于这条命令，几个必须理解的细节：**

| 细节 | 说明 |
|---|---|
| 必须是**一行** | qBittorrent 的输入框里不要换行、不要加多余空格 |
| `%N` `%I` `%L` `%G` `%F` `%D` `%R` | 是 qBittorrent 的占位符，会在执行时被替换成种子名称、哈希、分类、标签、内容路径、保存路径、根路径。**照抄，不要改** |
| **不要加 `kind=%K`** | qBittorrent 的 `%K` 官方语义是 **Torrent ID（种子内部数字 ID）**，**不是**任务类型；把它当 `kind` 传虽然会被白名单安全忽略，但语义是错的。任务类型由媒体库的处理器推导（`bangumi`/`movie` → `rename_hardlink`，其余 → `hardlink`）。`kind` 只是手工传 `hardlink`/`rename`/`rename_hardlink` 时的**可选覆盖项**，标准命令里一律不传（[`docs/contracts/ARCHITECTURE.md`](../contracts/ARCHITECTURE.md) §8 是唯一真源）。多传的其它占位符（`%Z`、`%S` 等）会被静默忽略 |
| `--data-urlencode` | 因为种子名/路径里可能含空格和中文，必须用 `--data-urlencode`，不能写成 `-d "name=%N"` |
| `%F`（content_path）为什么能直接用 | 因为 qBittorrent 容器和本应用容器把同一个宿主目录都挂在 `/vol1/1000/Download/qbittorrent`（见 §2.1）。**这是本方案能零转换的前提** |
| `curl` 从哪来 | qBittorrent 容器内自带 `curl`（linuxserver/qbittorrent 镜像已确认存在） |
| `-s` | 静默模式，避免在 qBittorrent 日志里刷一堆进度条 |

**别忘了**：qBittorrent 的「运行外部程序」只在**下载完成时**触发。所以还要去 **设置 → 下载 → 保存管理** 或相关位置确认「运行外部程序」的开关是启用的（不同 qBittorrent 版本叫法略有差异，通常在同一个「下载」页面里）。

### 5.3 验证回调真的生效

**（1）手动模拟一次回调（最可靠的验证方式）**

**【NAS 宿主】**（把 token 换成你的，把 `category` / `content_path` 换成你库里匹配得上的值）

```bash
curl -s -X POST "http://192.168.1.100:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" \
  --data-urlencode "name=测试种子" \
  --data-urlencode "hash=0000000000000000000000000000000000000000" \
  --data-urlencode "category=ban" \
  --data-urlencode "tags=" \
  --data-urlencode "content_path=/vol1/1000/Download/qbittorrent/Bangumi/Show A" \
  --data-urlencode "save_path=/vol1/1000/Download/qbittorrent/Bangumi" \
  --data-urlencode "root_path=/vol1/1000/Download/qbittorrent" \
  --data-urlencode "kind=completed"
```

| 返回 | 含义 | 下一步 |
|---|---|---|
| `{"matched":1,"tasks":[43],"ignored":[]}` | ✅ 成功命中并入队 | 去任务页看任务 |
| `{"matched":0,"tasks":[],"ignored":["番剧"]}` | 回调通了，但**没命中任何库** | 检查库的匹配条件：分类 `ban` 有没有写进 `match_categories`、`content_path` 是否以某个 `match_path_prefixes` 开头 |
| `{"detail":"回调 token 不正确。请在「设置」页复制完整的回调命令（含 token=...），或重新生成 token 后更新 qBittorrent 的「运行外部程序」。"}`（401） | token 错了或没带上 | 重新从设置页复制**完整回调命令**，注意别把引号或空格带进去 |
| 连接被拒绝 | 本应用没在跑，或端口不对 | `docker compose ps` 看容器状态 |

> ⚠️ 手动模拟只验证"**回调链路**通不通"，不代表下载完成时 qBittorrent 会真的调用。它是否真调用，取决于 §5.2 里的配置和 qBittorrent 的开关。

**（2）从 qBittorrent 容器内部测（验证网络可达性）**

**【NAS 宿主】**（借 qBittorrent 容器里的 curl 去请求本应用，完全等价于 qBittorrent 自己发起的调用）

```bash
docker exec qbittorrent curl -s -X POST "http://192.168.1.100:18081/api/system/health"
```

有 JSON 输出 = qBittorrent 容器到本应用的网络是通的。

**（3）真跑一个种子看日志**

在 qBittorrent 里下一个真实种子（或用分类 `ban` 的小种子），完成后：

**【NAS 宿主】**

```bash
docker compose logs --tail=100 media-ingest
```

应能看到类似：

```
INFO  qbittorrent 回调：name=... category=ban content_path=/vol1/1000/Download/qbittorrent/Bangumi/...
INFO  命中媒体库「番剧」，任务 #43 已入队
```

同时网页的**任务页**会出现一条新任务。

### 5.4 回调漏了怎么办（扫描补偿）

回调不一定 100% 可靠（qBittorrent 重启、curl 超时、容器没起来…）。本应用提供了两条补偿路径：

**（a）目录扫描补偿（推荐）**

定期扫描所有已启用库的 `source_dir`，发现「没记录过、或体积变了」的一级文件夹就自动建任务。在**网页 → 设置**里打开：

- `scan_enabled` = `true`
- `scan_interval_seconds` = 扫描周期（默认 `900` 秒 = 15 分钟）

**【NAS 宿主】**也可以用接口改：

```bash
curl -s -b cookies.txt -X PUT http://192.168.1.100:18081/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"scan_enabled":"true","scan_interval_seconds":"900"}'
```

**（b）手动/外部触发一次扫描**

**【NAS 宿主】**

```bash
curl -s -X POST "http://192.168.1.100:18081/api/hooks/scan?token=<HOOK_TOKEN>"
```

这个接口**不看** `scan_enabled` 开关，手动调用总是执行一次扫描。

> 扫描**只创建任务**，不做任何文件操作 —— 不会因为扫描本身动到你的任何文件。

### 5.5 （可选）填写 qBittorrent API 连接信息

「设置」页还有 `qbittorrent_url` / `qbittorrent_username` / `qbittorrent_password` 三项，用于 API 补偿（向 qBittorrent 查询种子列表）。应用**不靠它工作**，回调与扫描都不依赖它。填了就填，不填也不影响主流程。

---

## 6. 升级与回滚

### 6.1 核心概念：数据在宿主上，容器是可丢弃的

因为 `/data` 是宿主目录的 bind mount（§2.4），所以**升级不会碰你的数据**：账号、处理器与媒体库规则、任务历史、配置文件（`data/config/`）、日志与已处理记录（`data/logs/`）全都在 `/vol1/1000/Docker/media-ingest/data` 里原地不动。容器删了重建，数据还在。

### 6.2 升级步骤

**（1）先备份（永远先备份）**

见 §7。

**（2）替换代码并重建**

**【NAS 宿主】**

```bash
cd /vol1/1000/Docker/media-ingest
docker compose down                 # 停并删容器；data/ 不受影响
# 用新版本文件覆盖项目目录，但**保留 .env 和 data/ 不要动**
docker compose up -d --build
docker compose logs -f --tail=100
```

> ⚠️ **覆盖代码时千万不要删 `data/` 和 `.env`**。有版本管理（git）的话，`git pull` 是最安全的方式：它只会更新跟踪的文件，`data/` 与 `.env` 都不在跟踪范围内。

**（3）验证**

**【NAS 宿主】**

```bash
curl -s http://127.0.0.1:18081/api/system/health
docker inspect media-ingest --format '{{.State.Health.Status}}'
```

> **升级时多看一眼**：首次启动会自动补建每库账本表 `link_records`（新装库由 `SCHEMA` 带出，老库由 `migrate()` 的 `_ensure_link_records_table()` 补建，两处同一份 DDL）—— **不需要手工建表，也不用改 `data/` 里的任何东西**。但账本**不从旧的 `hard_links.log` 回填**，所以老媒体库的**第一次全量运行会把每个作品重新建链**，已在目标目录里改过名的文件会以原名再链一份、可能多出 ` (2)` 后缀（**不丢数据**）。建议升级后用**新媒体库**跑一次全量验证；原因、影响与三种规避办法见 [`docs/contracts/COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) §3.5 与 [`docs/meta/KNOWN_ISSUES.md`](../meta/KNOWN_ISSUES.md) KI-7。

### 6.3 回滚

回滚**就是把旧代码/旧镜像换回去**：

**【NAS 宿主】**

```bash
cd /vol1/1000/Docker/media-ingest
docker compose down
# 把项目文件换回旧版本（git 的话：git checkout <旧版本标签>），或用旧镜像
docker compose up -d --build
```

**关于数据**：`data/` 目录在回滚时**原地不动**，所以回滚本身不会丢数据。但有一条要自己判断：

> ⚠️ 如果新版本对数据库做过结构变更，回滚到旧版本时旧版本可能不认识新的库结构。所以**升级前先备份 `data/`**（§7）—— 这样万一需要，可以把 `data/` 也一起恢复到升级前的样子。升级前请先看 `CHANGELOG.md` 有没有提到数据结构变更。

### 6.4 只升级不重建的写法（用预构建镜像）

如果你的 compose 里用的是 `image:` 而不是本地构建：

**【NAS 宿主】**

```bash
cd /vol1/1000/Docker/media-ingest
docker compose pull
docker compose up -d
```

`data/` 依然不受影响。

### 6.5 升级后必做：确认路径白名单

> 这一节是**升级后 30 秒的确认步骤**。如果你是从更早的安装升上来的，请逐条看一遍。
> 现行形态是：容器里只有
> `${MEDIA_ROOT:-/vol1/1000}:/vol1/1000`（**rw**）+ `./data:/data` 两条挂载（见 §2.1）。

| 情况 | 行为 |
|---|---|
| 设置值**恰好逐字等于**更早版本的默认单值（源 `["/downloads"]`、目标 `["/library"]`） | **自动改写**为 `[DEFAULT_SOURCE_ROOT]` / `[DEFAULT_TARGET_ROOT]`（即宿主真实路径），只做一次、只改这一个精确值 |
| 你自己改过（任何别的值，包括多元素列表） | **一律不动** —— 迁移刻意不猜你的意图 |

如果什么都不做，而两个 roots 又停在 `/downloads` / `/library`（单挂载形态之后容器里**根本不存在**这两个路径），
你会看到：

- `GET /api/system/health` 的 `downloads_readable` / `library_writable` 变成 **`false`** ⇒ 容器**开箱 unhealthy**（其余项都正常，很容易误判成权限问题）。

**升级后请这样确认（30 秒）**：

**【NAS 宿主】**

```bash
curl -s http://127.0.0.1:18081/api/system/health | python3 -m json.tool   # downloads_readable / library_writable 应为 true
curl -s http://127.0.0.1:18081/api/settings | python3 -m json.tool        # 看 allowed_source_roots / allowed_target_roots
```

- 两个 roots 显示为**宿主真实路径**（如 `/vol1/1000/Download/qbittorrent`、`/vol1/1000/Library`）⇒ 迁移成功，正常用；
- 仍显示 `/downloads` / `/library`（说明你**曾自定义过**，迁移按约定跳过了）⇒ 到 **WebUI 设置页改成宿主真实路径**；
- ⚠️ `DEFAULT_SOURCE_ROOT` / `DEFAULT_TARGET_ROOT` 这两个 env **只影响首次播种**，对**已存在的 DB 不生效** —— 改它们解决不了升级后的死角，必须用上面的方式确认/手改。

**（2）升级步骤**

1. **先备份 `data/`**（§7）—— 升级会写数据库结构与配置文件；
2. 用新的 `docker-compose.yml` / `.env.example` 替换旧的，**volumes 必须是两条 bind**：
   `- ${MEDIA_ROOT:-/vol1/1000}:/vol1/1000` + `- ./data:/data`
   （不要有 `/downloads`、`/library` 这类额外挂载，见 §2.1），
   `.env` 里的 `HOST_PATH_MAP` 可以留空（默认恒等）；
3. `cd /vol1/1000/Docker/media-ingest && docker compose up -d --build`；
4. 首次启动会**自动**：写入三个内置处理器 → 写出
   `/data/config/libraries.yaml` 与 `processors/*.yaml` → 并按上面的规则**检查/迁移两个白名单**；
5. **验证**：

   ```bash
   curl -s http://127.0.0.1:18081/api/system/health | python3 -m json.tool     # 六项 checks 全 true（含 downloads_readable / library_writable）
   curl -s http://127.0.0.1:18081/api/processors | python3 -m json.tool         # 三个内置处理器
   curl -s http://127.0.0.1:18081/api/config | python3 -m json.tool             # errors 为空
   curl -s http://127.0.0.1:18081/api/settings | python3 -m json.tool           # 两个 roots 已是宿主真实路径
   ```

6. **`/data/logs/` 不需要动**：磁盘上已有的日志**原样继续使用**
   （硬链接日志的现有格式继续被识别）。

**（3）升级后行为上的两处变化**（看一眼再决定要不要调）

- **重命名改由规则引擎执行**。默认（`default` 预设）在 `legacy` 之外**多了拒绝目录规则与 9 条集数规则** ——
  想要最朴素的旧口径，到处理器页把 `bangumi` / `movie` 「恢复默认」→ 选 `legacy`；
- **硬链接改由应用自己的原生实现执行**（`app/processors/hardlink.py`）。可观察输出（stdout 与日志文件的
  逐字格式）记录在 `docs/contracts/BASELINE.md` §1，由 `tests/test_hardlink_native.py` 的冻结黄金值守护 ——
  镜像搬到别的设备也不会缺依赖。

**（4）回滚注意**

回滚到旧版本要把整个 `data/` 一起恢复（§7.3）、并把 `docker-compose.yml` / `.env` 换成旧版本那一套。
新版本写出的 `/data/config/` 对旧版本是无害的额外文件，可以留着。

---

## 7. 备份与恢复

### 7.1 必须备份哪些文件

| 必须备份 | 路径（宿主） | 丢了会怎样 |
|---|---|---|
| ✅ **数据库** | `data/mlm.db` | 账号、**所有处理器与媒体库规则**、任务历史、设置全丢，要重配 |
| ✅ **数据库 WAL 伴生文件** | `data/mlm.db-wal`、`data/mlm.db-shm` | 如果数据库当前是 WAL 模式且有未合并的改动，只备份 `mlm.db` 可能丢最近的写入 |
| ✅ **配置文件** | `data/config/`（`libraries.yaml` + `processors/*.yaml`） | 规则在数据库里也有一份，所以**不一定致命**；但如果你手改过 YAML（注释、自定义处理器、手调的正则），那些改动**只有这里有** → 见 §7.5 |
| ✅ **会话密钥** | `data/secret.key` | 所有已登录会话失效（要重新登录）。**不影响**文件与规则 |
| ✅ **日志目录** | `data/logs/` | 幂等判定的依据（硬链接日志 + **每个媒体库一条**重命名日志）丢了 → 已处理过的文件夹会被当作"未处理"重跑一次（产生一批无害的 `File exists` 失败，**不损坏文件**）。所以强烈建议备份 |

**不需要备份**：项目代码本身（重新拷贝/拉取即可）、`.env`（建议手抄一份 PUID/PGID/TZ/ADMIN 设置，但它含明文口令，别和备份放一起）、Docker 镜像。

### 7.2 备份命令

**推荐做法：先停容器再打包**，这样数据库没有正在写入的事务，备份一定一致。

**【NAS 宿主】**

```bash
cd /vol1/1000/Docker/media-ingest

# 1) 停容器（数据不会丢）
docker compose stop

# 2) 打包整个 data 目录（含 mlm.db、secret.key、config/、logs/ 和 WAL 伴生文件）
tar -czf /vol1/1000/backup/mlm-data-$(date +%Y%m%d-%H%M%S).tar.gz -C /vol1/1000/Docker/media-ingest data

# 3) 重新启动
docker compose start
```

如果 `/vol1/1000/backup` 不存在，先建：

```bash
mkdir -p /vol1/1000/backup
```

**不停容器也能备份（但要带上 WAL 文件）**：

**【NAS 宿主】**

```bash
docker exec media-ingest sh -c 'cd /data && tar -czf - mlm.db mlm.db-wal mlm.db-shm secret.key config logs' \
  > /vol1/1000/backup/mlm-data-live-$(date +%Y%m%d-%H%M%S).tar.gz
```

> 这是通过容器内的 tar 打包，避免宿主上文件权限不够读不到。缺点：打包瞬间数据库可能正在被写入。**要稳妥就停容器。**

**建议**：把这条命令加进 NAS 的定时任务（cron / 计划任务），每天做一次，保留最近 7~14 份。

### 7.3 恢复命令

**【NAS 宿主】**

```bash
cd /vol1/1000/Docker/media-ingest

# 1) 停容器
docker compose stop

# 2) 把当前 data 目录改名保留（万一要后悔），不要直接删
mv data data.before-restore

# 3) 建空目录并解包备份
mkdir -p data
tar -xzf /vol1/1000/backup/mlm-data-20261009-012011.tar.gz -C .

# 4) 启动
docker compose start
```

### 7.4 恢复后需要做什么

1. **修属主**（如果你是解包成 root 属主的，容器里的 `app` 用户可能读写不了）—— 见 §10.3：

   **【NAS 宿主】**

   ```bash
   chown -R 1000:1001 /vol1/1000/Docker/media-ingest/data
   ```

2. **检查容器健康**：

   **【NAS 宿主】**

   ```bash
   docker compose ps
   curl -s http://127.0.0.1:18081/api/system/health
   ```

3. **重新登录网页**。如果 `secret.key` 没有被正确恢复（或者你换了 `SECRET_KEY`），所有旧会话失效 —— 用原来的用户名密码重新登录即可，**账号还在数据库里**。

4. **重新核对设置页**：`hook_token` 是否和 qBittorrent 里填的一致（备份恢复后应该一致；如果恢复的是旧备份、之后又重生成过 token，就要在 qBittorrent 里更新回调命令）。

5. **看一次任务历史**：确认规则、历史都在。

6. **确认后删掉 `data.before-restore`**（确认没问题再删，别急着删）：

   **【NAS 宿主】**

   ```bash
   rm -rf /vol1/1000/Docker/media-ingest/data.before-restore
   ```

> ⚠️ 第 6 步是**不可逆删除**。执行前请确认新数据一切正常，或者把 `data.before-restore` 移到别处而不是删掉。

### 7.5 只备份 / 只恢复配置目录（`/data/config`）

**为什么值得单独做**：`data/config/` 是你手改过的重命名规则的**可读快照** —— 数据库里那份是二进制形态，
人要读、要备份给别的机器、要贴给别人看，靠的就是这些 YAML。整份 `data/` 备份当然包含它，
但"只搬配置"是很常见的需求。

**只备份配置：**

```bash
# 【NAS 宿主】
cd /vol1/1000/Docker/media-ingest
tar -czf /vol1/1000/backup/mlm-config-$(date +%Y%m%d-%H%M%S).tar.gz -C data config
```

**只恢复配置**（容器可以不停）：

```bash
# 1) 先解到临时目录看一眼内容，别直接盖
mkdir -p /tmp/mlm-config
tar -xzf /vol1/1000/backup/mlm-config-20261020-101500.tar.gz -C /tmp/mlm-config
ls -R /tmp/mlm-config/config | head -30

# 2) 放进数据卷，并修属主（容器里跑的是 uid 1000，见 §10）
cp -a /tmp/mlm-config/config/. /vol1/1000/Docker/media-ingest/data/config/
chown -R 1000:1001 /vol1/1000/Docker/media-ingest/data/config

# 3) 让应用从文件导入（也可以什么都不做，等默认 5 秒的自动同步）
curl -s -X POST -b /tmp/mlm-cookies.txt http://127.0.0.1:18081/api/config/reload
```

> ⚠️ **恢复配置会覆盖数据库里的规则**（导入方向是"文件胜出"）。如果你只是想撤销某一次误改，
> 更稳的做法是先在网页上 `POST /api/config/export`（把当前 DB 写出去）留一份，再覆盖。
> 只想把某一个处理器恢复出厂设置：处理器页「恢复默认」，或 `POST /api/processors/{id}/reset`。
>
> `reload` / `export` 都是**需要登录**的接口（用浏览器的 Cookie 或先 `curl -c cookies.txt` 登录，见
> [`docs/contracts/API.md`](../contracts/API.md) §8）。

---

## 8. 端口与反向代理

### 8.1 换端口

默认用 `18081`。要换成别的（比如 `18082`）：

**（1）改 `.env`**

```bash
PORT=18082
```

**（2）改 `docker-compose.yml` 的端口映射**（左边是宿主端口，右边是容器内端口）

```yaml
ports:
  - "18082:18082"
```

⚠️ 两边都要改，而且必须一致：右边对应容器里 uvicorn 监听的端口（由 `PORT` 决定），左边才是你在浏览器里访问的端口。

**（3）重建**

**【NAS 宿主】**

```bash
cd /vol1/1000/Docker/media-ingest
docker compose up -d
```

> 改了端口，qBittorrent 里的回调命令也要同步改（URL 里的端口）。

### 8.2 反向代理（可选）

想让应用走 `https://mlm.你的域名`，或者和别的服务共用 `80/443`，可以在前面的 Nginx 上加一段。

**下面的配置对 SSE（`/api/tasks/stream`）是必须的** —— 少了 `proxy_buffering off`，实时日志会被 Nginx 缓冲住，页面看着像"卡住了"。

**【NAS 宿主】**（写在 Nginx 的 `server { ... }` 块里）

```nginx
location / {
    proxy_pass http://192.168.1.100:18081;

    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # SSE / WebSocket 必需：关闭缓冲，让事件逐条实时下发
    proxy_buffering off;
    proxy_cache off;
    proxy_set_header Connection "";
    proxy_read_timeout 3600s;
    chunked_transfer_encoding on;
}
```

要点：

- `proxy_buffering off;` + `proxy_cache off;` —— **SSE 的关键**，不加会"看着没反应"；
- `proxy_read_timeout 3600s;` —— SSE 是长连接，别让 Nginx 一分钟就掐断；
- 用 HTTPS 时，会话 Cookie 是 `SameSite=Lax`，同站点访问不受影响；跨站嵌入（iframe）场景才需要额外考虑；
- 反向代理不需要额外配 `X-Forwarded-*` 才能工作，但配上方便排查；
- ⚠️ **不要把本应用直接暴露到公网**。它是给局域网用的管理工具，只有一层口令认证。要外网访问请走 VPN（WireGuard/Tailscale）或至少加一层 HTTPS + 访问控制。

---

## 9. x86_64 与 ARM64

### 9.1 成本评估：结论是"几乎零成本"

| 因素 | 情况 |
|---|---|
| 基础镜像 | `python:3.11-slim`，Docker 官方镜像，**同时提供 `linux/amd64` 与 `linux/arm64`** |
| Python 依赖 | 只有 `fastapi` 与 `uvicorn[standard]`，**纯 Python**，没有需要编译的 C 扩展 |
| 前端 | **免构建**的原生 ES modules + CSS —— 不需要 node/npm，不需要构建阶段 |
| **镜像体积（参考值）** | 约 **180MB**（单平台构建出的 `media-ingest:latest` 大小；随基础镜像与依赖版本变化，以 `docker image ls` 为准） |
| 结论 | **ARM64 无需交叉编译任何东西**，只要 Buildx 换平台参数，或者直接在 ARM NAS 上 `docker compose build` |

也就是说：ARM64 支持不是"另做一套"，只是"换个平台标签"。

> 📌 本文示例与验收针对 **x86_64**（`arch=amd64`）。
> **如果你只是在一台 x86_64 机器上装，请直接用 §4 的 `docker compose up -d --build`，不要在 ARM64 机器上构建**（单平台构建出来的镜像只能在那台机器/那个架构上跑）。

### 9.2 方式 A：在 ARM NAS 上本机构建（最简单）

【NAS 宿主】（ARM NAS 上执行）

```bash
cd /vol1/1000/Docker/media-ingest
docker compose up -d --build
```

就这么简单 —— 官方基础镜像会自动拉取 arm64 版本，`pip install` 会自动装 arm64 的 wheel（纯 Python 包通常就是同一个包）。

### 9.3 方式 B：用 buildx 交叉构建并推送到镜像仓库

在**任意一台装了 Docker + Buildx 的电脑**上构建多架构镜像，推到你自己的镜像仓库，然后在 NAS 上 `pull`。

**【任意电脑】**（先确认 buildx 可用）

```bash
docker buildx version
docker buildx create --use --name media-ingest-builder
docker buildx inspect --bootstrap
```

**（1）同时构建 amd64 + arm64 并推送**（多平台必须 `--push`，不能 `--load`）

```bash
cd /path/to/媒体入库整理

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -f docker/Dockerfile \
  -t <你的仓库地址>/media-ingest:latest \
  --push \
  .
```

**（2）只构建某一种架构**（这时可以 `--load` 到本地）

```bash
# 只构建 arm64
docker buildx build --platform linux/arm64 -f docker/Dockerfile -t media-ingest:latest --load .

# 只构建 amd64
docker buildx build --platform linux/amd64 -f docker/Dockerfile -t media-ingest:latest --load .
```

**（3）在 NAS 上改用这个镜像**

把 `docker-compose.yml` 里的 `build:` 段去掉（或者保留但注释掉），只留 `image:`：

```yaml
services:
  media-ingest:
    image: <你的仓库地址>/media-ingest:latest
    # build:
    #   context: .
    #   dockerfile: docker/Dockerfile
```

然后：

**【NAS 宿主】**

```bash
cd /vol1/1000/Docker/media-ingest
docker compose pull
docker compose up -d
```

> 💡 别把私人镜像仓库的账号密码写进 `docker-compose.yml`。用 `docker login` 让 Docker 把凭据存在自己的配置里。

---

## 10. 非 root 运行与 PUID / PGID

### 10.1 为什么要关心这个

容器**默认以 root 运行的话**，它创建出来的文件和目录就是 root 属主。后果：

- 下载目录里的新文件，你的普通 NAS 账号可能删不掉/改不了；
- 媒体库里的硬链接文件夹，Emby 或你自己可能没权限读写；
- 反过来，如果容器以错误的 uid 运行，**它自己**也可能读不到你的文件（`Permission denied`，任务直接失败）。

所以本项目**刻意不以 root 执行业务逻辑**：容器入口脚本（entrypoint）以 root 启动只是为了**修权限**，修完立刻降权到 `app` 用户再启动应用。

### 10.2 PUID / PGID 是什么

它们**不是**应用特有的概念，而是"让容器里的进程假装成你 NAS 上的某个用户"：

| 变量 | 含义 | 本项目参考值 |
|---|---|---|
| `PUID` | 容器内应用用户要使用的 **User ID** | `1000`（示例；按 §10.1 用 `id` 查出你自己的） |
| `PGID` | 容器内应用用户要使用的 **Group ID** | `1001`（= 宿主 `Users` 组） |

容器启动流程（概念上）：

1. entrypoint 以 root 启动；
2. 按 `PUID`/`PGID` 改写容器内 `app` 用户的 uid/gid；
3. `chown` 数据目录与代码目录；
4. 用 `gosu` 降权到 `app` 用户；
5. 以 `app` 身份启动 `uvicorn`。

> `PUID` / `PGID` 的**含义**和 linuxserver 系列镜像一致，但本项目的实现是自带的 `docker/entrypoint.sh`。具体行为以仓库里的 `docker/entrypoint.sh` 为准。

> ✅ **核对方式**：读容器里的 `/proc/1/status`，其中 `Uid:` / `Gid:` 应当是你配置的 `PUID` / `PGID`（示例 `1000` / `1001`），即业务逻辑**不以 root 在跑**。
>
> ⚠️ **但 `docker exec` 进去默认是 root shell**：镜像的 `Config.User` 是空的，降权是 entrypoint 内部用 `gosu` 完成的，所以 exec 新起的进程不受影响。要以**应用身份**操作（验证权限、手建测试文件等），显式指定用户：
>
> ```bash
> docker exec -u 1000:1001 -it media-ingest sh
> ```

### 10.3 怎么查、怎么改

**（1）在 NAS 上查出正确的 uid/gid**

**【NAS 宿主】**

```bash
id youruser
```

输出形如：

```
uid=1000(youruser) gid=1001(Users) groups=1001(Users),...
```

取 `uid=` 后面的数字填 `PUID`，`gid=` 后面的数字填 `PGID`。

**（2）改 `.env`**

```bash
PUID=1000
PGID=1001
```

**（3）重建容器让 entrypoint 重新修权限**

**【NAS 宿主】**

```bash
cd /vol1/1000/Docker/media-ingest
docker compose down
docker compose up -d --build
```

**（4）改完需要 `chown` 什么**

| 路径 | 谁负责 | 说明 |
|---|---|---|
| `/data`（= 宿主 `./data`） | **entrypoint 自动处理** | 每次启动都会按 PUID/PGID 修属主。你也可以手动兜底：`chown -R 1000:1001 /vol1/1000/Docker/media-ingest/data` |
| `/srv/mlm`（容器内应用目录） | **entrypoint 自动处理** | 无需你关心 |
| `/vol1/1000/Library`（= 宿主 `/vol1/1000/Library`） | ⚠️ **需要你自己处理** | 这是宿主目录，entrypoint 不会（也不该）去 `chown` 你的媒体库整棵树。要让新的 `PUID` **可写**：`chown 1000:1001 /vol1/1000/Library` 或放宽权限（参考 §10.4） |
| `/vol1/1000/Download/qbittorrent`（= 宿主 `/vol1/1000/Download/qbittorrent`） | 只需**可读** | 应用**代码层从不写这里**，源目录也**不做 `:ro` 挂载**（内核要求同挂载才能建硬链接），改由代码层保证 + 任务前后逐文件源完整性核对接管（见 README 不变量 3）。确保 `PUID` 能读进去即可：`sudo -u '#1000' test -r /vol1/1000/Download/qbittorrent` |

**（5）改完怎么验证**

**【NAS 宿主】**

```bash
# 容器里应用进程的实际身份
docker exec media-ingest id

# 数据目录属主
ls -ld /vol1/1000/Docker/media-ingest/data

# 目标目录对新身份可写？
sudo -u '#1000' test -w /vol1/1000/Library && echo OK || echo '不可写，需要 chown/chmod'
```

### 10.4 权限调不过去怎么办

如果 `/vol1/1000/Library` 的属主是别人、你又不想改，可以放宽它的权限位（媒体库目录通常不需要严格保护）：

**【NAS 宿主】**

```bash
# 只对目录本身放宽（让新 uid 能进去并创建子目录）
chmod 775 /vol1/1000/Library
```

媒体库里的**内容**（已有的硬链接文件）因为继承的是源 inode 的权限（见 [`docs/contracts/BASELINE.md`](../contracts/BASELINE.md) §1.5），一般不需要额外处理。

> ⚠️ **不要**用 `chown -R` 递归改 `/vol1/1000/Library` 整棵树去"试试看" —— 那会同时改掉已经建好的硬链接的属主（因为硬链接共享 inode，改它等于改**下载源文件**的属性！）。
> 这正是应用**默认不执行 `chmod`** 的原因：媒体库里的文件与下载原文件**共享同一个 inode**，`chmod` 媒体库等于同时改掉源文件的权限位，那就违反了"绝不修改 qBittorrent 原始下载文件"。**当前有一个显式开关**（`permission_mode`，**默认关**）：只有在界面上开启并二次确认后，应用才会把**目标侧**（库根 + 本次处理到的作品子树，含被跳过的旧作品）改成 `0777`，代价就是源文件权限位一起变（见 [`docs/contracts/BASELINE.md`](../contracts/BASELINE.md) §1.6 与 §4 的 **B13**、[`docs/contracts/API.md`](../contracts/API.md) §3.2）。**优先**在宿主上针对**目录**做权限调整（本节），不要递归 `chmod`/`chown` 整棵媒体库树。

### 10.5 常见权限报错的对照

| 报错 | 原因 | 处理 |
|---|---|---|
| `Permission denied: '/data/config'` | 配置目录属主不对（少见） | `chown -R <PUID>:<PGID> ./data` 后重启（§10.3） |
| `Permission denied: '/vol1/1000/Download/qbittorrent'` | `PUID` 读不了下载目录 | 见 §10.3 第（4）步 |
| `Permission denied: '/vol1/1000/Library/...'` | `PUID` 写不了媒体库 | 见 §10.4 |
| `Permission denied: '/data/mlm.db'` | `data/` 属主不对 | `chown -R <PUID>:<PGID> ./data` 后重建 |
| 任务失败但日志写"跨文件系统错误" | 不是权限，是文件系统 | 见 §3 |

---

## 11. 常用运维命令速查

全部在**【NAS 宿主】**执行（`cd /vol1/1000/Docker/media-ingest` 之后）：

```bash
# 启动 / 停止 / 重启
docker compose up -d
docker compose stop
docker compose start
docker compose restart

# 看状态（含健康检查）
docker compose ps
docker inspect media-ingest --format '{{.State.Status}} {{.State.Health.Status}}'

# 看日志
docker compose logs -f --tail=200

# 进容器看看（⚠️ 默认是 root shell；要以应用身份操作就加 -u 1000:1001）
docker exec -it media-ingest sh
docker exec -u 1000:1001 -it media-ingest sh

# 看应用进程（PID 1）的真实身份：应为 Uid 1000 / Gid 1001
docker exec media-ingest sh -c 'grep -E "^(Uid|Gid):" /proc/1/status'

# 健康检查
curl -s http://127.0.0.1:18081/api/system/health

# 看卷挂载是否与预期一致
docker inspect media-ingest --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{if .RW}}rw{{else}}ro{{end}}){{println}}{{end}}'

# 看容器内源与目标的设备号（判断能否建硬链接）
docker exec media-ingest stat -c '%d %n' /vol1/1000/Download/qbittorrent /vol1/1000/Library

# 磁盘占用
docker system df
du -sh /vol1/1000/Docker/media-ingest/data
```

**看应用自己的日志文件（容器内路径 `/data/logs/`）：**

**【NAS 宿主】**

```bash
ls -lh /vol1/1000/Docker/media-ingest/data/logs/
tail -n 100 /vol1/1000/Docker/media-ingest/data/logs/hard_links.log
```

---

## 下一步看哪篇

- 网页上每个按钮/字段是什么意思 → [`docs/guides/USER_GUIDE.md`](USER_GUIDE.md)
- 我要用 curl / 脚本调接口 → [`docs/contracts/API.md`](../contracts/API.md)
- 「为什么没触发」「为什么任务失败」这类问题 → [`docs/guides/FAQ.md`](FAQ.md)
- 具体报错怎么修 → [`docs/guides/TROUBLESHOOTING.md`](TROUBLESHOOTING.md)
- 兼容规则为什么是这样（不要改的边界在哪） → [`docs/contracts/BASELINE.md`](../contracts/BASELINE.md)、[`docs/contracts/COMPATIBILITY.md`](../contracts/COMPATIBILITY.md)
