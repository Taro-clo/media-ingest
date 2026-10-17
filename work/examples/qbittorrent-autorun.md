# qBittorrent 下载完成后自动触发（回调配置全流程）

> 本文是回调的**唯一完整实操副本**：完整命令、7 个参数、token 从哪拿、`%K` 陷阱、「必须一行」都在这里。
> 命令为什么长这样（契约）→ [`ARCHITECTURE.md`](../docs/contracts/ARCHITECTURE.md) §8；部署流程 → [`DEPLOYMENT.md`](../docs/guides/DEPLOYMENT.md) §5；坏了怎么查 → [`TROUBLESHOOTING.md`](../docs/guides/TROUBLESHOOTING.md) 的「回调没触发」「回调 401」两节。

| 占位 | 示例值 | 说明 |
|---|---|---|
| `<NAS_IP>` | `192.168.1.100` | NAS 的局域网 IP。qBittorrent 在自己的容器里执行命令，**不能用 `127.0.0.1`**（那指向 qB 自己） |
| `<应用端口>` | `18081` | 本应用端口。端口是**发布到宿主**的（compose 的 `ports`），不是 host 网络，所以走宿主 IP + 端口 |
| `<媒体父目录>` | `/vol1/1000` | 下载目录与媒体库的共同父目录 |
| `<下载目录>` | `/vol1/1000/Download/qbittorrent` | qB 的下载目录（回调传进来的路径都以它开头） |

## 0. 三条前置条件

| 条件 | 怎么确认 |
|---|---|
| 容器已起来且 `(healthy)` | `docker compose ps`；`curl -s http://127.0.0.1:18081/api/system/health` 里 `status` 是 `"ok"` |
| 已有一个**能命中**的媒体库 | 库的分类 / 标签 / 路径前缀**同时**对得上你的种子（三条是 AND）。没库或没条件 ⇒ 回调通了也只会 `matched:0` |
| 容器路径 = 宿主路径 | 回调传的是**宿主路径**；两者不一致时先看 §4 |

## 1. 先拿到回调命令（不要手抄）

1. 浏览器打开 `http://<NAS_IP>:18081`，用管理员登录；
2. 进 **设置**页，找 **回调 / Hook** 一张卡；
3. 卡上有 **hook token** 和一条**完整可复制的回调命令**，点「复制」；
4. 这条命令就是 §2 要粘进 qBittorrent 的东西。

命令长这样（`<HOOK_TOKEN>` 是设置页给你的真 token，**必须是一行、不换行、不加多余空格**）：

```sh
curl -s -X POST "http://<NAS_IP>:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" --data-urlencode "name=%N" --data-urlencode "hash=%I" --data-urlencode "category=%L" --data-urlencode "tags=%G" --data-urlencode "content_path=%F" --data-urlencode "save_path=%D" --data-urlencode "root_path=%R"
```

- **7 个 `--data-urlencode` 一个都不能少、顺序照抄**。多传别的占位符会被静默忽略（服务端只取白名单里的 8 个键）。
- `--data-urlencode` 不是可选的：种子名和路径含空格/中文，写成 `-d "name=%N"` 会坏掉。
- **token 等同写权限**（拿到就能触发任务），别贴到论坛/截图/聊天群；怀疑泄露就在设置页**重新生成** ——
  旧 token 立即失效，**必须马上把新命令粘回 §2 覆盖旧的**，否则 qB 侧会开始 401。
- **token 不在 `.env` 里**：它由应用首次启动随机生成，存在 `/data` 的数据库里。等价取法：`GET /api/settings` 的 `hook_token` 字段
  （`curl -s -b cookies.txt http://127.0.0.1:18081/api/settings`）。
- 想批量/脚本化，也可以用 `PUT /api/settings` 换 token，但别忘了同样要更新 qB。

## 2. 粘进 qBittorrent（七步）

1. 打开 qBittorrent WebUI（`http://<NAS_IP>:18080`），登录；
2. 齿轮图标 → **设置**；
3. 左侧选 **下载**；
4. 找到 **运行外部程序** 输入框；
5. 把 §1 复制的那一整行**原样粘进去**；
6. 确认「运行外部程序」这一项是启用状态（不同版本叫法略有差异，通常在同一页）；
7. 保存。

> ⚠️ **不要勾** 「Torrent added（添加种子时运行）」这类选项 —— 回调是给**下载完成**用的。
> 勾了会在还没下完时就触发，任务跑空或反复重试。

## 3. 参数与占位符

| 占位符 | 含义 | 本应用拿它做什么 |
|---|---|---|
| `%N` | 种子名称 | 日志与响应回显，便于对账 |
| `%I` | 种子哈希 | 响应 `torrent.hash`，用于定位是哪一条 |
| `%L` | 分类 | 与库的「匹配分类」比对（AND 条件之一） |
| `%G` | 标签（逗号分隔） | 与库的「匹配标签」求交集（AND 条件之一） |
| `%F` | 内容路径 | 用来定位**一级文件夹**，并与库的「匹配路径前缀」比对 |
| `%D` | 保存路径 | 仅记账/诊断 |
| `%R` | 根路径 | 仅记账/诊断 |

**关于 `%K`：它是 Torrent ID（种子内部数字 ID），不是任务类型。** 不要拿 `%K` 去填 `kind` ——
任务类型由库的处理器推导（`bangumi`/`movie` → `rename_hardlink`，其余 → `hardlink`），标准命令里**一律不传** `kind`。
老版本 qB 若缺某个占位符，就删掉对应的那个 `--data-urlencode`（应用会把缺的当作没传），不要自己编值。

