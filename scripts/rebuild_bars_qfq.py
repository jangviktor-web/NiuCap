#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 daily_bars 的历史段从 hfq（后复权）重灌为 qfq（前复权）。

## 为什么需要这个脚本

库内发生过「复权口径中途切换」：

  · 2026-10-07 的 e86cd3b 引入 `--adjust`。当时 eltdx 的 qfq 在历史分页段
    会产出负值价格，所以补多年历史只能走 `--adjust hfq`。
  · 于是 `date <= 2026-09-15` 的段落被灌成 hfq（锚定上市首日的累积后复权）。
  · 之后日常增量同步用默认 qfq，只覆盖尾部几天。
  · 而 sync_bars 的增量预筛是「尾部日期够新 + 根数够多 → 整只跳过」，
    永远不会回头修正历史段。

结果：同一张表里 2026-09-15 及之前是 hfq、之后是 qfq，两套口径拼接。
实测 5562 只 / 489 万行（全库 98%）受影响，断点日 2026-09-15 上全市场
1166 只出现 >10% 跳变（茅台 8960→1258，平安银行 3707→11）。

危害是静默的：K 线图、MACD/KDJ/BOLL、形态识别、回测、因子全部失真，
持仓页的吊灯止损取「近 22 日最高价」跨过断点，把茅台的止损算成 8502
（真实价 1280 附近），前端显示「已破 545%」。

## 为什么现在能用 qfq 重灌了

`e86cd3b` 的「qfq 历史分页必坏」结论在 eltdx v3.2.2 上已不成立。
实测安全深度约 2400 根（≈ 2016-11 至今），2600 根才出负值。
库内 hfq 段根数的中位数只有 496 根，绝大多数股票完全在安全范围内。

脚本对每只股票按需分档取数（1200 / 1800 / 2400），一旦命中负值护栏就
跳过该只并记录，不强灌脏数据。

## 用法

    python3 scripts/rebuild_bars_qfq.py --dry-run          # 只抽样验证，不写库
    python3 scripts/rebuild_bars_qfq.py --scope hs300      # 小范围试跑
    python3 scripts/rebuild_bars_qfq.py --scope all        # 全量重灌

跑完务必再跑一次：
    python3 scripts/rebuild_bars_qfq.py --verify
