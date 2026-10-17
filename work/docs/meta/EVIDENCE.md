# 验证证据记录

> 📌 本文件收录**可复现**的证据：实测命令、数值与判据，
> 用于支撑 `docs/contracts/BASELINE.md` 的契约结论与 `docs/contracts/ARCHITECTURE.md` 的设计决策。
> 测试套件本身的结论归 `docs/contracts/COMPATIBILITY.md`（由契约文档维护）；
> 当前实现与契约以 `docs/contracts/**` 为准。

本节数值的记录环境：Windows + Python 3.14.7（Python 路径示例
`C:\Users\youruser\AppData\Local\Programs\Python\Python314\python.exe`，按你的实际安装位置替换），
**不需要** Docker，全部在系统临时目录下进行、**不触碰**任何真实媒体库路径。

---

## E3. 硬链接执行体的适配策略（`ScopedOs` 定向重跑 / 输出契约 / 容器编码）

硬链接与重命名由应用自己的代码实现：`app/processors/hardlink.py`（原生实现）与
`app/processors/engine.py`（规则引擎），由 `app/script_adapter.py` 调度。本节的四点实测构成
`app/script_adapter.py` 的设计依据，其中第 3、4 点的结论**今天仍然生效**：

1. **定向缩小扫描范围**：把模块级 `os` 换成代理对象 `ScopedOs`，仅在
   `os.path.abspath(path) == abspath(scoped_path)` 时过滤 `os.listdir` 的返回值 ⇒ 一次只处理指定的一级
   文件夹，且 `finally` 恢复后行为不变（A7）。这条今天依然成立，因为 `hardlink.py` 里的 `os.*`
   都是**调用期现取模块级 `os`**（不是把函数提到模块级），替换 `module.os` 照样能收敛扫描范围。
   契约与回归用例：`app/script_adapter.py` 的 `ScopedOs` / `_scoped_os`、`tests/test_adapter_scoped.py`。
2. `create_hard_links(source_dir, target_dir, ...)` 生成的确实是硬链接：
   `os.stat(src).st_ino == os.stat(dst).st_ino` 且 `st_nlink == 2`。
3. stdout 是**适配层解析结果的契约**，不是给人看的日志：`hardlink.py` 的每一行打印前缀
   （`已创建硬链接: ` / `创建硬链接失败: ` / `文件夹已记录且体积一致，跳过: ` /
   `Deleted orphaned hard link folder: ` / `已删除旧记录: ` / `已记录文件夹体积: `）与日志文件的行格式
   （`%(asctime)s - Source: … - Folder: … - Folder size: N`）都由 `app/script_adapter.py` 解析回来，
   改动会让结果解析失配。
4. **输出必须锁定 UTF-8**：上一条里的中文打印与中文日志行在 POSIX locale 下会触发 `UnicodeEncodeError`，
   所以容器**必须**有 `LANG=C.UTF-8` + `PYTHONUTF8=1`（A11；`docker/Dockerfile` 的 `ENV` 块已设）。

---

## E4. 部署前提（部署到目标机前逐项核对）

| 前提 | 怎么核对 / 不满足会怎样 |
|---|---|
| 源目录与库目录同一文件系统 | 硬链接要求两者在**同一个挂载**内；`st_dev` 相同只是必要不充分（见 E3 与 `docs/contracts/BASELINE.md`）。跨挂载时 `link()` 返 `EXDEV`，应用**不会**回退为复制文件 |
| 目标端口空闲 | 默认 `18081`（容器内 `PORT` 可改）；被占用时启动会失败，先确认或改端口 |
| 构建期可访问 PyPI | 构建镜像需要能访问 PyPI（或配置镜像源 / 使用离线 wheel），否则 pip 安装会失败 |
| 基础镜像可用 | 若目标机不能拉取 `python:3.11-slim`，需提前导入或改用可访问的基础镜像 |
| 非 root 运行可行 | 媒体目录需对容器运行身份（`PUID`/`PGID`，示例 `1000`/`1001`）可读写：目标库需可写、源目录需可读 |
| ⚠️ 目标库的权限位可控不等于源目录安全 | 硬链接与源文件共享 inode ⇒ 对目标侧 `chmod` 会连带改到源文件的**权限位**；`permission_mode` 因此默认关闭，开启前需二次确认（BASELINE B13） |
| 没有第二个进程动同一份日志 | 确认没有别的 cron / `/etc/cron.d` 条目 / systemd timer / 外部脚本同时读写同一份 `hard_links.log` |
| qBittorrent `[AutoRun]` | 需要 `enabled=true` 并填上回调命令，否则下载完成不会触发回调 |

