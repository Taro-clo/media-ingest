# NAS 实战示例：部署 + 新增媒体库 + 验证硬链接

> 这是一份**可以照着敲**的完整流程（飞牛 fnOS / 任意 Linux NAS 通用）。
> 与 `docs/guides/DEPLOYMENT.md`（部署手册，讲原理与全部选项）的分工：
> 本文只给**一个具体实例**的完整命令与预期输出，出错了再去查手册对应章节。
>
> 本例中出现的宿主路径都是本项目的示例默认值（可按你的环境修改）：
> 媒体共同父挂载 `/vol1/1000`（源 `/vol1/1000/Download/qbittorrent`、
> 媒体库 `/vol1/1000/Library` 都在它下面）、
> 应用数据 `/vol1/1000/Docker/media-ingest/data`。
>
> **有两条硬性形态要求**（都不是"建议"，违反会直接失败）：
> 1. **源与目标必须在同一个挂载里** —— 所以只挂一条"共同父挂载" `/vol1/1000`，
>    且**容器内路径 = 宿主路径**。内核在 `link()` 时比较两次路径解析得到的**挂载身份**
>    （`fs/namei.c` 的 `do_linkat()`：`old_path.mnt != new_path.mnt` 即回 `EXDEV`），
>    与 `st_dev`/`fsid`/是否同一子卷**都无关**；把源与库拆成两条 bind 就会失败，
>    `linked=0 / link_failed=1`（`[Errno 18] Invalid cross-device link`）。
> 2. 容器**自包含**：应用只执行自己的原生实现（`app/processors/hardlink.py` 与
>    `app/processors/engine.py`），不需要宿主提供任何额外脚本或目录。
>
> 挂载只有**两条**：`/vol1/1000:/vol1/1000`（媒体）与 `./data:/data`（应用数据）。

---

## 0. 本例要达成的结果

1. 容器**自包含**：硬链接与重命名都由应用自己的实现（`app/processors/hardlink.py` / `app/processors/engine.py`）完成，不需要宿主提供任何额外文件或目录；
2. 媒体挂载与数据卷是**两条独立挂载**（启动时那条 `chown -R /data` 绝不碰媒体文件）；
3. 网页能打开、容器显示 `(healthy)`；
4. 新增一个「番剧库」，让 `Anime` 分类的种子在下载完成后自动：
   建硬链接到 `/vol1/1000/Library/Bangumi/<一级文件夹名>/`，并把集数文件名改成 `SxxExx`；
5. 用 `stat` 证明媒体库里的文件与下载源**是同一个 inode**（真硬链接，不是复制）。

---

## 1. 起服务（两种方式，任选一种）

### 方式 A：用 `examples/docker-compose.nas.yml`（推荐，路径已内联，不依赖 .env）

```sh
# 1) 建数据目录并把属主给容器内运行身份（PUID:PGID = 1000:1001）
sudo mkdir -p /vol1/1000/Docker/media-ingest/data
sudo chown -R 1000:1001 /vol1/1000/Docker/media-ingest

# 2) 构建并启动（在仓库根执行；本文件里的 build.context 是 ..）
docker compose -f examples/docker-compose.nas.yml up -d --build

# 3) 看状态，等 STATUS 变成 Up ... (healthy)
docker compose -f examples/docker-compose.nas.yml ps
```

> 宿主上**不需要**任何额外准备：镜像自包含，媒体父挂载 `/vol1/1000` 也**不需要**做任何事
> （entrypoint 只读地检查它，唯一的 `chown -R` 只作用于 `/data`）。

**参考数据（示例环境）：** `python:3.11-slim` 基础镜像预先存在时，
`docker compose build` 约 **2~5 分钟**、产出镜像 `media-ingest:latest` **180 MB**。
`/vol1` 剩余空间需 ≳ 1 GB 即可（构建缓存与新图层）。

