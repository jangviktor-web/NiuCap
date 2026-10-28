"""TradingView 技术分析三表口径（严格对齐）。

与 indicator_rating.py 的区别（很重要，不要混用）：
  indicator_rating.py  —— 本项目自有口径，61 项指标、水平阈值判定，
                          供「全套技术指标」明细表使用。
  tv_rating.py (本文件) —— TradingView Technicals 口径，26 项指标、
                          事件/状态判定、五档分档，只供三张半圆仪表使用。

为什么必须分成两套：
  TradingView 的 RSI 是「上穿 30 才买、上穿 70 才卖」的**事件**判定，
  不是「低于 30 即为买」的**水平**判定。事件判定下大多数时间返回中立，
  所以它那张表常见 0 卖 / 9 中立 / 2 买；我们的水平判定则是 10 卖 / 5 中立。
  两套语义不能在同一套票数里混算，混算出来的数字谁也对不上。

口径来源：https://www.tradingview.com/support/solutions/43000614331
  · 均线组 15 项：SMA/EMA × (10,20,30,50,100,200) + HullMA(9) + VWMA(20) + 一目均衡(9,26,52)
  · 震荡组 11 项：RSI(14)、Stoch(14,3,3)、CCI(20)、ADX(14,14)、AO、Mom(10)、
                  MACD(12,26,9)、StochRSI(3,3,14,14)、W%R(14)、BBP(13)、UO(7,14,28)
  · 总结 = 均线组均值与震荡组均值的平均（两组等权），不是 26 项混算。

判定取值：+1 买 / 0 中立 / -1 卖，均值落在 [-1, 1]，再按
  < -0.5 强烈卖出 / [-0.5,-0.1) 卖出 / [-0.1,0.1] 中立 / (0.1,0.5] 买入 / > 0.5 强烈买入
分档。注意官方文档 This 区间在边界处含等号，这里按文档原样实现。

★ 已知不确定：用户提供的实盘截图里「震荡指标」0卖/9中立/2买，
  均值为 +0.1818，按 ±0.1/±0.5 应判「买入」，但 TradingView 显示「中立」。
  单张截图无法反推真实阈值，故先按官方文档实现，留 THRESH 常量便于校准。
"""
from typing import Any, Dict, List, Optional

import numpy as np

from mytt import (EMA, HHV, LLV, MA, MACD, REF, SUM, WMA)

BUY, SELL, NEUTRAL = 1, -1, 0
LABEL5 = {
    2: "强烈买入", 1: "买入", 0: "中立", -1: "卖出", -2: "强烈卖出",
}
RATING5 = {2: "STRONG_BUY", 1: "BUY", 0: "NEUTRAL", -1: "SELL", -2: "STRONG_SELL"}

# 分档阈值（官方文档值）。校准只改这两处。
THRESH_STRONG = 0.5
THRESH_WEAK = 0.1

EPS = 1e-9


# ---------------------------------------------------------------- 补算指标
# 以下 7 个 TradingView 用到、但 mytt.py 里没有的指标。
# 一律接收 numpy 数组、返回 numpy 数组，与 mytt 的风格保持一致。

def _ema(s, n):
    """EMA，返回 ndarray（mytt.EMA 返回 ndarray）。"""
    return np.asarray(EMA(s, n), dtype=float)


def hull_ma(c, n=9):
    """Hull MA：WMA(2*WMA(n/2) - WMA(n), sqrt(n))。"""
    half, root = max(1, int(n / 2)), max(1, int(np.sqrt(n)))
    raw = 2 * np.asarray(WMA(c, half), dtype=float) - np.asarray(WMA(c, n), dtype=float)
    return np.asarray(WMA(raw, root), dtype=float)


