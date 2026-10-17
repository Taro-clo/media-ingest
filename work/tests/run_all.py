"""测试统一入口：有 pytest 用 pytest，没有 pytest 用纯 stdlib 兜底运行器。

用法（仓库根目录下执行）：:

    python tests/run_all.py          # 自动选择后端
    python tests/run_all.py -v       # 透传给 pytest / 兜底运行器
    python -m pytest tests -q        # 若容器/开发机装了 pytest

设计说明：有的开发环境**没有 pytest 且不允许联网安装**，
因此这里提供 ``tests/_minipytest.py`` 作为 pytest 子集兜底，保证同一批测试
文件在没有 pytest 时也能跑完整套并给出 PASS/SKIP/FAIL 与退出码。
容器内验收仍应以 ``python -m pytest tests -q`` 为准（参见 docs/contracts/COMPATIBILITY.md
「测试执行方式」一节）。
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
for _path in (str(REPO_ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def main(argv: list[str]) -> int:
    rest = [arg for arg in argv if arg not in ("-v", "--verbose", "-q", "--quiet")]
    verbose = "-q" not in argv and "--quiet" not in argv

    try:
        import pytest  # noqa: F401
    except ImportError:
        pytest = None  # type: ignore[assignment]

    if pytest is not None:
        args = rest or [str(TESTS_DIR)]
        if verbose:
            args.append("-v")
        else:
            args.append("-q")
        return int(pytest.main(args))

    import _minipytest

    print("=" * 78)
    print("警告：当前解释器未安装 pytest —— 改用 tests/_minipytest.py（纯 stdlib 子集）")
    print("      本节结果与 pytest 的断言内容完全相同，但夹具语义是子集实现。")
    print("      正式验收请在容器内执行：python -m pytest tests -q")
    print("=" * 78)
    print("")
    return _minipytest.run(TESTS_DIR, verbose=verbose)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
