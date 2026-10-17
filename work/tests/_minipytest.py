"""极简 pytest 兼容层（纯 stdlib）。

有的环境**没有 pytest，也不允许联网安装**，因此
``tests/run_all.py`` 需要有办法在零依赖下运行同一批测试文件。本模块实现
tests/ 里用到的 pytest 子集：

``fixture`` / ``autouse`` / ``parametrize`` / ``skipif`` / ``skip`` /
``raises`` / ``importorskip`` / ``fail`` / ``mark`` / ``monkeypatch`` /
``tmp_path``

设计原则：**只实现子集，不模拟全部语义**。装有 pytest 时 ``run_all.py``
直接委托给真正的 pytest，本模块不参与；其存在只是为了「无 pytest 也能跑」。
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import inspect
import io
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

__all__ = [
    "Skipped",
    "Failed",
    "ExceptionInfo",
    "fixture",
    "parametrize",
    "skipif",
    "skip",
    "fail",
    "raises",
    "importorskip",
    "mark",
    "MonkeyPatch",
    "run",
]


# --------------------------------------------------------------------------- 异常


class Skipped(Exception):
    """pytest.skip / importorskip 的载体。"""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


class Failed(AssertionError):
    """pytest.fail / 断言辅助失败。"""


class _NotSet:
    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return "<notset>"


NOTSET = _NotSet()


class ExceptionInfo:
    """``pytest.raises`` 的返回值。"""

    def __init__(self) -> None:
        self.type = None
        self.value: BaseException | None = None
        self.tb = None

    def match(self, regexp: str) -> bool:
        return bool(re.search(regexp, str(self.value)))


# --------------------------------------------------------------------------- marks


def parametrize(argnames, argvalues, ids=None, **kwargs):
    """``@pytest.mark.parametrize``：支持堆叠（笛卡尔积）与单参数简写。"""
    names = (
        [n.strip() for n in argnames.split(",")]
        if isinstance(argnames, str)
        else list(argnames)
    )
    rows = []
    for item in argvalues:
        if len(names) == 1:
            rows.append((item,))
        else:
            rows.append(tuple(item))

    def decorator(func):
        groups = list(getattr(func, "_mlm_param_groups", []))
        groups.append((names, rows, ids))
        func._mlm_param_groups = groups
        return func

    return decorator


def skipif(condition, reason: str = ""):
    def decorator(func):
        if condition:
            func._mlm_skip = reason or "skipif 条件成立"
        return func

    return decorator


def skip(reason: str = "", allow_module_level: bool = False):
    raise Skipped(reason)


def fail(msg: str = "", pytrace: bool = True):
    raise Failed(msg)


class _MarkSkip:
    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def __call__(self, func):
        func._mlm_skip = self.reason or "无条件 skip"
        return func


class _Mark:
    """``pytest.mark`` 命名空间；未实现的 mark 退化为恒等装饰器。"""

    parametrize = staticmethod(parametrize)
    skipif = staticmethod(skipif)
    skip = staticmethod(_MarkSkip)

    def __getattr__(self, name):  # pragma: no cover - 兜底
        def decorator(*args, **kwargs):
            if len(args) == 1 and callable(args[0]) and not kwargs:
                return args[0]
            return lambda func: func

        return decorator


mark = _Mark()


# --------------------------------------------------------------------------- raises


@contextlib.contextmanager
def raises(expected, match: str | None = None):
    info = ExceptionInfo()
    try:
        yield info
    except expected as exc:  # type: ignore[misc]
        info.type = type(exc)
        info.value = exc
        if match is not None and not re.search(match, str(exc)):
            raise Failed(f"异常信息 {str(exc)!r} 不匹配 {match!r}") from exc
        return
    except Exception as exc:  # noqa: BLE001
        raise Failed(
            f"期望抛出 {expected}，实际抛出 {type(exc).__name__}: {exc}"
        ) from exc
    raise Failed(f"DID NOT RAISE {expected}")


# --------------------------------------------------------------------------- imports


def importorskip(modname: str, minversion: str | None = None, reason: str | None = None):
    try:
        module = importlib.import_module(modname)
    except ImportError as exc:
        raise Skipped(reason or f"缺少可选依赖 {modname}: {exc}") from exc
    return module


# --------------------------------------------------------------------------- fixtures


def fixture(func=None, *, autouse: bool = False, scope: str = "function",
            name: str | None = None, params=None, ids=None):
    def decorator(target):
        target._mlm_fixture = {
            "autouse": bool(autouse),
            "scope": scope,
            "name": name or target.__name__,
            "params": params,
        }
        return target

    if func is not None:
        return decorator(func)
    return decorator


class MonkeyPatch:
    """``monkeypatch`` fixture 的子集实现，退出时统一回滚。"""

    def __init__(self) -> None:
        self._undo: list = []

    # -- attributes ------------------------------------------------------
    def setattr(self, target, name, value=NOTSET, raising: bool = True):
        if isinstance(target, str) and value is NOTSET:
            value = name
            modname, _, attr = target.rpartition(".")
            target = importlib.import_module(modname)
            name = attr
        old = getattr(target, name, NOTSET)
        if old is NOTSET and raising:
            raise AttributeError(f"{target!r} 没有属性 {name!r}")
        self._undo.append(("setattr", target, name, old))
        setattr(target, name, value)
        return value

    def delattr(self, target, name, raising: bool = True):
        old = getattr(target, name, NOTSET)
        if old is NOTSET:
            if raising:
                raise AttributeError(f"{target!r} 没有属性 {name!r}")
            return
        self._undo.append(("setattr", target, name, old))
        delattr(target, name)

    # -- container -------------------------------------------------------
    def setitem(self, dic, name, value):
        old = dic.get(name, NOTSET)
        self._undo.append(("setitem", dic, name, old))
        dic[name] = value

    def delitem(self, dic, name, raising: bool = True):
        if name not in dic:
            if raising:
                raise KeyError(name)
            return
        self._undo.append(("setitem", dic, name, dic[name]))
        del dic[name]

    # -- environment / cwd ----------------------------------------------
    def setenv(self, name: str, value: str, prepend: str | None = None):
        old = os.environ.get(name, NOTSET)
        self._undo.append(("env", name, old))
        value = str(value)
        if prepend and name in os.environ:
            value = value + prepend + os.environ[name]
        os.environ[name] = value

    def delenv(self, name: str, raising: bool = True):
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        self._undo.append(("env", name, os.environ[name]))
        del os.environ[name]

    def syspath_prepend(self, path):
        path = str(path)
        self._undo.append(("syspath", path, None))
        sys.path.insert(0, path)

    def chdir(self, path):
        old = os.getcwd()
        self._undo.append(("cwd", old, None))
        os.chdir(path)

    # -- rollback --------------------------------------------------------
    def undo(self) -> None:
        while self._undo:
            kind, obj, name, old = self._undo.pop()
            try:
                if kind == "setattr":
                    if old is NOTSET:
                        delattr(obj, name)
                    else:
                        setattr(obj, name, old)
                elif kind == "setitem":
                    if old is NOTSET:
                        obj.pop(name, None)
                    else:
                        obj[name] = old
                elif kind == "env":
                    if old is NOTSET:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = old
                elif kind == "syspath":
                    try:
                        sys.path.remove(obj)
                    except ValueError:
                        pass
                elif kind == "cwd":
                    os.chdir(obj)
            except Exception:  # noqa: BLE001 - 回滚尽力而为
                pass


# --------------------------------------------------------------------------- runner


class _FixtureError(Exception):
    pass


class _CaptureResult:
    """``capsys.readouterr()`` 的返回值：``.out`` / ``.err`` 两个字符串。"""

    def __init__(self, out: str, err: str) -> None:
        self.out = out
        self.err = err


class CapsysCapture:
    """``capsys`` 夹具的兜底实现：把进程级 stdout/stderr 重定向到内存缓冲。

    语义按本测试集用到的程度对齐 pytest：``readouterr()`` 返回自上次读取以来
    的输出并清空缓冲；``close()`` 由夹具 teardown 调用，恢复原 stdout/stderr。
    """

    def __init__(self) -> None:
        self._out = io.StringIO()
        self._err = io.StringIO()
        self._redirects: list = []

    def start(self) -> None:
        self._redirects = [
            contextlib.redirect_stdout(self._out),
            contextlib.redirect_stderr(self._err),
        ]
        for redirect in self._redirects:
            redirect.__enter__()

    def readouterr(self) -> _CaptureResult:
        result = _CaptureResult(self._out.getvalue(), self._err.getvalue())
        for buffer in (self._out, self._err):
            buffer.seek(0)
            buffer.truncate()
        return result

    def close(self) -> None:
        for redirect in reversed(self._redirects):
            redirect.__exit__(None, None, None)
        self._redirects = []


class _Case:
    def __init__(self, module_name: str, func, params: dict, case_id: str) -> None:
        self.module_name = module_name
        self.func = func
        self.params = params
        self.case_id = case_id

    @property
    def name(self) -> str:
        base = f"{self.module_name}::{self.func.__name__}"
        return f"{base}[{self.case_id}]" if self.case_id else base


class _Ctx:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = MonkeyPatch()
        self.fixtures: dict[str, object] = {}
        self.teardowns: list = []
        self.resolving: list[str] = []


class MiniRunner:
    def __init__(self, tests_dir: Path, verbose: bool = True) -> None:
        self.tests_dir = Path(tests_dir).resolve()
        self.verbose = verbose
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures: list[tuple[str, str]] = []
        self.skip_reasons: list[tuple[str, str]] = []
        self.collect_errors: list[tuple[str, str]] = []
        self.conftest_fixtures: dict[str, dict] = {}

    # -- collection ------------------------------------------------------
    def _import_conftest(self) -> None:
        path = self.tests_dir / "conftest.py"
        if not path.is_file():
            return
        spec = importlib.util.spec_from_file_location("conftest", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["conftest"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        self.conftest_fixtures = self._collect_fixtures(module)

    @staticmethod
    def _collect_fixtures(module) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for value in vars(module).values():
            meta = getattr(value, "_mlm_fixture", None)
            if meta:
                found[meta["name"]] = {"func": value, **meta}
        return found

    def collect(self) -> list[_Case]:
        cases: list[_Case] = []
        # 开发期快跑：MLM_ONLY 用逗号分隔模块名（或模块名片段），未设置时收集全部
        only = [part.strip() for part in os.environ.get("MLM_ONLY", "").split(",") if part.strip()]
        for path in sorted(self.tests_dir.glob("test_*.py")):
            modname = path.stem
            if only and not any(part in modname for part in only):
                continue
            try:
                spec = importlib.util.spec_from_file_location(modname, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[modname] = module
                spec.loader.exec_module(module)  # type: ignore[union-attr]
            except Skipped as exc:
                self.collect_errors.append((modname, f"模块级 skip: {exc.reason}"))
                continue
            except Exception:  # noqa: BLE001
                self.collect_errors.append((modname, traceback.format_exc()))
                continue
            for name, value in vars(module).items():
                if not name.startswith("test_") or not inspect.isfunction(value):
                    continue
                cases.extend(self._expand(modname, name, value))
        return cases

    @staticmethod
    def _expand(modname: str, name: str, func) -> list[_Case]:
        groups = getattr(func, "_mlm_param_groups", [])
        combos: list[tuple[dict, list[str]]] = [({}, [])]
        for names, rows, ids in groups:
            new: list[tuple[dict, list[str]]] = []
            for existing, idparts in combos:
                for index, row in enumerate(rows):
                    merged = dict(existing)
                    merged.update(dict(zip(names, row)))
                    label = ids[index] if ids and index < len(ids) else "-".join(
                        str(v) for v in row
                    )
                    new.append((merged, idparts + [str(label)]))
            combos = new
        return [
            _Case(modname, func, params, "-".join(idparts))
            for params, idparts in combos
        ]

    # -- execution -------------------------------------------------------
    def run(self) -> int:
        self._import_conftest()
        cases = self.collect()
        for modname, error in self.collect_errors:
            print(f"COLLECT-ERROR {modname}\n{error}")
        for case in cases:
            self._run_case(case)
        self._summary()
        return 1 if (self.failed or self.collect_errors) else 0

    def _registry(self, module_name: str) -> dict[str, dict]:
        registry = dict(self.conftest_fixtures)
        module = sys.modules.get(module_name)
        if module is not None:
            for name, meta in self._collect_fixtures(module).items():
                registry[name] = meta
        return registry

    def _run_case(self, case: _Case) -> None:
        registry = self._registry(case.module_name)
        tmp_path = Path(tempfile.mkdtemp(prefix="mlmmt_"))
        ctx = _Ctx(tmp_path)
        skipped_reason = getattr(case.func, "_mlm_skip", None)
        try:
            if skipped_reason:
                raise Skipped(skipped_reason)
            for name, meta in registry.items():
                if meta["autouse"]:
                    self._resolve(name, ctx, registry)
            kwargs = {}
            for param in inspect.signature(case.func).parameters:
                if param in case.params:
                    kwargs[param] = case.params[param]
                else:
                    kwargs[param] = self._resolve(param, ctx, registry)
            case.func(**kwargs)
        except Skipped as exc:
            self.skipped += 1
            self.skip_reasons.append((case.name, exc.reason))
            self._emit(case, "SKIP", exc.reason)
        except BaseException:  # noqa: BLE001
            self.failed += 1
            text = traceback.format_exc()
            self.failures.append((case.name, text))
            self._emit(case, "FAIL", text.strip().splitlines()[-1])
        else:
            self.passed += 1
            self._emit(case, "PASS", "")
        finally:
            self._teardown(ctx)
            shutil.rmtree(tmp_path, ignore_errors=True)

    def _emit(self, case: _Case, status: str, detail: str) -> None:
        if self.verbose or status != "PASS":
            suffix = f" :: {detail}" if detail and status != "FAIL" else ""
            print(f"{status:6} {case.name}{suffix}", flush=True)

    @staticmethod
    def _teardown(ctx: _Ctx) -> None:
        while ctx.teardowns:
            finalizer = ctx.teardowns.pop()
            try:
                finalizer()
            except Exception:  # noqa: BLE001
                pass
        ctx.monkeypatch.undo()

    def _resolve(self, name: str, ctx: _Ctx, registry: dict[str, dict]):
        if name in ctx.fixtures:
            return ctx.fixtures[name]
        if name in ctx.resolving:
            raise _FixtureError(f"fixture 循环依赖: {' -> '.join(ctx.resolving + [name])}")

        if name == "tmp_path":
            ctx.fixtures[name] = ctx.tmp_path
            return ctx.tmp_path
        if name == "monkeypatch":
            ctx.fixtures[name] = ctx.monkeypatch
            return ctx.monkeypatch
        if name == "tmp_path_factory":
            raise _FixtureError("本兜底运行器未实现 tmp_path_factory")
        if name == "capsys":
            capture = CapsysCapture()
            capture.start()
            ctx.teardowns.append(capture.close)
            ctx.fixtures[name] = capture
            return capture

        meta = registry.get(name)
        if meta is None:
            raise _FixtureError(f"未定义的 fixture: {name!r}")
        func = meta["func"]
        ctx.resolving.append(name)
        try:
            kwargs = {}
            for param in inspect.signature(func).parameters:
                kwargs[param] = self._resolve(param, ctx, registry)
            if inspect.isgeneratorfunction(func):
                gen = func(**kwargs)
                value = next(gen)

                def finalize(generator=gen) -> None:
                    try:
                        next(generator)
                    except StopIteration:
                        pass

                ctx.teardowns.append(finalize)
            else:
                value = func(**kwargs)
        finally:
            ctx.resolving.pop()
        ctx.fixtures[name] = value
        return value

    def _summary(self) -> None:
        print("")
        if self.skip_reasons:
            print("---- 跳过原因 ----")
            for name, reason in self.skip_reasons:
                print(f"  SKIP {name}: {reason}")
        if self.failures:
            print("---- 失败详情 ----")
            for name, text in self.failures:
                print(f"\n=== {name} ===\n{text}")
        print(
            f"==== {self.passed} passed, {self.skipped} skipped, "
            f"{self.failed} failed, {len(self.collect_errors)} collect-error ===="
        )


def run(tests_dir: str | os.PathLike, verbose: bool = True) -> int:
    # 让 conftest.py / test_*.py 里的 `import pytest` 解析到本模块（无 pytest 场景）。
    sys.modules.setdefault("pytest", sys.modules[__name__])
    return MiniRunner(Path(tests_dir), verbose=verbose).run()
