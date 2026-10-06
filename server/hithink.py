"""同花顺（hithink-finance）特色数据层。

通过 REST 调用 https://fuyao.aicubes.cn，认证头 X-api-key。
凭证读取顺序：环境变量 HITHINK_FINANCE_API_KEY → 用户级 credentials.env。
凭证绝不写入代码、日志或前端响应。

覆盖特色数据：人气热榜、涨停池、连板天梯、炸板池、跌停池、龙虎榜、异动分析。
这些接口多为 today-only 能力，非交易日或盘前会返回空列表，属正常行为，
调用方须优雅降级而不是报错。
"""

from __future__ import annotations

import datetime
import json
import os
import re
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


# ---------------------------------------------------------------- 集合竞价
#
# 契约来源：skills/hithink-finance/references/api/a-share/auction.md
# 实测（2026-10-06）：休市日仍返回数据，且是**上一交易日的竞价终态**，
#   不是空列表 —— 所以调用方必须看 status/date 判新鲜度，不能只看有没有数据。


def _auction_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """竞价明细字段映射（保留 auction_ 前缀，避免与实时价混淆）。"""
    return {
        "code": r.get("thscode") or "",
        "ticker": r.get("ticker", ""),
        "name": (r.get("name") or "").strip(),
        "auction_price": _f(r.get("auction_price")),
        "auction_pct": _f(r.get("auction_pct")),
        "auction_volume": _f(r.get("auction_volume")),
        "auction_amount": _f(r.get("auction_amount")),
        "auction_unmatched": _f(r.get("auction_unmatched")),
        "auction_turnover_pct": _f(r.get("auction_turnover_pct")),
        "auction_yesterday_ratio_pct": _f(r.get("auction_yesterday_ratio_pct")),
        "auction_volume_ratio": _f(r.get("auction_volume_ratio")),
        "pre_close": _f(r.get("pre_close_price")),
        "open": _f(r.get("open_price")),
        "last": _f(r.get("last_price")),
        "float_cap": _f(r.get("float_market_cap")),
    }


def auction_snapshot(codes: List[str], stage: str = "final") -> Dict[str, Any]:
    """A 股集合竞价快照。

    codes : 我方格式（sh600519 / 600519.SH 都吃），单次最多 100 只，超出截断。
    stage : live(实时) / final(终态)。

    返回 {ok, phase, status, total, items}；不可用时 ok=False、items=[]。
    """
    ths = [to_thscode(c) for c in (codes or []) if str(c).strip()]
    ths = [t for t in ths if t][:100]          # 上游硬上限 100
    if not ths:
        return {"ok": False, "phase": stage, "status": "empty",
                "total": 0, "items": []}
    data = _cached("/api/a-share/auction/snapshot",
                   {"thscodes": ",".join(ths), "stage": stage}, "realtime")
    if not data:
        return {"ok": False, "phase": stage, "status": "unavailable",
                "total": 0, "items": []}
    return {
        "ok": True,
        "phase": (data.get("auction_phase") or stage),
        "status": (data.get("data_status") or ""),
        "total": _i(data.get("total")),
        "items": [_auction_row(r) for r in (data.get("item") or [])],
    }


def auction_benchmark(date: Optional[str] = None) -> Dict[str, Any]:
    """短线风向标竞价基准（当日全市场竞价涨跌幅 + 标签）。

    date : yyyy-MM-dd，省略取服务端当日；显式传非交易日**不回退**（返回空）。

    ⚠ 实测与契约文档不一致：文档示例 tags 是 ["高开","放量"]，
      实测返回的是行业/概念标签（如 ["住宅开发","租售同权"]）。
      前端文案按"概念归因"理解，不要按技术形态渲染。

    返回 {ok, date, total, items}；休市/无数据时 ok=True 但 items=[]。
    """
    params = {"date": date} if date else None
    data = _cached("/api/a-share/auction/short-term-benchmark",
                   params, "realtime")
    if not data:
        return {"ok": False, "date": date or "", "total": 0, "items": []}
    items = []
    for r in (data.get("item") or []):
        tags = r.get("tags") or []
        items.append({
            "code": r.get("thscode") or "",
            "ticker": r.get("ticker", ""),
            "name": (r.get("name") or "").strip(),
            "auction_pct": _f(r.get("auction_pct")),
            "tags": [str(t).strip() for t in tags if str(t).strip()],
        })
    return {
        "ok": True,
        "date": (data.get("date") or date or "").strip(),
        "total": len(items),
        "items": items,
    }


