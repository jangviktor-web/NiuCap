"""经典技术指标序列版（方案 A · 从 cinar/indicator 蒸馏）。

## 为什么单独成模块

`screener.py` 的策略需要 ATR / SuperTrend / ConnorsRSI / TD9 / MFI / CMF
这类**经典指标**，而 `history.py` 的 `metrics()` 只算均线 / 量比 / 新高
这类基础量。**不往 history.py 塞**（它是稳定文件，且这些指标在体检的
`_TruncatedEngine` 上下文里要被反复调用），这里用纯 numpy 实现，
供 screener 的新策略按需取用。

## 设计要点（与项目既有约定一致）

1. **序列版 + 末值版并存**。
   - 序列版（`*_series`）返回与输入同长的 np.ndarray（含 NaN 预热），
     主要用于人工核对 / 指标面板 / selfcheck。
   - 末值版（`*_last`）只算当前（最后一根）那一个值，并把输入截到
     只够用的尾巴，复杂度压到 O(窗口)，供策略在「全市场 × 120 天」的
     体检循环里廉价调用（否则 ConnorsRSI 的 PercentRank 会爆成 O(n×窗口)）。

2. **数据源唯一**：所有函数只吃 OHLCV 四个序列 + volume，与 daily_bars
   完全对齐——这正是「策略可回测」的前提（daily_bars 没有 turnover/
   total_cap/name，这些指标都不依赖它们）。

3. **数据不足一律返回 None / 全 NaN**，调用方显式判断，绝不拿 0 冒充有效值。

4. **公式对照 cinar/indicator v2.1.44 源码核对**：
   - ATR：volatility/atr.go（Wilder 平滑）
   - SuperTrend：volatility/super_trend.go（Kivanc 通道翻转）
   - ConnorsRSI：momentum/connors_rsi.go（RSI(3)+RSI(2 of streak)+Rank100(ROC1)）
   - TD Sequential：td_sequential.go（连续 9 根 close < 4 日前 close）
   - MFI：volume/mfi.go（量权 RSI）
   - CMF：volume/cmf.go（Chaikin 资金流）

## 用法

    import indicators_extra as ix
    atr = ix.atr_last(high, low, close, 14)        # 当日 ATR
    crsi = ix.connors_rsi_last(close, 100)          # 当日 ConnorsRSI
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

import numpy as np


# ===========================================================================
# 基础件：True Range / ATR（Wilder）
# ===========================================================================

def true_range(high, low, close) -> np.ndarray:
    """真实波幅序列。TR = max(H−L, |H−prevC|, |L−prevC|)。

    TR[0] 没有前收，退化为 H[0]−L[0]（不影响后续 ATR 计算，因为 ATR 首值
    用 TR[1:period+1]）。
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    prev_close = np.empty_like(close)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    return np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])


def atr_series(high, low, close, period: int = 14) -> np.ndarray:
    """ATR 序列（Wilder 平滑）。首值落在 index=period，之前为 NaN。"""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    atr = np.full(n, np.nan)
    if n < period + 1:
        return atr
    tr = true_range(high, low, close)
    # 首值：前 period 个有效 TR（TR[1..period]）的均值
    atr[period] = float(np.mean(tr[1:period + 1]))
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def atr_last(high, low, close, period: int = 14) -> Optional[float]:
    """当日 ATR。输入截到末 period+1 根，O(period)。"""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    if len(close) < period + 1:
        return None
    arr = atr_series(high[-(period + 1):], low[-(period + 1):],
                     close[-(period + 1):], period)
    v = arr[-1]
    return float(v) if not math.isnan(v) else None


# ===========================================================================
# RSI（Wilder）—— ConnorsRSI 与 MFI 都会用到
# ===========================================================================

def rsi_series(close, period: int = 14) -> np.ndarray:
    """RSI 序列（Wilder）。首值落在 index=period。"""
    close = np.asarray(close, dtype=float)
    n = len(close)
    rsi = np.full(n, np.nan)
    if n < period + 1:
        return rsi
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag = float(np.mean(gain[:period]))
    al = float(np.mean(loss[:period]))
    rsi[period] = (100.0 - 100.0 / (1.0 + ag / al)) if al > 0 else 100.0
    for i in range(period, len(delta)):
        ag = (ag * (period - 1) + gain[i]) / period
        al = (al * (period - 1) + loss[i]) / period
        rsi[i + 1] = (100.0 - 100.0 / (1.0 + ag / al)) if al > 0 else 100.0
    return rsi


def rsi_last(close, period: int = 14) -> Optional[float]:
    """当日 RSI。截到末 period+1 根。"""
    close = np.asarray(close, dtype=float)
    if len(close) < period + 1:
        return None
    arr = rsi_series(close[-(period + 1):], period)
    v = arr[-1]
    return float(v) if not math.isnan(v) else None


# ===========================================================================
# SuperTrend（Kivanc 通道翻转）
# ===========================================================================

