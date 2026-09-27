"""
关键价位计算 —— 借鉴 TSP 个股分析的「9 类关键价位」

纯函数，基于日K序列实时计算（毫秒级）：
  1. 压力支撑 (近 N 日高低点簇)
  2. 枢轴点 (Pivot Points: P / R1-R3 / S1-S3)
  3. 前高前低 (近 60/120 日极值)
  4. Keltner 通道 (EMA20 ± 2×ATR10)
  5. ATR 波动通道 (Close ± ATR)
  6. 缺口位 (向上/向下跳空缺口)
  7. 斐波那契回撤 (近期波段 0.236/0.382/0.5/0.618/0.786)
  8. 整数关口 (价格附近的整十/整百位)
  9. 成交密集区 (按价格区间成交量分布 Top 簇)
"""


def _f(v, d=0.0):
    try:
        x = float(v)
        return d if x != x else x
    except (TypeError, ValueError):
        return d


def _ema(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _atr(klines, n=14):
    trs = []
    for i, k in enumerate(klines):
        hi = _f(k["high"]); lo = _f(k["low"])
        pc = _f(klines[i - 1]["close"]) if i > 0 else _f(k["open"])
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    if not trs:
        return 0.0
    n = min(n, len(trs))
    return sum(trs[-n:]) / n


def key_levels(klines):
    """返回 9 类关键价位，均为 dict 列表 {label, price, kind}"""
    if not klines or len(klines) < 20:
        return []

    closes = [_f(k["close"]) for k in klines]
    highs = [_f(k["high"]) for k in klines]
    lows = [_f(k["low"]) for k in klines]
    cur = closes[-1]
    out = []

    # --- 1. 压力支撑（近20日高点/低点 + 近5日）---
    h20 = max(highs[-20:]); l20 = min(lows[-20:])
    h5 = max(highs[-5:]); l5 = min(lows[-5:])
    out.append({"label": "20日压力", "price": round(h20, 2), "kind": "resistance"})
    out.append({"label": "20日支撑", "price": round(l20, 2), "kind": "support"})
    out.append({"label": "5日压力", "price": round(h5, 2), "kind": "resistance"})
    out.append({"label": "5日支撑", "price": round(l5, 2), "kind": "support"})

    # --- 2. 枢轴点 (标准 Pivot) ---
    h = highs[-1]; l = lows[-1]; c = closes[-1]
    p = (h + l + c) / 3.0
    r1 = 2 * p - l; s1 = 2 * p - h
    r2 = p + (h - l); s2 = p - (h - l)
    r3 = h + 2 * (p - l); s3 = l - 2 * (h - p)
    for lab, px in [("枢轴P", p), ("R1", r1), ("R2", r2), ("S1", s1), ("S2", s2)]:
        out.append({"label": lab, "price": round(px, 2),
                    "kind": "resistance" if px > cur else "support"})

    # --- 3. 前高前低 (60/120日) ---
    if len(klines) >= 60:
        out.append({"label": "60日高", "price": round(max(highs[-60:]), 2), "kind": "resistance"})
        out.append({"label": "60日低", "price": round(min(lows[-60:]), 2), "kind": "support"})
    if len(klines) >= 120:
        out.append({"label": "120日高", "price": round(max(highs[-120:]), 2), "kind": "resistance"})
        out.append({"label": "120日低", "price": round(min(lows[-120:]), 2), "kind": "support"})

    # --- 4. Keltner 通道 (EMA20 ± 2×ATR10) ---
    ema20 = _ema(closes, 20)[-1]
    atr10 = _atr(klines, 10)
    out.append({"label": "Keltner上轨", "price": round(ema20 + 2 * atr10, 2), "kind": "resistance"})
    out.append({"label": "Keltner下轨", "price": round(ema20 - 2 * atr10, 2), "kind": "support"})

    # --- 5. ATR 波动通道 ---
    atr14 = _atr(klines, 14)
    out.append({"label": "ATR上沿", "price": round(cur + atr14, 2), "kind": "resistance"})
    out.append({"label": "ATR下沿", "price": round(cur - atr14, 2), "kind": "support"})

    # --- 6. 缺口位（近 60 日跳空缺口）---
    gaps = 0
    for i in range(max(1, len(klines) - 60), len(klines)):
        prev_c = closes[i - 1]
        op = _f(klines[i]["open"])
        if prev_c <= 0:
            continue
        # 向上跳空：今日最低 > 昨收
        if _f(klines[i]["low"]) > prev_c * 1.005:
            out.append({"label": "向上缺口", "price": round((_f(klines[i]["low"]) + op) / 2, 2),
                        "kind": "support"})
            gaps += 1
        # 向下跳空：今日最高 < 昨收
        elif _f(klines[i]["high"]) < prev_c * 0.995:
            out.append({"label": "向下缺口", "price": round((_f(klines[i]["high"]) + op) / 2, 2),
                        "kind": "resistance"})
            gaps += 1
        if gaps >= 4:
            break

    # --- 7. 斐波那契回撤 (近 60 日波段) ---
    seg = min(60, len(klines))
    sh = max(highs[-seg:]); sl = min(lows[-seg:])
    diff = sh - sl
    if diff > 0:
        for ratio, lab in [(0.236, "Fib23.6%"), (0.382, "Fib38.2%"),
                           (0.5, "Fib50%"), (0.618, "Fib61.8%"), (0.786, "Fib78.6%")]:
            px = sh - diff * ratio
            out.append({"label": lab, "price": round(px, 2),
                        "kind": "resistance" if px > cur else "support"})

    # --- 8. 整数关口（当前价附近，步长自适应）---
    step = 10 if cur >= 100 else 5 if cur >= 30 else 1
    base = round(cur / step) * step
    for i in (-1, 0, 1, 2):
        px = base + i * step
        if px > 0:
            out.append({"label": f"整数{int(px)}", "price": round(px, 2),
                        "kind": "resistance" if px > cur else "support"})

    # --- 9. 成交密集区（价格分段，成交量加权 Top3）---
    lo_bound = min(lows[-60:]) if len(klines) >= 60 else min(lows)
    hi_bound = max(highs[-60:]) if len(klines) >= 60 else max(highs)
    span = hi_bound - lo_bound
    if span > 0:
        bins = 20
        vol_bins = [0.0] * bins
        px_bins = [0.0] * bins
        seg_data = klines[-60:] if len(klines) >= 60 else klines
        for k in seg_data:
            mid = (_f(k["high"]) + _f(k["low"])) / 2.0
            bi = int((mid - lo_bound) / span * (bins - 1))
            bi = max(0, min(bins - 1, bi))
            vol_bins[bi] += _f(k["volume"])
            px_bins[bi] = lo_bound + (bi + 0.5) / bins * span
        top = sorted(range(bins), key=lambda i: -vol_bins[i])[:3]
        for bi in top:
            if vol_bins[bi] > 0:
                px = px_bins[bi]
                out.append({"label": "成交密集", "price": round(px, 2),
                            "kind": "resistance" if px > cur else "support"})

    # 去重（同价位 0.5% 内视为同一）+ 按与现价距离排序
    seen = []
    for lv in sorted(out, key=lambda x: abs(x["price"] - cur)):
        if all(abs(lv["price"] - s) / max(cur, 1) > 0.005 for s in seen):
            seen.append(lv["price"])
        else:
            continue
    return sorted(out, key=lambda x: x["price"])
