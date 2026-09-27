#!/usr/bin/env python3
"""把本地 SQLite 库（data/tick.db）的存量数据迁移到 MySQL 协议库。

用法：
    TICK_DB_HOST=... TICK_DB_USER=... TICK_DB_PASSWORD=... \
        python3 scripts/migrate_sqlite_to_mysql.py            # 干跑，只对比不写入
    ... python3 scripts/migrate_sqlite_to_mysql.py --apply    # 真正写入

行为要点：
- 按 users → sessions → folders → watch_items → positions → trades 顺序写入，
  满足外键依赖。
- 保留原主键 ID，避免父子关系错配（显式写 id，自增列允许指定值）。
- 幂等：写入前先清空目标表，重复运行结果一致，不会累加。
- 写完后逐表比对行数，不一致立刻报错退出。
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import store  # noqa: E402

# 每张表的字段清单（目标库列顺序），SQLite 侧缺失的字段用右侧默认值补齐
TABLE_DEFS = [
    ("users",
     ["id", "username", "display", "pwd_hash", "pwd_salt", "cash", "created_at"],
     {"pwd_hash": "", "pwd_salt": "", "cash": 1000000.0, "created_at": 0.0,
      "display": ""}),
    ("sessions",
     ["token", "user_id", "created_at", "expires_at"],
     {}),
    ("folders",
     ["id", "user_id", "name", "sort", "is_default", "created_at"],
     {"sort": 0, "is_default": 0, "created_at": 0.0}),
    ("watch_items",
     ["id", "folder_id", "code", "name", "note", "sort", "created_at"],
     {"name": "", "note": "", "sort": 0, "created_at": 0.0}),
    ("positions",
     ["id", "user_id", "code", "name", "qty", "cost", "created_at", "updated_at"],
     {"name": "", "qty": 0, "cost": 0.0, "created_at": 0.0, "updated_at": 0.0}),
    ("trades",
     ["id", "user_id", "code", "name", "side", "qty", "price", "fee", "amount",
      "pnl", "created_at"],
     {"name": "", "fee": 0.0, "created_at": 0.0}),
]

# 清空顺序：先子表后父表，避免外键约束拦路（有 CASCADE，但显式更稳）
WIPE_ORDER = ["trades", "positions", "watch_items", "folders", "sessions", "users"]


def read_source(db_path: str, table: str, cols: list, defaults: dict):
    """从 SQLite 读取一张表，缺列补默认值。"""
    if not os.path.exists(db_path):
        raise SystemExit(f"源库不存在：{db_path}")
    src = sqlite3.connect(db_path)
    src.row_factory = sqlite3.Row
    try:
        exist = {r["name"] for r in src.execute(f"PRAGMA table_info({table})")}
        if not exist:
            return []
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        src.close()
    out = []
    for r in rows:
        d = dict(r)
        out.append(tuple(d.get(c, defaults.get(c)) for c in cols))
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    if not store.IS_MYSQL:
        print("请先设置 TICK_DB_HOST / TICK_DB_USER / TICK_DB_PASSWORD 切换到 MySQL 后端")
        return 2

    src_db = os.environ.get("TICK_DB_PATH") or os.path.join(ROOT, "data", "tick.db")
    print(f"源  : {src_db}")
    print(f"目标: {store._target_label()}")
    print(f"模式: {'写入' if apply else '干跑（加 --apply 才写入）'}\n")

    c = store._conn()
    store.initialize()

    payload = []
    print("读取源库：")
    for table, cols, defaults in TABLE_DEFS:
        rows = read_source(src_db, table, cols, defaults)
        payload.append((table, cols, rows))
        print(f"  {table:<12} {len(rows):>5} 行")

    total = sum(len(r) for _, _, r in payload)
    if total == 0:
        print("\n源库没有任何数据，无需迁移。")
        return 0

    if not apply:
        print(f"\n干跑结束，共 {total} 行待迁移。确认无误后加 --apply 执行。")
        return 0

    print("\n清空目标表：")
    for t in WIPE_ORDER:
        n = c.execute(f"DELETE FROM {t}").rowcount
        print(f"  {t:<12} 清掉 {n} 行")
    c.commit()

    print("\n写入目标库：")
    for table, cols, rows in payload:
        if not rows:
            continue
        ph = ", ".join(["?"] * len(cols))
        collist = ", ".join(cols)
        cur = c.executemany(
            f"INSERT INTO {table}({collist}) VALUES({ph})", rows)
        c.commit()
        print(f"  {table:<12} 写入 {len(rows)} 行")

    print("\n校验行数：")
    ok = True
    for table, _, rows in payload:
        got = c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        flag = "OK " if got == len(rows) else "差异"
        if got != len(rows):
            ok = False
        print(f"  {flag} {table:<12} 源 {len(rows)} → 目标 {got}")

    # 顺手对外键做一次抽查：每个 folder 的 user_id、每个 item 的 folder_id 都有归属
    orphan_f = c.execute(
        "SELECT COUNT(*) AS n FROM folders f "
        "LEFT JOIN users u ON u.id=f.user_id WHERE u.id IS NULL").fetchone()["n"]
    orphan_i = c.execute(
        "SELECT COUNT(*) AS n FROM watch_items w "
        "LEFT JOIN folders f ON f.id=w.folder_id WHERE f.id IS NULL").fetchone()["n"]
    print(f"\n孤立记录检查: folders={orphan_f} watch_items={orphan_i}")
    if orphan_f or orphan_i:
        ok = False

    print("\n迁移" + ("成功" if ok else "完成但存在问题，请检查上面的差异"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
