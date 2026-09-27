#!/usr/bin/env python3
"""数据库写入性能诊断 —— 拆解 connect / execute / commit 各环节耗时。

用途：
  当落库变慢（例如 sync_bars.py 单只超过几秒）时，跑这个脚本定位瓶颈
  在【建连】【语句往返】【提交】还是【数据源限流】哪一环。

背景（2026-09 实测，TiDB Cloud Serverless）：
  · pymysql.connect()        : 1.3 ~ 2.4s   ← 建连很贵
  · 复用连接 SELECT 1         : ~200ms/RTT   ← 往返很贵
  · _conn() 的 ping 探活      : ~200ms/次    ← 已加 30s 节流，见 store._PING_IDLE
  · 逐行 INSERT + 逐行 commit : 367ms/条
  · 多值批量 100 行/语句      : 2.8ms/条     ← upsert_bars 采用此方案
  · 单条 SQL 灌 200 行        : 1.9ms/条

结论：成本由【语句条数】决定，与数据量基本无关。批量是唯一有效的优化。

用法：
    set -a && source .env && set +a && .venv/bin/python scripts/diag_bars_perf.py

脚本使用独立影子表 _perf_bars，测完自动删除，不碰业务数据。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

import store as st  # noqa: E402

TBL = "_perf_bars"
COLS = "code,date,open,close,high,low,volume,amount,updated_at"
PH = "(?,?,?,?,?,?,?,?,?)"
DDL = (
    f"CREATE TABLE IF NOT EXISTS {TBL} ("
    + (" code VARCHAR(32) NOT NULL, date VARCHAR(16) NOT NULL," if st.IS_MYSQL
       else " code TEXT NOT NULL, date TEXT NOT NULL,")
    + (" open DOUBLE, close DOUBLE, high DOUBLE, low DOUBLE, volume DOUBLE,"
       " amount DOUBLE, updated_at DOUBLE NOT NULL," if st.IS_MYSQL else
       " open REAL, close REAL, high REAL, low REAL, volume REAL,"
       " amount REAL, updated_at REAL NOT NULL,")
    + " PRIMARY KEY (code, date))"
)


def bar(i: int, code: str) -> list:
    return [code, f"2024-{i % 12 + 1:02d}-{i % 28 + 1:02d}",
            10.0, 11.0, 12.0, 9.0, 1e6, 1e7, 1.0]


def ms(t: float) -> str:
    return f"{t * 1000:8.1f}ms"


def main() -> None:
    print(f"backend = {st.BACKEND}")
    if st.IS_MYSQL:
        print(f"dsn     = {st.MYSQL_HOST}:{st.MYSQL_PORT}/{st.MYSQL_DB}")
        for i in range(3):
            t0 = time.perf_counter()
            raw = st._mysql_connect(st.MYSQL_DB)
            print(f"[0.{i}] pymysql.connect()          : {ms(time.perf_counter() - t0)}")
            raw.close()
        raw = st._mysql_connect(st.MYSQL_DB)
        cur = raw.cursor()
        for _ in range(3):
            t0 = time.perf_counter()
            cur.execute("SELECT 1")
            cur.fetchall()
            print(f"[0.r] 复用连接 SELECT 1            : {ms(time.perf_counter() - t0)}")
        cur.execute("SELECT VERSION()")
        print(f"[0.v] 版本                         : {cur.fetchall()[0][0]}")
        raw.close()

    c = st._conn()
    c.execute(f"DROP TABLE IF EXISTS {TBL}")
    t0 = time.perf_counter()
    c.execute(DDL)
    c.commit()
    print(f"\n[1] CREATE TABLE + commit          : {ms(time.perf_counter() - t0)}")

    N = 200

    t0 = time.perf_counter()
    for i in range(N):
        c.execute(f"INSERT INTO {TBL}({COLS}) VALUES{PH}", bar(i, f"A{i:04d}"))
        c.commit()
    d = time.perf_counter() - t0
    print(f"\n[2] 逐行 + 逐行 commit ({N})         : {d:6.2f}s  ({d / N * 1000:6.1f}ms/条)")

    t0 = time.perf_counter()
    for i in range(N, 2 * N):
        c.execute(f"INSERT INTO {TBL}({COLS}) VALUES{PH}", bar(i, f"B{i:04d}"))
    te = time.perf_counter() - t0
    t0 = time.perf_counter()
    c.commit()
    tc = time.perf_counter() - t0
    print(f"[3] 逐行 NO-commit ({N})            : execute={te:5.2f}s"
          f" ({te / N * 1000:5.1f}ms/条)  commit={tc:5.2f}s")

    for BATCH, base in ((50, 2 * N), (100, 3 * N), (200, 4 * N), (500, 5 * N)):
        t0 = time.perf_counter()
        for i in range(0, N, BATCH):
            idxs = list(range(base + i, base + i + min(BATCH, N - i)))
            ph = ",".join([PH] * len(idxs))
            args = [v for k in idxs for v in bar(k, f"C{k:05d}")]
            c.execute(f"INSERT INTO {TBL}({COLS}) VALUES{ph}", args)
        te = time.perf_counter() - t0
        t0 = time.perf_counter()
        c.commit()
        tc = time.perf_counter() - t0
        print(f"[4] 批量 {BATCH:>3}/批 单次 commit       : execute={te:5.2f}s  "
              f"commit={tc:5.2f}s  合计={te + tc:5.2f}s  ({((te + tc) / N) * 1000:5.1f}ms/条)")

    base = 6 * N
    t0 = time.perf_counter()
    c.execute(f"INSERT INTO {TBL}({COLS}) VALUES{','.join([PH] * N)}",
              [v for k in range(base, base + N) for v in bar(k, f"D{k:05d}")])
    te = time.perf_counter() - t0
    t0 = time.perf_counter()
    c.commit()
    tc = time.perf_counter() - t0
    print(f"[5] 单条 SQL 灌 {N} 行               : execute={te:5.2f}s  "
          f"commit={tc:5.2f}s  合计={te + tc:5.2f}s")

    c.execute(f"DROP TABLE {TBL}")
    c.commit()
    print("\n[6] 清理完成")


if __name__ == "__main__":
    main()