# ---------------------------------------------------------------- 行情备源
#
# 定位：**第三备源**，排在 eltdx / 腾讯 / 新浪之后。只在那些源全挂时才走，
# 所以常态零开销。它的价值是「前两个源同时不可用时页面还有数据」，而不是
# 「更快的行情源」——腾讯 0.35s 节流批量的速度远优于这里。
#
# 实测约束（2026-10-06，均为踩过的坑，改动前务必重测）：
#  1. **混入 1 个无效代码会让整批归零**（code=3001）。自选股里只要有一只
#     退市/未上市的票，整批就全废 —— 所以 quote_snapshot 必须整批失败后逐只
#     重试，否则这个源等于不存在。
#  2. **只支持沪深 A 股个股**：ETF(510300)、指数(000300)、北交所(430047)
#     全部返回 None。别指望它给 ETF 页兜底。
#  3. historical 只支持 interval=1d，且窗口跨度 ≤ 10 年（实测 3650 天 OK、
#     3660 天返回 code=1003）。
#  4. volume 单位是**股**、turnover 是**元**；date_ms 是 Asia/Shanghai 零点
#     毫秒，用 UTC 换算会整体差一天。

_SH_TZ = datetime.timezone(datetime.timedelta(hours=8))
_HIST_MAX_DAYS = 3600          # 实测 3650 天可用、3660 超限；留 50 天余量


def _sh_date(ms: int) -> str:
    """毫秒时间戳 -> 'YYYY-MM-DD'（Asia/Shanghai）。"""
    return datetime.datetime.fromtimestamp(ms / 1000, _SH_TZ).strftime("%Y-%m-%d")


def _th_to_internal(code: str) -> str:
    """thscode(600519.SH) -> 我方内部格式(sh600519)；已是内部格式则原样返回。

    注意别写反：thscode 的交易所后缀在**末尾**（600519.SH），
    我方内部格式的交易所前缀在**开头**（sh600519）。
    """
    c = (code or "").strip()
    if "." in c:
        head, _, ex = c.partition(".")
        if head.isdigit() and len(ex) == 2:          # 600519.SH / 430047.BJ
            return ex.lower() + head
    return c


def _bare(code: str) -> str:
    """内部格式取纯代码：sh600519 -> 600519（与 datasource.bare 同语义，
    独立实现以避免 datasource ← hithink 的循环 import）。"""
    return re.sub(r"^(sh|sz|bj|hk|us)", "", str(code).lower())


def quote_snapshot(codes: List[str], per_code_limit: int = 30) -> Dict[str, Dict[str, Any]]:
    """行情快照（备源）。codes: 我方格式 -> {内部代码: 行情行}。

    行字段与 quote_tencent / snapshot_eltdx **完全兼容**，上层无需分叉。
    单位已换算成我方口径：volume 手、amount 万元、float_cap/total_cap 亿。

    整批失败时逐只重试（最多 per_code_limit 只）—— 因为一个坏代码就能让整批
    归零，这是实测踩到的第一个坑。逐只重试只在降级路径发生，正常源可用时
    根本走不到这里。
    """
    ths: List[str] = []
    for c in (codes or []):
        t = to_thscode(str(c).strip())
        if t and "." in t and t not in ths:
            ths.append(t)
    if not ths:
        return {}

    data = _cached("/api/a-share/prices/snapshot",
                   {"thscodes": ",".join(ths)}, "realtime")

    if not data:
        # 整批失败 → 逐只试，隔离坏代码
        out: Dict[str, Dict[str, Any]] = {}
        for t in ths[:per_code_limit]:
            one = _cached("/api/a-share/prices/snapshot", {"thscodes": t}, "realtime")
            if not one:
                continue
            for r in (one.get("item") or []):
                row = _snap_row(r)
                if row:
                    out[row["code"]] = row
        return out

    out = {}
    for r in (data.get("item") or []):
        row = _snap_row(r)
        if row:
            out[row["code"]] = row
    return out


