# reports/ —— 验收报告与产物

本仓库**只保留 1.0.0 的验收证据**。

| 目录 | 内容 |
|---|---|
| [`1.0.0/`](1.0.0/) | [`RELEASE-1.0.0.md`](1.0.0/RELEASE-1.0.0.md)：1.0.0 首发的镜像构建、单元测试、56 项全量验收、两库真实源全量入库与静态自验结果 |

## 验收证据在哪

| 想知道什么 | 去哪看 |
|---|---|
| 1.0.0 改了什么、逐条影响 | [`../CHANGELOG.md`](../CHANGELOG.md) 的 `[1.0.0]` 小节 |
| 1.0.0 发版实测（镜像 / pytest / 56 项 / 两库计数） | 同上「发版实测」子节 + [`1.0.0/RELEASE-1.0.0.md`](1.0.0/RELEASE-1.0.0.md) |
| 结论是怎么测出来的、红线在哪 | [`../docs/meta/EVIDENCE.md`](../docs/meta/EVIDENCE.md) |
| 自己复现一遍 | [`1.0.0/RELEASE-1.0.0.md`](1.0.0/RELEASE-1.0.0.md) 的「复现步骤」+ [`../scripts/README.md`](../scripts/README.md) |

## 命名与口径

- 报告名：`RELEASE-<版本>.md`（发版验收）或 `TEST-<版本>-<范围>-<日期>.md`（专项轮次）。
- 若保留原始产物：同版本目录下放 `FULL-<版本数字>.json`（机器可读，含每条断言与关键计数）、
  `.md`（人读清单）、`.log.txt`（原始 stdout），报告里的 `sha256` 作为「产物没被改过」的锚点。
- 一次验收的标准流程见 [`../scripts/README.md`](../scripts/README.md)：
  `build-image.sh` → `run-container.sh` → `acceptance-test.py` → `pytest-in-container.sh`。
  `acceptance-test.py` 必须在**全新数据库**上跑，且 `MLM_ADMIN_PW` / `MLM_QBT_PASS`
  必须由调用方用环境变量提供（仓库内不含任何真实口令）。
