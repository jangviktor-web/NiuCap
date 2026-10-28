"""Tier 4：技术指标 → 多空评级（BUY / SELL / NEUTRAL）。

把每个技术指标按其经典阈值区间映射为买卖评级，并聚合出综合评级。

配色约定（关键、与 tvscreener 相反）：
    BUY  = 红（涨） = 前端 var(--up)
    SELL = 绿（跌） = 前端 var(--down)
    NEUTRAL = 灰
A 股地区约定为红涨绿跌，tvscreener 源码是绿涨红跌，**切勿照搬其色值**，
前端一律复用 --up / --down 变量即可自动本地化。

设计原则：
- 纯函数、numpy 无关：输入是已算好的标量 float / None，输出可 JSON 序列化。
- 数值先 round(v, 2) 再比较，规避 float32 边界抖动（5.10→5.09999990 类问题，
  与项目 _gt/_lt/_ge 的容差习惯一致）。
- 无规则或不具备方向性的指标（MA*/成交量/OBV/ATR/ADX/带宽分位等）返回 None，
  前端不渲染徽标，避免制造假信号。
"""
from typing import Any, Dict, List, Optional

EPS = 1e-6
BUY, SELL, NEUTRAL = "BUY", "SELL", "NEUTRAL"

# 中文标签（前端可直接用）
LABEL = {BUY: "买入", SELL: "卖出", NEUTRAL: "中性"}


def _num(value: Any) -> Optional[float]:
    """None / NaN / 非数值 → None；否则 round 到 2 位，规避 float 抖动。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(f, 2)


def _band(v: Optional[float], lo: float, hi: float) -> Optional[str]:
    """lo 以下=BUY（超卖/低位），hi 以上=SELL（超买/高位），中间=NEUTRAL。"""
    if v is None:
        return None
    if v <= lo + EPS:
        return BUY
    if v >= hi - EPS:
        return SELL
    return NEUTRAL


def _band_inv(v: Optional[float], lo: float, hi: float) -> Optional[str]:
    """反向区间（WR、布林%B）：lo 以下=SELL，hi 以上=BUY。"""
    if v is None:
        return None
    if v <= lo + EPS:
        return SELL
    if v >= hi - EPS:
        return BUY
    return NEUTRAL


def _cmp(a: Optional[float], b: Optional[float]) -> Optional[str]:
    """a 高于 b → BUY，低于 → SELL，相等/缺失 → NEUTRAL。"""
    if a is None or b is None:
        return None
    if a > b + EPS:
        return BUY
    if a < b - EPS:
        return SELL
    return NEUTRAL


def rate_indicator(name: str, value: Any,
                  ctx: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """单个指标 → BUY / SELL / NEUTRAL / None（无法评级）。

    ctx 提供配对所需 Companion 值：DIF/DEA、TRIX/TRMA、PDI/MDI、cur（现价）。
    """
    ctx = ctx or {}
    v = _num(value)
    n = (name or "").strip()

    # ---------- 震荡类（超买 / 超卖）----------
    if n in ("RSI6", "RSI12", "RSI24"):
        return _band(v, 30, 70)
    if n in ("K", "D"):
        return _band(v, 20, 80)
    if n == "J":
        return _band(v, 0, 100)
    if n == "CCI14":
        return _band(v, -100, 100)
    if n in ("WR", "WR6"):
        return _band_inv(v, 20, 80)   # WR 倒置：>80 超卖 BUY，<20 超买 SELL
    if n in ("BIAS6", "BIAS12", "BIAS24"):
        return _band(v, -8, 8)
    if n == "PSY":
        return _band(v, 25, 75)
    if n in ("ROC", "MTM"):
        return _band(v, -10, 10)
    if n == "MFI14":
        return _band(v, 20, 80)
    if n == "VR":
        return _band(v, 70, 250)

    # ---------- 趋势 / 方向类（需配对值）----------
    if n == "DIF":
        return _cmp(v, _num(ctx.get("DEA")))
    if n == "DEA":
        return _cmp(v, _num(ctx.get("DIF")))
    if n == "TRIX":
        return _cmp(v, _num(ctx.get("TRMA")))
    if n == "TRMA":
        return _cmp(v, _num(ctx.get("TRIX")))
    if n == "PDI":
        return _cmp(v, _num(ctx.get("MDI")))
    if n == "MDI":
        return _cmp(v, _num(ctx.get("PDI")))
    if n == "MACD柱":
        if v is None:
            return None
        if v > EPS:
            return BUY
        if v < -EPS:
            return SELL
        return NEUTRAL

    # ---------- 价格位置类（需现价 cur）----------
    cur = _num(ctx.get("cur"))
    if n in ("BOLL上轨", "唐安奇上轨", "肯特纳上轨"):
        return BUY if (v is not None and cur is not None and cur >= v - EPS) else None
    if n in ("BOLL下轨", "唐安奇下轨", "肯特纳下轨"):
        return SELL if (v is not None and cur is not None and cur <= v + EPS) else None
    if n in ("BOLL中轨", "BBI"):
        return _cmp(cur, v)   # 现价高于中轨 / BBI → BUY
    if n == "吊灯止损":
        return _cmp(cur, v)   # 现价高于止损线（未破位）→ BUY
    # MA 系列：价位判断，不是阈值判断——现价在均线上方即多头占优。
    # 原先一律不评级，导致「移动平均线」那张表永远 0 票、画不出指针。
    # 这不是"没有明确阈值"的问题：MA 的多空含义就是价格与它的相对位置。
    if n.startswith("MA") and n[2:].isdigit():
        return _cmp(cur, v)

    # ---------- 波动率类 ----------
    if n == "布林%B":
        return _band(v, 0, 1)   # >1 冲出上轨=超买 SELL，<0 跌出下轨=超卖 BUY
    if n == "Z-Score(20)":
        return _band(v, -2, 2)

    # ADX 仅表趋势强度、不表方向；MA*/成交量/OBV/ATR/带宽/分位等无明确多空阈值 → 不评级
    return None


def rate_groups(groups: List[Dict[str, Any]],
                ctx: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """就地给每个指标项加 `rating` 字段（BUY/SELL/NEUTRAL/None）。"""
    for g in groups:
        for it in g.get("items", []):
            it["rating"] = rate_indicator(it.get("name", ""), it.get("value"), ctx)
    return groups


def aggregate_rating(groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总所有评级 → 综合评级 + 方向强度。

    综合评级：多空净票 net = buy - sell，需 |net| >= 3 才判 BUY/SELL（共识门槛），
    否则 NEUTRAL，避免单指标翻转误判。
    方向强度 score：0-100（50 为多空平衡），= 50 + 50 * net / max(1, 已评级总数)。
    """
    buy = sell = neutral = total = 0
    for g in groups:
        for it in g.get("items", []):
            r = it.get("rating")
            if r == BUY:
                buy += 1
            elif r == SELL:
                sell += 1
            elif r == NEUTRAL:
                neutral += 1
            if r in (BUY, SELL, NEUTRAL):
                total += 1
    net = buy - sell
    if net >= 3:
        rating = BUY
    elif net <= -3:
        rating = SELL
    else:
        rating = NEUTRAL
    denom = max(1, total)
    score = round(50.0 + 50.0 * net / denom, 1)
    return {
        "rating": rating,
        "label": LABEL[rating],
        "score": score,        # 0-100 多空方向强度，50 平衡
        "buy": buy,
        "sell": sell,
        "neutral": neutral,
        "total_rated": total,
    }