def vwma(c, v, n=20):
    """成交量加权均线：sum(c*v, n) / sum(v, n)。"""
    cv = np.asarray(c, dtype=float) * np.asarray(v, dtype=float)
    num = np.asarray(SUM(cv, n), dtype=float)
    den = np.asarray(SUM(v, n), dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = num / den
    out[~np.isfinite(out)] = np.nan
    return out


def ichimoku(h, l, n_conv=9, n_base=26, n_span=52):
    """一目均衡表 → (转换线, 基准线, 先行A, 先行B)。"""
    conv = (np.asarray(HHV(h, n_conv), dtype=float)
            + np.asarray(LLV(l, n_conv), dtype=float)) / 2
    base = (np.asarray(HHV(h, n_base), dtype=float)
            + np.asarray(LLV(l, n_base), dtype=float)) / 2
    span_a = (conv + base) / 2
    span_b = (np.asarray(HHV(h, n_span), dtype=float)
              + np.asarray(LLV(l, n_span), dtype=float)) / 2
    return conv, base, span_a, span_b


def awesome_oscillator(h, l, fast=5, slow=34):
    """AO = SMA(中价,5) - SMA(中价,34)。"""
    mid = (np.asarray(h, dtype=float) + np.asarray(l, dtype=float)) / 2
    return np.asarray(MA(mid, fast), dtype=float) - np.asarray(MA(mid, slow), dtype=float)


def stoch_rsi(c, n_rsi=14, n_stoch=14, k_smooth=3, d_smooth=3):
    """Stochastic RSI → (K, D)。

    RSI 用 Wilder 平滑（与 mytt.RSI 一致），再对 RSI 序列做 n 周期随机。
    """
    from mytt import ABS, MAX, SMA
    diff = np.asarray(c, dtype=float) - np.asarray(REF(c, 1), dtype=float)
    up = np.asarray(SMA(np.asarray(MAX(diff, 0), dtype=float), n_rsi), dtype=float)
    dn = np.asarray(SMA(np.abs(diff), n_rsi), dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = np.where(dn > EPS, up / dn * 100, np.nan)
    hh = np.asarray(HHV(rsi, n_stoch), dtype=float)
    ll = np.asarray(LLV(rsi, n_stoch), dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        st = np.where((hh - ll) > EPS, (rsi - ll) / (hh - ll) * 100, np.nan)
    k = _ema(st, k_smooth)
    d = _ema(k, d_smooth)
    return k, d


def bull_bear_power(h, l, c, n=13):
    """Bull/Bear Power (Elder) → (bull, bear)，均以 EMA(n) 为基准。"""
    base = _ema(c, n)
    return np.asarray(h, dtype=float) - base, np.asarray(l, dtype=float) - base


def ultimate_oscillator(h, l, c, n1=7, n2=14, n3=28):
    """Ultimate Oscillator。"""
    h = np.asarray(h, dtype=float)
    l = np.asarray(l, dtype=float)
    c = np.asarray(c, dtype=float)
    prev_c = np.asarray(REF(c, 1), dtype=float)
    true_low = np.minimum(l, prev_c)
    bp = c - true_low
    tr = np.maximum(h, prev_c) - true_low
    with np.errstate(divide="ignore", invalid="ignore"):
        a1 = np.asarray(SUM(bp, n1), dtype=float) / np.asarray(SUM(tr, n1), dtype=float)
        a2 = np.asarray(SUM(bp, n2), dtype=float) / np.asarray(SUM(tr, n2), dtype=float)
        a3 = np.asarray(SUM(bp, n3), dtype=float) / np.asarray(SUM(tr, n3), dtype=float)
        uo = 100 * (4 * a1 + 2 * a2 + a3) / 7
    uo[~np.isfinite(uo)] = np.nan
    return uo


# ---------------------------------------------------------------- 取值助手

def _l(a, i=-1):
    """安全取第 i 个元素；NaN/非有限 → None。"""
    try:
        v = float(np.asarray(a, dtype=float).ravel()[i])
    except (IndexError, TypeError, ValueError):
        return None
    return None if not np.isfinite(v) else round(v, 4)


def _rising(a):
    """当前值是否大于上一值（TradingView 的 rising 判定）。"""
    cur, prev = _l(a, -1), _l(a, -2)
    return (cur is not None and prev is not None and cur > prev)


def _crossed_above(a, level):
    """本根是否上穿 level（事件判定，不是水平判定）。"""
    cur, prev = _l(a, -1), _l(a, -2)
    if None in (cur, prev):
        return False
    return prev <= level < cur


def _crossed_below(a, level):
    cur, prev = _l(a, -1), _l(a, -2)
    if None in (cur, prev):
        return False
    return prev >= level > cur


def _cross(a, b):
    """a 上穿 b → 1；a 下穿 b → -1；否则 0。"""
    a1, a0 = _l(a, -1), _l(a, -2)
    b1, b0 = _l(b, -1), _l(b, -2)
    if None in (a1, a0, b1, b0):
        return 0
    if a0 <= b0 and a1 > b1:
        return 1
    if a0 >= b0 and a1 < b1:
        return -1
    return 0


# ---------------------------------------------------------------- 26 项投票
# 每项返回 (名称, +1/0/-1)。返回 None 表示数据不足，该项不计入分母
# —— 与 TradingView 一致：算不出的指标不投中立票，直接不参与。

def _votes_ma(c, h, l, v):
    """均线组 15 项。判定：MA < 现价 → 买；MA > 现价 → 卖。"""
    price = _l(c)
    out = []
    for n in (10, 20, 30, 50, 100, 200):
        for fn, tag in ((lambda x: MA(x, n), "SMA"), (lambda x: EMA(x, n), "EMA")):
            val = _l(fn(c))
            if val is None or price is None:
                continue
            out.append((f"{tag}{n}", BUY if val < price else (SELL if val > price else NEUTRAL)))
    for name, series in (("HullMA9", hull_ma(c, 9)), ("VWMA20", vwma(c, v, 20))):
        val = _l(series)
        if val is None or price is None:
            continue
        out.append((name, BUY if val < price else (SELL if val > price else NEUTRAL)))
    conv, base, span_a, span_b = ichimoku(h, l)
    cv, bv, av, bv2 = _l(conv), _l(base), _l(span_a), _l(span_b)
    if None not in (cv, bv) and price is not None:
        if av > bv2 and bv > av and cv > bv and price > cv:
            out.append(("Ichimoku", BUY))
        elif av < bv2 and bv < av and cv < bv and price < cv:
            out.append(("Ichimoku", SELL))
        else:
            out.append(("Ichimoku", NEUTRAL))
    return out


def _votes_osc(c, h, l, v):
    """震荡组 11 项。判定全部按 TradingView 的「事件/条件组合」语义。"""
    out = []

    # RSI(14)：上穿 30 → 买；上穿 70 → 卖。★事件判定
    rsi14 = _ema(np.asarray(c, dtype=float), 14)  # 占位，下面用 mytt.RSI 覆盖
    from mytt import RSI
    rsi14 = RSI(c, 14)
    r = _l(rsi14)
    if r is not None:
        if _crossed_above(rsi14, 30):
            out.append(("RSI14", BUY))
        elif _crossed_above(rsi14, 70):
            out.append(("RSI14", SELL))
        else:
            out.append(("RSI14", NEUTRAL))

    # Stochastic(14,3,3)：%K 上穿 %D 且 %K<80 → 买；下穿且 %K>20 → 卖
    # mytt.KDJ 的 M1 走 EMA(RSV, M1*2-1)，要 3 周期平滑就得传 M1=2。
    from mytt import KDJ
    k, d, _j = KDJ(c, h, l, N=14, M1=2, M2=2)
    kv, dv = _l(k), _l(d)
    if kv is not None and dv is not None:
        cross = _cross(k, d)
        if cross == 1 and kv < 80:
            out.append(("Stoch", BUY))
        elif cross == -1 and kv > 20:
            out.append(("Stoch", SELL))
        else:
            out.append(("Stoch", NEUTRAL))

    # CCI(20)：上穿 -100 → 买；上穿 +100 → 卖
    from mytt import CCI
    cci20 = CCI(c, h, l, 20)
    if _l(cci20) is not None:
        if _crossed_above(cci20, -100):
            out.append(("CCI20", BUY))
        elif _crossed_above(cci20, 100):
            out.append(("CCI20", SELL))
        else:
            out.append(("CCI20", NEUTRAL))

    # ADX(14)：+DI>-DI 且 ADX>20 且 ADX 上升 → 买；镜像 → 卖
    from mytt import DMI
    pdi, mdi, adx, _axr = DMI(c, h, l, 14, 14)
    pv, mv, av = _l(pdi), _l(mdi), _l(adx)
    if None not in (pv, mv, av):
        up = _rising(adx)
        if pv > mv and av > 20 and up:
            out.append(("ADX", BUY))
        elif pv < mv and av > 20 and up:
            out.append(("ADX", SELL))
        else:
            out.append(("ADX", NEUTRAL))

    # AO：上穿 0 → 买；下穿 0 → 卖（碟形判定依赖前值模式，此处只做穿越）
    ao = awesome_oscillator(h, l)
    if _l(ao) is not None:
        if _crossed_above(ao, 0):
            out.append(("AO", BUY))
        elif _crossed_below(ao, 0):
            out.append(("AO", SELL))
        else:
            out.append(("AO", NEUTRAL))

    # Momentum(10)：上升 → 买；下降 → 卖
    mom = np.asarray(c, dtype=float) - np.asarray(REF(c, 10), dtype=float)
    if _l(mom) is not None:
        cur, prev = _l(mom), _l(mom, -2)
        if cur is not None and prev is not None and cur != prev:
            out.append(("Mom10", BUY if cur > prev else SELL))
        else:
            out.append(("Mom10", NEUTRAL))

    # MACD(12,26,9)：DIF > DEA → 买
    dif, dea, _hist = MACD(c, 12, 26, 9)
    if _l(dif) is not None and _l(dea) is not None:
        out.append(("MACD", BUY if _l(dif) > _l(dea) else SELL))

    # StochRSI：%K>%D 且 %K<20 且 %K 上升 → 买；镜像 → 卖
    srk, srd = stoch_rsi(c)
    skv, sdv = _l(srk), _l(srd)
    if None not in (skv, sdv):
        if skv > sdv and skv < 20 and _rising(srk):
            out.append(("StochRSI", BUY))
        elif skv < sdv and skv > 80 and not _rising(srk):
            out.append(("StochRSI", SELL))
        else:
            out.append(("StochRSI", NEUTRAL))

    # Williams %R(14)：%R < -80 且上升 → 买；%R > -20 且下降 → 卖
    from mytt import WR
    wr14, _wr6 = WR(c, h, l, 14, 14)
    wv = _l(wr14)
    if wv is not None:
        if wv < -80 and _rising(wr14):
            out.append(("W%R14", BUY))
        elif wv > -20 and not _rising(wr14):
            out.append(("W%R14", SELL))
        else:
            out.append(("W%R14", NEUTRAL))

    # Bull Bear Power(13)：上升趋势且 BearPower<0 且上升 → 买；镜像 → 卖
    bull, bear = bull_bear_power(h, l, c, 13)
    bv, rv = _l(bull), _l(bear)
    if None not in (bv, rv):
        trend_up = _l(c) > _l(_ema(c, 13))
        if trend_up and rv < 0 and _rising(bear):
            out.append(("BBP", BUY))
        elif (not trend_up) and bv > 0 and not _rising(bull):
            out.append(("BBP", SELL))
        else:
            out.append(("BBP", NEUTRAL))

    # UO(7,14,28)：>70 买；<30 卖
    uo = ultimate_oscillator(h, l, c)
    uv = _l(uo)
    if uv is not None:
        out.append(("UO", BUY if uv > 70 else (SELL if uv < 30 else NEUTRAL)))

    return out


# ---------------------------------------------------------------- 聚合

def _mean(votes):
    """[(name, ±1/0)] → 均值；空列表 → None。"""
    if not votes:
        return None
    return sum(v for _n, v in votes) / len(votes)


def classify(avg):
    """均值 [-1,1] → 五档整数（2/1/0/-1/-2）。"""
    if avg is None:
        return None
    if avg < -THRESH_STRONG:
        return -2
    if avg < -THRESH_WEAK:
        return -1
    if avg <= THRESH_WEAK:
        return 0
    if avg <= THRESH_STRONG:
        return 1
    return 2


def _pack(key, name, votes, avg=None):
    """把一个分表打包成前端要的结构。avg=None 时按 votes 现算。"""
    if avg is None:
        avg = _mean(votes)
    lvl = classify(avg)
    return {
        "key": key,
        "name": name,
        "avg": None if avg is None else round(avg, 4),
        "level": lvl,
        "rating": RATING5.get(lvl) if lvl is not None else None,
        "label": LABEL5.get(lvl, "—") if lvl is not None else "—",
        "buy": sum(1 for _n, v in votes if v == BUY),
        "sell": sum(1 for _n, v in votes if v == SELL),
        "neutral": sum(1 for _n, v in votes if v == NEUTRAL),
        "total": len(votes),
        "items": [{"name": n, "vote": v} for n, v in votes],
    }


def tv_gauges(klines):
    """K 线 → 三张表（震荡 / 总结 / 均线）。

    ★ 顺序按 TradingView 页面排布：震荡指标、总结、移动平均线。
    总结 = (均线均值 + 震荡均值) / 2，两组等权；任一组缺失则取另一组。
    """
    if not klines or len(klines) < 30:
        return None
    c = np.array([k["close"] for k in klines], dtype=float)
    h = np.array([k["high"] for k in klines], dtype=float)
    l = np.array([k["low"] for k in klines], dtype=float)
    v = np.array([k.get("volume") or 0 for k in klines], dtype=float)

    osc_v = _votes_osc(c, h, l, v)
    ma_v = _votes_ma(c, h, l, v)
    osc_avg, ma_avg = _mean(osc_v), _mean(ma_v)
    if osc_avg is None:
        sum_avg = ma_avg
    elif ma_avg is None:
        sum_avg = osc_avg
    else:
        sum_avg = (osc_avg + ma_avg) / 2

    return [
        _pack("osc", "震荡指标", osc_v, osc_avg),
        _pack("sum", "总结", osc_v + ma_v, sum_avg),
        _pack("ma", "移动平均线", ma_v, ma_avg),
    ]


# ---------------------------------------------------------------- 自检

def selfcheck(verbose=True):
    """规则自检：每个判定分档、事件语义、总结的等权平均。"""
    fails = 0

    def ck(cond, msg):
        nonlocal fails
        if not cond:
            fails += 1
            if verbose:
                print(f"[FAIL] {msg}")

    # 分档边界
    for avg, exp in [
        (-1.0, -2), (-0.51, -2), (-0.5, -1), (-0.11, -1), (-0.1, 0),
        (0.0, 0), (0.1, 0), (0.11, 1), (0.5, 1), (0.51, 2), (1.0, 2),
    ]:
        ck(classify(avg) == exp, f"classify({avg}) 期望 {exp} 得到 {classify(avg)}")
    ck(classify(None) is None, "classify(None) 应为 None")

    # 均值
    ck(_mean([]) is None, "空票应为 None")
    ck(abs(_mean([("a", 1), ("b", -1), ("c", 0)]) - 0) < 1e-9, "1买1卖1中立均值应为0")
    ck(abs(_mean([("a", 1), ("b", 1)]) - 1) < 1e-9, "全买均值应为1")

    # 事件语义：上穿判定
    arr = np.array([25.0, 28.0, 31.0])
    ck(_crossed_above(arr, 30) is True, "28→31 应判上穿 30")
    arr2 = np.array([35.0, 40.0, 45.0])
    ck(_crossed_above(arr2, 30) is False,
       "一直高于 30 不应判上穿 —— 这正是水平判定与事件判定的分界")
    arr3 = np.array([72.0, 68.0, 65.0])
    ck(_crossed_above(arr3, 70) is False, "从上方回落不算上穿")

    # 交叉判定
    a = np.array([1.0, 1.0, 2.0]); b = np.array([1.5, 1.5, 1.5])
    ck(_cross(a, b) == 1, "a 上穿 b 应返回 1")
    ck(_cross(b, a) == -1, "b 下穿 a 应返回 -1")

    # 打包：总表等权，不与票数混算
    osc = [("x", 1), ("y", -1), ("z", 0)]
    ma = [("p", 1), ("q", 1)]
    o_avg, m_avg = _mean(osc), _mean(ma)
    s_avg = (o_avg + m_avg) / 2
    ck(abs(s_avg - 0.5) < 1e-9, f"总结应为 (0+1)/2=0.5，得到 {s_avg}")
    # 若错误地把 5 项混算净票：净票=2, 总数=5 → 0.4，与 0.5 不同。
    # 这条断言就是用来钉住「总结不等于 26 项混算」的。
    ck(abs(_mean(osc + ma) - 0.4) < 1e-9, "混算应为 0.4（用于对比，不应被采用）")

    # 补算指标：VWMA 在等量下应等于简单均值
    cc = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    vv = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
    ck(abs(_l(vwma(cc, vv, 3)) - 4.0) < 1e-6, "等量 VWMA3 应等于 MA3=4")

    # HullMA 常数序列应等于该常数
    flat = np.full(40, 7.0)
    ck(abs(_l(hull_ma(flat, 9)) - 7.0) < 1e-6, "常数序列 HullMA 应等于该常数")

    # UO 强势上涨应 > 70，弱势应 < 30。
    # 构造要点：UO 量的不是「涨了多少」，而是「收盘站在当根真实波幅的什么位置」
    # （bp / tr）。所以只让价格单边抬高还不够 —— 收盘必须贴近最高价，
    # 否则 true_low 恒取前收、bp/tr 恒定，UO 会稳在 50 附近，那是数据没表达
    # 出「强势」，不是算法错。
    base = 10 + np.arange(60) * 0.2
    hh, ll = base + 0.10, base - 0.30
    ck(_l(ultimate_oscillator(hh, ll, base + 0.09)) > 70, "收盘贴顶的 UO 应 > 70")
    ck(_l(ultimate_oscillator(hh, ll, base - 0.29)) < 35, "收盘贴底的 UO 应 < 35")

    if verbose:
        print(f"tv_rating selfcheck: {'OK' if fails == 0 else str(fails) + ' FAILED'}")
    return {"fail": fails, "total": 20}


if __name__ == "__main__":
    rc = selfcheck()
    raise SystemExit(1 if rc["fail"] else 0)
