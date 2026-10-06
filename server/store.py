"""存储层 —— 账号、自选股、分组、虚拟盘。

支持两种后端，运行时由环境变量决定，代码与调用方完全一致：

    SQLite（默认）   数据落在本地 data/tick.db，单机自用、零外部依赖。
    MySQL 协议库     设 TICK_DB_HOST 即可，TiDB Cloud / MySQL / MariaDB 通用。

设计要点：
- 线程安全：FastAPI 请求跑在线程池里，故用 thread-local 连接。
  SQLite 走 WAL 避免 "database is locked"；MySQL 走独占 TCP 连接。
- 方言隔离：业务 SQL 统一用 `?` 占位符书写，MySQL 模式底层自动改写为 `%s`
  （跳过字符串字面量，避免误伤）。DDL、UPSERT、自增 ID 等少数方言
  差异集中在本文末尾的 _DDL / _UPSERT_ITEM / _new_row_id 处。
- 存储抽象：对外只暴露仓储函数（watchlists / items / users），
  调用方（app.py）完全不感知底层是哪种库。
- 幂等建表：initialize() 可在每次启动时安全调用，含版本号便于后续迁移。

表结构：
  users            账号
  sessions         登录会话
  folders          自选股分组（文件夹）
  watch_items      自选股条目（属于某个分组）
  positions        虚拟盘持仓
  trades           虚拟盘成交流水
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------------ 路径

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
DATA_DIR = os.path.join(_ROOT, "data")
DB_PATH = os.environ.get("TICK_DB_PATH") or os.path.join(DATA_DIR, "tick.db")

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

SCHEMA_VERSION = 2

# ------------------------------------------------------------------ 后端选择

# 只要给了主机，就走 MySQL 协议线（TiDB Cloud / MySQL / MariaDB）。
MYSQL_HOST = os.environ.get("TICK_DB_HOST", "").strip()
MYSQL_PORT = int(os.environ.get("TICK_DB_PORT") or "4000")
MYSQL_USER = os.environ.get("TICK_DB_USER", "").strip()
MYSQL_PASSWORD = os.environ.get("TICK_DB_PASSWORD", "")
MYSQL_DB = os.environ.get("TICK_DB_NAME", "").strip() or "tick"
MYSQL_SSL = os.environ.get("TICK_DB_SSL", "1").strip() not in ("0", "false", "no")

# 连接探活节流窗口（秒）。见 _conn() 的说明。
# TiDB Cloud Serverless 单次往返约 200ms，每次取连接都 ping 会显著拖慢批处理。
try:
    _PING_IDLE = max(0.0, float(os.environ.get("TICK_DB_PING_IDLE", "30")))
except ValueError:
    _PING_IDLE = 30.0

BACKEND = "mysql" if (MYSQL_HOST and MYSQL_USER) else "sqlite"
IS_MYSQL = BACKEND == "mysql"

if IS_MYSQL:
    import pymysql
    import pymysql.cursors
    IntegrityError = pymysql.err.IntegrityError
    OperationalError = pymysql.err.OperationalError
else:
    import sqlite3
    IntegrityError = sqlite3.IntegrityError
    OperationalError = sqlite3.OperationalError


# 旧库迁移：为已存在的表补列（列名 → 建表语句片段）
_MIGRATIONS = {
    "users": [
        ("pwd_hash", "TEXT    NOT NULL DEFAULT ''"),
        ("pwd_salt", "TEXT    NOT NULL DEFAULT ''"),
        ("cash", "REAL    NOT NULL DEFAULT 1000000.0"),
        # 虚拟盘费率：按用户各自持有，注册时固化为当时的默认值。
        # 老库补列时 DEFAULT 与运行默认一致，行为不变。
        ("fee_rate", "REAL NOT NULL DEFAULT 0.00025"),
        ("fee_min", "REAL NOT NULL DEFAULT 5.0"),
        ("stamp_rate", "REAL NOT NULL DEFAULT 0.0005"),
        ("etf_fee_rate", "REAL NOT NULL DEFAULT 0.00025"),
        ("etf_fee_min", "REAL NOT NULL DEFAULT 5.0"),
    ],
    "trades": [
        # 成交价来源：实时 / 收盘 / 昨收 / 手填。老库补空串（前端显示为「—」）。
        ("pspan", "TEXT NOT NULL DEFAULT ''"),
    ],
}

_MIGRATIONS_MYSQL = {
    "users": [
        ("pwd_hash", "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("pwd_salt", "VARCHAR(64)  NOT NULL DEFAULT ''"),
        ("cash", "DOUBLE NOT NULL DEFAULT 1000000.0"),
        ("fee_rate", "DOUBLE NOT NULL DEFAULT 0.00025"),
        ("fee_min", "DOUBLE NOT NULL DEFAULT 5.0"),
        ("stamp_rate", "DOUBLE NOT NULL DEFAULT 0.0005"),
        ("etf_fee_rate", "DOUBLE NOT NULL DEFAULT 0.00025"),
        ("etf_fee_min", "DOUBLE NOT NULL DEFAULT 5.0"),
    ],
    "trades": [
        ("pspan", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ],
}


def _conv_sql(sql: str) -> str:
    """SQLite 的 `?` 占位符改写为 MySQL 的 `%s`，跳过字符串字面量。

    业务 SQL 一律写 `?`，MySQL 模式在驱动层转换为 `%s`，
    这样同一份代码两套后端都能跑，也避免遗漏导致参数错位。

    注意：SQL 里天然存在的 `%`（例如 LIKE '%abc%'）必须先转义成 `%%`。
    pymysql 用 Python 的 `%` 格式化整条语句，不区分是否在引号内，
    不转义就会踩 "not enough arguments for format string"。
    顺序很关键：先转义 `%`，再把 `?` 换成 `%s`，后者就不再被处理。
    """
    if not IS_MYSQL or ("?" not in sql and "%" not in sql):
        return sql
    if "?" not in sql:
        return sql.replace("%", "%%")
    return sql.replace("%", "%%").replace("?", "%s")


def _table_cols(c, table: str) -> set:
    """取表的列名集合（SQLite 走 PRAGMA，MySQL 走 information_schema）。"""
    if IS_MYSQL:
        rows = c.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema=? AND table_name=?", (MYSQL_DB, table)).fetchall()
        return {r["name"] for r in rows}
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def _table_exists(c, table: str) -> bool:
    if IS_MYSQL:
        r = c.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=? AND table_name=?", (MYSQL_DB, table)).fetchone()
        return r is not None
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


def _migrate(c) -> None:
    """为老库补上新增的列。幂等：已存在的列会跳过。"""
    mig = _MIGRATIONS_MYSQL if IS_MYSQL else _MIGRATIONS
    for table, cols in mig.items():
        if not _table_exists(c, table):
            continue
        have = _table_cols(c, table)
        for name, decl in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    _commit(c)

# 名称解析钩子：由 app.py 在启动时注入（入参 code，返回中文简称或 None）。
# 这样存储层不依赖行情模块，保持分层干净、便于将来替换实现。
_name_resolver = None


def set_name_resolver(fn) -> None:
    """注入代码→名称的解析函数（app.py 启动时调用）。"""
    global _name_resolver
    _name_resolver = fn


def _guess_name(code: str) -> str:
    if not _name_resolver or not code:
        return ""
    try:
        return (_name_resolver(code) or "").strip()
    except Exception:
        return ""


_CODE_LIKE = re.compile(r"(?i)^(sh|sz|bj)?\d{6}$")


def is_code_like(s: str) -> bool:
    """「SH600519 / sz002342 / 002342」这类代码串不是名字，别当名字入库。"""
    return bool(_CODE_LIKE.match((s or "").strip()))


# ------------------------------------------------------------------ 连接

class _CursorProxy:
    """游标代理：统一参数占位符与取值方式。

    MySQL 模式下把业务 SQL 里的 `?` 改写为 `%s`，并把参数规整为 tuple，
    使上层写法与 SQLite 完全一致。
    """

    __slots__ = ("_c",)

    def __init__(self, cur):
        self._c = cur

    @staticmethod
    def _args(a):
        if a is None:
            return ()
        if isinstance(a, dict):
            return a
        return tuple(a)

    def execute(self, sql, args=None):
        return self._c.execute(_conv_sql(sql), self._args(args))

    def executemany(self, sql, seq):
        return self._c.executemany(_conv_sql(sql), [self._args(a) for a in seq])

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    def close(self):
        return self._c.close()

    def __iter__(self):
        return iter(self._c)

    @property
    def rowcount(self):
        return self._c.rowcount

    @property
    def lastrowid(self):
        return self._c.lastrowid


class _ConnProxy:
    """连接代理：给 Python DB-API 连接补一层统一的 execute 简写。"""

    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw

    def cursor(self):
        if IS_MYSQL:
            return _CursorProxy(self.raw.cursor(pymysql.cursors.DictCursor))
        return _CursorProxy(self.raw.cursor())

    def execute(self, sql, args=None):
        cur = self.cursor()
        cur.execute(sql, args)
        return cur

    def executemany(self, sql, seq):
        cur = self.cursor()
        cur.executemany(sql, seq)
        return cur

    def executescript(self, script: str):
        """拆成单条执行。SQLite 原生 executescript 会隐式提交，
        这里手动拆分并各自 execute，两种后端行为一致。
        """
        for raw_stmt in script.split(";"):
            stmt = "\n".join(
                line for line in raw_stmt.splitlines()
                if not line.strip().startswith("--")).strip()
            if stmt:
                self.execute(stmt)

    def ping(self):
        if IS_MYSQL:
            self.raw.ping(reconnect=True)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        try:
            self.raw.close()
        except Exception:
            pass


def _mysql_connect(database: Optional[str] = None):
    kwargs = dict(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
        password=MYSQL_PASSWORD, charset="utf8mb4", autocommit=False,
        connect_timeout=15, read_timeout=30, write_timeout=30,
    )
    if database:
        kwargs["database"] = database
    if MYSQL_SSL:
        # ca=None → 走系统默认证书链校验；check_hostname=False 兼容网关证书
        kwargs["ssl"] = {"ca": None, "check_hostname": False}
    return pymysql.connect(**kwargs)


def _ensure_database() -> None:
    """MySQL 模式下确保目标库存在（Serverless 初始只有 test 等系统库）。"""
    c = _mysql_connect()
    try:
        c.cursor().execute(
            f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DB}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
    finally:
        c.close()


_db_ready = False


def _new_conn():
    """建一个新连接并按后端做好会话设置。"""
    if IS_MYSQL:
        try:
            return _ConnProxy(_mysql_connect(MYSQL_DB))
        except Exception as e:
            # 1049 Unknown database：首次连接时目标库还不存在，建好再连一次
            if getattr(e, "args", None) and e.args[0] == 1049:
                _ensure_database()
                return _ConnProxy(_mysql_connect(MYSQL_DB))
            raise
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    raw = sqlite3.connect(DB_PATH, timeout=15.0)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")      # 读写并发更稳
    raw.execute("PRAGMA foreign_keys=ON")       # 级联删除生效
    raw.execute("PRAGMA busy_timeout=15000")
    return _ConnProxy(raw)


def _conn():
    """取当前线程的连接（不存在则建）。

    MySQL 空闲连接可能被网关回收，故取用前需要探活；但探活本身要一次
    网络往返。TiDB Cloud Serverless 的往返实测约 190~260ms，而一次
    落库流程会调用 _conn() 多次（upsert_bars、record_sync 各一次），
    每次都 ping 会凭空吃掉近 1 秒/只股票。

    因此这里做【ping 节流】：距上次成功 ping 未超过 _PING_IDLE 秒的连接
    直接复用，不再探活；超过才真的 ping 一次。窗口取 30s 是折中——
    短于网关通常的空闲回收时间，又足以让一次批处理只付出一次探活成本。
    可用 TICK_DB_PING_IDLE 覆盖（设为 0 则每次必 ping，回到旧行为）。
    """
    c = getattr(_local, "conn", None)
    if c is not None:
        if not IS_MYSQL:
            return c
        last = getattr(_local, "pinged_at", 0.0)
        if time.time() - last < _PING_IDLE:
            return c
        try:
            c.ping()
            _local.pinged_at = time.time()
            return c
        except Exception:
            try:
                c.close()
            except Exception:
                pass
            _local.conn = None
    c = _new_conn()
    _local.conn = c
    _local.pinged_at = time.time()
    return c


def _commit(c) -> None:
    c.commit()


# ------------------------------------------------------------------ 建表

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL UNIQUE,
    display     TEXT    NOT NULL DEFAULT '',
    pwd_hash    TEXT    NOT NULL DEFAULT '',
    pwd_salt    TEXT    NOT NULL DEFAULT '',
    cash        REAL    NOT NULL DEFAULT 1000000.0,
    fee_rate      REAL NOT NULL DEFAULT 0.00025,
    fee_min       REAL NOT NULL DEFAULT 5.0,
    stamp_rate    REAL NOT NULL DEFAULT 0.0005,
    etf_fee_rate  REAL NOT NULL DEFAULT 0.00025,
    etf_fee_min   REAL NOT NULL DEFAULT 5.0,
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  REAL    NOT NULL,
    expires_at  REAL    NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    sort        INTEGER NOT NULL DEFAULT 0,
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    UNIQUE(user_id, name),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS watch_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id   INTEGER NOT NULL,
    code        TEXT    NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT '',
    sort        INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    UNIQUE(folder_id, code),
    FOREIGN KEY(folder_id) REFERENCES folders(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    code        TEXT    NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    qty         INTEGER NOT NULL DEFAULT 0,
    cost        REAL    NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    UNIQUE(user_id, code),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    code        TEXT    NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    side        TEXT    NOT NULL,
    qty         INTEGER NOT NULL,
    price       REAL    NOT NULL,
    fee         REAL    NOT NULL DEFAULT 0,
    amount      REAL    NOT NULL,
    pnl         REAL,
    pspan       TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_folders_user   ON folders(user_id);
CREATE INDEX IF NOT EXISTS idx_items_folder   ON watch_items(folder_id);
CREATE INDEX IF NOT EXISTS idx_items_code     ON watch_items(code);
CREATE INDEX IF NOT EXISTS idx_sessions_user  ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id);
CREATE INDEX IF NOT EXISTS idx_trades_user    ON trades(user_id, created_at DESC);

-- #96 监控中心：规则（结构化条件，字段/算子白名单，绝不 eval）
CREATE TABLE IF NOT EXISTS alert_rules (
    id           TEXT PRIMARY KEY,
    name         TEXT    NOT NULL,
    kind         TEXT    NOT NULL,          -- price|signal|market|strategy
    code         TEXT    NOT NULL DEFAULT '',
    conds        TEXT    NOT NULL DEFAULT '[]',
    logic        TEXT    NOT NULL DEFAULT 'AND',
    severity     TEXT    NOT NULL DEFAULT 'warn',
    cooldown_min INTEGER NOT NULL DEFAULT 60,
    push         INTEGER NOT NULL DEFAULT 1,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_fired   REAL    NOT NULL DEFAULT 0,
    fired_count  INTEGER NOT NULL DEFAULT 0,
    created_at   REAL    NOT NULL,
    params       TEXT    NOT NULL DEFAULT '{}'
);

-- #96 告警流：ts 作主键（毫秒级），保留 7 天 / 5000 条
CREATE TABLE IF NOT EXISTS alerts (
    ts        REAL PRIMARY KEY,
    rule_id   TEXT NOT NULL,
    rule_name TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL,
    code      TEXT NOT NULL DEFAULT '',
    severity  TEXT NOT NULL DEFAULT 'warn',
    msg       TEXT NOT NULL,
    value     REAL,
    is_read   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_read ON alerts(is_read, ts DESC);

-- 网格计划（虚拟盘内的实盘网格）：档位由 grid.build_levels 实时算，不落库；
-- fired 只记已成交过的档位下标，避免刷新后重复提示
CREATE TABLE IF NOT EXISTS grids (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    code         TEXT    NOT NULL,
    name         TEXT    NOT NULL DEFAULT '',
    center_price REAL    NOT NULL,
    upper_price  REAL    NOT NULL,
    lower_price  REAL    NOT NULL,
    step_pct     REAL    NOT NULL DEFAULT 2.0,
    mode         TEXT    NOT NULL DEFAULT 'arith',
    lot          INTEGER NOT NULL DEFAULT 10000,
    fired        TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'active',
    created_at   REAL    NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- 日线历史行情（供选股/回测做真历史计算，避免每次请求实时拉取）
-- 主键用 (code, date) 而非自增 id：天然去重，重复入库幂等
CREATE TABLE IF NOT EXISTS daily_bars (
    code        TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        REAL,
    close       REAL,
    high        REAL,
    low         REAL,
    volume      REAL,
    amount      REAL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (code, date)
);

CREATE INDEX IF NOT EXISTS idx_bars_code_date ON daily_bars(code, date DESC);
CREATE INDEX IF NOT EXISTS idx_bars_date      ON daily_bars(date);

-- 落库任务进度（记录每只股票已同步到哪一天，支持增量拉取）
CREATE TABLE IF NOT EXISTS bar_sync (
    code        TEXT PRIMARY KEY,
    last_date   TEXT NOT NULL DEFAULT '',
    bars        INTEGER NOT NULL DEFAULT 0,
    synced_at   REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ok',
    err         TEXT NOT NULL DEFAULT ''
);

-- 选股历史：小白/策略/条件每次执行后自动存档，便于过后回看胜率（纯统计，与虚拟盘隔离）
-- user_id 不挂外键：匿名用户统一存 0，单机自用不分用户；预留列便于以后按用户隔离
CREATE TABLE IF NOT EXISTS screen_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL DEFAULT 0,
    module      TEXT    NOT NULL,          -- newbie | strategy | screen
    title       TEXT    NOT NULL DEFAULT '',
    params_json TEXT    NOT NULL DEFAULT '{}',
    items_json  TEXT    NOT NULL DEFAULT '[]',
    item_count  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screen_hist_user ON screen_history(user_id, created_at DESC);
"""

