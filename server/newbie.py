"""小白选股引擎 —— 面向零基础用户的一键选股。

设计取向（与现有 screener.py 的区别）：
- screener.py 面向"懂行的人"：给一堆参数和 15 个策略，要求用户自己判断；
- 本模块面向"不懂的人"：给 4 个大按钮，点一下就出结果，并且每一只票都
  用大白话解释"为什么选它"，再配一个 0-100 的友好度评分。

评分不是玄学，每一分都有出处：
- 每套方案有一个评分函数，逐项加分并在 labels 里留下"因为什么加的"；
- 最终对得分做全市场百分位压缩，映射到 40~98 区间，避免出现"99 分幻觉"。

全部基于当日快照计算，不逐股拉 K 线，秒级扫全市场。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional

# ===========================================================================
# 基础工具
# ===========================================================================


def _n(v, default: float = 0.0) -> float:
    """安全转 float，NaN/Inf 归零。"""
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _is_st(name: str) -> bool:
    u = (name or "").upper()
    return "ST" in u or "退" in (name or "")


def _is_index_like(code: str, name: str) -> bool:
    """排除指数/ETF/退市等不适合新手买入的标的。"""
    if _is_st(name):
        return True
    n = name or ""
    for kw in ("指数", "ETF", "基金", "转债", "国债", "B股"):
        if kw in n:
            return True
    return False


def _board(code: str) -> str:
    """板块归类：主板 / 创业板 / 科创板 / 北交所。"""
    c = code
    if c.startswith("bj"):
        return "北交所"
    d = "".join(ch for ch in c if ch.isdigit())
    if d.startswith(("300", "301")):
        return "创业板"
    if d.startswith(("688", "689")):
        return "科创板"
    return "主板"


def _limit_pct(code: str) -> float:
    """按板块的涨跌停幅度（%）。"""
    if code.startswith("bj"):
        return 30.0
    d = "".join(ch for ch in code if ch.isdigit())
    if d.startswith(("300", "301", "302", "688", "689")):
        return 20.0
    return 10.0


def _fmt_cap(cap: float) -> str:
    """市值转人话：亿。"""
    return f"{cap:.0f}亿"


def _fmt_amt(amt: float) -> str:
    """成交额（万元）转人话。"""
    if amt >= 10000:
        return f"{amt / 10000:.1f}亿"
    return f"{amt:.0f}万"


# ===========================================================================
# 百分位压缩 —— 把原始得分映射到人类可读区间
# ===========================================================================


def _compress(scores: List[float], lo: int = 40, hi: int = 98) -> List[int]:
    """把一组原始得分按排名压到 [lo, hi]，保证"分高就是真靠前"。"""
    n = len(scores)
    if n == 0:
        return []
    if n == 1:
        return [hi]
    order = sorted(range(n), key=lambda i: scores[i])
    out = [0] * n
    for rank, idx in enumerate(order):
        out[idx] = round(lo + (hi - lo) * rank / (n - 1))
    return out


# ===========================================================================
# 通用排除：不适合新手买入的标的，一律先进黑名单
# ===========================================================================


def _base_filter(r: Dict[str, Any]) -> bool:
    """基础准入：非 ST、非指数/ETF、有价格、有成交。"""
    if _is_index_like(r.get("code", ""), r.get("name", "")):
        return False
    if _n(r.get("price")) <= 0:
        return False
    if _n(r.get("amount")) <= 0:
        return False
    return True


# ===========================================================================
# 方案一：稳健白马 —— 给"想拿得住、怕亏本金"的人
# ===========================================================================


def _score_steady(r: Dict[str, Any]) -> float:
    """稳健白马评分：重估值合理 + 盘子够大 + 波动温和。

    思路：新手最怕的是"买了就腰斩"。所以这里优先要"跌不动"的属性：
    大市值、低估值、走势平稳，而不是涨得最猛的。
    """
    cap = _n(r.get("total_cap"))
    pe = _n(r.get("pe"))
    pb = _n(r.get("pb"))
    chg = abs(_n(r.get("change_pct")))
    tov = _n(r.get("turnover"))
    amount = _n(r.get("amount"))

    if cap < 200 or pe <= 0 or pe > 45:
        return -1.0                      # 不满足硬门槛，直接出局
    if chg > 7:                          # 波动太大，不适合求稳
        return -1.0

    s = 0.0
    s += min(cap / 5000, 1.0) * 30       # 市值越大越稳（5000亿封顶）
    s += max(0.0, (45 - pe) / 35) * 28   # PE 越低越好（45 以下有效）
    s += max(0.0, (6 - pb) / 5) * 14     # PB 低加分
    s += max(0.0, (3 - chg) / 3) * 14    # 当日波动越小越好
    s += min(tov / 3, 1.0) * 8           # 要有一定流动性
    s += min(amount / 80000, 1.0) * 6    # 成交额够大才买得进卖得出
    return s


def _reasons_steady(r: Dict[str, Any]) -> List[str]:
    cap = _n(r.get("total_cap"))
    pe = _n(r.get("pe"))
    pb = _n(r.get("pb"))
    chg = _n(r.get("change_pct"))
    out = []
    if cap >= 1000:
        out.append(f"大块头，市值 {_fmt_cap(cap)}，抗跌能力相对强")
    else:
        out.append(f"中大盘，市值 {_fmt_cap(cap)}，不算小票")
    if 0 < pe <= 20:
        out.append(f"估值便宜，市盈率只有 {pe:.1f} 倍")
    elif 0 < pe <= 45:
        out.append(f"估值不算贵，市盈率 {pe:.1f} 倍")
    if 0 < pb <= 2:
        out.append(f"市净率 {pb:.2f}，接近净资产，下跌空间有限")
    if abs(chg) <= 1.5:
        out.append("今天走势平稳，没有大起大落")
    return out


# ===========================================================================
# 方案二：超跌反弹 —— 给"想抄底、能承受波动"的人
#
# 说明：真正的"超跌"必须看历史跌幅，快照里没有这个信息，所以本方案分两步：
#   1) 先用快照做粗筛（今天上涨但未涨停 + 流动性够），把候选压到几百只；
#   2) 再对候选逐个拉日K，算"20日跌幅 / 距60日高点回撤 / 是否站上5日线"，
#      只有真的跌够了才留下。这一步由 enrich 参数驱动，属于必要的额外请求。
# ===========================================================================

# 超跌门槛：20 日跌幅至少这么多才算"跌够了"
_REBOUND_DROP20 = 0.12          # 12%
_REBOUND_DRAWDOWN = 0.20        # 距 60 日高点回撤 20%


def _prefilter_rebound(r: Dict[str, Any]) -> bool:
    """超跌方案的快照粗筛：今天在涨、没涨停、盘子与流动性过得去。"""
    chg = _n(r.get("change_pct"))
    cap = _n(r.get("total_cap"))
    amount = _n(r.get("amount"))
    limit = _limit_pct(r.get("code", ""))
    if chg <= 0 or chg >= limit - 0.5:
        return False
    if cap < 30 or amount < 8000:
        return False
    return True


def _rebound_metrics(klines: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """从日K序列算出超跌相关指标；数据不足返回 None。"""
    if not klines or len(klines) < 25:
        return None
    closes = [_n(k.get("close")) for k in klines]
    highs = [_n(k.get("high")) for k in klines]
    if not closes or closes[-1] <= 0:
        return None

    last = closes[-1]
    # 20 日跌幅（正数表示下跌幅度）
    base20 = closes[-21] if len(closes) >= 21 else closes[0]
    drop20 = (base20 - last) / base20 if base20 > 0 else 0.0

    # 距 60 日高点的回撤
    win = highs[-60:] if len(highs) >= 60 else highs
    peak = max(win) if win else 0.0
    drawdown = (peak - last) / peak if peak > 0 else 0.0

    # 是否刚站上 5 日线（反弹启动信号）
    ma5 = sum(closes[-5:]) / 5
    above_ma5 = last >= ma5
    prev_ma5 = sum(closes[-6:-1]) / 5 if len(closes) >= 6 else ma5
    just_crossed = above_ma5 and last > prev_ma5

    return {
        "drop20": drop20,
        "drawdown": drawdown,
        "above_ma5": 1.0 if above_ma5 else 0.0,
        "just_crossed": 1.0 if just_crossed else 0.0,
        "ma5": ma5,
    }


def _score_rebound_rich(r: Dict[str, Any], m: Dict[str, float]) -> float:
    """带真实超跌指标的反弹评分。"""
    chg = _n(r.get("change_pct"))
    tov = _n(r.get("turnover"))
    amount = _n(r.get("amount"))
    price, low, high = _n(r.get("price")), _n(r.get("low")), _n(r.get("high"))
    limit = _limit_pct(r.get("code", ""))

    drop20 = m["drop20"]
    drawdown = m["drawdown"]

    # 硬门槛：真的跌够了，而且要出现反弹迹象
    if drop20 < _REBOUND_DROP20 or drawdown < _REBOUND_DRAWDOWN:
        return -1.0
    if not m["above_ma5"]:
        return -1.0

    s = 0.0
    # 跌得越透，反弹空间越大（30% 跌幅打满）
    s += min(drop20 / 0.30, 1.0) * 30
    # 距高点回撤越深越好（50% 打满）
    s += min(drawdown / 0.50, 1.0) * 20
    # 刚站上5日线，是"启动"而不是"还在跌"
    s += m["just_crossed"] * 16
    # 当日收在振幅上半区 => 有资金承接
    if high > low and price > 0:
        s += (price - low) / (high - low) * 14
    s += min(tov / 10, 1.0) * 10
    s += min(amount / 100000, 1.0) * 10
    return s


def _reasons_rebound_rich(r: Dict[str, Any], m: Dict[str, float]) -> List[str]:
    chg = _n(r.get("change_pct"))
    tov = _n(r.get("turnover"))
    amount = _n(r.get("amount"))
    low, high, price = _n(r.get("low")), _n(r.get("high")), _n(r.get("price"))
    out = [
        f"过去 20 天跌了 {m['drop20'] * 100:.1f}%，跌得比较透",
        f"距离 60 天最高点回撤 {m['drawdown'] * 100:.1f}%，属于深度调整",
    ]
    if m["just_crossed"]:
        out.append("今天价格刚站上 5 日均线，像是止跌企稳的信号")
    else:
        out.append("价格已回到 5 日均线上方，短期有转强迹象")
    out.append(f"今天涨 {chg:.1f}%，没到涨停，还买得进")
    if high > low and price > 0 and (price - low) / (high - low) > 0.7:
        out.append("收盘价在当日高位，说明尾盘有资金在抢")
    out.append(f"成交额 {_fmt_amt(amount)}，换手 {tov:.1f}%，流动性够用")
    return out


# ===========================================================================
# 方案三：成长活跃 —— 给"想赚快钱、能扛波动"的人
# ===========================================================================


def _score_growth(r: Dict[str, Any]) -> float:
    """成长活跃评分：当日有力度 + 量能配合 + 有流动性。"""
    chg = _n(r.get("change_pct"))
    tov = _n(r.get("turnover"))
    amount = _n(r.get("amount"))
    cap = _n(r.get("total_cap"))

    limit = _limit_pct(r.get("code", ""))
    if chg <= 0 or chg >= limit - 0.5:
        return -1.0
    if cap < 50 or cap > 3000:
        return -1.0
    if tov < 2 or amount < 15000:
        return -1.0

    s = 0.0
    s += min(chg / (limit * 0.8), 1.0) * 34    # 涨得有力但不封板
    s += min(tov / 12, 1.0) * 26
    s += min(amount / 200000, 1.0) * 22
    s += max(0.0, (2000 - cap) / 2000) * 18 if cap <= 2000 else 0
    return s


def _reasons_growth(r: Dict[str, Any]) -> List[str]:
    chg = _n(r.get("change_pct"))
    tov = _n(r.get("turnover"))
    amount = _n(r.get("amount"))
    cap = _n(r.get("total_cap"))
    out = [f"今天涨 {chg:.1f}%，短线有力度"]
    if tov >= 5:
        out.append(f"换手率 {tov:.1f}%，买卖活跃，进出方便")
    out.append(f"成交额 {_fmt_amt(amount)}，不缺接盘的")
    if cap <= 500:
        out.append(f"市值 {_fmt_cap(cap)}，盘子不大，拉升相对容易")
    return out


# ===========================================================================
# 方案四：打板热点 —— 给"就想追涨停、清楚风险"的人
# ===========================================================================


def _score_hot(r: Dict[str, Any]) -> float:
    """涨停/准涨停评分。仅取真封板或极接近封板的标的。"""
    chg = _n(r.get("change_pct"))
    amount = _n(r.get("amount"))
    tov = _n(r.get("turnover"))
    price, high = _n(r.get("price")), _n(r.get("high"))

    limit = _limit_pct(r.get("code", ""))
    if chg < limit - 0.3:
        return -1.0                              # 没到板，不算打板
    if amount < 5000:
        return -1.0

    s = 0.0
    s += min(amount / 100000, 1.0) * 40          # 封单/成交越大越强
    s += min(tov / 15, 1.0) * 26
    # 收盘价贴着最高价 => 封得死
    if high > 0 and price >= high - 0.001:
        s += 34
    return s


def _reasons_hot(r: Dict[str, Any]) -> List[str]:
    chg = _n(r.get("change_pct"))
    amount = _n(r.get("amount"))
    tov = _n(r.get("turnover"))
    price, high = _n(r.get("price")), _n(r.get("high"))
    out = []
    if chg >= _limit_pct(r.get("code", "")) - 0.05:
        out.append(f"今天涨停（+{chg:.1f}%），封板状态")
    else:
        out.append(f"接近涨停（+{chg:.1f}%），属于强势票")
    if high > 0 and price >= high - 0.001:
        out.append("收盘价就是全天最高价，封得很死")
    out.append(f"成交额 {_fmt_amt(amount)}，资金关注度高")
    if tov >= 5:
        out.append(f"换手率 {tov:.1f}%，筹码充分交换")
    out.append("⚠️ 涨停股次日可能高开低走，只适合能承受波动的人")
    return out


# ===========================================================================
# 方案注册表
# ===========================================================================

PRESETS: List[Dict[str, Any]] = [
    {
        "key": "steady",
        "icon": "🏛️",
        "name": "稳健白马",
        "tagline": "想安安稳稳拿住，怕亏本金",
        "desc": "大盘子、估值合理的公司，波动相对小，适合第一次买股票或想长期拿着的人。",
        "risk": "低",
        "hold": "中长期",
        "color": "#2f9e6f",
        "score": _score_steady,
        "reasons": _reasons_steady,
    },
    {
        "key": "rebound",
        "icon": "🪃",
        "name": "超跌反弹",
        "tagline": "想捡便宜，赌它跌多了会弹",
        "desc": "先筛出过去 20 天跌超 12%、距高点回撤超 20%，且今天刚站上 5 日均线的票。"
                "买的是止跌企稳的第一天，不追高。",
        "risk": "中高",
        "hold": "短线（几天）",
        "color": "#d38b1f",
        "score": None,              # 走 enrich 分支，需真实K线
        "reasons": None,
        "prefilter": _prefilter_rebound,
        "enrich": True,
    },
    {
        "key": "growth",
        "icon": "🚀",
        "name": "成长活跃",
        "tagline": "想赚快钱，能扛得住波动",
        "desc": "当日有力度上涨、成交量跟得上的活跃股，短线机会多，但涨跌都快。",
        "risk": "高",
        "hold": "短线（几周）",
        "color": "#c2453d",
        "score": _score_growth,
        "reasons": _reasons_growth,
    },
    {
        "key": "hot",
        "icon": "🔥",
        "name": "打板热点",
        "tagline": "就想追最热的票，清楚在赌",
        "desc": "今天已经涨停或接近涨停的强势股。收益可能很高，但风险同样高，新手请谨慎。",
        "risk": "极高",
        "hold": "超短线（隔日）",
        "color": "#b3261e",
        "score": _score_hot,
        "reasons": _reasons_hot,
    },
]

PRESET_BY_KEY = {p["key"]: p for p in PRESETS}


def list_presets() -> List[Dict[str, Any]]:
    """返回给前端渲染的方案目录（不含函数引用）。"""
    return [
        {
            "key": p["key"], "icon": p["icon"], "name": p["name"],
            "tagline": p["tagline"], "desc": p["desc"],
            "risk": p["risk"], "hold": p["hold"], "color": p["color"],
        }
        for p in PRESETS
    ]


# ===========================================================================
# 统一主题标签 —— 从快照字段推出一个"这只票是什么货色"的短标签
# ===========================================================================


def _tags(r: Dict[str, Any]) -> List[str]:
    t = []
    cap = _n(r.get("total_cap"))
    pe = _n(r.get("pe"))
    pb = _n(r.get("pb"))

    if cap >= 2000:
        t.append("超大盘")
    elif cap >= 500:
        t.append("大盘")
    elif cap >= 100:
        t.append("中盘")
    else:
        t.append("小盘")

    if 0 < pe <= 15:
        t.append("低估值")
    elif 0 < pe <= 30:
        t.append("估值合理")
    elif 30 < pe <= 60:
        t.append("估值偏高")
    elif pe > 60:
        t.append("高估值")
    elif pe <= 0:
        t.append("亏损中")

    if 0 < pb <= 1.5:
        t.append("破净边缘")

    b = _board(r.get("code", ""))
    if b != "主板":
        t.append(b)
    return t


# ===========================================================================
# 主入口
# ===========================================================================


def pick(rows: List[Dict[str, Any]], preset: str = "steady",
         limit: int = 20, kline_fn: Optional[Callable] = None,
         enrich_cap: int = 90, workers: int = 12) -> Dict[str, Any]:
    """按方案选股。

    kline_fn: 可选，签名 (code) -> list[dict]，用于需要真实历史行情的方案
              （如"超跌反弹"）。不传则该方案退化为按流动性排序的粗筛结果。
    enrich_cap: 需要拉K线时，最多对多少只候选做深挖。

    重要性能事实：行情接口对拉K线有服务端限流，实测吞吐恒为约 2.5 只/秒，
    加线程无效（12/24/36 线程耗时完全一致）。所以 enrich_cap 必须压得很小，
    否则单次请求会跑几十秒。生产路径应由 app.py 的后台缓存预热，接口只读缓存。
    """
    p = PRESET_BY_KEY.get(preset)
    if p is None:
        p = PRESET_BY_KEY["steady"]

    base = [r for r in rows if _base_filter(r)]
    meta = {k: p[k] for k in ("key", "icon", "name", "tagline", "desc",
                               "risk", "hold", "color")}

    # ---- 分支 A：需要真实K线的方案（超跌反弹） ----
    if p.get("enrich"):
        pre = p["prefilter"]
        cand = [r for r in base if pre(r)]
        # 限流下只能深挖很小的池子：优先取成交额居中的（超跌股通常不是当日最热门的）
        cand.sort(key=lambda r: _n(r.get("amount")), reverse=True)
        mid = len(cand) // 6                      # 跳过最头部最热的那批
        cand = cand[mid:mid + enrich_cap]

        scored: List[Any] = []
        note = ""
        if kline_fn is not None and cand:
            import concurrent.futures as _cf

            def _work(r):
                try:
                    kl = kline_fn(r["code"])
                except Exception:
                    return None
                m = _rebound_metrics(kl or [])
                if m is None:
                    return None
                s = _score_rebound_rich(r, m)
                if s < 0:
                    return None
                return (s, r, m)

            with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
                for res in ex.map(_work, cand):
                    if res:
                        scored.append(res)
        elif cand:
            note = "（未取到历史行情，当前仅按流动性与当日强度粗筛，不是真正的超跌判断）"
            scored = [(_n(r.get("amount")), r, None) for r in cand]

        if not scored:
            return {
                "preset": meta, "items": [], "universe": len(rows),
                "matched": 0, "scanned": len(cand), "note": note or
                "在本次扫描范围内没有同时满足「跌得够透 + 刚站上5日线」的股票，可以换一个方案试试。",
            }

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:limit]
        finals = _compress([s for s, _, _ in top], lo=40, hi=98)

        items = []
        for (raw, r, m), score_val in zip(top, finals):
            if m is not None:
                reasons = _reasons_rebound_rich(r, m)
                extra = {"drop20": round(m["drop20"] * 100, 1),
                         "drawdown": round(m["drawdown"] * 100, 1)}
            else:
                reasons = ["今天上涨且未涨停，流动性满足基本要求",
                           "未能取到历史行情，无法确认超跌幅度"]
                extra = {}
            items.append(_pack(r, score_val, reasons, extra))

        return {"preset": meta, "items": items, "universe": len(rows),
                "matched": len(scored), "scanned": len(cand), "note": note}

    # ---- 分支 B：纯快照方案 ----
    fn, rfn = p["score"], p["reasons"]
    scored = []
    for r in base:
        s = fn(r)
        if s < 0:
            continue
        scored.append((s, r))

    if not scored:
        return {
            "preset": meta, "items": [], "universe": len(rows),
            "matched": 0, "scanned": len(base),
            "note": "当前市场没有符合这套方案的股票，可以换一个方案试试。",
        }

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]
    finals = _compress([s for s, _ in top], lo=40, hi=98)

    items = [_pack(r, sv, rfn(r)) for (_, r), sv in zip(top, finals)]
    return {"preset": meta, "items": items, "universe": len(rows),
            "matched": len(scored), "scanned": len(base), "note": ""}



def _pack(r: Dict[str, Any], score_val: int, reasons: List[str],
          extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把一只票整理成前端要的形状。"""
    item = {
        "code": r.get("code"),
        "name": r.get("name"),
        "price": round(_n(r.get("price")), 2),
        "change_pct": round(_n(r.get("change_pct")), 2),
        "score": score_val,
        "reasons": reasons,
        "tags": _tags(r),
        "pe": round(_n(r.get("pe")), 1),
        "pb": round(_n(r.get("pb")), 2),
        "total_cap": round(_n(r.get("total_cap")), 1),
        "turnover": round(_n(r.get("turnover")), 2),
        "amount": round(_n(r.get("amount")), 0),
    }
    if extra:
        item.update(extra)
    return item

