# 文档导航（`work/docs/`）

`docs/` 按**读者**分三桶；三桶的边界就是「谁在什么场景下读它、改了要不要同步 CHANGELOG」。

| 目录 | 给谁看 | 性质 | 收录 |
|---|---|---|---|
| [`contracts/`](contracts/) | AI 与实现者 | **契约**：改行为必须同步改文档（`BASELINE.md` 为行为基线；改动记入 [`../CHANGELOG.md`](../CHANGELOG.md)） | ARCHITECTURE、PROCESSORS、API、BASELINE、COMPATIBILITY |
| [`guides/`](guides/) | 用户与运维 | **手册**：面向操作，跟着做就能部署 / 排障 | USER_GUIDE、DEPLOYMENT、TROUBLESHOOTING、FAQ |
| [`meta/`](meta/) | 维护者 | **结构与证据**：目录结构、验收证据、已知问题 | STRUCTURE、EVIDENCE、KNOWN_ISSUES |

## 建议阅读顺序

1. 第一次上手：先看工程入口 [`../README.md`](../README.md) 的「快速开始」，再看 [`guides/USER_GUIDE.md`](guides/USER_GUIDE.md)。
2. 部署到 NAS：[`guides/DEPLOYMENT.md`](guides/DEPLOYMENT.md) + [`../examples/nas-deploy.md`](../examples/nas-deploy.md)。
3. 改代码前：[`contracts/ARCHITECTURE.md`](contracts/ARCHITECTURE.md)、[`contracts/BASELINE.md`](contracts/BASELINE.md)（不可变契约）；
   改处理器与规则：[`contracts/PROCESSORS.md`](contracts/PROCESSORS.md)。
4. 出问题：[`guides/TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) → [`meta/KNOWN_ISSUES.md`](meta/KNOWN_ISSUES.md)。
5. 想知道「某个结论是怎么测出来的」：[`meta/EVIDENCE.md`](meta/EVIDENCE.md) + [`../reports/README.md`](../reports/README.md)。

## 约定

- 文件名一律 `大写下划线.md`；文内互相引用用相对链接。站在**桶内某篇**看：同桶 `./X.md`、跨桶 `../<桶>/X.md`、
  指向工程文件 `../../CHANGELOG.md`；本文与 `STRUCTURE.md` 这类位于 `docs/` 根或 `meta/` 的文件按实际层级推导。
- 目录结构、命名与「新增文件该放哪」的权威说明在 [`meta/STRUCTURE.md`](meta/STRUCTURE.md)。
- 全量文档索引（12 篇 + 工具链 / 报告）见 [`../README.md`](../README.md) 的「附：相关文档」。

> 本目录整体被 `.dockerignore` 排除，**不进镜像**（`../.dockerignore:29`）。