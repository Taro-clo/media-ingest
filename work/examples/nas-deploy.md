# NAS 实战：部署 → 建库 → 造假下载 → 看见硬链接

> **本文是一次跟做的 walkthrough**，约 15 分钟，终点是「用 `stat` 证明媒体库里的文件与下载源是同一个 inode」。
> 前置检查、卷挂载原理、升级备份、排错不在本文，见 [`DEPLOYMENT.md`](../docs/guides/DEPLOYMENT.md) §1 / §2–§3 / §6–§7。

**本文所有路径、端口、UID 都是示例值**，用前按下面这张表整篇替换（`docker-compose.nas.yml` 里是同一组值）：

| 占位 | 示例值（本文通篇用它） | 含义 / 怎么定 |
|---|---|---|
| `<NAS_IP>` | `192.168.1.100` | NAS 局域网 IP；**在本机执行用 `127.0.0.1`**，别的机器访问才换它 |
| `<媒体父目录>` | `/vol1/1000` | 下载目录与媒体库的**共同父目录**，必须同一个挂载 |
| `<下载目录>` | `/vol1/1000/Download/qbittorrent` | qBittorrent 的下载目录，在 `<媒体父目录>` 之下 |
| `<媒体库目录>` | `/vol1/1000/Library` | 建硬链接的目标树，也在 `<媒体父目录>` 之下 |
| `<数据目录>` | `/vol1/1000/Docker/media-ingest/data` | 挂到容器 `/data` 的宿主目录（唯一要备份的卷） |
| `<PUID>:<PGID>` | `1000:1001` | 容器运行身份；须能读 `<下载目录>`、写 `<媒体库目录>` |
| `<应用端口>` | `18081` | 本应用端口 |
| `<qB端口>` | `18080` | qBittorrent WebUI 端口 |

- `/vol1/1000` 只是飞牛 fnOS 的默认存储路径，**不是本项目的前提**。唯一的硬性形态要求：下载目录与媒体库在**同一个挂载**内
  —— `linkat(2)` 比较挂载身份，跨挂载一律 `EXDEV`，且应用不会退化成复制（[`COMPATIBILITY.md`](../docs/contracts/COMPATIBILITY.md) §2）。
- 代码块里一律写**示例值**，可逐条照抄再按上表替换；写成 `<…>` 的占位必须换成你自己的值。

---

## 0. 走完你会得到什么

1. 容器 `(healthy)`，且 `/api/system/health` 的 `checks` 六项全 `true`；
2. 一个「番剧库」：分类 `Anime` 的种子下载完成后，在 `<媒体库目录>/Bangumi/<一级文件夹名>/` 下建硬链接，集数文件改成 `标题 SxxExx`；
3. `stat` 显示源与目标**设备号相同、inode 相同、链接数 = 2** —— 真硬链接，不是复制；媒体文件属主也没被那条 `chown -R /data` 碰过。

## 1. 起服务（一条命令）

```sh
# 1) 建两个宿主目录：数据目录归容器运行身份；媒体库目录只要有写权限（PUID 通常就是本机管理员）
sudo mkdir -p /vol1/1000/Docker/media-ingest/data /vol1/1000/Library
sudo chown -R 1000:1001 /vol1/1000/Docker/media-ingest
# 2) 构建并启动（在仓库根执行；该 compose 的 build.context 是 ..），等 STATUS 出现 (healthy)
docker compose -f examples/docker-compose.nas.yml up -d --build
docker compose -f examples/docker-compose.nas.yml ps
```

> 长期部署走根目录 `docker-compose.yml` + `.env`（只差「示例值走插值」这一点），见 DEPLOYMENT §4.2–§4.3。
> entrypoint **只**对 `/data` 与应用根做 `chown`；发现 `/data` 与媒体挂载是同一个挂载时**直接拒绝启动**（否则 `chown -R` 会顺硬链接改到源文件属主）。

### 启动后必须过的三条硬校验

```sh
# ① 健康：必须 status=ok 且 checks 六项全 true。该端点永远返回 200，只看状态码等于没检查
curl -s http://127.0.0.1:18081/api/system/health
# 期望 {"status":"ok","checks":{"db":true,"data_dir":true,"downloads_readable":true,"library_writable":true,"processors":true,"config":true}}
# ② 运行身份不是 root
docker exec media-ingest sh -c 'id'          # 期望 uid=1000 gid=1001，即你的 <PUID>:<PGID>
# ③ 两条挂载互相独立（挂载 ID 不同 ⇒ chown -R /data 碰不到媒体）
docker exec media-ingest sh -c 'grep -E " /vol1/1000 | /data " /proc/self/mountinfo | cut -d" " -f1,5'
#    期望两行，且两行第 1 个字段（挂载 ID）不同
```

> ③ 只证明「两条挂载独立」，**不证明硬链接能建**（`st_dev` 相同不代表 `link()` 成功，例如 btrfs 同挂载跨子卷仍可能 `EXDEV`）。真正的判据是下面的实测探针：只读源文件，只在目标根下建一个临时名并立刻删除，源目录**零写入**。

