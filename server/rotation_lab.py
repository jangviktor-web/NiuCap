"""轮动实验室：多因子合成 + 组合轮动回测（含交易成本、大盘风控、分段检验）。

与 factor_lab 的分工：
- factor_lab 回答「哪些因子有预测力」（单因子 IC/ICIR）。
- 本模块回答「把这些因子合成后，实际能跑出什么收益」。

合成原则（数据驱动，见 FACTOR_SPECS 注释）：
中证1000 上达标的 7 个因子里有 4 个（20日动量/MA20乖离/布林%B/RSI）横截面秩相关
高达 0.79~0.90，本质是同一个「短期涨了多少」维度，全部纳入只会重复计权、放大噪声。
故只取三个两两相关 < 0.32 的独立维度：短期反转、中期动量、量能。

回测纪律：
- t 日收盘用因子（仅含 t 日及之前数据）选股，t+1 日生效并计收益 —— 无前视。
- 换仓扣双边成本：佣金万2.5 + 印花税千1(卖出) + 滑点万5，口径取自 backtest.py。
- 大盘风控：成分股等权指数跌破 MA 时空仓（这是把回撤压下来的主要手段）。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# 与 server/backtest.py 保持一致（不要在这里另立一套费率口径）
COMMISSION = 0.00025      # 佣金 万2.5（双边）
STAMP_TAX = 0.001         # 印花税 千1（卖出单边）
SLIPPAGE = 0.0005         # 滑点 万5（单边）
# 单次「全额换仓」的成本率 = 卖出(佣+印+滑) + 买入(佣+滑)
FULL_TURNOVER_COST = (COMMISSION + STAMP_TAX + SLIPPAGE) + (COMMISSION + SLIPPAGE)

# 三因子合成：只取两两相关 < 0.32 的独立维度
# (因子key, 方向sign, 权重) —— sign 来自该因子 IC 的符号：
#   rev_5     IC>0 → 越大越好（跌多的反弹）
#   mom_60    IC<0 → 越小越好（涨多的回落）
#   vol_surge IC<0 → 越小越好（放量后的退潮）
FACTOR_SPECS: List[Tuple[str, int, float]] = [
    ("rev_5",     +1, 1.0),
    ("mom_60",    -1, 1.0),
    ("vol_surge", -1, 1.0),
]


# ---------------------------------------------------------------------------
# 因子合成
# ---------------------------------------------------------------------------

def rank_norm(v: np.ndarray) -> np.ndarray:
    """横截面秩标准化到 [0,1]，NaN 保持 NaN。用秩而非 z-score：抗异常值。

    必须按「逐行横截面」求秩。注意不能用 np.where(m)[0]——多维下它只返回
    第一维索引，会把整行重复取值（二维数组上会直接形状错乱）。
    """
    out = np.full(v.shape, np.nan)
    if v.ndim == 1:
        m = np.isfinite(v)
        n = int(m.sum())
        if n < 2:
            return out
        idx = np.flatnonzero(m)
        order = np.argsort(v[idx], kind="mergesort")
        r = np.empty(n, dtype=np.float64)
        r[order] = np.arange(1, n + 1, dtype=np.float64)
        out[idx] = (r - 1.0) / (n - 1.0)
        return out
    for t in range(v.shape[0]):
        row = v[t]
        m = np.isfinite(row)
        n = int(m.sum())
        if n < 2:
            continue
        idx = np.flatnonzero(m)
        order = np.argsort(row[idx], kind="mergesort")
        r = np.empty(n, dtype=np.float64)
        r[order] = np.arange(1, n + 1, dtype=np.float64)
        out[t, idx] = (r - 1.0) / (n - 1.0)
    return out


def composite(facs: Dict[str, np.ndarray],
              specs: Sequence[Tuple[str, int, float]] = FACTOR_SPECS
              ) -> np.ndarray:
    """合成综合因子矩阵 (T,N)。缺失因子按 0.5（中性）处理，避免单因子缺失
    直接把股票判死。"""
    keys = [s[0] for s in specs if s[0] in facs]
    if not keys:
        raise ValueError("没有可用因子")
    T, N = facs[keys[0]].shape
    acc = np.zeros((T, N))
    wsum = np.zeros((T, N))
    for key, sign, w in specs:
        if key not in facs:
            continue
        r = rank_norm(facs[key])
        ok = np.isfinite(r)
        val = np.where(ok, r, 0.5)          # 缺失→中性
        acc += sign * w * val
        wsum += np.where(ok, w, 0.0)
    with np.errstate(invalid="ignore"):
        out = np.where(wsum > 0, acc / np.maximum(wsum, 1e-12), np.nan)
    # 全缺失的行置 NaN
    allnan = np.all([~np.isfinite(facs[k]) for k in keys], axis=0)
    out = np.where(allnan, np.nan, out)
    return out


# ---------------------------------------------------------------------------
# 轮动回测
# ---------------------------------------------------------------------------

def equal_weight_index(close: np.ndarray) -> np.ndarray:
    """成分股等权指数净值（用于大盘风控）。"""
    r = np.full(close.shape, np.nan)
    if close.shape[0] > 1:
        r[1:] = close[1:] / close[:-1] - 1.0
    eq = np.nanmean(r, axis=1)
    eq = np.where(np.isfinite(eq), eq, 0.0)
    return np.cumprod(1.0 + eq)


def ma(arr: np.ndarray, n: int) -> np.ndarray:
    """简单移动平均（前 n-1 个为 NaN）。"""
    out = np.full(arr.shape, np.nan)
    if arr.shape[0] >= n:
        c = np.cumsum(np.insert(arr, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _turnover(old: Sequence[int], new: Sequence[int]) -> float:
    """换手率 = Σ|w_new - w_old| / 2，权重**包含现金**（空仓 = 100% 现金）。

    含现金才是成本口径正确的定义：
      满仓A → 满仓B(完全不同)：卖 100% + 买 100% → 1.0
      空仓   → 满仓          ：现金 100% 变股票，需买入 100% → 1.0
      不变                    ：0.0
    若不含现金，空仓→满仓会算成 0.5，交易成本被低估一半——风控频繁进出时
    这个误差会显著美化策略表现。
    """
    wo = {c: 1.0 / len(old) for c in old} if old else {}
    wn = {c: 1.0 / len(new) for c in new} if new else {}
    keys = set(wo) | set(wn)
    diff = sum(abs(wn.get(c, 0.0) - wo.get(c, 0.0)) for c in keys)
    cash_diff = abs(sum(wn.values()) - sum(wo.values()))   # 现金权重变化
    return (diff + cash_diff) / 2.0


def rotate(mats: Dict[str, Any], score: np.ndarray, *,
           rebal: int = 21, topk: int = 20,
           ma_win: int = 120, use_filter: bool = True,
           cost_on: bool = True,
           warm_days: Optional[int] = None) -> Dict[str, Any]:
    """组合轮动回测。

    rebal      调仓周期（交易日）
    topk       持有只数（等权）
    ma_win     大盘风控均线窗口；等权指数 < MA(ma_win) 时空仓
    use_filter 是否启用大盘风控
    cost_on    是否计交易成本

    返回 {equity, ret, stats, turnover, ...}
    """
    close = mats["close"]
    T, N = close.shape
    ret = np.full((T, N), np.nan)
    ret[1:] = close[1:] / close[:-1] - 1.0

    idx = equal_weight_index(close)
    idx_ma = ma(idx, ma_win)

    # 预热：因子最长窗口(120)与风控均线(ma_win)都要满足。
    # warm_days 可由调用方覆盖（如 walk-forward：因子已在全历史上预计算，
    # 窗口起点的信号本就有效，无需再 warm-up；传 0 即可让首日即参与换仓）。
    warm = warm_days if warm_days is not None else \
        (max(130, ma_win) if use_filter else 130)
    start = min(warm, T - 2)

    equity = [1.0]
    hold: List[int] = []
    turnovers: List[float] = []
    rets: List[float] = []
    in_market = 0

    for t in range(1, T):
        cost = 0.0
        sig_day = t - 1                      # 信号在 t-1 收盘产生，t 日生效
        if sig_day >= start and (sig_day - start) % rebal == 0:
            s = score[sig_day]
            ok = np.where(np.isfinite(s))[0]
            pick: List[int] = []
            if ok.size:
                k = min(topk, ok.size)
                # 取综合得分最高的 k 只
                order = ok[np.argsort(-s[ok], kind="mergesort")[:k]]
                pick = [int(x) for x in order]
            # 大盘风控
            if use_filter and np.isfinite(idx_ma[sig_day]) and idx[sig_day] < idx_ma[sig_day]:
                pick = []
            tv = _turnover(hold, pick)
            if tv > 0:
                turnovers.append(tv)
                if cost_on:
                    cost = tv * FULL_TURNOVER_COST
            hold = pick

        if hold:
            r = float(np.nanmean(ret[t][hold]))
            in_market += 1
        else:
            r = 0.0
        rets.append(r)
        equity.append(equity[-1] * (1.0 + r) * (1.0 - cost))

    eq = np.array(equity)
    rr = np.array(rets)
    st = _stats(eq, rr)
    st.update({
        "turnover_avg": float(np.mean(turnovers)) if turnovers else 0.0,
        "rebalances": len(turnovers),
        "in_market_ratio": in_market / max(1, len(rets)),
        "rebal": rebal, "topk": topk, "ma_win": ma_win,
        "use_filter": use_filter, "cost_on": cost_on,
        "start_date": mats["dates"][start] if mats["dates"] else "",
    })
    return {"equity": eq, "ret": rr, "stats": st}


def _stats(eq: np.ndarray, rr: np.ndarray) -> Dict[str, Any]:
    n = len(rr)
    mean = float(np.mean(rr)) if n else 0.0
    sd = float(np.std(rr, ddof=1)) if n > 1 else 0.0
    peak = np.maximum.accumulate(eq)
    mdd = float(np.min(eq / peak - 1.0))
    total = float(eq[-1] - 1.0)
    years = n / 250.0
    cagr = (eq[-1] ** (1.0 / years) - 1.0) if years > 0 and eq[-1] > 0 else None
    sharpe = (mean / sd * math.sqrt(250)) if sd > 0 else None
    pos = float(np.mean(rr > 0)) if n else 0.0
    return {"total": total, "cagr": cagr, "vol": sd * math.sqrt(250),
            "mdd": mdd, "sharpe": sharpe, "pos_ratio": pos,
            "days": n, "best": float(np.max(rr)) if n else 0.0,
            "worst": float(np.min(rr)) if n else 0.0}


def buyhold_benchmark(mats: Dict[str, Any]) -> Dict[str, Any]:
    """基准：成分股等权买入持有。"""
    close = mats["close"]
    T, N = close.shape
    ret = np.full((T, N), np.nan)
    ret[1:] = close[1:] / close[:-1] - 1.0
    rr = np.nanmean(ret[1:], axis=1)
    eq = np.cumprod(1.0 + np.where(np.isfinite(rr), rr, 0.0))
    eq = np.concatenate([[1.0], eq])
    st = _stats(eq, np.concatenate([[0.0], np.where(np.isfinite(rr), rr, 0.0)]))
    return {"equity": eq, "stats": st}


# ---------------------------------------------------------------------------
# 分段检验（样本外稳定性）
# ---------------------------------------------------------------------------

def segment_check(mats: Dict[str, Any], score: np.ndarray, *,
                  nseg: int = 3, **kw) -> List[Dict[str, Any]]:
    """把区间切成 nseg 段统计表现，看策略在各段是否稳定。

    稳定 = 各段都能跑赢基准或至少不大幅跑输；只在一段暴赚基本是运气/过拟合。

    注意：这里是「在全样本回测出的净值序列上按区间统计」，而不是把数据切段
    后各自重新回测。后者每段都要吃掉 130 天的因子/均线预热期，段长不够时
    策略几乎无法建仓（实测在场比例只有 1%），分段结果完全失真。
    因子与风控信号本身只依赖历史，全样本算好后再切区间统计是等价且正确的。
    """
    r = rotate(mats, score, **kw)
    b = buyhold_benchmark(mats)
    dates = mats["dates"]
    T = len(dates)
    step = T // nseg
    out: List[Dict[str, Any]] = []
    for i in range(nseg):
        lo = i * step
        hi = T if i == nseg - 1 else (i + 1) * step
        if hi - lo < 20:
            continue
        es = r["equity"][lo:hi]
        eb = b["equity"][lo:hi]
        if len(es) < 2:
            continue
        peak = np.maximum.accumulate(es)
        out.append({
            "seg": i + 1,
            "span": [dates[lo], dates[hi - 1]],
            "strat": float(es[-1] / es[0] - 1.0),
            "bench": float(eb[-1] / eb[0] - 1.0),
            "mdd": float(np.min(es / peak - 1.0)),
        })
    return out


def param_sensitivity(mats: Dict[str, Any], score: np.ndarray, *,
                      rebals: Sequence[int] = (10, 21, 42),
                      topks: Sequence[int] = (10, 20, 30),
                      **kw) -> List[Dict[str, Any]]:
    """参数敏感性：好策略应对参数不敏感（高原），过拟合则是孤峰。"""
    out: List[Dict[str, Any]] = []
    for rb in rebals:
        for tk in topks:
            try:
                r = rotate(mats, score, rebal=rb, topk=tk, **kw)
                out.append({"rebal": rb, "topk": tk, "stats": r["stats"]})
            except Exception as e:
                out.append({"rebal": rb, "topk": tk, "error": str(e)})
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run(pool: str = "000852", *, rebal: int = 21, topk: int = 20,
        ma_win: int = 120, use_filter: bool = True,
        cost_on: bool = True,
        specs: Sequence[Tuple[str, int, float]] = FACTOR_SPECS
        ) -> Dict[str, Any]:
    import factor_lab as fl
    mats = fl.load_universe(pool)
    facs = fl.build_factors(mats)
    score = composite(facs, specs)
    r = rotate(mats, score, rebal=rebal, topk=topk, ma_win=ma_win,
               use_filter=use_filter, cost_on=cost_on)
    bench = buyhold_benchmark(mats)
    return {
        "ok": True, "pool": mats.get("_pool", pool),
        "universe": len(mats["codes"]),
        "span": [mats["dates"][0], mats["dates"][-1]],
        "specs": [list(s) for s in specs],
        "result": r, "bench": bench,
        "segments": segment_check(mats, score, rebal=rebal, topk=topk,
                                  ma_win=ma_win, use_filter=use_filter,
                                  cost_on=cost_on),
    }


def report_text(res: Dict[str, Any]) -> str:
    L: List[str] = []
    s = res["result"]["stats"]
    b = res["bench"]["stats"]
    L.append("=" * 76)
    L.append(f"轮动回测 | 池={res['pool']} | {res['universe']} 只 | "
             f"{res['span'][0]} ~ {res['span'][1]}")
    L.append(f"调仓={s['rebal']}日 持Top{s['topk']} 风控MA{s['ma_win']} "
             f"(启用={s['use_filter']}) 计成本={s['cost_on']}")
    L.append(f"因子合成: {', '.join(k + ('+' if g > 0 else '-') for k, g, _ in res['specs'])}")
    L.append("=" * 76)
    L.append(f"{'':<12}{'累计':>10}{'年化':>10}{'波动':>10}{'最大回撤':>10}"
             f"{'夏普':>8}{'正日':>8}")
    for nm, x in (("轮动策略", s), ("等权基准", b)):
        L.append(f"{nm:<12}{x['total']*100:>+9.1f}%"
                 f"{(x['cagr']*100 if x['cagr'] is not None else 0):>+9.1f}%"
                 f"{x['vol']*100:>9.1f}%{x['mdd']*100:>9.1f}%"
                 f"{(x['sharpe'] or 0):>8.2f}{x['pos_ratio']*100:>7.0f}%")
    L.append(f"\n在场比例 {s['in_market_ratio']*100:.0f}% | "
             f"调仓 {s['rebalances']} 次 | 平均换手 {s['turnover_avg']*100:.0f}%")
    L.append("")
    L.append("分段稳定性（在全样本净值上按区间统计）：")
    for g in res["segments"]:
        if "error" in g:
            L.append(f"  段{g['seg']}: {g['error']}")
            continue
        L.append(f"  段{g['seg']} {g['span'][0]}~{g['span'][1]}: "
                 f"策略{g['strat']*100:>+7.1f}% 基准{g['bench']*100:>+7.1f}% "
                 f"超额{(g['strat']-g['bench'])*100:>+6.1f}pp "
                 f"回撤{g['mdd']*100:>6.1f}%")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 离线自检
# ---------------------------------------------------------------------------

def selfcheck(verbose: bool = True) -> Dict[str, Any]:
    steps: List[Tuple[str, bool, str]] = []

    def rec(label: str, cond: bool, extra: str = "") -> None:
        steps.append((label, bool(cond), extra))
        if verbose:
            print(f"  {'✅' if cond else '❌'} {label}{(' → ' + extra) if extra else ''}")

    rng = np.random.default_rng(20261007)

    # --- 1. rank_norm ---
    v = np.array([3.0, 1.0, 2.0, np.nan])
    r = rank_norm(v)
    rec("rank_norm 归一化到 [0,1] 且 NaN 保留",
        np.allclose(r[:3], [1.0, 0.0, 0.5]) and np.isnan(r[3]), f"{r.tolist()}")

    # --- 2. composite 方向与缺失处理 ---
    facs = {"rev_5": np.array([[0.9, 0.1, np.nan]]),
            "mom_60": np.array([[0.1, 0.9, 0.5]])}
    sc = composite(facs, [("rev_5", +1, 1.0), ("mom_60", -1, 1.0)])
    # rev_5 高且 mom_60 低的应得分最高
    rec("composite 方向正确（rev_5 正向、mom_60 反向）",
        bool(np.nanargmax(sc[0]) == 0), f"{sc[0].tolist()}")
    facs2 = {"rev_5": np.array([[np.nan, 0.1]]),
             "mom_60": np.array([[0.5, 0.9]])}
    sc2 = composite(facs2, [("rev_5", +1, 1.0), ("mom_60", -1, 1.0)])
    rec("composite 单因子缺失按中性(0.5)处理而非判死",
        bool(np.isfinite(sc2[0][0])), f"{sc2[0].tolist()}")

    # --- 3. ma ---
    a = np.arange(1.0, 6.0)
    m = ma(a, 2)
    rec("MA 计算正确", np.allclose(m[1:], [1.5, 2.5, 3.5, 4.5]) and np.isnan(m[0]),
        f"{m.tolist()}")

    # --- 4. 换手率 ---
    rec("换手率：完全相同组合 = 0", _turnover([1, 2], [1, 2]) == 0.0,
        f"{_turnover([1,2],[1,2])}")
    rec("换手率：完全换仓 = 1", abs(_turnover([1, 2], [3, 4]) - 1.0) < 1e-12,
        f"{_turnover([1,2],[3,4]):.4f}")
    rec("换手率：空仓↔满仓 = 1", abs(_turnover([], [1, 2]) - 1.0) < 1e-12,
        f"{_turnover([],[1,2]):.4f}")

    # --- 5. 回测无前视：篡改未来收益不应改变历史净值 ---
    T, N = 300, 60
    close = 10 + np.cumsum(rng.normal(0, 0.01, (T, N)), axis=0)
    mats = {"dates": [f"d{i}" for i in range(T)], "codes": [f"c{i}" for i in range(N)],
            "close": close.copy(), "high": close * 1.01, "low": close * 0.99,
            "open": close.copy(), "volume": np.full((T, N), 1e6),
            "amount": np.full((T, N), 1e8)}
    score = np.tile(np.arange(N, dtype=float), (T, 1))   # 固定偏好顺序
    r0 = rotate(mats, score, rebal=20, topk=10, ma_win=120,
                use_filter=False, cost_on=False)
    mats2 = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in mats.items()}
    mid = T // 2
    mats2["close"][mid:] = rng.uniform(50, 60, (T - mid, N))
    r1 = rotate(mats2, score, rebal=20, topk=10, ma_win=120,
                use_filter=False, cost_on=False)
    cut = mid - 25
    same = np.allclose(r0["equity"][:cut], r1["equity"][:cut])
    rec("回测无前视（篡改未来价格不回改历史净值）", bool(same),
        f"前半段净值一致={same}")

    # --- 6. 成本确实被扣除 ---
    r_nocost = rotate(mats, score, rebal=20, topk=10, use_filter=False, cost_on=False)
    r_cost = rotate(mats, score, rebal=20, topk=10, use_filter=False, cost_on=True)
    rec("计成本后净值低于不计成本", r_cost["equity"][-1] < r_nocost["equity"][-1],
        f"{r_cost['equity'][-1]:.4f} < {r_nocost['equity'][-1]:.4f}")

    # --- 7. 风控确实会空仓 ---
    r_f = rotate(mats, score, rebal=20, topk=10, ma_win=120,
                 use_filter=True, cost_on=False)
    rec("大盘风控生效（在场比例 < 100%）",
        r_f["stats"]["in_market_ratio"] < 1.0,
        f"在场 {r_f['stats']['in_market_ratio']*100:.0f}%")

    # --- 8. 恒定下跌市中空仓应保住净值 ---
    dn = np.tile(np.linspace(10, 5, T).reshape(T, 1), (1, N))
    mats_dn = {"dates": [f"d{i}" for i in range(T)], "codes": [f"c{i}" for i in range(N)],
               "close": dn.copy(), "high": dn * 1.01, "low": dn * 0.99,
               "open": dn.copy(), "volume": np.full((T, N), 1e6),
               "amount": np.full((T, N), 1e8)}
    r_dn = rotate(mats_dn, score, rebal=20, topk=10, ma_win=60,
                  use_filter=True, cost_on=False)
    rec("单边下跌市：风控使净值不随基准深跌（末值接近 1）",
        r_dn["equity"][-1] > 0.9, f"末值={r_dn['equity'][-1]:.4f}")

    ok = all(s[1] for s in steps)
    out = {"ok": ok, "steps": [{"name": s[0], "ok": s[1], "extra": s[2]}
                               for s in steps],
           "fails": [s[0] for s in steps if not s[1]]}
    if verbose:
        print(f"\n自检 {'全部通过' if ok else '存在失败'}："
              f"{sum(1 for s in steps if s[1])}/{len(steps)}")
    return out


if __name__ == "__main__":
    import sys
    if "--selfcheck" in sys.argv:
        r = selfcheck()
        sys.exit(0 if r["ok"] else 1)
    kw: Dict[str, Any] = {}
    for a in sys.argv[1:]:
        if a.startswith("--"):
            k, _, v = a[2:].partition("=")
            if not v:
                continue
            # pool 是字符串代码（前导零有意义，如 000852），不能转 int
            kw[k] = v if k == "pool" else (int(v) if v.lstrip("-").isdigit() else v)
    pool = str(kw.pop("pool", "000852"))
    res = run(pool, **kw)
    print(report_text(res))
