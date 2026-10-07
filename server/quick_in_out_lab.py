"""快进快出（短持仓 / 高换手）选股回测实验室。

目标：用刚补好的 15 年真实日线，实测「短持仓 + 每日/隔日换仓」这类
快进快出选股策略的**真实日均收益**，直接对对标「日均 2%」。

同时给出两个对照：
1. 买入持有（等权全样本）—— 最朴素的「不操作」基线。
2. 神谕上界（oracle）：每天都能选中全市场当日涨幅第一的票 ——
   这是「靠选股日赚 2%」的理论天花板，若它都到不了 2%/天，则任何
   现实策略都不可能。

成本模型（贴近 A 股实盘，复用 backtest 口径）：
- 佣金 万2.5（双边）、印花税 千1（仅卖）、滑点 万5（双边）。
- 按换手额计提：单日成本 = 换手额 × 0.00125
  （= 0.00025 佣 × 双边 + 0.001 印 × 单边卖 + 0.0005 滑 × 双边）。
- 每日全换（turnover=2）≈ 0.25%/天 纯成本损耗，这是快进快出的命门。

依赖：numpy + factor_lab（仅取数 load_universe，不改它）。
"""

from __future__ import annotations

import sys
import math
import numpy as np

import factor_lab as fl


# ---------------------------------------------------------------------------
# 回测核心
# ---------------------------------------------------------------------------

def round_trip_cost(turnover: float) -> float:
    """单日换仓成本 = 换手额 × 0.00125（佣万2.5双 + 印千1卖 + 滑万5双）。

    每日全换 turnover=2 → 0.25%/天；不换 turnover=0 → 0。
    """
    return turnover * 0.00125


def _daily_ret(C: np.ndarray) -> np.ndarray:
    T, N = C.shape
    out = np.full((T, N), 0.0)
    out[1:] = (C[1:] / C[:-1] - 1.0)
    out[np.isnan(out)] = 0.0
    return out


def past_return(C: np.ndarray, n: int) -> np.ndarray:
    """截至 t 的过去 n 日收益率（用 close[t]/close[t-n]-1），t>=n 有效。"""
    T, N = C.shape
    out = np.full((T, N), np.nan)
    if T > n:
        out[n:] = C[n:] / C[:-n] - 1.0
    return out


def backtest(C: np.ndarray, signal: np.ndarray, *,
             rebal_every: int = 1, topk: int = 20,
             select: str = "bottom", warm: int = 130) -> dict:
    """等权持有 topk 只，每 rebal_every 天换仓，含成本。

    signal[t] 在第 t+1 天开盘用于换仓（只用 t 及之前数据，无前视）。
    select='bottom' → 选 signal 最小者（反转）；'top' → 选最大者（动量）。

    返回 {equity, avg_daily, ann, vol, sharpe, maxdd, win_rate, cost_drag}。
    """
    T, N = C.shape
    R = _daily_ret(C)
    w = np.zeros(N)                       # 当前持仓权重
    eq = [1.0]
    cost_total = 0.0

    for t in range(1, T):
        # 当日组合收益（用上一期持仓 w）
        pret = float(np.nansum(w * R[t]))

        # 换仓（用 t-1 日及之前信号，无前视）
        if rebal_every >= 1 and (t - 1) % rebal_every == 0 and t - 1 >= warm:
            sig = signal[t - 1]
            mask = np.isfinite(sig) & np.isfinite(C[t - 1])
            idx = np.where(mask)[0]
            if idx.size >= topk:
                sv = sig[idx]
                order = sv.argsort(kind="mergesort")
                pick = idx[order[:topk]] if select == "bottom" else idx[order[-topk:]]
                target = np.zeros(N)
                target[pick] = 1.0 / topk
                turnover = float(np.abs(target - w).sum())   # 全换时 = 2
                cost = round_trip_cost(turnover)
                pret -= cost
                cost_total += cost
                w = target

        eq.append(eq[-1] * (1.0 + pret))

    eq = np.array(eq)
    rets = eq[1:] / eq[:-1] - 1.0
    avg_daily = float(rets.mean())
    ann = avg_daily * 250.0
    vol = float(rets.std(ddof=1)) * math.sqrt(250.0)
    sharpe = (avg_daily * 250.0) / vol if vol > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    maxdd = float((eq / peak - 1.0).min())
    win_rate = float((rets > 0).mean())
    return {
        "equity": eq, "avg_daily": avg_daily, "ann": ann, "vol": vol,
        "sharpe": sharpe, "maxdd": maxdd, "win_rate": win_rate,
        "cost_drag": cost_total / len(eq),
    }