def supertrend_series(high, low, close, period: int = 10, mult: float = 3.0
                      ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (trend, final_up, final_dn)。

    trend：+1 多头 / −1 空头 / 0 未定（预热期）。
    中线 = (H+L)/2；上轨 = 中线 + mult×ATR；下轨 = 中线 − mult×ATR。
    趋势方向在「收盘价上穿上轨 → 多；下穿下轨 → 空」时翻转，否则维持。
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    atr = atr_series(high, low, close, period)
    hl2 = (high + low) / 2.0
    up = hl2 + mult * atr
    dn = hl2 - mult * atr
    trend = np.zeros(n)
    final_up = np.full(n, np.nan)
    final_dn = np.full(n, np.nan)
    for i in range(period, n):
        if i == period:
            final_up[i] = up[i]
            final_dn[i] = dn[i]
            trend[i] = 1.0 if close[i] > final_up[i] else -1.0
            continue
        # 上轨：若前收 <= 前上轨，则取 min(当上轨, 前上轨)（只降不升，锁定支撑）
        if close[i - 1] <= final_up[i - 1]:
            final_up[i] = min(up[i], final_up[i - 1])
        else:
            final_up[i] = up[i]
        if close[i - 1] >= final_dn[i - 1]:
            final_dn[i] = max(dn[i], final_dn[i - 1])
        else:
            final_dn[i] = dn[i]
        if close[i] > final_up[i]:
            trend[i] = 1.0
        elif close[i] < final_dn[i]:
            trend[i] = -1.0
        else:
            trend[i] = trend[i - 1]
    return trend, final_up, final_dn


def supertrend_last(high, low, close, period: int = 10, mult: float = 3.0
                    ) -> Optional[int]:
    """当日趋势方向（+1 多 / −1 空 / None 不足）。

    截到末 40 根：SuperTrend 的趋势态在 ~period 根内即可稳定，
    翻转判定只看最近 2 根，尾巴足够。
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    tail = max(period * 3, 30)
    if len(close) < tail:
        return None
    t, _, _ = supertrend_series(high[-tail:], low[-tail:], close[-tail:],
                                period, mult)
    v = t[-1]
    return int(v) if v in (1.0, -1.0) else None


def supertrend_flip_last(high, low, close, period: int = 10,
                         mult: float = 3.0) -> Optional[bool]:
    """当日是否「由空翻多」（趋势状态从 −1 变为 +1 的那一根）。

    这是 `s_supertrend_long` 的直接信号：SuperTrend 的经典买点是翻多当日，
    而不是单纯「处于多头」。返回 True 仅在那一根；None 表示数据不足。
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    tail = max(period * 3, 30)
    if len(close) < tail:
        return None
    t, _, _ = supertrend_series(high[-tail:], low[-tail:], close[-tail:],
                                period, mult)
    if t[-1] != 1.0:
        return False
    if len(t) >= 2 and t[-2] == -1.0:
        return True
    return False


# ===========================================================================
# ConnorsRSI
# ===========================================================================

def _streak_series(close) -> np.ndarray:
    """连涨 / 连跌天数序列（ConnorsRSI 的第二步）。

    连续上涨第 k 天记 +k，连续下跌第 k 天记 −k，平盘记 0。
    """
    close = np.asarray(close, dtype=float)
    n = len(close)
    streak = np.zeros(n)
    for i in range(1, n):
        d = close[i] - close[i - 1]
        if d > 0:
            streak[i] = streak[i - 1] + 1 if streak[i - 1] >= 0 else 1
        elif d < 0:
            streak[i] = streak[i - 1] - 1 if streak[i - 1] <= 0 else -1
        else:
            streak[i] = 0
    return streak


def connors_rsi_series(close, lookback: int = 100, rsi1: int = 3,
                       rsi2: int = 2) -> np.ndarray:
    """ConnorsRSI 序列 = mean(RSI(close,3), RSI(streak,2), Rank100(ROC1))。

    仅用于人工核对 / 面板 / selfcheck（O(n×lookback) 偏重，策略请用
    connors_rsi_last）。
    """
    close = np.asarray(close, dtype=float)
    n = len(close)
    r1 = rsi_series(close, rsi1)
    streak = _streak_series(close)
    r2 = rsi_series(streak, rsi2)
    roc = np.concatenate([[0.0], np.diff(close)])
    pr = np.full(n, np.nan)
    for i in range(1, n):
        win = roc[max(0, i - lookback):i]
        if len(win) >= 1:
            pr[i] = (float(np.sum(win <= roc[i])) / len(win)) * 100.0
    return (r1 + r2 + pr) / 3.0


def connors_rsi_last(close, lookback: int = 100, rsi1: int = 3,
                     rsi2: int = 2) -> Optional[float]:
    """当日 ConnorsRSI。截到末 (lookback+rsi1+2) 根，O(lookback)。"""
    close = np.asarray(close, dtype=float)
    n = len(close)
    need = lookback + rsi1 + 2
    if n < need:
        return None
    c = close[-need:]
    r1 = rsi_last(c, rsi1)
    if r1 is None:
        return None
    streak = _streak_series(c)
    r2 = rsi_last(streak, rsi2)
    if r2 is None:
        return None
    roc = np.concatenate([[0.0], np.diff(c)])
    cur = roc[-1]
    win = roc[-(lookback + 1):-1]        # 至多 lookback 个历史 ROC
    if len(win) == 0:
        return None
    pr = (float(np.sum(win <= cur)) / len(win)) * 100.0
    return (r1 + r2 + pr) / 3.0


# ===========================================================================
# TD Sequential（TD9 抄底）
# ===========================================================================

def td_count_series(close) -> np.ndarray:
    """TD 计数序列：连续「当日收盘 < 4 日前收盘」的天数（断则归零）。

    计满 9 即经典「TD9 买入 setup 完成」。
    """
    close = np.asarray(close, dtype=float)
    n = len(close)
    cnt = np.zeros(n, dtype=int)
    for i in range(4, n):
        if close[i] < close[i - 4]:
            cnt[i] = cnt[i - 1] + 1 if i > 4 else 1
        else:
            cnt[i] = 0
    return cnt


def td_count_last(close) -> Optional[int]:
    """当日 TD 计数（末值）。"""
    close = np.asarray(close, dtype=float)
    if len(close) < 5:
        return None
    return int(td_count_series(close)[-1])


# ===========================================================================
# MFI（量权 RSI）
# ===========================================================================

def mfi_series(high, low, close, volume, period: int = 14) -> np.ndarray:
    """MFI 序列（Money Flow Index）。首值落在 index=period。"""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)
    n = len(close)
    mfi = np.full(n, np.nan)
    if n < period + 1:
        return mfi
    tp = (high + low + close) / 3.0
    mf = tp * volume
    pos = np.zeros(n)
    neg = np.zeros(n)
    for i in range(1, n):
        if tp[i] > tp[i - 1]:
            pos[i] = mf[i]
        elif tp[i] < tp[i - 1]:
            neg[i] = mf[i]
    for i in range(period, n):
        ps = float(np.sum(pos[i - period + 1:i + 1]))
        ns = float(np.sum(neg[i - period + 1:i + 1]))
        mfi[i] = (100.0 - 100.0 / (1.0 + ps / ns)) if ns > 0 else 100.0
    return mfi


