#!/usr/bin/env python3
"""提交前语法自检：AST 解析 ``app/`` 与 ``tests/`` 下全部 .py，并 ``node --check`` 全部前端 js。

用法：
    python scripts/syntax-check.py [仓库根]

仓库根默认取本脚本所在目录的上一级，因此仓库整体移动不受影响。
node 可执行文件按以下顺序查找：``--node`` 参数 → 环境变量 ``NODE`` → PATH。
找不到 node 时只做 Python 检查，并明确打印 ``JS_SKIPPED``（不谎报通过）。
"""
from __future__ import annotations

import argparse
import ast
import os
import pathlib
import shutil
import subprocess
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="AST + node --check 语法自检")
    ap.add_argument("repo", nargs="?", default=None, help="仓库根（默认脚本上一级）")
    ap.add_argument("--node", default=None, help="node 可执行文件路径")
    args = ap.parse_args()

    root = pathlib.Path(args.repo).resolve() if args.repo else pathlib.Path(__file__).resolve().parents[1]

    py_files = []
    for sub in ("app", "tests"):
        py_files += [p for p in (root / sub).rglob("*.py") if "__pycache__" not in p.parts]
    py_files = sorted(set(py_files))

    failed = []
    for p in py_files:
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            failed.append((p, exc))
    print(f"ROOT {root}")
    print(f"PY_FILES {len(py_files)}")
    print(f"PY_SYNTAX_FAILED {len(failed)}")
    for p, exc in failed:
        print(f"  FAIL {p.relative_to(root)}: {exc!r}")

    node = args.node or os.environ.get("NODE") or shutil.which("node")
    js_files = sorted(p for p in (root / "web").rglob("*.js") if "vendor" not in p.parts)
    js_fail = []
    if node:
        for p in js_files:
            r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
            if r.returncode != 0:
                js_fail.append((p, r.stderr.strip().splitlines()[:3]))
    print(f"NODE {node or '(未找到)'}")
    print(f"JS_FILES {len(js_files)}")
    print(f"JS_SKIPPED {0 if node else 1}")
    print(f"JS_FAIL {len(js_fail)}")
    for p, err in js_fail:
        print(f"  FAIL {p.relative_to(root)}: {err}")

    return 1 if (failed or js_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