```sh
# 想只看构建结果、不启动：
docker compose build
docker images media-ingest:latest --format '{{.Repository}}:{{.Tag}} {{.Size}}'
```

### 方式 B：用仓库根 `docker-compose.yml`（带 `env_file: .env`，长期使用推荐）

```sh
cp .env.example .env
vi .env        # 至少确认 PUID / PGID / MEDIA_ROOT，并填 ADMIN_USERNAME / ADMIN_PASSWORD
docker compose up -d --build
docker compose ps
```

> `.env` 里的键**只用于 compose 插值**，不是自动注入容器的环境变量；
> 真正进容器的是 `docker-compose.yml` 的 `environment:` 段（它引用 `${VAR}`），
> 两边**逐条对应**。应用代码**不读** `.env` 文件。

### 启动后的三个硬校验

```sh
# ① 健康检查：必须 status=ok 且 checks 全 true（只看 HTTP 200 不算数，该端点永远 200）
curl -s http://127.0.0.1:18081/api/system/health
# 期望：{"status":"ok","checks":{"db":true,"data_dir":true,
#        "downloads_readable":true,"library_writable":true,
#        "processors":true,"config":true}}     ← 共六项，缺键也判 unhealthy

# ② 运行身份不是 root
docker exec media-ingest sh -c 'id'
# 期望：uid=1000 gid=1001

# ③ 源与目标同一挂载（硬链接可用的前提）+ 两条挂载互相独立（chown 碰不到媒体）
docker exec media-ingest sh -c \
  'grep -E " /vol1/1000 | /data " /proc/self/mountinfo | cut -d" " -f1,5'
# 期望两行，且两行的**挂载 ID 不同** → 媒体挂载与数据卷是两条独立挂载。
#    若两行 ID 相同，entrypoint 会直接拒绝启动（那条 chown -R /data 会改到媒体文件属主）。
```

> ③ 只证明"两条挂载独立"。**证明硬链接真能建**的是下面的实测探针（这才是主判据 ——
> 挂载 ID 相同则一定失败，但**挂载 ID 不同不保证成功**，例如 btrfs 同一挂载内跨子卷
> 仍可能 EXDEV）。探针**只读源文件**、只在目标根下建一个临时名并立即删除（源目录零写入）：

```sh
docker exec media-ingest sh -c '
  w=$(mktemp -d /vol1/1000/Library/.mlm-probe.XXXXXX) || exit 1
  f=$(find /vol1/1000/Download/qbittorrent -maxdepth 3 -type f -print -quit)
  [ -n "$f" ] || { echo "源目录里没有可链的样本文件（跳过）"; rm -rf "$w"; exit 0; }
  ln "$f" "$w/probe.link" && echo "实测 link() 成功（同一挂载）" || echo "实测 link() 失败 = 跨挂载"
  rm -rf "$w"'
# 期望输出 "实测 link() 成功（同一挂载）"。
# 报 "Invalid cross-device link" ⇒ 源与目标不在同一挂载，请检查 compose 的 volumes 形态。
```

如果 ① 不是 `status:"ok"`，先看**是哪一项 false**：

```sh
docker inspect --format '{{json .State.Health}}' media-ingest
# Log[].Output 里有 healthcheck.sh 打印的 [失败] / [缺失] 明细，例如：
#   healthcheck: 不健康 status='degraded'
#     [失败] checks.config = False         → /data/config 对容器用户不可写
#     [失败] checks.library_writable = False → 设置里的"允许的目标根目录"不存在或不可写
#     [失败] checks.downloads_readable = False → "允许的源根目录"不存在或不可读
```

> ⚠️ **沿用已有数据库时最容易踩的一条**：DB 里已存的"允许的源/目标根目录"仍然是
> `/downloads`、`/library`（那些路径在容器里**已不存在**），
> 现象就是上面两项 false、容器一直 unhealthy。修法：到 WebUI「设置」页（或
> `PUT /api/settings`）把**允许的源根目录**改成 `/vol1/1000/Download/qbittorrent`、
> **允许的目标根目录**改成 `/vol1/1000/Library`。改 `.env`/compose 里的
> `DEFAULT_SOURCE_ROOT`/`DEFAULT_TARGET_ROOT` **对已存在的 DB 不生效**（它们只是首播初值）。