def _snap_row(r: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """上游行情行 -> 我方快照行；价格 <= 0 视为无效丢弃。"""
    price = _f(r.get("last_price"))
    if price <= 0:
        return None
    code = _th_to_internal(r.get("thscode") or "")
    prev = _f(r.get("prev_price"))
    vol_shares = _f(r.get("volume"))
    amount_yuan = _f(r.get("turnover"))
    return {
        "code": code,
        "symbol": _bare(code),
        "name": (r.get("name") or "").strip(),
        "price": round(price, 3),
        "prev_close": round(prev, 3),
        "open": round(_f(r.get("open_price")), 3),
        "high": round(_f(r.get("high_price")), 3),
        "low": round(_f(r.get("low_price")), 3),
        "change": round(_f(r.get("price_change")), 3),
        "change_pct": round(_f(r.get("price_change_ratio_pct")), 2),
        "volume": round(vol_shares / 100.0, 0),          # 股 -> 手
        "amount": round(amount_yuan / 10000.0, 2),       # 元 -> 万元
        # 以下字段上游不提供，填 0：上层用 `> 0` 判断有无（榜单里 0 显示「亏损」）
        "turnover": 0.0,
        "pe": 0.0,
        "pb": 0.0,
        "amplitude": 0.0,
        "float_cap": 0.0,
        "total_cap": 0.0,
        "limit_up": 0.0,
        "limit_down": 0.0,
        "time": "",
        "source": "hithink",
    }


def kline_history(code: str, count: int = 250,
                  adjust: str = "forward") -> List[Dict[str, Any]]:
    """历史日 K（备源）。返回 [{date, open, close, high, low, volume}]，volume 单位手。

    count 只是**目标**根数：上游按时间窗返回，实际根数取决于窗口内有多少
    交易日。窗口算法：从今天回溯 count × 1.55 天（A 股约 0.65 交易日/天）
    再按 10 年上限截断，这样要 250 根时约能拿到 250±15 根，够用。

    adjust：forward(前复权) / backward(后复权) / none。我方主源是前复权，
    所以默认 forward，口径对齐。
    """
    ths = to_thscode(str(code).strip())
    if not ths or "." not in ths:
        return []
    now_ms = int(time.time() * 1000)
    span_days = min(int(count * 1.55) + 10, _HIST_MAX_DAYS)
    start_ms = now_ms - span_days * 86400000

    data = _cached("/api/a-share/prices/historical",
                   {"thscode": ths, "interval": "1d",
                    "start": start_ms, "end": now_ms, "adjust": adjust},
                   "daily")
    if not data:
        return []

    out = []
    for r in (data.get("item") or []):
        try:
            ms = int(r.get("date_ms") or 0)
            close = _f(r.get("close_price"))
            if ms <= 0 or close <= 0:
                continue
            out.append({
                "date": _sh_date(ms),
                "open": round(_f(r.get("open_price")), 3),
                "close": round(close, 3),
                "high": round(_f(r.get("high_price")), 3),
                "low": round(_f(r.get("low_price")), 3),
                "volume": round(_f(r.get("volume")) / 100.0, 0),   # 股 -> 手
            })
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["date"])
    return out[-count:] if count and len(out) > count else out


# ---------------------------------------------------------------- 交易日历
#
# 契约：GET /api/a-share/calendar/trading-days —— 无入参，固定窗口
#       [今日-1年, 今日]，实测 241 条。
# ⚠ 末端是「最后一个交易日」而非自然日今天（今天休市时今天不在序列里），
#   所以它只能校准历史，**不能预测未来交易日**。


