"""SQLite 数据层（ARCHITECTURE §6.2 / §3）。

选 SQLite + WAL 的理由见 ARCHITECTURE §1：单文件、随数据卷持久化、无需额外容器。
连接用 ``check_same_thread=False`` + thread-local 缓存，因为队列工作线程与 FastAPI
线程池（SSE/日志轮询）都要读库；写操作统一走 ``tx()`` 事务。

**单工作线程**：``create_hard_links`` 会整份读-改-写日志文件，因此全局只允许一个工作线程
调用；本模块不额外加锁的前提就是“调用永远串行”。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .config import settings

logger = logging.getLogger(__name__)

_local = threading.local()

#: ``libraries`` 列定义。抽成常量是因为**迁移要重建这张表**（SQLite 无法删除
#: v1 的 ``CHECK`` 约束），重建时的 DDL 必须与新装库时的 DDL 逐字一致。
#: ``processor`` 是 v1 遗留列：NOT NULL 但不再有 CHECK，写入时由 ``processor_id`` 派生
#: （见 ``configstore._legacy_processor_column``），保留它只是为了让 v1 数据与旧查询不炸。
_LIBRARIES_COLUMNS = """
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL,
  processor_id TEXT NOT NULL,
  processor TEXT NOT NULL DEFAULT 'hardlink_only',
  enabled INTEGER NOT NULL DEFAULT 1,
  source_dir TEXT NOT NULL,
  target_dir TEXT NOT NULL,
  match_categories TEXT NOT NULL DEFAULT '[]',
  match_tags TEXT NOT NULL DEFAULT '[]',
  match_path_prefixes TEXT NOT NULL DEFAULT '[]',
  orphan_cleanup INTEGER NOT NULL DEFAULT 1,
  permission_mode INTEGER NOT NULL DEFAULT 0,
  log_file TEXT,
  rule_override TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL"""

#: ``todos``（待办看板）列定义。与 ``_LIBRARIES_COLUMNS`` 同理抽成常量：
#: 新装库由 ``SCHEMA`` 插值建表、老库由 ``migrate()`` 用**同一份** DDL 补建，两处逐字一致。
#:
#: 两处刻意的设计（不要"顺手修正"）：
#: * ``library_id`` / ``task_id`` **故意不加外键**。任务历史会被 ``trim_history()`` 裁掉、
#:   媒体库也可能被删（``tasks`` 就是 ``ON DELETE SET NULL``），而待办是"发生过什么"的台账，
#:   必须比它们活得久；外键会让台账在清理历史时被悄悄改写成 NULL 或直接 CASCADE 掉。
#: * ``status`` / ``severity`` **故意不加 CHECK 约束**。v1 的 ``libraries`` 正是被 CHECK 逼出
#:   整表重建（见 `_rebuild_libraries`），取值校验放在 ``app/todos.py`` 的代码里，加约束只会
#:   给未来的自己挖坑。
_TODOS_COLUMNS = """
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  severity TEXT NOT NULL DEFAULT 'warning',
  title TEXT NOT NULL,
  detail TEXT,
  library_id INTEGER,
  library_name TEXT,
  task_id INTEGER,
  task_kind TEXT,
  occurrences INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  resolved_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL"""

#: ``todos`` 的索引：``(status, kind)`` 给看板首屏分组用，``(kind, dedupe_key, status)`` 给
#: 去重（"同种问题同一条"）用，``(library_id)`` 给按库筛选用，``(last_seen_at DESC)`` 给排序用。
_TODOS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_todos_status_kind ON todos(status, kind);
CREATE INDEX IF NOT EXISTS idx_todos_dedupe ON todos(kind, dedupe_key, status);
CREATE INDEX IF NOT EXISTS idx_todos_library ON todos(library_id);
CREATE INDEX IF NOT EXISTS idx_todos_seen ON todos(last_seen_at DESC);"""