---

## 2. 新增一个「番剧库」（本示例的核心）

字段含义见 `README.md` §8；处理器与类型的对应关系是硬约束：
`Bangumi → bangumi`、`Movie → movie`、`Custom → hardlink_only|bangumi|movie`。

**方式一：网页上点（推荐先用这个把界面认全）**

媒体库页 → 新建 → 按下面填：

| 字段 | 本例填的值 |
|---|---|
| 名称 | `番剧库` |
| 类型 | `番剧` |
| 处理器 | `bangumi`（番剧重命名） |
| 启用 | ✅ |
| 源目录 | `/vol1/1000/Download/qbittorrent/Bangumi` |
| 目标根路径 | `/vol1/1000/Library/Bangumi` |
| 匹配分类 | `Anime` |
| 匹配标签 | （留空） |
| 匹配路径前缀 | `/vol1/1000/Download/qbittorrent/Bangumi` |
| 孤儿清理 | ✅ |
| 权限模式 | ⬜（不要勾，除非你确实想让媒体库对所有人都可读） |

> **路径必须是宿主真实路径**（因为容器路径 = 宿主路径）。任何 `/downloads`、`/library`
> 这类旧路径在容器里都找不到东西 —— 它们只出现在旧 DB 的迁移场景里（见 §1 的提醒）。

**方式二：用 API（等价，适合脚本化/复现）**

```sh
BASE=http://127.0.0.1:18081
JAR=/tmp/mlm-cookies.txt

# 登录（若 .env 里设了 ADMIN_*，这里用同一对；未设则先到网页上创建管理员）
curl -sS -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
     -c "$JAR" -d '{"username":"admin","password":"你的密码"}'

# 创建媒体库 → 201 Created
# processor_id 是处理器引用的规范字段（可用 GET /api/processors 列出全部 id）；
# type 只是自由文本标签，不参与任何逻辑。
curl -sS -X POST "$BASE/api/libraries" -H 'Content-Type: application/json' -b "$JAR" -d '{
  "name": "番剧库",
  "type": "番剧",
  "processor_id": "bangumi",
  "enabled": true,
  "source_dir": "/vol1/1000/Download/qbittorrent/Bangumi",
  "target_dir": "/vol1/1000/Library/Bangumi",
  "match_categories": ["Anime"],
  "match_tags": [],
  "match_path_prefixes": ["/vol1/1000/Download/qbittorrent/Bangumi"],
  "orphan_cleanup": true
}'
# 响应里的 "id" 就是库 id，下面记为 <LIB_ID>
# 注意：示例里**故意不传** permission_mode —— 它是**默认关闭**的显式开关
# （开启会把目标库改成 0777，且因硬链接共享 inode 而**连带改到源文件权限位**；界面从关到开需二次确认）。
# 也不要传 log_file：留空时系统按库自动分配唯一名（第 1 个库 = bangumicache.log，
# 第 2 个 = bangumicache-2.log …）；显式传一个已被其它库占用的名字会 422。

# 先「验证」：会对源/目标做**真实 link 实测**（在目标根下建临时名再删，源目录零写入），
# 并返回 same_filesystem：true / false / null（null = 源目录里没有可实证的文件，如空目录）。
curl -sS -X POST "$BASE/api/libraries/<LIB_ID>/validate" -b "$JAR"
```

> **三条匹配条件（分类 / 标签 / 路径前缀）是 OR 关系，但"全空"≠"匹配所有"**。
> 三条都不填时该库**永远不命中**，回调会返回 `ignored` 并在 reason 里说明 —— 这是
> 刻意的安全设计（避免一个没配条件的库把整个下载源树的下载全吞进来）。

---

