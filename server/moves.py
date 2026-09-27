"""
异动监控 —— 借鉴 TSP 的「盘中异动聚合」

基于全市场快照，聚合同日异动信号（零新增采集）：
  涨停 / 炸板(曾涨停后开板) / 跌停 / 大涨 / 大跌 / 放量
按信号优先级 + 涨跌幅排序。

炸板判定：当日 high 触及涨停价但 close 未封住。
"""


def _f(v, d=0.0):
    try:
        x = float(v)
        return d if x != x else x
    except (TypeError, ValueError):
        return d


def _limit_pct(code):
    c = code.replace("sh", "").replace("sz", "").replace("bj", "")
    if code.startswith("bj"):
        return 30.0
    if c.startswith(("300", "301", "302", "688", "689")):
        return 20.0
    return 10.0


def _is_st(name):
    u = (name or "").upper()
    return "ST" in u


def detect_moves(rows, amount_min=100000.0):
    """
    rows: 全市场快照
    返回 {'涨停':[...], '炸板':[...], ...} 每项为股票列表（含涨跌幅等）
    """
    buckets = {"涨停": [], "炸板": [], "跌停": [], "大涨": [],
               "大跌": [], "放量": []}

    for r in rows:
        code = r["code"]; name = r.get("name", "")
        cp = _f(r["change_pct"])
        price = _f(r["price"]); prev = _f(r["prev_close"])
        hi = _f(r["high"]); lo = _f(r["low"])
        amt = _f(r["amount"])
        if price <= 0 or prev <= 0:
            continue

        lim = 5.0 if _is_st(name) else _limit_pct(code)

        item = {
            "code": code, "name": name, "price": round(price, 2),
            "change_pct": round(cp, 2), "turnover": round(_f(r["turnover"]), 2),
            "amount": round(amt, 0), "total_cap": round(_f(r["total_cap"]), 1),
        }

        # 涨停价 / 跌停价
        up_px = round(prev * (1 + lim / 100.0), 2)
        down_px = round(prev * (1 - lim / 100.0), 2)

        is_limit_up = hi >= up_px - 0.01 and price >= up_px - 0.01
        touched_up = hi >= up_px - 0.01
        is_limit_down = lo <= down_px + 0.01 and price <= down_px + 0.01

        if is_limit_up:
            buckets["涨停"].append(item)
        elif touched_up:
            buckets["炸板"].append(item)
        elif is_limit_down:
            buckets["跌停"].append(item)
        elif cp >= 7.0:
            buckets["大涨"].append(item)
        elif cp <= -7.0:
            buckets["大跌"].append(item)

        if amt >= amount_min and abs(cp) >= 3.0:
            buckets["放量"].append(item)

    # 每个桶内按涨跌幅绝对值排序
    for k in buckets:
        buckets[k].sort(key=lambda x: -abs(x["change_pct"]))

    counts = {k: len(v) for k, v in buckets.items()}
    return {"buckets": buckets, "counts": counts}