#: ``link_records``（每库「已执行作品」账本）列定义。与 ``_TODOS_COLUMNS`` 同样的理由抽成
#: 常量：新装库由 ``SCHEMA`` 插值建表、老库由 ``migrate()`` 用**同一份** DDL 补建，两处逐字一致。
#:
#: 列的含义（判据见 ``app/link_records.py``）：
#: * ``library_id`` / ``folder`` —— 账本主键的一半（唯一索引）；作品名就是建链阶段的**一级文件夹名**；
#: * ``source_dir`` / ``target_dir`` —— 参与跳过判断：换了源或换了目标，旧记录一律不算数；
#: * ``target_path`` —— 记录当时的落点，供 WebUI 展示与删除时核对；
#: * ``size`` —— 建链时那个文件夹的体积（与日志里的 ``Folder size`` 同源）；
#: * ``linked`` —— 本次实际新建的链接数（0 = 写了记录但一个都没新建，例如"体积变了但内容已存在"）。
#:
#: 刻意**不加外键**：库被删时由 ``app/routers/libraries.py`` 显式调 ``link_records.drop_by_library()``
#: 清掉，删除动作要看得见（同 ``todos`` 的理由）。
_LINK_RECORDS_COLUMNS = """
  id INTEGER PRIMARY KEY,
  library_id INTEGER NOT NULL,
  folder TEXT NOT NULL,
  source_dir TEXT NOT NULL DEFAULT '',
  target_dir TEXT NOT NULL DEFAULT '',
  target_path TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  linked INTEGER NOT NULL DEFAULT 0,
  first_at TEXT NOT NULL,
  updated_at TEXT NOT NULL"""

#: ``link_records`` 的唯一索引就是账本的身份：``(library_id, folder)`` 一行，重复写入只刷新不新增。
#: 第二个索引给「本库全部作品」的列表接口用。
_LINK_RECORDS_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_link_records_library_folder ON link_records(library_id, folder);
CREATE INDEX IF NOT EXISTS idx_link_records_library ON link_records(library_id, updated_at DESC);"""

# 注意：SCHEMA 是 f-string（要插值 `_LIBRARIES_COLUMNS`），所以 SQL 正文里凡是字面量花括号
# 都必须写成 `{{}}`——`DEFAULT '{}'` 会被 f-string 当成占位符而抛 SyntaxError。
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, created_at TEXT NOT NULL, last_login_at TEXT);

CREATE TABLE IF NOT EXISTS processors (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  builtin INTEGER NOT NULL DEFAULT 0,
  spec_json TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS libraries ({_LIBRARIES_COLUMNS});

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  library_id INTEGER REFERENCES libraries(id) ON DELETE SET NULL,
  library_name TEXT,
  kind TEXT NOT NULL,
  trigger TEXT NOT NULL,
  status TEXT NOT NULL,
  scope TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{{}}',
  result TEXT NOT NULL DEFAULT '{{}}',
  error TEXT,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);

CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS task_logs (
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  ts TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL, stream TEXT);
CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, id);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS config_files (
  path TEXT PRIMARY KEY, sha256 TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS config_events (
  id INTEGER PRIMARY KEY, path TEXT NOT NULL, direction TEXT NOT NULL,
  sha256 TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, note TEXT);
CREATE INDEX IF NOT EXISTS idx_config_events_id ON config_events(id DESC);

-- 待办看板：全局一份台账（去重语义见 app/todos.py）。不要删除或重排上面的任何表。
CREATE TABLE IF NOT EXISTS todos ({_TODOS_COLUMNS});
{_TODOS_INDEXES}

-- 每库「已执行作品」账本（建链阶段的跳过判据，见 app/link_records.py）。
-- 不要删除或重排上面的任何表。
CREATE TABLE IF NOT EXISTS link_records ({_LINK_RECORDS_COLUMNS});
{_LINK_RECORDS_INDEXES}
"""

