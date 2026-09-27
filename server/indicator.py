"""
技术指标 + 策略信号计算（复用技能自带的 MyTT / strategies）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from .mytt import (MA, EMA, MACD, RSI, KDJ, BOLL, CCI, ATR, OBV, WR, BIAS,
                       DMI, TRIX, BRAR, PSY, MFI, BBI, TAQ, KTN, VR, CR,
                       EMV, DPO, MASS, ASI, XSII, ROC, MTM, DFMA, EXPMA,
                       CROSS, HHV, LLV)
    from . import strategies as st
except ImportError:
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mytt import (MA, EMA, MACD, RSI, KDJ, BOLL, CCI, ATR, OBV, WR, BIAS,
                      DMI, TRIX, BRAR, PSY, MFI, BBI, TAQ, KTN, VR, CR,
                      EMV, DPO, MASS, ASI, XSII, ROC, MTM, DFMA, EXPMA,
                      CROSS, HHV, LLV)
    import strategies as st


def _lst(arr, n=60):
    """numpy 数组 -> 末尾 n 个值，NaN 转 None"""
    import math
    out = []
    for v in list(arr)[-n:]:
        try:
            f = float(v)
            out.append(None if math.isnan(f) or math.isinf(f) else round(f, 3))
        except (TypeError, ValueError):
            out.append(None)
    return out


def compute_indicators(klines):
    """输入 K线 list[dict]，输出指标序列 + 最新信号"""
    if not klines or len(klines) < 30:
        return None

    close = [k["close"] for k in klines]
    high = [k["high"] for k in klines]
    low = [k["low"] for k in klines]
    vol = [k["volume"] for k in klines]
    dates = [k["date"] for k in klines]

    import numpy as np
    C = np.array(close, dtype=float)
    H = np.array(high, dtype=float)
    L = np.array(low, dtype=float)
    V = np.array(vol, dtype=float)

    dif, dea, macd = MACD(C)
    K, D, J = KDJ(C, H, L)
    up, mid, lowb = BOLL(C)

    ma5, ma10, ma20, ma60 = MA(C, 5), MA(C, 10), MA(C, 20), MA(C, 60)
    rsi6, rsi14 = RSI(C, 6), RSI(C, 14)

    cur = C[-1]

    def val(arr):
        """取序列最后一个有效标量值（支持 1D/2D）"""
        import numpy as np
        try:
            a = np.asarray(arr, dtype=float)
            if a.ndim == 2:          # 多列输出（如 WR 两周期）取最后一列
                a = a[:, -1]
            a = a.ravel()
            for v in a[::-1]:
                f = float(v)
                if not (np.isnan(f) or np.isinf(f)):
                    return round(f, 3)
            return None
        except (TypeError, ValueError, IndexError):
            return None

    latest = {
        "price": round(float(cur), 2),
        "date": dates[-1],
        "ma5": val(ma5), "ma10": val(ma10), "ma20": val(ma20), "ma60": val(ma60),
        "dif": val(dif), "dea": val(dea), "macd": val(macd),
        "rsi6": val(rsi6), "rsi14": val(rsi14),
        "k": val(K), "d": val(D), "j": val(J),
        "boll_up": val(up), "boll_mid": val(mid), "boll_low": val(lowb),
        "cci": val(CCI(C, H, L)),
        "wr": val(WR(C, H, L, 14)),
        "bias": val(BIAS(C, 6)),
    }

    # ---- 信号判定 ----
    signals = []

    def near(a, b, tol=1e-9):
        return a is not None and b is not None and a > b - tol

    # 均线多空（短中期为主，MA60 单独看长期趋势）
    if latest["ma5"] and latest["ma20"]:
        if latest["ma5"] > latest["ma20"]:
            signals.append({"name": "短期均线多头", "dir": "bull",
                            "desc": f"MA5({latest['ma5']:.2f}) 在 MA20({latest['ma20']:.2f}) 上方"})
        else:
            signals.append({"name": "短期均线空头", "dir": "bear",
                            "desc": f"MA5({latest['ma5']:.2f}) 在 MA20({latest['ma20']:.2f}) 下方"})
    if latest["ma20"] and latest["ma60"]:
        if latest["ma20"] > latest["ma60"]:
            signals.append({"name": "中期趋势向上", "dir": "bull", "desc": "MA20 在 MA60 上方"})
        else:
            signals.append({"name": "中期趋势向下", "dir": "bear", "desc": "MA20 在 MA60 下方"})

    # MACD 金叉/死叉（结合前一日判断）
    if len(dif) >= 2:
        d0, d1 = float(dif[-1]), float(dif[-2])
        a0, a1 = float(dea[-1]), float(dea[-2])
        if d1 <= a1 and d0 > a0:
            signals.append({"name": "MACD金叉", "dir": "bull", "desc": "DIF上穿DEA，中期动能转强"})
        elif d1 >= a1 and d0 < a0:
            signals.append({"name": "MACD死叉", "dir": "bear", "desc": "DIF下穿DEA，中期动能转弱"})
        elif d0 > a0:
            signals.append({"name": "MACD多头", "dir": "bull", "desc": "DIF在DEA上方运行"})
        else:
            signals.append({"name": "MACD空头", "dir": "bear", "desc": "DIF在DEA下方运行"})

    # RSI
    r = latest["rsi14"]
    if r is not None:
        if r > 70:
            signals.append({"name": "RSI超买", "dir": "bear", "desc": f"RSI14={r:.1f}，短期过热"})
        elif r < 30:
            signals.append({"name": "RSI超卖", "dir": "bull", "desc": f"RSI14={r:.1f}，短期超跌"})

    # KDJ
    if len(K) >= 2 and latest["k"] is not None:
        k0, k1 = float(K[-1]), float(K[-2])
        d0, d1 = float(D[-1]), float(D[-2])
        if k1 <= d1 and k0 > d0:
            signals.append({"name": "KDJ金叉", "dir": "bull", "desc": "K线上穿D线，短线转强"})
        elif k1 >= d1 and k0 < d0:
            signals.append({"name": "KDJ死叉", "dir": "bear", "desc": "K线下穿D线，短线转弱"})

    # 布林带位置
    if latest["boll_low"] and latest["boll_up"]:
        if cur <= latest["boll_low"]:
            signals.append({"name": "触及布林下轨", "dir": "bull", "desc": "价格位于布林下轨附近，超跌"})
        elif cur >= latest["boll_up"]:
            signals.append({"name": "触及布林上轨", "dir": "bear", "desc": "价格位于布林上轨附近，超涨"})

    # 量能（近5日均量 vs 近20日均量）
    if len(V) >= 20:
        v5 = float(V[-5:].mean())
        v20 = float(V[-20:].mean())
        if v20 > 0:
            ratio = v5 / v20
            if ratio > 1.5:
                signals.append({"name": "放量", "dir": "neutral", "desc": f"近5日均量为20日的{ratio:.1f}倍"})
            elif ratio < 0.6:
                signals.append({"name": "缩量", "dir": "neutral", "desc": f"近5日均量为20日的{ratio:.1f}倍"})

    bull = sum(1 for s in signals if s["dir"] == "bull")
    bear = sum(1 for s in signals if s["dir"] == "bear")
    if bull - bear >= 2:
        score, level = 70 + min(bull - bear, 3) * 8, "偏多"
    elif bear - bull >= 2:
        score, level = 30 - min(bear - bull, 3) * 8, "偏空"
    else:
        score, level = 50 + (bull - bear) * 5, "中性"
    score = max(5, min(95, score))

    # 序列长度必须与 K 线等长：前端主图 x 轴用全部 K 线日期，若此处截断，
    # ECharts 按索引对齐会只渲染前一段（表现为均线/通道"只画一半"）。
    n = len(klines)
    return {
        "latest": latest,
        "signals": signals,
        "score": score,
        "level": level,
        "series": {
            "dates": dates[-n:],
            "close": _lst(C, n),
            "ma5": _lst(ma5, n),
            "ma10": _lst(ma10, n),
            "ma20": _lst(ma20, n),
            "ma60": _lst(ma60, n),
            "dif": _lst(dif, n),
            "dea": _lst(dea, n),
            "macd": _lst(macd, n),
            "k": _lst(K, n),
            "d": _lst(D, n),
            "j": _lst(J, n),
            "rsi6": _lst(rsi6, n),
            "rsi14": _lst(rsi14, n),
            "boll_up": _lst(up, n),
            "boll_mid": _lst(mid, n),
            "boll_low": _lst(lowb, n),
            "volume": [round(float(x), 0) for x in list(V)[-n:]],
        },
    }


# ---------------------------------------------------------------------------
# 策略信号（复用 skills 的 strategies 模块）
# ---------------------------------------------------------------------------

import pandas as pd

STRATEGY_LABELS = {
    "ma_cross": "均线金叉",
    "macd": "MACD",
    "rsi": "RSI超买超卖",
    "boll": "布林带轨道",
    "kdj": "KDJ金叉",
}

STRATEGY_KEYS = ("ma_cross", "macd", "rsi", "boll", "kdj")


def _latest_signal(arr):
    """策略返回 numpy array(1/-1/0)，取最近一次非零信号及其距今天数"""
    import numpy as np
    a = np.asarray(arr, dtype=float)
    nz = np.nonzero(a)[0]
    if len(nz) == 0:
        return 0, None
    idx = int(nz[-1])
    return float(a[idx]), len(a) - 1 - idx


def strategy_signals(klines):
    """5 个子策略 + ensemble 共振，输出最新信号及距今交易日数"""
    if not klines or len(klines) < 30:
        return None

    df = pd.DataFrame(klines)
    out = []
    for key in STRATEGY_KEYS:
        fn = st.STRATEGY_MAP.get(key)
        if not fn:
            continue
        try:
            arr = fn(df)
        except Exception:
            continue
        sig, ago = _latest_signal(arr)
        if sig > 0:
            txt, d = "买入", "bull"
        elif sig < 0:
            txt, d = "卖出", "bear"
        else:
            txt, d = "观望", "neutral"
        out.append({
            "key": key, "name": STRATEGY_LABELS.get(key, key),
            "signal": txt, "dir": d,
            "ago": ago,
            "ago_text": ("—" if ago is None else ("今日" if ago == 0 else f"{ago}日前")),
        })

    # ensemble 共振
    try:
        arr = st.STRATEGY_MAP["ensemble"](df)
        sig, ago = _latest_signal(arr)
        if sig > 0:
            txt, d = "共振买入", "bull"
        elif sig < 0:
            txt, d = "共振卖出", "bear"
        else:
            txt, d = "无共振", "neutral"
        out.append({
            "key": "ensemble", "name": "多策略共振",
            "signal": txt, "dir": d, "ago": ago,
            "ago_text": ("—" if ago is None else ("今日" if ago == 0 else f"{ago}日前")),
        })
    except Exception:
        pass

    # 汇总买卖票数
    buy = sum(1 for s in out if s["dir"] == "bull" and s["key"] != "ensemble")
    sell = sum(1 for s in out if s["dir"] == "bear" and s["key"] != "ensemble")

    return {"items": out, "buy_votes": buy, "sell_votes": sell,
            "total": len(STRATEGY_KEYS)}


def chip_analysis(klines):
    """筹码分布（复用技能 chip_distribution）"""
    try:
        import chip_distribution as cd
        fn = None
        for name in ("calc_chip", "compute_chip", "chip_distribution", "analyze"):
            if hasattr(cd, name):
                fn = getattr(cd, name)
                break
        if fn is None:
            return None
        res = fn(klines)
        if isinstance(res, dict):
            return res
        return None
    except Exception:
        return None


def channel_series(klines, n=None):
    """唐安奇通道 + 肯特纳通道 序列（借鉴 Klang，用于 K 线主图叠加）

    TAQ   : 海龟交易通道，上轨=近20日最高，下轨=近20日最低，中轨=均值
    KTN   : 肯特纳通道，中轨=EMA20，上下轨=中轨±2×(ATR10 的 EMA 或均值)

    n: 取末尾 n 个点；默认 None 表示与 K 线等长（保证主图叠加时 x 轴对齐）。
    """
    if not klines or len(klines) < 30:
        return None

    if n is None:
        n = len(klines)

    import numpy as np
    C = np.array([k["close"] for k in klines], dtype=float)
    H = np.array([k["high"] for k in klines], dtype=float)
    L = np.array([k["low"] for k in klines], dtype=float)

    try:
        tu, tm, td = TAQ(H, L, 20)
    except Exception:
        tu = tm = td = None
    try:
        ku, km, kd = KTN(C, H, L, 20, 10)
    except Exception:
        ku = km = kd = None

    return {
        "taq_up": _lst(tu, n) if tu is not None else None,
        "taq_mid": _lst(tm, n) if tm is not None else None,
        "taq_down": _lst(td, n) if td is not None else None,
        "ktn_up": _lst(ku, n) if ku is not None else None,
        "ktn_mid": _lst(km, n) if km is not None else None,
        "ktn_down": _lst(kd, n) if kd is not None else None,
    }


def stop_line_series(klines, n=None):
    """吊灯止损线序列（用于 K 线主图叠加），长度与 K 线一致。

    蒸馏自 cinar/indicator 的 volatility/chandelier_exit.go：
    多头止损价 = HHV(High, 22) − 3 × ATR(22)。

    之所以单独给一条线而不是塞进 `channel_series`：通道是「区间」，止损线是
    「一条该走的线」，语义不同、画法也不同（止损线要更醒目）。
    """
    if not klines or len(klines) < 30:
        return None

    if n is None:
        n = len(klines)

    import numpy as np
    H = np.array([k["high"] for k in klines], dtype=float)
    L = np.array([k["low"] for k in klines], dtype=float)
    C = np.array([k["close"] for k in klines], dtype=float)
    try:
        import indicators_extra as ix
        s = ix.chandelier_series(H, L, C, 22, 3.0)
    except Exception:
        return None
    return {"chandelier": _lst(s, n)}


# ===========================================================================
# 全套技术指标（借鉴 Klang info 命令：30+ 指标一屏总览）
# 说明：mytt.py 已内置 71 个指标函数，此处把此前未接入面板的全部暴露出来
# ===========================================================================

def _scalar(arr):
    """取序列最后一个有效标量（支持 1D/2D），返回 (value, prev_value)"""
    import numpy as np
    try:
        a = np.asarray(arr, dtype=float)
        if a.ndim == 2:
            a = a[:, -1]
        a = a.ravel()
        vals = [float(v) for v in a if not (np.isnan(v) or np.isinf(v))]
        if not vals:
            return None, None
        return round(vals[-1], 3), (round(vals[-2], 3) if len(vals) > 1 else None)
    except (TypeError, ValueError, IndexError):
        return None, None


def _money(v):
    """大数字转可读文本（万/亿）"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if abs(f) >= 1e8:
        return f"{f/1e8:.2f}亿"
    if abs(f) >= 1e4:
        return f"{f/1e4:.2f}万"
    return f"{f:.0f}"