```sh
docker exec media-ingest sh -c 'w=$(mktemp -d /vol1/1000/Library/.mlm-probe.XXXXXX) || exit 1
  f=$(find /vol1/1000/Download/qbittorrent -maxdepth 3 -type f -print -quit)
  [ -n "$f" ] || { echo "源里没有可链的样本文件（跳过）"; rm -rf "$w"; exit 0; }
  ln "$f" "$w/probe.link" && echo "实测 link() 成功（同一挂载）" || echo "实测 link() 失败 = 跨挂载"
  rm -rf "$w"'
# 期望输出 "实测 link() 成功（同一挂载）"；报 "Invalid cross-device link" 就是跨挂载，先别往下走
```

**① 不是 `"ok"` 时先看是哪一项 `false`：**

```sh
docker inspect --format '{{json .State.Health}}' media-ingest
# Log[].Output 里有 healthcheck 明细，例如 checks.config = False → /data/config 对容器用户不可写
#   checks.library_writable = False → 「允许的目标根目录」不存在/不可写；checks.downloads_readable = False → 源根不存在/不可读
```

> ⚠️ **沿用旧数据库时最容易踩的一条**：v1 存的「允许的源/目标根目录」是 `/downloads`、`/library`。启动时会做一次**有界自动迁移**：
> 存量值**逐字等于** v1 默认单值时才改写成**当前默认值** —— 源根 `/vol1/1000/Download`、目标根 `/vol1/1000/Library`。所以：
> - 默认源根是**整个下载树** `/vol1/1000/Download`，**不是** `.../Download/qbittorrent`（两者同挂载，都能用）；
> - 迁移**只看值不看路径**；手工填过别的路径（不等于 v1 单值）**不会**被迁移，去「设置」页或 `PUT /api/settings` 的 `allowed_source_roots` / `allowed_target_roots` 改；改 `.env` / compose 的 `DEFAULT_SOURCE_ROOT` / `DEFAULT_TARGET_ROOT` 对**已存在的 DB 不生效**（只是首次建库的初值）。细节见 [`DEPLOYMENT.md`](../docs/guides/DEPLOYMENT.md) §6.5；启动即 `degraded`（多半就是旧「允许根目录」还在）的排查见 [`TROUBLESHOOTING.md`](../docs/guides/TROUBLESHOOTING.md) §2。

## 2. 建一个「番剧库」

- 字段含义（类型、三态、默认值）→ [`USER_GUIDE.md`](../docs/guides/USER_GUIDE.md) §4.0；处理器与命名规则 → [`PROCESSORS.md`](../docs/contracts/PROCESSORS.md) §3。
- **「类型」只是一个自由文本标签**，不校验、不影响行为；真正决定行为的是**处理器**（`processor_id`）：番剧库习惯 `bangumi`、电影库 `movie`、其它库 `hardlink_only`。

**方式一：网页上点**（媒体库页 → 新建）

| 字段 | 本例填的值 | 说明 |
|---|---|---|
| 名称 / 类型 | `番剧库` / `番剧` | 类型随便填，只是标签 |
| 处理器 | `bangumi` | **真正决定行为**；会重命名 |
| 源目录 | `/vol1/1000/Download/qbittorrent/Bangumi` | 必须是**宿主路径**（容器路径 = 宿主路径） |
| 目标根路径 | `/vol1/1000/Library/Bangumi` | 同上；必须与源目录同一挂载 |
| 匹配分类 / 标签 | `Anime` / 留空 | 标签空 = 不按标签过滤 |
| 匹配路径前缀 | `/vol1/1000/Download/qbittorrent/Bangumi` | 回调的 `content_path` 须以它开头 |
| 孤儿清理 | ✅ | 破坏性开关，先读 [`USER_GUIDE.md`](../docs/guides/USER_GUIDE.md) §8.5 |
| 权限模式 | ⬜ 不勾 | 默认关闭；开启会把目标改成 `0777`，并因硬链接共享 inode 而**连带改到源文件权限位** |

**方式二：用 API**（等价，便于复现/脚本化）

```sh
BASE=http://127.0.0.1:18081
JAR=/tmp/mlm-cookies.txt
curl -sS -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
     -c "$JAR" -d '{"username":"admin","password":"你的密码"}'
# 建库 → 201，响应里的 "id" 记为 <LIB_ID>。processor_id 是处理器引用的规范字段（GET /api/processors 列出全部 id）；type 只是标签。
curl -sS -X POST "$BASE/api/libraries" -H 'Content-Type: application/json' -b "$JAR" -d '{
  "name": "番剧库", "type": "番剧", "processor_id": "bangumi", "enabled": true,
  "source_dir": "/vol1/1000/Download/qbittorrent/Bangumi",
  "target_dir": "/vol1/1000/Library/Bangumi",
  "match_categories": ["Anime"], "match_tags": [],
  "match_path_prefixes": ["/vol1/1000/Download/qbittorrent/Bangumi"],
  "orphan_cleanup": true }'

# 建完先验证：对源/目标做真实 link 实测（目标根下建临时名再删，源目录零写入）
curl -sS -X POST "$BASE/api/libraries/<LIB_ID>/validate" -b "$JAR"
# same_filesystem 三态：true（可建）/ false（明确跨挂载）/ null（源里没有可实证的文件）
```

