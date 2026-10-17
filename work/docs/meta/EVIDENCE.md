# 验证证据与实测结论

> **用途**：记录**可复现**的验证结论 —— 测了什么、怎么测、数字是多少、由此得出哪条结论。
> 它支撑 [`../contracts/BASELINE.md`](../contracts/BASELINE.md) 的契约结论与
> [`../contracts/ARCHITECTURE.md`](../contracts/ARCHITECTURE.md) 的设计决策；测试套件的现行状态归
> [`../contracts/COMPATIBILITY.md`](../contracts/COMPATIBILITY.md)，发版总账见
> [`../../reports/1.0.0/RELEASE-1.0.0.md`](../../reports/1.0.0/RELEASE-1.0.0.md)。
>
> 记录口径：除特别注明外，数字来自 1.0.0 发版实测（容器内 Python 3.11 + 真实磁盘）；
> 标注**待验证**的条目表示当前环境无法复现，结论保留但需自行复核。

---

## 1. 源目录零改动（B9 红线）

- **方法**：任务执行前后对源树做逐文件快照（相对路径、`st_size`、`st_ino`、`st_mtime_ns`），比对集合与数值。
- **结果**：两份全量测试库（8,534 / 3,986 文件）均「仅前 0 / 仅后 0 / 大小变化 0」。
- **结论**：应用只读源目录；目标侧硬链接与源文件共享 inode，但**不修改**源文件内容与元数据
  （`permission_mode` 开启时权限位例外，见 §5）。

## 2. 硬链接真实性

- **方法**：直接 `os.stat` 比对库侧与源侧文件，按 **inode 匹配**（库侧文件已改名，按文件名比对会误判）。
- **结果**：`st_ino` 相同且 `st_nlink >= 2`；目标树计数与源文件数一致（8,534 / 3,986 全部建成）。
- **结论**：产物是真正的硬链接，不是复制 —— **不占额外空间**，删源目录后目标仍可读（同 inode 引用计数）。

## 3. 输出契约与字符编码

硬链接阶段的 stdout 是**适配层解析结果的契约**，不是给人看的日志：`hardlink.py` 的每一行打印前缀
（`已创建硬链接: ` / `创建硬链接失败: ` / `文件夹已记录且体积一致，跳过: ` / `Deleted orphaned hard link folder: ` /
`已删除旧记录: ` / `已记录文件夹体积: `）与日志文件行格式（`%(asctime)s - Source: … - Folder: … - Folder size: N`）
都由 `app/script_adapter.py` 解析回来，**改动前缀会导致结果解析失配**。

中文打印与中文日志行在 POSIX locale 下会触发 `UnicodeEncodeError`，因此容器**必须**有
`LANG=C.UTF-8` + `PYTHONUTF8=1`（`docker/Dockerfile` 的 `ENV` 块已设）。

## 4. 接口级端到端（独立 harness）

- **形态**：真实 FastAPI `TestClient` + 真实磁盘，全程走 HTTP，与 `tests/**` **互为独立实现**
  （刻意不复用同一套 helper）；**关闭全部兼容垫片** —— 断言的是真实实现，不是被垫片兜住的行为。
- **覆盖**：静态与鉴权 / 设置与路径许可 / 媒体库 CRUD 与预检 / 任务执行 / 硬链接真实性 / 重命名落地 /
  任务·统计·系统信息 / 删库 / 日志时间字段 / 密码清空通路。
- **抓到的、单元测试抓不到的问题类型**（均已修，回归用例守在同名路径上）：

  1. **依赖注入缺注解**：`def require_user(request)` 少 `Request` 标注 ⇒ FastAPI 把参数当必需查询参数，
     该依赖下的所有路由统一 **422**；
  2. **签名不匹配**：把 `Request` 传给期望 `str` 的函数 ⇒ 首屏接口 500、前端白屏；
  3. **数据库列缺失**：登录成功路径 `UPDATE users SET last_login_at=?` 而表里没这列 ⇒ **口令正确反而 500**。

  > 教训：**单元层全绿不等于接口层可用** —— 跨层权限/装配问题只有"真发一次 HTTP"才会暴露。

- **SSE 协议**：`/api/tasks/{id}/events` 是**命名事件流** —— `event: task`（快照帧）→ `event: log`（日志帧）
  → `event: done`，**首帧不是日志行**；`event: log` 的载荷与 `GET /api/tasks/{id}/logs` 的 `items` 逐字段同形。

> 该 harness 是开发期工具，未随仓库发布；发布版以 `tests/**` + `scripts/acceptance-test.py` 为准（见 §7）。

## 5. 前端（免构建）

`web/**` 没有构建工具链（见 [`../contracts/ARCHITECTURE.md`](../contracts/ARCHITECTURE.md) §1），源码由浏览器直接加载。