# MySQL / TiDB 版：自增改 AUTO_INCREMENT，索引列一律 VARCHAR
# （MySQL 不允许在无限长 TEXT 上建唯一键），REAL 改 DOUBLE。
_SCHEMA_MYSQL = """
CREATE TABLE IF NOT EXISTS users (
    id          BIGINT PRIMARY KEY AUTO_INCREMENT,
    username    VARCHAR(64)  NOT NULL,
    display     VARCHAR(64)  NOT NULL DEFAULT '',
    pwd_hash    VARCHAR(255) NOT NULL DEFAULT '',
    pwd_salt    VARCHAR(64)  NOT NULL DEFAULT '',
    cash        DOUBLE NOT NULL DEFAULT 1000000.0,
    fee_rate      DOUBLE NOT NULL DEFAULT 0.00025,
    fee_min       DOUBLE NOT NULL DEFAULT 5.0,
    stamp_rate    DOUBLE NOT NULL DEFAULT 0.0005,
    etf_fee_rate  DOUBLE NOT NULL DEFAULT 0.00025,
    etf_fee_min   DOUBLE NOT NULL DEFAULT 5.0,
    created_at  DOUBLE NOT NULL,
    UNIQUE KEY uk_users_username(username)
);

CREATE TABLE IF NOT EXISTS sessions (
    token       VARCHAR(191) PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    created_at  DOUBLE NOT NULL,
    expires_at  DOUBLE NOT NULL,
    KEY idx_sessions_expires(expires_at),
    KEY idx_sessions_user(user_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS folders (
    id          BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id     BIGINT NOT NULL,
    name        VARCHAR(64) NOT NULL,
    sort        INT    NOT NULL DEFAULT 0,
    is_default  TINYINT NOT NULL DEFAULT 0,
    created_at  DOUBLE NOT NULL,
    UNIQUE KEY uk_folders_user_name(user_id, name),
    KEY idx_folders_user(user_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS watch_items (
    id          BIGINT PRIMARY KEY AUTO_INCREMENT,
    folder_id   BIGINT NOT NULL,
    code        VARCHAR(32) NOT NULL,
    name        VARCHAR(64) NOT NULL DEFAULT '',
    note        VARCHAR(512) NOT NULL DEFAULT '',
    sort        INT    NOT NULL DEFAULT 0,
    created_at  DOUBLE NOT NULL,
    UNIQUE KEY uk_items_folder_code(folder_id, code),
    KEY idx_items_folder(folder_id),
    KEY idx_items_code(code),
    FOREIGN KEY(folder_id) REFERENCES folders(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS positions (
    id          BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id     BIGINT NOT NULL,
    code        VARCHAR(32) NOT NULL,
    name        VARCHAR(64) NOT NULL DEFAULT '',
    qty         BIGINT NOT NULL DEFAULT 0,
    cost        DOUBLE NOT NULL DEFAULT 0,
    created_at  DOUBLE NOT NULL,
    updated_at  DOUBLE NOT NULL,
    UNIQUE KEY uk_positions_user_code(user_id, code),
    KEY idx_positions_user(user_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trades (
    id          BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id     BIGINT NOT NULL,
    code        VARCHAR(32) NOT NULL,
    name        VARCHAR(64) NOT NULL DEFAULT '',
    side        VARCHAR(8)  NOT NULL,
    qty         BIGINT NOT NULL,
    price       DOUBLE NOT NULL,
    fee         DOUBLE NOT NULL DEFAULT 0,
    amount      DOUBLE NOT NULL,
    pnl         DOUBLE NULL,
    pspan       VARCHAR(16) NOT NULL DEFAULT '',
    created_at  DOUBLE NOT NULL,
    KEY idx_trades_user(user_id, created_at DESC),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- #96 监控中心（MySQL 版：TEXT 不建索引，REAL 改 DOUBLE）
CREATE TABLE IF NOT EXISTS alert_rules (
    id           VARCHAR(32) PRIMARY KEY,
    name         VARCHAR(128) NOT NULL,
    kind         VARCHAR(16)  NOT NULL,
    code         VARCHAR(32)  NOT NULL DEFAULT '',
    conds        TEXT NULL,
    logic        VARCHAR(8)   NOT NULL DEFAULT 'AND',
    severity     VARCHAR(16)  NOT NULL DEFAULT 'warn',
    cooldown_min BIGINT NOT NULL DEFAULT 60,
    push         BIGINT NOT NULL DEFAULT 1,
    enabled      BIGINT NOT NULL DEFAULT 1,
    last_fired   DOUBLE NOT NULL DEFAULT 0,
    fired_count  BIGINT NOT NULL DEFAULT 0,
    created_at   DOUBLE NOT NULL,
    params       TEXT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    ts        DOUBLE PRIMARY KEY,
    rule_id   VARCHAR(32) NOT NULL,
    rule_name VARCHAR(128) NOT NULL DEFAULT '',
    kind      VARCHAR(16) NOT NULL,
    code      VARCHAR(32) NOT NULL DEFAULT '',
    severity  VARCHAR(16) NOT NULL DEFAULT 'warn',
    msg       TEXT NULL,
    value     DOUBLE NULL,
    is_read   BIGINT NOT NULL DEFAULT 0,
    KEY idx_alerts_read(is_read, ts)
);

CREATE TABLE IF NOT EXISTS grids (
    id           BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id      BIGINT NOT NULL,
    code         VARCHAR(32) NOT NULL,
    name         VARCHAR(64) NOT NULL DEFAULT '',
    center_price DOUBLE NOT NULL,
    upper_price  DOUBLE NOT NULL,
    lower_price  DOUBLE NOT NULL,
    step_pct     DOUBLE NOT NULL DEFAULT 2.0,
    mode         VARCHAR(8)  NOT NULL DEFAULT 'arith',
    lot          BIGINT NOT NULL DEFAULT 10000,
    fired        TEXT NULL,
    status       VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at   DOUBLE NOT NULL,
    KEY idx_grids_user(user_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meta (
    k VARCHAR(64) PRIMARY KEY,
    v VARCHAR(64) NOT NULL
);

-- 日线历史行情；主键 (code, date) 保证重复入库幂等
CREATE TABLE IF NOT EXISTS daily_bars (
    code        VARCHAR(32) NOT NULL,
    date        VARCHAR(16) NOT NULL,
    open        DOUBLE NULL,
    close       DOUBLE NULL,
    high        DOUBLE NULL,
    low         DOUBLE NULL,
    volume      DOUBLE NULL,
    amount      DOUBLE NULL,
    updated_at  DOUBLE NOT NULL,
    PRIMARY KEY (code, date),
    KEY idx_bars_code_date(code, date),
    KEY idx_bars_date(date)
);

-- 落库进度：记录每只股票已同步到哪一天，支持增量拉取
CREATE TABLE IF NOT EXISTS bar_sync (
    code        VARCHAR(32) PRIMARY KEY,
    last_date   VARCHAR(16) NOT NULL DEFAULT '',
    bars        BIGINT NOT NULL DEFAULT 0,
    synced_at   DOUBLE NOT NULL,
    status      VARCHAR(16) NOT NULL DEFAULT 'ok',
    err         VARCHAR(512) NOT NULL DEFAULT ''
);

-- 选股历史（见 SQLite 版注释；user_id 不挂外键，匿名统一 0）
CREATE TABLE IF NOT EXISTS screen_history (
    id          BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id     BIGINT  NOT NULL DEFAULT 0,
    module      VARCHAR(16) NOT NULL,
    title       VARCHAR(255) NOT NULL DEFAULT '',
    params_json TEXT    NOT NULL DEFAULT '{}',
    items_json  TEXT    NOT NULL DEFAULT '[]',
    item_count  BIGINT  NOT NULL DEFAULT 0,
    created_at  DOUBLE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screen_hist_user ON screen_history(user_id, created_at DESC);
"""


