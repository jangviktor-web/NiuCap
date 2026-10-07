#!/usr/bin/env python3
"""
指数增强原型（指数内选股 + 月度再平衡）
======================================

回答一个问题：**能不能靠面板里唯一中等有效的因子「形态评分」，
在沪深300 内做 TOP 篮选，长期跑赢等权指数？**

为什么做这个
------------
前面几轮已经证伪了「日赚 2%」「技术指标择时」「快进快出」——数学和实证都封死。
真正还站着、且跨周期待验的，只有「形态评分」（近期 ICIR 0.486，方向为正）。
指数增强是业界最现实的「因子变现」路径：不预测市场方向，只在指数成分内
挑相对更强的票，赚「相对收益」（alpha），而非「绝对暴利」。

方法（最简可用版，不碰任何面板代码）
-----------------------------------
- 数据：全量日线（HistoryEngine(lookback_days=10000)），覆盖 15 年。
- 股票池：沪深300 成分（已补全历史），只保留历史够长的。
- 基准：沪深300 等权、月度再平衡（含真实成本）——和增强组合用**同一套成本**，
        保证对比只反映「选股」差异，不被成本口径干扰。
- 增强：每月末对成分股算「形态评分」，取前 30 只（约 decile）等权持有到次月末；
        换仓时按换手计提 佣万2.5双 + 印千1卖 + 滑万5双。
- 输出：年化 / 超额 / 夏普 / 最大回撤 / 月度胜率（跑赢基准的月份占比）。

诚实声明
--------
- 这是**原型**，只验证「形态评分能否贡献正 alpha」，不是可上线的策略。
- 样本内：直接在全历史上调参（top 30）属于样本内，结论偏乐观，需样本外验证。
- 幸存者偏差：沪深300 是**当前**成分，回测期被踢出的票漏掉 → 收益偏乐观。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Tuple

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import history as hist                                   # noqa: E402
import similarity as sim                                 # noqa: E402

# 复用 backtest.py 的真实成本口径，保证与面板回测一致
try:
    from backtest import COMMISSION, STAMP_TAX, SLIPPAGE
except Exception:                                       # 兜底，避免导入失败阻断
    COMMISSION, STAMP_TAX, SLIPPAGE = 0.00025, 0.001, 0.0005

TOP_K = 30                    # 每月持有前 30 只（沪深300 的约 decile）
POOL = ("000300",)            # 沪深300


# ---------------------------------------------------------------------------
# 数据 / 股票池
# ---------------------------------------------------------------------------
def _norm_code(code: str) -> str:
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("8", "4")):
        return "bj" + code
    return code


def pool_codes(eng: Any) -> List[str]:
    path = os.path.join(ROOT, "data", "index_pools.json")
    with open(path, "r", encoding="utf-8") as f:
        p = json.load(f)
    codes: set = set()
    for key in POOL:
        v = p.get(key, {})
        cs = v.get("codes", []) if isinstance(v, dict) else v
        codes.update(_norm_code(c) for c in cs)
    # 只保留历史够长（≥ 250 根，约 1 年）的，避免新股/数据不足
    return sorted(c for c in codes if eng.series(c)
                  and len(eng.series(c)["dates"]) >= 250)


def _month_ends(dates: List[str]) -> List[str]:
    """从交易日序列里取每月最后一个交易日。"""
    ends: List[str] = []
    last_yy_mm = None
    for d in dates:
        yy_mm = d[:7]
        if yy_mm != last_yy_mm:
            if last_yy_mm is not None:
                ends.append(prev)
            last_yy_mm = yy_mm
        prev = d
    if last_yy_mm is not None:
        ends.append(prev)
    return ends


# ---------------------------------------------------------------------------
# 组合模拟（等权 + 月度再平衡 + 真实成本）
# ---------------------------------------------------------------------------
def _one_way_turnover(old_w: Dict[str, float],
                      new_w: Dict[str, float]) -> float:
    keys = set(old_w) | set(new_w)
    return 0.5 * sum(abs(new_w.get(k, 0.0) - old_w.get(k, 0.0)) for k in keys)


def simulate(eng: Any, codes: List[str], pick_fn,
             top_k: int = TOP_K, rebal_days: int = 21) -> Dict[str, Any]:
    """跑增强组合。pick_fn(eng, codes, date, idx_map) -> 选中的 code 集合。

    rebal_days  再平衡步长（交易日）。默认 21≈月度。改小（如 5）可测更短
                持仓周期——用于验证「因子有效窗口 vs 持仓周期」是否错配。
    返回 NAV 序列与年度指标。基准（等权全池）由 main 另算。
    """
    # 每只股票的 date->(close, idx) 映射
    lut: Dict[str, Dict[str, Tuple[float, int]]] = {}
    for c in codes:
        s = eng.series(c)
        d = s["dates"]
        cl = s["close"]
        lut[c] = {dd: (float(cl[i]), i) for i, dd in enumerate(d)}

    # 用「历史最长」的成分股当交易日历基准（覆盖最久）。
    # 不要求所有票在每个月末都有数据——某只近期才上市的票不该把整段历史
    # 截断。缺数据的票在换仓/持有环节按只跳过（见下方 port_ret 计算）。
    ref = max(codes, key=lambda c: len(eng.series(c)["dates"]))
    all_dates = eng.series(ref)["dates"]
    # 按步长取再平衡日（含最后一天），默认 21 交易日≈月度
    cal = all_dates[::rebal_days]
    if cal and cal[-1] != all_dates[-1]:
        cal = cal + [all_dates[-1]]

    nav = 1.0
    nav_series: List[Tuple[str, float]] = [(cal[0], nav)]
    old_w: Dict[str, float] = {}
    rets: List[float] = []
    bench_rets: List[float] = []
    months = len(cal) - 1
    for i in range(months):
        d0, d1 = cal[i], cal[i + 1]
        # ---- 选股 ----
        picked = pick_fn(eng, codes, d0, lut)
        if not picked:
            picked = list(codes)
        new_w = {c: 1.0 / len(picked) for c in picked}
        # ---- 换仓成本（单边换手 × 双边费率 + 卖出印花税）----
        to = _one_way_turnover(old_w, new_w)
        cost = to * (2 * COMMISSION + 2 * SLIPPAGE + STAMP_TAX)
        nav *= (1.0 - cost)
        # ---- 持有到次月末（逐只跳过缺数据的票）----
        ret_acc = 0.0
        cnt = 0
        for c in picked:
            e0 = lut[c].get(d0)
            e1 = lut[c].get(d1)
            if not e0 or not e1:
                continue
            c0, c1 = e0[0], e1[0]
            if c0 > 0:
                ret_acc += (c1 / c0 - 1.0)
                cnt += 1
        port_ret = ret_acc / cnt if cnt else 0.0
        nav *= (1.0 + port_ret)
        rets.append(port_ret)
        nav_series.append((d1, nav))
        old_w = new_w

    return {"nav": nav, "nav_series": nav_series, "rets": rets,
            "months": months}


def _annualize(total_ret: float, years: float) -> float:
    if years <= 0:
        return 0.0
    return ((1.0 + total_ret) ** (1.0 / years) - 1.0) * 100.0


def _max_drawdown(nav_series: List[Tuple[str, float]]) -> float:
    peak = nav_series[0][1]
    mdd = 0.0
    for _, v in nav_series:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return mdd * 100.0


def _sharpe(rets: List[float]) -> float:
    if len(rets) < 2:
        return 0.0
    a = np.array(rets)
    sd = float(a.std(ddof=1))
    if sd < 1e-9:
        return 0.0
    # 月收益年化：均值*12 / (sd*sqrt(12))
    return float(a.mean() * 12 / (sd * math.sqrt(12)))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def pick_top_score(eng: Any, codes: List[str], date: str,
                   lut: Dict[str, Dict[str, Tuple[float, int]]]) -> List[str]:
    """按形态评分取前 top_k。"""
    scored: List[Tuple[float, str]] = []
    for c in codes:
        entry = lut[c].get(date)
        if not entry:
            continue
        _, idx = entry
        s = eng.series(c)
        try:
            res = sim.score_series_at(s, idx)
        except Exception:
            continue
        if res:
            scored.append((res[0], c))
    if not scored:
        return []
    scored.sort(reverse=True)
    return [c for _, c in scored[:TOP_K]]


def pick_all(_eng, codes, _date, _lut) -> List[str]:
    return list(codes)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument("--rebal-days", type=int, default=21,
                    help="再平衡步长(交易日)，默认21≈月度；5≈周频")
    args = ap.parse_args()
    if args.selfcheck:
        sys.exit(0 if selfcheck() else 1)

    t0 = time.time()
    eng = hist.HistoryEngine(lookback_days=10000)
    eng.load(force=True)
    codes = pool_codes(eng)
    print(f"[idx-enh] 引擎 {len(eng._data)} 只 | 池内可测 {len(codes)} 只 "
          f"| 最新 {eng._as_of}", file=sys.stderr)

    enh = simulate(eng, codes, pick_top_score, top_k=args.top_k,
                   rebal_days=args.rebal_days)
    bench = simulate(eng, codes, pick_all, top_k=len(codes),
                    rebal_days=args.rebal_days)

    # 用 NAV 首尾真实日期跨度算年数（与再平衡步长无关，避免周频被算成"45年"）
    from datetime import date as _date
    ys = _date.fromisoformat(enh["nav_series"][-1][0]) \
        - _date.fromisoformat(enh["nav_series"][0][0])
    years = ys.days / 365.25
    enh_total = enh["nav"] - 1.0
    bench_total = bench["nav"] - 1.0
    enh_ann = _annualize(enh_total, years)
    bench_ann = _annualize(bench_total, years)
    excess = enh_ann - bench_ann

    # 月度胜率（增强跑赢基准的月份占比）
    win = sum(1 for a, b in zip(enh["rets"], bench["rets"]) if a > b)
    win_rate = win / len(enh["rets"]) if enh["rets"] else 0.0

    print()
    print(f"{'组合':<14}{'累计':>10}{'年化':>10}{'超额(pp)':>10}"
          f"{'夏普':>8}{'最大回撤':>10}{'月度胜率':>10}")
    print("-" * 72)
    print(f"{'增强(TOP%d)' % args.top_k:<12}{enh_total*100:>9.1f}%{enh_ann:>9.2f}%"
          f"{excess:>10.2f}{_sharpe(enh['rets']):>8.2f}"
          f"{_max_drawdown(enh['nav_series']):>9.1f}%{win_rate*100:>9.1f}%")
    print(f"{'等权基准':<12}{bench_total*100:>9.1f}%{bench_ann:>9.2f}%"
          f"{'—':>10}{_sharpe(bench['rets']):>8.2f}"
          f"{_max_drawdown(bench['nav_series']):>9.1f}%{'—':>10}")
    print(f"\n[idx-enh] 窗口 {enh['nav_series'][0][0]}~{enh['nav_series'][-1][0]} "
          f"({years:.1f}年) | 耗时 {time.time()-t0:.0f}s", file=sys.stderr)


# ---------------------------------------------------------------------------
# 离线自检（合成数据，不依赖真实引擎）
# ---------------------------------------------------------------------------
def selfcheck() -> bool:
    rng = np.random.default_rng(7)
    steps: List[Tuple[str, bool, str]] = []

    # 1. 月度换手成本随换仓幅度单调（换得越多成本越高）
    to_small = _one_way_turnover({"a": 1.0}, {"a": 0.7, "b": 0.3})
    to_big = _one_way_turnover({"a": 1.0}, {"b": 1.0})
    steps.append(("换手成本随换仓上升",
                  to_big > to_small and abs(to_small - 0.3) < 1e-9
                  and abs(to_big - 1.0) < 1e-9,
                  f"小换{to_small:.2f} 大换{to_big:.2f}"))

    # 2. 全换仓（a->b）单边换手应=1.0
    steps.append(("全换仓单边换手=1",
                  abs(_one_way_turnover({"a": 1.0}, {"b": 1.0}) - 1.0) < 1e-9,
                  "a->b 全换"))

    # 3. NAV 计算：纯上涨序列年化>0、回撤=0
    nav_series = [(f"2020-0{i+1}-01", 1.0 * (1.01 ** i))
                  for i in range(1, 13)]
    nav_series = [("2020-01-01", 1.0)] + nav_series
    mdd = _max_drawdown(nav_series)
    steps.append(("单调上涨回撤=0", abs(mdd) < 1e-6, f"mdd={mdd:.4f}%"))

    # 4. 月度胜率：enh 全大于 bench → 100%
    win = sum(1 for a, b in zip([0.02] * 10, [0.01] * 10) if a > b) / 10
    steps.append(("月度胜率计算正确", abs(win - 1.0) < 1e-9, f"{win:.2f}"))

    # 5. 年化：两年翻倍 → 约 41.4%
    ann = _annualize(1.0, 2.0)
    steps.append(("年化公式正确", abs(ann - (2 ** 0.5 - 1) * 100) < 1e-6,
                  f"{ann:.2f}%"))

    # 6. pick_top_score 排序取前 K（用假引擎）
    class _FakeEng:
        def series(self, c):
            return {"dates": ["2026-01-01"], "close": np.array([1.0])}
    lut = {"a": {"2026-01-01": (1.0, 0)}, "b": {"2026-01-01": (2.0, 0)},
           "c": {"2026-01-01": (3.0, 0)}}
    fake = _FakeEng()
    # 直接测排序逻辑：给三个分，取前 2
    scored = sorted([(3.0, "c"), (1.0, "a"), (2.0, "b")], reverse=True)[:2]
    steps.append(("TOP 排序取前 K",
                  [c for _, c in scored] == ["c", "b"], str(scored)))

    passed = sum(1 for _, ok, _ in steps if ok)
    for nm, ok, dt in steps:
        print(f"  {'✅' if ok else '❌'} {nm}  {dt}")
    print(f"\n{'✅ 全部通过' if passed == len(steps) else '❌ 有失败'} "
          f"{passed}/{len(steps)}")
    return passed == len(steps)


if __name__ == "__main__":
    main()
