"""市场宽度历史序列（方案 4）—— 市场的「健康档案」。

回答的问题
----------
指数只告诉你「平均分」，宽度告诉你「班级整体在干嘛」：
    · 今天多少家涨、多少家跌？          → 涨跌家数（赚钱效应的广度）
    · 多少只股票站在自己的 20 日线上？   → MA20 上方比例（中期趋势的底盘）
    · 多少只创 60 日新高 / 新低？        → 新高新低（多头的后排 / 伤兵营）
    · 多少家涨停 / 跌停？                → 情绪的两端

怎么用（历史经验，不是铁律）
--------------------------
宽度指标是**反向参考**的极端读数最值钱：
    站上 MA20 的比例 < 5% 分位 → 市场极度恐慌，历史上常在阶段底部附近
    站上 MA20 的比例 > 95% 分位 → 市场过热，追高风险大
平时它用于确认趋势：指数新高 + 宽度同步走高 = 健康上涨；
指数新高 + 宽度背离（新高家数萎缩）= 上涨后劲不足。

口径与近似
----------
    · 涨跌：当日收盘 vs 该股前一根 bar 收盘（停牌股复牌日一并结算）。
    · 涨停/跌停：收盘涨幅 ≥ 板块涨停比例（主板 10%、创业/科创 20%、
      北交 30%，见 equity_sim.limit_ratio）。库里没有 ST 标记，ST 股
      实际 5% 会被漏进「涨停」——家数口径是近似值，看趋势不看绝对值。
    · 新高/新低：当日收盘 ≥（≤）前 59 个交易日收盘的最值，即「创 60 日
      新高/新低」，窗口不含当日、当日单独比较。
    · MA20 上方比例的分母只含「数据 ≥ 20 根」的股票，新股不凑数。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np

# ---------------------------------------------------------------------------
# 缓存：全市场逐日统计要 2~3 秒，按数据版本（as_of）缓存
# ---------------------------------------------------------------------------
_CACHE: Dict[str, Any] = {"as_of": None, "ts": 0.0, "value": None}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 6 * 3600.0


def _limit_ratio_dn(code: str) -> float:
    """跌停比例（昨收 × 此值 ≈ 跌停价）。与涨停口径镜像。"""
    if code.startswith("sh68") or code.startswith("sz30"):
        return 0.802
    if code.startswith("bj"):
        return 0.702
    return 0.902


# counts 列含义（与 _compute_one 的写出顺序一一对应）
#   0 adv  1 dec  2 flat  3 limup  4 limdn
#   5 above20  6 denom20  7 nh60  8 nl60  9 denom60
_COLS = 10


def _compute_all(eng: Any) -> Dict[str, np.ndarray]:
    """对全市场逐日聚合宽度计数。返回 {date: counts_row}（行内为 int32 数组）。"""
    all_dates = set()
    for s in eng._data.values():
        all_dates.update(s["dates"])
    i2d = sorted(all_dates)
    date2i = {d: i for i, d in enumerate(i2d)}
    n = len(i2d)

    counts = np.zeros((n, _COLS), dtype=np.int32)

    from equity_sim import limit_ratio

    for code, s in eng._data.items():
        C = s["close"]
        m = len(C)
        if m < 2:
            continue

        def _gidx(sl: slice, length: int) -> np.ndarray:
            # dates[sl] 的全局日期索引
            return np.fromiter((date2i[d] for d in s["dates"][sl]),
                               dtype=np.int32, count=length)

        # ---- 涨 / 跌 / 平（相对前一根 bar）----
        prev = C[:-1].astype(np.float64)
        cur = C[1:].astype(np.float64)
        idx1 = _gidx(slice(1, None), m - 1)
        np.add.at(counts[:, 0], idx1, (cur > prev).astype(np.int32))
        np.add.at(counts[:, 1], idx1, (cur < prev).astype(np.int32))
        np.add.at(counts[:, 2], idx1, (cur == prev).astype(np.int32))

        # ---- 近似涨停 / 跌停（收盘口径）----
        ratio_up = limit_ratio(code)
        ratio_dn = _limit_ratio_dn(code)
        np.add.at(counts[:, 3], idx1,
                  (cur >= prev * ratio_up - 1e-9).astype(np.int32))
        np.add.at(counts[:, 4], idx1,
                  (cur <= prev * ratio_dn + 1e-9).astype(np.int32))

        # ---- 站上 MA20（需 ≥20 根）----
        # ma20[t] = (cs[t] - cs[t-20]) / 20，t=19..m-1；t=19 时减数为 0
        if m >= 20:
            cs = np.cumsum(C, dtype=np.float64)
            ma20 = (cs[19:] - np.concatenate(([0.0], cs[:m - 20]))) / 20.0
            above = (C[19:].astype(np.float64) > ma20)
            idx20 = _gidx(slice(19, None), m - 19)
            np.add.at(counts[:, 5], idx20, above.astype(np.int32))
            np.add.at(counts[:, 6], idx20, 1)

        # ---- 创 60 日新高 / 新低（需 ≥60 根；前 59 日窗口不含当日）----
        # sliding_window_view(C, 59) 第 k 个窗口 = C[k .. k+58]，对应
        # 目标日 t = k+59；可用窗口 k ∈ [0, m-59]，共 m-59 个
        if m >= 60:
            sw = np.lib.stride_tricks.sliding_window_view(C, 59)
            prev_max = sw.max(axis=1)[:m - 59]
            prev_min = sw.min(axis=1)[:m - 59]
            cur60 = C[59:].astype(np.float64)
            idx60 = _gidx(slice(59, None), m - 59)
            np.add.at(counts[:, 7], idx60,
                      (cur60 >= prev_max.astype(np.float64)).astype(np.int32))
            np.add.at(counts[:, 8], idx60,
                      (cur60 <= prev_min.astype(np.float64)).astype(np.int32))
            np.add.at(counts[:, 9], idx60, 1)

    return {i2d[i]: counts[i] for i in range(n)}


def _pct_rank(values: List[float], latest: float) -> Optional[float]:
    """最新值在历史窗口内的百分位（0~100，越高代表越「热」）。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(float(np.mean([1.0 if v <= latest else 0.0 for v in vals]))
                 * 100.0, 1)