def trading_days() -> Dict[str, Any]:
    """A 股近一年权威交易日序列。

    返回 {ok, first, last, count, days}，days 为 'YYYY-MM-DD' 升序列表
    （上游给的是 yyyyMMdd，这里统一转成与本地 holidays 一致的带横线格式）。
    """
    data = _cached("/api/a-share/calendar/trading-days", None, "daily")
    if not data:
        return {"ok": False, "first": "", "last": "", "count": 0, "days": []}
    days = []
    for r in (data.get("item") or []):
        s = str(r.get("date") or "").strip()
        if len(s) == 8 and s.isdigit():
            days.append(f"{s[:4]}-{s[4:6]}-{s[6:]}")   # 20260930 → 2026-09-30
    days.sort()
    return {
        "ok": bool(days),
        "first": days[0] if days else "",
        "last": days[-1] if days else "",
        "count": len(days),
        "days": days,
    }


# ---------------------------------------------------------------- 估值快照
#
# 契约：GET /api/a-share/valuations/snapshot
# ⚠ 实测（2026-10-06）：**混入 ETF 或指数会整批失败**（code≠0 返回 None），
#   单独传 ETF 同样失败。所以只用于个股，批量前必须过滤非个股代码。


def valuation(thscodes: List[str]) -> List[Dict[str, Any]]:
    """估值快照五口径：PE_TTM / PE_MRQ / PB_MRQ / PS_TTM / PCF_TTM。

    thscodes : 单个或多个（我方格式或 thscode 都吃）。
    失败（含混入 ETF/指数）返回 []，调用方应优雅降级而不是报错。
    """
    ths = [to_thscode(c) for c in (thscodes or []) if str(c).strip()]
    ths = [t for t in ths if t][:50]
    if not ths:
        return []
    data = _cached("/api/a-share/valuations/snapshot",
                   {"thscodes": ",".join(ths)}, "daily")
    if not data:
        return []
    out = []
    for r in (data.get("item") or []):
        out.append({
            "code": r.get("thscode") or "",
            "ticker": r.get("ticker", ""),
            "name": (r.get("name") or "").strip(),
            "pe_ttm": _f(r.get("pe_ttm")),
            "pe_mrq": _f(r.get("pe_mrq")),
            "pb_mrq": _f(r.get("pb_mrq")),
            "ps_ttm": _f(r.get("ps_ttm")),
            "pcf_ttm": _f(r.get("pcf_ttm")),
        })
    return out


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
#
# ponytail：非平凡逻辑留一个可运行自检。这里**完全离线**——用桩数据替换
# _cached，覆盖 A1/A2/A3/B4 的纯函数契约（字段映射、单位换算、代码转换、
# 窗口截断），因为这些地方错一个就是静默错数据，联网时反而难验。