## 3. 造一个假下载，验证硬链接真的建起来了

不用等真实下载，手工造一个与 qBittorrent 完全一样的目录结构即可：

```sh
# 一级文件夹名 = 番剧标题（应用只扫一级子目录）
sudo mkdir -p '/vol1/1000/Download/qbittorrent/Bangumi/葬送的芙莉莲'
sudo dd if=/dev/urandom of='/vol1/1000/Download/qbittorrent/Bangumi/葬送的芙莉莲/第1集.mkv' bs=1M count=8
```

触发一次**全量**或**单文件夹**执行：

```sh
# 单文件夹（folder 必须是一级文件夹名，含 / 会被 400 拒绝）
curl -sS -X POST "$BASE/api/libraries/<LIB_ID>/run" -H 'Content-Type: application/json' \
     -b "$JAR" -d '{"scope":{"mode":"folder","folder":"葬送的芙莉莲"},"force":false}'
# → 202 + 任务对象，含 "id"（下面记为 <TASK_ID>）

# 看任务是否成功 + 看日志
curl -sS "$BASE/api/tasks/<TASK_ID>" -b "$JAR"
curl -sS "$BASE/api/tasks/<TASK_ID>/logs?after_id=0&limit=200" -b "$JAR"
# 任务 result 里的计数是最终判据：linked > 0 且 link_failed == 0 才算成功。
# 若看到 link_failed > 0 且 samples 里是 "Invalid cross-device link" ⇒ 源与目标跨挂载。
```

**验收：inode 相同才是真硬链接**（复制会得到不同的 inode）：

```sh
stat -c '%d %i %h %n' \
  '/vol1/1000/Download/qbittorrent/Bangumi/葬送的芙莉莲/第1集.mkv' \
  '/vol1/1000/Library/Bangumi/葬送的芙莉莲/葬送的芙莉莲 S01E01.mkv'
# 期望：两行的 %d（设备号）与 %i（inode）完全相同，第二行 %h（链接数）= 2
# 注意重命名处理器会把文件名改成 "标题 SxxExx"，这就是输出名
# ⚠️ 注意：%d 相同**不代表** link() 一定能成功（跨挂载时 st_dev 也可能相同）。
#    所以真正的判据是"任务 result.linked > 0"，不是 %d。
```

如果你想**只看硬链接不看重命名**，把库的处理器换成 `hardlink_only`（类型填 `Custom`），
此时媒体库里会保留原始文件名，`stat` 比对更直观。

---

## 4. 接上 qBittorrent（让第 3 步自动发生）

照着 `examples/qbittorrent-autorun.md` 做即可，核心三步：

1. 网页「设置」页复制**完整回调命令**（含 `token=...`，token 等同写权限，别外泄）；
2. qBittorrent → ⚙ 选项 → **下载**（Downloads）→ 勾选
   **「Torrent 完成时运行外部程序」**（`Run external program on torrent finished`），
   把整条命令粘进去（所有 `--data-urlencode` 一个都别删）；
3. 故意勾错「添加时运行」会过早触发（文件还没下完），**不要勾那个**。

**⚠️ 路径含义（最容易踩的第二个坑）**：容器路径 = **宿主路径**，
所以回调里的 `%F`（内容路径）必须是**宿主路径**（`/vol1/1000/Download/qbittorrent/...`），
而 qBittorrent 容器里通常看到的是 `/downloads/...`。三选一：

```sh
# (a) 推荐：把 qBittorrent 也改成"容器路径 = 宿主路径"（与本源同一原则），
#     这样 %F 天生就是 /vol1/1000/Download/qbittorrent/...，零转换：
#       -v /vol1/1000/Download/qbittorrent:/vol1/1000/Download/qbittorrent
# (b) 保留 qB 的 /downloads 挂法，在回调脚本里把前缀换成宿主路径再发：
#       P="$1"; P="/vol1/1000/Download/qbittorrent${P#/downloads}"
# (c) 不改 qB：把媒体库的"源目录/匹配路径前缀"也写成 /downloads/... 是**没用**的 ——
#     那个路径在本应用容器里不存在，回调会因不在"允许的源根目录"内而被拒绝。
```

