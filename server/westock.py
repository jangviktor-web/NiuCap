"""WeStock CLI 数据源封装。

本模块把 `westock` 命令行返回的结构化 Markdown 表格解析为 Python 结构，
为 Tick 选股面板补充资金面、筹码、基本面、研报公告与市场热点等维度。

设计要点：
- CLI 位于 /root/.local/bin/westock（可能不在 PATH 中，需显式拼接）；
- CLI 输出为 Markdown 表格，含表头与分隔行，需解析为 list[dict]；
- 所有取数失败一律降级为 None/[]，绝不影响主页面可用性；
- 结果带 TTL 缓存，避免高频调用拖慢页面。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------- CLI 定位

_EXTRA_PATH = "/root/.local/bin"


def _find_cli() -> Optional[str]:
    """定位 westock 可执行文件，优先 PATH，其次常见安装位置。"""
    hit = shutil.which("westock")
    if hit:
        return hit
    cand = os.path.join(_EXTRA_PATH, "westock")
    if os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    return None


_CLI = _find_cli()
CLI_AVAILABLE = _CLI is not None


def get_cli() -> Optional[str]:
    """返回 CLI 路径，支持运行期安装后懒加载。"""
    global _CLI, CLI_AVAILABLE
    if _CLI is None:
        _CLI = _find_cli()
        CLI_AVAILABLE = _CLI is not None
    return _CLI


def refresh_status() -> bool:
    """重新探测 CLI 可用性。"""
    return get_cli() is not None

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------- 表格解析


def _clean(text: str) -> str:
    """去掉 ANSI 颜色码与首尾空白。"""
    return _ANSI_RE.sub("", text or "").strip()


def _split_row(line: str) -> List[str]:
    """把 `| a | b | c |` 切成 ['a', 'b', 'c']。"""
    raw = _clean(line)
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [c.strip() for c in raw.split("|")]


def _is_sep(line: str) -> bool:
    """判断是否为 Markdown 表格分隔行，如 |---|---|。"""
    cells = _split_row(line)
    if not cells:
        return False
    return all(set(c) <= set("-: ") and c for c in cells)


def parse_table(output: str) -> List[Dict[str, str]]:
    """从 CLI 输出中解析第一个 Markdown 表格为 list[dict]。

    westock 常在一次输出中打印多个表格（如 finance 的三大表），
    本函数只取第一个；需要全部表格时用 parse_tables()。
    """
    tables = parse_tables(output)
    return tables[0] if tables else []


def parse_tables(output: str, with_title: bool = False) -> List[Any]:
    """解析输出中所有 Markdown 表格，返回 list[list[dict]]。

    with_title=True 时返回 [{'title': '**利润表**', 'rows': [...]}] 结构，
    便于区分 finance 命令的三大报表。
    """
    lines = (_ANSI_RE.sub("", output or "")).splitlines()
    tables: List[Any] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # 表格需以 | 开头，且下一行是分隔行
        if line.strip().startswith("|") and i + 1 < n and _is_sep(lines[i + 1]):
            header = _split_row(line)
            rows: List[Dict[str, str]] = []
            j = i + 2
            while j < n and lines[j].strip().startswith("|"):
                cells = _split_row(lines[j])
                if len(cells) >= len(header):
                    rows.append({header[k]: cells[k] for k in range(len(header))})
                j += 1
            # 向上找最近的非空标题行
            title = ""
            k = i - 1
            while k >= 0:
                t = _clean(lines[k])
                if t:
                    title = t
                    break
                k -= 1
            if with_title:
                title_clean = re.sub(r"[*#>\s]", "", title)
                tables.append({"title": title_clean, "rows": rows})
            else:
                tables.append(rows)
            i = j
        else:
            i += 1
    return tables


def parse_target(output: str, code: str) -> Dict[str, str]:
    """在解析结果中挑出指定代码所在行（批量查询时用）。"""
    rows = parse_table(output)
    for r in rows:
        for key in ("code", "symbol", "SecuCode"):
            if r.get(key) == code:
                return r
    return rows[0] if rows else {}


def parse_quote_text(output: str) -> Dict[str, Any]:
    """解析 CLI 输出中的散文式提示，如"两市成交额：20771.00亿（较上日 +2539.66亿）"。"""
    text = _clean(_ANSI_RE.sub("", output or ""))
    out: Dict[str, Any] = {}
    m = re.search(r"两市成交额[：:]\s*([\d.]+)\s*亿(.*)", text)
    if m:
        out["turnover_yi"] = float(m.group(1))
        tail = m.group(2)
        mm = re.search(r"较上日\s*([+-]?[\d.]+)\s*亿", tail)
        if mm:
            out["turnover_delta_yi"] = float(mm.group(1))
    m = re.search(r"上涨家数占比全市场\s*(\d+)%", text)
    if m:
        out["up_ratio_pct"] = int(m.group(1))
    return out


# ---------------------------------------------------------------- 执行与缓存

_CACHE: Dict[str, Any] = {}
_CACHE_TTL = {
    "realtime": 60,     # 实时类：资金流、榜单
    "daily": 1800,      # 日频类：财务、研报、公告、筹码
    "static": 86400,    # 静态类：简况
}


def _cache_get(key: str, ttl: int):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(key: str, value):
    _CACHE[key] = (time.time(), value)


def run_cli(args: List[str], timeout: int = 25) -> Optional[str]:
    """执行 westock 子命令，返回 stdout；失败返回 None。"""
    cli = get_cli()
    if not cli:
        return None
    env = dict(os.environ)
    # CLI 可能依赖 PATH 中的自身位置，补齐
    if _EXTRA_PATH not in env.get("PATH", ""):
        env["PATH"] = env.get("PATH", "") + os.pathsep + _EXTRA_PATH
    try:
        proc = subprocess.run(
            [cli] + args,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _query(args: List[str], cache_key: str, kind: str = "realtime") -> Optional[str]:
    """带缓存的取数。"""
    ttl = _CACHE_TTL.get(kind, 300)
    hit = _cache_get(cache_key, ttl)
    if hit is not None:
        return hit
    out = run_cli(args)
    if out is None:
        return None
    _cache_put(cache_key, out)
    return out


# ---------------------------------------------------------------- 数值转换


def _f(v: Any, default: float = 0.0) -> float:
    try:
        s = str(v).strip().replace(",", "")
        if s in ("", "--", "null", "None", "-"):
            return default
        return float(s)
    except Exception:
        return default


def _i(v: Any, default: int = 0) -> int:
    return int(_f(v, default))


def _yi(v: Any) -> float:
    """元 → 亿元。"""
    return round(_f(v) / 1e8, 4)


# ---------------------------------------------------------------- 个股资金流


def fund_flow(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """个股资金流向（支持批量）。返回 {code: {...}}。

    含主力/超大单/大单/中单/小单净额，以及 5/10/20 日主力净额。
    """
    if not codes:
        return {}
    key = "ff:" + ",".join(sorted(codes))
    out = _query(["fund", "flow", ",".join(codes)], key, "realtime")
    if not out:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for r in parse_table(out):
        code = r.get("code") or r.get("symbol")
        if not code:
            continue
        result[code] = {
            "code": code,
            "name": r.get("name", ""),
            "main_net": _f(r.get("MainNetFlow")),
            "jumbo_net": _f(r.get("JumboNetFlow")),
            "block_net": _f(r.get("BlockNetFlow")),
            "mid_net": _f(r.get("MidNetFlow")),
            "small_net": _f(r.get("SmallNetFlow")),
            "main_in": _f(r.get("MainInFlow")),
            "main_out": _f(r.get("MainOutFlow")),
            "retail_in": _f(r.get("RetailInFlow")),
            "retail_out": _f(r.get("RetailOutFlow")),
            "main_net_5d": _f(r.get("MainNetFlow5D")),
            "main_net_10d": _f(r.get("MainNetFlow10D")),
            "main_net_20d": _f(r.get("MainNetFlow20D")),
            "main_rank": _i(r.get("MainInflowRank")),
            "main_circ_rate": _f(r.get("MainInflowCircRate")),
            "close": _f(r.get("ClosePrice")),
            "date": r.get("EndDate", ""),
        }
    return result


# ---------------------------------------------------------------- 筹码


def chip(code: str) -> Dict[str, Any]:
    """筹码成本分布（仅 A 股）。"""
    out = _query(["chip", code], f"chip:{code}", "daily")
    if not out:
        return {}
    rows = parse_table(out)
    if not rows:
        return {}
    r = rows[0]
    return {
        "code": r.get("code", code),
        "name": r.get("name", ""),
        "avg_cost": _f(r.get("chipAvgCost")),
        "concentration70": _f(r.get("chipConcentration70")),
        "concentration90": _f(r.get("chipConcentration90")),
        "profit_rate": _f(r.get("chipProfitRate")),
        "close": _f(r.get("closePrice")),
        "date": r.get("date", ""),
    }


# ---------------------------------------------------------------- 财务


_FIN_LABELS = {
    "利润表": "income",
    "资产负债表": "balance",
    "现金流量表": "cashflow",
}


def finance(code: str, limit: int = 1) -> Dict[str, Any]:
    """三大报表关键指标。返回 {income:{}, balance:{}, cashflow:{}}。"""
    out = _query(["finance", code, "--limit", str(limit)],
                 f"fin:{code}:{limit}", "daily")
    if not out:
        return {}
    tables = parse_tables(out, with_title=True)
    result: Dict[str, Any] = {}
    for t in tables:
        name = t.get("title", "")
        rows = t.get("rows") or []
        if not rows:
            continue
        bucket = None
        for label, key in _FIN_LABELS.items():
            if label in name:
                bucket = key
                break
        if bucket is None:
            bucket = name or f"table{len(result)}"
        result[bucket] = rows[0]
    return result


_INCOME_PICK = [
    ("eps", "BasicEPS", "每股收益(元)"),
    ("revenue", "OperatingRevenue", "营业收入"),
    ("revenue_ttm", "OperatingRevenueTTM", "营业收入TTM"),
    ("revenue_yoy_q", "OperatingRevenueGrowRate_Q", "营收同比(%)"),
    ("npp", "NPParentCompanyOwners", "归母净利润"),
    ("npp_ttm", "NPParentCompanyOwnersTTM", "归母净利润TTM"),
    ("npp_yoy", "NPParentCompanyYOY", "净利同比(%)"),
    ("npp_cut", "NPDeductNonRecurringPL", "扣非净利润"),
    ("gross_margin", "GrossIncomeRatio", "毛利率(%)"),
    ("net_margin", "NetProfitRatio", "净利率(%)"),
    ("roe", "ROEWeighted", "加权ROE(%)"),
    ("roe_ttm", "ROETTM", "ROE TTM(%)"),
    ("roa", "ROA", "ROA(%)"),
    ("roic", "ROIC", "ROIC(%)"),
    ("rd", "RAndD", "研发投入"),
]

_BALANCE_PICK = [
    ("total_asset", "TotalCurrentAssets", "流动资产"),
    ("total_liab", "TotalLiability", "总负债"),
    ("equity", "TotalShareholderEquity", "股东权益"),
    ("debt_ratio", "DebtAssetsRatio", "资产负债率(%)"),
    ("current_ratio", "CurrentRatio", "流动比率"),
    ("quick_ratio", "QuickRatio", "速动比率"),
    ("naps", "NAPS", "每股净资产(元)"),
    ("cash", "CashEquivalents", "货币资金"),
    ("working_capital", "WorkingCapital", "营运资本"),
    ("ar_turnover", "ARTRate", "应收账款周转"),
    ("inv_turnover", "InventoryTRate", "存货周转"),
]

_CASH_PICK = [
    ("op_cash", "NetOperateCashFlow", "经营现金流"),
    ("op_cash_ttm", "NetOperateCashFlowTTM", "经营现金流TTM"),
    ("inv_cash", "NetInvestCashFlow", "投资现金流"),
    ("fin_cash", "NetFinanceCashFlow", "筹资现金流"),
    ("fcff", "FCFF", "自由现金流FCFF"),
    ("fcfe", "FCFE", "自由现金流FCFE"),
    ("cash_ps", "OperCashFlowPS", "每股经营现金流"),
]


def _pick(row: Dict[str, str], spec) -> List[Dict[str, Any]]:
    out = []
    for key, field, label in spec:
        if field in row:
            out.append({"key": key, "label": label, "raw": _f(row.get(field))})
    return out


def fundamentals(code: str) -> Dict[str, Any]:
    """整理后的基本面摘要。"""
    fin = finance(code, limit=1)
    if not fin:
        return {}
    income = fin.get("income") or {}
    balance = fin.get("balance") or {}
    cash = fin.get("cashflow") or {}
    period = income.get("EndDate") or balance.get("EndDate") or cash.get("EndDate") or ""
    return {
        "period": period,
        "income": _pick(income, _INCOME_PICK),
        "balance": _pick(balance, _BALANCE_PICK),
        "cashflow": _pick(cash, _CASH_PICK),
    }


# ---------------------------------------------------------------- 研报 / 公告


def reports(code: str, limit: int = 10) -> List[Dict[str, Any]]:
    """机构研报列表。"""
    out = _query(["report", "list", code, "--limit", str(limit)],
                 f"rep:{code}:{limit}", "daily")
    if not out:
        return []
    items = []
    for r in parse_table(out):
        title = (r.get("title") or "").strip()
        if not title:
            continue
        items.append({
            "title": title,
            "time": (r.get("time") or "")[:10],
            "rating": (r.get("tzpj") or "").strip(),
            "type": (r.get("typeStr") or "").strip(),
        })
        if len(items) >= limit:
            break
    return items


def notices(code: str, limit: int = 10) -> List[Dict[str, Any]]:
    """公司公告列表。"""
    out = _query(["notice", "list", code, "--limit", str(limit)],
                 f"not:{code}:{limit}", "daily")
    if not out:
        return []
    items = []
    for r in parse_table(out):
        title = (r.get("title") or "").strip()
        if not title:
            continue
        items.append({
            "title": title,
            "time": (r.get("time") or "")[:16],
        })
        if len(items) >= limit:
            break
    return items


# ---------------------------------------------------------------- 市场


def market_overview() -> Dict[str, Any]:
    """A 股市场总览：12 维评分 + 总评。"""
    out = _query(["market-overview"], "mktov", "realtime")
    if not out:
        return {}
    clean = _ANSI_RE.sub("", out)
    dims = []
    for r in parse_table(out):
        dims.append({
            "name": r.get("dimension", ""),
            "score": _i(r.get("score")),
            "status": r.get("status", ""),
        })
    raw_score = 0.0
    adj_score = 0.0
    m = re.search(r"原始评分\s*\**\s*([\d.]+)", clean)
    if m:
        raw_score = float(m.group(1))
    m = re.search(r"调整评分\s*\**\s*([\d.]+)", clean)
    if m:
        adj_score = float(m.group(1))
    date = ""
    req_date = ""
    # 上游原文：数据日期 `2026-09-21`（请求日期 2026-09-22，后端实际数据日期）
    # 昨天行情已收盘、但今天的 market-overview 画像还没重算时，
    # date 会比请求日期落后 1 天。这不是我们的 bug，必须原样呈现，
    # 否则用户会以为是本地取数失败，或者误以为看的是今天的温度。
    m = re.search(r"数据日期\s*`([\d-]+)`", clean)
    if m:
        date = m.group(1)
    m = re.search(r"请求日期\s*([\d-]+)", clean)
    if m:
        req_date = m.group(1)
    stale = bool(date and req_date and date < req_date)
    return {
        "date": date,
        "request_date": req_date,
        "stale": stale,
        "raw_score": raw_score,
        "adj_score": adj_score,
        "dims": dims,
    }


def changedist() -> Dict[str, Any]:
    """沪深 A 股涨跌分布。"""
    out = _query(["changedist"], "chgdist", "realtime")
    if not out:
        return {}
    rows = parse_table(out)
    if not rows:
        return {}
    r = rows[0]
    sections = []
    for s in parse_tables(out):
        pass
    # 区间分布是第二张表
    allt = parse_tables(out)
    if len(allt) > 1:
        for row in allt[1]:
            sections.append({
                "section": row.get("section", ""),
                "direction": row.get("direction", ""),
                "count": _i(row.get("count")),
            })
    text = parse_quote_text(out)
    # upRatio 列偶发返回 0（后端未填充），优先用文案解析出的上涨占比；
    # 两者都缺时再按涨跌家数现算
    up_n = _i(r.get("upCount"))
    down_n = _i(r.get("downCount"))
    ratio = text.get("up_ratio_pct")
    if not ratio:
        cand = _i(r.get("upRatio"))
        ratio = cand if cand else (round(up_n / (up_n + down_n) * 100) if (up_n + down_n) else 0)
    text.pop("up_ratio_pct", None)
    return {
        "up": up_n,
        "down": down_n,
        "flat": _i(r.get("flatCount")),
        "suspend": _i(r.get("suspensionCount")),
        "up_limit": _i(r.get("upLimitCount")),
        "down_limit": _i(r.get("downLimitCount")),
        "up_ratio": ratio,
        "sections": sections,
        **text,
    }


def lhb_market(limit: int = 30) -> List[Dict[str, Any]]:
    """全市场龙虎榜（净买额排序）。"""
    out = _query(["lhb"], "lhb", "realtime")
    if not out:
        return []
    items = []
    date = ""
    m = re.search(r"（(\d{4}-\d{2}-\d{2})", _ANSI_RE.sub("", out))
    if m:
        date = m.group(1)
    for r in parse_table(out)[:limit]:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        items.append({
            "rank": _i(r.get("rank")),
            "code": r.get("code", ""),
            "name": name,
            "change_pct": _f(r.get("changePct")),
            "net_buy": _f(r.get("netBuyAmount")),
            "buy": _f(r.get("buyAmount")),
            "sell": _f(r.get("sellAmount")),
            "date": date,
        })
    return items


# ---------------------------------------------------------------- 东财龙虎榜（后验增强）

_EM_LHB_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EM_LHB_COLS = (
    "SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLAIN,"
    "CLOSE_PRICE,CHANGE_RATE,BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,"
    "BILLBOARD_SELL_AMT,ACCUM_AMOUNT,DEAL_NET_RATIO,TURNOVERRATE,"
    "D1_CLOSE_ADJCHRATE,D2_CLOSE_ADJCHRATE,D5_CLOSE_ADJCHRATE,"
    "D10_CLOSE_ADJCHRATE"
)
_EM_HEADERS = {
    "Referer": "https://data.eastmoney.com/stock/tradedetail.html",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _nf(v: Any, nd: int = 2) -> Optional[float]:
    """nullable float：None/非法值保持 None（后验字段 None=交易日未到期，不能当 0）。"""
    try:
        s = str(v).strip().replace(",", "")
        if s in ("", "--", "null", "None", "-"):
            return None
        return round(float(s), nd)
    except Exception:
        return None


def lhb_em(date: str = "", limit: int = 30) -> Dict[str, Any]:
    """东财龙虎榜明细：上榜原因 + D1/D2/D5/D10 后验复权涨跌幅（净买额排序）。

    借鉴 ArvinLovegood/go-stock（backend/data/market_news_api.go LongTiger）。
    - date 为空：取最近一个有数据的交易日（filter 只带上限，一次请求）；
    - date 非空：精确查询；该日无榜（周末/节假日）自动回退到之前最近一期；
    - 后验字段 None = 对应交易日尚未到期（当日榜 D1 最早次日收盘后才有）。
    返回 {"items", "date", "total", "source"}；接口失败返回 items=[]（由路由降级）。
    """
    cache_key = f"lhb_em:{date}:{limit}"
    cached = _cache_get(cache_key, _CACHE_TTL["realtime"])
    if cached is not None:
        return cached

    today = datetime.now().strftime("%Y-%m-%d")
    params = {
        "sortColumns": "TRADE_DATE,SECURITY_CODE",
        "sortTypes": "-1,1",
        "pageSize": "500",
        "pageNumber": "1",
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": _EM_LHB_COLS,
        "source": "WEB",
        "client": "WEB",
    }

    def _fetch(filt: str) -> List[Dict[str, Any]]:
        try:
            p = dict(params, filter=filt)
            r = requests.get(_EM_LHB_URL, params=p, headers=_EM_HEADERS, timeout=10)
            j = r.json()
            if not j.get("success"):
                return []
            return (j.get("result") or {}).get("data") or []
        except Exception:
            return []

    rows = _fetch(f"(TRADE_DATE<='{today}')" if not date
                  else f"(TRADE_DATE<='{date}')(TRADE_DATE>='{date}')")
    if date and not rows:  # 指定日期无榜（节假日）→ 回退到之前最近一期
        rows = _fetch(f"(TRADE_DATE<='{date}')")
    if not rows:
        return {"items": [], "date": "", "total": 0, "source": "eastmoney"}

    day = str(rows[0].get("TRADE_DATE", ""))[:10]
    rows = [r for r in rows if str(r.get("TRADE_DATE", ""))[:10] == day]
    rows.sort(key=lambda r: r.get("BILLBOARD_NET_AMT") or float("-inf"), reverse=True)

    # 东财同一股票可能因多个上榜类型出多条记录（如日榜+3日榜），按 code 去重保留净买额最大条
    seen = set()
    uniq = []
    for r in rows:
        code = (r.get("SECURITY_CODE") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        uniq.append(r)
    rows = uniq

    items: List[Dict[str, Any]] = []
    for idx, r in enumerate(rows[:limit], 1):
        explain = (r.get("EXPLAIN") or r.get("EXPLANATION") or "").strip()
        items.append({
            "rank": idx,
            "code": (r.get("SECURITY_CODE") or "").strip(),
            "name": (r.get("SECURITY_NAME_ABBR") or "").strip(),
            "change_pct": _nf(r.get("CHANGE_RATE")),
            "close": _nf(r.get("CLOSE_PRICE")),
            "net_buy": _nf(r.get("BILLBOARD_NET_AMT"), 0),
            "buy": _nf(r.get("BILLBOARD_BUY_AMT"), 0),
            "sell": _nf(r.get("BILLBOARD_SELL_AMT"), 0),
            "reason": explain,
            "turnover_rate": _nf(r.get("TURNOVERRATE")),
            "deal_net_ratio": _nf(r.get("DEAL_NET_RATIO")),
            "d1": _nf(r.get("D1_CLOSE_ADJCHRATE")),
            "d2": _nf(r.get("D2_CLOSE_ADJCHRATE")),
            "d5": _nf(r.get("D5_CLOSE_ADJCHRATE")),
            "d10": _nf(r.get("D10_CLOSE_ADJCHRATE")),
            "date": day,
        })
    out = {"items": items, "date": day, "total": len(rows), "source": "eastmoney"}
    _cache_put(cache_key, out)
    return out


def sector_rank(limit: int = 25) -> List[Dict[str, Any]]:
    """全市场板块（行业）行情榜，带主力净流入与领涨股。"""
    out = _query(["sector", "ranking"], "sectrank", "realtime")
    if not out:
        return []
    items = []
    for r in parse_table(out)[:limit]:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        items.append({
            "code": r.get("code", ""),
            "name": name,
            "change_pct": _f(r.get("changePct")),
            "main_net": _f(r.get("mainNetInflow")),
            "main_net_5d": _f(r.get("mainNetInflow5d")),
            "main_net_20d": _f(r.get("mainNetInflow20d")),
            "turnover": _f(r.get("turnover")),
            "turnover_rate": _f(r.get("turnoverRate")),
            "up_count": (r.get("upCount") or ""),
            "leader": (r.get("leader") or "").strip(),
        })
    return items


def profile(code: str) -> Dict[str, Any]:
    """个股简况。"""
    out = _query(["profile", code], f"prof:{code}", "static")
    if not out:
        return {}
    rows = parse_table(out)
    if not rows:
        return {}
    r = rows[0]
    return {
        "code": r.get("code", code),
        "name": r.get("name", ""),
        "listed_date": r.get("listedDate", ""),
        "business": (r.get("business") or "").strip(),
        "chairman": r.get("chairman", ""),
        "industry": r.get("industry", ""),
        "website": r.get("website", ""),
    }


def dividends(code: str, years: int = 3) -> List[Dict[str, Any]]:
    """分红历史。"""
    out = _query(["dividend", code, "--years", str(years)],
                 f"div:{code}:{years}", "daily")
    if not out:
        return []
    items = []
    for r in parse_table(out):
        plan = (r.get("dividendPlan") or "").strip()
        if not plan:
            continue
        items.append({
            "plan": plan,
            "cash": _f(r.get("cashDiviRMB")),
            "ex_date": r.get("exDiviDate", ""),
            "report_end": r.get("reportEndDate", ""),
            "total_cash": _f(r.get("totalCashDiviComRMB")),
        })
    return items


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":
    print("CLI:", _CLI)
    print("fund_flow:", list(fund_flow(["sh600519", "sz000858"]).keys()))
    print("chip:", chip("sh600519"))
    print("fundamentals period:", fundamentals("sh600519").get("period"))
    print("reports:", len(reports("sh600519", 3)))
    print("notices:", len(notices("sh600519", 3)))
    mo = market_overview()
    print("market dims:", len(mo.get("dims", [])), "score:", mo.get("adj_score"))
    cd = changedist()
    print("changedist:", cd.get("up"), cd.get("down"), cd.get("up_limit"))
    print("lhb:", len(lhb_market()), "sector:", len(sector_rank()))