- **语法检查**：`python scripts/syntax-check.py` 用 `node --check` 逐个检查 `web/**` 的 18 个 `.js` 文件（另做 Python AST 检查）。
  注意：某些 node 版本把无扩展名的脚本按 CJS 解析，会把 `import` 判成语法错误 —— 遇到这种情况先把文件复制成 `.mjs` 再检查。
- **模块图**：用浏览器或 `python -m http.server` 逐个 `await import()` 全部模块，0 报错；`js/pages/*.js` 均导出 `render`。
- **桩数据渲染**：8 条 hash 路由全部渲染成功，0 个 `window` 错误。
- **安全要求**：`StaticFiles(directory="web", html=True)` 把 `web/` 整棵挂在 `/`，任何临时文件
  （`_check.py` / `_smoke.html`）都会**不认证即可访问/下载** —— `web/` 里不得残留临时文件。
- 真实浏览器核验抓到并修掉的缺陷类型：**渲染转义**（组件返回裸字符串被模板转义）、
  **`[hidden]` 被作者样式覆盖**（属性为 `true` 却仍可见）、**局部变量遮蔽导入的函数**（页面抛错但控制台无报错）。
  > 教训：**属性正确不等于视觉正确**（涉及 `hidden` 的断言要看 `computed display`）；
  > **前端改动只能靠真浏览器 + 真接口验证**，`node --check` 覆盖不到运行时行为。

## 6. 部署验收要点（可照做）

| 步骤 | 应看到 |
|---|---|
| `docker compose config` | exit 0、stderr 空；`18081:18081` 两侧同源；恰好 2 条 bind（媒体共同父挂载 + 数据卷） |
| `docker compose build` / `up -d` | exit 0，容器 `Up (healthy)` |
| `docker inspect` → `.State.Health` | `{"Status":"healthy","FailingStreak":0,…}`；健康检查四项 `db / data_dir / processors / config` 全 true |
| `curl /api/system/health` | `{"status":"ok","checks":{…全部 true},"warnings":[],"version":…}`（该端点**永远 HTTP 200**，不能只看状态码） |
| 非 root | `/proc/1/status` 的 `Uid`/`Gid` 等于 `PUID`/`PGID`。镜像 `Config.User` 为空、降权由 entrypoint 内 `gosu` 完成 ⇒ `docker exec` 默认是 **root shell**，要以应用身份操作用 `docker exec -u 1000:1001` |

**回调端点实证**（未创建任务、未触碰媒体文件）：错 token / 无 token → **401**；正确 token 但无匹配库 →
`200 {"matched":0,…}`；改为 `Content-Type: application/json` 时 `torrent.name` 为空 ⇒
回调参数**只从查询串 / urlencoded 表单读**，JSON 体被静默忽略。

**传输到远端主机的注意**：部分设备的 SSH 会因 PAM/MOTD 横幅让 `scp`/`sftp` 报 `Received message too long`，
可改用分块 base64 + 远端 `base64 -d`，传完校验 size 与 sha256；`.env` 在目标机上生成，
`SECRET_KEY` 用远端 `secrets.token_hex(32)` 现算、不要经过日志，并 `chmod 600`。

## 7. 发版实测汇总（1.0.0）

| 项 | 结果 | 复现方式 |
|---|---|---|
| 容器内 pytest | `525 passed, 7 skipped, 2 errors in 32.98s` | `scripts/pytest-in-container.sh` |
| 全量验收 | `=== FULL100 RESULT PASS=56 FAIL=0 ===` | `scripts/acceptance-test.py`（**全新数据库**） |
| 两个全量测试库 | bangumi：8,534 文件 / 54 作品 / `renamed 1251`；movie：3,986 / 45 / `renamed 46`；复跑均 `linked 0` | 同上 |
| 静态自验 | `VALIDATOR_RC=0`、`34 PASS / 0 FAIL`；PY 71 / JS 18 语法全过 | `examples/validate-deployment-assets.sh`、`scripts/syntax-check.py` |

**2 个 errors 的说明**：来自 `tests/test_no_source_mutation.py` 两个用例的**夹具初始化**
（pytest 记为 ERROR 而非 FAILED），与 1.0.0 的应用改动无关，属已知的夹具问题。

**已知环境性差异（待验证/非缺陷）**：无 pytest 的环境可用纯标准库降级 runner `tests/run_all.py`
（覆盖面略小，个别依赖 pytest 夹具的用例会失败）；开发机的环境差异可能带来少量与实现无关的失败项，
以容器内 pytest 结果为准。

## 8. 一条工具层教训

经 PowerShell 送给远端主机的命令**必须是单行**：多行脚本按原样喂进去时，
`eval "$(printf %s '<b64>' | base64 -d)"` 会把它压成一行执行（PowerShell 向原生命令传参时的换行处理所致），
症状是 `TOKEN=$(...)` 之后的 `echo` 被当成 `$TOKEN` 命令的参数。传包/执行时的分块与校验一律写成单行。