---

## E5. 端到端集成验证

- **形态**：独立的接口级端到端跑法 —— 在真实 FastAPI `TestClient` + 真实磁盘上全程走 HTTP，
  与 `tests/**` **互为独立实现**，刻意不复用同一套 helper（两个独立 harness 同时绿灯才有说服力）
- **依赖与容器 pin 完全一致**：`fastapi 0.115.6` / `uvicorn 0.32.1` / `starlette 0.41.3` / `httpx 0.28.1`
- **沙箱**：`C:\e2e\{download,library,data}`（示例路径，可换成你自己的临时目录）。Windows 上容器路径
  `/e2e/download` 恰好解析为 `C:\e2e\download`，因此它**既**走真实的 `HOST_PATH_MAP` 换算，
  **又**是真实文件系统上的真硬链接
- **不碰网络、不碰任何真实媒体文件**（先整棵删除并重建 `C:\e2e\`）

### 结果

| 指标 | 值 |
|---|---|
| 断言 | **83 passed / 0 failed / 83 total**，退出码 **0**（零兼容垫片） |

覆盖 10 组：A 静态与鉴权 / B 设置与路径许可 / C 媒体库 CRUD 与预检 / D 任务执行 /
E 硬链接真实性 / F 重命名落地 / G 任务·统计·系统信息 / H 删除库 / **I 日志时间字段** / **J 密码清空通路**。

其中 I 组顺带把 SSE 的真实协议钉了下来：它是**命名事件**流 ——
`event: task`（任务快照帧）→ `event: log`（日志帧）→ `event: done`，
**首帧不是日志行**。任何"只看第一条 `data:`"的消费端都会解析错对象（探针第一版就是这么错的）；
`event: log` 的载荷与 `GET /api/tasks/{id}/logs` 的 `items` 逐字段同形，`ts` 也已本地化。

### 这条测试抓到的、单元测试抓不到的 3 个致命缺陷

（三者都已修，下面留的是症状与判据 —— 它们是"接口层才有"的缺陷样本，回归用例守在同名路径上。）

1. **`GET /api/auth/me` 必 500 ⇒ WebUI 白屏**：`app/routers/auth.py:60` 的
   `security.read_session(request)` 传了 `Request`，而 `app/security.py:90` 的签名是
   `(token: str)` ⇒ `AttributeError: 'Request' object has no attribute 'split'`；
   前端首屏因此无法判断"登录页 / 初始化向导"。
2. **口令正确时登录必 500**（口令错误反而正常返回 401）：`app/routers/auth.py:105` 执行
   `UPDATE users SET last_login_at=?`，而 `app/db.py` 的 users 表没有该列 ⇒
   `sqlite3.OperationalError: no such column: last_login_at`。
3. **所有 `Depends(security.require_user)` 路由不可用**（库 CRUD / 设置 / 统计 / 系统信息 / 任务…）：
   `app/security.py:122` 的 `def require_user(request)` **缺 `Request` 标注**，
   FastAPI 把"名叫 request、无标注"的参数当成必需**查询参数** ⇒
   `422 {"detail":[{"type":"missing","loc":["query","request"]}]}`。

外加 1 个**宿主可移植性**缺陷：`paths.normalize()` 曾直接用 `os.path.abspath`，会把容器路径
`/e2e/download/Bangumi` 变成 `C:/e2e/download/Bangumi`（Linux 上恰好是恒等变换，因此只在 Windows 暴露，
容器里测不出来）。连锁后果：executor 把宿主形态传给 `run_hardlinks` ⇒ 新写的日志行是宿主前缀
⇒ 「体积一致则跳过」的比对识不出同一个文件夹（宿主前缀 ≠ 日志里的容器前缀，字符串相等比较又对不上）
⇒ **「体积一致则跳过」完全失效**（实测 `skipped=0`），首次仍然全量重扫 —— 正是该机制要消灭的症状。
**现行做法**：`normalize()` 按输入形态分派两套语义 —— 以 `/` 开头的一律走 `posixpath.normpath`
（容器空间，绝不 `abspath`），其余（`C:/…`、`C:\…`、相对路径）才用 `os.path.abspath`（见 `app/paths.py`）。

> 教训：**单元层全绿不等于接口层可用**。单元层的 130 条断言全绿，却没有一条经过
> `Depends(require_user)`；而这类跨层权限问题只有"真发一次 HTTP"才会暴露。

### 绿灯覆盖的验收点（全部实测）

- **静态前端**：`GET /` → 200（`web/index.html`）、`/css/app.css` → 200、`/js/app.js` → 200
  ⇒ 路由注册先于 `StaticFiles(directory="web", html=True)` 挂载，前端相对路径可用；
- `/api/system/health` 免认证 200，且**没有 307/308 重定向**；
- **鉴权**：错误口令 401 / 正确口令 200 / 会话生效 / `logout` 204；
  **env 预建管理员生效**（`needs_setup=false`、`POST /api/auth/setup` → 409，即支持用环境变量初始化管理员）；
- **库 CRUD**：分页信封 `{items,total,limit,offset}`；非法 `type`/`processor` 组合被拒；
  不在 `allowed_source_roots` 内的 `source_dir` 被拒；
- `POST /api/libraries/{id}/run` → **202 + Task 对象**；`GET /api/tasks/{id}/logs?after_id=` 增量拉取；
- **真硬链接**：库侧文件与源文件的 `(st_dev, st_ino)` 相同且 `st_nlink >= 2`
  （校验**按 inode 匹配**，因为库侧文件已被重命名，按文件名比对会误判）；
- **源目录零改动**：两次执行前后，源树的 `(相对路径, size, mtime_ns, st_ino)` 快照**完全一致**；
- **重命名落地（在库侧、不在源侧）**：Bangumi `y 01.mkv → Show B S01E01.mkv`；
  Movie `Film.X.mkv → Film X.mkv`；
- **日志 `Source:` 前缀全为容器路径**（`/e2e/download/...`，无一条宿主前缀）；
  Show A 因日志体积一致被**跳过**（库侧一个文件都没建，这是「体积一致则跳过」的既定语义）、
  数据卷内已有日志副本，而**源侧那份 `hard_links.log` 字节未变**；
- `DELETE /api/libraries/{id}`：无 `confirm` → 400；有 `confirm` → `{"deleted":true,"hardlinks_kept":true}`
  且库侧已创建的硬链接**仍在**。

### 关于"假装通过"

这次跑法**关闭了全部兼容垫片**（垫片是在缺陷未修时用来让测试先跑通的临时手段，
现已从测试中移除），所以本节记录的所有绿灯都是**零垫片结果** —— 断言的是真实实现，
不是被垫片兜住的行为。

---

## E6. 前端免构建方案的验证

`web/**` 没有构建工具链（见 `docs/contracts/ARCHITECTURE.md` §1）：源码直接由浏览器加载，
因此没有打包步骤；若你机器上有 `node`（即使不在 `PATH` 上也可以用绝对路径调用），可以这样静态检查：

- **语法检查**：`node --check` 对 `.js` 按 CJS 解析会把 `import` 判为错误
  ⇒ 必须**先把每个文件复制成 `.mjs` 再 check**。当前 `web/**` 的 13 个 `.js` 模块全部通过。
- **模块图检查**：用浏览器（或 `python -m http.server`）逐个 `await import()` 全部 13 个模块，0 报错；
  `js/pages/*.js` 均导出 `render`。
- **桩数据渲染**：8 条 hash 路由（`#/dashboard`、`#/libraries`、`#/library/{id}`、`#/tasks`、`#/tasks/{id}`、
  `#/settings`、`#/login`…）全部渲染成功，0 个 `window` 错误；冒烟过程中抓到并修掉 3 个真实缺陷
  （弹窗把 HTML 当文本转义显示、弹窗跨路由残留、区块顺序注释与实际不符）。
- `web/` 最终 **16 个文件**，无临时文件残留 —— 这点是**安全要求**：`StaticFiles(directory="web", html=True)`
  把 `web/` 整棵挂在 `/`，任何 `_check.py` / `_smoke.html` 都会**不认证即可访问/下载**。

---

## E7. NAS 部署验收要点

部署目录示例 `/vol1/1000/Docker/media-ingest/`。往部分 NAS 传包**不能**用 `scp`/`sftp`
（PAM/MOTD 横幅会被协议当成文件数据，报 `Received message too long`），
可改用分块 base64 + 远端 `base64 -d`，传完校验 size 与 sha256。`.env` 在目标机上生成，
`SECRET_KEY` 可用远端 `secrets.token_hex(32)` 现算、不要经过日志，并 `chmod 600`。

| 步骤 | 验收时应看到 |
|---|---|
| `docker compose config` | exit 0、stderr 空；`18081:18081` 两侧同源；2 条 bind（媒体共同父挂载 + 数据卷） |
| `docker compose build` | exit 0 |
| `docker compose up -d` | exit 0，容器 `Up (healthy)` |
| `docker inspect .State.Health` | `{"Status":"healthy","FailingStreak":0,…}`，healthcheck 的六项 checks 全部通过（db, data_dir, downloads_readable, library_writable, processors, config） |
| `curl /api/system/health` | `{"status":"ok","checks":{…全部 true},"warnings":[],"version":…}` |

### 挂载只读性与非 root

| 容器内路径 | 宿主路径 | 模式 |
|---|---|---|
| `/vol1/1000`（媒体共同父挂载） | `/vol1/1000` | rw |
| `/data` | `/vol1/1000/Docker/media-ingest/data` | rw |

- 容器**只有这两条 bind**（媒体共同父挂载 + 数据卷，见 `docker-compose.yml` 的 `volumes`）；
- 验收时容器内 `/vol1/1000/Library`、`/data` 都应可写；
- `/proc/1/status` 的 `Uid`/`Gid` 应等于你配置的 `PUID`/`PGID`（示例 `1000`/`1001`），即业务逻辑以非 root 运行；
  注意镜像 `Config.User` 为空、降权由 entrypoint 内 `gosu` 完成，因此 `docker exec` 默认给的是
  **root shell**，要以应用身份操作用 `docker exec -u 1000:1001`；
- 同宿主上的其它容器不应受影响。

### 回调端点实证（**未创建任何任务、未触碰任何媒体文件**）

在 `qbittorrent` 容器内用 `curl` 打真实端点：

| 场景 | 结果 |
|---|---|
| 错 token | **401** |
| 无 token | **401** |
| 正确 token、无匹配媒体库 | `200 {"matched":0,…,"warnings":["当前没有任何**已启用**的媒体库，回调无处可去。"],"torrent":{"name":"AcceptanceProbe",…}}` |
| 改为 `Content-Type: application/json` | `200`，但 **`torrent.name` 为空** ⇒ 回调参数只从查询串/urlencoded 表单读，**JSON 体被静默忽略**（见 `docs/contracts/ARCHITECTURE.md` §5.0 第 9 条） |
| `/api/hooks/scan` | `200 {"matched":0,…}` |
| `tasks` 表 | **0 行** |
| `/vol1/1000/Library` | 内容与 mtime **完全未变** |

### WebUI 浏览器核验

`GET /` → 200（`web/index.html`）。用浏览器走完**首次设置向导**（创建管理员）→ 登录 →
仪表盘 / 媒体库 / 任务 / 设置四页均正常渲染，深色模式跟随系统、无 CDN 请求。

核验中抓到并修掉 **2 个只有真实浏览器渲染才能发现的缺陷**：

1. **设置页「系统信息」卡片把 HTML 当文本显示** —— `web/js/pages/settings.js` 的 `boolBadge()` /
   `kvRows()` 返回裸字符串，在 `html\`\`` 模板里被自动转义；同时 i18n 缺键 `set.system.baseline_ok`。
   修法：补 `raw(...)` + 新增 `set.system.baseline_ok`/`set.system.baseline_bad`。
2. **「有未保存的修改」徽章在 DOM 上 `hidden=true` 却始终可见** —— 根因是 CSS 而非 JS：
   `web/css/app.css` 的 `.badge { display: inline-flex }`（作者样式）盖掉了 UA 的
   `[hidden] { display: none }`。前几轮只断言了 `.hidden` 属性、没看 **computed display**，所以漏判。
   修法：基础段加全局 `[hidden] { display: none !important; }`。
   同一条规则**顺带修掉两个同类缺陷**：任务详情日志筛选不隐藏（`.log-line{display:flex}`）、
   未设密码时仍显示「清除已保存密码」（`.btn` 也有 display 规则）。

> 教训：**属性正确不等于视觉正确**；涉及 `hidden` 的断言必须查 `getComputedStyle()`。

---

## E8. 原生硬链接 / 每库命名模板 / 每库目录正则 / 全局后缀白名单的验证

本节覆盖四项能力：①硬链接由应用自己的原生实现执行、可独立运行；②每库可单独设定命名方式
（calibre 风格模板）；③每库可用正则接受/拒绝目录；④后缀白名单可改且对所有库生效。

### 测试套件（`python tests\run_all.py`，Windows + Python 3.14.7）

> 📌 测试套件的现行结论与逐用例状态归 [`COMPATIBILITY.md`](../contracts/COMPATIBILITY.md)（由契约文档维护）；
> 下表是测试套件的汇总结果；逐用例的现行状态以 [`COMPATIBILITY.md`](../contracts/COMPATIBILITY.md) 为准，
> 跑法与用例名仍然有效。

| 指标 | 值 |
|---|---|
| 全量 | **462 passed / 5 skipped / 0 failed / 0 collect-error**，退出码 **0** |
| 新增 `tests/test_hardlink_native.py` | **11 条**：A 组 6 条行为等价（新建树 / 已记录跳过 / 体积变化 / 源目录消失的孤儿清理 / 损坏日志 / 打印前缀冻结），B 组 5 条独立性 |
| 新增 `tests/test_library_overrides.py` | **16 条**：模板编译、变量表与引擎上下文一致性、覆盖归一化/字段级中文错误/跨层冲突、引擎用模板与库级正则、接口往返与拒绝、迁移加列、执行器读库行 |
| 新增 `tests/test_rename_extensions.py` | **5 条**：归一化、接口往返与小写去重、点开头校验、三层回退、库级优先 |

那 6 条行为等价的判据是**逐字节**：同一棵 fixture 树跑原生实现，归一化时间戳后断言 stdout 与日志文件内容
都等于基线；现行判据是项目回归测试里冻结的**黄金值**（见 `tests/test_hardlink_native.py` 的组注释与
`tests/test_works_ledger.py`）。

### 接口级端到端（真实 FastAPI `TestClient` + 真实磁盘）

一次独立的接口级端到端跑法（**13 项断言全 OK，退出码 0**）：

| 断言 | 实测 |
|---|---|
| 设置项默认值 | `rename_extensions == []`（= 未设置） |
| 归一化 | 传 `[".MKV", ".ts", ".mkv"]` ⇒ `['.mkv', '.ts']` |
| 点开头校验 | 传 `"mkv"` ⇒ 400「后缀必须以点开头…」，**原值不被破坏** |
| 库级覆盖往返 | `rule_override` 同时带 `naming.template` 原文与编译出的 `naming.segments` |
| 库级模板生效 | 任务 `success`，源 `x 01.mkv` → 目标 `[01-01] Show A.mkv`（处理器默认段会得到 `Show A S01E01.mkv`） |
| 库级 reject_dirs | `SP/Season 1/z 01.mkv` **保持原名**（整棵子树跳过） |
| 库级后缀白名单 | `x 01.chs.ass` **保持原名**（`extensions=[".mkv"]`） |

### 浏览器实测（真实实例 `uvicorn`，非 mock）

| 面 | 实测 |
|---|---|
| 设置页「允许重命名的文件后缀」 | 输入 `.MKV` / `.ts` 保存 ⇒ `GET /api/settings` 得 `['.mkv','.ts']`；输入 `mp4` ⇒ 前端拦下并提示、服务端 400、**库里的值不变** |
| 媒体库页「重命名规则（本库）」 | 开关开⇒显示表单；12 个变量按钮插入 `{name}`；模板 `[{season}-{episode}] {title}{suffix}` 实时预览 `[01-03] 示例作品.mkv`；接受/拒绝各行一条正则；保存后重开页面**模板原文、正则、开关全部还原** |
| 处理器页模板模式 | 模板框由现有段序列反推预填 `{title} S{season:02}E{episode:02}{suffix}`；`按模板生成段序列` 把 `{title} - {episode:02}{suffix}` 变成 4 段；未知变量实时报中文错 |
| 从 WebUI 触发任务 | 任务 `success`、`renamed=2`、`source_integrity {"ok":true,"changed":[]}`，任务日志含 `本库规则覆盖已生效：命名模板 '[{season}-{episode}] {title}{suffix}'；拒绝目录 1 条` 与 `Renamed: x 01.mkv -> [01-01] Show A.mkv` |
| 三层优先级 | 把库级 accept 改成 `^Season 9$` ⇒ `renamed=0`（处理器自己的 `^Season (\d+)$` 不再生效） |

### 只有真实渲染才能抓到的 3 个前端缺陷（已修）

| # | 位置 | 症状 | 修法 |
|---|---|---|---|
| 1 | `web/js/pages/library_edit.js` 的 `overrideHtml()` | 局部 `const raw = lib.rule_override` **遮蔽了从 `ui.js` 导入的 `raw()`** ⇒ 渲染整页抛 `raw is not a function`，路由停在「加载中…」且**控制台无报错** | 局部变量改名 `ov` |
| 2 | 同文件的 `updateOverridePreview()` | 把 `renderTemplate()` 的**返回值对象**写进 `textContent`，预览显示 `[object Object]` | 取 `.text`；自定义表达式另给提示 |
| 3 | `web/js/pages/processor_edit.js` 的模板框 | 新插入的模板输入框 `value: ''`，没有从现有段序列反推 | `value: segmentsToTemplate(segments)` + 边写边校验 |

> 教训：前端改动只能靠**真浏览器 + 真接口**验证（`node --check` 覆盖不到运行时行为）；
> 本次三条缺陷全部逃过了「模块能被 `import()`」与「Python 侧测试全绿」两种检查。

### 文档一致性清理

文档里的 `app/*.py:行号` 引用已逐条复核，修正了因代码移动而漂移的条目，
并复核了各文档对执行体（`app/processors/hardlink.py` 与 `app/processors/engine.py`）的描述是否与现行实现一致。

> 教训：代码移动后，文档里的 `文件:行号` 引用必须**逐条复核**，不能假定还指得准。

### 一条工具层教训

经 PowerShell 送给 NAS 的远端命令**必须是单行**。把多行脚本按原样喂进去后，
`eval "$(printf %s '<b64>' | base64 -d)"` 会把它压成一行执行（PowerShell 向原生命令传参时的
换行处理所致），症状是 `TOKEN=$(...)` 之后的 `echo` 被当成 `$TOKEN` 命令的参数。
因此传包/执行时的分块与校验一律写成单行。