def _new_row_id(c) -> int:
    """取上一条 INSERT 的自增 ID。SQLite 与 MySQL 函数名不同。"""
    if IS_MYSQL:
        return int(c.execute("SELECT LAST_INSERT_ID() AS i").fetchone()["i"])
    return int(c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])


def _begin(c) -> None:
    """开启事务。

    MySQL 下不做额外动作：pymysql 默认 autocommit=0，首条语句即隐式开事务，
    实际串行化靠调用点的 SELECT ... FOR UPDATE 行锁。
    """
    if not IS_MYSQL:
        c.execute("BEGIN IMMEDIATE")


# ===========================================================================
# 选股历史（小白 / 策略 / 条件 每次执行后自动存档；纯统计，与虚拟盘隔离）
# ===========================================================================

def save_screen_history(module: str, title: str, params: Dict[str, Any],
                        items: List[Dict[str, Any]],
                        user_id: Optional[int] = None) -> int:
    """存档一次选股结果。返回新记录 id。items 每项至少含 code。

    user_id 为 None 时取当前请求用户（未登录回落到默认本地账号），
    与自选股/虚拟盘保持同一套归属规则。
    """
    store_initialize = initialize
    store_initialize()
    uid = current_user_id() if user_id is None else int(user_id)
    c = _conn()
    c.execute(
        "INSERT INTO screen_history(user_id, module, title, params_json, "
        "items_json, item_count, created_at) VALUES(?,?,?,?,?,?,?)",
        (uid, module, title or "",
         json.dumps(params or {}, ensure_ascii=False),
         json.dumps(items or [], ensure_ascii=False),
         len(items or []), time.time()),
    )
    c.commit()
    return _new_row_id(c)


def list_screen_history(module: Optional[str] = None,
                        limit: int = 200,
                        user_id: Optional[int] = None,
                        ) -> List[Dict[str, Any]]:
    """列出选股历史（按时间倒序）。module 可过滤 newbie/strategy/screen。

    user_id 为 None 时不做归属过滤（管理员看全量）；否则严格按 user_id 隔离，
    匿名存档的 user_id 为 0。
    """
    initialize()
    c = _conn()
    where, args = [], []
    if user_id is not None:
        where.append("user_id=?")
        args.append(int(user_id))
    if module:
        where.append("module=?")
        args.append(module)
    sql = ("SELECT id, user_id, module, title, item_count, created_at "
           "FROM screen_history ")
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY created_at DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def count_screen_history(user_id: Optional[int] = None) -> Dict[str, Any]:
    """选股历史统计：总数 / 按模块分布 / 未归属(user_id=0)条数。管理员概览用。"""
    initialize()
    c = _conn()
    if user_id is None:
        rows = c.execute(
            "SELECT module, COUNT(*) AS n FROM screen_history GROUP BY module"
        ).fetchall()
        orphan = c.execute(
            "SELECT COUNT(*) AS n FROM screen_history WHERE user_id=0"
        ).fetchone()["n"]
    else:
        rows = c.execute(
            "SELECT module, COUNT(*) AS n FROM screen_history WHERE user_id=? "
            "GROUP BY module", (int(user_id),)).fetchall()
        orphan = 0
    by_module = {r["module"]: int(r["n"]) for r in rows}
    return {"total": sum(by_module.values()), "by_module": by_module,
            "orphan": int(orphan)}


def reassign_screen_history(to_user_id: int,
                            from_user_id: int = 0) -> int:
    """把某批历史记录改挂到指定用户名下（默认搬 user_id=0 的孤儿记录）。

    用于老数据迁移：隔离改造前存档的记录 user_id 全是 0，登录后看不见，
    管理员可一键认领到自己/目标用户账户。返回迁移条数。
    """
    initialize()
    c = _conn()
    cur = c.execute("UPDATE screen_history SET user_id=? WHERE user_id=?",
                    (int(to_user_id), int(from_user_id)))
    c.commit()
    return int(cur.rowcount)


