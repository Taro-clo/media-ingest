# qBittorrent → 媒体入库整理：下载完成自动触发

这份文档只讲一件事：**让 qBittorrent 在某个种子下载完成后，自动通知本应用去建硬链接**。

配置完的效果：qBittorrent 一完成种子 → 本应用立刻收到回调 → 命中匹配的媒体库 → 自动建硬链接
（需要时再由规则引擎 `app/processors/engine.py` 按处理器配置重命名）→ WebUI 的任务列表里能看到这次执行。

> 全文的命令与路径都按**示例值**给出（示例平台为飞牛 fnOS NAS，应用端口 18081，
> qBittorrent WebUI 端口 18080）。如果你的端口不同，把 `18081` 换成你的即可。
>
> 本应用**容器自包含**：应用只执行自己的原生实现（`app/processors/hardlink.py` 建硬链接、
> `app/processors/engine.py` 按配置重命名），不需要宿主提供任何额外脚本或目录，
> 你也不需要为此做任何权限准备。
>
> ⚠️ **回调路径的含义**：本应用容器里的路径 = **宿主路径**
> （源与目标共用一条共同父挂载 `/vol1/1000`，因为跨挂载的 `link()` 一律 `EXDEV`）。
> 所以回调传进来的内容路径应当是 `/vol1/1000/Download/qbittorrent/...` 这种**宿主路径**；
> 而 qBittorrent 容器里通常看到的是 `/downloads/...`。这一点**必须处理**，见 §0 第 2 条
> 与 §4。

---

## 0. 前置条件（三条都满足再往下做）

| # | 条件 | 怎么确认 |
|---|------|----------|
| 1 | 本应用已启动且健康 | `curl -s http://127.0.0.1:18081/api/system/health` 返回 `{"status":"ok",...}` |
| 2 | 回调里的内容路径 = **本应用容器能看到的路径**（即宿主路径） | 本应用只挂了 `/vol1/1000`（容器路径 = 宿主路径），所以 `%F` 必须是 `/vol1/1000/Download/qbittorrent/...`。若 qBittorrent 里是 `/downloads/...`，按 §4 的 (a)/(b) 两种办法之一处理 |
| 3 | qBittorrent 容器里有 `curl` | `docker exec qbittorrent sh -c 'command -v curl'` 有输出即可（linuxserver 镜像自带） |

第 2 条是**整条链路能工作的前提**，见 §4「回调路径怎么对齐」。

---

## 1. 先去本应用里复制回调命令（不要手抄）

1. 浏览器打开本应用：`http://<NAS地址>:18081`
2. 左侧菜单 → **设置**
3. 找到 **「qBittorrent 回调」** 卡片
4. 点 **「复制回调命令」**

卡片里会显示两个东西：

- **Hook Token** —— 一长串随机字符串，点「复制 Token」可单独复制
- **回调命令** —— 一行完整可用的 `curl` 命令，里面已经带上了你的 token

命令长这样（`<HOOK_TOKEN>` 处是真实 token）：

```sh
curl -s -X POST "http://192.168.1.100:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" --data-urlencode "name=%N" --data-urlencode "hash=%I" --data-urlencode "category=%L" --data-urlencode "tags=%G" --data-urlencode "content_path=%F" --data-urlencode "save_path=%D" --data-urlencode "root_path=%R"
```

> **关于 `kind`（`%K`）**：`kind` 是**可选的高级覆盖项**，用于手工指定
> `hardlink` / `rename` / `rename_hardlink` 来强行覆盖任务类型；不传时任务类型由**媒体库
> 选择的处理器**推导（默认行为，也是推荐用法），因此上面这条标准命令里**不传**。
> 特别注意：qBittorrent 的 `%K` 是 **Torrent ID**（种子在 qBittorrent 里的内部编号），
> **不是**任务类型 —— 千万不要拿 `%K` 去填 `kind` 参数。
>
> 命令与参数的**唯一真源**是 `docs/contracts/ARCHITECTURE.md` §8（`README.md` §5 是它的摘要）。
> 本应用设置页里「复制回调命令」给出的就是同一形式（不含 `kind`）。

