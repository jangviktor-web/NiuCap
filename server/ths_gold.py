"""同花顺黄金数据蒸馏：宏观风向 + 黄金行情。

两个入口都带 TTL 内存缓存、失败静默降级返回 {}，由前端决定展示/隐藏，
不在请求链路里抛异常（面板其它卡片不应被黄金数据拖垮）。

macro_snapshot() —— 宏观风向（日更，T-1）：
  同花顺黄金数据中心 external API（GET，头 X-Token），4 指标：
    美联储降息概率 / 10Y 美债实际利率 / 10Y-2Y 期限利差 / 恐慌指数。
  指标序列为**倒序**（time[0]/value[0] 为最新一期）；
  降息概率取「距今最近一场会议」的区间概率（按概率降序取前二）。

gold_quotes() —— 黄金行情（盘中）：
  复刻自 skill「ths-gold-market-quote」的 fetch_quotes.py：
  POST 10jqka fuyao 报价聚合接口，X-Fuyao-Auth 可用环境变量
  THS_GOLD_QUOTE_AUTH 覆盖默认 JWT。6 品种：伦敦金现 / 广发·民生·浙商
  积存金 / AU9999 / AUTD。实测批量请求只返回首个 code，故按品种逐个
  并发请求（与 skill 脚本一致）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

# ===========================================================================
# 宏观风向（同花顺黄金数据中心 external API）
# ===========================================================================

MACRO_BASE = "http://jcj.thsi.cn/jcj_data_detail"
MACRO_TOKEN = os.environ.get("THS_GOLD_CENTER_TOKEN", "10jqka")
# key -> (接口路径后缀, 面板展示名, 主序列名关键词)
MACRO_SERIES = {
    "fed_rate": ("fed_interest_rate", "美联储降息概率", None),
    "real_yield": ("real_yield", "10Y 美债实际利率", "tenYearUsRealYieldCurve"),
    "term_spread": ("term_spread", "10Y-2Y 期限利差", "termSpread"),
    "fear_index": ("fear_index", "恐慌指数(VIX)", "fearIndex"),
}
_MACRO_TTL = 300.0  # 日更数据，5 分钟缓存足够
_macro_cache: dict[str, Any] = {"ts": 0.0, "data": {}}
_macro_lock = threading.Lock()


def _macro_get(path: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(
        MACRO_BASE + path,
        headers={"Accept": "application/json", "X-Token": MACRO_TOKEN},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _latest_pair(metrics_data: dict) -> tuple[str | None, float | None]:
    """time/value 倒序配对，取最新一期；长度不一致只取共同长度。"""
    times = metrics_data.get("time") or []
    values = metrics_data.get("value") or []
    n = min(len(times), len(values))
    for i in range(n):
        try:
            return str(times[i]), float(values[i])
        except (TypeError, ValueError):
            continue
    return None, None


def _parse_fed_rate(data: Any) -> dict | None:
    """data.list[] → 距今最近一场会议的区间概率前二。"""
    meetings = (data or {}).get("list") or []
    if not meetings:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    upcoming = [m for m in meetings if str(m.get("meeting_date") or "")[:10] >= today]
    meeting = upcoming[0] if upcoming else meetings[-1]
    items = [
        it for it in (meeting.get("items") or [])
        if it.get("probability") is not None
    ]
    if not items:
        return None
    items.sort(key=lambda it: float(it["probability"]), reverse=True)
    top, second = items[0], (items[1] if len(items) > 1 else None)
    out = {
        "meeting": str(meeting.get("meeting_date") or "")[:10],
        "top_range": str(top.get("rate_range") or ""),
        "top_prob": round(float(top["probability"]) * 100, 1),
        "is_in_range": bool(top.get("is_in_range")),
    }
    if second is not None:
        out["second_range"] = str(second.get("rate_range") or "")
        out["second_prob"] = round(float(second["probability"]) * 100, 1)
    return out


def _parse_metric(data: Any, name_kw: str | None) -> dict | None:
    """data.metrics[] → 主序列最新值/上期值；顺带带回 XAUUSD 对照价。"""
    metrics = (data or {}).get("metrics") or []
    main = None
    gold = None
    for m in metrics:
        name = str(m.get("metrics_name") or "")
        if name == "XAUUSD":
            gold = m
        elif name_kw is None or name_kw in name:
            main = m
    if main is None and metrics:
        main = metrics[0]
    if main is None:
        return None
    md = main.get("metrics_data") or {}
    times = md.get("time") or []
    values = md.get("value") or []
    n = min(len(times), len(values))
    if n < 1:
        return None
    out: dict[str, Any] = {
        "name": str(main.get("metrics_name") or ""),
        "date": str(times[0]),
        "value": None,
        "prev": None,
    }
    try:
        out["value"] = round(float(values[0]), 4)
    except (TypeError, ValueError):
        pass
    if n > 1:
        try:
            out["prev"] = round(float(values[1]), 4)
        except (TypeError, ValueError):
            pass
    if gold is not None:
        gd = gold.get("metrics_data") or {}
        gt, gv = gd.get("time") or [], gd.get("value") or []
        if gt and gv:
            try:
                out["gold"] = round(float(gv[0]), 2)
                out["gold_date"] = str(gt[0])
            except (TypeError, ValueError):
                pass
    return out


def macro_snapshot(force: bool = False) -> dict:
    """宏观风向 4 指标。任一接口失败只丢该项；全部失败返回 {}。"""
    now = time.time()
    with _macro_lock:
        if not force and _macro_cache["data"] and now - _macro_cache["ts"] < _MACRO_TTL:
            return _macro_cache["data"]

    out: dict[str, Any] = {"ok": True}
    for key, (path, _label, name_kw) in MACRO_SERIES.items():
        try:
            resp = _macro_get("/external/api/gold_center/ifind/v1/" + path)
            data = (resp or {}).get("data") or {}
            if key == "fed_rate":
                parsed = _parse_fed_rate(data)
            else:
                parsed = _parse_metric(data, name_kw)
            if parsed:
                out[key] = parsed
        except Exception as e:  # 单指标失败不影响其它
            print(f"[ths-gold] macro {key} 取数失败：{e}")
    if len(out) <= 1:
        return {}
    with _macro_lock:
        _macro_cache["ts"] = time.time()
        _macro_cache["data"] = out
    return out


# ===========================================================================
# 黄金行情（复刻自 skill ths-gold-market-quote / fetch_quotes.py）
# ===========================================================================

GOLD_ENDPOINT = "https://quota-h.10jqka.com.cn/fuyao/common_hq_aggr/quote/v1/single_trend"
GOLD_DEFAULT_AUTH = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
    "eyJhdXRob3JpemVyX25hbWVzcGFjZSI6ImNvbW1vbi1ocS1hZ2dyIiwibGljZW5zZWVfdHlwZSI6"
    "IkZST05UX0FQUCIsImxpY2Vuc2VlX25hbWVzcGFjZSI6Imh4a2xpbmUtR01UX2FpQnV5R29sZF9Q"
    "YWdlIn0.gAC_QltNrfp6TXxZmW5ARTWfogjcO4qpVffmWskBeYw"
)
GOLD_FIELDS = ["10", "1", "6", "7", "8", "9", "13", "11", "19", "199112", "264648", "1968584"]
GOLD_PRODUCTS = {
    "伦敦金现": ("218", "AUUSDO"),
    "广发积存金": ("UAGM", "GF001"),
    "民生积存金": ("UAGM", "MS001"),
    "浙商积存金": ("UAGM", "ZS001"),
    "AU9999": ("81", "AU9999"),
    "AUTD": ("81", "AUTD"),
}
GOLD_FIELD_NAMES = {
    "10": "最新价",
    "1": "行情时间",
    "6": "昨收价",
    "7": "开盘价",
    "8": "最高价",
    "9": "最低价",
    "13": "成交量",
    "11": "收盘价",
    "19": "成交额",
    "199112": "涨跌幅",
    "264648": "涨跌额",
    "1968584": "换手率",
}
_GOLD_TTL = 60.0
_gold_cache: dict[str, Any] = {"ts": 0.0, "data": {}}
_gold_lock = threading.Lock()


def _gold_auth() -> str:
    return os.environ.get("THS_GOLD_QUOTE_AUTH", GOLD_DEFAULT_AUTH).strip()


def _gold_payload(product: str) -> dict[str, Any]:
    market, code = GOLD_PRODUCTS[product]
    return {
        "code_list": [{"market": market, "codes": [code.upper()]}],
        "trade_class": "intraday",
        "trade_date": 0,
        "data_fields": GOLD_FIELDS,
    }


def _gold_fetch(payload: dict[str, Any], timeout: float = 15.0) -> Any:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        GOLD_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json", "X-Fuyao-Auth": _gold_auth()},
        method="POST",
    )
    last_error: Exception | None = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError) as exc:  # URLError/HTTPError 均为 OSError 子类
            last_error = exc
    raise last_error or RuntimeError("gold quote unavailable")


def _gold_to_local_time(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d%H%M%S", "%Y%m%d"):
            try:
                return datetime.strptime(
                    text[:19] if fmt != "%Y%m%d" else text[:8], fmt
                ).strftime("%Y-%m-%d %H:%M:%S" if fmt != "%Y%m%d" else "%Y-%m-%d")
            except ValueError:
                continue
        if text.isdigit():
            value = int(text)
        else:
            return text
    if not isinstance(value, (int, float)):
        return None
    if value > 10_000_000_000_000:
        value = value / 1000
    digits = str(int(value))
    if len(digits) == 14:
        try:
            return datetime.strptime(digits, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if len(digits) == 8 and digits.startswith("20"):
        try:
            return datetime.strptime(digits, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    timestamp = value / 1000 if value > 10_000_000_000 else value
    try:
        return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None


def _gold_rows(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    return []


def _gold_rows_from_response(response: Any) -> list[dict]:
    if not isinstance(response, dict):
        return []
    for key in ("quote_data", "data", "quotes", "list", "items"):
        nested = response.get(key)
        rows = _gold_rows(nested)
        if rows:
            return rows
        if isinstance(nested, dict):
            inner = _gold_rows_from_response(nested)
            if inner:
                return inner
            synthesized = _gold_rows_from_market_map(nested)
            if synthesized:
                return synthesized
    return _gold_rows_from_market_map(response)


def _gold_rows_from_market_map(payload: dict) -> list[dict]:
    rows: list[dict] = []
    for name, (market, code) in GOLD_PRODUCTS.items():
        bucket = payload.get(market)
        if not isinstance(bucket, dict):
            continue
        item = bucket.get(code) or bucket.get(code.lower()) or bucket.get(code.upper())
        if isinstance(item, dict):
            row = dict(item)
            row.setdefault("market", market)
            row.setdefault("code", code)
            rows.append(row)
        elif isinstance(item, list):
            rows.append({"market": market, "code": code, "value": item})
    return rows


def _gold_latest_point(points: Any) -> Any:
    if not isinstance(points, list) or not points:
        return None
    for item in reversed(points):
        if isinstance(item, list) and len(item) > 1 and item[1] is not None:
            return item
        if isinstance(item, dict) and any(item.get(k) is not None for k in ("10", "最新价")):
            return item
    return None


def _gold_product_name(market: str, code: str) -> str | None:
    return next(
        (
            name
            for name, pair in GOLD_PRODUCTS.items()
            if pair[0] == market and pair[1].casefold() == code.casefold()
        ),
        None,
    )


def _gold_as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("%", "").replace(",", "").strip())
        except ValueError:
            return None
    return None


def _gold_price_decimals(*values: float) -> int:
    decimals = 2
    for value in values:
        text = f"{value:.8f}".rstrip("0").rstrip(".")
        if "." in text:
            decimals = max(decimals, min(len(text.split(".")[1]), 4))
    return decimals


def _gold_fill_prev_and_change(result: dict, row: dict) -> None:
    last = _gold_as_float(result.get("最新价"))
    prev = _gold_as_float(result.get("昨收价"))
    if prev is None:
        prev = _gold_as_float(row.get("base_price"))
        if prev is not None:
            result["昨收价"] = prev
    if last is None or prev is None or prev == 0:
        return
    decimals = _gold_price_decimals(last, prev)
    if "涨跌额" not in result:
        result["涨跌额"] = round(last - prev, decimals)
    if "涨跌幅" not in result:
        result["涨跌幅"] = round((last - prev) / prev * 100, 2)


def _gold_business_row(row: dict) -> dict | None:
    market = str(row.get("market", ""))
    code = str(row.get("code", row.get("stock_code", "")))
    product = _gold_product_name(market, code)
    if product is None:
        return None

    values = row.get("data") or row.get("latest") or row.get("quote")
    if values is None:
        values = _gold_latest_point(row.get("value"))
    if values is None:
        values = row
    if isinstance(values, list):
        field_ids = row.get("data_fields") or GOLD_FIELDS
        values = dict(zip(field_ids, values))
    if not isinstance(values, dict):
        return {"品种": product, "状态": "当前未返回行情"}

    result: dict[str, Any] = {"品种": product}
    for field_id, display_name in GOLD_FIELD_NAMES.items():
        value = values.get(field_id)
        if value is None:
            value = values.get(display_name)
        if value is None:
            continue
        result[display_name] = (
            _gold_to_local_time(value) if field_id == "1" else value
        )
    if "最新价" not in result:
        return {"品种": product, "状态": "当前未返回行情"}
    _gold_fill_prev_and_change(result, row)
    return result


def gold_quotes(force: bool = False) -> dict:
    """黄金行情 6 品种。60s 缓存；部分失败返回已有行；全部失败返回 {}。"""
    now = time.time()
    with _gold_lock:
        if not force and _gold_cache["data"] and now - _gold_cache["ts"] < _GOLD_TTL:
            return _gold_cache["data"]

    merged: list[dict] = []
    last_error: Exception | None = None
    products = list(GOLD_PRODUCTS)
    with ThreadPoolExecutor(max_workers=min(6, len(products))) as pool:
        futures = [pool.submit(_gold_fetch, _gold_payload(name)) for name in products]
        for future in as_completed(futures):
            try:
                merged.extend(_gold_rows_from_response(future.result()))
            except Exception as exc:
                last_error = exc
    if not merged:
        if last_error is not None:
            print(f"[ths-gold] gold_quotes 取数失败：{last_error}")
        return {}

    records = [r for r in (_gold_business_row(row) for row in merged) if r]
    by_name = {r["品种"]: r for r in records if "最新价" in r}
    normalized = [
        by_name.get(name, {"品种": name, "状态": "当前未返回行情"})
        for name in products
    ]
    out = {"ok": True, "quotes": normalized, "fetched_at": datetime.now().strftime("%H:%M:%S")}
    with _gold_lock:
        _gold_cache["ts"] = time.time()
        _gold_cache["data"] = out
    return out