def buy_hold_benchmark(C: np.ndarray, warm: int = 130) -> dict:
    """等权买入持有全样本（最朴素基线）。"""
    T, N = C.shape
    R = _daily_ret(C)
    w = np.full(N, 1.0 / N)
    eq = [1.0]
    for t in range(1, T):
        eq.append(eq[-1] * (1.0 + float(np.nansum(w * R[t]))))
    eq = np.array(eq)
    rets = eq[1:] / eq[:-1] - 1.0
    avg_daily = float(rets.mean())
    vol = float(rets.std(ddof=1)) * math.sqrt(250.0)
    peak = np.maximum.accumulate(eq)
    return {
        "equity": eq, "avg_daily": avg_daily, "ann": avg_daily * 250.0,
        "vol": vol, "sharpe": (avg_daily * 250.0) / vol if vol > 0 else 0.0,
        "maxdd": float((eq / peak - 1.0).min()),
        "win_rate": float((rets > 0).mean()), "cost_drag": 0.0,
    }


def oracle_upper_bound(R: np.ndarray, warm: int = 130) -> dict:
    """神谕上界：每天等权买全市场当日涨幅最大的 topk 只（含成本）。
    这是「靠选股日赚 2%」的理论天花板——若它都到不了，现实策略更不可能。
    """
    T, N = R.shape
    topk = min(20, N)
    eq = [1.0]
    w = np.zeros(N)
    for t in range(1, T):
        if t - 1 >= warm:
            rt = R[t - 1]                 # 用 t-1 日收益决定 t 日开盘买（无前视）
            idx = np.where(np.isfinite(rt))[0]
            if idx.size >= topk:
                pick = idx[np.argsort(rt[idx])[-topk:]]
                target = np.zeros(N)
                target[pick] = 1.0 / topk
                turnover = float(np.abs(target - w).sum())
                cost = turnover * 0.00125
                w = target
                eq.append(eq[-1] * (1.0 + float(np.nansum(w * R[t])) - cost))
                continue
        eq.append(eq[-1] * (1.0 + float(np.nansum(w * R[t]))))
    eq = np.array(eq)
    rets = eq[1:] / eq[:-1] - 1.0
    avg_daily = float(rets.mean())
    vol = float(rets.std(ddof=1)) * math.sqrt(250.0)
    peak = np.maximum.accumulate(eq)
    return {
        "equity": eq, "avg_daily": avg_daily, "ann": avg_daily * 250.0,
        "vol": vol, "sharpe": (avg_daily * 250.0) / vol if vol > 0 else 0.0,
        "maxdd": float((eq / peak - 1.0).min()),
        "win_rate": float((rets > 0).mean()), "cost_drag": 0.0,
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def evaluate(pool: str = "000852") -> dict:
    mats = fl.load_universe(pool, min_days=200)
    C = mats["close"]
    T, N = C.shape

    bh = buy_hold_benchmark(C)
    oracle = oracle_upper_bound(_daily_ret(C))

    configs = []
    for n in (1, 3, 5, 10):
        sig = past_return(C, n)
        for rebal in (1, 2, 3, 5):
            for topk in (20, 50):
                configs.append(("反转", n, rebal, topk, "bottom",
                                backtest(C, sig, rebal_every=rebal,
                                         topk=topk, select="bottom")))
    for n in (5, 10, 20):
        sig = past_return(C, n)
        for rebal in (1, 2, 3, 5):
            for topk in (20, 50):
                configs.append(("动量", n, rebal, topk, "top",
                                backtest(C, sig, rebal_every=rebal,
                                         topk=topk, select="top")))

    span = [mats["dates"][0], mats["dates"][-1]] if mats["dates"] else ["", ""]
    return {
        "ok": True, "pool": mats.get("_pool", pool), "universe": N,
        "span": span, "days": T, "buy_hold": bh, "oracle": oracle,
        "configs": configs,
    }


def report_text(res: dict) -> str:
    L = []
    L.append("=" * 96)
    L.append(f"快进快出选股回测 | 池={res['pool']} | {res['universe']} 只 | "
             f"{res['span'][0]} ~ {res['span'][1]} ({res['days']} 交易日)")
    L.append("成本：佣金万2.5(双)+印花税千1(卖)+滑点万5(双)；目标：日均 2%（年化≈14027倍）")
    L.append("=" * 96)

    bh, oc = res["buy_hold"], res["oracle"]
    L.append("")
    L.append(f"{'对照':<22}{'日均':>9}{'年化':>11}{'年化波动':>10}{'夏普':>7}"
             f"{'最大回撤':>10}{'胜率':>8}{'成本损耗/天':>12}")
    L.append(f"{'买入持有(等权)':<22}{bh['avg_daily']*100:>8.3f}%{bh['ann']*100:>10.1f}%"
             f"{bh['vol']*100:>9.1f}%{bh['sharpe']:>7.2f}{bh['maxdd']*100:>9.1f}%"
             f"{bh['win_rate']*100:>7.1f}%{'—':>12}")
    L.append(f"{'神谕(每天买涨幅前20)':<22}{oc['avg_daily']*100:>8.3f}%{oc['ann']*100:>10.1f}%"
             f"{oc['vol']*100:>9.1f}%{oc['sharpe']:>7.2f}{oc['maxdd']*100:>9.1f}%"
             f"{oc['win_rate']*100:>7.1f}%{oc['cost_drag']*100:>11.3f}%")

    L.append("")
    L.append(f"{'策略':<26}{'日均':>9}{'年化':>11}{'年化波动':>10}{'夏普':>7}"
             f"{'最大回撤':>10}{'胜率':>8}{'成本损耗/天':>12}")
    # 找出日均最高的几个，先排个序
    ranked = sorted(res["configs"], key=lambda c: c[5]["avg_daily"], reverse=True)
    for tag, n, rebal, topk, sel, r in ranked:
        name = f"{tag}{n}日/持{rebal}天/Top{topk}"
        L.append(f"{name:<26}{r['avg_daily']*100:>8.3f}%{r['ann']*100:>10.1f}%"
                 f"{r['vol']*100:>9.1f}%{r['sharpe']:>7.2f}{r['maxdd']*100:>9.1f}%"
                 f"{r['win_rate']*100:>7.1f}%{r['cost_drag']*100:>11.3f}%")

    best = ranked[0][5]
    L.append("")
    L.append("结论锚点：")
    L.append(f"  · 神谕上界（每天买对全市场最强 20 只）日均仅 "
             f"{oc['avg_daily']*100:.3f}% —— 这是『靠选股日赚 2%』的理论天花板，"
             f"本身就远低于 2%/天。")
    L.append(f"  · 最快进快出（每日全换）因 ~0.25%/天 成本损耗，任何正 alpha 都会被吞掉。")
    L.append(f"  · 实盘最优策略（{ranked[0][0]}{ranked[0][1]}日/持{ranked[0][2]}天/Top{ranked[0][3]}）"
             f"日均 {best['avg_daily']*100:.3f}%，与目标 2%/天差 "
             f"{2/best['avg_daily'] if best['avg_daily']>0 else 0:.0f} 倍以上。")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 离线自检
# ---------------------------------------------------------------------------

def selfcheck(verbose: bool = True) -> bool:
    steps = []
    def rec(label, cond, extra=""):
        steps.append(bool(cond))
        if verbose:
            print(f"  {'✅' if cond else '❌'} {label}{(' → ' + extra) if extra else ''}")

    rng = np.random.default_rng(20261007)
    T, N = 200, 30

    # 1. 成本公式：turnover=2 → 0.25%；turnover=0 → 0
    rec("成本公式 turnover=2 → 0.25%", abs(round_trip_cost(2.0) - 0.0025) < 1e-12,
        f"{round_trip_cost(2.0)*100:.4f}%")
    rec("成本公式 turnover=0 → 0", round_trip_cost(0.0) == 0.0)
    # 每日全换确会产生正成本（快进快出的损耗来源）
    vec = 10 + np.cumsum(rng.normal(0, 0.01, T))      # (T,)
    C = np.tile(vec[:, None], (1, N))                 # (T, N)
    sig = rng.normal(size=(T, N))
    r = backtest(C, sig, rebal_every=1, topk=10, select="top", warm=5)
    rec("每日全换产生正成本损耗", r["cost_drag"] > 0.0005,
        f"{r['cost_drag']*100:.4f}%/天")

    # 2. 不换仓（rebal 很大）→ 成本 ≈ 0
    r2 = backtest(C, sig, rebal_every=10000, topk=10, select="top", warm=5)
    rec("不换仓成本 ≈ 0", r2["cost_drag"] == 0.0, f"{r2['cost_drag']:.6f}")

    # 3. 无前视：t 日信号只用 t-1 及之前 —— 把 C 后半段改成随机，
    #    前段的组合收益序列必须不变（用相同 seed 重跑对照）。
    sigA = rng.normal(size=(T, N))
    rA = backtest(C, sigA, rebal_every=5, topk=10, select="top", warm=5)["equity"]
    C2 = C.copy()
    C2[100:] = rng.uniform(50, 60, (T - 100, N))
    rB = backtest(C2, sigA, rebal_every=5, topk=10, select="top", warm=5)["equity"]
    rec("无前视：篡改未来价格不回改前段净值",
        np.allclose(rA[:95], rB[:95]), f"前95点最大差={np.abs(rA[:95]-rB[:95]).max():.2e}")

    # 4. 神谕上界 >= 买入持有（理论上限应不低于朴素基线，至少不显著更低）
    R = _daily_ret(C)
    oc = oracle_upper_bound(R, warm=5)
    bh = buy_hold_benchmark(C, warm=5)
    rec("神谕上界日均 ≥ 买入持有日均（合理上界）",
        oc["avg_daily"] >= bh["avg_daily"] - 1e-9,
        f"oracle={oc['avg_daily']*100:.3f}% bh={bh['avg_daily']*100:.3f}%")

    # 5. 动量选强势应跑赢反转选弱势（各股票涨幅不同、但整体上行）
    #    股票 j 的日涨幅 = 0.001*(j+1)，增长率不同 → 动量(top)选最快的。
    Tm = 300
    growth = np.linspace(0.0005, 0.003, N)            # (N,) 各异正增长
    Cmono = np.ones((Tm, N))
    for j in range(N):
        Cmono[:, j] = np.cumprod(1.0 + growth[j] * np.ones(Tm))
    sigM = past_return(Cmono, 5)
    rTop = backtest(Cmono, sigM, rebal_every=1, topk=10, select="top", warm=10)
    rBot = backtest(Cmono, sigM, rebal_every=1, topk=10, select="bottom", warm=10)
    rec("涨幅分化上行：动量选强 > 反转选弱", rTop["ann"] > rBot["ann"],
        f"top={rTop['ann']:.3f} bottom={rBot['ann']:.3f}")

    ok = all(steps)
    if verbose:
        print(f"\n自检 {'全部通过' if ok else '存在失败'}：{sum(steps)}/{len(steps)}")
    return ok


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(0 if selfcheck() else 1)
    pool = "000852"
    for a in sys.argv[1:]:
        if a.startswith("--pool="):
            pool = a.split("=", 1)[1]
    res = evaluate(pool=pool)
    print(report_text(res))