def mfi_last(high, low, close, volume, period: int = 14) -> Optional[float]:
    """当日 MFI。截到末 period+1 根。"""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)
    if len(close) < period + 1:
        return None
    sl = slice(-(period + 1), None)
    arr = mfi_series(high[sl], low[sl], close[sl], volume[sl], period)
    v = arr[-1]
    return float(v) if not math.isnan(v) else None


# ===========================================================================
# CMF（Chaikin Money Flow）
# ===========================================================================

def cmf_series(high, low, close, volume, period: int = 20) -> np.ndarray:
    """CMF 序列（Chaikin Money Flow）。首值落在 index=period-1。

    CLV = ((C−L)−(H−C))/(H−L) = (2C−H−L)/(H−L)，范围 [−1, 1]。
    CMF = Σ(CLV×Vol, period) / Σ(Vol, period)。
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)
    n = len(close)
    cmf = np.full(n, np.nan)
    if n < period:
        return cmf
    hl = high - low
    clv = np.zeros(n)                        # 先填 0（HL 相等 → CLV 定义为 0）
    nz = hl != 0                             # 只在不等的位置做除法，避开 0/0 警告
    clv[nz] = (2.0 * close[nz] - high[nz] - low[nz]) / hl[nz]
    for i in range(period - 1, n):
        cv = clv[i - period + 1:i + 1]
        vv = volume[i - period + 1:i + 1]
        tot = float(np.sum(vv))
        cmf[i] = float(cv.dot(vv) / tot) if tot > 0 else 0.0
    return cmf


def cmf_last(high, low, close, volume, period: int = 20) -> Optional[float]:
    """当日 CMF。截到末 period 根。"""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)
    if len(close) < period:
        return None
    sl = slice(-period, None)
    arr = cmf_series(high[sl], low[sl], close[sl], volume[sl], period)
    v = arr[-1]
    return float(v) if not math.isnan(v) else None


# ===========================================================================
# Choppiness Index（CHOP）—— 方案 D 市况开关的蒸馏源
# 蒸馏源：cinar/indicator v2.1.44 volatility/chop.go
#
#   CHOP = 100 × log10( Σ(TR, n) / (MAX(High,n) − MIN(Low,n)) ) / log10(n)
#
# 口径（逐字对齐 chop.go）：
#   · TR 用「前收」：TR = max(H−L, |H−prevC|, |L−prevC|)；首根无前收退化 H−L
#   · Σ(TR,n) 是最近 n 根真实波幅之和（不是 ATR 平滑）
#   · MAX(High,n)/MIN(Low,n) 是窗口内最高价/最低价
#   · 分母=0（一字板/横盘）时 chop=0（源码 diff==0 → 返回 0）
#   · IdlePeriod = n：前 n 根为预热占位（NaN）
# 含义：chop 低（<38.2）→ 趋势市；高（>61.8）→ 震荡市；之间过渡市。
# ===========================================================================

def chop_series(high, low, close, period: int = 14) -> np.ndarray:
    """Choppiness Index 序列。前 period 个为 NaN（预热占位）。

    与 chop.go 对齐：TR 用前收，窗口内 ΣTR / (HH−LL)，log10 归一。
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    chop = np.full(n, np.nan)
    if n < period + 1:
        return chop

    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev),
        np.abs(low - prev),
    ])

    # Σ(TR, n)：滚动 n 根之和（cumsum 差）
    csum = np.cumsum(tr)
    sum_tr = np.full(n, np.nan)
    # 窗口 [i−period+1, i]；要求起点 ≥1（排除首根退化 TR）
    start = np.arange(n) - (period - 1)
    ok = start >= 1
    idx = np.where(ok)[0]
    s = start[idx]
    sum_tr[idx] = csum[idx] - np.where(s > 0, csum[s - 1], 0.0)

    # HH(n) / LL(n)：窗口内最高/最低。关键：与 ΣTR 同窗对齐——
    # ΣTR 的窗口从 bar1 起（排除首根退化 TR），HH/LL 也必须从 bar1 起窗，
    # 否则分母偏大会把震荡市误判成趋势市。
    if n >= period + 1:
        sw_h = np.lib.stride_tricks.sliding_window_view(high, period)
        sw_l = np.lib.stride_tricks.sliding_window_view(low, period)
        hh = np.full(n, np.nan)
        ll = np.full(n, np.nan)
        # sw_h[k] = high[k:k+period] 对应输出 bar=k+period-1；取 k>=1 即窗口
        # 从 bar1 起，与 ΣTR（start>=1）严格对齐。
        hh[period:] = sw_h[1:].max(axis=1)
        ll[period:] = sw_l[1:].min(axis=1)
        diff = hh - ll

        valid = ok & (diff > 0) & np.isfinite(sum_tr)
        if valid.any():
            ratio = sum_tr[valid] / diff[valid]
            chop[valid] = 100.0 * np.log10(ratio) / math.log10(period)
        # 分母=0（横盘/一字板）→ 0
        zero = ok & (diff == 0)
        chop[zero] = 0.0
    return chop


