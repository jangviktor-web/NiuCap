"""连板梯队 + 情绪周期（蒸馏 easy-stock 超短连板 / 复用 #95 市场情绪周期）

- 今日涨停：复用 moves.detect_moves（零新增采集，与 /api/moves 同源）。
- 连板天数：只对「今日涨停子集」取近 12 日 K 线（get_kline，腾讯/新浪），
  全市场 ~5500 只逐只取数不可行 → 只算涨停子集（几十~一百只）。
- 按交易日对齐：周末/节假日不误判连板（easy-stock 的 trading_calendar 踩过的坑）。
- 性能：涨停子集逐只 K 线 ~0.4s，首次约 15~60s → 后台线程 + 按日缓存，
  接口立即返回今日涨停 + 缓存连板；首跑返回 computing=true，前端轮询补满。
- 情绪周期：#95 market_phase 已做 6 阶段，本模块只取快照注入，不重造。
"""

import time
import threading
import datetime as _dt
from typing import Any, Dict, List, Optional

import datasource as ds
import moves as mvs

# 按交易日缓存：date -> {"board_days": {code:int}, "yest": {code:float}, "ts":float}
_LIMIT_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def _limit_pct(code: str) -> float:
    c = code.replace("sh", "").replace("sz", "").replace("bj", "")
    if code.startswith("bj"):
        return 30.0
    if c.startswith(("300", "301", "302", "688", "689")):
        return 20.0
    return 10.0


def _is_st(name: str) -> bool:
    return "ST" in (name or "").upper()


def _trading_day(d: _dt.date) -> bool:
    # ponytail: 简易交易日历——跳过周末；法定节假日表留空，后续可接交易日历文件
    return d.weekday() < 5


def _today_str() -> str:
    return _dt.date.today().isoformat()


def _count_board_days(pcts: List[float], lim: float) -> int:
    """从最新一日往前数连续达到涨停幅度的天数（pcts 由旧到新）。"""
    days = 0
    for p in reversed(pcts):
        if p >= lim - 0.5:  # 容差半个百分点
            days += 1
        else:
            break
    return days


def _compute_board_days(limit_codes: List[Dict[str, Any]], date: str) -> Dict[str, Any]:
    """对涨停子集取近 12 日 K 线，算连板天数 + 昨日反馈。"""
    board_days: Dict[str, int] = {}
    yest: Dict[str, float] = {}
    for it in limit_codes:
        code = it["code"]
        try:
            kl = ds.get_kline(code, "1d", count=12, use_cache=True)
        except Exception:
            kl = []
        pcts: List[float] = []
        for i in range(1, len(kl)):
            prev, cur = kl[i - 1]["close"], kl[i]["close"]
            if prev > 0:
                pcts.append((cur - prev) / prev * 100.0)
        lim = 5.0 if _is_st(it.get("name", "")) else _limit_pct(code)
        board_days[code] = _count_board_days(pcts, lim)
        if len(pcts) >= 2:
            yest[code] = round(pcts[-2], 2)
    return {"board_days": board_days, "yest": yest, "ts": time.time()}