# ---------------------------------------------------------------------------
# 市况开关（方案 D）：全市场等权指数的 Choppiness Index
# ---------------------------------------------------------------------------
# 蒸馏源：cinar/indicator volatility/chop.go。Chop<38.2=趋势市、>61.8=震荡市、
# 之间=过渡市。Chop 需要「指数」的 OHLC，但我们只有逐股日线——没有真实的
# 全市场指数。做法：用横截面等权日收益合成指数收盘，用横截面日内半振幅均值
# 为合成指数造出每日 H/L（合成口径，界面注明）。
# 该序列按数据版本缓存（与宽度同 TTL），一次构建约 2~3 秒。

import indicators_extra as _ix          # 延迟在模块中部 import，避免环依赖顾虑

_REGIME_CACHE: Dict[str, Any] = {"as_of": None, "ts": 0.0, "value": None}
_REGIME_TTL = 6 * 3600.0

# 市况阈值（与 chop.go 通用约定一致）
_CHOP_TREND = 38.2
_CHOP_RANGE = 61.8


def _compute_market_index(eng: Any) -> Dict[str, np.ndarray]:
    """全市场等权指数（合成）的 OHLC 序列。

    收盘 P[t] = P[t-1] × (1 + r_mkt[t])，r_mkt[t] 为当日全市场等权日收益均值。
    高/低用「指数当日典型波动」r̄[t] 造：P_high = P[t-1]×(1+r_mkt+r̄)、
    P_low = P[t-1]×(1+r_mkt−r̄)。r̄[t] 取当日全市场日收益的**横截面均值绝对值**
    （典型个股日波动幅度 ~1.x%，是指数真实日摆幅的代理），这样有真实趋势
    时漂移会压过波动、Chop 降下来；纯震荡时漂移≈0、波动主导、Chop 升。
    无效日期（无样本）填 0 → 指数持平，不污染 Chop。
    返回 {dates, P, Phigh, Plow}（等长）。
    """
    all_dates = set()
    for s in eng._data.items():
        all_dates.update(s[1]["dates"])
    i2d = sorted(all_dates)
    date2i = {d: i for i, d in enumerate(i2d)}
    n = len(i2d)

    sum_r = np.zeros(n)
    sum_ar = np.zeros(n)
    cnt_r = np.zeros(n)

    for code, s in eng._data.items():
        C = s["close"].astype(np.float64)
        m = len(C)
        if m < 2:
            continue

        def _gidx(sl: slice, length: int) -> np.ndarray:
            return np.fromiter((date2i[d] for d in s["dates"][sl]),
                               dtype=np.int32, count=length)

        idx1 = _gidx(slice(1, None), m - 1)
        ret = (C[1:] / C[:-1] - 1.0)
        np.add.at(sum_r, idx1, ret)
        np.add.at(sum_ar, idx1, np.abs(ret))
        np.add.at(cnt_r, idx1, 1.0)

    mean_r = np.where(cnt_r > 0, sum_r / np.maximum(cnt_r, 1e-9), 0.0)
    mean_ar = np.where(cnt_r > 0, sum_ar / np.maximum(cnt_r, 1e-9), 0.0)

    P = np.ones(n) * 100.0
    for t in range(1, n):
        P[t] = P[t - 1] * (1.0 + mean_r[t])
    Phigh = np.empty(n)
    Plow = np.empty(n)
    Phigh[0] = P[0]
    Plow[0] = P[0]
    for t in range(1, n):
        base = P[t - 1] * (1.0 + mean_r[t])
        Phigh[t] = base + P[t - 1] * mean_ar[t]
        Plow[t] = base - P[t - 1] * mean_ar[t]
    return {"dates": i2d, "P": P, "Phigh": Phigh, "Plow": Plow}