# TradingView 技术分析三表的映射。
# 它那三个仪表盘不是三套算法，而是同一批指标按类切三份：
#   「震荡指标」只看摆动类，「移动平均线」只看均线，「总结」是全部。
# 这里照搬这个切法，聚合仍走 aggregate_rating，保证三个表口径一致、
# 且分表之和必然等于总结（不会出现"震荡说买、均线说卖、总结说中立"
# 却和两边票数都对不上的情况）。
GAUGE_GROUPS = [
    ("osc", "震荡指标", ["震荡"]),
    ("ma", "移动平均线", ["均线"]),
    ("sum", "总结", None),          # None = 全部组
]


def gauge_ratings(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 GAUGE_GROUPS 切出三个仪表盘的评级。

    组名对不上（后续增删分组）时该表 total_rated=0，rating=None，
    前端会显示"—"而不是画一个假装有结论的指针。
    """
    out = []
    for key, label, cats in GAUGE_GROUPS:
        sub = groups if cats is None else [
            g for g in groups if g.get("cat") in cats]
        agg = aggregate_rating(sub)
        agg["key"] = key
        agg["name"] = label
        if not agg["total_rated"]:
            agg["rating"] = None
            agg["label"] = "—"
            agg["score"] = 50.0
        out.append(agg)
    return out


def selfcheck(verbose: bool = True) -> Dict[str, Any]:
    """评级规则自检：覆盖每个算子、float 边界、配对、缺失、聚合。"""
    cases = [
        # (name, value, ctx, expect)
        ("RSI6", 25, None, BUY),
        ("RSI6", 75, None, SELL),
        ("RSI6", 50, None, NEUTRAL),
        ("RSI6", 30.000001, None, BUY),          # float 边界
        ("RSI24", 29.999999, None, BUY),
        ("K", 15, None, BUY),
        ("D", 85, None, SELL),
        ("J", -5, None, BUY),
        ("J", 105, None, SELL),
        ("J", 50, None, NEUTRAL),
        ("CCI14", -120, None, BUY),
        ("CCI14", 150, None, SELL),
        ("WR", 85, None, BUY),                   # 倒置
        ("WR", 15, None, SELL),
        ("WR6", 50, None, NEUTRAL),
        ("BIAS6", -9, None, BUY),
        ("BIAS6", 9, None, SELL),
        ("PSY", 20, None, BUY),
        ("PSY", 80, None, SELL),
        ("ROC", -12, None, BUY),
        ("ROC", 12, None, SELL),
        ("MTM", -11, None, BUY),
        ("MFI14", 15, None, BUY),
        ("MFI14", 85, None, SELL),
        ("VR", 60, None, BUY),
        ("VR", 300, None, SELL),
        ("VR", 150, None, NEUTRAL),
        # 配对
        ("DIF", 1.2, {"DEA": 0.8}, BUY),
        ("DIF", 0.5, {"DEA": 0.8}, SELL),
        ("DEA", 0.8, {"DIF": 1.2}, SELL),
        ("TRIX", 0.3, {"TRMA": 0.1}, BUY),
        ("TRMA", 0.1, {"TRIX": 0.3}, SELL),
        ("PDI", 25, {"MDI": 18}, BUY),
        ("MDI", 18, {"PDI": 25}, SELL),
        ("MACD柱", 0.5, None, BUY),
        ("MACD柱", -0.5, None, SELL),
        ("MACD柱", 0.0, None, NEUTRAL),
        # 价格位置
        ("BOLL上轨", 10.0, {"cur": 10.5}, BUY),   # 现价>=上轨 → 突破 BUY
        ("BOLL下轨", 9.0, {"cur": 8.5}, SELL),    # 现价<=下轨 → 跌破 SELL
        ("BOLL中轨", 10.0, {"cur": 10.5}, BUY),
        ("BBI", 9.5, {"cur": 10.0}, BUY),
        ("吊灯止损", 9.0, {"cur": 8.5}, SELL),
        ("布林%B", 1.2, None, SELL),
        ("布林%B", -0.2, None, BUY),
        ("Z-Score(20)", 2.5, None, SELL),
        ("Z-Score(20)", -2.5, None, BUY),
        # 缺失 / 无规则
        ("RSI6", None, None, None),
        ("ADX", 30, None, None),                 # 无方向
        ("成交量", 100000, None, None),
        # MA 系列：价位判断（现价 vs 均线），不是阈值判断。
        # 原先这里断言「无规则」，导致「移动平均线」仪表永远 0 票。
        # 注意 ctx 必须带 cur，只给 None 会让 _cmp 返回 None，
        # 于是"期望 None"的旧用例永远通过 —— 测试看着绿，规则其实没生效。
        ("MA5", 10.0, {"cur": 11.0}, BUY),       # 现价在均线上方
        ("MA5", 10.0, {"cur": 9.0}, SELL),       # 现价在均线下方
        ("MA20", 10.0, {"cur": 10.0}, NEUTRAL),  # 贴线
        ("MA5", 10.0, None, None),               # 没有现价 → 不评级
    ]
    fails = 0
    for name, val, ctx, exp in cases:
        got = rate_indicator(name, val, ctx)
        if got != exp:
            fails += 1
            if verbose:
                print(f"[FAIL] {name}={val} ctx={ctx} expect={exp} got={got}")

    # 聚合：全多 → BUY
    grp = [{"cat": "t", "items": [
        {"name": "RSI6", "value": 25, "rating": rate_indicator("RSI6", 25)},
        {"name": "MACD柱", "value": 0.5, "rating": rate_indicator("MACD柱", 0.5)},
        {"name": "DIF", "value": 1.2, "rating": rate_indicator("DIF", 1.2, {"DEA": 0.8})},
        {"name": "WR", "value": 85, "rating": rate_indicator("WR", 85)},
    ]}]
    agg = aggregate_rating(grp)
    if agg["rating"] != BUY or agg["score"] != 100.0:
        fails += 1
        if verbose:
            print(f"[FAIL] aggregate all-buy got {agg}")

    # 聚合：全中性
    grp2 = [{"cat": "t", "items": [
        {"name": "RSI6", "value": 50, "rating": rate_indicator("RSI6", 50)},
        {"name": "WR", "value": 50, "rating": rate_indicator("WR", 50)},
    ]}]
    agg2 = aggregate_rating(grp2)
    if agg2["rating"] != NEUTRAL or agg2["score"] != 50.0:
        fails += 1
        if verbose:
            print(f"[FAIL] aggregate neutral got {agg2}")

    if verbose:
        print(f"indicator_rating selfcheck: {'OK' if fails == 0 else str(fails) + ' FAILED'}")
    return {"fail": fails, "total": len(cases) + 2}


if __name__ == "__main__":
    rc = selfcheck()
    raise SystemExit(1 if rc["fail"] else 0)