def chop_last(high, low, close, period: int = 14) -> Optional[float]:
    """当日 Chop。输入至少 period+1 根，否则 None。

    供市场等权指数的「当日市况」判定用（全市场一次性算，不进体检热循环）。
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    if len(close) < period + 1:
        return None
    arr = chop_series(high, low, close, period)
    v = arr[-1]
    return float(v) if np.isfinite(v) else None


# ===========================================================================
# 波动率三件套（蒸馏自 cinar/indicator 的 volatility 包）
#
# 为什么只补这三个（而不是再多抄几个指标）：
#   · 吊灯止损 —— 唯一能直接落到「虚拟盘持仓该在哪止损」的指标，有消费方
#   · 布林 %B / 带宽 —— 我们实测「近 120 天 0 个趋势日、90 天震荡」，震荡市
#     里带宽挤压（squeeze）才是真正有信息量的信号；%B 把「触及上下轨」这种
#     二值判断升级成连续位置
#   · Ulcer / Z-Score —— 分别补「回撤有多痛」和「偏离均值多远」两个维度
#
# 公式对照 cinar/indicator v2.1.44：
#   · Chandelier Exit : volatility/chandelier_exit.go（HHV(High,n) − mult×ATR(n)）
#   · Percent B       : volatility/percent_b.go（(C−LOW)/(UP−LOW)）
#   · Bollinger Width : volatility/bollinger_band_width.go（(UP−LOW)/MID×100）
#   · Ulcer Index     : volatility/ulcer_index.go（窗口内回撤百分比的 RMS）
#   · Z-Score         : volatility/z_score.go（(C−SMA(n))/STD(n)）
#
# 布林口径与 mytt.BOLL 保持一致：SMA + 总体标准差（ddof=0），
# 否则同一个股票在指标面板和 K 线图上会算出两条不一致的布林带。
# ===========================================================================

def _rolling(arr, period: int, fn) -> np.ndarray:
    """滚动窗口统计，输出前 period−1 个为 NaN（与既有 *_series 同约定）。"""
    arr = np.asarray(arr, dtype=float)
    n = len(arr)
    out = np.full(n, np.nan)
    if n < period or period <= 0:
        return out
    sw = np.lib.stride_tricks.sliding_window_view(arr, period)
    out[period - 1:] = fn(sw, axis=1)
    return out


def bollinger_series(close, period: int = 20, mult: float = 2.0
                     ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """布林三轨序列（up / mid / low），口径同 mytt.BOLL（ddof=0）。"""
    close = np.asarray(close, dtype=float)
    mid = _rolling(close, period, np.mean)
    # np.std 默认 ddof=0，与 mytt.STD 一致
    sd = _rolling(close, period, lambda w, axis: w.std(axis=axis))
    return mid + mult * sd, mid, mid - mult * sd


def chandelier_series(high, low, close, period: int = 22, mult: float = 3.0
                      ) -> np.ndarray:
    """吊灯止损（多头）序列：HHV(High, n) − mult × ATR(n)。

    ponytail: 只算多头止损线——A 股虚拟盘不能做空，空头线没有消费方。

    ponytail: 不做「只上不下」的棘轮版本。cinar 原式是原始口径，止损线会随
    n 日高点过期而下移；叠在 K 线上看趋势更直观，改成棘轮会丢掉这个可读性。
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    atr = atr_series(high, low, close, period)
    hh = _rolling(high, period, np.max)
    ok = np.isfinite(atr) & np.isfinite(hh)
    out[ok] = hh[ok] - mult * atr[ok]
    return out