def get_cached(date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return _LIMIT_CACHE.get(date or _today_str())


def ensure_cached(rows: List[Dict[str, Any]], limit: int = 200) -> Dict[str, Any]:
    """确保连板缓存就绪：有且非空则用，否则同步算（当日首次约 9s，仅涨停子集取 K 线）。

    ponytail: 不用后台线程——曾因「缓存非空即跳过」导致首跑偶发失败存了空 board_days
    后永不再重算（连板全变首板）。同步算 + 非空校验更简单可靠，9s 仅当日首次，
    之后走内存缓存（history 引擎预热也是这个节奏）。
    """
    date = _today_str()
    cached = _LIMIT_CACHE.get(date)
    if cached and cached.get("board_days"):
        return cached
    res = mvs.detect_moves(rows, amount_min=0)
    zt = res["buckets"].get("涨停", [])[:limit]
    cached = _compute_board_days(zt, date)
    with _CACHE_LOCK:
        _LIMIT_CACHE[date] = cached
    return cached


def build_ladder(rows: List[Dict[str, Any]], limit: int = 200,
                board_cache: Optional[Dict[str, Any]] = None,
                background: bool = True) -> Dict[str, Any]:
    """今日涨停 + 连板梯队分组 + 连板率。情绪周期由调用方注入 phase。"""
    date = _today_str()
    cache = board_cache if board_cache is not None else get_cached(date)
    if cache is None and not background:
        cache = _compute_board_days(
            mvs.detect_moves(rows, amount_min=0).get("buckets", {}).get("涨停", [])[:limit],
            date)

    if not _trading_day(_dt.date.fromisoformat(date)):
        return {"date": date, "trading_day": False, "涨停": [], "ladder": {},
                "rate": None, "max_height": 0, "computing": False, "phase": None}

    res = mvs.detect_moves(rows, amount_min=0)
    zt = res["buckets"].get("涨停", [])[:limit]
    board_days = (cache or {}).get("board_days", {})
    yest = (cache or {}).get("yest", {})

    ladder = {"首板": [], "2连板": [], "3连板": [], "4+连板": []}
    max_height = 0
    for it in zt:
        d = board_days.get(it["code"])
        if d is None:
            d = 0 if cache is not None else 1  # 缓存未就绪默认首板；无缓存首跑也算首板
        if d <= 0:
            d = 1
        max_height = max(max_height, d)
        key = ("首板" if d <= 1 else "2连板" if d == 2
               else "3连板" if d == 3 else "4+连板")
        e = dict(it)
        e["board_days"] = d
        e["yest_pct"] = yest.get(it["code"], None)
        ladder[key].append(e)

    n_lb = sum(len(v) for k, v in ladder.items() if k != "首板")
    rate = round(n_lb / len(zt), 3) if zt else None
    computing = cache is None and background
    return {"date": date, "trading_day": True, "涨停": zt, "ladder": ladder,
            "rate": rate, "max_height": max_height, "computing": computing,
            "phase": None}


def selfcheck() -> int:
    """ponytail: 连板天数判定的最小自检。"""
    fails = 0

    def rec(name, ok, detail=""):
        nonlocal fails
        if not ok:
            fails += 1
            print(f"  ❌ {name} {detail}")
        else:
            print(f"  ✅ {name}")

    # _limit_pct 边界
    rec("科创板 20%", _limit_pct("sh688001") == 20.0)
    rec("创业板 20%", _limit_pct("sz300750") == 20.0)
    rec("北交所 30%", _limit_pct("bj830799") == 30.0)
    rec("主板 10%", _limit_pct("sh600519") == 10.0)
    rec("ST 识别", _is_st("ST 某某") is True)
    # 连板计数（纯函数）：由旧到新 [8,9,10.2,10.0] → 末二连板(今日+昨日)，前日 9% 破 = 2
    rec("连板计数 末二连板", _count_board_days([8.0, 9.0, 10.2, 10.0], 10.0) == 2,
        f"得 {_count_board_days([8.0,9.0,10.2,10.0],10.0)}")
    rec("连板计数 断板归零", _count_board_days([9.0, 10.5, -2.0, 10.2], 10.0) == 1,
        f"得 {_count_board_days([9.0,10.5,-2.0,10.2],10.0)}")
    # 交易日历：周末非交易日
    import datetime as _d
    rec("周末非交易日", _trading_day(_d.date(2026, 10, 3)) is False)  # 周六
    rec("工作日交易日", _trading_day(_d.date(2026, 9, 28)) is True)  # 周一
    return fails


if __name__ == "__main__":
    print("limitup selfcheck:")
    n = selfcheck()
    print("FAIL" if n else "OK")