def get_screen_history(hid: int) -> Optional[Dict[str, Any]]:
    """取单条历史（含 params / items 解析）。不存在返回 None。"""
    initialize()
    c = _conn()
    r = c.execute(
        "SELECT id, user_id, module, title, params_json, items_json, "
        "item_count, created_at FROM screen_history WHERE id=?", (hid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["params"] = json.loads(d.pop("params_json") or "{}")
    except Exception:
        d["params"] = {}
    try:
        d["items"] = json.loads(d.pop("items_json") or "[]")
    except Exception:
        d["items"] = []
    return d


def initialize() -> Dict[str, Any]:
    """建表 + 迁移 + 保证存在一个默认账号与默认分组。幂等。"""
    global _initialized, _db_ready
    with _init_lock:
        if IS_MYSQL and not _db_ready:
            _ensure_database()      # Serverless 首次连接时目标库可能还不存在
            _db_ready = True
        schema = _SCHEMA_MYSQL if IS_MYSQL else _SCHEMA_SQLITE
        c = _conn()
        c.executescript(schema)
        c.commit()
        _migrate(c)                      # 老库补列

        # 版本记录（升级时更新）
        cur = c.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        if cur is None:
            c.execute("INSERT INTO meta(k,v) VALUES('schema_version',?)",
                      (str(SCHEMA_VERSION),))
        elif str(cur["v"]) != str(SCHEMA_VERSION):
            c.execute("UPDATE meta SET v=? WHERE k='schema_version'",
                      (str(SCHEMA_VERSION),))
        c.commit()

        # 默认账号（本地验证用；未登录时数据归属此账号）
        uid = _ensure_default_user(c)
        _ensure_default_folder(c, uid)
        c.commit()

        # 数据迁移（只跑一次，靠 meta 记标记）：选股历史改为「按账户归属」
        # 之前，所有记录 user_id 都是 0。这里统一挂到默认本地账号，避免升级
        # 后旧记录凭空消失（管理员后续可用 reassign_screen_history 改挂他人）。
        try:
            r = c.execute("SELECT v FROM meta WHERE k='screen_hist_owner'").fetchone()
            if r is None:
                c.execute("UPDATE screen_history SET user_id=? WHERE user_id=0",
                          (uid,))
                c.execute("INSERT INTO meta(k,v) VALUES('screen_hist_owner',?)",
                          (str(uid),))
                c.commit()
        except Exception as e:                       # 迁移失败不影响启动
            print(f"[store] screen_history 归属迁移跳过: {e}")

        _initialized = True

        # 进程内缓存默认 uid：避免每个请求都回源查一次云端（省 ~190ms）
        global _default_uid
        _default_uid = uid

        return {"db": _target_label(), "schema": SCHEMA_VERSION, "default_user": uid}


def _target_label() -> str:
    return f"mysql://{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}" if IS_MYSQL else DB_PATH


_default_uid: Optional[int] = None


def _ensure_default_user(c) -> int:
    r = c.execute("SELECT id FROM users WHERE username='local'").fetchone()
    if r:
        return int(r["id"])
    c.execute("INSERT INTO users(username,display,created_at) VALUES(?,?,?)",
              ("local", "本地用户", time.time()))
    c.commit()
    return _new_row_id(c)


def _ensure_default_folder(c, uid: int) -> int:
    r = c.execute(
        "SELECT id FROM folders WHERE user_id=? AND is_default=1", (uid,)
    ).fetchone()
    if r:
        return int(r["id"])
    c.execute(
        "INSERT INTO folders(user_id,name,sort,is_default,created_at) VALUES(?,?,?,?,?)",
        (uid, "我的自选", 0, 1, time.time()),
    )
    c.commit()
    return _new_row_id(c)


# 请求级用户身份：由 API 层在每次请求开始时设置（中间件），
# 存储层据此判断数据归属。未设置时回落到默认本地账号。
_current_uid: "contextvars.ContextVar[Optional[int]]" = contextvars.ContextVar(
    "tick_current_uid", default=None)


def set_current_user(uid: Optional[int]) -> None:
    """设置本请求的用户 ID（None 表示匿名 → 回落默认账号）。"""
    _current_uid.set(uid)


def current_user_id() -> int:
    """当前用户 ID。

    优先取本请求注入的登录用户；未登录时回落到默认本地账号，
    保证单机自用场景（不登录）照样能用自选股。
    """
    if not _initialized:
        initialize()
    uid = _current_uid.get()
    if uid:
        return int(uid)
    if _default_uid is not None:
        return int(_default_uid)      # 云端场景省掉一次回源查询
    c = _conn()
    return _ensure_default_user(c)


def current_user_is_anonymous() -> bool:
    """本请求是否未登录。

    与 current_user_id() 的区别：后者在匿名时会回落到 local 默认账号，
    所以「返回的是不是默认账号」不能直接代表「是不是匿名」。这里直接看
    中间件有没有注入真实登录用户，才是「真的没登录」。

    用途：盘前竞价等以「我的」为名的接口，匿名访客没有真正的自选，
    不该把 local 默认账号的私有自选当成「我的」展示出来。
    """
    return _current_uid.get() is None


# ------------------------------------------------------------------ 账号

# 密码哈希：标准库 pbkdf2_hmac，零新依赖。
# 迭代 20 万次（2026 年的合理强度），每个用户独立随机盐。
_PBKDF2_ITER = 200_000
SESSION_TTL = 60 * 60 * 24 * 30      # 会话 30 天

_INITIAL_CASH = 1_000_000.0           # 虚拟盘初始资金


def _hash_pwd(pwd: str, salt: str) -> str:
    import hashlib
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"),
                             salt.encode("utf-8"), _PBKDF2_ITER)
    return dk.hex()


def _new_salt() -> str:
    import secrets
    return secrets.token_hex(16)


def register(username: str, password: str, display: str = "") -> Dict[str, Any]:
    """注册新账号。密码强度：至少 6 位。"""
    username = (username or "").strip()
    if not username:
        raise ValueError("用户名不能为空")
    if len(username) > 24:
        raise ValueError("用户名最长 24 个字符")
    if not password or len(password) < 6:
        raise ValueError("密码至少 6 位")

    salt = _new_salt()
    ph = _hash_pwd(password, salt)
    c = _conn()
    try:
        # 费率在注册这一刻固化：之后再改默认值不影响已开户的人，
        # 各用户之间也互不干扰。
        _f = default_fees()
        c.execute(
            """INSERT INTO users(username,display,pwd_hash,pwd_salt,cash,
                                 fee_rate,fee_min,stamp_rate,etf_fee_rate,etf_fee_min,
                                 created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (username, display or username, ph, salt, _INITIAL_CASH,
             _f["fee_rate"], _f["fee_min"], _f["stamp_rate"],
             _f["etf_fee_rate"], _f["etf_fee_min"], time.time()))
        c.commit()
    except IntegrityError:
        raise ValueError(f"用户名已存在：{username}")

    uid = _new_row_id(c)
    _ensure_default_folder(c, uid)
    c.commit()
    return {"id": uid, "username": username, "display": display or username,
            "cash": _INITIAL_CASH}


def verify_login(username: str, password: str) -> Optional[Dict[str, Any]]:
    """校验用户名密码。成功返回用户信息，失败返回 None。"""
    username = (username or "").strip()
    c = _conn()
    r = c.execute(
        "SELECT id,username,display,pwd_hash,pwd_salt,cash FROM users WHERE username=?",
        (username,)).fetchone()
    if not r or not r["pwd_hash"]:
        return None
    if _hash_pwd(password or "", r["pwd_salt"]) != r["pwd_hash"]:
        return None
    return {"id": int(r["id"]), "username": r["username"],
            "display": r["display"], "cash": float(r["cash"])}


def create_session(user_id: int) -> str:
    """签发会话 token。"""
    import secrets
    tok = secrets.token_urlsafe(32)
    now = time.time()
    c = _conn()
    c.execute("INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
              (tok, int(user_id), now, now + SESSION_TTL))
    c.commit()
    return tok


def user_by_token(token: str) -> Optional[Dict[str, Any]]:
    """按 token 取用户。过期或无效返回 None，并顺手清理过期会话。"""
    if not token:
        return None
    c = _conn()
    now = time.time()
    c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))   # 惰性清理
    c.commit()
    r = c.execute(
        """SELECT u.id,u.username,u.display,u.cash
           FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token=? AND s.expires_at >= ?""", (token, now)).fetchone()
    if not r:
        return None
    return {"id": int(r["id"]), "username": r["username"],
            "display": r["display"], "cash": float(r["cash"])}


def logout(token: str) -> bool:
    c = _conn()
    n = c.execute("DELETE FROM sessions WHERE token=?", (token,)).rowcount
    c.commit()
    return n > 0


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    c = _conn()
    r = c.execute("SELECT id,username,display,cash,created_at FROM users WHERE id=?",
                  (int(user_id),)).fetchone()
    return dict(r) if r else None


def create_user(username: str, display: str = "") -> Dict[str, Any]:
    """旧接口：建一个无密码账号（供内部/测试用）。"""
    c = _conn()
    try:
        c.execute("INSERT INTO users(username,display,created_at) VALUES(?,?,?)",
                  (username.strip(), display or username, time.time()))
        c.commit()
    except IntegrityError:
        raise ValueError(f"用户名已存在：{username}")
    uid = _new_row_id(c)
    _ensure_default_folder(c, uid)
    c.commit()
    return {"id": uid, "username": username, "display": display or username}


def list_users() -> List[Dict[str, Any]]:
    c = _conn()
    return [dict(r) for r in c.execute(
        """SELECT u.id,u.username,u.display,u.cash,u.created_at,
                  (SELECT COUNT(*) FROM watch_items w
                     JOIN folders f ON f.id=w.folder_id
                    WHERE f.user_id=u.id) AS watch_cnt,
                  (SELECT COUNT(*) FROM positions p WHERE p.user_id=u.id) AS pos_cnt
           FROM users u ORDER BY u.id""").fetchall()]


# ------------------------------------------------------------------ 管理页专用

def set_password(user_id: int, password: str) -> bool:
    """管理员重置他人密码。空密码拒绝（沿用 register 的最低要求）。"""
    if not password or len(password) < 2:
        raise ValueError("密码至少 2 个字符")
    salt = _new_salt()
    c = _conn()
    cur = c.execute("UPDATE users SET pwd_hash=?,pwd_salt=? WHERE id=?",
                    (_hash_pwd(password, salt), salt, int(user_id)))
    c.commit()
    return cur.rowcount > 0


def delete_user(user_id: int) -> bool:
    """删除用户。关联的 sessions/folders/watch_items/positions/trades
    依赖外键 ON DELETE CASCADE 自动清理（SQLite 需 PRAGMA foreign_keys=ON，
    见 _conn；MySQL 原生支持）。

    拒绝删除最后一个用户——否则无人能再登录，只能去手动改库。
    """
    c = _conn()
    n = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    if n and int(n["n"]) <= 1:
        raise ValueError("不能删除最后一个用户（否则无人能登录）")
    cur = c.execute("DELETE FROM users WHERE id=?", (int(user_id),))
    c.commit()
    return cur.rowcount > 0


def purge_sessions(user_id: int) -> int:
    """踢下线：清掉某用户所有会话。改密码后调用。"""
    c = _conn()
    cur = c.execute("DELETE FROM sessions WHERE user_id=?", (int(user_id),))
    c.commit()
    return cur.rowcount


# meta 键值存取。配置项（数据源参数等）落这里，读时优先于环境变量。
# 注意：_conn() 在 SQLite 下是 thread-local，管理页在请求线程里调用是安全的。

def meta_get(k: str, default: Optional[str] = None) -> Optional[str]:
    c = _conn()
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(k: str, v: str) -> None:
    c = _conn()
    c.execute("REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))
    c.commit()


def meta_del(k: str) -> bool:
    """删掉覆盖值，回落到环境变量默认。"""
    c = _conn()
    cur = c.execute("DELETE FROM meta WHERE k=?", (k,))
    c.commit()
    return cur.rowcount > 0


def meta_all(prefix: str = "") -> Dict[str, str]:
    c = _conn()
    if prefix:
        rows = c.execute("SELECT k,v FROM meta WHERE k LIKE ?",
                         (prefix + "%",)).fetchall()
    else:
        rows = c.execute("SELECT k,v FROM meta").fetchall()
    return {r["k"]: r["v"] for r in rows}


def data_summary() -> Dict[str, Any]:
    """管理页「数据落库」卡片用的一次性汇总，避免前端串多个请求。"""
    try:
        cov = bars_coverage()
    except Exception as e:
        cov = {"codes": 0, "rows": 0, "start": None, "end": None, "error": str(e)}
    users = 0
    try:
        c = _conn()
        users = int(c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])
    except Exception:
        pass
    return {"users": users, "bars": cov}


# ------------------------------------------------------------------ 分组

def list_folders(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    rows = c.execute(
        """SELECT f.id, f.name, f.sort, f.is_default, f.created_at,
                  (SELECT COUNT(*) FROM watch_items w WHERE w.folder_id=f.id) AS cnt
             FROM folders f WHERE f.user_id=?
            ORDER BY f.is_default DESC, f.sort ASC, f.id ASC""",
        (uid,),
    ).fetchall()
    return [dict(r) for r in rows]


def create_folder(name: str, user_id: Optional[int] = None) -> Dict[str, Any]:
    uid = user_id if user_id is not None else current_user_id()
    name = (name or "").strip()
    if not name:
        raise ValueError("分组名称不能为空")
    if len(name) > 30:
        raise ValueError("分组名称过长（最多 30 字）")
    c = _conn()
    nxt = c.execute("SELECT COALESCE(MAX(sort),0)+1 AS s FROM folders WHERE user_id=?",
                    (uid,)).fetchone()["s"]
    try:
        c.execute("INSERT INTO folders(user_id,name,sort,is_default,created_at) "
                  "VALUES(?,?,?,0,?)", (uid, name, nxt, time.time()))
        c.commit()
    except IntegrityError:
        raise ValueError(f"分组已存在：{name}")
    fid = _new_row_id(c)
    return {"id": fid, "name": name, "sort": nxt, "is_default": 0, "cnt": 0}


def rename_folder(folder_id: int, name: str) -> bool:
    name = (name or "").strip()
    if not name:
        raise ValueError("分组名称不能为空")
    c = _conn()
    r = c.execute("SELECT is_default FROM folders WHERE id=?", (folder_id,)).fetchone()
    if r is None:
        raise ValueError("分组不存在")
    if r["is_default"]:
        raise ValueError("默认分组不支持重命名")
    try:
        c.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))
        c.commit()
    except IntegrityError:
        raise ValueError(f"分组已存在：{name}")
    return True


def delete_folder(folder_id: int) -> bool:
    c = _conn()
    r = c.execute("SELECT is_default FROM folders WHERE id=?", (folder_id,)).fetchone()
    if r is None:
        raise ValueError("分组不存在")
    if r["is_default"]:
        raise ValueError("默认分组不可删除")
    # 级联删除组内自选股（外键 ON DELETE CASCADE）
    c.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    c.commit()
    return True


# ------------------------------------------------------------------ 自选股

def add_item(code: str, name: str = "", folder_id: Optional[int] = None,
             note: str = "") -> Dict[str, Any]:
    """加入自选；若已存在则只更新名称/备注，不报错（幂等）。

    并发要点：不能先 SELECT 再 INSERT —— 两个线程同时判断"不存在"会双双插入，
    触发 UNIQUE 冲突。这里改用单条 UPSERT（SQLite 3.24+ 的 ON CONFLICT），
    把"插入或更新"交给数据库原子完成，天然线程安全。
    """
    uid = current_user_id()
    if folder_id is None:
        fid = list_folders(uid)[0]["id"]
    else:
        fid = int(folder_id)
        if not _owns_folder(fid, uid):
            raise ValueError("分组不存在")
    code = (code or "").strip().lower()
    if not code:
        raise ValueError("股票代码不能为空")

    # 名称补全：名称为空或与代码相同时，尝试从行情快照取中文简称
    name = (name or "").strip()
    if not name or name.lower() == code:
        guess = _guess_name(code)
        if guess:
            name = guess

    c = _conn()
    # 判断是否已存在（仅用于返回 existed 标记，不参与写入决策）
    existed = c.execute(
        "SELECT 1 FROM watch_items WHERE folder_id=? AND code=?",
        (fid, code)).fetchone() is not None

    nxt = c.execute(
        "SELECT COALESCE(MAX(sort),0)+1 AS s FROM watch_items WHERE folder_id=?",
        (fid,)).fetchone()["s"]

    # 原子 upsert：冲突时保留原有非空值；新插入时名称兜底为代码。
    # SQLite 用 PostgreSQL 风格 ON CONFLICT，MySQL/TiDB 用私有的
    # ON DUPLICATE KEY UPDATE —— 两种写法语义等价，均已实测验证。
    if IS_MYSQL:
        c.execute(
            """INSERT INTO watch_items(folder_id,code,name,note,sort,created_at)
               VALUES(?,?,?,?,?,?)
               ON DUPLICATE KEY UPDATE
                 name = IF(VALUES(name) <> '' AND VALUES(name) <> VALUES(code),
                           VALUES(name), name),
                 note = IF(VALUES(note) <> '', VALUES(note), note)""",
            (fid, code, name or code, note, nxt, time.time()))
    else:
        c.execute(
            """INSERT INTO watch_items(folder_id,code,name,note,sort,created_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(folder_id,code) DO UPDATE SET
                 name = CASE WHEN excluded.name != '' AND excluded.name != excluded.code
                             THEN excluded.name ELSE watch_items.name END,
                 note = CASE WHEN excluded.note != '' THEN excluded.note
                             ELSE watch_items.note END""",
            (fid, code, name or code, note, nxt, time.time()))
    c.commit()

    row = c.execute(
        "SELECT id,name,note FROM watch_items WHERE folder_id=? AND code=?",
        (fid, code)).fetchone()

    # 若库里存的仍是「代码当名称」，说明加自选时还没解析出中文名，
    # 此时再尝试补一次，让已存在的条目也能被修正。
    if row["name"] == code and _name_resolver:
        fixed = _guess_name(code)
        if fixed and fixed != code:
            c.execute("UPDATE watch_items SET name=? WHERE id=?", (fixed, row["id"]))
            c.commit()
            row = c.execute("SELECT id,name,note FROM watch_items WHERE id=?",
                            (row["id"],)).fetchone()

    return {"id": int(row["id"]), "code": code, "name": row["name"],
            "note": row["note"], "folder_id": fid, "existed": existed}


def _owns_folder(folder_id: int, uid: int) -> bool:
    c = _conn()
    r = c.execute("SELECT 1 FROM folders WHERE id=? AND user_id=?",
                  (folder_id, uid)).fetchone()
    return r is not None


def list_items(folder_id: Optional[int] = None, user_id: Optional[int] = None
               ) -> List[Dict[str, Any]]:
    """列出自选股。folder_id 为 None 时返回该用户全部分组下的条目。"""
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    if folder_id is None:
        rows = c.execute(
            """SELECT w.id,w.folder_id,w.code,w.name,w.note,w.sort,w.created_at,
                      f.name AS folder_name
                 FROM watch_items w JOIN folders f ON f.id=w.folder_id
                WHERE f.user_id=?
                ORDER BY w.sort ASC, w.id ASC""", (uid,)).fetchall()
    else:
        if not _owns_folder(int(folder_id), uid):
            raise ValueError("分组不存在")
        rows = c.execute(
            """SELECT w.id,w.folder_id,w.code,w.name,w.note,w.sort,w.created_at,
                      f.name AS folder_name
                 FROM watch_items w JOIN folders f ON f.id=w.folder_id
                WHERE w.folder_id=?
                ORDER BY w.sort ASC, w.id ASC""", (int(folder_id),)).fetchall()
    return [dict(r) for r in rows]


def remove_item(item_id: Optional[int] = None, code: str = "",
                folder_id: Optional[int] = None) -> int:
    """删除自选。支持按 id，或按 (code[, folder_id]) 删除。返回删除条数。"""
    uid = current_user_id()
    c = _conn()
    if item_id is not None:
        # 校验归属，避免越权删他人数据
        r = c.execute(
            """SELECT w.id FROM watch_items w JOIN folders f ON f.id=w.folder_id
                WHERE w.id=? AND f.user_id=?""", (int(item_id), uid)).fetchone()
        if not r:
            return 0
        c.execute("DELETE FROM watch_items WHERE id=?", (int(item_id),))
        c.commit()
        return 1

    code = (code or "").strip().lower()
    if not code:
        raise ValueError("请提供 item_id 或 code")
    if folder_id is not None:
        cur = c.execute("DELETE FROM watch_items WHERE code=? AND folder_id=?",
                        (code, int(folder_id)))
    else:
        cur = c.execute(
            """DELETE FROM watch_items WHERE code=? AND folder_id IN
               (SELECT id FROM folders WHERE user_id=?)""", (code, uid))
    c.commit()
    return cur.rowcount or 0


def move_item(item_id: int, to_folder_id: int) -> bool:
    """把自选股移到另一个分组。"""
    uid = current_user_id()
    if not _owns_folder(int(to_folder_id), uid):
        raise ValueError("目标分组不存在")
    c = _conn()
    r = c.execute(
        """SELECT w.id FROM watch_items w JOIN folders f ON f.id=w.folder_id
            WHERE w.id=? AND f.user_id=?""", (int(item_id), uid)).fetchone()
    if not r:
        raise ValueError("自选股不存在")
    try:
        c.execute("UPDATE watch_items SET folder_id=? WHERE id=?",
                  (int(to_folder_id), int(item_id)))
        c.commit()
    except IntegrityError:
        raise ValueError("目标分组中已存在该股票")
    return True


def has_code(code: str) -> bool:
    """该代码是否已在（当前用户）任一自选分组中。"""
    uid = current_user_id()
    c = _conn()
    r = c.execute(
        """SELECT 1 FROM watch_items w JOIN folders f ON f.id=w.folder_id
            WHERE w.code=? AND f.user_id=? LIMIT 1""",
        ((code or "").strip().lower(), uid)).fetchone()
    return r is not None


def codes_of(user_id: Optional[int] = None) -> List[str]:
    """当前用户全部自选代码（去重），供行情批量拉取。"""
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    rows = c.execute(
        """SELECT DISTINCT w.code FROM watch_items w JOIN folders f ON f.id=w.folder_id
            WHERE f.user_id=?""", (uid,)).fetchall()
    return [r["code"] for r in rows]


# ------------------------------------------------------------------ 虚拟盘

#: 新用户开户时的费率默认值。
#:
#: 注册那一刻**固化写入 users 行**，之后各改各的，互不影响。
#: 想改「今后新用户」的费率，改环境变量或这里的字面量即可——
#: 已开户的人不受影响（这正是按列存费率的用意）。
#:
#: 键名与 .env.example 里的 TICK_FEE_* 一一对应。
DEFAULT_FEES = {
    "fee_rate": 0.00025,        # 股票佣金万 2.5，双向
    "fee_min": 5.0,             # 单笔最低 5 元
    "stamp_rate": 0.0005,       # 印花税千 0.5，仅卖出
    "etf_fee_rate": 0.00025,    # ETF 佣金万 2.5
    "etf_fee_min": 5.0,         # ETF 最低 5 元
}

#: 环境变量名 → DEFAULT_FEES 的键
_FEE_ENV = {
    "TICK_FEE_RATE": "fee_rate",
    "TICK_FEE_MIN": "fee_min",
    "TICK_STAMP_RATE": "stamp_rate",
    "TICK_ETF_FEE_RATE": "etf_fee_rate",
    "TICK_ETF_FEE_MIN": "etf_fee_min",
}


def default_fees() -> Dict[str, float]:
    """开户用的费率默认值：环境变量 > 内置字面量。

    只在注册时调用一次，不进缓存——改环境变量后重启即可对新用户生效，
    不必引入一套热更新配置（虚拟盘费率不需要那种灵活性）。
    """
    import os as _os
    out = dict(DEFAULT_FEES)
    for env, key in _FEE_ENV.items():
        raw = _os.environ.get(env)
        if raw in (None, ""):
            continue
        try:
            f = float(raw)
        except (TypeError, ValueError):
            continue          # 脏环境值不该拖垮注册
        if 0.0 <= f <= (100.0 if key.endswith("_min") else 0.05):
            out[key] = f
    return out

# 兼容旧引用：FEE_RATE 等常量在别处被读过，保留为默认值的别名。
FEE_RATE = DEFAULT_FEES["fee_rate"]
FEE_MIN = DEFAULT_FEES["fee_min"]
STAMP_RATE = DEFAULT_FEES["stamp_rate"]


#: 场内基金代码段（沪 5 开头、深 15/16/18 开头）。
#: 与 datasource._PROBE_SEGMENTS 里的基金段保持一致——ETF / LOF / 分级基金
#: 都免印花税，税法如此，所以这一条**不做成可配项**，用户改不了。
_ETF_PREFIXES = ("sh5", "sz15", "sz16", "sz18")


# ---------------------------------------------------------------- 网格计划

def create_grid(code: str, name: str, center_price: float, upper_price: float,
                lower_price: float, step_pct: float = 2.0, mode: str = "arith",
                lot: int = 10000, user_id: Optional[int] = None) -> int:
    """建一个网格计划。档位不落库（由 grid.build_levels 实时算），
    只记参数 + 已成交档位下标（fired），避免刷新后重复提示同一格。"""
    uid = user_id if user_id is not None else current_user_id()
    code = (code or "").strip().lower()
    center_price, upper_price, lower_price = (float(center_price), float(upper_price),
                                              float(lower_price))
    if not code:
        raise ValueError("标的代码不能为空")
    # 0 < 下界 < 中心 < 上界。价格必须为正，否则中心价 0 会让 build_levels
    # 把所有档位去重合并成 1 档，页面上是一张没有意义的空表。
    if not (0 < lower_price < center_price < upper_price):
        raise ValueError("价格区间必须满足：0 < 下界 < 中心价 < 上界")
    if float(step_pct) <= 0:
        raise ValueError("步长必须大于 0")
    lot = int(max(100, lot) // 100) * 100
    c = _conn()
    _begin(c)
    try:
        c.execute(
            """INSERT INTO grids(user_id,code,name,center_price,upper_price,lower_price,
               step_pct,mode,lot,fired,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,'','active',?)""",
            (uid, code, (name or "").strip() or code, center_price, upper_price,
             lower_price, float(step_pct), mode if mode in ("arith", "geo") else "arith",
             lot, time.time()))
        gid = _new_row_id(c)
        c.commit()
        return gid
    except Exception:
        c.rollback()
        raise


def list_grids(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    rows = c.execute(
        """SELECT id,code,name,center_price,upper_price,lower_price,step_pct,mode,
                  lot,fired,status,created_at
           FROM grids WHERE user_id=? ORDER BY created_at DESC""", (uid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["fired_list"] = [int(x) for x in str(d.get("fired") or "").split(",") if x.strip()]
        out.append(d)
    return out


def mark_grid_fired(grid_id: int, idx: int, user_id: Optional[int] = None) -> None:
    """把某档标记为已成交（幂等，重复标记无害）。"""
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    r = c.execute("SELECT fired FROM grids WHERE id=? AND user_id=?",
                  (int(grid_id), uid)).fetchone()
    if not r:
        return
    cur = [x for x in str(r["fired"] or "").split(",") if x.strip()]
    if str(int(idx)) not in cur:
        cur.append(str(int(idx)))
    c.execute("UPDATE grids SET fired=? WHERE id=? AND user_id=?",
              (",".join(cur), int(grid_id), uid))
    c.commit()


def delete_grid(grid_id: int, user_id: Optional[int] = None) -> bool:
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    cur = c.execute("DELETE FROM grids WHERE id=? AND user_id=?", (int(grid_id), uid))
    c.commit()
    return bool(getattr(cur, "rowcount", 0))


def is_etf(code: str) -> bool:
    """是否场内基金（ETF/LOF/分级）。按代码段判，不依赖网络。"""
    return (code or "").strip().lower().startswith(_ETF_PREFIXES)


def get_fees(user_id: Optional[int] = None) -> Dict[str, float]:
    """取该用户的费率。库里缺失（老行）时回落默认值。"""
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    r = c.execute(
        "SELECT fee_rate,fee_min,stamp_rate,etf_fee_rate,etf_fee_min "
        "FROM users WHERE id=?", (uid,)).fetchone()
    out = dict(DEFAULT_FEES)
    if r:
        for k in DEFAULT_FEES:
            v = r[k] if k in r.keys() else None
            if v is not None:
                out[k] = float(v)
    return out


def set_fees(fees: Dict[str, Any], user_id: Optional[int] = None) -> Dict[str, float]:
    """改费率。只认白名单键，逐项校验范围，全过才落库。"""
    uid = user_id if user_id is not None else current_user_id()
    cur = get_fees(uid)
    for k, v in (fees or {}).items():
        if k not in DEFAULT_FEES:
            continue                      # 未知键静默忽略（前端可能多传）
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"费率 {k} 应为数字")
        # 两个量纲不同的东西，边界必须分开给：
        #   *_rate 是比例（万 2.5 = 0.00025），上界放到 5% 留足自由度；
        #   *_fee_min 是【元】，拿 0.05 去卡它会把最低佣金 5 元判成非法。
        if k in ("fee_min", "etf_fee_min"):
            if not (0.0 <= f <= 100.0):
                raise ValueError(f"最低佣金应在 0 ~ 100 元之间，收到 {f}")
        elif not (0.0 <= f <= 0.05):
            raise ValueError(f"费率 {k} 应在 0 ~ 0.05（即 0%~5%）之间，收到 {f}")
        cur[k] = f
    c = _conn()
    c.execute(
        "UPDATE users SET fee_rate=?,fee_min=?,stamp_rate=?,"
        "etf_fee_rate=?,etf_fee_min=? WHERE id=?",
        (cur["fee_rate"], cur["fee_min"], cur["stamp_rate"],
         cur["etf_fee_rate"], cur["etf_fee_min"], uid))
    c.commit()
    return cur


def _calc_fee(amount: float, side: str, fees: Optional[Dict[str, float]] = None,
              etf: bool = False) -> float:
    """佣金（有最低值）+ 卖出印花税。

    费率口径随标的而变：**ETF/场内基金免印花税**，所以走 etf_ 那套费率，
    且印花税恒为 0。这不是可配项——税法如此，用户改错了会算出假盈亏。
    """
    f = fees or DEFAULT_FEES
    if etf:
        comm = max(amount * float(f.get("etf_fee_rate", DEFAULT_FEES["etf_fee_rate"])),
                   float(f.get("etf_fee_min", DEFAULT_FEES["etf_fee_min"])))
        tax = 0.0
    else:
        comm = max(amount * float(f.get("fee_rate", DEFAULT_FEES["fee_rate"])),
                   float(f.get("fee_min", DEFAULT_FEES["fee_min"])))
        tax = amount * float(f.get("stamp_rate", DEFAULT_FEES["stamp_rate"])) if side == "sell" else 0.0
    return round(comm + tax, 2)


def list_positions(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    rows = c.execute(
        """SELECT id,code,name,qty,cost,created_at,updated_at
           FROM positions WHERE user_id=? AND qty > 0 ORDER BY code""", (uid,)).fetchall()
    return [dict(r) for r in rows]


def get_position(code: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    uid = user_id if user_id is not None else current_user_id()
    code = (code or "").strip().lower()
    c = _conn()
    r = c.execute("SELECT * FROM positions WHERE user_id=? AND code=?",
                  (uid, code)).fetchone()
    return dict(r) if r else None


def list_trades(user_id: Optional[int] = None, limit: int = 100
                ) -> List[Dict[str, Any]]:
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    rows = c.execute(
        """SELECT id,code,name,side,qty,price,fee,amount,pnl,pspan,created_at
           FROM trades WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?""",
        (uid, int(limit))).fetchall()
    return [dict(r) for r in rows]


def buy_stock(code: str, qty: int, price: float, name: str = "",
              user_id: Optional[int] = None, pspan: str = "") -> Dict[str, Any]:
    """模拟买入。整个操作在一个事务里完成，避免并发下资金/持仓错乱。"""
    uid = user_id if user_id is not None else current_user_id()
    code = (code or "").strip().lower()
    qty = int(qty)
    price = float(price)
    if not code:
        raise ValueError("股票代码不能为空")
    if qty <= 0:
        raise ValueError("买入数量必须大于 0")
    if qty % 100 != 0:
        raise ValueError("买入数量必须是 100 的整数倍（1 手 = 100 股）")
    if price <= 0:
        raise ValueError("买入价格必须大于 0")

    name = (name or "").strip()
    if is_code_like(name):        # 前端纯代码买入会把 "SZ002342" 当 name 传来
        name = ""
    name = name or _guess_name(code) or code
    amount = round(price * qty, 2)
    etf = is_etf(code)
    fees = get_fees(uid)
    fee = _calc_fee(amount, "buy", fees, etf=etf)
    total = round(amount + fee, 2)

    c = _conn()
    try:
        _begin(c)
        # MySQL 侧靠悲观行锁串行化（SQLite 的 BEGIN IMMEDIATE 已拿写锁，无需再加）
        _lk = " FOR UPDATE" if IS_MYSQL else ""
        r = c.execute(f"SELECT cash FROM users WHERE id=?{_lk}", (uid,)).fetchone()
        if not r:
            raise ValueError("用户不存在")
        cash = float(r["cash"])
        if total > cash + 1e-6:
            raise ValueError(f"资金不足：需 {total:.2f} 元，可用 {cash:.2f} 元")

        c.execute("UPDATE users SET cash = cash - ? WHERE id=?", (total, uid))

        # 持仓：加权平均成本
        pos = c.execute("SELECT id,qty,cost FROM positions WHERE user_id=? AND code=?",
                        (uid, code)).fetchone()
        if pos:
            old_qty, old_cost = int(pos["qty"]), float(pos["cost"])
            # 成本含手续费，贴近真实持仓成本
            new_qty = old_qty + qty
            new_cost = round((old_cost * old_qty + amount + fee) / new_qty, 4)
            c.execute("""UPDATE positions SET qty=?, cost=?, name=?, updated_at=?
                         WHERE id=?""", (new_qty, new_cost, name, time.time(), pos["id"]))
        else:
            c.execute(
                """INSERT INTO positions(user_id,code,name,qty,cost,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (uid, code, name, qty, round((amount + fee) / qty, 4),
                 time.time(), time.time()))

        c.execute(
            """INSERT INTO trades(user_id,code,name,side,qty,price,fee,amount,pnl,pspan,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (uid, code, name, "buy", qty, price, fee, amount, None, pspan, time.time()))
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {"code": code, "name": name, "qty": qty, "price": price,
            "amount": amount, "fee": fee, "total": total,
            "is_etf": etf, "pspan": pspan,
            "cash": round(cash - total, 2)}


def sell_stock(code: str, qty: int, price: float,
               user_id: Optional[int] = None, pspan: str = "") -> Dict[str, Any]:
    """模拟卖出。返回本笔盈亏。"""
    uid = user_id if user_id is not None else current_user_id()
    code = (code or "").strip().lower()
    qty = int(qty)
    price = float(price)
    if not code:
        raise ValueError("股票代码不能为空")
    if qty <= 0:
        raise ValueError("卖出数量必须大于 0")
    if price <= 0:
        raise ValueError("卖出价格必须大于 0")

    c = _conn()
    try:
        _begin(c)
        _lk = " FOR UPDATE" if IS_MYSQL else ""
        pos = c.execute(
            f"SELECT id,qty,cost,name FROM positions WHERE user_id=? AND code=?{_lk}",
            (uid, code)).fetchone()
        if not pos or int(pos["qty"]) <= 0:
            raise ValueError("没有该股票的持仓")
        held = int(pos["qty"])

        # ── T+1：当日买入的份额当日不可卖（A 股 / A 股 ETF 均为 T+1）──
        # ponytail: 用成交流水按「买入日期 == 今天」累加，可卖 = 总持仓 - 今日买入。
        # 这既符合「今天买的不能卖、之前买的能卖」，又不必给 positions 加批次字段
        # （FIFO 下今日买入永远最后卖，总仓减今日买入即最大可卖）。
        # 用 Python 端 time.strftime 比较，避开 SQLite/MySQL 日期函数方言与时区坑，
        # 单用户单日买入笔数很小，无性能问题。
        today = time.strftime("%Y-%m-%d")
        bought_today = 0
        bs = c.execute(
            "SELECT qty, created_at FROM trades"
            " WHERE user_id=? AND code=? AND side='buy'",
            (uid, code)).fetchall()
        for r in bs:
            try:
                d = time.strftime("%Y-%m-%d", time.localtime(float(r["created_at"])))
            except (TypeError, ValueError):
                continue
            if d == today:
                bought_today += int(r["qty"])
        sellable = held - bought_today
        if qty > sellable:
            raise ValueError(
                f"T+1 限制：当日买入的 {bought_today} 股需下一交易日方可卖出"
                f"（当前可卖 {sellable} 股）")
        if qty > held:
            raise ValueError(f"持仓不足：持有 {held} 股，尝试卖出 {qty} 股")

        amount = round(price * qty, 2)
        etf = is_etf(code)
        fee = _calc_fee(amount, "sell", get_fees(uid), etf=etf)
        net = round(amount - fee, 2)
        cost_total = round(float(pos["cost"]) * qty, 2)
        pnl = round(net - cost_total, 2)

        c.execute("UPDATE users SET cash = cash + ? WHERE id=?", (net, uid))

        left = held - qty
        if left > 0:
            c.execute("UPDATE positions SET qty=?, updated_at=? WHERE id=?",
                      (left, time.time(), pos["id"]))
        else:
            c.execute("DELETE FROM positions WHERE id=?", (pos["id"],))

        c.execute(
            """INSERT INTO trades(user_id,code,name,side,qty,price,fee,amount,pnl,pspan,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (uid, code, pos["name"], "sell", qty, price, fee, amount, pnl, pspan, time.time()))
        r = c.execute("SELECT cash FROM users WHERE id=?", (uid,)).fetchone()
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {"code": code, "name": pos["name"], "qty": qty, "price": price,
            "amount": amount, "fee": fee, "net": net, "pnl": pnl,
            "is_etf": is_etf(code), "pspan": pspan,
            "cash": round(float(r["cash"]), 2)}


def seed_test_position(uid: int, code: str, qty: int, price: float,
                       name: str = "", days_ago: int = 1) -> Dict[str, Any]:
    """仅供测试：造一条「N 天前买入」的持仓 + 流水。

    ponytail: 虚拟盘已启用 T+1（当日买入当日不可卖），但印花税 / 盈亏这类断言
    又必须基于一笔「能卖」的卖出。这里给测试一个造非今日仓的入口，避免
    check_paper.py、selfcheck() 里到处拼脆弱的裸 SQL。生产代码勿用。

    实现要点：先清掉该 (user, code) 的持仓与全部买入流水，再插入昨日仓，
    这样无论测试前是否已有当日买入，seed 都是「干净的昨日仓」，不会和 T+1
    的 today_bought 计算打架，也不会撞 positions 唯一约束。
    """
    uid = int(uid)
    code = (code or "").strip().lower()
    qty = int(qty)
    price = float(price)
    name = (name or "").strip() or _guess_name(code) or code
    ts = int(time.time()) - 86400 * max(0, int(days_ago))
    amount = round(price * qty, 2)
    c = _conn()
    try:
        _begin(c)
        c.execute("DELETE FROM positions WHERE user_id=? AND code=?", (uid, code))
        c.execute("DELETE FROM trades WHERE user_id=? AND code=? AND side='buy'",
                  (uid, code))
        c.execute(
            "INSERT INTO positions(user_id,code,name,qty,cost,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?)", (uid, code, name, qty, round(price, 4), ts, ts))
        c.execute(
            "INSERT INTO trades(user_id,code,name,side,qty,price,fee,amount,pnl,pspan,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (uid, code, name, "buy", qty, price, 0.0, amount, None, "昨收", ts))
        c.execute("COMMIT")
        return {"ok": True, "code": code, "qty": qty, "price": price}
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise


def reset_account(user_id: Optional[int] = None, cash: float = _INITIAL_CASH
                  ) -> Dict[str, Any]:
    """清空持仓与流水，资金复位。"""
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    c.execute("DELETE FROM positions WHERE user_id=?", (uid,))
    c.execute("DELETE FROM trades WHERE user_id=?", (uid,))
    c.execute("UPDATE users SET cash=? WHERE id=?", (float(cash), uid))
    c.commit()
    return {"cash": float(cash)}


def portfolio_summary(user_id: Optional[int] = None) -> Dict[str, Any]:
    """汇总：可用资金 / 持仓市值由调用方补（需行情），这里只出成本口径。"""
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    u = c.execute("SELECT cash FROM users WHERE id=?", (uid,)).fetchone()
    rows = list_positions(uid)
    cost_sum = round(sum(float(p["cost"]) * int(p["qty"]) for p in rows), 2)
    return {"cash": round(float(u["cash"]), 2) if u else 0.0,
            "position_count": len(rows),
            "cost_total": cost_sum}


# ------------------------------------------------------ 匿名数据认领（搬走制）

# 虚拟盘加登录守卫后，守卫之前产生的数据挂在默认账号（username='local'）下，
# 不登录就再也看不到。这里的「认领」把那份数据**整体搬到**目标账号。
#
# 为什么是搬走而不是复制：复制会让每个登录的人都白拿一份同样的资金与持仓，
# 两个人登录就凭空多出一份钱。所以匿名数据是一份**待认领的遗产**，先到先得。


def anon_state() -> Dict[str, Any]:
    """匿名（默认账号）名下的虚拟盘数据概况。用于前端决定是否显示「认领」。"""
    c = _conn()
    r = c.execute("SELECT id,cash FROM users WHERE username='local'").fetchone()
    if not r:
        return {"exists": False, "cash": 0.0, "position_count": 0, "trade_count": 0}
    uid = int(r["id"])
    pos = c.execute("SELECT COUNT(*) AS n FROM positions WHERE user_id=? AND qty>0",
                    (uid,)).fetchone()
    trd = c.execute("SELECT COUNT(*) AS n FROM trades WHERE user_id=?",
                    (uid,)).fetchone()
    n_pos = int(pos["n"]) if pos else 0
    n_trd = int(trd["n"]) if trd else 0
    return {
        "exists": bool(n_pos or n_trd),
        "cash": round(float(r["cash"]), 2),
        "position_count": n_pos,
        "trade_count": n_trd,
    }


def claim_anon_state(user_id: Optional[int] = None) -> Dict[str, Any]:
    """把匿名账号的持仓/流水/资金搬给目标账号，搬完全部清空（搬走制）。

    **资金不是相加而是替换**：匿名那份 cash 代表"当年那 100 万花剩多少"，
    搬到新账号后新账号的 cash 直接取匿名那份，否则等于白送一次初始资金。
    这样"认领"= 把整张账户原样过户，不含任何赠予。

    返回搬了什么，供前端提示。没有可认领数据时抛 ValueError。
    """
    uid = user_id if user_id is not None else current_user_id()
    c = _conn()
    _begin(c)
    try:
        r = c.execute("SELECT id,cash FROM users WHERE username='local'").fetchone()
        if not r:
            raise ValueError("没有可认领的历史数据")
        src = int(r["id"])
        if src == uid:
            raise ValueError("当前就是匿名账号，无需认领")
        st = anon_state()
        if not st["exists"]:
            raise ValueError("没有可认领的历史数据")

        # 目标账号已有同代码持仓时不能直接搬（会撞 UNIQUE(user_id,code)），
        # 先按加权成本合并过去。
        src_pos = c.execute(
            "SELECT id,code,name,qty,cost FROM positions WHERE user_id=? AND qty>0",
            (src,)).fetchall()
        moved = 0
        for p in src_pos:
            code = p["code"]
            qty = int(p["qty"])
            cost = float(p["cost"])
            tgt = c.execute(
                "SELECT id,qty,cost FROM positions WHERE user_id=? AND code=?",
                (uid, code)).fetchone()
            if tgt:
                tq, tc = int(tgt["qty"]), float(tgt["cost"])
                nq = tq + qty
                nc = round((tc * tq + cost * qty) / nq, 4)
                c.execute("UPDATE positions SET qty=?,cost=?,updated_at=? WHERE id=?",
                          (nq, nc, time.time(), tgt["id"]))
                c.execute("DELETE FROM positions WHERE id=?", (p["id"],))
            else:
                c.execute("UPDATE positions SET user_id=? WHERE id=?", (uid, p["id"]))
            moved += 1

        # 流水整体过户（保留原时间戳，复盘时顺序不乱）
        c.execute("UPDATE trades SET user_id=? WHERE user_id=?", (uid, src))
        cash = float(r["cash"])
        c.execute("UPDATE users SET cash=? WHERE id=?", (cash, uid))
        # 清空匿名账号，确保不会被第二个账号再认领一次
        c.execute("UPDATE users SET cash=? WHERE id=?", (_INITIAL_CASH, src))
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {"ok": True, "positions": moved, "cash": round(cash, 2),
            "trades": st["trade_count"]}


# ------------------------------------------------------------------ 日线历史行情

def upsert_bars(code: str, bars: List[Dict[str, Any]]) -> int:
    """批量写入/更新某只股票的日线，返回写入条数。

    幂等：主键 (code, date) 冲突时更新 OHLCV，重复同步同一段不会产生重复行。
    这是历史行情落库的核心写入入口。

    性能：用「多值 INSERT」把 N 行合成一条 SQL，而不是逐行 execute。

    这条优化很关键。TiDB Cloud Serverless 的单次网络往返实测约 190~260ms，
    因此成本几乎完全由【语句条数】决定，与数据量基本无关：

        · 逐行 INSERT + 逐行 commit : 367 ms/条
        · 逐行 INSERT（仅末尾 commit）: 181 ms/条
        · 多值批量 100 行/语句      : 2.8 ms/条
        · 单条 SQL 灌 200 行         : 1.9 ms/条

    500 条一次写入实测约 1.4s（含建连与提交）。改回逐行会让单只股票
    从 ~1s 退化到 3 分钟级，务必保持批量。
    """
    if not bars:
        return 0
    now = time.time()
    c = _conn()

    # 组装行数据
    rows = []
    for b in bars:
        d = str(b.get("date") or "")
        if not d:
            continue
        rows.append((code, d,
                     _num_or_none(b.get("open")), _num_or_none(b.get("close")),
                     _num_or_none(b.get("high")), _num_or_none(b.get("low")),
                     _num_or_none(b.get("volume")), _num_or_none(b.get("amount")),
                     now))
    if not rows:
        return 0

    tail = (" ON DUPLICATE KEY UPDATE open=VALUES(open),close=VALUES(close),"
            "high=VALUES(high),low=VALUES(low),volume=VALUES(volume),"
            "amount=VALUES(amount),updated_at=VALUES(updated_at)"
            ) if IS_MYSQL else (
            " ON CONFLICT(code,date) DO UPDATE SET open=excluded.open,"
            "close=excluded.close,high=excluded.high,low=excluded.low,"
            "volume=excluded.volume,amount=excluded.amount,"
            "updated_at=excluded.updated_at")

    # 分批：单条 SQL 的占位符不宜过多（SQLite 默认上限 999 个变量，
    # 每行 9 个 → 每批最多 110 行；这里取 100 行留余量）
    BATCH = 100
    written = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        ph = ",".join(["(?,?,?,?,?,?,?,?,?)"] * len(chunk))
        sql = ("INSERT INTO daily_bars"
               "(code,date,open,close,high,low,volume,amount,updated_at)"
               f" VALUES{ph}") + tail
        args = [v for row in chunk for v in row]
        c.execute(sql, args)
        written += len(chunk)
    c.commit()
    return written


def _num_or_none(v):
    """转 float，失败或 NaN 返回 None（DB 列允许 NULL）"""
    try:
        f = float(v)
        if f != f:      # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def get_bars(code: str, limit: int = 300, end_date: str = "") -> List[Dict[str, Any]]:
    """读取某只股票的日线（按日期升序返回，最多 limit 条）。

    end_date 非空时只取该日期（含）之前的数据，便于做历史时点回看。
    """
    c = _conn()
    if end_date:
        rows = c.execute(
            "SELECT code,date,open,close,high,low,volume,amount FROM daily_bars"
            " WHERE code=? AND date<=? ORDER BY date DESC LIMIT ?",
            (code, end_date, int(limit))).fetchall()
    else:
        rows = c.execute(
            "SELECT code,date,open,close,high,low,volume,amount FROM daily_bars"
            " WHERE code=? ORDER BY date DESC LIMIT ?",
            (code, int(limit))).fetchall()
    out = [dict(r) for r in rows]
    out.reverse()       # 转成日期升序，与 datasource.get_kline 的约定一致
    return out


def bars_coverage(codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """统计落库覆盖情况：多少只股票有数据、总行数、日期范围。"""
    c = _conn()
    if codes:
        marks = ",".join("?" for _ in codes)
        row = c.execute(
            f"SELECT COUNT(DISTINCT code) AS n, COUNT(*) AS rows_,"
            f" MIN(date) AS d0, MAX(date) AS d1 FROM daily_bars"
            f" WHERE code IN ({marks})", tuple(codes)).fetchone()
    else:
        row = c.execute(
            "SELECT COUNT(DISTINCT code) AS n, COUNT(*) AS rows_,"
            " MIN(date) AS d0, MAX(date) AS d1 FROM daily_bars").fetchone()
    d = dict(row) if row else {}
    return {"codes": int(d.get("n") or 0),
            "rows": int(d.get("rows_") or 0),
            "start": d.get("d0") or "",
            "end": d.get("d1") or ""}


def bars_progress(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """批量查每股日线进度：max(date) 与已落库根数。

    同步守门（sync_bars.gate_full_sync 抽检覆盖率）与增量预筛
    （每股只拉缺口天数，不整段重拉）共用这一个查询，一次 GROUP BY
    拿到「每只最新到哪天 + 存了多少根」两个答案。

    SQLite 的 IN 变量上限 999（每批 3 个占位符），按 500/批切分留余量。
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not codes:
        return out
    c = _conn()
    BATCH = 500
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        marks = ",".join("?" for _ in chunk)
        rows = c.execute(
            f"SELECT code, MAX(date) AS d, COUNT(*) AS n FROM daily_bars"
            f" WHERE code IN ({marks}) GROUP BY code", tuple(chunk)).fetchall()
        for r in rows:
            out[r["code"]] = {"last": r["d"] or "", "n": int(r["n"] or 0)}
    return out


def record_sync(code: str, last_date: str, bars: int,
                status: str = "ok", err: str = "") -> None:
    """登记某只股票的同步进度（供增量拉取与断点续传）"""
    c = _conn()
    sql = ("INSERT INTO bar_sync(code,last_date,bars,synced_at,status,err)"
           " VALUES(?,?,?,?,?,?)")
    sql += (" ON DUPLICATE KEY UPDATE last_date=VALUES(last_date),"
            "bars=VALUES(bars),synced_at=VALUES(synced_at),"
            "status=VALUES(status),err=VALUES(err)"
            ) if IS_MYSQL else (
            " ON CONFLICT(code) DO UPDATE SET last_date=excluded.last_date,"
            "bars=excluded.bars,synced_at=excluded.synced_at,"
            "status=excluded.status,err=excluded.err")
    c.execute(sql, (code, last_date, int(bars), time.time(), status, err[:500]))
    c.commit()


def sync_status(limit: int = 0) -> List[Dict[str, Any]]:
    """查看同步进度列表（按最近同步时间倒序）"""
    c = _conn()
    sql = "SELECT code,last_date,bars,synced_at,status,err FROM bar_sync ORDER BY synced_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in c.execute(sql).fetchall()]


# ------------------------------------------------------------------ 自检

def selfcheck() -> Dict[str, Any]:
    """读写自检：建表 -> 写 -> 读 -> 更新 -> 删除，全链路验证。

    两条硬规矩，改动时务必保持：

    1. 【不碰用户真实分组】。自检用到的分组全部自己创建，并在 finally 里
       删干净。曾经这里直接把测试股票 move 到 list_folders()[0]（通常是
       用户的「我的自选」），一旦该分组已有同名股票，唯一约束 (folder_id,
       code) 就会抛 ValueError —— 表现为自检无故失败，且失败路径不清理，
       留下一堆 __自检分组__ 垃圾分组。
    2. 【失败也要清理】。整段包在 try/finally 中，任何断言失败都不残留数据，
       否则下一次自检会被上一次的垃圾干扰。
    """
    out: Dict[str, Any] = {"ok": False, "steps": []}
    _conn()
    created_folders: List[int] = []
    created_grids: List[int] = []
    try:
        info = initialize()
        out["init"] = info
        out["steps"].append("initialize OK")

        f = create_folder("__自检分组__")
        created_folders.append(f["id"])
        out["steps"].append(f"create_folder OK id={f['id']}")

        a = add_item("sh600519", "贵州茅台", f["id"], "自检写入")
        out["steps"].append(f"add_item OK id={a['id']}")

        a2 = add_item("sh600519", "贵州茅台", f["id"])   # 幂等验证
        assert a2["existed"] is True, "重复加入应返回 existed=True"
        out["steps"].append("add_item 幂等 OK")

        items = list_items(f["id"])
        assert len(items) == 1, f"应恰好 1 条，实际 {len(items)}"
        assert items[0]["code"] == "sh600519"
        out["steps"].append("list_items OK")

        assert has_code("sh600519") is True
        assert has_code("sz999999") is False
        out["steps"].append("has_code OK")

        b = add_item("sz000858", "五粮液", f["id"])
        assert len(list_items(f["id"])) == 2
        out["steps"].append("add_item 第二条 OK")

        remove_item(item_id=b["id"])
        assert len(list_items(f["id"])) == 1
        out["steps"].append("remove_item OK")

        # move_item 在自检自己的两个分组间验证，绝不使用用户既有分组
        f2 = create_folder("__自检分组2__")
        created_folders.append(f2["id"])
        move_item(a["id"], f2["id"])
        assert len(list_items(f2["id"])) == 1 and len(list_items(f["id"])) == 0
        out["steps"].append("move_item OK")

        # 目标分组已有同一只股票时，应给出明确错误而不是静默成功
        try:
            dup = add_item("sh600519", "贵州茅台", f["id"])
            remove_item(item_id=dup["id"])
            out["steps"].append("move_item 后同码可再加 OK")
        except ValueError:
            out["steps"].append("move_item 后同码可再加 OK(已存在)")

        delete_folder(f2["id"])
        created_folders.remove(f2["id"])
        out["steps"].append("delete_folder OK")

        # ---------- 账号 ----------
        uname = f"_test_{int(time.time())}"
        u = register(uname, "pwd123456", "测试账号")
        out["steps"].append(f"register OK id={u['id']}")
        assert verify_login(uname, "pwd123456") is not None
        assert verify_login(uname, "wrongpwd") is None
        out["steps"].append("verify_login 正确/错误密码 OK")

        try:
            register(uname, "pwd123456")
            raise AssertionError("重名注册应被拒绝")
        except ValueError:
            out["steps"].append("register 重名拒绝 OK")

        tok = create_session(u["id"])
        assert user_by_token(tok)["id"] == u["id"]
        assert user_by_token("bogus") is None
        out["steps"].append("session 签发/校验 OK")

        # ---------- 虚拟盘 ----------
        r = buy_stock("sh600519", 100, 1200.0, "贵州茅台", u["id"])
        assert r["qty"] == 100 and r["fee"] >= FEE_MIN
        assert abs(r["cash"] - (_INITIAL_CASH - r["total"])) < 0.01
        out["steps"].append(f"buy_stock OK 扣款 {r['total']:.2f}")

        pos = get_position("sh600519", u["id"])
        assert pos and pos["qty"] == 100
        out["steps"].append("持仓生成 OK")

        # 加仓：验证加权平均成本
        buy_stock("sh600519", 100, 1300.0, "贵州茅台", u["id"])
        pos = get_position("sh600519", u["id"])
        assert pos["qty"] == 200 and 1200 < pos["cost"] < 1300
        out["steps"].append(f"加仓加权成本 OK cost={pos['cost']:.2f}")

        # T+1：当日买入当日不可卖——这句必须被拒
        try:
            sell_stock("sh600519", 100, 1400.0, u["id"])
            raise AssertionError("T+1：当日买入当日卖应被拒绝")
        except ValueError as e:
            assert "T+1" in str(e), f"应提示 T+1，实际: {e}"
            out["steps"].append("T+1 当日卖拒绝 OK")

        # 用一条「昨日建仓」验证正常卖出与盈亏计算（今日仓仍在、可卖为 0）
        seed_test_position(u["id"], "sh600519", 100, 1200.0, "贵州茅台", days_ago=1)
        s = sell_stock("sh600519", 100, 1400.0, u["id"])
        assert s["pnl"] is not None
        assert s["is_etf"] is False
        out["steps"].append(f"sell_stock(昨日仓) OK 盈亏 {s['pnl']:.2f}")

        # ---------- 费率：按用户存 + ETF 免印花税 ----------
        f0 = get_fees(u["id"])
        assert f0["fee_rate"] == DEFAULT_FEES["fee_rate"]
        # 印花税只在卖出收，且 ETF 恒为 0
        a = 100000.0
        stock_sell = _calc_fee(a, "sell", f0, etf=False)
        stock_buy = _calc_fee(a, "buy", f0, etf=False)
        etf_sell = _calc_fee(a, "sell", f0, etf=True)
        assert stock_sell > stock_buy, "卖出应比买入多一笔印花税"
        assert abs(stock_sell - stock_buy - a * 0.0005) < 0.01
        assert abs(etf_sell - _calc_fee(a, "buy", f0, etf=True)) < 0.01, "ETF 卖出不该有印花税"
        out["steps"].append(
            f"费率口径 OK 股卖 {stock_sell} / 股买 {stock_buy} / ETF卖 {etf_sell}")

        # 改费率立刻影响计算
        nf = set_fees({"fee_rate": 0.0001, "fee_min": 1.0}, u["id"])
        assert nf["fee_rate"] == 0.0001
        assert _calc_fee(a, "buy", get_fees(u["id"]), etf=False) == 10.0
        out["steps"].append("set_fees 生效 OK")
        for bad in ({"fee_rate": "abc"}, {"fee_rate": -0.1}, {"fee_rate": 0.9},
                    {"fee_min": 9999}):
            try:
                set_fees(bad, u["id"])
                raise AssertionError(f"非法费率 {bad} 应被拒绝")
            except ValueError:
                pass
        out["steps"].append("非法费率拒绝 OK")
        set_fees(DEFAULT_FEES, u["id"])           # 还原

        # ETF 是按代码段判的，不联网
        assert is_etf("sh510300") and is_etf("sz159915") and is_etf("sh588000")
        assert not is_etf("sh600519") and not is_etf("sz000858")
        out["steps"].append("is_etf 代码段判定 OK")

        # 开户默认值可被环境变量覆盖，但脏值/越界必须回落（否则一个手滑的
        # .env 会让所有新用户按 99 倍费率算钱）
        import os as _os
        assert default_fees()["fee_rate"] == 0.00025
        _os.environ["TICK_FEE_RATE"] = "0.0001"
        assert default_fees()["fee_rate"] == 0.0001
        for bad in ("abc", "99", "-1", ""):
            _os.environ["TICK_FEE_RATE"] = bad
            assert default_fees()["fee_rate"] == 0.00025, f"脏值 {bad!r} 应回落"
        _os.environ.pop("TICK_FEE_RATE", None)
        assert default_fees()["fee_rate"] == 0.00025
        out["steps"].append("开户费率环境变量覆盖 + 脏值回落 OK")

        try:
            sell_stock("sh600519", 9999, 1400.0, u["id"])
            raise AssertionError("超量卖出应被拒绝")
        except ValueError:
            out["steps"].append("超量卖出拒绝 OK")

        try:
            buy_stock("sz000858", 150, 70.0, "", u["id"])
            raise AssertionError("非整手买入应被拒绝")
        except ValueError:
            out["steps"].append("非整手拒绝 OK")

        try:
            buy_stock("sz000858", 100, 999999.0, "", u["id"])
            raise AssertionError("资金不足应被拒绝")
        except ValueError:
            out["steps"].append("资金不足拒绝 OK")

        # ---------- 网格计划（F3）----------
        # 档位本身不落库（由 grid.build_levels 实时算），这里只验参数与 fired 集合。
        # 断言重点有三：区间必须严格单调、fired 标记幂等、跨用户不可见。
        for bad in ({"lower_price": 11.0},                      # 下界 >= 中心
                    {"upper_price": 9.0},                       # 中心 >= 上界
                    {"center_price": 0.0, "upper_price": 1.0, "lower_price": -1.0}):
            kw = {"code": "sh600519", "name": "非法区间", "center_price": 10.0,
                  "upper_price": 12.0, "lower_price": 8.0}
            kw.update(bad)
            try:
                create_grid(user_id=u["id"], **kw)
                raise AssertionError(f"非法网格区间 {bad} 应被拒绝")
            except ValueError:
                pass
        try:
            create_grid("sh600519", "茅台", 10.0, 12.0, 8.0, step_pct=0, user_id=u["id"])
            raise AssertionError("步长 0 应被拒绝")
        except ValueError:
            pass
        try:
            create_grid("", "空", 10.0, 12.0, 8.0, user_id=u["id"])
            raise AssertionError("空代码应被拒绝")
        except ValueError:
            pass
        out["steps"].append("create_grid 非法参数拒绝 OK")

        gid = create_grid("sh600519", "茅台网格", 10.0, 12.0, 8.0,
                          step_pct=2.0, lot=150, user_id=u["id"])
        created_grids.append(gid)
        assert gid > 0
        g = [x for x in list_grids(u["id"]) if x["id"] == gid][0]
        assert g["center_price"] == 10.0 and g["upper_price"] == 12.0
        assert g["lower_price"] == 8.0 and g["step_pct"] == 2.0
        assert g["mode"] == "arith" and g["status"] == "active"
        assert g["lot"] == 100, f"每手份数应向下取整到 100，拿到 {g['lot']}"
        assert g["fired_list"] == [], "新建网格不该有已成交档位"
        out["steps"].append(f"create_grid + list_grids OK id={gid} lot={g['lot']}")

        # 幂等：同一档标两次，fired 里只能有一个（否则前端会重复提示成交）
        mark_grid_fired(gid, 3, u["id"])
        mark_grid_fired(gid, 3, u["id"])
        g = [x for x in list_grids(u["id"]) if x["id"] == gid][0]
        assert g["fired_list"] == [3], f"fired 应幂等，拿到 {g['fired_list']}"
        mark_grid_fired(gid, 5, u["id"])
        g = [x for x in list_grids(u["id"]) if x["id"] == gid][0]
        assert g["fired_list"] == [3, 5], f"追加档位失败 {g['fired_list']}"
        out["steps"].append("mark_grid_fired 幂等 + 追加 OK")

        # 别人的网格看不见，也标不动（mark 对不存在/非本人行是静默 no-op）
        u2 = register(f"_test2_{int(time.time())}", "pwd123456", "测试账号2")
        assert len(list_grids(u2["id"])) == 0, "新用户不该看到别人的网格"
        mark_grid_fired(gid, 7, u2["id"])
        g = [x for x in list_grids(u["id"]) if x["id"] == gid][0]
        assert g["fired_list"] == [3, 5], "跨用户标记居然生效了"
        out["steps"].append("网格用户隔离 OK")

        assert delete_grid(gid, u["id"]) is True
        assert delete_grid(gid, u["id"]) is False, "重复删除应返回 False"
        created_grids.remove(gid)
        assert len(list_grids(u["id"])) == 0
        out["steps"].append("delete_grid OK（重复删除返回 False）")

        cc = _conn()
        cc.execute("DELETE FROM users WHERE id=?", (u2["id"],))
        cc.commit()

        assert len(list_trades(u["id"])) == 3
        out["steps"].append("trades 流水 OK（3 笔）")

        # ---------- 匿名数据认领（搬走制）----------
        # 用一个临时"匿名账号"模拟守卫之前的数据，验证搬走语义：
        # 资金是【替换】不是相加，否则认领等于白送一次初始资金。
        cc0 = _conn()
        anon_id = _ensure_default_user(cc0)       # username='local'
        anon_cash_before = float(
            cc0.execute("SELECT cash FROM users WHERE id=?", (anon_id,)).fetchone()["cash"])
        b0 = buy_stock("sh600519", 100, 1000.0, "贵州茅台", anon_id)
        anon_cash_after_buy = float(b0["cash"])   # 买入后匿名侧余额（已扣本金+佣金）
        st_before = anon_state()
        assert st_before["exists"] is True

        target_cash_before = float(
            cc0.execute("SELECT cash FROM users WHERE id=?", (u["id"],)).fetchone()["cash"])
        r2 = claim_anon_state(u["id"])
        assert r2["positions"] >= 1
        # 资金是【替换】语义：目标账号余额变成匿名侧那份（含它已花掉的那笔），
        # 而不是两边相加。用「买入后的匿名余额」直接比对，能同时排除
        # 「相加」（会多出 100 万）与「赠予」（会多出 10 万）两种错法。
        assert abs(r2["cash"] - anon_cash_after_buy) < 0.01, \
            f"认领后余额应等于匿名侧买入后余额，拿到 {r2['cash']} 期望 {anon_cash_after_buy}"
        assert r2["cash"] < target_cash_before + anon_cash_before, "认领不该是资金相加"
        # 匿名侧已清空，第二个账号认领不到
        assert anon_state()["exists"] is False, "搬走后匿名侧应为空"
        try:
            claim_anon_state(u["id"])
            raise AssertionError("已搬空后再次认领应被拒绝")
        except ValueError:
            pass
        out["steps"].append("认领匿名数据（搬走制）OK")

        # 清理：把匿名账号复位，别把测试持仓留在生产匿名账号里
        cc0.execute("DELETE FROM positions WHERE user_id=?", (anon_id,))
        cc0.execute("DELETE FROM trades WHERE user_id=?", (anon_id,))
        cc0.execute("UPDATE users SET cash=1000000.0 WHERE id=?", (anon_id,))
        cc0.commit()
        out["steps"].append("匿名账号已复位 OK")

        logout(tok)
        assert user_by_token(tok) is None
        out["steps"].append("logout OK")

        # 清理测试账号（级联清掉其分组/自选/持仓/流水/会话）
        cc = _conn()
        cc.execute("DELETE FROM users WHERE id=?", (u["id"],))
        cc.commit()
        out["steps"].append("清理测试账号 OK")

        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        # 无论成败都清掉自检自建的分组，避免垃圾数据累积干扰下一次自检。
        for fid in created_folders:
            try:
                delete_folder(fid)
            except Exception:
                pass
        if created_folders:
            out["steps"].append(f"清理自检分组 OK（{len(created_folders)} 个）")
        for gid in created_grids:
            try:
                delete_grid(gid)
            except Exception:
                pass
        if created_grids:
            out["steps"].append(f"清理自检网格 OK（{len(created_grids)} 个）")
    return out


if __name__ == "__main__":
    import json
    print("DB:", DB_PATH)
    print(json.dumps(selfcheck(), ensure_ascii=False, indent=2))