def market_regime_series(eng: Any, use_cache: bool = True) -> Dict[str, Any]:
    """全市场逐日 Chop 序列 + 市况标签（趋势/震荡/过渡）。

    返回 {dates, chop:[float|None], tag:[str|None]}（与 dates 等长）。
    按数据版本缓存。
    """
    as_of = eng._as_of
    if (use_cache and _REGIME_CACHE["value"] is not None
            and _REGIME_CACHE["as_of"] == as_of
            and time.time() - _REGIME_CACHE["ts"] < _REGIME_TTL):
        return _REGIME_CACHE["value"]

    idx = _compute_market_index(eng)
    chops = _ix.chop_series(idx["Phigh"], idx["Plow"], idx["P"], 14)
    tags: List[Optional[str]] = []
    for v in chops:
        if not np.isfinite(v):
            tags.append(None)
        elif v < _CHOP_TREND:
            tags.append("trend")
        elif v > _CHOP_RANGE:
            tags.append("range")
        else:
            tags.append("transition")
    out = {
        "dates": idx["dates"],
        "chop": [None if not np.isfinite(v) else round(float(v), 2)
                 for v in chops],
        "tag": tags,
    }
    _REGIME_CACHE["as_of"] = as_of
    _REGIME_CACHE["ts"] = time.time()
    _REGIME_CACHE["value"] = out
    return out