把 `http://192.168.1.100:18081` 换成**你实际访问本应用的地址**（用 NAS 的局域网 IP，
不要写 `127.0.0.1` —— qBittorrent 是在自己的容器里执行这条命令，`127.0.0.1` 指向它自己）。
本应用与 qBittorrent 都在宿主网络上，直接用 NAS IP 即可。

> ⚠️ **token 等同于写权限**：拿到它就能让本应用执行任务。不要贴到截图、聊天或 issue 里。
> 怀疑泄露时，在设置页点「重新生成 Token」，然后回到 qBittorrent 更新这条命令。

---

## 2. 在 qBittorrent 里粘贴并启用

按以下顺序点（qBittorrent WebUI 5.0.x 的菜单名，已对照官方源码核实）：

1. 打开 qBittorrent WebUI：`http://<NAS地址>:18080`
2. 右上角 **⚙ 齿轮图标**（或菜单 **Tools / 工具 → Options / 选项**）
3. 左侧选中 **「Downloads / 下载」** 这一页
4. 往下滚到 **「Run external program / 运行外部程序」** 分组
5. 勾选 **「Run external program on torrent finished / 种子完成时运行外部程序」**
   - ⚠️ 同一分组里还有一个 **「Run external program on torrent added / 种子添加时运行外部程序」**，
     **不要**勾它。我们要的是"下载完成"，不是"刚添加"（那时文件还没下完）。
6. 把第 1 步复制的**整行命令**粘进它右边的输入框
7. 点窗口底部的 **「Save / 保存」**

> WebUI 是英文界面时，第 5 步那两个勾选框字面就是
> `Run external program on torrent added` 与 `Run external program on torrent finished`，
> 认准 **finished**。

---

## 3. 占位符分别是什么（命令里用到 7 个）

qBittorrent 会在执行命令前，把 `%X` 形式的占位符替换成**这个种子的真实值**。
下表取自 qBittorrent 5.0.4 官方源码（`src/webui/www/private/views/preferences.html`，
参数列表与软件界面里显示的完全一致）：

| 占位符 | qBittorrent 官方含义 | 本应用拿它做什么 |
|--------|----------------------|------------------|
| `%N` | Torrent name —— 种子名 | 记进任务详情，便于回溯"这次是谁触发的" |
| `%I` | Info hash v1 —— 种子 v1 信息哈希 | 同上（任务详情里的种子哈希列） |
| `%L` | Category —— 分类 | **匹配条件之一**：库若填了「匹配分类」，就比对这个值 |
| `%G` | Tags (separated by comma) —— 标签，逗号分隔 | **匹配条件之一**：与库的「匹配标签」取交集（大小写不敏感，中英文逗号/分号/空格都当分隔符） |
| `%F` | Content path (same as root path for multifile torrent) —— 内容路径 | **最关键**：本应用用它算出"这次该处理哪个一级文件夹"，也因此它是「匹配路径前缀」的比较对象 |
| `%D` | Save path —— 保存路径 | 记进任务详情 |
| `%R` | Root path (first torrent subdirectory path) —— 种子子目录根 | 记进任务详情 |

**关于 `%K`（务必知道它不是什么）**：官方含义是 **Torrent ID** —— 种子在 qBittorrent 里的
内部编号，**与任务类型毫无关系**。本应用的 `kind` 参数只承认
`hardlink` / `link` / `rename` / `rename_hardlink` 这几个**语义**取值，而 `%K` 是个数字，
不在白名单里，因此即便传了也会被安全忽略。**标准命令里干脆不传它。**

为什么不传就够：任务类型本来就该由**该媒体库配置的处理器**推导 ——
处理器是 `bangumi`/`movie` → 建硬链接 + 重命名，`hardlink_only` → 只建硬链接。