> 参数名、顺序、`kind` 的处理规则以 ARCHITECTURE §8 为准；匹配细则见 [`USER_GUIDE.md`](../docs/guides/USER_GUIDE.md) §4.4。

## 4. 路径对不齐时的三种解法

回调把 qB **眼里**的路径原样传过来。本应用要求它是**宿主路径**，且与库配置的前缀一致。

**(a) 单一共同父挂载（推荐，也是默认形态）** —— qB 与本应用都把 `<媒体父目录>` 挂成同一个容器路径，
于是 `%F` 既是 qB 的路径也是本应用的路径，零转换。为什么必须这样：[`COMPATIBILITY.md`](../docs/contracts/COMPATIBILITY.md) §2、[`DEPLOYMENT.md`](../docs/guides/DEPLOYMENT.md) §2。

**(b) 让 qB 也按宿主路径同构挂载** —— 把 qB 的下载目录从 `/downloads` 改成 `/vol1/1000/Download/qbittorrent`，
或者给 qB 加一条 `-v /vol1/1000:/vol1/1000`。之后回调的 `%F` 天然就是宿主路径。

**(c) 在回调里改写前缀（次选）** —— 前提仍是两端**同一挂载**，否则改完照样 `EXDEV`。
把改写逻辑放进一个脚本：qB 里调用脚本、把占位符当参数传进去（脚本**内部**写的 `%F` 不会被 qB 替换，别直接照抄 §1 那行）：

```sh
#!/bin/sh
# 存成 /vol1/1000/scripts/hook.sh 并 chmod +x。
# qB「运行外部程序」里写：/vol1/1000/scripts/hook.sh "%F" "%N" "%I" "%L" "%G" "%D" "%R"
P="/vol1/1000/Download/qbittorrent${1#/downloads}"
curl -s -X POST "http://<NAS_IP>:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" \
  --data-urlencode "name=$2" --data-urlencode "hash=$3" --data-urlencode "category=$4" \
  --data-urlencode "tags=$5" --data-urlencode "content_path=$P" \
  --data-urlencode "save_path=$6" --data-urlencode "root_path=$7"
```

## 5. 验证它真的通了（三步）

**（1）手动模拟一次回调**（最可靠，能立刻区分「链路问题」和「配置问题」）

```sh
# 在 NAS 宿主上执行；token 换成你的，category / content_path 换成你库里能匹配上的值
curl -s -X POST "http://127.0.0.1:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" \
  --data-urlencode "name=测试种子" \
  --data-urlencode "hash=0000000000000000000000000000000000000000" \
  --data-urlencode "category=Anime" \
  --data-urlencode "tags=" \
  --data-urlencode "content_path=/vol1/1000/Download/qbittorrent/Bangumi/某作品" \
  --data-urlencode "save_path=/vol1/1000/Download/qbittorrent/Bangumi" \
  --data-urlencode "root_path=/vol1/1000/Download/qbittorrent"
```

| 返回 | 含义 |
|---|---|
| `{"matched":1,"tasks":[43],"ignored":[]}` | ✅ 命中并入队，去任务页看 |
| `{"matched":0,"tasks":[],"ignored":[...],"ignored_details":[{"library_id":1,"name":"番剧库","reason":"分类 Anime 不在该库的分类列表里。"}]}` | 链路通了但**没命中**；按 `reason` 改库的匹配条件 |
| `401` + "回调 token 不正确…" | token 错/没带上，重新复制完整命令（含 `token=`） |
| 连接被拒绝 | 应用没起或端口不对，看 `docker compose ps` |

**（2）从 qB 容器内部测可达性**（等价于 qB 自己发起的调用）

```sh
docker exec qbittorrent curl -s -X POST "http://<NAS_IP>:18081/api/system/health"
# 有 JSON 输出 = qB 容器到应用网络是通的。连不上先确认 <NAS_IP> 是宿主真实局域网 IP（不是 127.0.0.1）
```

**（3）真下一个种子**：完成后 `docker compose logs --tail=100 media-ingest` 应出现
`qbittorrent 回调：name=… category=… content_path=…`，任务页同时多一条记录。

> 手动模拟只证明「链路通」，不代表 qB 真的会调用 —— 那取决于 §2 的第 6、7 步。
> `Force recheck` 或重新校验完成时也会再触发一次（带 `content_path`），所以同一作品重复通知是正常的，幂等靠日志记账兜住。

## 6. 回调漏了怎么办

- **扫描补偿（推荐）**：设置页打开 `scan_enabled`，`scan_interval_seconds` 默认 900 秒；它会扫所有启用库的源目录，把「没记录过或体积变了」的一级文件夹补成任务。说明见 [`USER_GUIDE.md`](../docs/guides/USER_GUIDE.md) §8.4。
- **手动触发一次扫描**（不看开关，总是执行）：`curl -s -X POST "http://<NAS_IP>:18081/api/hooks/scan?token=<HOOK_TOKEN>"`。
- **手动跑某个文件夹**：库详情页的「运行」，等价于 `POST /api/libraries/<id>/run`（`folder` 必须是一级文件夹名）。
- **为什么回调只是入队**：全局单工作线程串行执行，避免两个任务同时改同一目录；突发几十个种子会排队，不是卡死。

回调没触发、401、端口不通的排查清单：[`TROUBLESHOOTING.md`](../docs/guides/TROUBLESHOOTING.md) 里「回调没触发」「回调 401」「端口被占用」三节（按小标题找，别按编号找 —— 该文编号会随版本调整）。
