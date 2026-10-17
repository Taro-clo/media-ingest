# 文档导航

`work/docs/` 按**读者**分三桶，边界是「谁在什么场景下读它、改了要不要同步 CHANGELOG」。

| 目录 | 给谁看 | 性质 | 收录 |
|---|---|---|---|
| [`contracts/`](contracts/) | 实现者 / 二开 | **契约**：当前行为与接口的定义；改行为必须同步改文档并记入 [`../CHANGELOG.md`](../CHANGELOG.md) | [ARCHITECTURE](contracts/ARCHITECTURE.md)（架构与数据模型）、[PROCESSORS](contracts/PROCESSORS.md)（处理器与规则引擎）、[API](contracts/API.md)（HTTP 接口）、[BASELINE](contracts/BASELINE.md)（行为基线 + B/L 编号索引）、[COMPATIBILITY](contracts/COMPATIBILITY.md)（跨环境兼容性） |
| [`guides/`](guides/) | 用户 / 运维 | **手册**：面向操作，跟着做就能部署 / 使用 / 排障 | [USER_GUIDE](guides/USER_GUIDE.md)（WebUI 使用）、[DEPLOYMENT](guides/DEPLOYMENT.md)（部署 / 升级 / 备份 / 恢复）、[TROUBLESHOOTING](guides/TROUBLESHOOTING.md)、[FAQ](guides/FAQ.md) |
| [`meta/`](meta/) | 维护者 | **结构与证据**：目录结构、测试映射、验证证据、已知问题 | [STRUCTURE](meta/STRUCTURE.md)、[TEST-MAP](meta/TEST-MAP.md)、[EVIDENCE](meta/EVIDENCE.md)、[KNOWN_ISSUES](meta/KNOWN_ISSUES.md) |

## 按任务找文档

| 我想… | 先读 |
|---|---|
| 知道这是什么、值不值得装 | [`../../README.md`](../../README.md)（根 README：30 秒定位 + 快速开始） |
| 把它部署到宿主机上 | [部署演练](../examples/nas-deploy.md)（完整演练）→ [`guides/DEPLOYMENT.md`](guides/DEPLOYMENT.md)（每个选项、升级、备份） |
| 学会用 WebUI 建库、跑任务、看日志 | [`guides/USER_GUIDE.md`](guides/USER_GUIDE.md) |
| 接 qBittorrent，下载完成自动入库 | [`../examples/qbittorrent-autorun.md`](../examples/qbittorrent-autorun.md)（回调命令的唯一权威副本） |
| 改命名规则 / 处理器 / 特典判据 | [`contracts/PROCESSORS.md`](contracts/PROCESSORS.md) |
| 改代码 / 调接口 | [`contracts/ARCHITECTURE.md`](contracts/ARCHITECTURE.md)、[`contracts/API.md`](contracts/API.md)、[`meta/STRUCTURE.md`](meta/STRUCTURE.md) |
| 判断"我的环境能不能用 / 为什么跨挂载不行" | [`contracts/COMPATIBILITY.md`](contracts/COMPATIBILITY.md) |
| 排障 | [`guides/TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) → [`guides/FAQ.md`](guides/FAQ.md) → [`meta/KNOWN_ISSUES.md`](meta/KNOWN_ISSUES.md) |
| 知道某条结论是怎么测出来的 | [`meta/EVIDENCE.md`](meta/EVIDENCE.md)、[`../reports/1.0.0/RELEASE-1.0.0.md`](../reports/1.0.0/RELEASE-1.0.0.md) |
| 改完代码怎么回归 | [`meta/TEST-MAP.md`](meta/TEST-MAP.md)（契约条目 ↔ 测试）、[`../scripts/README.md`](../scripts/README.md)（工具链） |

## 建议阅读路径

- **10 分钟（决定装不装）**：根 [`README.md`](../../README.md) → [`guides/DEPLOYMENT.md`](guides/DEPLOYMENT.md) §1 前置检查。
- **30 分钟（装好并建第一个库）**：[部署演练](../examples/nas-deploy.md) → [`guides/USER_GUIDE.md`](guides/USER_GUIDE.md) §3–§4。
- **半天（接手维护）**：[`contracts/ARCHITECTURE.md`](contracts/ARCHITECTURE.md) → [`meta/STRUCTURE.md`](meta/STRUCTURE.md) → [`contracts/BASELINE.md`](contracts/BASELINE.md) → [`meta/TEST-MAP.md`](meta/TEST-MAP.md)。

## 约定

- 文件名一律 `大写下划线.md`；文内互相引用用**相对链接**：同桶 `./X.md`、跨桶 `../<桶>/X.md`、指向工程文件 `../../<文件>`。
- 每份文档只做一件事；同一事实的权威位置只有一个（见 [`meta/STRUCTURE.md`](meta/STRUCTURE.md) §6），其它地方改指针。
- 目录结构、命名与「新增文件该放哪」的权威说明在 [`meta/STRUCTURE.md`](meta/STRUCTURE.md)。
- 根 [`README.md`](../../README.md) 是仓库总索引兼新用户入口；本目录的整体索引就是本页。

> 本目录整体被 `.dockerignore` 排除，**不进镜像**。