这么做是**故意的**：qBittorrent 完成事件只给"种子"这一层信息，它不包含"A 站抓的用 A 规则、
B 站抓的用 B 规则"这种业务判断；靠种子 ID 猜处理器只会引入难以排查的错配。
所以 `kind` 只作为**可选的高级覆盖项**保留（手工传 `hardlink`/`rename`/`rename_hardlink`
可强行覆盖），默认链路完全不依赖它。

> 另外三个官方占位符本项目没有使用：`%C`（文件数）、`%Z`（种子字节数）、
> `%T`（当前 tracker）。想加可以自己追加 `--data-urlencode "x=%C"`，
> 但本应用不认识它们，加了也只是被忽略。

---

## 4. 这条命令为什么是"可复制粘贴"的，以及回调路径怎么对齐

三件事让它开箱即用：

1. **参数走 `--data-urlencode`**：种子名里带空格、中文、`&`、`?`、`#` 都不会把命令搞崩，
   curl 会正确做 URL 编码。用 `?name=%N` 这种查询串拼接就会在特殊字符上翻车。
2. **token 放在查询串里**：`?token=...` 是唯一的认证方式，不需要 Cookie、不需要登录。
3. **回调路径必须与"本应用容器里能看到的路径"一致**（重点）：

   本应用只挂了一条媒体共同父挂载，并且**容器内路径 = 宿主路径**：
   `-v /vol1/1000:/vol1/1000`（为什么必须单挂：跨挂载的 `link()` 一律 `EXDEV`）。
   因此回调里的内容路径必须是 `/vol1/1000/Download/qbittorrent/某番剧/...` 这种**宿主路径**。

   而 qBittorrent 容器里看到的通常还是 `/downloads/某番剧/...`（它自己的挂载方式没变），
   两者**不再自动相等** —— 需要在下面二选一：

   ```sh
   # (a) 推荐：让 qBittorrent 也按"容器路径 = 宿主路径"挂（与本源同一原则）。
   #     这样 %F 天生就是宿主路径，零转换：
   #       -v /vol1/1000/Download/qbittorrent:/vol1/1000/Download/qbittorrent
   #     注意：改挂载后 qBittorrent 里已存在的任务路径也会跟着变，建议先停 qB 再改。

   # (b) 保留 qB 现有的 /downloads 挂法，在触发时把前缀换成宿主路径。
   #     做法是在外部程序里套一层脚本（qB 的"运行外部程序"支持带参数的脚本）：
   #       /config/notify-mlm.sh "%F" "%N" "%I" "%L" "%G" "%D" "%R"
   #     脚本内容（P 是 qB 传来的内容路径，换成宿主前缀后再发给本应用）：
   #       P="$1"; P="/vol1/1000/Download/qbittorrent${P#/downloads}"
   #       curl -s -X POST "http://192.168.1.100:18081/api/hooks/qbittorrent?token=<TOKEN>" \
   #         --data-urlencode "name=$2" --data-urlencode "hash=$3" \
   #         --data-urlencode "category=$4" --data-urlencode "tags=$5" \
   #         --data-urlencode "content_path=$P" --data-urlencode "save_path=$P" \
   #         --data-urlencode "root_path=$P"
   ```

   > 传一个本应用容器里**不存在**的路径（比如 `/downloads/...`）会怎样：
   > 该路径不在设置里的"允许的源根目录"内 ⇒ 所有库都不命中，返回 `matched:0`，
   > `ignored_details[].reason` 会说明原因（这比"悄无声息地什么都不做"友好，但仍然是失败）。
   > 反过来，手填宿主路径是**允许**的（库里存的就是宿主路径）。

---

## 5. 验证：三步确认真的通了

**第 1 步 · 手工模拟一次回调**（把 `<HOOK_TOKEN>` 换成真 token；`%N` 等替换成任意测试值）：