def compute(days: int = 250, use_cache: bool = True) -> Dict[str, Any]:
    """返回最近 days 个交易日的宽度序列 + 最新读数的分位。

    返回
    ----
    {
      "ok": True, "as_of": str, "days": int,
      "series": [{date, adv, dec, flat, limup, limdn,
                  above20_ratio, nh60, nl60}, ...],   # 时间升序
      "latest": {date, adv, dec, flat, limup, limdn,
                 above20_ratio, above20_pct, adv_pct,
                 nh60, nl60, extreme, extreme_note},
    }
    """
    import history

    # auto_load=True：引擎没加载时在这里加载（约 10 秒 < 网关 60 秒），
    # 而不是直接报「未加载」把用户挡回去。加载有锁，并发请求安全。
    eng = history.get_engine(auto_load=True)
    if not eng or not eng._data:
        return {"ok": False, "error": "历史数据未加载"}

    as_of = eng._as_of
    with _CACHE_LOCK:
        if (use_cache and _CACHE["value"] is not None
                and _CACHE["as_of"] == as_of
                and time.time() - _CACHE["ts"] < _CACHE_TTL):
            full = _CACHE["value"]
        else:
            t0 = time.time()
            full = _compute_all(eng)
            _CACHE["as_of"] = as_of
            _CACHE["ts"] = time.time()
            _CACHE["value"] = full
            print(f"[market-breadth] 全市场宽度统计完成 "
                  f"{time.time() - t0:.2f}s · as_of={as_of}")

    # 只输出「当日样本足够」的日期（与体检的轴同思路：碎日期不进序列）
    dates = sorted(d for d, row in full.items() if int(row[1] + row[0]) >= 100)
    if not dates:
        return {"ok": False, "error": "数据不足，无法计算市场宽度"}
    tail = dates[-days:]

    # 市况：全市场等权指数的 Chop(14) 序列（按数据版本缓存）。thin/早期无值
    # 的日期 chop=None，序列元素按日期查表补 chop 字段。
    try:
        _rg = market_regime_series(eng)
        _rg_map = {d: (c, t) for d, c, t in
                   zip(_rg["dates"], _rg["chop"], _rg["tag"])}
    except Exception:
        _rg_map = {}

    series: List[Dict[str, Any]] = []
    for d in tail:
        r = full[d]
        adv, dec, above20, denom20 = int(r[0]), int(r[1]), int(r[5]), int(r[6])
        rc = _rg_map.get(d, (None, None))
        series.append({
            "date": d,
            "adv": adv, "dec": dec, "flat": int(r[2]),
            "limup": int(r[3]), "limdn": int(r[4]),
            "above20_ratio": (round(above20 / denom20 * 100.0, 1)
                              if denom20 else None),
            "nh60": int(r[7]), "nl60": int(r[8]),
            "chop": rc[0],
        })

    # 最新读数 + 分位（分位基于本序列窗口，窗口默认 250 日 ≈ 一年）
    latest = dict(series[-1])
    adv_ratios = [s["adv"] / max(1, s["adv"] + s["dec"]) * 100.0
                  for s in series]
    above = [s["above20_ratio"] for s in series
             if s["above20_ratio"] is not None]
    cur_adv = (latest["adv"] / max(1, latest["adv"] + latest["dec"]) * 100.0)
    adv_pct = _pct_rank(adv_ratios, cur_adv)
    above_pct = (_pct_rank(above, latest["above20_ratio"])
                 if latest["above20_ratio"] is not None else None)

    extreme, note = None, ""
    # 极端读数以「站上 MA20 比例」为主判据：它是中期趋势的底盘，
    # 比单日涨跌家数稳得多
    if above_pct is not None and above_pct <= 5:
        extreme, note = "panic", (
            f"站上 20 日线的股票只占 {latest['above20_ratio']:.0f}%，"
            f"是一年里最恐慌的 {above_pct:.0f}% 分位——历史上这类极端常在"
            f"阶段底部附近，恐慌时割肉往往割在地板上")
    elif above_pct is not None and above_pct >= 95:
        extreme, note = "overheat", (
            f"站上 20 日线的股票占 {latest['above20_ratio']:.0f}%，"
            f"是一年里最过热的 {above_pct:.0f}% 分位——普涨之后注意节奏，"
            f"追高容易买在短期高点")
    elif adv_pct is not None and adv_pct >= 99:
        extreme, note = "overheat", (
            f"今日上涨家数占 {cur_adv:.0f}%，罕见的普涨日（分位 {adv_pct:.0f}%）")

    latest["adv_pct"] = adv_pct
    latest["above20_pct"] = above_pct
    latest["extreme"] = extreme
    latest["extreme_note"] = note

    # 市况读数（方案 D）：今日全市场 Chop 与标签
    _lt_chop, _lt_tag = _rg_map.get(latest["date"], (None, None))
    _regime_label = {
        "trend": "趋势市（适合趋势类策略）",
        "range": "震荡市（适合超跌反弹类）",
        "transition": "过渡市（趋势/震荡交界）",
    }.get(_lt_tag, "—")
    latest["chop"] = _lt_chop
    latest["regime"] = _lt_tag
    latest["regime_label"] = _regime_label

    return {"ok": True, "as_of": as_of, "days": len(series),
            "series": series, "latest": latest}