DEFAULT_SETTINGS: dict[str, str] = {
    # 白名单根的**首次播种初值**来自 env（见 config.DEFAULT_SOURCE_ROOT 的说明）。
    # 已存在的 DB 不会被这里覆盖（DEFAULT_SETTINGS 只在 init_db 时 INSERT OR IGNORE）。
    "allowed_source_roots": json.dumps([settings.default_source_root]),
    "allowed_target_roots": json.dumps([settings.default_target_root]),
    "scan_enabled": "false",
    "scan_interval_seconds": "900",
    "qbittorrent_url": "",
    "qbittorrent_username": "",
    "qbittorrent_password": "",
    "log_level": "INFO",
    "orphan_cleanup_enabled": "true",
    # 全局重命名后缀白名单。`[]` = 未设置 ⇒ 各库沿用"库覆盖 → 处理器默认"。
    "rename_extensions": "[]",
    # L3 特典排除（取消"移入 其他/"）：全局特典判据（**正则**
    # 数组，一行一条）。与正片平级的散装特典文件命中后**只是被排除在改名之外** —— 保留原名、
    # 留在原位置，不改名、不移动、不建目录。`[]` = 关闭 L3。
    # 判据全局生效于每一个库，可在设置页修改并支持正则；`PUT /api/settings` 逐条校验能否编译。
    # 出厂默认 7 条。
    "extras_patterns": '["NCOP", "NCED", "Preview", "Creditless", "Info", "予告編", "CM集"]',
    "max_history": "2000",
    "max_task_log_lines": "5000",
    "config_sync_interval_seconds": "5",
    "config_last_import_at": "",
    "config_last_export_at": "",
    # 待办看板：启用的种类（逗号分隔的 kind），空串 = 全部启用（缺省行为）。
    # 非法/未知取值由 `todos.enabled_kinds()` 忽略并回退默认；这里不做校验，与其它设置项一致。
    "todo_kinds": "",
}


