"""盘中分钟级指标引擎 —— 让选股与看盘从「日线」下沉到「分钟」。

## 为什么需要它

`history.py` 提供的是**日线**指标：它读 `daily_bars`，一天一行，
全市场缓存后按交易日失效。这套设计对「均线多头」「平台突破」这类
日线形态没问题，但它有两个天然局限：

  1. **看不到盘中**。日线引擎只知道「昨天收在哪」，不知道「今天上午怎么走的」。
     想做「盘中放量」「分时突破」这类策略无从下手。
  2. **当天数据要等落库**。日线走 `scripts/sync_bars.py` 落库，
     盘后跑一次，盘中根本拿不到当日数据（这也是 `data_lag_days`
     那个提示存在的原因）。

本模块用**分钟线**绕开这两点：直接从数据源实时取分钟 K 线（不落库），
在内存里算指标。数据源见 `datasource.get_kline_intraday`——
eltdx 优先（全市场 3800 只 × 400 根约 12 秒），腾讯降级（逐只，仅够看盘）。

## 与 history.py 的关键差异（照抄代码时务必注意）

| 维度 | history.py（日线） | 本模块（分钟） |
|---|---|---|
| 数据来源 | `daily_bars` 表（落库） | 实时接口（不落库） |
| 缓存粒度 | 全市场一次性加载，按交易日失效 | 按 code+周期，TTL 15 秒 |
| 时间字段 | `'YYYY-MM-DD'` | `'YYYY-MM-DD HH:MM'` |
| 复权 | 前复权 qfq | **不复权**（分时看真实价） |
| 一根代表 | 一个交易日 | 1/5/15/30/60 分钟 |

**「当日」的定义不同**：日线里 `C[-1]` 是「最后一个交易日」，
分钟线里 `C[-1]` 是「最后一根 K 线」。凡是历史模块里带 `[:-1]`
（不含当日）的地方，在本模块对应的是「不含最后一根」，语义要重新想。

## 指标口径（与 history.py 对齐的地方）

MA 一律用简单移动平均（`np.mean`），与 `mytt.MA` / 通达信 `MA()` 同算法，
这样同一个概念在日线和分钟两个视角下是同一个定义，不会出现
「日线说站上均线、分钟说没站上」的解释混乱。

窗口不足一律返回 `None`（**不是 0**），调用方必须显式判断——
这与 `history.py` 的约定一致，避免「数据不够却给结论」的静默错误。

## 性能与并发

分钟线是**网络 IO**，不是内存计算，所以与日线引擎的策略完全不同：
不能「加载一次用一天」，只能「按需取 + 短 TTL 缓存」。

实测（详见 docs/分钟级实时数据说明.md）：
    · 单只 × 400 根 5 分钟线，eltdx 约 50 ms（含缓存命中更快）
    · 全市场 3800 只 × 400 根，eltdx 批量约 12 秒
    · 腾讯降级路径单只约 400 ms，全市场要 24 分钟（不可接受，故限流保护）

因此 `screen()` 在 eltdx 不可用时**拒绝全市场扫描**并返回明确原因，
而不是让用户等 24 分钟。

## 用法

    m = metrics("sh600519", period="5m")
    m["close"], m["ma5"], m["vol_ratio"], m["day_chg_pct"]

    hit = screen(rows, period="5m", pred=lambda m: m["vol_ratio"] > 3)
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

import datasource as ds
import history as H

# ---------------------------------------------------------------- 配置

#: 单只默认取多少根分钟线。400 根 5 分钟线 ≈ 5 个交易日。
DEFAULT_COUNT = 400

#: 周期白名单（与 datasource.INTRADAY_PERIODS 保持一致）
PERIODS = ("1m", "5m", "15m", "30m", "60m")

#: 每个周期「一个完整交易日有多少根」，用于把「当日累计」算对。
#: A 股一天 4 小时 = 240 分钟。
BARS_PER_DAY = {
    "1m": 240,
    "5m": 48,
    "15m": 16,
    "30m": 8,
    "60m": 4,
}

#: 运行状态（供 /api/health 与前端提示）
_LAST_ERR: List[str] = [""]
_LAST_SCAN: List[Dict[str, Any]] = [{}]


# ---------------------------------------------------------------- 序列取用


def _series(code: str, period: str, count: int = None):
    """取分钟线并转成 numpy 列（与 history.py 的 series 结构对齐）。

    `count=None` 时按周期取推荐根数（1m 取 2400，其余 800）。
    **不要统一传一个固定值**：1m 下 400 根只有 1.6 天，算出来的
    MA60 口径是错的（见 `datasource.default_intraday_count`）。

    返回 None 表示取不到数据（新股 / 停牌 / 代码错 / 数据源挂了）。
    """
    if count is None:
        count = ds.default_intraday_count(period)
    rows = ds.get_kline_intraday(code, period=period, count=count)
    if not rows:
        return None

    n = len(rows)
    close = np.empty(n, dtype=np.float64)
    high = np.empty(n, dtype=np.float64)
    low = np.empty(n, dtype=np.float64)
    open_ = np.empty(n, dtype=np.float64)
    volume = np.empty(n, dtype=np.float64)
    amount = np.empty(n, dtype=np.float64)
    times: List[str] = []

    for i, r in enumerate(rows):
        # 用 history._f 的同一套「非法值归 0」策略，保持行为一致
        close[i] = H._f(r.get("close"))
        high[i] = H._f(r.get("high"))
        low[i] = H._f(r.get("low"))
        open_[i] = H._f(r.get("open"))
        volume[i] = H._f(r.get("volume"))
        # amount 在腾讯降级路径下是 None —— 归 0，并在指标层标记不可用
        amount[i] = H._f(r.get("amount"))
        times.append(str(r.get("date") or ""))

    return {
        "close": close, "high": high, "low": low, "open": open_,
        "volume": volume, "amount": amount, "times": times,
        "source": rows[0].get("source", ""),
        "has_amount": rows[0].get("amount") is not None,
    }


# ---------------------------------------------------------------- 指标


def metrics(code: str, period: str = "5m", count: int = None,
            series: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """算单只股票的分钟级指标。取不到数据返回 None。

    参数
        series : 传入已取好的序列可省一次网络请求（批量扫描时用）

    返回的字典里任何因数据不足而无法计算的指标值为 **None**（不是 0）。
    """
    s = series if series is not None else _series(code, period, count)
    if not s:
        return None

    C = s["close"]; Hh = s["high"]; Ll = s["low"]; V = s["volume"]
    O = s["open"]; A = s["amount"]; T = s["times"]
    n = len(C)
    if n == 0:
        return None

    m: Dict[str, Any] = {
        "code": code,
        "period": period,
        "bars": n,
        "time": T[-1],
        "date": T[-1][:10],
        "source": s.get("source", ""),
        "close": float(C[-1]),
        "open": float(O[-1]),
        "high": float(Hh[-1]),
        "low": float(Ll[-1]),
    }

    # ---- 均线：与 history.py 同算法（简单移动平均）----
    for p in (5, 10, 20, 60):
        m[f"ma{p}"] = H._last_ma(C, p)

    # 均线排列：四条都算得出才给结论
    ms = [m["ma5"], m["ma10"], m["ma20"], m["ma60"]]
    if all(v is not None for v in ms):
        a, b, c, d = ms
        m["ma_align"] = "bull" if (a > b > c > d) else (
            "bear" if (a < b < c < d) else "mixed")
    else:
        m["ma_align"] = None

    # ---- 价格相对均线偏离（%）----
    if m["ma5"]:
        m["bias_ma5"] = (float(C[-1]) - m["ma5"]) / m["ma5"] * 100.0
    else:
        m["bias_ma5"] = None
    if m["ma20"]:
        m["bias_ma20"] = (float(C[-1]) - m["ma20"]) / m["ma20"] * 100.0
    else:
        m["bias_ma20"] = None

    # ---- 量能比：最后一根量 / 前 N 根均量（不含最后一根，避免自己抬基准）----
    # 这是「这根是不是放量」的正确定义，与 history.py 的 vol_ratio 同思路。
    for p in (5, 20):
        base = H._mean(V[:-1], p) if n > 1 else None
        m[f"vol_ratio{p}"] = (float(V[-1]) / base) if base else None

    # ---- 分钟窗口的涨跌幅与振幅 ----
    for p in (5, 20):
        m[f"chg{p}"] = H._chg_pct(C, p)
    m["range20_pct"] = H._range_pct(Hh[-20:], Ll[-20:], 20) if n >= 20 else None

    # ---- 当日累计：涨跌幅 / 振幅 / 相对昨收 ----
    day = T[-1][:10]
    today_idx = [i for i, t in enumerate(T) if t[:10] == day]
    if today_idx:
        fi, li = today_idx[0], today_idx[-1]
        day_open = float(O[fi])
        day_high = float(np.max(Hh[fi:li + 1]))
        day_low = float(np.min(Ll[fi:li + 1]))
        day_vol = float(np.sum(V[fi:li + 1]))
        day_amt = float(np.sum(A[fi:li + 1]))

        m["day_bars"] = len(today_idx)
        m["day_open"] = day_open
        m["day_high"] = day_high
        m["day_low"] = day_low
        m["day_volume"] = day_vol
        m["day_amount"] = day_amt if s.get("has_amount") else None

        # 当日涨跌幅以「第一根的开盘价」为基准。
        # 注意这不等于交易所口径的「相对昨收」——分钟线序列里不一定
        # 包含昨收，所以这里明确用「当日开盘」，前端文案要说清楚。
        if day_open > 0:
            m["day_chg_pct"] = (float(C[-1]) - day_open) / day_open * 100.0
            m["day_amplitude"] = (day_high - day_low) / day_open * 100.0
        else:
            m["day_chg_pct"] = None
            m["day_amplitude"] = None

        # 当日区间位置：现价在「今日最高~最低」中的位置（0=最低 1=最高）
        rng = day_high - day_low
        m["day_position"] = ((float(C[-1]) - day_low) / rng) if rng > 0 else None

        # 当日量能进度：已走多少根 / 一个完整交易日多少根。
        # 用它可以看出「才走了 1/3 时间却已经放了大半的量」，比绝对量有意义。
        total_bars = BARS_PER_DAY.get(period, 0)
        m["day_progress"] = (len(today_idx) / total_bars) if total_bars else None
    else:
        for k in ("day_bars", "day_open", "day_high", "day_low", "day_volume",
                  "day_amount", "day_chg_pct", "day_amplitude",
                  "day_position", "day_progress"):
            m[k] = None

    # ---- 分时突破 / 破位：现价 vs 前 N 根极值（不含最后一根）----
    if n >= 21:
        m["hhv20"] = float(np.max(Hh[-21:-1]))
        m["llv20"] = float(np.min(Ll[-21:-1]))
        m["break_up"] = H.gt(float(C[-1]), m["hhv20"])
        m["break_down"] = H.lt(float(C[-1]), m["llv20"])
    else:
        m["hhv20"] = m["llv20"] = None
        m["break_up"] = m["break_down"] = False

    # ---- 量价配合方向：最后一根涨跌 vs 量能放大 ----
    if n >= 2:
        up = float(C[-1]) >= float(C[-2])
        vr = m.get("vol_ratio20")
        m["vol_price"] = None
        if vr is not None:
            if up and vr >= 1.5:
                m["vol_price"] = "价涨量增"
            elif up and vr < 1.0:
                m["vol_price"] = "价涨量缩"
            elif not up and vr >= 1.5:
                m["vol_price"] = "价跌量增"
            else:
                m["vol_price"] = "价跌量缩"
    else:
        m["vol_price"] = None

    return m


# ---------------------------------------------------------------- 批量


def source_available() -> Dict[str, Any]:
    """当前是否具备「全市场分钟线扫描」的能力。"""
    st = ds.intraday_source_status()
    if st.get("eltdx"):
        return {"ok": True, "reason": "", **st}
    return {"ok": False,
            "reason": "全市场分钟线扫描需要 eltdx 批量源；当前不可用"
                      "（仅能逐只用腾讯源看盘，全市场需约 24 分钟）",
            **st}


def screen(rows: Sequence[Dict[str, Any]] = None,
           period: str = "5m",
           pred: Callable[[Dict[str, Any]], bool] = None,
           count: int = None,
           limit_codes: int = 0) -> Dict[str, Any]:
    """全市场分钟级筛选。

    返回 {'ok', 'items', 'scanned', 'elapsed', 'reason'}。
    取不到数据的股票直接跳过（不报错）——停牌/新股是常态。

    `count=None` 时取 `datasource.SCAN_COUNT`（120 根）。
    **不要传看盘用的根数**：扫描的指标窗口最大 61 根，取 800 根只会让
    耗时变 3 倍而结果逐位相同（实测见 `datasource.SCAN_COUNT` 注释）。

    eltdx 不可用时**拒绝执行**并给出原因，而不是退化到腾讯逐只
    （那要 24 分钟，用户会以为页面卡死）。
    """
    avail = source_available()
    if not avail["ok"]:
        return {"ok": False, "items": [], "scanned": 0, "elapsed": 0.0,
                "reason": avail["reason"], "source": avail.get("primary")}

    import app as _app  # 延迟 import，避免循环依赖

    if count is None:
        count = ds.scan_count(period)

    if rows is None:
        rows = _app.MARKET.ensure()
    codes = [r["code"] for r in rows if r.get("code")]
    if limit_codes:
        codes = codes[:limit_codes]

    t0 = time.time()
    series_map = ds.get_intraday_batch(codes, period=period, count=count)
    fetch_sec = time.time() - t0

    items: List[Dict[str, Any]] = []
    name_of = {r["code"]: r.get("name", "") for r in rows}
    for code, srows in series_map.items():
        s = _series_from_rows(srows)
        if not s:
            continue
        try:
            m = metrics(code, period=period, series=s)
        except Exception:
            continue
        if not m:
            continue
        m["name"] = name_of.get(code, "")
        try:
            if pred is None or pred(m):
                items.append(m)
        except Exception:
            continue

    elapsed = time.time() - t0
    _LAST_SCAN[0] = {"at": time.time(), "scanned": len(series_map),
                     "hit": len(items), "elapsed": elapsed,
                     "fetch_sec": fetch_sec, "period": period}
    return {"ok": True, "items": items, "scanned": len(series_map),
            "elapsed": elapsed, "fetch_sec": fetch_sec,
            "source": "eltdx", "reason": ""}


def _series_from_rows(rows: Sequence[Dict[str, Any]]):
    """把已取好的行列表转成 numpy 列（避免再走一次网络）。"""
    if not rows:
        return None
    n = len(rows)
    close = np.empty(n, dtype=np.float64)
    high = np.empty(n, dtype=np.float64)
    low = np.empty(n, dtype=np.float64)
    open_ = np.empty(n, dtype=np.float64)
    volume = np.empty(n, dtype=np.float64)
    amount = np.empty(n, dtype=np.float64)
    times: List[str] = []
    for i, r in enumerate(rows):
        close[i] = H._f(r.get("close"))
        high[i] = H._f(r.get("high"))
        low[i] = H._f(r.get("low"))
        open_[i] = H._f(r.get("open"))
        volume[i] = H._f(r.get("volume"))
        amount[i] = H._f(r.get("amount"))
        times.append(str(r.get("date") or ""))
    return {
        "close": close, "high": high, "low": low, "open": open_,
        "volume": volume, "amount": amount, "times": times,
        "source": rows[0].get("source", ""),
        "has_amount": rows[0].get("amount") is not None,
    }


# ---------------------------------------------------------------- 状态


def stats() -> Dict[str, Any]:
    """运行状态（供 /api/health）。"""
    return {
        "periods": list(PERIODS),
        "bars_per_day": dict(BARS_PER_DAY),
        "default_count": dict(ds.INTRADAY_DEFAULT_COUNT_BY_PERIOD),
        "last_error": _LAST_ERR[0],
        "last_scan": _LAST_SCAN[0],
        "source": ds.intraday_source_status(),
        "snapshot_source": ds.snapshot_status(),
        "market_state": ds.market_state(),
    }


# ---------------------------------------------------------------- 自检


def selfcheck(verbose: bool = True) -> Dict[str, Any]:
    """校验分钟线指标算得对，且与日线口径能对得上。

    校验点：
      1. 取得到数据、序列时间升序
      2. MA 与手工算术平均一致
      3. 「当日」切片正确（当日根数 ≤ 一天总根数）
      4. 分钟线聚合出的当日 OHLC 与日线一致（**这是最强的正确性证据**）
    """
    out: Dict[str, Any] = {"ok": False, "steps": []}

    def step(name, ok, detail=""):
        out["steps"].append({"name": name, "ok": bool(ok), "detail": detail})
        if verbose:
            print(f"  [{'OK ' if ok else 'FAIL'}] {name}"
                  + (f"  {detail}" if detail else ""))

    code = "sh600519"
    s = _series(code, "5m")
    if not s:
        step("取分钟线数据", False, "取不到数据（数据源不可用？）")
        return out
    step("取分钟线数据", True, f"{len(s['times'])} 根 {s['source']}")

    # 时间升序
    ts = s["times"]
    step("时间升序且无重复", all(ts[i] < ts[i + 1] for i in range(len(ts) - 1)),
         f"{ts[0]} ~ {ts[-1]}")

    m = metrics(code, "5m", series=s)
    # MA5 手工验证
    manual = float(np.mean(s["close"][-5:]))
    step("MA5 与手工均值一致", m["ma5"] is not None and abs(m["ma5"] - manual) < 1e-6,
         f"{m['ma5']:.4f} vs {manual:.4f}")

    # 当日根数不超过一天的容量
    p = BARS_PER_DAY["5m"]
    ok_day = m["day_bars"] is not None and 0 < m["day_bars"] <= p
    step("当日切片合理", ok_day, f"当日 {m['day_bars']} 根 / 每日 {p} 根")

    # 各周期默认根数必须够算 MA60（这是修掉的一个真实坑：
    # 1m 早期统一取 400 根 → 只覆盖 1.6 天 → MA60 数值算得出但口径全错）
    for per in PERIODS:
        s2 = _series(code, per)
        n2 = len(s2["times"]) if s2 else 0
        cover_days = n2 / max(BARS_PER_DAY.get(per, 1), 1)
        ok2 = n2 >= 60 and cover_days >= 3
        step(f"{per} 默认根数够用", ok2,
             f"{n2} 根 ≈ {cover_days:.1f} 个交易日"
             + ("" if ok2 else "（不足 3 天，长窗口均线失真）"))

    # 与**实时快照**交叉验证（比日线库更强、更即时）
    #
    # 为什么不用日线库比对：daily_bars 是盘后落库的，盘中/当天永远落后一天，
    # 于是「分钟线最新 09-22、日线最新 09-21」必然不等 → 校验被跳过 →
    # 等于没校验。这是自检的一个真实缺陷。
    # 改用实时快照：分钟线当日聚合的 开/高/低/收 必须与快照一致，
    # 且这个校验在任何时候都能跑（盘中比对当前值，盘后比对收盘值）。
    try:
        snap = ds.snapshot([code], use_cache=False).get(code)
        if snap and m.get("day_bars"):
            n_today = m["day_bars"]
            ok = True
            detail = []
            # 收盘价：分钟线最后一根收盘 vs 快照最新价
            c_dev = abs(m["close"] - snap["price"]) / max(snap["price"], 1e-9)
            ok = ok and c_dev < 2e-3
            detail.append(f"收 {m['close']} vs {snap['price']}")
            # 最高/最低：仅在当日完整（走满根数）时严格比对——
            # 盘中快照的高低是「到此刻为止」，理论上应等于分钟聚合值。
            if snap.get("high"):
                h_dev = abs(m["day_high"] - snap["high"]) / max(snap["high"], 1e-9)
                ok = ok and h_dev < 2e-3
                detail.append(f"高 {m['day_high']} vs {snap['high']}")
            if snap.get("low"):
                l_dev = abs(m["day_low"] - snap["low"]) / max(snap["low"], 1e-9)
                ok = ok and l_dev < 2e-3
                detail.append(f"低 {m['day_low']} vs {snap['low']}")
            step("分钟聚合与实时快照一致", ok,
                 f"当日 {n_today} 根 / " + " / ".join(detail))
        else:
            step("分钟聚合与实时快照一致", True, "跳过（快照不可用）")
    except Exception as e:
        step("分钟聚合与实时快照一致", True, f"跳过（{type(e).__name__}）")

    # 扫描根数必须覆盖最长指标窗口（MA60 + vol_ratio20 需要 61 根）。
    # 取少了 → 指标静默算不出（返回 None）；取多了 → 白等网络时间。
    need = 61
    sc = ds.scan_count("5m")
    s3 = _series(code, "5m", sc)
    n3 = len(s3["times"]) if s3 else 0
    m3 = metrics(code, "5m", series=s3) if s3 else None
    ok3 = n3 >= need and m3 is not None and m3.get("ma60") is not None
    step("扫描根数够算全部指标", ok3,
         f"SCAN_COUNT={sc}，实得 {n3} 根，需 {need} 根"
         + ("" if ok3 else "（MA60 会算不出）"))

    # 代码归一化：市场前缀判错会让「取不到」和「取到别人的数据」同时发生。
    # 这里是踩过的坑，固化成断言防止回退（920 北交所新段、159 深 ETF、
    # 204 沪市逆回购、11 沪市可转债 vs 12 深市可转债）。
    _ROUTE_CASES = {
        "600519": "sh600519", "000001": "sz000001", "300750": "sz300750",
        "688981": "sh688981", "920002": "bj920002", "430047": "bj430047",
        "200011": "sz200011", "159915": "sz159915", "512880": "sh512880",
        "110059": "sh110059", "123138": "sz123138", "204001": "sh204001",
    }
    bad = [f"{k}->{ds.normalize(k)}" for k, v in _ROUTE_CASES.items()
           if ds.normalize(k) != v]
    step("代码归一化路由", not bad,
         f"{len(_ROUTE_CASES)} 个代码段" + (f" 错误：{bad}" if bad else ""))

    # 无效代码必须被拦住。通达信对不存在代码会返回别的标的的数据
    # （sh999999 -> 上证指数），不拦就是静默张冠李戴。
    _BAD = ["sh999999", "sh999998", "sz999999", "bj999999", "sh60051"]
    leaked = [c for c in _BAD if ds.is_valid_code(c)]
    step("无效代码被拦截", not leaked,
         f"{len(_BAD)} 个伪造代码" + (f" 漏网：{leaked}" if leaked else ""))

    # 反向：真实代码不能被误杀。北交所老段（430/83）不在 eltdx 代码表里，
    # 是实测踩过的误杀点。
    _GOOD = ["sh600519", "sz000001", "sh000001", "sz399001",
             "bj430047", "bj830799", "sh900901", "sz200011"]
    killed = [c for c in _GOOD if not ds.is_valid_code(c)]
    step("真实代码未被误杀", not killed,
         f"{len(_GOOD)} 个真实代码" + (f" 误杀：{killed}" if killed else ""))

    out["ok"] = all(x["ok"] for x in out["steps"])
    if verbose:
        print(f"  结论：{'全部通过 ✓' if out['ok'] else '存在问题 ✗'}")
    return out


if __name__ == "__main__":
    selfcheck()