> 示例**故意不传** `permission_mode`（默认关闭的显式开关，理由见上表），也**不传** `log_file`：留空时按库自动分配唯一日志名（`bangumicache.log`、`bangumicache-2.log` …）；显式传一个已被其它库占用的名字会 `422`。

> **三条匹配条件（分类 / 标签 / 路径前缀）是 AND**：填了几项就必须**同时**满足几项（单个条件内部的列表才是「任一命中即可」）。
> 三条全空的库**永远不命中**，回调返回 `matched:0` 并在 `ignored_details[].reason` 里说明 —— 刻意如此，免得一个没配条件的库把整个下载树的完成事件全吞进来。细则见 [`USER_GUIDE.md`](../docs/guides/USER_GUIDE.md) §4.4。

## 3. 造一个假下载，验证硬链接真的建起来了

不用等真实下载，手工造出与 qBittorrent 一致的目录形状（应用只处理源目录下的**一级**文件夹）：

```sh
sudo mkdir -p '/vol1/1000/Download/qbittorrent/Bangumi/葬送的芙莉莲'
sudo dd if=/dev/urandom of='/vol1/1000/Download/qbittorrent/Bangumi/葬送的芙莉莲/第1集.mkv' bs=1M count=8
```

触发处理（沿用 §2 的 `$BASE` / `$JAR`）：

```sh
# folder 必须是一级文件夹名，含 / 会被 400 拒绝
curl -sS -X POST "$BASE/api/libraries/<LIB_ID>/run" -H 'Content-Type: application/json' \
     -b "$JAR" -d '{"scope":{"mode":"folder","folder":"葬送的芙莉莲"},"force":false}'
# → 202 + 任务对象，含 "id"（记为 <TASK_ID>）
curl -sS "$BASE/api/tasks/<TASK_ID>" -b "$JAR"
curl -sS "$BASE/api/tasks/<TASK_ID>/logs?after_id=0&limit=200" -b "$JAR"
```

**判据看任务 `result` 的计数**：`linked > 0` 且 `link_failed == 0` 才算成功；`link_failed > 0` 且 `samples` 里是 `Invalid cross-device link` ⇒ 源与目标跨挂载，回 §1 的探针。

**验收：inode 相同才是真硬链接**（复制会得到不同 inode）

```sh
stat -c '%d %i %h %n' \
  '/vol1/1000/Download/qbittorrent/Bangumi/葬送的芙莉莲/第1集.mkv' \
  '/vol1/1000/Library/Bangumi/葬送的芙莉莲/葬送的芙莉莲 S01E01.mkv'
# 期望：两行 %d（设备号）与 %i（inode）完全相同；第二行 %h（链接数）= 2
# ⚠️ %d 相同不能证明 link() 一定成功（跨挂载时 st_dev 也可能相同），权威判据是 result.linked > 0
```

> 只想看硬链接、不看重命名？把处理器换成 `hardlink_only`（类型随便填），媒体库里保留原文件名，`stat` 比对更直观；重命名结果与规则对照见 [`COMPATIBILITY.md`](../docs/contracts/COMPATIBILITY.md) §3.1、[`PROCESSORS.md`](../docs/contracts/PROCESSORS.md) §3.1。

## 4. 接 qBittorrent（下载完成自动入库）

qBittorrent WebUI（`http://127.0.0.1:18080`）→ 设置 → 下载 → **运行外部程序**，粘进去「设置 → 回调」页复制的那条命令。

完整命令、7 个 `--data-urlencode` 参数、token 从哪拿、为什么不能传 `%K`、为什么必须一行，全在 [`qbittorrent-autorun.md`](qbittorrent-autorun.md)（本文不重复，避免两处命令漂移）。
传进来的 `content_path` 必须是**宿主路径**且以 §2 的「匹配路径前缀」开头 —— 这正是「容器路径 = 宿主路径」的原因。

## 5. 收尾：静态自验、卸载重来

```sh
sh examples/validate-deployment-assets.sh .
# 判据：末尾汇总里 FAIL 计数为 0（PASS 条数随版本增长，别记死数字）。
# 它只做静态检查，不构建镜像、不起容器；镜像可构建、容器 healthy、硬链接真建立仍须真机验收，即 §1 与 §3。

docker compose -f examples/docker-compose.nas.yml down    # 停容器，媒体库里的硬链接不动
# 想真正「重来」：再删数据目录（数据库/密钥/配置/日志都在里面）
# sudo rm -rf /vol1/1000/Docker/media-ingest/data
# 删硬链接不删数据（只是少一个名字）；但删掉源里的原文件会让硬链接变成孤儿 —— 内容还在，直到最后一个链接被删。
```

下一站：[`USER_GUIDE.md`](../docs/guides/USER_GUIDE.md) §6–§7 学跑任务与看结果；出问题先走 [`TROUBLESHOOTING.md`](../docs/guides/TROUBLESHOOTING.md) §0 五分钟诊断。