# ---------------------------------------------------------------------------
# 自检（合成数据，不依赖真实库）
# ---------------------------------------------------------------------------

def selfcheck() -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []

    def rec(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"name": name, "ok": bool(ok), "detail": detail})

    class _FakeEng:
        _as_of = "d1"
        _data = {
            # A：一路涨 d0→d2（10 → 11 → 12）→ adv×2、创 60 日新高（数据
            # 不足 60 根则不计）、站上 MA20（数据不足 20 根不计）
            "A": {
                "close": np.array([10.0, 11.0, 12.0], dtype=np.float32),
                "dates": ["d0", "d1", "d2"],
            },
            # B：一路跌 10 → 9 → 8
            "B": {
                "close": np.array([10.0, 9.0, 8.0], dtype=np.float32),
                "dates": ["d0", "d1", "d2"],
            },
            # C：平盘
            "C": {
                "close": np.array([10.0, 10.0, 10.0], dtype=np.float32),
                "dates": ["d0", "d1", "d2"],
            },
            # D：主板涨停（10 → 11.0 = ×1.1）
            "D": {
                "close": np.array([10.0, 11.0, 11.0], dtype=np.float32),
                "dates": ["d0", "d1", "d2"],
            },
        }

    r = _compute_all(_FakeEng())
    d2 = r["d2"]
    rec("d2 涨家数 = 1（A）", int(d2[0]) == 1,
        f"adv={int(d2[0])}")
    rec("d2 跌家数 = 1（B）", int(d2[1]) == 1, f"dec={int(d2[1])}")
    rec("d2 平家数 = 2（C、D）", int(d2[2]) == 2, f"flat={int(d2[2])}")
    d1 = r["d1"]
    rec("d1 近似涨停 = 2（A、D 均收 ×1.1）", int(d1[3]) == 2,
        f"limup={int(d1[3])}")

    # 跌停：主板 0.902
    class _FakeEng2:
        _as_of = "d1"
        _data = {
            "E": {"close": np.array([10.0, 9.0], dtype=np.float32),
                  "dates": ["d0", "d1"]},      # ×0.9 ≤ 0.902 → 跌停
        }

    r2 = _compute_all(_FakeEng2())
    rec("d1 近似跌停 = 1", int(r2["d1"][4]) == 1, f"limdn={int(r2['d1'][4])}")

    # MA20 / 新高新低：构造 25 根与 65 根的序列
    class _FakeEng3:
        _as_of = "t64"
        _data = {
            # F：25 根，前 10 根 10 元、后 15 根 12 元 → 第 20 根起站上 MA20
            "F": {"close": np.concatenate(
                      [np.full(10, 10.0), np.full(15, 12.0)])
                      .astype(np.float32),
                  "dates": [f"t{i:02d}" for i in range(25)]},
            # G：65 根一路阴跌 → 第 60 根起天天创 60 日新低
            "G": {"close": np.linspace(20.0, 10.0, 65).astype(np.float32),
                  "dates": [f"t{i:02d}" for i in range(65)]},
        }

    r3 = _compute_all(_FakeEng3())
    t24 = r3["t24"]     # F 的最后一根：12 元 vs MA20（=11.5）→ 上方
    # t24 当天：F（25 根）与 G（65 根）都够 20 根 → 分母 2；
    # G 一路阴跌，收盘在其 MA20 下方 → 只有 F 在上方
    rec("站上 MA20 计数（t24 单日）",
        int(t24[5]) == 1 and int(t24[6]) == 2,
        f"above={int(t24[5])}/{int(t24[6])}")
    t64 = r3["t64"]
    # t64 当天：只有 G 够 60 根 → 分母 1；G 阴跌创新低
    rec("创 60 日新低计数（t64 单日）",
        int(t64[8]) == 1 and int(t64[9]) == 1,
        f"nl={int(t64[8])}/{int(t64[9])}")
    rec("G 不创新高", int(t64[7]) == 0, f"nh={int(t64[7])}")

    # 分位函数
    rec("百分位：最低值 = 0", _pct_rank([1.0, 2.0, 3.0], 0.5) == 0.0)
    rec("百分位：最高值 = 100", _pct_rank([1.0, 2.0, 3.0], 99.0) == 100.0)
    rec("百分位：中位 = 2/3",
        abs((_pct_rank([1.0, 2.0, 3.0], 2.0) or 0) - 66.7) < 0.1)

    # ---- 市况开关（方案 D）：合成指数的 Chop 标签 ----
    def _mk_eng(close_seqs):
        class _E:
            _as_of = "rg"
            _data = {}
        e = _E()
        for i, c in enumerate(close_seqs):
            e._data[f"S{i}"] = {
                "close": np.asarray(c, dtype=np.float32),
                "high": np.asarray(c, dtype=np.float32) + 0.3,
                "low": np.asarray(c, dtype=np.float32) - 0.3,
                "dates": [f"d{j:02d}" for j in range(len(c))],
            }
        return e

    # 平滑单边上涨（30 天）→ 等权指数平滑趋势 → Chop 低 → trend
    up = [float(100 + i) for i in range(30)]
    rg_up = market_regime_series(_mk_eng([up] * 3), use_cache=False)
    rec("趋势市标签=trend", rg_up["tag"][-1] == "trend",
        f"末tag={rg_up['tag'][-1]}, chop={rg_up['chop'][-1]}")

    # 锯齿震荡（45↔55）→ 等权指数来回摆、净位移≈0 → Chop 高 → range
    zz = [float(50 + (5.0 if i % 2 else -5.0)) for i in range(30)]
    rg_z = market_regime_series(_mk_eng([zz] * 3), use_cache=False)
    rec("震荡市标签=range", rg_z["tag"][-1] == "range",
        f"末tag={rg_z['tag'][-1]}, chop={rg_z['chop'][-1]}")

    # 阈值序：趋势 chop < 震荡 chop
    cu = [v for v in rg_up["chop"] if v is not None]
    cz = [v for v in rg_z["chop"] if v is not None]
    rec("Chop 趋势<震荡（定性）", cu and cz and max(cu) < min(cz),
        f"趋势max={max(cu):.1f} < 震荡min={min(cz):.1f}")

    # 前 14 天为预热占位（None）
    rec("Chop 前 14 天占位 None", all(v is None for v in rg_up["chop"][:14]))

    passed = sum(1 for s in steps if s["ok"])
    return {
        "steps": steps,
        "total": len(steps),
        "passed": passed,
        "failed": len(steps) - passed,
        "all_passed": passed == len(steps),
    }


if __name__ == "__main__":
    r = selfcheck()
    for s in r["steps"]:
        print(f"  {'✅' if s['ok'] else '❌'} {s['name']}  {s['detail']}")
    print()
    print(f"{'✅ 全部通过' if r['all_passed'] else '❌ 有失败'}  "
          f"{r['passed']}/{r['total']}")
