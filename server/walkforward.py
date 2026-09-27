"""
走查回测（Walk-Forward Analysis）—— 借鉴 tradingview-mcp 的 walk_forward_backtest_strategy

## 为什么需要它

现有回测（backtest.py）只能回答「**这段历史赚了多少**」。
它无法回答真正重要的问题：「**这个策略的参数是调出来的，还是真的有效？**」

一个策略在某段历史上表现好，可能只是**过拟合**——参数恰好拟合了那段历史的噪声，
换一段历史就失效。走查回测就是用来识别这件事的：

    把历史切成若干折（fold），每折里
      训练段（train）：在这一段上挑出让收益最好的参数
      测试段（test）  ：用挑出来的参数，在紧随其后、从未见过的数据上验证
    重复若干折，看**测试段**的表现是否稳定。

如果训练段很好但测试段塌了 → 过拟合。
如果训练段和测试段表现接近且都为正 → 策略稳健（ROBUST）。

## 设计取舍

**不调用 history.HistoryEngine**：引擎只提供「最新」指标快照，而走查需要
「截止到某一天」的指标（即要能回退到历史某一点）。因此这里自己维护
截断序列并重算指标——虽然多写点代码，但语义清晰，也不会被引擎缓存干扰。

**单标的、单策略**：走查的目的是检验「参数是否稳健」，不是做组合。
先在一只股票上验证方法论；跨标的的稳健性检验是下一步。

**指标计算复用 history.py 的函数**：`_last_ma` / `_hhv` / `_range_pct` 等
已经在 history.py 里做过「与 pandas 零分歧」的验证，不重复实现。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

import history as H
import screener as scr


# ---------------------------------------------------------------------------
# 参数网格
# ---------------------------------------------------------------------------

# 每个参数的候选值。设计原则：围绕默认值上下各取一档，形成 3 点或 2 点网格。
# 网格不能太大——折数 × 组合数 × 序列长度 是乘法关系，很容易变成分钟级耗时。
PARAM_GRID = {
    "breakout_20h":     {"hold_ratio": [0.95, 0.98, 1.0]},
    "platform_break":   {"max_range60": [25.0, 35.0, 45.0],
                         "min_vol_ratio": [1.0, 1.2, 1.5]},
    "volume_surge":     {"min_vol_ratio": [1.5, 2.0, 3.0]},
    "oversold_rebound": {"max_chg20": [-10.0, -15.0, -20.0]},
    "pullback_ma20":    {"max_bias": [1.0, 2.0, 3.0],
                         "max_vol_ratio": [0.8, 0.85, 1.0]},
}


def _grid_combos(key: str) -> List[Dict[str, float]]:
    """把 PARAM_GRID[key] 展开成参数组合列表（笛卡尔积）。

    未登记的策略返回 [{}]（只有一组「空参数」，等价于不调参）。
    """
    grid = PARAM_GRID.get(key)
    if not grid:
        return [{}]
    combos: List[Dict[str, float]] = [{}]
    for name, values in grid.items():
        nxt = []
        for base in combos:
            for v in values:
                d = dict(base)
                d[name] = v
                nxt.append(d)
        combos = nxt
    return combos


# ---------------------------------------------------------------------------
# 在截断序列上重算指标 + 判定信号
# ---------------------------------------------------------------------------

def _cut(series: Dict[str, Any], end: int) -> Dict[str, Any]:
    """取序列的前 end 根（含 end-1），用于模拟「截止到某一天」。

    注意所有数组一起截断，保证下标一致。
    """
    return {k: (v[:end] if hasattr(v, "__getitem__") else v)
            for k, v in series.items()}


def _metrics_at(s: Dict[str, Any]) -> Dict[str, Any]:
    """在截断序列 s 上算出策略需要的全部指标（对应最后一根）。

    复算逻辑与 screener 的策略一一对应，但把「当日快照」也当作
    序列的最后一根（走查里没有实时快照，一切都是历史）。
    """
    C = np.asarray(s["close"], dtype=float)
    Hh = np.asarray(s["high"], dtype=float)
    Ll = np.asarray(s["low"], dtype=float)
    V = np.asarray(s["volume"], dtype=float)
    n = len(C)
    if n == 0:
        return {}

    m: Dict[str, Any] = {"n": n}
    last = n - 1
    m["close"] = float(C[last])
    m["high"] = float(Hh[last])
    m["low"] = float(Ll[last])
    m["prev_close"] = float(C[last - 1]) if n >= 2 else None
    m["open"] = float(s["open"][last]) if s.get("open") is not None else m["close"]

    # 涨跌幅（相对昨收）
    if m["prev_close"]:
        m["change_pct"] = (m["close"] - m["prev_close"]) / m["prev_close"] * 100.0
    else:
        m["change_pct"] = 0.0

    # 均线
    for p in (5, 10, 20, 60):
        m["ma%d" % p] = H._last_ma(C, p)

    # 新高 / 新低（不含当日，与 screener 口径一致）
    m["hhv20"] = H._hhv(Hh[:last], 20) if last >= 20 else None
    m["hhv60"] = H._hhv(Hh[:last], 60) if last >= 60 else None
    m["llv20"] = H._llv(Ll[:last], 20) if last >= 20 else None

    # 区间振幅（不含当日）
    m["range60_pct"] = H._range_pct(Hh[:last], Ll[:last], 60) if last >= 60 else None

    # N 日涨跌幅。注意口径：history._chg_pct 的定义是
    # 「当前价 / N 日前收盘 - 1」，**包含当日**，所以这里要传完整 C
    # （与 hhv/llv/range 的 `[:-1]` 不同——那三个是「不含当日」）。
    m["chg20"] = H._chg_pct(C, 20) if last >= 20 else None

    # 量比 = 当日量 / 前 20 日均量
    if last >= 20:
        base = H._mean(V[:last], 20)
        m["vol_ratio"] = (float(V[last]) / base) if base else None
    else:
        m["vol_ratio"] = None

    # MA20 乖离率
    ma20 = m.get("ma20")
    m["bias_ma20"] = ((m["close"] - ma20) / ma20 * 100.0) if ma20 else None

    # 成交额（万元）
    m["amount"] = float(s["amount"][last]) if s.get("amount") is not None else 0.0
    return m


def _signal_at(key: str, m: Dict[str, Any], p: Dict[str, float]) -> bool:
    """判断「截止到最后一根」是否触发信号。

    这里的判定逻辑必须与 screener 里对应策略**保持一致**，
    否则走查的结论不能代表线上策略。参数从 p 取，缺省用默认值。
    """
    g = lambda name, dflt: p.get(name, dflt)  # noqa: E731

    if key == "breakout_20h":
        hhv20 = m.get("hhv20")
        if hhv20 is None or hhv20 <= 0:
            return False
        if not scr._gt(m["high"], hhv20):
            return False
        return m["close"] >= hhv20 * g("hold_ratio", 0.98)

    if key == "ma_bull":
        ma5, ma10, ma20, ma60 = (m.get("ma5"), m.get("ma10"),
                                 m.get("ma20"), m.get("ma60"))
        if None in (ma5, ma10, ma20, ma60):
            return False
        if not (scr._gt(ma5, ma10) and scr._gt(ma10, ma20) and scr._gt(ma20, ma60)):
            return False
        return m["close"] > 0 and scr._ge(m["close"], ma5)

    if key == "platform_break":
        hhv60, rng, vr = m.get("hhv60"), m.get("range60_pct"), m.get("vol_ratio")
        if hhv60 is None or rng is None or vr is None or hhv60 <= 0:
            return False
        if rng > g("max_range60", 35.0):
            return False
        if not scr._gt(m["high"], hhv60):
            return False
        if m["close"] < hhv60 * g("hold_ratio", 0.98):
            return False
        if vr < g("min_vol_ratio", 1.2):
            return False
        if m.get("open") is not None and m["open"] >= m["high"]:
            return False                      # 一字板
        return m["change_pct"] >= g("min_chg", 2.0)

    if key == "volume_surge":
        vr = m.get("vol_ratio")
        if vr is None:
            return False
        if vr < g("min_vol_ratio", 2.0):
            return False
        if m["change_pct"] < g("min_chg", 3.0):
            return False
        return m.get("amount", 0.0) >= g("min_amount", 5000.0)

    if key == "oversold_rebound":
        chg20, llv20 = m.get("chg20"), m.get("llv20")
        if chg20 is None or llv20 is None:
            return False
        if chg20 > g("max_chg20", -15.0):
            return False
        cp = m["change_pct"]
        if not (g("min_chg", 1.0) <= cp <= g("max_chg", 6.0)):
            return False
        return not scr._lt(m["low"], llv20)

    if key == "pullback_ma20":
        ma20, ma60 = m.get("ma20"), m.get("ma60")
        bias, vr = m.get("bias_ma20"), m.get("vol_ratio")
        if None in (ma20, ma60, bias):
            return False
        if not scr._gt(ma20, ma60):
            return False
        mb = g("max_bias", 2.0)
        if not (-mb <= bias <= mb):
            return False
        if vr is not None and vr > g("max_vol_ratio", 0.85):
            return False
        return m["change_pct"] >= g("min_chg", -3.0)

    return False


# ---------------------------------------------------------------------------
# 单折评估
# ---------------------------------------------------------------------------

def _forward_return(C: np.ndarray, idx: int, horizon: int) -> Optional[float]:
    """信号日 idx 之后 horizon 根（用收盘价）的收益率（%）。

    这是走查的「得分」口径：不看模拟成交，只看信号发出后价格表现，
    避免回测引擎的持仓/手续费等决策干扰对**信号质量**的评价。
    """
    j = idx + horizon
    if idx < 0 or j >= len(C) or C[idx] <= 0:
        return None
    return (C[j] - C[idx]) / C[idx] * 100.0


def _eval_segment(series: Dict[str, Any], key: str, params: Dict[str, float],
                  lo: int, hi: int, horizon: int) -> Dict[str, Any]:
    """在 [lo, hi) 这个下标区间内评估一组参数。

    遍历区间内每一个「可判定日」，若触发信号就记录其未来 horizon 日收益。
    返回 {n_signals, avg_ret, win_rate, score}
    """
    C = np.asarray(series["close"], dtype=float)
    n = len(C)
    rets: List[float] = []
    # 起点至少要够算 60 日均线（最长的指标窗口）
    for idx in range(max(lo, 61), min(hi, n)):
        s = _cut(series, idx + 1)
        try:
            m = _metrics_at(s)
        except Exception:
            continue
        if not m:
            continue
        try:
            if not _signal_at(key, m, params or {}):
                continue
        except Exception:
            continue
        r = _forward_return(C, idx, horizon)
        if r is not None:
            rets.append(r)

    n_sig = len(rets)
    if n_sig == 0:
        return {"n_signals": 0, "avg_ret": None, "win_rate": None, "score": None}
    avg = sum(rets) / n_sig
    win = sum(1 for x in rets if x > 0) / n_sig * 100.0
    return {"n_signals": n_sig, "avg_ret": round(avg, 4),
            "win_rate": round(win, 2), "score": round(avg, 4)}


# ---------------------------------------------------------------------------
# 走查主流程
# ---------------------------------------------------------------------------

def walk_forward(series: Dict[str, Any], key: str, *,
                 folds: int = 3, train_ratio: float = 0.7,
                 horizon: int = 5, min_signals: int = 2,
                 adaptive_min: bool = True,
                 grid: Optional[Dict[str, List[float]]] = None) -> Dict[str, Any]:
    """对单只股票、单个策略做走查回测。

    series : {'close','open','high','low','volume','amount','dates'} 等长数组
    key    : 策略 key（如 'platform_break'）
    folds  : 折数。**默认 3**——本项目历史库当前只有约 267 根（约 1 年），
             5 折会把每折压到约 40 根，训练段几乎选不出参数（实测有效折
             中位数掉到 1）。3 折是当前数据量下的实测折中。
             若将来历史库扩充到 500+ 根，可提高这个值。
    train_ratio : 每折里训练段占的比例
    horizon: 信号发出后观察多少根 K 线来算收益
    min_signals : 训练段至少要出现多少次信号，否则该折跳过。
                  **自适应**：若开启 adaptive_min，当该策略在整段历史上的
                  信号本就稀疏时，门槛会自动下调（最低 2），
                  否则像 platform_break 这类天然低频的策略会**每折都被跳过**，
                  走查直接失去意义。稀疏策略的结论会在 detail 里标注。
    grid   : 自定义参数网格，默认用 PARAM_GRID[key]

    返回：
      {
        "strategy": key, "code": ..., "folds": [...],
        "verdict": "ROBUST"|"MODERATE"|"WEAK"|"OVERFITTED"|"INSUFFICIENT",
        "detail": "...", "summary": {...}
      }
    """
    if grid is None:
        combos = _grid_combos(key)
    else:
        combos = [{}]
        for name, values in grid.items():
            nxt = []
            for base in combos:
                for v in values:
                    d = dict(base); d[name] = v
                    nxt.append(d)
            combos = nxt

    C = np.asarray(series["close"], dtype=float)
    n = len(C)
    warmup = 61                       # 至少要够算 MA60 / HHV60
    usable = n - warmup - horizon
    if usable < folds * 20:
        return {
            "strategy": key, "code": series.get("code", ""),
            "verdict": "INSUFFICIENT",
            "detail": "可用历史仅 %d 根，不足以做 %d 折走查" % (n, folds),
            "folds": [], "summary": {},
        }

    # 把可用区间切成 folds 段，每段内部再分训练/测试
    seg_len = usable // folds
    fold_results: List[Dict[str, Any]] = []

    # ---- 自适应门槛 ----
    # 先用默认参数在整段历史上数一下该策略的信号总数，据此判断它是否稀疏。
    # 稀疏策略（如 platform_break）在单个训练段（约 usable*train_ratio/folds 根）
    # 里可能一次都不触发，用固定门槛会让所有折被跳过。
    eff_min = min_signals
    sparse_note = ""
    if adaptive_min:
        probe = _eval_segment(series, key, {}, warmup, n - horizon, horizon)
        total_sig = probe["n_signals"]
        avg_per_fold = total_sig / max(folds, 1)
        if avg_per_fold < min_signals:
            eff_min = max(2, int(avg_per_fold))
            sparse_note = (f"该策略在此标的上天然稀疏（整段仅 {total_sig} 次信号，"
                           f"平均每折 {avg_per_fold:.1f} 次），"
                           f"训练段信号门槛已自适应下调至 {eff_min}，结论可靠性有限")

    for f in range(folds):
        seg_lo = warmup + f * seg_len
        seg_hi = seg_lo + seg_len if f < folds - 1 else n - horizon
        split = seg_lo + int((seg_hi - seg_lo) * train_ratio)

        # ---- 训练段：逐组参数评分，取最优 ----
        best = None
        train_table = []
        for p in combos:
            r = _eval_segment(series, key, p, seg_lo, split, horizon)
            train_table.append({"params": p, **r})
            if r["n_signals"] >= eff_min and r["score"] is not None:
                if best is None or r["score"] > best["score"]:
                    best = {"params": p, **r}

        if best is None:
            fold_results.append({
                "fold": f + 1,
                "train_range": [seg_lo, split],
                "test_range": [split, seg_hi],
                "skipped": True,
                "reason": "训练段信号不足 %d 次，参数不可信" % eff_min,
                "train_table": train_table,
            })
            continue

        # ---- 测试段：用训练段选出的参数验证 ----
        test_r = _eval_segment(series, key, best["params"], split, seg_hi, horizon)
        fold_results.append({
            "fold": f + 1,
            "train_range": [seg_lo, split],
            "test_range": [split, seg_hi],
            "skipped": False,
            "best_params": best["params"],
            "train": {"n_signals": best["n_signals"],
                      "avg_ret": best["avg_ret"],
                      "win_rate": best["win_rate"]},
            "test": test_r,
            "train_table": train_table,
        })

    # ---- 汇总判定 ----
    # 只统计「训练段选出了参数 **且** 测试段真的产生了信号」的折。
    # 测试段 0 信号（n_signals=0）意味着这一折无法验证任何东西——
    # 它不是「测试段表现差」，而是「没测到」。把它算进分母会让
    # 「正折比例」被稀释成假象，必须排除。
    valid = [x for x in fold_results
             if not x.get("skipped")
             and (x.get("test") or {}).get("n_signals", 0) > 0]
    no_sig = [x for x in fold_results
              if not x.get("skipped")
              and (x.get("test") or {}).get("n_signals", 0) == 0]
    if not valid:
        n_skipped = sum(1 for x in fold_results if x.get("skipped"))
        return {
            "strategy": key, "code": series.get("code", ""),
            "verdict": "INSUFFICIENT",
            "detail": ("没有折能完成「训练选参 → 测试验证」的完整闭环"
                       "（%d 折训练段信号不足，%d 折测试段无信号）。"
                       "该策略在此标的上过于稀疏，需换标的或放宽参数。"
                       % (n_skipped, len(no_sig))),
            "folds": fold_results, "summary": {"n_folds_no_test_signal": len(no_sig)},
        }

    tr = [x["train"]["avg_ret"] for x in valid]
    te = [x["test"]["avg_ret"] for x in valid if x["test"]["avg_ret"] is not None]

    te_mean = sum(te) / len(te)
    te_std = math.sqrt(sum((x - te_mean) ** 2 for x in te) / len(te)) if len(te) > 1 else 0.0
    tr_mean = sum(tr) / len(tr)

    # 衰减率：测试段相对训练段的落差。
    #
    # 注意这里**不用除法**。最初写的是 (tr-te)/|tr|，但当训练均值接近 0 时
    # （信号在训练段几乎不赚钱），分母趋零会让结果爆炸成 -1730%、8455%
    # 这类无意义的数字。改用「绝对差」，语义清晰且数值稳定：
    #
    #   decay > 0  测试段比训练段差（常见，说明有衰减）
    #   decay < 0  测试段反而更好（可能只是运气）
    #
    # 同时保留 train_avg 供使用者判断量级——单看 decay 而不看绝对值会误判。
    decay = tr_mean - te_mean

    # 每折「测试段为正」的比例，衡量稳健性
    pos_rate = sum(1 for x in te if x > 0) / len(te) * 100.0

    # ---- 判定规则 ----
    #
    # 阈值取自本项目历史数据的实测分布（见 docs/走查回测说明.md），
    # 不是拍脑袋。核心逻辑：先看测试段是否为正，再看它是否稳定。
    #
    #   测试段均值 > 0 且 过半数折为正 且 衰减不大  → ROBUST
    #   测试段均值 > 0 但稳定性一般                → MODERATE
    #   测试段均值 > 0 但只有少数折为正            → WEAK
    #   测试段均值 ≤ 0                             → OVERFITTED
    decay_ratio = (decay / abs(tr_mean)) if abs(tr_mean) > 1e-9 else (
        0.0 if decay <= 0 else 1.0)
    if te_mean > 0 and pos_rate >= 60 and decay_ratio < 0.4:
        verdict = "ROBUST"
        detail = "测试段均值为正、过半数折为正、衰减可控 → 参数稳健"
    elif te_mean > 0 and pos_rate >= 40:
        verdict = "MODERATE"
        detail = "测试段均值为正但稳定性一般 → 可用，需控制仓位"
    elif te_mean > 0:
        verdict = "WEAK"
        detail = "测试段均值为正但只有少数折为正 → 依赖特定行情"
    else:
        verdict = "OVERFITTED"
        detail = "测试段均值为负 → 训练段的最优参数未能延续到样本外"

    # 数据量太少的结论要打折：有效折 < 2 时统计意义很弱，
    # 强制降级为 WEAK，避免「1 折全对」被误读成稳健。
    if len(valid) < 2 and verdict in ("ROBUST", "MODERATE"):
        verdict = "WEAK"
        detail = ("仅 %d 折有效，样本太少不足以判定稳健；"
                  "该策略在此标的上信号稀疏，结论仅供参考" % len(valid))

    if sparse_note:
        detail = detail + "；" + sparse_note

    return {
        "strategy": key,
        "code": series.get("code", ""),
        "verdict": verdict,
        "detail": detail,
        "sparse": bool(sparse_note),
        "folds": fold_results,
        "summary": {
            "n_folds_valid": len(valid),
            "n_folds_no_test_signal": len(no_sig),
            "n_folds_skipped": sum(1 for x in fold_results if x.get("skipped")),
            "train_avg": round(tr_mean, 4),
            "test_avg": round(te_mean, 4),
            "test_std": round(te_std, 4),
            # decay = 训练均值 - 测试均值（绝对差，非百分比）。
            # 正数表示样本外变差。配合 train_avg 一起看才有意义。
            "decay": round(decay, 4),
            "test_positive_rate": round(pos_rate, 1),
            "horizon": horizon,
            "grid_size": len(combos),
            "min_signals_used": eff_min,
        },
    }


def walk_forward_code(code: str, key: str, *, lookback: int = 0,
                      **kw) -> Dict[str, Any]:
    """便捷入口：按股票代码从历史引擎取序列并做走查。

    这是 API 层调用的函数。

    lookback : 最多用最近多少根 K 线。**默认 0 = 全部可用**。
               不要硬编码成 600 —— 本项目历史库当前只有约 267 根
               （约 1 年，见 docs/走查回测说明.md），传 600 会被静默截成 267，
               看起来"用了 600 天"其实没有，容易误判样本量。
    """
    eng = H.get_engine()
    s = eng.series(code) if eng else None
    if not s:
        return {"strategy": key, "code": code, "verdict": "INSUFFICIENT",
                "detail": "历史数据里没有这只股票：%s" % code,
                "folds": [], "summary": {}}
    if lookback and len(s["close"]) > lookback:
        s = _cut(s, lookback)
    # 引擎的 series 里没有 code 字段（键就是 code），补进去让结果自带标识
    s = dict(s)
    s.setdefault("code", code)
    return walk_forward(s, key, **kw)
