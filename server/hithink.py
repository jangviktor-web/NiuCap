"""同花顺（hithink-finance）特色数据层。

通过 REST 调用 https://fuyao.aicubes.cn，认证头 X-api-key。
凭证读取顺序：环境变量 HITHINK_FINANCE_API_KEY → 用户级 credentials.env。
凭证绝不写入代码、日志或前端响应。

覆盖特色数据：人气热榜、涨停池、连板天梯、炸板池、跌停池、龙虎榜、异动分析。
这些接口多为 today-only 能力，非交易日或盘前会返回空列表，属正常行为，
调用方须优雅降级而不是报错。
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

BASE_URL = "https://fuyao.aicubes.cn"

_CRED_PATHS = [
    os.path.expanduser("~/.config/hithink-finance/credentials.env"),
    os.path.expanduser("~/Library/Application Support/hithink-finance/credentials.env"),
]


def _load_key() -> Optional[str]:
    """按约定顺序查找统一 API Key。"""
    key = (os.environ.get("HITHINK_FINANCE_API_KEY") or "").strip()
    if key:
        return key
    for name in ("FUYAO_TOKEN", "API_KEY"):
        key = (os.environ.get(name) or "").strip()
        if key:
            return key
    for path in _CRED_PATHS:
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() in ("HITHINK_FINANCE_API_KEY", "FUYAO_TOKEN", "API_KEY"):
                        v = v.strip().strip('"').strip("'")
                        if v:
                            return v
        except Exception:
            continue
    return None


_KEY = _load_key()
API_AVAILABLE = _KEY is not None


def refresh_status() -> bool:
    """重新探测凭证可用性（支持运行期注入环境变量后再加载）。"""
    global _KEY, API_AVAILABLE
    if _KEY is None:
        _KEY = _load_key()
        API_AVAILABLE = _KEY is not None
    return _KEY is not None


# ---------------------------------------------------------------- 请求


def _get(path: str, params: Optional[Dict[str, Any]] = None,
         timeout: int = 15) -> Optional[Dict[str, Any]]:
    """发起 GET 请求，返回 data 字段；失败返回 None。"""
    key = _KEY or _load_key()          # 支持运行期注入环境变量后再懒加载
    if not key:
        return None
    url = BASE_URL + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    req = urllib.request.Request(url, headers={
        "X-api-key": key,
        "Accept": "application/json",
        "User-Agent": "tick-stock-panel/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("code") != 0:
        return None
    return payload.get("data")


_CACHE: Dict[str, Any] = {}
_TTL = {"realtime": 60, "daily": 1800}


def _cached(path: str, params: Optional[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    key = path + "|" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
    hit = _CACHE.get(key)
    ttl = _TTL.get(kind, 300)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    data = _get(path, params)
    if data is None:
        return None
    _CACHE[key] = (time.time(), data)
    return data


def _f(v: Any, default: float = 0.0) -> float:
    try:
        s = str(v).strip().replace(",", "").replace("%", "")
        if s in ("", "--", "null", "None", "-"):
            return default
        return float(s)
    except Exception:
        return default


def _i(v: Any, default: int = 0) -> int:
    return int(_f(v, default))


# ---------------------------------------------------------------- 人气热榜


def hot_rank(limit: int = 30) -> List[Dict[str, Any]]:
    """人气热榜（今日人气排名与热度值）。"""
    data = _cached("/api/a-share/special-data/hot-stock-list", None, "realtime")
    if not data:
        return []
    items = []
    for r in (data.get("item") or [])[:limit]:
        code = r.get("thscode") or ""
        items.append({
            "rank": _i(r.get("rank")),
            "code": code,
            "ticker": r.get("ticker", ""),
            "name": (r.get("name") or "").strip(),
            "heat": _f(r.get("heat")),
            "rank_change": _i(r.get("rank_change")),
            "rank_trend": r.get("rank_trend", "flat"),
        })
    return items


# ---------------------------------------------------------------- 涨停 / 跌停 / 炸板 / 连板


def _pool(path: str, page_size: int = 60) -> Dict[str, Any]:
    """通用股票池接口：返回 {total, items}。

    上游字段实测（2026-09 核对）：
      涨停池 last_price / price_change_ratio_pct / limit_up_time /
             limit_up_reason / continue_day_text / continue_day_cnt /
             seal_money / max_seal_money / is_st / is_new
      炸板池 last_price / price_change_ratio_pct / open_times /
             turnover_ratio_pct / turnover
    历史字段名（change_pct/latest/amount）在上游已不存在，
    保留 or 兜底仅为兼容旧响应形状。
    """
    data = _cached(path, {"page": 1, "size": page_size}, "realtime")
    if not data:
        return {"total": 0, "items": []}
    pag = data.get("pagination") or {}
    total = _i(pag.get("total"))
    items = []
    for r in (data.get("item") or []):
        cnt = _i(r.get("continue_day_cnt"))
        items.append({
            "code": r.get("thscode") or "",
            "ticker": r.get("ticker", ""),
            "name": (r.get("name") or "").strip(),
            # 涨跌幅：上游 percentage 字段，历史兜底保留
            "change_pct": _f(r.get("price_change_ratio_pct")
                             or r.get("change_pct") or r.get("changePct")),
            # 价格：上游 last_price
            "price": _f(r.get("last_price") or r.get("latest")
                        or r.get("price") or r.get("close")),
            "turnover_rate": _f(r.get("turnover_ratio_pct")
                                or r.get("turnover_rate") or r.get("turnoverRate")),
            "amount": _f(r.get("turnover") or r.get("amount")),
            # 涨停池专属
            "limit_up_time": (r.get("limit_up_time") or "").strip(),
            "continue_day_cnt": cnt,
            "continue_day_text": (r.get("continue_day_text") or "").strip(),
            "seal_money": _f(r.get("seal_money")),
            "max_seal_money": _f(r.get("max_seal_money")),
            "open_times": _i(r.get("open_times")),      # 炸板池
            "is_st": bool(r.get("is_st")),
            "is_new": bool(r.get("is_new")),
            "reason": (r.get("limit_up_reason") or r.get("reason")
                       or r.get("tag_name") or "").strip(),
            "raw": r,
        })
    return {"total": total, "items": items}


def limit_up_pool() -> Dict[str, Any]:
    """涨停股票池。"""
    return _pool("/api/a-share/special-data/limit-up-pool")


def limit_down_pool() -> Dict[str, Any]:
    """跌停股票池。"""
    return _pool("/api/a-share/special-data/limit-down-pool")


def limit_break_pool() -> Dict[str, Any]:
    """炸板股票池。"""
    return _pool("/api/a-share/special-data/limit-break-pool")


_BOARD_KEYS = [
    ("seven_over", "7板以上", 7),
    ("six_board", "6板", 6),
    ("five_board", "5板", 5),
    ("four_board", "4板", 4),
    ("three_board", "3板", 3),
    ("two_board", "2板", 2),
]


def limit_up_ladder() -> Dict[str, Any]:
    """连板天梯（按连板高度分层）。

    上游结构实测（2026-09 核对）：
      data.item = [ {date: "2026-09-22", boards: {
                       two_board:[{thscode,ticker,name,board_num,...}], ...}}, ... ]
    item 按日期倒序排列，item[0] 为最新交易日；每日的 boards 是按板数分组的字典。
    历史实现误把 item 当平铺记录读，导致 code/name 全空 —— 此处按真实结构解包。
    """
    data = _cached("/api/a-share/special-data/limit-up-ladder", None, "realtime")
    if not data:
        return {"total": 0, "levels": []}
    raw_items = data.get("item") or []
    if not raw_items:
        return {"total": 0, "levels": []}

    latest = raw_items[0]                     # 最新交易日
    date = (latest.get("date") or "").strip()
    boards = latest.get("boards") or {}
    levels = []
    total = 0
    for key, label, days in _BOARD_KEYS:      # 从高到低输出
        stocks = []
        for r in (boards.get(key) or []):
            stocks.append({
                "code": r.get("thscode") or "",
                "ticker": r.get("ticker", ""),
                "name": (r.get("name") or "").strip(),
                "days": _i(r.get("board_num")) or days,
            })
        total += len(stocks)
        if stocks:
            levels.append({"days": days, "label": label, "stocks": stocks})

    # 兜底：上游若仍返回平铺记录（旧形状），按 continue/board_num 归组
    if not levels and raw_items and not raw_items[0].get("boards"):
        buckets: Dict[int, List[Dict[str, Any]]] = {}
        for r in raw_items:
            d = _i(r.get("board_num") or r.get("limit_up_days")
                   or r.get("continue_day_cnt") or r.get("days") or 1)
            buckets.setdefault(d, []).append({
                "code": r.get("thscode") or "",
                "ticker": r.get("ticker", ""),
                "name": (r.get("name") or "").strip(),
                "days": d,
            })
        for d in sorted(buckets.keys(), reverse=True):
            total += len(buckets[d])
            levels.append({"days": d, "label": f"{d}板", "stocks": buckets[d]})

    return {"total": total, "date": date, "levels": levels}


# ---------------------------------------------------------------- 龙虎榜 / 异动


def dragon_tiger() -> Dict[str, Any]:
    """同花顺龙虎榜。

    上游结构实测（2026-09 核对）：
      data = {trade_date, board_type, count, stock_count,
              stock_items:[{thscode,ticker,name,concept_list,change(小数),
                            net_value,net_rate,hot_rank,buy_value,sell_value,
                            limit_reason,range_days,hot_money_net_value}],
              hot_money_items:[...]}
    注意键名是 stock_items 而非 item，且 change 是小数（0.09986 → 9.99%）。
    """
    data = _cached("/api/a-share/special-data/dragon-tiger-list",
                   {"page": 1, "size": 60}, "realtime")
    if not data:
        return {"total": 0, "items": [], "date": ""}
    rows = data.get("stock_items") or data.get("item") or []
    items = []
    for r in rows:
        chg = _f(r.get("change"))                # 小数口径
        if abs(chg) <= 1.0 and not r.get("change_pct"):
            chg *= 100                           # 0.09986 → 9.986
        concepts = r.get("concept_list") or []
        items.append({
            "code": r.get("thscode", ""),
            "ticker": r.get("ticker", ""),
            "name": (r.get("name") or "").strip(),
            "change_pct": round(chg, 2),
            "net_buy": _f(r.get("net_value")),
            "net_rate": _f(r.get("net_rate")) * 100,   # 0.0091 → 0.91%
            "buy": _f(r.get("buy_value")),
            "sell": _f(r.get("sell_value")),
            "hot_rank": _i(r.get("hot_rank")),
            "concepts": [c.get("name", "") for c in concepts if c.get("name")],
            "reason": (r.get("limit_reason") or r.get("reason")
                       or r.get("explain") or "").strip(),
        })
    total = _i(data.get("stock_count")) or len(items)
    return {"total": total, "items": items,
            "date": (data.get("trade_date") or "").strip()}


def anomaly_list(limit: int = 40) -> List[Dict[str, Any]]:
    """个股异动原因列表（今日）。

    上游字段实测：stock_name / analysis_content / keyword_list / thscode / tag_name。
    历史实现读 name 导致股票名恒为空；且上游无 change_pct，改由 tag 表达方向。
    """
    data = _cached("/api/a-share/special-data/anomaly-analysis-list",
                   {"page": 1, "size": limit}, "realtime")
    if not data:
        return []
    items = []
    for r in (data.get("item") or [])[:limit]:
        items.append({
            "code": r.get("thscode", ""),
            "ticker": r.get("ticker", ""),
            "name": (r.get("stock_name") or r.get("name") or "").strip(),
            "tag": (r.get("tag_name") or "").strip(),
            "content": (r.get("analysis_content") or "").strip(),
            "keywords": [k for k in (r.get("keyword_list") or []) if k],
        })
    return items


def anomaly_stock(thscodes: List[str]) -> List[Dict[str, Any]]:
    """按股票代码批量查询异动原因。"""
    if not thscodes:
        return []
    data = _cached("/api/a-share/special-data/anomaly-analysis-stock",
                   {"thscodes": ",".join(thscodes)}, "realtime")
    if not data:
        return []
    items = []
    for r in (data.get("item") or []):
        items.append({
            "code": r.get("thscode", ""),
            "ticker": r.get("ticker", ""),
            "name": (r.get("stock_name") or r.get("name") or "").strip(),
            "tag": (r.get("tag_name") or "").strip(),
            "content": (r.get("analysis_content") or "").strip(),
            "keywords": [k for k in (r.get("keyword_list") or []) if k],
        })
    return items


# ---------------------------------------------------------------- 代码转换


def to_thscode(code: str) -> str:
    """sh600519 → 600519.SH；sz000858 → 000858.SZ；bj430047 → 430047.BJ。"""
    c = (code or "").strip().lower()
    if len(c) == 8 and c[:2] in ("sh", "sz", "bj"):
        suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[c[:2]]
        return f"{c[2:]}.{suffix}"
    return code


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    print("API_AVAILABLE:", API_AVAILABLE)
    hr = hot_rank(5)
    print("hot_rank:", [(x["rank"], x["name"], x["heat"]) for x in hr])
    lu = limit_up_pool()
    print("limit_up total:", lu.get("total"))
    if lu["items"]:
        s = lu["items"][0]
        print("  sample:", s["code"], s["name"], s["price"], s["change_pct"],
              s["continue_day_text"], s["reason"][:20])
        assert s["price"] > 0 and s["change_pct"] != 0, "涨停池价格/涨幅仍为 0"
    la = limit_up_ladder()
    print("ladder total:", la.get("total"), "date:", la.get("date"),
          "levels:", [(l["label"], len(l["stocks"])) for l in la["levels"]])
    if la["levels"]:
        top = la["levels"][0]["stocks"][0]
        assert top["code"] and top["name"], "天梯个股 code/name 为空"
    dt = dragon_tiger()
    print("dragon_tiger:", dt.get("total"), dt.get("date"))
    if dt["items"]:
        s = dt["items"][0]
        print("  sample:", s["code"], s["name"], s["change_pct"], s["net_buy"])
    an = anomaly_list(3)
    print("anomaly:", len(an))
    if an:
        assert an[0]["name"], "异动股票名为空"
        print("  sample:", an[0]["code"], an[0]["name"], an[0]["tag"])
    print("to_thscode:", to_thscode("sh600519"), to_thscode("sz000858"))
    print("OK")
