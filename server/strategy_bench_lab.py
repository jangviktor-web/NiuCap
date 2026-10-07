#!/usr/bin/env python3
"""
面板技术指标策略 —— 全历史「信号→回测」实测
==========================================

回答一个问题：**strategies.py 里那些面板自带的技术指标信号
（MA 交叉 / MACD / RSI / 布林带 / KDJ / 共振 / 买入持有），在真实 15 年数据上
到底能不能跑赢「买入持有」？**

为什么需要这个脚本
------------------
- 面板的 `strategy_eval.py` 只评估 `screener.py` 的**选股**策略（截面 IC），
  且受历史引擎 `lookback_days=760` 的 cutoff 限制，只测近 ~500 个交易日。
- `strategies.py` 的技术指标信号只产出买卖点（1/-1/0），**从来没人验证过
  这些点算出来的择时实盘收益**。本脚本补上这一环。

方法
----
- 数据：直接加载全量日线（HistoryEngine(lookback_days=10000).load(force)，
       绕过 760 天 cutoff），覆盖到 2011~2015 年。
- 样本：沪深300（000300）+ 中证1000（000852）成分股，均已补全历史，共 ~1300 只，
       同时覆盖大盘与小微盘、且历经多轮牛熊。
- 每只股票：对其 K 线生成各策略信号 → backtest(pyramid=False, 满仓择时)
           → 收集 年化 / 胜率 / 盈亏比 / 回撤 / 交易次数 / 持仓天数
- 聚合：全市场平均「择时年化」vs 平均「买入持有年化」（超额），及胜率/盈亏比分布
- 成本：复用 backtest.py 真实口径（佣万2.5双 + 印千1卖 + 滑万5双 + T+1 + 整手）

判读
----
- 超额 > 0 且稳定：择时有效（少数）
- 超额 ≈ 0 或为负：择时等于/不如持有（多数技术指标的常态）
- buy_hold 作为基准行：其年化应≈股票持有年化，用于校验回测接入无误

注意：本脚本不改任何面板代码，是独立的实测工具。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategies as st          # noqa: E402
import backtest as bt            # noqa: E402
import history as hist           # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
def load_full_engine() -> Any:
    """加载全量日线（绕过 760 天 cutoff），覆盖 15 年历史。"""
    eng = hist.HistoryEngine(lookback_days=10000)
    eng.load(force=True)
    return eng


def _norm_code(code: str) -> str:
    """把无前缀的指数成分代码规范成 daily_bars/引擎的带前缀格式。

    index_pools.json 存的是裸代码（如 '000001'），而 daily_bars 的 code
    带市场前缀（sz/sh/bj）。规则：0/3→sz，6→sh，8/4→bj。
    """
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("8", "4")):
        return "bj" + code
    return code


def pool_codes(eng: Any, pools=("000300", "000852")) -> List[str]:
    """取沪深300 + 中证1000 成分（去重、带前缀规范化），只保留引擎里有的。"""
    path = os.path.join(ROOT, "data", "index_pools.json")
    with open(path, "r", encoding="utf-8") as f:
        p = json.load(f)
    codes: set = set()
    for key in pools:
        v = p.get(key, {})
        cs = v.get("codes", []) if isinstance(v, dict) else v
        codes.update(_norm_code(c) for c in cs)
    return sorted(c for c in codes if eng.series(c))


# ---------------------------------------------------------------------------
# 单只回测
# ---------------------------------------------------------------------------
def bench_one(code: str, eng: Any, fn) -> Dict[str, float]:
    s = eng.series(code)
    df = pd.DataFrame({
        "close": s["close"], "high": s["high"],
        "low": s["low"], "open": s["open"],
    })
    klines = [{"date": d, "open": float(o), "close": float(c),
               "high": float(h), "low": float(l)}
              for d, o, c, h, l in zip(
                  s["dates"], s["open"], s["close"], s["high"], s["low"])]
    sig = fn(df)
    if not isinstance(sig, np.ndarray):
        sig = np.array(list(sig))
    res = bt.backtest(klines, [int(x) for x in sig], pyramid=False)
    n = len(s["dates"])
    bh_ann = ((float(s["close"][-1]) / float(s["close"][0])) ** (244.0 / n) - 1) * 100.0
    return {
        "ann": res["annual_return"],
        "bh_ann": bh_ann,
        "win": res["win_rate"],
        "pl": res["pl_ratio"],
        "mdd": res["max_drawdown"],
        "trades": res["trades"],
        "hold": res["avg_hold_days"],
        "sharpe": res["sharpe"],
        "total": res["total_return"],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只测前 N 只（调试用）")
    ap.add_argument("--selfcheck", action="store_true", help="离线自检")
    args = ap.parse_args()
    if args.selfcheck:
        selfcheck()
        return

    t0 = time.time()
    eng = load_full_engine()
    codes = pool_codes(eng)
    if args.limit:
        codes = codes[:args.limit]
    print(f"[bench] 引擎 {len(eng._data)} 只 | 池内可测 {len(codes)} 只 "
          f"| 最新 {eng._as_of}", file=sys.stderr)

    agg: Dict[str, Dict[str, List[float]]] = {
        name: {"ann": [], "bh_ann": [], "win": [], "pl": [], "mdd": [],
               "trades": [], "hold": [], "sharpe": [], "total": []}
        for name in st.STRATEGY_MAP
    }

    for ci, code in enumerate(codes):
        s = eng.series(code)
        if len(s["dates"]) < 800:        # 历史太短跳过
            continue
        for name, fn in st.STRATEGY_MAP.items():
            try:
                r = bench_one(code, eng, fn)
            except Exception:
                continue
            a = agg[name]
            for k in r:
                a[k].append(r[k])
        if ci % 100 == 0:
            print(f"[bench] 进度 {ci}/{len(codes)} 已用 {time.time()-t0:.0f}s",
                  file=sys.stderr)

    # ---- 聚合输出 ----
    print()
    print(f"{'策略':<12}{'择时年化':>10}{'持有年化':>10}{'超额(pp)':>10}"
          f"{'胜率':>8}{'盈亏比':>8}{'中位回撤':>10}{'中位交易':>9}{'中位持仓':>9}{'样本':>7}")
    print("-" * 95)
    for name in st.STRATEGY_MAP:
        a = agg[name]
        m = len(a["ann"])
        if m == 0:
            continue
        mean_ann = float(np.mean(a["ann"]))
        mean_bh = float(np.mean(a["bh_ann"]))
        excess = mean_ann - mean_bh
        print(f"{name:<12}{mean_ann:>9.2f}%{mean_bh:>9.2f}%{excess:>10.2f}"
              f"{np.mean(a['win']):>7.1f}%{np.mean(a['pl']):>8.2f}"
              f"{np.median(a['mdd']):>9.1f}%{np.median(a['trades']):>9.0f}"
              f"{np.median(a['hold']):>9.1f}{m:>7}")
    print(f"\n[bench] 总耗时 {time.time()-t0:.0f}s", file=sys.stderr)


# ---------------------------------------------------------------------------
# 离线自检（不依赖真实数据）
# ---------------------------------------------------------------------------
def selfcheck() -> None:
    rng = np.random.default_rng(0)
    n = 300
    up = np.cumprod(1 + rng.normal(0.002, 0.012, n))     # 单边上涨合成
    df_up = pd.DataFrame({"close": up, "high": up * 1.01,
                          "low": up * 0.99, "open": up})
    sig = st.strategy_ma_cross(df_up)
    kl = [{"date": str(i), "open": up[i], "close": up[i],
           "high": up[i] * 1.01, "low": up[i] * 0.99}
          for i in range(n)]
    res = bt.backtest(kl, [int(x) for x in sig], pyramid=False)

    steps: List[tuple] = []

    # 1. ma_cross 能产出买卖信号
    steps.append(("ma_cross 产出买卖信号",
                  int((sig == 1).sum()) + int((sig == -1).sum()) > 0,
                  f"买{int((sig == 1).sum())} 卖{int((sig == -1).sum())}"))
    # 2. backtest 正常成交
    steps.append(("backtest 正常成交", res["trades"] > 0, f"trades={res['trades']}"))
    # 3. buy_hold 年化 ≈ 持有年化（校验回测接入无误）
    r_bh = bt.backtest(kl, [int(x) for x in st.strategy_buy_hold(df_up)],
                       pyramid=False)
    bh_ann = ((up[-1] / up[0]) ** (244.0 / n) - 1) * 100
    steps.append(("buy_hold 年化≈持有年化", abs(r_bh["annual_return"] - bh_ann) < 1.0,
                  f"回测{bh_ann:.1f}% vs 实际{bh_ann:.1f}%"))
    # 4. 无前视：截断序列（前 200 天）的末日信号应与全量第 200 天一致
    df_k = pd.DataFrame({"close": up[:200], "high": up[:200] * 1.01,
                         "low": up[:200] * 0.99, "open": up[:200]})
    sig_k = st.strategy_ma_cross(df_k)
    ok_nolook = bool(sig_k[-1] == sig[199])
    steps.append(("无前视(截断末日信号一致)",
                  ok_nolook, f"sig_k[-1]={sig_k[-1]} sig[199]={sig[199]}"))
    # 5. 所有 7 个策略函数都能跑出回测（不崩）
    ok_all = True
    detail = ""
    for name, fn in st.STRATEGY_MAP.items():
        try:
            r = bt.backtest(kl, [int(x) for x in fn(df_up)], pyramid=False)
            if r is None or r["trades"] < 0:
                ok_all = False
        except Exception as e:
            ok_all = False
            detail = f"{name}:{e}"
    steps.append(("7 个策略函数均能回测", ok_all, detail or "ok"))

    passed = sum(1 for _, ok, _ in steps if ok)
    for nm, ok, dt in steps:
        print(f"  {'✅' if ok else '❌'} {nm}  {dt}")
    print(f"\n{'✅ 全部通过' if passed == len(steps) else '❌ 有失败'} "
          f"{passed}/{len(steps)}")
    sys.exit(0 if passed == len(steps) else 1)


if __name__ == "__main__":
    main()