def chandelier_last(high, low, close, period: int = 22, mult: float = 3.0
                    ) -> Optional[float]:
    """当日吊灯止损价。数据不足返回 None。

    ponytail: 这里直接跑全序列再取末值，不做「截尾加速」——消费方只有虚拟盘
    持仓（几只票）和个股页，不是全市场热循环；截尾会让末值与序列版对不上，
    那种"看起来一样、数值差一点"的 bug 最难查。
    """
    arr = chandelier_series(high, low, close, period, mult)
    if not len(arr):
        return None
    v = arr[-1]
    return float(v) if np.isfinite(v) else None


def percent_b_series(close, period: int = 20, mult: float = 2.0) -> np.ndarray:
    """布林 %B 序列：0=贴下轨、0.5=中轨、1=贴上轨（突破轨道时可 <0 或 >1）。"""
    close = np.asarray(close, dtype=float)
    up, _mid, lo = bollinger_series(close, period, mult)
    rng = up - lo
    out = np.full(len(close), np.nan)
    ok = np.isfinite(rng) & (rng > 0)
    out[ok] = (close[ok] - lo[ok]) / rng[ok]
    return out


def bollinger_width_series(close, period: int = 20, mult: float = 2.0
                           ) -> np.ndarray:
    """布林带宽序列：(上轨−下轨)/中轨×100。数值越小=越挤压。"""
    close = np.asarray(close, dtype=float)
    up, mid, lo = bollinger_series(close, period, mult)
    out = np.full(len(close), np.nan)
    ok = np.isfinite(mid) & (mid > 0)
    out[ok] = (up[ok] - lo[ok]) / mid[ok] * 100.0
    return out


def bollinger_squeeze_last(close, period: int = 20, mult: float = 2.0,
                           lookback: int = 120) -> Optional[Dict[str, float]]:
    """带宽挤压读数：{"width": 当前带宽, "pct": 近 lookback 根中的分位}。

    pct=0 表示处于观察窗内最窄处（挤压）；=100 表示最宽。

    ponytail: 只给分位、不给「是否挤压」的布尔阈值——「多窄算挤压」没有公认
    标准，且不同票的带宽量级差很多，硬编码阈值只会误报。
    """
    w = bollinger_width_series(close, period, mult)
    wv = w[np.isfinite(w)]
    if len(wv) < 2:
        return None
    tail = wv[-lookback:] if lookback and lookback > 0 else wv
    cur = float(tail[-1])
    pct = float((tail <= cur).sum()) / len(tail) * 100.0
    return {"width": round(cur, 3), "pct": round(pct, 1)}


def ulcer_index_series(close, period: int = 14) -> np.ndarray:
    """Ulcer Index 序列：窗口内「回撤百分比」的 RMS，越大越痛。

    与 Sharpe 的区别：Sharpe 用波动率（上下都罚），Ulcer 只罚**下跌**且惩罚
    深度与持续，更贴近普通人「套牢有多难受」的真实感受。
    """
    close = np.asarray(close, dtype=float)
    n = len(close)
    out = np.full(n, np.nan)
    if n < period:
        return out
    sw = np.lib.stride_tricks.sliding_window_view(close, period)
    peak = np.maximum.accumulate(sw, axis=1)        # 窗口内running max
    safe = np.where(peak > 0, peak, 1.0)
    dd = 100.0 * (sw - peak) / safe                 # ≤0
    out[period - 1:] = np.sqrt((dd ** 2).mean(axis=1))
    return out


def zscore_series(close, period: int = 20) -> np.ndarray:
    """Z-Score 序列：(C − SMA(n)) / STD(n)。正=高于均值，负=低于均值。

    与 similarity.py 实测结论同向：A 股「越低、越缩量、前期涨得越少 → 未来
    越容易涨」，Z-Score 就是把这个「低」量化成标准差倍数。
    """
    close = np.asarray(close, dtype=float)
    mid = _rolling(close, period, np.mean)
    sd = _rolling(close, period, lambda w, axis: w.std(axis=axis))
    out = np.full(len(close), np.nan)
    ok = np.isfinite(sd) & (sd > 0)
    out[ok] = (close[ok] - mid[ok]) / sd[ok]
    return out


# ===========================================================================
# 自检：用已知小数据验证算法（不依赖真实行情）
# ===========================================================================