"""
import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import store as st                                        # noqa: E402
from sources import eltdx_source                          # noqa: E402

#: 口径断点。<= 该日期的行是 hfq，需要重灌；> 的是 qfq，保持不动。
#: 之所以没写死成常量判断，是因为重灌后这个边界就没意义了——
#: 它只用来「挑出要重灌的那些 code」，而不是给消费端做分界。
MIXED_BEFORE = "2026-09-15"

#: 分档取数深度。越深越慢，但对多数股票第一档就够（中位 496 根）。
#: 2400 是实测的 qfq 安全上限附近（2600 出负值），碰到护栏就退档。
DEPTHS = (1200, 1800, 2400)

DB = os.path.join(ROOT, "data", "tick.db")


def _conn():
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    return c


def find_dirty(limit: int = 0):
    """找出**真正**需要重灌的 code：库内历史段的值与新拉 qfq 对不上。

    注意别只按「date <= 断点日」筛——那只说明那段是早期灌的，不代表口径错。
    实测（见 --dry-run）：同类日期区间里既有倍率 1.000 的干净股票
    （sh601238 / sh600301 / sh688181 …，它们这段时间没有除权，hfq==qfq），
    也有倍率 14 的污染股票（sh600000）。混着重灌等于白拉一遍数据。

    判定法：取该只「断点日之前最近 3 根」的库内收盘价，与新拉 qfq 同日期
    的值比。中位倍率偏离 1 超过 2% 即判定为口径不一致，需要重灌。
    只比 3 根是因为除权是阶跃的，取最近几根就能反映当前因子，且成本低。

    返回 [{"code","n","d0","d1","ratio"}]。
    """
    c = _conn()
    q = (f"SELECT code, COUNT(*) n, MIN(date) d0, MAX(date) d1 FROM daily_bars"
         f" WHERE date <= '{MIXED_BEFORE}' GROUP BY code HAVING n >= 2"
         f" ORDER BY n DESC")
    rows = [dict(r) for r in c.execute(q)]

    print(f"  候选（断点日前有历史的）{len(rows)} 只，逐只比对 qfq 一致性……")
    dirty = []
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        code = row["code"]
        old = c.execute(
            "SELECT date, close FROM daily_bars WHERE code=? AND date<=?"
            " ORDER BY date DESC LIMIT 3", (code, MIXED_BEFORE)).fetchall()
        if not old:
            continue
        bars, _ = fetch(code)
        if not bars:
            continue
        qmap = {b["date"]: b["close"] for b in bars}
        ratios = []
        for o in old:
            nv = qmap.get(o["date"])
            if nv and o["close"] and nv > 0:
                ratios.append(o["close"] / nv)
        if not ratios:
            continue
        ratios.sort()
        med = ratios[len(ratios) // 2]
        if abs(med - 1.0) > 0.02:
            row["ratio"] = round(med, 4)
            dirty.append(row)
        if i % 500 == 0 or i == len(rows):
            print(f"    [{i:>5}/{len(rows)}] 判定为脏 {len(dirty)} 只"
                  f" | 已用 {time.time()-t0:.0f}s")
    c.close()
    return dirty[:limit] if limit else dirty


def fetch(code: str):
    """按 DEPTHS 逐档尝试，返回 (bars, depth) 或 (None, reason)。"""
    for depth in DEPTHS:
        try:
            r = eltdx_source.get_bars_batch([code], count=depth, adjust="qfq")
        except eltdx_source.EltdxUnavailable as e:
            # 主要是负值护栏命中（该只历史太深），退到下一档
            last = str(e)
            continue
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
        bars = r.get(code) or []
        if bars:
            return bars, depth
    return None, last if "last" in dir() else "无数据"


def rebuild_one(row):
    """重灌一只。返回结果字典。"""
    code = row["code"]
    t0 = time.time()
    bars, info = fetch(code)
    if not bars:
        return {"code": code, "ok": False, "bars": 0, "msg": f"取数失败：{info}"}

    # 只写「断点日及之前」的段落 + 顺带补上缺失的尾部（重灌后整段同口径）。
    # 不写 > 断点日 的部分：那部分本来就是 qfq，且可能已含今日实时数据。
    payload = [{
        "date": b.get("date"), "open": b.get("open"), "close": b.get("close"),
        "high": b.get("high"), "low": b.get("low"),
        "volume": b.get("volume"), "amount": b.get("amount"),
    } for b in bars if b.get("date")]
    try:
        n = st.upsert_bars(code, payload)
        return {"code": code, "ok": True, "bars": n, "depth": info,
                "sec": round(time.time() - t0, 2)}
    except Exception as e:
        return {"code": code, "ok": False, "bars": 0,
                "msg": f"{type(e).__name__}: {e}"}


def do_dry_run(sample: int):
    print("═" * 66)
    print("干跑：抽查若干只，比对「库内 hfq 值」与「新拉 qfq 值」，不写库")
    print("═" * 66)
    rows = find_dirty(limit=0)
    print(f"待重灌候选：{len(rows)} 只\n")
    picked = rows[:sample] + rows[len(rows) // 2: len(rows) // 2 + sample]
    c = _conn()
    for row in picked:
        code = row["code"]
        bars, depth = fetch(code)
        if not bars:
            print(f"  {code:<12} ✗ 取数失败（{depth}）")
            continue
        tail = [b for b in bars if b["date"] <= MIXED_BEFORE][-3:]
        print(f"  {code:<12} n={row['n']:<5} 拉到 {len(bars)} 根@{depth}  "
              f"库内 {row['d0']}~{row['d1']}")
        for b in tail:
            old = c.execute("SELECT close FROM daily_bars WHERE code=? AND date=?",
                            (code, b["date"])).fetchone()
            ov = old["close"] if old else None
            ratio = (ov / b["close"]) if (ov and b["close"]) else 0
            print(f"      {b['date']}  库内={ov if ov is None else round(ov,2):<12}"
                  f"新qfq={round(b['close'],2):<12} 倍率={ratio:.3f}")
    c.close()
    print("\n倍率列若普遍 >1（且各股不同），即为 hfq 放大效应得到确认。")


def do_verify():
    print("═" * 66)
    print("复验：扫描相邻交易日跳变 >30% 的断点，应只剩极少数真·异动")
    print("═" * 66)
    c = _conn()
    q = """
    with x as (
      select code, date, close,
             lag(close) over (partition by code order by date) pc
      from daily_bars where date >= '2026-08-01')
    select substr(date,1,7) ym, count(*) n from x
    where pc > 0 and (close/pc > 1.3 or close/pc < 0.77)
    group by ym order by ym
    """
    tot = 0
    for r in c.execute(q):
        print(f"  {r['ym']}  跳变股次数={r['n']}")
        tot += r["n"]
    print(f"\n  2026-08 起累计异常跳变：{tot} 次")
    print("  （重灌前仅 2026-09-15 一天就有 1166 只；修好后应降到几十以内，")
    print("    剩下的才是真实的涨跌停/复牌缺口）")
    c.close()
    return tot


def do_audit(sample: int = 120, tol: float = 0.02):
    """体检：随机抽样，查库内历史段与实时源是否同口径。

    为什么需要这个而不是只靠 rebuild：导致本次污染的那个机制还在——
    sync_bars 的增量预筛是「尾部日期够新 + 根数够多 → 整只跳过」，
    它**不会回头检查历史段的口径**。所以只要将来又有人换口径灌过一次
    历史，同样的病还会复发，而且同样是静默的（K 线看着有、值不对）。
    把抽检做成常跑的命令，比事后靠用户发现止损算错要好。

    判定：库内「60 日前的最近 3 根」收盘价 vs 新拉 qfq 同日期值，中位
    倍率偏离 1 超过 tol 即报脏。用 60 日前是为了避开尾部刚同步过的段，
    专挑「预筛会跳过、因而最可能没人管」的历史段。
    """
    import random
    print("═" * 66)
    print(f"口径体检：随机抽 {sample} 只，查历史段是否仍与 qfq 同口径")
    print("═" * 66)
    c = _conn()
    allc = [r["code"] for r in c.execute(
        "SELECT DISTINCT code FROM daily_bars ORDER BY code")]
    # 只看有足够历史的，太新的股票没有「历史段」可查
    allc = [x for x in allc
            if c.execute("SELECT COUNT(*) n FROM daily_bars WHERE code=?", (x,)).fetchone()["n"] >= 60]
    picked = random.sample(allc, min(sample, len(allc)))
    bad = []
    t0 = time.time()
    for code in picked:
        cut = c.execute(
            "SELECT date FROM daily_bars WHERE code=? ORDER BY date DESC LIMIT 1 OFFSET 59",
            (code,)).fetchone()
        if not cut:
            continue
        old = c.execute(
            "SELECT date, close FROM daily_bars WHERE code=? AND date<=?"
            " ORDER BY date DESC LIMIT 3", (code, cut["date"])).fetchall()
        bars, _ = fetch(code)
        qmap = {b["date"]: b["close"] for b in (bars or [])}
        ratios = [o["close"] / qmap[o["date"]] for o in old
                  if qmap.get(o["date"]) and o["close"]]
        if not ratios:
            continue
        ratios.sort()
        med = ratios[len(ratios) // 2]
        if abs(med - 1.0) > tol:
            bad.append((code, round(med, 4)))
    c.close()
    print(f"\n  抽样 {len(picked)} 只，耗时 {time.time()-t0:.0f}s")
    if not bad:
        print("  ✓ 未发现口径不一致——库内日线口径统一（全为 qfq）")
        return 0
    print(f"  ✗ 发现 {len(bad)} 只口径不一致（历史段疑似旧口径残留）：")
    for code, r in bad[:40]:
        print(f"      {code:<12} 倍率={r}")
    print("\n  修复：python3 scripts/rebuild_bars_qfq.py --scope all")
    return 1


def main():
    ap = argparse.ArgumentParser(description="把 daily_bars 历史段重灌为 qfq")
    ap.add_argument("--scope", default="all", choices=["all", "hs300", "codes"],
                    help="重灌范围：all=全市场 / hs300=仅沪深300 / codes=用 --codes 指定")
    ap.add_argument("--codes", default="", help="逗号分隔代码（配合 --scope codes）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（调试）")
    ap.add_argument("--workers", type=int, default=4, help="落库并发（默认4）")
    ap.add_argument("--dry-run", action="store_true", help="只抽样比对，不写库")
    ap.add_argument("--verify", action="store_true", help="只做复验扫描，不写库")
    ap.add_argument("--audit", action="store_true",
                    help="随机抽样体检口径一致性（建议定期跑）")
    args = ap.parse_args()

    if args.audit:
        return do_audit(args.limit or 120)
    if args.verify:
        do_verify()
        return 0
    if args.dry_run:
        do_dry_run(max(args.limit, 2))
        return 0

    rows = find_dirty()
    if args.scope == "hs300":
        import datasource as ds
        pool = set(ds.pool_rows("hs300") or [])
        rows = [r for r in rows if r["code"] in pool]
    elif args.scope == "codes":
        want = {x.strip() for x in args.codes.split(",") if x.strip()}
        rows = [r for r in rows if r["code"] in want]
    if args.limit:
        rows = rows[:args.limit]

    if not rows:
        print("没有需要重灌的股票。")
        return 0

    total_bars = sum(r["n"] for r in rows)
    print("═" * 66)
    print(f"重灌 qfq：{len(rows)} 只 / {total_bars} 行历史（断点日 {MIXED_BEFORE} 及之前）")
    print("═" * 66)

    t0 = time.time()
    ok = fail = 0
    written = 0
    failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(rebuild_one, r): r["code"] for r in rows}
        done = 0
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            if r["ok"]:
                ok += 1
                written += r["bars"]
            else:
                fail += 1
                failed.append(r)
            if done % 100 == 0 or done == len(rows):
                el = time.time() - t0
                eta = el / done * (len(rows) - done)
                print(f"  [{done:>5}/{len(rows)}] 成功 {ok:>5} 失败 {fail:>4} "
                      f"| 已写 {written:>9} 行 | 已用 {el/60:.1f}分 剩余 {eta/60:.1f}分")

    print(f"\n完成：成功 {ok} / 失败 {fail}，共写 {written} 行，"
          f"耗时 {(time.time()-t0)/60:.1f} 分")
    if failed:
        print("\n失败明细（前 30）：")
        for r in failed[:30]:
            print(f"  {r['code']:<12} {r.get('msg','')[:80]}")
    print("\n下一步：python3 scripts/rebuild_bars_qfq.py --verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