def now_iso() -> str:
    """统一时间戳格式（UTC，``...Z``）：任务排序、24h 统计都依赖它可字符串比较。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def hours_ago_iso(hours: int) -> str:
    """返回 ``hours`` 小时前的 ISO 时间戳，供 24h 统计用。"""
    moment = datetime.now(timezone.utc) - timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(raw: str | None) -> datetime | None:
    """把 ``now_iso()`` 之类的时间戳解析成 **带时区** 的 ``datetime``；失败返回 ``None``。

    容错要点：Python 3.11 以下的 ``fromisoformat`` 不认结尾的 ``Z``，所以先换成 ``+00:00``；
    库里也可能存着没有时区的历史值（那时按 UTC 解释，而不是按本地时区——时间戳在本项目里
    一律是 UTC）。调用方拿到的一定是 tz-aware 的，可以直接和 ``datetime.now(timezone.utc)`` 相减。
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def connect() -> sqlite3.Connection:
    """新建一个连接：``check_same_thread=False`` + WAL + ``row_factory=Row``。

    WAL 让 SSE/日志读取不被写事务阻塞；``Row`` 让上层可以按列名取值。
    """
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_conn() -> sqlite3.Connection:
    """取当前线程的连接（thread-local），不存在则创建。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect()
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """事务上下文：正常提交、异常回滚。所有写操作都应经由此处。"""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_setting(key: str, default: str | None = None) -> str | None:
    """读单个设置；不存在返回 ``default``。"""
    row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def set_setting(key: str, value: str) -> None:
    """写单个设置（upsert）。"""
    with tx() as conn:
        conn.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_iso()),
        )


def all_settings() -> dict[str, str]:
    """读全部设置（含 DEFAULT_SETTINGS 里尚未落库的键）。"""
    rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    result = dict(DEFAULT_SETTINGS)
    for row in rows:
        result[row["key"]] = row["value"]
    return result


def json_setting(key: str, default):
    """读一个 JSON 型设置并解码；解码失败时回退 ``default``。"""
    raw = get_setting(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def list_setting(key: str, default: list | None = None) -> list:
    """读一个 JSON 数组型设置（如 ``allowed_source_roots``），保证返回 ``list``。"""
    value = json_setting(key, default if default is not None else [])
    if isinstance(value, list):
        return [str(item) for item in value]
    return list(default or [])


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """``PRAGMA table_info`` 的列名集合；表不存在时返回空集合。"""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row["name"]) for row in rows}


def _rebuild_libraries(conn: sqlite3.Connection) -> None:
    """把旧库的 ``libraries`` 重建成当前形状（幂等，重建前会先判断是否需要）。

    SQLite 不能 ``ALTER TABLE ... DROP CONSTRAINT``，所以要：
    ``CREATE libraries_v2`` → ``INSERT ... SELECT`` → ``DROP libraries`` → ``RENAME``。
    ``tasks.library_id`` 的外键指向表名 ``libraries``，因此必须在重建期间
    ``PRAGMA legacy_alter_table=ON``（否则 RENAME 会把**别的表**里的引用改写成
    ``libraries_v2``，DROP 之后就指向不存在的表）并关掉 ``foreign_keys``。
    """
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute(f"CREATE TABLE libraries_v2 ({_LIBRARIES_COLUMNS})")
        conn.execute(
            "INSERT INTO libraries_v2 (id, name, type, processor_id, processor, enabled,"
            " source_dir, target_dir, match_categories, match_tags, match_path_prefixes,"
            " orphan_cleanup, permission_mode, log_file, rule_override, created_at, updated_at)"
            " SELECT id, name, type, processor, processor, enabled, source_dir, target_dir,"
            " match_categories, match_tags, match_path_prefixes, orphan_cleanup, permission_mode,"
            " NULL, NULL, created_at, updated_at FROM libraries"
        )
        conn.execute("DROP TABLE libraries")
        conn.execute("ALTER TABLE libraries_v2 RENAME TO libraries")
        conn.commit()
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
    try:
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error:
        broken = []
    if broken:
        logger.warning("迁移后发现外键问题（请检查 tasks.library_id）：%s", [tuple(r) for r in broken])


#: v1 的白名单根默认值（单值）。单挂载形态下容器里**不再存在** ``/downloads``
#: 与 ``/library``，而 v1 老库里的这两个设置项恰好就是它们 ⇒ 不迁移的话
#: ``downloads_readable``/``library_writable`` 恒为 false、容器开箱 unhealthy。
V1_DEFAULT_SOURCE_ROOT = "/downloads"
V1_DEFAULT_TARGET_ROOT = "/library"


def _rewrite_v1_default_root(key: str, v1_value: str, new_value: str) -> str | None:
    """把 ``key`` **恰好等于** v1 默认单值 ``[v1_value]`` 的存量值改写为 ``[new_value]``。

    只在逐字等于 v1 默认单值时才动手：用户改过的（多个根、或别的路径）**一个字符都不动**，
    否则会覆盖真实配置。解析不出 JSON 也一律不动。返回中文说明；未改动返回 ``None``。
    幂等：改写后值不再等于 v1 默认单值，重复调用不会再次改写。
    """
    raw = get_setting(key, None)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if parsed != [v1_value]:
        return None
    set_setting(key, json.dumps([new_value]))
    return (
        f"{key}: 已把 v1 默认根 {v1_value} 改写到新默认值 {new_value}"
        f"（仅当存量值恰好是 v1 默认单值时才改写）"
    )


def _ensure_todos_table(conn: sqlite3.Connection) -> str | None:
    """确保 ``todos``（待办看板）表存在；返回迁移说明，已存在则返回 ``None``。

    新装库由 ``SCHEMA`` 带出这张表，这里兜的是**只跑 ``migrate()`` 的路径**（测试、手工调用、
    以及将来若把建表从 ``init_db`` 里挪走时）——沿用 ``_bootstrap_v2()`` 的"启动时自愈"风格。

    幂等且**只增不改**：表已存在时一个字节都不动（里面是用户手动打钩的台账，重建会丢历史）。
    """
    if _table_columns(conn, "todos"):
        return None
    conn.executescript(f"CREATE TABLE IF NOT EXISTS todos ({_TODOS_COLUMNS});{_TODOS_INDEXES}")
    conn.commit()
    return "todos(待办看板)"


def _ensure_link_records_table(conn: sqlite3.Connection) -> str | None:
    """确保 ``link_records``（每库「已执行作品」账本）表存在；返回迁移说明，已存在则 ``None``。

    与 ``_ensure_todos_table`` 同一套路：新装库由 ``SCHEMA`` 带出，这里兜"只跑 ``migrate()``"的路径。
    **不迁移旧 ``hard_links.log``**：老的测试媒体库会删掉、新库重测，
    所以升级后账本从空开始（首轮会把该库的作品全部重新建链 —— 见 CHANGELOG 的升级提示）。
    """
    if _table_columns(conn, "link_records"):
        return None
    conn.executescript(
        f"CREATE TABLE IF NOT EXISTS link_records ({_LINK_RECORDS_COLUMNS});{_LINK_RECORDS_INDEXES}"
    )
    conn.commit()
    return "link_records(每库已执行作品账本)"


def migrate() -> list[str]:
    """把既有数据卷上的旧库就地升级到当前形状；返回实际执行的迁移步骤（幂等）。"""
    conn = get_conn()
    applied: list[str] = []

    columns = _table_columns(conn, "users")
    if columns and "last_login_at" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")
        conn.commit()
        applied.append("users.last_login_at")

    columns = _table_columns(conn, "libraries")
    if columns and ("processor_id" not in columns or "log_file" not in columns):
        _rebuild_libraries(conn)
        applied.append("libraries(v2)")

    # 每库规则覆盖：老库没有这一列，就地补；新库由 SCHEMA 带出。
    # 纯 ADD COLUMN（不是重建），所以老库的 16 列一个字节都不动。
    if columns and "rule_override" not in columns:
        conn.execute("ALTER TABLE libraries ADD COLUMN rule_override TEXT")
        conn.commit()
        applied.append("libraries.rule_override")
        columns = _table_columns(conn, "libraries")

    if _backfill_library_columns():
        applied.append("libraries.processor_id/log_file 回填")

    # 待办看板（todo-board）。老库在 init_db() 里由 SCHEMA 带出这张表；这里再兜一层，
    # 让"只跑 migrate()"的调用方也一定拿到它。
    todo_note = _ensure_todos_table(conn)
    if todo_note:
        applied.append(todo_note)

    # 每库「已执行作品」账本（link-record-ledger）。同上，SCHEMA 也会带出。
    ledger_note = _ensure_link_records_table(conn)
    if ledger_note:
        applied.append(ledger_note)

    # v1 默认根改写（有界、幂等、只碰逐字等于 v1 默认单值的存量值）。
    for key, v1_value, new_value in (
        ("allowed_source_roots", V1_DEFAULT_SOURCE_ROOT, settings.default_source_root),
        ("allowed_target_roots", V1_DEFAULT_TARGET_ROOT, settings.default_target_root),
    ):
        note = _rewrite_v1_default_root(key, v1_value, new_value)
        if note:
            applied.append(note)
    return applied


def _backfill_library_columns() -> int:
    """把 v1 行的 ``processor_id`` / ``log_file`` 补齐（重建后为 NULL）。

    * ``processor_id`` ← v1 的 ``processor``（取值恰好就是处理器 id）；
    * ``log_file`` ← 基名按处理器取 ``bangumicache.log``/``moviecache.log``/
      ``hardlink_only.log``，其它处理器按库名生成 ``{slug}cache.log``；
    * **按库 id 升序逐个去重**（``librarycache-2.log``、``bangumicache-2.log``…）。

    为什么要去重：日志里记的是「这个标题文件夹已经处理过、体积是多少」，两个库
    共用一个日志文件时，先跑的那个库会让后跑的库走「体积一致 ⇒ 已处理」分支被静默跳过。
    纯中文库名经 ``slugify`` 后都折叠成 ``library``，两个中文库就会这么撞上。

    已有非空 ``log_file``（历史库或本函数早先写过的）**原样保留**，只有与其它库撞名时才补
    序号：保住既有历史记录，避免升级后第一次运行对真实媒体库全量重跑。
    """
    from .processors.logs import slugify, unique_log_file_name

    fixed = 0
    with tx() as conn:
        rows = conn.execute(
            "SELECT id, name, processor, processor_id, log_file FROM libraries ORDER BY id ASC"
        ).fetchall()
        used: set[str] = set()
        for row in rows:
            processor_id = (row["processor_id"] or "").strip() or (row["processor"] or "").strip()
            if not processor_id:
                processor_id = "hardlink_only"
            current = (row["log_file"] or "").strip()
            if current:
                log_file = current
                if log_file in used:
                    log_file = unique_log_file_name(settings.log_dir, current, taken=used)
            else:
                base = {
                    "bangumi": "bangumicache.log",
                    "movie": "moviecache.log",
                    "hardlink_only": "hardlink_only.log",
                }.get(processor_id) or f"{slugify(str(row['name']) or processor_id)}cache.log"
                log_file = unique_log_file_name(settings.log_dir, base, taken=used)
            used.add(log_file)
            if processor_id != (row["processor_id"] or "") or log_file != (row["log_file"] or ""):
                conn.execute(
                    "UPDATE libraries SET processor_id = ?, log_file = ? WHERE id = ?",
                    (processor_id, log_file, row["id"]),
                )
                fixed += 1
    return fixed


def _bootstrap_v2() -> dict:
    """首次/升级启动的播种：三个内置处理器入库。

    唯一实现是 ``registry.ensure_builtins()``（它负责写 DB；``defaults`` 只提供
    spec 数据、不碰数据库）。惰性导入是**必须**的：``registry`` 反过来 import ``db``
    （读 libraries/processors 表），模块级导入会成环。
    """
    from .processors import registry

    return {"processors": registry.ensure_builtins()}


def init_db() -> None:
    """建表 + 迁移 + 写默认设置 + 播种内置处理器 + 按需播种管理员。

    幂等：可重复调用（应用启动、测试各调一次都安全）。"""
    settings.ensure_dirs()
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    applied = migrate()
    if applied:
        logger.info("数据库迁移：%s", "、".join(applied))

    with tx() as cur:
        stamp = now_iso()
        for key, value in DEFAULT_SETTINGS.items():
            cur.execute(
                "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?,?,?)",
                (key, value, stamp),
            )
        # hook_token 必须是随机的，且只生成一次。
        existing = cur.execute(
            "SELECT value FROM settings WHERE key='hook_token'"
        ).fetchone()
        if existing is None:
            import secrets

            cur.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?)",
                ("hook_token", secrets.token_urlsafe(32), stamp),
            )

    if settings.admin_username and settings.admin_password:
        from .security import hash_password

        with tx() as cur:
            row = cur.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            if row["n"] == 0:
                cur.execute(
                    "INSERT INTO users(username, password_hash, created_at) VALUES(?,?,?)",
                    (settings.admin_username, hash_password(settings.admin_password), now_iso()),
                )

    _bootstrap_v2()


def trim_history() -> int:
    """按 ``max_history`` 裁剪任务历史，返回删除条数（级联删除其日志）。"""
    try:
        limit = int(get_setting("max_history", "2000") or "2000")
    except ValueError:
        limit = 2000
    if limit <= 0:
        return 0
    with tx() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
        total = row["n"]
        if total <= limit:
            return 0
        doomed = conn.execute(
            "SELECT id FROM tasks ORDER BY id ASC LIMIT ?", (total - limit,)
        ).fetchall()
        ids = [r["id"] for r in doomed]
        conn.executemany("DELETE FROM tasks WHERE id=?", [(i,) for i in ids])
    if ids:
        # 日志清理要留痕 —— 删了多少、按哪条设置删的，都写进应用日志。
        logger.info(
            "按 max_history=%s 裁剪任务历史：删除最旧的 %s 条任务（它们的 task_logs 一并级联删除）。",
            limit,
            len(ids),
        )
    return len(ids)