def selfcheck(verbose: bool = True) -> dict:
    """手工构造数据，逐项验证指标正确性（防「算错却不知道」）。"""
    out = {"ok": False, "steps": []}

    def step(name, ok, detail=""):
        out["steps"].append({"name": name, "ok": bool(ok), "detail": detail})
        if verbose:
            print(f"  [{'OK ' if ok else 'FAIL'}] {name}"
                  + (f"  {detail}" if detail else ""))
        return ok

    # ---- 1. ATR（手工 TrueRange）----
    # high/low/close 递进，TR 含 prevC
    H = np.array([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24],
                 dtype=float)
    L = np.array([9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23],
                dtype=float)
    C = np.array([9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5,
                 19.5, 20.5, 21.5, 22.5, 23.5], dtype=float)
    atr14 = atr_last(H, L, C, 14)
    # 序列递增、daily range 恒为 1，但收盘居中（mid），TR 还要算 |H−prevC|：
    # H[i]−C[i-1] = 1.5 是最大项 → TR 恒为 1.5，ATR≈1.5（Wilder 正确口径）
    step("ATR(14)≈1.5（含 prevC 缺口）",
         atr14 is not None and abs(atr14 - 1.5) < 1e-4, f"ATR={atr14}")
    step("ATR 数据不足返回 None", atr_last(H[:5], L[:5], C[:5], 14) is None)

    # ---- 2. ATR 与序列版末值一致 ----
    a14 = atr_series(H, L, C, 14)
    step("ATR 序列末值=末值版", (not math.isnan(a14[-1]))
         and abs(a14[-1] - atr14) < 1e-9, f"{a14[-1]} vs {atr14}")

    # ---- 3. RSI：纯涨序列 → 100 ----
    up = np.arange(1, 21, dtype=float)        # 单调涨，RSI(14)=100
    r = rsi_last(up, 14)
    step("RSI(14) 纯涨=100", r is not None and abs(r - 100.0) < 1e-3, f"RSI={r}")
    dn = np.arange(20, 0, -1, dtype=float)    # 单调跌，RSI(14)=0（无亏损→惯例100?）
    # 纯跌：所有 delta<0，loss>0、gain=0 → RSI=0（al>0 分支）
    rd = rsi_last(dn, 14)
    step("RSI(14) 纯跌=0", rd is not None and abs(rd - 0.0) < 1e-3, f"RSI={rd}")

    # ---- 4. RSI 序列前 period 个为 NaN ----
    rs = rsi_series(up, 14)
    step("RSI 序列预热期 NaN", math.isnan(rs[13]) and not math.isnan(rs[14]))

    # ---- 5. SuperTrend 翻转：先跌后涨应在某处翻多 ----
    # 构造一段下跌再转涨，验证 trend 出现过 +1
    seg = np.concatenate([np.linspace(100, 80, 30),
                          np.linspace(80, 100, 20)]).astype(float)
    hh = seg + 1.0
    ll = seg - 1.0
    tr, _, _ = supertrend_series(hh, ll, seg, 10, 3.0)
    step("SuperTrend 含多头段", (tr == 1).any(),
         f"末方向={int(tr[-1])}")
    step("SuperTrend 末值版一致", supertrend_last(hh, ll, seg, 10, 3.0)
         == (int(tr[-1]) if tr[-1] in (1.0, -1.0) else None))
    # 翻多日检测：找到 −1→+1 的拐点，截断到该处应判为翻多
    flips = np.where((tr[1:] == 1) & (tr[:-1] == -1))[0] + 1
    if len(flips):
        fi = int(flips[0])
        fl = supertrend_flip_last(hh[:fi + 1], ll[:fi + 1], seg[:fi + 1])
        step("SuperTrend 翻多日检测", fl is True, f"flip@{fi}")
    else:
        step("SuperTrend 翻多日检测", False, "未检测到翻转")
    step("SuperTrend 翻多短数据=None",
         supertrend_flip_last(hh[:5], ll[:5], seg[:5]) is None)

    # ---- 6. ConnorsRSI：超卖场景应偏低 ----
    # 连续下跌 30 天 → RSI(3) 近 0、streak 负、ROC 负 → ConnorsRSI 很低
    # （lookback 用 20，数据 30 根即可满足 need=20+3+2=25）
    falling = np.linspace(50, 30, 30).astype(float)
    cr = connors_rsi_last(falling, lookback=20, rsi1=3, rsi2=2)
    step("ConnorsRSI 连跌偏低(<30)", cr is not None and cr < 30,
         f"CRSI={cr:.2f}")
    # 连涨 30 天 → 偏高
    rising = np.linspace(30, 50, 30).astype(float)
    crh = connors_rsi_last(rising, lookback=20, rsi1=3, rsi2=2)
    step("ConnorsRSI 连涨偏高(>70)", crh is not None and crh > 70,
         f"CRSI={crh:.2f}")
    step("ConnorsRSI 数据不足返回 None",
         connors_rsi_last(falling[:5], 100) is None)

    # ---- 7. TD9：构造连续 9 根 close<4日前 ----
    # 每隔 5 根的 4 日前都比今天高 → 计数递增
    td = np.array([100, 99, 98, 97, 96,        # 0..4
                   95, 94, 93, 92, 91,         # 5..9
                   90, 89, 88, 87, 86], dtype=float)
    # 从 i=4 起：close[i] < close[i-4] 一直成立（单调递减）
    cnt = td_count_series(td)
    step("TD 计数末值=11", int(cnt[-1]) == 11, f"末计数={int(cnt[-1])}")
    step("TD 计数第9根处=9", int(cnt[12]) == 9, f"cnt[12]={int(cnt[12])}")
    step("TD 末值版一致", td_count_last(td) == int(cnt[-1]))

    # ---- 8. MFI：量能全正、价全涨 → MFI=100 ----
    mc = np.arange(10, 30, dtype=float)        # 纯涨
    mh = mc + 1.0                              # high=close+1 → tp=close
    ml = mc - 1.0
    mv = np.ones(20) * 1000.0
    mfi_v = mfi_last(mh, ml, mc, mv, 14)
    step("MFI 纯涨=100", mfi_v is not None and abs(mfi_v - 100.0) < 1e-3,
         f"MFI={mfi_v}")
    # 价全跌 → 负向资金流 → MFI=0
    mc2 = np.arange(30, 10, -1, dtype=float)   # 纯跌
    mh2 = mc2 + 1.0
    ml2 = mc2 - 1.0
    mfi_d = mfi_last(mh2, ml2, mc2, mv, 14)
    step("MFI 纯跌=0", mfi_d is not None and abs(mfi_d - 0.0) < 1e-3,
         f"MFI={mfi_d}")

    # ---- 9. CMF：收盘价都在区间上半部（CLV>0）且量均匀 → CMF>0 ----
    ch = np.arange(10, 30, dtype=float)
    cl = ch - 2.0            # 低
    cc = ch - 0.2            # 收在接近高位 → CLV 近 +0.9
    cv = np.ones(20) * 500.0
    cmf_v = cmf_last(ch, cl, cc, cv, 20)
    step("CMF 收高位>0", cmf_v is not None and cmf_v > 0,
         f"CMF={cmf_v:.3f}")
    cc2 = ch - 1.8           # 收在接近低位 → CLV 近 −0.9
    cmf_d = cmf_last(ch, cl, cc2, cv, 20)
    step("CMF 收低位<0", cmf_d is not None and cmf_d < 0,
         f"CMF={cmf_d:.3f}")
    step("CMF 数据不足返回 None", cmf_last(ch[:5], cl[:5], cc[:5], cv[:5], 20)
         is None)

    # ---- 10. 末值版与序列版末值全一致（抽样）----
    mfi_s = mfi_series(mh, ml, mc, mv, 14)
    step("MFI 序列末值=末值版", abs(mfi_s[-1] - mfi_v) < 1e-9)
    cmf_s = cmf_series(ch, cl, cc, cv, 20)
    step("CMF 序列末值=末值版", abs(cmf_s[-1] - cmf_v) < 1e-9)

    # ---- 11. Chop（市况开关）：公式 + 趋势/震荡定性 ----
    # 11.0 横盘（H=L=C）→ 分母=0 → chop=0
    flat_h = np.full(16, 50.0)
    flat_l = np.full(16, 50.0)
    flat_c = np.full(16, 50.0)
    cf = chop_last(flat_h, flat_l, flat_c, 14)
    step("Chop 横盘(H=L=C)=0", cf is not None and abs(cf) < 1e-6,
         f"chop={cf}")
    # 11.1 平滑单边趋势（日净移 1、日内振幅仅 0.2）→ chop 低（<38.2 趋势市）
    n = 40
    trend_c = np.linspace(100, 140, n)
    trend_h = trend_c + 0.1
    trend_l = trend_c - 0.1
    ch_trend = chop_last(trend_h, trend_l, trend_c, 14)
    step("Chop 平滑趋势偏低(<38.2)",
         ch_trend is not None and ch_trend < 38.2, f"chop={ch_trend:.1f}")
    # 11.2 锯齿震荡（每日 45↔55 来回、日内振幅小）→ 净位移≈0 但 ΣTR 大 → chop 高
    zz = 50.0 + np.array([5.0 if i % 2 else -5.0 for i in range(n)])
    zz_h = zz + 0.1
    zz_l = zz - 0.1
    ch_choppy = chop_last(zz_h, zz_l, zz, 14)
    step("Chop 锯齿震荡偏高(>61.8)",
         ch_choppy is not None and ch_choppy > 61.8, f"chop={ch_choppy:.1f}")
    step("Chop 趋势<震荡（定性正确）",
         ch_trend is not None and ch_choppy is not None
         and ch_trend < ch_choppy,
         f"{ch_trend:.1f} < {ch_choppy:.1f}")
    # 11.3 序列版末值=末值版
    cs = chop_series(trend_h, trend_l, trend_c, 14)
    step("Chop 序列末值=末值版",
         (not math.isnan(cs[-1])) and abs(cs[-1] - ch_trend) < 1e-9)
    # 11.4 数据不足返回 None
    step("Chop 数据不足返回 None",
         chop_last(trend_h[:10], trend_l[:10], trend_c[:10], 14) is None)

    # ---- 12. 波动率三件套（吊灯止损 / 布林%B·带宽·挤压 / Ulcer / Z-Score）----
    # 12.1 横盘（H=L=C）→ TR=0 → ATR=0 → 止损价=价格本身
    fl = np.full(30, 50.0)
    st_flat = chandelier_last(fl, fl, fl, 22, 3.0)
    step("吊灯 横盘(ATR=0)=价格",
         st_flat is not None and abs(st_flat - 50.0) < 1e-6, f"stop={st_flat}")
    # 12.2 单边上涨：止损价应低于现价、且低于区间高点
    upc = np.linspace(10, 20, 40)
    uph = upc + 0.5
    upl = upc - 0.5
    st_up = chandelier_last(uph, upl, upc, 22, 3.0)
    step("吊灯 上涨中<现价",
         st_up is not None and st_up < upc[-1] and st_up < float(uph.max()),
         f"stop={st_up:.3f} close={upc[-1]:.2f}")
    # 12.3 序列末值=末值版
    cs_full = chandelier_series(uph, upl, upc, 22, 3.0)
    step("吊灯 序列末值=末值版",
         np.isfinite(cs_full[-1]) and abs(cs_full[-1] - st_up) < 1e-9,
         f"{cs_full[-1]} vs {st_up}")
    # 12.4 数据不足返回 None
    step("吊灯 数据不足=None",
         chandelier_last(upc[:10], upl[:10], upc[:10], 22) is None)

    # 12.5 布林 %B / 带宽：用「11,9,11,9…」交替序列精确验算
    #      任意 4 根窗口：均值=10、总体标准差=1 → 上轨 12 / 下轨 8 / 带宽 40%
    alt = np.array([11.0, 9.0] * 8)          # 末值 9
    pb = percent_b_series(alt, 4, 2.0)
    step("%B 收在下轨侧=0.25", abs(pb[-1] - 0.25) < 1e-9, f"%B={pb[-1]:.4f}")
    pb2 = percent_b_series(alt[:15], 4, 2.0)  # 末值 11
    step("%B 收在上轨侧=0.75", abs(pb2[-1] - 0.75) < 1e-9, f"%B={pb2[-1]:.4f}")
    bw = bollinger_width_series(alt, 4, 2.0)
    step("带宽 交替序列=40%", abs(bw[-1] - 40.0) < 1e-9, f"width={bw[-1]:.4f}")
    # 12.6 挤压分位：前段大幅摆动 + 末段几乎不动 → 末段分位应极低
    wide = 50.0 + np.array([5.0 if i % 2 else -5.0 for i in range(40)])
    narrow = 50.0 + np.array([0.1 if i % 2 else -0.1 for i in range(20)])
    sq = bollinger_squeeze_last(np.concatenate([wide, narrow]), 20, 2.0, 120)
    step("挤压 末段收窄→分位<20",
         sq is not None and sq["pct"] < 20, f"pct={sq['pct']} width={sq['width']}")

    # 12.7 Ulcer：单调上涨无回撤→0；单调下跌→>0
    ui_up = ulcer_index_series(np.linspace(10, 20, 30), 14)
    step("Ulcer 单调上涨=0", abs(float(ui_up[-1])) < 1e-9, f"UI={ui_up[-1]}")
    ui_dn = ulcer_index_series(np.linspace(20, 10, 30), 14)
    step("Ulcer 单调下跌>0", float(ui_dn[-1]) > 0, f"UI={ui_dn[-1]:.3f}")

    # 12.8 Z-Score：同一交替序列，末值 9 → −1、末值 11 → +1
    zs = zscore_series(alt, 4)
    step("Z-Score 收低位=-1", abs(zs[-1] + 1.0) < 1e-9, f"z={zs[-1]:.4f}")
    zs2 = zscore_series(alt[:15], 4)
    step("Z-Score 收高位=+1", abs(zs2[-1] - 1.0) < 1e-9, f"z={zs2[-1]:.4f}")

    # 12.9 数据不足一律 NaN / None，绝不拿 0 冒充
    step("波动率三件套 数据不足=NaN/None",
         math.isnan(float(percent_b_series(alt[:3], 4)[-1]))
         and math.isnan(float(zscore_series(alt[:3], 4)[-1]))
         and math.isnan(float(ulcer_index_series(alt[:3], 14)[-1]))
         and bollinger_squeeze_last(alt[:3], 20) is None)

    passed = sum(1 for s in out["steps"] if s["ok"])
    out["total"] = len(out["steps"])
    out["passed"] = passed
    out["failed"] = out["total"] - passed
    out["ok"] = out["failed"] == 0
    return out


if __name__ == "__main__":                # pragma: no cover
    print("indicators_extra 自检")
    print("=" * 60)
    r = selfcheck()
    print("=" * 60)
    print(f"结论：{'全部通过 ✓' if r['ok'] else '存在问题 ✗'}  "
          f"{r['passed']}/{r['total']}")
