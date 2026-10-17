"""部署默认值派生：``MEDIA_ROOT`` → ``DEFAULT_SOURCE_ROOT`` / ``DEFAULT_TARGET_ROOT``。

**为什么用子进程**：``app/config.py`` 里这两个常量是**导入时**按环境变量算好的模块属性，
同一进程内 ``importlib.reload`` 会牵动其他已缓存模块对旧对象/旧常量的引用。子进程把
环境彻底隔离，顺便验证的正是部署真实路径 ——「冷启动时只给 ``MEDIA_ROOT``，应用自己
派生出白名单根」。这样镜像部署的 compose 就不必再传 ``DEFAULT_*``（见
``docker-compose.release.yml`` 的锚点段与 ``examples/validate-deployment-assets.sh`` §6b）。

纪律：不在模块级 import ``app.*``（见 ``tests/conftest.py`` 的说明），只用 ``subprocess``。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SNIPPET = (
    "import app.config as c\n"
    "print(c.DEFAULT_SOURCE_ROOT)\n"
    "print(c.DEFAULT_TARGET_ROOT)\n"
)

_RELEVANT_ENV = ("MEDIA_ROOT", "DEFAULT_SOURCE_ROOT", "DEFAULT_TARGET_ROOT")


def _derive(env_overrides: dict[str, str]) -> tuple[str, str]:
    """在干净的子进程里 import app.config，返回派生出的 (源根, 目标根)。"""
    env = {key: value for key, value in os.environ.items() if key not in _RELEVANT_ENV}
    env.update(env_overrides)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) if not existing_pythonpath
        else str(REPO_ROOT) + os.pathsep + existing_pythonpath
    )
    env.setdefault("PYTHONUTF8", "1")
    completed = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"子进程导入 app.config 失败（returncode={completed.returncode}）:"
        f"\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) >= 2, f"输出不足两行: {completed.stdout!r}"
    return lines[0], lines[1]


def test_defaults_derive_from_media_root():
    """只给 MEDIA_ROOT 时，源/目标根自动派生到其下 Download / Library。"""
    assert _derive({"MEDIA_ROOT": "/mnt/media"}) == (
        "/mnt/media/Download",
        "/mnt/media/Library",
    )


def test_defaults_fall_back_to_vol1_when_media_root_is_absent():
    """MEDIA_ROOT 也没给时退回历史默认 /vol1/1000（老部署不因缺变量而换根）。"""
    assert _derive({}) == ("/vol1/1000/Download", "/vol1/1000/Library")


def test_media_root_trailing_slash_is_normalized():
    """MEDIA_ROOT 尾部带 / 时不拼出 //Download。"""
    assert _derive({"MEDIA_ROOT": "/mnt/media/"}) == (
        "/mnt/media/Download",
        "/mnt/media/Library",
    )


def test_explicit_source_and_target_win_over_derivation():
    """显式给了 DEFAULT_* 就用显式值（派生只是回退，不是覆盖）。"""
    assert _derive(
        {
            "MEDIA_ROOT": "/mnt/media",
            "DEFAULT_SOURCE_ROOT": "/data/dl",
            "DEFAULT_TARGET_ROOT": "/data/lib",
        }
    ) == ("/data/dl", "/data/lib")


def test_empty_env_values_are_treated_as_unset():
    """compose 写 ``DEFAULT_SOURCE_ROOT=`` 这类空串时不顶掉派生值。"""
    assert _derive(
        {
            "MEDIA_ROOT": "/mnt/media",
            "DEFAULT_SOURCE_ROOT": "",
            "DEFAULT_TARGET_ROOT": "",
        }
    ) == ("/mnt/media/Download", "/mnt/media/Library")


def test_empty_media_root_also_falls_back():
    """``MEDIA_ROOT=``（空串）按未设置处理，退回 /vol1/1000。"""
    assert _derive({"MEDIA_ROOT": ""}) == ("/vol1/1000/Download", "/vol1/1000/Library")