```sh
curl -s -X POST "http://127.0.0.1:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" \
  --data-urlencode "name=test-torrent" \
  --data-urlencode "hash=0123456789abcdef0123456789abcdef01234567" \
  --data-urlencode "category=Anime" \
  --data-urlencode "tags=anime" \
  --data-urlencode "content_path=/vol1/1000/Download/qbittorrent/测试番剧" \
  --data-urlencode "save_path=/vol1/1000/Download/qbittorrent" \
  --data-urlencode "root_path=/vol1/1000/Download/qbittorrent/测试番剧"
```

返回示例（**没命中**任何库时）：

```json
{"matched":0,"tasks":[],"ignored":["番剧库"],"ignored_details":[{"library_id":1,"name":"番剧库","reason":"分类 Anime 不在该库的分类列表里。"}],"torrent":{"name":"test-torrent","hash":"0123..."}}
```

**这份返回是排查利器**：`ignored_details[].reason` 会逐条中文说明**每个库为什么没命中**
（分类不符 / 标签无交集 / 路径前缀不符 / 该库压根没配匹配条件）。照它改库配置即可。

**第 2 步 · 看任务列表**：命中后返回 `matched:1, tasks:[123]`，到 WebUI「任务」页就能看到这次执行。

**第 3 步 · 真实下载一次**：找个已下载完的种子，右键 → **Force recheck / 强制校验**
（校验完成会触发一次"完成"事件），然后回本应用看任务列表。

---

## 6. 常见问题

| 症状 | 原因与处理 |
|------|-----------|
| 返回 `{"detail":"回调 token 不正确。..."}` | token 抄漏/过期。回设置页重新复制整行命令；若刚点过「重新生成 Token」，必须同步更新 qBittorrent 里的命令 |
| 返回 `matched:0`，且 `ignored_details` 说「该库没有配置任何匹配条件」 | 这是**故意**的安全默认：三条匹配条件（分类/标签/路径前缀）全空的库永远不命中，否则它会抓住每一个完成的种子。去库里至少填一条 |
| `curl` 报 `Could not resolve host` / `Connection refused` | ①命令里写的是 `127.0.0.1`（应改成 NAS 的局域网 IP）；②应用没起（`docker compose ps` 看 STATUS 是否 `(healthy)`） |
| 回调通了但没建硬链接，任务报 `跨文件系统错误 … EXDEV` | 源与目标落在了**不同挂载**里（内核 `link()` 比较挂载身份，与 `st_dev`/`fsid`/子卷无关）。本应用**绝不回退为复制**。修法：把 compose 的 volumes 改成**单一共同父挂载** `/vol1/1000:/vol1/1000`（容器路径 = 宿主路径），并把设置页的"允许的源/目标根目录"改成宿主真实路径 |
| 回调 `matched:0`，`ignored_details` 说路径前缀/源根不符 | 十有八九是 `%F` 还是 `/downloads/...`（qBittorrent 容器内路径），而本应用只认宿主路径 `/vol1/1000/...`。见 §4 的 (a)/(b) |
| 任务报「无法缩小范围，本次按全量执行」 | 单文件种子：`%F` 是文件本身、直接躺在源目录下，而现行硬链接实现只遍历**一级文件夹**，这是现行实现的既有边界，不是本应用的问题 |
| 想确认 qBittorrent 到底执行了什么 | `docker logs qbittorrent --tail=50`；命令用 `-s` 静默，出错时 curl 的报错会出现在 qBittorrent 容器日志里 |

---

## 7. 不想用「运行外部程序」？还有两条路

| 方式 | 何时用 | 怎么做 |
|------|--------|--------|
| **定时扫描** | 不想配回调，或担心漏触发 | 设置页开启「扫描」，应用会按间隔自己扫源目录（无需 qBittorrent 配合） |
| **手动执行** | 补跑、排查 | 媒体库页点「执行」，或在 WebUI 里选范围（全量 / 某个一级文件夹）后执行 |

三者的任务都进同一个队列，**全局串行**执行（现行硬链接实现对日志文件做"读-改-写整份"，
并发会互相覆盖），所以不用担心手动与回调撞车。
