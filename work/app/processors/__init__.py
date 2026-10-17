"""处理器规格、规则引擎与配置同步。

重命名由 :mod:`app.processors.engine` 按用户配置的 spec 执行，`legacy` 预设即最朴素的旧口径；
硬链接侧由 :mod:`app.processors.hardlink` 原生实现承担。两边的行为都由
**本项目自己的回归测试**锁定。

详见 ``docs/contracts/PROCESSORS.md`` §1、§2。

本包**没有 import 副作用**：不建目录、不读数据库、不启线程。
"""

from __future__ import annotations

__all__ = [
    "spec",
    "defaults",
    "logs",
    "engine",
    "registry",
]
