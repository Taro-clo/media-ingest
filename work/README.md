# 媒体入库整理（media-ingest）

为 **qBittorrent 已完成的下载**创建**硬链接**到指定媒体库路径，并按**可配置的处理器规则**重命名；
提供一个中文优先、跟随系统深色模式的现代 WebUI 来管理处理器规则、媒体库、任务队列、执行历史与日志。

> **容器自包含。** 硬链接由 `app/processors/hardlink.py` 完成，
> 重命名由配置驱动的规则引擎 `app/processors/engine.py` 完成，二者是**唯一**的执行体，
> 行为由项目自己的回归测试（冻结的黄金值）守护。
> 重命名规则在 WebUI 的
> 「处理器」页可视化编辑（并可按库覆盖），与 `/data/config/processors/*.yaml` **双向同步**。
> 契约唯一真源见 [`docs/contracts/PROCESSORS.md`](docs/contracts/PROCESSORS.md)，行为契约见 [`docs/contracts/BASELINE.md`](docs/contracts/BASELINE.md)。

---

## 目录

- [1. 它解决什么问题](#1-它解决什么问题)
- [2. 核心不变量](#2-核心不变量)
- [3. 架构](#3-架构)
- [4. 安装部署](#4-安装部署)
- [5. qBittorrent 配置（含完整回调命令）](#5-qbittorrent-配置含完整回调命令)
- [6. 卷挂载与「源和目标必须在同一个挂载」](#6-卷挂载与源和目标必须在同一个挂载)
- [7. 行为稳定性](#7-行为稳定性)
- [8. 重命名规则怎么改（处理器）](#8-重命名规则怎么改处理器)
- [9. 最小可复现实例](#9-最小可复现实例)
- [10. 常见故障排查](#10-常见故障排查)
- [11. 升级与备份](#11-升级与备份)
- [12. 开发与测试](#12-开发与测试)
- [13. ARM64 支持](#13-arm64-支持)
- [14. 目录结构](#14-目录结构)

---

## 1. 它解决什么问题

下载完成后，媒体文件通常还要再做两件事：**建硬链接**（下载目录里的文件保持原样继续做种，
媒体库目录里出现同一份内容）和**按规则重命名**（整理成播放器、刮削器认识的名字）。

如果这两件事靠人工、靠零散的一次性脚本去做，就绕不开三个问题：

1. 没人盯着就会忘；跑没跑、跑成功没有、失败在哪一步都不知道；
2. 想多加一个媒体库、或想把某一类作品的命名规则调一调，就得改代码；
3. 重复跑容易把处理过的目录再处理一遍，或者干脆复制文件、白白多占一份空间。

本项目把它变成一条自动流水线：**qBittorrent 下载完成 → 自动回调 → 排队 → 建硬链接 → 按处理器规则重命名 → WebUI 里看结果**。

重命名规则是**一条条可编辑的配置**（命名段、接受/拒绝目录正则、后缀白名单）：
在 WebUI 里改，或直接改 `/data/config/processors/*.yaml` 都行，两边 5 秒内自动同步。
**新增一类媒体库不需要写 Python** —— 复制一个处理器、改规则、建库时选它即可（见[第 8 节](#8-重命名规则怎么改处理器)）。

硬链接同样由应用自己实现，宿主上**不需要安装任何东西**：

① 所有逻辑都在容器内，宿主只要有 Docker；
② 每个媒体库可以**单独**设定重命名方式 —— calibre 风格的一行模板（`[{season}-{episode}] {title}{suffix}`）

+ 只给这个库用的接受/拒绝正则 + 只给这个库用的后缀白名单（见[第 8 节](#8-重命名规则怎么改处理器)）；
  ③ 后缀白名单另有一个**全局**开关（设置页），对所有媒体库生效。

## 2. 核心不变量

这四条是硬约束，代码与测试都围绕它们建立：

| #   | 不变量                                                    | 保障方式                                                                                                                                                                                                                                                                                                                                                                        |
| --- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | 硬链接的**路径规则与目录层级**稳定不变                    | 硬链接阶段由 **`app/processors/hardlink.py`** 完成（打印文案与日志格式固定），并由 `tests/test_hardlink_native.py` 的 A 组用**冻结的黄金值**（全新树 / 已记录 / 体积变化 / 源目录消失 / 日志损坏五种场景）回归守护；`docs/contracts/BASELINE.md` 记录相应契约（B2/B3）                                                                                                          |
| 2   | 重命名**默认规则**下的命名结果稳定不变                    | 内置处理器的默认规则是引擎的固定默认值；由项目自己的回归测试 `tests/test_processor_engine.py`、`tests/test_bangumi_episode_rules.py`、`tests/test_rename_extensions.py` 用**冻结的黄金值**守护（文件树、`Renamed:` 行集合、日志条目语义），`legacy` 预设同样必须通过                                                                                                            |
| 3   | **任何时候不移动 / 删除 / 修改 qBittorrent 原始下载文件** | ① 代码层**从不写源目录**；② **任务前后逐文件源完整性核对**（快照 `相对路径/size/ino/mtime_ns`，有差异即写 `Task.result.source_integrity` 与中文告警并列出路径）；③ `tests/test_no_source_mutation.py` 守住这条。**诚实说明**：单挂载形态下，源目录**不能再单独 `:ro` 挂载**（内核要求源与目标同挂载才能建硬链接，二者互斥），所以这一条是「**可验证的保证**」，而不是结构性保证 |
| 4   | 跨文件系统 / 跨挂载时**绝不静默复制文件冒充硬链接**       | 执行前用**实证 link 探针**（在目标目录建一个探测链接再删）判「能不能真建」；`st_dev` / mount ID 只作**预筛**（`st_dev` 相等**不能**预测 `link()` 成功）；执行后逐文件核对 `(st_dev, st_ino)`；失败则明确报错                                                                                                                                                                    |

## 3. 架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  qBittorrent 容器 (network_mode: host, WebUI :18080)                  │
│    [AutoRun] 下载完成 → curl POST → :18081/api/hooks/qbittorrent     │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  name/hash/category/tags/content_path/%F ...
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  media-ingest 容器 (:18081, 非 root, uid 1000)               │
│                                                                       │
│   FastAPI ──► 规则匹配（分类 / 标签 / 路径前缀）──► 任务队列（单工作线程串行）│
│      │                                                   │            │
│      │ WebUI (免构建原生 ES modules)                      ▼            │
│      │   · #/processors  处理器规则编辑器        executor.execute_task │
│      │   · #/libraries   媒体库                                        │
│      │                                     ┌─────────────┴──────────┐ │
│      │                                     ▼                        ▼ │
│      │                            硬链接阶段（原生实现）      重命名阶段（规则引擎）│
│      │                        app/processors/hardlink.py  app/processors/engine.py│
│      │                        （扫描与编排，              （配置驱动，WebUI 可改，│
│      │                          规则不因库而异）             可按库覆盖）          │
│      │                                     │                        │ │
│      └── SQLite (WAL) /data/mlm.db ◄───────┴────────────────────────┘ │
│          /data/logs/    每库一条重命名日志 + hard_links.log             │
│          /data/config/  processors/*.yaml + libraries.yaml（双向同步）  │
└──────────────────────────────────────────────────────────────────────┘
        │                        │
        ▼                        ▼
 同一个挂载里的两条子树（源只读使用 / 目标可写）：
 /vol1/1000/Download/qbittorrent                /vol1/1000/Library
 （同挂载是硬链接成立的前提，见第 6 节；源**不再**单独 :ro 挂载）
```

**技术栈**（刻意精简，降低 NAS 上的构建风险）：

- 后端：Python 3.11 + FastAPI + Uvicorn，依赖只有 `fastapi` 与 `uvicorn[standard]`，其余全是标准库
- 规则引擎：配置驱动（**YAML**，新增依赖只有 `PyYAML==6.0.2`），正则用 stdlib `re`；命名表达式用受限 `eval`（可用名字见 [`docs/contracts/PROCESSORS.md`](docs/contracts/PROCESSORS.md) §3.1）
- 存储：SQLite（WAL 模式），单文件，落在 `/data`
- 鉴权：`hashlib.scrypt` 口令哈希 + stdlib `hmac`/`secrets` 签名 Cookie 会话
- 前端：**免构建**的原生 ES modules + CSS（无 npm、无打包器、无 CDN）

**并发策略**：qBittorrent 可能同时完成多个种子，因此回调只做**入队**；
实际执行由**单工作线程串行消费**，从根本上避免两个任务同时改同一个目录。
两个阶段各自幂等：**硬链接阶段**按一级文件夹体积去重（日志
`/data/logs/hard_links.log`），**重命名阶段**每个媒体库有自己的一条缓存日志
（`/data/logs/<log_file>`）。所以重复触发不会造成重复劳动。

## 4. 安装部署

### 4.1 前置条件

- NAS 上有 Docker 与 Docker Compose v2（本项目在 Docker 28.5.2 / Compose v2.40.3 上验证）
- 下载目录与媒体库目录位于**同一个挂载**（比「同一个文件系统」更严格：Linux 的 `linkat` 比较**挂载身份**，不同挂载一律 `EXDEV`；见[第 6 节](#6-卷挂载与源和目标必须在同一个挂载)）

### 4.2 部署目录

部署就三步，照着做完即可。

**第一步：确认两个目录。** 应用要处理两个目录（一般都已经有了）：

- **下载目录**：qBittorrent 存种子的地方，例如 `/vol1/1000/Download/qbittorrent`；
- **媒体库目录**：整理后的硬链接放在这里，例如 `/vol1/1000/Library`。

两者必须在**同一个挂载**（通常就是同一块盘、同一个卷）之下，否则硬链接建不出来（原理见第 6 节）。
媒体库目录还没有就先建：

```bash
mkdir -p /vol1/1000/Library
```

**第二步：建一个部署目录，把两个文件放进去。**（路径随便挑，下面只是例子）

```bash
mkdir -p /vol1/1000/Docker/media-ingest
cd /vol1/1000/Docker/media-ingest
cp /path/to/repo/docker-compose.yml .      # 仓库里的这两个文件
cp /path/to/repo/.env.example .env
```

| 文件                 | 作用                                  | 要不要改                         |
| -------------------- | ------------------------------------- | -------------------------------- |
| `docker-compose.yml` | 容器定义：挂载、端口、数据卷          | 一般不用改                       |
| `.env`               | 你这台机器的配置：uid/gid、端口、目录 | **要改**，见 4.3；别提交、别外传 |

**第三步：知道数据存在哪。** 数据库、日志、配置副本默认落在
`${MLM_DATA_DIR:-/vol1/1000/Docker/media-ingest/data}`，也就是部署目录下的 `data/`。
**备份、迁移、换机器都只需要打包这一个目录**（见第 11 节）。

### 4.3 配置 `.env`

`.env` **不会被应用代码读取**：它只被 `docker compose` 用来填充 `docker-compose.yml` 里的
`${VAR}` 插值，容器内的真实环境变量来自 compose 的 `environment:` 段。`.env.example` 里每一项
都有注释，至少要确认下面这几项：

```dotenv
PUID=1000                                       # 运行容器的用户 uid（用 `id -u` 查）
PGID=1001                                       # 运行容器的用户组 gid（用 `id -g` 查）
TZ=Asia/Shanghai                                # 时区，影响日志时间戳
PORT=18081                                      # WebUI 端口，换一个没被占用的即可
CONFIG_DIR=/data/config                         # 处理器与媒体库配置文件所在目录
# ⚠️ 同步周期**不是环境变量**：真源是数据库设置项 `config_sync_interval_seconds`
# （默认 5 秒，区间 0–86400，在设置页或 PUT /api/settings 里改）。
MEDIA_ROOT=/vol1/1000                            # 单一共同父挂载（compose 的 volumes 用它）—— 源与目标必须同在它下面，见第 6 节
# 下面两个只影响**首次播种**（空 DB 时写进默认设置）；库已存在时不生效。旧默认单值 /downloads /library 会被自动迁移，见 docs/guides/DEPLOYMENT.md 的部署迁移说明
DEFAULT_SOURCE_ROOT=/vol1/1000/Download
DEFAULT_TARGET_ROOT=/vol1/1000/Library
HOST_PATH_MAP='{}'                               # 容器路径 = 宿主路径 ⇒ 通常恒等（留空即可）；仅"非恒等部署"才需要填
SECRET_KEY=                                     # 留空则首次启动生成 /data/secret.key（0600）
ADMIN_USERNAME=                                 # 两个都留空 → 首次打开 WebUI 时页面引导创建管理员（推荐）
ADMIN_PASSWORD=
```

> `HOST_PATH_MAP` **只影响显示**：它把容器路径翻译成宿主路径给页面看（例如 `/vol1/1000/Download/qbittorrent/Bangumi`
> 在非恒等部署下会被翻译）。容器路径 = 宿主路径，所以它**通常恒等**、留空即可，**填错或漏填不影响功能**。
> 随附的 `docker-compose.yml` / `.env.example` 给的是**显式恒等映射** `{"/vol1/1000":"/vol1/1000"}`；留空 `{}` 与应用默认（恒等）等价。
> 只有迁移到**非恒等部署**（容器内仍是 `/downloads`、`/library`）时才需要覆盖它。

> 用 `id` 确认 PUID/PGID：
>
> ```bash
> id          # 形如 uid=1000(you) gid=1000(you) groups=...
> ```
>
> 容器进程的 uid 必须能**读**下载目录并**写**媒体库目录。注意源目录**不能**以 `:ro` 挂载（单挂载形态下做不到），
> 改由「代码层不写源目录 + 任务前后逐文件源完整性核对」保证，见[第 2 节](#2-核心不变量)第 3 条。

> ⚠️ **`HOOK_TOKEN` 不是环境变量，不要往 `.env` 里加。**
> 回调 token 由应用首次启动时随机生成（`secrets.token_urlsafe(32)`）并保存在数据卷的 SQLite 里，
> 登录后在**设置页**一键复制。想换一个：设置页可重新生成，或 `PUT /api/settings {"hook_token": ""}`。
> 把它写进 `.env` 不会有任何效果，回调会因为 token 不匹配而返回 401。

### 4.4 启动

```bash
cd /vol1/1000/Docker/media-ingest
docker compose up -d --build
```

首次构建会拉取 `python:3.11-slim` 并安装两个依赖，所以这一步需要能访问 Docker 镜像仓库与 PyPI。
网络不通时，先在有网的机器上 `docker compose build`，再用 `docker save` / `docker load` 把镜像搬过去。

### 4.5 首次登录

浏览器打开：

```
http://<你的主机IP>:18081          # 就在这台机器上访问时用 http://127.0.0.1:18081
```

首次访问会跳到「创建管理员」页面 —— 设置用户名与密码后自动登录。
密码用 scrypt 加盐哈希后存入 `/data/mlm.db`，**明文不落盘**。

若希望**免交互**初始化，可在 `.env` 里设置 `ADMIN_USERNAME` / `ADMIN_PASSWORD`，
首次启动时自动创建管理员（创建后这两个变量即失效，不会覆盖已改过的密码）。

### 4.6 确认服务健康

```bash
docker compose ps
curl -s http://127.0.0.1:18081/api/system/health
```

`/api/system/health` **不需要认证**，便于健康检查脚本与容器 `HEALTHCHECK` 使用。

## 5. qBittorrent 配置（含完整回调命令）

### 5.1 为什么用「运行外部程序」

qBittorrent 的 **Run external program on torrent completion** 是最简单可靠的触发方式：
不依赖轮询、不依赖 API 认证、实时性好。本项目采用**单一共同父挂载、且容器路径 = 宿主路径**，
所以回调里的 `%F`（qBittorrent 交给宿主/容器看到的真实路径）**零转换**即可直接使用，不需要任何 `HOST_PATH_MAP` 翻译。

### 5.2 填写步骤

1. 打开 qBittorrent WebUI：`http://<你的主机IP>:18080`
2. 进入 **工具 → 选项 → 下载 → 运行外部程序**
3. 勾选 **「种子完成时运行外部程序」**
4. 把下面这条命令整行粘进去（把 `<HOOK_TOKEN>` 换成**设置页里「复制回调命令」按钮给出的那条**——token 不在 `.env` 里，见 §4.3）

### 5.3 完整回调命令

```bash
curl -s -X POST "http://<你的主机IP>:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" --data-urlencode "name=%N" --data-urlencode "hash=%I" --data-urlencode "category=%L" --data-urlencode "tags=%G" --data-urlencode "content_path=%F" --data-urlencode "save_path=%D" --data-urlencode "root_path=%R"
```

> ⚠️ **必须写成一行。** qBittorrent 会把整行交给 shell 执行，换行会截断命令。

**变量含义与用途**：

| 占位符 | 含义             | 本项目的用途                                                                                                        |
| ------ | ---------------- | ------------------------------------------------------------------------------------------------------------------- |
| `%N`   | 种子名称         | 任务标题、执行历史、日志                                                                                            |
| `%I`   | 种子哈希         | 记录在任务的 `payload.torrent_hash` 里，便于回溯（**不做去重**：重复回调会各建一个任务，但执行本身是幂等的，见 §2） |
| `%L`   | 分类             | **规则匹配**（`match_categories`）                                                                                  |
| `%G`   | 标签（逗号分隔） | **规则匹配**（`match_tags`）                                                                                        |
| `%F`   | 内容路径         | **定位要处理的目录**                                                                                                |
| `%D`   | 保存路径         | 规则匹配（`match_path_prefixes`）、路径校验                                                                         |
| `%R`   | 根路径           | 兜底定位                                                                                                            |

> `%F`、`%D`、`%R` 在 qBittorrent **5.x** 中可用。若你的版本较老不支持某个占位符，
> 删掉对应的 `--data-urlencode` 段即可 —— 除 `content_path` 外全部是可选的。
>
> **不要用 `%K` 当任务类型。** qBittorrent 里 `%K` 的官方语义是 **Torrent ID**（内部数字 ID），
> 不是"文件类型"。本应用的任务类型由媒体库选择的处理器推导（Bangumi/Movie → 重命名+硬链接，
> 其余 → 只建硬链接）；`kind` 只是一个**可选的高级覆盖项**（手工传 `hardlink` / `rename` /
> `rename_hardlink`），标准命令里不需要传。多传的占位符会被安全忽略。

### 5.4 验证回调是否通

在宿主机（或任何能访问该服务的机器）上模拟一次真实的回调：

```bash
curl -s -X POST "http://127.0.0.1:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" \
  --data-urlencode "name=测试种子" \
  --data-urlencode "hash=0123456789abcdef0123456789abcdef01234567" \
  --data-urlencode "category=Bangumi" \
  --data-urlencode "content_path=/vol1/1000/Download/qbittorrent/Bangumi/某番剧"
```

响应形如：

```json
{
  "matched": 1,
  "tasks": [12],
  "ignored": ["电影库"],
  "ignored_details": [{"library_id": 2, "name": "电影库", "reason": "分类 Bangumi 不在该库的分类列表里。"}],
  "warnings": [],
  "torrent": {"name": "测试种子", "hash": "0123…"}
}
```

`matched` 是**命中的媒体库数量**、`tasks` 是对应的任务 id 列表。命中 0 个库不是错误，
但要看 `ignored_details[].reason` —— 它用中文逐库说明"为什么没命中"（条件全空、分类不符、
没带标签、路径前缀不符…），这是排查回调配置的主要依据。

### 5.5 可选的补偿机制

回调是**主路径**，但下面的情况会让回调漏掉：qBittorrent 重启、容器重启、手动拷进去的文件。
因此在 **设置 → 扫描** 里可以打开**目录扫描**（默认关闭），按固定周期（默认 900 秒）
扫描下载目录，把有变化但未处理的一级文件夹补进队列。

也可以在 **设置 → qBittorrent** 里填 WebUI 地址与账号，用 API 侧查询作二次确认。

## 6. 卷挂载与「源和目标必须在同一个挂载」

### 6.1 硬链接的本质约束

**硬链接有两条硬要求：① 同一个文件系统；② 在 Linux 上还必须是同一个「挂载」。**
这不是本项目的限制，是内核的定义：硬链接就是「同一个 inode 的另一个目录项」，而 inode 编号只在单个文件系统内唯一；
更关键的是 `linkat(2)` 会先比较**挂载身份**（内核 `do_linkat()` 里的 `old_path.mnt != new_path.mnt`）——
**不同挂载一律 `EXDEV`，与 `st_dev`、`fsid`、btrfs 子卷都无关**。所以只要下载目录与媒体库目录被分别 bind 成两个挂载，
硬链接就**永远建不出来**，哪怕它们在宿主上明明在同一个盘里。

跨挂载 / 跨文件系统调用 `os.link()` 都会抛：

```
OSError: [Errno 18] Invalid cross-device link
```

> **为什么在宿主上直接 `ln` 能成功？** 因为那两个目录在宿主上同在 `/vol1` 这**一个挂载**里。
> 这正是本项目只挂**一个共同父目录**的原因（见第 6.3 节），而不是按目录各挂一条。
> 业界同生态的做法一致：*arr（Sonarr / Radarr）的文档明确要求下载目录与媒体库位于**同一个挂载点**下
> （否则「硬链接」只能退化成复制或链接失败）。

先确认这两个目录在同一个文件系统、同一个挂载上（下面的 `st_dev` 数字只是示例，两边**一致**即可）：

```bash
stat -c "%d %n" /vol1/1000/Download/qbittorrent /vol1/1000/Library
# 55 /vol1/1000/Download/qbittorrent    ← 示例输出
# 55 /vol1/1000/Library                 ← 与上一行相同
```

> `st_dev` 相同只是**必要条件**：真正决定成败的是「是否同一个挂载」。容器里可以直接看挂载点：

```bash
findmnt -T /vol1/1000/Download/qbittorrent
findmnt -T /vol1/1000/Library    # TARGET 应为同一个挂载点
```

> 同一个卷（分区 / LVM 逻辑卷）下的目录天然同盘。
> **但如果你的 NAS 把下载盘和媒体库盘分成了两个卷（例如 `/vol1` 与 `/vol2`），硬链接就不可能成立。**

### 6.2 本项目的处理方式

**绝不静默降级为复制。** 判据与动作分三层：

1. **预检（`?probe=true`）**：主判据是**实证 link 探针** —— 从源根**有界递归**（深度 ≤3、条目 ≤512、不跟随符号链接）找一个已存在的普通文件，
   在目标根下建一个临时硬链接再删掉（**不写源目录**）。这是唯一能预测「真跑能不能成」的办法。
   `st_dev`（以及 `/proc/self/mountinfo` 的 mount ID）只是**预筛**：mount ID 相同是**必要不充分**，
   而 `st_dev` 相等**根本不能**预测 `link()` 成功 —— 只看 `st_dev` 的预检正是因为这一点**假绿**（报了「可以创建硬链接」，真跑全失败）。
   结果三态：`true`（已实证可建）/ `false`（已实证跨挂载）/ `null`（无法实证，例如源目录下没有可链的普通文件）。
2. **运行前**：判定为跨挂载时**只警告、不硬拦** —— 告警写进 `Task.result.warnings[]`（中文、指名修法），任务照跑。
3. **执行后核对**：对每个产出的文件比对源与目标的 `(st_dev, st_ino)`（真硬链接必须**同 inode**）；
   同时对**源目录做任务前后的逐文件完整性核对**（第 2 节第 3 条不变量），发现源被动过就告警并列出路径。

失败原因会**同时**出现在：WebUI 任务详情的错误提示、`/data/logs/` 的应用日志、容器 `docker logs`。

错误信息形如：

```
跨挂载：源 /vol1/1000/Download/qbittorrent/Bangumi/某番剧 与目标 /vol1/1000/Library/Bangumi/某番剧 不在同一个挂载，
无法创建硬链接（os.link 会返回 EXDEV）。本应用不会回退为复制文件，
请把下载目录与媒体库目录放进同一个 bind 挂载（见 docker-compose.yml 的 MEDIA_ROOT）。
```

### 6.3 卷挂载表

**只有两条 bind mount**（也**不把源与目标分别挂开**）：

| 宿主路径                              | 容器路径                           | 权限 | 说明                                                                                                                                                                                                           |
| ------------------------------------- | ---------------------------------- | ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `${MEDIA_ROOT:-/vol1/1000}`           | `/vol1/1000`（**与宿主完全一致**） | `rw` | **单一共同父挂载**：下载目录与媒体库目录都在它下面，源与目标因此落在**同一个挂载**内 —— 这是硬链接能成立的前提。容器路径 = 宿主路径之后，容器里看到的路径就是您熟悉的宿主路径，日志与 WebUI 显示的都是真实路径 |
| `/vol1/1000/Docker/media-ingest/data` | `/data`                            | `rw` | SQLite、任务日志、每库重命名日志、`/data/config` 配置文件、`secret.key`                                                                                                                                        |

> ⚠️ **不要**把源与目标再拆成两条 bind（例如 `/vol1/1000/Download/qbittorrent:/vol1/1000/Download/qbittorrent:ro` + `/vol1/1000/Library:/vol1/1000/Library`）。
> 那样它们会变成**两个挂载**，`link()` 一律 `EXDEV`，**硬链接全部失败**（任务报 `link_failed`，绝不静默复制）。

### 6.4 媒体库路径的写入白名单

媒体库的「源目录 / 硬链接目标根路径」不能任意填。应用的默认白名单是：

- 允许的源根：`/vol1/1000/Download/qbittorrent`
- 允许的目标根：`/vol1/1000/Library`

（这两个默认值可由 `DEFAULT_SOURCE_ROOT` / `DEFAULT_TARGET_ROOT` 环境变量覆盖，但它们**只影响首次播种**，对已存在的 DB 不生效。
**旧默认值的自动迁移**：库里若存的还是旧默认单值 `/downloads`、`/library`，而容器里**不存在**这两个路径，
启动时会做**有界一次性自动迁移**（仅当设置恰好逐字等于 `["/downloads"]` / `["/library"]` 时才改写成上面的宿主真实路径；您自定义过的一律不动）。
确认方式与手动补救见 [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md) 的部署迁移节。）

也就是说，**WebUI 里能选的路径必须是容器内实际挂载进来的路径**。这既防止误操作，
也保证「硬链接只能建在同一挂载」这个前提不会被 UI 绕过去。

> `HOST_PATH_MAP` 在这个「容器路径 = 宿主路径」的形态下**通常恒等**：随附的 `docker-compose.yml` / `.env.example` 给的是
> **显式恒等映射** `{"/vol1/1000":"/vol1/1000"}`；留空 `{}` 与应用默认（恒等）等价。只有迁移到**非恒等部署**
> （容器内仍是 `/downloads`、`/library`）时才需要覆盖它。它**只影响界面显示**，既不参与白名单校验，也不影响任务行为。
>
> `MEDIA_ROOT` 同样是**compose 侧**的变量：只有 `docker/entrypoint.sh` 读它（做两条只读结构检查：与 `DATA_DIR` 不同挂载等），
> **应用代码不读它** —— 应用判断路径合法性只看数据库设置里的「允许的源/目标根目录」。

### 6.5 「已处理」记录与日志（v2）

两个阶段的幂等性都建立在**日志文件**上，而不是扫描目标目录：

| 日志           | 位置                        | 作用                                                                                                                                                                                                                                                                                                                                                         |
| -------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 硬链接日志     | `/data/logs/hard_links.log` | 记录「哪个一级文件夹处理过、当时多大」。日志行格式为 `%(asctime)s - Source: … - Folder: … - Folder size: …`，其中的路径是**容器路径**（如 `/vol1/1000/Download/qbittorrent/Bangumi`）。**这个文件只写两类"记账行"**（体积登记 / 孤儿目录清理），应用日志不混入、历史行也不清理（见 [`docs/contracts/ARCHITECTURE.md`](docs/contracts/ARCHITECTURE.md) §6.5） |
| 每库重命名日志 | `/data/logs/<log_file>`     | **每个媒体库一条**，记录「这个作品文件夹重命名时多大、什么时候」。默认文件名是 `<slug>cache.log`（如 `bangumicache.log`），可在媒体库表单里改，但**只能是文件名**，不能带路径                                                                                                                                                                                |

既有的 `/data/logs/*.log` **原样保留、继续追加**，所以升级不会有断档。
新建媒体库时，若它的重命名日志还不存在，应用就建一个空的 `{}`。

> ⚠️ **一个仍然要注意的坑：`Source:` 与媒体库的源目录是「字符串完全相等」比较。**
> 建链阶段（现行执行体：`app/processors/hardlink.py`）
> 用 `source_dir_from_log == source_dir` 判断「这条记录属不属于本次要处理的源目录」。
> 所以日志里的 `Source:` 必须与数据库里存的源目录**逐字相同**（两边都是容器路径，如 `/vol1/1000/Download/qbittorrent/Bangumi`）。
> 如果你把某个媒体库的源目录从一个子目录换到另一个（例如 `/vol1/1000/Download/qbittorrent/Bangumi` → `/vol1/1000/Download/qbittorrent/Bangumi2`），
> 历史记录就对不上了，那一次会全量重扫并产生一批**无害**的 `File exists` 失败（这些"目标就是同一 inode 硬链接"的条目会被算成幂等跳过，不计失败）。
> 排查方法见[第 10.7 节](#107-满屏-file-exists--明明处理过却重扫)。

## 7. 行为稳定性

### 7.1 行为是怎么保证稳定的

分两条路：

- **硬链接阶段：原生实现 + 回归测试。** 运行期执行 `app/processors/hardlink.py`
  —— 同样的扫描顺序、同样的打印文案、同样的日志行格式，
  连"体积未变就跳过""孤儿清理不删日志行"这些细节也照旧。适配层覆盖模块级 `LOG_FILE`
  把日志指到 `/data/logs/`，并用 `ScopedOs` 代理 `os` 只扫描本次要处理的那一个文件夹。
  行为由 `tests/test_hardlink_native.py` 的 A 组用**冻结的黄金值**守护：全新树、已有日志记录、
  文件夹体积变化、源文件夹消失、日志损坏五种场景各断言一次（黄金值由差分测试冻结下来的）。
- **重命名阶段：规则引擎 + 回归测试。** 引擎读取处理器配置（见[第 8 节](#8-重命名规则怎么改处理器)）；
  内置处理器的默认规则是引擎的固定默认值
  （依据见 [`docs/contracts/PROCESSORS.md`](docs/contracts/PROCESSORS.md) §3.2；行为契约见 [`docs/contracts/BASELINE.md`](docs/contracts/BASELINE.md)）。
  `tests/test_processor_engine.py`、`tests/test_bangumi_episode_rules.py`、`tests/test_rename_extensions.py`
  用**冻结的黄金值**断言重命名后的文件树、`Renamed:` 行集合与日志条目语义（`legacy` 预设同样必须通过）。

### 7.2 行为基线文档

完整契约（数据流、正则原文、每个分支的判定条件、已知限制）见 [`docs/contracts/BASELINE.md`](docs/contracts/BASELINE.md)
（该文件记录行为契约），对照见 [`docs/contracts/COMPATIBILITY.md`](docs/contracts/COMPATIBILITY.md)。

### 7.3 行为对照速查

> 下面三张表写的是**现行行为**（执行体是 `app/processors/hardlink.py` 与规则引擎
> `app/processors/engine.py`）。
> 内置处理器在 **`default` 预设**下额外加了「拒绝目录」规则
> （番剧、电影各一份清单），这是**有意的行为增量**；想要**最朴素的旧口径**，
> 请在处理器页用 **`legacy` 预设**重置（见[第 8 节](#8-重命名规则怎么改处理器)、
> [`docs/contracts/PROCESSORS.md`](docs/contracts/PROCESSORS.md) §3.2）。

**硬链接（现行执行体：`app/processors/hardlink.py`）**

| 输入                           | 行为                                                                                       |
| ------------------------------ | ------------------------------------------------------------------------------------------ |
| 新的一级文件夹                 | 在目标根下重建**同名**目录层级，对每个文件 `os.link`                                       |
| 已处理且体积未变               | **跳过**（幂等）                                                                           |
| 已处理但体积变了（新增了一集） | 清掉旧的体积记录，**重新建链**                                                             |
| 源里已删、目标里还在           | 若开启 `orphan_cleanup`，**删掉目标侧的孤儿文件夹**（只动 `/vol1/1000/Library`，绝不碰源） |

**番剧重命名（现行执行体是规则引擎 `app/processors/engine.py`）**

```
<作品>/Season N/x 01.chs.ass   →   <作品> S01E01.chs.ass
<作品>/Season N/x 01.mkv       →   <作品> S01E01.mkv
```

- 识别 `Season N` 目录与 `EPISODE_PATTERN` / `ALEPISODE_PATTERN`
- `.ass` 走单集分支，其他扩展名走另一分支（现行由规则引擎执行）
- 已重命名过的（日志里有记录）再次运行不会重复处理

**电影重命名（现行执行体是规则引擎 `app/processors/engine.py`）**

```
<电影文件夹>/abc.mkv   →   <电影文件夹>/<电影文件夹名>.mkv
```

- 只看**第一层**，只认 `.mkv` / `.mp4` / `.ass`（**大小写敏感**）
- 同一扩展名有多个文件时，**保留体积最大的那个**，其余不动

### 7.4 已知限制

完整列表见 [`docs/contracts/BASELINE.md`](docs/contracts/BASELINE.md) §5，最需要注意的两条：

- **L9**：建链阶段（`app/processors/hardlink.py`）用「一级文件夹总字节数」作为**唯一**的处理标记。
  体积相同就跳过 —— 所以「换了文件但总大小恰好不变」不会被重新处理。这是**刻意保留**的既有行为。
  应用层的「已处理」判据另见 [`docs/contracts/BASELINE.md`](docs/contracts/BASELINE.md) 的 B14。
- **编码**：日志文件 `open(LOG_FILE)` 未指定编码，依赖 locale。容器内已锁定
  `LANG=C.UTF-8` / `PYTHONUTF8=1`，避免 `UnicodeEncodeError`。

## 8. 重命名规则怎么改（处理器）

### 8.1 处理器（processor）是什么

一个**处理器 = 一套重命名规则 + 一条日志**。它由下面几块组成，全部可以在 WebUI 里改：

| 区块                                                          | 管什么                                                                                                                                                                                                                                                                                                                | 例子                                                                                           |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| **命名表达式（`naming.expressions`）**                        | 具名表达式，按**书写顺序**求值，可引用上下文与前面算出的表达式（右值是 Python 表达式，见 PROCESSORS §3.1）                                                                                                                                                                                                            | `year: basename(works_dir)[:4]`                                                                |
| **命名段（`naming.segments`）**                               | 新文件名由哪些「段」按顺序拼出来：参数（`title` / `season` / `episode` / `ext` / `extname` / `suffix` / `stem` / `season_raw` / `episode_raw` / `rel_dir` / `works_dir`）与固定文本 `literal`，参数可设补零位数 `pad`                                                                                                 | `title` + `" S"` + `season(pad=2)` + `"E"` + `episode(pad=2)` + `suffix` ⇒ `某番剧 S01E01.mkv` |
| **集数正则与兜底（`episode_patterns` / `episode_fallback`）** | 集数从文件名里怎么认出来，以及**认不出**时怎么办。命名引用了集数却全不命中、且 `episode_fallback` 留空（`null`）⇒ 该文件**保留原文件名、不改名**（计入 `Task.result.kept_unresolved_episode`，`warnings[]` 有一条汇总告警，任务日志一行 `Kept (no episode): …`）。`legacy` 预设的字符串 `"None"` 则照旧渲染那个字面量 | `episode_patterns: [{name: ep, pattern: 'EP\s*(\d{1,2})'}]` + `episode_fallback: null`         |
| **文件作用域（`traverse.file_scope`）**                       | `subdir` = 文件取自匹配 `accept_dirs` 的子目录（番剧语义）；`title` = 文件直接取自标题文件夹（电影语义）                                                                                                                                                                                                              | `title`（电影）                                                                                |
| **接受目录正则（`accept_dirs`）**                             | 标题文件夹下哪些子目录算「一季」，并可指定季号捕获组 `season_group`；空数组 ⇒ 任何子目录都接受                                                                                                                                                                                                                        | `^Season (\d+)$`                                                                               |
| **拒绝目录正则（`reject_dirs`）**                             | 命中即**整棵子树跳过**（标题层级与所有子目录层级都生效）                                                                                                                                                                                                                                                              | `^(SP\|CM\|OP\|ED\|特典CD\|Preview)$`                                                          |
| **接受后缀（`extensions`）**                                  | 后缀白名单，**大小写敏感**。**它是回退值**：设置页的 `rename_extensions`（全局）与库级的 `rule_override.extensions` 会盖过它                                                                                                                                                                                          | `.mkv` / `.mp4` / `.ass`                                                                       |
| **选文件策略（`selection.per`）**                             | `file` = 每个匹配文件都处理（番剧）；`extension` = 同后缀只留体积最大者（电影）                                                                                                                                                                                                                                       | `extension`                                                                                    |
| **日志（`log_file`）**                                        | 该处理器的媒体库把「已处理」记录写到哪里；默认 `<库名 slug>cache.log`，只能是文件名                                                                                                                                                                                                                                   | `bangumicache.log`                                                                             |

内置三个处理器（**可重置、不可删除**）：`hardlink_only`（只建硬链接）、`bangumi`、`movie`。

> **两种写法，一处生效**
>
> - 处理器编辑器里多了一个「**命名模板**」输入框：像 calibre 那样一行写完
>   （`[{season}-{episode}] {title}{suffix}` → `[01-01] 某番剧.mkv`），`{episode:02}` 表示补零两位；
>   点「按模板生成段序列」就会把它编译成段序列 —— **段序列仍是唯一真源**，模板只是更好写的一种输入法。
> - 想让**某一个库**和别的库不一样（换命名、只加拒绝目录、只认某种后缀），**别去改处理器**
>   （那会波及所有引用它的库）—— 在**库编辑页**的「**重命名规则（本库）**」卡片里写**库级覆盖**：
>   打开开关后就改你想改的那几项，**没碰的照样继承处理器**，保存时会做跨层校验（例如处理器是
>   `title` 作用域、你却给这个库写了 `accept_dirs`，会被拦下并说明冲突在哪一层）。
>   WebUI 与 YAML 都能写（`libraries.rule_override`，见 PROCESSORS §3.3）。
> - **全局**后缀白名单在设置页（`rename_extensions`，留空 = 未设置）。优先级是
>   **库级覆盖 → 全局设置 → 处理器清单**。

### 8.2 在哪里改：WebUI 与配置文件**双向同步**

- **WebUI**：左侧导航「处理器」（`#/processors`）。列表 → 复制/编辑 → 四个区块可视化编辑
  （段的增删与上下移动、正则行可增删并显示能否编译、后缀 chips），顶部有「保存」与「恢复默认」。
  命名段区还有**实时预览**，用示例上下文渲染出新文件名。
- **配置文件**：`/data/config/processors/*.yaml`（一个处理器一个文件，文件名 = 处理器 id）与
  `/data/config/libraries.yaml`（媒体库清单）。都是 UTF-8、**YAML 1.1**（`yaml.safe_load` /
  `safe_dump`，`allow_unicode=True`、`sort_keys=False`、`default_flow_style=False`），以
  `schema_version: 2` 开头；正则一律写成**单引号标量**（`pattern: '^Season (\d+)$'`）以免转义规则介入。
- **同步方向**：WebUI 保存 → **先写数据库、再原子写出文件**（`*.yaml.tmp` → `os.replace`）；你在宿主机上直接改文件 →
  应用在数据库设置项 `config_sync_interval_seconds`（默认 **5 秒**，区间 0–86400；**不是环境变量**）内自动导入。
- **导入失败不会毁数据**：YAML 语法或规格校验不过时，**数据库保持可用**（继续按旧规则跑），
  错误显示在 WebUI 顶部红色横幅并写入配置事件表，而且**不会覆盖你的文件**。

### 8.3 `default` 与 `legacy` 两个预设

| 预设      | 含义                                                                                                                                                                                                                                                                                                                                                             |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `default` | 内置处理器**推荐的默认规则**：相对 `legacy` 的四处增量 —— ① 番剧的「拒绝目录」清单、② 电影的「拒绝目录」清单（把 `SP` / `CM` / `特典CD` / `Preview` 这类目录整棵跳过）、③ 番剧的 `episode_fallback = null` ⇒ **认不出集数就保留原文件名、不改名**、④ 番剧的 9 条有序集数规则。L3 特典排除（`extras_patterns`）是**全局设置**，对两个预设同等生效，不属于预设差异 |
| `legacy`  | **最朴素的旧口径**：`reject_dirs: []`、`episode_fallback: "None"`、后缀白名单 `.mkv/.mp4/.ass`（**大小写敏感**）—— 没有集数号也照样重命名成 `ENone`                                                                                                                                                                                                              |

> **内置 `bangumi` 的推荐默认**（也就是新装实例 / 新建处理器 / 「恢复默认」选 `default` 用的那份）把
> `episode_fallback` 设为 `null` ⇒ 命名引用了集数、正则可又全不命中的文件**保留原文件名、不改名**
> （计入 `Task.result.kept_unresolved_episode`，日志一行 `Kept (no episode): …`）。`legacy` 预设是最朴素的旧口径：
> 字符串 `"None"` 会把那个字面量渲染进文件名（例如 `… S01ENone.mkv`，同一作品的多个压制版本会撞成同一个目标名互相覆盖）。
>
> ⚠️ **升级提示**：处理器规格是**播种后独立存在**的（DB / `processors/*.yaml` 里已经存了旧的字符串 `"None"`），**升级镜像不会自动改它**。想让既有实例的番剧处理器也保留原名，见 [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md)（升级）与 [`docs/contracts/PROCESSORS.md`](docs/contracts/PROCESSORS.md) §3.2。

**想要最朴素的旧口径就用 `legacy`。** 随时可切：处理器页「恢复默认」→ 弹窗里选 `default` 或 `legacy`（二次确认）。

### 8.4 新增一类媒体库（不需要写 Python）

**媒体库文件夹不写死。** 下载根下的**任何**子目录都能建库（`Documentary`、`Anime-CN`、
`4K-Movies`……），唯一前提是它与目标库在**同一个挂载**（否则硬链接不可能成立，见[第 6 节](#6-卷挂载与源和目标必须在同一个挂载)）。

以「纪录片」为例，完整步骤：

1. **复制一个处理器**：处理器页 → 选最接近的（比如 `movie`）→「复制」→ 名字改「纪录片」、id 改成 `documentary`；
2. **改规则**：调整命名段（例如改成 `<标题> (<年份>)`）、接受目录正则、拒绝目录正则、后缀白名单，用预览区确认输出；
3. **保存**：落库并写出 `/data/config/processors/documentary.yaml`；
4. **建库**：媒体库页 → 新建 → 名称「纪录片」、**处理器选 `documentary`**、
   源目录 `/vol1/1000/Download/qbittorrent/Documentary`、目标目录 `/vol1/1000/Library/Documentary`、匹配分类 `Documentary`；
5. **验证**：列表里点「验证」，确认路径可达、**源与目标在同一个挂载**、处理器存在；
6. **试跑**：点「立即运行」（或在 qBittorrent 里触发回调），到任务详情页看日志是不是你要的结果；
7. 满意就继续接回调；不满意回到第 2 步改规则 —— 已处理过的文件夹由该库自己的日志记住，**不会重复劳动**。

> **媒体库的「类型」字段（`Bangumi` / `Movie` / `Custom`）现在只是一个自由标签**，用于展示与统计；
> **真正决定行为的是 `processor_id`**（必须存在于处理器列表里）—— 「类型」不参与行为判定。

### 8.5 三条你一定用得上的既有结论

- **匹配条件是 AND，不是 OR。** 媒体库上填了分类、标签、路径前缀中的**任意一项**，回调就必须
  **同时满足已填写的每一项**才会命中；三项**全空则永不匹配**（避免误抓）。
  详见 [`docs/guides/USER_GUIDE.md`](docs/guides/USER_GUIDE.md) §4.4。
- **`permission_mode`（权限模式）默认关闭，开启后会真的生效。** 开启后应用把**目标库**改成
  `0777`（含体积一致而被跳过的旧作品），好让非 root 服务读得到；媒体库文件与下载目录里的文件**是同一个 inode**，
  所以源文件的权限位会一起变（内容/大小/inode/mtime 不变）—— 因此界面从关到开必须二次确认。契约见
  [`docs/contracts/BASELINE.md`](docs/contracts/BASELINE.md) §1.6 与 B13。
- **不做任务去重。** 同一个种子重复回调会各建一个任务；幂等性由两个阶段的日志保证，
  所以「多跑一次」是安全的，只是多一条历史记录。

### 8.6 引擎表达不了怎么办

引擎支持的是**可枚举的规则维度**（命名段、目录正则、后缀白名单）。如果确实需要它表达不了的语义
（按外部数据库改名、调用转码……），正确做法是**扩展引擎**（在 `app/processors/engine.py` 里加一个能力，
并补回归测试）；行为基准是项目自己的黄金值测试。

## 9. 最小可复现实例

下面这段可以在**任何**装了 Docker 的机器上跑通（不需要 qBittorrent，也不需要特定的 NAS 环境），
用来验证「硬链接 + 重命名」的核心行为 —— 尤其是**库内文件与源文件共享同一个 inode** 这一点。

> **执行体都是应用自己的代码** —— 硬链接阶段由原生实现 `app/processors/hardlink.py` 完成
> （扫描与编排算法，规则不因库而异）；
> 重命名阶段由**配置驱动**的 `app/processors/engine.py` 完成。
> 行为由项目自己的回归测试（`tests/test_hardlink_native.py` 的冻结黄金值等）与 `legacy` 预设守护。

### 9.1 准备目录

```bash
BASE=/tmp/mlm-demo
rm -rf "$BASE"; mkdir -p "$BASE"/{dl,lib,data}
```

### 9.2 造一个假的番剧下载

```bash
mkdir -p "$BASE/dl/Bangumi/示例番剧/Season 1"
cd "$BASE/dl/Bangumi/示例番剧/Season 1"
printf 'subtitle-data' > "x 01.chs.ass"
head -c 2000000 /dev/urandom > "x 01.mkv"
printf 'subtitle-data' > "x 02.chs.ass"
head -c 2000000 /dev/urandom > "x 02.mkv"
find "$BASE/dl" -type f -printf '%s\t%p\n'
```

### 9.3 一条命令跑完整链路（推荐）

```bash
# 【任意装了 docker 的机器 / NAS 宿主】在仓库根执行
sh examples/minimal-repro.sh
```

它把上面这些步骤串起来自动跑完，并**断言**：

- 镜像能构建、容器以 uid 1000（非 root）运行；
- 源与目标确实落在**同一个挂载**内，且容器内**真实 link 探针**成功（跨挂载会打印可执行修法：把两者放进同一条 bind）；
  源目录**不再**单独 `:ro` 挂载，改由任务前后的源完整性核对保证（见[第 2 节](#2-核心不变量)第 3 条）；
- 健康检查 `status=ok`（`checks` 六项全过）；
- 登录 → 建库 → 只读预检 → 触发 run 的 API 全链路可用；
- **最关键的**：库内文件与源文件 `st_dev` + `st_ino` 相同、链接数 ≥ 2 —— **真硬链接，不是复制**。

`KEEP=1 sh examples/minimal-repro.sh` 会保留临时目录以便排查；`PORT=18099` 可换端口。

> 执行体都是应用自己的代码：硬链接由原生实现 `app/processors/hardlink.py` 完成，
> 重命名由配置驱动的 `app/processors/engine.py` 完成。
> 所以**不要**在宿主上直接 `import` 这些模块来复现（它们依赖容器内的路径与运行时）；
> 要在本地复现就用上面这条脚本，或者按[第 4 节](#4-安装部署)起容器后走网页 / API。

### 9.4 期望结果

```bash
stat -c '%i %h %n' \
  "$BASE/dl/Bangumi/Show A/Season 1/[Lilith-Raws] Show A - 01.mkv" \
  "$BASE/lib/Bangumi/Show A/Season 1/Show A S01E01.mkv"
# 同一个 inode 号，链接数 2 —— 这就是真硬链接
```

```
$BASE/lib/Bangumi/Show A/Season 1/
  Show A S01E01.mkv        ← 与下载文件同 inode（名字由「番剧」处理器的规则算出）
  （同目录下还有一条字幕，同样被重命名；确切名字看任务详情页的 Renamed: 行）
```

下载目录**一个字节都没变** —— 靠的是「代码层不写源目录 + 任务前后逐文件源完整性核对」，
而**不是** `:ro` 挂载（单挂载形态下做不到，见[第 6 节](#6-卷挂载与源和目标必须在同一个挂载)与[第 2 节](#2-核心不变量)第 3 条）。

## 10. 常见故障排查

详见 [`docs/guides/TROUBLESHOOTING.md`](docs/guides/TROUBLESHOOTING.md)。最常见的几类：

### 10.1 任务失败：「跨挂载 / EXDEV」

**症状**：任务详情里出现 `跨挂载` 与 `EXDEV`，日志出现 `[Errno 18] Invalid cross-device link`，任务状态 `link_failed`。

**原因**：源目录与目标目录**不在同一个挂载**里。最常见的是把 compose 的 volumes 拆成了两条独立 bind
（`/vol1/1000/Download/qbittorrent:/vol1/1000/Download/qbittorrent:ro` + `/vol1/1000/Library:/vol1/1000/Library`）——
Linux 的 `linkat` 比较**挂载身份**，跨挂载一律 `EXDEV`，
**与 `st_dev` 是否相同无关**。

**排查**：

```bash
# 容器内：两条路径是否落在同一个挂载（两条都该指向同一个挂载点）
docker compose exec media-ingest sh -c "findmnt -T /vol1/1000/Download/qbittorrent; findmnt -T /vol1/1000/Library"
```

也可以在 WebUI 的媒体库页点「验证（含实证探针）」，看 `same_filesystem` 三态：
`true` = 已实证可建；`false` = 已实证跨挂载；`null` = 无法实证（例如源目录下没有可链的普通文件）。

**修法**：改成单一共同父挂载 —— 把源与目标放进**同一条 bind**（`docker-compose.yml` 的 `MEDIA_ROOT`）：
`${MEDIA_ROOT:-/vol1/1000}:/vol1/1000`。详见[第 6.3 节](#63-卷挂载表)。

> `stat -c "%d"`（`st_dev`）相等**不能**证明能建硬链接 —— 只看 `st_dev` 的预检正是因此**假绿**（见[第 6.2 节](#62-本项目的处理方式)）。
> 本应用**不会**自动改成复制文件来"让它成功"：那会让后续所有去重与容量统计失真。

### 10.2 回调没触发

1. 在宿主机上本地 `curl` 一次（见 [5.4](#54-验证回调是否通)）确认服务本身没问题；
2. 确认 qBittorrent 的 **Run external program** 已勾选；
3. 确认命令是**一行**，且 `token` 与**设置页显示的那个**一致（不是 `.env`——见 §4.3；token 变了要同步更新 qB 里的命令）；
4. 看 `docker logs qbittorrent` 有没有 curl 的报错
   （qB 容器默认镜像自带 curl；若你的镜像没有，改用宿主 crontab 调 `/api/hooks/scan`）。

### 10.3 任务成功但媒体库里没文件

按顺序检查：

1. 该任务的**媒体库匹配**是否命中（分类/标签/路径前缀）—— 任务详情里有「匹配到的库」；
2. 建链阶段是否因为**体积未变**而跳过（L9）—— 日志里会出现「跳过: <文件夹>」；
3. 目标路径是否真的写进了 `/vol1/1000/Library` —— 在宿主机上 `ls` 一下；
4. 若你重新下载了同一部番剧但**总大小恰好相同**，处理会跳过（判据是"体积一致"）。这是既有行为，
   在 WebUI 里对该库用「强制重新处理」可以绕过（它会先删掉日志里的体积记录）。

### 10.4 权限问题

| 症状                                  | 原因                             | 处理                                                                            |
| ------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------- |
| 建链接时 `Permission denied`          | 目标库目录对容器 uid 不可写      | `ls -ld /vol1/1000/Library`，必要时 `chmod` 或调整 `PGID`                       |
| 配置文件写入失败 / WebUI 顶部红色横幅 | `/data/config` 对容器 uid 不可写 | entrypoint 会自动修 `/data` 属主；若宿主目录被别的 uid 占了，检查 `PUID`/`PGID` |
| 容器起来就退出                        | `/data` 属主不可写               | entrypoint 会自动修；若宿主目录被别的 uid 占了，检查 `PUID`/`PGID`              |

排查用的命令：

```bash
docker compose exec media-ingest id
docker compose exec media-ingest ls -ld /data /data/config /data/logs
docker compose exec media-ingest sh -c "findmnt -T /vol1/1000/Download/qbittorrent; findmnt -T /vol1/1000/Library"
```

### 10.5 中文乱码 / `UnicodeEncodeError`

日志文件 `open(LOG_FILE)` 未指定编码。容器内已固定 `LANG=C.UTF-8` + `PYTHONUTF8=1`；
若你自行改了这些环境变量导致乱码，把它们改回来。

### 10.7 满屏 `File exists` / 明明处理过却重扫

**症状**：日志里大量 `创建硬链接失败: ... File exists`（Linux 是 `[Errno 17]`），
或者已经链接过的作品又被重新走了一遍；任务统计里 `skipped` 是 0。

> 如果这些目标是**源文件自己的硬链接**（同一 inode），应用会把它们算成
> **幂等跳过**（结果里的 `already_linked`，并计入 `skipped`），任务**不会**再因此判 `failed`；
> 只有"同名但 inode 不同"（目标被别的文件占着）仍然是 `link_failed` —— 应用**绝不覆盖**它。

**原因**：`hard_links.log` 里的 `Source:` 与当前媒体库的源目录**字符串不相等**，
建链阶段因此认为「这个源目录一条历史记录都没有」（判据是**完全相等**比较）。
最常见的两种情形：

1. 你**改了某个媒体库的源目录**（例如 `/vol1/1000/Download/qbittorrent/Bangumi` → `/vol1/1000/Download/qbittorrent/Bangumi2`）—— 历史记录还挂在旧路径下；
2. 日志被清空过（`rm /data/logs/hard_links.log`、换了数据卷、恢复了一份旧备份）。

**排查**（NAS 宿主执行）：

```bash
# 1) 看日志里实际记着哪些 Source（应当是容器路径）
docker compose -f /vol1/1000/Docker/media-ingest/docker-compose.yml \
  exec media-ingest sh -c "grep -o 'Source: [^ ]*' /data/logs/hard_links.log | sort -u | head"

# 2) 对比媒体库配置里的源目录（应为容器路径，如 /vol1/1000/Download/qbittorrent/Bangumi）
curl -s http://<你的主机IP>:18081/api/libraries | python3 -m json.tool | grep -E 'source_dir|name'
```

**处理**：

- 如果**是源目录填错了**：把它改回去即可，历史记录立刻重新生效；
- 如果**确实想按新源目录全量重来一次**：什么都不用做。那次重扫本身是安全的
  （对已存在的硬链接只是 `File exists` 失败，不覆盖、不删除数据），跑完就有了新的记录；
- 想确认记录到底有没有对上：看任务详情页的 `result.skipped`，以及每个被跳过项的
  `reason`（中文）—— 那是唯一权威的判据。

## 11. 升级与备份

### 11.1 备份

需要备份的**只有**一个目录：

```bash
# 建议先停一下保证 SQLite 一致（WAL 模式下也可以在线用 sqlite3 .backup）
docker compose stop media-ingest
tar czf ~/mlm-backup-$(date +%F).tar.gz -C /vol1/1000/Docker/media-ingest data
docker compose start media-ingest
```

`data/` 里包含：

| 文件         | 内容                                                                                      |
| ------------ | ----------------------------------------------------------------------------------------- |
| `mlm.db`     | 媒体库与处理器规则、任务历史、设置、管理员口令哈希                                        |
| `config/`    | **处理器与媒体库的配置文件**（`processors/*.yaml` + `libraries.yaml`）—— 与数据库双向同步 |
| `secret.key` | 会话签名密钥（丢了会强制所有登录失效）                                                    |
| `logs/*.log` | 硬链接体积记录 + **每个媒体库一条**重命名记录 —— **这是幂等性的依据，务必保留**           |

> ⚠️ `data/logs/hard_links.log` 记录着「哪个文件夹已经处理过、当时多大」。
> 丢掉它会**丢失幂等状态**：所有文件夹会被当成新的重新处理一遍
> （不会损坏数据，但会重跑一遍全量扫描，并且孤儿清理的判定会失准）。

**不需要备份的**：`/vol1/1000/Library` 里的硬链接 —— 它们是链接，源文件在下载目录。
但 `/vol1/1000/Library` 里**重命名过**的文件名是"数据"，删库重建不会自动恢复；
如果要连媒体库一起备份，直接备 `/vol1/1000/Library`。

### 11.2 升级

```bash
cd /vol1/1000/Docker/media-ingest

# 1. 备份
tar czf ~/mlm-backup-$(date +%F).tar.gz data

# 2. 取新代码，重新构建
docker compose build --pull

# 3. 重启（数据卷不动，SQLite 会自动迁移）
docker compose up -d

# 4. 验证
curl -s http://127.0.0.1:18081/api/system/health
docker compose logs --tail=50 media-ingest
```

数据库 schema 的变更由应用的启动迁移负责，是**向前兼容的增量迁移**（只加列/加表）。
**回滚**：把镜像 tag 切回上一版并恢复 `data/` 备份。

### 11.3 卸载

```bash
cd /vol1/1000/Docker/media-ingest
docker compose down          # 只停容器，数据卷保留
```

**删除数据前请记住**：`/vol1/1000/Library` 里的硬链接**不会被自动删除**。
即使删掉本应用的全部数据，媒体库里的文件依然可用（这正是硬链接的好处）。
如果要把 `/vol1/1000/Library` 一起清掉，先确认那些文件是不是还有别的硬链接指向同一 inode：

```bash
find /vol1/1000/Library -type f -links +1 -printf '%i %n\n' | head
```

## 12. 开发与测试

```bash
# 全量测试（需要 Python 3.11+ 与 pytest；不需要 docker）
python -m pytest tests/ -v

# 硬链接行为的黄金值回归
python -m pytest tests/test_hardlink_native.py -v

# 适配与编排（硬链接、孤儿清理、作用域）
python -m pytest tests/test_adapter_link.py tests/test_adapter_orphan.py tests/test_adapter_scoped.py -v

# 路径转换 / 库级规则覆盖 / 幂等性
python -m pytest tests/test_paths.py tests/test_library_overrides.py tests/test_idempotency.py -v
```

装不了 pytest 的环境可以用仓库自带的降级运行器（纯标准库，覆盖同一批测试）：

```bash
python tests/run_all.py
```

测试覆盖的核心点见 [`docs/contracts/COMPATIBILITY.md`](docs/contracts/COMPATIBILITY.md)：

- 硬链接**路径规则与目录层级**未变（与冻结的黄金值比对）
- 重命名**正则与命名结果**未变（含 `.ass` 特例与电影"保留最大文件"分支）
- **qBittorrent 原文件未被移动/删除/修改**（比对前后 mtime/inode/目录树）
- **跨文件系统时明确失败**，且产物里**不存在复制出来的副本**
- **幂等**：连跑两次，第二次不重复建链、不重复重命名
- 规则校验：非法路径、白名单外的目标、不存在的 `processor_id`、非法处理器规格都被拒绝
- 配置同步：外部编辑 `processors/*.yaml` 能被导入；导入失败时数据库保持可用且错误可见

## 13. ARM64 支持

镜像基座 `python:3.11-slim` 是官方多架构镜像（`linux/amd64` + `linux/arm64`），
Python 依赖只有 `fastapi` 与 `uvicorn[standard]`，两者都有 `aarch64` wheel；
`gosu` / `tzdata` / `ca-certificates` 在 Debian bookworm 的 arm64 仓库中齐全。
本项目**不使用**任何编译型依赖或平台特有的二进制。

因此在 ARM64 NAS 上构建的**成本是可控的**：

```bash
# 在 ARM64 机器上直接构建
docker compose build

# 或在 x86 机器上交叉构建（需要 buildx + qemu）
docker buildx build --platform linux/arm64 -f docker/Dockerfile -t media-ingest:latest .
```

**未经实测**的部分：交叉构建的产物，以及 qBittorrent 的 host 网络回调在 ARM 上的表现。
本项目当前只在 x86_64 上完整验证过；ARM64 属于「设计上支持、未经实测」。

---

## 14. 目录结构

四句话（完整版见 [`docs/meta/STRUCTURE.md`](docs/meta/STRUCTURE.md)）：

- **仓库工作树**（源码，进 git）：`app/`（后端）、`web/`（前端）、`docker/`（镜像三件套）、
  `tests/`、`docs/`、`examples/`、`scripts/`（当前工具链 6 个脚本，见 [`scripts/README.md`](scripts/README.md)）、
  `reports/`（按版本归档的验收报告与产物）。根目录只有源码目录与本文件、
  [`CHANGELOG.md`](CHANGELOG.md)、`docker-compose.yml`、`.env.example`、两个 ignore 文件。
- **镜像内**（构建产物）：应用根是 **`/srv/mlm`**，其下 `app/`（Python 包）与 `web/`（静态前端）；
  容器脚本安装在 `/usr/local/bin/mlm-entrypoint` 与 `/usr/local/bin/mlm-healthcheck`；
  镜像里没有 `tests/`、`docs/`、`.env`、数据库或密钥。
- **运行期数据卷**（宿主 `.../media-ingest/data` ↔ 容器 `/data`）：`mlm.db`、
  `secret.key`、`config/{libraries.yaml, processors/*.yaml}`、`logs/*.log` —— **唯一必须备份的东西**。
- **宿主上的构建/测试目录**：`.../media-ingest/{build-src,test-data}`（可丢，脚本自己重建）。

---

## 附：相关文档

> `docs/` 按读者分三桶：**`contracts/`**（给 AI / 实现者的契约）、**`guides/`**（给用户 / 运维的手册）、
> **`meta/`**（结构与证据）；用途与阅读顺序见 [`docs/README.md`](docs/README.md)。

| 文档                                                                 | 内容                                                                           |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| [`docs/README.md`](docs/README.md)                                   | **文档导航**：三桶划分与建议阅读顺序                                           |
| [`docs/meta/STRUCTURE.md`](docs/meta/STRUCTURE.md)                   | **目录结构权威说明**：仓库 / 镜像 / 数据卷三套目录、命名约定、新增文件该放哪   |
| [`scripts/README.md`](scripts/README.md)                             | 工具链脚本用法（构建镜像、起容器、跑 pytest、全量验收、备份归档）              |
| [`reports/README.md`](reports/README.md)                             | 验收报告索引与命名口径                                                         |
| [`docs/contracts/PROCESSORS.md`](docs/contracts/PROCESSORS.md)       | **契约唯一真源**：处理器规格、规则引擎语义、双向同步、日志模型、端点与迁移步骤 |
| [`docs/contracts/BASELINE.md`](docs/contracts/BASELINE.md)           | **行为契约**：硬链接与重命名的行为、已知限制 L1–L9                             |
| [`docs/contracts/ARCHITECTURE.md`](docs/contracts/ARCHITECTURE.md)   | 架构冻结：选型、容器路径布局、数据模型、API、模块契约、命名与部署约定          |
| [`docs/guides/DEPLOYMENT.md`](docs/guides/DEPLOYMENT.md)             | 部署详解与实操步骤                                                             |
| [`docs/contracts/API.md`](docs/contracts/API.md)                     | HTTP API 参考（含 qBittorrent 回调与健康检查）                                 |
| [`docs/guides/USER_GUIDE.md`](docs/guides/USER_GUIDE.md)             | WebUI 使用手册                                                                 |
| [`docs/contracts/COMPATIBILITY.md`](docs/contracts/COMPATIBILITY.md) | 兼容性验证方法与结果                                                           |
| [`docs/guides/TROUBLESHOOTING.md`](docs/guides/TROUBLESHOOTING.md)   | 故障排查手册                                                                   |
| [`docs/guides/FAQ.md`](docs/guides/FAQ.md)                           | 常见问答                                                                       |
| [`docs/meta/EVIDENCE.md`](docs/meta/EVIDENCE.md)                     | 验收证据：每条结论对应的验收项（E3–E8）                                        |
| [`docs/meta/KNOWN_ISSUES.md`](docs/meta/KNOWN_ISSUES.md)             | 已知问题与遗留清单（KI-1…）                                                    |
| [`CHANGELOG.md`](CHANGELOG.md)                                       | 版本变更记录                                                                   |

## 许可

MIT