def selfcheck() -> int:
    """hithink 纯函数自检（无网络、无需 Key）。返回失败项数。"""
    fails = 0

    def rec(name, ok, detail=""):
        nonlocal fails
        if not ok:
            fails += 1
            print(f"  ❌ {name} {detail}")
        else:
            print(f"  ✅ {name}")

    # ---- 代码转换 ----
    rec("to_thscode 沪市", to_thscode("sh600519") == "600519.SH")
    rec("to_thscode 深市", to_thscode("sz000858") == "000858.SZ")
    rec("to_thscode 北交所", to_thscode("bj430047") == "430047.BJ")
    rec("to_thscode 幂等", to_thscode("600519.SH") == "600519.SH")
    # thscode 后缀在末尾，我方内部前缀在开头——别写反
    rec("_th_to_internal 沪市", _th_to_internal("600519.SH") == "sh600519")
    rec("_th_to_internal 北交所", _th_to_internal("430047.BJ") == "bj430047")
    rec("_th_to_internal 幂等", _th_to_internal("sh600519") == "sh600519")
    rec("_th_to_internal 裸代码透传", _th_to_internal("600519") == "600519")
    rec("_bare 取引用", _bare("sh600519") == "600519")

    # ---- 时区：date_ms 是 Asia/Shanghai 零点毫秒，用 UTC 换算会差一天 ----
    # 2026-09-30 00:00:00 +08 == 2026-09-29 16:00:00 UTC
    ms = int(datetime.datetime(2026, 9, 29, 16, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    rec("_sh_date 时区换算", _sh_date(ms) == "2026-09-30", f"得 {_sh_date(ms)}")

    # ---- 数值清洗：heat 是字符串、"17457.0"，空值有多种写法 ----
    rec("_f 字符串数字", _f("17457.0") == 17457.0)
    rec("_f 千分位", _f("1,234.5") == 1234.5)
    rec("_f 带百分号", _f("3.21%") == 3.21)
    for bad in ("", "--", "null", "None", "-", None):
        if _f(bad) != 0.0:
            fails += 1
            print(f"  ❌ _f 空值 {bad!r} 应为 0.0，得 {_f(bad)}")
    print("  ✅ _f 空值 6 种写法均归 0")

    # ---- 单位换算：上游 volume 股 -> 手、turnover 元 -> 万元 ----
    row = _snap_row({
        "thscode": "600519.SH", "last_price": 1258.62, "prev_price": 1250.0,
        "volume": 12345, "turnover": 67890000.0,   # 12345 股 / 6789 万元
    })
    rec("_snap_row 成交量 股->手", row is not None and row["volume"] == 123.0,
        f"得 {row and row['volume']}")
    rec("_snap_row 成交额 元->万元", row is not None and abs(row["amount"] - 6789.0) < 0.01,
        f"得 {row and row['amount']}")
    rec("_snap_row 缺失字段填 0", row is not None and row["pe"] == 0.0 and row["float_cap"] == 0.0)
    rec("_snap_row 标记来源", row is not None and row["source"] == "hithink")
    rec("_snap_row 价格 0 丢弃", _snap_row({"thscode": "600519.SH", "last_price": 0}) is None)
    rec("_snap_row 空输入不崩", _snap_row({}) is None)

    # ---- 竞价字段映射：必须保留 auction_ 前缀，且基准的 tags 是概念标签 ----
    a = _auction_row({"thscode": "600519.SH", "auction_price": 1260.0,
                      "auction_pct": "1.28", "auction_volume_ratio": 3.5,
                      "pre_close_price": 1250.0})
    rec("_auction_row 前缀不丢", a["auction_price"] == 1260.0 and a["auction_pct"] == 1.28)
    rec("_auction_row 昨收映射", a["pre_close"] == 1250.0)

    # ---- 窗口截断：上游硬上限 100 / 50，日线跨度上限 3600 天 ----
    rec("_HIST_MAX_DAYS 留余量", _HIST_MAX_DAYS == 3600, f"得 {_HIST_MAX_DAYS}")

    # ---- 桩数据：替换 _cached 验证编排层（不联网） ----
    global _CACHE
    _CACHE.clear()
    calls = []

    def fake_cached(path, params, kind):
        calls.append((path, params or {}))
        if path.endswith("calendar/trading-days"):
            # 上游给的是 yyyyMMdd，且**乱序**返回，验证是否被正确转换+排序
            return {"item": [{"date": "20260930"}, {"date": "20260102"},
                              {"date": "20250630"}]}
        if path.endswith("valuations/snapshot"):
            return {"item": [{"thscode": "600519.SH", "name": "贵州茅台",
                               "pe_ttm": 20.5, "pe_mrq": 21.0, "pb_mrq": 7.2,
                               "ps_ttm": 8.1, "pcf_ttm": 19.3}]}
        if path.endswith("auction/short-term-benchmark"):
            return {"date": "2026-09-30", "item": [
                {"thscode": "000001.SZ", "name": " 平安银行 ", "auction_pct": 1.1,
                 "tags": ["住宅开发", "  ", ""]}]}
        return None

    old_cached = _cached
    globals()["_cached"] = fake_cached
    try:
        td = trading_days()
        rec("trading_days yyyyMMdd 转换", td["days"] == ["2025-06-30", "2026-01-02", "2026-09-30"],
            f"得 {td['days']}")
        rec("trading_days 升序", td["days"] == sorted(td["days"]))
        rec("trading_days 首尾", td["first"] == "2025-06-30" and td["last"] == "2026-09-30")
        rec("trading_days 计数", td["count"] == 3 and td["ok"] is True)

        v = valuation(["sh600519"])
        rec("valuation 五口径齐全", len(v) == 1 and all(
            k in v[0] for k in ("pe_ttm", "pe_mrq", "pb_mrq", "ps_ttm", "pcf_ttm")))
        rec("valuation 名称去空格", v[0]["name"] == "贵州茅台")
        # 上限 50：塞 60 只只应取前 50
        calls.clear()
        valuation([f"sh600{ i:03d}" for i in range(60)])
        rec("valuation 截断 50 只", len(calls[-1][1]["thscodes"].split(",")) == 50,
            f"得 {len(calls[-1][1]['thscodes'].split(','))}")

        bm = auction_benchmark()
        rec("benchmark 空标签过滤", bm["items"] and bm["items"][0]["tags"] == ["住宅开发"],
            f"得 {bm['items'] and bm['items'][0]['tags']}")
        rec("benchmark 名称去空格", bm["items"][0]["name"] == "平安银行")

        calls.clear()
        auction_snapshot([])
        rec("auction 空代码不发请求", not calls)
        snap = auction_snapshot(["sh600519"] * 150)
        rec("auction 截断 100 只", len(calls[-1][1]["thscodes"].split(",")) == 100,
            f"得 {len(calls[-1][1]['thscodes'].split(','))}")
        rec("auction 上游 None 时降级", snap["ok"] is False and snap["status"] == "unavailable")
        rec("quote_snapshot 无效代码不请求", quote_snapshot(["600519"]) == {})
        rec("kline_history 非个股返回空", kline_history("600519") == [])

        # 日线窗口：count=250 -> 250*1.55+10=397 天，count 超大则截到 3600
        calls.clear()
        kline_history("sh600519", count=250)
        span = calls[-1][1]
        rec("kline 只请求 1d", span.get("interval") == "1d")
        rec("kline 默认前复权", span.get("adjust") == "forward")
        # span_days 是局部变量，但窗口跨度 = end - start，可直接反推核对
        got = (span["end"] - span["start"]) / 86400000.0
        rec("kline 窗口 250 根 -> 397 天", abs(got - 397) < 1.0, f"得 {got:.0f} 天")
        calls.clear()
        kline_history("sh600519", count=99999)
        got2 = (calls[-1][1]["end"] - calls[-1][1]["start"]) / 86400000.0
        rec("kline 超长窗口截到 3600 天", got2 == _HIST_MAX_DAYS, f"得 {got2:.0f} 天")
    finally:
        globals()["_cached"] = old_cached
        _CACHE.clear()

    # ---- 无 Key 时必须优雅降级，不能抛异常 ----
    # 注意必须同时桩掉 _load_key：_get 里是 `_KEY or _load_key()`，只置空 _KEY
    # 会让它从 credentials 文件把真 Key 读回来，然后真的发一次网络请求。
    # 这里保留真实 _cached，完整走一遍降级路径。
    old_key, old_avail, old_loader = _KEY, API_AVAILABLE, _load_key
    globals()["_KEY"], globals()["API_AVAILABLE"] = None, False
    globals()["_load_key"] = lambda: None
    try:
        rec("无 Key 竞价降级", auction_snapshot(["sh600519"])["ok"] is False)
        rec("无 Key 日线降级", kline_history("sh600519") == [])
        rec("无 Key 日历降级", trading_days()["ok"] is False)
        rec("无 Key 估值降级", valuation(["sh600519"]) == [])
        rec("无 Key 快照降级", quote_snapshot(["sh600519"]) == {})
    finally:
        globals()["_KEY"], globals()["API_AVAILABLE"] = old_key, old_avail
        globals()["_load_key"] = old_loader

    return fails


if __name__ == "__main__":
    fails = selfcheck()
    print(f"[hithink.selfcheck] {'通过' if not fails else str(fails) + ' 项失败'}")
    if API_AVAILABLE:
        # 联网部分只在有 Key 时跑，作为补充观察（不计入失败）
        print(f"API_AVAILABLE: {API_AVAILABLE}")
        print("trading_days:", (trading_days() or {}).get("count"), "条")
        print("to_thscode:", to_thscode("sh600519"), to_thscode("sz000858"))
    else:
        print("未配置同花顺 API Key，跳过联网部分")
    raise SystemExit(1 if fails else 0)