def all_indicators(klines):
    """全部技术指标总览（借鉴 Klang 的 info 命令）

    返回按类别分组的指标列表，每项含 name/value/prev/note，
    以及自动生成的信号汇总。
    """
    if not klines or len(klines) < 30:
        return None

    import numpy as np
    C = np.array([k["close"] for k in klines], dtype=float)
    H = np.array([k["high"] for k in klines], dtype=float)
    L = np.array([k["low"] for k in klines], dtype=float)
    O = np.array([k["open"] for k in klines], dtype=float)
    V = np.array([k["volume"] for k in klines], dtype=float)
    cur = float(C[-1])
    signals = []

    def add(sig_list, name, kind):
        if sig_list:
            for s in sig_list:
                signals.append({"name": name, "dir": kind, "desc": s})

    groups = []

    # ---------- 1. 均线组 ----------
    ma_items = []
    for n in (5, 10, 20, 30, 60, 120):
        if len(C) >= n:
            v, p = _scalar(MA(C, n))
            ma_items.append({"name": f"MA{n}", "value": v, "prev": p})
    if len(ma_items) >= 4:
        seq = [m["value"] for m in ma_items[:4]]
        if all(x is not None for x in seq):
            if seq == sorted(seq, reverse=True):
                signals.append({"name": "均线多头排列", "dir": "bull",
                                "desc": "MA5>MA10>MA20>MA60，多头趋势明确"})
            elif seq == sorted(seq):
                signals.append({"name": "均线空头排列", "dir": "bear",
                                "desc": "MA5<MA10<MA20<MA60，空头趋势明确"})
    groups.append({"cat": "均线", "items": ma_items})

    # ---------- 2. MACD / TRIX / DMI 趋势组 ----------
    dif, dea, macd_hist = MACD(C)
    # TRIX 返回 (trix, trma)，可能为单值或序列
    try:
        trix_v, trma_v = TRIX(C)
    except Exception:
        trix_v, trma_v = None, None
    try:
        pdi, mdi, adx, adxr = DMI(C, H, L)
    except Exception:
        pdi = mdi = adx = adxr = None
    try:
        bbi_v, _ = _scalar(BBI(C))
    except Exception:
        bbi_v = None

    trend_items = [
        {"name": "DIF", "value": _scalar(dif)[0], "prev": _scalar(dif)[1]},
        {"name": "DEA", "value": _scalar(dea)[0], "prev": _scalar(dea)[1]},
        {"name": "MACD柱", "value": _scalar(macd_hist)[0], "prev": _scalar(macd_hist)[1]},
        {"name": "TRIX", "value": _scalar(trix_v)[0], "prev": _scalar(trix_v)[1]},
        {"name": "TRMA", "value": _scalar(trma_v)[0], "prev": _scalar(trma_v)[1]},
        {"name": "PDI", "value": _scalar(pdi)[0], "prev": _scalar(pdi)[1]},
        {"name": "MDI", "value": _scalar(mdi)[0], "prev": _scalar(mdi)[1]},
        {"name": "ADX", "value": _scalar(adx)[0], "prev": _scalar(adx)[1]},
        {"name": "BBI", "value": bbi_v, "prev": None},
    ]
    groups.append({"cat": "趋势", "items": trend_items})

    # DMI 信号
    adx_v = _scalar(adx)[0]
    pdi_v, mdi_v = _scalar(pdi)[0], _scalar(mdi)[0]
    if adx_v is not None and pdi_v is not None and mdi_v is not None:
        if adx_v > 25:
            if pdi_v > mdi_v:
                signals.append({"name": "DMI多头趋势", "dir": "bull",
                                "desc": f"ADX={adx_v:.1f}>25 且 PDI>MDI，趋势向上且力度足"})
            else:
                signals.append({"name": "DMI空头趋势", "dir": "bear",
                                "desc": f"ADX={adx_v:.1f}>25 且 MDI>PDI，趋势向下且力度足"})
        else:
            signals.append({"name": "DMI趋势不明", "dir": "neutral",
                            "desc": f"ADX={adx_v:.1f}<25，处于震荡格局"})
    # TRIX 金叉
    try:
        if _scalar(trix_v)[0] is not None and _scalar(trma_v)[0] is not None:
            tv, tp = _scalar(trix_v)
            mv, mp = _scalar(trma_v)
            if tp is not None and mp is not None:
                if tp <= mp and tv > mv:
                    signals.append({"name": "TRIX金叉", "dir": "bull", "desc": "TRIX上穿TRMA，长线动能转强"})
                elif tp >= mp and tv < mv:
                    signals.append({"name": "TRIX死叉", "dir": "bear", "desc": "TRIX下穿TRMA，长线动能转弱"})
    except Exception:
        pass
    # BBI 多空
    if bbi_v is not None:
        if cur > bbi_v:
            signals.append({"name": "站上BBI", "dir": "bull", "desc": f"现价{cur:.2f} 在 BBI({bbi_v:.2f}) 之上"})
        else:
            signals.append({"name": "跌破BBI", "dir": "bear", "desc": f"现价{cur:.2f} 在 BBI({bbi_v:.2f}) 之下"})

    # ---------- 3. 震荡组 ----------
    rsi6, rsi12, rsi24 = RSI(C, 6), RSI(C, 12), RSI(C, 24)
    K, D, J = KDJ(C, H, L)
    cci_v = CCI(C, H, L)
    wr_v, wr1 = WR(C, H, L)
    bias1, bias2, bias3 = BIAS(C)
    psy_v, psyma = PSY(C)
    roc_v, maroc = ROC(C)
    mtm_v, mtmma = MTM(C)

    osc_items = [
        {"name": "RSI6", "value": _scalar(rsi6)[0], "prev": _scalar(rsi6)[1]},
        {"name": "RSI12", "value": _scalar(rsi12)[0], "prev": _scalar(rsi12)[1]},
        {"name": "RSI24", "value": _scalar(rsi24)[0], "prev": _scalar(rsi24)[1]},
        {"name": "K", "value": _scalar(K)[0], "prev": _scalar(K)[1]},
        {"name": "D", "value": _scalar(D)[0], "prev": _scalar(D)[1]},
        {"name": "J", "value": _scalar(J)[0], "prev": _scalar(J)[1]},
        {"name": "CCI14", "value": _scalar(cci_v)[0], "prev": _scalar(cci_v)[1]},
        {"name": "WR", "value": _scalar(wr_v)[0], "prev": _scalar(wr_v)[1]},
        {"name": "WR6", "value": _scalar(wr1)[0], "prev": _scalar(wr1)[1]},
        {"name": "BIAS6", "value": _scalar(bias1)[0], "prev": _scalar(bias1)[1]},
        {"name": "BIAS12", "value": _scalar(bias2)[0], "prev": _scalar(bias2)[1]},
        {"name": "BIAS24", "value": _scalar(bias3)[0], "prev": _scalar(bias3)[1]},
        {"name": "PSY", "value": _scalar(psy_v)[0], "prev": _scalar(psy_v)[1]},
        {"name": "ROC", "value": _scalar(roc_v)[0], "prev": _scalar(roc_v)[1]},
        {"name": "MTM", "value": _scalar(mtm_v)[0], "prev": _scalar(mtm_v)[1]},
    ]
    groups.append({"cat": "震荡", "items": osc_items})

    # 震荡类信号
    for nm, val, hi, low in (("RSI6", _scalar(rsi6)[0], 80, 20),
                             ("RSI24", _scalar(rsi24)[0], 70, 30)):
        if val is not None:
            if val > hi:
                signals.append({"name": f"{nm}超买", "dir": "bear", "desc": f"{nm}={val:.1f}，短期过热"})
            elif val < low:
                signals.append({"name": f"{nm}超卖", "dir": "bull", "desc": f"{nm}={val:.1f}，短期超跌"})
    j_v = _scalar(J)[0]
    if j_v is not None:
        if j_v > 100:
            signals.append({"name": "KDJ超买区", "dir": "bear", "desc": f"J={j_v:.1f}>100"})
        elif j_v < 0:
            signals.append({"name": "KDJ超卖区", "dir": "bull", "desc": f"J={j_v:.1f}<0"})
    cci_now = _scalar(cci_v)[0]
    if cci_now is not None:
        if cci_now > 100:
            signals.append({"name": "CCI超买", "dir": "bear", "desc": f"CCI={cci_now:.1f}>100"})
        elif cci_now < -100:
            signals.append({"name": "CCI超卖", "dir": "bull", "desc": f"CCI={cci_now:.1f}<-100"})
    # WR 威廉指标（0-100，>80 超卖、<20 超买）
    wr_now = _scalar(wr_v)[0]
    if wr_now is not None:
        if wr_now > 80:
            signals.append({"name": "WR超卖", "dir": "bull", "desc": f"WR={wr_now:.1f}>80，超卖区"})
        elif wr_now < 20:
            signals.append({"name": "WR超买", "dir": "bear", "desc": f"WR={wr_now:.1f}<20，超买区"})
    # PSY 心理线（>75 过热、<25 低迷）
    psy_now = _scalar(psy_v)[0]
    if psy_now is not None:
        if psy_now > 75:
            signals.append({"name": "PSY过热", "dir": "bear", "desc": f"PSY={psy_now:.1f}>75，市场情绪过热"})
        elif psy_now < 25:
            signals.append({"name": "PSY低迷", "dir": "bull", "desc": f"PSY={psy_now:.1f}<25，市场情绪低迷"})
    # ROC / MTM 动量方向
    roc_now = _scalar(roc_v)[0]
    if roc_now is not None:
        if roc_now > 10:
            signals.append({"name": "ROC强势", "dir": "bull", "desc": f"ROC={roc_now:.1f}，12日动量强劲"})
        elif roc_now < -10:
            signals.append({"name": "ROC弱势", "dir": "bear", "desc": f"ROC={roc_now:.1f}，12日动量疲弱"})
    # BIAS 乖离率（偏离均线过多有回归压力）
    bias6_now = _scalar(bias1)[0]
    if bias6_now is not None:
        if bias6_now > 8:
            signals.append({"name": "BIAS正乖离过大", "dir": "bear",
                            "desc": f"BIAS6={bias6_now:.1f}%，短期偏离均线过多，有回归压力"})
        elif bias6_now < -8:
            signals.append({"name": "BIAS负乖离过大", "dir": "bull",
                            "desc": f"BIAS6={bias6_now:.1f}%，短期超跌，有反弹需求"})
    # KDJ 金叉死叉
    try:
        k_arr = np.asarray(K, dtype=float).ravel()
        d_arr = np.asarray(D, dtype=float).ravel()
        if len(k_arr) >= 2:
            if k_arr[-2] <= d_arr[-2] and k_arr[-1] > d_arr[-1]:
                signals.append({"name": "KDJ金叉", "dir": "bull", "desc": "K线上穿D线，短线转强"})
            elif k_arr[-2] >= d_arr[-2] and k_arr[-1] < d_arr[-1]:
                signals.append({"name": "KDJ死叉", "dir": "bear", "desc": "K线下穿D线，短线转弱"})
    except Exception:
        pass
    # MACD 金叉死叉
    try:
        dif_arr = np.asarray(dif, dtype=float).ravel()
        dea_arr = np.asarray(dea, dtype=float).ravel()
        if len(dif_arr) >= 2:
            if dif_arr[-2] <= dea_arr[-2] and dif_arr[-1] > dea_arr[-1]:
                signals.append({"name": "MACD金叉", "dir": "bull", "desc": "DIF上穿DEA，中期动能转强"})
            elif dif_arr[-2] >= dea_arr[-2] and dif_arr[-1] < dea_arr[-1]:
                signals.append({"name": "MACD死叉", "dir": "bear", "desc": "DIF下穿DEA，中期动能转弱"})
    except Exception:
        pass
    # 均线金叉死叉（MA5 vs MA20）
    try:
        ma5_a = np.asarray(MA(C, 5), dtype=float).ravel()
        ma20_a = np.asarray(MA(C, 20), dtype=float).ravel()
        if len(ma5_a) >= 2:
            if ma5_a[-2] <= ma20_a[-2] and ma5_a[-1] > ma20_a[-1]:
                signals.append({"name": "均线金叉", "dir": "bull", "desc": "MA5上穿MA20，短期趋势转强"})
            elif ma5_a[-2] >= ma20_a[-2] and ma5_a[-1] < ma20_a[-1]:
                signals.append({"name": "均线死叉", "dir": "bear", "desc": "MA5下穿MA20，短期趋势转弱"})
    except Exception:
        pass

    # ---------- 4. 通道组 ----------
    up, mid, lowb = BOLL(C)
    try:
        taq_up, taq_mid, taq_down = TAQ(H, L, 20)
    except Exception:
        taq_up = taq_mid = taq_down = None
    try:
        ktn_up, ktn_mid, ktn_down = KTN(C, H, L, 20, 10)
    except Exception:
        ktn_up = ktn_mid = ktn_down = None
    atr_v = ATR(C, H, L, 20)

    ch_items = [
        {"name": "BOLL上轨", "value": _scalar(up)[0], "prev": _scalar(up)[1]},
        {"name": "BOLL中轨", "value": _scalar(mid)[0], "prev": _scalar(mid)[1]},
        {"name": "BOLL下轨", "value": _scalar(lowb)[0], "prev": _scalar(lowb)[1]},
        {"name": "唐安奇上轨", "value": _scalar(taq_up)[0], "prev": _scalar(taq_up)[1]},
        {"name": "唐安奇中轨", "value": _scalar(taq_mid)[0], "prev": _scalar(taq_mid)[1]},
        {"name": "唐安奇下轨", "value": _scalar(taq_down)[0], "prev": _scalar(taq_down)[1]},
        {"name": "肯特纳上轨", "value": _scalar(ktn_up)[0], "prev": _scalar(ktn_up)[1]},
        {"name": "肯特纳中轨", "value": _scalar(ktn_mid)[0], "prev": _scalar(ktn_mid)[1]},
        {"name": "肯特纳下轨", "value": _scalar(ktn_down)[0], "prev": _scalar(ktn_down)[1]},
        {"name": "ATR20", "value": _scalar(atr_v)[0], "prev": _scalar(atr_v)[1]},
    ]
    groups.append({"cat": "通道", "items": ch_items})

    # 通道信号
    for label, arr, kind_up, kind_dn in (
        ("布林", up, "突破布林上轨", "跌破布林下轨"),
        ("唐安奇", taq_up, "突破唐安奇上轨", "跌破唐安奇下轨"),
    ):
        try:
            u = _scalar(arr)[0]
            if u is not None:
                if cur >= u:
                    signals.append({"name": kind_up, "dir": "bull",
                                    "desc": f"现价{cur:.2f} 触及{label}上轨({u:.2f})，突破形态"})
        except Exception:
            pass
    try:
        lo = _scalar(lowb)[0]
        if lo is not None and cur <= lo:
            signals.append({"name": "跌破布林下轨", "dir": "bear",
                            "desc": f"现价{cur:.2f} 触及布林下轨({lo:.2f})，超跌"})
    except Exception:
        pass
    try:
        td = _scalar(taq_down)[0]
        if td is not None and cur <= td:
            signals.append({"name": "跌破唐安奇下轨", "dir": "bear",
                            "desc": f"现价{cur:.2f} 跌破唐安奇下轨({td:.2f})，海龟离场信号"})
    except Exception:
        pass

    # ---------- 5. 量能组 ----------
    obv_v = OBV(C, V)
    mfi_v = MFI(C, H, L, V, 14)
    vr_v = VR(C, V)
    emv_v, emvma = EMV(H, L, V)
    vol_ratio = None
    if len(V) >= 21:
        avg20 = float(np.mean(V[-21:-1]))     # 前20日均量
        if avg20 > 0:
            vol_ratio = round(float(V[-1]) / avg20, 2)

    vol_items = [
        {"name": "量比", "value": vol_ratio, "prev": None},
        {"name": "成交量", "value": _scalar(V)[0], "prev": _scalar(V)[1], "text": _money(_scalar(V)[0])},
        {"name": "OBV", "value": _scalar(obv_v)[0], "prev": _scalar(obv_v)[1], "text": _money(_scalar(obv_v)[0])},
        {"name": "MFI14", "value": _scalar(mfi_v)[0], "prev": _scalar(mfi_v)[1]},
        {"name": "VR", "value": _scalar(vr_v)[0], "prev": _scalar(vr_v)[1]},
        {"name": "EMV", "value": _scalar(emv_v)[0], "prev": _scalar(emv_v)[1]},
        {"name": "EMVMA", "value": _scalar(emvma)[0], "prev": _scalar(emvma)[1]},
    ]
    groups.append({"cat": "量能", "items": vol_items})

    if vol_ratio is not None:
        if vol_ratio > 2:
            signals.append({"name": "显著放量", "dir": "neutral", "desc": f"量比{vol_ratio:.2f}，成交量显著放大"})
        elif vol_ratio < 0.5:
            signals.append({"name": "明显缩量", "dir": "neutral", "desc": f"量比{vol_ratio:.2f}，成交量明显萎缩"})
    mfi_now = _scalar(mfi_v)[0]
    if mfi_now is not None:
        if mfi_now > 80:
            signals.append({"name": "MFI超买", "dir": "bear", "desc": f"MFI={mfi_now:.1f}>80，资金面过热"})
        elif mfi_now < 20:
            signals.append({"name": "MFI超卖", "dir": "bull", "desc": f"MFI={mfi_now:.1f}<20，资金面超跌"})
    # VR 容量比率（<70 低迷、>250 过热、>450 警戒）
    vr_now = _scalar(vr_v)[0]
    if vr_now is not None:
        if vr_now > 450:
            signals.append({"name": "VR过热警戒", "dir": "bear", "desc": f"VR={vr_now:.1f}>450，成交量过热"})
        elif vr_now > 250:
            signals.append({"name": "VR偏高", "dir": "bear", "desc": f"VR={vr_now:.1f}>250，量能偏热"})
        elif vr_now < 70:
            signals.append({"name": "VR低迷", "dir": "bull", "desc": f"VR={vr_now:.1f}<70，量能极度低迷"})

    # ---------- 6. 情绪组 ----------
    try:
        ar, br = BRAR(O, C, H, L)
    except Exception:
        ar = br = None
    try:
        cr_v = CR(C, H, L)
    except Exception:
        cr_v = None
    try:
        dpo_v = DPO(C)
    except Exception:
        dpo_v = None
    try:
        mass_v = MASS(H, L)
    except Exception:
        mass_v = None
    try:
        asi_v = ASI(O, C, H, L)
    except Exception:
        asi_v = None
    try:
        xsii_v = XSII(C, H, L)
    except Exception:
        xsii_v = None
    try:
        dfma_v = DFMA(C)
    except Exception:
        dfma_v = None
    try:
        e12, e50 = EXPMA(C)
    except Exception:
        e12 = e50 = None

    sent_items = [
        {"name": "AR", "value": _scalar(ar)[0], "prev": _scalar(ar)[1]},
        {"name": "BR", "value": _scalar(br)[0], "prev": _scalar(br)[1]},
        {"name": "CR", "value": _scalar(cr_v)[0], "prev": _scalar(cr_v)[1]},
        {"name": "DPO", "value": _scalar(dpo_v)[0], "prev": _scalar(dpo_v)[1]},
        {"name": "MASS", "value": _scalar(mass_v)[0], "prev": _scalar(mass_v)[1]},
        {"name": "ASI", "value": _scalar(asi_v)[0], "prev": _scalar(asi_v)[1]},
        {"name": "EXPMA12", "value": _scalar(e12)[0], "prev": _scalar(e12)[1]},
        {"name": "EXPMA50", "value": _scalar(e50)[0], "prev": _scalar(e50)[1]},
    ]
    groups.append({"cat": "情绪", "items": sent_items})

    # ---------- 7. 波动率组（蒸馏自 cinar/indicator 的 volatility 包）----------
    # 选这四个而不是多抄几个指标，是因为它们都能直接落到操作上：
    # 止损有具体价位、位置是连续量、回撤有痛感、偏离可度量。
    try:
        import indicators_extra as ix
    except Exception:
        ix = None
    if ix is not None:
        try:
            stop_now = ix.chandelier_last(H, L, C, 22, 3.0)
        except Exception:
            stop_now = None
        try:
            pb_s = ix.percent_b_series(C, 20, 2.0)
            bw_s = ix.bollinger_width_series(C, 20, 2.0)
            ui_s = ix.ulcer_index_series(C, 14)
            zs_s = ix.zscore_series(C, 20)
            sq = ix.bollinger_squeeze_last(C, 20, 2.0, 120)
        except Exception:
            pb_s = bw_s = ui_s = zs_s = sq = None

        vol_items = [
            {"name": "吊灯止损", "value": (round(stop_now, 2)
                                          if stop_now is not None else None),
             "prev": None},
            {"name": "布林%B", "value": _scalar(pb_s)[0] if pb_s is not None else None,
             "prev": _scalar(pb_s)[1] if pb_s is not None else None},
            {"name": "布林带宽%", "value": _scalar(bw_s)[0] if bw_s is not None else None,
             "prev": _scalar(bw_s)[1] if bw_s is not None else None},
            {"name": "带宽分位", "value": (sq["pct"] if sq else None), "prev": None},
            {"name": "Ulcer(14)", "value": _scalar(ui_s)[0] if ui_s is not None else None,
             "prev": _scalar(ui_s)[1] if ui_s is not None else None},
            {"name": "Z-Score(20)", "value": _scalar(zs_s)[0] if zs_s is not None else None,
             "prev": _scalar(zs_s)[1] if zs_s is not None else None},
        ]
        groups.append({"cat": "波动率", "items": vol_items})

        # 自动信号：只挑「会改变操作」的三种
        pb_now = _scalar(pb_s)[0] if pb_s is not None else None
        if pb_now is not None:
            if pb_now > 1:
                signals.append({"name": "突破布林上轨", "dir": "bear",
                                "desc": f"%B={pb_now:.2f}>1，已冲出上轨"})
            elif pb_now < 0:
                signals.append({"name": "跌破布林下轨", "dir": "bull",
                                "desc": f"%B={pb_now:.2f}<0，已跌出下轨"})
        if sq and sq.get("pct") is not None and sq["pct"] <= 10:
            signals.append({"name": "带宽挤压", "dir": "neutral",
                            "desc": f"带宽处于近 120 日 {sq['pct']}% 分位，"
                                    f"波动极度收缩（变盘前兆，方向未定）"})
        if stop_now is not None and cur < stop_now:
            signals.append({"name": "跌破吊灯止损", "dir": "bear",
                            "desc": f"现价 {cur:.2f} < 吊灯止损 {stop_now:.2f}"})

    # ---------- 涨跌统计 ----------
    chg_pct = None
    if len(C) >= 2:
        chg_pct = round((cur - float(C[-2])) / float(C[-2]) * 100, 2)
    hi20 = _scalar(HHV(C, 20))[0] if len(C) >= 20 else None
    lo20 = _scalar(LLV(C, 20))[0] if len(C) >= 20 else None
    hi60 = _scalar(HHV(C, 60))[0] if len(C) >= 60 else None
    lo60 = _scalar(LLV(C, 60))[0] if len(C) >= 60 else None

    bull = sum(1 for s in signals if s["dir"] == "bull")
    bear = sum(1 for s in signals if s["dir"] == "bear")
    neutral = sum(1 for s in signals if s["dir"] == "neutral")

    if bull - bear >= 3:
        verdict, vdir = "技术面偏多", "bull"
    elif bear - bull >= 3:
        verdict, vdir = "技术面偏空", "bear"
    elif bull - bear >= 1:
        verdict, vdir = "技术面略偏多", "bull"
    elif bear - bull >= 1:
        verdict, vdir = "技术面略偏空", "bear"
    else:
        verdict, vdir = "技术面中性", "neutral"

    return {
        "groups": groups,
        "signals": signals,
        "stats": {
            "price": round(cur, 2),
            "change_pct": chg_pct,
            "high20": hi20, "low20": lo20,
            "high60": hi60, "low60": lo60,
            "vol_ratio": vol_ratio,
            "atr_pct": (round(_scalar(atr_v)[0] / cur * 100, 2)
                        if _scalar(atr_v)[0] and cur else None),
        },
        "verdict": verdict,
        "verdict_dir": vdir,
        "bull_count": bull,
        "bear_count": bear,
        "neutral_count": neutral,
        "indicator_count": sum(len(g["items"]) for g in groups),
    }
