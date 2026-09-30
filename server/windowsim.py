"""窗口胜率回测：模拟在开盘 09:30–09:50 窗口按 VWAP 买入、当天收盘结算，
统计一批股票的涨停率 / 上涨率 / 平均涨幅 / 累计盈亏。

纯统计模块，与虚拟盘资金、持仓完全隔离，不依赖 store，也不碰 T+1 / 手续费。

数据来源：
- 分钟线 ds.get_kline_intraday(code, '1m', count)：eltdx 源带真实 amount（元），
  故 VWAP = Σamount / (Σvolume×100) 为真实成交量加权均价；腾讯降级源 amount 为
  None 时降级为 Σclose×volume / Σvolume。
- 日线 ds.get_kline(code, '1d', count)：取同日收盘与前收（涨停价基准）。
- 涨停幅度复用 screener._limit_pct（主板 10% / 创业板 20% / 北交所 30%），ST 用 5%。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import datasource as ds
from screener import _is_st, _limit_pct

WIN_START = "09:30"
WIN_END = "09:50"
INTRADAY_MAX = 2400  # datasource 对 1m 的硬上限（约 10 个交易日）


def _vwap(bars: List[Dict[str, Any]]) -> Optional[float]:
    """窗口内成交量加权均价。优先用真实 amount；缺失则降级用 close 加权。"""
    tv = sum(float(b.get("volume") or 0) for b in bars)
    if tv <= 0:
        return None
    amts = [b.get("amount") for b in bars]
    if all(a is not None for a in amts):
        tot = sum(float(a) for a in amts)
        if tot > 0:
            return tot / (tv * 100.0)
    num = sum(float(b.get("close") or 0) * float(b.get("volume") or 0) for b in bars)
    return num / tv if tv > 0 else None


def _window_by_day(rows: List[Dict[str, Any]], days: int) -> List[List[Dict[str, Any]]]:
    """按交易日分组，返回最近 days 个交易日、且落在窗口内的 bar 列表。"""
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        d = (r.get("date") or "")[:10]
        hhmm = (r.get("date") or "")[11:16]
        if d and WIN_START <= hhmm <= WIN_END:
            by_day.setdefault(d, []).append(r)
    days_sorted = sorted(by_day.keys())
    return [by_day[d] for d in days_sorted[-days:]]


def _sim_one(code: str, name: str, days: int) -> Dict[str, Any]:
    """单只票统计：返回 {samples, limit_up_cnt, up_cnt, rets, skip_reason}。"""
    try:
        rows = ds.get_kline_intraday(code, "1m", min(days * 240, INTRADAY_MAX))
    except Exception:
        return {"samples": 0, "limit_up_cnt": 0, "up_cnt": 0, "rets": [],
                "skip_reason": "分钟线获取失败"}
    if not rows:
        return {"samples": 0, "limit_up_cnt": 0, "up_cnt": 0, "rets": [],
                "skip_reason": "无分钟线"}

    day_bars = _window_by_day(rows, days)
    if not day_bars:
        return {"samples": 0, "limit_up_cnt": 0, "up_cnt": 0, "rets": [],
                "skip_reason": "窗口无数据"}

    closes: Dict[str, float] = {}
    prev_close: Dict[str, float] = {}
    try:
        kl = ds.get_kline(code, "1d", days + 40)
        for i, b in enumerate(kl):
            dd = (b.get("date") or "")[:10]
            closes[dd] = float(b.get("close") or 0)
            if i > 0:
                prev_close[dd] = float(kl[i - 1].get("close") or 0)
    except Exception:
        return {"samples": 0, "limit_up_cnt": 0, "up_cnt": 0, "rets": [],
                "skip_reason": "日线获取失败"}

    limit = _limit_pct(code)
    samples = limit_up_cnt = up_cnt = 0
    rets: List[float] = []
    for bars in day_bars:
        d = (bars[0].get("date") or "")[:10]
        v = _vwap(bars)
        c = closes.get(d)
        pc = prev_close.get(d)
        if v is None or not c or not pc:
            continue
        lim = pc * 1.05 if _is_st(name) else pc * (1 + limit / 100.0)
        ret = (c - v) / v * 100.0
        samples += 1
        rets.append(ret)
        if c >= lim - 0.01:
            limit_up_cnt += 1
        if c > v:
            up_cnt += 1

    if samples == 0:
        return {"samples": 0, "limit_up_cnt": 0, "up_cnt": 0, "rets": [],
                "skip_reason": "无可用样本"}
    return {"samples": samples, "limit_up_cnt": limit_up_cnt,
            "up_cnt": up_cnt, "rets": rets, "skip_reason": ""}


def window_winrate(codes: List[Dict[str, Any]], amount_per: float = 10000.0,
                   days: int = 10, workers: int = 4) -> Dict[str, Any]:
    """对一批股票做窗口胜率统计。

    codes: [{code, name}, ...]；amount_per: 每只模拟投入（仅用于折算金额盈亏）。
    返回 items（逐只）+ summary（汇总）。
    """
    codes = [c for c in (codes or []) if c.get("code")]
    items: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_sim_one, c.get("code"), c.get("name", ""), days): c
                for c in codes}
        for f in futs:
            c = futs[f]
            try:
                r = f.result()
            except Exception as e:  # 单只异常不影响整体
                r = {"samples": 0, "limit_up_cnt": 0, "up_cnt": 0, "rets": [],
                     "skip_reason": f"异常:{e}"}
            samples = r["samples"]
            item = {
                "code": c.get("code"),
                "name": c.get("name", ""),
                "samples": samples,
                "limit_up_cnt": r["limit_up_cnt"],
                "up_cnt": r["up_cnt"],
                "limit_up_rate": round(r["limit_up_cnt"] / samples, 4) if samples else 0,
                "win_rate": round(r["up_cnt"] / samples, 4) if samples else 0,
                "avg_ret": round(sum(r["rets"]) / len(r["rets"]), 2) if r["rets"] else 0,
                "best": round(max(r["rets"]), 2) if r["rets"] else 0,
                "worst": round(min(r["rets"]), 2) if r["rets"] else 0,
                "total_pnl": round(amount_per * sum(r["rets"]) / 100.0, 2)
                            if r["rets"] else 0,
                "skip_reason": r["skip_reason"],
            }
            items.append(item)

    total_samples = sum(i["samples"] for i in items)
    total_limit = sum(i["limit_up_cnt"] for i in items)
    total_up = sum(i["up_cnt"] for i in items)
    weighted = sum(i["avg_ret"] * i["samples"] for i in items)
    summary = {
        "codes": len(items),
        "samples": total_samples,
        "limit_up_cnt": total_limit,
        "limit_up_rate": round(total_limit / total_samples, 4) if total_samples else 0,
        "up_cnt": total_up,
        "win_rate": round(total_up / total_samples, 4) if total_samples else 0,
        "avg_ret": round(weighted / total_samples, 2) if total_samples else 0,
        "total_pnl": round(amount_per * weighted / 100.0, 2) if total_samples else 0,
    }
    return {
        "days": days,
        "window": f"{WIN_START}-{WIN_END}",
        "amount_per": amount_per,
        "items": items,
        "summary": summary,
    }


if __name__ == "__main__":
    t0 = time.time()
    res = window_winrate([
        {"code": "sh600519", "name": "贵州茅台"},
        {"code": "sz300750", "name": "宁德时代"},
        {"code": "sz000001", "name": "平安银行"},
    ])
    print("耗时 %.1fs" % (time.time() - t0))
    import json
    print(json.dumps(res["summary"], ensure_ascii=False, indent=2))
    for it in res["items"]:
        print(it["code"], it["name"], "samples=%d" % it["samples"],
              "涨停率=%.0f%%" % (it["limit_up_rate"] * 100),
              "胜率=%.0f%%" % (it["win_rate"] * 100),
              "均涨=%.2f%%" % it["avg_ret"])


def since_added_perf(items: List[Dict[str, Any]], baseline_date: str,
                     amount_per: float = 10000.0) -> Dict[str, Any]:
    """入选后表现：以入选价（或入选日收盘兜底）为基准，对比最新价，统计涨跌幅/胜率。

    用于「历史选股」里回看某次选股从加入那天起到现在涨了还是跌了。
    与 window_winrate 的区别：这里是「持有至今」的实盘式回看，不模拟盘中成交。
    """
    codes = [i.get("code") for i in items if i.get("code")]
    if not codes:
        return {"baseline_date": baseline_date, "amount_per": amount_per,
                "items": [], "summary": _empty_perf_summary()}

    # 最新价（实时；休市时为最近收盘）
    try:
        quotes = ds.quote_tencent(codes) or {}
    except Exception:
        quotes = {}

    # 兜底基准：入选价缺失时，取入选日及之前最近一根日线收盘
    missing = [it for it in items if not (it.get("price") and float(it.get("price") or 0) > 0)]
    daily_close: Dict[str, float] = {}
    if missing:
        try:
            for it in missing:
                c = it.get("code")
                if c in daily_close:
                    continue
                kl = ds.get_kline(c, "1d", 260) or []
                best = None
                for b in kl:
                    d = (b.get("date") or "")[:10]
                    if d <= baseline_date:
                        best = b
                    elif d > baseline_date:
                        break
                if best:
                    daily_close[c] = float(best.get("close") or 0)
        except Exception:
            daily_close = {}

    out = []
    for it in items:
        code = it.get("code")
        name = it.get("name", "")
        base = it.get("price")
        if not (base and float(base or 0) > 0):
            base = daily_close.get(code)
        q = quotes.get(code) or {}
        cur = q.get("price") or q.get("close")
        if base and cur and float(base) > 0:
            b = float(base)
            c = float(cur)
            chg = (c - b) / b * 100.0
            out.append({"code": code, "name": name, "base": round(b, 2),
                        "cur": round(c, 2), "chg": round(chg, 2),
                        "up": chg > 0, "pnl": round(amount_per * chg / 100.0, 2)})
        else:
            out.append({"code": code, "name": name, "base": (round(float(base), 2) if base else None),
                        "cur": (round(float(cur), 2) if cur else None),
                        "chg": None, "up": None, "pnl": None})

    valid = [r for r in out if r["chg"] is not None]
    chgs = [r["chg"] for r in valid]
    up_cnt = sum(1 for r in valid if r["up"])
    return {
        "baseline_date": baseline_date,
        "amount_per": amount_per,
        "items": out,
        "summary": {
            "total": len(out),
            "valid": len(valid),
            "up_cnt": up_cnt,
            "win_rate": round(up_cnt / len(valid), 4) if valid else 0,
            "avg_chg": round(sum(chgs) / len(chgs), 2) if chgs else 0,
            "best": round(max(chgs), 2) if chgs else 0,
            "worst": round(min(chgs), 2) if chgs else 0,
            "total_pnl": round(amount_per * sum(chgs) / 100.0, 2) if chgs else 0,
        },
    }


def _empty_perf_summary() -> Dict[str, Any]:
    return {"total": 0, "valid": 0, "up_cnt": 0, "win_rate": 0,
            "avg_chg": 0, "best": 0, "worst": 0, "total_pnl": 0}