---

## 5. 本例涉及的卷挂载（一张表）

| 宿主路径 | 容器路径 | 模式 | 为什么 |
|---|---|---|---|
| `/vol1/1000` | `/vol1/1000` | `rw` | **单一共同父挂载**：源与目标都在里面 ⇒ `link()` 不 EXDEV；容器路径 = 宿主路径 ⇒ 日志/WebUI 显示的就是宿主真实路径 |
| `/vol1/1000/Docker/media-ingest/data` | `/data` | `rw` | SQLite、任务日志、每个媒体库的日志、`secret.key`，以及 `config/`（`libraries.yaml` + `processors/*.yaml`）|

**只有这两条**。不要把源与库拆成两条独立 bind（`/downloads` + `/library`），那样硬链接会失败。
自查：

```sh
docker inspect media-ingest --format '{{json .Mounts}}' | tr ',' '\n' | grep -o '"Destination":"[^"]*"'
# 期望**恰好两条**：/vol1/1000 与 /data
#   出现 /downloads 或 /library ⇒ compose 还是拆成两条 bind 的形态，硬链接会失败，请改掉
```

`/data/config` 就在 `/data` 卷里，**不需要**单独挂载（想单独备份配置时才额外 bind 一条，
见 `docker-compose.yml` 里 `CONFIG_DIR` 的注释）。

**唯一一条最容易踩的坑**：源与目标必须在**同一个挂载**里。内核在 `link()` 时比较
**挂载身份**（不是 `st_dev`、不是 `fsid`、不是"是否同一子卷"），跨挂载一律 `EXDEV`。
本应用**绝不**回退为复制文件，会直接把任务判为失败（`link_failed > 0`）。
自查（两条都要看：挂载 ID 是否相同、实测 link 是否成功）：

```sh
findmnt -T /vol1/1000/Download/qbittorrent -o TARGET,SOURCE,FSROOT
findmnt -T /vol1/1000/Library             -o TARGET,SOURCE,FSROOT
# 期望两行的 TARGET 都是 /vol1（同一个挂载）；⚠️ 注意 findmnt 显示的 TARGET 相同
# **不能**证明 link 一定成功（btrfs 同挂载跨子卷仍可能 EXDEV），所以以实测探针为准
# —— 见 §1 的 ③ 那段。若这两个目录同属一个挂载（示例：都在 `/vol1` 下），那么"各挂一条 bind"
# 会让源与目标分属不同挂载，link() 必然 EXDEV。
```

---

## 6. 一条命令跑完「静态自验」

部署前想先确认交付资产没问题（不需要 docker 也能跑前 5 节）：

```sh
sh examples/validate-deployment-assets.sh .
# 期望：末尾汇总 **0 FAIL**（当前 34 PASS / 0 FAIL —— PASS 条数会随版本增加，
# 判据是 [FAIL] 计数为 0，不是某个固定 PASS 数）
#         静态自验全部通过。

# 有 docker 时追加"构建上下文 + Dockerfile 干跑"检查
MLM_BUILD_CHECK=1 sh examples/validate-deployment-assets.sh .
```

> 它**不构建镜像、不起容器**。镜像真能构建、容器真 (healthy)、硬链接真建立，
> 这三项必须真机验收（就是本文 §1 与 §3 的内容）。

---

## 7. 卸载 / 重来

```sh
docker compose -f examples/docker-compose.nas.yml down
# 数据都在宿主上，删掉 data 目录才会真正"重来"：
# sudo rm -rf /vol1/1000/Docker/media-ingest/data
# 注意：媒体库下的硬链接与下载源里的原文件是同一个 inode，
#       删硬链接不会删数据；但删源里的原文件会让硬链接变成孤儿。
```
