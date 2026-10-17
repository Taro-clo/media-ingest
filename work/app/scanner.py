"""定时扫描线程（ARCHITECTURE §6.8）。

定位是**兜底**，不是主路径：qBittorrent 完成回调（``/api/hooks/qbittorrent``）才是
事件驱动的主路径。扫描线程只做三件事：

1. 每 ``scan_tick_seconds`` 醒一次；若 ``scan_enabled`` 为真且距上次扫描已超过
   ``scan_interval_seconds``，就给每个**已启用**的媒体库提交一个全量任务；
2. 同一库在同一时刻最多入队一个任务（避免堆叠）；
3. 顺带刷新 qBittorrent 连接状态（前端"qB 状态灯"）。

**没有 plans/schedules 表**：§3 的数据模型里没有它，调度状态就存在设置表里
（``scan_last_run_at`` / ``scan_next_run_at``）。为一个兜底定时器引入一张表、
以及随之而来的 CRUD 接口和一致性负担，是得不偿失的——库的启用状态本身就是"要不要扫"的表达。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from .config import settings
from .db import get_conn, get_setting, now_iso, parse_iso, tx

logger = logging.getLogger("app.scanner")

#: 扫描间隔的允许范围（秒）：太短会撞上日志的读-改-写（任务已串行，但没必要空转），
#: 太长则"兜底"就失去意义了。默认 900 秒来自 §3 的 ``scan_interval_seconds``。
MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 86400
DEFAULT_INTERVAL_SECONDS = 900

#: 真值集合，与项目其它地方保持一致。
TRUTHY = ("1", "true", "yes", "on")


def _iso_z(moment: datetime) -> str:
    """UTC ``datetime`` -> ``db.now_iso()`` 同风格字符串（毫秒 + ``Z``）。"""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _is_true(raw: str | None) -> bool:
    return (raw or "").strip().lower() in TRUTHY


def _kind_for_library(row) -> str:
    """媒体库 -> 任务类型：**由处理器的 ``kind`` 决定**，不猜 bangumi/movie。

    查不到处理器（被删了）时退回"只建硬链接"：重命名会改动库里的文件名，
    猜错方向的代价远大于少做一步。真正的错误会在执行器里以中文 ERROR 暴露出来。
    """
    processor_id = ""
    try:
        processor_id = str(row["processor_id"] or "")
    except (KeyError, IndexError):
        processor_id = ""
    if not processor_id:
        return "hardlink"
    try:
        from .processors import registry

        spec = registry.get_processor(processor_id)
    except Exception:  # noqa: BLE001 - 扫描线程绝不能因为查库失败而退出
        logger.warning("扫描时读不到处理器 %s 的规格，按只建硬链接处理。", processor_id, exc_info=True)
        return "hardlink"
    if spec and str(spec.get("kind")) == "rename":
        return "rename_hardlink"
    return "hardlink"


class Scanner:
    """单线程定时扫描器（daemon；随进程退出）。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 配置读取

    def _tick_seconds(self) -> float:
        """线程醒来的频率：只需保证"到点时能被看见"，与扫描间隔无关。"""
        raw = get_setting("scan_tick_seconds", "") or ""
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 60.0
        return max(5.0, min(value, 3600.0))

    def _interval_seconds(self) -> int:
        """两次自动扫描之间的间隔（秒），来自 ``scan_interval_seconds``。"""
        raw = get_setting("scan_interval_seconds", "") or ""
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = DEFAULT_INTERVAL_SECONDS
        return max(MIN_INTERVAL_SECONDS, min(value, MAX_INTERVAL_SECONDS))

    def _due(self, now: datetime) -> bool:
        """是否到了该自动扫描的时间（``scan_last_run_at`` 为空 = 从未扫过 = 立刻扫）。"""
        last = parse_iso(get_setting("scan_last_run_at"))
        if last is None:
            return True
        try:
            elapsed = (now - last).total_seconds()
        except TypeError:  # 时区信息缺失/不同类型，按"到点"处理而不是永久卡住
            return True
        return elapsed >= self._interval_seconds()

    # ------------------------------------------------------------------ 线程生命周期

    def start(self) -> None:
        """启动扫描线程（幂等）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="mlm-scanner", daemon=True)
            self._thread.start()
            logger.info("定时扫描线程已启动")

    def stop(self, timeout: float = 5) -> None:
        """停止扫描线程。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    # ------------------------------------------------------------------ 扫描

    def _has_active_task(self, library_id: int) -> bool:
        """该库是否已有排队/执行中的任务（同一库不堆叠）。"""
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE library_id=? AND status IN ('queued','running')",
            (library_id,),
        ).fetchone()
        return bool(row and int(row["n"]) > 0)

    def _submit_for_libraries(self) -> list[int]:
        """给所有已启用的库存提交一个全量任务，返回新任务 id 列表。"""
        from .queue import TaskSpec, queue

        rows = get_conn().execute(
            "SELECT * FROM libraries WHERE enabled=1 ORDER BY id ASC"
        ).fetchall()
        task_ids: list[int] = []
        for row in rows:
            library_id = int(row["id"])
            if self._has_active_task(library_id):
                logger.debug("库 %s 已有活动任务，扫描跳过", library_id)
                continue
            kind = _kind_for_library(row)
            task_ids.append(
                queue.submit(
                    TaskSpec(
                        library_id=library_id,
                        kind=kind,
                        trigger="scan",
                        scope={"mode": "full"},
                        payload={"library_name": row["name"]},
                    )
                )
            )
        return task_ids

    def run_once(self) -> int:
        """跑一轮检查，返回提交的任务数（也供 self-test 直接调用）。

        只有 ``scan_enabled`` 为真、且距上次自动扫描超过 ``scan_interval_seconds`` 时才干活；
        手动触发走 :meth:`scan_libraries`，不受这两条限制。
        """
        if not _is_true(get_setting("scan_enabled", "false")):
            return 0
        now = datetime.now(timezone.utc)
        if not self._due(now):
            return 0
        # 先把"上次扫描时间"推进，再提交：即使本次全部因活动任务被跳过，
        # 也不会下一个 tick 立刻又撞上来（错过的周期不补跑）。
        self._stamp_last_run(now)
        task_ids = self._submit_for_libraries()
        if task_ids:
            logger.info("定时扫描提交 %d 个任务：%s", len(task_ids), task_ids)
        return len(task_ids)

    def scan_libraries(self) -> list[int]:
        """立即对**所有已启用**的库提交一次全量任务，返回新任务 id 列表。

        给 ``POST /api/hooks/scan``（§6.2）用：该接口**不管** ``scan_enabled`` 是否为真，
        手动调用总是执行一遍。提交前照旧按库去重，避免和正在跑的任务堆叠。

        顺手推进 ``scan_last_run_at``：手动扫过之后，自动扫描不必紧接着再来一次。
        """
        self._stamp_last_run(datetime.now(timezone.utc))
        return self._submit_for_libraries()

    def _stamp_last_run(self, moment: datetime) -> None:
        """记录"上次扫描时间"（自动与手动共用）。

        只存这一把时间戳，**不**另存 ``scan_next_run_at``：下一次运行时间由
        ``scan_last_run_at + scan_interval_seconds`` 推导（见 :meth:`status`），
        存一份派生值只会让它有机会和间隔设置漂移。
        """
        with tx() as conn:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("scan_last_run_at", _iso_z(moment), now_iso()),
            )

    def status(self) -> dict:
        """给 ``GET /api/system/info`` / 设置页看的调度状态。"""
        interval = self._interval_seconds()
        last = parse_iso(get_setting("scan_last_run_at"))
        next_run = _iso_z(last + timedelta(seconds=interval)) if last is not None else None
        return {
            "enabled": _is_true(get_setting("scan_enabled", "false")),
            "interval_seconds": interval,
            "last_run_at": get_setting("scan_last_run_at"),
            # 从未扫过时为 null：那不叫"下次运行时间未知"，而叫"还没开始算"。
            "next_run_at": next_run,
            "running": bool(self._thread is not None and self._thread.is_alive()),
        }

    def _refresh_qbittorrent(self) -> None:
        """顺带刷新 qB 连接状态到 settings（失败只记 debug，不打扰用户）。

        这里必须调用**模块级**函数 :func:`app.qbittorrent.test_saved`（它的 docstring 就是
        为这个周期刷新写的）。``QBClient`` 上没有 ``test_connection()``（那是模块级函数），
        误写成 ``client.test_connection()`` 时异常会被下面的 ``except`` 吞掉，于是仪表盘的
        qB 状态灯与版本号永远刷不出来。
        另：``version`` 只在**成功**时写入（失败时第二个返回值是中文错误原因，不是版本号）。
        回归测试：``tests/test_scanner_qbittorrent.py``。
        """
        try:
            from .qbittorrent import get_client, test_saved

            if get_client() is None:  # 未配置地址：不写状态，也不去打扰 qB
                return
            ok, version = test_saved()
            stamp = now_iso()
            rows = [("qbittorrent_last_status", "ok" if ok else "error", stamp)]
            if ok and version:
                # 只有**成功**时第二个返回值才是版本号；失败时它是中文错误原因
                # （``test_connection()`` 的契约是 ``(False, 原因)``）。把它写进
                # ``qbittorrent_version`` 会让仪表盘显示"版本 = 认证失败：…"，所以
                # 失败时保留上一次成功的版本号不动。
                rows.append(("qbittorrent_version", version, stamp))
            with tx() as conn:
                for key, value, updated in rows:
                    conn.execute(
                        "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (key, value, updated),
                    )
        except Exception as exc:  # 兜底：状态刷新永不影响扫描
            logger.debug("刷新 qBittorrent 状态失败：%s", exc)

    def _run(self) -> None:
        """线程主循环。"""
        tick = self._tick_seconds()
        counter = 0
        while not self._stop_event.is_set():
            try:
                self.run_once()
                counter += 1
                # 每 5 个周期刷一次 qB 状态，避免过于频繁地打扰 qB API。
                if counter % 5 == 1:
                    self._refresh_qbittorrent()
            except Exception:
                logger.exception("扫描线程一轮执行失败")
            tick = self._tick_seconds()
            self._stop_event.wait(timeout=tick)
        logger.info("定时扫描线程已退出")


scanner = Scanner()
